#!/usr/bin/env python3
"""Agent de l'hôte pour le magasin de secrets OpenBao (2.9.0, tâche 6).

Lancé en root par `harvester-secrets-agent.service` : par l'unité de chemin
quand la console (conteneur sans privilège) dépose
<état>/secrets-agent/request.json, et chaque jour par la minuterie
`harvester-secrets-rotate.timer` (sans demande : rotation du `secret_id`
de la console s'il a 30 jours, ménage). `/etc/harvester-ops` est en lecture
seule dans le conteneur : le dépôt des identifiants, la pose de B1 et les
rotations passent par lui (point 268), sur le modèle de l'agent des mises à
jour.

Liste FERMÉE d'opérations (toute autre : refus) :

    store-secret-id    jeton d'enveloppe vérifié (creation_path) puis déballé,
                       connexion d'essai, identifiants écrits
    b1-install         paquet vérifié, image chargée, compte, CA, clé de
                       descellement, secret podman, fichiers d'amorce,
                       configuration, unité, initialisation, réglages par
                       le jeton d'amorce (révoqué), identifiants écrits ;
                       tout échec défait les étapes faites
    b1-upgrade         nouveau paquet, retour à l'image d'avant si échec
    b1-remove          unité arrêtée et retirée, données gardées de côté
    rotate-secret-id   nouveau secret_id emballé par l'identité de la
                       console, connexion d'essai, PUIS ancien détruit
    rotate-unseal-key  nouvelle clé en current, ancienne en previous,
                       redémarrage, sys/rotate/root par l'identité de
                       l'agent, instantané, PUIS ancienne retirée
    restore-snapshot   instance neuve sur un volume neuf, restauration par le
                       jeton d'amorce, relecture, bascule (ancien volume
                       gardé 7 jours)
    b1-jwt             2.10.0 : méthode `jwt` d'un Rancher (montage, politique
                       commune des personnes, configuration par découverte,
                       rôle `person`), relue ; émetteur, client et CA
                       épinglés à la première pose (seul le canal root les
                       change, revue 6-7 I-1)
    b1-jwt-remove      montage d'un Rancher retiré (politique commune avec
                       le dernier) ; les entités restent (point 289)
    b1-groups          groupe interne `hops-<id>-<nom>` et sa politique
                       (pose, membres, retrait), réglage « lecture seulement
                       par groupe »
    b1-eso-listener    2.10.0 : second écouteur TLS d'OpenBao pour ESO sur une
                       adresse de l'hôte (certificat et CA dédiés), ou son
                       retrait ; redémarrage d'OpenBao
    b1-eso-cluster     méthode `jwt` d'un cluster enfant (montage, clés de
                       ses comptes de service, rôle `eso` aux namespaces
                       activés, politique à gabarit), relue
    b1-eso-cluster-remove  montage et politique d'un cluster enfant retirés
    status             état sans aucune valeur

Aucune valeur secrète dans status.json, les journaux ni les messages : le
jeton d'enveloppe de `store-secret-id` est la seule valeur qui transite par
l'état (request.json 0600, effacé AVANT traitement, usage unique).

Usage : harvester-secrets-agent            (traite la demande, sinon ménage)
        harvester-secrets-agent --status   (dernier état)
"""

import calendar
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import openbao_bundle as ob  # noqa: E402
import openbao_client as oc  # noqa: E402
import openbao_jwt as oj  # noqa: E402
import openbao_policy as pol  # noqa: E402
import safe_fs as sf  # noqa: E402

VERSION_FILE = Path(os.environ.get("HARVESTER_OPS_OPT", "/opt/harvester-ops")) / "VERSION"
OPS = ("store-secret-id", "b1-install", "b1-upgrade", "b1-remove", "rotate-secret-id", "rotate-unseal-key",
       "restore-snapshot", "b1-policies", "export-unseal-key", "export-unseal-key-done", "ack-offline-copy",
       "tf-entity-remove", "b1-jwt", "b1-jwt-remove", "b1-groups", "b1-eso-listener", "b1-eso-cluster",
       "b1-eso-cluster-remove", "status")
KEYS = {
    "store-secret-id": {"console_id", "addr", "role_id", "wrap_token", "ca_pem"},
    "b1-install": {"console_id", "bundle"},
    "b1-upgrade": {"bundle"},
    "b1-remove": set(),
    "rotate-secret-id": set(),
    "rotate-unseal-key": set(),
    "restore-snapshot": {"file", "witness"},
    "b1-policies": set(),
    "export-unseal-key": set(),
    "export-unseal-key-done": set(),
    "ack-offline-copy": {"key_id"},
    "tf-entity-remove": {"decl"},
    # 2.10.0 (plan 2.10.0, tâches 6 et 7) : méthode jwt d'un Rancher (pose,
    # mise à jour des clés statiques), son retrait, groupes internes et
    # réglage « lecture seulement par groupe »
    "b1-jwt": {"rancher", "client_id", "issuer", "ca_pem", "pubkeys", "groups_only"},
    "b1-jwt-remove": {"rancher"},
    "b1-groups": {"name", "classes", "clusters", "members", "remove", "groups_only"},
    # 2.10.0 (tâche 9) : External Secrets Operator des clusters enfants
    "b1-eso-listener": {"address"},
    "b1-eso-cluster": {"key", "harvester", "namespace", "cluster", "issuer", "pubkeys", "namespaces", "node_cidr",
                       "sa_uids"},
    "b1-eso-cluster-remove": {"key"},
    "status": set(),
}
COMMON = {"op", "id", "requested_by"}
REQUEST, STATUS, AGENT = "request.json", "status.json", "agent.json"
REQUEST_MAX = 64 << 10
ROLE_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
SECRET_VALUE_RE = re.compile(r"^[\x21-\x7e]{1,256}$")
ADDR_MAX = 512
LOOPBACK = ("127.0.0.1", "::1", "localhost")
REQ_ID_RE = re.compile(r"^[a-f0-9]{8,32}$")
SNAP_RE = re.compile(r"^openbao-[0-9A-Za-z_.-]{1,80}\.snap$")
# 2.9.0 tâche 11 (revue M2 de la seconde passe) : l'identifiant de la clé de
# descellement est dans le nom de l'instantané (`openbao-<date>-<clé>[-<motif>].snap`),
# la date du fichier ne décide plus rien ; un instantané est borné en taille
SNAP_KEY_RE = re.compile(r"-(hops-[0-9]{8}-[0-9]{1,4})(?:-([a-z][a-z-]{0,30}))?\.snap$")
SNAP_MAX = int(os.environ.get("HARVESTER_SECRETS_SNAP_MAX", str(8 << 30)))
AGENT_SNAP_KEEP_DAYS = 14
HEALTH = "health.json"
WITNESS_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+){0,6}$")
UNIT = "harvester-ops-openbao.service"
CONSOLE_UNIT = "harvester-ops.service"
CONTAINER = "harvester-ops-openbao"
ACCOUNT = "harvester-ops-openbao"
IMAGE = "localhost/harvester-ops-openbao"
SECRET, SECRET_PREV = "hops-openbao-unseal", "hops-openbao-unseal-prev"
SECRET_ID_MAX_AGE = 30 * 86400          # point 260
OLD_VOLUME_DAYS = 7
LOCK_WAIT_S = 1800                 # une demande attend la fin d'une autre opération
OLD_VOLUME_RE = re.compile(r"\.old-(\d{8}-\d{6})$")
KEY_KEEP_DAYS = 14                      # comme les instantanés (point 259)
WAIT_S = int(os.environ.get("HARVESTER_SECRETS_AGENT_WAIT", "120"))
# Décision 275 : copie hors ligne de la clé de descellement, remise une fois
# à l'administrateur (fichier root:harvester-ops 0440 dans le répertoire
# root des identifiants de la console, monté en lecture seule dans le
# conteneur), effacé après le téléchargement ou au bout de 10 minutes.
EXPORT_NAME = "unseal-export.key"
# Revue 6-7 M-2 : l'écriture d'un rôle `jwt` fusionne avec le rôle existant ;
# les marges et options qu'un rôle `person` ne porte jamais sont remises à
# zéro par l'agent à chaque pose (une marge d'expiration laissée par un
# instantané ou un administrateur accepterait des jetons d'identité échus).
ROLE_RESET = {"expiration_leeway": 0, "not_before_leeway": 0, "clock_skew_leeway": 0, "max_age": 0,
              "user_claim_json_pointer": False}
# 2.10.0 : CA et clés publiques de Rancher dans une demande (rien de secret)
PEM_MAX = 16 << 10
PUBKEYS_MAX = 8
EXPORT_TTL_S = 600


class AgentError(Exception):
    """Échec d'une opération ; `code` stable, message sans valeur."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Paths:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        g = lambda k, d: Path(e.get(k) or d)  # noqa: E731
        self.state = g("HARVESTER_OPS_STATE_DIR", "/var/lib/harvester-ops") / "secrets-agent"
        self.console_state = self.state.parent
        self.etc = g("HARVESTER_OPS_ETC", "/etc/harvester-ops") / "openbao"
        self.b1_etc = g("HARVESTER_OPS_OPENBAO_ETC", "/etc/harvester-ops-openbao")
        self.b1_data = g("HARVESTER_OPS_OPENBAO_DATA", "/var/lib/harvester-ops-openbao")
        self.boot = g("HARVESTER_OPS_OPENBAO_BOOT", pol.BOOT_DIR)
        self.units = g("HARVESTER_OPS_UNIT_DIR", "/etc/systemd/system")
        self.unit_src = g("HARVESTER_OPS_OPT", "/opt/harvester-ops") / "openbao" / UNIT
        # clé publique de l'Application Collection posée par install.sh
        # (root) : la signature cosign d'un paquet B1 est vérifiée avec elle,
        # jamais avec une clé ou une provenance venues du paquet
        self.cosign_key = g("HARVESTER_OPS_OPT", "/opt/harvester-ops") / "openbao" / ob.COSIGN_KEY_NAME
        self.logrotate = g("HARVESTER_OPS_LOGROTATE_DIR", "/etc/logrotate.d") / "harvester-ops-openbao"
        self.work = g("HARVESTER_SECRETS_AGENT_WORK", "/var/lib/harvester-secrets-agent")
        self.logs = g("HARVESTER_OPS_LOG_DIR", "/var/log/harvester-ops")
        # relecture finale I4 : un seul agent à la fois (unité systemd et
        # `sudo harvester-secrets` en processus), verrou à côté du répertoire
        # de travail, dans un répertoire de root
        self.lock = self.work.with_name(self.work.name + ".lock")


def _w(path, data, mode, binary=False, gid=None):
    """Écriture atomique dans un répertoire de root, droits et groupe posés
    sur le descripteur avant le renommage (revue M3 : jamais d'intervalle où
    le fichier en place n'a pas encore son groupe)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "wb" if binary else "w") as f:
            os.fchmod(f.fileno(), mode)
            if gid is not None:
                os.fchown(f.fileno(), 0 if os.geteuid() == 0 else -1, gid)
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read(path):
    return Path(path).read_text().strip()


def check_addr(addr):
    """Adresse d'un OpenBao du mode A (revue M10) : https, ou http sur la
    boucle locale seulement ; hôte présent, longueur bornée, sans caractère
    de contrôle."""
    from urllib.parse import urlsplit
    if not isinstance(addr, str) or not addr or len(addr) > ADDR_MAX or sf.clean_line(addr) != addr:
        return False
    try:
        u = urlsplit(addr)
        host = u.hostname
        u.port                                         # noqa: B018 : port invalide -> ValueError
    except ValueError:
        return False
    if not host:
        return False
    return u.scheme == "https" or (u.scheme == "http" and host in LOOPBACK)


def check_secret_value(value, what):
    """Valeur déballée (secret_id, accesseur) : une chaîne courte, sans
    espace ni caractère de contrôle (elle est écrite telle quelle dans un
    fichier d'une ligne)."""
    if not isinstance(value, str) or not SECRET_VALUE_RE.match(value):
        raise AgentError("openbao-unwrap-bad", f"the wrapping token held an unexpected {what}")
    return value


class Host:
    """Gestes sur l'hôte (commandes, propriétaires) ; remplacé dans les tests."""

    def run(self, argv, input=None, check=True, timeout=600):
        r = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            tail = ((r.stderr or r.stdout or "").strip().splitlines() or [f"exit {r.returncode}"])[-1]
            raise AgentError("host-command-failed", f"{Path(argv[0]).name} {argv[1] if len(argv) > 1 else ''}: "
                                                    f"{tail[:200]}")
        return r

    def chown(self, path, uid, gid):
        os.chown(path, uid, gid, follow_symlinks=False)

    def uid_of(self, user):
        import pwd
        try:
            return pwd.getpwnam(user).pw_uid
        except KeyError:
            return None

    def service_owner(self):
        import grp
        import pwd
        try:
            return pwd.getpwnam("harvester-ops").pw_uid, grp.getgrnam("harvester-ops").gr_gid
        except KeyError:
            return None

    def group_gid(self, name):
        import grp
        try:
            return grp.getgrnam(name).gr_gid
        except KeyError:
            return None

    def addresses(self):
        """Adresses IPv4 des interfaces de l'hôte (`ip -j -4 addr`)."""
        r = self.run(["ip", "-j", "-4", "addr", "show"], check=False)
        try:
            doc = json.loads(r.stdout or "[]")
        except ValueError:
            return []
        return sorted({a.get("local") for i in doc for a in i.get("addr_info") or [] if a.get("local")})


class Agent:
    def __init__(self, paths=None, host=None, client=None, sleep=time.sleep, now=time.time, b1_addr=pol.B1_ADDR):
        self.p = paths or Paths()
        self.b1_addr = b1_addr
        self.host = host or Host()
        self.client = client or (lambda addr, ca_pem=None: oc.Client(addr, ca_pem=ca_pem, timeout=10,
                                                                     actor="secrets-agent"))
        self.sleep, self.now = sleep, now
        self.status = {}
        self.log_path = None
        self.log_fd = None
        self.state_dir = None        # <état>/secrets-agent, tenu par descripteur
        self.undo = []
        # revue 6-7 I-1 : demande passée en processus par `sudo
        # harvester-secrets` (canal root), jamais lue dans le fichier que la
        # console écrit ; seule elle change l'ancre de confiance d'un Rancher
        self.root_channel = False

    # -- journal et état ----------------------------------------------------
    # Revue C1 des tâches 5-7 (même famille que la 2.8.3) : <état>/secrets-agent
    # et le répertoire des journaux appartiennent au compte de la console.
    # Rien n'y est ouvert par un chemin : le répertoire est tenu par
    # descripteur pour toute l'opération (un échange en cours de route ne
    # change pas où l'agent écrit), le journal est un fichier neuf (O_EXCL,
    # O_NOFOLLOW) gardé ouvert, l'état est écrit sous un nom neuf puis
    # renommé, groupe posé sur le descripteur. Tout texte venu d'une demande
    # est nettoyé (une seule ligne, sans caractère de contrôle).
    def log(self, msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S ", time.localtime(self.now())) + sf.clean_line(msg)
        print(line, flush=True)
        if self.log_fd is not None:
            try:
                os.write(self.log_fd, (line + "\n").encode("utf-8", "replace"))
            except OSError:
                pass

    def log_block(self, title, text):
        """Plusieurs lignes (journal d'OpenBao) : chacune nettoyée."""
        self.log(title)
        for line in (text or "").splitlines():
            self.log("  " + line)

    def step(self, sid, state, message=""):
        message = sf.clean_line(message, 2000)
        steps = self.status.setdefault("steps", [])
        for s in steps:
            if s["id"] == sid:
                s.update(status=state, message=message, ts=self.now())
                break
        else:
            steps.append({"id": sid, "status": state, "message": message, "ts": self.now()})
        self.log(f"[{sid}] {state} {message}".rstrip())
        self.save()

    def finish(self, state, message, code=None):
        message = sf.clean_line(message, 2000)
        self.status.update(state=state, message=message, ended=self.now(), **({"code": code} if code else {}))
        self.log(f"=> {state}: {message}")
        self.save()

    def save(self):
        """status.json : fichier de root lisible par tous (aucune valeur),
        groupe de la console, écrit sous un nom neuf puis renommé : un lien
        posé à sa place est remplacé, jamais suivi."""
        if self.state_dir is None:
            return
        try:
            self.state_dir.write(STATUS, json.dumps(self.status, indent=2).encode(), 0o644, self._console_group())
        except OSError as e:
            print(f"status not written: {e}", file=sys.stderr, flush=True)

    def _service_owner(self):
        """(uid, gid) du compte de la console quand l'agent est root, sinon
        None (tests sans privilège : rien n'est donné)."""
        return self.host.service_owner() if os.geteuid() == 0 else None

    def _console_group(self):
        own = self._service_owner()
        return None if own is None else (-1, own[1])

    def open_state(self):
        """<état>/secrets-agent tenu par descripteur. Un lien (ou un fichier)
        posé à sa place est retiré sans être suivi, et refusé."""
        base = sf.SafeDir.open_base(self.p.console_state)
        try:
            try:
                self.state_dir = base.subdir(self.p.state.name, create_mode=0o750, owner=self._service_owner())
            except sf.UnsafePath:
                base.unlink(self.p.state.name)
                raise
        finally:
            base.close()

    def open_log(self):
        """Un journal neuf : jamais un fichier existant, jamais un lien posé
        d'avance sous le nom attendu. Fichier de root, 0640, groupe de la
        console (elle le lit, ne peut pas le réécrire)."""
        try:
            with sf.SafeDir.open_base(self.p.logs, create_mode=0o750, owner=self._service_owner()) as d:
                stem = f"secrets-agent-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.now()))}"
                for i in range(20):
                    name = f"{stem}.log" if i == 0 else f"{stem}-{i if i < 10 else secrets.token_hex(4)}.log"
                    try:
                        self.log_fd = d.open_new_append(name, 0o640, self._console_group())
                    except FileExistsError:
                        continue
                    self.log_path = self.p.logs / name
                    return
        except OSError as e:
            print(f"secrets agent log not written: {e}", file=sys.stderr, flush=True)

    def close(self):
        if self.log_fd is not None:
            os.close(self.log_fd)
            self.log_fd = None
        if self.state_dir is not None:
            self.state_dir.close()
            self.state_dir = None

    # -- demande --------------------------------------------------------------
    def read_request(self):
        """La demande, retirée AVANT tout traitement (usage unique, l'unité de
        chemin ne se relance pas) ; None s'il n'y en a pas. Lue par le
        descripteur du répertoire : fichier ordinaire, un seul lien physique,
        jamais un lien ni un tube ; un répertoire posé à sa place est écarté."""
        d = self.state_dir
        if not d.lexists(REQUEST):
            return None
        raw, bad = None, "unreadable request"
        try:
            raw = d.read(REQUEST, limit=REQUEST_MAX)
        except sf.UnsafePath:
            bad = "the request is not a small regular file"
        except OSError:
            pass
        finally:
            try:
                d.set_aside(REQUEST)
            except OSError:
                pass
        if raw is None:
            return {"_bad": bad}
        try:
            req = json.loads(raw)
        except ValueError:
            return {"_bad": "the request is not JSON"}
        return req if isinstance(req, dict) else {"_bad": "the request is not an object"}

    def check_request(self, req):
        if "_bad" in req:
            raise AgentError("bad-request", req["_bad"])
        op = req.get("op")
        if op not in OPS:
            raise AgentError("unknown-operation", "operation not in the closed list")
        extra = set(req) - COMMON - KEYS[op]
        if extra:
            raise AgentError("bad-request", f"unexpected keys for {op}: "
                                            f"{sf.clean_line(', '.join(sorted(extra)), 100)}")
        if req.get("id") is not None and not REQ_ID_RE.match(str(req["id"])):
            raise AgentError("bad-request", "bad request id")
        if "console_id" in KEYS[op] and not pol.ID_RE.match(str(req.get("console_id") or "")):
            raise AgentError("bad-request", "bad console id")
        return op

    def _take_lock(self, wait):
        """Verrou exclusif de l'agent (flock). `wait` : attendre jusqu'à
        LOCK_WAIT_S secondes (une demande), sinon renoncer tout de suite
        (le ménage de la minuterie). Rend le descripteur, ou None."""
        self.p.lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.p.lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        waited = 0.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if not wait or waited >= LOCK_WAIT_S:
                    os.close(fd)
                    return None
                time.sleep(1)
                waited += 1

    def process(self, request=None):
        """Traite la demande déposée (ou `request`, passée en processus par
        `sudo harvester-secrets`), sinon le ménage de la minuterie. Un seul
        agent à la fois (relecture finale I4) : le ménage ne tourne jamais
        pendant une restauration qui garde l'ancien volume pour son retour."""
        pending = request is not None or (self.p.state / REQUEST).exists()
        self.root_channel = request is not None
        lock = self._take_lock(wait=pending)
        if lock is None:
            print("harvester-secrets-agent: another agent is running"
                  + ("; request left for the next run" if pending else "; housekeeping skipped"),
                  file=sys.stderr, flush=True)
            return 1 if pending else 0
        try:
            return self._process_locked(request)
        finally:
            os.close(lock)

    def _process_locked(self, request):
        try:
            self._expire_export()
        except OSError:
            pass
        try:
            self.open_state()
        except OSError as e:
            # revue M7 de la seconde passe : un lien reposé à chaque passage
            # n'arrête pas le ménage de root (clés retirées, anciens volumes,
            # rotation), qui n'écrit rien dans l'état de la console
            print(f"harvester-secrets-agent: {e}; request ignored, housekeeping only", file=sys.stderr, flush=True)
            try:
                self.housekeeping()
            finally:
                shutil.rmtree(self.p.work, ignore_errors=True)
            return 1
        try:
            return self._process(request)
        finally:
            if self.state_dir is not None:
                self.save_health(self.state_dir)
            self.close()
            shutil.rmtree(self.p.work, ignore_errors=True)    # copie du paquet, PKI, instantané

    def _process(self, request):
        req = request if request is not None else self.read_request()
        if req is None:
            return self.housekeeping()
        self.open_log()
        who = req.get("requested_by")
        self.status = {"state": "running", "op": req.get("op") if req.get("op") in OPS else None,
                       "request_id": req.get("id") if REQ_ID_RE.match(str(req.get("id") or "")) else None,
                       "requested_by": sf.clean_line(who, 64) or None if isinstance(who, str) else None,
                       "started": self.now(), "log": self.log_path.name if self.log_path else None, "steps": []}
        self.undo = []
        try:
            op = self.check_request(req)
            msg = getattr(self, "op_" + op.replace("-", "_"))(req)
            self.finish("done", msg or op)
            return 0
        except (AgentError, ob.BundleError) as e:
            self.rollback()
            self.finish("failed", str(e), getattr(e, "code", None))
            return 1
        except oc.OpenBaoError as e:
            self.rollback()
            self.finish("failed", f"OpenBao: {e.code}", e.code)
            return 1
        except Exception as e:                          # noqa: BLE001
            traceback.print_exc()
            self.rollback()
            self.finish("failed", f"Unexpected {type(e).__name__}; see journalctl -u harvester-secrets-agent")
            return 1

    def rollback(self):
        """Défait les étapes faites, de la dernière à la première."""
        while self.undo:
            name, fn = self.undo.pop()
            try:
                fn()
                self.log(f"[undo] {name}")
            except Exception as e:                      # noqa: BLE001 : au mieux, journalisé
                self.log(f"[undo] {name} failed: {type(e).__name__}: {sf.clean_line(e, 200)}")

    # -- aides ----------------------------------------------------------------
    def ca_pem(self):
        """CA de l'OpenBao du mode A, déposée avec ses identifiants
        (store-secret-id) : jamais utilisée pour parler à B1."""
        try:
            return (self.p.etc / "ca.pem").read_text()
        except OSError:
            return None

    def b1_ca(self):
        """CA de B1, écrite par make_tls dans /etc/harvester-ops-openbao
        (root, 0700) : la seule à laquelle l'agent se fie sur 127.0.0.1:8210
        (revue I6 : la console peut faire réécrire la CA du mode A)."""
        try:
            return (self.p.b1_etc / "ca.pem").read_text()
        except OSError:
            return None

    def console_settings(self):
        """Réglages de la console, lus sans suivre de lien (fichier de la
        console : un lien vers un JSON de root ne doit rien décider)."""
        try:
            with sf.SafeDir.open_base(self.p.console_state) as d:
                raw = d.read("secrets-settings.json")
            doc = json.loads(raw) if raw else {}
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def console_client(self, addr=None, secret_id=None):
        """Client connecté avec l'identité de la console (role-id et
        secret-id de /etc/harvester-ops/openbao). B1 (installé, ou
        `addr` = B1) : adresse et CA de B1 ; mode A : celles de la console."""
        settings = self.console_settings() if addr is None else {}
        # B1 posé : toujours B1, quoi que disent les réglages de la console
        # (revue M1 de la seconde passe : `mode: a` posé par la console
        # aurait envoyé la rotation vers son serveur, puis remplacé le
        # secret_id de B1 par celui qu'il rend)
        if addr == self.b1_addr or (addr is None and (self.agent_state() or settings.get("mode") != "a")):
            addr, ca = self.b1_addr, self.b1_ca()
        else:
            addr = addr or settings.get("addr")
            if not check_addr(addr):
                raise AgentError("openbao-bad-address", "the OpenBao address of the settings is refused "
                                 "(https, or http on the loopback only)")
            ca = self.ca_pem()
        c = self.client(addr, ca)
        c.login_approle(_read(self.p.etc / "role-id"), secret_id or _read(self.p.etc / "secret-id"))
        return c

    def agent_client(self):
        c = self.client(self.b1_addr, self.b1_ca())
        c.login_approle(_read(self.p.b1_etc / "agent-role-id"), _read(self.p.b1_etc / "agent-secret-id"))
        return c

    def write_credentials(self, role_id, secret_id, accessor, ca_pem=None):
        """Identifiants de la console : role-id et secret-id
        root:harvester-ops 0640 (le conteneur les lit par son groupe), CA
        publique 0644, accesseur 0600 (rotation). Groupe posé avant le
        renommage (revue M3)."""
        self.p.etc.mkdir(parents=True, exist_ok=True)
        os.chmod(self.p.etc, 0o750)
        gid = self.host.group_gid("harvester-ops")
        if gid is not None:
            self.host.chown(self.p.etc, 0, gid)
        for name, value, mode in (("role-id", role_id, 0o640), ("secret-id", secret_id, 0o640),
                                  ("secret-id-accessor", accessor or "", 0o600)):
            _w(self.p.etc / name, value + "\n", mode, gid=gid if mode == 0o640 else None)
        if ca_pem:
            _w(self.p.etc / "ca.pem", ca_pem, 0o644)

    def agent_state(self):
        try:
            return json.loads((self.p.b1_etc / AGENT).read_text())
        except (OSError, ValueError):
            return {}

    def save_agent_state(self, st):
        _w(self.p.b1_etc / AGENT, json.dumps(st, indent=2), 0o600)

    def wait_unsealed(self, ca_pem, what="OpenBao"):
        """Attend `initialized` et descellé ; une unité tombée arrête l'attente."""
        c = self.client(self.b1_addr, ca_pem)
        deadline = self.now() + WAIT_S
        last = "no answer"
        while self.now() < deadline:
            try:
                st = c.seal_status()
                if st.get("initialized") and not st.get("sealed"):
                    return
                last = "sealed" if st.get("sealed") else "not initialized"
            except oc.OpenBaoError as e:
                last = e.code
            r = self.host.run(["systemctl", "is-active", UNIT], check=False)
            if (r.stdout or "").strip() == "failed":
                raise AgentError("b1-start-failed", f"{what} stopped ({last}); see journalctl -u {UNIT}")
            self.sleep(2)
        raise AgentError("b1-start-timeout", f"{what} not ready after {WAIT_S} s ({last})")

    def account_gid(self):
        """Groupe du compte du conteneur (créé par `useradd --user-group`) ;
        0 seulement si l'hôte ne le connaît pas (revue M7 : le conteneur
        n'a pas besoin du groupe 0, tout ce qu'il écrit est monté)."""
        gid = self.host.group_gid(ACCOUNT)
        return 0 if gid is None else gid

    def own(self, path, uid):
        self.host.chown(path, uid, self.account_gid())

    def unit_env(self, uid, image_ref, prev=False):
        _w(self.p.b1_etc / "unit.env",
           f"OPENBAO_IMAGE={image_ref}\nOPENBAO_UID={uid}\nOPENBAO_GID={self.account_gid()}\n"
           f"OPENBAO_PREV_KEY={1 if prev else 0}\n", 0o644)

    def podman_secret(self, name, src=None):
        self.host.run(["podman", "secret", "rm", name], check=False)
        if src is not None:
            self.host.run(["podman", "secret", "create", name, str(src)])

    def restart(self):
        self.host.run(["systemctl", "restart", UNIT])

    def new_key_id(self, taken):
        day = time.strftime("%Y%m%d", time.gmtime(self.now()))
        n = 1
        while f"hops-{day}-{n}" in taken:
            n += 1
        return f"hops-{day}-{n}"

    def render(self, st, template, prev=None, init=False):
        cfg = pol.b1_config(st["console_id"], st["current_key"], template, previous_key_id=prev, init=init,
                            mount=st.get("mount", pol.DEFAULT_MOUNT), eso_listener=st.get("eso_listener"))
        _w(self.p.b1_etc / "openbao.hcl", cfg, 0o444)

    def staged_bundle(self, name):
        """Le paquet de staged/, copié dans le répertoire de travail de root
        depuis un descripteur (revue M1) : staged/ ouvert sans suivre de
        lien, paquet fichier ordinaire à un seul lien physique, copie bornée
        à la taille vue à l'ouverture. Vérifié ensuite, signature comprise
        (clé de la collection posée par install.sh). Le répertoire de travail
        est retiré à la fin de l'opération, réussie ou non (process)."""
        if not ob.NAME_RE.match(str(name or "")):
            raise AgentError("bundle-bad-name", "bad bundle name in the request")
        missing = AgentError("bundle-missing", "the bundle is not in the staging directory")
        try:
            staged = self.state_dir.subdir("staged")
        except OSError:
            raise missing from None
        with staged:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY,
                             dir_fd=staged.fd)
            except OSError:
                raise missing from None
        with os.fdopen(fd, "rb") as src:
            st = os.fstat(src.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > ob.MAX_BUNDLE:
                raise missing
            if self.p.work.exists():
                shutil.rmtree(self.p.work)
            self.p.work.mkdir(mode=0o700, parents=True)
            dst = self.p.work / name
            left = st.st_size
            with open(dst, "wb") as out:
                while left > 0:
                    b = src.read(min(left, 1 << 20))
                    if not b:
                        break
                    out.write(b)
                    left -= len(b)
            if left != 0 or src.read(1):
                raise AgentError("bundle-changed", "the staged bundle changed while it was copied")
        return ob.verify(dst, self.p.work, cosign_key=self.p.cosign_key)

    def load_image(self, info):
        r = self.host.run(["podman", "load", "-i", str(info["image_tar"])])
        loaded = re.findall(r"Loaded image(?:\(s\)|s)?:\s*(\S+)", r.stdout or "")
        if not loaded:
            raise AgentError("b1-image-load", "podman load named no image")
        r2 = self.host.run(["podman", "image", "inspect", "--format", "{{.Digest}}", loaded[-1]])
        if (r2.stdout or "").strip() != info["manifest_digest"]:
            raise AgentError("bundle-digest-mismatch", "the loaded image does not have the digest of the bundle")
        ref = f"{IMAGE}:{info['tag']}"
        self.host.run(["podman", "tag", loaded[-1], ref])
        return ref

    def make_tls(self, uid):
        """CA de 10 ans contrainte à 127.0.0.1 et certificat serveur de 2 ans
        (extensions exigées par la vérification stricte de Python 3.13),
        mêmes paramètres que registry_seed.make_ca / sign_server (non
        importé : il tire PyYAML, absent de certains hôtes)."""
        tls = self.p.b1_etc / "tls"
        tls.mkdir(mode=0o700, exist_ok=True)
        work = self.p.work / "pki"
        work.mkdir(mode=0o700, exist_ok=True)
        run = self.host.run
        run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", str(work / "ca.key"), "-out", str(work / "ca.pem"), "-days", "3650",
             "-subj", "/CN=harvester-ops OpenBao CA", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash",
             "-addext", "nameConstraints=critical,permitted;IP:127.0.0.1/255.255.255.255"])
        run(["openssl", "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", str(tls / "server.key"), "-out", str(work / "server.csr"), "-subj", "/CN=127.0.0.1"])
        (work / "ext.cnf").write_text("subjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n"
                                      "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
                                      "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
        run(["openssl", "x509", "-req", "-in", str(work / "server.csr"), "-CA", str(work / "ca.pem"),
             "-CAkey", str(work / "ca.key"), "-CAcreateserial", "-out", str(tls / "server.pem"), "-days", "730",
             "-extfile", str(work / "ext.cnf")])
        ca = (work / "ca.pem").read_text()
        _w(self.p.b1_etc / "ca.pem", ca, 0o644)
        for f, mode in (("server.key", 0o400), ("server.pem", 0o444)):
            os.chmod(tls / f, mode)
            self.own(tls / f, uid)
        self.own(tls, uid)
        shutil.rmtree(work, ignore_errors=True)           # la clé de la CA ne sert plus
        return ca

    def write_boot(self, uid):
        """Fichiers d'amorce sur tmpfs (/run), 0400 au compte du conteneur,
        lus une fois par `initialize` (essai 3) ; valeurs gardées en mémoire."""
        self.p.boot.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.own(self.p.boot, uid)
        vals = {f: secrets.token_hex(16) for f in pol.BOOT_FILES}
        for f, v in vals.items():
            _w(self.p.boot / f, v, 0o400)
            self.own(self.p.boot / f, uid)
        return vals

    def clear_boot(self):
        shutil.rmtree(self.p.boot, ignore_errors=True)

    # -- opérations -------------------------------------------------------------
    def op_status(self, req):
        st = self.agent_state()
        r = self.host.run(["systemctl", "is-active", UNIT], check=False)
        sid = self.p.etc / "secret-id"
        info = {"b1_installed": bool(st), "b1_unit": (r.stdout or "").strip() or None,
                "current_key": st.get("current_key"), "image": st.get("image"),
                "keys": [{k: v for k, v in e.items() if k != "file"} for e in st.get("keys", [])],
                "secret_id_age_s": int(self.now() - sid.stat().st_mtime) if sid.exists() else None,
                "offline_copy": {k: (st.get("offline_copy") or {}).get(k)
                                 for k in ("key_id", "exported_at", "downloaded_at", "acked_at")} if st else None,
                "agent_version": _version()}
        self.status["info"] = info
        self.step("status", "done")
        return "status read"

    def op_store_secret_id(self, req):
        """Mode A (et nouveau dépôt) : l'enveloppe est vérifiée par
        `sys/wrapping/lookup` (creation_path exact) puis déballée ; déjà
        déballée, expirée ou d'un autre chemin : rien n'est écrit."""
        cid = req["console_id"]
        if self.agent_state():
            # revue I6 : en B1, les identifiants viennent de b1-install et de
            # rotate-secret-id ; un dépôt remplacerait la CA et l'adresse
            raise AgentError("b1-installed", "OpenBao B1 is installed on this host: its credentials are managed by "
                                             "the agent; to use another OpenBao, bring the classes back "
                                             "(harvester-secrets migrate --to local), then harvester-secrets "
                                             "disable --remove-b1")
        role_id = str(req.get("role_id") or "")
        if not ROLE_ID_RE.match(role_id):
            raise AgentError("bad-request", "bad role id")
        wrap = str(req.get("wrap_token") or "")
        if not wrap or len(wrap) > 512:
            raise AgentError("bad-request", "missing wrapping token")
        ca = req.get("ca_pem")
        if ca is not None and (not isinstance(ca, str) or "BEGIN CERTIFICATE" not in ca or len(ca) > 16384):
            raise AgentError("bad-request", "bad CA")
        addr = req.get("addr")
        if not check_addr(addr):
            raise AgentError("bad-request", "bad address (https, or http on the loopback only)")
        self.step("unwrap", "running", "checking the wrapping token (creation path) then unwrapping")
        c = self.client(addr, ca)
        try:
            data = c.unwrap(wrap, pol.secret_id_path(cid))
        except oc.OpenBaoError as e:
            if e.code == "openbao-unwrap-used":
                raise AgentError("openbao-unwrap-used",
                                 "wrapping token already used, expired or not from the console role: nothing "
                                 "written; someone else may have read the secret_id, generate a new one") from None
            raise
        secret_id, accessor = data.get("secret_id"), data.get("secret_id_accessor")
        if not secret_id:
            raise AgentError("openbao-unwrap-used", "the wrapping token held no secret_id")
        check_secret_value(secret_id, "secret_id")
        if accessor is not None:
            check_secret_value(accessor, "accessor")
        self.step("unwrap", "done")
        self.step("login", "running", "test login with the new secret_id")
        t = self.client(addr, ca)
        t.login_approle(role_id, secret_id)
        t.revoke_self()
        self.step("login", "done")
        self.step("credentials", "running")
        self.write_credentials(role_id, secret_id, accessor, ca)
        self.step("credentials", "done", str(self.p.etc))
        return "secret_id stored"

    def op_rotate_secret_id(self, req=None):
        """Nouveau `secret_id` (emballé) par l'identité de la console, dépôt,
        connexion d'essai avec le nouveau, PUIS destruction de l'ancien par
        son accesseur. Tout échec avant la destruction : l'ancien reste."""
        settings = self.console_settings()
        cid = settings.get("console_id")
        if settings.get("mode") not in ("a", "b1") or not pol.ID_RE.match(str(cid or "")):
            raise AgentError("openbao-inactive", "OpenBao is not active for this console")
        self.step("issue", "running", "new wrapped secret_id with the console identity")
        c = self.console_client()
        _, body = c._call("POST", f"auth/approle/role/{pol.console_role(cid)}/secret-id", {},
                          extra={"X-Vault-Wrap-TTL": "300"})
        wrap = ((body or {}).get("wrap_info") or {}).get("token")
        if not wrap:
            raise AgentError("rotate-no-wrap", "OpenBao did not wrap the new secret_id")
        data = self.client(c.addr, self.b1_ca() if c.addr == self.b1_addr else self.ca_pem()).unwrap(
            wrap, pol.secret_id_path(cid))
        new, new_acc = data.get("secret_id"), data.get("secret_id_accessor")
        check_secret_value(new, "secret_id")
        if new_acc is not None:
            check_secret_value(new_acc, "accessor")
        self.step("issue", "done")
        self.step("login", "running", "test login with the new secret_id")
        t = self.console_client(secret_id=new)
        self.step("login", "done")
        old_acc = ""
        try:
            old_acc = _read(self.p.etc / "secret-id-accessor")
        except OSError:
            pass
        self.write_credentials(_read(self.p.etc / "role-id"), new, new_acc)
        self.step("destroy-old", "running")
        if old_acc and old_acc != new_acc:
            t._call("POST", f"auth/approle/role/{pol.console_role(cid)}/secret-id-accessor/destroy",
                    {"secret_id_accessor": old_acc})
            self.step("destroy-old", "done")
        else:
            self.step("destroy-old", "warn", "no accessor known for the previous secret_id")
        t.revoke_self()
        c.revoke_self()
        return "secret_id rotated"

    def _need_podman(self):
        import container_runtime as cr
        try:
            rt = cr.select_runtime(self.p.units)
        except Exception as e:                              # noqa: BLE001
            raise AgentError("b1-needs-podman", f"no container runtime ({type(e).__name__})") from None
        if Path(rt).name != "podman":
            raise AgentError("b1-needs-podman", "mode B1 needs Podman (podman secrets, read-only container)")

    def op_b1_install(self, req):
        cid = req["console_id"]
        # décision 275 : l'acquittement de la copie hors ligne vient APRÈS la
        # pose (la clé n'existe pas avant) : export-unseal-key puis
        # ack-offline-copy ; `ack_offline_copy` n'est plus exigé ici
        if self.agent_state():
            raise AgentError("b1-already-installed", "OpenBao B1 is already installed on this host")
        data = self.p.b1_data / "data"
        if data.exists() and any(data.iterdir()):
            raise AgentError("b1-volume-exists", f"{data} is not empty: never wiped, remove it by hand if unused")
        self._need_podman()
        self.step("bundle", "running", "checking the OpenBao bundle")
        info = self.staged_bundle(req.get("bundle"))
        self.step("bundle", "done", f"OpenBao {info['version']} ({info['tag']}, {info['index_digest'][:19]})")
        self.step("image", "running", "podman load")
        ref = self.load_image(info)
        self.host.run(["podman", "tag", ref, f"{IMAGE}:current"])
        self.step("image", "done", ref)

        self.step("account", "running")
        uid = self.host.uid_of(ACCOUNT)
        if uid is None:
            self.host.run(["useradd", "--system", "--user-group", "--no-create-home", "--home-dir",
                           str(self.p.b1_data), "--shell", "/sbin/nologin", "--comment",
                           "harvester-ops OpenBao", ACCOUNT])
            uid = self.host.uid_of(ACCOUNT)
            if uid is None:
                raise AgentError("b1-account", f"account {ACCOUNT} not created")
        self.step("account", "done", f"{ACCOUNT} ({uid})")

        self.step("directories", "running")
        new_etc = not self.p.b1_etc.exists()
        self.p.b1_etc.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.p.b1_etc, 0o700)
        # une clé tirée n'est jamais perdue par un retour arrière : gardée
        # et reprise au prochain essai
        self.undo.append(("configuration", lambda: self._remove_b1_etc(keep_key=True, whole=new_etc)))
        new_data = not self.p.b1_data.exists()
        for d in (self.p.b1_data, data, self.p.b1_data / "audit"):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(d, 0o700)
            self.own(d, uid)
        snaps = self.p.b1_data / "snapshots"
        snaps.mkdir(mode=0o700, exist_ok=True)
        self.host.chown(snaps, 0, 0)
        self.undo.append(("volume", lambda: self._wipe_new_volume(whole=new_data)))
        self.step("directories", "done")

        self.step("tls", "running", "CA and server certificate for 127.0.0.1")
        ca = self.make_tls(uid)
        self.step("tls", "done")

        self.step("unseal-key", "running")
        st = {"console_id": cid, "mount": pol.DEFAULT_MOUNT, "installed": self.now(), "image": ref,
              "tag": info["tag"], "version": info["version"], "index_digest": info["index_digest"], "keys": []}
        keys = self.p.b1_etc / "keys"
        keys.mkdir(mode=0o700, exist_ok=True)
        master = self.p.b1_etc / "unseal.key"
        if master.exists() and len(master.read_bytes()) == 32:
            key_id = next((p.stem for p in keys.glob("hops-*.key") if p.read_bytes() == master.read_bytes()),
                          None) or self.new_key_id(set())
            reused = True
        else:
            key_id = self.new_key_id(set())
            _w(master, secrets.token_bytes(32), 0o400, binary=True)
            reused = False
        if not (keys / f"{key_id}.key").exists():
            shutil.copyfile(master, keys / f"{key_id}.key")
            os.chmod(keys / f"{key_id}.key", 0o400)
        st.update(current_key=key_id, keys=[{"id": key_id, "created": self.now(), "retired": None}])
        self.podman_secret(SECRET, master)
        self.undo.append(("podman secret", lambda: self.podman_secret(SECRET)))
        self.step("unseal-key", "done", f"{key_id}{' (kept from an earlier attempt)' if reused else ''}")

        self.step("boot", "running", "bootstrap files on tmpfs")
        boot = self.write_boot(uid)
        self.undo.append(("bootstrap files", self.clear_boot))
        self.step("boot", "done")

        self.step("config", "running")
        self.render(st, info["template"], init=True)
        self.own(self.p.b1_etc / "openbao.hcl", uid)
        _w(self.p.b1_etc / "openbao.hcl.tpl", info["template"], 0o644)
        self.unit_env(uid, f"{IMAGE}:current")
        self.p.logrotate.parent.mkdir(parents=True, exist_ok=True)
        _w(self.p.logrotate, pol.logrotate_conf(self.p.b1_data / "audit", ACCOUNT, CONTAINER), 0o644)
        self.undo.append(("logrotate", lambda: self.p.logrotate.unlink(missing_ok=True)))
        self.step("config", "done")

        self.step("start", "running", "OpenBao initializes itself")
        unit = self.p.units / UNIT
        if not unit.exists():
            if not self.p.unit_src.exists():
                raise AgentError("b1-unit-missing", f"{UNIT} is not installed (run install.sh --upgrade)")
            unit.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.p.unit_src, unit)
            os.chmod(unit, 0o644)
            self.undo.append(("unit file", lambda: unit.unlink(missing_ok=True)))
        self.host.run(["systemctl", "daemon-reload"], check=False)
        self.undo.append(("unit", lambda: self.host.run(["systemctl", "disable", "--now", UNIT], check=False)))
        self.host.run(["systemctl", "enable", "--now", UNIT])
        try:
            self.wait_unsealed(ca)
        except AgentError as e:
            # Initialisation en échec : le serveur refuse ensuite tout
            # démarrage ; seul geste, effacer ce volume NEUF (jamais descellé).
            tail = self.host.run(["journalctl", "-u", UNIT, "-n", "30", "--no-pager", "-o", "cat"], check=False)
            self.log_block("OpenBao log:", (tail.stdout or "")[-4000:])
            raise AgentError(e.code, f"{e}; the new volume is wiped, see the agent log") from None
        self.step("start", "done")

        self.step("settings", "running", "bootstrap token: max_versions, child token policy, role ids")
        b = self.client(self.b1_addr, ca)
        b.set_token(boot["boot-token"])
        try:
            _, cfg = b._call("GET", f"{st['mount']}/config")
            if int(((cfg or {}).get("data") or {}).get("max_versions") or 0) != pol.MAX_VERSIONS:
                b._call("POST", f"{st['mount']}/config", {"max_versions": pol.MAX_VERSIONS})
                self.log("max_versions set by the agent (initialize could not)")
            _, auths = b._call("GET", "sys/auth")
            auths = (auths or {}).get("data") or auths or {}
            acc = (auths.get("token/") or {}).get("accessor")
            b._call("POST", f"sys/policies/acl/{pol.tf_role(cid)}", {"policy": pol.tf_child_policy(cid, acc)})
            _, r1 = b._call("GET", f"auth/approle/role/{pol.console_role(cid)}/role-id")
            _, r2 = b._call("GET", f"auth/approle/role/{pol.agent_role(cid)}/role-id")
            _, lk = b._call("POST", f"auth/approle/role/{pol.console_role(cid)}/secret-id/lookup",
                            {"secret_id": boot["console-secret-id"]})
            b._call("DELETE", f"sys/policies/acl/{pol.boot_policy_name(cid)}")
        finally:
            try:
                b.revoke_self()
            except oc.OpenBaoError as e:
                self.log(f"bootstrap token not revoked ({e.code}); it expires after 15 min")
        self.step("settings", "done")

        self.step("verify", "running", "console and agent AppRole logins")
        role_id, agent_role_id = r1["data"]["role_id"], r2["data"]["role_id"]
        for rid, sid in ((role_id, boot["console-secret-id"]), (agent_role_id, boot["agent-secret-id"])):
            t = self.client(self.b1_addr, ca)
            t.login_approle(rid, sid)
            t.revoke_self()
        self.step("verify", "done")

        self.step("credentials", "running")
        _w(self.p.b1_etc / "agent-role-id", agent_role_id + "\n", 0o400)
        _w(self.p.b1_etc / "agent-secret-id", boot["agent-secret-id"] + "\n", 0o400)
        self.write_credentials(role_id, boot["console-secret-id"],
                               ((lk or {}).get("data") or {}).get("secret_id_accessor"), ca)
        self.undo.append(("credentials", self._remove_credentials))
        st["unsealed_once"] = True
        st["offline_copy"] = self._offline_pending(st["current_key"])
        self.save_agent_state(st)
        a = self.agent_client()
        try:
            self._record_default_policy(a)
        finally:
            a.revoke_self()
        self.render(st, info["template"], init=False)       # initialize ne sert plus
        self.own(self.p.b1_etc / "openbao.hcl", uid)
        self.clear_boot()
        self.undo = []
        self.step("credentials", "done")
        shutil.rmtree(self.p.work, ignore_errors=True)
        return f"OpenBao {info['version']} installed on 127.0.0.1:8210"

    def _remove_credentials(self):
        for f in ("role-id", "secret-id", "secret-id-accessor", "ca.pem"):
            (self.p.etc / f).unlink(missing_ok=True)

    def _remove_b1_etc(self, keep_key, whole):
        """Retour arrière : configuration retirée ; la clé de descellement
        gardée seulement si sa copie hors ligne a été acquittée (elle resservira
        au prochain essai, la copie reste valable)."""
        for f in ("openbao.hcl", "openbao.hcl.tpl", "unit.env", "ca.pem", "agent-role-id", "agent-secret-id",
                  AGENT):
            (self.p.b1_etc / f).unlink(missing_ok=True)
        shutil.rmtree(self.p.b1_etc / "tls", ignore_errors=True)
        if not keep_key:
            (self.p.b1_etc / "unseal.key").unlink(missing_ok=True)
            shutil.rmtree(self.p.b1_etc / "keys", ignore_errors=True)
        if whole and not any(self.p.b1_etc.iterdir()):
            self.p.b1_etc.rmdir()

    def _wipe_new_volume(self, whole):
        """Seulement le volume créé par CETTE pose, jamais descellé."""
        if self.agent_state().get("unsealed_once"):
            raise AgentError("b1-volume-unsealed", "refusing to wipe a volume that was unsealed once")
        for d in ("data", "audit", "snapshots"):
            shutil.rmtree(self.p.b1_data / d, ignore_errors=True)
        if whole and self.p.b1_data.exists() and not any(self.p.b1_data.iterdir()):
            self.p.b1_data.rmdir()

    def op_b1_upgrade(self, req):
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        self._need_podman()
        self.step("bundle", "running")
        info = self.staged_bundle(req.get("bundle"))
        if ob.version_key(info["version"]) < ob.version_key(st.get("version") or "0"):
            # revue M3 : une image signée plus ancienne (cosign ne signe pas le
            # tag) ne remplace pas la version en place
            raise AgentError("bundle-downgrade", f"OpenBao {info['version']} is older than the installed "
                                                 f"{st.get('version')}: refused")
        if st.get("eso_listener") and not re.search(r"^__ESO_LISTENER__$", info.get("template") or "", re.M):
            # revue 8-10 I-4 : un paquet construit avant la 2.10.0 porte un
            # modèle sans écouteur ESO ; posé, il ferait échouer le prochain
            # rendu (rotation, restauration) ou perdrait l'écouteur
            raise AgentError("eso-listener-template", "this OpenBao bundle was built before 2.10.0 and has no "
                             "ESO listener in its template while the listener is on: build the bundle with "
                             "harvester-ops 2.10.0 or later (harvester-secrets bundle), or remove External "
                             "Secrets first (harvester-secrets eso remove --all)")
        self.step("bundle", "done", f"OpenBao {info['version']} ({info['tag']})")
        # décision 277 A : pas de retour à une version plus ancienne ; le
        # chemin de retour après une mise à jour est cet instantané, pris
        # avant de toucher à l'image et restaurable dans la version posée
        self.step("snapshot", "running", "snapshot before the upgrade")
        pre = self.agent_snapshot(st, "pre-upgrade")
        self.step("snapshot", "done", pre)
        self.step("image", "running")
        ref = self.load_image(info)
        self.host.run(["podman", "tag", f"{IMAGE}:current", f"{IMAGE}:previous"])
        self.host.run(["podman", "tag", ref, f"{IMAGE}:current"])
        self.step("image", "done", ref)
        self.step("restart", "running")
        self.restart()
        try:
            self.wait_unsealed(self.b1_ca())
            t = self.console_client(addr=self.b1_addr)
            t.revoke_self()
        except (AgentError, oc.OpenBaoError) as e:
            self.step("rollback", "running", str(getattr(e, "code", e)))
            self.host.run(["podman", "tag", f"{IMAGE}:previous", f"{IMAGE}:current"])
            self.restart()
            self.wait_unsealed(self.b1_ca())
            self.step("rollback", "done", f"{st.get('version')} restored")
            raise AgentError("b1-upgrade-failed", f"OpenBao {info['version']} did not come up; "
                                                  f"{st.get('version')} restored") from None
        st.update(image=ref, tag=info["tag"], version=info["version"], index_digest=info["index_digest"])
        self.save_agent_state(st)
        _w(self.p.b1_etc / "openbao.hcl.tpl", info["template"], 0o644)
        self.step("restart", "done")
        return f"OpenBao {info['version']} running"

    # -- copie hors ligne de la clé de descellement (décision 275) -------------
    @staticmethod
    def _offline_pending(key_id):
        return {"key_id": key_id, "exported_at": None, "downloaded_at": None, "acked_at": None}

    def _expire_export(self):
        """Copie remise et jamais reprise : effacée après EXPORT_TTL_S."""
        f = self.p.etc / EXPORT_NAME
        if not f.exists() and not f.is_symlink():
            return
        o = (self.agent_state() or {}).get("offline_copy") or {}
        if not o.get("exported_at") or self.now() - o["exported_at"] > EXPORT_TTL_S:
            f.unlink(missing_ok=True)

    def op_export_unseal_key(self, req=None):
        """La clé de descellement EN SERVICE remise une fois à
        l'administrateur : copie root:harvester-ops 0440 dans le répertoire
        root des identifiants (lu par la console, en lecture seule), effacée
        après le téléchargement (export-unseal-key-done) ou au bout de
        EXPORT_TTL_S. Jamais de valeur dans le statut ni le journal."""
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        key_id = st["current_key"]
        src = self.p.b1_etc / "keys" / f"{key_id}.key"
        if not src.is_file():
            raise AgentError("unseal-key-missing", f"the key {key_id} is not on this host")
        self.step("export", "running", f"key {key_id}")
        self.p.etc.mkdir(parents=True, exist_ok=True)
        dst = self.p.etc / EXPORT_NAME
        dst.unlink(missing_ok=True)
        _w(dst, src.read_bytes(), 0o440, binary=True)
        gid = self.host.group_gid("harvester-ops")
        if gid is not None:
            self.host.chown(dst, 0, gid)
        o = st.get("offline_copy") or {}
        if o.get("key_id") != key_id:
            o = self._offline_pending(key_id)
        o.update(exported_at=self.now(), downloaded_at=None)
        st["offline_copy"] = o
        self.save_agent_state(st)
        self.status["info"] = {"key_id": key_id, "expires_in_s": EXPORT_TTL_S}
        self.step("export", "done", f"key {key_id}: one download within {EXPORT_TTL_S // 60} min")
        return f"unseal key {key_id} ready for one download"

    def op_export_unseal_key_done(self, req=None):
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        (self.p.etc / EXPORT_NAME).unlink(missing_ok=True)
        o = st.get("offline_copy") or {}
        if o.get("exported_at"):
            o["downloaded_at"] = self.now()
            st["offline_copy"] = o
            self.save_agent_state(st)
        self.status["info"] = {"key_id": o.get("key_id")}
        self.step("export", "done", "the copy given for download is erased")
        return "download copy erased"

    def op_ack_offline_copy(self, req):
        """Acquittement écrit de la copie hors ligne : seulement pour la clé
        en service, et une fois qu'elle a été remise (elle existe et
        l'administrateur l'a eue)."""
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        key_id = str(req.get("key_id") or "")
        o = st.get("offline_copy") or {}
        if key_id != st["current_key"] or o.get("key_id") != key_id:
            raise AgentError("offline-copy-wrong-key", "this is not the unseal key in service")
        if not o.get("exported_at"):
            raise AgentError("offline-copy-not-exported", "download the key first (export-unseal-key)")
        (self.p.etc / EXPORT_NAME).unlink(missing_ok=True)
        o["acked_at"] = self.now()
        st["offline_copy"] = o
        self.save_agent_state(st)
        self.status["info"] = {"key_id": key_id, "acked": True}
        self.step("ack", "done", f"offline copy of {key_id} acknowledged")
        return f"offline copy of {key_id} acknowledged"

    def op_b1_policies(self, req=None, review=False):
        """Décision 276 : remet les politiques `hops-*` de cette console au
        texte de la version installée (mise à niveau après la pose, plus de
        jeton racine en B1) : celle de l'agent et celle des jetons enfants
        d'OpenTofu. La politique de la console n'est jamais touchée."""
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        cid = st["console_id"]
        self.step("policies", "running")
        done = [pol.agent_role(cid), pol.tf_role(cid)]
        a = self.agent_client()
        try:
            _, auths = a._call("GET", "sys/auth")
            auths = (auths or {}).get("data") or auths or {}
            acc = (auths.get("token/") or {}).get("accessor")
            a._call("POST", f"sys/policies/acl/{pol.tf_role(cid)}", {"policy": pol.tf_child_policy(cid, acc)})
            a._call("POST", f"sys/policies/acl/{pol.agent_role(cid)}",
                    {"policy": pol.agent_policy(cid, st.get("mount") or pol.DEFAULT_MOUNT)})
            # revue 8-10 I-5 : jetons enfants d'OpenTofu liés à 127.0.0.1/32
            # (B1 posée avant la 2.10.0 ; la nouvelle politique de l'agent vaut
            # sur le jeton déjà ouvert, essai 6)
            if not review:
                a._call("POST", f"auth/token/roles/{pol.tf_role(cid)}",
                        pol.tf_token_role_params(cid, [pol.B1_CIDR]))
            # relecture finale I3 : empreinte de la politique `default` relevée
            # sur l'instance en service (jamais pendant la revue d'un
            # instantané restauré, qui la compare)
            if not review and not st.get("default_policy_sha256"):
                self._record_default_policy(a)
            # 2.10.0 : politique commune des personnes et politiques des
            # groupes remises au texte de cette version (jamais pendant la
            # revue d'un instantané, qui les compare)
            if not review:
                done += self._person_policies_refresh(a, st)
        finally:
            a.revoke_self()
        self.step("policies", "done", ", ".join(done))
        return "policies of the agent and of the OpenTofu tokens updated"

    def _person_policies_refresh(self, a, st):
        cid, kv, prefix = self._person_layout(st)
        done = []
        if st.get("person_jwt"):
            a._call("POST", f"sys/policies/acl/{pol.person_policy_name(cid)}",
                    {"policy": pol.person_policy(kv, prefix, cid, bool(st.get("person_groups_only")))})
            done.append(pol.person_policy_name(cid))
        if st.get("eso_clusters"):
            _, auths = a._call("GET", "sys/auth")
            auths = self._data(auths) or {}
            for key, e in sorted((st.get("eso_clusters") or {}).items()):
                acc = (auths.get(f"{e.get('mount')}/") or {}).get("accessor")
                if acc:
                    a._call("POST", f"sys/policies/acl/{pol.eso_policy_name(cid, key)}",
                            {"policy": pol.eso_policy(kv, prefix, cid, key, acc)})
                    done.append(pol.eso_policy_name(cid, key))
        for name, g in sorted((st.get("person_groups") or {}).items()):
            try:
                text = pol.group_policy(kv, prefix, cid, name, g.get("classes"), g.get("clusters"))
            except (ValueError, AttributeError):
                self.log(f"group {sf.clean_line(name, 40)} of the agent state unreadable, policy left as is")
                continue
            a._call("POST", f"sys/policies/acl/{pol.group_policy_name(cid, name)}", {"policy": text})
            done.append(pol.group_policy_name(cid, name))
        return done

    def _record_default_policy(self, a):
        """Empreinte (sha256) de la politique `default` de l'instance en
        service, gardée dans l'état de l'agent ; la revue d'un instantané
        restauré la compare (relecture finale I3)."""
        try:
            _, doc = a._call("GET", "sys/policies/acl/default")
        except oc.OpenBaoError as e:
            self.log(f"default policy not read ({e.code}); recorded at the next policy refresh")
            return
        text = str((self._data(doc) or {}).get("policy") or "")
        if text:
            self.save_agent_state(dict(self.agent_state(),
                                       default_policy_sha256=hashlib.sha256(text.encode()).hexdigest()))

    def remove_tf_entity(self, decl):
        """Retire l'entité du jeton enfant d'une déclaration (alias
        `tf-<décl>` du montage `token/`) ; False si elle n'existe pas."""
        a = self.agent_client()
        try:
            _, auths = a._call("GET", "sys/auth")
            auths = (auths or {}).get("data") or auths or {}
            acc = (auths.get("token/") or {}).get("accessor")
            return a.entity_delete_by_alias(pol.tf_alias(decl), acc)
        finally:
            a.revoke_self()

    def op_tf_entity_remove(self, req):
        """Déclaration chiffrée supprimée (tâche 9) : son entité `tf-<décl>`
        retirée. Nom validé par openbao_policy.tf_alias."""
        if not self.agent_state():
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        try:
            alias = pol.tf_alias(str(req.get("decl") or ""))
        except ValueError:
            raise AgentError("bad-request", "decl: declaration identifier expected") from None
        self.step("entity", "running", alias)
        gone = self.remove_tf_entity(req["decl"])
        self.step("entity", "done", "removed" if gone else "no such entity")
        return f"identity entity {alias} " + ("removed" if gone else "already absent")

    # -- 2.10.0 : jeton OpenBao par personne (tâches 6 et 7) -------------------
    def _with_agent(self, fn):
        a = self.agent_client()
        try:
            return fn(a)
        finally:
            a.revoke_self()

    def _person_layout(self, st):
        cid = st["console_id"]
        return cid, st.get("mount") or pol.DEFAULT_MOUNT, f"consoles/{cid}"

    @staticmethod
    def _bool_or_none(req, key):
        v = req.get(key)
        if v is not None and not isinstance(v, bool):
            raise AgentError("bad-request", f"{key}: true or false")
        return v

    def _jwt_spec(self, st, req):
        """Demande `b1-jwt` contrôlée : nom de montage dérivé (jamais lu dans
        la demande), configuration et rôle rendus par openbao_jwt ; plage de
        la console = 127.0.0.1/32 (B1, `--network host`). Revue 6-7 I-1
        (décision 313 B) : découverte par l'émetteur seulement, jamais de
        clés statiques en B1 (une clé posée par la console lui ferait signer
        le jeton d'identité de n'importe qui)."""
        cid = st["console_id"]
        if req.get("pubkeys") is not None:
            raise AgentError("jwt-static-keys-mode-a", "in B1 a person mount reads the keys of Rancher by "
                             "discovery only; static keys belong to mode A")
        ca = req.get("ca_pem")
        try:
            mount = oj.person_mount(cid, req.get("rancher"))
            if ca is not None and (not isinstance(ca, str) or len(ca) > PEM_MAX * 4
                                   or "-----BEGIN CERTIFICATE-----" not in ca):
                raise ValueError("ca_pem: PEM certificate expected")
            config = oj.mount_config(req.get("issuer"), ca_pem=ca)
            role = oj.person_role_params(req.get("client_id"), pol.B1_CIDR, cid)
        except ValueError as e:
            raise AgentError("bad-request", str(e)) from None
        self._person_mount_only(cid, mount)
        return mount, config, role

    @staticmethod
    def _pin_of(config, client_id):
        return {"issuer": config["bound_issuer"], "client_id": str(client_id),
                "ca_pem": (config.get("oidc_discovery_ca_pem") or "").strip() or None}

    def _pin_check(self, st, rancher, pin):
        """Revue 6-7 I-1 : émetteur, client et CA d'un Rancher épinglés dans
        l'état de root à la première pose (gardés après le retrait du
        montage) ; une demande déposée par la console qui les change est
        refusée, le canal root les remplace."""
        have = (st.get("person_pins") or {}).get(rancher)
        if not have or self.root_channel:
            return
        if any((have.get(k) or None) != (pin.get(k) or None) for k in ("issuer", "client_id", "ca_pem")):
            raise AgentError("jwt-pin-mismatch", f"the issuer, client or CA of the Rancher {rancher} are pinned "
                             "since its first set-up; change them with `sudo harvester-secrets person-token "
                             "assistant` on the host")

    @staticmethod
    def _person_mount_only(cid, mount):
        """Revue 6-7 I-3 : les gestes des personnes ne visent jamais un
        montage d'une autre famille (cluster enfant ESO)."""
        if not oj.is_person_mount(cid, mount) or oj.is_eso_mount(cid, mount):
            raise AgentError("jwt-mount-kind", f"{mount} is not a person mount")

    @staticmethod
    def _ttl_s(v):
        v = str(v if v is not None else "").strip()
        try:
            if v[-1:] in ("s", "m", "h"):
                return int(v[:-1]) * {"s": 1, "m": 60, "h": 3600}[v[-1]]
            return int(v or 0)
        except ValueError:
            return -1

    def _jwt_role_diff(self, d, client_id, cid):
        """Écarts entre un rôle `person` relu et celui que rend openbao_jwt
        (liste vide : conforme). `token_policies_template_claims` (propre à
        OpenBao) ferait choisir des politiques par le jeton : refusé."""
        want = oj.person_role_params(client_id, pol.B1_CIDR, cid)
        bad = []
        for k in ("role_type", "user_claim", "token_type"):
            if str(d.get(k) or "") != str(want[k]):
                bad.append(k)
        for k in ("bound_audiences", "token_policies"):
            if list(d.get(k) or []) != list(want[k]):
                bad.append(k)
        if set(d.get("policies") or []) - set(want["token_policies"]):
            bad.append("policies")
        if dict(d.get("claim_mappings") or {}) != want["claim_mappings"]:
            bad.append("claim_mappings")
        if _networks(d.get("token_bound_cidrs")) != _networks(want["token_bound_cidrs"]):
            bad.append("token_bound_cidrs")
        for k in ("token_ttl", "token_max_ttl"):
            if self._ttl_s(d.get(k)) != self._ttl_s(want[k]):
                bad.append(k)
        for k in ("groups_claim", "bound_claims", "bound_subject", "bound_cidrs", "token_period",
                  "token_explicit_max_ttl", "token_num_uses", "token_no_default_policy",
                  "token_policies_template_claims", "verbose_oidc_logging") + tuple(ROLE_RESET):
            if d.get(k):
                bad.append(k)
        return bad

    @staticmethod
    def _jwt_config_diff(d, issuer, ca_pem, pubkeys_allowed, static):
        """Écarts d'une configuration relue : émetteur, algorithme, rôle par
        défaut, une seule source de clés (la découverte par l'émetteur avec
        sa CA, ou des clés statiques parmi celles que l'agent a posées)."""
        bad = []
        if str(d.get("bound_issuer") or "") != issuer:
            bad.append("bound_issuer")
        if list(d.get("jwt_supported_algs") or []) != list(oj.ALGS):
            bad.append("jwt_supported_algs")
        if str(d.get("default_role") or "") != oj.PERSON_ROLE:
            bad.append("default_role")
        for k in ("jwks_url", "jwks_ca_pem", "provider_config", "oidc_client_id"):
            if d.get(k):
                bad.append(k)
        keys = [str(k).strip() for k in d.get("jwt_validation_pubkeys") or []]
        if static:
            if d.get("oidc_discovery_url") or not keys or set(keys) - {str(k).strip() for k in pubkeys_allowed}:
                bad.append("jwt_validation_pubkeys")
        else:
            if str(d.get("oidc_discovery_url") or "") != issuer or keys:
                bad.append("oidc_discovery_url")
            if str(d.get("oidc_discovery_ca_pem") or "").strip() != str(ca_pem or "").strip():
                bad.append("oidc_discovery_ca_pem")
        return bad

    def op_b1_jwt(self, req):
        """Méthode `jwt` d'un Rancher posée sans redémarrer OpenBao (essai 6) :
        montage (s'il manque), politique commune, configuration (découverte),
        rôle `person` ; rôles du montage relus (un rôle étranger est retiré),
        configuration et rôle comparés au rendu d'openbao_jwt. Émetteur,
        client et CA épinglés (revue 6-7 I-1) ; sur un montage existant, un
        échec repose l'ancienne configuration et l'ancien rôle (M-4)."""
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        mount, config, role = self._jwt_spec(st, req)
        groups_only = self._bool_or_none(req, "groups_only")
        if groups_only is None:
            groups_only = bool(st.get("person_groups_only"))
        cid, kv, prefix = self._person_layout(st)
        rancher = str(req["rancher"])
        if rancher not in (st.get("person_jwt") or {}) and len(st.get("person_jwt") or {}) >= pol.MAX_PERSON_MOUNTS:
            # revue 6-7 M-8 : une console ne bourre pas l'instance de montages
            raise AgentError("limit-jwt-mounts", f"at most {pol.MAX_PERSON_MOUNTS} Rancher jwt auth methods")
        pin = self._pin_of(config, req["client_id"])
        self._pin_check(st, rancher, pin)
        self.step("mount", "running", mount)

        def apply(a):
            _, auths = a._call("GET", "sys/auth")
            cur = (self._data(auths) or {}).get(mount + "/")
            if cur and (cur or {}).get("type") != "jwt":
                raise AgentError("jwt-mount-taken", f"{mount}/ is already used by another auth method")
            if not cur:
                a._call("POST", f"sys/auth/{mount}", {"type": "jwt",
                                                      "description": f"harvester-ops {cid} Rancher {rancher}"})
                self.undo.append(("jwt mount removed",
                                  lambda: self._with_agent(lambda b: b._call("DELETE", f"sys/auth/{mount}"))))
            else:
                self._keep_old_jwt(a, mount, list(role) + list(ROLE_RESET))
            self.step("mount", "done", mount + ("/ kept" if cur else "/ created"))
            self.step("policy", "running", pol.person_policy_name(cid))
            a._call("POST", f"sys/policies/acl/{pol.person_policy_name(cid)}",
                    {"policy": pol.person_policy(kv, prefix, cid, groups_only)})
            self.step("policy", "done", pol.person_policy_name(cid) + (" (groups only)" if groups_only else ""))
            self.step("config", "running", "discovery")
            a._call("POST", f"auth/{mount}/config", config)
            a._call("POST", f"auth/{mount}/role/{oj.PERSON_ROLE}", dict(role, **ROLE_RESET))
            for other in self._list(a, f"auth/{mount}/role"):
                if other != oj.PERSON_ROLE:
                    a._call("DELETE", f"auth/{mount}/role/{other}")
                    self.log(f"role {sf.clean_line(other, 64)} of {mount} removed (not set by the console)")
            _, doc = a._call("GET", f"auth/{mount}/config")
            bad = self._jwt_config_diff(self._data(doc) or {}, config["bound_issuer"], config.get(
                "oidc_discovery_ca_pem"), [], False)
            _, doc = a._call("GET", f"auth/{mount}/role/{oj.PERSON_ROLE}")
            bad += self._jwt_role_diff(self._data(doc) or {}, req["client_id"], cid)
            if bad:
                raise AgentError("jwt-readback", f"{mount}: read back differs ({', '.join(bad)})")
            self.step("config", "done", f"{mount}: configuration and role read back")
        self._with_agent(apply)
        entry = {"mount": mount, "client_id": str(req["client_id"]), "issuer": config["bound_issuer"],
                 "keys": "discovery", "ca_pem": config.get("oidc_discovery_ca_pem"),
                 "at": self.now(), "by": self.status.get("requested_by")}
        st = self.agent_state()
        st["person_jwt"] = dict(st.get("person_jwt") or {}, **{rancher: entry})
        st["person_pins"] = dict(st.get("person_pins") or {}, **{rancher: dict(
            pin, at=self.now(), by=self.status.get("requested_by"), root=self.root_channel)})
        st["person_groups_only"] = groups_only
        self.save_agent_state(st)
        self.undo = []
        self.status["info"] = {"mount": mount, "keys": entry["keys"]}
        return f"jwt auth method {mount} set"

    # champs de configuration qu'un retour arrière repose (ceux que b1-jwt écrit)
    CONFIG_KEYS = ("bound_issuer", "jwt_supported_algs", "default_role", "oidc_discovery_url",
                   "oidc_discovery_ca_pem", "jwt_validation_pubkeys")

    def _keep_old_jwt(self, a, mount, role_keys):
        """Revue 6-7 M-4 : configuration et rôle d'un montage existant relus
        avant d'être réécrits ; un échec de la suite les repose."""
        try:
            _, doc = a._call("GET", f"auth/{mount}/config")
            old_conf = {k: v for k, v in (self._data(doc) or {}).items() if k in self.CONFIG_KEYS and v}
        except oc.OpenBaoError:
            old_conf = None
        try:
            _, doc = a._call("GET", f"auth/{mount}/role/{oj.PERSON_ROLE}")
            d = self._data(doc) or {}
            old_role = {k: d[k] for k in role_keys if k in d}
        except oc.OpenBaoError:
            old_role = None

        def back(b):
            if old_conf:
                b._call("POST", f"auth/{mount}/config", old_conf)
            if old_role is not None:
                b._call("POST", f"auth/{mount}/role/{oj.PERSON_ROLE}", old_role)
        self.undo.append(("jwt configuration and role put back", lambda: self._with_agent(back)))

    def op_b1_jwt_remove(self, req):
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        cid = st["console_id"]
        try:
            mount = oj.person_mount(cid, req.get("rancher"))
        except ValueError as e:
            raise AgentError("bad-request", str(e)) from None
        self._person_mount_only(cid, mount)
        rancher = str(req["rancher"])
        rest = {k: v for k, v in (st.get("person_jwt") or {}).items() if k != rancher}
        self.step("mount", "running", mount)

        def remove(a):
            _, auths = a._call("GET", "sys/auth")
            cur = (self._data(auths) or {}).get(mount + "/")
            if cur and cur.get("type") == "jwt":
                a._call("DELETE", f"sys/auth/{mount}")
            self.step("mount", "done", f"{mount}/ " + ("removed" if cur else "already absent"))
            if not rest:
                a._call("DELETE", f"sys/policies/acl/{pol.person_policy_name(cid)}")
                self.step("policy", "done", pol.person_policy_name(cid) + " removed (last Rancher)")
        self._with_agent(remove)
        st = self.agent_state()
        st["person_jwt"] = rest
        self.save_agent_state(st)
        return f"jwt auth method {mount} removed"

    def _person_accessors(self, a, st):
        _, auths = a._call("GET", "sys/auth")
        auths = self._data(auths) or {}
        mounts = {e.get("mount") for e in (st.get("person_jwt") or {}).values() if isinstance(e, dict)}
        return {(v or {}).get("accessor") for k, v in auths.items()
                if k.rstrip("/") in mounts and (v or {}).get("type") == "jwt" and (v or {}).get("accessor")}

    def op_b1_groups(self, req):
        """Groupe interne `hops-<id>-<nom>` (décision 303 A) : sa politique
        rendue par openbao_policy, ses membres (entités d'une personne
        connue : alias sur un montage `jwt` de cette console, aucune
        politique propre). Retrait. Seul, `groups_only` réécrit la politique
        commune (« lecture seulement par groupe »)."""
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        cid, kv, prefix = self._person_layout(st)
        groups_only = self._bool_or_none(req, "groups_only")
        remove = self._bool_or_none(req, "remove")
        name = req.get("name")
        if name is None:
            if groups_only is None or any(req.get(k) is not None for k in ("classes", "clusters", "members",
                                                                          "remove")):
                raise AgentError("bad-request", "a group name, or groups_only alone")
            self._with_agent(lambda a: a._call("POST", f"sys/policies/acl/{pol.person_policy_name(cid)}",
                                               {"policy": pol.person_policy(kv, prefix, cid, groups_only)}))
            self.save_agent_state(dict(self.agent_state(), person_groups_only=groups_only))
            self.step("policy", "done", pol.person_policy_name(cid) + (" (groups only)" if groups_only else ""))
            return "person policy " + ("reduced to groups" if groups_only else "restored")
        if groups_only is not None:
            raise AgentError("bad-request", "groups_only goes alone")
        try:
            gname, pname = pol.group_name(cid, name), pol.group_policy_name(cid, name)
            if remove:
                if any(req.get(k) is not None for k in ("classes", "clusters", "members")):
                    raise ValueError("remove takes the group name only")
            else:
                text = pol.group_policy(kv, prefix, cid, name, req.get("classes"), req.get("clusters"))
                body = pol.group_body(cid, name, req.get("members"))
        except ValueError as e:
            raise AgentError("bad-request", str(e)) from None
        groups = dict(st.get("person_groups") or {})
        if not remove and name not in groups and len(groups) >= pol.MAX_GROUPS:
            raise AgentError("limit-groups", f"at most {pol.MAX_GROUPS} groups")       # revue 6-7 M-8
        self.step("group", "running", gname)
        if remove:
            def drop(a):
                try:
                    a._call("DELETE", f"identity/group/name/{gname}")
                except oc.OpenBaoError as e:
                    if e.code != "openbao-missing":
                        raise
                a._call("DELETE", f"sys/policies/acl/{pname}")
            self._with_agent(drop)
            groups.pop(name, None)
            self.save_agent_state(dict(self.agent_state(), person_groups=groups))
            self.step("group", "done", f"{gname} removed")
            return f"group {gname} removed"

        def put(a):
            accs = self._person_accessors(a, st)
            for eid in body["member_entity_ids"]:
                try:
                    _, doc = a._call("GET", f"identity/entity/id/{eid}")
                except oc.OpenBaoError as e:
                    if e.code == "openbao-missing":
                        raise AgentError("group-member-unknown", f"no identity entity {eid}") from None
                    raise
                d = self._data(doc) or {}
                aliases = d.get("aliases") or []
                if d.get("policies") or not aliases \
                        or any((al or {}).get("mount_accessor") not in accs for al in aliases):
                    raise AgentError("group-member-refused", f"entity {eid} is not a person of this console")
            a._call("POST", f"sys/policies/acl/{pname}", {"policy": text})
            a._call("POST", f"identity/group/name/{gname}", body)
            _, doc = a._call("GET", f"identity/group/name/{gname}")
            d = self._data(doc) or {}
            if d.get("type") != "internal" or list(d.get("policies") or []) != [pname] \
                    or sorted(d.get("member_entity_ids") or []) != sorted(body["member_entity_ids"]):
                raise AgentError("group-readback", f"{gname}: read back differs")
        self._with_agent(put)
        groups[name] = {"classes": list(req["classes"]), "clusters": list(req["clusters"]),
                        "members": list(body["member_entity_ids"]), "at": self.now(),
                        "by": self.status.get("requested_by")}
        self.save_agent_state(dict(self.agent_state(), person_groups=groups))
        self.step("group", "done", f"{gname}: {len(body['member_entity_ids'])} member(s)")
        return f"group {gname} set"

    # -- 2.10.0 : External Secrets Operator (tâche 9) ---------------------------
    def make_eso_tls(self, uid, ip):
        """CA dédiée à l'écouteur ESO (10 ans, contrainte à son adresse) et
        certificat serveur de 2 ans pour cette adresse ; la clé de la CA est
        jetée comme celle de l'écouteur local. La CA (publique) est remise à
        la console dans le répertoire de ses identifiants : les clusters
        enfants la reçoivent dans `caBundle`."""
        tls = self.p.b1_etc / "tls"
        tls.mkdir(mode=0o700, exist_ok=True)
        for f in ("eso.key", "eso.pem"):        # fichiers 0400 d'une pose précédente
            (tls / f).unlink(missing_ok=True)
        work = self.p.work / "eso-pki"
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        run = self.host.run
        run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", str(work / "ca.key"), "-out", str(work / "ca.pem"), "-days", "3650",
             "-subj", "/CN=harvester-ops OpenBao ESO CA", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash",
             "-addext", f"nameConstraints=critical,permitted;IP:{ip}/255.255.255.255"])
        run(["openssl", "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", str(tls / "eso.key"), "-out", str(work / "server.csr"), "-subj", f"/CN={ip}"])
        (work / "ext.cnf").write_text(f"subjectAltName=IP:{ip}\nbasicConstraints=critical,CA:FALSE\n"
                                      "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
                                      "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
        run(["openssl", "x509", "-req", "-in", str(work / "server.csr"), "-CA", str(work / "ca.pem"),
             "-CAkey", str(work / "ca.key"), "-CAcreateserial", "-out", str(tls / "eso.pem"), "-days", "730",
             "-extfile", str(work / "ext.cnf")])
        ca = (work / "ca.pem").read_text()
        for f, mode in (("eso.key", 0o400), ("eso.pem", 0o444)):
            os.chmod(tls / f, mode)
            self.own(tls / f, uid)
        shutil.rmtree(work, ignore_errors=True)
        _w(self.p.b1_etc / "eso-ca.pem", ca, 0o644)
        self.p.etc.mkdir(parents=True, exist_ok=True)
        _w(self.p.etc / "eso-ca.pem", ca, 0o644)
        return ca

    def _drop_eso_tls(self):
        for f in (self.p.b1_etc / "tls" / "eso.key", self.p.b1_etc / "tls" / "eso.pem",
                  self.p.b1_etc / "eso-ca.pem", self.p.etc / "eso-ca.pem"):
            try:
                f.unlink()
            except OSError:
                pass

    def _template(self):
        """Modèle de configuration de B1 : celui posé avec le paquet, ou, s'il
        est d'avant 2.10.0 (sans ligne `__ESO_LISTENER__`), celui
        qu'install.sh a posé dans /opt (root) ; il remplace alors l'ancien."""
        try:
            cur = (self.p.b1_etc / "openbao.hcl.tpl").read_text()
        except OSError:
            cur = ""
        if re.search(r"^__ESO_LISTENER__$", cur, re.M):
            return cur
        try:
            new = (self.p.unit_src.parent / "openbao.hcl.tpl").read_text()
        except OSError:
            new = ""
        if not re.search(r"^__ESO_LISTENER__$", new, re.M):
            raise AgentError("eso-listener-template", "the OpenBao template has no ESO listener line: upgrade "
                             "harvester-ops (install.sh --upgrade) first")
        _w(self.p.b1_etc / "openbao.hcl.tpl", new, 0o644)
        return new

    def _config_template(self, st):
        """Revue 8-10 I-4 : modèle d'un rendu. Écouteur ESO allumé : jamais un
        modèle qui le perdrait (`_template()`, qui prend celui d'install.sh
        ou refuse) ; sinon le modèle posé."""
        if st.get("eso_listener"):
            return self._template()
        return (self.p.b1_etc / "openbao.hcl.tpl").read_text()

    @staticmethod
    def _trial_render(st, tpl, prev=None, init=False):
        """Rendu d'essai, avant le premier geste d'une opération : un modèle
        qui ne se rend pas refuse l'opération sans rien changer."""
        try:
            pol.b1_config(st["console_id"], st["current_key"], tpl, previous_key_id=prev, init=init,
                          mount=st.get("mount", pol.DEFAULT_MOUNT), eso_listener=st.get("eso_listener"))
        except ValueError as e:
            raise AgentError("b1-template", f"the OpenBao configuration cannot be rendered: {e}") from None

    def op_b1_eso_listener(self, req):
        """Point 298 : second écouteur TLS sur `<adresse de l'hôte>:<port>`
        (8212 par défaut), éteint tant qu'aucun cluster n'a ESO. Il expose
        l'API sur le réseau (revue 8-10 I-5) : les rôles AppRole de la console
        et de l'agent, celui des personnes et les jetons enfants d'OpenTofu
        sont liés à 127.0.0.1/32, les points de rekey non authentifiés
        fermés ; seuls les rôles `eso` y aboutissent. Le pare-feu de l'hôte
        n'est pas touché (à ouvrir aux seuls réseaux des nœuds enfants).
        `address` null : écouteur retiré."""
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        addr = req.get("address")
        if addr is not None:
            try:
                ip, port = pol.check_eso_listen(addr)
            except ValueError as e:
                raise AgentError("bad-request", str(e)) from None
            if ip not in self.host.addresses():
                raise AgentError("eso-listener-not-local", f"{ip} is not an address of this host")
            addr = f"{ip}:{port}"
        elif st.get("eso_clusters"):
            raise AgentError("eso-listener-in-use", "child clusters still use the ESO listener: remove them first")
        old = st.get("eso_listener")
        if old == addr:
            self.step("listener", "done", f"ESO listener {addr or 'off'} unchanged")
            self.status["info"] = {"eso_listener": addr, "eso_ca_pem": self._eso_ca() if addr else None}
            return "ESO listener unchanged"
        uid = self.host.uid_of(ACCOUNT)
        if uid is None:
            raise AgentError("b1-not-installed", f"no {ACCOUNT} account on this host")
        tpl = self._template()                   # avant tout geste : un modèle trop ancien ne change rien
        self.step("listener", "running", addr or "off")
        ca = None
        if addr:
            ca = self.make_eso_tls(uid, addr.rpartition(":")[0])
        new = dict(st, eso_listener=addr)

        def back():
            self.render(st, tpl)
            self.own(self.p.b1_etc / "openbao.hcl", uid)
            if not old:
                self._drop_eso_tls()
            self.restart()
            self.wait_unsealed(self.b1_ca(), "OpenBao with its previous listeners")
        self.undo.append(("listeners put back", back))
        self.render(new, tpl)
        self.own(self.p.b1_etc / "openbao.hcl", uid)
        self.restart()
        self.wait_unsealed(self.b1_ca(), "OpenBao with the ESO listener" if addr else "OpenBao without the ESO listener")
        if not addr:
            self._drop_eso_tls()
        self.save_agent_state(dict(self.agent_state(), eso_listener=addr))
        self.undo = []
        self.step("listener", "done", f"ESO listener on {addr}" if addr else "ESO listener removed")
        self.status["info"] = {"eso_listener": addr, "eso_ca_pem": ca}
        return f"ESO listener {'on ' + addr if addr else 'removed'}"

    def _eso_ca(self):
        try:
            return (self.p.b1_etc / "eso-ca.pem").read_text()
        except OSError:
            return None

    def _eso_spec(self, st, req):
        cid = st["console_id"]
        key = str(req.get("key") or "")
        try:
            mount = oj.eso_mount(cid, key)
            for k in ("harvester", "namespace", "cluster"):
                v = req.get(k)
                if not isinstance(v, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$", v):
                    raise ValueError(f"{k}: a name")
            if hashlib.sha256(f"{req['harvester']}/{req['namespace']}/{req['cluster']}".encode()) \
                    .hexdigest()[:8] != key:
                raise ValueError("key: not the key of this child cluster")
            pubkeys = req.get("pubkeys")
            if not isinstance(pubkeys, list) or not 1 <= len(pubkeys) <= PUBKEYS_MAX \
                    or any(not isinstance(k, str) or len(k) > PEM_MAX for k in pubkeys):
                raise ValueError(f"pubkeys: 1 to {PUBKEYS_MAX} PEM public keys")
            config = oj.eso_mount_config(req.get("issuer"), pubkeys)
            if req.get("sa_uids") is not None and not isinstance(req.get("sa_uids"), dict):
                raise ValueError("sa_uids: {namespace: uid}")
            role = oj.eso_role_params(cid, key, req.get("namespaces"), req.get("node_cidr"), req.get("sa_uids"))
        except (ValueError, KeyError) as e:
            raise AgentError("bad-request", str(e)) from None
        if not oj.is_eso_mount(cid, mount) or oj.is_person_mount(cid, mount):
            raise AgentError("jwt-mount-kind", f"{mount} is not an ESO mount")
        return key, mount, config, role

    def _eso_role_diff(self, d, want):
        bad = []
        for k in ("role_type", "user_claim", "token_type", "bound_claims_type"):
            if str(d.get(k) or "") != str(want[k]):
                bad.append(k)
        for k in ("bound_audiences", "token_policies"):
            if list(d.get(k) or []) != list(want[k]):
                bad.append(k)
        if set(d.get("policies") or []) - set(want["token_policies"]):
            bad.append("policies")
        bc = d.get("bound_claims") or {}
        wbc = want["bound_claims"]
        if set(bc) != set(wbc) or bc.get("/kubernetes.io/serviceaccount/name") != wbc[
                "/kubernetes.io/serviceaccount/name"] or sorted(bc.get("/kubernetes.io/namespace") or []) != \
                wbc["/kubernetes.io/namespace"] or sorted(bc.get(oj.SA_UID_CLAIM) or []) != \
                sorted(wbc.get(oj.SA_UID_CLAIM) or []):
            bad.append("bound_claims")
        if dict(d.get("claim_mappings") or {}) != want["claim_mappings"]:
            bad.append("claim_mappings")
        if _networks(d.get("token_bound_cidrs")) != _networks(want.get("token_bound_cidrs")):
            bad.append("token_bound_cidrs")
        for k in ("token_ttl", "token_max_ttl"):
            if self._ttl_s(d.get(k)) != self._ttl_s(want[k]):
                bad.append(k)
        for k in ("groups_claim", "bound_subject", "bound_cidrs", "token_period", "token_explicit_max_ttl",
                  "token_num_uses", "token_no_default_policy", "token_policies_template_claims",
                  "verbose_oidc_logging") + tuple(ROLE_RESET):
            if d.get(k):
                bad.append(k)
        return bad

    @staticmethod
    def _eso_config_diff(d, config):
        bad = []
        if str(d.get("bound_issuer") or "") != config["bound_issuer"]:
            bad.append("bound_issuer")
        if list(d.get("jwt_supported_algs") or []) != list(oj.ALGS):
            bad.append("jwt_supported_algs")
        if str(d.get("default_role") or "") != oj.ESO_ROLE:
            bad.append("default_role")
        for k in ("jwks_url", "jwks_ca_pem", "provider_config", "oidc_client_id", "oidc_discovery_url"):
            if d.get(k):
                bad.append(k)
        if sorted(str(k).strip() for k in d.get("jwt_validation_pubkeys") or []) != \
                sorted(config["jwt_validation_pubkeys"]):
            bad.append("jwt_validation_pubkeys")
        return bad

    def op_b1_eso_cluster(self, req):
        """Point 295 : méthode `jwt` d'un cluster enfant, posée sans
        redémarrer OpenBao ; rejouée à chaque namespace activé ou retiré
        (décision 304 B) et pour remplacer les clés (`eso-keys-changed`).
        Les clés publiques viennent du JWKS du cluster enfant, lu par la
        console avec son kubeconfig (accès anonyme refusé par RKE2) : elles
        n'ouvrent que les chemins `eso/<clé>/*`, que la console peut déjà
        lire."""
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        if not st.get("eso_listener"):
            raise AgentError("eso-listener-off", "the ESO listener is off: set its address first")
        key, mount, config, role = self._eso_spec(st, req)
        if key not in (st.get("eso_clusters") or {}) and len(st.get("eso_clusters") or {}) >= pol.MAX_ESO_CLUSTERS:
            raise AgentError("limit-eso-clusters", f"at most {pol.MAX_ESO_CLUSTERS} child clusters with "
                                                   "External Secrets")                # revue 6-7 M-8
        cid, kv, prefix = self._person_layout(st)
        self.step("mount", "running", mount)

        def apply(a):
            _, auths = a._call("GET", "sys/auth")
            cur = (self._data(auths) or {}).get(mount + "/")
            if cur and cur.get("type") != "jwt":
                raise AgentError("jwt-mount-taken", f"{mount}/ is already used by another auth method")
            if not cur:
                a._call("POST", f"sys/auth/{mount}", {"type": "jwt", "description": f"harvester-ops {cid} ESO "
                                                      f"{req['harvester']}/{req['namespace']}/{req['cluster']}"})
                self.undo.append(("ESO jwt mount removed",
                                  lambda: self._with_agent(lambda b: b._call("DELETE", f"sys/auth/{mount}"))))
                _, auths = a._call("GET", "sys/auth")
                cur = (self._data(auths) or {}).get(mount + "/") or {}
            acc = cur.get("accessor")
            if not acc:
                raise AgentError("jwt-readback", f"{mount}: no accessor")
            self.step("mount", "done", f"{mount}/ ready")
            self.step("policy", "running", pol.eso_policy_name(cid, key))
            a._call("POST", f"sys/policies/acl/{pol.eso_policy_name(cid, key)}",
                    {"policy": pol.eso_policy(kv, prefix, cid, key, acc)})
            self.step("policy", "done", pol.eso_policy_name(cid, key))
            self.step("config", "running", f"{len(config['jwt_validation_pubkeys'])} key(s), "
                                           f"{len(role['bound_claims']['/kubernetes.io/namespace'])} namespace(s)")
            a._call("POST", f"auth/{mount}/config", config)
            a._call("POST", f"auth/{mount}/role/{oj.ESO_ROLE}", dict(role, **ROLE_RESET))
            for other in self._list(a, f"auth/{mount}/role"):
                if other != oj.ESO_ROLE:
                    a._call("DELETE", f"auth/{mount}/role/{other}")
                    self.log(f"role {sf.clean_line(other, 64)} of {mount} removed (not set by the console)")
            _, doc = a._call("GET", f"auth/{mount}/config")
            bad = self._eso_config_diff(self._data(doc) or {}, config)
            _, doc = a._call("GET", f"auth/{mount}/role/{oj.ESO_ROLE}")
            bad += self._eso_role_diff(self._data(doc) or {}, role)
            if bad:
                raise AgentError("jwt-readback", f"{mount}: read back differs ({', '.join(bad)})")
            self.step("config", "done", f"{mount}: configuration and role read back")
        self._with_agent(apply)
        entry = {"mount": mount, "harvester": req["harvester"], "namespace": req["namespace"],
                 "cluster": req["cluster"], "issuer": config["bound_issuer"],
                 "pubkeys": config["jwt_validation_pubkeys"],
                 "namespaces": role["bound_claims"]["/kubernetes.io/namespace"],
                 "node_cidr": (role.get("token_bound_cidrs") or [None])[0],
                 "sa_uids": oj.eso_sa_uids(req.get("sa_uids"), role["bound_claims"]["/kubernetes.io/namespace"]),
                 "at": self.now(), "by": self.status.get("requested_by")}
        st = self.agent_state()
        st["eso_clusters"] = dict(st.get("eso_clusters") or {}, **{key: entry})
        self.save_agent_state(st)
        self.undo = []
        self.status["info"] = {"mount": mount, "key": key, "eso_listener": st.get("eso_listener"),
                               "eso_ca_pem": self._eso_ca()}
        return f"ESO jwt auth method {mount} set"

    def op_b1_eso_cluster_remove(self, req):
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        cid = st["console_id"]
        key = str(req.get("key") or "")
        try:
            mount = oj.eso_mount(cid, key)
        except ValueError as e:
            raise AgentError("bad-request", str(e)) from None
        self.step("mount", "running", mount)

        def remove(a):
            _, auths = a._call("GET", "sys/auth")
            cur = (self._data(auths) or {}).get(mount + "/")
            if cur and cur.get("type") == "jwt":
                a._call("DELETE", f"sys/auth/{mount}")
            a._call("DELETE", f"sys/policies/acl/{pol.eso_policy_name(cid, key)}")
            self.step("mount", "done", f"{mount}/ " + ("removed" if cur else "already absent"))
        self._with_agent(remove)
        st = self.agent_state()
        st["eso_clusters"] = {k: v for k, v in (st.get("eso_clusters") or {}).items() if k != key}
        self.save_agent_state(st)
        self.status["info"] = {"left": len(st["eso_clusters"])}
        return f"ESO jwt auth method {mount} removed"

    def op_b1_remove(self, req):
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.now()))
        self.step("stop", "running")
        self.host.run(["systemctl", "disable", "--now", UNIT], check=False)
        (self.p.units / UNIT).unlink(missing_ok=True)
        self.host.run(["systemctl", "daemon-reload"], check=False)
        self.podman_secret(SECRET)
        self.podman_secret(SECRET_PREV)
        self.step("stop", "done")
        self.step("set-aside", "running", "data and keys kept aside, never deleted here")
        for src in (self.p.b1_data, self.p.b1_etc):
            if src.exists():
                src.rename(src.with_name(f"{src.name}.removed-{stamp}"))
        self.p.logrotate.unlink(missing_ok=True)
        self._remove_credentials()
        self.step("set-aside", "done", f"*.removed-{stamp}")
        return "OpenBao B1 removed (data kept aside)"

    def op_rotate_unseal_key(self, req):
        """Essai 7 : current = nouvelle, previous = ancienne, redémarrage,
        `sys/rotate/root` (sans lui la clé racine reste chiffrée par
        l'ancienne), instantané, PUIS retrait de l'ancienne."""
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        tpl = self._config_template(st)
        uid = self.host.uid_of(ACCOUNT)
        old = st["current_key"]
        keys = self.p.b1_etc / "keys"
        new = self.new_key_id({k["id"] for k in st["keys"]})
        st2 = dict(st, current_key=new)
        # revue 8-10 I-4 : les trois rendus de l'opération essayés avant de
        # toucher aux secrets podman (un échec après la bascule laissait B1
        # non descellable au redémarrage suivant)
        self._trial_render(st2, tpl, prev=old)
        self._trial_render(st2, tpl)
        self._trial_render(st, tpl)
        self.step("new-key", "running")
        _w(keys / f"{new}.key", secrets.token_bytes(32), 0o400, binary=True)

        def back():
            self.podman_secret(SECRET, keys / f"{old}.key")
            self.podman_secret(SECRET_PREV)
            self.render(st, tpl)
            self.own(self.p.b1_etc / "openbao.hcl", uid)
            self.unit_env(uid, f"{IMAGE}:current")
            (keys / f"{new}.key").unlink(missing_ok=True)
            self.restart()
        self.undo.append(("previous unseal key restored", back))
        self.podman_secret(SECRET_PREV, keys / f"{old}.key")
        self.podman_secret(SECRET, keys / f"{new}.key")
        self.render(st2, tpl, prev=old)
        self.own(self.p.b1_etc / "openbao.hcl", uid)
        self.unit_env(uid, f"{IMAGE}:current", prev=True)
        self.step("new-key", "done", f"{new} (previous {old})")
        self.step("restart", "running", "both keys")
        self.restart()
        self.wait_unsealed(self.b1_ca())
        self.step("restart", "done")
        self.step("rotate-root", "running", "sys/rotate/root with the agent identity")
        a = self.agent_client()
        a._call("POST", "sys/rotate/root", {})
        self.step("rotate-root", "done")
        a.revoke_self()
        self.step("snapshot", "running")
        name = self.agent_snapshot(st2, "rotation")
        self.step("snapshot", "done", name)
        self.undo = []                               # la clé racine est rechiffrée : plus de retour
        self.step("retire-old", "running")
        self.render(st2, tpl)
        self.own(self.p.b1_etc / "openbao.hcl", uid)
        self.unit_env(uid, f"{IMAGE}:current")
        self.podman_secret(SECRET_PREV)
        shutil.copyfile(keys / f"{new}.key", self.p.b1_etc / "unseal.key.next")
        os.chmod(self.p.b1_etc / "unseal.key.next", 0o400)
        os.replace(self.p.b1_etc / "unseal.key.next", self.p.b1_etc / "unseal.key")
        for k in st2["keys"]:
            if k["id"] == old:
                k["retired"] = self.now()
        st2["keys"] = st2["keys"] + [{"id": new, "created": self.now(), "retired": None}]
        st2["offline_copy"] = self._offline_pending(new)       # copie de la NOUVELLE clé attendue
        (self.p.etc / EXPORT_NAME).unlink(missing_ok=True)      # une copie de l'ancienne ne sert plus
        self.save_agent_state(st2)
        self.restart()
        self.wait_unsealed(self.b1_ca(), "OpenBao with the new key only")
        self.step("retire-old", "done", f"{old} kept {KEY_KEEP_DAYS} days for older snapshots")
        return f"unseal key rotated to {new}"

    def _snapshot_dirs(self):
        """(étiquette, répertoire tenu par descripteur) : celui de l'agent,
        puis le `snapshot_dir` réglé par l'administrateur dans la console,
        ouvert composant par composant sans suivre de lien (safe_fs)."""
        own = self.p.b1_data / "snapshots"
        sd = self.console_settings().get("snapshot_dir")
        for label, path in (("agent", own), ("console", sd)):
            if not isinstance(path, (str, Path)) or not os.path.isabs(str(path)):
                continue
            try:
                with sf.SafeDir.open_base("/") as root:
                    d = root.open_rel(str(path).lstrip("/"))
                d.as_console = False
            except PermissionError:
                # tâche 13 (essai réel) : un NFS en root_squash refuse root ;
                # le répertoire de la console s'ouvre alors sous son compte
                if label != "console":
                    continue
                try:
                    with self._as_console(), sf.SafeDir.open_base("/") as root:
                        d = root.open_rel(str(path).lstrip("/"))
                except OSError:
                    continue
                d.as_console = True
            except OSError:
                continue
            yield label, d

    @contextlib.contextmanager
    def _as_console(self):
        """Identité effective du compte de la console le temps d'une
        ouverture (un fichier ouvert garde ses droits de lecture ensuite) ;
        rien à faire hors root."""
        if os.geteuid() != 0:
            yield
            return
        uid, gid = self.host.uid_of("harvester-ops"), self.host.group_gid("harvester-ops")
        if uid is None or gid is None:
            raise PermissionError("no harvester-ops account")
        os.setegid(gid)
        os.seteuid(uid)
        try:
            yield
        finally:
            os.seteuid(0)
            os.setegid(0)

    def _snapshot_file(self, name):
        """Copie de travail de l'instantané `name` (revue M2) : ouvert par le
        descripteur de son répertoire, sans suivre de lien, sans bloquer
        (O_NONBLOCK : un tube nommé n'arrête pas l'agent), fichier ordinaire
        à un seul lien physique, taille bornée et relue sur le MÊME
        descripteur (fstat), copie arrêtée si le fichier grandit. Rend
        (copie, identifiant de clé tiré du nom ou None, date fstat)."""
        if not SNAP_RE.match(str(name or "")):
            raise AgentError("snapshot-bad-name", "bad snapshot file name")
        for _label, d in self._snapshot_dirs():
            with d:
                try:
                    with (self._as_console() if getattr(d, "as_console", False) else contextlib.nullcontext()):
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                                     | os.O_NOCTTY, dir_fd=d.fd)
                except OSError:
                    continue
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise AgentError("snapshot-not-plain", "the snapshot is not a plain file with one link")
                if st.st_size > SNAP_MAX:
                    raise AgentError("snapshot-too-big", f"snapshot larger than {SNAP_MAX} bytes")
                self.p.work.mkdir(mode=0o700, parents=True, exist_ok=True)
                dst = self.p.work / name
                n = 0
                with open(dst, "wb") as out:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        n += len(chunk)
                        if n > st.st_size or n > SNAP_MAX:
                            raise AgentError("snapshot-changing", "the snapshot grew while it was copied")
                        out.write(chunk)
                if n == 0:
                    raise AgentError("snapshot-empty", "empty snapshot file")
            m = SNAP_KEY_RE.search(name)
            return dst, (m.group(1) if m else None), st.st_mtime
        raise AgentError("snapshot-missing", "snapshot not found in the snapshot directories")

    def _key_for(self, st, when, named=None):
        """La clé de l'instantané : celle que porte son nom si l'agent la
        garde encore ; sinon (instantané d'avant la 2.9.0, nom sans clé) la
        clé active à sa date. Un nom faux ne fait que désigner une autre clé
        de l'agent : OpenBao refuse alors l'instantané (« could not verify
        hash file ») et tout est remis en place."""
        if named:
            return named
        best = None
        for k in sorted(st["keys"], key=lambda k: k["created"]):
            if k["created"] <= when + 60 and (k["retired"] is None or k["retired"] >= when - 60):
                best = k["id"]
        return best or st["current_key"]

    # -- revue d'un instantané restauré ------------------------------------------
    # Relecture finale I3 : la console obtient la clé de descellement
    # (copie hors ligne) et lit les instantanés ; elle peut donc en fabriquer
    # un. La revue n'est plus une liste de refus mais une liste PERMISE de
    # toute la configuration restaurée ; tout écart refuse l'instantané (le
    # volume précédent est remis), puis tous les jetons de l'instantané sont
    # révoqués (la console et l'agent se reconnectent par AppRole).
    REVIEW_MOUNTS = {"cubbyhole/": "cubbyhole", "identity/": "identity", "sys/": "system", "transit/": "transit"}

    @staticmethod
    def _data(doc):
        return (doc or {}).get("data") if isinstance((doc or {}).get("data"), dict) else (doc or {})

    def _list(self, a, path):
        try:
            _, doc = a._call("LIST", path)
        except oc.OpenBaoError as e:
            if e.code == "openbao-missing":
                return []
            raise
        keys = (self._data(doc) or {}).get("keys") or []
        if len(keys) > 10000:
            raise AgentError("snapshot-untrusted", f"too many entries under {path} in the restored snapshot")
        return [str(k) for k in keys]

    def _untrusted(self, what):
        raise AgentError("snapshot-untrusted", "the restored snapshot does not match this console: "
                         + sf.clean_line(what, 300))

    def review_restored(self, st, console):
        cid, mount = st["console_id"], st.get("mount") or pol.DEFAULT_MOUNT
        roles = {pol.console_role(cid): pol.console_role_params(pol.B1_CIDR), pol.agent_role(cid): pol.agent_role_params()}
        self.op_b1_policies(review=True)
        mine = set()
        a = self.agent_client()
        try:
            for c in (a, console):
                _, me = c._call("GET", "auth/token/lookup-self")
                mine.add(str((self._data(me) or {}).get("accessor") or ""))
            # méthodes d'authentification : token et approle, plus (2.10.0)
            # les montages `jwt` des personnes que l'agent a posés lui-même
            # (état de root) ; un instantané plus ancien peut en avoir moins
            _, auth_doc = a._call("GET", "sys/auth")
            auths = {k: (v or {}).get("type") for k, v in (self._data(auth_doc) or {}).items()
                     if isinstance(v, dict)}
            person = {e.get("mount"): e for e in (st.get("person_jwt") or {}).values() if isinstance(e, dict)}
            # 2.10.0 (tâche 9) : montages `jwt` des clusters enfants (ESO) posés
            # par l'agent, à part de ceux des personnes
            eso = {e.get("mount"): dict(e, key=k) for k, e in (st.get("eso_clusters") or {}).items()
                   if isinstance(e, dict)}
            all_present = {k.rstrip("/") for k, t in auths.items() if k not in ("token/", "approle/")}
            if auths.get("token/") != "token" or auths.get("approle/") != "approle" \
                    or any(auths[k + "/"] != "jwt" or (k not in person and k not in eso) for k in all_present):
                self._untrusted(f"auth methods {sorted(auths)}")
            jwt_present = {m for m in all_present if m in person}
            eso_present = {m for m in all_present if m in eso}
            token_acc = ((self._data(auth_doc) or {}).get("token/") or {}).get("accessor")
            acc_of = {m: ((self._data(auth_doc) or {}).get(m + "/") or {}).get("accessor") for m in all_present}
            jwt_accs = {acc_of[m] for m in jwt_present}
            eso_accs = {acc_of[m] for m in eso_present}
            for m in sorted(eso_present):
                self._review_eso_mount(a, m, eso[m], cid)
            pins = st.get("person_pins") or {}
            for m in sorted(jwt_present):
                r = next((k for k, e in (st.get("person_jwt") or {}).items() if (e or {}).get("mount") == m), None)
                self._review_jwt_mount(a, m, dict(person[m], **{k: v for k, v in (pins.get(r) or {}).items()
                                                              if k in ("issuer", "client_id", "ca_pem")}), cid)
            # montages : les quatre du système, le KV de la console et transit
            _, mounts = a._call("GET", "sys/mounts")
            mounts = {k: (v or {}).get("type") for k, v in (self._data(mounts) or {}).items() if isinstance(v, dict)}
            want = dict(self.REVIEW_MOUNTS, **{f"{mount}/": "kv"})
            if mounts != want:
                self._untrusted(f"secret mounts {sorted(mounts)}")
            # audit : le seul journal déclaré dans la configuration
            _, audit = a._call("GET", "sys/audit")
            audit = {k: (v or {}).get("type") for k, v in (self._data(audit) or {}).items() if isinstance(v, dict)}
            if set(audit) - {"hops/"} or any(t != "file" for t in audit.values()):
                self._untrusted(f"audit devices {sorted(audit)}")
            # politiques : noms et textes exacts
            expected = {pol.console_role(cid): pol.console_policy(mount, f"consoles/{cid}", cid, b1=True),
                        pol.agent_role(cid): pol.agent_policy(cid, mount),
                        pol.tf_role(cid): pol.tf_child_policy(cid, token_acc),
                        pol.boot_policy_name(cid): pol.boot_policy(cid, mount)}
            # 2.10.0 : politique commune (l'un ou l'autre réglage « lecture
            # seulement par groupe », textes rendus par la console) et celles
            # des groupes de l'état de l'agent, au texte exact
            prefix = f"consoles/{cid}"
            person_texts = {pol.person_policy(mount, prefix, cid, g).strip() for g in (False, True)}
            groups = {n: g for n, g in (st.get("person_groups") or {}).items() if isinstance(g, dict)}
            for n, g in groups.items():
                try:
                    expected[pol.group_policy_name(cid, n)] = pol.group_policy(mount, prefix, cid, n, g.get("classes"),
                                                                               g.get("clusters"))
                except ValueError:
                    continue
            for m in eso_present:
                try:
                    expected[pol.eso_policy_name(cid, eso[m]["key"])] = pol.eso_policy(mount, prefix, cid,
                                                                                     eso[m]["key"], acc_of[m])
                except ValueError:
                    self._untrusted(f"ESO jwt auth method {m}")
            for name in self._list(a, "sys/policies/acl"):
                if name == "root":
                    continue
                _, doc = a._call("GET", f"sys/policies/acl/{name}")
                text = str((self._data(doc) or {}).get("policy") or "")
                if name == "default":
                    seen = hashlib.sha256(text.encode()).hexdigest()
                    if st.get("default_policy_sha256") and seen != st["default_policy_sha256"]:
                        self._untrusted("the default policy was changed")
                    continue
                if name == pol.person_policy_name(cid) and text.strip() in person_texts:
                    continue
                if name not in expected or text.strip() != expected[name].strip():
                    self._untrusted(f"policy {name}")
            # rôles AppRole : les deux de cette console, leurs politiques et
            # leurs plages d'adresses, et leurs seuls secret_id attendus
            if set(self._list(a, "auth/approle/role")) != set(roles):
                self._untrusted("AppRole roles " + ", ".join(sorted(self._list(a, "auth/approle/role"))))
            for role, params in roles.items():
                _, doc = a._call("GET", f"auth/approle/role/{role}")
                d = self._data(doc) or {}
                if list(d.get("token_policies") or []) != [role] or set(d.get("policies") or []) - {role} \
                        or d.get("bind_secret_id") is False:
                    self._untrusted(f"AppRole role {role} policies")
                for k in ("token_bound_cidrs", "secret_id_bound_cidrs"):
                    # OpenBao 2.7.2 rend « 127.0.0.1 » pour token_bound_cidrs et
                    # « 127.0.0.1/32 » pour secret_id_bound_cidrs (relevé sur la
                    # VM client) : réseaux comparés, pas les textes
                    if _networks(d.get(k)) != _networks(params[k]):
                        self._untrusted(f"AppRole role {role} {k}")
            sid_acc = {pol.console_role(cid): _read(self.p.etc / "secret-id-accessor")}
            _, look = a._call("POST", f"auth/approle/role/{pol.agent_role(cid)}/secret-id/lookup",
                              {"secret_id": _read(self.p.b1_etc / "agent-secret-id")})
            sid_acc[pol.agent_role(cid)] = str((self._data(look) or {}).get("secret_id_accessor") or "")
            for role, acc in sid_acc.items():
                if self._list(a, f"auth/approle/role/{role}/secret-id") != [acc]:
                    self._untrusted(f"secret_id of the AppRole role {role}")
            # rôles de jeton : celui des jetons d'OpenTofu, tel que posé
            if self._list(a, "auth/token/roles") != [pol.tf_role(cid)]:
                self._untrusted("token roles")
            _, doc = a._call("GET", f"auth/token/roles/{pol.tf_role(cid)}")
            d = self._data(doc) or {}
            tp = pol.tf_token_role_params(cid)
            # revue 8-10 I-5 : lié à 127.0.0.1 (ou, instantané d'avant la
            # 2.10.0, sans liaison : reposée après la restauration)
            if list(d.get("allowed_policies") or []) != tp["allowed_policies"] \
                    or list(d.get("allowed_entity_aliases") or []) != tp["allowed_entity_aliases"] \
                    or d.get("allowed_policies_glob") or d.get("orphan") or d.get("renewable") \
                    or _networks(d.get("token_bound_cidrs")) not in (set(), _networks([pol.B1_CIDR])):
                self._untrusted("token role of the OpenTofu tokens")
            # groupes d'identité : seulement les groupes internes `hops-<id>-*`
            # de l'état de l'agent, chacun avec sa seule politique (2.10.0) ;
            # rien d'autre (un groupe porte des politiques)
            groups_present, group_members = self._review_groups(a, cid, groups)
            # relecture des corrections finales IMP-1 : entités d'identité sans
            # politique, alias sur approle/ (role_id des deux rôles), sur
            # token/ (`tf-<déclaration>`) ou (2.10.0) sur un montage jwt des
            # personnes seulement
            self._review_entities(a, roles, token_acc, jwt_accs - {None},
                                  {e for ms in group_members.values() for e in ms}, eso_accs - {None})
            # m4 : aucun espace de noms (révocation faite dans la racine)
            if self._list(a, "sys/namespaces"):
                self._untrusted("namespaces")
            # jetons : politiques permises, puis tous révoqués sauf les deux
            # de cette revue (révoqués ensuite)
            allowed = {"default", *roles, pol.tf_role(cid), pol.boot_policy_name(cid), pol.person_policy_name(cid),
                       *(pol.eso_policy_name(cid, eso[m]["key"]) for m in eso_present)}
            accessors = self._list(a, "auth/token/accessors")
            for acc in accessors:
                _, info = a._call("POST", "auth/token/lookup-accessor", {"accessor": acc})
                pols = set((self._data(info) or {}).get("policies") or [])
                if pols - allowed:
                    self._untrusted("a token carries the policies " + ", ".join(sorted(pols - allowed)))
            revoked = 0
            for acc in accessors:
                if acc not in mine:
                    a._call("POST", "auth/token/revoke-accessor", {"accessor": acc})
                    revoked += 1
            self.step("review", "running", f"{revoked} token(s) of the snapshot revoked")
            return {"jwt": jwt_present, "groups": groups_present, "members": group_members,
                    "eso": {eso[m]["key"] for m in eso_present}}
        finally:
            a.revoke_self()

    def _review_jwt_mount(self, a, m, entry, cid):
        """Montage `jwt` d'une personne restauré : un seul rôle, `person`,
        et la configuration que l'agent a posée (émetteur, client, CA ou
        clés statiques déjà vues pour ce montage)."""
        roles = self._list(a, f"auth/{m}/role")
        if roles != [oj.PERSON_ROLE]:
            self._untrusted(f"roles of {m}: " + ", ".join(roles))
        try:
            _, doc = a._call("GET", f"auth/{m}/role/{oj.PERSON_ROLE}")
            bad = self._jwt_role_diff(self._data(doc) or {}, entry.get("client_id"), cid)
            _, doc = a._call("GET", f"auth/{m}/config")
            conf = self._data(doc) or {}
            # revue 6-7 I-1 : découverte seulement en B1, avec l'émetteur et la
            # CA épinglés (clés statiques refusées)
            bad += self._jwt_config_diff(conf, entry.get("issuer"), entry.get("ca_pem"), [], False)
        except ValueError:
            bad = ["unreadable"]
        if bad:
            self._untrusted(f"jwt auth method {m} ({', '.join(bad)})")

    def _review_groups(self, a, cid, groups):
        """Groupes internes `hops-<id>-*` de l'état, chacun avec sa seule
        politique et (revue 6-7 I-2) des membres pris parmi ceux de l'état :
        un membre ajouté hors de la console (entité AppRole de la console,
        jeton d'OpenTofu, inconnu) recevrait les droits du groupe. Un
        instantané plus ancien peut en avoir moins. Rend ({nom}, {nom:
        membres restaurés})."""
        present, members = set(), {}
        for gid in self._list(a, "identity/group/id"):
            try:
                _, doc = a._call("GET", f"identity/group/id/{gid}")
            except oc.OpenBaoError:
                self._untrusted(f"identity group {gid}")
            d = self._data(doc) or {}
            name = str(d.get("name") or "")
            mine = next((n for n in groups if pol.group_name(cid, n) == name), None)
            if mine is None or d.get("type") != "internal" or (d.get("alias") or {}).get("id") \
                    or list(d.get("policies") or []) != [pol.group_policy_name(cid, mine)] \
                    or d.get("parent_group_ids") or d.get("member_group_ids"):
                self._untrusted(f"identity group {sf.clean_line(name or gid, 80)}")
            got = [str(e) for e in d.get("member_entity_ids") or []]
            if set(got) - set(groups[mine].get("members") or []):
                self._untrusted(f"members of the identity group {sf.clean_line(name, 80)}")
            present.add(mine)
            members[mine] = sorted(got)
        return present, members

    def _review_eso_mount(self, a, m, entry, cid):
        """Montage `jwt` d'un cluster enfant restauré : un seul rôle, `eso`,
        aux namespaces de l'état, et les clés que l'agent a posées."""
        roles = self._list(a, f"auth/{m}/role")
        if roles != [oj.ESO_ROLE]:
            self._untrusted(f"roles of {m}: " + ", ".join(roles))
        try:
            want_role = oj.eso_role_params(cid, entry.get("key"), entry.get("namespaces"), entry.get("node_cidr"),
                                           entry.get("sa_uids"))
            want_conf = oj.eso_mount_config(entry.get("issuer"), entry.get("pubkeys"))
            _, doc = a._call("GET", f"auth/{m}/role/{oj.ESO_ROLE}")
            bad = self._eso_role_diff(self._data(doc) or {}, want_role)
            _, doc = a._call("GET", f"auth/{m}/config")
            bad += self._eso_config_diff(self._data(doc) or {}, want_conf)
        except ValueError:
            bad = ["unreadable"]
        if bad:
            self._untrusted(f"ESO jwt auth method {m} ({', '.join(bad)})")

    def _review_entities(self, a, roles, token_acc, jwt_accs=frozenset(), members=frozenset(),
                         eso_accs=frozenset()):
        """Les politiques d'une entité valent pour chaque jeton de l'entité,
        y compris ceux émis après la révocation (prochain login AppRole de la
        console, jeton enfant `auth/token/create/<rôle>` avec un alias
        `tf-*`). Les entités attendues n'en portent aucune : la console,
        l'agent et les jetons d'OpenTofu tiennent leurs droits de leur rôle.
        Revue 6-7 I-2 : une entité qui a un alias jwt (une personne) n'a QUE
        des alias jwt, et tout membre d'un groupe est une telle entité (sinon
        un jeton d'OpenTofu ou la console recevrait les droits du groupe)."""
        _, auth_doc = a._call("GET", "sys/auth")
        approle_acc = ((self._data(auth_doc) or {}).get("approle/") or {}).get("accessor")
        role_ids = set()
        for role in roles:
            _, doc = a._call("GET", f"auth/approle/role/{role}/role-id")
            role_ids.add(str((self._data(doc) or {}).get("role_id") or ""))
        persons = set()
        for eid in self._list(a, "identity/entity/id"):
            _, doc = a._call("GET", f"identity/entity/id/{eid}")
            d = self._data(doc) or {}
            if d.get("policies"):
                self._untrusted(f"identity entity {eid} carries policies")
            aliases = d.get("aliases") or []
            on_jwt = [al for al in aliases if (al or {}).get("mount_accessor") in jwt_accs]
            if on_jwt and len(on_jwt) != len(aliases):
                self._untrusted(f"identity entity {eid} mixes person and service aliases")
            if on_jwt:
                persons.add(eid)
                continue                # une personne (alias = `sub`) ; ses droits viennent du rôle et des groupes
            on_eso = [al for al in aliases if (al or {}).get("mount_accessor") in eso_accs]
            if on_eso:
                if len(on_eso) != len(aliases):
                    self._untrusted(f"identity entity {eid} mixes ESO and other aliases")
                continue                # compte `hops-eso` d'un namespace : droits du seul rôle `eso`
            for al in aliases:
                acc, name = (al or {}).get("mount_accessor"), str((al or {}).get("name") or "")
                if acc and acc == approle_acc and name in role_ids:
                    continue
                if acc and acc == token_acc and name.startswith("tf-") and pol.DECL_RE.match(name[3:]):
                    continue
                self._untrusted(f"identity entity {eid} alias on an unexpected mount or name")
        if set(members) - persons:
            self._untrusted("a group member is not a person entity")

    def op_restore_snapshot(self, req):
        """Essai 6 : instance neuve, même clé statique (ou l'ancienne en
        previous), restauration simple par le jeton d'amorce de l'instance
        neuve ; ancien volume gardé 7 jours."""
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        witness = req.get("witness")
        if witness is not None and not WITNESS_RE.match(str(witness)):
            raise AgentError("bad-request", "bad witness path")
        snap, named, when = self._snapshot_file(req.get("file"))
        if named and named not in {k["id"] for k in st["keys"]}:
            raise AgentError("snapshot-key-unknown", f"the key {named} of this snapshot is no longer kept")
        key = self._key_for(st, when, named)
        keys = self.p.b1_etc / "keys"
        if not (keys / f"{key}.key").exists():
            raise AgentError("snapshot-key-unknown", f"the key {key} of this snapshot is no longer kept")
        prev = key if key != st["current_key"] else None
        tpl = self._config_template(st)
        # revue 8-10 I-4 : rendus essayés avant tout arrêt (un échec après
        # l'arrêt laissait B1 arrêté, le retour arrière rendant à son tour)
        self._trial_render(st, tpl, prev=prev, init=True)
        self._trial_render(st, tpl)
        uid = self.host.uid_of(ACCOUNT)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.now()))
        old_vol = self.p.b1_data.with_name(f"{self.p.b1_data.name}.old-{stamp}")
        self.step("stop", "running", "console and OpenBao stopped")
        self.host.run(["systemctl", "stop", CONSOLE_UNIT], check=False)
        self.undo.append(("console started", lambda: self.host.run(["systemctl", "start", CONSOLE_UNIT],
                                                                   check=False)))
        self.host.run(["systemctl", "stop", UNIT])
        self.step("stop", "done")
        self.step("volume", "running", "new volume, the current one kept aside")
        self.p.b1_data.rename(old_vol)

        def back():
            self.host.run(["systemctl", "stop", UNIT], check=False)
            shutil.rmtree(self.p.b1_data, ignore_errors=True)
            old_vol.rename(self.p.b1_data)
            self.podman_secret(SECRET_PREV)
            self.render(st, tpl)
            self.own(self.p.b1_etc / "openbao.hcl", uid)
            self.unit_env(uid, f"{IMAGE}:current")
            self.clear_boot()
            self.host.run(["systemctl", "start", UNIT], check=False)
        self.undo.append(("previous volume back", back))
        for d in (self.p.b1_data, self.p.b1_data / "data", self.p.b1_data / "audit"):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.own(d, uid)
        (self.p.b1_data / "snapshots").mkdir(mode=0o700, exist_ok=True)
        for s in (old_vol / "snapshots").glob("*.snap"):
            shutil.copy2(s, self.p.b1_data / "snapshots" / s.name)
        self.step("volume", "done")
        self.step("fresh", "running", "fresh instance with the key of the snapshot")
        boot = self.write_boot(uid)
        if prev:
            self.podman_secret(SECRET_PREV, keys / f"{prev}.key")
        self.render(st, tpl, prev=prev, init=True)
        self.own(self.p.b1_etc / "openbao.hcl", uid)
        self.unit_env(uid, f"{IMAGE}:current", prev=bool(prev))
        self.host.run(["systemctl", "start", UNIT])
        self.wait_unsealed(self.b1_ca(), "fresh OpenBao")
        self.step("fresh", "done")
        self.step("restore", "running")
        b = self.client(self.b1_addr, self.b1_ca())
        b.set_token(boot["boot-token"])
        b.raft_restore(str(snap), force=False)       # jamais snapshot-force (essai 6)
        self.clear_boot()
        self.wait_unsealed(self.b1_ca(), "restored OpenBao")
        self.step("restore", "done")
        if prev:
            self.step("rotate-root", "running", "the snapshot key is not the current one")
            a = self.agent_client()
            a._call("POST", "sys/rotate/root", {})
            a.revoke_self()
            self.render(st, tpl)
            self.own(self.p.b1_etc / "openbao.hcl", uid)
            self.unit_env(uid, f"{IMAGE}:current")
            self.podman_secret(SECRET_PREV)
            self.restart()
            self.wait_unsealed(self.b1_ca(), "OpenBao with the current key only")
            self.step("rotate-root", "done")
        else:
            self.render(st, tpl)
            self.own(self.p.b1_etc / "openbao.hcl", uid)
        self.step("witness", "running", "console identity and a witness secret")
        c = self._console_identity_back(st)
        self.step("review", "running", "tokens and console rights of the restored snapshot")
        try:
            present = self.review_restored(st, c)
        except BaseException:
            c.revoke_self()
            raise
        self.step("review", "done", "configuration as expected, tokens of the snapshot revoked")
        self._prune_person_state(present or {})
        # revue 6-7 M-1 : la politique commune (« lecture seulement par
        # groupe » compris) et celles des groupes reprennent le texte de
        # l'état de l'agent ; la revue en acceptait deux
        st_now = self.agent_state()
        # revue 8-10 I-5 : un instantané d'avant la 2.10.0 rend le rôle des
        # jetons d'OpenTofu sans liaison à 127.0.0.1/32 : reposé
        self._with_agent(lambda a: a._call("POST", f"auth/token/roles/{pol.tf_role(st_now['console_id'])}",
                                           pol.tf_token_role_params(st_now["console_id"], [pol.B1_CIDR])))
        if st_now.get("person_jwt") or st_now.get("person_groups") or st_now.get("eso_clusters"):
            done = self._with_agent(lambda a: self._person_policies_refresh(a, st_now))
            if done:
                self.step("person-policies", "done", ", ".join(done))
        if witness:
            c.kv_metadata(st["mount"], f"consoles/{st['console_id']}/{witness}")
        else:
            c.kv_list(st["mount"], f"consoles/{st['console_id']}")
        c.revoke_self()
        self.step("witness", "done")
        self.undo = [u for u in self.undo if u[0] == "console started"]
        self.rollback()                              # relance la console
        shutil.rmtree(self.p.work, ignore_errors=True)
        return f"snapshot {req['file']} restored; previous volume kept {OLD_VOLUME_DAYS} days"

    def _prune_person_state(self, present):
        """2.10.0 : un instantané plus ancien qu'un montage `jwt` ou qu'un
        groupe ne les porte pas : l'état de l'agent suit ce qui est restauré
        (la console les reconfigure par `person-token`)."""
        st = self.agent_state()
        pj = {k: v for k, v in (st.get("person_jwt") or {}).items()
              if isinstance(v, dict) and v.get("mount") in present.get("jwt", set())}
        pg = {k: (dict(v, members=present["members"][k]) if k in (present.get("members") or {}) else v)
              for k, v in (st.get("person_groups") or {}).items() if k in present.get("groups", set())}
        ec = {k: v for k, v in (st.get("eso_clusters") or {}).items() if k in present.get("eso", set())}
        if pj != (st.get("person_jwt") or {}) or pg != (st.get("person_groups") or {}) \
                or ec != (st.get("eso_clusters") or {}):
            gone = sorted(set(st.get("person_jwt") or {}) - set(pj)) + sorted(set(st.get("person_groups") or {}) - set(pg)) \
                + sorted(f"eso {k}" for k in set(st.get("eso_clusters") or {}) - set(ec))
            self.save_agent_state(dict(st, person_jwt=pj, person_groups=pg, eso_clusters=ec))
            self.step("person-state", "done", "absent from the snapshot: " + ", ".join(gone))

    def _console_identity_back(self, st):
        """Après une restauration, la console doit se connecter avec son
        `secret_id` courant. Un instantané pris depuis la dernière rotation
        le contient déjà (le cas courant) : `custom-secret-id` le refuserait
        en 500 « SecretID is already registered » (OpenBao 2.7.2, revue I3
        des tâches 5-7). Connexion d'abord ; le `secret_id` n'est reposé que
        si elle échoue ; l'accesseur est relu dans les deux cas. Rend le
        client connecté de la console."""
        role = pol.console_role(st["console_id"])
        sid = _read(self.p.etc / "secret-id")
        try:
            c = self.console_client(addr=self.b1_addr)
        except oc.OpenBaoError:
            a = self.agent_client()
            try:
                a._call("POST", f"auth/approle/role/{role}/custom-secret-id", {"secret_id": sid})
            except oc.OpenBaoError as e:
                if "already registered" not in str(e.detail or "").lower():
                    raise
            finally:
                a.revoke_self()
            c = self.console_client(addr=self.b1_addr)
        a = self.agent_client()
        try:
            _, lk = a._call("POST", f"auth/approle/role/{role}/secret-id/lookup", {"secret_id": sid})
        finally:
            a.revoke_self()
        acc = ((lk or {}).get("data") or {}).get("secret_id_accessor")
        if acc:
            _w(self.p.etc / "secret-id-accessor", acc + "\n", 0o600)
        return c

    def agent_snapshot(self, st, why):
        """Instantané pris par l'agent dans son propre répertoire (root), la
        clé en service dans le nom : `openbao-<date>-<clé>-<why>.snap`."""
        snaps = self.p.b1_data / "snapshots"
        snaps.mkdir(mode=0o700, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.now()))
        name = f"openbao-{stamp}-{st['current_key']}-{why}.snap"
        a = self.agent_client()
        try:
            a.raft_snapshot(str(snaps / name))
        finally:
            a.revoke_self()
        return name

    def prune_agent_snapshots(self):
        d = self.p.b1_data / "snapshots"
        snaps = []
        for f in d.glob("openbao-*.snap"):
            try:
                lst = f.lstat()
            except OSError:
                continue
            if stat.S_ISREG(lst.st_mode):
                snaps.append((f.name, lst.st_mtime))
        for name in _agent_snapshot_prune(snaps, self.now()):
            (d / name).unlink(missing_ok=True)

    def health(self):
        """État lisible par la console, sans valeur (`health.json` de
        <état>/secrets-agent, root 0644, groupe de la console) : clés et leurs
        dates, instantanés de l'agent, âge du secret_id, place du volume
        d'audit (OpenBao refuse toute requête qu'il ne peut pas journaliser).
        Écrit après chaque passage de l'agent."""
        st = self.agent_state()
        sid = self.p.etc / "secret-id"
        out = {"at": int(self.now()), "agent_version": _version(), "b1_installed": bool(st),
               "secret_id_age_s": int(self.now() - sid.stat().st_mtime) if sid.exists() else None}
        if st:
            out.update(current_key=st.get("current_key"), version=st.get("version"),
                       keys=[{k: e.get(k) for k in ("id", "created", "retired")} for e in st.get("keys", [])],
                       offline_copy={k: (st.get("offline_copy") or {}).get(k)
                                     for k in ("key_id", "exported_at", "downloaded_at", "acked_at")})
            snaps = []
            for f in sorted((self.p.b1_data / "snapshots").glob("openbao-*.snap")):
                try:
                    lst = f.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(lst.st_mode) and SNAP_RE.match(f.name):
                    snaps.append({"file": f.name, "bytes": lst.st_size, "at": int(lst.st_mtime)})
            out["snapshots"] = snaps[-50:]
            audit = self.p.b1_data / "audit"
            try:
                v = os.statvfs(audit if audit.exists() else self.p.b1_data)
                out["audit"] = {"free": v.f_bavail * v.f_frsize, "total": v.f_blocks * v.f_frsize}
            except OSError:
                out["audit"] = None
        return out

    def save_health(self, d=None):
        try:
            data = json.dumps(self.health(), indent=2).encode()
            if d is not None:
                d.write(HEALTH, data, 0o644, self._console_group())
                return
            with sf.SafeDir.open_base(self.p.console_state) as base, \
                    base.subdir(self.p.state.name) as sd:
                sd.write(HEALTH, data, 0o644, self._console_group())
        except OSError as e:
            print(f"health not written: {e}", file=sys.stderr, flush=True)

    # -- ménage (minuterie, sans demande) -----------------------------------------
    def housekeeping(self):
        out = 0
        settings = self.console_settings()
        sid = self.p.etc / "secret-id"
        if settings.get("mode") in ("a", "b1") and sid.exists() and \
                self.now() - sid.stat().st_mtime >= SECRET_ID_MAX_AGE:
            self.status = {"state": "running", "op": "rotate-secret-id", "request_id": None,
                           "requested_by": "timer", "started": self.now(), "steps": []}
            self.undo = []
            try:
                self.finish("done", self.op_rotate_secret_id())
            except (AgentError, oc.OpenBaoError) as e:
                self.finish("failed", str(e), getattr(e, "code", None))
                out = 1
        for old in self.p.b1_data.parent.glob(f"{self.p.b1_data.name}.old-*"):
            # relecture finale I4 : l'âge vient de l'horodatage du nom (posé
            # à la restauration) ; un renommage ne change pas le mtime du
            # répertoire, qui datait de la pose. Nom illisible : gardé.
            m = OLD_VOLUME_RE.search(old.name)
            try:
                at = calendar.timegm(time.strptime(m.group(1), "%Y%m%d-%H%M%S")) if m else None
            except ValueError:
                at = None
            if at is not None and self.now() - at > OLD_VOLUME_DAYS * 86400:
                shutil.rmtree(old, ignore_errors=True)
        self.prune_agent_snapshots()
        st = self.agent_state()
        if st:
            keep = []
            for k in st.get("keys", []):
                if k.get("retired") and self.now() - k["retired"] > KEY_KEEP_DAYS * 86400:
                    (self.p.b1_etc / "keys" / f"{k['id']}.key").unlink(missing_ok=True)
                else:
                    keep.append(k)
            if keep != st.get("keys"):
                self.save_agent_state(dict(st, keys=keep))
        return out


def _networks(values):
    """Ensemble de réseaux d'une liste d'adresses ou de plages ; une entrée
    illisible reste telle quelle (donc différente de toute plage attendue)."""
    out = set()
    for v in values or []:
        try:
            out.add(ipaddress.ip_network(str(v), strict=False))
        except ValueError:
            out.add(str(v))
    return out


def _agent_snapshot_prune(snaps, now, days=AGENT_SNAP_KEEP_DAYS):
    """Instantanés de l'agent à effacer : plus vieux que `days`, sauf le plus
    récent de chaque motif (le dernier `pre-upgrade` reste le chemin de
    retour d'une mise à jour, décision 277 A). `snaps` : [(nom, date)]."""
    newest = {}
    for name, at in snaps:
        m = SNAP_KEY_RE.search(name)
        why = (m.group(2) or "") if m else ""
        if why not in newest or at > newest[why][1]:
            newest[why] = (name, at)
    keep = {v[0] for v in newest.values()}
    return [n for n, at in snaps if n not in keep and now - at > days * 86400]


def _version():
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return None


def main(argv):
    if "--status" in argv:
        # lu par descripteur, sans suivre de lien (fichier de l'état de la console)
        p = Paths()
        try:
            with sf.SafeDir.open_base(p.console_state) as base, base.subdir(p.state.name) as d:
                raw = d.read(STATUS)
            st = json.loads(raw) if raw else {}
        except (OSError, ValueError):
            st = {}
        print(json.dumps(st if isinstance(st, dict) else {}, indent=2))
        return 0
    os.umask(0o077)
    return Agent().process()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
