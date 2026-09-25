"""harvester-ops : se connecter par Rancher, avec les droits de Rancher (v1.50.0).

Rancher (2.12 et suivants) a un fournisseur OIDC intégré. La console y est
déclarée comme client ; un utilisateur déjà connecté à Rancher entre dans la
console sans rien ressaisir. Ensuite, tout ce que la console fait sur un
cluster géré par ce Rancher passe par le mandataire de Rancher
(`/k8s/clusters/<id>`) avec le jeton de l'utilisateur : Rancher applique ses
propres droits, rien n'est recopié.

Ce module ne dépend que de la bibliothèque standard (livrable airgap). Il ne
vérifie pas la signature du jeton d'identité : celui-ci arrive directement
du point de jeton de Rancher, en TLS, contre le secret du client (OIDC Core
3.1.3.7), et l'identité est ensuite relue auprès de l'API de Rancher avec le
jeton lui-même. Rancher reste l'autorité.

Voir docs/design/2026-09-26-connexion-par-rancher.md.
"""

import base64
import hashlib
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

COOKIE = "hops_session"
PENDING_COOKIE = "hops_login"
PENDING_TTL = 600
DEFAULT_SESSION_HOURS = 12
ROLES = ("viewer", "operator", "admin")


class SSOError(Exception):
    """Un refus de connexion, avec un code que la page sait traduire."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def settings(cfg):
    """La section `rancher:` de la configuration, complétée ; None si la
    connexion par Rancher n'est pas configurée (ou pas utilisable)."""
    r = (cfg or {}).get("rancher") or {}
    url = str(r.get("url") or "").rstrip("/")
    cid = str(r.get("client_id") or "").strip()
    secret_file = r.get("client_secret_file")
    if not (url and cid and secret_file):
        return None
    try:
        secret = Path(secret_file).read_text().strip()
    except OSError:
        return None
    if not secret:
        return None
    role = r.get("default_role") or "operator"
    return {
        "url": url, "client_id": cid, "client_secret": secret,
        "issuer": f"{url}/oidc",
        "ca_file": r.get("ca_file") or None,
        "redirect_uri": r.get("redirect_uri") or None,
        "default_role": role if role in ROLES else "operator",
        "admin_groups": [str(g) for g in (r.get("admin_groups") or [])],
        "session_seconds": int(float(r.get("session_hours") or DEFAULT_SESSION_HOURS) * 3600),
        "label": r.get("label") or "Rancher",
    }


# ---------------------------------------------------------------------------
# HTTP (remplaçable dans les tests)
# ---------------------------------------------------------------------------

class Http:
    def __init__(self, ca_file=None, timeout=15):
        self.ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
        self.timeout = timeout

    def request(self, method, url, headers=None, data=None):
        body = None
        hdrs = dict(headers or {})
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, (bytes, str)):
            body = data.encode() if isinstance(data, str) else data
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self.ctx if url.startswith("https") else None) as r:
                raw = r.read().decode()
                status = r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read().decode(errors="replace"), e.code
        except (urllib.error.URLError, OSError) as e:
            raise SSOError("rancher-unreachable", str(getattr(e, "reason", e))[:200])
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, {"raw": raw[:300]}


def _bearer(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ---------------------------------------------------------------------------
# Le code d'autorisation
# ---------------------------------------------------------------------------

def pkce_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class PendingLogins:
    """Les connexions en cours : `state` à usage unique, lié au navigateur
    par une clé posée en cookie, oublié au bout de 10 minutes."""

    def __init__(self, now=time.time):
        self._d = {}
        self._lock = threading.Lock()
        self.now = now

    def start(self, next_path="/"):
        verifier, challenge = pkce_pair()
        state, nonce, browser = (secrets.token_urlsafe(24) for _ in range(3))
        with self._lock:
            self._purge()
            self._d[state] = {"verifier": verifier, "nonce": nonce, "browser": browser,
                              "created": self.now(), "next": next_path}
        return state, nonce, challenge, browser

    def take(self, state, browser):
        with self._lock:
            self._purge()
            p = self._d.pop(state or "", None)
        if p is None:
            raise SSOError("state-unknown")
        if not browser or not secrets.compare_digest(p["browser"], browser):
            raise SSOError("state-browser")
        return p

    def _purge(self):
        limit = self.now() - PENDING_TTL
        for k in [k for k, v in self._d.items() if v["created"] < limit]:
            del self._d[k]


def authorize_url(s, redirect_uri, state, nonce, challenge):
    return s["issuer"] + "/authorize?" + urllib.parse.urlencode({
        "client_id": s["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": "openid profile offline_access", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256"})


def exchange_code(http, s, code, verifier, redirect_uri):
    basic = base64.b64encode(f"{s['client_id']}:{s['client_secret']}".encode()).decode()
    status, tok = http.request("POST", s["issuer"] + "/token",
                               headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
                               data={"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": redirect_uri, "code_verifier": verifier})
    if status != 200 or not tok.get("access_token") or not tok.get("id_token"):
        raise SSOError("token-refused", str(tok.get("error_description") or tok.get("error") or status)[:200])
    return tok


def claims_of(jwt):
    """Le contenu d'un JWT, sans vérifier la signature (voir l'en-tête du
    module : le jeton vient du point de jeton, en TLS, contre notre secret)."""
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        raise SSOError("token-malformed")


def check_id_token(claims, s, nonce, now=None):
    now = time.time() if now is None else now
    if claims.get("iss") != s["issuer"]:
        raise SSOError("token-issuer", str(claims.get("iss")))
    aud = claims.get("aud")
    if s["client_id"] not in (aud if isinstance(aud, list) else [aud]):
        raise SSOError("token-audience")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - 60:
        raise SSOError("token-expired")
    if not nonce or not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise SSOError("token-nonce")
    if not claims.get("sub"):
        raise SSOError("token-subject")
    return claims


# ---------------------------------------------------------------------------
# L'identité, relue auprès de Rancher
# ---------------------------------------------------------------------------

def rancher_identity(http, s, token):
    """{'id', 'username', 'name', 'groups': [...], 'global_roles': [...],
    'admin': bool} de l'utilisateur que porte `token`."""
    status, me = http.request("GET", s["url"] + "/v3/users?me=true", headers=_bearer(token))
    users = (me or {}).get("data") or []
    if status != 200 or not users:
        raise SSOError("identity-refused", str(status))
    u = users[0]
    uid = u.get("id")
    status, grb = http.request("GET", s["url"] + "/v3/globalrolebindings?userId=" + urllib.parse.quote(uid),
                               headers=_bearer(token))
    roles = sorted({b.get("globalRoleId") for b in (grb or {}).get("data") or []
                    if b.get("userId") == uid and b.get("globalRoleId")})
    status, pr = http.request("GET", s["url"] + "/v3/principals", headers=_bearer(token))
    groups = sorted({p.get("id") for p in (pr or {}).get("data") or []
                     if p.get("principalType") == "group" and p.get("id")})
    return {"id": uid, "username": u.get("username") or uid, "name": u.get("name") or "",
            "groups": groups, "global_roles": roles, "admin": "admin" in roles}


def console_role(s, ident):
    """Le rôle dans la console : administrateur pour un administrateur de
    Rancher ou un membre d'un groupe désigné, sinon le rôle par défaut. Les
    gestes sur les clusters restent jugés par Rancher."""
    if ident.get("admin") or set(ident.get("groups") or []) & set(s["admin_groups"]):
        return "admin"
    return s["default_role"]


def refresh(http, s, refresh_token):
    """Un jeton d'accès neuf à partir du jeton de rafraîchissement.

    Vu sur Rancher 2.14 : `expires_in` y est donné en NANOSECONDES
    (600000000000 pour 10 minutes) ; seule la date `exp` du jeton fait foi.
    Et le jeton OIDC ne peut ni créer ni supprimer de jeton Rancher (401) :
    d'où le rafraîchissement plutôt qu'un jeton dérivé de longue durée."""
    basic = base64.b64encode(f"{s['client_id']}:{s['client_secret']}".encode()).decode()
    status, tok = http.request("POST", s["issuer"] + "/token",
                               headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
                               data={"grant_type": "refresh_token", "refresh_token": refresh_token})
    if status != 200 or not tok.get("access_token"):
        raise SSOError("refresh-refused", str(tok.get("error_description") or tok.get("error") or status)[:200])
    return tok


def expiry_of(access_token):
    try:
        exp = claims_of(access_token).get("exp")
    except SSOError:
        exp = None
    return float(exp) if isinstance(exp, (int, float)) else time.time() + 300


# ---------------------------------------------------------------------------
# Le cluster Rancher d'un cluster de la console
# ---------------------------------------------------------------------------

def discover_cluster_id(http, s, token, kube_system_uid):
    """L'id Rancher du cluster dont `kube-system` a cet UID : le même objet
    vu de la console et à travers Rancher. None si l'utilisateur ne le voit
    pas (pas géré par ce Rancher, ou pas de droit)."""
    if not kube_system_uid:
        return None
    status, out = http.request("GET", s["url"] + "/v3/clusters", headers=_bearer(token))
    for c in (out or {}).get("data") or []:
        cid = c.get("id")
        # un cluster « pending » ou « unavailable » ferait attendre le délai
        # du mandataire pour rien (vu : 6 s à la connexion d'un administrateur)
        if not cid or c.get("state", "active") != "active":
            continue
        st, ns = http.request("GET", f"{s['url']}/k8s/clusters/{cid}/api/v1/namespaces/kube-system",
                              headers=_bearer(token))
        if st == 200 and ((ns or {}).get("metadata") or {}).get("uid") == kube_system_uid:
            return cid
    return None


def kubeconfig_text(s, cid, token):
    cluster = {"server": f"{s['url']}/k8s/clusters/{cid}"}
    if s.get("ca_file"):
        cluster["certificate-authority"] = s["ca_file"]
    return json.dumps({
        "apiVersion": "v1", "kind": "Config", "current-context": "rancher",
        "clusters": [{"name": "rancher", "cluster": cluster}],
        "users": [{"name": "rancher-user", "user": {"token": token}}],
        "contexts": [{"name": "rancher", "context": {"cluster": "rancher", "user": "rancher-user"}}],
    })


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, sid, ident, role, token, refresh_token, expires):
        self.sid = sid
        self.ident = ident
        self.role = role
        self.token = token                     # jeton d'accès OIDC (court)
        self.refresh_token = refresh_token     # jamais hors du serveur
        self.token_expires = expiry_of(token)
        self.expires = expires                 # fin de la session
        self.kubeconfigs = {}                  # cid -> chemin
        self.lock = threading.Lock()

    @property
    def login(self):
        return f"{self.ident['username']}@rancher"

    def public(self):
        return {"user": self.login, "name": self.ident.get("name"), "rancher_id": self.ident["id"],
                "groups": self.ident.get("groups", []), "role": self.role,
                "expires": int(self.expires)}


class Sessions:
    """Les sessions ouvertes par Rancher, en mémoire. Le jeton ne quitte
    jamais le serveur ; ses kubeconfigs sont effacés à la fermeture."""

    def __init__(self, directory_fn, now=time.time):
        self._d = {}
        self._lock = threading.Lock()
        self.directory_fn = directory_fn
        self.now = now

    def open(self, ident, role, token, refresh_token, ttl):
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._d[sid] = Session(sid, ident, role, token, refresh_token, self.now() + ttl)
        return self._d[sid]

    def all(self):
        with self._lock:
            return list(self._d.values())

    def renew(self, session, http, settings_, margin=120):
        """Renouvelle le jeton d'accès s'il expire dans moins de `margin`
        secondes, et réécrit les kubeconfigs de la session : une action
        longue relit le fichier à chaque appel de kubectl. Rend False si
        Rancher refuse (session à fermer)."""
        with session.lock:
            if session.token_expires - self.now() > margin:
                return True
            try:
                tok = refresh(http, settings_, session.refresh_token)
            except SSOError:
                return False
            session.token = tok["access_token"]
            session.refresh_token = tok.get("refresh_token") or session.refresh_token
            session.token_expires = expiry_of(session.token)
            for cid, path in list(session.kubeconfigs.items()):
                _write_private(path, kubeconfig_text(settings_, cid, session.token))
            return True

    def get(self, sid):
        if not sid:
            return None
        with self._lock:
            s = self._d.get(sid)
        if s is None:
            return None
        if s.expires < self.now():
            self.close(sid)
            return None
        return s

    def close(self, sid):
        with self._lock:
            s = self._d.pop(sid, None)
        if s is not None:
            for p in s.kubeconfigs.values():
                try:
                    os.unlink(p)
                except OSError:
                    pass
        return s

    def expired(self):
        now = self.now()
        with self._lock:
            return [sid for sid, s in self._d.items() if s.expires < now]

    def kubeconfig(self, session, settings_, cid):
        """Le kubeconfig de cette session pour ce cluster Rancher (0600, dans
        le répertoire privé des identités)."""
        path = session.kubeconfigs.get(cid)
        if path and os.path.exists(path):
            return path
        d = self.directory_fn()
        if d is None:
            return None
        path = str(Path(d) / f"rancher-{hashlib.sha256((session.sid + cid).encode()).hexdigest()[:24]}.kubeconfig")
        _write_private(path, kubeconfig_text(settings_, cid, session.token))
        session.kubeconfigs[cid] = path
        return path


def _write_private(path, text):
    """Écrit en 0600 et remplace d'un coup : un kubectl qui lit le fichier
    pendant le renouvellement voit l'ancien ou le nouveau, jamais un morceau."""
    tmp = f"{path}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
