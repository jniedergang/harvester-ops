"""harvester-ops : l'inventaire des GPU d'un cluster Harvester (v1.92.0).

Une unité est ce qu'on pourra réserver plus tard. Quatre types :

- `gpu` : un GPU entier, un PCIDevice de classe 0300 (VGA) ou 0302 (3D)
  d'un vendeur NVIDIA (10de) ou AMD (1002), donné à une VM en passthrough ;
- `vgpu` : un VGPUDevice, une fonction virtuelle d'un GPU SR-IOV qui porte
  un type de vGPU (`NVIDIA_A2-4Q`) ;
- `mig` : les instances MIG d'un GPU (MigConfiguration), comptées par GPU ;
- `node` : la ressource GPU d'un nœud pour les conteneurs
  (`status.allocatable`, `nvidia.com/*` ou `amd.com/gpu`), sur un nœud
  marqué `harvesterhci.io/gpu-baremetal-workloads=true`.

Formes relevées dans harvester/pcidevices (pkg/apis/devices.harvesterhci.io/
v1beta1 et pkg/controller/gpudevice) :

- un VGPUDevice porte le NOM du PCIDevice de sa fonction virtuelle ; le
  contrôleur réécrit le `resourceName` de ce PCIDevice avec le nom de
  ressource du vGPU (`gpuhelper.GenerateDeviceName`). Ces fonctions
  virtuelles, de classe 0302 elles aussi, ne sont donc PAS des GPU entiers ;
- le GPU parent d'un vGPU : le label `harvesterhci.io/parentSRIOVGPUDevice`,
  sinon `PCIDeviceNameForHostname(parentGPUDeviceAddress, nodeName)` ;
- `MigProfileStatus.vGPUID` liste les identifiants des instances MIG créées
  (`instanceCount` du contrôleur MIG) ; `available` dit combien d'instances
  de ce profil on pourrait encore créer, `total` le maximum du profil. Une
  instance MIG est consommée par un vGPU adossé à MIG : les instances
  utilisées d'un GPU sont ses vGPU utilisés, au plus le nombre d'instances.

Le consommateur est générique, `{kind: vm|pod, cluster, namespace, name}`,
pour que la réservation compte VM et conteneurs de la même façon. Les VM
sont lues par `hv_devices.users()` (spec et annotation d'allocation) ; les
pods par leurs demandes de ressources GPU, tous namespaces confondus (un pod
système qui tient un GPU le tient quand même), marqués `system`. Les pods
des VM sont écartés : la VM est déjà le consommateur.

Fonctions pures, sans entrée-sortie.
"""

import re

import hv_containers as _hc
import hv_devices as _hd

GROUP = _hd.GROUP
# (ressource kubectl, clé dans `lists`) ; une CRD absente du cluster
# (add-on pcidevices-controller coupé, vGPU ou MIG non installés) est dite
# « non disponible », jamais une erreur
DEVICE_RESOURCES = (
    (f"pcidevices.{GROUP}", "pci"),
    (f"pcideviceclaims.{GROUP}", "pciclaims"),
    (f"sriovgpudevices.{GROUP}", "sriovgpu"),
    (f"vgpudevices.{GROUP}", "vgpu"),
    (f"migconfigurations.{GROUP}", "mig"),
)
TYPES = ("gpu", "vgpu", "mig", "node")
STATES = ("free", "used", "disabled")

GPU_CLASSES = ("0300", "0302")
GPU_VENDORS = {"10de": "NVIDIA", "1002": "AMD"}
PARENT_LABEL = "harvesterhci.io/parentSRIOVGPUDevice"
CONTAINER_LABEL = "harvesterhci.io/gpu-baremetal-workloads"
VGPU_CONFIGURED = "vGPUConfigured"
MIG_SYNC = ("synced", "out-of-sync", "disabled")


def _meta(o):
    return (o or {}).get("metadata") or {}


def _int(v, default=0):
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return default


def pci_name(address, node):
    """`PCIDeviceNameForHostname` de pcidevices : `<nœud>-<adresse sans : ni .>`."""
    return f"{node}-{str(address or '').replace(':', '').replace('.', '')}"


def vgpu_resource(type_name):
    """Le nom de ressource kubelet d'un type de vGPU
    (`common.GeneratevGPUDeviceName` de pcidevices)."""
    s = str(type_name or "").strip().replace("/", "_").replace(".", "_")
    return "nvidia.com/" + re.sub(r"\s+", "_", s)


def is_gpu_resource(key):
    return key.startswith(_hc.GPU_PREFIXES) or key in _hc.GPU_NAMES


def is_gpu_pci(dev):
    st = (dev or {}).get("status") or {}
    return str(st.get("classId") or "")[:4] in GPU_CLASSES \
        and str(st.get("vendorId") or "").lower() in GPU_VENDORS


def consumer(kind, cluster, namespace, name, **extra):
    return dict({"kind": kind, "cluster": cluster, "namespace": namespace, "name": name}, **extra)


def _vm_consumers(names, used, cluster):
    """Les VM qui tiennent l'un de ces noms, d'après `hv_devices.users()`."""
    out, seen = [], set()
    for n in names:
        for ref in used.get(n) or []:
            if ref in seen:
                continue
            seen.add(ref)
            ns, _, vm = ref.partition("/")
            out.append(consumer("vm", cluster, ns, vm))
    return out


def _counts(total, used):
    used = max(0, min(used, total))
    return {"total": total, "used": used, "free": total - used}


def _state(disabled, total, used):
    if disabled or total <= 0:
        return "disabled"
    return "used" if used >= total else "free"


def pod_workload(pod):
    """La charge d'un pod, d'après ses seuls propriétaires (v1.95.0) : un
    ReplicaSet `<deployment>-<pod-template-hash>` remonte à son Deployment ;
    StatefulSet, DaemonSet, Job : leur nom ; un pod seul : le sien. Rend le
    nom (la cible d'une réservation de charge)."""
    meta = _meta(pod)
    ref = _hc._controller_ref(pod or {}, _hc._PARENT_KINDS["Pod"])
    if not ref:
        return meta.get("name") or ""
    name = ref["name"]
    if ref["kind"] == "ReplicaSet":
        h = (meta.get("labels") or {}).get("pod-template-hash")
        if h and name.endswith("-" + h):
            return name[:-len(h) - 1]
    return name


def pod_gpu_holders(pods, cluster=""):
    """{(nœud, ressource): [(consommateur, nombre)]} des pods qui tiennent des
    GPU : planifiés, ni terminés ni des pods de VM ou de CDI."""
    out = {}
    for p in pods or []:
        meta, spec = _meta(p), (p or {}).get("spec") or {}
        node = spec.get("nodeName")
        if not node or not meta.get("name") or _hc.is_vm_or_cdi_pod(p):
            continue
        if ((p.get("status") or {}).get("phase")) in ("Succeeded", "Failed"):
            continue
        ns = meta.get("namespace") or ""
        who = consumer("pod", cluster, ns, meta["name"], system=_hc.is_system_namespace(ns),
                       workload=pod_workload(p))
        for key, n in _hc.gpu_requests(spec).items():
            if n > 0:
                out.setdefault((node, key), []).append((who, n))
    return out


def build(lists, cluster="", absent=(), addon=True):
    """L'inventaire d'un cluster.

    `lists` : {"pci", "pciclaims", "sriovgpu", "vgpu", "mig", "nodes",
    "pods", "vms", "vmis": [objets]} ; `absent` : les clés dont la CRD
    n'existe pas sur le cluster ; `addon` : l'add-on pcidevices-controller
    est activé. Rend {"units", "summary", "available"}. Un objet incomplet
    est ignoré, jamais fatal."""
    lists = lists or {}
    absent = set(absent or ())
    get = lambda k: [o for o in (lists.get(k) or []) if isinstance(o, dict)]  # noqa: E731
    running = _hd.running_set(get("vmis")) if "vmis" in lists else None
    used = _hd.users(get("vms"), running)
    claims = {_meta(c).get("name"): c for c in get("pciclaims")}
    pcis = {_meta(d).get("name"): d for d in get("pci") if _meta(d).get("name")}
    nodes = {_meta(n).get("name"): n for n in get("nodes") if _meta(n).get("name")}
    labelled = {n for n, o in nodes.items() if (_meta(o).get("labels") or {}).get(CONTAINER_LABEL) == "true"}
    units = []

    # -- vGPU (d'abord : leurs fonctions virtuelles sortent des GPU entiers) --
    vgpu_names = set()
    vf_addrs = set()
    sriov_on = set()                                  # GPU parents découpés en vGPU
    for s in get("sriovgpu"):
        sp = s.get("spec") or {}
        for a in ((s.get("status") or {}).get("vfAddresses") or []):
            vf_addrs.add((sp.get("nodeName"), a))
        if sp.get("enabled"):
            sriov_on.add(_meta(s).get("name") or pci_name(sp.get("address"), sp.get("nodeName")))
    by_parent = {}
    for v in get("vgpu"):
        name = _meta(v).get("name")
        if not name:
            continue
        vgpu_names.add(name)
        sp, st = v.get("spec") or {}, v.get("status") or {}
        node = sp.get("nodeName") or ""
        parent = (_meta(v).get("labels") or {}).get(PARENT_LABEL) \
            or (pci_name(sp.get("parentGPUDeviceAddress"), node) if sp.get("parentGPUDeviceAddress") else "")
        vtype = sp.get("vGPUTypeName") or st.get("configureVGPUTypeName") or ""
        configured = st.get("vGPUStatus") == VGPU_CONFIGURED and bool(sp.get("enabled", True))
        who = _vm_consumers([name], used, cluster) if configured else []
        c = _counts(1, 1 if who else 0)
        unit = {"id": name, "type": "vgpu", "cluster": cluster, "node": node, "name": name,
                "model": vtype, "address": sp.get("address") or "", "parent": parent,
                "resource": vgpu_resource(vtype) if vtype else "",
                "state": _state(not configured, 1, c["used"]), "consumers": who, **c}
        units.append(unit)
        by_parent.setdefault(parent, []).append(unit)

    # -- GPU entiers ---------------------------------------------------------
    mig_on = {}
    for m in get("mig"):
        sp = m.get("spec") or {}
        if sp.get("gpuAddress") and sp.get("nodeName"):
            mig_on[pci_name(sp["gpuAddress"], sp["nodeName"])] = m
    for name, d in pcis.items():
        st = d.get("status") or {}
        node = st.get("nodeName") or ""
        if not is_gpu_pci(d) or name in vgpu_names or (node, st.get("address")) in vf_addrs:
            continue
        claim = claims.get(name)
        enabled = bool(((claim or {}).get("status") or {}).get("passthroughEnabled"))
        who = _vm_consumers([name], used, cluster)
        if name in sriov_on:
            reason = "vgpu"
        elif name in mig_on and ((mig_on[name].get("spec") or {}).get("enabled")):
            reason = "mig"
        elif not enabled and not who:
            reason = "containers" if node in labelled else "passthrough-off"
        else:
            reason = ""
        c = _counts(1, 1 if who else 0)
        units.append({"id": name, "type": "gpu", "cluster": cluster, "node": node, "name": name,
                      "model": st.get("description") or "", "vendor": GPU_VENDORS.get(str(st.get("vendorId")).lower(), ""),
                      "address": st.get("address") or "", "parent": "", "resource": st.get("resourceName") or "",
                      "state": _state(bool(reason), 1, c["used"]), "reason": reason, "consumers": who,
                      "vgpus": len(by_parent.get(name, [])), **c})

    # -- MIG : une ligne par GPU partitionné --------------------------------
    for m in get("mig"):
        name = _meta(m).get("name")
        sp, st = m.get("spec") or {}, m.get("status") or {}
        if not name:
            continue
        node = sp.get("nodeName") or ""
        parent = pci_name(sp.get("gpuAddress"), node) if sp.get("gpuAddress") else ""
        profiles = []
        for p in st.get("profileStatus") or []:
            if isinstance(p, dict):
                profiles.append({"name": p.get("name") or "", "id": p.get("id"),
                                 "created": len(p.get("vGPUID") or []),
                                 "available": _int(p.get("available")), "total": _int(p.get("total"))})
        requested = {(r.get("name"), r.get("id")): _int(r.get("requested"))
                     for r in sp.get("profileSpec") or [] if isinstance(r, dict)}
        for p in profiles:
            p["requested"] = requested.get((p["name"], p["id"]), 0)
        created = sum(p["created"] for p in profiles)
        sync = st.get("status") or ""
        disabled = not sp.get("enabled") or sync == "disabled"
        holders = [u for u in by_parent.get(parent, []) if u["used"]]
        who = [c for u in holders for c in u["consumers"]]
        c = _counts(created, len(holders))
        # même nom que le PCIDevice du GPU : l'identifiant porte le type
        units.append({"id": f"{name}/mig", "type": "mig", "cluster": cluster, "node": node, "name": name,
                      "model": ", ".join(f"{p['name']} x{p['created']}" for p in profiles if p["created"]) or "",
                      "address": sp.get("gpuAddress") or "", "parent": parent, "resource": "",
                      "state": _state(disabled, created, c["used"]), "sync": sync,
                      "message": st.get("message") or "", "profiles": profiles,
                      "consumers": who, **c})

    # -- ressource GPU des nœuds pour les conteneurs -------------------------
    # les noms de ressource des passthrough et des vGPU sont aussi annoncés
    # par les nœuds (plugins de KubeVirt) : ce sont des unités de VM, pas
    # des GPU de conteneurs
    vm_resources = {(d.get("status") or {}).get("resourceName") for d in pcis.values()}
    vm_resources |= {u["resource"] for u in units if u["type"] == "vgpu"}
    holders = pod_gpu_holders(get("pods"), cluster)
    for nname in sorted(labelled):
        o = nodes[nname]
        st = o.get("status") or {}
        alloc = st.get("allocatable") or {}
        cap = st.get("capacity") or {}
        cordoned = bool((o.get("spec") or {}).get("unschedulable"))
        for key in sorted(set(alloc) | set(cap)):
            if not is_gpu_resource(key) or key in vm_resources:
                continue
            total = _int(alloc.get(key, cap.get(key)))
            if total <= 0:
                continue
            held = holders.get((nname, key), [])
            c = _counts(total, sum(n for _, n in held))
            units.append({"id": f"{nname}/{key}", "type": "node", "cluster": cluster, "node": nname,
                          "name": key, "model": key, "address": "", "parent": "", "resource": key,
                          "capacity": _int(cap.get(key, total)), "cordoned": cordoned,
                          "state": _state(cordoned, total, c["used"]),
                          "consumers": [dict(w, count=n) for w, n in held], **c})

    order = {t: i for i, t in enumerate(TYPES)}
    units.sort(key=lambda u: (order[u["type"]], u["node"], u["address"], u["name"]))
    return {"units": units, "summary": summarize(units), "available": {
        "addon": bool(addon),
        "gpu": addon and "pci" not in absent,
        "vgpu": addon and "vgpu" not in absent and "sriovgpu" not in absent,
        "mig": addon and "mig" not in absent,
        "node": True,
    }}


def summarize(units):
    """{type: {total, used, free}} : la somme des unités de chaque type."""
    out = {t: {"total": 0, "used": 0, "free": 0} for t in TYPES}
    for u in units or []:
        s = out.get(u.get("type"))
        if s is None:
            continue
        s["total"] += u.get("total", 0)
        s["used"] += u.get("used", 0)
        # une unité désactivée n'est pas libre
        s["free"] += 0 if u.get("state") == "disabled" else u.get("free", 0)
    return out
