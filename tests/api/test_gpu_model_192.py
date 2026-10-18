"""v1.92.0 : bin/lib/hv_gpu.py, l'inventaire GPU d'un cluster.

Fixtures construites sur les formes des CRD de harvester/pcidevices
(PCIDevice, PCIDeviceClaim, SRIOVGPUDevice, VGPUDevice, MigConfiguration)
et sur les objets Kubernetes (Node, Pod, VirtualMachine). Aucun matériel
vGPU ni MIG n'a servi : les formes viennent de la source de pcidevices."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import hv_gpu as hg  # noqa: E402

ALLOC = "harvesterhci.io/deviceAllocationDetails"


def pci(node, addr, cls="0302", vendor="10de", desc="3D controller: NVIDIA GA107GL [A2]", res="nvidia.com/GA107GL_A2"):
    return {"metadata": {"name": hg.pci_name(addr, node)},
            "status": {"address": addr, "vendorId": vendor, "deviceId": "25b6", "classId": cls, "iommuGroup": "40",
                       "nodeName": node, "resourceName": res, "description": desc, "kernelDriverInUse": "nvidia"}}


def claim(name, enabled=True):
    return {"metadata": {"name": name}, "spec": {"userName": "admin"}, "status": {"passthroughEnabled": enabled}}


def sriovgpu(node, addr, vfs=(), enabled=True):
    return {"metadata": {"name": hg.pci_name(addr, node)}, "spec": {"address": addr, "nodeName": node, "enabled": enabled},
            "status": {"vfAddresses": list(vfs), "vGPUDevices": [hg.pci_name(v, node) for v in vfs]}}


def vgpu(node, addr, parent_addr, vtype="NVIDIA A2-4Q", configured=True, label=True):
    meta = {"name": hg.pci_name(addr, node)}
    if label:
        meta["labels"] = {hg.PARENT_LABEL: hg.pci_name(parent_addr, node)}
    return {"metadata": meta,
            "spec": {"vGPUTypeName": vtype if configured else "", "address": addr, "enabled": configured,
                     "nodeName": node, "parentGPUDeviceAddress": parent_addr},
            "status": {"vGPUStatus": "vGPUConfigured" if configured else "", "uuid": "u-" + addr,
                       "configureVGPUTypeName": vtype if configured else "", "availableTypes": {vtype: "nvidia-745"}}}


def mig(node, addr, profiles, status="synced", enabled=True, message=""):
    return {"metadata": {"name": hg.pci_name(addr, node)},
            "spec": {"enabled": enabled, "gpuAddress": addr, "nodeName": node,
                     "profileSpec": [{"name": n, "id": i, "requested": len(ids)} for n, i, ids, _a, _t in profiles]},
            "status": {"profileStatus": [{"name": n, "id": i, "available": a, "total": t, "vGPUID": list(ids)}
                                         for n, i, ids, a, t in profiles],
                       "status": status, "message": message}}


def node(name, labelled=True, alloc=None, cap=None, cordoned=False):
    m = {"name": name, "labels": {hg.CONTAINER_LABEL: "true"} if labelled else {}}
    return {"metadata": m, "spec": {"unschedulable": cordoned} if cordoned else {},
            "status": {"allocatable": alloc or {}, "capacity": cap if cap is not None else (alloc or {})}}


def pod(ns, name, node_name, gpus=None, phase="Running", labels=None):
    res = {"limits": dict(gpus)} if gpus else {}
    return {"metadata": {"namespace": ns, "name": name, "labels": labels or {}},
            "spec": {"nodeName": node_name, "containers": [{"name": "c", "image": "cuda:12", "resources": res}]},
            "status": {"phase": phase}}


def vm(ns, name, host=(), gpus=(), alloc=None):
    meta = {"namespace": ns, "name": name}
    if alloc is not None:
        meta["annotations"] = {ALLOC: json.dumps(alloc)}
    return {"metadata": meta, "spec": {"template": {"spec": {"domain": {"devices": {
        "hostDevices": [{"name": n, "deviceName": d} for n, d in host],
        "gpus": [{"name": n, "deviceName": d} for n, d in gpus]}}}}}}


def vmi(ns, name):
    return {"metadata": {"namespace": ns, "name": name}}


def units_of(inv, t):
    return [u for u in inv["units"] if u["type"] == t]


def one(inv, uid):
    got = [u for u in inv["units"] if u["id"] == uid]
    assert len(got) == 1, [u["id"] for u in inv["units"]]
    return got[0]


# ---------------------------------------------------------------------------
# Reconnaître un GPU entier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls,vendor,is_gpu", [
    ("0300", "10de", True),       # VGA NVIDIA
    ("0302", "10de", True),       # 3D NVIDIA (A2, A100, H100)
    ("0300", "1002", True),       # VGA AMD
    ("0302", "1002", True),
    ("0300", "8086", False),      # Intel intégré (le seul « GPU » de harv1)
    ("0200", "10de", False),      # carte réseau NVIDIA (Mellanox)
    ("0380", "10de", False),      # autre contrôleur d'affichage
    ("", "10de", False),
])
def test_gpu_pci_class_and_vendor(cls, vendor, is_gpu):
    assert hg.is_gpu_pci(pci("n1", "0000:3b:00.0", cls=cls, vendor=vendor)) is is_gpu


def test_pci_name_and_vgpu_resource_follow_pcidevices():
    assert hg.pci_name("0000:3b:00.0", "n1") == "n1-00003b000"
    assert hg.vgpu_resource("NVIDIA A2-4Q") == "nvidia.com/NVIDIA_A2-4Q"
    assert hg.vgpu_resource(" GRID A100.1-5C ") == "nvidia.com/GRID_A100_1-5C"


# ---------------------------------------------------------------------------
# GPU entiers : libre, utilisé, désactivé et pourquoi
# ---------------------------------------------------------------------------

G = "n1-00003b000"


@pytest.mark.parametrize("case,lists,state,reason,consumers", [
    ("passthrough on, nobody", {"pciclaims": [claim(G)]}, "free", "", []),
    ("passthrough off", {}, "disabled", "passthrough-off", []),
    ("claim pending", {"pciclaims": [claim(G, enabled=False)]}, "disabled", "passthrough-off", []),
    ("running VM by allocation", {"pciclaims": [claim(G)],
                                  "vms": [vm("prod", "train", host=[("gpu1", "nvidia.com/GA107GL_A2")],
                                             alloc={"hostdevices": {"nvidia.com/GA107GL_A2": [G]}})],
                                  "vmis": [vmi("prod", "train")]}, "used", "", ["prod/train"]),
    ("stopped VM by spec name", {"pciclaims": [claim(G)],
                                 "vms": [vm("prod", "infer", host=[(G, "nvidia.com/GA107GL_A2")])],
                                 "vmis": []}, "used", "", ["prod/infer"]),
    ("resource name alone does not mark it", {"pciclaims": [claim(G)],
                                              "vms": [vm("prod", "x", host=[("gpu1", "nvidia.com/GA107GL_A2")])],
                                              "vmis": []}, "free", "", []),
    ("node given to containers", {"nodes": [node("n1")]}, "disabled", "containers", []),
    ("split into vGPUs", {"sriovgpu": [sriovgpu("n1", "0000:3b:00.0")]}, "disabled", "vgpu", []),
    ("SR-IOV GPU switched off", {"sriovgpu": [sriovgpu("n1", "0000:3b:00.0", enabled=False)],
                                 "pciclaims": [claim(G)]}, "free", "", []),
    ("partitioned with MIG", {"mig": [mig("n1", "0000:3b:00.0", [])]}, "disabled", "mig", []),
    ("MIG configuration off", {"mig": [mig("n1", "0000:3b:00.0", [], enabled=False)],
                               "pciclaims": [claim(G)]}, "free", "", []),
])
def test_whole_gpu_state(case, lists, state, reason, consumers):
    inv = hg.build(dict(lists, pci=[pci("n1", "0000:3b:00.0")]), cluster="c1")
    u = one(inv, G)
    assert (u["type"], u["state"], u["reason"]) == ("gpu", state, reason), case
    assert [f"{c['namespace']}/{c['name']}" for c in u["consumers"]] == consumers
    assert all(c["kind"] == "vm" and c["cluster"] == "c1" for c in u["consumers"])
    assert (u["total"], u["used"]) == (1, 1 if consumers else 0)


def test_non_gpu_pci_devices_are_left_out():
    inv = hg.build({"pci": [pci("n1", "0000:00:02.0", cls="0300", vendor="8086", res="intel.com/UHD"),
                            pci("n1", "0000:05:00.0", cls="0200", vendor="8086", res="intel.com/82574L")]})
    assert inv["units"] == []
    assert inv["summary"]["gpu"] == {"total": 0, "used": 0, "free": 0}


# ---------------------------------------------------------------------------
# vGPU : parent, configuré ou non, consommateurs
# ---------------------------------------------------------------------------

PARENT = "0000:3b:00.0"
VFS = ("0000:3b:00.4", "0000:3b:00.5", "0000:3b:00.6")


def vgpu_world(**extra):
    lists = {"pci": [pci("n1", PARENT)] + [pci("n1", v, res=hg.vgpu_resource("NVIDIA A2-4Q")) for v in VFS],
             "sriovgpu": [sriovgpu("n1", PARENT, VFS)],
             "vgpu": [vgpu("n1", VFS[0], PARENT), vgpu("n1", VFS[1], PARENT, label=False),
                      vgpu("n1", VFS[2], PARENT, configured=False)]}
    lists.update(extra)
    return lists


def test_virtual_functions_are_not_whole_gpus():
    inv = hg.build(vgpu_world())
    assert [u["id"] for u in units_of(inv, "gpu")] == [hg.pci_name(PARENT, "n1")]
    assert one(inv, hg.pci_name(PARENT, "n1"))["vgpus"] == 3


@pytest.mark.parametrize("vf,parent,state", [
    (VFS[0], hg.pci_name(PARENT, "n1"), "free"),     # parent par le label
    (VFS[1], hg.pci_name(PARENT, "n1"), "free"),     # parent par adresse + nœud
    (VFS[2], hg.pci_name(PARENT, "n1"), "disabled"), # vGPUStatus vide : pas de type configuré
])
def test_vgpu_parent_and_state(vf, parent, state):
    u = one(hg.build(vgpu_world()), hg.pci_name(vf, "n1"))
    assert (u["type"], u["parent"], u["state"]) == ("vgpu", parent, state)
    if state != "disabled":
        assert (u["model"], u["resource"]) == ("NVIDIA A2-4Q", "nvidia.com/NVIDIA_A2-4Q")


def test_a_vm_holding_two_vgpus_is_the_consumer_of_both():
    names = [hg.pci_name(v, "n1") for v in VFS[:2]]
    res = "nvidia.com/NVIDIA_A2-4Q"
    w = vgpu_world(vms=[vm("ml", "trainer", gpus=[("g1", res), ("g2", res)], alloc={"gpus": {res: names}})],
                   vmis=[vmi("ml", "trainer")])
    inv = hg.build(w, cluster="c1")
    for n in names:
        u = one(inv, n)
        assert u["state"] == "used"
        assert u["consumers"] == [{"kind": "vm", "cluster": "c1", "namespace": "ml", "name": "trainer"}]
    assert inv["summary"]["vgpu"] == {"total": 3, "used": 2, "free": 0}


def test_a_disabled_vgpu_named_by_a_vm_is_not_counted_as_used():
    name = hg.pci_name(VFS[2], "n1")
    inv = hg.build(vgpu_world(vms=[vm("ml", "x", gpus=[(name, "nvidia.com/x")])], vmis=[]))
    u = one(inv, name)
    assert (u["state"], u["used"], u["consumers"]) == ("disabled", 0, [])


# ---------------------------------------------------------------------------
# MIG : instances créées, utilisées par les vGPU du GPU, synchronisation
# ---------------------------------------------------------------------------

def mig_world(status="synced", enabled=True, users=0):
    vfs = VFS[:2]
    lists = {"pci": [pci("n1", PARENT, desc="3D controller: NVIDIA A100")],
             "sriovgpu": [sriovgpu("n1", PARENT, vfs)],
             "vgpu": [vgpu("n1", v, PARENT, vtype="NVIDIA A100-3-20C") for v in vfs],
             "mig": [mig("n1", PARENT, [("MIG 3g.20gb", 9, ["1", "2"], 0, 2), ("MIG 1g.5gb", 19, [], 0, 7)],
                         status=status, enabled=enabled, message="profile 9 not applied" if status == "out-of-sync" else "")]}
    res = "nvidia.com/NVIDIA_A100-3-20C"
    names = [hg.pci_name(v, "n1") for v in vfs[:users]]
    if users:
        lists["vms"] = [vm("ml", f"vm{i}", gpus=[("g", res)], alloc={"gpus": {res: [n]}}) for i, n in enumerate(names)]
        lists["vmis"] = [vmi("ml", f"vm{i}") for i in range(users)]
    return lists


@pytest.mark.parametrize("status,enabled,users,state,counts", [
    ("synced", True, 0, "free", (2, 0, 2)),
    ("synced", True, 1, "free", (2, 1, 1)),
    ("synced", True, 2, "used", (2, 2, 0)),
    ("out-of-sync", True, 1, "free", (2, 1, 1)),
    ("disabled", True, 0, "disabled", (2, 0, 2)),
    ("synced", False, 0, "disabled", (2, 0, 2)),
])
def test_mig_instances(status, enabled, users, state, counts):
    inv = hg.build(mig_world(status, enabled, users), cluster="c1")
    (u,) = units_of(inv, "mig")
    assert (u["state"], u["sync"]) == (state, status)
    assert (u["total"], u["used"], u["free"]) == counts
    assert u["parent"] == hg.pci_name(PARENT, "n1")
    assert u["model"] == "MIG 3g.20gb x2"
    assert [(p["name"], p["created"], p["available"], p["total"], p["requested"]) for p in u["profiles"]] == \
        [("MIG 3g.20gb", 2, 0, 2, 2), ("MIG 1g.5gb", 0, 0, 7, 0)]
    assert len(u["consumers"]) == users
    if status == "out-of-sync":
        assert u["message"] == "profile 9 not applied"
    # le GPU partitionné ne se donne pas en passthrough
    assert one(inv, hg.pci_name(PARENT, "n1"))["state"] == "disabled"
    expected_free = 0 if state == "disabled" else counts[2]
    assert inv["summary"]["mig"] == {"total": counts[0], "used": counts[1], "free": expected_free}


def test_mig_without_status_yet():
    m = mig("n1", PARENT, [])
    m["status"] = {}
    (u,) = units_of(hg.build({"mig": [m]}), "mig")
    assert (u["total"], u["state"], u["profiles"]) == (0, "disabled", [])


# ---------------------------------------------------------------------------
# GPU des nœuds pour les conteneurs
# ---------------------------------------------------------------------------

def test_a_pod_requesting_two_gpus_and_system_pods():
    lists = {"nodes": [node("n1", alloc={"nvidia.com/gpu": "4", "cpu": "32"})],
             "pods": [pod("ml", "trainer-0", "n1", {"nvidia.com/gpu": "2"}),
                      pod("gpu-operator-system", "validator", "n1", {"nvidia.com/gpu": "1"}),
                      pod("ml", "done", "n1", {"nvidia.com/gpu": "1"}, phase="Succeeded"),
                      pod("ml", "pending", "", {"nvidia.com/gpu": "1"}, phase="Pending"),
                      pod("ml", "virt-launcher-vm-x", "n1", {"nvidia.com/gpu": "1"},
                          labels={"kubevirt.io": "virt-launcher"}),
                      pod("ml", "web", "n1")]}
    inv = hg.build(lists, cluster="c1")
    (u,) = units_of(inv, "node")
    assert (u["id"], u["node"], u["name"], u["state"]) == ("n1/nvidia.com/gpu", "n1", "nvidia.com/gpu", "free")
    assert (u["total"], u["used"], u["free"]) == (4, 3, 1)
    assert u["consumers"] == [
        {"kind": "pod", "cluster": "c1", "namespace": "ml", "name": "trainer-0", "system": False, "count": 2},
        {"kind": "pod", "cluster": "c1", "namespace": "gpu-operator-system", "name": "validator", "system": True, "count": 1}]


@pytest.mark.parametrize("case,n,state", [
    ("labelled, all taken", node("n1", alloc={"nvidia.com/gpu": "2"}), "used"),
    ("cordoned", node("n1", alloc={"nvidia.com/gpu": "2"}, cordoned=True), "disabled"),
    ("not labelled: not a container GPU node", node("n1", labelled=False, alloc={"nvidia.com/gpu": "2"}), None),
    ("zero allocatable", node("n1", alloc={"nvidia.com/gpu": "0"}), None),
    ("AMD", node("n1", alloc={"amd.com/gpu": "2"}), "used"),
    ("MIG strategy mixed", node("n1", alloc={"nvidia.com/mig-1g.5gb": "2"}), "free"),
    ("capacity only", node("n1", alloc={}, cap={"nvidia.com/gpu": "2"}), "used"),
])
def test_node_resources(case, n, state):
    pods = [pod("ml", "p", "n1", {"nvidia.com/gpu": "2", "amd.com/gpu": "2"})]
    got = units_of(hg.build({"nodes": [n], "pods": pods}), "node")
    if state is None:
        assert got == [], case
    else:
        assert [u["state"] for u in got] == [state], case


def test_passthrough_and_vgpu_resources_on_a_node_are_not_container_gpus():
    # KubeVirt annonce aussi sur le nœud les ressources des passthrough et des vGPU
    alloc = {"nvidia.com/GA107GL_A2": "1", "nvidia.com/NVIDIA_A2-4Q": "2", "nvidia.com/gpu": "1"}
    w = vgpu_world(nodes=[node("n1", alloc=alloc)])
    assert [u["name"] for u in units_of(hg.build(w), "node")] == ["nvidia.com/gpu"]


def test_multi_gpu_multi_node_summary():
    lists = {"pci": [pci("n1", "0000:3b:00.0"), pci("n1", "0000:af:00.0"), pci("n2", "0000:3b:00.0")],
             "pciclaims": [claim("n1-00003b000"), claim("n1-0000af000"), claim("n2-00003b000")],
             "vms": [vm("a", "v1", host=[("n2-00003b000", "nvidia.com/GA107GL_A2")])], "vmis": [],
             "nodes": [node("n3", alloc={"nvidia.com/gpu": "8"}), node("n4", alloc={"nvidia.com/gpu": "8"})],
             "pods": [pod("ml", "p1", "n3", {"nvidia.com/gpu": "8"}), pod("ml", "p2", "n4", {"nvidia.com/gpu": "3"})]}
    inv = hg.build(lists, cluster="c1")
    assert inv["summary"]["gpu"] == {"total": 3, "used": 1, "free": 2}
    assert inv["summary"]["node"] == {"total": 16, "used": 11, "free": 5}
    assert [(u["type"], u["node"]) for u in inv["units"]] == [
        ("gpu", "n1"), ("gpu", "n1"), ("gpu", "n2"), ("node", "n3"), ("node", "n4")]
    assert {u["node"]: u["state"] for u in units_of(inv, "node")} == {"n3": "used", "n4": "free"}


# ---------------------------------------------------------------------------
# CRD absentes, add-on coupé, objets incomplets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("absent,addon,available", [
    ((), True, {"addon": True, "gpu": True, "vgpu": True, "mig": True, "node": True}),
    (("vgpu",), True, {"addon": True, "gpu": True, "vgpu": False, "mig": True, "node": True}),
    (("sriovgpu",), True, {"addon": True, "gpu": True, "vgpu": False, "mig": True, "node": True}),
    (("mig",), True, {"addon": True, "gpu": True, "vgpu": True, "mig": False, "node": True}),
    (("pci", "pciclaims", "sriovgpu", "vgpu", "mig"), False,
     {"addon": False, "gpu": False, "vgpu": False, "mig": False, "node": True}),
    ((), False, {"addon": False, "gpu": False, "vgpu": False, "mig": False, "node": True}),
])
def test_absent_crds_are_not_available_not_errors(absent, addon, available):
    lists = {k: [] for k in ("pci", "pciclaims", "sriovgpu", "vgpu", "mig") if k not in absent}
    lists.update(nodes=[node("n1", alloc={"nvidia.com/gpu": "1"})], pods=[])
    inv = hg.build(lists, absent=absent, addon=addon)
    assert inv["available"] == available
    # les GPU des conteneurs restent visibles sans l'add-on
    assert [u["type"] for u in inv["units"]] == ["node"]


@pytest.mark.parametrize("bad", [
    {"pci": [{"metadata": {}}, None, "x"]},
    {"vgpu": [{"metadata": {}, "spec": {}}]},
    {"mig": [{"metadata": {}}]},
    {"mig": [{"metadata": {"name": "m"}, "spec": None, "status": {"profileStatus": [None, "x"]}}]},
    {"nodes": [{"metadata": {"name": "n1", "labels": {hg.CONTAINER_LABEL: "true"}},
                "status": {"allocatable": {"nvidia.com/gpu": "lots"}}}]},
    {"pods": [{"metadata": {"name": "p"}, "spec": {"nodeName": "n1", "containers": [None]}}]},
    {"vms": [{"metadata": {"namespace": "a", "name": "b",
                           "annotations": {ALLOC: "{not json"}}}], "vmis": []},
])
def test_incomplete_objects_are_ignored(bad):
    inv = hg.build(bad)
    assert isinstance(inv["units"], list)


def test_summary_counts_disabled_units_in_total_but_not_in_free():
    s = hg.summarize([{"type": "gpu", "state": "disabled", "total": 1, "used": 0, "free": 1},
                      {"type": "gpu", "state": "free", "total": 1, "used": 0, "free": 1},
                      {"type": "other", "total": 5}])
    assert s["gpu"] == {"total": 2, "used": 0, "free": 1}
    assert set(s) == set(hg.TYPES)
