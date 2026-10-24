"""Opérations composées sur les VMs d'un hôte KVM (v1.97.0).

`kvm_host` reste le transport (ssh, virsh, batch) et les analyseurs ; ici
vivent les opérations qui enchaînent plusieurs lectures et écritures :
affichage VNC, instantanés internes, disques possédés, clonage par copie
complète et suppression d'une VM.

Principes :
- aucun pool libvirt n'est créé ni modifié : on lit `pool-list`,
  `pool-dumpxml`, `vol-list`, `vol-dumpxml`, et on crée ou supprime des
  volumes dans des pools existants ;
- chaque plan (clonage, suppression) est recalculé au moment d'agir : un
  aperçu montré plus tôt n'est jamais cru ;
- le mot de passe VNC (`passwd`, lu par `dumpxml --security-info`) ne sort
  jamais : ni dans un résultat, ni dans une erreur, ni dans un journal. Seule
  sa présence est dite (`password: True`) ;
- un disque n'est supprimé que si l'on PROUVE qu'aucun autre domaine ne s'en
  sert (directement ou comme image de base) ; une lecture impossible le garde.

Les refus et échecs propres à ces opérations sont des `OpError` (genre dans
OP_KINDS) ; les échecs de transport restent des `kvm_host.KvmError`.
"""

import io
import ipaddress
import json
import posixpath
import re
import secrets
import shlex
import time
import xml.etree.ElementTree as ET

import kvm_host as kh

OP_KINDS = ("invalid", "refused", "notfound", "needs-force", "timeout", "failed")

# nom d'un instantané créé par la console (un instantané existant, créé
# ailleurs, est désigné par tout nom accepté par check_vm_name)
SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DESCRIPTION_MAX = 1000

# états où un instantané est refusé (libvirt le refuserait ou le ferait mal)
_SNAPSHOT_BAD_STATES = ("paused", "in shutdown", "pmsuspended", "crashed")
# pools dont un volume est un fichier `<cible>/<nom>`
_DIR_POOLS = ("dir", "fs", "netfs")
# formats qui peuvent porter une image de base (un raw n'en a jamais)
_BACKING_FORMATS = (None, "qcow2", "qed", "vmdk", "vhdx", "qcow")

UNKNOWN = "unknown"
REASON_RO = "read-only or CD-ROM"
REASON_BLOCK = "block device"
REASON_NETWORK = "network disk"
REASON_UNKNOWN = "could not prove that no other VM uses it"
REASON_NOT_REQUESTED = "storage removal not requested"

COPY_POLL = 2.0


class OpError(Exception):
    """Refus ou échec d'une opération. `kind` parmi OP_KINDS, `message`
    lisible (jamais de secret), `data` : complément (refus, restes...)."""

    def __init__(self, kind, message, data=None):
        super().__init__(message)
        self.kind = kind if kind in OP_KINDS else "failed"
        self.message = message
        self.data = dict(data or {})


# --- noms ----------------------------------------------------------------

def check_vm_name(name):
    """Nom libvirt : ni vide, ni caractère de contrôle, 255 au plus. Il part
    toujours après une option (`--domain`), jamais comme option."""
    if not isinstance(name, str) or not name or len(name) > 255 or any(
            ord(c) < 32 or ord(c) == 127 for c in name):
        raise OpError("invalid", "invalid domain name")
    return name


def check_new_vm_name(name):
    """Nom d'un clone : celui d'un domaine, sans `/` et sans point en tête
    (il entre dans le nom des fichiers copiés `<nom>-<cible>.<ext>`)."""
    check_vm_name(name)
    if "/" in name or name.startswith("."):
        raise OpError("invalid", "invalid name for the copy: no '/' and no leading '.'")
    return name


def check_snapshot_name(name, new=False):
    """`new` : nom d'un instantané créé par la console (SNAPSHOT_RE) ; sinon
    un instantané existant, désigné par tout nom libvirt."""
    if new:
        if not isinstance(name, str) or not SNAPSHOT_RE.match(name):
            raise OpError("invalid", "invalid snapshot name: letters, digits, '.', '_' and '-', "
                          "64 characters at most, starting with a letter or digit")
        return name
    try:
        return check_vm_name(name)
    except OpError:
        raise OpError("invalid", "invalid snapshot name") from None


def _check_description(text):
    if text is None:
        return None
    if not isinstance(text, str) or len(text) > DESCRIPTION_MAX or "\x00" in text:
        raise OpError("invalid", f"invalid description ({DESCRIPTION_MAX} characters at most)")
    return text


# --- petites aides ---------------------------------------------------------

def _need(out, key):
    """Sortie d'une commande d'un batch, ou son erreur levée."""
    v = out[key]
    if not v.ok:
        raise v.error
    return str(v)


def _vm(xml):
    return kh.parse_domain_xml(xml) if isinstance(xml, str) else xml


def _writable_disk(d):
    return d.get("device", "disk") == "disk" and not d.get("readonly") and not d.get("shareable")


def _sh_batch(host, commands, timeout=None):
    """Plusieurs commandes shell (pas virsh) en UN appel ssh : chaque
    commande est une liste d'arguments, citée une à une ; rend [(rc,
    sortie)] (stderr mêlé à stdout). Le jeton des repères est tiré au hasard
    à chaque appel."""
    if not commands:
        return []
    token = "@@kvmops-" + secrets.token_hex(12)
    lines = ["export LC_ALL=C", f"T={token}",
             "_r() { i=$1; shift; \"$@\" </dev/null 2>&1; rc=$?; "
             "printf '\\n%s %d %d\\n' \"$T\" \"$i\" \"$rc\"; }"]
    for i, argv in enumerate(commands):
        lines.append(f"_r {i} " + " ".join(shlex.quote(str(a)) for a in argv))
    out = host.run(["sh", "-c", "\n".join(lines)], timeout=timeout)
    parts = re.split(rf"\n{re.escape(token)} (\d+) (-?\d+)\n", out)
    res = {}
    for j in range(0, len(parts) - 2, 3):
        res[int(parts[j + 1])] = (int(parts[j + 2]), parts[j])
    return [res.get(i, (-1, "no output")) for i in range(len(commands))]


def _last_line(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


# --- affichage ---------------------------------------------------------------

def display_target(xml_vm, live):
    """Où joindre l'affichage VNC d'une VM, vu depuis l'hôte : premier
    `<graphics type='vnc'>` du XML VIVANT (`xml_vm` : texte XML ou dict de
    `parse_domain_xml`) ; `live` : la VM tourne.

    Rend {"type": "vnc", "address", "port", "password": bool} ou {"reason"}
    parmi `notrunning`, `spice`, `socket`, `nodisplay`, `nolisten`, `listen`.
    `password` n'est vrai que si le XML a été lu par `--security-info` (sinon
    libvirt ne montre pas `passwd`) ; la valeur n'est jamais rendue."""
    vm = _vm(xml_vm)
    graphics = vm.get("graphics") or []
    vnc = next((g for g in graphics if g.get("type") == "vnc"), None)
    if vnc is None:
        if any(g.get("type") == "spice" for g in graphics):
            return {"reason": "spice"}
        return {"reason": "nodisplay"}
    lt = vnc.get("listen_type") or "address"
    if lt == "socket":
        # le socket appartient à root et qemu : le compte SSH ne s'y connecte pas
        return {"reason": "socket"}
    if lt == "none":
        return {"reason": "nolisten"}
    if not live or not vnc.get("port"):
        return {"reason": "notrunning"}
    listen = (vnc.get("listen") or "").strip().strip("[]")
    if not listen:
        address = "127.0.0.1"           # vnc_listen par défaut de qemu.conf
    else:
        try:
            ip = ipaddress.ip_address(listen)
        except ValueError:
            return {"reason": "listen"}  # nom d'hôte : jamais résolu par la console
        # joker : on passe par la boucle locale de l'hôte, jamais par le réseau
        address = "127.0.0.1" if ip.is_unspecified else ip.compressed
    return {"type": "vnc", "address": address, "port": vnc["port"],
            "password": bool(vnc.get("password"))}


def vnc_password(host, name):
    """Mot de passe VNC d'une VM (`dumpxml --security-info`), rendu au seul
    appelant, en mémoire ; None sans mot de passe. Jamais appelée par la CLI.
    Une erreur ne cite jamais le XML."""
    check_vm_name(name)
    xml = host.virsh("dumpxml", "--security-info", "--domain", name)
    try:
        root = kh._safe_xml(xml)
    except (ET.ParseError, ValueError):
        raise OpError("failed", f"{host.name}: unreadable definition for this VM") from None
    for g in root.findall("devices/graphics"):
        if g.get("type") == "vnc":
            return g.get("passwd") or None
    return None


# --- instantanés -------------------------------------------------------------

def snapshot_support(vm):
    """Raisons qui empêchent un instantané interne (liste vide = possible).
    Chaque disque inscriptible doit être un fichier ou un volume qcow2 ; un
    PCI attaché ou une nvram UEFI non qcow2 l'empêchent aussi. Les lecteurs
    en lecture seule ne comptent pas."""
    vm = _vm(vm)
    reasons = []
    for d in vm.get("disks") or []:
        if not _writable_disk(d) or not d.get("source"):
            continue
        t = d.get("target") or "?"
        if d.get("transient"):
            reasons.append(f"transient disk {t}: internal snapshots are not possible")
        elif d.get("type") == "block":
            reasons.append(f"block disk {t} (LVM, partition): internal snapshots need a qcow2 file")
        elif d.get("type") == "network":
            reasons.append(f"network disk {t}: internal snapshots need a qcow2 file")
        elif d.get("type") not in ("file", "volume"):
            reasons.append(f"{d.get('type')} disk {t}: internal snapshots need a qcow2 file")
        elif (d.get("format") or "raw") != "qcow2":
            reasons.append(f"{d.get('format') or 'raw'} disk {t}: internal snapshots need qcow2")
    if vm.get("hostdevs"):
        reasons.append("PCI or USB device attached (hostdev): libvirt cannot snapshot it")
    nv = vm.get("nvram")
    if nv and (nv.get("format") or "raw") != "qcow2":
        reasons.append(f"UEFI variables (nvram) are {nv.get('format') or 'raw'}: "
                       "internal snapshots need qcow2")
    return reasons


def _snapshot_commands(name):
    return {"tree": ("snapshot-list", "--domain", name, "--parent"),
            "internal": ("snapshot-list", "--domain", name, "--internal", "--name"),
            "external": ("snapshot-list", "--domain", name, "--external", "--name"),
            "current": ("snapshot-current", "--domain", name, "--name"),
            "xml": ("dumpxml", "--domain", name),
            "info": ("dominfo", "--domain", name)}


def list_snapshots(host, name):
    """Instantanés d'une VM en un seul `batch`. Rend {"snapshots": [{name,
    created, state, parent, location, current}], "current", "support",
    "running", "state", "persistent", "memory_kib"}. `state` d'un instantané
    = état du domaine à la prise (`running` : mémoire comprise)."""
    check_vm_name(name)
    out = host.batch(_snapshot_commands(name))
    xml, info = _need(out, "xml"), kh.parse_dominfo(_need(out, "info"))
    tree = kh.parse_snapshot_list(_need(out, "tree"))
    internal = set(kh.parse_names(_need(out, "internal")))
    external = set(kh.parse_names(_need(out, "external")))
    cur = out["current"]
    if cur.ok:
        current = str(cur).strip() or None
    elif "no current snapshot" in str(cur).lower():
        current = None
    else:
        raise cur.error
    vm = kh.parse_domain_xml(xml)
    snaps = []
    for s in tree:
        loc = "internal" if s["name"] in internal else "external" if s["name"] in external else None
        snaps.append({**s, "location": loc, "current": s["name"] == current})
    return {"snapshots": snaps, "current": current, "support": snapshot_support(vm),
            "running": info["state"] == "running", "state": info["state"],
            "persistent": info["persistent"],
            "memory_kib": vm.get("current_memory_kib") or vm.get("memory_kib")}


def _find_snapshot(listing, snapshot):
    snap = next((s for s in listing["snapshots"] if s["name"] == snapshot), None)
    if snap is None:
        raise OpError("notfound", f"snapshot {snapshot} not found")
    if snap["location"] == "external":
        raise OpError("refused", "external snapshot: not managed by the console in this version")
    return snap


def create_snapshot(host, name, snapshot, description=None, timeout=600):
    """Instantané interne (`snapshot-create-as --atomic`). VM en marche :
    mémoire comprise, l'invité est en pause pendant l'écriture. Jamais de
    `--disk-only` (il fabriquerait un instantané externe). OpError `refused`
    (raisons dans data["refusals"]) quand c'est impossible."""
    check_vm_name(name)
    check_snapshot_name(snapshot, new=True)
    description = _check_description(description)
    before = list_snapshots(host, name)
    refusals = list(before["support"])
    if any(s["location"] == "external" for s in before["snapshots"]):
        refusals.append("this VM uses external snapshots: not managed by the console "
                        "in this version")
    if not before["persistent"]:
        refusals.append("transient VM: snapshots need a persistent definition")
    if before["state"] in _SNAPSHOT_BAD_STATES:
        refusals.append(f"the VM is {before['state']}: wait until it is running or shut off")
    if any(s["name"] == snapshot for s in before["snapshots"]):
        refusals.append(f"a snapshot named {snapshot} already exists")
    if refusals:
        raise OpError("refused", "; ".join(refusals), {"refusals": refusals})
    args = ["snapshot-create-as", "--domain", name, "--name", snapshot]
    if description:
        args += ["--description", description]
    args.append("--atomic")
    host.virsh(*args, timeout=timeout)
    after = list_snapshots(host, name)
    return {"snapshot": snapshot, "memory": before["state"] == "running",
            "state": after["state"], "current": after["current"],
            "snapshots": after["snapshots"]}


def revert_snapshot(host, name, snapshot, timeout=300):
    """Retour à un instantané interne, jamais `--force` : une erreur de
    libvirt qui le réclame devient OpError `needs-force` (son message, sans
    le proposer). L'état réel est relu après (`domstate`)."""
    check_vm_name(name)
    check_snapshot_name(snapshot)
    snap = _find_snapshot(list_snapshots(host, name), snapshot)
    try:
        host.virsh("snapshot-revert", "--domain", name, "--snapshotname", snapshot,
                   timeout=timeout)
    except kh.KvmError as e:
        if "--force" in e.message:
            raise OpError("needs-force", e.message) from None
        raise
    state = host.virsh("domstate", "--domain", name).strip()
    return {"snapshot": snapshot, "taken_state": snap["state"], "state": state}


def delete_snapshot(host, name, snapshot, timeout=300):
    """Suppression d'un instantané interne (ses enfants sont rattachés à son
    parent, comportement de libvirt). Pas de `--children`."""
    check_vm_name(name)
    check_snapshot_name(snapshot)
    _find_snapshot(list_snapshots(host, name), snapshot)
    host.virsh("snapshot-delete", "--domain", name, "--snapshotname", snapshot, timeout=timeout)
    after = list_snapshots(host, name)
    return {"snapshot": snapshot, "current": after["current"], "snapshots": after["snapshots"]}


# --- disques et leurs utilisateurs ------------------------------------------

class _Survey:
    """Relevé des disques de l'hôte, fait une fois par opération : XML de
    chaque autre domaine (inactif, et vivant pour les actifs), pools actifs,
    volumes des pools qu'on regarde, chaînes `qemu-img` des fichiers hors pool
    des domaines arrêtés. Ce qui n'a pas pu être lu est noté dans `unknown`
    (on ne peut alors rien prouver)."""

    def __init__(self, host, exclude=None):
        self.host, self.exclude = host, exclude
        self.unknown = []
        first = host.batch({"all": ("list", "--all", "--name"), "active": ("list", "--name"),
                            "pools": ("pool-list", "--name")})
        names = kh.parse_names(_need(first, "all"))
        active = set(kh.parse_names(_need(first, "active")))
        pools = kh.parse_names(_need(first, "pools"))
        cmds = {}
        for d in names:
            cmds[f"inactive\0{d}"] = ("dumpxml", "--inactive", "--domain", d)
            if d in active and d != exclude:
                cmds[f"live\0{d}"] = ("dumpxml", "--domain", d)
        for p in pools:
            cmds[f"pool\0{p}"] = ("pool-dumpxml", "--pool", p)
        out = host.batch(cmds) if cmds else {}
        self.pools = {}
        for p in pools:
            v = out.get(f"pool\0{p}")
            if v is not None and v.ok:
                try:
                    self.pools[p] = kh.parse_pool_xml(str(v))
                except (ET.ParseError, ValueError):
                    self.unknown.append(f"pool {p}")
            else:
                self.unknown.append(f"pool {p}")
        self.target = None              # disques du domaine exclu (inactif)
        self.domains = {}               # autre domaine -> {"inactive": vm, "live": vm|None}
        for d in names:
            entry = {}
            for kind in ("inactive", "live"):
                v = out.get(f"{kind}\0{d}")
                if v is None:
                    continue
                if not v.ok:
                    if v.error.kind != "notfound":      # parti entre-temps : ignoré
                        self.unknown.append(f"domain {d}")
                    continue
                try:
                    entry[kind] = kh.parse_domain_xml(str(v))
                except (ET.ParseError, ValueError):
                    self.unknown.append(f"domain {d}")
            if d == exclude:
                self.target = entry.get("inactive")
            elif entry:
                entry["active"] = d in active
                self.domains[d] = entry
        self._vols = {}                 # pool -> {nom: vol} (lus à la demande)
        self._chains = None             # fichier hors pool -> chaîne (qemu-img)

    # identités d'un disque : son chemin et, s'il est dans un pool, « pool/volume »
    def locate(self, disk):
        """{"path", "pool", "volume"} d'un disque fichier ou volume (None
        pour ce qui ne se résout pas)."""
        dtype, src = disk.get("type"), disk.get("source")
        if not src:
            return {"path": None, "pool": None, "volume": None}
        if dtype == "volume":
            pool, _, vol = src.partition("/")
            info = self.pools.get(pool)
            path = (posixpath.join(info["target_path"], vol)
                    if info and info["type"] in _DIR_POOLS and info["target_path"] else None)
            return {"path": path, "pool": pool, "volume": vol}
        if dtype == "file":
            parent, base = posixpath.split(posixpath.normpath(src))
            for p, info in self.pools.items():
                if info["type"] in _DIR_POOLS and info["target_path"] and \
                        posixpath.normpath(info["target_path"]) == parent:
                    return {"path": src, "pool": p, "volume": base}
            return {"path": src, "pool": None, "volume": None}
        return {"path": src, "pool": None, "volume": None}

    def identities(self, disk):
        loc = self.locate(disk)
        ids = {disk.get("source")} if disk.get("source") else set()
        if loc["path"]:
            ids.add(posixpath.normpath(loc["path"]))
        if loc["pool"] and loc["volume"]:
            ids.add(f"{loc['pool']}/{loc['volume']}")
        return ids

    def users(self):
        """{identité de disque: [domaines]} des AUTRES domaines : sources de
        leurs disques (définition et XML vivant) et, pour les actifs, chaînes
        de sauvegarde de leur XML vivant."""
        users = {}
        for d, entry in self.domains.items():
            seen = set()
            for kind in ("inactive", "live"):
                vm = entry.get(kind)
                for disk in (vm or {}).get("disks", []):
                    ids = self.identities(disk)
                    for b in disk.get("backing_chain") or []:
                        ids |= {b, posixpath.normpath(b)}
                    seen |= ids
            for i in seen:
                users.setdefault(i, []).append(d)
        return users

    def pool_volumes(self, pool):
        """{nom: vol} d'un pool (vol-list puis vol-dumpxml, lus par libvirt
        avec ses droits) ; {} et `unknown` si illisible."""
        if pool in self._vols:
            return self._vols[pool]
        vols = {}
        try:
            names = [v["name"] for v in kh.parse_vol_list_details(
                self.host.virsh("vol-list", "--pool", pool, "--details"))]
            out = self.host.batch({n: ("vol-dumpxml", "--pool", pool, "--vol", n)
                                   for n in names}) if names else {}
            for n in names:
                v = out[n]
                if v.ok:
                    vols[n] = kh.parse_vol_xml(str(v))
                elif v.error.kind != "notfound":
                    self.unknown.append(f"volume {pool}/{n}")
        except (kh.KvmError, ET.ParseError, ValueError):
            self.unknown.append(f"pool {pool}")
        self._vols[pool] = vols
        return vols

    def stopped_files(self):
        """Fichiers hors pool des domaines arrêtés (formats à image de base) :
        leur chaîne n'est connue qu'en ouvrant le fichier."""
        files = set()
        for entry in self.domains.values():
            if entry.get("active"):
                continue
            for disk in (entry.get("inactive") or {}).get("disks", []):
                if disk.get("type") != "file" or not disk.get("source"):
                    continue
                if disk.get("format") not in _BACKING_FORMATS:
                    continue
                if self.locate(disk)["pool"] is None:
                    files.add(disk["source"])
        return sorted(files)

    def chains(self):
        """{fichier: [chemins de sa chaîne]} par `qemu-img info --force-share
        --backing-chain`, lancé par le compte SSH ; un fichier illisible va
        dans `unknown`."""
        if self._chains is not None:
            return self._chains
        files = self.stopped_files()
        self._chains = {}
        res = _sh_batch(self.host, [["qemu-img", "info", "--force-share", "--output=json",
                                     "--backing-chain", "--", f] for f in files]) if files else []
        for f, (rc, text) in zip(files, res):
            try:
                if rc != 0:
                    raise ValueError(text)
                self._chains[f] = _chain_paths(f, json.loads(text))
            except ValueError:
                self.unknown.append(f"file {f}")
        return self._chains

    def backing_users(self, path, pool=None):
        """Chemins dont la chaîne de sauvegarde passe par `path` : volumes du
        même pool (vol-dumpxml), fichiers hors pool des domaines arrêtés
        (qemu-img) ; UNKNOWN en fin de liste si une lecture a échoué."""
        target = posixpath.normpath(path)
        found = []
        if pool:
            vols = self.pool_volumes(pool)
            by_path = {posixpath.normpath(v["path"]): v for v in vols.values() if v.get("path")}
            for vpath, v in by_path.items():
                b, depth = v.get("backing"), 0
                while b and depth < 64:     # remonte la chaîne dans le pool
                    if posixpath.normpath(b) == target:
                        found.append(vpath)
                        break
                    nxt = by_path.get(posixpath.normpath(b))
                    b, depth = (nxt.get("backing") if nxt else None), depth + 1
        for f, chain in self.chains().items():
            if any(posixpath.normpath(c) == target for c in chain):
                found.append(f)
        found = sorted(set(found) - {target})
        if self.unknown:
            found.append(UNKNOWN)
        return found


def _chain_paths(path, info):
    """Chemins des images de base d'après `qemu-img info --backing-chain
    --output=json` (le premier élément est le fichier lui-même) ; un chemin
    relatif l'est au répertoire de l'image qui le cite."""
    info = info if isinstance(info, list) else [info]
    chain, prev = [], path
    for item in info[1:]:
        name = item.get("filename") if isinstance(item, dict) else None
        if not name:
            continue
        if not name.startswith("/") and ":" not in name:
            name = posixpath.join(posixpath.dirname(prev), name)
        chain.append(posixpath.normpath(name) if name.startswith("/") else name)
        prev = name
    return chain


def disk_users(host, exclude=None):
    """{chemin (ou « pool/volume »): [domaines]} : disques cités par chaque
    domaine autre que `exclude` (définition, XML vivant, et chaînes de
    sauvegarde des domaines actifs)."""
    return _Survey(host, exclude).users()


def backing_users(host, path):
    """Chemins dont `path` est l'image de base (volumes du même pool,
    fichiers hors pool des domaines arrêtés). UNKNOWN dans la liste quand une
    lecture a échoué : rien n'est alors prouvé."""
    survey = _Survey(host)
    pool = survey.locate({"type": "file", "source": path})["pool"]
    return survey.backing_users(path, pool)


def _owned(survey, vm):
    """Disques d'une VM avec ce qu'on peut en faire (voir owned_disks)."""
    users = survey.users()
    disks = []
    candidates = []
    for d in vm.get("disks") or []:
        loc = survey.locate(d)
        entry = {"target": d.get("target"), "source": d.get("source"), "pool": loc["pool"],
                 "volume": loc["volume"], "path": loc["path"], "format": d.get("format"),
                 "kind": None, "deletable": False, "reason": None}
        if loc["pool"] and loc["volume"] and d.get("type") == "file":
            # un fichier n'est un volume que si libvirt le connaît (pool rafraîchi)
            if loc["volume"] not in survey.pool_volumes(loc["pool"]):
                entry.update(pool=None, volume=None)
        entry["kind"] = "volume" if entry["volume"] else d.get("type")
        if d.get("device", "disk") != "disk" or d.get("readonly"):
            entry["reason"] = REASON_RO
        elif not d.get("source"):
            entry["reason"] = "empty drive"
        elif d.get("type") in ("block", "nvme"):
            entry["reason"] = REASON_BLOCK
        elif d.get("type") in ("network", "vhostuser"):
            entry["reason"] = REASON_NETWORK
        elif d.get("type") not in ("file", "volume"):
            entry["reason"] = f"{d.get('type')} disk"
        else:
            sharers = sorted({u for i in survey.identities(d) for u in users.get(i, [])})
            if sharers:
                entry["reason"] = f"shared with {', '.join(sharers)}"
            else:
                candidates.append(entry)
        disks.append(entry)
    own = {posixpath.normpath(e["path"]) for e in candidates if e["path"]}
    for e in candidates:
        if not e["path"]:
            e["reason"] = REASON_UNKNOWN
            continue
        found = survey.backing_users(e["path"], e["pool"])
        # une surcouche qui est elle-même un disque supprimé de la VM ne compte pas
        others = [f for f in found if f != UNKNOWN and posixpath.normpath(f) not in own]
        if others:
            e["reason"] = f"backing file of {others[0]}"
        elif UNKNOWN in found:
            e["reason"] = REASON_UNKNOWN
        else:
            e["deletable"] = True
    return disks


def owned_disks(host, vm):
    """Disques de la VM `vm` (nom) : [{target, source, pool, volume, path,
    format, kind, deletable, reason}]. Supprimable seulement si inscriptible,
    fichier ou volume, cité par aucun autre domaine (directement ou en chaîne
    de sauvegarde) et sans lecture `unknown`. Raisons de conservation :
    `shared with <vm>`, `backing file of <chemin>`, `block device`,
    `network disk`, `read-only or CD-ROM`, `could not prove that no other VM
    uses it`."""
    check_vm_name(vm)
    survey = _Survey(host, exclude=vm)
    if survey.target is None:
        raise OpError("notfound", f"{host.name}: domain {vm} not found")
    return _owned(survey, survey.target)


# --- clonage -----------------------------------------------------------------

def _dest_name(new_name, target, source):
    base = posixpath.basename(source or "")
    _, ext = posixpath.splitext(base)
    return f"{new_name}-{target}{ext}"


def clone_plan(host, name, new_name):
    """Plan d'une copie complète (lecture seule). Rend {"source", "new_name",
    "refusals", "disks": [{target, source, format, mode, pool, directory,
    destination, dest_name, capacity, allocation, needed}], "shared": [...],
    "space": [...], "nics", "nvram", "uefi_entries_lost"}. `refusals` vide =
    possible ; rien n'est écrit."""
    check_vm_name(name)
    check_new_vm_name(new_name)
    out = host.batch({"info": ("dominfo", "--domain", name),
                      "xml": ("dumpxml", "--inactive", "--domain", name),
                      "new": ("dominfo", "--domain", new_name)})
    info = kh.parse_dominfo(_need(out, "info"))
    vm = kh.parse_domain_xml(_need(out, "xml"))
    refusals = []
    if out["new"].ok:
        refusals.append(f"a VM named {new_name} already exists")
    elif out["new"].error.kind != "notfound":
        raise out["new"].error
    if info["state"] != "shut off":
        refusals.append(f"the VM must be shut off to be copied (state: {info['state']})")
    if not info["persistent"]:
        refusals.append("transient VM: only a persistent definition can be copied")
    if vm.get("hostdevs"):
        refusals.append("PCI or USB device attached (hostdev): a copy would claim the same device")
    if vm.get("seclabels_static"):
        refusals.append("static security label: a copy would share it")
    survey = _Survey(host, exclude=name)
    users = survey.users()
    disks, shared = [], []
    for d in vm.get("disks") or []:
        t = d.get("target") or "?"
        if d.get("device", "disk") != "disk" or d.get("readonly"):
            shared.append({"target": t, "source": d.get("source"), "reason": REASON_RO})
            continue
        if not d.get("source"):
            continue
        if d.get("type") in ("block", "nvme"):
            refusals.append(f"block disk {t}: not copied by the console")
            continue
        if d.get("type") in ("network", "vhostuser"):
            refusals.append(f"network disk {t}: not copied by the console")
            continue
        if d.get("type") not in ("file", "volume"):
            refusals.append(f"{d.get('type')} disk {t}: not copied by the console")
            continue
        if d.get("shareable"):
            refusals.append(f"shared disk {t}: a copy would not be shared any more")
            continue
        sharers = sorted({u for i in survey.identities(d) for u in users.get(i, [])})
        if sharers:
            refusals.append(f"disk {t} is shared with {', '.join(sharers)}")
            continue
        loc = survey.locate(d)
        vols = survey.pool_volumes(loc["pool"]) if loc["pool"] else {}
        in_pool = bool(loc["pool"] and loc["volume"] in vols)
        dest_name = _dest_name(new_name, t, loc["volume"] if d.get("type") == "volume"
                               else d.get("source"))
        directory = (survey.pools[loc["pool"]]["target_path"] if in_pool
                     and loc["pool"] in survey.pools else posixpath.dirname(d["source"]))
        entry = {"target": t, "source": d["source"], "type": d.get("type"),
                 "format": d.get("format") or "raw", "mode": "pool" if in_pool else "file",
                 "pool": loc["pool"] if in_pool else None,
                 "volume": loc["volume"] if in_pool else None,
                 "directory": directory, "dest_name": dest_name,
                 "destination": posixpath.join(directory, dest_name) if directory else None,
                 "capacity": None, "allocation": None, "needed": None}
        if in_pool:
            v = vols[loc["volume"]]
            entry.update(capacity=v["capacity"], allocation=v["allocation"])
            by_path = {posixpath.normpath(x["path"]): x for x in vols.values() if x.get("path")}
            total, b, known = v["allocation"] or 0, v.get("backing"), True
            for _ in range(64):         # la copie aplatit la chaîne : on la compte
                if not b:
                    break
                nxt = by_path.get(posixpath.normpath(b))
                if nxt is None:
                    known = False
                    break
                total += nxt["allocation"] or 0
                b = nxt.get("backing")
            cap = v["capacity"] or 0
            entry["needed"] = min(total, cap) if known and cap else cap or total
            if dest_name in vols:
                refusals.append(f"{entry['destination']} already exists")
        disks.append(entry)
    # lectures côté fichier, en un appel : tailles des fichiers hors pool,
    # existence des destinations, droit d'écrire, place des répertoires
    files = [e for e in disks if e["mode"] == "file"]
    dirs = sorted({e["directory"] for e in files})
    cmds = []
    for e in files:
        cmds.append(["qemu-img", "info", "--force-share", "--output=json", "--backing-chain",
                     "--", e["source"]])
        cmds.append(["test", "-e", e["destination"]])
    for dr in dirs:
        cmds.append(["test", "-w", dr])
        cmds.append(["stat", "-f", "-c", "%a %S", "--", dr])
    res = _sh_batch(host, cmds) if cmds else []
    space = {}
    for n, e in enumerate(files):
        rc, text = res[2 * n]
        if rc != 0:
            refusals.append(f"the SSH user cannot read {e['source']}: {_last_line(text)}")
        else:
            try:
                chain = json.loads(text)
                chain = chain if isinstance(chain, list) else [chain]
                cap = chain[0].get("virtual-size")
                alloc = sum(int(c.get("actual-size") or 0) for c in chain)
                e.update(capacity=cap, allocation=chain[0].get("actual-size"),
                         needed=min(alloc, cap) if cap else alloc)
            except (ValueError, AttributeError, IndexError, TypeError):
                refusals.append(f"unreadable image information for {e['source']}")
        if res[2 * n + 1][0] == 0:
            refusals.append(f"{e['destination']} already exists")
    base = 2 * len(files)
    for n, dr in enumerate(dirs):
        if res[base + 2 * n][0] != 0:
            refusals.append(f"the SSH user cannot write {dr}; put the disk in a storage pool "
                            "or grant write access")
        rc, text = res[base + 2 * n + 1]
        fields = text.split()
        avail = (int(fields[0]) * int(fields[1]) if rc == 0 and len(fields) == 2
                 and all(f.isdigit() for f in fields) else None)
        space[("dir", dr)] = {"where": dr, "available": avail, "needed": 0}
    for e in disks:
        if e["mode"] == "pool":
            key = ("pool", e["pool"])
            avail = (survey.pools.get(e["pool"]) or {}).get("available")
            space.setdefault(key, {"where": f"pool {e['pool']}", "available": avail, "needed": 0})
        else:
            key = ("dir", e["directory"])
        space[key]["needed"] += e["needed"] or 0
    for s in space.values():
        if s["available"] is None:
            refusals.append(f"free space unknown in {s['where']}")
        elif s["needed"] > s["available"]:
            refusals.append(f"not enough space in {s['where']}: {s['needed']} bytes needed, "
                            f"{s['available']} available")
    dests = [e["destination"] for e in disks]
    if len(set(dests)) != len(dests):
        refusals.append("two disks would be copied to the same file")
    nv = vm.get("nvram")
    return {"source": name, "new_name": new_name, "refusals": refusals, "disks": disks,
            "shared": shared, "space": list(space.values()), "nics": len(vm.get("nics") or []),
            "nvram": bool(nv), "uefi_entries_lost": bool(nv)}


def volume_xml(name, capacity, fmt):
    """XML du volume de destination d'une copie dans un pool : nom, capacité
    et format, SANS `<backingStore>` : libvirt copie par `qemu-img convert`
    sans image de base, la chaîne est aplatie et le clone ne dépend pas de
    l'image de base de la source. Vu sur libvirt 12.7.0 : `vol-clone` aplatit
    aussi le fichier, mais reprend le XML de la source et annonce encore son
    `<backingStore>` jusqu'au prochain rafraîchissement du pool."""
    vol = ET.Element("volume")
    ET.SubElement(vol, "name").text = name
    cap = ET.SubElement(vol, "capacity", unit="bytes")
    cap.text = str(int(capacity or 0))
    target = ET.SubElement(vol, "target")
    ET.SubElement(target, "format", type=fmt or "raw")
    return ET.tostring(vol, encoding="unicode")


def vol_create_args(entry):
    """`virsh vol-create-from` d'un disque du plan : le XML de destination
    arrive sur l'entrée standard (`--file /dev/stdin`)."""
    return ("vol-create-from", "--pool", entry["pool"], "--file", "/dev/stdin",
            "--vol", entry["volume"], "--inputpool", entry["pool"])


def _register_namespaces(xml_text):
    """Enregistre les préfixes du document pour que ElementTree les garde
    (métadonnées libosinfo et autres)."""
    for _, (prefix, uri) in ET.iterparse(io.StringIO(xml_text), events=("start-ns",)):
        if prefix and not re.fullmatch(r"ns\d+", prefix):
            try:
                ET.register_namespace(prefix, uri)
            except ValueError:
                pass


def clone_xml(xml_text, new_name, disk_map):
    """XML du clone, modifié de façon ciblée (rien d'autre ne change) :
    `<name>`, `<uuid>` retiré, `<title>` suffixé, `<mac>` retirées, sources des
    disques de `disk_map` ({cible: chemin ou nom de volume}) remplacées,
    texte de `<nvram>` vidé (libvirt la recrée depuis le modèle), port VNC ou
    SPICE fixe remplacé par `autoport`, entrée `uuid` de `<sysinfo>` retirée."""
    kh._safe_xml(xml_text)                  # refuse un DOCTYPE avant tout
    _register_namespaces(xml_text)
    root = ET.fromstring(xml_text)
    old_name = root.findtext("name")
    name_el = root.find("name")
    if name_el is None:
        name_el = ET.SubElement(root, "name")
    name_el.text = new_name
    uuid_el = root.find("uuid")
    if uuid_el is not None:
        root.remove(uuid_el)
    title = root.find("title")
    if title is not None and title.text:
        title.text = f"{title.text} (copy of {old_name})"
    devices = root.find("devices")
    for iface in (devices.findall("interface") if devices is not None else []):
        mac = iface.find("mac")
        if mac is not None:
            iface.remove(mac)
    for disk in (devices.findall("disk") if devices is not None else []):
        tgt = disk.find("target")
        t = tgt.get("dev") if tgt is not None else None
        if t not in disk_map:
            continue
        src = disk.find("source")
        if src is None:
            raise OpError("failed", f"disk {t} has no source to replace")
        if disk.get("type") == "volume":
            src.set("volume", disk_map[t])
        else:
            src.set("file", disk_map[t])
        for bs in disk.findall("backingStore"):
            disk.remove(bs)
    nv = root.find("os/nvram")
    if nv is not None:
        nv.text = None
    for g in (devices.findall("graphics") if devices is not None else []):
        if g.get("autoport") != "yes" and kh._int(g.get("port"), -1) > 0:
            for attr in ("port", "tlsPort", "websocket"):
                if attr in g.attrib and kh._int(g.get(attr), -1) > 0:
                    del g.attrib[attr]
            g.set("autoport", "yes")
    system = root.find("sysinfo/system")
    if system is not None:
        for entry in system.findall("entry"):
            if entry.get("name") == "uuid":
                system.remove(entry)
    return ET.tostring(root, encoding="unicode")


# script de copie lancé en arrière-plan sur l'hôte : $1 source, $2 fichier
# temporaire, $3 format ; PID de qemu-img dans $2.pid (ssh sans terminal ne
# transmet pas la coupure : l'annulation l'arrête par ce PID), progression
# dans $2.log, code de sortie dans $2.rc
_COPY_INNER = ('env LC_ALL=C qemu-img convert -p -O "$3" -- "$1" "$2" >"$2.log" 2>&1 & '
               'p=$!; echo "$p" >"$2.pid"; wait "$p"; echo "$?" >"$2.rc"')
_COPY_START = ('inner=$1; shift; nohup sh -c "$inner" sh "$@" </dev/null >/dev/null 2>&1 & '
               'echo started')
_COPY_POLL = ('t=$1; printf "rc=%s\\n" "$(cat "$t.rc" 2>/dev/null)"; '
              'printf "pid=%s\\n" "$(cat "$t.pid" 2>/dev/null)"; '
              'printf "progress=%s\\n" "$(tr "\\r" "\\n" <"$t.log" 2>/dev/null '
              '| grep -o "[0-9.]*/100%" | tail -n 1)"; '
              'printf "last=%s\\n" "$(tr "\\r" "\\n" <"$t.log" 2>/dev/null | grep -v "/100%" '
              '| grep . | tail -n 1)"')
# arrête qemu-img (s'il tourne encore) puis retire les fichiers de travail
_COPY_KILL = ('t=$1; n=0; while [ ! -e "$t.pid" ] && [ ! -e "$t.rc" ] && [ $n -lt 10 ]; '
              'do sleep 0.2; n=$((n+1)); done; p=$(cat "$t.pid" 2>/dev/null); '
              'if [ -n "$p" ] && [ ! -e "$t.rc" ]; then kill "$p" 2>/dev/null; '
              'n=0; while [ ! -e "$t.rc" ] && [ $n -lt 50 ]; do sleep 0.2; n=$((n+1)); done; fi; '
              'rm -f -- "$t" "$t.pid" "$t.log" "$t.rc"; [ ! -e "$t" ]')
# le lien dur échoue si la destination est apparue : jamais d'écriture par-dessus
_COPY_COMMIT = 'ln -T -- "$1" "$2" && rm -f -- "$1" "$1.pid" "$1.log" "$1.rc"'


def _poll_fields(text):
    return dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln)


def _copy_file(host, entry, created, deadline, progress, sleep, poll):
    """Copie d'un fichier hors pool par le compte SSH : qemu-img vers un
    fichier temporaire du répertoire de destination, suivi par sondage, puis
    lien dur vers la destination."""
    tmp = posixpath.join(entry["directory"],
                         f".{entry['dest_name']}.hops-{secrets.token_hex(6)}.tmp")
    created.append({"kind": "tmp", "path": tmp})
    host.run(["sh", "-c", _COPY_START, "sh", _COPY_INNER, entry["source"], tmp, entry["format"]])
    sid = f"copy-{entry['target']}"
    while True:
        f = _poll_fields(host.run(["sh", "-c", _COPY_POLL, "sh", tmp]))
        rc = f.get("rc", "").strip()
        if rc:
            break
        pct = re.match(r"([0-9.]+)/100%", f.get("progress", "").strip())
        if pct and progress:
            done = float(pct.group(1))
            copied = int((entry["capacity"] or 0) * done / 100)
            progress(sid, "running", f"{done:.0f}% ({copied} of {entry['capacity']} bytes)")
        if time.monotonic() > deadline:
            raise OpError("timeout", f"copy of {entry['target']} did not finish in time")
        sleep(poll)
    if rc != "0":
        raise OpError("failed", f"copy of {entry['target']} failed: "
                      f"{f.get('last', '').strip() or 'qemu-img exited with status ' + rc}")
    try:
        host.run(["sh", "-c", _COPY_COMMIT, "sh", tmp, entry["destination"]])
    except kh.KvmError as e:
        raise OpError("failed", f"{entry['destination']} could not be created "
                      f"(it may have appeared meanwhile): {e.message}") from None
    created[-1] = {"kind": "file", "path": entry["destination"]}


def _rollback(host, created):
    """Défait ce qui a été créé, dans l'ordre inverse ; rend ce qui reste."""
    leftovers = []
    for item in reversed(created):
        try:
            if item["kind"] == "domain":
                xml = host.virsh("dumpxml", "--inactive", "--domain", item["name"])
                vm = kh.parse_domain_xml(xml)
                mine = {d.get("source") for d in vm["disks"]} >= set(item["sources"])
                if not mine:                # ce n'est pas notre clone : on n'y touche pas
                    continue
                args = ["undefine", "--domain", item["name"]]
                if vm.get("nvram"):
                    args.append("--nvram")
                host.virsh(*args)
            elif item["kind"] == "volume":
                host.virsh("vol-delete", "--pool", item["pool"], "--vol", item["volume"])
            elif item["kind"] == "file":
                host.run(["rm", "-f", "--", item["path"]])
            elif item["kind"] == "tmp":
                host.run(["sh", "-c", _COPY_KILL, "sh", item["path"]])
        except kh.KvmError as e:
            if e.kind == "notfound" and item["kind"] in ("domain", "volume"):
                continue                     # déjà absent
            leftovers.append({**{k: v for k, v in item.items() if k != "sources"},
                              "error": e.message})
        except (OpError, ET.ParseError, ValueError) as e:
            leftovers.append({**{k: v for k, v in item.items() if k != "sources"},
                              "error": str(e)})
    return leftovers


def clone_vm(host, name, new_name, timeout=3600, progress=None, sleep=time.sleep,
             poll=COPY_POLL):
    """Copie complète d'une VM arrêtée : plan recalculé, volumes copiés
    (`vol-create-from` dans un pool, chaîne aplatie ; `qemu-img convert` hors
    pool), XML modifié par
    `clone_xml`, `virsh define /dev/stdin`. `progress(étape, statut, message)`
    reçoit `plan`, `copy-<cible>`, `define`, `verify`.

    Sur échec ou annulation (toute exception, y compris BaseException), retour
    arrière au mieux ; ce qui n'a pas pu être retiré est attaché à l'exception
    (`exc.leftovers`, et `data["leftovers"]` pour une OpError)."""
    deadline = time.monotonic() + timeout

    def step(sid, status, msg=""):
        if progress:
            progress(sid, status, msg)

    step("plan", "running", f"{name} -> {new_name}")
    plan = clone_plan(host, name, new_name)
    if plan["refusals"]:
        step("plan", "failed", "; ".join(plan["refusals"]))
        raise OpError("refused", "; ".join(plan["refusals"]), {"refusals": plan["refusals"],
                                                                "plan": plan})
    step("plan", "done", f"{len(plan['disks'])} disk(s) to copy")
    xml = host.virsh("dumpxml", "--inactive", "--security-info", "--domain", name)
    created, disk_map, copies, sources = [], {}, [], []
    try:
        for e in plan["disks"]:
            sid = f"copy-{e['target']}"
            step(sid, "running", f"{e['source']} -> {e['destination']}")
            if e["mode"] == "pool":
                created.append({"kind": "volume", "pool": e["pool"], "volume": e["dest_name"]})
                left = max(1, int(deadline - time.monotonic()))
                try:
                    host.run_input(["env", "LC_ALL=C", *host._virsh_argv(vol_create_args(e))],
                                   volume_xml(e["dest_name"], e["capacity"], e["format"]),
                                   timeout=left)
                except kh.KvmError as err:
                    if "already" in err.message.lower() or "exists" in err.message.lower():
                        created.pop()        # pas le nôtre : jamais supprimé
                    raise
                path = host.virsh("vol-path", "--pool", e["pool"], "--vol", e["dest_name"]).strip()
                disk_map[e["target"]] = e["dest_name"] if e["type"] == "volume" else path
                # source telle que parse_domain_xml la lira dans le clone
                sources.append(f"{e['pool']}/{e['dest_name']}" if e["type"] == "volume" else path)
                copies.append({"target": e["target"], "destination": path, "pool": e["pool"]})
            else:
                _copy_file(host, e, created, deadline, progress, sleep, poll)
                disk_map[e["target"]] = e["destination"]
                sources.append(e["destination"])
                copies.append({"target": e["target"], "destination": e["destination"],
                               "pool": None})
            step(sid, "done", copies[-1]["destination"])
        step("define", "running", new_name)
        new_xml = clone_xml(xml, new_name, disk_map)
        created.append({"kind": "domain", "name": new_name, "sources": sources})
        host.run_input(["env", "LC_ALL=C", *host._virsh_argv(("define", "/dev/stdin"))], new_xml)
        step("define", "done", new_name)
        step("verify", "running", new_name)
        out = host.batch({"info": ("dominfo", "--domain", new_name),
                          "xml": ("dumpxml", "--inactive", "--domain", new_name)})
        info = kh.parse_dominfo(_need(out, "info"))
        clone = kh.parse_domain_xml(_need(out, "xml"))
        step("verify", "done", f"uuid {clone['uuid']}")
    except BaseException as exc:
        leftovers = _rollback(host, created)
        exc.leftovers = leftovers
        if isinstance(exc, OpError):
            exc.data["leftovers"] = leftovers
        raise
    return {"source": name, "name": new_name, "uuid": clone["uuid"], "state": info["state"],
            "macs": [n.get("mac") for n in clone["nics"]], "disks": copies,
            "shared": plan["shared"], "nvram": bool(plan["nvram"]),
            "uefi_entries_lost": plan["uefi_entries_lost"], "leftovers": []}


# --- suppression ---------------------------------------------------------------

def delete_plan(host, name, remove_storage=False):
    """Plan de suppression (lecture seule). Rend {"name", "state",
    "persistent", "refusals", "internal_snapshots", "external_snapshots",
    "managed_save", "nvram", "remove_storage", "delete": [disques], "keep":
    [disques avec raison], "notes"}."""
    check_vm_name(name)
    out = host.batch({"info": ("dominfo", "--domain", name),
                      "xml": ("dumpxml", "--inactive", "--domain", name),
                      "internal": ("snapshot-list", "--domain", name, "--internal", "--name"),
                      "external": ("snapshot-list", "--domain", name, "--external", "--name")})
    info = kh.parse_dominfo(_need(out, "info"))
    vm = kh.parse_domain_xml(_need(out, "xml"))
    internal = kh.parse_names(_need(out, "internal"))
    external = kh.parse_names(_need(out, "external"))
    refusals = []
    if not info["persistent"]:
        refusals.append("transient: destroy removes it")
    elif info["state"] != "shut off":
        refusals.append(f"the VM is {info['state']}: shut it down or force it off first")
    if external:
        refusals.append("this VM uses external snapshots: not managed by the console "
                        "in this version")
    notes = []
    if remove_storage:
        survey = _Survey(host, exclude=name)
        disks = _owned(survey, survey.target or vm)
    else:
        disks = [{"target": d.get("target"), "source": d.get("source"), "pool": None,
                  "volume": None, "path": d.get("source"), "format": d.get("format"),
                  "kind": d.get("type"), "deletable": False, "reason": REASON_NOT_REQUESTED}
                 for d in vm.get("disks") or [] if d.get("source")]
        if internal:
            notes.append("internal snapshot data stays in the kept disk files "
                         "(qemu-img snapshot -l)")
    nv = vm.get("nvram")
    return {"name": name, "state": info["state"], "persistent": info["persistent"],
            "refusals": refusals, "internal_snapshots": len(internal),
            "external_snapshots": len(external), "managed_save": info["managed_save"],
            "nvram": (nv.get("path") or True) if nv else None,
            "remove_storage": bool(remove_storage),
            "delete": [d for d in disks if d["deletable"]],
            "keep": [d for d in disks if not d["deletable"] and d.get("source")],
            "notes": notes}


def undefine_argv(name, plan):
    """Arguments virsh du `undefine` d'un plan : `--storage` ne nomme QUE les
    disques supprimables qui sont des volumes de pool (jamais
    `--remove-all-storage`, qui viserait aussi un disque partagé)."""
    args = ["undefine", "--domain", name, "--managed-save", "--snapshots-metadata",
            "--checkpoints-metadata"]
    if plan.get("nvram"):
        args.append("--nvram")
    vols = [d["target"] for d in plan["delete"] if d.get("volume")]
    if vols:
        args += ["--storage", ",".join(vols)]
    return args


def delete_vm(host, name, remove_storage=False, timeout=300):
    """Supprime une VM arrêtée et persistante (plan recalculé ici). Les
    volumes possédés partent par `undefine --storage`, les fichiers possédés
    hors pool par `rm` après le `undefine` (si `test -f`). Rend {"name",
    "undefined", "deleted", "kept", "leftovers", "notes"}."""
    plan = delete_plan(host, name, remove_storage)
    if plan["refusals"]:
        raise OpError("refused", "; ".join(plan["refusals"]), {"refusals": plan["refusals"],
                                                                "plan": plan})
    error = None
    try:
        host.virsh(*undefine_argv(name, plan), timeout=timeout)
    except kh.KvmError as e:
        error = e                         # le domaine peut être parti malgré tout
    gone = False
    try:
        host.virsh("dominfo", "--domain", name)
    except kh.KvmError as e:
        if e.kind != "notfound":
            raise
        gone = True
    if not gone:
        raise error or OpError("failed", f"{name} is still defined after undefine")
    files = [d for d in plan["delete"] if not d.get("volume")]
    res = _sh_batch(host, [["sh", "-c", 'test -f "$1" && rm -f -- "$1"; [ ! -e "$1" ]', "sh",
                            d["path"]] for d in files]) if files else []
    deleted, leftovers = [], []
    for d, (rc, text) in zip(files, res):
        if rc == 0:
            deleted.append(d)
        else:
            leftovers.append({**d, "error": _last_line(text) or "could not remove the file"})
    vols = [d for d in plan["delete"] if d.get("volume")]
    checks = _sh_batch(host, [["test", "!", "-e", d["path"]] for d in vols if d.get("path")])
    it = iter(checks)
    for d in vols:
        ok = next(it)[0] == 0 if d.get("path") else error is None
        if ok:
            deleted.append(d)
        else:
            leftovers.append({**d, "error": (error.message if error else
                                             "the volume is still present")})
    return {"name": name, "undefined": True, "deleted": deleted, "kept": plan["keep"],
            "leftovers": leftovers, "notes": plan["notes"]}
