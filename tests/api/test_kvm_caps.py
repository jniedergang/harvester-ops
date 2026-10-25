"""Prochaine version KVM, tâche 1 : capacités d'un hôte (bin/lib/kvm_caps.py).

Jeux d'essai relevés sur node2 (libvirt 12.8.0, QEMU 11.1.2) :
capabilities-libvirt-12.8.xml, domcapabilities-q35.xml, domcapabilities-pc.xml ;
dérivés écrits à la main et nommés comme tels : domcapabilities-q35-no-efi-autoselect.xml,
domcapabilities-q35-no-tpm.xml. Les autres variantes sont fabriquées ici par
remplacement de texte sur le relevé réel.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import kvm_caps as kc  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "kvm"


def fx(name):
    return (FIX / name).read_text()


CAPS = kc.parse_capabilities(fx("capabilities-libvirt-12.8.xml"))
Q35 = fx("domcapabilities-q35.xml")
PC = fx("domcapabilities-pc.xml")


def dc(text):
    return kc.parse_domcapabilities(text)


# --- capabilities ----------------------------------------------------------

def test_parse_capabilities_real_node2():
    assert CAPS["arch"] == "x86_64"
    assert CAPS["domain_types"] == ["qemu", "kvm"]
    names = {m["name"]: m for m in CAPS["machines"]}
    assert names["q35"]["canonical"] == "pc-q35-11.1"
    assert names["pc"]["canonical"] == "pc-i440fx-11.1"
    assert names["pc-q35-11.1"]["max_cpus"] == 4096
    assert names["pc-q35-5.2"]["deprecated"] is True


def test_parse_capabilities_older_fixture_and_arch_choice():
    old = kc.parse_capabilities(fx("capabilities.xml"))
    assert {m["name"]: m["canonical"] for m in old["machines"]}["q35"] == "pc-q35-10.2"
    i686 = kc.parse_capabilities(fx("capabilities.xml"), arch="i686")
    assert i686["arch"] == "i686" and i686["machines"]
    none = kc.parse_capabilities(fx("capabilities.xml"), arch="aarch64")
    assert none == {"arch": "aarch64", "machines": [], "domain_types": []}


@pytest.mark.parametrize("text", [
    "<capabilities>",                                  # tronqué
    "<!DOCTYPE x [<!ENTITY a 'b'>]><capabilities/>",   # DOCTYPE refusé
    "<domainCapabilities/>",                           # mauvais document
    "",
])
def test_parse_capabilities_errors(text):
    with pytest.raises(ValueError):
        kc.parse_capabilities(text)


# --- domcapabilities ---------------------------------------------------------

def test_parse_domcapabilities_q35_real():
    d = dc(Q35)
    assert d["machine"] == "pc-q35-11.1" and d["arch"] == "x86_64" and d["domain"] == "kvm"
    assert d["firmware_autoselect"] is True
    assert d["secure_boot"] is True
    assert "/usr/share/qemu/ovmf-x86_64-4m-code.qcow2" in d["loaders"]
    assert d["cpu_modes"]["host_passthrough"] is True
    assert d["cpu_modes"]["host_model"] == "Broadwell-IBRS"
    assert "qemu64" in d["cpu_modes"]["custom"]
    # un modèle annoncé usable='no' n'est jamais proposé
    assert "Icelake-Server" not in d["cpu_modes"]["custom"]
    assert {"virtio", "scsi", "sata"} <= set(d["disk_buses"])
    assert {"virtio", "vga", "bochs"} <= set(d["video_models"])
    assert "vnc" in d["graphics_types"]
    assert d["tpm"]["backends"] == ["passthrough", "emulator", "external"]
    assert d["max_vcpus"] == 4096 and d["iothreads"] is True


def test_parse_domcapabilities_pc_real_has_no_secure_boot():
    d = dc(PC)
    assert d["machine"] == "pc-i440fx-11.1"
    assert d["secure_boot"] is False
    assert d["max_vcpus"] == 255
    assert "ide" in d["disk_buses"]


@pytest.mark.parametrize("text", ["<domainCapabilities", "<capabilities/>",
                                  "<!DOCTYPE d><domainCapabilities/>"])
def test_parse_domcapabilities_errors(text):
    with pytest.raises(ValueError):
        dc(text)


def test_parse_domcapabilities_minimal_document():
    d = dc("<domainCapabilities><machine>pc-q35-9.0</machine></domainCapabilities>")
    assert d["firmware_autoselect"] is False and d["loaders"] == []
    assert d["cpu_modes"] == {"host_passthrough": False, "host_model": None, "custom": []}
    assert d["tpm"] is None and d["max_vcpus"] is None and d["disk_buses"] == []


# --- form_options : un cas par option grisée ----------------------------------

def _no_secure(text):
    return text.replace("""      <enum name='secure'>
        <value>yes</value>
        <value>no</value>
      </enum>""", """      <enum name='secure'>
        <value>no</value>
      </enum>""")


def _no_scsi(text):
    return text.replace("        <value>scsi</value>\n", "", 1)


def _tpm_unsupported(text):
    start = text.index("<tpm supported='yes'>")
    end = text.index("</tpm>", start) + len("</tpm>")
    return text[:start] + "<tpm supported='no'/>" + text[end:]


FORM_CASES = [
    # (cas, domcaps q35, domcaps pc, attendu q35, attendu pc)
    ("real", Q35, PC,
     {"firmware": ["bios", "efi"], "secure_boot": True, "tpm": True, "efi.autoselect": True},
     {"firmware": ["bios", "efi"], "secure_boot": False, "disabled.secure_boot":
      kc.REASON_SECURE_PC}),
    ("no-efi-autoselect", fx("domcapabilities-q35-no-efi-autoselect.xml"), PC,
     {"efi.autoselect": False,
      "efi.loader": "/usr/share/qemu/ovmf-x86_64-4m-code.qcow2",
      "efi.nvram_template": "/usr/share/qemu/ovmf-x86_64-4m-vars.qcow2",
      "efi.secure_loader": "/usr/share/qemu/ovmf-x86_64-smm-ms-code.qcow2",
      "efi.secure_nvram_template": "/usr/share/qemu/ovmf-x86_64-smm-ms-vars.qcow2",
      "secure_boot": True}, {}),
    ("no-tpm", fx("domcapabilities-q35-no-tpm.xml"), PC,
     {"tpm": False, "disabled.tpm": kc.REASON_NO_TPM}, {}),
    ("tpm-unsupported", _tpm_unsupported(Q35), PC,
     {"tpm": False, "tpm_model": None, "disabled.tpm": kc.REASON_NO_TPM}, {}),
    ("no-secure-boot", _no_secure(Q35), PC,
     {"secure_boot": False, "disabled.secure_boot": kc.REASON_NO_SECURE}, {}),
    ("no-virtio-scsi", _no_scsi(Q35), PC,
     {"disk_buses": ["virtio", "sata"], "disabled.scsi": kc.REASON_NO_SCSI}, {}),
    ("pc-no-secure-boot", Q35, _no_secure(PC),
     {}, {"secure_boot": False, "disabled.secure_boot": kc.REASON_SECURE_PC,
          "cdrom_bus": "ide", "disk_buses": ["virtio", "scsi", "sata", "ide"]}),
]


def _get(opts, family, key):
    if key.startswith("disabled."):
        return opts["disabled"].get(family, {}).get(key.split(".", 1)[1])
    if key.startswith("efi."):
        return (opts["by_machine"][family]["efi"] or {}).get(key.split(".", 1)[1])
    return opts["by_machine"][family][key]


@pytest.mark.parametrize("case,q35,pc,want_q35,want_pc", FORM_CASES, ids=[c[0] for c in FORM_CASES])
def test_form_options_table(case, q35, pc, want_q35, want_pc):
    opts = kc.form_options(CAPS, {"q35": dc(q35), "pc": dc(pc)})
    assert opts["machines"] == ["q35", "pc"] and opts["default_machine"] == "q35"
    for key, value in want_q35.items():
        assert _get(opts, "q35", key) == value, key
    for key, value in want_pc.items():
        assert _get(opts, "pc", key) == value, key


def test_form_options_real_values():
    opts = kc.form_options(CAPS, {"q35": dc(Q35), "pc": dc(PC)})
    q = opts["by_machine"]["q35"]
    assert q["canonical"] == "pc-q35-11.1" and q["max_vcpus"] == 4096
    assert q["cpu_modes"] == ["host-model", "host-passthrough", "custom"]
    assert q["default_cpu_mode"] == "host-model" and q["host_model"] == "Broadwell-IBRS"
    assert q["disk_buses"] == ["virtio", "scsi", "sata"] and q["cdrom_bus"] == "sata"
    assert q["video_models"] == ["virtio", "vga", "bochs"] and q["default_video"] == "virtio"
    assert q["graphics"] == ["vnc", "spice"] and q["tpm_model"] == "tpm-crb"
    assert opts["by_machine"]["pc"]["max_vcpus"] == 255
    assert "q35" not in opts["disabled"]


def test_form_options_no_efi_at_all():
    text = fx("domcapabilities-q35-no-efi-autoselect.xml")
    start = text.index("<loader supported='yes'>")
    end = text.index("</loader>", start) + len("</loader>")
    text = text[:start] + "<loader supported='no'/>" + text[end:]
    opts = kc.form_options(CAPS, {"q35": dc(text)})
    assert opts["by_machine"]["q35"]["firmware"] == ["bios"]
    assert opts["disabled"]["q35"]["efi"] == kc.REASON_NO_EFI
    assert opts["disabled"]["q35"]["secure_boot"] == kc.REASON_NO_SECURE


def test_form_options_missing_family():
    opts = kc.form_options(CAPS, {"q35": dc(Q35)})
    assert opts["machines"] == ["q35"]
    assert opts["disabled"]["pc"] == {"machine": kc.REASON_NO_DOMCAPS}
    no_pc = dict(CAPS, machines=[m for m in CAPS["machines"] if m["name"] != "pc"])
    opts = kc.form_options(no_pc, {"q35": dc(Q35), "pc": dc(PC)})
    assert opts["disabled"]["pc"] == {"machine": "the host has no pc machine type"}
    empty = kc.form_options({"arch": "x86_64", "machines": []}, {})
    assert empty["machines"] == [] and empty["default_machine"] is None


@pytest.mark.parametrize("loader,template", [
    ("/usr/share/qemu/ovmf-x86_64-4m-code.qcow2", "/usr/share/qemu/ovmf-x86_64-4m-vars.qcow2"),
    ("/usr/share/OVMF/OVMF_CODE.secboot.fd", "/usr/share/OVMF/OVMF_VARS.secboot.fd"),
    ("/usr/share/edk2/ovmf/OVMF.fd", None),
    ("", None),
    (None, None),
])
def test_nvram_template(loader, template):
    assert kc.nvram_template(loader) == template


@pytest.mark.parametrize("machine,family", [
    ("q35", "q35"), ("pc-q35-11.1", "q35"), ("pc", "pc"), ("pc-i440fx-9.2", "pc"),
    ("microvm", None), ("", None), (None, None), ("q35x", None),
])
def test_machine_family(machine, family):
    assert kc.machine_family(machine) == family


def test_efi_loaders_skip_seabios_sev_tdx():
    plain, secure = kc._efi_loaders(dc(Q35)["loaders"])
    every = plain + secure
    assert not any(p.endswith(("bios.bin", "bios-256k.bin", "sev.bin", "tdx-secureboot.bin"))
                   for p in every)
    # les chargeurs sans SMM passent d'abord parmi les non sécurisés
    assert "smm" not in plain[0]
    assert all(("-ms-" in p or "suse" in p) for p in secure)
