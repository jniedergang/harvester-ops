"""harvester-ops : External Secrets Operator dans un cluster enfant (2.10.0).

Décision 139 (Secrets Kubernetes écrits par la console, ESO en option) et
points 293 à 307 du plan `docs/design/2026-11-08-jeton-personne-2100-plan.md`
(« External Secrets Operator », essai 7 de la tâche 1) :

- portée : clusters enfants RKE2 créés par la console, OpenBao actif,
  administrateur ; jamais un cluster Harvester (point 293) ;
- livraison : chart officiel `external-secrets` d'un paquet d'artefacts,
  installé par CAAPH sous le nom de service `eso` dans le namespace
  `external-secrets` (modèle de `flux2`, point 294) ; valeurs imposées par
  `VALUES` (noms relevés dans le chart 2.14.0, essai 7) ;
- magasin (points 296 et 305) : un `SecretStore` `hops-openbao` et un compte
  `hops-eso` sans droit dans chaque namespace activé, livrés par le
  ClusterResourceSet `art-eso` ; aucun magasin de portée cluster ;
- `ExternalSecret` : ceux des équipes (point 299) ; la console montre leur
  état sans aucune valeur ;
- retrait (point 300) : `ownerReferences` des Secrets synchronisés retirées
  d'abord (Secrets gardés à leur dernière valeur), `ExternalSecret`
  supprimés tant qu'ESO tourne, finalizers retirés en repli.

Bibliothèque standard seulement ; ce module ne parle à aucun cluster.
"""

import base64
import copy
import hashlib
import json
import re

import capi_manifests as mf

SERVICE = "eso"                       # nom du service CAAPH (élément `eso` d'un paquet)
NAMESPACE = "external-secrets"        # namespace de la release
ITEM = "eso"                          # ClusterResourceSet `art-eso`
PACKAGE = "harvester-ops"             # paquet des objets rendus par la console
PACKAGE_VERSION = "eso"
STORE = "hops-openbao"
ACCOUNT = "hops-eso"
ROLE = "eso"
API = "external-secrets.io/v1"
GROUP = "external-secrets.io"
NS_LABEL = "harvester-ops.io/eso"     # "on" sur un namespace activé
TOKEN_SECONDS = 600                   # défaut d'ESO (10 min), essai 7
FINALIZER = "externalsecrets.external-secrets.io/externalsecret-cleanup"
EXAMPLE_REFRESH = "1h"                # décision 307 : une connexion par passage
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
# nom d'un secret ESO et de ses champs (chemin `eso/<clé>/<ns>/<nom>`)
SECRET_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,61}[a-z0-9])?$")
FIELD_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
FIELDS_MAX = 32
NAMESPACES_MAX = 50

# Valeurs imposées au chart (noms relevés dans le chart 2.14.0, essai 7) :
# CRD par le chart (jamais par un ClusterResourceSet), webhook et contrôleur
# de certificats, aucun réconciliateur de portée cluster ni de PushSecret
# (points 296 et 305), pas de sous-chart bitwarden, pas de ServiceMonitor,
# pas de cache de jetons (point 307 : un jeton ne survit pas à son usage).
VALUES = {"installCRDs": True,
          "webhook": {"create": True},
          "certController": {"create": True},
          "processClusterStore": False,
          "processClusterExternalSecret": False,
          "processPushSecret": False,
          "processClusterPushSecret": False,
          "bitwarden-sdk-server": {"enabled": False},
          "serviceMonitor": {"enabled": False},
          "vault": {"enableTokenCache": False}}


class EsoError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def finding(code, level="block", **facts):
    return {"code": code, "level": level, "facts": facts}


def blocking(found):
    return [f for f in found if f["level"] == "block"]


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------
def chart_values(user_values=None):
    """Valeurs du chart : celles du paquet, puis `VALUES` imposées par-dessus
    (une valeur du paquet ne rallume ni un magasin de portée cluster, ni les
    PushSecret, ni le cache de jetons)."""
    out = json.loads(json.dumps(user_values or {}))
    for k, v in VALUES.items():
        if isinstance(v, dict):
            out[k] = dict(out[k] if isinstance(out.get(k), dict) else {}, **v)
        else:
            out[k] = v
    return out


def chart_locked(values):
    """Le service posé porte-t-il toutes les valeurs imposées ?"""
    values = values or {}
    for k, v in VALUES.items():
        if isinstance(v, dict):
            have = values.get(k)
            if not isinstance(have, dict) or any(have.get(sk) is not sv for sk, sv in v.items()):
                return False
        elif values.get(k) is not v:
            return False
    return True


# ---------------------------------------------------------------------------
# Noms
# ---------------------------------------------------------------------------
def cluster_key(harvester, ns, cluster):
    """Clé d'un cluster enfant : 8 hexadécimaux de sha256 de
    `<harvester>/<ns>/<c>` (essai 7 : `harvlab2/c210/c210` -> `1d905023`).
    Elle nomme le montage `jwt`, la politique et l'espace des valeurs."""
    for what, v in (("harvester", harvester), ("namespace", ns), ("cluster", cluster)):
        if not isinstance(v, str) or not v or "/" in v or len(v) > 253:
            raise ValueError(f"{what}: a name")
    return hashlib.sha256(f"{harvester}/{ns}/{cluster}".encode()).hexdigest()[:8]


def check_namespace(ns):
    """Constats d'un namespace à activer : nom RFC 1123, jamais un namespace
    système ni celui d'ESO (`eso-system-namespace`)."""
    if not isinstance(ns, str) or not NAME_RE.match(ns):
        return [finding("eso-namespace-invalid", namespace=str(ns)[:80])]
    if mf.is_system_namespace(ns) or ns == NAMESPACE:
        return [finding("eso-system-namespace", namespace=ns)]
    return []


def normalize_namespaces(raw):
    """Liste triée et sans doublon ; ValueError qui nomme le défaut."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > NAMESPACES_MAX:
        raise ValueError(f"namespaces: a list of {NAMESPACES_MAX} namespaces at most")
    out = sorted(set(raw))
    for ns in out:
        bad = check_namespace(ns)
        if bad:
            raise EsoError(bad[0]["code"], f"namespace {str(ns)[:80]}: "
                           + ("a system namespace" if bad[0]["code"] == "eso-system-namespace" else "invalid name"))
    return out


def value_path(key, namespace, name):
    """Chemin de la classe `eso` : `<clé>/<namespace>/<nom>` (sous le préfixe
    de la console : `<préfixe>/eso/<clé>/<namespace>/<nom>`)."""
    if not re.match(r"^[0-9a-f]{8}$", str(key or "")):
        raise ValueError("ESO cluster key: 8 hexadecimals")
    if check_namespace(namespace):
        raise ValueError("namespace: a team namespace")
    if not isinstance(name, str) or not SECRET_NAME_RE.match(name):
        raise ValueError("name: RFC 1123 name")
    return f"{key}/{namespace}/{name}"


def check_fields(fields):
    """{champ: valeur} d'une écriture : 1 à 32 champs, noms sûrs, valeurs
    chaînes non vides. Rend une copie ; ValueError sans aucune valeur."""
    if not isinstance(fields, dict) or not 1 <= len(fields) <= FIELDS_MAX:
        raise ValueError(f"fields: 1 to {FIELDS_MAX} fields")
    out = {}
    for k, v in fields.items():
        if not isinstance(k, str) or not FIELD_RE.match(k):
            raise ValueError("fields: names of letters, digits, '.', '_' or '-'")
        if not isinstance(v, str) or not v:
            raise ValueError(f"fields.{k}: a non-empty text")
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# Objets du magasin (ClusterResourceSet `art-eso`)
# ---------------------------------------------------------------------------
def namespace_objects(namespace, addr, kv_mount, jwt_mount, audience, ca_pem):
    """[ServiceAccount `hops-eso`, SecretStore `hops-openbao`] d'un namespace
    activé. Fournisseur `vault` (stable, OpenBao documenté par ESO),
    authentification `jwt` par le jeton du compte `hops-eso` demandé par ESO
    lui-même (audience de la console, 10 min). `caBundle` : la CA d'OpenBao
    en base64 (rien à monter, rien de secret)."""
    bad = check_namespace(namespace)
    if bad:
        raise EsoError(bad[0]["code"], f"{namespace}: not a team namespace")
    if not str(addr or "").startswith("https://"):
        raise ValueError("OpenBao address: https://")
    for what, v in (("kv mount", kv_mount), ("jwt mount", jwt_mount), ("audience", audience)):
        if not isinstance(v, str) or not v or any(c.isspace() for c in v):
            raise ValueError(f"{what}: required")
    provider = {"server": str(addr).rstrip("/"), "path": kv_mount, "version": "v2",
                "auth": {"jwt": {"path": jwt_mount, "role": ROLE,
                                 "kubernetesServiceAccountToken": {
                                     "serviceAccountRef": {"name": ACCOUNT},
                                     "audiences": [audience], "expirationSeconds": TOKEN_SECONDS}}}}
    if ca_pem:
        provider["caBundle"] = base64.b64encode(str(ca_pem).encode()).decode()
    return [{"apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
             "metadata": {"name": ACCOUNT, "namespace": namespace}},
            {"apiVersion": API, "kind": "SecretStore", "metadata": {"name": STORE, "namespace": namespace},
             "spec": {"provider": {"vault": provider}}}]


def store_objects(namespaces, addr, kv_mount, jwt_mount, audience, ca_pem):
    out = []
    for ns in namespaces:
        out += namespace_objects(ns, addr, kv_mount, jwt_mount, audience, ca_pem)
    return out


def example(namespace, key, prefix, name="db"):
    """`ExternalSecret` d'exemple (à copier ; jamais posé par la console,
    point 299) : un rafraîchissement par heure (décision 307)."""
    remote = f"{prefix}/eso/{value_path(key, namespace, name)}"
    return {"apiVersion": API, "kind": "ExternalSecret",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"refreshInterval": EXAMPLE_REFRESH,
                     "secretStoreRef": {"kind": "SecretStore", "name": STORE},
                     "target": {"name": name, "creationPolicy": "Owner"},
                     "dataFrom": [{"extract": {"key": remote}}]}}


# ---------------------------------------------------------------------------
# État (sans aucune valeur)
# ---------------------------------------------------------------------------
def _condition(obj, ctype="Ready"):
    for c in ((obj or {}).get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c
    return None


def status(externalsecrets):
    """[{ns, name, ready, reason, message, synced_at, store, target}] des
    `ExternalSecret` lus ; jamais une donnée ni une valeur (le statut d'ESO
    n'en porte pas, mais seuls ces champs sont recopiés)."""
    out = []
    for es in externalsecrets or []:
        md = es.get("metadata") or {}
        spec = es.get("spec") or {}
        st = es.get("status") or {}
        c = _condition(es) or {}
        out.append({"ns": md.get("namespace") or "", "name": md.get("name") or "",
                    "ready": c.get("status") == "True", "reason": str(c.get("reason") or "")[:80],
                    "message": str(c.get("message") or "")[:200],
                    "synced_at": str(st.get("refreshTime") or ""),
                    "store": str((spec.get("secretStoreRef") or {}).get("name") or ""),
                    "target": str((spec.get("target") or {}).get("name") or md.get("name") or "")})
    return sorted(out, key=lambda e: (e["ns"], e["name"]))


def store_status(secretstores):
    """[{ns, name, ready, reason, message, ours}] des `SecretStore`."""
    out = []
    for s in secretstores or []:
        md = s.get("metadata") or {}
        c = _condition(s) or {}
        out.append({"ns": md.get("namespace") or "", "name": md.get("name") or "",
                    "ready": c.get("status") == "True", "reason": str(c.get("reason") or "")[:80],
                    "message": str(c.get("message") or "")[:200],
                    "ours": mf.is_ours(s, ITEM, (md.get("labels") or {}).get(mf.L_CLUSTER) or "")
                    and md.get("name") == STORE})
    return sorted(out, key=lambda e: (e["ns"], e["name"]))


def stores_ready(secretstores, namespaces):
    """(tous prêts, en attente) : le `SecretStore` `hops-openbao` de chaque
    namespace activé porte `Ready` vrai."""
    seen = {(s["ns"]): s for s in store_status(secretstores) if s["name"] == STORE}
    waiting = [ns for ns in namespaces if not (seen.get(ns) or {}).get("ready")]
    return not waiting, waiting


# ---------------------------------------------------------------------------
# Retrait sans perte des Secrets (point 300)
# ---------------------------------------------------------------------------
def _is_es_owner(ref, uids):
    group = str(ref.get("apiVersion") or "").rpartition("/")[0]
    return ref.get("kind") == "ExternalSecret" and group == GROUP and ref.get("uid") in uids


def orphan_patches(secrets, externalsecrets):
    """[(namespace, nom, patch fusion)] : pour chaque Secret dont un
    propriétaire est un `ExternalSecret` lu, la liste `ownerReferences` sans
    lui (les autres propriétaires restent ; liste vide -> null). Le Secret
    survit alors au ramasse-miettes quand les `ExternalSecret` partent
    (essai 7 : avec la référence, il était supprimé)."""
    uids = {(e.get("metadata") or {}).get("uid") for e in externalsecrets or []}
    uids.discard(None)
    out = []
    for s in secrets or []:
        md = s.get("metadata") or {}
        refs = md.get("ownerReferences") or []
        keep = [r for r in refs if not _is_es_owner(r, uids)]
        if len(keep) != len(refs):
            out.append((md.get("namespace") or "", md.get("name") or "",
                        {"metadata": {"ownerReferences": copy.deepcopy(keep) or None}}))
    return out


def stuck_patches(externalsecrets):
    """[(namespace, nom, patch)] : finalizer d'ESO retiré des
    `ExternalSecret` encore là après l'attente (repli, `eso-remove-stuck`
    si l'attente dépasse encore son délai)."""
    out = []
    for e in externalsecrets or []:
        md = e.get("metadata") or {}
        fins = md.get("finalizers") or []
        if FINALIZER in fins:
            out.append((md.get("namespace") or "", md.get("name") or "",
                        {"metadata": {"finalizers": [f for f in fins if f != FINALIZER] or None}}))
    return out
