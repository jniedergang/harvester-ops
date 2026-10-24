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
  harvester-kvm.py vm display --host N --name D
  harvester-kvm.py vm delete --host N --name D [--remove-storage]
        [--acknowledge-unreadable=PATH ...] [--dry-run] [--timeout S]
  harvester-kvm.py snapshot list --host N --name D
  harvester-kvm.py snapshot create --host N --name D --snapshot S [--description T] [--timeout S]
  harvester-kvm.py snapshot revert|delete --host N --name D --snapshot S [--timeout S]
  harvester-kvm.py clone --host N --name D --new-name C [--dry-run] [--timeout S]

Options communes : --json (sortie JSON sur stdout), --state-dir (défaut
HARVESTER_OPS_STATE_DIR). Progression sur stderr en
STEP_EVENT|étape|statut|message : le message peut contenir `|`, le
consommateur découpe donc avec maxsplit=3 (`line.split("|", 3)`) ; les sauts
de ligne du message sont remplacés par des espaces.

Avec --json, CHAQUE sortie écrit exactement un objet JSON sur stdout : le
résultat, ou en cas d'échec {"ok": false, "error": "<message>", "kind":
"<kind>"} (kind d'une KvmError, ou `usage`, `busy`, `invalid`, `hostkey`,
`failed`). Une erreur imprévue rend un message générique, sans trace ni
chemin.

Les valeurs se passent sous la forme `--option=valeur` (c'est ce que
construit la console) : une valeur qui commence par un tiret reste alors
une valeur, pour toutes les options.

`host add` ne fait jamais confiance à la première connexion : l'empreinte
donnée doit être celle d'une clé relevée, et seule cette ligne est épinglée.
Si le test qui suit échoue, rien n'est gardé (sauf --keep-on-failure).

Les gestes suivent l'état réel (`virsh domstate`, toutes les 2 s) jusqu'à
l'état attendu ; à l'échéance de --timeout (120 s pour shutdown et reboot,
30 s sinon), échec qui propose l'arrêt forcé quand il existe. Un hôte
injoignable un instant pendant le suivi est réinterrogé jusqu'à l'échéance.
Une VM transitoire disparaît quand elle s'arrête : après shutdown ou
destroy, « domaine introuvable » vaut réussite (`"gone": true`). Un reboot
ACPI laisse la VM `running` aux yeux de libvirt : la réussite dit « reboot
demandé, la VM tourne » (`"confirmed": false`), sans prétendre que l'invité
a redémarré. Un geste manifestement impossible dans l'état courant
(reprendre une VM qui tourne) est refusé sans appeler libvirt ; pour le
reste, libvirt décide.

Instantanés, clonage et suppression (v1.97.0, bin/lib/kvm_ops.py) : chaque
commande émet `plan`, puis ses étapes, relit l'état réel à la fin et rend
ce qui a été fait (`deleted`, `kept` avec leur raison, `leftovers`). Une
demande refusée par l'état de la VM (en marche, disque raw...) sort en 1,
kind `refused`, avec la liste `refusals`. Deux opérations sur la même VM
(clonage : la source et le nom du clone) ne tournent jamais en même temps :
la seconde sort en 2, kind `busy`. `vm display` dit où joindre l'affichage
VNC et la commande `ssh -L` à lancer soi-même ; le mot de passe VNC n'est
jamais lu par ce script, seule sa présence est dite.

Codes de sortie : 0 fait, 1 échec (hôte, libvirt, délai), 2 erreur d'usage
ou demande refusée avant l'hôte, 3 annulé (SIGTERM ou SIGHUP : ce qui était
commencé est défait, `host add` ne laisse ni déclaration ni clé ; un clonage
retire ce qu'il a créé et nomme ce qui reste).
"""

import argparse
import contextlib
import errno
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import cluster_decl  # noqa: E402
import kvm_decl  # noqa: E402
import kvm_host as kh  # noqa: E402
import kvm_ops as ko  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
# code que la console lit comme « annulé » (même valeur que les autres outils)
EXIT_CANCELLED = 3
CANCEL_SIGNALS = (signal.SIGTERM, signal.SIGHUP)

# points d'injection des tests : exécution, horloge, attente
RUNNER = subprocess.run
CLOCK = time.monotonic
SLEEP = time.sleep

POLL_INTERVAL = 2
MAX_PARALLEL = 8
MAX_KEY_BYTES = 64 * 1024
DEFAULT_TIMEOUTS = {"shutdown": 120, "reboot": 120}
DEFAULT_TIMEOUT = 30
# suivi de l'état : sortes d'erreur réessayées jusqu'à l'échéance, et délai
# minimal d'un appel (un appel ne doit pas faire dépasser l'échéance de 30 s)
RETRY_KINDS = {"unreachable", "timeout"}
MIN_CALL_TIMEOUT = 5
KEY_CHECK_TIMEOUT = 10
# OpenSSH >= 9.8 (PerSourcePenalties) pénalise les connexions sans
# authentification, donc celles du relevé des clés : juste après, l'hôte
# coupe les connexions de la console ~15 s (vu en réel, OpenSSH 10.2). Le
# test de `host add` réessaie une fois la connexion après ce délai.
SSH_RETRY_AFTER_SCAN = 20

# geste -> (états acceptés à l'arrivée, états de départ refusés et pourquoi)
GESTURES = {
    "start": ({"running"}, {"running": "is already running",
                            "idle": "is already running",
                            "paused": "is paused: resume it instead",
                            "pmsuspended": "is suspended by the guest: resume it instead",
                            # on_crash=preserve : le domaine reste actif
                            "crashed": "has crashed and is still active (kept for inspection): "
                                       "use destroy, then start",
                            "in shutdown": "is shutting down: wait for it to stop"}),
    "shutdown": ({"shut off"}, {"shut off": "is already shut off",
                                "paused": "is paused and cannot react to a shutdown request: "
                                          "resume it first, or use destroy"}),
    "reboot": ({"running"}, {"shut off": "is shut off: use start",
                             "paused": "is paused: resume it first"}),
    "destroy": ({"shut off"}, {"shut off": "is already shut off"}),
    "suspend": ({"paused"}, {"shut off": "is shut off",
                             "paused": "is already paused",
                             "pmsuspended": "is already suspended by the guest"}),
    # seules une VM en pause (paused) ou suspendue par l'invité (pmsuspended)
    # se reprennent ; l'interface lit cette table, elle n'a pas de règle à elle
    "resume": ({"running"}, {"running": "is not paused (it is running)",
                             "idle": "is not paused (it is running)",
                             "shut off": "is shut off: use start",
                             "crashed": "has crashed: use destroy, then start",
                             "in shutdown": "is shutting down: wait for it to stop",
                             "blocked": "is blocked on a resource, not paused"}),
}

# commande virsh d'un geste selon l'état de départ (défaut : le nom du geste)
# ; une VM suspendue par l'invité (pmsuspended) se réveille par dompmwakeup
ACTION_COMMANDS = {("resume", "pmsuspended"): "dompmwakeup"}

# à l'échéance, le geste forcé à proposer (rien pour les autres)
FORCED = {"shutdown": ["destroy"], "reboot": ["destroy", "start"]}

# délais par défaut (s) des opérations de la 1.97.0
SNAPSHOT_TIMEOUTS = {"create": 600, "revert": 300, "delete": 300}
CLONE_TIMEOUT = 3600
DELETE_TIMEOUT = 300
# participe passé des messages d'annulation des instantanés
SNAPSHOT_DONE = {"create": "taken", "revert": "reverted", "delete": "deleted"}


class Cancelled(BaseException):
    """SIGTERM ou SIGHUP (bouton « Annuler » de la console) : levée dans le
    fil principal pour que les `finally` et le retour arrière de `host add`
    s'exécutent. BaseException : aucun `except Exception` ne l'avale."""


def _on_cancel_signal(signum, _frame):
    # un seul coup : un second signal ne doit pas interrompre le retour arrière
    for sig in CANCEL_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    raise Cancelled(signal.Signals(signum).name)


@contextlib.contextmanager
def cancel_on_signals():
    """Installe le gestionnaire le temps de la commande (fil principal
    seulement : la console charge ce module et n'appelle pas main)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    saved = {sig: signal.signal(sig, _on_cancel_signal) for sig in CANCEL_SIGNALS}
    try:
        yield
    finally:
        for sig, h in saved.items():
            signal.signal(sig, h)


class UsageError(ValueError):
    """Demande refusée avant tout appel à l'hôte (code 2)."""

    def __init__(self, message, kind="usage"):
        super().__init__(message)
        self.kind = kind


class Failure(Exception):
    """Échec déjà dit en STEP_EVENT (code 1). `data` complète l'objet JSON
    d'échec (champs du geste, par exemple)."""

    def __init__(self, message, kind="failed", data=None):
        super().__init__(message)
        self.message, self.kind, self.data = message, kind, data or {}


class Parser(argparse.ArgumentParser):
    """argparse qui lève UsageError au lieu de quitter : l'erreur d'usage
    passe alors par le même chemin (et le même JSON) que les autres."""

    def error(self, message):
        raise UsageError(message)


def step(sid, status, msg=""):
    # message sur une ligne ; il peut contenir `|` (découpe maxsplit=3)
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def print_json(data):
    print(json.dumps(data, indent=2, sort_keys=True))


def emit(args, data, text):
    """Le résultat : JSON avec --json, texte lisible sinon."""
    if args.json:
        print_json(data)
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


@contextlib.contextmanager
def host_lock(state):
    """Verrou des ajouts et retraits d'hôtes (`<état>/kvm.d/.lock`) : deux
    ajouts du même nom en même temps écriraient les mêmes fichiers."""
    d = kvm_decl.decl_dir(state)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UsageError("another host add or remove is in progress: try again",
                             kind="busy") from None
        yield
    finally:
        os.close(fd)                           # libère le verrou


def close_master(decl, state):
    """Ferme la connexion ControlMaster restée ouverte vers cet hôte
    (kvm_host.close_master) : sans cela, un test juste après un retrait ou
    un nouvel ajout passerait par l'ancienne connexion, sans vérifier ni la
    clé ni l'empreinte."""
    kh.close_master(decl, state, runner=RUNNER)


# ---------------------------------------------------------------------------
# host scan / add / test / list / remove
# ---------------------------------------------------------------------------

def _check_address(address, port):
    if not isinstance(address, str) or not kvm_decl.ADDRESS_RE.match(address):
        raise UsageError("invalid address")
    if not 1 <= port <= 65535:
        raise UsageError("port must be between 1 and 65535")


def scan_error(e):
    """Message d'un relevé en échec ; aucune clé reçue peut venir d'une
    pénalité OpenSSH (relevés répétés), on le dit."""
    msg = str(e)
    if "no host key received" in msg:
        msg += ("; if the host runs OpenSSH 9.8 or later, repeated scans are penalised "
                "for about 15s (PerSourcePenalties): try again shortly")
    return msg


def cmd_host_scan(args):
    _check_address(args.address, args.port)
    step("scan", "running", f"reading the host keys of {args.address}:{args.port}")
    try:
        keys = kvm_decl.scan_host_key(args.address, args.port, runner=RUNNER)
    except (kh.KvmError, kvm_decl.DeclError) as e:
        msg = scan_error(e)
        step("scan", "error", msg)
        raise Failure(msg, kind=getattr(e, "kind", "usage")) from None
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


def check_private_key(data, state):
    """La clé est lue par ssh-keygen AVANT tout appel réseau : une clé
    protégée par une phrase de passe ne marcherait jamais en BatchMode.
    Contrôle sur une copie 0600 (ssh-keygen refuse un fichier trop ouvert)
    rangée dans le répertoire privé (0700) des clés de l'état, jamais dans
    /tmp ; la clé publique rendue n'est jamais montrée."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".kvm-key-check-", dir=kvm_decl.ensure_ssh_dir(state))
        with os.fdopen(fd, "w") as f:
            f.write(data)
        res = RUNNER(["ssh-keygen", "-y", "-P", "", "-f", tmp], stdin=subprocess.DEVNULL,
                     capture_output=True, text=True, timeout=KEY_CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Failure("the SSH key check timed out", kind="timeout") from None
    except OSError as e:
        raise Failure(f"cannot check the SSH key ({errno.errorcode.get(e.errno, 'error')})") from None
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    if res.returncode != 0:
        if "passphrase" in (res.stderr or "").lower():
            raise UsageError("the key is protected by a passphrase; provide an unencrypted key",
                             kind="auth")
        raise UsageError("the SSH key file is not a valid private key (ssh-keygen cannot read it)")


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
    try:
        kh.control_path(decl, state)           # socket trop long, `%` : refusé avant tout
    except kh.KvmError as e:
        raise UsageError(e.message) from None
    if kvm_decl.decl_path(state, name).exists():
        raise UsageError(f"host {name} is already declared (remove it first)")
    key = read_private_key(args.ssh_key)
    check_private_key(key, state)
    with host_lock(state):
        # revu sous le verrou : un ajout concurrent a pu finir entre-temps
        if kvm_decl.decl_path(state, name).exists():
            raise UsageError(f"host {name} is already declared (remove it first)")
        step("validate", "done", f"{args.user}@{args.address}:{args.port}")
        return _add_locked(args, state, decl, key)


def _add_locked(args, state, decl, key):
    name = decl["name"]
    step("scan", "running", f"reading the host keys of {args.address}:{args.port}")
    try:
        keys = kvm_decl.scan_host_key(args.address, args.port, runner=RUNNER)
    except (kh.KvmError, kvm_decl.DeclError) as e:
        msg = scan_error(e)
        step("scan", "error", msg)
        raise Failure(msg, kind=getattr(e, "kind", "usage")) from None
    match = [k for k in keys if k["fingerprint"] == args.fingerprint]
    if not match:
        msg = (f"the fingerprint {args.fingerprint} matches none of the host keys "
               f"({', '.join(k['type'] + ' ' + k['fingerprint'] for k in keys)}): nothing saved")
        step("scan", "error", msg)
        raise Failure(msg, kind="hostkey")
    step("scan", "done", f"pinning the {match[0]['type']} key {args.fingerprint}")

    def rollback():
        # jamais d'exception ici : elle masquerait la cause
        with contextlib.suppress(Exception):
            close_master(decl, state)
        with contextlib.suppress(Exception):
            kvm_decl.remove(state, name)

    keep_on_error = False                     # écriture partielle : toujours défaite
    try:
        step("save", "running", "saving the key, the pinned fingerprint and the declaration")
        try:
            kvm_decl.write_key(state, name, key)
            kvm_decl.write_known_hosts(state, name, [match[0]["line"]])
            kvm_decl.write(state, decl)
        except (OSError, kvm_decl.DeclError) as e:
            # jamais de chemin dans le message
            reason = (errno.errorcode.get(e.errno, "error") if isinstance(e, OSError)
                      else "invalid data")
            msg = f"cannot save the host declaration ({reason})"
            step("save", "error", msg)
            raise Failure(msg) from None
        step("save", "done", f"host {name} saved")
        keep_on_error = args.keep_on_failure
        stored = kvm_decl.load(state, name)
        close_master(stored, state)
        checks = run_checks(kh.VirshHost(stored, state, runner=RUNNER), ssh_retry=SSH_RETRY_AFTER_SCAN)
    except BaseException:
        # rien de laissé à moitié, même sur une erreur imprévue
        if not keep_on_error:
            rollback()
        raise
    ok = not any(c["status"] == "error" for c in checks)
    kept = ok or args.keep_on_failure
    if not kept:
        rollback()
        step("save", "done", f"connection test failed: host {name} removed again")
    elif not ok:
        step("save", "warn", f"connection test failed: host {name} kept as asked")
    error, kind = checks_failure(checks)
    emit(args, {"host": public_decl(stored), "fingerprint": args.fingerprint,
                "ok": ok, "kept": kept, "error": error, "kind": kind, "checks": checks},
         checks_text(name, checks) + ("" if kept else f"\nhost {name} not saved"))
    return EXIT_OK if ok else EXIT_FAIL


def checks_failure(checks):
    """(error, kind) du premier point en échec, (None, None) sinon."""
    bad = next((c for c in checks if c["status"] == "error"), None)
    if bad is None:
        return None, None
    return f"connection test failed at {bad['check']}: {bad['message']}", bad["kind"] or "failed"


# champs propres à chaque point, toujours présents (None ou vide par défaut)
CHECK_FIELDS = {"virsh": {"version": None}, "uri": {"libvirt": None, "qemu": None},
                "vnc": {"domains": [], "exposed": 0}}


def _check(cid, status, message, **extra):
    out = {"check": cid, "status": status, "message": message, "kind": None}
    out.update({k: (list(v) if isinstance(v, list) else v)
                for k, v in CHECK_FIELDS.get(cid, {}).items()})
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
        if not listen and g.get("socket"):
            listen = f"socket:{g.get('socket')}"
        port = kh._int(g.get("port"))
        out.append({"type": g.get("type"), "listen": listen,
                    "port": port if port and port > 0 else None,
                    "password": bool(g.get("passwd"))})
    return out


def is_local_listen(listen):
    """Vrai pour une écoute joignable seulement depuis l'hôte : boucle locale
    (127.0.0.0/8, ::1), socket unix, ou pas d'écoute réseau du tout."""
    if not listen or listen.startswith("socket:"):
        return True
    addr = listen.strip("[]")
    if addr.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False                           # nom d'hôte, joker : exposé


def check_vnc(host):
    """Écoute des affichages des VMs actives. Une VM joignable hors de
    l'hôte (toute adresse autre que la boucle locale ou un socket unix) sans
    mot de passe est un avertissement de sécurité."""
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
            if not is_local_listen(d["listen"]) and not d["password"]:
                exposed.append(f"{n} ({d['type']} on {d['listen']}:{d['port'] or 'auto'})")
    if exposed:
        msg = ("security: display reachable from the network without a password: "
               + ", ".join(exposed))
        return _check("vnc", "warn", msg, domains=domains, exposed=len(exposed))
    msg = f"{len(domains)} display(s) on {len(names)} running domain(s), none exposed without a password"
    if unread:
        return _check("vnc", "warn", msg + f"; not read: {', '.join(unread)}", domains=domains)
    return _check("vnc", "ok", msg, domains=domains)


CHECK_IDS = ("ssh", "virsh", "uri", "qemu-img", "vnc")


def run_checks(host, ssh_retry=0):
    """Les points du test de connexion, un résultat chacun. Un point bloquant
    en échec (ssh, virsh, URI) saute les suivants. `ssh_retry` : secondes à
    attendre avant de retenter une fois une connexion refusée (`unreachable`)."""
    checks = []

    def skip_rest(after):
        for cid in CHECK_IDS[CHECK_IDS.index(after) + 1:]:
            checks.append(_check(cid, "skipped", f"skipped: {after} failed"))
        return checks

    try:
        try:
            host.run(["true"])
        except kh.KvmError as e:
            if not ssh_retry or e.kind != "unreachable":
                raise
            step("check-ssh", "warn", f"{e.message}; the host may be penalising the key scan "
                 f"connections (OpenSSH PerSourcePenalties): retrying in {ssh_retry}s")
            SLEEP(ssh_retry)
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
    error, kind = checks_failure(checks)
    emit(args, {"host": host.name, "ok": ok, "error": error, "kind": kind, "checks": checks},
         checks_text(host.name, checks))
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
    if not isinstance(args.name, str) or not kvm_decl.NAME_RE.match(args.name):
        raise UsageError("invalid host name")
    if not kvm_decl.decl_path(state, args.name).exists():
        raise UsageError(f"host {args.name} is not declared")
    with host_lock(state):
        return _remove_locked(args, state)


def _remove_locked(args, state):
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


def read_all(hosts, details=False, deadline=None):
    """Inventaire de chaque hôte, en parallèle ; un hôte en échec rend son
    erreur sans gêner les autres. `deadline` (s) : échéance de l'ensemble ;
    un hôte qui n'a pas répondu à temps rend {kind: "timeout"} et les autres
    sont rendus (sa lecture finit en arrière-plan, bornée par le délai SSH)."""
    def one(host):
        try:
            return kh.host_inventory(host, details=details)
        except kh.KvmError as e:
            return {"name": host.name, "address": host.decl.get("address"), "error": err_info(e)}
        except Exception as e:                 # noqa: BLE001 : jamais fatal pour les autres
            return {"name": host.name, "address": host.decl.get("address"),
                    "error": {"kind": "failed", "message": f"{host.name}: failed: {type(e).__name__}"}}
    if not hosts:
        return []
    pool = ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(hosts)))
    try:
        futures = [pool.submit(one, h) for h in hosts]
        wait(futures, timeout=deadline)
        out = []
        for host, fut in zip(hosts, futures):
            if fut.done():
                out.append(fut.result())
            else:
                out.append({"name": host.name, "address": host.decl.get("address"), "error": {
                    "kind": "timeout",
                    "message": f"{host.name}: timeout: no answer within {deadline:g}s"}})
        return out
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


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
        raise Failure(e.message, kind=e.kind) from None
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

def domstate(host, name, timeout=None):
    """État courant d'une VM (`virsh domstate`) ; KvmError sinon."""
    return _first_line(host.virsh("domstate", "--domain", name, timeout=timeout))


class StateTimeout(Exception):
    def __init__(self, state, elapsed):
        super().__init__(state)
        self.state, self.elapsed = state, elapsed


def follow_state(host, name, accept, timeout, clock=None, sleep=None, interval=POLL_INTERVAL,
                 wait_first=False):
    """Interroge l'état toutes les `interval` secondes jusqu'à un état de
    `accept` ; rend (état, secondes écoulées). StateTimeout à l'échéance.
    Chaque changement d'état est dit en STEP_EVENT. Un hôte injoignable ou
    lent (KvmError `unreachable`, `timeout`) est réinterrogé jusqu'à
    l'échéance, avec un avertissement par série ; toute autre erreur sort
    tout de suite. Ni l'attente ni le délai d'un appel ne dépassent le temps
    restant (sauf le minimum de MIN_CALL_TIMEOUT secondes par appel)."""
    clock, sleep = clock or CLOCK, sleep or SLEEP
    start, last, streak = clock(), None, False
    deadline = start + timeout

    def remaining():
        return deadline - clock()

    def pause():
        sleep(max(0, min(interval, remaining())))

    if wait_first:
        pause()
    while True:
        budget = max(MIN_CALL_TIMEOUT, min(getattr(host, "timeout", DEFAULT_TIMEOUT), remaining()))
        try:
            state = domstate(host, name, timeout=math.ceil(budget))
        except kh.KvmError as e:
            if e.kind not in RETRY_KINDS or remaining() <= 0:
                raise
            if not streak:
                step("wait", "warn", f"{name}: cannot read the state ({e.message}); retrying")
                streak = True
            pause()
            continue
        streak = False
        elapsed = clock() - start
        if state != last:
            step("wait", "running", f"{name}: {state}")
            last = state
        if state in accept:
            return state, elapsed
        if remaining() <= 0:
            raise StateTimeout(state, elapsed)
        pause()


def forced_gestures(action, state):
    """Gestes à proposer à l'échéance, dans l'ordre (vide s'il n'y en a pas)."""
    if action == "reboot" and state == "shut off":
        return ["start"]
    return list(FORCED.get(action, []))


def timeout_message(action, name, state, timeout, accept):
    """Message d'échéance ; propose le geste forcé quand il existe."""
    msg = f"{name} is still {state} after {timeout}s"
    if action == "shutdown":
        return msg + ": the guest did not shut down (no ACPI support or busy); use destroy to force it off"
    if action == "reboot":
        if state == "shut off":
            return msg + ": it did not come back to running; use start to boot it"
        return (msg + ": it did not come back to running; use destroy then start to force a restart")
    return msg + f": expected {' or '.join(sorted(accept))}"


def gesture(args, action):
    host = load_host(state_of(args), args.host)
    name = check_vm_name(args.name)
    timeout = args.timeout if args.timeout is not None else DEFAULT_TIMEOUTS.get(action, DEFAULT_TIMEOUT)
    if timeout <= 0:
        raise UsageError("--timeout must be positive")
    accept, refused = GESTURES[action]
    result = {"host": host.name, "name": name, "action": action, "ok": False,
              "from": None, "state": None, "suggest": []}

    def fail(sid, msg, kind, **extra):
        step(sid, "error", msg)
        result.update(extra)
        raise Failure(msg, kind=kind, data=result)

    step("state", "running", f"reading the state of {name}")
    try:
        before = domstate(host, name)
    except kh.KvmError as e:
        fail("state", e.message, e.kind)
    result["from"] = before
    step("state", "done", f"{name}: {before}")
    if before in refused:
        fail("validate", f"cannot {action} {name}: it {refused[before]}", "invalid")

    command = ACTION_COMMANDS.get((action, before), action)
    step("action", "running", f"virsh {command} {name}")
    try:
        host.virsh(command, "--domain", name)
    except kh.KvmError as e:
        fail("action", e.message, e.kind)
    step("action", "done", f"{action} requested")

    gone = False
    try:
        state, elapsed = follow_state(host, name, accept, timeout, wait_first=(action == "reboot"))
    except kh.KvmError as e:
        if e.kind == "notfound" and action in ("shutdown", "destroy"):
            # domaine transitoire : libvirt l'efface à l'arrêt, c'est la réussite
            gone, state, elapsed = True, None, 0.0
        else:
            fail("wait", f"{e.message}; the {action} was requested but its result is not confirmed",
                 e.kind)
    except StateTimeout as t:
        fail("wait", timeout_message(action, name, t.state, timeout, accept), "timeout",
             state=t.state, suggest=forced_gestures(action, t.state))
    if gone:
        text = f"{name}: stopped; the domain no longer exists (transient domain)"
    elif action == "reboot":
        text = f"{name}: reboot requested; the domain is running"
    else:
        text = f"{name}: {state}"
    step("wait", "done", text if gone else f"{text} after {elapsed:.0f}s")
    result.update(ok=True, state=state, elapsed_s=round(elapsed, 1), gone=gone,
                  confirmed=(action != "reboot"))
    del result["suggest"]
    emit(args, result, text)
    return EXIT_OK


# ---------------------------------------------------------------------------
# v1.97.0 : affichage, instantanés, clonage, suppression (bin/lib/kvm_ops.py)
# ---------------------------------------------------------------------------

def checked(fn, *a):
    """Contrôle d'entrée de kvm_ops ; un refus (OpError `invalid`) devient
    une erreur d'usage (code 2), avant tout appel à l'hôte."""
    try:
        return fn(*a)
    except ko.OpError as e:
        raise UsageError(e.message) from None


def positive_timeout(value, default):
    timeout = default if value is None else value
    if timeout <= 0:
        raise UsageError("--timeout must be positive")
    return timeout


@contextlib.contextmanager
def vm_lock(state, host, *names):
    """Verrou d'une opération sur une VM (`<état>/kvm.d/locks/`), pris sans
    attendre : un clonage et une suppression de la même VM ne se croisent
    jamais. Le nom du fichier est un condensé du nom de la VM (un nom libvirt
    peut contenir n'importe quoi)."""
    d = kvm_decl.decl_dir(state) / "locks"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fds = []
    try:
        for name in sorted(set(names)):
            digest = hashlib.sha256(name.encode()).hexdigest()[:16]
            fd = os.open(d / f"{host}-{digest}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            fds.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UsageError(f"another operation on {name} is in progress on {host}: "
                                 "try again later", kind="busy") from None
        yield
    finally:
        for fd in fds:
            os.close(fd)


class Progress:
    """Rappel `progress` de kvm_ops vers STEP_EVENT ; garde la dernière
    étape pour y dire l'échec si la bibliothèque ne l'a pas dit."""

    def __init__(self, first="plan"):
        self.sid, self.status = first, None

    def __call__(self, sid, status, msg=""):
        status = "error" if status == "failed" else status
        self.sid, self.status = sid, status
        step(sid, status, msg)

    def fail(self, msg):
        if self.status != "error":
            step(self.sid, "error", msg)
            self.status = "error"


def describe_leftover(item):
    """Ce qui reste d'un retour arrière, en une phrase."""
    kind = item.get("kind")
    if kind == "domain":
        what = f"domain {item.get('name')}"
    elif kind == "volume":
        what = f"volume {item.get('pool')}/{item.get('volume')}"
    else:
        what = item.get("destination") or item.get("path") or "?"
    return f"{what} ({item['error']})" if item.get("error") else what


def op_failure(prog, e, data):
    """OpError ou KvmError d'une opération -> Failure (code 1) dont l'objet
    JSON garde ses champs (refus, restes)."""
    msg = e.message
    prog.fail(msg)
    extra = dict(data)
    if isinstance(e, ko.OpError):
        if "refusals" in extra:
            extra["refusals"] = list(e.data.get("refusals") or [])
    if "leftovers" in extra:
        extra["leftovers"] = list(getattr(e, "leftovers", None) or [])
    return Failure(msg, kind=e.kind, data=extra)


DISPLAY_REASONS = {
    "notrunning": "the VM is not running: its display opens once it runs",
    "spice": "this VM has only a SPICE display: not supported",
    "socket": "the display listens on a UNIX socket owned by root and qemu: "
              "the SSH user cannot reach it",
    "nodisplay": "this VM has no graphical display",
    "nolisten": "the display has no listener (listen type none)",
    "listen": "the display listens on an address that is not one of the host's own: "
              "not reachable through the host",
}


def tunnel_command(decl, address, port):
    """Commande `ssh -L` à lancer soi-même (sans chemin de clé)."""
    target = f"[{address}]" if ":" in address else address
    return (f"ssh -N -L {port}:{target}:{port} -p {decl.get('port', kvm_decl.DEFAULT_PORT)} "
            f"-l {decl['user']} {decl['address']}")


def cmd_vm_display(args):
    """Où joindre l'affichage VNC d'une VM. Le XML est lu avec
    --security-info pour savoir SI un mot de passe existe ; sa valeur ne
    quitte jamais l'analyseur (parse_domain_xml ne rend qu'un booléen)."""
    host = load_host(state_of(args), args.host)
    name = check_vm_name(args.name)
    step("display", "running", f"reading the display of {name}")
    try:
        out = host.batch({"state": ("domstate", "--domain", name),
                          "xml": ("dumpxml", "--security-info", "--domain", name)})
        for key in ("state", "xml"):
            if not out[key].ok:
                raise out[key].error
        state = _first_line(out["state"])
        vm = kh.parse_domain_xml(str(out["xml"]))
        live = state != "shut off"
        target = ko.display_target(vm, live)
        if target.get("reason") == "listen":
            # IP précise : acceptée seulement si elle appartient à l'hôte
            target = ko.display_target(vm, live, host_addresses=ko.host_addresses(host))
    except kh.KvmError as e:
        step("display", "error", e.message)
        raise Failure(e.message, kind=e.kind) from None
    except (ValueError, SyntaxError):          # XML illisible (jamais cité)
        msg = f"{host.name}: unreadable definition for {name}"
        step("display", "error", msg)
        raise Failure(msg) from None
    reason = target.get("reason")
    result = {"ok": True, "host": host.name, "name": name, "state": state,
              "available": reason is None, "type": target.get("type"),
              "address": target.get("address"), "port": target.get("port"),
              "password": bool(target.get("password")), "reason": reason,
              "message": DISPLAY_REASONS.get(reason, ""), "tunnel": None}
    if reason is None:
        result["tunnel"] = tunnel_command(host.decl, target["address"], target["port"])
        result["message"] = (f"VNC on {target['address']}:{target['port']} of the host"
                             + (", protected by a password" if target["password"] else ""))
        text = (f"{name} on {host.name}: {result['message']}\n"
                f"open a tunnel with: {result['tunnel']}\n"
                f"then point a VNC viewer at localhost:{target['port']}")
    else:
        text = f"{name} on {host.name}: no display to open ({reason}): {result['message']}"
    step("display", "done" if reason is None else "warn", result["message"])
    emit(args, result, text)
    return EXIT_OK


def _gib_text(kib):
    return f"{kib / 1024 / 1024:.1f} GiB" if isinstance(kib, (int, float)) else "unknown size"


def snapshot_rows(snaps):
    head = ("NAME", "CREATED", "STATE", "LOCATION", "PARENT", "")
    rows = [head] + [(s["name"], s.get("created") or "-", s.get("state") or "-",
                      s.get("location") or "-", s.get("parent") or "-",
                      "current" if s.get("current") else "") for s in snaps]
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(head))]
    return ["  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]


def cmd_snapshot_list(args):
    host = load_host(state_of(args), args.host)
    name = check_vm_name(args.name)
    step("list", "running", f"reading the snapshots of {name}")
    try:
        listing = ko.list_snapshots(host, name)
    except (ko.OpError, kh.KvmError) as e:
        step("list", "error", e.message)
        raise Failure(e.message, kind=e.kind) from None
    step("list", "done", f"{len(listing['snapshots'])} snapshot(s)")
    lines = [f"{name} on {host.name}: {listing['state']}"]
    lines += snapshot_rows(listing["snapshots"]) if listing["snapshots"] else ["(no snapshot)"]
    lines += [f"cannot take a snapshot: {r}" for r in listing["support"]]
    emit(args, {"ok": True, "host": host.name, "name": name, **listing}, "\n".join(lines))
    return EXIT_OK


def _snapshot_target(args, new=False):
    host = load_host(state_of(args), args.host)
    name = check_vm_name(args.name)
    snap = checked(ko.check_snapshot_name, args.snapshot, new)
    return host, name, snap


def _snapshot_plan(prog, host, name, what):
    """Étape `plan` d'un geste d'instantané : la liste relue (la
    bibliothèque la relit encore au moment d'agir)."""
    prog("plan", "running", f"reading the snapshots of {name}")
    listing = ko.list_snapshots(host, name)
    prog("plan", "done", what(listing))
    return listing


def cmd_snapshot_create(args):
    host, name, snap = _snapshot_target(args, new=True)
    description = checked(ko._check_description, args.description)
    timeout = positive_timeout(args.timeout, SNAPSHOT_TIMEOUTS["create"])
    data = {"host": host.name, "name": name, "snapshot": snap, "refusals": []}
    prog = Progress()

    def what(listing):
        if listing["state"] == "running":
            return (f"{name} is running: the guest is paused while its memory "
                    f"({_gib_text(listing['memory_kib'])}) is saved")
        return f"{name} is {listing['state']}: the snapshot holds the disks"
    with vm_lock(state_of(args), host.name, name):
        try:
            _snapshot_plan(prog, host, name, what)
            prog("create", "running", f"snapshot {snap}")
            t0 = CLOCK()
            res = ko.create_snapshot(host, name, snap, description, timeout=timeout)
            elapsed = CLOCK() - t0
            prog("create", "done", f"snapshot {snap} taken in {elapsed:.1f}s"
                 + (" (memory included)" if res["memory"] else ""))
            prog("verify", "done", f"{len(res['snapshots'])} snapshot(s), current: "
                 f"{res['current'] or 'none'}")
        except (ko.OpError, kh.KvmError) as e:
            raise op_failure(prog, e, data) from None
    data.pop("refusals")
    emit(args, {"ok": True, **data, "memory": res["memory"], "state": res["state"],
                "current": res["current"], "snapshots": res["snapshots"],
                "elapsed_s": round(elapsed, 1)},
         f"{name}: snapshot {snap} taken" + (" with its memory" if res["memory"] else "")
         + f" in {elapsed:.1f}s")
    return EXIT_OK


def cmd_snapshot_revert(args):
    host, name, snap = _snapshot_target(args)
    timeout = positive_timeout(args.timeout, SNAPSHOT_TIMEOUTS["revert"])
    data = {"host": host.name, "name": name, "snapshot": snap, "refusals": []}
    prog = Progress()

    def what(listing):
        s = next((x for x in listing["snapshots"] if x["name"] == snap), None)
        if s is None:
            return f"snapshot {snap} not listed"
        return (f"back to {snap} (taken {s.get('state') or 'in an unknown state'}): the "
                f"current state of {name} that was not saved is lost")
    with vm_lock(state_of(args), host.name, name):
        try:
            _snapshot_plan(prog, host, name, what)
            prog("revert", "running", f"snapshot {snap}")
            t0 = CLOCK()
            res = ko.revert_snapshot(host, name, snap, timeout=timeout)
            elapsed = CLOCK() - t0
            prog("revert", "done", f"reverted to {snap} in {elapsed:.1f}s")
            prog("verify", "done", f"{name}: {res['state']}")
        except (ko.OpError, kh.KvmError) as e:
            raise op_failure(prog, e, data) from None
    data.pop("refusals")
    emit(args, {"ok": True, **data, "taken_state": res["taken_state"], "state": res["state"],
                "elapsed_s": round(elapsed, 1)},
         f"{name}: reverted to {snap}, now {res['state']}")
    return EXIT_OK


def cmd_snapshot_delete(args):
    host, name, snap = _snapshot_target(args)
    timeout = positive_timeout(args.timeout, SNAPSHOT_TIMEOUTS["delete"])
    data = {"host": host.name, "name": name, "snapshot": snap, "refusals": []}
    prog = Progress()

    def what(listing):
        children = [x["name"] for x in listing["snapshots"] if x.get("parent") == snap]
        if children:
            return (f"deleting {snap}: its children ({', '.join(children)}) are attached "
                    "to its parent")
        return f"deleting {snap}"
    with vm_lock(state_of(args), host.name, name):
        try:
            _snapshot_plan(prog, host, name, what)
            prog("delete", "running", f"snapshot {snap}")
            res = ko.delete_snapshot(host, name, snap, timeout=timeout)
            prog("delete", "done", f"snapshot {snap} deleted")
            prog("verify", "done", f"{len(res['snapshots'])} snapshot(s), current: "
                 f"{res['current'] or 'none'}")
        except (ko.OpError, kh.KvmError) as e:
            raise op_failure(prog, e, data) from None
    data.pop("refusals")
    emit(args, {"ok": True, **data, "current": res["current"], "snapshots": res["snapshots"]},
         f"{name}: snapshot {snap} deleted")
    return EXIT_OK


def plan_text(title, refusals, lines):
    out = [title] + [f"  {ln}" for ln in lines]
    out += [f"  refused: {r}" for r in refusals]
    return "\n".join(out)


def cmd_clone(args):
    host = load_host(state_of(args), args.host)
    name = check_vm_name(args.name)
    new = checked(ko.check_new_vm_name, args.new_name)
    timeout = positive_timeout(args.timeout, CLONE_TIMEOUT)
    data = {"host": host.name, "source": name, "new_name": new, "refusals": [],
            "leftovers": []}
    prog = Progress()
    if args.dry_run:
        prog("plan", "running", f"{name} -> {new} (dry run)")
        try:
            plan = ko.clone_plan(host, name, new)
        except (ko.OpError, kh.KvmError) as e:
            raise op_failure(prog, e, data) from None
        prog("plan", "warn" if plan["refusals"] else "done",
             "; ".join(plan["refusals"]) or f"{len(plan['disks'])} disk(s) to copy")
        lines = [f"copy {d['target']}: {d['source']} -> {d['destination']} "
                 f"({d['needed']} bytes)" for d in plan["disks"]]
        lines += [f"shared {d['target']}: {d['source']} ({d['reason']})" for d in plan["shared"]]
        emit(args, {"ok": True, "host": host.name, "source": name, "new_name": new,
                    "dry_run": True, "possible": not plan["refusals"], "plan": plan},
             plan_text(f"clone {name} -> {new} on {host.name} (dry run)", plan["refusals"],
                       lines))
        return EXIT_OK
    with vm_lock(state_of(args), host.name, name, new):
        t0 = CLOCK()
        try:
            res = ko.clone_vm(host, name, new, timeout=timeout, progress=prog, sleep=SLEEP)
        except (ko.OpError, kh.KvmError) as e:
            failure = op_failure(prog, e, data)
            rollback_step(e)
            raise failure from None
        except Exception as e:                 # noqa: BLE001 : restes dits, sans trace
            msg = f"unexpected internal error ({type(e).__name__})"
            prog.fail(msg)
            rollback_step(e)
            raise Failure(msg, data={**data, "leftovers": list(getattr(e, "leftovers", []))}) \
                from None
        elapsed = CLOCK() - t0
    lines = [f"disk {d['target']}: {d['destination']}" for d in res["disks"]]
    lines += [f"note: {w}" for w in res["warnings"]]
    if res["uefi_entries_lost"]:
        lines.append("note: new UEFI variables (boot entries are not copied)")
    emit(args, {"ok": True, "host": host.name, "dry_run": False,
                "elapsed_s": round(elapsed, 1), **res},
         plan_text(f"{new}: copy of {name} on {host.name}, uuid {res['uuid']}, {res['state']}",
                   [], lines))
    return EXIT_OK


def rollback_step(e):
    """Dit le retour arrière d'un clonage (si quelque chose avait été créé)."""
    left = getattr(e, "leftovers", None)
    if left is None:
        return
    if left:
        step("rollback", "warn", "not removed: " + ", ".join(describe_leftover(x) for x in left))
    else:
        step("rollback", "done", "everything created was removed")


def cmd_vm_delete(args):
    host = load_host(state_of(args), args.host)
    name = check_vm_name(args.name)
    ack = args.acknowledge_unreadable
    if ack and not args.remove_storage:
        raise UsageError("--acknowledge-unreadable only applies with --remove-storage")
    checked(ko.check_acknowledged, ack)
    timeout = positive_timeout(args.timeout, DELETE_TIMEOUT)
    data = {"host": host.name, "name": name, "refusals": [], "leftovers": [],
            "unreadable": []}
    prog = Progress()

    def disk_lines(d):
        return (d.get("path") or d.get("source") or d.get("target") or "?")
    if args.dry_run:
        prog("plan", "running", f"what {name} owns (dry run)")
        try:
            plan = ko.delete_plan(host, name, args.remove_storage, ack)
        except (ko.OpError, kh.KvmError) as e:
            raise op_failure(prog, e, data) from None
        prog("plan", "warn" if plan["refusals"] else "done",
             "; ".join(plan["refusals"]) or f"{len(plan['delete'])} disk(s) to delete, "
             f"{len(plan['keep'])} kept")
        lines = [f"delete {d['target']}: {disk_lines(d)}" for d in plan["delete"]]
        lines += [f"keep {d['target']}: {disk_lines(d)} ({d['reason']})" for d in plan["keep"]]
        lines += unreadable_lines(plan["unreadable"], plan["acknowledged_unreadable"])
        lines += [f"note: {n}" for n in plan["notes"]]
        emit(args, {"ok": True, "host": host.name, "name": name, "dry_run": True,
                    "possible": not plan["refusals"], "plan": plan},
             plan_text(f"delete {name} on {host.name} (dry run)", plan["refusals"], lines))
        return EXIT_OK
    with vm_lock(state_of(args), host.name, name):
        try:
            res = ko.delete_vm(host, name, args.remove_storage, timeout=timeout,
                               acknowledged_unreadable=ack, progress=prog)
        except (ko.OpError, kh.KvmError) as e:
            if isinstance(e, ko.OpError):
                data["unreadable"] = list((e.data.get("plan") or {}).get("unreadable") or [])
            raise op_failure(prog, e, data) from None
    lines = [f"deleted {d['target']}: {disk_lines(d)}" for d in res["deleted"]]
    lines += [f"kept {d['target']}: {disk_lines(d)} ({d['reason']})" for d in res["kept"]]
    lines += [f"left in place {d['target']}: {disk_lines(d)} ({d['error']})"
              for d in res["leftovers"]]
    lines += unreadable_lines(res["unreadable"], res["acknowledged_unreadable"])
    lines += [f"note: {n}" for n in res["notes"]]
    emit(args, {"ok": True, "host": host.name, "dry_run": False, **res},
         plan_text(f"{name} deleted from {host.name}", [], lines))
    return EXIT_OK


def unreadable_lines(unreadable, acknowledged):
    """Fichiers illisibles par le compte SSH qui ont empêché la preuve."""
    left = [p for p in unreadable if p not in set(acknowledged)]
    if not left:
        return []
    return [f"unreadable by the SSH user: {p}" for p in left] + [
        "disks are kept while these files cannot be read; if you are sure none of them "
        "uses a disk of this VM, add --acknowledge-unreadable=PATH for each"]


# ---------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    want_json = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
        want_json = args.json
        with cancel_on_signals():
            try:
                return args.fn(args)
            except Cancelled as e:
                # un clonage interrompu nomme ce que son retour arrière a laissé
                data = ({"leftovers": list(e.leftovers)} if getattr(e, "leftovers", None)
                        is not None else None)
                return fail_exit(want_json, "cancelled", cancel_message(args, e), "cancelled",
                                 EXIT_CANCELLED, data)
    except (UsageError, kvm_decl.DeclError) as e:
        kind = getattr(e, "kind", "usage")
        return fail_exit(want_json, "validate", str(e), kind, EXIT_USAGE)
    except Failure as e:
        return fail_exit(want_json, None, e.message, e.kind, EXIT_FAIL, e.data)
    except kh.KvmError as e:
        return fail_exit(want_json, "host", e.message, e.kind, EXIT_FAIL)
    except ko.OpError as e:
        if e.kind == "invalid":
            return fail_exit(want_json, "validate", e.message, "usage", EXIT_USAGE)
        return fail_exit(want_json, "host", e.message, e.kind, EXIT_FAIL)
    except Exception as e:                     # noqa: BLE001 : ni trace ni chemin à l'appelant
        return fail_exit(want_json, "internal", f"unexpected internal error ({type(e).__name__})",
                         "failed", EXIT_FAIL)


def cancel_message(args, sig):
    """Ce qu'une annulation laisse, selon la commande (jamais « défait » pour
    un geste : virsh a peut-être déjà reçu la demande)."""
    if args.cmd == "host" and args.hcmd == "add":
        return f"host add cancelled ({sig}): nothing saved"
    if args.cmd == "vm" and args.vcmd in GESTURES:
        return (f"{args.vcmd} cancelled ({sig}): it may already have been requested; "
                f"its result was not followed")
    if getattr(args, "dry_run", False):
        return f"cancelled ({sig})"            # un aperçu ne change rien
    if args.cmd == "snapshot" and args.scmd in SNAPSHOT_DONE:
        return (f"snapshot {args.scmd} cancelled ({sig}): it may already have been "
                f"{SNAPSHOT_DONE[args.scmd]}: list the snapshots")
    if args.cmd == "clone":
        left = getattr(sig, "leftovers", None)
        if left is None:
            return f"clone cancelled ({sig}) before any copy: nothing created"
        if not left:
            return f"clone cancelled ({sig}): rolled back"
        return (f"clone cancelled ({sig}): rollback incomplete, left on the host: "
                + ", ".join(describe_leftover(x) for x in left))
    if args.cmd == "vm" and args.vcmd == "delete":
        return (f"vm delete cancelled ({sig}): it may already have been undefined: "
                "check the VM list")
    return f"cancelled ({sig})"


def fail_exit(want_json, sid, message, kind, code, data=None):
    """Un échec : STEP_EVENT (sauf s'il est déjà dit), message sur stderr,
    et avec --json un seul objet {"ok": false, "error", "kind"} sur stdout."""
    if sid:
        step(sid, "error", message)
    print(f"error: {message}", file=sys.stderr)
    if want_json:
        print_json({**(data or {}), "ok": False, "error": message, "kind": kind})
    return code


def build_parser():
    common = Parser(add_help=False)
    common.add_argument("--json", action="store_true", help="JSON output on stdout")
    common.add_argument("--state-dir", help="console state directory (default HARVESTER_OPS_STATE_DIR)")

    ap = Parser(prog="harvester-kvm", description="KVM/libvirt hosts driven by virsh over SSH.")
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
    sp = vp.add_parser("display", parents=[common],
                       help="where to reach the VNC display of a VM (never its password)")
    sp.add_argument("--host", required=True)
    sp.add_argument("--name", required=True)
    sp.set_defaults(fn=cmd_vm_display)
    sp = vp.add_parser("delete", parents=[common], help="delete a shut off VM")
    sp.add_argument("--host", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--remove-storage", action="store_true",
                    help="also delete the disks this VM provably owns")
    sp.add_argument("--acknowledge-unreadable", action="append", metavar="PATH",
                    help="file the SSH user cannot read, accepted as using no disk of this VM "
                         "(repeatable)")
    sp.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sp.add_argument("--timeout", type=int, help=f"seconds (default {DELETE_TIMEOUT})")
    sp.set_defaults(fn=cmd_vm_delete)

    np = sub.add_parser("snapshot", help="internal snapshots of a VM").add_subparsers(
        dest="scmd", required=True)
    sp = np.add_parser("list", parents=[common], help="snapshots of a VM")
    sp.add_argument("--host", required=True)
    sp.add_argument("--name", required=True)
    sp.set_defaults(fn=cmd_snapshot_list)
    for verb, fn in (("create", cmd_snapshot_create), ("revert", cmd_snapshot_revert),
                     ("delete", cmd_snapshot_delete)):
        sp = np.add_parser(verb, parents=[common], help=f"{verb} an internal snapshot")
        sp.add_argument("--host", required=True)
        sp.add_argument("--name", required=True)
        sp.add_argument("--snapshot", required=True)
        if verb == "create":
            sp.add_argument("--description")
        sp.add_argument("--timeout", type=int,
                        help=f"seconds (default {SNAPSHOT_TIMEOUTS[verb]})")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("clone", parents=[common], help="full copy of a shut off VM")
    sp.add_argument("--host", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--new-name", required=True)
    sp.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sp.add_argument("--timeout", type=int, help=f"seconds (default {CLONE_TIMEOUT})")
    sp.set_defaults(fn=cmd_clone)
    return ap


if __name__ == "__main__":
    sys.exit(main())
