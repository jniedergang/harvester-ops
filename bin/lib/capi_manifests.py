"""harvester-ops : manifestes livrés aux clusters enfants (2.7.0, décision 123).

Un élément `manifest` ou `policy` d'un paquet, appliqué à un cluster enfant
`<ns>/<c>`, devient sur le cluster de gestion (Harvester), dans `<ns>`, un
ConfigMap et un `ClusterResourceSet` nommés `art-<élément>`, en mode
`Reconcile` (le mécanisme de kube-vip, `capi_cluster.kube_vip_docs`). Le
CRS vise le cluster par l'étiquette `harvester-ops.io/art-<élément>: "on"`
posée sur le `Cluster`.

Cluster API ne supprime jamais ce qu'un CRS a appliqué : chaque objet rendu
porte les étiquettes de la console (`stamp`), et le retrait (`removed`,
`delete_order`) ne supprime dans le cluster enfant que les objets qui les
portent encore (point 203). Les objets se déduisent du ConfigMap écrit par
la console (`objects_of`) : le `ClusterResourceSetBinding` ne liste que les
ressources (ConfigMaps) et leur empreinte.

Ce module ne parle à aucun cluster. Bibliothèque standard seulement : les
objets sont rangés en JSON dans le ConfigMap ; lire le YAML d'un paquet
demande PyYAML (chargé à l'appel) ou yq.

Voir docs/design/2026-11-07-artefacts-complets-defaire-270-plan.md.
"""

import copy
import hashlib
import json
import subprocess

import capi_cluster as cc
import capi_services as sv
import capi_stack as cs
import hv_containers as hc

PREFIX = "art-"
LABEL_PREFIX = "harvester-ops.io/art-"
MANAGED = cs.MANAGED
L_ITEM = "harvester-ops.io/artefact-item"
L_PACKAGE = "harvester-ops.io/artefact-package"
L_CLUSTER = "harvester-ops.io/artefact-cluster"
A_VERSION = "harvester-ops.io/artefact-version"
A_BY = "harvester-ops.io/applied-by"
CRS_API = "addons.cluster.x-k8s.io/v1beta1"        # celle de kube_vip_docs
K_CRS = cc.K_CRS
K_CRSB = "clusterresourcesetbindings.addons.cluster.x-k8s.io"
DATA_KEY = "objects.yaml"
MAX_OBJECTS = 500

# Portée cluster acceptée (point 204) ; toute autre sorte sans namespace est
# refusée, et celles-ci le sont toujours, namespace donné ou non.
CLUSTER_KINDS = ("Namespace", "ClusterRole", "ClusterRoleBinding", "StorageClass",
                 "PriorityClass", "IngressClass")
REFUSED_KINDS = ("CustomResourceDefinition", "APIService", "MutatingWebhookConfiguration",
                 "ValidatingWebhookConfiguration", "ValidatingAdmissionPolicy",
                 "ValidatingAdmissionPolicyBinding", "Node", "PersistentVolume", "CSIDriver",
                 "RuntimeClass")
# Namespaces système du cluster enfant : ceux de la console
# (hv_containers.SYSTEM_NAMESPACES, dont `*-system` couvre calico-system),
# plus celui de l'opérateur Calico que RKE2 pose.
EXTRA_SYSTEM_NAMESPACES = ("tigera-operator",)
SA_TOKEN_TYPE = "kubernetes.io/service-account-token"
# Un Namespace livré n'est supprimé que s'il ne porte aucune charge ni aucun
# volume d'ailleurs (objets sans propriétaire et sans les étiquettes de
# l'élément) : sa suppression emporterait le travail d'une équipe.
FOREIGN_KINDS = ("deployments.apps,statefulsets.apps,daemonsets.apps,jobs.batch,cronjobs.batch,"
                 "persistentvolumeclaims,pods")


def finding(code, level="block", **facts):
    return cc.finding(code, level, **facts)


def name_of(item):
    return PREFIX + item


def label_of(item):
    return LABEL_PREFIX + item


def is_system_namespace(ns):
    return hc.is_system_namespace(ns) or ns in EXTRA_SYSTEM_NAMESPACES


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------
def _load_yaml_all(text):
    """Documents YAML : PyYAML s'il est là, sinon yq (sortie JSON)."""
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            return list(yaml.safe_load_all(text))
        except yaml.YAMLError as e:
            raise ValueError(f"not YAML: {str(e).splitlines()[0]}") from None
    import kube
    try:
        p = subprocess.run(["yq", "-o=json", "-I=0", "."], input=text, capture_output=True,
                           text=True, timeout=30)
    except OSError:
        raise ValueError("reading YAML needs PyYAML or yq") from None
    if p.returncode != 0:
        raise ValueError(f"not YAML: {(p.stderr or '').strip()[:200]}")
    return kube.json_documents(p.stdout)


def _documents(text):
    try:
        return [json.loads(text)]
    except ValueError:
        return _load_yaml_all(text)


def parse(files, load=None):
    """{nom: texte} -> [objet], fichiers dans l'ordre des noms ; documents
    vides ignorés, `List` dépliée. ValueError pour un document qui n'est pas
    un objet."""
    load = load or _documents
    out = []
    for fname in sorted(files):
        for doc in load(files[fname]):
            if doc is None:
                continue
            if not isinstance(doc, dict):
                raise ValueError(f"{fname}: a document is not an object")
            if doc.get("kind") == "List" or str(doc.get("kind", "")).endswith("List") and "items" in doc:
                items = doc.get("items") or []
                if not all(isinstance(i, dict) for i in items):
                    raise ValueError(f"{fname}: a List item is not an object")
                out.extend(items)
            else:
                out.append(doc)
    return out


# ---------------------------------------------------------------------------
# Contrôle
# ---------------------------------------------------------------------------
def _group(api_version):
    g, sep, _ = str(api_version or "").rpartition("/")
    return g if sep else ""


def ref_of(obj):
    md = obj.get("metadata") or {}
    kind = obj.get("kind") or ""
    ns = "" if kind in CLUSTER_KINDS + REFUSED_KINDS else (md.get("namespace") or "")
    return (obj.get("apiVersion") or "", kind, ns, md.get("name") or "")


def _key(ref):
    return (_group(ref[0]), ref[1], ref[2], ref[3])


def data_of(objs):
    """Contenu du ConfigMap : les objets en JSON, un document par objet,
    séparés par `---` (YAML valide pour le ClusterResourceSet)."""
    return {DATA_KEY: "".join("---\n" + json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
                              for o in objs)}


def data_size(data):
    """Taille en octets (UTF-8) des données d'un ConfigMap."""
    return sum(len(k.encode()) + len(str(v).encode()) for k, v in (data or {}).items())


def objects_of(cm):
    """Les objets rangés par la console dans un ConfigMap `art-<élément>`."""
    text = ((cm or {}).get("data") or {}).get(DATA_KEY) or ""
    return [json.loads(chunk) for chunk in text.split("---\n") if chunk.strip()]


def check(objs, cluster, item=None):
    """Constats bloquants sur les objets d'un élément. `item` donné : la
    taille est mesurée sur le contenu réellement rangé (étiquettes
    comprises)."""
    found = []
    if len(objs) > MAX_OBJECTS:
        found.append(finding("manifest-too-many", count=len(objs), max=MAX_OBJECTS))
    seen = set()
    for i, o in enumerate(objs):
        md = o.get("metadata") or {}
        kind, name = o.get("kind") or "", md.get("name") or ""
        where = f"{kind or '?'} {name or '?'}"
        if not name or not kind or not o.get("apiVersion"):
            found.append(finding("manifest-no-name", index=i, object=where))
            continue
        if kind in REFUSED_KINDS:
            found.append(finding("manifest-kind-refused", object=where, kind=kind))
            continue
        ref = ref_of(o)
        if kind not in CLUSTER_KINDS and not ref[2]:
            found.append(finding("manifest-kind-refused", object=where, kind=kind,
                                 reason=f"cluster-scoped kinds allowed: {', '.join(CLUSTER_KINDS)}; "
                                        "a namespaced object needs metadata.namespace"))
            continue
        target_ns = name if kind == "Namespace" else ref[2]
        if target_ns and is_system_namespace(target_ns):
            found.append(finding("manifest-system-namespace", object=where, namespace=target_ns))
        if kind == "Secret" and o.get("type") == SA_TOKEN_TYPE:
            found.append(finding("manifest-sa-token-refused", object=where))
        if kind == "ClusterRoleBinding":
            role = (o.get("roleRef") or {}).get("name") or ""
            subjects = [str(s.get("name") or "") for s in o.get("subjects") or [] if isinstance(s, dict)]
            if role == "cluster-admin" or any(s.startswith("system:") for s in subjects):
                found.append(finding("manifest-crb-admin", object=where, role=role))
        if _key(ref) in seen:
            found.append(finding("manifest-duplicate", object=where, namespace=ref[2]))
        seen.add(_key(ref))
    size = data_size(data_of(stamp(objs, item, cluster) if item else objs))
    if size > cs.CM_LIMIT:
        found.append(finding("manifest-too-big", size=size, max=cs.CM_LIMIT))
    return found


def blocking(found):
    return cc.blocking(found)


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------
def marks(item, cluster):
    return {MANAGED: "true", L_ITEM: item, L_CLUSTER: cluster}


def stamp(objs, item, cluster):
    """Copies des objets portant les trois étiquettes de la console ; les
    étiquettes d'origine sont gardées."""
    out = []
    for o in objs:
        c = copy.deepcopy(o)
        md = c.setdefault("metadata", {})
        md["labels"] = dict(md.get("labels") or {}, **marks(item, cluster))
        out.append(c)
    return out


def is_ours(obj, item, cluster):
    """Vrai si l'objet du cluster enfant porte encore les trois étiquettes."""
    labels = ((obj or {}).get("metadata") or {}).get("labels") or {}
    return all(labels.get(k) == v for k, v in marks(item, cluster).items())


def docs(ns, cluster, item, package, version, objs, origin=None, by=""):
    """[ConfigMap, ClusterResourceSet] d'un élément appliqué à `<ns>/<cluster>`."""
    labels = {MANAGED: "true", L_ITEM: item, L_PACKAGE: package, L_CLUSTER: cluster}
    annotations = {A_VERSION: str(version), A_BY: by or ""}
    for k, label in cc.ORIGIN_LABELS.items():
        if (origin or {}).get(k):
            labels[label] = origin[k]
    for k, ann in cc.ORIGIN_ANNOTATIONS.items():
        if (origin or {}).get(k):
            annotations[ann] = origin[k]
    name = name_of(item)

    def meta():
        return {"name": name, "namespace": ns, "labels": dict(labels), "annotations": dict(annotations)}
    return [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(),
         "data": data_of(stamp(objs, item, cluster))},
        {"apiVersion": CRS_API, "kind": "ClusterResourceSet", "metadata": meta(),
         "spec": {"clusterSelector": {"matchLabels": {label_of(item): "on"}},
                  "resources": [{"kind": "ConfigMap", "name": name}],
                  "strategy": "Reconcile"}},
    ]


# ---------------------------------------------------------------------------
# Retrait
# ---------------------------------------------------------------------------
def removed(old_objs, new_objs):
    """Références des objets d'avant absents du nouveau contenu (même
    groupe, sorte, namespace et nom)."""
    keep = {_key(ref_of(o)) for o in new_objs}
    out = []
    for o in old_objs:
        ref = ref_of(o)
        if _key(ref) not in keep and ref not in out:
            out.append(ref)
    return out


def delete_order(refs):
    """Namespacés d'abord, puis portée cluster, puis les Namespace : un
    namespace supprimé emporterait les objets qu'on veut juger un par un."""
    def rank(ref):
        if ref[1] == "Namespace":
            return 2
        return 1 if not ref[2] else 0
    return sorted(refs, key=rank)


def resource_of(ref):
    """Nom de ressource pour kubectl : `Sorte.version.groupe` (sorte seule
    pour le groupe principal)."""
    api, kind = ref[0], ref[1]
    g, sep, v = api.rpartition("/")
    return f"{kind}.{v}.{g}" if sep else kind


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------
def content_hash(cm):
    """Empreinte d'un ConfigMap comme la calcule Cluster API pour le
    binding : sha256 des valeurs, clés triées (exp/addons/internal/controllers,
    `computeHash` et `normalizeData`, CAPI v1.13)."""
    h = hashlib.sha256()
    data = (cm or {}).get("data") or {}
    for k in sorted(data):
        h.update(str(data[k]).encode())
    return "sha256:" + h.hexdigest()


def _binding_entries(binding, crs_name):
    spec = (binding or {}).get("spec") or {}
    for b in spec.get("bindings") or []:
        if b.get("clusterResourceSetName") == crs_name:
            return b.get("resources") or []
    return None


def state(crs, bindings, cm):
    """État d'un élément appliqué : {"applied", "hash", "clusters":
    [{"cluster", "applied", "hash"}], "message"}. `applied` : chaque binding
    qui cite le CRS porte l'empreinte du ConfigMap actuel."""
    name = ((crs or cm or {}).get("metadata") or {}).get("name") or ""
    want = content_hash(cm) if cm else None
    clusters = []
    for b in bindings or []:
        entries = _binding_entries(b, name)
        if entries is None:
            continue
        cluster = ((b.get("spec") or {}).get("clusterName")
                   or (b.get("metadata") or {}).get("name") or "")
        e = next((x for x in entries if x.get("kind") == "ConfigMap" and x.get("name") == name), {})
        clusters.append({"cluster": cluster, "applied": bool(e.get("applied")) and e.get("hash") == want,
                         "hash": e.get("hash")})
    if crs is None:
        msg = "no ClusterResourceSet"
    elif cm is None:
        msg = "no ConfigMap"
    elif not clusters:
        msg = "not applied to any cluster yet"
    elif all(c["applied"] for c in clusters):
        msg = "applied"
    else:
        msg = "being applied"
    return {"applied": bool(crs and cm and clusters and all(c["applied"] for c in clusters)),
            "hash": want, "clusters": clusters, "message": msg}


def item_name_ok(item):
    """Nom applicable : celui d'un service (`capi_services.NAME_RE`, 42
    caractères au plus)."""
    return bool(sv.NAME_RE.match(item or ""))
