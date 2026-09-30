"""harvester-ops : les réglages et gestes d'un hôte, comme l'interface de Harvester (v1.62.0).

Formats relevés dans harvester-ui-extension v1.9.0, le serveur de Harvester
v1.9.0, node-disk-manager v1.9.0, node-manager et seeder v1.9.0 :

- nom affiché et URL de console : annotations du Node ;
- tags d'hôte : `nodes.longhorn.io` <nœud> spec.tags ; tags et planification
  d'un disque : spec.disks[<disque>] du même objet ;
- disques : BlockDevice (harvesterhci.io/v1beta1, longhorn-system),
  spec.provision vrai pour l'ajouter, faux pour le retirer (NDM évacue les
  répliques avant de retirer le disque de Longhorn) ;
- Hugepage et Ksmtuned : node.harvesterhci.io/v1beta1, du nom du nœud ;
- CPU manager : annotation `harvesterhci.io/cpu-manager-update-status`, un
  Job de Harvester redémarre rke2 sur le nœud puis pose le label cpumanager ;
- accès hors bande : Inventory de seeder (metal.harvesterhci.io/v1alpha1),
  gestes d'alimentation par spec.powerActionRequested, en maintenance.

Fonctions pures ; bin/harvester-resources.py `host` les applique.
"""

import copy
import json
import re

ANN_NAME = "harvesterhci.io/host-custom-name"
ANN_CONSOLE = "harvesterhci.io/host-console-url"
ANN_CPU = "harvesterhci.io/cpu-manager-update-status"
ANN_MAINT = "harvesterhci.io/maintain-status"
# ceux que cache Harvester, plus ceux que la console protège aussi : les
# retirer casserait le CPU manager (cpumanager, posé par KubeVirt), Rancher
# (cattle.io), Longhorn ou kube-ovn (kube-ovn/role, vu sur harv1)
HIDDEN_LABEL = re.compile(r"(k3s|kubernetes|kubevirt|harvesterhci|k3os|cattle|longhorn)+\.io/|^cpumanager$|^kube-ovn/")
SHOWN_LABELS = ("topology.kubernetes.io/zone", "topology.kubernetes.io/region")
LABEL_KEY = re.compile(r"^([a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
TAG_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")

K_BD = "blockdevices.harvesterhci.io"
K_LHNODE = "nodes.longhorn.io"
K_HUGEPAGE = "hugepages.node.harvesterhci.io"
K_KSM = "ksmtuneds.node.harvesterhci.io"
K_INVENTORY = "inventories.metal.harvesterhci.io"
K_BMC_JOB = "jobs.bmc.tinkerbell.org"

THP_ENABLED = ("always", "madvise", "never")
THP_SHMEM = ("always", "within_size", "advise", "never", "deny", "force")
THP_DEFRAG = ("always", "defer", "defer+madvise", "madvise", "never")
KSM_RUN = ("stop", "run", "prune")
KSM_MODES = {"standard": {"sleepMsec": 20, "boost": 0, "decay": 0, "minPages": 100, "maxPages": 100},
             "high": {"sleepMsec": 20, "boost": 200, "decay": 50, "minPages": 100, "maxPages": 10000}}
POWER_OPS = ("shutdown", "poweron", "reboot")


NODE_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


def check_node(name):
    """Un nom de nœud est un sous-domaine DNS (vu sur harv1 : `harv1.home.lo`)."""
    if not NODE_RE.match(name or ""):
        raise ValueError("host name: a node name (lower-case letters, digits, dots and dashes)")
    return name


def user_labels(node):
    """Les labels qu'une personne gère (ceux que montre Harvester)."""
    labels = ((node or {}).get("metadata") or {}).get("labels") or {}
    return {k: v for k, v in labels.items() if k in SHOWN_LABELS or not HIDDEN_LABEL.search(k)}


def basics_patch(node, custom_name=None, console_url=None, labels=None):
    """Merge patch du Node : nom affiché, URL de console, labels (les labels
    visibles retirés passent à null ; les autres ne sont jamais touchés)."""
    ann = {}
    if custom_name is not None:
        ann[ANN_NAME] = custom_name.strip() or None
    if console_url is not None:
        url = console_url.strip()
        if url and not re.match(r"^[a-z]+://\S+$", url):
            raise ValueError("console URL: an address such as https://10.0.0.21")
        ann[ANN_CONSOLE] = url or None
    patch = {"metadata": {}}
    if ann:
        patch["metadata"]["annotations"] = ann
    if labels is not None:
        new = {}
        for k, v in labels.items():
            if not LABEL_KEY.match(k) or (HIDDEN_LABEL.search(k) and k not in SHOWN_LABELS):
                raise ValueError(f"label {k!r}: invalid, or reserved to the system")
            if len(str(v)) > 63 or (v and not re.match(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$", str(v))):
                raise ValueError(f"label {k!r}: value of 63 letters, digits, dash, dot or underscore at most")
            new[k] = str(v)
        for k in user_labels(node):
            if k not in new:
                new[k] = None
        patch["metadata"]["labels"] = new
    return patch


def check_tags(tags):
    tags = [t.strip() for t in tags or [] if t and t.strip()]
    bad = [t for t in tags if not TAG_RE.match(t)]
    if bad:
        raise ValueError(f"tags: letters, digits, dash, dot, underscore ({', '.join(bad)})")
    if len(set(tags)) != len(tags):
        raise ValueError("tags: each tag once")
    return tags


def block_devices(bds, node_name, lh_node=None):
    """Les disques d'un nœud tels que la fenêtre les montre : ceux déjà
    ajoutés à Longhorn, et ceux qu'on peut ajouter (mêmes règles que
    Harvester : disque entier actif, non monté, non provisionné)."""
    lh_disks = ((lh_node or {}).get("spec") or {}).get("disks") or {}
    lh_status = ((lh_node or {}).get("status") or {}).get("diskStatus") or {}
    out = []
    for bd in bds:
        meta, spec, st = bd.get("metadata") or {}, bd.get("spec") or {}, bd.get("status") or {}
        if spec.get("nodeName") != node_name:
            continue
        det = (st.get("deviceStatus") or {}).get("details") or {}
        fs = (st.get("deviceStatus") or {}).get("fileSystem") or {}
        conds = {c.get("type"): str(c.get("status")) for c in st.get("conditions") or []}
        provisioned = bool(spec.get("provision") or (spec.get("fileSystem") or {}).get("provisioned"))
        name = meta.get("name")
        in_lh = name in lh_disks
        dstat = lh_status.get(name) or {}
        dconds = {c.get("type"): str(c.get("status")) for c in dstat.get("conditions") or []}
        out.append({
            "name": name, "dev_path": spec.get("devPath") or (st.get("deviceStatus") or {}).get("devPath"),
            "size": ((st.get("deviceStatus") or {}).get("capacity") or {}).get("sizeBytes"),
            "type": det.get("deviceType"), "state": st.get("state"), "phase": st.get("provisionPhase"),
            "mounted": bool(fs.get("mountPoint")), "provisioned": provisioned,
            "formatting": conds.get("Formatting") == "True",
            "provisioner": "lvm" if (spec.get("provisioner") or {}).get("lvm") else
                           ((spec.get("provisioner") or {}).get("longhorn") or {}).get("engineVersion") or ("LonghornV1" if provisioned else None),
            "addable": det.get("deviceType") == "disk" and st.get("state") == "Active" and not in_lh
                       and conds.get("AddedToNode") != "True" and not provisioned and not fs.get("mountPoint"),
            "in_longhorn": in_lh,
            "tags": (lh_disks.get(name) or {}).get("tags") or [],
            "scheduling": (lh_disks.get(name) or {}).get("allowScheduling"),
            "ready": dconds.get("Ready") == "True", "schedulable": dconds.get("Schedulable") == "True",
            "storage_available": dstat.get("storageAvailable"), "storage_maximum": dstat.get("storageMaximum"),
            "storage_scheduled": dstat.get("storageScheduled"),
            "message": st.get("deviceStatus", {}).get("message") if isinstance(st.get("deviceStatus"), dict) else None,
        })
    # les disques Longhorn sans BlockDevice (le disque par défaut, sur le disque
    # système) : Harvester les montre avec leurs tags, sans « retirer »
    seen = {d["name"] for d in out}
    for name, spec in lh_disks.items():
        if name in seen:
            continue
        dstat = lh_status.get(name) or {}
        dconds = {c.get("type"): str(c.get("status")) for c in dstat.get("conditions") or []}
        out.append({
            "name": name, "dev_path": spec.get("path"), "size": dstat.get("storageMaximum"), "type": "longhorn",
            "state": None, "phase": None, "mounted": True, "provisioned": False, "formatting": False,
            "provisioner": "LonghornV1" if (spec.get("diskType") or "filesystem") == "filesystem" else spec.get("diskType"),
            "addable": False, "in_longhorn": True, "removable": False,
            "tags": spec.get("tags") or [], "scheduling": spec.get("allowScheduling"),
            "ready": dconds.get("Ready") == "True", "schedulable": dconds.get("Schedulable") == "True",
            "storage_available": dstat.get("storageAvailable"), "storage_maximum": dstat.get("storageMaximum"),
            "storage_scheduled": dstat.get("storageScheduled"), "message": None,
        })
    for d in out:
        d.setdefault("removable", bool(d["provisioned"]))
    out.sort(key=lambda d: (not d["in_longhorn"], d["dev_path"] or ""))
    return out


def disk_add(bd, force_format=None, provisioner="LonghornV1", vg=None, tags=None):
    """Le BlockDevice passé en « provisionné » (Harvester : PUT du BlockDevice).
    `tags` (v1.78.0) : étiquettes du disque, que node-disk-manager recopie
    dans les tags du disque Longhorn (c'est ce qui range un disque dans un
    pool : une classe à `diskSelector` n'y place que ses répliques)."""
    spec = (bd or {}).get("spec") or {}
    st = (bd or {}).get("status") or {}
    fs = (st.get("deviceStatus") or {}).get("fileSystem") or {}
    if spec.get("provision") or (spec.get("fileSystem") or {}).get("provisioned"):
        raise ValueError("this disk is already added")
    if fs.get("mountPoint"):
        raise ValueError(f"this disk is mounted on {fs['mountPoint']}: it cannot be added")
    out = copy.deepcopy(bd)
    s = out.setdefault("spec", {})
    s["provision"] = True
    if force_format is None:
        # défaut de Harvester : formater sauf un disque déjà en ext4/XFS
        force_format = not (fs.get("LastFormattedAt") or str(fs.get("type") or "").lower() in ("ext4", "xfs"))
    s.setdefault("fileSystem", {})["forceFormatted"] = bool(force_format)
    if provisioner == "lvm":
        if not vg or not re.match(r"^[A-Za-z0-9+_.-]{1,127}$", vg):
            raise ValueError("volume group: a name")
        s["provisioner"] = {"lvm": {"vgName": vg}}
    elif provisioner in ("LonghornV1", "LonghornV2"):
        s["provisioner"] = {"longhorn": {"engineVersion": provisioner}}
    else:
        raise ValueError("provisioner: LonghornV1, LonghornV2 or lvm")
    if tags is not None:
        s["tags"] = check_tags(tags)
    # vu sur harvlab : le CRD BlockDevice n'a pas de sous-ressource status,
    # un remplacement sans status est refusé (« status: Required value »)
    return out


def disk_remove(bd):
    spec = (bd or {}).get("spec") or {}
    if not (spec.get("provision") or (spec.get("fileSystem") or {}).get("provisioned")):
        raise ValueError("this disk is not added")
    out = copy.deepcopy(bd)
    out["spec"]["provision"] = False
    if "fileSystem" in out["spec"]:
        out["spec"]["fileSystem"].pop("provisioned", None)
    return out


def lh_node_patch(lh_node, tags=None, disk=None, disk_tags=None, scheduling=None):
    """Merge patch du nœud Longhorn : tags de l'hôte, tags et planification
    d'un disque."""
    patch = {"spec": {}}
    if tags is not None:
        patch["spec"]["tags"] = check_tags(tags)
    if disk is not None:
        disks = ((lh_node or {}).get("spec") or {}).get("disks") or {}
        if disk not in disks:
            raise ValueError(f"no Longhorn disk {disk} on this node")
        d = {}
        if disk_tags is not None:
            d["tags"] = check_tags(disk_tags)
        if scheduling is not None:
            d["allowScheduling"] = bool(scheduling)
        patch["spec"]["disks"] = {disk: d}
    return patch


def hugepage_patch(enabled=None, shmem=None, defrag=None):
    t = {}
    for v, allowed, key in ((enabled, THP_ENABLED, "enabled"), (shmem, THP_SHMEM, "shmemEnabled"),
                            (defrag, THP_DEFRAG, "defrag")):
        if v is None:
            continue
        if v not in allowed:
            raise ValueError(f"{key}: one of {', '.join(allowed)}")
        t[key] = v
    if not t:
        raise ValueError("nothing to change")
    return {"spec": {"transparent": t}}


def ksmtuned_patch(run=None, mode=None, thres=None, merge=None, params=None):
    spec = {}
    if run is not None:
        if run not in KSM_RUN:
            raise ValueError("run: stop, run or prune")
        spec["run"] = run
    if thres is not None:
        t = int(thres)
        if not 0 <= t <= 100:
            raise ValueError("threshold: 0 to 100 (% of memory)")
        spec["thresCoef"] = t
    if merge is not None:
        spec["mergeAcrossNodes"] = 1 if merge else 0
    if mode is not None:
        if mode not in ("standard", "high", "customized"):
            raise ValueError("mode: standard, high or customized")
        spec["mode"] = mode
        if mode in KSM_MODES:
            spec["ksmtunedParameters"] = dict(KSM_MODES[mode])
        else:
            p = params or {}
            try:
                vals = {k: int(p[k]) for k in ("sleepMsec", "boost", "decay", "minPages", "maxPages")}
            except (KeyError, TypeError, ValueError):
                raise ValueError("customized mode: sleepMsec, boost, decay, minPages, maxPages as numbers") from None
            if any(v < 0 for v in vals.values()) or vals["minPages"] > vals["maxPages"]:
                raise ValueError("customized mode: positive numbers, minPages at most maxPages")
            spec["ksmtunedParameters"] = vals
    if not spec:
        raise ValueError("nothing to change")
    return {"spec": spec}


def cpu_manager_status(node):
    meta = (node or {}).get("metadata") or {}
    raw = (meta.get("annotations") or {}).get(ANN_CPU)
    try:
        st = json.loads(raw) if raw else {}
    except ValueError:
        st = {}
    return {"enabled": ((meta.get("labels") or {}).get("cpumanager") == "true"),
            "label": (meta.get("labels") or {}).get("cpumanager"),
            "policy": st.get("policy"), "status": st.get("status"), "job": st.get("jobName")}


def cpu_manager_request(node, enable, vmis_on_node=()):
    """L'annotation que pose l'action enableCPUManager / disableCPUManager,
    après les contrôles du webhook de Harvester."""
    labels = ((node or {}).get("metadata") or {}).get("labels") or {}
    if labels.get("node-role.harvesterhci.io/witness") is not None:
        raise ValueError("a witness node has no CPU manager")
    if "cpumanager" not in labels:
        raise ValueError("the node has no cpumanager label yet (KubeVirt sets it): try again later")
    want = "true" if enable else "false"
    if labels.get("cpumanager") == want:
        raise ValueError(f"the CPU manager is already {'enabled' if enable else 'disabled'}")
    st = cpu_manager_status(node)
    if st["status"] in ("requested", "running"):
        raise ValueError("a CPU manager change is already in progress on this node")
    if not enable:
        pinned = [f"{(v.get('metadata') or {}).get('namespace')}/{(v.get('metadata') or {}).get('name')}"
                  for v in vmis_on_node
                  if ((((v.get("spec") or {}).get("domain") or {}).get("cpu") or {}).get("dedicatedCpuPlacement"))]
        if pinned:
            raise ValueError(f"VMs with dedicated CPUs run on this node: {', '.join(pinned[:5])}")
    return {"metadata": {"annotations": {ANN_CPU: json.dumps(
        {"policy": "static" if enable else "none", "status": "requested"}, separators=(",", ":"))}}}


def inventory(node_name, host, port, secret_ns, secret_name, insecure=False, events=True, interval="1h",
              existing=None):
    """L'Inventory de seeder pour l'accès hors bande d'un hôte."""
    if not host or not re.match(r"^[A-Za-z0-9.:\[\]-]+$", host):
        raise ValueError("BMC address: a host name or an IP address")
    try:
        port = int(port or 623)
    except ValueError:
        raise ValueError("port: a number") from None
    if not 1 <= port <= 65535:
        raise ValueError("port: 1 to 65535")
    if not re.match(r"^\d+[hms]$", interval or ""):
        raise ValueError("polling interval: a number followed by h, m or s (e.g. 1h)")
    out = copy.deepcopy(existing) if existing else {
        "apiVersion": "metal.harvesterhci.io/v1alpha1", "kind": "Inventory",
        "metadata": {"name": node_name, "namespace": "harvester-system",
                     "annotations": {"metal.harvesterhci.io/local-inventory": "true",
                                     "metal.harvesterhci.io/local-node-name": node_name}},
        "spec": {"primaryDisk": "", "managementInterfaceMacAddress": ""}}
    spec = out.setdefault("spec", {})
    spec["baseboardSpec"] = {"connection": {"host": host, "port": port, "insecureTLS": bool(insecure),
                                            "authSecretRef": {"name": secret_name, "namespace": secret_ns}}}
    spec["events"] = {"enabled": bool(events), "pollingInterval": interval}
    out.pop("status", None)
    return out


def power_check(node, inv, operation):
    if operation not in POWER_OPS:
        raise ValueError("operation: shutdown, poweron or reboot")
    if inv is None:
        raise ValueError("no out-of-band access configured for this host")
    ann = ((node or {}).get("metadata") or {}).get("annotations") or {}
    if ANN_MAINT not in ann:
        raise ValueError("Harvester powers a host only in maintenance mode: put it in maintenance first")
    st = (inv.get("status") or {})
    if st.get("status") != "inventoryNodeReady":
        raise ValueError(f"the BMC is not ready ({st.get('status') or 'no status yet'})")
    pa = st.get("powerAction") or {}
    if (inv.get("spec") or {}).get("powerActionRequested") and "actionStatus" not in pa:
        raise ValueError("a power action is already in progress")
    state = st.get("machinePowerState")
    if operation == "poweron" and state == "on":
        raise ValueError("the host is already on")
    if operation in ("shutdown", "reboot") and state == "off":
        raise ValueError("the host is off")
    return {"spec": {"powerActionRequested": operation}}


def delete_target(node, nodes):
    """Ce que supprime « Supprimer l'hôte » : la Machine Cluster API du nœud
    quand il en a une (le contrôleur retire ensuite le Node), sinon le Node."""
    if len(nodes) < 2:
        raise ValueError("the last node of a cluster cannot be deleted")
    ann = ((node or {}).get("metadata") or {}).get("annotations") or {}
    machine, mns = ann.get("cluster.x-k8s.io/machine"), ann.get("cluster.x-k8s.io/cluster-namespace")
    if machine and mns:
        return ("machines.cluster.x-k8s.io", mns, machine)
    return ("nodes", None, (node.get("metadata") or {}).get("name"))


def bmc_secret(node_name, username, password, existing=None):
    """Le secret des identifiants du BMC (clés username et password, comme
    ceux que propose Harvester)."""
    import base64
    if not username or not password:
        raise ValueError("BMC user name and password are required")
    out = copy.deepcopy(existing) if existing else {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": f"{node_name}-bmc", "namespace": "harvester-system",
                     "labels": {"app.kubernetes.io/managed-by": "harvester-ops"}}}
    out["data"] = {"username": base64.b64encode(username.encode()).decode(),
                   "password": base64.b64encode(password.encode()).decode()}
    out.pop("stringData", None)
    return out


def bmc_error(inv):
    """Pourquoi le seeder ne joint pas le BMC, en une ligne par fournisseur
    essayé. Vu sur harvlab : la condition machineNotContactable liste chaque
    fournisseur de bmclib (Redfish toujours sur le port 443, le port de
    l'Inventory n'étant que celui d'IPMI ; mot de passe IPMI limité à 20 octets)."""
    for c in ((inv or {}).get("status") or {}).get("conditions") or []:
        if c.get("type") == "machineNotContactable" and str(c.get("status")) == "True":
            lines = [x.strip(" *\t") for x in str(c.get("message") or "").split("\n")]
            lines = [x for x in lines if x.startswith("provider:")]
            short = []
            for x in lines:
                name = x.split(":", 2)[1].strip() if x.count(":") >= 2 else x
                why = "password longer than 20 bytes (IPMI limit)" if "longer than 20 bytes" in x else \
                      ("connection refused or no route" if ("no route" in x or "refused" in x) else
                       ("timeout" if "timeout" in x.lower() else x.split(":", 2)[-1].strip()[:80]))
                short.append(f"{name}: {why}")
            return "; ".join(short[:6]) or (c.get("message") or "unreachable")[:200]
    return None


# ---------------------------------------------------------------------------
# v1.68.1 : le détail d'un hôte, comme sa page dans Harvester (Basics,
# Instances, Network, Events) ; lecture seule
# ---------------------------------------------------------------------------

K_VLANSTATUS = "vlanstatuses.network.harvesterhci.io"
K_LINKMONITOR = "linkmonitors.network.harvesterhci.io"
ANN_NTP = "node.harvesterhci.io/ntp-service"
_SI = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
       "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40}


def qty(v):
    """Quantité Kubernetes en nombre (cœurs pour un CPU, octets pour une
    mémoire) : « 8 », « 250m », « 123456789n », « 65730264Ki »."""
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(n|u|m|k|M|G|T|Ki|Mi|Gi|Ti)?\s*$", str(v if v is not None else ""))
    return float(m.group(1)) * _SI[m.group(2) or ""] if m else None


def node_roles(node):
    labels = ((node or {}).get("metadata") or {}).get("labels") or {}
    if "node-role.harvesterhci.io/witness" in labels:
        return "witness"
    if "node-role.kubernetes.io/control-plane" in labels or "node-role.kubernetes.io/master" in labels:
        return "management"
    return "compute"


def host_detail(node, metrics=None, lh_node=None, vmis=(), vlanstatuses=(), linkmonitors=(), events=(), limit=50):
    m = (node or {}).get("metadata") or {}
    name = m.get("name")
    ann, labels = m.get("annotations") or {}, m.get("labels") or {}
    st = (node or {}).get("status") or {}
    info = st.get("nodeInfo") or {}
    cap, alloc = st.get("capacity") or {}, st.get("allocatable") or {}
    usage = (metrics or {}).get("usage") or {}
    disks = (((lh_node or {}).get("status") or {}).get("diskStatus") or {}).values()
    try:
        ntp = json.loads(ann.get(ANN_NTP) or "{}")
    except ValueError:
        ntp = {}
    ready = next((c for c in st.get("conditions") or [] if c.get("type") == "Ready"), {})
    basics = {
        "custom_name": ann.get(ANN_NAME) or "", "console_url": ann.get(ANN_CONSOLE) or "",
        "ip": next((a.get("address") for a in st.get("addresses") or [] if a.get("type") == "InternalIP"), ""),
        "role": node_roles(node), "os": info.get("osImage") or "", "kernel": info.get("kernelVersion") or "",
        "runtime": info.get("containerRuntimeVersion") or "", "kubelet": info.get("kubeletVersion") or "",
        "uuid": info.get("systemUUID") or "", "created": m.get("creationTimestamp"),
        "ready": ready.get("status") == "True", "unschedulable": bool(((node or {}).get("spec") or {}).get("unschedulable")),
        "maintenance": ann.get(ANN_MAINT) or "",
        "manufacturer": labels.get("manufacturer") or "", "serial": labels.get("serialNumber") or "",
        "model": labels.get("model") or "",
        "ntp": {"status": ntp.get("ntpSyncStatus") or "", "servers": ntp.get("currentNtpServers") or ""},
        "cpu": {"capacity": qty(cap.get("cpu")), "allocatable": qty(alloc.get("cpu")), "used": qty(usage.get("cpu"))},
        "memory": {"capacity": qty(cap.get("memory")), "allocatable": qty(alloc.get("memory")),
                   "used": qty(usage.get("memory"))},
        "storage": {"maximum": sum(int(d.get("storageMaximum") or 0) for d in disks),
                    "available": sum(int(d.get("storageAvailable") or 0) for d in disks),
                    "scheduled": sum(int(d.get("storageScheduled") or 0) for d in disks)},
    }
    instances = []
    for v in vmis or []:
        vs = v.get("status") or {}
        if vs.get("nodeName") != name:
            continue
        dom = ((v.get("spec") or {}).get("domain") or {})
        c = dom.get("cpu") or {}
        mem = (dom.get("memory") or {}).get("guest") or ((dom.get("resources") or {}).get("limits") or {}).get("memory")
        vm_ = (v.get("metadata") or {})
        instances.append({"namespace": vm_.get("namespace"), "name": vm_.get("name"), "phase": vs.get("phase") or "",
                          "ips": [i.get("ipAddress") for i in vs.get("interfaces") or [] if i.get("ipAddress")],
                          "cpu": (c.get("cores") or 1) * (c.get("sockets") or 1) * (c.get("threads") or 1),
                          "memory": qty(mem), "created": vm_.get("creationTimestamp"),
                          "migrating": bool(vs.get("migrationState") and not (vs["migrationState"].get("completed")))})
    vlans = []
    for s in vlanstatuses or []:
        ss = s.get("status") or {}
        if ss.get("node") != name:
            continue
        cond = next((c for c in ss.get("conditions") or [] if c.get("type") == "ready"), {})
        vlans.append({"cluster_network": ss.get("clusterNetwork"), "vlan_config": ss.get("vlanConfig"),
                      "vlans": sorted(a.get("vlanID") for a in ss.get("localAreas") or [] if a.get("vlanID") is not None),
                      "ready": cond.get("status") == "True", "message": cond.get("message") or ""})
    links = {}
    for lm in linkmonitors or []:
        for li in (((lm.get("status") or {}).get("linkStatus") or {}).get(name) or []):
            links[li.get("index")] = li
    nics = []
    for li in sorted(links.values(), key=lambda x: (x.get("type") != "device", x.get("name") or "")):
        master = links.get(li.get("masterIndex"), {}).get("name") if li.get("masterIndex") else ""
        nics.append({"name": li.get("name"), "type": li.get("type") or "", "state": li.get("state") or "",
                     "mac": li.get("mac") or "", "master": master or ""})
    evs = []
    for e in events or []:
        io = e.get("involvedObject") or e.get("regarding") or {}
        if io.get("kind") != "Node" or io.get("name") != name:
            continue
        evs.append({"type": e.get("type") or "", "reason": e.get("reason") or "", "message": e.get("message") or e.get("note") or "",
                    "count": e.get("count") or 1,
                    "last": e.get("lastTimestamp") or e.get("eventTime") or ((e.get("metadata") or {}).get("creationTimestamp"))})
    evs.sort(key=lambda x: x["last"] or "", reverse=True)
    return {"node": name, "basics": basics, "instances": sorted(instances, key=lambda x: (x["namespace"], x["name"])),
            "vlans": vlans, "nics": nics, "events": evs[:limit]}


# ---------------------------------------------------------------------------
# Pools de disques de données (v1.78.0) : l'installeur de Harvester ne connaît
# qu'un disque de données ; les autres sont provisionnés après l'installation,
# un pool = une étiquette de disque Longhorn + une classe `longhorn-<tag>`.
# ---------------------------------------------------------------------------

POOL_TAG_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$")
SC_PROVISIONER = "driver.longhorn.io"


def norm_wwn(value):
    """lsblk écrit « 0x5000c500a1b2c3d4 », d'autres outils sans préfixe."""
    v = str(value or "").strip().lower()
    return v[2:] if v.startswith("0x") else v


def norm_serial(value):
    return str(value or "").strip().lower()


def check_pools(pools):
    """Description des pools, normalisée ; ValueError au premier défaut.
    [{"tag", "replicas", "disks": [{"serial", "wwn", "path"}]}]"""
    if pools is None:
        return []
    if not isinstance(pools, list):
        raise ValueError("pools: a list")
    out, tags, seen = [], set(), {}
    for i, p in enumerate(pools):
        if not isinstance(p, dict):
            raise ValueError(f"pools[{i}]: an object")
        tag = str(p.get("tag") or "").strip()
        if not POOL_TAG_RE.match(tag):
            raise ValueError(f"pools[{i}].tag: 1 to 32 lower-case letters, digits or dashes")
        if tag in tags:
            raise ValueError(f"pools[{i}].tag: {tag} is used twice")
        tags.add(tag)
        raw = p.get("replicas")
        if raw in (None, ""):
            replicas = 1                # null ou absent : une réplique
        else:
            if isinstance(raw, bool) or str(raw).strip() not in ("1", "2", "3"):
                raise ValueError(f"pools[{i}].replicas: 1 to 3")
            replicas = int(str(raw).strip())
        disks = p.get("disks")
        if not isinstance(disks, list) or not disks:
            raise ValueError(f"pools[{i}].disks: at least one disk")
        clean = []
        for j, d in enumerate(disks):
            if not isinstance(d, dict):
                raise ValueError(f"pools[{i}].disks[{j}]: an object")
            serial = str(d.get("serial") or "").strip()
            wwn = str(d.get("wwn") or "").strip()
            path = str(d.get("path") or "").strip()
            if not (serial or wwn):
                raise ValueError(f"pools[{i}].disks[{j}]: a serial number or a WWN")
            for val in (serial, wwn, path):
                if len(val) > 256 or any(ord(c) < 32 for c in val):
                    raise ValueError(f"pools[{i}].disks[{j}]: invalid characters")
            for key in (("serial", norm_serial(serial)) if serial else None,
                        ("wwn", norm_wwn(wwn)) if wwn else None):
                if key is None:
                    continue
                if key in seen:
                    raise ValueError(f"pools[{i}].disks[{j}]: this disk is already in the pool {seen[key]}")
                seen[key] = tag
            clean.append({"serial": serial, "wwn": wwn, "path": path})
        out.append({"tag": tag, "replicas": replicas, "disks": clean})
    return out


def disk_label(disk):
    """Nom d'un disque de pool dans un message : son chemin, sinon son identité."""
    if disk.get("path"):
        return disk["path"]
    return f"serial {disk['serial']}" if disk.get("serial") else f"WWN {disk['wwn']}"


def match_block_device(bds, node, disk):
    """Le BlockDevice de node-disk-manager qui porte ce disque, sur ce nœud,
    ou None. NDM nomme ses objets d'un UUID et le nom noyau peut changer au
    redémarrage : la correspondance se fait par série ou WWN, jamais par
    `sdX`. Un disque entier l'emporte sur ses partitions (même série) ; deux
    disques entiers de même identité sont un refus (multipath, série vide
    remplacée par une valeur générique)."""
    serial, wwn = norm_serial(disk.get("serial")), norm_wwn(disk.get("wwn"))
    hits = []
    for bd in bds or []:
        if ((bd.get("spec") or {}).get("nodeName")) != node:
            continue
        det = (((bd.get("status") or {}).get("deviceStatus") or {}).get("details") or {})
        if (wwn and norm_wwn(det.get("wwn")) == wwn) or (serial and norm_serial(det.get("serialNumber")) == serial):
            hits.append(bd)
    whole = [b for b in hits if (((b.get("status") or {}).get("deviceStatus") or {}).get("details") or {})
             .get("deviceType", "disk") == "disk"]
    if len(whole) > 1:
        names = ", ".join(sorted((b.get("metadata") or {}).get("name", "?") for b in whole))
        raise ValueError(f"{disk_label(disk)}: several block devices match ({names})")
    return whole[0] if whole else None


def pool_disk_state(bd, tag):
    """"todo" (à provisionner), "done" (déjà dans ce pool) ; ValueError si
    le disque sert déjà à autre chose : on ne le touche pas."""
    spec = (bd or {}).get("spec") or {}
    if spec.get("provision") or (spec.get("fileSystem") or {}).get("provisioned"):
        tags = spec.get("tags") or []
        if tag in tags:
            return "done"
        path = spec.get("devPath") or (bd.get("metadata") or {}).get("name")
        raise ValueError(f"{path} is already a storage disk (tags: {', '.join(tags) or 'none'}): left as is")
    return "todo"


SYSTEM_LABEL_PREFIXES = ("COS_", "HARV_")
LH_DEFAULT_DISK = "/var/lib/harvester/defaultdisk"


def _dev_status(bd):
    return ((bd or {}).get("status") or {}).get("deviceStatus") or {}


def system_use(bd, bds, lh_node=None):
    """Raison pour laquelle ce BlockDevice sert déjà au système, ou None :
    une partition enfant (même nœud, `parentDevice` vers lui) montée ou
    portant une étiquette COS_* / HARV_* (disque système, disque de données
    de l'installeur), ou le disque par défaut de Longhorn. Un disque de pool
    n'est jamais l'un d'eux."""
    node = ((bd or {}).get("spec") or {}).get("nodeName")
    dev = ((bd or {}).get("spec") or {}).get("devPath") or _dev_status(bd).get("devPath")
    name = ((bd or {}).get("metadata") or {}).get("name")
    mounts = [str((_dev_status(bd).get("fileSystem") or {}).get("mountPoint") or "")]
    for child in bds or []:
        if child is bd or ((child.get("spec") or {}).get("nodeName")) != node:
            continue
        st = _dev_status(child)
        if not dev or st.get("parentDevice") != dev:
            continue
        fs = st.get("fileSystem") or {}
        det = st.get("details") or {}
        cdev = (child.get("spec") or {}).get("devPath") or st.get("devPath")
        labels = [str(x or "") for x in (det.get("label"), det.get("partitionLabel"), fs.get("label"))]
        system = [lb for lb in labels if lb.upper().startswith(SYSTEM_LABEL_PREFIXES)]
        if system:
            return f"{dev} holds the partition {cdev} labelled {system[0]}"
        if fs.get("mountPoint"):
            return f"{dev} holds the partition {cdev} mounted on {fs['mountPoint']}"
    if any(m == LH_DEFAULT_DISK or m.startswith(LH_DEFAULT_DISK + "/") for m in mounts):
        return f"{dev} is Longhorn's default disk"
    for dname, d in (((lh_node or {}).get("spec") or {}).get("disks") or {}).items():
        path = str(d.get("path") or "")
        if path.rstrip("/") == LH_DEFAULT_DISK and dname == name:
            return f"{dev} is Longhorn's default disk"
    return None


def pool_class_name(tag):
    return f"longhorn-{tag}"


def pool_class_state(sc, tag):
    """"absent", "same" (la classe existe avec ce sélecteur) ; ValueError si
    elle existe avec un autre sélecteur : une classe existante n'est jamais
    modifiée (des volumes s'en servent peut-être)."""
    if sc is None:
        return "absent"
    params = sc.get("parameters") or {}
    sel = sorted(t.strip() for t in str(params.get("diskSelector") or "").split(",") if t.strip())
    if sc.get("provisioner") == SC_PROVISIONER and sel == [tag]:
        return "same"
    raise ValueError(f"the storage class {pool_class_name(tag)} already exists with another disk selector "
                     f"({','.join(sel) or 'none'}): left as is")
