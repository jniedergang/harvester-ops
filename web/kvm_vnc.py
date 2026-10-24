"""
harvester-ops : console VNC d'une VM KVM, par tunnel SSH (v1.97.0).

L'affichage d'une VM libvirt écoute sur l'hôte (en général 127.0.0.1:59xx).
La console l'atteint par `ssh -W <adresse>:<port>` : ssh relie son entrée et
sa sortie standard à ce port, vu depuis l'hôte, et ces deux tuyaux
remplissent le contrat amont de `vnc_mux` (`send`, `receive(timeout)`,
`connected`, `close`). Choix voulus :

  * aucun écouteur local : un `ssh -L` vers un socket local serait une porte
    VNC sans authentification sur la machine de la console, à nettoyer
    après un arrêt brutal ;
  * le `-W` passe par la connexion maître déjà ouverte par la lecture du
    XML (`ControlMaster=no`, même ControlPath) : pas de nouvelle poignée de
    main SSH par console, pas de pénalité `PerSourcePenalties` ;
  * empreinte épinglée (`StrictHostKeyChecking=yes`, known_hosts de la
    déclaration), comme toute commande vers l'hôte ;
  * la vie du tunnel est celle de la connexion partagée de `vnc_mux` : le
    dernier navigateur qui part la ferme, donc tue ssh. Un arrêt brutal de
    la console ferme le tuyau d'entrée de ssh, qui sort de lui-même.

Le port est relu dans le XML VIVANT à chaque ouverture (`autoport` le change
à chaque démarrage). Le mot de passe VNC éventuel est lu dans la même
définition (`dumpxml --security-info`), gardé en mémoire sur l'objet amont
le temps de la poignée de main (`vnc_mux` le remet à None), jamais renvoyé,
jamais journalisé.
"""

import os
import queue
import subprocess
import threading

import kvm_host as kh
import kvm_ops as ko

READ_CHUNK = 64 * 1024
STDERR_KEEP = 8 * 1024
_popen = subprocess.Popen          # remplaçable par les tests
CLOSE_GRACE = 2.0

# Raisons qui ne se corrigent pas d'elles-mêmes : la page ne réessaie pas.
TERMINAL_REASONS = ("spice", "socket", "nodisplay", "nolisten", "listen")

REASON_MESSAGES = {
    "notrunning": "the VM is not running: its display opens once it runs",
    "spice": "this VM has only a SPICE display: not supported; adding a VNC display will be "
             "possible from the VM settings in 1.98",
    "socket": "the display listens on a UNIX socket owned by root and qemu: the SSH user "
              "cannot reach it",
    "nodisplay": "this VM has no graphical display",
    "nolisten": "the display has no listener (listen type none)",
    "listen": "the display listens on an address that is not one of the host's own: not "
              "reachable through the host",
}


class DisplayUnavailable(Exception):
    """Pas d'affichage VNC joignable : `reason` parmi REASON_MESSAGES."""

    def __init__(self, reason):
        super().__init__(REASON_MESSAGES.get(reason, reason))
        self.reason = reason
        self.message = REASON_MESSAGES.get(reason, reason)


def read_display(vh, domain, want_password=False):
    """(état, cible, mot de passe) de l'affichage d'une VM : un `batch`
    (`domstate`, `dumpxml --security-info`), plus `ip -o addr show` quand
    l'affichage écoute sur une IP précise. `cible` est celle de
    `kvm_ops.display_target`. Le mot de passe n'est rendu qu'avec
    `want_password` (ouverture du tunnel) ; sinon seule sa présence compte.
    KvmError (transport, domaine absent) ou OpError (XML illisible)."""
    ko.check_vm_name(domain)
    out = vh.batch({"state": ("domstate", "--domain", domain),
                    "xml": ("dumpxml", "--security-info", "--domain", domain)}, idempotent=True)
    for k in ("state", "xml"):
        if not out[k].ok:
            raise out[k].error
    state = (str(out["state"]).strip().splitlines() or [""])[0].strip()
    xml = str(out["xml"])
    try:
        vm = kh.parse_domain_xml(xml)
    except (ValueError, SyntaxError):
        raise ko.OpError("failed", f"{vh.name}: unreadable definition for this VM") from None
    live = state != "shut off"
    target = ko.display_target(vm, live)
    if target.get("reason") == "listen":
        # IP précise : acceptée seulement si elle appartient à l'hôte
        target = ko.display_target(vm, live, host_addresses=ko.host_addresses(vh))
    password = None
    if want_password and target.get("password"):
        password = ko.vnc_password_of(xml)
    del xml
    return state, target, password


def classify_loss(vh, domain):
    """La connexion partagée est tombée sans nous : VM arrêtée ou disparue
    (`vm-stopped`), sinon `lost`."""
    try:
        state = (str(vh.virsh("domstate", "--domain", domain, idempotent=True)).strip().splitlines() or [""])[0]
    except kh.KvmError as e:
        return "vm-stopped" if e.kind == "notfound" else "lost"
    return "vm-stopped" if state.strip() == "shut off" else "lost"


class SshStdioUpstream:
    """L'amont de `vnc_mux` : un `ssh -W` dont on lit la sortie et écrit
    l'entrée. `vnc_password` : mot de passe à présenter à QEMU (type de
    sécurité 2), remis à None par `vnc_mux` dès la poignée de main faite."""

    def __init__(self, argv, popen=None, redact=()):
        self.vnc_password = None
        self._redact = [str(p) for p in redact if p]
        self._q = queue.Queue()
        self._ended = False
        self._closed = False
        self._lock = threading.Lock()
        self._stderr = bytearray()
        self.proc = (popen or _popen)(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True)
        self._out = threading.Thread(target=self._read_out, daemon=True, name="kvm-vnc-out")
        self._err = threading.Thread(target=self._read_err, daemon=True, name="kvm-vnc-err")
        self._out.start()
        self._err.start()

    def __repr__(self):
        # jamais le mot de passe, même dans une trace
        return f"<SshStdioUpstream pid={getattr(self.proc, 'pid', None)} connected={self.connected}>"

    # --- lecteurs ---------------------------------------------------------
    def _read_out(self):
        fd = self.proc.stdout.fileno()
        try:
            while True:
                data = os.read(fd, READ_CHUNK)
                if not data:
                    break
                self._q.put(data)
        except (OSError, ValueError):
            pass
        self._q.put(None)

    def _read_err(self):
        try:
            fd = self.proc.stderr.fileno()
            while True:
                data = os.read(fd, 4096)
                if not data:
                    break
                with self._lock:
                    self._stderr += data
                    del self._stderr[:-STDERR_KEEP]
        except (OSError, ValueError):
            pass

    # --- contrat amont de vnc_mux -----------------------------------------
    @property
    def connected(self):
        return not (self._ended or self._closed)

    def receive(self, timeout=None):
        if self._closed or self._ended:
            return None
        try:
            item = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            self._ended = True
            return None
        return item

    def send(self, data):
        if not self.connected:
            raise ConnectionError("tunnel closed")
        try:
            self.proc.stdin.write(bytes(data))
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            self._ended = True
            raise ConnectionError("tunnel closed") from e

    def close(self):
        """Ferme l'entrée de ssh, SIGTERM, 2 s, puis SIGKILL. Sans effet la
        seconde fois."""
        self.vnc_password = None
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=CLOSE_GRACE)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=CLOSE_GRACE)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass
        for f in (self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except (OSError, ValueError, AttributeError):
                pass

    # --- diagnostic -------------------------------------------------------
    def failure_reason(self):
        """Pourquoi le tunnel n'a rien donné, d'après la dernière ligne de
        stderr de ssh (chemins de l'état masqués, valeur `passwd` retirée)."""
        with self._lock:
            text = self._stderr.decode("utf-8", "replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return "the SSH tunnel closed before the display answered"
        last = lines[-1]
        if "open failed" in last.lower() or "connect failed" in last.lower():
            return "the VM display is not reachable from the host (connection refused)"
        return kh.scrub_secrets(kh._redact(last, self._redact))[:300]


def dial(vh, domain, popen=None):
    """Ouvre le tunnel vers l'affichage de `domain` : XML vivant relu (le
    port change à chaque démarrage), mot de passe lu s'il y en a un, puis
    `ssh -W` par la connexion maître. DisplayUnavailable si aucun affichage
    VNC n'est joignable ; KvmError/OpError d'une lecture."""
    _state, target, password = read_display(vh, domain, want_password=True)
    if target.get("reason"):
        raise DisplayUnavailable(target["reason"])
    argv = kh.stdio_forward_argv(vh.decl, vh.state, target["address"], target["port"])
    up = SshStdioUpstream(argv, popen=popen, redact=vh._secret_paths())
    up.vnc_password = password
    return up
