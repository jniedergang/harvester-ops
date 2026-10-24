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

import datetime
import hashlib
import ipaddress
import os
import re
import secrets
import shlex
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import kvm_decl

KINDS = ("unreachable", "hostkey", "auth", "denied", "notfound", "timeout", "failed")

# Classement des erreurs, en deux familles qui ne se mélangent jamais :
# - celles de ssh lui-même (empreinte, clé, réseau), lues seulement quand le
#   processus ssh rend 255 et seulement sur SA sortie d'erreur ;
# - celles de virsh/libvirt sur l'hôte, lues sur la sortie d'erreur de la
#   commande. Un domaine nommé « connection refused » reste donc `notfound`,
#   et un démon libvirt arrêté (« virtqemud-sock': Connection refused ») n'est
#   pas un hôte injoignable.
SSH_RC = 255
_SSH_MARKERS = (
    ("hostkey", ("host key verification failed", "remote host identification has changed",
                 "no matching host key", "host key for")),
    ("auth", ("permission denied (publickey", "permission denied (password",
              "permission denied (keyboard-interactive",
              "too many authentication failures", "no supported authentication methods")),
    ("unreachable", ("connection refused", "connection timed out", "no route to host",
                     "could not resolve hostname", "network is unreachable",
                     "operation timed out", "connection closed by", "connection reset")),
)
_REMOTE_MARKERS = (
    ("notfound", ("failed to get domain", "domain not found", "failed to get pool",
                  "storage pool not found", "failed to get network", "network not found",
                  "failed to get vol", "storage volume not found")),
    ("denied", ("authentication unavailable", "permission denied", "access denied",
                "authentication failed")),
)
# démon libvirt absent ou arrêté : classé `failed`, message explicite
_LIBVIRT_DOWN = ("failed to connect socket", "failed to connect to the hypervisor",
                 "no connection driver available", "end of file while reading data")
# codes de sortie de `timeout` (coreutils) : délai atteint, puis SIGKILL
_TIMEOUT_RCS = (124, 137)

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


def _match(markers, text):
    low = (text or "").lower()
    for kind, words in markers:
        if any(w in low for w in words):
            return kind
    return "failed"


def classify(stderr, ssh_level=False):
    """Genre d'une erreur. `ssh_level` : stderr de ssh lui-même (processus
    ssh en 255), seuls ses repères comptent ; sinon stderr d'une commande
    distante, seuls ceux de virsh/libvirt comptent."""
    return _match(_SSH_MARKERS if ssh_level else _REMOTE_MARKERS, stderr)


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


def make_error(host, stderr, rc, paths=(), ssh_level=False):
    """KvmError classée. `ssh_level` : l'échec vient de ssh (code 255, stderr
    de ssh) ; sinon de la commande distante."""
    kind = classify(stderr, ssh_level)
    detail = _error_detail(stderr, paths)
    if not ssh_level and kind == "failed" and any(m in (stderr or "").lower()
                                                  for m in _LIBVIRT_DOWN):
        detail = "libvirt is not reachable on the host: " + detail
    if not detail:
        detail = f"{'ssh' if ssh_level else 'command'} exited with status {rc}"
    return KvmError(kind, f"{host}: {kind}: {detail}", host=host, rc=rc)


def _ssh_opt_path(path):
    # ssh découpe la valeur d'une option -o sur les espaces : on la cite
    path = str(path)
    return f'"{path}"' if re.search(r"\s", path) else path


# sun_path de Linux : 108 octets, NUL compris. ssh lie d'abord le socket
# maître sous un nom temporaire `<ControlPath>.<16 caractères>` puis le
# renomme : le chemin final doit donc tenir en 107 - 17 = 90 octets (vérifié
# avec OpenSSH 10.5 : 90 passe, 91 échoue en « too long for Unix domain
# socket »).
MAX_CONTROL_PATH = 107 - 17
CONTROL_HASH_LEN = 8


def control_path(decl, state):
    """Socket ControlMaster d'une déclaration : `<état>/ssh/cm-<nom>-<hash>`.
    Le nom sépare deux déclarations vers le même hôte ; le hash (adresse,
    port, utilisateur) fait qu'une déclaration modifiée n'hérite jamais de
    l'ancienne connexion. On n'utilise pas `%C` de ssh : ses 40 caractères
    ne laisseraient que quelques octets au nom sous le répertoire d'état de
    la console de dev. KvmError (`failed`) si le chemin est trop long."""
    name = decl.get("name")
    st = str(state)
    if "%" in st:
        # ssh développe `%` dans ControlPath, IdentityFile, UserKnownHostsFile
        raise KvmError("failed", f"{name}: failed: the state directory must not contain '%'",
                       host=name)
    ident = f"{decl.get('user')}@{decl.get('address')}:{decl.get('port', kvm_decl.DEFAULT_PORT)}"
    digest = hashlib.sha256(ident.encode()).hexdigest()[:CONTROL_HASH_LEN]
    path = Path(state) / kvm_decl.SSH_DIR / f"cm-{name}-{digest}"
    size = len(os.fsencode(str(path)))
    if size > MAX_CONTROL_PATH:
        raise KvmError("failed", f"{name}: failed: the SSH control socket path would be {size} "
                       f"bytes, over the {MAX_CONTROL_PATH} ssh can bind; the state directory "
                       f"takes {len(os.fsencode(st))} bytes: shorten it or the host name",
                       host=name)
    return path


def _ssh_base(decl, state):
    """ssh et ses options pour une déclaration, sans destination."""
    name = decl.get("name") if isinstance(decl, dict) else "?"
    why = kvm_decl.check_decl(kvm_decl.normalize(decl)) if isinstance(decl, dict) else "not a mapping"
    if why:
        raise KvmError("failed", f"{name}: failed: invalid declaration: {why}", host=name)
    try:
        key = kvm_decl.resolve(state, decl["ssh_key"])
        known = kvm_decl.resolve(state, decl["known_hosts"])
    except kvm_decl.DeclError as e:
        raise KvmError("failed", f"{name}: failed: invalid declaration: {e}", host=name) from None
    cpath = control_path(decl, state)
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
        "-o", f"ControlPath={_ssh_opt_path(cpath)}",
        "-o", "ControlPersist=60",
        "-i", str(key),
        "-p", str(decl.get("port", kvm_decl.DEFAULT_PORT)),
        "-l", decl["user"],
    ]


def ssh_argv(decl, state, remote_argv):
    """Ligne de commande ssh qui lance `remote_argv` sur l'hôte. Chaque
    élément distant est cité (shlex.quote) et le tout part en UN argument :
    le shell distant le redécoupe exactement en `remote_argv`. KvmError
    (`failed`) pour une déclaration ou un chemin rangé invalide."""
    remote = " ".join(shlex.quote(str(a)) for a in remote_argv)
    return [*_ssh_base(decl, state), "--", decl["address"], remote]


VNC_PORT_MIN, VNC_PORT_MAX = 5900, 65535


def _forward_target(address, port):
    """`<adresse>:<port>` de `ssh -W`, contrôlé : IP littérale seulement
    (IPv6 entre crochets), port entier 5900..65535. Aucun texte venu du XML
    n'arrive tel quel dans la ligne de ssh."""
    if isinstance(port, bool) or not isinstance(port, int) or not (
            VNC_PORT_MIN <= port <= VNC_PORT_MAX):
        raise KvmError("failed", f"failed: invalid display port {port!r}")
    try:
        ip = ipaddress.ip_address(str(address))
    except ValueError:
        raise KvmError("failed", "failed: the display address must be a literal IP") from None
    if ip.version == 6:
        return f"[{ip.compressed}]:{port}"
    return f"{ip.compressed}:{port}"


def stdio_forward_argv(decl, state, address, port):
    """ssh qui relie son entrée et sa sortie standard à `<adresse>:<port>`
    vu depuis l'hôte (`ssh -W`), pour la console VNC. `ControlMaster=no` : le
    tunnel passe par la connexion maître si elle existe, sans jamais en
    devenir une (un maître né d'un `-W` mourrait avec la console du
    navigateur). Même ControlPath que les autres commandes. KvmError
    (`failed`) pour une adresse ou un port refusé."""
    target = _forward_target(address, port)
    base = _ssh_base(decl, state)
    i = base.index("ControlMaster=auto")
    base[i] = "ControlMaster=no"
    return [*base, "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-W", target, "--", decl["address"]]


def close_master(decl, state, runner=subprocess.run, timeout=10):
    """Ferme la connexion ControlMaster d'une déclaration (`ssh -O exit`,
    mêmes options). À appeler avant un ajout, une modification ou un
    retrait : sinon une commande suivante passerait par l'ancienne connexion
    sans revérifier ni la clé ni l'empreinte. Rend True si ssh a fermé une
    connexion ; jamais d'exception (rien à fermer n'est pas une erreur)."""
    try:
        base = _ssh_base(decl, state)
    except KvmError:
        return False
    argv = [base[0], "-O", "exit", *base[1:], "--", decl["address"]]
    try:
        res = runner(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                     timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(res, "returncode", 1) == 0


class VirshHost:
    """Un hôte KVM déclaré, interrogé par `virsh` à travers SSH."""

    def __init__(self, decl, state, runner=subprocess.run, timeout=30, cmd_timeout=20):
        self.decl = kvm_decl.normalize(decl)
        self.state = state
        self.runner = runner
        self.timeout = timeout
        # délai de CHAQUE commande d'un batch, appliqué sur l'hôte par
        # `timeout` quand il y existe
        self.cmd_timeout = cmd_timeout
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

    def _ssh(self, remote_argv, timeout, data=None):
        argv = ssh_argv(self.decl, self.state, remote_argv)
        try:
            kvm_decl.ensure_ssh_dir(self.state)
        except OSError:
            pass                            # ssh dira lui-même ce qui manque
        # stdin fermé : ssh ne lit jamais l'entrée de la console ; `data`
        # (run_input) est la seule entrée jamais transmise
        feed = {"stdin": subprocess.DEVNULL} if data is None else {"input": data}
        try:
            return self.runner(argv, **feed, capture_output=True, text=True,
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
            raise make_error(self.name, res.stderr, res.returncode, self._secret_paths(),
                             ssh_level=res.returncode == SSH_RC)
        return res.stdout or ""

    def run_input(self, remote_argv, data, timeout=None):
        """Comme `run()`, mais `data` (texte) part sur l'entrée standard de la
        commande distante. Sert à `virsh define /dev/stdin` : le XML, qui peut
        porter le mot de passe VNC, ne passe ni par un fichier temporaire ni
        par la ligne de commande. Le message d'erreur ne cite jamais `data`."""
        if not isinstance(data, str):
            raise TypeError("data must be a str")
        res = self._ssh(remote_argv, timeout, data=data)
        if res.returncode != 0:
            raise make_error(self.name, res.stderr, res.returncode, self._secret_paths(),
                             ssh_level=res.returncode == SSH_RC)
        return res.stdout or ""

    def virsh(self, *args, timeout=None):
        """Une commande virsh ; rend sa sortie, KvmError classée sinon."""
        return self.run(["env", "LC_ALL=C", *self._virsh_argv(args)], timeout=timeout)

    def batch_script(self, items, token, cmd_timeout):
        """Script `sh` d'un batch : une ligne repère de départ (ce que le
        shell distant affiche avant, un `echo` de .bashrc par exemple, est
        jeté), puis chaque commande suivie de `<jeton> <i> <rc>` sur stdout
        et `<jeton> <i>` sur stderr. Chaque commande passe par
        `timeout -k 5 <N>` si l'hôte l'a (et comprend `-k`) : une commande
        bloquée rend son propre échec au lieu d'emporter tout le lot."""
        lines = [
            "export LC_ALL=C",
            f"T={token}",
            "W=; S=0",
            "if command -v timeout >/dev/null 2>&1 && timeout -k 1 5 true </dev/null "
            f">/dev/null 2>&1; then W='timeout -k 5 {int(cmd_timeout)}'; S=1; fi",
            '_kvm_run() { i=$1; shift; $W "$@" </dev/null; rc=$?; '
            "printf '\\n%s %d %d\\n' \"$T\" \"$i\" \"$rc\"; "
            "printf '\\n%s %d\\n' \"$T\" \"$i\" >&2; }",
            "printf '%s start %d\\n' \"$T\" \"$S\"; printf '%s start\\n' \"$T\" >&2",
        ]
        for i, (_, args) in enumerate(items):
            lines.append(f"_kvm_run {i} " + " ".join(shlex.quote(a)
                                                     for a in self._virsh_argv(args)))
        return "\n".join(lines)

    def batch(self, commands, timeout=None, cmd_timeout=None):
        """Plusieurs commandes virsh en UN appel SSH. `commands` : un dict
        {clé: (arguments virsh)} ou une suite de tuples (clé = arguments
        joints par des espaces). Rend {clé: CmdOutput} ; une commande en échec
        n'efface pas les autres, sa valeur porte son erreur (`.error`), y
        compris `timeout` quand elle dépasse `cmd_timeout` sur l'hôte.

        Le jeton des repères est tiré au hasard à chaque appel : une sortie
        ne peut pas l'imiter. Délai de l'appel ssh entier : `timeout`, par
        défaut celui de l'hôte plus un délai de commande (une commande
        bloquée ne fait pas perdre les autres)."""
        if isinstance(commands, dict):
            items = [(str(k), tuple(map(str, v))) for k, v in commands.items()]
        else:
            items = [(" ".join(map(str, c)), tuple(map(str, c))) for c in commands]
        if not items:
            return {}
        # `timeout 0` voudrait dire « sans délai » : au moins une seconde
        cmd_timeout = max(1, int(cmd_timeout or self.cmd_timeout))
        token = "@@kvm-" + secrets.token_hex(12)
        res = self._ssh(["sh", "-c", self.batch_script(items, token, cmd_timeout)],
                        timeout or (self.timeout + cmd_timeout))
        tok = re.escape(token)
        paths = self._secret_paths()
        m_out = re.search(rf"(?:^|\n){tok} start (\d+)\n", res.stdout or "")
        if not m_out:
            # pas de repère de départ : le script n'a pas tourné ; en 255,
            # c'est ssh (hôte, empreinte, clé) et stderr est le sien
            raise make_error(self.name, res.stderr, res.returncode, paths,
                             ssh_level=res.returncode == SSH_RC)
        wrapped = m_out.group(1) == "1"
        stdout = (res.stdout or "")[m_out.end():]
        m_err = re.search(rf"(?:^|\n){tok} start\n", res.stderr or "")
        stderr = (res.stderr or "")[m_err.end():] if m_err else ""
        out_parts = re.split(rf"\n{tok} (\d+) (-?\d+)\n", stdout)
        err_parts = re.split(rf"\n{tok} (\d+)\n", stderr)
        outs, rcs = {}, {}
        for j in range(0, len(out_parts) - 2, 3):
            idx = int(out_parts[j + 1])
            outs[idx], rcs[idx] = out_parts[j], int(out_parts[j + 2])
        errs = {}
        for j in range(0, len(err_parts) - 1, 2):
            errs[int(err_parts[j + 1])] = err_parts[j]
        result = {}
        for i, (key, _) in enumerate(items):
            if i not in rcs:                 # coupure en cours de route
                err = KvmError("failed", f"{self.name}: failed: no output for '{key}' "
                               f"(connection lost, ssh status {res.returncode})",
                               host=self.name, rc=res.returncode)
                result[key] = CmdOutput(err.message, rc=-1, error=err)
            elif rcs[i] != 0 and wrapped and rcs[i] in _TIMEOUT_RCS:
                err = KvmError("timeout", f"{self.name}: timeout: '{key}' did not finish "
                               f"within {cmd_timeout}s on the host", host=self.name, rc=rcs[i])
                result[key] = CmdOutput(err.message, rc=rcs[i], error=err)
            elif rcs[i] != 0:
                # erreur d'UNE commande : repères de virsh/libvirt seulement
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


def _pci_address(el):
    """`<address domain= bus= slot= function=>` -> « 0000:01:00.0 »."""
    if el is None:
        return None
    try:
        dom, bus, slot, fn = (int(el.get(a, "0"), 0) for a in ("domain", "bus", "slot", "function"))
    except ValueError:
        return None
    return f"{dom:04x}:{bus:02x}:{slot:02x}.{fn:x}"


def _first_attr(el, attrs):
    for attr in attrs:
        if el.get(attr):
            return el.get(attr)
    return None


def _disk_source(disk):
    """Source d'un disque selon son type (libvirt formatdomain, « Hard drives,
    floppy disks, CDROMs ») ; None pour un lecteur vide."""
    src = disk.find("source")
    if src is None:
        return None
    dtype = disk.get("type")
    if dtype == "file":
        return src.get("file")
    if dtype == "block":
        return src.get("dev")
    if dtype == "dir":
        return src.get("dir")
    if dtype == "network":
        proto, name = src.get("protocol"), src.get("name")
        return f"{proto}:{name}" if proto and name else (name or proto)
    if dtype == "volume":
        pool, vol = src.get("pool"), src.get("volume")
        return f"{pool}/{vol}" if pool and vol else (vol or pool)
    if dtype == "nvme":
        return _pci_address(src.find("address"))
    if dtype == "vhostuser":
        return src.get("path")
    # type inconnu : au mieux
    return _first_attr(src, ("file", "dev", "dir", "path", "name", "volume"))


def _nic_source(iface):
    """Source d'une carte selon son type (libvirt formatdomain, « Network
    interfaces ») : le XML vivant d'une carte `network` porte aussi
    `bridge='virbr0'`, c'est le réseau qui compte."""
    itype = iface.get("type")
    src = iface.find("source")
    if itype == "user" or src is None:
        return None
    if itype == "network":
        return src.get("network")
    if itype == "bridge":
        return src.get("bridge")
    if itype == "direct":
        return src.get("dev")
    if itype in ("vhostuser", "vdpa"):
        return src.get("path") or src.get("dev")
    if itype == "hostdev":
        return _pci_address(src.find("address"))
    # ethernet, server, client, mcast, udp... : au mieux
    return _first_attr(src, ("network", "bridge", "dev", "path", "address"))


def _backing_chain(disk):
    """Chemins de la chaîne `<backingStore>` d'un disque (XML vivant : libvirt
    la décrit niveau par niveau), du plus proche au plus lointain. Vide pour
    un XML inactif ou un disque sans image de base."""
    chain, cur = [], disk
    for _ in range(64):                     # garde-fou contre une boucle
        bs = cur.find("backingStore")
        if bs is None or bs.find("source") is None:
            break
        src = _disk_source(bs)
        if src:
            chain.append(src)
        cur = bs
    return chain


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
        chain = _backing_chain(d)
        disks.append({"target": tgt.get("dev") if tgt is not None else None,
                      "device": d.get("device", "disk"), "type": d.get("type"),
                      "source": _disk_source(d),
                      "format": drv.get("type") if drv is not None else None,
                      "bus": tgt.get("bus") if tgt is not None else None,
                      "readonly": d.find("readonly") is not None,
                      "shareable": d.find("shareable") is not None,
                      "transient": d.find("transient") is not None,
                      "backing": chain[0] if chain else None,
                      "backing_chain": chain})
    nics = []
    for n in devices.findall("interface"):
        mac, model = n.find("mac"), n.find("model")
        nics.append({"type": n.get("type"), "source": _nic_source(n),
                     "mac": mac.get("address") if mac is not None else None,
                     "model": model.get("type") if model is not None else None})
    graphics = []
    for g in devices.findall("graphics"):
        sub = g.find("listen")
        listen = g.get("listen")
        if not listen:
            listen = sub.get("address") if sub is not None else None
        socket = g.get("socket") or (sub.get("socket") if sub is not None else None)
        if sub is not None and sub.get("type"):
            listen_type = sub.get("type")
        elif socket:
            listen_type = "socket"
        else:
            listen_type = "address"
        port = _int(g.get("port"))
        # `password` : présence seulement (XML lu par --security-info), jamais la valeur
        graphics.append({"type": g.get("type"), "port": port if port and port > 0 else None,
                         "listen": listen, "autoport": _yes(g.get("autoport")),
                         "listen_type": listen_type, "socket": socket,
                         "password": bool(g.get("passwd"))})
    nvram = None
    nv = os_el.find("nvram") if os_el is not None else None
    if nv is not None:
        nvram = {"path": (nv.text or "").strip() or None, "format": nv.get("format"),
                 "template": nv.get("template")}
    return {"name": root.findtext("name"), "uuid": root.findtext("uuid"),
            "domain_type": root.get("type"), "id": _int(root.get("id")),
            "vcpus": _int(vcpu.text) if vcpu is not None else None,
            "memory_kib": _to_kib(root.find("memory")),
            "current_memory_kib": _to_kib(root.find("currentMemory")),
            "cpu_pinned": pinned, "cpu_realtime": rt,
            "hugepages": root.find("memoryBacking/hugepages") is not None,
            "machine": os_type.get("machine") if os_type is not None else None,
            "arch": os_type.get("arch") if os_type is not None else None,
            "firmware": firmware, "disks": disks, "nics": nics, "graphics": graphics,
            "nvram": nvram, "hostdevs": len(devices.findall("hostdev")),
            "seclabels_static": any(sl.get("type") == "static" for sl in root.findall("seclabel")),
            "sysinfo_uuid": any(e.get("name") == "uuid"
                                for e in root.findall("sysinfo/system/entry"))}


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


def _parse_wide_table(text):
    """Tableau de virsh dont un titre contient une espace (« Creation Time ») :
    virsh sépare les colonnes par au moins deux espaces, les titres sont donc
    les suites de mots séparés par une seule."""
    lines = (text or "").splitlines()
    head = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if head is None or head + 1 >= len(lines) or not set(lines[head + 1].strip()) <= {"-"}:
        return []
    cols = [(m.start(), m.group(0)) for m in re.finditer(r"\S+(?: \S+)*", lines[head])]
    rows = []
    for ln in lines[head + 2:]:
        if not ln.strip():
            continue
        rows.append({title: ln[start:(cols[n + 1][0] if n + 1 < len(cols) else None)].strip()
                     for n, (start, title) in enumerate(cols)})
    return rows


def _iso_time(text):
    """« 2026-10-24 11:39:00 +0200 » -> « 2026-10-24T11:39:00+02:00 » ; le
    texte tel quel s'il ne se lit pas."""
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z").isoformat()
    except (TypeError, ValueError):
        return text or None


def parse_snapshot_list(text):
    """`virsh snapshot-list --parent` -> [{"name", "created", "state",
    "parent"}] ; `state` = état du domaine à la prise (`running` : mémoire
    comprise, `shutoff` : disques seuls), `parent` None pour une racine.
    Un nom d'instantané qui contient deux espaces de suite se lirait mal :
    `list_snapshots` le recoupe avec la liste `--name`."""
    out = []
    for r in _parse_wide_table(text):
        out.append({"name": r.get("Name", ""), "created": _iso_time(r.get("Creation Time")),
                    "state": r.get("State") or None, "parent": r.get("Parent") or None})
    return out


def _bytes(el):
    if el is None or _int(el.text) is None:
        return None
    return _int(el.text) * _XML_UNITS.get((el.get("unit") or "bytes").lower(), 1)


def parse_vol_xml(text):
    """XML de `virsh vol-dumpxml` -> {"name", "key", "path", "format",
    "capacity", "allocation", "backing"} (octets ; `backing` = chemin de
    l'image de base, None sans)."""
    root = _safe_xml(text)
    fmt = root.find("target/format")
    return {"name": root.findtext("name"), "key": root.findtext("key"),
            "path": root.findtext("target/path"),
            "format": fmt.get("type") if fmt is not None else None,
            "capacity": _bytes(root.find("capacity")),
            "allocation": _bytes(root.find("allocation")),
            "backing": (root.findtext("backingStore/path") or "").strip() or None}


def parse_pool_xml(text):
    """XML de `virsh pool-dumpxml` -> {"name", "type", "target_path",
    "available"} (octets)."""
    root = _safe_xml(text)
    return {"name": root.findtext("name"), "type": root.get("type"),
            "target_path": (root.findtext("target/path") or "").strip() or None,
            "available": _bytes(root.find("available"))}


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

# --nowait : un domaine dont le moniteur QEMU est occupé (job en cours) ne
# bloque pas la lecture, ses statistiques sont rendues partielles
DOMSTATS_GROUPS = ("--state", "--cpu-total", "--balloon", "--vcpu", "--interface", "--block")

INVENTORY_COMMANDS = {
    "hostname": ("hostname",),
    "nodeinfo": ("nodeinfo",),
    "nodememstats": ("nodememstats",),
    "version": ("version",),
    "list": ("list", "--all"),
    "autostart": ("list", "--all", "--autostart", "--name"),
    "transient": ("list", "--all", "--transient", "--name"),
    "domstats": ("domstats", "--raw", "--nowait", "--state", "--cpu-total", "--balloon", "--vcpu"),
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
                      "stats": ("domstats", "--raw", "--nowait", *DOMSTATS_GROUPS,
                                "--domain", name)})
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
