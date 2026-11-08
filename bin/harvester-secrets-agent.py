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
    status             état sans aucune valeur

Aucune valeur secrète dans status.json, les journaux ni les messages : le
jeton d'enveloppe de `store-secret-id` est la seule valeur qui transite par
l'état (request.json 0600, effacé AVANT traitement, usage unique).

Usage : harvester-secrets-agent            (traite la demande, sinon ménage)
        harvester-secrets-agent --status   (dernier état)
"""

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
import openbao_policy as pol  # noqa: E402
import safe_fs as sf  # noqa: E402

VERSION_FILE = Path(os.environ.get("HARVESTER_OPS_OPT", "/opt/harvester-ops")) / "VERSION"
OPS = ("store-secret-id", "b1-install", "b1-upgrade", "b1-remove", "rotate-secret-id", "rotate-unseal-key",
       "restore-snapshot", "b1-policies", "export-unseal-key", "export-unseal-key-done", "ack-offline-copy",
       "status")
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
WITNESS_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+){0,6}$")
UNIT = "harvester-ops-openbao.service"
CONSOLE_UNIT = "harvester-ops.service"
CONTAINER = "harvester-ops-openbao"
ACCOUNT = "harvester-ops-openbao"
IMAGE = "localhost/harvester-ops-openbao"
SECRET, SECRET_PREV = "hops-openbao-unseal", "hops-openbao-unseal-prev"
SECRET_ID_MAX_AGE = 30 * 86400          # point 260
OLD_VOLUME_DAYS = 7
KEY_KEEP_DAYS = 14                      # comme les instantanés (point 259)
WAIT_S = int(os.environ.get("HARVESTER_SECRETS_AGENT_WAIT", "120"))
# Décision 275 : copie hors ligne de la clé de descellement, remise une fois
# à l'administrateur (fichier root:harvester-ops 0440 dans le répertoire
# root des identifiants de la console, monté en lecture seule dans le
# conteneur), effacé après le téléchargement ou au bout de 10 minutes.
EXPORT_NAME = "unseal-export.key"
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
        self.logrotate = g("HARVESTER_OPS_LOGROTATE_DIR", "/etc/logrotate.d") / "harvester-ops-openbao"
        self.work = g("HARVESTER_SECRETS_AGENT_WORK", "/var/lib/harvester-secrets-agent")
        self.logs = g("HARVESTER_OPS_LOG_DIR", "/var/log/harvester-ops")


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

    def process(self, request=None):
        """Traite la demande déposée (ou `request`, passée en processus par
        `sudo harvester-secrets`), sinon le ménage de la minuterie."""
        try:
            self._expire_export()
        except OSError:
            pass
        try:
            self.open_state()
        except OSError as e:
            print(f"harvester-secrets-agent: {e}; nothing done", file=sys.stderr, flush=True)
            return 1
        try:
            return self._process(request)
        finally:
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
        if addr == self.b1_addr or (addr is None and settings.get("mode") != "a"):
            addr, ca = self.b1_addr, self.b1_ca()
        else:
            addr, ca = addr or settings.get("addr"), self.ca_pem()
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
                            mount=st.get("mount", pol.DEFAULT_MOUNT))
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
        return ob.verify(dst, self.p.work)

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
                                             "the agent (b1-remove first to use another OpenBao)")
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
        self.step("bundle", "done", f"OpenBao {info['version']} ({info['tag']})")
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

    def op_b1_policies(self, req=None):
        """Décision 276 : remet les politiques `hops-*` de cette console au
        texte de la version installée (mise à niveau après la pose, plus de
        jeton racine en B1) : celle de l'agent et celle des jetons enfants
        d'OpenTofu. La politique de la console n'est jamais touchée."""
        st = self.agent_state()
        if not st:
            raise AgentError("b1-not-installed", "OpenBao B1 is not installed")
        cid = st["console_id"]
        self.step("policies", "running")
        a = self.agent_client()
        try:
            _, auths = a._call("GET", "sys/auth")
            auths = (auths or {}).get("data") or auths or {}
            acc = (auths.get("token/") or {}).get("accessor")
            a._call("POST", f"sys/policies/acl/{pol.tf_role(cid)}", {"policy": pol.tf_child_policy(cid, acc)})
            a._call("POST", f"sys/policies/acl/{pol.agent_role(cid)}",
                    {"policy": pol.agent_policy(cid, st.get("mount") or pol.DEFAULT_MOUNT)})
        finally:
            a.revoke_self()
        self.step("policies", "done", f"{pol.agent_role(cid)}, {pol.tf_role(cid)}")
        return "policies of the agent and of the OpenTofu tokens updated"

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
        tpl = (self.p.b1_etc / "openbao.hcl.tpl").read_text()
        uid = self.host.uid_of(ACCOUNT)
        old = st["current_key"]
        keys = self.p.b1_etc / "keys"
        new = self.new_key_id({k["id"] for k in st["keys"]})
        self.step("new-key", "running")
        _w(keys / f"{new}.key", secrets.token_bytes(32), 0o400, binary=True)
        self.podman_secret(SECRET_PREV, keys / f"{old}.key")
        self.podman_secret(SECRET, keys / f"{new}.key")
        st2 = dict(st, current_key=new)
        self.render(st2, tpl, prev=old)
        self.own(self.p.b1_etc / "openbao.hcl", uid)
        self.unit_env(uid, f"{IMAGE}:current", prev=True)

        def back():
            self.podman_secret(SECRET, keys / f"{old}.key")
            self.podman_secret(SECRET_PREV)
            self.render(st, tpl)
            self.own(self.p.b1_etc / "openbao.hcl", uid)
            self.unit_env(uid, f"{IMAGE}:current")
            (keys / f"{new}.key").unlink(missing_ok=True)
            self.restart()
        self.undo.append(("previous unseal key restored", back))
        self.step("new-key", "done", f"{new} (previous {old})")
        self.step("restart", "running", "both keys")
        self.restart()
        self.wait_unsealed(self.b1_ca())
        self.step("restart", "done")
        self.step("rotate-root", "running", "sys/rotate/root with the agent identity")
        a = self.agent_client()
        a._call("POST", "sys/rotate/root", {})
        self.step("rotate-root", "done")
        self.step("snapshot", "running")
        snaps = self.p.b1_data / "snapshots"
        snaps.mkdir(mode=0o700, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.now()))
        n = a.raft_snapshot(str(snaps / f"openbao-{stamp}-{new}.snap"))
        a.revoke_self()
        self.step("snapshot", "done", f"{n} bytes")
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

    def _snapshot_file(self, name):
        if not SNAP_RE.match(str(name or "")):
            raise AgentError("snapshot-bad-name", "bad snapshot file name")
        dirs = [self.p.b1_data / "snapshots"]
        sd = self.console_settings().get("snapshot_dir")
        if isinstance(sd, str) and os.path.isabs(sd):
            dirs.append(Path(sd))
        for d in dirs:
            p = d / name
            try:
                fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                continue
            with os.fdopen(fd, "rb") as f:
                self.p.work.mkdir(mode=0o700, parents=True, exist_ok=True)
                dst = self.p.work / name
                with open(dst, "wb") as out:
                    shutil.copyfileobj(f, out)
            if dst.stat().st_size == 0:
                raise AgentError("snapshot-empty", "empty snapshot file")
            return dst, os.stat(p).st_mtime
        raise AgentError("snapshot-missing", "snapshot not found in the snapshot directories")

    def _key_for(self, st, when):
        """La clé active quand l'instantané a été pris (historique des clés)."""
        best = None
        for k in sorted(st["keys"], key=lambda k: k["created"]):
            if k["created"] <= when + 60 and (k["retired"] is None or k["retired"] >= when - 60):
                best = k["id"]
        return best or st["current_key"]

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
        snap, when = self._snapshot_file(req.get("file"))
        key = self._key_for(st, when)
        keys = self.p.b1_etc / "keys"
        if not (keys / f"{key}.key").exists():
            raise AgentError("snapshot-key-unknown", f"the key {key} of this snapshot is no longer kept")
        prev = key if key != st["current_key"] else None
        tpl = (self.p.b1_etc / "openbao.hcl.tpl").read_text()
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
            if self.now() - old.stat().st_mtime > OLD_VOLUME_DAYS * 86400:
                shutil.rmtree(old, ignore_errors=True)
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
