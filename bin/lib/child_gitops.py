"""harvester-ops : GitOps d'un cluster enfant (2.8.0, décision 124).

Aucun outil par défaut ; une entrée `gitops` choisit l'outil (point 223) :
**Fleet** si le cluster enfant est importé dans le Rancher de la session
(marque `harvester-ops.io/rancher-external` sur son `Cluster`), **Flux**
sinon. La console pose l'outil et sa source, montre son état, et ne
réécrit rien de ce que l'outil sait faire.

- Flux (point 224) : chart communautaire `flux2` (Apache-2.0, jamais Flux
  Operator sous AGPL) livré par un paquet et installé par CAAPH sous le nom
  de service `flux`. La console ne fixe pas sa version : Flux 2.10 refuse
  Kubernetes avant 1.35 (vu sur harvlab2, plan 2.8.0, « Essais préalables
  (tâche 1) », point 4), le paquet porte celle qui convient. Verrouillage
  multi-locataire (point 225) par les valeurs `multitenancy` du chart
  (`VALUES`). Objets d'une entrée dans `hops-gitops` (hors `*-system`) :
  source `OCIRepository` ou `GitRepository`, `Kustomization` qui vise un
  namespace d'équipe avec le compte `hops-gitops-<nom>` du namespace
  `hops-gitops` (Flux usurpe `system:serviceaccount:hops-gitops:<compte>`),
  lié à `edit` dans le namespace visé ; `privileges: cluster`
  (administrateur seulement) le lie à `cluster-admin`. Identifiants de
  lecture dans des Secrets du cluster enfant, livrés par la ressource Secret
  `resource-set` du CRS (point 227), jamais par le ConfigMap.
- Fleet : `GitRepo` ou `HelmOp` dans `fleet-default` du cluster `local` du
  Rancher, écrit avec le jeton de la session ; le cluster Fleet d'un cluster
  importé porte le nom du cluster de provisionnement (vu en 2.14).

Sources (point 226) : `oci` (artefact Flux d'un paquet publié, Flux
seulement), `git` (Flux et Fleet), `chart` (chart d'un registre, Fleet
seulement, `HelmOp`).

Bibliothèque standard seulement ; ce module ne parle à aucun cluster.
"""

import base64
import json
import re

import capi_manifests as mf

TOOLS = ("auto", "flux", "fleet")
SOURCES = ("oci", "git", "chart")
PRIVILEGES = ("namespace", "cluster")
NAMESPACE = "hops-gitops"
SERVICE = "flux"
FLUX_NAMESPACE = "flux-system"
ITEM_PREFIX = "gitops-"
NAME_MAX = 34                     # art-gitops-<nom> tient dans capi_services.NAME_RE (42)
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,32}[a-z0-9])?$")
REF_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
PATH_RE = re.compile(r"^\.?(/[A-Za-z0-9._-]+)*/?$")
GIT_URL_RE = re.compile(r"^(https|ssh)://[^\s@/]+(@[^\s/]+)?(:\d{1,5})?/[^\s]+$")
INTERVAL_MIN, INTERVAL_MAX, INTERVAL_DEFAULT = 1, 24 * 60, 10
FLEET_NAMESPACE = "fleet-default"
FLEET_API = "fleet.cattle.io/v1alpha1"
KEYS = {"name", "tool", "source", "namespace", "privileges", "interval_minutes", "prune"}
SOURCE_KEYS = {"oci": {"type", "package", "version", "item", "registry"},
               "git": {"type", "url", "branch", "path", "secret"},
               "chart": {"type", "package", "version", "item", "registry"}}
# Valeurs imposées au chart flux2 (noms relevés dans le chart 2.20.0 et
# 2.19.1, plan 2.8.0, point 4) : verrouillage multi-locataire, aucune
# automatisation d'images.
VALUES = {"multitenancy": {"enabled": True, "defaultServiceAccount": NAMESPACE, "privileged": False},
          "imageAutomationController": {"create": False},
          "imageReflectionController": {"create": False},
          "sourceWatcher": {"create": False}}
L_ENTRY = "harvester-ops.io/gitops-entry"


def flux_values(user_values=None):
    """Valeurs du chart flux2 : celles du paquet, puis `VALUES` imposées
    par-dessus (le verrouillage ne se retire pas par une valeur)."""
    out = json.loads(json.dumps(user_values or {}))
    for k, v in VALUES.items():
        out[k] = dict(out.get(k) or {}, **v) if isinstance(out.get(k), dict) else dict(v)
    return out


def flux_locked(values):
    """Le service flux posé porte-t-il le verrouillage multi-locataire ?"""
    mt = (values or {}).get("multitenancy") or {}
    return mt.get("enabled") is True and mt.get("privileged") is False \
        and mt.get("defaultServiceAccount") == NAMESPACE
FLUX_SOURCE_API = "source.toolkit.fluxcd.io/v1"
FLUX_KUSTOMIZE_API = "kustomize.toolkit.fluxcd.io/v1"


class GitopsError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def finding(code, level="block", **facts):
    return {"code": code, "level": level, "facts": facts}


def item_of(name):
    """Nom d'élément du CRS d'une entrée (`art-gitops-<nom>`)."""
    return ITEM_PREFIX + name


def choose(tool, imported):
    """`fleet` ou `flux` ; `auto` = fleet si importé, flux sinon.
    GitopsError `gitops-fleet-needs-rancher` pour fleet sans import."""
    if tool not in TOOLS:
        raise GitopsError("gitops-invalid", f"tool: one of {', '.join(TOOLS)}")
    if tool == "auto":
        return "fleet" if imported else "flux"
    if tool == "fleet" and not imported:
        raise GitopsError("gitops-fleet-needs-rancher", "Fleet needs the child cluster imported in Rancher")
    return tool


def normalize(entry):
    """Entrée nettoyée, défauts posés ; ValueError qui nomme le champ."""
    if not isinstance(entry, dict):
        raise ValueError("gitops entry: an object")
    for k in sorted(set(entry) - KEYS):
        raise ValueError(f"gitops.{k}: unknown key")
    name = entry.get("name")
    if not isinstance(name, str) or not REF_RE.match(name):
        raise ValueError("gitops.name: RFC 1123 name")
    out = {"name": name, "tool": entry.get("tool") or "auto", "privileges": entry.get("privileges") or "namespace",
           "prune": entry.get("prune", True), "interval_minutes": entry.get("interval_minutes", INTERVAL_DEFAULT)}
    if out["tool"] not in TOOLS:
        raise ValueError(f"gitops.tool: one of {', '.join(TOOLS)}")
    if out["privileges"] not in PRIVILEGES:
        raise ValueError(f"gitops.privileges: one of {', '.join(PRIVILEGES)}")
    if not isinstance(out["prune"], bool):
        raise ValueError("gitops.prune: true or false")
    try:
        n = int(out["interval_minutes"])
    except (TypeError, ValueError):
        raise ValueError(f"gitops.interval_minutes: {INTERVAL_MIN} to {INTERVAL_MAX}") from None
    if not INTERVAL_MIN <= n <= INTERVAL_MAX:
        raise ValueError(f"gitops.interval_minutes: {INTERVAL_MIN} to {INTERVAL_MAX}")
    out["interval_minutes"] = n
    ns = entry.get("namespace")
    if not isinstance(ns, str) or not REF_RE.match(ns):
        raise ValueError("gitops.namespace: the team namespace aimed at (RFC 1123)")
    out["namespace"] = ns
    src = entry.get("source")
    if not isinstance(src, dict) or src.get("type") not in SOURCES:
        raise ValueError(f"gitops.source.type: one of {', '.join(SOURCES)}")
    t = src["type"]
    for k in sorted(set(src) - SOURCE_KEYS[t]):
        raise ValueError(f"gitops.source.{k}: unknown key for a {t} source")
    if t in ("oci", "chart"):
        for k in ("package", "version", "item", "registry"):
            if not isinstance(src.get(k), str) or not src[k] or len(src[k]) > 128 or any(c.isspace() for c in src[k]):
                raise ValueError(f"gitops.source.{k}: required")
        out["source"] = {k: src[k] for k in ("type", "package", "version", "item", "registry")}
    else:
        url = src.get("url")
        if not isinstance(url, str) or len(url) > 2048 or not GIT_URL_RE.match(url):
            raise ValueError("gitops.source.url: https:// or ssh:// without credentials")
        if url.startswith("https://") and "@" in url.split("/", 3)[2]:
            raise ValueError("gitops.source.url: https:// or ssh:// without credentials")
        branch, path = src.get("branch") or "main", src.get("path") or "./"
        if not BRANCH_RE.match(branch) or ".." in branch:
            raise ValueError("gitops.source.branch: a branch name")
        if not PATH_RE.match(path) or ".." in path:
            raise ValueError("gitops.source.path: a path inside the repository")
        sec = src.get("secret")
        if sec is not None and (not isinstance(sec, str) or not REF_RE.match(sec)):
            raise ValueError("gitops.source.secret: the name of a secret of the plan")
        out["source"] = {"type": "git", "url": url, "branch": branch, "path": path}
        if sec:
            out["source"]["secret"] = sec
    return out


def check(entry, facts):
    """Constats d'une entrée normalisée. `facts` : {"tool" (choisi),
    "flux_ready", "role" (viewer, operator, admin), "secrets" (noms fournis),
    "published" (artefact ou chart publié dans le registre)}."""
    found = []
    tool, t = facts.get("tool"), entry["source"]["type"]
    if len(entry["name"]) > NAME_MAX:
        found.append(finding("gitops-name-too-long", name=entry["name"], max=NAME_MAX))
    if (tool == "fleet" and t == "oci") or (tool == "flux" and t == "chart"):
        found.append(finding("gitops-source-unsupported", tool=tool, source=t))
    if tool == "flux" and not facts.get("flux_ready"):
        found.append(finding("gitops-flux-missing", service=SERVICE))
    ns = entry["namespace"]
    if mf.is_system_namespace(ns) or ns in mf.IMMORTAL_NAMESPACES or ns == NAMESPACE:
        found.append(finding("gitops-namespace-not-team", namespace=ns))
    if entry["privileges"] == "cluster" and facts.get("role") != "admin":
        found.append(finding("gitops-privileges-admin-only"))
    if tool == "fleet":
        # revue 1-4 (I-4) : un GitRepo ou un HelmOp sans `serviceAccount`
        # déploie avec l'agent Fleet, administrateur du cluster enfant ;
        # `privileges: namespace` n'y est pas tenu. Fleet est donc réservé à
        # l'administrateur, et l'administrateur est prévenu (compte de Fleet
        # confiné à relever sur le banc, tâche 10)
        if facts.get("role") != "admin":
            found.append(finding("gitops-fleet-admin-only"))
        elif entry["privileges"] == "namespace":
            found.append(finding("gitops-fleet-not-confined", "warn", namespace=entry["namespace"]))
    sec = entry["source"].get("secret")
    if sec and sec not in set(facts.get("secrets") or ()):
        found.append(finding("gitops-secret-missing", secret=sec))
    if t in ("oci", "chart") and not facts.get("published"):
        s = entry["source"]
        found.append(finding("gitops-package-not-published", package=f"{s['package']}-{s['version']}",
                             item=s["item"], registry=s["registry"]))
    return found


def blocking(found):
    return [f for f in found if f["level"] == "block"]


# ---------------------------------------------------------------------------
# Flux
# ---------------------------------------------------------------------------
def account(entry):
    return f"hops-gitops-{entry['name']}"


def _secret(name, stype, data):
    return {"apiVersion": "v1", "kind": "Secret", "type": stype,
            "metadata": {"name": name, "namespace": NAMESPACE}, "stringData": data}


def dockerconfig(host, username, password):
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    return json.dumps({"auths": {host: {"username": username, "password": password, "auth": auth}}},
                      sort_keys=True)


def flux_objects(entry, source_url, secrets=None):
    """(objets du ConfigMap, Secrets du cluster enfant) d'une entrée Flux.
    `source_url` : `oci://<registre>/<dépôt>` ou l'adresse Git ;
    `secrets` : {"pull": {host, username, password}, "ca": pem,
    "git": {"identity", "known_hosts"} | {"username", "password"}}. Aucun
    identifiant ne va dans les objets du ConfigMap."""
    secrets = secrets or {}
    name, sa, src = entry["name"], account(entry), entry["source"]
    interval = f"{entry['interval_minutes']}m"
    objs = [{"apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
             "metadata": {"name": sa, "namespace": NAMESPACE}}]
    if entry["privileges"] == "cluster":
        objs.append(cluster_binding(entry))
    else:
        objs.append({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
                     "metadata": {"name": sa, "namespace": entry["namespace"]},
                     "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "edit"},
                     "subjects": [{"kind": "ServiceAccount", "name": sa, "namespace": NAMESPACE}]})
    out_secrets = []
    if src["type"] == "oci":
        spec = {"url": source_url, "ref": {"tag": src["version"]}, "interval": interval}
        pull = secrets.get("pull")
        if pull:
            out_secrets.append(_secret(f"{sa}-pull", "kubernetes.io/dockerconfigjson",
                                       {".dockerconfigjson": dockerconfig(pull["host"], pull["username"],
                                                                          pull["password"])}))
            spec["secretRef"] = {"name": f"{sa}-pull"}
        if secrets.get("ca"):
            out_secrets.append(_secret(f"{sa}-ca", "Opaque", {"ca.crt": secrets["ca"]}))
            spec["certSecretRef"] = {"name": f"{sa}-ca"}
        kind = "OCIRepository"
    else:
        spec = {"url": source_url, "ref": {"branch": src["branch"]}, "interval": interval}
        git = secrets.get("git")
        if git:
            data = {k: git[k] for k in ("identity", "known_hosts", "username", "password") if git.get(k)}
            out_secrets.append(_secret(f"{sa}-git", "Opaque", data))
            spec["secretRef"] = {"name": f"{sa}-git"}
        kind = "GitRepository"
    objs.append({"apiVersion": FLUX_SOURCE_API, "kind": kind,
                 "metadata": {"name": name, "namespace": NAMESPACE}, "spec": spec})
    objs.append({"apiVersion": FLUX_KUSTOMIZE_API, "kind": "Kustomization",
                 "metadata": {"name": name, "namespace": NAMESPACE},
                 "spec": {"sourceRef": {"kind": kind, "name": name},
                          "path": src.get("path") or "./", "prune": entry["prune"], "interval": interval,
                          "targetNamespace": entry["namespace"], "serviceAccountName": sa}})
    return objs, out_secrets


def cluster_binding(entry):
    """`privileges: cluster` : la liaison vers `cluster-admin` rendue par la
    console (administrateur seulement), admise par `capi_manifests.check`
    par son paramètre `allow`."""
    sa = account(entry)
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
            "metadata": {"name": sa},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
            "subjects": [{"kind": "ServiceAccount", "name": sa, "namespace": NAMESPACE}]}


def _ready(obj):
    for c in ((obj or {}).get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True", c.get("message") or c.get("reason") or ""
    return False, "not reconciled yet"


def state_flux(source, kustomization, revision=None):
    """{"ready", "revision", "message"} : source puis `Kustomization` prêtes,
    à la révision attendue quand elle est donnée (`<étiquette>@sha256:...`
    pour une source OCI : le préfixe d'étiquette suffit)."""
    if source is None or kustomization is None:
        return {"ready": False, "revision": "", "message": "not delivered yet"}
    ok, msg = _ready(source)
    if not ok:
        return {"ready": False, "revision": "", "message": f"source: {msg}"[:300]}
    ok, msg = _ready(kustomization)
    rev = ((kustomization.get("status") or {}).get("lastAppliedRevision")) or ""
    if not ok:
        return {"ready": False, "revision": rev, "message": f"kustomization: {msg}"[:300]}
    if revision and not (rev == revision or rev.startswith(f"{revision}@")):
        return {"ready": False, "revision": rev, "message": f"applied {rev}, waiting for {revision}"}
    return {"ready": True, "revision": rev, "message": "applied"}


# ---------------------------------------------------------------------------
# Fleet
# ---------------------------------------------------------------------------
def fleet_name(harvester, ns, cluster, entry):
    """`hops-<harvester>-<c>-<nom>` (`hops-<harvester>-<ns>-<c>-<nom>` si le
    namespace diffère du nom) : une entrée du même nom sur deux clusters
    enfants ne partage ni objet ni Secret (revue 1-4 I-5). Au-delà de 63
    caractères : 54 plus `-` et 8 hexadécimaux de sha256 du nom complet."""
    import hashlib
    child = cluster if ns == cluster else f"{ns}-{cluster}"
    full = f"hops-{harvester}-{child}-{entry['name']}".lower()
    if len(full) <= 63:
        return full
    return f"{full[:54].rstrip('-')}-{hashlib.sha256(full.encode()).hexdigest()[:8]}"


def fleet_labels(harvester, ns, cluster, entry):
    return {mf.MANAGED: "true", "harvester-ops.io/harvester": harvester, "harvester-ops.io/child-namespace": ns,
            "harvester-ops.io/child": cluster, L_ENTRY: entry["name"]}


A_PLAN = "harvester-ops.io/plan"


def fleet_object(entry, fleet_cluster, harvester, ns, cluster, chart_repo=None, secret_name=None,
                 by="", run=None, plan=None):
    """`GitRepo` (source git) ou `HelmOp` (source chart) de `fleet-default`,
    ciblé sur le cluster Fleet du cluster importé. `chart_repo` :
    `oci://<registre>/<chemin du chart>` ; `secret_name` : Secret de
    `fleet-default` (clé SSH ou compte de lecture). `plan` (revue 5-8
    I-11) : le plan qui la pose, comparé à la reprise comme pour Flux."""
    src = entry["source"]
    ann = {mf.A_BY: by or ""}
    if run:
        ann["harvester-ops.io/plan-run"] = run
    if plan:
        ann[A_PLAN] = plan
    meta = {"name": fleet_name(harvester, ns, cluster, entry), "namespace": FLEET_NAMESPACE,
            "labels": fleet_labels(harvester, ns, cluster, entry), "annotations": ann}
    targets = [{"clusterName": fleet_cluster}]
    if src["type"] == "git":
        spec = {"repo": src["url"], "branch": src["branch"], "paths": [src.get("path") or "./"],
                "targetNamespace": entry["namespace"], "targets": targets}
        if secret_name:
            spec["clientSecretName"] = secret_name
        return {"apiVersion": FLEET_API, "kind": "GitRepo", "metadata": meta, "spec": spec}
    if src["type"] == "chart":
        spec = {"helm": {"releaseName": entry["name"], "repo": chart_repo, "version": src["version"]},
                "namespace": entry["namespace"], "targets": targets}
        if secret_name:
            spec["helmSecretName"] = secret_name
        return {"apiVersion": FLEET_API, "kind": "HelmOp", "metadata": meta, "spec": spec}
    raise GitopsError("gitops-source-unsupported", f"Fleet does not take a {src['type']} source")


def fleet_ours(obj, harvester, ns, cluster, entry, expected_run=None):
    md = (obj or {}).get("metadata") or {}
    lab = md.get("labels") or {}
    if any(lab.get(k) != v for k, v in fleet_labels(harvester, ns, cluster, entry).items()):
        return False
    return not expected_run or (md.get("annotations") or {}).get("harvester-ops.io/plan-run") == expected_run


def state_fleet(obj):
    """{"ready", "message"} : `status.summary.ready` égal à `desiredReady`
    (non nul) ; message de la condition Ready sinon."""
    st = (obj or {}).get("status") or {}
    s = st.get("summary") or {}
    want, have = s.get("desiredReady") or 0, s.get("ready") or 0
    if want and have == want:
        return {"ready": True, "message": f"{have}/{want} ready"}
    ok, msg = _ready(obj)
    return {"ready": False, "message": (msg if not ok else f"{have}/{want} ready")[:300]}
