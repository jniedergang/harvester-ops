"""v1.97.0 : opérations composées (bin/lib/kvm_ops.py).

Deux étages :
- `Sim`, un petit libvirt simulé au niveau de l'API de VirshHost (virsh,
  batch, run, run_input) : domaines, pools, volumes, fichiers. Il comprend
  les scripts shell réellement envoyés par kvm_ops (lots `_r`, copie en
  arrière-plan) en les redécoupant avec shlex ;
- les scripts de copie et de lot joués pour de vrai dans un `sh` local, avec
  un faux `qemu-img` en tête du PATH (VirshHost réel, ssh remplacé).
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid as uuidlib
import posixpath
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import kvm_host as kh  # noqa: E402
import kvm_ops as ko  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "kvm"
FAKE_PASSWORD = "Fk7pw3Zq"
IMG = "/var/lib/libvirt/images"


def fx(name):
    return (FIX / name).read_text()


# --- fabrique de XML ----------------------------------------------------------

def disk(target, src, type="file", device="disk", fmt="qcow2", ro=False, share=False,
         transient=False, chain=()):
    if type == "file":
        source = f"<source file='{src}'/>" if src else ""
    elif type == "block":
        source = f"<source dev='{src}'/>"
    elif type == "volume":
        pool, vol = src.split("/", 1)
        source = f"<source pool='{pool}' volume='{vol}'/>"
    else:
        source = f"<source protocol='rbd' name='{src}'/>"
    bs = "".join(f"<backingStore type='file'><format type='qcow2'/><source file='{c}'/>"
                 for c in chain) + "<backingStore/>" * bool(chain) + "</backingStore>" * len(chain)
    flags = "<readonly/>" * ro + "<shareable/>" * share + "<transient/>" * transient
    return (f"<disk type='{type}' device='{device}'><driver name='qemu' type='{fmt}'/>{source}"
            f"{bs}<target dev='{target}' bus='virtio'/>{flags}</disk>")


def dom(name, disks=(), nics=1, graphics="", nvram=None, extra_dev="", extra="", uid=None,
        live_id=None):
    nv = ""
    if nvram is not None:
        nv = (f"<nvram template='/usr/share/qemu/ovmf-x86_64-4m-vars.qcow2' "
              f"templateFormat='qcow2' format='{nvram}'>/var/lib/libvirt/qemu/nvram/"
              f"{name}_VARS.{nvram}</nvram>")
    macs = "".join(f"<interface type='network'><mac address='52:54:00:00:00:{i:02x}'/>"
                   f"<source network='default'/><model type='virtio'/></interface>"
                   for i in range(nics))
    ident = f" id='{live_id}'" if live_id else ""
    return (f"<domain type='kvm'{ident}><name>{name}</name><uuid>{uid or uuidlib.uuid4()}</uuid>"
            f"<memory unit='KiB'>1048576</memory><os><type arch='x86_64'>hvm</type>{nv}</os>"
            f"{extra}<devices>{''.join(disks)}{macs}{graphics}{extra_dev}</devices></domain>")


def table(headers, rows):
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    line = lambda cells: " " + "   ".join(c.ljust(w) for c, w in zip(cells, widths))  # noqa: E731
    return "\n".join([line(headers), "-" * (sum(widths) + 3 * len(widths)),
                      *[line(r) for r in rows], ""])


def dominfo(name, state, persistent=True, uid="u", managed_save=False):
    return (f"Id:             {'1' if state == 'running' else '-'}\nName:           {name}\n"
            f"UUID:           {uid}\nState:          {state}\n"
            f"Persistent:     {'yes' if persistent else 'no'}\nAutostart:      disable\n"
            f"Managed save:   {'yes' if managed_save else 'no'}\n")


# --- libvirt simulé --------------------------------------------------------------

class Sim:
    """Hôte simulé : domaines, pools, volumes, fichiers. `calls` garde chaque
    appel (`virsh`, `batch`, `run`, `input`)."""

    name = "node2"

    def __init__(self):
        self.decl = {"uri": "qemu:///system"}
        self.domains = {}
        self.pools = {}
        self.files = {}          # chemin -> sortie json de qemu-img (liste) ; None = illisible
        #                          (fichier non déclaré : image sans base)
        self.inodes = {}         # chemin -> « dev:ino » imposé (liens) ; défaut : propre au chemin
        self.missing = set()     # chemins absents pour stat -L
        self.stat_fail = set()   # chemins dont stat -L échoue (droits)
        self.stat_errors = {}    # chemin -> texte d'erreur stat -L (toute autre cause, rc=1)
        self.qemu_errors = {}    # chemin -> texte d'erreur qemu-img info (toute autre cause, rc=1)
        self.qemu_garbage = {}   # chemin -> sortie non-JSON de qemu-img info (rc=0)
        self.no_output = set()   # chemin -> ni stat -L ni qemu-img info ne répond (délai dépassé)
        self.fstypes = {}        # répertoire -> type de `stat -f -c %T`
        self.exists = set()      # chemins vus par `test -e`
        self.writable = set()    # répertoires où le compte SSH écrit
        self.statf = {}          # répertoire -> "bavail bsize"
        self.rm_fail = set()
        self.calls = []
        self.fail = {}           # premier mot virsh -> (rc, stderr)
        self.copy_rc = "0"
        self.copy_polls = 2
        self.on_copy_poll = None
        self.copies = {}
        self.commit_out = "linked\nsrc=1000:100:644\ndst=1000:100:644\n"
        self.vol_perms = ("0644", "107", "107")

    # -- déclaration du monde
    def add_domain(self, name, xml, state="shut off", persistent=True, live=None, snaps=(),
                   current=None):
        self.domains[name] = {"xml": xml, "live": live, "state": state, "persistent": persistent,
                              "snaps": list(snaps), "current": current}

    def add_pool(self, name, path, available=10 ** 12, type="dir"):
        self.pools[name] = {"path": path, "available": available, "type": type, "vols": {}}

    def add_vol(self, pool, name, cap=64 << 20, alloc=1 << 20, fmt="qcow2", backing=None):
        path = f"{self.pools[pool]['path']}/{name}"
        self.pools[pool]["vols"][name] = {"path": path, "cap": cap, "alloc": alloc, "fmt": fmt,
                                          "backing": backing}
        self.exists.add(path)

    # -- API de VirshHost
    def _virsh_argv(self, args):
        return ["virsh", "-c", "qemu:///system", *map(str, args)]

    def _answer(self, args, data=None):
        args = list(map(str, args))
        cmd = args[0]
        if cmd in self.fail:
            return (*self.fail[cmd][:1], "", self.fail[cmd][1])
        handler = getattr(self, "v_" + cmd.replace("-", "_"), None)
        if handler is None:
            return (1, "", f"error: unexpected command {cmd}\n")
        opts, pos, i = {}, [], 1
        while i < len(args):
            a = args[i]
            valued = ("--domain", "--pool", "--vol", "--snapshotname", "--description",
                      "--file", "--inputpool", "--storage", "--newname")
            # `--name` : valeur pour snapshot-create-as, drapeau pour les listes
            if a in valued or (a == "--name" and cmd == "snapshot-create-as"):
                opts[a[2:]] = args[i + 1]
                i += 2
            elif a.startswith("--"):
                opts[a[2:]] = True
                i += 1
            else:
                pos.append(a)
                i += 1
        return handler(opts, pos, data)

    def virsh(self, *args, timeout=None):
        self.calls.append(("virsh", tuple(map(str, args))))
        rc, out, err = self._answer(args)
        if rc:
            raise kh.make_error(self.name, err, rc)
        return out

    def batch(self, commands, timeout=None, cmd_timeout=None):
        res = {}
        for k, args in commands.items():
            self.calls.append(("batch", tuple(map(str, args))))
            rc, out, err = self._answer(args)
            res[k] = kh.CmdOutput(out) if rc == 0 else kh.CmdOutput(
                err, rc=rc, error=kh.make_error(self.name, err, rc))
        return res

    def run_input(self, argv, data, timeout=None):
        self.calls.append(("input", tuple(argv), data))
        assert argv[:5] == ["env", "LC_ALL=C", "virsh", "-c", "qemu:///system"]
        rc, out, err = self._answer(argv[5:], data)
        if rc:
            raise kh.make_error(self.name, err, rc)
        return out

    def run(self, argv, timeout=None):
        self.calls.append(("run", tuple(argv)))
        if argv[:2] == ["sh", "-c"] and "\n_r " in argv[2]:
            return self._sh_batch(argv[2])
        if argv[:2] == ["sh", "-c"] and argv[2] == ko._COPY_START:
            _, src, tmp, fmt = argv[4:]
            self.copies[tmp] = {"src": src, "fmt": fmt, "polls": 0, "killed": False}
            return "started\n"
        if argv[:2] == ["sh", "-c"] and argv[2] == ko._COPY_POLL:
            c = self.copies[argv[4]]
            c["polls"] += 1
            if self.on_copy_poll:
                self.on_copy_poll(c)
            if c["polls"] < self.copy_polls:
                return "rc=\npid=4242\nprogress=50.00/100%\nlast=\n"
            return f"rc={self.copy_rc}\npid=4242\nprogress=100.00/100%\nlast=boom: no space\n"
        if argv[:2] == ["sh", "-c"] and argv[2] == ko._COPY_COMMIT:
            tmp, dest, src = argv[4:]
            if dest in self.exists:
                raise kh.make_error(self.name, "ln: failed to create hard link: File exists", 3)
            self.exists.add(dest)
            self.copies[tmp]["committed"] = dest
            return self.commit_out
        if argv[:2] == ["sh", "-c"] and argv[2] == ko._COPY_UNCOMMIT:
            self.copies[argv[4]]["killed"] = True
            return ""
        if argv[:2] == ["sh", "-c"] and argv[2] == ko._COPY_KILL:
            self.copies[argv[4]]["killed"] = True
            return ""
        if argv[:3] == ["rm", "-f", "--"]:
            self.exists.discard(argv[3])
            return ""
        raise AssertionError(f"unexpected run {argv}")

    def _sh_batch(self, script):
        token = re.search(r"^T=(@@kvmops-[0-9a-f]+)$", script, re.M).group(1)
        out = ""
        for chunk in re.split(r"\n_r (?=\d+ )", script)[1:]:
            words = shlex.split(chunk)
            i, argv = int(words[0]), words[1:]
            rc, text = self._shell(argv)
            if rc is None:
                continue              # délai dépassé : jamais de ligne de repère (`_sh_batch` réel
                                       # rend alors (-1, "no output") pour cet indice)
            out += f"{text}\n{token} {i} {rc}\n"
        return out

    def _shell(self, argv):
        if argv[:2] == ["qemu-img", "info"]:
            f = argv[-1]
            if f in self.no_output:
                return None, None
            if f in self.missing:
                return 1, f"qemu-img: Could not open '{f}': No such file or directory"
            if f in self.qemu_errors:
                return 1, self.qemu_errors[f]
            if f in self.qemu_garbage:
                return 0, self.qemu_garbage[f]
            if f in self.files and self.files[f] is None:
                return 1, f"qemu-img: Could not open '{f}': Permission denied"
            return 0, json.dumps(self.files.get(f) or [{"filename": f}])
        if argv[:4] == ["stat", "-L", "-c", "%d:%i"]:
            f = argv[-1]
            if f in self.no_output:
                return None, None
            if f in self.missing:
                return 1, f"stat: cannot statx '{f}': No such file or directory"
            if f in self.stat_errors:
                return 1, self.stat_errors[f]
            if f in self.stat_fail:
                return 1, f"stat: cannot statx '{f}': Permission denied"
            return 0, self.inodes.get(f) or f"64769:{zlib.crc32(f.encode())}"
        if argv[:4] == ["stat", "-f", "-c", "%T"]:
            f = argv[-1]
            if f in self.missing:
                return 1, f"stat: cannot read file system information for '{f}': No such file"
            return 0, self.fstypes.get(posixpath.dirname(f), "xfs") + "\n"
        if argv[:2] == ["test", "-e"]:
            return (0 if argv[2] in self.exists else 1), ""
        if argv[:3] == ["test", "!", "-e"]:
            return (1 if argv[3] in self.exists else 0), ""
        if argv[:2] == ["test", "-w"]:
            return (0 if argv[2] in self.writable else 1), ""
        if argv[:2] == ["stat", "-f"]:
            return (0, self.statf[argv[-1]]) if argv[-1] in self.statf else (1, "stat: no")
        if argv[:2] == ["sh", "-c"] and "rm -f" in argv[2]:
            path = argv[4]
            if path in self.rm_fail:
                return 1, f"rm: cannot remove '{path}': Permission denied"
            self.exists.discard(path)
            return 0, ""
        raise AssertionError(f"unexpected shell command {argv}")

    # -- commandes virsh simulées
    def _dom(self, name):
        d = self.domains.get(name)
        if d is None:
            return None
        return d

    def _nodom(self, name):
        return 1, "", f"error: failed to get domain '{name}'\n"

    def v_list(self, o, p, data):
        names = [n for n, d in self.domains.items()
                 if o.get("all") or d["state"] != "shut off"]
        return 0, "".join(f"{n}\n" for n in names) + "\n", ""

    def v_pool_list(self, o, p, data):
        return 0, "".join(f"{n}\n" for n in self.pools) + "\n", ""

    def v_dumpxml(self, o, p, data):
        d = self._dom(o["domain"])
        if d is None:
            return self._nodom(o["domain"])
        xml = d["xml"] if o.get("inactive") or d["state"] == "shut off" else (d["live"] or d["xml"])
        if not o.get("security-info"):
            xml = re.sub(r" passwd='[^']*'", "", xml)
        return 0, xml, ""

    def v_dominfo(self, o, p, data):
        d = self._dom(o["domain"])
        if d is None:
            return self._nodom(o["domain"])
        uid = kh.parse_domain_xml(d["xml"])["uuid"]
        return 0, dominfo(o["domain"], d["state"], d["persistent"], uid,
                          d.get("managed_save", False)), ""

    def v_domstate(self, o, p, data):
        d = self._dom(o["domain"])
        return (0, d["state"] + "\n\n", "") if d else self._nodom(o["domain"])

    def v_pool_dumpxml(self, o, p, data):
        pl = self.pools[o["pool"]]
        return 0, (f"<pool type='{pl['type']}'><name>{o['pool']}</name>"
                   f"<available unit='bytes'>{pl['available']}</available>"
                   f"<target><path>{pl['path']}</path></target></pool>"), ""

    def v_vol_list(self, o, p, data):
        vols = self.pools[o["pool"]]["vols"]
        rows = [[n, v["path"], "file", "64.00 MiB", "1.00 MiB"] for n, v in vols.items()]
        return 0, table(["Name", "Path", "Type", "Capacity", "Allocation"], rows), ""

    def v_vol_dumpxml(self, o, p, data):
        v = self.pools[o["pool"]]["vols"].get(o["vol"])
        if v is None:
            return 1, "", f"error: failed to get vol '{o['vol']}'\n"
        mode, owner, group = self.vol_perms
        bs = (f"<backingStore><path>{v['backing']}</path><format type='qcow2'/></backingStore>"
              if v["backing"] else "")
        return 0, (f"<volume type='file'><name>{o['vol']}</name><key>{v['path']}</key>"
                   f"<capacity unit='bytes'>{v['cap']}</capacity>"
                   f"<allocation unit='bytes'>{v['alloc']}</allocation>"
                   f"<target><path>{v['path']}</path><format type='{v['fmt']}'/>"
                   f"<permissions><mode>{mode}</mode><owner>{owner}</owner>"
                   f"<group>{group}</group></permissions></target>"
                   f"{bs}</volume>"), ""

    def v_vol_path(self, o, p, data):
        return 0, self.pools[o["pool"]]["vols"][o["vol"]]["path"] + "\n", ""

    def v_vol_create_from(self, o, p, data):
        assert o["file"] == "/dev/stdin" and data is not None
        root = ET.fromstring(data)
        name = root.findtext("name")
        if name in self.pools[o["pool"]]["vols"]:
            return 1, "", f"error: storage volume name '{name}' already in use.\n"
        src = self.pools[o["inputpool"]]["vols"][o["vol"]]
        self.add_vol(o["pool"], name, cap=int(root.findtext("capacity")), alloc=src["alloc"],
                     fmt=root.find("target/format").get("type"))
        return 0, f"Vol {name} created from input vol {o['vol']}\n", ""

    def v_vol_delete(self, o, p, data):
        vols = self.pools[o["pool"]]["vols"]
        if o["vol"] not in vols:
            return 1, "", f"error: failed to get vol '{o['vol']}'\n"
        self.exists.discard(vols.pop(o["vol"])["path"])
        return 0, "", ""

    def v_define(self, o, p, data):
        assert p == ["/dev/stdin"] and data is not None
        root = ET.fromstring(data)
        name = root.findtext("name")
        if name in self.domains:
            return 1, "", f"error: operation failed: domain '{name}' already exists\n"
        if root.find("uuid") is None:
            ET.SubElement(root, "uuid").text = str(uuidlib.uuid4())
        for n, iface in enumerate(root.findall("devices/interface")):
            if iface.find("mac") is None:
                ET.SubElement(iface, "mac", address=f"52:54:00:ab:cd:{n:02x}")
        self.add_domain(name, ET.tostring(root, encoding="unicode"))
        return 0, f"Domain '{name}' defined from /dev/stdin\n", ""

    def v_undefine(self, o, p, data):
        name = o["domain"]
        d = self._dom(name)
        if d is None:
            return self._nodom(name)
        vm = kh.parse_domain_xml(d["xml"])
        if vm["nvram"] and not o.get("nvram"):
            return 1, "", "error: Requested operation is not valid: cannot undefine domain with nvram\n"
        for t in (o.get("storage") or "").split(","):
            for dk in vm["disks"]:
                if t and dk["target"] == t:
                    for pl in self.pools.values():
                        for vn, v in list(pl["vols"].items()):
                            if v["path"] == dk["source"]:
                                del pl["vols"][vn]
                                self.exists.discard(v["path"])
        del self.domains[name]
        return 0, f"Domain '{name}' has been undefined\n", ""

    def v_snapshot_list(self, o, p, data):
        d = self._dom(o["domain"])
        if d is None:
            return self._nodom(o["domain"])
        snaps = d["snaps"]
        if o.get("internal") or o.get("external"):
            loc = "internal" if o.get("internal") else "external"
            return 0, "".join(f"{s[0]}\n" for s in snaps if s[3] == loc) + "\n", ""
        rows = [[s[0], "2026-10-24 11:39:00 +0200", s[1], s[2] or ""] for s in snaps]
        return 0, table(["Name", "Creation Time", "State", "Parent"], rows), ""

    def v_snapshot_current(self, o, p, data):
        d = self._dom(o["domain"])
        if d["current"] is None:
            return 1, "", f"error: domain '{o['domain']}' has no current snapshot\n"
        return 0, d["current"] + "\n", ""

    def v_snapshot_create_as(self, o, p, data):
        d = self._dom(o["domain"])
        state = "running" if d["state"] == "running" else "shutoff"
        d["snaps"].append((o["name"], state, d["current"], "internal"))
        d["current"] = o["name"]
        return 0, f"Domain snapshot {o['name']} created\n", ""

    def v_snapshot_revert(self, o, p, data):
        d = self._dom(o["domain"])
        snap = next(s for s in d["snaps"] if s[0] == o["snapshotname"])
        d["state"] = "running" if snap[1] == "running" else "shut off"
        d["current"] = snap[0]
        return 0, "", ""

    def v_snapshot_delete(self, o, p, data):
        d = self._dom(o["domain"])
        snap = next(s for s in d["snaps"] if s[0] == o["snapshotname"])
        d["snaps"] = [(s[0], s[1], snap[2] if s[2] == snap[0] else s[2], s[3])
                      for s in d["snaps"] if s[0] != snap[0]]
        if d["current"] == snap[0]:
            d["current"] = snap[2]
        return 0, f"Domain snapshot {snap[0]} deleted\n", ""

    # -- aides de lecture des appels
    def virsh_calls(self, first=None):
        return [c[1] for c in self.calls if c[0] in ("virsh", "batch")
                and (first is None or c[1][0] == first)]


# --- noms -----------------------------------------------------------------------

@pytest.mark.parametrize("name,ok", [
    ("vm", True), ("a;rm -rf /", True), ("$(id)", True), ("-x", True), ("x y", True),
    ("é" * 255, True), ("", False), ("a\nb", False), ("a\tb", False), ("x" * 256, False),
    (None, False), (3, False), ("a\x7fb", False),
])
def test_check_vm_name(name, ok):
    if ok:
        assert ko.check_vm_name(name) == name
    else:
        with pytest.raises(ko.OpError) as e:
            ko.check_vm_name(name)
        assert e.value.kind == "invalid"


@pytest.mark.parametrize("name,ok", [
    ("kvmtest-2", True), ("a;b", True), ("-x", True), ("a/b", False), (".hidden", False),
    ("..", False), ("", False),
])
def test_check_new_vm_name(name, ok):
    if ok:
        assert ko.check_new_vm_name(name) == name
    else:
        with pytest.raises(ko.OpError):
            ko.check_new_vm_name(name)


@pytest.mark.parametrize("name,new,ok", [
    ("s1", True, True), ("a.b_c-d", True, True), ("x" * 64, True, True),
    ("x" * 65, True, False), ("-x", True, False), ("a b", True, False), (".a", True, False),
    ("", True, False), ("é", True, False),
    ("-z w", False, True), ("$(id)", False, True), ("", False, False), ("a\nb", False, False),
])
def test_check_snapshot_name(name, new, ok):
    if ok:
        assert ko.check_snapshot_name(name, new=new) == name
    else:
        with pytest.raises(ko.OpError) as e:
            ko.check_snapshot_name(name, new=new)
        assert e.value.kind == "invalid"


# --- display_target ----------------------------------------------------------------

def vnc(listen=None, port=5900, listen_type=None, passwd=False):
    attrs = f" port='{port}'" if port is not None else ""
    attrs += f" listen='{listen}'" if listen and not listen_type else ""
    attrs += f" passwd='{FAKE_PASSWORD}'" if passwd else ""
    sub = ""
    if listen_type == "none":
        sub = "<listen type='none'/>"
    elif listen_type == "network":
        sub = f"<listen type='network' network='default' address='{listen}'/>"
    return f"<graphics type='vnc' autoport='yes'{attrs}>{sub}</graphics>"


SPICE = "<graphics type='spice' port='5930' autoport='yes' listen='127.0.0.1'/>"

DISPLAY_CASES = [
    ("loopback fixture", fx("dumpxml-vnc-password.xml"), True,
     {"type": "vnc", "address": "127.0.0.1", "port": 5900, "password": True}),
    ("joker v4 fixture", fx("dumpxml-kvmtest-uefi.xml"), True,
     {"type": "vnc", "address": "127.0.0.1", "port": 5901, "password": False}),
    ("joker v6", dom("j", graphics=vnc("::")), True,
     {"type": "vnc", "address": "127.0.0.1", "port": 5900, "password": False}),
    ("loopback v6", dom("l", graphics=vnc("::1")), True,
     {"type": "vnc", "address": "::1", "port": 5900, "password": False}),
    ("host IP", dom("h", graphics=vnc("172.16.1.12", 5905)), True,
     {"type": "vnc", "address": "172.16.1.12", "port": 5905, "password": False}),
    ("IP of another machine", dom("h", graphics=vnc("172.16.1.11", 5905)), True,
     {"reason": "listen"}),
    ("network listen", dom("n", graphics=vnc("192.168.122.1", listen_type="network")), True,
     {"type": "vnc", "address": "192.168.122.1", "port": 5900, "password": False}),
    ("network listen without address",
     dom("n", graphics="<graphics type='vnc' port='5900' autoport='yes'>"
                       "<listen type='network' network='default'/></graphics>"), True,
     {"reason": "listen"}),
    ("vnc then spice", dom("s", graphics=vnc("127.0.0.1", 5908) + SPICE), True,
     {"type": "vnc", "address": "127.0.0.1", "port": 5908, "password": False}),
    ("no listen address", dom("e", graphics=vnc(None)), True,
     {"type": "vnc", "address": "127.0.0.1", "port": 5900, "password": False}),
    ("host name", dom("x", graphics=vnc("node2.home.lo")), True, {"reason": "listen"}),
    ("localhost is a name", dom("x", graphics=vnc("localhost")), True, {"reason": "listen"}),
    ("socket fixture", fx("dumpxml-vnc-socket.xml"), True, {"reason": "socket"}),
    ("spice fixture", fx("dumpxml-spice-only.xml"), True, {"reason": "spice"}),
    ("spice then vnc", dom("s", graphics=SPICE + vnc("127.0.0.1", 5907)), True,
     {"type": "vnc", "address": "127.0.0.1", "port": 5907, "password": False}),
    ("no display", fx("dumpxml-netperf.xml"), True, {"reason": "nodisplay"}),
    ("listen none", dom("z", graphics=vnc(None, None, listen_type="none")), True,
     {"reason": "nolisten"}),
    ("shut off", fx("dumpxml-vnc-password.xml"), False, {"reason": "notrunning"}),
    ("port -1", dom("m", graphics=vnc("127.0.0.1", -1)), True, {"reason": "notrunning"}),
    ("socket while off", fx("dumpxml-vnc-socket.xml"), False, {"reason": "socket"}),
]


@pytest.mark.parametrize("label,xml,live,expected", DISPLAY_CASES, ids=[c[0] for c in DISPLAY_CASES])
def test_display_target(label, xml, live, expected):
    addrs = HOST_ADDRS
    assert ko.display_target(xml, live, host_addresses=addrs) == expected
    # même résultat sur le dict déjà lu
    assert ko.display_target(kh.parse_domain_xml(xml), live, host_addresses=addrs) == expected


IP_ADDR = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
1: lo    inet6 ::1/128 scope host noprefixroute \\       valid_lft forever preferred_lft forever
2: br0    inet 172.16.1.12/16 brd 172.16.255.255 scope global br0\\       valid_lft forever
2: br0    inet6 fe80::0a:0001/64 scope link proto kernel_ll \\       valid_lft forever
5: virbr0    inet 192.168.122.1/24 brd 192.168.122.255 scope global virbr0\\       valid_lft
"""
HOST_ADDRS = ko.parse_ip_addr(IP_ADDR)


def test_parse_ip_addr_and_host_addresses():
    assert HOST_ADDRS == {"127.0.0.1", "::1", "172.16.1.12", "fe80::a:1", "192.168.122.1"}
    sim = Sim()
    sim.run = lambda argv, timeout=None: IP_ADDR if argv == [
        "env", "LC_ALL=C", "ip", "-o", "addr", "show"] else None
    assert ko.host_addresses(sim) == HOST_ADDRS


@pytest.mark.parametrize("listen", ["172.16.1.12", "192.168.122.1"])
def test_display_target_specific_ip_needs_host_addresses(listen):
    # sans la liste des adresses de l'hôte, une IP précise n'est jamais crue
    xml = dom("h", graphics=vnc(listen, 5905))
    assert ko.display_target(xml, True) == {"reason": "listen"}
    assert ko.display_target(xml, True, host_addresses={"10.0.0.1"}) == {"reason": "listen"}
    assert ko.display_target(xml, True, host_addresses=HOST_ADDRS)["address"] == listen


def test_vnc_password_reads_security_info_only():
    sim = Sim()
    sim.add_domain("kvmtest-1", fx("dumpxml-vnc-password.xml"), state="running")
    sim.add_domain("plain", dom("plain", graphics=vnc("127.0.0.1")), state="running")
    assert ko.vnc_password(sim, "kvmtest-1") == FAKE_PASSWORD
    assert sim.calls[-1] == ("virsh", ("dumpxml", "--security-info", "--domain", "kvmtest-1"))
    assert ko.vnc_password(sim, "plain") is None
    with pytest.raises(kh.KvmError) as e:
        ko.vnc_password(sim, "absent")
    assert e.value.kind == "notfound"


def test_vnc_password_error_never_quotes_the_xml():
    sim = Sim()
    sim.add_domain("broken", f"<domain><graphics type='vnc' passwd='{FAKE_PASSWORD}'>")
    with pytest.raises(ko.OpError) as e:
        ko.vnc_password(sim, "broken")
    assert FAKE_PASSWORD not in repr(e.value) and FAKE_PASSWORD not in e.value.message


# --- snapshot_support ----------------------------------------------------------------

Q = f"{IMG}/q.qcow2"
SUPPORT_CASES = [
    ("qcow2 fixture", fx("dumpxml-vnc-password.xml"), []),
    ("uefi qcow2 nvram fixture", fx("dumpxml-kvmtest-uefi.xml"), []),
    ("raw", dom("r", [disk("vda", "/i/r.img", fmt="raw")]),
     ["raw disk vda: internal snapshots need qcow2"]),
    ("no driver format", dom("r", ["<disk type='file' device='disk'><source file='/i/x'/>"
                                   "<target dev='vda'/></disk>"]),
     ["raw disk vda: internal snapshots need qcow2"]),
    ("block", dom("b", [disk("vdb", "/dev/vg/lv", type="block", fmt="raw")]),
     ["block disk vdb (LVM, partition): internal snapshots need a qcow2 file"]),
    ("network", dom("n", [disk("vdc", "pool/img", type="network", fmt="raw")]),
     ["network disk vdc: internal snapshots need a qcow2 file"]),
    ("transient", dom("t", [disk("vdd", Q, transient=True)]),
     ["transient disk vdd: internal snapshots are not possible"]),
    ("hostdev", dom("h", [disk("vda", Q)], extra_dev="<hostdev mode='subsystem' type='pci'/>"),
     ["PCI or USB device attached (hostdev): libvirt cannot snapshot it"]),
    ("raw nvram", dom("u", [disk("vda", Q)], nvram="raw"),
     ["UEFI variables (nvram) are raw: internal snapshots need qcow2"]),
    ("read-only cdrom and disks ignored", dom("c", [
        disk("vda", Q), disk("sda", "/iso/x.iso", device="cdrom", fmt="raw", ro=True),
        disk("vdb", "/i/ro.img", fmt="raw", ro=True), disk("vdc", "/i/s.img", fmt="raw",
                                                             share=True)]), []),
    ("volume qcow2", dom("v", [disk("vda", "images/v.qcow2", type="volume")]), []),
    ("two reasons", dom("w", [disk("vda", "/i/a.img", fmt="raw"),
                              disk("vdb", "/dev/sdb", type="block", fmt="raw")]),
     ["raw disk vda: internal snapshots need qcow2",
      "block disk vdb (LVM, partition): internal snapshots need a qcow2 file"]),
]


@pytest.mark.parametrize("label,xml,reasons", SUPPORT_CASES, ids=[c[0] for c in SUPPORT_CASES])
def test_snapshot_support(label, xml, reasons):
    assert ko.snapshot_support(xml) == reasons


# --- instantanés ------------------------------------------------------------------------

SNAPS = [("s1", "running", None, "internal"), ("s2", "shutoff", "s1", "internal"),
         ("-z w", "shutoff", "s2", "internal")]


def snap_sim(state="shut off", snaps=SNAPS, current="-z w", xml=None, persistent=True):
    sim = Sim()
    sim.add_domain("kvmtest-1", xml or fx("dumpxml-vnc-password.xml"), state=state,
                   persistent=persistent, snaps=snaps, current=current)
    return sim


def test_list_snapshots_one_batch():
    sim = snap_sim()
    res = ko.list_snapshots(sim, "kvmtest-1")
    assert [c[0] for c in sim.calls] == ["batch"] * 6
    assert sim.virsh_calls() == [
        ("snapshot-list", "--domain", "kvmtest-1", "--parent"),
        ("snapshot-list", "--domain", "kvmtest-1", "--internal", "--name"),
        ("snapshot-list", "--domain", "kvmtest-1", "--external", "--name"),
        ("snapshot-current", "--domain", "kvmtest-1", "--name"),
        ("dumpxml", "--domain", "kvmtest-1"),
        ("dominfo", "--domain", "kvmtest-1")]
    assert [(s["name"], s["state"], s["parent"], s["location"], s["current"])
            for s in res["snapshots"]] == [("s1", "running", None, "internal", False),
                                           ("s2", "shutoff", "s1", "internal", False),
                                           ("-z w", "shutoff", "s2", "internal", True)]
    assert res["snapshots"][0]["created"] == "2026-10-24T11:39:00+02:00"
    assert (res["current"], res["support"], res["running"], res["state"], res["persistent"],
            res["memory_kib"]) == ("-z w", [], False, "shut off", True, 262144)


def test_list_snapshots_no_current_is_not_an_error():
    res = ko.list_snapshots(snap_sim(snaps=[], current=None), "kvmtest-1")
    assert (res["snapshots"], res["current"]) == ([], None)


def test_list_snapshots_other_errors_raise():
    sim = snap_sim()
    sim.fail["snapshot-current"] = (1, "error: internal error: boom\n")
    with pytest.raises(kh.KvmError):
        ko.list_snapshots(sim, "kvmtest-1")
    with pytest.raises(kh.KvmError) as e:
        ko.list_snapshots(sim, "absent")
    assert e.value.kind == "notfound"


@pytest.mark.parametrize("state,description,memory", [
    ("shut off", None, False), ("running", "-dash text; $(id)", True)])
def test_create_snapshot_argv(state, description, memory):
    sim = snap_sim(state=state)
    res = ko.create_snapshot(sim, "kvmtest-1", "new-1", description=description)
    creates = sim.virsh_calls("snapshot-create-as")
    expected = ["snapshot-create-as", "--domain", "kvmtest-1", "--name", "new-1"]
    if description:
        expected += ["--description", description]
    assert creates == [tuple(expected + ["--atomic"])]
    assert all("--disk-only" not in c for c in sim.virsh_calls())
    assert (res["snapshot"], res["memory"], res["current"]) == ("new-1", memory, "new-1")
    assert [s["name"] for s in res["snapshots"]][-1] == "new-1"


REFUSALS = [
    ("raw disk", dict(xml=dom("kvmtest-1", [disk("vda", "/i/r.img", fmt="raw")])),
     "raw disk vda: internal snapshots need qcow2"),
    ("external snapshots", dict(snaps=SNAPS + [("ext", "shutoff", "s1", "external")]),
     "this VM uses external snapshots: not managed by the console in this version"),
    ("transient", dict(persistent=False), "transient VM"),
    ("paused", dict(state="paused"), "the VM is paused"),
    ("in shutdown", dict(state="in shutdown"), "the VM is in shutdown"),
    ("existing name", dict(snaps=SNAPS + [("new-1", "shutoff", None, "internal")]),
     "a snapshot named new-1 already exists"),
]


@pytest.mark.parametrize("label,kw,text", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_create_snapshot_refusals(label, kw, text):
    sim = snap_sim(**kw)
    with pytest.raises(ko.OpError) as e:
        ko.create_snapshot(sim, "kvmtest-1", "new-1")
    assert e.value.kind == "refused" and text in e.value.message
    assert any(text in r for r in e.value.data["refusals"])
    assert not sim.virsh_calls("snapshot-create-as")


@pytest.mark.parametrize("kw", [dict(snapshot="bad name"), dict(snapshot="-x"),
                                dict(name=""), dict(description="x" * 1001),
                                dict(description="a\x00b")])
def test_create_snapshot_invalid_input_makes_no_call(kw):
    sim = snap_sim()
    args = {"name": "kvmtest-1", "snapshot": "ok", "description": None, **kw}
    with pytest.raises(ko.OpError) as e:
        ko.create_snapshot(sim, args["name"], args["snapshot"], description=args["description"])
    assert e.value.kind == "invalid" and sim.calls == []


@pytest.mark.parametrize("snap,start,end", [("s1", "shut off", "running"),
                                            ("-z w", "running", "shut off")])
def test_revert_snapshot_rereads_state(snap, start, end):
    sim = snap_sim(state=start)
    res = ko.revert_snapshot(sim, "kvmtest-1", snap)
    assert sim.virsh_calls("snapshot-revert") == [
        ("snapshot-revert", "--domain", "kvmtest-1", "--snapshotname", snap)]
    assert sim.virsh_calls()[-1] == ("domstate", "--domain", "kvmtest-1")
    assert res == {"snapshot": snap, "taken_state": dict((s[0], s[1]) for s in SNAPS)[snap],
                   "state": end}
    assert all("--force" not in c for c in sim.virsh_calls())


def test_revert_needing_force_is_named_not_forced():
    sim = snap_sim()
    # texte réel (VIR_ERR_SNAPSHOT_REVERT_RISKY, libvirt 12.7.0, qemu_snapshot.c)
    sim.fail["snapshot-revert"] = (1, "error: revert requires force: snapshot 's1' lacks "
                                      "domain 'kvmtest-1' rollback info\n")
    with pytest.raises(ko.OpError) as e:
        ko.revert_snapshot(sim, "kvmtest-1", "s1")
    assert e.value.kind == "needs-force" and "revert requires force" in e.value.message
    assert all("--force" not in c for c in sim.virsh_calls() if c[0] == "snapshot-revert")


def test_revert_other_libvirt_error_passes_through():
    sim = snap_sim()
    sim.fail["snapshot-revert"] = (1, "error: internal error: disk busy\n")
    with pytest.raises(kh.KvmError):
        ko.revert_snapshot(sim, "kvmtest-1", "s1")


@pytest.mark.parametrize("func", [ko.revert_snapshot, ko.delete_snapshot])
def test_external_or_missing_snapshot_refused(func):
    sim = snap_sim(snaps=SNAPS + [("ext", "shutoff", "s1", "external")])
    with pytest.raises(ko.OpError) as e:
        func(sim, "kvmtest-1", "ext")
    assert e.value.kind == "refused" and "external snapshot" in e.value.message
    with pytest.raises(ko.OpError) as e:
        func(sim, "kvmtest-1", "nope")
    assert e.value.kind == "notfound"
    assert not sim.virsh_calls("snapshot-revert") and not sim.virsh_calls("snapshot-delete")


def test_delete_snapshot_reattaches_children():
    sim = snap_sim()
    res = ko.delete_snapshot(sim, "kvmtest-1", "s2")
    assert sim.virsh_calls("snapshot-delete") == [
        ("snapshot-delete", "--domain", "kvmtest-1", "--snapshotname", "s2")]
    assert all("--children" not in c for c in sim.virsh_calls())
    assert [(s["name"], s["parent"]) for s in res["snapshots"]] == [("s1", None), ("-z w", "s1")]


# --- disques possédés -------------------------------------------------------------------

POOL = f"{IMG}/kvmtest"
OUT = "/srv/vm"


def storage_sim():
    """Pool `kvmtest` (dir) + répertoire hors pool /srv/vm, et quelques voisins."""
    sim = Sim()
    sim.add_pool("kvmtest", POOL)
    for v in ("base.qcow2", "mine.qcow2", "shared.qcow2", "lonely.qcow2"):
        sim.add_vol("kvmtest", v, backing=f"{POOL}/base.qcow2" if v == "mine.qcow2" else None)
    return sim


def owned_map(sim, vm="target"):
    return {d["target"]: (d["deletable"], d["reason"]) for d in ko.owned_disks(sim, vm)}


def test_owned_disks_reasons_table():
    sim = storage_sim()
    sim.files[f"{OUT}/stopped-ov.qcow2"] = [
        {"filename": f"{OUT}/stopped-ov.qcow2"}, {"filename": f"{OUT}/base-out.qcow2"}]
    sim.files[f"{OUT}/unread.qcow2"] = [{"filename": f"{OUT}/unread.qcow2"}]
    sim.add_domain("target", dom("target", [
        disk("vda", f"{POOL}/lonely.qcow2"),                     # supprimable
        disk("vdb", f"{POOL}/shared.qcow2"),                     # cité par other
        disk("vdc", f"{POOL}/base.qcow2"),                       # base de mine.qcow2 (pool)
        disk("vdd", f"{OUT}/base-out.qcow2"),                    # base d'une surcouche arrêtée
        disk("vde", f"{OUT}/running-base.qcow2"),                # base d'un domaine en marche
        disk("vdf", "/dev/vg/lv", type="block", fmt="raw"),
        disk("vdg", "rbd/img", type="network", fmt="raw"),
        disk("sda", "/iso/x.iso", device="cdrom", fmt="raw", ro=True),
        disk("vdh", "kvmtest/lonely2.qcow2", type="volume")]))
    sim.add_vol("kvmtest", "lonely2.qcow2")
    sim.add_domain("other", dom("other", [disk("vda", f"{POOL}/shared.qcow2")]))
    sim.add_domain("stopped", dom("stopped", [disk("vda", f"{OUT}/stopped-ov.qcow2")]))
    sim.add_domain("runner", dom("runner", [disk("vda", f"{OUT}/top.qcow2")]), state="running",
                   live=dom("runner", [disk("vda", f"{OUT}/top.qcow2",
                                            chain=[f"{OUT}/running-base.qcow2"])], live_id=3))
    assert owned_map(sim) == {
        "vda": (True, None),
        "vdb": (False, "shared with other"),
        "vdc": (False, f"backing file of {POOL}/mine.qcow2"),
        "vdd": (False, f"backing file of {OUT}/stopped-ov.qcow2"),
        "vde": (False, f"backing file of {OUT}/top.qcow2"),
        "vdf": (False, "block device"),
        "vdg": (False, "network disk"),
        "sda": (False, "read-only or CD-ROM"),
        "vdh": (True, None),
    }
    d = {x["target"]: x for x in ko.owned_disks(sim, "target")}
    assert (d["vda"]["pool"], d["vda"]["volume"], d["vda"]["kind"]) == (
        "kvmtest", "lonely.qcow2", "volume")
    assert (d["vdh"]["path"], d["vdh"]["volume"]) == (f"{POOL}/lonely2.qcow2", "lonely2.qcow2")


def test_owned_disks_unreadable_file_proves_nothing():
    sim = storage_sim()
    sim.add_domain("target", dom("target", [disk("vda", f"{POOL}/lonely.qcow2"),
                                            disk("vdb", f"{OUT}/own.qcow2")]))
    sim.add_domain("stopped", dom("stopped", [disk("vda", f"{OUT}/secret.qcow2")]))
    sim.files[f"{OUT}/secret.qcow2"] = None                  # illisible par le compte SSH
    assert owned_map(sim) == {"vda": (False, ko.REASON_UNKNOWN),
                              "vdb": (False, ko.REASON_UNKNOWN)}


def test_owned_disks_raw_neighbours_are_not_opened():
    # un raw ne peut pas avoir d'image de base : pas de qemu-img, pas d'inconnu
    sim = storage_sim()
    sim.add_domain("target", dom("target", [disk("vda", f"{POOL}/lonely.qcow2")]))
    sim.add_domain("rawvm", dom("rawvm", [disk("vda", f"{OUT}/r.img", fmt="raw")]))
    assert owned_map(sim) == {"vda": (True, None)}
    scripts = " ".join(c[1][2] for c in sim.calls if c[0] == "run")
    assert "qemu-img" not in scripts and f"{OUT}/r.img" in scripts      # identité lue, rien d'autre


def test_owned_disks_unreadable_domain_proves_nothing():
    sim = storage_sim()
    sim.add_domain("target", dom("target", [disk("vda", f"{POOL}/lonely.qcow2")]))
    sim.add_domain("weird", "<domain><name>weird</name><devices>")       # XML illisible
    assert owned_map(sim) == {"vda": (False, ko.REASON_UNKNOWN)}


def test_owned_disks_own_overlay_is_not_another_user():
    # vdb (de la VM) est la surcouche de vda : supprimer les deux est sûr
    sim = storage_sim()
    sim.add_vol("kvmtest", "top.qcow2", backing=f"{POOL}/lonely.qcow2")
    sim.add_domain("target", dom("target", [disk("vda", f"{POOL}/lonely.qcow2"),
                                            disk("vdb", f"{POOL}/top.qcow2")]))
    assert owned_map(sim) == {"vda": (True, None), "vdb": (True, None)}


def test_owned_disks_deep_chain_in_pool():
    # a <- b <- c : a reste la base (indirecte) de c
    sim = storage_sim()
    sim.add_vol("kvmtest", "b.qcow2", backing=f"{POOL}/lonely.qcow2")
    sim.add_vol("kvmtest", "c.qcow2", backing=f"{POOL}/b.qcow2")
    sim.add_domain("target", dom("target", [disk("vda", f"{POOL}/lonely.qcow2")]))
    assert owned_map(sim)["vda"] == (False, f"backing file of {POOL}/b.qcow2")


def test_owned_disks_relative_backing_from_qemu_img():
    sim = storage_sim()
    sim.files[f"{OUT}/ov.qcow2"] = [{"filename": f"{OUT}/ov.qcow2"}, {"filename": "base.qcow2"}]
    sim.add_domain("target", dom("target", [disk("vda", f"{OUT}/base.qcow2")]))
    sim.add_domain("stopped", dom("stopped", [disk("vda", f"{OUT}/ov.qcow2")]))
    assert owned_map(sim)["vda"] == (False, f"backing file of {OUT}/ov.qcow2")


def test_disk_users_and_backing_users():
    sim = storage_sim()
    sim.add_domain("a", dom("a", [disk("vda", f"{POOL}/shared.qcow2")]))
    sim.add_domain("b", dom("b", [disk("vda", "kvmtest/shared.qcow2", type="volume")]))
    users = ko.disk_users(sim, exclude="b")
    assert users[f"{POOL}/shared.qcow2"] == ["a"] and users["kvmtest/shared.qcow2"] == ["a"]
    assert ko.backing_users(sim, f"{POOL}/base.qcow2") == [f"{POOL}/mine.qcow2"]
    assert ko.backing_users(sim, f"{POOL}/lonely.qcow2") == []


def test_owned_disks_unknown_vm():
    with pytest.raises(ko.OpError) as e:
        ko.owned_disks(storage_sim(), "absent")
    assert e.value.kind == "notfound"


# --- clone_xml ---------------------------------------------------------------------------

def test_clone_xml_targeted_changes_uefi_fixture():
    src = fx("dumpxml-kvmtest-uefi-inactive.xml")
    out = ko.clone_xml(src, "kvmtest-2", {"vda": f"{POOL}/kvmtest-2-vda.qcow2"})
    root = ET.fromstring(out)
    old = ET.fromstring(src)
    assert root.findtext("name") == "kvmtest-2" and root.find("uuid") is None
    assert root.findtext("title") == "kvmtest kvmtest-uefi (copy of kvmtest-uefi)"
    assert all(i.find("mac") is None for i in root.findall("devices/interface"))
    assert root.find("devices/disk/source").get("file") == f"{POOL}/kvmtest-2-vda.qcow2"
    nv = root.find("os/nvram")
    assert not (nv.text or "").strip()
    assert (nv.get("template"), nv.get("format"), nv.get("templateFormat")) == (
        "/usr/share/qemu/ovmf-x86_64-4m-vars.qcow2", "qcow2", "qcow2")
    # métadonnées libosinfo gardées avec leur préfixe
    assert "<libosinfo:libosinfo" in out and 'xmlns:libosinfo="http://libosinfo.org/' in out
    # rien d'autre ne bouge : mêmes éléments, mêmes attributs ailleurs
    for path in ("memory", "vcpu", "os/type", "features", "devices/emulator",
                 "devices/disk/driver", "devices/graphics"):
        assert ET.tostring(root.find(path)) == ET.tostring(old.find(path)), path
    assert len(root.findall("devices/*")) == len(old.findall("devices/*"))


def test_clone_xml_keeps_pinning_and_realtime():
    src = fx("dumpxml-netperf.xml")
    out = ko.clone_xml(src, "netperf-2", {})
    root, old = ET.fromstring(out), ET.fromstring(src)
    for path in ("cputune", "memoryBacking", "cpu", "iothreads", "defaultiothread"):
        assert ET.tostring(root.find(path)) == ET.tostring(old.find(path)), path
    assert kh.parse_domain_xml(out)["cpu_realtime"] and kh.parse_domain_xml(out)["cpu_pinned"]


def test_clone_xml_fixed_port_sysinfo_volume_and_backing():
    src = dom("v", [disk("vda", "images/v.qcow2", type="volume"),
                    disk("vdb", "/i/b.qcow2", chain=["/i/base.qcow2"]),
                    disk("vdc", "/i/keep.qcow2")],
              graphics="<graphics type='vnc' port='5911' autoport='no' websocket='5711' "
                       f"listen='127.0.0.1' passwd='{FAKE_PASSWORD}'/>"
                       "<graphics type='spice' port='-1' autoport='yes'/>",
              extra="<sysinfo type='smbios'><system><entry name='manufacturer'>m</entry>"
                    "<entry name='uuid'>11111111-2222-3333-4444-555555555555</entry>"
                    "</system></sysinfo>")
    out = ko.clone_xml(src, "v2", {"vda": "v2-vda.qcow2", "vdb": "/i/v2-vdb.qcow2"})
    root = ET.fromstring(out)
    g = root.findall("devices/graphics")
    assert (g[0].get("port"), g[0].get("websocket"), g[0].get("autoport")) == (None, None, "yes")
    assert g[0].get("passwd") == FAKE_PASSWORD          # le clone garde le mot de passe
    assert (g[1].get("port"), g[1].get("autoport")) == ("-1", "yes")
    entries = [(e.get("name"), e.text) for e in root.findall("sysinfo/system/entry")]
    assert entries == [("manufacturer", "m")]
    srcs = [d.find("source").attrib for d in root.findall("devices/disk")]
    assert srcs == [{"pool": "images", "volume": "v2-vda.qcow2"}, {"file": "/i/v2-vdb.qcow2"},
                    {"file": "/i/keep.qcow2"}]
    assert root.findall("devices/disk")[1].find("backingStore") is None


def test_clone_xml_refuses_doctype():
    with pytest.raises(ValueError):
        ko.clone_xml("<!DOCTYPE d [<!ENTITY x 'y'>]><domain><name>&x;</name></domain>", "n", {})


# --- clone_plan / clone_vm -----------------------------------------------------------------

def clone_sim(mode="pool", state="shut off", **kw):
    sim = storage_sim()
    sim.writable.add(OUT)
    sim.statf[OUT] = "1000000 4096"
    if mode == "pool":
        disks = [disk("vda", f"{POOL}/mine.qcow2"),
                 disk("sda", "/iso/x.iso", device="cdrom", fmt="raw", ro=True)]
    else:
        disks = [disk("vda", f"{OUT}/src.qcow2"),
                 disk("sda", "/iso/x.iso", device="cdrom", fmt="raw", ro=True)]
        sim.exists.add(f"{OUT}/src.qcow2")
        sim.files[f"{OUT}/src.qcow2"] = [
            {"filename": f"{OUT}/src.qcow2", "virtual-size": 64 << 20, "actual-size": 3 << 20},
            {"filename": f"{OUT}/base.qcow2", "virtual-size": 64 << 20, "actual-size": 2 << 20}]
    xml = kw.pop("xml", None) or dom(
        "src", disks, nics=2, nvram="qcow2",
        graphics=f"<graphics type='vnc' autoport='yes' listen='127.0.0.1' "
                 f"passwd='{FAKE_PASSWORD}'/>")
    sim.add_domain("src", xml, state=state, **kw)
    return sim


def test_clone_plan_pool_mode():
    sim = clone_sim()
    plan = ko.clone_plan(sim, "src", "src-2")
    assert plan["refusals"] == []
    (d,) = plan["disks"]
    assert (d["mode"], d["pool"], d["volume"], d["dest_name"], d["destination"], d["format"]) == (
        "pool", "kvmtest", "mine.qcow2", "src-2-vda.qcow2", f"{POOL}/src-2-vda.qcow2", "qcow2")
    # la copie aplatit la chaîne : sa propre allocation plus celle de la base
    assert (d["capacity"], d["allocation"], d["needed"]) == (64 << 20, 1 << 20, 2 << 20)
    assert plan["shared"] == [{"target": "sda", "source": "/iso/x.iso",
                               "reason": "read-only or CD-ROM"}]
    assert (plan["nics"], plan["nvram"], plan["uefi_entries_lost"]) == (2, True, True)
    assert plan["space"] == [{"where": "pool kvmtest", "available": 10 ** 12, "needed": 2 << 20}]
    # lecture seule
    assert not [c for c in sim.calls if c[0] in ("input",)]
    assert not sim.virsh_calls("vol-create-from") and not sim.virsh_calls("define")


def test_clone_plan_file_mode():
    sim = clone_sim("file")
    plan = ko.clone_plan(sim, "src", "src-2")
    assert plan["refusals"] == []
    (d,) = plan["disks"]
    assert (d["mode"], d["pool"], d["directory"], d["destination"]) == (
        "file", None, OUT, f"{OUT}/src-2-vda.qcow2")
    assert (d["capacity"], d["allocation"], d["needed"]) == (64 << 20, 3 << 20, 5 << 20)
    assert plan["space"] == [{"where": OUT, "available": 1000000 * 4096, "needed": 5 << 20}]


def _refusal_world(label):
    if label == "running":
        return clone_sim(state="running"), "the VM must be shut off to be copied (state: running)"
    if label == "transient":
        return clone_sim(persistent=False), "transient VM"
    if label == "hostdev":
        return clone_sim(xml=dom("src", [disk("vda", f"{POOL}/mine.qcow2")],
                                 extra_dev="<hostdev mode='subsystem' type='pci'/>")), "hostdev"
    if label == "static label":
        return clone_sim(xml=dom("src", [disk("vda", f"{POOL}/mine.qcow2")])
                         .replace("</domain>", "<seclabel type='static' model='selinux'>"
                                  "<label>x</label></seclabel></domain>")), "static security label"
    if label == "block":
        return clone_sim(xml=dom("src", [disk("vda", "/dev/vg/x", type="block", fmt="raw")])), \
            "block disk vda: not copied by the console"
    if label == "network":
        return clone_sim(xml=dom("src", [disk("vda", "rbd/x", type="network", fmt="raw")])), \
            "network disk vda: not copied by the console"
    if label == "shareable":
        return clone_sim(xml=dom("src", [disk("vda", f"{POOL}/mine.qcow2", share=True)])), \
            "shared disk vda"
    if label == "shared with another VM":
        sim = clone_sim()
        sim.add_domain("other", dom("other", [disk("vda", f"{POOL}/mine.qcow2")]))
        return sim, "disk vda is shared with other"
    if label == "name taken":
        sim = clone_sim()
        sim.add_domain("src-2", dom("src-2"))
        return sim, "a VM named src-2 already exists"
    if label == "volume exists":
        sim = clone_sim()
        sim.add_vol("kvmtest", "src-2-vda.qcow2")
        return sim, f"{POOL}/src-2-vda.qcow2 already exists"
    if label == "file exists":
        sim = clone_sim("file")
        sim.exists.add(f"{OUT}/src-2-vda.qcow2")
        return sim, f"{OUT}/src-2-vda.qcow2 already exists"
    if label == "dir not writable":
        sim = clone_sim("file")
        sim.writable.clear()
        return sim, f"the SSH user cannot write {OUT}; put the disk in a storage pool"
    if label == "pool full":
        sim = clone_sim()
        sim.pools["kvmtest"]["available"] = 1000
        return sim, "not enough space in pool kvmtest"
    if label == "dir full":
        sim = clone_sim("file")
        sim.statf[OUT] = "10 4096"
        return sim, f"not enough space in {OUT}"
    if label == "unreadable source":
        sim = clone_sim("file")
        sim.files[f"{OUT}/src.qcow2"] = None
        return sim, f"the SSH user cannot read {OUT}/src.qcow2"
    raise AssertionError(label)


CLONE_REFUSALS = ["running", "transient", "hostdev", "static label", "block", "network",
                  "shareable", "shared with another VM", "name taken", "volume exists",
                  "file exists", "dir not writable", "pool full", "dir full", "unreadable source"]


@pytest.mark.parametrize("label", CLONE_REFUSALS)
def test_clone_plan_refusals(label):
    sim, text = _refusal_world(label)
    plan = ko.clone_plan(sim, "src", "src-2")
    assert any(text in r for r in plan["refusals"]), plan["refusals"]
    # et l'exécution refuse avant toute écriture
    sim.calls.clear()
    with pytest.raises(ko.OpError) as e:
        ko.clone_vm(sim, "src", "src-2")
    assert e.value.kind == "refused"
    assert not [c for c in sim.calls if c[0] in ("input",) or
                (c[0] == "run" and c[1][2:3] == (ko._COPY_START,))]


def steps_recorder():
    seen = []
    return seen, (lambda sid, status, msg="": seen.append((sid, status)))


def test_clone_vm_pool_mode_flattens_and_defines_by_stdin():
    sim = clone_sim()
    seen, progress = steps_recorder()
    res = ko.clone_vm(sim, "src", "src-2", progress=progress)
    inputs = [c for c in sim.calls if c[0] == "input"]
    create, define = inputs
    assert create[1][5:] == ("vol-create-from", "--pool", "kvmtest", "--file", "/dev/stdin",
                             "--vol", "mine.qcow2", "--inputpool", "kvmtest")
    vol = ET.fromstring(create[2])
    assert (vol.findtext("name"), vol.findtext("capacity"), vol.find("target/format").get("type"),
            vol.find("backingStore")) == ("src-2-vda.qcow2", str(64 << 20), "qcow2", None)
    assert define[1][5:] == ("define", "/dev/stdin")
    # le XML (et son mot de passe) part sur l'entrée standard, jamais en argument
    assert FAKE_PASSWORD in define[2]
    assert all(FAKE_PASSWORD not in a for c in sim.calls for a in c[1])
    xml = ET.fromstring(sim.domains["src-2"]["xml"])
    assert xml.find("devices/disk/source").get("file") == f"{POOL}/src-2-vda.qcow2"
    assert seen == [("plan", "running"), ("plan", "done"), ("copy-vda", "running"),
                    ("copy-vda", "done"), ("define", "running"), ("define", "done"),
                    ("verify", "running"), ("verify", "done")]
    assert res["name"] == "src-2" and res["uuid"] != kh.parse_domain_xml(
        sim.domains["src"]["xml"])["uuid"]
    assert res["macs"] == ["52:54:00:ab:cd:00", "52:54:00:ab:cd:01"]
    assert res["disks"] == [{"target": "vda", "destination": f"{POOL}/src-2-vda.qcow2",
                             "pool": "kvmtest"}]
    assert (res["leftovers"], res["nvram"], res["uefi_entries_lost"]) == ([], True, True)
    assert all(c[0] != "vol-clone" for c in sim.virsh_calls())


def test_clone_vm_file_mode_copies_then_links():
    sim = clone_sim("file")
    msgs = []
    res = ko.clone_vm(sim, "src", "src-2", progress=lambda s, st, m="": msgs.append((s, st, m)),
                      sleep=lambda s: None)
    (tmp, copy), = sim.copies.items()
    assert re.fullmatch(rf"{OUT}/\.src-2-vda\.qcow2\.hops-[0-9a-f]{{12}}\.tmp", tmp)
    assert (copy["src"], copy["fmt"], copy["committed"]) == (f"{OUT}/src.qcow2", "qcow2",
                                                             f"{OUT}/src-2-vda.qcow2")
    assert ("copy-vda", "running", f"50% ({32 << 20} of {64 << 20} bytes)") in msgs
    assert res["disks"] == [{"target": "vda", "destination": f"{OUT}/src-2-vda.qcow2",
                             "pool": None}]


def test_clone_vm_copy_failure_rolls_back():
    sim = clone_sim("file")
    sim.copy_rc = "1"
    with pytest.raises(ko.OpError) as e:
        ko.clone_vm(sim, "src", "src-2", sleep=lambda s: None)
    assert e.value.kind == "failed" and "boom: no space" in e.value.message
    (copy,) = sim.copies.values()
    assert copy["killed"] and e.value.data["leftovers"] == [] and e.value.leftovers == []
    assert "src-2" not in sim.domains


def test_clone_vm_define_failure_removes_copied_volume():
    sim = clone_sim()
    sim.fail["define"] = (1, "error: XML error: boom\n")
    with pytest.raises(kh.KvmError) as e:
        ko.clone_vm(sim, "src", "src-2")
    assert sim.virsh_calls("vol-delete") == [("vol-delete", "--pool", "kvmtest", "--vol",
                                              "src-2-vda.qcow2")]
    assert "src-2-vda.qcow2" not in sim.pools["kvmtest"]["vols"]
    assert e.value.leftovers == []
    # le domaine n'existe pas : rien à défaire de ce côté (notfound ignoré)
    assert not sim.virsh_calls("undefine")


class Stop(BaseException):
    """Annulation simulée (comme Cancelled de la CLI : BaseException)."""


def test_clone_vm_cancel_during_copy_kills_and_cleans():
    sim = clone_sim("file")
    sim.copy_polls = 10

    def cancel(copy):
        if copy["polls"] == 2:
            raise Stop()
    sim.on_copy_poll = cancel
    with pytest.raises(Stop) as e:
        ko.clone_vm(sim, "src", "src-2", sleep=lambda s: None)
    (copy,) = sim.copies.values()
    assert copy["killed"] and "committed" not in copy
    assert e.value.leftovers == [] and "src-2" not in sim.domains


def test_clone_vm_cancel_after_define_undefines_only_our_clone():
    sim = clone_sim()
    real = sim.v_define

    def define_then_cancel(o, p, data):
        real(o, p, data)                    # défini, puis annulation avant la réponse
        raise Stop()
    sim.v_define = define_then_cancel
    with pytest.raises(Stop) as e:
        ko.clone_vm(sim, "src", "src-2")
    assert sim.virsh_calls("undefine") == [("undefine", "--domain", "src-2", "--nvram")]
    assert "src-2" not in sim.domains and "src-2-vda.qcow2" not in sim.pools["kvmtest"]["vols"]
    assert e.value.leftovers == []


def test_clone_vm_never_undefines_a_domain_that_is_not_ours():
    # un domaine du même nom apparu pendant la copie : define échoue, et le
    # retour arrière ne le supprime pas
    sim = clone_sim()
    real = sim.v_vol_create_from

    def create_and_race(o, p, data):
        out = real(o, p, data)
        sim.add_domain("src-2", dom("src-2", [disk("vda", "/elsewhere/x.qcow2")]))
        return out
    sim.v_vol_create_from = create_and_race
    with pytest.raises(kh.KvmError):
        ko.clone_vm(sim, "src", "src-2")
    assert "src-2" in sim.domains and not sim.virsh_calls("undefine")


def test_clone_vm_existing_volume_race_is_never_deleted():
    sim = clone_sim()
    real = sim.v_vol_create_from

    def race(o, p, data):
        sim.add_vol("kvmtest", "src-2-vda.qcow2")       # apparu entre le plan et la copie
        return real(o, p, data)
    sim.v_vol_create_from = race
    with pytest.raises(kh.KvmError):
        ko.clone_vm(sim, "src", "src-2")
    assert "src-2-vda.qcow2" in sim.pools["kvmtest"]["vols"] and not sim.virsh_calls("vol-delete")


def test_clone_vm_rollback_failure_is_reported():
    sim = clone_sim()
    sim.fail["define"] = (1, "error: boom\n")
    real = sim._answer

    def answer(args, data=None):
        if str(args[0]) == "vol-delete":
            return 1, "", "error: cannot delete volume: busy\n"
        return real(args, data)
    sim._answer = answer
    with pytest.raises(kh.KvmError) as e:
        ko.clone_vm(sim, "src", "src-2")
    assert e.value.leftovers == [{"kind": "volume", "pool": "kvmtest", "volume": "src-2-vda.qcow2",
                                  "error": "node2: failed: error: cannot delete volume: busy"}]


# --- suppression -----------------------------------------------------------------------------

def delete_sim(nvram=None, state="shut off", snaps=(), persistent=True):
    sim = storage_sim()
    sim.add_domain("victim", dom("victim", [
        disk("vda", f"{POOL}/lonely.qcow2"),
        disk("vdb", f"{POOL}/shared.qcow2"),
        disk("vdc", f"{OUT}/own.qcow2"),
        disk("sda", "/iso/x.iso", device="cdrom", fmt="raw", ro=True)], nvram=nvram),
        state=state, snaps=snaps, persistent=persistent)
    sim.add_domain("other", dom("other", [disk("vda", f"{POOL}/shared.qcow2")]))
    sim.exists.add(f"{OUT}/own.qcow2")
    return sim


@pytest.mark.parametrize("nvram,flag", [(None, False), ("qcow2", True)])
def test_delete_vm_storage_only_owned_volumes(nvram, flag):
    sim = delete_sim(nvram=nvram)
    res = ko.delete_vm(sim, "victim", remove_storage=True)
    (undef,) = sim.virsh_calls("undefine")
    expected = ["undefine", "--domain", "victim", "--managed-save", "--snapshots-metadata",
                "--checkpoints-metadata"] + ["--nvram"] * flag + ["--storage", "vda"]
    assert undef == tuple(expected)
    assert "--remove-all-storage" not in undef
    assert [d["target"] for d in res["deleted"]] == ["vdc", "vda"]
    assert [(d["target"], d["reason"]) for d in res["kept"]] == [
        ("vdb", "shared with other"), ("sda", "read-only or CD-ROM")]
    assert res["leftovers"] == [] and res["undefined"]
    assert f"{OUT}/own.qcow2" not in sim.exists and f"{POOL}/shared.qcow2" in sim.exists
    assert "lonely.qcow2" not in sim.pools["kvmtest"]["vols"]
    assert "victim" not in sim.domains


def test_delete_vm_reports_progress_and_plan_fields():
    sim = delete_sim(nvram="qcow2", snaps=[("s1", "shutoff", None, "internal")])
    seen = []
    res = ko.delete_vm(sim, "victim", remove_storage=True,
                       progress=lambda s, st, m="": seen.append((s, st)))
    assert seen == [("plan", "running"), ("plan", "done"), ("undefine", "running"),
                    ("undefine", "done"), ("storage", "running"), ("storage", "done"),
                    ("verify", "done")]
    assert res["remove_storage"] is True and res["internal_snapshots"] == 1
    assert res["nvram"] and res["tpm_state"] is None and res["managed_save_lost"] is False
    assert res["acknowledged_unreadable"] == []


def test_delete_vm_refusal_reports_the_plan_step():
    sim = delete_sim(state="running")
    seen = []
    with pytest.raises(ko.OpError):
        ko.delete_vm(sim, "victim", progress=lambda s, st, m="": seen.append((s, st)))
    assert seen == [("plan", "running"), ("plan", "failed")]


def test_delete_vm_without_storage_keeps_every_disk():
    sim = delete_sim(snaps=[("s1", "shutoff", None, "internal")])
    plan = ko.delete_plan(sim, "victim", remove_storage=False)
    assert plan["delete"] == [] and all(d["reason"] == ko.REASON_NOT_REQUESTED
                                        for d in plan["keep"])
    assert plan["internal_snapshots"] == 1
    assert plan["notes"] == ["internal snapshot data stays in the kept disk files "
                             "(qemu-img snapshot -l)"]
    res = ko.delete_vm(sim, "victim")
    (undef,) = sim.virsh_calls("undefine")
    assert "--storage" not in undef and f"{OUT}/own.qcow2" in sim.exists
    assert res["deleted"] == [] and len(res["kept"]) == 4


def test_delete_vm_file_removal_failure_is_a_leftover():
    sim = delete_sim()
    sim.rm_fail.add(f"{OUT}/own.qcow2")
    res = ko.delete_vm(sim, "victim", remove_storage=True)
    assert [d["target"] for d in res["deleted"]] == ["vda"]
    assert [(d["target"], d["error"]) for d in res["leftovers"]] == [
        ("vdc", f"rm: cannot remove '{OUT}/own.qcow2': Permission denied")]


DELETE_REFUSALS = [
    ("running", dict(state="running"), "the VM is running: shut it down or force it off first"),
    ("paused", dict(state="paused"), "the VM is paused: shut it down"),
    ("transient", dict(persistent=False), "transient: destroy removes it"),
    ("external", dict(snaps=[("e", "shutoff", None, "external")]), "external snapshots"),
]


@pytest.mark.parametrize("label,kw,text", DELETE_REFUSALS, ids=[r[0] for r in DELETE_REFUSALS])
def test_delete_refusals(label, kw, text):
    sim = delete_sim(**kw)
    assert any(text in r for r in ko.delete_plan(sim, "victim")["refusals"])
    with pytest.raises(ko.OpError) as e:
        ko.delete_vm(sim, "victim", remove_storage=True)
    assert e.value.kind == "refused" and text in e.value.message
    assert not sim.virsh_calls("undefine") and "victim" in sim.domains


def test_delete_plan_is_recomputed_at_execution():
    # entre l'aperçu et l'exécution, un autre domaine s'est mis à citer vda
    sim = delete_sim()
    preview = ko.delete_plan(sim, "victim", remove_storage=True)
    assert [d["target"] for d in preview["delete"]] == ["vda", "vdc"]
    sim.add_domain("late", dom("late", [disk("vda", f"{POOL}/lonely.qcow2")]))
    res = ko.delete_vm(sim, "victim", remove_storage=True)
    assert "--storage" not in sim.virsh_calls("undefine")[0]
    assert ("vda", "shared with late") in [(d["target"], d["reason"]) for d in res["kept"]]


def test_delete_vm_undefine_error_with_domain_gone_reports_volumes():
    sim = delete_sim()
    real = sim.v_undefine

    def partial(o, p, data):
        real({**o, "storage": ""}, p, data)              # domaine parti, volume resté
        return 1, "", "error: Failed to remove storage volume 'vda'\n"
    sim.v_undefine = partial
    res = ko.delete_vm(sim, "victim", remove_storage=True)
    assert [d["target"] for d in res["deleted"]] == ["vdc"]
    assert [d["target"] for d in res["leftovers"]] == ["vda"]
    assert "Failed to remove storage volume" in res["leftovers"][0]["error"]


def test_delete_vm_still_defined_raises():
    sim = delete_sim()
    sim.v_undefine = lambda o, p, data: (1, "", "error: internal error: busy\n")
    with pytest.raises(kh.KvmError, match="busy"):
        ko.delete_vm(sim, "victim", remove_storage=True)


# --- aucun mot de passe nulle part ------------------------------------------------------------

def test_password_never_leaves_in_results_errors_or_json():
    texts = []
    sim = clone_sim()
    sim.domains["src"]["live"] = sim.domains["src"]["xml"]
    texts.append(json.dumps(ko.display_target(sim.virsh("dumpxml", "--security-info",
                                                        "--domain", "src"), True)))
    texts.append(json.dumps(ko.list_snapshots(sim, "src")))
    texts.append(json.dumps(ko.clone_plan(sim, "src", "src-2")))
    texts.append(json.dumps(ko.clone_vm(sim, "src", "src-2")))
    texts.append(json.dumps(ko.delete_plan(sim, "src-2", remove_storage=True)))
    texts.append(json.dumps(ko.delete_vm(sim, "src-2", remove_storage=True)))
    sim2 = clone_sim()
    sim2.fail["define"] = (1, "error: XML error\n")
    try:
        ko.clone_vm(sim2, "src", "src-3")
    except kh.KvmError as e:
        texts += [repr(e), str(e), json.dumps(getattr(e, "leftovers", None))]
    try:
        ko.create_snapshot(snap_sim(xml=dom("kvmtest-1", [disk("vda", "/i/r", fmt="raw")],
                                            graphics=vnc("127.0.0.1", passwd=True))),
                           "kvmtest-1", "s9")
    except ko.OpError as e:
        texts += [repr(e), str(e), json.dumps(e.data)]
    assert len(texts) == 12
    for t in texts:
        assert FAKE_PASSWORD not in t


# --- scripts réels : lot shell et copie, joués dans un sh local ----------------------------------

FAKE_QEMU_IMG = r"""#!/bin/sh
# faux qemu-img : « convert -p -O fmt -- src dst » écrit une progression,
# attend $QDELAY secondes, puis copie ; « info » rend du json
case "$1" in
  convert)
    shift; while [ "$1" != "--" ]; do shift; done; shift
    src=$1; dst=$2
    [ -r "$src" ] || { echo "qemu-img: Could not open '$src': Permission denied" >&2; exit 1; }
    : > "$dst"
    printf '    (0.00/100%%)\r'; sleep "${QDELAY:-0}"; printf '    (50.00/100%%)\r'
    sleep "${QDELAY:-0}"; cp "$src" "$dst"; printf '    (100.00/100%%)\r'; exit 0 ;;
  info) shift; for a; do f=$a; done
    [ -r "$f" ] || { echo "qemu-img: Could not open '$f'" >&2; exit 1; }
    printf '[{"filename": "%s", "virtual-size": 1024, "actual-size": 512}]' "$f" ;;
esac
"""


class LocalShell:
    """Remplace ssh : joue la commande distante dans un sh local."""

    def __init__(self, path, env=None):
        self.path, self.env = path, env or {}

    def __call__(self, argv, stdin=None, input=None, capture_output=False, text=False,
                 timeout=None):
        assert argv[0] == "ssh" and argv[-3] == "--"
        kw = {"input": input} if input is not None else {"stdin": stdin}
        return subprocess.run(["/bin/sh", "-c", argv[-1]], **kw, capture_output=True, text=True,
                              timeout=timeout, env={"PATH": self.path, **self.env})


@pytest.fixture
def local(tmp_path):
    st = Path(tempfile.mkdtemp(prefix="kvo-", dir="/tmp"))
    bindir = st / "bin"
    bindir.mkdir()
    (bindir / "qemu-img").write_text(FAKE_QEMU_IMG)
    (bindir / "qemu-img").chmod(0o755)
    work = tmp_path / "work dir"
    work.mkdir()
    decl = {"name": "node2", "address": "172.16.1.12", "port": 22, "user": "ju",
            "ssh_key": "ssh/node2_id", "known_hosts": "ssh/node2_known",
            "uri": "qemu:///system"}

    def make(**env):
        return kh.VirshHost(decl, st, runner=LocalShell(f"{bindir}:/usr/bin:/bin", env))
    yield SimpleNamespace(host=make, work=work)
    shutil.rmtree(st, ignore_errors=True)


HOSTILE_FILES = ["plain.img", "a'b; touch kvo-pwned", "$(id).img", "x\ny.img", "-x.img"]


def test_real_sh_batch_hostile_names(local):
    for n in HOSTILE_FILES:
        (local.work / n).write_text("x")
    cmds = [["test", "-e", str(local.work / n)] for n in HOSTILE_FILES]
    cmds += [["test", "-e", str(local.work / "absent")], ["printf", "%s", "no-newline"],
             ["stat", "-f", "-c", "%a %S", "--", str(local.work)]]
    res = ko._sh_batch(local.host(), cmds)
    assert [rc for rc, _ in res[:len(HOSTILE_FILES)]] == [0] * len(HOSTILE_FILES)
    assert res[len(HOSTILE_FILES)][0] == 1
    assert res[-2] == (0, "no-newline")
    assert res[-1][0] == 0 and re.fullmatch(r"\d+ \d+", res[-1][1].strip())
    assert not (Path.cwd() / "kvo-pwned").exists() and not (local.work / "kvo-pwned").exists()


def entry(work, src, dest_name):
    return {"target": "vda", "source": str(src), "format": "qcow2", "directory": str(work),
            "dest_name": dest_name, "destination": str(work / dest_name),
            "capacity": 1000}


def leftovers_in(work):
    return sorted(p.name for p in work.iterdir() if ".hops-" in p.name)


def test_real_copy_success_links_destination(local):
    src = local.work / "src $(id).qcow2"
    src.write_text("disk-data")
    e = entry(local.work, src, "new-vda.qcow2")
    created, msgs = [], []
    ko._copy_file(local.host(QDELAY="0.3"), e, created, time.monotonic() + 30,
                  lambda s, st, m="": msgs.append(m), time.sleep, 0.1)
    assert (local.work / "new-vda.qcow2").read_text() == "disk-data"
    assert created == [{"kind": "file", "path": str(local.work / "new-vda.qcow2")}]
    assert leftovers_in(local.work) == []
    assert any(m.startswith("50% (500 of 1000 bytes)") for m in msgs)


def test_real_copy_destination_appeared_is_never_overwritten(local):
    src = local.work / "src.qcow2"
    src.write_text("disk-data")
    dest = local.work / "new-vda.qcow2"
    e = entry(local.work, src, dest.name)
    created = []

    def appear(sid, status, msg=""):
        dest.write_text("someone else")
    with pytest.raises(ko.OpError, match="could not be created"):
        ko._copy_file(local.host(QDELAY="0.3"), e, created, time.monotonic() + 30, appear,
                      time.sleep, 0.1)
    assert dest.read_text() == "someone else"
    assert ko._rollback(local.host(), created) == []
    assert leftovers_in(local.work) == [] and dest.read_text() == "someone else"


def test_real_copy_cancel_kills_qemu_img_and_cleans(local):
    src = local.work / "src.qcow2"
    src.write_text("disk-data")
    e = entry(local.work, src, "new-vda.qcow2")
    created = []

    def cancel(sid, status, msg=""):
        raise Stop()
    t0 = time.monotonic()
    with pytest.raises(Stop):
        ko._copy_file(local.host(QDELAY="20"), e, created, time.monotonic() + 60, cancel,
                      time.sleep, 0.1)
    assert ko._rollback(local.host(), created) == []
    assert time.monotonic() - t0 < 15
    assert leftovers_in(local.work) == [] and not (local.work / "new-vda.qcow2").exists()
    out = subprocess.run(["pgrep", "-f", str(local.work)], capture_output=True, text=True)
    assert out.stdout.strip() == ""


def test_real_copy_qemu_img_failure_message(local):
    e = entry(local.work, local.work / "missing.qcow2", "new-vda.qcow2")
    created = []
    with pytest.raises(ko.OpError) as err:
        ko._copy_file(local.host(), e, created, time.monotonic() + 30, None, time.sleep, 0.1)
    assert "Permission denied" in err.value.message
    assert ko._rollback(local.host(), created) == []
    assert leftovers_in(local.work) == []


def test_real_copy_deadline(local):
    src = local.work / "src.qcow2"
    src.write_text("d")
    created = []
    with pytest.raises(ko.OpError) as err:
        ko._copy_file(local.host(QDELAY="20"), entry(local.work, src, "n.qcow2"), created,
                      time.monotonic() + 0.5, None, time.sleep, 0.1)
    assert err.value.kind == "timeout"
    assert ko._rollback(local.host(), created) == [] and leftovers_in(local.work) == []


# --- après revue : preuve de propriété sur tout l'hôte (C1) -------------------------------------

P2 = f"{IMG}/kvmtest2"
LONELY = f"{POOL}/lonely.qcow2"


def c1_sim():
    sim = storage_sim()
    sim.add_pool("kvmtest2", P2)
    sim.add_domain("target", dom("target", [disk("vda", LONELY)]))
    return sim


def _c1_other_pool(sim):
    # surcouche arrêtée dans un AUTRE pool, illisible par le compte SSH (root 0600) :
    # seule la lecture des volumes de ce pool par libvirt la voit
    sim.add_vol("kvmtest2", "ov.qcow2", backing=LONELY)
    sim.files[f"{P2}/ov.qcow2"] = None
    sim.add_domain("b", dom("b", [disk("vda", f"{P2}/ov.qcow2")]))


def _c1_other_pool_volume_disk(sim):
    sim.add_vol("kvmtest2", "ov.qcow2", backing=LONELY)
    sim.add_domain("b", dom("b", [disk("vda", "kvmtest2/ov.qcow2", type="volume")]))


def _c1_outside(sim):
    sim.files[f"{OUT}/ov.qcow2"] = [{"filename": f"{OUT}/ov.qcow2"}, {"filename": LONELY}]
    sim.add_domain("b", dom("b", [disk("vda", f"{OUT}/ov.qcow2")]))


def _c1_free_volume(sim):
    sim.add_vol("kvmtest2", "free.qcow2", backing=LONELY)        # attaché à aucun domaine


def _c1_running_live_chain(sim):
    sim.add_vol("kvmtest2", "run.qcow2")
    sim.add_domain("b", dom("b", [disk("vda", f"{P2}/run.qcow2")]), state="running",
                   live=dom("b", [disk("vda", f"{P2}/run.qcow2", chain=[LONELY])], live_id=7))


def _c1_exit_from_pools(sim):
    # volume libre -> fichier hors pool -> candidat : qemu-img lit la fin de la chaîne
    sim.add_vol("kvmtest2", "x.qcow2", backing=f"{OUT}/mid.qcow2")
    sim.files[f"{OUT}/mid.qcow2"] = [{"filename": f"{OUT}/mid.qcow2"}, {"filename": LONELY}]


def _c1_running_next_definition(sim):
    # domaine en marche dont la définition (prochain démarrage) cite une autre surcouche
    sim.files[f"{OUT}/next.qcow2"] = [{"filename": f"{OUT}/next.qcow2"}, {"filename": LONELY}]
    sim.add_domain("b", dom("b", [disk("vda", f"{OUT}/next.qcow2")]), state="running",
                   live=dom("b", [disk("vda", f"{OUT}/now.qcow2")], live_id=8))


C1_CASES = [
    ("stopped overlay in another pool", _c1_other_pool, (False, f"backing file of {P2}/ov.qcow2")),
    ("stopped overlay as a volume of another pool", _c1_other_pool_volume_disk,
     (False, f"backing file of {P2}/ov.qcow2")),
    ("stopped overlay outside any pool", _c1_outside, (False, f"backing file of {OUT}/ov.qcow2")),
    ("free volume of another pool", _c1_free_volume, (False, f"backing file of {P2}/free.qcow2")),
    ("running domain, live chain", _c1_running_live_chain,
     (False, f"backing file of {P2}/run.qcow2")),
    ("chain leaving the pools", _c1_exit_from_pools, (False, f"backing file of {P2}/x.qcow2")),
    ("next definition of a running domain", _c1_running_next_definition,
     (False, f"backing file of {OUT}/next.qcow2")),
    ("nobody", lambda sim: None, (True, None)),
]


@pytest.mark.parametrize("label,setup,expected", C1_CASES, ids=[c[0] for c in C1_CASES])
def test_ownership_proof_reads_every_chain_of_the_host(label, setup, expected):
    sim = c1_sim()
    setup(sim)
    assert owned_map(sim)["vda"] == expected
    # et la suppression garde le disque
    res = ko.delete_vm(sim, "target", remove_storage=True)
    assert [d["target"] for d in res["deleted"]] == (["vda"] if expected[0] else [])
    assert (LONELY in sim.exists) is (not expected[0])


def test_ownership_proof_batches_its_reads():
    sim = c1_sim()
    for setup in (_c1_other_pool, _c1_outside, _c1_free_volume, _c1_exit_from_pools):
        setup(sim)
    sim.add_domain("c", dom("c", [disk("vda", f"{OUT}/c1.qcow2"), disk("vdb", f"{OUT}/c2.qcow2")]))
    sim.files[f"{OUT}/c1.qcow2"] = [{"filename": f"{OUT}/c1.qcow2"}, {"filename": "deep.qcow2"}]
    sim.calls.clear()
    ko.owned_disks(sim, "target")
    runs = [c for c in sim.calls if c[0] == "run"]
    # un lot qemu-img + stat + stat -f, puis un lot stat des maillons découverts
    assert len(runs) == 2 and f"stat -L -c %d:%i -- {OUT}/deep.qcow2" in runs[1][1][2]
    assert {c[0] for c in sim.calls} == {"batch", "run"}


# --- stockage partagé entre hôtes (C2) --------------------------------------------------------

@pytest.mark.parametrize("fstype,shared", [
    ("nfs", True), ("nfs4", True), ("cifs", True), ("smb2", True), ("fuseblk", True),
    ("fuse.sshfs", True), ("glusterfs", True), ("ceph", True), ("9p", True), ("v9fs", True),
    ("virtiofs", True), ("gfs2", True), ("ocfs2", True),
    ("xfs", False), ("ext2/ext3", False), ("btrfs", False), ("tmpfs", False),
])
def test_shared_filesystem_keeps_the_disk(fstype, shared):
    sim = storage_sim()
    sim.add_domain("target", dom("target", [disk("vda", f"{OUT}/own.qcow2"),
                                            disk("vdb", LONELY)]))
    sim.fstypes[OUT] = fstype
    got = owned_map(sim)
    assert got["vda"] == ((False, ko.REASON_SHARED_STORAGE) if shared else (True, None))
    assert got["vdb"] == (True, None)


@pytest.mark.parametrize("ptype,shared", [("netfs", True), ("gluster", True), ("rbd", True),
                                          ("iscsi", True), ("dir", False), ("fs", False)])
def test_shared_pool_keeps_the_volume(ptype, shared):
    sim = storage_sim()
    sim.add_pool("remote", "/mnt/remote", type=ptype)
    sim.add_vol("remote", "r.qcow2")
    sim.add_domain("target", dom("target", [disk("vda", "remote/r.qcow2", type="volume")]))
    assert owned_map(sim)["vda"] == ((False, ko.REASON_SHARED_STORAGE) if shared else (True, None))


def test_shared_storage_reads_are_batched_and_unreadable_fs_proves_nothing():
    sim = storage_sim()
    sim.add_domain("target", dom("target", [disk("vda", f"{OUT}/a.qcow2"),
                                            disk("vdb", f"{OUT}/b.qcow2")]))
    real = sim._shell

    def shell(argv):
        if argv[:2] == ["stat", "-f"] and argv[-1].endswith("b.qcow2"):
            return 1, "stat: cannot read file system information: Input/output error"
        return real(argv)
    sim._shell = shell
    sim.calls.clear()
    assert owned_map(sim) == {"vda": (True, None), "vdb": (False, ko.REASON_UNKNOWN)}
    script = next(c[1][2] for c in sim.calls if c[0] == "run")
    assert script.count("stat -f -c %T") == 2


# --- identité des fichiers : périphérique et inode (I1) -----------------------------------------

def test_symlink_to_the_disk_used_by_another_domain_is_shared():
    sim = storage_sim()
    sim.add_domain("target", dom("target", [disk("vda", LONELY)]))
    sim.add_domain("other", dom("other", [disk("vda", f"{OUT}/alias.qcow2")]))
    sim.inodes[LONELY] = sim.inodes[f"{OUT}/alias.qcow2"] = "64769:4242"
    assert owned_map(sim)["vda"] == (False, "shared with other")


def test_symlink_in_a_backing_chain_is_seen():
    sim = storage_sim()
    sim.add_domain("target", dom("target", [disk("vda", LONELY)]))
    sim.files[f"{OUT}/ov.qcow2"] = [{"filename": f"{OUT}/ov.qcow2"},
                                    {"filename": f"{OUT}/base-link.qcow2"}]
    sim.add_domain("b", dom("b", [disk("vda", f"{OUT}/ov.qcow2")]))
    sim.inodes[LONELY] = sim.inodes[f"{OUT}/base-link.qcow2"] = "64769:77"
    assert owned_map(sim)["vda"] == (False, f"backing file of {OUT}/ov.qcow2")


def test_identity_of_another_file_unreadable_or_missing():
    sim = storage_sim()
    sim.add_domain("target", dom("target", [disk("vda", LONELY)]))
    sim.add_domain("b", dom("b", [disk("vda", f"{OUT}/gone.qcow2")]))
    sim.missing.add(f"{OUT}/gone.qcow2")
    assert owned_map(sim)["vda"] == (True, None)         # absent : il ne cite rien
    sim.add_domain("c", dom("c", [disk("vda", f"{OUT}/hidden.qcow2", fmt="raw")]))
    sim.stat_fail.add(f"{OUT}/hidden.qcow2")
    assert owned_map(sim)["vda"] == (False, ko.REASON_UNKNOWN)
    assert ko.delete_plan(sim, "target", remove_storage=True)["unreadable"] == [
        f"{OUT}/hidden.qcow2"]


def test_missing_own_disk_is_kept_not_deleted():
    sim = storage_sim()
    sim.add_domain("target", dom("target", [disk("vda", f"{OUT}/lost.qcow2")]))
    sim.missing.add(f"{OUT}/lost.qcow2")
    assert owned_map(sim)["vda"] == (False, ko.REASON_MISSING)


# --- fichiers illisibles acceptés par l'opérateur (décision 46) ---------------------------------

SECRET = f"{OUT}/secret.qcow2"


def d46_sim():
    sim = storage_sim()
    sim.add_domain("target", dom("target", [disk("vda", LONELY), disk("vdb", f"{OUT}/own.qcow2")]))
    sim.exists.add(f"{OUT}/own.qcow2")
    sim.add_domain("stopped", dom("stopped", [disk("vda", SECRET)]))
    sim.files[SECRET] = None
    return sim


def test_unreadable_files_are_listed_and_keep_everything_by_default():
    sim = d46_sim()
    plan = ko.delete_plan(sim, "target", remove_storage=True)
    assert plan["unreadable"] == [SECRET] and plan["acknowledged_unreadable"] == []
    assert plan["delete"] == [] and {d["reason"] for d in plan["keep"]} == {ko.REASON_UNKNOWN}
    res = ko.delete_vm(sim, "target", remove_storage=True)
    assert res["deleted"] == [] and LONELY in sim.exists and res["unreadable"] == [SECRET]


def test_acknowledged_unreadable_files_let_the_proof_proceed():
    sim = d46_sim()
    plan = ko.delete_plan(sim, "target", remove_storage=True, acknowledged_unreadable=[SECRET])
    assert [d["target"] for d in plan["delete"]] == ["vda", "vdb"]
    assert plan["acknowledged_unreadable"] == [SECRET]
    res = ko.delete_vm(sim, "target", remove_storage=True, acknowledged_unreadable=[SECRET])
    assert sorted(d["target"] for d in res["deleted"]) == ["vda", "vdb"]
    assert LONELY not in sim.exists


@pytest.mark.parametrize("label,extra,ack", [
    ("only one of two acknowledged", "second", [SECRET]),
    ("libvirt read missing too", "domain", [SECRET]),
    ("other path acknowledged", None, [f"{OUT}/elsewhere.qcow2"]),
])
def test_acknowledgement_is_not_a_blank_cheque(label, extra, ack):
    sim = d46_sim()
    if extra == "second":
        sim.add_domain("s2", dom("s2", [disk("vda", f"{OUT}/secret2.qcow2")]))
        sim.files[f"{OUT}/secret2.qcow2"] = None
    elif extra == "domain":
        sim.add_domain("weird", "<domain><name>weird</name><devices>")
    plan = ko.delete_plan(sim, "target", remove_storage=True, acknowledged_unreadable=ack)
    assert plan["delete"] == []


def test_acknowledgement_does_not_hide_a_readable_chain():
    sim = d46_sim()
    _c1_outside(sim)
    plan = ko.delete_plan(sim, "target", remove_storage=True, acknowledged_unreadable=[SECRET])
    assert ("vda", f"backing file of {OUT}/ov.qcow2") in [(d["target"], d["reason"])
                                                         for d in plan["keep"]]


@pytest.mark.parametrize("ack", ["/a/b", ["relative/x"], [3], ["/a\nb"], {"/a": 1}])
def test_acknowledged_unreadable_validated(ack):
    with pytest.raises(ko.OpError) as e:
        ko.delete_plan(d46_sim(), "target", remove_storage=True, acknowledged_unreadable=ack)
    assert e.value.kind == "invalid"


# --- classification des échecs de lecture : seul un refus de droits est acceptable --------------
#
# `stat -L` et `qemu-img info --backing-chain` échouent pour bien d'autres raisons qu'un refus
# de droits (E/S, descripteur NFS périmé, sortie illisible, délai dépassé). Seul le refus de
# droits (`Permission denied`) prouve que le fichier existe mais reste hors de portée : lui seul
# peut devenir acceptable (décision 46). Tout le reste ne prouve rien du tout sur ce que le
# fichier cite et doit bloquer la suppression même si l'opérateur le nomme explicitement.

def _fault_sim(path, fmt):
    """`stopped` cite `path` ; `target` a deux disques propres dont la suppression ne dépend
    que de la preuve faite sur `path`. `fmt="raw"` ne passe jamais par `qemu-img` (isole
    `_read_stats`) ; `fmt="qcow2"` fait lire la chaîne de sauvegarde par `qemu-img`."""
    sim = storage_sim()
    sim.add_domain("target", dom("target", [disk("vda", LONELY), disk("vdb", f"{OUT}/own.qcow2")]))
    sim.exists.add(f"{OUT}/own.qcow2")
    sim.add_domain("stopped", dom("stopped", [disk("vda", path, fmt=fmt)]))
    return sim


def _assert_classification(sim, path, acknowledgeable):
    """Sans acquittement, le chemin bloque toujours la suppression des disques propres de
    `target`. Avec `acknowledged_unreadable=[path]` : débloqué seulement pour un refus de
    droits ; pour toute autre cause, toujours bloqué (jamais acceptable)."""
    plan = ko.delete_plan(sim, "target", remove_storage=True)
    assert plan["delete"] == [] and {d["reason"] for d in plan["keep"]} == {ko.REASON_UNKNOWN}
    assert plan["unreadable"] == ([path] if acknowledgeable else [])
    ack_plan = ko.delete_plan(sim, "target", remove_storage=True, acknowledged_unreadable=[path])
    if acknowledgeable:
        assert sorted(d["target"] for d in ack_plan["delete"]) == ["vda", "vdb"]
    else:
        assert ack_plan["delete"] == []
        assert {d["reason"] for d in ack_plan["keep"]} == {ko.REASON_UNKNOWN}


IDENTITY_FAULTS = [
    ("permission denied", "Permission denied", True),
    ("input/output error", "Input/output error", False),
    ("stale file handle", "Stale file handle", False),
]


@pytest.mark.parametrize("label,text,acknowledgeable", IDENTITY_FAULTS,
                         ids=[c[0] for c in IDENTITY_FAULTS])
def test_identity_read_failure_classification(label, text, acknowledgeable):
    # fmt="raw" : seule l'identité (`stat -L`) est en cause, jamais `qemu-img`
    path = f"{OUT}/identity-fault.qcow2"
    sim = _fault_sim(path, fmt="raw")
    sim.stat_errors[path] = f"stat: cannot statx '{path}': {text}"
    _assert_classification(sim, path, acknowledgeable)


def test_identity_read_timeout_is_never_acknowledgeable():
    path = f"{OUT}/identity-timeout.qcow2"
    sim = _fault_sim(path, fmt="raw")
    sim.no_output.add(path)
    _assert_classification(sim, path, False)


CHAIN_FAULTS = [
    ("permission denied", "qemu_errors",
     "qemu-img: Could not open '{p}': Permission denied", True),
    ("input/output error", "qemu_errors",
     "qemu-img: Could not open '{p}': Input/output error", False),
    ("stale file handle", "qemu_errors",
     "qemu-img: Could not open '{p}': Stale file handle", False),
    ("garbage output", "qemu_garbage", "not json at all", False),
]


@pytest.mark.parametrize("label,attr,text,acknowledgeable", CHAIN_FAULTS,
                         ids=[c[0] for c in CHAIN_FAULTS])
def test_backing_chain_read_failure_classification(label, attr, text, acknowledgeable):
    # fmt="qcow2" (par défaut) : la chaîne de sauvegarde est lue par `qemu-img info`
    path = f"{OUT}/chain-fault.qcow2"
    sim = _fault_sim(path, fmt="qcow2")
    getattr(sim, attr)[path] = text.format(p=path)
    _assert_classification(sim, path, acknowledgeable)


def test_backing_chain_timeout_is_never_acknowledgeable():
    path = f"{OUT}/chain-timeout.qcow2"
    sim = _fault_sim(path, fmt="qcow2")
    sim.no_output.add(path)
    _assert_classification(sim, path, False)


# --- nvram, identifiants, TPM dans le clone (I2, I7) --------------------------------------------

def test_clone_xml_new_style_nvram_fixture():
    src = fx("dumpxml-kvmtest-nvram-source.xml")
    vm = kh.parse_domain_xml(src)
    assert vm["nvram"]["type"] == "file" and vm["nvram"]["path"].endswith("_VARS.qcow2")
    root = ET.fromstring(ko.clone_xml(src, "kvmtest-n2", {}))
    nv = root.find("os/nvram")
    assert nv.find("source") is None and not (nv.text or "").strip() and nv.get("type") is None
    assert nv.get("template") == ET.fromstring(src).find("os/nvram").get("template")
    assert kh.parse_domain_xml(ET.tostring(root, encoding="unicode"))["nvram"]["path"] is None
    # identifiants du relevé réel retirés et dits
    assert ko._clone_tree(src, "kvmtest-n2", {})[1] == [
        "genid: a new one is generated by libvirt", "disk vda: serial removed",
        "sysinfo system: serial removed"]


@pytest.mark.parametrize("nv,text", [
    ("<nvram type='block'><source dev='/dev/vg/nv'/></nvram>", "nvram) on a block source"),
    ("<nvram type='network'><source protocol='iscsi' name='t/1'/></nvram>",
     "nvram) on a network source"),
])
def test_clone_refuses_block_or_network_nvram(nv, text):
    xml = dom("src", [disk("vda", f"{POOL}/mine.qcow2")]).replace(
        "<os><type arch='x86_64'>hvm</type>", f"<os><type arch='x86_64'>hvm</type>{nv}")
    with pytest.raises(ko.OpError) as e:
        ko.clone_xml(xml, "n", {})
    assert e.value.kind == "refused" and text in e.value.message
    plan = ko.clone_plan(clone_sim(xml=xml), "src", "src-2")
    assert any(text in r for r in plan["refusals"])


IDENT = ("<genid>43dc0cf8-809b-4adb-9bea-a9abb5f3d90e</genid>"
         "<sysinfo type='smbios'><system><entry name='serial'>S1</entry>"
         "<entry name='uuid'>11111111-2222-3333-4444-555555555555</entry>"
         "<entry name='product'>p</entry></system>"
         "<baseBoard><entry name='serial'>B1</entry></baseBoard>"
         "<chassis><entry name='serial'>C1</entry><entry name='asset'>a</entry></chassis>"
         "</sysinfo>")


def test_clone_xml_drops_and_reports_identifiers():
    vda = disk("vda", f"{POOL}/mine.qcow2").replace(
        "</disk>", "<serial>SN-1</serial><wwn>5000c50015ea71ac</wwn></disk>")
    src = dom("src", [vda], extra=IDENT)
    root, changes = ko._clone_tree(src, "n", {})
    assert root.find("genid") is not None and not root.find("genid").text
    assert root.find("devices/disk/serial") is None and root.find("devices/disk/wwn") is None
    assert [(e.get("name"), e.text) for e in root.iter("entry")] == [
        ("product", "p"), ("asset", "a")]
    assert changes == ["genid: a new one is generated by libvirt", "disk vda: serial removed",
                       "disk vda: wwn removed", "sysinfo system: serial removed",
                       "sysinfo system: uuid removed", "sysinfo baseBoard: serial removed",
                       "sysinfo chassis: serial removed"]
    plan = ko.clone_plan(clone_sim(xml=src), "src", "src-2")
    assert plan["refusals"] == [] and plan["regenerated"] == changes


def test_clone_xml_empty_genid_is_not_reported():
    root, changes = ko._clone_tree(dom("g", extra="<genid/>"), "n", {})
    assert changes == [] and root.find("genid") is not None


@pytest.mark.parametrize("tpm,refusal,lost", [
    ("<tpm model='tpm-crb'><backend type='emulator' version='2.0'/></tpm>", None, True),
    ("<tpm model='tpm-crb'><backend type='emulator' version='2.0'>"
     "<source type='dir' path='/srv/tpm/x'/></backend></tpm>", "explicit state location", None),
    ("<tpm model='tpm-tis'><backend type='passthrough'><device path='/dev/tpm0'/></backend>"
     "</tpm>", "TPM with a passthrough backend", None),
    ("", None, False),
])
def test_clone_tpm(tpm, refusal, lost):
    sim = clone_sim(xml=dom("src", [disk("vda", f"{POOL}/mine.qcow2")], extra_dev=tpm))
    plan = ko.clone_plan(sim, "src", "src-2")
    if refusal:
        assert any(refusal in r for r in plan["refusals"])
        with pytest.raises(ko.OpError):
            ko.clone_xml(sim.domains["src"]["xml"], "n", {})
    else:
        assert plan["refusals"] == [] and plan["tpm_state_lost"] is lost
        assert ko.clone_vm(sim, "src", "src-2")["tpm_state_lost"] is lost


def test_clone_refuses_hostdev_network_card():
    xml = dom("src", [disk("vda", f"{POOL}/mine.qcow2")],
              extra_dev="<interface type='hostdev' managed='yes'><source>"
                        "<address type='pci' domain='0' bus='3' slot='0' function='1'/>"
                        "</source></interface>")
    plan = ko.clone_plan(clone_sim(xml=xml), "src", "src-2")
    assert any("hostdev" in r for r in plan["refusals"])


# --- droits, destinations, course (I3, I5, I6) ---------------------------------------------------

@pytest.mark.parametrize("perms,expected", [
    ({"mode": "0644", "owner": "107", "group": "107"}, ("0644", "107", "107")),
    ({"mode": "0600", "owner": None, "group": "36"}, ("0600", None, "36")),
    (None, None),
])
def test_volume_xml_permissions(perms, expected):
    root = ET.fromstring(ko.volume_xml("v.qcow2", 1 << 20, "qcow2", perms))
    p = root.find("target/permissions")
    if expected is None:
        assert p is None
    else:
        assert (p.findtext("mode"), p.findtext("owner"), p.findtext("group")) == expected


def test_volume_xml_refuses_odd_permissions():
    with pytest.raises(ko.OpError):
        ko.volume_xml("v", 1, "raw", {"mode": "0644</mode><x>"})


def test_clone_vm_pool_copy_keeps_source_permissions():
    sim = clone_sim()
    sim.vol_perms = ("0640", "107", "36")
    ko.clone_vm(sim, "src", "src-2")
    create = next(c for c in sim.calls if c[0] == "input" and "vol-create-from" in c[1])
    p = ET.fromstring(create[2]).find("target/permissions")
    assert (p.findtext("mode"), p.findtext("owner"), p.findtext("group")) == ("0640", "107", "36")


@pytest.mark.parametrize("out,warning", [
    ("linked\nsrc=1000:100:644\ndst=1000:100:644\n", None),
    ("linked\nsrc=107:107:600\ndst=1000:100:600\n", "belongs to 1000:100 (uid:gid), the source "
                                                    "to 107:107: changing the owner needs root"),
    ("linked\nsrc=1000:100:640\ndst=1000:100:644\n", "has mode 644, the source 640"),
    ("linked\ntmpleft\nsrc=1000:100:644\ndst=1000:100:644\n", "could not be removed"),
])
def test_clone_vm_file_copy_reports_ownership(out, warning):
    sim = clone_sim("file")
    sim.commit_out = out
    res = ko.clone_vm(sim, "src", "src-2", sleep=lambda s: None)
    if warning is None:
        assert res["warnings"] == []
    else:
        assert len(res["warnings"]) == 1 and warning in res["warnings"][0]


def test_clone_plan_pool_destination_unregistered_file_refused():
    sim = clone_sim()
    sim.exists.add(f"{POOL}/src-2-vda.qcow2")            # fichier présent, pool pas rafraîchi
    plan = ko.clone_plan(sim, "src", "src-2")
    assert plan["refusals"] == [f"{POOL}/src-2-vda.qcow2 already exists"]


def test_clone_vm_source_started_during_copy_is_discarded():
    sim = clone_sim()
    real = sim.v_vol_create_from

    def started(o, p, data):
        out = real(o, p, data)
        sim.domains["src"]["state"] = "running"
        return out
    sim.v_vol_create_from = started
    with pytest.raises(ko.OpError) as e:
        ko.clone_vm(sim, "src", "src-2")
    assert e.value.kind == "failed" and "src was started during the copy" in e.value.message
    assert "src-2" not in sim.domains and not sim.virsh_calls("define")
    assert "src-2-vda.qcow2" not in sim.pools["kvmtest"]["vols"]
    assert e.value.data["leftovers"] == []


def test_clone_vm_pool_copy_cancelled_names_the_volume_being_built():
    sim = clone_sim()

    def cancel(o, p, data):
        raise Stop()
    sim.v_vol_create_from = cancel
    real = sim._answer

    def answer(args, data=None):
        if str(args[0]) == "vol-delete":
            return 1, "", "error: Requested operation is not valid: volume 'src-2-vda.qcow2' " \
                          "is still being allocated.\n"
        return real(args, data)
    sim._answer = answer
    with pytest.raises(Stop) as e:
        ko.clone_vm(sim, "src", "src-2")
    (left,) = e.value.leftovers
    assert left["volume"] == "src-2-vda.qcow2" and "building" not in left
    assert "libvirtd may still be building volume kvmtest/src-2-vda.qcow2" in left["error"]


def test_clone_vm_copy_that_never_starts_fails_fast():
    sim = clone_sim("file")
    real = sim.run

    def run(argv, timeout=None):
        if argv[:3] == ["sh", "-c", ko._COPY_POLL]:
            sim.calls.append(("run", tuple(argv)))
            return "rc=\npid=\nprogress=\nlast=\n"
        return real(argv, timeout)
    sim.run = run
    with pytest.raises(ko.OpError) as e:
        ko.clone_vm(sim, "src", "src-2", sleep=lambda s: None, poll=1.0)
    assert e.value.kind == "failed" and "did not start on the host" in e.value.message
    polls = [c for c in sim.calls if c[1][2:3] == (ko._COPY_POLL,)]
    assert len(polls) == int(ko.COPY_START_GRACE) + 1


# --- suppression : notes du plan ------------------------------------------------------------------

@pytest.mark.parametrize("managed,tpm,state,notes", [
    (False, "", None, []),
    (True, "", None, ["the saved memory state (managed save) is discarded with the VM"]),
    (False, "<tpm model='tpm-crb'><backend type='emulator' version='2.0'/></tpm>", "removed",
     ["the vTPM state is removed with the VM"]),
    (False, "<tpm model='tpm-crb'><backend type='emulator' version='2.0' "
            "persistent_state='yes'/></tpm>", "kept",
     ["the vTPM state is kept by libvirt (persistent_state)"]),
])
def test_delete_plan_managed_save_and_tpm_notes(managed, tpm, state, notes):
    sim = storage_sim()
    sim.add_domain("v", dom("v", [disk("vda", LONELY)], extra_dev=tpm))
    sim.domains["v"]["managed_save"] = managed
    plan = ko.delete_plan(sim, "v", remove_storage=True)
    assert (plan["managed_save_lost"], plan["tpm_state"], plan["notes"]) == (managed, state, notes)


# --- scripts réels après revue : lien interrompu, PID vérifié, droits ---------------------------

def test_real_copy_keeps_source_mode(local):
    src = local.work / "src.qcow2"
    src.write_text("disk-data")
    src.chmod(0o640)
    e = entry(local.work, src, "new-vda.qcow2")
    created = []
    warnings = ko._copy_file(local.host(), e, created, time.monotonic() + 30, None,
                             time.sleep, 0.1)
    assert warnings == [] and oct((local.work / "new-vda.qcow2").stat().st_mode & 0o777) == "0o640"
    assert leftovers_in(local.work) == []


@pytest.mark.parametrize("state", ["linked, temporary still there", "temporary gone"])
def test_real_rollback_of_an_interrupted_link(local, state):
    tmp = local.work / ".new.hops-abc.tmp"
    dest = local.work / "new.qcow2"
    tmp.write_text("copy")
    (local.work / ".new.hops-abc.tmp.rc").write_text("0\n")
    os.link(tmp, dest)                                   # le lien a eu lieu, puis l'annulation
    if state == "temporary gone":
        tmp.unlink()
    left = ko._rollback(local.host(), [{"kind": "commit", "path": str(tmp),
                                       "destination": str(dest)}])
    assert leftovers_in(local.work) == []
    if state == "temporary gone":
        assert dest.exists() and len(left) == 1 and left[0]["path"] == str(dest)
        assert "origin cannot be proven" in left[0]["error"]
    else:
        assert not dest.exists() and left == []


def test_real_rollback_of_a_link_never_removes_someone_elses_file(local):
    tmp = local.work / ".new.hops-abc.tmp"
    dest = local.work / "new.qcow2"
    tmp.write_text("copy")
    (local.work / ".new.hops-abc.tmp.rc").write_text("0\n")
    dest.write_text("someone else")                      # le lien n'a pas eu lieu
    assert ko._rollback(local.host(), [{"kind": "commit", "path": str(tmp),
                                       "destination": str(dest)}]) == []
    assert dest.read_text() == "someone else" and leftovers_in(local.work) == []


def test_real_kill_checks_the_pid_is_our_qemu_img(local):
    tmp = local.work / ".new.hops-abc.tmp"
    tmp.write_text("partial")
    victim = subprocess.Popen(["sleep", "30"])          # PID étranger écrit dans le fichier
    try:
        (local.work / ".new.hops-abc.tmp.pid").write_text(f"{victim.pid}\n")
        assert ko._rollback(local.host(), [{"kind": "tmp", "path": str(tmp)}]) == []
        assert victim.poll() is None                     # toujours vivant
        assert leftovers_in(local.work) == []
    finally:
        victim.kill()
        victim.wait()
