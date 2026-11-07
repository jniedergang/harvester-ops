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

from capi_cluster import K_HARVESTERCLUSTER
from capi_services import K_HCP
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
    out["optional"] = []
    out["differs"] = []
    for w in wanted or []:
        got = next((s for s in out["providers"]
                    if s["name"] == w["name"] and s["type"] == w["type"]), None)
        gap = None
        if got is None or not got["ready"]:
            gap = f"{w['type']}/{w['name']}"
        elif w.get("version") and got["version"] != w["version"]:
            gap = f"{w['type']}/{w['name']} {w['version']} (installed {got['version']})"
            # 2.5.0 (décision 171) : une autre version que le paquet, dite à
            # part ; un plan ne la met jamais à niveau en silence
            out["differs"].append({"provider": f"{w['type']}/{w['name']}",
                                   "installed": got["version"], "bundle": w["version"]})
        if w.get("optional"):
            # v1.52.0 : un fournisseur facultatif (CAAPH, les services) ne
            # bloque pas la création de clusters ; il est dit à part.
            out["optional"].append({"provider": f"{w['type']}/{w['name']}", "version": w.get("version"),
                                    "ready": gap is None, "gap": gap})
        elif gap:
            out["missing"].append(gap)
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


VIP_ANNOTATION = "kube-vip.io/loadbalancerIPs"


def harvester_vip(kube):
    """L'adresse de Harvester (VIP), ou None. Une installation neuve de 1.9
    la porte dans le statut de `kube-system/rke2-traefik` ; un cluster mis à
    jour depuis 1.8 n'a pas ce service et garde `kube-system/ingress-expose`
    (statut et annotation kube-vip : vu sur harvlab2 le 05/11/2026). Le
    service de remplacement de la console n'a que l'annotation."""
    traefik = kube.get("services", SHIM_NS, "rke2-traefik") or {}
    ing = ((traefik.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    if ing and _usable(ing[0].get("ip")):
        return ing[0]["ip"]
    expose = kube.get("services", SHIM_NS, SHIM_NAME) or {}
    ing = ((expose.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    if ing and _usable(ing[0].get("ip")):
        return ing[0]["ip"]
    ann = ((expose.get("metadata") or {}).get("annotations") or {}).get(VIP_ANNOTATION) or ""
    first = ann.split(",")[0].strip()
    return first if _usable(first) else None


def _usable(ip):
    """Une VIP en DHCP porte `0.0.0.0` dans l'annotation kube-vip tant
    qu'elle n'a pas de bail : ce n'est pas une adresse joignable."""
    return bool(ip) and ip not in ("0.0.0.0", "::")


def shim_state(kube, wanted=()):
    """CAPHV <= v0.10.1 lit l'adresse de Harvester dans l'annotation
    `kube-vip.io/loadbalancerIPs` du service `kube-system/ingress-expose`.
    Harvester 1.9 a retiré ce service (l'adresse est portée par
    `kube-system/rke2-traefik`) : sans lui, CAPHV échoue en boucle et le
    répartiteur de l'API du cluster créé ne reçoit jamais sa machine (vu sur
    harv1 le 25/09/2026)."""
    needed_by = [w for w in wanted or [] if "ingress-expose" in (w.get("workarounds") or [])]
    svc = kube.get("services", SHIM_NS, SHIM_NAME)
    vip = harvester_vip(kube) if svc is None else None
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
            raise RuntimeError("cannot find the Harvester VIP (services kube-system/rke2-traefik "
                               "and ingress-expose have no address)")
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


# ---------------------------------------------------------------------------
# Retrait de la pile Turtles (2.7.0, décisions 178 et 215)
# ---------------------------------------------------------------------------

# Ordre de retrait : les services (CAAPH) avant l'infrastructure (CAPHV),
# puis le plan de contrôle et l'amorçage RKE2. Un fournisseur étiqueté d'un
# autre genre vient après, dans l'ordre de la liste.
UNINSTALL_ORDER = (("addon", "helm"), ("infrastructure", "harvester"),
                   ("controlPlane", "rke2"), ("bootstrap", "rke2"))
CAAPH = ("addon", "helm")
CAPHV = ("infrastructure", "harvester")
# Le cluster local de Rancher, que legacy_plan écarte déjà : il ne relève
# pas des fournisseurs de la console.
RANCHER_LOCAL = "fleet-local/local"


def _finding(code, level="block", **facts):
    return {"code": code, "level": level, "facts": facts}


def _rank(p):
    key = (p["type"], p["name"])
    return UNINSTALL_ORDER.index(key) if key in UNINSTALL_ORDER else len(UNINSTALL_ORDER)


def _deployment_images(kube, ns):
    out = []
    for d in kube.list("deployments", ns):
        for c in (((d.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []:
            if c.get("image") and c["image"] not in out:
                out.append(c["image"])
    return out


def uninstall_plan(kube, running=None):
    """Ce que le retrait de la pile Turtles supprimerait, et ses refus.

    Seuls les `CAPIProvider` étiquetés `harvester-ops.io/managed: "true"`
    (posés par `provider_manifest`) partent ; jamais le cœur ni Turtles,
    qui sont à Harvester. Turtles ne refuse aucune suppression (essai 6 de
    la 2.7.0, même celle du cœur avec un cluster enfant présent) : la
    console refuse elle-même. `running` : {"run", "plan"} d'un déroulé qui
    tient ce cluster, ou None. Rend {"providers", "configmaps", "shim",
    "images", "refusals", "kept", "blocked"} ; les CRD et les namespaces
    restent toujours."""
    objs = kube.list(K_CAPIPROVIDER, missing_ok=False)     # sans Turtles : KubeError
    providers, kept = [], [{"provider": "turtles", "namespace": None, "reason": "turtles"}]
    for o in objs:
        st = provider_state(o)
        ref = {"key": o["metadata"]["name"], "namespace": st["namespace"], "type": st["type"],
               "name": st["name"], "version": st["version"]}
        if st["type"] == "core":
            kept.append({"provider": f"core/{st['name']}", "namespace": st["namespace"], "reason": "core"})
        elif not st["managed_by_console"]:
            kept.append({"provider": f"{st['type']}/{st['name']}", "namespace": st["namespace"],
                         "reason": "not-managed"})
        else:
            providers.append(ref)
    providers.sort(key=_rank)
    refusals = []
    clusters = sorted(f"{(c.get('metadata') or {}).get('namespace')}/{c['metadata']['name']}"
                      for c in kube.list(K_CLUSTER))
    clusters = [c for c in clusters if c != RANCHER_LOCAL]
    if clusters:
        refusals.append(_finding("capi-clusters-present", clusters=", ".join(clusters[:20]),
                                 count=len(clusters)))
    if any((p["type"], p["name"]) == CAPHV for p in providers):
        # revue finale 2.7.0 (m3 i) : un HarvesterCluster resté sans son
        # Cluster garde le finaliseur de CAPHV ; sans CAPHV, plus personne
        # ne le lève (les Clusters présents sont déjà refusés plus haut)
        hcs = sorted(f"{h['metadata'].get('namespace')}/{h['metadata']['name']}"
                     for h in kube.list(K_HARVESTERCLUSTER))
        hcs = [h for h in hcs if h not in clusters]
        if hcs:
            refusals.append(_finding("capi-harvesterclusters-present", clusters=", ".join(hcs[:20]),
                                     count=len(hcs)))
    if any((p["type"], p["name"]) == CAAPH for p in providers):
        hcps = sorted(f"{h['metadata'].get('namespace')}/{h['metadata']['name']}" for h in kube.list(K_HCP))
        if hcps:
            refusals.append(_finding("caaph-services-present", services=", ".join(hcps[:20]), count=len(hcps)))
    if running:
        refusals.append(_finding("capi-plan-running", run=running.get("run") or "?",
                                 plan=running.get("plan") or "?"))
    namespaces = list(dict.fromkeys(p["namespace"] for p in providers))
    configmaps = [f"{ns}/{cm['metadata']['name']}" for ns in namespaces
                  for cm in kube.list("configmaps", ns, selector=f"{MANAGED}=true")]
    images = []
    for ns in namespaces:
        images += [i for i in _deployment_images(kube, ns) if i not in images]
    removed = {(p["type"], p["name"]) for p in providers}
    caphv_left = any(st["type"] == CAPHV[0] and st["name"] == CAPHV[1] and (st["type"], st["name"]) not in removed
                     for st in (provider_state(o) for o in objs))
    svc = kube.get("services", SHIM_NS, SHIM_NAME)
    shim = bool(svc) and _labels(svc).get(SHIM_LABEL) == "caphv-ingress-expose" and not caphv_left
    return {"providers": providers, "configmaps": configmaps, "shim": shim, "images": images,
            "refusals": refusals, "kept": kept, "blocked": bool(refusals)}


def _gone(e):
    return "NotFound" in str(e) or "not found" in str(e)


def uninstall(kube, step, dry_run=False, timeout=600, running=None, now=time.time, sleep=time.sleep):
    """Retire la pile Turtles posée par la console, dans l'ordre de
    UNINSTALL_ORDER : chaque `CAPIProvider` supprimé, puis attente de la
    disparition de ses déploiements (`timeout` secondes par fournisseur ;
    au-delà RuntimeError, les suivants ne sont pas touchés), puis les
    ConfigMaps du paquet (étiquette MANAGED) et le service de compatibilité
    (SHIM_LABEL). Les CRD ne sont JAMAIS supprimées : des objets d'ailleurs
    peuvent encore les employer. Rend le plan (`uninstall_plan`) ; un plan
    refusé n'a rien supprimé."""
    plan = uninstall_plan(kube, running)
    if plan["blocked"]:
        step("uninstall", "error", "refused: " + "; ".join(
            f"{f['code']} ({', '.join(f'{k}={v}' for k, v in f['facts'].items())})" for f in plan["refusals"]))
        return plan
    verb = "would delete" if dry_run else "deleted"
    for k in plan["kept"]:
        step("uninstall", "running", f"kept: {k['provider']} ({k['reason']})")
    for p in plan["providers"]:
        label = f"{p['type']}/{p['name']} ({p['namespace']}/{p['key']})"
        if dry_run:
            step("providers", "running", f"{verb} {label}")
            continue
        try:
            kube.delete(K_CAPIPROVIDER, p["namespace"], p["key"])
        except KubeError as e:
            if not _gone(e):
                raise
        step("providers", "running", f"{verb} {label}, waiting for its deployments")
        deadline = now() + timeout
        while True:
            left = [d["metadata"]["name"] for d in kube.list("deployments", p["namespace"])]
            obj = kube.get(K_CAPIPROVIDER, p["namespace"], p["key"])
            if not left and obj is None:
                break
            if now() > deadline:
                raise RuntimeError(f"{label} still has deployment(s) {', '.join(left) or '-'} after "
                                   f"{timeout} s; the next providers are untouched")
            sleep(5)
        step("providers", "running", f"{label} removed")
    for ref in plan["configmaps"]:
        ns, _, name = ref.partition("/")
        if not dry_run:
            try:
                kube.delete("configmaps", ns, name)
            except KubeError as e:
                if not _gone(e):
                    raise
        step("configmaps", "running", f"{verb} configmap {ref}")
    if plan["shim"]:
        if not dry_run:
            try:
                kube.delete("services", SHIM_NS, SHIM_NAME)
            except KubeError as e:
                if not _gone(e):
                    raise
        step("compatibility", "running", f"{verb} service {SHIM_NS}/{SHIM_NAME}")
    step("uninstall", "done", f"{len(plan['providers'])} provider(s) {'to remove' if dry_run else 'removed'}; "
                              "CRDs and namespaces kept")
    return plan
