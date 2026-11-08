"""Magasin de secrets de la console (2.4.0, supports multiples en 2.9.0).

Interface unique par laquelle passent les secrets de la console : lire,
écrire, supprimer, désigner par référence, remettre une copie à un outil.

Une référence a la forme `<support>:<classe>/<chemin>#<champ>` :

* `local:` désigne l'emplacement d'aujourd'hui : la mémoire pour les classes
  non persistantes (`plan-run`, 2.4.0), les fichiers et la base de la
  réplication pour les classes persistantes (support fichier, `FileStore`,
  qui ne change aucun fichier : il range l'existant derrière l'interface) ;
* `openbao:` désigne un secret dans OpenBao (`OpenBaoStore`), sous
  `<montage>/data/<préfixe>/<classe>/<chemin>`, champ `<champ>` ; le montage
  et le préfixe viennent des réglages, jamais de la référence. Refusé tant
  qu'OpenBao n'est pas actif (`openbao-inactive`).

Une valeur locale peut elle-même être une référence `openbao:` (après
migration, la valeur est remplacée par sa référence) : le magasin la suit.
Une valeur qui n'est pas une référence (chemin de kubeconfig, valeur d'un
en-tête...) est rendue telle quelle par `resolve_value` et `path_of` : rien
ne change tant qu'aucune référence n'a été posée.

Règle de chemin d'un déroulé : `plan-run/<action_id>/<nom>`, effacé par
`delete_tree("plan-run", <action_id>)` dans le `finally` du moteur.

Jamais de valeur dans un message d'erreur, une représentation, une
sérialisation, ni sur un disque persistant : le cache d'OpenBao est en
mémoire, les copies remises aux outils (`file`, `shared_file`) vivent sur
tmpfs, 0600 dans un répertoire 0700.
"""

import atexit
import contextlib
import json
import os
import re
import secrets as _secrets
import sqlite3
import threading
import time
from pathlib import Path

import secret_classes

REF_RE = re.compile(r"^(?P<backend>local|openbao):(?P<cls>[a-z0-9-]+)/(?P<path>[A-Za-z0-9._/-]{1,200})"
                    r"#(?P<field>[a-z0-9_]{1,64})$")
# 2.4.0 : chemins d'un déroulé en minuscules ; les classes persistantes
# portent des noms de cluster ou d'hôte, où la casse et `_` sont permis.
_LOWER_PATH_RE = re.compile(r"^[a-z0-9._/-]{1,200}$")
# Classe -> {"persist"} : `plan-run` vit en mémoire (2.4.0) ; les autres
# ont un emplacement local (support fichier) et peuvent migrer dans OpenBao.
CLASSES = {
    "plan-run": {"persist": False},
    "kubeconfig": {"persist": True},
    "ssh-cluster": {"persist": True},
    "ssh-kvm": {"persist": True},
    "rancher-oidc": {"persist": True},
    "replication-notify": {"persist": True},
    "replication-hook": {"persist": True},
    "tf-kvm": {"persist": True},
}

SETTINGS_FILE = "secrets-settings.json"
OPENBAO_DIR_DEFAULT = "/etc/harvester-ops/openbao"
DEFAULTS = {
    "mode": "local",              # local | a | b1
    "addr": None,
    "mount": "harvester-ops",
    "prefix": None,               # consoles/<console_id>
    "console_id": None,
    "cache_ttl_min": 15,
    "timeout": 10,
    "migrated": {},               # classe -> {"at", "by"}
    "secret_files_dir": None,     # développement seulement, avec dev_disk_secret_files
    "dev_disk_secret_files": False,
}
CACHE_TTL_RANGE = (1, 120)
VALUE_MAX = 1 << 20               # 1 Mio
TMPFS_TYPES = ("tmpfs", "ramfs")
# Erreurs du client qui disent « OpenBao ne répond pas » (bandeau, cache).
DOWN_CODES = ("openbao-unreachable", "openbao-tls", "openbao-sealed", "openbao-internal")


class SecretMissing(KeyError):
    """Le secret désigné n'est pas (ou plus) dans le magasin. Le message ne
    porte que la référence : ni valeur, ni chemin de l'état."""

    def __init__(self, ref):
        super().__init__(ref)
        self.ref = ref

    def __str__(self):
        return f"secret not found: {self.ref}"


class SecretError(Exception):
    """Erreur du magasin avec un code stable (`openbao-inactive`,
    `secret-files-not-tmpfs`, `class-not-migratable`, `secret-too-big`, ou un
    code du client OpenBao), la classe et le nom visés. Jamais de valeur."""

    def __init__(self, code, cls=None, name=None, detail=""):
        self.code, self.cls, self.name, self.detail = code, cls, name, str(detail or "")[:300]
        super().__init__(code, cls, name, self.detail)

    def __str__(self):
        where = f" {self.cls}/{self.name}" if self.cls else ""
        return f"{self.code}:{where}" + (f" ({self.detail})" if self.detail else "")

    def __reduce__(self):
        return (SecretError, (self.code, self.cls, self.name, self.detail))


# ---------------------------------------------------------------------------
# Références
# ---------------------------------------------------------------------------
def _check_path(path):
    """Chemin relatif sans segment vide, `.` ni `..` ; ValueError sinon."""
    if not isinstance(path, str) or not path or path.startswith("/") or path.endswith("/"):
        raise ValueError("invalid secret path")
    for seg in path.split("/"):
        if seg in ("", ".", ".."):
            raise ValueError("invalid secret path")
    return path


def parse_ref_full(ref):
    """(support, classe, chemin, champ) ; ValueError si la référence est mal
    formée, de classe inconnue, ou si son chemin sort de sa classe."""
    m = REF_RE.match(ref) if isinstance(ref, str) else None
    if not m:
        raise ValueError("invalid secret reference")
    if m["cls"] not in CLASSES:
        raise ValueError("unknown secret class")
    if not CLASSES[m["cls"]]["persist"] and not _LOWER_PATH_RE.match(m["path"]):
        raise ValueError("invalid secret path")
    return m["backend"], m["cls"], _check_path(m["path"]), m["field"]


def parse_ref(ref):
    """(classe, chemin, champ) d'une référence (les deux supports)."""
    return parse_ref_full(ref)[1:]


def is_ref(s):
    try:
        parse_ref_full(s)
    except ValueError:
        return False
    return True


def is_openbao_ref(s):
    return is_ref(s) and s.startswith("openbao:")


def make_ref(cls, path, field, backend="local"):
    if backend not in ("local", "openbao"):
        raise ValueError("unknown secret backend")
    ref = f"{backend}:{cls}/{path}#{field}"
    parse_ref_full(ref)
    return ref


# ---------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------
def _state_dir(state_dir=None):
    if state_dir:
        return Path(os.path.abspath(str(state_dir)))
    import cluster_decl
    return cluster_decl.state_dir()


def load_settings(state_dir=None):
    """`<état>/secrets-settings.json` avec les valeurs par défaut ; un
    fichier absent ou illisible vaut le mode local."""
    out = dict(DEFAULTS, migrated={})
    try:
        raw = json.loads((_state_dir(state_dir) / SETTINGS_FILE).read_text())
    except (OSError, ValueError):
        return out
    if isinstance(raw, dict):
        for k in DEFAULTS:
            if k in raw:
                out[k] = raw[k]
    if not isinstance(out["migrated"], dict):
        out["migrated"] = {}
    try:
        ttl = int(out["cache_ttl_min"])
    except (TypeError, ValueError):
        ttl = DEFAULTS["cache_ttl_min"]
    out["cache_ttl_min"] = max(CACHE_TTL_RANGE[0], min(CACHE_TTL_RANGE[1], ttl))
    if not out["prefix"] and out["console_id"]:
        out["prefix"] = f"consoles/{out['console_id']}"
    return out


def openbao_active(settings):
    return bool(settings.get("mode") in ("a", "b1") and settings.get("addr") and settings.get("prefix"))


def openbao_dir():
    return Path(os.environ.get("HARVESTER_OPS_OPENBAO_DIR") or OPENBAO_DIR_DEFAULT)


# ---------------------------------------------------------------------------
# Fichiers de travail sur tmpfs
# ---------------------------------------------------------------------------
def _unescape_mount(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def fs_type(path, mountinfo=None):
    """Type du système de fichiers qui porte `path` (le point de montage le
    plus long qui le contient, lu dans /proc/self/mountinfo)."""
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text()
        except OSError:
            return None
    p = os.path.realpath(str(path))
    best, kind = "", None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        f = left.split()
        if not sep or len(f) < 5:
            continue
        mnt = _unescape_mount(f[4])
        if (p == mnt or p.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
            best, kind = mnt, (right.split() or [None])[0]
    return kind


def _nearest_existing(p):
    p = Path(p)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def secret_files_dir(settings=None, mountinfo=None, env=None):
    """Répertoire 0700 des copies de travail, sur tmpfs : `$XDG_RUNTIME_DIR/
    hops-secrets` (développement), sinon `/tmp/hops-secrets` (le conteneur
    monte `--tmpfs /tmp`). Ailleurs : `secret-files-not-tmpfs`, sauf réglage
    de développement explicite (`dev_disk_secret_files` et
    `secret_files_dir`, point 256)."""
    settings = settings or DEFAULTS
    env = os.environ if env is None else env
    cands = []
    if settings.get("secret_files_dir"):
        cands.append(Path(settings["secret_files_dir"]))
    if env.get("XDG_RUNTIME_DIR"):
        cands.append(Path(env["XDG_RUNTIME_DIR"]) / "hops-secrets")
    cands.append(Path("/tmp/hops-secrets"))
    chosen = None
    for c in cands:
        if fs_type(_nearest_existing(c), mountinfo) in TMPFS_TYPES:
            chosen = c
            break
    if chosen is None:
        if settings.get("dev_disk_secret_files") and settings.get("secret_files_dir"):
            chosen = Path(settings["secret_files_dir"])
        else:
            raise SecretError("secret-files-not-tmpfs", detail="no tmpfs directory for secret working copies")
    old = os.umask(0o077)
    try:
        chosen.mkdir(mode=0o700, parents=True, exist_ok=True)
    finally:
        os.umask(old)
    st = os.lstat(chosen)
    if not os.path.isdir(chosen) or os.path.islink(chosen) or st.st_uid != os.geteuid():
        raise SecretError("secret-files-not-tmpfs", detail="secret working directory not owned by the console")
    os.chmod(chosen, 0o700)
    return chosen


_PURPOSE_RE = re.compile(r"[^a-z0-9-]")


def write_private_copy(directory, purpose, value):
    """Fichier 0600 neuf (O_EXCL, sans lien suivi) dans `directory`."""
    name = f"{_PURPOSE_RE.sub('-', str(purpose).lower())[:40] or 'secret'}-{_secrets.token_hex(8)}"
    p = Path(directory) / name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    return p


def _unlink(p):
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Supports
# ---------------------------------------------------------------------------
class Store:
    """Interface commune des supports."""

    def put(self, cls, path, field, value):                 # pragma: no cover - interface
        raise NotImplementedError

    def get(self, ref):                                      # pragma: no cover - interface
        raise NotImplementedError

    def delete(self, ref):                                   # pragma: no cover - interface
        raise NotImplementedError

    def delete_tree(self, cls, path):                        # pragma: no cover - interface
        raise NotImplementedError

    def refs(self, cls, path):                               # pragma: no cover - interface
        raise NotImplementedError


class LocalStore(Store):
    """Support local : un dictionnaire en mémoire sous verrou."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}

    def __repr__(self):
        with self._lock:
            n = len(self._data)
        return f"<{type(self).__name__} {n} secret(s)>"

    __str__ = __repr__

    def __getstate__(self):
        # Un magasin ne se sérialise pas avec ses valeurs.
        return {}

    def __setstate__(self, _state):
        self.__init__()

    def put(self, cls, path, field, value):
        ref = make_ref(cls, path, field)
        if CLASSES[cls]["persist"]:
            raise NotImplementedError("persistent secret classes arrive with a file or OpenBao backend")
        if not isinstance(value, str):
            raise TypeError("a secret value is a string")
        with self._lock:
            self._data[ref] = value
        return ref

    def get(self, ref):
        try:
            parse_ref(ref)
        except ValueError:
            raise SecretMissing(ref if isinstance(ref, str) else "(invalid)") from None
        with self._lock:
            try:
                return self._data[ref]
            except KeyError:
                raise SecretMissing(ref) from None

    def delete(self, ref):
        parse_ref(ref)
        with self._lock:
            return self._data.pop(ref, None) is not None

    @staticmethod
    def _in_tree(cls, path):
        """Prédicat « la référence est sous `<classe>/<chemin>` » : le chemin
        lui-même (`...#champ`) et ce qui est dessous (`.../...`). Séparateur
        exigé : `plan-run/a1` ne désigne pas `plan-run/a10`."""
        if cls not in CLASSES:
            raise ValueError("unknown secret class")
        base = f"local:{cls}/{_check_path(path)}"
        return lambda r: r.startswith(base + "/") or r.startswith(base + "#")

    def delete_tree(self, cls, path):
        inside = self._in_tree(cls, path)
        with self._lock:
            gone = [r for r in self._data if inside(r)]
            for r in gone:
                del self._data[r]
        return len(gone)

    def refs(self, cls, path):
        inside = self._in_tree(cls, path)
        with self._lock:
            return sorted(r for r in self._data if inside(r))


def _read_yaml(path):
    text = Path(path).read_text()
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:            # pragma: no cover  (outils bin/ sans PyYAML)
        return json.loads(text)


def _dig(doc, dotted):
    for k in dotted.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(k)
    return doc


class FileStore:
    """Support fichier : les emplacements d'aujourd'hui, sans rien changer.
    `local:kubeconfig/<n>#kubeconfig` désigne le kubeconfig de la
    déclaration du cluster (ou `<état>/kubeconfigs/<n>.yaml`), etc. ; la
    table des emplacements est dans `secret_classes.local_location`."""

    def __init__(self, state_dir, tf_dir=None, repl_db=None):
        self.state_dir = Path(state_dir)
        self.tf_dir = Path(tf_dir) if tf_dir else Path(
            os.environ.get("HARVESTER_OPS_TF_WORKSPACES") or self.state_dir / "terraform")
        self.repl_db = Path(repl_db) if repl_db else Path(
            os.environ.get("HARVESTER_OPS_REPLICATION_DB") or self.state_dir / "replication.db")

    def __repr__(self):
        return "<FileStore>"

    def location(self, cls, path, field):
        return secret_classes.local_location(cls, path, field, self.state_dir, self.tf_dir, self.repl_db)

    def _abs(self, p):
        return Path(p) if os.path.isabs(str(p)) else self.state_dir / p

    def local_file(self, cls, path, field):
        """Chemin local du secret pour les classes rangées en fichier ; la
        valeur du champ de déclaration quand c'est une référence ; None pour
        les classes en base."""
        kind, p, extra = self.location(cls, path, field)
        if kind == "file":
            return p
        if kind == "decl-file":
            decl, key = extra
            try:
                v = _dig(_read_yaml(decl), key) if decl.is_file() else None
            except (OSError, ValueError, UnicodeDecodeError):
                v = None
            except Exception:                   # noqa: BLE001 : YAML invalide = déclaration ignorée
                v = None
            if isinstance(v, str) and v:
                return v if is_ref(v) else self._abs(v)
            return p
        return None

    def read_local(self, cls, path, field):
        """Valeur locale telle qu'elle est rangée (une référence `openbao:`
        après migration), None si absente."""
        kind, p, extra = self.location(cls, path, field)
        if kind in ("file", "decl-file"):
            f = self.local_file(cls, path, field)
            if isinstance(f, str):
                return f                           # référence dans la déclaration
            try:
                text = Path(f).read_text()
            except (OSError, UnicodeDecodeError):
                return None
            # contenu tel quel pour les fichiers d'outils (clé, kubeconfig) ;
            # une valeur sur une ligne (secret OIDC, jeton) sans sa fin de ligne
            stripped = text.strip()
            return stripped if cls in ("rancher-oidc", "tf-kvm") or is_ref(stripped) else text
        if kind == "notify":
            cfg = self._db_json("SELECT value FROM settings WHERE key = 'notify'")
            return _notify_secret(cfg, *extra)
        if kind == "hook":
            plan, action, fld = extra
            opts = self._db_json("SELECT options FROM plans WHERE id = ?", (plan,))
            for h in (opts or {}).get("hooks") or []:
                if isinstance(h, dict) and h.get("name") == action:
                    for k, v in (h.get("secret_headers") or {}).items():
                        if secret_classes.header_field(k) == fld:
                            return v
            return None
        return None

    def _db_json(self, sql, args=()):
        if not self.repl_db.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{self.repl_db}?mode=ro", uri=True, timeout=10)
            try:
                r = conn.execute(sql, args).fetchone()
            finally:
                conn.close()
            return json.loads(r[0]) if r and r[0] else None
        except (sqlite3.Error, ValueError):
            return None

    def list_local(self, cls):
        """[(chemin, champ)] des secrets locaux d'une classe."""
        out = []
        st = self.state_dir
        if cls == "kubeconfig":
            names = {p.stem for p in (st / "clusters.d").glob("*.yaml")} | {
                p.stem for p in (st / "kubeconfigs").glob("*.yaml")}
            out = [(n, "kubeconfig") for n in sorted(names) if self.read_local(cls, n, "kubeconfig") is not None]
        elif cls == "ssh-cluster":
            names = {p.stem for p in (st / "clusters.d").glob("*.yaml")}
            out = [(n, "private_key") for n in sorted(names)
                   if (st / "ssh" / f"{n}_id").is_file() or isinstance(self.local_file(cls, n, "private_key"), str)]
        elif cls == "ssh-kvm":
            names = {p.stem for p in (st / "kvm.d").glob("*.yaml")}
            out = [(n, "private_key") for n in sorted(names) if (st / "ssh" / f"{n}_id").is_file()]
        elif cls == "rancher-oidc":
            out = [(p.name[: -len(".oidc-secret")], "client_secret")
                   for p in sorted((st / "rancher.d").glob("*.oidc-secret"))]
        elif cls == "tf-kvm":
            for p in sorted((self.tf_dir / ".kvm").glob("*/decls/*/.secrets/*")):
                if p.is_file():
                    out.append((f"{p.parts[-5]}/{p.parts[-3]}/{p.name}", "value"))
        elif cls == "replication-notify":
            cfg = self._db_json("SELECT value FROM settings WHERE key = 'notify'") or {}
            for ch in cfg.get("channels") or []:
                for secret in secret_classes.NOTIFY_SECRETS.get(ch.get("kind"), ()):
                    if ch.get(secret):
                        out.append((secret_classes.NOTIFY_PATH, secret_classes.notify_field(ch["id"], secret)))
                for k, v in (ch.get("secret_headers") or {}).items():
                    if v:
                        out.append((secret_classes.NOTIFY_PATH,
                                    secret_classes.notify_field(ch["id"], ("header", k))))
        elif cls == "replication-hook" and self.repl_db.is_file():
            try:
                conn = sqlite3.connect(f"file:{self.repl_db}?mode=ro", uri=True, timeout=10)
                try:
                    rows = conn.execute("SELECT id, options FROM plans").fetchall()
                finally:
                    conn.close()
            except sqlite3.Error:
                rows = []
            for pid, raw in rows:
                try:
                    opts = json.loads(raw or "{}")
                except ValueError:
                    continue
                for h in opts.get("hooks") or []:
                    for k, v in (h.get("secret_headers") or {}).items():
                        if v:
                            out.append((f"{pid}/{h.get('name')}", secret_classes.header_field(k)))
        return out

    def get(self, ref):
        _b, cls, path, field = parse_ref_full(ref)
        v = self.read_local(cls, path, field)
        if v is None:
            raise SecretMissing(ref)
        return v

    def put(self, cls, path, field, value):
        """Écrit à l'emplacement d'aujourd'hui (fichier 0600, écriture
        atomique) pour les classes rangées en fichier ; la base de la
        réplication garde son propre écrivain."""
        f = self.local_file(cls, path, field)
        if f is None or isinstance(f, str):
            raise NotImplementedError("this class is written by its own module")
        import kvm_decl
        if cls in ("rancher-oidc", "tf-kvm") and not value.endswith("\n"):
            value = value + "\n"
        kvm_decl.write_private(f, value)
        return make_ref(cls, path, field)


def _notify_secret(cfg, cid, secret):
    for ch in (cfg or {}).get("channels") or []:
        if not isinstance(ch, dict) or ch.get("id") != cid:
            continue
        if secret in secret_classes.NOTIFY_SECRETS.get(ch.get("kind"), ()):
            return ch.get(secret) or None
        for k, v in (ch.get("secret_headers") or {}).items():
            if secret == secret_classes.header_field(k):
                return v
    return None


class OpenBaoStore:
    """Support OpenBao : KV v2 sous `<montage>/data/<préfixe>/<classe>/
    <chemin>`, cache en mémoire (`cache_ttl` secondes), copies sur tmpfs.
    Aucun repli sur une copie locale : passé le cache, une lecture refusée
    remonte l'erreur avec la classe et le nom (point 258)."""

    def __init__(self, settings, client_factory, cache_ttl=None, files_dir=None, clock=time.monotonic):
        self.settings = settings
        self.mount = settings.get("mount") or DEFAULTS["mount"]
        self.prefix = str(settings.get("prefix") or "").strip("/")
        if not self.prefix:
            raise SecretError("openbao-inactive", detail="no console prefix")
        self._factory = client_factory
        self.cache_ttl = float(cache_ttl if cache_ttl is not None else settings.get("cache_ttl_min", 15) * 60)
        self._files_dir = files_dir or (lambda: secret_files_dir(settings))
        self._clock = clock
        self._lock = threading.RLock()
        self._cache = {}          # référence -> (valeur, instant)
        self._shared = {}         # référence -> (chemin, échéance)
        self.down_since = None
        self.last_error = None

    def __repr__(self):
        return f"<OpenBaoStore {self.mount} {len(self._cache)} cached>"

    __str__ = __repr__

    def __getstate__(self):
        return {}

    def __setstate__(self, _state):
        raise TypeError("an OpenBao store does not travel")

    # -- état ---------------------------------------------------------------
    def _kv(self, cls, path):
        return f"{self.prefix}/{cls}/{path}"

    def _client(self, actor=None, action=None):
        return self._factory(actor, action)

    def _failed(self, e, cls, name):
        import openbao_client as oc
        if isinstance(e, oc.OpenBaoError) and e.code in DOWN_CODES:
            with self._lock:
                if self.down_since is None:
                    self.down_since = time.time()
                self.last_error = e.code
        if getattr(e, "code", None) == "openbao-missing":
            return None
        return SecretError(getattr(e, "code", "openbao-unreachable"), cls, name, getattr(e, "detail", ""))

    def _ok(self):
        with self._lock:
            self.down_since, self.last_error = None, None

    def probe(self):
        """`sys/health` (sans jeton) : met à jour l'état exposé."""
        import openbao_client as oc
        try:
            h = self._client().health()
        except oc.OpenBaoError as e:
            self._failed(e, None, None)
            return False
        if h.get("sealed"):
            with self._lock:
                self.down_since = self.down_since or time.time()
                self.last_error = "openbao-sealed"
            return False
        self._ok()
        return True

    def status(self):
        now = self._clock()
        with self._lock:
            live = [t for (_v, t) in self._cache.values() if now - t < self.cache_ttl]
            until = (max(live) + self.cache_ttl - now) if live else 0
            if self.down_since is None:
                state = "ok"
            else:
                state = "degraded" if live else "down"
            return {"state": state, "down_since": self.down_since, "error": self.last_error,
                    "cached": len(live), "cache_left_s": round(until)}

    def clear_cache(self):
        with self._lock:
            self._cache.clear()
            for p, _e in self._shared.values():
                _unlink(p)
            self._shared.clear()

    # -- lecture, écriture ---------------------------------------------------
    def _cached(self, ref):
        with self._lock:
            hit = self._cache.get(ref)
            if hit and self._clock() - hit[1] < self.cache_ttl:
                return hit[0]
            self._cache.pop(ref, None)
        return None

    def _read(self, cls, path, actor=None, action=None):
        import openbao_client as oc
        try:
            data, meta = self._client(actor, action).kv_get(self.mount, self._kv(cls, path))
        except oc.OpenBaoError as e:
            err = self._failed(e, cls, path)
            if err is None:
                return None, None
            raise err from None
        self._ok()
        return data, meta

    def get(self, ref, actor=None):
        _b, cls, path, field = parse_ref_full(ref)
        v = self._cached(ref)
        if v is not None:
            return v
        data, _meta = self._read(cls, path, actor)
        if not data or field not in data:
            raise SecretMissing(ref)
        with self._lock:
            for f, val in data.items():
                if isinstance(val, str):
                    self._cache[make_ref(cls, path, f, "openbao")] = (val, self._clock())
        return data[field]

    def put(self, cls, path, field, value, actor=None, action=None):
        import openbao_client as oc
        try:
            secret_classes.require_migratable(cls)
        except secret_classes.NotMigratable:
            raise SecretError("class-not-migratable", cls, path) from None
        ref = make_ref(cls, path, field, "openbao")
        if not isinstance(value, str):
            raise TypeError("a secret value is a string")
        if len(value.encode()) > VALUE_MAX:
            raise SecretError("secret-too-big", cls, path, f"over {VALUE_MAX} bytes")
        c = self._client(actor, action)
        try:
            try:
                data, meta = c.kv_get(self.mount, self._kv(cls, path))
                cas = int(meta.get("version") or 0)
            except oc.OpenBaoError as e:
                if e.code != "openbao-missing":
                    raise
                # Dernière version supprimée (douce) ou détruite : 404 à la
                # lecture, mais la clé garde sa version courante (revue I5).
                data, cas = {}, self._current_version(c, cls, path)
            new = dict(data or {}, **{field: value})
            written = c.kv_put(self.mount, self._kv(cls, path), new, cas=cas,
                               custom_metadata={"hops_action": action or "", "hops_actor": actor or "service",
                                                "hops_class": cls})
        except oc.OpenBaoError as e:
            raise (self._failed(e, cls, path) or SecretError(e.code, cls, path, e.detail)) from None
        self._ok()
        with self._lock:
            self._cache[ref] = (value, self._clock())
        self.last_metadata_error = getattr(written, "metadata_error", None)
        return ref

    def _current_version(self, c, cls, path):
        """`current_version` des métadonnées, 0 si la clé n'existe pas."""
        import openbao_client as oc
        try:
            return int(c.kv_metadata(self.mount, self._kv(cls, path)).get("current_version") or 0)
        except oc.OpenBaoError as e:
            if e.code != "openbao-missing":
                raise
            return 0

    def delete(self, ref, actor=None, action=None):
        import openbao_client as oc
        _b, cls, path, field = parse_ref_full(ref)
        with self._lock:
            self._cache.pop(ref, None)
        c = self._client(actor, action)
        try:
            try:
                data, meta = c.kv_get(self.mount, self._kv(cls, path))
            except oc.OpenBaoError as e:
                if e.code == "openbao-missing":
                    return False
                raise
            if field not in data:
                return False
            rest = {k: v for k, v in data.items() if k != field}
            if rest:
                c.kv_put(self.mount, self._kv(cls, path), rest, cas=int(meta.get("version") or 0))
            else:
                c.kv_delete_all(self.mount, self._kv(cls, path))
        except oc.OpenBaoError as e:
            raise (self._failed(e, cls, path) or SecretError(e.code, cls, path, e.detail)) from None
        return True

    def _leaves(self, c, base):
        """Feuilles sous `base` (KV v2 n'a pas de suppression récursive :
        parcours par LIST, essai 5)."""
        out, todo = [], [base]
        while todo:
            cur = todo.pop()
            for k in c.kv_list(self.mount, cur):
                (todo if k.endswith("/") else out).append(f"{cur}/{k.rstrip('/')}")
        return out

    def _tree(self, c, cls, path):
        import openbao_client as oc
        base = self._kv(cls, _check_path(path))
        leaves = self._leaves(c, base)
        try:
            c.kv_metadata(self.mount, base)
            leaves.append(base)
        except oc.OpenBaoError as e:
            if e.code != "openbao-missing":
                raise
        return leaves

    def delete_tree(self, cls, path, actor=None, action=None):
        """`metadata delete` de chaque feuille de l'arbre, versions comprises."""
        import openbao_client as oc
        if cls not in CLASSES:
            raise ValueError("unknown secret class")
        c = self._client(actor, action)
        try:
            leaves = self._tree(c, cls, path)
            for leaf in leaves:
                c.kv_delete_all(self.mount, leaf)
        except oc.OpenBaoError as e:
            raise (self._failed(e, cls, path) or SecretError(e.code, cls, path, e.detail)) from None
        pre = f"openbao:{cls}/{path}"
        with self._lock:
            for r in [r for r in self._cache if r.startswith(pre + "/") or r.startswith(pre + "#")]:
                del self._cache[r]
        return len(leaves)

    def refs(self, cls, path, actor=None):
        import openbao_client as oc
        if cls not in CLASSES:
            raise ValueError("unknown secret class")
        c = self._client(actor)
        out = []
        try:
            for leaf in self._tree(c, cls, path):
                data, _m = c.kv_get(self.mount, leaf)
                rel = leaf[len(self.prefix) + len(cls) + 2:]
                out += [make_ref(cls, rel, f, "openbao") for f in data]
        except oc.OpenBaoError as e:
            raise (self._failed(e, cls, path) or SecretError(e.code, cls, path, e.detail)) from None
        return sorted(out)

    def versions(self, ref):
        """[{version, created, actor, action}] : métadonnées seules. Les
        `custom_metadata` de KV v2 sont celles de la clé : l'auteur n'est
        connu que pour la version courante."""
        import openbao_client as oc
        _b, cls, path, _f = parse_ref_full(ref)
        try:
            md = self._client().kv_metadata(self.mount, self._kv(cls, path))
        except oc.OpenBaoError as e:
            raise (self._failed(e, cls, path) or SecretMissing(ref)) from None
        cur = int(md.get("current_version") or 0)
        cm = md.get("custom_metadata") or {}
        out = []
        for v, info in sorted((md.get("versions") or {}).items(), key=lambda kv: int(kv[0])):
            v = int(v)
            out.append({"version": v, "created": (info or {}).get("created_time"),
                        "actor": cm.get("hops_actor") if v == cur else None,
                        "action": cm.get("hops_action") if v == cur else None})
        return out

    # -- copies de travail ---------------------------------------------------
    @contextlib.contextmanager
    def file(self, ref, purpose, actor=None):
        value = self.get(ref, actor)
        p = write_private_copy(self._files_dir(), purpose, value)
        try:
            yield p
        finally:
            _unlink(p)

    def shared_file(self, ref, purpose, actor=None):
        """Copie partagée entre requêtes de lecture (kubeconfig), gardée au
        plus la durée du cache, effacée à l'expiration et à l'arrêt."""
        now = self._clock()
        with self._lock:
            for r, (p, until) in list(self._shared.items()):
                if until <= now or not os.path.exists(p):
                    _unlink(p)
                    del self._shared[r]
            hit = self._shared.get(ref)
            if hit:
                return hit[0]
        value = self.get(ref, actor)
        p = write_private_copy(self._files_dir(), purpose, value)
        with self._lock:
            self._shared[ref] = (p, now + self.cache_ttl)
        return p


# ---------------------------------------------------------------------------
# Magasin du processus : mémoire (plan-run), fichiers, OpenBao
# ---------------------------------------------------------------------------
def default_client_factory(settings):
    """Fabrique des clients d'un processus : une connexion AppRole par
    processus (identifiants lus dans /etc/harvester-ops/openbao/), une copie
    du client par action pour les en-têtes de corrélation."""
    import openbao_client as oc
    base = {}
    lock = threading.Lock()

    def factory(actor=None, action=None):
        with lock:
            c = base.get("c")
            if c is None:
                d = openbao_dir()
                ca = settings.get("ca_file") or d / "ca.pem"
                ca_pem = Path(ca).read_text() if Path(ca).is_file() else None
                c = oc.Client(settings["addr"], ca_pem=ca_pem, timeout=oc.clamp_timeout(settings.get("timeout")))
                try:
                    role_id = (d / "role-id").read_text().strip()
                    secret_id = (d / "secret-id").read_text().strip()
                except OSError:
                    raise oc.OpenBaoError("openbao-login-failed", None, "AppRole credentials unreadable") from None
                c.login_approle(role_id, secret_id)
                base["c"] = c
        return c.with_headers(action, actor)
    return factory


class MultiStore(LocalStore):
    """Le magasin d'un processus. Mémoire pour `plan-run` (comportement
    2.4.0, hérité de LocalStore), support fichier pour les références
    `local:` des classes persistantes, OpenBao pour les références
    `openbao:` quand il est actif. Réglages relus à chaud."""

    SETTINGS_RECHECK_S = 1.0

    def __init__(self, state_dir=None, client_factory=None, mountinfo=None):
        super().__init__()
        self._state_arg = state_dir
        self._factory_arg = client_factory
        self._mountinfo = mountinfo
        self._cfg_lock = threading.Lock()
        self._sig = None
        self._checked = 0.0
        self.settings = dict(DEFAULTS, migrated={})
        self._bao = None
        self._files = None
        self._mem_files = {}

    def __repr__(self):
        with self._lock:
            n = len(self._data)
        return f"<MultiStore {n} secret(s) in memory, mode {self.settings.get('mode')}>"

    __str__ = __repr__

    # -- réglages -------------------------------------------------------------
    @property
    def state_dir(self):
        return _state_dir(self._state_arg)

    def set_state_dir(self, state_dir):
        if state_dir and not self._state_arg:
            self._state_arg = state_dir
            self._sig, self._checked = None, 0.0

    def _refresh(self):
        now = time.monotonic()
        if now - self._checked < self.SETTINGS_RECHECK_S and self._sig is not None:
            return
        with self._cfg_lock:
            self._checked = now
            p = self.state_dir / SETTINGS_FILE
            try:
                st = os.stat(p)
                sig = (str(p), st.st_mtime_ns, st.st_size)
            except OSError:
                sig = (str(p), None)
            if sig == self._sig:
                return
            new = load_settings(self.state_dir)
            old = self.settings
            self.settings, self._sig = new, sig
            self._files = FileStore(self.state_dir)
            server = lambda s: (s.get("mode"), s.get("addr"), s.get("mount"), s.get("prefix"))  # noqa: E731
            if self._bao is not None and (server(old) != server(new) or not openbao_active(new)):
                self._bao.clear_cache()
                self._bao = None
            if self._bao is None and openbao_active(new):
                factory = self._factory_arg or default_client_factory(new)
                self._bao = OpenBaoStore(new, factory,
                                         files_dir=lambda: secret_files_dir(self.settings, self._mountinfo))
            elif self._bao is not None:
                self._bao.cache_ttl = new["cache_ttl_min"] * 60
                self._bao.settings = new

    @property
    def files(self):
        self._refresh()
        return self._files

    @property
    def openbao(self):
        """Le support OpenBao actif, ou None."""
        self._refresh()
        return self._bao

    def active(self):
        return self.openbao is not None

    def _need_bao(self, cls=None, name=None):
        bao = self.openbao
        if bao is None:
            raise SecretError("openbao-inactive", cls, name, "OpenBao is not configured")
        return bao

    def migrated(self, cls):
        self._refresh()
        return cls in (self.settings.get("migrated") or {}) and self._bao is not None

    # -- interface -------------------------------------------------------------
    def put(self, cls, path, field, value, actor=None, action=None):
        if cls not in CLASSES:
            make_ref(cls, path, field)              # ValueError
        if not CLASSES[cls]["persist"]:
            return super().put(cls, path, field, value)
        make_ref(cls, path, field)
        if self.migrated(cls):
            return self._bao.put(cls, path, field, value, actor=actor, action=action)
        return self.files.put(cls, path, field, value)

    def get(self, ref, actor=None):
        try:
            backend, cls, path, _field = parse_ref_full(ref)
        except ValueError:
            raise SecretMissing(ref if isinstance(ref, str) else "(invalid)") from None
        if backend == "openbao":
            return self._need_bao(cls, path).get(ref, actor)
        if not CLASSES[cls]["persist"]:
            return super().get(ref)
        v = self.files.get(ref)
        if is_openbao_ref(v):                       # valeur locale remplacée par sa référence
            return self.get(v, actor)
        return v

    def delete(self, ref, actor=None, action=None):
        backend, cls, _path, _f = parse_ref_full(ref)
        if backend == "openbao":
            return self._need_bao(cls).delete(ref, actor=actor, action=action)
        if not CLASSES[cls]["persist"]:
            return super().delete(ref)
        raise NotImplementedError("local persistent secrets are removed by their own module")

    def delete_tree(self, cls, path, backend="local", actor=None, action=None):
        if backend == "openbao":
            return self._need_bao(cls, path).delete_tree(cls, path, actor=actor, action=action)
        return super().delete_tree(cls, path)

    def refs(self, cls, path, backend="local"):
        if backend == "openbao":
            return self._need_bao(cls, path).refs(cls, path)
        return super().refs(cls, path)

    def versions(self, ref):
        backend, cls, path, _f = parse_ref_full(ref)
        if backend == "openbao":
            return self._need_bao(cls, path).versions(ref)
        return []

    @contextlib.contextmanager
    def file(self, ref, purpose, actor=None):
        """Chemin d'un fichier qui porte la valeur, le temps du bloc. Support
        fichier : le fichier d'aujourd'hui lui-même (rien n'est copié ni
        effacé). OpenBao et mémoire : copie 0600 sur tmpfs, effacée à la
        sortie du bloc, exception comprise."""
        backend, cls, path, field = parse_ref_full(ref)
        if backend == "openbao":
            with self._need_bao(cls, path).file(ref, purpose, actor) as p:
                yield p
            return
        if CLASSES[cls]["persist"]:
            f = self.files.local_file(cls, path, field)
            local = self.files.read_local(cls, path, field)
            if is_openbao_ref(local):
                with self.file(local, purpose, actor) as p:
                    yield p
                return
            if isinstance(f, Path):
                if local is None:
                    raise SecretMissing(ref)
                yield f
                return
        value = self.get(ref, actor)
        p = write_private_copy(secret_files_dir(self.settings, self._mountinfo), purpose, value)
        try:
            yield p
        finally:
            _unlink(p)

    def shared_file(self, ref, purpose, actor=None):
        """Chemin gardé au-delà d'une requête : le fichier local lui-même, ou
        une copie tmpfs d'un secret OpenBao gardée la durée du cache."""
        backend, cls, path, field = parse_ref_full(ref)
        if backend == "local" and CLASSES[cls]["persist"]:
            local = self.files.read_local(cls, path, field)
            if is_openbao_ref(local):
                return self.shared_file(local, purpose, actor)
            f = self.files.local_file(cls, path, field)
            if isinstance(f, Path) and local is not None:
                return f
            raise SecretMissing(ref)
        if backend == "local":
            raise ValueError("memory secrets have no shared file")
        return self._need_bao(cls, path).shared_file(ref, purpose, actor)

    def status(self):
        bao = self.openbao
        if bao is None:
            return {"mode": "local", "state": "local"}
        return dict(bao.status(), mode=self.settings.get("mode"), addr=self.settings.get("addr"))

    def clear_cache(self):
        if self._bao is not None:
            self._bao.clear_cache()


_STORE = None
_STORE_LOCK = threading.Lock()


def store(state_dir=None):
    """Le magasin du processus (la console et chaque outil ont le leur),
    selon `<état>/secrets-settings.json`. Un seul par processus."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = MultiStore(state_dir)
        elif state_dir:
            _STORE.set_state_dir(state_dir)
        return _STORE


@atexit.register
def _cleanup():
    s = _STORE
    if s is not None:
        try:
            s.clear_cache()
        except Exception:                          # noqa: BLE001 : à l'arrêt, au mieux
            pass


# ---------------------------------------------------------------------------
# Aides des lecteurs (tâche 4)
# ---------------------------------------------------------------------------
def resolve_value(value, actor=None, st=None):
    """La valeur d'un champ qui peut porter une référence : la référence est
    lue par le magasin ; toute autre valeur est rendue telle quelle."""
    if isinstance(value, str) and is_ref(value):
        return (st or store()).get(value, actor)
    return value


def read_value_file(path, actor=None, st=None):
    """Contenu d'un fichier de secret sur une ligne (sans espaces de fin), ou
    la valeur désignée s'il porte une référence ; None si illisible."""
    if not path:
        return None
    if isinstance(path, str) and is_ref(path):
        try:
            return (st or store()).get(path, actor)
        except SecretMissing:
            return None
    try:
        s = Path(path).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if s and is_ref(s):
        try:
            return (st or store()).get(s, actor)
        except SecretMissing:
            return None
    return s or None


def path_of(value, purpose, actor=None, st=None):
    """Chemin à remettre à un outil pour un champ qui peut porter une
    référence (kubeconfig, clé SSH) : la référence donne une copie tmpfs
    partagée (ou le fichier local) ; un chemin est rendu tel quel."""
    if isinstance(value, str) and is_ref(value):
        return str((st or store()).shared_file(value, purpose, actor))
    return value


def is_active(st=None):
    """OpenBao est-il le support actif du processus ?"""
    try:
        return (st or store()).active()
    except Exception:                              # noqa: BLE001 : réglages illisibles = local
        return False
