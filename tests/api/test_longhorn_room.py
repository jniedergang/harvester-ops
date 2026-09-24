"""v1.45.0 : la place allouable Longhorn, partagée entre la console et le CLI.

Le transfert de VM entre clusters doit dire, AVANT de copier quoi que ce
soit, si la cible a la place. Le calcul existait déjà dans la console
(création de VM, vue Stockage) ; le moteur de transfert est un script qui
tourne aussi sans elle. Deux calculs finiraient par diverger : la fonction
quitte donc `web/app.py` pour `bin/lib/longhorn_room.py`, et la console
l'importe sous son ancien nom.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import app as wapp  # noqa: E402
import longhorn_room  # noqa: E402

GIB = 1024 ** 3


def lh_node(name="n1", maximum=100 * GIB, scheduled=0, available=100 * GIB,
            reserved=0, ready=True, schedulable=True, allow=True):
    cond = [{"type": "Ready", "status": "True" if ready else "False"},
            {"type": "Schedulable", "status": "True" if schedulable else "False"}]
    return {"metadata": {"name": name},
            "spec": {"disks": {"d1": {"path": "/var/lib/harvester/defaultdisk",
                                      "allowScheduling": allow,
                                      "storageReserved": reserved, "tags": []}}},
            "status": {"diskStatus": {"d1": {"conditions": cond,
                                             "storageMaximum": maximum,
                                             "storageScheduled": scheduled,
                                             "storageAvailable": available}}}}


def sc(name, replicas="1", provisioner="driver.longhorn.io"):
    return {"metadata": {"name": name}, "provisioner": provisioner,
            "parameters": {"numberOfReplicas": replicas}}


def test_the_console_uses_the_shared_function():
    assert wapp._storage_room is longhorn_room.storage_room


def test_over_provisioning_limits_when_it_is_tighter():
    room = longhorn_room.storage_room(
        [lh_node(scheduled=150 * GIB)], [sc("one")], over=200, minimal=25)
    # (100 * 200 %) - 150 = 50 Gio ; place réelle 100 - 25 = 75 Gio
    assert room["classes"]["one"]["allocatable"] == 50 * GIB
    assert room["disks"][0]["limited_by"] == "over-provisioning"


def test_free_space_limits_when_it_is_tighter():
    room = longhorn_room.storage_room(
        [lh_node(available=40 * GIB)], [sc("one")], over=200, minimal=25)
    # place réelle 40 - 25 = 15 Gio, sur-provisionnement 200 Gio
    assert room["classes"]["one"]["allocatable"] == 15 * GIB
    assert room["disks"][0]["limited_by"] == "free-space"


def test_an_unschedulable_disk_offers_nothing():
    room = longhorn_room.storage_room(
        [lh_node(schedulable=False)], [sc("one")], over=200, minimal=25)
    assert room["disks"][0]["room"] == 0
    assert room["schedulable_nodes"] == 0
    assert room["classes"]["one"]["reason"] == "not enough schedulable nodes"


def test_replicas_need_distinct_nodes():
    nodes = [lh_node("n1", available=100 * GIB), lh_node("n2", available=60 * GIB)]
    room = longhorn_room.storage_room(nodes, [sc("two", "2"), sc("three", "3")],
                                      over=200, minimal=25)
    # deux répliques : la plus serrée des deux meilleures places
    assert room["classes"]["two"]["allocatable"] == 35 * GIB
    assert room["classes"]["three"]["allocatable"] == 0
    assert room["classes"]["three"]["reason"] == "not enough schedulable nodes"


def test_non_longhorn_classes_are_ignored():
    room = longhorn_room.storage_room([lh_node()], [sc("nfs", provisioner="nfs.csi.k8s.io")],
                                      over=200, minimal=25)
    assert room["classes"] == {}
