"""v1.45.0 : les décisions du transfert de VM entre clusters.

Tout ce qui décide (nettoyer une VM, trouver ce qu'elle utilise, proposer
des correspondances, dire si un transfert est possible, choisir le moteur,
écrire et relire l'archive) vit dans `bin/lib/vm_transfer.py`, sans accès
au cluster. Les objets sont au format relevé sur harv1 (v1.9.0) : la VM
leap156, un disque né d'une image, un secret cloud-init.
"""

import copy
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import vm_transfer as vt  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "vm_transfer"
GIB = 1024 ** 3


def leap156():
    return json.loads((FIX / "vm-leap156.json").read_text())


def pvc(name, sc="longhorn-opensuse-leap-cloud", size="10Gi", image="default/opensuse-leap-cloud",
        modes=("ReadWriteMany",), mode="Block"):
    ann = {"pv.kubernetes.io/bind-completed": "yes"}
    if image:
        ann["harvesterhci.io/imageId"] = image
    return {"metadata": {"name": name, "namespace": "default", "annotations": ann},
            "spec": {"accessModes": list(modes), "storageClassName": sc,
                     "volumeMode": mode, "resources": {"requests": {"storage": size}}}}


def inventory(vm=None):
    vm = vm or leap156()
    return vt.vm_inventory(vm, {"leap156-disk-0-gfcec": pvc("leap156-disk-0-gfcec")})


# ---------------------------------------------------------------------------
# Petits outils
# ---------------------------------------------------------------------------

def test_version_tuple():
    assert vt.version_tuple("v1.9.0") == (1, 9, 0)
    assert vt.version_tuple("1.8.2-rc1") == (1, 8, 2)
    assert vt.version_tuple("") == (0, 0, 0)
    assert vt.version_tuple(None) == (0, 0, 0)


def test_parse_quantity():
    assert vt.parse_quantity("10Gi") == 10 * GIB
    assert vt.parse_quantity("512Mi") == 512 * 1024 ** 2
    assert vt.parse_quantity("1G") == 10 ** 9
    assert vt.parse_quantity("1073741824") == GIB
    assert vt.parse_quantity(None) == 0


def test_step_writes_a_single_clean_line(capsys):
    vt.step("export", "running", "disk 1/2\nsecond line | with a pipe")
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith("STEP_EVENT|export|running|disk 1/2 second line")


# ---------------------------------------------------------------------------
# Cibles de sauvegarde
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('{"type":"nfs","endpoint":"172.16.0.5:/volume1/BACKUP/harvester/"}',
     ("nfs", "172.16.0.5:/volume1/BACKUP/harvester")),
    ('{"type":"nfs","endpoint":"nfs://NAS.home.lo:/volume1/BACKUP/harvester"}',
     ("nfs", "nas.home.lo:/volume1/BACKUP/harvester")),
    ({"type": "s3", "endpoint": "https://s3.example:9000/", "bucketName": "hv",
      "bucketRegion": "eu", "credentialSecret": "never-compared"},
     ("s3", "https://s3.example:9000", "hv", "eu")),
    ("", None), (None, None), ("{not json", None), ('{"type":""}', None),
])
def test_normalize_backup_target(raw, expected):
    assert vt.normalize_backup_target(raw) == expected


# ---------------------------------------------------------------------------
# Nettoyage et inventaire
# ---------------------------------------------------------------------------

def test_sanitize_drops_cluster_state_and_controller_annotations():
    vm = leap156()
    vm["metadata"]["labels"][vt.TRANSFER_LABEL] = "old"
    vm["metadata"]["annotations"][vt.TRANSFERRED_TO] = "x/y/z"
    clean = vt.sanitize_vm(vm)
    md = clean["metadata"]
    assert "status" not in clean
    for k in ("uid", "resourceVersion", "creationTimestamp", "generation",
              "managedFields", "finalizers", "ownerReferences"):
        assert k not in md
    ann = md.get("annotations", {})
    assert not any(k.startswith(("kubevirt.io/", "kubectl.kubernetes.io/")) for k in ann)
    for k in ("harvesterhci.io/volumeClaimTemplates", "harvesterhci.io/mac-address",
              "harvesterhci.io/vmRunStrategy", "network.harvesterhci.io/ips",
              vt.TRANSFERRED_TO):
        assert k not in ann
    # ce qui appartient à l'exploitant reste
    assert "harvester-ops.io/shutdown-priority" in ann
    assert md["labels"] == {"harvesterhci.io/creator": "harvester"}
    # l'original n'est pas modifié
    assert "status" in vm


def test_inventory_of_leap156():
    inv = inventory()
    assert inv["disks"] == [{
        "volume": "disk-0", "claim": "leap156-disk-0-gfcec", "size": 10 * GIB,
        "storage_class": "longhorn-opensuse-leap-cloud",
        "access_modes": ["ReadWriteMany"], "volume_mode": "Block",
        "image": "default/opensuse-leap-cloud", "used": None,
    }]
    assert inv["networks"] == ["default/production"]
    assert inv["secrets"] == ["leap156-hd4ry"]
    assert inv["devices"] == []
    assert inv["node_affinity"] is False
    assert inv["run_strategy"] == "Halted"
    assert inv["running"] is False


def test_inventory_sees_devices_and_node_pinning():
    vm = leap156()
    dom = vm["spec"]["template"]["spec"]["domain"]["devices"]
    dom["hostDevices"] = [{"name": "nic1", "deviceName": "intel.com/82574"}]
    dom["gpus"] = [{"name": "gpu1", "deviceName": "nvidia.com/A30"}]
    vm["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "harv1"}
    inv = inventory(vm)
    assert inv["devices"] == ["hostDevice:intel.com/82574", "gpu:nvidia.com/A30"]
    assert inv["node_affinity"] is True


def test_the_network_affinity_is_not_a_node_pinning():
    """Harvester pose `network.harvesterhci.io/mgmt` d'après les réseaux de la
    VM (relevé sur leap156) : ce n'est pas une VM attachée à un nœud."""
    assert inventory()["node_affinity"] is False


# ---------------------------------------------------------------------------
# Correspondances
# ---------------------------------------------------------------------------

def target(networks=("default/production",), classes=("harvester-longhorn", "harv-rep1"),
           default="harvester-longhorn"):
    return {"networks": list(networks),
            "storage_classes": {c: {"replicas": 1, "allocatable": 500 * GIB, "image": False}
                                for c in classes},
            "default_storage_class": default}


def test_default_mappings_keep_names_that_exist():
    m = vt.default_mappings(inventory(), target(), "backup")
    assert m["networks"] == {"default/production": "default/production"}


def test_a_missing_network_is_left_to_the_operator():
    m = vt.default_mappings(inventory(), target(networks=("default/lab",)), "file")
    assert m["networks"] == {"default/production": None}


def test_file_engine_puts_image_disks_on_the_default_class():
    """Le disque arrive entier : le poser dans une classe d'image n'aurait
    aucun sens, et cette classe n'existe pas sur la cible."""
    m = vt.default_mappings(inventory(), target(), "file")
    assert m["storage_classes"] == {"longhorn-opensuse-leap-cloud": "harvester-longhorn"}


def test_file_engine_keeps_a_plain_class_that_exists():
    vm = leap156()
    inv = vt.vm_inventory(vm, {"leap156-disk-0-gfcec": pvc("leap156-disk-0-gfcec", sc="harv-rep1", image=None)})
    m = vt.default_mappings(inv, target(), "file")
    assert m["storage_classes"] == {"harv-rep1": "harv-rep1"}


# ---------------------------------------------------------------------------
# Réécriture pour la cible
# ---------------------------------------------------------------------------

def retarget(vm=None, **kw):
    args = dict(name="leap156-b", namespace="lab", networks={"default/production": "lab/vlan10"},
                claims={"leap156-disk-0-gfcec": "leap156-b-disk-0"},
                secrets={"leap156-hd4ry": "leap156-b-cloudinit"}, keep_mac=False,
                transfer_id="t123")
    args.update(kw)
    return vt.retarget_vm(vt.sanitize_vm(vm or leap156()), **args)


def test_retarget_rewrites_every_reference():
    vm, removed = retarget()
    md, spec = vm["metadata"], vm["spec"]
    assert (md["name"], md["namespace"]) == ("leap156-b", "lab")
    assert md["labels"][vt.TRANSFER_LABEL] == "t123"
    tspec = spec["template"]["spec"]
    assert spec["template"]["metadata"]["labels"]["harvesterhci.io/vmName"] == "leap156-b"
    assert tspec["networks"][0]["multus"]["networkName"] == "lab/vlan10"
    vols = {v["name"]: v for v in tspec["volumes"]}
    assert vols["disk-0"]["persistentVolumeClaim"]["claimName"] == "leap156-b-disk-0"
    ci = vols["cloudinitdisk"]["cloudInitNoCloud"]
    assert ci["secretRef"]["name"] == "leap156-b-cloudinit"
    assert ci["networkDataSecretRef"]["name"] == "leap156-b-cloudinit"
    assert spec["runStrategy"] == "Halted"
    assert "running" not in spec
    assert removed == []


def test_retarget_drops_mac_unless_kept():
    vm, _ = retarget(keep_mac=False)
    ifaces = vm["spec"]["template"]["spec"]["domain"]["devices"]["interfaces"]
    assert all("macAddress" not in i for i in ifaces)
    vm, _ = retarget(keep_mac=True)
    ifaces = vm["spec"]["template"]["spec"]["domain"]["devices"]["interfaces"]
    assert ifaces[0]["macAddress"] == "ca:02:10:ff:0f:61"


def test_retarget_drops_network_affinity_for_the_webhook():
    vm, _ = retarget()
    aff = vm["spec"]["template"]["spec"].get("affinity") or {}
    terms = (((aff.get("nodeAffinity") or {}).get("requiredDuringSchedulingIgnoredDuringExecution") or {})
             .get("nodeSelectorTerms") or [])
    keys = [e["key"] for t in terms for e in t.get("matchExpressions", [])]
    assert not any(k.startswith("network.harvesterhci.io/") for k in keys)


def test_retarget_removes_what_cannot_travel_and_says_so():
    vm = leap156()
    t = vm["spec"]["template"]["spec"]
    t["domain"]["devices"]["hostDevices"] = [{"name": "nic1", "deviceName": "intel.com/82574"}]
    t["domain"]["devices"]["gpus"] = [{"name": "gpu1", "deviceName": "nvidia.com/A30"}]
    t["nodeSelector"] = {"kubernetes.io/hostname": "harv1"}
    t["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"][0]["matchExpressions"].append(
        {"key": "kubernetes.io/hostname", "operator": "In", "values": ["harv1"]})
    out, removed = retarget(vm)
    t = out["spec"]["template"]["spec"]
    assert "hostDevices" not in t["domain"]["devices"]
    assert "gpus" not in t["domain"]["devices"]
    assert "nodeSelector" not in t
    assert removed == ["hostDevice:intel.com/82574", "gpu:nvidia.com/A30", "node-selector",
                       "node-affinity"]


def test_retarget_does_not_modify_its_input():
    clean = vt.sanitize_vm(leap156())
    before = copy.deepcopy(clean)
    vt.retarget_vm(clean, name="x", namespace="y", networks={}, claims={}, secrets={},
                   keep_mac=False, transfer_id="t")
    assert clean == before


# ---------------------------------------------------------------------------
# Manifestes
# ---------------------------------------------------------------------------

def test_restore_manifest_always_halts_and_creates_a_new_vm():
    m = vt.restore_manifest("default", "leap156-xfer-t1", "leap156-b", "lab", True, "t1")
    assert m["kind"] == "VirtualMachineRestore"
    assert m["metadata"]["namespace"] == "lab"
    assert m["metadata"]["labels"][vt.TRANSFER_LABEL] == "t1"
    s = m["spec"]
    assert s["newVM"] is True and s["haltAfterRestore"] is True and s["keepMacAddress"] is True
    assert s["target"] == {"apiGroup": "kubevirt.io", "kind": "VirtualMachine", "name": "leap156-b"}
    assert (s["virtualMachineBackupNamespace"], s["virtualMachineBackupName"]) == \
        ("default", "leap156-xfer-t1")


def test_backup_manifest_is_a_real_backup_not_a_snapshot():
    m = vt.backup_manifest("default", "leap156", "leap156-xfer-t1-a", "t1")
    assert m["spec"]["type"] == "backup"
    assert m["spec"]["source"] == {"apiGroup": "kubevirt.io", "kind": "VirtualMachine",
                                   "name": "leap156"}
    assert m["metadata"]["labels"][vt.TRANSFER_LABEL] == "t1"


def test_export_image_manifest_matches_what_worked_on_harv1():
    m = vt.export_image_manifest("default", "leap156-disk-0-gfcec", "xfer-t1-disk-0", "t1", "harv-rep1")
    s = m["spec"]
    assert s["sourceType"] == "export-from-volume"
    assert (s["pvcName"], s["pvcNamespace"]) == ("leap156-disk-0-gfcec", "default")
    assert s["targetStorageClassName"] == "harv-rep1"
    assert s["storageClassParameters"]["numberOfReplicas"] == "1"
    assert m["metadata"]["labels"][vt.TRANSFER_LABEL] == "t1"


def test_datavolume_manifest_matches_what_worked_on_harv1():
    disk = inventory()["disks"][0]
    m = vt.datavolume_manifest("lab", "leap156-b-disk-0", "http://10.0.0.1:8123/tok.raw.gz",
                               disk, "harvester-longhorn", "t1")
    assert m["kind"] == "DataVolume"
    assert m["spec"]["source"] == {"http": {"url": "http://10.0.0.1:8123/tok.raw.gz"}}
    st = m["spec"]["storage"]
    assert st["storageClassName"] == "harvester-longhorn"
    assert st["accessModes"] == ["ReadWriteMany"]
    assert st["volumeMode"] == "Block"
    assert st["resources"]["requests"]["storage"] == str(10 * GIB)
    assert m["metadata"]["annotations"]["cdi.kubevirt.io/storage.bind.immediate.requested"] == "true"


def test_secret_manifest_copies_data_only():
    src = {"metadata": {"name": "leap156-hd4ry", "namespace": "default", "uid": "u",
                        "resourceVersion": "1", "ownerReferences": [{"name": "leap156"}]},
           "type": "secret", "data": {"userdata": "I2Nsb3Vk"}}
    m = vt.secret_manifest(src, "leap156-b-cloudinit", "lab", "t1")
    assert m["metadata"] == {"name": "leap156-b-cloudinit", "namespace": "lab",
                             "labels": {vt.TRANSFER_LABEL: "t1"}}
    assert m["data"] == {"userdata": "I2Nsb3Vk"} and m["type"] == "secret"


# ---------------------------------------------------------------------------
# Choix du moteur et contrôle
# ---------------------------------------------------------------------------

NFS = ["nfs", "172.16.0.5:/volume1/BACKUP/lab"]


def src_facts(**kw):
    f = {"cluster": "harvlab", "version": "v1.8.2", "inventory": inventory(),
         "backup_target": NFS, "backup_target_ok": True,
         "images": {"default/opensuse-leap-cloud": {"display": "leap.qcow2", "size": 700,
                                                    "virtual_size": 10 * GIB}},
         "room_one": 400 * GIB}
    f.update(kw)
    return f


def dst_facts(**kw):
    f = {"cluster": "harvlab2", "reachable": True, "kubevirt": True, "cdi": True,
         "version": "v1.8.2", "namespaces": ["default", "lab"], "vm_names": [],
         "backup_target": NFS, "backup_target_ok": True, "images": {}}
    f.update(target())
    f.update(kw)
    return f


def req(**kw):
    r = {"kind": "migrate", "name": "leap156", "namespace": "default", "mode": "stop",
         "source": "stopped", "target": "started", "keep_mac": True,
         "networks": {"default/production": "default/production"},
         "storage_classes": {"longhorn-opensuse-leap-cloud": "harvester-longhorn"},
         "create_namespace": False, "store_free": None}
    r.update(kw)
    return r


def codes(findings, level=None):
    return [f["code"] for f in findings if level is None or f["level"] == level]


def test_engine_backup_when_the_target_is_shared():
    assert vt.choose_engine(src_facts(), dst_facts(), req()) == ("backup", "shared-target")


@pytest.mark.parametrize("src,dst,reason", [
    ({"backup_target": None}, {}, "source-no-target"),
    ({"backup_target_ok": False}, {}, "source-no-target"),
    ({}, {"backup_target": None}, "target-no-target"),
    ({}, {"backup_target": ["nfs", "10.0.0.9:/other"]}, "different-targets"),
])
def test_engine_file_otherwise(src, dst, reason):
    assert vt.choose_engine(src_facts(**src), dst_facts(**dst), req()) == ("file", reason)


def test_engine_file_for_export_import_and_when_forced():
    assert vt.choose_engine(src_facts(), None, req(kind="export")) == ("file", "file-requested")
    assert vt.choose_engine(src_facts(), dst_facts(), req(kind="import")) == ("file", "file-requested")
    assert vt.choose_engine(src_facts(), dst_facts(), req(engine="file")) == ("file", "forced")


def test_a_nominal_transfer_has_no_blocker():
    f = vt.check(src_facts(), dst_facts(), req())
    assert not vt.blocking(f), f
    eng = [x for x in f if x["code"] == "engine"][0]
    assert eng["level"] == "ok" and eng["facts"] == {"engine": "backup", "reason": "shared-target"}


def test_an_unreachable_target_stops_the_check_there():
    f = vt.check(src_facts(), dst_facts(reachable=False), req())
    assert codes(f) == ["target-unreachable"] and vt.blocking(f)


def test_kubevirt_missing_blocks():
    assert "kubevirt-missing" in codes(vt.check(src_facts(), dst_facts(kubevirt=False), req()), "block")


def test_an_older_target_only_warns():
    f = vt.check(src_facts(version="v1.9.0"), dst_facts(version="v1.8.2"), req())
    assert "version-older" in codes(f, "warn") and not vt.blocking(f)


def test_namespace_missing_blocks_unless_creation_is_accepted():
    f = vt.check(src_facts(), dst_facts(), req(namespace="newns"))
    assert "namespace-missing" in codes(f, "block")
    f = vt.check(src_facts(), dst_facts(), req(namespace="newns", create_namespace=True))
    assert "namespace-missing" in codes(f, "warn") and not vt.blocking(f)


def test_a_taken_name_blocks():
    f = vt.check(src_facts(), dst_facts(vm_names=["leap156"]), req())
    assert "vm-name-taken" in codes(f, "block")


def test_an_unmapped_network_blocks():
    f = vt.check(src_facts(), dst_facts(), req(networks={"default/production": None}))
    assert "network-unmapped" in codes(f, "block")
    f = vt.check(src_facts(), dst_facts(), req(networks={"default/production": "lab/absent"}))
    assert "network-unmapped" in codes(f, "block")


def test_file_engine_needs_a_mapped_class_that_exists():
    f = vt.check(src_facts(backup_target=None), dst_facts(),
                 req(storage_classes={"longhorn-opensuse-leap-cloud": "nope"}))
    assert "storage-class-unmapped" in codes(f, "block")


def test_backup_engine_cannot_rename_a_plain_class():
    inv = vt.vm_inventory(leap156(), {"leap156-disk-0-gfcec": pvc("leap156-disk-0-gfcec", sc="gold", image=None)})
    f = vt.check(src_facts(inventory=inv), dst_facts(), req())
    assert "storage-class-missing-backup" in codes(f, "block")


def test_capacity_short_blocks():
    f = vt.check(src_facts(backup_target=None), dst_facts(storage_classes={
        "harvester-longhorn": {"replicas": 3, "allocatable": 5 * GIB, "image": False}}), req())
    short = [x for x in f if x["code"] == "capacity-short"]
    assert short and short[0]["level"] == "block"
    assert short[0]["facts"] == {"storage_class": "harvester-longhorn", "needed": 10 * GIB,
                                 "allocatable": 5 * GIB}


def test_devices_and_pinning_warn():
    vm = leap156()
    vm["spec"]["template"]["spec"]["domain"]["devices"]["gpus"] = [{"name": "g", "deviceName": "nvidia.com/A30"}]
    vm["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "harv1"}
    f = vt.check(src_facts(inventory=inventory(vm)), dst_facts(), req())
    assert {"devices-removed", "node-affinity-removed"} <= set(codes(f, "warn"))


def test_backup_engine_image_conflict_blocks():
    """Harvester saute la synchro d'une image dont le nom existe déjà : la VM
    restaurée partirait sur un autre disque de base."""
    f = vt.check(src_facts(), dst_facts(images={"default/opensuse-leap-cloud": {
        "display": "leap.qcow2", "size": 999, "virtual_size": 10 * GIB}}), req())
    assert "image-conflict" in codes(f, "block")
    f = vt.check(src_facts(), dst_facts(images={"default/other": {
        "display": "leap.qcow2", "size": 999, "virtual_size": 10 * GIB}}), req())
    assert "image-conflict" in codes(f, "block")


def test_the_same_image_on_both_sides_is_fine():
    same = {"default/opensuse-leap-cloud": {"display": "leap.qcow2", "size": 700,
                                            "virtual_size": 10 * GIB}}
    f = vt.check(src_facts(), dst_facts(images=same), req())
    assert "image-conflict" not in codes(f)


def test_an_image_gone_from_the_source_warns_for_the_backup_engine():
    """Relevé sur harv1 : leap156 est née d'une image supprimée depuis."""
    f = vt.check(src_facts(images={}), dst_facts(), req())
    assert "image-missing-source" in codes(f, "warn")
    f = vt.check(src_facts(images={}, backup_target=None), dst_facts(), req())
    assert "image-missing-source" not in codes(f)


def test_backup_engine_needs_harvester_1_4_to_bring_images():
    f = vt.check(src_facts(), dst_facts(version="v1.3.2"), req())
    assert "image-sync-unsupported" in codes(f, "block")


def test_file_engine_needs_cdi():
    f = vt.check(src_facts(backup_target=None), dst_facts(cdi=False), req())
    assert "cdi-missing" in codes(f, "block")
    f = vt.check(src_facts(), dst_facts(cdi=False), req())
    assert "cdi-missing" not in codes(f)


def test_file_engine_needs_room_on_the_source_for_the_export():
    f = vt.check(src_facts(backup_target=None, room_one=GIB), dst_facts(), req())
    assert "source-room-short" in codes(f, "block")


def test_export_needs_room_in_the_store():
    f = vt.check(src_facts(), None, req(kind="export", store_free=GIB))
    assert "store-room-short" in codes(f, "block")
    assert "secrets-in-archive" in codes(f, "warn")


def test_short_mode_needs_the_backup_engine():
    f = vt.check(src_facts(backup_target=None), dst_facts(), req(mode="short"))
    assert "short-mode-needs-backup" in codes(f, "block")
    assert not vt.blocking(vt.check(src_facts(), dst_facts(), req(mode="short")))


def test_a_copy_that_keeps_running_warns_about_the_hostname():
    f = vt.check(src_facts(), dst_facts(), req(source="running"))
    assert "hostname-duplicate" in codes(f, "warn")


def test_import_checks_the_target_only():
    f = vt.check(src_facts(backup_target=None), dst_facts(), req(kind="import"))
    assert not vt.blocking(f)
    assert "source-room-short" not in codes(f)


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def chunks(data, size=65536):
    for i in range(0, len(data), size):
        yield data[i:i + size]


def test_archive_round_trip(tmp_path):
    disk = os.urandom(3 * 1024 * 1024 + 123)       # pas un multiple de 512
    path = tmp_path / ("vm" + vt.ARCHIVE_SUFFIX)
    w = vt.ArchiveWriter(path)
    w.add_json("manifest.json", {"format": vt.FORMAT, "vm": {"name": "x"}})
    info = w.add_stream("disks/disk-0.raw.gz", chunks(disk))
    w.close()
    assert info == {"size": len(disk), "sha256": hashlib.sha256(disk).hexdigest()}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # une archive tar ordinaire, lisible par les outils standard
    with tarfile.open(path) as tf:
        assert tf.getnames() == ["manifest.json", "disks/disk-0.raw.gz", "SHA256SUMS"]
    r = vt.ArchiveReader(path)
    assert r.complete is True
    assert r.manifest()["vm"] == {"name": "x"}
    off, size = r.member("disks/disk-0.raw.gz")
    assert size == len(disk)
    with r.open_member("disks/disk-0.raw.gz") as f:
        assert f.read() == disk
    assert r.verify() == []


def test_archive_verify_finds_a_changed_byte(tmp_path):
    path = tmp_path / "vm.hvx"
    w = vt.ArchiveWriter(path)
    w.add_json("manifest.json", {"format": 1})
    w.add_stream("disks/d.raw.gz", chunks(b"A" * 5000))
    w.close()
    r = vt.ArchiveReader(path)
    off, _ = r.member("disks/d.raw.gz")
    with open(path, "r+b") as f:
        f.seek(off + 10)
        f.write(b"B")
    assert vt.ArchiveReader(path).verify() == ["disks/d.raw.gz"]


def test_an_interrupted_export_is_incomplete(tmp_path):
    path = tmp_path / "vm.hvx"
    w = vt.ArchiveWriter(path)
    w.add_json("manifest.json", {"format": 1})
    w.add_stream("disks/d.raw.gz", chunks(b"A" * 5000))
    w.f.close()                                     # pas de close() : export coupé
    r = vt.ArchiveReader(path)
    assert r.complete is False
    assert r.manifest() == {"format": 1}


def test_archive_refuses_to_overwrite(tmp_path):
    path = tmp_path / "vm.hvx"
    path.write_bytes(b"")
    with pytest.raises(FileExistsError):
        vt.ArchiveWriter(path)


def test_large_member_header_is_rewritten_in_place():
    """Au-delà de 8 Gio, l'en-tête ustar ne sait plus écrire la taille : le
    format GNU l'encode en base 256 dans les MÊMES 512 octets, ce qui permet
    de le réécrire après coup."""
    hdr = vt._tar_header("disks/big.raw.gz", 20 * GIB, 0)
    assert len(hdr) == 512
    ti = tarfile.TarInfo.frombuf(hdr, "utf-8", "surrogateescape")
    assert ti.size == 20 * GIB and ti.name == "disks/big.raw.gz"
