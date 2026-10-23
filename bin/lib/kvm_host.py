"""Hôtes KVM pilotés par `virsh` à travers SSH (v1.96.0).

Aucun agent sur l'hôte et aucun module `libvirt` dans la console : `virsh`
est lancé SUR l'hôte par `ssh`, et sa sortie est lue ici. Deux moitiés :

- l'exécution (`ssh_argv`, `VirshHost`) : options SSH strictes (empreinte
  épinglée, jamais `accept-new`), connexion réutilisée par ControlMaster,
  délai par commande, et `batch()` qui passe plusieurs commandes en UN seul
  appel SSH ;
- les analyseurs, purs, testés sur des sorties réelles
  (tests/fixtures/kvm/, libvirt 12.7.0).

`ssh` recolle ses arguments en une ligne lue par le shell distant : chaque
argument distant passe par `shlex.quote`, un nom de VM ne peut donc jamais
injecter de commande. `LC_ALL=C` est posé à distance : sinon `virsh` répond
dans la langue de l'hôte (« État », « impossible de récupérer le domaine »)
et ni les analyseurs ni le classement des erreurs ne s'y retrouvent.

Les erreurs sont des `KvmError` classées (`kind`) ; leur message nomme
l'hôte, jamais un chemin de clé ni un secret.
"""

import ipaddress
import re
import secrets
import shlex
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import kvm_decl

KINDS = ("unreachable", "hostkey", "auth", "denied", "notfound", "timeout", "failed")

# Classement des erreurs de ssh puis de virsh, dans cet ordre : le refus de
# clé de ssh (« Permission denied (publickey ») passe avant le refus de
# libvirt (« ...virtqemud-sock': Permission denied »).
_ERROR_MARKERS = (
    ("hostkey", ("host key verification failed", "remote host identification has changed",
                 "no matching host key", "host key for")),
    ("auth", ("permission denied (publickey", "permission denied (password",
              "too many authentication failures", "no supported authentication methods")),
    ("unreachable", ("connection refused", "connection timed out", "no route to host",
                     "could not resolve hostname", "network is unreachable",
                     "operation timed out", "connection closed by", "connection reset")),
    ("notfound", ("failed to get domain", "domain not found", "failed to get pool",
                  "storage pool not found", "failed to get network", "network not found",
                  "failed to get vol", "storage volume not found")),
    ("denied", ("authentication unavailable", "permission denied", "access denied",
                "authentication failed")),
)

# virDomainState -> libellé de `virsh domstate`
DOMAIN_STATES = {0: "no state", 1: "running", 2: "idle", 3: "paused", 4: "in shutdown",
                 5: "shut off", 6: "crashed", 7: "pmsuspended"}

_SIZE_UNITS = {"b": 1, "bytes": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3,
               "tib": 1024 ** 4, "pib": 1024 ** 5, "eib": 1024 ** 6}
# unités de libvirt (XML de domaine) : k/KiB = 1024, KB = 1000
_XML_UNITS = {"b": 1, "bytes": 1, "k": 1024, "kib": 1024, "kb": 1000, "m": 1024 ** 2,
              "mib": 1024 ** 2, "mb": 1000 ** 2, "g": 1024 ** 3, "gib": 1024 ** 3,
              "gb": 1000 ** 3, "t": 1024 ** 4, "tib": 1024 ** 4, "tb": 1000 ** 4}


class KvmError(Exception):
    """Échec classé d'un appel ssh/virsh. `kind` parmi KINDS ; `message`
    nomme l'hôte et la raison, sans chemin de clé ni secret."""

    def __init__(self, kind, message, host=None, rc=None):
        super().__init__(message)
        self.kind = kind if kind in KINDS else "failed"
        self.message = message
        self.host = host
        self.rc = rc


class CmdOutput(str):
    """Sortie d'une commande d'un `batch` : une chaîne (la sortie, ou le
    texte d'erreur en cas d'échec), avec `rc` et `error` (KvmError ou None)."""

    def __new__(cls, text, rc=0, error=None):
        obj = super().__new__(cls, text)
        obj.rc, obj.error = rc, error
        return obj

    @property
    def ok(self):
        return self.rc == 0 and self.error is None


def classify(stderr):
    """Genre d'une erreur ssh/virsh d'après son texte."""
    low = (stderr or "").lower()
    for kind, markers in _ERROR_MARKERS:
        if any(m in low for m in markers):
            return kind
    return "failed"


def _redact(text, paths):
    for p in paths:
        if p:
            text = text.replace(str(p), "<state>")
    return text


def _error_detail(stderr, paths):
    """Dernière ligne utile de stderr, chemins de l'état masqués."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.lower().startswith(("warning:", "debug"))] or lines
    return _redact(lines[-1], paths)[:300] if lines else ""


def make_error(host, stderr, rc, paths=()):
    kind = classify(stderr)
    detail = _error_detail(stderr, paths)
    return KvmError(kind, f"{host}: {kind}" + (f": {detail}" if detail else ""), host=host, rc=rc)


def _ssh_opt_path(path):
    # ssh découpe la valeur d'une option -o sur les espaces : on la cite
    path = str(path)
    return f'"{path}"' if re.search(r"\s", path) else path


def control_path(state):
    return Path(state) / kvm_decl.SSH_DIR / "cm-%C"


def ssh_argv(decl, state, remote_argv):
    """Ligne de commande ssh qui lance `remote_argv` sur l'hôte. Chaque
    élément distant est cité (shlex.quote) et le tout part en UN argument :
    le shell distant le redécoupe exactement en `remote_argv`."""
    why = kvm_decl.check_decl(kvm_decl.normalize(decl))
    if why:
        raise KvmError("failed", f"{decl.get('name') if isinstance(decl, dict) else '?'}: "
                       f"invalid declaration: {why}")
    key = kvm_decl.resolve(state, decl["ssh_key"])
    known = kvm_decl.resolve(state, decl["known_hosts"])
    remote = " ".join(shlex.quote(str(a)) for a in remote_argv)
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={_ssh_opt_path(known)}",
        # seule l'empreinte épinglée compte, pas celles du système
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "IdentitiesOnly=yes",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={_ssh_opt_path(control_path(state))}",
        "-o", "ControlPersist=60",
        "-i", str(key),
        "-p", str(decl.get("port", kvm_decl.DEFAULT_PORT)),
        "-l", decl["user"],
        "--", decl["address"],
        remote,
    ]


class VirshHost:
    """Un hôte KVM déclaré, interrogé par `virsh` à travers SSH."""

    def __init__(self, decl, state, runner=subprocess.run, timeout=30):
        self.decl = kvm_decl.normalize(decl)
        self.state = state
        self.runner = runner
        self.timeout = timeout
        self.name = self.decl.get("name")

    def _secret_paths(self):
        paths = [str(Path(self.state))]
        for field in ("ssh_key", "known_hosts"):
            try:
                paths.append(str(kvm_decl.resolve(self.state, self.decl.get(field))))
            except kvm_decl.DeclError:
                pass
        return sorted(paths, key=len, reverse=True)

    def _virsh_argv(self, args):
        return ["virsh", "-c", self.decl.get("uri", kvm_decl.DEFAULT_URI), *map(str, args)]

    def _ssh(self, remote_argv, timeout):
        argv = ssh_argv(self.decl, self.state, remote_argv)
        try:
            kvm_decl.ensure_ssh_dir(self.state)
        except OSError:
            pass                            # ssh dira lui-même ce qui manque
        try:
            return self.runner(argv, capture_output=True, text=True,
                               timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired as e:
            raise KvmError("timeout", f"{self.name}: timeout after {timeout or self.timeout}s",
                           host=self.name) from e
        except OSError as e:
            raise KvmError("failed", f"{self.name}: failed: cannot run ssh ({e.strerror})",
                           host=self.name) from e

    def run(self, remote_argv, timeout=None):
        """Lance une commande quelconque sur l'hôte ; rend sa sortie."""
        res = self._ssh(remote_argv, timeout)
        if res.returncode != 0:
            raise make_error(self.name, res.stderr, res.returncode, self._secret_paths())
        return res.stdout or ""

    def virsh(self, *args, timeout=None):
        """Une commande virsh ; rend sa sortie, KvmError classée sinon."""
        return self.run(["env", "LC_ALL=C", *self._virsh_argv(args)], timeout=timeout)

    def batch(self, commands, timeout=None):
        """Plusieurs commandes virsh en UN appel SSH. `commands` : un dict
        {clé: (arguments virsh)} ou une suite de tuples (clé = arguments
        joints par des espaces). Rend {clé: CmdOutput} ; une commande en échec
        n'efface pas les autres, sa valeur porte son erreur (`.error`).

        Chaque commande est suivie d'une ligne repère `<jeton> <i> <rc>` sur
        stdout et `<jeton> <i>` sur stderr ; le jeton est tiré au hasard à
        chaque appel, une sortie ne peut donc pas l'imiter."""
        if isinstance(commands, dict):
            items = [(str(k), tuple(v)) for k, v in commands.items()]
        else:
            items = [(" ".join(map(str, c)), tuple(c)) for c in commands]
        if not items:
            return {}
        token = "@@kvm-" + secrets.token_hex(12)
        lines = ["export LC_ALL=C"]
        for i, (_, args) in enumerate(items):
            cmd = " ".join(shlex.quote(a) for a in self._virsh_argv(args))
            lines.append(f"{cmd} </dev/null; printf '\\n%s %d %d\\n' {token} {i} \"$?\"; "
                         f"printf '\\n%s %d\\n' {token} {i} >&2")
        res = self._ssh(["sh", "-c", "\n".join(lines)], timeout)
        out_parts = re.split(rf"\n{re.escape(token)} (\d+) (-?\d+)\n", res.stdout or "")
        err_parts = re.split(rf"\n{re.escape(token)} (\d+)\n", res.stderr or "")
        if len(out_parts) < 3:
            # aucun repère : ssh n'a rien lancé (hôte, empreinte, clé)
            raise make_error(self.name, res.stderr, res.returncode, self._secret_paths())
        outs, rcs = {}, {}
        for j in range(0, len(out_parts) - 2, 3):
            idx = int(out_parts[j + 1])
            outs[idx], rcs[idx] = out_parts[j], int(out_parts[j + 2])
        errs = {}
        for j in range(0, len(err_parts) - 1, 2):
            errs[int(err_parts[j + 1])] = err_parts[j]
        paths = self._secret_paths()
        result = {}
        for i, (key, _) in enumerate(items):
            if i not in rcs:                 # coupure en cours de route
                err = KvmError("failed", f"{self.name}: failed: no output for '{key}'",
                               host=self.name, rc=res.returncode)
                result[key] = CmdOutput(err.message, rc=-1, error=err)
            elif rcs[i] != 0:
                err = make_error(self.name, errs.get(i, ""), rcs[i], paths)
                result[key] = CmdOutput(_redact(errs.get(i, ""), paths) or err.message,
                                        rc=rcs[i], error=err)
            else:
                result[key] = CmdOutput(outs[i])
        return result


# --- analyseurs ---------------------------------------------------------

def _int(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def parse_size(text):
    """« 931.06 GiB » -> octets (int) ; None pour « - » ou illisible."""
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*$", text or "")
    if not m or m.group(2).lower() not in _SIZE_UNITS:
        return None
    return int(round(float(m.group(1)) * _SIZE_UNITS[m.group(2).lower()]))


def _colon_fields(text):
    out = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _kib(v):
    m = re.match(r"^\s*(\d+)\s*KiB\s*$", v or "")
    return int(m.group(1)) if m else None


def parse_nodeinfo(text):
    f = _colon_fields(text)
    mhz = re.match(r"(\d+)", f.get("CPU frequency", ""))
    return {"cpu_model": f.get("CPU model"), "cpus": _int(f.get("CPU(s)")),
            "mhz": int(mhz.group(1)) if mhz else None,
            "sockets": _int(f.get("CPU socket(s)")),
            "cores_per_socket": _int(f.get("Core(s) per socket")),
            "threads_per_core": _int(f.get("Thread(s) per core")),
            "numa_cells": _int(f.get("NUMA cell(s)")),
            "memory_kib": _kib(f.get("Memory size"))}


def parse_nodememstats(text):
    """{"total_kib", "free_kib", "available_kib", "buffers_kib", "cached_kib"}."""
    return {f"{k}_kib": _kib(v) for k, v in _colon_fields(text).items() if _kib(v) is not None}


def parse_version(text):
    lib = re.search(r"Using library: libvirt (\S+)", text or "")
    hyp = re.search(r"Running hypervisor: (\S+) (\S+)", text or "")
    return {"libvirt": lib.group(1) if lib else None,
            "hypervisor": hyp.group(1) if hyp else None,
            "qemu": hyp.group(2) if hyp and hyp.group(1) == "QEMU" else None}


def _parse_table(text):
    """Tableau de virsh (en-tête, tirets, lignes) -> [{colonne: valeur}].
    Les colonnes sont alignées à gauche sur leur en-tête : on découpe aux
    positions des titres, ce qui garde les valeurs à espaces (« shut off »,
    « 931.06 GiB »)."""
    lines = (text or "").splitlines()
    head = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if head is None or head + 1 >= len(lines) or not set(lines[head + 1].strip()) <= {"-"}:
        return []
    cols = [(m.start(), m.group(0)) for m in re.finditer(r"\S+", lines[head])]
    rows = []
    for ln in lines[head + 2:]:
        if not ln.strip():
            continue
        row = {}
        for n, (start, title) in enumerate(cols):
            end = cols[n + 1][0] if n + 1 < len(cols) else None
            row[title] = ln[start:end].strip()
        rows.append(row)
    return rows


def _yes(v):
    return str(v).strip().lower() in ("yes", "enable", "enabled", "true")


def parse_list_all(text):
    """`virsh list --all` -> [{"name", "state", "id"}] (id None si arrêtée)."""
    return [{"name": r.get("Name", ""), "state": r.get("State", ""),
             "id": _int(r.get("Id"))} for r in _parse_table(text)]


def parse_names(text):
    """`virsh list --name` : un nom par ligne."""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def parse_dominfo(text):
    f = _colon_fields(text)
    cpu_time = re.match(r"([0-9.]+)s", f.get("CPU time", ""))
    return {"id": _int(f.get("Id")), "name": f.get("Name"), "uuid": f.get("UUID"),
            "state": f.get("State"), "vcpus": _int(f.get("CPU(s)")),
            "cpu_time_s": float(cpu_time.group(1)) if cpu_time else None,
            "max_memory_kib": _kib(f.get("Max memory")),
            "used_memory_kib": _kib(f.get("Used memory")),
            "persistent": _yes(f.get("Persistent")), "autostart": _yes(f.get("Autostart")),
            "managed_save": _yes(f.get("Managed save"))}


def _safe_xml(text):
    # pas de DOCTYPE : rien à faire d'entités dans un XML de libvirt
    if "<!DOCTYPE" in (text or "") or "<!ENTITY" in (text or ""):
        raise ValueError("XML with a DOCTYPE refused")
    return ET.fromstring(text)


def _to_kib(el):
    if el is None or _int(el.text) is None:
        return None
    unit = (el.get("unit") or "KiB").lower()
    return _int(el.text) * _XML_UNITS.get(unit, 1024) // 1024


def _source(el):
    """Source d'un disque ou d'une carte : le premier attribut porteur."""
    if el is None:
        return None
    for attr in ("file", "dev", "bridge", "network", "name", "path", "dir", "volume"):
        if el.get(attr):
            if attr == "volume" and el.get("pool"):
                return f"{el.get('pool')}/{el.get('volume')}"
            return el.get(attr)
    return None


def parse_domain_xml(text):
    """XML de `virsh dumpxml` -> dict (nom, uuid, vCPU, mémoire en Kio,
    épinglage/ordonnanceur, machine, micrologiciel, disques, cartes,
    affichages). Lecture seule : on ne régénère jamais ce XML, le modifier
    se fera de façon ciblée pour ne pas perdre un réglage (épinglage, temps
    réel)."""
    root = _safe_xml(text)
    vcpu = root.find("vcpu")
    os_el = root.find("os")
    os_type = os_el.find("type") if os_el is not None else None
    cputune = root.find("cputune")
    pinned = bool((vcpu is not None and vcpu.get("cpuset")) or (cputune is not None and any(
        cputune.find(t) is not None for t in ("vcpupin", "emulatorpin", "iothreadpin"))))
    rt = bool(cputune is not None and any(
        cputune.find(t) is not None for t in ("vcpusched", "emulatorsched", "iothreadsched")))
    firmware = None
    if os_el is not None:
        loader = os_el.find("loader")
        firmware = os_el.get("firmware") or (
            "efi" if loader is not None and loader.get("type") == "pflash" else "bios")
    devices = root.find("devices")
    if devices is None:
        devices = ET.Element("devices")
    disks = []
    for d in devices.findall("disk"):
        tgt, drv = d.find("target"), d.find("driver")
        disks.append({"target": tgt.get("dev") if tgt is not None else None,
                      "device": d.get("device", "disk"), "type": d.get("type"),
                      "source": _source(d.find("source")),
                      "format": drv.get("type") if drv is not None else None,
                      "bus": tgt.get("bus") if tgt is not None else None})
    nics = []
    for n in devices.findall("interface"):
        mac, model = n.find("mac"), n.find("model")
        nics.append({"type": n.get("type"), "source": _source(n.find("source")),
                     "mac": mac.get("address") if mac is not None else None,
                     "model": model.get("type") if model is not None else None})
    graphics = []
    for g in devices.findall("graphics"):
        listen = g.get("listen")
        if not listen:
            sub = g.find("listen")
            listen = sub.get("address") if sub is not None else None
        port = _int(g.get("port"))
        graphics.append({"type": g.get("type"), "port": port if port and port > 0 else None,
                         "listen": listen, "autoport": _yes(g.get("autoport"))})
    return {"name": root.findtext("name"), "uuid": root.findtext("uuid"),
            "domain_type": root.get("type"), "id": _int(root.get("id")),
            "vcpus": _int(vcpu.text) if vcpu is not None else None,
            "memory_kib": _to_kib(root.find("memory")),
            "current_memory_kib": _to_kib(root.find("currentMemory")),
            "cpu_pinned": pinned, "cpu_realtime": rt,
            "hugepages": root.find("memoryBacking/hugepages") is not None,
            "machine": os_type.get("machine") if os_type is not None else None,
            "arch": os_type.get("arch") if os_type is not None else None,
            "firmware": firmware, "disks": disks, "nics": nics, "graphics": graphics}


def parse_domstats_raw(text):
    """`virsh domstats --raw` -> {domaine: {"state", "cpu_time_ns",
    "memory_kib", "memory_max_kib", "vcpus", "vcpus_max", "net_rx_bytes",
    "net_tx_bytes", "disk_rd_bytes", "disk_wr_bytes"}} (sommes sur les cartes
    et disques ; None quand le groupe n'a pas été rendu)."""
    raw, cur = {}, None
    for line in (text or "").splitlines():
        if line.startswith("Domain: "):
            name = line[len("Domain: "):].strip()
            if len(name) >= 2 and name[0] == name[-1] == "'":
                name = name[1:-1]
            cur = raw.setdefault(name, {})
        elif cur is not None and "=" in line:
            k, v = line.strip().split("=", 1)
            cur[k] = v
    out = {}
    for name, kv in raw.items():
        def total(pattern):
            vals = [_int(v) for k, v in kv.items() if re.fullmatch(pattern, k)]
            vals = [v for v in vals if v is not None]
            return sum(vals) if vals else None
        code = _int(kv.get("state.state"))
        out[name] = {"state": DOMAIN_STATES.get(code) if code is not None else None,
                     "state_code": code, "cpu_time_ns": _int(kv.get("cpu.time")),
                     "memory_kib": _int(kv.get("balloon.current")),
                     "memory_max_kib": _int(kv.get("balloon.maximum")),
                     "vcpus": _int(kv.get("vcpu.current")),
                     "vcpus_max": _int(kv.get("vcpu.maximum")),
                     "net_rx_bytes": total(r"net\.\d+\.rx\.bytes"),
                     "net_tx_bytes": total(r"net\.\d+\.tx\.bytes"),
                     "disk_rd_bytes": total(r"block\.\d+\.rd\.bytes"),
                     "disk_wr_bytes": total(r"block\.\d+\.wr\.bytes")}
    return out


def parse_pool_list_details(text):
    """`virsh pool-list --all --details` -> [{"name", "state", "autostart",
    "persistent", "capacity", "allocation", "available"}] (octets)."""
    return [{"name": r.get("Name", ""), "state": r.get("State", ""),
             "autostart": _yes(r.get("Autostart")), "persistent": _yes(r.get("Persistent")),
             "capacity": parse_size(r.get("Capacity")),
             "allocation": parse_size(r.get("Allocation")),
             "available": parse_size(r.get("Available"))} for r in _parse_table(text)]


def parse_vol_list_details(text):
    """`virsh vol-list --pool P --details` -> [{"name", "path", "type",
    "capacity", "allocation"}] (octets)."""
    return [{"name": r.get("Name", ""), "path": r.get("Path", ""), "type": r.get("Type", ""),
             "capacity": parse_size(r.get("Capacity")),
             "allocation": parse_size(r.get("Allocation"))} for r in _parse_table(text)]


def parse_net_list(text):
    """`virsh net-list --all` -> [{"name", "state", "active", "autostart", "persistent"}]."""
    return [{"name": r.get("Name", ""), "state": r.get("State", ""),
             "active": r.get("State", "") == "active", "autostart": _yes(r.get("Autostart")),
             "persistent": _yes(r.get("Persistent"))} for r in _parse_table(text)]


def parse_net_xml(text):
    """XML de `virsh net-dumpxml` -> {"name", "uuid", "forward", "bridge",
    "mac", "domain", "ips": [{"family", "address", "prefix", "dhcp_ranges"}]}."""
    root = _safe_xml(text)
    fwd, br, mac, dom = (root.find(t) for t in ("forward", "bridge", "mac", "domain"))
    ips = []
    for ip in root.findall("ip"):
        prefix = _int(ip.get("prefix"))
        if prefix is None and ip.get("netmask"):
            try:
                prefix = ipaddress.ip_network(f"0.0.0.0/{ip.get('netmask')}").prefixlen
            except ValueError:
                prefix = None
        ips.append({"family": ip.get("family", "ipv4"), "address": ip.get("address"),
                    "prefix": prefix,
                    "dhcp_ranges": [{"start": r.get("start"), "end": r.get("end")}
                                    for r in ip.findall("dhcp/range")]})
    return {"name": root.findtext("name"), "uuid": root.findtext("uuid"),
            "forward": fwd.get("mode", "nat") if fwd is not None else "isolated",
            "bridge": br.get("name") if br is not None else None,
            "mac": mac.get("address") if mac is not None else None,
            "domain": dom.get("name") if dom is not None else None, "ips": ips}


# --- lectures composées -------------------------------------------------

DOMSTATS_GROUPS = ("--state", "--cpu-total", "--balloon", "--vcpu", "--interface", "--block")

INVENTORY_COMMANDS = {
    "hostname": ("hostname",),
    "nodeinfo": ("nodeinfo",),
    "nodememstats": ("nodememstats",),
    "version": ("version",),
    "list": ("list", "--all"),
    "autostart": ("list", "--all", "--autostart", "--name"),
    "transient": ("list", "--all", "--transient", "--name"),
    "domstats": ("domstats", "--raw", "--state", "--cpu-total", "--balloon", "--vcpu"),
}
DETAIL_COMMANDS = {
    "pools": ("pool-list", "--all", "--details"),
    "networks": ("net-list", "--all"),
}


def host_inventory(host, details=False):
    """Résumé d'un hôte et de ses VMs en un appel `batch`. Une commande en
    échec laisse son champ vide et s'ajoute à `errors` ; seul un hôte
    injoignable (aucune commande lancée) ou dont toutes les commandes
    échouent (libvirt refusé) lève KvmError. `details` ajoute
    pools (avec leurs volumes, second appel) et réseaux."""
    cmds = dict(INVENTORY_COMMANDS)
    if details:
        cmds.update(DETAIL_COMMANDS)
    out = host.batch(cmds)
    errors = [{"command": k, "kind": v.error.kind, "message": v.error.message}
              for k, v in out.items() if not v.ok]
    if len(errors) == len(out):
        # rien de lu (libvirt refusé, démon arrêté) : l'hôte entier est en échec
        raise next(iter(out.values())).error

    def ok(key):
        v = out.get(key)
        return str(v) if v is not None and v.ok else ""

    node = parse_nodeinfo(ok("nodeinfo")) if ok("nodeinfo") else {}
    mem = parse_nodememstats(ok("nodememstats"))
    ver = parse_version(ok("version"))
    autostart = set(parse_names(ok("autostart")))
    transient = set(parse_names(ok("transient")))
    stats = parse_domstats_raw(ok("domstats"))
    vms = []
    for d in parse_list_all(ok("list")):
        st = stats.get(d["name"], {})
        vms.append({"host": host.name, "name": d["name"], "id": d["id"], "state": d["state"],
                    "persistent": d["name"] not in transient,
                    "autostart": d["name"] in autostart,
                    "vcpus": st.get("vcpus"), "memory_kib": st.get("memory_kib"),
                    "memory_max_kib": st.get("memory_max_kib"),
                    "cpu_time_ns": st.get("cpu_time_ns")})
    inv = {"name": host.name, "hostname": ok("hostname").strip() or None,
           "address": host.decl.get("address"), "labels": dict(host.decl.get("labels") or {}),
           "cpu": node, "memory": {"total_kib": mem.get("total_kib", node.get("memory_kib")),
                                   "free_kib": mem.get("free_kib"),
                                   "available_kib": mem.get("available_kib")},
           "libvirt_version": ver["libvirt"], "qemu_version": ver["qemu"],
           "vms": vms, "errors": errors}
    if details:
        pools = parse_pool_list_details(ok("pools"))
        active = [p["name"] for p in pools if p["state"] == "running"]
        vols = host.batch({p: ("vol-list", "--pool", p, "--details") for p in active}) if active else {}
        for p in pools:
            v = vols.get(p["name"])
            p["volumes"] = parse_vol_list_details(str(v)) if v is not None and v.ok else []
            if v is not None and not v.ok:
                errors.append({"command": f"vol-list {p['name']}", "kind": v.error.kind,
                               "message": v.error.message})
        inv["pools"] = pools
        inv["networks"] = parse_net_list(ok("networks"))
    return inv


def vm_detail(host, name):
    """Détail d'une VM : définition (dumpxml), état et persistance
    (dominfo), statistiques (domstats). KvmError `notfound` si elle n'existe
    pas ; une statistique illisible laisse `stats` vide."""
    out = host.batch({"xml": ("dumpxml", "--domain", name),
                      "info": ("dominfo", "--domain", name),
                      "stats": ("domstats", "--raw", *DOMSTATS_GROUPS, "--domain", name)})
    for key in ("xml", "info"):
        if not out[key].ok:
            raise out[key].error
    vm = parse_domain_xml(str(out["xml"]))
    info = parse_dominfo(str(out["info"]))
    stats = parse_domstats_raw(str(out["stats"])) if out["stats"].ok else {}
    vm.update({"host": host.name, "state": info["state"], "persistent": info["persistent"],
               "autostart": info["autostart"], "managed_save": info["managed_save"],
               "stats": stats.get(vm["name"]) or next(iter(stats.values()), {})})
    return vm
