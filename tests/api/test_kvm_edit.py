"""Prochaine version KVM : modification d'une VM, gestes à chaud et seed
cloud-init (bin/lib/kvm_edit.py, et la reconnaissance du seed par kvm_ops)."""

import json
import xml.etree.ElementTree as ET

import pytest

from kvm_next_sim import Sim2, W, fx
import iso9660
import kvm_create as kc
import kvm_edit as ke
import kvm_host as kh
import kvm_ops as ko
import kvm_xml as kx

IMG = "/var/lib/libvirt/images"
MAC = "52:54:00:00:00:aa"
OPTS = kc.host_options(Sim2())


def base_spec(**over):
    sp = {"name": "vm1", "os": "linux", "machine": "q35",
          "cpu": {"vcpus": 1, "max_vcpus": 4, "hotplug": True},
          "memory": {"current_mib": 512, "max_mib": 1024},
          "disks": [{"target": "vda", "source": {"kind": "volume", "pool": "images",
                                                 "volume": "d.qcow2", "format": "qcow2"}},
                    {"target": "sda", "source": {"kind": "empty"}}],
          "nics": [{"kind": "network", "source": "default", "mac": MAC}],
          "display": {"vnc": True, "password": False}}
    sp.update(over)
    return sp


def vm_world(state="shut off", persistent=True, spec=None):
    s = Sim2()
    s.add_pool("images", f"{IMG}/images")
    s.add_vol("images", "d.qcow2", fmt="qcow2")
    s.add_vol("images", "base.qcow2", cap=1 << 30, fmt="qcow2")
    s.add_vol("images", "boot.iso", fmt="iso")
    s.add_net("default", "192.168.122.0/24")
    xml = kx.build_domain(spec or base_spec(), OPTS)
    xml = xml.replace("<name>vm1</name>", "<name>vm1</name><uuid>u-vm1</uuid>")
    s.add_domain("vm1", xml, state=state, persistent=persistent,
                 live=xml if state in ("running", "paused") else None)
    return s


def plan_for(s, spec, gesture=False):
    return ke.edit_plan(s, "vm1", spec, options=OPTS, gesture=gesture)


# --- table HOTNESS jouée ligne par ligne -----------------------------------------------

NEW_DISK = {"bus": "virtio", "source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 20}}
SATA_DISK = {"bus": "sata", "source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 20}}
DISKS = [{"target": "vda"}, {"target": "sda"}]
CASES = [
    # (libellé, spec, champ, running, shut off, paused, transient)
    ("vcpus up", {"cpu": {"vcpus": 2}}, "cpu.vcpus", "now", "now", "refused", "refused"),
    ("vcpus above max", {"cpu": {"vcpus": 6, "max_vcpus": 8}}, "cpu.vcpus", "next-boot", "now",
     "refused", "refused"),
    ("memory", {"memory": {"current_mib": 768}}, "memory.current_mib", "now", "now", "refused",
     "refused"),
    ("memory max", {"memory": {"max_mib": 2048}}, "memory.max_mib", "next-boot", "now",
     "refused", "refused"),
    ("cpu mode", {"cpu": {"mode": "host-passthrough"}}, "cpu.mode", "next-boot", "now",
     "refused", "refused"),
    ("video", {"display": {"video": "vga"}}, "display.video", "next-boot", "now", "refused",
     "refused"),
    ("slots", {"hotplug_slots": 6}, "hotplug_slots", "next-boot", "now", "refused", "refused"),
    ("agent", {"guest_agent": False}, "guest_agent", "next-boot", "now", "refused", "refused"),
    ("title", {"title": "T"}, "title", "now", "now", "refused", "refused"),
    ("description", {"description": "D"}, "description", "now", "now", "refused", "refused"),
    ("autostart", {"autostart": True}, "autostart", "now", "now", "refused", "refused"),
    ("add virtio disk", {"disks": DISKS + [NEW_DISK]}, "disks[+3]", "now", "now", "refused",
     "refused"),
    ("add sata disk", {"disks": DISKS + [SATA_DISK]}, "disks[+3]", "next-boot", "now",
     "refused", "refused"),
    ("remove virtio disk", {"disks": [{"target": "sda"}]}, "disks[vda]", "now", "now",
     "refused", "refused"),
    ("remove cdrom", {"disks": [{"target": "vda"}]}, "disks[sda]", "next-boot", "now", "refused",
     "refused"),
    ("insert media", {"disks": [{"target": "vda"}, {"target": "sda", "source": {
        "kind": "iso", "pool": "images", "volume": "boot.iso"}}]}, "disks[sda].source", "now",
     "now", "refused", "refused"),
    ("disk cache", {"disks": [{"target": "vda", "cache": "writeback"}, {"target": "sda"}]},
     "disks[vda].cache", "next-boot", "now", "refused", "refused"),
    ("add nic", {"nics": [{"mac": MAC}, {"kind": "network", "source": "default"}]}, "nics[+2]",
     "now", "now", "refused", "refused"),
    ("remove nic", {"nics": []}, f"nics[{MAC}]", "now", "now", "refused", "refused"),
    ("nic model", {"nics": [{"mac": MAC, "model": "e1000e"}]}, f"nics[{MAC}].model",
     "next-boot", "now", "refused", "refused"),
    ("rename", {"name": "vm2"}, "name", "refused", "now", "refused", "refused"),
    ("balloon", {"memory": {"balloon": False}}, "memory.balloon", "refused", "refused",
     "refused", "refused"),
]


@pytest.mark.parametrize("label,spec,field,running,stopped,paused,transient", CASES,
                         ids=[c[0] for c in CASES])
def test_hotness_table(label, spec, field, running, stopped, paused, transient):
    for state, persistent, expected in (("running", True, running), ("shut off", True, stopped),
                                        ("paused", True, paused),
                                        ("running", False, transient)):
        s = vm_world(state, persistent)
        plan = plan_for(s, spec)
        got = {c["field"]: c["applied"] for c in plan["changes"]}
        assert got.get(field) == expected, (state, persistent, plan["changes"])
        assert s.writes == []                  # un plan n'écrit jamais


def test_every_classified_field_has_a_hotness_rule():
    for _, spec, field, *_ in CASES:
        if field == "memory.balloon":
            continue
        assert ke._rule(field) is not None or field.endswith("]"), field
    fields = {r["field"] for r in ke.HOTNESS}
    assert {"cpu.vcpus", "memory.current_mib", "disks[+]", "disks[-]", "nics[+]", "nics[-]",
            "disks[].source", "name", "autostart", "cloudinit"} <= fields


def test_transient_full_edit_is_refused_with_the_reason():
    s = vm_world("running", persistent=False)
    with pytest.raises(ko.OpError) as e:
        ke.edit_vm(s, "vm1", {"title": "x"}, options=OPTS)
    assert e.value.kind == "refused" and "transient VM" in e.value.message and s.writes == []


def test_transient_gesture_is_live_only():
    s = vm_world("running", persistent=False)
    ke.set_cpu_memory(s, "vm1", vcpus=2)
    assert s.writes == [("setvcpus", "--domain", "vm1", "--count", "2", "--live")]


# --- commandes -------------------------------------------------------------------------

def test_running_vm_live_commands_then_one_define():
    s = vm_world("running")
    form = ke.vm_form(s, "vm1")
    res = ke.edit_vm(s, "vm1", {"cpu": {"vcpus": 2}, "memory": {"current_mib": 768,
                                                                 "max_mib": 2048},
                                "title": "T"}, form["config_digest"], options=OPTS,
                     sleep=lambda x: None)
    assert s.writes == [
        ("setvcpus", "--domain", "vm1", "--count", "2", "--live", "--config"),
        ("setmem", "--domain", "vm1", "--size", str(768 * 1024), "--live", "--config"),
        ("desc", "--domain", "vm1", "--live", "--config", "--title", "--new-desc", "T"),
        ("define", "--validate", "/dev/stdin")]
    applied = {c["field"]: c["applied"] for c in res["changes"]}
    assert applied == {"cpu.vcpus": "now", "memory.current_mib": "now",
                       "memory.max_mib": "next-boot", "title": "now"}
    root = ET.fromstring(s.domains["vm1"]["xml"])
    assert root.findtext("memory") == str(2048 * 1024)


def test_stopped_vm_everything_in_one_define():
    s = vm_world("shut off")
    form = ke.vm_form(s, "vm1")
    ke.edit_vm(s, "vm1", {"cpu": {"vcpus": 2}, "title": "T", "tablet": False,
                          "disks": DISKS + [NEW_DISK]}, form["config_digest"], options=OPTS)
    assert [w[0] for w in s.writes] == ["vol-create-as", "define"]
    root = ET.fromstring(s.domains["vm1"]["xml"])
    assert root.find("vcpu").get("current") == "2" and root.findtext("title") == "T"
    assert not [i for i in root.findall("devices/input") if i.get("type") == "tablet"]
    assert "vdb" in [d.find("target").get("dev") for d in root.findall("devices/disk")]


def test_stale_digest_is_refused_before_anything():
    s = vm_world("running")
    with pytest.raises(ko.OpError) as e:
        ke.edit_vm(s, "vm1", {"title": "x"}, "0" * 64, options=OPTS)
    assert e.value.kind == "changed" and "reload" in e.value.message and s.writes == []


def test_empty_edit_writes_nothing():
    s = vm_world("running")
    form = ke.vm_form(s, "vm1")
    res = ke.edit_vm(s, "vm1", {"cpu": {"vcpus": 1}, "title": None}, form["config_digest"],
                     options=OPTS)
    assert res["empty"] and s.writes == []


def test_vcpu_above_max_is_refused_as_a_whole():
    s = vm_world("shut off")
    with pytest.raises(ko.OpError, match="above the maximum"):
        ke.edit_vm(s, "vm1", {"cpu": {"vcpus": 9}}, options=OPTS)
    assert s.writes == []


def test_vcpu_decrease_falls_back_to_next_boot():
    s = vm_world("running", spec=base_spec(cpu={"vcpus": 3, "max_vcpus": 4, "hotplug": True}))
    s.vcpu_unplug_ok = False
    res = ke.set_cpu_memory(s, "vm1", vcpus=1)
    assert s.writes == [("setvcpus", "--domain", "vm1", "--count", "1", "--live", "--config"),
                        ("setvcpus", "--domain", "vm1", "--count", "1", "--config")]
    assert res["changes"][0]["applied"] == "next-boot"
    assert "QEMU refused" in res["changes"][0]["reason"]


def test_memory_the_guest_does_not_follow_is_said():
    s = vm_world("running")
    s.memstat_follow = False
    res = ke.set_cpu_memory(s, "vm1", memory_mib=768, sleep=lambda x: None)
    assert res["changes"][0]["applied"] == "now"
    assert "no balloon driver" in res["changes"][0]["reason"]


def test_hot_attach_disk_creates_then_attaches():
    s = vm_world("running")
    res = ke.attach_disk(s, "vm1", NEW_DISK)
    assert s.writes[0] == ("vol-create-as", "--pool", "images", "--name", "vm1-vdb.qcow2",
                           "--capacity", str(1 << 20), "--format", "qcow2")
    assert s.writes[1] == ("attach-device", "--domain", "vm1", "--file", "/dev/stdin",
                           "--live", "--config")
    xml = [c for c in s.calls if c[0] == "input"][0][2]
    assert "volume=\"vm1-vdb.qcow2\"" in xml and "dev=\"vdb\"" in xml
    assert res["changes"][0]["target"] == "vdb"


def test_hot_attach_linked_to_an_image():
    s = vm_world("running")
    ke.attach_disk(s, "vm1", {"source": {"kind": "image", "pool": "images",
                                         "image": "base.qcow2", "size_bytes": 1}})
    assert s.writes[0] == ("vol-create-as", "--pool", "images", "--name", "vm1-vdb.qcow2",
                           "--capacity", str(1 << 30), "--format", "qcow2", "--backing-vol",
                           "base.qcow2", "--backing-vol-format", "qcow2")


def test_hot_attach_failure_deletes_the_new_volume():
    s = vm_world("running")
    s.fail_next["attach-device"] = (1, "error: internal error: no more available PCI slots\n")
    with pytest.raises(kh.KvmError) as e:
        ke.attach_disk(s, "vm1", NEW_DISK)
    assert ("vol-delete", "--pool", "images", "--vol", "vm1-vdb.qcow2") in s.writes
    assert "vm1-vdb.qcow2" not in s.pools["images"]["vols"]
    assert e.value.changes[0]["applied"] == "failed"
    assert "no more available PCI slots" in e.value.changes[0]["reason"]


def test_no_free_pcie_slot_waits_for_the_next_start():
    s = vm_world("running", spec=base_spec(hotplug_slots=0))
    # sans adresses (XML jamais défini), les ports comptés libres sont ceux du
    # modèle : on simule un XML vivant où tous les ports sont occupés
    live = ET.fromstring(s.domains["vm1"]["live"])
    for c in live.findall("devices/controller"):
        if c.get("model") == "pcie-root-port":
            ET.SubElement(live.find("devices"), "x").append(
                ET.Element("address", type="pci", bus=hex(int(c.get("index")))))
    s.domains["vm1"]["live"] = ET.tostring(live, encoding="unicode")
    plan = plan_for(s, {"disks": DISKS + [NEW_DISK]}, gesture=True)
    ch = plan["changes"][0]
    assert ch["applied"] == "next-boot" and "no free PCIe slot" in ch["reason"]


def test_detach_disk_waits_and_never_deletes_the_volume():
    s = vm_world("running")
    slept = []
    res = ke.detach_disk(s, "vm1", "vda", sleep=slept.append)
    assert s.writes == [("detach-device", "--domain", "vm1", "--file", "/dev/stdin", "--live",
                         "--config")]
    assert res["changes"][0]["applied"] == "now" and slept == []
    assert "d.qcow2" in s.pools["images"]["vols"]


def test_detach_disk_the_guest_does_not_release():
    s = vm_world("running")
    s.detach_sticky = True
    slept = []
    res = ke.detach_disk(s, "vm1", "vda", sleep=slept.append)
    assert res["changes"][0]["applied"] == "next-boot"
    assert res["changes"][0]["reason"] == ke.REASON_DETACH_PENDING
    assert sum(slept) == ke.DETACH_WAIT
    assert not any(w[0] == "vol-delete" for w in s.writes)


def test_media_insert_and_eject():
    s = vm_world("running")
    ke.insert_media(s, "vm1", "sda", "images", "boot.iso")
    assert s.writes[-1] == ("update-device", "--domain", "vm1", "--file", "/dev/stdin", "--live",
                            "--config")
    xml = [c for c in s.calls if c[0] == "input"][-1][2]
    assert "<source pool=\"images\" volume=\"boot.iso\" />" in xml and "type=\"volume\"" in xml
    ke.eject_media(s, "vm1", "sda")
    assert s.writes[-1] == ("change-media", "--domain", "vm1", "--path", "sda", "--eject",
                            "--live", "--config")
    with pytest.raises(ko.OpError, match="not a CD-ROM"):
        ke.eject_media(s, "vm1", "vda")


def test_nic_attach_and_detach():
    s = vm_world("running")
    ke.attach_nic(s, "vm1", {"kind": "network", "source": "default"})
    assert s.writes[-1] == ("attach-device", "--domain", "vm1", "--file", "/dev/stdin", "--live",
                            "--config")
    ke.detach_nic(s, "vm1", MAC.upper())
    assert s.writes[-1] == ("detach-interface", "--domain", "vm1", "--type", "network", "--mac",
                            MAC, "--live", "--config")


def test_rename_stopped_vm_last():
    s = vm_world("shut off")
    res = ke.edit_vm(s, "vm1", {"name": "vm2", "title": "T"}, options=OPTS)
    assert [w[0] for w in s.writes] == ["define", "domrename"]
    assert res["name"] == "vm2" and "vm2" in s.domains


def test_rename_refused_with_snapshots():
    s = vm_world("shut off")
    s.domains["vm1"]["snaps"] = [("s1", "shutoff", None, "internal")]
    plan = plan_for(s, {"name": "vm2"})
    assert plan["changes"][0]["applied"] == "refused"
    assert "snapshots" in plan["changes"][0]["reason"]


def test_transport_loss_during_a_write_is_uncertain_and_never_replayed():
    s = vm_world("running")
    s.fail_next["setmem"] = (255, "Connection reset by peer\n")
    with pytest.raises(kh.KvmError) as e:
        ke.edit_vm(s, "vm1", {"memory": {"current_mib": 768}, "title": "T",
                              "cpu": {"vcpus": 2}}, options=OPTS, sleep=lambda x: None)
    assert [w[0] for w in s.writes].count("setmem") == 1
    res = {c["field"]: c for c in e.value.changes}
    assert res["cpu.vcpus"]["applied"] == "now"                 # déjà fait, gardé
    assert res["memory.current_mib"]["applied"] == "uncertain"
    assert res["memory.current_mib"]["reason"] == ke.REASON_UNCERTAIN
    assert res["title"]["applied"] == "not-done"
    assert not any(w[0] == "desc" for w in s.writes)


def test_unmanaged_settings_kept_byte_for_byte():
    s = Sim2()
    xml = fx("dumpxml-netperf.xml")
    s.add_domain("netperf", xml)
    ke.edit_vm(s, "netperf", {"title": "edited"}, options=OPTS)
    new = ET.fromstring([c for c in s.calls if c[0] == "input"][-1][2])
    old = ET.fromstring(xml)
    for tag in ("cputune", "seclabel", "numatune"):
        if old.find(tag) is not None:
            assert kx.canonical(new.find(tag)) == kx.canonical(old.find(tag)), tag
    assert [kx.canonical(i) for i in new.findall("devices/interface")] == \
        [kx.canonical(i) for i in old.findall("devices/interface")]


def test_form_has_no_password_and_ignores_host_model_expansion():
    s = vm_world("running", spec=base_spec(display={"vnc": True, "password": True}))
    xml = kx.build_domain(base_spec(display={"vnc": True, "password": True}), OPTS,
                          vnc_password=W.FAKE_PASSWORD)
    xml = xml.replace("<name>vm1</name>", "<name>vm1</name><uuid>u-vm1</uuid>")
    live = xml.replace('<cpu mode="host-model" check="partial">',
                       '<cpu mode="custom" match="exact" check="full">'
                       '<model fallback="forbid">Broadwell-IBRS</model>')
    s.domains["vm1"].update(xml=xml, live=live)
    form = ke.vm_form(s, "vm1")
    assert form["pending"] == [] and form["view"]["display"]["password"] is True
    assert W.FAKE_PASSWORD not in json.dumps(form)


def test_edit_password_generated_and_scrubbed():
    s = vm_world("shut off")
    res = ke.apply_edit(s, "vm1", plan_for(s, {"display": {"password": True}}),
                        password_gen=lambda: W.FAKE_PASSWORD)
    assert "passwd=\"" + W.FAKE_PASSWORD in s.domains["vm1"]["xml"]
    assert W.FAKE_PASSWORD not in json.dumps(res)


# --- seed cloud-init -----------------------------------------------------------------

UD = "#cloud-config\nhostname: x\n"


def seeded_world(state="shut off"):
    s = vm_world()
    del s.domains["vm1"]
    kc.create_vm(s, {"name": "ci1", "disks": [{"source": {"kind": "blank", "pool": "images",
                                                          "size_bytes": 1 << 20}}],
                     "nics": [{"kind": "network", "source": "default"}],
                     "cloudinit": {"user_data": UD, "seed_pool": "images"}},
                 options=OPTS, uuid_gen=lambda: "u-ci1", start=state == "running")
    s.writes.clear()
    return s


def test_regenerate_seed_new_volume_then_old_removed():
    s = seeded_world("running")
    res = ke.regenerate_seed(s, "ci1", UD + "# v2\n", None, rerun=False)
    assert res["seed"]["volume"] == "ci1-cidata-1.iso" and res["removed"] is True
    verbs = [w[0] for w in s.writes]
    assert verbs == ["vol-create-as", "vol-upload", "update-device", "define", "vol-delete"]
    assert ("update-device", "--domain", "ci1", "--file", "/dev/stdin", "--live",
            "--config") in s.writes
    meta = json.loads(iso9660.read_iso(s.contents[("images", "ci1-cidata-1.iso")])
                      ["files"]["meta-data"])
    assert meta["instance-id"] == "u-ci1"                     # même identité : pas de rejeu
    assert kh.parse_domain_xml(s.domains["ci1"]["xml"])["seed"] == {
        "pool": "images", "volume": "ci1-cidata-1.iso"}


def test_regenerate_seed_rerun_changes_instance_id_and_keeps_a_shared_seed():
    s = seeded_world()
    s.add_domain("other", W.dom("other", [W.disk("sdb", "images/ci1-cidata.iso", type="volume",
                                                device="cdrom", ro=True)]))
    res = ke.regenerate_seed(s, "ci1", UD, None, rerun=True, uuid_gen=lambda: "u-new")
    assert res["removed"] is False and "ci1-cidata.iso" in s.pools["images"]["vols"]
    meta = json.loads(iso9660.read_iso(s.contents[("images", "ci1-cidata-1.iso")])
                      ["files"]["meta-data"])
    assert meta["instance-id"] == "u-new"
    assert ("update-device", "--domain", "ci1", "--file", "/dev/stdin", "--config") in s.writes


@pytest.mark.parametrize("user_data,clear,refused", [
    ("", False, True),          # champ resté vide : le cloud-init n'est pas effacé
    (None, False, True),
    ("  \n", False, True),
    ("", True, False),          # vider est voulu : accepté
    (UD + "# v2\n", False, False),
])
def test_an_empty_user_data_needs_clear_to_replace_a_non_empty_seed(user_data, clear, refused):
    s = seeded_world()
    if refused:
        with pytest.raises(ko.OpError) as e:
            ke.regenerate_seed(s, "ci1", user_data, None, clear=clear)
        assert e.value.kind == "refused" and "clear" in e.value.message
        assert s.writes == []                                   # rien d'écrit
        return
    res = ke.regenerate_seed(s, "ci1", user_data, None, clear=clear)
    files = iso9660.read_iso(s.contents[("images", res["seed"]["volume"])])["files"]
    assert files["user-data"].decode() == (user_data or "")


def test_an_empty_seed_can_stay_empty_without_clear():
    s = seeded_world()
    first = ke.regenerate_seed(s, "ci1", "", None, clear=True)
    s.writes.clear()
    second = ke.regenerate_seed(s, "ci1", "", "version: 2\n")
    assert second["seed"]["volume"] != first["seed"]["volume"]


@pytest.mark.parametrize("ci,applied", [
    ({"user_data": ""}, "refused"),
    ({"user_data": "", "clear": True}, "next-boot"),
    ({"user_data": UD + "# v3\n"}, "next-boot"),
])
def test_the_edit_plan_refuses_an_empty_user_data_without_clear(ci, applied):
    s = seeded_world("running")
    plan = ke.edit_plan(s, "ci1", {"cloudinit": ci}, options=OPTS)
    ch = next(c for c in plan["changes"] if c["field"] == "cloudinit")
    assert ch["applied"] == applied
    assert bool(plan["refusals"]) is (applied == "refused")
    assert "hostname: x" not in json.dumps(plan["changes"])     # jamais le texte du user-data


def test_seed_is_owned_and_deleted_with_the_vm():
    s = seeded_world()
    disks = {d["target"]: d for d in ko.owned_disks(s, "ci1")}
    seed = next(d for d in disks.values() if d["volume"] == "ci1-cidata.iso")
    assert seed["deletable"] and seed["reason"] == ko.REASON_SEED
    plan = ko.delete_plan(s, "ci1", remove_storage=True)
    args = ko.undefine_argv("ci1", plan)
    assert args[args.index("--storage") + 1] == "vda,sda"


def test_seed_cited_by_another_vm_is_kept():
    s = seeded_world()
    s.add_domain("other", W.dom("other", [W.disk("sdb", "images/ci1-cidata.iso", type="volume",
                                                device="cdrom", ro=True)]))
    seed = next(d for d in ko.owned_disks(s, "ci1") if d["volume"] == "ci1-cidata.iso")
    assert not seed["deletable"] and seed["reason"] == "shared with other"


def test_clone_regenerates_the_seed_with_the_new_hostname():
    s = seeded_world()
    plan = ko.clone_plan(s, "ci1", "ci1-copy")
    assert plan["seed"] == {"target": "sda", "pool": "images", "volume": "ci1-cidata.iso",
                            "new_volume": "ci1-copy-cidata.iso"}
    res = ko.clone_vm(s, "ci1", "ci1-copy")
    files = iso9660.read_iso(s.contents[("images", "ci1-copy-cidata.iso")])["files"]
    meta = json.loads(files["meta-data"])
    assert meta == {"instance-id": res["uuid"], "local-hostname": "ci1-copy"}
    assert files["user-data"] == UD.encode()
    clone = kh.parse_domain_xml(s.domains["ci1-copy"]["xml"])
    assert clone["seed"] == {"pool": "images", "volume": "ci1-copy-cidata.iso"}
    assert any(d["source"] == "images/ci1-copy-cidata.iso" for d in clone["disks"])
    assert s.contents[("images", "ci1-cidata.iso")]          # la source est intacte


def test_clone_refused_when_the_new_seed_exists():
    s = seeded_world()
    s.add_vol("images", "ci1-copy-cidata.iso")
    plan = ko.clone_plan(s, "ci1", "ci1-copy")
    assert "a volume named ci1-copy-cidata.iso already exists in images" in plan["refusals"]


@pytest.mark.parametrize("text,actual", [("actual 1048576\nswap_in 0\n", 1048576),
                                         ("", None), ("actual x\n", None)])
def test_parse_memstat(text, actual):
    assert ke.parse_memstat(text).get("actual") == actual


# --- retrait d'un appareil présent seulement dans la définition (ajout reporté) ------
# Vu en réel sur node2 (tâche 7) : un disque ajouté « au prochain démarrage »
# faute de port PCIe libre n'existe que dans le XML inactif ; le retirer de la
# VM en marche partait en detach-device --live et échouait (« no disk with
# target vdf »). Le retrait annule l'ajout reporté : définition seule, « now ».

PENDING_CASES = [
    # (libellé, appareil en plus dans la définition, geste)
    ("virtio disk", {"target": "vdb", "source": {"kind": "volume", "pool": "images",
                                                 "volume": "extra.qcow2", "format": "qcow2"}},
     lambda s: ke.detach_disk(s, "vm1", "vdb")),
    ("sata disk", {"target": "sdb", "bus": "sata",
                   "source": {"kind": "volume", "pool": "images", "volume": "extra.qcow2",
                              "format": "qcow2"}},
     lambda s: ke.detach_disk(s, "vm1", "sdb")),
    ("nic", {"nic": {"kind": "network", "source": "default", "mac": "52:54:00:00:00:bb"}},
     lambda s: ke.detach_nic(s, "vm1", "52:54:00:00:00:bb")),
]


@pytest.mark.parametrize("label,extra,gesture", PENDING_CASES, ids=[c[0] for c in PENDING_CASES])
def test_removing_a_device_only_in_the_definition_of_a_running_vm(label, extra, gesture):
    s = vm_world("running")
    s.add_vol("images", "extra.qcow2", fmt="qcow2")
    live = s.domains["vm1"]["live"]
    sp = base_spec()
    if "nic" in extra:
        sp["nics"] = sp["nics"] + [extra["nic"]]
    else:
        sp["disks"] = sp["disks"] + [extra]
    xml = kx.build_domain(sp, OPTS).replace("<name>vm1</name>",
                                            "<name>vm1</name><uuid>u-vm1</uuid>")
    s.domains["vm1"].update(xml=xml, live=live)
    res = gesture(s)
    ch = res["changes"][0]
    assert ch["applied"] == "now", ch
    assert not any("--live" in w for w in s.writes), s.writes
    assert [w[0] for w in s.writes] == ["define"]
    assert ("extra.qcow2" not in s.domains["vm1"]["xml"]
            and "52:54:00:00:00:bb" not in s.domains["vm1"]["xml"])
    assert s.domains["vm1"]["live"] == live
    assert "extra.qcow2" in s.pools["images"]["vols"]          # le volume n'est jamais supprimé
    assert not any(k.startswith("_") for k in ch)


# --- MAC tirée par la console pour une carte ajoutée (revue finale I2) -------------

ADDED_NIC_CASES = [
    # (libellé, état, carte ajoutée, tirages, MAC attendue, refus attendu)
    ("drawn on a running VM", "running", {"kind": "network", "source": "default"},
     [0xAA, 0xBB], "52:54:00:00:00:bb", None),       # 0xaa = la carte existante (MAC)
    ("drawn on a stopped VM", "shut off", {"kind": "network", "source": "default"},
     [0xCC], "52:54:00:00:00:cc", None),
    ("given MAC kept", "shut off", {"kind": "network", "source": "default",
                                    "mac": "52:54:00:00:00:dd"}, [], "52:54:00:00:00:dd", None),
    ("given MAC of another VM refused", "shut off", {"kind": "network", "source": "default",
                                                     "mac": "52:54:00:00:00:EE"}, [], None,
     "already used"),
]


@pytest.mark.parametrize("label,state,nic,draws,want,refusal", ADDED_NIC_CASES,
                         ids=[c[0] for c in ADDED_NIC_CASES])
def test_an_added_card_gets_a_console_drawn_mac(label, state, nic, draws, want, refusal):
    s = vm_world(state)
    s.add_domain("other", kx.build_domain(base_spec(
        name="other", nics=[{"kind": "network", "source": "default",
                             "mac": "52:54:00:00:00:ee"}]), OPTS))
    it = iter(draws)
    plan = ke.edit_plan(s, "vm1", {"nics": [{"mac": MAC}, nic]}, options=OPTS,
                        mac_rand=lambda: next(it))
    if refusal:
        assert any(refusal in r for r in plan["refusals"])
        with pytest.raises(ko.OpError):
            ke.apply_edit(s, "vm1", plan)
        return
    (ch,) = [c for c in plan["changes"] if c["old"] is None and c["field"].startswith("nics[")]
    assert ch["new"]["mac"] == want
    ke.apply_edit(s, "vm1", plan)
    xml = s.domains["vm1"]["xml"]
    assert want in [m.get("address") for m in ET.fromstring(xml).iter("mac")]


def test_the_attach_nic_gesture_sends_the_drawn_mac_to_libvirt():
    s = vm_world("running")
    ke.attach_nic(s, "vm1", {"kind": "network", "source": "default"})
    sent = [c for c in s.calls if c[0] == "input" and "attach-device" in c[1]][-1][2]
    mac = ET.fromstring(sent).find("mac")
    assert mac is not None and mac.get("address").startswith("52:54:00:")
    assert mac.get("address") != MAC
