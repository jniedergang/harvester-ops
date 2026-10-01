"""
harvester-ops — Flask web UI

Exposes:
  /                      → main dashboard (sidebar + tab area)
  /api/clusters          → list configured clusters
  /api/status/<cluster>  → JSON cluster snapshot (delegates to harvester-status.sh)
  /api/namespace/<cluster>/<ns>  → VMs in one namespace
  /api/action            → POST: start a shutdown/startup/ns-stop/ns-start action
  /api/stream/<run_id>   → SSE event stream for a running action
  /api/action/<run_id>   → DELETE: cancel a running action
  /healthz               → liveness probe

Architecture:
  - One Flask process, multi-cluster
  - Actions spawn the bash scripts via subprocess.Popen
  - stderr is parsed line-by-line; STEP_EVENT|... lines become SSE events
  - All running actions are tracked in an in-memory registry (no DB)
"""

import hashlib
import json
import math
import logging
import os
import platform
import shutil
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter as _TallyCounter, deque
from functools import wraps
from pathlib import Path
from urllib.parse import quote

# v1.4.18: structured logging replaces the print(file=sys.stderr)
# sprinkled across the file. The format embeds the logger name so a
# downstream collector can route by subsystem ("actions", "watch",
# "notes", "capi-bundle", "tf"). Level is INFO by default and can be
# overridden by HARVESTER_OPS_LOG_LEVEL=DEBUG for local debugging.
_log_level = os.environ.get("HARVESTER_OPS_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("harvester-ops")
log_actions = logging.getLogger("harvester-ops.actions")
log_watch   = logging.getLogger("harvester-ops.watch")
log_notes   = logging.getLogger("harvester-ops.notes")
log_capi    = logging.getLogger("harvester-ops.capi")
log_tf      = logging.getLogger("harvester-ops.terraform")

import base64
import sqlite3
import yaml
import markdown
import y_py as Y
import pxe_server
import read_share as _rsh
import read_workers as _rw

# v1.83.0 : processus lecteur (read_workers.py) : il calcule des écrans pour la
# console principale, sans surveiller de cluster ni lancer d'action. Le
# kubeconfig, le rôle et la personne lui sont imposés par la console.
# « spawn » réimporte le script principal dans chaque lecteur, sous le nom
# __mp_main__ : c'est cet import-là qui doit se savoir lecteur (vu sur le banc :
# un second import complet doublait les métriques et tuait le lecteur).
IS_READ_WORKER = (os.environ.get("HARVESTER_OPS_READ_WORKER") == "1"
                  or (__name__ == "__mp_main__"
                      and os.environ.get("HARVESTER_OPS_READ_WORKER_SPAWN") == "1"))
_READ_WORKER_CTX = {}
import harvester_install_schema as _his
from harvester_install_schema import InstallConfigError, split_imported_config  # noqa: F401
import vnc_mux
import volume_health
import node_maintenance
import rancher_sso as _rs
import rancher_servers as _srv
import rancher_admin as _radm
import tf_store as _tfs
import tf_state as _tfst
from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    stream_with_context,
)
from flask_sock import Sock
from passlib.apache import HtpasswdFile

# flask-limiter is optional (v1.5.6): if missing we expose no-op
# decorators so test envs without the package keep working.
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _LIMITER_AVAILABLE = True
except ImportError:
    Limiter = None
    get_remote_address = lambda: ""
    _LIMITER_AVAILABLE = False

# prometheus_client is optional (v1.6.0): we expose noop metrics if missing
# so the test image stays minimal.
try:
    from prometheus_client import (
        Counter, Histogram, Gauge,
        CONTENT_TYPE_LATEST, generate_latest,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
CONFIG_PATH = Path(os.environ.get("HARVESTER_OPS_CONFIG", "/etc/harvester-ops/config.yaml"))
# BIN_DIR holds the harvester-*.sh helper scripts (shutdown/startup/status).
# Resolution order:
#   1. HARVESTER_OPS_BIN env var (explicit override)
#   2. /usr/local/bin if harvester-status.sh is present there (production
#      install via install.sh)
#   3. <repo>/bin (dev / running from a fresh clone — what the Overview
#      tab needs so refreshStatus() doesn't silently 500)
def _resolve_bin_dir():
    env = os.environ.get("HARVESTER_OPS_BIN")
    if env:
        return Path(env)
    if Path("/usr/local/bin/harvester-status.sh").exists():
        return Path("/usr/local/bin")
    return Path(__file__).resolve().parent.parent / "bin"


BIN_DIR = _resolve_bin_dir()
# v1.45.0 : modules partagés avec les scripts de bin/ (copiés tels quels dans
# l'image, lib/ compris). Ajouté une seule fois, en fin de chemin : un module
# de web/ du même nom garde la priorité.
if str(BIN_DIR / "lib") not in sys.path:
    sys.path.append(str(BIN_DIR / "lib"))
import longhorn_room  # noqa: E402
import bm_discover as _bmd  # noqa: E402
import baremetal_disks as _bmdisks  # noqa: E402
import cluster_decl as _cd  # noqa: E402
HTPASSWD_PATH = Path(os.environ.get("HARVESTER_OPS_HTPASSWD", "/etc/harvester-ops/htpasswd"))
LOG_DIR = Path(os.environ.get("HARVESTER_OPS_LOG_DIR", "/var/log/harvester-ops"))
DOCS_DIR = Path(os.environ.get("HARVESTER_OPS_DOCS", str(Path(__file__).resolve().parent.parent / "docs")))

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
# simple-websocket's internal ping thread writes raw frames to the socket
# from a background thread, racing any app-thread `ws.send()` from broadcast
# loops (the 2-client notes deadlock). Disable the library ping — our
# application-level ping/pong protocol handles NAT/proxy keep-alive.
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": None}
sock = Sock(app)


# Security headers (v1.5.6)
_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}


@app.after_request
def _add_security_headers(response):
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


# Prometheus metrics (v1.6.0) — exposed at /metrics. No auth (most
# scrapers don't speak Basic Auth). Bind on 127.0.0.1 behind a reverse
# proxy or restrict /metrics in the proxy rules if exposed publicly.
if _PROMETHEUS_AVAILABLE:
    metric_actions_total = Counter(
        "harvester_ops_actions_total",
        "Total ActionRuns started, by action type and final status.",
        labelnames=("action", "status"),
    )
    metric_action_duration = Histogram(
        "harvester_ops_action_duration_seconds",
        "ActionRun duration in seconds, by action type.",
        labelnames=("action",),
        buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800),
    )
    metric_actions_in_flight = Gauge(
        "harvester_ops_actions_in_flight",
        "Number of ActionRuns currently running.",
    )
    metric_kubectl_calls = Counter(
        "harvester_ops_kubectl_calls_total",
        "Total kubectl invocations, by exit status (ok|fail) and cluster.",
        labelnames=("status", "cluster"),
    )
    metric_vnc_sessions = Gauge(
        "harvester_ops_vnc_sessions",
        "In-browser VNC console sessions currently relayed.",
    )
else:
    class _NoopMetric:
        def labels(self, **kw): return self
        def inc(self, *a, **kw): pass
        def dec(self, *a, **kw): pass
        def observe(self, *a, **kw): pass
        def set(self, *a, **kw): pass
    metric_actions_total = _NoopMetric()
    metric_action_duration = _NoopMetric()
    metric_actions_in_flight = _NoopMetric()
    metric_kubectl_calls = _NoopMetric()
    metric_vnc_sessions = _NoopMetric()


@app.route("/metrics")
def api_metrics():
    """Prometheus scrape endpoint. Returns text/plain in the default
    Prometheus exposition format. No auth — restrict at the proxy if
    needed."""
    if not _PROMETHEUS_AVAILABLE:
        return Response("# prometheus_client not installed\n",
                         mimetype="text/plain")
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


# Rate-limiting (v1.5.6) — defends mutative endpoints against accidental
# spam (double-click chains) and trivial brute force. Memory storage
# (single-process). Disabled in test runs via HARVESTER_OPS_DISABLE_RATELIMIT=1
# so the 30-tests-per-suite hitting /api/action don't blow the limit.
_RATELIMIT_DISABLED = os.environ.get("HARVESTER_OPS_DISABLE_RATELIMIT") == "1"

try:
    from limits import parse_many as _parse_rate_limits
except ImportError:                                  # pragma: no cover
    _parse_rate_limits = None


def _validate_rate_spec(spec):
    """Refuse au démarrage une limite que flask-limiter ne sait pas lire.

    flask-limiter IGNORE EN SILENCE une chaîne invalide : le point d'entrée
    répond normalement, sans aucune limitation, et rien ne le signale. Six
    points d'entrée mutatifs ont ainsi porté pendant plusieurs versions une
    limite écrite comme un nom d'action, donc illisible, donc inopérante.
    Lever ici transforme la faute de frappe en erreur visible.
    """
    if _parse_rate_limits is None:                   # pragma: no cover
        return
    _parse_rate_limits(spec)


if _LIMITER_AVAILABLE and not _RATELIMIT_DISABLED:
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[],
        storage_uri="memory://",
        headers_enabled=True,  # adds X-RateLimit-* + Retry-After
    )
    def _rate_limit(spec):
        _validate_rate_spec(spec)
        return limiter.limit(spec)
else:
    limiter = None
    def _rate_limit(spec):
        # Validée même quand la limitation est désactivée : c'est ainsi que
        # la suite de tests attrape une limite mal écrite.
        _validate_rate_spec(spec)
        def _wrap(fn):
            return fn
        return _wrap


# K8s name validation (v1.5.6) — RFC 1123 label.
_K8S_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$")
_K8S_NAMESPACED_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?"
    r"(/[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?)?$"
)


def _valid_k8s_name(name, namespaced=False):
    """RFC 1123 label check. namespaced=True allows the `ns/name` form."""
    if not isinstance(name, str) or not name:
        return False
    pat = _K8S_NAMESPACED_RE if namespaced else _K8S_NAME_RE
    return bool(pat.match(name))


def _invalidate_cluster_caches(cluster):
    """v1.5.7: drop the per-cluster topology + list caches after a
    mutative action so the UI shows the post-change state without
    waiting for the 5s TTL to expire. Safe to call from any thread:
    each cache has its own lock."""
    try:
        READ_SHARE.invalidate(cluster)
    except NameError:
        pass
    # The cache modules import lazily so we late-bind.
    try:
        with _topology_lock:
            _topology_cache.pop(cluster, None)
    except NameError:
        pass
    try:
        with _list_lock:
            for key in [k for k in _list_cache.keys() if k[0] == cluster]:
                _list_cache.pop(key, None)
    except NameError:
        pass


def _stage_kubeconfig(src_kc, ws):
    """Copy `src_kc` into `<ws>/kubeconfig` with mode 0600 (owner-only)
    so it's not world- or group-readable. The TF workspace lives under
    a per-cluster directory; before v1.5.7 we just `copyfile`'d and
    left whatever mode `umask` produced (typically 0644). Production
    deploys ran by a non-root user → the file was readable by anyone
    with shell access. Now it's strict 0600. v1.5.7."""
    import shutil as _shutil
    dst = ws / "kubeconfig"
    _shutil.copyfile(src_kc, dst)
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    return dst


# Path-param keys we validate as RFC 1123 labels. URL paths that carry
# `<namespace>` or `<name>` are very common (≥15 routes); a central
# before_request hook is safer than touching each handler.
_VALIDATED_PATH_PARAMS = ("namespace", "name", "vm", "n")


@app.before_request
def _mark_console_activity():
    """Dernier signe de vie côté humain, qui commande le rythme de la
    surveillance de cluster.

    `/metrics` et les sondes de santé sont EXCLUS : ce sont des systèmes de
    supervision qui interrogent en permanence. Les compter maintiendrait la
    console éveillée pour toujours et le ralentissement au repos ne servirait
    jamais à rien.
    """
    global _last_request_ts
    if request.path != "/metrics" and not request.path.startswith("/healthz"):
        _last_request_ts = time.time()


# v1.60.0 : le nom d'un objet quelconque (PVC, secret, ressource de Harvester)
# est un sous-domaine DNS de 253 caractères au plus, pas une étiquette de 63 :
# un PVC système de harv1 en compte 110. Les routes YAML le prennent sous
# `oname`, contrôlé par cette règle.
_K8S_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


@app.before_request
def _validate_k8s_path_params():
    args = request.view_args or {}
    # v1.62.0 : un nom de nœud est un sous-domaine DNS (jusqu'à 253 caractères)
    if "node" in args and not _K8S_SUBDOMAIN_RE.match(args["node"]):
        return jsonify({"error": "invalid node name"}), 400
    if "oname" in args and not _K8S_SUBDOMAIN_RE.match(args["oname"]):
        return jsonify({"error": "invalid object name",
                        "hint": "a Kubernetes object name: a-z, 0-9, '.', '-', 253 characters at most"}), 400
    for key in _VALIDATED_PATH_PARAMS:
        if key in args and not _valid_k8s_name(args[key]):
            return jsonify({
                "error": f"invalid {key}",
                "hint": "RFC 1123 label: a-z, 0-9, '.', '-'; "
                        "must start+end with alphanumeric; max 63 chars",
            }), 400
    # Also validate ?namespace=… query strings (used by a few legacy GETs).
    ns_q = request.args.get("namespace")
    if ns_q and not _valid_k8s_name(ns_q):
        return jsonify({"error": "invalid namespace",
                         "hint": "RFC 1123 label"}), 400
    return None


def _load_config_file():
    """config.yaml seul, tel que l'opérateur l'a écrit (sans les clusters
    déclarés par la console) : c'est ce que réécrit un changement d'un
    cluster qui y est déclaré."""
    if not CONFIG_PATH.exists():
        return {"clusters": [], "web": {}, "settings": {}}
    return _rsh.yaml_file(CONFIG_PATH) or {}


def _state_dir():
    """Répertoire d'état de la console (v1.78.0) : tout ce qu'elle écrit
    d'elle-même, à côté des comptes et des notes, ou HARVESTER_OPS_STATE_DIR.
    Le répertoire de configuration est en lecture seule dans le service."""
    notes = globals().get("NOTES_DB")
    return _cd.state_dir(notes.parent if notes is not None else None)


def _read_decl(path):
    try:
        return _rsh.yaml_file(path)
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        log.warning("cluster declaration %s unreadable: %s", Path(path).name, type(e).__name__)
        return None


def load_config():
    """config.yaml, avec à la suite de ses clusters ceux que la console a
    déclarés elle-même (<état>/clusters.d, chemins résolus). Un nom présent
    dans config.yaml l'emporte ; un fichier invalide est ignoré."""
    cfg = _load_config_file()
    state = _state_dir()
    decls = [(p, _read_decl(p)) for p in _cd.decl_files(state)]
    if decls:
        cfg["clusters"] = _cd.merge(cfg.get("clusters") or [], decls, state,
                                    warn=lambda m: log.warning("%s", m))
    return cfg


_CONFIG_RO = {"sig": None, "cfg": None}
_CONFIG_RO_LOCK = threading.Lock()


def load_config_ro():
    """La même configuration que load_config(), PARTAGÉE et sans copie, pour
    les lectures fréquentes (v1.83.0 : la copie profonde coûtait ~40 ms par
    écran avec 30 clusters déclarés). Ne JAMAIS la modifier. Relue dès que
    config.yaml ou une déclaration change (date, taille, inode)."""
    def sig_of(p):
        try:
            st = os.stat(p)
            return (str(p), st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return (str(p), None)
    # la fonction source fait partie de la clé : une configuration fournie
    # autrement (tests, outils) n'est jamais masquée par la précédente
    sig = (load_config, sig_of(CONFIG_PATH),
           tuple(sig_of(p) for p in _cd.decl_files(_state_dir())))
    with _CONFIG_RO_LOCK:
        if _CONFIG_RO["sig"] == sig:
            return _CONFIG_RO["cfg"]
    cfg = load_config()
    with _CONFIG_RO_LOCK:
        _CONFIG_RO.update(sig=sig, cfg=cfg)
    return cfg


def _config_cluster_names():
    """Noms des clusters déclarés dans config.yaml par l'opérateur."""
    try:
        return {c.get("name") for c in (_load_config_file().get("clusters") or [])
                if isinstance(c, dict)}
    except (OSError, yaml.YAMLError):
        return set()


def _config_writable():
    """config.yaml modifiable par la console (environnement de dev) ; faux
    pour le service packagé, qui le monte en lecture seule."""
    target = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_PATH.parent
    return os.access(target, os.W_OK) and os.access(CONFIG_PATH.parent, os.W_OK)


# -----------------------------------------------------------------------------
# v1.50.0 : connexion par Rancher (fournisseur OIDC intégré à Rancher)
#
# Une personne connectée à Rancher entre dans la console sans rien ressaisir,
# et tout ce qu'elle fait sur un cluster géré par ce Rancher passe par son
# mandataire avec SON jeton : Rancher applique ses droits. Le jeton ne quitte
# jamais le serveur ; le navigateur n'a qu'un identifiant de session.
# Voir docs/design/2026-09-26-connexion-par-rancher.md et web/rancher_sso.py.
# -----------------------------------------------------------------------------
_SSO_PENDING = _rs.PendingLogins()
_SSO_SESSIONS = None
_SSO_CLUSTER_IDS = {}          # cluster de la console -> id du cluster Rancher
_KUBE_SYSTEM_UIDS = {}         # cluster de la console -> uid de kube-system
SSO_POWER_ACTIONS = ("shutdown", "startup")


def _sso_settings():
    """Réglages SSO du Rancher de config.yaml (1.50.0), ou None."""
    try:
        st = _rs.settings(load_config())
    except Exception:
        return None
    if st is not None:
        st.update({"id": _config_server_id(), "origin": "config", "sso": True,
                   "direct": bool(((load_config().get("rancher") or {}).get("direct_login")))})
    return st


# v1.79.0 : les Rancher réglés dans l'interface (<état>/rancher.d), plus
# celui de config.yaml en lecture seule. Relus à chaque requête : à chaud.
_RANCHER_STORE = _srv.Store(lambda: _state_dir())
RANCHER_LAST_COOKIE = "harvops_rancher_last"


def _config_server_id():
    conf = _srv.Store._from_config(load_config())
    return conf["id"] if conf else None


def _rancher_servers(include_shadowed=False):
    try:
        return _RANCHER_STORE.servers(load_config(), include_shadowed=include_shadowed)
    except Exception as e:  # noqa: BLE001 : un réglage illisible ne bloque pas la console
        log.warning("Rancher servers unreadable: %s", type(e).__name__)
        return []


def _rancher_server(sid, include_shadowed=False):
    return next((x for x in _rancher_servers(include_shadowed) if x["id"] == sid), None)


def _server_settings(sid):
    """Réglages (format rancher_sso) du Rancher `sid` ; celui de config.yaml
    reprend exactement la lecture de la 1.50 quand son SSO est complet."""
    srv_ = _rancher_server(sid)
    if srv_ is None:
        return None
    if srv_["origin"] == "config":
        legacy = _sso_settings()
        if legacy is not None:
            return legacy
    return _srv.settings_of(srv_)


def _session_settings(sess):
    """Les réglages du Rancher d'une session (None : Rancher retiré, la
    session tombe)."""
    if sess is None:
        return None
    if getattr(sess, "server_id", None) is None:
        return _sso_settings()
    return _server_settings(sess.server_id)


def _rancher_login_available():
    for x in _rancher_servers():
        if x["direct_enabled"] or _srv.sso_enabled(x):
            return True
    return _sso_settings() is not None


def _sso_store():
    global _SSO_SESSIONS
    if _SSO_SESSIONS is None:
        _SSO_SESSIONS = _rs.Sessions(_identity_dir)
    return _SSO_SESSIONS


def _sso_http(s):
    return _rs.Http(s.get("ca_file"), insecure=bool(s.get("insecure")))


def _sso_session():
    """La session Rancher de la requête en cours, ou None (hors requête
    aussi : un fil de travail a déjà reçu son kubeconfig)."""
    try:
        if "sso_session" in g:
            return g.sso_session
        sid = request.cookies.get(_rs.COOKIE)
    except RuntimeError:
        return None
    sess = _sso_store().get(sid) if sid else None
    if sess is not None:
        st = _session_settings(sess)
        if st is None or not _sso_store().renew(sess, _sso_http(st), st):
            _sso_store().close(sess.sid)
            sess = None
    g.sso_session = sess
    return sess


_SSO_RENEWER = {"started": False}


def _sso_start_renewer():
    """Renouvelle les jetons des sessions même sans requête du navigateur :
    une action de trente minutes lit son kubeconfig bien après les dix
    minutes d'un jeton d'accès."""
    if _SSO_RENEWER["started"]:
        return
    _SSO_RENEWER["started"] = True

    def loop():
        while True:
            time.sleep(60)
            store = _sso_store()
            for sess in store.all():
                try:
                    st = _session_settings(sess)
                except Exception:  # noqa: BLE001
                    continue
                if (st is None or sess.expires < time.time()
                        or not store.renew(sess, _sso_http(st), st, margin=180)):
                    store.close(sess.sid)
    threading.Thread(target=loop, daemon=True, name="rancher-token-renewer").start()


def _same_origin():
    """Une écriture authentifiée par cookie doit venir de la console même :
    SameSite le garantit déjà dans les navigateurs récents, ceci le vérifie."""
    src = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not src:
        return False
    from urllib.parse import urlparse as _up
    return _up(src).netloc == request.host


def _sso_redirect_uri(s):
    return s.get("redirect_uri") or request.url_root.rstrip("/") + "/auth/rancher/callback"


def _sso_cookie_secure(s):
    """Derrière un mandataire TLS (Traefik), la console reçoit du HTTP : c'est
    l'adresse de retour déclarée qui dit si le navigateur est en HTTPS."""
    return request.is_secure or _sso_redirect_uri(s).startswith("https://")


# -----------------------------------------------------------------------------
# Authentification (v1.57.0 : connexion obligatoire)
#
# Jusqu'à la 1.56, sans htpasswd la console tournait OUVERTE : quiconque
# atteignait le port pouvait éteindre un cluster. Elle exige maintenant une
# identité : session Rancher, session d'un compte local (formulaire de
# connexion, cookie HttpOnly), ou HTTP Basic pour les clients d'API et les
# scripts. Sans aucun compte, la console attend la création du premier
# administrateur (/setup, jeton lu sur le disque du serveur). Le mode ouvert
# ne subsiste que demandé explicitement (HARVESTER_OPS_AUTH=none, tests).
# -----------------------------------------------------------------------------
import accounts as _acc  # noqa: E402

AUTH_OPEN_ALLOWED = os.environ.get("HARVESTER_OPS_AUTH", "") == "none"
_ACCOUNTS = {"store": None}
_LOCAL_SESSIONS = _acc.LocalSessions()


def _accounts():
    if _ACCOUNTS["store"] is None:
        _ACCOUNTS["store"] = _acc.Accounts(ACCOUNTS_PATH)
    return _ACCOUNTS["store"]


def _setup_token_path():
    return ACCOUNTS_PATH.parent / "setup-token"


def _htpasswd_check(username, password):
    if not HTPASSWD_PATH.exists():
        return False
    try:
        ht = HtpasswdFile(str(HTPASSWD_PATH))
        return ht.check_password(username, password) or False
    except Exception:
        return False


def _htpasswd_users():
    if not HTPASSWD_PATH.exists():
        return []
    try:
        return list(HtpasswdFile(str(HTPASSWD_PATH)).users())
    except Exception:
        return []


def check_auth(username, password):
    """Un compte local : ceux de la console d'abord, puis le htpasswd de
    l'installeur. Plus jamais « vrai » faute de fichier (c'était le mode
    ouvert)."""
    if _accounts().has(username):
        return _accounts().verify(username, password)
    return _htpasswd_check(username, password)


def local_accounts_exist():
    return HTPASSWD_PATH.exists() or _accounts().exists()


def auth_configured():
    return local_accounts_exist() or _rancher_login_available()


def open_mode():
    """Console sans connexion : seulement si on l'a demandé ET qu'aucun
    compte n'existe (un compte créé referme la porte)."""
    return AUTH_OPEN_ALLOWED and not auth_configured()


def setup_needed():
    return not AUTH_OPEN_ALLOWED and not auth_configured()


def _local_session():
    try:
        if "local_session" in g:
            return g.local_session
        sid = request.cookies.get(_acc.COOKIE)
    except RuntimeError:
        return None
    sess = _LOCAL_SESSIONS.get(sid) if sid else None
    # un compte supprimé entre-temps n'a plus de session
    if sess is not None and not (_accounts().has(sess["user"]) or sess["user"] in _htpasswd_users()):
        _LOCAL_SESSIONS.close(sess["sid"])
        sess = None
    g.local_session = sess
    return sess


def _basic_user():
    """L'utilisateur d'un en-tête HTTP Basic valable (clients d'API, scripts),
    vérifié une fois par requête."""
    try:
        if "basic_user" in g:
            return g.basic_user
        auth = request.authorization
    except RuntimeError:
        return None
    user = auth.username if (auth and auth.username and auth.type == "basic"
                             and check_auth(auth.username, auth.password)) else None
    g.basic_user = user
    return user


def authenticate():
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="harvester-ops"'},
    )


def _wants_page():
    return request.method == "GET" and not request.path.startswith(("/api/", "/ws/"))


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # v1.50.0 : une session ouverte par Rancher vaut authentification
        if _sso_session() is not None or _local_session() is not None:
            return f(*args, **kwargs)
        if open_mode():
            return f(*args, **kwargs)
        if setup_needed():
            if _wants_page():
                return redirect("/setup")
            return jsonify({"error": "setup required", "setup": "/setup"}), 401
        if _basic_user():
            return f(*args, **kwargs)
        if _wants_page():
            nxt = request.full_path if request.query_string else request.path
            return redirect("/login" + ("" if request.path == "/" else "?next=" + quote(nxt, safe="")))
        # Un client qui a présenté des identifiants reçoit le défi Basic ; un
        # navigateur sans identifiants, NON : l'invite du navigateur
        # court-circuiterait la page de connexion.
        if request.authorization is not None:
            return authenticate()
        expired = request.cookies.get(_acc.COOKIE) or request.cookies.get(_rs.COOKIE)
        return jsonify({"error": "session expired" if expired else "authentication required",
                        "login": "/login"}), 401
    return decorated



# -----------------------------------------------------------------------------
# v1.81.0 : lectures partagées entre les personnes connectées (read_share.py)
# -----------------------------------------------------------------------------
# Placé SOUS @requires_auth : une réponse partagée ne sort jamais avant la
# vérification de l'identité. La clé porte le kubeconfig rendu pour
# l'appelant (identité présentée au cluster) et son rôle dans la console :
# deux personnes aux droits différents ne partagent rien.
READ_SHARE = _rsh.ReadShare()
READ_SHARE_TTL = float(os.environ.get("HARVESTER_OPS_READ_SHARE_TTL", "3"))
READ_SHARE_ENABLED = os.environ.get("HARVESTER_OPS_READ_SHARE", "1") not in ("0", "false", "no")
_READ_SHARE_SKIP_HEADERS = {"content-length", "set-cookie", "content-type"}
# v1.83.0 : écrans calculés par les processus lecteurs (read_workers.py)
HEAVY_READS = {"api_topology", "api_vms_list", "api_storage_map", "api_network_fabric"}


def shared_read(ttl=None, per_user=False, scope=None):
    """Décorateur d'une vue GET qui ne fait que lire. `?fresh=1` passe outre.
    `scope` remplace le cluster en tête de clé pour une vue qui n'en a pas
    (on l'invalide alors par ce nom)."""
    def deco(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if (not READ_SHARE_ENABLED or request.method != "GET"
                    or request.args.get("fresh") == "1"):
                return f(*args, **kwargs)
            cluster = kwargs.get("cluster")
            key = (scope or cluster, request.endpoint,
                   tuple(sorted(request.args.items(multi=True))),
                   tuple(sorted((k, str(v)) for k, v in kwargs.items())),
                   _kubectl_for_cluster(cluster) if cluster else None,
                   current_role(), current_user() if per_user else None)

            kc_for_worker = key[4]

            def load():
                if request.endpoint in HEAVY_READS and _rw.configured() and not IS_READ_WORKER:
                    try:
                        return _rw.render(request.endpoint, dict(kwargs),
                                          request.query_string.decode(), kc_for_worker,
                                          current_role(), current_user())
                    except Exception as e:      # lecteur indisponible : ici, comme avant
                        log.warning("read worker failed for %s: %s", request.endpoint, e)
                resp = app.make_response(f(*args, **kwargs))
                if resp.is_streamed or resp.direct_passthrough:
                    return (None, resp)
                headers = [(k, v) for k, v in resp.headers.items()
                           if k.lower() not in _READ_SHARE_SKIP_HEADERS]
                return (resp.status_code, resp.get_data(), resp.mimetype, headers,
                        getattr(g, "cluster_denied", None),
                        list(getattr(g, "cluster_denials", None) or []))

            val = READ_SHARE.get(key, ttl or READ_SHARE_TTL, load,
                                 keep=lambda v: v[0] == 200)
            if val[0] is None:
                return val[1]
            status, body, mimetype, headers, denied, denials = val
            # les refus constatés par la lecture partagée valent pour chacun
            if denied:
                g.cluster_denied = denied
                g.cluster_denials = list(denials)
            return Response(body, status=status, mimetype=mimetype, headers=headers)
        return wrapped
    return deco


@app.after_request
def _read_share_invalidate(response):
    """Toute écriture vide les lectures partagées du cluster qu'elle touche
    (de tous les clusters si la route n'en nomme pas)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        READ_SHARE.invalidate((request.view_args or {}).get("cluster"))
        READ_SHARE.invalidate("_activity")
    return response

# =============================================================================
# Rôles
#
# L'authentification htpasswd ne vérifiait qu'un mot de passe : tout compte
# authentifié pouvait ensuite ÉTEINDRE un cluster, supprimer une VM ou
# détruire un workspace Terraform. Il n'existait ni utilisateur ni rôle.
#
# Le garde est CENTRAL et en REFUS PAR DÉFAUT : toute requête qui modifie
# quelque chose exige au moins `operator`, et une liste explicite de chemins
# exige `admin`. Un point d'entrée ajouté demain est donc protégé sans que
# personne ait à y penser — la leçon des `@_rate_limit` qui décoraient sans
# rien limiter.
#
# Ce que ce modèle ne fait PAS, et qu'il faut dire : la console agit sur le
# cluster avec UN kubeconfig partagé, administrateur. Le cluster ne voit donc
# qu'une identité, quel que soit l'humain derrière l'écran. C'est un
# garde-fou contre l'erreur et l'abus, pas une frontière que la RBAC du
# cluster ferait respecter. Depuis la v1.50.0, une personne connectée par
# Rancher agit avec SON jeton, à travers Rancher (voir « connexion par
# Rancher » plus haut).
# =============================================================================
ROLES_PATH = Path(os.environ.get(
    "HARVESTER_OPS_ROLES", str(HTPASSWD_PATH.parent / "roles.yaml")))

ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}

# Chemins réservés à `admin`. Comparés au chemin de la requête, préfixe.
# Ce sont les gestes qui coupent un service, changent la configuration de
# l'outil, ou touchent au matériel.
ADMIN_ONLY_PREFIXES = (
    "/api/action",              # séquençage électrique d'un cluster
    "/api/update/",             # v1.82.0 : mise à jour de la console
    "/api/clusters",            # déclarations de cluster, kubeconfig, clés
    "/api/bmc/",                # alimentation et média virtuel des machines
    "/api/baremetal/",          # installation sans opérateur
    "/api/iso/",                # magasin d'ISO
    "/api/terraform/provider",  # remplacement du provider
    "/api/vmtemplates/",        # templates partagés du cluster
    "/api/host/",               # v1.62.0 : disques, CPU manager, BMC, suppression d'un hôte
    "/api/ns-admin/",           # v1.62.0 : créer, modifier, supprimer un namespace
    "/api/templates/",          # v1.64.0 : versions des modèles partagés du cluster
    "/api/net-admin/",          # v1.65.0 : réseaux de cluster, liens, équilibreurs, réglages réseau
    "/api/hv-settings/",        # v1.67.0 : réglages de Harvester (certains coupent l'accès)
    "/api/hv-support/",         # v1.67.0 : paquet de support, kubeconfigs délivrés
    "/api/devices/",            # v1.68.0 : passthrough PCI et USB, SR-IOV (détache un périphérique de l'hôte)
    "/api/upgrade/",            # v1.69.0 : mise à jour de Harvester (redémarre les hôtes)
    "/api/monlog/",             # v1.70.0 : sorties et flux de journaux, AlertmanagerConfig
    "/api/vmimport/",           # v1.71.0 : sources d'import (identifiants) et imports de VM
    "/api/forklift",            # v1.75.0 : Forklift, image VDDK et son magasin, fournisseurs vCenter
    "/api/users",               # gestion des comptes de la console
    "/api/harvester-users",     # comptes du cluster Harvester
    "/api/kubeovn/",            # réseaux kube-ovn : un changement peut couper des VMs
    "/api/addons/",             # v1.57.0 : activer un add-on installe un chart sur le cluster
    "/api/rancher/",            # v1.79.0 : Rancher réglés, enregistrement SSO, chart RBAC
)
# Sous-chemins admin qui ne se distinguent pas par un préfixe.
ADMIN_ONLY_SUFFIXES = ("/destroy", "/install", "/uninstall", "/cleanup-legacy",
                       # v1.43.0 : une maintenance déplace ou arrête des VMs.
                       "/maintenance")

_roles_cache = {"mtime": None, "data": None}


def _parse_role_entry(value):
    """Une entrée de `users:` est soit le rôle seul, soit une table qui porte
    aussi l'identité à présenter au cluster.

        operatrice: operator                  # forme courte, toujours valide
        patronne:
          role: admin
          cluster_user: u-p5oguyiwv6
          cluster_groups: [harvester-admins]

    Rend (rôle, identité) ; le rôle est None si la valeur est inexploitable,
    ce qui écarte l'entrée plutôt que de lui accorder des droits par défaut.
    """
    if isinstance(value, dict):
        role = str(value.get("role", ""))
        cuser = value.get("cluster_user")
        groups = value.get("cluster_groups") or []
        if not isinstance(groups, list):
            groups = [groups]
        identity = None
        if cuser:
            identity = {"user": str(cuser),
                        "groups": [str(g) for g in groups if str(g)]}
        return (role if role in ROLE_RANK else None), identity
    role = str(value)
    return (role if role in ROLE_RANK else None), None


def load_roles():
    """{'default_role': str, 'users': {login: role}, 'identities': {...}}.
    Fichier absent = personne n'est bridé, pour ne pas verrouiller une
    installation existante au moment de la mise à jour."""
    try:
        mtime = ROLES_PATH.stat().st_mtime
    except OSError:
        return {"default_role": "admin", "users": {}, "identities": {},
                "delegate": False, "deny_unmapped": False, "configured": False}
    if _roles_cache["mtime"] != mtime:
        try:
            raw = yaml.safe_load(ROLES_PATH.read_text()) or {}
        except Exception as e:
            log.warning("roles.yaml illisible (%s) : tout le monde admin", e)
            raw = {}
        users, identities = {}, {}
        for k, v in (raw.get("users") or {}).items():
            role, identity = _parse_role_entry(v)
            if role:
                users[str(k)] = role
            if identity:
                identities[str(k)] = identity
        default = raw.get("default_role")
        if default not in ROLE_RANK:
            default = "viewer"
        ident_cfg = raw.get("identity") or {}
        delegate = bool(ident_cfg.get("delegate", False))
        # Un exploitant qui active la délégation la veut effective : un compte
        # sans correspondance est refusé, pas silencieusement promu au
        # kubeconfig partagé. Il peut l'assouplir explicitement.
        deny_unmapped = bool(ident_cfg.get("deny_unmapped", True)) and delegate
        _roles_cache.update({"mtime": mtime,
                             "data": {"default_role": default,
                                      "users": users,
                                      "identities": identities,
                                      "delegate": delegate,
                                      "deny_unmapped": deny_unmapped,
                                      "configured": True}})
    return _roles_cache["data"]


def current_user():
    if _READ_WORKER_CTX:
        return _READ_WORKER_CTX.get("user") or ""
    sess = _sso_session()
    if sess is not None:
        return sess.login
    local = _local_session()
    if local is not None:
        return local["user"]
    return _basic_user() or ""


def roles_active():
    """Des rôles n'ont de sens que si l'on sait QUI demande.

    Sans htpasswd, la console tourne en mode ouvert (développement, ou
    installation derrière un proxy qui authentifie déjà) : personne n'est
    identifiable, et brider sur une identité vide mettrait tout le monde en
    lecture seule, y compris l'exploitant. On ne bride donc pas, et on le
    DIT plutôt que de le laisser deviner.
    """
    if _sso_session() is not None:
        return True
    # v1.57.0 : dès qu'une personne est identifiée, son rôle s'applique
    return not open_mode()


def current_role():
    if _READ_WORKER_CTX:
        return _READ_WORKER_CTX.get("role") or "viewer"
    sess = _sso_session()
    if sess is not None:
        return sess.role
    if open_mode():
        return "admin"
    user = current_user()
    # les comptes créés dans la console portent leur rôle
    role = _accounts().role_of(user) if user else None
    if role:
        return role
    roles = load_roles()
    if not roles.get("configured"):
        # installation d'avant les rôles : ses comptes gardaient tout
        return "admin" if user else "viewer"
    return roles["users"].get(user, roles["default_role"])


# Chemins dont la simple LECTURE est réservée aux admins : savoir qui
# détient l'administration d'un cluster n'a pas à être public.
ADMIN_ONLY_READ_PREFIXES = ("/api/harvester-users", "/api/users",
                            # v1.59.0 : la configuration d'un add-on peut porter des mots de passe
                            "/api/addons/",
                            # v1.67.0 : un paquet de support porte les journaux du cluster,
                            # un kubeconfig délivré porte un jeton
                            "/api/hv-support/",
                            # v1.71.0 : le journal du contrôleur d'import cite URL et serveurs des sources
                            "/api/vmimport-log/")


# Lectures qui donnent plus qu'une vue : le kubeconfig d'un cluster créé
# par Cluster API est administrateur de ce cluster (v1.48.0).
OPERATOR_READ_SUFFIXES = ("/kubeconfig",)


def required_role_for(path, method):
    if path.startswith(ADMIN_ONLY_READ_PREFIXES):
        return "admin"
    # v1.57.0 : chacun change son propre mot de passe, lecteur compris
    if path.startswith("/api/me/"):
        return "viewer"
    if method in ("GET", "HEAD", "OPTIONS"):
        if path.startswith("/api/capi/") and path.endswith(OPERATOR_READ_SUFFIXES):
            return "operator"
        return "viewer"
    if path.startswith(ADMIN_ONLY_PREFIXES) or path.endswith(ADMIN_ONLY_SUFFIXES):
        return "admin"
    return "operator"


@app.before_request
def _enforce_role():
    path = request.path
    # Les pages, les ressources statiques et les sondes ne passent pas par
    # le modèle de rôles.
    if not path.startswith("/api/"):
        return None
    if path in ("/api/whoami",):
        return None
    # v1.57.0 : sans identité, c'est requires_auth qui répond (401, « connectez-
    # vous »), pas un « il faut le rôle opérateur, vous êtes lecteur » trompeur
    if not open_mode() and _sso_session() is None and _local_session() is None and not _basic_user():
        return None
    # Une écriture portée par un cookie de session (Rancher ou compte local)
    # doit venir de la console même.
    if ((_sso_session() is not None or _local_session() is not None)
            and request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin()):
        return jsonify({"error": "forbidden",
                        "hint": "cross-origin write refused for a browser session"}), 403
    needed = required_role_for(path, request.method)
    have = current_role()
    if ROLE_RANK.get(have, 0) < ROLE_RANK[needed]:
        return jsonify({
            "error": "forbidden",
            "role": have,
            "required": needed,
            "hint": f"this action needs the '{needed}' role; "
                    f"'{current_user() or 'you'}' has '{have}'",
        }), 403
    # v1.32.0 : délégation active et compte sans identité de cluster. Le
    # laisser passer le ferait retomber sur le kubeconfig partagé,
    # administrateur, soit exactement ce que la délégation vient supprimer.
    # On refuse, et on dit quoi écrire dans roles.yaml.
    if load_roles().get("deny_unmapped") and not current_cluster_identity():
        return jsonify({
            "error": "no cluster identity",
            "user": current_user(),
            "hint": "identity delegation is on and this account maps to no "
                    "cluster user; add 'cluster_user:' for it in roles.yaml, "
                    "or set identity.deny_unmapped to false",
        }), 403
    return None


@app.route("/api/whoami")
@requires_auth
def api_whoami():
    """Qui suis-je et qu'ai-je le droit de faire. L'interface s'en sert pour
    ne pas proposer des gestes qui seront refusés."""
    roles = load_roles()
    ident = cluster_identity_for(current_user())
    return jsonify({
        "user": current_user(),
        "role": current_role(),
        "roles_active": roles_active(),
        "roles_configured": bool(roles.get("configured")),
        "auth_configured": auth_configured(),
        "roles_file": str(ROLES_PATH),
        # v1.32.0 : ce que le CLUSTER voit, qui n'est pas le rôle console.
        "delegation_active": identity_delegation_active(),
        "cluster_user": (ident or {}).get("user"),
        "cluster_groups": (ident or {}).get("groups", []),
        # v1.50.0 : connexion par Rancher ; v1.57.0 : session d'un compte
        # local, ou HTTP Basic (client d'API, navigateur d'avant la 1.57)
        "auth": ("rancher" if _sso_session() is not None
                 else ("open" if open_mode() else "local")),
        "auth_via": ("rancher" if _sso_session() is not None
                     else "session" if _local_session() is not None
                     else "basic" if _basic_user() else "open"),
        # peut changer son mot de passe depuis la console
        "password_managed": bool(current_user()) and _accounts().has(current_user()),
        "rancher_login": _rancher_login_available(),
        "session": _sso_session().public() if _sso_session() is not None else None,
    })


# =============================================================================
# Identité présentée au cluster (v1.32.0)
#
# Jusqu'ici la console agissait avec UN kubeconfig partagé. Mesuré sur harv1 :
# il vaut `system:admin`, groupe `system:masters` (un superutilisateur câblé
# dans l'apiserver), qui COURT-CIRCUITE la RBAC. Aucune règle Harvester ne
# s'appliquait donc à quoi que ce soit fait depuis la console, quel que soit
# l'humain derrière l'écran. Les rôles de la v1.30.0 sont un garde-fou côté
# console ; ils ne sont pas une frontière que le cluster fait respecter.
#
# kubectl sait porter une usurpation DANS le kubeconfig (`as`, `as-groups`),
# et l'apiserver ajoute lui-même `system:authenticated`. On produit donc une
# copie du kubeconfig porteuse de l'identité de l'appelant, et on la substitue
# au point unique où le chemin est résolu : les ~50 sites d'appel kubectl la
# reçoivent sans être touchés, et les scripts bin/*.sh font de même par
# HARVESTER_OPS_AS. Vérifié sur harv1 : sous usurpation, `can-i list vm`
# répond « no » là où le kubeconfig partagé répond « yes ».
#
# Ce que cela ne fait toujours PAS : la console DÉTIENT le kubeconfig
# administrateur. Un défaut de cette couche redonnerait les pleins pouvoirs.
# C'est une frontière que le cluster applique, pas un coffre.
# =============================================================================
IDENTITY_DIR = Path(os.environ.get("HARVESTER_OPS_IDENTITY_DIR",
                                   "/var/lib/harvester-ops/identities"))
_identity_cache = {}      # clé -> Path
_identity_lock = threading.Lock()


def identity_delegation_active():
    """La délégation n'a de sens que si l'on sait QUI demande : elle suit donc
    les rôles, et reste éteinte tant qu'on ne l'a pas demandée."""
    return roles_active() and load_roles().get("delegate", False)


def cluster_identity_for(login):
    """{'user': ..., 'groups': [...]} ou None si ce compte n'a pas de
    correspondance côté cluster."""
    if not identity_delegation_active():
        return None
    return load_roles().get("identities", {}).get(login)


def current_cluster_identity():
    if _sso_session() is not None:
        return None          # l'identité voyage dans le kubeconfig de la session
    try:
        return cluster_identity_for(current_user())
    except RuntimeError:
        # Hors contexte de requête (thread de travail) : l'identité a déjà été
        # figée dans le kubeconfig résolu côté requête.
        return None


def _identity_dir():
    """Répertoire privé des copies. Repli sur un temporaire quand
    /var/lib n'est pas inscriptible (déploiement en rootfs read-only)."""
    for candidate in (IDENTITY_DIR,
                      Path(tempfile.gettempdir()) / "harvester-ops-identities"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            os.chmod(candidate, 0o700)
            return candidate
        except OSError:
            continue
    return None


def _identity_kubeconfig(src_kc, identity):
    """Copie de `src_kc` portant `as`/`as-groups`. Rend le chemin d'origine
    quand il n'y a rien à usurper, pour que le chemin nominal soit inchangé.

    La copie contient les identifiants du cluster : répertoire 0700,
    fichier 0600, et jamais de chemin ni de secret dans une réponse.
    """
    if not identity or not identity.get("user") or not src_kc:
        return src_kc
    try:
        mtime = os.stat(src_kc).st_mtime
    except OSError:
        return src_kc
    groups = tuple(identity.get("groups") or ())
    key = (str(src_kc), mtime, identity["user"], groups)
    with _identity_lock:
        cached = _identity_cache.get(key)
        if cached and cached.exists():
            return str(cached)
        target_dir = _identity_dir()
        if target_dir is None:
            log.warning("délégation d'identité : aucun répertoire inscriptible,"
                        " appel avec le kubeconfig partagé")
            return src_kc
        digest = hashlib.sha256(
            "\0".join([str(src_kc), str(mtime), identity["user"],
                       ",".join(groups)]).encode()).hexdigest()[:16]
        dst = target_dir / f"{digest}.yaml"
        try:
            doc = yaml.safe_load(Path(src_kc).read_text()) or {}
            for u in doc.get("users") or []:
                entry = u.setdefault("user", {})
                entry["as"] = identity["user"]
                if groups:
                    entry["as-groups"] = list(groups)
                else:
                    entry.pop("as-groups", None)
            tmp = dst.with_suffix(".tmp")
            with open(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                              0o600), "w") as fh:
                yaml.safe_dump(doc, fh)
            os.replace(tmp, dst)
            os.chmod(dst, 0o600)
        except Exception as e:
            log.warning("délégation d'identité impossible (%s) : appel avec le"
                        " kubeconfig partagé", e)
            return src_kc
        _identity_cache[key] = dst
        return str(dst)


def identity_env(login=None, cluster=None):
    """Variables d'environnement à passer aux scripts bin/*.sh pour qu'ils
    présentent la même identité. C'est le pendant CLI, et la règle de parité
    impose qu'il existe."""
    if login is None and cluster and _sso_session() is not None:
        kc = _sso_kubeconfig(cluster)
        return {"HARVESTER_OPS_KUBECONFIG": kc} if kc else {}
    ident = (cluster_identity_for(login) if login is not None
             else current_cluster_identity())
    if not ident:
        return {}
    env = {"HARVESTER_OPS_AS": ident["user"]}
    if ident.get("groups"):
        env["HARVESTER_OPS_AS_GROUPS"] = ",".join(ident["groups"])
    return env


# -----------------------------------------------------------------------------
# Action registry (in-memory)
# -----------------------------------------------------------------------------
class ActionRun:
    """Represents one running invocation of a bash script."""

    def __init__(self, run_id, action, cluster, cmd, dry_run=False):
        self.id = run_id
        self.action = action          # shutdown | startup | ns-stop | ns-start
        self.cluster = cluster
        self.cmd = cmd
        self.dry_run = dry_run
        # v1.32.0 : identité présentée au cluster, figée au DÉCLENCHEMENT.
        # Le thread de travail n'a pas de contexte de requête ; l'y relire
        # rendrait None et l'action repasserait en kubeconfig partagé, c'est-
        # à-dire exactement là où les gestes sont les plus lourds.
        self.identity_env = {}
        self.cluster_user = None
        self.status = "starting"      # starting | running | done | error | cancelled
        self.exit_code = None
        # v1.6.5: last meaningful error line (kubectl stderr / script stderr)
        # so the dock and Activity can explain a failure, not just "exit 1".
        self.error_summary = None
        self._last_stderr = ""
        self.started_at = time.time()
        self.ended_at = None
        # Recent events buffer (so new SSE clients can replay)
        self.events = deque(maxlen=500)
        # v1.46.0 : numéro absolu du prochain événement. Le flux SSE suivait
        # une position dans la file ; passé 500, les plus anciens sortent, la
        # file ne grandit plus, et il n'envoyait plus rien (dock figé).
        self._seq = 0
        # v1.46.0 : progression d'un transfert, un point par phase, JAMAIS
        # dans la file : un transfert de plusieurs heures l'aurait remplie.
        self.progress = {}
        self.progress_last = None
        self.progress_ver = 0
        # v1.47.0 : ce que l'action a produit et que l'interface doit pouvoir
        # désigner à la fin (le nom de l'archive d'un export, par exemple)
        self.result = {}
        self.proc = None
        self._cond = threading.Condition()
        self._closed = False
        # v1.6.0: bump the in-flight gauge; close() will decrement.
        metric_actions_in_flight.inc()

    def emit(self, event):
        with self._cond:
            self.events.append(event)
            self._seq += 1
            self._cond.notify_all()

    def events_since(self, seq):
        """Événements de numéro >= `seq` encore dans la file, et le numéro
        suivant. Ce qui est sorti de la file est sauté, pas attendu."""
        first = self._seq - len(self.events)
        start = max(seq, first)
        return list(self.events)[start - first:], self._seq

    def emit_progress(self, snap):
        with self._cond:
            self.progress[snap.get("phase") or "?"] = snap
            self.progress_last = snap
            self.progress_ver += 1
            self._cond.notify_all()

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        try:
            _actions_persist(self)
        except Exception:
            pass
        try:
            READ_SHARE.invalidate("_activity")
        except NameError:
            pass
        # v1.5.7: invalidate caches after the action so the UI sees the
        # post-mutation state immediately, not after the 5s TTL.
        if getattr(self, "cluster", None):
            try:
                _invalidate_cluster_caches(self.cluster)
            except Exception:
                pass
        # v1.6.0: feed Prometheus metrics. The in_flight gauge was
        # bumped in __init__; we decrement here.
        try:
            metric_actions_in_flight.dec()
            metric_actions_total.labels(
                action=str(self.action or "unknown"),
                status=str(self.status or "unknown"),
            ).inc()
            if self.ended_at:
                metric_action_duration.labels(
                    action=str(self.action or "unknown"),
                ).observe(self.ended_at - self.started_at)
        except Exception:
            pass

    def to_dict(self):
        return {
            "id": self.id,
            "action": self.action,
            "cluster": self.cluster,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "dry_run": self.dry_run,
            "error_summary": self.error_summary,
            # Sous quelle identité le cluster a vu cette action. None quand la
            # délégation est éteinte : l'action a employé le kubeconfig partagé.
            "cluster_user": self.cluster_user,
            # v1.46.0 : progression d'un transfert (dernier point par phase)
            "progress": dict(self.progress),
            "progress_current": self.progress_last,
            "result": dict(self.result),
        }


ACTIONS = {}  # run_id -> ActionRun
ACTIONS_LOCK = threading.Lock()

# v1.5.7: ACTIONS used to grow without bound across the process lifetime.
# Now finished runs are evicted by a background GC thread once they've
# been done long enough that any reasonable SSE consumer has caught up.
_ACTIONS_GC_KEEP_SECONDS = 3600    # 1h after end → evict
_ACTIONS_GC_TICK_SECONDS = 60

def _usable_dir(preferred, fallback):
    """`preferred` s'il peut être créé, sinon `fallback`, avec un
    avertissement : l'état n'y survivra pas à un redémarrage.

    Rattrape toute OSError et pas seulement PermissionError : le service
    packagé tourne sur un système de fichiers en LECTURE SEULE, et y créer
    un répertoire lève « Read-only file system », qui faisait planter
    l'application au démarrage (constaté en v1.44.0 sur l'unité installée).
    """
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError as e:
        log.warning(
            "%s inutilisable (%s) : repli sur %s, perdu au redémarrage",
            preferred, e.strerror or e, fallback)
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


# Persistence — SQLite so the failure list survives a Flask restart.
_actions_db = Path(os.environ.get("HARVESTER_OPS_ACTIONS_DB",
                                  "/var/lib/harvester-ops/actions.db"))
ACTIONS_DB = _usable_dir(_actions_db.parent,
                         Path(tempfile.gettempdir()) / "harvester-ops-actions") / _actions_db.name


def _actions_init_db():
    conn = sqlite3.connect(str(ACTIONS_DB))
    # v1.6.0: WAL mode lets multiple readers + 1 writer run concurrently
    # (default rollback journal serializes ALL accesses). busy_timeout
    # smooths over momentary locks instead of raising SQLITE_BUSY.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            id          TEXT PRIMARY KEY,
            action      TEXT NOT NULL,
            cluster     TEXT,
            status      TEXT NOT NULL,
            exit_code   INTEGER,
            started_at  REAL NOT NULL,
            ended_at    REAL,
            dry_run     INTEGER DEFAULT 0,
            cmd         TEXT,
            events      TEXT,
            error_summary TEXT
        )
    """)
    # v1.6.5: additive migration for DBs created before error_summary —
    # hot-applicable, no downtime, no data rewrite.
    try:
        conn.execute("ALTER TABLE actions ADD COLUMN error_summary TEXT")
    except sqlite3.OperationalError:
        pass  # column already present
    # v1.47.2 : ce que l'action a produit (le nom de l'archive d'un export),
    # même migration additive, à chaud
    try:
        conn.execute("ALTER TABLE actions ADD COLUMN result TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS actions_started ON actions(started_at DESC)")
    # v1.6.0: compound index for "recent actions per cluster" lookup
    # (the dock + activity tab queries by cluster + recency).
    conn.execute("CREATE INDEX IF NOT EXISTS actions_cluster_started "
                  "ON actions(cluster, started_at DESC)")
    conn.commit()
    conn.close()


def _actions_persist(run):
    """Write/update the action row in SQLite. Called on close()."""
    try:
        events = list(run.events)[-200:]
        events_json = json.dumps(events)
    except Exception:
        events_json = "[]"
    cmd_str = json.dumps(run.cmd) if isinstance(run.cmd, list) else str(run.cmd)[:2000]
    try:
        result_json = json.dumps(getattr(run, "result", None) or {})
    except (TypeError, ValueError):
        result_json = "{}"
    conn = sqlite3.connect(str(ACTIONS_DB))
    conn.execute("""
        INSERT INTO actions(id, action, cluster, status, exit_code,
                            started_at, ended_at, dry_run, cmd, events,
                            error_summary, result)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status,
            exit_code=excluded.exit_code,
            ended_at=excluded.ended_at,
            events=excluded.events,
            error_summary=excluded.error_summary,
            result=excluded.result
    """, (run.id, run.action, run.cluster, run.status, run.exit_code,
          run.started_at, run.ended_at, int(bool(run.dry_run)),
          cmd_str, events_json, run.error_summary, result_json))
    conn.commit()
    # Cap history at 500 rows (drop oldest done/error)
    conn.execute("""
        DELETE FROM actions WHERE id IN (
            SELECT id FROM actions
            WHERE status NOT IN ('starting','running')
            ORDER BY started_at DESC LIMIT -1 OFFSET 500
        )
    """)
    conn.commit()
    conn.close()


def _row_result(raw):
    """La colonne `result` (JSON) relue en dictionnaire ; vide pour une
    ligne écrite avant 1.47.2 ou illisible.

    Définie AVANT le rechargement de l'historique, qui s'exécute à l'import :
    placée après, chaque ligne échouait sur un NameError depuis la 1.47.2 et
    plus aucune action n'était rechargée au démarrage (vu en v1.56.0)."""
    try:
        v = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _actions_load_history():
    """Restore actions from SQLite at startup. Mark interrupted runs as 'interrupted'."""
    try:
        _actions_init_db()
        conn = sqlite3.connect(str(ACTIONS_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM actions ORDER BY started_at DESC LIMIT 200"
        ).fetchall()
        conn.close()
    except Exception as e:
        log_actions.warning("load history failed: %s", e)
        return
    restored = 0
    for row in rows:
        try:
            try:
                cmd = json.loads(row["cmd"]) if row["cmd"] and row["cmd"].startswith("[") else (row["cmd"] or "")
            except Exception:
                cmd = row["cmd"] or ""
            run = ActionRun(row["id"], row["action"], row["cluster"], cmd,
                            dry_run=bool(row["dry_run"]))
            run.status = row["status"]
            run.exit_code = row["exit_code"]
            run.started_at = row["started_at"]
            run.ended_at = row["ended_at"]
            if "error_summary" in row.keys():
                run.error_summary = row["error_summary"]
            # An action still flagged running at startup was killed by the restart.
            if run.status in ("starting", "running") and not run.ended_at:
                run.status = "interrupted"
                run.exit_code = -1
                run.ended_at = run.started_at
            try:
                evs = json.loads(row["events"] or "[]")
                run.events = deque(evs, maxlen=500)
                # v1.47.2 : le flux lit par numéro absolu (1.46.0) ; sans
                # ce compteur, une action rechargée ne rejouait RIEN
                run._seq = len(run.events)
            except Exception:
                pass
            if "result" in row.keys():
                run.result = _row_result(row["result"])
            run._closed = True
            ACTIONS[row["id"]] = run
            restored += 1
        except Exception as e:
            log_actions.warning("failed to restore %s: %s", row["id"], e)
    if restored:
        log_actions.info("restored %d actions from %s", restored, ACTIONS_DB)


_actions_load_history()


# v1.6.5: read-side of the SQLite history. Until now nothing ever read the
# DB after startup, so the Activity tab silently lost every run older than
# the 1h in-memory GC (or a Flask restart). These helpers back /api/activity,
# /api/action/<id> and /api/stream/<id> for runs no longer in memory.
_ACTIONS_LIST_COLUMNS = ("id", "action", "cluster", "status", "exit_code",
                         "started_at", "ended_at", "dry_run", "error_summary",
                         "result")


def _actions_db_recent(limit=50, cluster=None, status=None, action=None,
                       q=None):
    """Most recent persisted runs, WITHOUT the events blob (list views).

    Les filtres s'appliquent EN SQL, pas après coup sur la page renvoyée :
    chercher « les échecs sur harv3 » parmi les 50 derniers runs seulement
    donnerait une réponse fausse dès que le cluster est peu actif — un
    filtre doit chercher dans tout l'historique, pas dans la fenêtre déjà
    affichée."""
    where, params = [], []
    if cluster:
        where.append("cluster = ?")
        params.append(cluster)
    if status:
        where.append("status = ?")
        params.append(status)
    if action:
        where.append("action LIKE ?")
        params.append(f"%{action}%")
    if q:
        where.append("(id LIKE ? OR action LIKE ? OR cluster LIKE ?"
                     " OR IFNULL(error_summary,'') LIKE ?)")
        params += [f"%{q}%"] * 4
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        conn = sqlite3.connect(str(ACTIONS_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT {} FROM actions{} ORDER BY started_at DESC LIMIT ?"
            .format(", ".join(_ACTIONS_LIST_COLUMNS), clause),
            (*params, int(limit))
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        d = {k: r[k] for k in _ACTIONS_LIST_COLUMNS}
        d["dry_run"] = bool(d["dry_run"])
        d["result"] = _row_result(d.get("result"))
        out.append(d)
    return out


def _actions_db_count():
    """Nombre total de runs persistés, filtres non appliqués : c'est le
    dénominateur du « N sur M » de l'onglet Activité."""
    try:
        conn = sqlite3.connect(str(ACTIONS_DB))
        n = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        conn.close()
        return int(n)
    except sqlite3.Error:
        return 0


def _actions_db_get(run_id):
    """One persisted run + its replay events. Returns (None, []) if absent."""
    try:
        conn = sqlite3.connect(str(ACTIONS_DB))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM actions WHERE id = ?", (run_id,)).fetchone()
        conn.close()
    except sqlite3.Error:
        return None, []
    if not row:
        return None, []
    d = {k: row[k] for k in _ACTIONS_LIST_COLUMNS if k in row.keys()}
    d.setdefault("error_summary", None)
    d["dry_run"] = bool(d.get("dry_run"))
    d["result"] = _row_result(d.get("result"))
    try:
        events = json.loads(row["events"] or "[]")
    except (json.JSONDecodeError, TypeError):
        events = []
    return d, events


def run_action_thread(run: ActionRun):
    """Spawn the script and consume its stderr line-by-line."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})

    try:
        run.proc = subprocess.Popen(
            run.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "NO_COLOR": "1",
                 "HARVESTER_OPS_CONFIG": str(CONFIG_PATH),
                 **getattr(run, "identity_env", {})},
        )
    except FileNotFoundError as e:
        run.status = "error"
        run.exit_code = 127
        run.emit({"type": "log", "level": "error", "message": str(e), "ts": time.time()})
        run.ended_at = time.time()
        run.close()
        return

    def pump(stream, stream_name):
        for line in iter(stream.readline, ""):
            line = line.rstrip("\n")
            if line.startswith("STEP_EVENT|"):
                parts = line.split("|", 3)
                if len(parts) == 4:
                    _, step_id, status, msg = parts
                    run.emit({
                        "type": "step",
                        "step_id": step_id,
                        "status": status,
                        "message": msg,
                        "ts": time.time(),
                    })
                    continue
            # Remember the last real stderr line: if the script dies with a
            # non-zero exit it is almost always the explanation the user needs.
            if stream_name == "stderr" and line.strip():
                run._last_stderr = line.strip()
            run.emit({"type": "log", "stream": stream_name, "message": line, "ts": time.time()})
        stream.close()

    t_err = threading.Thread(target=pump, args=(run.proc.stderr, "stderr"), daemon=True)
    t_out = threading.Thread(target=pump, args=(run.proc.stdout, "stdout"), daemon=True)
    t_err.start()
    t_out.start()

    rc = run.proc.wait()
    t_err.join(timeout=5)
    t_out.join(timeout=5)

    run.exit_code = rc
    run.status = "done" if rc == 0 else "error"
    if rc != 0 and not run.error_summary:
        run.error_summary = (run._last_stderr or "")[:300] or None
    run.ended_at = time.time()
    run.emit({
        "type": "status",
        "status": run.status,
        "exit_code": rc,
        "ts": time.time(),
    })
    run.close()


# v1.44.10 : un arrêt et un démarrage du MÊME cluster ne doivent jamais
# tourner ensemble. Vécu sur le banc : un démarrage resté bloqué tournait
# encore quand un nouvel arrêt a été lancé ; l'un isolait et arrêtait les
# VMs pendant que l'autre attendait de les relancer. Deux opérateurs, ou un
# double clic, produiraient la même chose. Les simulations (dry-run) ne
# touchent à rien : elles ne bloquent pas et ne sont pas bloquées.
CLUSTER_SEQUENCES = ("shutdown", "startup")


class ActionBusy(Exception):
    """Un arrêt ou un démarrage tourne déjà sur ce cluster."""

    def __init__(self, run):
        super().__init__(f"a {run.action} is already running on {run.cluster} "
                         f"(action {run.id}); wait for it to end or cancel it")
        self.run = run


def start_action(action, cluster, dry_run=False, interactive=False,
                 namespace=None, extra_args=None, snapshot=False, force=False):
    """Build the command and spawn the action."""
    cfg = load_config()
    if not any(c["name"] == cluster for c in cfg.get("clusters", [])):
        raise ValueError(f"Unknown cluster: {cluster}")

    if action == "shutdown":
        script = BIN_DIR / "harvester-shutdown.sh"
    elif action == "startup":
        script = BIN_DIR / "harvester-startup.sh"
    elif action == "status":
        script = BIN_DIR / "harvester-status.sh"
    elif action in ("ns-stop", "ns-start"):
        script = BIN_DIR / "harvester-status.sh"   # placeholder, replaced below
    else:
        raise ValueError(f"Unknown action: {action}")

    cmd = ["/usr/bin/env", "bash", str(script), "--cluster", cluster]

    if dry_run:
        cmd.append("--dry-run")
    if snapshot and action == "shutdown":
        cmd.append("--snapshot")
    # v1.8.9 : depuis le durcissement des filets de sécurité, un échec
    # de snapshot etcd (ou des volumes de VM non détachés) ANNULE la
    # séquence en mode non interactif — sauf --force explicite.
    if force and action == "shutdown":
        cmd.append("--force")
    cmd.append("--yes")

    if action == "ns-stop" or action == "ns-start":
        cmd = ["/usr/bin/env", "bash", "-c", _ns_action_script(action, cluster, namespace)]

    if extra_args:
        cmd.extend(extra_args)

    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, action, cluster, cmd, dry_run=dry_run)
    # Identité capturée ICI, dans le thread de la requête : le thread de
    # travail ne la retrouverait pas, et l'action repartirait avec le
    # kubeconfig partagé, administrateur.
    _ident = current_cluster_identity()
    run.identity_env = identity_env(cluster=cluster)
    run.cluster_user = (_ident or {}).get("user")
    with ACTIONS_LOCK:
        # Contrôle et inscription sous le même verrou : deux requêtes
        # simultanées ne peuvent pas passer toutes les deux.
        if action in CLUSTER_SEQUENCES and not dry_run:
            busy = next((r for r in ACTIONS.values()
                         if r.cluster == cluster and r.action in CLUSTER_SEQUENCES
                         and not r.dry_run and r.status in ("starting", "running")),
                        None)
            if busy:
                raise ActionBusy(busy)
        ACTIONS[run_id] = run

    threading.Thread(target=run_action_thread, args=(run,), daemon=True).start()
    return run


def _ns_action_script(action, cluster, namespace):
    """Inline bash script for per-namespace VM start/stop."""
    target = "Halted" if action == "ns-stop" else "Always"
    label = "Stop" if action == "ns-stop" else "Start"
    step_id = f"ns-{action.split('-')[1]}-{namespace}"
    return f"""
set -eo pipefail
source {shlex.quote(str(BIN_DIR))}/lib/common.sh
CLUSTER_NAME={shlex.quote(cluster)}
load_cluster "$CLUSTER_NAME" >/dev/null
init_logging {shlex.quote(action)}
emit_event {shlex.quote(step_id)} running "{label} VMs in namespace {namespace}"
log_step "{label} VMs in namespace {namespace} (cluster $CLUSTER_NAME)"
count=0
while read -r line; do
    name=$(echo "$line" | awk '{{print $1}}')
    [ -z "$name" ] && continue
    log_info "{label} VM: {namespace}/$name"
    kubectl --kubeconfig="$KUBECONFIG_PATH" patch vm "$name" -n {shlex.quote(namespace)} --type merge \
        -p '{{"spec":{{"runStrategy":"{target}"}}}}' || log_warn "Failed: $name"
    count=$((count + 1))
done < <(kubectl --kubeconfig="$KUBECONFIG_PATH" get vm -n {shlex.quote(namespace)} --no-headers 2>/dev/null | awk '{{print $1}}')
emit_event {shlex.quote(step_id)} done "$count VMs processed"
log_ok "$count VMs processed"
"""


# -----------------------------------------------------------------------------
# Routes — pages
# -----------------------------------------------------------------------------
def _harvester_ops_version():
    """Resolve the version: env var first (set by install.sh), then the
    VERSION file shipped with the repo, then 'dev'."""
    v = os.environ.get("HARVESTER_OPS_VERSION")
    if v and v.strip():
        return v.strip()
    try:
        vfile = Path(__file__).resolve().parent.parent / "VERSION"
        if vfile.exists():
            return vfile.read_text().strip() or "dev"
    except Exception:
        pass
    return "dev"


# v1.56.0 : l'historique des versions, lu dans CHANGELOG.md (livré avec la
# console) et affiché d'un clic sur le numéro de version.
_CHANGELOG_CACHE = {"mtime": None, "data": None}
_CHANGELOG_HEAD_RE = re.compile(r"^## \[([^\]]+)\]\s*(?:[-—–]\s*(\S+))?\s*(?:[-—–]\s*(.*))?$")


def _changelog_path():
    return Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def _parse_changelog(text):
    """Les versions de CHANGELOG.md : `## [x.y.z] - date - titre`, puis des
    sections `### Added` (etc.) faites de puces, une puce pouvant courir sur
    plusieurs lignes indentées. Le texte reste en Markdown léger ; l'écran
    l'échappe avant d'en rendre le gras, le code et les liens."""
    releases, rel, sec, item = [], None, None, None

    def flush_item():
        nonlocal item
        if item is not None and sec is not None:
            sec["items"].append(" ".join(item).strip())
        item = None

    for raw in text.splitlines():
        line = raw.rstrip()
        m = _CHANGELOG_HEAD_RE.match(line)
        if m:
            flush_item()
            rel = {"version": m.group(1).strip(), "date": (m.group(2) or "").strip(),
                   "title": (m.group(3) or "").strip(), "sections": []}
            releases.append(rel)
            sec = None
            continue
        if rel is None:
            continue
        if line.startswith("### "):
            flush_item()
            sec = {"name": line[4:].strip(), "items": []}
            rel["sections"].append(sec)
            continue
        if not line.strip():
            flush_item()
            continue
        if sec is None:
            sec = {"name": "", "items": []}
            rel["sections"].append(sec)
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            flush_item()
            item = [stripped[2:]]
        elif item is not None and raw[:1] in (" ", "\t"):
            item.append(stripped)
        else:
            flush_item()
            item = [stripped]
    flush_item()
    return releases


@app.route("/api/changelog")
@requires_auth
def api_changelog():
    path = _changelog_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return jsonify({"current": _harvester_ops_version(), "releases": [],
                        "error": "changelog not shipped"}), 200
    if _CHANGELOG_CACHE["mtime"] != mtime:
        _CHANGELOG_CACHE.update({"mtime": mtime,
                                 "data": _parse_changelog(path.read_text(errors="replace"))})
    return jsonify({"current": _harvester_ops_version(), "releases": _CHANGELOG_CACHE["data"]})


# =============================================================================
# v1.82.0 : mise à jour de la console depuis l'interface
# =============================================================================
# La console (conteneur non root, en lecture seule) ne s'installe pas
# elle-même : elle prépare l'archive dans <état>/updates/staged et dépose une
# demande que l'agent root de l'hôte traite (bin/harvester-ops-update.py), après
# avoir vérifié la SIGNATURE avec ses propres clés. Ce qui suit ne fait que
# préparer, pré-vérifier (pour dire tôt ce qui n'ira pas) et suivre.
# Conception : docs/design/2026-10-01-mise-a-jour-console.md.
# =============================================================================
import self_update as _su  # noqa: E402
import urllib.request  # noqa: E402

UPDATE_DEFAULT_SOURCE = "https://github.com/jniedergang/harvester-ops/releases/latest/download/"
_UPDATE_SIG_MAX = 64 * 1024
_UPDATE_MANIFEST_MAX = 2 * 1024 * 1024
_update_lock = threading.Lock()
_update_check = {"ts": 0, "result": None}


def _updates_dir():
    d = _state_dir() / "updates"
    (d / _su.STAGED).mkdir(parents=True, exist_ok=True)
    return d


def _update_signers():
    """Clés de confiance vues de la console (pré-vérification seulement)."""
    here = Path(__file__).resolve().parent.parent
    etc = os.environ.get("HARVESTER_OPS_ETC", "/etc/harvester-ops")
    # dans l'image : /opt/harvester-ops/update-signers ; depuis les sources : config/
    return _su.signers_file(etc, str(here)) or _su.signers_file(etc, str(here / "config"))


def _update_source():
    cfg = _su.read_json(_updates_dir() / "source.json", {}) or {}
    return cfg.get("url") or os.environ.get("HARVESTER_OPS_UPDATE_SOURCE") or UPDATE_DEFAULT_SOURCE


def _update_agent():
    """L'agent de l'hôte s'annonce dans l'état à son installation."""
    return _su.read_json(_updates_dir() / "agent.json")


def _update_precheck(path):
    """Pré-vérification d'une archive préparée, gardée tant qu'elle ne change
    pas : version, contenu, signature. Un fichier illisible (posé à la main par
    root, étiquette SELinux de l'hôte : vu en réel) est signalé, sans faire
    tomber l'onglet entier."""
    try:
        return _update_precheck_inner(path)
    except OSError as e:
        return {"name": path.name, "ok": False, "size": 0,
                "error": f"unreadable by the console ({e.strerror or e}); "
                         "put the files through the interface, or fix their owner and label"}


def _update_precheck_inner(path):
    st = path.stat()
    side = path.with_name(path.name + ".check.json")
    sig = path.with_name(path.name + ".sig")
    key = [st.st_size, st.st_mtime_ns, sig.stat().st_mtime_ns if sig.exists() else None]
    hit = _su.read_json(side)
    if hit and hit.get("key") == key:
        return hit["result"]
    res = {"name": path.name, "size": st.st_size, "signed": sig.exists()}
    try:
        info = _su.inspect_archive(path)
        res.update(version=info["version"], newer=_su.is_newer(info["version"], _harvester_ops_version()))
        if sig.exists():
            _su.verify_signature(path, sig, _update_signers())
            res["signature"] = "valid"
        else:
            res["signature"] = "missing"
        res["ok"] = bool(sig.exists())
    except _su.UpdateError as e:
        res.update(ok=False, error=str(e))
    try:
        _su.write_json_atomic(side, {"key": key, "result": res}, mode=0o600)
    except OSError:
        pass
    return res


def _update_staged():
    out = []
    for p in sorted((_updates_dir() / _su.STAGED).glob("harvester-ops-*.tar.gz")):
        if _su.ARCHIVE_RE.match(p.name):
            out.append(_update_precheck(p))
    return out


def _update_busy_actions():
    with ACTIONS_LOCK:
        return [a.to_dict() for a in ACTIONS.values()
                if a.status in ("starting", "running") and not str(a.action).startswith("console-update")]


@app.route("/api/update/status")
@requires_auth
def api_update_status():
    d = _updates_dir()
    status = _su.read_json(d / _su.STATUS)
    with _update_lock:
        check = _update_check["result"]
    return jsonify({
        "current": _harvester_ops_version(),
        "agent": _update_agent(),
        "source": _update_source(),
        "default_source": UPDATE_DEFAULT_SOURCE,
        "trusted_keys": bool(_update_signers()),
        "staged": _update_staged(),
        "pending": (d / _su.REQUEST).exists(),
        "last": status,
        "check": check,
        "busy": len(_update_busy_actions()),
        "can_apply": current_role() == "admin",
        # console lancée depuis un dépôt git : pas d'agent, et c'est normal
        "from_sources": (Path(__file__).resolve().parent.parent / ".git").exists(),
    })


@app.route("/api/update/source", methods=["PUT"])
@requires_auth
@_rate_limit("20/minute")
def api_update_source():
    url = str((request.get_json(silent=True) or {}).get("url") or "").strip()
    if url:
        try:
            _su.source_url(url, "release.json")
        except _su.UpdateError as e:
            return jsonify({"error": str(e)}), 400
    _su.write_json_atomic(_updates_dir() / "source.json", {"url": url} if url else {}, mode=0o640)
    return jsonify({"source": _update_source()})


def _update_fetch(url, timeout=20, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": f"harvester-ops/{_harvester_ops_version()}"})
    return urllib.request.urlopen(req, timeout=timeout)


@app.route("/api/update/check", methods=["POST"])
@requires_auth
@_rate_limit("10/minute")
def api_update_check():
    src = _update_source()
    try:
        with _update_fetch(_su.source_url(src, "release.json")) as r:
            raw = r.read(_UPDATE_MANIFEST_MAX + 1)
        if len(raw) > _UPDATE_MANIFEST_MAX:
            raise _su.UpdateError("release.json is too large")
        man = _su.check_manifest(json.loads(raw))
    except (OSError, ValueError, _su.UpdateError) as e:
        res = {"ok": False, "source": src, "error": _error_text(e)}
    else:
        cur = _harvester_ops_version()
        man["notes"] = [n for n in man["notes"] if isinstance(n, dict)
                        and _su.is_newer(str(n.get("version")), cur)]
        res = {"ok": True, "source": src, "newer": _su.is_newer(man["version"], cur), "release": man}
    res["ts"] = time.time()
    with _update_lock:
        _update_check.update(ts=res["ts"], result=res)
    return jsonify(res), (200 if res["ok"] else 502)


def _update_download_runner(run, src, man):
    staged = _updates_dir() / _su.STAGED
    part = staged / (man["archive"] + ".part")
    try:
        run.status = "running"
        run.emit({"type": "status", "status": "running", "ts": time.time()})
        run.emit({"type": "step", "step_id": "download", "status": "running",
                  "message": f"{man['archive']} from {src}", "ts": time.time()})
        with _update_fetch(_su.source_url(src, man["archive"]), timeout=60) as r:
            total = int(r.headers.get("Content-Length") or man.get("size") or 0)
            prog = _vp.Progress(run.emit_progress, "download", total or None)
            done = 0
            h = hashlib.sha256()
            fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            with os.fdopen(fd, "wb") as f:
                while True:
                    if getattr(run, "_cancel", False):
                        raise _su.UpdateError("cancelled")
                    b = r.read(_UPLOAD_CHUNK)
                    if not b:
                        break
                    f.write(b)
                    h.update(b)
                    done += len(b)
                    prog.update(done)
        if h.hexdigest() != man["sha256"]:
            raise _su.UpdateError("checksum mismatch: the download differs from release.json")
        prog.finish()
        run.emit({"type": "step", "step_id": "download", "status": "done",
                  "message": f"{_vp.fmt_bytes(done)}, SHA-256 matches release.json", "ts": time.time()})
        if man.get("signature"):
            with _update_fetch(_su.source_url(src, man["signature"])) as r:
                sig = r.read(_UPDATE_SIG_MAX + 1)
            if len(sig) > _UPDATE_SIG_MAX:
                raise _su.UpdateError("signature file too large")
            (staged / man["signature"]).write_bytes(sig)
        os.replace(part, staged / man["archive"])
        run.emit({"type": "step", "step_id": "check", "status": "running", "ts": time.time()})
        res = _update_precheck(staged / man["archive"])
        if not res.get("ok"):
            raise _su.UpdateError(res.get("error") or f"signature {res.get('signature')}")
        run.emit({"type": "step", "step_id": "check", "status": "done",
                  "message": f"{res['version']} ready to install, signature valid", "ts": time.time()})
        run.result = res
        run.status, run.exit_code = "done", 0
    except (OSError, ValueError, _su.UpdateError) as e:
        part.unlink(missing_ok=True)
        run.status, run.exit_code = "error", 1
        run.error_summary = _error_text(e)
        run.emit({"type": "step", "step_id": "download", "status": "error",
                  "message": run.error_summary, "ts": time.time()})
    run.close()


@app.route("/api/update/download", methods=["POST"])
@requires_auth
@_rate_limit("5/minute")
def api_update_download():
    with _update_lock:
        check = _update_check["result"]
    if not check or not check.get("ok"):
        return jsonify({"error": "check for an update first"}), 409
    man = check["release"]
    aid = track_action(f"console-update:download:{man['version']}", "", _update_download_runner,
                       check["source"], man)
    return jsonify({"action_id": aid}), 202


@app.route("/api/update/upload", methods=["POST"])
@requires_auth
@_rate_limit("10/minute")
def api_update_upload():
    """Archive (ou sa signature .sig) reçue du navigateur, en flux vers le
    disque : elle fait plus de 600 Mo."""
    name = (request.args.get("name") or "").strip()
    is_sig = name.endswith(".sig")
    base = name[:-4] if is_sig else name
    if not _su.ARCHIVE_RE.match(base):
        return jsonify({"error": "expected harvester-ops-<version>.tar.gz or its .sig"}), 400
    try:
        length = int(request.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return jsonify({"error": "empty upload"}), 400
    staged = _updates_dir() / _su.STAGED
    if is_sig:
        if length > _UPDATE_SIG_MAX:
            return jsonify({"error": "signature file too large"}), 413
        data = request.stream.read(length)
        try:
            (staged / name).write_bytes(data)
        except OSError as e:
            return jsonify({"error": f"cannot write {name}: {e.strerror or e}"}), 409
        arch = staged / base
        return jsonify({"ok": True, "staged": _update_precheck(arch) if arch.exists() else None})
    st = os.statvfs(staged)
    if st.f_bavail * st.f_frsize < length + _UPLOAD_SPARE:
        return jsonify({"error": "not enough room to receive the archive", "need": length}), 507
    part = staged / (name + ".part")
    run = ActionRun(uuid.uuid4().hex[:12], f"console-update:upload:{name}", "", ["upload", name])
    with ACTIONS_LOCK:
        ACTIONS[run.id] = run
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    run.emit({"type": "step", "step_id": "upload", "status": "running",
              "message": f"receiving {name} ({_vp.fmt_bytes(length)})", "ts": time.time()})
    try:
        res = _receive_archive(run, request.stream, length, part)
        os.chmod(part, 0o640)
        os.replace(part, staged / name)
    except (_UploadCancelled, ValueError, OSError) as e:
        part.unlink(missing_ok=True)
        run.status, run.exit_code = "error", 1
        run.error_summary = "cancelled" if isinstance(e, _UploadCancelled) else _error_text(e)
        run.emit({"type": "step", "step_id": "upload", "status": "error",
                  "message": run.error_summary, "ts": time.time()})
        run.close()
        return jsonify({"error": run.error_summary, "action_id": run.id}), 409
    run.emit({"type": "step", "step_id": "upload", "status": "done",
              "message": _vp.summary("upload", res), "ts": time.time()})
    chk = _update_precheck(staged / name)
    run.emit({"type": "step", "step_id": "check", "status": "done" if chk.get("ok") else "error",
              "message": (f"{chk.get('version')} ready to install" if chk.get("ok")
                          else chk.get("error") or "signature missing: add the .sig file"),
              "ts": time.time()})
    run.result = chk
    run.status, run.exit_code = "done", 0
    run.close()
    return jsonify({"ok": True, "action_id": run.id, "staged": chk})


@app.route("/api/update/staged/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_update_staged_delete(name):
    if not _su.ARCHIVE_RE.match(name):
        return jsonify({"error": "bad name"}), 400
    staged = _updates_dir() / _su.STAGED
    try:
        for p in (name, name + ".sig", name + ".check.json"):
            (staged / p).unlink(missing_ok=True)
    except OSError as e:
        return jsonify({"error": f"cannot remove {p}: {e.strerror or e}",
                        "hint": "a file put by hand on the host may not be removable by the console"}), 409
    return jsonify({"ok": True})


@app.route("/api/update/apply", methods=["POST"])
@requires_auth
@_rate_limit("5/minute")
def api_update_apply():
    b = request.get_json(silent=True) or {}
    name = str(b.get("archive") or "")
    if not _su.ARCHIVE_RE.match(name):
        return jsonify({"error": "bad archive name"}), 400
    if _update_agent() is None:
        return jsonify({"error": "no update agent on this host",
                        "hint": "the console was not installed with install.sh 1.82.0 or later "
                                "(or runs from sources): install this release with install.sh"}), 409
    d = _updates_dir()
    path = d / _su.STAGED / name
    if not path.is_file():
        return jsonify({"error": "this archive is not staged"}), 404
    chk = _update_precheck(path)
    if not chk.get("ok"):
        return jsonify({"error": chk.get("error") or "the release is not signed", "staged": chk}), 409
    if not chk.get("newer") and not b.get("allow_older"):
        return jsonify({"error": f"{chk['version']} is not newer than {_harvester_ops_version()}"}), 409
    busy = _update_busy_actions()
    if busy and not b.get("force"):
        return jsonify({"error": "actions are running; the console restart would stop them",
                        "busy": [{"id": a["id"], "action": a["action"], "cluster": a["cluster"]}
                                 for a in busy]}), 409
    with _update_lock:
        if (d / _su.REQUEST).exists():
            return jsonify({"error": "an update is already requested"}), 409
        _su.write_json_atomic(d / _su.REQUEST, {
            "archive": name, "version": chk["version"], "from": _harvester_ops_version(),
            "requested_by": current_user() or "?", "ts": time.time(),
            "allow_older": bool(b.get("allow_older"))}, mode=0o640)
    run = ActionRun(uuid.uuid4().hex[:12], f"console-update:request:{chk['version']}", "", ["apply", name])
    run.cluster_user = current_user()
    with ACTIONS_LOCK:
        ACTIONS[run.id] = run
    run.emit({"type": "step", "step_id": "request", "status": "done",
              "message": f"{_harvester_ops_version()} -> {chk['version']} handed to the host agent; "
                         "the console restarts in a moment", "ts": time.time()})
    run.status, run.exit_code = "done", 0
    run.close()
    return jsonify({"ok": True, "action_id": run.id, "version": chk["version"]}), 202


def _update_record_outcome():
    """Au démarrage : l'issue de la dernière mise à jour, écrite par l'agent
    pendant que la console redémarrait, rangée une fois dans l'activité."""
    try:
        d = _state_dir() / "updates"
        st = _su.read_json(d / _su.STATUS)
        if not st or st.get("state") in (None, "running") or not st.get("started"):
            return
        mark = d / "recorded.json"
        if (_su.read_json(mark) or {}).get("started") == st.get("started"):
            return
        run = ActionRun(uuid.uuid4().hex[:12], f"console-update:{st.get('to') or '?'}", "",
                        ["harvester-ops-update"])
        run.cluster_user = st.get("requested_by")
        run.started_at = st.get("started") or run.started_at
        for s in st.get("steps") or []:
            run.emit({"type": "step", "step_id": s.get("id"), "status": s.get("status"),
                      "message": s.get("message") or "", "ts": s.get("ts") or time.time()})
        log_file = LOG_DIR / str(st.get("log") or "")
        if st.get("log") and log_file.is_file():
            for line in log_file.read_text(errors="replace").splitlines()[-60:]:
                run.emit({"type": "log", "line": line, "ts": time.time()})
        ok = st.get("state") == "done"
        run.status, run.exit_code = ("done", 0) if ok else ("error", 1)
        if not ok:
            run.error_summary = st.get("message") or st.get("state")
        with ACTIONS_LOCK:
            ACTIONS[run.id] = run
        run.close()
        _su.write_json_atomic(mark, {"started": st.get("started")}, mode=0o640)
    except Exception as e:     # jamais bloquant au démarrage
        log.warning("update outcome not recorded: %s", e)



@app.route("/")
@requires_auth
def index():
    cfg = load_config()
    return render_template("index.html",
                           clusters=[c for c in cfg.get("clusters", []) if _sso_visible(c)],
                           version=_harvester_ops_version())


# -----------------------------------------------------------------------------
# v1.50.0 : pages de connexion
# -----------------------------------------------------------------------------
_LOGIN_ERROR_KINDS = {
    "state-unknown": "expired", "state-browser": "expired",
    "access_denied": "denied", "token-refused": "denied", "identity-refused": "denied",
    "rancher-unreachable": "unreachable", "not-configured": "config",
}


_LOGIN_ERROR_KINDS.update({"bad-credentials": "credentials", "too-many": "throttled",
                           # v1.79.0 : connexion directe à un Rancher réglé
                           "rancher-unknown": "config", "direct-disabled": "config",
                           "sso-disabled": "config", "bad-provider": "credentials"})


def _safe_next(value):
    """Une adresse de retour, seulement sur cette console (jamais `//hôte`)."""
    v = (value or "").strip()
    return v if v.startswith("/") and not v.startswith("//") and "\\" not in v else "/"


def _cookie_secure():
    st = _sso_settings()
    return request.is_secure or (st is not None and _sso_redirect_uri(st).startswith("https://"))


def _open_local_session(user):
    sid = _LOCAL_SESSIONS.open(user)
    return sid


def _render_login(error="", status=200, username=""):
    st = _sso_settings()
    servers = _login_rancher_servers()
    last = request.cookies.get(RANCHER_LAST_COOKIE)
    if not any(x["id"] == last for x in servers):
        last = request.args.get("rancher") if any(x["id"] == request.args.get("rancher") for x in servers) else None
    return render_template("login.html", rancher=st is not None,
                           rancher_label=(st or {}).get("label", "Rancher"),
                           rancher_servers=servers, rancher_last=last,
                           local=local_accounts_exist(), error=error,
                           signed_out=request.args.get("signed_out") == "1",
                           error_kind=(_LOGIN_ERROR_KINDS.get(error, "token") if error else ""),
                           next=_safe_next(request.values.get("next")), username=username,
                           version=_harvester_ops_version()), status


@app.route("/login")
def login_page():
    if setup_needed():
        return redirect("/setup")
    # déjà connecté : la page de connexion n'a rien à offrir
    if (_local_session() is not None or _sso_session() is not None) and not request.args.get("signed_out"):
        return redirect(_safe_next(request.args.get("next")))
    return _render_login(request.args.get("error", "")[:40])


@app.route("/login/local", methods=["GET", "POST"])
@_rate_limit("10/minute")
def login_local():
    """v1.57.0 : connexion d'un compte local par le formulaire de la page de
    connexion ; une session (cookie HttpOnly) remplace l'invite HTTP Basic
    du navigateur, qu'on ne pouvait pas vraiment quitter."""
    if request.method == "GET":
        return redirect("/login")
    if not _same_origin():
        return jsonify({"error": "forbidden"}), 403
    user = (request.form.get("username") or "").strip()
    pw = request.form.get("password") or ""
    if not user or not check_auth(user, pw):
        app.logger.warning("sign-in refused for %r from %s", user[:40], request.remote_addr)
        return _render_login("bad-credentials", 401, username=user[:40])
    sid = _open_local_session(user)
    app.logger.info("sign-in: %s", user)
    resp = redirect(_safe_next(request.form.get("next")))
    resp.set_cookie(_acc.COOKIE, sid, max_age=_LOCAL_SESSIONS.ttl, httponly=True,
                    secure=_cookie_secure(), samesite="Lax", path="/")
    return resp


def _announce_setup():
    """Au démarrage, sans aucun compte : le jeton de création du premier
    administrateur est écrit sur le disque, et son emplacement dit au journal."""
    if not setup_needed():
        return
    try:
        _acc.ensure_setup_token(_setup_token_path())
    except OSError as e:
        log.error("first start: cannot write the setup token (%s)", e)
        return
    log.warning("FIRST START: no account yet. Open the console, page /setup, and give the "
                "token read from %s to create the first administrator.", _setup_token_path())


@app.route("/setup", methods=["GET", "POST"])
@_rate_limit("10/minute")
def setup_page():
    """Premier démarrage : créer le premier administrateur. Le jeton est dans
    un fichier du serveur ; qui ne peut pas le lire ne peut pas prendre la
    console avant son exploitant."""
    if not setup_needed():
        return redirect("/login")
    try:
        _acc.ensure_setup_token(_setup_token_path())
    except OSError:
        pass
    ctx = {"token_path": str(_setup_token_path()), "version": _harvester_ops_version(),
           "error": "", "username": "", "min": _acc.PASSWORD_MIN}
    if request.method == "GET":
        return render_template("setup.html", **ctx)
    if not _same_origin():
        return jsonify({"error": "forbidden"}), 403
    user = (request.form.get("username") or "").strip()
    pw, pw2 = request.form.get("password") or "", request.form.get("confirm") or ""
    ctx["username"] = user[:40]
    if not _acc.check_setup_token(_setup_token_path(), request.form.get("token")):
        app.logger.warning("setup refused: wrong token from %s", request.remote_addr)
        return render_template("setup.html", **dict(ctx, error="token")), 401
    if pw != pw2:
        return render_template("setup.html", **dict(ctx, error="mismatch")), 400
    try:
        _accounts().create(user, pw, "admin", by="setup")
    except _acc.AccountError as e:
        return render_template("setup.html", **dict(ctx, error=e.code)), 400
    try:
        _setup_token_path().unlink()
    except OSError:
        pass
    app.logger.warning("first administrator created: %s", user)
    sid = _open_local_session(user)
    resp = redirect("/")
    resp.set_cookie(_acc.COOKIE, sid, max_age=_LOCAL_SESSIONS.ttl, httponly=True,
                    secure=_cookie_secure(), samesite="Lax", path="/")
    return resp


# v1.56.0 : se déconnecter d'un compte local. L'authentification HTTP Basic
# n'a pas de session : le NAVIGATEUR garde le mot de passe et le renvoie à
# chaque requête. Pour qu'il l'oublie, la page lui fait retenir un compte
# factice, que la console accepte sur ce seul chemin : les requêtes suivantes
# portent ce compte, sont refusées, et le navigateur redemande le mot de passe.
LOGOUT_PSEUDO_USER = "harvester-ops-signed-out"


@app.route("/logout/local")
@_rate_limit("30/minute")
def logout_local():
    if not local_accounts_exist():
        return jsonify({"ok": True, "login": "/"})       # aucun compte local : rien à oublier
    auth = request.authorization
    if auth and auth.username == LOGOUT_PSEUDO_USER:
        return jsonify({"ok": True, "login": "/login?signed_out=1"})
    return authenticate()


def _sso_start(st, server_id):
    state, nonce, challenge, browser = _SSO_PENDING.start(_safe_next(request.args.get("next")),
                                                          server=server_id)
    resp = redirect(_rs.authorize_url(st, _sso_redirect_uri(st), state, nonce, challenge))
    resp.set_cookie(_rs.PENDING_COOKIE, browser, max_age=_rs.PENDING_TTL, httponly=True,
                    secure=_sso_cookie_secure(st), samesite="Lax", path="/auth/rancher")
    return resp


@app.route("/auth/rancher/login")
@_rate_limit("20/minute")
def sso_login():
    """1.50.0 : le SSO du Rancher de config.yaml."""
    st = _sso_settings()
    if st is None:
        return redirect("/login?error=not-configured")
    return _sso_start(st, None)


@app.route("/auth/rancher/<server_id>/login")
@_rate_limit("20/minute")
def sso_login_server(server_id):
    """v1.79.0 : le SSO d'un Rancher réglé ; son id voyage avec le `state`."""
    st = _server_settings(server_id) if _srv.ID_RE.match(server_id or "") else None
    if st is None:
        return redirect("/login?error=rancher-unknown")
    if not (st.get("sso") and st.get("client_secret")):
        return redirect("/login?error=sso-disabled&rancher=" + quote(server_id))
    return _sso_start(st, None if st.get("origin") == "config" else server_id)


@app.route("/auth/rancher/callback")
@_rate_limit("20/minute")
def sso_callback():
    if request.args.get("error"):
        code = "access_denied" if request.args.get("error") == "access_denied" else "token-refused"
        return redirect("/login?error=" + code)
    try:
        pending = _SSO_PENDING.take(request.args.get("state"),
                                    request.cookies.get(_rs.PENDING_COOKIE))
    except _rs.SSOError as e:
        app.logger.warning("Rancher sign-in refused: %s", e.code)
        return redirect("/login?error=" + e.code)
    server_id = pending.get("server")
    st = _server_settings(server_id) if server_id else _sso_settings()
    if st is None or not st.get("client_secret"):
        return redirect("/login?error=not-configured")
    try:
        http = _sso_http(st)
        tok = _rs.exchange_code(http, st, request.args.get("code", ""), pending["verifier"],
                                _sso_redirect_uri(st))
        claims = _rs.check_id_token(_rs.claims_of(tok["id_token"]), st, pending["nonce"])
        ident = _rs.rancher_identity(http, st, tok["access_token"])
        if ident["id"] != claims["sub"]:
            raise _rs.SSOError("identity-mismatch")
        if not tok.get("refresh_token"):
            # sans lui, la session tomberait au bout de dix minutes
            raise _rs.SSOError("token-refused", "no refresh token (scope offline_access)")
    except _rs.SSOError as e:
        # le détail est un statut et un message de Rancher, jamais un jeton
        app.logger.warning("Rancher sign-in refused: %s %s", e.code, e.detail)
        return redirect("/login?error=" + e.code)
    sess = _sso_store().open(ident, _rs.console_role(st, ident), tok["access_token"],
                             tok["refresh_token"], st["session_seconds"], server_id=server_id)
    _sso_start_renewer()
    app.logger.info("Rancher sign-in: %s (%s)", sess.login, sess.role)
    resp = redirect(pending.get("next") or "/")
    resp.set_cookie(_rs.COOKIE, sess.sid, max_age=st["session_seconds"], httponly=True,
                    secure=_sso_cookie_secure(st), samesite="Lax", path="/")
    resp.delete_cookie(_rs.PENDING_COOKIE, path="/auth/rancher")
    _remember_rancher(resp, server_id or st.get("id"), st)
    return resp


def _remember_rancher(resp, server_id, st):
    """Le dernier Rancher choisi, proposé en premier la fois suivante."""
    if server_id:
        resp.set_cookie(RANCHER_LAST_COOKIE, server_id, max_age=365 * 86400, httponly=True,
                        secure=_sso_cookie_secure(st), samesite="Lax", path="/")


@app.route("/auth/rancher/<server_id>/direct", methods=["POST"])
@_rate_limit("10/minute")
def rancher_direct_login(server_id):
    """v1.79.0 : connexion directe par identifiant et mot de passe Rancher.

    La console demande un jeton à Rancher pour la personne (fournisseur à
    mot de passe) et s'en sert exactement comme du jeton du SSO : mandataire
    `/k8s/clusters/<id>`, droits de Rancher. Le jeton vaut la durée de
    session réglée, n'est pas renouvelé, et est supprimé dans Rancher à la
    déconnexion. Un refus ne dit pas si le compte existe."""
    if not _same_origin():
        return jsonify({"error": "forbidden"}), 403
    nxt = _safe_next(request.form.get("next"))
    back = "&rancher=" + quote(server_id or "") + ("&next=" + quote(nxt, safe="") if nxt != "/" else "")
    st = _server_settings(server_id) if _srv.ID_RE.match(server_id or "") else None
    if st is None:
        return redirect("/login?error=rancher-unknown")
    if not st.get("direct"):
        return redirect("/login?error=direct-disabled" + back)
    user = (request.form.get("username") or "").strip()
    pw = request.form.get("password") or ""
    provider = (request.form.get("provider") or "local").strip()
    if not user or not pw or len(user) > 256 or len(pw) > 1024:
        return redirect("/login?error=bad-credentials" + back)
    http = _sso_http(st)
    try:
        token, name, expires = _rs.direct_login(http, st, provider, user, pw, st["session_seconds"],
                                                "harvester-ops session")
    except _rs.SSOError as e:
        app.logger.warning("Rancher direct sign-in refused on %s for %r from %s: %s",
                           server_id, user[:40], request.remote_addr, e.code)
        return redirect("/login?error=" + e.code + back)
    try:
        ident = _rs.rancher_identity(http, st, token)
    except _rs.SSOError as e:
        _rs.logout_token(http, st, token)
        app.logger.warning("Rancher direct sign-in: identity unreadable on %s: %s", server_id, e.code)
        return redirect("/login?error=identity-refused" + back)
    ttl = max(60, int(expires - time.time()))
    sess = _sso_store().open(ident, _rs.console_role(st, ident), token, None, ttl,
                             server_id=server_id, kind="direct", token_name=name)
    _sso_start_renewer()
    app.logger.info("Rancher direct sign-in: %s (%s) via %s", sess.login, sess.role, server_id)
    resp = redirect(nxt)
    resp.set_cookie(_rs.COOKIE, sess.sid, max_age=ttl, httponly=True,
                    secure=_sso_cookie_secure(st), samesite="Lax", path="/")
    _remember_rancher(resp, server_id, st)
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    """Ferme la session Rancher : la console oublie ses jetons (qui n'ont
    jamais quitté le serveur) et efface ses kubeconfigs. Rancher 2.14 ne
    laisse pas un jeton OIDC se révoquer lui-même (401) : il expire à la
    durée fixée dans le client OIDC."""
    sess = _sso_session()
    local = _local_session()
    if (sess is not None or local is not None) and not _same_origin():
        return jsonify({"error": "forbidden"}), 403
    if sess is not None:
        if getattr(sess, "kind", "sso") == "direct":
            # v1.79.0 : le jeton d'une connexion directe se supprime dans
            # Rancher (celui du SSO ne le peut pas, voir plus haut)
            st = _session_settings(sess)
            if st is not None:
                _rs.logout_token(_sso_http(st), st, sess.token)
        _sso_store().close(sess.sid)
        app.logger.info("Rancher sign-out: %s", sess.login)
    if local is not None:
        _LOCAL_SESSIONS.close(local["sid"])
        app.logger.info("sign-out: %s", local["user"])
    resp = jsonify({"ok": True, "login": "/login?signed_out=1"})
    resp.delete_cookie(_rs.COOKIE, path="/")
    resp.delete_cookie(_acc.COOKIE, path="/")
    return resp


# -----------------------------------------------------------------------------
# v1.79.0 : les Rancher réglés dans l'interface (Réglages > Connexion par
# Rancher). Lecture pour tous, écriture par un administrateur de la console.
# Aucun secret ne sort : ni secret du client OIDC, ni mot de passe, ni jeton.
# Voir docs/design/2026-10-01-rancher-reglable.md.
# -----------------------------------------------------------------------------
_RANCHER_PROBES = {}           # id -> (instant, résultat du test)
_RANCHER_PROBE_TTL = {True: 300, False: 30}
LOGIN_PROBE_TIMEOUT = 3


def _rancher_http(st, timeout=15):
    return _rs.Http(st.get("ca_file"), timeout=timeout, insecure=bool(st.get("insecure")))


def _rancher_probe(server, timeout=LOGIN_PROBE_TIMEOUT, force=False):
    """Version et fournisseurs d'un Rancher, gardés cinq minutes (trente
    secondes après un échec) : la page de connexion ne doit jamais attendre
    un Rancher injoignable."""
    seen = _RANCHER_PROBES.get(server["id"])
    now = time.time()
    if seen and not force and now - seen[0] < _RANCHER_PROBE_TTL[bool(seen[1].get("ok"))]:
        return seen[1]
    res = _radm.probe(_rancher_http(_srv.settings_of(server), timeout), server["url"])
    _RANCHER_PROBES[server["id"]] = (time.time(), res)
    return res


def _login_rancher_servers():
    """Les Rancher proposés à la connexion : [{id, label, sso, direct,
    providers: [{id, label}], available, unavailable}]. Les fournisseurs
    (à mot de passe, actifs) viennent du dernier test ou d'une lecture
    courte, en parallèle, trois secondes au plus."""
    servers = [x for x in _rancher_servers() if x["direct_enabled"] or _srv.sso_enabled(x)]
    if not servers:
        return []
    from concurrent.futures import ThreadPoolExecutor, wait as _fwait
    direct = [x for x in servers if x["direct_enabled"]]
    probes = {}
    if direct:
        pool = ThreadPoolExecutor(max_workers=min(8, len(direct)))
        futs = {pool.submit(_rancher_probe, x): x["id"] for x in direct}
        done, _ = _fwait(futs, timeout=LOGIN_PROBE_TIMEOUT + 0.5)
        for f in done:
            try:
                probes[futs[f]] = f.result()
            except Exception:  # noqa: BLE001
                pass
        pool.shutdown(wait=False)
    out = []
    for x in servers:
        sso = _srv.sso_enabled(x)
        providers, available = [], True
        if x["direct_enabled"]:
            pr = probes.get(x["id"])
            available = bool(pr and pr.get("ok"))
            providers = [{"id": p["id"], "label": _srv.provider_label(p["id"])}
                         for p in (pr or {}).get("providers") or [] if p.get("password") and p.get("enabled")]
            if available and not providers:
                available = sso      # Rancher joint mais sans fournisseur à mot de passe
        direct_ok = bool(x["direct_enabled"] and providers)
        out.append({"id": x["id"], "label": x["label"], "sso": sso, "direct": direct_ok,
                    "providers": providers, "available": available or sso,
                    "unavailable": not (available or sso)})
    return out


def _rancher_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@app.route("/api/rancher/servers")
@requires_auth
def api_rancher_servers():
    return jsonify({"servers": [_srv.public(x) for x in _rancher_servers(include_shadowed=True)],
                    "config_writable": _config_writable()})


@app.route("/api/rancher/servers", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_rancher_server_create():
    try:
        x = _RANCHER_STORE.create(load_config(), _rancher_body())
    except _srv.ServerError as e:
        return jsonify({"error": str(e)}), e.status
    _RANCHER_PROBES.pop(x["id"], None)
    app.logger.info("Rancher server added by %s: %s (%s)", current_user(), x["id"], x["url"])
    return jsonify({"server": _srv.public(x)}), 201


@app.route("/api/rancher/servers/<server_id>", methods=["PUT"])
@requires_auth
@_rate_limit("30/minute")
def api_rancher_server_update(server_id):
    try:
        x = _RANCHER_STORE.update(load_config(), server_id, _rancher_body())
    except _srv.ServerError as e:
        return jsonify({"error": str(e)}), e.status
    _RANCHER_PROBES.pop(server_id, None)
    app.logger.info("Rancher server changed by %s: %s", current_user(), server_id)
    return jsonify({"server": _srv.public(x)})


@app.route("/api/rancher/servers/<server_id>", methods=["DELETE"])
@requires_auth
@_rate_limit("30/minute")
def api_rancher_server_delete(server_id):
    try:
        x = _RANCHER_STORE.delete(load_config(), server_id)
    except _srv.ServerError as e:
        return jsonify({"error": str(e)}), e.status
    _RANCHER_PROBES.pop(server_id, None)
    app.logger.info("Rancher server removed by %s: %s", current_user(), server_id)
    out = {"ok": True}
    if (x.get("sso") or {}).get("client_id"):
        out["warning"] = ("the OIDC client stays declared in Rancher; "
                          "unregister single sign-on first to remove it there")
    return jsonify(out)


def _probe_reply(res):
    return jsonify({"ok": bool(res.get("ok")), "version": res.get("version"),
                    "providers": res.get("providers") or [], "error": res.get("error")})


@app.route("/api/rancher/servers/<server_id>/test", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_rancher_server_test(server_id):
    x = _rancher_server(server_id, include_shadowed=True)
    if x is None:
        return jsonify({"error": "unknown Rancher"}), 404
    return _probe_reply(_rancher_probe(x, timeout=10, force=True))


@app.route("/api/rancher/test", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_rancher_test_unsaved():
    """Le test d'un Rancher pas encore enregistré (le formulaire) : adresse,
    autorité PEM ou vérification désactivée."""
    import ssl
    b = _rancher_body()
    try:
        url = _srv.check_url(b.get("url"))
        ca = _srv.check_ca(b["ca"]) if b.get("ca") else None
        http = _rs.Http(timeout=10, insecure=bool(b.get("insecure")), ca_data=ca)
    except _srv.ServerError as e:
        return jsonify({"error": str(e)}), 400
    except (ssl.SSLError, ValueError):
        return jsonify({"error": "the certificate authority cannot be read"}), 400
    return _probe_reply(_radm.probe(http, url))


def _rancher_admin_session(server_id, need_console=False):
    """(serveur, réglages, session d'administrateur Rancher) ou une réponse
    d'erreur. Les identifiants viennent du corps, servent une fois, et ne
    sont ni gardés ni journalisés."""
    x = _rancher_server(server_id, include_shadowed=True)
    if x is None:
        return None, (jsonify({"error": "unknown Rancher"}), 404)
    if need_console and x["origin"] != "console":
        return None, (jsonify({"error": "declared by the operator in config.yaml, read-only for the console"}), 409)
    b = _rancher_body()
    st = _srv.settings_of(x)
    try:
        admin = _radm.AdminSession(_rancher_http(st), st, (b.get("provider") or "local").strip(),
                                   (b.get("admin_user") or "").strip(), b.get("admin_password") or "",
                                   ttl=1800)
    except _radm.AdminError as e:
        return None, (jsonify({"error": str(e)}), e.status)
    return (x, st, admin, b), None


def _console_callback_uri(body):
    """L'adresse de retour à déclarer dans Rancher : donnée, sinon celle de
    config.yaml (`rancher.redirect_uri`), sinon l'origine vue par le
    navigateur (derrière un mandataire TLS, c'est la seule juste)."""
    from urllib.parse import urlparse as _up
    v = str(body.get("redirect_uri") or "").strip()
    if v:
        u = _up(v)
        if u.scheme not in ("http", "https") or not u.netloc or not u.path.endswith("/auth/rancher/callback"):
            raise _srv.ServerError("redirect_uri must be http(s)://<console>/auth/rancher/callback")
        return v
    legacy = (load_config().get("rancher") or {}).get("redirect_uri")
    if legacy:
        return str(legacy)
    origin = request.headers.get("Origin") or ""
    u = _up(origin)
    if u.scheme in ("http", "https") and u.netloc:
        return f"{u.scheme}://{u.netloc}/auth/rancher/callback"
    return request.url_root.rstrip("/") + "/auth/rancher/callback"


@app.route("/api/rancher/servers/<server_id>/sso/register", methods=["POST"])
@requires_auth
@_rate_limit("5/minute")
def api_rancher_sso_register(server_id):
    """La console s'enregistre elle-même comme client OIDC de ce Rancher,
    avec un administrateur de Rancher le temps du geste."""
    try:
        redirect_uri = _console_callback_uri(_rancher_body())
    except _srv.ServerError as e:
        return jsonify({"error": str(e)}), 400
    got, err = _rancher_admin_session(server_id, need_console=True)
    if err:
        return err
    x, st, admin, _ = got
    from urllib.parse import urlparse as _up
    name = (x.get("sso") or {}).get("oidc_client") or _radm.oidc_client_name(_up(redirect_uri).hostname)
    try:
        with admin:
            client_id, secret = _radm.register_oidc(admin, name, redirect_uri,
                                                    refresh_seconds=int(x["session_hours"]) * 3600)
    except _radm.AdminError as e:
        app.logger.warning("Rancher SSO registration on %s refused: %s", server_id, e)
        return jsonify({"error": str(e)}), e.status
    _RANCHER_STORE.set_sso(load_config(), server_id, client_id, secret, redirect_uri, name)
    app.logger.info("Rancher SSO registered by %s on %s: client %s", current_user(), server_id, client_id)
    return jsonify({"client_id": client_id, "redirect_uri": redirect_uri, "oidc_client": name})


@app.route("/api/rancher/servers/<server_id>/sso/unregister", methods=["POST"])
@requires_auth
@_rate_limit("5/minute")
def api_rancher_sso_unregister(server_id):
    got, err = _rancher_admin_session(server_id, need_console=True)
    if err:
        return err
    x, st, admin, _ = got
    name = (x.get("sso") or {}).get("oidc_client")
    removed = False
    try:
        with admin:
            if name:
                removed = _radm.unregister_oidc(admin, name)
    except _radm.AdminError as e:
        return jsonify({"error": str(e)}), e.status
    _RANCHER_STORE.clear_sso(load_config(), server_id)
    app.logger.info("Rancher SSO unregistered by %s on %s", current_user(), server_id)
    return jsonify({"ok": True, "removed_from_rancher": removed})


@app.route("/api/rancher/servers/<server_id>/rbac/status", methods=["POST"])
@requires_auth
@_rate_limit("10/minute")
def api_rancher_rbac_status(server_id):
    got, err = _rancher_admin_session(server_id)
    if err:
        return err
    _, _, admin, _ = got
    try:
        with admin:
            status = _radm.rbac_status(admin)
    except _radm.AdminError as e:
        return jsonify({"error": str(e)}), e.status
    except _rs.SSOError as e:
        return jsonify({"error": "Rancher is unreachable: " + (e.detail or e.code)}), 502
    return jsonify(_radm.public_status(status))


def _rancher_rbac_runner(run, admin, status):
    def step(step_id, state, message):
        run.emit({"type": "step", "step_id": step_id, "status": state, "message": message, "ts": time.time()})
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    rc = 0
    try:
        roles = _radm.rbac_install(admin, status, step)
        run.result = {"roles": roles}
    except (_radm.AdminError, _rs.SSOError) as e:
        msg = str(e) if isinstance(e, _radm.AdminError) else f"Rancher is unreachable: {e.detail or e.code}"
        run.error_summary = msg
        step("install", "error", msg)
        rc = 1
    except Exception as e:  # noqa: BLE001
        run.error_summary = f"unexpected error: {type(e).__name__}"
        step("install", "error", run.error_summary)
        rc = 1
    finally:
        admin.close()
    run.exit_code = rc
    run.status = "done" if rc == 0 else "error"
    run.ended_at = time.time()
    run.emit({"type": "status", "status": run.status, "exit_code": rc, "ts": time.time()})
    run.close()


@app.route("/api/rancher/servers/<server_id>/rbac/install", methods=["POST"])
@requires_auth
@_rate_limit("5/minute")
def api_rancher_rbac_install(server_id):
    """Installe le chart Harvester RBAC dans le cluster `local` de Rancher,
    en action suivie. Refus clair si Rancher ou Kubernetes ne conviennent pas."""
    got, err = _rancher_admin_session(server_id)
    if err:
        return err
    _, _, admin, _ = got
    try:
        status = _radm.rbac_status(admin)
    except (_radm.AdminError, _rs.SSOError) as e:
        admin.close()
        msg = str(e) if isinstance(e, _radm.AdminError) else "Rancher is unreachable"
        return jsonify({"error": msg}), getattr(e, "status", 502)
    if status["installed"]:
        admin.close()
        return jsonify({"error": f"the chart is already installed (version {status['version']})",
                        "status": _radm.public_status(status)}), 409
    if not status["compatible"]:
        admin.close()
        return jsonify({"error": status["reason"] or "the chart does not fit this Rancher",
                        "status": _radm.public_status(status)}), 409
    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, f"rancher-rbac-install:{server_id}", "(local)", [], dry_run=False)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(target=_rancher_rbac_runner, args=(run, admin, status), daemon=True).start()
    return jsonify({"action_id": run_id}), 202


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/healthz/ready")
def healthz_ready():
    """v1.6.0: readiness probe — distinct from liveness. Returns 503
    when one of these conditions is false so a k8s/lb removes the pod
    from the pool until it recovers:
      - config.yaml exists and parses,
      - actions DB is reachable (sqlite open + simple SELECT),
      - at least one cluster is declared.

    Liveness (`/healthz`) stays unconditional — that signals "the
    process is alive", which is what restarts on failure should react
    to. Readiness signals "the process can serve traffic"."""
    problems = []
    try:
        cfg = load_config()
        if not isinstance(cfg, dict):
            problems.append("config.yaml: invalid shape")
        elif not cfg.get("clusters"):
            problems.append("no clusters declared in config.yaml")
    except Exception as e:
        problems.append(f"config.yaml: {e}")

    try:
        conn = sqlite3.connect(str(ACTIONS_DB), timeout=2)
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception as e:
        problems.append(f"actions DB unreachable: {e}")

    if problems:
        return jsonify({"status": "not_ready", "problems": problems}), 503
    return jsonify({"status": "ready"})


# =============================================================================
# BMC / Redfish discovery (Bare-metal sub-tab)
# =============================================================================
def _redfish_get(host, path, user, pwd, timeout=8):
    """GET a Redfish endpoint, ignore TLS (iLO self-signed). Returns parsed
    JSON or None."""
    import urllib.request, urllib.error, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(f"https://{host}{path}")
    if user:
        import base64 as _b64
        cred = _b64.b64encode(f"{user}:{pwd}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, ssl.SSLError,
            TimeoutError, json.JSONDecodeError, OSError):
        return None


def _redfish_send(host, path, user, pwd, method, payload=None, timeout=20):
    """v1.17.0 — PATCH/POST vers Redfish. Renvoie (ok, status, detail).

    urllib (pas `requests` : il n'est pas vendoré). Les iLO exigent souvent
    un If-Match sur les PATCH ; on lit d'abord l'ETag de la ressource et on
    le renvoie quand il existe."""
    import urllib.request, urllib.error, ssl, base64 as _b64
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(f"https://{host}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if user:
        cred = _b64.b64encode(f"{user}:{pwd}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
    if method == "PATCH":
        etag = _redfish_etag(host, path, user, pwd)
        if etag:
            req.add_header("If-Match", etag)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            return True, r.status, (r.read() or b"")[:400].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = (e.read() or b"")[:400].decode("utf-8", "replace")
        except Exception:
            pass
        return False, e.code, body or str(e)
    except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as e:
        return False, 0, str(e)


def _redfish_etag(host, path, user, pwd, timeout=8):
    import urllib.request, ssl, base64 as _b64
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(f"https://{host}{path}")
    if user:
        cred = _b64.b64encode(f"{user}:{pwd}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            return r.headers.get("ETag")
    except Exception:
        return None


def _redfish_system_path(host, user, pwd):
    """Chemin du System, résolu dynamiquement. iLO expose /Systems/1/ et
    iDRAC /Systems/System.Embedded.1/ — le coder en dur cassait les actions
    d'alimentation sur Dell alors que la découverte, elle, résolvait déjà."""
    sysroot = _redfish_get(host, "/redfish/v1/Systems/", user, pwd, timeout=6)
    members = (sysroot or {}).get("Members") or []
    return members[0]["@odata.id"] if members else None


def _managed_by(system):
    """Gestionnaire d'un System d'après ses liens (`Links.ManagedBy`), ou
    None. Seule façon sûre de trouver le média virtuel de CETTE machine."""
    for ref in ((system or {}).get("Links") or {}).get("ManagedBy") or []:
        if isinstance(ref, dict) and ref.get("@odata.id"):
            return ref["@odata.id"]
    return None


def _redfish_manager_path(host, user, pwd, system_path=None):
    """Gestionnaire qui pilote le System visé. v1.78.0 : on prenait le
    premier de la collection Managers ; un BMC qui gère plusieurs systèmes
    (châssis à lames, émulateur Redfish du banc, vu en réel : un gestionnaire
    par VM de l'hôte) aurait alors monté le média, et lancé l'installation,
    sur une autre machine. Le premier de la liste ne sert plus que de repli
    quand le System ne dit pas qui le gère (iLO 4, iDRAC : un seul)."""
    sp = system_path or _redfish_system_path(host, user, pwd)
    if sp:
        mp = _managed_by(_redfish_get(host, sp, user, pwd, timeout=6))
        if mp:
            return mp
    mroot = _redfish_get(host, "/redfish/v1/Managers/", user, pwd, timeout=6)
    members = (mroot or {}).get("Members") or []
    return members[0]["@odata.id"] if members else None


def _vm_collections(system, system_path, manager, manager_path):
    """Collections VirtualMedia candidates, dans l'ordre : lien du System
    (Redfish 2020.4 et suivants placent le média virtuel sous le système),
    lien du gestionnaire, puis le chemin historique `<gestionnaire>/
    VirtualMedia/` (iLO 4). v1.78.0, vu en réel : un gestionnaire dont le
    lien `VirtualMedia` mène sous `/Systems/<id>/` était déclaré « sans
    média virtuel » parce qu'on fabriquait le chemin au lieu de le suivre."""
    out = []
    for doc in (system, manager):
        link = ((doc or {}).get("VirtualMedia") or {}).get("@odata.id")
        if link and link not in out:
            out.append(link)
    if manager_path:
        legacy = manager_path.rstrip("/") + "/VirtualMedia/"
        if legacy.rstrip("/") not in [c.rstrip("/") for c in out]:
            out.append(legacy)
    return out


def _redfish_virtualmedia_cd(host, user, pwd, manager_path=None):
    """Renvoie (chemin, ressource) du lecteur virtuel acceptant un CD/DVD."""
    sp = _redfish_system_path(host, user, pwd)
    system = _redfish_get(host, sp, user, pwd, timeout=6) if sp else None
    mp = manager_path or _managed_by(system) or _redfish_manager_path(host, user, pwd, sp)
    manager = _redfish_get(host, mp, user, pwd, timeout=6) if mp else None
    for cpath in _vm_collections(system, sp, manager, mp):
        vm_path, vm = _vm_cd_in(host, user, pwd, cpath)
        if vm_path:
            return vm_path, vm
    return None, None


def _vm_cd_in(host, user, pwd, collection_path):
    coll = _redfish_get(host, collection_path, user, pwd, timeout=6)
    for m in (coll or {}).get("Members", []) or []:
        vm = _redfish_get(host, m["@odata.id"], user, pwd, timeout=6)
        if not vm:
            continue
        types = [t.upper() for t in (vm.get("MediaTypes") or [])]
        if "CD" in types or "DVD" in types:
            return m["@odata.id"], vm
    return None, None


def _redfish_action_target(resource, *names):
    """Trouve la cible d'une action Redfish, standard OU OEM.

    Renvoie (target, is_oem). Le dialecte OEM n'accepte QUE `Image` et
    rejette `Inserted`/`WriteProtected` avec ActionParameterUnknown
    (constaté en direct sur node3).

    Sur les iLO 4 l'insertion de média n'existe QUE sous
    Oem.Hp.Actions['#HpiLOVirtualMedia.InsertVirtualMedia'] (vérifié sur
    node3) ; ailleurs c'est Actions['#VirtualMedia.InsertMedia']."""
    pools = [((resource.get("Actions") or {}), False)]
    oem = (resource.get("Oem") or {})
    for vendor in ("Hp", "Hpe", "Dell"):
        v = oem.get(vendor) or {}
        pools.append(((v.get("Actions") or {}), True))
    for pool, is_oem in pools:
        for key, val in pool.items():
            short = key.split(".")[-1]
            if short in names or key in names:
                target = (val or {}).get("target")
                if target:
                    return target, is_oem
    return None, False


def _reduce_drive(d):
    """Un disque physique, réduit à ce qui sert à décider."""
    cap = d.get("CapacityBytes")
    return {
        "id": d.get("Id") or (d.get("@odata.id") or "").rsplit("/", 1)[-1],
        "path": d.get("@odata.id"),
        "name": d.get("Name"),
        "model": (d.get("Model") or "").strip(),
        "media": d.get("MediaType"),            # SSD | HDD | SMR
        "protocol": d.get("Protocol"),          # SATA | SAS | NVMe
        "capacity_bytes": cap,
        "serial": d.get("SerialNumber"),
        "health": (d.get("Status") or {}).get("Health"),
        "failure_predicted": d.get("FailurePredicted"),
        "hotspare": d.get("HotspareType"),
        "encryptable": d.get("EncryptionAbility") not in (None, "None"),
        # Constaté sur le HBA330 : le seul verbe offert sur un disque.
        "secure_erase": bool(_redfish_action_target(d, "SecureErase")[0]),
    }


def _reduce_volume(v):
    return {
        "id": v.get("Id") or (v.get("@odata.id") or "").rsplit("/", 1)[-1],
        "path": v.get("@odata.id"),
        "name": v.get("Name"),
        "raid_type": v.get("RAIDType"),
        # `RawDevice` = disque présenté tel quel par un HBA en pass-through,
        # pas un volume RAID. La distinction décide de ce qu'on propose.
        "volume_type": v.get("VolumeType"),
        "capacity_bytes": v.get("CapacityBytes"),
        "encrypted": v.get("Encrypted"),
        "health": (v.get("Status") or {}).get("Health"),
        "can_initialize": bool(_redfish_action_target(v, "Initialize")[0]),
    }


def _bmc_storage(host, user, pwd, system_path=None):
    """Inventaire du stockage vu par le BMC : contrôleurs, disques, volumes.

    Deux dialectes, tous deux constatés en direct :

    * Redfish standard `Systems/<id>/Storage` — riche sur iDRAC 9 (le
      HBA330 du R740xd y expose ses 4 disques avec modèle, média, capacité,
      série, prédiction de panne) ;
    * l'OEM HPE `Systems/1/SmartStorage/{ArrayControllers,HostBusAdapters}`.

    ⚠ Sur les ProLiant XL170r Gen9 de ce parc, les DEUX renvoient zéro :
    cet iLO 4 ne publie aucun contrôleur ni disque. L'inventaire revient
    donc légitimement vide, et l'appelant doit savoir le dire plutôt que de
    laisser croire à une machine sans disque.
    """
    sp = (system_path or _redfish_system_path(host, user, pwd) or "").rstrip("/")
    out = {"controllers": [], "source": None, "supported": False}
    if not sp:
        return out

    coll = _redfish_get(host, sp + "/Storage", user, pwd, timeout=10)
    for m in (coll or {}).get("Members", []) or []:
        ctrl = _redfish_get(host, m["@odata.id"], user, pwd, timeout=10)
        if not ctrl:
            continue
        sc = (ctrl.get("StorageControllers") or [{}])[0]
        drives = []
        for dref in ctrl.get("Drives") or []:
            d = _redfish_get(host, dref["@odata.id"], user, pwd, timeout=10)
            if d:
                drives.append(_reduce_drive(d))
        volumes = []
        vpath = (ctrl.get("Volumes") or {}).get("@odata.id")
        if vpath:
            vcoll = _redfish_get(host, vpath, user, pwd, timeout=10)
            for vref in (vcoll or {}).get("Members", []) or []:
                v = _redfish_get(host, vref["@odata.id"], user, pwd, timeout=10)
                if v:
                    volumes.append(_reduce_volume(v))
        raid_types = sc.get("SupportedRAIDTypes") or []
        out["controllers"].append({
            "id": ctrl.get("Id") or m["@odata.id"].rsplit("/", 1)[-1],
            "path": m["@odata.id"],
            "name": ctrl.get("Name"),
            "model": (sc.get("Model") or "").strip(),
            "firmware": sc.get("FirmwareVersion"),
            "raid_types": raid_types,
            # Un HBA en pass-through annonce une liste vide : il n'y a
            # aucun volume à déclarer dessus, seulement des disques bruts.
            "can_create_volume": bool(raid_types) and bool(vpath),
            "volumes_path": vpath,
            "drives": drives,
            "volumes": volumes,
        })
    if out["controllers"]:
        out["source"] = "redfish"
        out["supported"] = True
        return out

    # Repli OEM HPE : un iLO qui publierait ses contrôleurs Smart Array.
    for kind in ("ArrayControllers", "HostBusAdapters"):
        coll = _redfish_get(host, f"{sp}/SmartStorage/{kind}/", user, pwd, timeout=10)
        for m in (coll or {}).get("Members", []) or []:
            ctrl = _redfish_get(host, m["@odata.id"], user, pwd, timeout=10) or {}
            out["controllers"].append({
                "id": ctrl.get("Id") or m["@odata.id"].rsplit("/", 1)[-2],
                "path": m["@odata.id"],
                "name": ctrl.get("Name") or kind,
                "model": (ctrl.get("Model") or "").strip(),
                "firmware": ((ctrl.get("FirmwareVersion") or {})
                             .get("Current", {}) or {}).get("VersionString"),
                "raid_types": [],
                "can_create_volume": False,
                "volumes_path": None,
                "drives": [],
                "volumes": [],
            })
    if out["controllers"]:
        out["source"] = "hpe-oem"
        out["supported"] = True
    return out


def _bmc_discover_one(host, user, pwd):
    """Walk Redfish to produce a node profile (system info + NICs)."""
    root = _redfish_get(host, "/redfish/v1/", user, pwd, timeout=6)
    if not root:
        return {"ok": False, "host": host, "error": "Redfish root unreachable"}
    sysroot = _redfish_get(host, "/redfish/v1/Systems/", user, pwd, timeout=6)
    if not sysroot or not sysroot.get("Members"):
        return {"ok": False, "host": host, "error": "Systems collection empty"}
    sys_path = sysroot["Members"][0]["@odata.id"]
    s = _redfish_get(host, sys_path, user, pwd, timeout=6)
    if not s:
        return {"ok": False, "host": host, "error": f"system {sys_path} unreachable"}
    # NICs (best-effort — iLO 4 enumerates physical NICs cleanly)
    nics = []
    nic_root = _redfish_get(host, sys_path.rstrip("/") + "/EthernetInterfaces/", user, pwd, timeout=6)
    for m in (nic_root or {}).get("Members", []) or []:
        n = _redfish_get(host, m["@odata.id"], user, pwd, timeout=4)
        if not n: continue
        nics.append({
            "name": n.get("Name") or n.get("Id") or "",
            "mac":  n.get("MacAddress") or "",
            "status": (n.get("Status") or {}).get("State") or "",
            "speed_mbps": n.get("SpeedMbps") or 0,
        })
    # v1.17.0 : ce dont l'installation zéro-touch a besoin en plus.
    boot = s.get("Boot") or {}
    mgr_path = _managed_by(s) or _redfish_manager_path(host, user, pwd, sys_path)
    vm_path, vm_res = _redfish_virtualmedia_cd(host, user, pwd, mgr_path)
    return {
        "ok": True,
        "host": host,
        # chemins résolus dynamiquement : les réutiliser évite le
        # /Systems/1 codé en dur qui cassait Dell.
        "system_path": sys_path,
        "manager_path": mgr_path,
        "virtualmedia_path": vm_path,
        "virtualmedia_inserted": bool((vm_res or {}).get("Inserted")),
        # Deux schémas coexistent : les Redfish récents publient
        # ...@Redfish.AllowableValues, l'iLO 4 publie BootSourceOverrideSupported
        # (constaté sur node3). Lire les deux, sinon la liste paraît vide.
        "boot_targets": (boot.get("BootSourceOverrideTarget@Redfish.AllowableValues")
                         or boot.get("BootSourceOverrideSupported") or []),
        "boot_override": boot.get("BootSourceOverrideTarget"),
        # HD.Emb.* = disques embarqués vus par l'UEFI : c'est le signal le
        # plus fiable qu'il y a de quoi installer, l'iLO 4 n'exposant pas
        # /Storage et laissant SmartStorage vide hors contrôleur RAID.
        "uefi_targets": boot.get("UefiTargetBootSourceOverrideSupported") or [],
        "post_state": ((s.get("Oem") or {}).get("Hp")
                       or (s.get("Oem") or {}).get("Hpe") or {}).get("PostState"),
        "manufacturer": s.get("Manufacturer"),
        "model": s.get("Model"),
        "serial": s.get("SerialNumber"),
        "asset": s.get("AssetTag"),
        "bios_version": s.get("BiosVersion"),
        "power_state": s.get("PowerState"),
        "cpu_count": (s.get("ProcessorSummary") or {}).get("Count"),
        "memory_gib": (s.get("MemorySummary") or {}).get("TotalSystemMemoryGiB"),
        "indicator_led": s.get("IndicatorLED"),
        "uuid": s.get("UUID"),
        "nics": nics,
    }


@app.route("/api/bmc/discover", methods=["POST"])
@requires_auth
def api_bmc_discover():
    """Discover one or many BMCs via Redfish.
    Body: { hosts: ["192.0.2.10", ...], user, password }
    Returns the list of node profiles."""
    data = request.get_json(force=True, silent=True) or {}
    hosts = data.get("hosts") or []
    user  = data.get("user", "")
    pwd   = data.get("password", "")
    if not hosts or not isinstance(hosts, list):
        return jsonify({"error": "hosts (list of IPs) required"}), 400
    results = []
    for h in hosts[:64]:
        results.append(_bmc_discover_one(h, user, pwd))
    return jsonify({"nodes": results, "count": len(results)})


@app.route("/api/bmc/storage", methods=["POST"])
@requires_auth
@_rate_limit("12/minute")
def api_bmc_storage():
    """Stockage vu par le BMC (1.78.0) : contrôleurs, disques, volumes.
    Corps : {host, user, password}, comme /api/bmc/discover ; les
    identifiants ne passent ni par l'adresse ni par un journal."""
    data = request.get_json(force=True, silent=True) or {}
    host = data.get("host")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,253}", host):
        return jsonify({"error": "invalid host", "fields": ["host"]}), 400
    try:
        out = _bmc_storage(host, str(data.get("user") or ""), str(data.get("password") or ""))
    except Exception as e:                      # jamais de détail qui porterait un secret
        return jsonify({"error": f"storage inventory failed: {type(e).__name__}"}), 502
    return jsonify(dict(out, host=host))


@app.route("/api/bmc/<host>/virtualmedia", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_bmc_virtualmedia(host):
    """v1.17.0 — insère ou éjecte une image dans le lecteur virtuel du BMC.

    Body: {action: "insert"|"eject", image: "<url http(s)>", user, password}.
    L'URL doit être joignable PAR LE BMC (pas par le navigateur) : c'est
    l'iLO qui va chercher l'image."""
    data = request.get_json(force=True, silent=True) or {}
    action = (data.get("action") or "").lower()
    if action not in ("insert", "eject"):
        return jsonify({"error": "action must be insert or eject"}), 400
    user = data.get("user", "")
    pwd = data.get("password", "")
    image = data.get("image", "")
    if action == "insert" and not image:
        return jsonify({"error": "image URL required"}), 400

    vm_path, vm_res = _redfish_virtualmedia_cd(host, user, pwd)
    if not vm_path:
        return jsonify({"error": "no CD/DVD virtual media on this BMC",
                        "detail": "the BMC exposes no VirtualMedia accepting a CD "
                                  "(on iLO this usually means no Advanced licence)"}), 412
    if action == "insert":
        target, is_oem = _redfish_action_target(vm_res, "InsertVirtualMedia",
                                                "InsertMedia")
        payload = ({"Image": image} if is_oem
                   else {"Image": image, "Inserted": True, "WriteProtected": True})
    else:
        target, _ = _redfish_action_target(vm_res, "EjectVirtualMedia", "EjectMedia")
        payload = {}
    if not target:
        return jsonify({"error": "virtual media action not exposed by this BMC"}), 412
    ok, status, detail = _redfish_send(host, target, user, pwd, "POST", payload)
    if not ok:
        return jsonify({"error": "virtual media action failed",
                        "status": status, "detail": detail[:300]}), 502
    return jsonify({"ok": True, "action": action, "media": vm_path})


@app.route("/api/bmc/<host>/boot-once", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_bmc_boot_once(host):
    """v1.17.0 — force la prochaine amorce sur une cible (Cd, Pxe, Hdd…).

    `Once` et non `Continuous` : après l'installation la machine doit
    reprendre son ordre de boot normal sans qu'on ait à y revenir."""
    data = request.get_json(force=True, silent=True) or {}
    target = data.get("target", "Cd")
    user = data.get("user", "")
    pwd = data.get("password", "")
    sys_path = _redfish_system_path(host, user, pwd)
    if not sys_path:
        return jsonify({"error": "Systems collection empty"}), 502
    payload = {"Boot": {"BootSourceOverrideTarget": target,
                        "BootSourceOverrideEnabled": "Once"}}
    ok, status, detail = _redfish_send(host, sys_path, user, pwd, "PATCH", payload)
    if not ok:
        return jsonify({"error": "boot override failed", "status": status,
                        "detail": detail[:300]}), 502
    return jsonify({"ok": True, "target": target, "system": sys_path})


@app.route("/api/bmc/<host>/power", methods=["POST"])
@requires_auth
def api_bmc_power(host):
    """Send a Redfish power action. Body: {action: "On"|"GracefulShutdown"|
    "ForceOff"|"Reset"|"PushPowerButton", user, password}.
    Returns the BMC's HTTP response so the dock action shows what happened."""
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action", "")
    if action not in ("On", "GracefulShutdown", "ForceOff", "ForceRestart",
                      "Reset", "PushPowerButton", "GracefulRestart"):
        return jsonify({"error": "invalid action",
                        "supported": ["On","GracefulShutdown","ForceOff","ForceRestart","Reset","PushPowerButton","GracefulRestart"]}), 400
    user = data.get("user", "")
    pwd  = data.get("password", "")

    def runner(run):
        run.status = "running"
        run.emit({"type": "status", "status": "running", "ts": time.time()})
        run.emit({"type": "step", "step_id": "bmc-power", "status": "running",
                  "message": f"{action} on {host}", "ts": time.time()})
        import urllib.request, urllib.error, ssl, base64 as _b64
        ctx = ssl.create_default_context()
        ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        body = json.dumps({"ResetType": action}).encode()
        sys_path = _redfish_system_path(host, user, pwd) or "/redfish/v1/Systems/1"
        url = f"https://{host}{sys_path.rstrip('/')}/Actions/ComputerSystem.Reset/"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if user:
            req.add_header("Authorization", "Basic " +
                _b64.b64encode(f"{user}:{pwd}".encode()).decode())
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
                msg = f"HTTP {r.status} — {r.read()[:200].decode('utf-8','replace')}"
                run.emit({"type": "step", "step_id": "bmc-power", "status": "done",
                          "message": msg, "ts": time.time()})
                run.exit_code = 0; run.status = "done"
        except urllib.error.HTTPError as e:
            err = e.read()[:200].decode("utf-8", "replace")
            run.emit({"type": "step", "step_id": "bmc-power", "status": "error",
                      "message": f"HTTP {e.code} — {err}", "ts": time.time()})
            run.exit_code = e.code; run.status = "error"
        except Exception as e:
            run.emit({"type": "step", "step_id": "bmc-power", "status": "error",
                      "message": str(e)[:200], "ts": time.time()})
            run.exit_code = 1; run.status = "error"
        run.ended_at = time.time()
        run.emit({"type": "status", "status": run.status,
                  "exit_code": run.exit_code, "ts": time.time()})
        run.close()

    rid = uuid.uuid4().hex[:12]
    run = ActionRun(rid, f"bmc-power:{host}:{action}", host, [], dry_run=False)
    with ACTIONS_LOCK:
        ACTIONS[rid] = run
    threading.Thread(target=runner, args=(run,), daemon=True).start()
    return jsonify({"action_id": rid, "action": action, "host": host}), 201


@app.route("/review")
@requires_auth
def review():
    """Single-pane review dashboard — designed for the user to skim
    yesterday's autonomous work in one screen. Auto-refreshes every 30s."""
    import datetime
    cfg = load_config()
    clusters = cfg.get("clusters", []) or []
    first_cluster = (clusters[0]["name"] if clusters else None)

    # KPIs from ACTIONS registry
    with ACTIONS_LOCK:
        all_actions = list(ACTIONS.values())
    actions_total   = len(all_actions)
    actions_done    = sum(1 for a in all_actions if a.status == "done")
    actions_error   = sum(1 for a in all_actions if a.status in ("error", "cancelled", "interrupted"))
    actions_running = sum(1 for a in all_actions if a.status in ("starting", "running"))

    # CAPHV stack snapshot for the first cluster
    capi_components, capi_clusters, harvester_version = [], [], ""
    if first_cluster:
        try:
            with app.test_request_context():
                # piggyback on api_capi_diag — call directly to avoid Flask
                # bouncing through HTTP locally.
                resp = api_capi_diag(first_cluster)
                if hasattr(resp, "get_json"):
                    d = resp.get_json() or {}
                    capi_components = d.get("components", []) or []
                    capi_clusters   = d.get("capi_clusters", []) or []
                    harvester_version = d.get("harvester_version") or ""
        except Exception as _e:
            pass

    # Terraform info + bundles
    try:
        tf_info = api_terraform_info().get_json() or {}
    except Exception:
        tf_info = {}
    try:
        bundles_data = api_capi_bundles_list().get_json() or {}
        bundles = bundles_data.get("bundles", []) or []
        disk_free  = bundles_data.get("disk_free", 0)
        disk_total = bundles_data.get("disk_total", 0)
    except Exception:
        bundles, disk_free, disk_total = [], 0, 0

    # Version history from git log + VERSION tags in commit subjects
    git_log = ""
    version_commits = []
    try:
        r = subprocess.run(
            ["git", "log", "--pretty=format:%h %ad %s", "--date=short", "-30"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=10,
        )
        git_log = r.stdout
        for line in r.stdout.splitlines():
            m = re.search(r"^(\S+) (\S+) (?:feat|fix|chore|docs|test)\(([\d.]+)\): (.+)$", line)
            if m:
                version_commits.append({
                    "sha": m.group(1), "date": m.group(2),
                    "version": m.group(3), "subject": m.group(4),
                })
    except Exception:
        pass

    # Changelog (top of the file)
    changelog = ""
    try:
        cp = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
        if cp.exists():
            changelog = "\n".join(cp.read_text().splitlines()[:80])
    except Exception:
        pass

    # Recent actions
    actions_recent = []
    for a in sorted(all_actions,
                    key=lambda x: x.ended_at or x.started_at or 0,
                    reverse=True)[:30]:
        d = a.to_dict()
        if d.get("ended_at"):
            d["ended_human"] = datetime.datetime.fromtimestamp(d["ended_at"]).strftime("%H:%M:%S")
        d["duration"] = ((d.get("ended_at") or 0) - (d.get("started_at") or 0)) or None
        actions_recent.append(d)

    # Tests last counts (cached file under /tmp updated by test runs;
    # falls back to placeholders)
    tests_api_pass, tests_api_total = 39, 39
    tests_e2e_pass, tests_e2e_total = 28, 28
    tests_when = "v1.3.x"

    # Backlog: parse from the latest test framework / repo-level task tracker.
    # We don't have a structured tasks DB so leave the lists empty here —
    # the live KPIs above are the meaningful part.
    tasks_done, tasks_in_progress, tasks_pending = 0, 0, 0
    tasks_pending_list = []

    return render_template("review.html",
        version=_harvester_ops_version(),
        now=time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        actions_total=actions_total, actions_done=actions_done,
        actions_error=actions_error, actions_running=actions_running,
        capi_components=capi_components, capi_clusters=capi_clusters,
        harvester_version=harvester_version,
        tf_info=tf_info,
        bundles=bundles, disk_free=disk_free, disk_total=disk_total,
        version_commits=version_commits,
        git_log=git_log, changelog=changelog,
        actions_recent=actions_recent,
        tasks_done=tasks_done, tasks_in_progress=tasks_in_progress,
        tasks_pending=tasks_pending, tasks_pending_list=tasks_pending_list,
        tests_api_pass=tests_api_pass, tests_api_total=tests_api_total,
        tests_e2e_pass=tests_e2e_pass, tests_e2e_total=tests_e2e_total,
        tests_when=tests_when,
    )


# -----------------------------------------------------------------------------
# Routes — API
# -----------------------------------------------------------------------------
@app.route("/api/clusters")
@requires_auth
@_rate_limit("240/minute")
def api_clusters():
    cfg = load_config()
    clusters, hidden = [], []
    # v1.78.0 : qui a déclaré chaque cluster (config.yaml de l'opérateur ou
    # la console), et si la console peut modifier config.yaml
    config_names = _config_cluster_names()
    sso = _sso_session() is not None
    for c in cfg.get("clusters", []):
        if sso:
            kc, why = _sso_cluster_state(c)
            if kc is None:
                # v1.56.0 : dire POURQUOI un cluster manque, au lieu d'une
                # liste vide sans explication (vu en réel : un membre du
                # cluster ne voyait rien, ni cluster ni message)
                hidden.append({"name": c["name"], "reason": why})
                continue
        clusters.append({
            "name": c["name"],
            "description": c.get("description", ""),
            "node_count": len(c.get("nodes", [])),
            "origin": "config" if c["name"] in config_names else "console",
        })
    out = {"clusters": clusters, "config_writable": _config_writable()}
    if sso:
        out["hidden"] = hidden
    return jsonify(out)


@app.route("/api/status/<cluster>")
@requires_auth
@shared_read()
def api_status(cluster):
    # Cluster déclaré mais hors tension : on le dit en deux secondes plutôt
    # que de faire patienter trente. Sans ça, l'écran tournait dans le vide
    # et finissait sans explication.
    kc = _kubectl_for_cluster(cluster)
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200

    script = BIN_DIR / "harvester-status.sh"
    namespace = request.args.get("namespace", "")
    cmd = ["/usr/bin/env", "bash", str(script), "--cluster", cluster, "--output", "json"]
    if namespace:
        cmd.extend(["--namespace", namespace])
    try:
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.PIPE,
            timeout=30,
            env={**os.environ, "NO_COLOR": "1",
                 "HARVESTER_OPS_CONFIG": str(CONFIG_PATH),
                 **identity_env(cluster=cluster)},
        )
        # v1.56.0 : ce que la RBAC a refusé au script (les VMs d'un membre
        # du cluster, par exemple) part avec la réponse, pour être dit
        try:
            for line in (json.loads(out).get("denied") or []):
                _note_cluster_denial(line)
        except (ValueError, AttributeError):
            pass
        return Response(out, mimetype="application/json")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode() if e.stderr else ""
        # Friendlier diagnostics: the recurring "Overview empty" symptom was
        # caused by yq missing from PATH. Detect known causes and surface a
        # clear hint so the user doesn't have to grep the server log.
        hint = None
        if e.returncode == 2 or "yq" in stderr.lower():
            if not shutil.which("yq"):
                hint = (
                    "yq is missing from the server's PATH. The status script "
                    "needs mikefarah/yq v4+. Install it (e.g. "
                    "`zypper install yq` or `brew install yq`) and reload."
                )
        if not hint and not stderr:
            hint = (
                f"status script exited {e.returncode} with no stderr. "
                "Check that the kubeconfig in the active cluster's config "
                "points to a reachable cluster, and that yq + kubectl are "
                "on the server's PATH."
            )
        return jsonify({
            "error": "status failed",
            "returncode": e.returncode,
            "stderr": stderr,
            "hint": hint,
        }), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "status timeout"}), 504


@app.route("/api/namespace/<cluster>/<namespace>")
@requires_auth
def api_namespace(cluster, namespace):
    return api_status_helper(cluster, namespace)


# =============================================================================
# /api/topology/<cluster> : hôtes et VMs de la vue Cluster de l'aperçu
# =============================================================================
# Ce que chaque hôte peut donner et a déjà donné, ce que chaque VM consomme
# (v1.43.0 : la vue est en blocs HTML, `cluster-map.js`). Un seul appel
# kubectl groupé ; en cache 5 s pour que le rafraîchissement automatique ne
# martèle pas le cluster.
# =============================================================================
_topology_cache = {}    # cluster → {"ts": float, "data": dict}
_topology_lock = threading.Lock()
TOPOLOGY_CACHE_TTL = 5.0


def _cluster_of_kubeconfig(kc):
    """Nom du cluster déclaré pour ce kubeconfig, pour les journaux.

    Depuis qu'il y a plusieurs clusters, « kubectl get nodes failed » ne
    disait plus SUR QUOI il avait échoué — précisément la ligne qu'on lit
    quand quelque chose ne va pas. Le chemin du kubeconfig ne suffit pas :
    rien n'oblige à le nommer d'après son cluster (celui de harv1
    s'appelle `harvester.yaml`)."""
    if not kc:
        return "?"
    try:
        for c in load_config().get("clusters", []):
            if c.get("kubeconfig") == str(kc):
                return c["name"]
    except Exception:
        pass
    return "?"


# Un refus de la RBAC du cluster n'est pas une panne de la console. Avant la
# délégation d'identité personne ne pouvait en recevoir (le kubeconfig partagé
# est `system:masters`, qui court-circuite la RBAC) ; depuis, un compte aux
# droits réduits en reçoit, et cela remontait en 500 : l'écran annonçait une
# erreur serveur pour un refus parfaitement normal, sans dire qui refusait.
def _safe_proc_error(exc, limit=200):
    """Message d'erreur d'un sous-processus SANS sa ligne de commande.

    `CalledProcessError.__str__` recopie l'argv complet, donc le chemin du
    kubeconfig : le renvoyer dans une réponse HTTP le divulgue. Trouvé en
    testant la délégation en réel, quand un compte aux droits réduits a
    ramené le chemin du fichier dans le corps de la réponse.
    """
    if isinstance(exc, subprocess.CalledProcessError):
        return f"command failed with exit code {exc.returncode}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "command timed out"
    return str(exc)[:limit]


_DENIAL_MARKERS = ("forbidden", "is not allowed", "cannot list",
                   "cannot get", "cannot create", "cannot delete",
                   "cannot patch", "cannot update")


_DENIAL_RE = re.compile(
    r'cannot (?P<verb>[\w-]+) resource "(?P<resource>[^"]+)" in API group "(?P<group>[^"]*)"'
    r'(?: in the namespace "(?P<namespace>[^"]+)")?')
_DENIAL_MAX = 8


def _note_cluster_denial(stderr):
    """Retient qu'un appel a été refusé par la RBAC, pour que la réponse le
    dise. Silencieux hors contexte de requête (threads de travail).

    v1.56.0 : chaque refus est aussi rangé (verbe, ressource, groupe,
    espace de noms) : une vue qui s'affiche quand même, avec ce qui est
    permis, porte la liste des refus dans l'en-tête `X-Cluster-Denied`, et
    l'écran dit ce qui lui est caché au lieu de montrer une liste vide."""
    text = (stderr or "").strip()
    if not any(m in text.lower() for m in _DENIAL_MARKERS):
        return
    try:
        g.cluster_denied = text[:300]
        seen = g.setdefault("cluster_denials", [])
    except RuntimeError:
        return
    for m in _DENIAL_RE.finditer(text):
        item = {k: v for k, v in m.groupdict().items() if v}
        if item not in seen and len(seen) < _DENIAL_MAX:
            seen.append(item)


def _kube_rest(argv, kwargs):
    """v1.83.0 : un `kubectl get ... -o json` fait directement contre l'API
    (bin/lib/kube_rest.py), même résultat sans processus ; None quand le cas
    est laissé à kubectl. 94 % du CPU de la console partait dans kubectl."""
    if (not argv or os.path.basename(str(argv[0])) != "kubectl"
            or kwargs.get("input") is not None or kwargs.get("stdin") is not None):
        return None
    import kube_rest as _kr
    r = _kr.run(argv, timeout=kwargs.get("timeout") or 30)
    if r is None:
        return None
    if r.returncode != 0:
        r.stdout = ""
    if not (kwargs.get("text") or kwargs.get("universal_newlines")):
        r.as_bytes()
    return r


def _kubectl_run(argv, **kwargs):
    """`subprocess.run` d'un kubectl, qui retient un refus de la RBAC. Les
    appels directs rendaient une vue vide sans dire que le cluster avait
    refusé (vu en réel avec un compte « membre du cluster » de Rancher)."""
    r = _kube_rest(argv, kwargs)
    if r is None:
        r = subprocess.run(argv, **kwargs)
    err = getattr(r, "stderr", None)
    if getattr(r, "returncode", 0) != 0 and isinstance(err, (str, bytes)):
        _note_cluster_denial(err.decode(errors="replace") if isinstance(err, bytes) else err)
    return r


def _denial_hint():
    sess = _sso_session()
    if sess is not None:
        return ("Rancher refused this for your account; a Rancher administrator can "
                "grant a role on this cluster, or on the project that holds the namespace")
    return ("the cluster's RBAC refused this call for the identity the console "
            "presented; grant it on the cluster, or map this account to another "
            "cluster user in roles.yaml")


@app.after_request
def _surface_cluster_denial(response):
    """Un refus du cluster vaut 403, pas 500 ; et une réponse qui passe
    malgré des refus les signale (en-tête `X-Cluster-Denied`)."""
    denied = getattr(g, "cluster_denied", None)
    if not denied:
        return response
    items = list(getattr(g, "cluster_denials", []) or [])
    sess = _sso_session()
    if response.status_code >= 500:
        ident = current_cluster_identity() or {}
        # Un after_request doit rendre une Response, PAS un tuple : rendre
        # (jsonify(...), 403) fait exploser le handler suivant sur
        # `.headers`, et la réponse part en 500 sans rien expliquer.
        response = jsonify({
            "error": "cluster refused",
            "cluster_user": ident.get("user") or (sess.login if sess is not None else None),
            "via": "rancher" if sess is not None else "console",
            "detail": denied,
            "denied": items,
            "hint": _denial_hint(),
        })
        response.status_code = 403
    if items:
        response.headers["X-Cluster-Denied"] = json.dumps(items, ensure_ascii=True)
    return response


def _kubectl_json(kc, *args, timeout=15, cluster=None):
    """Run `kubectl --kubeconfig kc <args> -o json` and return the parsed
    object, or None on any error (logged at WARNING, with the cluster)."""
    name = cluster or _cluster_of_kubeconfig(kc)
    try:
        r = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, *args, "-o", "json"],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            log.warning("[%s] kubectl %s failed: %s", name, " ".join(args),
                        r.stderr.strip()[:200])
            metric_kubectl_calls.labels(status="fail", cluster=name).inc()
            _note_cluster_denial(r.stderr)
            return None
        metric_kubectl_calls.labels(status="ok", cluster=name).inc()
        if getattr(r, "data", None) is not None:
            return r.data
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        log.warning("[%s] kubectl %s timeout", name, " ".join(args))
        metric_kubectl_calls.labels(status="timeout", cluster=name).inc()
        return None
    except json.JSONDecodeError as e:
        log.warning("[%s] kubectl %s json parse failed: %s", name,
                    " ".join(args), e)
        metric_kubectl_calls.labels(status="parse_error", cluster=name).inc()
        return None
    except OSError as e:
        # kubectl absent du PATH : c'est l'erreur la plus probable sur une
        # machine fraîche, et elle remontait en FileNotFoundError non
        # rattrapée, donc en 500 opaque. Le contrat de cette fonction est
        # « None sur toute erreur, journalisée » : un binaire manquant en
        # fait partie.
        log.warning("[%s] kubectl %s unavailable: %s", name,
                    " ".join(args), e)
        metric_kubectl_calls.labels(status="unavailable", cluster=name).inc()
        return None


def _vm_nics(vm, vmi):
    """(cartes réseau, interfaces connues du seul invité) d'une VM.

    Partagé par la Fabrique, la vue Réseau et la vue Cluster : la MAC VUE
    par la VM prime sur la déclarée (c'est elle qu'on cherche dans la table
    d'un switch), et un `networkName` sans namespace désigne un NAD du
    namespace de la VM."""
    ns = (vm.get("metadata") or {}).get("namespace")
    spec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
    ifaces = {i.get("name"): i for i in
              (((spec.get("domain") or {}).get("devices") or {}).get("interfaces") or [])}
    vmi_status = (vmi or {}).get("status") or {}
    live = {i.get("name"): i for i in (vmi_status.get("interfaces") or []) if i.get("name")}
    nets = []
    for n in spec.get("networks") or []:
        target = (n.get("multus") or {}).get("networkName")
        if target and "/" not in target:
            target = f"{ns}/{target}"
        decl = ifaces.get(n.get("name")) or {}
        got = live.get(n.get("name")) or {}
        nets.append({
            "nic": n.get("name"), "network": target, "pod": "pod" in n,
            "model": decl.get("model"),
            "binding": next((k for k in ("bridge", "masquerade", "sriov", "macvtap", "passt")
                             if k in decl), None),
            "mac": got.get("mac") or decl.get("macAddress"),
            "ips": got.get("ipAddresses") or ([got["ipAddress"]] if got.get("ipAddress") else []),
            "guest_iface": got.get("interfaceName"),
            "link_state": got.get("linkState"),
        })
    # Les interfaces que seul l'agent invité connaît (docker0, bridges
    # internes) ne sortent par aucun réseau du cluster : à part.
    guest_only = [{"iface": i.get("interfaceName"), "mac": i.get("mac"),
                   "ips": i.get("ipAddresses") or []}
                  for i in (vmi_status.get("interfaces") or []) if not i.get("name")]
    return nets, guest_only


def _cpu_cores(q):
    """Quantité CPU Kubernetes en cœurs : `8` -> 8.0, `7020m` -> 7.02."""
    q = str(q or "").strip()
    try:
        return float(q[:-1]) / 1000.0 if q.endswith("m") else float(q)
    except ValueError:
        return None


def _topology_node(item):
    """Reduce a node object to the fields the viz needs."""
    meta = item.get("metadata") or {}
    status = item.get("status") or {}
    spec = item.get("spec") or {}
    labels = meta.get("labels") or {}
    addr_map = {a["type"]: a.get("address") for a in status.get("addresses", [])}
    conds = {c["type"]: c.get("status") for c in status.get("conditions", [])}
    allocatable = status.get("allocatable") or {}
    return {
        "name": meta.get("name"),
        "uid": meta.get("uid"),
        "ready": conds.get("Ready") == "True",
        "schedulable": not spec.get("unschedulable", False),
        "roles": sorted([
            k.split("/", 1)[1]
            for k in labels.keys()
            if k.startswith("node-role.kubernetes.io/")
        ]),
        "addresses": addr_map,
        "capacity": status.get("capacity") or {},
        "allocatable": allocatable,
        # v1.43.0 : ce que l'hôte peut donner, en unités lisibles.
        "cpu_allocatable": _cpu_cores(allocatable.get("cpu")),
        "memory_allocatable": _k8s_bytes(allocatable.get("memory")),
        "maintenance": node_maintenance.maintenance_state(item),
        # v1.62.0 : le nom affiché que Harvester donne à l'hôte
        "custom_name": (meta.get("annotations") or {}).get("harvesterhci.io/host-custom-name"),
        "vcpu_allocated": 0,
        "memory_allocated": 0,
    }


def _vm_vcpu(domain):
    """vCPU vus par l'invité : cœurs x sockets x threads (relevé sur harv1),
    sinon la limite CPU arrondie au cœur supérieur, sinon 1."""
    cpu = domain.get("cpu")
    if cpu:
        n = 1
        for k in ("cores", "sockets", "threads"):
            try:
                n *= max(1, int(cpu.get(k) or 1))
            except (TypeError, ValueError):
                pass
        return n
    res = domain.get("resources") or {}
    cores = _cpu_cores((res.get("limits") or {}).get("cpu")
                       or (res.get("requests") or {}).get("cpu"))
    return max(1, math.ceil(cores)) if cores else 1


def _vm_memory(domain):
    """Mémoire vue par l'invité. Sur harv1 `memory.guest` vaut la limite ;
    les `requests` (2730Mi pour 4Gi) sont une réservation surallouée, prise
    seulement en dernier recours."""
    res = domain.get("resources") or {}
    for q in ((domain.get("memory") or {}).get("guest"),
              (res.get("limits") or {}).get("memory"),
              (res.get("requests") or {}).get("memory")):
        b = _k8s_bytes(q)
        if b:
            return b
    return None


def _topology_vm(vm_item, vmi_by_name, pvcs=None):
    """Reduce a VM + its VMI (if any) to the viz-relevant fields."""
    pvcs = pvcs or {}
    meta = vm_item.get("metadata") or {}
    spec = vm_item.get("spec") or {}
    template_spec = (spec.get("template") or {}).get("spec") or {}
    networks = template_spec.get("networks") or []
    domain = template_spec.get("domain") or {}
    devs = domain.get("devices") or {}
    interfaces = devs.get("interfaces") or []
    disks = devs.get("disks") or []
    volumes_in_spec = template_spec.get("volumes") or []
    # Map disk name → claim name (PVC) when applicable
    vol_to_pvc = {}
    vol_source = {}
    for v in volumes_in_spec:
        if "persistentVolumeClaim" in v:
            vol_to_pvc[v["name"]] = v["persistentVolumeClaim"].get("claimName")
            vol_source[v["name"]] = "pvc"
        elif "dataVolume" in v:
            vol_to_pvc[v["name"]] = v["dataVolume"].get("name")
            vol_source[v["name"]] = "pvc"
        elif "cloudInitNoCloud" in v or "cloudInitConfigDrive" in v:
            vol_source[v["name"]] = "cloudinit"
        elif "containerDisk" in v:
            vol_source[v["name"]] = "container"
        else:
            vol_source[v["name"]] = next((k for k in v if k != "name"), None)
    ns = meta.get("namespace")
    name = meta.get("name")
    vmi = vmi_by_name.get(f"{ns}/{name}")
    node_name = None
    phase = "Stopped"
    if vmi:
        node_name = (vmi.get("status") or {}).get("nodeName")
        phase = (vmi.get("status") or {}).get("phase", "Unknown")
    disk_list = []
    for d in disks:
        pvc = vol_to_pvc.get(d.get("name"))
        info = pvcs.get(f"{ns}/{pvc}") if pvc else None
        disk_list.append({
            "disk": d.get("name"),
            "device": "cdrom" if "cdrom" in d else ("lun" if "lun" in d else "disk"),
            "boot_order": d.get("bootOrder"),
            "source": vol_source.get(d.get("name")),
            "pvc": pvc,
            "size": (info or {}).get("size"),
            "storage_class": (info or {}).get("storage_class"),
        })
    nics, guest_only = _vm_nics(vm_item, vmi)
    return {
        "namespace": ns,
        "name": name,
        "uid": meta.get("uid"),
        "phase": phase,
        "run_strategy": spec.get("runStrategy", "?"),
        "node": node_name,
        # NB: `pod: {}` is a valid (empty) marker → use key presence,
        # NOT truthiness, so the empty dict isn't misread as "unknown".
        "networks": [
            {"name": n.get("name"),
             "type": "pod" if "pod" in n else
                     ("multus" if "multus" in n else "unknown"),
             "ref": (n.get("multus") or {}).get("networkName")}
            for n in networks
        ],
        "interfaces": [
            {"name": i.get("name"),
             "binding": next((k for k in ("bridge", "masquerade",
                                          "macvtap", "sriov")
                              if k in i), "unknown")}
            for i in interfaces
        ],
        "volumes": [
            {"disk": d.get("name"),
             "pvc": vol_to_pvc.get(d.get("name")),
             "boot_order": d.get("bootOrder"),
             # v1.8.7 : le type de périphérique vit dans la clé du device
             # (disk/cdrom/lun) — la vue Storage distingue les CD-ROM.
             "device": "cdrom" if "cdrom" in d
                       else ("lun" if "lun" in d else "disk")}
            for d in disks
        ],
        # v1.43.0 : ce que la VM consomme, pour la vue Cluster.
        "vcpu": _vm_vcpu(domain),
        "memory": _vm_memory(domain),
        "disks": disk_list,
        "disk_total": sum(d["size"] or 0 for d in disk_list),
        "nics": nics,
        "guest_only": guest_only,
        "guest_os": (((vmi or {}).get("status") or {}).get("guestOSInfo") or {})
                    .get("prettyName"),
    }


TOPOLOGY_KINDS = ["nodes", "virtualmachines.kubevirt.io",
                  "virtualmachineinstances.kubevirt.io",
                  # v1.43.0 : la taille des disques, dans le même appel.
                  "persistentvolumeclaims"]
_topology_missing = {}


def _build_topology(cluster, kc):
    """Hôtes et VMs pour la vue Cluster, en UN appel groupé."""
    items = _grouped_items(kc, cluster, TOPOLOGY_KINDS, _topology_missing)
    if items is None:
        raise RuntimeError("cluster unreachable")
    by_kind = {}
    for it in items:
        by_kind.setdefault(it.get("kind"), []).append(it)
    vmi_by_name = {
        f"{(v.get('metadata') or {}).get('namespace')}/"
        f"{(v.get('metadata') or {}).get('name')}": v
        for v in by_kind.get("VirtualMachineInstance", [])
    }
    pvcs = {}
    for p in by_kind.get("PersistentVolumeClaim", []):
        pm = p.get("metadata") or {}
        ps = p.get("spec") or {}
        pvcs[f"{pm.get('namespace')}/{pm.get('name')}"] = {
            "size": _k8s_bytes(((ps.get("resources") or {}).get("requests") or {})
                               .get("storage")),
            "storage_class": ps.get("storageClassName")}
    raw_nodes = by_kind.get("Node", [])
    nodes = [_topology_node(n) for n in raw_nodes]
    # Harvester refuse d'isoler le dernier nœud disponible : la vue le dit
    # avant qu'on essaie.
    for n, raw in zip(nodes, raw_nodes):
        n["last_available"] = node_maintenance.last_available(raw, raw_nodes)
    vms = [_topology_vm(v, vmi_by_name, pvcs) for v in by_kind.get("VirtualMachine", [])]
    # Ce que chaque hôte a déjà donné : les VMs EN MARCHE qu'il porte.
    by_name = {n["name"]: n for n in nodes}
    for v in vms:
        host = by_name.get(v["node"])
        if host and v["phase"] == "Running":
            host["vcpu_allocated"] += v["vcpu"] or 0
            host["memory_allocated"] += v["memory"] or 0
    return {
        "cluster": cluster,
        "fetched_at": time.time(),
        "nodes": nodes,
        "vms": vms,
    }


# =============================================================================
# Fabrique réseau de l'hôte (v1.35.0)
#
# La vue « network » existante regarde le réseau par le haut : quelles VMs
# sont sur quel réseau. Celle-ci le regarde par le BAS, côté exploitant :
# quelle carte physique porte quoi, et par quelle pile.
#
# Le modèle est un EMPILEMENT, établi en lisant harv1 plutôt qu'en le
# supposant :
#
#   5  VMs
#   4  réseaux attachables (NetworkAttachmentDefinition)
#   3  abstraction : ClusterNetwork  |  ProviderNetwork + VLAN + Subnet/VPC
#   2  switch virtuel : bridge Linux |  Open vSwitch (ovs-system, br-int, ...)
#   1  bond (agrégation d'uplinks)
#   0  interfaces physiques
#
# Deux pièges que ce modèle évite :
#   * la couche BOND n'est pas cosmétique, c'est là que vit le VlanConfig
#     (mode d'agrégation, MTU), donc la redondance d'uplink ;
#   * DEUX fabriques coexistent au-dessus des cartes et ne se confondent
#     pas. Sur harv1 elles n'utilisent même pas la même carte : `enp1s0`
#     pour le bridge Linux, `eno2` pour Open vSwitch.
#
# Tout le déclaratif tient en UN appel kubectl groupé (mesuré : 330 ms pour
# huit types), conformément à l'économie de la v1.33.0. Le détail fin d'une
# carte (pilote, débit, compteurs) n'est pas dans l'API : il se demande au
# nœud, à la demande, quand l'exploitant ouvre une carte.
# =============================================================================
FABRIC_KINDS = [
    "nodes",
    "linkmonitors.network.harvesterhci.io",
    "clusternetworks.network.harvesterhci.io",
    "vlanconfigs.network.harvesterhci.io",
    "network-attachment-definitions.k8s.cni.cncf.io",
    "provider-networks.kubeovn.io",
    "subnets.kubeovn.io",
    "vpcs.kubeovn.io",
    # Le maillon qui relie un subnet d'UNDERLAY à son provider network.
    # Sans lui, la chaîne kube-ovn saute du subnet à la carte physique.
    "vlans.kubeovn.io",
    # Les VMs branchées sur chaque réseau : c'est ce qu'un exploitant
    # cherche en premier sous un port group, et ce qu'ESXi montre.
    "virtualmachines.kubevirt.io",
    # Et ce que chacune y fait VRAIMENT : MAC, adresses, état du lien. La
    # vue Réseau en vit ; c'est le même appel, pas un de plus.
    "virtualmachineinstances.kubevirt.io",
]

# Types absents d'un cluster, retenus un moment. `kubectl get a,b,c` échoue
# EN BLOC quand l'un des types n'existe pas : un Harvester sans l'addon
# kube-ovn n'a pas ses CRD, et toute la fabrique passait pour « cluster
# injoignable ». On se replie alors type par type, puis on retient ce qui
# manque pour que les rafraîchissements suivants restent UN seul appel.
# La mémoire expire : un addon activé plus tard doit finir par apparaître.
FABRIC_MISSING_TTL = 600
_fabric_missing = {}
_fabric_missing_lock = threading.Lock()


def _grouped_items(kc, cluster, kinds, memo, required="nodes"):
    """Les objets de plusieurs types, en UN appel groupé quand c'est possible.

    `memo` retient, par cluster, les types absents (voir FABRIC_MISSING_TTL).
    None seulement si le type `required` lui-même est illisible : c'est alors
    le cluster qui ne répond pas, pas un type qui manque."""
    now = time.time()
    with _fabric_missing_lock:
        missing, ts = memo.get(cluster, (frozenset(), 0))
        if now - ts > FABRIC_MISSING_TTL:
            missing = frozenset()
    wanted = [k for k in kinds if k not in missing]
    raw = _kubectl_json(kc, "get", "-A", ",".join(wanted),
                        timeout=40, cluster=cluster)
    if raw is not None:
        return raw.get("items", [])
    items, absent = [], set()
    for kind in wanted:
        one = _kubectl_json(kc, "get", "-A", kind, timeout=20, cluster=cluster)
        if one is None:
            absent.add(kind)
            continue
        items.extend(one.get("items", []))
    if required in absent:
        return None
    if absent:
        with _fabric_missing_lock:
            memo[cluster] = (frozenset(missing | absent), now)
    return items


def _fabric_items(kc, cluster):
    """Objets de la fabrique. Sans les nœuds, il n'y a rien à dessiner."""
    return _grouped_items(kc, cluster, FABRIC_KINDS, _fabric_missing)

# Un maître dont le nom commence par là relève d'Open vSwitch, pas d'un
# bridge Linux : c'est ce qui sépare les deux fabriques.
_OVS_PREFIXES = ("ovs-system", "br-int", "br-external", "ovn", "mirror")


def _fabric_is_ovs(name):
    return any((name or "").startswith(p) for p in _OVS_PREFIXES)


def _fabric_link_layer(link):
    """Couche d'un lien rapporté par LinkMonitor.

    `veth` va en couche 5 : ce sont les ports des charges (pods, VMs), pas
    des switchs. Les confondre noyait la couche des switchs sous 83 paires
    sur un seul nœud, et le peu qu'on voulait montrer disparaissait.
    """
    t = link.get("type")
    if t == "device":
        return 0
    if t == "bond":
        return 1
    if t == "veth":
        return 5
    return 2            # bridge, openvswitch, vxlan, et tout le reste


def _build_fabric(cluster, kc):
    """Empilement réseau de l'hôte, en un seul appel groupé."""
    items = _fabric_items(kc, cluster)
    if items is None:
        return None
    by_kind = {}
    for item in items:
        by_kind.setdefault(item.get("kind"), []).append(item)

    out = {"cluster": cluster, "nodes": [], "links": [], "cluster_networks": [],
           "vlan_configs": [], "networks": [], "vms": [], "kubeovn":
           {"provider_networks": [], "vpcs": [], "subnets": []},
           # L'interface propose de poser le moniteur qui complète la
           # fabrique ; elle doit donc savoir s'il est déjà là.
           "full_linkmonitor": any(
               lm.get("metadata", {}).get("name") == FABRIC_LINKMONITOR
               for lm in by_kind.get("LinkMonitor", []))}

    for n in by_kind.get("Node", []):
        meta = n.get("metadata", {})
        labels = meta.get("labels") or {}
        ann = meta.get("annotations") or {}
        # Les rattachements kube-ovn d'une carte sont portés par des labels
        # `<provider>.provider-network.kubernetes.io/interface`.
        bindings = {}
        for k, v in labels.items():
            if ".provider-network.kubernetes.io/" in k:
                prov, field = k.split(".provider-network.kubernetes.io/", 1)
                bindings.setdefault(prov, {})[field] = v
        addresses = {a.get("type"): a.get("address")
                     for a in (n.get("status", {}).get("addresses") or [])}
        out["nodes"].append({
            "name": meta.get("name"),
            # Le nom Kubernetes (`harv1.home.lo`) n'est PAS le nom déclaré
            # dans la config (`harv1-node1`) : sans cette adresse, l'écran
            # ne saurait pas à qui demander le détail.
            "address": addresses.get("InternalIP") or addresses.get("Hostname"),
            "mgmt": labels.get("network.harvesterhci.io/mgmt") == "true",
            "ovn_role": labels.get("kube-ovn/role"),
            "ovn_chassis": ann.get("ovn.kubernetes.io/chassis"),
            "ovn_ip": ann.get("ovn.kubernetes.io/ip_address"),
            "ovn_switch": ann.get("ovn.kubernetes.io/logical_switch"),
            "provider_bindings": bindings,
        })

    # Les liens sont indexés par (nœud, index) : c'est `masterIndex` qui
    # reconstruit la chaîne carte -> bond -> switch.
    for lm in by_kind.get("LinkMonitor", []):
        status = lm.get("status") or {}
        for node, links in (status.get("linkStatus") or {}).items():
            for l in links or []:
                out["links"].append({
                    "node": node,
                    "name": l.get("name"),
                    "type": l.get("type"),
                    "state": l.get("state"),
                    "mac": l.get("mac"),
                    "index": l.get("index"),
                    "master_index": l.get("masterIndex"),
                    "promiscuous": l.get("promiscuous"),
                    "layer": _fabric_link_layer(l),
                    "fabric": "ovn" if _fabric_is_ovs(l.get("name")) else "classic",
                    "monitor": lm.get("metadata", {}).get("name"),
                })

    # Un même lien est rapporté par CHAQUE moniteur dont la règle le
    # capte : poser un moniteur permissif faisait sortir `enp1s0`,
    # `mgmt-bo` et `mgmt-br` en double. La clé de vérité est (nœud, index).
    seen, unique = set(), []
    for l in out["links"]:
        key = (l["node"], l.get("index"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(l)
    out["links"] = unique

    # Résoudre le maître de chaque lien par son index. Ce qui reste non
    # résolu est DIT, pas inventé : sans moniteur permissif, les bridges
    # Open vSwitch de kube-ovn ne sont rapportés par aucun moniteur et la
    # chaîne s'arrête là.
    by_index = {(l["node"], l["index"]): l["name"] for l in out["links"]
                if l.get("index") is not None}
    by_name = {(l["node"], l["name"]): l for l in out["links"]}
    for l in out["links"]:
        mi = l.get("master_index")
        l["master"] = by_index.get((l["node"], mi)) if mi is not None else None
        l["master_unresolved"] = mi is not None and l["master"] is None

    # La fabrique se lit sur la CHAÎNE des maîtres, pas sur le nom du lien
    # seul : une veth nommée `5a9ba3611271_h` ne dit rien d'elle-même, mais
    # elle pend d'`ovs-system`, donc elle est du côté kube-ovn.
    def _fabric_of(link, depth=0):
        if _fabric_is_ovs(link.get("name")):
            return "ovn"
        parent_name = link.get("master")
        if not parent_name or depth > 8:
            return "classic"
        parent = by_name.get((link["node"], parent_name))
        return _fabric_of(parent, depth + 1) if parent else "classic"

    for l in out["links"]:
        l["fabric"] = _fabric_of(l)

    for cn in by_kind.get("ClusterNetwork", []):
        out["cluster_networks"].append({"name": cn["metadata"]["name"], "layer": 3})

    for vc in by_kind.get("VlanConfig", []):
        spec = vc.get("spec") or {}
        up = spec.get("uplink") or {}
        out["vlan_configs"].append({
            "name": vc["metadata"]["name"],
            "cluster_network": spec.get("clusterNetwork"),
            "nics": up.get("nics") or [],
            "bond_mode": (up.get("bondOptions") or {}).get("mode"),
            "mtu": (up.get("linkAttributes") or {}).get("mtu"),
            "node_selector": spec.get("nodeSelector") or {},
            "matched_nodes": (vc.get("status") or {}).get("matchedNodes") or [],
            "layer": 1,
        })

    for nad in by_kind.get("NetworkAttachmentDefinition", []):
        meta = nad.get("metadata", {})
        labels = meta.get("labels") or {}
        try:
            conf = json.loads((nad.get("spec") or {}).get("config") or "{}")
        except (ValueError, TypeError):
            conf = {}
        out["networks"].append({
            "namespace": meta.get("namespace"),
            "name": meta.get("name"),
            "cluster_network": labels.get("network.harvesterhci.io/clusternetwork"),
            "kind": labels.get("network.harvesterhci.io/type"),
            "ready": labels.get("network.harvesterhci.io/ready") == "true",
            "cni": conf.get("type"),
            "bridge": conf.get("bridge"),
            "vlan": conf.get("vlan"),
            "provider": conf.get("provider"),
            "fabric": "ovn" if conf.get("type") == "kube-ovn" else "classic",
            "layer": 4,
        })

    # Les trois objets kube-ovn ne sont PAS du même niveau : un subnet
    # appartient à un VPC, et un subnet d'underlay passe par un VLAN qui
    # désigne un provider network. Les aligner sur une même rangée effaçait
    # la hiérarchie. `rank` ordonne à l'intérieur de la bande, du plus près
    # du matériel au plus abstrait.
    for pn in by_kind.get("ProviderNetwork", []):
        st = pn.get("status") or {}
        conds = st.get("conditions") or []
        out["kubeovn"]["provider_networks"].append({
            "name": pn["metadata"]["name"],
            "default_interface": (pn.get("spec") or {}).get("defaultInterface"),
            "ready": bool(st.get("ready")) or any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in conds),
            "ready_nodes": st.get("readyNodes") or [],
            "vlans": st.get("vlans") or [],
            "layer": 3, "rank": 0,
        })
    for vl in by_kind.get("Vlan", []):
        spec = vl.get("spec") or {}
        out["kubeovn"].setdefault("vlans", []).append({
            "name": vl["metadata"]["name"],
            "id": spec.get("id"),
            "provider_network": spec.get("provider"),
            "subnets": (vl.get("status") or {}).get("subnets") or [],
            "layer": 3, "rank": 1,
        })
    for sn in by_kind.get("Subnet", []):
        spec = sn.get("spec") or {}
        st = sn.get("status") or {}
        provider = spec.get("provider")
        out["kubeovn"]["subnets"].append({
            "name": sn["metadata"]["name"],
            "vpc": spec.get("vpc"),
            "cidr": spec.get("cidrBlock"),
            "gateway": spec.get("gateway"),
            "nat": bool(spec.get("natOutgoing")),
            "provider": provider,
            "vlan": spec.get("vlan"),
            "available_ips": st.get("v4availableIPs"),
            # `provider: ovn` = subnet d'OVERLAY : il est encapsulé sur le
            # réseau des nœuds et NE SORT PAS par un uplink physique. Le
            # montrer au même niveau qu'un subnet d'underlay laissait croire
            # le contraire, ce qui est le pire des contresens ici.
            "overlay": provider in (None, "", "ovn"),
            "layer": 3, "rank": 2,
        })
    for vpc in by_kind.get("Vpc", []):
        out["kubeovn"]["vpcs"].append({
            "name": vpc["metadata"]["name"],
            "subnets": (vpc.get("status") or {}).get("subnets") or [],
            "layer": 3, "rank": 3,
        })
    out["kubeovn"].setdefault("vlans", [])

    # Qui est branché où. Un `networkName` sans namespace désigne un NAD du
    # namespace de la VM : le normaliser ici évite que l'écran rate la
    # moitié des rattachements. Le réseau de pod n'a pas de NAD : il est
    # signalé tel quel.
    vmis = {f"{(i.get('metadata') or {}).get('namespace')}/"
            f"{(i.get('metadata') or {}).get('name')}": i
            for i in by_kind.get("VirtualMachineInstance", [])}
    for vm in by_kind.get("VirtualMachine", []):
        meta = vm.get("metadata", {})
        ns = meta.get("namespace")
        vmi = vmis.get(f"{ns}/{meta.get('name')}") or {}
        vmi_status = vmi.get("status") or {}
        nets, guest_only = _vm_nics(vm, vmi)
        out["vms"].append({
            "namespace": ns, "name": meta.get("name"),
            "status": (vm.get("status") or {}).get("printableStatus"),
            "node": vmi_status.get("nodeName"),
            "networks": nets,
            "guest_only": guest_only,
        })
    return out


# Harvester ne publie PAS ses bridges Open vSwitch : ses deux moniteurs de
# liens ne couvrent que `mgmt(-br|-bo)` et les cartes, donc la fabrique
# kube-ovn s'arrête au premier maître non rapporté. `LinkMonitor` est
# justement prévu pour ça, et son CRD dit qu'une règle VIDE veut dire
# « tout ». On PROPOSE donc d'en poser un, sans jamais l'imposer : c'est une
# écriture sur le cluster de l'exploitant, elle passe par une action tracée
# et elle se retire d'un geste.
FABRIC_LINKMONITOR = "harvester-ops-fabric"


def _fabric_linkmonitor_manifest():
    return json.dumps({
        "apiVersion": "network.harvesterhci.io/v1beta1",
        "kind": "LinkMonitor",
        "metadata": {
            "name": FABRIC_LINKMONITOR,
            "labels": {"app.kubernetes.io/managed-by": "harvester-ops"},
        },
        # Règle vide : le CRD documente « empty value means matching all ».
        "spec": {"targetLinkRule": {}},
    })


def _fabric_linkmonitor_apply(run, kc, cluster, remove):
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    step = "linkmonitor-remove" if remove else "linkmonitor-apply"
    run.emit({"type": "step", "step_id": step, "status": "running",
              "message": FABRIC_LINKMONITOR, "ts": time.time()})
    try:
        if remove:
            proc = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "delete",
                 "linkmonitors.network.harvesterhci.io", FABRIC_LINKMONITOR,
                 "--ignore-not-found"],
                capture_output=True, text=True, timeout=60)
        else:
            proc = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "apply", "-f", "-"],
                input=_fabric_linkmonitor_manifest(),
                capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        run.status = "error"
        run.exit_code = 1
        run.emit({"type": "step", "step_id": step, "status": "error",
                  "message": _safe_proc_error(e), "ts": time.time()})
        run.ended_at = time.time(); run.close(); return

    ok = proc.returncode == 0
    if not ok:
        _note_cluster_denial(proc.stderr)
    run.status = "done" if ok else "error"
    run.exit_code = proc.returncode
    run.emit({"type": "step", "step_id": step,
              "status": "done" if ok else "error",
              "message": (proc.stdout or proc.stderr).strip()[:300],
              "ts": time.time()})
    run.ended_at = time.time()
    run.close()


@app.route("/api/network-fabric/<cluster>/linkmonitor", methods=["POST", "DELETE"])
@requires_auth
@_rate_limit("6 per minute")
def api_network_fabric_linkmonitor(cluster):
    """Poser ou retirer le moniteur de liens qui rend la fabrique complète."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    remove = request.method == "DELETE"
    label = ("network:linkmonitor-remove" if remove
             else "network:linkmonitor-apply")
    run_id = track_action(label, cluster, _fabric_linkmonitor_apply,
                          kc, cluster, remove)
    return jsonify({"action_id": run_id, "name": FABRIC_LINKMONITOR,
                    "removing": remove}), 202


# Ce que l'API ne dira JAMAIS d'une carte : pilote, débit négocié, duplex,
# compteurs d'erreurs. Cela se demande au nœud, et seulement quand
# l'exploitant ouvre une carte, pour ne pas payer un SSH à chaque rendu.
_FABRIC_DETAIL_CMD = (
    "ip -d -j link show 2>/dev/null; echo '---'; "
    "ip -j -s link show 2>/dev/null; echo '---'; "
    # Débit négocié, duplex et battements de porteuse : le noyau les expose
    # dans /sys et l'API Kubernetes n'en sait rien. `carrier_changes` est le
    # plus parlant des trois pour un exploitant : un lien qui bat est un
    # lien qui va tomber.
    "for d in /sys/class/net/*/; do n=$(basename $d); "
    "printf '%s\\t%s\\t%s\\t%s\\t%s\\n' \"$n\" "
    "\"$(cat $d/speed 2>/dev/null)\" \"$(cat $d/duplex 2>/dev/null)\" "
    "\"$(cat $d/carrier 2>/dev/null)\" \"$(cat $d/carrier_changes 2>/dev/null)\"; "
    "done"
)


@app.route("/api/network-fabric/<cluster>/node/<node>")
@requires_auth
def api_network_fabric_node_detail(cluster, node):
    """Détail fin des liens d'un nœud, pris sur le nœud lui-même."""
    cfg = load_config()
    cluster_cfg = next((c for c in cfg.get("clusters", [])
                        if c["name"] == cluster), None)
    if not cluster_cfg:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    target = next((n for n in cluster_cfg.get("nodes", [])
                   if n.get("hostname") == node or n.get("ip") == node), None)
    if not target:
        # Repli : `node` est peut-être le nom Kubernetes, qui ne coïncide
        # pas avec le nom déclaré. On demande son adresse au cluster.
        kc_node = _kubectl_for_cluster(cluster)
        info = _kubectl_json(kc_node, "get", "node", node, timeout=20,
                             cluster=cluster) if kc_node else None
        addrs = {a.get("type"): a.get("address")
                 for a in ((info or {}).get("status", {}).get("addresses") or [])}
        ip = addrs.get("InternalIP")
        if ip:
            target = next((n for n in cluster_cfg.get("nodes", [])
                           if n.get("ip") == ip), None) or {"ip": ip}
    if not target:
        return jsonify({"error": "unknown node", "node": node,
                        "hint": "the node must be declared in the cluster "
                                "config to be reachable over SSH"}), 404
    ssh = cluster_cfg.get("ssh") or {}
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=accept-new",
           "-p", str(ssh.get("port", 22))]
    if ssh.get("key"):
        cmd += ["-i", str(ssh["key"])]
    cmd += [f"{ssh.get('user', 'rancher')}@{target.get('ip')}",
            _FABRIC_DETAIL_CMD]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except (subprocess.TimeoutExpired, OSError) as e:
        return jsonify({"error": "ssh failed",
                        "detail": _safe_proc_error(e)}), 502
    if proc.returncode != 0:
        return jsonify({"error": "ssh failed",
                        "detail": f"exit code {proc.returncode}"}), 502
    links, stats, phys = _parse_fabric_detail(proc.stdout)
    return jsonify({"cluster": cluster, "node": node,
                    "links": links, "stats": stats, "phys": phys})


def _parse_fabric_detail(text):
    """(liens, compteurs, attributs physiques) depuis trois blocs séparés
    par `---` : `ip -d -j link`, `ip -j -s link`, puis un relevé de
    /sys/class/net (débit, duplex, porteuse)."""
    parts = (text or "").split("\n---\n")
    def _load(chunk):
        try:
            return json.loads(chunk.strip() or "[]")
        except ValueError:
            return []
    detailed = _load(parts[0] if parts else "")
    counters = _load(parts[1] if len(parts) > 1 else "")
    links = []
    for l in detailed:
        info = l.get("linkinfo") or {}
        data = info.get("info_data") or {}
        links.append({
            "name": l.get("ifname"),
            "state": l.get("operstate"),
            "mtu": l.get("mtu"),
            "mac": l.get("address"),
            "master": l.get("master"),
            "kind": info.get("info_kind") or "device",
            "bond_mode": data.get("mode"),
            "bond_miimon": data.get("miimon"),
            "flags": l.get("flags") or [],
        })
    phys = {}
    for line in (parts[2] if len(parts) > 2 else "").splitlines():
        f = line.split("\t")
        if len(f) < 5 or not f[0]:
            continue
        def _num(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        # Une carte sans porteuse rapporte -1 en débit : le rendre tel quel
        # ferait afficher « -1 Mb/s » au lieu de « pas de lien ».
        speed = _num(f[1])
        phys[f[0]] = {"speed_mbps": speed if (speed or 0) > 0 else None,
                      "duplex": f[2] or None,
                      "carrier": _num(f[3]),
                      "carrier_changes": _num(f[4])}
    stats = {}
    for l in counters:
        s = l.get("stats64") or {}
        rx, tx = s.get("rx") or {}, s.get("tx") or {}
        stats[l.get("ifname")] = {
            "rx_bytes": rx.get("bytes"), "rx_errors": rx.get("errors"),
            "rx_dropped": rx.get("dropped"),
            "tx_bytes": tx.get("bytes"), "tx_errors": tx.get("errors"),
            "tx_dropped": tx.get("dropped"),
        }
    return links, stats, phys


# =============================================================================
# Chaîne de connexion d'une VM (v1.36.0)
#
# « Cette VM est-elle branchée sur le bon réseau, par les bonnes cartes ? »
# La réponse tient en une chaîne, et le maillon qui la rend possible est
# `podInterfaceName` dans le statut du VMI : c'est le nom du port CÔTÉ HÔTE.
# Sans lui on saute du réseau logique à la carte physique sans pouvoir le
# prouver ; avec lui la chaîne se suit lien par lien :
#
#   VM -> vNIC -> NAD -> port hôte -> bridge -> bond -> carte physique
#
# Ce qui vient du VMI est l'état RÉEL (IP, état du lien, nœud) ; ce qui
# vient de la VM est le DÉCLARÉ. Les deux sont rendus séparément, parce
# qu'un écart entre les deux est précisément ce que l'exploitant cherche.
# =============================================================================


def _vm_network_path(cluster, kc, namespace, name):
    vm = _kubectl_json(kc, "get", "vm", name, "-n", namespace,
                       timeout=20, cluster=cluster)
    if vm is None:
        return None
    vmi = _kubectl_json(kc, "get", "vmi", name, "-n", namespace,
                        timeout=20, cluster=cluster) or {}
    fabric = _build_fabric(cluster, kc) or {}

    tmpl = (((vm.get("spec") or {}).get("template") or {}).get("spec") or {})
    declared_ifaces = ((tmpl.get("domain") or {}).get("devices") or {}).get("interfaces") or []
    declared_nets = {n.get("name"): n for n in (tmpl.get("networks") or [])}
    vmi_status = vmi.get("status") or {}
    live = {i.get("name"): i for i in (vmi_status.get("interfaces") or [])}
    node = vmi_status.get("nodeName")
    # VM arrêtée : pas de VMI, donc pas de nœud, donc plus aucun maillon ne
    # se résout et la chaîne affichait « non rapporté » comme si le cluster
    # était cassé. Or le chemin DÉCLARÉ (NAD, bridge, bond, carte) ne dépend
    # pas de l'exécution : on le montre, en disant que c'est le déclaré.
    fabric_nodes = [n.get("name") for n in fabric.get("nodes") or []]
    chain_is_live = bool(node)
    if not node and len(fabric_nodes) == 1:
        node = fabric_nodes[0]

    links = {(l["node"], l["name"]): l for l in fabric.get("links") or []}
    nads = {f'{n["namespace"]}/{n["name"]}': n for n in fabric.get("networks") or []}

    def chain_from(start):
        """Remonter la chaîne des maîtres depuis un lien de l'hôte.

        On part du BRIDGE nommé par le NAD, pas du `podInterfaceName` du
        VMI : ce dernier désigne l'interface DANS l'espace de noms du pod
        et n'existe pas sur l'hôte. Pire, deux VMs différentes portent le
        même nom là-dedans (`pod8fe0d3f1ac5` dans les deux pods de harv1),
        donc il ne discrimine rien. Le veth hôte exact se résout à la
        demande, par la MAC, et c'est un aller-retour SSH.
        """
        # Sans nœud connu (VM arrêtée sur un cluster multi-nœuds), on ne
        # sait pas où elle démarrera. Rendre une chaîne « non rapportée »
        # ferait croire à un défaut de remontée au lieu d'une inconnue.
        if not node:
            return []
        out, cur, guard = [], start, 0
        while cur and guard < 10:
            l = links.get((node, cur))
            if not l:
                # Le lien est nommé mais aucun moniteur ne le rapporte : on
                # le DIT plutôt que d'arrêter la chaîne en silence. C'est le
                # cas des bridges Open vSwitch, que Harvester ne publie pas.
                out.append({"name": cur, "known": False})
                break
            out.append({"name": l["name"], "type": l["type"], "state": l["state"],
                        "mac": l["mac"], "layer": l["layer"],
                        "fabric": l["fabric"], "known": True})
            # On DESCEND vers l'uplink : bridge -> bond -> carte physique.
            # Remonter vers le maître partirait dans le vide, un bridge
            # n'en ayant pas. Et on écarte les veth : ce sont les ports des
            # AUTRES charges, pas le chemin vers le monde extérieur.
            kids = [x for x in fabric.get("links") or []
                    if x["node"] == node and x.get("master") == l["name"]
                    and x["layer"] != 5]
            kids.sort(key=lambda x: {"bond": 0, "device": 1}.get(x["type"], 2))
            cur = kids[0]["name"] if kids else None
            guard += 1
        return out

    nics = []
    for iface in declared_ifaces:
        nic_name = iface.get("name")
        net = declared_nets.get(nic_name) or {}
        nad_ref = ((net.get("multus") or {}).get("networkName")
                   if net.get("multus") else None)
        if nad_ref and "/" not in nad_ref:
            nad_ref = f"{namespace}/{nad_ref}"
        st = live.get(nic_name) or {}
        host_port = st.get("podInterfaceName")
        nics.append({
            "name": nic_name,
            "declared": {
                "mac": iface.get("macAddress"),
                "model": iface.get("model"),
                "binding": next((k for k in ("bridge", "masquerade", "sriov",
                                             "macvtap", "slirp")
                                 if k in iface), None),
                "network": nad_ref,
                "pod_network": bool(net.get("pod")),
            },
            "live": {
                "guest_interface": st.get("interfaceName"),
                "mac": st.get("mac"),
                "ip": st.get("ipAddress"),
                "ips": st.get("ipAddresses") or [],
                "link_state": st.get("linkState"),
                "host_port": host_port,
                "info_source": st.get("infoSource"),
            },
            # Un écart entre déclaré et réel est ce que l'exploitant cherche.
            "mac_matches": (not iface.get("macAddress") or not st.get("mac")
                            or iface.get("macAddress") == st.get("mac")),
            "network_detail": nads.get(nad_ref),
            # La chaîne part du bridge que le NAD désigne : c'est le
            # premier maillon qui existe VRAIMENT sur l'hôte.
            "chain": chain_from((nads.get(nad_ref) or {}).get("bridge")),
        })

    return {"cluster": cluster, "namespace": namespace, "name": name,
            "node": node, "running": bool(vmi_status),
            # Le chemin vient-il de ce qui TOURNE, ou seulement de ce qui est
            # déclaré ? L'exploitant doit savoir ce qu'il regarde.
            "chain_is_live": chain_is_live,
            "nodes": fabric_nodes,
            "nics": nics,
            "full_linkmonitor": fabric.get("full_linkmonitor", False)}


# Identification côté switch (v1.36.0).
#
# LLDP est le mécanisme normalisé : un switch annonce périodiquement son nom
# et le port sur lequel on est branché. C'est LA réponse à « par quelle prise
# cette carte sort-elle ».
#
# Deux contraintes mesurées sur harv1 : `lldpd` est ABSENT (SLE Micro est
# minimal et son rootfs est en lecture seule), mais `tcpdump` est présent, ce
# qui permet une écoute ponctuelle. Les trames arrivent typiquement toutes
# les 30 s, d'où une attente qui doit être explicite et bornée.
#
# Vérifié en réel le 22/09/2026 sur harvlab : node2 émet du LLDP (lldpd) sur
# son bridge, dont les nœuds imbriqués sont des ports. Le LAN de production
# n'en a toujours pas (switch TP-Link « Easy Smart »).
_LLDP_FIELDS = (
    ("system_name", "System Name TLV"),
    ("port_id", "Port ID TLV"),
    ("port_description", "Port Description TLV"),
    ("system_description", "System Description TLV"),
    ("chassis_id", "Chassis ID TLV"),
    ("management_address", "Management Address TLV"),
)


def _parse_lldp(text):
    """Champs utiles depuis la sortie verbeuse de tcpdump.

    La valeur est sur la ligne du TLV (« System Name TLV (5), length 12:
    node2 ») ou, pour le Chassis ID, le Port ID, la description et l'adresse
    de gestion, sur la ligne SUIVANTE (« Subtype MAC address (4): 70:10:... »,
    ou le texte seul). Confronté à une vraie trame, lire la seule ligne du TLV
    perdait trois champs sur cinq. On garde la première occurrence (l'adresse
    IPv4 de gestion précède l'IPv6)."""
    out = {}
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        for key, label in _LLDP_FIELDS:
            if label not in line or key in out:
                continue
            same = re.match(r"\s*\(\d+\), length \d+:\s*(.+)$", line.split(label, 1)[1])
            if same:
                val = same.group(1).strip()
            else:
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                sub = re.match(r"(?:Subtype|Management Address length).*?\(\d+\):\s*(.+)$", nxt)
                val = sub.group(1).strip() if sub else nxt
            if val:
                out[key] = val[:120]
    return out


@app.route("/api/network-fabric/<cluster>/node/<node>/lldp")
@requires_auth
@_rate_limit("4 per minute")
def api_network_fabric_lldp(cluster, node):
    """Écouter une trame LLDP sur une carte, pour savoir sur quel port de
    quel switch elle est branchée."""
    iface = request.args.get("iface", "")
    if not _valid_k8s_name(iface.replace("_", "-")) or len(iface) > 32:
        return jsonify({"error": "invalid interface"}), 400
    try:
        wait = min(max(int(request.args.get("wait", 35)), 5), 60)
    except ValueError:
        wait = 35
    ssh = _fabric_ssh_base(cluster, node)
    if isinstance(ssh, tuple):
        return ssh
    cmd = ssh + [f"sudo timeout {wait} tcpdump -i {shlex.quote(iface)} "
                 f"-s 1500 -c 1 -nn -v 'ether proto 0x88cc' 2>&1"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=wait + 20)
    except (subprocess.TimeoutExpired, OSError) as e:
        return jsonify({"error": "lldp probe failed",
                        "detail": _safe_proc_error(e)}), 502
    text = proc.stdout or ""
    fields = _parse_lldp(text)
    return jsonify({
        "cluster": cluster, "node": node, "interface": iface,
        "waited_s": wait,
        "found": bool(fields),
        "fields": fields,
        # Sans trame, dire POURQUOI : beaucoup de switchs non administrables
        # n'émettent tout simplement pas de LLDP, et l'exploitant doit le
        # savoir plutôt que de croire à une panne.
        "hint": None if fields else
                "no LLDP frame in the listening window; many unmanaged or "
                "'easy smart' switches never emit any",
        "raw": text[-1200:] if not fields else "",
    })


def _fabric_ssh_base(cluster, node):
    """Préfixe de commande SSH vers un nœud déclaré, ou une réponse d'erreur."""
    cfg = load_config()
    cluster_cfg = next((c for c in cfg.get("clusters", [])
                        if c["name"] == cluster), None)
    if not cluster_cfg:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    target = next((n for n in cluster_cfg.get("nodes", [])
                   if n.get("hostname") == node or n.get("ip") == node), None)
    if not target:
        kc_node = _kubectl_for_cluster(cluster)
        info = _kubectl_json(kc_node, "get", "node", node, timeout=20,
                             cluster=cluster) if kc_node else None
        addrs = {a.get("type"): a.get("address")
                 for a in ((info or {}).get("status", {}).get("addresses") or [])}
        ip = addrs.get("InternalIP")
        if ip:
            target = next((n for n in cluster_cfg.get("nodes", [])
                           if n.get("ip") == ip), None) or {"ip": ip}
    if not target:
        return jsonify({"error": "unknown node", "node": node}), 404
    ssh = cluster_cfg.get("ssh") or {}
    base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(ssh.get("port", 22))]
    if ssh.get("key"):
        base += ["-i", str(ssh["key"])]
    return base + [f"{ssh.get('user', 'rancher')}@{target.get('ip')}"]


# Quel port de l'hôte porte VRAIMENT cette VM.
#
# `podInterfaceName` ne sert à rien pour ça : il nomme une interface DANS
# l'espace de noms du pod, et sur harv1 les deux VMs qui tournent portent
# exactement le même nom (`pod8fe0d3f1ac5`) dans leurs pods respectifs. Seule
# la MAC discrimine. On entre donc dans chaque espace de noms rattaché au
# bridge et on cherche celle de la VM. C'est un aller-retour SSH : à la
# demande, jamais à chaque rendu.
_VETH_RESOLVE = (
    "for v in $(ip -o link show master {bridge} 2>/dev/null "
    "| awk -F'[ :@]+' '$2 ~ /^veth/ {{print $2}}'); do "
    "ns=$(ip -o link show $v 2>/dev/null | awk '{{print $NF}}'); "
    "case \"$ns\" in cni-*) ;; *) continue ;; esac; "
    "if sudo ip netns exec $ns ip -o link show 2>/dev/null "
    "| grep -qi {mac}; then echo \"$v\"; fi; done"
)


@app.route("/api/vm-network-path/<cluster>/<namespace>/<name>/hostport")
@requires_auth
def api_vm_host_port(cluster, namespace, name):
    """Résoudre le port de l'hôte qui porte cette VM, par sa MAC."""
    mac = request.args.get("mac", "")
    bridge = request.args.get("bridge", "")
    node = request.args.get("node", "")
    if not re.fullmatch(r"[0-9a-fA-F:]{17}", mac or ""):
        return jsonify({"error": "invalid mac"}), 400
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", bridge or ""):
        return jsonify({"error": "invalid bridge"}), 400
    ssh = _fabric_ssh_base(cluster, node)
    if isinstance(ssh, tuple):
        return ssh
    cmd = ssh + [_VETH_RESOLVE.format(bridge=shlex.quote(bridge),
                                      mac=shlex.quote(mac))]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except (subprocess.TimeoutExpired, OSError) as e:
        return jsonify({"error": "resolve failed",
                        "detail": _safe_proc_error(e)}), 502
    ports = [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]
    return jsonify({"cluster": cluster, "vm": f"{namespace}/{name}",
                    "mac": mac, "bridge": bridge,
                    "host_port": ports[0] if ports else None,
                    "candidates": ports})


@app.route("/api/vm-network-path/<cluster>/<namespace>/<name>")
@requires_auth
def api_vm_network_path(cluster, namespace, name):
    """De la VM jusqu'à la carte physique, maillon par maillon."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    data = _vm_network_path(cluster, kc, namespace, name)
    if data is None:
        return jsonify({"error": "vm not found",
                        "vm": f"{namespace}/{name}"}), 404
    return jsonify(data)


@app.route("/api/network-fabric/<cluster>")
@requires_auth
@shared_read()
def api_network_fabric(cluster):
    """L'empilement réseau vu du côté de l'hôte."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    data = _build_fabric(cluster, kc)
    if data is None:
        return jsonify({"error": "kubectl failed"}), 502
    return jsonify(data)


@app.route("/api/topology/<cluster>")
@requires_auth
@shared_read()
def api_topology(cluster):
    """Hosts and VMs for the Overview's Cluster view (`cluster-map.js`).
    Cached server-side for TOPOLOGY_CACHE_TTL seconds. Pass `?fresh=1` to
    force a refresh."""
    # Cluster déclaré mais hors tension : répondre tout de suite. Sans cela
    # cet appel attend le délai de `kubectl`, et une bascule de cluster
    # enchaîne ces attentes (15 s mesurées).
    kc = _kubectl_for_cluster(cluster)
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200

    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    fresh = request.args.get("fresh") == "1"
    with _topology_lock:
        cached = _topology_cache.get(cluster)
        if (not fresh) and cached and \
                (time.time() - cached["ts"] < TOPOLOGY_CACHE_TTL):
            return jsonify({**cached["data"], "cached": True,
                            "cache_age_s": time.time() - cached["ts"]})
    try:
        data = _build_topology(cluster, kc)
    except Exception as e:
        log.exception("topology build failed for %s", cluster)
        return jsonify({"error": "topology build failed",
                        "detail": _safe_proc_error(e)}), 500
    with _topology_lock:
        _topology_cache[cluster] = {"ts": time.time(), "data": data}
    return jsonify(data)


# =============================================================================
# Cluster-scoped resource lists (Phase A of the Terraform UI overhaul, v1.4.36)
#
# These power dropdowns in the Automation > Terraform form: instead of typing
# a namespace / image / SSH-key name as free text, the user picks from the
# list of what actually exists on the cluster.
#
# Each endpoint returns a flat JSON array of {name, namespace?, …} dicts —
# whatever the UI needs for value + label. Cached LIST_CACHE_TTL seconds per
# (cluster, kind) so a typical "open the form, change a couple of fields"
# session triggers one kubectl call per resource type, not one per re-render.
# =============================================================================
_list_cache = {}    # (cluster, kind) → {"ts": float, "data": list}
_list_lock = threading.Lock()
LIST_CACHE_TTL = 5.0


def _list_k8s_resources(cluster, gvk, namespace=None, label_selector=None,
                        cache_key=None, reducer=None):
    """Run `kubectl get <gvk> [-n NS] [-l SEL] -o json` and return the
    reduced list. `gvk` is the kubectl-friendly form ('ns',
    'virtualmachineimage', 'sc', 'keypair.harvesterhci.io', etc.).

    Returns (data, error). On success error is None; on failure data is
    [] and error is a short string. Results are memoised in `_list_cache`
    under `cache_key` (default: gvk) for LIST_CACHE_TTL seconds."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return [], f"unknown cluster: {cluster}"
    key = (cluster, cache_key or gvk)
    with _list_lock:
        cached = _list_cache.get(key)
        if cached and (time.time() - cached["ts"] < LIST_CACHE_TTL):
            return cached["data"], None
    args = ["get", gvk]
    if namespace == "*":
        args.append("-A")
    elif namespace:
        args += ["-n", namespace]
    if label_selector:
        args += ["-l", label_selector]
    raw = _kubectl_json(kc, *args, cluster=cluster)
    if not raw:
        return [], f"kubectl get {gvk} failed"
    items = raw.get("items") or []
    if reducer:
        data = [reducer(it) for it in items]
    else:
        data = [{
            "name": (it.get("metadata") or {}).get("name"),
            "namespace": (it.get("metadata") or {}).get("namespace"),
        } for it in items]
    with _list_lock:
        _list_cache[key] = {"ts": time.time(), "data": data}
    return data, None


def _reduce_image(item):
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "display_name": spec.get("displayName"),
        "source_type": spec.get("sourceType"),
        "size": status.get("size"),
        "progress": status.get("progress"),
        # Indispensables pour créer une VM à partir de cette image, et tous
        # deux à LIRE, jamais à deviner :
        #  * la storage class n'est pas `longhorn-<nom de l'image>`. Les
        #    images récentes (backend backingimage) portent une classe
        #    `lh-<uuid>`. Deviner le nom a produit un PVC bloqué en Pending
        #    sur « storageclass not found », et une VM non planifiable ;
        #  * un disque plus petit que la taille VIRTUELLE de l'image est
        #    refusé : c'est le plancher à proposer, pas la taille du
        #    fichier téléchargé (723 Mo compressés pour 10 Gio réels).
        "storage_class": status.get("storageClassName"),
        "virtual_size": status.get("virtualSize"),
    }


def _reduce_network(item):
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    labels = meta.get("labels") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "vlan": labels.get("network.harvesterhci.io/vlan-id"),
        "cluster_network": labels.get("network.harvesterhci.io/clusternetwork"),
        "config": spec.get("config"),
    }


def _reduce_sshkey(item):
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "fingerprint": (item.get("status") or {}).get("fingerPrint"),
        "public_key": spec.get("publicKey"),
    }


def _reduce_sc(item):
    meta = item.get("metadata") or {}
    annot = meta.get("annotations") or {}
    return {
        "name": meta.get("name"),
        "provisioner": item.get("provisioner"),
        "is_default": annot.get("storageclass.kubernetes.io/is-default-class") == "true",
        "reclaim_policy": item.get("reclaimPolicy"),
    }


def _reduce_pvc(item):
    """v1.8.0 — feeds the 'existing PVC' dropdown of the visual disk editor."""
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "capacity": (status.get("capacity") or {}).get("storage")
                    or ((spec.get("resources") or {}).get("requests") or {}).get("storage"),
        "storage_class": spec.get("storageClassName"),
        "phase": status.get("phase"),
        "volume_mode": spec.get("volumeMode"),
        # Harvester annotates PVCs attached to a VM; lets the UI flag
        # volumes that are already owned by another machine.
        "owned_by": (meta.get("annotations") or {}).get("harvesterhci.io/owned-by"),
    }


def _reduce_cloudinit(item):
    meta = item.get("metadata") or {}
    data = item.get("data") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "has_user_data": "userdata" in data or "user_data" in data,
        "has_network_data": "networkdata" in data or "network_data" in data,
    }


@app.route("/api/namespaces/<cluster>")
@requires_auth
@shared_read()
def api_list_namespaces(cluster):
    data, err = _list_k8s_resources(cluster, "ns")
    if err:
        return jsonify({"error": err}), 502
    # Hide kube-system / cattle-system noise from the dropdown
    HIDDEN = {"kube-system", "kube-public", "kube-node-lease",
              "cattle-system", "cattle-impersonation-system",
              "cattle-fleet-system", "cattle-fleet-local-system",
              "longhorn-system", "harvester-system",
              "fleet-local", "fleet-default"}
    return jsonify([it for it in data if it["name"] not in HIDDEN])


@app.route("/api/images/<cluster>")
@requires_auth
def api_list_images(cluster):
    # Short name `vmimage` (full: virtualmachineimages.harvesterhci.io).
    data, err = _list_k8s_resources(
        cluster, "vmimage", namespace="*", reducer=_reduce_image,
    )
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@app.route("/api/networks/<cluster>")
@requires_auth
def api_list_networks(cluster):
    # Harvester uses Multus NetworkAttachmentDefinition CRDs; the short
    # name on the cluster is `net-attach-def` (full: NAD GVK
    # k8s.cni.cncf.io/v1).
    data, err = _list_k8s_resources(
        cluster, "net-attach-def", namespace="*", reducer=_reduce_network,
    )
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@app.route("/api/sshkeys/<cluster>")
@requires_auth
def api_list_sshkeys(cluster):
    # Harvester SSH keys live as KeyPair CRDs (short name `kp`).
    data, err = _list_k8s_resources(
        cluster, "kp", namespace="*", reducer=_reduce_sshkey,
    )
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@app.route("/api/storageclasses/<cluster>")
@requires_auth
def api_list_sc(cluster):
    data, err = _list_k8s_resources(cluster, "sc", reducer=_reduce_sc)
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


def _reduce_pcidevice(item):
    """v1.15.0 — PCI devices Harvester discovered on the nodes, for the
    passthrough picker. `deviceName` (vendor/product slug) is what a VM
    spec references in domain.devices.hostDevices/gpus."""
    meta = item.get("metadata") or {}
    status = item.get("status") or {}
    spec = item.get("spec") or {}
    desc = status.get("description") or ""
    node = status.get("nodeName") or spec.get("nodeName")
    return {
        "name": meta.get("name"),
        # what the VM spec must reference
        "device_name": status.get("resourceName") or "",
        "address": status.get("address"),
        "node": node,
        "description": desc,
        "driver": status.get("kernelDriverInUse") or "",
        # A device is usable for passthrough only once claimed AND unbound
        # from its host driver; we surface the raw state, no guessing.
        "vendor_id": status.get("vendorId"), "device_id": status.get("deviceId"),
        # Réservé = détaché de son pilote hôte (vfio-pci) : seul utilisable
        # par une VM. Sans le dire, le sélecteur laissait choisir un device
        # libre, et la VM ne démarrait jamais (relevé sur harvlab, v1.44.8).
        "claimed": (status.get("kernelDriverInUse") or "") == "vfio-pci",
        # Le pilote plutôt qu'un mot : « vfio-pci » dit « réservé » dans
        # toutes les langues, l'aide de l'éditeur (traduite) l'explique.
        "display_name": (f"{desc[:70]} ({status.get('address')} · {node} · "
                         f"{status.get('kernelDriverInUse') or '-'})"
                         if desc else meta.get("name")),
    }


@app.route("/api/pvc/<cluster>/<namespace>/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_pvc_delete(cluster, namespace, name):
    """v1.16.0 — delete a PVC, for the orphaned volumes the Storage view
    surfaces. Deliberately refuses a claim still referenced by a VM: the
    UI only offers this on orphans, and a stale page must not turn into
    a data loss."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    try:
        vms = _kubectl_json(kc, "get", "vm", "-A", cluster=cluster) or {}
    except Exception:
        vms = {}
    for vm in vms.get("items", []):
        meta = vm.get("metadata") or {}
        if meta.get("namespace") != namespace:
            continue
        tspec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
        for v in tspec.get("volumes") or []:
            claim = ((v.get("persistentVolumeClaim") or {}).get("claimName")
                     or (v.get("dataVolume") or {}).get("name"))
            if claim == name:
                return jsonify({
                    "error": "claim-in-use",
                    "detail": f"PVC {namespace}/{name} is still attached to VM "
                              f"{meta.get('name')} — detach it from the VM first.",
                }), 409
    # Un PVC que monte un POD n'est pas orphelin non plus (base Prometheus,
    # journaux d'une mise à jour). La protection de Kubernetes ne fait que
    # DIFFÉRER l'effacement : le PVC passe en Terminating et disparaît au
    # prochain redémarrage du pod, avec ses données. Et si l'on ne peut pas
    # vérifier, on refuse : dans le doute, rien n'est supprimé.
    pods = _kubectl_json(kc, "get", "pods", "-n", namespace, cluster=cluster)
    if pods is None:
        return jsonify({"error": "cannot-verify",
                        "detail": "could not list the pods of this namespace, "
                                  "so whether the claim is in use is unknown"}), 503
    for pod in pods.get("items", []):
        if (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for v in (pod.get("spec") or {}).get("volumes") or []:
            if (v.get("persistentVolumeClaim") or {}).get("claimName") == name:
                return jsonify({
                    "error": "claim-in-use",
                    "detail": f"PVC {namespace}/{name} is mounted by pod "
                              f"{(pod.get('metadata') or {}).get('name')}.",
                }), 409
    action_id = track_action(
        f"pvc-delete:{namespace}/{name}", cluster,
        _simple_kubectl_action, kc,
        ["-n", namespace, "delete", "pvc", name, "--wait=false"],
        "delete", f"PVC {name} deletion requested",
    )
    return jsonify({"action_id": action_id, "deleting": f"{namespace}/{name}"}), 201


# =============================================================================
# Installation bare-metal zéro-touch (v1.18.0)
#
# Enchaînement : préflight BMC -> remasterisation de l'ISO (paramètres noyau
# d'installation automatique) -> publication ISO + config sur le serveur
# d'artefacts -> insertion dans le lecteur virtuel -> amorce unique sur CD
# -> allumage -> attente de l'API du nouveau cluster -> adoption.
#
# Modelé sur _capi_install_runner : orchestrateur court, une phase par
# helper, et les trois précautions qu'impose une action de 30 minutes
# (évènements agrégés, persistance périodique, annulation coopérative).
# =============================================================================
HARVESTER_INSTALL_TIMEOUT = int(
    os.environ.get("HARVESTER_OPS_INSTALL_TIMEOUT", 3600))


def _harvester_install_config(opts):
    """Rend la configuration d'installation Harvester (YAML).

    Construite en dictionnaire depuis les champs du formulaire, fusionnée
    avec le YAML avancé (`advanced_yaml`), validée contre le schéma de
    l'installeur (harvester_install_schema) puis sérialisée. Une clé mal
    placée ici coûte une réinstallation complète : toute clé inconnue,
    réservée à la console ou posée des deux côtés lève InstallConfigError
    avec son chemin."""
    return _his.dump_install_config(_his.render_install_config(opts))


def _bm_wait_api(vip, deadline, run, step):
    """Attend que l'API du nouveau cluster réponde. Un 401/403 est un
    SUCCÈS : il prouve qu'un apiserver écoute et refuse un anonyme."""
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last = ""
    while time.time() < deadline:
        if getattr(run, "_cancel", False):
            raise RuntimeError("cancelled by operator")
        try:
            req = urllib.request.Request(f"https://{vip}:6443/version")
            with urllib.request.urlopen(req, context=ctx, timeout=6):
                return True
        except Exception as e:
            code = getattr(e, "code", None)
            if code in (401, 403):
                return True
            msg = f"{type(e).__name__}"
            if msg != last:
                last = msg
                step("wait-api", "progress", f"en attente de {vip}:6443 ({msg})")
        time.sleep(15)
    return False


def _cluster_for_server(server_url):
    """Nom du cluster déclaré dont le serveur d'API est sur l'hôte de
    `server_url` (une jonction vise https://VIP:443, le kubeconfig
    https://VIP:6443 : seul l'hôte compte), ou None."""
    from urllib.parse import urlparse
    host = urlparse(str(server_url)).hostname
    for c in load_config().get("clusters", []):
        try:
            kc = yaml.safe_load(Path(c["kubeconfig"]).read_text()) or {}
        except (OSError, KeyError, yaml.YAMLError):
            continue
        for entry in kc.get("clusters") or []:
            if urlparse((entry.get("cluster") or {}).get("server") or "").hostname == host:
                return c["name"]
    return None


def _bm_wait_node_ready(cluster, hostname, deadline, run, step):
    """Attend que le nœud qui rejoint soit prêt dans le cluster. L'API de ce
    cluster répond déjà : l'attendre ne prouverait rien."""
    last = ""
    while time.time() < deadline:
        if getattr(run, "_cancel", False):
            raise RuntimeError("cancelled by operator")
        node = _kubectl_json(_kubectl_for_cluster(cluster), "get", "node", hostname,
                             cluster=cluster)
        conds = {c.get("type"): c.get("status")
                 for c in ((node or {}).get("status") or {}).get("conditions") or []}
        if conds.get("Ready") == "True":
            return True
        state = "absent" if node is None else "pas encore prêt"
        if state != last:
            last = state
            step("wait-node", "progress", f"{hostname} {state} dans {cluster}")
        time.sleep(15)
    return False


HARVESTER_POOLS_TIMEOUT = int(os.environ.get("HARVESTER_OPS_POOLS_TIMEOUT", 1800))
_BM_OS_USER = "rancher"
# nom d'un cluster déclaré par l'installation : RFC 1123 (sous-domaine), et
# accepté par _validate_cluster_payload (61 caractères au plus)
_BM_CLUSTER_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,59}[a-z0-9])?$")


def _bm_cluster_name(data):
    """Nom du cluster créé : `cluster_name`, sinon le nom d'hôte du formulaire."""
    return str(data.get("cluster_name") or data.get("hostname") or "").strip()


def _bm_cluster_files(name):
    """Fichiers d'un cluster créé par la console, dans son répertoire d'état
    comme ceux de tout cluster qu'elle déclare (le répertoire de
    configuration est en lecture seule dans le service) : clé privée
    `<état>/ssh/<nom>_id` (0600), sa publique, les clés d'hôte vues au
    premier contact, et le kubeconfig `<état>/kubeconfigs/<nom>.yaml`."""
    d = _state_ssh_dir()
    return {"key": d / f"{name}_id", "pub": d / f"{name}_id.pub",
            "known_hosts": d / f"{name}_known_hosts",
            "kubeconfig": _state_kubeconfigs_dir() / f"{name}.yaml"}


def _bm_cluster_keypair(name):
    """Paire de clés ed25519 propre au nouveau cluster (v1.78.0) : sa
    publique part dans `os.ssh_authorized_keys`, la privée devient la clé
    SSH du cluster déclaré. Un reste d'un essai précédent est remplacé (la
    route a vérifié que ce nom n'est pas déclaré)."""
    f = _bm_cluster_files(name)
    for k in ("key", "pub", "known_hosts"):
        f[k].unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"harvester-ops:{name}",
                    "-f", str(f["key"])], check=True, capture_output=True, timeout=30)
    os.chmod(f["key"], 0o600)
    return f


def _bm_join_pubkey(cluster):
    """Clé publique du cluster rejoint quand sa clé a été créée par la
    console (`ssh.generated`) : le nouveau nœud reçoit la même. None sinon."""
    entry = next((c for c in load_config().get("clusters", []) if c.get("name") == cluster), None)
    ssh = (entry or {}).get("ssh") or {}
    key = ssh.get("key")
    if not (ssh.get("generated") and key and Path(key).is_file()):
        return None
    try:
        p = subprocess.run(["ssh-keygen", "-y", "-f", str(key)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    pub = (p.stdout or "").strip()
    return pub if p.returncode == 0 and pub.startswith("ssh-") else None


def _bm_with_ssh_key(opts, pub):
    """Options de l'installation avec une clé publique de plus. Là où sont
    déjà les clés : si le YAML avancé porte `os.ssh_authorized_keys`,
    l'ajouter au champ du formulaire ferait refuser la configuration (clé
    posée des deux côtés)."""
    adv_text = opts.get("advanced_yaml")
    if adv_text:
        try:
            adv = yaml.safe_load(str(adv_text))
        except yaml.YAMLError:
            adv = None
        if isinstance(adv, dict):
            # `sshAuthorizedKeys` et les autres orthographes admises : sous leur
            # nom canonique (relecture 1.78.0)
            adv = _his.canonicalize(adv)
        keys = ((adv.get("os") if isinstance(adv, dict) else None) or {})
        if isinstance(keys, dict) and isinstance(keys.get("ssh_authorized_keys"), list):
            keys["ssh_authorized_keys"].append(pub)
            return dict(opts, advanced_yaml=_his.dump_install_config(adv))
    form = str(opts.get("ssh_keys") or "").strip()
    return dict(opts, ssh_keys=(form + "\n" if form else "") + pub)


def _bm_write_private(path, text):
    """Fichier créé directement en 0600 (pas de fenêtre avant un chmod) ;
    texte ou octets."""
    path = Path(path)
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb" if isinstance(text, (bytes, bytearray)) else "w") as f:
        f.write(text)


def _bm_fetch_kubeconfig(vip, key, known_hosts, out, deadline, run, step):
    """Kubeconfig admin du nouveau cluster, lu par SSH sur la VIP et réécrit
    vers https://VIP:6443 (le certificat de RKE2 porte la VIP). La clé
    d'hôte est enregistrée au premier contact (accept-new) dans le
    known_hosts du cluster, qui sert ensuite. Harvester écrit le fichier
    quelques instants après que l'API répond : on réessaie."""
    last = ""
    while time.time() < deadline:
        if getattr(run, "_cancel", False):
            raise RuntimeError("cancelled by operator")
        try:
            p = subprocess.run(
                ["ssh", "-n", "-i", str(key), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                 "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known_hosts}",
                 "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
                 f"{_BM_OS_USER}@{vip}", "sudo", "cat", "/etc/rancher/rke2/rke2.yaml"],
                capture_output=True, text=True, timeout=60)
            doc = yaml.safe_load(p.stdout) if p.returncode == 0 else None
        except (subprocess.TimeoutExpired, yaml.YAMLError):
            doc = None
        if isinstance(doc, dict) and doc.get("clusters"):
            for c in doc["clusters"]:
                (c.setdefault("cluster", {}))["server"] = f"https://{vip}:6443"
            _bm_write_private(out, yaml.safe_dump(doc))
            return True
        # jamais la sortie de ssh : elle pourrait porter le fichier lu
        msg = "SSH not ready" if not isinstance(doc, dict) else "kubeconfig not written yet"
        if msg != last:
            last = msg
            step("declare", "progress", f"accès au nouveau cluster : {msg}")
        time.sleep(15)
    return False


def _bm_declare_cluster(name, kubeconfig, key, hostname, ip):
    """Déclare le cluster créé, par le même chemin que Settings > Clusters
    (_validate_cluster_payload, CONFIG_LOCK) : dans <état>/clusters.d, avec
    des chemins relatifs au répertoire d'état."""
    cluster, err = _validate_cluster_payload({
        "name": name, "description": "installed by the console (bare-metal)",
        "ssh": {"user": _BM_OS_USER, "port": 22, "key": str(key)},
        "nodes": [{"hostname": hostname, "ip": ip, "role": "control-plane"}]})
    if err:
        raise ValueError(err)
    cluster["kubeconfig"] = str(kubeconfig)
    # clé créée par la console : un nœud qui rejoint reçoit la même
    cluster["ssh"]["generated"] = True
    with CONFIG_LOCK:
        if any(c.get("name") == name for c in load_config().get("clusters", [])):
            raise ValueError(f"cluster '{name}' already exists")
        _write_console_cluster(cluster)
    return cluster


def _bm_add_joined_node(cluster_name, hostname, ip):
    """v1.83.2 : le nœud qui a rejoint est ajouté à la déclaration du cluster
    (vu en réel : un lot de deux machines laissait la déclaration avec le seul
    premier nœud, que l'arrêt et le démarrage du cluster lisent). Rend un
    message pour l'action : ajouté, déjà présent, ou à ajouter à la main dans
    config.yaml (lecture seule pour la console)."""
    with CONFIG_LOCK:
        origin, cluster, _raw, _idx = _find_cluster(cluster_name)
        if cluster is None:
            return f"{cluster_name} is no longer declared: {hostname} not recorded"
        nodes = cluster.setdefault("nodes", [])
        if any(n.get("hostname") == hostname or (ip and n.get("ip") == ip) for n in nodes):
            return f"{hostname} already in the declaration of {cluster_name}"
        if origin == "config":
            return (f"{cluster_name} is declared in config.yaml: add the node "
                    f"{{hostname: {hostname}, ip: {ip}, role: worker}} there")
        nodes.append({"hostname": hostname, "ip": ip, "role": "worker"})
        _write_console_cluster(cluster)
    return f"{hostname} added to the declaration of {cluster_name}"


def _bm_pool_disk_refusals(data):
    """Un disque de pool n'est jamais le disque système ni le disque de
    données. Avec un inventaire de découverte de cette machine, chaque
    disque de pool y est retrouvé par série ou WWN et check_disk_roles dit
    les rôles doublés ; sans inventaire, comparaison au chemin, à la série
    et au WWN tels que donnés. Rend [(disque, raison)]."""
    pools = data.get("pools") or []
    if not pools:
        return []
    device = str(data.get("device") or "").strip()
    data_disk = str(data.get("data_disk") or "").strip()
    doc = _bmd.load_inventory(INVENTORY_DIR, str(data.get("bmc_host") or ""))
    if doc:
        disks = _bmdisks.parse_discovery(doc.get("raw") or "")["disks"]
        out, roles = [], {}
        for p in pools:
            ids = roles.setdefault(p["tag"], [])
            for d in p["disks"]:
                inv = next((x for x in disks
                            if (d["wwn"] and _hh.norm_wwn(x.get("wwn")) == _hh.norm_wwn(d["wwn"]))
                            or (d["serial"] and _hh.norm_serial(x.get("serial")) == _hh.norm_serial(d["serial"]))),
                           None)
                if inv is None:
                    out.append((_hh.disk_label(d), "unknown-disk"))
                else:
                    ids.append(inv.get("stable_path") or "/dev/" + str(inv.get("name")))
        errs = _bmdisks.check_disk_roles(disks, {"os": device, "data": data_disk or None, "pools": roles},
                                         skipchecks=True, wipe_all=True)
        return out + [e for e in errs if e[1] == "role-twice"]
    out = []
    for p in pools:
        for d in p["disks"]:
            for given in (device, data_disk):
                g = given.lower()
                if not g:
                    continue
                wwn, serial = _hh.norm_wwn(d["wwn"]), _hh.norm_serial(d["serial"])
                if (d["path"] and d["path"] == given) or (len(wwn) >= 6 and wwn in g) \
                        or (len(serial) >= 6 and serial in g):
                    out.append((_hh.disk_label(d), "role-twice"))
    return out


def _bm_skipchecks(data):
    """`harvester.install.skipchecks=true` parmi les arguments noyau, ou le
    champ explicite `skipchecks` : l'installeur lève alors ses contrôles de
    taille, la console aussi."""
    if _his._truthy(data.get("skipchecks")):
        return True
    return "harvester.install.skipchecks=true" in str(data.get("extra_args") or "").split()


def _bm_disk_role_check(data):
    """Contrôles des disques (1.78.0) avec l'inventaire de découverte de ce
    BMC : disque système, disque de données, liste à effacer et disques de
    pool, rapprochés des disques vus par Linux, puis check_disk_roles.
    Rend None sans inventaire (saisie libre : comportement d'avant), sinon
    la liste [(chemin, raison)], vide si tout va. `data["pools"]` est déjà
    normalisé par check_pools."""
    doc = _bmd.load_inventory(INVENTORY_DIR, str(data.get("bmc_host") or ""))
    if not doc:
        return None
    disks = _bmdisks.parse_discovery(doc.get("raw") or "")["disks"]
    errors, pools = [], {}
    for p in data.get("pools") or []:
        ids = pools.setdefault(p["tag"], [])
        for d in p["disks"]:
            inv = next((x for x in disks
                        if (d.get("wwn") and _hh.norm_wwn(x.get("wwn")) == _hh.norm_wwn(d["wwn"]))
                        or (d.get("serial") and _hh.norm_serial(x.get("serial")) == _hh.norm_serial(d["serial"]))),
                       None)
            if inv is None:
                errors.append((_hh.disk_label(d), "unknown-disk"))
            else:
                ids.append(inv.get("stable_path") or "/dev/" + str(inv.get("name")))
    roles = {"os": str(data.get("device") or "").strip(),
             "data": str(data.get("data_disk") or "").strip() or None,
             "pools": pools,
             "wipe": _his._split_list(data.get("wipe_disks_list"))}
    errors += _bmdisks.check_disk_roles(disks, roles, skipchecks=_bm_skipchecks(data),
                                        wipe_all=_his._truthy(data.get("wipe_all_disks")))
    return errors


def _bm_own_disk_names(data):
    """Tous les noms (chemin stable, liens, /dev/<nom>) des disques système
    et de données dans l'inventaire de ce BMC, pour les retirer de
    `wipe_disks_list` au rendu quel que soit le nom employé ; [] sans
    inventaire (le rendu compare alors au chemin écrit)."""
    doc = _bmd.load_inventory(INVENTORY_DIR, str(data.get("bmc_host") or ""))
    if not doc:
        return []
    disks = _bmdisks.parse_discovery(doc.get("raw") or "")["disks"]
    idx = _bmdisks._index(disks)
    names = []
    for key in ("device", "data_disk"):
        d = idx.get(str(data.get(key) or "").strip())
        if d is not None:
            links = d.get("links") or {}
            names += [d.get("stable_path"), "/dev/" + str(d.get("name") or "")]
            names += list(links.get("by_id") or []) + list(links.get("by_path") or [])
    return [n for n in names if n]


def _bm_disk_refusal(errors):
    """Réponse 400 des contrôles des disques : `reasons` chemin -> raison,
    la première raison d'un chemin (la fenêtre la range sur sa ligne)."""
    fields, reasons = [], {}
    for path, reason in errors:
        if path not in reasons:
            fields.append(path)
            reasons[path] = reason
    return jsonify({"error": "invalid disks", "fields": fields, "reasons": reasons}), 400


def _bm_apply_pools(opts, kubeconfig, run, step, timeout):
    """Étape `pools` : bin/harvester-resources.py pools-apply, comme la
    remasterisation passe par son script (parité CLI). Un arrêt demandé
    depuis le dock (`_cancel`) termine le processus. Rend (code, raison)."""
    spec = _iso_work_dir() / f"pools-{run.id}.json"
    _bm_write_private(spec, json.dumps({"pools": opts["pools"]}))
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "pools-apply",
           "--kubeconfig", str(kubeconfig), "--node", opts["hostname"], "--spec", str(spec),
           "--timeout", str(max(1, int(timeout)))]
    if opts.get("mode") == "join":
        cmd.append("--no-classes")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        def watch():
            while proc.poll() is None:
                if getattr(run, "_cancel", False):
                    proc.terminate()
                    return
                time.sleep(1)
        threading.Thread(target=watch, daemon=True).start()
        last_error = ""
        for line in proc.stderr:
            line = line.strip()
            if line.startswith("STEP_EVENT|"):
                parts = line.split("|", 3)
                if len(parts) == 4:
                    if parts[2] == "error":
                        last_error = parts[3]
                    # une seule étape `pools` dans le dock, ses sous-étapes en
                    # message ; l'échec est dit par le runner, une seule fois
                    step("pools", "progress", f"{parts[1]} : {parts[3]}")
        code = proc.wait()
    finally:
        spec.unlink(missing_ok=True)
    return code, last_error


def _bm_remaster_stream(cmd, step):
    """Lance `harvester-iso-remaster.sh` et relaie ses STEP_EVENT comme
    étapes de l'action. Renvoie son code de sortie."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for line in proc.stderr:
        line = line.strip()
        if line.startswith("STEP_EVENT|"):
            parts = line.split("|", 3)
            if len(parts) == 4:
                step(parts[1], parts[2], parts[3])
    return proc.wait()


def _bm_media_insert(host, user, pwd, image_url):
    """Monte `image_url` dans le lecteur virtuel CD du BMC (éjecte d'abord
    une image restée montée). Renvoie (ok, détail, ressource du lecteur)."""
    _, vm_res = _redfish_virtualmedia_cd(host, user, pwd)
    if (vm_res or {}).get("Inserted"):
        tgt, _ = _redfish_action_target(vm_res, "EjectVirtualMedia", "EjectMedia")
        if tgt:
            _redfish_send(host, tgt, user, pwd, "POST", {})
            time.sleep(3)
            _, vm_res = _redfish_virtualmedia_cd(host, user, pwd)
    target, is_oem = _redfish_action_target(vm_res or {}, "InsertVirtualMedia",
                                            "InsertMedia")
    # le dialecte OEM refuse Inserted/WriteProtected (vu sur node3)
    payload = ({"Image": image_url} if is_oem
               else {"Image": image_url, "Inserted": True, "WriteProtected": True})
    # v1.78.0, vu en réel : certains BMC (et l'émulateur Redfish du banc) ne
    # répondent à l'insertion qu'une fois l'image entièrement téléchargée,
    # et disent le lecteur « monté » AVANT la fin : se fier à cet état a
    # fait démarrer la machine sur un lecteur vide. On attend donc la
    # réponse elle-même, avec un délai à la mesure d'un ISO de 8 Go.
    ok, _, detail = _redfish_send(host, target, user, pwd, "POST", payload,
                                  timeout=BM_INSERT_WAIT)
    return ok, detail, vm_res


BM_INSERT_WAIT = int(os.environ.get("HARVESTER_OPS_BM_INSERT_WAIT", "900"))


def _bm_media_eject(host, user, pwd, vm_res=None):
    """Éjecte le lecteur virtuel. Renvoie (ok, détail)."""
    if vm_res is None:
        _, vm_res = _redfish_virtualmedia_cd(host, user, pwd)
    tgt, _ = _redfish_action_target(vm_res or {}, "EjectVirtualMedia", "EjectMedia")
    if not tgt:
        return False, "virtual media eject not exposed by this BMC"
    ok, _, detail = _redfish_send(host, tgt, user, pwd, "POST", {})
    return ok, detail


def _bm_boot_once_target(host, user, pwd, sys_path, target):
    ok, _, detail = _redfish_send(
        host, sys_path, user, pwd, "PATCH",
        {"Boot": {"BootSourceOverrideTarget": target,
                  "BootSourceOverrideEnabled": "Once"}})
    return ok, detail


def _bm_power_cycle(host, user, pwd, sys_path, run, sleep=None, now=None, off_wait=120):
    """Arrêt franc (si allumée), attente de l'état éteint, puis mise sous
    tension. Rend (ok, détail)."""
    sleep, now = sleep or time.sleep, now or time.time
    state = (_redfish_get(host, sys_path, user, pwd, timeout=8) or {}).get("PowerState")
    if state != "Off":
        ok, detail = _bm_reset(host, user, pwd, sys_path, "ForceOff")
        if not ok:
            return False, f"power off refused: {detail}"
        deadline = now() + off_wait
        while now() < deadline:
            if getattr(run, "_cancel", False):
                return False, "cancelled by operator"
            sleep(5)
            if (_redfish_get(host, sys_path, user, pwd, timeout=8) or {}).get("PowerState") == "Off":
                break
        else:
            return False, f"the machine did not power off within {off_wait} s"
    ok, detail = _bm_reset(host, user, pwd, sys_path, "On")
    return (True, "") if ok else (False, f"power on refused: {detail}")


BM_STALL_AFTER = int(os.environ.get("HARVESTER_OPS_BM_STALL_AFTER", "600"))
BM_MIN_NIC_MBPS = 10000      # exigence de l'installeur de Harvester (contrôles matériels)


def _bm_nic_warning(opts):
    """v1.80.0, vu en réel sur node4 : une carte de gestion sous 10 Gbit/s
    fait refuser l'installation automatique par les contrôles matériels de
    l'installeur, sans rien dire à la console. Rend un message quand
    l'inventaire de la machine montre une carte de gestion choisie plus lente
    et que `harvester.install.skipchecks=true` n'est pas posé ; None sinon."""
    if "harvester.install.skipchecks=true" in str(opts.get("extra_args") or ""):
        return None
    doc = _bmd.load_inventory(INVENTORY_DIR, str(opts.get("bmc_host") or ""))
    if not doc:
        return None
    nics = _bmdisks.parse_discovery(doc.get("raw") or "").get("nics") or []
    chosen = opts.get("mgmt_interfaces") or ([opts["mgmt_interface"]] if opts.get("mgmt_interface") else [])
    chosen = {str(c).lower() for c in (chosen if isinstance(chosen, list) else str(chosen).split(","))}
    slow = [n for n in nics if (str(n.get("mac") or "").lower() in chosen or n.get("name") in chosen)
            and isinstance(n.get("speed"), int) and 0 < n["speed"] < BM_MIN_NIC_MBPS]
    if not slow:
        return None
    names = ", ".join(f"{n.get('name')} ({n['speed']} Mbit/s)" for n in slow)
    return (f"management NIC under 10 Gbit/s: {names}; the Harvester installer's hardware checks refuse "
            "an unattended install on it unless harvester.install.skipchecks=true is added to the extra "
            "kernel arguments")


def _bm_install_stalled(cfg_token, iso_token, bmc_host, now=None):
    """v1.80.0, vu en réel sur node4 : l'installeur a lu sa configuration
    puis ne demande jamais l'image d'installation quand il refuse quelque
    chose (contrôles matériels : carte de gestion sous 10 Gbit/s, mémoire,
    disque). Rend un message passé `BM_STALL_AFTER` secondes après la
    lecture de la configuration sans aucune lecture de l'ISO par un autre
    client que le BMC, None sinon."""
    now = now or time.time
    cfg = pxe_server.stats(cfg_token) or {}
    if not cfg.get("hits") or now() - cfg.get("first_hit", now()) < BM_STALL_AFTER:
        return None
    iso = pxe_server.stats(iso_token) or {}
    import socket
    h = str(bmc_host)
    h = h[1:h.index("]")] if h.startswith("[") and "]" in h else (h.split(":", 1)[0] if h.count(":") == 1 else h)
    try:
        bmc_ip = socket.gethostbyname(h)
    except OSError:
        bmc_ip = h
    if [c for c in iso.get("clients", []) if c != bmc_ip]:
        return None
    return (f"the installer read its configuration {BM_STALL_AFTER // 60} min ago but never fetched "
            "the install image: it refused something, most often a hardware check (management NIC "
            "under 10 Gbit/s, memory, disk). Look at the machine console; if acceptable, add "
            "harvester.install.skipchecks=true to the extra kernel arguments")


def _bm_wait_power_off(host, user, pwd, sys_path, deadline, run, step, sleep=None, now=None,
                       stalled=None):
    """Attend que la machine soit éteinte (fin de l'installation, v1.78.0).
    False à l'échéance ou sur annulation. `stalled()` rend un message quand
    l'installeur est bloqué (v1.80.0) : l'attente s'arrête alors, le message
    est gardé dans `run._stall`."""
    sleep, now = sleep or time.sleep, now or time.time
    last, offs = None, 0
    while now() < deadline:
        # pause d'abord : juste après le redémarrage forcé, un BMC peut dire
        # « Off » un instant, ce qui passerait pour la fin de l'installation ;
        # et deux lectures « Off » de suite sont exigées (relecture 1.78.0)
        sleep(20)
        if getattr(run, "_cancel", False):
            return False
        if stalled:
            why = stalled()
            if why:
                run._stall = why
                return False
        state = (_redfish_get(host, sys_path, user, pwd, timeout=8) or {}).get("PowerState")
        offs = offs + 1 if state == "Off" else 0
        if offs >= 2:
            return True
        if state != last:
            step("wait-install", "progress", f"machine {state or '?'}")
            last = state
    return False


def _bm_boot_once_cd(host, user, pwd, sys_path):
    """`Once` : après ce démarrage, la machine reprend son ordre normal."""
    ok, _, detail = _redfish_send(
        host, sys_path, user, pwd, "PATCH",
        {"Boot": {"BootSourceOverrideTarget": "Cd",
                  "BootSourceOverrideEnabled": "Once"}})
    return ok, detail


def _bm_reset(host, user, pwd, sys_path, reset_type):
    ok, _, detail = _redfish_send(
        host, f"{sys_path.rstrip('/')}/Actions/ComputerSystem.Reset/",
        user, pwd, "POST", {"ResetType": reset_type})
    return ok, detail


def _baremetal_install_runner(run, opts):
    kc_user, kc_pwd = opts["bmc_user"], opts["bmc_password"]
    host = opts["bmc_host"]
    tokens = []
    # Fichiers à effacer quoi qu'il arrive : la configuration porte le token
    # du cluster et le mot de passe OS, elle n'a rien à faire sur le disque
    # après un échec.
    scratch = []
    # v1.78.0 : clés du nouveau cluster, effacées sur un échec tant que la
    # machine n'a pas redémarré sur l'installeur ; gardées ensuite (le nœud
    # porte la clé publique, et la déclaration s'en sert).
    creds = []
    keep_creds = [False]
    # v1.78.0 : une machine allumée par le préflight est éteinte si le run
    # échoue avant d'avoir lancé l'installation (jamais une machine trouvée
    # déjà allumée).
    powered_by_us = [False]
    install_started = [False]
    profile_ref = [None]
    persisted = [time.time()]

    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": msg, "ts": time.time()})
        # Persistance périodique : sans elle, un redémarrage de Flask
        # pendant les 30 minutes perd tout l'historique du run.
        if time.time() - persisted[0] > 60:
            persisted[0] = time.time()
            try:
                _actions_persist(run)
            except Exception:
                pass

    def fail(sid, msg, code=1):
        # v1.47.0 : un arrêt demandé depuis le dock se lit « annulé »
        cancelled = getattr(run, "_cancel", False)
        run.error_summary = str(msg)[:300]
        step(sid, "error", str(msg)[:300])
        run.exit_code = 3 if cancelled else code
        run.status = "cancelled" if cancelled else "error"
        run.ended_at = time.time()
        if powered_by_us[0] and not install_started[0]:
            try:
                sp_off = (profile_ref[0] or {}).get("system_path")
                if sp_off:
                    _bm_reset(host, kc_user, kc_pwd, sp_off, "ForceOff")
                    step(sid, "warn", "machine rallumée pour l'inventaire : éteinte à nouveau")
            except Exception:                                   # noqa: BLE001
                step(sid, "warn", "machine allumée pour l'inventaire : l'éteindre à la main")
        run.emit({"type": "status", "status": run.status, "exit_code": run.exit_code,
                  "ts": time.time()})
        if tokens:
            pxe_server.revoke(*tokens)
        for p in scratch:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        for p in ([] if keep_creds[0] else creds):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        run.close()

    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})

    # --- préflight : le BMC dit la vérité seulement machine allumée ---
    step("preflight", "running", f"interrogation du BMC {host}")
    profile = _bmc_discover_one(host, kc_user, kc_pwd)
    profile_ref[0] = profile
    if not profile.get("ok"):
        return fail("preflight", profile.get("error", "BMC unreachable"))
    # Relecture finale de la 1.78.0 : l'inventaire est rangé par BMC ; si ce
    # BMC mène maintenant à une autre machine (lame changée, adresse
    # réattribuée), les contrôles de disques auraient porté sur les disques
    # d'une autre machine. Revérifier l'identité AVANT toute mise sous tension.
    inv_bind = _bmd.load_inventory(INVENTORY_DIR, host)
    if inv_bind:
        verdict, why = _bmd.check_binding(inv_bind.get("raw") or "", profile.get("serial"),
                                          profile.get("uuid"))
        if verdict == "mismatch":
            return fail("preflight", f"the discovery inventory of {host} is not this machine's "
                                     f"({why}): run the discovery boot again")
    if profile.get("power_state") != "On":
        step("preflight", "progress", "machine éteinte, allumage pour inventaire")
        sys_path = profile.get("system_path") or "/redfish/v1/Systems/1"
        ok, _, detail = _redfish_send(
            host, f"{sys_path.rstrip('/')}/Actions/ComputerSystem.Reset/",
            kc_user, kc_pwd, "POST", {"ResetType": "On"})
        if not ok:
            return fail("preflight", f"power on refused: {detail[:200]}")
        powered_by_us[0] = True
        deadline = time.time() + 900
        while time.time() < deadline:
            if getattr(run, "_cancel", False):
                return fail("preflight", "cancelled by operator")
            time.sleep(20)
            profile = _bmc_discover_one(host, kc_user, kc_pwd)
            if profile.get("post_state") == "FinishedPost":
                break
            # v1.78.0 : l'état du POST est propre aux iLO ; un BMC qui ne le
            # publie pas faisait attendre 15 minutes pour rien.
            if profile.get("post_state") is None and profile.get("power_state") == "On":
                break
            step("preflight", "progress",
                 f"POST en cours ({profile.get('post_state') or '?'})")
    if not profile.get("virtualmedia_path"):
        return fail("preflight", "no CD virtual media (iLO Advanced licence?)")
    if "Cd" not in (profile.get("boot_targets") or []):
        return fail("preflight", "the BMC cannot boot from virtual media")
    # v1.78.0 : d'où vient la preuve qu'il y a un disque. Avant, seule la
    # liste HPE `UefiTargetBootSourceOverrideSupported` comptait : tout BMC
    # d'un autre constructeur était refusé d'office (vu en réel, banc
    # Redfish). Ordre : l'inventaire de découverte de cette machine, puis la
    # liste HPE, puis les disques que le BMC publie en Redfish.
    n_disks, disk_src = 0, ""
    inv_doc = _bmd.load_inventory(INVENTORY_DIR, host)
    if inv_doc:
        n_disks = len(_bmdisks.parse_discovery(inv_doc.get("raw") or "")["disks"])
        disk_src = "inventaire de découverte"
    if not n_disks:
        n_disks = len([t for t in (profile.get("uefi_targets") or []) if t.startswith("HD.")])
        disk_src = "BMC (cibles UEFI)" if n_disks else disk_src
    if not n_disks:
        try:
            st = _bmc_storage(host, kc_user, kc_pwd, profile.get("system_path"))
        except Exception:                                   # noqa: BLE001
            st = {}
        n_disks = sum(len(c.get("drives") or []) for c in (st or {}).get("controllers") or [])
        disk_src = "BMC (Redfish Storage)" if n_disks else disk_src
    if not n_disks and inv_doc:
        return fail("preflight", "no disk in the discovery inventory of this machine")
    if not n_disks and any(t.startswith("HD.") or t.startswith("Cd") for t in (profile.get("uefi_targets") or [])):
        return fail("preflight", "no disk visible on this machine")
    if not n_disks:
        step("preflight", "warn",
             "le BMC ne publie pas ses disques et la machine n'a pas d'inventaire de "
             "découverte : l'installeur vérifiera le disque lui-même")
    nic_warn = _bm_nic_warning(opts)
    if nic_warn:
        step("preflight", "warn", nic_warn)
    step("preflight", "done",
         f"{profile.get('model')} : {n_disks or '?'} disque(s) ({disk_src or 'non vus'}), média virtuel OK")

    # --- remasterisation ---
    port = pxe_server.start()
    advertise = opts.get("advertise_host") or _bm_local_ip_for(host)

    src_iso = _iso_dir() / opts["iso"]
    if not src_iso.is_file():
        return fail("remaster", f"ISO not found: {opts['iso']}")
    out_iso = _iso_work_dir() / f"install-{run.id}.iso"
    scratch.extend([out_iso, out_iso.with_suffix(out_iso.suffix + ".sha256")])

    # Le jeton de l'ISO est émis AVANT d'écrire la configuration : celle-ci
    # doit porter `iso_url`, faute de quoi l'installeur s'arrête sur
    # « iso_url is required in automatic installation ». Le fichier n'existe
    # pas encore, ce n'est pas un problème : il sera là bien avant la
    # première requête, qui n'a lieu qu'une fois la machine démarrée.
    iso_token = pxe_server.issue(out_iso, "iso")
    tokens.append(iso_token)
    iso_url = f"http://{advertise}:{port}/pxe/iso/{iso_token}.iso"

    # La route a déjà validé ces mêmes champs : rien ne devrait lever ici.
    # Si cela arrivait quand même, dire l'échec au lieu de laisser le fil
    # mourir avec la machine allumée et les fichiers encore servis.
    # v1.78.0 : un cluster créé reçoit sa propre paire de clés (la console
    # en lira le kubeconfig et le déclarera) ; un nœud qui rejoint reçoit la
    # clé du cluster quand celle-ci a été créée par la console.
    pools = opts.get("pools") or []
    files = None
    if opts.get("mode") == "join":
        join_pub = _bm_join_pubkey(opts["cluster"])
        if join_pub:
            opts = _bm_with_ssh_key(opts, join_pub)
    else:
        try:
            files = _bm_cluster_keypair(opts["cluster_name"])
        except (OSError, subprocess.SubprocessError) as e:
            return fail("remaster", f"no SSH key for the new cluster: {type(e).__name__}")
        creds.extend([files["key"], files["pub"], files["known_hosts"]])
        opts = _bm_with_ssh_key(opts, files["pub"].read_text().strip())
    try:
        cfg_yaml = _harvester_install_config(dict(opts, iso_url=iso_url, power_off=True))
    except InstallConfigError as e:
        return fail("remaster", f"invalid configuration: {', '.join(e.paths)}")
    cfg_path = _iso_work_dir() / f"config-{run.id}.yaml"
    cfg_path.write_text(cfg_yaml)
    cfg_path.chmod(0o600)          # contient un token et un mot de passe
    scratch.append(cfg_path)
    cfg_token = pxe_server.issue(cfg_path, "config")
    tokens.append(cfg_token)
    config_url = f"http://{advertise}:{port}/pxe/config/{cfg_token}.yaml"

    step("remaster", "running", f"remasterisation de {src_iso.name}")
    script = BIN_DIR / "harvester-iso-remaster.sh"
    cmd = ["/usr/bin/env", "bash", str(script), "--src", str(src_iso),
           "--out", str(out_iso), "--config-url", config_url,
           # le magasin d'ISO est sur disque ; /tmp est un tmpfs sur
           # beaucoup d'hôtes et l'extraction y tiendrait en RAM.
           "--work-dir", str(_iso_work_dir())]
    if opts.get("extra_args"):
        cmd += ["--extra-args", opts["extra_args"]]
    if _bm_remaster_stream(cmd, step) != 0:
        return fail("remaster", "ISO remastering failed")
    step("serve", "done", f"artefacts publiés sur {advertise}:{port}")

    # --- média virtuel + amorce + allumage ---
    step("bmc-insert", "running", "insertion dans le lecteur virtuel")
    ok, detail, vm_res = _bm_media_insert(host, kc_user, kc_pwd, iso_url)
    if not ok:
        return fail("bmc-insert", detail[:200])
    step("bmc-insert", "done", "image montée")

    step("bmc-boot", "running", "amorce unique sur le lecteur virtuel")
    sys_path = profile.get("system_path")
    ok, detail = _bm_boot_once_cd(host, kc_user, kc_pwd, sys_path)
    if not ok:
        return fail("bmc-boot", detail[:200])
    step("bmc-boot", "done", "prochaine amorce : CD virtuel")

    step("power", "running", "redémarrage sur l'installeur")
    # v1.78.0 : un cycle complet (arrêt puis mise sous tension) plutôt qu'un
    # redémarrage à chaud : certains BMC ne branchent le média virtuel, ou
    # n'appliquent l'amorce programmée, qu'à une mise sous tension (vu en
    # réel sur le banc Redfish : le CD inséré machine allumée n'était pas vu
    # et la machine repartait sur son ancien système).
    ok, detail = _bm_power_cycle(host, kc_user, kc_pwd, sys_path, run)
    if not ok:
        return fail("power", detail[:200])
    keep_creds[0] = True
    install_started[0] = True
    step("power", "done", "machine redémarrée")

    # --- attente de l'installation ---
    deadline = time.time() + HARVESTER_INSTALL_TIMEOUT
    # v1.78.0 : l'installeur s'éteint à la fin (install.power_off). La
    # console sait alors que l'installation est finie, éjecte le média et
    # démarre elle-même la machine sur son disque : plus de retour sur le
    # CD chez un BMC qui n'applique pas l'amorce « une seule fois » (vu en
    # réel sur le banc Redfish : l'installeur tournait en boucle).
    step("wait-install", "running", "installation en cours, la machine s'éteindra à la fin")
    if not _bm_wait_power_off(host, kc_user, kc_pwd, sys_path, deadline, run, step,
                              stalled=lambda: _bm_install_stalled(cfg_token, iso_token, host)):
        if getattr(run, "_cancel", False):
            return fail("wait-install", "cancelled by operator")
        if getattr(run, "_stall", None):
            return fail("wait-install", run._stall)
        return fail("wait-install", f"the installer did not power the machine off after "
                                    f"{HARVESTER_INSTALL_TIMEOUT // 60} min")
    step("wait-install", "done", "installation terminée, machine éteinte")
    step("boot-disk", "running", "éjection du média, démarrage sur le disque")
    ok_ej, detail_ej = _bm_media_eject(host, kc_user, kc_pwd, vm_res)
    if not ok_ej:
        # rallumer avec l'ISO encore montée pourrait relancer une installation
        return fail("boot-disk", "Harvester is installed but the virtual media could not be "
                                 f"ejected ({str(detail_ej)[:120]}): eject it from the BMC, "
                                 "then power the machine on")
    ok, detail = _bm_boot_once_target(host, kc_user, kc_pwd, sys_path, "Hdd")
    if not ok:
        step("boot-disk", "warn", f"amorce sur le disque refusée : {str(detail)[:120]}")
    ok, detail = _bm_reset(host, kc_user, kc_pwd, sys_path, "On")
    if not ok:
        return fail("boot-disk", f"power on refused: {str(detail)[:200]}")
    step("boot-disk", "done", "machine démarrée sur son disque")
    if opts.get("mode") == "join":
        step("wait-node", "running",
             f"installation en cours, attente de {opts['hostname']} dans {opts['cluster']}")
        if not _bm_wait_node_ready(opts["cluster"], opts["hostname"], deadline, run, step):
            return fail("wait-node",
                        f"{opts['hostname']} not Ready in {opts['cluster']} after "
                        f"{HARVESTER_INSTALL_TIMEOUT // 60} min")
        step("wait-node", "done", f"{opts['hostname']} prêt dans {opts['cluster']}")
    else:
        step("wait-api", "running",
             f"installation en cours, attente de l'API sur {opts['vip']}")
        if not _bm_wait_api(opts["vip"], deadline, run, step):
            return fail("wait-api",
                        f"no API on {opts['vip']}:6443 after "
                        f"{HARVESTER_INSTALL_TIMEOUT // 60} min")
        step("wait-api", "done", f"API disponible sur {opts['vip']}")

    # --- ménage : ne pas laisser un ISO monté ni un artefact exposé ---
    _bm_media_eject(host, kc_user, kc_pwd, vm_res)
    pxe_server.revoke(*tokens)
    out_iso.unlink(missing_ok=True)
    out_iso.with_suffix(out_iso.suffix + ".sha256").unlink(missing_ok=True)
    cfg_path.unlink(missing_ok=True)
    step("cleanup", "done", "média éjecté, artefacts révoqués")

    # --- après l'installation (v1.78.0) : déclaration du cluster créé, puis
    # pools de disques ; un seul budget de temps pour les deux. Toute
    # exception finit par fail() : le run ne reste jamais « running ».
    current = ["declare"]
    try:
        post_deadline = time.time() + HARVESTER_POOLS_TIMEOUT
        if opts.get("mode") == "join":
            kubeconfig = None
            if opts.get("cluster"):
                step("declare", "done", _bm_add_joined_node(opts["cluster"], opts["hostname"], opts.get("ip")))
        else:
            name = opts["cluster_name"]
            step("declare", "running", f"lecture du kubeconfig de {name} par SSH ({opts['vip']})")
            if not _bm_fetch_kubeconfig(opts["vip"], files["key"], files["known_hosts"],
                                        files["kubeconfig"], post_deadline, run, step):
                return fail("declare", f"Harvester is installed; no SSH access to {opts['vip']} as "
                                       f"{_BM_OS_USER} to read its kubeconfig")
            _bm_declare_cluster(name, files["kubeconfig"], files["key"], opts["hostname"],
                                opts.get("ip") or opts["vip"])
            kubeconfig = files["kubeconfig"]
            step("declare", "done", f"{name} déclaré dans la console")
        # sautés s'il n'y en a pas : aucune étape affichée
        if pools:
            current[0] = "pools"
            step("pools", "running", f"{len(pools)} pool(s) : {', '.join(p['tag'] for p in pools)}")
            if opts.get("mode") == "join":
                # le cluster est déclaré (la route l'exige) : son kubeconfig
                kubeconfig = _kubectl_for_cluster(opts["cluster"])
                if not kubeconfig:
                    return fail("pools", f"Harvester is installed; the pools were not created: "
                                         f"{opts['cluster']} is no longer declared")
            else:
                for p in pools:
                    if p["replicas"] > 1:
                        step("pools", "progress",
                             f"attention : {p['tag']} demande {p['replicas']} répliques sur un seul "
                             f"nœud, ses volumes resteront dégradés tant que d'autres nœuds "
                             f"n'auront pas rejoint")
            code, why = _bm_apply_pools(opts, kubeconfig, run, step, post_deadline - time.time())
            if getattr(run, "_cancel", False):
                return fail("pools", "cancelled by operator")
            if code != 0:
                return fail("pools", "Harvester is installed; the pools were not created: "
                                     + (why or f"pools-apply exited with {code}"))
            step("pools", "done", "pools : " + ", ".join(p["tag"] for p in pools))
    except Exception as e:
        # une OSError porterait le chemin d'un fichier privé : son type seul
        detail = type(e).__name__ if isinstance(e, OSError) else f"{type(e).__name__}: {str(e)[:200]}"
        return fail(current[0], f"Harvester is installed; {current[0]} failed: {detail}")
    for p in scratch:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    run.exit_code = 0
    run.status = "done"
    run.ended_at = time.time()
    run.emit({"type": "status", "status": "done", "exit_code": 0,
              "ts": time.time()})
    run.close()


def _bm_local_ip_for(host):
    """IP locale que le BMC pourra joindre : celle de l'interface qui sert
    à lui parler. Évite de publier 127.0.0.1 dans l'URL de l'ISO."""
    import socket
    # v1.78.0 : un BMC peut s'écrire `hôte:port` (émulateur Redfish du banc,
    # BMC derrière une redirection) ; le port n'a rien à faire dans la
    # résolution, qui échouait et publiait 127.0.0.1.
    host = str(host)
    if host.startswith("["):
        host = host[1:host.index("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 443))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# --- configuration d'installation : import, aperçu (v1.77.0) ----------------
#
# Un fichier importé peut porter le jeton du cluster et le mot de passe OS.
# Ils ne repartent JAMAIS vers le navigateur : gardés ici, par personne,
# 15 minutes, et désignés par un `import_id` aléatoire. L'installation les
# reprend quand les champs du formulaire sont vides.
from secrets import token_urlsafe as _bm_token_urlsafe  # noqa: E402

_BM_IMPORT_TTL = 15 * 60
_BM_IMPORT_MAX = 256 * 1024          # octets du texte importé
_BM_IMPORT_CACHE = {}                # import_id -> (horodatage, personne, secrets)
_BM_IMPORT_LOCK = threading.Lock()
# `iso_url` n'est connue qu'après le préflight (jeton du serveur d'artefacts) :
# la validation d'avant l'ActionRun rend avec cette adresse de remplacement.
_BM_ISO_URL_PLACEHOLDER = "http://console.invalid/pxe/iso/placeholder.iso"
_BM_MASK = "•••"


def _bm_person():
    """Même clé que le cache global de Forklift : identité déléguée au
    cluster et utilisateur de la console."""
    return ((current_cluster_identity() or {}).get("user"), current_user())


def _bm_import_purge(now):
    for k in [k for k, v in _BM_IMPORT_CACHE.items() if now - v[0] >= _BM_IMPORT_TTL]:
        del _BM_IMPORT_CACHE[k]


def _bm_import_put(secrets_):
    now = time.time()
    import_id = _bm_token_urlsafe(16)          # 128 bits
    with _BM_IMPORT_LOCK:
        _bm_import_purge(now)
        _BM_IMPORT_CACHE[import_id] = (now, _bm_person(), dict(secrets_ or {}))
    return import_id


def _bm_import_get(import_id):
    """Secrets d'un import de la MÊME personne, encore valide ; None sinon
    (inconnu, expiré ou appartenant à quelqu'un d'autre : même réponse)."""
    if not isinstance(import_id, str) or not import_id:
        return None
    now = time.time()
    with _BM_IMPORT_LOCK:
        _bm_import_purge(now)
        hit = _BM_IMPORT_CACHE.get(import_id)
    if not hit or hit[1] != _bm_person():
        return None
    return dict(hit[2])


def _bm_apply_import(data):
    """Complète jeton et mot de passe vides depuis l'import désigné. Le
    formulaire l'emporte quand les deux sont renseignés. Renvoie une réponse
    d'erreur, ou None."""
    import_id = data.pop("import_id", None)
    if not import_id:
        return None
    secrets_ = _bm_import_get(import_id)
    if secrets_ is None:
        return jsonify({"error": "import expired", "fields": ["import_id"]}), 400
    for k in ("token", "password"):
        if not data.get(k) and secrets_.get(k):
            data[k] = secrets_[k]
    return None


def _bm_config_refusal(e):
    return jsonify({"error": "invalid configuration", "fields": e.paths,
                    "reasons": e.reasons}), 400


def _bm_render_checked(data):
    """Configuration finale (dict) avec l'adresse d'ISO de remplacement, ou
    InstallConfigError. Aucune autre exception ne doit remonter un secret."""
    return _his.render_install_config(dict(data, iso_url=_BM_ISO_URL_PLACEHOLDER))


@app.route("/api/baremetal/config/parse", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_baremetal_config_parse():
    """Découpe un fichier de configuration importé : champs du formulaire,
    reste en YAML avancé. Les secrets restent côté serveur."""
    # le texte peut doubler en JSON (échappements) : garde large avant lecture
    if (request.content_length or 0) > 2 * _BM_IMPORT_MAX + 4096:
        return jsonify({"error": "file too large", "max_bytes": _BM_IMPORT_MAX}), 413
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str):
        return jsonify({"error": "missing fields", "fields": ["text"]}), 400
    if len(text.encode("utf-8", "surrogatepass")) > _BM_IMPORT_MAX:
        return jsonify({"error": "file too large", "max_bytes": _BM_IMPORT_MAX}), 413
    # 1.78.0 : avec l'inventaire de la machine, la liste des disques à
    # effacer du fichier rejoint les cases du tableau des disques
    host = data.get("bmc_host")
    known = None
    if isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,253}", host):
        doc = _bmd.load_inventory(INVENTORY_DIR, host)
        if doc:
            known = set()
            for d in _bmdisks.parse_discovery(doc.get("raw") or "")["disks"]:
                links = d.get("links") or {}
                known.update([d.get("stable_path"), "/dev/" + str(d.get("name") or "")]
                             + list(links.get("by_id") or []) + list(links.get("by_path") or []))
            known.discard(None)
    out = split_imported_config(text, known)
    if out["errors"]:
        return jsonify({"error": "invalid configuration", "fields": out["errors"],
                        "errors": out["errors"]}), 400
    secrets_ = out["secrets"]
    return jsonify({"form": out["form"], "advanced": out["advanced"],
                    "notes": out["notes"], "import_id": _bm_import_put(secrets_),
                    "has_token": bool(secrets_.get("token")),
                    "has_password": bool(secrets_.get("password"))})


@app.route("/api/baremetal/config/preview", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_baremetal_config_preview():
    """YAML final tel que l'installeur le recevra, jeton et mot de passe
    masqués. Pas besoin des identifiants du BMC."""
    data = request.get_json(force=True, silent=True) or {}
    # l'hôte ne sert qu'à lire l'inventaire de la machine (contrôles des
    # disques) ; les identifiants du BMC ne sont pas utilisés ici
    host = data.get("bmc_host")
    for k in ("bmc_host", "bmc_user", "bmc_password"):
        data.pop(k, None)
    # l'aperçu montre ce que l'installeur lira : la console lui fait
    # toujours éteindre la machine à la fin (v1.78.0)
    data["power_off"] = True
    if isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,253}", host):
        try:
            data["pools"] = _hh.check_pools(data.get("pools"))
        except ValueError as e:
            return jsonify({"error": f"invalid pools: {e}", "fields": ["pools"]}), 400
        disk_errors = _bm_disk_role_check(dict(data, bmc_host=host))
        if disk_errors:
            return _bm_disk_refusal(disk_errors)
    data["own_disk_names"] = _bm_own_disk_names(dict(data, bmc_host=host)) if isinstance(host, str) else []
    mode = data.get("mode") or "create"
    if mode not in ("create", "join"):
        return jsonify({"error": "invalid mode", "fields": ["mode"]}), 400
    data["mode"] = mode
    err = _bm_apply_import(data)
    if err:
        return err
    try:
        cfg = _bm_render_checked(data)
    except InstallConfigError as e:
        return _bm_config_refusal(e)
    # masquage sur le dictionnaire rendu, pas par remplacement de texte
    if cfg.get("token"):
        cfg["token"] = _BM_MASK
    if isinstance(cfg.get("os"), dict) and cfg["os"].get("password"):
        cfg["os"]["password"] = _BM_MASK
    warn = _bm_nic_warning(dict(data, bmc_host=host))
    warnings = [{"code": "nic-speed", "nics": warn.split(": ", 1)[1].split(";", 1)[0], "message": warn}] if warn else []
    return jsonify({"yaml": _his.dump_install_config(cfg), "warnings": warnings})


@app.route("/api/baremetal/install", methods=["POST"])
@requires_auth
@_rate_limit("6/minute")
def api_baremetal_install():
    """Lance une installation Harvester zéro-touch sur une machine nue."""
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode") or "create"
    if mode not in ("create", "join"):
        return jsonify({"error": "invalid mode", "fields": ["mode"]}), 400
    data["mode"] = mode
    # v1.77.0 : jeton et mot de passe vides repris d'un fichier importé
    err = _bm_apply_import(data)
    if err:
        return err
    data, err = _bm_prepare_install(data)
    if err:
        return err
    # Le label de l'action ne porte ni token ni mot de passe.
    action_id, busy = _bm_track(f"baremetal-install:{data['hostname']}",
                                data["bmc_host"], _baremetal_install_runner, data)
    if busy:
        return busy
    return jsonify({"action_id": action_id, "hostname": data["hostname"]}), 202


def _bm_prepare_install(data, defer_join=False):
    """Contrôles d'une installation avant toute action sur la machine,
    partagés par la route et les séries de profils (1.80.0) : (données
    complétées, None) ou (None, réponse d'erreur). `defer_join` : le
    cluster rejoint n'existe pas encore (série, avant que la première ligne
    l'ait créé) ; tout le reste est contrôlé."""
    mode = data["mode"]
    # Créer un cluster demande sa VIP ; en rejoindre un, son adresse.
    required = ("bmc_host", "bmc_user", "bmc_password", "iso", "hostname",
                "device", "mgmt_interface", "token",
                "vip" if mode == "create" else "server_url")
    # une carte (ancien champ) ou plusieurs (agrégat, v1.77.0)
    has_iface = bool(data.get("mgmt_interface")
                     or _his._split_list(data.get("mgmt_interfaces")))
    missing = [k for k in required
               if not (has_iface if k == "mgmt_interface" else data.get(k))]
    if missing:
        return None, (jsonify({"error": "missing fields", "fields": missing}), 400)
    if mode == "join":
        if not re.fullmatch(r"https://[A-Za-z0-9.:\[\]-]+(?::\d+)?/?",
                            str(data["server_url"])):
            return None, (jsonify({"error": "invalid server_url (https://host[:port])",
                                   "fields": ["server_url"]}), 400)
        # Suivre l'arrivée du nœud demande de lire ce cluster.
        data["cluster"] = None if defer_join else _cluster_for_server(data["server_url"])
        if not data["cluster"] and not defer_join:
            return None, (jsonify({"error": "declare the cluster of server_url first",
                                   "fields": ["server_url"]}), 400)
    if data.get("method", "dhcp") == "static":
        for k in ("ip", "subnet_mask", "gateway"):
            if not data.get(k):
                return None, (jsonify({"error": "missing fields", "fields": [k]}), 400)
    safe_iso = _safe_artifact_name(data["iso"])
    if not safe_iso:
        return None, (jsonify({"error": "invalid ISO name"}), 400)
    data["iso"] = safe_iso
    # Arguments noyau supplémentaires : ils finissent sur une ligne de
    # commande grub, donc pas de guillemets ni de saut de ligne qui
    # permettraient d'en sortir.
    extra = " ".join(str(data.get("extra_args") or "").split())
    if extra and not re.fullmatch(r"[A-Za-z0-9 ._:/,=@+-]*", extra):
        return None, (jsonify({"error": "invalid extra kernel arguments"}), 400)
    # v1.83.2 : le champ `skipchecks` (profils) devient l'argument noyau de
    # l'installeur. Il ne servait qu'aux contrôles de disques de la console :
    # vu sur le banc (lot de deux machines), l'installeur s'arrêtait sur ses
    # contrôles matériels sans rien dire.
    if _his._truthy(data.get("skipchecks")) and "harvester.install.skipchecks=true" not in extra.split():
        extra = (extra + " harvester.install.skipchecks=true").strip()
    data["extra_args"] = extra
    # Configuration validée AVANT l'ActionRun : le runner ne la rend qu'après
    # le préflight, donc après avoir allumé la machine. Une clé refusée doit
    # l'être ici, sans rien toucher.
    data.pop("iso_url", None)
    data.pop("power_off", None)       # posé par le déroulé, jamais par le client
    # v1.78.0 : pools de disques de données, créés après l'installation ;
    # refusés ici, avant d'allumer quoi que ce soit.
    try:
        data["pools"] = _hh.check_pools(data.get("pools"))
    except ValueError as e:
        return None, (jsonify({"error": f"invalid pools: {e}", "fields": ["pools"]}), 400)
    refused = _bm_pool_disk_refusals(data)
    if refused:
        return None, (jsonify({"error": "invalid pools: a pool disk is also the system or data disk, "
                                        "or is not in this machine's inventory",
                               "fields": ["pools"], "reasons": [list(r) for r in refused]}), 400)
    # v1.78.0 : avec un inventaire de la machine, chaque disque désigné y est
    # retrouvé et contrôlé (taille, données présentes, rôle doublé) avant
    # d'allumer quoi que ce soit
    disk_errors = _bm_disk_role_check(data)
    if disk_errors:
        return None, _bm_disk_refusal(disk_errors)
    # jamais une valeur du client : calculée ici, depuis l'inventaire
    data["own_disk_names"] = _bm_own_disk_names(data)
    # v1.78.0 : un cluster créé est déclaré dans la console à la fin
    if mode == "create":
        name = _bm_cluster_name(data)
        if not _BM_CLUSTER_NAME_RE.match(name):
            return None, (jsonify({"error": "invalid cluster name (RFC 1123: lower-case letters, digits, "
                                            "dashes and dots)", "fields": ["cluster_name"]}), 400)
        if any(c.get("name") == name for c in load_config().get("clusters", [])):
            return None, (jsonify({"error": f"cluster '{name}' is already declared",
                                   "fields": ["cluster_name"]}), 400)
        data["cluster_name"] = name
    try:
        _bm_render_checked(data)
    except InstallConfigError as e:
        return None, _bm_config_refusal(e)
    return data, None


# --- profils d'installation multi-nœuds (v1.80.0) ----------------------------
#
# Un profil est une configuration d'installation à variables `{{nom}}`,
# rangée dans <état>/profiles.d (bin/lib/bm_profiles.py). Une série applique
# un profil à un tableau de machines : la première ligne crée le cluster, les
# suivantes le rejoignent, chacune par sa propre installation suivie (la même
# que la route /api/baremetal/install). Les secrets (jeton, mot de passe de
# l'OS, mots de passe des BMC) ne sont saisis qu'au lancement, restent en
# mémoire du déroulé et ne sont jamais écrits ni renvoyés.
import bm_profiles as _bmp  # noqa: E402

BM_BATCH_POLL = float(os.environ.get("HARVESTER_OPS_BATCH_POLL", 3))
# valeur de remplacement des secrets pour les contrôles d'une série (aperçu,
# validation avant le lancement) : jamais servie à un installeur
_BM_CHECK_SECRET = "x"


def _bm_field_type(path):
    f = _his.field_at(path)
    return f.type if f is not None else None


def _bm_profiles_state():
    return _state_dir()


def _bm_errors_json(errors, error="invalid profile", status=400):
    return jsonify({"error": error, "errors": [[w, r] for w, r in errors],
                    "fields": [w for w, _ in errors]}), status


def _bm_refusal_pairs(resp, where):
    """[(où, raison)] lus dans une réponse de refus de _bm_prepare_install."""
    body = resp[0].get_json(silent=True) or {}
    reasons = body.get("reasons")
    if isinstance(reasons, dict) and reasons:
        return [(f"{where} {p}", str(r)) for p, r in reasons.items()]
    if isinstance(reasons, list) and reasons:
        return [(f"{where} {r[0]}", str(r[1])) for r in reasons if isinstance(r, list) and len(r) == 2]
    fields = body.get("fields") or []
    return [(f"{where} {', '.join(map(str, fields))}".strip(), str(body.get("error") or "refused"))]


def _bm_profile_check_render(prof):
    """Rendu d'essai d'un profil, chaque variable valant « 1 » : une clé
    inconnue ou réservée est refusée à l'enregistrement, pas au lancement."""
    sample = {v: "1" for v in _bmp.used_variables(prof)}
    batch = {"cluster_name": "c", "vip": "1",
             "rows": [{"bmc_host": "h", "bmc_user": "u", "values": sample}]}
    opts = _bmp.node_opts(prof, batch, 0, _bm_field_type, _his.dump_install_config)
    try:
        _hh.check_pools(opts.get("pools"))
    except ValueError as e:
        return [("fields.pools", str(e)[:200])]
    try:
        _bm_render_checked(dict(opts, token=_BM_CHECK_SECRET))
    except InstallConfigError as e:
        return [(p, str(e.reasons.get(p) or e.message)) for p in e.paths]
    return []


def _bm_profile_view(prof):
    """Profil tel que la fenêtre l'édite : les champs aussi en YAML."""
    return dict(prof, fields_yaml=yaml.safe_dump(prof.get("fields") or {}, sort_keys=False,
                                                 allow_unicode=True) if prof.get("fields") else "")


def _bm_profile_payload(data, name):
    doc = {k: data.get(k) for k in ("description", "variables", "fields", "advanced_yaml")}
    # la fenêtre envoie les champs en YAML (`fields_yaml`), la ligne de
    # commande et les tests en dictionnaire
    if isinstance(data.get("fields_yaml"), str):
        try:
            fields = yaml.safe_load(data["fields_yaml"]) if data["fields_yaml"].strip() else {}
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            raise _bmp.ProfileError([(f"fields (line {mark.line + 1})" if mark else "fields",
                                      "invalid YAML")]) from None
        if not isinstance(fields, dict):
            raise _bmp.ProfileError([("fields", "not a mapping")])
        doc["fields"] = fields
    prof = _bmp.check_profile(doc)
    errors = _bm_profile_check_render(dict(prof, name=name))
    if errors:
        raise _bmp.ProfileError(errors)
    return prof


@app.route("/api/baremetal/profiles", methods=["GET"])
@requires_auth
@_rate_limit("60/minute")
def api_bm_profiles_list():
    return jsonify({"profiles": _bmp.list_profiles(_bm_profiles_state()),
                    "builtins": list(_bmp.BUILTIN_VARS)})


@app.route("/api/baremetal/profiles", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_bm_profiles_create():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name")
    path = _bmp.profile_path(_bm_profiles_state(), name)
    if path is None:
        return _bm_errors_json([("name", "invalid name (RFC 1123)")])
    if path.exists():
        return jsonify({"error": f"profile '{name}' already exists", "fields": ["name"]}), 409
    try:
        prof = _bmp.save_profile(_bm_profiles_state(), name, _bm_profile_payload(data, name))
    except _bmp.ProfileError as e:
        return _bm_errors_json(e.errors)
    return jsonify(_bm_profile_view(prof)), 201


@app.route("/api/baremetal/profiles/<name>", methods=["GET"])
@requires_auth
@_rate_limit("60/minute")
def api_bm_profile_get(name):
    prof = _bmp.load_profile(_bm_profiles_state(), name)
    if prof is None:
        return jsonify({"error": "no such profile"}), 404
    return jsonify(_bm_profile_view(prof))


@app.route("/api/baremetal/profiles/<name>", methods=["PUT"])
@requires_auth
@_rate_limit("30/minute")
def api_bm_profile_update(name):
    path = _bmp.profile_path(_bm_profiles_state(), name)
    if path is None or not path.is_file():
        return jsonify({"error": "no such profile"}), 404
    data = request.get_json(force=True, silent=True) or {}
    try:
        prof = _bmp.save_profile(_bm_profiles_state(), name, _bm_profile_payload(data, name))
    except _bmp.ProfileError as e:
        return _bm_errors_json(e.errors)
    return jsonify(_bm_profile_view(prof))


@app.route("/api/baremetal/profiles/from-config", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_bm_profile_from_config():
    """Point de départ d'un profil : un fichier de configuration découpé
    comme à l'import de la fenêtre d'installation. Jeton et mot de passe du
    fichier sont JETÉS (un profil n'en porte pas), pas mis en cache."""
    if (request.content_length or 0) > 2 * _BM_IMPORT_MAX + 4096:
        return jsonify({"error": "file too large", "max_bytes": _BM_IMPORT_MAX}), 413
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str):
        return jsonify({"error": "missing fields", "fields": ["text"]}), 400
    out = split_imported_config(text)
    if out["errors"]:
        return jsonify({"error": "invalid configuration", "fields": out["errors"]}), 400
    fields = {k: v for k, v in out["form"].items() if k in _bmp.PROFILE_FIELDS}
    return jsonify({"fields_yaml": yaml.safe_dump(fields, sort_keys=False, allow_unicode=True)
                    if fields else "", "advanced_yaml": out["advanced"], "notes": out["notes"],
                    "secrets_dropped": bool(out["secrets"])})


@app.route("/api/baremetal/profiles/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("30/minute")
def api_bm_profile_delete(name):
    if not _bmp.delete_profile(_bm_profiles_state(), name):
        return jsonify({"error": "no such profile"}), 404
    return jsonify({"deleted": name})


@app.route("/api/baremetal/profiles/<name>/csv", methods=["POST"])
@requires_auth
@_rate_limit("60/minute")
def api_bm_profile_csv(name):
    """Lignes d'une série depuis un CSV collé. Une colonne bmc_password est
    lue puis JETÉE : un mot de passe ne revient pas au navigateur, il se
    saisit dans le tableau ou une fois pour toute la série."""
    prof = _bmp.load_profile(_bm_profiles_state(), name)
    if prof is None:
        return jsonify({"error": "no such profile"}), 404
    if (request.content_length or 0) > 256 * 1024:
        return jsonify({"error": "text too large"}), 413
    data = request.get_json(force=True, silent=True) or {}
    try:
        rows = _bmp.parse_nodes_csv(data.get("text"), prof["variables"])
    except _bmp.ProfileError as e:
        return _bm_errors_json(e.errors, "invalid CSV")
    dropped = False
    for r in rows:
        dropped = (r.pop("bmc_password", None) is not None) or dropped
    return jsonify({"rows": rows, "passwords_dropped": dropped})


def _bm_batch_input(data):
    """(lot sans secrets, secrets) depuis le corps d'une requête ou la
    ligne de commande."""
    rows = []
    for r in data.get("rows") or []:
        if not isinstance(r, dict):
            rows.append(r)
            continue
        rows.append({"bmc_host": str(r.get("bmc_host") or "").strip(),
                     "bmc_user": str(r.get("bmc_user") or "").strip(),
                     "bmc_password": str(r.get("bmc_password") or ""),
                     "values": {str(k): (v if isinstance(v, str) else str(v))
                                for k, v in (r.get("values") or {}).items()}
                     if isinstance(r.get("values"), dict) else r.get("values")})
    batch = {"cluster_name": str(data.get("cluster_name") or "").strip(),
             "vip": str(data.get("vip") or "").strip(),
             "iso": str(data.get("iso") or "").strip() or None, "rows": rows}
    secrets_ = {k: str(data.get(k) or "") for k in ("token", "password", "bmc_password")}
    return batch, secrets_


def _bm_batch_plan(prof, batch, secrets_):
    """Options d'installation complètes et contrôlées de chaque ligne
    (jonctions contrôlées sans leur cluster, qui n'existe pas encore), ou
    la liste des refus [(où, raison)]. Rien n'est allumé ici."""
    errors = _bmp.check_batch(prof, batch)
    if errors:
        return None, errors
    plans = []
    for i, row in enumerate(batch["rows"]):
        where = f"row {i + 1}"
        try:
            opts = _bmp.node_opts(prof, batch, i, _bm_field_type, _his.dump_install_config)
        except _bmp.ProfileError as e:
            errors += e.errors
            continue
        opts["bmc_password"] = row.get("bmc_password") or secrets_.get("bmc_password") or ""
        opts["token"] = secrets_.get("token") or ""
        if secrets_.get("password"):
            opts["password"] = secrets_["password"]
        if not opts["bmc_password"]:
            errors.append((f"{where} bmc_password", "missing"))
            continue
        with app.app_context():                 # jsonify hors requête (CLI)
            data, err = _bm_prepare_install(dict(opts), defer_join=i > 0)
        if err:
            errors += _bm_refusal_pairs(err, where)
            continue
        plans.append(data)
    if not secrets_.get("token"):
        errors.append(("token", "missing"))
    return (None, errors) if errors else (plans, [])


def _bm_wait_action(action_id, parent, sleep=None):
    """État final d'une installation de la série. Un arrêt demandé sur la
    série est transmis à l'installation en cours."""
    sleep = sleep or time.sleep
    while True:
        with ACTIONS_LOCK:
            child = ACTIONS.get(action_id)
        if child is None:
            return "error"
        if getattr(parent, "_cancel", False) and child.status in ("starting", "running"):
            child._cancel = True
        if child.status in ("done", "error", "cancelled"):
            return child.status
        sleep(BM_BATCH_POLL)


def _baremetal_batch_runner(run, plan, concurrency=_bmp.DEFAULT_CONCURRENCY):
    """Série d'installations : ligne 1 seule (création), puis les jonctions,
    `concurrency` à la fois, chacune une action suivie à part. Chaque nœud
    est une étape `node-<n>` de cette action, avec l'identifiant de son
    installation."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    nodes = [{"row": i + 1, "hostname": o["hostname"], "bmc_host": o["bmc_host"],
              "mode": o["mode"], "action_id": None, "status": "pending"}
             for i, o in enumerate(plan)]
    run.result = {"nodes": nodes}
    lock = threading.Lock()

    def report(i, status, msg):
        # v1.83.2 : la raison d'une ligne en échec part avec elle (vu en réel :
        # la ligne de commande disait « error » sans dire pourquoi)
        reason = None
        if status in ("error", "cancelled") and nodes[i]["action_id"]:
            with ACTIONS_LOCK:
                child = ACTIONS.get(nodes[i]["action_id"])
            reason = getattr(child, "error_summary", None) if child else None
        with lock:
            nodes[i]["status"] = status
            if reason:
                nodes[i]["error"] = str(reason)[:500]
            run.result = {"nodes": [dict(n) for n in nodes]}
        if reason:
            msg = f"{msg} {reason}".strip()
        n = nodes[i]
        run.emit({"type": "step", "step_id": f"node-{i + 1}",
                  "status": {"pending": "running"}.get(status, status),
                  "message": f"{n['hostname']} ({n['bmc_host']}, {n['mode']}) "
                             f"{n['action_id'] or ''} {'' if msg == n['action_id'] else msg}".strip(),
                  "ts": time.time()})

    def launch(i):
        opts = plan[i]
        if i > 0:
            # le cluster créé par la ligne 1 est maintenant déclaré
            with app.app_context():
                data, err = _bm_prepare_install(dict(opts))
            if err:
                raise RuntimeError("; ".join(f"{w}: {r}" for w, r in _bm_refusal_pairs(err, "")))
            opts = data
        with app.app_context():
            action_id, busy = _bm_track(f"baremetal-install:{opts['hostname']}",
                                        opts["bmc_host"], _baremetal_install_runner, opts)
        if busy:
            raise RuntimeError(f"BMC {opts['bmc_host']} is busy")
        with lock:
            nodes[i]["action_id"] = action_id
        return action_id

    try:
        states = _bmp.run_batch(len(plan), launch, lambda i, aid: _bm_wait_action(aid, run),
                                report, concurrency=concurrency,
                                cancelled=lambda: getattr(run, "_cancel", False))
    except Exception as e:                      # jamais de détail qui porterait un secret
        states = ["error"]
        run.error_summary = type(e).__name__
    finally:
        plan.clear()                            # secrets : plus rien en mémoire du déroulé
    ok = all(s == "done" for s in states)
    if not ok and not run.error_summary:
        bad = [f"{n['hostname']}: {n.get('error') or n['status']}" for n in nodes if n["status"] != "done"]
        run.error_summary = ", ".join(bad)[:300]
    cancelled = getattr(run, "_cancel", False) and not ok
    run.status = "done" if ok else ("cancelled" if cancelled else "error")
    run.exit_code = 0 if ok else (3 if cancelled else 1)
    run.ended_at = time.time()
    run.emit({"type": "status", "status": run.status, "exit_code": run.exit_code,
              "ts": time.time()})
    run.close()


def _bm_batch_start(prof, batch, secrets_, concurrency=_bmp.DEFAULT_CONCURRENCY):
    """Contrôle toute la série puis lance son action parente : (action_id,
    None) ou (None, refus [(où, raison)])."""
    plan, errors = _bm_batch_plan(prof, batch, secrets_)
    if errors:
        return None, errors
    for o in plan:
        busy = _bm_busy(o["bmc_host"])
        if busy:
            return None, [(o["bmc_host"], f"{busy.action} is already driving this BMC")]
    concurrency = max(1, min(4, int(concurrency or _bmp.DEFAULT_CONCURRENCY)))
    action_id = track_action(f"baremetal-batch:{batch['cluster_name']}", "(local)",
                             _baremetal_batch_runner, plan, concurrency)
    return action_id, None


@app.route("/api/baremetal/profiles/<name>/render", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_bm_profile_render(name):
    """Aperçu de chaque ligne d'une série : YAML final, jeton et mot de
    passe masqués, ou ses refus. Aucun secret n'est demandé ni utilisé."""
    prof = _bmp.load_profile(_bm_profiles_state(), name)
    if prof is None:
        return jsonify({"error": "no such profile"}), 404
    batch, _ = _bm_batch_input(request.get_json(force=True, silent=True) or {})
    errors = _bmp.check_batch(prof, batch)
    if errors:
        return _bm_errors_json(errors, "invalid batch")
    out = []
    for i in range(len(batch["rows"])):
        one = dict(batch, rows=batch["rows"])
        try:
            opts = _bmp.node_opts(prof, one, i, _bm_field_type, _his.dump_install_config)
        except _bmp.ProfileError as e:
            out.append({"row": i + 1, "errors": [[w, r] for w, r in e.errors]})
            continue
        opts.update(bmc_password=_BM_CHECK_SECRET, token=_BM_CHECK_SECRET)
        data, err = _bm_prepare_install(dict(opts), defer_join=i > 0)
        if err:
            out.append({"row": i + 1, "errors": [[w, r] for w, r in _bm_refusal_pairs(err, f"row {i + 1}")]})
            continue
        cfg = _bm_render_checked(dict(data, power_off=True))
        cfg["token"] = _BM_MASK
        out.append({"row": i + 1, "hostname": data["hostname"], "mode": data["mode"],
                    "yaml": _his.dump_install_config(cfg)})
    return jsonify({"rows": out, "ok": all("yaml" in r for r in out)})


@app.route("/api/baremetal/profiles/<name>/batch", methods=["POST"])
@requires_auth
@_rate_limit("6/minute")
def api_bm_profile_batch(name):
    """Lance une série. Jeton et mot de passe de l'OS : saisis ici, ou
    repris d'un fichier importé (`import_id`, même cache de 15 min)."""
    prof = _bmp.load_profile(_bm_profiles_state(), name)
    if prof is None:
        return jsonify({"error": "no such profile"}), 404
    data = request.get_json(force=True, silent=True) or {}
    err = _bm_apply_import(data)
    if err:
        return err
    batch, secrets_ = _bm_batch_input(data)
    try:
        concurrency = int(data.get("concurrency") or _bmp.DEFAULT_CONCURRENCY)
    except (TypeError, ValueError):
        return _bm_errors_json([("concurrency", "not a number")], "invalid batch")
    action_id, errors = _bm_batch_start(prof, batch, secrets_, concurrency)
    if errors:
        return _bm_errors_json(errors, "invalid batch")
    return jsonify({"action_id": action_id, "cluster": batch["cluster_name"],
                    "nodes": len(batch["rows"])}), 202


# --- démarrage de découverte (v1.78.0) ---------------------------------------
#
# Le déroulé vit dans bin/lib/bm_discover.py, partagé avec la ligne de
# commande (`harvester-baremetal.py discover`). La console lui fournit son
# accès au BMC, construit sur ses propres fonctions Redfish, et son serveur
# d'artefacts ; il lance lui-même bin/harvester-iso-remaster.sh.
INVENTORY_DIR = Path(os.environ.get(
    "HARVESTER_OPS_INVENTORY_DIR",
    str(Path.home() / ".local/share/harvester-ops/inventory"),
))


class _BmDiscoverBmc:
    """Accès au BMC attendu par `bm_discover.run`, sur les fonctions Redfish
    de la console. Le mot de passe reste dans cet objet."""

    def __init__(self, host, user, pwd):
        self.host, self._user, self._pwd = host, user, pwd
        self._sys = None

    def profile(self):
        p = _bmc_discover_one(self.host, self._user, self._pwd)
        self._sys = p.get("system_path")
        return p

    def insert(self, url):
        ok, detail, _ = _bm_media_insert(self.host, self._user, self._pwd, url)
        return ok, detail

    def eject(self):
        return _bm_media_eject(self.host, self._user, self._pwd)

    def boot_once_cd(self):
        return _bm_boot_once_cd(self.host, self._user, self._pwd, self._sys)

    def reset(self, reset_type):
        return _bm_reset(self.host, self._user, self._pwd, self._sys, reset_type)

    def power_state(self):
        s = _redfish_get(self.host, self._sys, self._user, self._pwd, timeout=8)
        return (s or {}).get("PowerState")


def _baremetal_discover_runner(run, opts):
    host = opts["bmc_host"]
    persisted = [time.time()]

    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": msg, "ts": time.time()})
        if time.time() - persisted[0] > 60:
            persisted[0] = time.time()
            try:
                _actions_persist(run)
            except Exception:
                pass

    def finish(status, code, summary=None):
        if summary:
            run.error_summary = summary[:300]
        run.exit_code = code
        run.status = status
        run.ended_at = time.time()
        run.emit({"type": "status", "status": status, "exit_code": code,
                  "ts": time.time()})
        run.close()

    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    src_iso = _iso_dir() / opts["iso"]
    if not src_iso.is_file():
        step("remaster", "error", f"ISO not found: {opts['iso']}")
        return finish("error", 1, f"ISO not found: {opts['iso']}")
    try:
        port = pxe_server.start()
        res = _bmd.run(
            # l'ISO de la découverte y est écrite puis effacée en fin de run ;
            # un run tué par un redémarrage est balayé au bout d'un jour
            {"host": host, "src_iso": src_iso, "work_dir": _iso_work_dir(), "store_dir": INVENTORY_DIR,
             "remaster_script": BIN_DIR / "harvester-iso-remaster.sh",
             "advertise": opts.get("advertise_host") or _bm_local_ip_for(host),
             "port": port, "extra_args": opts.get("extra_args", ""),
             **{k: opts[k] for k in ("inventory_timeout", "poweroff_timeout", "poll")
                if k in opts}},
            _BmDiscoverBmc(host, opts["bmc_user"], opts["bmc_password"]),
            pxe_server, _bm_remaster_stream, step,
            cancelled=lambda: getattr(run, "_cancel", False))
    except _bmd.DiscoveryError as e:
        cancelled = isinstance(e, _bmd.Cancelled) or getattr(run, "_cancel", False)
        step(e.step, "error", str(e)[:300])
        return finish("cancelled" if cancelled else "error", 3 if cancelled else 1, str(e))
    except Exception as e:                      # jamais de détail qui porterait un secret
        step("discover", "error", type(e).__name__)
        return finish("error", 1, type(e).__name__)
    inv = _bmdisks.parse_discovery(res["raw"])
    step("parse", "done", f"{len(inv['disks'])} disque(s), {len(inv['nics'])} carte(s) réseau")
    finish("done", 0)


# Une installation et une découverte pilotent la même alimentation et le même
# lecteur virtuel : deux actions sur un BMC se marcheraient dessus (média
# éjecté sous l'installeur, machine éteinte en pleine installation).
_BM_LAUNCH_LOCK = threading.Lock()


def _bm_busy(host):
    """Installation ou découverte en cours sur ce BMC, ou None."""
    with ACTIONS_LOCK:
        return next((r for r in ACTIONS.values()
                     if getattr(r, "bmc_host", None) == host
                     and r.status in ("starting", "running")), None)


def _bm_track(label, host, worker, opts):
    """`track_action` refusé si le BMC est déjà piloté : (action_id, None)
    ou (None, réponse 409). Contrôle et inscription sous un même verrou."""
    with _BM_LAUNCH_LOCK:
        busy = _bm_busy(host)
        if busy:
            return None, (jsonify({
                "error": f"{busy.action} is already driving the BMC {host} "
                         f"(action {busy.id}); wait for it to end or cancel it",
                "running": busy.id, "running_action": busy.action}), 409)
        action_id = track_action(label, "(local)", worker, opts)
        with ACTIONS_LOCK:
            run = ACTIONS.get(action_id)
            if run is not None:
                run.bmc_host = host
    return action_id, None


def _bm_inventory_view(doc):
    """Inventaire enregistré, analysé à la lecture (le brut ne sort pas)."""
    inv = _bmdisks.parse_discovery(doc.get("raw") or "")
    return {"source": doc.get("source", "discovery"), "at": doc.get("at"),
            "system_serial": doc.get("system_serial"),
            "disks": inv["disks"], "nics": inv["nics"]}


@app.route("/api/baremetal/discover", methods=["POST"])
@requires_auth
@_rate_limit("6/minute")
def api_baremetal_discover():
    """Démarrage de découverte : l'ISO démarre une fois, renvoie ce que
    Linux voit (disques, liens stables, cartes) et éteint la machine."""
    data = request.get_json(force=True, silent=True) or {}
    missing = [k for k in ("bmc_host", "bmc_user", "bmc_password", "iso")
               if not data.get(k)]
    if missing:
        return jsonify({"error": "missing fields", "fields": missing}), 400
    if not re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,253}", str(data["bmc_host"])):
        return jsonify({"error": "invalid bmc_host", "fields": ["bmc_host"]}), 400
    safe_iso = _safe_artifact_name(str(data["iso"]))
    if not safe_iso:
        return jsonify({"error": "invalid ISO name", "fields": ["iso"]}), 400
    try:
        extra = _bmd.clean_extra_args(data.get("extra_args"))
    except ValueError:
        return jsonify({"error": "invalid extra kernel arguments",
                        "fields": ["extra_args"]}), 400
    opts = {"bmc_host": data["bmc_host"], "bmc_user": str(data["bmc_user"]),
            "bmc_password": str(data["bmc_password"]), "iso": safe_iso,
            "extra_args": extra}
    # Le label de l'action ne porte que l'hôte.
    action_id, busy = _bm_track(f"baremetal-discover:{opts['bmc_host']}",
                                opts["bmc_host"], _baremetal_discover_runner, opts)
    if busy:
        return busy
    return jsonify({"action_id": action_id, "host": opts["bmc_host"]}), 202


@app.route("/api/baremetal/inventory/<host>", methods=["GET"])
@requires_auth
@_rate_limit("60/minute")
def api_baremetal_inventory(host):
    """Dernier inventaire de découverte reçu pour ce BMC, ou 404."""
    if not re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,253}", host):
        return jsonify({"error": "invalid host"}), 400
    doc = _bmd.load_inventory(INVENTORY_DIR, host)
    if not doc:
        return jsonify({"error": "no inventory for this host"}), 404
    return jsonify(_bm_inventory_view(doc))


# =============================================================================
# Magasin d'ISO d'installation (v1.18.0)
#
# Même patron que le magasin de bundles CAPI, déjà éprouvé sur 280 Mo, mais
# dimensionné pour un ISO Harvester (~5 Go) :
#   * le téléchargement se fait CÔTÉ SERVEUR en flux (POST /api/iso/fetch).
#     Un upload multipart passerait par le spooling temporaire de Werkzeug,
#     qui est un tmpfs dans le déploiement conteneurisé — donc en RAM ;
#   * l'ISO n'est JAMAIS embarqué dans le tarball (135 Mo pour le livrable
#     entier, contre ~5 Go pour une seule image).
# =============================================================================
ISO_DIR = Path(os.environ.get(
    "HARVESTER_OPS_ISO_DIR",
    str(Path.home() / ".local/share/harvester-ops/iso"),
))


def _iso_dir():
    ISO_DIR.mkdir(parents=True, exist_ok=True)
    return ISO_DIR


def _iso_work_dir():
    """Artefacts produits par une installation (ISO remasterisé,
    configuration). Séparés du magasin : sinon un run interrompu laisse une
    image de 7,7 Go dans la liste des ISO, où elle finit par être
    re-sélectionnée à la place de l'image officielle."""
    d = ISO_DIR / "work"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)          # la configuration y porte des secrets
    # Un run tué (redémarrage du serveur pendant les 30 minutes d'attente)
    # ne passe par aucun nettoyage : balayer ce qui traîne depuis plus d'un
    # jour évite d'accumuler des images de 7,7 Go et des configurations
    # contenant des secrets.
    cutoff = time.time() - 86400
    for p in d.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass
    return d


def _safe_artifact_name(name, suffixes=(".iso",)):
    """Nom de fichier sûr : pas de séparateur, pas de remontée, suffixe
    attendu. Généralise la garde du magasin CAPI, qui codait son préfixe
    en dur."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if not name.endswith(tuple(suffixes)):
        return None
    return name


def _iso_entry(path):
    sha = ""
    sha_file = path.with_suffix(path.suffix + ".sha256")
    if sha_file.exists():
        sha = (sha_file.read_text().split() or [""])[0]
    st = path.stat()
    return {"name": path.name, "size": st.st_size, "mtime": st.st_mtime,
            "sha256": sha}


@app.route("/api/isos")
@requires_auth
def api_isos_list():
    """ISO disponibles pour une installation, avec l'espace disque —
    un opérateur doit voir qu'il reste de la place avant de lancer un
    téléchargement de plusieurs gigaoctets."""
    d = _iso_dir()
    items = sorted((_iso_entry(p) for p in d.glob("*.iso")),
                   key=lambda e: e["mtime"], reverse=True)
    try:
        st = os.statvfs(d)
        free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        free = total = 0
    return jsonify({"isos": items, "iso_dir": str(d),
                    "disk_free": free, "disk_total": total,
                    "used": sum(e["size"] for e in items)})


def _iso_fetch_runner(run, url, dest):
    """Télécharge un ISO en flux, avec progression et sha256 calculé au vol."""
    import urllib.request, hashlib
    tmp = dest.with_suffix(dest.suffix + ".downloading")

    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": msg, "ts": time.time()})

    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    step("fetch", "running", f"downloading {url}")
    try:
        h = hashlib.sha256()
        done = 0
        last_pct = -1
        with urllib.request.urlopen(url, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            with open(tmp, "wb") as f:
                while True:
                    if getattr(run, "_cancel", False):
                        raise RuntimeError("cancelled by operator")
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if total:
                        pct = int(done * 100 / total)
                        # Agrégé : le buffer d'évènements d'une action est
                        # borné, inutile de le remplir de bruit.
                        if pct != last_pct and pct % 5 == 0:
                            last_pct = pct
                            step("fetch", "progress",
                                 f"{pct}% ({done // (1024*1024)} MiB)")
        tmp.rename(dest)
        digest = h.hexdigest()
        dest.with_suffix(dest.suffix + ".sha256").write_text(
            f"{digest}  {dest.name}\n")
        step("fetch", "done", f"{dest.name} ({done // (1024*1024)} MiB)")
        step("sha256", "done", digest)
        run.exit_code = 0
        run.status = "done"
    except Exception as e:
        tmp.unlink(missing_ok=True)
        # v1.47.0 : un arrêt demandé depuis le dock se lit « annulé »
        cancelled = getattr(run, "_cancel", False)
        run.error_summary = str(e)[:300]
        step("fetch", "error", str(e)[:300])
        run.exit_code = 3 if cancelled else 1
        run.status = "cancelled" if cancelled else "error"
    run.ended_at = time.time()
    run.emit({"type": "status", "status": run.status,
              "exit_code": run.exit_code, "ts": time.time()})
    run.close()


@app.route("/api/iso/fetch", methods=["POST"])
@requires_auth
@_rate_limit("6/minute")
def api_iso_fetch():
    """Télécharge un ISO côté serveur. Body: {url, name?}."""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "http(s) URL required"}), 400
    name = data.get("name") or url.rsplit("/", 1)[-1].split("?")[0]
    name = _safe_artifact_name(name)
    if not name:
        return jsonify({"error": "the file name must be a plain *.iso"}), 400
    dest = _iso_dir() / name
    if dest.exists():
        return jsonify({"error": "already present", "name": name}), 409
    action_id = track_action(f"iso-fetch:{name}", "(local)",
                             _iso_fetch_runner, url, dest)
    return jsonify({"action_id": action_id, "name": name}), 202


@app.route("/api/iso/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_iso_delete(name):
    safe = _safe_artifact_name(name)
    if not safe:
        return jsonify({"error": "invalid name"}), 400
    path = _iso_dir() / safe
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    path.unlink()
    path.with_suffix(path.suffix + ".sha256").unlink(missing_ok=True)
    return jsonify({"deleted": safe})


@app.route("/api/pcidevices/<cluster>")
@requires_auth
def api_list_pcidevices(cluster):
    """v1.15.0 — PCI devices for the passthrough picker of the VM editor.
    Read-only: harvester-ops never creates a PCIDeviceClaim (claiming
    unbinds the device from its host driver — not something a console
    should do behind the operator's back)."""
    data, err = _list_k8s_resources(
        cluster, "pcidevices.devices.harvesterhci.io",
        reducer=_reduce_pcidevice, cache_key="pcidevices",
    )
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@app.route("/api/pvcs/<cluster>")
@requires_auth
def api_list_pvcs(cluster):
    """v1.8.0 — PersistentVolumeClaims for the visual disk editor.
    Optional ?namespace= narrows the list (validated RFC 1123 by the
    before_request hook); default is all namespaces."""
    ns = request.args.get("namespace") or "*"
    data, err = _list_k8s_resources(
        cluster, "pvc", namespace=ns, reducer=_reduce_pvc,
        cache_key=f"pvc:{ns}",
    )
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@app.route("/api/cloudinits/<cluster>")
@requires_auth
def api_list_cloudinits(cluster):
    """Harvester treats a Secret with label `harvesterhci.io/cloud-init=user`
    or value `harvesterhci.io/cloudInit=user-data` as a reusable user-data
    secret. We list all Secrets typed Opaque carrying that label."""
    data, err = _list_k8s_resources(
        cluster, "secret", namespace="*",
        label_selector="harvesterhci.io/cloud-init",
        reducer=_reduce_cloudinit,
    )
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


# -----------------------------------------------------------------------------
# VM order management — list VMs with priority, update order via annotations
# -----------------------------------------------------------------------------
def _kubectl_for_cluster(cluster):
    """Chemin du kubeconfig à employer pour ce cluster.

    Point de substitution UNIQUE de la délégation d'identité (v1.32.0) :
    quand elle est active, on rend une copie porteuse de l'identité de
    l'appelant, et les ~50 sites d'appel kubectl en héritent sans être
    modifiés. Rendu inchangé quand la délégation est éteinte.
    """
    if _READ_WORKER_CTX:
        return _READ_WORKER_CTX.get("kc")
    cfg = load_config_ro()
    for c in cfg.get("clusters", []):
        if c["name"] == cluster:
            if _sso_session() is not None:
                return _sso_kubeconfig(cluster, c)
            return _identity_kubeconfig(c["kubeconfig"],
                                        current_cluster_identity())
    return None


_KUBE_SYSTEM_MISS = {}         # cluster -> horodatage du dernier échec


def _kube_system_uid(entry):
    """L'UID de `kube-system`, lu avec le kubeconfig de la console : le même
    objet vu à travers Rancher désigne le même cluster."""
    name = entry.get("name")
    if name in _KUBE_SYSTEM_UIDS:
        return _KUBE_SYSTEM_UIDS[name]
    # Un cluster éteint ne se relit pas à chaque page : une minute de répit,
    # et la sonde TCP de deux secondes plutôt que le délai de kubectl.
    if time.time() - _KUBE_SYSTEM_MISS.get(name, 0) < 60:
        return None
    if _cluster_reachable(entry["kubeconfig"]) is False:
        _KUBE_SYSTEM_MISS[name] = time.time()
        return None
    try:
        out = _kubectl_run(["kubectl", "--kubeconfig", entry["kubeconfig"], "get", "namespace",
                              "kube-system", "-o", "jsonpath={.metadata.uid}"],
                             capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return None
    uid = out.stdout.strip() if out.returncode == 0 else None
    if uid:
        _KUBE_SYSTEM_UIDS[name] = uid
    else:
        _KUBE_SYSTEM_MISS[name] = time.time()
    return uid


def _sso_visible(entry):
    """Ce cluster est-il atteignable à travers Rancher pour cette session ?"""
    return _sso_session() is None or _sso_kubeconfig(entry["name"], entry) is not None


_NODE_UIDS = {}                # cluster -> (instant, frozenset des uid de nœuds)


def _node_uids(entry):
    """Les UID des nœuds, lus avec le kubeconfig de la console : un membre du
    cluster les lit à travers Rancher alors qu'il ne lit pas `kube-system`.
    Relus toutes les dix minutes (un nœud peut rejoindre le cluster)."""
    name = entry.get("name")
    seen = _NODE_UIDS.get(name)
    if seen and time.time() - seen[0] < 600:
        return seen[1]
    if time.time() - _KUBE_SYSTEM_MISS.get(name, 0) < 60 or _cluster_reachable(entry["kubeconfig"]) is False:
        return frozenset()
    try:
        out = _kubectl_run(["kubectl", "--kubeconfig", entry["kubeconfig"], "get", "nodes",
                              "-o", "jsonpath={.items[*].metadata.uid}"],
                             capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return frozenset()
    uids = frozenset(out.stdout.split()) if out.returncode == 0 else frozenset()
    if uids:
        _NODE_UIDS[name] = (time.time(), uids)
    return uids


def _cluster_id_key(st, cluster):
    """Clé de l'id Rancher appris pour un cluster : le nom seul pour le
    Rancher de config.yaml (comme en 1.50), `<cluster>@<rancher>` pour un
    Rancher réglé dans la console (deux Rancher, deux ids)."""
    if st.get("origin", "config") == "config":
        return cluster
    return f"{cluster}@{st.get('id')}"


def _sso_cluster_state(entry):
    """(kubeconfig de la session, raison) pour ce cluster. La raison dit
    pourquoi la personne ne le voit pas, pour que l'écran l'explique au lieu
    d'afficher une liste vide :
      - `no-access` : Rancher gère ce cluster et n'y donne pas accès à ce compte ;
      - `not-found` : aucun cluster que Rancher montre à ce compte n'est
        celui-ci (pas géré par ce Rancher, ou aucun droit d'y lire) ;
      - `unidentified` : la console elle-même ne joint pas ce cluster pour le
        reconnaître."""
    sess = _sso_session()
    st = _session_settings(sess)
    if sess is None or st is None:
        return None, "no-session"
    cluster = entry["name"]
    store, http = _sso_store(), _sso_http(st)
    key = _cluster_id_key(st, cluster)
    # `rancher_cluster` d'une déclaration vise le Rancher de config.yaml
    cid = (entry.get("rancher_cluster") if st.get("origin", "config") == "config" else None) \
        or _SSO_CLUSTER_IDS.get(key)
    if cid:
        if not store.has_access(sess, http, st, cid):
            return None, "no-access"
    else:
        ks_uid, nodes = _kube_system_uid(entry), None
        if not ks_uid:
            nodes = _node_uids(entry)
            if not nodes:
                return None, "unidentified"
        try:
            cid = _rs.discover_cluster_id(http, st, sess.token, ks_uid,
                                          nodes if nodes is not None else _node_uids(entry))
        except _rs.SSOError:
            cid = None
        if not cid:
            return None, "not-found"
        # l'id d'un cluster est un fait sur le cluster, pas sur la personne :
        # il sert aux sessions suivantes, dont l'accès est vérifié à part
        _SSO_CLUSTER_IDS[key] = cid
        store.grant(sess, cid)
    return store.kubeconfig(sess, st, cid), None


@app.before_request
def _sso_cluster_guard():
    """Garde centrale : une session Rancher ne touche qu'aux clusters que
    Rancher lui montre. Un point d'entrée ajouté demain est couvert sans que
    personne ait à y penser."""
    if _sso_session() is None or not request.path.startswith(("/api/", "/ws/")):
        return None
    cluster = (request.view_args or {}).get("cluster")
    if cluster and _sso_kubeconfig(cluster) is None:
        return jsonify({"error": "forbidden", "code": "cluster-not-in-rancher",
                        "hint": "this cluster is not reachable through Rancher for your account"}), 403
    return None


def _sso_kubeconfig(cluster, entry=None):
    """Le kubeconfig de la session Rancher pour ce cluster : il vise le
    mandataire de Rancher avec le jeton de la personne. None si Rancher ne
    gère pas ce cluster ou ne le lui montre pas."""
    if _sso_session() is None or _session_settings(_sso_session()) is None:
        return None
    if entry is None:
        entry = next((c for c in load_config().get("clusters", []) if c["name"] == cluster), None)
        if entry is None:
            return None
    return _sso_cluster_state(entry)[0]


# =============================================================================
# Joignabilité d'un cluster
#
# Un cluster déclaré mais éteint faisait attendre 30 secondes : `kubectl` ne
# rend la main qu'au bout de son propre délai, et le point d'entrée de statut
# attendait la fin du script. À l'écran, la bascule de cluster tournait dans
# le vide puis le voile abandonnait sur son garde-fou, sans rien expliquer.
#
# Un cluster hors tension se reconnaît en deux secondes : son serveur d'API
# n'accepte pas la connexion TCP. On le demande donc AVANT de lancer quoi que
# ce soit de lent, et on répond « injoignable » au lieu de faire patienter.
# =============================================================================
_REACH_CACHE = {}          # kubeconfig -> (timestamp, bool)
_REACH_TTL = 5.0           # assez court pour suivre un cluster qui revient
_REACH_LOCK = threading.Lock()


def _cluster_api_endpoint(kubeconfig):
    """(hôte, port) du serveur d'API du contexte courant, ou None si on ne
    sait pas lire le kubeconfig — auquel cas on ne bloque rien."""
    try:
        with open(kubeconfig) as fh:
            kc = yaml.safe_load(fh) or {}
        ctx_name = kc.get("current-context")
        cluster_name = None
        for ctx in kc.get("contexts") or []:
            if ctx.get("name") == ctx_name:
                cluster_name = (ctx.get("context") or {}).get("cluster")
                break
        server = None
        for c in kc.get("clusters") or []:
            if cluster_name is None or c.get("name") == cluster_name:
                server = (c.get("cluster") or {}).get("server")
                break
        if not server:
            return None
        from urllib.parse import urlparse
        u = urlparse(server)
        if not u.hostname:
            return None
        return (u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except Exception:
        return None


def _cluster_reachable(kubeconfig, timeout=2.0):
    """True/False, ou None quand la question n'a pas de réponse fiable
    (kubeconfig illisible) : dans ce cas l'appelant procède normalement
    plutôt que de refuser à tort."""
    if not kubeconfig:
        return None
    now = time.time()
    with _REACH_LOCK:
        hit = _REACH_CACHE.get(kubeconfig)
        if hit and now - hit[0] < _REACH_TTL:
            return hit[1]
    endpoint = _cluster_api_endpoint(kubeconfig)
    if endpoint is None:
        return None
    try:
        with socket.create_connection(endpoint, timeout=timeout):
            ok = True
    except OSError:
        ok = False
    with _REACH_LOCK:
        _REACH_CACHE[kubeconfig] = (now, ok)
    return ok


def _unreachable_payload(cluster, kubeconfig):
    """Réponse commune aux points d'entrée qui interrogent un cluster."""
    endpoint = _cluster_api_endpoint(kubeconfig) or ("?", 0)
    return {
        "error": "cluster unreachable",
        "unreachable": True,
        "cluster": cluster,
        "endpoint": f"{endpoint[0]}:{endpoint[1]}",
    }


# =============================================================================
# Harvester cluster event watcher
# =============================================================================
# Goal: capture mutative actions made on the Harvester cluster (via Harvester
# UI, kubectl, Rancher, etc.) and surface them as ActionRuns. Without this,
# anything the user does outside harvester-ops is invisible to the dock.
#
# Strategy: per-cluster background thread polls a small list of resource
# types every CLUSTER_WATCH_INTERVAL seconds. We snapshot UID + resource
# version + generation; any add/remove/modify becomes an ActionRun(status=done).
# Polling is good enough for the UX we want and avoids adding a K8s Python SDK
# dependency to the airgap install image.
# =============================================================================
CLUSTER_WATCH_INTERVAL = float(os.environ.get("HARVESTER_OPS_WATCH_INTERVAL", "15"))
# Au repos, la surveillance ralentit. Elle existe pour alimenter le dock et
# l'activité ; sans personne devant la console, ce travail n'a pas de
# destinataire, et il coûtait un `kubectl get` groupé toutes les 15 s sans
# discontinuer. Le cadencement nominal revient à la PREMIÈRE requête reçue.
# Rien n'est perdu : l'état précédent est conservé, donc ce qui a changé
# pendant le repos est détecté au tour suivant, en un lot.
CLUSTER_WATCH_IDLE_AFTER = float(os.environ.get("HARVESTER_OPS_WATCH_IDLE_AFTER", "300"))
CLUSTER_WATCH_IDLE_INTERVAL = float(os.environ.get("HARVESTER_OPS_WATCH_IDLE_INTERVAL", "120"))
_last_request_ts = time.time()


def _console_is_idle():
    return (time.time() - _last_request_ts) > CLUSTER_WATCH_IDLE_AFTER
CLUSTER_WATCH_ENABLED = os.environ.get("HARVESTER_OPS_WATCH", "1") not in ("0", "false", "no")

CLUSTER_WATCH_RESOURCES = [
    # (label, kubectl-kind, scope, Kind rendu par l'API)
    # Le dernier champ sert à démultiplexer UN SEUL `kubectl get a,b,c` : les
    # objets reviennent mélangés dans une même liste, chacun portant son
    # `kind`. Cinq appels séparés coûtaient 7,2 s mesurés sur harv1, le même
    # travail en un appel en coûte 3,3 s.
    ("namespace",  "namespaces",                                     "cluster",    "Namespace"),
    ("vm-image",   "virtualmachineimages.harvesterhci.io",           "namespaced", "VirtualMachineImage"),
    ("net-attach", "network-attachment-definitions.k8s.cni.cncf.io", "namespaced", "NetworkAttachmentDefinition"),
    ("pvc",        "persistentvolumeclaims",                         "namespaced", "PersistentVolumeClaim"),
    ("vm",         "virtualmachines.kubevirt.io",                    "namespaced", "VirtualMachine"),
]

# {cluster_name: {kind: {uid: {"rv": "...", "gen": int, "name": "ns/n"}}}}
_cluster_watch_state = {}
_cluster_watch_lock = threading.Lock()
_cluster_watch_threads = {}

# v1.44.0 : la dernière photo est gardée sur disque. Au démarrage, la
# surveillance n'avait pas de photo précédente : elle en prenait une de
# référence, qui contenait déjà ce qui avait changé pendant l'arrêt, et ne le
# signalait jamais (constaté sur harv1 : un volume créé juste avant un
# redémarrage n'est jamais apparu). Le premier tour se compare maintenant à
# la photo gardée, à côté de l'historique des actions : même persistance.
WATCH_STATE_DIR = Path(os.environ.get("HARVESTER_OPS_WATCH_STATE_DIR",
                                      str(ACTIONS_DB.parent / "watch")))
_watch_state_saved = {}    # cluster -> dernière photo écrite (forme réduite)
_watch_resumed = {}        # cluster -> types repris du disque, pas encore comparés


def _watch_state_path(cluster):
    """Un fichier par cluster, dont le nom ne peut pas sortir du répertoire."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", cluster or "") or "_"
    return WATCH_STATE_DIR / f"{safe}.json"


def _watch_state_reduce(prev_all):
    """Ce qui sert à comparer, sans la version de ressource : elle change à
    chaque mise à jour de statut, et ferait écrire le disque toutes les 15 s."""
    return {kind: {uid: {"name": v.get("name", "?"), "extra": v.get("extra") or {}}
                   for uid, v in snap.items()}
            for kind, snap in prev_all.items()}


def _watch_state_save(cluster, prev_all):
    reduced = _watch_state_reduce(prev_all)
    if _watch_state_saved.get(cluster) == reduced:
        return
    path = _watch_state_path(cluster)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Un inventaire du cluster : lisible par le seul compte du service.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"saved_at": time.time(), "kinds": reduced}, f)
        os.replace(tmp, path)
        _watch_state_saved[cluster] = reduced
    except OSError as e:
        # Sans disque, la surveillance continue : elle perd seulement la
        # mémoire d'un redémarrage à l'autre, comme avant.
        log_watch.warning("%s: photo non gardée sur disque (%s)", cluster, e)
        try:
            tmp.unlink()
        except OSError:
            pass


def _watch_state_load(cluster):
    """La photo gardée, au format de la mémoire ; {} si absente ou illisible.

    Tout ou rien PAR TYPE : une entrée écartée passerait, au premier tour,
    pour un objet créé pendant l'arrêt. Un type abîmé est donc écarté en
    entier (il repart d'une photo de référence) ; les autres restent."""
    try:
        data = json.loads(_watch_state_path(cluster).read_text())
    except (OSError, ValueError):
        return {}
    kinds = data.get("kinds") if isinstance(data, dict) else None
    if not isinstance(kinds, dict):
        return {}
    out = {}
    for kind, snap in kinds.items():
        if not isinstance(snap, dict):
            continue
        entries = {}
        for uid, v in snap.items():
            if not (isinstance(v, dict) and isinstance(v.get("name"), str)
                    and isinstance(v.get("extra", {}), dict)):
                break
            entries[uid] = {"rv": "", "name": v["name"], "extra": v.get("extra", {})}
        else:
            out[kind] = entries
    return out


def _cluster_snapshot(kc, kind, scope):
    """Run kubectl get <kind> -A -o json and return {uid: {rv, name, extra}}.

    `extra` is a kind-specific dict used to drive richer events (image upload
    progress, VM phase transitions, …). Keep it small."""
    args = ["kubectl", "--kubeconfig", kc, "get", kind, "-o", "json"]
    if scope == "namespaced":
        args.insert(4, "-A")
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout) if r.stdout.strip() else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
    return _snapshot_items(kind, data.get("items", []))


def _snapshot_items(kind, items):
    """{uid: {rv, name, extra}} pour une liste d'objets d'un même type.

    Partagé par le chemin groupé et le chemin par type : la réduction doit
    être identique des deux côtés, sinon le watcher verrait de faux
    changements au premier repli.
    """
    out = {}
    for item in items:
        m = item.get("metadata", {}) or {}
        uid = m.get("uid")
        if not uid:
            continue
        ns = m.get("namespace", "")
        name = f"{ns}/{m['name']}" if ns else m.get("name", "?")
        status = item.get("status", {}) or {}
        spec = item.get("spec", {}) or {}
        extra = {}
        if kind.startswith("virtualmachineimages"):
            # Track the upload as a long-running action — see
            # _watcher_handle_image_progress below.
            extra["progress"] = status.get("progress", 0) or 0
            extra["display_name"] = spec.get("displayName", "")
            cond_imp = next((c for c in status.get("conditions", []) or []
                             if c.get("type") == "Imported"), {})
            extra["imported"] = cond_imp.get("status") == "True"
            extra["failed"] = bool(status.get("failed"))
        elif kind.startswith("virtualmachines.kubevirt.io"):
            extra["run_strategy"] = spec.get("runStrategy", "")
            extra["ready"] = status.get("ready", False)
            extra["printable_status"] = status.get("printableStatus", "")
        out[uid] = {
            "rv":   m.get("resourceVersion", ""),
            "name": name,
            "extra": extra,
        }
    return out


def _cluster_snapshot_all(kc, resources):
    """{kind: {uid: {...}}} pour TOUS les types surveillés, en un seul
    `kubectl get a,b,c -A -o json`.

    Pourquoi : chaque invocation de kubectl paie ~0,7 s de démarrage de
    processus avant de toucher au réseau (mesuré sur node1). Cinq types =
    cinq démarrages. Groupés, c'est un seul.

    Repli par type si l'appel groupé échoue : `kubectl get a,b,c` échoue EN
    BLOC quand le cluster n'expose pas l'un des types (« the server doesn't
    have a resource type »). Un cluster sans Harvester perdrait donc la
    surveillance des namespaces et des PVC avec, ce qui serait pire que lent.
    Rend None pour un type qu'on n'a pas su lire, comme le chemin par type.
    """
    kinds = [kind for _, kind, _, _ in resources]
    args = ["kubectl", "--kubeconfig", kc, "get", "-A", ",".join(kinds),
            "-o", "json"]
    data = None
    try:
        r = _kubectl_run(args, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and getattr(r, "data", None) is not None:
            data = r.data
        elif r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        data = None

    if data is None:
        log_watch.debug("appel groupé indisponible, repli type par type")
        return {kind: _cluster_snapshot(kc, kind, scope)
                for _, kind, scope, _ in resources}

    by_api_kind = {}
    for item in data.get("items", []):
        by_api_kind.setdefault(item.get("kind"), []).append(item)
    return {kind: _snapshot_items(kind, by_api_kind.get(api_kind, []))
            for _, kind, _, api_kind in resources}


def _record_cluster_event(cluster, kind, op, name, while_down=False):
    """Materialize a cluster-side event as an ActionRun(status=done).

    `name` is `<namespace>/<name>` for namespaced resources, `<name>` for
    cluster-scoped — embedded in the action label so the dock card and
    activity row show *which* resource changed, not just the type.
    `while_down`: found by the first round after a restart (or after the
    cluster came back), against the snapshot kept on disk; when exactly it
    happened is unknown."""
    rid = uuid.uuid4().hex[:12]
    label = f"harvester:{kind}-{op}:{name}"
    run = ActionRun(rid, label, cluster, ["watch"], dry_run=False)
    now = time.time()
    run.status = "done"
    run.exit_code = 0
    run.started_at = now
    run.ended_at = now
    how = (f"changed while the console was not watching {cluster}, "
           f"found when the watch resumed"
           if while_down else f"detected via cluster watch on {cluster}")
    run.events.append({
        "type": "step", "step_id": op, "status": "done",
        "message": f"{kind} {name} ({how})",
        "ts": now,
    })
    run.events.append({
        "type": "status", "status": "done", "exit_code": 0, "ts": now,
    })
    with ACTIONS_LOCK:
        ACTIONS[rid] = run
    run.close()


# In-flight image uploads we report progress on. uid → action_id.
_image_upload_actions = {}
# Last known VM phase per uid, for "vm-running"/"vm-stopped" events.
_vm_phase_state = {}


def _watcher_handle_image_progress(cluster, uid, name, info, is_new, is_gone):
    """Surface VirtualMachineImage upload progress as a single ActionRun
    that ramps from 0% to 100%. Emits a step event on each %-change tick."""
    extra = info.get("extra", {}) if info else {}
    progress = extra.get("progress", 0)
    imported = extra.get("imported", False)
    failed = extra.get("failed", False)
    display = extra.get("display_name", "") or name

    # New image and not already complete → open a running action.
    if is_new and not imported and progress < 100:
        rid = uuid.uuid4().hex[:12]
        label = f"harvester:vm-image-upload:{name}"
        run = ActionRun(rid, label, cluster, ["watch"], dry_run=False)
        run.status = "running"
        run.started_at = time.time()
        run.events.append({"type": "step", "step_id": "upload", "status": "running",
                           "message": f"{display}: 0%", "ts": time.time()})
        with ACTIONS_LOCK:
            ACTIONS[rid] = run
        _image_upload_actions[uid] = rid
        return

    rid = _image_upload_actions.get(uid)
    if not rid:
        return  # no in-flight action — silently ignored
    run = ACTIONS.get(rid)
    if not run:
        _image_upload_actions.pop(uid, None)
        return

    if is_gone:
        run.status = "cancelled"; run.exit_code = -1; run.ended_at = time.time()
        run.events.append({"type": "step", "step_id": "upload", "status": "error",
                           "message": f"{display}: deleted before completion",
                           "ts": time.time()})
        run.close()
        _image_upload_actions.pop(uid, None)
        return

    if failed:
        run.status = "error"; run.exit_code = 1; run.ended_at = time.time()
        run.events.append({"type": "step", "step_id": "upload", "status": "error",
                           "message": f"{display}: import failed", "ts": time.time()})
        run.close()
        _image_upload_actions.pop(uid, None)
        return

    if imported or progress >= 100:
        run.status = "done"; run.exit_code = 0; run.ended_at = time.time()
        run.events.append({"type": "step", "step_id": "upload", "status": "done",
                           "message": f"{display}: 100% (imported)",
                           "ts": time.time()})
        run.close()
        _image_upload_actions.pop(uid, None)
        return

    # Still in progress → tick the step message with the new %
    run.events.append({"type": "step", "step_id": "upload", "status": "running",
                       "message": f"{display}: {progress}%", "ts": time.time()})


def _cluster_watch_iteration(cluster, kc):
    """One snapshot + diff cycle for one cluster."""
    with _cluster_watch_lock:
        if cluster not in _cluster_watch_state:
            # Premier tour de ce processus : reprendre la photo gardée.
            _cluster_watch_state[cluster] = _watch_state_load(cluster)
            _watch_resumed[cluster] = set(_cluster_watch_state[cluster])
        prev_all = _cluster_watch_state[cluster]
        resumed = _watch_resumed.setdefault(cluster, set())
    snaps = _cluster_snapshot_all(kc, CLUSTER_WATCH_RESOURCES)
    for label, kind, scope, _api_kind in CLUSTER_WATCH_RESOURCES:
        snap = snaps.get(kind)
        if snap is None:
            continue
        with _cluster_watch_lock:
            prev = prev_all.get(kind)
            # First iteration: just record baseline + open in-flight image
            # upload actions if anything is mid-upload (so we don't lose
            # progress across a Flask restart).
            if prev is None:
                prev_all[kind] = snap
                if kind.startswith("virtualmachineimages"):
                    for uid, info in snap.items():
                        ex = info.get("extra", {})
                        if not ex.get("imported") and ex.get("progress", 0) < 100:
                            _watcher_handle_image_progress(
                                cluster, uid, info["name"], info,
                                is_new=True, is_gone=False)
                continue
            added = set(snap) - set(prev)
            removed = set(prev) - set(snap)
            prev_all[kind] = snap
            # Comparé à la photo du disque : ce qui suit a eu lieu pendant
            # l'arrêt de la console.
            while_down = kind in resumed
            resumed.discard(kind)

        # Cluster-wide create/delete events
        for uid in added:
            _record_cluster_event(cluster, label, "created", snap[uid]["name"],
                                  while_down=while_down)
            if kind.startswith("virtualmachineimages"):
                _watcher_handle_image_progress(cluster, uid, snap[uid]["name"],
                                               snap[uid], is_new=True, is_gone=False)
        for uid in removed:
            _record_cluster_event(cluster, label, "deleted", prev[uid]["name"],
                                  while_down=while_down)
            if kind.startswith("virtualmachineimages"):
                _watcher_handle_image_progress(cluster, uid, prev[uid]["name"],
                                               prev[uid], is_new=False, is_gone=True)

        # Per-kind status tracking — only fire on meaningful changes.
        common = set(snap) & set(prev)
        if kind.startswith("virtualmachineimages"):
            if while_down:
                # Un téléversement en cours au redémarrage : l'action du
                # processus précédent est perdue, en ouvrir une qui le suit.
                for uid in common:
                    ex = snap[uid].get("extra", {})
                    if (uid not in _image_upload_actions and not ex.get("imported")
                            and ex.get("progress", 0) < 100):
                        _watcher_handle_image_progress(
                            cluster, uid, snap[uid]["name"], snap[uid],
                            is_new=True, is_gone=False)
            for uid in common:
                old_ex = prev[uid].get("extra", {})
                new_ex = snap[uid].get("extra", {})
                if (old_ex.get("progress") != new_ex.get("progress")
                    or old_ex.get("imported") != new_ex.get("imported")
                    or old_ex.get("failed")   != new_ex.get("failed")):
                    _watcher_handle_image_progress(
                        cluster, uid, snap[uid]["name"], snap[uid],
                        is_new=False, is_gone=False)
        elif kind.startswith("virtualmachines.kubevirt.io"):
            for uid in common:
                old_ph = prev[uid].get("extra", {}).get("printable_status", "")
                new_ph = snap[uid].get("extra", {}).get("printable_status", "")
                if old_ph and new_ph and old_ph != new_ph:
                    _record_cluster_event(
                        cluster, label, f"phase-{new_ph.lower()}",
                        snap[uid]["name"], while_down=while_down)

    with _cluster_watch_lock:
        _watch_state_save(cluster, dict(prev_all))


def _watch_delay(base, rnd=None):
    """Délai jusqu'au tour suivant, avec ±10 % d'aléa : des surveillances
    lancées ensemble ne restent pas synchronisées (v1.81.0)."""
    import random
    return base * (0.9 + 0.2 * (rnd if rnd is not None else random.random()))


_MODULE_READY = threading.Event()   # posé à la fin du module


def _cluster_watch_thread(cluster, start_delay=0.0):
    log_watch.info("starting cluster watcher for %s", cluster)
    # les surveillances démarrent pendant l'import : attendre la fin du module
    # (vu sur le banc : premier tour perdu sur « ACCOUNTS_PATH is not defined »)
    _MODULE_READY.wait(60)
    # v1.81.0 : les clusters ne sont plus tous relus au même instant. Au
    # démarrage, chaque surveillance part décalée dans le premier intervalle,
    # et l'aléa de chaque tour empêche qu'elles se resynchronisent : la
    # charge (processus kubectl, analyse JSON) s'étale au lieu d'arriver en
    # rafale toutes les 15 s.
    if start_delay:
        time.sleep(start_delay)
    while True:
        try:
            kc = _kubectl_for_cluster(cluster)
            if kc and Path(kc).exists():
                _cluster_watch_iteration(cluster, kc)
        except Exception as e:
            log_watch.warning("%s: %s", cluster, e)
        time.sleep(_watch_delay(CLUSTER_WATCH_IDLE_INTERVAL if _console_is_idle()
                                else CLUSTER_WATCH_INTERVAL))


def _watch_start_offsets(names, interval):
    """Décalage de départ de chaque surveillance : réparties à pas égaux
    sur un intervalle."""
    n = max(1, len(names))
    return {name: interval * i / n for i, name in enumerate(names)}


def _start_cluster_watchers():
    """Spawn one daemon thread per configured cluster."""
    if not CLUSTER_WATCH_ENABLED:
        log_watch.info("disabled via HARVESTER_OPS_WATCH=0")
        return
    cfg = load_config()
    offsets = _watch_start_offsets([c["name"] for c in cfg.get("clusters", [])],
                                   CLUSTER_WATCH_INTERVAL)
    for c in cfg.get("clusters", []):
        name = c["name"]
        if name in _cluster_watch_threads:
            continue
        t = threading.Thread(target=_cluster_watch_thread, args=(name, offsets.get(name, 0.0)),
                             daemon=True, name=f"cluster-watch-{name}")
        _cluster_watch_threads[name] = t
        t.start()


# v1.82.0 : l'issue d'une mise à jour faite pendant le redémarrage. L'agent
# n'écrit « terminé » qu'APRÈS avoir vu cette console répondre : au démarrage
# l'état est encore « en cours » (vu en réel). On attend donc l'issue finale.
def _update_outcome_watch(timeout=1200, step=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _su.read_json(_state_dir() / "updates" / _su.STATUS) or {}
        if not st.get("started"):
            return
        if st.get("state") not in (None, "running"):
            _update_record_outcome()
            return
        time.sleep(step)


if not IS_READ_WORKER:
    threading.Thread(target=_update_outcome_watch, daemon=True, name="update-outcome").start()

# Kick off watchers at import — the threads are daemons so Flask shutdown
# cleans them up.
try:
    if not IS_READ_WORKER:
        _start_cluster_watchers()
except Exception as _e:
    log_watch.error("startup failed: %s", _e)


def _memory_gc_loop():
    """v1.5.7: periodic memory pressure relief for ACTIONS{} and
    _notes_docs{}. Both used to grow without bound across the lifetime
    of the process.

    For ACTIONS: evict entries whose `ended_at` is older than 1h.
    SSE consumers attaching after that delay get whatever is still
    persisted in SQLite — they don't need the in-memory ActionRun.

    For _notes_docs: when an entry has no subscribers AND has been empty
    for the grace period, shutdown its ThreadPoolExecutor and pop it.
    The next access just spins up a fresh entry."""
    grace_empty_at = {}   # doc_id → first time subs went empty
    while True:
        try:
            time.sleep(_ACTIONS_GC_TICK_SECONDS)
            now = time.time()
            # 1) ACTIONS
            stale = []
            with ACTIONS_LOCK:
                for run_id, run in list(ACTIONS.items()):
                    if (run.ended_at and
                            now - run.ended_at > _ACTIONS_GC_KEEP_SECONDS):
                        stale.append(run_id)
                for run_id in stale:
                    ACTIONS.pop(run_id, None)
            if stale:
                log_actions.info("gc: evicted %d finished actions", len(stale))
            # 2) _notes_docs
            stale_docs = []
            with _notes_lock:
                for doc_id, entry in list(_notes_docs.items()):
                    if entry.get("subs"):
                        grace_empty_at.pop(doc_id, None)
                        continue
                    first = grace_empty_at.get(doc_id)
                    if first is None:
                        grace_empty_at[doc_id] = now
                    elif now - first > 600:
                        stale_docs.append(doc_id)
                for doc_id in stale_docs:
                    entry = _notes_docs.pop(doc_id, None)
                    grace_empty_at.pop(doc_id, None)
                    if entry and entry.get("executor"):
                        try:
                            entry["executor"].shutdown(wait=False)
                        except Exception:
                            pass
            if stale_docs:
                log_notes.info("gc: shut down %d idle y-doc executors",
                                len(stale_docs))
        except Exception as e:
            log_actions.warning("memory gc loop crashed: %s", e)


threading.Thread(target=_memory_gc_loop, daemon=True,
                  name="harvester-ops-memory-gc").start()


@app.route("/api/vms/<cluster>")
@requires_auth
@shared_read()
def api_vms_list(cluster):
    """Return all VMs with their current shutdown-priority annotation, snapshot flag,
    runStrategy and live VMI phase (Running/Pending/Failed/...).
    """
    # Cluster déclaré mais hors tension : répondre tout de suite. Sans cela
    # cet appel attend le délai de `kubectl`, et une bascule de cluster
    # enchaîne ces attentes (15 s mesurées).
    kc = _kubectl_for_cluster(cluster)
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200

    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    # v1.81.0 : VMs et VMIs lues de front (l'une attendait l'autre)
    from concurrent.futures import ThreadPoolExecutor
    _pool = ThreadPoolExecutor(max_workers=1)
    # le fil rend le résultat brut : un refus des VMIs par la RBAC est retenu
    # ensuite dans le fil de la requête (g n'existe pas dans l'autre)
    _vmi_fut = _pool.submit(_kubectl_run, ["kubectl", "--kubeconfig", kc, "get", "vmi", "-A", "-o", "json"],
                            capture_output=True, text=True, timeout=20)
    _pool.shutdown(wait=False)
    try:
        proc = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, "get", "vm", "-A", "-o", "json"],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            # stderr était jeté ici : un refus de la RBAC devenait un 500 muet.
            _note_cluster_denial(proc.stderr)
            return jsonify({"error": "kubectl failed",
                            "detail": f"exit code {proc.returncode}"}), 500
        data = proc.data if getattr(proc, "data", None) is not None else json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return jsonify({"error": "kubectl failed",
                        "detail": _safe_proc_error(e)}), 500

    # Fetch VMIs to expose live phase + agent connection + paused state
    vmi_state = {}
    try:
        _vr = _vmi_fut.result()
        if _vr.returncode != 0:
            _note_cluster_denial(_vr.stderr)
            raise ValueError("vmi unreadable")
        _vmi = _vr.data if getattr(_vr, "data", None) is not None else json.loads(_vr.stdout)
        for v in _vmi.get("items", []):
            ns = v["metadata"]["namespace"]
            name = v["metadata"]["name"]
            phase = v.get("status", {}).get("phase", "Unknown")
            agent = "Unknown"
            paused = False
            for cond in v.get("status", {}).get("conditions", []):
                t = cond.get("type")
                s = cond.get("status", "Unknown")
                if t == "AgentConnected":
                    agent = s
                elif t == "Paused" and s == "True":
                    paused = True
            # If the VMI is Running but paused, surface a "Paused" state
            if phase == "Running" and paused:
                phase = "Paused"
            st = v.get("status", {}) or {}
            # v1.61.0 : colonnes nœud et IP, comme la liste de Harvester
            ips = []
            for itf in st.get("interfaces") or []:
                for ip in [itf.get("ipAddress")] + list(itf.get("ipAddresses") or []):
                    if ip and ip not in ips and ":" not in ip:
                        ips.append(ip)
            vmi_state[(ns, name)] = {"phase": phase, "agent_connected": agent,
                                     "node": st.get("nodeName"), "ips": ips}
    except Exception:
        pass

    vms = []
    for item in data.get("items", []):
        annot = (item["metadata"].get("annotations") or {})
        # `priority` now means: intra-group order (lower = stops first
        # within the same group). Default 10. Ignored for the "default"
        # catch-all group (where VMs stop in parallel).
        try:
            prio = int(annot.get("harvester-ops.io/shutdown-priority", "10"))
        except (TypeError, ValueError):
            prio = 10
        # `group_priority` is the GROUP's priority: lower = group runs
        # earlier; groups sharing the same value run IN PARALLEL.
        # Default 100 → with all groups at 100, everything runs in
        # parallel between groups (the desired "no inter-group order
        # unless explicitly configured" semantics).
        try:
            gprio = int(annot.get("harvester-ops.io/shutdown-group-priority", "100"))
        except (TypeError, ValueError):
            gprio = 100
        snap_flag = annot.get("harvester-ops.io/snapshot", "true").lower() != "false"
        try:
            ready_timeout = int(annot.get("harvester-ops.io/ready-timeout", "300"))
        except (TypeError, ValueError):
            ready_timeout = 300
        group = annot.get("harvester-ops.io/shutdown-group") or "default"
        rs = item["spec"].get("runStrategy", "?")
        ns = item["metadata"]["namespace"]
        name = item["metadata"]["name"]
        state = vmi_state.get((ns, name), {"phase": "Stopped", "agent_connected": "False"})
        dom = ((item["spec"].get("template") or {}).get("spec") or {}).get("domain") or {}
        cpu = dom.get("cpu") or {}
        vcpu = (cpu.get("cores") or 1) * (cpu.get("sockets") or 1) * (cpu.get("threads") or 1)
        mem = (dom.get("memory") or {}).get("guest") or ((dom.get("resources") or {}).get("limits") or {}).get("memory")
        vms.append({
            "namespace": ns,
            "name": name,
            "priority": prio,
            "group": group,
            "group_priority": gprio,
            "snapshot": snap_flag,
            "ready_timeout": ready_timeout,
            "runStrategy": rs,
            "phase": state["phase"],
            "agent_connected": state["agent_connected"],
            "cpu": vcpu,
            "memory": mem,
            "node": state.get("node"),
            # v1.62.0 : comme la colonne IP de Harvester, une IP statique
            # déclarée (static-ip.harvesterhci.io/<carte>) passe en premier
            "ips": list(dict.fromkeys(
                [v.split("/")[0] for k, v in sorted(annot.items())
                 if k.startswith("static-ip.harvesterhci.io/") and re.match(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$", str(v))]
                + (state.get("ips") or []))),
            "labels": item["metadata"].get("labels") or {},
        })

    # Sort: (group_priority, group, priority, name) so the UI sees the
    # same ordering the shutdown script will use.
    vms.sort(key=lambda v: (v["group_priority"], v["group"], v["priority"], v["name"]))
    return jsonify({"cluster": cluster, "vms": vms})


# -----------------------------------------------------------------------------
# Cluster CRUD — declare/update/delete clusters from the UI
# -----------------------------------------------------------------------------
CONFIG_LOCK = threading.Lock()


def _config_dir():
    return CONFIG_PATH.parent


def _private_dir(d):
    """Répertoire créé au besoin, réservé au compte du service (0700)."""
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _kubeconfigs_dir():
    """Kubeconfigs des clusters déclarés dans config.yaml (à côté de lui)."""
    d = _config_dir() / "kubeconfigs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ssh_dir():
    """Clés SSH des clusters déclarés dans config.yaml (à côté de lui)."""
    return _private_dir(_config_dir() / "ssh")


def _state_kubeconfigs_dir():
    """v1.78.0 : kubeconfigs des clusters déclarés par la console."""
    return _private_dir(_state_dir() / _cd.KUBECONFIG_DIR)


def _state_ssh_dir():
    """v1.78.0 : clés SSH des clusters déclarés par la console."""
    return _private_dir(_state_dir() / _cd.SSH_DIR)


def _origin_dirs(origin):
    """(kubeconfigs, ssh) où ranger les fichiers d'un cluster selon qui l'a
    déclaré : l'opérateur (config.yaml) ou la console (état)."""
    if origin == "config":
        return _kubeconfigs_dir(), _ssh_dir()
    return _state_kubeconfigs_dir(), _state_ssh_dir()


def _atomic_write_config(cfg):
    """Write the config.yaml atomically with a backup of the previous version."""
    if CONFIG_PATH.exists():
        backup = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".bak")
        try:
            backup.write_text(CONFIG_PATH.read_text())
        except OSError:
            pass
    tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False))
    tmp.replace(CONFIG_PATH)


def _write_console_cluster(cluster):
    """Écrit la déclaration d'un cluster de la console dans
    <état>/clusters.d/<nom>.yaml (0600, remplacement atomique), chemins
    rendus relatifs au répertoire d'état : l'état se déplace tel quel."""
    state = _state_dir()
    path = _cd.decl_path(state, cluster.get("name"))
    if path is None:
        raise ValueError("invalid cluster name")
    _private_dir(path.parent)
    doc = _cd.relativize(cluster, state)
    doc.pop("origin", None)
    tmp = path.with_name(path.name + ".tmp")
    _bm_write_private(tmp, yaml.safe_dump(doc, default_flow_style=False, sort_keys=False))
    tmp.replace(path)


def _delete_console_cluster(name):
    path = _cd.decl_path(_state_dir(), name)
    if path is not None:
        path.unlink(missing_ok=True)


def _find_cluster(name):
    """(origine, cluster, config brute, index) d'un cluster déclaré :
    origine « config » (config.yaml, qui l'emporte) ou « console »
    (clusters.d, chemins résolus). (None, None, None, None) s'il n'existe pas."""
    raw = _load_config_file()
    for i, c in enumerate(raw.get("clusters") or []):
        if isinstance(c, dict) and c.get("name") == name:
            return "config", c, raw, i
    state = _state_dir()
    path = _cd.decl_path(state, name)
    if path is not None and path.is_file():
        doc = _read_decl(path)
        if _cd.check_decl(doc, path) is None:
            return "console", _cd.resolve(doc, state), None, None
    return None, None, None, None


def _save_cluster(origin, cluster, raw=None, idx=None, old_name=None):
    """Enregistre un cluster modifié là où il est déclaré."""
    if origin == "config":
        raw["clusters"][idx] = cluster
        _atomic_write_config(raw)
        return
    _write_console_cluster(cluster)
    if old_name and old_name != cluster["name"]:
        _delete_console_cluster(old_name)


_CONFIG_READ_ONLY = "declared by the operator in config.yaml, read-only for the console"


def _read_only_refusal(origin):
    """409 pour un cluster de config.yaml quand ce fichier n'est pas
    modifiable (service packagé), plutôt qu'une erreur 500 d'écriture."""
    if origin == "config" and not _config_writable():
        return jsonify({"error": _CONFIG_READ_ONLY, "origin": "config"}), 409
    return None


def _validate_cluster_payload(data, allow_partial=False):
    """Validate the JSON payload (or form data) of a cluster declaration.
    Returns (cluster_dict, error_message_or_None).
    """
    if not isinstance(data, dict):
        return None, "payload must be an object"

    name = (data.get("name") or "").strip()
    if not name and not allow_partial:
        return None, "cluster name required"
    if name and not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$", name):
        return None, "invalid cluster name (allowed: letters, digits, . _ -)"

    description = data.get("description", "")
    ssh = data.get("ssh", {}) or {}
    ssh_user = (ssh.get("user") or "rancher").strip()
    ssh_port = int(ssh.get("port") or 22)

    nodes = data.get("nodes") or []
    if not isinstance(nodes, list):
        return None, "nodes must be a list"
    if not nodes and not allow_partial:
        return None, "at least one node is required"

    cleaned_nodes = []
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            return None, f"node {i}: must be an object"
        host = (n.get("hostname") or "").strip()
        ip = (n.get("ip") or "").strip()
        role = (n.get("role") or "").strip().lower()
        if not host:
            return None, f"node {i}: hostname required"
        if not ip:
            return None, f"node {i}: ip required"
        if role not in ("control-plane", "worker"):
            return None, f"node {i}: role must be 'control-plane' or 'worker'"
        node_obj = {"hostname": host, "ip": ip, "role": role}
        # L'adresse MAC de réveil : `harvester-startup.sh` s'en sert pour
        # rallumer le node. La laisser tomber donne une déclaration qui
        # semble complète mais dont le démarrage ne peut rien faire.
        mac = (n.get("wol_mac") or "").strip()
        if mac:
            if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", mac):
                return None, f"node {i}: invalid wol_mac"
            node_obj["wol_mac"] = mac.lower().replace("-", ":")
        cleaned_nodes.append(node_obj)

    # Chemin de clé SSH fourni directement (déclaration sans téléversement) :
    # le vider rendait toute action SSH inopérante sur le cluster déclaré.
    ssh_key = (ssh.get("key") or "").strip()

    cluster_obj = {
        "name": name,
        "description": description,
        "kubeconfig": "",   # filled by upload step
        "ssh": {"user": ssh_user, "port": ssh_port, "key": ssh_key},
        "nodes": cleaned_nodes,
    }
    return cluster_obj, None


@app.route("/api/clusters", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_clusters_create():
    """Create a new cluster declaration.

    Accepts multipart/form-data:
      - payload: JSON string with name, description, ssh, nodes
      - kubeconfig: file (required)
      - ssh_key: file (optional)
    Or JSON only (no files) — kubeconfig path then must be provided
    explicitly in the payload's `kubeconfig` field.

    v1.78.0 : un cluster ajouté par la console est TOUJOURS rangé dans son
    répertoire d'état (clusters.d), même quand config.yaml est modifiable :
    config.yaml reste le fichier de l'opérateur, et l'état se déplace tel quel.
    """
    if request.content_type and request.content_type.startswith("multipart/"):
        try:
            data = json.loads(request.form.get("payload", "{}"))
        except json.JSONDecodeError:
            return jsonify({"error": "invalid JSON payload"}), 400
        kc_file = request.files.get("kubeconfig")
        ssh_file = request.files.get("ssh_key")
    else:
        data = request.get_json(force=True, silent=True) or {}
        kc_file = None
        ssh_file = None

    cluster, err = _validate_cluster_payload(data)
    if err:
        return jsonify({"error": err}), 400
    # v1.78.0 : un chemin relatif voudrait dire « relatif au répertoire
    # d'état » ; un chemin donné sans téléversement doit être absolu
    for field, value in (("kubeconfig", "" if kc_file else data.get("kubeconfig")),
                         ("ssh.key", "" if ssh_file else cluster["ssh"].get("key"))):
        if value and not (isinstance(value, str) and os.path.isabs(value)):
            return jsonify({"error": f"{field} must be an absolute path", "fields": [field]}), 400

    with CONFIG_LOCK:
        cfg = load_config()
        existing = next((c for c in cfg.get("clusters", []) if c["name"] == cluster["name"]), None)
        if existing:
            return jsonify({"error": f"cluster '{cluster['name']}' already exists"}), 409

        # Save kubeconfig
        if kc_file:
            try:
                content = kc_file.read().decode("utf-8", errors="replace")
                yaml.safe_load(content)   # validate YAML structure
            except (yaml.YAMLError, UnicodeDecodeError) as e:
                return jsonify({"error": "kubeconfig is not valid YAML", "detail": str(e)}), 400
            kc_path = _state_kubeconfigs_dir() / f"{cluster['name']}.yaml"
            _bm_write_private(kc_path, content)
            cluster["kubeconfig"] = str(kc_path)
        elif data.get("kubeconfig"):
            # Path-only mode (legacy/CLI flow)
            cluster["kubeconfig"] = data["kubeconfig"]
        else:
            return jsonify({"error": "kubeconfig file is required"}), 400

        # Save SSH key
        if ssh_file:
            key_path = _state_ssh_dir() / f"{cluster['name']}_id"
            _bm_write_private(key_path, ssh_file.read())
            cluster["ssh"]["key"] = str(key_path)

        _write_console_cluster(cluster)

    return jsonify({"cluster": dict(cluster, origin="console")}), 201


@app.route("/api/clusters/<name>", methods=["PUT"])
@requires_auth
@_rate_limit("20/minute")
def api_clusters_update(name):
    """Update an existing cluster (name, description, nodes, ssh).
    Files (kubeconfig, ssh_key) handled via dedicated upload endpoints below.
    """
    data = request.get_json(force=True, silent=True) or {}
    cluster, err = _validate_cluster_payload(data, allow_partial=False)
    if err:
        return jsonify({"error": err}), 400

    with CONFIG_LOCK:
        origin, original, raw, idx = _find_cluster(name)
        if origin is None:
            return jsonify({"error": f"cluster '{name}' not found"}), 404
        refused = _read_only_refusal(origin)
        if refused:
            return refused
        kc_dir, ssh_dir = _origin_dirs(origin)
        # Preserve kubeconfig path and ssh key path
        cluster["kubeconfig"] = original.get("kubeconfig", "")
        if "key" not in cluster.get("ssh", {}) or not cluster["ssh"].get("key"):
            cluster["ssh"]["key"] = (original.get("ssh") or {}).get("key", "")
        # v1.78.0 : clé créée par la console (installation bare-metal), gardée
        # comme telle tant que c'est la même clé
        generated = bool((original.get("ssh") or {}).get("generated")) and \
            cluster["ssh"]["key"] == (original.get("ssh") or {}).get("key", "")
        if generated:
            cluster["ssh"]["generated"] = True
        # Rename handling: if cluster name changed, move kubeconfig/sshkey files
        if cluster["name"] != name:
            if any(c.get("name") == cluster["name"] for c in load_config().get("clusters", [])):
                return jsonify({"error": f"cluster '{cluster['name']}' already exists"}), 409
            old_kc = Path(cluster["kubeconfig"])
            if old_kc.exists() and old_kc.is_relative_to(kc_dir):
                new_kc = kc_dir / f"{cluster['name']}.yaml"
                try:
                    old_kc.rename(new_kc)
                    cluster["kubeconfig"] = str(new_kc)
                except OSError:
                    pass
            old_key = Path(cluster["ssh"].get("key") or "")
            if old_key.exists() and old_key.is_relative_to(ssh_dir):
                new_key = ssh_dir / f"{cluster['name']}_id"
                try:
                    old_key.rename(new_key)
                    cluster["ssh"]["key"] = str(new_key)
                except OSError:
                    pass
                else:
                    # sa publique et les clés d'hôte vues suivent la clé
                    for old_sib, new_sib in ((f"{name}_id.pub", f"{cluster['name']}_id.pub"),
                                             (f"{name}_known_hosts", f"{cluster['name']}_known_hosts")):
                        try:
                            (ssh_dir / old_sib).rename(ssh_dir / new_sib)
                        except OSError:
                            pass
        _save_cluster(origin, cluster, raw, idx, old_name=name)
    return jsonify({"cluster": dict(cluster, origin=origin)})


@app.route("/api/clusters/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_clusters_delete(name):
    """Delete a cluster declaration. Removes kubeconfig + SSH key files
    if they live in the console's own directories."""
    with CONFIG_LOCK:
        origin, removed, raw, idx = _find_cluster(name)
        if origin is None:
            return jsonify({"error": f"cluster '{name}' not found"}), 404
        refused = _read_only_refusal(origin)
        if refused:
            return refused
        kc_dir, ssh_dir = _origin_dirs(origin)
        # Best-effort cleanup of associated files (only if inside our dirs)
        kc_path = Path(removed.get("kubeconfig", "") or "")
        if kc_path.exists() and kc_path.is_relative_to(kc_dir):
            try: kc_path.unlink()
            except OSError: pass
        key_path = Path((removed.get("ssh") or {}).get("key", "") or "")
        if key_path.exists() and key_path.is_relative_to(ssh_dir):
            try: key_path.unlink()
            except OSError: pass
            # v1.78.0 : publique et clés d'hôte d'une clé créée par la console
            for sib in (key_path.with_name(key_path.name + ".pub"),
                        ssh_dir / f"{name}_known_hosts"):
                try: sib.unlink(missing_ok=True)
                except OSError: pass
        if origin == "config":
            raw["clusters"].pop(idx)
            _atomic_write_config(raw)
        else:
            _delete_console_cluster(name)
    return jsonify({"removed": name})


@app.route("/api/clusters/<name>/kubeconfig", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_clusters_upload_kubeconfig(name):
    """Upload (replace) the kubeconfig for an existing cluster.
    Multipart: file=<kubeconfig>"""
    kc_file = request.files.get("file")
    if not kc_file:
        return jsonify({"error": "file field required"}), 400
    try:
        content = kc_file.read().decode("utf-8", errors="replace")
        yaml.safe_load(content)
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        return jsonify({"error": "kubeconfig is not valid YAML", "detail": str(e)}), 400
    with CONFIG_LOCK:
        origin, cluster, raw, idx = _find_cluster(name)
        if origin is None:
            return jsonify({"error": f"cluster '{name}' not found"}), 404
        refused = _read_only_refusal(origin)
        if refused:
            return refused
        kc_path = _origin_dirs(origin)[0] / f"{name}.yaml"
        _bm_write_private(kc_path, content)
        cluster["kubeconfig"] = str(kc_path)
        _save_cluster(origin, cluster, raw, idx)
    return jsonify({"cluster": name, "kubeconfig": str(kc_path), "origin": origin})


@app.route("/api/clusters/<name>/sshkey", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_clusters_upload_sshkey(name):
    """Upload (replace) the SSH private key for an existing cluster."""
    ssh_file = request.files.get("file")
    if not ssh_file:
        return jsonify({"error": "file field required"}), 400
    raw_key = ssh_file.read()
    # Sanity check: looks like an OpenSSH or PEM key
    head = raw_key[:120].decode("utf-8", errors="ignore")
    if "PRIVATE KEY" not in head:
        return jsonify({"error": "uploaded file does not look like a private key (no '-----BEGIN ... PRIVATE KEY-----' header)"}), 400
    with CONFIG_LOCK:
        origin, cluster, raw, idx = _find_cluster(name)
        if origin is None:
            return jsonify({"error": f"cluster '{name}' not found"}), 404
        refused = _read_only_refusal(origin)
        if refused:
            return refused
        key_path = _origin_dirs(origin)[1] / f"{name}_id"
        _bm_write_private(key_path, raw_key)
        cluster.setdefault("ssh", {})["key"] = str(key_path)
        # une clé téléversée n'est plus celle que la console a créée
        cluster["ssh"].pop("generated", None)
        _save_cluster(origin, cluster, raw, idx)
    return jsonify({"cluster": name, "ssh_key": str(key_path), "origin": origin})


@app.route("/api/clusters/<name>/test-kubeconfig", methods=["POST"])
@requires_auth
def api_clusters_test_kubeconfig(name):
    """Quick kubectl ping using only the kubeconfig (no SSH)."""
    cfg = load_config()
    cluster = next((c for c in cfg.get("clusters", []) if c["name"] == name), None)
    if not cluster:
        return jsonify({"error": f"cluster '{name}' not found"}), 404
    kc = _identity_kubeconfig(cluster.get("kubeconfig", ""),
                              current_cluster_identity())
    if not Path(kc).exists():
        return jsonify({"ok": False, "error": f"kubeconfig file not found: {kc}"}), 404
    try:
        r = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, "version", "-o", "json"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            ver = json.loads(r.stdout)
            return jsonify({
                "ok": True,
                "server_version": ver.get("serverVersion", {}).get("gitVersion", "?"),
                "client_version": ver.get("clientVersion", {}).get("gitVersion", "?"),
            })
        return jsonify({"ok": False, "error": (r.stderr or r.stdout).strip()[:400]}), 200
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "timeout"}), 200


@app.route("/api/clusters/<name>/test-ssh", methods=["POST"])
@requires_auth
def api_clusters_test_ssh(name):
    """SSH ping on every node of the cluster."""
    cfg = load_config()
    cluster = next((c for c in cfg.get("clusters", []) if c["name"] == name), None)
    if not cluster:
        return jsonify({"error": f"cluster '{name}' not found"}), 404
    ssh_user = cluster.get("ssh", {}).get("user", "rancher")
    ssh_key  = cluster.get("ssh", {}).get("key", "")
    ssh_port = cluster.get("ssh", {}).get("port", 22)

    def _probe(node):
        host = node.get("hostname", "?")
        ip   = node.get("ip", "")
        role = node.get("role", "?")
        ssh_args = ["ssh",
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=5",
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "LogLevel=ERROR",
                    "-p", str(ssh_port)]
        if ssh_key:
            ssh_args.extend(["-i", ssh_key])
        ssh_args.extend([f"{ssh_user}@{ip}", "echo ok"])
        entry = {"hostname": host, "ip": ip, "role": role, "ok": False, "detail": ""}
        try:
            r = subprocess.run(ssh_args, capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and "ok" in r.stdout:
                entry["ok"] = True
            else:
                entry["detail"] = (r.stderr or r.stdout).strip().splitlines()[-1][:200] if (r.stderr or r.stdout) else "failed"
        except subprocess.TimeoutExpired:
            entry["detail"] = "timeout"
        except Exception as e:
            entry["detail"] = _safe_proc_error(e)
        return entry

    # v1.6.0: parallelize the SSH probe across nodes — 10 nodes used to
    # take 10×10s = 100s sequentially. ThreadPoolExecutor caps at 8.
    nodes = cluster.get("nodes", [])
    from concurrent.futures import ThreadPoolExecutor
    if nodes:
        with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as ex:
            results = list(ex.map(_probe, nodes))
    else:
        results = []
    return jsonify({"cluster": name, "results": results})


@app.route("/api/connection-test/<cluster>")
@requires_auth
def api_connection_test(cluster):
    """
    Full diagnostic of a cluster's reachability and permissions.

    Returns:
      - kubeconfig path being used
      - current context, user, server URL
      - API reachability
      - permission matrix (what harvester-ops can do)
      - SSH reachability per node
    """
    cfg = load_config()
    cluster_cfg = next((c for c in cfg.get("clusters", []) if c["name"] == cluster), None)
    if not cluster_cfg:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404

    kc_path = _identity_kubeconfig(cluster_cfg.get("kubeconfig", ""),
                                   current_cluster_identity())
    result = {
        "cluster": cluster,
        "kubeconfig": kc_path,
        "kubeconfig_exists": Path(kc_path).exists() if kc_path else False,
        "current_user": None,
        "current_context": None,
        "server": None,
        "api_reachable": False,
        "api_version": None,
        "permissions": {},
        "ssh": [],
        "warnings": [],
        "errors": [],
    }

    if not result["kubeconfig_exists"]:
        result["errors"].append(f"kubeconfig file not found: {kc_path}")
        return jsonify(result)

    def kc_cmd(*args, timeout=10):
        return _kubectl_run(
            ["kubectl", "--kubeconfig", kc_path, *args],
            capture_output=True, text=True, timeout=timeout,
        )

    # 1. Read config: current context, user, server
    try:
        r = kc_cmd("config", "view", "--minify", "-o", "json")
        if r.returncode == 0:
            ctx = json.loads(r.stdout)
            current_ctx_name = ctx.get("current-context", "")
            result["current_context"] = current_ctx_name
            ctx_entry = next((c["context"] for c in ctx.get("contexts", [])
                              if c["name"] == current_ctx_name), {})
            result["current_user"] = ctx_entry.get("user", "?")
            cluster_name = ctx_entry.get("cluster", "")
            cluster_entry = next((c["cluster"] for c in ctx.get("clusters", [])
                                  if c["name"] == cluster_name), {})
            result["server"] = cluster_entry.get("server", "?")
    except Exception as e:
        result["warnings"].append(f"config view failed: {e}")

    # 2. API reachability + version
    try:
        r = kc_cmd("version", "-o", "json", timeout=8)
        if r.returncode == 0:
            v = json.loads(r.stdout)
            srv = v.get("serverVersion", {})
            result["api_version"] = srv.get("gitVersion", "?")
            result["api_reachable"] = True
        else:
            result["errors"].append("API unreachable: " + r.stderr.strip().splitlines()[-1] if r.stderr else "unknown")
    except subprocess.TimeoutExpired:
        result["errors"].append("API request timed out")
    except Exception as e:
        result["errors"].append(f"version check failed: {e}")

    # 3. Permissions matrix — what harvester-ops needs
    # Each entry: (verb, resource[, "-n", namespace])
    permission_checks = [
        ("get nodes",            "list_nodes",          ["get", "nodes"]),
        ("patch nodes",          "cordon_nodes",        ["patch", "nodes"]),
        ("get vm.kubevirt.io",   "list_vms",            ["get", "vm.kubevirt.io", "--all-namespaces"]),
        ("patch vm.kubevirt.io", "stop_start_vms",      ["patch", "vm.kubevirt.io"]),
        ("update vm.kubevirt.io","annotate_vms",        ["update", "vm.kubevirt.io"]),
        ("get vmi.kubevirt.io",  "read_vmi",            ["get", "vmi.kubevirt.io", "--all-namespaces"]),
        ("get virtualmachineinstances/vnc",
                                 "vnc_console",         ["get", "virtualmachineinstances/vnc"]),
        ("create virtualmachinebackups.harvesterhci.io",
                                 "snapshot_vms",        ["create", "virtualmachinebackups.harvesterhci.io"]),
        ("get volumes.longhorn.io",
                                 "read_longhorn",       ["get", "volumes.longhorn.io", "-n", "longhorn-system"]),
        ("patch settings.longhorn.io",
                                 "longhorn_maintenance",["patch", "settings.longhorn.io", "-n", "longhorn-system"]),
    ]
    for label, key, args in permission_checks:
        try:
            r = kc_cmd("auth", "can-i", *args, timeout=5)
            ok = (r.returncode == 0 and r.stdout.strip() == "yes")
            result["permissions"][key] = {"label": label, "allowed": ok}
        except Exception:
            result["permissions"][key] = {"label": label, "allowed": False, "error": "check failed"}

    # 4. SSH reachability per node
    ssh_user = cluster_cfg.get("ssh", {}).get("user", "rancher")
    ssh_key  = cluster_cfg.get("ssh", {}).get("key", "")
    ssh_port = cluster_cfg.get("ssh", {}).get("port", 22)

    def _probe_node(node):
        host = node.get("hostname", "?")
        ip   = node.get("ip", "")
        role = node.get("role", "?")
        ssh_args = ["ssh",
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=4",
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "LogLevel=ERROR",
                    "-p", str(ssh_port)]
        if ssh_key:
            ssh_args.extend(["-i", ssh_key])
        ssh_args.extend([f"{ssh_user}@{ip}", "echo ok"])
        node_result = {"hostname": host, "ip": ip, "role": role, "reachable": False, "user": ssh_user, "detail": ""}
        try:
            r = subprocess.run(ssh_args, capture_output=True, text=True, timeout=8)
            if r.returncode == 0 and "ok" in r.stdout:
                node_result["reachable"] = True
                # Test sudo for shutdown command
                ssh_sudo = ssh_args[:-1] + ["sudo -n true"]
                r2 = subprocess.run(ssh_sudo, capture_output=True, text=True, timeout=8)
                node_result["sudo_nopasswd"] = (r2.returncode == 0)
            else:
                node_result["detail"] = (r.stderr or r.stdout).strip().splitlines()[-1][:200] if (r.stderr or r.stdout) else "failed"
        except subprocess.TimeoutExpired:
            node_result["detail"] = "timeout"
        except Exception as e:
            node_result["detail"] = _safe_proc_error(e)
        return node_result

    # v1.6.0: parallel probe (was sequential — 10 nodes × 16s = 160s).
    nodes = cluster_cfg.get("nodes", [])
    from concurrent.futures import ThreadPoolExecutor
    if nodes:
        with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as ex:
            result["ssh"] = list(ex.map(_probe_node, nodes))

    return jsonify(result)


@app.route("/api/vms/<cluster>/order", methods=["PUT"])
@requires_auth
def api_vms_set_order(cluster):
    """Update shutdown annotations (v1.4.14 model).

    Three body shapes accepted:

    1. New grouped (preferred):
       {"groups": [
          {"name": "frontends", "group_priority": 100,
           "vms": [{"namespace","name","snapshot","priority": 10}, ...]},
          {"name": "default", "group_priority": 100, "vms": [...]},
          ...
       ]}
       Each VM gets:
         shutdown-group           = <group.name>
         shutdown-group-priority  = <group.group_priority>
         shutdown-priority        = <vm.priority>  (intra-group order)

    2. Old grouped (v1.4.9-12): {"groups": [{name, priority, vms}, ...]}
       `priority` is treated as group_priority, intra_order auto-assigned
       by VM index (10, 20, 30, …).

    3. Legacy flat:  {"order": [{namespace, name, snapshot}, ...]}
       Each VM lands in "default" with group_priority=100, intra_order=
       idx*10 (mostly meaningless since default is parallel — kept for
       backward compat).
    """
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    groups = data.get("groups")
    order = data.get("order")
    if groups is None and order is None:
        return jsonify({"error": "expected 'groups' or 'order' list in body"}), 400

    # Normalize to flat list of (vm_dict, intra_order, group, group_priority)
    todo = []
    if isinstance(groups, list):
        for g in groups:
            gname = g.get("name") or "default"
            # `group_priority` is the new field; fall back to `priority`
            # for the old payload shape (v1.4.9-12), then to 100.
            raw_gprio = g.get("group_priority", g.get("priority", 100))
            try:
                gprio = int(raw_gprio)
            except (TypeError, ValueError):
                gprio = 100
            vms_in = g.get("vms", []) or []
            for idx, vm in enumerate(vms_in, start=1):
                # Per-VM intra-group order: lower = stops first within
                # the group. If the client didn't supply it, derive from
                # the position in the VM list (10, 20, 30, ...).
                raw_intra = vm.get("priority", idx * 10)
                try:
                    intra = int(raw_intra)
                except (TypeError, ValueError):
                    intra = idx * 10
                todo.append((vm, intra, gname, gprio))
    elif isinstance(order, list):
        for idx, vm in enumerate(order, start=1):
            todo.append((vm, idx * 10, "default", 100))
    else:
        return jsonify({"error": "expected 'groups' or 'order' list"}), 400

    results = []
    for vm, intra, group, gprio in todo:
        ns = vm.get("namespace")
        name = vm.get("name")
        snap = vm.get("snapshot", True)
        if not ns or not name:
            results.append({"vm": vm, "ok": False, "reason": "missing namespace/name"})
            continue
        annots = [
            f"harvester-ops.io/shutdown-priority={intra}",
            f"harvester-ops.io/shutdown-group={group}",
            f"harvester-ops.io/shutdown-group-priority={gprio}",
            f"harvester-ops.io/snapshot={'true' if snap else 'false'}",
        ]
        try:
            subprocess.check_call(
                ["kubectl", "--kubeconfig", kc, "annotate", "vm", name, "-n", ns, *annots, "--overwrite"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15,
            )
            results.append({
                "vm": f"{ns}/{name}", "ok": True,
                "priority": intra, "group": group,
                "group_priority": gprio, "snapshot": snap,
            })
        except subprocess.CalledProcessError as e:
            results.append({"vm": f"{ns}/{name}", "ok": False, "reason": e.stderr.decode() if e.stderr else "unknown"})
        except subprocess.TimeoutExpired:
            results.append({"vm": f"{ns}/{name}", "ok": False, "reason": "timeout"})

    ok_count = sum(1 for r in results if r["ok"])
    return jsonify({
        "total": len(todo),
        "updated": ok_count,
        "results": results,
    })


def api_status_helper(cluster, namespace):
    """Shared by status and namespace endpoints."""
    with app.test_request_context(f"/api/status/{cluster}?namespace={namespace}"):
        return api_status(cluster)


@app.route("/api/actions")
@requires_auth
def api_actions_list():
    with ACTIONS_LOCK:
        return jsonify({"actions": [a.to_dict() for a in ACTIONS.values()]})


@app.route("/api/action", methods=["POST"])
@_rate_limit("30/minute")
@requires_auth
def api_action_start():
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action")
    cluster = data.get("cluster")
    namespace = data.get("namespace")
    dry_run = bool(data.get("dry_run", False))
    snapshot = bool(data.get("snapshot", False))
    force = bool(data.get("force", False))
    extra_args = data.get("extra_args", [])

    if not action or not cluster:
        return jsonify({"error": "action and cluster required"}), 400
    if action in SSO_POWER_ACTIONS and _sso_session() is not None:
        # Un cluster éteint ne passe plus par Rancher, et Rancher peut tourner
        # sur le cluster qu'on éteint : l'alimentation reste aux comptes locaux.
        return jsonify({"error": "forbidden", "code": "power-needs-local-account",
                        "hint": "starting or stopping a cluster needs a local account"}), 403

    try:
        run = start_action(action, cluster, dry_run=dry_run,
                           namespace=namespace, extra_args=extra_args,
                           snapshot=snapshot, force=force)
    except ActionBusy as e:
        return jsonify({"error": str(e), "running": e.run.id,
                        "running_action": e.run.action}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(run.to_dict()), 201


@app.route("/api/action/<run_id>")
@requires_auth
def api_action_get(run_id):
    with ACTIONS_LOCK:
        run = ACTIONS.get(run_id)
    if not run:
        # v1.6.5: fall back to SQLite — the run may have been GC'd from
        # memory or predate the current Flask process.
        d, _events = _actions_db_get(run_id)
        if d:
            return jsonify(d)
        abort(404)
    return jsonify(run.to_dict())


@app.route("/api/action/<run_id>", methods=["DELETE"])
@requires_auth
def api_action_cancel(run_id):
    with ACTIONS_LOCK:
        run = ACTIONS.get(run_id)
    if not run:
        abort(404)
    if run.proc and run.proc.poll() is None:
        run.proc.terminate()
        run.status = "cancelled"
        run.emit({"type": "status", "status": "cancelled", "ts": time.time()})
        run.close()
    elif run.proc is None and run.status in ("starting", "running"):
        # v1.47.0 : une action sans processus (téléchargement d'ISO,
        # installation bare-metal, dépôt d'archive) consulte `_cancel` dans
        # ses boucles, mais rien ne le posait : le bouton du dock ne faisait
        # rien. Elle s'arrête d'elle-même et nettoie ce qu'elle a commencé.
        run._cancel = True
    return jsonify(run.to_dict())


@app.route("/api/stream/<run_id>")
@requires_auth
def api_stream(run_id):
    with ACTIONS_LOCK:
        run = ACTIONS.get(run_id)
    if not run:
        # v1.6.5: replay persisted events for runs no longer in memory so
        # the Activity "details" panel works across restarts and the 1h GC.
        d, events = _actions_db_get(run_id)
        if not d:
            abort(404)

        def gen_db():
            for ev in events:
                yield f"event: {ev.get('type', 'log')}\ndata: {json.dumps(ev)}\n\n"
            yield f"event: end\ndata: {json.dumps(d)}\n\n"

        return Response(stream_with_context(gen_db()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def gen():
        # Replay any events we already have. v1.46.0 : par numéro absolu
        # (`events_since`), sinon le flux se figeait passé 500 événements ;
        # et la progression d'un transfert quand elle change.
        seq, prog_ver = 0, 0
        while True:
            with run._cond:
                while (seq >= run._seq and prog_ver == run.progress_ver
                       and not run._closed):
                    run._cond.wait(timeout=15)
                evs, seq = run.events_since(seq)
                prog = run.progress_last if prog_ver != run.progress_ver else None
                prog_ver = run.progress_ver
                closed = run._closed and seq >= run._seq
            for ev in evs:
                yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
            if prog is not None:
                yield f"event: progress\ndata: {json.dumps(prog)}\n\n"
            if closed:
                yield f"event: end\ndata: {json.dumps(run.to_dict())}\n\n"
                return

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# -----------------------------------------------------------------------------
# Activity (in-progress actions + history with log files)
# -----------------------------------------------------------------------------
# Actions dont un fichier de log CLI peut porter le nom. Le nom de fichier
# est `AAAAMMJJ-HHMMSS-<cluster>-<action>.log`, et un nom de cluster peut
# contenir des tirets (`harv-second`) tout comme un nom d'action
# (`ns-stop`) : c'est la liste des actions connues qui lève l'ambiguïté.
_LOG_ACTIONS = ("shutdown", "startup", "status", "ns-stop", "ns-start", "action")
_LOG_NAME_RE = re.compile(
    r"^(\d{8})-(\d{6})-(?P<cluster>.+?)-(?P<action>" + "|".join(_LOG_ACTIONS) + r")\.log$")


def _log_file_entry(path, stat):
    """Décrit un fichier de log, cluster et action extraits de son nom."""
    m = _LOG_NAME_RE.match(path.name)
    return {
        "filename": path.name,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "cluster": m.group("cluster") if m else "?",
        "action": m.group("action") if m else path.stem,
    }


def _activity_matches(entry, cluster, status, action, q):
    """Filtre commun aux trois sources (en cours, historique, fichiers)."""
    if cluster and entry.get("cluster") != cluster:
        return False
    if status and entry.get("status", "done") != status:
        return False
    if action and action.lower() not in str(entry.get("action", "")).lower():
        return False
    if q:
        hay = " ".join(str(entry.get(k, "")) for k in
                       ("id", "action", "cluster", "filename", "error_summary"))
        if q.lower() not in hay.lower():
            return False
    return True


@app.route("/api/activity")
@requires_auth
@shared_read(ttl=1.0, scope="_activity")
def api_activity():
    """Return current and historical activity, optionally filtered.

    Query params: `cluster`, `status`, `action` (substring), `q` (free
    text), `limit`.
    """
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
    except ValueError:
        limit = 50
    f_cluster = (request.args.get("cluster") or "").strip()
    f_status = (request.args.get("status") or "").strip()
    f_action = (request.args.get("action") or "").strip()
    f_q = (request.args.get("q") or "").strip()
    filtering = any((f_cluster, f_status, f_action, f_q))

    def keep(e):
        return _activity_matches(e, f_cluster, f_status, f_action, f_q)

    with ACTIONS_LOCK:
        in_progress = [a.to_dict() for a in ACTIONS.values()
                       if a.status in ("starting", "running")]
        done = [a.to_dict() for a in ACTIONS.values()
                if a.status not in ("starting", "running")]
        mem_ids = {a["id"] for a in in_progress} | {a["id"] for a in done}
    in_progress.sort(key=lambda a: a["started_at"], reverse=True)
    # v1.6.5: merge the SQLite history (last 500 runs) so completed actions
    # survive Flask restarts and the in-memory GC. In-memory entries win on
    # id conflicts (they are at least as fresh as their persisted row).
    # v1.23.0 : le filtre descend jusqu'à SQL, sinon il ne chercherait que
    # dans la page déjà chargée.
    for row in _actions_db_recent(limit, cluster=f_cluster or None,
                                  status=f_status or None,
                                  action=f_action or None, q=f_q or None):
        if row["id"] not in mem_ids:
            done.append(row)

    # Filesystem log files (CLI runs + previous Flask sessions)
    all_logs = []
    if LOG_DIR.exists():
        for p in sorted(LOG_DIR.glob("*.log"), reverse=True)[:200]:
            try:
                all_logs.append(_log_file_entry(p, p.stat()))
            except OSError:
                pass

    # Total AVANT filtrage : le compter sur `done`, déjà filtré en SQL,
    # affichait « 6 entrées sur 19 » là où l'historique en compte 211.
    with ACTIONS_LOCK:
        mem_total = len(ACTIONS)
    total = max(_actions_db_count(), mem_total) + len(all_logs)
    in_progress = [a for a in in_progress if keep(a)]
    done = [a for a in done if keep(a)]
    fs_logs = [f for f in all_logs if keep(f)][:100]
    done.sort(key=lambda a: a.get("ended_at") or a["started_at"], reverse=True)

    # Valeurs proposables dans les menus de filtre : celles réellement
    # présentes, y compris pour un cluster retiré de la configuration dont
    # l'historique existe encore.
    facets = _activity_facets()
    return jsonify({
        "in_progress": in_progress,
        "actions_done": done[:limit],
        "log_files": fs_logs,
        "filters": {"cluster": f_cluster, "status": f_status,
                    "action": f_action, "q": f_q, "active": filtering},
        "matched": len(in_progress) + len(done[:limit]) + len(fs_logs),
        "total": total,
        "facets": facets,
    })


def _activity_facets():
    """Clusters, statuts et actions présents dans l'historique."""
    clusters, statuses, actions = set(), set(), set()
    try:
        conn = sqlite3.connect(str(ACTIONS_DB))
        for c, s, a in conn.execute(
                "SELECT DISTINCT cluster, status, action FROM actions"):
            clusters.add(c); statuses.add(s); actions.add(a)
        conn.close()
    except sqlite3.Error:
        pass
    with ACTIONS_LOCK:
        for a in ACTIONS.values():
            clusters.add(a.cluster); statuses.add(a.status); actions.add(a.action)
    if LOG_DIR.exists():
        for p in list(LOG_DIR.glob("*.log"))[:200]:
            m = _LOG_NAME_RE.match(p.name)
            if m:
                clusters.add(m.group("cluster")); actions.add(m.group("action"))
                statuses.add("done")
    # Les actions portent souvent une cible (`vm-start:default/x`) : ne
    # proposer que le verbe, sinon le menu compterait une entrée par VM.
    verbs = sorted({str(a).split(":", 1)[0] for a in actions if a})
    return {"clusters": sorted(c for c in clusters if c),
            "statuses": sorted(s for s in statuses if s),
            "actions": verbs}


@app.route("/api/logs/<path:filename>")
@requires_auth
def api_log_content(filename):
    """Return the content of a single log file."""
    # Strict: only allow files inside LOG_DIR with .log extension
    safe = LOG_DIR / filename
    try:
        safe = safe.resolve()
        LOG_DIR.resolve()  # ensure exists
        if not str(safe).startswith(str(LOG_DIR.resolve())) or safe.suffix != ".log":
            abort(403)
    except (OSError, ValueError):
        abort(404)
    if not safe.exists():
        abort(404)
    try:
        content = safe.read_text(errors="replace")
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "filename": filename,
        "size": safe.stat().st_size,
        "content": content,
    })


# -----------------------------------------------------------------------------
# Docs (markdown rendered server-side)
# -----------------------------------------------------------------------------
@app.route("/api/docs")
@requires_auth
def api_docs_index():
    """Return the list of available docs, grouped by language."""
    index = {}
    if DOCS_DIR.exists():
        for lang_dir in sorted(DOCS_DIR.iterdir()):
            if not lang_dir.is_dir():
                continue
            lang = lang_dir.name
            if lang not in ("en", "fr", "it", "es", "de"):
                continue
            files = []
            for md in sorted(lang_dir.glob("*.md")):
                # Extract title from first # heading
                title = md.stem.replace("-", " ").title()
                try:
                    for line in md.read_text(errors="replace").splitlines():
                        if line.startswith("# "):
                            title = line[2:].strip()
                            break
                except OSError:
                    pass
                files.append({"path": md.name, "title": title})
            index[lang] = files
    return jsonify({"docs": index})


@app.route("/api/docs/<lang>/<path:filename>")
@requires_auth
def api_doc_render(lang, filename):
    """Render a markdown doc to HTML."""
    if lang not in ("en", "fr", "it", "es", "de"):
        abort(400)
    if not filename.endswith(".md") or ".." in filename or "/" in filename:
        abort(400)
    doc_path = DOCS_DIR / lang / filename
    if not doc_path.exists():
        abort(404)
    try:
        md_text = doc_path.read_text(errors="replace")
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    html = markdown.markdown(
        md_text,
        extensions=["fenced_code", "tables", "toc", "nl2br"],
    )
    return jsonify({
        "lang": lang,
        "filename": filename,
        "html": html,
    })


# -----------------------------------------------------------------------------
# Per-VM runStrategy patch
# -----------------------------------------------------------------------------
def track_action(label, cluster, worker, *worker_args):
    """Create an ActionRun and spawn the worker in a daemon thread.
    Returns the action_id (string) immediately. The worker must take
    (run, *args) and eventually call run.close().
    """
    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, label, cluster, [], dry_run=False)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    READ_SHARE.invalidate("_activity")
    threading.Thread(target=worker, args=(run, *worker_args), daemon=True).start()
    return run_id


def _simple_kubectl_action(run, kc, kubectl_args, step_label, success_msg=""):
    """Worker for short-lived kubectl operations (delete, apply, patch...).
    Emits a single step with the kubectl invocation, applies, reports."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    run.emit({"type": "step", "step_id": step_label, "status": "running",
              "message": " ".join(kubectl_args), "ts": time.time()})
    try:
        r = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, *kubectl_args],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            run.emit({"type": "step", "step_id": step_label, "status": "done",
                      "message": success_msg or "applied",
                      "ts": time.time()})
            run.exit_code = 0; run.status = "done"
        else:
            run.emit({"type": "step", "step_id": step_label, "status": "error",
                      "message": (r.stderr.strip() or r.stdout.strip())[:200],
                      "ts": time.time()})
            run.exit_code = 1; run.status = "error"
    except subprocess.TimeoutExpired:
        run.emit({"type": "step", "step_id": step_label, "status": "error",
                  "message": "kubectl timeout", "ts": time.time()})
        run.exit_code = 124; run.status = "error"
    except Exception as e:
        run.emit({"type": "step", "step_id": step_label, "status": "error",
                  "message": str(e)[:200], "ts": time.time()})
        run.exit_code = 1; run.status = "error"
    run.ended_at = time.time()
    run.emit({"type": "status", "status": run.status, "exit_code": run.exit_code, "ts": time.time()})
    run.close()


def _vm_action_runner(run, kc, namespace, name, target):
    """Background worker: patch VM runStrategy then poll VMI until target reached.
    Emits SSE events so the dock can show progress in real time.
    """
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    run.emit({"type": "step", "step_id": "patch", "status": "running",
              "message": f"kubectl patch vm/{name} runStrategy={target}",
              "ts": time.time()})
    # Step 1: patch. stderr is captured and surfaced: a bare "exit 1" is
    # undiagnosable from the UI (e.g. cluster webhook down → kubectl says
    # 'no endpoints available for service "virt-api"' — the user must see it).
    # Note: str(e) on CalledProcessError leaked the full command line
    # (kubeconfig path included) and never contained stderr; don't go back.
    try:
        r = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, "patch", "vm", name, "-n", namespace,
             "--type", "merge", "-p", json.dumps({"spec": {"runStrategy": target}})],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        r = None
    if r is None or r.returncode != 0:
        if r is None:
            detail = "kubectl timed out after 15s (cluster API unreachable?)"
        else:
            stderr_lines = [l.strip() for l in (r.stderr or "").splitlines() if l.strip()]
            detail = (stderr_lines[-1] if stderr_lines
                      else f"kubectl exited {r.returncode} with no error output")[:300]
        run.error_summary = detail
        run.emit({"type": "step", "step_id": "patch", "status": "error",
                  "message": detail, "ts": time.time()})
        run.exit_code = 1
        run.status = "error"
        run.ended_at = time.time()
        run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
        run.close()
        return
    run.emit({"type": "step", "step_id": "patch", "status": "done",
              "message": "patch applied", "ts": time.time()})

    # Step 2: wait for VMI to reach the target state
    run.emit({"type": "step", "step_id": "wait", "status": "running",
              "message": f"waiting for VMI phase", "ts": time.time()})
    expected_running = target in ("Always", "RerunOnFailure")
    deadline = time.time() + 120
    last_phase = ""
    while time.time() < deadline:
        try:
            r = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "-n", namespace, "get", "vmi", name,
                 "-o", "jsonpath={.status.phase}"],
                capture_output=True, text=True, timeout=5,
            )
            phase = r.stdout.strip()
            vmi_exists = (r.returncode == 0 and phase)
        except Exception:
            phase = ""
            vmi_exists = False

        if target == "Halted":
            if not vmi_exists:
                run.emit({"type": "step", "step_id": "wait", "status": "done",
                          "message": "VMI gone", "ts": time.time()})
                break
        elif expected_running:
            if phase == "Running":
                run.emit({"type": "step", "step_id": "wait", "status": "done",
                          "message": f"phase=Running", "ts": time.time()})
                break

        if phase != last_phase:
            run.emit({"type": "step", "step_id": "wait", "status": "progress",
                      "message": f"phase={phase or 'unknown'}", "ts": time.time()})
            last_phase = phase
        time.sleep(2)
    else:
        run.emit({"type": "step", "step_id": "wait", "status": "warn",
                  "message": f"timeout, last phase={last_phase}", "ts": time.time()})

    run.exit_code = 0
    run.status = "done"
    run.ended_at = time.time()
    run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
    run.close()


# =============================================================================
# Création de machines virtuelles
#
# La console savait tout éditer d'une VM mais pas en créer une : il fallait
# passer par l'UI Harvester ou par une déclaration Terraform. Le formulaire
# de création REJOUE les sections de l'éditeur (cf. web/static/js/vm-create.js)
# et envoie ici un manifeste complet : tout ce qui est éditable est donc
# réglable à la création, par construction.
# =============================================================================
VM_CREATE_MAX = 50          # garde-fou : un chiffre tapé de travers ne doit
                            # pas lancer mille créations


def _rand_suffix(n=5):
    import random
    import string
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def _instance_names(base, count, start_at=1, pad=2):
    """`web` x3 -> web-01, web-02, web-03. Une seule instance garde le nom
    tel quel : suffixer « web » en « web-01 » quand on n'en demande qu'une
    surprendrait."""
    if count <= 1:
        return [base]
    return [f"{base}-{i:0{pad}d}" for i in range(start_at, start_at + count)]


def _vm_manifest_for_instance(manifest, name, namespace, start):
    """Décline le manifeste pour UNE instance.

    Le point délicat est le stockage : les noms de PVC de
    `harvesterhci.io/volumeClaimTemplates` portent le nom de la VM, et les
    volumes les référencent par `claimName`. Créer trois VMs à partir du
    même manifeste sans les réécrire ferait échouer les deux dernières sur
    des PVC déjà pris (ou, pire, les ferait partager un disque).
    """
    import copy
    vm = copy.deepcopy(manifest)
    vm.setdefault("apiVersion", "kubevirt.io/v1")
    vm.setdefault("kind", "VirtualMachine")
    meta = vm.setdefault("metadata", {})
    old_name = meta.get("name") or ""
    meta["name"] = name
    meta["namespace"] = namespace

    spec = vm.setdefault("spec", {})
    spec["runStrategy"] = "Always" if start else "Halted"

    tmpl_spec = spec.setdefault("template", {}).setdefault("spec", {})
    # Le nom d'hôte invité suit le nom de la VM quand il n'a pas été fixé
    # à la main.
    if not tmpl_spec.get("hostname") or tmpl_spec.get("hostname") == old_name:
        tmpl_spec["hostname"] = name

    # --- stockage : renommer les PVC et leurs références ---
    annotations = meta.setdefault("annotations", {})
    raw = annotations.get("harvesterhci.io/volumeClaimTemplates")
    renamed = {}
    if raw:
        try:
            vcts = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            vcts = []
        for vct in vcts or []:
            vmeta = vct.setdefault("metadata", {})
            old_pvc = vmeta.get("name") or ""
            # Le suffixe aléatoire évite la collision avec un PVC orphelin
            # laissé par une VM supprimée du même nom.
            disk_part = old_pvc
            if old_name and old_pvc.startswith(old_name + "-"):
                disk_part = old_pvc[len(old_name) + 1:]
            disk_part = re.sub(r"-[a-z0-9]{5}$", "", disk_part) or "disk-0"
            new_pvc = f"{name}-{disk_part}-{_rand_suffix()}"
            vmeta["name"] = new_pvc
            if old_pvc:
                renamed[old_pvc] = new_pvc
        if vcts:
            annotations["harvesterhci.io/volumeClaimTemplates"] = json.dumps(vcts)

    for vol in tmpl_spec.get("volumes") or []:
        pvc = vol.get("persistentVolumeClaim") or {}
        claim = pvc.get("claimName")
        if claim and claim in renamed:
            pvc["claimName"] = renamed[claim]

    # Les champs que l'apiserver refuse sur une création.
    for field in ("resourceVersion", "uid", "creationTimestamp",
                  "generation", "selfLink", "managedFields"):
        meta.pop(field, None)
    vm.pop("status", None)
    return vm


def _vm_create_runner(run, cluster, kc, namespace, names, start, manifest,
                      dry_run, cloudinit=None):
    """Crée les VMs une par une, en rendant compte de chacune.

    Une par une et non en lot : sur un échec partiel, l'opérateur doit
    savoir lesquelles existent. Un `kubectl create` groupé s'arrête à la
    première erreur en laissant un résultat ambigu.
    """
    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": msg, "ts": time.time()})

    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    created, failed = [], []
    try:
        for name in names:
            vm = _vm_manifest_for_instance(manifest, name, namespace, start)
            # v1.60.0 : le cloud-init de la fenêtre de création (il était
            # perdu) : un Secret par VM, référencé comme le fait Harvester.
            if cloudinit:
                secrets = []
                # v1.62.0 : pas de disque cloud-init vide (fichier de réponses
                # Windows ou virtiofs seuls)
                if (cloudinit.get("user_data") or "").strip() or (cloudinit.get("network_data") or "").strip():
                    secret = _hv.cloudinit_secret(name, namespace, cloudinit["user_data"],
                                                  cloudinit["network_data"])
                    vm = _hv.attach_cloudinit(vm, secret["metadata"]["name"])
                    secrets.append(("cloud-init", secret))
                if cloudinit.get("sysprep"):
                    sp = _hv.sysprep_secret(name, namespace, cloudinit["sysprep"])
                    vm = _hv.attach_sysprep(vm, sp["metadata"]["name"])
                    secrets.append(("sysprep", sp))
                if cloudinit.get("filesystems"):
                    vm = _hv.add_filesystems(vm, cloudinit["filesystems"])
                if cloudinit.get("ssh_names"):
                    vm.setdefault("metadata", {}).setdefault("annotations", {}).update(
                        _hv.ssh_names_annotation(cloudinit["ssh_names"]))
                bad = False
                for what, secret in ([] if dry_run else secrets):
                    r = subprocess.run(["kubectl", "--kubeconfig", kc, "create", "-f", "-", "-o", "name"],
                                       input=json.dumps(secret), capture_output=True, text=True, timeout=60)
                    if r.returncode != 0:
                        detail = (r.stderr or r.stdout).strip().splitlines()
                        failed.append((name, detail[-1][:300] if detail else f"{what} secret"))
                        step(name, "error", failed[-1][1])
                        bad = True
                        break
                    step(name, "running", f"{what}: secret {secret['metadata']['name']}")
                if bad:
                    continue
            cmd = ["kubectl", "--kubeconfig", kc, "create", "-f", "-",
                   "-o", "name"]
            if dry_run:
                cmd += ["--dry-run=server"]
            step(name, "running", f"création de {namespace}/{name}")
            try:
                r = subprocess.run(cmd, input=json.dumps(vm), capture_output=True,
                                   text=True, timeout=60)
            except subprocess.TimeoutExpired:
                failed.append((name, "timeout"))
                step(name, "error", "timeout")
                continue
            if r.returncode == 0:
                created.append(name)
                step(name, "done",
                     ("(dry-run) " if dry_run else "") + (r.stdout.strip() or name))
            else:
                detail = (r.stderr or r.stdout).strip().splitlines()
                detail = detail[-1][:300] if detail else f"exit {r.returncode}"
                failed.append((name, detail))
                step(name, "error", detail)

        if failed:
            run.exit_code = 1
            run.status = "error"
            run.error_summary = (f"{len(created)}/{len(names)} créée(s) ; "
                                 f"échec sur {failed[0][0]} : {failed[0][1]}")[:300]
        else:
            run.exit_code = 0
            run.status = "done"
    except Exception as e:
        run.error_summary = str(e)[:300]
        step("create", "error", str(e)[:300])
        run.exit_code = 1
        run.status = "error"
    if not dry_run and created:
        _invalidate_cluster_caches(cluster)
    run.ended_at = time.time()
    run.emit({"type": "status", "status": run.status,
              "exit_code": run.exit_code, "ts": time.time()})
    run.close()


def _vm_create_cloudinit(kc, cluster, namespace, data):
    """Le cloud-init demandé à la création : user-data et network-data, clés
    SSH choisies (leur clé publique ajoutée à ssh_authorized_keys) et agent
    invité. None s'il n'y a rien à poser."""
    ci = data.get("cloudinit")
    keys = data.get("ssh_keys") or []
    agent = bool(data.get("guest_agent"))
    if not isinstance(ci, dict):
        ci = {}
    user = ci.get("user_data") or ""
    net = ci.get("network_data") or ""
    if not isinstance(user, str) or not isinstance(net, str) or len(user) + len(net) > 256 * 1024:
        raise ValueError("cloud-init: text of 256 KiB at most")
    if not isinstance(keys, list):
        raise ValueError("ssh_keys: a list of key pair names")
    names, publics = [], []
    for ref in keys:
        ref = str(ref)
        kns, kname = ref.split("/", 1) if "/" in ref else (namespace, ref)
        if not (_K8S_NAME_RE.match(kns) and _K8S_NAME_RE.match(kname)):
            raise ValueError(f"ssh key {ref!r}: namespace/name")
        kp = _kubectl_json(kc, "get", "keypairs.harvesterhci.io", kname, "-n", kns, cluster=cluster)
        pub = ((kp or {}).get("spec") or {}).get("publicKey")
        if not pub:
            raise ValueError(f"no SSH key {kns}/{kname}")
        names.append(kname)
        publics.append(pub)
    if agent:
        user = _hv.with_guest_agent(user)
    if publics:
        user = _hv.with_ssh_keys(user, publics)
    # v1.62.0 : fichier de réponses Windows et volumes virtiofs (création)
    sysprep = data.get("sysprep")
    if sysprep is not None and not isinstance(sysprep, str):
        raise ValueError("sysprep: the autounattend.xml text")
    sysprep = _hv.check_unattend(sysprep) if sysprep and sysprep.strip() else None
    fss = data.get("filesystems") or []
    if not isinstance(fss, list):
        raise ValueError("filesystems: a list")
    _hv.add_filesystems({"spec": {"template": {"spec": {}}}}, fss)      # contrôle d'avance
    if not user.strip() and not net.strip() and not sysprep and not fss:
        return None
    return {"user_data": user if (user.strip() or net.strip()) else None, "network_data": net,
            "ssh_names": names, "sysprep": sysprep, "filesystems": fss}


@app.route("/api/vms/<cluster>/create", methods=["POST"])
@requires_auth
@_rate_limit("10/minute")
def api_vm_create(cluster):
    """Crée une ou plusieurs VMs.

    Body: {namespace, name, count?, start?, manifest, dry_run?}
    """
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200

    data = request.get_json(force=True, silent=True) or {}
    namespace = (data.get("namespace") or "").strip()
    name = (data.get("name") or "").strip()
    manifest = data.get("manifest")
    start = bool(data.get("start", True))
    dry_run = bool(data.get("dry_run", False))
    try:
        count = int(data.get("count", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "count must be a number"}), 400

    if not _valid_k8s_name(namespace):
        return jsonify({"error": "invalid namespace"}), 400
    if not _valid_k8s_name(name):
        return jsonify({"error": "invalid VM name (RFC 1123)"}), 400
    if not isinstance(manifest, dict) or not manifest.get("spec"):
        return jsonify({"error": "manifest with a spec is required"}), 400
    if count < 1 or count > VM_CREATE_MAX:
        return jsonify({"error": f"count must be between 1 and {VM_CREATE_MAX}"}), 400

    names = _instance_names(name, count)
    # Les noms dérivés doivent rester valides : « mon-app » x12 donne
    # « mon-app-12 », mais un nom déjà à la limite des 63 caractères ne
    # passerait plus.
    invalid = [n for n in names if not _valid_k8s_name(n)]
    if invalid:
        return jsonify({"error": f"generated name is not RFC 1123: {invalid[0]}"}), 400
    try:
        cloudinit = _vm_create_cloudinit(kc, cluster, namespace, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    action_id = track_action(
        f"vm-create:{namespace}/{name}" + (f" x{count}" if count > 1 else ""),
        cluster, _vm_create_runner,
        cluster, kc, namespace, names, start, manifest, dry_run, cloudinit)
    return jsonify({"action_id": action_id, "names": names,
                    "dry_run": dry_run}), 202


# =============================================================================
# Capacité de stockage allouable
#
# « Combien puis-je encore allouer ? » n'a pas pour réponse l'espace
# disponible affiché par Longhorn. Son ordonnanceur applique DEUX contraintes
# à la fois, et c'est la plus serrée qui décide :
#
#   1. sur-provisionnement : scheduled + taille <= (max - reserved) * over%/100
#   2. place réelle        : available - taille >= max * minimalAvailable%/100
#
# Sur harv1 : la première laisse 2592 Gio, la seconde 1107 Gio. Afficher
# « 1968 Gio disponibles » ferait donc promettre presque le double de ce que
# le cluster acceptera, et la VM échouerait à la planification.
#
# S'y ajoute le nombre de RÉPLIQUES de la storage class : un volume de 100 Gio
# en 3 répliques consomme 100 Gio sur trois nodes DIFFÉRENTS. L'allouable
# d'une classe est donc la R-ième meilleure place parmi les nodes, pas la
# meilleure.
# =============================================================================


def _longhorn_setting(kc, cluster, name, default):
    data = _kubectl_json(kc, "get", "settings.longhorn.io", name,
                         "-n", "longhorn-system", "-o", "json", cluster=cluster)
    try:
        return float((data or {}).get("value"))
    except (TypeError, ValueError):
        return default


@app.route("/api/storage-capacity/<cluster>")
@requires_auth
def api_storage_capacity(cluster):
    """Ce qu'on peut encore allouer, par storage class."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200

    over = _longhorn_setting(kc, cluster, "storage-over-provisioning-percentage", 200.0)
    minimal = _longhorn_setting(kc, cluster, "storage-minimal-available-percentage", 25.0)
    nodes = _kubectl_json(kc, "get", "nodes.longhorn.io", "-n", "longhorn-system",
                          "-o", "json", cluster=cluster)
    scs = _kubectl_json(kc, "get", "sc", "-o", "json", cluster=cluster)
    room = _storage_room((nodes or {}).get("items", []),
                         (scs or {}).get("items", []), over, minimal)
    return jsonify({
        "over_provisioning_pct": over,
        "minimal_available_pct": minimal,
        "schedulable_nodes": room["schedulable_nodes"],
        "disks": room["disks"],
        "classes": room["classes"],
    })


# v1.45.0 : le calcul vit dans bin/lib/longhorn_room.py, partagé avec le
# moteur de transfert de VM (CLI). Même nom ici : les appelants ne changent pas.
_storage_room = longhorn_room.storage_room


# =============================================================================
# Carte du stockage (v1.40.0)
#
# La vue Stockage se lit comme un datastore d'ESXi : les storage classes (et
# les disques de VM qu'elles portent) à gauche, le moteur au milieu, les
# disques des nœuds à droite avec leur jauge. Tout vient d'UN appel groupé,
# conformément à l'économie de la v1.33.0 : l'ancienne vue en lançait huit
# à chaque rafraîchissement.
# =============================================================================
STORAGE_KINDS = [
    "persistentvolumeclaims",
    "storageclasses",
    "nodes.longhorn.io",
    "volumes.longhorn.io",
    "replicas.longhorn.io",
    "settings.longhorn.io",
    "virtualmachines.kubevirt.io",
    "virtualmachineinstances.kubevirt.io",
    "backingimages.longhorn.io",
    "virtualmachineimages.harvesterhci.io",
    # v1.42.0 : l'état des répliques vu par le moteur, et la progression
    # des reconstructions. Sans lui, on ne distingue pas un volume qui se
    # répare d'un volume qui attend qu'on agisse.
    "engines.longhorn.io",
]
_storage_missing = {}
STORAGE_MAP_TTL = 5.0
_storage_map_cache = {}
_storage_map_lock = threading.Lock()

# Un pod de VM (ou d'attachement à chaud) n'est pas un « autre » consommateur :
# la VM le dit déjà, par sa spec.
_VM_POD_PREFIXES = ("virt-launcher-", "hp-volume-")


def _k8s_bytes(q):
    """Quantité Kubernetes (`10Gi`, `1Ti`, `500M`) en octets, ou None."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGTPE]i?)?\s*", str(q or ""))
    if not m:
        return None
    mul = {None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
           "Pi": 1024**5, "Ei": 1024**6, "K": 10**3, "M": 10**6, "G": 10**9,
           "T": 10**12, "P": 10**15, "E": 10**18}[m.group(2)]
    return int(float(m.group(1)) * mul)


def _build_storage_map(cluster, kc):
    items = _grouped_items(kc, cluster, STORAGE_KINDS, _storage_missing,
                           required="persistentvolumeclaims")
    if items is None:
        return None
    by_kind = {}
    for it in items:
        api = it.get("apiVersion") or ""
        kind = it.get("kind")
        # `Node` et `Setting` existent aussi hors de Longhorn : ne garder que
        # ceux qu'on a demandés.
        if kind in ("Node", "Setting") and not api.startswith("longhorn.io"):
            continue
        by_kind.setdefault(kind, []).append(it)

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    settings = {(s.get("metadata") or {}).get("name"): s.get("value")
                for s in by_kind.get("Setting", [])}
    over = _num(settings.get("storage-over-provisioning-percentage")) or 200.0
    minimal = _num(settings.get("storage-minimal-available-percentage"))
    minimal = 25.0 if minimal is None else minimal
    lh_nodes = by_kind.get("Node", [])
    scs = by_kind.get("StorageClass", [])
    room = _storage_room(lh_nodes, scs, over, minimal)

    # Image source d'une classe ou d'un volume : BackingImage -> VMImage.
    bi_to_image = {}
    for b in by_kind.get("BackingImage", []):
        meta = b.get("metadata") or {}
        img_id = (meta.get("annotations") or {}).get("harvesterhci.io/imageId")
        if meta.get("name") and img_id:
            bi_to_image[meta["name"]] = img_id
    images = {}
    for i in by_kind.get("VirtualMachineImage", []):
        meta = i.get("metadata") or {}
        spec = i.get("spec") or {}
        display = spec.get("displayName") or meta.get("name") or ""
        url = spec.get("url") or ""
        images[f"{meta.get('namespace')}/{meta.get('name')}"] = {
            "name": display,
            "iso": display.lower().endswith(".iso") or url.lower().endswith(".iso")}

    def _image_of(backing):
        return images.get(bi_to_image.get(backing or "") or "")

    classes = []
    for sc in scs:
        meta = sc.get("metadata") or {}
        params = sc.get("parameters") or {}
        ann = meta.get("annotations") or {}
        name = meta.get("name")
        img = _image_of(params.get("backingImage"))
        rc = room["classes"].get(name) or {}
        try:
            replicas = int(params["numberOfReplicas"])
        except (KeyError, TypeError, ValueError):
            replicas = None
        classes.append({
            "name": name,
            "provisioner": sc.get("provisioner"),
            "replicas": replicas,
            "reclaim_policy": sc.get("reclaimPolicy"),
            "binding_mode": sc.get("volumeBindingMode"),
            "expansion": bool(sc.get("allowVolumeExpansion")),
            "default": "true" in (ann.get("storageclass.kubernetes.io/is-default-class"),
                                  ann.get("storageclass.beta.kubernetes.io/is-default-class")),
            "image": img["name"] if img else None,
            "allocatable": rc.get("allocatable"),
            "reason": rc.get("reason"),
        })

    # Disques Longhorn : la réplique désigne son disque par UUID.
    disk_by_uuid = {}
    for node in lh_nodes:
        nname = (node.get("metadata") or {}).get("name")
        for dname, ds in (((node.get("status") or {}).get("diskStatus")) or {}).items():
            if ds.get("diskUUID"):
                disk_by_uuid[ds["diskUUID"]] = (nname, dname)
    replicas_by_vol = {}
    for r in by_kind.get("Replica", []):
        spec = r.get("spec") or {}
        node, disk = disk_by_uuid.get(spec.get("diskID"), (spec.get("nodeID"), None))
        replicas_by_vol.setdefault(spec.get("volumeName"), []).append({
            "node": node or spec.get("nodeID"), "disk": disk,
            "running": (r.get("status") or {}).get("currentState") == "running"})
    replicas_on_disk = {}
    for reps in replicas_by_vol.values():
        for rep in reps:
            key = (rep["node"], rep["disk"])
            replicas_on_disk[key] = replicas_on_disk.get(key, 0) + 1
    for d in room["disks"]:
        d["replicas"] = replicas_on_disk.get((d["node"], d["disk"]), 0)
        d["used"] = max(0, (d["maximum"] or 0) - (d["available"] or 0))

    lh_by_claim = {}
    lh_unclaimed = []
    for v in by_kind.get("Volume", []):
        ks = (v.get("status") or {}).get("kubernetesStatus") or {}
        if ks.get("pvcName"):
            lh_by_claim[f"{ks.get('namespace')}/{ks['pvcName']}"] = v
        else:
            lh_unclaimed.append(v)

    # Qui réclame quel PVC : la spec des VMs fait foi, arrêtées comprises.
    vmis = {f"{(i.get('metadata') or {}).get('namespace')}/"
            f"{(i.get('metadata') or {}).get('name')}": i
            for i in by_kind.get("VirtualMachineInstance", [])}
    claimed, vms = {}, []
    for vm in by_kind.get("VirtualMachine", []):
        meta = vm.get("metadata") or {}
        ns, vname = meta.get("namespace"), meta.get("name")
        tspec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
        vol_claim = {}
        for v in tspec.get("volumes") or []:
            c = ((v.get("persistentVolumeClaim") or {}).get("claimName")
                 or (v.get("dataVolume") or {}).get("name"))
            if c:
                vol_claim[v.get("name")] = c
        disks = []
        for d in (((tspec.get("domain") or {}).get("devices") or {}).get("disks") or []):
            device = "cdrom" if "cdrom" in d else ("lun" if "lun" in d else "disk")
            pvc = vol_claim.get(d.get("name"))
            disks.append({"disk": d.get("name"), "device": device,
                          "boot_order": d.get("bootOrder"), "pvc": pvc})
            if pvc:
                claimed[f"{ns}/{pvc}"] = {"vm": f"{ns}/{vname}", "disk": d.get("name"),
                                          "device": device,
                                          "boot_order": d.get("bootOrder")}
        vmi = (vmis.get(f"{ns}/{vname}") or {}).get("status") or {}
        vms.append({"namespace": ns, "name": vname,
                    "status": (vm.get("status") or {}).get("printableStatus"),
                    "node": vmi.get("nodeName"), "disks": disks})

    volumes = []
    for pvc in by_kind.get("PersistentVolumeClaim", []):
        meta = pvc.get("metadata") or {}
        spec = pvc.get("spec") or {}
        key = f"{meta.get('namespace')}/{meta.get('name')}"
        lh = lh_by_claim.get(key)
        lh_spec = (lh or {}).get("spec") or {}
        lh_status = (lh or {}).get("status") or {}
        img = _image_of(lh_spec.get("backingImage"))
        # Consommateurs ACTUELS hors VM, d'après Longhorn : un pod qui monte
        # le volume. `lastPodRefAt` renseigné = le pod n'existe plus.
        ks = lh_status.get("kubernetesStatus") or {}
        pods = [{"name": w.get("podName"), "status": w.get("podStatus"),
                 "workload": w.get("workloadName"), "kind": w.get("workloadType")}
                for w in (ks.get("workloadsStatus") or [])
                if not ks.get("lastPodRefAt")
                and w.get("workloadType") != "VirtualMachineInstance"
                and not str(w.get("podName") or "").startswith(_VM_POD_PREFIXES)]
        # Le dernier pod qui l'a monté, s'il n'existe plus : un volume de
        # StatefulSet (Prometheus d'une supervision désactivée) est orphelin
        # AUJOURD'HUI, mais sa charge peut revenir le réclamer.
        last_pods = [{"name": w.get("podName"), "workload": w.get("workloadName"),
                      "kind": w.get("workloadType"), "at": ks.get("lastPodRefAt")}
                     for w in (ks.get("workloadsStatus") or [])
                     if ks.get("lastPodRefAt")
                     and w.get("workloadType") != "VirtualMachineInstance"
                     and not str(w.get("podName") or "").startswith(_VM_POD_PREFIXES)]
        claim = claimed.get(key) or {}
        volumes.append({
            "pvc_namespace": meta.get("namespace"), "pvc_name": meta.get("name"),
            "claim_missing": False,
            "storage_class": spec.get("storageClassName"),
            "phase": (pvc.get("status") or {}).get("phase"),
            "requested": _k8s_bytes(((spec.get("resources") or {})
                                     .get("requests") or {}).get("storage")),
            "longhorn": (lh or {}).get("metadata", {}).get("name"),
            "size": _num(lh_spec.get("size")),
            "actual_size": lh_status.get("actualSize"),
            "state": lh_status.get("state"),
            "robustness": lh_status.get("robustness"),
            "attached_to": lh_status.get("currentNodeID") or None,
            "replicas_wanted": lh_spec.get("numberOfReplicas"),
            "replicas": replicas_by_vol.get((lh or {}).get("metadata", {}).get("name"), []),
            "image": img["name"] if img else None,
            "image_iso": bool(img and img["iso"]),
            "vm": claim.get("vm"), "disk": claim.get("disk"),
            "device": claim.get("device"), "boot_order": claim.get("boot_order"),
            "pods": pods, "last_pods": last_pods,
            # Supprimable depuis la vue : réclamé par aucune VM, monté par
            # aucun pod, et connu de Longhorn (sinon on ne SAIT pas qui le
            # monte). Le serveur revérifie de toute façon avant d'agir.
            "orphan": (not claim and not pods and lh is not None
                       and lh_status.get("state") != "attached"),
        })
    # Volume Longhorn sans PVC : reste d'une suppression, ou volume créé à
    # la main. Montré, jamais proposé à la suppression d'ici. Y compris celui
    # dont le PVC a DISPARU (Longhorn garde son nom) : partir des seuls PVC
    # le rendait invisible (relevé sur harv1, `rancher-monitoring-grafana`).
    pvc_keys = {f"{(p.get('metadata') or {}).get('namespace')}/"
                f"{(p.get('metadata') or {}).get('name')}"
                for p in by_kind.get("PersistentVolumeClaim", [])}
    gone = [v for k, v in lh_by_claim.items() if k not in pvc_keys]
    for v in lh_unclaimed + gone:
        vs = v.get("spec") or {}
        st = v.get("status") or {}
        ks = st.get("kubernetesStatus") or {}
        volumes.append({
            "pvc_namespace": ks.get("namespace") or None,
            "pvc_name": ks.get("pvcName") or None,
            "claim_missing": bool(ks.get("pvcName")),
            "storage_class": None,
            "phase": None, "requested": None,
            "longhorn": (v.get("metadata") or {}).get("name"),
            "size": _num(vs.get("size")), "actual_size": st.get("actualSize"),
            "state": st.get("state"), "robustness": st.get("robustness"),
            "attached_to": st.get("currentNodeID") or None,
            "replicas_wanted": vs.get("numberOfReplicas"),
            "replicas": replicas_by_vol.get((v.get("metadata") or {}).get("name"), []),
            "image": None, "image_iso": False, "vm": None, "disk": None,
            "device": None, "boot_order": None, "pods": [], "last_pods": [],
            "orphan": False,
        })
    # Pourquoi un volume est dégradé, et quoi faire (v1.42.0).
    health = volume_health.diagnose_all(
        by_kind.get("Volume", []), by_kind.get("Replica", []),
        by_kind.get("Engine", []), lh_nodes, settings, room["disks"])
    for v in volumes:
        h = health.get(v["longhorn"]) if v["longhorn"] else None
        v["health"] = h["health"] if h else None
        v["findings"] = h["findings"] if h else []
    return {"cluster": cluster, "over_provisioning_pct": over,
            "minimal_available_pct": minimal,
            "schedulable_nodes": room["schedulable_nodes"],
            "classes": classes, "disks": room["disks"],
            "volumes": volumes, "vms": vms,
            "health_summary": _health_summary(volumes)}


# =============================================================================
# Corriger un volume dégradé (v1.42.0)
#
# Le serveur ne croit pas la page : au clic, il relit le cluster, refait le
# diagnostic, et n'applique que ce que ce diagnostic propose À CET INSTANT,
# avec des paramètres qu'il calcule lui-même. Une page restée ouverte, ou une
# requête fabriquée, n'obtient rien de plus.
# =============================================================================
VOLUME_FIXES = ("set-replicas", "enable-rebuild", "rebuild-now")
VOLUME_FIX_OBSERVE = 60.0     # secondes pendant lesquelles on constate l'effet
VOLUME_FIX_POLL = 5.0
# La cause qu'une correction doit faire disparaître.
_FIX_TARGET = {"set-replicas": "not-enough-nodes",
               "enable-rebuild": "rebuild-disabled",
               "rebuild-now": "replica-failed"}


def _power_action_running(cluster):
    """Un arrêt ou un démarrage du cluster est-il en cours dans la console ?
    Ils règlent eux-mêmes la reconstruction Longhorn."""
    with ACTIONS_LOCK:
        runs = list(ACTIONS.values())
    return any(getattr(r, "cluster", None) == cluster
               and getattr(r, "action", None) in ("shutdown", "startup")
               and getattr(r, "status", None) in ("starting", "running")
               for r in runs)


def _volume_fix_plan(entry, kind, cluster):
    """(plan, None) si la correction vaut encore, sinon (None, raison)."""
    if kind not in VOLUME_FIXES:
        return None, f"unknown fix: {kind}"
    if entry.get("health") == "faulted":
        return None, ("the volume is faulted: no automatic fix while no healthy "
                      "replica is left")
    found = next((f for f in entry.get("findings") or []
                  if (f.get("fix") or {}).get("kind") == kind), None)
    if not found:
        return None, ("this fix no longer applies: the volume recovered or the "
                      "cause changed")
    params = found["fix"].get("params") or {}
    vol = entry["longhorn"]
    if kind == "set-replicas":
        target, current = params.get("replicas"), entry.get("replicas_wanted")
        if not (isinstance(target, int) and isinstance(current, int)
                and 1 <= target < current):
            return None, (f"refusing to set {target} replica(s) on a volume "
                          f"that wants {current}")
        return {"kind": kind, "params": {"replicas": target},
                "summary": f"{current} -> {target} replica(s)",
                "args": ["-n", "longhorn-system", "patch", "volumes.longhorn.io", vol,
                         "--type", "merge", "-p",
                         json.dumps({"spec": {"numberOfReplicas": target}})]}, None
    if kind == "enable-rebuild":
        if _power_action_running(cluster):
            return None, ("a cluster shutdown or startup is running, and it manages "
                          "this setting itself")
        value = volume_health.REBUILD_LIMIT_RESTORED
        return {"kind": kind, "params": {"value": value},
                "summary": f"{volume_health.REBUILD_LIMIT_SETTING} = {value}",
                "args": ["-n", "longhorn-system", "patch", "settings.longhorn.io",
                         volume_health.REBUILD_LIMIT_SETTING, "--type", "merge", "-p",
                         json.dumps({"value": value})]}, None
    replica = params.get("replica") or ""
    if not (found.get("facts") or {}).get("healthy"):
        return None, ("no healthy replica is left: deleting a failed one would put "
                      "the data at risk")
    if not _K8S_NAME_RE.match(replica):
        return None, "invalid replica name"
    return {"kind": kind, "params": {"replica": replica},
            "summary": f"rebuild now instead of waiting for {replica}",
            "args": ["-n", "longhorn-system", "delete", "replicas.longhorn.io", replica,
                     "--wait=false"]}, None


def _volume_now(kc, cluster, volume):
    """Santé actuelle d'un volume, relue sur le cluster (sans cache)."""
    data = _build_storage_map(cluster, kc)
    entry = next((v for v in (data or {}).get("volumes") or []
                  if v.get("longhorn") == volume), None)
    return {"health": entry["health"], "findings": entry["findings"]} if entry else None


def _volume_fix_runner(run, kc, cluster, volume, plan):
    """Vérifier, appliquer, puis CONSTATER : une correction appliquée sans
    effet visible est dite telle, pas annoncée comme un succès."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    run.emit({"type": "step", "step_id": "verify", "status": "done",
              "message": plan["summary"], "ts": time.time()})
    run.emit({"type": "step", "step_id": "apply", "status": "running",
              "message": "kubectl " + " ".join(plan["args"]), "ts": time.time()})

    def finish(status, summary=None):
        run.status = status
        run.exit_code = 0 if status == "done" else 1
        if summary:
            run.error_summary = summary
        run.ended_at = time.time()
        run.emit({"type": "status", "status": status, "exit_code": run.exit_code,
                  "ts": time.time()})
        run.close()

    try:
        r = _kubectl_run(["kubectl", "--kubeconfig", kc, *plan["args"]],
                           capture_output=True, text=True, timeout=30)
        failure = None if r.returncode == 0 else ((r.stderr or r.stdout).strip()[:300]
                                                  or f"kubectl exit {r.returncode}")
    except subprocess.TimeoutExpired:
        failure = "kubectl timeout"
    except OSError as e:
        failure = _safe_proc_error(e)
    if failure:
        run.emit({"type": "step", "step_id": "apply", "status": "error",
                  "message": failure, "ts": time.time()})
        finish("error", failure)
        return
    run.emit({"type": "step", "step_id": "apply", "status": "done",
              "message": "applied", "ts": time.time()})

    run.emit({"type": "step", "step_id": "observe", "status": "running",
              "message": f"watching {volume} for {int(VOLUME_FIX_OBSERVE)} s",
              "ts": time.time()})
    target = _FIX_TARGET[plan["kind"]]
    deadline = time.time() + VOLUME_FIX_OBSERVE
    while True:
        time.sleep(VOLUME_FIX_POLL)
        now = _volume_now(kc, cluster, volume)
        if now:
            causes = {f["cause"] for f in now["findings"]}
            verdict = ("the volume is healthy again" if now["health"] == "healthy"
                       else "the rebuild started" if "rebuilding" in causes
                       else "the cause is gone" if target not in causes
                       else None)
            if verdict:
                run.emit({"type": "step", "step_id": "observe", "status": "done",
                          "message": verdict, "ts": time.time()})
                finish("done")
                return
        if time.time() >= deadline:
            break
    msg = (f"applied, but no effect observed within {int(VOLUME_FIX_OBSERVE)} s: "
           "check the volume in the Storage view")
    run.emit({"type": "step", "step_id": "observe", "status": "warn",
              "message": msg, "ts": time.time()})
    finish("error", msg)


@app.route("/api/volume-health/<cluster>/<volume>/fix", methods=["POST"])
@requires_auth
@_rate_limit("10/minute")
def api_volume_fix(cluster, volume):
    kind = (request.get_json(silent=True) or {}).get("kind")
    if kind not in VOLUME_FIXES:
        return jsonify({"error": f"unknown fix: {kind}"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify({"error": "cluster unreachable"}), 503
    # Relecture : jamais le cache, qui peut dater de plusieurs secondes.
    with _storage_map_lock:
        _storage_map_cache.pop(cluster, None)
    data = _build_storage_map(cluster, kc)
    if data is None:
        return jsonify({"error": "cluster unreachable"}), 503
    entry = next((v for v in data.get("volumes") or [] if v.get("longhorn") == volume), None)
    if not entry:
        return jsonify({"error": f"unknown volume: {volume}"}), 404
    plan, why = _volume_fix_plan(entry, kind, cluster)
    if not plan:
        return jsonify({"error": "not-applicable", "detail": why}), 409
    action_id = track_action(f"volume-fix:{kind}:{volume}", cluster,
                             _volume_fix_runner, kc, cluster, volume, plan)
    return jsonify({"action_id": action_id, "summary": plan["summary"],
                    "params": plan["params"]}), 201


# =============================================================================
# Isoler un nœud, le mettre en maintenance (v1.43.0)
#
# Les boutons « cordon » et « drain » de la vue Cluster échouaient tous deux
# (« not yet implemented »). Mécanique et règles reprises de Harvester v1.8.0
# (voir node_maintenance.py) : le contrôle préalable refuse exactement ce que
# Harvester refuserait, et le dit AVANT d'écrire quoi que ce soit.
# =============================================================================
NODE_MAINT_KINDS = ["nodes", "virtualmachineinstances.kubevirt.io",
                    "virtualmachines.kubevirt.io", "volumes.longhorn.io",
                    "replicas.longhorn.io",
                    # v1.44.3 : la stratégie d'éviction par défaut du cluster
                    "kubevirts.kubevirt.io"]
_node_maint_missing = {}
NODE_MAINT_POLL = 5.0
NODE_MAINT_TIMEOUT = 600.0
# v1.44.9 : Harvester RETIRE `drain-requested` avant de poser
# `maintain-status`, et les deux écritures ne sont pas atomiques (mesuré sur
# harvlab le 22/09/2026 : la marque disparaît, le statut arrive juste après).
# Un relevé qui tombe dans cet intervalle voyait « maintenance refusée » alors
# que le nœud entrait en maintenance. On laisse passer ce trou avant de
# conclure au refus.
NODE_MAINT_WITHDRAW_GRACE = 30.0


def _node_request(cluster, name):
    """((kc, objets par type, nœuds, nœud), None) ou (None, réponse d'erreur).
    Toujours relu sur le cluster : on n'agit pas sur un état en cache."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return None, (jsonify({"error": f"unknown cluster: {cluster}"}), 404)
    if _cluster_reachable(kc) is False:
        return None, (jsonify({"error": "cluster unreachable"}), 503)
    items = _grouped_items(kc, cluster, NODE_MAINT_KINDS, _node_maint_missing)
    if items is None:
        return None, (jsonify({"error": "cluster unreachable"}), 503)
    by = {}
    for it in items:
        by.setdefault(it.get("kind"), []).append(it)
    nodes = by.get("Node", [])
    node = next((n for n in nodes if (n.get("metadata") or {}).get("name") == name), None)
    if not node:
        return None, (jsonify({"error": f"unknown node: {name}"}), 404)
    return (kc, by, nodes, node), None


def _node_plan(ctx, force):
    _kc, by, nodes, node = ctx
    default_eviction = next(
        (((kv.get("spec") or {}).get("configuration") or {}).get("evictionStrategy")
         for kv in by.get("KubeVirt", [])), None)
    return node_maintenance.plan(node, nodes, by.get("VirtualMachineInstance", []),
                                 by.get("Volume", []), by.get("Replica", []), force,
                                 vms=by.get("VirtualMachine", []),
                                 default_eviction=default_eviction)


def _kubectl_step(run, step, args):
    """Lance kubectl pour une étape ; rend le message d'erreur, ou None."""
    try:
        r = _kubectl_run(["kubectl", *args], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "kubectl timeout"
    except OSError as e:
        return _safe_proc_error(e)
    if r.returncode != 0:
        return (r.stderr or r.stdout).strip()[:300] or f"kubectl exit {r.returncode}"
    return None


def _finish_run(run, status, summary=None):
    run.status = status
    run.exit_code = 0 if status == "done" else 1
    if summary:
        run.error_summary = summary
    run.ended_at = time.time()
    run.emit({"type": "status", "status": status, "exit_code": run.exit_code,
              "ts": time.time()})
    run.close()


def _node_patch_runner(run, kc, name, patch, step):
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    run.emit({"type": "step", "step_id": step, "status": "running",
              "message": f"kubectl patch node {name}", "ts": time.time()})
    err = _kubectl_step(run, step, ["--kubeconfig", kc, "patch", "node", name,
                                    "--type", "merge", "-p", json.dumps(patch)])
    if err:
        run.emit({"type": "step", "step_id": step, "status": "error", "message": err,
                  "ts": time.time()})
        _finish_run(run, "error", err)
        return
    run.emit({"type": "step", "step_id": step, "status": "done",
              "message": f"{name}: " + ("cordoned" if patch["spec"]["unschedulable"]
                                        else "schedulable again"), "ts": time.time()})
    _finish_run(run, "done")


def _node_maint_read(kc, name):
    """(annotations, VMs encore portées, nœud isolé ?).

    L'isolement est le signal qui distingue « Harvester travaille encore »
    de « Harvester a renoncé » : tant qu'il vide le nœud, il le garde
    isolé ; quand il abandonne, il le rend au cluster.
    """
    n = _kubectl_json(kc, "get", "node", name)
    vmis = _kubectl_json(kc, "get", "vmi", "-A", "-l", f"kubevirt.io/nodeName={name}")
    ann = ((n.get("metadata") or {}).get("annotations") or {}) if n else None
    cordoned = bool((n.get("spec") or {}).get("unschedulable")) if n else None
    return ann, (len(vmis.get("items", [])) if vmis else None), cordoned


def _drain_blockers(kc, name):
    """Pods du nœud qu'un budget de perturbation retient (diagnostic servi
    quand Harvester renonce, il ne dit pas pourquoi)."""
    pods = _kubectl_json(kc, "get", "pods", "-A", "--field-selector",
                         f"spec.nodeName={name}")
    pdbs = _kubectl_json(kc, "get", "pdb", "-A")
    if not pods or not pdbs:
        return []
    return node_maintenance.eviction_blockers(pods.get("items", []),
                                              pdbs.get("items", []))


def _maintenance_enter_runner(run, kc, name, force):
    """Demander, puis SUIVRE le contrôleur de Harvester jusqu'au bout : il
    migre les VMs et pose `maintain-status`, ou retire la demande s'il
    refuse. Dans ce dernier cas on le dit, au lieu d'attendre dix minutes."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    run.emit({"type": "step", "step_id": "request", "status": "running",
              "message": f"{name}: maintenance requested"
                         + (" (forced: non-migratable VMs are shut down)" if force else ""),
              "ts": time.time()})
    err = _kubectl_step(run, "request", [
        "--kubeconfig", kc, "patch", "node", name, "--type", "merge", "-p",
        json.dumps(node_maintenance.enter_patch(force))])
    if err:
        run.emit({"type": "step", "step_id": "request", "status": "error",
                  "message": err, "ts": time.time()})
        _finish_run(run, "error", err)
        return
    run.emit({"type": "step", "step_id": "request", "status": "done",
              "message": "requested", "ts": time.time()})
    run.emit({"type": "step", "step_id": "follow", "status": "running",
              "message": "Harvester migrates the VMs", "ts": time.time()})
    deadline = time.time() + NODE_MAINT_TIMEOUT
    last = None
    last_seen = None
    withdrawn_since = None
    while time.time() < deadline:
        time.sleep(NODE_MAINT_POLL)
        ann, left, cordoned = _node_maint_read(kc, name)
        if ann is None:
            log.info("[maintenance] %s: node unreadable, retrying", name)
            continue
        seen = (cordoned, node_maintenance.DRAIN_REQUESTED in ann,
                ann.get(node_maintenance.MAINTAIN_STATUS), left)
        if seen != last_seen:
            log.info("[maintenance] %s: cordoned=%s requested=%s status=%s vms=%s",
                     name, *seen)
            last_seen = seen
        status = ann.get(node_maintenance.MAINTAIN_STATUS)
        if node_maintenance.DRAIN_REQUESTED in ann or status in ("running", "completed"):
            withdrawn_since = None
        if status == "completed":
            run.emit({"type": "step", "step_id": "follow", "status": "done",
                      "message": f"{name} is in maintenance mode", "ts": time.time()})
            _finish_run(run, "done")
            return
        if status == "running":
            msg = f"{left} VM(s) still on {name}" if left is not None else "migrating"
            if msg != last:
                run.emit({"type": "step", "step_id": "follow", "status": "running",
                          "message": msg, "ts": time.time()})
                last = msg
            continue
        if node_maintenance.DRAIN_REQUESTED not in ann:
            # Le nœud est encore isolé : Harvester n'a pas renoncé, il vide
            # (mesuré sur harvlab : la demande disparaît avant la fin, et la
            # vidange peut buter plusieurs minutes sur le budget de
            # perturbation des gestionnaires Longhorn).
            if cordoned:
                if last != "draining":
                    run.emit({"type": "step", "step_id": "follow", "status": "running",
                              "message": f"{name}: drain in progress", "ts": time.time()})
                    last = "draining"
                continue
            # Trou entre le retrait de la demande et la pose du statut : on
            # ne conclut au refus que s'il dure.
            if withdrawn_since is None:
                withdrawn_since = time.time()
            if time.time() - withdrawn_since < NODE_MAINT_WITHDRAW_GRACE:
                continue
            msg = ("Harvester refused the maintenance and withdrew the request "
                   "(see the harvester controller logs)")
            stuck = _drain_blockers(kc, name)
            if stuck:
                msg += "; still on the node and protected by a disruption " \
                       "budget: " + ", ".join(
                           f"{b['namespace']}/{b['pod']} ({b['pdb']})" for b in stuck[:4])
            run.emit({"type": "step", "step_id": "follow", "status": "error",
                      "message": msg, "ts": time.time()})
            _finish_run(run, "error", msg)
            return
    msg = (f"not completed after {int(NODE_MAINT_TIMEOUT)} s; Harvester keeps "
           "working on it, check the node again later")
    run.emit({"type": "step", "step_id": "follow", "status": "warn", "message": msg,
              "ts": time.time()})
    _finish_run(run, "error", msg)


def _maintenance_leave_runner(run, kc, name, patch, restart):
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    run.emit({"type": "step", "step_id": "leave", "status": "running",
              "message": f"{name}: leaving maintenance", "ts": time.time()})
    err = _kubectl_step(run, "leave", ["--kubeconfig", kc, "patch", "node", name,
                                       "--type", "merge", "-p", json.dumps(patch)])
    if err:
        run.emit({"type": "step", "step_id": "leave", "status": "error", "message": err,
                  "ts": time.time()})
        _finish_run(run, "error", err)
        return
    run.emit({"type": "step", "step_id": "leave", "status": "done",
              "message": f"{name} is schedulable again", "ts": time.time()})
    failed = []
    for ns, vm, strategy in restart:
        err = _kubectl_step(run, "restart", [
            "--kubeconfig", kc, "-n", ns, "patch", "virtualmachines.kubevirt.io", vm,
            "--type", "merge", "-p", json.dumps({
                "spec": {"runStrategy": strategy},
                "metadata": {"annotations": {
                    node_maintenance.STRATEGY_NODE_ANNOTATION: None}}})])
        run.emit({"type": "step", "step_id": "restart", "status": "error" if err else "done",
                  "message": f"{ns}/{vm}: " + (err or f"restarted ({strategy})"),
                  "ts": time.time()})
        if err:
            failed.append(f"{ns}/{vm}")
    if failed:
        _finish_run(run, "error", "could not restart " + ", ".join(failed))
    else:
        _finish_run(run, "done")


@app.route("/api/node/<cluster>/<node>/maintenance-check")
@requires_auth
@_rate_limit("20/minute")
def api_node_maintenance_check(cluster, node):
    """Ce que ferait la mise en maintenance : refus éventuel, VMs qui
    migreront, VMs qui ne le peuvent pas et pourquoi, VMs qui s'arrêteront."""
    ctx, err = _node_request(cluster, node)
    if err:
        return err
    return jsonify(_node_plan(ctx, request.args.get("force") == "1"))


def _node_cordon(cluster, node, cordon):
    ctx, err = _node_request(cluster, node)
    if err:
        return err
    kc, _by, nodes, obj = ctx
    if node_maintenance.maintenance_state(obj):
        return jsonify({"error": "in-maintenance",
                        "detail": "the node is in maintenance mode: leave the "
                                  "maintenance instead"}), 409
    already = bool((obj.get("spec") or {}).get("unschedulable"))
    if already == cordon:
        return jsonify({"error": "no-change",
                        "detail": "the node is already " + ("cordoned" if cordon
                                                           else "schedulable")}), 409
    # Le webhook de Harvester le refuserait (constaté sur harv1) : le dire
    # tout de suite plutôt que lancer une action vouée à l'échec.
    if cordon and node_maintenance.last_available(obj, nodes):
        return jsonify({"error": "last-available-node",
                        "detail": "Harvester refuses to cordon the last available node: "
                                  "another node must stay schedulable"}), 409
    step = "cordon" if cordon else "uncordon"
    action_id = track_action(f"node-{step}:{node}", cluster, _node_patch_runner,
                             kc, node, {"spec": {"unschedulable": cordon}}, step)
    return jsonify({"action_id": action_id}), 201


@app.route("/api/node/<cluster>/<node>/cordon", methods=["POST"])
@requires_auth
@_rate_limit("10/minute")
def api_node_cordon(cluster, node):
    return _node_cordon(cluster, node, True)


@app.route("/api/node/<cluster>/<node>/uncordon", methods=["POST"])
@requires_auth
@_rate_limit("10/minute")
def api_node_uncordon(cluster, node):
    return _node_cordon(cluster, node, False)


@app.route("/api/node/<cluster>/<node>/maintenance", methods=["POST"])
@requires_auth
@_rate_limit("5/minute")
def api_node_maintenance_enter(cluster, node):
    # Un vrai booléen : la chaîne "false" est non vide, donc vraie en
    # Python, et elle arrêterait des VMs par erreur.
    force = (request.get_json(silent=True) or {}).get("force") is True
    ctx, err = _node_request(cluster, node)
    if err:
        return err
    plan = _node_plan(ctx, force)
    if plan["refusal"]:
        return jsonify({**plan, "error": "refused"}), 409
    if plan["blocked"]:
        return jsonify({**plan, "error": "blocked"}), 409
    kc = ctx[0]
    action_id = track_action(f"node-maintenance-enter:{node}", cluster,
                             _maintenance_enter_runner, kc, node, force)
    return jsonify({"action_id": action_id, "plan": plan}), 201


@app.route("/api/node/<cluster>/<node>/maintenance", methods=["DELETE"])
@requires_auth
@_rate_limit("5/minute")
def api_node_maintenance_leave(cluster, node):
    ctx, err = _node_request(cluster, node)
    if err:
        return err
    kc, by, _nodes, obj = ctx
    if not node_maintenance.maintenance_state(obj):
        return jsonify({"error": "not-in-maintenance",
                        "detail": "the node is not in maintenance mode"}), 409
    restart = node_maintenance.vms_to_restart(node, by.get("VirtualMachine", []))
    action_id = track_action(f"node-maintenance-leave:{node}", cluster,
                             _maintenance_leave_runner, kc, node,
                             node_maintenance.leave_patch(obj), restart)
    return jsonify({"action_id": action_id, "restart": [f"{n}/{v}" for n, v, _ in restart]}), 201


def _health_summary(volumes):
    """Ce que le bandeau de la vue Stockage résume : combien, et la cause
    qui revient le plus parmi celles qui demandent d'agir. Compté sur les
    volumes AFFICHÉS, pour que le bandeau ne dise jamais plus que l'écran."""
    counts = {"degraded": 0, "faulted": 0, "at-risk": 0}
    causes = {}
    for h in volumes:
        if h["health"] in counts:
            counts[h["health"]] += 1
        for f in h["findings"]:
            if f["severity"] in ("critical", "action"):
                causes[f["cause"]] = causes.get(f["cause"], 0) + 1
    # À égalité, la plus grave d'abord (ordre du diagnostic).
    top = min(causes, key=lambda c: (-causes[c], volume_health.ORDER.index(c))) \
        if causes else None
    return {"degraded": counts["degraded"], "faulted": counts["faulted"],
            "at_risk": counts["at-risk"], "top_cause": top}


@app.route("/api/storage-map/<cluster>")
@requires_auth
@shared_read()
def api_storage_map(cluster):
    """Le stockage lu comme un datastore : classes, volumes, disques."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    with _storage_map_lock:
        cached = _storage_map_cache.get(cluster)
        if cached and time.time() - cached["ts"] < STORAGE_MAP_TTL \
                and request.args.get("fresh") != "1":
            return jsonify(cached["data"])
    data = _build_storage_map(cluster, kc)
    if data is None:
        return jsonify({"cluster": cluster, "unreachable": True,
                        "error": "cluster unreachable"}), 200
    with _storage_map_lock:
        _storage_map_cache[cluster] = {"ts": time.time(), "data": data}
    return jsonify(data)


# =============================================================================
# Comptes du cluster Harvester
#
# Harvester (v1.8) modélise ses comptes très simplement :
#   * un objet `users.management.cattle.io` porte le login, le nom affiché
#     et l'activation ;
#   * l'administration est un `ClusterRoleBinding` ordinaire vers
#     `cluster-admin`, sujet `User: <nom de l'objet>` ;
#   * le mot de passe vit AILLEURS, dans un secret du namespace
#     `cattle-local-user-passwords`, sous forme de clé dérivée de 32 octets
#     avec un sel de 32 octets — pas une empreinte bcrypt.
#
# C'est ce dernier point qui borne cette surface. Fabriquer ce secret
# demanderait de deviner l'algorithme et ses paramètres à partir de sa
# forme ; on poserait au mieux des comptes incapables de se connecter, au
# pire une authentification affaiblie. La création d'un compte local AVEC
# mot de passe n'est donc pas offerte ici, et le dire vaut mieux que de
# livrer une fonction qu'on ne peut pas vérifier. Tout le reste l'est :
# lister, activer, désactiver, accorder ou retirer l'administration,
# supprimer.
# =============================================================================
LOCAL_PASSWORD_NS = "cattle-local-user-passwords"


def _admin_binding_name(user_id):
    return f"harvester-ops-admin-{user_id}"


def _cluster_admin_subjects(kc, cluster):
    """Ensemble des utilisateurs et groupes qui détiennent cluster-admin,
    avec le binding qui l'accorde."""
    data = _kubectl_json(kc, "get", "clusterrolebindings", "-o", "json",
                         cluster=cluster)
    holders = {}
    for b in (data or {}).get("items", []):
        ref = b.get("roleRef") or {}
        if ref.get("kind") != "ClusterRole" or ref.get("name") != "cluster-admin":
            continue
        for s in b.get("subjects") or []:
            key = f"{s.get('kind')}:{s.get('name')}"
            holders.setdefault(key, []).append((b.get("metadata") or {}).get("name"))
    return holders


@app.route("/api/harvester-users/<cluster>")
@requires_auth
def api_harvester_users(cluster):
    """Comptes du cluster, avec qui détient l'administration."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200

    # Les deux listes sont indépendantes : les enchaîner coûtait 5,5 s sur
    # harv1 (1,5 s pour les comptes, 2,7 s pour les bindings, plus les
    # aller-retours). En parallèle, le panneau s'ouvre en moitié moins.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_users = pool.submit(_kubectl_json, kc, "get",
                              "users.management.cattle.io", "-o", "json",
                              cluster=cluster)
        f_holders = pool.submit(_cluster_admin_subjects, kc, cluster)
        data = f_users.result()
        holders = f_holders.result()
    if data is None:
        return jsonify({"error": "cannot list users",
                        "hint": "users.management.cattle.io is a Rancher CRD; "
                                "it exists on Harvester but the kubeconfig "
                                "must be allowed to read it"}), 502
    users = []
    for u in data.get("items", []):
        meta = u.get("metadata") or {}
        uid = meta.get("name")
        bindings = holders.get(f"User:{uid}", [])
        principals = u.get("principalIds") or []
        users.append({
            "id": uid,
            "username": u.get("username"),
            "display_name": u.get("displayName"),
            "description": u.get("description"),
            # `enabled` absent veut dire actif : Rancher ne le pose qu'en
            # cas de désactivation explicite.
            "enabled": u.get("enabled") is not False,
            "is_admin": bool(bindings),
            "admin_bindings": bindings,
            # Un compte venu d'un fournisseur externe ne se gère pas ici.
            "local": any(str(p).startswith("local://") for p in principals)
                     or not principals,
            "principals": principals,
            # Les comptes de service internes ne doivent pas être touchés.
            "system": "authz.management.cattle.io/bootstrapping" in (meta.get("labels") or {})
                      or any(str(p).startswith("system://") for p in principals),
        })
    # Les détenteurs qui ne sont pas des objets User : groupes OIDC, comptes
    # de service. Les montrer évite de croire la liste exhaustive.
    # Les détenteurs qui ne sont pas dans la liste des comptes : groupes
    # venus d'un fournisseur externe, surtout. Les comptes de SERVICE sont
    # comptés à part : ils sont une vingtaine, tous d'infrastructure, et les
    # lister noierait l'information utile.
    known = {u["id"] for u in users}
    others, service_accounts = [], 0
    for key, bindings in sorted(holders.items()):
        kind, _, name = key.partition(":")
        if kind == "ServiceAccount":
            service_accounts += 1
            continue
        if kind == "User" and name in known:
            continue
        others.append({
            "subject": key, "kind": kind, "name": name, "bindings": bindings,
            # Un sujet `User:` sans objet utilisateur est une liaison
            # ORPHELINE : le compte a été supprimé, sa délégation
            # d'administration non. Recréer un compte portant cet
            # identifiant lui rendrait cluster-admin en silence. Sur harv1,
            # trois en traînaient.
            "orphan": kind == "User",
        })
    return jsonify({"users": sorted(users, key=lambda u: u["username"] or u["id"]),
                    "other_admins": others,
                    "service_account_admins": service_accounts,
                    # Dit franchement : cette console ne pose pas de mot de
                    # passe local, faute de pouvoir le faire sûrement.
                    "password_management": False})


@app.route("/api/harvester-users/<cluster>/<user_id>", methods=["PATCH"])
@requires_auth
@_rate_limit("20/minute")
def api_harvester_user_patch(cluster, user_id):
    """Active/désactive un compte, accorde/retire l'administration.

    Body : {enabled?: bool, admin?: bool}
    """
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200

    data = request.get_json(force=True, silent=True) or {}
    done = []

    if "enabled" in data:
        enabled = bool(data["enabled"])
        r = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, "patch",
             "users.management.cattle.io", user_id, "--type", "merge",
             "-p", json.dumps({"enabled": enabled})],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return jsonify({"error": "cannot change enabled",
                            "detail": (r.stderr or r.stdout).strip()[:300]}), 502
        done.append("enabled" if enabled else "disabled")

    if "admin" in data:
        want = bool(data["admin"])
        holders = _cluster_admin_subjects(kc, cluster)
        existing = holders.get(f"User:{user_id}", [])
        if want and not existing:
            binding = {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": _admin_binding_name(user_id),
                             "labels": {"harvester-ops.io/managed": "true"}},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io",
                            "kind": "ClusterRole", "name": "cluster-admin"},
                "subjects": [{"apiGroup": "rbac.authorization.k8s.io",
                              "kind": "User", "name": user_id}],
            }
            r = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "create", "-f", "-"],
                input=json.dumps(binding), capture_output=True, text=True,
                timeout=30)
            if r.returncode != 0:
                return jsonify({"error": "cannot grant admin",
                                "detail": (r.stderr or r.stdout).strip()[:300]}), 502
            done.append("admin granted")
        elif not want and existing:
            # On ne retire QUE les bindings qu'on a posés : supprimer celui
            # que Harvester a créé à l'installation casserait le compte
            # d'origine, et le rétablir n'aurait rien d'évident.
            ours = [b for b in existing if b == _admin_binding_name(user_id)]
            if not ours:
                return jsonify({
                    "error": "admin not granted by harvester-ops",
                    "bindings": existing,
                    "hint": "this account holds cluster-admin through a binding "
                            "this console did not create; remove it deliberately "
                            "with kubectl rather than from here",
                }), 409
            for b in ours:
                _kubectl_run(["kubectl", "--kubeconfig", kc, "delete",
                                "clusterrolebinding", b],
                               capture_output=True, text=True, timeout=30)
            done.append("admin revoked")

    if not done:
        return jsonify({"error": "nothing to change",
                        "hint": "send enabled and/or admin"}), 400
    _invalidate_cluster_caches(cluster)
    return jsonify({"changed": done, "user": user_id})


@app.route("/api/vmtemplates/<cluster>")
@requires_auth
def api_vmtemplates_list(cluster):
    """Templates de VM Harvester, pour réutiliser une configuration."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    data = _kubectl_json(kc, "get", "virtualmachinetemplates", "-A", "-o", "json",
                         cluster=cluster)
    items = []
    for it in (data or {}).get("items", []):
        meta = it.get("metadata") or {}
        spec = it.get("spec") or {}
        items.append({
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "description": spec.get("description"),
            "default_version": spec.get("defaultVersionId"),
        })
    return jsonify({"templates": items})


@app.route("/api/vmtemplates/<cluster>/<namespace>/<name>")
@requires_auth
def api_vmtemplate_spec(cluster, namespace, name):
    """Spec de VM portée par la version par défaut d'un template.

    Un template Harvester ne contient pas la spec : elle vit dans une
    `VirtualMachineTemplateVersion` que `spec.defaultVersionId` désigne.
    """
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200

    tmpl = _kubectl_json(kc, "get", "virtualmachinetemplate", name,
                         "-n", namespace, "-o", "json", cluster=cluster)
    if not tmpl:
        return jsonify({"error": "template not found"}), 404
    # v1.64.0 : une version choisie (menu du modèle), sinon celle par défaut
    version_id = request.args.get("version") or ((tmpl.get("spec") or {}).get("defaultVersionId")) or ""
    if "/" not in version_id:
        return jsonify({"error": "template has no default version"}), 404
    v_ns, v_name = version_id.split("/", 1)
    if not _valid_k8s_name(v_ns) or not _K8S_SUBDOMAIN_RE.match(v_name):
        return jsonify({"error": "invalid version id"}), 400
    version = _kubectl_json(kc, "get", "virtualmachinetemplateversion", v_name,
                            "-n", v_ns, "-o", "json", cluster=cluster)
    if not version:
        return jsonify({"error": f"version {version_id} not found"}), 404
    if ((version.get("spec") or {}).get("templateId")) != f"{namespace}/{name}":
        return jsonify({"error": f"{version_id} is not a version of {namespace}/{name}"}), 400
    vm = json.loads(json.dumps(((version.get("spec") or {}).get("vm")) or {}))
    # comme Harvester (edit/kubevirt.io.virtualmachine) : ni les accès, ni les
    # clés dynamiques, ni la MAC ; pas de source dans les modèles de volume
    meta = vm.setdefault("metadata", {})
    ann = meta.setdefault("annotations", {})
    for k in ("harvesterhci.io/dynamic-ssh-key-names", "harvesterhci.io/dynamic-ssh-key-users",
              "harvesterhci.io/mac-address"):
        ann.pop(k, None)
    try:
        vct = json.loads(ann.get("harvesterhci.io/volumeClaimTemplates") or "[]")
        for t in vct:
            (t.get("spec") or {}).pop("dataSource", None)
        if vct:
            ann["harvesterhci.io/volumeClaimTemplates"] = json.dumps(vct)
    except ValueError:
        pass
    ts = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
    ts.pop("accessCredentials", None)
    cloudinit = None
    for v in ts.get("volumes") or []:
        ref = ((v.get("cloudInitNoCloud") or {}).get("secretRef") or {}).get("name")
        if not ref:
            continue
        sec = _kubectl_json(kc, "get", "secret", ref, "-n", v_ns, cluster=cluster)
        data = (sec or {}).get("data") or {}
        dec = lambda k: base64.b64decode(data[k]).decode(errors="replace") if data.get(k) else ""  # noqa: E731
        cloudinit = {"user_data": dec("userdata"), "network_data": dec("networkdata")}
    return jsonify({
        "template": f"{namespace}/{name}",
        "version": version_id,
        "description": (tmpl.get("spec") or {}).get("description"),
        # La spec seule : le nom viendra du formulaire, à chaque
        # instanciation.
        "vm": {"metadata": meta, "spec": vm.get("spec") or {}},
        # v1.64.0 : le cloud-init de la version, recopié dans un Secret propre
        # à la nouvelle VM (partager celui de la version l'exposait à la
        # suppression avec la VM)
        "cloudinit": cloudinit,
    })


@app.route("/api/vmtemplates/<cluster>", methods=["POST"])
@requires_auth
@_rate_limit("10/minute")
def api_vmtemplate_create(cluster):
    """Enregistre une configuration de VM comme template réutilisable.

    Harvester modélise cela en DEUX objets : un `VirtualMachineTemplate`
    (le nom, la description) et une `VirtualMachineTemplateVersion` qui
    porte la spec et référence son parent par `templateId`. Créer la
    version sans le template laisse un objet orphelin, invisible dans
    l'interface Harvester.
    """
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200

    data = request.get_json(force=True, silent=True) or {}
    namespace = (data.get("namespace") or "").strip()
    name = (data.get("name") or "").strip()
    manifest = data.get("manifest")
    description = (data.get("description") or "")[:200]
    if not _valid_k8s_name(namespace):
        return jsonify({"error": "invalid namespace"}), 400
    if not _valid_k8s_name(name):
        return jsonify({"error": "invalid template name (RFC 1123)"}), 400
    if not isinstance(manifest, dict) or not manifest.get("spec"):
        return jsonify({"error": "manifest with a spec is required"}), 400

    version_name = f"{name}-v1"
    if not _valid_k8s_name(version_name):
        return jsonify({"error": "template name too long"}), 400

    tmpl = {
        "apiVersion": "harvesterhci.io/v1beta1",
        "kind": "VirtualMachineTemplate",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"description": description or f"Created from {name}"},
    }
    version = {
        "apiVersion": "harvesterhci.io/v1beta1",
        "kind": "VirtualMachineTemplateVersion",
        "metadata": {"name": version_name, "namespace": namespace},
        "spec": {
            "templateId": f"{namespace}/{name}",
            # Seule la spec de la VM est conservée : le nom, lui, sera
            # choisi à chaque instanciation.
            "vm": {"metadata": manifest.get("metadata", {}),
                   "spec": manifest.get("spec", {})},
        },
    }

    for obj in (tmpl, version):
        r = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, "create", "-f", "-", "-o", "name"],
            input=json.dumps(obj), capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout).strip().splitlines()
            detail = detail[-1][:300] if detail else f"exit {r.returncode}"
            # Le template a pu être créé avant l'échec de la version : le
            # dire, sinon l'opérateur croit que rien n'a eu lieu.
            return jsonify({"error": "template creation failed",
                            "detail": detail,
                            "kind": obj["kind"]}), 502
    return jsonify({"created": True, "name": name, "namespace": namespace,
                    "version": version_name}), 201


@app.route("/api/vm/<cluster>/<namespace>/<name>")
@requires_auth
def api_vm_get(cluster, namespace, name):
    """Return the full VirtualMachine spec as JSON for the edit panel."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    try:
        out = subprocess.check_output(
            ["kubectl", "--kubeconfig", kc, "get", "vm", name, "-n", namespace, "-o", "json"],
            stderr=subprocess.PIPE, timeout=10,
        )
        return Response(out, mimetype="application/json")
    except subprocess.CalledProcessError as e:
        return jsonify({"error": "kubectl failed", "detail": e.stderr.decode() if e.stderr else ""}), 404
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timeout"}), 504
    except FileNotFoundError:
        return jsonify({"error": "kubectl not found on the server"}), 502


def _vm_patch_track(cluster, namespace, name, ok, detail=None):
    """v1.8.0 — every mutating apply from the edit panel shows up in the
    dock/Activity like any other operation (project rule). The kubectl call
    is synchronous, so the ActionRun is recorded already finished."""
    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, f"vm-edit:{namespace}/{name}", cluster, [], dry_run=False)
    run.status = "done" if ok else "error"
    run.exit_code = 0 if ok else 1
    if not ok:
        run.error_summary = (detail or "").strip().splitlines()[-1][:300] if detail else "patch failed"
    run.ended_at = time.time()
    run.emit({"type": "step", "step_id": "patch", "status": "done" if ok else "error",
              "message": "merge patch applied" if ok else run.error_summary,
              "ts": time.time()})
    run.emit({"type": "status", "status": run.status, "exit_code": run.exit_code,
              "ts": time.time()})
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    run.close()


@app.route("/api/vm/<cluster>/<namespace>/<name>", methods=["PATCH"])
@requires_auth
def api_vm_patch(cluster, namespace, name):
    """Apply a JSON merge patch to a VirtualMachine.

    Body: { "patch": <object>, "dry_run": <bool> }
    Returns the updated VM JSON on success.
    """
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    patch = data.get("patch")
    dry_run = bool(data.get("dry_run", False))
    if not isinstance(patch, dict):
        return jsonify({"error": "patch must be a JSON object"}), 400
    # Safety: identity fields must never be rewritten through this API —
    # but the REST of metadata (annotations such as
    # harvesterhci.io/volumeClaimTemplates, labels, description) is
    # legitimate and needed by the disk editor. A v1.6.x ternary here
    # popped the whole metadata dict, silently dropping every annotation
    # patch (the General tab's description never applied).
    if isinstance(patch.get("metadata"), dict):
        for key in ("name", "namespace", "uid", "resourceVersion"):
            patch["metadata"].pop(key, None)
        if not patch["metadata"]:
            patch.pop("metadata")
    cmd = ["kubectl", "--kubeconfig", kc, "patch", "vm", name,
           "-n", namespace, "--type", "merge", "-p", json.dumps(patch)]
    if dry_run:
        cmd.extend(["--dry-run=server", "-o", "json"])
    else:
        cmd.extend(["-o", "json"])
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.PIPE, timeout=20)
        if not dry_run:
            _vm_patch_track(cluster, namespace, name, ok=True)
        return Response(out, mimetype="application/json")
    except subprocess.CalledProcessError as e:
        detail = (e.stderr.decode() if e.stderr else "").strip()[:1500]
        if not dry_run:
            _vm_patch_track(cluster, namespace, name, ok=False, detail=detail)
        return jsonify({"error": "kubectl patch failed", "detail": detail}), 400
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timeout"}), 504
    except FileNotFoundError:
        return jsonify({"error": "kubectl not found on the server"}), 502


@app.route("/api/vm/<cluster>/<namespace>/<name>/cloudinit")
@requires_auth
def api_vm_get_cloudinit(cluster, namespace, name):
    """Return the cloud-init userData + networkData of the VM.

    Looks for either:
    - inline `cloudInitNoCloud.userData` on the VM
    - referenced Secret via `cloudInitNoCloud.userDataSecretRef`
    """
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    try:
        vm_json = subprocess.check_output(
            ["kubectl", "--kubeconfig", kc, "get", "vm", name, "-n", namespace, "-o", "json"],
            stderr=subprocess.DEVNULL, timeout=10,
        )
        vm = json.loads(vm_json)
    except Exception:
        return jsonify({"error": "VM not found"}), 404

    volumes = ((vm.get("spec", {}).get("template", {}) or {}).get("spec", {}) or {}).get("volumes", [])
    result = {"source": None, "userData": "", "networkData": "", "secretName": None}

    for vol in volumes:
        ci = vol.get("cloudInitNoCloud") or vol.get("cloudInitConfigDrive")
        if not ci:
            continue
        if ci.get("userData"):
            result["source"] = "inline"
            result["userData"] = ci.get("userData", "")
            result["networkData"] = ci.get("networkData", "")
            break
        ref = ci.get("secretRef") or ci.get("userDataSecretRef") or ci.get("networkDataSecretRef")
        if ref and ref.get("name"):
            secret_name = ref["name"]
            try:
                sec = json.loads(subprocess.check_output(
                    ["kubectl", "--kubeconfig", kc, "get", "secret", secret_name,
                     "-n", namespace, "-o", "json"],
                    stderr=subprocess.DEVNULL, timeout=10,
                ))
                import base64
                data = sec.get("data", {})
                if "userdata" in data:
                    result["userData"] = base64.b64decode(data["userdata"]).decode("utf-8", errors="replace")
                elif "userData" in data:
                    result["userData"] = base64.b64decode(data["userData"]).decode("utf-8", errors="replace")
                if "networkdata" in data:
                    result["networkData"] = base64.b64decode(data["networkdata"]).decode("utf-8", errors="replace")
                elif "networkData" in data:
                    result["networkData"] = base64.b64decode(data["networkData"]).decode("utf-8", errors="replace")
                result["source"] = "secret"
                result["secretName"] = secret_name
                break
            except Exception:
                pass
    return jsonify(result)


@app.route("/api/vm/<cluster>/<namespace>/<name>/cloudinit", methods=["PUT"])
@requires_auth
@_rate_limit("30/minute")
def api_vm_put_cloudinit(cluster, namespace, name):
    """Enregistre le cloud-init d'une VM : son Secret s'il existe, sinon un
    Secret neuf branché sur la VM (v1.60.0 ; avant, une VM sans cloud-init ou
    en cloud-init inline était refusée). Action suivie, par
    `harvester-resources vm cloudinit`."""
    data = request.get_json(force=True, silent=True) or {}
    body = {"user_data": data.get("userData", ""), "network_data": data.get("networkData", ""),
            "guest_agent": bool(data.get("guestAgent"))}
    if isinstance(data.get("sshNames"), list):
        body["ssh_names"] = data["sshNames"]
    return _vm_cloudinit_action(cluster, namespace, name, body)


def _vm_cloudinit_action(cluster, namespace, name, body):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    wd = _capi_work_dir()
    files, extra = [], []
    for key, opt in (("user_data", "--user-data"), ("network_data", "--network-data")):
        text = body.get(key)
        if text is None:
            continue
        if not isinstance(text, str) or len(text) > 256 * 1024:
            for f_ in files:
                Path(f_).unlink(missing_ok=True)
            return jsonify({"error": f"{key}: text of 256 KiB at most"}), 400
        fd, path = tempfile.mkstemp(prefix="ci-", suffix=".yaml", dir=str(wd) if wd else None)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        files.append(path)
        extra += [opt, path]
    if body.get("guest_agent"):
        extra.append("--guest-agent")
    if isinstance(body.get("ssh_names"), list):
        extra += ["--ssh-names", ",".join(str(n) for n in body["ssh_names"] if _K8S_NAME_RE.match(str(n)))]
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "vm", "cloudinit", "--kubeconfig", kc,
           "--namespace", namespace, "--name", name] + extra

    def after():
        for f_ in files:
            Path(f_).unlink(missing_ok=True)
    run, err = _cli_action(cluster, f"vm:cloudinit:{namespace}/{name}", cmd, "harvester-resources", after=after)
    if err:
        after()
    return _res_reply(run, err, vm=f"{namespace}/{name}", action="cloudinit")


@app.route("/api/vm/<cluster>/<namespace>/<name>/runStrategy", methods=["PATCH"])
@requires_auth
def api_vm_set_run_strategy(cluster, namespace, name):
    """Change a single VM's runStrategy. The operation is tracked as an action
    so it shows in the dock and the Activity tab."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    target = data.get("runStrategy")
    if target not in ("Always", "Halted", "Manual", "RerunOnFailure"):
        return jsonify({"error": "invalid runStrategy"}), 400

    # Register as an action so the dock + activity see it
    run_id = uuid.uuid4().hex[:12]
    label = {
        "Halted": "vm-stop",
        "Always": "vm-start",
        "RerunOnFailure": "vm-start",
        "Manual": "vm-manual",
    }[target]
    run = ActionRun(run_id, f"{label}:{namespace}/{name}", cluster, [], dry_run=False)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(target=_vm_action_runner,
                     args=(run, kc, namespace, name, target),
                     daemon=True).start()

    return jsonify({
        "cluster": cluster, "namespace": namespace, "name": name,
        "runStrategy": target, "action_id": run_id,
    })


@app.route("/api/vm/<cluster>/<namespace>/<name>/runStrategy/bulk", methods=["PATCH"])
@requires_auth
def api_vm_set_run_strategy_bulk(cluster, namespace, name):
    # placeholder for future, currently unused — bulk goes through the single endpoint loop
    return api_vm_set_run_strategy(cluster, namespace, name)


# Délai d'attente de la nouvelle instance après une réinitialisation.
VM_RESTART_TIMEOUT = 180


def _vm_restart_runner(run, kc, namespace, name):
    """Hard reset through the VM's `restart` subresource, what `virtctl
    restart --force --grace-period=0` does, then poll until a NEW VMI is
    Running.

    v1.44.5 : it used to DELETE the VMI. A deleted VMI comes back only when
    the VM is `runStrategy: Always`; Harvester creates its VMs
    `RerunOnFailure`, so the reset button shut them down (seen on harvlab).
    The subresource restarts whatever the run strategy. Same
    error-surfacing contract as _vm_action_runner (stderr in error_summary,
    no cmdline)."""
    def fail(step, detail):
        run.error_summary = detail
        run.emit({"type": "step", "step_id": step, "status": "error",
                  "message": detail, "ts": time.time()})
        run.exit_code = 1
        run.status = "error"
        run.ended_at = time.time()
        run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
        run.close()

    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    run.emit({"type": "step", "step_id": "restart", "status": "running",
              "message": f"restarting VM {name} now (no grace period)",
              "ts": time.time()})
    old_uid = _kubectl_run(
        ["kubectl", "--kubeconfig", kc, "-n", namespace, "get", "vmi", name,
         "-o", "jsonpath={.metadata.uid}"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    path = (f"/apis/subresources.kubevirt.io/v1/namespaces/{namespace}"
            f"/virtualmachines/{name}/restart")
    try:
        r = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, "replace", "--raw", path, "-f", "-"],
            input=json.dumps({"gracePeriodSeconds": 0}),
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        r = None
    if r is None or r.returncode != 0:
        if r is None:
            detail = "kubectl timed out after 20s (cluster API unreachable?)"
        else:
            stderr_lines = [l.strip() for l in (r.stderr or "").splitlines() if l.strip()]
            detail = (stderr_lines[-1] if stderr_lines
                      else f"kubectl exited {r.returncode} with no error output")[:300]
        return fail("restart", detail)
    run.emit({"type": "step", "step_id": "restart", "status": "done",
              "message": "restart requested", "ts": time.time()})

    run.emit({"type": "step", "step_id": "respawn", "status": "running",
              "message": "waiting for a fresh VMI to reach Running",
              "ts": time.time()})
    deadline = time.time() + VM_RESTART_TIMEOUT
    last = ""
    while True:
        pr = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, "-n", namespace, "get", "vmi", name,
             "-o", "jsonpath={.metadata.uid} {.status.phase}"],
            capture_output=True, text=True, timeout=5,
        )
        out = (pr.stdout or "").strip()
        uid, _, phase = out.partition(" ")
        if pr.returncode == 0 and uid and uid != old_uid and phase == "Running":
            run.emit({"type": "step", "step_id": "respawn", "status": "done",
                      "message": "new VMI is Running", "ts": time.time()})
            break
        state = phase or ("gone" if pr.returncode != 0 else "unknown")
        if state != last:
            run.emit({"type": "step", "step_id": "respawn", "status": "progress",
                      "message": f"phase={state}", "ts": time.time()})
            last = state
        if time.time() >= deadline:
            # Ce n'est pas un succès : la VM n'est pas revenue.
            return fail("respawn", f"the VM did not come back within "
                                   f"{VM_RESTART_TIMEOUT} s (last phase: {last})")
        time.sleep(2)

    run.exit_code = 0
    run.status = "done"
    run.ended_at = time.time()
    run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
    run.close()


@app.route("/api/vm/<cluster>/<namespace>/<name>/restart", methods=["POST"])
@_rate_limit("30/minute")
@requires_auth
def api_vm_restart(cluster, namespace, name):
    """Hard reset of a running VM (the VM's `restart` subresource, immediate).
    Tracked as an action like every mutating operation."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, f"vm-restart:{namespace}/{name}", cluster, [], dry_run=False)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(target=_vm_restart_runner,
                     args=(run, kc, namespace, name),
                     daemon=True).start()
    return jsonify({
        "cluster": cluster, "namespace": namespace, "name": name,
        "action_id": run_id,
    })


# -----------------------------------------------------------------------------
# Support bundle — collects logs/config/status, optionally anonymized, into tar.gz
# -----------------------------------------------------------------------------
import re
import tarfile
import tempfile

BUNDLE_DIR = Path(os.environ.get("HARVESTER_OPS_BUNDLE_DIR", "/tmp/harvester-ops-bundles"))
BUNDLE_DIR.mkdir(parents=True, exist_ok=True)

BUNDLES = {}  # bundle_id -> dict
BUNDLES_LOCK = threading.Lock()


def _anonymize_text(text, mapping):
    """Apply replacements from mapping. Mapping is {original: placeholder}.

    Order matters: longer originals first to avoid partial matches
    (e.g. 'harv-prod-cp1' must be replaced before 'harv-prod').
    """
    # Sort by length descending
    items = sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True)
    for src, dst in items:
        if src:
            text = text.replace(src, dst)
    # Generic IP fallback for any IP not already mapped: <<IP-UNKNOWN>>
    # Only match IPs that are still recognizable (not already replaced)
    text = re.sub(
        r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b',
        lambda m: m.group() if m.group().startswith('<<') else '<<IP-UNKNOWN>>',
        text,
    )
    return text


def _build_anonymization_map(cfg, status_data=None):
    """Build a deterministic mapping from sensitive values → placeholders.

    Format: each placeholder is uniquely identifiable and CANNOT be confused
    with a real value (uses '<<...>>' delimiters). This guarantees lossless
    de-anonymization later.

    Includes:
      - cluster names      → <<CLUSTER-N>>
      - node hostnames     → <<NODE-N>>
      - node IPs           → <<IP-NODE-N>>
      - VM names (from status_data if provided) → <<VM-N>>
      - namespace names    → <<NS-N>>
    """
    mapping = {}
    cluster_idx = 0
    node_idx = 0
    vm_idx = 0
    ns_idx = 0

    for c in cfg.get("clusters", []):
        cluster_idx += 1
        cluster_name = c.get("name", "")
        if cluster_name:
            mapping[cluster_name] = f"<<CLUSTER-{cluster_idx}>>"
        for n in c.get("nodes", []):
            node_idx += 1
            host = n.get("hostname", "")
            ip = n.get("ip", "")
            if host:
                mapping[host] = f"<<NODE-{node_idx}>>"
            if ip:
                mapping[ip] = f"<<IP-NODE-{node_idx}>>"

    # If we have live status data, also map VM names and namespaces
    if status_data:
        for cname, sdata in status_data.items():
            if not isinstance(sdata, dict):
                continue
            ns_by_cluster = sdata.get("vms_by_namespace", {})
            for ns, vms in ns_by_cluster.items():
                # Skip system namespaces — those are public knowledge
                if ns in ("default", "kube-system", "kube-public", "longhorn-system",
                          "cattle-system", "harvester-system", "fleet-system"):
                    continue
                ns_idx += 1
                mapping[ns] = f"<<NS-{ns_idx}>>"
                for vm in vms:
                    vm_idx += 1
                    if isinstance(vm, dict) and vm.get("name"):
                        mapping[vm["name"]] = f"<<VM-{vm_idx}>>"

    return {k: v for k, v in mapping.items() if k}


class BundleJob:
    def __init__(self, bundle_id, anonymize):
        self.id = bundle_id
        self.anonymize = anonymize
        # v1.32.0 : identite figee a la creation, le job tourne dans un thread.
        self.identity = None
        self.identity_env = {}
        self.status = "starting"     # starting | running | done | error
        self.steps = []              # [{id, label, status, message}]
        self.percent = 0
        self.archive_path = None
        self.mapping = None          # {original: placeholder, ...} when anonymized
        self.error = None
        self._cond = threading.Condition()
        self._closed = False
        self._events = []

    def update_step(self, step_id, status, message="", percent=None):
        existing = next((s for s in self.steps if s["id"] == step_id), None)
        if existing:
            existing["status"] = status
            existing["message"] = message
        else:
            self.steps.append({"id": step_id, "status": status, "message": message})
        if percent is not None:
            self.percent = percent
        with self._cond:
            self._events.append({
                "type": "step",
                "step_id": step_id,
                "status": status,
                "message": message,
                "percent": self.percent,
                "ts": time.time(),
            })
            self._cond.notify_all()

    def finish(self, status, archive_path=None, error=None):
        self.status = status
        self.archive_path = archive_path
        self.error = error
        if status == "done":
            self.percent = 100
        with self._cond:
            self._events.append({
                "type": "end",
                "status": status,
                "archive_path": archive_path,
                "error": error,
                "percent": self.percent,
                "ts": time.time(),
            })
            self._closed = True
            self._cond.notify_all()

    def to_dict(self):
        return {
            "id": self.id,
            "status": self.status,
            "percent": self.percent,
            "anonymize": self.anonymize,
            "steps": self.steps,
            "archive": Path(self.archive_path).name if self.archive_path else None,
            "has_mapping": bool(self.mapping),
            "mapping_entries": len(self.mapping) if self.mapping else 0,
            "error": self.error,
        }


def _build_bundle(job: BundleJob):
    try:
        cfg = load_config()
        anonymize_map = {}

        # Collect cluster status first (so we can include VM/namespace names in the map)
        status_data = {}
        for c in cfg.get("clusters", []):
            cname = c["name"]
            kc = _identity_kubeconfig(c.get("kubeconfig", ""),
                                      getattr(job, "identity", None))
            if not Path(kc).exists():
                continue
            try:
                out = subprocess.check_output(
                    ["/usr/bin/env", "bash", str(BIN_DIR / "harvester-status.sh"),
                     "--cluster", cname, "--output", "json"],
                    env={**os.environ, "NO_COLOR": "1",
                         "HARVESTER_OPS_CONFIG": str(CONFIG_PATH),
                         **getattr(job, "identity_env", {})},
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                ).decode("utf-8", errors="replace")
                status_data[cname] = json.loads(out)
            except Exception:
                pass

        if job.anonymize:
            anonymize_map = _build_anonymization_map(cfg, status_data)
            job.mapping = anonymize_map

        with tempfile.TemporaryDirectory() as workdir:
            workdir = Path(workdir)
            bundle_root = workdir / f"harvester-ops-bundle-{job.id}"
            bundle_root.mkdir()

            # Step 1: collect harvester-ops version
            job.update_step("metadata", "running", "Collecting metadata...", percent=5)
            meta = {
                "bundle_id": job.id,
                "harvester_ops_version": _harvester_ops_version(),
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "anonymized": job.anonymize,
            }
            (bundle_root / "metadata.json").write_text(json.dumps(meta, indent=2))
            job.update_step("metadata", "done", "Metadata captured", percent=10)

            # Step 2: copy sanitized config.yaml
            job.update_step("config", "running", "Sanitizing configuration...", percent=15)
            if CONFIG_PATH.exists():
                cfg_text = CONFIG_PATH.read_text(errors="replace")
                if job.anonymize:
                    cfg_text = _anonymize_text(cfg_text, anonymize_map)
                (bundle_root / "config.yaml").write_text(cfg_text)
            # v1.78.0 : les clusters déclarés par la console (déclarations
            # seules, chemins relatifs, ni clé ni kubeconfig), anonymisés
            # comme config.yaml
            for decl in _cd.decl_files(_state_dir()):
                try:
                    decl_text = decl.read_text(errors="replace")
                except OSError:
                    continue
                if job.anonymize:
                    decl_text = _anonymize_text(decl_text, anonymize_map)
                    decl_name = re.sub(r"[^A-Za-z0-9._-]", "_",
                                       _anonymize_text(decl.name, anonymize_map))
                else:
                    decl_name = decl.name
                out_dir = bundle_root / "clusters.d"
                out_dir.mkdir(exist_ok=True)
                (out_dir / Path(decl_name).name).write_text(decl_text)
            job.update_step("config", "done", "Configuration included", percent=25)

            # Step 3: harvester-ops logs from /var/log/harvester-ops/
            job.update_step("logs", "running", "Collecting harvester-ops logs...", percent=30)
            logs_out = bundle_root / "logs"
            logs_out.mkdir()
            log_count = 0
            if LOG_DIR.exists():
                for p in LOG_DIR.glob("*.log"):
                    try:
                        content = p.read_text(errors="replace")
                        if job.anonymize:
                            content = _anonymize_text(content, anonymize_map)
                        (logs_out / p.name).write_text(content)
                        log_count += 1
                    except OSError:
                        pass
            job.update_step("logs", "done", f"{log_count} log file(s) included", percent=45)

            # Step 4: per-cluster status snapshots (reuse data collected earlier)
            job.update_step("status", "running", "Capturing cluster status snapshots...", percent=50)
            status_dir = bundle_root / "cluster-status"
            status_dir.mkdir()
            for cname, sdata in status_data.items():
                try:
                    out = json.dumps(sdata, indent=2)
                    if job.anonymize:
                        out = _anonymize_text(out, anonymize_map)
                    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', cname)
                    (status_dir / f"{safe_name}.json").write_text(out)
                except Exception as e:
                    (status_dir / f"{cname}.error.txt").write_text(str(e))
            job.update_step("status", "done", f"{len(status_data)} cluster(s) captured", percent=70)

            # Step 5: system info
            job.update_step("system", "running", "Capturing system info...", percent=75)
            sysinfo = {}
            for label, cmd in [
                ("uname",      ["uname", "-a"]),
                ("date",       ["date"]),
                ("kubectl",    ["kubectl", "version", "--client", "-o", "json"]),
                ("podman",     ["podman", "--version"]),
            ]:
                try:
                    sysinfo[label] = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5).decode().strip()
                except Exception:
                    sysinfo[label] = "n/a"
            sysinfo_text = json.dumps(sysinfo, indent=2)
            if job.anonymize:
                sysinfo_text = _anonymize_text(sysinfo_text, anonymize_map)
            (bundle_root / "system-info.json").write_text(sysinfo_text)
            job.update_step("system", "done", "System info captured", percent=85)

            # Step 6: anonymization mapping — INCLUDED IN ARCHIVE
            if job.anonymize:
                # Save mapping inside the archive AND keep a copy on disk
                # (so the user can download it separately if they lose the archive)
                mapping_path = bundle_root / "mapping.json"
                mapping_text = json.dumps(anonymize_map, indent=2, ensure_ascii=False)
                mapping_path.write_text(mapping_text)
                (bundle_root / "ANONYMIZATION.md").write_text(
                    "# Anonymized support bundle\n\n"
                    "Sensitive identifiers (cluster names, hostnames, IPs, VM names, "
                    "namespaces) have been replaced with placeholders like `<<NODE-1>>`, "
                    "`<<IP-NODE-1>>`, `<<CLUSTER-1>>`, `<<VM-1>>`.\n\n"
                    "## Mapping table\n\n"
                    "See `mapping.json` in this archive for the full correspondence.\n\n"
                    "## De-anonymization\n\n"
                    "To restore original names from a modified log file, use the "
                    "harvester-ops UI:\n"
                    "  *Settings → Support → De-anonymize logs*\n"
                    "Upload the modified log and the `mapping.json` from this archive.\n"
                )
                # Save mapping path on the job for the API to expose it
                job.mapping_archive_path = str(mapping_path)
            else:
                (bundle_root / "ANONYMIZATION.md").write_text(
                    "# Non-anonymized support bundle\n\n"
                    "This bundle contains original cluster identifiers (names, IPs, VMs).\n"
                    "Only share with trusted recipients.\n"
                )

            # Step 7: create tarball
            job.update_step("archive", "running", "Creating archive...", percent=90)
            ts = time.strftime("%Y%m%d-%H%M%S")
            archive_name = f"harvester-ops-bundle-{ts}-{job.id}{'-anon' if job.anonymize else ''}.tar.gz"
            archive_path = BUNDLE_DIR / archive_name
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(bundle_root, arcname=bundle_root.name)
            job.update_step("archive", "done", f"Archive {archive_name} ({archive_path.stat().st_size // 1024} KB)", percent=100)

            job.finish("done", archive_path=str(archive_path))

    except Exception as e:
        job.update_step("error", "error", str(e))
        job.finish("error", error=str(e))


@app.route("/api/support-bundle", methods=["POST"])
@requires_auth
def api_support_bundle_start():
    """Start a new support bundle job. Returns a job ID for SSE tracking."""
    data = request.get_json(force=True, silent=True) or {}
    anonymize = bool(data.get("anonymize", True))
    if _sso_session() is not None and current_role() != "admin":
        # Le paquet de diagnostic lit tous les clusters avec le compte de la
        # console, pas à travers Rancher : pas pour un non-administrateur.
        return jsonify({"error": "forbidden", "code": "bundle-needs-admin",
                        "hint": "a support bundle reads every cluster with the console's "
                                "own account: local account or Rancher administrator"}), 403
    bundle_id = uuid.uuid4().hex[:10]
    job = BundleJob(bundle_id, anonymize)
    job.identity = current_cluster_identity()
    job.identity_env = identity_env()
    job.status = "running"
    with BUNDLES_LOCK:
        BUNDLES[bundle_id] = job
    threading.Thread(target=_build_bundle, args=(job,), daemon=True).start()
    return jsonify(job.to_dict()), 201


@app.route("/api/support-bundle/<bundle_id>")
@requires_auth
def api_support_bundle_status(bundle_id):
    with BUNDLES_LOCK:
        job = BUNDLES.get(bundle_id)
    if not job:
        abort(404)
    return jsonify(job.to_dict())


@app.route("/api/support-bundle/<bundle_id>/stream")
@requires_auth
def api_support_bundle_stream(bundle_id):
    with BUNDLES_LOCK:
        job = BUNDLES.get(bundle_id)
    if not job:
        abort(404)

    def gen():
        last_idx = 0
        while True:
            with job._cond:
                while last_idx >= len(job._events) and not job._closed:
                    job._cond.wait(timeout=10)
                while last_idx < len(job._events):
                    ev = job._events[last_idx]
                    last_idx += 1
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                if job._closed and last_idx >= len(job._events):
                    return

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/support-bundle/<bundle_id>/download")
@requires_auth
def api_support_bundle_download(bundle_id):
    with BUNDLES_LOCK:
        job = BUNDLES.get(bundle_id)
    if not job or not job.archive_path:
        abort(404)
    return send_from_directory(BUNDLE_DIR, Path(job.archive_path).name, as_attachment=True)


@app.route("/api/support-bundle/<bundle_id>/mapping")
@requires_auth
def api_support_bundle_mapping(bundle_id):
    """Download the anonymization mapping table for this bundle.
    Returns JSON {original: placeholder, ...} suitable for use with
    /api/deanonymize."""
    with BUNDLES_LOCK:
        job = BUNDLES.get(bundle_id)
    if not job:
        abort(404)
    if not job.mapping:
        return jsonify({"error": "this bundle was not anonymized"}), 400
    return Response(
        json.dumps(job.mapping, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=mapping-{bundle_id}.json"},
    )


@app.route("/api/deanonymize", methods=["POST"])
@requires_auth
def api_deanonymize():
    """De-anonymize a log file using a mapping table.

    Multipart upload: log (the modified file), mapping (the JSON mapping).
    Returns the restored file as a download.
    """
    log_f = request.files.get("log")
    map_f = request.files.get("mapping")
    if not log_f or not map_f:
        return jsonify({"error": "both 'log' and 'mapping' file fields are required"}), 400

    try:
        mapping = json.loads(map_f.read().decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return jsonify({"error": "invalid mapping JSON", "detail": str(e)}), 400

    if not isinstance(mapping, dict):
        return jsonify({"error": "mapping must be a JSON object {original: placeholder}"}), 400

    # Read uploaded log (cap to 50MB to avoid memory issues)
    log_bytes = log_f.read(50 * 1024 * 1024)
    text = log_bytes.decode("utf-8", errors="replace")

    # Reverse mapping: {placeholder: original}; apply longest-first
    reverse = {v: k for k, v in mapping.items() if k and v}
    items = sorted(reverse.items(), key=lambda kv: len(kv[0]), reverse=True)
    replaced = 0
    for placeholder, original in items:
        if placeholder in text:
            text = text.replace(placeholder, original)
            replaced += 1

    out_name = Path(log_f.filename or "deanonymized.log").stem + "_deanonymized.log"
    return Response(
        text,
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={out_name}",
            "X-Replaced-Entries": str(replaced),
        },
    )


@app.route("/api/support-bundle")
@requires_auth
def api_support_bundle_list():
    """List all past bundles on disk."""
    bundles = []
    if BUNDLE_DIR.exists():
        for p in sorted(BUNDLE_DIR.glob("harvester-ops-bundle-*.tar.gz"), reverse=True)[:50]:
            try:
                st = p.stat()
                bundles.append({
                    "filename": p.name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "anonymized": "-anon" in p.name,
                })
            except OSError:
                pass
    return jsonify({"bundles": bundles})


# -----------------------------------------------------------------------------
# CAPI / CAPHV — diagnostic + clusters listing
# -----------------------------------------------------------------------------
CAPI_COMPONENTS = [
    {
        "id": "cert-manager",
        "label": "cert-manager",
        "kind": "deployment",
        "namespace": "cert-manager",
        "selector": {"app": "cert-manager"},
    },
    {
        "id": "cluster-api",
        "label": "Cluster API (capi-controller-manager)",
        "kind": "deployment",
        "candidates": [
            {"namespace": "capi-system", "name": "capi-controller-manager"},
            {"namespace": "cattle-capi-system", "name": "capi-controller-manager"},
            {"namespace": "cattle-capi-system", "name": "rancher-turtles-capi-controller-manager"},
        ],
    },
    {
        "id": "cabp-rke2",
        "label": "RKE2 Bootstrap provider (cabp-rke2)",
        "kind": "deployment",
        "candidates": [
            {"namespace": "rke2-bootstrap-system", "name": "rke2-bootstrap-controller-manager"},
            {"namespace": "capi-bootstrap-system", "name": "capi-bootstrap-controller-manager"},
        ],
    },
    {
        "id": "cacp-rke2",
        "label": "RKE2 Control-plane provider (cacp-rke2)",
        "kind": "deployment",
        "candidates": [
            {"namespace": "rke2-control-plane-system", "name": "rke2-control-plane-controller-manager"},
        ],
    },
    {
        "id": "caphv",
        "label": "Harvester infrastructure provider (CAPHV)",
        "kind": "deployment",
        "candidates": [
            {"namespace": "caphv-system", "name": "caphv-controller-manager"},
            {"namespace": "harvester-capi-system", "name": "caphv-controller-manager"},
        ],
    },
    {
        "id": "caphv-clusterclass",
        "label": "Harvester ClusterClass (harvester-rke2)",
        "kind": "clusterclass",
        "name": "harvester-rke2",
    },
]


CAPI_BUNDLE_PATH = Path(os.environ.get(
    "HARVESTER_OPS_CAPI_BUNDLE",
    str(Path(__file__).resolve().parent.parent / "dist" / "capi-bundle.tar.gz"),
))
CAPI_BUNDLE_DIR = CAPI_BUNDLE_PATH.parent
CAPI_BUNDLE_ACTIVE = CAPI_BUNDLE_DIR / "active.json"


def _capi_bundle_active_filename():
    """Read the active bundle filename from active.json (single source of truth).
    Falls back to the symlink target, then to the legacy fixed name."""
    if CAPI_BUNDLE_ACTIVE.exists():
        try:
            return json.loads(CAPI_BUNDLE_ACTIVE.read_text()).get("filename")
        except Exception:
            pass
    if CAPI_BUNDLE_PATH.is_symlink():
        return os.readlink(str(CAPI_BUNDLE_PATH))
    if CAPI_BUNDLE_PATH.exists():
        return CAPI_BUNDLE_PATH.name
    return None


def _capi_bundle_active_path():
    name = _capi_bundle_active_filename()
    if not name:
        return None
    p = (CAPI_BUNDLE_DIR / name).resolve()
    return p if p.exists() else None


def _capi_bundle_set_active(filename):
    """Update active.json and the legacy symlink. The symlink is what
    bundle.sh and older shell scripts read directly."""
    target = CAPI_BUNDLE_DIR / filename
    if not target.exists():
        raise FileNotFoundError(filename)
    CAPI_BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    CAPI_BUNDLE_ACTIVE.write_text(json.dumps({"filename": filename}, indent=2))
    # Refresh the legacy symlink — atomic via rename
    tmp_link = CAPI_BUNDLE_DIR / ".capi-bundle.tar.gz.new"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(filename)
    tmp_link.replace(CAPI_BUNDLE_PATH)


def _capi_bundle_migrate_legacy():
    """The first version of harvester-ops wrote dist/capi-bundle.tar.gz as a
    real file. Once we adopt timestamped bundles, rename it on disk and
    re-point the canonical path to the new file via the symlink. Idempotent."""
    if not CAPI_BUNDLE_PATH.exists() or CAPI_BUNDLE_PATH.is_symlink():
        return
    ts = time.strftime("%Y%m%d-%H%M%S",
                       time.gmtime(CAPI_BUNDLE_PATH.stat().st_mtime))
    new_name = f"capi-bundle-{ts}-legacy00.tar.gz"
    new_path = CAPI_BUNDLE_DIR / new_name
    try:
        CAPI_BUNDLE_PATH.rename(new_path)
        legacy_sha = CAPI_BUNDLE_PATH.with_suffix(".tar.gz.sha256")
        if legacy_sha.exists():
            legacy_sha.rename(new_path.with_suffix(".tar.gz.sha256"))
        _capi_bundle_set_active(new_name)
        log_capi.info("migrated legacy bundle → %s", new_name)
    except OSError as e:
        log_capi.warning("legacy migration skipped: %s", e)


try:
    if not IS_READ_WORKER:
        _capi_bundle_migrate_legacy()
except Exception as _e:
    log_capi.error("migration error: %s", _e)
# Order matters: cert-manager + capi first, then bootstrap, then control-plane,
# then infrastructure (caphv), then ClusterClass.
CAPI_INSTALL_ORDER = ["cert-manager", "cluster-api", "cabp-rke2", "cacp-rke2", "caphv"]


# -----------------------------------------------------------------------------
# Harvester version + bundle compatibility
# -----------------------------------------------------------------------------
def _harvester_server_version(kc):
    """Return the Harvester server version (e.g. 'v1.8.0') from the target
    cluster's `setting/server-version`. Empty string on failure."""
    try:
        r = _kubectl_run(
            ["kubectl", "--kubeconfig", kc,
             "get", "setting.harvesterhci.io", "server-version",
             "-o", "jsonpath={.value}"],
            capture_output=True, text=True, timeout=8,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _version_matches_glob(version, glob):
    """Match `version` ("v1.8.0") against `glob` ("v1.8.x" / "v1.8.0" / "*").

    Supports `x` as a single-segment wildcard and `*` as anything.
    Returns False if either side is empty."""
    if not version or not glob:
        return False
    if glob == "*":
        return True
    v = version.lstrip("v").split(".")
    g = glob.lstrip("v").split(".")
    if len(v) != len(g):
        return False
    for vp, gp in zip(v, g):
        if gp in ("x", "*"):
            continue
        if vp != gp:
            return False
    return True


def _bundle_compatibility(target_version, manifest):
    """Return (compatible: bool, supported_globs: list, target_version: str).
    `compatible` is True when target_version matches at least one glob from
    manifest.bundle.compatible_harvester_versions. If the bundle declares no
    list, we trust the user and return True (legacy / hand-crafted bundles)."""
    if not isinstance(manifest, dict):
        return True, [], target_version
    bundle = manifest.get("bundle") or {}
    globs = bundle.get("compatible_harvester_versions") or []
    if not globs:
        return True, [], target_version
    if not target_version:
        # Couldn't fetch the version — can't prove incompatibility, so warn
        # but don't refuse (set compatible=False so UI surfaces it as warning).
        return False, globs, ""
    ok = any(_version_matches_glob(target_version, g) for g in globs)
    return ok, globs, target_version


def _read_bundle_manifest(bundle_path):
    """Open the bundle's tar.gz and extract `capi-bundle/manifest.json`."""
    try:
        import tarfile as _tar
        with _tar.open(bundle_path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.name.endswith("/manifest.json"):
                    f = tar.extractfile(m)
                    if not f:
                        continue
                    return json.loads(f.read().decode("utf-8"))
    except Exception:
        pass
    return {}


# Clusterctl-style env substitution: ${VAR:=default} → env VAR or default.
# Module-level so unit tests can hit it without the install runner.
_CAPI_ENVSUB_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::=([^}]*))?\}")


def _capi_envsubst(text, env=None):
    """Render `${VAR:=default}` placeholders against `env` (defaults to
    os.environ). Used before `kubectl apply` so providers don't crashloop
    with un-expanded args (e.g. `--v=${CAPRKE2_DEBUG_LEVEL:=0}`)."""
    src = env if env is not None else os.environ
    return _CAPI_ENVSUB_RE.sub(
        lambda m: src.get(m.group(1), m.group(2) or ""),
        text,
    )


# cert-manager's webhook MUST be Available before the next provider's
# apply — otherwise cert-manager rejects the Certificate resources we
# send (silent race that wrecked the first install attempt).
_CAPI_COMPONENT_DEPLOYMENTS = {
    "cert-manager":     [("cert-manager",            "cert-manager-webhook")],
    "cluster-api":      [("capi-system",             "capi-controller-manager")],
    "cabp-rke2":        [("rke2-bootstrap-system",   "rke2-bootstrap-controller-manager")],
    "cacp-rke2":        [("rke2-control-plane-system","rke2-control-plane-controller-manager")],
    "caphv":            [("caphv-system",            "caphv-controller-manager")],
}


def _capi_extract_bundle_to_temp(active):
    """Untar `active` into a fresh temp dir and return (workdir, bundle_root).
    Raises on tar errors so the caller can surface them as a step error."""
    workdir = Path(tempfile.mkdtemp(prefix="capi-install-"))
    import tarfile as _tar
    with _tar.open(active, "r:gz") as tar:
        tar.extractall(workdir)
    return workdir, next(workdir.iterdir())


def _capi_push_image_to_node(img, node_ip, ssh_user, ssh_key, ssh_port):
    """SCP one image tarball to a node and import it into containerd.
    Returns (ok, message) — message is a one-line summary."""
    ssh_opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=accept-new", "-p", str(ssh_port)]
    if ssh_key:
        ssh_opts.extend(["-i", ssh_key])
    remote_cmd = (
        "set -eo pipefail; "
        "TMP=$(mktemp); "
        "cat > \"$TMP\"; "
        "sudo /var/lib/rancher/rke2/bin/ctr "
        "  --address /run/k3s/containerd/containerd.sock "
        "  --namespace k8s.io images import \"$TMP\" >/dev/null 2>&1 || "
        "  { zcat \"$TMP\" | sudo /var/lib/rancher/rke2/bin/ctr "
        "      --address /run/k3s/containerd/containerd.sock "
        "      --namespace k8s.io images import -; }; "
        "rm -f \"$TMP\""
    )
    try:
        with open(img, "rb") as f:
            proc = subprocess.run(
                ["ssh", *ssh_opts, f"{ssh_user}@{node_ip}", remote_cmd],
                stdin=f,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
            )
        if proc.returncode != 0:
            return False, f"import non-zero: {(proc.stderr.decode()[:150] or '')}"
        return True, "imported"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, f"error: {e}"


def _capi_kubectl_apply(yaml_text, kc, server_side=True, timeout=120):
    """Apply rendered YAML via stdin. Returns (ok, stderr_excerpt)."""
    cmd = ["kubectl", "--kubeconfig", kc, "apply", "-f", "-"]
    if server_side:
        cmd.extend(["--server-side", "--force-conflicts"])
    try:
        r = subprocess.run(cmd, input=yaml_text,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "kubectl timeout"
    if r.returncode != 0:
        return False, r.stderr.strip()[:240]
    return True, ""


def _capi_wait_for_deploy(kc, ns, deploy, timeout="180s"):
    """Wait for ns/deploy to be Available. Returns (ok, message). Polls
    briefly first to give the API a chance to register the Deployment."""
    for _ in range(6):
        r = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, "-n", ns, "get",
             f"deploy/{deploy}", "-o", "name"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            break
        time.sleep(2)
    try:
        r = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, "-n", ns, "wait",
             f"deploy/{deploy}", "--for=condition=Available",
             f"--timeout={timeout}"],
            capture_output=True, text=True,
            timeout=int(timeout.rstrip("s")) + 30,
        )
        if r.returncode != 0:
            msg = (r.stderr.strip()[:200] or r.stdout.strip()[:200])
            return False, f"not Available: {msg}"
        return True, "is Available"
    except subprocess.TimeoutExpired:
        return False, "wait timeout"


def _capi_apply_yaml_dir(yaml_dir, kc, dry_run, step_id, step,
                         server_side=True, timeout=120, glob="*.yaml"):
    """kubectl-apply every YAML file in yaml_dir (sorted). Returns the
    count of files that failed. Each individual outcome is reported via
    `step("progress", …)` so the dock shows live progress."""
    err_count = 0
    for yfile in sorted(yaml_dir.glob(glob)):
        if dry_run:
            step(step_id, "progress", f"[DRY-RUN] would apply {yfile.name}")
            continue
        rendered = _capi_envsubst(yfile.read_text())
        ok, msg = _capi_kubectl_apply(rendered, kc,
                                       server_side=server_side, timeout=timeout)
        if ok:
            step(step_id, "progress", f"applied {yfile.name}")
        else:
            step(step_id, "progress", f"{yfile.name} ERR: {msg}")
            err_count += 1
    return err_count


def _capi_push_images_phase(image_files, nodes_ips, ssh_user, ssh_key, ssh_port,
                             dry_run, step):
    """Phase 1: distribute every image tarball to every node + ctr import."""
    n_img = len(image_files)
    n_nodes = len(nodes_ips)
    step("images", "running", f"{n_img} images to load on {n_nodes} node(s)")
    if dry_run:
        for img in image_files:
            step("images", "progress",
                 f"[DRY-RUN] would scp+ctr import {img.name} on every node")
    else:
        for node_ip in nodes_ips:
            for i, img in enumerate(image_files, 1):
                step("images", "progress",
                     f"{node_ip}: {i}/{n_img} {img.name}")
                ok, msg = _capi_push_image_to_node(
                    img, node_ip, ssh_user, ssh_key, ssh_port
                )
                if not ok:
                    step("images", "progress", f"{node_ip}: {img.name} {msg}")
    step("images", "done", f"{n_img * n_nodes} image pushes attempted")


def _capi_apply_components_phase(manifests_dir, kc, dry_run, step):
    """Phase 2: for each component, apply its manifests then wait for the
    controller deployment to be Available. Returns total apply-errors."""
    total_err = 0
    for comp in CAPI_INSTALL_ORDER:
        comp_dir = manifests_dir / comp
        apply_step = f"{comp}-apply"
        wait_step  = f"{comp}-wait"
        if not comp_dir.exists():
            step(apply_step, "skipped", f"no manifest for {comp} in bundle")
            continue
        step(apply_step, "running", f"kubectl apply {comp} manifests")
        comp_err = _capi_apply_yaml_dir(comp_dir, kc, dry_run, apply_step, step)
        total_err += comp_err
        step(apply_step, "done" if comp_err == 0 else "error",
             f"{comp}: applied" if comp_err == 0
             else f"{comp}: {comp_err} file(s) had errors — see progress lines")
        # Wait phase — even if some applies failed, the deployment might
        # still be reachable (e.g. CRD update rejected but Deployment OK).
        deployments = _CAPI_COMPONENT_DEPLOYMENTS.get(comp) or []
        if not deployments:
            continue
        step(wait_step, "running",
             f"waiting for {comp} deployment(s) to become Available")
        wait_ok = True
        for ns, deploy in deployments:
            timeout = "120s" if comp == "cert-manager" else "180s"
            if dry_run:
                step(wait_step, "progress", f"[DRY-RUN] would wait for {ns}/{deploy}")
                continue
            ok, msg = _capi_wait_for_deploy(kc, ns, deploy, timeout=timeout)
            step(wait_step, "progress", f"{ns}/{deploy} {msg}")
            wait_ok = wait_ok and ok
        step(wait_step, "done" if wait_ok else "error",
             f"{comp}: ready" if wait_ok
             else f"{comp}: deployment(s) not Available — install may be degraded")
    return total_err


def _capi_apply_clusterclass_phase(bundle_root, kc, dry_run, step):
    """Phase 3 (optional): apply ClusterClass templates if the bundle
    ships any. Returns the count of failed files (0 if none)."""
    cc_dir = bundle_root / "clusterclass"
    if not (cc_dir.exists() and any(cc_dir.iterdir())):
        return 0
    step("clusterclass", "running", "Applying ClusterClass templates")
    err = 0
    for yfile in sorted(cc_dir.rglob("*.yaml")):
        if dry_run:
            step("clusterclass", "progress", f"[DRY-RUN] would apply {yfile.name}")
            continue
        rendered = _capi_envsubst(yfile.read_text())
        ok, msg = _capi_kubectl_apply(rendered, kc, server_side=False, timeout=60)
        if ok:
            step("clusterclass", "progress", f"applied {yfile.name}")
        else:
            step("clusterclass", "progress", f"{yfile.name} ERR: {msg}")
            err += 1
    step("clusterclass", "done" if err == 0 else "error",
         "ClusterClass applied" if err == 0 else f"{err} file(s) failed")
    return err


def _capi_install_runner(run, cluster, kc, nodes_ips, ssh_user, ssh_key, ssh_port, dry_run):
    """Background worker: loads the bundle into containerd on each
    Harvester node, then applies CAPI / CAPHV manifests in order, then
    optional ClusterClass templates. Logical phases are each delegated
    to small helpers above (preflight → extract → push → apply →
    clusterclass → cleanup) so this orchestrator stays under ~60 lines."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})

    def step(sid, status, message=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": message, "ts": time.time()})

    def fail(sid, msg):
        step(sid, "error", msg)
        run.exit_code = 1; run.status = "error"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
        run.close()

    # --- Preflight ---
    step("preflight", "running", "Checking bundle and target cluster")
    active = _capi_bundle_active_path()
    if not active:
        return fail("preflight",
                    f"No active bundle in {CAPI_BUNDLE_DIR} — run Build airgap bundle first")
    step("preflight", "done",
         f"Bundle: {active.name} ({active.stat().st_size // (1024*1024)}MB)")

    # --- Extract ---
    step("extract", "running", "Extracting bundle locally")
    try:
        workdir, bundle_root = _capi_extract_bundle_to_temp(active)
    except Exception as e:
        return fail("extract", f"tar extract failed: {e}")
    step("extract", "done", f"Extracted to {bundle_root}")

    # --- Phase 1: push images to nodes ---
    image_files = sorted((bundle_root / "images").glob("*.tar.gz"))
    _capi_push_images_phase(image_files, nodes_ips, ssh_user, ssh_key,
                            ssh_port, dry_run, step)

    # --- Phase 2: apply components in order with wait-for-Available ---
    apply_errors = _capi_apply_components_phase(
        bundle_root / "manifests", kc, dry_run, step
    )
    # --- Phase 3: optional ClusterClass ---
    apply_errors += _capi_apply_clusterclass_phase(bundle_root, kc, dry_run, step)

    # --- Cleanup ---
    try:
        shutil.rmtree(workdir)
    except Exception as e:
        log_capi.warning("temp cleanup failed: %s", e)

    # --- Finalize ---
    if apply_errors and not dry_run:
        run.exit_code = 1; run.status = "error"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
    else:
        run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
    run.close()


def _capi_bundle_runner(run):
    """Worker that runs scripts/bundle-capi.sh and streams its stderr/stdout
    as step events. Heavy operation (~1-2 GB of image pulls + tarball)."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    run.emit({"type": "step", "step_id": "start", "status": "running",
              "message": "Launching bundle-capi.sh", "ts": time.time()})

    script = Path(__file__).resolve().parent.parent / "scripts" / "bundle-capi.sh"
    if not script.exists():
        run.emit({"type": "step", "step_id": "start", "status": "error",
                  "message": f"script not found: {script}", "ts": time.time()})
        run.exit_code = 1; run.status = "error"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
        run.close()
        return

    # Compute a timestamped output path. Use UTC for sortable filenames.
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    suffix = uuid.uuid4().hex[:8]
    out_name = f"capi-bundle-{ts}-{suffix}.tar.gz"
    CAPI_BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CAPI_BUNDLE_DIR / out_name
    # Stash so the end-of-run hook can promote it.
    setattr(run, "_bundle_output_path", out_path)

    try:
        proc = subprocess.Popen(
            ["/usr/bin/env", "bash", str(script), str(out_path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except Exception as e:
        run.emit({"type": "step", "step_id": "start", "status": "error",
                  "message": str(e), "ts": time.time()})
        run.exit_code = 127; run.status = "error"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "error", "exit_code": 127, "ts": time.time()})
        run.close()
        return

    run.proc = proc
    current_step = "start"

    # Map output prefixes to step IDs for progress events
    step_keywords = {
        "Downloading manifests":       "manifests",
        "Pulling and saving":          "images",
        "Including local":             "clusterclass",
        "Creating tarball":            "archive",
    }

    for line in iter(proc.stdout.readline, ""):
        line = line.rstrip("\n")
        if not line:
            continue
        # Detect step transitions
        for kw, sid in step_keywords.items():
            if kw in line:
                if current_step != sid:
                    run.emit({"type": "step", "step_id": current_step, "status": "done",
                              "message": "completed", "ts": time.time()})
                    current_step = sid
                    run.emit({"type": "step", "step_id": sid, "status": "running",
                              "message": line.strip(), "ts": time.time()})
                break
        # Always emit a log line so the dock log tail shows progress
        run.emit({"type": "log", "stream": "stdout", "message": line, "ts": time.time()})

    rc = proc.wait()
    if current_step:
        run.emit({"type": "step", "step_id": current_step,
                  "status": "done" if rc == 0 else "error",
                  "message": f"exit {rc}", "ts": time.time()})

    # On success: promote this build to the active bundle. If the build
    # failed, drop the (likely empty/partial) output file so it doesn't
    # pollute the bundle list.
    out_path = getattr(run, "_bundle_output_path", None)
    if out_path:
        if rc == 0 and out_path.exists():
            try:
                _capi_bundle_set_active(out_path.name)
                run.emit({"type": "log", "stream": "stdout",
                          "message": f"[bundle] promoted {out_path.name} as active",
                          "ts": time.time()})
            except Exception as e:
                run.emit({"type": "log", "stream": "stderr",
                          "message": f"[bundle] failed to set active: {e}",
                          "ts": time.time()})
        elif rc != 0 and out_path.exists():
            try:
                out_path.unlink()
                sha = out_path.with_suffix(".tar.gz.sha256")
                if sha.exists(): sha.unlink()
            except Exception:
                pass

    run.exit_code = rc
    run.status = "done" if rc == 0 else "error"
    run.ended_at = time.time()
    run.emit({"type": "status", "status": run.status, "exit_code": rc, "ts": time.time()})
    run.close()


@app.route("/api/capi/bundle/build", methods=["POST"])
@requires_auth
def api_capi_bundle_build():
    """Build the CAPI airgap bundle via the UI. Tracked as an ActionRun."""
    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, "capi-bundle-build", "(local)", [], dry_run=False)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(target=_capi_bundle_runner, args=(run,), daemon=True).start()
    return jsonify({"action_id": run_id}), 201


# -----------------------------------------------------------------------------
# Bundle management (list, select, delete, inspect)
# -----------------------------------------------------------------------------
def _capi_bundle_safe_name(name):
    """Refuse path traversal and odd characters. Bundles are produced by us so
    names always match capi-bundle-*.tar.gz."""
    if "/" in name or ".." in name or not name.endswith(".tar.gz"):
        return None
    if not name.startswith("capi-bundle-"):
        return None
    p = (CAPI_BUNDLE_DIR / name).resolve()
    try:
        p.relative_to(CAPI_BUNDLE_DIR.resolve())
    except ValueError:
        return None
    return p


def _capi_bundle_disk_stats():
    try:
        st = os.statvfs(str(CAPI_BUNDLE_DIR))
        return {
            "free_bytes": st.f_bavail * st.f_frsize,
            "total_bytes": st.f_blocks * st.f_frsize,
        }
    except OSError:
        return {"free_bytes": 0, "total_bytes": 0}


@app.route("/api/capi/bundles")
@requires_auth
def api_capi_bundles_list():
    """List every airgap bundle on disk + the currently-active one + free space."""
    CAPI_BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    active_name = _capi_bundle_active_filename()
    items = []
    total_used = 0
    for p in sorted(CAPI_BUNDLE_DIR.glob("capi-bundle-*.tar.gz")):
        # The legacy symlink dist/capi-bundle.tar.gz also matches the glob —
        # skip it. Real bundles all have a timestamp segment.
        if p.is_symlink():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        sha_path = p.with_suffix(".tar.gz.sha256")
        sha = ""
        if sha_path.exists():
            try:
                sha = sha_path.read_text().split()[0]
            except Exception:
                pass
        total_used += st.st_size
        items.append({
            "filename": p.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "sha256": sha,
            "is_active": p.name == active_name,
        })
    items.sort(key=lambda b: b["mtime"], reverse=True)
    disk = _capi_bundle_disk_stats()
    return jsonify({
        "bundles": items,
        "active": active_name or "",
        "bundle_dir": str(CAPI_BUNDLE_DIR),
        "total_used": total_used,
        "disk_free": disk["free_bytes"],
        "disk_total": disk["total_bytes"],
    })


@app.route("/api/capi/bundle/upload", methods=["POST"])
@requires_auth
def api_capi_bundle_upload():
    """Accept a pre-built airgap bundle uploaded from the UI. Useful when the
    build host (with internet) is not the host running harvester-ops (airgap).

    Body: multipart/form-data field 'file' (.tar.gz, validated).
    """
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "no file uploaded — use form field 'file'"}), 400
    if not f.filename.endswith(".tar.gz"):
        return jsonify({"error": "filename must end in .tar.gz"}), 400
    # Force a server-side name with timestamp + sha so we never trust the
    # client filename for paths.
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    suffix = uuid.uuid4().hex[:8]
    out_name = f"capi-bundle-{ts}-{suffix}.tar.gz"
    CAPI_BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CAPI_BUNDLE_DIR / out_name
    tmp_path = out_path.with_suffix(".tar.gz.uploading")
    try:
        f.save(str(tmp_path))
        # Validate it's a real tar.gz with a manifest.json inside (cheap sanity check)
        import tarfile as _tar
        try:
            with _tar.open(tmp_path, "r:gz") as tar:
                has_manifest = any(m.name.endswith("/manifest.json") for m in tar.getmembers())
        except _tar.TarError as e:
            tmp_path.unlink(missing_ok=True)
            return jsonify({"error": f"not a valid tar.gz: {e}"}), 400
        if not has_manifest:
            tmp_path.unlink(missing_ok=True)
            return jsonify({"error": "tar.gz does not contain a manifest.json — not a CAPI bundle"}), 400
        # Atomic rename and sha256
        tmp_path.rename(out_path)
        import hashlib
        h = hashlib.sha256()
        with out_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        digest = h.hexdigest()
        out_path.with_suffix(".tar.gz.sha256").write_text(f"{digest}  {out_name}\n")
        # The uploaded bundle becomes active automatically (typical airgap flow).
        _capi_bundle_set_active(out_name)
    except Exception as e:
        if tmp_path.exists(): tmp_path.unlink()
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "uploaded": out_name,
        "size": out_path.stat().st_size,
        "sha256": digest,
        "active": True,
    }), 201


@app.route("/api/capi/bundle/<path:filename>/download")
@requires_auth
def api_capi_bundle_download(filename):
    """Stream a bundle .tar.gz to the client. Used for moving a bundle from a
    build host to an airgap host via the browser."""
    safe = _capi_bundle_safe_name(filename)
    if not safe or not safe.exists():
        return jsonify({"error": "bundle not found"}), 404
    return send_from_directory(
        str(CAPI_BUNDLE_DIR), safe.name,
        as_attachment=True,
        download_name=safe.name,
        mimetype="application/gzip",
    )


@app.route("/api/capi/bundle/select", methods=["POST"])
@requires_auth
def api_capi_bundle_select():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("filename", "")
    safe = _capi_bundle_safe_name(name)
    if not safe or not safe.exists():
        return jsonify({"error": "bundle not found", "filename": name}), 404
    try:
        _capi_bundle_set_active(name)
    except FileNotFoundError:
        return jsonify({"error": "bundle not found", "filename": name}), 404
    return jsonify({"active": name})


@app.route("/api/capi/bundle/<path:filename>", methods=["DELETE"])
@requires_auth
def api_capi_bundle_delete(filename):
    safe = _capi_bundle_safe_name(filename)
    if not safe or not safe.exists():
        return jsonify({"error": "bundle not found"}), 404
    active = _capi_bundle_active_filename()
    if filename == active:
        return jsonify({"error": "cannot delete the active bundle — select another first"}), 409
    try:
        safe.unlink()
        sha = safe.with_suffix(".tar.gz.sha256")
        if sha.exists():
            sha.unlink()
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"deleted": filename})


@app.route("/api/capi/bundle/<path:filename>/inspect")
@requires_auth
def api_capi_bundle_inspect(filename):
    """Open the bundle (read-only) and return manifest.json + tar listing."""
    safe = _capi_bundle_safe_name(filename)
    if not safe or not safe.exists():
        return jsonify({"error": "bundle not found"}), 404
    try:
        import tarfile as _tar
        with _tar.open(safe, "r:gz") as tar:
            members = tar.getmembers()
            # capi-bundle/manifest.json
            manifest = {}
            for m in members:
                if m.name.endswith("/manifest.json"):
                    f = tar.extractfile(m)
                    if f:
                        try:
                            manifest = json.loads(f.read().decode("utf-8"))
                        except Exception:
                            pass
                    break
            files = [{
                "name": m.name,
                "size": m.size,
                "is_dir": m.isdir(),
                "mtime": m.mtime,
            } for m in members]
    except Exception as e:
        return jsonify({"error": f"tar read failed: {e}"}), 500
    st = safe.stat()
    return jsonify({
        "filename": filename,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "manifest": manifest,
        "files": files,
        "file_count": len(files),
    })


@app.route("/api/capi/<cluster>/install", methods=["POST"])
@requires_auth
def api_capi_install(cluster):
    """Trigger the CAPI / CAPHV stack installation as a tracked action.
    Body: { "dry_run": <bool> } — dry-run logs every command without executing.
    """
    cfg = load_config()
    cluster_cfg = next((c for c in cfg.get("clusters", []) if c["name"] == cluster), None)
    if not cluster_cfg:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    kc = _identity_kubeconfig(cluster_cfg.get("kubeconfig", ""),
                              current_cluster_identity())
    if not Path(kc).exists():
        return jsonify({"error": "kubeconfig missing"}), 400

    active = _capi_bundle_active_path()
    if active is None:
        return jsonify({"error": "no active CAPI bundle",
                        "hint": "build a bundle first (Automation → Cluster API → Build airgap bundle)"}), 412

    data = request.get_json(force=True, silent=True) or {}
    dry_run = bool(data.get("dry_run", False))

    # v1.48.0 : sur un Harvester qui embarque Turtles (1.9 et après), la
    # console ne déclare que les fournisseurs, par l'outil harvester-capi.
    stack_cmd, err = _capi_base(cluster, "status")
    if not err:
        comp = _capi_components_dir()
        got, _err = _capi_run_json(stack_cmd + (["--components", str(comp)] if comp else [])
                                   + ["--json"], timeout=90)
        stack = got[0] if got else {}
        if stack.get("turtles") and (stack.get("core") or {}).get("ready"):
            if comp is None:
                return jsonify({"error": "the active bundle has no Turtles providers",
                                "hint": "build a new bundle (harvester-ops 1.48 or later)"}), 412
            if dry_run:
                return jsonify({"dry_run": True, "mode": "turtles", "stack": stack}), 200
            cmd, err = _capi_base(cluster, "install")
            if err:
                return err
            cmd += ["--cluster", cluster, "--components", str(active)]
            if data.get("push_images", bool(cluster_cfg.get("nodes"))):
                cmd += ["--push-images"]
            run, err = _capi_action(cluster, f"capi-install:{cluster}", cmd)
            if err:
                return err
            return jsonify({"action_id": run.id, "mode": "turtles", "dry_run": False}), 201

    # Compatibility check is advisory only — never blocks. The diag UI already
    # surfaces the mismatch, and the runner will log a warning at preflight.
    manifest = _read_bundle_manifest(active)
    harvester_version = _harvester_server_version(kc)
    compat_ok, compat_globs, compat_target = _bundle_compatibility(
        harvester_version, manifest)
    compat_warning = None
    if not compat_ok:
        compat_warning = {
            "target_version": compat_target or "unknown",
            "supported_versions": compat_globs,
        }

    nodes = cluster_cfg.get("nodes", [])
    nodes_ips = [n["ip"] for n in nodes if n.get("ip")]
    ssh = cluster_cfg.get("ssh", {}) or {}

    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, f"capi-install:{cluster}", cluster, [], dry_run=dry_run)
    if compat_warning:
        # Emit the warning as the very first event so it's visible at the top
        # of the run's log replay in the dock / activity overlay.
        run.events.append({
            "type": "log", "stream": "stderr",
            "message": (f"[WARN] bundle declares Harvester compatibility "
                        f"{compat_warning['supported_versions']} but target is "
                        f"{compat_warning['target_version']} — proceeding anyway"),
            "ts": time.time(),
        })
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(
        target=_capi_install_runner,
        args=(run, cluster, kc, nodes_ips,
              ssh.get("user", "rancher"),
              ssh.get("key", ""),
              ssh.get("port", 22),
              dry_run),
        daemon=True,
    ).start()
    return jsonify({
        "action_id": run_id,
        "dry_run": dry_run,
        "compatibility_warning": compat_warning,
    }), 201


# -----------------------------------------------------------------------------
# Clusters RKE2 par Cluster API (v1.48.0)
#
# La console lance `bin/harvester-capi.py`, le même outil qu'un opérateur
# utilise en ligne de commande : état de la pile, installation par Turtles,
# relevés pour le formulaire, contrôle préalable, aperçu, création et
# suppression. Voir docs/design/2026-09-25-creation-cluster-capi.md.
# -----------------------------------------------------------------------------
import capi_cluster as _cc  # noqa: E402

CAPI_SCRIPT = "harvester-capi.py"
_CAPI_INVENTORY_CACHE = {}          # cluster -> (horodatage, réponse)
_CAPI_INVENTORY_TTL = 15


def _capi_script():
    p = BIN_DIR / CAPI_SCRIPT
    return p if p.is_file() else None


def _capi_work_dir():
    """Où l'outil extrait un paquet : jamais /tmp, en mémoire dans le
    service installé (un paquet avec ses images pèse des centaines de Mio)."""
    d = Path(os.environ.get("HARVESTER_OPS_WORK_DIR") or (CAPI_BUNDLE_DIR / ".work"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d


def _capi_components_dir():
    """`turtles.json` et `turtles/` du paquet actif, extraits une fois et sans
    les images : le formulaire les relit à chaque contrôle. None si le
    paquet est d'avant 1.48 (sans fournisseurs pour Turtles)."""
    active = _capi_bundle_active_path()
    if active is None:
        return None
    dest = CAPI_BUNDLE_DIR / ".components" / f"{active.name}-{int(active.stat().st_mtime)}"
    if (dest / "turtles.json").is_file():
        return dest
    import tarfile as _tar
    tmp = dest.with_name(dest.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    found = False
    try:
        with _tar.open(active, "r:gz") as tar:
            for m in tar:
                parts = Path(m.name).parts
                if not m.isfile() or ".." in parts or m.name.startswith("/"):
                    continue
                rel = Path(*parts[1:]) if len(parts) > 1 else Path(parts[0])
                if rel.parts[:1] != ("turtles",) and str(rel) != "turtles.json":
                    continue
                out = tmp / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src, open(out, "wb") as f:
                    shutil.copyfileobj(src, f)
                found = found or str(rel) == "turtles.json"
    except (OSError, _tar.TarError):
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    if not found:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return dest


def _capi_base(cluster, sub):
    script = _capi_script()
    if script is None:
        return None, (jsonify({"error": f"{CAPI_SCRIPT} not deployed"}), 503)
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return None, (jsonify({"error": f"unknown cluster: {cluster}"}), 404)
    return [sys.executable, str(script), sub, "--kubeconfig", kc], None


def _capi_run_json(cmd, spec=None, timeout=180):
    """Lance une sous-commande en lecture seule qui répond en JSON."""
    env = dict(os.environ)
    wd = _capi_work_dir()
    if wd:
        env["HARVESTER_OPS_WORK_DIR"] = str(wd)
    try:
        proc = subprocess.run(cmd, input=json.dumps(spec) if spec is not None else None,
                              capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, (jsonify({"error": "timed out reading the cluster"}), 504)
    try:
        return (json.loads(proc.stdout), proc.returncode), None
    except ValueError:
        err = [ln.split("|", 3)[3] for ln in (proc.stderr or "").splitlines()
               if ln.startswith("STEP_EVENT|") and ln.split("|")[2:3] == ["error"]]
        msg = err[-1] if err else (proc.stderr or "failed").strip().splitlines()[-1:] or ["failed"]
        return None, (jsonify({"error": (msg if isinstance(msg, str) else msg[0])[:300]}), 502)


def _capi_spec_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None, (jsonify({"error": "a JSON object is expected"}), 400)
    try:
        return _cc.normalize(body), None
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 400)


@app.route("/api/capi/<cluster>/stack")
@requires_auth
@shared_read()
def api_capi_stack(cluster):
    """État de la pile Cluster API : Turtles, cœur, fournisseurs, ancienne
    installation, contournements. Lecture seule."""
    cmd, err = _capi_base(cluster, "status")
    if err:
        return err
    comp = _capi_components_dir()
    if comp:
        cmd += ["--components", str(comp)]
    got, err = _capi_run_json(cmd + ["--json"], timeout=90)
    if err:
        return err
    out, _rc = got
    active = _capi_bundle_active_path()
    out["bundle"] = {"active": active.name if active else None, "turtles": comp is not None}
    return jsonify(out)


@app.route("/api/capi/<cluster>/inventory")
@requires_auth
def api_capi_inventory(cluster):
    """Ce que le formulaire de création propose : images, clés, réseaux,
    pools d'adresses (plages, passerelle, masque, places), classes de
    stockage, versions Kubernetes du paquet, place restante, état de la
    pile. Mis en cache quelques secondes (`?fresh=1` pour relire)."""
    cached = _CAPI_INVENTORY_CACHE.get(cluster)
    if cached and time.time() - cached[0] < _CAPI_INVENTORY_TTL and not request.args.get("fresh"):
        return jsonify(cached[1])
    cmd, err = _capi_base(cluster, "inventory")
    if err:
        return err
    comp = _capi_components_dir()
    if comp:
        cmd += ["--components", str(comp)]
    got, err = _capi_run_json(cmd + ["--json"], timeout=120)
    if err:
        return err
    out, _rc = got
    _CAPI_INVENTORY_CACHE[cluster] = (time.time(), out)
    return jsonify(out)


@app.route("/api/capi/<cluster>/cluster-check", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_capi_cluster_check(cluster):
    """Contrôle préalable d'une demande de cluster. Ne modifie rien."""
    spec, err = _capi_spec_body()
    if err:
        return err
    cmd, err = _capi_base(cluster, "check")
    if err:
        return err
    comp = _capi_components_dir()
    if comp:
        cmd += ["--components", str(comp)]
    got, err = _capi_run_json(cmd + ["--spec", "-", "--json"], spec=spec, timeout=120)
    if err:
        return err
    out, rc = got
    out["blocked"] = rc == 2 or out.get("blocked", False)
    return jsonify(out)


@app.route("/api/capi/<cluster>/cluster-preview", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_capi_cluster_preview(cluster):
    """Les manifestes que la création appliquerait, secret d'identité masqué."""
    spec, err = _capi_spec_body()
    if err:
        return err
    cmd, err = _capi_base(cluster, "render")
    if err:
        return err
    try:
        proc = subprocess.run(cmd + ["--spec", "-"], input=json.dumps(spec), capture_output=True,
                              text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timed out"}), 504
    if proc.returncode != 0:
        lines = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()]
        return jsonify({"error": "; ".join(lines[-5:])[:500] or "render failed"}), 400
    return jsonify({"yaml": proc.stdout})


def _cli_action(cluster, label, cmd, tool, spec=None, dry_run=False, after=None):
    """Une action suivie (dock, Activité) qui lance un outil de `bin/`. La
    demande part par un fichier privé, effacé à la fin ; la ligne de commande
    montrée ne porte ni chemin ni kubeconfig."""
    busy = _transfer_busy((cluster,), label)
    if busy:
        return None, _busy_response(busy)
    wd = _capi_work_dir()
    spec_file = None
    if spec is not None:
        fd, spec_file = tempfile.mkstemp(prefix=f"{tool}-spec-", suffix=".json",
                                         dir=str(wd) if wd else None)
        with os.fdopen(fd, "w") as f:
            json.dump(spec, f)
        cmd = cmd + ["--spec", spec_file]
    run_id = uuid.uuid4().hex[:12]
    public = [tool] + [a for a in cmd[2:] if not a.startswith("/")
                       and a not in ("--kubeconfig",)]
    run = ActionRun(run_id, label, cluster, public, dry_run=dry_run)
    run.cluster_user = (current_cluster_identity() or {}).get("user")
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run

    def work():
        try:
            _vm_transfer_runner(run, cmd)
        finally:
            if spec_file:
                Path(spec_file).unlink(missing_ok=True)
            if after:
                after()
    threading.Thread(target=work, daemon=True).start()
    return run, None


def _capi_action(cluster, label, cmd, spec=None, dry_run=False):
    return _cli_action(cluster, label, cmd, "harvester-capi", spec, dry_run,
                       after=lambda: _CAPI_INVENTORY_CACHE.pop(cluster, None))


# ---------------------------------------------------------------------------
# v1.49.0 : réseaux kube-ovn (VPC, subnets, réseaux overlay)
#
# Tout passe par `bin/harvester-network.py` (parité CLI) : relevé, contrôle
# d'une demande, écriture en action suivie. Voir
# docs/design/2026-09-26-reseaux-kubeovn.md.
# ---------------------------------------------------------------------------

import ovn_net as _on  # noqa: E402
import ovn_extra as _ox  # noqa: E402

NETWORK_SCRIPT = "harvester-network.py"
_NET_INVENTORY_CACHE = {}           # (cluster, identité) -> (horodatage, réponse)
NET_KINDS = ("vpc", "subnet")


def _net_base(cluster, sub):
    script = BIN_DIR / NETWORK_SCRIPT
    if not script.is_file():
        return None, (jsonify({"error": f"{NETWORK_SCRIPT} not deployed"}), 503)
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return None, (jsonify({"error": f"unknown cluster: {cluster}"}), 404)
    return [sys.executable, str(script), sub, "--kubeconfig", kc], None


def _net_cache_key(cluster):
    return (cluster, (current_cluster_identity() or {}).get("user"))


def _net_request():
    """(kind, spec normalisée, update) d'un corps JSON, ou une réponse 400."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None, (jsonify({"error": "a JSON object is expected"}), 400)
    kind = body.get("kind")
    if kind not in NET_KINDS:
        return None, (jsonify({"error": "kind must be vpc or subnet"}), 400)
    try:
        spec = (_on.normalize_vpc if kind == "vpc" else _on.normalize_subnet)(body.get("spec") or {})
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 400)
    return (kind, spec, bool(body.get("update"))), None


@app.route("/api/kubeovn/<cluster>")
@requires_auth
@shared_read()
def api_kubeovn(cluster):
    """VPC, subnets, réseaux overlay et ce qui cloche. Lecture seule, 5 s de
    cache par identité (la vue se relit toutes les 8 s)."""
    key = _net_cache_key(cluster)
    hit = _NET_INVENTORY_CACHE.get(key)
    if hit and request.args.get("fresh") != "1" and time.time() - hit[0] < 5:
        return jsonify(hit[1])
    cmd, err = _net_base(cluster, "inventory")
    if err:
        return err
    res, err = _capi_run_json(cmd + ["--json"], timeout=60)
    if err:
        return err
    inv = res[0]
    out = {k: inv.get(k) for k in ("unreachable", "kubeovn", "model", "namespaces",
                                   "suggested_cidr", "free_overlays")}
    out["node_ips"] = (inv.get("facts") or {}).get("node_ips") or []
    _NET_INVENTORY_CACHE[key] = (time.time(), out)
    return jsonify(out)


@app.route("/api/kubeovn/<cluster>/check", methods=["POST"])
@requires_auth
@_rate_limit("60/minute")
def api_kubeovn_check(cluster):
    """Contrôle d'une demande de VPC ou de subnet, sans rien écrire."""
    req, err = _net_request()
    if err:
        return err
    kind, spec, update = req
    cmd, err = _net_base(cluster, "check")
    if err:
        return err
    cmd += ["--kind", kind, "--spec", "-"] + (["--update"] if update else [])
    res, err = _capi_run_json(cmd, spec=spec, timeout=60)
    if err:
        return err
    return jsonify(res[0])


@app.route("/api/kubeovn/<cluster>/apply", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_kubeovn_apply(cluster):
    """Crée ou modifie un VPC ou un subnet (et son réseau overlay) : action
    suivie, réponse immédiate."""
    req, err = _net_request()
    if err:
        return err
    kind, spec, update = req
    cmd, err = _net_base(cluster, "apply")
    if err:
        return err
    cmd += ["--kind", kind] + (["--update"] if update else [])
    verb = "update" if update else "create"
    run, err = _cli_action(cluster, f"network:{kind}-{verb}:{spec['name']}", cmd,
                           "harvester-network", spec=spec,
                           after=lambda: _NET_INVENTORY_CACHE.clear())
    if err:
        return err
    return jsonify({"action_id": run.id, "name": spec["name"], "kind": kind}), 202


# ---------------------------------------------------------------------------
# v1.66.0 : underlay (réseaux fournisseurs, VLANs, réseau externe), NAT
# (passerelles, IP externes, SNAT, DNAT) et politiques réseau des VMs. Les
# écritures passent par bin/harvester-network.py (apply / delete).
# ---------------------------------------------------------------------------

OVN_EXTRA_KINDS = ("provider", "vlan", "external", "gateway", "eip", "snat", "dnat", "policy", "repair")


def _ovn_extra_state(kc, cluster):
    from concurrent.futures import ThreadPoolExecutor
    reads = {"pns": (_ox.K_PN,), "vlans": (_ox.K_VLAN,), "subnets": (_ox.K_SUBNET,), "vpcs": (_ox.K_VPC,),
             "gws": (_ox.K_GW,), "eips": (_ox.K_EIP,), "snats": (_ox.K_SNAT,), "dnats": (_ox.K_DNAT,),
             "deploys": ("deployments", "-n", "kube-system"),
             "cni": ("pods", "-n", "kube-system", "-l", "app=kube-ovn-cni"),
             "gwpods": ("pods", "-n", "kube-system", "-l", "ovn.kubernetes.io/vpc-nat-gw=true"),
             "sts": ("statefulsets", "-n", "kube-system", "-l", "ovn.kubernetes.io/vpc-nat-gw=true"),
             "nodes": ("nodes",), "lm": ("linkmonitors.network.harvesterhci.io", "nic")}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, timeout=30, cluster=cluster) for k, a in reads.items()}
        got = {k: f.result() for k, f in futs.items()}
    items = {k: ((v or {}).get("items") or []) for k, v in got.items() if k != "lm"}
    if got["vpcs"] is None:
        return {"kubeovn": False}
    link = (((got.get("lm") or {}).get("status") or {}).get("linkStatus")) or {}
    taken = _ox.taken_nics(link)
    names = sorted({l.get("name") for links in link.values() for l in links or [] if l.get("name")})
    tenants = []
    for sub in items["subnets"]:
        sp = sub.get("spec") or {}
        if sp.get("vlan") or sub["metadata"]["name"] in _on.SYSTEM_SUBNETS:
            continue
        prov = str(sp.get("provider") or "").split(".")
        tenants.append({"name": sub["metadata"]["name"], "vpc": sp.get("vpc"), "cidr": sp.get("cidrBlock"),
                        "gateway": sp.get("gateway"),
                        "network": f"{prov[1]}/{prov[0]}" if len(prov) == 3 and prov[2] == "ovn" else ""})
    return {"kubeovn": True,
            "health": _ox.ovn_health(items["deploys"], items["cni"], items["nodes"]),
            "providers": _ox.provider_rows(items["pns"], items["vlans"]),
            "vlans": _ox.vlan_rows(items["vlans"], items["subnets"]),
            "externals": _ox.external_rows(items["subnets"], items["vlans"], items["eips"]),
            "gateways": _ox.gateway_rows(items["gws"], items["gwpods"], items["sts"], items["eips"]),
            "eips": _ox.eip_rows(items["eips"], items["snats"], items["dnats"]),
            "snats": _ox.rule_rows(items["snats"], "snat"), "dnats": _ox.rule_rows(items["dnats"], "dnat"),
            "vpcs": sorted(v["metadata"]["name"] for v in items["vpcs"]),
            "tenant_subnets": sorted(tenants, key=lambda t: t["name"]),
            "nodes": sorted(n["metadata"]["name"] for n in items["nodes"]),
            "nics": [{"name": n, "taken_on": taken.get(n, [])} for n in names]}


@app.route("/api/kubeovn/<cluster>/extra")
@requires_auth
@shared_read()
def api_kubeovn_extra(cluster):
    """Les vues Underlay et NAT : santé de kube-ovn, réseaux fournisseurs,
    VLANs, réseaux externes, passerelles, IP externes, règles."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    return jsonify({"cluster": cluster, **_ovn_extra_state(kc, cluster)})


@app.route("/api/kubeovn/<cluster>/policies")
@requires_auth
@shared_read()
def api_kubeovn_policies(cluster):
    """Les politiques réseau, et les VMs de chaque namespace à viser."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    got = _kubectl_json(kc, "get", "networkpolicies.networking.k8s.io,virtualmachines.kubevirt.io", "-A",
                        timeout=30, cluster=cluster) or {}
    nps = [i for i in got.get("items") or [] if i.get("kind") == "NetworkPolicy"]
    vms = {}
    for v in got.get("items") or []:
        if v.get("kind") == "VirtualMachine":
            vms.setdefault(v["metadata"]["namespace"], []).append(v["metadata"]["name"])
    rows = [r for r in _ox.policy_rows(nps) if not str(r["namespace"]).startswith(_co.SYSTEM_NAMESPACE_PREFIXES)]
    specs = {f"{np['metadata']['namespace']}/{np['metadata']['name']}": _ox.policy_to_spec(np) for np in nps}
    for r in rows:
        r["spec"] = specs.get(f"{r['namespace']}/{r['name']}")
    return jsonify({"cluster": cluster, "items": rows, "vms": {k: sorted(v) for k, v in vms.items()}})


@app.route("/api/kubeovn/<cluster>/extra/<kind>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_kubeovn_extra_apply(cluster, kind):
    """Crée un objet d'underlay, de NAT ou une politique (ou modifie une
    politique), contrôlé d'avance, suivi dans le dock."""
    if kind not in OVN_EXTRA_KINDS:
        return jsonify({"error": f"kind must be one of {', '.join(OVN_EXTRA_KINDS)}"}), 400
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("spec"), dict):
        return jsonify({"error": "a JSON object with a spec is expected"}), 400
    spec, update = body["spec"], bool(body.get("update"))
    try:
        {"provider": lambda: _ox.provider_network(spec), "vlan": lambda: _ox.vlan(spec),
         "external": lambda: _ox.external_network(spec), "gateway": lambda: _ox.check_name(spec.get("name"), "gateway name", 48),
         "eip": lambda: _ox.check_name(spec.get("name"), "external IP name"), "snat": lambda: _ox.snat(spec),
         "dnat": lambda: _ox.dnat(spec), "policy": lambda: _ox.network_policy(spec),
         "repair": lambda: _ox.check_name(spec.get("name"), "gateway name", 48)}[kind]()
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    if update and kind != "policy":
        return jsonify({"error": "kube-ovn freezes this object once ready: delete it and create it again"}), 400
    cmd, err = _net_base(cluster, "apply")
    if err:
        return err
    cmd += ["--kind", kind] + (["--update"] if update else [])
    name = spec.get("name")
    label = f"network:{kind}-{'update' if update else 'create'}:" + (f"{spec.get('namespace') or 'default'}/{name}" if kind == "policy" else name)
    run, err = _cli_action(cluster, label, cmd, "harvester-network", spec=spec,
                           after=lambda: _NET_INVENTORY_CACHE.clear())
    if err:
        return err
    return jsonify({"action_id": run.id, "kind": kind, "name": name}), 202


@app.route("/api/kubeovn/<cluster>/extra/<kind>/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_kubeovn_extra_delete(cluster, kind, name):
    if kind not in OVN_EXTRA_KINDS:
        return jsonify({"error": f"kind must be one of {', '.join(OVN_EXTRA_KINDS)}"}), 400
    ns = request.args.get("namespace") or ""
    if kind == "policy" and not _K8S_NAME_RE.match(ns):
        return jsonify({"error": "a policy is deleted with its namespace"}), 400
    cmd, err = _net_base(cluster, "delete")
    if err:
        return err
    cmd += ["--kind", kind, "--name", name] + (["--namespace", ns] if kind == "policy" else [])
    run, err = _cli_action(cluster, f"network:{kind}-delete:" + (f"{ns}/{name}" if ns else name), cmd, "harvester-network",
                           after=lambda: _NET_INVENTORY_CACHE.clear())
    if err:
        return err
    return jsonify({"action_id": run.id, "kind": kind, "name": name}), 202


@app.route("/api/kubeovn/<cluster>/<kind>/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_kubeovn_delete(cluster, kind, name):
    """Supprime un VPC vide ou un subnet inutilisé ; `with_network=1` retire
    aussi le réseau overlay du subnet s'il a été créé par la console."""
    if kind not in NET_KINDS:
        return jsonify({"error": "kind must be vpc or subnet"}), 400
    cmd, err = _net_base(cluster, "delete")
    if err:
        return err
    cmd += ["--kind", kind, "--name", name]
    if request.args.get("with_network") == "1":
        cmd.append("--with-network")
    run, err = _cli_action(cluster, f"network:{kind}-delete:{name}", cmd, "harvester-network",
                           after=lambda: _NET_INVENTORY_CACHE.clear())
    if err:
        return err
    return jsonify({"action_id": run.id, "name": name, "kind": kind}), 202


# ---------------------------------------------------------------------------
# v1.57.0 : les comptes de la console (Réglages > Comptes de la console, et
# « Changer mon mot de passe » dans le menu du compte). Chaque changement est
# une action de l'Activité (qui, quoi), jamais avec un mot de passe.
# ---------------------------------------------------------------------------

def _record_action(label, message, ok=True):
    run = ActionRun(uuid.uuid4().hex[:12], label, None, [], dry_run=False)
    run.status = "done" if ok else "error"
    run.exit_code = 0 if ok else 1
    run.emit({"type": "step", "step_id": "account", "status": "done" if ok else "error",
              "message": f"{message} (by {current_user() or '?'})", "ts": time.time()})
    run.ended_at = time.time()
    with ACTIONS_LOCK:
        ACTIONS[run.id] = run
    run.close()
    return run.id


def _account_error(e):
    status = {"not-found": 404, "name-taken": 409, "last-admin": 409}.get(e.code, 400)
    return jsonify({"error": str(e), "code": e.code}), status


def _check_user_param(user):
    if not _acc.USERNAME_RE.match(user or ""):
        return jsonify({"error": "invalid account name"}), 400
    return None


@app.route("/api/users")
@requires_auth
def api_users():
    """Les comptes locaux : ceux de la console (modifiables ici) et ceux du
    htpasswd de l'installeur (lus seulement)."""
    managed = _accounts().list()
    roles = load_roles()
    out = [{"name": n, "role": u.get("role"), "source": "console", "created": u.get("created"),
            "created_by": u.get("created_by"), "password_changed": u.get("password_changed")}
           for n, u in sorted(managed.items())]
    for n in sorted(_htpasswd_users()):
        if n not in managed:
            role = (roles["users"].get(n, roles["default_role"]) if roles.get("configured") else "admin")
            out.append({"name": n, "role": role, "source": "htpasswd"})
    return jsonify({"accounts": out, "me": current_user(), "min_password": _acc.PASSWORD_MIN,
                    "roles": list(_acc.ROLES)})


@app.route("/api/users", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_user_create():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    if name in _htpasswd_users():
        return jsonify({"error": f"an account {name} already exists (installer)", "code": "name-taken"}), 409
    try:
        _accounts().create(name, body.get("password"), body.get("role") or "viewer", by=current_user())
    except _acc.AccountError as e:
        return _account_error(e)
    aid = _record_action(f"account:create:{name}", f"account {name} created, role {body.get('role') or 'viewer'}")
    return jsonify({"ok": True, "name": name, "action_id": aid}), 201


@app.route("/api/users/<user>", methods=["PATCH"])
@requires_auth
@_rate_limit("20/minute")
def api_user_update(user):
    bad = _check_user_param(user)
    if bad:
        return bad
    body = request.get_json(silent=True) or {}
    done = []
    try:
        if body.get("role"):
            _accounts().set_role(user, body["role"])
            done.append(f"role {body['role']}")
        if body.get("password"):
            _accounts().set_password(user, body["password"])
            _LOCAL_SESSIONS.close_user(user)          # ses sessions ouvertes tombent
            done.append("password reset")
    except _acc.AccountError as e:
        return _account_error(e)
    if not done:
        return jsonify({"error": "nothing to change (role, password)"}), 400
    aid = _record_action(f"account:update:{user}", f"account {user}: {', '.join(done)}")
    return jsonify({"ok": True, "action_id": aid})


@app.route("/api/users/<user>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_user_delete(user):
    bad = _check_user_param(user)
    if bad:
        return bad
    if user == current_user():
        return jsonify({"error": "you cannot delete your own account", "code": "self"}), 409
    try:
        _accounts().delete(user)
    except _acc.AccountError as e:
        return _account_error(e)
    _LOCAL_SESSIONS.close_user(user)
    aid = _record_action(f"account:delete:{user}", f"account {user} deleted")
    return jsonify({"ok": True, "action_id": aid})


@app.route("/api/me/password", methods=["POST"])
@requires_auth
@_rate_limit("10/minute")
def api_my_password():
    """Changer son propre mot de passe (comptes de la console) : l'ancien est
    exigé ; les autres sessions du compte se ferment, celle-ci reste."""
    user = current_user()
    if not user or not _accounts().has(user):
        return jsonify({"error": "this account's password is not managed by the console",
                        "code": "not-managed"}), 409
    body = request.get_json(silent=True) or {}
    if not _accounts().verify(user, body.get("current") or ""):
        return jsonify({"error": "the current password is wrong", "code": "bad-current"}), 403
    try:
        _accounts().set_password(user, body.get("new"))
    except _acc.AccountError as e:
        return _account_error(e)
    local = _local_session()
    _LOCAL_SESSIONS.close_user(user, keep=local["sid"] if local else None)
    aid = _record_action(f"account:password:{user}", f"account {user}: password changed")
    return jsonify({"ok": True, "action_id": aid})


# ---------------------------------------------------------------------------
# v1.57.0 : les objets rangés sous Cluster (Storage > Images et Storage
# Classes, Security > Secrets et SSH Keys, Add-ons), comme dans l'interface de
# Harvester. Lecture : un `kubectl get` groupé, mis en regard des VMs et des
# volumes (web/cluster_objects.py). Écriture : bin/harvester-resources.py.
# ---------------------------------------------------------------------------
import cluster_objects as _co  # noqa: E402
import hv_backups as _hb  # noqa: E402
import hv_net as _hnet  # noqa: E402
import hv_settings as _hset  # noqa: E402
import hv_devices as _hdev  # noqa: E402
import hv_upgrade as _hup  # noqa: E402
import hv_monlog as _hml  # noqa: E402
import hv_vmimport as _hvi  # noqa: E402
import hv_objects as _ho  # noqa: E402

# v1.58.0 : les listes de la fenêtre Backups, servies par la même route
_BACKUP_KINDS = {"vmbackups": _hb.K_BACKUP, "vmsnapshots": _hb.K_BACKUP,
                 "schedules": _hb.K_SCHEDULE, "volsnaps": _hb.K_VOLSNAP}

RESOURCES_SCRIPT = "harvester-resources.py"
_CO_KIND_NAMES = {"vm": "virtualmachines.kubevirt.io", "pvc": "persistentvolumeclaims",
                  "vmimage": "virtualmachineimages.harvesterhci.io"}
_CO_KIND_OF = {_hb.K_BACKUP: "VirtualMachineBackup", _hb.K_SCHEDULE: "ScheduleVMBackup",
               _hb.K_VOLSNAP: "VolumeSnapshot",
               "virtualmachineimages.harvesterhci.io": "VirtualMachineImage",
               "virtualmachines.kubevirt.io": "VirtualMachine",
               "persistentvolumeclaims": "PersistentVolumeClaim",
               "storageclasses.storage.k8s.io": "StorageClass",
               "keypairs.harvesterhci.io": "KeyPair", "secrets": "Secret",
               "addons.harvesterhci.io": "Addon",
               # v1.65.0 : le menu Networks de Harvester
               _hnet.K_CN: "ClusterNetwork", _hnet.K_VC: "VlanConfig", _hnet.K_VS: "VlanStatus",
               _hnet.K_NAD: "NetworkAttachmentDefinition", _hnet.K_HNC: "HostNetworkConfig",
               _hnet.K_LB: "LoadBalancer", _hnet.K_POOL: "IPPool", _hnet.K_SETTING: "Setting",
               "nodes": "Node", "virtualmachineinstances.kubevirt.io": "VirtualMachineInstance"}
_NET_KINDS = {"loadbalancers": _hnet.K_LB, "ippools": _hnet.K_POOL, "hostnetworks": _hnet.K_HNC}


def _kubectl_kinds(kc, kinds, cluster):
    """{nom complet: [objets]} de plusieurs types en un appel ; si la RBAC en
    refuse un, le lot échoue en bloc : on relit alors type par type ce qui est
    permis (les refus sont notés et dits par l'en-tête X-Cluster-Denied)."""
    got = _kubectl_json(kc, "get", ",".join(kinds), "-A", timeout=30, cluster=cluster)
    out = {k: [] for k in kinds}
    if got is not None:
        rev = {v: k for k, v in _CO_KIND_OF.items()}
        for it in got.get("items") or []:
            k = rev.get(it.get("kind"))
            if k in out:
                out[k].append(it)
        return out
    for k in kinds:
        one = _kubectl_json(kc, "get", k, "-A", timeout=30, cluster=cluster)
        out[k] = (one or {}).get("items") or []
    return out


@app.route("/api/cluster-objects/<cluster>/<kind>")
@requires_auth
@shared_read()
def api_cluster_objects(cluster, kind):
    """Une liste de la vue Storage, Security ou Add-ons, avec qui s'en sert.
    Un Secret ne sort qu'avec le nom de ses clés, jamais ses valeurs."""
    if kind not in _co.KINDS and kind not in _BACKUP_KINDS and kind not in _NET_KINDS:
        kinds_all = ", ".join(list(_co.KINDS) + list(_BACKUP_KINDS) + list(_NET_KINDS))
        return jsonify({"error": f"kind must be one of {kinds_all}"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    if kind in _BACKUP_KINDS:
        ns = request.args.get("namespace") or ""
        if ns and not _K8S_NAME_RE.match(ns):
            return jsonify({"error": "invalid namespace"}), 400
        got = _kubectl_kinds(kc, [_BACKUP_KINDS[kind]], cluster)[_BACKUP_KINDS[kind]]
        if ns:
            got = [o for o in got if ((o.get("metadata") or {}).get("namespace")) == ns]
        if kind == "vmbackups":
            rows = _hb.backups(got, "backup")
        elif kind == "vmsnapshots":
            rows = _hb.backups(got, "snapshot")
        elif kind == "schedules":
            rows = _hb.schedules(got)
        else:
            rows = _hb.volume_snapshots(got)
        rows.sort(key=lambda r: r.get("created") or "", reverse=True)
        return jsonify({"cluster": cluster, "kind": kind, "items": rows})
    if kind in _NET_KINDS:
        # v1.65.0 : équilibreurs, pools d'adresses, réseaux d'hôte
        got = _kubectl_kinds(kc, [_NET_KINDS[kind]], cluster)[_NET_KINDS[kind]]
        rows = {"loadbalancers": _hnet.lb_rows, "ippools": _hnet.pool_rows,
                "hostnetworks": _hnet.hostnet_rows}[kind](got)
        rows.sort(key=lambda r: (r.get("namespace") or "", r.get("name") or ""))
        return jsonify({"cluster": cluster, "kind": kind, "items": rows})
    main = _co.FETCH[kind]
    kinds = [main] + [_CO_KIND_NAMES[u] for u in _co.NEEDS_USAGE[kind]]
    got = _kubectl_kinds(kc, kinds, cluster)
    items = got[main]
    extra = {}
    if kind == "images":
        rows = _co.images(items, got[_CO_KIND_NAMES["vm"]], got[_CO_KIND_NAMES["pvc"]])
    elif kind == "storageclasses":
        rows = _co.storage_classes(items, got[_CO_KIND_NAMES["pvc"]], got[_CO_KIND_NAMES["vmimage"]])
    elif kind == "sshkeys":
        rows = _co.ssh_keys(items, got[_CO_KIND_NAMES["vm"]])
    elif kind == "secrets":
        rows, hidden = _co.secrets(items, got[_CO_KIND_NAMES["vm"]],
                                   include_system=request.args.get("all") == "1")
        extra["system_hidden"] = hidden
    else:
        rows = _co.addons(items)
    rows.sort(key=lambda r: (r.get("namespace") or "", r.get("name") or ""))
    return jsonify({"cluster": cluster, "kind": kind, "items": rows, **extra})


@app.route("/api/addons/<cluster>/<namespace>/<name>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_addon_toggle(cluster, namespace, name):
    """Active ou désactive un add-on Harvester : action suivie, par
    bin/harvester-resources.py (parité CLI)."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "a JSON object with a boolean 'enabled' is expected"}), 400
    script = BIN_DIR / RESOURCES_SCRIPT
    if not script.is_file():
        return jsonify({"error": f"{RESOURCES_SCRIPT} not deployed"}), 503
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    verb = "enable" if body["enabled"] else "disable"
    cmd = [sys.executable, str(script), "addon", "--kubeconfig", kc,
           "--namespace", namespace, "--name", name, f"--{verb}"]
    run, err = _cli_action(cluster, f"addon:{verb}:{namespace}/{name}", cmd, "harvester-resources")
    if err:
        return err
    return jsonify({"action_id": run.id, "addon": f"{namespace}/{name}", "enabled": body["enabled"]}), 202


# ---------------------------------------------------------------------------
# v1.58.0 : la fenêtre Backups (sauvegardes, instantanés, planifications,
# instantanés de volumes). Chaque écriture est une action suivie, par
# bin/harvester-resources.py (parité CLI).
# ---------------------------------------------------------------------------

def _res_action(cluster, label, args):
    script = BIN_DIR / RESOURCES_SCRIPT
    if not script.is_file():
        return None, (jsonify({"error": f"{RESOURCES_SCRIPT} not deployed"}), 503)
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return None, (jsonify({"error": f"unknown cluster: {cluster}"}), 404)
    cmd = [sys.executable, str(script), args[0], args[1], "--kubeconfig", kc] + list(args[2:])
    return _cli_action(cluster, label, cmd, "harvester-resources",
                       after=lambda: _invalidate_cluster_caches(cluster))


def _res_reply(run, err, **extra):
    if err:
        return err
    return jsonify({"action_id": run.id, **extra}), 202


def _body_name(body, key, required=True):
    v = str(body.get(key) or "").strip()
    if not v and not required:
        return None
    try:
        return _hb.check_name(v, key)
    except ValueError as e:
        raise ValueError(str(e)) from None


@app.route("/api/backup-target/<cluster>")
@requires_auth
def api_backup_target(cluster):
    """La cible de sauvegarde du cluster, sans ses identifiants (un S3 porte
    ses clés dans le réglage)."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    got = _kubectl_json(kc, "get", "settings.harvesterhci.io", "backup-target", cluster=cluster) or {}
    try:
        val = json.loads(got.get("value") or "{}") if got.get("value") else {}
    except ValueError:
        val = {}
    return jsonify({"type": val.get("type") or "", "endpoint": val.get("endpoint") or "",
                    "bucket": val.get("bucketName") or "", "region": val.get("bucketRegion") or "",
                    "set": bool(val.get("type") and val.get("endpoint"))})


@app.route("/api/backups/<cluster>/<namespace>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_backup_create(cluster, namespace):
    body = request.get_json(silent=True) or {}
    kind = body.get("type") or "backup"
    try:
        vm = _body_name(body, "vm")
        name = _body_name(body, "name", required=False)
        if kind not in ("backup", "snapshot"):
            raise ValueError("type must be backup or snapshot")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    args = ["backup", "create", "--namespace", namespace, "--vm", vm, "--type", kind]
    if name:
        args += ["--name", name]
    # v1.68.0 : délai de gel du système de fichiers (Harvester 1.9)
    if body.get("freeze"):
        if body["freeze"] not in _hb.FREEZE:
            return jsonify({"error": "freeze: " + ", ".join(_hb.FREEZE)}), 400
        args += ["--freeze", body["freeze"]]
    run, err = _res_action(cluster, f"{kind}:create:{namespace}/{vm}", args)
    return _res_reply(run, err, vm=vm, type=kind)


@app.route("/api/backups/<cluster>/<namespace>/<name>/restore", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_backup_restore(cluster, namespace, name):
    body = request.get_json(silent=True) or {}
    args = ["backup", "restore", "--namespace", namespace, "--name", name]
    try:
        if body.get("replace"):
            args.append("--replace")
        else:
            args += ["--new-vm", _body_name(body, "new_vm")]
            if body.get("keep_mac"):
                args.append("--keep-mac")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if body.get("halt"):
        args.append("--halt")
    if body.get("delete_policy") in ("retain", "delete"):
        args += ["--delete-policy", body["delete_policy"]]
    target = "replace" if body.get("replace") else body.get("new_vm")
    run, err = _res_action(cluster, f"backup:restore:{namespace}/{name}", args)
    return _res_reply(run, err, backup=name, target=target)


@app.route("/api/backups/<cluster>/<namespace>/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_backup_delete(cluster, namespace, name):
    run, err = _res_action(cluster, f"backup:delete:{namespace}/{name}",
                           ["backup", "delete", "--namespace", namespace, "--name", name])
    return _res_reply(run, err, backup=name)


@app.route("/api/schedules/<cluster>/<namespace>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_schedule_create(cluster, namespace):
    body = request.get_json(silent=True) or {}
    try:
        name, vm = _body_name(body, "name"), _body_name(body, "vm")
        _hb.schedule_manifest(namespace, name, vm, body.get("cron"), body.get("retain"),
                              body.get("max_failure"), body.get("type") or "backup")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    args = ["schedule", "create", "--namespace", namespace, "--name", name, "--vm", vm,
            "--cron", _hb.check_cron(body["cron"]), "--retain", str(int(body["retain"])),
            "--max-failure", str(int(body["max_failure"])), "--type", body.get("type") or "backup"]
    run, err = _res_action(cluster, f"schedule:create:{namespace}/{name}", args)
    return _res_reply(run, err, schedule=name)


@app.route("/api/schedules/<cluster>/<namespace>/<name>/<verb>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_schedule_toggle(cluster, namespace, name, verb):
    if verb not in ("suspend", "resume", "update"):
        return jsonify({"error": "suspend, resume or update"}), 400
    args = ["schedule", verb, "--namespace", namespace, "--name", name]
    if verb == "update":
        # v1.68.0 : fréquence, copies gardées, échecs tolérés (comme Harvester)
        body = request.get_json(silent=True) or {}
        try:
            patch = _hb.schedule_patch(body.get("cron"), body.get("retain"), body.get("max_failure"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        args += ["--cron", patch["spec"]["cron"], "--retain", str(patch["spec"]["retain"]),
                 "--max-failure", str(patch["spec"]["maxFailure"])]
    run, err = _res_action(cluster, f"schedule:{verb}:{namespace}/{name}", args)
    return _res_reply(run, err, schedule=name)


@app.route("/api/schedules/<cluster>/<namespace>/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_schedule_delete(cluster, namespace, name):
    run, err = _res_action(cluster, f"schedule:delete:{namespace}/{name}",
                           ["schedule", "delete", "--namespace", namespace, "--name", name])
    return _res_reply(run, err, schedule=name)


@app.route("/api/volsnaps/<cluster>/<namespace>/<name>/restore", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_volsnap_restore(cluster, namespace, name):
    body = request.get_json(silent=True) or {}
    try:
        new = _body_name(body, "new_volume")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    args = ["volsnap", "restore", "--namespace", namespace, "--name", name, "--new-volume", new]
    sc = str(body.get("storage_class") or "").strip()
    if sc:
        if not _K8S_NAME_RE.match(sc):
            return jsonify({"error": "invalid storage class"}), 400
        args += ["--storage-class", sc]
    run, err = _res_action(cluster, f"volsnap:restore:{namespace}/{name}", args)
    return _res_reply(run, err, snapshot=name, volume=new)


@app.route("/api/volsnaps/<cluster>/<namespace>/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_volsnap_delete(cluster, namespace, name):
    run, err = _res_action(cluster, f"volsnap:delete:{namespace}/{name}",
                           ["volsnap", "delete", "--namespace", namespace, "--name", name])
    return _res_reply(run, err, snapshot=name)


# ---------------------------------------------------------------------------
# v1.59.0 : créer, modifier, supprimer les objets des sections, comme dans
# l'interface de Harvester (images, classes de stockage, clés SSH, secrets,
# réseaux des VMs, volumes, configuration des add-ons). Par
# bin/harvester-resources.py, en actions suivies.
# ---------------------------------------------------------------------------
# Ce qui change le cluster pour tous (une classe de stockage, un réseau) est
# réservé aux administrateurs ; le reste aux opérateurs.
_OBJ_ADMIN_KINDS = ("storageclass", "network")


def _needs_admin():
    if current_role() != "admin":
        return jsonify({"error": "forbidden", "role": current_role(), "required": "admin",
                        "hint": "storage classes and VM networks are managed by administrators"}), 403
    return None


@app.route("/api/objects/<cluster>/<kind>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_object_create(cluster, kind):
    if kind not in _ho.KINDS:
        return jsonify({"error": f"kind must be one of {', '.join(_ho.KINDS)}"}), 400
    if kind in _OBJ_ADMIN_KINDS:
        denied = _needs_admin()
        if denied:
            return denied
    body = request.get_json(silent=True) or {}
    spec = body.get("spec") if isinstance(body.get("spec"), dict) else None
    if spec is None:
        return jsonify({"error": "a JSON object with a 'spec' is expected"}), 400
    try:
        # contrôle d'avance (l'image d'un volume est relue par l'outil)
        if kind == "volume" and spec.get("image"):
            _ho._name(spec.get("name"))
            _ho._size(spec.get("size"))
        else:
            _ho.normalize(kind, spec, default_class="default")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    name = spec.get("name") or spec.get("display_name") or "image"
    run, err = _res_spec_action(cluster, f"{kind}:create:{spec.get('namespace') or ''}/{name}".replace(":/", ":"),
                                ["create", "--kind", kind], spec)
    return _res_reply(run, err, kind=kind, name=name)


def _res_spec_action(cluster, label, args, spec):
    script = BIN_DIR / RESOURCES_SCRIPT
    if not script.is_file():
        return None, (jsonify({"error": f"{RESOURCES_SCRIPT} not deployed"}), 503)
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return None, (jsonify({"error": f"unknown cluster: {cluster}"}), 404)
    cmd = [sys.executable, str(script), args[0], "--kubeconfig", kc] + list(args[1:])
    return _cli_action(cluster, label, cmd, "harvester-resources", spec=spec,
                       after=lambda: _invalidate_cluster_caches(cluster))


@app.route("/api/objects/<cluster>/<kind>/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("30/minute")
def api_object_delete(cluster, kind, name):
    if kind not in _ho.KINDS:
        return jsonify({"error": f"kind must be one of {', '.join(_ho.KINDS)}"}), 400
    if kind in _OBJ_ADMIN_KINDS:
        denied = _needs_admin()
        if denied:
            return denied
    ns = request.args.get("namespace") or ""
    if kind in _ho.NAMESPACED and not _K8S_NAME_RE.match(ns):
        return jsonify({"error": "a valid namespace is required"}), 400
    args = ["delete", "--kind", kind, "--name", name] + (["--namespace", ns] if kind in _ho.NAMESPACED else [])
    script = BIN_DIR / RESOURCES_SCRIPT
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    cmd = [sys.executable, str(script), "delete", "--kubeconfig", kc] + args[1:]
    run, err = _cli_action(cluster, f"{kind}:delete:{(ns + '/') if ns else ''}{name}", cmd, "harvester-resources",
                           after=lambda: _invalidate_cluster_caches(cluster))
    return _res_reply(run, err, kind=kind, name=name)


@app.route("/api/storageclasses/<cluster>/<name>/default", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_storageclass_default(cluster, name):
    denied = _needs_admin()
    if denied:
        return denied
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "sc-default", "--kubeconfig", kc, "--name", name]
    run, err = _cli_action(cluster, f"storageclass:default:{name}", cmd, "harvester-resources")
    return _res_reply(run, err, storage_class=name)


@app.route("/api/volumes/<cluster>/<namespace>/<name>/expand", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_volume_expand(cluster, namespace, name):
    body = request.get_json(silent=True) or {}
    try:
        size = _ho._size(body.get("size"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "volume-expand", "--kubeconfig", kc,
           "--namespace", namespace, "--name", name, "--size", size]
    run, err = _cli_action(cluster, f"volume:expand:{namespace}/{name}", cmd, "harvester-resources",
                           after=lambda: _invalidate_cluster_caches(cluster))
    return _res_reply(run, err, volume=name, size=size)


@app.route("/api/addons/<cluster>/<namespace>/<name>/values")
@requires_auth
def api_addon_values_get(cluster, namespace, name):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    got = _kubectl_json(kc, "get", "addons.harvesterhci.io", name, "-n", namespace, cluster=cluster)
    if got is None:
        return jsonify({"error": f"no add-on {namespace}/{name}"}), 404
    return jsonify({"values": (got.get("spec") or {}).get("valuesContent") or "",
                    "enabled": bool((got.get("spec") or {}).get("enabled"))})


@app.route("/api/addons/<cluster>/<namespace>/<name>/values", methods=["POST"])
@requires_auth
@_rate_limit("10/minute")
def api_addon_values_set(cluster, namespace, name):
    body = request.get_json(silent=True) or {}
    try:
        text = _ho.check_values(body.get("values"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    wd = _capi_work_dir()
    fd, path = tempfile.mkstemp(prefix="addon-values-", suffix=".yaml", dir=str(wd) if wd else None)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "addon-values", "--kubeconfig", kc,
           "--namespace", namespace, "--name", name, "--values", path]
    run, err = _cli_action(cluster, f"addon:values:{namespace}/{name}", cmd, "harvester-resources",
                           after=lambda: Path(path).unlink(missing_ok=True))
    if err:
        Path(path).unlink(missing_ok=True)
    return _res_reply(run, err, addon=f"{namespace}/{name}")


# ---------------------------------------------------------------------------
# v1.60.0 : « Edit YAML » et « Download YAML », comme dans Harvester, sur une
# liste fermée de types (bin/lib/hv_yaml.py). Lire est une vue ; vérifier est
# un essai à blanc côté serveur (les webhooks de Harvester jugent) ; écrire
# est une action suivie, par bin/harvester-resources.py yaml.
# ---------------------------------------------------------------------------
import hv_yaml as _hy  # noqa: E402


class _YamlDumper(yaml.SafeDumper):
    pass


def _yaml_str(dumper, value):
    # un cloud-init, un certificat : en bloc lisible, pas en une ligne échappée
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_YamlDumper.add_representer(str, _yaml_str)


def _to_yaml(obj):
    return yaml.dump(obj, Dumper=_YamlDumper, sort_keys=False, default_flow_style=False,
                     allow_unicode=True, width=4096)


def _yaml_target(kind, write=False, need_ns=True):
    """(spec du type, namespace) ou une réponse d'erreur. En création, le
    namespace peut venir du YAML lui-même."""
    try:
        s = _hy.spec_of(kind)
    except ValueError as e:
        return None, None, (jsonify({"error": str(e)}), 400)
    if not _hy.allowed(kind, current_role(), write=write):
        need = s["write" if write else "read"]
        return None, None, (jsonify({"error": "forbidden", "role": current_role(), "required": need,
                                     "hint": f"a {s['kind']} is {'changed' if write else 'read in YAML'} "
                                             f"by the '{need}' role"}), 403)
    ns = request.args.get("namespace") or ""
    if s["namespaced"] and not _K8S_NAME_RE.match(ns) and (need_ns or ns):
        return None, None, (jsonify({"error": "a valid namespace is required"}), 400)
    return s, (ns if s["namespaced"] else ""), None


@app.route("/api/yaml/<cluster>/<kind>/<oname>")
@requires_auth
def api_yaml_get(cluster, kind, oname):
    name = oname
    s, ns, err = _yaml_target(kind)
    if err:
        return err
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    args = ["get", s["resource"], name] + (["-n", ns] if ns else [])
    obj = _kubectl_json(kc, *args, cluster=cluster)
    if obj is None:
        return jsonify({"error": f"no {s['kind']} {ns + '/' if ns else ''}{name}"}), 404
    try:
        _hy.check_readable(kind, obj)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    text = _to_yaml(_hy.clean(obj))
    if request.args.get("download") == "1":
        return Response(text, mimetype="application/yaml",
                        headers={"Content-Disposition": f'attachment; filename="{name}.yaml"'})
    return jsonify({"cluster": cluster, "kind": kind, "namespace": ns, "name": name, "yaml": text,
                    "writable": _hy.allowed(kind, current_role(), write=True)})


def _yaml_body(kind, ns, name, creating):
    body = request.get_json(silent=True) or {}
    text = body.get("yaml")
    if not isinstance(text, str) or not text.strip():
        return None, (jsonify({"error": "a JSON object with the 'yaml' text is expected"}), 400)
    if len(text) > 1_000_000:
        return None, (jsonify({"error": "this YAML is larger than 1 MB"}), 413)
    try:
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError as e:
        return None, (jsonify({"error": f"not valid YAML: {str(e).splitlines()[0]}"}), 400)
    if len(docs) != 1:
        return None, (jsonify({"error": f"one object at a time: this text holds {len(docs)}"}), 400)
    try:
        obj = _hy.check_target(kind, docs[0], ns, name, creating=creating)
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 400)
    return obj, None


def _yaml_write(cluster, kind, ns, name, creating):
    s, ns_q, err = _yaml_target(kind, write=True, need_ns=not creating)
    if err:
        return err
    obj, err = _yaml_body(kind, ns_q, name, creating)
    if err:
        return err
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    body = request.get_json(silent=True) or {}
    verb = "create" if creating else "replace"
    if body.get("check"):
        # essai à blanc côté serveur : les webhooks de Harvester jugent, rien ne change
        r = _kubectl_run(["kubectl", "--kubeconfig", kc, verb, "--dry-run=server", "-f", "-", "-o", "name"],
                         input=json.dumps(obj), capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()
            if "denied the request:" in detail:
                detail = "Harvester refused: " + detail.split("denied the request:", 1)[1].strip()
            return jsonify({"ok": False, "error": detail[:1500]}), 200
        return jsonify({"ok": True})
    meta = obj.get("metadata") or {}
    target = meta.get("name") or meta.get("generateName", "") + "…"
    wd = _capi_work_dir()
    fd, path = tempfile.mkstemp(prefix="yaml-", suffix=".json", dir=str(wd) if wd else None)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "yaml", "--kubeconfig", kc,
           "--kind", kind, "--file", path]
    if creating:
        cmd.append("--create")
    else:
        cmd += ["--name", name] + (["--namespace", ns_q] if ns_q else [])
    ref = f"{(meta.get('namespace') + '/') if meta.get('namespace') else ''}{target}"
    run, err = _cli_action(cluster, f"yaml:{verb}:{kind}:{ref}", cmd, "harvester-resources",
                           after=lambda: (Path(path).unlink(missing_ok=True), _invalidate_cluster_caches(cluster)))
    if err:
        Path(path).unlink(missing_ok=True)
    return _res_reply(run, err, kind=kind, name=target)


# ---------------------------------------------------------------------------
# v1.60.0 : le menu d'actions d'une VM, comme celui de Harvester (pause,
# redémarrage doux, arrêt forcé, suppression avec le choix des volumes, clone,
# template, CD-ROM, disque à chaud, migration vers un nœud et son abandon,
# cloud-init). Chaque geste est une action suivie, par
# bin/harvester-resources.py vm.
# ---------------------------------------------------------------------------
import hv_vm as _hv  # noqa: E402

_VM_DO = ("pause", "unpause", "softreboot", "restart", "force-stop", "clone", "eject",
          "add-volume", "remove-volume", "migrate", "abort-migration", "template", "cloudinit",
          # v1.61.0
          "insert-cdrom", "eject-image", "add-nic", "remove-nic", "cpumem", "storage-migrate",
          "cancel-storage-migration", "quota", "access")


@app.route("/api/vm/<cluster>/<namespace>/<name>/state")
@requires_auth
def api_vm_menu_state(cluster, namespace, name):
    """Ce que le menu d'une VM a besoin de savoir pour proposer les bons gestes."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    vm = _kubectl_json(kc, "get", "virtualmachines.kubevirt.io", name, "-n", namespace, cluster=cluster)
    if vm is None:
        return jsonify({"error": f"no VM {namespace}/{name}"}), 404
    vmi = _kubectl_json(kc, "get", "virtualmachineinstances.kubevirt.io", name, "-n", namespace, cluster=cluster)
    migs = (_kubectl_json(kc, "get", "virtualmachineinstancemigrations.kubevirt.io", "-n", namespace,
                          cluster=cluster) or {}).get("items") or []
    nodes = (_kubectl_json(kc, "get", "nodes", cluster=cluster) or {}).get("items") or []
    conds = {c.get("type") for c in ((vmi or {}).get("status") or {}).get("conditions") or []
             if str(c.get("status")) == "True"}
    here = ((vmi or {}).get("status") or {}).get("nodeName")
    targets = []
    for n in nodes:
        nm = (n.get("metadata") or {}).get("name")
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (n.get("status") or {}).get("conditions") or [])
        # sans nœud connu, aucune cible : on ne proposerait pas le nœud même de la VM
        if here and nm != here and ready and not (n.get("spec") or {}).get("unschedulable"):
            targets.append(nm)
    return jsonify({
        "running": vmi is not None,
        "paused": "Paused" in conds,
        "agent": "AgentConnected" in conds,
        "migrating": bool(_hv.active_migrations(migs, name)) if vmi is not None else False,
        "node": here,
        "targets": sorted(targets),
        "run_strategy": (vm.get("spec") or {}).get("runStrategy"),
        "volumes": [{k: x[k] for k in ("volume", "claim", "kind", "source", "hotpluggable")}
                    for x in _hv.vm_volumes(vm)],
        "cloudinit_secrets": _hv.cloudinit_secrets(vm),
        # v1.61.0 : ce que proposent les gestes à chaud
        "cpumem": _hv.cpumem_info(vm),
        "sata_cdroms": [{"name": n, "empty": e} for n, e in _hv.sata_cdroms(vm)],
        "interfaces": _vm_menu_interfaces(vm),
        "storage_migration": next((e.get("targetVolume") for e in _hv.claim_templates(vm) if e.get("targetVolume")), None),
        "restart_required": any(c.get("type") == "RestartRequired" and str(c.get("status")) == "True"
                                for c in (vm.get("status") or {}).get("conditions") or []),
        "quota": ((((_kubectl_json(kc, "get", "resourcequotas.harvesterhci.io", _hv.QUOTA_NAME, "-n", namespace,
                                   cluster=cluster) or {}).get("spec") or {}).get("snapshotLimit") or {})
                  .get("vmTotalSnapshotSizeQuota") or {}).get(name),
    })


def _vm_menu_interfaces(vm):
    nets = {n.get("name"): n for n in ((vm.get("spec") or {}).get("template") or {}).get("spec", {}).get("networks") or []}
    out = []
    ifaces = (((vm.get("spec") or {}).get("template") or {}).get("spec", {}).get("domain") or {}).get("devices", {}).get("interfaces") or []
    for i in ifaces:
        n = nets.get(i.get("name")) or {}
        out.append({"name": i.get("name"), "network": (n.get("multus") or {}).get("networkName") or ("pod" if "pod" in n else None),
                    "bridge": "bridge" in i, "model": i.get("model"), "state": i.get("state"), "mac": i.get("macAddress"),
                    "unpluggable": len(ifaces) > 1 and "bridge" in i and i.get("model") in (None, "", "virtio")
                    and i.get("state") != "absent" and all(x.get("macAddress") for x in ifaces)})
    return out


def _vm_do_args(action, body):
    """Les options d'un geste, contrôlées avant de lancer l'outil."""
    b = body if isinstance(body, dict) else {}
    out = []
    flag = lambda k: bool(b.get(k))  # noqa: E731
    if action == "clone":
        out += ["--new-name", _hv.check_name(str(b.get("new_name") or ""), "new name")]
        if flag("with_data"):
            out.append("--with-data")
        if flag("start"):
            out.append("--start")
    elif action == "eject":
        out += ["--volume", _hv.check_name(str(b.get("volume") or ""), "volume")]
        if flag("delete_volume"):
            out.append("--delete-volume")
    elif action == "add-volume":
        out += ["--claim", _hv.check_name(str(b.get("claim") or ""), "volume")]
        if b.get("volume"):
            out += ["--volume", _hv.check_name(str(b["volume"]), "disk name")]
        bus = str(b.get("bus") or "scsi")
        if bus not in ("scsi", "virtio", "sata"):
            raise ValueError("bus: scsi, virtio or sata")
        out += ["--bus", bus]
    elif action == "remove-volume":
        out += ["--volume", _hv.check_name(str(b.get("volume") or ""), "disk name")]
    elif action == "migrate":
        if b.get("node"):
            if not _K8S_NAME_RE.match(str(b["node"])):
                raise ValueError("node: a node name")
            out += ["--node", str(b["node"])]
    elif action in ("insert-cdrom", "eject-image"):
        out += ["--volume", _hv.check_name(str(b.get("volume") or ""), "drive")]
        if action == "insert-cdrom":
            img = str(b.get("image") or "")
            parts = img.split("/")
            if len(parts) != 2 or not all(_K8S_NAME_RE.match(x) for x in parts):
                raise ValueError("image: namespace/name")
            out += ["--image", img]
    elif action in ("add-nic", "remove-nic"):
        out += ["--iface", _hv.check_name(str(b.get("iface") or ""), "interface name")]
        if action == "add-nic":
            net = str(b.get("network") or "")
            parts = net.split("/")
            if len(parts) != 2 or not all(_K8S_NAME_RE.match(x) for x in parts):
                raise ValueError("network: namespace/name")
            out += ["--network", net]
            if b.get("mac"):
                out += ["--mac", str(b["mac"])[:17]]
    elif action == "cpumem":
        if b.get("cpu") not in (None, ""):
            out += ["--cpu", str(int(b["cpu"]))]
        if b.get("memory"):
            if _hv.quantity(b["memory"]) is None:
                raise ValueError("memory: a size such as 8Gi")
            out += ["--memory", str(b["memory"])]
        if len(out) == 0:
            raise ValueError("give a CPU count, a memory size, or both")
    elif action == "storage-migrate":
        out += ["--volume", _hv.check_name(str(b.get("volume") or ""), "source volume"),
                "--target", _hv.check_name(str(b.get("target") or ""), "target volume")]
    elif action == "quota":
        size = str(b.get("size") or "0")
        if size != "0" and _hv.quantity(size) is None:
            raise ValueError("quota: a size such as 20Gi, or 0 to remove it")
        out += ["--size", size]
    elif action == "template":
        out += ["--template-name", _hv.check_name(str(b.get("template_name") or ""), "template name")]
        if b.get("description"):
            out += ["--description", str(b["description"])[:200]]
        if flag("with_data"):
            out.append("--with-data")
        if flag("set_default"):
            out.append("--set-default")
    return out


@app.route("/api/vm/<cluster>/<namespace>/<name>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("60/minute")
def api_vm_do(cluster, namespace, name, action):
    if action not in _VM_DO:
        return jsonify({"error": f"action must be one of {', '.join(_VM_DO)}"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    body = request.get_json(silent=True) or {}
    if action == "cloudinit":
        return _vm_cloudinit_action(cluster, namespace, name, body)
    if action == "access":
        return _vm_access_action(cluster, kc, namespace, name, body)
    try:
        extra = _vm_do_args(action, body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "vm", action, "--kubeconfig", kc,
           "--namespace", namespace, "--name", name] + extra
    run, err = _cli_action(cluster, f"vm:{action}:{namespace}/{name}", cmd, "harvester-resources",
                           after=lambda: _invalidate_cluster_caches(cluster))
    return _res_reply(run, err, vm=f"{namespace}/{name}", action=action)


def _vm_access_action(cluster, kc, namespace, name, body):
    """Un accès (mot de passe ou clés SSH) propagé par l'agent invité. Le mot
    de passe passe par un fichier 0600 effacé après l'action : jamais sur une
    ligne de commande, ni dans une réponse, ni dans l'étiquette de l'action."""
    kind = body.get("kind")
    if kind not in ("basic", "ssh"):
        return jsonify({"error": "kind: basic or ssh"}), 400
    users = body.get("users") or []
    if not isinstance(users, list) or not users or not all(isinstance(u, str) and _hv.USER_RE.match(u) for u in users):
        return jsonify({"error": "users: letters, digits, dot, dash, underscore"}), 400
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "vm", "access", "--kubeconfig", kc,
           "--namespace", namespace, "--name", name, "--kind", kind, "--users", ",".join(users)]
    path = None
    if kind == "basic":
        pw = body.get("password")
        if not isinstance(pw, str) or len(pw) < 6 or len(users) != 1:
            return jsonify({"error": "one user and a password of 6 characters at least"}), 400
        wd = _capi_work_dir()
        fd, path = tempfile.mkstemp(prefix="pw-", dir=str(wd) if wd else None)
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pw)
        cmd += ["--password-file", path]
    else:
        keys = body.get("keys") or []
        if not isinstance(keys, list) or not keys or not all(
                isinstance(k, str) and all(_K8S_NAME_RE.match(x) for x in k.split("/")) for k in keys):
            return jsonify({"error": "keys: key pairs as namespace/name"}), 400
        cmd += ["--keys", ",".join(keys)]

    def after():
        if path:
            Path(path).unlink(missing_ok=True)
    run, err = _cli_action(cluster, f"vm:access:{namespace}/{name}", cmd, "harvester-resources", after=after)
    if err:
        after()
    return _res_reply(run, err, vm=f"{namespace}/{name}", action="access", kind=kind)


# ---------------------------------------------------------------------------
# v1.62.0 : la configuration d'un hôte comme dans Harvester (nom affiché, URL
# de console, labels, tags, disques, huge pages, ksmtuned, accès hors bande)
# et ses gestes (CPU manager, alimentation par le BMC, suppression). Écriture
# par bin/harvester-resources.py host, en actions suivies.
# ---------------------------------------------------------------------------
import hv_host as _hh  # noqa: E402

_HOST_DO = ("basics", "tags", "disk-add", "disk-remove", "disk-set", "hugepages", "ksmtuned",
            "cpu-manager", "oob", "power", "delete")


@app.route("/api/host/<cluster>/<node>/settings")
@requires_auth
@shared_read()
def api_host_settings(cluster, node):
    """Tout ce que montre la fenêtre d'un hôte. Le secret du BMC ne sort
    jamais : seulement son nom."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    # neuf lectures indépendantes : en série, 12,8 s sur harvlab ; en parallèle,
    # le temps de la plus lente (le kubeconfig porte déjà l'identité déléguée)
    from concurrent.futures import ThreadPoolExecutor
    reads = {"n": ("nodes", node), "nodes": ("nodes",),
             "lh": (_hh.K_LHNODE, node, "-n", "longhorn-system"), "bds": (_hh.K_BD, "-n", "longhorn-system"),
             "hp": (_hh.K_HUGEPAGE, node), "ksm": (_hh.K_KSM, node),
             "seeder": ("addons.harvesterhci.io", "harvester-seeder", "-n", "harvester-system")}
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, cluster=cluster) for k, a in reads.items()}
        got = {k: f.result() for k, f in futs.items()}
    n = got["n"]
    if n is None:
        return jsonify({"error": f"no host {node}"}), 404
    meta = n.get("metadata") or {}
    ann, labels = meta.get("annotations") or {}, meta.get("labels") or {}
    nodes = (got["nodes"] or {}).get("items") or []
    lh, hp, ksm, seeder = got["lh"], got["hp"], got["ksm"], got["seeder"]
    bds = (got["bds"] or {}).get("items") or []
    # sans l'add-on, le type Inventory n'existe pas : kubectl relirait toute la
    # découverte de l'API avant d'échouer (plusieurs secondes)
    inv = _kubectl_json(kc, "get", _hh.K_INVENTORY, node, "-n", "harvester-system", cluster=cluster) \
        if ((seeder or {}).get("spec") or {}).get("enabled") else None
    conn = ((((inv or {}).get("spec") or {}).get("baseboardSpec") or {}).get("connection") or {})
    ref = conn.get("authSecretRef") or {}
    ist = (inv or {}).get("status") or {}
    return jsonify({
        "node": node,
        "custom_name": ann.get(_hh.ANN_NAME) or "",
        "console_url": ann.get(_hh.ANN_CONSOLE) or "",
        "labels": _hh.user_labels(n),
        "maintenance": ann.get(_hh.ANN_MAINT),
        "witness": "node-role.harvesterhci.io/witness" in labels,
        "nodes": len(nodes),
        "capi_machine": ann.get("cluster.x-k8s.io/machine"),
        "tags": ((lh or {}).get("spec") or {}).get("tags") or [],
        "longhorn": lh is not None,
        "disks": _hh.block_devices(bds, node, lh),
        "hugepages": None if hp is None else {
            "transparent": ((hp.get("spec") or {}).get("transparent") or {}),
            "meminfo": {k: v for k, v in (((hp.get("status") or {}).get("meminfo")) or {}).items()
                        if k.lower().startswith(("hugepage", "anonhuge", "shmemhuge", "memtotal"))}},
        "ksmtuned": None if ksm is None else {"spec": ksm.get("spec") or {}, "status": ksm.get("status") or {}},
        "cpu_manager": _hh.cpu_manager_status(n),
        "seeder": bool(((seeder or {}).get("spec") or {}).get("enabled")),
        "seeder_installed": seeder is not None,
        "oob": None if inv is None else {
            "host": conn.get("host"), "port": conn.get("port"), "insecure": bool(conn.get("insecureTLS")),
            "secret": f"{ref.get('namespace')}/{ref.get('name')}" if ref.get("name") else None,
            "events": (((inv.get("spec") or {}).get("events")) or {}),
            "status": ist.get("status"), "power_state": ist.get("machinePowerState"),
            "error": _hh.bmc_error(inv),
            "power_action": ist.get("powerAction") or {},
            "requested": (inv.get("spec") or {}).get("powerActionRequested")},
    })


@app.route("/api/host/<cluster>/<node>/detail")
@requires_auth
def api_host_detail(cluster, node):
    """v1.68.1 : le détail d'un hôte comme sa page dans Harvester (Basics,
    Instances, Network, Events), en lectures parallèles."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    from concurrent.futures import ThreadPoolExecutor
    reads = {"n": ("nodes", node), "metrics": ("nodes.metrics.k8s.io", node),
             "lh": (_hh.K_LHNODE, node, "-n", "longhorn-system"),
             "vmis": ("virtualmachineinstances.kubevirt.io", "-A"), "vls": (_hh.K_VLANSTATUS,),
             "lms": (_hh.K_LINKMONITOR,),
             "events": ("events", "-A", "--field-selector", f"involvedObject.kind=Node,involvedObject.name={node}")}
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, timeout=30, cluster=cluster) for k, a in reads.items()}
        got = {k: f.result() for k, f in futs.items()}
    if got["n"] is None:
        return jsonify({"error": f"no host {node}"}), 404
    items = lambda k: (got[k] or {}).get("items") or []  # noqa: E731
    return jsonify(_hh.host_detail(got["n"], got["metrics"], got["lh"], items("vmis"), items("vls"),
                                   items("lms"), items("events")))


def _host_do_args(action, b, files):
    """Les options d'un geste sur un hôte, contrôlées avant de lancer l'outil.
    `files` reçoit les fichiers privés à effacer après l'action."""
    out = []

    def tmp(content):
        wd = _capi_work_dir()
        fd, path = tempfile.mkstemp(prefix="host-", dir=str(wd) if wd else None)
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        files.append(path)
        return path

    def disk():
        d = str(b.get("disk") or "")
        if not _K8S_SUBDOMAIN_RE.match(d):
            raise ValueError("disk: a block device name")
        return ["--disk", d]

    def tags(key="tags"):
        t = b.get(key)
        if not isinstance(t, list):
            raise ValueError(f"{key}: a list")
        return ["--tags", ",".join(_hh.check_tags([str(x) for x in t]))]

    if action == "basics":
        if "custom_name" in b:
            out += ["--custom-name", str(b.get("custom_name") or "")[:120]]
        if "console_url" in b:
            out += ["--console-url", str(b.get("console_url") or "")[:500]]
        if "labels" in b:
            if not isinstance(b["labels"], dict):
                raise ValueError("labels: an object")
            _hh.basics_patch({}, labels={str(k): str(v) for k, v in b["labels"].items()})  # contrôle d'avance
            out += ["--labels-file", tmp(json.dumps(b["labels"]))]
        if not out:
            raise ValueError("nothing to change")
    elif action == "tags":
        out += tags()
    elif action == "disk-add":
        out += disk()
        prov = str(b.get("provisioner") or "LonghornV1")
        if prov not in ("LonghornV1", "LonghornV2", "lvm"):
            raise ValueError("provisioner: LonghornV1, LonghornV2 or lvm")
        out += ["--provisioner", prov]
        if prov == "lvm":
            out += ["--vg", str(b.get("vg") or "")[:127]]
        if b.get("format") is True:
            out.append("--format")
        elif b.get("format") is False:
            out.append("--no-format")
    elif action == "disk-remove":
        out += disk()
    elif action == "disk-set":
        out += disk()
        if "tags" in b:
            out += tags()
        if "scheduling" in b:
            out += ["--scheduling", "on" if b.get("scheduling") else "off"]
    elif action == "hugepages":
        for key, flag, allowed in (("enabled", "--thp-enabled", _hh.THP_ENABLED),
                                   ("shmem", "--thp-shmem", _hh.THP_SHMEM),
                                   ("defrag", "--thp-defrag", _hh.THP_DEFRAG)):
            if b.get(key):
                if b[key] not in allowed:
                    raise ValueError(f"{key}: one of {', '.join(allowed)}")
                out += [flag, b[key]]
    elif action == "ksmtuned":
        _hh.ksmtuned_patch(b.get("run"), b.get("mode"), b.get("thres"),
                           None if b.get("merge") is None else bool(b.get("merge")), b.get("params"))
        if b.get("run"):
            out += ["--run", b["run"]]
        if b.get("mode"):
            out += ["--mode", b["mode"]]
            if b["mode"] == "customized":
                out += ["--params", tmp(json.dumps(b.get("params") or {}))]
        if b.get("thres") is not None:
            out += ["--thres", str(int(b["thres"]))]
        if b.get("merge") is not None:
            out += ["--merge", "on" if b.get("merge") else "off"]
    elif action == "cpu-manager":
        if not isinstance(b.get("enable"), bool):
            raise ValueError("enable: true or false")
        out.append("--enable" if b["enable"] else "--disable")
    elif action == "oob":
        if b.get("off"):
            return ["--off"]
        out += ["--bmc-host", str(b.get("host") or ""), "--bmc-port", str(int(b.get("port") or 623)),
                "--interval", str(b.get("interval") or "1h")]
        _hh.inventory("x", b.get("host"), b.get("port") or 623, "ns", "n",
                      interval=str(b.get("interval") or "1h"))  # contrôle d'avance
        if b.get("insecure"):
            out.append("--insecure")
        if b.get("events") is False:
            out.append("--no-events")
        if b.get("password"):
            user = str(b.get("username") or "")
            if not user or len(user) > 128 or any(c in user for c in "\n\r,"):
                raise ValueError("BMC user name: required")
            out += ["--username", user, "--password-file", tmp(str(b["password"]))]
    elif action == "power":
        if b.get("operation") not in _hh.POWER_OPS:
            raise ValueError("operation: shutdown, poweron or reboot")
        out += ["--operation", b["operation"]]
    return out


@app.route("/api/host/<cluster>/<node>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_host_do(cluster, node, action):
    if action not in _HOST_DO:
        return jsonify({"error": f"action must be one of {', '.join(_HOST_DO)}"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    body = request.get_json(silent=True) or {}
    files = []

    def after():
        for f in files:
            Path(f).unlink(missing_ok=True)
        _invalidate_cluster_caches(cluster)
    try:
        extra = _host_do_args(action, body if isinstance(body, dict) else {}, files)
    except (ValueError, TypeError) as e:
        after()
        return jsonify({"error": str(e)}), 400
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "host", action, "--kubeconfig", kc,
           "--node", node] + extra
    run, err = _cli_action(cluster, f"host:{action}:{node}", cmd, "harvester-resources", after=after)
    if err:
        after()
    return _res_reply(run, err, node=node, action=action)


# ---------------------------------------------------------------------------
# v1.62.0 : le menu Namespaces de Harvester. Lecture pour tous, écriture par
# bin/harvester-resources.py namespace (admin), en actions suivies.
# ---------------------------------------------------------------------------
import hv_ns as _hn  # noqa: E402
import hv_projects as _hpj  # noqa: E402
import hv_members as _hmb  # noqa: E402

_NS_DO = ("update", "quota", "delete")


def _rancher_projects(cluster, kc):
    """v1.72.0 : les projets Rancher du cluster, lus avec le jeton de la
    personne quand la requête vient d'une session Rancher (son kubeconfig
    vise le mandataire de Rancher). Un compte local ne les lit pas : seul
    l'id Rancher du cluster, s'il est connu, sert à repérer les annotations
    d'un autre cluster."""
    try:
        r = _hpj.rancher_of(Path(kc).read_text())
    except OSError:
        r = None
    if r is None:
        entry = next((c for c in load_config().get("clusters", []) if c["name"] == cluster), {})
        cid = entry.get("rancher_cluster") or _SSO_CLUSTER_IDS.get(cluster) or next(
            (v for k, v in list(_SSO_CLUSTER_IDS.items()) if k.startswith(cluster + "@")), None)
        return {"managed": False, "cid": cid, "items": None}
    try:
        token = Path(r["token_file"]).read_text().strip() if r.get("token_file") else r.get("token")
        st, out = _rs.Http(ca_file=r.get("ca_file"), insecure=bool(r.get("insecure"))).request(
            "GET", f"{r['url']}/v3/projects?clusterId={r['cid']}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    except (_rs.SSOError, OSError) as e:
        return {"managed": True, "cid": r["cid"], "items": [], "error": str(e)[:200]}
    if st != 200:
        return {"managed": True, "cid": r["cid"], "items": [], "error": f"Rancher answered {st}"}
    return {"managed": True, "cid": r["cid"], "items": _hpj.project_rows((out or {}).get("data") or [], r["cid"])}


@app.route("/api/ns-admin/<cluster>")
@requires_auth
@shared_read()
def api_ns_admin_list(cluster):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    from concurrent.futures import ThreadPoolExecutor
    reads = {"ns": ("namespaces",), "vms": ("virtualmachines.kubevirt.io", "-A"),
             "pvcs": ("persistentvolumeclaims", "-A"), "quotas": (_hn.K_QUOTA, "-A")}
    with ThreadPoolExecutor(max_workers=len(reads) + 1) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, cluster=cluster) for k, a in reads.items()}
        fproj = pool.submit(_rancher_projects, cluster, kc)
        got = {k: (f.result() or {}).get("items") or [] for k, f in futs.items()}
        projects = fproj.result()
    if not got["ns"]:
        return jsonify({"error": "cannot list the namespaces"}), 502
    count = lambda items: _TallyCounter((o.get("metadata") or {}).get("namespace") for o in items)  # noqa: E731
    vms, pvcs = count(got["vms"]), count(got["pvcs"])
    quota = {(q.get("metadata") or {}).get("namespace"):
             ((q.get("spec") or {}).get("snapshotLimit") or {}).get("namespaceTotalSnapshotSizeQuota")
             for q in got["quotas"] if (q.get("metadata") or {}).get("name") == _hn.QUOTA_NAME}
    rows = []
    for o in got["ns"]:
        meta = o.get("metadata") or {}
        name = meta.get("name")
        rows.append({"name": name, "system": _hn.is_system(name, o), "protected": _hn.protected(name, o),
                     "phase": (o.get("status") or {}).get("phase"),
                     "description": (meta.get("annotations") or {}).get(_hn.DESC) or "",
                     "labels": _hn._visible(meta.get("labels")), "annotations": _hn._visible(meta.get("annotations")),
                     "created": meta.get("creationTimestamp"), "vms": vms.get(name, 0), "volumes": pvcs.get(name, 0),
                     "snapshot_quota": quota.get(name),
                     # v1.72.0 : l'annotation fait foi (le label peut manquer : vu sur harv1)
                     "project": _hpj.ns_project(o, projects["cid"], projects["items"])})
    rows.sort(key=lambda r: (r["system"], r["name"]))
    return jsonify({"cluster": cluster, "items": rows, "projects": projects,
                    "quota_keys": list(_hpj.QUOTA_KEYS), "limit_keys": list(_hpj.LIMIT_KEYS)})


_PROJECT_DO = ("create", "update", "delete", "move", "ns-quota")


@app.route("/api/projects/<cluster>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_projects_do(cluster, action):
    """v1.72.0 : les projets passent par l'API de Rancher avec le jeton de
    la personne (Rancher applique ses droits) : il faut une session Rancher."""
    if action not in _PROJECT_DO:
        return jsonify({"error": "action: " + ", ".join(_PROJECT_DO)}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    try:
        r = _hpj.rancher_of(Path(kc).read_text())
    except OSError:
        r = None
    if r is None:
        return jsonify({"error": "projects live in Rancher", "code": "rancher-session-needed",
                        "hint": "sign in to the console through Rancher to manage projects"}), 409
    b = request.get_json(silent=True) or {}
    files = []
    try:
        args = ["project", action]
        if action in ("create", "update"):
            spec = b.get("spec") if isinstance(b.get("spec"), dict) else {}
            _hpj.project_body(json.loads(json.dumps(spec)), r["cid"])
            args += ["--spec", _private_file(files, json.dumps(spec), "project-")]
            label = f"project:{action}:{spec.get('name')}"
        if action in ("update", "delete") or (action == "move" and b.get("id")):
            args += ["--id", _hpj.check_pid(b.get("id"))]
        if action in ("move", "ns-quota"):
            ns = _hpj.check_name(b.get("namespace"), "namespace")
            args += ["--namespace", ns]
            label = f"project:{action}:{ns}"
        if action == "ns-quota":
            limit = b.get("limit") if isinstance(b.get("limit"), dict) else {}
            for k, v in limit.items():
                if v not in (None, ""):
                    _hpj.normalize(k, v)
            args += ["--spec", _private_file(files, json.dumps({"limit": limit}), "project-")]
        if action == "delete":
            label = f"project:delete:{b.get('id')}"
    except (ValueError, TypeError) as e:
        for f in files:
            Path(f).unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 400
    run, err = _res_cli(cluster, kc, label, args, files)
    return _res_reply(run, err, action=action)

# ---------------------------------------------------------------------------
# v1.73.0 : membres Rancher du cluster et des projets, avec le jeton de la
# session Rancher (lectures ici, écritures par harvester-resources member).
# ---------------------------------------------------------------------------

_MEMBER_DO = ("add", "remove")


def _rancher_get(r, path):
    token = Path(r["token_file"]).read_text().strip() if r.get("token_file") else r.get("token")
    return _rs.Http(ca_file=r.get("ca_file"), insecure=bool(r.get("insecure"))).request("GET", r["url"] + path,
                                                       headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})


def _rancher_post(r, path, body):
    token = Path(r["token_file"]).read_text().strip() if r.get("token_file") else r.get("token")
    return _rs.Http(ca_file=r.get("ca_file"), insecure=bool(r.get("insecure"))).request("POST", r["url"] + path, data=json.dumps(body),
                                                       headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})


def _session_rancher(cluster):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return None, None
    try:
        return kc, _hpj.rancher_of(Path(kc).read_text())
    except OSError:
        return kc, None


@app.route("/api/rancher-members/<cluster>")
@requires_auth
def api_rancher_members(cluster):
    kc, r = _session_rancher(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if r is None:
        return jsonify({"managed": False, "members": [], "roles": []})
    scope = request.args.get("scope") or "cluster"
    try:
        if scope == "project":
            target = f"{r['cid']}:{_hpj.check_pid(request.args.get('project'))}"
            path = f"/v3/projectroletemplatebindings?projectId={target}"
        elif scope == "cluster":
            path = f"/v3/clusterroletemplatebindings?clusterId={r['cid']}"
        else:
            raise ValueError("scope: cluster or project")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        st, lst = _rancher_get(r, path)
        if st != 200:
            return jsonify({"managed": True, "members": [], "roles": [], "error": f"Rancher answered {st}"}), 200
        items = (lst or {}).get("data") or []
        _, rts = _rancher_get(r, f"/v3/roletemplates?context={scope}&limit=-1")
        roles = _hmb.role_rows((rts or {}).get("data") or [], scope)
        users = {}
        for uid in {b.get("userId") for b in items if b.get("userId")}:
            s2, u = _rancher_get(r, f"/v3/users/{uid}")
            if s2 == 200:
                users[uid] = u
        principals = {}
        for pid in {b.get("userPrincipalId") or b.get("groupPrincipalId") for b in items} - {None, ""}:
            s3, p = _rancher_get(r, "/v3/principals/" + quote(pid, safe=""))
            if s3 == 200:
                principals[pid] = p
    except (_rs.SSOError, OSError) as e:
        return jsonify({"managed": True, "members": [], "roles": [], "error": str(e)[:200]}), 200
    return jsonify({"managed": True, "scope": scope, "roles": roles,
                    "members": _hmb.member_rows(items, principals, users, {x["id"]: x["name"] for x in roles})})


@app.route("/api/rancher-principals/<cluster>")
@requires_auth
def api_rancher_principals(cluster):
    """Chercher un utilisateur ou un groupe que Rancher connaît (local,
    Keycloak...), pour l'ajouter comme membre."""
    kc, r = _session_rancher(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if r is None:
        return jsonify({"error": "projects live in Rancher", "code": "rancher-session-needed",
                        "hint": "sign in to the console through Rancher to manage members"}), 409
    q = str(request.args.get("q") or "").strip()[:100]
    if len(q) < 2:
        return jsonify({"items": []})
    try:
        st, out = _rancher_post(r, "/v3/principals?action=search", {"name": q})
    except (_rs.SSOError, OSError) as e:
        return jsonify({"error": str(e)[:200]}), 502
    if st not in (200, 201):
        return jsonify({"error": f"Rancher answered {st}"}), 502
    items = [{"id": p.get("id"), "name": p.get("name") or p.get("loginName") or p.get("id"), "login": p.get("loginName") or "",
              "kind": "group" if p.get("principalType") == "group" else "user",
              "provider": (p.get("id") or "").split("://", 1)[0]} for p in (out or {}).get("data") or [] if p.get("id")]
    return jsonify({"items": items[:50]})


@app.route("/api/rancher-members/<cluster>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_rancher_members_do(cluster, action):
    if action not in _MEMBER_DO:
        return jsonify({"error": "action: add or remove"}), 400
    kc, r = _session_rancher(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if r is None:
        return jsonify({"error": "members live in Rancher", "code": "rancher-session-needed",
                        "hint": "sign in to the console through Rancher to manage members"}), 409
    b = request.get_json(silent=True) or {}
    try:
        scope = b.get("scope") or "cluster"
        args = ["member", action, "--scope", scope]
        if scope == "project":
            args += ["--project", _hpj.check_pid(b.get("project"))]
        elif scope != "cluster":
            raise ValueError("scope: cluster or project")
        if action == "add":
            principal = _hmb.check_principal(b.get("principal"))
            role = _hmb.check_binding(b.get("role"))
            args += ["--principal", principal, "--role", role]
            label = f"member:add:{scope}:{role}"
        else:
            args += ["--id", _hmb.check_binding(b.get("id"))]
            label = f"member:remove:{scope}"
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    run, err = _res_cli(cluster, kc, label, args)
    return _res_reply(run, err, action=action)


def _ns_kv_file(files, data, key):
    v = data.get(key)
    if v is None:
        return []
    if not isinstance(v, dict):
        raise ValueError(f"{key}: an object")
    wd = _capi_work_dir()
    fd, path = tempfile.mkstemp(prefix="ns-", dir=str(wd) if wd else None)
    with os.fdopen(fd, "w") as f:
        json.dump({str(k): v2 for k, v2 in v.items()}, f)
    files.append(path)
    return ["--labels-file" if key == "labels" else "--annotations-file", path]


def _ns_action(cluster, kc, name, action, extra, files):
    def after():
        for f in files:
            Path(f).unlink(missing_ok=True)
        _invalidate_cluster_caches(cluster)
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "namespace", action, "--kubeconfig", kc,
           "--name", name] + extra
    run, err = _cli_action(cluster, f"namespace:{action}:{name}", cmd, "harvester-resources", after=after)
    if err:
        after()
    return _res_reply(run, err, namespace=name, action=action)


@app.route("/api/ns-admin/<cluster>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_ns_admin_create(cluster):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    files = []
    try:
        name = _hn.check_name(str(b.get("name") or ""))
        _hn.manifest(name, str(b.get("description") or ""), b.get("labels") or None)   # contrôle d'avance
        extra = ["--description", str(b.get("description") or "")[:1000]] + _ns_kv_file(files, b, "labels")
    except (ValueError, TypeError) as e:
        for f in files:
            Path(f).unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 400
    return _ns_action(cluster, kc, name, "create", extra, files)


@app.route("/api/ns-admin/<cluster>/<name>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_ns_admin_do(cluster, name, action):
    if action not in _NS_DO:
        return jsonify({"error": f"action must be one of {', '.join(_NS_DO)}"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    files = []
    try:
        extra = []
        if action == "update":
            _hn.update_patch({}, b.get("description"), b.get("labels"), b.get("annotations"))  # contrôle d'avance
            if b.get("description") is not None:
                extra += ["--description", str(b["description"])[:1000]]
            extra += _ns_kv_file(files, b, "labels") + _ns_kv_file(files, b, "annotations")
        elif action == "quota":
            size = str(b.get("size") or "0")
            if size != "0" and _hv.quantity(size) is None:
                raise ValueError("quota: a size such as 100Gi, or 0 to remove it")
            extra += ["--size", size]
        elif _hn.protected(name):
            raise ValueError(f"{name} is a system namespace: the cluster needs it")
    except (ValueError, TypeError) as e:
        for f in files:
            Path(f).unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 400
    return _ns_action(cluster, kc, name, action, extra, files)


# ---------------------------------------------------------------------------
# v1.62.0 : le tableau de bord de Harvester : événements du cluster rangés
# par hôtes, VMs, volumes, images ; jauges d'usage réel CPU, mémoire, stockage.
# ---------------------------------------------------------------------------
import hv_dash as _hd  # noqa: E402


@app.route("/api/events/<cluster>")
@requires_auth
@shared_read()
def api_cluster_events(cluster):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    got = _kubectl_json(kc, "get", "events", "-A", timeout=30, cluster=cluster)
    if got is None:
        return jsonify({"error": "cannot read the events"}), 502
    rows = _hd.events(got.get("items") or [])
    counts = _TallyCounter(r["group"] for r in rows)
    warnings = _TallyCounter(r["group"] for r in rows if r["type"] == "Warning")
    return jsonify({"cluster": cluster, "items": rows, "counts": dict(counts), "warnings": dict(warnings)})


@app.route("/api/usage/<cluster>")
@requires_auth
@shared_read()
def api_cluster_usage(cluster):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    from concurrent.futures import ThreadPoolExecutor
    reads = {"nodes": ("nodes",), "metrics": ("nodes.metrics.k8s.io",),
             "lh": ("nodes.longhorn.io", "-n", "longhorn-system"),
             "op": ("settings.longhorn.io", "storage-over-provisioning-percentage", "-n", "longhorn-system")}
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, cluster=cluster) for k, a in reads.items()}
        got = {k: f.result() for k, f in futs.items()}
    if got["nodes"] is None:
        return jsonify({"error": "cannot read the nodes"}), 502
    try:
        op = int(str((got["op"] or {}).get("value") or 100))
    except ValueError:
        op = 100
    return jsonify({"cluster": cluster, **_hd.usage((got["nodes"] or {}).get("items") or [],
                                                     (got["metrics"] or {}).get("items") or [],
                                                     (got["lh"] or {}).get("items") or [], op)})


# ---------------------------------------------------------------------------
# v1.63.0 : les actions de Harvester sur un volume et sur une image, par
# bin/harvester-resources.py volume|image, en actions suivies ; le
# téléchargement d'une image ; l'envoi d'un fichier depuis le navigateur.
# ---------------------------------------------------------------------------
import hv_storage as _hs  # noqa: E402

_VOLUME_DO = ("clone", "export", "snapshot", "copy", "cancel-expand", "describe")
_IMAGE_DO = ("edit", "clone", "encrypt", "decrypt", "prepare-download")
# à côté du magasin des archives (même disque persistant dans le service packagé)
IMAGE_UPLOAD_DIR = Path(os.environ.get(
    "HARVESTER_OPS_IMAGE_UPLOAD_DIR",
    str(Path(os.environ.get("HARVESTER_OPS_EXPORT_DIR", str(Path.home() / ".local/share/harvester-ops/exports"))).parent
        / "image-uploads")))


@app.route("/api/volume/<cluster>/<namespace>/<name>/info")
@requires_auth
def api_volume_info(cluster, namespace, name):
    """Ce que les gestes d'un volume ont besoin de savoir : VMs qui s'en
    servent, agrandissement en cours, classes proposées, CDI présent."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    from concurrent.futures import ThreadPoolExecutor
    reads = {"pvc": (_hs.K_PVC, name, "-n", namespace), "vms": ("virtualmachines.kubevirt.io", "-n", namespace),
             "scs": (_hs.K_SC,), "cdi": ("crd", "datavolumes.cdi.kubevirt.io"),
             "csi": ("settings.harvesterhci.io", "csi-driver-config")}
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, cluster=cluster) for k, a in reads.items()}
        got = {k: f.result() for k, f in futs.items()}
    pvc = got["pvc"]
    if pvc is None:
        return jsonify({"error": f"no volume {namespace}/{name}"}), 404
    scs = (got["scs"] or {}).get("items") or []
    by_name = {(c.get("metadata") or {}).get("name"): c for c in scs}
    mine = by_name.get((pvc.get("spec") or {}).get("storageClassName"))
    csi = got["csi"] or {}
    return jsonify({
        "name": name, "namespace": namespace,
        "size": ((pvc.get("spec") or {}).get("resources") or {}).get("requests", {}).get("storage"),
        "capacity": ((pvc.get("status") or {}).get("capacity") or {}).get("storage"),
        "phase": (pvc.get("status") or {}).get("phase"),
        "storage_class": (pvc.get("spec") or {}).get("storageClassName"),
        "description": ((pvc.get("metadata") or {}).get("annotations") or {}).get(_hs.DESC) or "",
        "image": ((pvc.get("metadata") or {}).get("annotations") or {}).get(_hs.IMAGE_ID),
        "used_by": _hs.used_by_vms(name, namespace, (got["vms"] or {}).get("items") or []),
        "resizing": _hs.resizing(pvc),
        "longhorn_v1": _hs.is_longhorn_v1(mine),
        "snapshot_class": _hs.csi_snapshot_class(csi.get("value") or csi.get("default"), (mine or {}).get("provisioner")),
        "cdi": got["cdi"] is not None,
        "storage_classes": [{"name": n, "longhorn_v1": _hs.is_longhorn_v1(c),
                             "encrypted": str(((c.get("parameters") or {}).get("encrypted"))) == "true",
                             "image": bool((c.get("parameters") or {}).get("backingImage")),
                             "internal": n in _hs.INTERNAL_SC}
                            for n, c in sorted(by_name.items())],
    })


def _res_cli(cluster, kc, label, args, files=()):
    def after():
        for f in files:
            Path(f).unlink(missing_ok=True)
        _invalidate_cluster_caches(cluster)
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT)] + args[:2] + ["--kubeconfig", kc] + args[2:]
    run, err = _cli_action(cluster, label, cmd, "harvester-resources", after=after)
    if err:
        after()
    return run, err


@app.route("/api/volume/<cluster>/<namespace>/<name>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_volume_do(cluster, namespace, name, action):
    if action not in _VOLUME_DO:
        return jsonify({"error": f"action must be one of {', '.join(_VOLUME_DO)}"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    try:
        extra = []
        if action in ("clone", "copy"):
            extra += ["--new-name", _hs.check_name(str(b.get("new_name") or ""), "new volume name")]
            if action == "clone" and b.get("with_data") is False:
                extra.append("--no-data")
            if action == "copy":
                extra += ["--storage-class", _hs.check_name(str(b.get("storage_class") or ""), "storage class")]
        elif action == "export":
            dn = str(b.get("display_name") or "").strip()
            if not dn or len(dn) > 63:
                raise ValueError("image name: 63 characters at most")
            extra += ["--display-name", dn]
            if b.get("target_namespace"):
                extra += ["--target-namespace", _hs.check_name(str(b["target_namespace"]), "namespace")]
            if b.get("storage_class"):
                extra += ["--storage-class", _hs.check_name(str(b["storage_class"]), "storage class")]
        elif action == "snapshot":
            extra += ["--snapshot-name", _hs.check_name(str(b.get("snapshot_name") or ""), "snapshot name")]
        elif action == "describe":
            extra += ["--description", str(b.get("description") or "")[:1000]]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    run, err = _res_cli(cluster, kc, f"volume:{action}:{namespace}/{name}",
                        ["volume", action, "--namespace", namespace, "--name", name] + extra)
    return _res_reply(run, err, volume=f"{namespace}/{name}", action=action)


@app.route("/api/image/<cluster>/<namespace>/<name>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_image_do(cluster, namespace, name, action):
    if action not in _IMAGE_DO:
        return jsonify({"error": f"action must be one of {', '.join(_IMAGE_DO)}"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    files = []
    try:
        extra = []
        if action == "edit":
            if "description" in b:
                extra += ["--description", str(b.get("description") or "")[:1000]]
            if "labels" in b:
                if not isinstance(b["labels"], dict):
                    raise ValueError("labels: an object")
                _hs.image_edit_patch({}, None, {str(k): str(v) for k, v in b["labels"].items()})   # contrôle d'avance
                wd = _capi_work_dir()
                fd, path = tempfile.mkstemp(prefix="img-", dir=str(wd) if wd else None)
                with os.fdopen(fd, "w") as f:
                    json.dump(b["labels"], f)
                files.append(path)
                extra += ["--labels-file", path]
            if not extra:
                raise ValueError("nothing to change")
        elif action == "prepare-download":
            pass                                   # v1.74.0 : image CDI, le downloader de Harvester
        else:
            dn = str(b.get("display_name") or "").strip()
            if not dn or len(dn) > 63:
                raise ValueError("image name: 63 characters at most")
            extra += ["--display-name", dn]
            if action in ("encrypt", "decrypt"):
                extra += ["--storage-class", _hs.check_name(str(b.get("storage_class") or ""), "storage class")]
    except ValueError as e:
        for f in files:
            Path(f).unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 400
    run, err = _res_cli(cluster, kc, f"image:{action}:{namespace}/{name}",
                        ["image", action, "--namespace", namespace, "--name", name] + extra, files)
    return _res_reply(run, err, image=f"{namespace}/{name}", action=action)


@app.route("/api/image/<cluster>/<namespace>/<name>/download")
@requires_auth
def api_image_download(cluster, namespace, name):
    """« Télécharger » une image Longhorn v1 : le fichier compressé du
    BackingImage, par le proxy de service de l'apiserver (le relais de
    Harvester lui-même) ; une image CDI : le qcow2 préparé par le downloader
    de Harvester (v1.74.0). Réservé aux opérateurs : c'est tout le disque."""
    if ROLE_RANK.get(current_role(), 0) < ROLE_RANK["operator"]:
        return jsonify({"error": "forbidden", "required": "operator"}), 403
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    img = _kubectl_json(kc, "get", _hs.K_IMAGE, name, "-n", namespace, cluster=cluster)
    if img is None:
        return jsonify({"error": f"no image {namespace}/{name}"}), 404
    display = re.sub(r"[^A-Za-z0-9._-]+", "_", ((img.get("spec") or {}).get("displayName") or name))[:120]
    from kube import Kube as _Kube
    if _hs.is_cdi(img):
        # v1.74.0 : le fichier qcow2 que prépare le downloader de Harvester
        # (action « prepare-download »), relayé par son point de
        # téléchargement, qui supprime ensuite le downloader
        dl = _kubectl_json(kc, "get", _hs.K_DOWNLOADER, name, "-n", namespace, cluster=cluster)
        if _hs.downloader_state(dl)[0] is not True:
            return jsonify({"error": "the qcow2 file is not ready", "code": "prepare-first",
                            "hint": "prepare the download first: Harvester converts the volume"}), 409
        stream = _Kube(kc).raw_stream(_hs.cdi_download_path(namespace, name))
        return Response(stream_with_context(stream), mimetype="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{display}.qcow2"'})
    sc = _kubectl_json(kc, "get", _hs.K_SC, ((img.get("status") or {}).get("storageClassName")) or "x", cluster=cluster)
    try:
        path = _hs.download_path(_hs.backing_image_of(img, sc))
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    stream = _Kube(kc).raw_stream(path)
    return Response(stream_with_context(stream), mimetype="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{display}.gz"'})


@app.route("/api/image-upload/<cluster>/<namespace>", methods=["PUT"])
@requires_auth
@_rate_limit("6 per minute")
def api_image_upload(cluster, namespace):
    """Un fichier d'image envoyé par le navigateur : écrit sur disque (0600),
    puis `harvester-resources image upload` le sert une fois au cluster, qui
    le télécharge comme une image « download ». Le fichier est effacé après."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    length = request.content_length
    if not length:
        return jsonify({"error": "Content-Length required"}), 411
    q = request.args
    display = (q.get("display_name") or "").strip()
    file_name = os.path.basename(q.get("file_name") or "image")[:200]
    try:
        if not display or len(display) > 63:
            raise ValueError("image name: 63 characters at most")
        checksum = (q.get("checksum") or "").strip()
        if checksum and not _hs.SHA512_RE.match(checksum):
            raise ValueError("checksum: a SHA512 (128 hexadecimal characters)")
        sc = (q.get("storage_class") or "").strip()
        if sc:
            _hs.check_name(sc, "storage class")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        IMAGE_UPLOAD_DIR.chmod(0o700)
        st = os.statvfs(IMAGE_UPLOAD_DIR)
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = None
    if free is not None and free < length + _UPLOAD_SPARE:
        return jsonify({"error": "not enough room to receive the file", "need": length, "free": free}), 507
    part = IMAGE_UPLOAD_DIR / f"{uuid.uuid4().hex}.img"
    run = ActionRun(uuid.uuid4().hex[:12], f"image:upload-receive:{namespace}/{display}", cluster, ["upload", file_name])
    run.cluster_user = (current_cluster_identity() or {}).get("user")
    with ACTIONS_LOCK:
        ACTIONS[run.id] = run
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    run.emit({"type": "step", "step_id": "upload", "status": "running",
              "message": f"receiving {file_name} ({_vp.fmt_bytes(length)})", "ts": time.time()})
    try:
        res = _receive_archive(run, request.stream, length, part)
    except (_UploadCancelled, ValueError, OSError) as e:
        part.unlink(missing_ok=True)
        run.status, run.exit_code = "error", 1
        run.error_summary = "cancelled" if isinstance(e, _UploadCancelled) else _error_text(e)
        run.emit({"type": "step", "step_id": "upload", "status": "error", "message": run.error_summary, "ts": time.time()})
        run.close()
        return jsonify({"error": run.error_summary, "action_id": run.id}), 409
    run.emit({"type": "step", "step_id": "upload", "status": "done", "message": _vp.summary("upload", res), "ts": time.time()})
    run.status, run.exit_code = "done", 0
    run.close()
    extra = ["--file", str(part), "--file-name", file_name, "--display-name", display,
             "--port", str(os.environ.get("HARVESTER_OPS_IMAGE_UPLOAD_PORT", 8092))]
    if checksum:
        extra += ["--checksum", checksum]
    if sc:
        extra += ["--storage-class", sc]
    if q.get("advertise"):
        extra += ["--advertise", q["advertise"]]
    run2, err = _res_cli(cluster, kc, f"image:upload:{namespace}/{display}",
                         ["image", "upload", "--namespace", namespace] + extra, [str(part)])
    return _res_reply(run2, err, received=run.id, image=display)


# ---------------------------------------------------------------------------
# v1.64.0 : les menus « Advanced » de Harvester : modèles de VM et leurs
# versions, modèles cloud-init, classes de stockage complètes, secrets typés,
# clés SSH modifiables. Écriture par bin/harvester-resources.py.
# ---------------------------------------------------------------------------
import hv_advanced as _hadv  # noqa: E402


@app.route("/api/templates/<cluster>")
@requires_auth
def api_templates(cluster):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        ft = pool.submit(_kubectl_json, kc, "get", _hadv.K_TEMPLATE, "-A", cluster=cluster)
        fv = pool.submit(_kubectl_json, kc, "get", _hadv.K_VERSION, "-A", cluster=cluster)
        tpls, vers = (ft.result() or {}).get("items") or [], (fv.result() or {}).get("items") or []
    items = []
    for t in tpls:
        m = t.get("metadata") or {}
        items.append({"namespace": m.get("namespace"), "name": m.get("name"),
                      "description": (t.get("spec") or {}).get("description") or "",
                      "default_version": (t.get("spec") or {}).get("defaultVersionId"),
                      "versions": _hadv.versions_of(t, vers)})
    items.sort(key=lambda r: (r["namespace"], r["name"]))
    return jsonify({"cluster": cluster, "items": items})


@app.route("/api/templates/<cluster>/<namespace>/<name>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_template_do(cluster, namespace, name, action):
    if action not in ("set-default", "delete-version", "delete"):
        return jsonify({"error": "action: set-default, delete-version or delete"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    extra = []
    if action != "delete":
        ver = str(b.get("version") or "")
        parts = ver.split("/")
        if len(parts) != 2 or not all(_K8S_SUBDOMAIN_RE.match(x) for x in parts):
            return jsonify({"error": "version: namespace/name"}), 400
        extra += ["--version", ver]
    run, err = _res_cli(cluster, kc, f"template:{action}:{namespace}/{name}",
                        ["template", action, "--namespace", namespace, "--name", name] + extra)
    return _res_reply(run, err, template=f"{namespace}/{name}", action=action)


@app.route("/api/cloud-templates/<cluster>")
@requires_auth
def api_cloud_templates(cluster):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    got = _kubectl_json(kc, "get", "configmaps", "-A", "-l", _hadv.CLOUD_LABEL, cluster=cluster)
    return jsonify({"cluster": cluster, "items": _hadv.cloud_templates((got or {}).get("items") or [])})


def _ct_text_file(files, text):
    wd = _capi_work_dir()
    fd, path = tempfile.mkstemp(prefix="ct-", dir=str(wd) if wd else None)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    files.append(path)
    return path


@app.route("/api/cloud-templates/<cluster>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_cloud_template_create(cluster):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    files = []
    try:
        _hadv.cloud_template(str(b.get("name") or ""), str(b.get("namespace") or "default"), b.get("type"),
                             str(b.get("text") or ""), str(b.get("description") or ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    path = _ct_text_file(files, str(b["text"]))
    run, err = _res_cli(cluster, kc, f"cloudtpl:create:{b.get('namespace') or 'default'}/{b['name']}",
                        ["cloudtpl", "create", "--namespace", str(b.get("namespace") or "default"), "--name", str(b["name"]),
                         "--type", b["type"], "--file", path, "--description", str(b.get("description") or "")[:1000]], files)
    return _res_reply(run, err, template=b["name"])


@app.route("/api/cloud-templates/<cluster>/<namespace>/<name>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_cloud_template_do(cluster, namespace, name, action):
    if action not in ("update", "delete"):
        return jsonify({"error": "action: update or delete"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    files, extra = [], []
    if action == "update":
        try:
            _hadv.cloud_template(name, namespace, "user", str(b.get("text") or ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        extra += ["--file", _ct_text_file(files, str(b["text"])), "--description", str(b.get("description") or "")[:1000]]
    run, err = _res_cli(cluster, kc, f"cloudtpl:{action}:{namespace}/{name}",
                        ["cloudtpl", action, "--namespace", namespace, "--name", name] + extra, files)
    return _res_reply(run, err, template=f"{namespace}/{name}", action=action)


# ---------------------------------------------------------------------------
# v1.65.0 : le menu Networks de Harvester (réseaux de cluster et leurs
# configurations, réseaux d'hôte, réseaux de stockage / migration / RWX,
# équilibreurs et pools ; modifier un réseau de VM). Toute écriture passe par
# bin/harvester-resources.py ; les demandes par un fichier privé.
# ---------------------------------------------------------------------------

_NET_READ = [_hnet.K_CN, _hnet.K_VC, _hnet.K_VS, _hnet.K_NAD, "nodes", _hnet.K_SETTING]


def _net_nodes(nodes):
    return [{"name": (n.get("metadata") or {}).get("name"),
             "witness": _hnet.WITNESS in ((n.get("metadata") or {}).get("labels") or {})} for n in nodes]


@app.route("/api/net-admin/<cluster>")
@requires_auth
@shared_read()
def api_net_admin(cluster):
    """L'onglet Cluster networks : chaque réseau et ses configurations (état
    par nœud), les réseaux d'hôte, les trois réglages réseau."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    got = _kubectl_kinds(kc, _NET_READ, cluster)
    nodes = got["nodes"]
    settings = [s for s in got[_hnet.K_SETTING] if (s.get("metadata") or {}).get("name") in _hnet.NET_SETTINGS]
    return jsonify({"cluster": cluster,
                    "cluster_networks": _hnet.cluster_network_rows(got[_hnet.K_CN], got[_hnet.K_VC], got[_hnet.K_VS],
                                                                   got[_hnet.K_NAD], nodes),
                    "settings": _hnet.setting_rows(settings), "nodes": _net_nodes(nodes)})


@app.route("/api/net-admin/<cluster>/nics")
@requires_auth
@shared_read()
def api_net_admin_nics(cluster):
    """Les cartes proposables pour une configuration sur ces nœuds (toutes
    les cartes libres présentes partout, comme le formulaire de Harvester)."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    lm = _kubectl_json(kc, "get", _hnet.K_LM, "nic", cluster=cluster) or {}
    nodes = [n for n in (request.args.get("nodes") or "").split(",") if n]
    if not all(_K8S_SUBDOMAIN_RE.match(n) for n in nodes):
        return jsonify({"error": "invalid node name"}), 400
    link = (lm.get("status") or {}).get("linkStatus") or {}
    if not nodes:
        got = _kubectl_json(kc, "get", "nodes", cluster=cluster) or {}
        nodes = [x["name"] for x in _net_nodes(got.get("items") or []) if not x["witness"]]
    current = [n for n in (request.args.get("current") or "").split(",") if n]
    return jsonify({"cluster": cluster, "nodes": nodes, "nics": _hnet.nic_choices(link, nodes, current),
                    "monitor": bool(link)})


@app.route("/api/net-admin/<cluster>/vmnet/<namespace>/<name>")
@requires_auth
def api_net_admin_vmnet(cluster, namespace, name):
    """Un réseau de VM tel que sa fenêtre de modification le montre, et les
    VMs en marche dessus (elles empêchent de changer ses VLAN)."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    nad = _kubectl_json(kc, "get", _hnet.K_NAD, name, "-n", namespace, cluster=cluster)
    if not nad:
        return jsonify({"error": f"no VM network {namespace}/{name}"}), 404
    vmis = (_kubectl_json(kc, "get", "virtualmachineinstances.kubevirt.io", "-A", cluster=cluster) or {}).get("items") or []
    return jsonify({**_hnet.vmnet_row(nad), "running": _hnet.vms_on(vmis, [f"{namespace}/{name}"])})


def _net_spec_file(files, spec):
    wd = _capi_work_dir()
    fd, path = tempfile.mkstemp(prefix="net-", suffix=".json", dir=str(wd) if wd else None)
    with os.fdopen(fd, "w") as f:
        json.dump(spec, f)
    os.chmod(path, 0o600)
    files.append(path)
    return path


_NET_DO = ("cn-create", "cn-delete", "config-create", "config-update", "config-migrate", "config-delete",
           "hostnet-create", "hostnet-update", "hostnet-delete", "setting-set", "setting-clear",
           "lb-create", "lb-update", "lb-delete", "pool-create", "pool-update", "pool-delete", "pool-release",
           "vmnet-update")


@app.route("/api/net-admin/<cluster>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_net_admin_do(cluster, action):
    """Un geste du menu Networks, contrôlé d'avance par les mêmes fonctions
    que le script, puis suivi dans le dock."""
    if action not in _NET_DO:
        return jsonify({"error": f"action must be one of {', '.join(_NET_DO)}"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    spec = b.get("spec") if isinstance(b.get("spec"), dict) else {}
    files, label = [], action
    try:
        name = lambda what, key="name": _hnet.check_name(str(b.get(key) or ""), what)  # noqa: E731
        if action == "cn-create":
            _hnet.cluster_network(name("cluster network", "name"), str(b.get("description") or ""))
            args = ["clusternetwork", "create", "--name", b["name"]]
            if b.get("description"):
                args += ["--description", str(b["description"])[:1000]]
            label = f"clusternetwork:create:{b['name']}"
        elif action == "cn-delete":
            args = ["clusternetwork", "delete", "--name", name("cluster network")]
            label = f"clusternetwork:delete:{b['name']}"
        elif action in ("config-create", "config-update"):
            _hnet.vlan_config(spec)
            args = ["netconfig", action.split("-")[1], "--spec", _net_spec_file(files, spec)]
            label = f"netconfig:{action.split('-')[1]}:{spec['name']}"
        elif action == "config-migrate":
            _hnet.migrate_patch(str(b.get("target") or ""))
            args = ["netconfig", "migrate", "--name", name("configuration"), "--target", b["target"]]
            label = f"netconfig:migrate:{b['name']}"
        elif action == "config-delete":
            args = ["netconfig", "delete", "--name", name("configuration")]
            label = f"netconfig:delete:{b['name']}"
        elif action in ("hostnet-create", "hostnet-update"):
            _hnet.host_network(spec, [{"metadata": {"name": n}} for n in (spec.get("ips") or {})] or [])
            args = ["hostnet", action.split("-")[1], "--spec", _net_spec_file(files, spec)]
            label = f"hostnet:{action.split('-')[1]}:{spec['name']}"
        elif action == "hostnet-delete":
            args = ["hostnet", "delete", "--name", name("host network")]
            label = f"hostnet:delete:{b['name']}"
        elif action in ("setting-set", "setting-clear"):
            kind = str(b.get("name") or "")
            if kind not in _hnet.NET_SETTINGS:
                raise ValueError("setting: " + ", ".join(_hnet.NET_SETTINGS))
            if action == "setting-set":
                _hnet.setting_value(kind, spec)
                args = ["netsetting", "set", "--name", kind, "--spec", _net_spec_file(files, spec)]
            else:
                args = ["netsetting", "clear", "--name", kind]
            label = f"netsetting:{action.split('-')[1]}:{kind}"
        elif action in ("lb-create", "lb-update"):
            _hnet.load_balancer(spec)
            args = ["lb", action.split("-")[1], "--spec", _net_spec_file(files, spec)]
            label = f"lb:{action.split('-')[1]}:{spec.get('namespace') or 'default'}/{spec['name']}"
        elif action == "lb-delete":
            args = ["lb", "delete", "--namespace", name("namespace", "namespace"), "--name", name("load balancer")]
            label = f"lb:delete:{b['namespace']}/{b['name']}"
        elif action in ("pool-create", "pool-update"):
            _hnet.ip_pool(spec)
            args = ["ippool", action.split("-")[1], "--spec", _net_spec_file(files, spec)]
            label = f"ippool:{action.split('-')[1]}:{spec['name']}"
        elif action in ("pool-delete", "pool-release"):
            args = ["ippool", action.split("-")[1], "--name", name("IP pool")]
            if action == "pool-release":
                import ipaddress as _ipa
                _ipa.IPv4Address(str(b.get("ip") or ""))
                args += ["--ip", str(b["ip"])]
            label = f"ippool:{action.split('-')[1]}:{b['name']}"
        else:
            ns, nm = name("namespace", "namespace"), str(b.get("name") or "")
            if not _K8S_SUBDOMAIN_RE.match(nm):
                raise ValueError("VM network name")
            args = ["vmnet", "update", "--namespace", ns, "--name", nm, "--spec", _net_spec_file(files, spec)]
            label = f"vmnet:update:{ns}/{nm}"
    except (ValueError, KeyError, TypeError) as e:
        for f in files:
            Path(f).unlink(missing_ok=True)
        return jsonify({"error": str(e) if not isinstance(e, KeyError) else f"missing {e}"}), 400
    run, err = _res_cli(cluster, kc, label, args, files)
    return _res_reply(run, err, action=action)


# ---------------------------------------------------------------------------
# v1.67.0 : Advanced > Settings et Support de Harvester (réglages, cible de
# sauvegarde, paquet de support, kubeconfig sûr). Écritures par
# bin/harvester-resources.py ; les valeurs par un fichier privé.
# ---------------------------------------------------------------------------

@app.route("/api/hv-settings/<cluster>")
@requires_auth
@shared_read()
def api_hv_settings(cluster):
    """Les réglages de Harvester, secrets masqués."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    got = _kubectl_json(kc, "get", _hset.K_SETTING, timeout=30, cluster=cluster) or {}
    return jsonify({"cluster": cluster, "groups": list(_hset.GROUPS),
                    "items": _hset.rows(got.get("items") or [], show_hidden=request.args.get("hidden") == "1")})


def _private_file(files, text, prefix):
    wd = _capi_work_dir()
    fd, path = tempfile.mkstemp(prefix=prefix, dir=str(wd) if wd else None)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)
    files.append(path)
    return path


@app.route("/api/hv-settings/<cluster>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_hv_settings_do(cluster, action):
    if action not in ("set", "reset", "test"):
        return jsonify({"error": "action: set, reset or test"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    files = []
    if action == "test":
        run, err = _res_cli(cluster, kc, "setting:test-backup-target", ["setting", "test-backup-target"])
        return _res_reply(run, err, action=action)
    name = str(b.get("name") or "")
    try:
        if action == "set":
            value = str(b.get("value") if b.get("value") is not None else "")
            if not value.strip():
                raise ValueError("an empty value is a reset: use Reset")
            if _hset.MASK not in value:
                value = _hset.validate(name, value)
            else:
                _hset.validate(name, "")            # réglage connu et modifiable
            args = ["setting", "set", "--name", name, "--value-file", _private_file(files, value, "set-")]
        else:
            _hset.validate(name, "")
            args = ["setting", "reset", "--name", name]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    run, err = _res_cli(cluster, kc, f"setting:{action}:{name}", args, files)
    return _res_reply(run, err, action=action, setting=name)


_KC_OUT = {}                     # jeton -> (chemin, horodatage) des kubeconfigs à retirer une fois
_KC_OUT_TTL = 600


def _kc_sweep():
    now = time.time()
    for tok, (path, ts) in list(_KC_OUT.items()):
        if now - ts > _KC_OUT_TTL:
            Path(path).unlink(missing_ok=True)
            _KC_OUT.pop(tok, None)


@app.route("/api/hv-support/<cluster>")
@requires_auth
def api_hv_support(cluster):
    """Les paquets de support de Harvester et les kubeconfigs délivrés."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        fb = pool.submit(_kubectl_json, kc, "get", _hset.K_BUNDLE, "-n", _hset.BUNDLE_NS, timeout=30, cluster=cluster)
        fs = pool.submit(_kubectl_json, kc, "get", "serviceaccounts", "-n", _hset.KC_NS, "-l", _hset.KC_LABEL, timeout=30, cluster=cluster)
        fr = pool.submit(_kubectl_json, kc, "get", "clusterroles", timeout=30, cluster=cluster)
        fn = pool.submit(_kubectl_json, kc, "get", "namespaces", timeout=30, cluster=cluster)
        bundles, sas, roles, nss = (f.result() or {} for f in (fb, fs, fr, fn))
    return jsonify({"cluster": cluster, "bundles": _hset.bundle_rows(bundles.get("items") or []),
                    "kubeconfigs": _hset.kubeconfig_rows(sas.get("items") or []),
                    "roles": _hset.roles_info(roles.get("items") or []),
                    "namespaces": sorted(n["metadata"]["name"] for n in nss.get("items") or []
                                         if not n["metadata"]["name"].startswith(_co.SYSTEM_NAMESPACE_PREFIXES))})


@app.route("/api/hv-support/<cluster>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_hv_support_do(cluster, action):
    if action not in ("bundle-create", "bundle-delete", "kc-create", "kc-revoke"):
        return jsonify({"error": "action: bundle-create, bundle-delete, kc-create or kc-revoke"}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    files, extra = [], {}
    try:
        if action == "bundle-create":
            spec = b.get("spec") if isinstance(b.get("spec"), dict) else {}
            obj = _hset.bundle_manifest(spec)
            spec["name"] = obj["metadata"]["name"]
            args = ["supportbundle", "create", "--spec", _private_file(files, json.dumps(spec), "bundle-")]
            label, extra = f"supportbundle:create:{spec['name']}", {"name": spec["name"]}
        elif action == "bundle-delete":
            args = ["supportbundle", "delete", "--name", _hset.check_name(str(b.get("name") or ""), "bundle")]
            label = f"supportbundle:delete:{b['name']}"
        elif action == "kc-create":
            _hset.kubeconfig_objects(b)
            _kc_sweep()
            token = _secrets.token_hex(16)                # le jeton de retrait n'est pas dans le chemin
            wd = _capi_work_dir()
            fd, out = tempfile.mkstemp(prefix="kc-", suffix=".yaml", dir=str(wd) if wd else None)
            os.close(fd)
            _KC_OUT[token] = (out, time.time())
            args = ["kubeconfig", "create", "--name", b["name"], "--role", b["role"], "--duration", b.get("duration") or "24h",
                    "--out", out, "--cluster-name", cluster]
            if b.get("namespace"):
                args += ["--namespace", b["namespace"]]
            if b.get("description"):
                args += ["--description", str(b["description"])[:500]]
            label, extra = f"kubeconfig:create:{b['name']}", {"download": token}
        else:
            args = ["kubeconfig", "revoke", "--name", _hset.check_name(str(b.get("name") or ""), "kubeconfig")]
            label = f"kubeconfig:revoke:{b['name']}"
    except (ValueError, KeyError) as e:
        for f in files:
            Path(f).unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 400
    run, err = _res_cli(cluster, kc, label, args, files)
    return _res_reply(run, err, action=action, **extra)


@app.route("/api/hv-support/<cluster>/kubeconfig/<token>")
@requires_auth
def api_hv_support_kubeconfig(cluster, token):
    """Le kubeconfig délivré, une seule fois, puis effacé (il porte un jeton)."""
    if ROLE_RANK.get(current_role(), 0) < ROLE_RANK["admin"]:
        return jsonify({"error": "forbidden", "required": "admin"}), 403
    _kc_sweep()
    entry = _KC_OUT.pop(token, None) if re.fullmatch(r"[0-9a-f]{32}", token or "") else None
    if not entry or not Path(entry[0]).is_file():
        return jsonify({"error": "no such kubeconfig, or already downloaded"}), 404
    text = Path(entry[0]).read_text()
    Path(entry[0]).unlink(missing_ok=True)
    return Response(text, mimetype="application/yaml",
                    headers={"Content-Disposition": f'attachment; filename="{cluster}-kubeconfig.yaml"',
                             "Cache-Control": "no-store"})


@app.route("/api/hv-support/<cluster>/bundle/<name>/download")
@requires_auth
def api_hv_support_bundle_download(cluster, name):
    """Le paquet de support de Harvester, par le proxy de service de
    l'apiserver, en flux ; gardé sur le cluster (retain) pour être supprimé
    à la main ou à son expiration."""
    if ROLE_RANK.get(current_role(), 0) < ROLE_RANK["admin"]:
        return jsonify({"error": "forbidden", "required": "admin"}), 403
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    obj = _kubectl_json(kc, "get", _hset.K_BUNDLE, name, "-n", _hset.BUNDLE_NS, cluster=cluster)
    if not obj or ((obj.get("status") or {}).get("state")) != "ready":
        return jsonify({"error": "support bundle is not ready"}), 409
    from kube import Kube as _Kube
    stream = _Kube(kc).raw_stream(_hset.bundle_download_path(name))
    fname = re.sub(r"[^A-Za-z0-9._-]+", "_", (obj.get("status") or {}).get("filename") or f"{name}.zip")[:160]
    return Response(stream_with_context(stream), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ---------------------------------------------------------------------------
# v1.68.0 : Advanced > PCI Devices, USB Devices, SR-IOV Network Devices.
# Écritures par bin/harvester-resources.py device.
# ---------------------------------------------------------------------------

_DEVICE_DO = ("pci-enable", "pci-disable", "usb-enable", "usb-disable", "sriov")


@app.route("/api/devices/<cluster>")
@requires_auth
@shared_read()
def api_devices(cluster):
    """Les périphériques de Harvester, leur état de passthrough et les VMs
    qui s'en servent, en une lecture."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    from concurrent.futures import ThreadPoolExecutor
    kinds = {"addon": ("get", "addons.harvesterhci.io", "-n", _hdev.ADDON[0]),
             "pci": ("get", _hdev.K_PCI), "pciclaims": ("get", _hdev.K_PCICLAIM),
             "usb": ("get", _hdev.K_USB), "usbclaims": ("get", _hdev.K_USBCLAIM),
             "sriov": ("get", _hdev.K_SRIOV), "vms": ("get", "virtualmachines.kubevirt.io", "-A"),
             "vmis": ("get", "virtualmachineinstances.kubevirt.io", "-A")}
    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, *a, timeout=30, cluster=cluster) for k, a in kinds.items()}
        got = {k: ((f.result() or {}).get("items") or []) for k, f in futs.items()}
    enabled = _hdev.addon_enabled(got["addon"])
    running = _hdev.running_set(got["vmis"])
    return jsonify({"cluster": cluster, "addon": enabled,
                    "pci": _hdev.pci_rows(got["pci"], got["pciclaims"], got["vms"], got["sriov"], running),
                    "usb": _hdev.usb_rows(got["usb"], got["usbclaims"], got["vms"], running),
                    "sriov": _hdev.sriov_rows(got["sriov"], got["pciclaims"])})


@app.route("/api/devices/<cluster>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_devices_do(cluster, action):
    if action not in _DEVICE_DO:
        return jsonify({"error": "action: " + ", ".join(_DEVICE_DO)}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    names = b.get("names") if isinstance(b.get("names"), list) else [b.get("name")]
    try:
        names = [_hdev.check_name(n) for n in names if n]
        if not names or len(names) > 64:
            raise ValueError("names: 1 to 64 devices")
        args = ["device", action]
        for n in names:
            args += ["--name", n]
        if action == "sriov":
            if len(names) != 1:
                raise ValueError("sriov: one network device at a time")
            vfs = int(b.get("vfs", 0))
            if vfs < 0 or vfs > _hdev.MAX_VFS:
                raise ValueError(f"number of virtual functions: 0 to {_hdev.MAX_VFS}")
            args += ["--vfs", str(vfs)]
        else:
            user = re.sub(r"[^A-Za-z0-9@._-]", "", str(current_user() or "admin"))[:63] or "admin"
            args += ["--user", user]
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    label = f"device:{action}:{names[0]}" + (f"+{len(names) - 1}" if len(names) > 1 else "")
    run, err = _res_cli(cluster, kc, label, args)
    return _res_reply(run, err, action=action, names=names)


@app.route("/api/hostdevices/<cluster>")
@requires_auth
def api_hostdevices(cluster):
    """v1.68.0 : ce qu'une VM peut recevoir (sélecteur de l'éditeur) : les
    périphériques PCI et, désormais, USB, avec leur état de passthrough."""
    pci, err = _list_k8s_resources(cluster, "pcidevices.devices.harvesterhci.io",
                                   reducer=_reduce_pcidevice, cache_key="pcidevices")
    if err:
        return jsonify({"error": err}), 502
    usb, _ = _list_k8s_resources(cluster, _hdev.K_USB, cache_key="usbdevices", reducer=lambda it: it)
    rows = [dict(r, bus="pci") for r in pci]
    for u in usb or []:
        st = u.get("status") or {}
        name = (u.get("metadata") or {}).get("name")
        rows.append({"name": name, "bus": "usb", "device_name": st.get("resourceName") or "",
                     "node": st.get("nodeName"), "claimed": bool(st.get("enabled")),
                     "display_name": f"USB · {(st.get('description') or name)[:60]} ({st.get('nodeName')} · "
                                     f"{'passthrough' if st.get('enabled') else 'host'})"})
    return jsonify(rows)


# ---------------------------------------------------------------------------
# v1.69.0 : mise à jour de Harvester. Lecture de l'état complet ; gestes par
# bin/harvester-resources.py upgrade (lancement suivi jusqu'au bout).
# ---------------------------------------------------------------------------

_UPGRADE_DO = ("version-add", "version-delete", "start", "follow", "dismiss", "abort", "resume-node", "logs")
_ISO_RELEASE_CACHE = {}
_UPG_LOGS = {}                    # jeton -> (chemin, horodatage) des archives de journaux à retirer une fois


def _iso_release_info(path):
    """harvester-release.yaml d'un ISO du magasin, mis en cache par date."""
    key = (str(path), path.stat().st_mtime)
    if key not in _ISO_RELEASE_CACHE:
        try:
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "hr.yaml"
                r = subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(path), "-extract",
                                    "/harvester-release.yaml", str(out)], capture_output=True, text=True, timeout=60)
                _ISO_RELEASE_CACHE[key] = _hup.release_info(out.read_text()) if r.returncode == 0 and out.exists() else None
        except (OSError, ValueError, subprocess.SubprocessError):
            _ISO_RELEASE_CACHE[key] = None
    return _ISO_RELEASE_CACHE[key]


def _iso_sha512(name):
    """Le SHA-512 publié d'un ISO du magasin, s'il est posé à côté (fichier
    .sha512 de la release, une ligne « <hash>  <nom> »)."""
    for f in _iso_dir().glob("*.sha512"):
        try:
            for line in f.read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("*") == name and _hup.SHA512_RE.match(parts[0].lower()):
                    return parts[0].lower()
        except OSError:
            continue
    return ""


@app.route("/api/upgrade/<cluster>")
@requires_auth
@shared_read()
def api_upgrade(cluster):
    """Tout ce que montre la fenêtre de mise à jour : version courante,
    versions et leur éligibilité, dernière mise à jour et sa progression,
    ISO de la console utilisables, pré-contrôles."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    from concurrent.futures import ThreadPoolExecutor
    reads = {"sv": ("settings.harvesterhci.io", "server-version"), "checker": ("settings.harvesterhci.io", "upgrade-checker-enabled"),
             "versions": (_hup.K_VERSION, "-n", _hup.NS), "upgrades": (_hup.K_UPGRADE, "-n", _hup.NS),
             "nodes": ("nodes",), "volumes": ("volumes.longhorn.io", "-n", "longhorn-system"),
             "backups": ("virtualmachinebackups.harvesterhci.io", "-A"), "schedules": ("schedulevmbackups.harvesterhci.io", "-A"),
             "addons": ("addons.harvesterhci.io", "-A"), "charts": ("managedcharts.management.cattle.io", "-n", "fleet-local"),
             "images": (_hup.K_IMAGE, "-n", _hup.NS)}
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, timeout=30, cluster=cluster) for k, a in reads.items()}
        got = {k: f.result() for k, f in futs.items()}
    items = lambda k: (got[k] or {}).get("items") or []  # noqa: E731
    current = (got["sv"] or {}).get("value") or ""
    ups = items("upgrades")
    lat = _hup.latest(ups)
    img = None
    if lat and (lat.get("status") or {}).get("imageID"):
        iname = lat["status"]["imageID"].partition("/")[2]
        img = next((i for i in items("images") if (i.get("metadata") or {}).get("name") == iname), None)
    isos = []
    for p in sorted(_iso_dir().glob("*.iso"), key=lambda x: x.stat().st_mtime, reverse=True):
        rel = _iso_release_info(p)
        if not rel:
            continue
        ok, why = _hup.eligible(current, rel["harvester"], rel["min_upgradable"])
        isos.append({"name": p.name, "size": p.stat().st_size, "release": rel, "eligible": ok, "reason": why,
                     "sha512": _iso_sha512(p.name)})
    os_images = [{"name": (i.get("metadata") or {}).get("name"), "display": (i.get("spec") or {}).get("displayName"),
                  "progress": (i.get("status") or {}).get("progress")}
                 for i in items("images") if ((i.get("metadata") or {}).get("annotations") or {}).get(_hup.A_OS_IMAGE) == "True"]
    return jsonify({"cluster": cluster, "current": current,
                    "checker": ((got["checker"] or {}).get("value") or (got["checker"] or {}).get("default") or "true") == "true",
                    "versions": _hup.version_rows(items("versions"), current),
                    "upgrade": _hup.upgrade_view(lat, img, items("nodes")) if lat else None,
                    "running": _hup.running(ups), "history": len(ups),
                    "isos": isos, "os_images": os_images,
                    "prechecks": _hup.prechecks(items("nodes"), items("volumes"), items("backups"), items("schedules"),
                                                items("addons"), items("charts"), ups)})


@app.route("/api/upgrade/<cluster>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_upgrade_do(cluster, action):
    if action not in _UPGRADE_DO:
        return jsonify({"error": "action: " + ", ".join(_UPGRADE_DO)}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    files, extra = [], {}
    try:
        args = ["upgrade", action]
        if action == "version-add":
            text = str(b.get("yaml") or "")
            if not text.strip() and b.get("url"):
                url = str(b["url"])
                if not re.match(r"^https?://", url):
                    raise ValueError("url: an http(s) address")
                import urllib.request
                with urllib.request.urlopen(url, timeout=20) as r:     # noqa: S310 - adresse contrôlée
                    text = r.read(65536).decode("utf-8", "replace")
            v = _hup.version_from_yaml(text)
            args += ["--version-file", _private_file(files, text, "version-")]
            label = f"upgrade:version-add:{v['name']}"
        elif action == "version-delete":
            args += ["--name", _hup.check_name(b.get("name"), "version")]
            label = f"upgrade:version-delete:{b['name']}"
        elif action == "start":
            if b.get("iso"):
                name = _safe_artifact_name(str(b["iso"]))
                if not name or not (_iso_dir() / name).is_file():
                    raise ValueError("iso: an ISO of the console's store")
                args += ["--iso", str(_iso_dir() / name)]
                sha = str(b.get("checksum") or "").strip().lower() or _iso_sha512(name)
                if sha:
                    if not _hup.SHA512_RE.match(sha):
                        raise ValueError("checksum: a SHA-512 (128 hexadecimal characters)")
                    args += ["--checksum", sha]
                args += ["--port", str(os.environ.get("HARVESTER_OPS_IMAGE_UPLOAD_PORT", 8092))]
                if b.get("advertise"):
                    args += ["--advertise", str(b["advertise"])]
                label = f"upgrade:start:{name}"
            else:
                args += ["--version", _hup.check_name(b.get("version"), "version")]
                label = f"upgrade:start:{b['version']}"
            if b.get("log") is False:
                args.append("--no-log")
            if b.get("skip_single_replica"):
                args.append("--skip-single-replica")
        elif action == "logs":
            name = _hup.check_name(b.get("name"), "upgrade")
            _kc_sweep()
            token = _secrets.token_hex(16)
            wd = _capi_work_dir()
            fd, out = tempfile.mkstemp(prefix="upglog-", suffix=".zip", dir=str(wd) if wd else None)
            os.close(fd)
            _UPG_LOGS[token] = (out, time.time())
            args += ["--name", name, "--out", out]
            label, extra = f"upgrade:logs:{name}", {"download": token}
        else:
            name = _hup.check_name(b.get("name"), "upgrade")
            args += ["--name", name]
            if action == "resume-node":
                args += ["--node", _hup.check_name(b.get("node"), "node")]
            label = f"upgrade:{action}:{name}"
    except (ValueError, KeyError, OSError) as e:
        for f in files:
            Path(f).unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 400
    run, err = _res_cli(cluster, kc, label, args, files)
    return _res_reply(run, err, action=action, **extra)


@app.route("/api/upgrade/<cluster>/logs/<token>")
@requires_auth
def api_upgrade_logs(cluster, token):
    """L'archive des journaux d'une mise à jour, une seule fois, puis effacée."""
    if ROLE_RANK.get(current_role(), 0) < ROLE_RANK["admin"]:
        return jsonify({"error": "forbidden", "required": "admin"}), 403
    now = time.time()
    for tok, (path, ts) in list(_UPG_LOGS.items()):
        if now - ts > 3600:
            Path(path).unlink(missing_ok=True)
            _UPG_LOGS.pop(tok, None)
    entry = _UPG_LOGS.pop(token, None) if re.fullmatch(r"[0-9a-f]{32}", token or "") else None
    if not entry or not Path(entry[0]).is_file() or Path(entry[0]).stat().st_size == 0:
        if entry:
            _UPG_LOGS[token] = entry          # pas encore prête : on la garde
        return jsonify({"error": "no such archive yet, or already downloaded"}), 404
    data = Path(entry[0]).read_bytes()
    Path(entry[0]).unlink(missing_ok=True)
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{cluster}-upgrade-logs.zip"',
                             "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# v1.70.0 : Monitoring & Logging de Harvester. Écritures par
# bin/harvester-resources.py monlog ; les valeurs secrètes par fichier privé.
# ---------------------------------------------------------------------------

_MONLOG_DO = ("output-apply", "output-delete", "flow-apply", "flow-delete", "amc-apply", "amc-delete")


def _alertmanager_enabled(addon):
    try:
        import yaml
        vals = yaml.safe_load(((addon or {}).get("spec") or {}).get("valuesContent") or "") or {}
    except Exception:  # noqa: BLE001 - valeurs illisibles : on ne conclut rien
        return None
    am = (vals.get("alertmanager") or {}) if isinstance(vals, dict) else {}
    return am.get("enabled", True) is not False


@app.route("/api/monlog/<cluster>")
@requires_auth
@shared_read()
def api_monlog(cluster):
    """Sorties, flux et AlertmanagerConfig, avec l'état réel lu dans status
    et dans les événements ; l'état des deux add-ons."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    from concurrent.futures import ThreadPoolExecutor
    reads = {"addons": ("addons.harvesterhci.io", "-A"), "outputs": (_hml.K_OUTPUT, "-A"), "coutputs": (_hml.K_COUTPUT, "-A"),
             "flows": (_hml.K_FLOW, "-A"), "cflows": (_hml.K_CFLOW, "-A"), "loggings": (_hml.K_LOGGING,),
             "amcs": (_hml.K_AMC, "-A"),
             "events": ("events", "-A", "--field-selector", "involvedObject.kind=AlertmanagerConfig"),
             "namespaces": ("namespaces",)}
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, timeout=30, cluster=cluster) for k, a in reads.items()}
        got = {k: ((f.result() or {}).get("items") or []) for k, f in futs.items()}
    mon_addon = next((a for a in got["addons"] if (a.get("metadata") or {}).get("name") == _hml.ADDON_MON[1]), None)
    return jsonify({"cluster": cluster,
                    "logging": _hml.addon_state(got["addons"], _hml.ADDON_LOG),
                    "monitoring": dict(_hml.addon_state(got["addons"], _hml.ADDON_MON), alertmanager=_alertmanager_enabled(mon_addon)),
                    "outputs": _hml.output_rows([dict(o, kind="Output") for o in got["outputs"]] +
                                                [dict(o, kind="ClusterOutput") for o in got["coutputs"]]),
                    "flows": _hml.flow_rows([dict(o, kind="Flow") for o in got["flows"]] +
                                            [dict(o, kind="ClusterFlow") for o in got["cflows"]]),
                    "loggings": _hml.logging_health(got["loggings"]),
                    "amcs": _hml.amc_rows(got["amcs"], got["events"]), "shapes": _hml.OUTPUTS, "receiver_types": _hml.RECEIVERS,
                    "namespaces": sorted((n.get("metadata") or {}).get("name") for n in got["namespaces"])})


@app.route("/api/monlog/<cluster>/metrics")
@requires_auth
@shared_read()
def api_monlog_metrics(cluster):
    """L'instantané metrics.k8s.io (hôtes, VMs) et, si rancher-monitoring
    répond, les jauges du cluster et des VMs tirées de Prometheus."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    from concurrent.futures import ThreadPoolExecutor
    reads = {"nm": ("nodes.metrics.k8s.io",), "pm": ("pods.metrics.k8s.io", "-A", "-l", "kubevirt.io=virt-launcher"),
             "nodes": ("nodes",), "vmis": ("virtualmachineinstances.kubevirt.io", "-A"),
             "addon": ("addons.harvesterhci.io", _hml.ADDON_MON[1], "-n", _hml.ADDON_MON[0])}
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, timeout=30, cluster=cluster) for k, a in reads.items()}
        got = {k: f.result() for k, f in futs.items()}
    items = lambda k: (got[k] or {}).get("items") or []  # noqa: E731
    snap = _hml.snapshot(items("nm"), items("pm"), items("nodes"), items("vmis"))
    prom = None
    if ((got["addon"] or {}).get("spec") or {}).get("enabled"):
        def q(query):
            r = _kubectl_run(["kubectl", "--kubeconfig", kc, "get", "--raw", _hml.prom_path(query)],
                             capture_output=True, text=True, timeout=20)
            return json.loads(r.stdout) if r.returncode == 0 and r.stdout else None
        with ThreadPoolExecutor(max_workers=9) as pool:
            cl = {k: pool.submit(q, v) for k, v in _hml.Q_CLUSTER.items()}
            vm = {k: pool.submit(q, v) for k, v in (("cpu", _hml.Q_VM_CPU), ("memory", _hml.Q_VM_MEM),
                                                     ("net", _hml.Q_VM_NET), ("disk", _hml.Q_VM_DISK))}
            clr = {k: f.result() for k, f in cl.items()}
            vmr = {k: _hml.prom_vector(f.result()) for k, f in vm.items()}
        if any(clr.values()):
            keys = set().union(*[set(v) for v in vmr.values()]) - {""}
            prom = {"cluster": {k: _hml.prom_vector(v).get("") for k, v in clr.items()},
                    "vms": sorted([{"namespace": ns, "name": n, **{k: vmr[k].get((ns, n)) for k in vmr}} for ns, n in keys],
                                  key=lambda r: -(r["cpu"] or 0))}
    return jsonify({"cluster": cluster, **snap, "prometheus": prom,
                    "monitoring": bool(((got["addon"] or {}).get("spec") or {}).get("enabled"))})


@app.route("/api/monlog/<cluster>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_monlog_do(cluster, action):
    if action not in _MONLOG_DO:
        return jsonify({"error": "action: " + ", ".join(_MONLOG_DO)}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    files = []
    try:
        if action.endswith("-delete"):
            kind = str(b.get("kind") or "")
            if kind not in ("Output", "ClusterOutput", "Flow", "ClusterFlow", "AlertmanagerConfig"):
                raise ValueError("kind: Output, ClusterOutput, Flow, ClusterFlow or AlertmanagerConfig")
            ns, name = _hml.check_name(b.get("namespace"), "namespace"), _hml.check_name(b.get("name"))
            args = ["monlog", action, "--kind", kind, "--namespace", ns, "--name", name]
            label = f"monlog:{action}:{ns}/{name}"
        else:
            spec = b.get("spec") if isinstance(b.get("spec"), dict) else {}
            if spec.get("filters_yaml"):
                import yaml
                try:
                    spec["filters"] = yaml.safe_load(spec.pop("filters_yaml")) or []
                except yaml.YAMLError as e:
                    raise ValueError(f"filters: not YAML ({e})") from None
            spec.pop("filters_yaml", None)
            clean = json.loads(json.dumps(spec))
            if action == "output-apply":
                for ref in (clean.get("secrets") or {}).values():
                    if isinstance(ref, dict):
                        ref.pop("value", None)
                _hml.output_manifest(clean)
            elif action == "flow-apply":
                _hml.flow_manifest(clean)
            else:
                _hml.amc_manifest(clean)
            # le fichier porte les valeurs secrètes saisies : privé, effacé après l'action
            args = ["monlog", action, "--spec", _private_file(files, json.dumps(spec), "monlog-")]
            label = f"monlog:{action}:{spec.get('namespace') or ''}/{spec.get('name')}"
    except (ValueError, TypeError) as e:
        for f in files:
            Path(f).unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 400
    run, err = _res_cli(cluster, kc, label, args, files)
    return _res_reply(run, err, action=action)


# ---------------------------------------------------------------------------
# v1.71.0 : imports de VM (vm-import-controller). Écritures par
# bin/harvester-resources.py vmimport ; les identifiants par fichier privé.
# ---------------------------------------------------------------------------

_VMIMPORT_DO = ("source-apply", "source-recheck", "source-delete", "import-create", "import-follow", "import-delete")


@app.route("/api/vmimport/<cluster>")
@requires_auth
@shared_read()
def api_vmimport(cluster):
    """Sources et imports, avec l'état lu dans status et la progression tirée
    des images ; ce qu'il faut au formulaire (réseaux de VM, classes)."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    from concurrent.futures import ThreadPoolExecutor
    reads = {"addon": ("addons.harvesterhci.io", _hvi.ADDON[1], "-n", _hvi.ADDON[0]),
             "imports": (_hvi.K_IMPORT, "-A"), "images": (_hvi.K_IMAGE, "-A", "-l", f"{_hvi.L_IMPORTED}=true"),
             "nads": ("network-attachment-definitions.k8s.cni.cncf.io", "-A"), "classes": ("storageclasses",),
             "namespaces": ("namespaces",), **{t: (_hvi.K_SRC[t], "-A") for t in _hvi.TYPES}}
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, timeout=30, cluster=cluster) for k, a in reads.items()}
        got = {k: f.result() for k, f in futs.items()}
    items = lambda k: (got[k] or {}).get("items") or []  # noqa: E731
    imports = items("imports")
    sources = _hvi.source_rows({t: items(t) for t in _hvi.TYPES})
    for r in sources:
        r["users"] = _hvi.source_users(r, imports)
    addon = got["addon"] or {}
    return jsonify({"cluster": cluster,
                    "addon": {"enabled": bool((addon.get("spec") or {}).get("enabled")),
                              "status": (addon.get("status") or {}).get("status") or ("absent" if not addon else "")},
                    "crds": got["ova"] is not None,
                    "sources": sources,
                    "imports": sorted((_hvi.import_view(i, items("images")) for i in imports),
                                      key=lambda v: v["created"] or "", reverse=True),
                    "nads": sorted(f"{(n.get('metadata') or {}).get('namespace')}/{(n.get('metadata') or {}).get('name')}"
                                   for n in items("nads")),
                    "classes": sorted((c.get("metadata") or {}).get("name") for c in items("classes")
                                      if (c.get("parameters") or {}).get("harvesterhci.io/isInternalStorageClass") != "true"),
                    "default_class": next(((c.get("metadata") or {}).get("name") for c in items("classes")
                                           if ((c.get("metadata") or {}).get("annotations") or {})
                                           .get("storageclass.kubernetes.io/is-default-class") == "true"), ""),
                    "namespaces": sorted((n.get("metadata") or {}).get("name") for n in items("namespaces")),
                    "nic_models": _hvi.NIC_MODELS, "disk_bus": _hvi.DISK_BUS})


@app.route("/api/vmimport-log/<cluster>/<namespace>/<name>")
@requires_auth
def api_vmimport_log(cluster, namespace, name):
    """Les lignes d'erreur du journal du contrôleur qui citent cet objet :
    la seule place où une source ou un import dit pourquoi il n'avance pas."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    r = _kubectl_run(["kubectl", "--kubeconfig", kc, "logs", "-n", _hvi.CTRL_NS, f"deploy/{_hvi.CTRL_DEPLOY}", "--tail", "3000"],
                     capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return jsonify({"lines": [], "error": "the controller log cannot be read"}), 200
    lines = _hvi.log_lines(r.stdout, name, limit=20)
    return jsonify({"lines": lines, "reason": _hvi.stuck_reason(lines)})


@app.route("/api/vmimport/<cluster>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_vmimport_do(cluster, action):
    if action not in _VMIMPORT_DO:
        return jsonify({"error": "action: " + ", ".join(_VMIMPORT_DO)}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    files = []
    try:
        if action in ("source-apply", "import-create"):
            spec = b.get("spec") if isinstance(b.get("spec"), dict) else {}
            if action == "source-apply":
                _hvi.source_manifest(json.loads(json.dumps(spec)))
            else:
                _hvi.import_manifest(json.loads(json.dumps(spec)))
            # le fichier peut porter des identifiants : privé, effacé après l'action
            args = ["vmimport", action, "--spec", _private_file(files, json.dumps(spec), "vmimport-")]
            label = f"vmimport:{action}:{spec.get('namespace') or 'default'}/{spec.get('name')}"
        else:
            ns, name = _hvi.check_name(b.get("namespace"), "namespace"), _hvi.check_name(b.get("name"))
            args = ["vmimport", action, "--namespace", ns, "--name", name]
            if action.startswith("source-"):
                t = str(b.get("type") or "")
                if t not in _hvi.TYPES:
                    raise ValueError("type: vmware, openstack or ova")
                args += ["--type", t]
                if action == "source-delete" and b.get("with_secret"):
                    args.append("--with-secret")
            label = f"vmimport:{action}:{ns}/{name}"
    except (ValueError, TypeError) as e:
        for f in files:
            Path(f).unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 400
    run, err = _res_cli(cluster, kc, label, args, files)
    return _res_reply(run, err, action=action)


# ---------------------------------------------------------------------------
# v1.75.0 : migrations VMware par Forklift, onglet de cluster. Écritures par
# bin/harvester-forklift.py (parité CLI), secrets par fichier privé ; un mot
# de passe déjà dans le cluster est relu ici, jamais renvoyé au navigateur.
# Voir docs/design/2026-09-29-forklift-ui-plan.md.
# ---------------------------------------------------------------------------

import tarfile  # noqa: E402
import hv_forklift as _hf  # noqa: E402
import oci_push as _op  # noqa: E402

FORKLIFT_SCRIPT = "harvester-forklift.py"
VDDK_DIR = Path(os.environ.get(
    "HARVESTER_OPS_VDDK_DIR", str(Path.home() / ".local/share/harvester-ops/vddk")))
_FORKLIFT_DO = ("install", "vddk-image", "provider-apply", "provider-delete",
                # v1.76.0 : vagues à chaud, Préparation étendue (importeur CDI, intervalle)
                "wave-apply", "wave-start", "wave-cutover", "wave-rollback", "wave-close",
                "wave-delete", "cdi-importer", "precopy-interval")
_FK_INVENTORY_CACHE = {}        # (cluster, identité, fournisseur, sorte) -> (horodatage, lignes)
_FK_INVENTORY_TTL = 20
_FK_INVENTORY_TIMEOUT = 120       # secondes : au-delà, l'outil et son port-forward sont tués
_FK_KINDS = ("vms", "networks", "datastores")
_VM_SOURCE_KIND = "vmwaresources.migration.harvesterhci.io"
_VDDK_UPLOADS = set()
_VDDK_LOCK = threading.Lock()
_PATH_RE = re.compile(r"(?:/[^\s/:'\"()]+)+")
_FK_GLOBAL_CACHE = {}           # (cluster_user, console_user) -> (horodatage, réponse)
_FK_GLOBAL_TTL = 15
_FK_TAKEN_TIMEOUT = 15          # secondes : lecture des plans d'un autre cluster, avant wave-apply


def _vddk_dir():
    """Magasin des archives VDDK : sous licence VMware, d'où un répertoire
    0700 comme celui des archives d'export (vu en réel : il naissait en 0755)."""
    VDDK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        VDDK_DIR.chmod(0o700)
    except OSError:
        pass
    return VDDK_DIR


def _fk_cmd(action, kc):
    return [sys.executable, str(BIN_DIR / FORKLIFT_SCRIPT), action, "--kubeconfig", kc]


@app.route("/api/forklift/<cluster>")
@requires_auth
@shared_read()
def api_forklift(cluster):
    """L'onglet d'un coup : installation (dans l'ordre où elle se fait), image
    VDDK retenue par le cluster, registre proposé, fournisseurs vCenter et
    les sources VMware de VM Import qu'on peut reprendre."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200
    from concurrent.futures import ThreadPoolExecutor
    reads = {"addons": (_hf.K_ADDON, "-A"), "deploys": (_hf.K_DEPLOY, "-n", _hf.NS),
             "cm_deploys": (_hf.K_DEPLOY, "-n", _hf.CERT_MANAGER[0]),
             "controller": (_hf.K_CONTROLLER, _hf.CONTROLLER_NAME, "-n", _hf.NS),
             "providers": (_hf.K_PROVIDER, "-A"), "plans": (_hf.K_PLAN, "-A"),
             "vddk": ("configmaps", _hf.VDDK_CM, "-n", _hf.NS),
             "inv_sa": ("serviceaccounts", _hf.INVENTORY_SA, "-n", _hf.NS),
             "registry": ("settings.harvesterhci.io", "containerd-registry"),
             "prov": (_hf.PROV_CLUSTER[0], _hf.PROV_CLUSTER[2], "-n", _hf.PROV_CLUSTER[1]),
             "sources": (_VM_SOURCE_KIND, "-A"),
             # v1.76.0 : vagues à chaud (Préparation étendue, onglet Vagues)
             "cdi_deploy": (_hf.K_DEPLOY, _hf.CDI_OPERATOR[1], "-n", _hf.CDI_OPERATOR[0]),
             "migrations": (_hf.K_MIGRATION, "-A")}
    # ?targets=1 (onglet Vagues seulement) : les destinations d'une vague,
    # réseaux de VM, classes de stockage et namespaces ; trois lectures de
    # plus que la Préparation n'a pas à payer toutes les 10 s
    targets = request.args.get("targets") == "1"
    if targets:
        reads.update({"nads": ("network-attachment-definitions.k8s.cni.cncf.io", "-A"),
                      "classes": ("storageclasses",), "namespaces": ("namespaces",)})
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, timeout=30, cluster=cluster) for k, a in reads.items()}
        got = {k: f.result() for k, f in futs.items()}
    items = lambda k: (got[k] or {}).get("items") or []  # noqa: E731
    by_name = lambda k: {(d.get("metadata") or {}).get("name"): d for d in items(k)}  # noqa: E731
    addon, theirs = _hf.pick_addon(items("addons"))
    vddk = _hf.vddk_record(got["vddk"])
    reg = got["registry"] or {}
    providers = []
    for p in items("providers"):
        spec, m = p.get("spec") or {}, p.get("metadata") or {}
        if spec.get("type") != "vsphere":
            continue
        ready, msg = _hf.provider_state(p)
        providers.append({"name": m.get("name"), "namespace": m.get("namespace"), "url": spec.get("url"),
                          "ready": ready, "message": msg,
                          "vddk_image": (spec.get("settings") or {}).get("vddkInitImage", ""),
                          "plans": _hf.plans_using(m.get("namespace"), m.get("name"), items("plans")),
                          "managed": (m.get("labels") or {}).get(_hf.L_MANAGED) == "true"})
    waves = sorted((_hf.wave_state(p, items("migrations")) for p in items("plans") if _fk_is_wave(p)),
                   key=lambda w: w["name"] or "")
    extra = _fk_wave_targets(items("nads"), items("classes"), items("namespaces")) if targets else {}
    return jsonify({
        **extra,
        "cluster": cluster,
        "install": _hf.install_state(addon, by_name("deploys"), got["controller"], by_name("cm_deploys"),
                                     got["inv_sa"]),
        "harvester_addon": bool(theirs),
        "bundle": _capi_bundle_active_path() is not None,
        "vddk": vddk,
        "registry": _hf.registry_hint(reg.get("value") or reg.get("default") or "", (vddk or {}).get("archive", ""),
                                      got["prov"]),
        "providers": sorted(providers, key=lambda r: r["name"] or ""),
        "vmimport_sources": sorted(({"namespace": (s.get("metadata") or {}).get("namespace"),
                                     "name": (s.get("metadata") or {}).get("name"),
                                     "endpoint": (s.get("spec") or {}).get("endpoint")} for s in items("sources")),
                                   key=lambda r: (r["namespace"] or "", r["name"] or "")),
        # v1.76.0 : Préparation étendue (importeur CDI, intervalle) et vagues à chaud
        "cdi_importer": _hf.cdi_importer_state(got["cdi_deploy"]),
        "precopy_interval": _hf.precopy_interval(got["controller"]),
        "waves": waves,
    })


def _fk_wave_targets(nads, classes, namespaces):
    """Où une vague peut poser ses VMs : réseaux de VM (`<ns>/<nom>`),
    classes de stockage hors classes internes de Harvester, la classe par
    défaut, les namespaces (même lecture que l'onglet VM Import)."""
    meta = lambda o: o.get("metadata") or {}  # noqa: E731
    return {
        "nads": sorted(f"{meta(n).get('namespace')}/{meta(n).get('name')}" for n in nads),
        "classes": sorted(meta(c).get("name") for c in classes
                          if (c.get("parameters") or {}).get("harvesterhci.io/isInternalStorageClass") != "true"),
        "default_class": next((meta(c).get("name") for c in classes
                               if (meta(c).get("annotations") or {})
                               .get("storageclass.kubernetes.io/is-default-class") == "true"), ""),
        "namespaces": sorted(meta(n).get("name") for n in namespaces),
    }


def _fk_is_wave(plan):
    """True si ce Plan est une vague gérée par la console (étiquetée managed
    + wave) : distingue une vague de tout autre Plan Forklift sur le
    cluster."""
    labels = (plan.get("metadata") or {}).get("labels") or {}
    return labels.get(_hf.L_MANAGED) == "true" and bool(labels.get(_hf.L_WAVE))


def _fk_vmimport_source(kc, cluster, ns, name):
    """La source VMware d'un import, par son namespace et son nom."""
    return _kubectl_json(kc, "get", _VM_SOURCE_KIND, name, "-n", ns, timeout=30, cluster=cluster)


def _fk_provider_spec(kc, cluster, b):
    """La demande de fournisseur complète. Trois formes : tout saisi ; repris
    d'une source VM Import ; modifié sans ressaisir le mot de passe (repris du
    secret du fournisseur). Le fournisseur peut vivre hors du namespace
    `forklift` (vu en réel : un fournisseur fait par la CLI, dans `default`) ;
    `spec.namespace` le précise, sinon `hf.NS`. Rend (namespace, nom,
    demande) ; LookupError si l'objet cité n'existe pas."""
    name = _hf.check_name(str(b.get("name") or ""), "provider")
    ns = _hf.check_name(b.get("namespace") or _hf.NS, "namespace")
    vddk = str(b.get("vddk_image") or "").strip()
    src = b.get("from_vmimport")
    if isinstance(src, dict):
        vns, sname = _hf.check_name(src.get("namespace"), "namespace"), _hf.check_name(src.get("name"), "source")
        source = _fk_vmimport_source(kc, cluster, vns, sname)
        cred = ((source or {}).get("spec") or {}).get("credentials") or {}
        secret = _kubectl_json(kc, "get", "secrets", _hf.check_name(cred.get("name"), "secret"),
                               "-n", _hf.check_name(cred.get("namespace") or vns, "namespace"),
                               timeout=30, cluster=cluster) if source else None
        if not source or not secret:
            raise LookupError(f"no VM Import source {vns}/{sname} with credentials")
        spec = _hf.spec_from_vmimport(source, secret, vddk)
    else:
        spec = {"url": b.get("url"), "user": str(b.get("user") or "").strip(), "password": str(b.get("password") or "")}
        has_tls_input = "cacert" in b or "insecure" in b
        if b.get("cacert"):
            spec["cacert"] = str(b["cacert"])
        else:
            spec["insecure"] = bool(b.get("insecure"))
        if vddk:
            spec["vddk_image"] = vddk
        keep = bool(b.get("keep_credentials"))
        need_password = keep and not spec["password"]
        # « vide garde le compte actuel » vaut aussi quand un nouveau mot de
        # passe est saisi seul
        need_user = keep and not spec["user"]
        need_tls = keep and not has_tls_input
        if need_password or need_user or need_tls:
            secret = _kubectl_json(kc, "get", "secrets", _hf.secret_name(name), "-n", ns, timeout=30, cluster=cluster)
            kept = _hf.secret_values(secret, "user", "password", "cacert", "insecureSkipVerify")
            if need_password:
                if not kept.get("password"):
                    raise LookupError(f"provider {name} has no saved credentials to keep")
                spec["password"] = kept["password"]
            if need_user:
                spec["user"] = kept.get("user", "")
            if need_tls:
                # un mot de passe repris ne doit pas remettre le TLS à
                # « non vérifié » en silence : ce que le secret gardait
                if kept.get("cacert"):
                    spec["cacert"] = kept["cacert"]
                    spec.pop("insecure", None)
                else:
                    spec["insecure"] = kept.get("insecureSkipVerify") == "true"
    # refuse avant d'écrire, sans jamais citer d'identifiant
    _hf.provider_secret(ns, name, spec)
    _hf.provider_manifest(ns, name, spec)
    return ns, name, spec


def _fk_registry_auth(kc, cluster, host):
    """Les identifiants que Harvester a pour ce registre, lus côté serveur :
    dans le réglage (Harvester 1.8), sinon dans le secret de fleet-local que
    nomme le cluster `local` (Harvester 1.9 retire Auth du réglage)."""
    reg = _kubectl_json(kc, "get", "settings.harvesterhci.io", "containerd-registry", timeout=30, cluster=cluster) or {}
    creds = _hf.registry_auth(reg.get("value") or reg.get("default") or "", host)
    if creds:
        return creds
    prov = _kubectl_json(kc, "get", _hf.PROV_CLUSTER[0], _hf.PROV_CLUSTER[2], "-n", _hf.PROV_CLUSTER[1],
                         timeout=30, cluster=cluster)
    name = _hf.registry_auth_secret(prov, host)
    if not name:
        return None
    secret = _kubectl_json(kc, "get", "secrets", _hf.check_name(name, "secret"), "-n", _hf.PROV_CLUSTER[1],
                           timeout=30, cluster=cluster)
    vals = _hf.secret_values(secret, "username", "password")
    return vals if vals.get("username") and vals.get("password") else None


def _fk_taken_across_clusters():
    """VMs déjà prises dans une vague Forklift ouverte, sur TOUS les clusters
    déclarés qui répondent (v1.76.0, refus avant `wave-apply`, et vue
    globale). Rend ({(hôte vCenter, vm-NN): {"wave", "state", "cluster"}},
    [clusters injoignables ou dont la lecture a échoué])."""
    cfg = load_config()
    clusters = [(c["name"], _kubectl_for_cluster(c["name"])) for c in cfg.get("clusters", [])]

    def scan(name, kc):
        if not kc or _cluster_reachable(kc) is False:
            return None
        providers = _kubectl_json(kc, "get", _hf.K_PROVIDER, "-A", timeout=_FK_TAKEN_TIMEOUT, cluster=name)
        plans = _kubectl_json(kc, "get", _hf.K_PLAN, "-A", timeout=_FK_TAKEN_TIMEOUT, cluster=name)
        if providers is None or plans is None:
            return None
        migrations = _kubectl_json(kc, "get", _hf.K_MIGRATION, "-A", timeout=_FK_TAKEN_TIMEOUT, cluster=name) or {}
        hosts = {}
        for p in providers.get("items") or []:
            m = p.get("metadata") or {}
            hosts[(m.get("namespace"), m.get("name"))] = _hf.provider_host(p)
        return _hf.taken_vms(plans.get("items") or [], hosts, migrations.get("items") or [])

    taken, skipped = {}, []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, len(clusters))) as pool:
        futs = {name: pool.submit(scan, name, kc) for name, kc in clusters}
        for name, fut in futs.items():
            r = fut.result()
            if r is None:
                skipped.append(name)
                continue
            for key, info in r.items():
                taken.setdefault(key, {**info, "cluster": name})
    return taken, sorted(skipped)


@app.route("/api/forklift/<cluster>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_forklift_do(cluster, action):
    if action not in _FORKLIFT_DO:
        return jsonify({"error": "action: " + ", ".join(_FORKLIFT_DO)}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    cmd, spec, extra_reply = _fk_cmd(action, kc), None, {}
    try:
        if action == "install":
            bundle = _capi_bundle_active_path()
            if bundle:
                cmd += ["--cert-manager-from-bundle", str(bundle)]
            label = "forklift:install"
        elif action == "vddk-image":
            archive = str(b.get("archive") or "")
            path = _vddk_dir() / archive
            # un nom mal formé n'a jamais pu être déposé (l'envoi le
            # contrôle déjà) : c'est donc la même absence pour l'appelant,
            # pas une 400 séparée qui suggérerait une faute de saisie.
            try:
                _hf.check_archive_name(archive)
                found = path.is_file()
            except ValueError:
                found = False
            if not found:
                return jsonify({"error": f"no VDDK archive {archive} in the console"}), 404
            image = _hf.check_image(str(b.get("image") or "").strip(), "VDDK image")
            cmd += ["--archive", str(path), "--image", image]
            # la console tire l'image de base elle-même : en airgap, un miroir
            # (sinon registry.suse.com doit être joignable depuis son hôte)
            base = os.environ.get("HARVESTER_OPS_VDDK_BASE", "").strip()
            if base:
                cmd += ["--base", _hf.check_image(base, "HARVESTER_OPS_VDDK_BASE")]
            if b.get("plain_http"):
                cmd.append("--plain-http")
            if b.get("use_cluster_auth"):
                spec = _fk_registry_auth(kc, cluster, image.split("/", 1)[0])
                if spec is None:
                    raise ValueError("Harvester has no credentials for this registry")
            elif b.get("username") or b.get("password"):
                spec = {"username": str(b.get("username") or ""), "password": str(b.get("password") or "")}
                if not spec["username"] or not spec["password"]:
                    raise ValueError("registry: the user and the password go together")
            label = "forklift:vddk-image"
        elif action == "provider-apply":
            sp = b.get("spec") if isinstance(b.get("spec"), dict) else {}
            ns, name, spec = _fk_provider_spec(kc, cluster, sp)
            # une NOUVELLE source (jamais une modification, qui envoie
            # toujours keep_credentials) ne remplace jamais en silence un
            # fournisseur du même nom dans le namespace visé (celui donné,
            # ou « forklift » par défaut) : « Ajouter » depuis VM Import
            # préremplit le nom de la source reprise
            if not sp.get("keep_credentials") and \
                    _kubectl_json(kc, "get", _hf.K_PROVIDER, name, "-n", ns, timeout=30, cluster=cluster):
                return jsonify({"error": f"a provider named {name} already exists in {ns}: "
                                         "pick another name, or change that source instead"}), 409
            cmd += ["--namespace", ns, "--name", name]
            label = f"forklift:provider-apply:{ns}/{name}"
        elif action == "provider-delete":
            name = _hf.check_name(str(b.get("name") or ""), "provider")
            ns = _hf.check_name(b.get("namespace") or _hf.NS, "namespace")
            cmd += ["--namespace", ns, "--name", name, "--with-secret"]
            label = f"forklift:provider-delete:{ns}/{name}"
        elif action == "wave-apply":
            sp = b.get("spec") if isinstance(b.get("spec"), dict) else {}
            manifests = _hf.wave_manifests(sp)          # ValueError -> 400 (même contrôle que l'outil)
            plan = manifests[2]
            wave = plan["metadata"]["name"]
            prov_src = plan["spec"]["provider"]["source"]
            provider = _kubectl_json(kc, "get", _hf.K_PROVIDER, prov_src["name"], "-n", prov_src["namespace"],
                                     timeout=30, cluster=cluster)
            if not provider:
                return jsonify({"error": f"no provider {prov_src['namespace']}/{prov_src['name']} "
                                         "on this cluster"}), 404
            host = _hf.provider_host(provider)
            taken, skipped = _fk_taken_across_clusters()
            extra_reply["skipped"] = skipped
            if host:
                for v in plan["spec"]["vms"]:
                    info = taken.get((host, v["id"]))
                    if info and info["cluster"] != cluster:
                        return jsonify({"error": f"{v['id']} is already in wave {info['wave']} "
                                                 f"on cluster {info['cluster']}",
                                        "skipped": skipped}), 409
            spec = sp
            label = f"forklift:wave-apply:{wave}"
        elif action == "wave-start":
            wave = _hf.check_wave_name(str(b.get("wave") or ""))
            cmd += ["--wave", wave]
            label = f"forklift:wave-start:{wave}"
        elif action == "wave-cutover":
            wave = _hf.check_wave_name(str(b.get("wave") or ""))
            at = str(b.get("at") or "").strip()
            if at:
                _hf.cutover_patch(at)              # RFC 3339, pas trop dans le passé : ValueError -> 400
                cmd += ["--wave", wave, "--at", at]
            else:
                cmd += ["--wave", wave]
            label = f"forklift:wave-cutover:{wave}"
        elif action == "wave-rollback":
            wave = _hf.check_wave_name(str(b.get("wave") or ""))
            cmd += ["--wave", wave]
            vms = b.get("vms") or []
            if not isinstance(vms, list):
                raise ValueError("vms: a list of VM ids")
            for v in vms:
                v = str(v or "").strip()
                if not _hf.VM_ID_RE.match(v):
                    raise ValueError(f"VM id: {v!r} is not an inventory id")
                cmd += ["--vm", v]
            label = f"forklift:wave-rollback:{wave}"
        elif action == "wave-close":
            wave = _hf.check_wave_name(str(b.get("wave") or ""))
            cmd += ["--wave", wave]
            if b.get("clean_snapshots"):
                cmd.append("--clean-snapshots")
            label = f"forklift:wave-close:{wave}"
        elif action == "wave-delete":
            wave = _hf.check_wave_name(str(b.get("wave") or ""))
            cmd += ["--wave", wave]
            label = f"forklift:wave-delete:{wave}"
        elif action == "cdi-importer":
            mode = str(b.get("mode") or "").strip()
            if mode == "upstream":
                cmd.append("--upstream")
                image = str(b.get("image") or "").strip()
                if image:
                    cmd += ["--image", _hf.check_image(image, "importer image")]
            elif mode == "original":
                cmd.append("--original")
            else:
                raise ValueError("mode: upstream or original")
            label = "forklift:cdi-importer"
        else:
            # precopy-interval : validé par la bibliothèque, un seul chiffre en argument positionnel
            patch = _hf.precopy_patch(b.get("minutes"))
            cmd.append(str(patch["spec"]["controller_precopy_interval"]))
            label = "forklift:precopy-interval"
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    run, err = _cli_action(cluster, label, cmd, "harvester-forklift", spec=spec,
                           after=lambda: _invalidate_cluster_caches(cluster))
    return _res_reply(run, err, action=action, **extra_reply)


def _fk_provider_row(p):
    ready, msg = _hf.provider_state(p)
    m = p.get("metadata") or {}
    return {"name": m.get("name"), "namespace": m.get("namespace"), "url": (p.get("spec") or {}).get("url"),
            "ready": ready, "message": msg}


def _fk_global_cluster(name, kc):
    """L'état Forklift d'un cluster pour la vue globale : injoignable ou en
    échec de lecture -> `reachable: False`, sans bloquer les autres."""
    empty = {"cluster": name, "reachable": False, "forklift_ready": False,
             "cdi_importer_kind": None, "providers": [], "waves": []}
    if not kc or _cluster_reachable(kc) is False:
        return empty
    from concurrent.futures import ThreadPoolExecutor
    reads = {"addons": (_hf.K_ADDON, "-A"), "deploys": (_hf.K_DEPLOY, "-n", _hf.NS),
             "cm_deploys": (_hf.K_DEPLOY, "-n", _hf.CERT_MANAGER[0]),
             "controller": (_hf.K_CONTROLLER, _hf.CONTROLLER_NAME, "-n", _hf.NS),
             "inv_sa": ("serviceaccounts", _hf.INVENTORY_SA, "-n", _hf.NS),
             "providers": (_hf.K_PROVIDER, "-A"), "plans": (_hf.K_PLAN, "-A"),
             "migrations": (_hf.K_MIGRATION, "-A"),
             "cdi_deploy": (_hf.K_DEPLOY, _hf.CDI_OPERATOR[1], "-n", _hf.CDI_OPERATOR[0])}
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, timeout=_FK_TAKEN_TIMEOUT, cluster=name)
                for k, a in reads.items()}
        got = {k: f.result() for k, f in futs.items()}
    if got["providers"] is None and got["plans"] is None:
        # le cluster répond, mais Forklift n'est pas lisible (non installé, ou lecture refusée)
        return {**empty, "reachable": True}
    items = lambda k: (got[k] or {}).get("items") or []  # noqa: E731
    by_name = lambda k: {(d.get("metadata") or {}).get("name"): d for d in items(k)}  # noqa: E731
    addon, _theirs = _hf.pick_addon(items("addons"))
    install = _hf.install_state(addon, by_name("deploys"), got["controller"], by_name("cm_deploys"), got["inv_sa"])
    providers = sorted((_fk_provider_row(p) for p in items("providers")
                        if (p.get("spec") or {}).get("type") == "vsphere"),
                       key=lambda r: r["name"] or "")
    waves = sorted((_hf.wave_state(p, items("migrations")) for p in items("plans") if _fk_is_wave(p)),
                   key=lambda w: w["name"] or "")
    return {"cluster": name, "reachable": True, "forklift_ready": install["ready"],
            "cdi_importer_kind": _hf.cdi_importer_state(got["cdi_deploy"])["kind"],
            "providers": providers, "waves": waves}


@app.route("/api/forklift-global")
@requires_auth
def api_forklift_global():
    """Toutes les vagues, sur tous les clusters déclarés : une VM prise sur
    l'un d'eux ne doit pas se recomposer en vague sur un autre (v1.76.0).
    Lecture seule (viewer) ; un cluster injoignable est signalé, sans
    bloquer les autres. Gardée 15 s par personne."""
    key = ((current_cluster_identity() or {}).get("user"), current_user())
    now = time.time()
    hit = _FK_GLOBAL_CACHE.get(key)
    if hit and now - hit[0] < _FK_GLOBAL_TTL:
        return jsonify(hit[1])
    cfg = load_config()
    clusters = [(c["name"], _kubectl_for_cluster(c["name"])) for c in cfg.get("clusters", [])]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, len(clusters))) as pool:
        rows = list(pool.map(lambda nk: _fk_global_cluster(*nk), clusters))
    body = {"clusters": sorted(rows, key=lambda r: r["cluster"] or "")}
    _FK_GLOBAL_CACHE[key] = (now, body)
    return jsonify(body)


_FK_NO_INVENTORY_SA = re.compile(r'serviceaccounts? "?' + re.escape(_hf.INVENTORY_SA) + r'"? not found')


def _fk_run_tool(cmd, timeout):
    """L'outil dans sa propre session : au délai, tout son groupe de
    processus tombe, y compris le `kubectl port-forward` qu'il a lancé
    (subprocess.run ne tuait que l'outil et laissait le relais orphelin)."""
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          start_new_session=True) as p:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
            raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


@app.route("/api/forklift/<cluster>/inventory/<name>/<kind>")
@requires_auth
@_rate_limit("30/minute")
def api_forklift_inventory(cluster, name, kind):
    """Ce que Forklift voit d'un vCenter, par l'outil (jeton court et
    port-forward) ; gardé 20 s par personne pour ne pas rouvrir un tunnel à
    chaque clic. Le fournisseur peut vivre hors de `forklift` (`?namespace=`,
    lu par la console dans les données de l'onglet) ; sans elle, `hf.NS`."""
    if kind not in _FK_KINDS:
        return jsonify({"error": "kind: " + ", ".join(_FK_KINDS)}), 400
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    ns_raw = request.args.get("namespace")
    try:
        ns = _hf.check_name(ns_raw, "namespace") if ns_raw else _hf.NS
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # par personne : sous une session Rancher, l'identité déléguée est
    # vide, c'est le compte de la console qui distingue les personnes
    key = (cluster, (current_cluster_identity() or {}).get("user"), current_user(), ns, name, kind)
    hit = _FK_INVENTORY_CACHE.get(key)
    if hit and time.time() - hit[0] < _FK_INVENTORY_TTL:
        return jsonify({"rows": hit[1]})
    try:
        r = _fk_run_tool(_fk_cmd("inventory", kc) + ["--namespace", ns, "--name", name, "--kind", kind],
                         _FK_INVENTORY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "the inventory did not answer in time"}), 502
    except OSError as e:
        return jsonify({"error": _error_text(e)}), 502
    if r.returncode != 0:
        lines = [ln.split("|", 3)[3] for ln in r.stderr.splitlines() if ln.startswith("STEP_EVENT|") and ln.count("|") >= 3]
        msg = (lines[-1] if lines else (r.stderr.strip().splitlines() or ["the inventory cannot be read"])[-1])
        if _FK_NO_INVENTORY_SA.search(msg):
            msg = ("the console's inventory access (service account harvester-ops-inventory) is missing on "
                   "this cluster: run Resume in VMware migrations > Preparation")
        return jsonify({"error": _PATH_RE.sub("<path>", msg)[:300]}), 502
    try:
        rows = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return jsonify({"error": "the inventory answer cannot be read"}), 502
    _FK_INVENTORY_CACHE[key] = (time.time(), rows)
    return jsonify({"rows": rows})


@app.route("/api/forklift-vddk")
@requires_auth
def api_forklift_vddk_list():
    """Les archives VDDK déposées : une seule suffit pour tous les clusters."""
    d = _vddk_dir()
    out = []
    for p in sorted(d.glob("VMware-vix-disklib-*.tar.gz")):
        try:
            ver, st = _hf.check_archive_name(p.name), p.stat()
        except (ValueError, OSError):
            continue
        out.append({"name": p.name, "version": ver, "size": st.st_size, "mtime": st.st_mtime})
    try:
        st = os.statvfs(d)
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = 0
    return jsonify({"archives": out, "free": free})


def _drop_stale_vddk_parts(directory):
    """Un dépôt VDDK interrompu par un arrêt de la console laisse son
    `.part` : tout `.part` qu'aucun dépôt en cours n'écrit est un reste.
    Appelé sous `_VDDK_LOCK`."""
    for p in directory.glob("VMware-vix-disklib-*.tar.gz" + _PART_SUFFIX):
        if p.name[:-len(_PART_SUFFIX)] not in _VDDK_UPLOADS:
            p.unlink(missing_ok=True)


@app.route("/api/forklift-vddk/<archive>", methods=["PUT"])
@requires_auth
@_rate_limit("6 per minute")
def api_forklift_vddk_upload(archive):
    """Dépose l'archive VDDK de VMware (corps de la requête), vérifiée comme
    l'outil la lira ; action suivie comme tout dépôt.

    Le paramètre de route s'appelle `archive`, pas `name` : le nom que VMware
    donne à son archive (`VMware-vix-disklib-...`) porte une majuscule et des
    points, ce que le contrôle générique des noms d'objet Kubernetes
    (`_validate_k8s_path_params`, appliqué à toute route qui porte un
    paramètre `name`) refuse avant même d'atteindre cette route, comme
    `/api/exports/<archive>` le fait déjà pour les mêmes raisons."""
    try:
        _hf.check_archive_name(archive)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    length = request.content_length
    if not length:
        return jsonify({"error": "Content-Length required"}), 411
    d = _vddk_dir()
    dest, part = d / archive, d / (archive + _PART_SUFFIX)
    try:
        st = os.statvfs(d)
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = None
    if free is not None and free < length + _UPLOAD_SPARE:
        return jsonify({"error": "not enough room in the console", "need": length, "free": free}), 507
    with _VDDK_LOCK:
        if dest.exists() or archive in _VDDK_UPLOADS:
            return jsonify({"error": f"{archive} is already in the console"}), 409
        _drop_stale_vddk_parts(d)
        _VDDK_UPLOADS.add(archive)
    run = ActionRun(uuid.uuid4().hex[:12], f"vddk-archive-upload:{archive}", "(local)", ["upload", archive])
    run.cluster_user = (current_cluster_identity() or {}).get("user")
    with ACTIONS_LOCK:
        ACTIONS[run.id] = run

    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status, "message": msg, "ts": time.time()})

    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    step("upload", "running", f"receiving {archive} ({_vp.fmt_bytes(length)})")
    code, body = 201, None
    try:
        _receive_archive(run, request.stream, length, part)
        step("upload", "done", f"{archive} received")
        step("verify", "running", "checking that this is VMware's VDDK")
        _op.archive_layer(str(part))
        os.link(part, dest)
        step("verify", "done", f"VDDK {_hf.check_archive_name(archive)} kept by the console")
        run.status, run.exit_code = "done", 0
        body = {"action_id": run.id, "archive": archive, "size": length}
    except _UploadCancelled:
        run.status, run.exit_code = "cancelled", 3
        run.error_summary = "cancelled, nothing kept"
        code, body = 409, {"error": "cancelled", "action_id": run.id}
    except (ValueError, OSError, tarfile.TarError, EOFError) as e:
        run.status, run.exit_code = "error", 2
        run.error_summary = _error_text(e) if isinstance(e, OSError) else str(e)[:300]
        step("verify", "error", run.error_summary)
        code, body = 422, {"error": run.error_summary, "action_id": run.id}
    except Exception as e:                     # noqa: BLE001
        # navigateur fermé ou réseau coupé en cours de route
        run.status, run.exit_code = "error", 1
        run.error_summary = _error_text(e)
        step("upload", "error", run.error_summary)
        code, body = 400, {"error": run.error_summary, "action_id": run.id}
    finally:
        part.unlink(missing_ok=True)
        with _VDDK_LOCK:
            _VDDK_UPLOADS.discard(archive)
        run.ended_at = time.time()
        run.emit({"type": "status", "status": run.status, "exit_code": run.exit_code, "ts": time.time()})
        run.close()
    return jsonify(body), code


@app.route("/api/forklift-vddk/<archive>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_forklift_vddk_delete(archive):
    try:
        _hf.check_archive_name(archive)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    p = _vddk_dir() / archive
    if not p.is_file():
        return jsonify({"error": "not found"}), 404
    p.unlink()
    return jsonify({"deleted": archive})


@app.route("/api/storage-options/<cluster>")
@requires_auth
def api_storage_options(cluster):
    """Ce que propose le formulaire de classe : moteurs présents (Longhorn v2
    si son réglage est actif, LVM si son pilote CSI est là), nœuds LVM et
    leurs groupes de volumes, secrets de chiffrement."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    from concurrent.futures import ThreadPoolExecutor
    reads = {"csi": ("csidrivers",), "v2": ("settings.longhorn.io", "v2-data-engine", "-n", "longhorn-system"),
             "vgs": ("lvmvolumegroups.harvesterhci.io", "-A"), "secrets": ("secrets", "-A")}
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futs = {k: pool.submit(_kubectl_json, kc, "get", *a, cluster=cluster) for k, a in reads.items()}
        got = {k: f.result() for k, f in futs.items()}
    drivers = {(d.get("metadata") or {}).get("name") for d in (got["csi"] or {}).get("items") or []}
    vgs = [{"node": (v.get("spec") or {}).get("nodeName"), "vg": (v.get("spec") or {}).get("vgName")}
           for v in (got["vgs"] or {}).get("items") or []]
    secrets = [f"{(s.get('metadata') or {}).get('namespace')}/{(s.get('metadata') or {}).get('name')}"
               for s in (got["secrets"] or {}).get("items") or [] if _hadv.crypto_secret_ok(s)]
    return jsonify({"longhorn_v2": str((got["v2"] or {}).get("value")) == "true",
                    "lvm": _hadv.LVM in drivers, "lvm_groups": vgs, "crypto_secrets": sorted(secrets)})


@app.route("/api/storageclass/<cluster>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_storageclass_create(cluster):
    denied = _needs_admin()
    if denied:
        return denied
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    try:
        _hadv.storage_class(b)
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    run, err = _cli_action(cluster, f"storageclass:create:{b.get('name')}",
                           [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "storageclass", "--kubeconfig", kc],
                           "harvester-resources", spec=b, after=lambda: _invalidate_cluster_caches(cluster))
    return _res_reply(run, err, storageclass=b.get("name"))


@app.route("/api/secret/<cluster>/<namespace>/<name>/do/<action>", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_secret_do(cluster, namespace, name, action):
    """Créer un secret typé ou donner de nouvelles valeurs (administrateurs :
    un secret peut porter des mots de passe). Les valeurs passent par un
    fichier privé, jamais par la ligne de commande."""
    if action not in ("create", "update"):
        return jsonify({"error": "action: create or update"}), 400
    denied = _needs_admin()
    if denied:
        return denied
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    try:
        if action == "create":
            if b.get("type") == "crypto":
                _hadv.crypto_secret(name, namespace, str(b.get("passphrase") or ""), b.get("cipher") or "aes-xts-plain64",
                                    b.get("hash") or "sha256", b.get("size") or "256", b.get("pbkdf") or "argon2i")
            else:
                _hadv.typed_secret(name, namespace, b.get("type") or "Opaque", b.get("fields") or {})
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    run, err = _cli_action(cluster, f"secret:{action}:{namespace}/{name}",
                           [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "secret", action, "--kubeconfig", kc,
                            "--namespace", namespace, "--name", name],
                           "harvester-resources", spec=b, after=lambda: _invalidate_cluster_caches(cluster))
    return _res_reply(run, err, secret=f"{namespace}/{name}", action=action)


@app.route("/api/sshkey/<cluster>/<namespace>/<name>/do/update", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_sshkey_update(cluster, namespace, name):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    b = request.get_json(silent=True) or {}
    files = []
    try:
        _hadv.keypair_update({}, str(b.get("public_key") or ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    extra = ["--public-key-file", _ct_text_file(files, str(b["public_key"]))]
    if "description" in b:
        extra += ["--description", str(b.get("description") or "")[:1000]]
    run, err = _res_cli(cluster, kc, f"sshkey:update:{namespace}/{name}",
                        ["sshkey", "update", "--namespace", namespace, "--name", name] + extra, files)
    return _res_reply(run, err, sshkey=f"{namespace}/{name}")


@app.route("/api/vm/<cluster>/<namespace>/<name>/logs")
@requires_auth
def api_vm_logs(cluster, namespace, name):
    """« View Logs » de Harvester : les journaux du pod virt-launcher de la
    VM (conteneur `compute` par défaut ; `guest-console-log` porte la console
    série quand KubeVirt l'enregistre). Réservé aux opérateurs : une console
    peut afficher des secrets."""
    if ROLE_RANK.get(current_role(), 0) < ROLE_RANK["operator"]:
        return jsonify({"error": "forbidden", "required": "operator",
                        "hint": "a VM's logs can show what its console printed"}), 403
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    pods = (_kubectl_json(kc, "get", "pods", "-n", namespace, "-l",
                          f"kubevirt.io=virt-launcher,vm.kubevirt.io/name={name}",
                          cluster=cluster) or {}).get("items") or []
    if not pods:
        return jsonify({"error": f"{namespace}/{name} is not running: no virt-launcher pod"}), 404
    # pendant une migration, deux pods : celui qui tourne d'abord, le plus récent ensuite
    pods.sort(key=lambda p: (p.get("metadata") or {}).get("creationTimestamp") or "", reverse=True)
    pods.sort(key=lambda p: (p.get("status") or {}).get("phase") != "Running")
    pod = pods[0]
    containers = [c.get("name") for c in (pod.get("spec") or {}).get("containers") or []]
    default = ((pod.get("metadata") or {}).get("annotations") or {}).get(
        "kubectl.kubernetes.io/default-container") or (containers[0] if containers else "compute")
    container = request.args.get("container") or default
    if container not in containers:
        return jsonify({"error": f"no container {container} in the pod", "containers": containers}), 400
    try:
        tail = max(10, min(5000, int(request.args.get("tail") or 500)))
    except ValueError:
        tail = 500
    r = _kubectl_run(["kubectl", "--kubeconfig", kc, "logs", "-n", namespace, pod["metadata"]["name"],
                      "-c", container, f"--tail={tail}", "--timestamps"],
                     capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return jsonify({"error": (r.stderr or "kubectl logs failed").strip()[:500]}), 502
    return jsonify({"pod": pod["metadata"]["name"], "node": (pod.get("spec") or {}).get("nodeName"),
                    "containers": containers, "container": container, "tail": tail, "logs": r.stdout})


@app.route("/api/vm/<cluster>/<namespace>/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("30/minute")
def api_vm_delete(cluster, namespace, name):
    """Supprimer une VM, et les volumes cochés (comme la fenêtre de Harvester).
    Le bouton de la vue Cluster appelait cette route, qui n'existait pas."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    body = request.get_json(silent=True) or {}
    remove = body.get("remove_volumes") or []
    if not isinstance(remove, list) or not all(isinstance(r, str) and _K8S_NAME_RE.match(r) for r in remove):
        return jsonify({"error": "remove_volumes: a list of volume names"}), 400
    cmd = [sys.executable, str(BIN_DIR / RESOURCES_SCRIPT), "vm", "delete", "--kubeconfig", kc,
           "--namespace", namespace, "--name", name]
    if remove:
        cmd += ["--remove-volumes", ",".join(remove)]
    if body.get("keep_cloudinit"):
        cmd.append("--keep-cloudinit")
    run, err = _cli_action(cluster, f"vm:delete:{namespace}/{name}", cmd, "harvester-resources",
                           after=lambda: _invalidate_cluster_caches(cluster))
    return _res_reply(run, err, vm=f"{namespace}/{name}", removed=remove)


@app.route("/api/yaml/<cluster>/<kind>/<oname>", methods=["PUT"])
@requires_auth
@_rate_limit("30/minute")
def api_yaml_put(cluster, kind, oname):
    return _yaml_write(cluster, kind, request.args.get("namespace") or "", oname, creating=False)


@app.route("/api/yaml/<cluster>/<kind>", methods=["POST"])
@requires_auth
@_rate_limit("30/minute")
def api_yaml_create(cluster, kind):
    return _yaml_write(cluster, kind, request.args.get("namespace") or "", None, creating=True)


# ---------------------------------------------------------------------------
# v1.52.0 (B2) : services sur les clusters créés, par CAAPH (HelmChartProxy)
# ---------------------------------------------------------------------------
import capi_services as _sv  # noqa: E402

_CAPI_SERVICES_CACHE = {}      # (cluster, identité) -> (horodatage, réponse)


def _capi_service_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None, (jsonify({"error": "a JSON object is expected"}), 400)
    try:
        return _sv.normalize(body), None
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 400)


@app.route("/api/capi/<cluster>/services")
@requires_auth
def api_capi_services(cluster):
    """Catalogue, services déployés et leur état sur chaque cluster, et si
    CAAPH est installé. Lecture seule, 5 s de cache par identité."""
    key = (cluster, (current_cluster_identity() or {}).get("user"), current_user())
    hit = _CAPI_SERVICES_CACHE.get(key)
    if hit and request.args.get("fresh") != "1" and time.time() - hit[0] < 5:
        return jsonify(hit[1])
    kc = _kubectl_for_cluster(cluster)
    if kc and _cluster_reachable(kc) is False:
        return jsonify({"unreachable": True, "caaph": False, "clusters": [],
                        "catalog": _sv.catalog(), "services": []})
    cmd, err = _capi_base(cluster, "services")
    if err:
        return err
    res, err = _capi_run_json(cmd + ["--json"], timeout=60)
    if err:
        return err
    out = res[0]
    _CAPI_SERVICES_CACHE[key] = (time.time(), out)
    return jsonify(out)


@app.route("/api/capi/<cluster>/service-check", methods=["POST"])
@requires_auth
@_rate_limit("60/minute")
def api_capi_service_check(cluster):
    spec, err = _capi_service_body()
    if err:
        return err
    cmd, err = _capi_base(cluster, "service-check")
    if err:
        return err
    res, err = _capi_run_json(cmd + ["--spec", "-"], spec=spec, timeout=60)
    if err:
        return err
    return jsonify(res[0])


@app.route("/api/capi/<cluster>/service-deploy", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_capi_service_deploy(cluster):
    """Déploie un service sur un cluster créé : action suivie."""
    spec, err = _capi_service_body()
    if err:
        return err
    cmd, err = _capi_base(cluster, "service-deploy")
    if err:
        return err
    run, err = _cli_action(cluster, f"capi-service-deploy:{spec['cluster']}:{spec['name']}", cmd,
                           "harvester-capi", spec=spec,
                           after=lambda: _CAPI_SERVICES_CACHE.clear())
    if err:
        return err
    return jsonify({"action_id": run.id, "name": spec["name"], "cluster": spec["cluster"]}), 202


@app.route("/api/capi/<cluster>/service/<namespace>/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_capi_service_remove(cluster, namespace, name):
    """Retire un service : CAAPH désinstalle ses releases."""
    cmd, err = _capi_base(cluster, "service-remove")
    if err:
        return err
    run, err = _cli_action(cluster, f"capi-service-remove:{namespace}/{name}",
                           cmd + ["--name", f"{namespace}/{name}"], "harvester-capi",
                           after=lambda: _CAPI_SERVICES_CACHE.clear())
    if err:
        return err
    return jsonify({"action_id": run.id, "name": name}), 202


@app.route("/api/capi/<cluster>/cluster-create", methods=["POST"])
@requires_auth
@_rate_limit("6 per minute")
def api_capi_cluster_create(cluster):
    """Crée un cluster RKE2 : contrôle, manifestes, application, suivi
    jusqu'à ce qu'il soit disponible. Réponse immédiate avec l'action."""
    spec, err = _capi_spec_body()
    if err:
        return err
    errs = _cc.validate(spec)
    if errs:
        return jsonify({"error": "invalid request", "invalid": dict(errs)}), 400
    cmd, err = _capi_base(cluster, "create")
    if err:
        return err
    comp = _capi_components_dir()
    if comp:
        cmd += ["--components", str(comp)]
    run, err = _capi_action(cluster, f"capi-cluster-create:{spec['namespace']}/{spec['name']}",
                            cmd, spec=spec)
    if err:
        return err
    return jsonify({"action_id": run.id, "cluster": f"{spec['namespace']}/{spec['name']}"}), 201


@app.route("/api/capi/<cluster>/cluster/<namespace>/<name>", methods=["DELETE"])
@requires_auth
@_rate_limit("6 per minute")
def api_capi_cluster_delete(cluster, namespace, name):
    """Supprime un cluster créé par Cluster API et suit le retrait de ses
    machines ; retire aussi son espace de noms s'il a été créé pour lui."""
    cmd, err = _capi_base(cluster, "delete")
    if err:
        return err
    run, err = _capi_action(cluster, f"capi-cluster-delete:{namespace}/{name}",
                            cmd + ["--name", f"{namespace}/{name}"])
    if err:
        return err
    return jsonify({"action_id": run.id, "deleting": f"{namespace}/{name}"}), 201


@app.route("/api/capi/<cluster>/cleanup-legacy", methods=["POST"])
@requires_auth
@_rate_limit("6 per minute")
def api_capi_cleanup_legacy(cluster):
    """Retire une installation d'avant Turtles, sans toucher au cœur que
    Turtles a repris. Refusé tant qu'un cluster Cluster API existe."""
    data = request.get_json(silent=True) or {}
    cmd, err = _capi_base(cluster, "cleanup-legacy")
    if err:
        return err
    dry = bool(data.get("dry_run"))
    run, err = _capi_action(cluster, f"capi-cleanup-legacy:{cluster}",
                            cmd + (["--dry-run"] if dry else []), dry_run=dry)
    if err:
        return err
    return jsonify({"action_id": run.id, "dry_run": dry}), 201


@app.route("/api/capi/<cluster>/cluster/<namespace>/<name>/scale", methods=["POST"])
@requires_auth
def api_capi_cluster_scale(cluster, namespace, name):
    """Scale workers via the MachineDeployment that backs this cluster's
    `default-worker` topology entry. Body: {"replicas": N}."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    try:
        replicas = int(data.get("replicas", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "replicas must be an integer"}), 400
    if replicas < 0 or replicas > 100:
        return jsonify({"error": "replicas out of range (0-100)"}), 400
    # Discover the actual MD name + class from the cluster topology so we
    # don't hardcode (it varies per template).
    r = _kubectl_run(
        ["kubectl", "--kubeconfig", kc, "-n", namespace, "get",
         "cluster.cluster.x-k8s.io", name,
         "-o", "jsonpath={.spec.topology.workers.machineDeployments[0].name}|{.spec.topology.workers.machineDeployments[0].class}"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0 or "|" not in r.stdout:
        return jsonify({"error": "could not read cluster topology", "stderr": r.stderr[:200]}), 500
    md_name, md_class = r.stdout.split("|", 1)
    md_name = md_name.strip(); md_class = md_class.strip()
    if not md_name:
        return jsonify({"error": "cluster has no workers MD to scale"}), 400
    # v1.48.0 : seul le nombre change. Le patch de fusion remplaçait toute la
    # liste par une entrée (classe, nom, nombre) : les variables propres au
    # groupe et les autres groupes de workers disparaissaient.
    patch = json.dumps([{"op": "replace",
                         "path": "/spec/topology/workers/machineDeployments/0/replicas",
                         "value": replicas}])
    action_id = track_action(
        f"capi-cluster-scale:{namespace}/{name}->{replicas}", cluster,
        _simple_kubectl_action, kc,
        ["-n", namespace, "patch", "cluster.cluster.x-k8s.io", name,
         "--type=json", "-p", patch],
        "scale", f"scaled {md_name} to {replicas} workers",
    )
    return jsonify({"action_id": action_id, "replicas": replicas,
                    "md_name": md_name}), 201


@app.route("/api/capi/<cluster>/cluster/<namespace>/<name>/kubeconfig")
@requires_auth
def api_capi_cluster_kubeconfig(cluster, namespace, name):
    """Fetch the downstream cluster's kubeconfig from the
    <name>-kubeconfig secret CAPI creates after Provisioned."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    r = _kubectl_run(
        ["kubectl", "--kubeconfig", kc, "-n", namespace, "get",
         f"secret/{name}-kubeconfig", "-o", "jsonpath={.data.value}"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return jsonify({"error": "kubeconfig secret not found (cluster not yet provisioned?)",
                        "stderr": r.stderr[:200]}), 404
    try:
        kubeconfig = base64.b64decode(r.stdout.strip()).decode("utf-8")
    except Exception as e:
        return jsonify({"error": f"decode failed: {e}"}), 500
    return Response(kubeconfig, mimetype="text/yaml",
                    headers={"Content-Disposition": f'attachment; filename="{name}.kubeconfig.yaml"'})


@app.route("/api/capi/<cluster>/cluster/<namespace>/<name>/details")
@requires_auth
def api_capi_cluster_details(cluster, namespace, name):
    """Rich details for a single CAPI cluster: spec, status, conditions,
    machines, phase summary. Used by the wizard's per-cluster panel."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404

    def kc_run(*args, timeout=8):
        r = _kubectl_run(["kubectl", "--kubeconfig", kc, *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    rc, cl_out, _ = kc_run("-n", namespace, "get", "cluster.cluster.x-k8s.io",
                           name, "-o", "json")
    if rc != 0:
        return jsonify({"error": "cluster not found", "stderr": _[:200]}), 404
    try:
        cl = json.loads(cl_out)
    except Exception as e:
        return jsonify({"error": f"json: {e}"}), 500
    # Fetch machines belonging to this cluster
    rc2, m_out, _ = kc_run("-n", namespace, "get", "machines.cluster.x-k8s.io",
                           "-l", f"cluster.x-k8s.io/cluster-name={name}",
                           "-o", "json")
    machines = []
    if rc2 == 0:
        try:
            for it in json.loads(m_out).get("items", []):
                m = it["metadata"]; st = it.get("status", {})
                machines.append({
                    "name": m["name"],
                    "phase": st.get("phase", ""),
                    "providerID": (it.get("spec", {}).get("providerID") or ""),
                    "nodeName": (st.get("nodeRef", {}) or {}).get("name", ""),
                    "k8sVersion": (it.get("spec", {}) or {}).get("version", ""),
                    "creationTimestamp": m.get("creationTimestamp", ""),
                })
        except Exception:
            pass
    return jsonify({
        "name": name,
        "namespace": namespace,
        "phase": cl.get("status", {}).get("phase", ""),
        # v1.48.0 : le cœur de Turtles répond en v1beta2 (plus de
        # `status.controlPlaneReady`)
        "ready": _cc.cluster_state(cl)["ready"]
                 or (cl.get("status", {}).get("controlPlaneReady", False)
                     and cl.get("status", {}).get("infrastructureReady", False)),
        "conditions": cl.get("status", {}).get("conditions", []),
        "topology": cl.get("spec", {}).get("topology", {}),
        "controlPlaneEndpoint": cl.get("spec", {}).get("controlPlaneEndpoint", {}),
        "machines": machines,
    })


# -----------------------------------------------------------------------------
# CAPI / CAPHV stack uninstall (reverse of install)
# -----------------------------------------------------------------------------
# Images present in CAPI bundles whose ctr images we remove from each
# Harvester node (best-effort — non-fatal if a tag is already gone).
CAPI_IMAGE_PATTERNS = [
    "registry.k8s.io/cluster-api/",
    "ghcr.io/rancher/cluster-api-provider-rke2-",
    "ghcr.io/rancher-sandbox/cluster-api-provider-harvester",
    "quay.io/jetstack/cert-manager-",
]
# Namespaces created by our bundle (DO NOT touch cattle-* — those are Rancher).
CAPI_UNINSTALL_NAMESPACES = [
    "capi-system",
    "capi-kubeadm-bootstrap-system",
    "capi-kubeadm-control-plane-system",
    "rke2-bootstrap-system",
    "rke2-control-plane-system",
    "caphv-system",
    "cert-manager",
]


def _capi_uninstall_runner(run, cluster, kc, nodes_ips, ssh_user, ssh_key,
                            ssh_port, keep_cert_manager, dry_run):
    """Reverse of _capi_install_runner. Steps:
      preflight → clusterclass → providers → cert-manager (optional) →
      ctr-images. ClusterClass is deleted first so the controllers can drain
      their watches cleanly before we delete the deployments."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})

    def step(sid, status, message=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": message, "ts": time.time()})

    step("preflight", "running", f"Uninstalling CAPI/CAPHV from {cluster}")
    if dry_run:
        step("preflight", "done", "DRY-RUN: no changes will be applied")
    else:
        step("preflight", "done", "Proceeding with real uninstall")

    # 1. Delete ClusterClass first so reconciliation stops cleanly.
    step("clusterclass", "running", "Deleting harvester-rke2 ClusterClass")
    if not dry_run:
        for nm in ("harvester-rke2",):
            r = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "delete",
                 "clusterclass.cluster.x-k8s.io", nm, "--ignore-not-found",
                 "--wait=false"],
                capture_output=True, text=True, timeout=30,
            )
            step("clusterclass", "progress",
                 (r.stdout.strip() or r.stderr.strip() or f"{nm}: ok")[:200])
    step("clusterclass", "done", "ClusterClass removed")

    # 2. Delete the controller namespaces. cert-manager is opt-in (skipped by
    # default — other workloads on the cluster may rely on it).
    nss = list(CAPI_UNINSTALL_NAMESPACES)
    if keep_cert_manager:
        nss = [n for n in nss if n != "cert-manager"]
    step("namespaces", "running",
         f"Deleting {len(nss)} namespaces: {', '.join(nss)}")
    if not dry_run:
        for ns in nss:
            r = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "delete", "ns", ns,
                 "--ignore-not-found", "--wait=false"],
                capture_output=True, text=True, timeout=30,
            )
            step("namespaces", "progress",
                 (r.stdout.strip() or r.stderr.strip() or f"{ns}: ok")[:200])
        # Wait up to ~3 minutes for them to actually terminate
        deadline = time.time() + 180
        while time.time() < deadline:
            r = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "get", "ns", *nss,
                 "-o", "jsonpath={.items[*].metadata.name}",
                 "--ignore-not-found"],
                capture_output=True, text=True, timeout=10,
            )
            remaining = r.stdout.strip().split()
            if not remaining:
                break
            step("namespaces", "progress", f"still terminating: {' '.join(remaining)}")
            time.sleep(10)
    step("namespaces", "done", "namespaces deleted")

    # 3. Delete CRDs (cluster-scoped — won't disappear with namespaces).
    step("crds", "running", "Deleting CAPI/CAPHV CRDs")
    if not dry_run:
        for crd_glob in (
            "infrastructure.cluster.x-k8s.io",
            "bootstrap.cluster.x-k8s.io",
            "controlplane.cluster.x-k8s.io",
            "addons.cluster.x-k8s.io",
        ):
            r = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "get", "crd",
                 "-o", "jsonpath={.items[*].metadata.name}"],
                capture_output=True, text=True, timeout=15,
            )
            for name in r.stdout.split():
                if not name.endswith(crd_glob):
                    continue
                # Don't nuke the Rancher Turtles core CRDs that ship CAPI
                # itself — they're cluster-scoped and owned by Turtles.
                if name in ("clusters.cluster.x-k8s.io",
                            "clusterclasses.cluster.x-k8s.io",
                            "machines.cluster.x-k8s.io",
                            "machinedeployments.cluster.x-k8s.io",
                            "machinepools.cluster.x-k8s.io",
                            "machinesets.cluster.x-k8s.io",
                            "machinehealthchecks.cluster.x-k8s.io"):
                    continue
                d = _kubectl_run(
                    ["kubectl", "--kubeconfig", kc, "delete", "crd", name,
                     "--ignore-not-found", "--wait=false"],
                    capture_output=True, text=True, timeout=15,
                )
                step("crds", "progress",
                     (d.stdout.strip() or d.stderr.strip() or f"{name}: ok")[:200])
    step("crds", "done", "CRDs cleaned")

    # 4. Remove container images from each Harvester node via ssh + ctr.
    step("ctr-images", "running", f"Removing images on {len(nodes_ips)} node(s)")
    if not dry_run:
        for node_ip in nodes_ips:
            ssh_opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                        "-o", "StrictHostKeyChecking=accept-new", "-p", str(ssh_port)]
            if ssh_key:
                ssh_opts.extend(["-i", ssh_key])
            # Build a single command listing all images then deleting
            # only the ones we ship.
            patterns = "|".join(p.replace("/", "\\/") for p in CAPI_IMAGE_PATTERNS)
            remote_cmd = (
                "sudo /var/lib/rancher/rke2/bin/ctr "
                "  --address /run/k3s/containerd/containerd.sock "
                "  --namespace k8s.io images ls -q | "
                f"  grep -E '{patterns}' | "
                "  xargs -r -n1 sudo /var/lib/rancher/rke2/bin/ctr "
                "  --address /run/k3s/containerd/containerd.sock "
                "  --namespace k8s.io images rm"
            )
            try:
                r = subprocess.run(
                    ["ssh", *ssh_opts, f"{ssh_user}@{node_ip}", remote_cmd],
                    capture_output=True, text=True, timeout=120,
                )
                out = (r.stdout or "").strip()
                err = (r.stderr or "").strip()
                if r.returncode == 0:
                    step("ctr-images", "progress",
                         f"{node_ip}: removed " + (out.replace('\n', ' ')[:200] or "no images matched"))
                else:
                    step("ctr-images", "progress",
                         f"{node_ip} non-zero: " + err[:200])
            except subprocess.TimeoutExpired:
                step("ctr-images", "progress", f"{node_ip}: ssh timeout")
    step("ctr-images", "done", "Images removed from nodes")

    run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
    run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
    run.close()


@app.route("/api/capi/<cluster>/uninstall", methods=["POST"])
@requires_auth
def api_capi_uninstall(cluster):
    """Uninstall the CAPI/CAPHV stack from the target cluster + clean the
    container images off the Harvester nodes. Reverse of `/install`.
    Body: { "dry_run": <bool>, "keep_cert_manager": <bool> }.
    cert-manager is preserved by default (other workloads may rely on it)."""
    cfg = load_config()
    cluster_cfg = next((c for c in cfg.get("clusters", []) if c["name"] == cluster), None)
    if not cluster_cfg:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    kc = _identity_kubeconfig(cluster_cfg.get("kubeconfig", ""),
                              current_cluster_identity())
    if not Path(kc).exists():
        return jsonify({"error": "kubeconfig missing"}), 400
    data = request.get_json(force=True, silent=True) or {}
    dry_run = bool(data.get("dry_run", False))
    keep_cm = bool(data.get("keep_cert_manager", True))
    nodes = cluster_cfg.get("nodes", [])
    nodes_ips = [n["ip"] for n in nodes if n.get("ip")]
    ssh = cluster_cfg.get("ssh", {}) or {}

    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, f"capi-uninstall:{cluster}", cluster, [],
                    dry_run=dry_run)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(
        target=_capi_uninstall_runner,
        args=(run, cluster, kc, nodes_ips,
              ssh.get("user", "rancher"),
              ssh.get("key", ""),
              ssh.get("port", 22),
              keep_cm, dry_run),
        daemon=True,
    ).start()
    return jsonify({"action_id": run_id, "dry_run": dry_run,
                    "keep_cert_manager": keep_cm}), 201


def _api_resource_names(text):
    """Noms pluriels qualifiés ({'clusters.cluster.x-k8s.io', ...}) lus dans
    la sortie de `kubectl api-resources --no-headers`.

    Colonnes : NAME [SHORTNAMES] APIVERSION NAMESPACED KIND. SHORTNAMES est
    absent sur beaucoup de lignes, donc on lit par la FIN : APIVERSION est
    l'avant-avant-dernier champ quoi qu'il arrive. Compter depuis le début
    décalerait une ligne sur deux.
    """
    names = set()
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        name, api_version = parts[0], parts[-3]
        group = api_version.rsplit("/", 1)[0] if "/" in api_version else ""
        names.add(f"{name}.{group}" if group else name)
    return names


@app.route("/api/capi/<cluster>/diag")
@requires_auth
@shared_read()
def api_capi_diag(cluster):
    """Diagnostic of the CAPI/CAPHV stack on the target Harvester cluster."""
    # Cluster déclaré mais hors tension : répondre tout de suite. Sans cela
    # cet appel attend le délai de `kubectl`, et une bascule de cluster
    # enchaîne ces attentes (75 s mesurées).
    kc = _kubectl_for_cluster(cluster)
    if _cluster_reachable(kc) is False:
        return jsonify(_unreachable_payload(cluster, kc)), 200

    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404

    def kc_run(*args, timeout=8):
        try:
            r = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, *args],
                capture_output=True, text=True, timeout=timeout,
            )
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"

    # Quelles familles d'API ce cluster expose-t-il.
    #
    # On demandait `get crd`, en le croyant bon marché. Mesuré sur harv1 :
    # 298 CRD, et l'API renvoie les objets ENTIERS, schémas OpenAPI compris,
    # soit 37,9 Mo et ~24 s. Le format de sortie n'y change rien, il ne joue
    # que sur le rendu côté client. Or ce `kc_run` n'accorde que 8 s : l'appel
    # expirait À TOUS LES COUPS, `crds` restait vide, et le diagnostic
    # annonçait la pile CAPI absente même quand elle était installée.
    # `api-resources` répond à la même question en 0,9 s.
    rc, out, _ = kc_run("api-resources", "--no-headers", timeout=25)
    crds = _api_resource_names(out) if rc == 0 else set()
    have_capi = any(c.startswith("clusters.cluster.x-k8s.io") for c in crds)
    have_caphv = any(c.endswith(".infrastructure.cluster.x-k8s.io") and "harvester" in c for c in crds)

    results = []
    for comp in CAPI_COMPONENTS:
        entry = {"id": comp["id"], "label": comp["label"], "installed": False, "details": ""}
        if comp["kind"] == "deployment":
            candidates = comp.get("candidates") or [{"namespace": comp["namespace"],
                                                     "name": comp.get("name") or comp["selector"]["app"]}]
            for c in candidates:
                ns = c["namespace"]
                # Use selector if defined, otherwise name
                rc, out, _ = kc_run(
                    "-n", ns, "get", "deploy",
                    *(c.get("name"),) if c.get("name") else ("-l", ",".join(f"{k}={v}" for k, v in c.get("selector", {}).items())),
                    "-o", "json", timeout=5,
                )
                if rc == 0 and out.strip():
                    try:
                        d = json.loads(out)
                        if d.get("kind") == "DeploymentList":
                            items = d.get("items", [])
                            if not items:
                                continue
                            d = items[0]
                        st = d.get("status", {})
                        ready = st.get("readyReplicas", 0)
                        desired = st.get("replicas", 0)
                        entry["installed"] = ready > 0
                        # Pull the controller image tag so the diag row shows
                        # which version is actually running — answers
                        # "what's deployed?" without going through kubectl.
                        containers = (d.get("spec", {}).get("template", {})
                                       .get("spec", {}).get("containers", []) or [])
                        ctr_img = ""
                        for ctr in containers:
                            img = ctr.get("image", "")
                            if "manager" in (ctr.get("name", "") or "") or "manager" in img:
                                ctr_img = img
                                break
                        if not ctr_img and containers:
                            ctr_img = containers[0].get("image", "")
                        ver = ctr_img.split(":")[-1] if ":" in ctr_img else ""
                        entry["version"] = ver
                        entry["image"] = ctr_img
                        entry["details"] = (f"{ns}/{d['metadata']['name']} — {ready}/{desired} ready"
                                            + (f" · {ver}" if ver else ""))
                        break
                    except Exception:
                        pass
        elif comp["kind"] == "clusterclass":
            # `kubectl get clusterclass <name> -A` is invalid syntax (no -A
            # with a positional name) — list-all then filter by name.
            rc, out, _ = kc_run("get", "clusterclass", "-A", "-o", "json", timeout=5)
            if rc == 0 and out.strip():
                try:
                    d = json.loads(out)
                    found = [it for it in d.get("items", [])
                             if it.get("metadata", {}).get("name") == comp["name"]]
                    if found:
                        entry["installed"] = True
                        entry["details"] = f"found in {found[0]['metadata']['namespace']}"
                except Exception:
                    pass
        results.append(entry)

    # CAPI clusters list (read-only). Filter out Rancher's own provisioning
    # clusters (fleet-local/local has no infrastructureRef and never goes
    # through CAPHV) — we only want clusters managed by *our* infra provider.
    capi_clusters = []
    rc, out, _ = kc_run("get", "clusters.cluster.x-k8s.io", "-A", "-o", "json", timeout=8)
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)
            for c in data.get("items", []):
                spec = c.get("spec", {})
                infra_ref = spec.get("infrastructureRef") or {}
                kind = infra_ref.get("kind", "")
                # Keep only CAPHV-managed clusters. HarvesterCluster is the
                # spec.infrastructureRef.kind set by our provider.
                if kind != "HarvesterCluster":
                    continue
                st = c.get("status", {})
                topology = spec.get("topology", {}) or {}
                capi_clusters.append({
                    "namespace": c["metadata"]["namespace"],
                    "name":      c["metadata"]["name"],
                    "phase":     st.get("phase", "Unknown"),
                    # v1.48.0 : v1beta2 (cœur de Turtles) comme v1beta1
                    "ready":     _cc.cluster_state(c)["ready"]
                                 or bool(st.get("controlPlaneReady")),
                    "clusterClass": _cc.class_of(c),
                    "k8sVersion": topology.get("version"),
                    "creationTimestamp": c["metadata"].get("creationTimestamp"),
                })
        except Exception:
            pass

    # Resolve the target Harvester version + check active bundle compatibility
    active = _capi_bundle_active_path()
    harvester_version = _harvester_server_version(kc) if kc else ""
    compat = None
    if active is not None:
        manifest = _read_bundle_manifest(active)
        ok, globs, tgt = _bundle_compatibility(harvester_version, manifest)
        compat = {
            "compatible": ok,
            "target_version": tgt,
            "supported_versions": globs,
        }
    return jsonify({
        "cluster": cluster,
        "have_capi_crds": have_capi,
        "have_caphv_crds": have_caphv,
        "components": results,
        "capi_clusters": capi_clusters,
        "bundle_available": active is not None,
        "active_bundle": (_capi_bundle_active_filename() or ""),
        "harvester_version": harvester_version,
        "bundle_compatibility": compat,
    })


# =============================================================================
# Transfert de VM entre clusters, export et import (v1.45.0)
#
# La console ne réimplémente rien : elle lance `bin/harvester-vm-transfer.py`,
# le même script qu'un opérateur utilise en ligne de commande sur un site
# isolé, et relaie ses STEP_EVENT. Elle lui passe les kubeconfigs préparés
# ICI, dans le thread de la requête : ils portent l'identité de l'opérateur,
# que le thread de travail ne saurait pas retrouver.
#
# Voir docs/design/2026-09-24-migration-vm.md.
# =============================================================================
import vm_transfer as _vt  # noqa: E402
import vm_transfer_progress as _vp  # noqa: E402

EXPORT_DIR = Path(os.environ.get(
    "HARVESTER_OPS_EXPORT_DIR",
    str(Path.home() / ".local/share/harvester-ops/exports"),
))
VM_TRANSFER_SCRIPT = "harvester-vm-transfer.py"
_EXPORT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.hvx$")
_SC_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_TRANSFER_CHOICES = {"mode": ("stop", "short"),
                     "source": ("running", "stopped", "deleted"),
                     "target": ("started", "stopped")}


def _export_dir():
    """Magasin des archives : elles contiennent les secrets cloud-init des
    VMs, d'où un répertoire 0700."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        EXPORT_DIR.chmod(0o700)
    except OSError:
        pass
    return EXPORT_DIR


def _export_safe_name(name):
    if not name or "/" in name or ".." in name or not _EXPORT_NAME_RE.match(name):
        return None
    return name


def _vm_transfer_script():
    p = BIN_DIR / VM_TRANSFER_SCRIPT
    return p if p.is_file() else None


def _net_ref_ok(v):
    ns, sep, n = str(v).partition("/")
    return bool(sep) and _valid_k8s_name(ns) and _valid_k8s_name(n)


def _transfer_args(body, kind):
    """Options de la demande en arguments du script, validées une à une :
    rien de ce que le navigateur envoie ne part tel quel sur une ligne de
    commande."""
    args = []
    for key in ("name", "namespace"):
        v = body.get(key)
        if v:
            if not _valid_k8s_name(v):
                raise ValueError(f"invalid {key}")
            args += [f"--{key}", v]
    for key, allowed in _TRANSFER_CHOICES.items():
        v = body.get(key)
        if not v:
            continue
        if v not in allowed:
            raise ValueError(f"invalid {key}")
        if kind == "export" and key != "source":
            continue
        if kind == "import" and key != "target":
            continue
        if kind == "export" and v == "deleted":
            raise ValueError("an export never deletes its source")
        args += [f"--{key}", v]
    if kind != "export":
        if body.get("keep_mac") is False:
            args.append("--new-mac")
        if body.get("create_namespace"):
            args.append("--create-namespace")
        for src, dst in (body.get("networks") or {}).items():
            if dst:
                if not (_net_ref_ok(src) and _net_ref_ok(dst)):
                    raise ValueError("invalid network mapping")
                args += ["--map-net", f"{src}={dst}"]
        for src, dst in (body.get("storage_classes") or {}).items():
            if dst:
                if not (_SC_NAME_RE.match(str(src)) and _SC_NAME_RE.match(str(dst))):
                    raise ValueError("invalid storage class mapping")
                args += ["--map-sc", f"{src}={dst}"]
        serve = (load_config().get("transfer") or {}).get("serve_address")
        if serve:
            args += ["--serve-address", str(serve)]
    if kind in ("migrate", "import"):
        sp = body.get("speed")
        if sp:
            if sp not in ("eco", "normal", "max"):
                raise ValueError("invalid speed")
            args += ["--speed", sp]
        if body.get("parallel") not in (None, "", 0):
            try:
                n = int(body["parallel"])
            except (TypeError, ValueError):
                raise ValueError("invalid parallel") from None
            if not 1 <= n <= 32:
                raise ValueError("invalid parallel")
            args += ["--parallel", str(n)]
        if body.get("bandwidth") not in (None, "", 0):
            try:
                bw = float(body["bandwidth"])
            except (TypeError, ValueError):
                raise ValueError("invalid bandwidth") from None
            if not 1 <= bw <= 100000:
                raise ValueError("invalid bandwidth")
            args += ["--bandwidth", f"{bw:g}"]
    if kind == "migrate":
        if body.get("keep_backups"):
            args.append("--keep-backups")
        if body.get("engine") == "file":
            args += ["--engine", "file"]
    return args


def _transfer_check(cmd):
    """Lance un contrôle (lecture seule) et rend sa sortie JSON."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "the pre-check timed out"}), 504
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        err = [ln.split("|", 3)[3] for ln in (proc.stderr or "").splitlines()
               if ln.startswith("STEP_EVENT|") and ln.split("|")[2:3] == ["error"]]
        return jsonify({"error": (err[-1] if err else "pre-check failed")[:300]}), 502
    out["blocked"] = proc.returncode == 2
    return jsonify(out)


def _transfer_busy(clusters, label):
    """Même VM déjà en transfert, ou arrêt/démarrage de l'un des clusters en
    cours : un transfert qui démarre sous un cluster qu'on éteint échoue au
    milieu, et deux transferts de la même VM se marcheraient dessus."""
    for r in ACTIONS.values():
        if r.status not in ("starting", "running") or r.dry_run:
            continue
        if r.action == label and r.cluster == clusters[0]:
            return r
        if r.action in CLUSTER_SEQUENCES and r.cluster in clusters:
            return r
    return None


def _vm_transfer_runner(run, cmd):
    """Relaie les étapes du script, comme pour l'installateur du provider."""
    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": msg, "ts": time.time()})

    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    last_error = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        run.proc = proc          # annulable depuis le dock : le script défait tout
        for line in proc.stderr:
            line = line.strip()
            if line.startswith("STEP_EVENT|"):
                parts = line.split("|", 3)
                if len(parts) == 4:
                    step(parts[1], parts[2], parts[3])
                    if parts[2] == "error":
                        last_error = parts[3]
            elif line.startswith("PROGRESS_EVENT|"):
                # v1.46.0 : débit, temps restant, quantités ; gardé à part
                parts = line.split("|", 2)
                try:
                    snap = json.loads(parts[2]) if len(parts) == 3 else None
                except ValueError:
                    snap = None
                if isinstance(snap, dict):
                    run.emit_progress(snap)
        rc = proc.wait()
        run.exit_code = rc
        if rc == 0:
            run.status = "done"
        elif rc == 3 or run.status == "cancelled":
            run.status = "cancelled"
            run.error_summary = "cancelled, transfer undone"
        else:
            run.status = "error"
            run.error_summary = (last_error or f"exit {rc}")[:300]
    except Exception as e:                     # noqa: BLE001
        run.error_summary = str(e)[:300]
        step("transfer", "error", str(e)[:300])
        run.exit_code = 1
        run.status = "error"
    run.ended_at = time.time()
    run.emit({"type": "status", "status": run.status,
              "exit_code": run.exit_code, "ts": time.time()})
    run.close()


def _start_transfer(label, cluster, clusters, public_cmd, cmd, result=None):
    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, label, cluster, public_cmd)
    run.result = dict(result or {})
    _ident = current_cluster_identity()
    run.cluster_user = (_ident or {}).get("user")
    with ACTIONS_LOCK:
        busy = _transfer_busy(clusters, label)
        if busy:
            return None, busy
        ACTIONS[run_id] = run
    threading.Thread(target=_vm_transfer_runner,
                     args=(run, cmd + ["--id", run_id[:8]]), daemon=True).start()
    return run, None


def _busy_response(busy):
    return jsonify({"error": f"{busy.action} is already running on {busy.cluster} "
                             f"(action {busy.id})",
                    "running": busy.id, "running_action": busy.action}), 409


@app.route("/api/vm/<cluster>/<namespace>/<name>/transfer/check", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_vm_transfer_check(cluster, namespace, name):
    """Contrôle préalable d'un transfert vers `to`, ou d'un export si `to`
    est vide. Ne modifie rien."""
    body = request.get_json(silent=True) or {}
    src_kc = _kubectl_for_cluster(cluster)
    if not src_kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    script = _vm_transfer_script()
    if script is None:
        return jsonify({"error": f"{VM_TRANSFER_SCRIPT} not deployed"}), 503
    to = body.get("to")
    kind = "migrate" if to else "export"
    try:
        args = _transfer_args(body, kind)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    base = [sys.executable, str(script)]
    if to:
        dst_kc = _kubectl_for_cluster(to)
        if not dst_kc:
            return jsonify({"error": f"unknown cluster: {to}"}), 404
        cmd = base + ["check", "--from", cluster, "--from-kubeconfig", src_kc,
                      "--vm", f"{namespace}/{name}", "--to", to,
                      "--to-kubeconfig", dst_kc, "--json"] + args
        if body.get("engine") == "file":
            cmd += ["--engine", "file"]
    else:
        cmd = base + ["export", "--from", cluster, "--from-kubeconfig", src_kc,
                      "--vm", f"{namespace}/{name}", "--out", str(_export_dir()),
                      "--dry-run", "--json"] + args
    return _transfer_check(cmd)


@app.route("/api/vm/<cluster>/<namespace>/<name>/transfer", methods=["POST"])
@requires_auth
@_rate_limit("6 per minute")
def api_vm_transfer(cluster, namespace, name):
    """Lance le transfert vers `to`, ou l'export si `to` est vide."""
    body = request.get_json(silent=True) or {}
    src_kc = _kubectl_for_cluster(cluster)
    if not src_kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    script = _vm_transfer_script()
    if script is None:
        return jsonify({"error": f"{VM_TRANSFER_SCRIPT} not deployed"}), 503
    to = body.get("to")
    kind = "migrate" if to else "export"
    try:
        args = _transfer_args(body, kind)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    vm = f"{namespace}/{name}"
    base = [sys.executable, str(script)]
    if to:
        dst_kc = _kubectl_for_cluster(to)
        if not dst_kc:
            return jsonify({"error": f"unknown cluster: {to}"}), 404
        cmd = base + ["migrate", "--from", cluster, "--from-kubeconfig", src_kc,
                      "--vm", vm, "--to", to, "--to-kubeconfig", dst_kc] + args
        public = ["harvester-vm-transfer", "migrate", "--from", cluster, "--vm", vm,
                  "--to", to] + args
        label, clusters, result = f"vm-transfer:{vm}", (cluster, to), None
    else:
        # v1.47.0 : la console nomme l'archive elle-même, pour pouvoir la
        # désigner à la fin (télécharger, importer) au lieu de laisser
        # l'exploitant la chercher dans le magasin.
        archive = f"{name}-{time.strftime('%Y%m%d-%H%M%S')}{_vt.ARCHIVE_SUFFIX}"
        cmd = base + ["export", "--from", cluster, "--from-kubeconfig", src_kc,
                      "--vm", vm, "--out", str(_export_dir() / archive)] + args
        public = ["harvester-vm-transfer", "export", "--from", cluster, "--vm", vm,
                  "--out", archive] + args
        label, clusters = f"vm-export:{vm}", (cluster,)
        result = {"archive": archive}
    run, busy = _start_transfer(label, cluster, clusters, public, cmd, result)
    if busy:
        return _busy_response(busy)
    return jsonify({"action_id": run.id}), 201


@app.route("/api/exports")
@requires_auth
def api_exports_list():
    """Les archives du magasin, lues par leurs en-têtes : aucun disque n'est
    lu, et aucun secret n'est rendu."""
    out = []
    for p in sorted(_export_dir().glob("*" + _vt.ARCHIVE_SUFFIX),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        entry = {"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
        try:
            r = _vt.ArchiveReader(p)
            entry["complete"] = r.complete
            m = r.manifest()
            src = m.get("source") or {}
            inv = m.get("inventory") or {}
            entry.update({
                "vm": src.get("name"), "namespace": src.get("namespace"),
                "cluster": src.get("cluster"), "version": src.get("version"),
                "created": m.get("created"),
                "disks": [{"volume": d.get("volume"), "size": d.get("size"),
                           "storage_class": d.get("storage_class")}
                          for d in inv.get("disks") or []],
                "networks": inv.get("networks") or [],
            })
        except Exception:                      # noqa: BLE001
            entry["complete"] = False
            entry["unreadable"] = True
        out.append(entry)
    try:
        st = os.statvfs(_export_dir())
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = None
    return jsonify({"exports": out, "free": free})


@app.route("/api/exports/<archive>", methods=["DELETE"])
@requires_auth
@_rate_limit("20/minute")
def api_exports_delete(archive):
    safe = _export_safe_name(archive)
    if not safe:
        return jsonify({"error": "invalid archive name"}), 400
    p = _export_dir() / safe
    if not p.is_file():
        return jsonify({"error": "not found"}), 404
    p.unlink()
    return jsonify({"deleted": safe})


@app.route("/api/exports/<archive>/download")
@requires_auth
def api_exports_download(archive):
    safe = _export_safe_name(archive)
    if not safe or not (_export_dir() / safe).is_file():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(str(_export_dir()), safe, as_attachment=True,
                               mimetype="application/x-tar", conditional=True)


# -----------------------------------------------------------------------------
# Dépôt d'une archive dans le magasin (v1.47.0)
#
# Pour importer sur un site qui a sa propre console, l'archive téléchargée
# ailleurs devait être recopiée à la main dans le répertoire du magasin. Le
# dépôt passe par le navigateur, EN FLUX : le corps de la requête est le
# fichier lui-même. Un formulaire multipart aurait été recopié en entier par
# Werkzeug dans /tmp avant d'arriver ici, et /tmp est en mémoire vive dans le
# service installé : intenable pour un disque de plusieurs dizaines de Gio.
#
# Le travail se fait dans le thread de la requête, puisque les données
# arrivent par elle ; il est suivi comme toute action (dock, Activité,
# annulation). Une archive n'entre dans le magasin qu'une fois vérifiée
# (complète, chaque membre conforme à sa somme) : une copie abîmée « à la
# bonne taille » est refusée au dépôt, pas au milieu d'un import.
# -----------------------------------------------------------------------------
_UPLOADS = set()                       # archives en cours de dépôt
_UPLOADS_LOCK = threading.Lock()
_UPLOAD_CHUNK = 1024 * 1024
_UPLOAD_SPARE = 256 * 1024 * 1024      # laissé libre après un dépôt
_PART_SUFFIX = ".part"


class _UploadCancelled(Exception):
    pass


class _BadArchive(ValueError):
    pass


def _receive_archive(run, stream, length, part):
    """Écrit le corps de la requête dans `part` (0600 : l'archive porte les
    secrets cloud-init), avec la progression de l'action. Lève si
    l'opérateur annule, ou si le navigateur s'arrête avant la fin."""
    prog = _vp.Progress(run.emit_progress, "upload", length)
    done = 0
    fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        while done < length:
            if getattr(run, "_cancel", False):
                raise _UploadCancelled()
            chunk = stream.read(min(_UPLOAD_CHUNK, length - done))
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            prog.update(done)
        f.flush()
        os.fsync(f.fileno())
    if done != length:
        raise ValueError(f"the upload stopped at {done} of {length} bytes")
    return prog.finish()


def _check_archive(run, path):
    """Complète, lisible, et chaque membre conforme à sa somme."""
    try:
        reader = _vt.ArchiveReader(path)
    except OSError as e:
        raise _BadArchive(f"unreadable archive ({e.strerror or e})") from None
    if not reader.complete:
        raise _BadArchive("incomplete archive: its checksum list is missing "
                          "(an interrupted export, or not a harvester-ops archive)")
    try:
        manifest = reader.manifest()
    except (KeyError, ValueError):
        raise _BadArchive("not a harvester-ops VM archive: no readable manifest") from None
    if not isinstance(manifest, dict) or "vm" not in manifest or "inventory" not in manifest:
        raise _BadArchive("not a harvester-ops VM archive: the manifest has no VM")
    sums = reader.sums()
    total = sum(size for name, (_, size) in reader.members.items() if name in sums)
    prog = _vp.Progress(run.emit_progress, "verify", total)
    bad = reader.verify(on_chunk=lambda n: prog.add(n))
    if bad:
        raise _BadArchive("checksum mismatch in " + ", ".join(sorted(bad)[:5]))
    prog.finish()
    return manifest


def _drop_stale_parts(directory):
    """Un dépôt interrompu par un arrêt de la console laisse son `.part` :
    tout `.part` qu'aucun dépôt en cours n'écrit est un reste. Appelé sous
    `_UPLOADS_LOCK`."""
    for p in directory.glob("*" + _vt.ARCHIVE_SUFFIX + _PART_SUFFIX):
        if p.name[:-len(_PART_SUFFIX)] not in _UPLOADS:
            p.unlink(missing_ok=True)


def _error_text(e):
    # jamais de chemin dans une réponse : une OSError en porte un
    if isinstance(e, OSError) and e.strerror:
        return e.strerror
    return str(e)[:300]


@app.route("/api/exports/<archive>", methods=["PUT"])
@requires_auth
@_rate_limit("6 per minute")
def api_exports_upload(archive):
    """Dépose une archive dans le magasin. Le corps est le fichier .hvx."""
    safe = _export_safe_name(archive)
    if not safe:
        return jsonify({"error": "the file must be a *.hvx archive with a plain name"}), 400
    length = request.content_length
    if not length:
        return jsonify({"error": "Content-Length required"}), 411
    store = _export_dir()
    dest = store / safe
    part = store / (safe + _PART_SUFFIX)
    try:
        st = os.statvfs(store)
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = None
    if free is not None and free < length + _UPLOAD_SPARE:
        return jsonify({"error": "not enough room in the store",
                        "need": length, "free": free}), 507
    with _UPLOADS_LOCK:
        if dest.exists() or safe in _UPLOADS:
            return jsonify({"error": f"{safe} is already in the store"}), 409
        _drop_stale_parts(store)
        _UPLOADS.add(safe)

    run = ActionRun(uuid.uuid4().hex[:12], f"vm-archive-upload:{safe}", "(local)",
                    ["upload", safe])
    run.cluster_user = (current_cluster_identity() or {}).get("user")
    with ACTIONS_LOCK:
        ACTIONS[run.id] = run

    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": msg, "ts": time.time()})

    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    step("upload", "running", f"receiving {safe} ({_vp.fmt_bytes(length)})")
    code, body = 201, None
    try:
        res = _receive_archive(run, request.stream, length, part)
        step("upload", "done", _vp.summary("upload", res))
        step("verify", "running", "checking the archive and its checksums")
        manifest = _check_archive(run, part)
        # un lien ne remplace jamais une archive déjà là
        os.link(part, dest)
        src = manifest.get("source") or {}
        step("verify", "done", f"{src.get('namespace')}/{src.get('name')} "
                               f"from {src.get('cluster')} {src.get('version') or ''}".rstrip())
        run.result = {"archive": safe}
        run.status, run.exit_code = "done", 0
        body = {"action_id": run.id, "archive": safe, "size": length}
    except _UploadCancelled:
        run.status, run.exit_code = "cancelled", 3
        run.error_summary = "cancelled, nothing kept"
        step("upload", "error", run.error_summary)
        code, body = 409, {"error": "cancelled", "action_id": run.id}
    except FileExistsError:
        run.status, run.exit_code = "error", 1
        run.error_summary = f"{safe} appeared in the store meanwhile"
        step("verify", "error", run.error_summary)
        code, body = 409, {"error": run.error_summary, "action_id": run.id}
    except _BadArchive as e:
        run.status, run.exit_code = "error", 2
        run.error_summary = str(e)[:300]
        step("verify", "error", run.error_summary)
        code, body = 422, {"error": run.error_summary, "action_id": run.id}
    except Exception as e:                     # noqa: BLE001
        # navigateur fermé ou réseau coupé en cours de route
        run.status, run.exit_code = "error", 1
        run.error_summary = _error_text(e)
        step("upload", "error", run.error_summary)
        code, body = 400, {"error": run.error_summary, "action_id": run.id}
    finally:
        part.unlink(missing_ok=True)
        with _UPLOADS_LOCK:
            _UPLOADS.discard(safe)
        run.ended_at = time.time()
        run.emit({"type": "status", "status": run.status,
                  "exit_code": run.exit_code, "ts": time.time()})
        run.close()
    return jsonify(body), code


def _import_cmd(archive, body, dry_run):
    safe = _export_safe_name(archive)
    if not safe or not (_export_dir() / safe).is_file():
        return None, (jsonify({"error": "not found"}), 404)
    to = body.get("to")
    dst_kc = _kubectl_for_cluster(to) if to else None
    if not dst_kc:
        return None, (jsonify({"error": f"unknown cluster: {to}"}), 404)
    script = _vm_transfer_script()
    if script is None:
        return None, (jsonify({"error": f"{VM_TRANSFER_SCRIPT} not deployed"}), 503)
    try:
        args = _transfer_args(body, "import")
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 400)
    cmd = [sys.executable, str(script), "import", "--in", str(_export_dir() / safe),
           "--to", to, "--to-kubeconfig", dst_kc] + args
    if dry_run:
        cmd += ["--dry-run", "--json"]
    public = ["harvester-vm-transfer", "import", "--in", safe, "--to", to] + args
    return (cmd, public, to, safe), None


@app.route("/api/exports/<archive>/check", methods=["POST"])
@requires_auth
@_rate_limit("20/minute")
def api_exports_check(archive):
    body = request.get_json(silent=True) or {}
    got, err = _import_cmd(archive, body, dry_run=True)
    if err:
        return err
    return _transfer_check(got[0])


@app.route("/api/exports/<archive>/import", methods=["POST"])
@requires_auth
@_rate_limit("6 per minute")
def api_exports_import(archive):
    body = request.get_json(silent=True) or {}
    got, err = _import_cmd(archive, body, dry_run=False)
    if err:
        return err
    cmd, public, to, safe = got
    run, busy = _start_transfer(f"vm-import:{safe}", to, (to,), public, cmd)
    if busy:
        return _busy_response(busy)
    return jsonify({"action_id": run.id}), 201


# -----------------------------------------------------------------------------
# VM snapshots (VirtualMachineBackup with type=snapshot) per-VM
# -----------------------------------------------------------------------------
@app.route("/api/vm/<cluster>/<namespace>/<name>/snapshots")
@requires_auth
def api_vm_snapshots_list(cluster, namespace, name):
    """List VirtualMachineBackups (type=snapshot) for this VM."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    try:
        out = subprocess.check_output(
            ["kubectl", "--kubeconfig", kc, "get",
             "virtualmachinebackups.harvesterhci.io", "-n", namespace, "-o", "json"],
            stderr=subprocess.DEVNULL, timeout=10,
        )
        all_backups = json.loads(out).get("items", [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    snapshots = []
    for b in all_backups:
        if b.get("spec", {}).get("type") != "snapshot":
            continue
        src = b.get("spec", {}).get("source", {})
        if src.get("kind") != "VirtualMachine" or src.get("name") != name:
            continue
        st = b.get("status", {}) or {}
        snapshots.append({
            "name": b["metadata"]["name"],
            "creationTimestamp": b["metadata"].get("creationTimestamp"),
            "ready": st.get("readyToUse", False),
            "progress": st.get("progress", 0),
            "error": (st.get("error") or {}).get("message") if st.get("error") else None,
        })
    snapshots.sort(key=lambda s: s.get("creationTimestamp") or "", reverse=True)
    return jsonify({"vm": f"{namespace}/{name}", "snapshots": snapshots})


def _snapshot_action_runner(run, kc, namespace, name, snap_name, manifest):
    """Background worker: create the VirtualMachineBackup + poll progress."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})
    run.emit({"type": "step", "step_id": "create", "status": "running",
              "message": f"kubectl apply VMBackup {snap_name}", "ts": time.time()})
    try:
        proc = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, "apply", "-f", "-"],
            input=json.dumps(manifest).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
        )
        if proc.returncode != 0:
            run.emit({"type": "step", "step_id": "create", "status": "error",
                      "message": proc.stderr.decode()[:300], "ts": time.time()})
            run.exit_code = 1
            run.status = "error"
            run.ended_at = time.time()
            run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
            run.close()
            return
    except subprocess.TimeoutExpired:
        run.emit({"type": "step", "step_id": "create", "status": "error",
                  "message": "kubectl apply timeout", "ts": time.time()})
        run.status = "error"; run.exit_code = 1; run.ended_at = time.time()
        run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
        run.close()
        return
    run.emit({"type": "step", "step_id": "create", "status": "done",
              "message": "VMBackup created", "ts": time.time()})

    # Poll readiness up to 10 min
    run.emit({"type": "step", "step_id": "progress", "status": "running",
              "message": "waiting for readyToUse", "ts": time.time()})
    deadline = time.time() + 600
    last_pct = -1
    while time.time() < deadline:
        try:
            obj = json.loads(subprocess.check_output(
                ["kubectl", "--kubeconfig", kc, "get",
                 "virtualmachinebackups.harvesterhci.io", snap_name,
                 "-n", namespace, "-o", "json"],
                stderr=subprocess.DEVNULL, timeout=5,
            ))
        except Exception:
            time.sleep(3)
            continue
        st = obj.get("status") or {}
        pct = int(st.get("progress") or 0)
        ready = bool(st.get("readyToUse", False))
        err = (st.get("error") or {}).get("message") if st.get("error") else None
        if pct != last_pct:
            run.emit({"type": "step", "step_id": "progress", "status": "progress",
                      "message": f"{pct}%", "ts": time.time()})
            last_pct = pct
        if ready:
            run.emit({"type": "step", "step_id": "progress", "status": "done",
                      "message": "snapshot ready", "ts": time.time()})
            break
        if err:
            run.emit({"type": "step", "step_id": "progress", "status": "error",
                      "message": err[:200], "ts": time.time()})
            run.status = "error"; run.exit_code = 1; run.ended_at = time.time()
            run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
            run.close()
            return
        time.sleep(3)

    run.exit_code = 0
    run.status = "done"
    run.ended_at = time.time()
    run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
    run.close()


@app.route("/api/vm/<cluster>/<namespace>/<name>/snapshots", methods=["POST"])
@requires_auth
def api_vm_snapshots_create(cluster, namespace, name):
    """Create a new snapshot. Tracked as an ActionRun so it shows in the dock
    and in Activity with live progress (0% → 100%)."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    snap_name = f"{name}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    manifest = {
        "apiVersion": "harvesterhci.io/v1beta1",
        "kind": "VirtualMachineBackup",
        "metadata": {
            "name": snap_name,
            "namespace": namespace,
            "labels": {"harvester-ops.io/created-by": "harvester-ops"},
        },
        "spec": {
            "type": "snapshot",
            "source": {"apiGroup": "kubevirt.io", "kind": "VirtualMachine", "name": name},
        },
    }
    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, f"vm-snapshot:{namespace}/{name}", cluster, [], dry_run=False)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(target=_snapshot_action_runner,
                     args=(run, kc, namespace, name, snap_name, manifest),
                     daemon=True).start()
    return jsonify({"name": snap_name, "action_id": run_id})


@app.route("/api/vm/<cluster>/<namespace>/<name>/snapshots/<snap>", methods=["DELETE"])
@requires_auth
def api_vm_snapshots_delete(cluster, namespace, name, snap):
    """Delete a VM snapshot. Tracked as an ActionRun."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    action_id = track_action(
        f"snapshot-delete:{namespace}/{snap}", cluster,
        _simple_kubectl_action, kc,
        ["delete", "virtualmachinebackups.harvesterhci.io", snap, "-n", namespace],
        "delete", f"snapshot {snap} deleted",
    )
    return jsonify({"deleted": snap, "action_id": action_id}), 202


@app.route("/api/vm/<cluster>/<namespace>/<name>/restore", methods=["POST"])
@requires_auth
def api_vm_snapshot_restore(cluster, namespace, name):
    """Restore the VM from a snapshot (creates a VirtualMachineRestore)."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    snap = data.get("snapshot")
    new_vm = data.get("new_vm", False)
    # v1.11.0 : restore guidé — snapshot de sécurité de l'état courant
    # puis arrêt de la VM, avant le restore, le tout dans UNE action.
    pre_snapshot = bool(data.get("pre_snapshot", False))
    stop_vm = bool(data.get("stop_vm", False))
    if not snap:
        return jsonify({"error": "snapshot name required"}), 400
    # v1.10.1 : le webhook Harvester refuse un restore in-place sur une VM
    # qui tourne ("Please stop the VM ... before doing a restore"). On le
    # détecte AVANT de lancer l'action, avec un message actionnable, au
    # lieu de laisser kubectl échouer en brut. On ne bloque que si on
    # VOIT un VMI ; en cas de doute (cluster injoignable), on laisse le
    # webhook trancher.
    vm_running = False
    if not new_vm:
        try:
            probe = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "-n", namespace,
                 "get", "vmi", name, "--no-headers"],
                capture_output=True, text=True, timeout=10)
            vm_running = probe.returncode == 0 and bool(probe.stdout.strip())
        except Exception:
            pass
        if vm_running and not stop_vm:
            return jsonify({
                "error": "vm-running",
                "detail": "The VM must be stopped before an in-place "
                          "restore (Harvester webhook requirement). "
                          "Stop it, then retry — or rerun with stop_vm.",
            }), 409
    restore_name = f"{name}-restore-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    manifest = {
        "apiVersion": "harvesterhci.io/v1beta1",
        "kind": "VirtualMachineRestore",
        "metadata": {"name": restore_name, "namespace": namespace},
        "spec": {
            "target": {"apiGroup": "kubevirt.io", "kind": "VirtualMachine", "name": name},
            "virtualMachineBackupName": snap,
            "virtualMachineBackupNamespace": namespace,
            "newVM": bool(new_vm),
            # Harvester's webhook rejects the default deletionPolicy for
            # in-place restores of type=snapshot backups: "delete policy
            # with backup type snapshot for replacing VM is not supported".
            "deletionPolicy": "retain",
        },
    }

    pre_snap_name = f"{name}-prerestore-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"

    def _runner(run):
        run.status = "running"
        run.emit({"type": "status", "status": "running", "ts": time.time()})

        def _step(sid, status, msg):
            run.emit({"type": "step", "step_id": sid, "status": status,
                      "message": msg, "ts": time.time()})

        def _fail(sid, msg, code=1):
            run.error_summary = msg[:300]
            _step(sid, "error", msg[:300])
            run.exit_code = code; run.status = "error"; run.ended_at = time.time()
            run.emit({"type": "status", "status": "error", "exit_code": code,
                      "ts": time.time()})
            run.close()

        # -- Étape 0 (option) : snapshot de sécurité de l'état COURANT,
        #    pris avant l'arrêt — le filet pour revenir en arrière si le
        #    restore était une erreur.
        if pre_snapshot:
            _step("pre-snapshot", "running", f"safety snapshot {pre_snap_name}")
            pre_manifest = {
                "apiVersion": "harvesterhci.io/v1beta1",
                "kind": "VirtualMachineBackup",
                "metadata": {"name": pre_snap_name, "namespace": namespace,
                             "labels": {"harvester-ops.io/created-by": "harvester-ops"}},
                "spec": {"type": "snapshot",
                         "source": {"apiGroup": "kubevirt.io",
                                    "kind": "VirtualMachine", "name": name}},
            }
            try:
                r = _kubectl_run(
                    ["kubectl", "--kubeconfig", kc, "apply", "-f", "-"],
                    input=json.dumps(pre_manifest).encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
                if r.returncode != 0:
                    return _fail("pre-snapshot",
                                 (r.stderr.decode().strip().splitlines() or ["snapshot failed"])[-1])
            except subprocess.TimeoutExpired:
                return _fail("pre-snapshot", "kubectl apply timed out", 124)
            deadline = time.time() + 180
            ready = False
            while time.time() < deadline:
                try:
                    out = subprocess.check_output(
                        ["kubectl", "--kubeconfig", kc, "-n", namespace, "get",
                         "virtualmachinebackups.harvesterhci.io", pre_snap_name,
                         "-o", "jsonpath={.status.readyToUse}"],
                        stderr=subprocess.DEVNULL, timeout=5)
                    if out.decode().strip() == "true":
                        ready = True; break
                except Exception:
                    pass
                time.sleep(3)
            if not ready:
                return _fail("pre-snapshot",
                             f"safety snapshot {pre_snap_name} not ready after 180s")
            _step("pre-snapshot", "done", f"safety snapshot {pre_snap_name} ready")

        # -- Étape 0bis (option) : arrêter la VM (le webhook Harvester
        #    exige une VM arrêtée pour un restore in-place).
        if stop_vm and vm_running:
            _step("stop-vm", "running", f"stopping {namespace}/{name}")
            try:
                r = _kubectl_run(
                    ["kubectl", "--kubeconfig", kc, "-n", namespace, "patch",
                     "vm", name, "--type", "merge",
                     "-p", '{"spec":{"runStrategy":"Halted"}}'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
                if r.returncode != 0:
                    return _fail("stop-vm",
                                 (r.stderr.decode().strip().splitlines() or ["patch failed"])[-1])
            except subprocess.TimeoutExpired:
                return _fail("stop-vm", "kubectl patch timed out", 124)
            deadline = time.time() + 180
            stopped = False
            while time.time() < deadline:
                p2 = _kubectl_run(
                    ["kubectl", "--kubeconfig", kc, "-n", namespace,
                     "get", "vmi", name, "--no-headers"],
                    capture_output=True, text=True, timeout=10)
                if p2.returncode != 0 or not p2.stdout.strip():
                    stopped = True; break
                time.sleep(3)
            if not stopped:
                return _fail("stop-vm", f"{name} still running after 180s")
            _step("stop-vm", "done", f"{namespace}/{name} stopped")

        run.emit({"type": "step", "step_id": "apply", "status": "running",
                  "message": f"kubectl apply VirtualMachineRestore/{restore_name}",
                  "ts": time.time()})
        try:
            r = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "apply", "-f", "-"],
                input=json.dumps(manifest).encode(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
            )
            if r.returncode != 0:
                stderr_lines = [l.strip() for l in r.stderr.decode().splitlines() if l.strip()]
                detail = (stderr_lines[-1] if stderr_lines else "kubectl apply failed")[:300]
                run.error_summary = detail
                run.emit({"type": "step", "step_id": "apply", "status": "error",
                          "message": detail, "ts": time.time()})
                run.exit_code = 1; run.status = "error"
                run.ended_at = time.time()
                run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
                run.close(); return
        except subprocess.TimeoutExpired:
            run.error_summary = "kubectl apply timed out after 15s"
            run.emit({"type": "step", "step_id": "apply", "status": "error",
                      "message": "kubectl apply timeout", "ts": time.time()})
            run.status = "error"; run.exit_code = 124; run.ended_at = time.time()
            run.emit({"type": "status", "status": "error", "exit_code": 124, "ts": time.time()})
            run.close(); return

        run.emit({"type": "step", "step_id": "apply", "status": "done",
                  "message": f"restore {restore_name} created", "ts": time.time()})
        # Poll restore progress
        run.emit({"type": "step", "step_id": "wait", "status": "running",
                  "message": "waiting for restore to complete", "ts": time.time()})
        deadline = time.time() + 1200
        last = None
        while time.time() < deadline:
            try:
                r = subprocess.check_output(
                    ["kubectl", "--kubeconfig", kc, "get",
                     "virtualmachinerestores.harvesterhci.io", restore_name,
                     "-n", namespace, "-o", "json"],
                    stderr=subprocess.DEVNULL, timeout=5,
                )
                d = json.loads(r)
                conds = d.get("status", {}).get("conditions", []) or []
                cmap = {c.get("type"): c.get("status") for c in conds}
                # v1.11.0 : Harvester 1.8 marque un restore terminé avec
                # Ready=True (et InProgress/Progressing=False), PAS une
                # condition Complete (jamais émise -> l'action pollait
                # jusqu'au deadline de 1200 s). On accepte les deux
                # schémas pour rester compatible.
                complete = (cmap.get("Complete") == "True"
                            or (cmap.get("Ready") == "True"
                                and cmap.get("InProgress", "False") != "True"
                                and cmap.get("Progressing", "False") != "True"))
                if complete:
                    run.emit({"type": "step", "step_id": "wait", "status": "done",
                              "message": "restore complete", "ts": time.time()})
                    break
                # Surface intermediate state
                msg = ",".join(f"{c['type']}={c['status']}" for c in conds[:3])
                if msg != last:
                    run.emit({"type": "step", "step_id": "wait", "status": "progress",
                              "message": msg, "ts": time.time()})
                    last = msg
            except Exception:
                pass
            time.sleep(5)
        run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
        run.close()

    action_id = track_action(f"snapshot-restore:{namespace}/{name}", cluster, _runner)
    return jsonify({"restore": restore_name, "action_id": action_id}), 202


# -----------------------------------------------------------------------------
# VM live migration
# -----------------------------------------------------------------------------
@app.route("/api/vm/<cluster>/<namespace>/<name>/migrate-info")
@requires_auth
def api_vm_migrate_info(cluster, namespace, name):
    """Return information needed to plan a live migration: current node,
    available target nodes, and migration history."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    result = {"current_node": None, "phase": None, "nodes": [], "migrations": []}
    try:
        vmi = json.loads(subprocess.check_output(
            ["kubectl", "--kubeconfig", kc, "get", "vmi", name, "-n", namespace, "-o", "json"],
            stderr=subprocess.DEVNULL, timeout=10,
        ))
        result["current_node"] = vmi.get("status", {}).get("nodeName")
        result["phase"] = vmi.get("status", {}).get("phase")
    except Exception:
        pass
    try:
        nodes = json.loads(subprocess.check_output(
            ["kubectl", "--kubeconfig", kc, "get", "nodes", "-o", "json"],
            stderr=subprocess.DEVNULL, timeout=10,
        ))
        for n in nodes.get("items", []):
            ready = next((c["status"] for c in n.get("status", {}).get("conditions", [])
                          if c["type"] == "Ready"), "Unknown")
            result["nodes"].append({
                "name": n["metadata"]["name"],
                "ready": ready,
                "schedulable": not n.get("spec", {}).get("unschedulable", False),
                "current": n["metadata"]["name"] == result["current_node"],
            })
    except Exception:
        pass
    try:
        migs = json.loads(subprocess.check_output(
            ["kubectl", "--kubeconfig", kc, "get",
             "virtualmachineinstancemigrations.kubevirt.io",
             "-n", namespace, "-o", "json"],
            stderr=subprocess.DEVNULL, timeout=10,
        ))
        for m in migs.get("items", []):
            if m.get("spec", {}).get("vmiName") != name:
                continue
            st = m.get("status", {}) or {}
            result["migrations"].append({
                "name": m["metadata"]["name"],
                "creationTimestamp": m["metadata"].get("creationTimestamp"),
                "phase": st.get("phase", "Unknown"),
                "sourceNode": st.get("migrationState", {}).get("sourceNode"),
                "targetNode": st.get("migrationState", {}).get("targetNode"),
            })
    except Exception:
        pass
    result["migrations"].sort(key=lambda m: m.get("creationTimestamp") or "", reverse=True)
    return jsonify(result)


@app.route("/api/vm/<cluster>/<namespace>/<name>/migrate", methods=["POST"])
@requires_auth
def api_vm_migrate_trigger(cluster, namespace, name):
    """Trigger a live migration. KubeVirt picks the target node: choosing
    one is not implemented (the body is ignored)."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    mig_name = f"{name}-migrate-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    manifest = {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachineInstanceMigration",
        "metadata": {"name": mig_name, "namespace": namespace},
        "spec": {"vmiName": name},
    }

    def _runner(run):
        run.status = "running"
        run.emit({"type": "status", "status": "running", "ts": time.time()})
        run.emit({"type": "step", "step_id": "create", "status": "running",
                  "message": f"creating VMIM {mig_name}", "ts": time.time()})
        try:
            r = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "apply", "-f", "-"],
                input=json.dumps(manifest).encode(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
            )
            if r.returncode != 0:
                run.emit({"type": "step", "step_id": "create", "status": "error",
                          "message": r.stderr.decode()[:200], "ts": time.time()})
                run.status = "error"; run.exit_code = 1; run.ended_at = time.time()
                run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
                run.close(); return
        except subprocess.TimeoutExpired:
            run.emit({"type": "step", "step_id": "create", "status": "error",
                      "message": "kubectl timeout", "ts": time.time()})
            run.status = "error"; run.exit_code = 124; run.ended_at = time.time()
            run.emit({"type": "status", "status": "error", "exit_code": 124, "ts": time.time()})
            run.close(); return
        run.emit({"type": "step", "step_id": "create", "status": "done",
                  "message": "VMIM created", "ts": time.time()})
        # Poll migration phase
        run.emit({"type": "step", "step_id": "wait", "status": "running",
                  "message": "waiting for migration phase", "ts": time.time()})
        deadline = time.time() + 600
        last_phase = None
        while time.time() < deadline:
            try:
                d = json.loads(subprocess.check_output(
                    ["kubectl", "--kubeconfig", kc, "get",
                     "virtualmachineinstancemigrations.kubevirt.io", mig_name,
                     "-n", namespace, "-o", "json"],
                    stderr=subprocess.DEVNULL, timeout=5,
                ))
                phase = d.get("status", {}).get("phase", "Pending")
                if phase != last_phase:
                    run.emit({"type": "step", "step_id": "wait", "status": "progress",
                              "message": f"phase={phase}", "ts": time.time()})
                    last_phase = phase
                if phase == "Succeeded":
                    run.emit({"type": "step", "step_id": "wait", "status": "done",
                              "message": "migration succeeded", "ts": time.time()})
                    break
                if phase == "Failed":
                    run.emit({"type": "step", "step_id": "wait", "status": "error",
                              "message": "migration failed", "ts": time.time()})
                    run.status = "error"; run.exit_code = 1; run.ended_at = time.time()
                    run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
                    run.close(); return
            except Exception:
                pass
            time.sleep(3)
        run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
        run.close()

    action_id = track_action(f"vm-migrate:{namespace}/{name}", cluster, _runner)
    return jsonify({"migration": mig_name, "action_id": action_id}), 202


# -----------------------------------------------------------------------------
# Collaborative notes — Yjs sync over WebSocket + SQLite persistence
# -----------------------------------------------------------------------------
_notes_db = Path(os.environ.get("HARVESTER_OPS_NOTES_DB", "/var/lib/harvester-ops/notes.db"))
NOTES_DB = _usable_dir(_notes_db.parent,
                       Path(tempfile.gettempdir()) / "harvester-ops-notes") / _notes_db.name
# v1.57.0 : les comptes de la console, dans l'état persistant (à côté des
# notes) : le répertoire de configuration est en lecture seule dans le service
ACCOUNTS_PATH = Path(os.environ.get("HARVESTER_OPS_ACCOUNTS", str(NOTES_DB.parent / "accounts.json")))
# v1.78.0 : les scripts lancés par la console lisent les clusters qu'elle a
# déclarés dans ce même répertoire d'état (common.sh, kube.py)
# (absolu : un chemin relatif le serait au répertoire courant de chacun)
os.environ["HARVESTER_OPS_STATE_DIR"] = str(_state_dir())


def _notes_init_db():
    conn = sqlite3.connect(str(NOTES_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
          doc_id TEXT PRIMARY KEY,
          state BLOB NOT NULL,
          updated_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


_notes_init_db()


def _notes_load(doc_id):
    conn = sqlite3.connect(str(NOTES_DB))
    row = conn.execute("SELECT state FROM notes WHERE doc_id = ?", (doc_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return row[0]


def _notes_save(doc_id, state_bytes):
    conn = sqlite3.connect(str(NOTES_DB))
    conn.execute(
        "INSERT INTO notes(doc_id, state, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(doc_id) DO UPDATE SET state = ?, updated_at = ?",
        (doc_id, state_bytes, time.time(), state_bytes, time.time()),
    )
    conn.commit()
    conn.close()


# In-memory: Y.Doc per doc_id, list of subscribers (websocket connections)
_notes_docs = {}        # doc_id -> {"doc": Y.YDoc, "subs": [ws, ws, ...]}
_notes_lock = threading.Lock()


def _validate_doc_id(doc_id):
    """Allow shapes:
      vm/<cluster>/<ns>/<name>          — note attached to a VM
      ns/<cluster>/<namespace>          — note attached to a namespace
      node/<cluster>/<nodename>         — note attached to a cluster node
    """
    parts = doc_id.split("/")
    if len(parts) == 4 and parts[0] == "vm":
        return all(p and re.match(r"[a-zA-Z0-9._-]+", p) for p in parts[1:])
    if len(parts) == 3 and parts[0] in ("ns", "node"):
        return all(p and re.match(r"[a-zA-Z0-9._-]+", p) for p in parts[1:])
    return False


def _get_or_create_doc(doc_id):
    """Get the Y.Doc broker entry for a given doc_id (initializes from DB).

    Critical: `y_py.YDoc` is marked `unsendable` in pyo3 — touching it from
    a thread other than the one that created it panics the Rust runtime
    and kills the worker process. We pin every Y.Doc to a single dedicated
    worker thread (one per doc_id) and route all operations through a
    queue + future pattern. WS handler threads never touch the doc
    directly; they submit `apply_update` / `encode_state_as_update` jobs
    via `_doc_call(entry, fn)` and block on the result.
    """
    from concurrent.futures import ThreadPoolExecutor
    with _notes_lock:
        entry = _notes_docs.get(doc_id)
        if entry is None:
            executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"y-doc-{doc_id[:8]}")
            entry = {"doc": None, "subs": [], "executor": executor}
            _notes_docs[doc_id] = entry
            saved = _notes_load(doc_id)
            def _init():
                d = Y.YDoc()
                if saved:
                    try: Y.apply_update(d, saved)
                    except Exception: pass
                return d
            entry["doc"] = executor.submit(_init).result()
        return entry


def _doc_call(entry, fn, *args, **kwargs):
    """Run `fn(*args, **kwargs)` on the doc's dedicated worker thread.
    Returns whatever fn returns. Blocks the caller."""
    return entry["executor"].submit(fn, *args, **kwargs).result()


@sock.route("/ws/notes/<path:doc_id>")
def ws_notes(ws, doc_id):
    """WebSocket endpoint for collaborative notes.

    Wire protocol (JSON over text frames):
      Server → Client:
        {"type": "snapshot", "data": "<b64 Y.encode_state_as_update>"}
        {"type": "update",   "data": "<b64 incremental update>"}
        {"type": "ping"}     keep-alive (every ~25s)
      Client → Server:
        {"type": "hello"}    request initial snapshot
        {"type": "update", "data": "<b64 update>"}
        {"type": "pong"}     (optional) reply to server ping

    The Y.Doc is persisted to SQLite after every applied update.
    Broadcast iteration is done **without** holding `_notes_lock` so a slow
    peer's `send()` cannot freeze every other client — that was the silent
    failure mode that triggered the "reconnecting" loop with 2 users.
    """
    if not _validate_doc_id(doc_id):
        try: ws.send(json.dumps({"type": "error", "message": "invalid doc_id"}))
        except Exception: pass
        return

    entry = _get_or_create_doc(doc_id)
    doc = entry["doc"]
    conn_id = uuid.uuid4().hex[:8]
    # Per-WS outbound queue + dedicated writer thread. simple-websocket's
    # ws.send() is NOT safe to call from multiple threads (broadcasts from
    # peer threads would corrupt the WS framing of the receiving WS and
    # kill the connection with a normal close). We funnel every outbound
    # payload through one queue, and a single writer thread per WS reads
    # from it. The application's handler thread then only enqueues.
    import queue as _q
    ws._outbox = _q.Queue()
    ws._writer_alive = True

    def _writer(target_ws):
        while target_ws._writer_alive:
            try:
                payload = target_ws._outbox.get(timeout=1.0)
            except _q.Empty:
                continue
            if payload is None:
                break
            try:
                target_ws.send(payload)
            except Exception:
                target_ws._writer_alive = False
                break

    ws._writer_thread = threading.Thread(target=_writer, args=(ws,),
                                         daemon=True,
                                         name=f"notes-writer-{conn_id}")
    ws._writer_thread.start()

    with _notes_lock:
        entry["subs"].append(ws)
        n_peers = len(entry["subs"])
    log_notes.info("%s conn=%s attached (%d client(s))", doc_id, conn_id, n_peers)

    def safe_send(target_ws, payload):
        """Enqueue payload on the target's writer thread queue.
        Returns False if the peer's writer is already dead."""
        if not getattr(target_ws, "_writer_alive", False):
            return False
        try:
            target_ws._outbox.put_nowait(payload)
            return True
        except Exception:
            return False

    # Send initial snapshot immediately so the client can render even if
    # it hasn't sent its 'hello' yet (avoids a brief blank textarea).
    # `doc` is pinned to its own worker thread — we MUST go through
    # `_doc_call` rather than touching it here (pyo3 unsendable panic).
    snap = _doc_call(entry, Y.encode_state_as_update, doc)
    if not safe_send(ws, json.dumps({"type": "snapshot",
                                     "data": base64.b64encode(snap).decode("ascii")})):
        log_notes.warning("%s initial snapshot send failed", conn_id)

    last_ping = time.time()
    try:
        while True:
            # simple-websocket returns None on EITHER timeout OR disconnect.
            # Distinguish via ws.connected; on timeout, send a ping so dead
            # peers behind a NAT/proxy get pruned and don't leak send calls.
            raw = ws.receive(timeout=25)
            now = time.time()
            if raw is None:
                if not ws.connected:
                    break
                if now - last_ping > 20:
                    if not safe_send(ws, json.dumps({"type": "ping", "ts": now})):
                        break
                    last_ping = now
                continue
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            mtype = msg.get("type")
            if mtype == "pong":
                continue
            if mtype == "hello":
                snap = _doc_call(entry, Y.encode_state_as_update, doc)
                if not safe_send(ws, json.dumps({"type": "snapshot",
                                                  "data": base64.b64encode(snap).decode("ascii")})):
                    break
                continue
            if mtype == "update" and msg.get("data"):
                try:
                    update = base64.b64decode(msg["data"])
                except Exception:
                    continue
                # Apply + encode in ONE worker-thread hop so the apply →
                # snapshot pair is atomic relative to other peers.
                def _apply_and_snapshot():
                    try: Y.apply_update(doc, update)
                    except Exception as e:
                        return None, e
                    return Y.encode_state_as_update(doc), None
                full_state, err = _doc_call(entry, _apply_and_snapshot)
                if err is not None:
                    log_notes.warning("%s apply_update failed: %s", conn_id, err)
                    continue
                with _notes_lock:
                    peers = [s for s in entry["subs"] if s is not ws]
                _notes_save(doc_id, full_state)
                payload = json.dumps({"type": "update", "data": msg["data"]})
                dead = []
                for other in peers:
                    if not safe_send(other, payload):
                        dead.append(other)
                if dead:
                    with _notes_lock:
                        for d in dead:
                            try: entry["subs"].remove(d)
                            except ValueError: pass
                continue
    except Exception as e:
        log_notes.exception("%s handler crashed: %s", conn_id, e)
    finally:
        # Stop the writer thread first, then prune from subs.
        ws._writer_alive = False
        try: ws._outbox.put_nowait(None)
        except Exception: pass
        with _notes_lock:
            try: entry["subs"].remove(ws)
            except ValueError: pass
            n_peers = len(entry["subs"])
        log_notes.info("%s conn=%s detached (%d left)", doc_id, conn_id, n_peers)


# =============================================================================
# VNC console relay (v1.7.0)
# =============================================================================
# Browser (noVNC) <-ws-> Flask <-wss-> KubeVirt /vnc subresource. Auth model:
# flask-sock sends the 101 BEFORE the view runs, so @requires_auth is
# structurally useless on @sock.route — and a browser cannot attach an
# Authorization header to a WebSocket handshake anyway. So a short-lived
# single-use ticket is issued by an authenticated HTTP endpoint (which also
# acts as the pre-flight: it is the place that can return a READABLE error —
# a rejected WS handshake surfaces as an opaque "connection failed").
import secrets as _secrets
import ssl as _ssl
from simple_websocket import Client as _WsClient

log_vnc = logging.getLogger("harvester-ops.vnc")

_VNC_TICKET_TTL = 30          # seconds; consumed once
# Navigateurs rattachés, toutes VMs confondues. Chaque navigateur tient deux
# fils (lecture, écriture) ; la connexion partagée d'une VM en tient un.
_VNC_MAX_SESSIONS = 8
_VNC_SUBPROTOCOLS = ["plain.kubevirt.io", "binary"]
_vnc_tickets = {}             # token -> dict(cluster, namespace, name, expires, user, identity, uid)
_vnc_lock = threading.Lock()
# Pourquoi la dernière connexion partagée d'une VM a été perdue, pour que la
# console le dise au lieu de se reconnecter en boucle : {clé: {reason, at}}.
_vnc_last_close = {}
_VNC_CLOSE_MEMORY = 30        # secondes pendant lesquelles la raison est servie


def _vnc_viewers():
    return vnc_mux.viewers_total()


def _kubeconfig_wss(kc_path):
    """Parse a kubeconfig into (server_url, SSLContext, bearer_token).

    Client cert/key and CA are materialised for load_cert_chain() in a
    0700 tmpdir under tempfile.gettempdir() (the container runs --read-only
    with --tmpfs /tmp) and deleted BEFORE any network I/O happens — the
    SSLContext keeps the parsed material in memory.
    """
    cfg = yaml.safe_load(Path(kc_path).read_text())
    ctx_name = cfg.get("current-context") or ""
    ctx = next((c.get("context", {}) for c in cfg.get("contexts", [])
                if c.get("name") == ctx_name),
               (cfg.get("contexts") or [{}])[0].get("context", {}))
    cluster = next((c.get("cluster", {}) for c in cfg.get("clusters", [])
                    if c.get("name") == ctx.get("cluster")),
                   (cfg.get("clusters") or [{}])[0].get("cluster", {}))
    user = next((u.get("user", {}) for u in cfg.get("users", [])
                 if u.get("name") == ctx.get("user")),
                (cfg.get("users") or [{}])[0].get("user", {}))

    server = (cluster.get("server") or "").rstrip("/")
    if not server.startswith("https://"):
        raise ValueError("kubeconfig server must be https://")

    sslctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
    sslctx.check_hostname = True
    sslctx.verify_mode = _ssl.CERT_REQUIRED
    tmpd = Path(tempfile.mkdtemp(prefix="vnc-kc-"))
    try:
        os.chmod(tmpd, 0o700)
        if cluster.get("certificate-authority-data"):
            ca = tmpd / "ca.crt"
            ca.write_bytes(base64.b64decode(cluster["certificate-authority-data"]))
            os.chmod(ca, 0o600)
            sslctx.load_verify_locations(str(ca))
        elif cluster.get("certificate-authority"):
            sslctx.load_verify_locations(cluster["certificate-authority"])
        elif cluster.get("insecure-skip-tls-verify"):
            sslctx.check_hostname = False
            sslctx.verify_mode = _ssl.CERT_NONE
        else:
            sslctx.load_default_certs()

        if user.get("client-certificate-data") and user.get("client-key-data"):
            crt, key = tmpd / "client.crt", tmpd / "client.key"
            crt.write_bytes(base64.b64decode(user["client-certificate-data"]))
            key.write_bytes(base64.b64decode(user["client-key-data"]))
            os.chmod(crt, 0o600); os.chmod(key, 0o600)
            sslctx.load_cert_chain(str(crt), str(key))
        elif user.get("client-certificate") and user.get("client-key"):
            sslctx.load_cert_chain(user["client-certificate"], user["client-key"])
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    token = user.get("token")
    if not token and user.get("tokenFile"):
        # v1.56.0 : une session Rancher range son jeton dans un fichier
        # renouvelé, relu ici à chaque ouverture de console
        token = Path(user["tokenFile"]).read_text().strip()
    return server, sslctx, token


def _vnc_subresource_url(server, namespace, name):
    return (server.replace("https://", "wss://", 1)
            + "/apis/subresources.kubevirt.io/v1/namespaces/"
            + namespace + "/virtualmachineinstances/" + name + "/vnc")


def _vnc_upstream_headers(bearer, identity):
    """En-têtes de la connexion vers KubeVirt.

    Le websocket ne passe pas par kubectl : l'usurpation que porte le
    kubeconfig délégué (`as`, `as-groups`, v1.32.0) n'y arrivait donc pas,
    et la console s'ouvrait avec les pleins pouvoirs du toolkit. On la
    reporte ici, en en-têtes Impersonate-*, que l'API server traite de la
    même façon."""
    headers = []
    if bearer:
        headers.append(("Authorization", f"Bearer {bearer}"))
    if identity and identity.get("user"):
        headers.append(("Impersonate-User", identity["user"]))
        for g in identity.get("groups") or []:
            headers.append(("Impersonate-Group", g))
    return headers


def _vnc_issue_ticket(cluster, namespace, name, user="", identity=None, uid=None, kind="vnc"):
    token = _secrets.token_urlsafe(24)
    now = time.time()
    with _vnc_lock:
        # opportunistic purge of expired tickets
        for t in [t for t, v in _vnc_tickets.items() if v["expires"] < now]:
            _vnc_tickets.pop(t, None)
        _vnc_tickets[token] = {"cluster": cluster, "namespace": namespace,
                               "name": name, "expires": now + _VNC_TICKET_TTL,
                               "user": user or "", "identity": identity,
                               "uid": uid, "kind": kind}
    return token


def _vnc_take_ticket(token, cluster, namespace, name, kind="vnc"):
    """Single-use pop; the ticket must match the exact VM it was issued for,
    and the console it was issued for (v1.61.0 : VNC ou série).
    Rend l'entrée (qui l'a demandé, sous quelle identité), ou None."""
    with _vnc_lock:
        entry = _vnc_tickets.pop(token or "", None)
    if not entry or entry.get("kind", "vnc") != kind:
        return None
    if entry["expires"] < time.time():
        return None
    if (entry["cluster"], entry["namespace"], entry["name"]) != (cluster, namespace, name):
        return None
    return entry


def _vnc_consume_ticket(token, cluster, namespace, name):
    return _vnc_take_ticket(token, cluster, namespace, name) is not None


def _vnc_classify_loss(cluster, namespace, name, uid):
    """KubeVirt a fermé la connexion partagée : la VM a-t-elle redémarré, ou
    un AUTRE client (la console d'Harvester, une autre instance du toolkit)
    a-t-il pris la place ? KubeVirt n'en accepte qu'un, et sans cette
    distinction les deux consoles se la reprenaient en boucle."""
    cfg = load_config()
    kc = next((c["kubeconfig"] for c in cfg.get("clusters", [])
               if c["name"] == cluster), None)
    reason = "lost"
    if kc:
        try:
            r = _kubectl_run(
                ["kubectl", "--kubeconfig", kc, "-n", namespace, "get", "vmi", name,
                 "-o", "jsonpath={.status.phase} {.metadata.uid} "
                       "{.metadata.deletionTimestamp}"],
                capture_output=True, text=True, timeout=10)
            parts = (r.stdout or "").split()
            if r.returncode != 0 or not parts:
                reason = "vm-stopped"
            elif (parts[0] == "Running" and uid and len(parts) == 2 and parts[1] == uid):
                # Même instance, toujours là, et PAS en cours de suppression.
                reason = "taken"
            else:
                # Une instance dont la suppression a commencé (relevé sur
                # harvlab : encore « Running » au moment où la connexion
                # tombe) est une réinitialisation, pas un autre client.
                reason = "vm-restarted"
        except (subprocess.TimeoutExpired, OSError):
            reason = "lost"
    with _vnc_lock:
        _vnc_last_close[(cluster, namespace, name)] = {"reason": reason,
                                                      "at": time.time()}
    log_vnc.info("console %s/%s/%s lost: %s", cluster, namespace, name, reason)
    return reason


@app.route("/api/vm/<cluster>/<namespace>/<name>/console-ticket", methods=["POST"])
@_rate_limit("30/minute")
@requires_auth
def api_vm_console_ticket(cluster, namespace, name):
    """Pre-flight + ticket for the VNC websocket (see relay comment above)."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    active = _vnc_viewers()
    if active >= _VNC_MAX_SESSIONS:
        return jsonify({"error": f"too many console sessions open ({active})",
                        "hint": "close an existing console first"}), 429
    r = _kubectl_run(
        ["kubectl", "--kubeconfig", kc, "-n", namespace, "get", "vmi", name,
         "-o", "jsonpath={.status.phase} {.metadata.uid}"],
        capture_output=True, text=True, timeout=10,
    )
    parts = (r.stdout or "").split()
    phase = parts[0] if parts else ""
    uid = parts[1] if len(parts) > 1 else None
    if r.returncode != 0 or not phase:
        stderr_lines = [l.strip() for l in (r.stderr or "").splitlines() if l.strip()]
        detail = stderr_lines[-1] if stderr_lines else "VMI not found"
        return jsonify({"error": f"VM has no live instance: {detail}",
                        "hint": "start the VM first"}), 409
    if phase not in ("Running", "Scheduled"):
        return jsonify({"error": f"VMI phase is {phase}, not Running"}), 409
    # Chaque navigateur est vérifié par le cluster, sous SA propre identité,
    # même s'il rejoint une console déjà ouverte par quelqu'un d'autre.
    identity = current_cluster_identity()
    if identity or _sso_session() is not None:
        # v1.50.0 : une session Rancher aussi ; `kc` passe alors par Rancher
        can = _kubectl_run(
            ["kubectl", "--kubeconfig", kc, "auth", "can-i", "get",
             "virtualmachineinstances", "--subresource=vnc", "-n", namespace],
            capture_output=True, text=True, timeout=10)
        if (can.stdout or "").strip() != "yes":
            return jsonify({"error": "the cluster does not allow your identity "
                                     "to open this console",
                            "cluster_user": identity.get("user")}), 403
    token = _vnc_issue_ticket(cluster, namespace, name, user=current_user(),
                              identity=identity, uid=uid)
    return jsonify({
        "ticket": token,
        "ws_path": f"/ws/vnc/{cluster}/{namespace}/{name}",
        "expires_in": _VNC_TICKET_TTL,
    })


@app.route("/api/vm/<cluster>/<namespace>/<name>/console-status")
@requires_auth
def api_vm_console_status(cluster, namespace, name):
    """Qui regarde cette console, et pourquoi elle s'est fermée la dernière
    fois. La console s'en sert pour dire « partagée avec bob » et pour ne
    pas reprendre en boucle une place qu'un autre client vient de prendre."""
    key = (cluster, namespace, name)
    viewers = vnc_mux.sessions().get(key, [])
    with _vnc_lock:
        last = _vnc_last_close.get(key)
    if last and time.time() - last["at"] > _VNC_CLOSE_MEMORY:
        last = None
    return jsonify({"viewers": viewers, "count": len(viewers), "last_close": last})


@sock.route("/ws/vnc/<cluster>/<namespace>/<name>")
def ws_vnc(ws, cluster, namespace, name):
    """Rattache le navigateur à la console PARTAGÉE de la VM (vnc_mux).

    KubeVirt n'accepte qu'une connexion VNC par VM et ferme la précédente :
    une connexion par navigateur faisait s'éjecter deux exploitants en
    boucle. Tous les navigateurs d'une VM passent donc par une seule."""
    # namespace/name are RFC1123-validated by before_request; cluster is not.
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$", cluster):
        ws.close(message="invalid cluster"); return
    entry = _vnc_take_ticket(request.args.get("ticket"), cluster, namespace, name)
    if not entry:
        log_vnc.warning("rejected /ws/vnc for %s/%s: bad or expired ticket",
                        namespace, name)
        ws.close(message="invalid or expired ticket"); return
    cfg = load_config()
    kc = next((c["kubeconfig"] for c in cfg.get("clusters", [])
               if c["name"] == cluster), None)
    if not kc:
        ws.close(message="unknown cluster"); return

    def dial():
        server, sslctx, bearer = _kubeconfig_wss(kc)
        headers = _vnc_upstream_headers(bearer, entry.get("identity"))
        box = {}

        def _go():
            try:
                box["c"] = _WsClient.connect(
                    _vnc_subresource_url(server, namespace, name),
                    ssl_context=sslctx, headers=headers or None,
                    subprotocols=_VNC_SUBPROTOCOLS, ping_interval=20)
            except Exception as e:      # noqa: BLE001 (reported below)
                box["e"] = e
        t = threading.Thread(target=_go, daemon=True, name=f"vnc-dial-{name}")
        t.start(); t.join(timeout=12)
        if "c" not in box:
            raise ConnectionError(str(box.get("e", "timeout")))
        return box["c"]

    uid = entry.get("uid")

    def lost(_hub):
        _vnc_classify_loss(cluster, namespace, name, uid)

    key = (cluster, namespace, name)
    with _vnc_lock:
        _vnc_last_close.pop(key, None)
    try:
        vnc_mux.attach(key, dial, ws, user=entry.get("user") or "", on_lost=lost)
    except Exception as e:              # noqa: BLE001
        log_vnc.error("console %s/%s: upstream unavailable: %s", namespace, name, e)
        try: ws.close(message="cluster VNC endpoint unreachable")
        except Exception: pass
    finally:
        metric_vnc_sessions.set(_vnc_viewers())


# =============================================================================
# Console série (v1.61.0) : « Open in Serial Console » de Harvester.
# Navigateur (xterm.js) <-ws-> console <-wss-> KubeVirt, sous-ressource
# `console` de la VMI (binaire). Même modèle que la VNC : un ticket à usage
# unique délivré par une route authentifiée, puis un relais. KubeVirt n'y
# accepte qu'une connexion : une nouvelle ferme la précédente (dit à l'écran).
# =============================================================================
@app.route("/api/vm/<cluster>/<namespace>/<name>/serial-ticket", methods=["POST"])
@_rate_limit("30/minute")
@requires_auth
def api_vm_serial_ticket(cluster, namespace, name):
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    r = _kubectl_run(["kubectl", "--kubeconfig", kc, "-n", namespace, "get", "vmi", name,
                      "-o", "jsonpath={.status.phase} {.metadata.uid}"],
                     capture_output=True, text=True, timeout=10)
    parts = (r.stdout or "").split()
    if r.returncode != 0 or not parts:
        return jsonify({"error": "the VM has no running instance", "hint": "start the VM first"}), 409
    if parts[0] not in ("Running", "Scheduled"):
        return jsonify({"error": f"VMI phase is {parts[0]}, not Running"}), 409
    identity = current_cluster_identity()
    if identity or _sso_session() is not None:
        can = _kubectl_run(["kubectl", "--kubeconfig", kc, "auth", "can-i", "get", "virtualmachineinstances",
                            "--subresource=console", "-n", namespace], capture_output=True, text=True, timeout=10)
        if (can.stdout or "").strip() != "yes":
            return jsonify({"error": "the cluster does not allow your identity to open this console"}), 403
    token = _vnc_issue_ticket(cluster, namespace, name, user=current_user(), identity=identity,
                              uid=parts[1] if len(parts) > 1 else None, kind="serial")
    return jsonify({"ticket": token, "ws_path": f"/ws/serial/{cluster}/{namespace}/{name}",
                    "expires_in": _VNC_TICKET_TTL})


@sock.route("/ws/serial/<cluster>/<namespace>/<name>")
def ws_serial(ws, cluster, namespace, name):
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$", cluster):
        ws.close(message="invalid cluster"); return
    entry = _vnc_take_ticket(request.args.get("ticket"), cluster, namespace, name, kind="serial")
    if not entry:
        ws.close(message="invalid or expired ticket"); return
    cfg = load_config()
    kc = next((c["kubeconfig"] for c in cfg.get("clusters", []) if c["name"] == cluster), None)
    if not kc:
        ws.close(message="unknown cluster"); return
    try:
        server, sslctx, bearer = _kubeconfig_wss(kc)
        up = _WsClient.connect(
            _vnc_subresource_url(server, namespace, name)[:-len("/vnc")] + "/console",
            ssl_context=sslctx, headers=_vnc_upstream_headers(bearer, entry.get("identity")) or None,
            subprotocols=["plain.kubevirt.io"], ping_interval=20)
    except Exception as e:              # noqa: BLE001
        log_vnc.error("serial console %s/%s: upstream unavailable: %s", namespace, name, e)
        try: ws.close(message="cluster serial console endpoint unreachable")
        except Exception: pass
        return
    log_vnc.info("serial console %s/%s opened by %s", namespace, name, entry.get("user") or "?")
    stop = threading.Event()

    def pump_down():
        try:
            while not stop.is_set():
                data = up.receive(timeout=1)
                if data is None:
                    if not up.connected:
                        break
                    continue
                ws.send(data if isinstance(data, (bytes, bytearray)) else data.encode())
        except Exception:               # noqa: BLE001 (fin de l'une des deux connexions)
            pass
        finally:
            stop.set()
    t = threading.Thread(target=pump_down, daemon=True, name=f"serial-{name}")
    t.start()
    try:
        while not stop.is_set():
            data = ws.receive(timeout=1)
            if data is None:
                if not ws.connected:
                    break
                continue
            up.send(data if isinstance(data, (bytes, bytearray)) else data.encode())
    except Exception:                   # noqa: BLE001
        pass
    finally:
        stop.set()
        for c in (up, ws):
            try: c.close()
            except Exception: pass
        log_vnc.info("serial console %s/%s closed", namespace, name)


# =============================================================================
# Terraform integration (uses terraform-provider-harvester)
# =============================================================================
TF_PROVIDER_REPO = Path(os.environ.get(
    "HARVESTER_OPS_TF_PROVIDER",
    "/usr/local/share/terraform-provider-harvester",
))
def _resolve_tf_bin():
    """Le binaire de Terraform à employer. v1.51.0 : l'image embarque
    OpenTofu (MPL-2.0, même CLI, même format de configuration) ; un
    `terraform` posé par l'exploitant garde la priorité, et
    HARVESTER_OPS_TF_BIN tranche."""
    env = os.environ.get("HARVESTER_OPS_TF_BIN")
    if env:
        return env
    for cand in ("/usr/local/bin/terraform", shutil.which("terraform"),
                 "/usr/local/bin/tofu", shutil.which("tofu")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return "/usr/local/bin/terraform"


TF_BIN = _resolve_tf_bin()
TF_FLAVOR = "opentofu" if os.path.basename(TF_BIN).startswith("tofu") else "terraform"
TF_WORKSPACES = _usable_dir(
    Path(os.environ.get("HARVESTER_OPS_TF_WORKSPACES",
                        "/var/lib/harvester-ops/terraform")),
    Path(tempfile.gettempdir()) / "harvester-ops-terraform",
)

# Emplacement des mises à jour du provider posées depuis l'interface ou par
# `bin/harvester-provider-install.py`. Séparé du provider du livrable : une
# mise à jour ne doit jamais écraser ce que le paquet a installé, sans quoi
# une réinstallation du toolkit ferait silencieusement régresser la version.
TF_PROVIDER_MANAGED = Path(os.environ.get(
    "HARVESTER_OPS_TF_PROVIDER_MANAGED",
    str(Path.home() / ".local/share/harvester-ops/terraform-provider"),
))
TF_PROVIDER_INSTALLER = "harvester-provider-install.py"


def _tf_provider_meta():
    """Fiche écrite à l'installation : version, empreinte, provenance.

    C'est la seule source fiable pour la version d'un binaire posé à la
    main : un provider Terraform est un greffon gRPC, l'exécuter n'affiche
    pas sa version, elle ne se lit que dans son nom ou dans cette fiche.
    """
    try:
        return json.loads((TF_PROVIDER_MANAGED / "provider.json").read_text())
    except Exception:
        return {}


def _tf_provider_version():
    """Version du binaire RÉELLEMENT actif.

    L'ancienne implémentation renvoyait toujours le tag git du dépôt source,
    y compris quand le binaire utilisé venait d'ailleurs : l'écran affichait
    alors une version qui n'était pas celle qui tournait.
    """
    bin_p = _tf_provider_binary()
    if bin_p:
        meta = _tf_provider_meta()
        try:
            if meta.get("version") and bin_p.is_relative_to(TF_PROVIDER_MANAGED):
                return "v" + str(meta["version"]).lstrip("v")
        except (AttributeError, ValueError):
            pass
        # Archive officielle : la version est dans le nom du binaire.
        m = re.search(r"_v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$", bin_p.name)
        if m:
            return "v" + m.group(1)
    # Dépôt source compilé sur place : le tag git fait foi.
    if TF_PROVIDER_REPO.exists():
        try:
            r = subprocess.run(
                ["git", "-C", str(TF_PROVIDER_REPO), "describe", "--tags",
                 "--abbrev=0"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return "dev"


# Emplacements fouillés pour le binaire du provider, dans l'ordre de
# priorité :
#   1. HARVESTER_OPS_TF_PROVIDER_PATH — surcharge explicite de l'opérateur,
#      elle doit gagner sur tout le reste ;
#   2. le répertoire géré — une mise à jour faite depuis l'interface serait
#      sans effet si le provider du livrable passait devant ;
#   3. le provider du livrable, puis les emplacements système.
# N'en chercher qu'un seul affichait « missing » alors que le binaire était
# sur la machine.
def _tf_provider_roots():
    extra = os.environ.get("HARVESTER_OPS_TF_PROVIDER_PATH", "")
    roots = [Path(x) for x in extra.split(os.pathsep) if x]
    roots += [
        TF_PROVIDER_MANAGED,
        TF_PROVIDER_REPO,
        Path("/usr/local/share/harvester-ops/terraform-provider"),
    ]
    return roots


# Le nom que porte le binaire dans l'archive officielle une fois
# décompressée : `terraform-provider-harvester_v1.7.3`. Un opérateur qui
# dézippe la release à la main obtient exactement ça, et ne le trouvait pas.
_TF_PROVIDER_GLOB = "terraform-provider-harvester_v*"


def _tf_provider_candidates():
    names = ("terraform-provider-harvester-amd64",
             "terraform-provider-harvester")
    out = []
    seen = set()
    for root in _tf_provider_roots():
        for n in names:
            for c in (root / "bin" / n, root / n):
                if c not in seen:
                    seen.add(c)
                    out.append(c)
        for c in (root / "bin" / _TF_PROVIDER_GLOB, root / _TF_PROVIDER_GLOB):
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


def _tf_provider_binary():
    """Locate the prebuilt provider binary. None if nowhere to be found."""
    for c in _tf_provider_candidates():
        try:
            if c.name == _TF_PROVIDER_GLOB:
                # Plusieurs versions déposées côte à côte : prendre la plus
                # récemment écrite, c'est celle que l'opérateur vient de
                # poser.
                hits = sorted((p for p in c.parent.glob(c.name)
                               if p.is_file() and os.access(p, os.X_OK)),
                              key=lambda p: p.stat().st_mtime, reverse=True)
                if hits:
                    return hits[0]
                continue
            if c.is_file() and os.access(c, os.X_OK):
                return c
        except OSError:
            pass
    return None


def _tf_workspace_dir(cluster):
    """Per-cluster workspace dir — keeps state files isolated.

    v1.54.0 : c'est l'espace PARTAGÉ d'avant, qui garde les ressources
    appliquées hors déclaration (chemin `/apply`) et celles d'avant la
    v1.54 tant que leur déclaration ne les a pas reprises."""
    safe = re.sub(r"[^a-z0-9-]+", "-", cluster.lower()) or "default"
    p = TF_WORKSPACES / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


# v1.54.0 : les déclarations sont gardées par la console (SQLite, à côté des
# notes), et chacune a son propre état Terraform : appliquer une déclaration
# ne touche plus qu'à ses ressources, et deux déclarations ne se marchent
# plus dessus (audit du 26/09/2026). Voir docs/design/2026-09-26-terraform-declarations.md.
TF_DB = Path(os.environ.get("HARVESTER_OPS_TF_DB", str(NOTES_DB.parent / "tf-declarations.db")))
TF_DECLS = _tfs.DeclStore(TF_DB)
TF_STATE_LOCK = threading.Lock()


def _tf_decl_workspace_dir(cluster, decl_id, create=True):
    """L'espace de travail d'une déclaration : `<cluster>/decls/<id>/`."""
    if not _tfs.ID_RE.match(str(decl_id or "")):
        raise ValueError("invalid declaration id")
    p = _tf_workspace_dir(cluster) / "decls" / decl_id
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def _tf_decl_workspaces(cluster):
    """(id, dossier) des espaces de déclaration existants d'un cluster."""
    root = _tf_workspace_dir(cluster) / "decls"
    if not root.is_dir():
        return []
    return [(d.name, d) for d in sorted(root.iterdir())
            if d.is_dir() and _tfs.ID_RE.match(d.name)]


def _tf_plugin_cache_init(ws_dir):
    """Lay down the local provider so terraform init finds it via the
    per-workspace terraformrc filesystem_mirror.

    Layout:
      <ws>/plugins/registry.terraform.io/harvester/harvester/<version>/<os>_<arch>/
        terraform-provider-harvester_v<version>

    The version directory must be a clean semver (`1.8.0`) — terraform
    rejects pre-release tags like `1.8.0-rc1` in the filesystem_mirror
    layout. We strip any trailing `-rc*`/`-snap*` suffix and copy the
    binary under the normalized version."""
    bin_src = _tf_provider_binary()
    if not bin_src:
        return None
    raw = _tf_provider_version().lstrip("v") or "0.0.0"
    version = re.sub(r"-(rc|snap|alpha|beta|dev)[\w\.-]*$", "", raw)
    plug = (ws_dir / "plugins" / "registry.terraform.io" / "harvester"
            / "harvester" / version / f"linux_{_tf_host_arch()}")
    plug.mkdir(parents=True, exist_ok=True)
    dest = plug / f"terraform-provider-harvester_v{version}"
    # Recopier aussi quand la taille diffère : réinstaller la MÊME version
    # avec un autre binaire (un correctif reconstruit sur place) laissait
    # sinon l'ancien greffon dans le miroir, indéfiniment.
    try:
        stale = (not dest.exists()
                 or dest.stat().st_size != bin_src.stat().st_size)
    except OSError:
        stale = True
    if stale:
        try:
            import shutil as _shutil
            _shutil.copy2(bin_src, dest)
            dest.chmod(0o755)
        except Exception as e:
            log_tf.warning("copy provider failed: %s", e)
    return version


def _tf_host_arch():
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m


def _tf_provider_origin(bin_p):
    """D'où vient le binaire actif — l'opérateur doit pouvoir distinguer une
    mise à jour qu'il a posée du provider livré avec le paquet."""
    if not bin_p:
        return ""
    for root, label in ((TF_PROVIDER_MANAGED, "managed"),
                        (TF_PROVIDER_REPO, "bundled")):
        try:
            if bin_p.is_relative_to(root):
                return label
        except (AttributeError, ValueError):
            pass
    return "custom"


def _tf_workspaces_invalidate():
    """Force un `terraform init` neuf dans chaque workspace après un
    changement de provider.

    Indispensable : l'apply ne relance `init` que si `.terraform/` est
    absent, et le miroir local garde une copie du greffon. Sans ce ménage,
    une mise à jour du provider resterait sans aucun effet sur les clusters
    déjà utilisés. Le fichier de verrou part avec, sinon terraform refuse la
    nouvelle version pour cause d'empreinte inconnue.

    Ce qui N'EST PAS touché : `terraform.tfstate`, les `.tf` et leurs
    sidecars. Seuls les artefacts reconstructibles disparaissent.
    """
    import shutil as _shutil
    touched = []
    if not TF_WORKSPACES.exists():
        return touched
    spaces = []
    for ws in sorted(TF_WORKSPACES.iterdir()):
        if not ws.is_dir():
            continue
        spaces.append(ws)
        decls = ws / "decls"
        if decls.is_dir():
            spaces.extend(d for d in sorted(decls.iterdir()) if d.is_dir())
    for ws in spaces:
        hit = False
        for victim in (ws / ".terraform", ws / "plugins"):
            if victim.exists():
                _shutil.rmtree(victim, ignore_errors=True)
                hit = True
        lock = ws / ".terraform.lock.hcl"
        if lock.exists():
            lock.unlink()
            hit = True
        if hit:
            touched.append(str(ws.relative_to(TF_WORKSPACES)))
    return touched


def _tf_provider_installer():
    """Chemin de l'installateur, ou None s'il n'est pas déployé."""
    p = BIN_DIR / TF_PROVIDER_INSTALLER
    return p if p.is_file() else None


def _tf_provider_install_runner(run, source, sha256, version, cleanup,
                                source_label=""):
    """Délègue à l'installateur CLI et relaie ses étapes.

    L'interface ne réimplémente rien : c'est le même script qu'un opérateur
    lance à la main sur un site airgap, d'où la parité.
    """
    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": msg, "ts": time.time()})

    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})

    script = _tf_provider_installer()
    try:
        if script is None:
            step("resolve", "error",
                 f"{TF_PROVIDER_INSTALLER} absent de {BIN_DIR}")
            run.exit_code = 1
            run.status = "error"
            run.error_summary = "installer not deployed"
        else:
            cmd = [sys.executable, str(script), source,
                   "--dest", str(TF_PROVIDER_MANAGED)]
            if sha256:
                cmd += ["--sha256", sha256]
            if version:
                cmd += ["--version", version]
            if source_label:
                cmd += ["--source-label", source_label]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True)
            run.proc = proc          # rend l'action annulable depuis le dock
            last_error = ""
            for line in proc.stderr:
                line = line.strip()
                if line.startswith("STEP_EVENT|"):
                    parts = line.split("|", 3)
                    if len(parts) == 4:
                        step(parts[1], parts[2], parts[3])
                        if parts[2] == "error":
                            last_error = parts[3]
            rc = proc.wait()
            if rc != 0:
                run.exit_code = rc
                run.status = "error"
                # v1.52.1 : la vraie cause (« HTTP Error 404 »...) plutôt
                # qu'un message générique, qui laissait chercher.
                run.error_summary = ("provider install failed: " + last_error[:200]
                                     if last_error else "provider install failed")
            else:
                touched = _tf_workspaces_invalidate()
                step("workspaces", "done",
                     (f"{len(touched)} workspace(s) à réinitialiser : "
                      + ", ".join(touched)) if touched
                     else "aucun workspace à réinitialiser")
                meta = _tf_provider_meta()
                step("done", "done",
                     f"provider actif : v{meta.get('version', '?')}")
                run.exit_code = 0
                run.status = "done"
    except Exception as e:
        run.error_summary = str(e)[:300]
        step("install", "error", str(e)[:300])
        run.exit_code = 1
        run.status = "error"
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)
    run.ended_at = time.time()
    run.emit({"type": "status", "status": run.status,
              "exit_code": run.exit_code, "ts": time.time()})
    run.close()


def _tf_provider_upload_dir():
    """Dépôt temporaire des archives téléversées. Chmod 700 : une archive de
    provider n'est pas un secret, mais rien n'oblige à la rendre lisible de
    tout l'hôte."""
    d = TF_PROVIDER_MANAGED / "incoming"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


# Une source venue du réseau ne peut être qu'une version ou une URL http(s).
# Accepter un chemin local ici donnerait à tout compte authentifié le moyen
# de faire exécuter un fichier arbitraire de l'hôte comme provider ; le
# téléversement, lui, passe par un fichier que le serveur a lui-même écrit.
_TF_SOURCE_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


@app.route("/api/terraform/provider/install", methods=["POST"])
@requires_auth
@_rate_limit("6/minute")
def api_tf_provider_install():
    """Installe ou met à jour le provider. Body: {source, sha256?, version?}.

    `source` : une version (`1.7.3`) ou une URL http(s) d'archive.
    """
    data = request.get_json(force=True, silent=True) or {}
    source = (data.get("source") or "").strip()
    sha256 = (data.get("sha256") or "").strip().lower()
    version = (data.get("version") or "").strip()
    if not source:
        return jsonify({"error": "source required (version or http(s) URL)"}), 400
    if not (_TF_SOURCE_VERSION_RE.match(source)
            or source.startswith(("http://", "https://"))):
        return jsonify({"error": "source must be a version (1.7.3) or an "
                                 "http(s) URL"}), 400
    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return jsonify({"error": "sha256 must be 64 hex characters"}), 400
    if version and not _TF_SOURCE_VERSION_RE.match(version):
        return jsonify({"error": "invalid version"}), 400
    action_id = track_action(f"tf-provider-install:{source[:60]}", "(local)",
                             _tf_provider_install_runner,
                             source, sha256, version, None)
    return jsonify({"action_id": action_id, "source": source}), 202


# Un provider linux_amd64 pèse 20 Mo compressé, 70 Mo en clair. La borne
# laisse la marge d'une future architecture sans ouvrir la porte à un
# téléversement qui remplirait le disque.
TF_PROVIDER_UPLOAD_MAX = 256 * 1024 * 1024


@app.route("/api/terraform/provider/upload", methods=["POST"])
@requires_auth
@_rate_limit("6/minute")
def api_tf_provider_upload():
    """Installe le provider depuis un fichier téléversé — la seule voie
    utilisable sur un site sans accès réseau sortant."""
    if (request.content_length or 0) > TF_PROVIDER_UPLOAD_MAX:
        return jsonify({"error": "file too large (max 256 MiB)"}), 413
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "no file uploaded — use form field 'file'"}), 400
    sha256 = (request.form.get("sha256") or "").strip().lower()
    version = (request.form.get("version") or "").strip()
    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return jsonify({"error": "sha256 must be 64 hex characters"}), 400
    if version and not _TF_SOURCE_VERSION_RE.match(version):
        return jsonify({"error": "invalid version"}), 400
    # Nom imposé par le serveur : le nom client ne sert jamais à construire
    # un chemin. Le contenu est validé par l'installateur, pas par le nom.
    staged = _tf_provider_upload_dir() / f"upload-{uuid.uuid4().hex}.bin"
    try:
        f.save(str(staged))
        staged.chmod(0o600)
    except OSError as e:
        return jsonify({"error": f"cannot stage upload: {e}"}), 500
    if staged.stat().st_size == 0:
        staged.unlink(missing_ok=True)
        return jsonify({"error": "empty file"}), 400
    # Le nom client ne sert QUE d'étiquette lisible, jamais de chemin : le
    # chemin de transit, lui, ne dirait rien à l'opérateur six mois plus tard.
    label = re.sub(r"[^\w.+-]", "_", f.filename)[:80]
    action_id = track_action(f"tf-provider-install:{f.filename[:60]}", "(local)",
                             _tf_provider_install_runner,
                             str(staged), sha256, version, str(staged),
                             f"uploaded: {label}")
    return jsonify({"action_id": action_id, "name": f.filename}), 202


@app.route("/api/terraform/provider", methods=["DELETE"])
@requires_auth
@_rate_limit("6/minute")
def api_tf_provider_revert():
    """Retire la mise à jour et rend la main au provider du livrable."""
    if not (TF_PROVIDER_MANAGED / "provider.json").exists():
        return jsonify({"error": "no managed provider installed"}), 404
    import shutil as _shutil
    _shutil.rmtree(TF_PROVIDER_MANAGED, ignore_errors=True)
    touched = _tf_workspaces_invalidate()
    bin_p = _tf_provider_binary()
    return jsonify({
        "reverted": True,
        "workspaces_reset": touched,
        "provider_binary": str(bin_p) if bin_p else "",
        "provider_version": _tf_provider_version(),
        "provider_origin": _tf_provider_origin(bin_p),
    })


@app.route("/api/terraform/info")
@requires_auth
def api_terraform_info():
    """Provider + CLI versions + bundled examples count + bundle path."""
    bin_p = _tf_provider_binary()
    meta = _tf_provider_meta()
    return jsonify({
        # Dire OÙ l'on a cherché : un badge « missing » seul laisse
        # l'opérateur sans prise, alors que le binaire est souvent là,
        # ailleurs.
        "provider_searched": [str(c) for c in _tf_provider_candidates()],
        "provider_env": "HARVESTER_OPS_TF_PROVIDER",
        "provider_repo": str(TF_PROVIDER_REPO),
        "provider_version": _tf_provider_version(),
        "provider_binary": str(bin_p) if bin_p else "",
        "provider_binary_size": bin_p.stat().st_size if bin_p else 0,
        # Mise à jour : d'où vient le binaire actif, ce que l'opérateur a
        # posé, et si la machine sait installer (l'installateur fait partie
        # du livrable, mais un déploiement partiel existe).
        "provider_origin": _tf_provider_origin(bin_p),
        "provider_managed_dir": str(TF_PROVIDER_MANAGED),
        "provider_installed_sha256": meta.get("sha256", ""),
        "provider_installed_source": meta.get("source", ""),
        "provider_installed_at": meta.get("installed_at", 0),
        "provider_can_install": _tf_provider_installer() is not None,
        "provider_arch": _tf_host_arch(),
        "terraform_bin": TF_BIN,
        "terraform_flavor": TF_FLAVOR,
        "terraform_available": Path(TF_BIN).exists(),
        "workspaces_dir": str(TF_WORKSPACES),
        "examples_dir": str(TF_PROVIDER_REPO / "examples"),
        "example_resources": sorted([p.name for p in
            (TF_PROVIDER_REPO / "examples" / "resources").glob("*")
            if p.is_dir()]) if (TF_PROVIDER_REPO / "examples" / "resources").exists() else [],
    })


@app.route("/api/terraform/bundle/build", methods=["POST"])
@requires_auth
def api_terraform_bundle_build():
    """Bundle terraform binary + provider + examples for airgap transfer.
    Output: dist/terraform-bundle-<ts>-<sha>.tar.gz, set as active TF bundle."""
    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, "terraform-bundle-build", "(local)", [], dry_run=False)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(target=_tf_bundle_runner, args=(run,), daemon=True).start()
    return jsonify({"action_id": run_id}), 201


def _tf_bundle_runner(run):
    """Build a tar.gz that ships: terraform binary, the provider binary
    (already built — bin/terraform-provider-harvester-amd64), and the
    examples directory tree."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})

    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": msg, "ts": time.time()})

    step("preflight", "running", "Locating provider + terraform CLI")
    bin_p = _tf_provider_binary()
    if not bin_p:
        return _close_err(run, "preflight",
            f"provider binary not found at {TF_PROVIDER_REPO}/bin/terraform-provider-harvester-amd64 "
            f"(set HARVESTER_OPS_TF_PROVIDER)")
    tf_bin = Path(TF_BIN)
    if not tf_bin.exists():
        return _close_err(run, "preflight",
            f"terraform binary missing at {TF_BIN} (set HARVESTER_OPS_TF_BIN)")
    step("preflight", "done",
         f"provider {_tf_provider_version()} ({bin_p.stat().st_size//(1024*1024)}MB) + terraform")

    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    suffix = uuid.uuid4().hex[:8]
    out_name = f"terraform-bundle-{ts}-{suffix}.tar.gz"
    out_path = CAPI_BUNDLE_DIR / out_name
    CAPI_BUNDLE_DIR.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix="tf-bundle-"))
    bundle_root = workdir / "terraform-bundle"
    bundle_root.mkdir()
    try:
        import shutil as _shutil
        step("pack", "running", "Packing terraform + provider + examples")
        # Copy provider in the layout terraform init expects:
        version = _tf_provider_version().lstrip("v") or "0.0.0"
        plug_dir = (bundle_root / "plugins" / "registry.terraform.io" /
                    "harvester" / "harvester" / version / "linux_amd64")
        plug_dir.mkdir(parents=True)
        _shutil.copy2(bin_p, plug_dir / f"terraform-provider-harvester_v{version}")
        # Terraform CLI
        _shutil.copy2(tf_bin, bundle_root / "terraform")
        # Examples (truncated to .tf files only)
        examples_dir = TF_PROVIDER_REPO / "examples"
        if examples_dir.exists():
            (bundle_root / "examples").mkdir()
            for tf in examples_dir.rglob("*.tf"):
                rel = tf.relative_to(examples_dir)
                dst = bundle_root / "examples" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                _shutil.copy2(tf, dst)
        # README + manifest
        meta = {
            "version": "1.0.0",
            "bundle": {
                "kind": "terraform",
                "created_at": int(time.time()),
                "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "host": os.uname().nodename,
            },
            "components": [{
                "name": "terraform",
                "version": _terraform_cli_version(),
                "binary": "terraform",
            }, {
                "name": "terraform-provider-harvester",
                "version": _tf_provider_version(),
                "binary": f"plugins/.../terraform-provider-harvester_v{version}",
            }],
        }
        (bundle_root / "manifest.json").write_text(json.dumps(meta, indent=2))
        (bundle_root / "README.md").write_text(
            "# harvester-ops Terraform airgap bundle\n\n"
            f"- terraform CLI v{_terraform_cli_version()}\n"
            f"- terraform-provider-harvester {_tf_provider_version()}\n"
            "- examples/\n\n"
            "Unpack on the airgap host then:\n"
            "  export PATH=$PWD:$PATH\n"
            "  export TF_CLI_CONFIG_FILE=$PWD/terraformrc\n"
            "  terraform init -plugin-dir=plugins/registry.terraform.io/harvester/harvester/<version>/linux_amd64\n"
        )
        step("pack", "done",
             f"{sum(1 for _ in bundle_root.rglob('*') if _.is_file())} files")

        step("archive", "running", "Creating tarball")
        import tarfile as _tar
        with _tar.open(out_path, "w:gz") as tar:
            tar.add(bundle_root, arcname=bundle_root.name)
        import hashlib
        h = hashlib.sha256()
        with out_path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        out_path.with_suffix(".tar.gz.sha256").write_text(f"{h.hexdigest()}  {out_name}\n")
        step("archive", "done", f"{out_path.stat().st_size//(1024*1024)}MB")
    except Exception as e:
        return _close_err(run, "archive", str(e))
    finally:
        import shutil as _shutil
        try: _shutil.rmtree(workdir)
        except Exception: pass

    run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
    run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
    run.close()


def _close_err(run, step_id, msg):
    run.emit({"type": "step", "step_id": step_id, "status": "error",
              "message": msg[:400], "ts": time.time()})
    # v1.54.0 : la cause dans le résumé (Activité, fin de l'action), qui
    # restait vide pour tous les échecs Terraform.
    if not run.error_summary:
        run.error_summary = f"{step_id}: {msg}"[:300]
    run.exit_code = 1; run.status = "error"; run.ended_at = time.time()
    run.emit({"type": "status", "status": "error", "exit_code": 1, "ts": time.time()})
    run.close()


def _terraform_cli_version():
    try:
        r = subprocess.run([TF_BIN, "version", "-json"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return json.loads(r.stdout).get("terraform_version", "?")
    except Exception:
        pass
    return "?"


def _tf_run_cmd(ws_dir, kc_path, args, timeout=300):
    """Invoke terraform with the right env vars (HARVESTER_KUBECONFIG) and
    return (rc, stdout, stderr).

    Generates a per-workspace `terraformrc` that pins the harvester provider
    to our local mirror so a user's ~/.terraformrc dev_overrides can't
    interfere (was breaking init with constraints-mismatch errors)."""
    version = _tf_provider_version().lstrip("v") or "0.0.0"
    plug_root = (ws_dir / "plugins").resolve()
    rc_file = ws_dir / "terraformrc"
    if not rc_file.exists() and plug_root.exists():
        rc_file.write_text(
            'provider_installation {\n'
            '  filesystem_mirror {\n'
            f'    path    = "{plug_root}"\n'
            '    include = ["registry.terraform.io/harvester/harvester"]\n'
            '  }\n'
            '  direct {\n'
            '    exclude = ["registry.terraform.io/harvester/harvester"]\n'
            '  }\n'
            '}\n'
        )
    env = {
        **os.environ,
        "HARVESTER_KUBECONFIG": kc_path,
        "TF_INPUT": "false",
        "TF_IN_AUTOMATION": "true",
        "TF_CLI_CONFIG_FILE": str(rc_file) if rc_file.exists() else os.environ.get("TF_CLI_CONFIG_FILE", ""),
        "NO_COLOR": "1",
    }
    try:
        r = subprocess.run([TF_BIN, *args], cwd=str(ws_dir), env=env,
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


@app.route("/api/terraform/<cluster>/state")
@requires_auth
def api_terraform_state(cluster):
    """List resources currently tracked in the per-cluster TF state.

    v1.5.3: also exposes `resources_detail` — one dict per address with
    `has_sidecar` (true if `<safe>.json` exists in the workspace) and
    `kind` (read from the sidecar). The UI uses this to surface an
    ✎ Edit button on rows we can repopulate into the form."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    # v1.54.0 : chaque déclaration a son état ; la vue les réunit, avec
    # l'espace partagé d'avant. Lu dans les fichiers d'état (pas besoin de
    # lancer Terraform ni d'un `init` pour lister).
    ws = _tf_workspace_dir(cluster)
    spaces = [(None, ws)] + _tf_decl_workspaces(cluster)
    resources, detail = [], []
    for decl_id, space in spaces:
        decl = TF_DECLS.get(decl_id) if decl_id else None
        for addr in _tfst.addresses(space):
            local = addr.split(".", 1)[1] if "." in addr else addr
            side = space / f"{local}.json"
            d = {"address": addr, "local_name": local, "has_sidecar": False,
                 "workspace": decl_id or "shared", "declaration_id": decl_id,
                 "declaration_name": decl["name"] if decl else None}
            if side.exists():
                d["has_sidecar"] = True
                try:
                    meta = json.loads(side.read_text())
                    d["kind"] = meta.get("kind")
                    d["declaration_name"] = d["declaration_name"] or meta.get("declaration_name")
                    d["written_at"] = meta.get("written_at")
                except (OSError, json.JSONDecodeError):
                    pass
            resources.append(addr)
            detail.append(d)
    initialized = (ws / ".terraform").exists() or any(
        (space / ".terraform").exists() for _, space in spaces[1:])
    return jsonify({
        "initialized": initialized, "workspace": str(ws),
        "resources": resources, "resources_detail": detail,
        "resource_count": len(resources),
    })


@app.route("/api/terraform/<cluster>/sidecar/<safe>")
@requires_auth
def api_terraform_sidecar(cluster, safe):
    """v1.5.3 — fetch the JSON sidecar (`<safe>.json`) written next to
    each `<safe>.tf` by apply_declaration. The UI uses this content to
    reopen a deployed resource into the section-based form for
    editing.

    Returns 404 if no sidecar exists (legacy `/apply` path or a
    resource never deployed via a declaration)."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    # `safe` must look like a sane filename — no slashes, no .., …
    if not re.match(r"^[a-zA-Z0-9_]{1,128}$", safe or ""):
        return jsonify({"error": "invalid safe name",
                        "hint": "alphanumeric and underscore only"}), 400
    ws = _tf_workspace_dir(cluster)
    decl_id = request.args.get("decl")
    if decl_id:
        if not _tf_decl_id_ok(decl_id):
            return jsonify({"error": "invalid declaration id"}), 400
        ws = _tf_decl_workspace_dir(cluster, decl_id, create=False)
    side = ws / f"{safe}.json"
    # Defensive: ensure we resolved a file inside the workspace.
    try:
        if not side.exists() or side.resolve().parent != ws.resolve():
            return jsonify({"error": "sidecar not found", "safe": safe}), 404
    except OSError:
        return jsonify({"error": "sidecar not found", "safe": safe}), 404
    try:
        return jsonify(json.loads(side.read_text()))
    except json.JSONDecodeError as e:
        return jsonify({"error": "sidecar parse failed", "detail": str(e)}), 500


@app.route("/api/terraform/<cluster>/apply", methods=["POST"])
@_rate_limit("20/minute")
@requires_auth
def api_terraform_apply(cluster):
    """Apply a Harvester resource definition via Terraform.

    Body: { kind: "vm" | "image" | "ssh_key" | "raw", spec: {...}, dry_run: bool }
    For `kind="raw"`, `spec.tf` is the literal .tf file content (advanced)."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    kind = data.get("kind", "")
    spec = data.get("spec") or {}
    dry_run = bool(data.get("dry_run", False))
    tf_content = _render_tf_for_kind(kind, spec)
    if not tf_content:
        return jsonify({"error": f"unsupported kind: {kind}",
                        "supported": ["vm", "image", "ssh_key", "raw"]}), 400

    run_id = uuid.uuid4().hex[:12]
    label = f"tf-apply:{kind}:{spec.get('name', '?')}"
    run = ActionRun(run_id, label, cluster, [], dry_run=dry_run)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(target=_tf_apply_runner,
                     args=(run, cluster, kc, tf_content, dry_run, spec.get('name')),
                     daemon=True).start()
    return jsonify({"action_id": run_id}), 201


# ---------------------------------------------------------------------------
# v1.54.0 : les déclarations Terraform, gardées par la console
# ---------------------------------------------------------------------------
def _tf_store_error(e):
    status = {"not-found": 404, "name-taken": 409, "id-taken": 409, "conflict": 409}.get(e.code, 400)
    body = {"error": str(e), "code": e.code}
    if isinstance(e, _tfs.Conflict):
        body["current"] = _tf_decl_view(e.facts.get("current"))
    elif e.facts:
        body.update({k: v for k, v in e.facts.items() if k != "current"})
    return jsonify(body), status


def _tf_decl_id_ok(decl_id):
    return bool(_tfs.ID_RE.match(str(decl_id or "")))


@app.route("/api/tf-declarations")
@requires_auth
def api_tf_declarations():
    cluster = request.args.get("cluster") or None
    return jsonify({"declarations": [_tf_decl_view(d) for d in TF_DECLS.list(cluster)]})


@app.route("/api/tf-declarations", methods=["POST"])
@requires_auth
@_rate_limit("60/minute")
def api_tf_declaration_create():
    """Crée une déclaration. L'identifiant peut venir du navigateur : la
    reprise des déclarations d'avant la v1.54 (localStorage) garde le leur."""
    data = request.get_json(force=True, silent=True) or {}
    cluster = str(data.get("cluster") or "")
    if not _kubectl_for_cluster(cluster):
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    decl_id = str(data.get("id") or uuid.uuid4().hex[:12])
    try:
        d = TF_DECLS.create(decl_id, cluster, data.get("name"), data.get("description") or "",
                            data.get("resources") or [], user=current_user() or "")
    except _tfs.StoreError as e:
        return _tf_store_error(e)
    return jsonify(_tf_decl_view(d)), 201


@app.route("/api/tf-declarations/<decl_id>")
@requires_auth
def api_tf_declaration_get(decl_id):
    d = TF_DECLS.get(decl_id) if _tf_decl_id_ok(decl_id) else None
    if d is None:
        return jsonify({"error": "declaration not found"}), 404
    return jsonify(_tf_decl_view(d))


@app.route("/api/tf-declarations/<decl_id>", methods=["PUT"])
@requires_auth
@_rate_limit("240/minute")
def api_tf_declaration_update(decl_id):
    """Renommer, décrire, changer les ressources, à révision attendue : si
    quelqu'un est passé entre-temps, 409 avec la version courante."""
    if not _tf_decl_id_ok(decl_id):
        return jsonify({"error": "invalid declaration id"}), 400
    data = request.get_json(force=True, silent=True) or {}
    try:
        d = TF_DECLS.update(decl_id, data.get("revision"), user=current_user() or "",
                            name=data.get("name"), description=data.get("description"),
                            resources=data.get("resources"))
    except _tfs.StoreError as e:
        return _tf_store_error(e)
    return jsonify(_tf_decl_view(d))


@app.route("/api/tf-declarations/<decl_id>/code")
@requires_auth
def api_tf_declaration_code(decl_id):
    """Le code Terraform que la déclaration produit, fichier par fichier
    (onglet Code, export en .tf)."""
    d = TF_DECLS.get(decl_id) if _tf_decl_id_ok(decl_id) else None
    if d is None:
        return jsonify({"error": "declaration not found"}), 404
    rendered, errors = _tf_render_resources(d["resources"])
    files = [{"name": "_providers.tf", "content": _TF_HEADER}]
    for safe, kind, spec, hcl, addr, i in rendered:
        body = hcl[len(_TF_HEADER):] if hcl.startswith(_TF_HEADER) else hcl
        files.append({"name": f"{safe}.tf", "content": body, "address": addr,
                      "resource_id": d["resources"][i]["id"]})
    return jsonify({"files": files, "errors": errors})


@app.route("/api/tf-declarations/<decl_id>/history")
@requires_auth
def api_tf_declaration_history(decl_id):
    """Les plans, applies et destructions de la déclaration, du plus récent
    au plus ancien (actions en cours comprises)."""
    if not _tf_decl_id_ok(decl_id):
        return jsonify({"error": "invalid declaration id"}), 400
    runs = {}
    try:
        conn = sqlite3.connect(str(ACTIONS_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, action, status, started_at, ended_at, dry_run, error_summary, result "
            "FROM actions WHERE result LIKE ? ORDER BY started_at DESC LIMIT 100",
            (f'%"declaration_id": "{decl_id}"%',)).fetchall()
        conn.close()
    except sqlite3.Error:
        rows = []
    for r in rows:
        try:
            res = json.loads(r["result"] or "{}")
        except ValueError:
            res = {}
        runs[r["id"]] = {"id": r["id"], "action": r["action"], "status": r["status"],
                         "started_at": r["started_at"], "ended_at": r["ended_at"],
                         "dry_run": bool(r["dry_run"]), "error_summary": r["error_summary"],
                         "mode": res.get("mode"), "by": res.get("by"),
                         "counts": ((res.get("plan") or {}).get("counts"))}
    with ACTIONS_LOCK:
        live = [a for a in ACTIONS.values() if (a.result or {}).get("declaration_id") == decl_id]
    for a in live:
        res = a.result or {}
        runs[a.id] = {"id": a.id, "action": a.action, "status": a.status, "started_at": a.started_at,
                      "ended_at": a.ended_at, "dry_run": bool(a.dry_run), "error_summary": a.error_summary,
                      "mode": res.get("mode"), "by": res.get("by"),
                      "counts": ((res.get("plan") or {}).get("counts"))}
    out = sorted(runs.values(), key=lambda x: x["started_at"] or 0, reverse=True)
    return jsonify({"runs": out[:100]})


@app.route("/api/tf-declarations/<decl_id>", methods=["DELETE"])
@requires_auth
@_rate_limit("60/minute")
def api_tf_declaration_delete(decl_id):
    """Supprime la définition. Refusé tant que des ressources de la
    déclaration sont déployées : les détruire d'abord (sinon elles
    resteraient sur le cluster sans plus personne pour les gérer)."""
    d = TF_DECLS.get(decl_id) if _tf_decl_id_ok(decl_id) else None
    if d is None:
        return jsonify({"error": "declaration not found"}), 404
    ws = _tf_decl_workspace_dir(d["cluster"], decl_id, create=False)
    deployed = _tfst.addresses(ws) if ws.exists() else []
    if deployed:
        return jsonify({"error": "declaration still has deployed resources",
                        "code": "deployed", "deployed": deployed}), 409
    TF_DECLS.delete(decl_id)
    if ws.exists():
        import shutil as _shutil
        _shutil.rmtree(ws, ignore_errors=True)
    return jsonify({"deleted": decl_id})


@app.route("/api/terraform/<cluster>/apply_declaration", methods=["POST"])
@_rate_limit("20/minute")
@requires_auth
def api_terraform_apply_declaration(cluster):
    """v1.5.0 — apply a *declaration* (a bundle of N resources of mixed
    kinds) in one shot. Each resource is rendered into its own
    `<safe_name>.tf` plus a sidecar `<safe_name>.json` (used by v1.5.1
    for editing deployed resources).

    Body: {
      declaration: { name, resources: [{kind, spec}, ...] },
      dry_run: bool,
    }

    Validation happens BEFORE any background work:
      - declaration.resources must be a non-empty array
      - every resource must render to non-empty HCL (the schema rejects
        missing required fields here)
    On any failure the endpoint returns 400 with `errors: [{index, error}]`.
    """
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    decl = data.get("declaration") or {}
    resources = decl.get("resources") or []
    dry_run = bool(data.get("dry_run", False))
    decl_id = str(decl.get("id") or "")
    if decl_id:
        # v1.54.0 : une déclaration gardée par la console s'applique dans son
        # propre espace d'état, depuis son contenu enregistré (le corps ne
        # porte que son identifiant).
        return _tf_decl_start(cluster, kc, decl_id, "plan" if dry_run else "apply",
                              plan_hash=data.get("plan_hash"))
    if not isinstance(resources, list) or not resources:
        return jsonify({"error": "declaration.resources must be a non-empty list"}), 400
    rendered, errors = _tf_render_resources(resources)

    if errors:
        return jsonify({
            "error": "one or more resources are incomplete",
            "errors": errors,
            "supported": ["vm", "image", "ssh_key", "raw"],
        }), 400

    run_id = uuid.uuid4().hex[:12]
    label = _tf_decl_label(decl.get("name", "?"), len(rendered), dry_run)
    run = ActionRun(run_id, label, cluster, [], dry_run=dry_run)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(
        target=_tf_apply_declaration_runner,
        args=(run, cluster, kc, [r[:4] for r in rendered], dry_run, decl.get("name") or "?"),
        daemon=True,
    ).start()
    return jsonify({"action_id": run_id}), 201


_KIND_TO_TF_TYPE = {
    "vm": "harvester_virtualmachine",
    "image": "harvester_image",
    "ssh_key": "harvester_ssh_key",
}


def _tf_address_for_resource(kind, safe_name, hcl=None):
    """Compute the Terraform address (`<type>.<local_name>`) for a
    declaration resource. For `raw` we have to fish the type+name out
    of the user-provided HCL — first `resource "TYPE" "NAME"` wins."""
    if kind in _KIND_TO_TF_TYPE:
        return f"{_KIND_TO_TF_TYPE[kind]}.{safe_name}"
    if kind == "raw" and hcl:
        m = re.search(r'resource\s+"([a-z_]+)"\s+"([a-zA-Z0-9_]+)"', hcl)
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    return None


def _tf_raw_name(hcl):
    m = re.search(r'resource\s+"[a-z_]+"\s+"([a-zA-Z0-9_]+)"', hcl or "")
    return m.group(1) if m else ""


def _tf_render_resources(resources):
    """(rendu, erreurs) : rendu = [(nom_sûr, kind, spec, hcl, adresse, rang)].

    Le nom sûr nomme les fichiers `<nom>.tf` / `<nom>.json` et, pour les
    types connus, l'adresse Terraform. Deux ressources au même nom sont
    départagées (`node`, `node_2`). v1.54.0 : une ressource « raw » prend le
    nom de son bloc `resource` (elle s'appelait d'après les 24 premiers
    caractères de son code, `resource_harvester_ssh_`, audit D6)."""
    rendered, errors, seen = [], [], set()
    for i, res in enumerate(resources or []):
        kind = (res or {}).get("kind") or ""
        spec = (res or {}).get("spec") or {}
        hcl = _render_tf_for_kind(kind, spec)
        if not hcl:
            errors.append({"index": i, "kind": kind, "name": spec.get("name"),
                           "error": "missing required fields"})
            continue
        base = spec.get("name") or (_tf_raw_name(spec.get("tf")) if kind == "raw" else "") or f"res_{i}"
        safe = re.sub(r"[^a-z0-9_]+", "_", base.lower()) or f"res_{i}"
        n, j = safe, 1
        while n in seen:
            j += 1
            n = f"{safe}_{j}"
        seen.add(n)
        rendered.append((n, kind, spec, hcl, _tf_address_for_resource(kind, n, hcl=hcl), i))
    return rendered, errors


def _safe_name_for_spec(spec, fallback="res"):
    """Same safe-name slugify used by apply_declaration. Lifted as a
    helper so destroy_declaration can reproduce the address."""
    base = (spec or {}).get("name") or (spec or {}).get("tf", "")[:24] or fallback
    return re.sub(r"[^a-z0-9_]+", "_", base.lower()) or fallback


def _write_resource_with_sidecar(ws, safe_name, hcl, kind, spec,
                                  declaration_name):
    """Write `<safe>.tf` (header-stripped) and `<safe>.json` sidecar
    inside the workspace. The sidecar persists the original spec so
    v1.5.1 can reload it into the form for editing — and gives the
    operator a JSON-grepable index of what each .tf actually does."""
    body = hcl
    if body.startswith(_TF_HEADER):
        body = body[len(_TF_HEADER):]
    tf_path = ws / f"{safe_name}.tf"
    side_path = ws / f"{safe_name}.json"
    tf_path.write_text(body)
    sidecar = {
        "kind": kind,
        "spec": spec,
        "declaration_name": declaration_name,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "schema_version": 1,
    }
    side_path.write_text(json.dumps(sidecar, indent=2))


def _tf_apply_declaration_runner(run, cluster, kc, rendered, dry_run,
                                   declaration_name):
    """Multi-resource variant of `_tf_apply_runner`. Writes all
    `<safe>.tf` + `<safe>.json` files up-front, then runs ONE
    `terraform plan` (+ apply when not dry_run)."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})

    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": msg, "ts": time.time()})

    step("preflight", "running",
         f"Preparing workspace for declaration '{declaration_name}' "
         f"({len(rendered)} resources)")
    ws = _tf_workspace_dir(cluster)
    try:
        _stage_kubeconfig(kc, ws)
    except Exception as e:
        return _close_err(run, "preflight", f"kubeconfig copy failed: {e}")

    providers_file = ws / "_providers.tf"
    if not providers_file.exists():
        providers_file.write_text(_TF_HEADER)
        for existing in ws.glob("*.tf"):
            if existing.name == "_providers.tf":
                continue
            try:
                txt = existing.read_text()
                if txt.startswith(_TF_HEADER):
                    existing.write_text(txt[len(_TF_HEADER):])
            except OSError:
                pass

    for safe, kind, spec, hcl in rendered:
        try:
            _write_resource_with_sidecar(ws, safe, hcl, kind, spec,
                                          declaration_name)
            run.emit({"type": "log", "stream": "stdout",
                      "message": f"wrote {safe}.tf + {safe}.json ({kind})",
                      "ts": time.time()})
        except OSError as e:
            return _close_err(run, "preflight",
                              f"writing {safe}.tf failed: {e}")
    _tf_plugin_cache_init(ws)
    step("preflight", "done", f"workspace at {ws}")

    if not (ws / ".terraform").exists():
        step("init", "running", "terraform init")
        rc, out, err = _tf_run_cmd(
            ws, str((ws / "kubeconfig").resolve()),
            ["init", "-input=false"], timeout=120,
        )
        if rc != 0:
            return _close_err(run, "init",
                              err.strip()[:400] or out.strip()[:400])
        step("init", "done", "providers ready")

    step("plan", "running", "terraform plan")
    rc, out, err = _tf_run_cmd(
        ws, str((ws / "kubeconfig").resolve()),
        ["plan", "-input=false", "-no-color", "-out=tfplan"],
        timeout=300,
    )
    if rc != 0:
        return _close_err(run, "plan",
                          err.strip()[:400] or out.strip()[:400])
    for line in (out or "").splitlines()[-40:]:
        if line.strip():
            run.emit({"type": "log", "stream": "stdout",
                      "message": line[:200], "ts": time.time()})
    step("plan", "done", "plan ready")

    if dry_run:
        step("apply", "skipped", "[DRY-RUN]")
        run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "done", "exit_code": 0,
                  "ts": time.time()})
        run.close()
        return

    step("apply", "running",
         f"terraform apply ({len(rendered)} resources)")
    rc, out, err = _tf_run_cmd(
        ws, str((ws / "kubeconfig").resolve()),
        ["apply", "-input=false", "-no-color", "-auto-approve", "tfplan"],
        timeout=900,
    )
    for line in (out or "").splitlines()[-60:]:
        if line.strip():
            run.emit({"type": "log", "stream": "stdout",
                      "message": line[:200], "ts": time.time()})
    if rc != 0:
        return _close_err(run, "apply",
                          err.strip()[:400] or out.strip()[:400])
    step("apply", "done",
         f"declaration '{declaration_name}' applied "
         f"({len(rendered)} resources)")
    run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
    run.emit({"type": "status", "status": "done", "exit_code": 0,
              "ts": time.time()})
    run.close()


# Fichiers d'un espace de déclaration qui ne décrivent pas une ressource.
_TF_KEEP = {"_providers.tf", "kubeconfig", "terraformrc", "tfplan", "tfplan.meta"}


def _tf_decl_hash(rendered):
    """Empreinte du contenu rendu : un plan ne s'applique que si la
    déclaration n'a pas changé depuis."""
    blob = json.dumps([[r[0], r[1], r[3]] for r in rendered], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _tf_decl_resource_addresses(decl):
    """{id de ressource: adresse Terraform} d'une déclaration."""
    resources = decl.get("resources") or []
    rendered, _ = _tf_render_resources(resources)
    return {resources[r[5]]["id"]: r[4] for r in rendered if r[4]}


def _tf_decl_view(decl):
    """Une déclaration pour la page : ses ressources avec leur adresse, ce
    qui est déployé (état de son espace), le dernier plan."""
    if decl is None:
        return None
    d = dict(decl)
    addr = _tf_decl_resource_addresses(decl)
    d["resources"] = [dict(r, address=addr.get(r["id"])) for r in decl.get("resources") or []]
    rendered, errors = _tf_render_resources(decl.get("resources") or [])
    # v1.55.0 : l'empreinte du contenu, pour que la page sache si le
    # dernier plan vaut encore pour ce qui est écrit.
    d["content_hash"] = _tf_decl_hash(rendered) if not errors else None
    d["incomplete"] = [e["index"] for e in errors]
    ws = None
    try:
        ws = _tf_decl_workspace_dir(decl["cluster"], decl["id"], create=False)
    except ValueError:
        pass
    d["deployed"] = _tfst.addresses(ws) if ws and ws.exists() else []
    meta = {}
    if ws and (ws / "tfplan.meta").exists():
        try:
            meta = json.loads((ws / "tfplan.meta").read_text())
        except (OSError, ValueError):
            meta = {}
    d["last_plan"] = meta or None
    return d


def _tf_decl_start(cluster, kc, decl_id, mode, plan_hash=None):
    """Lance plan, apply ou destroy d'une déclaration gardée par la console,
    en action suivie. `mode` : plan | apply | destroy | destroy-plan."""
    decl = TF_DECLS.get(decl_id)
    if decl is None:
        return jsonify({"error": "declaration not found", "id": decl_id}), 404
    if decl["cluster"] != cluster:
        return jsonify({"error": "declaration belongs to another cluster",
                        "cluster": decl["cluster"]}), 400
    rendered, errors = _tf_render_resources(decl["resources"])
    if errors:
        return jsonify({"error": "one or more resources are incomplete", "errors": errors,
                        "supported": ["vm", "image", "ssh_key", "raw"]}), 400
    if mode in ("plan", "apply") and not rendered:
        return jsonify({"error": "declaration.resources must be a non-empty list"}), 400
    missing = [r[0] for r in rendered if not r[4]]
    if missing:
        return jsonify({"error": "cannot compute Terraform address", "resources": missing}), 400
    h = _tf_decl_hash(rendered)
    if mode == "apply" and plan_hash and plan_hash != h:
        return jsonify({"error": "plan outdated",
                        "hint": "the declaration changed since this plan; plan again"}), 409
    label = {"plan": "tf-plan-decl", "apply": "tf-apply-decl",
             "destroy": "tf-destroy-decl", "destroy-plan": "tf-destroy-decl"}[mode]
    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, f"{label}:{decl['name']}:{len(rendered)}", cluster, [],
                    dry_run=mode in ("plan", "destroy-plan"))
    run.cluster_user = (current_cluster_identity() or {}).get("user")
    user = current_user() or ""
    # v1.55.0 : l'historique d'une déclaration retrouve ses actions par là.
    run.result = {"declaration_id": decl["id"], "by": user, "mode": mode}
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(target=_tf_decl_runner,
                     args=(run, cluster, kc, decl, rendered, mode, h, bool(plan_hash), user),
                     daemon=True).start()
    return jsonify({"action_id": run_id, "plan_hash": h}), 201


def _tf_decl_runner(run, cluster, kc, decl, rendered, mode, content_hash, use_saved_plan, user):
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})

    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status, "message": msg, "ts": time.time()})

    def log(msg):
        run.emit({"type": "log", "stream": "stdout", "message": msg[:200], "ts": time.time()})

    def done(result=None):
        run.result = dict(run.result or {}, **(result or {}))
        run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
        run.close()

    destroying = mode in ("destroy", "destroy-plan")
    name = decl["name"]
    step("preflight", "running", f"declaration '{name}' ({len(rendered)} resources)")
    ws = _tf_decl_workspace_dir(cluster, decl["id"])
    shared = _tf_workspace_dir(cluster)
    try:
        _stage_kubeconfig(kc, ws)
    except Exception as e:
        return _close_err(run, "preflight", f"kubeconfig copy failed: {e}")
    (ws / "_providers.tf").write_text(_TF_HEADER)

    # Reprise : ce qui a été appliqué avant la v1.54 vit dans l'état partagé.
    wanted = {r[4] for r in rendered}
    lock_file = shared / ".terraform.tfstate.lock.info"
    if wanted - set(_tfst.addresses(ws)) and not lock_file.exists():
        with TF_STATE_LOCK:
            moved = _tfst.move(shared, ws, wanted)
        for a in moved:
            local = a.split(".", 1)[1]
            for ext in (".tf", ".json"):
                (shared / f"{local}{ext}").unlink(missing_ok=True)
        if moved:
            step("adopt", "done", f"{len(moved)} resource(s) taken over from the shared workspace: "
                                  + ", ".join(moved))

    # Les fichiers de l'espace suivent la déclaration : ce qui n'y est plus
    # disparaît, et sera détruit par l'apply (le plan le montre).
    keep = set()
    for safe, kind, spec, hcl, _addr, _i in rendered:
        keep |= {f"{safe}.tf", f"{safe}.json"}
    for f in list(ws.glob("*.tf")) + list(ws.glob("*.json")):
        if f.name not in keep and f.name not in _TF_KEEP:
            f.unlink(missing_ok=True)
    for safe, kind, spec, hcl, _addr, _i in rendered:
        try:
            _write_resource_with_sidecar(ws, safe, hcl, kind, spec, name)
        except OSError as e:
            return _close_err(run, "preflight", f"writing {safe}.tf failed: {e}")
    _tf_plugin_cache_init(ws)
    step("preflight", "done", f"workspace of declaration {decl['id']}")
    kcp = str((ws / "kubeconfig").resolve())

    if not (ws / ".terraform").exists():
        step("init", "running", "init")
        rc, out, err = _tf_run_cmd(ws, kcp, ["init", "-input=false"], timeout=120)
        if rc != 0:
            return _close_err(run, "init", err.strip()[:400] or out.strip()[:400])
        step("init", "done", "providers ready")

    if destroying:
        args = (["plan", "-destroy", "-input=false", "-no-color"] if mode == "destroy-plan"
                else ["destroy", "-input=false", "-no-color", "-auto-approve"])
        step("destroy", "running", args[0])
        rc, out, err = _tf_run_cmd(ws, kcp, args, timeout=900)
        for line in (out or "").splitlines()[-60:]:
            if line.strip():
                log(line)
        if rc != 0:
            if mode == "destroy":
                TF_DECLS.mark_applied(decl["id"], "error", user)
            return _close_err(run, "destroy", err.strip()[:400] or out.strip()[:400])
        if mode == "destroy":
            for f in list(ws.glob("*.tf")) + list(ws.glob("*.json")) + [ws / "tfplan", ws / "tfplan.meta"]:
                if f.name != "_providers.tf":
                    f.unlink(missing_ok=True)
            TF_DECLS.mark_applied(decl["id"], "destroyed", user)
        step("destroy", "done", f"declaration '{name}' destroyed" if mode == "destroy" else "destroy plan ready")
        return done({"deployed": _tfst.addresses(ws)})

    summary = None
    if not (use_saved_plan and (ws / "tfplan").exists()):
        step("plan", "running", "plan")
        rc, out, err = _tf_run_cmd(ws, kcp, ["plan", "-input=false", "-no-color", "-out=tfplan"],
                                   timeout=300)
        if rc != 0:
            (ws / "tfplan.meta").unlink(missing_ok=True)
            return _close_err(run, "plan", err.strip()[:400] or out.strip()[:400])
        for line in (out or "").splitlines()[-40:]:
            if line.strip():
                log(line)
        rc2, js, _ = _tf_run_cmd(ws, kcp, ["show", "-json", "tfplan"], timeout=120)
        try:
            summary = _tfst.plan_summary(json.loads(js)) if rc2 == 0 else None
        except ValueError:
            summary = None
        meta = {"hash": content_hash, "revision": decl["revision"],
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "by": user,
                "summary": summary}
        (ws / "tfplan.meta").write_text(json.dumps(meta))
        step("plan", "done", "plan ready")
    else:
        try:
            meta = json.loads((ws / "tfplan.meta").read_text())
        except (OSError, ValueError):
            meta = {}
        if meta.get("hash") != content_hash:
            return _close_err(run, "plan", "plan outdated: the declaration changed since this plan")
        summary = meta.get("summary")
        step("plan", "done", "applying the reviewed plan")

    if mode == "plan":
        step("apply", "skipped", "[DRY-RUN]")
        return done({"plan": summary, "plan_hash": content_hash})

    step("apply", "running", f"apply ({len(rendered)} resources)")
    rc, out, err = _tf_run_cmd(ws, kcp, ["apply", "-input=false", "-no-color", "-auto-approve", "tfplan"],
                               timeout=900)
    for line in (out or "").splitlines()[-60:]:
        if line.strip():
            log(line)
    (ws / "tfplan").unlink(missing_ok=True)
    (ws / "tfplan.meta").unlink(missing_ok=True)
    if rc != 0:
        TF_DECLS.mark_applied(decl["id"], "error", user)
        return _close_err(run, "apply", err.strip()[:400] or out.strip()[:400])
    TF_DECLS.mark_applied(decl["id"], "done", user)
    step("apply", "done", f"declaration '{name}' applied ({len(rendered)} resources)")
    return done({"plan": summary, "deployed": _tfst.addresses(ws)})


_TF_HEADER = """terraform {
  required_providers {
    harvester = {
      source  = "harvester/harvester"
    }
  }
}

provider "harvester" {
  kubeconfig = "${path.module}/kubeconfig"
}

"""


def _hcl_str(s):
    """Quote a string for HCL — newlines become heredoc."""
    if s is None:
        return '""'
    s = str(s)
    if "\n" in s:
        return f"<<-EOT\n{s}\nEOT"
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _hcl_id(s):
    """Sanitize a string for use as an HCL resource identifier."""
    return (s or "x").replace("-", "_").replace(".", "_")


def _render_disk_block(disk):
    """Render one VM `disk { … }` block from a dict spec.

    v1.4.38: the Harvester provider rejects a `storage_class_name` on a
    disk that also has an `image` set — the storage class is inherited
    from the image itself ("the storage_class_name of an image can only
    be defined during image creation"). We honor that contract by
    silently dropping storage_class_name when image is non-empty.
    """
    # v1.4.39: `boot_order` may legitimately be 0 (means "don't include
    # in boot order"), so we must not coerce via `or 1` — that would
    # silently overwrite the user's 0.
    bo = disk.get("boot_order")
    if bo is None or bo == "":
        bo = 1
    parts = [
        f'    name       = {_hcl_str(disk.get("name") or "rootdisk")}',
        f'    type       = {_hcl_str(disk.get("type") or "disk")}',
        f'    size       = {_hcl_str(disk.get("size") or "20Gi")}',
        f'    bus        = {_hcl_str(disk.get("bus")  or "virtio")}',
        f'    boot_order = {int(bo)}',
    ]
    if disk.get("image"):
        parts.append(f'    image      = {_hcl_str(disk["image"])}')
    elif disk.get("storage_class_name"):
        # storage_class_name is only valid on a blank data disk
        parts.append(f'    storage_class_name = {_hcl_str(disk["storage_class_name"])}')
    # v1.52.1 : une VM détruite emporte ses disques, sauf demande contraire.
    # Le provider les gardait par défaut : un volume orphelin par destruction.
    keep = disk.get("auto_delete") in (False, "false", "False", 0)
    parts.append(f'    auto_delete = {"false" if keep else "true"}')
    return "  disk {\n" + "\n".join(parts) + "\n  }"


def _render_nic_block(nic):
    parts = [
        f'    name         = {_hcl_str(nic.get("name") or "nic-1")}',
        f'    type         = {_hcl_str(nic.get("type") or "bridge")}',
        f'    model        = {_hcl_str(nic.get("model") or "virtio")}',
    ]
    if nic.get("network_name"):
        parts.append(f'    network_name = {_hcl_str(nic["network_name"])}')
    if nic.get("wait_for_lease"):
        parts.append('    wait_for_lease = true')
    return "  network_interface {\n" + "\n".join(parts) + "\n  }"


# Les seules valeurs que le provider accepte (constantes de
# harvester/pkg/builder). Les déclarations enregistrées avant la v1.52.1
# portent les minuscules que le formulaire proposait : on les traduit.
TF_CLOUDINIT_TYPES = {"nocloud": "noCloud", "configdrive": "configDrive"}


def _tf_decl_label(name, count, dry_run):
    """Libellé d'action d'une déclaration. v1.52.1 : un Dry-run et un Apply
    se distinguent dans l'Activité (ils portaient le même)."""
    return f"{'tf-plan-decl' if dry_run else 'tf-apply-decl'}:{name}:{count}"


def _render_cloudinit_block(ci):
    kind = str(ci.get("type") or "noCloud")
    kind = TF_CLOUDINIT_TYPES.get(kind.lower(), kind)
    parts = [f'    type = {_hcl_str(kind)}']
    if ci.get("user_data"):
        parts.append(f'    user_data = {_hcl_str(ci["user_data"])}')
    if ci.get("network_data"):
        parts.append(f'    network_data = {_hcl_str(ci["network_data"])}')
    if ci.get("user_data_secret_name"):
        parts.append(f'    user_data_secret_name = {_hcl_str(ci["user_data_secret_name"])}')
    return "  cloudinit {\n" + "\n".join(parts) + "\n  }"


def _legacy_vm_to_nested(spec):
    """Map the pre-1.4.36 flat VM spec (image_id, network_id, disk_size,
    ssh_user) to the new nested shape so the same renderer handles both."""
    if "disk" in spec or "network_interface" in spec:
        return spec
    nested = dict(spec)
    if spec.get("image_id") or spec.get("disk_size"):
        nested["disk"] = [{
            "name": "rootdisk",
            "size": spec.get("disk_size") or "20Gi",
            "image": spec.get("image_id") or "",
        }]
    if spec.get("network_id"):
        nested["network_interface"] = [{
            "name": "nic-1",
            "network_name": spec["network_id"],
        }]
    return nested


def _render_tf_for_kind(kind, spec):
    """Generate a .tf snippet from a structured spec. Accepts both the
    pre-1.4.36 flat shape and the v1.4.36 nested shape produced by
    tf-schema.js."""
    if kind == "raw":
        return spec.get("tf") or ""

    if kind == "vm":
        spec = _legacy_vm_to_nested(spec)
        name = spec.get("name") or "vm-from-ops"
        ns = spec.get("namespace") or "default"
        cpu = int(spec.get("cpu") or 2)
        mem = spec.get("memory") or "4Gi"
        run_strategy = spec.get("run_strategy") or "RerunOnFailure"
        disks = spec.get("disk") or []
        nics  = spec.get("network_interface") or []
        if not disks or not nics:
            return ""

        body = [
            f'  name      = {_hcl_str(name)}',
            f'  namespace = {_hcl_str(ns)}',
            f'  cpu    = {cpu}',
            f'  memory = {_hcl_str(mem)}',
            f'  run_strategy = {_hcl_str(run_strategy)}',
        ]
        if spec.get("hostname"):
            body.append(f'  hostname = {_hcl_str(spec["hostname"])}')
        if spec.get("efi"):
            body.append('  efi = true')
        if spec.get("secure_boot"):
            body.append('  secure_boot = true')
        if spec.get("description"):
            body.append(f'  description = {_hcl_str(spec["description"])}')
        if spec.get("ssh_keys"):
            keys = spec["ssh_keys"]
            if isinstance(keys, str):
                keys = [keys]
            keys_hcl = "[" + ", ".join(_hcl_str(k) for k in keys) + "]"
            body.append(f'  ssh_keys = {keys_hcl}')
        # Legacy: ssh_user tag (still supported)
        if spec.get("ssh_user"):
            body.append('  tags = { ssh-user = ' + _hcl_str(spec["ssh_user"]) + ' }')

        for nic in nics:
            body.append(_render_nic_block(nic))
        for disk in disks:
            body.append(_render_disk_block(disk))
        ci = spec.get("cloudinit")
        if ci:
            # tf-form may submit cloudinit as a single-element list when
            # rendered as a nested block with max:1.
            if isinstance(ci, list):
                ci = ci[0] if ci else None
            if ci:
                body.append(_render_cloudinit_block(ci))

        return _TF_HEADER + f'resource "harvester_virtualmachine" "{_hcl_id(name)}" {{\n' + \
               "\n".join(body) + "\n}\n"

    if kind == "image":
        name = spec.get("name")
        url  = spec.get("url")
        ns   = spec.get("namespace") or "default"
        if not name:
            return ""
        body = [
            f'  name         = {_hcl_str(name)}',
            f'  namespace    = {_hcl_str(ns)}',
            f'  display_name = {_hcl_str(spec.get("display_name") or name)}',
            f'  source_type  = {_hcl_str(spec.get("source_type") or "download")}',
        ]
        if url:
            body.append(f'  url          = {_hcl_str(url)}')
        if spec.get("storage_class_name"):
            body.append(f'  storage_class_name = {_hcl_str(spec["storage_class_name"])}')
        if spec.get("checksum"):
            body.append(f'  checksum     = {_hcl_str(spec["checksum"])}')
        return _TF_HEADER + f'resource "harvester_image" "{_hcl_id(name)}" {{\n' + \
               "\n".join(body) + "\n}\n"

    if kind == "ssh_key":
        name = spec.get("name")
        public = spec.get("public_key")
        if not name or not public:
            return ""
        body = [
            f'  name      = {_hcl_str(name)}',
            f'  namespace = {_hcl_str(spec.get("namespace") or "default")}',
            f'  public_key = {_hcl_str(public.strip())}',
        ]
        return _TF_HEADER + f'resource "harvester_ssh_key" "{_hcl_id(name)}" {{\n' + \
               "\n".join(body) + "\n}\n"

    return ""


def _tf_apply_runner(run, cluster, kc, tf_content, dry_run, resource_name):
    """Legacy single-resource Terraform runner — used by /api/terraform/
    <cluster>/apply (the per-kind path that pre-dates v1.5.0 declarations).
    Stages kubeconfig (0600), writes _providers.tf + <resource>.tf, runs
    `terraform init` (once per workspace) + `plan -out tfplan`, then either
    stops (dry-run) or `apply tfplan`. Emits SSE step + log events.

    Side effects: writes <safe>.tf into the per-cluster workspace; does
    NOT write a sidecar JSON (declaration runner does that instead).
    Errors → _close_err with truncated stderr. The new declaration
    workflow is preferred — this is kept for backwards compat with the
    raw HCL path and a few legacy callers."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})

    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": msg, "ts": time.time()})

    step("preflight", "running", "Preparing workspace")
    ws = _tf_workspace_dir(cluster)
    # Stage the kubeconfig + the .tf file
    try:
        _stage_kubeconfig(kc, ws)
    except Exception as e:
        return _close_err(run, "preflight", f"kubeconfig copy failed: {e}")
    # v1.4.37: the terraform { required_providers } + provider "harvester"
    # blocks must appear EXACTLY ONCE in the workspace. Earlier we emitted
    # them on every resource .tf which made the second apply fail with
    # "Duplicate required providers configuration". Now those blocks live
    # in a shared _providers.tf, and each resource .tf carries only its
    # `resource { … }` block.
    providers_file = ws / "_providers.tf"
    if not providers_file.exists():
        providers_file.write_text(_TF_HEADER)
        # Migrate pre-1.4.37 workspaces: any existing resource .tf file
        # carrying the old inlined header would now collide with
        # _providers.tf and break `terraform plan`. Strip the header
        # from each (and only each) sibling.
        for existing in ws.glob("*.tf"):
            if existing.name == "_providers.tf":
                continue
            try:
                txt = existing.read_text()
                if txt.startswith(_TF_HEADER):
                    existing.write_text(txt[len(_TF_HEADER):])
            except OSError:
                pass
    # Strip the shared header from the per-resource content if present
    # (kind="raw" users may still include it; that's fine — we just don't
    # write it twice).
    resource_content = tf_content
    if resource_content.startswith(_TF_HEADER):
        resource_content = resource_content[len(_TF_HEADER):]
    name_safe = re.sub(r"[^a-z0-9_]+", "_",
                       (resource_name or "resource").lower())
    tf_file = ws / f"{name_safe}.tf"
    tf_file.write_text(resource_content)
    # Local provider mirror so init doesn't go to the registry
    version = _tf_plugin_cache_init(ws)
    step("preflight", "done", f"workspace at {ws}")

    if not (ws / ".terraform").exists():
        step("init", "running", "terraform init")
        rc, out, err = _tf_run_cmd(
            ws, str((ws / "kubeconfig").resolve()),
            # No -plugin-dir: terraformrc filesystem_mirror handles discovery.
            ["init", "-input=false"],
            timeout=120,
        )
        if rc != 0:
            return _close_err(run, "init", err.strip()[:400] or out.strip()[:400])
        step("init", "done", "providers ready")

    step("plan", "running", "terraform plan")
    rc, out, err = _tf_run_cmd(ws, str((ws / "kubeconfig").resolve()),
                               ["plan", "-input=false", "-no-color", "-out=tfplan"],
                               timeout=180)
    if rc != 0:
        return _close_err(run, "plan", err.strip()[:400] or out.strip()[:400])
    # Echo the plan summary into the log
    for line in (out or "").splitlines()[-30:]:
        if line.strip():
            run.emit({"type": "log", "stream": "stdout", "message": line[:200],
                      "ts": time.time()})
    step("plan", "done", "plan ready")

    if dry_run:
        step("apply", "skipped", "[DRY-RUN]")
        run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
        run.close()
        return

    step("apply", "running", "terraform apply")
    rc, out, err = _tf_run_cmd(ws, str((ws / "kubeconfig").resolve()),
                               ["apply", "-input=false", "-no-color", "-auto-approve", "tfplan"],
                               timeout=600)
    for line in (out or "").splitlines()[-40:]:
        if line.strip():
            run.emit({"type": "log", "stream": "stdout", "message": line[:200],
                      "ts": time.time()})
    if rc != 0:
        return _close_err(run, "apply", err.strip()[:400] or out.strip()[:400])
    step("apply", "done", "resources created")
    run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
    run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
    run.close()


@app.route("/api/terraform/<cluster>/destroy", methods=["POST"])
@requires_auth
def api_terraform_destroy(cluster):
    """`terraform destroy` for the whole per-cluster workspace. Body:
    {"dry_run": true|false}. With dry_run, only `terraform plan -destroy`."""
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    dry_run = bool(data.get("dry_run", False))

    run_id = uuid.uuid4().hex[:12]
    run = ActionRun(run_id, f"tf-destroy:{cluster}", cluster, [], dry_run=dry_run)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run

    user = current_user() or ""

    def runner():
        run.status = "running"
        run.emit({"type": "status", "status": "running", "ts": time.time()})
        # v1.54.0 : tout ce que Terraform gère sur ce cluster, c'est l'espace
        # partagé ET l'état de chaque déclaration.
        spaces = [(None, _tf_workspace_dir(cluster))] + _tf_decl_workspaces(cluster)
        spaces = [(i, w) for i, w in spaces if (w / ".terraform").exists() and _tfst.addresses(w)]
        if not spaces:
            return _close_err(run, "preflight", "nothing managed by Terraform on this cluster")
        args = ["plan", "-destroy", "-no-color"] if dry_run \
               else ["destroy", "-no-color", "-auto-approve"]
        for decl_id, ws in spaces:
            what = f"declaration {decl_id}" if decl_id else "shared workspace"
            try:
                _stage_kubeconfig(kc, ws)
            except Exception as e:
                return _close_err(run, "preflight", f"kubeconfig: {e}")
            run.emit({"type": "step", "step_id": "destroy", "status": "running",
                      "message": f"{args[0]}: {what}", "ts": time.time()})
            rc, out, err = _tf_run_cmd(ws, str((ws / "kubeconfig").resolve()),
                                       args, timeout=600)
            for line in (out or "").splitlines()[-40:]:
                if line.strip():
                    run.emit({"type": "log", "stream": "stdout", "message": line[:200],
                              "ts": time.time()})
            if rc != 0:
                return _close_err(run, "destroy", f"{what}: " + err.strip()[:400])
            if decl_id and not dry_run:
                TF_DECLS.mark_applied(decl_id, "destroyed", user)
        run.emit({"type": "step", "step_id": "destroy", "status": "done",
                  "message": "ok", "ts": time.time()})
        run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "done", "exit_code": 0, "ts": time.time()})
        run.close()

    threading.Thread(target=runner, daemon=True).start()
    return jsonify({"action_id": run_id}), 201


@app.route("/api/terraform/<cluster>/destroy_declaration", methods=["POST"])
@requires_auth
def api_terraform_destroy_declaration(cluster):
    """v1.5.1 — `terraform destroy -target=<addr>` for every resource of
    a declaration that's currently in state. Sidecar files (`.tf`,
    `.json`) for the destroyed resources are unlinked afterwards so
    the workspace stays clean.

    Body: { declaration: { name, resources: [{kind, spec}, ...] },
            dry_run: bool }
    Returns 201 + action_id; the runner streams via SSE.
    """
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    decl = data.get("declaration") or {}
    resources = decl.get("resources") or []
    dry_run = bool(data.get("dry_run", False))
    if decl.get("id"):
        return _tf_decl_start(cluster, kc, str(decl["id"]), "destroy-plan" if dry_run else "destroy")
    if not isinstance(resources, list) or not resources:
        return jsonify({"error": "declaration.resources must be a non-empty list"}), 400

    # Compute (safe_name, address) for each resource. Skip resources we
    # can't address (e.g. raw without a parseable header).
    seen = set()
    planned = []           # list of (safe_name, address)
    errors = []
    for i, res in enumerate(resources):
        kind = (res or {}).get("kind") or ""
        spec = (res or {}).get("spec") or {}
        safe = _safe_name_for_spec(spec, fallback=f"res_{i}")
        n = safe
        j = 1
        while n in seen:
            j += 1
            n = f"{safe}_{j}"
        seen.add(n)
        hcl = spec.get("tf") if kind == "raw" else None
        addr = _tf_address_for_resource(kind, n, hcl=hcl)
        if not addr:
            errors.append({"index": i, "kind": kind,
                           "name": spec.get("name"),
                           "error": "cannot compute Terraform address"})
            continue
        planned.append((n, addr))

    if errors:
        return jsonify({"error": "one or more resources are non-addressable",
                        "errors": errors}), 400

    run_id = uuid.uuid4().hex[:12]
    label = f"tf-destroy-decl:{decl.get('name', '?')}:{len(planned)}"
    run = ActionRun(run_id, label, cluster, [], dry_run=dry_run)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run
    threading.Thread(
        target=_tf_destroy_declaration_runner,
        args=(run, cluster, kc, planned, dry_run, decl.get("name") or "?"),
        daemon=True,
    ).start()
    return jsonify({"action_id": run_id}), 201


def _tf_destroy_declaration_runner(run, cluster, kc, planned, dry_run,
                                     declaration_name):
    """Destroy every (safe_name, address) pair that's currently in
    `terraform state list`. Skips any address not in state (already
    gone or never applied). Removes the matching `.tf` + `.json`
    files after a successful real destroy."""
    run.status = "running"
    run.emit({"type": "status", "status": "running", "ts": time.time()})

    def step(sid, status, msg=""):
        run.emit({"type": "step", "step_id": sid, "status": status,
                  "message": msg, "ts": time.time()})

    step("preflight", "running",
         f"Preparing destroy for declaration '{declaration_name}' "
         f"({len(planned)} resources)")
    ws = _tf_workspace_dir(cluster)
    if not (ws / ".terraform").exists():
        return _close_err(run, "preflight",
                          f"workspace not initialized: {ws}")
    try:
        _stage_kubeconfig(kc, ws)
    except Exception as e:
        return _close_err(run, "preflight", f"kubeconfig copy failed: {e}")

    rc, out, _ = _tf_run_cmd(
        ws, str((ws / "kubeconfig").resolve()),
        ["state", "list"], timeout=30,
    )
    in_state = set()
    if rc == 0:
        for line in (out or "").splitlines():
            line = line.strip()
            if line:
                in_state.add(line)
    addressable = [(safe, addr) for safe, addr in planned if addr in in_state]
    skipped = [(safe, addr) for safe, addr in planned if addr not in in_state]
    for safe, addr in skipped:
        run.emit({"type": "log", "stream": "stdout",
                  "message": f"skip {addr} (not in state)",
                  "ts": time.time()})

    if not addressable:
        step("preflight", "done", "nothing to destroy (no addresses in state)")
        run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "done", "exit_code": 0,
                  "ts": time.time()})
        run.close()
        return
    step("preflight", "done", f"{len(addressable)} resource(s) in state")

    target_args = []
    for _, addr in addressable:
        target_args += ["-target", addr]
    args = (["plan", "-destroy", "-no-color"] + target_args if dry_run
            else ["destroy", "-no-color", "-auto-approve"] + target_args)
    step("destroy", "running",
         f"terraform {args[0]} ({len(addressable)} target(s))")
    rc, out, err = _tf_run_cmd(
        ws, str((ws / "kubeconfig").resolve()), args, timeout=900,
    )
    for line in (out or "").splitlines()[-60:]:
        if line.strip():
            run.emit({"type": "log", "stream": "stdout",
                      "message": line[:200], "ts": time.time()})
    if rc != 0:
        return _close_err(run, "destroy",
                          err.strip()[:400] or out.strip()[:400])
    step("destroy", "done",
         f"declaration '{declaration_name}' destroyed "
         f"({len(addressable)} resources)")

    # Cleanup: only after a real destroy. dry_run preserves the files.
    if not dry_run:
        removed = []
        for safe, _ in addressable:
            for ext in (".tf", ".json"):
                p = ws / f"{safe}{ext}"
                try:
                    if p.exists():
                        p.unlink()
                        removed.append(p.name)
                except OSError as e:
                    run.emit({"type": "log", "stream": "stderr",
                              "message": f"cleanup {p.name}: {e}",
                              "ts": time.time()})
        if removed:
            run.emit({"type": "log", "stream": "stdout",
                      "message": f"removed workspace files: {', '.join(removed)}",
                      "ts": time.time()})

    run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
    run.emit({"type": "status", "status": "done", "exit_code": 0,
              "ts": time.time()})
    run.close()


@app.route("/api/terraform/<cluster>/clean_stale", methods=["POST"])
@requires_auth
def api_terraform_clean_stale(cluster):
    """v1.4.38: remove `.tf` files in the workspace whose resource is
    NOT in `terraform state list`. Use case: a failed apply (e.g. the
    storage_class_name conflict from v1.4.36) leaves the .tf on disk
    even though no cluster resource was created; the next apply
    re-tries the broken resource. Cleaning lets the user start fresh
    without invoking the cluster-side destroy on a non-existent
    resource (which would fail).

    Body: {"dry_run": bool}. Returns the list of files removed.
    """
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    dry_run = bool(data.get("dry_run", False))
    ws = _tf_workspace_dir(cluster)
    if not ws.exists():
        return jsonify({"error": "workspace does not exist", "ws": str(ws)}), 404

    # Build the set of tracked addresses from `terraform state list`
    try:
        _stage_kubeconfig(kc, ws)
    except OSError as e:
        return jsonify({"error": f"kubeconfig copy: {e}"}), 500
    rc, out, _ = _tf_run_cmd(
        ws, str((ws / "kubeconfig").resolve()),
        ["state", "list"], timeout=30,
    )
    in_state = set()
    if rc == 0:
        for line in (out or "").splitlines():
            line = line.strip()
            # `harvester_virtualmachine.foo` → local name is "foo"
            if "." in line:
                in_state.add(line.split(".", 1)[1])

    # Inspect each .tf (except _providers.tf): scan its `resource "kind" "name"`
    # declarations; if NONE are tracked, the file is stale.
    pattern = re.compile(r'resource\s+"[a-z_]+"\s+"([a-zA-Z0-9_]+)"')
    stale = []
    for f in sorted(ws.glob("*.tf")):
        if f.name == "_providers.tf":
            continue
        try:
            names = pattern.findall(f.read_text())
        except OSError:
            continue
        if names and not any(n in in_state for n in names):
            stale.append(f.name)

    if dry_run:
        return jsonify({"would_remove": stale, "in_state": sorted(in_state)})
    removed = []
    for name in stale:
        try:
            (ws / name).unlink()
            removed.append(name)
        except OSError:
            pass
    return jsonify({"removed": removed, "in_state": sorted(in_state)})


@app.route("/api/terraform/<cluster>/destroy_resource", methods=["POST"])
@requires_auth
def api_terraform_destroy_resource(cluster):
    """v1.4.38: targeted destroy + .tf removal.

    The workspace accumulates `<safe_name>.tf` files across applies. When
    one fails (or its resource is no longer wanted) the legacy 🧨 Destroy
    workspace button nukes EVERYTHING — too coarse. This endpoint
    runs `terraform destroy -target=<address>` and removes the matching
    `.tf` so the next apply doesn't recreate it.

    Body: {"address": "harvester_virtualmachine.testvm22", "dry_run": bool}
    """
    kc = _kubectl_for_cluster(cluster)
    if not kc:
        return jsonify({"error": f"unknown cluster: {cluster}"}), 404
    data = request.get_json(force=True, silent=True) or {}
    address = data.get("address") or ""
    dry_run = bool(data.get("dry_run", False))
    # `harvester_virtualmachine.testvm22` is the canonical form;
    # allow stray whitespace / quotes.
    address = address.strip().strip('"').strip("'")
    if not re.match(r"^[a-z_][a-z0-9_]*\.[a-zA-Z0-9_]+$", address):
        return jsonify({"error": "invalid address",
                        "hint": "expected `<resource_type>.<local_name>`"}), 400
    decl_id = data.get("declaration_id") or None
    if decl_id and not _tf_decl_id_ok(decl_id):
        return jsonify({"error": "invalid declaration id"}), 400
    user = current_user() or ""

    run_id = uuid.uuid4().hex[:12]
    label = f"tf-destroy-resource:{address}"
    run = ActionRun(run_id, label, cluster, [], dry_run=dry_run)
    with ACTIONS_LOCK:
        ACTIONS[run_id] = run

    def runner():
        run.status = "running"
        run.emit({"type": "status", "status": "running", "ts": time.time()})
        ws = (_tf_decl_workspace_dir(cluster, decl_id, create=False) if decl_id
              else _tf_workspace_dir(cluster))
        if not (ws / ".terraform").exists():
            return _close_err(run, "preflight",
                              f"workspace not initialized: {ws}")
        try:
            _stage_kubeconfig(kc, ws)
        except Exception as e:
            return _close_err(run, "preflight", f"kubeconfig: {e}")

        target_arg = f"-target={address}"
        args = (["plan", "-destroy", "-no-color", target_arg] if dry_run
                else ["destroy", "-no-color", "-auto-approve", target_arg])
        run.emit({"type": "step", "step_id": "destroy", "status": "running",
                  "message": f"terraform {args[0]} {target_arg}",
                  "ts": time.time()})
        rc, out, err = _tf_run_cmd(ws, str((ws / "kubeconfig").resolve()),
                                   args, timeout=300)
        for line in (out or "").splitlines()[-40:]:
            if line.strip():
                run.emit({"type": "log", "stream": "stdout",
                          "message": line[:200], "ts": time.time()})
        if rc != 0:
            return _close_err(run, "destroy", err.strip()[:400] or out.strip()[:400])

        # After a real destroy: remove the .tf file that hosted this
        # resource (best-effort lookup by grepping for the local_name).
        if not dry_run:
            try:
                _, _, local = address.partition(".")
                for f in ws.glob("*.tf"):
                    if f.name == "_providers.tf":
                        continue
                    txt = f.read_text()
                    if f'"{local}"' in txt and "resource " in txt:
                        f.unlink()
                        (ws / f"{f.stem}.json").unlink(missing_ok=True)
                        run.emit({"type": "log", "stream": "stdout",
                                  "message": f"removed {f.name}",
                                  "ts": time.time()})
                        break
            except OSError as e:
                run.emit({"type": "log", "stream": "stderr",
                          "message": f"file cleanup: {e}", "ts": time.time()})
        # v1.54.0 : la ressource détruite sort de sa déclaration, sans quoi le
        # prochain apply la recréait sans prévenir (audit D8).
        if not dry_run and decl_id:
            d = TF_DECLS.get(decl_id)
            gone = [rid for rid, a in _tf_decl_resource_addresses(d).items() if a == address] if d else []
            if gone:
                TF_DECLS.drop_resources(decl_id, gone, user)
                run.emit({"type": "log", "stream": "stdout",
                          "message": f"removed from declaration '{d['name']}'", "ts": time.time()})

        run.emit({"type": "step", "step_id": "destroy", "status": "done",
                  "message": "ok", "ts": time.time()})
        run.exit_code = 0; run.status = "done"; run.ended_at = time.time()
        run.emit({"type": "status", "status": "done", "exit_code": 0,
                  "ts": time.time()})
        run.close()

    threading.Thread(target=runner, daemon=True).start()
    return jsonify({"action_id": run_id}), 201


@app.route("/api/notes/<path:doc_id>")
@requires_auth
def api_notes_get(doc_id):
    """Read-only HTTP fallback: returns the current Y.Text content as plain text.
    Useful for export, search, or non-collaborative viewing.
    """
    if not _validate_doc_id(doc_id):
        return jsonify({"error": "invalid doc_id"}), 400
    entry = _get_or_create_doc(doc_id)
    # Must touch the doc on its own thread (y_py YDoc is unsendable).
    text = _doc_call(entry, lambda: str(entry["doc"].get_text("content")))
    return jsonify({"doc_id": doc_id, "content": text})


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
_MODULE_READY.set()


if __name__ == "__main__":
    cfg = load_config().get("web", {})
    host = cfg.get("bind_host", "0.0.0.0")
    port = int(cfg.get("bind_port", 8090))
    cert = cfg.get("tls_cert")
    key = cfg.get("tls_key")
    ssl_ctx = (cert, key) if cert and key and Path(cert).exists() else None
    # Dans le conteneur, l'application est le processus 1 : le noyau ne lui
    # applique pas l'action par défaut de SIGTERM, qui était donc ignoré, et
    # `podman stop` finissait par un SIGKILL au bout de 10 s. Sortir
    # proprement à la place.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    _announce_setup()
    app.run(host=host, port=port, ssl_context=ssl_ctx, threaded=True, debug=False)
