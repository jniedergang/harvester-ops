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
import html
import ipaddress
import os
import re
import secrets
import select
import shlex
import subprocess
import tempfile
import time
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

# Reprise d'une LECTURE coupée au niveau du transport (ssh rend 255 sans que
# la commande ait abouti). Vu en réel le 24/10/2026 : deux lectures lancées
# ensemble sur le même ControlMaster ont fait tomber sshd 10.2p1 de l'hôte
# (désynchronisation du moniteur privsep, « mm_request_receive: read: bad
# msg_len »), le maître est mort avec la session en vol. Une lecture se
# rejoue sans risque ; une écriture jamais (on ne sait pas si elle a eu
# lieu). La reprise part sur une connexion neuve, hors du maître (mort).
READ_RETRIES = 1
READ_RETRY_DELAY = 0.5
MAX_READ_RETRIES = 3
# échecs de ssh qui ne sont pas une coupure : les rejouer ne changerait rien
# (empreinte, clé) ou ferait attendre un second ConnectTimeout (hôte absent)
_NO_RETRY_SSH = (
    *_SSH_MARKERS[0][1], *_SSH_MARKERS[1][1],
    "connection timed out", "no route to host", "could not resolve hostname",
    "network is unreachable", "operation timed out",
)


def transport_lost(rc, stderr):
    """Vrai si ssh a rendu 255 pour une coupure de la connexion (session
    fermée, maître disparu, connexion réinitialisée) et non pour une
    empreinte, une clé ou un hôte injoignable."""
    if rc != SSH_RC:
        return False
    low = (stderr or "").lower()
    return not any(m in low for m in _NO_RETRY_SSH)

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


class Cancelled(Exception):
    """Opération arrêtée par l'annulation (`cancel()` vrai) : pour un envoi
    en flux, ssh a été arrêté et `sent` octets étaient partis. Ce n'est pas
    une KvmError : rien n'a échoué côté hôte, l'appelant défait ce qu'il
    avait commencé."""

    def __init__(self, sent=0, message=None):
        super().__init__(message or f"cancelled after {sent} bytes sent")
        self.sent = sent


class CmdOutput(str):
    """Sortie d'une commande d'un `batch` : une chaîne (la sortie, ou le
    texte d'erreur en cas d'échec), avec `rc` et `error` (KvmError ou None)."""

    def __new__(cls, text, rc=0, error=None, stderr=""):
        obj = super().__new__(cls, text)
        obj.rc, obj.error = rc, error
        # 2.0.0 : stderr d'une commande réussie (une commande virsh à
        # plusieurs sous-commandes rend 0 même si l'une d'elles a échoué)
        obj.stderr = stderr
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


# attribut `passwd` d'un XML de libvirt (graphics VNC/SPICE), même tronqué :
# sa valeur ne doit jamais apparaître dans une erreur
_PASSWD_ATTR_RE = re.compile(r"""(passwd\s*=\s*)(['"])[^'"\n]*\2?""")


def input_secrets(data, known=()):
    """Valeurs secrètes d'une entrée (XML) : les `passwd` lus dans le texte,
    sous leur forme brute et décodée (entités XML), plus `known` (valeurs déjà
    analysées par l'appelant)."""
    found = {str(v) for v in known if v}
    for m in re.finditer(r"""passwd\s*=\s*(['"])([^'"]*)\1""", data or ""):
        if m.group(2):
            found.add(m.group(2))
            found.add(html.unescape(m.group(2)))
    return sorted((v for v in found if v), key=len, reverse=True)


def scrub_secrets(text, secrets=()):
    """Texte sans aucune occurrence des valeurs secrètes connues ni d'un
    attribut `passwd='...'`/`passwd="..."`."""
    text = text or ""
    for v in secrets or ():
        if v:
            text = text.replace(v, "***")
    return _PASSWD_ATTR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***{m.group(2)}", text)


def _error_detail(stderr, paths):
    """Dernière ligne utile de stderr, chemins de l'état masqués."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.lower().startswith(("warning:", "debug"))] or lines
    return _redact(lines[-1], paths)[:300] if lines else ""


def make_error(host, stderr, rc, paths=(), ssh_level=False, secrets=()):
    """KvmError classée. `ssh_level` : l'échec vient de ssh (code 255, stderr
    de ssh) ; sinon de la commande distante. `secrets` : valeurs retirées du
    texte avant tout (un attribut `passwd` l'est toujours)."""
    stderr = scrub_secrets(stderr, secrets)
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
        # rien que les erreurs sur stderr : la bannière de connexion d'un
        # sshd durci (`Banner /etc/issue.net`, vue sur node2) ne doit jamais
        # entrer dans un message d'erreur lu sur la sortie d'erreur
        "-o", "LogLevel=ERROR",
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


def ssh_argv(decl, state, remote_argv, fresh=False):
    """Ligne de commande ssh qui lance `remote_argv` sur l'hôte. Chaque
    élément distant est cité (shlex.quote) et le tout part en UN argument :
    le shell distant le redécoupe exactement en `remote_argv`. `fresh` :
    connexion directe, sans passer par le ControlMaster ni en créer un
    (reprise d'une lecture après la mort du maître). KvmError (`failed`)
    pour une déclaration ou un chemin rangé invalide."""
    remote = " ".join(shlex.quote(str(a)) for a in remote_argv)
    base = _ssh_base(decl, state)
    if fresh:
        base[base.index("ControlMaster=auto")] = "ControlMaster=no"
        i = next(i for i, a in enumerate(base) if a.startswith("ControlPath="))
        base[i] = "ControlPath=none"
    return [*base, "--", decl["address"], remote]


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


# envoi en flux (run_stream) : taille d'un bloc lu dans la source, morceau
# écrit dans le tuyau entre deux contrôles de l'annulation, délai laissé à
# ssh pour s'arrêter avant SIGKILL, attente de la fin de la commande
# distante une fois tout envoyé (libvirt vide ses tampons)
STREAM_CHUNK = 4 << 20
STREAM_PIECE = 64 << 10
STREAM_KILL_GRACE = 2.0
STREAM_FINISH_TIMEOUT = 600
# une seconde entre deux appels de `progress`
STREAM_PROGRESS_EVERY = 1.0
# débit plafonné : crédit du seau à jetons borné à ce temps d'envoi. Le temps
# passé avant que ssh lise (échange de clés) n'est jamais rendu en rafale :
# vu en réel, 7,2 Mio/s mesurés pour 40 Mbit/s demandés.
STREAM_BURST_S = 0.25


class VirshHost:
    """Un hôte KVM déclaré, interrogé par `virsh` à travers SSH."""

    def __init__(self, decl, state, runner=subprocess.run, timeout=30, cmd_timeout=20,
                 read_retries=READ_RETRIES, retry_delay=READ_RETRY_DELAY, sleep=time.sleep,
                 popen=subprocess.Popen, clock=time.monotonic):
        # `popen` et `clock` : remplaçables par les tests de run_stream
        self.popen = popen
        self.clock = clock
        self.decl = kvm_decl.normalize(decl)
        self.state = state
        self.runner = runner
        self.timeout = timeout
        # délai de CHAQUE commande d'un batch, appliqué sur l'hôte par
        # `timeout` quand il y existe
        self.cmd_timeout = cmd_timeout
        # reprises d'une lecture idempotente coupée (borné), attente
        # croissante entre deux essais
        self.read_retries = max(0, min(int(read_retries), MAX_READ_RETRIES))
        self.retry_delay = max(0.0, float(retry_delay))
        self.sleep = sleep
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

    def _ssh(self, remote_argv, timeout, data=None, fresh=False):
        argv = ssh_argv(self.decl, self.state, remote_argv, fresh=fresh)
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

    def _retry(self, attempt):
        """Attend avant la reprise n° `attempt` (1, 2, ...)."""
        self.sleep(self.retry_delay * attempt)

    def run(self, remote_argv, timeout=None, idempotent=False, fresh=False):
        """Lance une commande quelconque sur l'hôte ; rend sa sortie.
        `idempotent=True` (lecture seulement) : une coupure du transport est
        rejouée `read_retries` fois sur une connexion neuve. Une écriture
        (défaut) n'est jamais rejouée : son issue est inconnue. `fresh` :
        connexion propre dès le premier essai (commande longue)."""
        res = self._ssh(remote_argv, timeout, fresh=fresh)
        attempt = 0
        while (idempotent and res.returncode != 0 and attempt < self.read_retries
               and transport_lost(res.returncode, res.stderr)):
            attempt += 1
            self._retry(attempt)
            res = self._ssh(remote_argv, timeout, fresh=True)
        if res.returncode != 0:
            raise make_error(self.name, res.stderr, res.returncode, self._secret_paths(),
                             ssh_level=res.returncode == SSH_RC)
        return res.stdout or ""

    def run_input(self, remote_argv, data, timeout=None, secrets=(), fresh=False):
        """Comme `run()`, mais `data` (texte) part sur l'entrée standard de la
        commande distante. Sert à `virsh define /dev/stdin` : le XML, qui peut
        porter le mot de passe VNC, ne passe ni par un fichier temporaire ni
        par la ligne de commande. Une erreur qui recopierait l'entrée est
        expurgée : chaque `passwd` lu dans `data`, chaque valeur de `secrets`
        et tout attribut `passwd=` sont masqués avant de bâtir le message.
        `fresh=True` : connexion propre, hors de la connexion partagée, pour
        une écriture longue qu'un plantage de sshd sous sessions multiplexées
        simultanées (OpenSSH 10.2p1) ne doit pas couper."""
        if not isinstance(data, str):
            raise TypeError("data must be a str")
        res = self._ssh(remote_argv, timeout, data=data, fresh=fresh)
        if res.returncode != 0:
            raise make_error(self.name, res.stderr, res.returncode, self._secret_paths(),
                             ssh_level=res.returncode == SSH_RC,
                             secrets=input_secrets(data, secrets))
        return res.stdout or ""

    def run_stream(self, remote_argv, source, size=None, progress=None, cancel=None,
                   limit_bps=None, finish_timeout=STREAM_FINISH_TIMEOUT):
        """Envoie `source` (chemin ou fichier binaire ouvert) sur l'entrée
        standard de `remote_argv`, lancé sur une connexion PROPRE
        (`ControlMaster=no`, `ControlPath=none`) : une écriture longue ne
        passe jamais par la connexion partagée, qu'un plantage de sshd sous
        sessions multiplexées couperait, et elle ne bloque pas les lectures.

        Écrit par morceaux dans le tuyau de ssh (non bloquant) : `cancel()`
        est consulté entre deux morceaux et pendant une attente, une entrée
        bouchée ne fige donc jamais l'annulation. `progress(octets)` au plus
        une fois par seconde, et une dernière fois à la fin. `limit_bps` :
        débit plafonné (seau à jetons). `size` : octets attendus ; une source
        plus courte est une erreur (le volume serait incomplet).

        Annulation : entrée fermée, SIGTERM, `STREAM_KILL_GRACE` secondes,
        SIGKILL ; lève Cancelled. Échec de la commande ou de ssh :
        KvmError classée comme run(), chemins de l'état masqués. Jamais
        rejoué (écriture). Rend la sortie standard de la commande."""
        if limit_bps is not None and (isinstance(limit_bps, bool) or not isinstance(
                limit_bps, (int, float)) or limit_bps <= 0):
            raise ValueError("limit_bps must be a positive number")
        argv = ssh_argv(self.decl, self.state, remote_argv, fresh=True)
        try:
            kvm_decl.ensure_ssh_dir(self.state)
        except OSError:
            pass
        own = not hasattr(source, "read")
        src = open(source, "rb") if own else source
        out_f, err_f = tempfile.TemporaryFile(), tempfile.TemporaryFile()
        try:
            proc = None
            try:
                # lancement DANS le bloc gardé : un signal reçu juste après
                # popen() arrête quand même ssh
                try:
                    proc = self.popen(argv, stdin=subprocess.PIPE, stdout=out_f,
                                      stderr=err_f, bufsize=0)
                except OSError as e:
                    raise KvmError("failed", f"{self.name}: failed: cannot run ssh "
                                   f"({e.strerror})", host=self.name) from e
                sent, broken = self._feed(proc, src, progress, cancel, limit_bps)
                try:
                    rc = proc.wait(timeout=finish_timeout)
                except subprocess.TimeoutExpired:
                    self._stop(proc)
                    raise KvmError("timeout", f"{self.name}: timeout: the host did not finish "
                                   f"within {finish_timeout}s after the last byte",
                                   host=self.name) from None
            except BaseException:
                # signal de l'appelant (annulation par SIGTERM levée dans ce fil)
                # ou erreur : ssh n'est jamais laissé en vie derrière nous
                if proc is not None:
                    self._stop(proc)
                raise
            out_f.seek(0)
            err_f.seek(0)
            stdout = out_f.read().decode("utf-8", "replace")
            stderr = err_f.read().decode("utf-8", "replace")
            if rc != 0:
                raise make_error(self.name, stderr, rc, self._secret_paths(),
                                 ssh_level=rc == SSH_RC)
            if broken or (size is not None and sent != size):
                what = (f"the host stopped reading after {sent} bytes" if broken else
                        f"{sent} bytes sent, {size} expected: the source changed")
                raise KvmError("failed", f"{self.name}: failed: {what}", host=self.name)
            return stdout
        finally:
            out_f.close()
            err_f.close()
            if own:
                src.close()

    def _stop(self, proc):
        """Arrête ssh : SIGTERM, attente courte, SIGKILL."""
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STREAM_KILL_GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def _feed(self, proc, src, progress, cancel, limit_bps):
        """Boucle d'écriture de run_stream ; rend (octets envoyés, tuyau
        rompu). Lève Cancelled après avoir arrêté ssh."""
        fd = proc.stdin.fileno()
        os.set_blocking(fd, False)
        sent, broken = 0, False
        last = self.clock()
        # seau à jetons : horloge lancée à la première écriture réussie,
        # crédit plafonné à STREAM_BURST_S secondes d'envoi
        burst = max(limit_bps * STREAM_BURST_S, 1) if limit_bps else 0
        tokens, tick = 0.0, None
        # zéro octet au départ du flux : la mesure de débit de l'appelant part
        # d'ici, pas de son premier rappel (vu en réel : 2,8 Mo comptés sur un
        # temps nul, un débit absurde affiché la première seconde)
        if progress is not None:
            progress(0)

        def cancelled():
            if cancel is not None and cancel():
                self._stop(proc)
                raise Cancelled(sent)

        while not broken:
            cancelled()
            block = src.read(STREAM_CHUNK)
            if not block:
                break
            view = memoryview(block)
            while view and not broken:
                cancelled()
                piece = view[:STREAM_PIECE]
                if limit_bps:
                    piece = view[:max(1, min(len(piece), int(limit_bps / 5) or 1))]
                    if tick is not None:
                        now = self.clock()
                        tokens = min(burst, tokens + (now - tick) * limit_bps)
                        tick = now
                        if tokens < len(piece):
                            self.sleep(min((len(piece) - tokens) / limit_bps, 0.2))
                            continue
                _, ready, _ = select.select([], [fd], [], 0.5)
                if not ready:
                    if proc.poll() is not None:
                        broken = True       # ssh parti sans tout lire
                    continue
                try:
                    n = os.write(fd, piece)
                except BlockingIOError:
                    continue
                except (BrokenPipeError, OSError):
                    broken = True
                    break
                sent += n
                view = view[n:]
                now = self.clock()
                if limit_bps:
                    if tick is None:
                        tick = now          # première écriture : l'horloge part
                    else:
                        tokens -= n
                if progress is not None and now - last >= STREAM_PROGRESS_EVERY:
                    last = now
                    progress(sent)
        cancelled()
        try:
            proc.stdin.close()
        except OSError:
            broken = True
        if progress is not None:
            progress(sent)
        return sent, broken

    def virsh(self, *args, timeout=None, idempotent=False):
        """Une commande virsh ; rend sa sortie, KvmError classée sinon.
        `idempotent=True` pour une lecture (voir `run`)."""
        return self.run(["env", "LC_ALL=C", *self._virsh_argv(args)], timeout=timeout,
                        idempotent=idempotent)

    @staticmethod
    def _batch_lost(res, m_out, tok, count):
        """Lot coupé par le transport : pas de repère de départ et ssh en
        coupure, ou départ vu mais ssh en 255 avant la fin de la dernière
        commande."""
        if not m_out:
            return transport_lost(res.returncode, res.stderr)
        if res.returncode != SSH_RC:
            return False
        done = re.findall(rf"\n{tok} \d+ -?\d+\n", (res.stdout or "")[m_out.end():])
        return len(done) < count

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

    def batch(self, commands, timeout=None, cmd_timeout=None, idempotent=False):
        """Plusieurs commandes virsh en UN appel SSH. `commands` : un dict
        {clé: (arguments virsh)} ou une suite de tuples (clé = arguments
        joints par des espaces). Rend {clé: CmdOutput} ; une commande en échec
        n'efface pas les autres, sa valeur porte son erreur (`.error`), y
        compris `timeout` quand elle dépasse `cmd_timeout` sur l'hôte.

        Le jeton des repères est tiré au hasard à chaque appel : une sortie
        ne peut pas l'imiter. Délai de l'appel ssh entier : `timeout`, par
        défaut celui de l'hôte plus un délai de commande (une commande
        bloquée ne fait pas perdre les autres).

        `idempotent=True` (lot de lectures seulement) : un lot coupé au
        niveau du transport (ssh en 255 sans repère de départ, ou avant la
        dernière commande) est rejoué en entier, `read_retries` fois, sur une
        connexion neuve. Un lot qui écrit (défaut) ne l'est jamais."""
        if isinstance(commands, dict):
            items = [(str(k), tuple(map(str, v))) for k, v in commands.items()]
        else:
            items = [(" ".join(map(str, c)), tuple(map(str, c))) for c in commands]
        if not items:
            return {}
        # `timeout 0` voudrait dire « sans délai » : au moins une seconde
        cmd_timeout = max(1, int(cmd_timeout or self.cmd_timeout))
        token = "@@kvm-" + secrets.token_hex(12)
        script = self.batch_script(items, token, cmd_timeout)
        tok = re.escape(token)
        paths = self._secret_paths()
        attempt = 0
        while True:
            res = self._ssh(["sh", "-c", script], timeout or (self.timeout + cmd_timeout),
                            fresh=attempt > 0)
            m_out = re.search(rf"(?:^|\n){tok} start (\d+)\n", res.stdout or "")
            if not (idempotent and attempt < self.read_retries
                    and self._batch_lost(res, m_out, tok, len(items))):
                break
            attempt += 1
            self._retry(attempt)
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
                result[key] = CmdOutput(outs[i], stderr=_redact(errs.get(i, ""), paths))
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


def parse_uuid_names(text):
    """`virsh list --all --uuid --name` -> {nom: uuid} (une ligne « uuid nom »
    par domaine, ligne vide finale ignorée)."""
    out = {}
    for ln in (text or "").splitlines():
        # virsh écrit « uuid nom » avec UNE espace : le nom est gardé tel quel,
        # espaces de tête ou de fin comprises
        uid, sep, name = ln.lstrip().partition(" ")
        if sep and name and UUID_RE.match(uid):
            out[name] = uid.lower()
    return out


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


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
        # deux formes (formatdomain, « BIOS bootloader ») : chemin en texte, ou
        # `<nvram type='file|block|network'><source file=|dev=|name=/></nvram>`
        nv_src = nv.find("source")
        nv_path = (nv.text or "").strip() or None
        if nv_path is None and nv_src is not None:
            nv_path = _first_attr(nv_src, ("file", "dev", "name"))
        nvram = {"path": nv_path, "format": nv.get("format"), "template": nv.get("template"),
                 "type": nv.get("type") or "file"}
    tpm = None
    tpm_el = devices.find("tpm")
    if tpm_el is not None:
        be = tpm_el.find("backend")
        be_src = be.find("source") if be is not None else None
        tpm = {"model": tpm_el.get("model"),
               "backend": be.get("type") if be is not None else None,
               "version": be.get("version") if be is not None else None,
               "persistent_state": _yes(be.get("persistent_state")) if be is not None else False,
               # source explicite de l'état (fichier ou répertoire choisi à la main)
               "state_source": (_first_attr(be_src, ("path", "file", "dir"))
                                if be_src is not None else None)}
    genid = root.find("genid")
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
            "nvram": nvram,
            # une carte `<interface type='hostdev'>` est aussi un périphérique de l'hôte
            "hostdevs": len(devices.findall("hostdev")) + sum(
                1 for n in devices.findall("interface") if n.get("type") == "hostdev"),
            "tpm": tpm,
            "genid": None if genid is None else ((genid.text or "").strip() or True),
            "seclabels_static": any(sl.get("type") == "static" for sl in root.findall("seclabel")),
            "sysinfo_uuid": any(e.get("name") == "uuid"
                                for e in root.findall("sysinfo/system/entry")),
            "seed": _hops_seed(root)}


# espace de noms des métadonnées de la console (kvm_xml.HOPS_NS)
HOPS_NS = "https://harvester-ops.io/xmlns/vm/1"


def _hops_seed(root):
    """Volume cloud-init possédé par la VM (`<hops:seed pool volume>`), ou
    None : écrit par la console à la création, reconnu à la suppression."""
    el = root.find(f"metadata/{{{HOPS_NS}}}vm/{{{HOPS_NS}}}seed")
    if el is None or not el.get("pool") or not el.get("volume"):
        return None
    return {"pool": el.get("pool"), "volume": el.get("volume")}


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
            "backing": (root.findtext("backingStore/path") or "").strip() or None,
            # droits du fichier vus par libvirt (texte tel quel : mode octal, uid, gid)
            "mode": (root.findtext("target/permissions/mode") or "").strip() or None,
            "owner": (root.findtext("target/permissions/owner") or "").strip() or None,
            "group": (root.findtext("target/permissions/group") or "").strip() or None}


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


# --- dossiers (navigation 2.0) -----------------------------------------
#
# Le dossier d'une VM KVM est écrit DANS le domaine, en métadonnées libvirt :
#   <hops:folder xmlns:hops="https://harvester-ops.io/xmlns/folder/1" path="/Production/ERP"/>
# Il suit donc le domaine (redéfinition, clone, autre console). Mesuré sur
# node2 (15 domaines, libvirt 12.8.0, docs/design/2026-10-24-navigation-hotes-vms.md) :
# un `virsh metadata` par domaine coûte ~180 ms chacun (+2,7 s pour 15),
# un `dumpxml` par domaine +2,9 s (93 Ko) ; UNE invocation de virsh qui
# enchaîne les 15 lectures (« cmd; cmd; ... ») +0,3 s. C'est celle-ci,
# avec un repère `echo` aléatoire entre deux domaines.
FOLDER_NS = "https://harvester-ops.io/xmlns/folder/1"
FOLDER_PREFIX = "hops"
# lecture impossible (distincte de « pas de dossier », qui vaut None)
FOLDER_UNREAD = object()
# domaines par invocation de virsh : une chaîne de commandes reste loin de
# la borne d'un argument (128 Kio sous Linux)
FOLDER_CHUNK = 200
# libvirt < 12 répond « metadata not found » (code 1) pour un élément
# absent ; libvirt 12.8.0 rend 0 et une sortie vide (relevé sur node2)
_FOLDER_ABSENT_RE = re.compile(r"metadata not found|metadata element is not present", re.I)


def _virsh_word(text):
    """Argument entre apostrophes pour l'analyseur de commandes de virsh ;
    None si le nom porte un caractère qu'il faudrait échapper (il est alors
    lu à part, par une commande à lui)."""
    if any(c in text for c in "'\\\n\r") or any(ord(c) < 32 for c in text):
        return None
    return f"'{text}'"


def parse_folder_metadata(text):
    """Sortie de `virsh metadata --uri FOLDER_NS` -> chemin, ou None quand le
    domaine n'en porte pas (sortie vide). ValueError pour un XML illisible,
    un autre élément ou un élément sans `path`."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        root = _safe_xml(t)
    except ET.ParseError as e:
        raise ValueError(f"unreadable folder metadata ({e})") from None
    if root.tag.rsplit("}", 1)[-1] != "folder":
        raise ValueError("unexpected folder metadata element")
    path = root.get("path")
    if path is None:
        raise ValueError("folder metadata without a path")
    return path


def _absent_only(stderr):
    """True si chaque ligne d'erreur dit seulement « pas de métadonnées »."""
    lines = [ln for ln in (stderr or "").splitlines() if ln.strip()]
    return all(_FOLDER_ABSENT_RE.search(ln) or not ln.lower().startswith("error")
               for ln in lines)


def read_folders(host, names):
    """Dossier de chaque domaine nommé : ({nom: chemin ou None}, erreurs).
    Un domaine absent du dictionnaire n'a pas pu être lu (FOLDER_UNREAD pour
    l'appelant) ; son erreur est dans la liste. Une invocation de virsh par
    tranche de FOLDER_CHUNK domaines, toutes dans le même appel SSH ; si
    virsh signale une erreur autre que « pas de métadonnées », la tranche est
    relue domaine par domaine pour savoir lequel a échoué."""
    found, errors = {}, []
    simple = [n for n in names if _virsh_word(n)]
    retry = [n for n in names if not _virsh_word(n)]
    chunks = [simple[i:i + FOLDER_CHUNK] for i in range(0, len(simple), FOLDER_CHUNK)]
    tok = "@@hops-" + secrets.token_hex(8)
    cmds = {}
    for c, chunk in enumerate(chunks):
        parts = []
        for i, n in enumerate(chunk):
            parts += [f"echo {tok}-{i}", f"metadata --domain {_virsh_word(n)} --uri {FOLDER_NS}"]
        parts.append(f"echo {tok}-end")
        cmds[f"folders-{c}"] = ("; ".join(parts),)
    out = host.batch(cmds, idempotent=True) if cmds else {}
    for c, chunk in enumerate(chunks):
        res = out.get(f"folders-{c}")
        if res is None or not res.ok or not _absent_only(res.stderr):
            retry.extend(chunk)
            continue
        pieces = re.split(rf"(?m)^{re.escape(tok)}-(\d+|end)$", str(res))
        texts = {int(pieces[j]): pieces[j + 1] for j in range(1, len(pieces) - 1, 2)
                 if pieces[j] != "end"}
        for i, n in enumerate(chunk):
            if i not in texts:
                retry.append(n)
                continue
            try:
                found[n] = parse_folder_metadata(texts[i])
            except ValueError as e:
                errors.append({"command": f"metadata {n}", "kind": "failed",
                               "message": f"{host.name}: {n}: {e}"})
    if retry:
        outs = host.batch({n: ("metadata", "--domain", n, "--uri", FOLDER_NS) for n in retry},
                          idempotent=True)
        for n in retry:
            v = outs.get(n)
            if v is not None and not v.ok and _FOLDER_ABSENT_RE.search(str(v)):
                found[n] = None
            elif v is not None and v.ok:
                try:
                    found[n] = parse_folder_metadata(str(v))
                except ValueError as e:
                    errors.append({"command": f"metadata {n}", "kind": "failed",
                                   "message": f"{host.name}: {n}: {e}"})
            else:
                errors.append({"command": f"metadata {n}",
                               "kind": v.error.kind if v is not None else "failed",
                               "message": v.error.message if v is not None
                               else f"{host.name}: no answer for {n}"})
    return found, errors


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
    # 2.0.0 : l'UUID de chaque domaine, clé stable d'une VM KVM (un domaine
    # renommé la garde) pour la vue unique et les dossiers
    "uuids": ("list", "--all", "--uuid", "--name"),
    "autostart": ("list", "--all", "--autostart", "--name"),
    "transient": ("list", "--all", "--transient", "--name"),
    "domstats": ("domstats", "--raw", "--nowait", "--state", "--cpu-total", "--balloon", "--vcpu"),
}
DETAIL_COMMANDS = {
    "pools": ("pool-list", "--all", "--details"),
    "networks": ("net-list", "--all"),
}


def host_inventory(host, details=False, folders=False):
    """Résumé d'un hôte et de ses VMs en un appel `batch`. Une commande en
    échec laisse son champ vide et s'ajoute à `errors` ; seul un hôte
    injoignable (aucune commande lancée) ou dont toutes les commandes
    échouent (libvirt refusé) lève KvmError. `details` ajoute
    pools (avec leurs volumes, second appel) et réseaux. `folders` (2.0.0)
    ajoute le dossier de chaque VM (`folder`, lu dans ses métadonnées par
    `read_folders`, un appel de plus) ; une lecture de dossier en échec laisse
    `folder` à None, met `folder_unread` à True et s'ajoute à `errors`."""
    cmds = dict(INVENTORY_COMMANDS)
    if details:
        cmds.update(DETAIL_COMMANDS)
    out = host.batch(cmds, idempotent=True)
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
    uuids = parse_uuid_names(ok("uuids"))
    vms = []
    for d in parse_list_all(ok("list")):
        st = stats.get(d["name"], {})
        vms.append({"host": host.name, "name": d["name"], "id": d["id"], "state": d["state"],
                    "uuid": uuids.get(d["name"]),
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
    if folders and vms:
        found, ferrors = read_folders(host, [v["name"] for v in vms])
        errors.extend(ferrors)
        for v in vms:
            got = found.get(v["name"], FOLDER_UNREAD)
            v["folder"] = None if got is FOLDER_UNREAD else got
            v["folder_unread"] = got is FOLDER_UNREAD
    if details:
        pools = parse_pool_list_details(ok("pools"))
        active = [p["name"] for p in pools if p["state"] == "running"]
        vols = (host.batch({p: ("vol-list", "--pool", p, "--details") for p in active},
                           idempotent=True) if active else {})
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
                                "--domain", name)}, idempotent=True)
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
