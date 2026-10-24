"""Prochaine version KVM, tâche 1 : XML des domaines (bin/lib/kvm_xml.py).

- golden files : un spec par cas, produit avec les capacités réelles de node2
  (libvirt 12.8.0), comparé à tests/fixtures/kvm/golden/<cas>.xml ;
  régénération par UPDATE_GOLDEN=1 seulement ;
- schéma : chaque golden file passé à `virt-xml-validate` quand l'outil est
  présent (sauté sinon, et dit) ;
- check_spec : un cas par refus ; valeurs hostiles jamais en XML mal formé ;
- modifications ciblées sur dumpxml-netperf.xml (épinglage, temps réel,
  cartes direct, ordonnanceur gardés à l'identique en C14N).
"""

import copy
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import kvm_caps as kc  # noqa: E402
import kvm_ops as ko  # noqa: E402
import kvm_xml as kx  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "kvm"
GOLDEN = FIX / "golden"
FAKE_VNC = "Fk7pw3Zq"


def fx(name):
    return (FIX / name).read_text()


CAPS = kc.parse_capabilities(fx("capabilities-libvirt-12.8.xml"))
OPTS = kc.form_options(CAPS, {"q35": kc.parse_domcapabilities(fx("domcapabilities-q35.xml")),
                              "pc": kc.parse_domcapabilities(fx("domcapabilities-pc.xml"))})
OPTS_NO_AUTO = kc.form_options(CAPS, {
    "q35": kc.parse_domcapabilities(fx("domcapabilities-q35-no-efi-autoselect.xml")),
    "pc": kc.parse_domcapabilities(fx("domcapabilities-pc.xml"))})


def disk(kind="blank", pool="images", size=10 << 30, **kw):
    src = {"kind": kind, "pool": pool}
    if kind == "blank":
        src["size_bytes"] = size
    src.update(kw.pop("source", {}))
    d = {"source": src}
    d.update(kw)
    return d


BASE = {"name": "kvmtest-1", "cpu": {"vcpus": 2}, "memory": {"current_mib": 2048},
        "disks": [disk(boot_order=1)], "nics": [{"kind": "network", "source": "default"}]}


def spec(**kw):
    s = copy.deepcopy(BASE)
    s.update(kw)
    return s


GOLDEN_CASES = {
    "linux-bios-q35": (spec(), OPTS, None),
    "linux-uefi-secure-tpm": (spec(firmware="efi", secure_boot=True, tpm=True), OPTS, None),
    "windows-uefi": (spec(name="kvmtest-win", os="windows", firmware="efi",
                          cpu={"vcpus": 4, "sockets": 1, "cores": 2, "threads": 2},
                          memory={"current_mib": 4096},
                          disks=[disk(size=60 << 30, boot_order=2),
                                 disk("iso", source={"volume": "win2025.iso"}, boot_order=1)],
                          nics=[{"kind": "bridge", "source": "br0"}]), OPTS, None),
    "pc-i440fx-ide-cdrom": (spec(name="kvmtest-old", machine="pc",
                                 disks=[disk(boot_order=1, bus="ide"),
                                        disk("iso", source={"volume": "dos.iso"}),
                                        disk("empty", pool=None)]), OPTS, None),
    "three-disks-mixed-buses": (spec(disks=[
        disk(boot_order=1, cache="none", io_thread=True, serial="ROOT-1"),
        disk(bus="sata", size=20 << 30, source={"format": "raw"}),
        disk("volume", source={"volume": "shared.qcow2", "format": "qcow2"}, bus="scsi",
             readonly=True, shareable=True)]), OPTS, None),
    "nics-network-bridge-direct": (spec(nics=[
        {"kind": "network", "source": "default", "boot_order": 2},
        {"kind": "bridge", "source": "br0", "model": "e1000e", "mac": "52:54:00:12:34:56"},
        {"kind": "direct", "source": "eno1", "mode": "vepa", "model": "rtl8139"}]), OPTS, None),
    "vnc-password": (spec(display={"password": True, "video": "bochs"}, guest_agent=False,
                          rng=False, tablet=False, serial_console=False,
                          memory={"current_mib": 1024, "balloon": False}), OPTS, FAKE_VNC),
    "hotplug-cpu-mem": (spec(cpu={"vcpus": 2, "max_vcpus": 8, "hotplug": True},
                             memory={"current_mib": 2048, "max_mib": 8192}, hotplug_slots=8,
                             watchdog="reset", on_crash="coredump-restart",
                             title="Hot plug", description="CPU & memory <hot>"), OPTS, None),
    "cloudinit-seed": (spec(disks=[disk("image", source={"image": "leap-16.qcow2",
                                                          "size_bytes": 20 << 30}, boot_order=1)],
                            cloudinit={"user_data": "#cloud-config\n", "seed_pool": "seeds"},
                            sysinfo_serial="SN-0001"), OPTS, None),
    "efi-explicit-fallback": (spec(firmware="efi", secure_boot=True), OPTS_NO_AUTO, None),
    "efi-explicit-plain": (spec(firmware="efi"), OPTS_NO_AUTO, None),
}


@pytest.mark.parametrize("case", sorted(GOLDEN_CASES))
def test_golden(case):
    s, opts, pw = GOLDEN_CASES[case]
    assert kx.check_spec(s, opts) == []
    out = kx.build_domain(s, opts, None, vnc_password=pw)
    path = GOLDEN / f"{case}.xml"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_text(out)
    assert path.exists(), f"golden file missing: {path} (UPDATE_GOLDEN=1 to create)"
    assert out == path.read_text()
    ET.fromstring(out)


@pytest.mark.skipif(not shutil.which("virt-xml-validate"),
                    reason="virt-xml-validate absent (paquet libvirt-client) : schéma non vérifié")
@pytest.mark.parametrize("case", sorted(GOLDEN_CASES))
def test_golden_schema(case):
    r = subprocess.run(["virt-xml-validate", str(GOLDEN / f"{case}.xml"), "domain"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


# --- structure du XML produit --------------------------------------------------

def build(s, opts=OPTS, pw=None):
    return ET.fromstring(kx.build_domain(s, opts, None, vnc_password=pw))


def root_ports(root):
    return [c for c in root.iter("controller") if c.get("model") == "pcie-root-port"]


@pytest.mark.parametrize("slots,nics,disks,want", [
    # ports = appareils PCIe (disques virtio, cartes, USB, ballon, aléa, agent) + emplacements libres
    (4, 1, 1, 1 + 1 + 1 + 1 + 1 + 1 + 4),
    (0, 1, 1, 6),
    (16, 0, 0, 4 + 16),
    (2, 3, 2, 2 + 3 + 4 + 2),
])
def test_pcie_root_ports_count(slots, nics, disks, want):
    s = spec(hotplug_slots=slots, nics=[{"kind": "network", "source": "default"}] * nics,
             disks=[disk() for _ in range(disks)])
    root = build(s)
    ports = root_ports(root)
    assert len(ports) == want
    assert [int(p.get("index")) for p in ports] == list(range(1, want + 1))
    pcie_root = [c for c in root.iter("controller") if c.get("model") == "pcie-root"]
    assert len(pcie_root) == 1 and pcie_root[0].get("index") == "0"


def test_session_defined_fixture_keeps_free_slots():
    # le spec complet défini dans libvirt (qemu:///session) : 4 ports restent libres
    view = kx.managed_view(fx("dumpxml-session-defined-q35.xml"))
    assert view["hotplug_slots"] == 4
    assert view["machine"] == "pc-q35-11.1" and view["firmware"] == "efi"
    assert view["secure_boot"] is True and view["tpm"] is True
    assert view["seed"] == {"pool": "images", "volume": "kvmtest-1-cidata.iso"}
    assert view["os"] == "linux"


def test_pc_has_no_root_ports_and_ide_cdrom():
    root = build(GOLDEN_CASES["pc-i440fx-ide-cdrom"][0])
    assert root_ports(root) == []
    assert root.find("devices/controller[@type='usb']") is None
    cds = [d for d in root.iter("disk") if d.get("device") == "cdrom"]
    assert [d.find("target").get("bus") for d in cds] == ["ide", "ide"]
    assert root.find("devices/watchdog") is None


def test_targets_assigned_by_bus():
    s = spec(disks=[disk(), disk(bus="sata"), disk(), disk(bus="scsi"),
                    disk("empty", pool=None)])
    targets = [(d.find("target").get("dev"), d.find("target").get("bus"))
               for d in build(s).iter("disk")]
    assert targets == [("vda", "virtio"), ("sda", "sata"), ("vdb", "virtio"),
                       ("sdb", "scsi"), ("sdc", "sata")]
    assert len(build(s).findall("devices/controller[@type='scsi']")) == 1


def test_no_scsi_controller_without_scsi_disk():
    assert build(spec()).find("devices/controller[@type='scsi']") is None


def test_firmware_autoselect_never_writes_paths():
    root = build(spec(firmware="efi"))
    assert root.find("os").get("firmware") == "efi"
    assert root.find("os/loader") is None and root.find("os/nvram") is None
    feats = {f.get("name"): f.get("enabled") for f in root.iter("feature")}
    assert feats == {"secure-boot": "no"}
    assert root.find("features/smm") is None


def test_firmware_explicit_paths_from_host():
    root = build(spec(firmware="efi", secure_boot=True), OPTS_NO_AUTO)
    loader = root.find("os/loader")
    assert root.find("os").get("firmware") is None
    assert loader.text == "/usr/share/qemu/ovmf-x86_64-smm-ms-code.qcow2"
    assert loader.get("secure") == "yes" and loader.get("format") == "qcow2"
    assert root.find("os/nvram").get("template") == "/usr/share/qemu/ovmf-x86_64-smm-ms-vars.qcow2"
    assert root.find(f"metadata/{{{kx.HOPS_NS}}}vm/{{{kx.HOPS_NS}}}firmware").text == "explicit"
    assert root.find("features/smm").get("state") == "on"


def test_metadata_namespace_on_element():
    out = kx.build_domain(spec(cloudinit={"user_data": ""}), OPTS, None)
    assert '<hops:vm xmlns:hops="https://harvester-ops.io/xmlns/vm/1">' in out
    assert "<domain type=\"kvm\">" in out          # pas de déclaration sur la racine
    seed = ET.fromstring(out).find(f"metadata/{{{kx.HOPS_NS}}}vm/{{{kx.HOPS_NS}}}seed")
    assert seed.attrib == {"pool": "images", "volume": "kvmtest-1-cidata.iso"}


def test_no_uuid_no_generated_mac_vnc_loopback():
    root = build(spec())
    assert root.find("uuid") is None
    assert root.find("devices/interface/mac") is None
    g = root.find("devices/graphics")
    assert g.get("type") == "vnc" and g.get("passwd") is None
    assert g.find("listen").attrib == {"type": "address", "address": "127.0.0.1"}


def test_vnc_password_only_when_asked():
    assert build(spec(), pw=FAKE_VNC).find("devices/graphics").get("passwd") is None
    root = build(spec(display={"password": True}), pw=FAKE_VNC)
    assert root.find("devices/graphics").get("passwd") == FAKE_VNC


@pytest.mark.parametrize("pw", ["", "123456789", "a b", "é", 12345])
def test_vnc_password_refused(pw):
    with pytest.raises(ko.OpError) as e:
        kx.build_domain(spec(display={"password": True}), OPTS, None, vnc_password=pw)
    assert e.value.kind == "invalid"
    assert str(pw) not in e.value.message or pw == ""


def test_build_refuses_bad_spec_with_reasons():
    with pytest.raises(ko.OpError) as e:
        kx.build_domain(spec(machine="pc", secure_boot=True, firmware="efi"), OPTS, None)
    assert e.value.kind == "invalid"
    assert kc.REASON_SECURE_PC in e.value.data["refusals"]


def test_build_with_explicit_volumes():
    s = spec(disks=[disk(), disk("empty", pool=None)])
    vols = {"disks": [{"pool": "fast", "volume": "renamed.qcow2", "format": "qcow2"}, None],
            "seed": None}
    root = ET.fromstring(kx.build_domain(s, OPTS, vols))
    assert root.find("devices/disk/source").attrib == {"pool": "fast", "volume": "renamed.qcow2"}
    with pytest.raises(ko.OpError):
        kx.build_domain(s, OPTS, {"disks": [None]})


def test_resolve_volumes():
    s = kx.normalize(spec(disks=[disk(), disk("image", source={"image": "base.qcow2"}),
                                 disk("iso", source={"volume": "a.iso"}),
                                 disk("volume", source={"volume": "old.raw"}),
                                 disk("empty", pool=None)],
                          cloudinit={"user_data": "x"}), OPTS)
    r = kx.resolve_volumes(s)
    assert [v and (v["volume"], v["create"]) for v in r["disks"]] == [
        ("kvmtest-1-vda.qcow2", True), ("kvmtest-1-vdb.qcow2", True), ("a.iso", False),
        ("old.raw", False), None]
    assert r["seed"] == {"pool": "images", "volume": "kvmtest-1-cidata.iso", "format": "raw",
                         "create": True}


def test_windows_preset():
    root = build(GOLDEN_CASES["windows-uefi"][0])
    assert root.find("clock").get("offset") == "localtime"
    assert root.find("features/hyperv/relaxed") is not None
    assert root.find("devices/interface/model").get("type") == "e1000e"
    assert root.find("devices/video/model").get("type") == "vga"
    assert {d.find("target").get("bus") for d in root.iter("disk")} == {"sata"}


# --- check_spec : un cas par refus ---------------------------------------------

def _images(**kw):
    o = copy.deepcopy(OPTS)
    o.update(kw)
    return o


REFUSALS = [
    ("topology", spec(cpu={"vcpus": 4, "sockets": 1, "cores": 2, "threads": 1}), OPTS,
     "the topology (sockets x cores x threads = 2) must equal the maximum vCPUs (4)"),
    ("memory-over-max", spec(memory={"current_mib": 4096, "max_mib": 2048}), OPTS,
     "current memory above the maximum"),
    ("secure-boot-pc", spec(machine="pc", firmware="efi", secure_boot=True), OPTS,
     kc.REASON_SECURE_PC),
    ("secure-boot-bios", spec(secure_boot=True), OPTS, "Secure Boot needs UEFI firmware"),
    ("same-target", spec(disks=[disk(target="vda"), disk(target="vda")]), OPTS,
     "two disks use the target vda"),
    ("bad-mac", spec(nics=[{"kind": "network", "source": "default", "mac": "52:54:00:zz:00:01"}]),
     OPTS, "invalid MAC address: '52:54:00:zz:00:01'"),
    ("multicast-mac", spec(nics=[{"kind": "network", "source": "default",
                                  "mac": "01:00:5e:00:00:01"}]),
     OPTS, "invalid MAC address: '01:00:5e:00:00:01'"),
    ("dup-mac", spec(nics=[{"kind": "network", "source": "default", "mac": "52:54:00:00:00:01"},
                           {"kind": "bridge", "source": "br0", "mac": "52:54:00:00:00:01"}]),
     OPTS, "MAC address used twice: 52:54:00:00:00:01"),
    ("mac-on-host", spec(nics=[{"kind": "network", "source": "default",
                                "mac": "52:54:00:E7:30:86"}]),
     _images(macs_in_use=["52:54:00:e7:30:86"]),
     "MAC address already used on this host: 52:54:00:e7:30:86"),
    ("dup-boot", spec(disks=[disk(boot_order=1)],
                      nics=[{"kind": "network", "source": "default", "boot_order": 1}]),
     OPTS, "two devices use boot order 1"),
    ("below-image", spec(disks=[disk("image", source={"image": "big.qcow2",
                                                       "size_bytes": 1 << 30})]),
     _images(images={"images/big.qcow2": {"capacity": 10 << 30, "format": "qcow2"}}),
     "disk vda: size below the image virtual size (10737418240 bytes)"),
    ("name-slash", spec(name="a/b"), OPTS,
     "name: invalid name for the copy: no '/' and no leading '.'"),
    ("name-dot", spec(name=".hidden"), OPTS,
     "name: invalid name for the copy: no '/' and no leading '.'"),
    ("name-control", spec(name="a\x01b"), OPTS, "name: invalid domain name"),
    ("ide-on-q35", spec(disks=[disk(bus="ide")]), OPTS, "disk hda: the q35 machine type has no IDE bus"),
    ("cdrom-virtio", spec(disks=[disk("iso", source={"volume": "a.iso"}, bus="virtio")]), OPTS,
     "disk vda: a CD-ROM cannot use the virtio bus"),
    ("readonly-sata", spec(disks=[disk(bus="sata", readonly=True)]), OPTS,
     "disk sda: a read-only disk needs the virtio or scsi bus"),
    ("blank-no-size", spec(disks=[disk(size=None)]), OPTS, "disk vda: a blank disk needs a size in bytes"),
    ("target-bus", spec(disks=[disk(target="sda")]), OPTS, "disk sda: target does not match bus virtio"),
    ("bad-target", spec(disks=[disk(target="xvda")]), OPTS, "invalid disk target: 'xvda'"),
    ("iothread-sata", spec(disks=[disk(bus="sata", io_thread=True)]), OPTS,
     "disk sda: a dedicated I/O thread needs the virtio bus"),
    ("vol-slash", spec(disks=[disk("volume", source={"volume": "../etc/passwd"})]), OPTS,
     "invalid disk vda volume: '../etc/passwd'"),
    ("pool-dash", spec(disks=[disk(pool="-x")]), OPTS, "invalid disk vda pool: '-x'"),
    ("cpu-model", spec(cpu={"vcpus": 1, "mode": "custom", "model": "Icelake-Server"}), OPTS,
     "CPU model not usable on this host: 'Icelake-Server'"),
    ("too-many-vcpus", spec(machine="pc", cpu={"vcpus": 256}), OPTS,
     "more vCPUs than the machine type allows (255)"),
    ("vcpus-over-max", spec(cpu={"vcpus": 4, "max_vcpus": 2}), OPTS, "more vCPUs than the maximum"),
    ("tpm-missing", spec(tpm=True),
     kc.form_options(CAPS, {"q35": kc.parse_domcapabilities(fx("domcapabilities-q35-no-tpm.xml"))}),
     kc.REASON_NO_TPM),
    ("bridge-name", spec(nics=[{"kind": "bridge", "source": "br0; rm -rf /"}]), OPTS,
     "network card 1: invalid host interface 'br0; rm -rf /'"),
    ("direct-mode", spec(nics=[{"kind": "direct", "source": "eno1", "mode": "nat"}]), OPTS,
     "network card 1: unknown macvtap mode 'nat'"),
    ("nic-model", spec(nics=[{"kind": "network", "source": "default", "model": "vmxnet3"}]), OPTS,
     "network card 1: unknown model 'vmxnet3'"),
    ("slots", spec(hotplug_slots=17), OPTS, "hot-plug slots: 0 to 16"),
    ("title-newline", spec(title="a\nb"), OPTS, "invalid title: one line, 256 characters at most"),
    ("description-nul", spec(description="a\x00b"), OPTS, "invalid description (1000 characters at most)"),
    ("machine", spec(machine="microvm"), OPTS, "machine type not offered by this host: 'microvm'"),
    ("video", spec(display={"video": "qxl"}), OPTS, "video model not offered by this host: 'qxl'"),
    ("watchdog", spec(watchdog="explode"), OPTS, "unknown watchdog action: 'explode'"),
    ("lifecycle", spec(on_reboot="coredump-restart"), OPTS, "unknown on_reboot action: 'coredump-restart'"),
    ("serial", spec(disks=[disk(serial="a b")]), OPTS,
     "disk vda: invalid serial number (20 letters, digits, _ . + - at most)"),
    ("linked-other-pool", spec(disks=[disk("image", source={"image": "b.qcow2",
                                                             "image_pool": "other"})]),
     OPTS, "disk vda: a linked disk must be in the image pool"),
    ("seed-no-pool", spec(disks=[disk("empty", pool=None)], cloudinit={"user_data": ""}), OPTS,
     "cloud-init needs a pool for its seed volume"),
    ("not-object", "x", OPTS, "the spec must be an object"),
    ("disks-not-list", spec(disks="vda"), OPTS, "disks must be a list of objects"),
    ("cpu-not-object", spec(cpu=4), OPTS, "cpu must be an object"),
]


@pytest.mark.parametrize("case,s,opts,reason", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_check_spec_refusals(case, s, opts, reason):
    reasons = kx.check_spec(s, opts)
    assert reason in reasons, reasons


def test_check_spec_valid_base():
    assert kx.check_spec(spec(), OPTS) == []


HOSTILE = [
    "kvm<test>", 'kvm"x', "kvm&amp;", "kvm]]>x", "kvm'x", "-x", "kvm x", "kvm x",
    "kvm\ud800x", "kvm\x7fx", "kvm\tx", "é" * 255, "kvm￿x",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_values_never_malformed(value):
    cases = [spec(name=value), spec(title=value), spec(description=value),
             spec(disks=[disk(pool=value)]), spec(nics=[{"kind": "network", "source": value}]),
             spec(disks=[disk("volume", source={"volume": value})]), spec(sysinfo_serial=value)]
    for s in cases:
        try:
            out = kx.build_domain(s, OPTS, None)
        except ko.OpError as exc:
            assert exc.kind == "invalid"
            continue
        root = ET.fromstring(out)          # jamais mal formé
        # et la valeur ressort à l'identique
        texts = [el.text for el in root.iter()] + [v for el in root.iter() for v in el.attrib.values()]
        assert value in texts


# --- device_xml ------------------------------------------------------------

def test_device_xml_disk_and_interface():
    d = ET.fromstring(kx.device_xml("disk", {"target": "vdb", "bus": "virtio", "pool": "images",
                                             "volume": "x.qcow2", "format": "qcow2",
                                             "cache": "none"}))
    assert d.tag == "disk" and d.get("type") == "volume"
    assert d.find("source").attrib == {"pool": "images", "volume": "x.qcow2"}
    assert d.find("driver").attrib == {"name": "qemu", "type": "qcow2", "cache": "none",
                                       "discard": "unmap"}
    cd = ET.fromstring(kx.device_xml("disk", {"target": "sdc", "bus": "sata", "device": "cdrom"}))
    assert cd.find("source") is None and cd.find("readonly") is not None
    i = ET.fromstring(kx.device_xml("interface", {"kind": "direct", "source": "eno1",
                                                  "mode": "private", "mac": "52:54:00:AA:00:01"}))
    assert i.find("mac").get("address") == "52:54:00:aa:00:01"
    assert i.find("source").attrib == {"dev": "eno1", "mode": "private"}


@pytest.mark.parametrize("kind,item", [
    ("disk", {"target": "vdb", "bus": "floppy"}),
    ("disk", {"target": "../x", "bus": "virtio"}),
    ("disk", {"target": "vdb", "bus": "virtio", "pool": "p", "volume": "a/b"}),
    ("interface", {"kind": "bridge", "source": "br0 x"}),
    ("interface", {"kind": "network", "source": "default", "mac": "zz"}),
    ("hostdev", {}),
])
def test_device_xml_errors(kind, item):
    with pytest.raises(ko.OpError) as e:
        kx.device_xml(kind, item)
    assert e.value.kind == "invalid"


# --- managed_view, diff, changes -------------------------------------------------

def test_managed_view_netperf():
    v = kx.managed_view(fx("dumpxml-netperf.xml"))
    assert v["cpu"] == {"mode": "host-passthrough", "model": None, "vcpus": 2, "max_vcpus": 2,
                        "sockets": 1, "cores": 2, "threads": 1, "hotplug": False}
    assert v["memory"] == {"current_mib": 2048, "max_mib": 2048, "balloon": False}
    assert [n["kind"] for n in v["nics"]] == ["direct", "direct"]
    assert v["nics"][0] == {"kind": "direct", "source": "netperf.educ", "mode": "bridge",
                            "model": "virtio", "mac": "52:54:00:e7:30:86", "boot_order": None}
    assert v["disks"][0]["io_thread"] is True and v["disks"][0]["cache"] == "writeback"
    # 5 ports : bus 1 et 2 (cartes), 3 (USB), 4 (disque) occupés, le 5e libre
    assert v["hotplug_slots"] == 1
    assert {"cputune", "memoryBacking", "pm"} <= set(v["unmanaged"])
    assert v["display"] == {"vnc": False, "password": False, "video": None}
    assert v["watchdog"] == "reset" and v["firmware"] == "bios"


def test_managed_view_password_is_only_a_flag():
    v = kx.managed_view(fx("dumpxml-vnc-password.xml"))
    assert v["display"]["password"] is True
    assert FAKE_VNC not in repr(v)
    assert "metadata:http://libosinfo.org/xmlns/libvirt/domain/1.0" in v["unmanaged"]


def test_managed_view_uefi_inactive_autoselect():
    v = kx.managed_view(fx("dumpxml-kvmtest-uefi-inactive.xml"))
    assert v["firmware"] == "efi" and v["firmware_auto"] is True and v["secure_boot"] is False
    assert v["cpu"]["mode"] == "custom" and v["cpu"]["model"] == "qemu64"


def test_diff_pending():
    inactive = kx.managed_view(fx("dumpxml-netperf.xml"))
    live = copy.deepcopy(inactive)
    assert kx.diff(inactive, live) == []
    inactive["cpu"]["max_vcpus"] = 4
    inactive["display"]["password"] = True          # vu seulement avec --security-info
    inactive["disks"][0]["cache"] = "none"
    assert kx.diff(inactive, live) == ["cpu.max_vcpus", "disks[vda].cache"]


def test_changes():
    view = kx.managed_view(fx("dumpxml-netperf.xml"))
    ch = kx.changes(view, {"cpu": {"vcpus": 2, "max_vcpus": 4}, "title": "T",
                           "disks": [{"target": "vda", "cache": "none"},
                                     {"source": {"kind": "blank", "pool": "p", "size_bytes": 1}}],
                           "nics": [{"mac": "52:54:00:E7:30:86", "model": "e1000e"},
                                    {"kind": "network", "source": "default"}]})
    fields = [(c["field"], c["old"] if not isinstance(c["old"], dict) else "dict",
               c["new"] if not isinstance(c["new"], dict) else "dict") for c in ch]
    assert fields == [
        ("cpu.max_vcpus", 2, 4), ("title", None, "T"),
        ("disks[vda].cache", "writeback", "none"), ("disks[+2]", None, "dict"),
        ("nics[52:54:00:e7:30:86].model", "virtio", "e1000e"), ("nics[+2]", None, "dict"),
        ("nics[52:54:00:f7:2d:74]", "dict", None)]
    assert kx.changes(view, {}) == []


def test_changes_cdrom_media():
    view = kx.managed_view(fx("dumpxml-session-defined-q35.xml"))
    ch = kx.changes(view, {"disks": [dict(d, source={"kind": "iso", "pool": "iso",
                                                       "volume": "new.iso"})
                                     if d["target"] == "sdb" else d for d in view["disks"]]})
    assert ch == [{"field": "disks[sdb].source", "old": None,
                   "new": {"pool": "iso", "volume": "new.iso"}}]


# --- empreinte -------------------------------------------------------------

def test_canonical_digest_ignores_password_and_formatting():
    text = fx("dumpxml-vnc-password.xml")
    d1 = kx.canonical_digest(text)
    assert d1 == kx.canonical_digest(text.replace(FAKE_VNC, "Other123"))
    assert d1 == kx.canonical_digest(text.replace("\n  ", "\n      "))
    assert d1 != kx.canonical_digest(text.replace("<memory unit='KiB'>", "<memory unit='KiB'>1"))
    assert len(d1) == 64 and FAKE_VNC not in d1


def test_canonical_digest_unreadable():
    with pytest.raises(ko.OpError):
        kx.canonical_digest("<domain>")


# --- modifications ciblées ------------------------------------------------------

NETPERF = fx("dumpxml-netperf.xml")
KEEP = ["cputune", "memoryBacking", "iothreads", "defaultiothread", "pm", "clock",
        "devices/interface", "devices/watchdog", "cpu/feature", "features/pmu"]


def _c14n(root, path):
    return [kx.canonical(el) for el in root.findall(path)]


def _kept(edit_fn, text=NETPERF, keep=KEEP):
    before = ET.fromstring(text)
    e = kx.Edit(text)
    edit_fn(e)
    after = ET.fromstring(e.xml())
    for path in keep:
        # un ajout se place après les éléments existants, qui restent identiques
        kept = _c14n(before, path)
        assert _c14n(after, path)[:len(kept)] == kept, path
    return e, after


EDITS = [
    ("vcpus-current", lambda e: e.set_vcpus(1, 2)),
    ("vcpus-topology", lambda e: e.set_vcpus(4, 4, {"sockets": 1, "cores": 4, "threads": 1})),
    ("memory", lambda e: e.set_memory(1024, 4096)),
    ("cpu-model", lambda e: e.set_cpu("custom", "Broadwell-IBRS")),
    ("machine", lambda e: e.set_machine("pc-q35-11.1")),
    ("firmware-efi", lambda e: e.set_firmware("efi")),
    ("tpm", lambda e: e.set_tpm(True)),
    ("add-disk", lambda e: e.add_disk({"bus": "virtio", "pool": "p", "volume": "v.qcow2",
                                       "format": "qcow2"})),
    ("disk-cache", lambda e: e.set_disk("vda", cache="none")),
    ("add-nic", lambda e: e.add_nic({"kind": "network", "source": "default"})),
    ("boot", lambda e: e.set_boot_order({"vda": 1})),
    ("display", lambda e: e.set_display(True, FAKE_VNC)),
    ("video", lambda e: e.set_video("virtio")),
    ("title", lambda e: e.set_title("T & <t>")),
    ("description", lambda e: e.set_description("d\nd")),
    ("lifecycle", lambda e: e.set_lifecycle(on_crash="restart")),
    ("os", lambda e: e.set_metadata_os("linux")),
    ("seed", lambda e: e.set_seed("images", "netperf-cidata.iso")),
]


@pytest.mark.parametrize("case,fn", EDITS, ids=[c[0] for c in EDITS])
def test_edit_keeps_unmanaged(case, fn):
    e, after = _kept(fn)
    assert e.modified
    # le XML reste lisible et toujours aussi complet
    assert after.find("cputune/vcpusched") is not None
    assert len(after.findall("devices/controller")) >= 8


def test_edit_set_vcpus_keeps_cputune_exactly():
    before = ET.fromstring(NETPERF)
    e = kx.Edit(NETPERF)
    e.set_vcpus(2, 4, {"sockets": 1, "cores": 4, "threads": 1})
    after = ET.fromstring(e.xml())
    assert kx.canonical(before.find("cputune")) == kx.canonical(after.find("cputune"))
    assert after.find("vcpu").text == "4" and after.find("vcpu").get("current") == "2"
    assert after.find("cpu/topology").get("cores") == "4"


def test_edit_empty_is_identical():
    e = kx.Edit(NETPERF)
    assert e.modified is False
    assert kx.canonical(e.xml()) == kx.canonical(NETPERF)
    e.set_vcpus(2, 2)
    e.set_memory(2048, 2048)
    e.set_cpu("host-passthrough")
    assert e.modified is False


def test_edit_boot_dev_removed_only_on_per_device_order():
    e = kx.Edit(NETPERF)
    e.set_title("x")
    assert ET.fromstring(e.xml()).find("os/boot") is not None
    e.set_boot_order({"vda": 1, "52:54:00:e7:30:86": 2})
    root = ET.fromstring(e.xml())
    assert root.find("os/boot") is None
    assert root.find("devices/disk/boot").get("order") == "1"
    with pytest.raises(kx.EditError):
        e.set_boot_order({"52:54:00:f7:2d:74": 1})


def test_edit_keeps_libosinfo_metadata_and_password_out_of_digest():
    text = fx("dumpxml-vnc-password.xml")
    e = kx.Edit(text)
    e.set_metadata_os("linux")
    e.set_display(True, "Newpass1")
    out = e.xml()
    root = ET.fromstring(out)
    lib = root.find("metadata/{http://libosinfo.org/xmlns/libvirt/domain/1.0}libosinfo")
    assert lib is not None
    assert kx.canonical(lib) == kx.canonical(ET.fromstring(text).find(
        "metadata/{http://libosinfo.org/xmlns/libvirt/domain/1.0}libosinfo"))
    assert root.find(f"metadata/{{{kx.HOPS_NS}}}vm/{{{kx.HOPS_NS}}}os").text == "linux"
    e2 = kx.Edit(text)
    e2.set_metadata_os("linux")
    assert kx.canonical_digest(out) == kx.canonical_digest(e2.xml())
    e.set_display(True, False)
    assert ET.fromstring(e.xml()).find("devices/graphics").get("passwd") is None


def test_edit_firmware_cases():
    # dépliée par libvirt après une sélection automatique : retirée puis reposée
    auto = kx.Edit(fx("dumpxml-kvmtest-uefi-inactive.xml"))
    auto.set_firmware("efi", secure_boot=True)
    root = ET.fromstring(auto.xml())
    assert root.find("os/loader") is None and root.find("os/nvram") is None
    assert root.find("os").get("firmware") == "efi"
    assert root.find("features/smm").get("state") == "on"
    auto.set_firmware("bios")
    root = ET.fromstring(auto.xml())
    assert root.find("os").get("firmware") is None and root.find("os/firmware") is None
    # posée à la main : refus nommé
    hand = ET.fromstring(fx("dumpxml-kvmtest-uefi-inactive.xml"))
    del hand.find("os").attrib["firmware"]
    e = kx.Edit(ET.tostring(hand, encoding="unicode"))
    with pytest.raises(kx.EditError) as err:
        e.set_firmware("bios")
    assert err.value.message == kx.REASON_FIRMWARE_HAND
    # posée par la console en repli : modifiable
    built = kx.build_domain(spec(firmware="efi"), OPTS_NO_AUTO, None)
    e = kx.Edit(built)
    e.set_firmware("efi", secure_boot=True, efi=OPTS_NO_AUTO["by_machine"]["q35"]["efi"])
    assert ET.fromstring(e.xml()).find("os/loader").get("secure") == "yes"
    e.set_firmware("bios")
    root = ET.fromstring(e.xml())
    assert root.find("os/loader") is None
    assert root.find(f"metadata/{{{kx.HOPS_NS}}}vm/{{{kx.HOPS_NS}}}firmware") is None


@pytest.mark.parametrize("fn,kind", [
    (lambda e: e.set_vcpus(1, 1), "refused"),                 # vcpupin vise le vCPU 1
    (lambda e: e.set_vcpus(3, 2), "invalid"),
    (lambda e: e.set_vcpus(4, 4, {"sockets": 1, "cores": 2, "threads": 1}), "invalid"),
    (lambda e: e.set_memory(4096, 1024), "invalid"),
    (lambda e: e.set_cpu("custom"), "invalid"),
    (lambda e: e.set_machine("pc-i440fx-11.1"), "refused"),
    (lambda e: e.set_machine("virt"), "invalid"),
    (lambda e: e.set_firmware("bios", True), "invalid"),
    (lambda e: e.remove_disk("vdz"), "notfound"),
    (lambda e: e.remove_nic("52:54:00:00:00:00"), "notfound"),
    (lambda e: e.add_disk({"bus": "virtio", "target": "vda", "pool": "p", "volume": "v"}), "invalid"),
    (lambda e: e.add_disk({"bus": "ide", "pool": "p", "volume": "v"}), "invalid"),
    (lambda e: e.add_nic({"kind": "direct", "source": "eno1", "mac": "52:54:00:f7:2d:74"}), "invalid"),
    (lambda e: e.set_disk("vda", cache="fast"), "invalid"),
    (lambda e: e.set_display(True, "toolongpassword"), "invalid"),
    (lambda e: e.set_title("a\nb"), "invalid"),
    (lambda e: e.set_lifecycle(on_poweroff="coredump-destroy"), "invalid"),
    (lambda e: e.set_metadata_os("beos"), "invalid"),
    (lambda e: e.set_seed("p", "a/b"), "invalid"),
    (lambda e: e.set_video("qxl"), "invalid"),
])
def test_edit_errors(fn, kind):
    e = kx.Edit(NETPERF)
    with pytest.raises(kx.EditError) as err:
        fn(e)
    assert err.value.kind == kind


def test_edit_tpm_by_hand_refused():
    text = NETPERF.replace("<memballoon model='none'/>",
                           "<tpm model='tpm-tis'><backend type='passthrough'/></tpm>"
                           "<memballoon model='none'/>")
    with pytest.raises(kx.EditError) as err:
        kx.Edit(text).set_tpm(False)
    assert err.value.message == kx.REASON_TPM_HAND


def test_edit_disk_bus_change_renames_target_and_drops_address():
    e = kx.Edit(NETPERF)
    new = e.set_disk("vda", bus="scsi")
    root = ET.fromstring(e.xml())
    d = root.find("devices/disk")
    assert new == "sda" and d.find("target").attrib == {"dev": "sda", "bus": "scsi"}
    assert d.find("address") is None and d.find("driver").get("iothread") is None
    assert root.find("devices/controller[@type='scsi']").get("model") == "virtio-scsi"


def test_edit_add_remove_round_trip():
    e = kx.Edit(NETPERF)
    t = e.add_disk({"bus": "virtio", "pool": "p", "volume": "x.qcow2", "format": "qcow2"})
    assert t == "vdb"
    e.add_nic({"kind": "bridge", "source": "br0", "mac": "52:54:00:00:00:09"})
    e.remove_disk("vdb")
    e.remove_nic("52:54:00:00:00:09")
    assert e.modified is False


def test_edit_set_cpu_custom_drops_migratable():
    # refus de libvirt vu en réel (define --validate) : migratable seulement en host-passthrough
    e = kx.Edit(NETPERF)
    e.set_cpu("custom", "Broadwell-IBRS")
    cpu = ET.fromstring(e.xml()).find("cpu")
    assert cpu.get("migratable") is None and cpu.get("mode") == "custom"
    assert cpu.find("model").text == "Broadwell-IBRS"
    assert len(cpu.findall("feature")) == 3 and cpu.find("topology") is not None
