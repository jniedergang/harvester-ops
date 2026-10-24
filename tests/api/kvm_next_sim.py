"""Hôte simulé de la prochaine version KVM (création, modification, images,
pools, réseaux), bâti sur le `Sim` de test_kvm_ops_197 (chargé par
importlib, comme le font les tests de la CLI 1.97).

Étendu à : vol-create-as, vol-upload (par `run_stream`), vol-resize,
define --validate, attach-device, detach-device, update-device,
change-media, detach-interface, setvcpus, setmem, dommemstat, desc,
autostart, domrename, start, destroy, pool-* et net-*, et aux scripts
shell de kvm_storage/kvm_ops (sonde ISO, empreinte, lecture de seed, ip -j).
Chaque écriture est notée dans `writes` ; `fail_next[commande]` fait
échouer la prochaine occurrence (rc, stderr).
"""

import base64
import hashlib
import importlib.util
import io
import json
import re
import shlex
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import iso9660  # noqa: E402
import kvm_host as kh  # noqa: E402
import kvm_storage as ks  # noqa: E402
import kvm_ops as ko  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "kvm"

_spec = importlib.util.spec_from_file_location("kvm_ops197_world",
                                               ROOT / "tests" / "api" / "test_kvm_ops_197.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)

FAKE_PASSWORD = W.FAKE_PASSWORD
WRITE_VERBS = {"vol-create-as", "vol-create-from", "vol-delete", "vol-resize", "define",
               "undefine", "attach-device", "detach-device", "update-device", "change-media",
               "detach-interface", "setvcpus", "setmem", "desc", "autostart", "domrename",
               "start", "destroy", "pool-define", "pool-build", "pool-start", "pool-destroy",
               "pool-delete", "pool-undefine", "pool-autostart", "pool-refresh", "net-define",
               "net-start", "net-destroy", "net-undefine", "net-autostart", "vol-upload"}


def fx(name):
    return (FIX / name).read_text()


class Sim2(W.Sim):
    """Hôte simulé étendu."""

    def __init__(self):
        super().__init__()
        self.decl = {"uri": "qemu:///system"}
        self.contents = {}          # (pool, vol) -> octets téléversés
        self.writes = []            # commandes d'écriture, dans l'ordre
        self.fail_next = {}         # commande -> (rc, stderr) pour la prochaine occurrence
        self.streams = []           # (argv, octets, fresh)
        self.networks = {}
        self.autostart = {}
        self.memstat_follow = True
        self.link_json = fx("ip-link.json")
        self.addr_json = fx("ip-addr.json")
        self.route_json = fx("ip-route.json")
        self.sha256sum = True
        self.vcpu_unplug_ok = True
        self.detach_sticky = False  # l'invité ne libère pas le disque
        self.caps = fx("capabilities-libvirt-12.8.xml")
        self.domcaps = {"q35": fx("domcapabilities-q35.xml"), "pc": fx("domcapabilities-pc.xml")}

    # -- monde
    def add_pool(self, name, path, available=10 ** 12, type="dir", state="running",
                 autostart=True, persistent=True):
        super().add_pool(name, path, available, type)
        self.pools[name].update(state=state, autostart=autostart, persistent=persistent)

    def add_net(self, name, subnet=None, active=True, bridge=None, forward=None):
        self.networks[name] = {"subnet": subnet, "active": active, "bridge": bridge,
                               "forward": forward, "autostart": False, "persistent": True}

    # -- interception commune (`_answer` : voir _answer2 plus bas)
    def run(self, argv, timeout=None, idempotent=False, fresh=False):
        argv = list(argv)
        if argv[:2] == ["sh", "-c"] and argv[2] in (ks._SHA_SCRIPT, ko._SEED_READ):
            self.modes.append(("run", "sh:" + ("sha" if argv[2] == ks._SHA_SCRIPT else "seed"),
                               idempotent))
            self.calls.append(("run", tuple(argv)))
            pool, vol = argv[5], argv[6]
            data = self._vol_bytes(pool, vol)
            if argv[2] == ks._SHA_SCRIPT:
                if not self.sha256sum:
                    return "nosha256sum\n"
                return hashlib.sha256(data).hexdigest() + "  -\n"
            return base64.b64encode(data).decode() + "\n"
        return super().run(argv, timeout=timeout, idempotent=idempotent)

    def _vol_bytes(self, pool, vol):
        if (pool, vol) not in self.contents:
            raise kh.make_error(self.name, f"error: failed to get vol '{vol}'\n", 1)
        return self.contents[(pool, vol)]

    def run_stream(self, remote_argv, source, size=None, progress=None, cancel=None,
                   limit_bps=None, finish_timeout=None):
        argv = list(remote_argv)
        assert argv[:6] == ["env", "LC_ALL=C", "virsh", "-q", "-c", "qemu:///system"], argv
        assert argv[6] == "vol-upload" and "--sparse" not in argv
        opts = dict(zip(argv[7::2], argv[8::2]))
        assert opts["--file"] == "/dev/stdin"
        src = open(source, "rb") if not hasattr(source, "read") else source
        data = b""
        while True:
            if cancel is not None and cancel():
                raise kh.Cancelled(len(data))
            block = src.read(1 << 20)
            if not block:
                break
            data += block
            if progress:
                progress(len(data))
        if not hasattr(source, "read"):
            src.close()
        self.writes.append(("vol-upload", opts["--pool"], opts["--vol"]))
        self.streams.append((tuple(argv), len(data)))
        if "vol-upload" in self.fail_next:
            rc, err = self.fail_next.pop("vol-upload")
            raise kh.make_error(self.name, err, rc, ssh_level=rc == kh.SSH_RC)
        vols = self.pools[opts["--pool"]]["vols"]
        if opts["--vol"] not in vols:
            raise kh.make_error(self.name, f"error: failed to get vol '{opts['--vol']}'\n", 1)
        self.contents[(opts["--pool"], opts["--vol"])] = data
        v = vols[opts["--vol"]]
        v["alloc"] = len(data)
        if data[:4] == b"QFI\xfb":
            v["fmt"] = "qcow2"
            v["cap"] = int.from_bytes(data[24:32], "big")
        elif data[32769:32774] == b"CD001":
            v["fmt"] = "iso"
        return ""

    def _shell(self, argv):
        if argv[:3] == ["ip", "-j", "-d"]:
            return 0, self.link_json
        if argv[:3] == ["ip", "-j", "addr"]:
            return 0, self.addr_json
        if argv[:3] == ["ip", "-j", "route"]:
            return 0, self.route_json
        if argv[:2] == ["sh", "-c"] and argv[2] == ks._ISO_PROBE:
            pool, vol = argv[5], argv[6]
            data = self.contents.get((pool, vol), b"")
            return 0, " C D 0 0 1\n" if data[32769:32774] == b"CD001" else " \\0 \\0\n"
        return super()._shell(argv)

    # -- volumes
    def _pool(self, name):
        return self.pools.get(name)

    def v_vol_create_as(self, o, p, data):
        pl = self._pool(o["pool"])
        if pl is None:
            return 1, "", f"error: failed to get pool '{o['pool']}'\n"
        if o["name"] in pl["vols"]:
            return 1, "", f"error: storage volume name '{o['name']}' already in use.\n"
        cap = int(o["capacity"])
        backing = None
        if o.get("backing-vol"):
            b = pl["vols"].get(o["backing-vol"])
            if b is None:
                return 1, "", f"error: failed to get vol '{o['backing-vol']}'\n"
            backing = b["path"]
        self.add_vol(o["pool"], o["name"], cap=cap, alloc=0, fmt=o.get("format") or "raw",
                     backing=backing)
        return 0, f"Vol {o['name']} created\n", ""

    def v_vol_resize(self, o, p, data):
        v = self.pools[o["pool"]]["vols"].get(o["vol"])
        if v is None:
            return 1, "", f"error: failed to get vol '{o['vol']}'\n"
        v["cap"] = int(o["capacity"])
        return 0, "", ""

    def v_vol_info(self, o, p, data):
        v = self.pools[o["pool"]]["vols"].get(o["vol"])
        if v is None:
            return 1, "", f"error: failed to get vol '{o['vol']}'\n"
        return 0, (f"Name:           {o['vol']}\nType:           file\n"
                   f"Capacity:       {v['cap']} bytes\nAllocation:     {v['alloc']} bytes\n"), ""

    def v_pool_refresh(self, o, p, data):
        return 0, "", ""

    def v_vol_list(self, o, p, data):
        pl = self._pool(o["pool"])
        if pl is None:
            return 1, "", f"error: failed to get pool '{o['pool']}'\n"
        if pl.get("state", "running") != "running":
            return 1, "", "error: Requested operation is not valid: storage pool is not active\n"
        return super().v_vol_list(o, p, data)

    # -- pools
    def v_pool_list(self, o, p, data):
        names = [n for n, pl in self.pools.items()
                 if o.get("all") or pl.get("state", "running") == "running"]
        return 0, "".join(f"{n}\n" for n in names) + "\n", ""

    def v_pool_dumpxml(self, o, p, data):
        pl = self._pool(o["pool"])
        if pl is None:
            return 1, "", f"error: failed to get pool '{o['pool']}'\n"
        src = ""
        if pl["type"] == "netfs":
            src = (f"<source><host name='{pl.get('host', 'nas')}'/><dir path='/export'/>"
                   "<format type='nfs'/></source>")
        return 0, (f"<pool type='{pl['type']}'><name>{o['pool']}</name>"
                   f"<available unit='bytes'>{pl['available']}</available>{src}"
                   f"<target><path>{pl['path']}</path></target></pool>"), ""

    def v_pool_info(self, o, p, data):
        pl = self._pool(o["pool"])
        if pl is None:
            return 1, "", f"error: failed to get pool '{o['pool']}'\n"
        return 0, (f"Name:           {o['pool']}\nState:          {pl.get('state', 'running')}\n"
                   f"Persistent:     {'yes' if pl.get('persistent', True) else 'no'}\n"
                   f"Autostart:      {'yes' if pl.get('autostart') else 'no'}\n"), ""

    def v_pool_define(self, o, p, data):
        assert o["file"] == "/dev/stdin" and o.get("validate") and data
        root = ET.fromstring(data)
        name = root.findtext("name")
        if name in self.pools:
            return 1, "", f"error: operation failed: pool '{name}' already exists\n"
        self.add_pool(name, root.findtext("target/path"), type=root.get("type"),
                      state="inactive", autostart=False)
        self.pools[name]["defined_xml"] = data
        return 0, f"Pool {name} defined from /dev/stdin\n", ""

    def v_pool_build(self, o, p, data):
        self.exists.add(self.pools[o["pool"]]["path"])
        return 0, "", ""

    def v_pool_start(self, o, p, data):
        self.pools[o["pool"]]["state"] = "running"
        return 0, "", ""

    def v_pool_destroy(self, o, p, data):
        self.pools[o["pool"]]["state"] = "inactive"
        return 0, "", ""

    def v_pool_delete(self, o, p, data):
        self.exists.discard(self.pools[o["pool"]]["path"])
        return 0, "", ""

    def v_pool_undefine(self, o, p, data):
        del self.pools[o["pool"]]
        return 0, "", ""

    def v_pool_autostart(self, o, p, data):
        self.pools[o["pool"]]["autostart"] = not o.get("disable")
        return 0, "", ""

    # -- réseaux
    def v_net_list(self, o, p, data):
        rows = [[n, "active" if v["active"] else "inactive",
                 "yes" if v["autostart"] else "no", "yes" if v["persistent"] else "no"]
                for n, v in self.networks.items()]
        return 0, W.table(["Name", "State", "Autostart", "Persistent"], rows), ""

    def v_net_dumpxml(self, o, p, data):
        n = self.networks.get(o["network"])
        if n is None:
            return 1, "", f"error: failed to get network '{o['network']}'\n"
        if "xml" in n:
            return 0, n["xml"], ""
        fwd = f"<forward mode='{n['forward']}'/>" if n["forward"] else ""
        br = f"<bridge name='{n['bridge']}'/>" if n["bridge"] else ""
        ip = ""
        if n["subnet"]:
            import ipaddress
            net = ipaddress.ip_network(n["subnet"])
            ip = f"<ip address='{next(net.hosts())}' prefix='{net.prefixlen}'/>"
        return 0, f"<network><name>{o['network']}</name>{fwd}{br}{ip}</network>", ""

    def v_net_define(self, o, p, data):
        assert o["file"] == "/dev/stdin" and o.get("validate") and data
        root = ET.fromstring(data)
        name = root.findtext("name")
        self.networks[name] = {"xml": data, "active": False, "autostart": False,
                               "persistent": True, "subnet": None, "bridge": None,
                               "forward": None}
        return 0, f"Network {name} defined from /dev/stdin\n", ""

    def v_net_start(self, o, p, data):
        self.networks[o["network"]]["active"] = True
        return 0, "", ""

    def v_net_destroy(self, o, p, data):
        self.networks[o["network"]]["active"] = False
        return 0, "", ""

    def v_net_undefine(self, o, p, data):
        del self.networks[o["network"]]
        return 0, "", ""

    def v_net_autostart(self, o, p, data):
        self.networks[o["network"]]["autostart"] = not o.get("disable")
        return 0, "", ""

    # -- capacités
    def v_capabilities(self, o, p, data):
        return 0, self.caps, ""

    def v_domcapabilities(self, o, p, data):
        return 0, self.domcaps[o["machine"]], ""

    # -- domaines
    def v_define(self, o, p, data):
        assert p == ["/dev/stdin"] and data is not None
        root = ET.fromstring(data)
        name = root.findtext("name")
        if name in self.domains:
            # redéfinition d'un domaine existant (modification)
            self.domains[name]["xml"] = data
            return 0, f"Domain '{name}' defined from /dev/stdin\n", ""
        return super().v_define(o, p, data)

    def v_dominfo(self, o, p, data):
        rc, out, err = super().v_dominfo(o, p, data)
        if rc == 0:
            out = out.replace("Autostart:      disable",
                              "Autostart:      " + ("enable" if self.autostart.get(o["domain"])
                                                    else "disable"))
        return rc, out, err

    def v_autostart(self, o, p, data):
        self.autostart[o["domain"]] = not o.get("disable")
        return 0, "", ""

    def v_start(self, o, p, data):
        d = self.domains[o["domain"]]
        d["state"] = "running"
        d["live"] = d["xml"]
        return 0, "", ""

    def v_destroy(self, o, p, data):
        d = self.domains.get(o["domain"])
        if d is None:
            return self._nodom(o["domain"])
        if d["state"] != "running":
            return 1, "", "error: Requested operation is not valid: domain is not running\n"
        d["state"] = "shut off"
        d["live"] = None
        return 0, "", ""

    def _both(self, name, o, fn):
        """Applique `fn(root)` au XML vivant (--live) et/ou inactif (--config)."""
        d = self.domains[name]
        if o.get("config"):
            root = ET.fromstring(d["xml"])
            fn(root)
            d["xml"] = ET.tostring(root, encoding="unicode")
        if o.get("live") and d["state"] == "running":
            root = ET.fromstring(d["live"] or d["xml"])
            fn(root)
            d["live"] = ET.tostring(root, encoding="unicode")

    def v_attach_device(self, o, p, data):
        assert o["file"] == "/dev/stdin" and data
        el = ET.fromstring(data)
        if el.tag == "interface" and el.find("mac") is None:
            # libvirt tire une MAC ; la même pour --live et --config
            self.mac_seq = getattr(self, "mac_seq", 0) + 1
            ET.SubElement(el, "mac", address=f"52:54:00:ee:00:{self.mac_seq:02x}")
        dev = ET.tostring(el, encoding="unicode")
        self._both(o["domain"], o, lambda r: r.find("devices").append(ET.fromstring(dev)))
        return 0, f"Device attached successfully ({el.tag})\n", ""

    def v_detach_device(self, o, p, data):
        assert o["file"] == "/dev/stdin" and data
        target = ET.fromstring(data).find("target").get("dev")

        def drop(root, live=False):
            devs = root.find("devices")
            for dk in devs.findall("disk"):
                if dk.find("target").get("dev") == target:
                    devs.remove(dk)
        d = self.domains[o["domain"]]
        if o.get("config"):
            root = ET.fromstring(d["xml"])
            drop(root)
            d["xml"] = ET.tostring(root, encoding="unicode")
        if o.get("live") and d["state"] == "running" and not self.detach_sticky:
            root = ET.fromstring(d["live"] or d["xml"])
            drop(root)
            d["live"] = ET.tostring(root, encoding="unicode")
        return 0, "Device detached successfully\n", ""

    def v_update_device(self, o, p, data):
        assert o["file"] == "/dev/stdin" and data
        new = ET.fromstring(data)
        target = new.find("target").get("dev")

        def upd(root):
            devs = root.find("devices")
            for i, dk in enumerate(list(devs)):
                if dk.tag == "disk" and dk.find("target").get("dev") == target:
                    devs.remove(dk)
                    devs.insert(i, ET.fromstring(data))
        self._both(o["domain"], o, upd)
        return 0, "Device updated successfully\n", ""

    def v_change_media(self, o, p, data):
        target = o["path"]

        def ej(root):
            for dk in root.findall("devices/disk"):
                if dk.find("target").get("dev") == target:
                    for s in dk.findall("source"):
                        dk.remove(s)
        self._both(o["domain"], o, ej)
        return 0, "Successfully ejected media.\n", ""

    def v_detach_interface(self, o, p, data):
        mac = o["mac"]

        def drop(root):
            devs = root.find("devices")
            for i in devs.findall("interface"):
                m = i.find("mac")
                if m is not None and m.get("address") == mac:
                    devs.remove(i)
        self._both(o["domain"], o, drop)
        return 0, "Interface detached successfully\n", ""

    def v_setvcpus(self, o, p, data):
        n = int(o["count"])
        d = self.domains[o["domain"]]
        if o.get("live") and d["state"] == "running" and not self.vcpu_unplug_ok:
            cur = ET.fromstring(d["live"]).find("vcpu")
            if n < int(cur.get("current") or cur.text):
                return 1, "", "error: operation failed: vcpu unplug request timed out\n"

        def setv(root):
            v = root.find("vcpu")
            if n == int(v.text):
                v.attrib.pop("current", None)
            else:
                v.set("current", str(n))
        self._both(o["domain"], o, setv)
        return 0, "", ""

    def v_setmem(self, o, p, data):
        kib = int(o["size"])

        def setm(root):
            root.find("currentMemory").text = str(kib)
        self._both(o["domain"], o, setm)
        self.domains[o["domain"]]["actual"] = kib if self.memstat_follow else None
        return 0, "", ""

    def v_dommemstat(self, o, p, data):
        d = self.domains[o["domain"]]
        actual = d.get("actual") or int(ET.fromstring(d["live"] or d["xml"])
                                        .findtext("memory") or 0)
        return 0, f"actual {actual}\nswap_in 0\nrss 1000\n\n", ""

    def v_desc(self, o, p, data):
        text = o.get("new-desc") or ""
        tag = "title" if o.get("title") else "description"

        def setd(root):
            el = root.find(tag)
            if el is None:
                el = ET.Element(tag)
                root.insert(2, el)
            el.text = text
        self._both(o["domain"], o, setd)
        return 0, "", ""

    def v_snapshot_list(self, o, p, data):
        if o.get("name") and not (o.get("internal") or o.get("external")):
            d = self._dom(o["domain"])
            if d is None:
                return self._nodom(o["domain"])
            return 0, "".join(f"{x[0]}\n" for x in d["snaps"]) + "\n", ""
        return super().v_snapshot_list(o, p, data)

    def v_domrename(self, o, p, data):
        d = self.domains.pop(o["domain"])
        root = ET.fromstring(d["xml"])
        root.find("name").text = o["new-name"]
        d["xml"] = ET.tostring(root, encoding="unicode")
        self.domains[o["new-name"]] = d
        return 0, "", ""


# options à valeur des nouvelles commandes (le `Sim` de 1.98 ne les connaît pas)
_VALUED = ("--domain", "--pool", "--vol", "--snapshotname", "--description", "--file",
           "--inputpool", "--storage", "--newname", "--name", "--capacity", "--format",
           "--backing-vol", "--backing-vol-format", "--count", "--size", "--network",
           "--type", "--mac", "--path", "--new-desc", "--new-name", "--virttype", "--arch",
           "--machine", "--source", "--offset", "--length")


def _parse(cmd, args):
    opts, pos, i = {}, [], 1
    while i < len(args):
        a = args[i]
        if a in _VALUED and not (a == "--name" and cmd in ("list", "pool-list", "net-list",
                                                            "snapshot-list")):
            opts[a[2:]] = args[i + 1]
            i += 2
        elif a.startswith("--"):
            opts[a[2:]] = True
            i += 1
        else:
            pos.append(a)
            i += 1
    return opts, pos


def _answer2(self, args, data=None):
    args = list(map(str, args))
    if args and args[0] in WRITE_VERBS:
        self.writes.append(tuple(args))
    cmd = args[0]
    if cmd in self.fail_next:
        rc, err = self.fail_next.pop(cmd)
        return rc, "", err
    if cmd in self.fail:
        return (*self.fail[cmd][:1], "", self.fail[cmd][1])
    handler = getattr(self, "v_" + cmd.replace("-", "_"), None)
    if handler is None:
        return 1, "", f"error: unexpected command {cmd}\n"
    opts, pos = _parse(cmd, args)
    return handler(opts, pos, data)


Sim2._answer = _answer2


def host_view(sim):
    """Lecture d'un volume téléversé comme ISO (pour les tests de seed)."""
    return {k: iso9660.read_iso(v) for k, v in sim.contents.items() if v[32769:32774] == b"CD001"}


def write_file(path, data):
    Path(path).write_bytes(data)
    return str(path)


__all__ = ["Sim2", "W", "fx", "FAKE_PASSWORD", "host_view", "write_file", "io", "json", "re",
           "shlex"]
