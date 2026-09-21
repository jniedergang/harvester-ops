"""v1.40.0 : le stockage lu comme un datastore, côté serveur.

La vue Stockage reprend la présentation de la Fabrique : les storage
classes et les disques de VM à gauche, le moteur au milieu, les disques des
nœuds à droite. Ce que le serveur doit lui donner, et que l'ancienne vue
ignorait :

  * QUI consomme chaque volume : une VM (même arrêtée, par sa spec) ou un
    POD. L'ancienne vue rangeait parmi les « non rattachés », avec un
    bouton de suppression, la base de Prometheus que montait un pod ;
  * où vivent les répliques, disque par disque, et la place qui reste,
    calculée par la MÊME fonction que le panneau de création de VM ;
  * le tout en UN appel kubectl groupé, contre huit auparavant.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402

GIB = 1024 ** 3


@pytest.fixture(autouse=True)
def _fresh():
    wapp._storage_missing.clear()
    wapp._storage_map_cache.clear()
    yield
    wapp._storage_missing.clear()
    wapp._storage_map_cache.clear()


def pvc(name, ns="default", sc="harv-rep1", size="10Gi"):
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": ns},
            "spec": {"storageClassName": sc,
                     "resources": {"requests": {"storage": size}}},
            "status": {"phase": "Bound"}}


def sc(name, replicas="1", default=False, backing=None):
    params = {"numberOfReplicas": replicas}
    if backing:
        params["backingImage"] = backing
    ann = {"storageclass.kubernetes.io/is-default-class": "true"} if default else {}
    return {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": name, "annotations": ann},
            "provisioner": "driver.longhorn.io", "parameters": params,
            "reclaimPolicy": "Delete", "volumeBindingMode": "Immediate",
            "allowVolumeExpansion": True}


def lh_node(name="n1", maximum=1000 * GIB, available=800 * GIB, scheduled=100 * GIB):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Node",
            "metadata": {"name": name, "namespace": "longhorn-system"},
            "spec": {"disks": {"d1": {"path": "/var/lib/harvester/defaultdisk",
                                      "allowScheduling": True,
                                      "storageReserved": 0, "tags": []}}},
            "status": {"diskStatus": {"d1": {
                "diskUUID": "uuid-1", "diskPath": "/var/lib/harvester/defaultdisk",
                "storageMaximum": maximum, "storageAvailable": available,
                "storageScheduled": scheduled,
                "conditions": [{"type": "Ready", "status": "True"},
                               {"type": "Schedulable", "status": "True"}]}}}}


def lh_volume(name, claim, ns="default", state="detached", workloads=None,
              last_ref="", backing=None):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
            "metadata": {"name": name, "namespace": "longhorn-system"},
            "spec": {"size": str(10 * GIB), "numberOfReplicas": 1,
                     "backingImage": backing},
            "status": {"state": state, "robustness": "healthy",
                       "kubernetesStatus": {"pvcName": claim, "namespace": ns,
                                            "workloadsStatus": workloads or [],
                                            "lastPodRefAt": last_ref}}}


def replica(vol, disk_uuid="uuid-1", node="n1", running=False):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Replica",
            "metadata": {"name": vol + "-r-1"},
            "spec": {"volumeName": vol, "diskID": disk_uuid, "nodeID": node},
            "status": {"currentState": "running" if running else "stopped"}}


def vm(name, claims, ns="default", status="Stopped"):
    vols = [{"name": f"d{i}", "persistentVolumeClaim": {"claimName": c}}
            for i, c in enumerate(claims)]
    disks = [{"name": f"d{i}", "bootOrder": i + 1, "disk": {"bus": "virtio"}}
             for i, _ in enumerate(claims)]
    return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
            "metadata": {"name": name, "namespace": ns},
            "spec": {"template": {"spec": {"volumes": vols,
                                           "domain": {"devices": {"disks": disks}}}}},
            "status": {"printableStatus": status}}


def setting(name, value, longhorn=True):
    return {"apiVersion": "longhorn.io/v1beta2" if longhorn else "harvesterhci.io/v1beta1",
            "kind": "Setting", "metadata": {"name": name}, "value": value}


def build(items, monkeypatch, calls=None):
    def fake(kc, *args, **kw):
        if calls is not None:
            calls.append(args)
        return {"items": items}
    monkeypatch.setattr(wapp, "_kubectl_json", fake)
    return wapp._build_storage_map("c", "/kc")


def vol(out, claim):
    return next(v for v in out["volumes"] if v["pvc_name"] == claim)


# ---------------------------------------------------------------------------
# Un appel
# ---------------------------------------------------------------------------

def test_the_whole_map_is_one_kubectl_call(monkeypatch):
    calls = []
    build([pvc("a"), sc("harv-rep1")], monkeypatch, calls)
    assert len(calls) == 1
    assert calls[0][calls[0].index("-A") + 1] == ",".join(wapp.STORAGE_KINDS)


# ---------------------------------------------------------------------------
# Qui consomme quoi
# ---------------------------------------------------------------------------

def test_a_vm_disk_is_joined_to_its_claim(monkeypatch):
    out = build([pvc("root"), sc("harv-rep1"), lh_volume("pvc-1", "root"),
                 vm("web", ["root"])], monkeypatch)
    v = vol(out, "root")
    assert (v["vm"], v["disk"], v["device"], v["boot_order"]) == \
        ("default/web", "d0", "disk", 1)
    assert v["orphan"] is False


def test_a_claim_of_a_stopped_vm_is_not_an_orphan(monkeypatch):
    """Une VM arrêtée n'a pas de pod : c'est sa spec qui dit qu'elle tient
    son disque. Sans elle, le disque d'une VM éteinte passerait pour
    orphelin, et supprimable."""
    out = build([pvc("root"), sc("harv-rep1"), lh_volume("pvc-1", "root"),
                 vm("idle", ["root"], status="Stopped")], monkeypatch)
    assert vol(out, "root")["orphan"] is False


def test_a_claim_mounted_by_a_pod_is_not_an_orphan(monkeypatch):
    """Le défaut que cette version corrige : la base Prometheus, montée par
    un pod et réclamée par aucune VM, était proposée à la suppression."""
    out = build([pvc("prom-db", ns="mon"), sc("harv-rep1"),
                 lh_volume("pvc-1", "prom-db", ns="mon", state="attached",
                           workloads=[{"podName": "prometheus-0", "podStatus": "Running",
                                       "workloadName": "prometheus",
                                       "workloadType": "StatefulSet"}])],
                monkeypatch)
    v = vol(out, "prom-db")
    assert v["orphan"] is False
    assert v["pods"][0]["name"] == "prometheus-0"


def test_the_vm_pod_is_not_counted_as_another_consumer(monkeypatch):
    """Le pod de la VM, et celui d'un attachement à chaud, sont la VM
    elle-même : les compter en plus doublerait chaque consommateur."""
    out = build([pvc("root"), sc("harv-rep1"),
                 lh_volume("pvc-1", "root", state="attached", workloads=[
                     {"podName": "virt-launcher-web-abcde", "podStatus": "Running",
                      "workloadName": "web", "workloadType": "VirtualMachineInstance"},
                     # Le pod d'attachement à chaud ne se déclare pas comme
                     # une VM : seul son nom le trahit.
                     {"podName": "hp-volume-xk2p9", "podStatus": "Running",
                      "workloadName": "", "workloadType": ""}]),
                 vm("web", ["root"], status="Running")], monkeypatch)
    assert vol(out, "root")["pods"] == []


def test_a_pod_consumer_wins_even_before_the_volume_is_attached(monkeypatch):
    """Pendant l'attachement, le volume n'est pas encore `attached` mais un
    pod le réclame déjà : ce n'est pas un orphelin."""
    out = build([pvc("db"), sc("harv-rep1"),
                 lh_volume("pvc-1", "db", state="attaching", workloads=[
                     {"podName": "db-0", "podStatus": "Pending",
                      "workloadName": "db", "workloadType": "StatefulSet"}])],
                monkeypatch)
    assert vol(out, "db")["orphan"] is False


def test_a_leftover_says_which_workload_last_used_it(monkeypatch):
    """Orphelin aujourd'hui, mais sa StatefulSet peut revenir le réclamer :
    l'exploitant doit le savoir AVANT de supprimer."""
    out = build([pvc("prom-db", ns="mon"), sc("harv-rep1"),
                 lh_volume("pvc-1", "prom-db", ns="mon", last_ref="2026-08-25T13:49:18Z",
                           workloads=[{"podName": "prometheus-0",
                                       "workloadName": "prometheus",
                                       "workloadType": "StatefulSet"}])],
                monkeypatch)
    v = vol(out, "prom-db")
    assert v["pods"] == []
    assert v["orphan"] is True
    assert v["last_pods"][0]["workload"] == "prometheus"
    assert v["last_pods"][0]["at"] == "2026-08-25T13:49:18Z"


def test_an_attached_volume_is_never_offered(monkeypatch):
    """Attaché sans consommateur connu : on ne sait pas qui l'utilise, donc
    on ne propose rien."""
    out = build([pvc("x"), sc("harv-rep1"),
                 lh_volume("pvc-1", "x", state="attached")], monkeypatch)
    assert vol(out, "x")["orphan"] is False


def test_a_claim_unknown_to_longhorn_is_never_offered(monkeypatch):
    """Sans le volume Longhorn, on ne SAIT pas qui le monte."""
    out = build([pvc("nfs-data", sc="nfs"), sc("harv-rep1")], monkeypatch)
    assert vol(out, "nfs-data")["orphan"] is False


def test_a_longhorn_volume_without_claim_is_shown_but_not_offered(monkeypatch):
    lone = lh_volume("pvc-lone", None)
    lone["status"]["kubernetesStatus"] = {}
    out = build([sc("harv-rep1"), lone], monkeypatch)
    v = next(x for x in out["volumes"] if x["longhorn"] == "pvc-lone")
    assert v["pvc_name"] is None and v["orphan"] is False


# ---------------------------------------------------------------------------
# Où vivent les répliques, et la place qui reste
# ---------------------------------------------------------------------------

def test_a_replica_lands_on_its_disk_by_uuid(monkeypatch):
    out = build([pvc("root"), sc("harv-rep1"), lh_node(),
                 lh_volume("pvc-1", "root"), replica("pvc-1", running=True)],
                monkeypatch)
    assert vol(out, "root")["replicas"] == [{"node": "n1", "disk": "d1",
                                             "running": True}]
    d = out["disks"][0]
    assert (d["node"], d["disk"], d["replicas"]) == ("n1", "d1", 1)
    assert d["path"] == "/var/lib/harvester/defaultdisk"
    assert d["used"] == 200 * GIB


def test_the_room_is_the_same_as_the_vm_creation_panel(monkeypatch):
    """Deux calculs de « place restante » finiraient par diverger, et
    l'exploitant lirait deux chiffres pour la même question."""
    items = [sc("harv-rep1"), sc("three", replicas="3"), lh_node(),
             setting("storage-over-provisioning-percentage", "150"),
             setting("storage-minimal-available-percentage", "10")]
    out = build(items, monkeypatch)
    ref = wapp._storage_room([lh_node()], [sc("harv-rep1"), sc("three", replicas="3")],
                             150.0, 10.0)
    by = {c["name"]: c for c in out["classes"]}
    assert by["harv-rep1"]["allocatable"] == ref["classes"]["harv-rep1"]["allocatable"] > 0
    # Trois répliques sur un seul nœud : rien d'allouable, et la raison dite.
    assert by["three"]["allocatable"] == 0
    assert by["three"]["reason"] == "not enough schedulable nodes"
    assert out["over_provisioning_pct"] == 150.0
    assert out["minimal_available_pct"] == 10.0


def test_a_harvester_setting_does_not_pass_for_a_longhorn_one(monkeypatch):
    """`Setting` existe aussi chez Harvester : un homonyme ne doit pas
    fausser le calcul."""
    items = [sc("harv-rep1"), lh_node(),
             setting("storage-over-provisioning-percentage", "999", longhorn=False)]
    assert build(items, monkeypatch)["over_provisioning_pct"] == 200.0


def test_a_kubernetes_node_is_not_a_longhorn_disk(monkeypatch):
    core = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "n1"},
            "status": {}}
    out = build([sc("harv-rep1"), lh_node(), core], monkeypatch)
    assert len(out["disks"]) == 1


# ---------------------------------------------------------------------------
# Les classes
# ---------------------------------------------------------------------------

def test_an_image_class_names_its_image_and_the_default_is_flagged(monkeypatch):
    bi = {"apiVersion": "longhorn.io/v1beta2", "kind": "BackingImage",
          "metadata": {"name": "vmi-1",
                       "annotations": {"harvesterhci.io/imageId": "default/img-1"}}}
    img = {"apiVersion": "harvesterhci.io/v1beta1", "kind": "VirtualMachineImage",
           "metadata": {"name": "img-1", "namespace": "default"},
           "spec": {"displayName": "debian-13-netinst.iso"}}
    out = build([sc("lh-1", backing="vmi-1"), sc("harv-rep1", default=True), bi, img],
                monkeypatch)
    by = {c["name"]: c for c in out["classes"]}
    assert by["lh-1"]["image"] == "debian-13-netinst.iso"
    assert by["harv-rep1"]["default"] is True and by["lh-1"]["default"] is False


def test_an_iso_is_recognised_by_its_url_alone(monkeypatch):
    """Un nom d'affichage sans extension (l'image autounattend) mais une
    URL en .ISO : c'est bien un disque optique."""
    bi = {"apiVersion": "longhorn.io/v1beta2", "kind": "BackingImage",
          "metadata": {"name": "vmi-c",
                       "annotations": {"harvesterhci.io/imageId": "default/i"}}}
    img = {"apiVersion": "harvesterhci.io/v1beta1", "kind": "VirtualMachineImage",
           "metadata": {"name": "i", "namespace": "default"},
           "spec": {"displayName": "win2025-autounattend",
                    "url": "http://x/autounattend.ISO"}}
    out = build([pvc("inst"), sc("harv-rep1"), bi, img,
                 lh_volume("pvc-1", "inst", backing="vmi-c")], monkeypatch)
    v = vol(out, "inst")
    assert v["image"] == "win2025-autounattend" and v["image_iso"] is True


def test_the_requested_size_is_read_in_bytes():
    assert wapp._k8s_bytes("10Gi") == 10 * GIB
    assert wapp._k8s_bytes("1Ti") == 1024 * GIB
    assert wapp._k8s_bytes("500M") == 500 * 10 ** 6
    assert wapp._k8s_bytes("nonsense") is None


# ---------------------------------------------------------------------------
# Supprimer un PVC : le serveur revérifie
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    wapp.app.config["TESTING"] = True
    return wapp.app.test_client()


def fake_cluster(monkeypatch, pods, tracked):
    def fake(kc, *args, **kw):
        if args[:2] == ("get", "vm"):
            return {"items": []}
        if args[:2] == ("get", "pods"):
            return pods
        raise AssertionError(args)
    monkeypatch.setattr(wapp, "_kubectl_json", fake)
    monkeypatch.setattr(wapp, "track_action",
                        lambda *a, **k: tracked.append(a) or "act1")


def pod(name, claim, phase="Running"):
    return {"metadata": {"name": name}, "status": {"phase": phase},
            "spec": {"volumes": [{"name": "data",
                                  "persistentVolumeClaim": {"claimName": claim}}]}}


def test_a_claim_mounted_by_a_pod_is_refused(client, monkeypatch):
    tracked = []
    fake_cluster(monkeypatch, {"items": [pod("prometheus-0", "prom-db")]}, tracked)
    r = client.delete("/api/pvc/c1/mon/prom-db")
    assert r.status_code == 409
    assert "prometheus-0" in r.get_json()["detail"]
    assert not tracked, "la suppression est partie quand même"


def test_when_pods_cannot_be_listed_nothing_is_deleted(client, monkeypatch):
    """Dans le doute, rien n'est supprimé."""
    tracked = []
    fake_cluster(monkeypatch, None, tracked)
    r = client.delete("/api/pvc/c1/mon/prom-db")
    assert r.status_code == 503
    assert not tracked


def test_a_finished_pod_does_not_hold_the_claim(client, monkeypatch):
    tracked = []
    fake_cluster(monkeypatch, {"items": [pod("job-1", "scratch", phase="Succeeded")]},
                 tracked)
    r = client.delete("/api/pvc/c1/ns1/scratch")
    assert r.status_code == 201 and tracked


def test_a_true_orphan_is_deleted_as_a_tracked_action(client, monkeypatch):
    tracked = []
    fake_cluster(monkeypatch, {"items": [pod("other", "another-claim")]}, tracked)
    r = client.delete("/api/pvc/c1/ns1/orphan")
    assert r.status_code == 201
    assert r.get_json()["action_id"] == "act1"
