#!/usr/bin/env python3
"""harvester-kvm : les hôtes KVM/libvirt de la console (v1.96.0).

`virsh` est lancé SUR l'hôte par SSH (bin/lib/kvm_host.py) ; les hôtes sont
déclarés dans le répertoire d'état (bin/lib/kvm_decl.py). Ce script est le
seul chemin d'écriture : la console le lance en action suivie.

  harvester-kvm.py host scan --address A [--port P]
  harvester-kvm.py host add --name N --address A [--port P] --user U --ssh-key FILE
        --fingerprint SHA256:... [--uri URI] [--label k=v ...] [--keep-on-failure]
  harvester-kvm.py host test N
  harvester-kvm.py host list
  harvester-kvm.py host remove N
  harvester-kvm.py inventory [--host N] [--details]
  harvester-kvm.py vm list [--host N]
  harvester-kvm.py vm show --host N --name D
  harvester-kvm.py vm start|shutdown|reboot|destroy|suspend|resume --host N --name D [--timeout S]

Options communes : --json (sortie JSON sur stdout), --state-dir (défaut
HARVESTER_OPS_STATE_DIR). Progression sur stderr en
STEP_EVENT|étape|statut|message.

`host add` ne fait jamais confiance à la première connexion : l'empreinte
donnée doit être celle d'une clé relevée, et seule cette ligne est épinglée.
Si le test qui suit échoue, rien n'est gardé (sauf --keep-on-failure).

Les gestes suivent l'état réel (`virsh domstate`, toutes les 2 s) jusqu'à
l'état attendu ; à l'échéance de --timeout (120 s pour shutdown et reboot,
30 s sinon), échec. Un geste manifestement impossible dans l'état courant
(reprendre une VM qui tourne) est refusé sans appeler libvirt ; pour le
reste, libvirt décide.

Codes de sortie : 0 fait, 1 échec (hôte, libvirt, délai), 2 erreur d'usage
ou demande refusée avant l'hôte.
"""

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import cluster_decl  # noqa: E402
import kvm_decl  # noqa: E402
import kvm_host as kh  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

# points d'injection des tests : exécution, horloge, attente
RUNNER = subprocess.run
CLOCK = time.monotonic
SLEEP = time.sleep

POLL_INTERVAL = 2
MAX_PARALLEL = 8
MAX_KEY_BYTES = 64 * 1024
DEFAULT_TIMEOUTS = {"shutdown": 120, "reboot": 120}
DEFAULT_TIMEOUT = 30

# geste -> (états acceptés à l'arrivée, états de départ refusés et pourquoi)
GESTURES = {
    "start": ({"running"}, {"running": "is already running",
                            "idle": "is already running",
                            "paused": "is paused: resume it instead",
                            "in shutdown": "is shutting down: wait for it to stop"}),
    "shutdown": ({"shut off"}, {"shut off": "is already shut off",
                                "paused": "is paused and cannot react to a shutdown request: "
                                          "resume it first, or use destroy"}),
    "reboot": ({"running"}, {"shut off": "is shut off: use start",
                             "paused": "is paused: resume it first"}),
    "destroy": ({"shut off"}, {"shut off": "is already shut off"}),
    "suspend": ({"paused"}, {"shut off": "is shut off",
                             "paused": "is already paused"}),
    "resume": ({"running"}, {"running": "is not paused (it is running)",
                             "idle": "is not paused (it is running)",
                             "shut off": "is shut off: use start"}),
}

# adresses d'écoute qui ouvrent l'affichage à tout le réseau
ALL_INTERFACES = {"0.0.0.0", "::", "[::]", "*"}


class UsageError(ValueError):
    """Demande refusée avant tout appel à l'hôte (code 2)."""


class GestureError(Exception):
    """Échec déjà dit en STEP_EVENT (code 1)."""


def step(sid, status, msg=""):
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def emit(args, data, text):
    """Le résultat : JSON avec --json, texte lisible sinon."""
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif text:
        print(text)


def public_decl(decl):
    """Une déclaration sans chemin de clé ni d'empreinte (jamais montrés)."""
    d = kvm_decl.normalize(decl)
    return {k: d.get(k) for k in ("name", "address", "port", "user", "uri", "labels")}


def err_info(e):
    return {"kind": e.kind, "message": e.message}


def state_of(args):
    return Path(args.state_dir) if args.state_dir else cluster_decl.state_dir()


def load_host(state, name):
    """VirshHost d'un hôte déclaré ; UsageError s'il n'existe pas."""
    if not isinstance(name, str) or not kvm_decl.NAME_RE.match(name):
        raise UsageError("invalid host name")
    try:
        decl = kvm_decl.load(state, name)
    except FileNotFoundError:
        raise UsageError(f"host {name} is not declared") from None
    except kvm_decl.DeclError as e:
        raise UsageError(str(e)) from None
    return kh.VirshHost(decl, state, runner=RUNNER)


def check_vm_name(name):
    # libvirt accepte presque tout ; on refuse le vide et les caractères de
    # contrôle (le nom part toujours après --domain, jamais comme option)
    if not isinstance(name, str) or not name or len(name) > 255 or any(ord(c) < 32 for c in name):
        raise UsageError("invalid domain name")
    return name


def close_master(decl, state):
    """Ferme une connexion ControlMaster restée ouverte vers cet hôte. Le
    socket ne dépend que de l'adresse, du port et de l'utilisateur : sans
    cela, un test juste après un retrait passerait par l'ancienne connexion,
    sans vérifier ni la clé ni l'empreinte."""
    try:
        argv = kh.ssh_argv(decl, state, [])[:-1]
    except (kh.KvmError, kvm_decl.DeclError):
        return
    argv[1:1] = ["-O", "exit"]
    try:
        RUNNER(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------------------
# host scan / add / test / list / remove
# ---------------------------------------------------------------------------

def _check_address(address, port):
    if not isinstance(address, str) or not kvm_decl.ADDRESS_RE.match(address):
        raise UsageError("invalid address")
    if not 1 <= port <= 65535:
        raise UsageError("port must be between 1 and 65535")


def cmd_host_scan(args):
    _check_address(args.address, args.port)
    step("scan", "running", f"reading the host keys of {args.address}:{args.port}")
    try:
        keys = kvm_decl.scan_host_key(args.address, args.port, runner=RUNNER)
    except kvm_decl.DeclError as e:
        step("scan", "error", str(e))
        raise GestureError(str(e)) from None
    step("scan", "done", f"{len(keys)} host key(s)")
    keys = [{"type": k["type"], "fingerprint": k["fingerprint"], "line": k["line"]} for k in keys]
    emit(args, {"address": args.address, "port": args.port, "keys": keys},
         "\n".join(f"{k['type']}  {k['fingerprint']}" for k in keys))
    return EXIT_OK


def parse_labels(items):
    labels = {}
    for item in items or []:
        k, sep, v = item.partition("=")
        if not sep or not k:
            raise UsageError(f"invalid label {item!r} (expected key=value)")
        labels[k] = v
    return labels


def read_private_key(path):
    try:
        p = Path(path)
        if not p.is_file():
            raise UsageError("the SSH key file does not exist")
        if p.stat().st_size > MAX_KEY_BYTES:
            raise UsageError("the SSH key file is too large")
        data = p.read_text()
    except (OSError, UnicodeDecodeError):
        raise UsageError("the SSH key file cannot be read") from None
    if "-----BEGIN" not in data or "PRIVATE KEY-----" not in data:
        raise UsageError("the SSH key file is not a private key")
    return data


def cmd_host_add(args):
    step("validate", "running", "checking the request")
    labels = parse_labels(args.label)
    _check_address(args.address, args.port)
    if not kvm_decl.FINGERPRINT_RE.fullmatch(args.fingerprint or ""):
        raise UsageError("invalid fingerprint (expected SHA256:...)")
    name = args.name
    decl = {"name": name, "address": args.address, "port": args.port, "user": args.user,
            "ssh_key": kvm_decl.key_rel(name), "known_hosts": kvm_decl.known_hosts_rel(name),
            "uri": args.uri, "labels": labels}
    why = kvm_decl.check_decl(decl)
    if why:
        raise UsageError(why)
    state = state_of(args)
    if kvm_decl.decl_path(state, name).exists():
        raise UsageError(f"host {name} is already declared (remove it first)")
    key = read_private_key(args.ssh_key)
    step("validate", "done", f"{args.user}@{args.address}:{args.port}")

    step("scan", "running", f"reading the host keys of {args.address}:{args.port}")
    try:
        keys = kvm_decl.scan_host_key(args.address, args.port, runner=RUNNER)
    except kvm_decl.DeclError as e:
        step("scan", "error", str(e))
        raise GestureError(str(e)) from None
    match = [k for k in keys if k["fingerprint"] == args.fingerprint]
    if not match:
        msg = (f"the fingerprint {args.fingerprint} matches none of the host keys "
               f"({', '.join(k['type'] + ' ' + k['fingerprint'] for k in keys)}): nothing saved")
        step("scan", "error", msg)
        raise GestureError(msg)
    step("scan", "done", f"pinning the {match[0]['type']} key {args.fingerprint}")

    step("save", "running", "saving the key, the pinned fingerprint and the declaration")
    try:
        kvm_decl.write_key(state, name, key)
        kvm_decl.write_known_hosts(state, name, [match[0]["line"]])
        kvm_decl.write(state, decl)
    except (OSError, kvm_decl.DeclError) as e:
        kvm_decl.remove(state, name)
        step("save", "error", f"cannot save the host: {e}")
        raise GestureError(f"cannot save the host: {e}") from None
    step("save", "done", f"host {name} saved")

    stored = kvm_decl.load(state, name)
    close_master(stored, state)
    host = kh.VirshHost(stored, state, runner=RUNNER)
    try:
        checks = run_checks(host)
    except BaseException:
        # rien de laissé à moitié, même sur une erreur imprévue
        if not args.keep_on_failure:
            close_master(stored, state)
            kvm_decl.remove(state, name)
        raise
    ok = not any(c["status"] == "error" for c in checks)
    kept = ok or args.keep_on_failure
    if not kept:
        close_master(stored, state)
        kvm_decl.remove(state, name)
        step("save", "done", f"connection test failed: host {name} removed again")
    elif not ok:
        step("save", "warn", f"connection test failed: host {name} kept as asked")
    emit(args, {"host": public_decl(stored), "fingerprint": args.fingerprint,
                "ok": ok, "kept": kept, "checks": checks},
         checks_text(name, checks) + ("" if kept else f"\nhost {name} not saved"))
    return EXIT_OK if ok else EXIT_FAIL


def _check(cid, status, message, **extra):
    out = {"check": cid, "status": status, "message": message}
    out.update(extra)
    step(f"check-{cid}", status, message)
    return out


def _first_line(text):
    return next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")


def graphics_exposure(xml_text):
    """Affichages d'une VM en marche, lus dans `dumpxml --security-info` :
    [{"type", "listen", "port", "password"}]. Seule la PRÉSENCE d'un mot de
    passe est rendue, jamais sa valeur."""
    root = kh._safe_xml(xml_text)
    out = []
    for g in root.findall("devices/graphics"):
        if g.get("type") not in ("vnc", "spice"):
            continue
        listen = g.get("listen")
        sub = g.find("listen")
        if not listen and sub is not None:
            listen = sub.get("address") or (f"socket:{sub.get('socket')}"
                                            if sub.get("type") == "socket" else None)
        port = kh._int(g.get("port"))
        out.append({"type": g.get("type"), "listen": listen,
                    "port": port if port and port > 0 else None,
                    "password": bool(g.get("passwd"))})
    return out


def check_vnc(host):
    """Écoute des affichages des VMs actives. Une VM qui écoute sur toutes
    les interfaces sans mot de passe est un avertissement de sécurité."""
    try:
        names = kh.parse_names(host.virsh("list", "--name"))
    except kh.KvmError as e:
        return _check("vnc", "warn", f"cannot list the running domains: {e.message}", kind=e.kind)
    if not names:
        return _check("vnc", "ok", "no running domain", domains=[])
    try:
        out = host.batch({n: ("dumpxml", "--security-info", "--domain", n) for n in names})
    except kh.KvmError as e:
        return _check("vnc", "warn", f"cannot read the displays: {e.message}", kind=e.kind)
    domains, exposed, unread = [], [], []
    for n in names:
        res = out.get(n)
        if res is None or not res.ok:
            unread.append(n)
            continue
        try:
            displays = graphics_exposure(str(res))
        except Exception:                      # noqa: BLE001 : XML illisible
            unread.append(n)
            continue
        for d in displays:
            d["domain"] = n
            domains.append(d)
            if d["listen"] in ALL_INTERFACES and not d["password"]:
                exposed.append(f"{n} ({d['type']} on {d['listen']}:{d['port'] or 'auto'})")
    if exposed:
        msg = ("security: display reachable from the whole network without a password: "
               + ", ".join(exposed))
        return _check("vnc", "warn", msg, domains=domains, exposed=len(exposed))
    msg = f"{len(domains)} display(s) on {len(names)} running domain(s), none exposed without a password"
    if unread:
        return _check("vnc", "warn", msg + f"; not read: {', '.join(unread)}", domains=domains)
    return _check("vnc", "ok", msg, domains=domains)


CHECK_IDS = ("ssh", "virsh", "uri", "qemu-img", "vnc")


def run_checks(host):
    """Les points du test de connexion, un résultat chacun. Un point bloquant
    en échec (ssh, virsh, URI) saute les suivants."""
    checks = []

    def skip_rest(after):
        for cid in CHECK_IDS[CHECK_IDS.index(after) + 1:]:
            checks.append(_check(cid, "skipped", f"skipped: {after} failed"))
        return checks

    try:
        host.run(["true"])
        checks.append(_check("ssh", "ok", f"connected as {host.decl['user']}@{host.decl['address']}"))
    except kh.KvmError as e:
        checks.append(_check("ssh", "error", e.message, kind=e.kind))
        return skip_rest("ssh")
    try:
        out = host.run(["env", "LC_ALL=C", "virsh", "--version"])
        checks.append(_check("virsh", "ok", f"virsh {_first_line(out)}", version=_first_line(out)))
    except kh.KvmError as e:
        checks.append(_check("virsh", "error", e.message, kind=e.kind))
        return skip_rest("virsh")
    uri = host.decl.get("uri")
    try:
        ver = kh.parse_version(host.virsh("version"))
        checks.append(_check("uri", "ok", f"{uri}: libvirt {ver['libvirt']}, "
                             f"{ver['hypervisor'] or 'hypervisor'} {ver['qemu'] or ''}".strip(),
                             libvirt=ver["libvirt"], qemu=ver["qemu"]))
    except kh.KvmError as e:
        hint = (f" (give {host.decl['user']} access to {uri}, e.g. the libvirt group)"
                if e.kind == "denied" else "")
        checks.append(_check("uri", "error", e.message + hint, kind=e.kind))
        return skip_rest("uri")
    try:
        out = host.run(["qemu-img", "--version"])
        checks.append(_check("qemu-img", "ok", _first_line(out)))
    except kh.KvmError as e:
        # absent : l'inventaire et les gestes marchent, les disques non
        checks.append(_check("qemu-img", "warn", e.message, kind=e.kind))
    checks.append(check_vnc(host))
    return checks


def checks_text(name, checks):
    return "\n".join([f"host {name}:"] + [f"  {c['check']:<9} {c['status']:<8} {c['message']}"
                                         for c in checks])


def cmd_host_test(args):
    host = load_host(state_of(args), args.name)
    checks = run_checks(host)
    ok = not any(c["status"] == "error" for c in checks)
    emit(args, {"host": host.name, "ok": ok, "checks": checks}, checks_text(host.name, checks))
    return EXIT_OK if ok else EXIT_FAIL


def cmd_host_list(args):
    warnings = []
    hosts = [public_decl(d) for d in kvm_decl.load_all(state_of(args), warn=warnings.append)]
    lines = [f"{h['name']:<20} {h['user']}@{h['address']}:{h['port']}  {h['uri']}" for h in hosts]
    emit(args, {"hosts": hosts, "warnings": warnings},
         "\n".join(lines + warnings) or "(no KVM host declared)")
    return EXIT_OK


def cmd_host_remove(args):
    state = state_of(args)
    try:
        decl = kvm_decl.load(state, args.name)
    except FileNotFoundError:
        raise UsageError(f"host {args.name} is not declared") from None
    except kvm_decl.DeclError:
        decl = None                            # déclaration abîmée : on la retire quand même
    if decl:
        close_master(decl, state)
    step("remove", "running", f"removing host {args.name}")
    kvm_decl.remove(state, args.name)
    step("remove", "done", f"host {args.name} removed, with its key and pinned fingerprint")
    emit(args, {"host": args.name, "removed": True}, f"host {args.name} removed")
    return EXIT_OK


# ---------------------------------------------------------------------------
# inventory / vm list / vm show
# ---------------------------------------------------------------------------

def targets(args):
    """[VirshHost] : l'hôte demandé, ou tous les hôtes déclarés."""
    state = state_of(args)
    if args.host:
        return [load_host(state, args.host)]
    return [kh.VirshHost(d, state, runner=RUNNER)
            for d in kvm_decl.load_all(state, warn=lambda m: step("hosts", "warn", m))]


def read_all(hosts, details=False):
    """Inventaire de chaque hôte, en parallèle ; un hôte en échec rend son
    erreur sans gêner les autres."""
    def one(host):
        try:
            return kh.host_inventory(host, details=details)
        except kh.KvmError as e:
            return {"name": host.name, "address": host.decl.get("address"), "error": err_info(e)}
        except Exception as e:                 # noqa: BLE001 : jamais fatal pour les autres
            return {"name": host.name, "address": host.decl.get("address"),
                    "error": {"kind": "failed", "message": f"{host.name}: failed: {e}"}}
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(hosts))) as pool:
        return list(pool.map(one, hosts))


def _gib(kib):
    return f"{kib / 1024 / 1024:.1f}G" if isinstance(kib, (int, float)) else "-"


def cmd_inventory(args):
    hosts = targets(args)
    invs = read_all(hosts, details=args.details)
    lines = []
    for inv in invs:
        if "error" in inv:
            lines.append(f"{inv['name']}: {inv['error']['kind']} ({inv['error']['message']})")
            continue
        cpu, mem = inv.get("cpu") or {}, inv.get("memory") or {}
        running = sum(1 for v in inv["vms"] if v["state"] == "running")
        lines.append(f"{inv['name']}: {cpu.get('cpus', '-')} CPU, {_gib(mem.get('total_kib'))} "
                     f"({_gib(mem.get('available_kib'))} available), libvirt {inv['libvirt_version']}, "
                     f"QEMU {inv['qemu_version']}, {len(inv['vms'])} VM(s), {running} running")
        for e in inv.get("errors") or []:
            lines.append(f"  partly read: {e['command']}: {e['message']}")
    emit(args, {"hosts": invs}, "\n".join(lines) or "(no KVM host declared)")
    if args.host and invs and "error" in invs[0]:
        return EXIT_FAIL
    return EXIT_OK


def cmd_vm_list(args):
    invs = read_all(targets(args))
    vms, hosts = [], []
    for inv in invs:
        if "error" in inv:
            hosts.append({"name": inv["name"], "state": inv["error"]["kind"],
                          "error": inv["error"]["message"]})
            continue
        hosts.append({"name": inv["name"], "state": "ok", "count": len(inv["vms"])})
        vms.extend(inv["vms"])
    vms.sort(key=lambda v: (v["name"], v["host"]))
    head = ("HOST", "NAME", "STATE", "VCPUS", "MEMORY")
    rows = [head] + [(v["host"], v["name"], v["state"], str(v["vcpus"] or "-"), _gib(v["memory_kib"]))
                     for v in vms]
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]
    lines += [f"host {h['name']}: {h['state']} ({h['error']})" for h in hosts if h["state"] != "ok"]
    emit(args, {"vms": vms, "hosts": hosts}, "\n".join(lines))
    if args.host and hosts and hosts[0]["state"] != "ok":
        return EXIT_FAIL
    return EXIT_OK


def cmd_vm_show(args):
    host = load_host(state_of(args), args.host)
    name = check_vm_name(args.name)
    try:
        vm = kh.vm_detail(host, name)
    except kh.KvmError as e:
        step("show", "error", e.message)
        raise GestureError(e.message) from None
    text = "\n".join([f"{vm['name']} on {vm['host']}: {vm['state']}",
                      f"  vCPU {vm['vcpus']}, memory {_gib(vm['memory_kib'])}, machine {vm['machine']}, "
                      f"firmware {vm['firmware']}"]
                     + [f"  disk {d['target']}: {d['source']} ({d['format']}, {d['bus']})" for d in vm["disks"]]
                     + [f"  nic {n['mac']}: {n['type']} {n['source']} ({n['model']})" for n in vm["nics"]])
    emit(args, vm, text)
    return EXIT_OK


# ---------------------------------------------------------------------------
# gestes de cycle de vie
# ---------------------------------------------------------------------------

def domstate(host, name):
    """État courant d'une VM (`virsh domstate`) ; KvmError sinon."""
    return _first_line(host.virsh("domstate", "--domain", name))


class StateTimeout(Exception):
    def __init__(self, state, elapsed):
        super().__init__(state)
        self.state, self.elapsed = state, elapsed


def follow_state(host, name, accept, timeout, clock=None, sleep=None, interval=POLL_INTERVAL,
                 wait_first=False):
    """Interroge l'état toutes les `interval` secondes jusqu'à un état de
    `accept` ; rend (état, secondes écoulées). StateTimeout à l'échéance.
    Chaque changement d'état est dit en STEP_EVENT."""
    clock, sleep = clock or CLOCK, sleep or SLEEP
    start, last = clock(), None
    if wait_first:
        sleep(interval)
    while True:
        state = domstate(host, name)
        elapsed = clock() - start
        if state != last:
            step("wait", "running", f"{name}: {state}")
            last = state
        if state in accept:
            return state, elapsed
        if elapsed >= timeout:
            raise StateTimeout(state, elapsed)
        sleep(interval)


def gesture(args, action):
    host = load_host(state_of(args), args.host)
    name = check_vm_name(args.name)
    timeout = args.timeout if args.timeout is not None else DEFAULT_TIMEOUTS.get(action, DEFAULT_TIMEOUT)
    if timeout <= 0:
        raise UsageError("--timeout must be positive")
    accept, refused = GESTURES[action]
    result = {"host": host.name, "name": name, "action": action, "ok": False}

    def fail(sid, msg, **extra):
        step(sid, "error", msg)
        result.update(extra, error=msg)
        emit(args, result, None)
        raise GestureError(msg)

    step("state", "running", f"reading the state of {name}")
    try:
        before = domstate(host, name)
    except kh.KvmError as e:
        fail("state", e.message, kind=e.kind)
    result["from"] = before
    step("state", "done", f"{name}: {before}")
    if before in refused:
        fail("validate", f"cannot {action} {name}: it {refused[before]}", kind="invalid")

    step("action", "running", f"virsh {action} {name}")
    try:
        host.virsh(action, "--domain", name)
    except kh.KvmError as e:
        fail("action", e.message, kind=e.kind)
    step("action", "done", f"{action} requested")

    try:
        state, elapsed = follow_state(host, name, accept, timeout, wait_first=(action == "reboot"))
    except kh.KvmError as e:
        fail("wait", e.message, kind=e.kind)
    except StateTimeout as t:
        msg = f"{name} is still {t.state} after {timeout}s"
        if action == "shutdown":
            msg += ": the guest did not shut down (no ACPI support or busy); use destroy to force it off"
        elif action == "reboot":
            msg += ": it did not come back to running"
        else:
            msg += f": expected {' or '.join(sorted(accept))}"
        fail("wait", msg, kind="timeout", state=t.state)
    step("wait", "done", f"{name}: {state} after {elapsed:.0f}s")
    result.update(ok=True, state=state, elapsed_s=round(elapsed, 1))
    emit(args, result, f"{name}: {state}")
    return EXIT_OK


# ---------------------------------------------------------------------------

def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="JSON output on stdout")
    common.add_argument("--state-dir", help="console state directory (default HARVESTER_OPS_STATE_DIR)")

    ap = argparse.ArgumentParser(prog="harvester-kvm", description="KVM/libvirt hosts driven by virsh over SSH.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    hp = sub.add_parser("host", help="declared KVM hosts").add_subparsers(dest="hcmd", required=True)
    sp = hp.add_parser("scan", parents=[common], help="host key fingerprints of a host")
    sp.add_argument("--address", required=True)
    sp.add_argument("--port", type=int, default=kvm_decl.DEFAULT_PORT)
    sp.set_defaults(fn=cmd_host_scan)
    sp = hp.add_parser("add", parents=[common], help="declare a host after checking its fingerprint")
    sp.add_argument("--name", required=True)
    sp.add_argument("--address", required=True)
    sp.add_argument("--port", type=int, default=kvm_decl.DEFAULT_PORT)
    sp.add_argument("--user", required=True)
    sp.add_argument("--ssh-key", required=True, help="private key file (copied into the state directory)")
    sp.add_argument("--fingerprint", required=True, help="SHA256:... of the host key to pin")
    sp.add_argument("--uri", default=kvm_decl.DEFAULT_URI)
    sp.add_argument("--label", action="append", metavar="KEY=VALUE")
    sp.add_argument("--keep-on-failure", action="store_true",
                    help="keep the host even if the connection test fails")
    sp.set_defaults(fn=cmd_host_add)
    sp = hp.add_parser("test", parents=[common], help="connection test, one result per check")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_host_test)
    sp = hp.add_parser("list", parents=[common], help="declared hosts")
    sp.set_defaults(fn=cmd_host_list)
    sp = hp.add_parser("remove", parents=[common], help="remove a host, its key and fingerprint")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_host_remove)

    sp = sub.add_parser("inventory", parents=[common], help="host summary and VMs (all hosts in parallel)")
    sp.add_argument("--host")
    sp.add_argument("--details", action="store_true", help="also storage pools, volumes and networks")
    sp.set_defaults(fn=cmd_inventory)

    vp = sub.add_parser("vm", help="virtual machines").add_subparsers(dest="vcmd", required=True)
    sp = vp.add_parser("list", parents=[common], help="VMs of one or all hosts")
    sp.add_argument("--host")
    sp.set_defaults(fn=cmd_vm_list)
    sp = vp.add_parser("show", parents=[common], help="definition and state of one VM")
    sp.add_argument("--host", required=True)
    sp.add_argument("--name", required=True)
    sp.set_defaults(fn=cmd_vm_show)
    for action in GESTURES:
        sp = vp.add_parser(action, parents=[common], help=f"{action} a VM and follow its state")
        sp.add_argument("--host", required=True)
        sp.add_argument("--name", required=True)
        sp.add_argument("--timeout", type=int,
                        help=f"seconds (default {DEFAULT_TIMEOUTS.get(action, DEFAULT_TIMEOUT)})")
        sp.set_defaults(fn=lambda a, _act=action: gesture(a, _act))

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (UsageError, kvm_decl.DeclError) as e:
        step("validate", "error", str(e))
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except GestureError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAIL
    except kh.KvmError as e:
        step("host", "error", e.message)
        print(f"error: {e.message}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
