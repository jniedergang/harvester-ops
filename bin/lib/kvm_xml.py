"""XML des domaines libvirt fabriqué et modifié par la console (prochaine
version KVM, tâche 1).

Module pur : aucune connexion à l'hôte. Il produit le XML de création d'une
VM à partir du spec du formulaire (`build_domain`), les fragments d'un
disque ou d'une carte pour `attach-device` (`device_xml`), et applique des
modifications ciblées sur l'arbre d'un XML inactif existant (`Edit`).

Principes :
- tout passe par ElementTree, jamais par concaténation de chaînes : une
  valeur hostile (nom, titre, chemin) ne peut pas casser le document. Les
  caractères interdits en XML 1.0 (et les fins de ligne là où libvirt n'en
  veut pas) sont refusés par `check_spec` avant toute fabrication ;
- le formulaire n'est jamais une source de vérité complète : une
  modification ne touche que ses éléments et leurs attributs, tout le reste
  (épinglage, ordonnanceur temps réel, `<hostdev>`, `<seclabel>`,
  métadonnées d'autres outils) reste tel quel ;
- le mot de passe VNC n'entre jamais dans une empreinte (`canonical_digest`)
  ni dans une vue (`managed_view` dit seulement `password: True`).
"""

import copy
import hashlib
import re
import xml.etree.ElementTree as ET

import kvm_caps
import kvm_host as kh
import kvm_ops as ko

HOPS_NS = "https://harvester-ops.io/xmlns/vm/1"
HOPS_PREFIX = "hops"

OS_TYPES = ("linux", "windows", "other")
FIRMWARES = ("bios", "efi")
CPU_MODES = ("host-model", "host-passthrough", "custom")
DISK_DEVICES = ("disk", "cdrom")
DISK_BUSES = ("virtio", "scsi", "sata", "ide")
SOURCE_KINDS = ("blank", "image", "volume", "iso", "empty")
DISK_FORMATS = ("qcow2", "raw")
CACHE_MODES = ("none", "writeback", "writethrough", "directsync", "unsafe", "default")
NIC_KINDS = ("network", "bridge", "direct")
NIC_MODELS = ("virtio", "e1000e", "e1000", "rtl8139")
DIRECT_MODES = ("bridge", "vepa", "private", "passthrough")
VIDEO_MODELS = ("virtio", "vga", "bochs")
LIFECYCLE = ("destroy", "restart", "preserve", "rename-restart")
CRASH_ACTIONS = LIFECYCLE + ("coredump-destroy", "coredump-restart")
WATCHDOG_ACTIONS = ("reset", "shutdown", "poweroff", "pause", "none", "dump", "inject-nmi")
HOTPLUG_SLOTS_DEFAULT = 4
HOTPLUG_SLOTS_MAX = 16
VNC_PASSWORD_LEN = 8          # limite du protocole VNC
TITLE_MAX = 256
SERIAL_MAX = 64
GUEST_AGENT_CHANNEL = "org.qemu.guest_agent.0"

# préréglages par type d'OS (bus et modèles par défaut, vidéo)
OS_PRESETS = {
    "linux": {"disk_bus": "virtio", "nic_model": "virtio", "video": "virtio"},
    "windows": {"disk_bus": "sata", "nic_model": "e1000e", "video": "vga"},
    "other": {"disk_bus": "sata", "nic_model": "e1000e", "video": "vga"},
}

# appareils PCI conventionnels : sur q35 libvirt ajoute un pont
# pcie-to-pci-bridge, qui prend lui-même un port PCIe
_CONVENTIONAL_PCI_NICS = ("e1000", "rtl8139")

REASON_FIRMWARE_HAND = "firmware set by hand: change it outside the console"
REASON_TPM_HAND = "TPM set by hand: change it outside the console"
REASON_FAMILY = ("changing between the q35 and pc machine types is not supported: "
                 "the device addresses would not fit")

_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
_IFNAME_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,15}$")
_TARGET_RE = re.compile(r"^(vd|sd|hd)([a-z]{1,2})$")
_TARGET_PREFIX = {"virtio": "vd", "scsi": "sd", "sata": "sd", "ide": "hd"}


class EditError(ko.OpError):
    """Refus d'une modification ciblée (genre `refused` ou `invalid`)."""


# --- contrôles des valeurs ----------------------------------------------

def _xml_char_ok(ch, multiline):
    o = ord(ch)
    if ch in "\t\n\r":
        return multiline
    return 0x20 <= o <= 0xD7FF or 0xE000 <= o <= 0xFFFD or 0x10000 <= o <= 0x10FFFF


def xml_safe(text, multiline=False):
    """Vrai si `text` peut entrer tel quel dans un XML 1.0 (et sans fin de
    ligne quand `multiline` est faux : un attribut normaliserait la valeur,
    libvirt refuse un titre sur plusieurs lignes)."""
    return isinstance(text, str) and all(_xml_char_ok(c, multiline) for c in text)


def _safe_name(value, what, reasons, allow_slash=False, max_len=255):
    """Nom libvirt (pool, volume, réseau) : non vide, pas de caractère de
    contrôle, pas de `/` (sauf permis), pas de point en tête."""
    if not isinstance(value, str) or not value or len(value) > max_len \
            or not xml_safe(value) or value.strip() != value:
        reasons.append(f"invalid {what}: {_show(value)}")
        return False
    if (not allow_slash and "/" in value) or value.startswith(".") or value.startswith("-"):
        reasons.append(f"invalid {what}: {_show(value)}")
        return False
    return True


def _show(value):
    """Valeur citée dans une raison, bornée et sans caractère de contrôle."""
    text = value if isinstance(value, str) else repr(value)
    text = "".join(c if _xml_char_ok(c, False) else "?" for c in text)
    return repr(text[:64] + ("..." if len(text) > 64 else ""))


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{1,19}", value.strip()):
        return int(value.strip())
    return None


def _target_index(suffix):
    """`a` -> 0, `z` -> 25, `aa` -> 26 (comme libvirt)."""
    n = 0
    for c in suffix:
        n = n * 26 + (ord(c) - 96)
    return n - 1


def _target_name(prefix, index):
    s = ""
    index += 1
    while index > 0:
        index, r = divmod(index - 1, 26)
        s = chr(97 + r) + s
    return prefix + s


def _free_target(prefix, used):
    i = 0
    while _target_name(prefix, i) in used:
        i += 1
    return _target_name(prefix, i)


def volume_name(vm, target):
    """Nom du volume créé pour un disque : `<vm>-<cible>.qcow2`."""
    return f"{vm}-{target}.qcow2"


def seed_volume_name(vm):
    """Nom du volume cloud-init d'une VM : `<vm>-cidata.iso`."""
    return f"{vm}-cidata.iso"


# --- normalisation du spec ------------------------------------------------

def _machine_opts(options, machine):
    return (options or {}).get("by_machine", {}).get(machine) or {}


def normalize(spec, options):
    """Copie du spec avec les valeurs par défaut posées (préréglage d'OS,
    machine par défaut de l'hôte, cibles attribuées par bus). Ne contrôle
    rien : voir check_spec."""
    s = copy.deepcopy(spec or {})
    s.setdefault("os", "linux")
    preset = OS_PRESETS.get(s["os"], OS_PRESETS["other"])
    s["machine"] = s.get("machine") or (options or {}).get("default_machine") or "q35"
    mo = _machine_opts(options, s["machine"])
    s.setdefault("firmware", "bios")
    s["secure_boot"] = bool(s.get("secure_boot"))
    s["tpm"] = bool(s.get("tpm"))
    cpu = dict(s.get("cpu") or {})
    cpu["mode"] = cpu.get("mode") or mo.get("default_cpu_mode") or "host-model"
    cpu.setdefault("model", None)
    cpu["hotplug"] = bool(cpu.get("hotplug"))
    vcpus = _int(cpu.get("vcpus"))
    cpu["vcpus"] = vcpus if vcpus is not None else cpu.get("vcpus", 1)
    if cpu.get("max_vcpus") is None:
        cpu["max_vcpus"] = cpu["vcpus"]
    if cpu["hotplug"]:
        # une unité par socket, comme Harvester : sockets = max, cœurs = threads = 1
        cpu["sockets"], cpu["cores"], cpu["threads"] = cpu["max_vcpus"], 1, 1
    elif cpu.get("sockets") is None and cpu.get("cores") is None and cpu.get("threads") is None:
        cpu["sockets"], cpu["cores"], cpu["threads"] = cpu["max_vcpus"], 1, 1
    else:
        for k in ("sockets", "cores", "threads"):
            if cpu.get(k) is None:
                cpu[k] = 1
    s["cpu"] = cpu
    mem = dict(s.get("memory") or {})
    mem.setdefault("current_mib", 1024)
    if mem.get("max_mib") is None:
        mem["max_mib"] = mem["current_mib"]
    mem["balloon"] = mem.get("balloon", True) is not False
    s["memory"] = mem
    disks = []
    used = set()
    for d in s.get("disks") or []:
        d = dict(d)
        src = dict(d.get("source") or {})
        src.setdefault("kind", "blank")
        if src["kind"] in ("iso", "empty"):
            d.setdefault("device", "cdrom")
        d.setdefault("device", "disk")
        if d.get("device") == "cdrom":
            d["bus"] = d.get("bus") or mo.get("cdrom_bus") or (
                "sata" if kvm_caps.machine_family(s["machine"]) != "pc" else "ide")
        else:
            d["bus"] = d.get("bus") or preset["disk_bus"]
        if src["kind"] in ("blank", "image"):
            src.setdefault("format", "qcow2")
        elif src["kind"] == "iso":
            src.setdefault("format", "raw")
        if src["kind"] == "image":
            src["linked"] = src.get("linked", True) is not False
        d["source"] = src
        d["readonly"] = bool(d.get("readonly")) or d["device"] == "cdrom"
        d["shareable"] = bool(d.get("shareable"))
        d["io_thread"] = bool(d.get("io_thread"))
        if d.get("target"):
            used.add(d["target"])
        disks.append(d)
    for d in disks:
        if not d.get("target"):
            d["target"] = _free_target(_TARGET_PREFIX.get(d["bus"], "sd"), used)
            used.add(d["target"])
    s["disks"] = disks
    nics = []
    for n in s.get("nics") or []:
        n = dict(n)
        n.setdefault("kind", "network")
        n["model"] = n.get("model") or preset["nic_model"]
        if n["kind"] == "direct":
            n["mode"] = n.get("mode") or "bridge"
        if isinstance(n.get("mac"), str):
            n["mac"] = n["mac"].lower()
        nics.append(n)
    s["nics"] = nics
    disp = dict(s.get("display") or {})
    disp["vnc"] = disp.get("vnc", True) is not False
    disp["password"] = bool(disp.get("password"))
    vid = disp.get("video")
    if not vid:
        offered = mo.get("video_models") or []
        vid = preset["video"] if (not offered or preset["video"] in offered) else (
            mo.get("default_video") or preset["video"])
    disp["video"] = vid
    s["display"] = disp
    for k, default in (("guest_agent", True), ("serial_console", True), ("tablet", True),
                       ("rng", True)):
        s[k] = s.get(k, default) is not False
    s.setdefault("watchdog", None)
    if s.get("hotplug_slots") is None:
        s["hotplug_slots"] = HOTPLUG_SLOTS_DEFAULT
    s["autostart"] = bool(s.get("autostart"))
    s["on_poweroff"] = s.get("on_poweroff") or "destroy"
    s["on_reboot"] = s.get("on_reboot") or "restart"
    s["on_crash"] = s.get("on_crash") or "destroy"
    s.setdefault("sysinfo_serial", None)
    s.setdefault("cloudinit", None)
    return s


def check_spec(spec, options):
    """Raisons de refus du spec (liste vide = acceptable). `options` :
    résultat de kvm_caps.form_options, éventuellement complété par
    `images` ({"pool/volume": {"capacity", "format"}}) et `macs_in_use`
    (MAC des domaines de l'hôte)."""
    reasons = []
    if not isinstance(spec, dict):
        return ["the spec must be an object"]
    for key in ("cpu", "memory", "display"):
        if spec.get(key) is not None and not isinstance(spec[key], dict):
            reasons.append(f"{key} must be an object")
    for key in ("disks", "nics"):
        items = spec.get(key)
        if items is not None and (not isinstance(items, list)
                                  or not all(isinstance(i, dict) for i in items)
                                  or any(i.get("source") is not None
                                         and not isinstance(i["source"], (dict, str))
                                         for i in items)):
            reasons.append(f"{key} must be a list of objects")
    for d in spec.get("disks") or []:
        if isinstance(d, dict) and d.get("source") is not None and not isinstance(d["source"], dict):
            reasons.append("disks must be a list of objects")
            break
    for key in ("name", "title", "description", "os", "machine", "firmware"):
        if spec.get(key) is not None and not isinstance(spec[key], str):
            reasons.append(f"{key} must be text")
    if reasons:
        return reasons
    s = normalize(spec, options)
    try:
        ko.check_new_vm_name(s.get("name"))
    except ko.OpError as exc:
        reasons.append(f"name: {exc.message}")
    else:
        if not xml_safe(s["name"]):
            reasons.append("name: invalid characters")
    if s.get("title") is not None and (not xml_safe(s["title"]) or len(s["title"]) > TITLE_MAX):
        reasons.append(f"invalid title: one line, {TITLE_MAX} characters at most")
    if s.get("description") is not None and (
            not xml_safe(s["description"], multiline=True)
            or len(s["description"]) > ko.DESCRIPTION_MAX):
        reasons.append(f"invalid description ({ko.DESCRIPTION_MAX} characters at most)")
    if s["os"] not in OS_TYPES:
        reasons.append(f"unknown operating system: {_show(s['os'])}")
    machine = s["machine"]
    family = kvm_caps.machine_family(machine) if isinstance(machine, str) else None
    mo = _machine_opts(options, machine)
    off = (options or {}).get("disabled", {}).get(machine, {}) if isinstance(machine, str) else {}
    if machine not in MACHINE_CHOICES or not mo:
        reasons.append(off.get("machine") or f"machine type not offered by this host: {_show(machine)}")
    if s["firmware"] not in FIRMWARES:
        reasons.append(f"unknown firmware: {_show(s['firmware'])}")
    elif s["firmware"] == "efi" and mo and "efi" not in mo.get("firmware", []):
        reasons.append(off.get("efi") or kvm_caps.REASON_NO_EFI)
    if s["secure_boot"]:
        if family == "pc":
            reasons.append(kvm_caps.REASON_SECURE_PC)
        elif s["firmware"] != "efi":
            reasons.append("Secure Boot needs UEFI firmware")
        elif mo and not mo.get("secure_boot"):
            reasons.append(off.get("secure_boot") or kvm_caps.REASON_NO_SECURE)
    if s["tpm"] and mo and not mo.get("tpm"):
        reasons.append(off.get("tpm") or kvm_caps.REASON_NO_TPM)
    _check_cpu(s, mo, reasons)
    _check_memory(s, reasons)
    _check_disks(s, mo, options or {}, reasons)
    _check_nics(s, options or {}, reasons)
    _check_misc(s, mo, reasons)
    return reasons


MACHINE_CHOICES = kvm_caps.MACHINE_FAMILIES


def _check_cpu(s, mo, reasons):
    cpu = s["cpu"]
    if cpu["mode"] not in CPU_MODES or (mo and cpu["mode"] not in mo.get("cpu_modes", [])):
        reasons.append(f"CPU mode not offered by this host: {_show(cpu['mode'])}")
    if cpu["mode"] == "custom":
        if not isinstance(cpu.get("model"), str) or not cpu["model"] or (
                mo and cpu["model"] not in mo.get("cpu_models", [])):
            reasons.append(f"CPU model not usable on this host: {_show(cpu.get('model'))}")
    vals = {k: _int(cpu.get(k)) for k in ("vcpus", "max_vcpus", "sockets", "cores", "threads")}
    bad = [k for k, v in vals.items() if v is None or v < 1]
    if bad:
        reasons.append("vCPU counts must be positive integers: " + ", ".join(bad))
        return
    if vals["vcpus"] > vals["max_vcpus"]:
        reasons.append("more vCPUs than the maximum")
    limit = mo.get("max_vcpus") if mo else None
    if limit and vals["max_vcpus"] > limit:
        reasons.append(f"more vCPUs than the machine type allows ({limit})")
    product = vals["sockets"] * vals["cores"] * vals["threads"]
    if product != vals["max_vcpus"]:
        reasons.append(f"the topology (sockets x cores x threads = {product}) must equal "
                       f"the maximum vCPUs ({vals['max_vcpus']})")


def _check_memory(s, reasons):
    mem = s["memory"]
    cur, mx = _int(mem.get("current_mib")), _int(mem.get("max_mib"))
    if cur is None or mx is None or cur < 1 or mx < 1:
        reasons.append("memory must be a positive number of MiB")
        return
    if cur > mx:
        reasons.append("current memory above the maximum")


def _check_disks(s, mo, options, reasons):
    seen, boots = set(), {}
    images = options.get("images") or {}
    buses = mo.get("disk_buses") if mo else None
    family = kvm_caps.machine_family(s["machine"]) if isinstance(s["machine"], str) else None
    for d in s["disks"]:
        target = d.get("target")
        label = f"disk {target}" if isinstance(target, str) and xml_safe(target) else "disk"
        if d.get("device") not in DISK_DEVICES:
            reasons.append(f"{label}: unknown device {_show(d.get('device'))}")
            continue
        bus = d.get("bus")
        if bus not in DISK_BUSES:
            reasons.append(f"{label}: unknown bus {_show(bus)}")
            continue
        if bus == "ide" and family == "q35":
            reasons.append(f"{label}: the q35 machine type has no IDE bus")
        elif buses is not None and bus not in buses and bus != "ide":
            reasons.append(f"{label}: bus {bus} not offered by this host")
        if d["device"] == "cdrom" and bus == "virtio":
            reasons.append(f"{label}: a CD-ROM cannot use the virtio bus")
        if d["device"] == "disk" and d["readonly"] and bus not in ("virtio", "scsi"):
            # refus de libvirt vu en réel : « readonly sata disks are not supported »
            reasons.append(f"{label}: a read-only disk needs the virtio or scsi bus")
        m = _TARGET_RE.match(target) if isinstance(target, str) else None
        if not m:
            reasons.append(f"invalid disk target: {_show(target)}")
        else:
            if m.group(1) != _TARGET_PREFIX[bus]:
                reasons.append(f"{label}: target does not match bus {bus}")
            if target in seen:
                reasons.append(f"two disks use the target {target}")
            seen.add(target)
        src = d.get("source") or {}
        kind = src.get("kind")
        if kind not in SOURCE_KINDS:
            reasons.append(f"{label}: unknown source {_show(kind)}")
            continue
        if kind in ("iso", "empty") and d["device"] != "cdrom":
            reasons.append(f"{label}: an ISO or an empty drive needs a CD-ROM")
        if d["device"] == "cdrom" and kind not in ("iso", "empty", "volume"):
            reasons.append(f"{label}: a CD-ROM takes an ISO, a volume or nothing")
        if kind != "empty":
            _safe_name(src.get("pool"), f"{label} pool", reasons)
        if kind == "volume":
            _safe_name(src.get("volume"), f"{label} volume", reasons)
        if kind == "iso":
            _safe_name(src.get("volume") or src.get("image"), f"{label} ISO", reasons)
        if kind in ("blank", "image") and src.get("format") not in DISK_FORMATS:
            reasons.append(f"{label}: unknown format {_show(src.get('format'))}")
        size = src.get("size_bytes")
        if kind == "blank":
            if _int(size) is None or _int(size) < 1:
                reasons.append(f"{label}: a blank disk needs a size in bytes")
        if kind == "image":
            ipool = src.get("image_pool") or src.get("pool")
            if _safe_name(src.get("image"), f"{label} image", reasons) and isinstance(ipool, str):
                info = images.get(f"{ipool}/{src['image']}") or {}
                cap = _int(info.get("capacity"))
                if size is not None and (_int(size) is None or _int(size) < 1):
                    reasons.append(f"{label}: invalid size")
                elif cap and size is not None and _int(size) < cap:
                    reasons.append(f"{label}: size below the image virtual size ({cap} bytes)")
                if src.get("linked") and src.get("image_pool") and src["image_pool"] != src.get("pool"):
                    reasons.append(f"{label}: a linked disk must be in the image pool")
                ifmt = info.get("format")
                if src.get("linked") and ifmt and ifmt not in DISK_FORMATS:
                    reasons.append(f"{label}: a linked disk needs a qcow2 or raw image")
        cache = d.get("cache")
        if cache is not None and cache not in CACHE_MODES:
            reasons.append(f"{label}: unknown cache mode {_show(cache)}")
        serial = d.get("serial")
        if serial is not None and (not xml_safe(serial) or not serial
                                   or len(serial) > 20 or not re.fullmatch(r"[A-Za-z0-9_.+-]+", serial)):
            reasons.append(f"{label}: invalid serial number (20 letters, digits, _ . + - at most)")
        if d["io_thread"]:
            if bus != "virtio":
                reasons.append(f"{label}: a dedicated I/O thread needs the virtio bus")
            elif mo and not mo.get("iothreads", True):
                reasons.append(f"{label}: the host offers no I/O threads")
        _check_boot(d.get("boot_order"), label, boots, reasons)
    s["_boots"] = boots


def _check_boot(order, label, boots, reasons):
    if order is None:
        return
    o = _int(order)
    if o is None or o < 1:
        reasons.append(f"{label}: invalid boot order {_show(order)}")
    elif o in boots:
        reasons.append(f"two devices use boot order {o}")
    else:
        boots[o] = label


def _check_nics(s, options, reasons):
    macs = set()
    in_use = {m.lower() for m in options.get("macs_in_use") or [] if isinstance(m, str)}
    boots = s.pop("_boots", {})
    for i, n in enumerate(s["nics"]):
        label = f"network card {i + 1}"
        kind = n.get("kind")
        if kind not in NIC_KINDS:
            reasons.append(f"{label}: unknown kind {_show(kind)}")
            continue
        src = n.get("source")
        if kind == "network":
            _safe_name(src, f"{label} network", reasons)
        elif not isinstance(src, str) or not _IFNAME_RE.match(src) or src in (".", ".."):
            reasons.append(f"{label}: invalid host interface {_show(src)}")
        if kind == "direct" and n.get("mode") not in DIRECT_MODES:
            reasons.append(f"{label}: unknown macvtap mode {_show(n.get('mode'))}")
        if n.get("model") not in NIC_MODELS:
            reasons.append(f"{label}: unknown model {_show(n.get('model'))}")
        mac = n.get("mac")
        if mac is not None:
            if not isinstance(mac, str) or not _MAC_RE.match(mac) or int(mac[:2], 16) & 1 \
                    or mac == "00:00:00:00:00:00":
                reasons.append(f"invalid MAC address: {_show(mac)}")
            elif mac in macs:
                reasons.append(f"MAC address used twice: {mac}")
            elif mac in in_use:
                reasons.append(f"MAC address already used on this host: {mac}")
            if isinstance(mac, str):
                macs.add(mac)
        _check_boot(n.get("boot_order"), label, boots, reasons)


def _check_misc(s, mo, reasons):
    disp = s["display"]
    if disp["video"] not in VIDEO_MODELS or (
            mo and mo.get("video_models") and disp["video"] not in mo["video_models"]):
        reasons.append(f"video model not offered by this host: {_show(disp['video'])}")
    if disp["vnc"] and mo and mo.get("graphics") is not None and "vnc" not in mo["graphics"]:
        reasons.append("the host offers no VNC display")
    slots = _int(s.get("hotplug_slots"))
    if slots is None or not 0 <= slots <= HOTPLUG_SLOTS_MAX:
        reasons.append(f"hot-plug slots: 0 to {HOTPLUG_SLOTS_MAX}")
    for key, allowed in (("on_poweroff", LIFECYCLE), ("on_reboot", LIFECYCLE),
                         ("on_crash", CRASH_ACTIONS)):
        if s[key] not in allowed:
            reasons.append(f"unknown {key} action: {_show(s[key])}")
    wd = s.get("watchdog")
    if wd not in (None, False) and wd not in WATCHDOG_ACTIONS:
        reasons.append(f"unknown watchdog action: {_show(wd)}")
    ser = s.get("sysinfo_serial")
    if ser is not None and (not isinstance(ser, str) or not ser or len(ser) > SERIAL_MAX
                            or not xml_safe(ser)):
        reasons.append(f"invalid firmware serial number ({SERIAL_MAX} characters at most)")
    ci = s.get("cloudinit")
    if ci:
        if not isinstance(ci, dict):
            reasons.append("cloud-init must be an object")
        else:
            for k in ("user_data", "network_data"):
                if ci.get(k) is not None and not isinstance(ci[k], str):
                    reasons.append(f"cloud-init {k} must be text")
            pool = ci.get("seed_pool") or _first_pool(s)
            if pool is None:
                reasons.append("cloud-init needs a pool for its seed volume")
            else:
                _safe_name(pool, "cloud-init seed pool", reasons)


def _first_pool(s):
    for d in s["disks"]:
        src = d.get("source") or {}
        if src.get("kind") != "empty" and src.get("pool"):
            return src["pool"]
    return None


# --- fabrication ----------------------------------------------------------

def _sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items() if v is not None})
    if text is not None:
        el.text = str(text)
    return el


def resolve_volumes(spec):
    """Volumes utilisés par chaque disque d'un spec NORMALISÉ, dans l'ordre :
    [{pool, volume, format, create}] (None pour un lecteur vide), puis le seed.
    `create` : le volume est à créer par la console (vierge, image)."""
    out = []
    for d in spec["disks"]:
        src = d["source"]
        kind = src["kind"]
        if kind == "empty":
            out.append(None)
        elif kind in ("blank", "image"):
            out.append({"pool": src["pool"], "volume": volume_name(spec["name"], d["target"]),
                        "format": src.get("format") or "qcow2", "create": True})
        elif kind == "iso":
            out.append({"pool": src["pool"], "volume": src.get("volume") or src.get("image"),
                        "format": "raw", "create": False})
        else:
            out.append({"pool": src["pool"], "volume": src["volume"],
                        "format": src.get("format") or "raw", "create": False})
    seed = None
    ci = spec.get("cloudinit")
    if ci:
        seed = {"pool": ci.get("seed_pool") or _first_pool(spec),
                "volume": seed_volume_name(spec["name"]), "format": "raw", "create": True}
    return {"disks": out, "seed": seed}


def _disk_el(parent, d, vol, iothread=None):
    """`<disk type='volume'>` d'un disque normalisé, avec son volume résolu."""
    dev = d.get("device", "disk")
    disk = _sub(parent, "disk", type="volume", device=dev)
    fmt = (vol or {}).get("format") or "raw"
    drv = {"name": "qemu", "type": fmt}
    if d.get("cache"):
        drv["cache"] = d["cache"]
    if dev == "disk":
        drv["discard"] = "unmap"
    if iothread is not None:
        drv["iothread"] = iothread
    _sub(disk, "driver", **drv)
    if vol:
        _sub(disk, "source", pool=vol["pool"], volume=vol["volume"])
    _sub(disk, "target", dev=d["target"], bus=d["bus"])
    if d.get("boot_order") is not None:
        _sub(disk, "boot", order=_int(d["boot_order"]))
    if d.get("serial"):
        _sub(disk, "serial", d["serial"])
    if d.get("readonly") or dev == "cdrom":
        _sub(disk, "readonly")
    if d.get("shareable"):
        _sub(disk, "shareable")
    return disk


def _nic_el(parent, n):
    iface = _sub(parent, "interface", type=n["kind"])
    if n.get("mac"):
        _sub(iface, "mac", address=n["mac"].lower())
    if n["kind"] == "network":
        _sub(iface, "source", network=n["source"])
    elif n["kind"] == "bridge":
        _sub(iface, "source", bridge=n["source"])
    else:
        _sub(iface, "source", dev=n["source"], mode=n.get("mode") or "bridge")
    _sub(iface, "model", type=n["model"])
    if n.get("boot_order") is not None:
        _sub(iface, "boot", order=_int(n["boot_order"]))
    return iface


def _pcie_ports_needed(s):
    """Ports PCIe pris par les appareils que libvirt placera sur q35 : chaque
    disque virtio, le contrôleur virtio-scsi, chaque carte PCIe, le
    contrôleur USB, le ballon, le générateur d'aléa, le contrôleur
    virtio-serial de l'agent, et un pont pour les cartes PCI conventionnelles.
    La vidéo est intégrée au bus racine (00:01.0), le SATA aussi (00:1f.2)."""
    n = sum(1 for d in s["disks"] if d["bus"] == "virtio")
    n += 1 if any(d["bus"] == "scsi" for d in s["disks"]) else 0
    n += sum(1 for x in s["nics"] if x["model"] not in _CONVENTIONAL_PCI_NICS)
    n += 1 if any(x["model"] in _CONVENTIONAL_PCI_NICS for x in s["nics"]) else 0
    n += 1                                   # contrôleur USB qemu-xhci
    n += 1 if s["memory"]["balloon"] else 0
    n += 1 if s["rng"] else 0
    n += 1 if s["guest_agent"] else 0
    return n


def _hops_metadata(parent, os_type, seed=None, firmware=None):
    """`<metadata><hops:vm xmlns:hops=...>` écrit avec le préfixe sur
    l'élément lui-même (comme le rend libvirt), pas sur la racine."""
    meta = _sub(parent, "metadata")
    vm = ET.SubElement(meta, f"{HOPS_PREFIX}:vm", {f"xmlns:{HOPS_PREFIX}": HOPS_NS})
    _sub(vm, f"{HOPS_PREFIX}:os", os_type)
    if firmware:
        _sub(vm, f"{HOPS_PREFIX}:firmware", firmware)
    if seed:
        _sub(vm, f"{HOPS_PREFIX}:seed", pool=seed["pool"], volume=seed["volume"])
    return meta


def _explicit_firmware(loader, template, secure):
    """`<loader>` et `<nvram>` explicites (repli sans sélection automatique).
    Un micrologiciel en qcow2 exige `format='qcow2'` (libvirt suppose raw)."""
    if not isinstance(loader, str) or not loader.startswith("/") or not xml_safe(loader):
        raise ko.OpError("invalid", "invalid firmware path")
    fmt = "qcow2" if loader.endswith(".qcow2") else None
    ld = ET.Element("loader", {"readonly": "yes", "type": "pflash"})
    if secure:
        ld.set("secure", "yes")
    if fmt:
        ld.set("format", fmt)
    ld.text = loader
    nv = ET.Element("nvram")
    if template and xml_safe(template):
        nv.set("template", template)
        if template.endswith(".qcow2"):
            nv.set("templateFormat", "qcow2")
    if fmt:
        nv.set("format", fmt)
    return [ld, nv]


def build_domain(spec, options, volumes=None, vnc_password=None):
    """XML de création d'une VM (voir le tableau des choix du plan).

    `volumes` : {"disks": [{pool, volume, format}|None, ...], "seed": {pool,
    volume}|None} aligné sur les disques ; None = déduit du spec
    (`resolve_volumes`). `vnc_password` : 8 caractères au plus, généré par
    la console, posé seulement si `display.password` est vrai. Lève
    OpError `invalid` (data["refusals"]) si le spec est refusé."""
    reasons = check_spec(spec, options)
    if vnc_password is not None and (not isinstance(vnc_password, str) or not vnc_password
                                     or len(vnc_password) > VNC_PASSWORD_LEN
                                     or not re.fullmatch(r"[\x21-\x7e]+", vnc_password)):
        reasons.append(f"invalid VNC password ({VNC_PASSWORD_LEN} printable characters at most)")
    if reasons:
        raise ko.OpError("invalid", "; ".join(reasons), {"refusals": reasons})
    s = normalize(spec, options)
    family = kvm_caps.machine_family(s["machine"])
    mo = _machine_opts(options, s["machine"])
    vols = volumes if volumes is not None else resolve_volumes(s)
    if isinstance(vols, list):
        vols = {"disks": vols, "seed": resolve_volumes(s)["seed"]}
    disk_vols = list(vols.get("disks") or [])
    if len(disk_vols) != len(s["disks"]):
        raise ko.OpError("invalid", "one volume per disk is expected")
    seed = vols.get("seed")
    efi = mo.get("efi") or {}
    explicit_fw = s["firmware"] == "efi" and not efi.get("autoselect")

    dom = ET.Element("domain", type="kvm")
    _sub(dom, "name", s["name"])
    if s.get("title"):
        _sub(dom, "title", s["title"])
    if s.get("description"):
        _sub(dom, "description", s["description"])
    _hops_metadata(dom, s["os"], seed=seed, firmware="explicit" if explicit_fw else None)
    kib = 1024
    _sub(dom, "memory", s["memory"]["max_mib"] * kib, unit="KiB")
    _sub(dom, "currentMemory", s["memory"]["current_mib"] * kib, unit="KiB")
    cpu = s["cpu"]
    vcpu_attrs = {"placement": "static"}
    if cpu["vcpus"] < cpu["max_vcpus"]:
        vcpu_attrs["current"] = cpu["vcpus"]
    _sub(dom, "vcpu", cpu["max_vcpus"], **vcpu_attrs)
    io_disks = [d for d in s["disks"] if d["io_thread"]]
    if io_disks:
        _sub(dom, "iothreads", len(io_disks))
    if s.get("sysinfo_serial"):
        si = _sub(dom, "sysinfo", type="smbios")
        system = _sub(si, "system")
        _sub(system, "entry", s["sysinfo_serial"], name="serial")
    os_el = _sub(dom, "os", firmware="efi" if s["firmware"] == "efi" and not explicit_fw else None)
    _sub(os_el, "type", "hvm", arch=(options or {}).get("arch") or "x86_64", machine=s["machine"])
    if s["firmware"] == "efi":
        if explicit_fw:
            loader = efi.get("secure_loader") if s["secure_boot"] else efi.get("loader")
            tmpl = efi.get("secure_nvram_template") if s["secure_boot"] else efi.get("nvram_template")
            for el in _explicit_firmware(loader, tmpl, s["secure_boot"]):
                os_el.append(el)
        else:
            fw = _sub(os_el, "firmware")
            _sub(fw, "feature", enabled="yes" if s["secure_boot"] else "no", name="secure-boot")
            if s["secure_boot"]:
                _sub(fw, "feature", enabled="yes", name="enrolled-keys")
    if s.get("sysinfo_serial"):
        _sub(os_el, "smbios", mode="sysinfo")
    feats = _sub(dom, "features")
    _sub(feats, "acpi")
    _sub(feats, "apic")
    if s["os"] == "windows":
        hv = _sub(feats, "hyperv", mode="custom")
        _sub(hv, "relaxed", state="on")
        _sub(hv, "vapic", state="on")
        _sub(hv, "spinlocks", state="on", retries=8191)
    if s["secure_boot"]:
        _sub(feats, "smm", state="on")
    cpu_attrs = {"host-model": {"mode": "host-model", "check": "partial"},
                 "host-passthrough": {"mode": "host-passthrough", "check": "none",
                                      "migratable": "on"},
                 "custom": {"mode": "custom", "match": "exact", "check": "partial"}}[cpu["mode"]]
    cpu_el = _sub(dom, "cpu", **cpu_attrs)
    if cpu["mode"] == "custom":
        _sub(cpu_el, "model", cpu["model"], fallback="forbid")
    _sub(cpu_el, "topology", sockets=cpu["sockets"], dies=1, clusters=1, cores=cpu["cores"],
         threads=cpu["threads"])
    if s["os"] == "windows":
        clock = _sub(dom, "clock", offset="localtime")
        _sub(clock, "timer", name="rtc", tickpolicy="catchup")
        _sub(clock, "timer", name="pit", tickpolicy="delay")
        _sub(clock, "timer", name="hpet", present="no")
        _sub(clock, "timer", name="hypervclock", present="yes")
    else:
        _sub(dom, "clock", offset="utc")
    _sub(dom, "on_poweroff", s["on_poweroff"])
    _sub(dom, "on_reboot", s["on_reboot"])
    _sub(dom, "on_crash", s["on_crash"])
    devs = _sub(dom, "devices")
    io_n = 0
    for d, vol in zip(s["disks"], disk_vols):
        it = None
        if d["io_thread"]:
            io_n += 1
            it = io_n
        _disk_el(devs, d, vol, iothread=it)
    if seed:
        used = {d["target"] for d in s["disks"]}
        cbus = mo.get("cdrom_bus") or ("ide" if family == "pc" else "sata")
        _disk_el(devs, {"device": "cdrom", "bus": cbus, "readonly": True,
                        "target": _free_target(_TARGET_PREFIX[cbus], used)},
                 {"pool": seed["pool"], "volume": seed["volume"], "format": "raw"})
    if any(d["bus"] == "scsi" for d in s["disks"]):
        _sub(devs, "controller", type="scsi", model="virtio-scsi")
    if family == "q35":
        _sub(devs, "controller", type="usb", model="qemu-xhci")
        # index explicites : un contrôleur PCI sans index prend l'index 0,
        # réservé à pcie-root sur q35 (refus de libvirt vu en réel)
        _sub(devs, "controller", type="pci", index=0, model="pcie-root")
        for i in range(_pcie_ports_needed(s) + _int(s["hotplug_slots"])):
            _sub(devs, "controller", type="pci", index=i + 1, model="pcie-root-port")
    for n in s["nics"]:
        _nic_el(devs, n)
    if s["serial_console"]:
        ser = _sub(devs, "serial", type="pty")
        _sub(ser, "target", port=0)
        con = _sub(devs, "console", type="pty")
        _sub(con, "target", type="serial", port=0)
    if s["guest_agent"]:
        ch = _sub(devs, "channel", type="unix")
        _sub(ch, "target", type="virtio", name=GUEST_AGENT_CHANNEL)
    if s["tablet"]:
        _sub(devs, "input", type="tablet", bus="usb")
    if s["display"]["vnc"]:
        g = _sub(devs, "graphics", type="vnc", port=-1, autoport="yes",
                 passwd=vnc_password if (vnc_password and s["display"]["password"]) else None)
        _sub(g, "listen", type="address", address="127.0.0.1")
    video = _sub(devs, "video")
    _sub(video, "model", type=s["display"]["video"])
    if s["watchdog"] not in (None, False):
        _sub(devs, "watchdog", model="itco" if family == "q35" else "i6300esb",
             action=s["watchdog"])
    _sub(devs, "memballoon", model="virtio" if s["memory"]["balloon"] else "none")
    if s["rng"]:
        rng = _sub(devs, "rng", model="virtio")
        _sub(rng, "backend", "/dev/urandom", model="random")
    if s["tpm"]:
        tpm = _sub(devs, "tpm", model=mo.get("tpm_model") or "tpm-crb")
        _sub(tpm, "backend", type="emulator", version="2.0")
    ET.indent(dom, space="  ")
    return ET.tostring(dom, encoding="unicode") + "\n"


def device_xml(kind, item):
    """Un `<disk>` ou un `<interface>` seul, pour `attach-device` et
    `update-device`. `item` disque : {target, device, bus, pool, volume,
    format, cache, serial, readonly, shareable, boot_order} ; carte : {kind,
    source, mode, model, mac, boot_order}."""
    holder = ET.Element("holder")
    if kind == "disk":
        d = dict(item)
        if not isinstance(d.get("target"), str) or not _TARGET_RE.match(d["target"]) \
                or d.get("bus") not in DISK_BUSES:
            raise ko.OpError("invalid", "a disk needs a valid target and bus")
        vol = None
        if d.get("volume"):
            reasons = []
            _safe_name(d.get("pool"), "pool", reasons)
            _safe_name(d.get("volume"), "volume", reasons)
            if reasons:
                raise ko.OpError("invalid", "; ".join(reasons), {"refusals": reasons})
            vol = {"pool": d["pool"], "volume": d["volume"], "format": d.get("format") or "raw"}
        el = _disk_el(holder, d, vol)
    elif kind in ("interface", "nic"):
        n = dict(item)
        n.setdefault("kind", "network")
        n.setdefault("model", "virtio")
        reasons = []
        _check_nics({"nics": [n]}, {}, reasons)
        if reasons:
            raise ko.OpError("invalid", "; ".join(reasons), {"refusals": reasons})
        el = _nic_el(holder, n)
    else:
        raise ko.OpError("invalid", f"unknown device kind: {_show(kind)}")
    ET.indent(el, space="  ")
    return ET.tostring(el, encoding="unicode")


# --- lecture : la VM vue par le formulaire -----------------------------------

def _parse(xml_text):
    try:
        ko._register_namespaces(xml_text)
        return kh._safe_xml(xml_text)
    except (ET.ParseError, ValueError, TypeError) as exc:
        raise ko.OpError("failed", f"unreadable domain XML: {exc.__class__.__name__}") from None


def _mib(el):
    kib = kh._to_kib(el)
    return None if kib is None else kib // 1024


def _bool_child(el, tag):
    return el is not None and el.find(tag) is not None


def _disk_view(d):
    src = d.find("source")
    tgt = d.find("target")
    drv = d.find("driver")
    boot = d.find("boot")
    source = None
    if src is not None:
        source = {k: v for k, v in src.attrib.items() if k in (
            "pool", "volume", "file", "dev", "protocol", "name", "dir")} or None
    return {
        "target": tgt.get("dev") if tgt is not None else None,
        "device": d.get("device") or "disk",
        "type": d.get("type"),
        "bus": tgt.get("bus") if tgt is not None else None,
        "boot_order": _int(boot.get("order")) if boot is not None else None,
        "source": source,
        "format": drv.get("type") if drv is not None else None,
        "cache": drv.get("cache") if drv is not None else None,
        "serial": d.findtext("serial"),
        "readonly": d.find("readonly") is not None,
        "shareable": d.find("shareable") is not None,
        "io_thread": drv is not None and drv.get("iothread") is not None,
    }


def _nic_view(i):
    src = i.find("source")
    kind = i.get("type")
    source = None
    if src is not None:
        source = src.get({"network": "network", "bridge": "bridge"}.get(kind, "dev"))
    boot = i.find("boot")
    mac = i.find("mac")
    model = i.find("model")
    return {"kind": kind, "source": source,
            "mode": src.get("mode") if (src is not None and kind == "direct") else None,
            "model": model.get("type") if model is not None else None,
            "mac": mac.get("address").lower() if mac is not None and mac.get("address") else None,
            "boot_order": _int(boot.get("order")) if boot is not None else None}


def _hops(root):
    return root.find(f"metadata/{{{HOPS_NS}}}vm")


_UNMANAGED_TOP = ("cputune", "numatune", "memoryBacking", "seclabel", "resource", "blkiotune",
                  "memtune", "keywrap", "launchSecurity", "idmap", "perf", "pm")
_MANAGED_DEVICES = ("emulator", "disk", "controller", "interface", "serial", "console",
                    "channel", "input", "graphics", "video", "watchdog", "memballoon", "rng",
                    "tpm", "audio")


def _free_root_ports(root):
    """Ports pcie-root-port qu'aucun appareil n'occupe (bus de leurs index
    absent des adresses PCI). Sans adresses (XML jamais défini), tous."""
    devs = root.find("devices")
    if devs is None:
        return 0
    ports = [c for c in devs.findall("controller")
             if c.get("type") == "pci" and c.get("model") == "pcie-root-port"]
    used = set()
    for el in devs.iter("address"):
        if el.get("type") == "pci" and el.get("bus"):
            try:
                used.add(int(el.get("bus"), 0))
            except ValueError:
                pass
    free = 0
    for c in ports:
        idx = _int(c.get("index"))
        if idx is None or idx not in used:
            free += 1
    return free


def managed_view(xml_text):
    """La VM vue par le formulaire : les champs du spec qu'il sait relire,
    plus `unmanaged` (réglages gardés tels quels). Le mot de passe VNC n'y
    est jamais : `display.password` dit seulement s'il existe."""
    root = _parse(xml_text) if isinstance(xml_text, str) else xml_text
    hops = _hops(root)
    os_el = root.find("os")
    type_el = os_el.find("type") if os_el is not None else None
    machine = type_el.get("machine") if type_el is not None else None
    fw_auto = os_el is not None and os_el.get("firmware") == "efi"
    loader = os_el.find("loader") if os_el is not None else None
    firmware = "efi" if fw_auto or (loader is not None and loader.get("type") == "pflash") else "bios"
    secure = False
    if fw_auto:
        for f in os_el.findall("firmware/feature"):
            if f.get("name") == "secure-boot" and f.get("enabled") == "yes":
                secure = True
    elif loader is not None and loader.get("secure") == "yes":
        secure = True
    vcpu = root.find("vcpu")
    max_vcpus = _int(vcpu.text) if vcpu is not None else None
    cur = _int(vcpu.get("current")) if vcpu is not None and vcpu.get("current") else max_vcpus
    cpu_el = root.find("cpu")
    topo = cpu_el.find("topology") if cpu_el is not None else None
    mode = cpu_el.get("mode") if cpu_el is not None else None
    if mode is None and cpu_el is not None and cpu_el.find("model") is not None:
        mode = "custom"
    mem_max = _mib(root.find("memory"))
    mem_cur = _mib(root.find("currentMemory")) or mem_max
    devs = root.find("devices")
    if devs is None:
        devs = ET.Element("devices")
    graphics = [g for g in devs.findall("graphics") if g.get("type") == "vnc"]
    video = devs.find("video/model")
    balloon = devs.find("memballoon")
    wd = devs.find("watchdog")
    tpm = devs.find("tpm")
    serial_entry = None
    for e in root.findall("sysinfo/system/entry"):
        if e.get("name") == "serial":
            serial_entry = e.text
    seed = None
    if hops is not None:
        s_el = hops.find(f"{{{HOPS_NS}}}seed")
        if s_el is not None:
            seed = {"pool": s_el.get("pool"), "volume": s_el.get("volume")}
    os_type = hops.findtext(f"{{{HOPS_NS}}}os") if hops is not None else None
    if os_type is None:
        os_type = "windows" if root.find("features/hyperv") is not None else None
    sockets = _int(topo.get("sockets")) if topo is not None else max_vcpus
    cores = _int(topo.get("cores")) if topo is not None else 1
    threads = _int(topo.get("threads")) if topo is not None else 1
    unmanaged = [t for t in _UNMANAGED_TOP if root.find(t) is not None]
    if cpu_el is not None and cpu_el.find("numa") is not None:
        unmanaged.append("cpu/numa")
    for child in devs:
        if child.tag not in _MANAGED_DEVICES and f"devices/{child.tag}" not in unmanaged:
            unmanaged.append(f"devices/{child.tag}")
    meta = root.find("metadata")
    if meta is not None:
        for child in meta:
            if not child.tag.startswith(f"{{{HOPS_NS}}}"):
                unmanaged.append(f"metadata:{child.tag.split('}')[0].lstrip('{')}")
    for child in root:
        if child.tag.startswith("{"):        # qemu:commandline et autres
            unmanaged.append(child.tag.split("}", 1)[1] if "}" in child.tag else child.tag)
    return {
        "name": root.findtext("name"),
        "uuid": root.findtext("uuid"),
        "title": root.findtext("title"),
        "description": root.findtext("description"),
        "os": os_type,
        "machine": machine,
        "machine_family": kvm_caps.machine_family(machine),
        "firmware": firmware,
        "firmware_auto": fw_auto,
        "secure_boot": secure,
        "tpm": tpm is not None,
        "cpu": {"mode": mode,
                "model": (cpu_el.findtext("model") if cpu_el is not None and mode == "custom" else None),
                "vcpus": cur, "max_vcpus": max_vcpus,
                "sockets": sockets, "cores": cores, "threads": threads,
                "hotplug": bool((cur or 0) < (max_vcpus or 0) or (mem_cur or 0) < (mem_max or 0))},
        "memory": {"current_mib": mem_cur, "max_mib": mem_max,
                   "balloon": balloon is not None and balloon.get("model") not in (None, "none")},
        "disks": [_disk_view(d) for d in devs.findall("disk")],
        "nics": [_nic_view(i) for i in devs.findall("interface")],
        "display": {"vnc": bool(graphics), "password": any(g.get("passwd") for g in graphics),
                    "video": video.get("type") if video is not None else None},
        "guest_agent": any(c.find("target") is not None
                           and c.find("target").get("name") == GUEST_AGENT_CHANNEL
                           for c in devs.findall("channel")),
        "serial_console": devs.find("serial") is not None or devs.find("console") is not None,
        "tablet": any(i.get("type") == "tablet" for i in devs.findall("input")),
        "rng": devs.find("rng") is not None,
        "watchdog": wd.get("action") if wd is not None else None,
        "hotplug_slots": _free_root_ports(root),
        "on_poweroff": root.findtext("on_poweroff"),
        "on_reboot": root.findtext("on_reboot"),
        "on_crash": root.findtext("on_crash"),
        "sysinfo_serial": serial_entry,
        "seed": seed,
        "unmanaged": unmanaged,
    }


_SKIP = ("unmanaged", "name", "uuid", "seed", "machine_family", "firmware_auto")


def _flatten(view, prefix=""):
    out = {}
    for k, v in (view or {}).items():
        if not prefix and k in _SKIP:
            continue
        key = f"{prefix}{k}"
        if k == "disks" and not prefix and isinstance(v, list):
            for d in v:
                out.update(_flatten({x: y for x, y in d.items() if x != "target"},
                                    f"disks[{d.get('target')}]."))
        elif k == "nics" and not prefix and isinstance(v, list):
            for i, n in enumerate(v):
                ident = n.get("mac") or f"#{i + 1}"
                out.update(_flatten({x: y for x, y in n.items() if x != "mac"},
                                    f"nics[{ident}]."))
        elif isinstance(v, dict) and k != "source":
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


# champs dont la valeur vive diffère par nature de la valeur inactive
_LIVE_ONLY = ("display.password", "hotplug_slots", "cpu.hotplug")


def diff(view_inactive, view_live):
    """Champs gérés dont la définition persistante diffère de la VM en
    marche : changements qui attendent le prochain démarrage (`pending`)."""
    a, b = _flatten(view_inactive), _flatten(view_live)
    if a.get("cpu.mode") == "host-model" and b.get("cpu.mode") == "custom":
        # libvirt développe host-model en modèle nommé dans le XML vivant (vu sur
        # node2 : `mode='custom'` + Broadwell-IBRS) : ce n'est pas un changement
        for k in ("cpu.mode", "cpu.model"):
            a.pop(k, None)
            b.pop(k, None)
    keys = sorted(set(a) | set(b))
    return [k for k in keys if k not in _LIVE_ONLY and a.get(k) != b.get(k)]


_DISK_FIELDS = ("bus", "cache", "serial", "readonly", "shareable", "boot_order", "io_thread")
_NIC_FIELDS = ("kind", "source", "mode", "model", "boot_order")


def changes(view_before, spec_after):
    """Changements demandés par un spec (partiel) par rapport à la vue :
    [{field, old, new}]. Seuls les champs présents dans le spec comptent ;
    `disks` et `nics`, s'ils sont donnés, sont des listes complètes (un
    disque absent est retiré, désigné par sa cible ; une carte par sa MAC)."""
    out = []
    before = _flatten(view_before)
    flat_after = _flatten({k: v for k, v in (spec_after or {}).items()
                           if k not in ("disks", "nics", "cloudinit", "name")})
    for key in sorted(flat_after):
        if before.get(key) != flat_after[key]:
            out.append({"field": key, "old": before.get(key), "new": flat_after[key]})
    if "disks" in (spec_after or {}):
        old = {d["target"]: d for d in view_before.get("disks", [])}
        new_targets = set()
        for i, d in enumerate(spec_after["disks"] or []):
            t = d.get("target")
            if t in old:
                new_targets.add(t)
                for f in _DISK_FIELDS:
                    if f in d and d[f] != old[t].get(f):
                        out.append({"field": f"disks[{t}].{f}", "old": old[t].get(f), "new": d[f]})
                src = d.get("source") or {}
                if old[t]["device"] == "cdrom" and src.get("kind") in ("iso", "volume", "empty"):
                    want = None if src["kind"] == "empty" else {
                        "pool": src.get("pool"), "volume": src.get("volume") or src.get("image")}
                    have = old[t].get("source")
                    have = {"pool": have.get("pool"), "volume": have.get("volume")} if have else None
                    if want != have:
                        out.append({"field": f"disks[{t}].source", "old": have, "new": want})
            else:
                out.append({"field": f"disks[{t or '+' + str(i + 1)}]", "old": None, "new": d})
        for t, d in old.items():
            if t not in new_targets:
                out.append({"field": f"disks[{t}]", "old": d, "new": None})
    if "nics" in (spec_after or {}):
        old = {n["mac"]: n for n in view_before.get("nics", []) if n.get("mac")}
        kept = set()
        for i, n in enumerate(spec_after["nics"] or []):
            mac = (n.get("mac") or "").lower() or None
            if mac in old:
                kept.add(mac)
                for f in _NIC_FIELDS:
                    if f in n and n[f] != old[mac].get(f):
                        out.append({"field": f"nics[{mac}].{f}", "old": old[mac].get(f), "new": n[f]})
            else:
                out.append({"field": f"nics[{mac or '+' + str(i + 1)}]", "old": None, "new": n})
        for mac, n in old.items():
            if mac not in kept:
                out.append({"field": f"nics[{mac}]", "old": n, "new": None})
    return out


def canonical_digest(xml_text):
    """sha256 du XML canonique (C14N 2.0, blancs de mise en forme retirés),
    `passwd` retiré AVANT le calcul : l'empreinte transmise à la page ne
    dépend jamais du mot de passe VNC."""
    root = _parse(xml_text)
    for el in root.iter():
        if "passwd" in el.attrib:
            del el.attrib["passwd"]
    raw = ET.tostring(root, encoding="unicode")
    canon = ET.canonicalize(xml_data=raw, strip_text=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def canonical(el_or_text):
    """Forme canonique (C14N 2.0, blancs retirés) d'un élément ou d'un XML."""
    raw = el_or_text if isinstance(el_or_text, str) else ET.tostring(el_or_text, encoding="unicode")
    return ET.canonicalize(xml_data=raw, strip_text=True)


# --- modifications ciblées --------------------------------------------------

class Edit:
    """Modifications ciblées sur l'arbre d'un XML INACTIF. Chaque méthode
    ne touche que ses éléments et leurs attributs ; `xml()` rend le
    document, `modified` dit s'il diffère (forme canonique) de l'original :
    une modification vide ne doit lancer aucun `define`."""

    def __init__(self, xml_text):
        self.root = _parse(xml_text)
        self._orig = canonical(self.root)

    # aides
    @property
    def modified(self):
        return canonical(self.root) != self._orig

    def xml(self):
        return ET.tostring(self.root, encoding="unicode")

    def _devices(self):
        devs = self.root.find("devices")
        if devs is None:
            devs = ET.SubElement(self.root, "devices")
        return devs

    def _child(self, parent, tag, before=()):
        """Enfant `tag` de `parent`, créé s'il manque, placé avant le premier
        des éléments `before` présents (ordre de libvirt)."""
        el = parent.find(tag)
        if el is not None:
            return el
        el = ET.Element(tag)
        for i, c in enumerate(list(parent)):
            if c.tag in before:
                parent.insert(i, el)
                return el
        parent.append(el)
        return el

    def _disk(self, target):
        for d in self._devices().findall("disk"):
            t = d.find("target")
            if t is not None and t.get("dev") == target:
                return d
        raise EditError("notfound", f"no disk with target {_show(target)}")

    def _nic(self, mac):
        mac = (mac or "").lower()
        for i in self._devices().findall("interface"):
            m = i.find("mac")
            if m is not None and (m.get("address") or "").lower() == mac:
                return i
        raise EditError("notfound", f"no network card with MAC {_show(mac)}")

    def _family(self):
        t = self.root.find("os/type")
        return kvm_caps.machine_family(t.get("machine") if t is not None else None)

    def _insert_device(self, el, after_tag):
        devs = self._devices()
        children = list(devs)
        idx = None
        for i, c in enumerate(children):
            if c.tag == after_tag:
                idx = i
        if idx is None:
            for i, c in enumerate(children):
                if c.tag != "emulator":
                    idx = i - 1
                    break
            else:
                idx = len(children) - 1
        devs.insert(idx + 1, el)

    # CPU et mémoire
    def set_vcpus(self, current, maximum, topology=None):
        cur, mx = _int(current), _int(maximum)
        if cur is None or mx is None or cur < 1 or cur > mx:
            raise EditError("invalid", "vCPUs: 1 <= current <= maximum")
        for pin in self.root.findall("cputune/vcpupin"):
            v = _int(pin.get("vcpu"))
            if v is not None and v >= mx:
                raise EditError("refused", f"vCPU pinning refers to vCPU {v}: lower the "
                                           "maximum outside the console")
        vcpu = self.root.find("vcpu")
        if vcpu is None:
            vcpu = self._child(self.root, "vcpu", before=("iothreads", "cputune", "os"))
            vcpu.set("placement", "static")
        vcpu.text = str(mx)
        if cur < mx:
            vcpu.set("current", str(cur))
        elif "current" in vcpu.attrib:
            del vcpu.attrib["current"]
        cpu = self.root.find("cpu")
        topo = cpu.find("topology") if cpu is not None else None
        if topology:
            if cpu is None:
                cpu = self._child(self.root, "cpu", before=("clock", "on_poweroff"))
            topo = cpu.find("topology")
            if topo is None:
                topo = ET.Element("topology")
                cpu.insert(len([c for c in cpu if c.tag in ("model", "vendor")]), topo)
            for k in ("sockets", "cores", "threads"):
                v = _int(topology.get(k))
                if v is None or v < 1:
                    raise EditError("invalid", f"topology {k} must be a positive integer")
                topo.set(k, str(v))
        if topo is not None:
            product = 1
            for k in ("sockets", "dies", "clusters", "cores", "threads"):
                product *= _int(topo.get(k)) or 1
            if product != mx:
                raise EditError("invalid", f"the topology ({product}) must equal the maximum "
                                           f"vCPUs ({mx})")

    def set_memory(self, current_mib, max_mib):
        cur, mx = _int(current_mib), _int(max_mib)
        if cur is None or mx is None or cur < 1 or cur > mx:
            raise EditError("invalid", "memory: 1 <= current <= maximum (MiB)")
        mem = self._child(self.root, "memory", before=("currentMemory", "vcpu"))
        mem.attrib.clear()
        mem.set("unit", "KiB")
        mem.text = str(mx * 1024)
        curm = self._child(self.root, "currentMemory", before=("blkiotune", "memtune",
                                                               "memoryBacking", "vcpu"))
        curm.attrib.clear()
        curm.set("unit", "KiB")
        curm.text = str(cur * 1024)

    def set_cpu(self, mode, model=None):
        if mode not in CPU_MODES:
            raise EditError("invalid", f"unknown CPU mode {_show(mode)}")
        if mode == "custom" and (not isinstance(model, str) or not model or not xml_safe(model)):
            raise EditError("invalid", "a custom CPU needs a model")
        cpu = self._child(self.root, "cpu", before=("clock", "on_poweroff", "devices"))
        cpu.set("mode", mode)
        m = cpu.find("model")
        if mode == "custom":
            cpu.set("match", cpu.get("match") or "exact")
            # refus de libvirt vu en réel : migratable seulement en host-passthrough
            cpu.attrib.pop("migratable", None)
            if m is None:
                m = ET.Element("model")
                cpu.insert(0, m)
            m.attrib.clear()
            m.set("fallback", "forbid")
            m.text = model
        else:
            if m is not None:
                cpu.remove(m)
            cpu.attrib.pop("match", None)
            if mode == "host-passthrough":
                cpu.set("migratable", cpu.get("migratable") or "on")
            else:
                cpu.attrib.pop("migratable", None)

    def set_machine(self, machine):
        t = self.root.find("os/type")
        if t is None:
            raise EditError("failed", "the domain has no <os><type>")
        new_family = kvm_caps.machine_family(machine)
        if new_family is None or not xml_safe(machine):
            raise EditError("invalid", f"unknown machine type {_show(machine)}")
        if new_family != kvm_caps.machine_family(t.get("machine")):
            raise EditError("refused", REASON_FAMILY)
        t.set("machine", machine)

    def _fw_state(self):
        os_el = self.root.find("os")
        auto = os_el is not None and os_el.get("firmware") == "efi"
        loader = os_el.find("loader") if os_el is not None else None
        hops = _hops(self.root)
        explicit = hops is not None and hops.findtext(f"{{{HOPS_NS}}}firmware") == "explicit"
        hand = loader is not None and not auto and not explicit
        return os_el, auto, explicit, hand

    def set_firmware(self, kind, secure_boot=False, efi=None):
        """`kind` bios ou efi. Les `<loader>`/`<nvram>` ne sont retirés que
        s'ils viennent d'une sélection automatique (libvirt les a dépliés)
        ou d'un repli posé par la console (`<hops:firmware>explicit`).
        `efi` : options de la machine (kvm_caps) si la sélection automatique
        manque."""
        if kind not in FIRMWARES:
            raise EditError("invalid", f"unknown firmware {_show(kind)}")
        os_el, auto, explicit, hand = self._fw_state()
        if os_el is None:
            raise EditError("failed", "the domain has no <os>")
        if hand:
            raise EditError("refused", REASON_FIRMWARE_HAND)
        if secure_boot and kind != "efi":
            raise EditError("invalid", "Secure Boot needs UEFI firmware")
        if secure_boot and self._family() != "q35":
            raise EditError("refused", kvm_caps.REASON_SECURE_PC)
        for tag in ("loader", "nvram", "firmware"):
            for el in os_el.findall(tag):
                os_el.remove(el)
        os_el.attrib.pop("firmware", None)
        hops = _hops(self.root)
        if hops is not None:
            for el in hops.findall(f"{{{HOPS_NS}}}firmware"):
                hops.remove(el)
        if kind == "efi":
            if efi and not efi.get("autoselect"):
                loader = efi.get("secure_loader") if secure_boot else efi.get("loader")
                if not loader:
                    raise EditError("refused", kvm_caps.REASON_NO_SECURE if secure_boot
                                    else kvm_caps.REASON_NO_EFI)
                pos = self._os_insert_pos(os_el)
                tmpl = efi.get("secure_nvram_template") if secure_boot else efi.get("nvram_template")
                for i, el in enumerate(_explicit_firmware(loader, tmpl, secure_boot)):
                    os_el.insert(pos + i, el)
                self._set_hops("firmware", "explicit")
            else:
                os_el.set("firmware", "efi")
                fw = ET.Element("firmware")
                ET.SubElement(fw, "feature", enabled="yes" if secure_boot else "no",
                              name="secure-boot")
                if secure_boot:
                    ET.SubElement(fw, "feature", enabled="yes", name="enrolled-keys")
                os_el.insert(self._os_insert_pos(os_el), fw)
            if secure_boot:
                feats = self._child(self.root, "features", before=("cpu", "clock", "on_poweroff"))
                smm = feats.find("smm")
                if smm is None:
                    smm = ET.SubElement(feats, "smm")
                smm.set("state", "on")

    @staticmethod
    def _os_insert_pos(os_el):
        children = list(os_el)
        for i, c in enumerate(children):
            if c.tag not in ("type",):
                return i
        return len(children)

    def set_tpm(self, enabled, model="tpm-crb"):
        devs = self._devices()
        tpms = devs.findall("tpm")
        for t in tpms:
            b = t.find("backend")
            if b is None or b.get("type") != "emulator":
                raise EditError("refused", REASON_TPM_HAND)
        if not enabled:
            for t in tpms:
                devs.remove(t)
            return
        if tpms:
            return
        t = ET.Element("tpm", model=model or "tpm-crb")
        ET.SubElement(t, "backend", type="emulator", version="2.0")
        self._insert_device(t, "rng")

    # disques
    def _targets(self):
        out = set()
        for d in self._devices().findall("disk"):
            t = d.find("target")
            if t is not None and t.get("dev"):
                out.add(t.get("dev"))
        return out

    def add_disk(self, item):
        """Ajoute un `<disk type='volume'>` (item comme device_xml, cible
        attribuée si absente). Rend la cible."""
        d = dict(item)
        if d.get("bus") not in DISK_BUSES:
            raise EditError("invalid", f"unknown bus {_show(d.get('bus'))}")
        if d["bus"] == "ide" and self._family() == "q35":
            raise EditError("invalid", "the q35 machine type has no IDE bus")
        used = self._targets()
        if not d.get("target"):
            d["target"] = _free_target(_TARGET_PREFIX[d["bus"]], used)
        elif d["target"] in used:
            raise EditError("invalid", f"two disks use the target {d['target']}")
        el = ET.fromstring(device_xml("disk", d))
        self._insert_device(el, "disk")
        if d["bus"] == "scsi" and not any(c.get("type") == "scsi"
                                          for c in self._devices().findall("controller")):
            ctl = ET.Element("controller", type="scsi", model="virtio-scsi")
            self._insert_device(ctl, "disk")
        return d["target"]

    def remove_disk(self, target):
        self._devices().remove(self._disk(target))

    def set_disk(self, target, bus=None, cache=None, readonly=None, shareable=None,
                 serial=None):
        """Bus, cache, lecture seule, partage, numéro de série d'un disque.
        Un changement de bus renomme la cible (vd* <-> sd*) et retire
        l'adresse ; rend la cible (nouvelle ou inchangée)."""
        d = self._disk(target)
        tgt = d.find("target")
        if bus is not None and bus != tgt.get("bus"):
            if bus not in DISK_BUSES:
                raise EditError("invalid", f"unknown bus {_show(bus)}")
            if bus == "ide" and self._family() == "q35":
                raise EditError("invalid", "the q35 machine type has no IDE bus")
            if d.get("device") == "cdrom" and bus == "virtio":
                raise EditError("invalid", "a CD-ROM cannot use the virtio bus")
            tgt.set("bus", bus)
            if _TARGET_PREFIX[bus] != target[:2]:
                used = self._targets() - {target}
                tgt.set("dev", _free_target(_TARGET_PREFIX[bus], used))
            addr = d.find("address")
            if addr is not None:
                d.remove(addr)
            drv = d.find("driver")
            if bus != "virtio" and drv is not None:
                drv.attrib.pop("iothread", None)
            if bus == "scsi" and not any(c.get("type") == "scsi"
                                         for c in self._devices().findall("controller")):
                self._insert_device(ET.Element("controller", type="scsi", model="virtio-scsi"),
                                    "disk")
        if cache is not None:
            if cache not in CACHE_MODES:
                raise EditError("invalid", f"unknown cache mode {_show(cache)}")
            drv = d.find("driver")
            if drv is None:
                drv = ET.Element("driver", name="qemu")
                d.insert(0, drv)
            drv.set("cache", cache)
        for flag, tag in ((readonly, "readonly"), (shareable, "shareable")):
            if flag is None:
                continue
            el = d.find(tag)
            if flag and el is None:
                ET.SubElement(d, tag)
            elif not flag and el is not None:
                d.remove(el)
        if serial is not None:
            el = d.find("serial")
            if serial == "":
                if el is not None:
                    d.remove(el)
            else:
                if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,20}", serial):
                    raise EditError("invalid", "invalid serial number")
                if el is None:
                    el = ET.SubElement(d, "serial")
                el.text = serial
        return tgt.get("dev")

    # cartes
    def add_nic(self, item):
        n = dict(item)
        if n.get("mac"):
            n["mac"] = n["mac"].lower()
            for i in self._devices().findall("interface"):
                m = i.find("mac")
                if m is not None and (m.get("address") or "").lower() == n["mac"]:
                    raise EditError("invalid", f"MAC address used twice: {n['mac']}")
        el = ET.fromstring(device_xml("interface", n))
        self._insert_device(el, "interface")

    def remove_nic(self, mac):
        self._devices().remove(self._nic(mac))

    def set_boot_order(self, mapping):
        """{cible ou MAC: ordre ou None}. Retire `<os><boot dev>` dès qu'un
        ordre par périphérique est demandé (libvirt refuse les deux)."""
        wanted = False
        for key, order in (mapping or {}).items():
            el = self._nic(key) if _MAC_RE.match(key or "") else self._disk(key)
            b = el.find("boot")
            if order is None:
                if b is not None:
                    el.remove(b)
                continue
            o = _int(order)
            if o is None or o < 1:
                raise EditError("invalid", f"invalid boot order {_show(order)}")
            wanted = True
            if b is None:
                b = ET.Element("boot")
                tgt = el.find("target") if el.tag == "disk" else el.find("model")
                pos = list(el).index(tgt) + 1 if tgt is not None else len(el)
                el.insert(pos, b)
            b.attrib.clear()
            b.set("order", str(o))
        orders = [b.get("order") for b in self._devices().iter("boot")]
        dup = {o for o in orders if orders.count(o) > 1}
        if dup:
            raise EditError("invalid", f"two devices use boot order {sorted(dup)[0]}")
        if wanted:
            os_el = self.root.find("os")
            for b in (os_el.findall("boot") if os_el is not None else []):
                os_el.remove(b)

    # affichage
    def set_display(self, vnc, password=None):
        """`vnc` : vrai pour avoir un affichage VNC en boucle locale.
        `password` : texte = posé, False = retiré, None = inchangé."""
        devs = self._devices()
        graphics = [g for g in devs.findall("graphics") if g.get("type") == "vnc"]
        if not vnc:
            for g in graphics:
                devs.remove(g)
            return
        if not graphics:
            g = ET.Element("graphics", type="vnc", port="-1", autoport="yes")
            ET.SubElement(g, "listen", type="address", address="127.0.0.1")
            self._insert_device(g, "graphics" if devs.find("graphics") is not None else "input")
            graphics = [g]
        if password is False:
            for g in graphics:
                g.attrib.pop("passwd", None)
        elif password is not None:
            if not isinstance(password, str) or not re.fullmatch(r"[\x21-\x7e]{1,8}", password):
                raise EditError("invalid", "invalid VNC password (8 printable characters at most)")
            for g in graphics:
                g.set("passwd", password)

    def set_video(self, model):
        if model not in VIDEO_MODELS:
            raise EditError("invalid", f"unknown video model {_show(model)}")
        devs = self._devices()
        video = devs.find("video")
        if video is None:
            video = ET.Element("video")
            ET.SubElement(video, "model")
            self._insert_device(video, "graphics")
        m = video.find("model")
        if m is None:
            m = ET.SubElement(video, "model")
        if m.get("type") == model:
            return
        primary = m.get("primary")
        for c in list(m):
            m.remove(c)
        m.attrib.clear()
        m.set("type", model)
        if primary:
            m.set("primary", primary)

    # textes et cycle de vie
    def _set_text(self, tag, value, before):
        el = self.root.find(tag)
        if value in (None, ""):
            if el is not None:
                self.root.remove(el)
            return
        el = self._child(self.root, tag, before=before)
        el.text = value

    def set_title(self, title):
        if title not in (None, "") and (not xml_safe(title) or len(title) > TITLE_MAX):
            raise EditError("invalid", f"invalid title: one line, {TITLE_MAX} characters at most")
        self._set_text("title", title, ("description", "metadata", "maxMemory", "memory"))

    def set_description(self, text):
        if text not in (None, "") and (not xml_safe(text, multiline=True)
                                       or len(text) > ko.DESCRIPTION_MAX):
            raise EditError("invalid", f"invalid description ({ko.DESCRIPTION_MAX} characters at most)")
        self._set_text("description", text, ("metadata", "maxMemory", "memory"))

    def set_lifecycle(self, on_poweroff=None, on_reboot=None, on_crash=None):
        for tag, value, allowed in (("on_poweroff", on_poweroff, LIFECYCLE),
                                    ("on_reboot", on_reboot, LIFECYCLE),
                                    ("on_crash", on_crash, CRASH_ACTIONS)):
            if value is None:
                continue
            if value not in allowed:
                raise EditError("invalid", f"unknown {tag} action {_show(value)}")
            order = ("on_poweroff", "on_reboot", "on_crash")
            before = order[order.index(tag) + 1:] + ("on_lockfailure", "pm", "perf", "devices")
            self._child(self.root, tag, before=before).text = value

    # métadonnées de la console
    def _hops_el(self):
        hops = _hops(self.root)
        if hops is None:
            meta = self._child(self.root, "metadata", before=("maxMemory", "memory"))
            ET.register_namespace(HOPS_PREFIX, HOPS_NS)
            hops = ET.SubElement(meta, f"{{{HOPS_NS}}}vm")
        return hops

    def _set_hops(self, tag, text):
        hops = self._hops_el()
        el = hops.find(f"{{{HOPS_NS}}}{tag}")
        if el is None:
            el = ET.SubElement(hops, f"{{{HOPS_NS}}}{tag}")
        el.text = text

    def set_metadata_os(self, os_type):
        if os_type not in OS_TYPES:
            raise EditError("invalid", f"unknown operating system {_show(os_type)}")
        self._set_hops("os", os_type)

    def set_seed(self, pool, volume):
        """Volume cloud-init possédé par la VM ; volume None = oublié."""
        hops = _hops(self.root)
        if volume is None:
            if hops is not None:
                for el in hops.findall(f"{{{HOPS_NS}}}seed"):
                    hops.remove(el)
            return
        reasons = []
        _safe_name(pool, "pool", reasons)
        _safe_name(volume, "volume", reasons)
        if reasons:
            raise EditError("invalid", "; ".join(reasons))
        hops = self._hops_el()
        el = hops.find(f"{{{HOPS_NS}}}seed")
        if el is None:
            el = ET.SubElement(hops, f"{{{HOPS_NS}}}seed")
        el.attrib.clear()
        el.set("pool", pool)
        el.set("volume", volume)
