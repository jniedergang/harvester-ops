"""Prochaine version KVM : images, volumes et pools (bin/lib/kvm_storage.py),
et l'envoi en flux de kvm_host (run_stream) contre un vrai processus local."""

import hashlib
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from kvm_next_sim import Sim2, W
import kvm_host as kh
import kvm_ops as ko
import kvm_storage as ks

IMG = "/var/lib/libvirt/images"


@pytest.fixture
def work():
    d = tempfile.mkdtemp(prefix="kvms-", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def qcow2_file(path, size=64 << 20, backing=False):
    """En-tête qcow2 v3 minimal (fabriqué à la main, comme test_qcow2)."""
    hdr = bytearray(112)
    hdr[0:4] = b"QFI\xfb"
    hdr[4:8] = (3).to_bytes(4, "big")
    if backing:
        hdr[8:16] = (512).to_bytes(8, "big")
        hdr[16:20] = (10).to_bytes(4, "big")
    hdr[20:24] = (16).to_bytes(4, "big")
    hdr[24:32] = size.to_bytes(8, "big")
    hdr[100:104] = (112).to_bytes(4, "big")
    data = bytes(hdr) + b"\0" * 400 + (b"/etc/passwd" if backing else b"\0" * 11) + b"\0" * 70000
    Path(path).write_bytes(data)
    return str(path)


def raw_file(path, size=300000):
    Path(path).write_bytes(os.urandom(size))
    return str(path)


def sim_pool(state="running", available=10 ** 12):
    s = Sim2()
    s.add_pool("images", f"{IMG}/images", available=available, state=state)
    return s


# --- noms -------------------------------------------------------------------------

@pytest.mark.parametrize("name,ok", [
    ("cirros.qcow2", True), ("a", True), ("x+y_z-1.iso", True), ("../etc", False),
    ("a/b", False), (".hidden", False), ("-x", False), ("a b", False), ("a..b", False),
    ("", False), (None, False), ("a;rm -rf /", False), ("$(id)", False), ("a\nb", False),
])
def test_check_volume_name(name, ok):
    if ok:
        assert ks.check_volume_name(name) == name
    else:
        with pytest.raises(ko.OpError) as e:
            ks.check_volume_name(name)
        assert e.value.kind == "invalid"


@pytest.mark.parametrize("name,new,ok", [
    ("kvmtest-pool", True, True), ("a.b_c", True, True), ("-x", True, False),
    ("a b", True, False), ("a b", False, True), ("x\ny", False, False), ("", False, False),
])
def test_check_pool_name(name, new, ok):
    if ok:
        assert ks.check_pool_name(name, new=new) == name
    else:
        with pytest.raises(ko.OpError):
            ks.check_pool_name(name, new=new)


# --- téléversement ------------------------------------------------------------------

def test_upload_argv_stream_verify_and_note(work):
    s = sim_pool()
    f = raw_file(work / "disk.img")
    sha = hashlib.sha256(Path(f).read_bytes()).hexdigest()
    steps = []
    res = ks.upload_volume(s, "images", "disk.img", f, sha256=sha.upper(), notes_state=work,
                           author="op", step=lambda *a: steps.append(a))
    assert s.writes[0] == ("vol-create-as", "--pool", "images", "--name", "disk.img",
                           "--capacity", str(os.path.getsize(f)), "--format", "raw")
    argv, sent = s.streams[0]
    assert argv == ("env", "LC_ALL=C", "virsh", "-q", "-c", "qemu:///system", "vol-upload",
                    "--pool", "images", "--vol", "disk.img", "--file", "/dev/stdin")
    assert sent == os.path.getsize(f) and s.contents[("images", "disk.img")] == Path(f).read_bytes()
    assert res["verified"] is True and res["sha256"] == sha
    assert [a[:2] for a in steps] == [("check", "running"), ("check", "done"),
                                      ("upload", "running"), ("upload", "done"),
                                      ("verify", "running"), ("verify", "done")]
    note = ks.load_notes(work, "node2")["images/disk.img"]
    assert note["sha256"] == sha and note["author"] == "op"
    assert stat.S_IMODE(os.stat(ks.notes_path(work, "node2")).st_mode) == 0o600


def test_upload_qcow2_format_is_read_back(work):
    s = sim_pool()
    f = qcow2_file(work / "img.qcow2")
    res = ks.upload_volume(s, "images", "img.qcow2", f, verify=False)
    assert res["format"] == "qcow2" and res["verified"] is None


@pytest.mark.parametrize("label,setup,kind,text", [
    ("backing file", lambda s, w: qcow2_file(w / "b.qcow2", backing=True), "refused",
     "backing file"),
    ("empty file", lambda s, w: (Path(w / "e").write_bytes(b""), str(w / "e"))[1], "refused",
     "empty"),
    ("volume exists", lambda s, w: (s.add_vol("images", "v.img"), raw_file(w / "v"))[1],
     "refused", "already exists"),
    ("pool inactive", lambda s, w: (s.pools["images"].update(state="inactive"),
                                    raw_file(w / "v"))[1], "refused", "not active"),
    ("no space", lambda s, w: (s.pools["images"].update(available=10), raw_file(w / "v"))[1],
     "refused", "not enough space"),
    ("wrong hint", lambda s, w: raw_file(w / "v"), "refused", "not qcow2"),
])
def test_upload_refusals_write_nothing(work, label, setup, kind, text):
    s = sim_pool()
    f = setup(s, work)
    with pytest.raises(ko.OpError) as e:
        ks.upload_volume(s, "images", "v.img", f, fmt_hint="qcow2" if label == "wrong hint"
                         else None)
    assert e.value.kind == kind and text in e.value.message
    assert s.writes == [] and s.streams == []


def test_upload_checksum_mismatch_before_upload(work):
    s = sim_pool()
    f = raw_file(work / "v")
    with pytest.raises(ko.OpError) as e:
        ks.upload_volume(s, "images", "v.img", f, sha256="0" * 64)
    assert "does not match" in e.value.message and s.writes == []


def test_upload_checksum_mismatch_on_host_deletes_the_volume(work, monkeypatch):
    s = sim_pool()
    f = raw_file(work / "v")
    monkeypatch.setattr(ks, "remote_sha256", lambda *a, **k: "f" * 64)
    with pytest.raises(ko.OpError) as e:
        ks.upload_volume(s, "images", "v.img", f)
    assert "it was deleted" in e.value.message
    assert ("vol-delete", "--pool", "images", "--vol", "v.img") in s.writes
    assert "v.img" not in s.pools["images"]["vols"] and e.value.data["leftovers"] == []


def test_upload_cancel_deletes_the_partial_volume(work):
    s = sim_pool()
    f = raw_file(work / "big", 5 << 20)
    calls = []
    with pytest.raises(kh.Cancelled) as e:
        ks.upload_volume(s, "images", "v.img", f, progress=calls.append,
                         cancel=lambda: len(calls) >= 2)
    assert e.value.leftovers == []
    assert s.writes[-1] == ("vol-delete", "--pool", "images", "--vol", "v.img")
    assert "v.img" not in s.pools["images"]["vols"]


def test_upload_failure_with_undeletable_partial_names_it(work):
    s = sim_pool()
    f = raw_file(work / "v")
    s.fail_next["vol-upload"] = (1, "error: cannot send data to volume v.img\n")
    s.fail_next["vol-delete"] = (1, "error: Permission denied\n")
    with pytest.raises(kh.KvmError) as e:
        ks.upload_volume(s, "images", "v.img", f)
    assert e.value.leftovers == [{"kind": "volume", "pool": "images", "volume": "v.img",
                                  "error": "node2: denied: error: Permission denied"}]


def test_upload_without_sha256sum_on_host_is_said(work):
    s = sim_pool()
    s.sha256sum = False
    steps = []
    res = ks.upload_volume(s, "images", "v.img", raw_file(work / "v"),
                           step=lambda *a: steps.append(a))
    assert res["verified"] == "skipped"
    assert ("verify", "warn", "sha256sum is not installed on the host: not verified") in steps


def test_verify_image_uses_quiet_virsh_and_a_fresh_connection():
    s = sim_pool()
    s.add_vol("images", "a.qcow2", cap=100)
    s.contents[("images", "a.qcow2")] = b"abc"
    calls = []
    orig = s.run

    def spy(argv, timeout=None, idempotent=False, fresh=False):
        calls.append((argv, fresh, idempotent))
        return orig(argv, timeout=timeout, idempotent=idempotent, fresh=fresh)
    s.run = spy
    res = ks.verify_image(s, "images", "a.qcow2", hashlib.sha256(b"abc").hexdigest())
    assert res["match"] is True
    argv, fresh, idem = calls[0]
    assert fresh and idem and "virsh -q -c" in argv[2]
    assert ks.verify_image(s, "images", "a.qcow2", "0" * 64)["match"] is False


def test_sha_script_reports_a_failed_read(work):
    """Le script d'empreinte joué dans un vrai sh : une lecture virsh en
    échec rend un code non nul, pas la somme du vide."""
    bindir = work / "bin"
    bindir.mkdir()
    (bindir / "virsh").write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
    (bindir / "virsh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    r = subprocess.run(["sh", "-c", ks._SHA_SCRIPT, "sh", "qemu:///system", "p", "v"],
                       capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "boom" in r.stderr
    (bindir / "virsh").write_text("#!/bin/sh\nprintf abc\n")
    r = subprocess.run(["sh", "-c", ks._SHA_SCRIPT, "sh", "qemu:///system", "p", "v"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.startswith(hashlib.sha256(b"abc").hexdigest())


# --- catalogue ------------------------------------------------------------------------

def catalog_sim():
    s = sim_pool()
    s.add_pool("iso", f"{IMG}/iso")
    s.add_vol("images", "base.qcow2", fmt="qcow2")
    s.add_vol("images", "used.qcow2", fmt="qcow2")
    s.add_vol("iso", "installer", fmt="raw")
    s.contents[("iso", "installer")] = b"\0" * 32769 + b"CD001" + b"\0" * 100
    s.add_vol("iso", "plain.raw", fmt="raw")
    s.contents[("iso", "plain.raw")] = b"\0" * 40000
    s.add_domain("vm1", W.dom("vm1", [W.disk("vda", "images/used.qcow2", type="volume")]))
    return s


def test_list_images_free_volumes_without_declaration():
    res = ks.list_images(catalog_sim(), {}, {"images/base.qcow2": {"sha256": "ab" * 32}})
    names = {(i["pool"], i["volume"]): i for i in res["images"]}
    assert ("images", "used.qcow2") not in names          # cité par vm1
    assert names[("images", "base.qcow2")]["sha256"] == "ab" * 32
    assert names[("iso", "installer")]["iso"] is True       # signature CD001 lue par libvirt
    assert names[("iso", "plain.raw")]["iso"] is False


def test_list_images_declared_pools_show_used_volumes_too():
    res = ks.list_images(catalog_sim(), {"image_pools": ["images", "gone"]})
    assert res["pools"] == ["images"] and res["missing_pools"] == ["gone"]
    used = next(i for i in res["images"] if i["volume"] == "used.qcow2")
    assert used["used_by"] == ["vm1"]


# Vu en réel (tâche 7) : sans pools d'images déclarés, une image servant de
# base à un disque lié disparaissait du catalogue dès la première VM créée à
# partir d'elle (la chaîne de sauvegarde comptait comme un usage direct), et
# la fenêtre de création ne la proposait plus. Seul l'usage direct l'écarte.
def test_list_images_keeps_a_base_image_used_only_through_a_backing_chain():
    s = catalog_sim()
    s.add_vol("images", "child.qcow2", fmt="qcow2", backing=f"{IMG}/images/base.qcow2")
    s.add_domain("vm2", W.dom("vm2", [W.disk("vda", "images/child.qcow2", type="volume")]))
    res = ks.list_images(s, {})
    names = {(i["pool"], i["volume"]): i for i in res["images"]}
    base = names[("images", "base.qcow2")]
    assert base["used_by"] == [] and base["backing_users"] == [f"{IMG}/images/child.qcow2"]
    assert ("images", "child.qcow2") not in names and ("images", "used.qcow2") not in names


# --- volumes ----------------------------------------------------------------------------

def test_create_volume_blank_and_linked_argv():
    s = sim_pool()
    s.add_vol("images", "base.qcow2", cap=2 << 30, fmt="qcow2")
    ks.create_volume(s, "images", "d1.qcow2", 1 << 30)
    ks.create_volume(s, "images", "d2.qcow2", 1 << 30, backing="base.qcow2")
    assert s.writes == [
        ("vol-create-as", "--pool", "images", "--name", "d1.qcow2", "--capacity",
         str(1 << 30), "--format", "qcow2"),
        # capacité portée à celle de l'image, format de l'image de base nommé
        ("vol-create-as", "--pool", "images", "--name", "d2.qcow2", "--capacity",
         str(2 << 30), "--format", "qcow2", "--backing-vol", "base.qcow2",
         "--backing-vol-format", "qcow2")]


@pytest.mark.parametrize("kw,text", [
    ({"backing": "nope"}, "not found"),
    ({"fmt": "raw", "backing": "base.iso"}, "must be qcow2"),
    ({"backing": "base.iso"}, "only qcow2 and raw"),
    ({"fmt": "raw", "capacity": 10 ** 13}, "not enough space"),
    ({"fmt": "vmdk"}, "qcow2 or raw"),
    ({"capacity": 0}, "positive"),
])
def test_create_volume_refusals(kw, text):
    s = sim_pool(available=10 ** 12)
    s.add_vol("images", "base.iso", fmt="iso")
    args = {"capacity": 1 << 30, **kw}
    with pytest.raises(ko.OpError) as e:
        ks.create_volume(s, "images", "d.qcow2", **args)
    assert text in e.value.message and s.writes == []


def test_resize_volume_rules():
    s = sim_pool()
    s.add_vol("images", "d.qcow2", cap=1 << 30)
    s.add_domain("vm1", W.dom("vm1", [W.disk("vda", "images/d.qcow2", type="volume")]),
                 state="running")
    with pytest.raises(ko.OpError, match="running VM"):
        ks.resize_volume(s, "images", "d.qcow2", 2 << 30)
    s.domains["vm1"]["state"] = "shut off"
    with pytest.raises(ko.OpError, match="larger"):
        ks.resize_volume(s, "images", "d.qcow2", 1 << 30)
    ks.resize_volume(s, "images", "d.qcow2", 2 << 30)
    assert s.writes == [("vol-resize", "--pool", "images", "--vol", "d.qcow2", "--capacity",
                         str(2 << 30))]


@pytest.mark.parametrize("label,setup,text", [
    ("cited by an inactive VM", lambda s: s.add_domain(
        "vm1", W.dom("vm1", [W.disk("vda", "images/d.qcow2", type="volume")])), "used by vm1"),
    ("cited by path", lambda s: s.add_domain(
        "vm2", W.dom("vm2", [W.disk("vda", f"{IMG}/images/d.qcow2")])), "used by vm2"),
    ("backing of another volume", lambda s: s.add_vol(
        "images", "top.qcow2", backing=f"{IMG}/images/d.qcow2"), "backing file of"),
    ("unreadable file", lambda s: (s.add_domain("vm3", W.dom("vm3", [W.disk("vda", "/x/y.qcow2")])),
                                   s.files.__setitem__("/x/y.qcow2", None)), "could not prove"),
])
def test_delete_volume_refusals(label, setup, text):
    s = sim_pool()
    s.add_vol("images", "d.qcow2")
    setup(s)
    with pytest.raises(ko.OpError) as e:
        ks.delete_volume(s, "images", "d.qcow2")
    assert text in e.value.message and s.writes == []


def test_delete_volume_unreadable_acknowledged_then_deleted(work):
    s = sim_pool()
    s.add_vol("images", "d.qcow2")
    s.add_domain("vm3", W.dom("vm3", [W.disk("vda", "/x/y.qcow2")]))
    s.files["/x/y.qcow2"] = None
    ks.save_note(work, "node2", "images", "d.qcow2", {"sha256": "a" * 64})
    res = ks.delete_volume(s, "images", "d.qcow2", notes_state=work,
                           acknowledged_unreadable=["/x/y.qcow2"])
    assert res["deleted"] and s.writes == [("vol-delete", "--pool", "images", "--vol", "d.qcow2")]
    assert ks.load_notes(work, "node2") == {}


# --- pools ---------------------------------------------------------------------------------

POOLS = [{"name": "images", "type": "dir", "target_path": IMG},
         {"name": "harvlab", "type": "dir", "target_path": f"{IMG}/harvlab"},
         {"name": "vg", "type": "logical", "target_path": "/dev/vg0"}]


@pytest.mark.parametrize("label,spec,text", [
    ("root", {"path": "/"}, "system directory (/)"),
    ("etc child", {"path": "/etc/pools"}, "system directory (/etc)"),
    ("qemu dir", {"path": "/var/lib/libvirt/qemu/x"}, "/var/lib/libvirt/qemu"),
    ("parent of qemu dir", {"path": "/var/lib/libvirt"}, "/var/lib/libvirt/qemu"),
    ("usr", {"path": "/usr"}, "(/usr)"),
    ("relative", {"path": "pools/x"}, "absolute path"),
    ("dot dot", {"path": "/srv/../etc"}, "'..'"),
    ("same dir as a pool", {"path": f"{IMG}/harvlab/"}, "directory of pool harvlab"),
    ("inside a logical pool", {"path": "/dev/vg0/x"}, "system directory"),
    ("logical type", {"type": "logical"}, "only dir and NFS"),
    ("name taken", {"name": "images"}, "already exists"),
    ("bad name", {"name": "-x"}, "invalid pool name"),
    ("nfs bad host", {"type": "netfs", "source_host": "a b", "source_dir": "/e"},
     "invalid server"),
    ("nfs relative export", {"type": "netfs", "source_host": "nas", "source_dir": "e"},
     "exported directory"),
    ("autostart text", {"autostart": "yes"}, "true or false"),
])
def test_check_pool_spec_refusals(label, spec, text):
    full = {"name": "kvmtest-pool", "type": "dir", "path": "/srv/kvmtest", **spec}
    reasons = ks.check_pool_spec(full, POOLS)
    assert any(text in r for r in reasons), reasons


@pytest.mark.parametrize("spec", [
    {"name": "kvmtest-pool", "type": "dir", "path": f"{IMG}/kvmtest"},      # imbriqué : accepté
    {"name": "p2", "type": "dir", "path": "/srv/pools/p2", "autostart": True},
    {"name": "nfs1", "type": "netfs", "path": "/mnt/nfs1", "source_host": "172.16.0.5",
     "source_dir": "/volume1/vm"},
])
def test_check_pool_spec_accepts(spec):
    assert ks.check_pool_spec(spec, POOLS) == []


def test_pool_xml_dir_and_netfs():
    assert ks.pool_xml({"name": "p", "type": "dir", "path": "/srv/p/"}) == (
        "<pool type=\"dir\">\n  <name>p</name>\n  <target>\n    <path>/srv/p</path>\n"
        "  </target>\n</pool>\n")
    x = ks.pool_xml({"name": "n", "type": "netfs", "path": "/mnt/n", "source_host": "nas",
                     "source_dir": "/export/vm"})
    assert ("<source>\n    <host name=\"nas\" />\n    <dir path=\"/export/vm\" />\n"
            "    <format type=\"nfs\" />\n  </source>") in x


def test_define_pool_sequence_and_xml_on_stdin():
    s = sim_pool()
    s.add_pool("all", IMG)
    steps = []
    res = ks.define_pool(s, {"name": "kvmtest-pool", "type": "dir", "path": f"{IMG}/kvmtest",
                             "autostart": True}, step=lambda *a: steps.append(a))
    assert s.writes == [("pool-define", "--validate", "--file", "/dev/stdin"),
                        ("pool-build", "--pool", "kvmtest-pool"),
                        ("pool-start", "--pool", "kvmtest-pool"),
                        ("pool-autostart", "--pool", "kvmtest-pool")]
    inp = [c for c in s.calls if c[0] == "input"][0]
    assert "<path>/var/lib/libvirt/images/kvmtest</path>" in inp[2]
    assert res["nested_with"] == ["all"] and res["state"]["state"] == "running"
    assert ("plan", "done", f"dir pool at {IMG}/kvmtest (nested with pool all)") in steps


@pytest.mark.parametrize("existed,removes_dir", [(False, True), (True, False)])
def test_define_pool_rollback(existed, removes_dir):
    s = sim_pool()
    if existed:
        s.exists.add("/srv/kvmtest")
    s.fail_next["pool-start"] = (1, "error: cannot mount\n")
    with pytest.raises(kh.KvmError) as e:
        ks.define_pool(s, {"name": "kvmtest-pool", "type": "dir", "path": "/srv/kvmtest"})
    verbs = [w[0] for w in s.writes]
    assert verbs[:3] == ["pool-define", "pool-build", "pool-start"]
    assert ("pool-delete" in verbs) is removes_dir and verbs[-1] == "pool-undefine"
    assert "kvmtest-pool" not in s.pools and e.value.leftovers == []


def test_define_pool_dry_run_writes_nothing():
    s = sim_pool()
    res = ks.define_pool(s, {"name": "p", "type": "dir", "path": "/srv/p"}, dry_run=True)
    assert res["dry_run"] and "<name>p</name>" in res["xml"] and s.writes == []


def pool_sim():
    s = sim_pool()
    s.add_pool("kvmtest-pool", "/srv/kvmtest")
    return s


def test_stop_pool_refused_while_a_running_vm_uses_it():
    s = pool_sim()
    s.add_domain("vm1", W.dom("vm1", [W.disk("vda", "kvmtest-pool/a.qcow2", type="volume")]),
                 state="running")
    with pytest.raises(ko.OpError, match="running VM"):
        ks.stop_pool(s, "kvmtest-pool")
    s.domains["vm1"]["state"] = "shut off"
    ks.stop_pool(s, "kvmtest-pool")
    assert s.writes == [("pool-destroy", "--pool", "kvmtest-pool")]


@pytest.mark.parametrize("label,setup,kw,text", [
    ("inactive VM cites a volume", lambda s: s.add_domain(
        "vm1", W.dom("vm1", [W.disk("vda", "kvmtest-pool/a.qcow2", type="volume")])), {},
     "used by vm1"),
    ("inactive VM cites a file under it", lambda s: s.add_domain(
        "vm2", W.dom("vm2", [W.disk("vda", "/srv/kvmtest/sub/b.qcow2")])), {}, "used by vm2"),
    ("console writes to it", lambda s: None, {"busy": ["kvm:vm-create:node2/x"]},
     "an operation of the console"),
    ("not empty", lambda s: s.add_vol("kvmtest-pool", "left.qcow2"), {"remove_dir": True},
     "still holds 1 volume"),
    ("unreadable VM", lambda s: (s.add_domain("vm3", W.dom("vm3")),
                                 s.fail.__setitem__("dumpxml", (1, "error: boom"))), {},
     "could not prove"),
])
def test_delete_pool_refusals(label, setup, kw, text):
    s = pool_sim()
    setup(s)
    with pytest.raises(ko.OpError) as e:
        ks.delete_pool(s, "kvmtest-pool", **kw)
    assert text in e.value.message and s.writes == []


@pytest.mark.parametrize("remove_dir,verbs", [
    (False, ["pool-destroy", "pool-undefine"]),
    (True, ["pool-destroy", "pool-delete", "pool-undefine"]),
])
def test_delete_pool_sequence(remove_dir, verbs):
    s = pool_sim()
    s.add_domain("other", W.dom("other", [W.disk("vda", "images/x.qcow2", type="volume")]))
    ks.delete_pool(s, "kvmtest-pool", remove_dir=remove_dir)
    assert [w[0] for w in s.writes] == verbs and "kvmtest-pool" not in s.pools


def test_pool_users_reads_live_and_inactive_definitions():
    doms = {"domains": {
        "a": {"active": True, "inactive": {"disks": []},
              "live": {"disks": [{"type": "volume", "source": "p/x"}]}},
        "b": {"active": False, "inactive": {"disks": [{"type": "file", "source": "/srv/p/y"}]},
              "live": None}}, "unknown": []}
    assert ks.pool_users(doms, "p", "/srv/p") == ["a", "b"]
    assert ks.pool_users(doms, "p", "/srv/p", live_only=True) == ["a"]
    assert ks.pool_users(doms, "p", "/srv/pp") == ["a"]       # /srv/pp n'est pas sous /srv/p


def test_hostile_names_stay_one_argument():
    """Un nom piégé arrive tel quel en UN argument de virsh, au travers de la
    ligne de ssh (shlex) : rien n'est interprété par le shell distant."""
    seen = []

    def runner(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    st = tempfile.mkdtemp(prefix="kvms-", dir="/tmp")
    try:
        decl = {"name": "node2", "address": "172.16.1.12", "user": "ju",
                "ssh_key": "ssh/node2_id", "known_hosts": "ssh/node2_known"}
        h = kh.VirshHost(decl, st, runner=runner)
        for name in ("a;rm -rf /", "$(id)", "x y", "-x", "a\nb"):
            h.virsh(*ks.create_volume_argv(name, "v", 10))
            words = shlex.split(seen[-1][-1])
            assert words[words.index("--pool") + 1] == name
    finally:
        shutil.rmtree(st, ignore_errors=True)


# --- run_stream : vrai processus local ----------------------------------------------------

DECL = {"name": "node2", "address": "172.16.1.12", "user": "ju", "ssh_key": "ssh/node2_id",
        "known_hosts": "ssh/node2_known"}


class LocalPopen:
    """Remplace ssh : lance la ligne distante dans un `sh` local et garde
    l'argv de ssh pour les contrôles."""

    def __init__(self):
        self.argv = []

    def __call__(self, argv, **kw):
        self.argv.append(argv)
        return subprocess.Popen(["sh", "-c", argv[-1]], **kw)


@pytest.fixture
def state():
    d = tempfile.mkdtemp(prefix="kvmr-", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def test_run_stream_bytes_progress_and_fresh_connection(state, work):
    src = raw_file(work / "src", 9 << 20)
    out = work / "out"
    pop = LocalPopen()
    h = kh.VirshHost(DECL, state, popen=pop)
    seen = []
    h.run_stream(["sh", "-c", 'cat > "$1"', "sh", str(out)], src, size=9 << 20,
                 progress=seen.append)
    assert out.read_bytes() == Path(src).read_bytes()
    assert seen[-1] == 9 << 20 and seen == sorted(seen)
    assert seen[0] == 0                      # la mesure de débit part du début du flux
    argv = pop.argv[0]
    assert "ControlMaster=no" in argv and "ControlPath=none" in argv
    assert "ControlMaster=auto" not in argv and "LogLevel=ERROR" in argv


def test_run_stream_rate_limit(state, work):
    src = raw_file(work / "src", 2 << 20)
    h = kh.VirshHost(DECL, state, popen=LocalPopen())
    t0 = time.monotonic()
    h.run_stream(["sh", "-c", "cat > /dev/null"], src, size=2 << 20, limit_bps=1 << 20)
    elapsed = time.monotonic() - t0
    # 2 Mio à 1 Mio/s : seul le premier morceau part sans attendre
    assert 1.8 <= elapsed <= 3.0, elapsed


@pytest.mark.parametrize("size,delay", [(4 << 20, 1.5), (3 << 20, 1.0)])
def test_run_stream_rate_limit_ignores_time_before_the_reader(state, work, size, delay):
    """Le temps passé avant que ssh lise (échange de clés) n'est pas rendu
    en rafale : une fois la lecture commencée, le débit reste plafonné
    (crédit borné à STREAM_BURST_S). Mesuré depuis le début de la lecture :
    depuis l'appel, l'ancien seau sans plafond tenait aussi la durée totale."""
    src = raw_file(work / "src", size)
    mark = work / "reader-start"
    h = kh.VirshHost(DECL, state, popen=LocalPopen())
    h.run_stream(["sh", "-c", f'sleep {delay}; date +%s.%N > "$1"; cat >/dev/null', "sh", str(mark)],
                 src, size=size, limit_bps=1 << 20)
    after_read = time.time() - float(mark.read_text())
    # tout sauf le tuyau (64 Kio) et le crédit borné (0,25 s) part à 1 Mio/s
    floor = (size - (64 << 10)) / (1 << 20) - kh.STREAM_BURST_S - 0.15
    assert after_read >= floor, (after_read, floor)
    if size == 4 << 20:
        assert after_read >= 3.6, after_read


def test_run_stream_rate_limit_tiny_rate_still_moves(state, work):
    """Débit minuscule : le crédit couvre au moins un octet (jamais bloqué)."""
    src = raw_file(work / "src", 6)
    h = kh.VirshHost(DECL, state, popen=LocalPopen())
    t0 = time.monotonic()
    h.run_stream(["sh", "-c", "cat > /dev/null"], src, size=6, limit_bps=4)
    assert 1.0 <= time.monotonic() - t0 <= 3.5


def test_run_stream_remote_stops_reading_is_classified(state, work):
    src = raw_file(work / "src", 8 << 20)
    h = kh.VirshHost(DECL, state, popen=LocalPopen())
    with pytest.raises(kh.KvmError) as e:
        h.run_stream(["sh", "-c", "head -c 1000 >/dev/null; echo 'error: stream aborted' >&2; "
                      "exit 1"], src, size=8 << 20)
    assert e.value.kind == "failed" and "stream aborted" in e.value.message


def test_run_stream_remote_exits_zero_early_is_an_error(state, work):
    src = raw_file(work / "src", 8 << 20)
    h = kh.VirshHost(DECL, state, popen=LocalPopen())
    with pytest.raises(kh.KvmError, match="stopped reading"):
        h.run_stream(["sh", "-c", "head -c 1000 >/dev/null"], src, size=8 << 20)


def test_run_stream_cancel_stops_the_process(state, work):
    src = raw_file(work / "src", 8 << 20)
    pop = LocalPopen()
    procs = []
    orig = pop.__call__

    def popen(argv, **kw):
        p = orig(argv, **kw)
        procs.append(p)
        return p
    h = kh.VirshHost(DECL, state, popen=popen)
    seen = []
    with pytest.raises(kh.Cancelled) as e:
        h.run_stream(["sh", "-c", "sleep 30"], src, size=8 << 20, progress=seen.append,
                     cancel=lambda: True)
    assert procs[0].poll() is not None and e.value.sent < 8 << 20


def test_run_stream_source_shorter_than_announced(state, work):
    src = raw_file(work / "src", 1000)
    h = kh.VirshHost(DECL, state, popen=LocalPopen())
    with pytest.raises(kh.KvmError, match="expected"):
        h.run_stream(["sh", "-c", "cat >/dev/null"], src, size=2000)


def test_run_stream_hides_state_paths(state, work):
    src = raw_file(work / "src", 10)
    h = kh.VirshHost(DECL, state, popen=LocalPopen())
    with pytest.raises(kh.KvmError) as e:
        h.run_stream(["sh", "-c", f"cat >/dev/null; echo 'error: {state}/ssh/node2_id bad' >&2; "
                      "exit 1"], src, size=10)
    assert str(state) not in e.value.message and "<state>" in e.value.message
