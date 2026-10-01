"""harvester-ops : les projets Rancher d'un cluster Harvester (v1.72.0).

Un projet n'existe QUE sur le serveur Rancher (management.cattle.io/v3,
namespace = id du cluster) : noms, quotas, limites par défaut. Sur le
cluster Harvester, un namespace dit son projet par l'annotation
field.cattle.io/projectId « <cluster>:<projet> » (l'interface de Rancher
pose aussi le label, sans la partie cluster), et Rancher y écrit le quota du
namespace (annotation field.cattle.io/resourceQuota, ResourceQuota et
LimitRange « default-… »). Vu sur harv1 le 27/09/2026 : 19 namespaces
annotés avec l'id d'un cluster que Rancher ne connaît plus (premier import),
donc « hors projet » pour Rancher ; la console le dit au lieu de les ranger
dans un projet qui n'existe pas.

Les écritures passent par l'API de Rancher avec le jeton de la personne
(connexion par Rancher) : Rancher y applique ses droits et ses règles, que
la console vérifie aussi avant d'écrire (celles du rancher-webhook).
"""

import json
import re

ANN_PROJECT = "field.cattle.io/projectId"
ANN_QUOTA = "field.cattle.io/resourceQuota"
ANN_LIMIT = "field.cattle.io/containerDefaultResourceLimit"
ANN_STATUS = "cattle.io/status"
L_DEFAULT_QUOTA = "resourcequota.management.cattle.io/default-resource-quota"

# les ressources d'un quota (noms de l'API de Rancher) et leur nature
QUOTA_KEYS = {"limitsCpu": "cpu", "limitsMemory": "mem", "requestsCpu": "cpu", "requestsMemory": "mem",
              "requestsStorage": "mem", "persistentVolumeClaims": "count", "pods": "count", "services": "count",
              "servicesLoadBalancers": "count", "servicesNodePorts": "count", "configMaps": "count",
              "secrets": "count", "replicationControllers": "count"}
LIMIT_KEYS = ("requestsCpu", "requestsMemory", "limitsCpu", "limitsMemory")
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
PID_RE = re.compile(r"^p-[a-z0-9]{3,12}$")
CID_RE = re.compile(r"^(c-[a-z0-9]{3,12}|local)$")
_MEM = {"": 1, "k": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4,
        "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}


def check_name(name, what="name"):
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError(f"{what}: lowercase letters, digits and '-', 63 characters at most")
    return name


def check_pid(pid):
    pid = str(pid or "").strip()
    if not PID_RE.match(pid):
        raise ValueError("project: an id like p-xxxxx")
    return pid


# ---------------------------------------------------------------------------
# Quantités (les formats du formulaire de Rancher : millicœurs, Mi)
# ---------------------------------------------------------------------------

def millicores(v):
    s = str(v).strip()
    try:
        return int(s[:-1]) if s.endswith("m") else int(round(float(s) * 1000))
    except ValueError:
        raise ValueError(f"CPU: '{v}' is not a quantity (2, 0.5 or 500m)") from None


def mem_bytes(v):
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(Ki|Mi|Gi|Ti|k|M|G|T)?$", str(v).strip())
    if not m:
        raise ValueError(f"memory: '{v}' is not a quantity (4Gi, 512Mi)")
    return int(float(m.group(1)) * _MEM[m.group(2) or ""])


def normalize(key, v):
    """La valeur telle que Rancher l'écrit : CPU en millicœurs, mémoire et
    stockage en Mi, les nombres d'objets en entiers."""
    kind = QUOTA_KEYS.get(key)
    if kind is None:
        raise ValueError(f"quota: unknown resource {key}")
    if kind == "cpu":
        n = millicores(v)
        if n < 0:
            raise ValueError(f"{key}: 0 or more")
        return f"{n}m"
    if kind == "mem":
        b = mem_bytes(v)
        return f"{-(-b // 1024 ** 2)}Mi"
    try:
        n = int(str(v).strip())
    except ValueError:
        raise ValueError(f"{key}: a whole number") from None
    if n < 0:
        raise ValueError(f"{key}: 0 or more")
    return str(n)


def magnitude(key, v):
    kind = QUOTA_KEYS.get(key, "count")
    return millicores(v) if kind == "cpu" else mem_bytes(v) if kind == "mem" else int(v)


def _limits(d, what):
    out = {}
    for k, v in (d or {}).items():
        if v in (None, ""):
            continue
        out[k] = normalize(k, v)
    return out


# ---------------------------------------------------------------------------
# Projets
# ---------------------------------------------------------------------------

def project_body(spec, cid):
    """{name (affiché), description, quota {clé: valeur}, ns_default {clé:
    valeur}, container {requestsCpu, ...}} -> corps de /v3/projects. Les
    règles du rancher-webhook : quota du projet et défaut des namespaces
    ensemble, sur les mêmes ressources, défaut <= limite ; requests <=
    limits pour les conteneurs."""
    name = str(spec.get("name") or "").strip()
    if not name or len(name) > 63:
        raise ValueError("project name: 1 to 63 characters")
    if not CID_RE.match(str(cid or "")):
        raise ValueError("cluster: the Rancher id of the cluster (c-xxxxx)")
    quota, nsdef = _limits(spec.get("quota"), "quota"), _limits(spec.get("ns_default"), "namespace default")
    if bool(quota) != bool(nsdef):
        raise ValueError("quotas: the project limit and the namespace default go together")
    if set(quota) != set(nsdef):
        raise ValueError("quotas: the same resources in the project limit and the namespace default")
    for k in quota:
        if magnitude(k, nsdef[k]) > magnitude(k, quota[k]):
            raise ValueError(f"quotas: the namespace default of {k} exceeds the project limit")
    cont = {}
    for k, v in (spec.get("container") or {}).items():
        if k not in LIMIT_KEYS:
            raise ValueError("VM default limit: " + ", ".join(LIMIT_KEYS))
        if v not in (None, ""):
            cont[k] = normalize(k, v)
    for r, l in (("requestsCpu", "limitsCpu"), ("requestsMemory", "limitsMemory")):
        if r in cont and l in cont and magnitude(r, cont[r]) > magnitude(l, cont[l]):
            raise ValueError(f"VM default limit: {r} exceeds {l}")
    body = {"type": "project", "clusterId": cid, "name": name, "description": str(spec.get("description") or "").strip()}
    body["resourceQuota"] = {"limit": quota} if quota else None
    body["namespaceDefaultResourceQuota"] = {"limit": nsdef} if nsdef else None
    body["containerDefaultResourceLimit"] = cont or None
    return body


def _clean(d, keys):
    """L'API de Rancher ajoute « type: /v3/schemas/... » à ces objets (vu en
    réel) : seules les ressources restent."""
    return {k: v for k, v in (d or {}).items() if k in keys}


def project_rows(items, cid):
    """/v3/projects -> lignes : id (p-…), nom affiché, quotas, usage, système."""
    out = []
    for p in items or []:
        full = p.get("id") or ""
        pid = full.split(":", 1)[1] if ":" in full else full
        if p.get("clusterId") not in (None, cid):
            continue
        rq = p.get("resourceQuota") or {}
        labels = p.get("labels") or {}
        out.append({"id": pid, "full_id": full, "name": p.get("name") or pid, "description": p.get("description") or "",
                    "quota": _clean(rq.get("limit"), QUOTA_KEYS), "used": _clean(rq.get("usedLimit"), QUOTA_KEYS),
                    "ns_default": _clean((p.get("namespaceDefaultResourceQuota") or {}).get("limit"), QUOTA_KEYS),
                    "container": _clean(p.get("containerDefaultResourceLimit"), LIMIT_KEYS), "state": p.get("state") or "",
                    "system": labels.get("authz.management.cattle.io/system-project") == "true" or p.get("name") == "System",
                    "default": labels.get("authz.management.cattle.io/default-project") == "true"})
    return sorted(out, key=lambda r: (r["system"], not r["default"], r["name"].lower()))


def parse_project(value):
    """« c-xxx:p-yyy » -> (c-xxx, p-yyy) ; un label « p-yyy » seul -> (None, p-yyy)."""
    v = str(value or "").strip()
    if not v:
        return None, None
    if ":" in v:
        c, p = v.split(":", 1)
        return c, p
    return None, v


def _status_conditions(meta):
    try:
        return (json.loads(((meta.get("annotations") or {}).get(ANN_STATUS)) or "{}") or {}).get("Conditions") or []
    except ValueError:
        return []


def ns_project(ns, cid=None, projects=None):
    """Ce que dit un namespace de son projet, comparé au cluster et aux
    projets que Rancher connaît : membre d'un projet connu, d'un projet
    inconnu, d'un AUTRE cluster (annotation périmée), ou hors projet ; son
    quota et ce que Rancher en a dit."""
    meta = (ns or {}).get("metadata") or {}
    ann = meta.get("annotations") or {}
    c, p = parse_project(ann.get(ANN_PROJECT))
    if p is None:
        c, p = parse_project((meta.get("labels") or {}).get(ANN_PROJECT))
    known = {r["id"]: r for r in projects or []}
    if p is None:
        state = "none"
    elif c and cid and c != cid:
        state = "foreign"            # l'id d'un autre cluster : Rancher l'ignore
    elif projects is not None and p not in known:
        state = "unknown"
    else:
        state = "member"
    try:
        quota = (json.loads(ann.get(ANN_QUOTA) or "{}") or {}).get("limit") or {}
    except ValueError:
        quota = {}
    cond = {c_.get("Type"): c_ for c_ in _status_conditions(meta)}
    val = cond.get("ResourceQuotaValidated") or {}
    return {"cluster": c, "project": p, "state": state, "name": (known.get(p) or {}).get("name") if p else None,
            "quota": quota, "quota_ok": None if not val else val.get("Status") == "True",
            "quota_message": val.get("Message") or ""}


def ns_quota(limit):
    """La valeur de field.cattle.io/resourceQuota pour un namespace ({} = retirer)."""
    lim = _limits(limit, "namespace quota")
    return json.dumps({"limit": lim}, separators=(",", ":")) if lim else None


def check_ns_quota(limit, project):
    """Le quota d'un namespace : seulement les ressources limitées par son
    projet, et sous la limite du projet (Rancher mettrait sinon le quota à
    zéro, ce qui empêche tout démarrage de VM)."""
    lim = _limits(limit, "namespace quota")
    pq = (project or {}).get("quota") or {}
    if lim and not pq:
        raise ValueError("namespace quota: the project has no quota; Rancher would ignore it")
    for k, v in lim.items():
        if k not in pq:
            raise ValueError(f"namespace quota: {k} is not limited by the project")
        if magnitude(k, v) > magnitude(k, pq[k]):
            raise ValueError(f"namespace quota: {k} exceeds the project limit ({pq[k]})")
    return lim


# ---------------------------------------------------------------------------
# Rancher, vu du kubeconfig de la session
# ---------------------------------------------------------------------------

def rancher_of(kubeconfig_text):
    """Le kubeconfig d'une session Rancher vise le mandataire
    https://<rancher>/k8s/clusters/<id> avec un fichier de jeton : il dit
    tout ce qu'il faut pour parler à Rancher au nom de la personne. None
    pour un kubeconfig direct (compte local de la console)."""
    try:
        import yaml
        kc = yaml.safe_load(kubeconfig_text) or {}
    except Exception:  # noqa: BLE001
        try:
            kc = json.loads(kubeconfig_text)
        except ValueError:
            return None
    cluster = ((kc.get("clusters") or [{}])[0].get("cluster")) or {}
    user = ((kc.get("users") or [{}])[0].get("user")) or {}
    m = re.match(r"^(https?://[^/]+(?:/[^/]+)*?)/k8s/clusters/([^/]+)/?$", cluster.get("server") or "")
    if not m or not (user.get("tokenFile") or user.get("token")):
        return None
    return {"url": m.group(1), "cid": m.group(2), "token_file": user.get("tokenFile"), "token": user.get("token"),
            "ca_file": cluster.get("certificate-authority"),
            # v1.79.0 : un Rancher réglé sans vérification TLS
            "insecure": bool(cluster.get("insecure-skip-tls-verify"))}
