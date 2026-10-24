"""Prochaine version KVM : corrections après relecture des tâches 1-2.

Preuve de possession d'un volume (identité, chaînes, stockage partagé) pour
la suppression, le redimensionnement et l'ancien seed ; pools imbriqués ;
retour arrière qui ne supprime jamais un volume d'un autre ; seed régénéré
après un changement de média ; réseaux cités par leur pont ; volumes des
disques ajoutés au prochain démarrage ; progression de la copie complète ;
chaînes vers un pool supprimé ; relevé gardé pour le catalogue."""

import threading
import time
from pathlib import Path

import pytest

from kvm_next_sim import Sim2, W
from test_kvm_edit import DISKS, NEW_DISK, OPTS, SATA_DISK, UD, seeded_world, vm_world
import kvm_create as kc
import kvm_edit as ke
import kvm_host as kh
import kvm_net as kn
import kvm_ops as ko
import kvm_storage as ks
import qcow2

IMG = "/var/lib/libvirt/images"
SAME = "64769:424242"
BOOM = (1, "error: internal error: boom\n")
LOST = (kh.SSH_RC, "Connection reset by peer\n")


def _dom(s, name, disks, running=False):
    x = W.dom(name, disks)
    s.add_domain(name, x, state="running" if running else "shut off",
                 live=x if running else None)


def same_inode(s, *paths):
    for p in paths:
        s.inodes[p] = SAME


# --- C1 : preuve de possession (suppression, redimensionnement, ancien seed) ------------

def _alias(s, path, running):
    _dom(s, "alias", [W.disk("vda", "/srv/alias.qcow2")], running)
    same_inode(s, path, "/srv/alias.qcow2")


def _hardlink(s, path, running):
    _dom(s, "hard", [W.disk("vdb", "/data/other-dir/hard.qcow2")], running)
    same_inode(s, path, "/data/other-dir/hard.qcow2")


def _chain(s, path, running):
    _dom(s, "chained", [W.disk("vda", "/data/top.qcow2", chain=("/data/base-link.qcow2",))],
         running)
    s.files["/data/top.qcow2"] = [{"filename": "/data/top.qcow2"},
                                  {"filename": "/data/base-link.qcow2"}]
    same_inode(s, path, "/data/base-link.qcow2")


def _netfs(s, path, running):
    pool = next(p for p, v in s.pools.items() if path.startswith(v["path"] + "/"))
    s.pools[pool]["type"] = "netfs"


def _nfs_fs(s, path, running):
    s.fstypes[path.rsplit("/", 1)[0]] = "nfs4"


def _io_error(s, path, running):
    _dom(s, "eio", [W.disk("vda", "/x/y.qcow2")], running)
    s.stat_errors["/x/y.qcow2"] = "stat: cannot statx '/x/y.qcow2': Input/output error"


C1_CASES = [
    ("symlink alias cited by another VM", _alias, "used by"),
    ("same inode in another directory", _hardlink, "used by"),
    ("backing chain of another VM through an alias", _chain, "backing file of /data/top.qcow2"),
    ("volume of a netfs pool", _netfs, ko.REASON_SHARED_STORAGE),
    ("dir pool on an NFS file system", _nfs_fs, ko.REASON_SHARED_STORAGE),
    ("I/O error reading another VM's disk", _io_error, ks.REASON_UNPROVEN),
]


def _gesture_world(gesture):
    if gesture == "seed":
        return seeded_world(), "images", "ci1-cidata.iso"
    s = Sim2()
    s.add_pool("images", f"{IMG}/images")
    s.add_vol("images", "d.qcow2", cap=1 << 30)
    return s, "images", "d.qcow2"


def _play(s, gesture, pool, vol, **kw):
    if gesture == "delete":
        return ks.delete_volume(s, pool, vol, **kw)
    if gesture == "resize":
        return ks.resize_volume(s, pool, vol, 2 << 30, **kw)
    return ke.regenerate_seed(s, "ci1", UD, None, **kw)


@pytest.mark.parametrize("gesture", ["delete", "resize", "seed"])
@pytest.mark.parametrize("label,setup,text", C1_CASES)
def test_c1_ownership_proof_refuses(gesture, label, setup, text):
    s, pool, vol = _gesture_world(gesture)
    path = s.pools[pool]["vols"][vol]["path"]
    setup(s, path, running=gesture == "resize")
    s.writes.clear()
    if gesture == "seed":
        res = _play(s, gesture, pool, vol)
        assert res["removed"] is False and text in res["kept_reason"], res
        assert ("vol-delete", "--pool", pool, "--vol", vol) not in s.writes
    else:
        with pytest.raises(ko.OpError) as e:
            _play(s, gesture, pool, vol)
        assert e.value.kind == "refused" and text in e.value.message, e.value.message
        assert s.writes == []
    assert vol in s.pools[pool]["vols"]


@pytest.mark.parametrize("gesture", ["delete", "resize", "seed"])
def test_c1_nothing_cites_it_then_the_gesture_goes_through(gesture):
    s, pool, vol = _gesture_world(gesture)
    _dom(s, "elsewhere", [W.disk("vda", "/data/unrelated.qcow2")], gesture == "resize")
    res = _play(s, gesture, pool, vol)
    if gesture == "delete":
        assert res["deleted"] and vol not in s.pools[pool]["vols"]
    elif gesture == "resize":
        assert s.writes[-1][0] == "vol-resize"
    else:
        assert res["removed"] is True and vol not in s.pools[pool]["vols"]


def test_c1_running_only_counts_for_resize_but_not_for_delete():
    s, pool, vol = _gesture_world("resize")
    _alias(s, s.pools[pool]["vols"][vol]["path"], running=False)
    ks.resize_volume(s, pool, vol, 2 << 30)                    # VM arrêtée : permis (décision 66)
    with pytest.raises(ko.OpError, match="used by alias"):
        ks.delete_volume(s, pool, vol)


@pytest.mark.parametrize("error,acknowledgeable", [
    ("stat: cannot statx '/x/y.qcow2': Permission denied", True),
    ("stat: cannot statx '/x/y.qcow2': Input/output error", False),
    ("stat: cannot statx '/x/y.qcow2': Stale file handle", False),
])
def test_c1_only_a_permission_refusal_can_be_acknowledged(error, acknowledgeable):
    s, pool, vol = _gesture_world("delete")
    _dom(s, "vmx", [W.disk("vda", "/x/y.qcow2")])
    s.stat_errors["/x/y.qcow2"] = error
    if acknowledgeable:
        res = ks.delete_volume(s, pool, vol, acknowledged_unreadable=["/x/y.qcow2"])
        assert res["deleted"] and res["acknowledged_unreadable"] == ["/x/y.qcow2"]
    else:
        with pytest.raises(ko.OpError, match="could not prove"):
            ks.delete_volume(s, pool, vol, acknowledged_unreadable=["/x/y.qcow2"])


def test_c1_volume_path_is_read_before_the_survey_and_stat_ed():
    s, pool, vol = _gesture_world("delete")
    ks.delete_volume_plan(s, pool, vol)
    stats = [c for c in s.calls if c[0] == "run" and "stat -L" in " ".join(c[1])]
    assert any(f"{IMG}/images/d.qcow2" in " ".join(c[1]) for c in stats)
    first_dump = next(i for i, c in enumerate(s.calls) if c[1][:1] == ("vol-dumpxml",))
    first_list = next(i for i, c in enumerate(s.calls) if c[1][:1] == ("list",))
    assert first_dump < first_list


@pytest.mark.parametrize("label,setup,text", [
    ("nested pool directory (by name)", lambda s: (
        s.add_pool("kvmtest-pool", f"{IMG}/images/kvmtest"),
        s.add_vol("images", "kvmtest", fmt="dir")), "the directory of pool kvmtest-pool"),
    ("nested pool directory (through a link)", lambda s: (
        s.add_pool("kvmtest-pool", "/srv/kt"), s.add_vol("images", "kvmtest", fmt="dir"),
        same_inode(s, "/srv/kt", f"{IMG}/images/kvmtest")), "the directory of pool kvmtest-pool"),
    ("plain directory", lambda s: s.add_vol("images", "kvmtest", fmt="dir"), ks.REASON_DIRECTORY),
])
def test_i1_directory_volumes_are_never_deleted(label, setup, text):
    s = Sim2()
    s.add_pool("images", f"{IMG}/images")
    setup(s)
    with pytest.raises(ko.OpError) as e:
        ks.delete_volume(s, "images", "kvmtest")
    assert text in e.value.message and s.writes == []


def test_delete_volume_digest_plan_then_execute():
    s, pool, vol = _gesture_world("delete")
    plan = ks.delete_volume_plan(s, pool, vol)
    assert plan["refusals"] == [] and len(plan["digest"]) == 64
    # volume remplacé sous le même nom entre l'aperçu et l'exécution
    s.inodes[plan["path"]] = "64769:1"
    steps = []
    with pytest.raises(ko.OpError) as e:
        ks.delete_volume(s, pool, vol, expect_digest=plan["digest"],
                         progress=lambda *a: steps.append(a))
    assert e.value.kind == "plan-changed" and s.writes == []
    again = ks.delete_volume_plan(s, pool, vol)
    res = ks.delete_volume(s, pool, vol, expect_digest=again["digest"],
                           progress=lambda *a: steps.append(a))
    assert res["deleted"] and [x[:2] for x in steps[-4:]] == [
        ("plan", "running"), ("plan", "done"), ("delete", "running"), ("delete", "done")]


# --- I1 : pools imbriqués --------------------------------------------------------------------

POOLS = [{"name": "images", "type": "dir", "target_path": IMG},
         {"name": "harvlab", "type": "dir", "target_path": f"{IMG}/harvlab"},
         {"name": "nfs", "type": "netfs", "target_path": "/mnt/nfs1"},
         {"name": "fsp", "type": "fs", "target_path": "/mnt/fs1"},
         {"name": "datap", "type": "dir", "target_path": "/data/p"},
         {"name": "vg", "type": "logical", "target_path": "/dev/vg0"}]
NFS = {"type": "netfs", "source_host": "nas", "source_dir": "/export"}


@pytest.mark.parametrize("label,spec,ids,text", [
    ("netfs nested in a dir pool", {**NFS, "path": f"{IMG}/n1"}, {},
     "inside pool images"),
    ("netfs mount above a dir pool", {**NFS, "path": "/data"}, {}, "around pool datap"),
    ("dir inside a netfs pool", {"path": "/mnt/nfs1/x"}, {}, "inside pool nfs"),
    ("dir around a netfs and an fs pool", {"path": "/mnt"}, {}, "around pool nfs"),
    ("dir inside an fs pool", {"path": "/mnt/fs1/x"}, {}, "inside pool fsp"),
    ("link to a pool directory", {"path": "/srv/link"},
     {"/srv/link": "1:9", f"{IMG}/harvlab": "1:9"}, "the directory of pool harvlab"),
    ("under a link to a netfs mount", {"path": "/srv/l/x"},
     {"/srv/l": "1:7", "/mnt/nfs1": "1:7"}, "inside pool nfs"),
    ("netfs mount on a link above a dir pool", {**NFS, "path": "/srv/m"},
     {"/srv/m": "1:5", "/data": "1:5"}, "around pool datap"),
    ("link to /etc", {"path": "/srv/e"}, {"/srv/e": "1:3", "/etc": "1:3"},
     "system directory (/etc)"),
])
def test_i1_pool_overlap_refusals(label, spec, ids, text):
    full = {"name": "kvmtest-pool", "type": "dir", **spec}
    reasons = ks.check_pool_spec(full, POOLS, ids)
    assert any(text in r for r in reasons), reasons


@pytest.mark.parametrize("label,spec,ids,nested", [
    ("dir inside a dir pool", {"path": f"{IMG}/kvmtest"}, {}, ["images"]),
    ("dir inside a dir pool through a link", {"path": "/srv/k"},
     {"/srv": "1:2", IMG: "1:2"}, ["images"]),
    ("unrelated", {"path": "/srv/pools/p2"}, {}, []),
])
def test_i1_pool_nesting_accepted_between_dir_pools(label, spec, ids, nested):
    full = {"name": "kvmtest-pool", "type": "dir", **spec}
    assert ks.check_pool_spec(full, POOLS, ids) == []
    assert ks.nested_pools(full, POOLS, ids) == nested


def test_i1_define_pool_compares_targets_by_identity():
    s = Sim2()
    s.add_pool("harvlab", f"{IMG}/harvlab")
    same_inode(s, "/srv/link", f"{IMG}/harvlab")
    with pytest.raises(ko.OpError, match="the directory of pool harvlab"):
        ks.define_pool(s, {"name": "kvmtest-pool", "type": "dir", "path": "/srv/link"})
    assert s.writes == []
    assert any("/srv/link" in " ".join(c[1]) for c in s.calls if c[0] == "run")


def test_i1_catalog_never_lists_directories():
    s = Sim2()
    s.add_pool("images", f"{IMG}/images")
    s.add_pool("kvmtest-pool", f"{IMG}/images/kvmtest")
    s.add_vol("images", "kvmtest", fmt="dir")
    s.add_vol("images", "free.qcow2")
    names = [i["volume"] for i in ks.list_images(s)["images"]]
    assert names == ["free.qcow2"]


# --- I2 : le retour arrière ne supprime jamais un volume d'un autre ----------------------

def _create_world():
    s = Sim2()
    s.add_pool("images", f"{IMG}/images")
    s.add_pool("other", f"{IMG}/other")
    s.add_vol("images", "cirros.qcow2", cap=112 << 20, alloc=21 << 20)
    s.add_vol("images", "data.qcow2", cap=1 << 30)
    s.add_net("kvmtest-net", "10.213.77.0/24")
    return s


def _spec(**over):
    sp = {"name": "kvmtest-1", "os": "linux", "machine": "q35",
          "cpu": {"vcpus": 1, "max_vcpus": 2}, "memory": {"current_mib": 256, "max_mib": 512},
          "disks": [{"source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 20}}],
          "nics": [{"kind": "network", "source": "kvmtest-net"}],
          "display": {"vnc": True, "password": False}}
    sp.update(over)
    return sp


IN_USE = "error: storage volume name '{}' already in use.\n"


@pytest.mark.parametrize("label,over,verb,vol", [
    ("full copy", {"disks": [{"source": {"kind": "image", "pool": "other", "image_pool": "images",
                                         "image": "cirros.qcow2", "linked": False}}]},
     "vol-create-from", "kvmtest-1-vda.qcow2"),
    ("seed upload", {"disks": [{"source": {"kind": "volume", "pool": "images",
                                           "volume": "data.qcow2", "format": "qcow2"}}],
                     "cloudinit": {"user_data": UD, "seed_pool": "images"}},
     "vol-create-as", "kvmtest-1-cidata.iso"),
    ("blank disk", {}, "vol-create-as", "kvmtest-1-vda.qcow2"),
])
def test_i2_volume_created_meanwhile_is_never_deleted(label, over, verb, vol):
    s = _create_world()
    s.fail_next[verb] = (1, IN_USE.format(vol))
    with pytest.raises(kh.KvmError) as e:
        kc.create_vm(s, _spec(**over), options=OPTS)
    assert not [w for w in s.writes if w[0] == "vol-delete"], s.writes
    assert e.value.leftovers == []


def test_i2_existence_rechecked_before_each_instance():
    s = _create_world()

    def progress(sid, status, msg):
        if sid == "kvmtest-1-01:verify" and status == "done":
            s.add_vol("images", "kvmtest-1-02-vda.qcow2")       # créé par quelqu'un d'autre
    with pytest.raises(ko.OpError) as e:
        kc.create_vm(s, _spec(instances=2), options=OPTS, progress=progress)
    assert "created meanwhile: images/kvmtest-1-02-vda.qcow2" in e.value.message
    assert [c["name"] for c in e.value.data["created"]] == ["kvmtest-1-01"]
    assert not [w for w in s.writes if "kvmtest-1-02-vda.qcow2" in w]
    assert "kvmtest-1-02-vda.qcow2" in s.pools["images"]["vols"]


# --- copie complète : octets copiés --------------------------------------------------------

def test_full_copy_reports_bytes_copied_from_vol_info():
    s = _create_world()
    orig = s.v_vol_create_from

    def slow_copy(o, p, data):
        rc, out, err = orig(o, p, data)
        v = s.pools[o["pool"]]["vols"][o.get("vol") and "kvmtest-1-vda.qcow2"]
        for n in (5, 10, 21):
            v["alloc"] = n << 20
            time.sleep(0.12)
        return rc, out, err
    s.v_vol_create_from = slow_copy
    steps = []
    sp = _spec(disks=[{"source": {"kind": "image", "pool": "other", "image_pool": "images",
                                  "image": "cirros.qcow2", "linked": False}}])
    old = kc.COPY_POLL
    kc.COPY_POLL = 0.05
    try:
        kc.create_vm(s, sp, options=OPTS, progress=lambda *a: steps.append(a))
    finally:
        kc.COPY_POLL = old
    copied = [int(m.split()[0]) for sid, st, m in steps
              if sid == "volume-vda" and st == "running" and "bytes copied" in m]
    assert len(copied) >= 2 and copied == sorted(copied) and copied[-1] == 21 << 20
    assert any("image: 117440512 bytes" in m for _, _, m in steps)


def test_with_progress_reraises_the_copy_error():
    s = _create_world()
    s.add_vol("images", "x.qcow2")

    def work():
        time.sleep(0.1)
        raise kh.KvmError("failed", "node2: failed: copy broke", host="node2", rc=1)
    with pytest.raises(kh.KvmError, match="copy broke"):
        kc._with_progress(s, work, "images", "x.qcow2", lambda n: None, poll=0.02)


# --- I3 : seed régénéré, échec après le changement de média ------------------------------

@pytest.mark.parametrize("label,verb,fail,kept", [
    ("upload fails", "vol-upload", BOOM, False),
    ("update-device refused", "update-device", BOOM, False),
    ("update-device transport loss", "update-device", LOST, True),
    ("define fails after update-device", "define", BOOM, True),
    ("define transport loss", "define", LOST, True),
])
def test_i3_regenerate_seed_failure_keeps_what_the_vm_may_use(label, verb, fail, kept):
    s = seeded_world("running")
    s.fail_next[verb] = fail
    with pytest.raises(kh.KvmError) as e:
        ke.regenerate_seed(s, "ci1", UD + "# v2\n", None)
    new = "ci1-cidata-1.iso"
    assert (new in s.pools["images"]["vols"]) is kept
    assert "ci1-cidata.iso" in s.pools["images"]["vols"]     # ancien seed jamais touché
    assert ("vol-delete", "--pool", "images", "--vol", "ci1-cidata.iso") not in s.writes
    if kept:
        assert [(x["pool"], x["volume"]) for x in e.value.leftovers] == [("images", new)]
    else:
        assert e.value.leftovers == []


# --- I4 : réseau cité par son pont ---------------------------------------------------------

def _bridge_dom(name, bridge):
    return W.dom(name, nics=0, extra_dev=f"<interface type='bridge'><source bridge='{bridge}'/>"
                                         "<model type='virtio'/></interface>")


@pytest.mark.parametrize("label,forward,bridge,dom_bridge,refused", [
    ("NAT network: card on its virbr", "nat", "virbr5", "virbr5", True),
    ("isolated network: card on its virbr", None, "virbr6", "virbr6", True),
    ("routed network: other bridge", "route", "virbr7", "br0", False),
    ("bridge-mode network: card on the host bridge", "bridge", "br0", "br0", False),
])
def test_i4_network_cited_by_its_bridge(label, forward, bridge, dom_bridge, refused):
    s = Sim2()
    s.add_net("kvmtest-net", "10.213.77.0/24" if forward != "bridge" else None,
              bridge=bridge, forward=forward)
    x = _bridge_dom("vmb", dom_bridge)
    s.add_domain("vmb", x, state="running", live=x)
    if refused:
        with pytest.raises(ko.OpError, match="vmb"):
            kn.stop_network(s, "kvmtest-net")
        s.domains["vmb"].update(state="shut off", live=None)
        with pytest.raises(ko.OpError, match="used by vmb"):
            kn.delete_network(s, "kvmtest-net")
        assert s.writes == []
    else:
        kn.stop_network(s, "kvmtest-net")
        assert s.writes == [("net-destroy", "--network", "kvmtest-net")]


# --- I6 : volumes des disques ajoutés par le define --------------------------------------

@pytest.mark.parametrize("label,state,disk,fail,kept", [
    ("stopped VM, define refused", "shut off", NEW_DISK, BOOM, False),
    ("stopped VM, define transport loss", "shut off", NEW_DISK, LOST, True),
    ("running VM, SATA disk for next boot, define refused", "running", SATA_DISK, BOOM, False),
])
def test_i6_next_boot_disk_volume_rolled_back(label, state, disk, fail, kept):
    s = vm_world(state)
    s.fail_next["define"] = fail
    before = set(s.pools["images"]["vols"])
    with pytest.raises(kh.KvmError) as e:
        ke.edit_vm(s, "vm1", {"disks": DISKS + [disk]}, options=OPTS)
    added = set(s.pools["images"]["vols"]) - before
    if kept:
        assert len(added) == 1
        assert [x["volume"] for x in e.value.leftovers] == list(added)
    else:
        assert added == set() and e.value.leftovers == []
        assert [w[0] for w in s.writes][-1] == "vol-delete"


def test_i6_second_new_disk_refused_rolls_back_the_first():
    s = vm_world()
    two = [NEW_DISK, dict(NEW_DISK)]
    orig = s.v_vol_create_as
    count = {"n": 0}

    def second_fails(o, p, data):
        count["n"] += 1
        if count["n"] == 2:
            return 1, "", "error: no space left\n"
        return orig(o, p, data)
    s.v_vol_create_as = second_fails
    before = set(s.pools["images"]["vols"])
    with pytest.raises(kh.KvmError):
        ke.edit_vm(s, "vm1", {"disks": DISKS + two}, options=OPTS)
    assert set(s.pools["images"]["vols"]) == before


def test_hot_attach_transport_loss_keeps_the_volume():
    s = vm_world("running")
    s.fail_next["attach-device"] = LOST
    before = set(s.pools["images"]["vols"])
    with pytest.raises(kh.KvmError) as e:
        ke.attach_disk(s, "vm1", NEW_DISK)
    added = set(s.pools["images"]["vols"]) - before
    assert len(added) == 1 and [x["volume"] for x in e.value.leftovers] == list(added)
    assert "vol-delete" not in [w[0] for w in s.writes]


def test_hot_attach_refused_deletes_the_volume():
    s = vm_world("running")
    s.fail_next["attach-device"] = BOOM
    before = set(s.pools["images"]["vols"])
    with pytest.raises(kh.KvmError):
        ke.attach_disk(s, "vm1", NEW_DISK)
    assert set(s.pools["images"]["vols"]) == before


# --- suppression d'un pool : chaînes de sauvegarde --------------------------------------------

def _pool_world():
    s = Sim2()
    s.add_pool("images", f"{IMG}/images")
    s.add_pool("kvmtest-pool", "/srv/kvmtest")
    s.add_vol("kvmtest-pool", "base.qcow2")
    return s


@pytest.mark.parametrize("label,setup,text", [
    ("domain chain into the pool", lambda s: (
        _dom(s, "vmc", [W.disk("vda", "/data/top.qcow2")]),
        s.files.__setitem__("/data/top.qcow2", [{"filename": "/data/top.qcow2"},
                                                {"filename": "/srv/kvmtest/base.qcow2"}])),
     "a backing chain uses a file of the pool: vmc"),
    ("domain chain through an alias", lambda s: (
        _dom(s, "vmc", [W.disk("vda", "/data/top.qcow2")]),
        s.files.__setitem__("/data/top.qcow2", [{"filename": "/data/top.qcow2"},
                                                {"filename": "/data/alias.qcow2"}]),
        same_inode(s, "/data/alias.qcow2", "/srv/kvmtest/base.qcow2")),
     "a backing chain uses a file of the pool: vmc"),
    ("free volume of another pool", lambda s: s.add_vol(
        "images", "ov.qcow2", backing="/srv/kvmtest/base.qcow2"),
     f"a backing chain uses a file of the pool: {IMG}/images/ov.qcow2"),
    ("domain cites a pool file through an alias", lambda s: (
        _dom(s, "vma", [W.disk("vda", "/data/alias2.qcow2")]),
        same_inode(s, "/data/alias2.qcow2", "/srv/kvmtest/base.qcow2")), "used by vma"),
])
def test_delete_pool_refused_by_backing_chains(label, setup, text):
    s = _pool_world()
    setup(s)
    with pytest.raises(ko.OpError) as e:
        ks.delete_pool(s, "kvmtest-pool")
    assert text in e.value.message and s.writes == []


def test_delete_pool_with_unrelated_chains_goes_through():
    s = _pool_world()
    _dom(s, "vmc", [W.disk("vda", "/data/top.qcow2")])
    s.files["/data/top.qcow2"] = [{"filename": "/data/top.qcow2"},
                                  {"filename": "/data/base.qcow2"}]
    ks.delete_pool(s, "kvmtest-pool")
    assert [w[0] for w in s.writes] == ["pool-destroy", "pool-undefine"]


# --- divers ----------------------------------------------------------------------------------

def _qcow2(path, size):
    hdr = bytearray(112)
    hdr[0:4] = b"QFI\xfb"
    hdr[4:8] = (3).to_bytes(4, "big")
    hdr[20:24] = (16).to_bytes(4, "big")
    hdr[24:32] = size.to_bytes(8, "big")
    hdr[100:104] = (112).to_bytes(4, "big")
    Path(path).write_bytes(bytes(hdr) + b"\0" * 70000)
    return str(path)


@pytest.mark.parametrize("size,limit,refused", [
    (32 << 40, None, True),                 # au-delà des 16 Tio par défaut
    (16 << 40, None, False),
    (2 << 30, 1 << 30, True),               # plafond réglé par l'appelant
])
def test_upload_virtual_size_limit(tmp_path, size, limit, refused):
    s = Sim2()
    s.add_pool("images", f"{IMG}/images")
    f = _qcow2(tmp_path / "big.qcow2", size)
    if refused:
        with pytest.raises(ko.OpError, match="above the limit"):
            ks.upload_volume(s, "images", "big.qcow2", f, verify=False,
                             max_virtual_size=limit)
        assert s.writes == []
    else:
        ks.upload_volume(s, "images", "big.qcow2", f, verify=False, max_virtual_size=limit)


def test_qcow2_check_upload_limit_can_be_lifted():
    hdr = bytearray(112)
    hdr[0:4] = b"QFI\xfb"
    hdr[4:8] = (3).to_bytes(4, "big")
    hdr[24:32] = (64 << 40).to_bytes(8, "big")
    hdr[100:104] = (112).to_bytes(4, "big")
    assert qcow2.check_upload(bytes(hdr), None) == []
    assert any("above the limit" in r for r in qcow2.check_upload(bytes(hdr)))


@pytest.mark.parametrize("state", ["running", "shut off"])
def test_disk_source_change_of_a_non_cdrom_is_refused_up_front(state):
    s = vm_world(state)
    plan = ke.edit_plan(s, "vm1", {"disks": [
        {"target": "vda", "source": {"kind": "volume", "pool": "images", "volume": "base.qcow2",
                                     "format": "qcow2"}}, {"target": "sda"}]}, options=OPTS)
    ch = next(c for c in plan["changes"] if c["field"] == "disks[vda].source")
    assert ch["applied"] == "refused" and "only the media of a CD-ROM" in ch["reason"]


# --- relevé gardé pour le catalogue ------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _cache(**kw):
    made = []

    def factory(host):
        made.append(host.name)
        return f"survey-{len(made)}"
    spawned = []
    c = ko.SurveyCache(ttl=10, max_stale=60, clock=kw.pop("clock"), enabled=True,
                       spawn=kw.pop("spawn", lambda fn: spawned.append(fn)), factory=factory)
    return c, made, spawned


def test_survey_cache_fresh_stale_background_and_invalidation():
    clock = _Clock()
    c, made, spawned = _cache(clock=clock)
    h = Sim2()
    assert c.get(h) == ("survey-1", 0.0)
    clock.t += 5
    assert c.get(h) == ("survey-1", 5.0) and made == ["node2"]          # frais
    clock.t += 10
    assert c.get(h) == ("survey-1", 15.0) and len(spawned) == 1         # périmé : servi + fil
    assert c.get(h)[0] == "survey-1" and len(spawned) == 1              # un seul fil à la fois
    spawned.pop()()
    assert c.get(h)[0] == "survey-2"
    c.invalidate(h)
    assert c.get(h) == ("survey-3", 0.0)                                # oublié : refait
    clock.t += 100
    assert c.get(h) == ("survey-4", 0.0)                                # trop vieux : sur place


def test_survey_cache_refresh_started_before_a_write_is_discarded():
    clock = _Clock()
    c, made, spawned = _cache(clock=clock)
    h = Sim2()
    c.get(h)
    clock.t += 15
    c.get(h)
    c.invalidate(h)                         # écriture de la console pendant le rafraîchissement
    spawned.pop()()
    assert c.get(h) == ("survey-3", 0.0)


def test_list_images_uses_the_cache_and_writes_invalidate_it(monkeypatch):
    clock = _Clock()
    cache = ko.SurveyCache(ttl=10, clock=clock, enabled=True, spawn=lambda fn: None)
    monkeypatch.setattr(ko, "SURVEYS", cache)
    s = Sim2()
    s.add_pool("images", f"{IMG}/images")
    s.add_vol("images", "a.qcow2")
    assert [i["volume"] for i in ks.list_images(s)["images"]] == ["a.qcow2"]
    s.add_vol("images", "b.qcow2")                      # vu par libvirt, pas par le cache
    assert [i["volume"] for i in ks.list_images(s)["images"]] == ["a.qcow2"]
    ks.create_volume(s, "images", "c.qcow2", 1 << 20)   # écriture de la console : oublié
    assert [i["volume"] for i in ks.list_images(s)["images"]] == ["a.qcow2", "b.qcow2",
                                                                   "c.qcow2"]
    assert ks.list_images(s, fresh=True)["survey_age_s"] == 0.0


def test_delete_volume_never_uses_the_cache(monkeypatch):
    clock = _Clock()
    cache = ko.SurveyCache(ttl=10, clock=clock, enabled=True, spawn=lambda fn: None)
    monkeypatch.setattr(ko, "SURVEYS", cache)
    s, pool, vol = _gesture_world("delete")
    ks.list_images(s)                                   # cache rempli : personne ne cite d.qcow2
    _alias(s, s.pools[pool]["vols"][vol]["path"], running=False)
    with pytest.raises(ko.OpError, match="used by alias"):
        ks.delete_volume(s, pool, vol)


def test_survey_cache_is_thread_safe_under_concurrent_gets():
    c = ko.SurveyCache(ttl=0, max_stale=60, enabled=True,
                       spawn=lambda fn: threading.Thread(target=fn).start(),
                       factory=lambda h: object())
    h = Sim2()
    errors = []

    def hammer():
        try:
            for _ in range(50):
                c.get(h)
                c.invalidate(h)
        except Exception as e:     # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=hammer) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
