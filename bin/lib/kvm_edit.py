"""Modification d'une VM d'un hôte KVM et gestes à chaud (prochaine version KVM).

La modification relit `dumpxml --inactive --security-info`, applique des
changements ciblés (kvm_xml.Edit) et redéfinit : tout ce que le formulaire
ne gère pas reste octet pour octet. Chaque changement a une règle fixe
(HOTNESS) : appliqué maintenant (`--live --config`), au prochain démarrage
(un seul `define --validate` du XML inactif), ou refusé avec sa raison.

Règles :
- l'empreinte (`canonical_digest` du XML inactif ENTIER, pas seulement des
  champs gérés) lue à l'ouverture de la fenêtre est revérifiée au plan
  recalculé juste avant d'écrire : tout changement venu d'ailleurs depuis
  l'ouverture, sur n'importe quel champ, donne OpError `changed`. Reste une
  fenêtre entre ce contrôle et le `define` (les gestes à chaud passent
  avant, jusqu'à 30 s pour un retrait de disque) : le `define` part du XML
  relu à ce moment-là et n'y change que les champs modifiés, donc un
  changement extérieur sur un AUTRE champ est gardé, mais un changement
  extérieur sur le MÊME champ géré est écrasé par la valeur du formulaire ;
- les écritures ne sont jamais rejouées ; après une coupure du transport
  pendant une écriture, le changement est dit `uncertain` et le reste n'est
  pas tenté ;
- pas de transaction : une erreur au milieu laisse les changements déjà
  faits, nommés dans le résultat, jamais défaits automatiquement ;
- un retrait de disque ne supprime jamais son volume ;
- un volume créé pour un disque ajouté (à chaud ou au prochain démarrage)
  est retiré si son attache ou le `define` échoue ; quand l'écriture est
  incertaine (coupure du transport), il est gardé et nommé dans `leftovers`.
"""

import copy
import time
import uuid as uuidlib
import xml.etree.ElementTree as ET

import kvm_create as kc
import kvm_host as kh
import kvm_ops as ko
import kvm_storage as ks
import kvm_xml as kx

OpError = ko.OpError

DETACH_WAIT = 30.0
DETACH_POLL = 1.0
LIVE_STATES = ("running", "idle", "blocked")
REASON_TRANSIENT = ("transient VM: its definition disappears at shutdown, so there is nothing "
                    "to edit; make it persistent first")
REASON_TRANSIENT_NEXT = "transient VM: nothing persists after shutdown"
REASON_UNSUPPORTED = "not changeable by the console in this version"
REASON_EMPTY_USER_DATA = ("an empty user-data would erase the current one: send clear to confirm "
                          "that it must be emptied")
REASON_NOT_DONE = "not attempted: an earlier change failed"
REASON_UNCERTAIN = ("the connection was lost during the write: reload the VM to see whether "
                    "it was applied")
REASON_DETACH_PENDING = ("the guest has not released the disk yet: it is removed from the "
                         "configuration and will be gone at next boot")
REASON_RESTART = ("applied at the next start: a reboot inside the guest is not enough, the VM "
                  "must be shut down then started")

# Règle de chaque champ (servie à la console avec le formulaire). `running` :
# ce qui se passe quand la VM tourne ; une VM arrêtée applique tout par un
# seul `define` (sauf renommage, autostart et média de CD-ROM).
HOTNESS = (
    {"field": "cpu.vcpus", "running": "now", "command": "setvcpus --live --config --count N",
     "note": "up to the maximum; a decrease waits for the next start if QEMU refuses it"},
    {"field": "memory.current_mib", "running": "now", "command": "setmem --live --config",
     "note": "up to the maximum, through the balloon driver of the guest"},
    {"field": "cpu.max_vcpus", "running": "next-boot", "command": "define"},
    {"field": "cpu.sockets", "running": "next-boot", "command": "define"},
    {"field": "cpu.cores", "running": "next-boot", "command": "define"},
    {"field": "cpu.threads", "running": "next-boot", "command": "define"},
    {"field": "cpu.mode", "running": "next-boot", "command": "define"},
    {"field": "cpu.model", "running": "next-boot", "command": "define"},
    {"field": "memory.max_mib", "running": "next-boot", "command": "define"},
    {"field": "machine", "running": "next-boot", "command": "define"},
    {"field": "firmware", "running": "next-boot", "command": "define"},
    {"field": "secure_boot", "running": "next-boot", "command": "define"},
    {"field": "tpm", "running": "next-boot", "command": "define"},
    {"field": "display.vnc", "running": "next-boot", "command": "define"},
    {"field": "display.password", "running": "next-boot", "command": "define"},
    {"field": "display.video", "running": "next-boot", "command": "define"},
    {"field": "os", "running": "next-boot", "command": "define"},
    {"field": "hotplug_slots", "running": "next-boot", "command": "define"},
    {"field": "guest_agent", "running": "next-boot", "command": "define"},
    {"field": "tablet", "running": "next-boot", "command": "define"},
    {"field": "rng", "running": "next-boot", "command": "define"},
    {"field": "on_poweroff", "running": "next-boot", "command": "define"},
    {"field": "on_reboot", "running": "next-boot", "command": "define"},
    {"field": "on_crash", "running": "next-boot", "command": "define"},
    {"field": "disks[].bus", "running": "next-boot", "command": "define"},
    {"field": "disks[].cache", "running": "next-boot", "command": "define"},
    {"field": "disks[].serial", "running": "next-boot", "command": "define"},
    {"field": "disks[].readonly", "running": "next-boot", "command": "define"},
    {"field": "disks[].shareable", "running": "next-boot", "command": "define"},
    {"field": "disks[].boot_order", "running": "next-boot", "command": "define"},
    {"field": "disks[].source", "running": "now", "command": "update-device --live --config",
     "note": "CD-ROM media change or eject; the source of any other disk is not changeable "
             "(refused: remove the disk and add another one)"},
    {"field": "disks[+]", "running": "now", "command": "attach-device --live --config",
     "note": "virtio, or scsi with a SCSI controller, and a free PCIe slot on q35; SATA and "
             "CD-ROM drives wait for the next start"},
    {"field": "disks[-]", "running": "now", "command": "detach-device --live --config",
     "note": "virtio or scsi; the guest must release it within 30 s, the volume is kept"},
    {"field": "nics[+]", "running": "now", "command": "attach-device --live --config",
     "note": "needs a free PCIe slot on q35"},
    {"field": "nics[-]", "running": "now", "command": "detach-interface --live --config"},
    {"field": "nics[].model", "running": "next-boot", "command": "define"},
    {"field": "nics[].kind", "running": "next-boot", "command": "define"},
    {"field": "nics[].source", "running": "next-boot", "command": "define"},
    {"field": "nics[].mode", "running": "next-boot", "command": "define"},
    {"field": "nics[].boot_order", "running": "next-boot", "command": "define"},
    {"field": "title", "running": "now", "command": "desc --live --config --title"},
    {"field": "description", "running": "now", "command": "desc --live --config"},
    {"field": "autostart", "running": "now", "command": "autostart [--disable]"},
    {"field": "cloudinit", "running": "next-boot", "command": "vol-upload + update-device",
     "note": "new seed ISO, read by the guest at its next start"},
    {"field": "name", "running": "refused", "command": "domrename",
     "note": "the VM must be shut off and have no snapshot"},
)
_UNSUPPORTED = ("memory.balloon", "serial_console", "watchdog", "sysinfo_serial")
_UNSUPPORTED_SUB = ("io_thread",)


def _rule(field):
    """Règle HOTNESS d'un champ (`disks[vdb].cache` -> `disks[].cache`)."""
    key = field
    if field.startswith(("disks[", "nics[")):
        head, _, rest = field.partition("]")
        base = head.split("[")[0]
        if not rest:
            key = f"{base}[?]"
        else:
            key = f"{base}[]{rest}"
    for r in HOTNESS:
        if r["field"] == key:
            return r
    return None


def _flags(ctx):
    if ctx["live"] and ctx["persistent"]:
        return ["--live", "--config"]
    if ctx["live"]:
        return ["--live"]
    return ["--config"]


# --- lecture -----------------------------------------------------------------------

def read_vm(host, name):
    """XML inactif et vivant (avec --security-info), état, persistance,
    autostart, instantanés. OpError notfound si la VM n'existe pas."""
    ko.check_vm_name(name)
    out = host.batch({"info": ("dominfo", "--domain", name),
                      "inactive": ("dumpxml", "--inactive", "--security-info", "--domain", name),
                      "live": ("dumpxml", "--security-info", "--domain", name),
                      "snaps": ("snapshot-list", "--domain", name, "--name")}, idempotent=True)
    if not out["info"].ok and out["info"].error.kind == "notfound":
        raise OpError("notfound", f"{host.name}: domain {name} not found")
    info = kh.parse_dominfo(ko._need(out, "info"))
    inactive = ko._need(out, "inactive")
    live = ko._need(out, "live") if info["state"] in LIVE_STATES else None
    snaps = kh.parse_names(str(out["snaps"])) if out["snaps"].ok else []
    return {"info": info, "inactive": inactive, "live": live, "snapshots": snaps}


def vm_form(host, name):
    """Ce que la fenêtre « Modifier » lit : vue gérée du XML inactif,
    réglages non gérés, changements en attente (vivant contre inactif),
    règles HOTNESS, empreinte. Jamais de mot de passe : `display.password`
    est un booléen ; le cloud-init n'est pas lu ici."""
    vm = read_vm(host, name)
    view = kx.managed_view(vm["inactive"])
    pending = kx.diff(view, kx.managed_view(vm["live"])) if vm["live"] else []
    return {"name": name, "view": view, "unmanaged": view.get("unmanaged", []),
            "pending": pending, "restart_required": bool(pending), "hotness": list(HOTNESS),
            "config_digest": kx.canonical_digest(vm["inactive"]),
            "state": vm["info"]["state"], "persistent": vm["info"]["persistent"],
            "autostart": vm["info"].get("autostart"), "snapshots": len(vm["snapshots"]),
            "id": vm["info"].get("id")}


# --- plan --------------------------------------------------------------------------

def _scrub(value):
    """Valeur d'un changement rendue sans secret (cloud-init : présence et
    taille seulement)."""
    if isinstance(value, dict) and ("user_data" in value or "network_data" in value):
        return {k: (bool(v), len(v)) if isinstance(v, str) else v for k, v in value.items()
                if k in ("user_data", "network_data")} | {
            k: v for k, v in value.items() if k in ("rerun", "hostname", "clear")}
    return value


def _ctx(vm, options=None):
    info = vm["info"]
    live_view = kx.managed_view(vm["live"]) if vm["live"] else None
    inactive_view = kx.managed_view(vm["inactive"])
    root = kh._safe_xml(vm["live"] or vm["inactive"])
    scsi = any(c.get("type") == "scsi" for c in root.findall("devices/controller"))
    return {"state": info["state"], "live": info["state"] in LIVE_STATES,
            "persistent": info["persistent"], "autostart": info.get("autostart"),
            "family": inactive_view.get("machine_family"),
            "free_ports": (live_view or inactive_view).get("hotplug_slots") or 0,
            "live_view": live_view, "view": inactive_view, "scsi": scsi,
            "snapshots": vm["snapshots"], "options": options}


def _classify(ch, ctx, spec_after, gesture):
    """(applied, reason) d'un changement : `now`, `next-boot` ou `refused`.
    `ctx["free_ports"]` est consommé par les ajouts à chaud."""
    field, new = ch["field"], ch["new"]
    state = ctx["state"]
    if not ctx["persistent"] and not gesture:
        return "refused", REASON_TRANSIENT
    if state not in LIVE_STATES and state != "shut off":
        return "refused", f"the VM is {state}: resume it or shut it down first"
    sub = field.partition("].")[2] if "]." in field else None
    if field in _UNSUPPORTED or sub in _UNSUPPORTED_SUB or field == "cpu.hotplug":
        return "refused", REASON_UNSUPPORTED
    if field == "name":
        if ctx["live"]:
            return "refused", "rename: the VM must be shut off"
        if ctx["snapshots"]:
            return "refused", "rename: libvirt refuses to rename a VM that has snapshots"
        return "config", None
    if field.startswith("disks[") and field.endswith("].source"):
        target = field[len("disks["):field.index("]")]
        disk = next((d for d in ctx["view"].get("disks") or [] if d.get("target") == target), None)
        if not disk or disk.get("device") != "cdrom":
            return "refused", ("only the media of a CD-ROM drive can be changed: remove the disk "
                               "and add another one")
    live = ctx["live"]
    if not live:
        return "config", None
    persistent = ctx["persistent"]
    view = ctx["live_view"]
    target_max = ((spec_after.get("cpu") or {}).get("max_vcpus") or view["cpu"]["max_vcpus"])
    if field == "cpu.vcpus":
        if new > view["cpu"]["max_vcpus"]:
            if target_max >= new and persistent:
                return "next-boot", f"above the current maximum ({view['cpu']['max_vcpus']}): " \
                    + REASON_RESTART
            return "refused", f"above the maximum ({view['cpu']['max_vcpus']})"
        return "now", None
    if field == "memory.current_mib":
        mx = view["memory"]["max_mib"]
        if new > mx:
            tmax = (spec_after.get("memory") or {}).get("max_mib") or mx
            if tmax >= new and persistent:
                return "next-boot", f"above the current maximum ({mx} MiB): " + REASON_RESTART
            return "refused", f"above the maximum ({mx} MiB)"
        return "now", None
    if field in ("title", "description", "autostart"):
        if field == "autostart" and not persistent:
            return "refused", REASON_TRANSIENT_NEXT
        return "now", None
    if field.startswith("disks["):
        if field.endswith("].source"):
            return "now", None
        if ch["old"] is None and isinstance(new, dict):       # ajout
            device = new.get("device") or ("cdrom" if (new.get("source") or {}).get("kind")
                                           in ("iso", "empty") else "disk")
            bus = new.get("bus") or kx.OS_PRESETS.get(ctx["view"].get("os") or "linux",
                                                      kx.OS_PRESETS["linux"])["disk_bus"]
            if device == "cdrom" or bus in ("sata", "ide"):
                why = "a CD-ROM or SATA drive cannot be hot-plugged on QEMU"
                return ("next-boot", why) if persistent else ("refused", why)
            if bus == "scsi" and not ctx["scsi"]:
                why = "no SCSI controller in the running VM"
                return ("next-boot", why) if persistent else ("refused", why)
            if bus == "virtio" and ctx["family"] == "q35":
                if ctx["free_ports"] < 1:
                    why = "no free PCIe slot for hot-plug (raise the hot-plug slots, next start)"
                    return ("next-boot", why) if persistent else ("refused", why)
                ctx["free_ports"] -= 1
            return "now", None
        if new is None:                                        # retrait
            old = ch["old"] or {}
            if old.get("device") == "cdrom" or old.get("bus") in ("sata", "ide"):
                why = "a CD-ROM or SATA drive cannot be unplugged live on QEMU"
                return ("next-boot", why) if persistent else ("refused", why)
            return "now", None
    if field.startswith("nics["):
        if ch["old"] is None and isinstance(new, dict):
            model = new.get("model") or "virtio"
            if ctx["family"] == "q35":
                if model in ("e1000", "rtl8139"):
                    why = f"a {model} card needs a PCI bridge: added at the next start"
                    return ("next-boot", why) if persistent else ("refused", why)
                if ctx["free_ports"] < 1:
                    why = "no free PCIe slot for hot-plug (raise the hot-plug slots, next start)"
                    return ("next-boot", why) if persistent else ("refused", why)
                ctx["free_ports"] -= 1
            return "now", None
        if new is None:
            return "now", None
    if field == "cloudinit":
        return "next-boot", "the guest reads the new seed at its next start"
    if not persistent:
        return "refused", REASON_TRANSIENT_NEXT
    return "next-boot", REASON_RESTART


def edit_plan(host, name, spec, expect_digest=None, options=None, gesture=False, vm=None):
    """Plan d'une modification (lecture seule) : XML relus, empreinte
    contrôlée (OpError `changed`), changements calculés et classés. Rend
    {name, state, persistent, config_digest, changes: [{field, old, new,
    applied, reason}], refusals, empty}. Les champs `_` sont internes."""
    if not isinstance(spec, dict):
        raise OpError("invalid", "the spec must be an object")
    if expect_digest is not None:
        ko.check_digest(expect_digest)
    vm = vm or read_vm(host, name)
    digest = kx.canonical_digest(vm["inactive"])
    if expect_digest is not None and digest != expect_digest:
        raise OpError("changed", "the VM was changed since you opened it: reload")
    ctx = _ctx(vm, options)
    view = ctx["view"]
    body = {k: v for k, v in spec.items() if k not in ("name", "autostart", "cloudinit")}
    changes = kx.changes(view, body)
    if isinstance(spec.get("name"), str) and spec["name"] != name:
        changes.append({"field": "name", "old": name, "new": spec["name"]})
    if "autostart" in spec and bool(spec["autostart"]) != bool(ctx["autostart"]):
        changes.append({"field": "autostart", "old": bool(ctx["autostart"]),
                        "new": bool(spec["autostart"])})
    if spec.get("cloudinit") is not None:
        changes.append({"field": "cloudinit", "old": view.get("seed"),
                        "new": spec["cloudinit"]})
    # cpu.hotplug est déduit (courant < maximum), jamais un réglage
    changes = [c for c in changes if c["field"] != "cpu.hotplug"]
    refusals, blocking = [], []
    for ch in changes:
        applied, reason = _classify(ch, ctx, spec, gesture)
        if applied == "config":
            applied = "now"
        if ch["field"] == "cloudinit" and applied != "refused":
            why = empty_user_data_refusal(host, view.get("seed"), ch["new"])
            if why:
                applied, reason = "refused", why
        ch["applied"], ch["reason"] = applied, reason
        if applied == "refused":
            refusals.append(f"{ch['field']}: {reason}")
    _check_targets(changes, view, spec, blocking)
    refusals += blocking
    public = [{k: _scrub(v) if k in ("new", "old") else v for k, v in ch.items()}
              for ch in changes]
    return {"name": name, "state": ctx["state"], "persistent": ctx["persistent"],
            "config_digest": digest, "changes": public, "refusals": refusals,
            "empty": not changes, "_blocking": blocking, "_changes": changes, "_ctx": ctx, "_spec": spec, "_vm": vm}


def _check_targets(changes, view, spec, refusals):
    """Contrôles croisés : vCPU au-dessus du maximum visé, nouveau nom."""
    cpu = {**view["cpu"], **(spec.get("cpu") or {})}
    if isinstance(cpu.get("vcpus"), int) and isinstance(cpu.get("max_vcpus"), int) \
            and cpu["vcpus"] > cpu["max_vcpus"]:
        refusals.append(f"cpu.vcpus: {cpu['vcpus']} is above the maximum ({cpu['max_vcpus']})")
    mem = {**view["memory"], **(spec.get("memory") or {})}
    if isinstance(mem.get("current_mib"), int) and isinstance(mem.get("max_mib"), int) \
            and mem["current_mib"] > mem["max_mib"]:
        refusals.append(f"memory.current_mib: above the maximum ({mem['max_mib']} MiB)")
    for ch in changes:
        if ch["field"] == "name":
            try:
                ko.check_new_vm_name(ch["new"])
            except OpError as e:
                refusals.append(f"name: {e.message}")


# --- exécution ---------------------------------------------------------------------

def parse_memstat(text):
    """`virsh dommemstat` -> {clé: KiB} (lignes « actual 1048576 »)."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def _disk_element(xml_text, target):
    root = kh._safe_xml(xml_text)
    for d in root.findall("devices/disk"):
        t = d.find("target")
        if t is not None and t.get("dev") == target:
            return d
    raise OpError("notfound", f"no disk with target {target}")


def _media_xml(xml_text, target, pool=None, volume=None):
    """`<disk>` d'un lecteur de CD-ROM avec un nouveau média (pool/volume)
    ou sans (éjection), pour `update-device`."""
    el = _disk_element(xml_text, target)
    if el.get("device") != "cdrom":
        raise OpError("refused", f"{target} is not a CD-ROM drive")
    for tag in ("source", "backingStore"):
        for x in el.findall(tag):
            el.remove(x)
    if volume:
        el.set("type", "volume")
        src = ET.Element("source", pool=pool, volume=volume)
        drv = el.find("driver")
        el.insert(list(el).index(drv) + 1 if drv is not None else 0, src)
        if drv is not None:
            drv.set("type", "raw")
    return ET.tostring(el, encoding="unicode")


def _free_target(xml_texts, bus):
    used = set()
    for x in xml_texts:
        if x:
            for d in kh._safe_xml(x).findall("devices/disk/target"):
                if d.get("dev"):
                    used.add(d.get("dev"))
    return kx._free_target(kx._TARGET_PREFIX[bus], used)


def _new_disk(host, name, d, vm, ctx, created):
    """Crée le volume d'un disque ajouté (vierge ou depuis une image) et rend
    l'item de device_xml. Le volume créé est noté dans `created`."""
    d = copy.deepcopy(d)
    src = dict(d.get("source") or {})
    kind = src.get("kind") or "blank"
    device = d.get("device") or ("cdrom" if kind in ("iso", "empty") else "disk")
    preset = kx.OS_PRESETS.get(ctx["view"].get("os") or "linux", kx.OS_PRESETS["linux"])
    if device == "cdrom":
        bus = d.get("bus") or ("ide" if ctx["family"] == "pc" else "sata")
    else:
        bus = d.get("bus") or preset["disk_bus"]
    target = d.get("target") or _free_target([vm["inactive"], vm["live"]], bus)
    item = {"target": target, "device": device, "bus": bus, "cache": d.get("cache"),
            "serial": d.get("serial"), "readonly": bool(d.get("readonly")) or device == "cdrom",
            "shareable": bool(d.get("shareable")), "boot_order": d.get("boot_order")}
    if kind in ("blank", "image"):
        pool = src.get("pool")
        ks.check_pool_name(pool)
        vol = kx.volume_name(name, target)
        fmt = src.get("format") or "qcow2"
        if kind == "blank":
            size = src.get("size_bytes")
            ks._check_size(size, "size_bytes")
            args = ks.create_volume_argv(pool, vol, size, fmt)
        else:
            ipool = src.get("image_pool") or pool
            if ipool != pool or src.get("linked") is False:
                raise OpError("refused", "adding a disk from an image is linked to an image of "
                              "the same pool in this version")
            img = ks._volume(host, pool, src.get("image"))
            if img is None or img.get("format") not in ks.VOLUME_FORMATS:
                raise OpError("refused", f"image {src.get('image')} not found in {pool} or not "
                              "qcow2/raw")
            size = max(int(src.get("size_bytes") or 0), img.get("capacity") or 0)
            args = ks.create_volume_argv(pool, vol, size, "qcow2", src["image"], img["format"])
            fmt = "qcow2"
        if ks._volume(host, pool, vol) is not None:
            raise OpError("refused", f"a volume named {vol} already exists in {pool}")
        entry = {"kind": "volume", "pool": pool, "volume": vol}
        created.append(entry)
        try:
            host.virsh(*args)
        except kh.KvmError as e:
            if kc._already(e):
                created.remove(entry)        # créé par un autre entre-temps : jamais supprimé
            elif _transport_lost(e):
                entry["uncertain"] = True    # peut-être créé : gardé et nommé
            raise
        item.update(pool=pool, volume=vol, format=fmt)
    elif kind in ("volume", "iso"):
        pool, volname = src.get("pool"), src.get("volume") or src.get("image")
        ks.check_pool_name(pool)
        ko.check_vm_name(volname)
        vol = ks._volume(host, pool, volname)
        if vol is None:
            raise OpError("refused", f"volume {pool}/{volname} not found")
        fmt = "raw" if kind == "iso" or vol.get("format") not in ks.VOLUME_FORMATS \
            else vol["format"]
        item.update(pool=pool, volume=volname, format=fmt)
    return item


def _transport_lost(err):
    """Coupure du transport pendant une écriture : son effet est inconnu."""
    return isinstance(err, kh.KvmError) and err.rc == kh.SSH_RC and err.kind in (
        "unreachable", "failed")


def _drop_created(host, created, entries):
    """Retire les volumes `entries` (créés par ce geste) ; rend les restes.
    Un volume à la création incertaine n'est jamais supprimé : il est nommé."""
    leftovers = []
    for c in entries:
        if c not in created:
            continue
        if c.get("uncertain"):
            leftovers.append({"kind": "volume", "pool": c["pool"], "volume": c["volume"],
                              "error": "created or not: the connection was lost; check and "
                                       "remove it if unused"})
            created.remove(c)
            continue
        try:
            host.virsh("vol-delete", "--pool", c["pool"], "--vol", c["volume"])
        except kh.KvmError as e:
            if e.kind != "notfound":
                leftovers.append({"kind": "volume", "pool": c["pool"], "volume": c["volume"],
                                  "error": e.message})
        created.remove(c)
    return leftovers


def _apply_now(host, name, ch, ctx, vm, created, sleep):
    """Un changement « maintenant » ; rend une raison (texte) quand il n'est
    pas complet (repli au prochain démarrage)."""
    field, new = ch["field"], ch["new"]
    flags = _flags(ctx)
    if field == "cpu.vcpus":
        try:
            host.virsh("setvcpus", "--domain", name, "--count", str(new), *flags)
        except kh.KvmError as e:
            if e.rc == kh.SSH_RC or new > ch["old"] or not ctx["persistent"]:
                raise
            host.virsh("setvcpus", "--domain", name, "--count", str(new), "--config")
            ch["applied"] = "next-boot"
            return "QEMU refused to remove the vCPUs live: " + REASON_RESTART
        return None
    if field == "memory.current_mib":
        host.virsh("setmem", "--domain", name, "--size", str(new * 1024), *flags)
        for _ in range(5):
            actual = parse_memstat(host.virsh("dommemstat", "--domain", name,
                                              idempotent=True)).get("actual")
            if actual is not None and abs(actual - new * 1024) <= 1024:
                return None
            sleep(1.0)
        return ("the guest has not followed yet (no balloon driver?): the setting is "
                "recorded, the guest memory may stay as it was")
    if field in ("title", "description"):
        args = ["desc", "--domain", name, *flags]
        if field == "title":
            args.append("--title")
        args += ["--new-desc", new or ""]
        host.virsh(*args)
        return None
    if field == "autostart":
        host.virsh("autostart", "--domain", name, *([] if new else ["--disable"]))
        return None
    if field.startswith("disks[") and field.endswith("].source"):
        target = field[len("disks["):field.index("]")]
        xml = vm["live"] if ctx["live"] else vm["inactive"]
        if new is None:
            host.virsh("change-media", "--domain", name, "--path", target, "--eject", *flags)
        else:
            ks.virsh_input(host, ("update-device", "--domain", name, "--file", "/dev/stdin",
                                  *flags), _media_xml(xml, target, new["pool"], new["volume"]))
        return None
    if field.startswith("disks["):
        if new is None:
            target = field[len("disks["):field.index("]")]
            el = ET.tostring(_disk_element(vm["live"] or vm["inactive"], target),
                             encoding="unicode")
            ks.virsh_input(host, ("detach-device", "--domain", name, "--file", "/dev/stdin",
                                  *flags), el)
            if not ctx["live"]:
                return None
            waited = 0.0
            while waited < DETACH_WAIT:
                live = host.virsh("dumpxml", "--domain", name, idempotent=True)
                targets = {t.get("dev") for t in kh._safe_xml(live).findall("devices/disk/target")}
                if target not in targets:
                    return None
                sleep(DETACH_POLL)
                waited += DETACH_POLL
            ch["applied"] = "next-boot"
            return REASON_DETACH_PENDING
        before = list(created)
        item = _new_disk(host, name, new, vm, ctx, created)
        ch["target"] = item["target"]
        mine = [c for c in created if c not in before]
        try:
            ks.virsh_input(host, ("attach-device", "--domain", name, "--file", "/dev/stdin",
                                  *flags), kx.device_xml("disk", item))
        except BaseException as exc:
            if _transport_lost(exc):
                for c in mine:               # l'attache a pu réussir : le volume est gardé
                    c["uncertain"] = True
            ctx.setdefault("leftovers", []).extend(_drop_created(host, created, mine))
            raise
        for c in mine:
            created.remove(c)                # attaché : appartient à la VM
        return None
    if field.startswith("nics["):
        if new is None:
            mac = field[len("nics["):field.index("]")]
            itype = (ch["old"] or {}).get("kind") or "network"
            host.virsh("detach-interface", "--domain", name, "--type", itype, "--mac", mac,
                       *flags)
            return None
        n = dict(new)
        n.setdefault("kind", "network")
        n.setdefault("model", "virtio")
        ks.virsh_input(host, ("attach-device", "--domain", name, "--file", "/dev/stdin", *flags),
                       kx.device_xml("interface", n))
        return None
    raise OpError("failed", f"no live action for {field}")


def _toggle(edit, kind, enabled):
    """Agent invité, tablette USB, générateur d'aléa : ajoute ou retire
    l'appareil géré par la console (rien d'autre n'est touché)."""
    devs = edit._devices()
    if kind == "guest_agent":
        found = [c for c in devs.findall("channel") if c.find("target") is not None
                 and c.find("target").get("name") == kx.GUEST_AGENT_CHANNEL]
        el = ET.Element("channel", type="unix")
        ET.SubElement(el, "target", type="virtio", name=kx.GUEST_AGENT_CHANNEL)
        after = "console"
    elif kind == "tablet":
        found = [i for i in devs.findall("input") if i.get("type") == "tablet"]
        el = ET.Element("input", type="tablet", bus="usb")
        after = "channel"
    else:
        found = devs.findall("rng")
        el = ET.Element("rng", model="virtio")
        ET.SubElement(el, "backend", model="random").text = "/dev/urandom"
        after = "memballoon"
    if enabled and not found:
        edit._insert_device(el, after)
    elif not enabled:
        for f in found:
            devs.remove(f)


def _set_hotplug_slots(edit, wanted):
    """Ports pcie-root-port libres (q35) : ajoute ou retire des ports
    qu'aucun appareil n'occupe, jusqu'à `wanted` libres."""
    if edit._family() != "q35":
        raise kx.EditError("refused", "hot-plug slots exist on the q35 machine type only")
    if not isinstance(wanted, int) or not 0 <= wanted <= kx.HOTPLUG_SLOTS_MAX:
        raise kx.EditError("invalid", f"hot-plug slots: 0 to {kx.HOTPLUG_SLOTS_MAX}")
    devs = edit._devices()
    ports = [c for c in devs.findall("controller")
             if c.get("type") == "pci" and c.get("model") == "pcie-root-port"]
    used = set()
    for a in devs.iter("address"):
        if a.get("type") == "pci" and a.get("bus"):
            try:
                used.add(int(a.get("bus"), 0))
            except ValueError:
                pass
    free = [c for c in ports if kx._int(c.get("index")) not in used]
    if len(free) > wanted:
        for c in sorted(free, key=lambda c: kx._int(c.get("index")) or 0)[wanted:]:
            devs.remove(c)
    else:
        top = max([kx._int(c.get("index")) or 0 for c in devs.findall("controller")
                   if c.get("type") == "pci"] or [0])
        for i in range(wanted - len(free)):
            edit._insert_device(ET.Element("controller", type="pci", index=str(top + 1 + i),
                                           model="pcie-root-port"), "controller")


def _set_nic(edit, mac, field, value):
    iface = edit._nic(mac)
    if field == "model":
        if value not in kx.NIC_MODELS:
            raise kx.EditError("invalid", f"unknown model {value}")
        m = iface.find("model")
        if m is None:
            m = ET.SubElement(iface, "model")
        m.set("type", value)
        addr = iface.find("address")
        if addr is not None:
            iface.remove(addr)              # le bus change avec le modèle
        return
    src = iface.find("source")
    if src is None:
        src = ET.SubElement(iface, "source")
    if field == "kind":
        if value not in kx.NIC_KINDS:
            raise kx.EditError("invalid", f"unknown kind {value}")
        iface.set("type", value)
        old = dict(src.attrib)
        src.attrib.clear()
        val = old.get("network") or old.get("bridge") or old.get("dev")
        key = {"network": "network", "bridge": "bridge"}.get(value, "dev")
        if val:
            src.set(key, val)
        if value == "direct":
            src.set("mode", old.get("mode") or "bridge")
    elif field == "source":
        key = {"network": "network", "bridge": "bridge"}.get(iface.get("type"), "dev")
        for k in ("network", "bridge", "dev"):
            src.attrib.pop(k, None)
        src.set(key, value)
    elif field == "mode":
        if value not in kx.DIRECT_MODES:
            raise kx.EditError("invalid", f"unknown macvtap mode {value}")
        src.set("mode", value)


def _config_edits(host, name, vm, items, ctx, created, password_gen):
    """Applique sur le XML inactif relu les changements `items` (champs
    gérés par define) ; rend (xml, secrets) ou (None, ()) si rien ne change."""
    xml = host.virsh("dumpxml", "--inactive", "--security-info", "--domain", name,
                     idempotent=True)
    edit = kx.Edit(xml)
    view = kx.managed_view(xml)
    spec = ctx["spec"]
    fields = {c["field"] for c in items}
    secrets = list(ko._passwords(xml))
    if fields & {"cpu.vcpus", "cpu.max_vcpus", "cpu.sockets", "cpu.cores", "cpu.threads"}:
        cpu = {**view["cpu"], **{k: v for k, v in (spec.get("cpu") or {}).items()
                                 if v is not None}}
        topo = None
        if fields & {"cpu.sockets", "cpu.cores", "cpu.threads", "cpu.max_vcpus"}:
            if (spec.get("cpu") or {}).get("hotplug") or not (
                    fields & {"cpu.sockets", "cpu.cores", "cpu.threads"}):
                topo = {"sockets": cpu["max_vcpus"], "cores": 1, "threads": 1}
            else:
                topo = {k: cpu[k] for k in ("sockets", "cores", "threads")}
        edit.set_vcpus(cpu["vcpus"], cpu["max_vcpus"], topo)
    if fields & {"cpu.mode", "cpu.model"}:
        cpu = {**view["cpu"], **(spec.get("cpu") or {})}
        edit.set_cpu(cpu["mode"], cpu.get("model"))
    if fields & {"memory.current_mib", "memory.max_mib"}:
        mem = {**view["memory"], **(spec.get("memory") or {})}
        edit.set_memory(mem["current_mib"], mem["max_mib"])
    if "machine" in fields:
        edit.set_machine(spec["machine"])
    if fields & {"firmware", "secure_boot"}:
        fam = view.get("machine_family")
        efi = (((ctx["options"] or {}).get("by_machine") or {}).get(fam) or {}).get("efi")
        edit.set_firmware(spec.get("firmware", view["firmware"]),
                          spec.get("secure_boot", view["secure_boot"]), efi=efi)
    if "tpm" in fields:
        fam = view.get("machine_family")
        model = (((ctx["options"] or {}).get("by_machine") or {}).get(fam) or {}).get("tpm_model")
        edit.set_tpm(spec["tpm"], model or "tpm-crb")
    if fields & {"display.vnc", "display.password"}:
        disp = {**view["display"], **(spec.get("display") or {})}
        pw = None
        if "display.password" in fields:
            pw = password_gen() if disp["password"] else False
            if pw:
                secrets.append(pw)
        edit.set_display(disp["vnc"], pw)
    if "display.video" in fields:
        edit.set_video(spec["display"]["video"])
    if "os" in fields:
        edit.set_metadata_os(spec["os"])
    if "hotplug_slots" in fields:
        _set_hotplug_slots(edit, spec["hotplug_slots"])
    for k in ("guest_agent", "tablet", "rng"):
        if k in fields:
            _toggle(edit, k, bool(spec[k]))
    if fields & {"on_poweroff", "on_reboot", "on_crash"}:
        edit.set_lifecycle(spec.get("on_poweroff"), spec.get("on_reboot"), spec.get("on_crash"))
    if "title" in fields:
        edit.set_title(spec.get("title") or None)
    if "description" in fields:
        edit.set_description(spec.get("description") or None)
    boots = {}
    for c in items:
        f = c["field"]
        if not f.startswith(("disks[", "nics[")):
            continue
        ident = f[f.index("[") + 1:f.index("]")]
        sub = f.partition("].")[2]
        if f.startswith("disks["):
            if c["old"] is None and isinstance(c["new"], dict):
                # cibles prises relues sur le XML en cours (deux disques ajoutés
                # dans la même modification n'ont pas la même cible)
                item = _new_disk(host, name, c["new"], {"inactive": edit.xml(),
                                                        "live": vm["live"]}, ctx, created)
                c["target"] = edit.add_disk(item)
            elif c["new"] is None and not sub:
                edit.remove_disk(ident)
            elif sub == "boot_order":
                boots[ident] = c["new"]
            elif sub in ("bus", "cache", "serial", "readonly", "shareable"):
                edit.set_disk(ident, **{sub: c["new"]})
        else:
            if c["old"] is None and isinstance(c["new"], dict):
                n = dict(c["new"])
                n.setdefault("kind", "network")
                n.setdefault("model", "virtio")
                edit.add_nic(n)
            elif c["new"] is None and not sub:
                edit.remove_nic(ident)
            elif sub == "boot_order":
                boots[ident] = c["new"]
            else:
                _set_nic(edit, ident, sub, c["new"])
    if boots:
        edit.set_boot_order(boots)
    if not edit.modified:
        return None, ()
    return edit.xml(), secrets


def apply_edit(host, name, plan, progress=None, cancel=None, sleep=time.sleep,
               password_gen=kc.vnc_password):
    """Applique un plan d'edit_plan : gestes « maintenant » un par un, puis un
    seul `define --validate` du XML inactif pour le reste, renommage en
    dernier, cloud-init régénéré si demandé. Rend {name, changes: [{field,
    applied, reason}], defined, renamed}. Sur erreur : OpError `failed` avec
    `data["changes"]` (déjà fait, échoué, incertain, pas tenté)."""
    changes = plan["_changes"]
    ctx = dict(plan["_ctx"], spec=plan["_spec"])
    vm = plan["_vm"]
    if plan["_blocking"] or (plan["refusals"] and all(c["applied"] == "refused"
                                                      for c in changes)):
        raise OpError("refused", "; ".join(plan["refusals"]), {"refusals": plan["refusals"],
                                                                "changes": _result(changes)})

    def step(sid, status, msg=""):
        if progress:
            progress(sid, status, msg)

    created = []
    ko.invalidate_survey(host)
    now = [c for c in changes if c["applied"] == "now" and ctx["live"]
           and c["field"] not in ("name", "cloudinit")]
    special = {"autostart", "name", "cloudinit"}
    config = [c for c in changes if c["applied"] in ("now", "next-boot")
              and c not in now and c["field"] not in special
              and not (c["field"].endswith("].source") and c["field"].startswith("disks["))]
    media_cold = [c for c in changes if c["applied"] == "now" and not ctx["live"]
                  and c["field"].startswith("disks[") and c["field"].endswith("].source")]
    tail = [c for c in changes if c["applied"] in ("now", "next-boot")
            and c["field"] in ("autostart",) and c not in now]
    renames = [c for c in changes if c["field"] == "name" and c["applied"] == "now"]
    seeds = [c for c in changes if c["field"] == "cloudinit" and c["applied"] != "refused"]
    for c in changes:
        c.setdefault("status", "refused" if c["applied"] == "refused" else "pending")
    defined = False
    try:
        for c in now + media_cold:
            if cancel is not None and cancel():
                raise kh.Cancelled(0, "edit cancelled")
            step("apply", "running", c["field"])
            reason = _write(lambda c=c: _apply_now(host, name, c, ctx, vm, created, sleep), c)
            if reason:
                c["reason"] = reason
            c["status"] = "done"
            step("apply", "warn" if reason else "done", c["field"] + (f": {reason}"
                                                                     if reason else ""))
        if config:
            step("define", "running", f"{len(config)} change(s)")
            before = list(created)
            in_define = False
            try:
                xml, secrets = _write(lambda: _config_edits(host, name, vm, config, ctx, created,
                                                            password_gen), config[0])
                if xml is not None:
                    in_define = True
                    _write(lambda: ks.virsh_input(host, ("define", "--validate", "/dev/stdin"),
                                                  xml, secrets=secrets), config[0])
                    defined = True
            except BaseException as exc:
                mine = [c for c in created if c not in before]
                if in_define and _transport_lost(exc):
                    for c in mine:           # le define a pu passer : volumes gardés
                        c["uncertain"] = True
                ctx.setdefault("leftovers", []).extend(_drop_created(host, created, mine))
                raise
            created[:] = before              # cités par la définition : appartiennent à la VM
            for c in config:
                c["status"] = "done"
            step("define", "done", "configuration updated" if defined else "nothing to change")
        for c in tail:
            _write(lambda c=c: _apply_now(host, name, c, dict(ctx, live=False), vm, created,
                                          sleep), c)
            c["status"] = "done"
        for c in seeds:
            ci = c["new"] or {}
            _write(lambda ci=ci: regenerate_seed(host, name, ci.get("user_data"),
                                                 ci.get("network_data"), bool(ci.get("rerun")),
                                                 hostname=ci.get("hostname"),
                                                 clear=ci.get("clear") is True), c)
            c["status"] = "done"
        for c in renames:
            _write(lambda c=c: host.virsh("domrename", "--domain", name, "--new-name", c["new"]),
                   c)
            c["status"] = "done"
    except BaseException as exc:
        for c in changes:
            if c["status"] == "pending":
                c["status"] = "not-done"
                if c["applied"] != "refused":
                    c["reason"] = REASON_NOT_DONE
        res = _result(changes)
        leftovers = ctx.get("leftovers", []) + list(getattr(exc, "leftovers", None) or [])
        if isinstance(exc, OpError):
            exc.data["changes"] = res
            exc.data["leftovers"] = leftovers
        exc.changes = res
        exc.leftovers = leftovers
        raise
    finally:
        ko.invalidate_survey(host)
    return {"name": renames[0]["new"] if renames else name, "changes": _result(changes),
            "defined": defined, "renamed": bool(renames), "leftovers": []}


def _write(fn, change):
    """Une écriture : jamais rejouée. Une coupure du transport rend le
    changement `uncertain` ; toute autre erreur le rend `failed`."""
    try:
        return fn()
    except kh.KvmError as e:
        if e.rc == kh.SSH_RC and e.kind in ("unreachable", "failed"):
            change["status"] = "uncertain"
            change["reason"] = REASON_UNCERTAIN
        else:
            change["status"] = "failed"
            change["reason"] = e.message
        raise
    except kx.EditError as e:
        change["status"] = "failed"
        change["reason"] = e.message
        raise
    except OpError as e:
        change["status"] = "failed"
        change["reason"] = e.message
        raise


def _result(changes):
    out = []
    for c in changes:
        status = c.get("status")
        applied = c["applied"]
        if status in ("failed", "uncertain", "not-done"):
            applied = status
        out.append({"field": c["field"], "applied": applied, "reason": c.get("reason"),
                    **({"target": c["target"]} if c.get("target") else {})})
    return out


def edit_vm(host, name, spec, expect_digest=None, progress=None, cancel=None, options=None,
            dry_run=False, sleep=time.sleep, gesture=False):
    """Plan recalculé ici (empreinte contrôlée) puis application ; avec
    `dry_run`, le plan seul. Une modification vide ne lance aucune écriture."""
    if progress:
        progress("plan", "running", name)
    plan = edit_plan(host, name, spec, expect_digest, options=options, gesture=gesture)
    if progress:
        progress("plan", "done" if not plan["refusals"] else "warn",
                 f"{len(plan['changes'])} change(s)"
                 + (f"; refused: {'; '.join(plan['refusals'])}" if plan["refusals"] else ""))
    if dry_run or plan["empty"]:
        return {**public_plan(plan), "dry_run": dry_run,
                "changes": plan["changes"] if dry_run else []}
    res = apply_edit(host, name, plan, progress, cancel, sleep)
    return {**res, "config_digest": plan["config_digest"], "state": plan["state"]}


def public_plan(plan):
    return {k: v for k, v in plan.items() if not k.startswith("_")}


# --- gestes à chaud ----------------------------------------------------------------

def _gesture(host, name, build, progress=None, sleep=time.sleep, options=None):
    vm = read_vm(host, name)
    view = kx.managed_view(vm["inactive"] if vm["info"]["persistent"] else vm["live"])
    spec = build(view)
    plan = edit_plan(host, name, spec, options=options, gesture=True, vm=vm)
    if plan["empty"]:
        return {"name": name, "changes": [], "defined": False, "renamed": False}
    return apply_edit(host, name, plan, progress, None, sleep)


def set_cpu_memory(host, name, vcpus=None, memory_mib=None, progress=None, sleep=time.sleep):
    """vCPU et mémoire courante (à chaud jusqu'aux maximums)."""
    if vcpus is None and memory_mib is None:
        raise OpError("invalid", "give vcpus or memory_mib")
    for v, what in ((vcpus, "vcpus"), (memory_mib, "memory_mib")):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            raise OpError("invalid", f"{what} must be a positive integer")

    def build(view):
        spec = {}
        if vcpus is not None:
            spec["cpu"] = {"vcpus": vcpus}
        if memory_mib is not None:
            spec["memory"] = {"current_mib": memory_mib}
        return spec
    return _gesture(host, name, build, progress, sleep)


def attach_disk(host, name, disk, progress=None, sleep=time.sleep):
    """Ajoute un disque (volume vierge ou lié à une image créé ici, ou
    volume existant) ; le volume créé est retiré si l'attache échoue."""
    if not isinstance(disk, dict):
        raise OpError("invalid", "the disk must be an object")
    return _gesture(host, name, lambda v: {"disks": [{"target": d["target"]}
                                                     for d in v["disks"]] + [disk]},
                    progress, sleep)


def detach_disk(host, name, target, progress=None, sleep=time.sleep):
    """Retire un disque ; son volume n'est jamais supprimé."""
    def build(view):
        if target not in {d["target"] for d in view["disks"]}:
            raise OpError("notfound", f"no disk with target {target}")
        return {"disks": [{"target": d["target"]} for d in view["disks"]
                          if d["target"] != target]}
    return _gesture(host, name, build, progress, sleep)


def insert_media(host, name, target, pool, volume, progress=None, sleep=time.sleep):
    ks.check_pool_name(pool)
    ko.check_vm_name(volume)

    def build(view):
        return {"disks": [{"target": d["target"], **({"source": {"kind": "iso", "pool": pool,
                                                                 "volume": volume}}
                                                     if d["target"] == target else {})}
                          for d in view["disks"]]}
    return _media(host, name, target, build, progress, sleep)


def eject_media(host, name, target, progress=None, sleep=time.sleep):
    def build(view):
        return {"disks": [{"target": d["target"], **({"source": {"kind": "empty"}}
                                                     if d["target"] == target else {})}
                          for d in view["disks"]]}
    return _media(host, name, target, build, progress, sleep)


def _media(host, name, target, build, progress, sleep):
    def checked(view):
        d = next((d for d in view["disks"] if d["target"] == target), None)
        if d is None:
            raise OpError("notfound", f"no disk with target {target}")
        if d["device"] != "cdrom":
            raise OpError("refused", f"{target} is not a CD-ROM drive")
        return build(view)
    return _gesture(host, name, checked, progress, sleep)


def attach_nic(host, name, nic, progress=None, sleep=time.sleep):
    if not isinstance(nic, dict):
        raise OpError("invalid", "the network card must be an object")
    return _gesture(host, name, lambda v: {"nics": [{"mac": n["mac"]} for n in v["nics"]
                                                    if n.get("mac")] + [nic]}, progress, sleep)


def detach_nic(host, name, mac, progress=None, sleep=time.sleep):
    mac = (mac or "").lower()

    def build(view):
        if mac not in {n.get("mac") for n in view["nics"]}:
            raise OpError("notfound", f"no network card with MAC {mac}")
        return {"nics": [{"mac": n["mac"]} for n in view["nics"] if n.get("mac") != mac]}
    return _gesture(host, name, build, progress, sleep)


# --- cloud-init --------------------------------------------------------------------

def empty_user_data_refusal(host, seed, ci):
    """Raison de refus si `ci` remplacerait un user-data non vide par un vide
    sans `clear: true` (un champ resté vide par erreur effacerait le
    cloud-init de la VM) ; None sinon. Le seed actuel n'est lu que dans ce
    cas (user-data vide demandé, seed présent)."""
    ci = ci if isinstance(ci, dict) else {}
    if (ci.get("user_data") or "").strip() or ci.get("clear") is True or not seed:
        return None
    files = ko.read_seed(host, seed["pool"], seed["volume"])
    return REASON_EMPTY_USER_DATA if (files.get("user-data") or b"").strip() else None


def regenerate_seed(host, name, user_data, network_data, rerun=False, hostname=None,
                    uuid_gen=uuidlib.uuid4, acknowledged_unreadable=None, before_write=None,
                    clear=False):
    """Nouvelle ISO de seed téléversée sous un nom neuf (`<vm>-cidata-<n>.iso`),
    média changé (à chaud si la VM tourne), `<hops:seed>` mis à jour par un
    `define`. `rerun` : nouvel `instance-id` (cloud-init rejoue ses modules
    de premier démarrage). Un user-data vide qui remplacerait un user-data
    non vide est refusé (OpError `refused`) sauf `clear=True`. Rend {seed, previous, removed, kept_reason,
    next_boot, rerun, leftovers}.

    Ancien seed : supprimé SEULEMENT après le `define` réussi, et seulement
    si la preuve de la 1.98 le permet (relevé neuf, identité
    périphérique:inode, chaînes, stockage partagé : delete_volume_plan) ;
    sinon gardé, avec la raison dans `kept_reason`. `acknowledged_unreadable` :
    comme pour la suppression d'un volume (décision 46). `before_write(pool,
    volume)` : appelé avant toute écriture avec le pool et le nom du nouveau
    seed (l'appelant y prend ses verrous ; une exception arrête tout).

    Échec : avant le changement de média, le nouveau volume est retiré ;
    après (`update-device` a pu basculer le lecteur sur lui, ou la coupure
    du transport le laisse incertain), il est GARDÉ, car la VM le cite, et
    nommé dans `exc.leftovers` ; l'ancien seed n'est alors jamais touché."""
    ko.check_acknowledged(acknowledged_unreadable)
    vm = read_vm(host, name)
    if not vm["info"]["persistent"]:
        raise OpError("refused", REASON_TRANSIENT)
    view = kx.managed_view(vm["inactive"])
    seed = view.get("seed")
    why = empty_user_data_refusal(host, seed, {"user_data": user_data, "clear": bool(clear)})
    if why:
        raise OpError("refused", why)
    live = vm["info"]["state"] in LIVE_STATES
    instance_id = str(uuid_gen()) if rerun else (view.get("uuid") or str(uuid_gen()))
    if seed is None:
        pools = [((d.get("source") or {}).get("pool")) for d in view["disks"]
                 if (d.get("source") or {}).get("pool")]
        if not pools:
            raise OpError("refused", "the VM has no volume in a pool to hold its seed")
        pool = pools[0]
    else:
        pool = seed["pool"]
    vols = ks.pool_volumes(host, pool)
    n = 1
    while f"{name}-cidata-{n}.iso" in vols:
        n += 1
    new_vol = f"{name}-cidata-{n}.iso"
    data = kc.seed_iso({"user_data": user_data, "network_data": network_data}, instance_id,
                       hostname or kc.guest_hostname(name))
    if before_write is not None:
        before_write(pool, new_vol)
    created = []
    switched = False                    # le lecteur peut citer le nouveau volume
    with ko.writing(host):
        try:
            kc.upload_bytes(host, pool, new_vol, data, created=created)
            flags = ["--live", "--config"] if live else ["--config"]
            target = None
            for d in view["disks"]:
                src = d.get("source") or {}
                if seed and src.get("pool") == seed["pool"] and \
                        src.get("volume") == seed["volume"]:
                    target = d["target"]
            if target:
                try:
                    ks.virsh_input(host, ("update-device", "--domain", name, "--file",
                                          "/dev/stdin", *flags),
                                   _media_xml(vm["live"] if live else vm["inactive"], target,
                                              pool, new_vol))
                except kh.KvmError as e:
                    switched = _transport_lost(e)
                    raise
                switched = True
            xml = host.virsh("dumpxml", "--inactive", "--security-info", "--domain", name,
                             idempotent=True)
            edit = kx.Edit(xml)
            if not target:
                fam = view.get("machine_family")
                bus = "ide" if fam == "pc" else "sata"
                used = {d["target"] for d in view["disks"]}
                target = edit.add_disk({"device": "cdrom", "bus": bus, "readonly": True,
                                        "target": kx._free_target(kx._TARGET_PREFIX[bus], used),
                                        "pool": pool, "volume": new_vol, "format": "raw"})
            edit.set_seed(pool, new_vol)
            try:
                ks.virsh_input(host, ("define", "--validate", "/dev/stdin"), edit.xml(),
                               secrets=ko._passwords(xml))
            except kh.KvmError as e:
                switched = switched or _transport_lost(e)
                raise
        except BaseException as exc:
            if switched and _reconcile_seed(host, name, pool, new_vol):
                # le lecteur cite le nouveau volume et <hops:seed> aussi : la
                # suppression de la VM le reconnaît comme possédé, une
                # régénération suivante le remplace (pas de second lecteur)
                leftovers = []
                if seed and (seed["pool"], seed["volume"]) != (pool, new_vol):
                    leftovers.append({"kind": "volume", "pool": seed["pool"],
                                      "volume": seed["volume"],
                                      "error": "previous seed, no longer used by the VM: it "
                                               "was not removed"})
            elif switched:
                leftovers = [{"kind": "volume", "pool": pool, "volume": new_vol,
                              "error": "kept: the CD-ROM of the VM may already use it; the "
                                       "previous seed was not removed"}]
            else:
                leftovers = kc.rollback(host, created)
            exc.leftovers = leftovers
            if isinstance(exc, OpError):
                exc.data["leftovers"] = leftovers
            raise
        removed, kept_reason = False, None
        if seed:
            try:
                plan = ks.delete_volume_plan(host, seed["pool"], seed["volume"],
                                             acknowledged_unreadable)
                if plan["refusals"]:
                    kept_reason = "; ".join(plan["refusals"])
                else:
                    host.virsh("vol-delete", "--pool", seed["pool"], "--vol", seed["volume"])
                    removed = True
            except (kh.KvmError, OpError) as e:
                kept_reason = e.message
    return {"seed": {"pool": pool, "volume": new_vol, "target": target},
            "previous": seed, "removed": removed, "kept_reason": kept_reason,
            "next_boot": True, "rerun": bool(rerun), "leftovers": []}


def _reconcile_seed(host, name, pool, volume):
    """Après un échec qui suit le changement de média : si la définition
    persistante cite déjà le nouveau volume sur un CD-ROM, `<hops:seed>` est
    remis d'accord avec ce lecteur (un `define` qui ne change que lui).
    Rend True si la définition est cohérente à la sortie ; jamais d'erreur
    (au mieux : le reste est dit par l'appelant)."""
    try:
        xml = host.virsh("dumpxml", "--inactive", "--security-info", "--domain", name,
                         idempotent=True)
        vm = kh.parse_domain_xml(xml)
        cites = any(d.get("device") == "cdrom" and d.get("type") == "volume"
                    and d.get("source") == f"{pool}/{volume}" for d in vm.get("disks") or [])
        if not cites:
            return False
        if vm.get("seed") == {"pool": pool, "volume": volume}:
            return True
        edit = kx.Edit(xml)
        edit.set_seed(pool, volume)
        ks.virsh_input(host, ("define", "--validate", "/dev/stdin"), edit.xml(),
                       secrets=ko._passwords(xml))
        return True
    except (kh.KvmError, OpError, ValueError, SyntaxError):
        return False
