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
        [--acknowledge-unreadable=PATH ...] (--dry-run | --expect-delete=DIGEST)
        [--timeout S] [--survey-timeout S]
  harvester-kvm.py snapshot list --host N --name D
  harvester-kvm.py snapshot create --host N --name D --snapshot S [--description T] [--timeout S]
  harvester-kvm.py snapshot revert|delete --host N --name D --snapshot S [--timeout S]
  harvester-kvm.py clone --host N --name D --new-name C [--dry-run] [--timeout S]

  Prochaine version KVM (créer, modifier, images, volumes, pools, réseaux) :
  harvester-kvm.py host caps --host N [--machine=q35|pc]
  harvester-kvm.py vm create --host N --spec-file=F|- [--dry-run] [--timeout S]
        (ou, à la main : --name --disk=KIND:... --nic=KIND:... --vcpus --memory-mib
        --user-data=FILE --start ...)
  harvester-kvm.py vm edit --host N --name D --spec-file=F --expect-config=DIGEST
        [--dry-run] [--timeout S]
  harvester-kvm.py vm cpumem --host N --name D [--vcpus=N] [--memory-mib=M]
  harvester-kvm.py vm add-volume|add-nic --host N --name D --spec-file=F
  harvester-kvm.py vm remove-volume|eject-image --host N --name D --target=T
  harvester-kvm.py vm insert-cdrom --host N --name D --target=T --pool=P --volume=V
  harvester-kvm.py vm remove-nic --host N --name D --mac=M
  harvester-kvm.py vm cloudinit --host N --name D --spec-file=F [--rerun]
  harvester-kvm.py image list --host N [--fresh]
  harvester-kvm.py image verify --host N --pool=P --volume=V [--sha256=H]
  harvester-kvm.py image upload|vol upload --host N --pool=P --name=V --file=PATH
        [--sha256=H] [--format=F] [--no-verify] [--limit-mbps=M] [--author=A]
  harvester-kvm.py image download-url --host N --pool=P --name=V --url=U
        [--allow-private=CIDR ...] [--max-size=SIZE] [...]
  harvester-kvm.py image delete --host N --pool=P --volume=V (--dry-run | --expect-delete=D)
  harvester-kvm.py vol create --host N --pool=P --name=V --capacity=SIZE [--format=F]
        [--backing=IMAGE]
  harvester-kvm.py vol resize --host N --pool=P --name=V --capacity=SIZE
  harvester-kvm.py vol delete --host N --pool=P --name=V (--dry-run | --expect-delete=D)
  harvester-kvm.py pool define --host N --spec-file=F|--name --path [--type] [--dry-run]
  harvester-kvm.py pool start|stop|refresh --host N --pool=P
  harvester-kvm.py pool autostart --host N --pool=P [--disable]
  harvester-kvm.py pool delete --host N --pool=P [--remove-dir] (--dry-run | --expect-delete=D)
  harvester-kvm.py net define --host N --spec-file=F|--name --mode [--subnet] [--dry-run]
  harvester-kvm.py net start|stop --host N --network=R
  harvester-kvm.py net autostart --host N --network=R [--disable]
  harvester-kvm.py net delete --host N --network=R (--dry-run | --expect-delete=D)
  harvester-kvm.py net links --host N

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

Instantanés, clonage et suppression (v1.98.0, bin/lib/kvm_ops.py) : chaque
commande émet `plan`, puis ses étapes, relit l'état réel à la fin et rend
ce qui a été fait (`deleted`, `kept` avec leur raison, `leftovers`). Une
demande refusée par l'état de la VM (en marche, disque raw...) sort en 1,
kind `refused`, avec la liste `refusals`. Deux opérations sur la même VM
(clonage : la source et le nom du clone) ne tournent jamais en même temps :
la seconde sort en 2, kind `busy` ; les gestes de la 1.96 prennent le même
verrou. `vm display` dit où joindre l'affichage VNC et la commande `ssh -L`
à lancer soi-même ; le XML est lu avec --security-info, en mémoire, pour
savoir SI un mot de passe VNC existe : sa valeur n'est jamais écrite.

Création, modification, images, volumes, pools et réseaux : le spec arrive
par un fichier (`--spec-file=`, 0600 dans le répertoire de travail de la
console), jamais par argv, et son contenu n'est jamais recopié dans une
sortie (le JSON dit `user_data: true` et la taille). Le mot de passe VNC est
tiré par la console et n'apparaît nulle part. Chaque commande relit l'état
à la fin. Une création tient le verrou de chaque nom d'instance, en
exclusif chaque volume qu'elle va créer (disques, seed), en partage les
pools où elle écrit et les réseaux qu'elle cite ; arrêter ou supprimer un
pool ou un réseau le prend en exclusif, supprimer un volume ou une image
prend le verrou du volume (kind `busy` si une création l'écrit). Toute
suppression réelle (vm, vol, image, pool, net) exige
`--expect-delete=<digest de l'aperçu>` montré par `--dry-run` (sans lui :
erreur d'usage, code 2 ; `plan-changed` si l'objet a changé depuis
l'aperçu), la modification exige
`--expect-config=<config_digest lu à l'ouverture>` (`changed` sinon).
Un téléversement envoie le fichier sur une connexion ssh propre, publie sa
progression en PROGRESS_EVENT|upload|<json> (débit, temps restant), et un
volume partiel est supprimé sur échec ou annulation (libvirt le dit « in
use » tant que le flux distant n'est pas fermé : la suppression est
réessayée jusqu'à 30 s, puis le reste est nommé). Un téléversement n'a pas
de --timeout : il s'arrête par SIGTERM/SIGHUP (bouton « Annuler »).
`image download-url` télécharge sur la machine de la console (jamais sur
l'hôte), puis envoie ; seules les adresses publiques sont jointes, à chaque
redirection (pas de boucle locale, de lien local, d'adresse privée ni de
métadonnées), sauf les plages données par --allow-private=CIDR ; taille
plafonnée par --max-size.

Codes de sortie : 0 fait, 1 échec (hôte, libvirt, délai), 2 erreur d'usage
ou demande refusée avant l'hôte, 3 annulé (SIGTERM ou SIGHUP : ce qui était
commencé est défait, `host add` ne laisse ni déclaration ni clé ; un clonage
retire ce qu'il a créé et nomme ce qui reste).
"""

import argparse
import contextlib
import errno
import fcntl
import functools
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import cluster_decl  # noqa: E402
import kvm_decl  # noqa: E402
import kvm_caps  # noqa: E402
import kvm_create as kc  # noqa: E402
import kvm_edit as ke  # noqa: E402
import kvm_host as kh  # noqa: E402
import kvm_net as kn  # noqa: E402
import kvm_ops as ko  # noqa: E402
import kvm_storage as ks  # noqa: E402
import vm_transfer_progress as vtp  # noqa: E402

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

# délais par défaut (s) des opérations de la 1.98.0
SNAPSHOT_TIMEOUTS = {"create": 600, "revert": 300, "delete": 300}
CLONE_TIMEOUT = 3600
DELETE_TIMEOUT = 300
# délai de chaque appel ssh du relevé des disques de l'hôte (suppression)
DELETE_SURVEY_TIMEOUT = 120
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
        names = kh.parse_names(host.virsh("list", "--name", idempotent=True))
    except kh.KvmError as e:
        return _check("vnc", "warn", f"cannot list the running domains: {e.message}", kind=e.kind)
    if not names:
        return _check("vnc", "ok", "no running domain", domains=[])
    try:
        out = host.batch({n: ("dumpxml", "--security-info", "--domain", n) for n in names},
                         idempotent=True)
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
    return _first_line(host.virsh("domstate", "--domain", name, timeout=timeout, idempotent=True))


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
    # même verrou par VM que les opérations de la 1.98 : un geste ne croise
    # jamais un clonage, une suppression ou un instantané de la même VM
    with vm_lock(state_of(args), host.name, name):
        return _gesture_locked(args, action, host, name, timeout)


def _gesture_locked(args, action, host, name, timeout):
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
# v1.98.0 : affichage, instantanés, clonage, suppression (bin/lib/kvm_ops.py)
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
    --security-info : le mot de passe VNC est donc lu, en mémoire, pour
    savoir s'il en existe un ; il n'est jamais écrit (ni sortie, ni étape,
    ni erreur), parse_domain_xml n'en rend qu'un booléen."""
    host = load_host(state_of(args), args.host)
    name = check_vm_name(args.name)
    step("display", "running", f"reading the display of {name}")
    try:
        out = host.batch({"state": ("domstate", "--domain", name),
                          "xml": ("dumpxml", "--security-info", "--domain", name)},
                         idempotent=True)
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
    if not isinstance(kib, (int, float)):
        return "unknown size"
    if kib < 1024 * 1024:
        return f"{kib / 1024:.0f} MiB"
    return f"{kib / 1024 / 1024:.1f} GiB"


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
        taken = s.get("state") or "in an unknown state"
        text = (f"back to {snap} (taken {taken}): the current state of {name} that was not "
                "saved is lost")
        if listing["state"] != "shut off" and taken in ("shutoff", "shut off"):
            text += (f"; {name} is {listing['state']} and the snapshot was taken shut off: "
                     f"reverting powers {name} off")
        return text
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
    with vm_lock(state_of(args), host.name, name, new), noting_progress(prog):
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
             plan_text(f"{new}: copy of {name} on {host.name}, uuid {res['uuid']}, "
                       f"{res['state']}", [], lines))
    return EXIT_OK


@contextlib.contextmanager
def noting_progress(prog):
    """Une annulation emporte la dernière étape dite : une annulation qui
    arrive après `verify|done` (opération déjà complète) n'est alors jamais
    annoncée comme « rien n'a été fait »."""
    try:
        yield
    except Cancelled as c:
        c.last_step = (prog.sid, prog.status)
        raise


def _completed(sig):
    return getattr(sig, "last_step", None) == ("verify", "done")


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
    survey_timeout = positive_timeout(args.survey_timeout, DELETE_SURVEY_TIMEOUT)
    expect = required_digest(args, "a VM")
    data = {"host": host.name, "name": name, "refusals": [], "leftovers": [],
            "unreadable": []}
    prog = Progress()

    def disk_lines(d):
        return (d.get("path") or d.get("source") or d.get("target") or "?")
    if args.dry_run:
        prog("plan", "running", f"what {name} owns (dry run)")
        try:
            plan = ko.delete_plan(host, name, args.remove_storage, ack,
                                  survey_timeout=survey_timeout)
        except (ko.OpError, kh.KvmError) as e:
            raise op_failure(prog, e, data) from None
        prog("plan", "warn" if plan["refusals"] else "done",
             "; ".join(plan["refusals"]) or f"{len(plan['delete'])} disk(s) to delete, "
             f"{len(plan['keep'])} kept")
        lines = [f"delete {d['target']}: {disk_lines(d)}" for d in plan["delete"]]
        lines += [f"keep {d['target']}: {disk_lines(d)} ({d['reason']})" for d in plan["keep"]]
        lines += unreadable_lines(plan["unreadable"], plan["acknowledged_unreadable"])
        lines += [f"note: {n}" for n in plan["notes"]]
        if not plan["refusals"]:
            lines.append(f"to delete exactly this, add --expect-delete={plan['digest']}")
        emit(args, {"ok": True, "host": host.name, "name": name, "dry_run": True,
                    "possible": not plan["refusals"], "plan": plan},
             plan_text(f"delete {name} on {host.name} (dry run)", plan["refusals"], lines))
        return EXIT_OK
    with vm_lock(state_of(args), host.name, name), noting_progress(prog):
        try:
            res = ko.delete_vm(host, name, args.remove_storage, timeout=timeout,
                               acknowledged_unreadable=ack, progress=prog,
                               expect_delete=expect, survey_timeout=survey_timeout)
        except (ko.OpError, kh.KvmError) as e:
            failure = op_failure(prog, e, data)
            if isinstance(e, ko.OpError):
                plan = e.data.get("plan") or {}
                failure.data["unreadable"] = list(plan.get("unreadable") or [])
                left = [p for p in failure.data["unreadable"]
                        if p not in set(plan.get("acknowledged_unreadable") or [])]
                if e.kind == "refused" and left:
                    failure.message = (f"{failure.message}; if you are sure, add "
                                       "--acknowledge-unreadable=PATH for each of: "
                                       + ", ".join(left))
            raise failure from None
        lines = [f"deleted {d['target']}: {disk_lines(d)}" for d in res["deleted"]]
        lines += [f"kept {d['target']}: {disk_lines(d)} ({d['reason']})" for d in res["kept"]]
        lines += [f"no longer deleted since the preview {d['target']}: {d['path']} "
                  f"({d['reason']})" for d in res["skipped"]]
        lines += [f"left in place {d['target']}: {disk_lines(d)} ({d['error']})"
                  for d in res["leftovers"]]
        lines += unreadable_lines(res["unreadable"], res["acknowledged_unreadable"])
        lines += [f"note: {n}" for n in res["notes"]]
        emit(args, {"ok": True, "host": host.name, "dry_run": False, **res},
             plan_text(f"{name} deleted from {host.name}", [], lines))
    return EXIT_OK


def required_digest(args, what):
    """Empreinte d'aperçu (`--expect-delete`) : obligatoire pour toute
    suppression réelle, l'aperçu (`--dry-run`) la montre. Refus d'usage
    (code 2) avant tout appel à l'hôte."""
    digest = checked(ko.check_digest, args.expect_delete)
    if digest is None and not args.dry_run:
        raise UsageError(f"--expect-delete is required to delete {what}: run the same "
                         "command with --dry-run first, then pass the digest it shows")
    return digest


def unreadable_lines(unreadable, acknowledged):
    """Fichiers illisibles par le compte SSH qui ont empêché la preuve."""
    left = [p for p in unreadable if p not in set(acknowledged)]
    if not left:
        return []
    return [f"unreadable by the SSH user: {p}" for p in left] + [
        "disks are kept while these files cannot be read; if you are sure none of them "
        "uses a disk of this VM, add --acknowledge-unreadable=PATH for each"]


# ---------------------------------------------------------------------------
# Prochaine version KVM : créer, modifier, gestes à chaud, images, volumes,
# pools, réseaux (bin/lib/kvm_create.py, kvm_edit.py, kvm_storage.py,
# kvm_net.py)
# ---------------------------------------------------------------------------

CREATE_TIMEOUT = 3600
EDIT_TIMEOUT = 600
# taille maximale d'un fichier de spec (création, disque, carte, cloud-init)
SPEC_MAX = 1 << 20
# imbrication maximale d'un spec JSON (un VmSpec en compte moins de 6)
SPEC_MAX_DEPTH = 32
# taille maximale d'un user-data ou network-data donné par --user-data=
CLOUDINIT_MAX = 1 << 20
DOWNLOAD_TIMEOUT = 60
DOWNLOAD_MAX_REDIRECTS = 5
DOWNLOAD_CHUNK = 1 << 20
# marge laissée libre sur le disque de la console pendant un téléchargement
DOWNLOAD_MARGIN = 256 << 20
# taille maximale d'un téléchargement par défaut (--max-size)
DOWNLOAD_MAX_SIZE = 64 << 30
AUTHOR_MAX = 128
SIZE_UNITS = {"": 1, "B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
# participe passé des gestes de pool et de réseau (annulation)
OBJECT_VERBS = {"define": "defined", "start": "started", "stop": "stopped",
                "refresh": "refreshed", "autostart": "changed", "delete": "deleted"}

POPEN = subprocess.Popen


def load_host_next(state, name):
    """VirshHost d'un hôte déclaré, avec le lanceur de flux injectable (tests)."""
    host = load_host(state, name)
    host.popen = POPEN
    return host


def deadline_cancel(timeout):
    """Rappel `cancel()` des bibliothèques : vrai une fois l'échéance passée
    (consulté entre deux étapes et pendant un téléversement)."""
    end = CLOCK() + timeout
    return lambda: CLOCK() > end


class _NotFinite(Exception):
    """Nombre NaN ou infini dans un spec JSON."""


def _no_constant(_name):
    # NaN, Infinity, -Infinity : acceptés par json.loads, refusés ici
    raise _NotFinite()


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):               # 1e400 devient inf
        raise _NotFinite()
    return value


def json_depth(text):
    """Profondeur d'imbrication des objets et listes d'un texte JSON, comptée
    AVANT l'analyse (json.loads irait jusqu'à RecursionError) ; les crochets
    des chaînes ne comptent pas."""
    depth = deepest = 0
    in_str = esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif ch in "]}":
            depth -= 1
    return deepest


def read_spec(path, what="spec"):
    """Spec JSON lu depuis un fichier (ou `-` : entrée standard). Le contenu
    n'est jamais recopié dans une sortie (il peut porter un user-data), ni le
    chemin du fichier."""
    if not isinstance(path, str) or not path:
        raise UsageError(f"--spec-file: give the {what} file")
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(SPEC_MAX + 1)
        else:
            with open(path, "rb") as f:
                raw = f.read(SPEC_MAX + 1)
    except OSError as e:
        raise UsageError(f"cannot read the {what} file ({errno.errorcode.get(e.errno, 'error')})") \
            from None
    if len(raw) > SPEC_MAX:
        raise UsageError(f"the {what} file is larger than {SPEC_MAX} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise UsageError(f"the {what} file is not valid JSON") from None
    if json_depth(text) > SPEC_MAX_DEPTH:
        raise UsageError(f"the {what} file nests deeper than {SPEC_MAX_DEPTH} levels")
    try:
        data = json.loads(text, parse_constant=_no_constant, parse_float=_finite_float)
    except _NotFinite:
        raise UsageError(f"the {what} file holds a number that is not finite (NaN or "
                         "infinity)") from None
    except (ValueError, RecursionError):
        raise UsageError(f"the {what} file is not valid JSON") from None
    if not isinstance(data, dict):
        raise UsageError(f"the {what} must be a JSON object")
    return data


def read_text_file(path, what):
    """user-data ou network-data donné par chemin (usage manuel)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(CLOUDINIT_MAX + 1)
    except OSError as e:
        raise UsageError(f"cannot read the {what} file ({errno.errorcode.get(e.errno, 'error')})") \
            from None
    if len(raw) > CLOUDINIT_MAX:
        raise UsageError(f"the {what} file is larger than {CLOUDINIT_MAX} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise UsageError(f"the {what} file is not UTF-8 text") from None


def parse_size(text, what="size"):
    """Octets : entier, ou suffixe binaire K, M, G, T (`20G`)."""
    t = (text or "").strip().upper().removesuffix("IB").removesuffix("B") \
        if isinstance(text, str) else ""
    unit = t[-1:] if t[-1:] in SIZE_UNITS else ""
    digits = t[:-1] if unit else t
    if not (digits.isascii() and digits.isdigit()) or int(digits) <= 0:
        raise UsageError(f"invalid {what}: a positive number of bytes, or with K, M, G, T")
    return int(digits) * SIZE_UNITS[unit]


def _pool_vol(text, what):
    pool, sep, vol = (text or "").partition("/")
    if not sep or not pool or not vol:
        raise UsageError(f"--disk={what}: POOL/VOLUME expected")
    return pool, vol


def disk_from_flag(text):
    """`--disk=` : blank:POOL:SIZE, image:POOL/IMAGE[:SIZE][:copy],
    volume:POOL/VOLUME, iso:POOL/VOLUME. Un nom de pool ne contient ni `:`
    ni `/` (POOL_NAME_RE), la découpe est donc sans ambiguïté."""
    kind, _, rest = (text or "").partition(":")
    if kind == "blank":
        pool, sep, size = rest.partition(":")
        if not sep:
            raise UsageError("--disk=blank:POOL:SIZE expected")
        return {"source": {"kind": "blank", "pool": pool, "size_bytes": parse_size(size)}}
    if kind == "image":
        parts = rest.split(":")
        pool, vol = _pool_vol(parts[0], "image")
        src = {"kind": "image", "pool": pool, "image": vol}
        for extra in parts[1:]:
            if extra == "copy":
                src["linked"] = False
            else:
                src["size_bytes"] = parse_size(extra)
        return {"source": src}
    if kind in ("volume", "iso"):
        pool, vol = _pool_vol(rest, kind)
        return {"source": {"kind": kind, "pool": pool, "volume": vol}}
    raise UsageError("--disk: blank:POOL:SIZE, image:POOL/IMAGE[:SIZE][:copy], "
                     "volume:POOL/VOLUME or iso:POOL/VOLUME")


def nic_from_flag(text):
    """`--nic=` : network:NAME, bridge:NAME, direct:IFACE[:MODE]."""
    kind, _, rest = (text or "").partition(":")
    if kind not in ("network", "bridge", "direct") or not rest:
        raise UsageError("--nic: network:NAME, bridge:NAME or direct:IFACE[:MODE]")
    nic = {"kind": kind, "source": rest}
    if kind == "direct" and ":" in rest:
        nic["source"], nic["mode"] = rest.split(":", 1)
    return nic


CREATE_FLAGS = ("name", "instances", "vcpus", "memory_mib", "os", "machine", "firmware",
                "title", "description", "disk", "nic", "user_data", "network_data",
                "hostname", "seed_pool")
CREATE_SWITCHES = ("start", "autostart", "secure_boot", "tpm", "vnc_password")


def spec_from_flags(args):
    """Spec de création construit depuis les options (usage manuel ; la
    console passe toujours --spec-file)."""
    if not args.name:
        raise UsageError("give --spec-file, or at least --name with the options of the VM")
    spec = {"name": args.name}
    if args.instances is not None:
        spec["instances"] = args.instances
    for flag in ("os", "machine", "firmware", "title", "description"):
        if getattr(args, flag) is not None:
            spec[flag] = getattr(args, flag)
    if args.vcpus is not None:
        spec["cpu"] = {"vcpus": args.vcpus}
    if args.memory_mib is not None:
        spec["memory"] = {"current_mib": args.memory_mib}
    spec["disks"] = [disk_from_flag(d) for d in args.disk or []]
    spec["nics"] = [nic_from_flag(n) for n in args.nic or []]
    for flag in ("start", "autostart", "secure_boot", "tpm"):
        if getattr(args, flag):
            spec[flag] = True
    if args.vnc_password:
        spec["display"] = {"vnc": True, "password": True}
    ci = {}
    if args.user_data:
        ci["user_data"] = read_text_file(args.user_data, "user-data")
    if args.network_data:
        ci["network_data"] = read_text_file(args.network_data, "network-data")
    if args.hostname:
        ci["hostname"] = args.hostname
    if args.seed_pool:
        ci["seed_pool"] = args.seed_pool
    if ci:
        spec["cloudinit"] = ci
    return spec


def create_spec(args):
    flags = [f for f in CREATE_FLAGS if getattr(args, f, None) not in (None, [])]
    flags += [f for f in CREATE_SWITCHES if getattr(args, f, False)]
    if args.spec_file is not None:
        if flags:
            raise UsageError("give either --spec-file or the options of the VM, not both "
                             f"(--{flags[0].replace('_', '-')})")
        return read_spec(args.spec_file)
    return spec_from_flags(args)


def spec_pools(spec):
    """Pools où une création écrit (disques, images, seed) : ils sont tenus
    en partage pendant la création, un retrait de pool attend."""
    pools = set()
    for d in spec.get("disks") or []:
        src = (d or {}).get("source") if isinstance(d, dict) else None
        if isinstance(src, dict):
            for k in ("pool", "image_pool"):
                if isinstance(src.get(k), str):
                    pools.add(src[k])
    ci = spec.get("cloudinit")
    if isinstance(ci, dict) and isinstance(ci.get("seed_pool"), str):
        pools.add(ci["seed_pool"])
    return sorted(pools)


@contextlib.contextmanager
def object_lock(state, host, kind, *names, shared=False):
    """Verrou d'un objet de l'hôte (pool, volume, réseau), pris sans
    attendre. `shared` : plusieurs créations peuvent écrire dans le même
    pool, mais un retrait ou un arrêt du pool (exclusif) n'y entre pas."""
    d = kvm_decl.decl_dir(state) / "locks"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fds = []
    try:
        for name in sorted(set(names)):
            digest = hashlib.sha256(name.encode()).hexdigest()[:16]
            fd = os.open(d / f"{host}-{kind}-{digest}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            fds.append(fd)
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UsageError(f"another operation on {kind} {name} is in progress on "
                                 f"{host}: try again later", kind="busy") from None
        yield
    finally:
        for fd in fds:
            os.close(fd)


def lib_error(prog, e, data):
    """Exception à lever pour une erreur de bibliothèque : refus d'entrée
    (OpError `invalid`) en erreur d'usage (code 2), autres erreurs en
    Failure (code 1) qui garde `refusals` et `leftovers`, échéance
    (kh.Cancelled levée par le rappel `cancel`) en kind `timeout`."""
    if isinstance(e, ko.OpError) and e.kind == "invalid":
        prog.fail(e.message)
        return UsageError(e.message)
    if isinstance(e, (ko.OpError, kh.KvmError)):
        return op_failure(prog, e, data)
    msg = "the operation did not finish within --timeout"
    prog.fail(msg)
    extra = dict(data)
    if "leftovers" in extra:
        extra["leftovers"] = list(getattr(e, "leftovers", None) or [])
    return Failure(msg, kind="timeout", data=extra)


LIB_ERRORS = (ko.OpError, kh.KvmError, kh.Cancelled)


def lib_call(prog, data, fn):
    try:
        return fn()
    except LIB_ERRORS as e:
        raise lib_error(prog, e, data) from None


def change_lines(changes):
    return [f"{c['field']}{' ' + c['target'] if c.get('target') else ''}: {c['applied']}"
            + (f" ({c['reason']})" if c.get("reason") else "") for c in changes]


def leftover_lines(left):
    return [f"left on the host: {describe_leftover(x)}" for x in left or []]


def after_edit(host, name):
    """Relecture après une modification : état, empreinte, changements en
    attente (la fenêtre affiche « restart needed » d'après `pending`)."""
    try:
        form = ke.vm_form(host, name)
    except (ko.OpError, kh.KvmError) as e:
        step("verify", "warn", f"cannot read {name} back: {e.message}")
        return {"state": None, "config_digest": None, "pending": None}
    step("verify", "done", f"{form['state']}"
         + (f", restart needed for: {', '.join(form['pending'])}" if form["pending"] else ""))
    return {"state": form["state"], "config_digest": form["config_digest"],
            "pending": form["pending"]}


# --- host caps -------------------------------------------------------------------

def cmd_host_caps(args):
    host = load_host_next(state_of(args), args.host)
    if args.machine is not None and args.machine not in kvm_caps.MACHINE_FAMILIES:
        raise UsageError(f"--machine: one of {', '.join(kvm_caps.MACHINE_FAMILIES)}")
    prog = Progress("caps")
    prog("caps", "running", f"capabilities of {host.name}")
    options = lib_call(prog, {}, lambda: kc.host_options(host))
    if args.machine:
        options = {**options,
                   "by_machine": {k: v for k, v in options["by_machine"].items()
                                  if k == args.machine},
                   "disabled": {k: v for k, v in options["disabled"].items()
                                if k == args.machine}}
    fams = sorted(options["by_machine"])
    prog("caps", "done", f"machine types: {', '.join(fams) or 'none'}")
    lines = [f"{host.name}: {options.get('arch')}, default machine {options.get('default_machine')}"]
    for fam in fams:
        o = options["by_machine"][fam]
        lines.append(f"  {fam}: firmware {', '.join(o.get('firmware') or [])}; disk buses "
                     f"{', '.join(o.get('disk_buses') or [])}; video "
                     f"{', '.join(o.get('video_models') or [])}")
        for opt, reason in sorted((options["disabled"].get(fam) or {}).items()):
            lines.append(f"    {opt}: {reason}")
    emit(args, {"ok": True, "host": host.name, "machine": args.machine, "options": options},
         "\n".join(lines))
    return EXIT_OK


# --- vm create -------------------------------------------------------------------

def cmd_vm_create(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    spec = create_spec(args)
    timeout = positive_timeout(args.timeout, CREATE_TIMEOUT)
    names = [s.get("name") for s in checked(kc.instance_specs, spec)]
    if not all(isinstance(n, str) and n for n in names):
        raise UsageError("the spec needs a name")
    for n in names:
        checked(ko.check_new_vm_name, n)
    data = {"host": host.name, "refusals": [], "created": [], "failed": None, "leftovers": []}
    prog = Progress()
    if args.dry_run:
        prog("plan", "running", f"{names[0]} (dry run)")
        plan = lib_call(prog, data, lambda: kc.create_plan(host, spec))
        pub = kc.public_plan(plan)
        prog("plan", "warn" if pub["refusals"] else "done",
             "; ".join(pub["refusals"]) or f"{len(pub['instances'])} VM(s)")
        lines = []
        for i in pub["instances"]:
            lines.append(f"{i['name']}:")
            lines += [f"  volume {v['target']}: {v['pool']}/{v['volume']} ({v['kind']}, "
                      f"{v['capacity']} bytes)" for v in i["volumes"]]
            if i["seed"]:
                lines.append(f"  cloud-init seed: {i['seed']['pool']}/{i['seed']['volume']}")
        lines += [f"warning: {w}" for w in pub["warnings"]]
        emit(args, {"ok": True, "host": host.name, "dry_run": True,
                    "possible": not pub["refusals"], "plan": pub},
             plan_text(f"create {', '.join(names)} on {host.name} (dry run)", pub["refusals"],
                       lines))
        return EXIT_OK
    with contextlib.ExitStack() as locks:
        locks.enter_context(vm_lock(state, host.name, *names))
        locks.enter_context(object_lock(state, host.name, "pool", *spec_pools(spec),
                                        shared=True))
        locks.enter_context(noting_progress(prog))

        def hold(volumes, networks):
            # plan accepté, rien d'écrit : chaque volume à créer (disques,
            # seed) en exclusif, contre un retrait ou un envoi du même nom ;
            # chaque réseau cité en partage, contre son arrêt ou son retrait
            locks.enter_context(object_lock(state, host.name, "volume",
                                            *[f"{p}/{v}" for p, v in volumes]))
            locks.enter_context(object_lock(state, host.name, "network", *networks,
                                            shared=True))
        t0 = CLOCK()
        try:
            res = kc.create_vm(host, spec, progress=prog, cancel=deadline_cancel(timeout),
                               before_write=hold)
        except UsageError:
            raise                              # verrou occupé : rien n'a été écrit
        except LIB_ERRORS as e:
            data.update(created=list(getattr(e, "created", None) or []),
                        failed=getattr(e, "failed", None))
            err = lib_error(prog, e, data)
            if isinstance(err, Failure):
                err.data["leftovers"] = list(getattr(e, "leftovers", None) or [])
            rollback_step(e)
            raise err from None
        except Exception as e:                 # noqa: BLE001 : restes dits, sans trace
            msg = f"unexpected internal error ({type(e).__name__})"
            prog.fail(msg)
            rollback_step(e)
            raise Failure(msg, data={**data, "created": list(getattr(e, "created", None) or []),
                                     "failed": getattr(e, "failed", None),
                                     "leftovers": list(getattr(e, "leftovers", None) or [])}) \
                from None
        elapsed = CLOCK() - t0
    lines = [f"{c['name']}: {c['state']}, uuid {c['uuid']}"
             + (f", volumes {', '.join(c['volumes'])}" if c["volumes"] else "")
             + (f", cloud-init seed {c['seed']}" if c["seed"] else "") for c in res["created"]]
    lines += [f"warning: {w}" for w in res["warnings"]]
    emit(args, {"ok": True, "host": host.name, "dry_run": False, "elapsed_s": round(elapsed, 1),
                **res}, plan_text(f"created on {host.name}", [], lines))
    return EXIT_OK


# --- vm edit et gestes à chaud ------------------------------------------------------

def edit_failure(prog, e, data):
    """Échec d'une modification : ce qui a été fait, échoué, pas tenté."""
    err = lib_error(prog, e, data)
    if isinstance(err, Failure):
        err.data["changes"] = list(getattr(e, "changes", None) or (
            e.data.get("changes") if isinstance(e, ko.OpError) else None) or [])
        err.data["leftovers"] = list(getattr(e, "leftovers", None) or [])
    return err


def cmd_vm_edit(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    name = check_vm_name(args.name)
    spec = read_spec(args.spec_file)
    expect = checked(ko.check_digest, args.expect_config)
    if expect is None and not args.dry_run:
        raise UsageError("--expect-config: give the config_digest read when the form was opened "
                         "(a dry run shows it)")
    timeout = positive_timeout(args.timeout, EDIT_TIMEOUT)
    data = {"host": host.name, "name": name, "refusals": [], "changes": [], "leftovers": []}
    prog = Progress()
    if args.dry_run:
        res = lib_call(prog, data, lambda: ke.edit_vm(host, name, spec, expect, progress=prog,
                                                      dry_run=True))
        plan = {k: res.get(k) for k in ("name", "state", "persistent", "config_digest",
                                         "changes", "refusals", "empty")}
        emit(args, {"ok": True, "host": host.name, "name": name, "dry_run": True,
                    "possible": not plan["refusals"], "plan": plan},
             plan_text(f"edit {name} on {host.name} (dry run), config {plan['config_digest']}",
                       plan["refusals"], change_lines(plan["changes"])))
        return EXIT_OK
    new_name = spec.get("name") if isinstance(spec.get("name"), str) else name
    with vm_lock(state, host.name, name, new_name), noting_progress(prog):
        try:
            res = ke.edit_vm(host, name, spec, expect, progress=prog,
                             cancel=deadline_cancel(timeout), sleep=SLEEP)
        except LIB_ERRORS as e:
            raise edit_failure(prog, e, data) from None
        after = after_edit(host, res.get("name") or name)
    changes = res.get("changes") or []
    out = {"ok": True, "host": host.name, "dry_run": False, "name": res.get("name") or name,
           "previous_name": name, "changes": changes, "empty": not changes,
           "defined": bool(res.get("defined")), "renamed": bool(res.get("renamed")),
           "leftovers": list(res.get("leftovers") or []),
           "config_digest_before": res.get("config_digest"), "after": after,
           "restart_required": bool(after.get("pending"))}
    lines = change_lines(changes) or ["nothing to change"]
    lines += leftover_lines(out["leftovers"])
    if out["restart_required"]:
        lines.append("restart needed (shut down, then start: a reboot inside the guest is not "
                     "enough) for: " + ", ".join(after["pending"]))
    emit(args, out, plan_text(f"{out['name']} on {host.name}", [], lines))
    return EXIT_OK


def hot_gesture(args, gesture, fn, pools=()):
    """Un geste à chaud : verrou de la VM (et des `pools` en partage),
    étapes, résultat par changement, relecture (état, changements en
    attente)."""
    state = state_of(args)
    host = load_host_next(state, args.host)
    name = check_vm_name(args.name)
    data = {"host": host.name, "name": name, "gesture": gesture, "refusals": [],
            "changes": [], "leftovers": []}
    prog = Progress()
    def chained(sid, status, msg=""):
        # la première étape de la bibliothèque clôt l'étape `plan`
        if prog.sid == "plan" and prog.status == "running":
            prog("plan", "done", gesture)
        prog(sid, status, msg)
    with vm_lock(state, host.name, name), \
            object_lock(state, host.name, "pool", *pools, shared=True), \
            noting_progress(prog):
        prog("plan", "running", f"{gesture} on {name}")
        try:
            res = fn(host, name, chained)
        except LIB_ERRORS as e:
            raise edit_failure(prog, e, data) from None
        if prog.sid == "plan":
            prog("plan", "done", "nothing to change" if not res.get("changes") else gesture)
        after = after_edit(host, name)
    out = {"ok": True, "host": host.name, "name": name, "gesture": gesture,
           "changes": res.get("changes") or [], "defined": bool(res.get("defined")),
           "leftovers": list(res.get("leftovers") or []), "after": after,
           "restart_required": bool(after.get("pending"))}
    lines = change_lines(out["changes"]) or ["nothing to change"]
    emit(args, out, plan_text(f"{gesture} on {name} ({host.name})", [], lines))
    return EXIT_OK


def cmd_vm_cpumem(args):
    if args.vcpus is None and args.memory_mib is None:
        raise UsageError("give --vcpus and/or --memory-mib")
    for v, flag in ((args.vcpus, "--vcpus"), (args.memory_mib, "--memory-mib")):
        if v is not None and v < 1:
            raise UsageError(f"{flag} must be positive")
    return hot_gesture(args, "cpumem", lambda h, n, p: ke.set_cpu_memory(
        h, n, vcpus=args.vcpus, memory_mib=args.memory_mib, progress=p, sleep=SLEEP))


def cmd_vm_add_volume(args):
    disk = read_spec(args.spec_file, "disk")
    # pools où le disque peut être créé (ou lu) : tenus en partage
    return hot_gesture(args, "add-volume", lambda h, n, p: ke.attach_disk(
        h, n, disk, progress=p, sleep=SLEEP), pools=spec_pools({"disks": [disk]}))


def check_target(target):
    if not isinstance(target, str) or not re.fullmatch(r"[a-z]{2,3}[a-z0-9]{1,4}", target):
        raise UsageError("--target: a disk target such as vdb or sda")
    return target


def cmd_vm_remove_volume(args):
    target = check_target(args.target)
    return hot_gesture(args, "remove-volume", lambda h, n, p: ke.detach_disk(
        h, n, target, progress=p, sleep=SLEEP))


def cmd_vm_insert_cdrom(args):
    target = check_target(args.target)
    pool = checked(ks.check_pool_name, args.pool)
    volume = checked(ko.check_vm_name, args.volume)
    return hot_gesture(args, "insert-cdrom", lambda h, n, p: ke.insert_media(
        h, n, target, pool, volume, progress=p, sleep=SLEEP))


def cmd_vm_eject_image(args):
    target = check_target(args.target)
    return hot_gesture(args, "eject-image", lambda h, n, p: ke.eject_media(
        h, n, target, progress=p, sleep=SLEEP))


def cmd_vm_add_nic(args):
    nic = read_spec(args.spec_file, "network card")
    return hot_gesture(args, "add-nic", lambda h, n, p: ke.attach_nic(
        h, n, nic, progress=p, sleep=SLEEP))


MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")


def cmd_vm_remove_nic(args):
    if not isinstance(args.mac, str) or not MAC_RE.match(args.mac):
        raise UsageError("--mac: a MAC address such as 52:54:00:12:34:56")
    return hot_gesture(args, "remove-nic", lambda h, n, p: ke.detach_nic(
        h, n, args.mac, progress=p, sleep=SLEEP))


def cmd_vm_cloudinit(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    name = check_vm_name(args.name)
    ci = read_spec(args.spec_file, "cloud-init")
    unknown = sorted(set(ci) - {"user_data", "network_data", "hostname"})
    if unknown:
        raise UsageError(f"cloud-init: unknown field(s) {', '.join(unknown)}")
    for k in ("user_data", "network_data", "hostname"):
        if ci.get(k) is not None and not isinstance(ci[k], str):
            raise UsageError(f"cloud-init: {k} must be a string")
    ack = args.acknowledge_unreadable
    checked(ko.check_acknowledged, ack)
    data = {"host": host.name, "name": name, "refusals": [], "leftovers": []}
    prog = Progress("seed")
    with contextlib.ExitStack() as locks:
        locks.enter_context(vm_lock(state, host.name, name))
        locks.enter_context(noting_progress(prog))

        def hold(pool, volume):
            # pool du seed en partage (un retrait ou un arrêt du pool attend),
            # nouveau seed en exclusif
            locks.enter_context(object_lock(state, host.name, "pool", pool, shared=True))
            locks.enter_context(object_lock(state, host.name, "volume", f"{pool}/{volume}"))
        prog("seed", "running", f"new cloud-init seed for {name}"
             + (" (cloud-init runs again at next boot)" if args.rerun else ""))
        res = lib_call(prog, data, lambda: ke.regenerate_seed(
            host, name, ci.get("user_data"), ci.get("network_data"), rerun=args.rerun,
            hostname=ci.get("hostname"), acknowledged_unreadable=ack, before_write=hold))
        prog("seed", "done", f"{res['seed']['pool']}/{res['seed']['volume']} on "
             f"{res['seed']['target']}, read by the guest at its next start")
        if res["previous"]:
            prog("cleanup", "done" if res["removed"] else "warn",
                 f"previous seed {res['previous']['pool']}/{res['previous']['volume']} "
                 + ("removed" if res["removed"] else f"kept: {res['kept_reason']}"))
        after = after_edit(host, name)
    out = {"ok": True, "host": host.name, "name": name, **res, "after": after,
           "user_data": bool(ci.get("user_data")), "user_data_bytes": len(ci.get("user_data") or ""),
           "network_data": bool(ci.get("network_data"))}
    lines = [f"seed {res['seed']['pool']}/{res['seed']['volume']} on {res['seed']['target']}"]
    if res["previous"] and not res["removed"]:
        lines.append(f"previous seed kept: {res['kept_reason']}")
    emit(args, out, plan_text(f"cloud-init of {name} on {host.name}", [], lines))
    return EXIT_OK


# --- images et volumes -----------------------------------------------------------------

def check_author(author):
    if author is None:
        return None
    if not isinstance(author, str) or not author or len(author) > AUTHOR_MAX or any(
            ord(c) < 32 or ord(c) == 127 for c in author):
        raise UsageError("--author: a name without control characters, "
                         f"{AUTHOR_MAX} characters at most")
    return author


def cmd_image_list(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    prog = Progress("list")
    prog("list", "running", f"images of {host.name}")
    res = lib_call(prog, {}, lambda: ks.list_images(host, host.decl,
                                                    ks.load_notes(state, host.name),
                                                    fresh=args.fresh))
    prog("list", "done", f"{len(res['images'])} image(s)")
    head = ("POOL", "VOLUME", "FORMAT", "SIZE", "ISO", "BASE OF", "SHA-256")
    rows = [head] + [(i["pool"], i["volume"], i["format"] or "-", str(i["capacity"] or "-"),
                      "yes" if i["iso"] else "", str(len(i["backing_users"])),
                      (i["sha256"] or "")[:12]) for i in res["images"]]
    widths = [max(len(r[c]) for r in rows) for c in range(len(head))]
    text = "\n".join("  ".join(v.ljust(widths[c]) for c, v in enumerate(r)).rstrip()
                     for r in rows)
    if res["missing_pools"]:
        text += "\nimage pools not found: " + ", ".join(res["missing_pools"])
    emit(args, {"ok": True, "host": host.name, **res}, text)
    return EXIT_OK


def cmd_image_verify(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    pool = checked(ks.check_pool_name, args.pool)
    volume = checked(ko.check_vm_name, args.volume)
    expected = args.sha256 or (ks.load_notes(state, host.name).get(f"{pool}/{volume}")
                               or {}).get("sha256")
    if expected is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise UsageError("--sha256: 64 hexadecimal characters")
    prog = Progress("verify")
    prog("verify", "running", f"SHA-256 of {pool}/{volume} on the host")
    res = lib_call(prog, {}, lambda: ks.verify_image(host, pool, volume, expected))
    out = {"host": host.name, "pool": pool, "volume": volume, **res}
    if res["match"] is False:
        msg = f"{pool}/{volume} does not match the expected SHA-256"
        prog("verify", "error", msg)
        raise Failure(msg, kind="mismatch", data=out)
    if res["skipped"]:
        prog("verify", "warn", res["skipped"])
    else:
        prog("verify", "done", res["sha256"] + (" (matches)" if res["match"] else ""))
    emit(args, {"ok": True, **out},
         f"{pool}/{volume}: " + (res["skipped"] or res["sha256"]
                                 + (" matches" if res["match"] else "")))
    return EXIT_OK


def staging_dir(state):
    """Répertoire des fichiers téléchargés avant leur envoi (0700)."""
    d = Path(state) / ks.NOTES_DIR / "staging"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    return d


def public_url(url):
    """URL sans identifiants, requête ni fragment (une URL signée porte son
    jeton dans la requête) : c'est elle qui est notée et affichée. L'URL a
    été contrôlée par check_url (port valide)."""
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{p.port}" if p.port else "")
    return urllib.parse.urlunsplit((p.scheme, netloc, p.path, "", ""))


def check_url(url):
    """URL http(s) avec un hôte et un port valide, sans caractère de
    contrôle ni espace ; rend le message de refus, ou None."""
    if not isinstance(url, str) or not url or any(ord(c) < 33 or ord(c) == 127 for c in url):
        return "an http or https URL"
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port
    except ValueError:
        return "an http or https URL with a valid port (1 to 65535)"
    if p.scheme not in ("http", "https") or not p.hostname:
        return "an http or https URL"
    if port == 0:
        return "an http or https URL with a valid port (1 to 65535)"
    return None


def parse_cidrs(items):
    """--allow-private=CIDR (répétable) : plages privées autorisées."""
    nets = []
    for item in items or []:
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise UsageError(f"--allow-private: {item!r} is not an address range such as "
                             "10.0.0.0/8") from None
    return nets


def address_allowed(addr, allow=()):
    """Adresse joignable par un téléchargement : publique (globale, ni
    multicast), ou dans une plage de --allow-private. Refusées par défaut :
    boucle locale, lien local (169.254.169.254, adresse des métadonnées des
    clouds, comprise), privées, partagées (100.64/10), réservées."""
    ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if any(ip.version == n.version and ip in n for n in allow):
        return True
    return ip.is_global and not ip.is_multicast


def _guarded_connection(base):
    """Classe de connexion http.client qui résout le nom elle-même, refuse
    toute adresse non autorisée AVANT de se connecter, puis se connecte à
    l'adresse vérifiée (pas de seconde résolution : un changement de DNS
    entre le contrôle et la connexion ne passe pas)."""
    class Guarded(base):
        def __init__(self, *a, allow=(), **kw):
            super().__init__(*a, **kw)
            self._allow = allow
            self._create_connection = self._connect_checked

        def _connect_checked(self, address, timeout=None, source_address=None, *a, **kw):
            host, port = address
            try:
                infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except socket.gaierror as e:
                raise OSError(f"cannot resolve {host} ({e.strerror})") from None
            bad = sorted({sa[0] for *_, sa in infos
                          if not address_allowed(sa[0], self._allow)})
            if bad:
                raise Failure(f"{host} resolves to a loopback, link-local, private or reserved "
                              f"address ({', '.join(bad)}): refused; if this server is "
                              "intended, add --allow-private=CIDR", kind="refused")
            last = None
            for family, socktype, proto, _canon, sa in infos:
                sock = socket.socket(family, socktype, proto)
                try:
                    if timeout is not None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                        sock.settimeout(timeout)
                    if source_address:
                        sock.bind(source_address)
                    sock.connect(sa)
                    return sock
                except OSError as e:
                    last = e
                    sock.close()
            raise last or OSError(f"no address for {host}")
    return Guarded


_GuardedHTTP = _guarded_connection(http.client.HTTPConnection)
_GuardedHTTPS = _guarded_connection(http.client.HTTPSConnection)


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, allow):
        super().__init__()
        self._allow = allow

    def http_open(self, req):
        return self.do_open(functools.partial(_GuardedHTTP, allow=self._allow), req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, allow):
        super().__init__()
        self._allow = allow

    def https_open(self, req):
        return self.do_open(functools.partial(_GuardedHTTPS, allow=self._allow), req,
                            context=self._context)


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Redirections bornées, vers http ou https seulement, port valide ;
    l'adresse de chaque étape est contrôlée à sa connexion."""
    max_redirections = DOWNLOAD_MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if check_url(newurl) is not None:
            raise Failure("the download was redirected outside http/https or to an invalid "
                          "URL: refused", kind="refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_opener(allow=()):
    """Ouvreur urllib du téléchargement : sans mandataire d'environnement
    (l'adresse contrôlée est celle du serveur joint), sans ftp ni file,
    adresses contrôlées à chaque connexion, redirections bornées."""
    opener = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler({}), urllib.request.UnknownHandler(),
              urllib.request.HTTPDefaultErrorHandler(), _Redirects(),
              urllib.request.HTTPErrorProcessor(), _GuardedHTTPHandler(allow),
              _GuardedHTTPSHandler(allow)):
        opener.add_handler(h)
    return opener


def download_url(url, dest, progress=None, opener=None, max_size=None, allow=()):
    """Télécharge `url` (http/https) dans un fichier 0600 de `dest`, en flux,
    SHA-256 calculé au passage. Adresses du serveur et de chaque redirection
    contrôlées (address_allowed, `allow` : plages autorisées). Taille
    plafonnée à `max_size` (annoncée et reçue) et contrôlée contre la place
    libre (marge DOWNLOAD_MARGIN). Rend (chemin, taille, sha256). Le fichier
    partiel est supprimé sur toute erreur ou annulation."""
    max_size = DOWNLOAD_MAX_SIZE if max_size is None else max_size
    opener = opener or download_opener(allow)
    try:
        resp = opener.open(urllib.request.Request(url, headers={"User-Agent": "harvester-ops"}),
                           timeout=DOWNLOAD_TIMEOUT)
    except (UsageError, Failure):
        raise
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:                # redirection hors http(s), ou trop nombreuses
            raise Failure("the download was redirected outside http/https or too many times: "
                          "refused", kind="refused") from None
        raise Failure(f"download refused by the server (HTTP {e.code})", kind="failed") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        reason = getattr(e, "reason", None) or type(e).__name__
        raise Failure(f"download failed ({reason})", kind="unreachable") from None
    fd, tmp = tempfile.mkstemp(dir=dest, prefix="dl-")
    try:
        with resp, os.fdopen(fd, "wb") as f:
            length = resp.headers.get("Content-Length")
            total = int(length) if length and length.isascii() and length.isdigit() else None
            if total is not None and total > max_size:
                raise Failure(f"the file is larger than --max-size ({total} bytes announced, "
                              f"{max_size} allowed)", kind="refused")
            free = shutil.disk_usage(dest).free
            if total is not None and total + DOWNLOAD_MARGIN > free:
                raise Failure(f"not enough space on the console for {total} bytes "
                              f"({free} free)", kind="refused")
            h, got = hashlib.sha256(), 0
            while True:
                block = resp.read(DOWNLOAD_CHUNK)
                if not block:
                    break
                got += len(block)
                if total is not None and got > total:
                    raise Failure("the server sent more than the size it announced",
                                  kind="failed")
                if got > max_size:
                    raise Failure(f"the file is larger than --max-size ({max_size} bytes)",
                                  kind="refused")
                if got % (64 * DOWNLOAD_CHUNK) < len(block) and \
                        shutil.disk_usage(dest).free < DOWNLOAD_MARGIN:
                    raise Failure("the console is running out of space", kind="refused")
                h.update(block)
                f.write(block)
                if progress:
                    progress(got, total)
            if total is not None and got != total:
                raise Failure(f"the download stopped after {got} of {total} bytes",
                              kind="failed")
        return tmp, got, h.hexdigest()
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _upload_progress(phase):
    """Rappel de progression d'un envoi : débit et temps restant
    (PROGRESS_EVENT, comme les transferts de VM)."""
    box = {}

    def cb(done, total=None):
        if "p" not in box:
            box["p"] = vtp.Progress(vtp.emit_line, phase, total or 0, every=1.0)
        if total and not box["p"].total:
            box["p"].total = total
        box["p"].update(done)
    cb.box = box
    return cb


def _limit_bps(args):
    if args.limit_mbps is None:
        return None
    if not math.isfinite(args.limit_mbps) or args.limit_mbps <= 0:
        raise UsageError("--limit-mbps must be a positive finite number")
    return args.limit_mbps * 1_000_000 / 8


def do_upload(args, state, host, pool, name, path, sha, url=None):
    """Envoi d'un fichier local comme volume (kvm_storage.upload_volume),
    avec progression, plafond de débit, contrôle sur l'hôte."""
    data = {"host": host.name, "pool": pool, "volume": name, "refusals": [], "leftovers": []}
    prog = Progress("check")
    size = os.path.getsize(path)
    progress = _upload_progress("upload")
    with object_lock(state, host.name, "pool", pool, shared=True), \
            object_lock(state, host.name, "volume", f"{pool}/{name}"):
        res = lib_call(prog, data, lambda: ks.upload_volume(
            host, pool, name, path, fmt_hint=args.format, sha256=sha, verify=not args.no_verify,
            progress=lambda n: progress(n, size), limit_bps=_limit_bps(args),
            notes_state=state, author=check_author(args.author), url=url, step=prog))
    if "p" in progress.box:
        progress.box["p"].finish()
    return res


def check_upload_args(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    pool = checked(ks.check_pool_name, args.pool)
    name = checked(ks.check_volume_name, args.name)
    if args.sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", args.sha256):
        raise UsageError("--sha256: 64 hexadecimal characters")
    if args.format is not None and args.format not in ("qcow2", "raw", "iso"):
        raise UsageError("--format: qcow2, raw or iso")
    check_author(args.author)
    _limit_bps(args)
    return state, host, pool, name


def upload_text(res):
    verified = {True: "verified on the host", "skipped": "not verified (no sha256sum on the "
                "host)", None: "not verified"}[res["verified"]]
    return (f"{res['pool']}/{res['volume']}: {res['format']}, {res['capacity']} bytes, "
            f"{verified}, {res['elapsed_s']}s")


def cmd_vol_upload(args):
    state, host, pool, name = check_upload_args(args)
    if not args.file or not os.path.isfile(args.file):
        raise UsageError("--file: a readable local file")
    if not os.access(args.file, os.R_OK):
        raise UsageError("--file: the file cannot be read")
    res = do_upload(args, state, host, pool, name, args.file, args.sha256)
    emit(args, {"ok": True, "host": host.name, **res}, upload_text(res))
    return EXIT_OK


def cmd_image_download(args):
    """Téléchargement par la machine de la console (jamais par l'hôte : il n'a
    souvent pas d'accès sortant), puis le même envoi qu'un téléversement ;
    le fichier reçu est supprimé à la fin, réussite ou échec."""
    state, host, pool, name = check_upload_args(args)
    url = args.url
    bad = check_url(url)
    if bad:
        raise UsageError(f"--url: {bad}")
    allow = parse_cidrs(args.allow_private)
    max_size = DOWNLOAD_MAX_SIZE if args.max_size is None else parse_size(args.max_size,
                                                                            "--max-size")
    shown = public_url(url)
    prog = Progress("download")
    prog("download", "running", shown)
    progress = _upload_progress("download")
    path = None
    try:
        try:
            path, size, sha = download_url(url, staging_dir(state), progress,
                                           max_size=max_size, allow=allow)
        except Cancelled as c:
            c.stage = "download"
            raise
        except Failure as f:
            prog.fail(f.message)
            raise
        if "p" in progress.box:
            progress.box["p"].finish()
        if args.sha256 and sha != args.sha256.lower():
            msg = "the downloaded file does not match the expected SHA-256: nothing was sent"
            prog.fail(msg)
            raise Failure(msg, kind="refused", data={"host": host.name, "refusals": [msg]})
        prog("download", "done", f"{size} bytes, SHA-256 {sha}")
        res = do_upload(args, state, host, pool, name, path, sha, url=shown)
    finally:
        if path:
            with contextlib.suppress(OSError):
                os.unlink(path)
    emit(args, {"ok": True, "host": host.name, "url": shown, **res}, upload_text(res))
    return EXIT_OK


def cmd_vol_create(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    pool = checked(ks.check_pool_name, args.pool)
    name = checked(ks.check_volume_name, args.name)
    capacity = parse_size(args.capacity, "--capacity")
    if args.format not in ks.VOLUME_FORMATS:
        raise UsageError("--format: qcow2 or raw")
    if args.backing is not None:
        checked(ko.check_vm_name, args.backing)
    prog = Progress("create")
    data = {"host": host.name, "pool": pool, "volume": name, "refusals": []}
    with object_lock(state, host.name, "pool", pool, shared=True), \
            object_lock(state, host.name, "volume", f"{pool}/{name}"):
        prog("create", "running", f"{pool}/{name}"
             + (f" on image {args.backing}" if args.backing else ""))
        vol = lib_call(prog, data, lambda: ks.create_volume(
            host, pool, name, capacity, args.format, args.backing, args.prealloc_metadata))
        prog("verify", "done", f"{pool}/{name}: {(vol or {}).get('format')}, "
             f"{(vol or {}).get('capacity')} bytes")
    emit(args, {"ok": True, "host": host.name, "pool": pool, "name": name, "volume": vol},
         f"{pool}/{name}: {(vol or {}).get('format')}, {(vol or {}).get('capacity')} bytes")
    return EXIT_OK


def cmd_vol_resize(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    pool = checked(ks.check_pool_name, args.pool)
    name = checked(ko.check_vm_name, args.name)
    capacity = parse_size(args.capacity, "--capacity")
    ack = args.acknowledge_unreadable
    checked(ko.check_acknowledged, ack)
    prog = Progress("resize")
    data = {"host": host.name, "pool": pool, "volume": name, "refusals": []}
    with object_lock(state, host.name, "volume", f"{pool}/{name}"):
        prog("resize", "running", f"{pool}/{name} to {capacity} bytes")
        vol = lib_call(prog, data, lambda: ks.resize_volume(host, pool, name, capacity, ack))
        prog("resize", "done", f"{(vol or {}).get('capacity')} bytes")
    emit(args, {"ok": True, "host": host.name, "pool": pool, "name": name, "volume": vol},
         f"{pool}/{name}: {(vol or {}).get('capacity')} bytes")
    return EXIT_OK


def cmd_vol_delete(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    pool = checked(ks.check_pool_name, args.pool)
    name = checked(ko.check_vm_name, args.name)
    ack = args.acknowledge_unreadable
    checked(ko.check_acknowledged, ack)
    expect = required_digest(args, "an image" if args.cmd == "image" else "a volume")
    data = {"host": host.name, "pool": pool, "volume": name, "refusals": [], "unreadable": []}
    prog = Progress()
    if args.dry_run:
        prog("plan", "running", f"{pool}/{name} (dry run)")
        plan = lib_call(prog, data, lambda: ks.delete_volume_plan(host, pool, name, ack))
        prog("plan", "warn" if plan["refusals"] else "done",
             "; ".join(plan["refusals"]) or "nothing uses it")
        lines = unreadable_lines(plan["unreadable"], plan["acknowledged_unreadable"])
        if not plan["refusals"]:
            lines.append(f"to delete exactly this, add --expect-delete={plan['digest']}")
        emit(args, {"ok": True, "host": host.name, "dry_run": True,
                    "possible": not plan["refusals"], "plan": plan},
             plan_text(f"delete {pool}/{name} on {host.name} (dry run)", plan["refusals"],
                       lines))
        return EXIT_OK
    with object_lock(state, host.name, "volume", f"{pool}/{name}"), noting_progress(prog):
        try:
            res = ks.delete_volume(host, pool, name, notes_state=state,
                                   acknowledged_unreadable=ack, expect_digest=expect,
                                   progress=prog)
        except LIB_ERRORS as e:
            err = lib_error(prog, e, data)
            if isinstance(err, Failure) and isinstance(e, ko.OpError):
                err.data["unreadable"] = list((e.data.get("plan") or {}).get("unreadable")
                                              or [])
            raise err from None
        gone = lib_call(prog, data, lambda: ks._volume(host, pool, name)) is None
        prog("verify", "done" if gone else "warn",
             f"{pool}/{name} " + ("deleted" if gone else "still listed by libvirt"))
    emit(args, {"ok": True, "host": host.name, "dry_run": False, **res, "gone": gone},
         f"{pool}/{name} deleted from {host.name}")
    return EXIT_OK


# --- pools ---------------------------------------------------------------------------

def pool_spec(args):
    flags = {"name": args.name, "type": args.type, "path": args.path,
             "source_host": args.source_host, "source_dir": args.source_dir}
    given = [k for k, v in flags.items() if v is not None] + (["autostart"] if args.autostart
                                                               else [])
    if args.spec_file is not None:
        if given:
            raise UsageError("give either --spec-file or the options of the pool, not both")
        return read_spec(args.spec_file, "pool spec")
    if not args.name or not args.path:
        raise UsageError("give --spec-file, or --name and --path (and --type)")
    spec = {k: v for k, v in flags.items() if v is not None}
    spec.setdefault("type", "dir")
    spec["autostart"] = bool(args.autostart)
    return spec


def pool_line(st):
    return (f"pool {st.get('name')}: {st.get('state') or 'unknown'}, {st.get('type')}, "
            f"{st.get('target_path') or '-'}, autostart {'yes' if st.get('autostart') else 'no'}")


def cmd_pool_define(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    spec = pool_spec(args)
    name = spec.get("name") if isinstance(spec.get("name"), str) else ""
    checked(ks.check_pool_name, name, True)
    prog = Progress()
    data = {"host": host.name, "pool": name, "refusals": [], "leftovers": []}
    with object_lock(state, host.name, "pool", name), noting_progress(prog):
        res = lib_call(prog, data, lambda: ks.define_pool(host, spec, step=prog,
                                                          dry_run=args.dry_run))
        if not args.dry_run:
            prog("verify", "done", pool_line(res["state"]))
    lines = [f"nested with pool {', '.join(res['nested_with'])}"] if res["nested_with"] else []
    if res.get("existing_directory"):
        lines.append("the target directory already existed")
    title = (f"pool {name} on {host.name} (dry run)" if args.dry_run
             else pool_line(res["state"]))
    emit(args, {"ok": True, "host": host.name, **res}, plan_text(title, [], lines))
    return EXIT_OK


def pool_gesture(args, verb):
    state = state_of(args)
    host = load_host_next(state, args.host)
    pool = checked(ks.check_pool_name, args.pool)
    prog = Progress(verb)
    data = {"host": host.name, "pool": pool, "refusals": []}
    fns = {"start": lambda: ks.start_pool(host, pool),
           "stop": lambda: ks.stop_pool(host, pool),
           "refresh": lambda: ks.refresh_pool(host, pool),
           "autostart": lambda: ks.autostart_pool(host, pool, not args.disable)}
    shared = verb in ("refresh", "start")
    with object_lock(state, host.name, "pool", pool, shared=shared), noting_progress(prog):
        what = (f"autostart {'off' if args.disable else 'on'}" if verb == "autostart"
                else verb)
        prog(verb, "running", f"{what} pool {pool}")
        st = lib_call(prog, data, fns[verb])
        prog(verb, "done", pool_line(st))
    emit(args, {"ok": True, "host": host.name, "pool": pool, "action": verb, "state": st},
         pool_line(st))
    return EXIT_OK


def cmd_pool_delete(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    pool = checked(ks.check_pool_name, args.pool)
    ack = args.acknowledge_unreadable
    checked(ko.check_acknowledged, ack)
    expect = required_digest(args, "a pool")
    data = {"host": host.name, "pool": pool, "refusals": [], "unreadable": []}
    prog = Progress()
    if args.dry_run:
        prog("plan", "running", f"pool {pool} (dry run)")
        plan = lib_call(prog, data, lambda: ks.delete_pool_plan(
            host, pool, args.remove_dir, acknowledged_unreadable=ack))
        prog("plan", "warn" if plan["refusals"] else "done",
             "; ".join(plan["refusals"]) or "nothing uses it")
        lines = unreadable_lines(plan["unreadable"], plan["acknowledged_unreadable"])
        lines.append("the directory is removed only if it is empty: the deletion is refused "
                     "while the pool holds volumes" if args.remove_dir else
                     "the directory and its files are kept")
        if not plan["refusals"]:
            lines.append(f"to delete exactly this, add --expect-delete={plan['digest']}")
        emit(args, {"ok": True, "host": host.name, "dry_run": True,
                    "possible": not plan["refusals"], "plan": plan},
             plan_text(f"delete pool {pool} on {host.name} (dry run)", plan["refusals"], lines))
        return EXIT_OK
    with object_lock(state, host.name, "pool", pool), noting_progress(prog):
        res = lib_call(prog, data, lambda: ks.delete_pool(
            host, pool, args.remove_dir, step=prog, acknowledged_unreadable=ack,
            expect_digest=expect))
        names = lib_call(prog, data, lambda: kh.parse_names(
            host.virsh("pool-list", "--all", "--name", idempotent=True)))
        gone = pool not in names
        prog("verify", "done" if gone else "warn",
             f"pool {pool} " + ("removed" if gone else "still listed by libvirt"))
    emit(args, {"ok": True, "host": host.name, "dry_run": False, **res, "gone": gone},
         f"pool {pool} deleted from {host.name}"
         + (" with its directory" if res["directory_removed"] else ", files kept"))
    return EXIT_OK


# --- réseaux --------------------------------------------------------------------------

def net_spec(args):
    flags = {"name": args.name, "mode": args.mode, "subnet": args.subnet,
             "bridge": args.bridge, "dhcp_start": args.dhcp_start, "dhcp_end": args.dhcp_end,
             "domain": args.domain}
    given = [k for k, v in flags.items() if v is not None] + (
        ["autostart"] if args.autostart else []) + (["no_dhcp"] if args.no_dhcp else [])
    if args.spec_file is not None:
        if given:
            raise UsageError("give either --spec-file or the options of the network, not both")
        return read_spec(args.spec_file, "network spec")
    if not args.name or not args.mode:
        raise UsageError("give --spec-file, or --name and --mode (and --subnet or --bridge)")
    spec = {k: v for k, v in flags.items() if v is not None}
    spec["dhcp"] = not args.no_dhcp
    spec["autostart"] = bool(args.autostart)
    return spec


def net_line(n):
    ips = ", ".join(f"{i['address']}/{i['prefix']}" for i in n.get("ips") or [])
    return (f"network {n.get('name')}: {'active' if n.get('active') else 'inactive'}, "
            f"{n.get('forward')}, bridge {n.get('bridge') or '-'}"
            + (f", {ips}" if ips else "")
            + f", autostart {'yes' if n.get('autostart') else 'no'}")


def cmd_net_define(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    spec = net_spec(args)
    name = spec.get("name") if isinstance(spec.get("name"), str) else ""
    checked(kn.check_network_name, name, True)
    prog = Progress()
    data = {"host": host.name, "network": name, "refusals": [], "leftovers": []}
    with object_lock(state, host.name, "network", name), noting_progress(prog):
        res = lib_call(prog, data, lambda: kn.define_network(host, spec, step=prog,
                                                             dry_run=args.dry_run))
        if not args.dry_run:
            prog("verify", "done", net_line(res["network"]))
    title = (f"network {name} on {host.name} (dry run)" if args.dry_run
             else net_line(res["network"]))
    emit(args, {"ok": True, "host": host.name, **res}, title)
    return EXIT_OK


def net_gesture(args, verb):
    state = state_of(args)
    host = load_host_next(state, args.host)
    net = checked(kn.check_network_name, args.network)
    prog = Progress(verb)
    data = {"host": host.name, "network": net, "refusals": []}
    fns = {"start": lambda: kn.start_network(host, net),
           "stop": lambda: kn.stop_network(host, net),
           "autostart": lambda: kn.autostart_network(host, net, not args.disable)}
    # démarrer n'ôte rien à une création qui cite le réseau (partage) ; arrêt
    # et démarrage automatique en exclusif, comme pour les pools
    with object_lock(state, host.name, "network", net, shared=verb == "start"), \
            noting_progress(prog):
        what = (f"autostart {'off' if args.disable else 'on'}" if verb == "autostart"
                else verb)
        prog(verb, "running", f"{what} network {net}")
        n = lib_call(prog, data, fns[verb])
        prog(verb, "done", net_line(n))
    emit(args, {"ok": True, "host": host.name, "network": net, "action": verb, "state": n},
         net_line(n))
    return EXIT_OK


def cmd_net_delete(args):
    state = state_of(args)
    host = load_host_next(state, args.host)
    net = checked(kn.check_network_name, args.network)
    expect = required_digest(args, "a network")
    data = {"host": host.name, "network": net, "refusals": []}
    prog = Progress()
    if args.dry_run:
        prog("plan", "running", f"network {net} (dry run)")
        plan = lib_call(prog, data, lambda: kn.delete_network_plan(host, net))
        prog("plan", "warn" if plan["refusals"] else "done",
             "; ".join(plan["refusals"]) or "nothing uses it")
        lines = [] if plan["refusals"] else [
            f"to delete exactly this, add --expect-delete={plan['digest']}"]
        emit(args, {"ok": True, "host": host.name, "dry_run": True,
                    "possible": not plan["refusals"], "plan": plan},
             plan_text(f"delete network {net} on {host.name} (dry run)", plan["refusals"],
                       lines))
        return EXIT_OK
    with object_lock(state, host.name, "network", net), noting_progress(prog):
        res = lib_call(prog, data, lambda: kn.delete_network(host, net, step=prog,
                                                             expect_digest=expect))
        names = [n["name"] for n in lib_call(prog, data, lambda: kn.list_networks(host))]
        gone = net not in names
        prog("verify", "done" if gone else "warn",
             f"network {net} " + ("removed" if gone else "still listed by libvirt"))
    emit(args, {"ok": True, "host": host.name, "dry_run": False, **res, "gone": gone},
         f"network {net} deleted from {host.name}")
    return EXIT_OK


def cmd_net_links(args):
    host = load_host_next(state_of(args), args.host)
    prog = Progress("links")
    prog("links", "running", f"bridges and links of {host.name}")
    res = lib_call(prog, {}, lambda: kn.host_links(host))
    prog("links", "done", f"{len(res['bridges'])} bridge(s), {len(res['physical'])} physical "
         "link(s)")
    lines = [f"bridge {b['name']}" for b in res["bridges"]]
    lines += [f"physical {p['name'] if isinstance(p, dict) else p}" for p in res["physical"]]
    emit(args, {"ok": True, "host": host.name, **res}, "\n".join(lines))
    return EXIT_OK


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
                return fail_exit(want_json, "cancelled", cancel_message(args, e), "cancelled",
                                 EXIT_CANCELLED, cancel_data(args, e))
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
    if args.cmd == "vm" and args.vcmd == "create":
        return create_cancel_message(sig)
    if args.cmd == "vm" and args.vcmd in EDIT_COMMANDS:
        return (f"{EDIT_COMMANDS[args.vcmd]} cancelled ({sig}): some changes may already be "
                "applied: reload the VM")
    if args.cmd in ("vol", "image") and getattr(args, "ocmd", None) in UPLOADS:
        return upload_cancel_message(args, sig)
    if args.cmd in ("vol", "image", "pool", "net"):
        what = {"vol": "volume", "image": "image", "pool": "pool", "net": "network"}[args.cmd]
        return (f"{what} {args.ocmd} cancelled ({sig}): it may already have been "
                f"{OBJECT_VERBS.get(args.ocmd, 'done')}: check the list")
    if args.cmd == "snapshot" and args.scmd in SNAPSHOT_DONE:
        return (f"snapshot {args.scmd} cancelled ({sig}): it may already have been "
                f"{SNAPSHOT_DONE[args.scmd]}: list the snapshots")
    if args.cmd == "clone":
        left = getattr(sig, "leftovers", None)
        if left is None and _completed(sig):
            return (f"clone cancelled ({sig}) after the copy was complete: "
                    f"{args.new_name} was created")
        if left is None:
            return f"clone cancelled ({sig}) before any copy: nothing created"
        if not left:
            return f"clone cancelled ({sig}): rolled back"
        return (f"clone cancelled ({sig}): rollback incomplete, left on the host: "
                + ", ".join(describe_leftover(x) for x in left))
    if args.cmd == "vm" and args.vcmd == "delete":
        prog = getattr(sig, "delete_progress", None)
        if prog is None and _completed(sig):
            return f"vm delete cancelled ({sig}) after it completed: {args.name} was deleted"
        if prog is None or not prog["undefine_started"]:
            return f"vm delete cancelled ({sig}) before anything was changed: the VM is kept"
        msg = (f"vm delete cancelled ({sig}): it may already have been undefined: "
               "check the VM list")
        if prog["pending"]:
            msg += ("; owned disks not confirmed removed (possibly partly deleted, check "
                    "them): " + ", ".join(prog["pending"]))
        return msg
    return f"cancelled ({sig})"


# sous-commandes `vm` de modification -> nom dit dans le message d'annulation
EDIT_COMMANDS = {"edit": "edit", "cpumem": "cpumem", "add-volume": "add-volume",
                 "remove-volume": "remove-volume", "insert-cdrom": "insert-cdrom",
                 "eject-image": "eject-image", "add-nic": "add-nic",
                 "remove-nic": "remove-nic", "cloudinit": "cloud-init"}
UPLOADS = ("upload", "download-url")


def _last_done(sig):
    """La dernière étape dite est une relecture réussie : l'opération était
    complète quand l'annulation est arrivée."""
    last = getattr(sig, "last_step", None) or (None, None)
    return str(last[0]).endswith("verify") and last[1] == "done"


def create_cancel_message(sig):
    left = getattr(sig, "leftovers", None)
    created = [c.get("name") for c in getattr(sig, "created", None) or []]
    kept = f"; already created and kept: {', '.join(created)}" if created else ""
    if left is None and _last_done(sig):
        return f"creation cancelled ({sig}) after it completed: the VM(s) were created"
    if left is None:
        return f"creation cancelled ({sig}) before anything was created"
    if not left:
        return f"creation cancelled ({sig}): rolled back{kept}"
    return (f"creation cancelled ({sig}): rollback incomplete, left on the host: "
            + ", ".join(describe_leftover(x) for x in left) + kept)


def upload_cancel_message(args, sig):
    if getattr(sig, "stage", None) == "download":
        return f"download cancelled ({sig}): nothing was sent to the host"
    left = getattr(sig, "leftovers", None)
    if left is None and _last_done(sig):
        return f"upload cancelled ({sig}) after it completed: {args.pool}/{args.name} is kept"
    if left is None:
        return f"upload cancelled ({sig}) before any volume was created"
    if not left:
        return f"upload cancelled ({sig}): the partial volume was deleted"
    return (f"upload cancelled ({sig}): the partial volume could not be deleted: "
            + ", ".join(describe_leftover(x) for x in left))


def cancel_data(args, sig):
    """Champs du JSON d'annulation : `leftovers` (et `created`) d'un clonage
    commencé, `pending` d'une suppression dont le undefine était parti ;
    `created`, `failed`, `leftovers` d'une création ; `changes` et
    `leftovers` d'une modification ; `leftovers` d'un téléversement."""
    if getattr(args, "dry_run", False):
        return None
    if args.cmd == "vm" and args.vcmd == "create" and hasattr(sig, "leftovers"):
        return {"created": list(getattr(sig, "created", None) or []),
                "failed": getattr(sig, "failed", None), "leftovers": list(sig.leftovers)}
    if args.cmd == "vm" and args.vcmd in EDIT_COMMANDS and hasattr(sig, "changes"):
        return {"changes": list(sig.changes), "leftovers": list(getattr(sig, "leftovers", [])
                                                                or [])}
    if args.cmd in ("vol", "image") and getattr(args, "ocmd", None) in UPLOADS and \
            hasattr(sig, "leftovers"):
        return {"leftovers": list(sig.leftovers)}
    if args.cmd == "clone" and not getattr(args, "dry_run", False):
        left = getattr(sig, "leftovers", None)
        if left is not None:
            return {"leftovers": list(left)}
        if _completed(sig):
            return {"leftovers": [], "created": args.new_name}
    if args.cmd == "vm" and args.vcmd == "delete" and not getattr(args, "dry_run", False):
        prog = getattr(sig, "delete_progress", None)
        if prog and prog["undefine_started"]:
            return {"pending": list(prog["pending"])}
    return None


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
    sp = hp.add_parser("caps", parents=[common],
                       help="what the VM form can offer on a host, with the reason of each "
                            "option that is not available")
    sp.add_argument("--host", required=True)
    sp.add_argument("--machine", help="q35 or pc (default: both)")
    sp.set_defaults(fn=cmd_host_caps)

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
    sp.add_argument("--expect-delete", metavar="DIGEST",
                    help="digest shown by --dry-run, required to delete: refused if more "
                         "would be deleted")
    sp.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sp.add_argument("--timeout", type=int, help=f"seconds (default {DELETE_TIMEOUT})")
    sp.add_argument("--survey-timeout", type=int,
                    help=f"seconds for each read of the host's disks "
                         f"(default {DELETE_SURVEY_TIMEOUT})")
    sp.set_defaults(fn=cmd_vm_delete)

    add_next_parsers(sub, vp, common)

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


def _vm_target(sp):
    sp.add_argument("--host", required=True)
    sp.add_argument("--name", required=True)


def add_next_parsers(sub, vp, common):
    """Sous-commandes de création, modification, images, volumes, pools et
    réseaux (prochaine version KVM)."""
    sp = vp.add_parser("create", parents=[common],
                       help="create one or several VMs from a JSON spec (or options)")
    sp.add_argument("--host", required=True)
    sp.add_argument("--spec-file", help="JSON VmSpec (- for standard input)")
    sp.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sp.add_argument("--timeout", type=int,
                    help=f"seconds (default {CREATE_TIMEOUT}); checked between steps (before "
                         "each volume, the seed, define, start) and between blocks of an "
                         "upload, never inside a running virsh command (a full copy keeps "
                         "its own limit); past it, the creation is rolled back")
    sp.add_argument("--name")
    sp.add_argument("--instances", type=int)
    sp.add_argument("--vcpus", type=int)
    sp.add_argument("--memory-mib", type=int)
    for flag in ("os", "machine", "firmware", "title", "description", "hostname",
                 "seed-pool"):
        sp.add_argument(f"--{flag}")
    sp.add_argument("--disk", action="append", metavar="KIND:...",
                    help="blank:POOL:SIZE, image:POOL/IMAGE[:SIZE][:copy], volume:POOL/VOLUME, "
                         "iso:POOL/VOLUME (repeatable)")
    sp.add_argument("--nic", action="append", metavar="KIND:SOURCE",
                    help="network:NAME, bridge:NAME, direct:IFACE[:MODE] (repeatable)")
    sp.add_argument("--user-data", metavar="FILE")
    sp.add_argument("--network-data", metavar="FILE")
    for flag in ("start", "autostart", "secure-boot", "tpm"):
        sp.add_argument(f"--{flag}", action="store_true")
    sp.add_argument("--vnc-password", action="store_true",
                    help="protect the display with a password generated by the console "
                         "(never shown)")
    sp.set_defaults(fn=cmd_vm_create)

    sp = vp.add_parser("edit", parents=[common],
                       help="change a VM: applied now when libvirt allows it, else at next start")
    _vm_target(sp)
    sp.add_argument("--spec-file", required=True, help="JSON spec of the fields to change")
    sp.add_argument("--expect-config", "--expect-digest", dest="expect_config",
                    metavar="DIGEST", help="config_digest read when the form was opened")
    sp.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sp.add_argument("--timeout", type=int,
                    help=f"seconds (default {EDIT_TIMEOUT}); checked between the changes, "
                         "never inside a running virsh command")
    sp.set_defaults(fn=cmd_vm_edit)

    sp = vp.add_parser("cpumem", parents=[common], help="vCPU and memory, live up to the maximums")
    _vm_target(sp)
    sp.add_argument("--vcpus", type=int)
    sp.add_argument("--memory-mib", type=int)
    sp.set_defaults(fn=cmd_vm_cpumem)
    sp = vp.add_parser("add-volume", parents=[common], help="add a disk (JSON disk spec)")
    _vm_target(sp)
    sp.add_argument("--spec-file", required=True)
    sp.set_defaults(fn=cmd_vm_add_volume)
    sp = vp.add_parser("remove-volume", parents=[common],
                       help="remove a disk (its volume is kept)")
    _vm_target(sp)
    sp.add_argument("--target", required=True)
    sp.set_defaults(fn=cmd_vm_remove_volume)
    sp = vp.add_parser("insert-cdrom", parents=[common], help="put a volume in a CD-ROM drive")
    _vm_target(sp)
    sp.add_argument("--target", required=True)
    sp.add_argument("--pool", required=True)
    sp.add_argument("--volume", required=True)
    sp.set_defaults(fn=cmd_vm_insert_cdrom)
    sp = vp.add_parser("eject-image", parents=[common], help="empty a CD-ROM drive")
    _vm_target(sp)
    sp.add_argument("--target", required=True)
    sp.set_defaults(fn=cmd_vm_eject_image)
    sp = vp.add_parser("add-nic", parents=[common], help="add a network card (JSON card spec)")
    _vm_target(sp)
    sp.add_argument("--spec-file", required=True)
    sp.set_defaults(fn=cmd_vm_add_nic)
    sp = vp.add_parser("remove-nic", parents=[common], help="remove the network card with this MAC")
    _vm_target(sp)
    sp.add_argument("--mac", required=True)
    sp.set_defaults(fn=cmd_vm_remove_nic)
    sp = vp.add_parser("cloudinit", parents=[common],
                       help="new cloud-init seed (JSON: user_data, network_data, hostname)")
    _vm_target(sp)
    sp.add_argument("--spec-file", required=True)
    sp.add_argument("--rerun", action="store_true",
                    help="new instance-id: cloud-init runs its first-boot modules again")
    sp.add_argument("--acknowledge-unreadable", action="append", metavar="PATH",
                    help="file the SSH user cannot read, accepted when removing the old seed")
    sp.set_defaults(fn=cmd_vm_cloudinit)

    def upload_opts(sp):
        sp.add_argument("--host", required=True)
        sp.add_argument("--pool", required=True)
        sp.add_argument("--name", required=True, help="name of the new volume")
        sp.add_argument("--sha256", help="expected SHA-256 of the file")
        sp.add_argument("--format", help="qcow2, raw or iso (checked against the file)")
        sp.add_argument("--no-verify", action="store_true",
                        help="do not recompute the SHA-256 on the host after the upload")
        sp.add_argument("--limit-mbps", type=float, help="bandwidth cap in megabits per second")
        sp.add_argument("--author", help="who uploads (kept in the console notes)")

    def delete_opts(sp, volume_flag="--name"):
        sp.add_argument("--host", required=True)
        sp.add_argument("--pool", required=True)
        sp.add_argument(volume_flag, dest="name", required=True)
        sp.add_argument("--acknowledge-unreadable", action="append", metavar="PATH")
        sp.add_argument("--expect-delete", metavar="DIGEST",
                        help="digest shown by --dry-run, required to delete: refused if the "
                             "volume changed")
        sp.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
        sp.set_defaults(fn=cmd_vol_delete)

    ip = sub.add_parser("image", help="image catalogue of a host").add_subparsers(
        dest="ocmd", required=True)
    sp = ip.add_parser("list", parents=[common], help="images of a host")
    sp.add_argument("--host", required=True)
    sp.add_argument("--fresh", action="store_true", help="read the host now (no cached survey)")
    sp.set_defaults(fn=cmd_image_list)
    sp = ip.add_parser("verify", parents=[common], help="recompute the SHA-256 on the host")
    sp.add_argument("--host", required=True)
    sp.add_argument("--pool", required=True)
    sp.add_argument("--volume", required=True)
    sp.add_argument("--sha256", help="expected value (default: the one noted at upload)")
    sp.set_defaults(fn=cmd_image_verify)
    sp = ip.add_parser("upload", parents=[common], help="send a local file as a new image")
    upload_opts(sp)
    sp.add_argument("--file", required=True)
    sp.set_defaults(fn=cmd_vol_upload)
    sp = ip.add_parser("download-url", parents=[common],
                       help="download an http(s) URL on this machine, then send it to the host")
    upload_opts(sp)
    sp.add_argument("--url", required=True)
    sp.add_argument("--allow-private", action="append", metavar="CIDR",
                    help="address range the server may be in although it is private, loopback "
                         "or link-local (repeatable; default: public addresses only, checked "
                         "at every redirect)")
    sp.add_argument("--max-size", metavar="SIZE",
                    help=f"largest file accepted (default {DOWNLOAD_MAX_SIZE >> 30}G)")
    sp.set_defaults(fn=cmd_image_download)
    sp = ip.add_parser("delete", parents=[common], help="delete an image (refused while used)")
    delete_opts(sp, "--volume")

    op = sub.add_parser("vol", help="storage volumes").add_subparsers(dest="ocmd", required=True)
    sp = op.add_parser("create", parents=[common], help="blank volume, or linked to an image")
    sp.add_argument("--host", required=True)
    sp.add_argument("--pool", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--capacity", required=True, help="bytes, or with K, M, G, T")
    sp.add_argument("--format", default="qcow2")
    sp.add_argument("--backing", metavar="IMAGE", help="image of the same pool (linked volume)")
    sp.add_argument("--prealloc-metadata", action="store_true")
    sp.set_defaults(fn=cmd_vol_create)
    sp = op.add_parser("upload", parents=[common], help="send a local file as a new volume")
    upload_opts(sp)
    sp.add_argument("--file", required=True)
    sp.set_defaults(fn=cmd_vol_upload)
    sp = op.add_parser("resize", parents=[common], help="grow a volume of a shut off VM")
    sp.add_argument("--host", required=True)
    sp.add_argument("--pool", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--capacity", required=True, help="bytes, or with K, M, G, T")
    sp.add_argument("--acknowledge-unreadable", action="append", metavar="PATH")
    sp.set_defaults(fn=cmd_vol_resize)
    sp = op.add_parser("delete", parents=[common], help="delete a volume (refused while used)")
    delete_opts(sp)

    pp = sub.add_parser("pool", help="storage pools").add_subparsers(dest="ocmd", required=True)
    sp = pp.add_parser("define", parents=[common], help="create a dir or NFS pool")
    sp.add_argument("--host", required=True)
    sp.add_argument("--spec-file")
    sp.add_argument("--name")
    sp.add_argument("--type", help="dir (default) or netfs")
    sp.add_argument("--path")
    sp.add_argument("--source-host")
    sp.add_argument("--source-dir")
    sp.add_argument("--autostart", action="store_true")
    sp.add_argument("--dry-run", action="store_true", help="checks and XML only")
    sp.set_defaults(fn=cmd_pool_define)
    for verb, text in (("start", "start a pool"),
                       ("stop", "stop a pool (pool-destroy: no data is destroyed)"),
                       ("refresh", "rescan the volumes of a pool"),
                       ("autostart", "start the pool with the host (--disable: no)")):
        sp = pp.add_parser(verb, parents=[common], help=text)
        sp.add_argument("--host", required=True)
        sp.add_argument("--pool", required=True)
        if verb == "autostart":
            sp.add_argument("--disable", action="store_true")
        sp.set_defaults(fn=lambda a, _v=verb: pool_gesture(a, _v))
    sp = pp.add_parser("delete", parents=[common], help="undefine a pool (files kept by default)")
    sp.add_argument("--host", required=True)
    sp.add_argument("--pool", required=True)
    sp.add_argument("--remove-dir", action="store_true",
                    help="also remove the directory, only when the pool is empty")
    sp.add_argument("--acknowledge-unreadable", action="append", metavar="PATH")
    sp.add_argument("--expect-delete", metavar="DIGEST",
                    help="digest shown by --dry-run (same flags), required to delete: refused "
                         "if the pool changed")
    sp.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sp.set_defaults(fn=cmd_pool_delete)

    xp = sub.add_parser("net", help="libvirt networks").add_subparsers(dest="ocmd", required=True)
    sp = xp.add_parser("define", parents=[common], help="create a NAT, routed, isolated or "
                                                        "bridge network")
    sp.add_argument("--host", required=True)
    sp.add_argument("--spec-file")
    sp.add_argument("--name")
    sp.add_argument("--mode", help="nat, route, isolated or bridge")
    sp.add_argument("--subnet")
    sp.add_argument("--bridge")
    sp.add_argument("--dhcp-start")
    sp.add_argument("--dhcp-end")
    sp.add_argument("--domain")
    sp.add_argument("--no-dhcp", action="store_true")
    sp.add_argument("--autostart", action="store_true")
    sp.add_argument("--dry-run", action="store_true", help="checks and XML only")
    sp.set_defaults(fn=cmd_net_define)
    for verb, text in (("start", "start a network"), ("stop", "stop a network"),
                       ("autostart", "start the network with the host (--disable: no)")):
        sp = xp.add_parser(verb, parents=[common], help=text)
        sp.add_argument("--host", required=True)
        sp.add_argument("--network", required=True)
        if verb == "autostart":
            sp.add_argument("--disable", action="store_true")
        sp.set_defaults(fn=lambda a, _v=verb: net_gesture(a, _v))
    sp = xp.add_parser("delete", parents=[common], help="undefine a network (refused while cited)")
    sp.add_argument("--host", required=True)
    sp.add_argument("--network", required=True)
    sp.add_argument("--expect-delete", metavar="DIGEST",
                    help="digest shown by --dry-run, required to delete: refused if the "
                         "network changed")
    sp.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sp.set_defaults(fn=cmd_net_delete)
    sp = xp.add_parser("links", parents=[common], help="bridges and links of the host")
    sp.add_argument("--host", required=True)
    sp.set_defaults(fn=cmd_net_links)


if __name__ == "__main__":
    sys.exit(main())
