"""Capacités d'un hôte KVM, lues par `virsh capabilities` et
`virsh domcapabilities` (prochaine version KVM, tâche 1).

Module pur : il ne lance rien, il analyse le texte relu par la couche
transport (`kvm_host`) et en déduit ce que le formulaire de création peut
proposer, avec la raison de chaque option grisée.

Choix :
- aucun chemin de micrologiciel codé en dur : on prend ce que l'hôte
  annonce (`<loader><value>`), et la sélection automatique de libvirt
  (`<enum name='firmware'><value>efi`) passe avant les chemins explicites ;
- un TPM n'est proposé que si le moteur `emulator` (swtpm) est annoncé ;
- deux familles de machine seulement : `q35` (par défaut) et `pc` (i440fx).
"""

import re
import xml.etree.ElementTree as ET

import kvm_host as kh

# familles de machine proposées, dans l'ordre d'affichage
MACHINE_FAMILIES = ("q35", "pc")
DEFAULT_MACHINE = "q35"

REASON_NO_MACHINE = "the host has no {family} machine type"
REASON_NO_EFI = "the host announces no UEFI firmware"
REASON_SECURE_PC = "Secure Boot needs the q35 machine type"
REASON_NO_SECURE = "the host has no Secure Boot firmware"
REASON_NO_TPM = "the host has no TPM emulator (swtpm)"
REASON_NO_SCSI = "the host offers no SCSI disk bus"
REASON_NO_DOMCAPS = "the host capabilities for this machine type could not be read"

# un chargeur explicite reconnu comme Secure Boot par son nom (repli quand
# la sélection automatique manque) : clés enrôlées d'un éditeur (ms, suse,
# opensuse, redhat) ou nom explicite (secboot, secure, enrolled)
_SECURE_LOADER_RE = re.compile(r"(secboot|secure|enrolled|[-_.](ms|suse|opensuse|redhat)[-_.])", re.I)
_EFI_LOADER_RE = re.compile(r"(ovmf|edk2)", re.I)
_NOT_PLAIN_EFI_RE = re.compile(r"(sev|tdx|csm|^bios)", re.I)


def _safe(text):
    try:
        return kh._safe_xml(text)
    except (ET.ParseError, ValueError, TypeError) as exc:
        raise ValueError(f"unreadable capabilities XML: {exc.__class__.__name__}") from None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _enum(parent, name):
    """Valeurs d'un `<enum name=...>` direct de `parent`, dans l'ordre."""
    if parent is None:
        return []
    for e in parent.findall("enum"):
        if e.get("name") == name:
            return [(v.text or "").strip() for v in e.findall("value") if (v.text or "").strip()]
    return []


def _supported(el):
    return el is not None and el.get("supported") == "yes"


def machine_family(machine):
    """`q35`, `pc` ou None d'après un nom de machine QEMU."""
    m = (machine or "").strip()
    if m == "q35" or m.startswith("pc-q35-"):
        return "q35"
    if m == "pc" or m.startswith("pc-i440fx-"):
        return "pc"
    return None


def parse_capabilities(text, arch=None):
    """`virsh capabilities` -> {arch, machines: [{name, canonical, max_cpus,
    deprecated}], domain_types}. `arch` : architecture invitée voulue, par
    défaut celle de l'hôte (`<host><cpu><arch>`)."""
    root = _safe(text)
    if root.tag != "capabilities":
        raise ValueError("not a capabilities document")
    host_arch = (root.findtext("host/cpu/arch") or "").strip() or None
    want = arch or host_arch
    for guest in root.findall("guest"):
        if (guest.findtext("os_type") or "").strip() != "hvm":
            continue
        a = guest.find("arch")
        if a is None or a.get("name") != want:
            continue
        machines = []
        for m in a.findall("machine"):
            name = (m.text or "").strip()
            if not name:
                continue
            machines.append({"name": name, "canonical": m.get("canonical") or None,
                             "max_cpus": _int(m.get("maxCpus")),
                             "deprecated": m.get("deprecated") == "yes"})
        domains = [d.get("type") for d in a.findall("domain") if d.get("type")]
        return {"arch": want, "machines": machines, "domain_types": domains}
    return {"arch": want, "machines": [], "domain_types": []}


def parse_domcapabilities(text):
    """`virsh domcapabilities --virttype kvm --arch A --machine M` -> dict
    (voir le plan). Les booléens et listes absents valent False ou []."""
    root = _safe(text)
    if root.tag != "domainCapabilities":
        raise ValueError("not a domainCapabilities document")
    os_el = root.find("os")
    loader = os_el.find("loader") if os_el is not None else None
    firmware = _enum(os_el, "firmware") if _supported(os_el) else []
    loaders = []
    if _supported(loader):
        loaders = [(v.text or "").strip() for v in loader.findall("value")
                   if (v.text or "").strip()]
    secure_enum = _enum(loader, "secure") if _supported(loader) else []
    cpu = root.find("cpu")
    modes = {}
    if cpu is not None:
        for mode in cpu.findall("mode"):
            modes[mode.get("name")] = mode
    hp = modes.get("host-passthrough")
    hm = modes.get("host-model")
    custom = modes.get("custom")
    host_model = None
    if _supported(hm):
        host_model = (hm.findtext("model") or "").strip() or None
    custom_models = []
    if _supported(custom):
        for m in custom.findall("model"):
            name = (m.text or "").strip()
            if name and m.get("usable") == "yes":
                custom_models.append(name)
    devices = root.find("devices")

    def dev(name):
        return devices.find(name) if devices is not None else None

    disk = dev("disk")
    tpm_el = dev("tpm")
    tpm = None
    if _supported(tpm_el):
        tpm = {"models": _enum(tpm_el, "model"), "backends": _enum(tpm_el, "backendModel"),
               "versions": _enum(tpm_el, "backendVersion")}
    features = root.find("features")
    smm = None
    if features is not None and features.find("smm") is not None:
        smm = _supported(features.find("smm"))
    return {
        "machine": (root.findtext("machine") or "").strip() or None,
        "arch": (root.findtext("arch") or "").strip() or None,
        "domain": (root.findtext("domain") or "").strip() or None,
        "firmware_autoselect": "efi" in firmware,
        "loaders": loaders,
        "secure_boot": "yes" in secure_enum,
        "cpu_modes": {"host_passthrough": _supported(hp),
                      "host_model": host_model,
                      "custom": custom_models},
        "disk_buses": _enum(disk, "bus") if _supported(disk) else [],
        "disk_devices": _enum(disk, "diskDevice") if _supported(disk) else [],
        "video_models": _enum(dev("video"), "modelType") if _supported(dev("video")) else [],
        "graphics_types": _enum(dev("graphics"), "type") if _supported(dev("graphics")) else [],
        "tpm": tpm,
        "iothreads": _supported(root.find("iothreads")),
        "smm": smm,
        "max_vcpus": _int(root.find("vcpu").get("max")) if root.find("vcpu") is not None else None,
    }


def _efi_loaders(loaders):
    """Chargeurs UEFI x86 utilisables en repli : (non sécurisés, sécurisés),
    dans l'ordre de l'hôte ; parmi les non sécurisés, ceux qui n'exigent pas
    SMM passent d'abord."""
    plain, plain_smm, secure = [], [], []
    for path in loaders:
        base = path.rsplit("/", 1)[-1]
        if not _EFI_LOADER_RE.search(base) or _NOT_PLAIN_EFI_RE.search(base):
            continue
        if _SECURE_LOADER_RE.search(base):
            secure.append(path)
        else:
            (plain_smm if "smm" in base.lower() else plain).append(path)
    return plain + plain_smm, secure


def nvram_template(loader):
    """Modèle de variables déduit du chargeur (`...code...` -> `...vars...`),
    ou None : libvirt prend alors le modèle de sa configuration."""
    if not loader:
        return None
    head, _, base = loader.rpartition("/")
    for a, b in (("CODE", "VARS"), ("code", "vars"), ("Code", "Vars")):
        if a in base:
            return (head + "/" if head else "") + base.replace(a, b, 1)
    return None


def _canonical(caps, family):
    """Nom canonique d'une famille (`q35` -> `pc-q35-10.2`) et max de vCPU."""
    for m in caps.get("machines") or []:
        if m["name"] == family:
            canon = m.get("canonical") or family
            max_cpus = m.get("max_cpus")
            for other in caps["machines"]:
                if other["name"] == canon and other.get("max_cpus"):
                    max_cpus = other["max_cpus"]
            return canon, max_cpus
    return None, None


def form_options(caps, domcaps_by_machine):
    """Ce que le formulaire propose sur cet hôte.

    `caps` : résultat de parse_capabilities ; `domcaps_by_machine` :
    {"q35": parse_domcapabilities(...), "pc": ...} (une entrée absente ou
    None = capacités illisibles pour cette famille).

    Rend {arch, machines, default_machine, by_machine: {famille: {...}},
    disabled: {famille: {option: raison}}}."""
    by_machine, disabled = {}, {}
    machines = []
    for family in MACHINE_FAMILIES:
        canon, max_cpus = _canonical(caps, family)
        off = {}
        if canon is None:
            disabled[family] = {"machine": REASON_NO_MACHINE.format(family=family)}
            continue
        dc = (domcaps_by_machine or {}).get(family)
        if not dc:
            disabled[family] = {"machine": REASON_NO_DOMCAPS}
            continue
        machines.append(family)
        plain, secure = _efi_loaders(dc["loaders"])
        efi = None
        firmware = ["bios"]
        if dc["firmware_autoselect"]:
            efi = {"autoselect": True, "loader": None, "nvram_template": None,
                   "secure_loader": None, "secure_nvram_template": None}
        elif plain or secure:
            lp = plain[0] if plain else None
            ls = secure[0] if secure else None
            efi = {"autoselect": False, "loader": lp, "nvram_template": nvram_template(lp),
                   "secure_loader": ls, "secure_nvram_template": nvram_template(ls)}
        if efi:
            firmware.append("efi")
        else:
            off["efi"] = REASON_NO_EFI
        if family != "q35":
            secure_ok = False
            off["secure_boot"] = REASON_SECURE_PC
        elif not efi or not dc["secure_boot"] or dc.get("smm") is False or (
                not efi["autoselect"] and not efi["secure_loader"]):
            secure_ok = False
            off["secure_boot"] = REASON_NO_SECURE
        else:
            secure_ok = True
        tpm = dc.get("tpm")
        tpm_ok = bool(tpm and "emulator" in tpm["backends"]
                      and (not tpm["versions"] or "2.0" in tpm["versions"]))
        tpm_model = None
        if tpm_ok:
            for m in ("tpm-crb", "tpm-tis"):
                if m in tpm["models"]:
                    tpm_model = m
                    break
            tpm_ok = tpm_model is not None
        if not tpm_ok:
            off["tpm"] = REASON_NO_TPM
        buses = [b for b in ("virtio", "scsi", "sata", "ide") if b in dc["disk_buses"]]
        if family == "q35":
            buses = [b for b in buses if b != "ide"]   # pas d'IDE sur q35
        if "scsi" not in buses:
            off["scsi"] = REASON_NO_SCSI
        cdrom_bus = "sata" if family == "q35" else "ide"
        cpu_modes = []
        if dc["cpu_modes"]["host_model"]:
            cpu_modes.append("host-model")
        if dc["cpu_modes"]["host_passthrough"]:
            cpu_modes.append("host-passthrough")
        if dc["cpu_modes"]["custom"]:
            cpu_modes.append("custom")
        videos = [v for v in ("virtio", "vga", "bochs") if v in dc["video_models"]]
        dmax = dc.get("max_vcpus")
        limit = min(x for x in (max_cpus, dmax) if x) if (max_cpus or dmax) else None
        by_machine[family] = {
            "canonical": canon, "max_vcpus": limit, "firmware": firmware, "efi": efi,
            "secure_boot": secure_ok, "tpm": tpm_ok, "tpm_model": tpm_model,
            "cpu_modes": cpu_modes, "default_cpu_mode": cpu_modes[0] if cpu_modes else None,
            "host_model": dc["cpu_modes"]["host_model"],
            "cpu_models": list(dc["cpu_modes"]["custom"]),
            "disk_buses": buses, "cdrom_bus": cdrom_bus,
            "video_models": videos, "default_video": videos[0] if videos else None,
            "graphics": [g for g in dc["graphics_types"] if g in ("vnc", "spice")],
            "iothreads": bool(dc.get("iothreads")),
        }
        if off:
            disabled[family] = off
    default = DEFAULT_MACHINE if DEFAULT_MACHINE in machines else (machines[0] if machines else None)
    return {"arch": caps.get("arch"), "machines": machines, "default_machine": default,
            "by_machine": by_machine, "disabled": disabled}
