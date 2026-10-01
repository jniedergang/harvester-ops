"""harvester-ops : lectures Kubernetes sans kubectl (v1.83.0).

Mesuré sur un banc de 30 clusters de 200 VMs : 94 % du CPU de la console
partait dans les `kubectl get -o json` qu'elle lance (chaque appel décode puis
réencode toute la liste), 9,8 cœurs sur 10,5 avec 30 personnes. Ce module fait
les mêmes lectures directement contre l'API, en bibliothèque standard, et rend
exactement ce que kubectl aurait écrit : une `List` dont chaque objet porte son
`kind` et son `apiVersion`, l'objet seul pour un nom, et en cas de refus un code
de sortie 1 avec le message de kubectl (« Error from server (Forbidden): ... »),
que la console sait reconnaître.

Ne traite que `get` en JSON, avec les options connues ; tout le reste (autre
verbe, autre sortie, option inconnue, authentification par plugin `exec`,
proxy) est rendu à kubectl : `run()` répond None et l'appelant lance kubectl
comme avant. Désactivable : HARVESTER_OPS_KUBE_REST=0.
"""
import base64
import hashlib
import http.client
import io
import json
import os
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml as _yaml
    _Loader = getattr(_yaml, "CSafeLoader", _yaml.SafeLoader)
except ImportError:          # pragma: no cover  (outils bin/ sans PyYAML)
    _yaml = None

ENABLED = os.environ.get("HARVESTER_OPS_KUBE_REST", "1") not in ("0", "false", "no")
DISCOVERY_TTL = 600.0
_lock = threading.Lock()
_configs = {}        # chemin -> (signature du fichier, Conn ou None)
_discovery = {}      # serveur -> (instant, {nom: Resource})
_key_dir = None


class Unsupported(Exception):
    """Cas laissé à kubectl."""


class Result:
    """Même forme que subprocess.CompletedProcess pour les appelants. `stdout`
    n'est écrit que si quelqu'un le lit (texte ou octets selon `text`) :
    l'appelant qui prend `data` ne paie pas la sérialisation."""

    def __init__(self, returncode, stdout="", stderr="", data=None, text=True):
        self.returncode = returncode
        self._stdout = stdout
        self.stderr = stderr
        self.data = data          # objet déjà analysé (évite de relire stdout)
        self.text = text

    @property
    def stdout(self):
        if self._stdout is None:
            self._stdout = json.dumps(self.data)
        if not self.text and isinstance(self._stdout, str):
            self._stdout = self._stdout.encode()
        return self._stdout

    @stdout.setter
    def stdout(self, value):
        self._stdout = value

    def as_bytes(self):
        """Mode binaire de subprocess (sans `text=True`)."""
        self.text = False
        if isinstance(self.stderr, str):
            self.stderr = self.stderr.encode()
        return self


class Resource:
    __slots__ = ("group", "version", "plural", "kind", "namespaced")

    def __init__(self, group, version, plural, kind, namespaced):
        self.group, self.version, self.plural = group, version, plural
        self.kind, self.namespaced = kind, namespaced

    @property
    def api_version(self):
        return f"{self.group}/{self.version}" if self.group else self.version

    def path(self, namespace=None, name=None):
        base = f"/apis/{self.group}/{self.version}" if self.group else f"/api/{self.version}"
        if self.namespaced and namespace:
            base += f"/namespaces/{urllib.parse.quote(namespace)}"
        base += f"/{self.plural}"
        if name:
            base += f"/{urllib.parse.quote(name)}"
        return base


# ---------------------------------------------------------------------------
# kubeconfig -> connexion
# ---------------------------------------------------------------------------
class Conn:
    """Connexions HTTPS gardées ouvertes et réemployées (une poignée de main
    TLS par lecture coûtait autant que le décodage, mesuré sur le banc). Avec
    un proxy d'environnement, urllib s'en charge comme avant."""
    POOL_MAX = 8

    def __init__(self, server, ctx, headers, namespace="default"):
        self.server = server.rstrip("/")
        self.ctx = ctx
        self.headers = headers
        self.namespace = namespace
        u = urllib.parse.urlsplit(self.server)
        self._scheme, self._host, self._port = u.scheme, u.hostname, u.port
        self._prefix = u.path.rstrip("/")
        self._idle = []
        self._pool_lock = threading.Lock()
        self._proxied = bool(urllib.request.getproxies().get(u.scheme)) and \
            not urllib.request.proxy_bypass(u.hostname or "")

    def _new(self, timeout):
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=timeout, context=self.ctx)
        return http.client.HTTPConnection(self._host, self._port, timeout=timeout)

    def get(self, path, query=None, timeout=30, accept="application/json", _retry=True):
        target = self._prefix + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        hdrs = {**self.headers, "Accept": accept}
        if self._proxied:
            req = urllib.request.Request(self.server + target[len(self._prefix):], headers=hdrs)
            with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as r:
                return json.loads(r.read())
        for attempt in (0, 1):
            c = None
            if attempt == 0:            # le second essai part sur une connexion neuve
                with self._pool_lock:
                    c = self._idle.pop() if self._idle else None
            reused = c is not None
            if c is None:
                c = self._new(timeout)
            try:
                c.timeout = timeout
                if c.sock is not None:
                    c.sock.settimeout(timeout)
                c.request("GET", target, headers=hdrs)
                r = c.getresponse()
                body = r.read()
            except (OSError, http.client.HTTPException):
                c.close()
                if reused and attempt == 0:
                    continue            # connexion gardée fermée par le serveur : une nouvelle
                raise
            if r.will_close:
                c.close()
            else:
                with self._pool_lock:
                    if len(self._idle) < self.POOL_MAX:
                        self._idle.append(c)
                    else:
                        c.close()
            if r.status == 429 and _retry:
                # le serveur demande de patienter (priorité et équité de
                # l'API) : kubectl attend et réessaie, faisons de même
                try:
                    wait = min(float(r.headers.get("Retry-After") or 1), 3.0)
                except ValueError:
                    wait = 1.0
                time.sleep(wait)
                return self.get(path, query, timeout, accept, _retry=False)
            if r.status >= 400:
                raise urllib.error.HTTPError(self.server + target, r.status, r.reason, r.headers,
                                             io.BytesIO(body))
            return json.loads(body)


def _b64_file(data, suffix):
    """Les certificats en ligne du kubeconfig, en fichiers 0600 (ssl ne lit
    une clé client que depuis un fichier), dans un répertoire 0700 propre au
    processus, nommés par empreinte."""
    global _key_dir
    if _key_dir is None:
        _key_dir = tempfile.mkdtemp(prefix="hops-kube-")
        os.chmod(_key_dir, 0o700)
    raw = base64.b64decode(data)
    p = Path(_key_dir) / (hashlib.sha256(raw).hexdigest()[:24] + suffix)
    if not p.exists():
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
    return str(p)


def _resolve(base, path):
    return path if not path or os.path.isabs(path) else str(Path(base).parent / path)


def conn_for(kubeconfig):
    """Connexion du contexte courant, gardée tant que le fichier ne change
    pas ; Unsupported si l'authentification demande kubectl."""
    st = os.stat(kubeconfig)
    sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    with _lock:
        hit = _configs.get(kubeconfig)
    if hit and hit[0] == sig:
        if hit[1] is None:
            raise Unsupported("kubeconfig handled by kubectl")
        return hit[1]
    try:
        conn = _build_conn(kubeconfig)
    except Unsupported:
        with _lock:
            _configs[kubeconfig] = (sig, None)
        raise
    with _lock:
        _configs[kubeconfig] = (sig, conn)
    return conn


def _build_conn(path):
    if _yaml is None:
        raise Unsupported("no YAML parser")
    cfg = _yaml.load(Path(path).read_text(), Loader=_Loader) or {}
    cur = cfg.get("current-context")
    ctx = next((c.get("context") or {} for c in cfg.get("contexts") or [] if c.get("name") == cur), None)
    if ctx is None:
        raise Unsupported("no current context")
    cl = next((c.get("cluster") or {} for c in cfg.get("clusters") or [] if c.get("name") == ctx.get("cluster")), None)
    us = next((u.get("user") or {} for u in cfg.get("users") or [] if u.get("name") == ctx.get("user")), {})
    if not cl or not cl.get("server"):
        raise Unsupported("no server")
    if cl.get("proxy-url") or us.get("exec") or us.get("auth-provider") or us.get("tokenFile") \
            or us.get("username"):
        raise Unsupported("authentication left to kubectl")
    sctx = ssl.create_default_context()
    if cl.get("insecure-skip-tls-verify"):
        sctx.check_hostname = False
        sctx.verify_mode = ssl.CERT_NONE
    elif cl.get("certificate-authority-data"):
        sctx.load_verify_locations(cadata=base64.b64decode(cl["certificate-authority-data"]).decode())
    elif cl.get("certificate-authority"):
        sctx.load_verify_locations(cafile=_resolve(path, cl["certificate-authority"]))
    if cl.get("tls-server-name"):
        raise Unsupported("tls-server-name")
    headers = {"User-Agent": "harvester-ops"}
    if us.get("token"):
        headers["Authorization"] = "Bearer " + us["token"]
    cert = us.get("client-certificate-data") and _b64_file(us["client-certificate-data"], ".crt") \
        or (us.get("client-certificate") and _resolve(path, us["client-certificate"]))
    key = us.get("client-key-data") and _b64_file(us["client-key-data"], ".key") \
        or (us.get("client-key") and _resolve(path, us["client-key"]))
    if cert and key:
        sctx.load_cert_chain(cert, key)
    elif cert or key:
        raise Unsupported("incomplete client certificate")
    # usurpation portée par le kubeconfig (délégation d'identité de la console)
    if us.get("as"):
        headers["Impersonate-User"] = us["as"]
    if us.get("as-groups"):
        # urllib ne répète pas un en-tête : plusieurs groupes restent à kubectl
        groups = list(us["as-groups"])
        if len(groups) > 1:
            raise Unsupported("several impersonated groups")
        headers["Impersonate-Group"] = groups[0]
    if us.get("as-user-extra"):
        raise Unsupported("impersonation extras")
    return Conn(cl["server"], sctx, headers, ctx.get("namespace") or "default")


# ---------------------------------------------------------------------------
# Découverte des ressources
# ---------------------------------------------------------------------------
def resources(conn, timeout=30):
    with _lock:
        hit = _discovery.get(conn.server)
    if hit and time.time() - hit[0] < DISCOVERY_TTL:
        return hit[1]
    table = {}

    def add(group, version, r):
        if "/" in r["name"]:            # sous-ressource
            return
        res = Resource(group, version, r["name"], r["kind"], bool(r.get("namespaced")))
        keys = [r["name"], r.get("singularName") or "", r["kind"].lower()] + list(r.get("shortNames") or [])
        for k in filter(None, keys):
            table.setdefault(k, res)
            if group:
                table.setdefault(f"{k}.{group}", res)
                table.setdefault(f"{k}.{version}.{group}", res)

    core = conn.get("/api/v1", timeout=timeout)
    for r in core.get("resources", []):
        add("", "v1", r)
    groups = conn.get("/apis", timeout=timeout).get("groups", [])
    from concurrent.futures import ThreadPoolExecutor

    def one(g):
        gv = (g.get("preferredVersion") or {}).get("groupVersion")
        if not gv:
            return g["name"], gv, []
        try:
            return g["name"], gv, conn.get(f"/apis/{gv}", timeout=timeout).get("resources", [])
        except (urllib.error.URLError, OSError, ValueError):
            return g["name"], gv, []          # groupe d'une API agrégée absente
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, gv, rs in pool.map(one, groups):
            for r in rs:
                add(name, gv.split("/", 1)[1], r)
    with _lock:
        _discovery[conn.server] = (time.time(), table)
    return table


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------
_FLAGS_WITH_VALUE = {"-n": "namespace", "--namespace": "namespace", "-l": "selector",
                     "--selector": "selector", "--field-selector": "field", "-o": "output",
                     "--output": "output", "--request-timeout": "timeout", "--kubeconfig": "kubeconfig"}


def parse_get(argv):
    """argv de kubectl -> requête, ou Unsupported."""
    args = list(argv)
    if args and os.path.basename(args[0]) == "kubectl":
        args = args[1:]
    opts = {"all": False}
    pos = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-A", "--all-namespaces"):
            opts["all"] = True
        elif a in _FLAGS_WITH_VALUE:
            if i + 1 >= len(args):
                raise Unsupported(a)
            opts[_FLAGS_WITH_VALUE[a]] = args[i + 1]
            i += 1
        elif a.startswith("--") and "=" in a and a.split("=", 1)[0] in _FLAGS_WITH_VALUE:
            k, v = a.split("=", 1)
            opts[_FLAGS_WITH_VALUE[k]] = v
        elif a.startswith("-"):
            raise Unsupported(a)
        else:
            pos.append(a)
        i += 1
    if not pos or pos[0] != "get" or opts.get("output") != "json" or len(pos) < 2:
        raise Unsupported("not a get -o json")
    kinds = pos[1]
    if "/" in kinds:
        raise Unsupported("type/name form")
    opts["kinds"] = kinds.split(",")
    opts["names"] = pos[2:]
    if opts["names"] and len(opts["kinds"]) > 1:
        raise Unsupported("names with several types")
    return opts


def _timeout(opts, default):
    t = opts.get("timeout")
    if not t:
        return default
    try:
        return float(t.rstrip("s")) if t.endswith("s") else float(t)
    except ValueError:
        raise Unsupported("timeout")


def _with_kind(item, res):
    item.setdefault("kind", res.kind)
    item.setdefault("apiVersion", res.api_version)
    return item


def _status_error(e, res, name=None):
    """Le message de kubectl pour une erreur de l'API."""
    try:
        st = json.loads(e.read())
        reason, msg = st.get("reason") or "", st.get("message") or ""
    except (ValueError, AttributeError, OSError):
        reason, msg = "", ""
    if e.code == 404 and name and not msg:
        msg = f'{res.plural}.{res.group} "{name}" not found' if res.group else f'{res.plural} "{name}" not found'
    return f"Error from server ({reason or e.code}): {msg}".rstrip()


def run(argv, timeout=30, kubeconfig=None):
    """Result comme kubectl l'aurait donné, ou None pour laisser kubectl."""
    if not ENABLED:
        return None
    try:
        opts = parse_get(argv)
        kc = opts.get("kubeconfig") or kubeconfig or os.environ.get("KUBECONFIG")
        if not kc or os.pathsep in kc:
            raise Unsupported("kubeconfig")
        to = _timeout(opts, timeout)
        try:
            conn = conn_for(kc)
        except (OSError, ValueError, ssl.SSLError, Exception) as e:
            # kubeconfig absent, illisible ou d'une forme non prévue : kubectl
            # dira lui-même ce qui ne va pas
            if isinstance(e, Unsupported):
                raise
            return None
        table = resources(conn, timeout=to)
        rs = []
        for k in opts["kinds"]:
            res = table.get(k.lower())
            if res is None:
                return Result(1, "", f'error: the server doesn\'t have a resource type "{k}"')
            rs.append(res)
        ns = None if opts["all"] else (opts.get("namespace") or conn.namespace)
        query = {}
        if opts.get("selector"):
            query["labelSelector"] = opts["selector"]
        if opts.get("field"):
            query["fieldSelector"] = opts["field"]
        if opts["names"]:
            res = rs[0]
            if res.namespaced and opts["all"]:
                raise Unsupported("named get across namespaces")
            objs = []
            for n in opts["names"]:
                try:
                    objs.append(_with_kind(conn.get(res.path(ns, n), timeout=to), res))
                except urllib.error.HTTPError as e:
                    return Result(1, "", _status_error(e, res, n))
            data = objs[0] if len(objs) == 1 else {"apiVersion": "v1", "kind": "List", "items": objs,
                                                   "metadata": {"resourceVersion": ""}}
        else:
            items = []
            for res in rs:
                try:
                    lst = conn.get(res.path(ns if res.namespaced else None), query or None, timeout=to)
                except urllib.error.HTTPError as e:
                    return Result(1, "", _status_error(e, res))
                items.extend(_with_kind(it, res) for it in lst.get("items") or [])
            data = {"apiVersion": "v1", "kind": "List", "items": items, "metadata": {"resourceVersion": ""}}
        return Result(0, None, "", data)
    except Unsupported:
        return None
    except (urllib.error.URLError, OSError, ssl.SSLError) as e:
        # cluster injoignable : répondre comme kubectl, sans le relancer (il
        # attendrait le même délai une seconde fois)
        why = getattr(e, "reason", None) or e
        return Result(1, "", f"Unable to connect to the server: {why}")
    except ValueError:
        return None


def stdout_of(result):
    """Le texte que kubectl aurait écrit (pour les appelants qui relisent stdout)."""
    return result.stdout
