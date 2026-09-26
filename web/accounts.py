"""harvester-ops : les comptes de la console et leurs sessions (v1.57.0).

Jusqu'ici, sans fichier htpasswd, la console tournait OUVERTE : n'importe qui
atteignant le port pouvait éteindre un cluster. Elle exige maintenant une
connexion ; au premier démarrage, le premier administrateur se crée par
/setup avec un jeton écrit sur le disque du serveur (seul qui y a accès peut
le lire), comme le mot de passe initial d'un Jenkins.

- Les comptes créés depuis la console vivent dans `accounts.json`, dans
  l'état persistant du service (le répertoire de configuration est en lecture
  seule dans le conteneur). Le htpasswd posé par l'installeur reste lu.
- Les mots de passe sont hachés en bcrypt ; aucun n'est jamais rendu.
- Une session de navigateur est un identifiant aléatoire (cookie HttpOnly),
  gardé en mémoire côté serveur ; se déconnecter l'oublie. Changer un mot de
  passe ou supprimer un compte ferme ses sessions.
"""

import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from passlib.hash import bcrypt

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
PASSWORD_MIN = 12
ROLES = ("viewer", "operator", "admin")
COOKIE = "hops_session"


_DUMMY = []


def _dummy_hash():
    if not _DUMMY:
        _DUMMY.append(bcrypt.hash(secrets.token_hex(8)))
    return _DUMMY[0]


class AccountError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def check_password_policy(password, username=""):
    if not isinstance(password, str) or len(password) < PASSWORD_MIN:
        raise AccountError("password-short", f"the password needs at least {PASSWORD_MIN} characters")
    if username and username.lower() in password.lower():
        raise AccountError("password-name", "the password must not contain the account name")


class Accounts:
    """Comptes gérés par la console (fichier JSON 0600, écrit d'un coup)."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"users": {}}
        if not isinstance(data.get("users"), dict):
            data["users"] = {}
        return data

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{secrets.token_hex(4)}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def list(self):
        return {name: {k: v for k, v in u.items() if k != "hash"}
                for name, u in self._read()["users"].items()}

    def exists(self):
        return bool(self._read()["users"])

    def role_of(self, name):
        u = self._read()["users"].get(name)
        return u.get("role") if u else None

    def has(self, name):
        return name in self._read()["users"]

    def verify(self, name, password):
        u = self._read()["users"].get(name)
        if not u or not isinstance(password, str):
            # même coût qu'un vrai contrôle : ne pas dire quels comptes existent
            bcrypt.verify("x", _dummy_hash())
            return False
        try:
            return bcrypt.verify(password, u["hash"])
        except (ValueError, TypeError):
            return False

    def create(self, name, password, role, by=""):
        if not USERNAME_RE.match(name or ""):
            raise AccountError("name-invalid", "2 to 32 characters: lower-case letters, digits, dot, dash, underscore")
        if role not in ROLES:
            raise AccountError("role-invalid", f"role must be one of {', '.join(ROLES)}")
        check_password_policy(password, name)
        with self._lock:
            data = self._read()
            if name in data["users"]:
                raise AccountError("name-taken", f"an account {name} already exists")
            data["users"][name] = {"hash": bcrypt.hash(password), "role": role,
                                   "created": int(time.time()), "created_by": by,
                                   "password_changed": int(time.time())}
            self._write(data)

    def set_password(self, name, password):
        check_password_policy(password, name)
        with self._lock:
            data = self._read()
            if name not in data["users"]:
                raise AccountError("not-found", f"no account {name}")
            data["users"][name]["hash"] = bcrypt.hash(password)
            data["users"][name]["password_changed"] = int(time.time())
            self._write(data)

    def set_role(self, name, role):
        if role not in ROLES:
            raise AccountError("role-invalid", f"role must be one of {', '.join(ROLES)}")
        with self._lock:
            data = self._read()
            if name not in data["users"]:
                raise AccountError("not-found", f"no account {name}")
            if data["users"][name].get("role") == "admin" and role != "admin" and self._admins(data) <= 1:
                raise AccountError("last-admin", "the last administrator cannot lose the role")
            data["users"][name]["role"] = role
            self._write(data)

    def delete(self, name):
        with self._lock:
            data = self._read()
            if name not in data["users"]:
                raise AccountError("not-found", f"no account {name}")
            if data["users"][name].get("role") == "admin" and self._admins(data) <= 1:
                raise AccountError("last-admin", "the last administrator cannot be deleted")
            del data["users"][name]
            self._write(data)

    @staticmethod
    def _admins(data):
        return sum(1 for u in data["users"].values() if u.get("role") == "admin")


class LocalSessions:
    """Sessions de navigateur des comptes locaux, en mémoire."""

    def __init__(self, ttl=12 * 3600, now=time.time):
        self.ttl = ttl
        self.now = now
        self._d = {}
        self._lock = threading.Lock()

    def open(self, user):
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._d[sid] = {"user": user, "expires": self.now() + self.ttl, "opened": self.now()}
        return sid

    def get(self, sid):
        if not sid:
            return None
        with self._lock:
            s = self._d.get(sid)
            if s and s["expires"] < self.now():
                del self._d[sid]
                s = None
        return dict(s, sid=sid) if s else None

    def close(self, sid):
        with self._lock:
            self._d.pop(sid, None)

    def close_user(self, user, keep=None):
        with self._lock:
            for sid in [k for k, v in self._d.items() if v["user"] == user and k != keep]:
                del self._d[sid]


# ---------------------------------------------------------------------------
# Premier démarrage : le jeton de création du premier administrateur
# ---------------------------------------------------------------------------

def ensure_setup_token(path):
    """Le jeton (créé s'il manque), dans un fichier 0600 lisible par qui
    administre le serveur. Rend (jeton, créé maintenant)."""
    p = Path(path)
    try:
        tok = p.read_text().strip()
        if tok:
            return tok, False
    except OSError:
        pass
    tok = secrets.token_urlsafe(18)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok, True


def check_setup_token(path, given):
    try:
        tok = Path(path).read_text().strip()
    except OSError:
        return False
    return bool(tok) and isinstance(given, str) and hmac.compare_digest(tok, given.strip())
