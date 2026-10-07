"""v1.87.0 : une vague dont les disques ne tiennent pas, dite au lieu d'attendre.

Vu en réel le 07/10/2026 (VM Windows de 40 Gio vers harvlab2) : Longhorn ne
pouvait pas placer le disque, le volume restait détaché, l'importeur
attendait et la vague affichait « copie des disques 0 % » sans un mot.
Désormais : refus à la composition et au premier lancement, avec les
chiffres ; et une copie arrêtée faute de volume le dit."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_forklift_b2_cli_176 as T  # noqa: E402
from test_forklift_b2_cli_176 import FakeKube, hf, hfk, ns  # noqa: E402

GIB = 1024 ** 3
NOW = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)


def with_longhorn(k, maximum=52 * GIB, scheduled=21 * GIB, available=34 * GIB, over="100", replicas="1",
                  sc="harvester-longhorn", nodes=1):
    for i in range(nodes):
        k.put("nodes.longhorn.io", "longhorn-system", f"n{i}", {
            "metadata": {"name": f"n{i}"},
            "spec": {"disks": {"d": {"path": "/var/lib/harvester/defaultdisk", "allowScheduling": True,
                                     "storageReserved": 0}}},
            "status": {"diskStatus": {"d": {"storageMaximum": maximum, "storageScheduled": scheduled,
                                            "storageAvailable": available,
                                            "conditions": [{"type": "Schedulable", "status": "True"},
                                                           {"type": "Ready", "status": "True"}]}}}})
    k.put("settings.longhorn.io", "longhorn-system", "storage-over-provisioning-percentage", {"value": over})
    k.put("settings.longhorn.io", "longhorn-system", "storage-minimal-available-percentage", {"value": "25"})
    k.put("storageclasses", None, sc, {"metadata": {"name": sc}, "provisioner": "driver.longhorn.io",
                                       "parameters": {"numberOfReplicas": replicas}})


ROWS = [{"id": "vm-19", "name": "win", "disks": [{"datastore": "datastore-12", "capacity": 40 * GIB}]},
        {"id": "vm-16", "name": "deb", "disks": [{"datastore": "datastore-12", "capacity": 10 * GIB}]}]


# -- calcul pur --------------------------------------------------------------------

def test_a_new_volume_is_limited_by_over_provisioning_not_by_free_space():
    """Chiffres réels de harvlab2 le 07/10/2026 : un disque de 40 Gio placé
    par Longhorn à 200 % de sur-provisionnement avec 19,5 Gio de « place
    libre » ; refusé à 100 % (28,9 Gio de marge)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin" / "lib"))
    import longhorn_room
    k = FakeKube()
    with_longhorn(k, maximum=int(48.9 * GIB), scheduled=20 * GIB, available=int(31.7 * GIB))
    nodes = k.list("nodes.longhorn.io", "longhorn-system")
    sc = k.list("storageclasses")
    at200 = longhorn_room.storage_room(nodes, sc, 200, 25, new_volume=True)["classes"]["harvester-longhorn"]
    at100 = longhorn_room.storage_room(nodes, sc, 100, 25, new_volume=True)["classes"]["harvester-longhorn"]
    assert at200["allocatable"] > 40 * GIB > at100["allocatable"] > 28 * GIB
    # le calcul prudent (création et transfert de VM) reste inchangé
    assert longhorn_room.storage_room(nodes, sc, 200, 25)["classes"]["harvester-longhorn"]["allocatable"] < 20 * GIB
    # sous le seuil de place libre, plus rien n'est plaçable
    k2 = FakeKube()
    with_longhorn(k2, maximum=100 * GIB, scheduled=0, available=20 * GIB)
    assert longhorn_room.storage_room(k2.list("nodes.longhorn.io", "longhorn-system"), sc, 200, 25,
                                      new_volume=True)["classes"]["harvester-longhorn"]["allocatable"] == 0

def test_room_shortfall_counts_the_disks_of_the_wave_only():
    classes = {"rep1": {"allocatable": 30 * GIB}}
    assert hf.room_shortfall(ROWS, ["vm-16"], {"datastore-12": "rep1"}, classes) == []
    assert hf.room_shortfall(ROWS, ["vm-19", "vm-16"], {"datastore-12": "rep1"}, classes) == \
        [("rep1", 50 * GIB, 30 * GIB)]


def test_room_shortfall_uses_the_degraded_room_and_ignores_unknown_classes():
    classes = {"rep3": {"allocatable": 0, "degraded_allocatable": 60 * GIB}}
    assert hf.room_shortfall(ROWS, ["vm-19"], {"datastore-12": "rep3"}, classes) == []
    assert hf.room_shortfall(ROWS, ["vm-19"], {"datastore-12": "lvm"}, classes) == []


def test_room_refusal_gives_the_figures_and_what_to_do():
    msg = hf.room_refusal([("harv-rep1", 40 * GIB, int(18.5 * GIB))])
    assert "storage class harv-rep1 can place 18.5 GiB, the disks of this wave need 40.0 GiB" in msg
    assert "storage-over-provisioning-percentage" in msg and "—" not in msg
    assert hf.room_refusal([]) is None


def plan(uid="p-1"):
    return {"metadata": {"name": "win-a", "uid": uid}}


def pvc(name, vm="vm-19", uid="p-1"):
    return {"metadata": {"name": name, "labels": {"plan": uid, "vmID": vm}}}


def pod(name, claim):
    return {"metadata": {"name": name}, "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": claim}}]}}


def event(kind, name, msg, ago=60, typ="Warning"):
    t = (NOW - timedelta(seconds=ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"type": typ, "involvedObject": {"kind": kind, "name": name}, "message": msg, "lastTimestamp": t}


ATTACH = ('AttachVolume.Attach failed for volume "pvc-019d" : rpc error: code = Aborted desc = volume pvc-019d is '
          'not ready for workloads: volume is currently in detached state with some un-schedulable replicas')


def test_disk_blockers_follow_the_importer_pod_to_its_vm():
    """Réel : l'événement porte sur le pod d'import, qui monte le volume
    intermédiaire prime-…, lui-même étiqueté plan et vmID par Forklift."""
    pvcs = [pvc("win-a-vm-19-tzxzm"), pvc("prime-5443"), pvc("other", uid="p-2")]
    pods = [pod("importer-prime-5443-checkpoint", "prime-5443")]
    out = hf.disk_blockers(plan(), pvcs, pods, [event("Pod", "importer-prime-5443-checkpoint", ATTACH)], now=NOW)
    assert list(out) == ["vm-19"] and "insufficient storage" in out["vm-19"]
    assert "storage-over-provisioning-percentage" in out["vm-19"]


def test_disk_blockers_ignore_old_unrelated_and_foreign_events():
    pvcs = [pvc("prime-5443"), pvc("x", vm="vm-7", uid="p-2")]
    pods = [pod("importer-prime-5443-c", "prime-5443")]
    evs = [event("Pod", "importer-prime-5443-c", ATTACH, ago=3600),                 # trop ancien
           event("Pod", "importer-prime-5443-c", "Back-off pulling image"),         # autre cause
           event("PersistentVolumeClaim", "x", ATTACH),                             # autre plan
           event("Pod", "importer-prime-5443-c", ATTACH, typ="Normal")]
    assert hf.disk_blockers(plan(), pvcs, pods, evs, now=NOW) == {}


def stalled_wave():
    """Une vague en copie dont le disque n'a pas commencé (forme réelle du
    statut Forklift : DiskTransfer démarrée, 0 sur 40960 Mio)."""
    vm = {"id": "vm-19", "name": "win", "phase": "CopyDisks", "started": "2026-10-07T14:43:10Z",
          "pipeline": [{"name": "Initialize", "phase": "Completed", "started": "2026-10-07T14:43:10Z",
                        "completed": "2026-10-07T14:43:50Z", "progress": {"completed": 0, "total": 0}},
                       {"name": "DiskTransfer", "phase": "Running", "started": "2026-10-07T14:43:50Z",
                        "progress": {"completed": 0, "total": 40960}}]}
    p = {"metadata": {"name": "win-a", "uid": "p-1",
                      "labels": {hf.L_MANAGED: "true", hf.L_WAVE: "win-a"}},
         "spec": {"vms": [{"id": "vm-19"}], "targetNamespace": "mig-lanes"},
         "status": {"conditions": [{"type": "Ready", "status": "True"}, {"type": "Executing", "status": "True"}],
                    "migration": {"vms": [vm]}}}
    return p


def test_a_blocked_copy_is_said_by_the_vm_and_the_wave():
    p = stalled_wave()
    st = hf.wave_state(p, [], blockers={"vm-19": "the target storage cannot place this disk"})
    vm = st["vms"][0]
    assert st["state"] == "copying" and vm["progress"]["done"] == 0
    assert vm["blocked"] == "the target storage cannot place this disk"
    assert st["message"] == "win: the target storage cannot place this disk"
    assert hf.wave_state(p, [])["vms"][0]["blocked"] == ""
    # la copie a commencé : plus de blocage affiché, même si un vieil événement traîne
    p["status"]["migration"]["vms"][0]["pipeline"][1]["progress"]["completed"] = 512
    assert hf.wave_state(p, [], blockers={"vm-19": "x"})["vms"][0]["blocked"] == ""


def test_wave_status_reads_the_blockers_only_for_a_stalled_disk(capsys):
    k = FakeKube()
    p = stalled_wave()
    k.put(hf.K_PLAN, hf.NS, "win-a", p)
    k.put("persistentvolumeclaims", "mig-lanes", "prime-5443", pvc("prime-5443"))
    k.put("pods", "mig-lanes", "importer-prime-5443-c", pod("importer-prime-5443-c", "prime-5443"))
    ev = event("Pod", "importer-prime-5443-c", ATTACH, ago=30)
    ev["lastTimestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    k.put("events", "mig-lanes", "e1", ev)
    assert hfk.cmd_wave_status(ns(wave="win-a"), kube=k) == hfk.EXIT_OK
    import json
    out = json.loads(capsys.readouterr().out)
    assert "insufficient storage" in out["vms"][0]["blocked"] and "insufficient storage" in out["message"]


# -- ligne de commande -------------------------------------------------------------------

def test_wave_apply_refuses_disks_that_do_not_fit(tmp_path, capsys):
    k = FakeKube()
    with_longhorn(k, maximum=20 * GIB, scheduled=15 * GIB, available=18 * GIB)
    assert T.apply_wave(k, T.WAVE, T.inventory_items(), tmp_path) == hfk.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "storage class harvester-longhorn can place" in err and "the disks of this wave need 10.0 GiB" in err
    assert not [c for c in k.calls if c[0] == "apply"]


def test_wave_apply_passes_when_the_disks_fit(tmp_path, capsys):
    k = FakeKube()
    with_longhorn(k, maximum=200 * GIB, scheduled=0, available=190 * GIB)
    assert T.apply_wave(k, T.WAVE, T.inventory_items(), tmp_path) == hfk.EXIT_OK


def test_wave_start_checks_the_room_on_its_first_migration_only(tmp_path, capsys):
    k = FakeKube()
    with_longhorn(k, maximum=200 * GIB, scheduled=0, available=190 * GIB)
    assert T.apply_wave(k, T.WAVE, T.inventory_items(), tmp_path) == hfk.EXIT_OK
    with_longhorn(k, maximum=20 * GIB, scheduled=15 * GIB, available=18 * GIB)   # la place a fondu
    c = T.Clock()
    rc = hfk.cmd_wave_start(ns(wave="vague-3"), kube=k, sleep=c.sleep, now=c.now,
                            fetch=T.fetcher(T.inventory_items()))
    assert rc == hfk.EXIT_REFUSED
    assert "the disks of this wave need 10.0 GiB" in capsys.readouterr().err
    assert not [x for x in k.calls if x[0] == "create"]
    # une vague qui a déjà ses volumes (une migration a eu lieu) ne compte pas deux fois
    k2 = FakeKube()
    T.with_wave(k2, migs=("vague-1-m3",), plan_conditions=[{"type": "Ready", "status": "True"}])
    with_longhorn(k2, maximum=20 * GIB, scheduled=15 * GIB, available=18 * GIB)
    assert hfk.cmd_wave_start(ns(wave="vague-1"), kube=k2, sleep=c.sleep, now=c.now) == hfk.EXIT_OK
