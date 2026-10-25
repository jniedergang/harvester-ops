"""Prochaine version KVM : création de VMs (bin/lib/kvm_create.py)."""

import json
import xml.etree.ElementTree as ET

import pytest

from kvm_next_sim import Sim2, W
import iso9660
import kvm_create as kc
import kvm_host as kh
import kvm_ops as ko

IMG = "/var/lib/libvirt/images"
SECRET = "Ud7SeCrEt99"          # valeur factice du user-data : ne doit sortir nulle part
UID = "11111111-2222-3333-4444-555555555555"


def world(available=10 ** 12):
    s = Sim2()
    s.add_pool("images", f"{IMG}/images", available=available)
    s.add_pool("other", f"{IMG}/other", available=available)
    s.add_vol("images", "cirros.qcow2", cap=112 << 20, alloc=21 << 20, fmt="qcow2")
    s.add_vol("images", "installer.iso", cap=300 << 20, fmt="iso")
    s.add_vol("images", "data.qcow2", cap=1 << 30, fmt="qcow2")
    s.add_net("kvmtest-net", "10.213.77.0/24")
    s.add_net("off-net", "10.213.78.0/24", active=False)
    return s


def spec(**over):
    sp = {"name": "kvmtest-1", "os": "linux", "machine": "q35",
          "cpu": {"vcpus": 1, "max_vcpus": 2, "hotplug": True},
          "memory": {"current_mib": 256, "max_mib": 512},
          "disks": [{"source": {"kind": "image", "pool": "images", "image": "cirros.qcow2",
                                "linked": True, "size_bytes": 1 << 30}, "boot_order": 1}],
          "nics": [{"kind": "network", "source": "kvmtest-net"}],
          "display": {"vnc": True, "password": True},
          "cloudinit": {"user_data": f"#cloud-config\npassword: {SECRET}\n",
                        "hostname": "guest-one", "seed_pool": "images"}}
    sp.update(over)
    return sp


def create(s, sp, **kw):
    steps = []
    res = kc.create_vm(s, sp, progress=lambda *a: steps.append(a),
                       uuid_gen=lambda: UID, password_gen=lambda: W.FAKE_PASSWORD, **kw)
    return res, steps


def all_text(s, *objs):
    parts = [json.dumps(o, default=str) for o in objs]
    for c in s.calls:
        parts.append(repr(c[:2]))
    return "\n".join(parts)


def test_host_options_reads_caps_once_per_family():
    s = world()
    opts = kc.host_options(s)
    assert opts["default_machine"] == "q35" and "pc" in opts["by_machine"]
    cmds = [c[1] for c in s.calls if c[0] in ("virsh", "batch")]
    assert cmds[0] == ("capabilities",)
    assert ("domcapabilities", "--virttype", "kvm", "--arch", "x86_64", "--machine", "q35") in cmds


def test_create_linked_disk_seed_define_start_argv():
    s = world()
    res, steps = create(s, spec(), start=True)
    w = s.writes
    assert w[0] == ("vol-create-as", "--pool", "images", "--name", "kvmtest-1-vda.qcow2",
                    "--capacity", str(1 << 30), "--format", "qcow2", "--backing-vol",
                    "cirros.qcow2", "--backing-vol-format", "qcow2")
    assert w[1][:6] == ("vol-create-as", "--pool", "images", "--name", "kvmtest-1-cidata.iso",
                        "--capacity") and w[1][-2:] == ("--format", "raw")
    assert w[2] == ("vol-upload", "images", "kvmtest-1-cidata.iso")
    assert w[3] == ("define", "--validate", "/dev/stdin")
    assert w[4] == ("start", "--domain", "kvmtest-1")
    # le XML part sur l'entrée standard, jamais dans un argv
    inp = [c for c in s.calls if c[0] == "input"]
    assert len(inp) == 1 and inp[0][1][5:] == ("define", "--validate", "/dev/stdin")
    root = ET.fromstring(inp[0][2])
    assert root.findtext("uuid") == UID
    assert root.find("devices/graphics").get("passwd") == W.FAKE_PASSWORD
    seed = root.find("metadata/{https://harvester-ops.io/xmlns/vm/1}vm/"
                     "{https://harvester-ops.io/xmlns/vm/1}seed")
    assert seed.attrib == {"pool": "images", "volume": "kvmtest-1-cidata.iso"}
    assert [st[0] for st in steps] == ["plan", "plan", "volume-vda", "volume-vda", "seed", "seed",
                                       "define", "define", "start", "start", "verify", "verify"]
    assert res["created"][0]["uuid"] == UID and res["created"][0]["state"] == "running"
    assert res["created"][0]["seed"] == "images/kvmtest-1-cidata.iso"


def test_seed_carries_the_domain_uuid_and_the_hostname():
    s = world()
    create(s, spec())
    iso = iso9660.read_iso(s.contents[("images", "kvmtest-1-cidata.iso")])
    assert iso["label"] == "cidata"
    meta = json.loads(iso["files"]["meta-data"])
    assert meta == {"instance-id": UID, "local-hostname": "guest-one"}
    assert SECRET.encode() in iso["files"]["user-data"]


def test_full_copy_into_another_pool_on_a_fresh_connection():
    s = world()
    sp = spec(cloudinit=None)
    sp["disks"] = [{"source": {"kind": "image", "pool": "other", "image_pool": "images",
                               "image": "cirros.qcow2", "linked": False,
                               "size_bytes": 1 << 30}}]
    create(s, sp)
    assert s.writes[0] == ("vol-create-from", "--pool", "other", "--file", "/dev/stdin", "--vol",
                           "cirros.qcow2", "--inputpool", "images")
    assert s.writes[1] == ("vol-resize", "--pool", "other", "--vol", "kvmtest-1-vda.qcow2",
                           "--capacity", str(1 << 30))
    assert ("vol-create-from", True) in s.fresh_inputs
    vxml = [c for c in s.calls if c[0] == "input"][0][2]
    assert "<backingStore" not in vxml and "<capacity unit=\"bytes\">117440512</capacity>" in vxml


def test_blank_disk_iso_cdrom_and_existing_volume():
    s = world()
    sp = spec(cloudinit=None, display={"vnc": True})
    sp["disks"] = [{"source": {"kind": "blank", "pool": "images", "size_bytes": 8 << 30}},
                   {"source": {"kind": "iso", "pool": "images", "volume": "installer.iso"},
                    "boot_order": 1},
                   {"source": {"kind": "volume", "pool": "images", "volume": "data.qcow2",
                               "format": "qcow2"}}]
    res, _ = create(s, sp)
    creates = [w for w in s.writes if w[0] == "vol-create-as"]
    assert creates == [("vol-create-as", "--pool", "images", "--name", "kvmtest-1-vda.qcow2",
                        "--capacity", str(8 << 30), "--format", "qcow2")]
    root = ET.fromstring([c for c in s.calls if c[0] == "input"][0][2])
    assert root.find("devices/graphics").get("passwd") is None
    srcs = [(d.get("device"), d.find("source").get("volume") if d.find("source") is not None
             else None) for d in root.findall("devices/disk")]
    assert srcs == [("disk", "kvmtest-1-vda.qcow2"), ("cdrom", "installer.iso"),
                    ("disk", "data.qcow2")]
    assert res["created"][0]["volumes"] == ["images/kvmtest-1-vda.qcow2"]


@pytest.mark.parametrize("label,mutate,text", [
    ("name taken", lambda s, sp: s.add_domain("kvmtest-1", W.dom("kvmtest-1")),
     "a VM named kvmtest-1 already exists"),
    ("pool inactive", lambda s, sp: s.pools["images"].update(state="inactive"),
     "pool images is not active"),
    ("image missing", lambda s, sp: sp["disks"][0]["source"].update(image="nope.qcow2"),
     "images/nope.qcow2 not found"),
    ("network missing", lambda s, sp: sp["nics"][0].update(source="nonet"),
     "network nonet not found"),
    ("volume exists", lambda s, sp: s.add_vol("images", "kvmtest-1-vda.qcow2"),
     "a volume named kvmtest-1-vda.qcow2 already exists"),
    ("seed exists", lambda s, sp: s.add_vol("images", "kvmtest-1-cidata.iso"),
     "kvmtest-1-cidata.iso already exists"),
    ("no space for a copy", lambda s, sp: (s.pools["other"].update(available=10),
                                           sp["disks"][0]["source"].update(
                                               pool="other", image_pool="images",
                                               linked=False)),
     "not enough space in pool other"),
    ("fixed MAC on several instances", lambda s, sp: (sp.update(instances=2),
                                                     sp["nics"][0].update(
                                                         mac="52:54:00:aa:bb:cc")),
     "cannot share fixed MAC"),
    ("MAC used on the host", lambda s, sp: (s.add_domain("vmx", W.dom("vmx")),
                                            sp["nics"][0].update(mac="52:54:00:00:00:00")),
     "already used on this host"),
    ("size below the image", lambda s, sp: sp["disks"][0]["source"].update(size_bytes=1024),
     "below the image virtual size"),
    ("bridge not on the host", lambda s, sp: sp["nics"].append({"kind": "bridge",
                                                                "source": "nobr0"}),
     "nobr0 is not a bridge of the host"),
])
def test_plan_refusals_write_nothing(label, mutate, text):
    s = world()
    sp = spec()
    mutate(s, sp)
    with pytest.raises(ko.OpError) as e:
        create(s, sp)
    assert e.value.kind == "refused"
    assert any(text in r for r in e.value.data["refusals"]), e.value.data["refusals"]
    assert s.writes == []


def test_plan_warns_about_an_inactive_network_and_hides_the_password():
    s = world()
    sp = spec()
    sp["nics"] = [{"kind": "network", "source": "off-net"}]
    plan = kc.create_plan(s, sp)
    assert plan["possible"]
    assert any("off-net is inactive" in w for w in plan["warnings"])
    assert 'passwd="***"' in plan["instances"][0]["xml_preview"]
    pub = kc.public_plan(plan)
    assert "_instances" not in pub and SECRET not in json.dumps(pub, default=str)


def test_three_instances_second_fails_at_define():
    s = world()
    sp = spec(instances=3, cloudinit=None)
    sp["disks"] = [{"source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 30}}]
    uids = iter(["u-1", "u-2", "u-3"])
    real = s.v_define
    count = {"n": 0}

    def define(o, p, data):
        count["n"] += 1
        if count["n"] == 2:
            return 1, "", "error: XML error: boom\n"
        return real(o, p, data)
    s.v_define = define
    with pytest.raises(kh.KvmError) as e:
        kc.create_vm(s, sp, uuid_gen=lambda: next(uids), password_gen=lambda: "pw")
    assert [c["name"] for c in e.value.created] == ["kvmtest-1-01"]
    assert e.value.failed == "kvmtest-1-02" and e.value.leftovers == []
    vols = set(s.pools["images"]["vols"])
    assert "kvmtest-1-01-vda.qcow2" in vols and "kvmtest-1-02-vda.qcow2" not in vols
    assert "kvmtest-1-03-vda.qcow2" not in vols and "kvmtest-1-03" not in s.domains
    assert "kvmtest-1-01" in s.domains and "kvmtest-1-02" not in s.domains


def test_rollback_never_deletes_an_existing_source_volume():
    s = world()
    sp = spec(cloudinit=None)
    sp["disks"].append({"source": {"kind": "volume", "pool": "images", "volume": "data.qcow2",
                                   "format": "qcow2"}})
    s.fail_next["define"] = (1, "error: XML error\n")
    with pytest.raises(kh.KvmError) as e:
        create(s, sp)
    assert "data.qcow2" in s.pools["images"]["vols"]
    assert "cirros.qcow2" in s.pools["images"]["vols"]
    assert "kvmtest-1-vda.qcow2" not in s.pools["images"]["vols"]
    deleted = [w for w in s.writes if w[0] == "vol-delete"]
    assert deleted == [("vol-delete", "--pool", "images", "--vol", "kvmtest-1-vda.qcow2")]
    assert e.value.leftovers == []


def test_rollback_after_start_failure_undefines_and_deletes():
    s = world()
    s.fail_next["start"] = (1, "error: network kvmtest-net is not active\n")
    with pytest.raises(kh.KvmError):
        create(s, spec(), start=True)
    verbs = [w[0] for w in s.writes]
    assert verbs[-3:] == ["undefine", "vol-delete", "vol-delete"]
    assert ("undefine", "--domain", "kvmtest-1", "--nvram") in s.writes
    assert "kvmtest-1" not in s.domains
    assert {"kvmtest-1-vda.qcow2", "kvmtest-1-cidata.iso"}.isdisjoint(s.pools["images"]["vols"])


def test_rollback_leaves_a_domain_that_is_not_ours():
    s = world()

    def define(o, p, data):
        # une autre console a défini le même nom entre-temps avec un autre uuid
        s.add_domain("kvmtest-1", W.dom("kvmtest-1", uid="other-uuid"))
        return 1, "", "error: operation failed: domain 'kvmtest-1' already exists\n"
    s.v_define = define
    with pytest.raises(kh.KvmError):
        create(s, spec(cloudinit=None))
    assert "kvmtest-1" in s.domains
    assert not any(w[0] == "undefine" for w in s.writes)


def test_cancel_between_steps_rolls_back():
    s = world()
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 2
    with pytest.raises(kh.Cancelled) as e:
        create(s, spec(), cancel=cancel)
    assert e.value.leftovers == []
    assert "kvmtest-1-vda.qcow2" not in s.pools["images"]["vols"]
    assert "kvmtest-1" not in s.domains


def test_no_secret_in_steps_results_errors_or_calls():
    s = world()
    res, steps = create(s, spec())
    text = all_text(s, res, steps)
    assert SECRET not in text and W.FAKE_PASSWORD not in text
    # erreur de define qui recopierait l'entrée : expurgée
    s2 = world()
    s2.fail_next["define"] = (1, f"error: bad passwd='{W.FAKE_PASSWORD}' near {SECRET}\n")
    with pytest.raises(kh.KvmError) as e:
        create(s2, spec())
    assert W.FAKE_PASSWORD not in e.value.message


def test_dry_run_plan_writes_nothing():
    s = world()
    plan = kc.create_plan(s, spec(instances=2))
    assert [i["name"] for i in plan["instances"]] == ["kvmtest-1-01", "kvmtest-1-02"]
    assert s.writes == []


@pytest.mark.parametrize("n,ok", [(1, True), (20, True), (0, False), (21, False),
                                  (True, False), ("2", False)])
def test_instance_count(n, ok):
    if ok:
        assert len(kc.instance_specs({"name": "a", "instances": n})) == n
    else:
        with pytest.raises(ko.OpError):
            kc.instance_specs({"name": "a", "instances": n})


@pytest.mark.parametrize("name,host", [("kvmtest-1", "kvmtest-1"), ("My VM_2", "my-vm-2"),
                                       ("___", "vm"), ("a" * 80, "a" * 63)])
def test_guest_hostname(name, host):
    assert kc.guest_hostname(name) == host


def test_vnc_password_shape():
    pw = kc.vnc_password()
    assert len(pw) == 8 and pw.isalnum() and kc.vnc_password() != pw


def test_hostile_vm_name_reaches_virsh_as_one_argument():
    s = world()
    name = "a;b $(id)"
    create(s, spec(name=name, cloudinit=None))
    assert ("define", "--validate", "/dev/stdin") in s.writes
    assert name in s.domains
    assert ("vol-create-as", "--pool", "images", "--name", f"{name}-vda.qcow2") == \
        s.writes[0][:5]


# --- retour arrière d'une copie complète encore en cours (vu en réel, tâche 7) ------
# Annulée pendant `vol-create-from`, la copie continue dans libvirtd : le
# volume refuse d'être supprimé (« still being allocated ») jusqu'à la fin de
# la copie. Le retour arrière attend (recul borné) puis le supprime.

class _BuildingHost:
    def __init__(self, refusals):
        self.refusals, self.calls = list(refusals), []

    def virsh(self, *args, **kw):
        self.calls.append(args)
        if args[0] == "vol-delete" and self.refusals:
            raise kh.KvmError("failed", self.refusals.pop(0), rc=1)
        return ""


BUILDING = "error: Requested operation is not valid: volume 'x' is still being allocated."
ROLLBACK_CASES = [
    # (libellé, refus successifs, reste attendu, appels vol-delete)
    ("copy ends while waiting", [BUILDING] * 3, None, 4),
    ("still copying at the deadline", [BUILDING] * 500, "may still be building", None),
    ("other error said at once", ["error: disk on fire"], "disk on fire", 1),
]


@pytest.mark.parametrize("label,refusals,left,calls", ROLLBACK_CASES, ids=[c[0] for c in ROLLBACK_CASES])
def test_rollback_waits_for_a_volume_libvirt_is_still_copying(label, refusals, left, calls, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(kc, "BUILDING_DELETE_RETRY", 60.0)
    host = _BuildingHost(refusals)
    created = [{"kind": "volume", "pool": "p", "volume": "x", "building": True}]
    out = kc.rollback(host, created, sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                      clock=lambda: clock["t"])
    deletes = [c for c in host.calls if c[0] == "vol-delete"]
    if left is None:
        assert out == [] and len(deletes) == calls
    else:
        assert len(out) == 1 and left in out[0]["error"] and "building" not in out[0]
        if calls:
            assert len(deletes) == calls
        else:
            assert clock["t"] <= 60.0 and len(deletes) > 3


def test_rollback_of_a_finished_volume_does_not_wait(monkeypatch):
    host = _BuildingHost([BUILDING])
    out = kc.rollback(host, [{"kind": "volume", "pool": "p", "volume": "x"}],
                      sleep=lambda s: pytest.fail("no wait for a volume that is not being built"))
    assert len(out) == 1 and "still being allocated" in out[0]["error"]
