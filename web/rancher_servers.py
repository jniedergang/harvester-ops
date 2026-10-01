"""harvester-ops : les serveurs Rancher réglés dans l'interface (v1.79.0).

Jusqu'à la 1.78, un seul Rancher, écrit à la main dans `config.yaml`
(section `rancher:`). Les Rancher se règlent maintenant dans la console, à
chaud, plusieurs à la fois. Ils vivent dans le répertoire d'état, comme les
clusters déclarés par la console (1.78.0) :

    <état>/rancher.d/<id>.yaml          un fichier par Rancher (0600)
    <état>/rancher.d/<id>.ca.pem        son autorité de certification (0600)
    <état>/rancher.d/<id>.oidc-secret   le secret du client OIDC (0600)

Les chemins rangés dans ces fichiers sont RELATIFS au répertoire d'état :
copier l'état ailleurs suffit à déplacer la console.

La section `rancher:` de `config.yaml` reste lue, en lecture seule (origine
« config ») ; elle l'emporte sur un réglage de la console qui vise la même
adresse.

Module pur (bibliothèque standard + PyYAML) : aucune requête réseau ici.
Voir docs/design/2026-10-01-rancher-reglable.md.
"""

import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml

STORE_DIR = "rancher.d"
ROLES = ("viewer", "operator", "admin")
DEFAULT_ROLE = "operator"
DEFAULT_SESSION_HOURS = 12
# fournisseurs de Rancher qui acceptent un identifiant et un mot de passe
PASSWORD_PROVIDERS = ("local", "ldap", "openldap", "activedirectory", "freeipa")
PROVIDER_LABELS = {"local": "Local", "ldap": "LDAP", "openldap": "OpenLDAP",
                   "activedirectory": "Active Directory", "freeipa": "FreeIPA"}
ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
LABEL_MAX = 60


class ServerError(ValueError):
    """Un réglage refusé ; `status` est le code HTTP à rendre."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Petits outils
# ---------------------------------------------------------------------------

def slug(text, fallback="rancher"):
    """Une étiquette RFC 1123 tirée d'un libellé ou d'un nom d'hôte."""
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    s = s[:63].strip("-")
    return s or fallback


def norm_url(url):
    """L'adresse comparable : schéma et hôte en minuscules, sans / final."""
    u = str(url or "").strip().rstrip("/")
    p = urlparse(u)
    if not p.scheme or not p.netloc:
        return u.lower()
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path}"


def check_url(url):
    u = str(url or "").strip().rstrip("/")
    p = urlparse(u)
    if p.scheme != "https" or not p.hostname:
        raise ServerError("the Rancher address must be an https:// URL")
    if p.username or p.password or p.query or p.fragment:
        raise ServerError("the Rancher address must not carry credentials, a query or a fragment")
    return u


def check_ca(pem):
    pem = str(pem or "").strip()
    if "PRIVATE KEY" in pem:
        raise ServerError("this is a private key; give the certificate authority (public) only")
    if "-----BEGIN CERTIFICATE-----" not in pem or "-----END CERTIFICATE-----" not in pem:
        raise ServerError("the certificate authority must be a PEM certificate")
    if len(pem) > 64 * 1024:
        raise ServerError("the certificate authority is too large")
    return pem + "\n"


def write_private(path, text):
    """Écrit en 0600 et remplace d'un coup."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def provider_label(pid):
    return PROVIDER_LABELS.get(pid, pid)


def password_provider(item):
    """Ce fournisseur (élément de /v3-public/authProviders) prend-il un mot
    de passe ? Son id le dit (`local`, `openldap`...), son type aussi."""
    pid = str(item.get("id") or "").lower()
    typ = str(item.get("type") or "").lower()
    if typ.endswith("provider"):
        typ = typ[:-len("provider")]
    return pid in PASSWORD_PROVIDERS or typ in PASSWORD_PROVIDERS


def providers_of(auth_providers):
    """Les fournisseurs publiés par /v3-public/authProviders, réduits à ce
    que la page de connexion et le test montrent."""
    out = []
    for item in (auth_providers or {}).get("data") or []:
        pid = item.get("id")
        if not pid:
            continue
        out.append({"id": pid, "type": item.get("type") or "",
                    # la liste publique ne montre que les fournisseurs actifs
                    "enabled": item.get("enabled") is not False,
                    "password": password_provider(item)})
    return out


def login_path(provider):
    """Le point de connexion d'un fournisseur à mot de passe. Construit sur
    l'adresse réglée, jamais pris dans les liens que Rancher renvoie (son
    adresse publique peut différer de celle que la console joint)."""
    p = provider if isinstance(provider, dict) else {"id": provider}
    pid = str(p.get("id") or "local")
    typ = str(p.get("type") or "")
    if not typ:
        typ = {"local": "localProvider", "openldap": "openLdapProvider",
               "activedirectory": "activeDirectoryProvider", "freeipa": "freeIpaProvider",
               "ldap": "ldapProvider"}.get(pid, pid + "Provider")
    if not re.match(r"^[A-Za-z0-9]+$", typ) or not re.match(r"^[a-z0-9-]+$", pid):
        raise ServerError("unknown authentication provider")
    return f"/v3-public/{typ}s/{pid}?action=login"


# ---------------------------------------------------------------------------
# Versions et contraintes de chart (« >= 2.14.0-0 < 2.15.0-0 »)
# ---------------------------------------------------------------------------

def version_tuple(v):
    m = re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(v or ""))
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def satisfies(version, constraint):
    """La version respecte-t-elle la contrainte d'un chart ? Les suffixes
    (`-0`, `-rc1`, `+rke2r1`) sont ignorés : la contrainte `-0` des charts
    de Rancher sert justement à accepter les préversions. Alternatives
    séparées par `||`. None si l'on ne sait pas lire la version."""
    vt = version_tuple(version)
    if vt is None:
        return None
    constraint = str(constraint or "").strip()
    if not constraint:
        return True
    for alt in constraint.split("||"):
        ok = True
        for op, ver in re.findall(r"(>=|<=|!=|>|<|=)?\s*v?(\d+(?:\.\d+){0,2}(?:-[0-9A-Za-z.]+)?)", alt.replace(",", " ")):
            ct = version_tuple(ver)
            op = op or "="
            if ct is None:
                continue
            ok = ok and {">=": vt >= ct, "<=": vt <= ct, ">": vt > ct, "<": vt < ct,
                         "=": vt == ct, "!=": vt != ct}[op]
        if ok:
            return True
    return False


# ---------------------------------------------------------------------------
# Le magasin
# ---------------------------------------------------------------------------

class Store:
    """Les Rancher réglés : ceux de la console (`<état>/rancher.d`) et celui
    de `config.yaml`. Relu à chaque appel (cache invalidé par les dates de
    modification) : un réglage vaut sans redémarrage."""

    def __init__(self, state_fn, now=time.time):
        self.state_fn = state_fn
        self.now = now
        self._cache = (None, [])

    # -- chemins -----------------------------------------------------------
    @property
    def state(self):
        return Path(self.state_fn())

    @property
    def dir(self):
        return self.state / STORE_DIR

    def _ensure_dir(self):
        d = self.dir
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d

    def _abs(self, p):
        if not p:
            return None
        return p if os.path.isabs(p) else str(self.state / p)

    def _file(self, sid):
        if not isinstance(sid, str) or not ID_RE.match(sid):
            return None
        return self.dir / f"{sid}.yaml"

    # -- lecture -----------------------------------------------------------
    def _console_raw(self):
        d = self.dir
        try:
            files = sorted(p for p in d.glob("*.yaml") if ID_RE.match(p.stem))
        except OSError:
            files = []
        sig = []
        for p in files:
            try:
                sig.append((p.name, p.stat().st_mtime_ns))
            except OSError:
                pass
        sig = (str(d), tuple(sig))
        if self._cache[0] == sig:
            return self._cache[1]
        out = []
        for p in files:
            try:
                raw = yaml.safe_load(p.read_text()) or {}
            except (OSError, yaml.YAMLError, UnicodeDecodeError):
                continue
            if isinstance(raw, dict):
                raw["id"] = p.stem
                out.append(raw)
        self._cache = (sig, out)
        return out

    def _from_console(self, raw):
        sso = raw.get("sso") if isinstance(raw.get("sso"), dict) else {}
        secret_file = self._abs(sso.get("secret_file"))
        role = raw.get("default_role")
        return {
            "id": raw["id"], "label": str(raw.get("label") or raw["id"]),
            "url": str(raw.get("url") or "").rstrip("/"), "origin": "console", "editable": True,
            "insecure": bool(raw.get("insecure")),
            "ca_file": self._abs(raw.get("ca_file")),
            "default_role": role if role in ROLES else DEFAULT_ROLE,
            "session_hours": _hours(raw.get("session_hours")),
            "direct_enabled": raw.get("direct_enabled") is not False,
            "admin_groups": [str(g) for g in (raw.get("admin_groups") or [])],
            "sso": {"client_id": str(sso.get("client_id") or ""), "secret_file": secret_file,
                    "registered_at": sso.get("registered_at"), "redirect_uri": sso.get("redirect_uri"),
                    "oidc_client": sso.get("oidc_client")},
        }

    @staticmethod
    def _from_config(cfg):
        r = (cfg or {}).get("rancher")
        if not isinstance(r, dict) or not r.get("url"):
            return None
        url = str(r.get("url")).rstrip("/")
        role = r.get("default_role")
        return {
            "id": slug(urlparse(url).hostname or url), "label": str(r.get("label") or "Rancher"),
            "url": url, "origin": "config", "editable": False,
            "insecure": bool(r.get("insecure")),
            "ca_file": r.get("ca_file") or None,
            "default_role": role if role in ROLES else DEFAULT_ROLE,
            "session_hours": _hours(r.get("session_hours")),
            # la 1.50 n'avait que le SSO : la connexion directe se demande
            "direct_enabled": bool(r.get("direct_login", False)),
            "admin_groups": [str(g) for g in (r.get("admin_groups") or [])],
            "sso": {"client_id": str(r.get("client_id") or ""),
                    "secret_file": r.get("client_secret_file") or None,
                    "registered_at": None, "redirect_uri": r.get("redirect_uri") or None,
                    "oidc_client": None},
        }

    def servers(self, cfg, include_shadowed=False):
        """Tous les Rancher, celui de config.yaml d'abord. Un réglage de la
        console qui vise la même adresse (ou porte le même id) est masqué ;
        `include_shadowed` le rend quand même, marqué, pour qu'on puisse le
        supprimer."""
        out = []
        conf = self._from_config(cfg)
        if conf is not None:
            conf["shadowed"] = False
            out.append(conf)
        for raw in self._console_raw():
            s = self._from_console(raw)
            s["shadowed"] = bool(conf and (norm_url(conf["url"]) == norm_url(s["url"]) or conf["id"] == s["id"]))
            if s["shadowed"] and not include_shadowed:
                continue
            out.append(s)
        return out

    def get(self, cfg, sid, include_shadowed=False):
        for s in self.servers(cfg, include_shadowed=include_shadowed):
            if s["id"] == sid:
                return s
        return None

    # -- écriture ----------------------------------------------------------
    def _write(self, sid, raw):
        self._ensure_dir()
        doc = {k: v for k, v in raw.items() if k != "id"}
        write_private(self._file(sid), yaml.safe_dump(doc, sort_keys=False))
        self._cache = (None, [])

    def _raw(self, sid):
        f = self._file(sid)
        if f is None or not f.exists():
            return None
        try:
            raw = yaml.safe_load(f.read_text()) or {}
        except (OSError, yaml.YAMLError):
            return None
        return raw if isinstance(raw, dict) else None

    def _apply(self, sid, raw, data, creating):
        if creating or "label" in data:
            label = str(data.get("label") or "").strip()
            if not label or len(label) > LABEL_MAX:
                raise ServerError(f"a label of 1 to {LABEL_MAX} characters is required")
            raw["label"] = label
        if creating or "url" in data:
            raw["url"] = check_url(data.get("url"))
        if "insecure" in data:
            raw["insecure"] = bool(data.get("insecure"))
        if "default_role" in data:
            if data["default_role"] not in ROLES:
                raise ServerError("default_role must be viewer, operator or admin")
            raw["default_role"] = data["default_role"]
        if "session_hours" in data:
            try:
                h = int(data["session_hours"])
            except (TypeError, ValueError):
                h = 0
            if not 1 <= h <= 24:
                raise ServerError("session_hours must be between 1 and 24")
            raw["session_hours"] = h
        if "direct_enabled" in data:
            raw["direct_enabled"] = bool(data.get("direct_enabled"))
        if "admin_groups" in data:
            groups = data.get("admin_groups") or []
            if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
                raise ServerError("admin_groups must be a list of Rancher group principals")
            raw["admin_groups"] = [g.strip() for g in groups if g.strip()][:50]
        if "ca" in data:
            ca = data.get("ca")
            ca_path = self.dir / f"{sid}.ca.pem"
            if ca:
                pem = check_ca(ca)
                self._ensure_dir()
                write_private(ca_path, pem)
                raw["ca_file"] = f"{STORE_DIR}/{sid}.ca.pem"
            else:
                raw.pop("ca_file", None)
                ca_path.unlink(missing_ok=True)
        raw.setdefault("insecure", False)
        raw.setdefault("default_role", DEFAULT_ROLE)
        raw.setdefault("session_hours", DEFAULT_SESSION_HOURS)
        raw.setdefault("direct_enabled", True)
        return raw

    def create(self, cfg, data):
        if not isinstance(data, dict):
            raise ServerError("a JSON object is expected")
        url = check_url(data.get("url"))
        taken = {s["id"] for s in self.servers(cfg, include_shadowed=True)}
        for s in self.servers(cfg, include_shadowed=True):
            if norm_url(s["url"]) == norm_url(url):
                raise ServerError(f"this Rancher is already set ({s['id']})", 409)
        base = slug(data.get("label") or urlparse(url).hostname)
        sid, n = base, 2
        while sid in taken:
            sid = f"{base[:60]}-{n}"
            n += 1
        raw = self._apply(sid, {}, data, creating=True)
        self._write(sid, raw)
        return self.get(cfg, sid, include_shadowed=True)

    def update(self, cfg, sid, data):
        if not isinstance(data, dict):
            raise ServerError("a JSON object is expected")
        cur = self.get(cfg, sid, include_shadowed=True)
        if cur is None:
            raise ServerError("unknown Rancher", 404)
        if cur["origin"] == "config":
            raise ServerError("declared by the operator in config.yaml, read-only for the console", 409)
        raw = self._raw(sid) or {}
        if "url" in data:
            url = check_url(data.get("url"))
            for s in self.servers(cfg, include_shadowed=True):
                if s["id"] != sid and norm_url(s["url"]) == norm_url(url):
                    raise ServerError(f"this Rancher is already set ({s['id']})", 409)
        raw = self._apply(sid, raw, data, creating=False)
        self._write(sid, raw)
        return self.get(cfg, sid, include_shadowed=True)

    def delete(self, cfg, sid):
        cur = self.get(cfg, sid, include_shadowed=True)
        if cur is None:
            raise ServerError("unknown Rancher", 404)
        if cur["origin"] == "config":
            raise ServerError("declared by the operator in config.yaml, read-only for the console", 409)
        for p in (self._file(sid), self.dir / f"{sid}.ca.pem", self.dir / f"{sid}.oidc-secret"):
            Path(p).unlink(missing_ok=True)
        self._cache = (None, [])
        return cur

    def set_sso(self, cfg, sid, client_id, client_secret, redirect_uri, oidc_client):
        cur = self.get(cfg, sid, include_shadowed=True)
        if cur is None or cur["origin"] != "console":
            raise ServerError("unknown Rancher", 404)
        raw = self._raw(sid) or {}
        self._ensure_dir()
        write_private(self.dir / f"{sid}.oidc-secret", client_secret.strip() + "\n")
        raw["sso"] = {"client_id": client_id, "secret_file": f"{STORE_DIR}/{sid}.oidc-secret",
                      "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now())),
                      "redirect_uri": redirect_uri, "oidc_client": oidc_client}
        self._write(sid, raw)
        return self.get(cfg, sid, include_shadowed=True)

    def clear_sso(self, cfg, sid):
        cur = self.get(cfg, sid, include_shadowed=True)
        if cur is None or cur["origin"] != "console":
            raise ServerError("unknown Rancher", 404)
        raw = self._raw(sid) or {}
        raw.pop("sso", None)
        (self.dir / f"{sid}.oidc-secret").unlink(missing_ok=True)
        self._write(sid, raw)
        return self.get(cfg, sid, include_shadowed=True)


def _hours(v):
    try:
        h = int(float(v))
    except (TypeError, ValueError):
        return DEFAULT_SESSION_HOURS
    return min(24, max(1, h))


def read_secret(path):
    if not path:
        return None
    try:
        s = Path(path).read_text().strip()
    except OSError:
        return None
    return s or None


def sso_enabled(server):
    sso = server.get("sso") or {}
    return bool(sso.get("client_id") and read_secret(sso.get("secret_file")))


def public(server):
    """Ce que l'interface reçoit : jamais de secret, ni de chemin."""
    sso = server.get("sso") or {}
    return {
        "id": server["id"], "label": server["label"], "url": server["url"],
        "origin": server["origin"], "editable": server["editable"],
        "shadowed": bool(server.get("shadowed")),
        "insecure": server["insecure"], "has_ca": bool(server.get("ca_file")),
        "default_role": server["default_role"], "session_hours": server["session_hours"],
        "direct_enabled": server["direct_enabled"],
        "admin_groups": list(server.get("admin_groups") or []),
        "sso": {"enabled": sso_enabled(server), "client_id": sso.get("client_id") or None,
                "registered_at": sso.get("registered_at")},
    }


def settings_of(server):
    """Les réglages au format de rancher_sso (url, issuer, client...), pour
    les sessions SSO comme directes. `client_secret` vaut None quand le SSO
    n'est pas enregistré."""
    sso = server.get("sso") or {}
    secret = read_secret(sso.get("secret_file"))
    return {
        "id": server["id"], "origin": server["origin"],
        "url": server["url"], "issuer": f"{server['url']}/oidc",
        "client_id": sso.get("client_id") or "", "client_secret": secret,
        "sso": bool(sso.get("client_id") and secret),
        "direct": bool(server["direct_enabled"]),
        "ca_file": server.get("ca_file") or None, "insecure": bool(server.get("insecure")),
        "redirect_uri": sso.get("redirect_uri") or None,
        "default_role": server["default_role"],
        "admin_groups": list(server.get("admin_groups") or []),
        "session_seconds": int(server["session_hours"]) * 3600,
        "label": server["label"],
    }
