"""Client OpenBao (2.9.0), bibliothèque standard seulement.

Parle à un OpenBao (ou à un Vault compatible, API commune) : KV v2, AppRole,
enveloppes de réponse, jetons, capacités, transit, instantanés raft, état du
scellement. Aucune dépendance (`urllib.request`, `ssl`, `json`).

Règles (plan 2.9.0, « Client OpenBao ») :

* délai par requête réglable (10 s par défaut ; la configuration le borne de 5
  à 60 par `clamp_timeout`), réessais espacés de 0,5, 1 et 2 s sur erreur
  réseau et 5xx, jamais sur 4xx, jamais sur une erreur de certificat ;
* jeton dans `X-Vault-Token` ; en-têtes de corrélation `X-Hops-Action` et
  `X-Hops-Actor` sur chaque requête ;
* `VAULT_*` et `BAO_*` de l'environnement ignorés (seule la configuration
  passée au constructeur compte), proxys de l'environnement ignorés aussi ;
* aucune valeur dans un message d'erreur, `repr`, `str`, une sérialisation :
  le corps d'une réponse d'erreur est réduit au code HTTP et à la liste
  `errors` d'OpenBao, tronquée à 300 caractères et filtrée des chaînes qui
  ressemblent à un jeton ;
* codes d'erreur stables (`OpenBaoError.code`), voir `CODES`.

Réponses réelles relevées par les essais préalables (tâche 1) et reproduites
par le faux serveur des tests :

* `cas=0` sur un chemin existant : 400 « check-and-set parameter did not
  match the current version », même quand la valeur est identique ;
* enveloppe déjà déballée, expirée ou inconnue : 400 « wrapping token is not
  valid or does not exist » (les trois cas sont indiscernables) ;
* scellé : 503 « Vault is sealed » ;
* journal d'audit plein : 500 « internal error » sur toute requête
  journalisée, alors que `sys/health` dit descellé ;
* jeton hors de sa plage d'adresses : 403 « permission denied », comme une
  capacité manquante.
"""

import base64
import http.client
import json
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 10
TIMEOUT_MIN, TIMEOUT_MAX = 5, 60
RETRY_DELAYS = (0.5, 1, 2)
DETAIL_MAX = 300
# Jetons OpenBao (`s.`, `b.`, `r.`) et Vault (`hvs.`, `hvb.`, `hvr.`).
TOKEN_RE = re.compile(r"\b(?:hvs|hvb|hvr|s|b|r)\.[A-Za-z0-9_-]{20,}")
_HEADER_RE = re.compile(r"[^A-Za-z0-9._@:+-]")
# `sys/health` répond par des codes choisis selon l'état (actif, veille,
# scellé...) avec un corps JSON : ce ne sont pas des erreurs.
HEALTH_CODES = (200, 429, 472, 473, 501, 503)

CODES = (
    "openbao-unreachable",    # réseau, délai, 5xx autres que 500 répétés
    "openbao-tls",            # certificat refusé : aucun réessai
    "openbao-sealed",         # 503 « sealed »
    "openbao-denied",         # 403
    "openbao-login-failed",   # connexion AppRole refusée, jeton non renouvelable
    "openbao-unwrap-used",    # enveloppe déjà déballée, expirée, ou mauvais chemin
    "openbao-cas-conflict",   # check-and-set refusé
    "openbao-not-kv2",        # montage absent ou en version 1
    "openbao-missing",        # 404
    "openbao-internal",       # 500 répétés (journal d'audit plein ?)
    "openbao-bad-request",    # autre 4xx
)

_CAS_MSG = "check-and-set parameter did not match"
_UNWRAP_MSG = "wrapping token is not valid or does not exist"


def clamp_timeout(value):
    """Délai par requête borné à [5, 60] s, 10 par défaut (réglages)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(TIMEOUT_MIN, min(TIMEOUT_MAX, v))


def scrub(text):
    """Texte sans jeton, tronqué à DETAIL_MAX caractères."""
    text = TOKEN_RE.sub("[token]", str(text))
    return text[:DETAIL_MAX]


def header_value(value, default):
    """Valeur d'en-tête sûre (ASCII restreint, 128 caractères au plus) : un
    nom de personne ne peut ni injecter un en-tête ni casser l'encodage."""
    v = _HEADER_RE.sub("_", str(value if value not in (None, "") else default))
    return v[:128]


class OpenBaoError(Exception):
    """Erreur du client. `detail` ne porte jamais de valeur : seulement le
    chemin, le code HTTP et la liste `errors` d'OpenBao filtrée."""

    def __init__(self, code, http_status=None, detail=""):
        self.code = code
        self.http_status = http_status
        self.detail = scrub(detail)
        super().__init__(self.code, self.http_status, self.detail)

    def __str__(self):
        st = f" (HTTP {self.http_status})" if self.http_status else ""
        return f"{self.code}{st}: {self.detail}" if self.detail else f"{self.code}{st}"

    def __repr__(self):
        return f"OpenBaoError({self.code!r}, {self.http_status!r}, {self.detail!r})"

    def __reduce__(self):
        return (OpenBaoError, (self.code, self.http_status, self.detail))


class Written(int):
    """Numéro de version rendu par `kv_put`. `metadata_error` vaut le code
    d'erreur de la seconde requête (`custom_metadata`) si elle a échoué : la
    valeur reste écrite, l'appelant le signale dans son étape."""

    metadata_error = None


class _Token:
    """Jeton et identifiants AppRole d'un client, partagés par ses copies
    (`with_headers`). Rien de tout cela ne se sérialise ni ne s'affiche."""

    def __init__(self):
        self.lock = threading.RLock()
        self.value = None
        self.ttl = 0
        self.obtained = 0.0
        self.renewable = False
        self.role_id = None
        self.secret_id = None

    def clear(self):
        self.value, self.ttl, self.obtained, self.renewable = None, 0, 0.0, False

    def __repr__(self):
        return "<token>"

    def __getstate__(self):
        return {}

    def __setstate__(self, _state):
        self.__init__()


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Redirection refusée (le code 3xx remonte) si elle quitte https ou,
    en http, l'origine de la requête : le jeton ne part jamais en clair
    ni vers un autre hôte sans TLS vérifié par la CA de la console."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if not (new.scheme == "https" and old.scheme == "https"
                or (new.scheme, new.netloc) == (old.scheme, old.netloc)):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _errors_of(raw):
    try:
        body = json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        return ""
    errs = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errs, list):
        return "; ".join(str(e) for e in errs)
    return ""


class Client:
    """Client d'un serveur OpenBao. Un objet par processus et par serveur ;
    `with_headers` en donne une copie (même jeton) pour une action."""

    def __init__(self, addr, ca_pem=None, timeout=DEFAULT_TIMEOUT, actor="service", action=None,
                 retry_delays=RETRY_DELAYS, sleep=time.sleep, clock=time.monotonic):
        u = urllib.parse.urlparse(str(addr or ""))
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("invalid OpenBao address")
        self.addr = f"{u.scheme}://{u.netloc}"
        self._ca_pem = ca_pem
        self.timeout = float(timeout)
        self.actor = actor
        self.action = action
        self._delays = tuple(retry_delays)
        self._sleep = sleep
        self._clock = clock
        self._tok = _Token()
        self._kv2 = {}          # montage -> True (vérifié une fois)
        self._ctx = None
        if u.scheme == "https":
            self._ctx = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
        # Aucun proxy : ni celui de l'environnement, ni un autre. Redirection
        # suivie seulement vers https (CA épinglée) ou la même origine : un
        # nœud de veille qui renvoie vers un api_addr en http ne reçoit
        # jamais le jeton en clair (revue M2).
        handlers = [urllib.request.ProxyHandler({}), _SafeRedirect()]
        if self._ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=self._ctx))
        self._opener = urllib.request.build_opener(*handlers)

    # -- représentations sans secret -------------------------------------
    def __repr__(self):
        state = "logged-in" if self._tok.value else "anonymous"
        return f"<openbao.Client {self.addr} {state}>"

    __str__ = __repr__

    def __getstate__(self):
        # Un client ne se sérialise pas : ni jeton, ni identifiants.
        return {"addr": self.addr}

    def __setstate__(self, state):
        self.__init__(state.get("addr", "http://invalid"))

    def with_headers(self, action, actor):
        """Copie du client pour une action : mêmes réglages, même jeton
        (partagé : un renouvellement vaut pour toutes les copies)."""
        c = object.__new__(Client)
        c.__dict__.update(self.__dict__)
        c.action, c.actor = action, actor
        return c

    @property
    def logged_in(self):
        return self._tok.value is not None

    # -- transport -------------------------------------------------------
    def _headers(self, token):
        h = {"X-Hops-Action": header_value(self.action, "bg-console"),
             "X-Hops-Actor": header_value(self.actor, "service")}
        if token:
            h["X-Vault-Token"] = token
        return h

    def _once(self, method, path, body, token, raw_body, stream_to, extra):
        url = f"{self.addr}/v1/{path.lstrip('/')}"
        data, headers = None, self._headers(token)
        headers.update(extra or {})
        if raw_body is not None:
            data = raw_body
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with self._opener.open(req, timeout=self.timeout) as r:
            if stream_to is not None:
                n = 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    stream_to.write(chunk)
                    n += len(chunk)
                return r.status, n
            return r.status, r.read()

    def _request(self, method, path, body=None, token=True, ok=(), retry=True, raw_body=None,
                 stream_to=None, extra=None):
        """(code HTTP, corps JSON ou None). Lève OpenBaoError sinon."""
        tok = None
        if token is True:
            tok = self._token()
        elif isinstance(token, str):
            tok = token
        attempts = 1 + (len(self._delays) if retry else 0)
        last = None
        for i in range(attempts):
            if i:
                self._sleep(self._delays[i - 1])
                if stream_to is not None:
                    stream_to.seek(0)
                    stream_to.truncate()
            try:
                status, raw = self._once(method, path, body, tok, raw_body, stream_to, extra)
            except urllib.error.HTTPError as e:
                status = e.code
                try:
                    raw = e.read()
                except OSError:
                    raw = b""
                if status in ok:
                    return status, _json(raw)
                err = self._map_http(status, raw, path, method)
                if status >= 500 and err.code != "openbao-sealed":
                    last = err
                    continue
                raise err from None
            except urllib.error.URLError as e:
                reason = e.reason
                if isinstance(reason, ssl.SSLError) or isinstance(reason, ssl.CertificateError):
                    raise OpenBaoError("openbao-tls", None, f"{path}: {type(reason).__name__}") from None
                last = OpenBaoError("openbao-unreachable", None, f"{path}: {type(reason).__name__}")
                continue
            except ssl.SSLError as e:
                raise OpenBaoError("openbao-tls", None, f"{path}: {type(e).__name__}") from None
            except (OSError, http.client.HTTPException) as e:
                last = OpenBaoError("openbao-unreachable", None, f"{path}: {type(e).__name__}")
                continue
            if stream_to is not None:
                return status, raw
            return status, _json(raw)
        if last is not None and last.code == "openbao-internal":
            raise last
        if last is not None and last.http_status:
            raise OpenBaoError("openbao-unreachable", last.http_status, last.detail)
        raise last or OpenBaoError("openbao-unreachable", None, path)

    def _map_http(self, status, raw, path, method):
        errs = _errors_of(raw)
        detail = f"{method} {path}: {errs}" if errs else f"{method} {path}"
        low = errs.lower()
        if status == 503 and "sealed" in low:
            return OpenBaoError("openbao-sealed", status, detail)
        if status == 500:
            return OpenBaoError("openbao-internal", status, detail)
        if status >= 500 or 300 <= status < 400:
            if 300 <= status < 400:
                detail += " (redirect refused or not followed: standby node?)"
            return OpenBaoError("openbao-unreachable", status, detail)
        if status == 403:
            return OpenBaoError("openbao-denied", status, detail)
        if status == 404:
            return OpenBaoError("openbao-missing", status, detail)
        if _CAS_MSG in low:
            return OpenBaoError("openbao-cas-conflict", status, detail)
        if _UNWRAP_MSG in low:
            return OpenBaoError("openbao-unwrap-used", status, detail)
        return OpenBaoError("openbao-bad-request", status, detail)

    def _denied(self, err, path):
        """403 : ajoute au détail les capacités lues par
        `sys/capabilities-self` (jeton hors de sa plage ou capacité
        manquante : OpenBao rend le même 403)."""
        try:
            caps = self.capabilities([path]).get(path, [])
        except OpenBaoError:
            caps = ["?"]
        return OpenBaoError("openbao-denied", err.http_status,
                            f"{err.detail} (capabilities: {','.join(caps) or 'none'})")

    def _call(self, method, path, body=None, _relogged=False, **kw):
        try:
            return self._request(method, path, body, **kw)
        except OpenBaoError as e:
            if e.code == "openbao-denied" and kw.get("token", True) is True and self._tok.value:
                # Revue M3 : jeton révoqué ailleurs ou effacé par une
                # restauration d'instantané : une seule reconnexion AppRole,
                # puis la requête rejouée une fois.
                if not _relogged and self._token_invalid():
                    t = self._tok
                    with t.lock:
                        creds = (t.role_id, t.secret_id)
                        if creds[0] and creds[1]:
                            t.value = None
                            self._login(*creds)
                    if creds[0] and creds[1]:
                        return self._call(method, path, body, _relogged=True, **kw)
                raise self._denied(e, path) from None
            raise

    def _token_invalid(self):
        """Le jeton courant est-il refusé partout (lookup-self en 403) ?"""
        try:
            self._request("GET", "auth/token/lookup-self", retry=False)
        except OpenBaoError as e:
            return e.code == "openbao-denied"
        return False

    # -- jeton -----------------------------------------------------------
    def _token(self):
        """Jeton courant ; renouvelé aux deux tiers de sa durée, puis
        nouvelle connexion AppRole si le renouvellement échoue. Une seule
        tentative de chaque : jamais de boucle."""
        t = self._tok
        with t.lock:
            if t.value is None:
                if t.role_id and t.secret_id:
                    self._login(t.role_id, t.secret_id)
                    return t.value
                raise OpenBaoError("openbao-login-failed", None, "not logged in")
            if t.ttl and self._clock() - t.obtained >= t.ttl * 2 / 3:
                try:
                    self._renew()
                except OpenBaoError:
                    if not (t.role_id and t.secret_id):
                        t.clear()
                        raise OpenBaoError("openbao-login-failed", None, "token renewal failed") from None
                    t.clear()
                    self._login(t.role_id, t.secret_id)
            return t.value

    def _set_auth(self, auth):
        t = self._tok
        if not isinstance(auth, dict) or not auth.get("client_token"):
            raise OpenBaoError("openbao-login-failed", None, "no token in response")
        t.value = auth["client_token"]
        t.ttl = int(auth.get("lease_duration") or 0)
        t.renewable = bool(auth.get("renewable"))
        t.obtained = self._clock()

    def _login(self, role_id, secret_id):
        # `secret_id` peut être une fonction : la valeur est relue à chaque
        # connexion (secret_id tourné par l'agent de l'hôte, fichier réécrit)
        sid = secret_id() if callable(secret_id) else secret_id
        try:
            _, body = self._request("POST", "auth/approle/login",
                                    {"role_id": role_id, "secret_id": sid}, token=False)
        except OpenBaoError as e:
            if e.code in ("openbao-unreachable", "openbao-tls", "openbao-sealed", "openbao-internal"):
                raise
            raise OpenBaoError("openbao-login-failed", e.http_status, e.detail) from None
        self._set_auth((body or {}).get("auth"))

    def login_approle(self, role_id, secret_id):
        """Connexion AppRole ; le jeton reste dans l'objet (mémoire seule),
        les identifiants aussi pour une reconnexion si le renouvellement
        échoue."""
        t = self._tok
        with t.lock:
            t.clear()
            self._login(role_id, secret_id)
            t.role_id, t.secret_id = role_id, secret_id

    def set_token(self, token, ttl=0):
        """Jeton fourni (jeton d'amorce de l'agent, essais)."""
        t = self._tok
        with t.lock:
            t.value, t.ttl, t.obtained = token, int(ttl or 0), self._clock()

    def _renew(self):
        t = self._tok
        _, body = self._request("POST", "auth/token/renew-self", {}, token=t.value)
        self._set_auth((body or {}).get("auth"))

    def renew_self(self):
        with self._tok.lock:
            if self._tok.value is None:
                raise OpenBaoError("openbao-login-failed", None, "not logged in")
            self._renew()

    def token_lookup_self(self):
        _, body = self._call("GET", "auth/token/lookup-self")
        return (body or {}).get("data") or {}

    def revoke_self(self):
        t = self._tok
        with t.lock:
            if t.value is None:
                return
            try:
                self._request("POST", "auth/token/revoke-self", {}, token=t.value, retry=False)
            finally:
                t.clear()
                t.role_id = t.secret_id = None

    # -- état --------------------------------------------------------------
    def seal_status(self):
        _, body = self._request("GET", "sys/seal-status", token=False)
        return body or {}

    def health(self):
        _, body = self._request("GET", "sys/health", token=False, ok=HEALTH_CODES)
        return body or {}

    # -- enveloppes --------------------------------------------------------
    def unwrap(self, wrap_token, expect_path):
        """Vérifie `creation_path` par `sys/wrapping/lookup`, puis déballe.
        Mauvais chemin : `openbao-unwrap-used` sans déballer (quelqu'un a pu
        substituer une autre enveloppe). Déjà déballée ou expirée : même
        code (OpenBao ne les distingue pas)."""
        _, info = self._request("POST", "sys/wrapping/lookup", {"token": wrap_token}, token=False, retry=False)
        path = ((info or {}).get("data") or {}).get("creation_path")
        if path != expect_path:
            raise OpenBaoError("openbao-unwrap-used", None, f"unexpected creation path: {path}")
        _, body = self._request("POST", "sys/wrapping/unwrap", {}, token=wrap_token, retry=False)
        return (body or {}).get("data") or {}

    # -- capacités -----------------------------------------------------------
    def capabilities(self, paths):
        paths = list(paths)
        _, body = self._request("POST", "sys/capabilities-self", {"paths": paths})
        body = body or {}
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        return {p: list(data.get(p) or ([] if len(paths) > 1 else body.get("capabilities") or [])) for p in paths}

    # -- KV v2 ---------------------------------------------------------------
    @staticmethod
    def _seg(path):
        path = str(path).strip("/")
        if not path or any(s in ("", ".", "..") for s in path.split("/")):
            raise ValueError("invalid OpenBao path")
        return path

    def kv_mount_version(self, mount):
        """Version du montage : `sys/internal/ui/mounts/<montage>` (ce que fait
        la CLI), sinon `<montage>/config` (lisible par la politique de la
        console, propre à KV v2). 0 pour un montage d'un autre type ; un
        montage absent ou KV v1 rend 1 (dans les deux cas : pas KV v2)."""
        mount = self._seg(mount)
        try:
            _, body = self._request("GET", f"sys/internal/ui/mounts/{mount}")
            data = (body or {}).get("data") or {}
            if data.get("type") not in (None, "kv", "generic"):
                return 0
            return int(((data.get("options") or {}).get("version")) or 1)
        except OpenBaoError as e:
            if e.code not in ("openbao-denied", "openbao-missing", "openbao-bad-request"):
                raise
        try:
            _, body = self._call("GET", f"{mount}/config")
        except OpenBaoError as e:
            if e.code in ("openbao-missing", "openbao-bad-request"):
                return 1 if e.code == "openbao-missing" else 0
            raise
        data = (body or {}).get("data") or {}
        return 2 if "max_versions" in data else 1

    def _need_kv2(self, mount):
        if self._kv2.get(mount):
            return
        if self.kv_mount_version(mount) != 2:
            raise OpenBaoError("openbao-not-kv2", None, f"{mount}: not a KV version 2 mount")
        self._kv2[mount] = True

    def kv_get(self, mount, path, version=None):
        mount, path = self._seg(mount), self._seg(path)
        self._need_kv2(mount)
        q = f"?version={int(version)}" if version is not None else ""
        _, body = self._call("GET", f"{mount}/data/{path}{q}")
        d = (body or {}).get("data") or {}
        if d.get("data") is None:
            raise OpenBaoError("openbao-missing", 404, f"GET {mount}/data/{path}: deleted or destroyed")
        return d["data"], d.get("metadata") or {}

    def kv_put(self, mount, path, data, cas=None, custom_metadata=None):
        """Écrit une version ; rend son numéro (`Written`). `custom_metadata`
        ne s'écrit que par le chemin `metadata` (passé dans `data`, OpenBao
        l'ignore : essai 5), en seconde requête."""
        mount, path = self._seg(mount), self._seg(path)
        if not isinstance(data, dict):
            raise TypeError("KV data is a dict")
        self._need_kv2(mount)
        body = {"data": data}
        if cas is not None:
            body["options"] = {"cas": int(cas)}
        _, resp = self._call("POST", f"{mount}/data/{path}", body)
        out = Written(int(((resp or {}).get("data") or {}).get("version") or 0))
        if custom_metadata:
            try:
                self.kv_set_metadata(mount, path, custom_metadata)
            except OpenBaoError as e:
                out.metadata_error = e.code
        return out

    def kv_set_metadata(self, mount, path, custom_metadata):
        """`custom_metadata` d'une clé (chemin `metadata`, seul effectif)."""
        mount, path = self._seg(mount), self._seg(path)
        self._call("POST", f"{mount}/metadata/{path}",
                   {"custom_metadata": {str(k): str(v) for k, v in custom_metadata.items()}})

    def kv_list(self, mount, prefix):
        mount = self._seg(mount)
        prefix = str(prefix).strip("/")
        if prefix:
            prefix = self._seg(prefix)
        self._need_kv2(mount)
        try:
            _, body = self._call("LIST", f"{mount}/metadata/{prefix}".rstrip("/") + "/")
        except OpenBaoError as e:
            if e.code == "openbao-missing":
                return []
            raise
        return list(((body or {}).get("data") or {}).get("keys") or [])

    def kv_metadata(self, mount, path):
        mount, path = self._seg(mount), self._seg(path)
        self._need_kv2(mount)
        _, body = self._call("GET", f"{mount}/metadata/{path}")
        return (body or {}).get("data") or {}

    def kv_delete_all(self, mount, path):
        """`metadata delete` : la feuille et toutes ses versions. KV v2 n'a
        pas de suppression récursive (un « dossier » rend 204 sans rien
        retirer, essai 5) : l'appelant parcourt par `kv_list`."""
        mount, path = self._seg(mount), self._seg(path)
        self._need_kv2(mount)
        try:
            self._call("DELETE", f"{mount}/metadata/{path}")
        except OpenBaoError as e:
            if e.code != "openbao-missing":
                raise

    # -- transit -------------------------------------------------------------
    def transit_create(self, key, mount="transit"):
        self._call("POST", f"{self._seg(mount)}/keys/{self._seg(key)}", {"type": "aes256-gcm96"})

    def transit_rotate(self, key, mount="transit"):
        self._call("POST", f"{self._seg(mount)}/keys/{self._seg(key)}/rotate", {})

    def transit_roundtrip(self, key, mount="transit"):
        """Chiffre puis déchiffre un aléa : vrai si l'aléa revient."""
        mount, key = self._seg(mount), self._seg(key)
        probe = base64.b64encode(secrets.token_bytes(16)).decode()
        _, enc = self._call("POST", f"{mount}/encrypt/{key}", {"plaintext": probe})
        ct = ((enc or {}).get("data") or {}).get("ciphertext")
        if not ct:
            return False
        _, dec = self._call("POST", f"{mount}/decrypt/{key}", {"ciphertext": ct})
        return ((dec or {}).get("data") or {}).get("plaintext") == probe

    # -- jetons enfants --------------------------------------------------------
    def token_create_child(self, role, policies, ttl, entity_alias=None):
        """Jeton enfant par un rôle de jeton (`auth/token/create/<rôle>`).
        Rend le jeton : l'appelant le remet à un seul processus par son
        environnement, jamais en argument ni dans un fichier."""
        body = {"policies": list(policies), "ttl": str(ttl)}
        if entity_alias:
            body["entity_alias"] = entity_alias
        _, resp = self._call("POST", f"auth/token/create/{self._seg(role)}", body, retry=False)
        tok = ((resp or {}).get("auth") or {}).get("client_token")
        if not tok:
            raise OpenBaoError("openbao-bad-request", None, "no token in response")
        return tok

    # -- instantanés raft ------------------------------------------------------
    def raft_snapshot(self, dst_path):
        """Instantané raft dans `dst_path` (0600, écrit à côté puis renommé)."""
        dst = str(dst_path)
        tmp = f"{dst}.part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                _, n = self._call("GET", "sys/storage/raft/snapshot", stream_to=f)
            os.replace(tmp, dst)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return n

    def raft_restore(self, src_path, force=False):
        with open(src_path, "rb") as f:
            data = f.read()
        path = "sys/storage/raft/snapshot-force" if force else "sys/storage/raft/snapshot"
        self._call("POST", path, raw_body=data, retry=False)


def _json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
