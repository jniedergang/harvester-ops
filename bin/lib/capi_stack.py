"""harvester-ops : la pile Cluster API d'un cluster Harvester (v1.48.0).

Harvester 1.9 embarque Rancher Turtles, qui installe lui-même le cœur Cluster
API (`cattle-capi-system`) et gère les certificats de webhook sans
cert-manager. La console ne fait donc que déclarer les fournisseurs
manquants (RKE2 amorçage et plan de contrôle, CAPHV) en objets
`CAPIProvider`, leurs composants venant de ConfigMaps qu'elle crée depuis son
paquet : rien ne descend d'Internet.

Ce module lit l'état de la pile, prépare l'installation, applique les
contournements connus des versions livrées, et sait retirer une ancienne
installation (d'avant Turtles) sans toucher à ce que Turtles a repris.

Voir docs/design/2026-09-25-creation-cluster-capi.md.
"""

import base64
import gzip
import json
import re
import time
from pathlib import Path

from kube import KubeError

K_CAPIPROVIDER = "capiproviders.turtles-capi.cattle.io"
K_CLUSTER = "clusters.cluster.x-k8s.io"
K_CLUSTERCLASS = "clusterclasses.cluster.x-k8s.io"
K_CRD = "customresourcedefinitions.apiextensions.k8s.io"

MANAGED = "harvester-ops.io/managed"
SHIM_LABEL = "harvester-ops.io/shim"

# Espaces d'une installation faite par la console avant Turtles.
LEGACY_NAMESPACES = ("capi-system", "capi-kubeadm-bootstrap-system",
                     "capi-kubeadm-control-plane-system", "rke2-bootstrap-system",
                     "rke2-control-plane-system", "caphv-system")
PROVIDER_GROUPS = ("bootstrap.cluster.x-k8s.io", "controlplane.cluster.x-k8s.io",
                   "infrastructure.cluster.x-k8s.io")
TURTLES_OWNED = "objectset.rio.cattle.io/"

# Une ConfigMap ne dépasse pas 1 Mio : au-delà, composants compressés.
CM_LIMIT = 900 * 1024


# ---------------------------------------------------------------------------
# Description des fournisseurs livrés (turtles.json du paquet)
# ---------------------------------------------------------------------------

def load_components(root):
    """Le fichier `turtles.json` et les composants d'un paquet extrait :
    {"providers": [...], "kubernetes_versions": [...], ...}. Chaque
    fournisseur porte ses fichiers `components` et `metadata`."""
    root = Path(root)
    desc = json.loads((root / "turtles.json").read_text())
    for p in desc.get("providers") or []:
        d = root / "turtles" / p["key"]
        p["components_path"] = str(d / "components.yaml")
        p["metadata_path"] = str(d / "metadata.yaml")
        p["image_files"] = [str(root / f) for f in p.get("image_files") or []]
    return desc


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------

def _labels(o):
    return (o.get("metadata") or {}).get("labels") or {}


def _is_turtles_owned(o):
    ann = (o.get("metadata") or {}).get("annotations") or {}
    return any(k.startswith(TURTLES_OWNED) for k in ann)


def provider_state(p):
    st = p.get("status") or {}
    spec = p.get("spec") or {}
    conds = {c.get("type"): c.get("status") for c in st.get("conditions") or []}
    return {"name": spec.get("name") or st.get("name"), "type": spec.get("type"),
            "version": st.get("installedVersion") or spec.get("version"),
            "namespace": (p.get("metadata") or {}).get("namespace"),
            "phase": st.get("phase") or "Pending",
            "ready": st.get("phase") == "Ready" and conds.get("Ready") in ("True", None),
            "managed_by_console": _labels(p).get(MANAGED) == "true"}


def stack_status(kube, wanted=()):
    """État de la pile sur le cluster lu par `kube`.

    `wanted` : les fournisseurs attendus ({"name","type","version"}), tirés du
    paquet ; un fournisseur présent dans une autre version est signalé."""
    out = {"turtles": False, "core": None, "providers": [], "missing": [],
           "legacy": False, "legacy_namespaces": [], "shim": {"needed": False, "present": False},
           "ready": False, "harvester_version": None}
    try:
        setting = kube.get("settings.harvesterhci.io", None, "server-version") or {}
        out["harvester_version"] = setting.get("value")
    except KubeError:
        pass
    try:
        providers = kube.list(K_CAPIPROVIDER)
        out["turtles"] = True
    except KubeError:
        providers = []
    states = [provider_state(p) for p in providers]
    out["core"] = next((s for s in states if s["type"] == "core"), None)
    out["providers"] = [s for s in states if s["type"] != "core"]
    for w in wanted or []:
        got = next((s for s in out["providers"]
                    if s["name"] == w["name"] and s["type"] == w["type"]), None)
        if got is None or not got["ready"]:
            out["missing"].append(f"{w['type']}/{w['name']}")
        elif w.get("version") and got["version"] != w["version"]:
            out["missing"].append(f"{w['type']}/{w['name']} {w['version']} (installed {got['version']})")
    if not out["turtles"]:
        out["missing"].insert(0, "turtles")
    elif not (out["core"] and out["core"]["ready"]):
        out["missing"].insert(0, "core/cluster-api")
    managed_ns = {s["namespace"] for s in states}
    try:
        names = {n["metadata"]["name"] for n in kube.list("namespaces")}
    except KubeError:
        names = set()
    out["legacy_namespaces"] = [ns for ns in LEGACY_NAMESPACES if ns in names and ns not in managed_ns]
    out["legacy"] = bool(out["legacy_namespaces"])
    shim = shim_state(kube, wanted)
    out["shim"] = shim
    if shim["needed"] and not shim["present"]:
        out["missing"].append("compatibility/ingress-expose")
    out["contract"] = contract_state(kube, wanted)
    if out["contract"]:
        out["missing"].append("compatibility/contract-label")
    out["ready"] = not out["missing"]
    return out


# ---------------------------------------------------------------------------
# Contournement : l'étiquette de contrat que retire le correctif du fournisseur
# ---------------------------------------------------------------------------

def label_removals(wanted):
    """Les étiquettes que les correctifs (`patches`) du paquet retirent d'une
    CRD : [(crd, étiquette)]."""
    out = []
    for w in wanted or []:
        for p in w.get("patches") or []:
            t = p.get("target") or {}
            if t.get("kind") != "CustomResourceDefinition" or not t.get("name"):
                continue
            for m in re.finditer(r"op:\s*remove\s+path:\s*/metadata/labels/(\S+)", p.get("patch") or ""):
                out.append((t["name"], m.group(1).replace("~1", "/").replace("~0", "~")))
    return out


def contract_state(kube, wanted=()):
    """Turtles applique les composants corrigés, mais son application ne
    retire pas une étiquette déjà posée sur la CRD : vu sur harv1, une
    réinstallation par-dessus une CRD qui l'avait (fournisseur déclaré un
    temps sans le correctif) la laissait, et les machines seraient restées
    « Provisioning ». Rend les étiquettes encore présentes."""
    left = []
    for crd, label in label_removals(wanted):
        try:
            obj = kube.get(K_CRD, None, crd)
        except KubeError:
            obj = None
        if obj is not None and label in _labels(obj):
            left.append({"crd": crd, "label": label})
    return left


# ---------------------------------------------------------------------------
# Contournement : l'adresse de Harvester que lit CAPHV <= v0.10.1
# ---------------------------------------------------------------------------

SHIM_NS, SHIM_NAME = "kube-system", "ingress-expose"


def shim_state(kube, wanted=()):
    """CAPHV <= v0.10.1 lit l'adresse de Harvester dans l'annotation
    `kube-vip.io/loadbalancerIPs` du service `kube-system/ingress-expose`.
    Harvester 1.9 a retiré ce service (l'adresse est portée par
    `kube-system/rke2-traefik`) : sans lui, CAPHV échoue en boucle et le
    répartiteur de l'API du cluster créé ne reçoit jamais sa machine (vu sur
    harv1 le 25/09/2026)."""
    needed_by = [w for w in wanted or [] if "ingress-expose" in (w.get("workarounds") or [])]
    svc = kube.get("services", SHIM_NS, SHIM_NAME)
    vip = None
    if svc is None:
        traefik = kube.get("services", SHIM_NS, "rke2-traefik") or {}
        ing = ((traefik.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        vip = ing[0].get("ip") if ing else None
    present = svc is not None
    ours = present and _labels(svc).get(SHIM_LABEL) == "caphv-ingress-expose"
    return {"needed": bool(needed_by) and (not present or ours),
            "present": present, "ours": ours, "vip": vip}


def shim_manifest(vip):
    return {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {
            "name": SHIM_NAME, "namespace": SHIM_NS,
            "labels": {SHIM_LABEL: "caphv-ingress-expose", MANAGED: "true"},
            "annotations": {
                "kube-vip.io/loadbalancerIPs": vip,
                "harvester-ops.io/why": ("CAPHV <= v0.10.1 reads the Harvester VIP here; "
                                         "Harvester 1.9 moved it to rke2-traefik"),
            },
        },
        "spec": {"type": "ClusterIP", "clusterIP": "None",
                 "ports": [{"name": "https", "port": 443}]},
    }


# ---------------------------------------------------------------------------
# Installation par Turtles
# ---------------------------------------------------------------------------

def configmap_manifest(p):
    """Les composants d'un fournisseur, lus par Turtles (`fetchConfig.selector`)."""
    components = Path(p["components_path"]).read_bytes()
    metadata = Path(p["metadata_path"]).read_text()
    cm = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": p["version"], "namespace": p["namespace"],
                     "labels": {"provider.cluster.x-k8s.io/name": p["name"],
                                "provider.cluster.x-k8s.io/type": p["type"],
                                "provider.cluster.x-k8s.io/version": p["version"],
                                MANAGED: "true"}},
        "data": {"metadata": metadata},
    }
    if len(components) > CM_LIMIT:
        cm["metadata"]["annotations"] = {"provider.cluster.x-k8s.io/compressed": "true"}
        cm["binaryData"] = {"components": base64.b64encode(gzip.compress(components)).decode()}
    else:
        cm["data"]["components"] = components.decode()
    return cm


def provider_manifest(p):
    spec = {"name": p["name"], "type": p["type"], "version": p["version"],
            "fetchConfig": {"selector": {"matchLabels": {
                "provider.cluster.x-k8s.io/name": p["name"],
                "provider.cluster.x-k8s.io/type": p["type"]}}}}
    if p.get("patches"):
        spec["patches"] = p["patches"]
    return {"apiVersion": "turtles-capi.cattle.io/v1alpha1", "kind": "CAPIProvider",
            "metadata": {"name": p["key"], "namespace": p["namespace"],
                         "labels": {MANAGED: "true"}},
            "spec": spec}


def install(kube, desc, step, wait=True, timeout=300, now=time.time, sleep=time.sleep):
    """Déclare les fournisseurs du paquet et attend qu'ils soient prêts.
    Idempotent : relancé sur une pile prête, il ne change rien."""
    providers = desc.get("providers") or []
    status = stack_status(kube, providers)
    if not status["turtles"] or not (status["core"] and status["core"]["ready"]):
        raise RuntimeError("Rancher Turtles and its Cluster API core are required "
                           "(Harvester 1.9 or later); none is ready on this cluster")
    for p in providers:
        step("providers", "running", f"{p['type']}/{p['name']} {p['version']}")
        kube.apply([{"apiVersion": "v1", "kind": "Namespace",
                     "metadata": {"name": p["namespace"]}}])
        kube.apply([configmap_manifest(p)])
        kube.apply([provider_manifest(p)])
    if wait:
        deadline = now() + timeout
        pending = {p["key"] for p in providers}
        while pending:
            for p in providers:
                if p["key"] not in pending:
                    continue
                obj = kube.get(K_CAPIPROVIDER, p["namespace"], p["key"]) or {}
                st = provider_state(obj)
                if st["ready"] and st["version"] == p["version"] and _available(kube, p["namespace"]):
                    pending.discard(p["key"])
                    step("providers", "running", f"{p['type']}/{p['name']} {p['version']} ready")
            if pending and now() > deadline:
                raise RuntimeError("providers not ready in time: " + ", ".join(sorted(pending)))
            if pending:
                sleep(5)
    shim = shim_state(kube, providers)
    if shim["needed"] and not shim["present"]:
        if not shim["vip"]:
            raise RuntimeError("cannot find the Harvester VIP (service kube-system/rke2-traefik)")
        kube.apply([shim_manifest(shim["vip"])])
        step("compatibility", "done", f"ingress-expose recreated for CAPHV (VIP {shim['vip']})")
    for left in contract_state(kube, providers):
        kube.patch(K_CRD, None, left["crd"], {"metadata": {"labels": {left["label"]: None}}})
        step("compatibility", "done", f"label {left['label']} removed from {left['crd']}")
    step("providers", "done", "Cluster API providers ready")
    return stack_status(kube, providers)


def _available(kube, ns):
    try:
        deps = kube.list("deployments", ns)
    except KubeError:
        return False
    if not deps:
        return False
    for d in deps:
        st = d.get("status") or {}
        want = (d.get("spec") or {}).get("replicas", 1)
        if (st.get("availableReplicas") or 0) < want:
            return False
    return True


# ---------------------------------------------------------------------------
# Retrait d'une installation d'avant Turtles
# ---------------------------------------------------------------------------

def legacy_plan(kube):
    """Ce qu'une ancienne installation a laissé, dans l'ordre de retrait.

    Jamais : une CRD reprise par Turtles (annotations objectset), le cœur de
    Turtles, un espace qui porte un `CAPIProvider`. Refuse (lève) s'il existe
    un cluster Cluster API autre que `fleet-local/local` : ses objets
    dépendent peut-être de ces fournisseurs."""
    status = stack_status(kube)
    legacy_ns = set(status["legacy_namespaces"])
    if not legacy_ns:
        return []
    clusters = [f"{c['metadata']['namespace']}/{c['metadata']['name']}"
                for c in kube.list(K_CLUSTER)]
    others = [c for c in clusters if c != "fleet-local/local"]
    if others:
        raise RuntimeError("clusters still exist: " + ", ".join(sorted(others)))
    plan = []
    # ClusterClass dont les gabarits relèvent des anciens fournisseurs
    for cc in kube.list(K_CLUSTERCLASS):
        md = cc["metadata"]
        if _labels(cc).get(MANAGED) != "true":
            plan.append((K_CLUSTERCLASS, md["namespace"], md["name"]))
    for kind in ("validatingwebhookconfigurations", "mutatingwebhookconfigurations"):
        for w in kube.list(kind):
            svc_ns = {((h.get("clientConfig") or {}).get("service") or {}).get("namespace")
                      for h in w.get("webhooks") or []}
            if svc_ns & legacy_ns:
                plan.append((kind, None, w["metadata"]["name"]))
    for ns in sorted(legacy_ns):
        plan.append(("namespaces", None, ns))
    for crd in kube.list(K_CRD):
        name = crd["metadata"]["name"]
        if not name.endswith(PROVIDER_GROUPS) or _is_turtles_owned(crd):
            continue
        svc = ((((crd.get("spec") or {}).get("conversion") or {}).get("webhook") or {})
               .get("clientConfig") or {}).get("service") or {}
        if svc.get("namespace") in legacy_ns or not svc:
            plan.append((K_CRD, None, name))
    bindings = kube.list("clusterrolebindings")
    roles_used = {}
    for b in bindings:
        roles_used.setdefault(b["roleRef"]["name"], []).append(b)
    for b in bindings:
        subs = [s for s in b.get("subjects") or [] if s.get("kind") == "ServiceAccount"]
        if subs and all(s.get("namespace") in legacy_ns for s in subs):
            plan.append(("clusterrolebindings", None, b["metadata"]["name"]))
            role = b["roleRef"]["name"]
            if all(x is b for x in roles_used.get(role, [])):
                plan.append(("clusterroles", None, role))
    return plan


def cleanup_legacy(kube, step, dry_run=False):
    plan = legacy_plan(kube)
    if not plan:
        step("legacy", "done", "no legacy installation")
        return plan
    for kind, ns, name in plan:
        label = f"{kind.split('.')[0]} {ns + '/' if ns else ''}{name}"
        if dry_run:
            step("legacy", "running", f"would delete {label}")
            continue
        try:
            kube.delete(kind, ns, name)
            step("legacy", "running", f"deleted {label}")
        except KubeError as e:
            if "NotFound" not in str(e) and "not found" not in str(e):
                raise
    step("legacy", "done", f"{len(plan)} legacy object(s) {'to remove' if dry_run else 'removed'}")
    return plan
