"""Tests for the /api/topology/<cluster> endpoint introduced in v1.4.19.

The endpoint consolidates kubectl snapshots for the Aperçu viz. Most of
the work is in pure reducers — we unit-test those — plus a smoke test
on the live endpoint.
"""

import json
import sys
from pathlib import Path
import importlib

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
sys.path.insert(0, str(WEB_DIR))
app_module = importlib.import_module("app")

_topology_node          = app_module._topology_node
_topology_vm            = app_module._topology_vm


# ---------------------------------------------------------------------------
# Reducer: _topology_node
# ---------------------------------------------------------------------------
def test_topology_node_marks_ready_from_condition_status():
    raw = {
        "metadata": {"name": "n1", "uid": "u1", "labels": {
            "node-role.kubernetes.io/control-plane": "",
            "node-role.kubernetes.io/etcd": "",
        }},
        "spec": {},
        "status": {
            "addresses": [
                {"type": "InternalIP", "address": "10.0.0.1"},
                {"type": "Hostname",   "address": "n1.local"},
            ],
            "conditions": [{"type": "Ready", "status": "True"}],
            "capacity":    {"cpu": "8",   "memory": "16Gi"},
            "allocatable": {"cpu": "7.5", "memory": "15Gi"},
        },
    }
    out = _topology_node(raw)
    assert out["name"] == "n1"
    assert out["ready"] is True
    assert out["schedulable"] is True
    assert set(out["roles"]) == {"control-plane", "etcd"}
    assert out["addresses"]["InternalIP"] == "10.0.0.1"
    assert out["capacity"]["cpu"] == "8"


def test_topology_node_handles_not_ready_and_cordoned():
    raw = {
        "metadata": {"name": "n2", "labels": {}},
        "spec": {"unschedulable": True},
        "status": {
            "conditions": [{"type": "Ready", "status": "False"}],
            "addresses": [],
        },
    }
    out = _topology_node(raw)
    assert out["ready"] is False
    assert out["schedulable"] is False


def test_topology_node_no_conditions_defaults_to_not_ready():
    """Missing or empty conditions list → ready=False (defensive)."""
    raw = {"metadata": {"name": "n3", "labels": {}}, "spec": {}, "status": {}}
    out = _topology_node(raw)
    assert out["ready"] is False


# ---------------------------------------------------------------------------
# Reducer: _topology_vm
# ---------------------------------------------------------------------------
def test_topology_vm_links_to_running_vmi_node():
    """When a VMI exists for the VM, the reducer must pull nodeName +
    phase from the VMI status, not from the VM spec."""
    vm = {
        "metadata": {"namespace": "ns1", "name": "vm-a"},
        "spec": {
            "runStrategy": "Always",
            "template": {"spec": {
                "domain": {"devices": {
                    "interfaces": [{"name": "nic-1", "bridge": {}}],
                    "disks":      [{"name": "disk-1", "bootOrder": 1}],
                }},
                "networks": [{"name": "nic-1", "multus": {"networkName": "ns1/prod"}}],
                "volumes":  [{"name": "disk-1",
                              "persistentVolumeClaim": {"claimName": "vm-a-disk-1"}}],
            }},
        },
    }
    vmi_by = {
        "ns1/vm-a": {"status": {"phase": "Running", "nodeName": "n1"}},
    }
    out = _topology_vm(vm, vmi_by)
    assert out["phase"] == "Running"
    assert out["node"] == "n1"
    assert out["networks"] == [{"name": "nic-1", "type": "multus", "ref": "ns1/prod"}]
    assert out["interfaces"] == [{"name": "nic-1", "binding": "bridge"}]
    assert out["volumes"][0]["pvc"] == "vm-a-disk-1"
    assert out["volumes"][0]["boot_order"] == 1


def test_topology_vm_phase_stopped_when_no_vmi():
    """A Halted VM has no VMI → phase falls back to Stopped, node is
    null. Frontend uses these to group into the 'unscheduled' bucket."""
    vm = {
        "metadata": {"namespace": "ns1", "name": "vm-halted"},
        "spec": {
            "runStrategy": "Halted",
            "template": {"spec": {
                "domain": {"devices": {"interfaces": [], "disks": []}},
                "networks": [],
                "volumes": [],
            }},
        },
    }
    out = _topology_vm(vm, {})
    assert out["phase"] == "Stopped"
    assert out["node"] is None
    assert out["run_strategy"] == "Halted"


def test_topology_vm_pod_network_type():
    """The reducer should classify pod-attached networks separately
    from multus so the viz can color them differently."""
    vm = {
        "metadata": {"namespace": "ns", "name": "vm-pod"},
        "spec": {"template": {"spec": {
            "networks": [{"name": "default", "pod": {}}],
            "domain": {"devices": {"interfaces": [{"name": "default", "masquerade": {}}], "disks": []}},
            "volumes": [],
        }}},
    }
    out = _topology_vm(vm, {})
    assert out["networks"] == [{"name": "default", "type": "pod", "ref": None}]
    assert out["interfaces"][0]["binding"] == "masquerade"


# ---------------------------------------------------------------------------
# Endpoint contract — error paths
# ---------------------------------------------------------------------------
def test_topology_endpoint_404_on_unknown_cluster(api):
    status, body = api("GET", "/api/topology/no-such", expect_status=404)
    assert "unknown cluster" in body["error"].lower()


def test_topology_endpoint_returns_500_on_kubectl_failure(api):
    """harv-fake's kubeconfig points to an unreachable cluster. The
    endpoint must surface a clear error JSON, not crash."""
    status, body = api("GET", "/api/topology/harv-fake", expect_status=None)
    # 200 happens if some kubectl call succeeded (unlikely against the
    # fake config). Either way, body must be JSON object.
    assert isinstance(body, dict)
    if status == 500:
        assert "error" in body


# ---------------------------------------------------------------------------
# Vues de l'aperçu : montage et chargement
# ---------------------------------------------------------------------------
def test_each_overview_board_mounts_into_its_own_subtab():
    """REGRESSION (v1.4.21), toujours valable avec les vues de blocs : un
    identifiant global (`#topology-canvas`) renvoyait la PREMIÈRE zone des
    sous-onglets, et Réseau ou Stockage se dessinaient dans celle du
    Cluster. Chaque module cherche donc la zone de SON sous-onglet."""
    js = WEB_DIR / "static" / "js"
    html = (WEB_DIR / "templates" / "index.html").read_text()
    # v1.57.0 : les vues ont quitté l'aperçu pour leurs sections (Storage,
    # Network) ; chacune cherche la zone qui porte SON data-board, présente
    # une seule fois dans la page.
    for module, mode in (("cluster-map.js", "cluster"), ("netmap.js", "network"),
                         ("fabric.js", "fabric"), ("storage-map.js", "storage"),
                         ("vpc.js", "vpc")):
        src = (js / module).read_text()
        assert (f"document.querySelector('[data-board=\"{mode}\"] .topology-host')") in src, module
        assert "#topology-canvas" not in src, module
        assert html.count(f'data-board="{mode}"') == 1, mode
    app = (js / "app.js").read_text()
    assert "cluster: window.ClusterMap" in app


def test_i18n_exposes_itself_on_window_for_es_modules():
    """REGRESSION (v1.4.23): topology.js (an ES module) couldn't read
    the `i18n` const from i18n.js (a classic script) because classic
    top-level `const` bindings aren't reachable via window. Result:
    every node click threw `Cannot read properties of undefined`.
    Fix: i18n.js must do `window.i18n = i18n` so any consumer kind
    (classic script or ES module) resolves it the same way."""
    i18n_js = WEB_DIR / "static" / "js" / "i18n.js"
    src = i18n_js.read_text()
    assert "window.i18n = " in src or "window['i18n']" in src, (
        "i18n.js must expose `i18n` on window so ES modules can use it."
    )


def test_app_js_restores_subtabs_after_setcluster():
    """REGRESSION (v1.4.31): the Overview sub-tab restoration must run
    AFTER setCluster() — otherwise mountTopology() bails on null
    cluster and the Cluster/Network/Storage tabs come back with an
    empty canvas. The restore helper is a separate function so the
    ordering is explicit at the init() call site."""
    app_js = WEB_DIR.parent / "web" / "static" / "js" / "app.js"
    src = app_js.read_text()
    assert "function restoreSubTabsFromStorage" in src, (
        "restoreSubTabsFromStorage helper missing — sub-tab restore "
        "race with setCluster() will recur."
    )
    # And init() must call it AFTER setCluster()
    init_body = src[src.find("function init()"):]
    init_body = init_body[:2500]
    sc = init_body.find("setCluster(")
    rs = init_body.find("restoreSubTabsFromStorage()")
    assert sc >= 0 and rs >= 0, (
        f"init() body lacks setCluster() or restoreSubTabsFromStorage() "
        f"(setCluster={sc}, restoreSubTabs={rs})"
    )
    assert rs > sc, (
        "restoreSubTabsFromStorage() must be called AFTER setCluster() "
        "in init() — otherwise currentCluster is null at click time and "
        "mountTopology() short-circuits."
    )


def test_topology_cache_ttl_is_reasonable():
    """Cache too short → kubectl thrash. Cache too long → stale UX. The
    sweet spot has been 5 s; this test catches accidental zero or huge
    values introduced during refactors."""
    assert 1.0 <= app_module.TOPOLOGY_CACHE_TTL <= 60.0


# ---------------------------------------------------------------------------
# v1.8.7 — CD-ROM identification in the Storage view
# ---------------------------------------------------------------------------

def test_topology_vm_exposes_device_type_per_volume():
    """The device type lives in the disk entry's key (disk/cdrom/lun) —
    the Storage view renders CD-ROMs with a distinct disc silhouette."""
    vm = {
        "metadata": {"namespace": "ns", "name": "vm-cd"},
        "spec": {"template": {"spec": {
            "domain": {"devices": {"interfaces": [], "disks": [
                {"name": "rootdisk", "bootOrder": 1, "disk": {"bus": "virtio"}},
                {"name": "installcd", "bootOrder": 2, "cdrom": {"bus": "sata"}},
                {"name": "san", "lun": {}},
            ]}},
            "networks": [],
            "volumes": [
                {"name": "rootdisk", "persistentVolumeClaim": {"claimName": "r"}},
                {"name": "installcd", "persistentVolumeClaim": {"claimName": "cd"}},
            ],
        }}},
    }
    out = _topology_vm(vm, {})
    devices = {v["disk"]: v["device"] for v in out["volumes"]}
    assert devices == {"rootdisk": "disk", "installcd": "cdrom", "san": "lun"}


# ---------------------------------------------------------------------------
# v1.40.0 : la vue Cluster est la seule restée en graphe
# ---------------------------------------------------------------------------
def test_the_cluster_view_costs_one_grouped_call(monkeypatch):
    """Elle lançait huit kubectl à chaque rafraîchissement, dont cinq pour
    des volumes, des répliques et des images qu'elle n'affichait pas :
    Réseau et Stockage ont désormais leurs propres points d'accès."""
    calls = []

    def fake(kc, *args, **kw):
        calls.append(args)
        return {"items": [
            {"kind": "Node", "metadata": {"name": "n1", "labels": {}},
             "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"kind": "VirtualMachine", "metadata": {"name": "web", "namespace": "default"},
             "spec": {"runStrategy": "Always", "template": {"spec": {}}}},
            {"kind": "VirtualMachineInstance",
             "metadata": {"name": "web", "namespace": "default"},
             "status": {"phase": "Running", "nodeName": "n1"}},
        ]}
    app_module._topology_missing.clear()
    monkeypatch.setattr(app_module, "_kubectl_json", fake)
    out = app_module._build_topology("c", "/kc")
    assert len(calls) == 1
    assert calls[0][calls[0].index("-A") + 1] == ",".join(app_module.TOPOLOGY_KINDS)
    assert [n["name"] for n in out["nodes"]] == ["n1"]
    assert out["vms"][0]["node"] == "n1" and out["vms"][0]["phase"] == "Running"
    assert "volumes" not in out


# ---------------------------------------------------------------------------
# v1.43.0 : la vue Cluster montre ce que chaque VM consomme
# ---------------------------------------------------------------------------
GIB = 1024 ** 3


def harv_vm(name="web", ns="default", cpu=None, memory=None, resources=None,
            disks=None, volumes=None, networks=None, interfaces=None):
    """Une VM au format de harv1 (Harvester 1.8)."""
    domain = {"devices": {"disks": disks if disks is not None else [
                  {"name": "rootdisk", "disk": {"bus": "virtio"}, "bootOrder": 1},
                  {"name": "cloudinitdisk", "disk": {"bus": "virtio"}}],
                          "interfaces": interfaces or [
                  {"name": "nic-1", "bridge": {}, "model": "virtio",
                   "macAddress": "02:00:00:00:00:01"}]}}
    if cpu is not None:
        domain["cpu"] = cpu
    if memory is not None:
        domain["memory"] = memory
    if resources is not None:
        domain["resources"] = resources
    return {"kind": "VirtualMachine",
            "metadata": {"name": name, "namespace": ns, "uid": "u-" + name},
            "spec": {"runStrategy": "RerunOnFailure", "template": {"spec": {
                "domain": domain,
                "networks": networks or [{"name": "nic-1",
                                          "multus": {"networkName": "default/production"}}],
                "volumes": volumes if volumes is not None else [
                    {"name": "rootdisk", "persistentVolumeClaim": {"claimName": name + "-root"}},
                    {"name": "cloudinitdisk", "cloudInitNoCloud": {"userData": "#cloud-config"}}],
            }}}}


def harv_vmi(name="web", ns="default", node="harv1", ips=("172.16.3.43",), os_name=None):
    st = {"phase": "Running", "nodeName": node,
          "interfaces": [{"name": "nic-1", "mac": "ce:0f:db:1f:33:ee",
                          "ipAddress": ips[0] if ips else None,
                          "ipAddresses": list(ips), "interfaceName": "eth0",
                          "linkState": "up"}]}
    if os_name:
        st["guestOSInfo"] = {"prettyName": os_name}
    return {"kind": "VirtualMachineInstance",
            "metadata": {"name": name, "namespace": ns}, "status": st}


PVCS = {"default/web-root": {"size": 20 * GIB, "storage_class": "longhorn-image-c7rvm"}}


def reduce(vm, vmi=None, pvcs=PVCS):
    key = f"{vm['metadata']['namespace']}/{vm['metadata']['name']}"
    return _topology_vm(vm, {key: vmi} if vmi else {}, pvcs)


def test_vcpu_is_cores_times_sockets_times_threads():
    """Relevé sur harv1 : `cpu: {cores: 2, sockets: 1, threads: 1}`."""
    assert reduce(harv_vm(cpu={"cores": 2, "sockets": 1, "threads": 1}))["vcpu"] == 2
    assert reduce(harv_vm(cpu={"cores": 2, "sockets": 2, "threads": 2}))["vcpu"] == 8
    assert reduce(harv_vm(cpu={"cores": 4}))["vcpu"] == 4


def test_vcpu_falls_back_on_the_cpu_limit():
    assert reduce(harv_vm(resources={"limits": {"cpu": "3"}}))["vcpu"] == 3
    # Des millicœurs : une VM à 1,5 cœur occupe 2 vCPU.
    assert reduce(harv_vm(resources={"limits": {"cpu": "1500m"}}))["vcpu"] == 2
    assert reduce(harv_vm())["vcpu"] == 1


def test_memory_is_what_the_guest_sees_not_the_reservation():
    """harv1 : guest 4Gi, limits 4Gi, requests 2730Mi. Les requests sont une
    réservation surallouée : les afficher ferait croire à 2,7 Gio."""
    vm = harv_vm(memory={"guest": "4Gi"},
                 resources={"limits": {"memory": "4Gi"}, "requests": {"memory": "2730Mi"}})
    assert reduce(vm)["memory"] == 4 * GIB
    assert reduce(harv_vm(resources={"limits": {"memory": "8Gi"},
                                     "requests": {"memory": "2Gi"}}))["memory"] == 8 * GIB
    assert reduce(harv_vm(resources={"requests": {"memory": "2Gi"}}))["memory"] == 2 * GIB
    assert reduce(harv_vm())["memory"] is None


def test_disks_carry_their_size_class_and_boot_order():
    v = reduce(harv_vm())
    root, cloudinit = v["disks"]
    assert root == {"disk": "rootdisk", "device": "disk", "boot_order": 1,
                    "source": "pvc", "pvc": "web-root", "size": 20 * GIB,
                    "storage_class": "longhorn-image-c7rvm"}
    # Le disque cloud-init n'est pas un volume : pas de taille à compter.
    assert cloudinit["source"] == "cloudinit" and cloudinit["size"] is None
    assert v["disk_total"] == 20 * GIB


def test_a_cdrom_and_a_missing_claim_are_told_apart():
    vm = harv_vm(disks=[{"name": "cd", "cdrom": {"bus": "sata"}},
                        {"name": "gone", "disk": {"bus": "virtio"}}],
                 volumes=[{"name": "cd", "persistentVolumeClaim": {"claimName": "iso"}},
                          {"name": "gone", "persistentVolumeClaim": {"claimName": "lost"}}])
    cd, gone = reduce(vm, pvcs={"default/iso": {"size": 5 * GIB, "storage_class": "x"}})["disks"]
    assert cd["device"] == "cdrom" and cd["size"] == 5 * GIB
    assert gone["pvc"] == "lost" and gone["size"] is None


def test_nics_carry_mac_and_addresses_from_the_vmi():
    v = reduce(harv_vm(), harv_vmi(ips=("172.16.3.43", "fe80::1"), os_name="RHEL 9.7"))
    nic = v["nics"][0]
    assert (nic["network"], nic["mac"], nic["ips"]) == (
        "default/production", "ce:0f:db:1f:33:ee", ["172.16.3.43", "fe80::1"])
    assert v["guest_os"] == "RHEL 9.7"


def test_a_stopped_vm_keeps_its_declared_mac():
    nic = reduce(harv_vm())["nics"][0]
    assert nic["mac"] == "02:00:00:00:00:01" and nic["ips"] == []


def node_item(name="harv1", cpu="7020m", memory="65624056Ki", annotations=None,
              unschedulable=False, labels=None):
    return {"kind": "Node",
            "metadata": {"name": name, "annotations": annotations or {},
                         "labels": labels or {"node-role.kubernetes.io/control-plane": "true"}},
            "spec": {"unschedulable": unschedulable},
            "status": {"allocatable": {"cpu": cpu, "memory": memory},
                       "capacity": {"cpu": "8", "memory": memory},
                       "conditions": [{"type": "Ready", "status": "True"}],
                       "addresses": [{"type": "InternalIP", "address": "172.16.3.11"}]}}


def build_items(items, monkeypatch):
    app_module._topology_missing.clear()
    monkeypatch.setattr(app_module, "_kubectl_json", lambda kc, *a, **k: {"items": items})
    return app_module._build_topology("c", "/kc")


def test_a_host_says_what_it_can_give_and_what_is_given(monkeypatch):
    items = [node_item(),
             harv_vm("web", cpu={"cores": 2}, memory={"guest": "4Gi"}), harv_vmi("web"),
             harv_vm("db", cpu={"cores": 4}, memory={"guest": "8Gi"}), harv_vmi("db"),
             harv_vm("idle", cpu={"cores": 16}, memory={"guest": "64Gi"})]   # arrêtée
    host = build_items(items, monkeypatch)["nodes"][0]
    assert host["cpu_allocatable"] == pytest.approx(7.02)
    assert host["memory_allocatable"] == 65624056 * 1024
    # Seules les VMs en marche SUR cet hôte comptent.
    assert host["vcpu_allocated"] == 6
    assert host["memory_allocated"] == 12 * GIB


@pytest.mark.parametrize("annotations,state", [
    ({}, None),
    ({"harvesterhci.io/drain-requested": "true"}, "requested"),
    ({"harvesterhci.io/maintain-status": "running"}, "running"),
    ({"harvesterhci.io/maintain-status": "completed"}, "completed"),
])
def test_a_host_tells_its_maintenance_state(monkeypatch, annotations, state):
    host = build_items([node_item(annotations=annotations)], monkeypatch)["nodes"][0]
    assert host["maintenance"] == state


def test_a_host_tells_whether_it_is_the_last_available(monkeypatch):
    """Harvester refuse d'isoler le dernier nœud disponible : la vue le sait
    pour le dire avant qu'on essaie (relevé en réel sur harv1)."""
    alone = build_items([node_item()], monkeypatch)["nodes"][0]
    assert alone["last_available"] is True
    two = build_items([node_item(), node_item(name="harv2")], monkeypatch)["nodes"]
    assert [n["last_available"] for n in two] == [False, False]
    one_cordoned = build_items([node_item(), node_item(name="harv2", unschedulable=True)],
                               monkeypatch)["nodes"]
    assert [n["last_available"] for n in one_cordoned] == [True, False]


def test_the_view_still_costs_one_call_with_the_claims():
    """Les tailles de disque viennent des PVC, dans le même appel. Les
    répliques Longhorn, elles, ne servent qu'au contrôle de maintenance, lu
    à la demande : pas à chaque rafraîchissement de 8 secondes."""
    assert "persistentvolumeclaims" in app_module.TOPOLOGY_KINDS
    assert "replicas.longhorn.io" not in app_module.TOPOLOGY_KINDS


def test_the_claim_sizes_come_from_the_same_call(monkeypatch):
    pvc = {"kind": "PersistentVolumeClaim",
           "metadata": {"name": "web-root", "namespace": "default"},
           "spec": {"storageClassName": "harv-rep1",
                    "resources": {"requests": {"storage": "20Gi"}}}}
    out = build_items([node_item(), harv_vm("web"), harv_vmi("web"), pvc], monkeypatch)
    root = out["vms"][0]["disks"][0]
    assert (root["size"], root["storage_class"]) == (20 * GIB, "harv-rep1")
