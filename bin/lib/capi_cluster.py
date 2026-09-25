"""harvester-ops : créer un cluster RKE2 sur Harvester par Cluster API (v1.48.0).

Décisions pures, sans accès au cluster : ce que l'exploitant peut
demander (liste blanche), comment cela devient des options du générateur
`caphv-generate` (livré dans `bin/`, tiré du dépôt CAPHV), ce qu'on corrige
dans ce qu'il produit, les calculs d'adresses à partir d'un IPPool
Harvester, le contrôle préalable, et la lecture de la progression d'un
cluster en v1beta1 comme en v1beta2.

Voir docs/design/2026-09-25-creation-cluster-capi.md.

Bibliothèque standard seulement : le script tourne sur un hôte airgap.
"""

import copy
import ipaddress
import re

# ---------------------------------------------------------------------------
# Ce que l'exploitant peut demander
# ---------------------------------------------------------------------------

NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{0,38}[a-z0-9])?$")
K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
NS_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?/[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
# Une image se désigne par son nom affiché, libre (`x86_64`, majuscules) :
# c'est ce que le générateur attend.
IMAGE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?/[^/\s]{1,253}$")
QUANTITY_RE = re.compile(r"^[1-9][0-9]{0,5}(Mi|Gi|Ti)$")
VERSION_RE = re.compile(r"^v1\.[0-9]{2}\.[0-9]{1,3}$")

CNIS = ("calico", "canal", "cilium", "none")
ENCAPSULATIONS = ("VXLAN", "VXLANCrossSubnet", "IPIP", "IPIPCrossSubnet", "None")
BGP_MODES = ("Enabled", "Disabled")

# Espaces de noms à ne jamais prendre : ceux de Harvester, de Rancher, des
# fournisseurs Cluster API. Le générateur y écrirait une ClusterClass.
RESERVED_NAMESPACES = {
    "default", "kube-system", "kube-public", "kube-node-lease", "longhorn-system",
    "harvester-system", "harvester-public", "cattle-system", "cattle-capi-system",
    "cattle-turtles-system", "cattle-fleet-system", "cattle-fleet-local-system",
    "fleet-default", "fleet-local", "caphv-system", "rke2-bootstrap-system",
    "rke2-control-plane-system", "capi-system", "cert-manager",
}

# Valeurs par défaut, pour un cluster de test minimal ; le formulaire les
# remplace par ce que le cluster propose vraiment.
DEFAULTS = {
    "cp_replicas": 1, "worker_replicas": 1, "cpu": 2, "memory": "4Gi",
    "disk_size": "40Gi", "ssh_user": "sles", "cni": "calico",
    "pod_cidr": "10.42.0.0/16", "service_cidr": "10.43.0.0/16",
    "cni_mtu": 1500, "cni_encapsulation": "VXLANCrossSubnet", "cni_bgp": "Disabled",
    "fleet_branch": "main", "target_namespace": "default", "rancher_import": False,
}

# Option du formulaire -> option du générateur. Tout le reste est refusé :
# le générateur accepte `--apply`, qui appliquerait avec le kubeconfig
# ambiant du service.
FLAGS = {
    "name": "--name", "namespace": "--namespace", "k8s_version": "--k8s-version",
    "cp_replicas": "--cp-replicas", "worker_replicas": "--worker-replicas",
    "cpu": "--cpu", "memory": "--memory", "disk_size": "--disk-size",
    "image": "--image", "ssh_user": "--ssh-user", "ssh_keypair": "--ssh-keypair",
    "network": "--network", "gateway": "--gateway", "subnet_mask": "--subnet-mask",
    "ip_pool": "--ip-pool", "cni": "--cni", "pod_cidr": "--pod-cidr",
    "cni_mtu": "--cni-mtu", "cni_encapsulation": "--cni-encapsulation",
    "cni_bgp": "--cni-bgp", "fleet_repo": "--fleet-addon-repo",
    "fleet_branch": "--fleet-addon-branch",
}
LIST_FLAGS = {"dns": "--dns", "ip_pool_refs": "--ip-pool-refs"}
# Options traitées par la console elle-même, après le générateur.
CONSOLE_OPTIONS = {"extra_disk_size", "extra_disk_class", "target_namespace",
                   "rancher_import", "extra_networks", "service_cidr"}
KNOWN = set(FLAGS) | set(LIST_FLAGS) | CONSOLE_OPTIONS


def _as_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def normalize(raw):
    """La demande nettoyée, les valeurs par défaut posées. Lève ValueError
    sur une option inconnue (liste blanche)."""
    raw = dict(raw or {})
    unknown = sorted(k for k in raw if k not in KNOWN)
    if unknown:
        raise ValueError("unknown option(s): " + ", ".join(unknown))
    spec = dict(DEFAULTS)
    for k, v in raw.items():
        if v is None or v == "":
            continue
        spec[k] = v
    for k in ("cp_replicas", "worker_replicas", "cpu", "cni_mtu"):
        if k in spec:
            try:
                spec[k] = int(spec[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be an integer") from None
    spec["dns"] = _as_list(spec.get("dns"))
    spec["ip_pool_refs"] = _as_list(spec.get("ip_pool_refs"))
    spec["extra_networks"] = _as_list(spec.get("extra_networks"))
    spec["rancher_import"] = spec.get("rancher_import") in (True, "true", "1", 1, "on")
    if not spec.get("namespace") and spec.get("name"):
        spec["namespace"] = spec["name"]
    return spec


def validate(spec):
    """Les erreurs de forme, avant de lire le cluster : [(option, message)]."""
    errs = []

    def need(key, ok, msg):
        if not ok:
            errs.append((key, msg))

    name = spec.get("name") or ""
    need("name", bool(NAME_RE.match(name)),
         "lowercase letters, digits and '-', starting with a letter, 1 to 40 characters")
    ns = spec.get("namespace") or ""
    need("namespace", bool(K8S_NAME_RE.match(ns)), "not a valid namespace name")
    need("namespace", ns not in RESERVED_NAMESPACES,
         f"'{ns}' belongs to Harvester, Rancher or Cluster API")
    tns = spec.get("target_namespace") or ""
    need("target_namespace", bool(K8S_NAME_RE.match(tns)), "not a valid namespace name")
    v = spec.get("k8s_version") or ""
    need("k8s_version", bool(VERSION_RE.match(v)), "expected v1.MINOR.PATCH, e.g. v1.33.5")
    need("cp_replicas", 1 <= spec.get("cp_replicas", 0) <= 7, "between 1 and 7")
    need("worker_replicas", 0 <= spec.get("worker_replicas", -1) <= 50, "between 0 and 50")
    need("cpu", 1 <= spec.get("cpu", 0) <= 64, "between 1 and 64")
    for key in ("memory", "disk_size"):
        need(key, bool(QUANTITY_RE.match(str(spec.get(key) or ""))), "a size such as 8Gi")
    need("image", bool(IMAGE_RE.match(str(spec.get("image") or ""))),
         "expected namespace/display name")
    for key in ("ssh_keypair", "network"):
        need(key, bool(NS_NAME_RE.match(str(spec.get(key) or ""))), "expected namespace/name")
    for net in spec.get("extra_networks") or []:
        need("extra_networks", bool(NS_NAME_RE.match(net)), f"'{net}': expected namespace/name")
    need("ssh_user", bool(re.match(r"^[a-z_][a-z0-9_-]{0,31}$", spec.get("ssh_user") or "")),
         "a Linux user name")
    need("ip_pool", bool(K8S_NAME_RE.match(spec.get("ip_pool") or "")), "an IPPool name")
    for p in spec.get("ip_pool_refs") or []:
        need("ip_pool_refs", bool(K8S_NAME_RE.match(p)), f"'{p}': an IPPool name")
    for key in ("gateway",):
        need(key, _is_ipv4(spec.get(key)), "an IPv4 address")
    need("subnet_mask", _mask_prefix(spec.get("subnet_mask")) is not None,
         "a contiguous IPv4 mask such as 255.255.255.0")
    need("dns", bool(spec.get("dns")), "at least one DNS server (the provider refuses none)")
    for d in spec.get("dns") or []:
        need("dns", _is_ipv4(d), f"'{d}': an IPv4 address")
    need("cni", spec.get("cni") in CNIS, "calico, canal, cilium or none")
    for key in ("pod_cidr", "service_cidr"):
        need(key, _net(spec.get(key)) is not None, "a CIDR such as 10.42.0.0/16")
    need("cni_mtu", 576 <= spec.get("cni_mtu", 0) <= 9000, "between 576 and 9000")
    need("cni_encapsulation", spec.get("cni_encapsulation") in ENCAPSULATIONS,
         ", ".join(ENCAPSULATIONS))
    need("cni_bgp", spec.get("cni_bgp") in BGP_MODES, "Enabled or Disabled")
    repo = spec.get("fleet_repo")
    if repo:
        need("fleet_repo", bool(re.match(r"^(https?://|ssh://|git@)[^\s]+$", repo)),
             "a git URL (https, ssh)")
        need("fleet_branch", bool(re.match(r"^[A-Za-z0-9._/-]{1,100}$", spec.get("fleet_branch") or "")),
             "a branch name")
    size = spec.get("extra_disk_size")
    if size:
        need("extra_disk_size", bool(QUANTITY_RE.match(str(size))), "a size such as 20Gi")
        need("extra_disk_class", bool(K8S_NAME_RE.match(spec.get("extra_disk_class") or "")),
             "a storage class")
    return errs


def to_argv(spec, harvester_kubeconfig):
    """Les options du générateur, dans un ordre stable. Jamais `--apply`."""
    argv = []
    for key, flag in FLAGS.items():
        v = spec.get(key)
        if v is None or v == "":
            continue
        if key in ("fleet_branch",) and not spec.get("fleet_repo"):
            continue
        if key in ("cni_mtu", "cni_encapsulation", "cni_bgp") and not spec.get("fleet_repo"):
            # le générateur ne les applique qu'en mode Fleet
            continue
        argv += [flag, str(v)]
    for key, flag in LIST_FLAGS.items():
        if spec.get(key):
            argv += [flag, ",".join(spec[key])]
    if spec.get("extra_disk_size"):
        argv += ["--extra-disk", f"{spec['extra_disk_size']}:{spec['extra_disk_class']}"]
    argv += ["--harvester-kubeconfig", str(harvester_kubeconfig)]
    return argv


# ---------------------------------------------------------------------------
# Ce qu'on corrige dans ce que le générateur produit
# ---------------------------------------------------------------------------

HARVESTER_KINDS = {"HarvesterClusterTemplate", "HarvesterMachineTemplate",
                   "HarvesterCluster", "HarvesterMachine"}
AUTO_IMPORT = "cluster-api.cattle.io/rancher-auto-import"
# Posé sur chaque objet rendu : la suppression retrouve ce que la console a
# créé (le secret d'identité porte un kubeconfig du cluster Harvester).
GENERATED = "harvester-ops.io/generated"
GENERATED_KINDS = (
    "clusterclasses.cluster.x-k8s.io",
    "harvesterclustertemplates.infrastructure.cluster.x-k8s.io",
    "harvestermachinetemplates.infrastructure.cluster.x-k8s.io",
    "rke2controlplanetemplates.controlplane.cluster.x-k8s.io",
    "rke2configtemplates.bootstrap.cluster.x-k8s.io",
    "clusterresourcesets.addons.cluster.x-k8s.io",
    "machinehealthchecks.cluster.x-k8s.io",
    "configmaps", "secrets",
)
INFRA_OLD = "infrastructure.cluster.x-k8s.io/v1alpha1"
INFRA_NEW = "infrastructure.cluster.x-k8s.io/v1beta1"


def _retarget_refs(node):
    """Toute référence de gabarit `infrastructure.../v1alpha1` passe en
    v1beta1 (ClusterClass : `ref` / `templateRef`)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "apiVersion" and v == INFRA_OLD:
                node[k] = INFRA_NEW
            else:
                _retarget_refs(v)
    elif isinstance(node, list):
        for v in node:
            _retarget_refs(v)


def postprocess(docs, spec):
    """Corrige les documents produits par le générateur ; rend une copie.

    - **v1alpha1 -> v1beta1 pour les objets Harvester.** Le générateur écrit
      encore l'ancienne version ; CAPHV v0.10 la convertit en v1beta1 en y
      ajoutant `spec.template.metadata: {}`, que son propre schéma refuse
      ensuite quand la topologie recopie le gabarit (vu sur harv1).
    - **Pas d'import automatique dans Rancher**, sauf demande : sur Harvester,
      le Rancher intégré n'est pas fait pour gérer d'autres clusters.
    - **Espace de noms des VMs** (`targetNamespace`), réseaux
      supplémentaires : des variables de la ClusterClass que le générateur
      ne sait pas poser.
    - **CIDR des services** : le générateur ne le pose pas (RKE2 prend
      10.43.0.0/16).
    - **Étiquette `GENERATED`** sur tout ce qui n'est ni l'espace de noms ni
      le Cluster, pour que la suppression n'oublie rien.
    """
    out = []
    for d in docs:
        if not d:
            continue
        d = copy.deepcopy(d)
        _retarget_refs(d)
        if d.get("kind") not in ("Namespace", "Cluster"):
            meta = d.setdefault("metadata", {})
            meta["labels"] = dict(meta.get("labels") or {}, **{GENERATED: "true"})
        if d.get("kind") == "Cluster":
            labels = d.setdefault("metadata", {}).setdefault("labels", {})
            if spec.get("rancher_import"):
                labels[AUTO_IMPORT] = "true"
            else:
                labels.pop(AUTO_IMPORT, None)
            topo = d.get("spec", {}).get("topology", {})
            for var in topo.get("variables", []):
                if var.get("name") == "targetNamespace":
                    var["value"] = spec.get("target_namespace") or "default"
                elif var.get("name") == "vmNetworks" and spec.get("extra_networks"):
                    var["value"] = list(var.get("value") or []) + [
                        n for n in spec["extra_networks"] if n not in (var.get("value") or [])]
            svc = spec.get("service_cidr")
            if svc and svc != DEFAULTS["service_cidr"]:
                d["spec"].setdefault("clusterNetwork", {}).setdefault("services", {})[
                    "cidrBlocks"] = [svc]
        out.append(d)
    return out


def masked(docs):
    """Pour un aperçu : le secret d'identité porte le kubeconfig Harvester."""
    out = []
    for d in docs:
        d = copy.deepcopy(d)
        if d.get("kind") == "Secret":
            for field in ("data", "stringData"):
                if field in d:
                    d[field] = {k: "<hidden>" for k in d[field]}
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Adresses
# ---------------------------------------------------------------------------

def _is_ipv4(v):
    try:
        ipaddress.IPv4Address(str(v))
        return True
    except (ValueError, ipaddress.AddressValueError):
        return False


def _net(v):
    try:
        return ipaddress.IPv4Network(str(v), strict=False)
    except (ValueError, TypeError):
        return None


def _mask_prefix(mask):
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    except (ValueError, TypeError):
        return None


def mask_of(prefixlen):
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefixlen}").netmask)


def pool_info(pool):
    """Ce qu'un IPPool Harvester (loadbalancer.harvesterhci.io) dit d'utile
    au formulaire : sous-réseau, passerelle, masque, plages, places."""
    spec = (pool or {}).get("spec") or {}
    st = (pool or {}).get("status") or {}
    ranges = []
    size = 0
    for r in spec.get("ranges") or []:
        net = _net(r.get("subnet"))
        start = r.get("rangeStart") or (str(next(net.hosts())) if net else None)
        end = r.get("rangeEnd") or (str(net.broadcast_address - 1) if net else None)
        n = 0
        if start and end and _is_ipv4(start) and _is_ipv4(end):
            n = int(ipaddress.IPv4Address(end)) - int(ipaddress.IPv4Address(start)) + 1
        size += max(0, n)
        ranges.append({"subnet": r.get("subnet"), "gateway": r.get("gateway"),
                       "start": start, "end": end, "size": max(0, n)})
    first = ranges[0] if ranges else {}
    net = _net(first.get("subnet"))
    total = st.get("total") if isinstance(st.get("total"), int) else size
    # Le compteur « available » de Harvester dérive : vu à 24 sur un pool de
    # 16 adresses après quelques suppressions de clusters. La table des
    # adresses allouées fait foi (absente quand rien n'est alloué).
    available = max(0, total - len(st.get("allocated") or {}))
    return {
        "name": (pool or {}).get("metadata", {}).get("name"),
        "subnet": first.get("subnet"), "gateway": first.get("gateway"),
        "mask": mask_of(net.prefixlen) if net else None,
        "ranges": ranges, "total": total, "available": available,
        "used": sorted((st.get("allocated") or {}).keys()),
    }


def addresses_needed(spec):
    """Une adresse par machine, plus une pour la machine de remplacement
    qu'un déploiement progressif crée avant d'en retirer une (maxSurge 1)."""
    return spec.get("cp_replicas", 0) + spec.get("worker_replicas", 0) + 1


def cidr_overlaps(spec, vm_subnet):
    """Chevauchements qui cassent le routage :
    [(option, autre, (plage, autre plage))]."""
    out = []
    pod, svc, vm = _net(spec.get("pod_cidr")), _net(spec.get("service_cidr")), _net(vm_subnet)
    if pod and svc and pod.overlaps(svc):
        out.append(("pod_cidr", "service_cidr", (str(pod), str(svc))))
    for key, net in (("pod_cidr", pod), ("service_cidr", svc)):
        if net and vm and net.overlaps(vm):
            out.append((key, "vm_subnet", (str(net), str(vm))))
    return out


# ---------------------------------------------------------------------------
# Contrôle préalable
# ---------------------------------------------------------------------------

def finding(code, level, **facts):
    return {"code": code, "level": level, "facts": facts}


def check(spec, facts):
    """Constats sur une demande, à partir de ce qu'on a lu du cluster.

    `facts` : {
      "stack": {"ready": bool, "missing": [..], "turtles": bool, "legacy": bool},
      "namespaces": [...], "clusters": ["ns/name", ...],
      "images": {"ns/displayName": {"ready": bool, "iso": bool}},
      "keypairs": [...], "networks": [...], "storage_classes": [...],
      "pools": {"name": pool_info(...)},
      "versions": ["v1.33.5", ...],
      "free_cpu_m": millicœurs | None, "free_memory": octets | None,
      "overcommit": {"cpu": 1600, "memory": 150},
    }
    Niveaux : block, warn, ok (comme le transfert de VM).
    """
    out = []
    stack = facts.get("stack") or {}
    if not stack.get("ready"):
        out.append(finding("stack-not-ready", "block", missing=", ".join(stack.get("missing") or [])))
    if stack.get("legacy"):
        out.append(finding("stack-legacy", "warn"))
    for key, msg in validate(spec):
        out.append(finding("invalid", "block", option=key, message=msg))
    ns, name = spec.get("namespace"), spec.get("name")
    clusters = set(facts.get("clusters") or [])
    neighbours = sorted(c for c in clusters if c.split("/", 1)[0] == ns)
    if f"{ns}/{name}" in clusters:
        out.append(finding("name-taken", "block", name=f"{ns}/{name}"))
    elif neighbours:
        # Le générateur nomme ses objets sans le nom du cluster (ClusterClass,
        # secret d'identité, compléments) : un second cluster les écraserait.
        out.append(finding("namespace-has-cluster", "block", namespace=ns,
                           cluster=neighbours[0]))
    elif ns in set(facts.get("namespaces") or []):
        out.append(finding("namespace-exists", "warn", namespace=ns))
    img = (facts.get("images") or {}).get(spec.get("image"))
    if spec.get("image") and img is None:
        out.append(finding("image-missing", "block", image=spec.get("image")))
    elif img and img.get("iso"):
        out.append(finding("image-iso", "block", image=spec.get("image")))
    elif img and not img.get("ready"):
        out.append(finding("image-not-ready", "block", image=spec.get("image")))
    elif img and img.get("os") == "sles":
        # Vu sur harv1 : une image SLES non enregistrée n'a aucun dépôt,
        # cloud-init n'installe ni iptables ni qemu-guest-agent, et les pods
        # à port d'hôte (ingress-nginx) restent en ContainerCreating.
        out.append(finding("image-sles-repos", "warn", image=spec.get("image")))
    if spec.get("ssh_keypair") and spec["ssh_keypair"] not in set(facts.get("keypairs") or []):
        out.append(finding("keypair-missing", "block", keypair=spec["ssh_keypair"]))
    nets = set(facts.get("networks") or [])
    for net in [spec.get("network")] + list(spec.get("extra_networks") or []):
        if net and net not in nets:
            out.append(finding("network-missing", "block", network=net))
    if spec.get("extra_disk_class") and spec.get("extra_disk_size") and \
            spec["extra_disk_class"] not in set(facts.get("storage_classes") or []):
        out.append(finding("storage-class-missing", "block", storage_class=spec["extra_disk_class"]))
    pools = facts.get("pools") or {}
    names = [spec.get("ip_pool")] + [p for p in spec.get("ip_pool_refs") or [] if p != spec.get("ip_pool")]
    available = 0
    for p in names:
        if not p:
            continue
        info = pools.get(p)
        if info is None:
            out.append(finding("pool-missing", "block", pool=p))
            continue
        available += info.get("available") or 0
    first = pools.get(spec.get("ip_pool")) or {}
    need = addresses_needed(spec)
    if names and all(p in pools for p in names if p) and available < need:
        out.append(finding("pool-short", "block", needed=need, available=available))
    if first.get("gateway") and spec.get("gateway") and first["gateway"] != spec["gateway"]:
        out.append(finding("gateway-differs", "warn", gateway=spec["gateway"], pool=first["gateway"]))
    if first.get("mask") and spec.get("subnet_mask") and first["mask"] != spec["subnet_mask"]:
        out.append(finding("mask-differs", "warn", mask=spec["subnet_mask"], pool=first["mask"]))
    for key, other, (rng, other_rng) in cidr_overlaps(spec, first.get("subnet")):
        out.append(finding("cidr-overlap", "block", option=key, other=other,
                           range=rng, other_range=other_rng))
    if spec.get("cp_replicas", 1) % 2 == 0:
        out.append(finding("cp-even", "warn", count=spec["cp_replicas"]))
    if spec.get("cp_replicas") == 1:
        out.append(finding("cp-single", "warn"))
    mem = _bytes(spec.get("memory"))
    if spec.get("cpu", 2) < 2 or (mem and mem < 4 * 1024 ** 3):
        out.append(finding("small-nodes", "warn", cpu=spec.get("cpu"), memory=spec.get("memory")))
    versions = facts.get("versions") or []
    if versions and spec.get("k8s_version") not in versions:
        out.append(finding("version-untested", "warn", version=spec.get("k8s_version"),
                           tested=", ".join(versions)))
    if not spec.get("fleet_repo") and (spec.get("cni_mtu") != DEFAULTS["cni_mtu"]
                                       or spec.get("cni_encapsulation") != DEFAULTS["cni_encapsulation"]
                                       or spec.get("cni_bgp") != DEFAULTS["cni_bgp"]):
        out.append(finding("cni-tuning-ignored", "warn"))
    need_cpu, need_mem = requests_needed(spec, facts.get("overcommit"))
    free_cpu, free_mem = facts.get("free_cpu_m"), facts.get("free_memory")
    if free_cpu is not None and need_cpu > free_cpu:
        out.append(finding("cpu-short", "warn", needed=round(need_cpu / 1000, 2),
                           free=round(free_cpu / 1000, 2)))
    if free_mem is not None and need_mem > free_mem:
        out.append(finding("memory-short", "warn", needed=need_mem, free=free_mem))
    out.append(finding("endpoint-dhcp", "ok"))
    return out


# Ce que coûte une VM à l'ordonnanceur, au-delà de sa mémoire invitée
# (relevé sur harv1 : 4 Gio demandés pour 3,08 Gio de requête à 150 %).
VM_MEMORY_OVERHEAD = 350 * 1024 ** 2


def requests_needed(spec, overcommit=None):
    """Ce que les VMs du cluster demanderont à l'ordonnanceur, en tenant
    compte du surengagement de Harvester (`overcommit-config` : un vCPU à
    1600 % ne demande qu'un seizième de cœur). Rend (millicœurs, octets)."""
    oc = overcommit or {}
    cpu_oc = max(100, int(oc.get("cpu") or 100))
    mem_oc = max(100, int(oc.get("memory") or 100))
    vms = spec.get("cp_replicas", 0) + spec.get("worker_replicas", 0)
    cpu_m = vms * spec.get("cpu", 0) * 1000 * 100 / cpu_oc
    mem = vms * (_bytes(spec.get("memory")) * 100 / mem_oc + VM_MEMORY_OVERHEAD)
    return int(cpu_m), int(mem)


def blocking(findings):
    return [f for f in findings if f["level"] == "block"]


_QUANTITY = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}


def _bytes(q):
    m = re.match(r"^([0-9]+)(Ki|Mi|Gi|Ti)$", str(q or ""))
    return int(m.group(1)) * _QUANTITY[m.group(2)] if m else 0


# ---------------------------------------------------------------------------
# Progression d'un cluster (v1beta1 comme v1beta2)
# ---------------------------------------------------------------------------

def _cond(obj, ctype):
    for c in ((obj or {}).get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c
    return None


def cluster_state(cluster, machines=None):
    """État lisible d'un Cluster CAPI et de ses machines, quelle que soit la
    version servie : v1beta2 publie `status.initialization.*`, v1beta1
    `status.controlPlaneReady` et `status.infrastructureReady`."""
    st = (cluster or {}).get("status") or {}
    init = st.get("initialization") or {}
    infra = bool(init.get("infrastructureProvisioned", st.get("infrastructureReady")))
    cp = bool(init.get("controlPlaneInitialized", st.get("controlPlaneReady")))
    phase = st.get("phase") or "Pending"
    cp_total = cp_running = wk_total = wk_running = 0
    for m in machines or []:
        labels = (m.get("metadata") or {}).get("labels") or {}
        is_cp = "cluster.x-k8s.io/control-plane" in labels
        running = _machine_up(m)
        if is_cp:
            cp_total += 1
            cp_running += running
        else:
            wk_total += 1
            wk_running += running
    if machines is None:
        # Sans la liste des machines (vue liste) : les compteurs que le cœur
        # v1beta2 publie dans le statut du Cluster.
        for key, out in (("controlPlane", "cp"), ("workers", "wk")):
            rep = st.get(key) or {}
            if "desiredReplicas" in rep:
                got = rep.get("availableReplicas", rep.get("readyReplicas")) or 0
                if out == "cp":
                    cp_running, cp_total = got, rep["desiredReplicas"] or 0
                else:
                    wk_running, wk_total = got, rep["desiredReplicas"] or 0
    # Le nombre voulu vient de la topologie : au début, aucune machine
    # n'existe encore et « 0/0 » ne disait rien de ce qui est attendu.
    topo = ((cluster or {}).get("spec") or {}).get("topology") or {}
    want_cp = (topo.get("controlPlane") or {}).get("replicas")
    mds = (topo.get("workers") or {}).get("machineDeployments") or []
    want_wk = sum(md.get("replicas") or 0 for md in mds) if mds else None
    if isinstance(want_cp, int):
        cp_total = want_cp
    if isinstance(want_wk, int):
        wk_total = want_wk
    avail = _cond(cluster, "Available")
    # Available ne suffit pas : la topologie ne crée les MachineDeployments
    # qu'une fois le plan de contrôle initialisé, et dans l'intervalle le
    # cluster se dit disponible sans aucun worker (vu sur harv1 : « prêt »
    # annoncé quatre minutes avant que le worker existe).
    counts = cp_total > 0 and cp_running >= cp_total and wk_running >= wk_total
    if avail is not None:
        ready = counts and avail.get("status") == "True"
    else:
        # cœur d'avant la condition Available (v1beta1 seulement)
        ready = counts and phase == "Provisioned" and cp and infra
    failed = phase == "Failed"
    message = ""
    if not ready and avail is not None and avail.get("status") != "True":
        message = (avail.get("message") or "").replace("\n", " ").strip()[:300]
    return {"phase": phase, "infrastructure": infra, "control_plane": cp,
            "cp": [cp_running, cp_total], "workers": [wk_running, wk_total],
            "ready": bool(ready), "failed": failed, "message": message}


def _machine_up(machine):
    """Une machine compte quand son nœud est prêt, pas seulement inscrit :
    vu sur harv1 (essai3), le worker avait son nodeRef une demi-minute avant
    que son nœud passe Ready (calico encore en initialisation)."""
    st = (machine or {}).get("status") or {}
    if st.get("phase") != "Running" or not st.get("nodeRef"):
        return False
    conds = {c.get("type"): c.get("status") for c in st.get("conditions") or []}
    old = {c.get("type"): c.get("status")
           for c in ((st.get("deprecated") or {}).get("v1beta1") or {}).get("conditions") or []}
    for kind in ("NodeReady", "NodeHealthy"):
        if kind in conds:
            return conds[kind] == "True"
    if "NodeHealthy" in old:
        return old["NodeHealthy"] == "True"
    return True


def class_of(cluster):
    """Nom de la ClusterClass : `topology.class` (v1beta1) ou
    `topology.classRef.name` (v1beta2)."""
    topo = ((cluster or {}).get("spec") or {}).get("topology") or {}
    return topo.get("class") or (topo.get("classRef") or {}).get("name")
