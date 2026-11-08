"""harvester-ops : OIDC de l'API d'un cluster enfant (2.8.0, décision 125).

Quand le cluster enfant n'est pas importé dans un Rancher, les personnes
s'y connectent par le fournisseur d'identité du client : l'API RKE2 reçoit
une configuration d'authentification structurée (point 228,
`AuthenticationConfiguration`, un seul fournisseur), posée à la création
par le modèle de cluster dans le SEUL gabarit du plan de contrôle :

- fichier `/etc/rancher/rke2/hops-auth/authentication-config.yaml`
  (`root:root`, `0600`) ;
- `serverConfig.kubeAPIServer.extraArgs` +
  `authentication-config=<fichier>`.

Vu sur rke2-banc (RKE2 v1.34.11, 07/11/2026, plan 2.8.0, « Essais
préalables (tâche 1) », point 5) : RKE2 monte lui-même en lecture seule le
fichier cité par l'argument ; `extraMounts` n'ajoutait qu'un second montage
en écriture, il n'est pas posé. Jamais les drapeaux `--oidc-*`, exclusifs de
`--authentication-config`.

Aucun secret : l'API n'a besoin que de l'audience. Le kubeconfig remis aux
personnes (point 230) passe par le greffon kubelogin (`kubectl oidc-login`)
et ne porte aucun identifiant.

Bibliothèque standard seulement ; ce module ne parle à aucun cluster.
"""

import json
import re
import ssl
import urllib.parse

DIR = "/etc/rancher/rke2/hops-auth"
FILE = f"{DIR}/authentication-config.yaml"
ARG = "authentication-config"
KEYS = ("issuer_url", "audience", "username_claim", "username_prefix", "groups_claim", "groups_prefix",
        "ca_pem", "admin_group")
DEFAULTS = {"username_claim": "email", "username_prefix": "oidc:", "groups_claim": "groups",
            "groups_prefix": "oidc:"}
ISSUER_MAX, AUDIENCE_MAX, CLAIM_MAX, PREFIX_MAX, GROUP_MAX = 2048, 256, 128, 64, 128
CA_MAX = 64 * 1024
CLAIM_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
VERSION_RE = re.compile(r"^v?1\.(\d{1,3})(\.\d{1,4})?")
MIN_MINOR, V1_MINOR = 30, 34
FORBIDDEN_ARGS = ("oidc-",)
USER = "oidc"
# Réglages publics (sans CA) posés sur le `Cluster` à la création : le
# kubeconfig des personnes et le groupe administrateur en sont rendus.
ANNOTATION = "harvester-ops.io/oidc"
PUBLIC_KEYS = ("issuer_url", "audience", "username_prefix", "groups_prefix", "admin_group")


def finding(code, level="block", **facts):
    return {"code": code, "level": level, "facts": facts}


def _minor(version):
    m = VERSION_RE.match(str(version or ""))
    return int(m.group(1)) if m else None


def api_version(k8s_version):
    """`apiserver.config.k8s.io/v1` dès 1.34, `v1beta1` de 1.30 à 1.33 ;
    None avant 1.30 ou version illisible."""
    minor = _minor(k8s_version)
    if minor is None or minor < MIN_MINOR:
        return None
    return "apiserver.config.k8s.io/" + ("v1" if minor >= V1_MINOR else "v1beta1")


def _issuer_ok(url):
    if not isinstance(url, str) or not url or len(url) > ISSUER_MAX or any(c.isspace() for c in url):
        return False
    p = urllib.parse.urlparse(url)
    return p.scheme == "https" and bool(p.hostname) and not p.username and not p.password \
        and not p.query and not p.fragment


def _ca_ok(pem):
    if len(pem) > CA_MAX or "-----BEGIN CERTIFICATE-----" not in pem:
        return False
    try:
        ssl.create_default_context(cadata=pem)
    except (ssl.SSLError, ValueError):
        return False
    return True


def check(cfg, k8s_version, rancher=False):
    """(réglages normalisés, constats). `cfg` : dictionnaire de l'option
    `oidc` du cluster enfant. `rancher` : import externe demandé aussi
    (avertissement `oidc-with-rancher`, point 231)."""
    found = []
    if not isinstance(cfg, dict):
        return {}, [finding("oidc-invalid", reason="an object")]
    for k in sorted(set(cfg) - set(KEYS)):
        found.append(finding("oidc-invalid", reason=f"unknown key {k}"))
    out = dict(DEFAULTS)
    out.update({k: cfg[k] for k in KEYS if cfg.get(k) not in (None, "")})
    for k in ("username_prefix", "groups_prefix"):
        if cfg.get(k) == "":
            out[k] = ""                         # vide demandé : refusé plus bas, jamais remplacé
    if not _issuer_ok(out.get("issuer_url")):
        found.append(finding("oidc-issuer-invalid", reason="https://, no credentials nor query, "
                                                           f"{ISSUER_MAX} characters at most"))
    aud = out.get("audience")
    if not isinstance(aud, str) or not 1 <= len(aud) <= AUDIENCE_MAX or any(c.isspace() for c in aud):
        found.append(finding("oidc-audience-invalid", max=AUDIENCE_MAX))
    for k in ("username_claim", "groups_claim"):
        if not isinstance(out.get(k), str) or not CLAIM_RE.match(out[k]):
            found.append(finding("oidc-claim-invalid", option=k))
    for k in ("username_prefix", "groups_prefix"):
        v = out.get(k)
        if not isinstance(v, str) or not v.strip():
            found.append(finding("oidc-prefix-required", option=k))
        elif v.startswith("system:"):
            found.append(finding("oidc-prefix-system", option=k))
        elif len(v) > PREFIX_MAX or any(c.isspace() for c in v):
            found.append(finding("oidc-claim-invalid", option=k))
    ca = out.get("ca_pem")
    if ca is not None and (not isinstance(ca, str) or not _ca_ok(ca)):
        found.append(finding("oidc-ca-unreadable", max=CA_MAX))
    group = out.get("admin_group")
    if group is not None and (not isinstance(group, str) or not group.strip() or len(group) > GROUP_MAX
                              or "\n" in group or "\r" in group):
        found.append(finding("oidc-admin-group-invalid", max=GROUP_MAX))
    if api_version(k8s_version) is None:
        found.append(finding("oidc-version-too-old", version=str(k8s_version or ""), minimum=f"v1.{MIN_MINOR}"))
    if rancher:
        found.append(finding("oidc-with-rancher", "warn"))
    return out, found


def blocking(found):
    return [f for f in found if f["level"] == "block"]


def auth_config(cfg, k8s_version):
    """L'`AuthenticationConfiguration` (dictionnaire) de réglages normalisés."""
    issuer = {"url": cfg["issuer_url"], "audiences": [cfg["audience"]]}
    if cfg.get("ca_pem"):
        issuer["certificateAuthority"] = cfg["ca_pem"]
    return {"apiVersion": api_version(k8s_version), "kind": "AuthenticationConfiguration",
            "jwt": [{"issuer": issuer,
                     "claimMappings": {"username": {"claim": cfg["username_claim"], "prefix": cfg["username_prefix"]},
                                       "groups": {"claim": cfg["groups_claim"], "prefix": cfg["groups_prefix"]}}}]}


def template_patch(cfg, k8s_version):
    """{"files": [...], "extraArgs": [...]} à ajouter au gabarit du plan de
    contrôle. Le fichier est du JSON, donc du YAML valide, lu tel quel par
    l'API."""
    content = json.dumps(auth_config(cfg, k8s_version), indent=2, sort_keys=True) + "\n"
    return {"files": [{"path": FILE, "owner": "root:root", "permissions": "0600", "content": content}],
            "extraArgs": [f"{ARG}={FILE}"]}


def apply_patch(tpl_spec, patch):
    """Pose `patch` dans `spec.template.spec` d'un `RKE2ControlPlaneTemplate`
    (modifié sur place). ValueError `oidc-arg-conflict` si un argument
    `authentication-config` ou `oidc-*`, ou un fichier au même chemin, y est
    déjà : rien n'est écrasé."""
    api = tpl_spec.setdefault("serverConfig", {}).setdefault("kubeAPIServer", {})
    args = list(api.get("extraArgs") or [])
    for a in args:
        key = str(a).lstrip("-").split("=", 1)[0]
        if key == ARG or key.startswith(FORBIDDEN_ARGS):
            raise ValueError(f"oidc-arg-conflict: the control plane template already passes {key}")
    files = list(tpl_spec.get("files") or [])
    if any(f.get("path") == FILE for f in files):
        raise ValueError(f"oidc-arg-conflict: the control plane template already writes {FILE}")
    api["extraArgs"] = args + list(patch["extraArgs"])
    tpl_spec["files"] = files + [dict(f) for f in patch["files"]]
    return tpl_spec


def public(cfg):
    """Valeur de l'annotation : réglages publics, JSON trié."""
    return json.dumps({k: cfg[k] for k in PUBLIC_KEYS if cfg.get(k)}, sort_keys=True, separators=(",", ":"))


def of_cluster(cluster_obj):
    """Inverse de `public`, lu sur un `Cluster` ; None sans OIDC."""
    raw = (((cluster_obj or {}).get("metadata") or {}).get("annotations") or {}).get(ANNOTATION)
    try:
        d = json.loads(raw) if raw else None
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("issuer_url") and d.get("audience") else None


def of_template(cp_template, cluster_obj=None):
    """Réglages complets lus dans le fichier `FILE` d'un
    `RKE2ControlPlaneTemplate` (la source de vérité : revendications,
    préfixes, CA de l'émetteur), le groupe administrateur dans l'annotation
    du `Cluster` ; None sans OIDC (revue 5-8 I-3 : l'annotation publique
    seule perdait revendications et CA)."""
    inner = ((((cp_template or {}).get("spec") or {}).get("template") or {}).get("spec") or {})
    f = next((x for x in inner.get("files") or [] if x.get("path") == FILE), None)
    if f is None or not isinstance(f.get("content"), str):
        return None
    try:
        doc = json.loads(f["content"])
        jwt = doc["jwt"][0]
        issuer, maps = jwt["issuer"], jwt.get("claimMappings") or {}
        cfg = {"issuer_url": issuer["url"], "audience": (issuer.get("audiences") or [None])[0],
               "username_claim": (maps.get("username") or {}).get("claim"),
               "username_prefix": (maps.get("username") or {}).get("prefix"),
               "groups_claim": (maps.get("groups") or {}).get("claim"),
               "groups_prefix": (maps.get("groups") or {}).get("prefix"),
               "ca_pem": issuer.get("certificateAuthority")}
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
    group = (of_cluster(cluster_obj) or {}).get("admin_group")
    if group:
        cfg["admin_group"] = group
    return {k: v for k, v in cfg.items() if v is not None}


def kubeconfig(cfg, server, ca_b64, cluster):
    """Kubeconfig remis aux personnes (point 230) : serveur et CA du cluster
    enfant, utilisateur `oidc` en greffon `exec` kubelogin. Aucun identifiant :
    il ne donne rien sans le fournisseur d'identité."""
    name = str(cluster)
    cl = {"server": server}
    if ca_b64:
        cl["certificate-authority-data"] = ca_b64
    doc = {"apiVersion": "v1", "kind": "Config", "current-context": name,
           "clusters": [{"name": name, "cluster": cl}],
           "contexts": [{"name": name, "context": {"cluster": name, "user": USER}}],
           "users": [{"name": USER, "user": {"exec": {
               "apiVersion": "client.authentication.k8s.io/v1", "command": "kubectl",
               "args": ["oidc-login", "get-token", f"--oidc-issuer-url={cfg['issuer_url']}",
                        f"--oidc-client-id={cfg['audience']}"],
               "interactiveMode": "IfAvailable", "provideClusterInfo": False}}}]}
    return json.dumps(doc, indent=2) + "\n"


def server_of(kubeconfig_text):
    """(serveur, CA en base64) du kubeconfig Cluster API d'un cluster enfant."""
    try:
        import yaml
        kc = yaml.safe_load(kubeconfig_text) or {}
    except Exception:  # noqa: BLE001
        kc = json.loads(kubeconfig_text)
    cl = ((kc.get("clusters") or [{}])[0].get("cluster")) or {}
    return cl.get("server") or "", cl.get("certificate-authority-data") or ""
