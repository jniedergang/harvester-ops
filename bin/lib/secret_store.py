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
import sys
import threading
import time
import weakref
from pathlib import Path

import secret_classes

REF_RE = re.compile(r"^(?P<backend>local|openbao):(?P<cls>[a-z0-9-]+)/(?P<path>[A-Za-z0-9._/-]{1,200})"
                    r"#(?P<field>[a-z0-9_]{1,64})$")
# 2.4.0 : chemins d'un déroulé en minuscules ; les classes persistantes
# portent des noms de cluster ou d'hôte, où la casse et `_` sont permis.
_LOWER_PATH_RE = re.compile(r"^[a-z0-9._/-]{1,200}$")
# Classe -> {"persist"} : `plan-run` vit en mémoire (2.4.0) ; les autres
# ont un emplacement local (support fichier) et peuvent migrer dans OpenBao.
PLAN_RUN = "plan-run"
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
# 2.9.0 tâche 8 : originaux gardés après migration (sans valeur : chemins,
# dates) ; le fichier gardé porte ce suffixe, 0600, à côté de l'original
ORIGINALS_FILE = "secrets-originals.json"
ORIGINALS_LOCK = ".secrets-originals.lock"
MIGRATED_SUFFIX = ".migrated"
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
    "ca_file": None,              # CA d'OpenBao ailleurs que <openbao>/ca.pem (revue M7)
    "secret_files_dir": None,     # développement seulement, avec dev_disk_secret_files
    "dev_disk_secret_files": False,
    "originals_keep_days": 7,     # garde des originaux migrés (1 à 30)
    "outage_test": None,          # {"ok_at", "by"} : panne simulée réussie (point 255)
    "plan_secrets_keep_days": 7,  # secrets d'un déroulé arrêté reprenable (1 à 30, décision 141)
    "tf_vault_ack": None,         # {"addr", "version", "at", "by"} : Vault > 1.14 acquitté (point 270)
    "tf_entities_pending": [],    # mode A : entités tf-<décl> à retirer par l'administrateur
    # 2.9.0 tâche 11 (point 259) : instantanés de B1 dans un répertoire réglé
    # par l'administrateur (jamais sous l'état), quotidiens, 14 gardés
    "snapshot_dir": None,
    "snapshot_time": "02:30",
    "snapshot_keep": 14,
    "snapshots": None,            # {"last_ok_at", "last_file", "bytes", "last_error", "last_error_at"}
    "tf_keys_retired": {},        # décision 278 : décl -> {"at", "trial"[, "error"]}
}
SNAPSHOT_KEEP_RANGE = (1, 365)
SNAPSHOT_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
ORIGINALS_KEEP_RANGE = (1, 30)
PLAN_SECRETS_KEEP_RANGE = (1, 30)
CACHE_TTL_RANGE = (1, 120)
VALUE_MAX = 1 << 20               # 1 Mio
TMPFS_TYPES = ("tmpfs", "ramfs")
# Erreurs du client qui disent « OpenBao ne répond pas » (bandeau, cache).
# Revue I6 : un outil lancé par la console ne se connecte jamais à OpenBao.
# La console pose SPAWNED_ENV (son propre pid) dans son environnement, hérité
# par tout ce qu'elle lance ; elle remet les copies de travail dont l'outil a
# besoin par un fichier de correspondance tmpfs (FILES_ENV : {référence:
# chemin}). Un lecteur de la console (read_workers) reste la console.
SPAWNED_ENV = "HARVESTER_OPS_SPAWNED"
FILES_ENV = "HARVESTER_OPS_SECRET_FILES"
# Une copie partagée retirée mais encore tenue par un fil vivant est effacée
# au plus tard après ce délai (revue I1 : borne si un fil ne finit jamais).
SHARED_HOLD_MAX_S = 12 * 3600
DOWN_CODES = ("openbao-unreachable", "openbao-tls", "openbao-sealed", "openbao-internal")


VERSIONS_KEPT = 10                # max_versions du montage (B1), recommandé en A


def version_metadata(custom, version, cls, actor, action, kept=VERSIONS_KEPT):
    """`custom_metadata` d'une écriture : auteur et action de la version
    courante (`hops_actor`, `hops_action`, `hops_class`) et de chaque version
    gardée (`hops_v<N>_actor`, `hops_v<N>_action`), les plus anciennes
    retirées au-delà de `kept`. Jamais d'empreinte de la valeur."""
    out = {k: str(v) for k, v in (custom or {}).items()
           if not re.match(r"^hops_v(\d+)_(actor|action)$", str(k))
           or int(re.match(r"^hops_v(\d+)_", str(k)).group(1)) > version - kept}
    out.update({"hops_action": action or "", "hops_actor": actor or "service", "hops_class": cls,
                f"hops_v{version}_actor": actor or "service", f"hops_v{version}_action": action or ""})
    return out


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
    out = dict(DEFAULTS, migrated={}, tf_entities_pending=[], tf_keys_retired={})
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
    try:
        keep = int(out["originals_keep_days"])
    except (TypeError, ValueError):
        keep = DEFAULTS["originals_keep_days"]
    out["originals_keep_days"] = max(ORIGINALS_KEEP_RANGE[0], min(ORIGINALS_KEEP_RANGE[1], keep))
    try:
        keep = int(out["plan_secrets_keep_days"])
    except (TypeError, ValueError):
        keep = DEFAULTS["plan_secrets_keep_days"]
    out["plan_secrets_keep_days"] = max(PLAN_SECRETS_KEEP_RANGE[0], min(PLAN_SECRETS_KEEP_RANGE[1], keep))
    if not isinstance(out["tf_entities_pending"], list):
        out["tf_entities_pending"] = []
    try:
        keep = int(out["snapshot_keep"])
    except (TypeError, ValueError):
        keep = DEFAULTS["snapshot_keep"]
    out["snapshot_keep"] = max(SNAPSHOT_KEEP_RANGE[0], min(SNAPSHOT_KEEP_RANGE[1], keep))
    if not isinstance(out["snapshot_time"], str) or not SNAPSHOT_TIME_RE.match(out["snapshot_time"]):
        out["snapshot_time"] = DEFAULTS["snapshot_time"]
    if not isinstance(out["tf_keys_retired"], dict):
        out["tf_keys_retired"] = {}
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


def _exit_on_term():
    """Revue M6 : atexit ne tourne pas sur SIGTERM (signal d'annulation par
    défaut). Un outil qui a écrit une copie de travail sort proprement sur
    SIGTERM, ses copies sont alors effacées. Posé seulement si personne n'a
    réglé SIGTERM (la console a le sien) et depuis le fil principal."""
    import signal
    try:
        if (threading.current_thread() is threading.main_thread()
                and signal.getsignal(signal.SIGTERM) == signal.SIG_DFL):
            signal.signal(signal.SIGTERM, lambda *_a: sys.exit(143))
    except (ValueError, OSError):
        pass


def write_private_copy(directory, purpose, value):
    """Fichier 0600 neuf (O_EXCL, sans lien suivi) dans `directory`."""
    _exit_on_term()
    name = f"{_PURPOSE_RE.sub('-', str(purpose).lower())[:40] or 'secret'}-{_secrets.token_hex(8)}"
    p = Path(directory) / name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value)
    except BaseException:
        _unlink(p)                                 # tmpfs plein : pas de copie partielle (revue M5)
        raise
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
        elif cls in ("ssh-cluster", "ssh-kvm"):
            names = {p.stem for p in (st / ("clusters.d" if cls == "ssh-cluster" else "kvm.d")).glob("*.yaml")}
            for n in sorted(names):
                f = self.local_file(cls, n, "private_key")
                if isinstance(f, str) or (isinstance(f, Path) and f.is_file()):
                    out.append((n, "private_key"))
        elif cls == "rancher-oidc":
            out = [(p.name[: -len(".oidc-secret")], "client_secret")
                   for p in sorted((st / "rancher.d").glob("*.oidc-secret"))]
        elif cls == "tf-kvm":
            for p in sorted((self.tf_dir / ".kvm").glob("*/decls/*/.secrets/*")):
                if p.is_file() and not p.name.endswith(MIGRATED_SUFFIX):
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


    # -- migration (2.9.0 tâche 8) ---------------------------------------------
    def _base(self, cls):
        return self.tf_dir if cls == "tf-kvm" else self.state_dir

    def _norm(self, cls, data):
        """Valeur d'un fichier telle que `read_local` la rend."""
        text = data.decode()
        return text.strip() if cls in ("rancher-oidc", "tf-kvm") else text

    @contextlib.contextmanager
    def _originals_lock(self):
        import fcntl
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state_dir / ORIGINALS_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _originals_doc(self):
        try:
            doc = json.loads((self.state_dir / ORIGINALS_FILE).read_text())
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def _originals_save(self, doc):
        import kvm_decl
        kvm_decl.write_private(self.state_dir / ORIGINALS_FILE, json.dumps(doc, indent=2, sort_keys=True) + "\n")

    def _decl_lock(self, cls):
        """Verrou de la déclaration qui désigne le secret : celui des hôtes
        KVM (`kvm.d/.lock`, pris par harvester-kvm et harvester-folders) pour
        ssh-kvm, celui des clusters (`clusters.d/.lock`, pris par CONFIG_LOCK
        de la console) sinon."""
        if cls == "ssh-kvm":
            import kvm_decl
            return kvm_decl.decl_lock(self.state_dir, wait=30.0)
        import cluster_decl
        return cluster_decl.decl_lock(self.state_dir, wait=30.0)

    def _set_decl(self, cls, path, decl, key, value):
        with self._decl_lock(cls):
            self._set_decl_locked(cls, path, decl, key, value)

    def _set_decl_locked(self, cls, path, decl, key, value):
        """Champ de la déclaration (cluster ou hôte KVM) qui désigne le
        secret : sa référence après migration, son chemin après retour.
        Appelé sous `_decl_lock(cls)` : la déclaration est relue ici."""
        import kvm_decl
        if cls == "ssh-kvm":
            doc = kvm_decl.load(self.state_dir, path)
            doc["ssh_key"] = value
            kvm_decl.write(self.state_dir, doc)
            return
        doc = _read_yaml(decl)
        if not isinstance(doc, dict):
            raise ValueError("cluster declaration unreadable")
        if key == "kubeconfig":
            doc["kubeconfig"] = value
        else:
            doc["ssh"] = dict(doc.get("ssh") or {}, key=value)
        try:
            import yaml
            text = yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)
        except ImportError:                     # pragma: no cover  (JSON est du YAML)
            text = json.dumps(doc, indent=2)
        kvm_decl.write_private(decl, text)

    def _inside(self, cls, rel):
        """Chemin d'une entrée de secrets-originals.json : relatif et dans
        la base de la classe (revue M8) ; ValueError sinon."""
        base = self._base(cls)
        if not isinstance(rel, str) or not rel or os.path.isabs(rel):
            raise ValueError("originals registry entry with an unsafe path")
        p = base / rel
        b = os.path.realpath(str(base))
        if not os.path.realpath(str(p.parent)).startswith(b + os.sep) and os.path.realpath(str(p.parent)) != b:
            raise ValueError("originals registry entry with an unsafe path")
        if ".." in Path(rel).parts or os.path.islink(p):
            raise ValueError("originals registry entry with an unsafe path")
        return p

    def eligible(self, cls, path, field):
        """None si le secret peut migrer, sinon le code de ce qui le garde en
        local : fichier hors du répertoire d'état (fichier de l'opérateur),
        lien, déclaration absente, nom qui ne fait pas une référence."""
        try:
            kind, _p, extra = self.location(cls, path, field)
            make_ref(cls, path, field, "openbao")
        except ValueError:
            return "secret-bad-name"
        if kind in ("file", "decl-file"):
            f = self.local_file(cls, path, field)
            if isinstance(f, str):
                return None
            # chemin réel (revue I1 de la seconde passe) : un répertoire lien
            # sous l'état ne fait pas passer un fichier de l'hôte pour un
            # secret de la console
            base = os.path.realpath(str(self._base(cls)))
            if not os.path.realpath(str(f)).startswith(base + os.sep) or os.path.islink(f):
                return "secret-outside-state"
            if kind == "decl-file" and not Path(extra[0]).is_file():
                return "secret-no-declaration"
        return None

    def write_ref(self, cls, path, field, ref, value=None, action=None):
        """La valeur locale devient sa référence `openbao:`, l'original gardé
        (`<fichier>.migrated` 0600 noté dans secrets-originals.json, ou table
        `secret_rollback`). `value` : la valeur migrée ; refus (ValueError)
        si la valeur locale a changé entre-temps, rien n'est touché."""
        if not is_openbao_ref(ref):
            raise ValueError("an openbao: reference is expected")
        kind, _p, extra = self.location(cls, path, field)
        if kind in ("notify", "hook"):
            import replication_db
            conn = replication_db.connect(self.repl_db)
            try:
                replication_db.secret_to_ref(conn, cls, path, field, ref, value, action)
            finally:
                conn.close()
            return
        import kvm_decl
        lock = self._decl_lock(cls) if kind == "decl-file" else contextlib.nullcontext()
        # lecture, comparaison, original gardé, référence posée et fichier
        # retiré dans un seul bloc sous le verrou de la déclaration : une
        # écriture de la console intercalée n'est jamais perdue (revue I2)
        with lock:
            f = self.local_file(cls, path, field)
            if not isinstance(f, Path):
                raise ValueError("no local file for this secret")
            data = f.read_bytes()
            if value is not None and self._norm(cls, data) != value:
                raise ValueError("local value changed during the migration")
            base = self._base(cls)
            mig = f.with_name(f.name + MIGRATED_SUFFIX)
            key = f"{cls}/{path}#{field}"
            with self._originals_lock():
                # l'entrée d'abord, la copie ensuite (revue M3 de la tâche 8) :
                # un arrêt brutal entre les deux laisse au pire une entrée sans
                # fichier (sans effet), jamais une copie en clair inconnue du
                # registre, donc du délai de garde et de la confirmation
                doc = self._originals_doc()
                doc[key] = {
                    "cls": cls, "path": path, "field": field, "at": time.time(), "action": action or "",
                    "file": Path(os.path.relpath(mig, base)).as_posix(),
                    "dest": Path(os.path.relpath(f, base)).as_posix()}
                self._originals_save(doc)
                try:
                    kvm_decl.write_private(mig, data)
                except BaseException:
                    _unlink(mig)
                    doc = self._originals_doc()
                    doc.pop(key, None)
                    self._originals_save(doc)
                    raise
            if kind == "file":
                # classes sans verrou partagé avec leurs écrivains (la classe
                # est déjà marquée migrée : ils écrivent dans OpenBao) :
                # comparée encore juste avant le remplacement atomique
                if self._norm(cls, f.read_bytes()) != self._norm(cls, data):
                    with self._originals_lock():
                        doc = self._originals_doc()
                        doc.pop(key, None)
                        self._originals_save(doc)
                    _unlink(mig)
                    raise ValueError("local value changed during the migration")
                kvm_decl.write_private(f, ref + "\n")
            else:
                self._set_decl_locked(cls, path, extra[0], extra[1], ref)
                _unlink(f)

    def restore_local(self, cls, path, field, value):
        """Retour en local : la valeur relue dans OpenBao réécrite à son
        emplacement (0600), les octets de l'original gardé quand il porte la
        même valeur ; l'original gardé est ensuite retiré."""
        kind, p, extra = self.location(cls, path, field)
        if kind in ("notify", "hook"):
            import replication_db
            conn = replication_db.connect(self.repl_db)
            try:
                replication_db.secret_from_ref(conn, cls, path, field, value)
            finally:
                conn.close()
            return
        import kvm_decl
        base = self._base(cls)
        key = f"{cls}/{path}#{field}"
        with self._originals_lock():
            ent = self._originals_doc().get(key) or {}
        dest = self._inside(cls, ent["dest"]) if ent.get("dest") else p
        mig = self._inside(cls, ent["file"]) if ent.get("file") else None
        data = None
        if mig is not None and mig.is_file():
            raw = mig.read_bytes()
            if self._norm(cls, raw) == value:
                data = raw
        if data is None:
            data = value.encode()
            if cls in ("rancher-oidc", "tf-kvm") and not data.endswith(b"\n"):
                data += b"\n"
        if kind == "decl-file":
            rel = kvm_decl.key_rel(path) if cls == "ssh-kvm" else Path(os.path.relpath(dest, base)).as_posix()
            with self._decl_lock(cls):
                kvm_decl.write_private(dest, data)
                self._set_decl_locked(cls, path, extra[0], extra[1], rel)
        else:
            kvm_decl.write_private(dest, data)
        with self._originals_lock():
            doc = self._originals_doc()
            if doc.pop(key, None) is not None:
                self._originals_save(doc)
        if mig is not None:
            _unlink(mig)

    def originals(self, cls):
        """[{path, field, at, where}] des originaux gardés d'une classe, sans
        valeur."""
        if cls in ("replication-notify", "replication-hook"):
            if not self.repl_db.is_file():
                return []
            import replication_db
            conn = replication_db.connect(self.repl_db)
            try:
                rows = replication_db.secret_rollback_rows(conn, cls)
            finally:
                conn.close()
            return [{"path": r["path"], "field": r["field"], "at": r["at"], "where": "db"} for r in rows]
        return [{"path": e["path"], "field": e["field"], "at": e.get("at") or 0, "where": "file"}
                for e in sorted(self._originals_doc().values(), key=lambda e: (e.get("path"), e.get("field")))
                if isinstance(e, dict) and e.get("cls") == cls]

    def erase_original(self, cls, path, field):
        """Efface l'original gardé (confirmation, délai de garde). Rend True
        s'il y en avait un."""
        if cls in ("replication-notify", "replication-hook"):
            if not self.repl_db.is_file():
                return False
            import replication_db
            conn = replication_db.connect(self.repl_db)
            try:
                return bool(replication_db.secret_rollback_delete(conn, cls, path, field))
            finally:
                conn.close()
        kind, _p, _extra = self.location(cls, path, field)
        base = self._base(cls)
        with self._originals_lock():
            doc = self._originals_doc()
            ent = doc.pop(f"{cls}/{path}#{field}", None)
            if not ent:
                return False
            _unlink(self._inside(cls, ent["file"]))
            # reste d'une migration coupée entre la référence et le retrait
            # du fichier : effacé seulement si la déclaration porte la référence
            if kind == "decl-file" and is_openbao_ref(self.read_local(cls, path, field)):
                _unlink(self._inside(cls, ent["dest"]))
            self._originals_save(doc)
        return True

    def ref_users(self, ref):
        """Noms (chemins) dont la valeur locale est exactement `ref` :
        déclarations de clusters ou d'hôtes, actions annexes, etc."""
        try:
            _b, cls, _path, _field = parse_ref_full(ref)
        except ValueError:
            return []
        return [p for p, f in self.list_local(cls) if self.read_local(cls, p, f) == ref]

    def rename_original(self, cls, old, new, field):
        """L'original gardé d'un secret suit le renommage de son cluster :
        entrée du registre et `<fichier>.migrated` à l'emplacement du
        nouveau nom quand il était à l'emplacement de l'ancien."""
        import kvm_decl
        base = self._base(cls)
        with self._originals_lock():
            doc = self._originals_doc()
            ent = doc.pop(f"{cls}/{old}#{field}", None)
            if not ent:
                return False
            old_default = Path(os.path.relpath(self.location(cls, old, field)[1], base)).as_posix()
            if ent.get("dest") == old_default:
                new_dest = self.location(cls, new, field)[1]
                new_mig = new_dest.with_name(new_dest.name + MIGRATED_SUFFIX)
                src = self._inside(cls, ent["file"])
                if src.is_file():
                    kvm_decl.write_private(new_mig, src.read_bytes())
                    _unlink(src)
                ent["dest"] = Path(os.path.relpath(new_dest, base)).as_posix()
                ent["file"] = Path(os.path.relpath(new_mig, base)).as_posix()
            ent["path"] = new
            doc[f"{cls}/{new}#{field}"] = ent
            self._originals_save(doc)
        return True

    def original_value(self, cls, path, field):
        """Valeur de l'original gardé (telle que `read_local` la rendrait),
        None s'il n'y en a pas : la confirmation n'efface un original dont
        la valeur locale n'est pas la référence que s'il porte cette valeur
        (revue I1)."""
        if cls in ("replication-notify", "replication-hook"):
            if not self.repl_db.is_file():
                return None
            import replication_db
            conn = replication_db.connect(self.repl_db)
            try:
                r = conn.execute("SELECT value FROM secret_rollback WHERE cls = ? AND path = ? AND field = ?",
                                 (cls, path, field)).fetchone()
            finally:
                conn.close()
            return r[0] if r else None
        with self._originals_lock():
            ent = self._originals_doc().get(f"{cls}/{path}#{field}")
        if not ent:
            return None
        try:
            return self._norm(cls, self._inside(cls, ent["file"]).read_bytes())
        except (OSError, ValueError, UnicodeDecodeError, KeyError):
            return None


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
        self._shared = {}         # référence -> (chemin, échéance) : copie en service
        self._copies = {}         # chemin -> {ref, holders (fils), retired} (revue I1)
        self._derived = {}        # groupe -> chemin d'une copie dérivée en service (revue I2)
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
        """Vide le cache ; les copies partagées sont retirées du service et
        effacées dès qu'aucune action qui les a reçues ne tourne encore."""
        with self._lock:
            self._cache.clear()
            for r in list(self._shared):
                self._retire(r)
            for g in list(self._derived):
                self._retire_derived(g)
            self._sweep()

    def purge(self):
        """Arrêt du processus : toutes les copies partagées sont effacées."""
        with self._lock:
            self._cache.clear()
            self._shared.clear()
            self._derived.clear()
            for p in list(self._copies):
                _unlink(p)
            self._copies.clear()

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
        kv = self._kv(cls, path)
        try:
            try:
                data, meta = c.kv_get(self.mount, kv)
                cas = int(meta.get("version") or 0)
                custom = meta.get("custom_metadata") or {}
            except oc.OpenBaoError as e:
                if e.code != "openbao-missing":
                    raise
                # Dernière version supprimée (douce) ou détruite : 404 à la
                # lecture, mais la clé garde sa version courante (revue I5).
                md = self._metadata(c, kv)
                data, cas, custom = {}, int(md.get("current_version") or 0), md.get("custom_metadata") or {}
            new = dict(data or {}, **{field: value})
            cm = version_metadata(custom, cas + 1, cls, actor, action)
            try:
                written = c.kv_put(self.mount, kv, new, cas=cas, custom_metadata=cm)
            except oc.OpenBaoError as e:
                # Revue M1 : écriture appliquée dont la réponse s'est perdue
                # (délai, 5xx), puis réessai refusé par `cas` : relire et
                # comparer (essai 5) avant de conclure au conflit.
                if e.code != "openbao-cas-conflict":
                    raise
                try:
                    again, _m = c.kv_get(self.mount, kv)
                except oc.OpenBaoError:
                    raise e from None
                if again != new:
                    raise
                written = oc.Written(cas + 1)
                try:
                    c.kv_set_metadata(self.mount, kv, cm)
                except oc.OpenBaoError as me:
                    written.metadata_error = me.code
        except oc.OpenBaoError as e:
            raise (self._failed(e, cls, path) or SecretError(e.code, cls, path, e.detail)) from None
        self._ok()
        with self._lock:
            self._cache[ref] = (value, self._clock())
        self.last_metadata_error = getattr(written, "metadata_error", None)
        return ref

    def copy_new(self, ref, new_path, actor=None, action=None):
        """Copie le secret `ref` vers `new_path` (même classe, même champ),
        écrit avec `cas=0` : jamais par-dessus une autre valeur ; relu hors
        cache et comparé par SHA-256 avant de rendre la nouvelle référence
        (renommage d'un cluster, revue C1 de la tâche 8). La source n'est
        pas touchée : l'appelant la retire une fois la déclaration réécrite."""
        import hashlib
        import openbao_client as oc
        _b, cls, path, field = parse_ref_full(ref)
        new_ref = make_ref(cls, new_path, field, "openbao")
        data, _m = self._read(cls, path, actor, action)
        if not data or field not in data:
            raise SecretMissing(ref)
        value = data[field]
        c = self._client(actor, action)
        kv = self._kv(cls, new_path)
        try:
            try:
                c.kv_put(self.mount, kv, {field: value}, cas=0,
                         custom_metadata=version_metadata({}, 1, cls, actor, action))
            except oc.OpenBaoError as e:
                if e.code != "openbao-cas-conflict":
                    raise
        except oc.OpenBaoError as e:
            raise (self._failed(e, cls, new_path) or SecretError(e.code, cls, new_path, e.detail)) from None
        with self._lock:
            self._cache.pop(new_ref, None)
        back, _m = self._read(cls, new_path, actor, action)
        sha = lambda v: hashlib.sha256(str(v).encode()).hexdigest()   # noqa: E731
        if not back or field not in back or sha(back[field]) != sha(value):
            raise SecretError("openbao-cas-conflict", cls, new_path,
                              "another value is already stored under the new name")
        self._ok()
        return new_ref

    def _metadata(self, c, kv):
        import openbao_client as oc
        try:
            return c.kv_metadata(self.mount, kv)
        except oc.OpenBaoError as e:
            if e.code != "openbao-missing":
                raise
            return {}

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

    def children(self, cls, actor=None):
        """Noms de premier niveau sous `<classe>/` (LIST, sans lire de
        valeur) : identifiants des déroulés de `plan-run` (balayage)."""
        import openbao_client as oc
        if cls not in CLASSES:
            raise ValueError("unknown secret class")
        try:
            return sorted({k.rstrip("/") for k in self._client(actor).kv_list(self.mount, f"{self.prefix}/{cls}")})
        except oc.OpenBaoError as e:
            raise (self._failed(e, cls, "") or SecretError(e.code, cls, "", e.detail)) from None

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
        `custom_metadata` de KV v2 sont celles de la clé : l'auteur de
        chaque version y est gardé sous `hops_v<N>_actor` et
        `hops_v<N>_action` (version_metadata)."""
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
            actor = cm.get(f"hops_v{v}_actor", cm.get("hops_actor") if v == cur else None)
            action = cm.get(f"hops_v{v}_action", cm.get("hops_action") if v == cur else None)
            out.append({"version": v, "created": (info or {}).get("created_time"),
                        "actor": actor, "action": action or None})
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
        """Copie partagée entre requêtes de lecture (kubeconfig), servie au
        plus la durée du cache. Revue I1 : le fil qui la reçoit (une action,
        une requête) la tient ; une copie retirée (expirée, cache vidé) n'est
        effacée qu'une fois tous ses fils terminés, ou au plus tard
        SHARED_HOLD_MAX_S après son retrait, et à l'arrêt. Un fil qui
        reprend le même secret rend sa copie précédente."""
        now = self._clock()
        with self._lock:
            hit = self._shared.get(ref)
            if hit and (hit[1] <= now or not os.path.exists(hit[0])):
                self._retire(ref)
                hit = None
            if hit:
                self._hold(hit[0], ref)
                self._sweep()
                return hit[0]
        value = self.get(ref, actor)
        p = write_private_copy(self._files_dir(), purpose, value)
        with self._lock:
            hit = self._shared.get(ref)
            if hit and hit[1] > now and os.path.exists(hit[0]):
                _unlink(p)                       # un autre fil l'a écrite entre-temps (M4)
                p = hit[0]
            else:
                if hit:
                    self._retire(ref)
                self._shared[ref] = (p, now + self.cache_ttl)
                self._copies[str(p)] = {"ref": ref, "holders": [], "retired": None}
            self._hold(p, ref)
            self._sweep()
        return p

    def _retire(self, ref):
        p, _until = self._shared.pop(ref)
        c = self._copies.get(str(p))
        if c is not None and c["retired"] is None:
            c["retired"] = self._clock()

    def _retire_derived(self, group):
        c = self._copies.get(self._derived.pop(group))
        if c is not None and c["retired"] is None:
            c["retired"] = self._clock()

    def group_of(self, path):
        """Référence d'une copie partagée en service (ou groupe d'une copie
        dérivée), None pour tout autre chemin."""
        with self._lock:
            c = self._copies.get(str(path))
            return c["ref"] if c is not None and c["retired"] is None else None

    def adopt(self, path, group, ephemeral=False):
        """Copie dérivée d'une copie partagée (kubeconfig usurpé, revue I2) :
        une seule en service par groupe, tenue par le fil qui l'a demandée,
        effacée comme les copies partagées (fils terminés, arrêt).
        `ephemeral` (copie d'un espace Terraform, revue I3) : effacée dès que
        les fils qui la tiennent sont terminés, même en service."""
        with self._lock:
            old = self._derived.get(group)
            if old is not None and old != str(path):
                self._retire_derived(group)
            self._derived[group] = str(path)
            self._copies.setdefault(str(path), {"ref": group, "holders": [], "retired": None,
                                                "ephemeral": ephemeral})
            self._hold(path, group)
            self._sweep()

    def sweep(self):
        with self._lock:
            self._sweep()

    def hold(self, path):
        """Le fil courant tient une copie déjà servie ; False si elle n'est
        plus en service."""
        with self._lock:
            c = self._copies.get(str(path))
            if c is None or c["retired"] is not None:
                return False
            self._hold(path, c["ref"])
            return True

    def hold_for(self, path, lease):
        """L'action `lease` (Lease) tient la copie `path` jusqu'à sa fin,
        quel que soit le fil qui l'a reçue (revue I1 des tâches 5-7 : copie
        résolue par le fil de la requête, utilisée par le fil de l'action).
        False si `path` n'est pas une copie de ce magasin."""
        with self._lock:
            c = self._copies.get(str(path))
            if c is None:
                return False
            if not any(w() is lease for w in c["holders"]):
                c["holders"].append(lease.ref)
            return True

    def lease_thread_holds(self, lease):
        """Les copies tenues par le fil courant passent aussi à `lease`
        (création d'une action dans le fil d'une requête)."""
        me = threading.current_thread()
        with self._lock:
            for c in self._copies.values():
                if any(w() is me for w in c["holders"]) and not any(w() is lease for w in c["holders"]):
                    c["holders"].append(lease.ref)

    def _hold(self, p, ref):
        me = threading.current_thread()
        for path, c in self._copies.items():
            if c["ref"] == ref and path != str(p):
                c["holders"] = [w for w in c["holders"] if w() is not me]
        c = self._copies[str(p)]
        if not any(w() is me for w in c["holders"]):
            c["holders"].append(weakref.ref(me))

    def _sweep(self):
        now = self._clock()
        for path, c in list(self._copies.items()):
            if c["retired"] is None and not c.get("ephemeral"):
                continue
            alive = [w for w in c["holders"] if w() is not None and w().is_alive()]
            c["holders"] = alive
            if not alive or (c["retired"] is not None and now - c["retired"] > SHARED_HOLD_MAX_S):
                _unlink(path)
                del self._copies[path]
                if self._derived.get(c["ref"]) == path:
                    del self._derived[c["ref"]]


class Lease:
    """Bail d'une action sur des copies de travail : vivant jusqu'à
    `release()` (fin de l'action), quel que soit le fil qui tient la copie."""

    def __init__(self):
        self._alive = True

    def is_alive(self):
        return self._alive

    def ref(self):                                  # forme d'un weakref : holder() -> objet vivant
        return self

    def release(self):
        self._alive = False


# ---------------------------------------------------------------------------
# Magasin du processus : mémoire (plan-run), fichiers, OpenBao
# ---------------------------------------------------------------------------
def spawned(env=None):
    """Processus lancé par la console (et pas la console elle-même ni un de
    ses lecteurs) : il ne se connecte pas, il lit les copies remises."""
    env = os.environ if env is None else env
    mark = env.get(SPAWNED_ENV)
    return bool(mark) and mark != str(os.getpid()) and env.get("HARVESTER_OPS_READ_WORKER") != "1"


def spawned_file(ref, required=True):
    """Chemin de la copie de travail remise par la console pour `ref` (une
    référence `openbao:` ou `local:`, telle qu'écrite dans la configuration).
    Absente : SecretError `openbao-spawned-no-file` (jamais de connexion), ou
    None si `required` est faux (une référence `local:` se lit alors sur
    place)."""
    _b, cls, path, _f = parse_ref_full(ref)
    mp = os.environ.get(FILES_ENV)
    try:
        files = json.loads(Path(mp).read_text()) if mp else {}
    except (OSError, ValueError):
        files = {}
    p = files.get(ref) if isinstance(files, dict) else None
    if p and os.path.isfile(p):
        return Path(p)
    if not required:
        return None
    raise SecretError("openbao-spawned-no-file", cls, path,
                      "launched by the console without a working file for this secret")


def default_client_factory(settings):
    """Fabrique des clients d'un processus : une connexion AppRole par
    processus (identifiants lus dans /etc/harvester-ops/openbao/), une copie
    du client par action pour les en-têtes de corrélation."""
    import openbao_client as oc
    base = {}
    lock = threading.Lock()

    def factory(actor=None, action=None):
        if spawned():
            raise oc.OpenBaoError("openbao-login-failed", None, "a tool launched by the console does not log in")
        with lock:
            c = base.get("c")
            if c is None:
                d = openbao_dir()
                ca = settings.get("ca_file") or d / "ca.pem"
                try:
                    ca_pem = Path(ca).read_text() if Path(ca).is_file() else None
                except OSError:
                    raise oc.OpenBaoError("openbao-tls", None, "OpenBao CA file unreadable") from None
                c = oc.Client(settings["addr"], ca_pem=ca_pem, timeout=oc.clamp_timeout(settings.get("timeout")))
                def secret_id():
                    try:
                        return (d / "secret-id").read_text().strip()
                    except OSError:
                        raise oc.OpenBaoError("openbao-login-failed", None,
                                              "AppRole credentials unreadable") from None
                try:
                    role_id = (d / "role-id").read_text().strip()
                except OSError:
                    raise oc.OpenBaoError("openbao-login-failed", None, "AppRole credentials unreadable") from None
                # relu à chaque connexion : l'agent le fait tourner tous les 30 jours
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
        self._old_bao = []
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
                self._old_bao.append(self._bao)     # copies encore tenues : balayées plus tard
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
            # 2.9.0 (décision 141, tâche 10) : OpenBao actif, les secrets d'un
            # déroulé y sont gardés jusqu'à sa fin (reprise sans les
            # redemander) ; inactif, la mémoire de la 2.4.0.
            bao = self.openbao
            if bao is not None and cls == PLAN_RUN:
                make_ref(cls, path, field)
                try:
                    return bao.put(cls, path, field, value, actor=actor, action=action)
                except SecretError as e:
                    if e.code not in DOWN_CODES:
                        raise
                    # OpenBao coupé : secrets tapés gardés en mémoire comme en
                    # 2.4.0 (redemandés si la console s'arrête)
            return super().put(cls, path, field, value)
        make_ref(cls, path, field)
        bao = self.openbao                          # une seule lecture (revue M9)
        if bao is not None and cls in (self.settings.get("migrated") or {}):
            self._refuse_owned(make_ref(cls, path, field, "openbao"), "written")
            return bao.put(cls, path, field, value, actor=actor, action=action)
        return self.files.put(cls, path, field, value)

    def get(self, ref, actor=None):
        try:
            backend, cls, path, _field = parse_ref_full(ref)
        except ValueError:
            raise SecretMissing(ref if isinstance(ref, str) else "(invalid)") from None
        if spawned() and CLASSES[cls]["persist"]:
            p = spawned_file(ref, required=backend == "openbao")
            if p is not None:
                return p.read_text()
        if backend == "openbao":
            return self._need_bao(cls, path).get(ref, actor)
        if not CLASSES[cls]["persist"]:
            try:
                return super().get(ref)
            except SecretMissing:
                bao = self.openbao if cls == PLAN_RUN else None
                if bao is None:
                    raise
                with self._lock:
                    here = any(r.startswith(f"local:{cls}/{path}#") for r in self._data)
                if here:
                    # ce secret vit en mémoire (OpenBao coupé à l'écriture) :
                    # un champ absent est absent, OpenBao n'est pas consulté
                    raise
            # pas en mémoire, OpenBao actif : gardé dans OpenBao (décision 141)
            return bao.get(make_ref(cls, path, _field, "openbao"), actor)
        v = self.files.get(ref)
        if is_openbao_ref(v):                       # valeur locale remplacée par sa référence
            return self.get(v, actor)
        return v

    def _refuse_owned(self, ref, what):
        """Revue C1 de la tâche 8 : un secret rangé par NOM (cluster, hôte,
        action annexe) n'est jamais écrit ni retiré quand une AUTRE
        déclaration désigne ce chemin (cluster renommé dont le secret n'a
        pas suivi, nom repris entre-temps) : ce serait un croisement
        d'identifiants ou une perte."""
        _b, cls, path, _f = parse_ref_full(ref)
        if not CLASSES.get(cls, {}).get("persist"):
            return
        others = [u for u in self.files.ref_users(ref) if u != path]
        if others:
            raise SecretError("secret-path-owned", cls, path,
                              f"not {what}: the declaration of {others[0]} uses this secret")

    def move(self, ref, new_path, actor=None, action=None):
        """Copie vérifiée du secret `ref` sous `new_path` (renommage) ; rend
        la nouvelle référence. La source reste jusqu'à `delete`."""
        _b, cls, _p, _f = parse_ref_full(ref)
        self._refuse_owned(make_ref(cls, new_path, _f, "openbao"), "written")
        return self._need_bao(cls).copy_new(ref, new_path, actor=actor, action=action)

    def delete(self, ref, actor=None, action=None):
        backend, cls, _path, _f = parse_ref_full(ref)
        if backend == "openbao":
            self._refuse_owned(ref, "removed")
            return self._need_bao(cls).delete(ref, actor=actor, action=action)
        if not CLASSES[cls]["persist"]:
            return super().delete(ref)
        raise NotImplementedError("local persistent secrets are removed by their own module")

    def delete_tree(self, cls, path, backend="local", actor=None, action=None):
        if backend == "openbao":
            return self._need_bao(cls, path).delete_tree(cls, path, actor=actor, action=action)
        n = super().delete_tree(cls, path)
        bao = self.openbao if cls == PLAN_RUN else None
        if bao is not None:
            # fin d'un déroulé : `metadata delete` de chaque feuille, versions
            # comprises (KV v2 n'a pas de suppression récursive, essai 5).
            # OpenBao coupé : le balayage des déroulés finis s'en chargera.
            try:
                n += bao.delete_tree(cls, path, actor=actor, action=action)
            except SecretError as e:
                if e.code not in DOWN_CODES:
                    raise
        return n

    def forget_memory(self, cls, path):
        """Copies EN MÉMOIRE seulement (`plan-run` gardé pendant une panne
        d'OpenBao) : la reprise ne lit que celles d'OpenBao, une copie en
        mémoire ne servirait plus (revue m6 des tâches 9-10)."""
        return LocalStore.delete_tree(self, cls, path)

    def refs(self, cls, path, backend="local"):
        if backend == "openbao":
            return self._need_bao(cls, path).refs(cls, path)
        return super().refs(cls, path)

    def children(self, cls, backend="openbao"):
        """Noms de premier niveau d'une classe dans OpenBao (balayage)."""
        return self._need_bao(cls).children(cls)

    def bao_client(self, actor=None, action=None):
        """Client connecté de la console (identité de service), pour les
        appels qui ne sont pas des lectures de secrets : jeton enfant
        d'OpenTofu, clés transit (tâche 9). SecretError openbao-inactive
        si OpenBao n'est pas actif."""
        import openbao_client as oc
        bao = self._need_bao()
        try:
            return bao._client(actor, action)
        except oc.OpenBaoError as e:
            raise (bao._failed(e, None, None) or SecretError(e.code, None, None, e.detail)) from None

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
        if spawned() and CLASSES[cls]["persist"]:
            p = spawned_file(ref, required=backend == "openbao")
            if p is not None:
                yield p                          # copie de la console : elle l'efface
                return
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
        if spawned() and CLASSES[cls]["persist"]:
            p = spawned_file(ref, required=backend == "openbao")
            if p is not None:
                return p
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
        self._sweep_old()
        return self._need_bao(cls, path).shared_file(ref, purpose, actor)

    def status(self):
        bao = self.openbao
        if bao is None:
            return {"mode": "local", "state": "local"}
        return dict(bao.status(), mode=self.settings.get("mode"), addr=self.settings.get("addr"))

    def clear_cache(self):
        if self._bao is not None:
            self._bao.clear_cache()

    def purge(self):
        for b in [self._bao, *self._old_bao]:
            if b is not None:
                b.purge()
        self._old_bao = []

    def hold_for(self, paths, lease):
        """Bail de l'action `lease` sur celles des `paths` qui sont des
        copies de travail (anciens supports compris) ; les autres chemins
        sont ignorés. Rend le nombre de copies tenues."""
        n = 0
        stores = [b for b in [self._bao, *self._old_bao] if b is not None]
        for p in paths or ():
            if not isinstance(p, (str, Path)):
                continue
            p = str(p).split("=", 1)[1] if str(p).startswith("--") and "=" in str(p) else str(p)
            n += sum(1 for b in stores if b.hold_for(p, lease))
        return n

    def lease_thread_holds(self, lease):
        for b in [self._bao, *self._old_bao]:
            if b is not None:
                b.lease_thread_holds(lease)

    def release(self, lease):
        """Fin de l'action : son bail tombe, ses copies retirées partent."""
        lease.release()
        for b in [self._bao, *self._old_bao]:
            if b is not None:
                b.sweep()
        self._sweep_old()

    def _sweep_old(self):
        for b in list(self._old_bao):
            with b._lock:
                b._sweep()
                if not b._copies:
                    self._old_bao.remove(b)


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
            s.purge()
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


# ---------------------------------------------------------------------------
# Écrivains des outils de bin/ lancés seuls (revue I4 de la tâche 8)
# ---------------------------------------------------------------------------
def cli_put(cls, path, field, value, actor=None, action=None, st=None):
    """Secret nouveau écrit par un outil de bin/ : sa référence quand la
    classe est migrée et que l'outil est lancé seul (il se connecte avec
    son propre jeton, comme la console) ; None quand la classe est locale
    ou la valeur trop grosse (l'outil écrit en local comme avant), et quand
    l'outil est lancé par la console (elle remet déjà des références : il
    ne se connecte pas). SecretError si OpenBao refuse."""
    st = st or store()
    if spawned() or not st.migrated(cls):
        return None
    if isinstance(value, bytes):
        value = value.decode()
    try:
        return st.put(cls, path, field, value, actor=actor, action=action)
    except SecretError as e:
        if e.code == "secret-too-big":
            return None
        raise
    except ValueError:                             # nom qui ne fait pas une référence
        return None


def hooks_to_refs(hooks, plan_key, put, keep=()):
    """En-têtes secrets des actions annexes d'un plan : chaque valeur
    nouvelle passe par `put` (`replication-hook/<plan>/<action>#hdr_<nom>`)
    et la copie rendue porte sa référence ; les valeurs de `keep`
    (`********`) et les références passent telles quelles. Partagé par la
    console (_secret_put) et harvester-replication lancé seul (cli_put)."""
    import copy
    out = copy.deepcopy(hooks)
    for h in out or []:
        sh = h.get("secret_headers") if isinstance(h, dict) else None
        if not isinstance(sh, dict):
            continue
        for k, v in list(sh.items()):
            if isinstance(v, str) and v and v not in keep and not is_ref(v):
                ref = put("replication-hook", f"{plan_key}/{h.get('name')}", secret_classes.header_field(k), v)
                if ref:
                    sh[k] = ref
    return out
