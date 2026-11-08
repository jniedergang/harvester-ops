#!/usr/bin/env python3
"""Magasin de secrets de la console : OpenBao (2.9.0, tâche 7).

    harvester-secrets status [--json]
    harvester-secrets assistant --addr URL --console-cidr CIDR [--mount M] [--prefix P]
    harvester-secrets check --mode a|b1 [--addr URL --ca FILE --mount M --prefix P]
                            [--wrapped-stdin] [--transit] [--dry-run]
    harvester-secrets enable --mode a --addr URL --role-id ID --wrapped-stdin [--ca FILE]
                             [--mount M] [--prefix P] [--dry-run]
    harvester-secrets enable --mode b1 --bundle FILE [--dry-run]
    harvester-secrets disable [--remove-b1] [--leave-plan-secrets] [--dry-run]
    harvester-secrets migrate --to openbao|local [--class C ...] [--name N] [--dry-run]
    harvester-secrets confirm --class C [--older-than-days N]
    harvester-secrets outage-test
    harvester-secrets versions REF
    harvester-secrets rollback REF --version N [--dry-run]
    harvester-secrets rotate secret-id | unseal-key | transit DECL | ssh-kvm HOST
    harvester-secrets offline-copy export [--to FILE] | done | ack --key-id ID
    harvester-secrets snapshot [--dest DIR] [--keep N] [--dry-run]
    harvester-secrets snapshots
    harvester-secrets settings [--set KEY=VALUE ...]
    harvester-secrets sweep-orphans [--class C ...] [--min-age-days N] [--dry-run]
    harvester-secrets restore FILE [--dry-run]
    harvester-secrets bundle --registry R --user U --password-stdin [--repository P] [--tag T]
                             [--cosign-key FILE] -o DIR|FILE
    harvester-secrets file REF
    harvester-secrets tf-encryption status | ack-vault
    harvester-secrets tf-entity-remove DECL [--done] [--trial]
    harvester-secrets tf-keys list | delete DECL --confirm DECL | sweep

Options communes : `--id` (action de la console, en-tête X-Hops-Action),
`--actor` (personne, en-tête X-Hops-Actor).

Ce qui demande root (dépôt du `secret_id`, pose de B1, rotations, restauration)
passe par l'agent de l'hôte (`harvester-secrets-agent`) : une demande dans
<état>/secrets-agent/request.json, suivie jusqu'à son état. Lancé en root sur
l'hôte, l'outil joue l'agent lui-même (même code), ce qui permet
`sudo harvester-secrets enable ...` sans agent installé.

Jamais de secret en argument : le jeton d'enveloppe et le mot de passe du
compte de service de l'Application Collection ne se lisent que sur l'entrée
standard ; aucune valeur dans les étapes, la sortie ni les erreurs.

Progression sur stderr au format STEP_EVENT|<étape>|<statut>|<message> ;
résultat en JSON sur stdout. Codes : 0 succès, 1 échec, 2 refus.

Plan : docs/design/2026-11-08-openbao-290-plan.md (tâches 7, 8, 11).
"""

import argparse
import calendar
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import openbao_bundle as ob  # noqa: E402
import openbao_client as oc  # noqa: E402
import openbao_policy as pol  # noqa: E402
import secret_classes as sc  # noqa: E402
import secret_store as ss  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2
AGENT_TIMEOUT = {"b1-install": 1800, "b1-upgrade": 1800, "restore-snapshot": 1800}
SNAP_RE = re.compile(r"^openbao-[0-9A-Za-z_.-]{1,80}\.snap$")
COSIGN_KEY = HERE.parent / "config" / "openbao" / "application-collection-cosign.pub"
ARGS = argparse.Namespace(id=None, actor=None)
SPEC = {}                     # --spec : fichier 0600 de la console (jeton d'enveloppe, CA), effacé par elle
SERVICE_ACCOUNT = "harvester-ops"
# 2.9.0, revue de la seconde passe (I1) : lancé en root, l'outil ne fait
# lui-même que l'appel en mémoire à l'agent ; tout le reste tourne dans un
# fils passé au compte de la console AVANT de lire son état. `bundle` ne lit
# ni n'écrit l'état de la console (il reste en root) ; `snapshot` écrit dans
# un répertoire de l'administrateur avec l'identité de la console : refusé
# en root (le lancer avec `sudo -u harvester-ops`).
ROOT_DIRECT = ("bundle",)
ROOT_REFUSED = ("snapshot",)
AGENT_CHANNEL = None          # fils : envoie une demande au parent root, rend son état
INPUT_FDS = {}                # chemin donné par l'administrateur -> fd ouvert par root
OUTPUT_FDS = {}
REQUEST_LINE_MAX = 64 << 10
ROTATE_RUNNER = None                          # ssh de la rotation d'une clé KVM (tests)
OUTAGE_ADDR = "https://127.0.0.1:9"           # panne simulée : port sans serveur (discard)


def cosign_key():
    """Clé publique de l'Application Collection : celle qu'install.sh pose
    dans /opt/harvester-ops/openbao (le script tourne depuis /usr/local/bin,
    où `HERE.parent/config` n'existe pas), sinon celle du dépôt."""
    opt = Path(os.environ.get("HARVESTER_OPS_OPT") or "/opt/harvester-ops") / "openbao" / ob.COSIGN_KEY_NAME
    return opt if opt.is_file() else COSIGN_KEY


def step(sid, status, msg=""):
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def out(obj):
    print(json.dumps(obj, indent=2))


def refuse(code, message, **extra):
    step("refused", "error", f"{code}: {message}")
    out({"ok": False, "code": code, "error": message, **extra})
    return EXIT_REFUSED


def fail(code, message, **extra):
    step("failed", "error", f"{code}: {message}")
    out({"ok": False, "code": code, "error": message, **extra})
    return EXIT_FAIL


# ---------------------------------------------------------------------------
# Réglages (<état>/secrets-settings.json, sans secret)
# ---------------------------------------------------------------------------
def state():
    return ss._state_dir()


def raw_settings():
    try:
        doc = json.loads((state() / ss.SETTINGS_FILE).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(doc):
    """Écrit les réglages sous un nom neuf puis les renomme, droits posés sur
    le descripteur (jamais un chmod par chemin qu'un lien posé entre-temps
    pourrait détourner)."""
    import safe_fs as sf
    p = state()
    p.mkdir(parents=True, exist_ok=True)
    with sf.SafeDir.open_base(p) as d:
        d.write(ss.SETTINGS_FILE, json.dumps(doc, indent=2).encode(), 0o600)


def ensure_console_id(doc):
    """Identifiant de la console : tiré à la première activation, gardé,
    jamais réutilisé par une autre console."""
    if not pol.ID_RE.match(str(doc.get("console_id") or "")):
        doc["console_id"] = secrets.token_hex(6)
        save_settings(doc)
    return doc["console_id"]


def settings():
    return ss.load_settings(state())


# ---------------------------------------------------------------------------
# Connexion d'administration (l'outil se connecte lui-même : il EST l'outil
# d'administration, même lancé par la console ; les lectures ordinaires des
# autres outils passent par les copies remises, revue I6)
# ---------------------------------------------------------------------------
def creds_present():
    d = ss.openbao_dir()
    return (d / "role-id").is_file() and (d / "secret-id").is_file()


def ca_of(path=None):
    p = Path(path) if path else ss.openbao_dir() / "ca.pem"
    try:
        return p.read_text() if p.is_file() else None
    except OSError:
        raise oc.OpenBaoError("openbao-tls", None, "CA file unreadable") from None


def admin_client(addr=None, ca=None, s=None):
    s = s or settings()
    c = oc.Client(addr or s["addr"], ca_pem=ca if ca is not None else ca_of(s.get("ca_file")),
                  timeout=oc.clamp_timeout(s.get("timeout")), actor=ARGS.actor or "service", action=ARGS.id)
    d = ss.openbao_dir()
    try:
        role_id, secret_id = (d / "role-id").read_text().strip(), (d / "secret-id").read_text().strip()
    except OSError:
        raise oc.OpenBaoError("openbao-login-failed", None, "AppRole credentials not deposited") from None
    c.login_approle(role_id, secret_id)
    return c


def admin_store(s=None):
    s = s or settings()
    if not ss.openbao_active(s):
        raise ss.SecretError("openbao-inactive", detail="OpenBao is not active (harvester-secrets enable)")
    base = admin_client(s=s)
    return ss.OpenBaoStore(s, lambda actor=None, action=None: base.with_headers(action or ARGS.id,
                                                                                actor or ARGS.actor),
                           files_dir=lambda: ss.secret_files_dir(s))


# ---------------------------------------------------------------------------
# Agent de l'hôte
# ---------------------------------------------------------------------------
def _agent_module():
    spec = importlib.util.spec_from_file_location("harvester_secrets_agent", HERE / "harvester-secrets-agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _is_root():
    return os.geteuid() == 0


def run_agent(req, timeout=None):
    """Une opération de l'agent ; rend son état final. Lancé par root, le
    fils sans privilège la passe au parent root, qui la joue en mémoire ;
    sinon l'outil dépose la demande (refus si une autre attend) et suit
    status.json."""
    timeout = timeout or AGENT_TIMEOUT.get(req["op"], 600)
    req = dict(req, id=secrets.token_hex(8), requested_by=ARGS.actor or os.environ.get("USER") or "cli")
    d = state() / "secrets-agent"
    rp = d / "request.json"
    if AGENT_CHANNEL is not None:
        if os.path.lexists(rp):
            raise CliError("secrets-agent-busy", "another request is waiting for the agent", EXIT_REFUSED)
        st = AGENT_CHANNEL(req)
        for s in st.get("steps") or []:
            step(f"agent-{s.get('id')}", s.get("status"), s.get("message") or "")
        if st.get("state") not in ("done", "failed"):
            raise CliError("secrets-agent-failed", "the agent did not run (see its log)", EXIT_FAIL)
        return st
    if not (d / "agent.json").is_file():
        raise CliError("secrets-agent-missing", "no secret store agent on this host: run "
                       "`sudo harvester-secrets ...` on the host, or install the agent (install.sh --upgrade)",
                       EXIT_REFUSED)
    d.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(rp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        raise CliError("secrets-agent-busy", "another request is waiting for the agent", EXIT_REFUSED) from None
    with os.fdopen(fd, "w") as f:
        json.dump(req, f)
    deadline = time.monotonic() + timeout
    shown = {}
    while True:
        try:
            st = json.loads((d / "status.json").read_text())
        except (OSError, ValueError):
            st = {}
        if st.get("request_id") == req["id"]:
            for s in st.get("steps") or []:
                key = (s.get("status"), s.get("message"))
                if shown.get(s["id"]) != key:
                    shown[s["id"]] = key
                    step(f"agent-{s['id']}", s.get("status"), s.get("message") or "")
            if st.get("state") in ("done", "failed"):
                return st
        if time.monotonic() > deadline:
            raise CliError("secrets-agent-timeout", f"no answer from the agent after {timeout} s", EXIT_FAIL)
        _pause(0.5)


def _pause(s):
    time.sleep(s)


# ---------------------------------------------------------------------------
# Lancé en root (revue I1 de la seconde passe)
# ---------------------------------------------------------------------------
def _agent_env(state_dir):
    """Répertoires de l'agent joué en mémoire : ceux de l'outil (revue M11
    des tâches 5-7)."""
    env = dict(os.environ, HARVESTER_OPS_STATE_DIR=str(state_dir))
    if ss.openbao_dir().name == "openbao":
        env["HARVESTER_OPS_ETC"] = str(ss.openbao_dir().parent)
    return env


def _root_agent_call(state_dir):
    """Le parent root : joue l'agent en mémoire avec la demande du fils
    (l'agent la contrôle comme une demande déposée : liste fermée, clés,
    identifiants) et rend son état. Rien d'autre n'est fait en root."""
    def call(req):
        if not isinstance(req, dict):
            return {"state": "failed", "code": "bad-request", "message": "not an object", "steps": []}
        mod = _agent_module()
        agent = mod.Agent(paths=mod.Paths(_agent_env(state_dir)))
        agent.process(request=req)
        return agent.status
    return call


def _drop_privileges():
    """Le fils passe au compte de la console (groupes, puis gid, puis uid,
    les trois ids réels/effectifs/sauvés) : il ne peut plus lire ni écrire
    que ce que la console peut lire ou écrire elle-même."""
    import grp
    import pwd
    try:
        pw = pwd.getpwnam(SERVICE_ACCOUNT)
    except KeyError:
        raise CliError("service-account-missing", f"no {SERVICE_ACCOUNT} account on this host: "
                       "the tool never works on the console state as root", EXIT_REFUSED) from None
    try:
        gid = grp.getgrnam(SERVICE_ACCOUNT).gr_gid
    except KeyError:
        gid = pw.pw_gid
    os.setgroups([gid])
    os.setresgid(gid, gid, gid)
    os.setresuid(pw.pw_uid, pw.pw_uid, pw.pw_uid)
    if os.getresuid() != (pw.pw_uid,) * 3 or os.getresgid() != (gid,) * 3:
        raise CliError("privilege-drop-failed", "could not drop to the console account", EXIT_FAIL)
    os.environ["HOME"] = pw.pw_dir
    os.umask(0o077)


def _pipe_channel(w, r):
    wf, rf = os.fdopen(w, "wb"), os.fdopen(r, "rb")

    def call(req):
        wf.write(json.dumps(req).encode() + b"\n")
        wf.flush()
        line = rf.readline(REQUEST_LINE_MAX * 16)
        if not line:
            raise CliError("secrets-agent-failed", "no answer from the root parent", EXIT_FAIL)
        return json.loads(line)
    return call


def _spawn_unprivileged(fn, handler):
    """fn() dans un fils passé au compte de la console ; ses appels à
    l'agent remontent par un tube au parent root, qui les passe à
    handler(demande) et renvoie l'état. Le demandeur est fixé par le parent
    (utilisateur de sudo). Rend le code de sortie du fils."""
    req_r, req_w = os.pipe()
    resp_r, resp_w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:                                        # pragma: no cover (fils, couvert par son code de sortie)
        code = EXIT_FAIL
        try:
            os.close(req_r)
            os.close(resp_w)
            _drop_privileges()
            global AGENT_CHANNEL
            AGENT_CHANNEL = _pipe_channel(req_w, resp_r)
            code = fn()
        except CliError as e:
            code = (refuse if e.exit_code == EXIT_REFUSED else fail)(e.code, str(e))
        except BaseException:                           # noqa: BLE001
            import traceback
            traceback.print_exc()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code if isinstance(code, int) else EXIT_FAIL)
    os.close(req_w)
    os.close(resp_r)
    who = ARGS.actor or os.environ.get("SUDO_USER") or "root"
    with os.fdopen(req_r, "rb") as rin, os.fdopen(resp_w, "wb") as rout:
        while True:
            line = rin.readline(REQUEST_LINE_MAX)
            if not line:
                break
            try:
                req = json.loads(line)
                if isinstance(req, dict):
                    req["requested_by"] = who
                st = handler(req)
            except Exception as e:                      # noqa: BLE001 : rendu au fils, jamais propagé
                st = {"state": "failed", "code": "secrets-agent-failed",
                      "message": f"{type(e).__name__}", "steps": []}
            try:
                rout.write(json.dumps(st).encode() + b"\n")
                rout.flush()
            except BrokenPipeError:
                break
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def _preopen(args):
    """Fichiers de l'administrateur (chemins tapés après sudo) ouverts par
    root avant de quitter ses droits : le fils les lit ou les écrit par
    descripteur, jamais par chemin."""
    for attr in ("ca", "bundle"):
        path = getattr(args, attr, None)
        if path:
            try:
                INPUT_FDS[path] = os.open(path, os.O_RDONLY | os.O_NOCTTY)
            except OSError:
                pass                    # le fils le dira (il l'ouvre alors sans droit particulier)
    to = getattr(args, "to", None) if args.cmd == "offline-copy" else None
    if to:
        try:
            OUTPUT_FDS[to] = os.open(to, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NOCTTY, 0o600)
        except OSError as e:
            raise CliError("offline-copy-write", f"--to: {e.strerror} (a new file is required)", EXIT_REFUSED) \
                from None


def open_input(path):
    """Fichier d'entrée donné en argument : par le descripteur ouvert par
    root s'il y en a un, sinon par son chemin."""
    fd = INPUT_FDS.get(path)
    if fd is None:
        return open(path, "rb")
    os.lseek(fd, 0, os.SEEK_SET)
    return os.fdopen(os.dup(fd), "rb")


def read_input_text(path):
    with open_input(path) as f:
        return f.read(1 << 20).decode()


def _main_as_root(args):
    if args.cmd in ROOT_DIRECT:
        return COMMANDS[args.cmd](args)
    if args.cmd in ROOT_REFUSED:
        return refuse("root-refused", f"`{args.cmd}` runs as the console account: "
                      f"sudo -u {SERVICE_ACCOUNT} harvester-secrets {args.cmd} ...")
    state_dir = ss._state_dir()         # chemin seulement (variable d'environnement ou défaut), rien n'est lu
    _preopen(args)
    to = OUTPUT_FDS and next(iter(OUTPUT_FDS))
    rc = _spawn_unprivileged(lambda: _dispatch(args), _root_agent_call(state_dir))
    if to and rc != EXIT_OK:
        try:
            if os.fstat(OUTPUT_FDS[to]).st_size == 0:
                os.unlink(to)                           # fichier neuf resté vide : retiré
        except OSError:
            pass
    return rc


class CliError(Exception):
    def __init__(self, code, message, exit_code=EXIT_FAIL):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def agent_ok(st):
    if st.get("state") != "done":
        raise CliError(st.get("code") or "secrets-agent-failed", st.get("message") or "agent failed", EXIT_FAIL)
    return st


# ---------------------------------------------------------------------------
# Vérifications avant activation (plan, « Mode A ») : chacune son résultat
# et sa raison ; rien n'est activé tant qu'une vérification est rouge
# ---------------------------------------------------------------------------
def run_checks(addr, ca, mount, prefix, cid, b1=False, wrap=None, use_creds=True, mutate=True, transit=False):
    res = []

    def put(cid_, ok, reason=""):
        res.append({"id": cid_, "ok": ok, "reason": reason})
        step(f"check-{cid_}", "done" if ok else ("skipped" if ok is None else "error"), reason)
        return ok

    try:
        u = __import__("urllib.parse").parse.urlparse(addr or "")
        loop = u.hostname in ("127.0.0.1", "localhost", "::1")
        if u.scheme not in ("https", "http") or (u.scheme == "http" and not loop) or not u.hostname:
            put("address", False, "https:// address required (http only on the loopback)")
            return res
        put("address", True, addr)
        c = oc.Client(addr, ca_pem=ca, timeout=10, actor=ARGS.actor or "service", action=ARGS.id)
        try:
            st = c.seal_status()
        except oc.OpenBaoError as e:
            put("tls", False, f"{e.code}: {e.detail}"[:200])
            return res
        put("tls", True, "server reached" + (", certificate checked" if u.scheme == "https" else ""))
        if not put("seal", bool(st.get("initialized")) and not st.get("sealed"),
                   "unsealed" if not st.get("sealed") else "sealed or not initialized"):
            return res
        if wrap is not None:
            try:
                _, info = c._request("POST", "sys/wrapping/lookup", {"token": wrap}, token=False, retry=False)
                path = ((info or {}).get("data") or {}).get("creation_path")
                put("wrap", path == pol.secret_id_path(cid),
                    "wrapped secret_id of the console role" if path == pol.secret_id_path(cid)
                    else f"wrapping token from another path: {path}")
            except oc.OpenBaoError as e:
                put("wrap", False, f"{e.code}: already unwrapped, expired or unknown")
        if not use_creds:
            return res
        if not creds_present():
            put("login", False, "AppRole credentials not deposited yet (enable deposits them)")
            return res
        try:
            a = admin_client(addr, ca)
            put("login", True, "AppRole login")
        except oc.OpenBaoError as e:
            put("login", False, f"{e.code}: {e.detail}"[:200])
            return res
        v = a.kv_mount_version(mount)
        put("kv2", v == 2, f"{mount}: KV version {v}" if v else f"{mount}: no such mount")
        need = pol.required_capabilities(mount, prefix, cid, b1)
        if not transit:
            need = {p: c_ for p, c_ in need.items() if not p.startswith(("transit/", "auth/token/create/"))}
        got = a.capabilities(list(need))
        missing = sorted(p for p, caps in need.items() if not set(caps) <= set(got.get(p) or []))
        put("capabilities", not missing, "all paths allowed" if not missing
            else "missing: " + ", ".join(missing)[:200])
        if not mutate:
            put("roundtrip", None, "dry run: no write")
            return res
        probe = f"{prefix}/check/{secrets.token_hex(6)}"
        marker = secrets.token_hex(16)
        try:
            a.kv_put(mount, probe, {"probe": marker}, cas=0)
            ok = a.kv_get(mount, probe)[0].get("probe") == marker
            a.kv_delete_all(mount, probe)
            put("roundtrip", ok, "write, read back, destroy under the console prefix")
        except oc.OpenBaoError as e:
            put("roundtrip", False, f"{e.code}: {e.detail}"[:200])
        if transit:
            key = f"hops-{cid}-check"
            try:
                a.transit_create(key)
                put("transit", a.transit_roundtrip(key), "transit key created, encrypt and decrypt")
            except oc.OpenBaoError as e:
                put("transit", False, f"{e.code}: {e.detail}"[:200])
            try:
                t = a.token_create_child(pol.tf_role(cid), [pol.tf_role(cid)], "60s", entity_alias="tf-check")
                tc = oc.Client(addr, ca_pem=ca, timeout=10)
                tc.set_token(t)
                tc.revoke_self()
                put("tf-role", True, "child token role for OpenTofu")
            except oc.OpenBaoError as e:
                put("tf-role", False, f"{e.code}: {e.detail}"[:200])
        a.revoke_self()
    except oc.OpenBaoError as e:
        put("openbao", False, f"{e.code}: {e.detail}"[:200])
    return res


def all_green(res):
    return all(r["ok"] is not False for r in res) and any(r["ok"] for r in res)


# ---------------------------------------------------------------------------
# Sous-commandes
# ---------------------------------------------------------------------------
def cmd_status(args):
    s, raw = settings(), raw_settings()
    d = state() / "secrets-agent"
    try:
        last = json.loads((d / "status.json").read_text())
    except (OSError, ValueError):
        last = {}
    info = {"mode": s["mode"], "active": ss.openbao_active(s), "addr": s["addr"], "mount": s["mount"],
            "prefix": s["prefix"], "console_id": s["console_id"], "cache_ttl_min": s["cache_ttl_min"],
            "migrated": sorted(s.get("migrated") or {}), "credentials": creds_present(),
            "agent": (d / "agent.json").is_file(),
            "agent_last": {k: last.get(k) for k in ("op", "state", "message", "ended")} if last else None,
            "snapshot_dir": raw.get("snapshot_dir")}
    if info["active"]:
        try:
            h = oc.Client(s["addr"], ca_pem=ca_of(s.get("ca_file")), timeout=5).health()
            info["health"] = {"sealed": h.get("sealed"), "initialized": h.get("initialized")}
        except oc.OpenBaoError as e:
            info["health"] = {"error": e.code}
    if args.json:
        out(info)
    else:
        for k, v in info.items():
            print(f"{k}: {v}")
    return EXIT_OK


def cmd_assistant(args):
    doc = raw_settings()
    cid = ensure_console_id(doc)
    prefix = args.prefix or f"consoles/{cid}"
    try:
        cmds = pol.mode_a_commands(args.addr, args.mount, prefix, cid, args.console_cidr)
    except ValueError as e:
        return refuse("bad-input", str(e))
    out({"ok": True, "console_id": cid, "mount": args.mount, "prefix": prefix,
         "policy": pol.console_policy(args.mount, prefix, cid), "commands": cmds})
    return EXIT_OK


def _mode_target(args, s):
    if args.mode == "b1":
        return pol.B1_ADDR, ca_of(), pol.DEFAULT_MOUNT
    return args.addr or s["addr"], (read_input_text(args.ca) if args.ca else ca_of()), args.mount or s["mount"]


def cmd_check(args):
    s = settings()
    doc = raw_settings()
    cid = ensure_console_id(doc)
    addr, ca, mount = _mode_target(args, s)
    if args.mode == "a" and SPEC.get("ca_pem"):
        ca = SPEC["ca_pem"]
    prefix = args.prefix or s["prefix"] or f"consoles/{cid}"
    wrap = SPEC.get("wrap_token") or (sys.stdin.readline().strip() if args.wrapped_stdin else None)
    res = run_checks(addr, ca, mount, prefix, cid, b1=args.mode == "b1", wrap=wrap,
                     mutate=not args.dry_run, transit=args.transit or args.mode == "b1")
    ok = all_green(res)
    out({"ok": ok, "checks": res, "dry_run": args.dry_run})
    return EXIT_OK if ok else EXIT_FAIL


def cmd_enable(args):
    doc = raw_settings()
    if ss.openbao_active(ss.load_settings(state())):
        return refuse("openbao-already-active", f"OpenBao is already active (mode {doc.get('mode')}); disable first")
    if args.mode == "a":
        return _enable_a(args, doc)
    return _enable_b1(args, doc)


def _enable_a(args, doc):
    if not args.addr or not args.role_id or not (args.wrapped_stdin or SPEC.get("wrap_token")):
        return refuse("bad-input", "mode a needs --addr, --role-id and --wrapped-stdin")
    wrap = SPEC.get("wrap_token") or sys.stdin.readline().strip()
    if not wrap:
        return refuse("bad-input", "no wrapping token on stdin")
    ca = read_input_text(args.ca) if args.ca else (SPEC.get("ca_pem") or None)
    cid = ensure_console_id(doc)
    mount = args.mount or pol.DEFAULT_MOUNT
    prefix = args.prefix or f"consoles/{cid}"
    try:
        pol.console_rules(mount, prefix, cid)
    except ValueError as e:
        return refuse("bad-input", str(e))
    pre = run_checks(args.addr, ca, mount, prefix, cid, wrap=wrap, use_creds=False)
    if not all_green(pre):
        out({"ok": False, "code": "check-failed", "checks": pre})
        return EXIT_FAIL
    if args.dry_run:
        out({"ok": True, "dry_run": True, "checks": pre, "would": ["deposit the secret_id (agent)",
                                                                    "run every check", "activate mode a"]})
        return EXIT_OK
    step("deposit", "running", "the agent unwraps and stores the secret_id")
    agent_ok(run_agent({"op": "store-secret-id", "console_id": cid, "addr": args.addr, "role_id": args.role_id,
                        "wrap_token": wrap, **({"ca_pem": ca} if ca else {})}))
    step("deposit", "done")
    res = run_checks(args.addr, ca_of(), mount, prefix, cid, transit=True)
    if not all_green(res):
        out({"ok": False, "code": "check-failed", "checks": res, "note": "credentials deposited, OpenBao NOT active"})
        return EXIT_FAIL
    doc.update(mode="a", addr=args.addr, mount=mount, prefix=prefix)
    save_settings(doc)
    step("activate", "done", f"mode a, {args.addr}")
    out({"ok": True, "mode": "a", "addr": args.addr, "console_id": cid, "checks": res})
    return EXIT_OK


def _stage_bundle(local):
    """Le paquet vérifié rangé dans <état>/secrets-agent/staged/ : répertoires
    ouverts sans suivre de lien (un lien posé à leur place est remplacé),
    fichier écrit sous un nom neuf puis renommé (un lien à son nom est
    remplacé, jamais suivi)."""
    import safe_fs as sf
    with sf.SafeDir.open_base(state()) as base, \
            base.makedirs("secrets-agent/staged", 0o750) as d:
        d.copy_from(str(local), local.name, 0o640)


def _enable_b1(args, doc):
    if not args.bundle:
        return refuse("bad-input", "mode b1 needs --bundle")
    src = Path(args.bundle)
    if not ob.NAME_RE.match(src.name):
        return refuse("bundle-bad-name", "not an OpenBao bundle name (harvester-ops-openbao-<version>-<tag>.tar.gz)")
    step("bundle", "running", "checking the OpenBao bundle")
    with tempfile.TemporaryDirectory(prefix="openbao-bundle-") as tmp:
        # copie privée d'abord (par le descripteur de root s'il y en a un),
        # vérifiée, puis rangée dans staged/ sans suivre de lien : c'est
        # cette copie vérifiée que l'agent revérifie
        local = Path(tmp) / src.name
        with open_input(args.bundle) as f, open(local, "wb") as o:
            shutil.copyfileobj(f, o, 1 << 20)
        try:
            info = ob.verify(local, Path(tmp) / "check", cosign_key=cosign_key())
        except ob.BundleError as e:
            return refuse(e.code, str(e))
        step("bundle", "done", f"OpenBao {info['version']} ({info['tag']}, {info['index_digest'][:19]})")
        if args.dry_run:
            out({"ok": True, "dry_run": True, "version": info["version"], "tag": info["tag"],
                 "would": ["stage the bundle", "install OpenBao B1 (agent)", "run every check", "activate mode b1"]})
            return EXIT_OK
        cid = ensure_console_id(doc)
        _stage_bundle(local)
    agent_ok(run_agent({"op": "b1-install", "console_id": cid, "bundle": src.name}))
    # décision 275 : la copie hors ligne de la clé se fait maintenant qu'elle
    # existe (offline-copy export, puis ack) ; migrer attend l'acquittement
    doc["offline_copy"] = {"pending": True, "since": int(time.time())}
    prefix = f"consoles/{cid}"
    res = run_checks(pol.B1_ADDR, ca_of(), pol.DEFAULT_MOUNT, prefix, cid, b1=True, transit=True)
    if not all_green(res):
        out({"ok": False, "code": "check-failed", "checks": res, "note": "B1 installed, OpenBao NOT active"})
        return EXIT_FAIL
    doc.update(mode="b1", addr=pol.B1_ADDR, mount=pol.DEFAULT_MOUNT, prefix=prefix)
    save_settings(doc)
    out({"ok": True, "mode": "b1", "addr": pol.B1_ADDR, "console_id": cid, "version": info["version"],
         "checks": res, "offline_copy": "pending: harvester-secrets offline-copy export, then ack"})
    return EXIT_OK


def tf_workspaces_dir():
    """Racine des espaces Terraform de la console : la variable, sinon celle
    que la console a écrite au démarrage (revue m12), sinon `<état>/terraform`."""
    return ss.tf_workspaces_dir()


def encrypted_states(root=None):
    """Identifiants des déclarations dont l'état est chiffré (clés de
    premier niveau seulement, jamais le contenu)."""
    import tf_encryption as tfe
    root = Path(root or tf_workspaces_dir())
    out = set()
    for f in list(root.glob("*/decls/*/terraform.tfstate")) + list(root.glob(".kvm/*/decls/*/terraform.tfstate")):
        try:
            if not f.is_symlink() and tfe.is_encrypted(f.read_text()):
                out.add(f.parent.name)
        except OSError:
            continue
    return sorted(out)


def cmd_disable(args):
    doc = raw_settings()
    migrated = sorted((doc.get("migrated") or {}))
    if migrated:
        return refuse("classes-still-migrated", "bring these classes back first (migrate --to local): "
                      + ", ".join(migrated), classes=migrated)
    # 2.9.0 (tâche 9) : un état chiffré deviendrait illisible sans OpenBao
    enc = encrypted_states()
    if enc:
        return refuse("tf-states-encrypted", "put these declaration states back in clear text first "
                      "(POST /api/tf-declarations/<id>/decrypt-state): " + ", ".join(enc), declarations=enc)
    was = doc.get("mode") or "local"
    if args.remove_b1 and was != "b1":
        return refuse("bad-input", "--remove-b1 only in mode b1")
    # revue I4 des tâches 9-10 : les secrets des déroulés arrêtés (gardés
    # dans OpenBao pour la reprise, décision 262) sont effacés avant : le
    # balayage ne tourne plus une fois OpenBao désactivé. Leur reprise
    # redemandera les secrets (comportement 2.4.0).
    runs, bao = [], None
    if ss.openbao_active(settings()):
        try:
            bao = admin_store()
            runs = bao.children(ss.PLAN_RUN)
        except (ss.SecretError, oc.OpenBaoError) as e:
            if not args.leave_plan_secrets:
                return refuse("plan-secrets-unchecked", "the secrets of stopped plan runs cannot be listed "
                              f"({getattr(e, 'code', '?')}): bring OpenBao back, or disable with "
                              "--leave-plan-secrets and erase them on the server")
            step("plan-secrets", "warn", "secrets of stopped plan runs left on the server (not listed)")
    if args.dry_run:
        out({"ok": True, "dry_run": True, "mode": was, "plan_runs": runs,
             "would": (["erase the secrets of stopped plan runs"] if runs else []) + ["back to local files"]
             + (["remove OpenBao B1 (agent, data kept aside)"] if args.remove_b1 else [])})
        return EXIT_OK
    erased = []
    for rid in runs:
        try:
            bao.delete_tree(ss.PLAN_RUN, rid, actor=ARGS.actor, action=ARGS.id)
            erased.append(rid)
            step(f"plan-run-{rid}", "done", "secrets of the run erased")
        except ss.SecretError as e:
            return fail(e.code, f"secrets of the plan run {rid} not erased; OpenBao stays active")
    doc = raw_settings()
    doc.update(mode="local", addr=None)
    save_settings(doc)
    step("disable", "done", "local files")
    if args.remove_b1:
        agent_ok(run_agent({"op": "b1-remove"}))
    out({"ok": True, "mode": "local", "was": was, "plan_runs_erased": erased})
    return EXIT_OK


def _sha(v):
    return hashlib.sha256(v.encode()).hexdigest()


def _drop_cache(bao, cls, path):
    with bao._lock:
        for r in [r for r in bao._cache if r.startswith(f"openbao:{cls}/{path}#")]:
            del bao._cache[r]


def _write_cas0(bao, cls, path, want, actor=None, action=None, dry_run=False, replace=False):
    """Écrit les champs `want` d'un chemin avec `cas=0` (plan, « Migration »).
    Refus de `cas` : OpenBao rend le même 400 que la valeur soit la même ou
    non (essai 5), la valeur rangée est donc relue et comparée par SHA-256 en
    mémoire, champ par champ. Rend {champ: written|resumed|conflict} ; un
    champ absent d'un chemin qui existe (reprise d'une classe à plusieurs
    champs) est ajouté par une écriture à la version courante."""
    c = bao._client(actor, action)
    kv = bao._kv(cls, path)
    if dry_run:
        data, _m = bao._read(cls, path, actor, action)
        data = data or {}
        return {f: ("write" if f not in data else "resume" if _sha(data[f]) == _sha(v) else "conflict")
                for f, v in want.items()}
    try:
        try:
            w = c.kv_put(bao.mount, kv, dict(want), cas=0,
                         custom_metadata=ss.version_metadata({}, 1, cls, actor, action))
            bao.last_metadata_error = getattr(w, "metadata_error", None)
            return {f: "written" for f in want}
        except oc.OpenBaoError as e:
            if e.code != "openbao-cas-conflict":
                raise
        try:
            data, _m = c.kv_get(bao.mount, kv)
        except oc.OpenBaoError as e:
            if e.code != "openbao-missing":
                raise
            data = {}                       # dernière version supprimée ou détruite
    except oc.OpenBaoError as e:
        raise (bao._failed(e, cls, path) or ss.SecretError(e.code, cls, path, e.detail)) from None
    out = {}
    for f, v in want.items():
        if f in data and _sha(data[f]) != _sha(v) and replace:
            # remplacement demandé par l'exploitant pour CE nom (revue M2) :
            # écrit à la version courante, l'ancienne valeur reste une version
            # précédente dans OpenBao (`versions`, `rollback`), jamais détruite
            bao.put(cls, path, f, v, actor=actor, action=action)
            out[f] = "replaced"
        elif f in data:
            out[f] = "resumed" if _sha(data[f]) == _sha(v) else "conflict"
        else:
            bao.put(cls, path, f, v, actor=actor, action=action)
            out[f] = "written"
    return out


def migrate_class(bao, fs, cls, name=None, dry_run=False, actor=None, action=None, replace=False):
    """Migration d'une classe vers OpenBao (plan, « Migration ») : pour
    chaque chemin, écriture `cas=0` ; refus de `cas` -> valeur relue et
    comparée (identique : reprise sans écriture ; différente : conflit, local
    intact) ; relecture hors cache, SHA-256 comparés, PUIS la valeur locale
    devient la référence (l'adaptateur garde l'original). Un secret trop gros,
    hors du répertoire d'état ou sans déclaration reste local. Une panne
    d'OpenBao ou une relecture différente arrête la classe : ce qui est fait
    reste fait (références), le reste est intact, une relance reprend.
    Rend (faits, erreurs)."""
    ad = sc.adapter(cls)
    done, errors = [], []
    groups = {}
    for path, field in ad.list_local(fs):
        if name and path.split("/")[0] != name:
            continue
        label = f"{cls}/{path}#{field}"
        value = ad.read_local(fs, path, field)
        if value is None or ss.is_ref(value):
            step(f"migrate-{cls}", "skipped", f"{label}: already a reference or empty")
            continue
        if len(value.encode()) > ss.VALUE_MAX:
            errors.append({"secret": label, "code": "secret-too-big"})
            step(f"migrate-{cls}", "error", f"{label}: secret-too-big, stays local")
            continue
        why = ad.eligible(fs, path, field) if ad.eligible else None
        if why:
            errors.append({"secret": label, "code": why})
            step(f"migrate-{cls}", "error", f"{label}: {why}, stays local")
            continue
        groups.setdefault(path, []).append((field, value, label))
    for path, items in groups.items():
        want = {f: v for f, v, _l in items}
        try:
            status = _write_cas0(bao, cls, path, want, actor, action, dry_run, replace)
        except ss.SecretError as e:
            for _f, _v, label in items:
                errors.append({"secret": label, "code": e.code})
            step(f"migrate-{cls}", "error", f"{cls}/{path}: {e.code}, local kept")
            if e.code in ss.DOWN_CODES:
                break                        # OpenBao ne répond plus : la classe s'arrête
            continue
        if dry_run:
            for f, _v, label in items:
                done.append({"secret": label, "would": status[f]})
            continue
        _drop_cache(bao, cls, path)
        try:
            back, _m = bao._read(cls, path, actor, action)
        except ss.SecretError as e:
            errors += [{"secret": label, "code": e.code} for _f, _v, label in items]
            step(f"migrate-{cls}", "error", f"{cls}/{path}: read back failed ({e.code}), local kept")
            break
        stop = False
        for f, v, label in items:
            if status[f] == "conflict":
                errors.append({"secret": label, "code": "openbao-cas-conflict"})
                step(f"migrate-{cls}", "error", f"{label}: another value already in OpenBao, local kept "
                     f"(see `versions`; to replace it: migrate --name {path.split('/')[0]} --replace-existing)")
                continue
            if not back or _sha(back.get(f, "")) != _sha(v):
                errors.append({"secret": label, "code": "migrate-readback-mismatch"})
                step(f"migrate-{cls}", "error", f"{label}: read back differs, local kept, class stopped")
                stop = True
                break
            ref = ss.make_ref(cls, path, f, "openbao")
            try:
                ad.write_ref(fs, path, f, ref, v, action)
            except TypeError:                # adaptateur simulé (fs, path, field, ref)
                ad.write_ref(fs, path, f, ref)
            except (ValueError, KeyError, OSError) as e:
                errors.append({"secret": label, "code": "local-reference-failed"})
                step(f"migrate-{cls}", "error", f"{label}: local value not replaced ({type(e).__name__}), "
                      "local kept")
                continue
            resumed = status[f] == "resumed"
            done.append({"secret": label, "resumed": resumed, **({"replaced": True} if status[f] == "replaced"
                                                                  else {})})
            step(f"migrate-{cls}", "running", f"{label}: " + (
                "already there" if resumed else
                "migrated, the other value is kept as the previous version" if status[f] == "replaced"
                else "migrated"))
        if stop:
            break
    return done, errors


def restore_class(bao, fs, cls, name=None, dry_run=False, actor=None, action=None):
    """Retour en local : la valeur relue dans OpenBao est réécrite en local
    (0600), relue, et seulement alors détruite dans OpenBao."""
    ad = sc.adapter(cls)
    done, errors = [], []
    for path, field in ad.list_local(fs):
        if name and path.split("/")[0] != name:
            continue
        ref = ad.read_local(fs, path, field)
        if not ss.is_openbao_ref(ref):
            continue
        if dry_run:
            done.append({"secret": ref, "would": "restore"})
            continue
        try:
            value = bao.get(ref, actor)
        except ss.SecretMissing:
            errors.append({"secret": ref, "code": "openbao-missing"})
            step(f"restore-{cls}", "error", f"{ref}: absent from OpenBao, reference kept")
            continue
        except ss.SecretError as e:
            errors.append({"secret": ref, "code": e.code})
            step(f"restore-{cls}", "error", f"{ref}: {e.code}, reference kept")
            if e.code in ss.DOWN_CODES:
                break
            continue
        try:
            ad.restore_local(fs, path, field, value)
        except NotImplementedError:
            errors.append({"secret": ref, "code": "class-adapter-pending"})
            break
        except (ValueError, KeyError, OSError) as e:
            errors.append({"secret": ref, "code": "restore-local-failed"})
            step(f"restore-{cls}", "error", f"{ref}: local write failed ({type(e).__name__}), kept in OpenBao")
            continue
        if ad.read_local(fs, path, field) != value:
            errors.append({"secret": ref, "code": "restore-readback-mismatch"})
            step(f"restore-{cls}", "error", f"{ref}: local read back differs, kept in OpenBao")
            break
        try:
            bao.delete(ref, actor=actor, action=action)
        except ss.SecretError as e:
            errors.append({"secret": ref, "code": e.code})
            step(f"restore-{cls}", "error", f"{ref}: local again, not yet removed from OpenBao ({e.code})")
            continue
        done.append({"secret": ref})
        step(f"restore-{cls}", "running", f"{ref}: back to local")
    return done, errors


def _refs_left(fs, cls):
    ad = sc.adapter(cls)
    return [f"{cls}/{p}#{f}" for p, f in ad.list_local(fs) if ss.is_openbao_ref(ad.read_local(fs, p, f))]


def _kubeconfig_gate(doc):
    """Point 255 : la classe `kubeconfig` (sans elle, plus aucun cluster) ne
    migre qu'après une autre classe migrée et une panne simulée réussie."""
    others = [c for c in (doc.get("migrated") or {}) if c != "kubeconfig"]
    if not others:
        return "migrate another class first (replication-notify, rancher-oidc, ...)"
    ot = doc.get("outage_test") or {}
    if not ot.get("ok_at"):
        return "run a simulated outage first (harvester-secrets outage-test)"
    # l'essai vaut pour le serveur et le mode d'alors (revue M7)
    if (ot.get("addr"), ot.get("mode")) != (doc.get("addr"), doc.get("mode")):
        return "the OpenBao server or mode changed since the simulated outage: run it again"
    return None


def cmd_migrate(args):
    s = settings()
    classes = args.cls or []
    if len(classes) != 1:
        return refuse("one-class-per-action", "give exactly one --class (one action per class)")
    c = classes[0]
    try:
        sc.require_migratable(c)
    except (sc.NotMigratable, ValueError):
        return refuse("class-not-migratable", f"{c} cannot move to OpenBao")
    if c == "plan-run":
        return refuse("class-not-migratable", "plan-run has nothing to migrate (new runs go to OpenBao)")
    if args.to == "openbao" and not args.dry_run and (raw_settings().get("offline_copy") or {}).get("pending"):
        return refuse("offline-copy-ack-required", "keep the offline copy of the unseal key first "
                      "(harvester-secrets offline-copy export, then offline-copy ack --key-id ID)")
    if args.replace_existing and (args.to != "openbao" or not args.name):
        return refuse("replace-needs-name", "--replace-existing replaces one named secret: "
                      "give --to openbao and --name")
    doc = raw_settings()
    if args.to == "openbao" and c == "kubeconfig" and not args.dry_run:
        why = _kubeconfig_gate(doc)
        if why:
            return refuse("kubeconfig-gate", why)
    bao = admin_store(s)
    fs = ss.FileStore(state())
    if args.to == "openbao" and not args.dry_run:
        # marque posée AVANT le premier secret : un écrivain de la console
        # qui écrit pendant la migration range sa valeur dans OpenBao au lieu
        # de réécrire le fichier local que la migration remplace (revue I2) ;
        # recalculée en fin d'action
        doc = raw_settings()
        mig = doc.setdefault("migrated", {})
        if c not in mig:
            mig[c] = {"at": int(time.time()), "by": ARGS.actor or "cli", "action": ARGS.id or "",
                      "complete": False}
            save_settings(doc)
    if args.to == "openbao":
        done, errors = migrate_class(bao, fs, c, args.name, args.dry_run, ARGS.actor, ARGS.id,
                                     replace=args.replace_existing)
    else:
        done, errors = restore_class(bao, fs, c, args.name, args.dry_run, ARGS.actor, ARGS.id)
    if not args.dry_run:
        # une classe dont un secret est une référence est migrée (même en
        # partie) : `disable` refusé, les nouveaux secrets vont dans OpenBao
        # aller sans erreur : la classe est marquée même sans aucun secret
        # local (une classe vide migrée range ses secrets nouveaux dans
        # OpenBao) ; seul le retour en local (ou `disable`) retire la marque
        left = _refs_left(fs, c)
        doc = raw_settings()
        mig = doc.setdefault("migrated", {})
        if left or (args.to == "openbao" and not errors):
            prev = mig.get(c) or {}
            mig[c] = {"at": prev.get("at") or int(time.time()), "by": prev.get("by") or ARGS.actor or "cli",
                      "action": prev.get("action") or ARGS.id or "", "complete": not errors}
        else:
            mig.pop(c, None)
        save_settings(doc)
        step("class", "done", f"{c}: {len(left)} secret(s) in OpenBao")
    out({"ok": not errors, "to": args.to, "dry_run": args.dry_run, "classes": {c: {"done": done, "errors": errors}}})
    return EXIT_OK if not errors else EXIT_FAIL


def cmd_confirm(args):
    """Efface les originaux gardés d'une classe migrée (confirmation), ou
    ceux de plus de `--older-than-days` jours (délai de garde, lancé par la
    tâche de fond de la console). Un original n'est effacé que si OpenBao
    rend encore le secret qui l'a remplacé."""
    doc = raw_settings()
    try:
        sc.require_migratable(args.cls)
    except (sc.NotMigratable, ValueError):
        return refuse("class-not-migratable", f"{args.cls} has no originals")
    fs = ss.FileStore(state())
    ad = sc.adapter(args.cls)
    originals = ad.originals(fs) if ad.originals else []
    if args.cls not in (doc.get("migrated") or {}) and not originals:
        return refuse("class-not-migrated", f"{args.cls} is not migrated")
    now = time.time()
    due = [o for o in originals
           if args.older_than_days is None or now - float(o.get("at") or 0) >= args.older_than_days * 86400]
    bao = admin_store() if due else None
    erased, errors = 0, []
    for o in due:
        label = f"{args.cls}/{o['path']}#{o['field']}"
        local = ad.read_local(fs, o["path"], o["field"])
        expected = ss.make_ref(args.cls, o["path"], o["field"], "openbao")
        if local != expected:
            # la valeur locale n'est plus la référence de cet original
            # (déclaration réécrite, fichier remplacé) : l'original n'est
            # effacé que s'il porte la valeur locale actuelle (revue I1)
            same = local is not None and not ss.is_ref(local) and ad.original_value is not None \
                and ad.original_value(fs, o["path"], o["field"]) == local
            if not same:
                errors.append({"secret": label, "code": "original-kept-local-changed"})
                step("confirm", "error", f"{label}: local value is not its reference, original kept")
                continue
        else:
            try:
                bao.get(local, ARGS.actor)
            except ss.SecretMissing:
                errors.append({"secret": label, "code": "openbao-missing"})
                step("confirm", "error", f"{label}: absent from OpenBao, original kept")
                continue
            except ss.SecretError as e:
                errors.append({"secret": label, "code": e.code})
                step("confirm", "error", f"{label}: {e.code}, original kept")
                if e.code in ss.DOWN_CODES:
                    break
                continue
        try:
            gone = ad.erase_original(fs, o["path"], o["field"])
        except ValueError:
            errors.append({"secret": label, "code": "originals-registry-unsafe"})
            step("confirm", "error", f"{label}: unsafe path in the originals registry, nothing erased")
            continue
        if gone:
            erased += 1
            step("confirm", "running", f"{label}: original erased")
    doc = raw_settings()
    if args.cls in (doc.get("migrated") or {}) and not errors and len(due) == len(originals):
        doc["migrated"][args.cls]["confirmed"] = int(now)
        save_settings(doc)
    out({"ok": not errors, "class": args.cls, "originals_erased": erased,
         "originals_kept": len(originals) - erased, "errors": errors})
    return EXIT_OK if not errors else EXIT_FAIL


def cmd_outage_test(args):
    """Panne simulée (point 255) : un secret d'une classe migrée est lu
    (cache chaud), puis la connexion est coupée le temps d'une lecture :
    la valeur doit venir du cache (état « dégradé »), et une lecture à
    froid doit être refusée avec la classe et le nom, sans repli local."""
    s = settings()
    doc = raw_settings()
    fs = ss.FileStore(state())
    ref = next((r for c in (doc.get("migrated") or {}) for r in _refs_left(fs, c)), None)
    if ref is None:
        return refuse("class-not-migrated", "migrate a class first: the test reads one of its secrets")
    bao = admin_store(s)
    ref = f"openbao:{ref}"
    cls, path, _f = ss.parse_ref(ref)
    checks = []
    warm = _sha(bao.get(ref, ARGS.actor))
    real = bao._factory

    def cut(actor=None, action=None):
        # adresse sans serveur (port 9, discard) : la requête échoue au réseau
        c = oc.Client(OUTAGE_ADDR, timeout=5, retry_delays=(), actor=actor or "service", action=action)
        c.set_token("outage-test")
        return c
    bao._factory = cut
    try:
        served = _sha(bao.get(ref, ARGS.actor))
        try:
            bao._read(cls, path)             # lecture à froid : met l'état à jour
        except ss.SecretError:
            pass
        st = bao.status()
        checks.append({"id": "cache", "ok": served == warm and st.get("state") == "degraded",
                       "reason": f"state {st.get('state')}"})
        bao.clear_cache()
        try:
            bao.get(ref, ARGS.actor)
            checks.append({"id": "cold-refused", "ok": False, "reason": "a value was served without OpenBao"})
        except ss.SecretError as e:
            checks.append({"id": "cold-refused", "ok": e.code == "openbao-unreachable" and e.cls == cls,
                           "reason": e.code})
        except ss.SecretMissing:
            checks.append({"id": "cold-refused", "ok": False, "reason": "missing instead of unreachable"})
    finally:
        bao._factory = real
    ok = all(c["ok"] for c in checks)
    for c in checks:
        step(f"outage-{c['id']}", "done" if c["ok"] else "error", c["reason"])
    if ok:
        doc = raw_settings()
        doc["outage_test"] = {"ok_at": int(time.time()), "by": ARGS.actor or "cli",
                              "addr": doc.get("addr"), "mode": doc.get("mode")}
        save_settings(doc)
    out({"ok": ok, "checks": checks})
    return EXIT_OK if ok else EXIT_FAIL


def cmd_offline_copy(args):
    """Décision 275 : copie hors ligne de la clé de descellement de B1.
    `export` la remet une fois (fichier lu par la console pour un seul
    téléchargement, ou copié par `--to` puis effacé), `done` efface la copie
    remise, `ack` acquitte la copie gardée de la clé en service (refusé
    tant qu'elle n'a pas été remise). Jamais la clé dans la sortie."""
    s = settings()
    if s.get("mode") != "b1":
        return refuse("b1-only", "the unseal key belongs to mode B1")
    if args.what == "export":
        st = agent_ok(run_agent({"op": "export-unseal-key"}))
        key_id = (st.get("info") or {}).get("key_id")
        if args.to:
            src = ss.openbao_dir() / "unseal-export.key"
            try:
                data = src.read_bytes()
                fd = OUTPUT_FDS.get(args.to)
                if fd is None:
                    fd = os.open(args.to, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                 0o600)
                else:
                    fd = os.dup(fd)             # ouvert par root (fichier neuf de l'administrateur)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
            except OSError as e:
                agent_ok(run_agent({"op": "export-unseal-key-done"}))
                return fail("offline-copy-write", f"copy not written ({type(e).__name__}); nothing kept")
            agent_ok(run_agent({"op": "export-unseal-key-done"}))
            step("offline-copy", "done", f"key {key_id} copied; store it offline, then offline-copy ack")
        out({"ok": True, "key_id": key_id, "copied": bool(args.to)})
        return EXIT_OK
    if args.what == "done":
        agent_ok(run_agent({"op": "export-unseal-key-done"}))
        out({"ok": True})
        return EXIT_OK
    if not args.key_id:
        return refuse("bad-input", "offline-copy ack needs --key-id")
    st = agent_ok(run_agent({"op": "ack-offline-copy", "key_id": args.key_id}))
    doc = raw_settings()
    doc["offline_copy"] = {"pending": False, "key_id": args.key_id, "acked_at": int(time.time()),
                           "by": ARGS.actor or "cli"}
    save_settings(doc)
    out({"ok": True, "key_id": args.key_id, "acked": True})
    return EXIT_OK


def cmd_versions(args):
    if not ss.is_openbao_ref(args.ref):
        return refuse("bad-input", "an openbao: reference is required")
    out({"ok": True, "ref": args.ref, "versions": admin_store().versions(args.ref)})
    return EXIT_OK


def cmd_rollback(args):
    if not ss.is_openbao_ref(args.ref):
        return refuse("bad-input", "an openbao: reference is required")
    bao = admin_store()
    _b, cls, path, field = ss.parse_ref_full(args.ref)
    c = bao._client(ARGS.actor, ARGS.id)
    try:
        data, _m = c.kv_get(bao.mount, bao._kv(cls, path), version=args.version)
    except oc.OpenBaoError as e:
        return fail(e.code, f"version {args.version} unreadable: {e.detail}"[:200])
    if field not in data:
        return fail("openbao-missing", f"version {args.version} has no field {field}")
    if args.dry_run:
        out({"ok": True, "dry_run": True, "ref": args.ref, "would": f"write version {args.version} again"})
        return EXIT_OK
    bao.put(cls, path, field, data[field], actor=ARGS.actor, action=ARGS.id)
    out({"ok": True, "ref": args.ref, "from_version": args.version})
    return EXIT_OK


def _rotate_ssh_kvm(args, s):
    """Point 260 : rotation de la clé SSH d'un hôte KVM (kvm_host.rotate_key),
    sous le verrou des déclarations d'hôtes. Clé locale ou dans OpenBao ; les
    copies de travail vont sur tmpfs quand OpenBao est actif, sinon dans un
    répertoire privé de l'état ; effacées à la fin."""
    import kvm_decl
    import kvm_host
    name = args.target or ""
    try:
        kvm_decl.decl_path(state(), name)
    except kvm_decl.DeclError:
        return refuse("bad-input", "rotate ssh-kvm HOST: declared KVM host name expected")
    sd = state()
    try:
        with kvm_decl.decl_lock(sd):
            try:
                decl = kvm_decl.load(sd, name)
            except FileNotFoundError:
                return refuse("not-found", f"no KVM host {name}")
            value = decl["ssh_key"]
            in_bao = isinstance(value, str) and value.startswith("openbao:")
            if args.dry_run:
                out({"ok": True, "dry_run": True, "host": name, "store": "openbao" if in_bao else "local",
                     "would": ["new key pair", "new public key added with the current key",
                               "test login with the new key", "new key stored",
                               "old public key removed with the new key"]})
                return EXIT_OK
            bao = admin_store(s) if in_bao else None
            if in_bao:
                old_text = bao.get(value, ARGS.actor)
            else:
                old_text = kvm_decl.resolve(sd, value).read_text()
            base = ss.secret_files_dir(s) if ss.openbao_active(s) else kvm_decl.ssh_dir(sd)
            Path(base).mkdir(parents=True, exist_ok=True, mode=0o700)
            work = Path(tempfile.mkdtemp(prefix="rotate-", dir=str(base)))
            try:
                old_key, new_key = work / "old", work / "new"
                fd = os.open(old_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(old_text if old_text.endswith("\n") else old_text + "\n")
                stamp = time.strftime("%Y%m%d", time.gmtime())
                r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                                    f"harvester-ops {name} {stamp}", "-f", str(new_key)],
                                   capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
                o = subprocess.run(["ssh-keygen", "-y", "-f", str(old_key)], capture_output=True, text=True,
                                   timeout=30, stdin=subprocess.DEVNULL)
                if r.returncode != 0 or o.returncode != 0:
                    return fail("ssh-keygen-failed", "ssh-keygen could not make or read a key")
                new_text = new_key.read_text()
                new_pub = (work / "new.pub").read_text().strip()

                def put(text):
                    if in_bao:
                        bao.put("ssh-kvm", name, "private_key", text, actor=ARGS.actor, action=ARGS.id)
                    else:
                        kvm_decl.write_key(sd, name, text)
                res = kvm_host.rotate_key(decl, sd, old_key, new_key, o.stdout.strip(), new_pub,
                                          lambda: put(new_text), lambda: put(old_text), step=step,
                                          runner=ROTATE_RUNNER or subprocess.run)
            finally:
                shutil.rmtree(work, ignore_errors=True)
    except TimeoutError as e:
        return refuse("kvm-busy", str(e))
    except kvm_host.KvmError as e:
        return fail(f"kvm-{e.kind}", f"key rotation of {name} failed, previous key kept: {e.message}"[:300])
    out({"ok": True, "rotated": "ssh-kvm", "host": name, "old_refused": res["old_refused"],
         "store": "openbao" if in_bao else "local"})
    return EXIT_OK


def cmd_rotate(args):
    s = settings()
    if args.what == "ssh-kvm":
        return _rotate_ssh_kvm(args, s)
    if not ss.openbao_active(s):
        return refuse("openbao-inactive", "OpenBao is not active")
    if args.what == "secret-id":
        if args.dry_run:
            out({"ok": True, "dry_run": True, "would": "new secret_id, test login, then old one destroyed"})
            return EXIT_OK
        st = agent_ok(run_agent({"op": "rotate-secret-id"}))
        out({"ok": True, "rotated": "secret-id", "message": st.get("message")})
        return EXIT_OK
    if args.what == "unseal-key":
        if s["mode"] != "b1":
            return refuse("b1-only", "the unseal key belongs to mode B1")
        if args.dry_run:
            out({"ok": True, "dry_run": True, "would": "new key, restart, sys/rotate/root, snapshot, old key retired"})
            return EXIT_OK
        st = agent_ok(run_agent({"op": "rotate-unseal-key"}))
        doc = raw_settings()
        doc["offline_copy"] = {"pending": True, "since": int(time.time())}
        save_settings(doc)
        out({"ok": True, "rotated": "unseal-key", "message": st.get("message"),
             "offline_copy": "pending: harvester-secrets offline-copy export, then ack"})
        return EXIT_OK
    if args.what == "transit":
        try:
            key = pol.tf_key(s["console_id"], args.target or "")
        except ValueError as e:
            return refuse("bad-input", str(e))
        if args.dry_run:
            out({"ok": True, "dry_run": True, "would": f"rotate transit key {key} (older states stay readable)"})
            return EXIT_OK
        admin_client(s=s).transit_rotate(key)
        out({"ok": True, "rotated": "transit", "key": key})
        return EXIT_OK
    return refuse("bad-input", "unknown rotation")


KEY_ID_RE = re.compile(r"^hops-[0-9]{8}-[0-9]{1,4}$")


def agent_health():
    """`health.json` de l'agent (sans valeur) : clé en service, ses
    instantanés, place du volume d'audit, âge du secret_id."""
    try:
        doc = json.loads((state() / "secrets-agent" / "health.json").read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def snapshot_files(dest):
    """Instantanés d'un répertoire : fichiers ordinaires au nom attendu, du
    plus ancien au plus récent (la date est dans le nom)."""
    out = []
    try:
        names = os.listdir(dest)
    except OSError:
        return out
    for n in sorted(names):
        if not SNAP_RE.match(n):
            continue
        try:
            st = os.lstat(os.path.join(dest, n))
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            out.append({"file": n, "bytes": st.st_size, "at": int(st.st_mtime)})
    return out


def _snapshot_dest(raw, given=None):
    dest = Path(given or raw.get("snapshot_dir") or "")
    if not dest.is_absolute() or not dest.is_dir():
        raise CliError("bad-input", "give --dest (an existing absolute directory) or set snapshot_dir", EXIT_REFUSED)
    try:
        dest.resolve().relative_to(state().resolve())
        raise CliError("snapshot-in-state-dir", "never in the state directory (it is backed up with the console)",
                       EXIT_REFUSED)
    except ValueError:
        pass
    return dest


def cmd_snapshot(args):
    """Instantané raft de B1 (point 259) : `openbao-<date>-<clé>.snap` 0600
    dans le répertoire réglé (jamais sous l'état), la clé de descellement en
    service dans le nom (lue dans l'état de l'agent, sans valeur) ; les
    plus anciens au-delà de `snapshot_keep` (14) effacés. Résultat gardé
    dans les réglages (dernier réussi, dernière erreur)."""
    s, raw = settings(), raw_settings()
    if s["mode"] != "b1" or not ss.openbao_active(s):
        return refuse("b1-only", "snapshots are taken in mode B1 (in mode A the server belongs to you)")
    dest = _snapshot_dest(raw, args.dest)
    key = str(agent_health().get("current_key") or "")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"openbao-{stamp}-{key}.snap" if KEY_ID_RE.match(key) else f"openbao-{stamp}.snap"
    keep = args.keep or s["snapshot_keep"]
    if args.dry_run:
        out({"ok": True, "dry_run": True, "file": name, "keep": keep})
        return EXIT_OK

    def record(**kw):
        doc = raw_settings()
        doc["snapshots"] = dict(doc.get("snapshots") or {}, **kw)
        save_settings(doc)
    try:
        n = admin_client(s=s).raft_snapshot(str(dest / name))
    except (oc.OpenBaoError, OSError) as e:
        code = getattr(e, "code", None) or type(e).__name__
        record(last_error=code, last_error_at=int(time.time()))
        return fail("snapshot-failed", f"snapshot not taken ({code})")
    step("snapshot", "done", f"{name}, {n} bytes")
    files = snapshot_files(dest)
    pruned = [f["file"] for f in files[:-keep]] if len(files) > keep else []
    for f in pruned:
        try:
            os.unlink(dest / f)
        except OSError:
            pass
    if pruned:
        step("retention", "done", f"{len(pruned)} older snapshot(s) removed, {keep} kept")
    record(last_ok_at=int(time.time()), last_file=name, bytes=n, last_error=None, last_error_at=None)
    out({"ok": True, "file": name, "bytes": n, "key_id": key or None, "pruned": pruned})
    return EXIT_OK


def cmd_snapshots(args):
    """Liste sans valeur : instantanés du répertoire réglé et ceux de
    l'agent (rotation, avant une mise à jour), réglages, dernier résultat."""
    s, raw = settings(), raw_settings()
    d = raw.get("snapshot_dir")
    h = agent_health()
    out({"ok": True, "mode": s["mode"], "dir": d, "time": s["snapshot_time"], "keep": s["snapshot_keep"],
         "files": snapshot_files(d) if isinstance(d, str) and os.path.isabs(d) else [],
         "agent_files": h.get("snapshots") or [], "keys": h.get("keys") or [],
         "current_key": h.get("current_key"), "last": s.get("snapshots") or None})
    return EXIT_OK


SETTABLE = {
    "cache_ttl_min": ss.CACHE_TTL_RANGE, "originals_keep_days": ss.ORIGINALS_KEEP_RANGE,
    "plan_secrets_keep_days": ss.PLAN_SECRETS_KEEP_RANGE, "snapshot_keep": ss.SNAPSHOT_KEEP_RANGE,
    "timeout": (5, 60), "snapshot_time": None, "snapshot_dir": None,
}


def cmd_settings(args):
    """Réglages sans secret, modifiables à chaud (point 269) : `--set
    CLÉ=VALEUR` (bornes vérifiées), sinon affichage."""
    doc = raw_settings()
    changed = {}
    for item in args.set or []:
        k, sep, v = item.partition("=")
        if not sep or k not in SETTABLE:
            return refuse("bad-input", f"unknown setting {k!r} (allowed: {', '.join(sorted(SETTABLE))})")
        rng = SETTABLE[k]
        if rng:
            try:
                n = int(v)
            except ValueError:
                return refuse("bad-input", f"{k}: integer expected")
            if not rng[0] <= n <= rng[1]:
                return refuse("bad-input", f"{k}: between {rng[0]} and {rng[1]}")
            changed[k] = n
        elif k == "snapshot_time":
            if not ss.SNAPSHOT_TIME_RE.match(v):
                return refuse("bad-input", "snapshot_time: HH:MM expected")
            changed[k] = v
        else:
            if v == "":
                changed[k] = None
                continue
            _snapshot_dest(doc, v)
            if not os.access(v, os.W_OK | os.X_OK):
                return refuse("snapshot-dir-not-writable", "the console account cannot write in this directory")
            changed[k] = str(Path(v))
    if changed:
        doc = raw_settings()
        doc.update(changed)
        save_settings(doc)
        step("settings", "done", ", ".join(f"{k}={v}" for k, v in sorted(changed.items())))
    s = settings()
    out({"ok": True, "changed": sorted(changed), "settings": {k: s.get(k) for k in SETTABLE}})
    return EXIT_OK


# Balayage des secrets orphelins (rapport de la tâche 8, point 2) : canal de
# notification ou action annexe retirés, espace Terraform KVM supprimé. Un
# chemin n'est effacé que si AUCUNE référence locale ne le désigne, s'il n'a
# pas été écrit depuis `min_age_days`, et si la source locale de la classe a
# été trouvée (une source absente ou vide ne vaut pas « rien n'est utilisé »).
SWEEP_CLASSES = ("replication-notify", "replication-hook", "tf-kvm")
REPORT_CLASSES = ("rancher-oidc", "ssh-kvm", "ssh-cluster", "kubeconfig")


def _last_written(versions):
    best = 0.0
    for v in versions or []:
        t = str(v.get("created") or "")
        try:
            best = max(best, calendar.timegm(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")))
        except ValueError:
            continue
    return best or None


def orphan_scan(bao, fs, classes, min_age_days, now=None):
    now = time.time() if now is None else now
    rows = []
    for cls in classes:
        source = fs.repl_db if cls.startswith("replication-") else (fs.tf_dir if cls == "tf-kvm" else fs.state_dir)
        used = set()
        for path, field in fs.list_local(cls):
            v = fs.read_local(cls, path, field)
            if isinstance(v, str) and ss.is_openbao_ref(v):
                used.add(v)
        by_path = {}
        for top in bao.children(cls):
            for ref in bao.refs(cls, top):
                _b, c, path, _f = ss.parse_ref_full(ref)
                by_path.setdefault(path, []).append(ref)
        for path, refs in sorted(by_path.items()):
            unused = [r for r in refs if r not in used]
            if not unused:
                continue
            whole = len(unused) == len(refs)
            written = _last_written(bao.versions(refs[0]))
            age_ok = written is not None and now - written >= min_age_days * 86400
            why = None
            if not Path(source).exists():
                why = "local source not found"
            elif not used:
                why = "no local reference seen for this class"
            elif not whole:
                why = "some fields of this secret are still used"
            elif not age_ok:
                why = f"written less than {min_age_days} days ago"
            rows.append({"class": cls, "path": path, "fields": len(unused), "written": written,
                         "sweepable": why is None and cls in SWEEP_CLASSES, "kept_because": why})
    return rows


def cmd_sweep_orphans(args):
    s = settings()
    if not ss.openbao_active(s):
        return refuse("openbao-inactive", "OpenBao is not active")
    fs = ss.FileStore(state(), tf_dir=args.tf_dir, repl_db=args.repl_db)
    bao = admin_store(s)
    classes = [c for c in (args.cls or SWEEP_CLASSES + REPORT_CLASSES) if c in SWEEP_CLASSES + REPORT_CLASSES]
    rows = orphan_scan(bao, fs, classes, args.min_age_days)
    removed = []
    for r in rows:
        if not r["sweepable"]:
            continue
        if args.dry_run:
            continue
        try:
            bao.delete_tree(r["class"], r["path"], actor=ARGS.actor, action=ARGS.id)
            removed.append(f"{r['class']}/{r['path']}")
            step(f"orphan-{r['class']}", "done", f"{r['class']}/{r['path']} removed (no reference)")
        except ss.SecretError as e:
            step(f"orphan-{r['class']}", "error", f"{r['class']}/{r['path']}: {e.code}")
    out({"ok": True, "dry_run": args.dry_run, "orphans": rows, "removed": removed})
    return EXIT_OK


def cmd_restore(args):
    s = settings()
    if s["mode"] != "b1":
        return refuse("b1-only", "restore applies to mode B1")
    if not SNAP_RE.match(Path(args.file).name) or "/" in args.file:
        return refuse("snapshot-bad-name", "give the file name of a snapshot (openbao-<date>.snap)")
    if args.dry_run:
        out({"ok": True, "dry_run": True, "would": f"restore {args.file} in a fresh instance (agent)"})
        return EXIT_OK
    st = agent_ok(run_agent({"op": "restore-snapshot", "file": args.file}))
    out({"ok": True, "restored": args.file, "message": st.get("message")})
    return EXIT_OK


def _cosign_runner(key, registry, user, password):
    """Vérification cosign par l'outil s'il est présent ; les identifiants
    vont dans une configuration de registre privée (0600, effacée), jamais
    en argument."""
    exe = shutil.which("cosign")
    if not exe:
        return None
    import base64

    def run(ref):
        d = tempfile.mkdtemp(prefix="hops-cosign-", dir=os.environ.get("XDG_RUNTIME_DIR") or None)
        try:
            os.chmod(d, 0o700)
            auth = base64.b64encode(f"{user}:{password}".encode()).decode()
            fd = os.open(Path(d) / "config.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"auths": {registry: {"auth": auth}}}, f)
            r = subprocess.run([exe, "verify", "--key", str(key), ref], capture_output=True, text=True,
                               env={"PATH": os.environ.get("PATH", ""), "HOME": d, "DOCKER_CONFIG": d}, timeout=300)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if r.returncode == 0:
            return ("verified", f"cosign verify --key {Path(key).name}")
        return ("failed", ((r.stderr or "").strip().splitlines() or ["cosign failed"])[-1][:200])
    return run


def cmd_bundle(args):
    import oci_push as op
    if not args.password_stdin:
        return refuse("bad-input", "the service account password is read from stdin only (--password-stdin)")
    password = sys.stdin.readline().rstrip("\n")
    if not password:
        return refuse("bad-input", "no password on stdin")
    o = Path(args.o)
    if o.name.endswith(".tar.gz"):
        if not ob.NAME_RE.match(o.name):
            return refuse("bundle-bad-name", "the bundle name must be harvester-ops-openbao-<version>-<tag>.tar.gz")
        out_dir, wanted = o.parent, o.name
    else:
        out_dir, wanted = o, None
    out_dir.mkdir(parents=True, exist_ok=True)
    key = Path(args.cosign_key) if args.cosign_key else cosign_key()
    cos = _cosign_runner(key, args.registry, args.user, password) if key.is_file() else None
    src = op.Registry(args.registry, {"username": args.user, "password": password}, plain_http=args.plain_http)
    step("pull", "running", f"{args.registry}/{args.repository}:{args.tag}")
    try:
        got = ob.build(src, args.registry, args.repository, args.tag, (HERE.parent / "config" / "openbao" /
                                                                       "openbao.hcl.tpl").read_text(),
                       out_dir, cosign=cos, cosign_key=key if key.is_file() else None)
    except ob.BundleError as e:
        return fail(e.code, str(e))
    except op.RegistryError as e:
        return fail(getattr(e, "code", None) or "registry-error", ss_scrub(str(e), password))
    finally:
        password = None
    if wanted and got["name"] != wanted:
        Path(got["path"]).unlink(missing_ok=True)
        Path(got["path"] + ".sha256").unlink(missing_ok=True)
        return fail("bundle-bad-name", f"the image gives {got['name']}, not {wanted}")
    step("pull", "done", f"{got['name']} (cosign: {got['cosign']})")
    out({"ok": True, **got})
    return EXIT_OK


def ss_scrub(text, secret):
    return oc.scrub(text.replace(secret, "***") if secret else text)


def cmd_file(args):
    """Chemin d'une copie tmpfs (0600) de la valeur, pour common.sh qui
    l'efface à la sortie du script. Lancé par la console : la copie qu'elle
    a remise (jamais de connexion)."""
    st = ss.store()
    try:
        value = st.get(args.ref)
        p = ss.write_private_copy(ss.secret_files_dir(st.settings), "file", value)
    except (ss.SecretError, ss.SecretMissing, ValueError) as e:
        sys.stderr.write(f"harvester-secrets file: {e}\n")
        return EXIT_FAIL
    print(p)
    return EXIT_OK


def cmd_tf_encryption(args):
    """États OpenTofu chiffrés (point 270 B) : `status` dit si le serveur
    est vérifié (OpenBao, ou Vault 1.14 au plus) ; `ack-vault` acquitte un
    Vault plus récent, pour cette adresse et cette version majeure.mineure."""
    import tf_encryption as tfe
    s, doc = settings(), raw_settings()
    if not ss.openbao_active(s):
        return refuse("openbao-inactive", "OpenBao is not active")
    try:
        status = oc.Client(s["addr"], ca_pem=ca_of(s.get("ca_file")),
                           timeout=oc.clamp_timeout(s.get("timeout"))).seal_status()
    except oc.OpenBaoError as e:
        return fail(e.code, f"OpenBao at {s['addr']} does not answer")
    v = tfe.server_version(status)
    verdict = tfe.server_verdict(s.get("mode"), status)
    acked = tfe.ack_matches(s.get("tf_vault_ack"), s.get("addr"), status)
    info = {"mode": s.get("mode"), "addr": s.get("addr"), "version": f"{v[0]}.{v[1]}" if v else None,
            "warning": verdict, "acknowledged": acked, "encrypted_declarations": encrypted_states(),
            "entities_pending": list(s.get("tf_entities_pending") or [])}
    if args.what == "status":
        out(dict(info, ok=True))
        return EXIT_OK
    if verdict is None:
        return refuse("not-needed", "this server needs no acknowledgement", **info)
    if v is None:
        return refuse("tf-encryption-version-unknown", "the server version cannot be read: nothing to acknowledge")
    doc["tf_vault_ack"] = {"addr": s["addr"], "version": f"{v[0]}.{v[1]}", "at": int(time.time()),
                           "by": ARGS.actor or "cli"}
    save_settings(doc)
    step("ack", "done", f"OpenTofu state encryption accepted on {s['addr']} (version {v[0]}.{v[1]})")
    out(dict(info, ok=True, acknowledged=True))
    return EXIT_OK


TF_KEY_KEEP_DAYS = 30           # décision 278 : un état sauvegardé peut encore demander la clé


def _retire_tf_key(doc, decl, trial):
    """Clé transit d'une déclaration supprimée (décision 278) : gardée 30
    jours (un état dans une sauvegarde peut en avoir besoin), puis retirée
    par le balayage. Clé d'un essai (aucun état durable, revue I2 des
    tâches 9-10) : retirée tout de suite. Rend une ligne d'étape."""
    s = settings()
    key = pol.tf_key(s["console_id"], decl)
    retired = dict(doc.get("tf_keys_retired") or {})
    if trial:
        try:
            gone = admin_client(s=s).transit_delete(key)
        except oc.OpenBaoError as e:
            retired[decl] = {"at": int(time.time()), "trial": True, "error": e.code}
            doc["tf_keys_retired"] = retired
            save_settings(doc)
            return f"transit key {key} kept for the sweep ({e.code})"
        retired.pop(decl, None)
        doc["tf_keys_retired"] = retired
        save_settings(doc)
        return f"transit key {key} " + ("removed" if gone else "already absent")
    if decl not in retired:
        retired[decl] = {"at": int(time.time()), "trial": False}
        doc["tf_keys_retired"] = retired
        save_settings(doc)
    return f"transit key {key} kept {TF_KEY_KEEP_DAYS} days, then removed"


def cmd_tf_entity_remove(args):
    """Entité d'identité `tf-<décl>` d'une déclaration supprimée : retirée
    par l'agent en B1 ; en mode A, notée à retirer par l'administrateur
    (commandes `bao` rendues), `--done` la retire de la liste. Idempotent
    (déclaration jamais chiffrée : « already absent »). La clé transit de la
    déclaration suit la décision 278 (`--trial` : retirée tout de suite)."""
    try:
        alias = pol.tf_alias(args.decl)
    except ValueError as e:
        return refuse("bad-input", str(e))
    s, doc = settings(), raw_settings()
    if not ss.openbao_active(s):
        return refuse("openbao-inactive", "OpenBao is not active")
    pending = [d for d in (doc.get("tf_entities_pending") or []) if isinstance(d, str)]
    if args.done:
        doc["tf_entities_pending"] = [d for d in pending if d != args.decl]
        save_settings(doc)
        out({"ok": True, "pending": doc["tf_entities_pending"]})
        return EXIT_OK
    key_msg = _retire_tf_key(doc, args.decl, args.trial)
    step("transit-key", "done", key_msg)
    if s.get("mode") == "b1":
        st = agent_ok(run_agent({"op": "tf-entity-remove", "decl": args.decl}))
        out({"ok": True, "mode": "b1", "alias": alias, "agent": st.get("message"), "transit_key": key_msg})
        return EXIT_OK
    doc = raw_settings()
    if args.decl not in pending:
        doc["tf_entities_pending"] = pending + [args.decl]
        save_settings(doc)
    step("pending", "done", f"identity entity {alias} to be removed by the administrator of {s['addr']}")
    out({"ok": True, "mode": "a", "alias": alias, "pending": doc.get("tf_entities_pending", pending),
         "transit_key": key_msg, "commands": tf_entity_commands(args.decl)})
    return EXIT_OK


def tf_entity_commands(decl):
    alias = pol.tf_alias(decl)
    return [f"ACCESSOR=$(bao auth list -format=json | jq -r '.\"token/\".accessor')",
            f"ID=$(bao write -field=id identity/lookup/entity alias_name={alias} alias_mount_accessor=$ACCESSOR)",
            "bao delete identity/entity/id/$ID",
            f"harvester-secrets tf-entity-remove {decl} --done"]


def tf_keys_view(doc=None, now=None):
    """Clés transit gardées des déclarations supprimées, avec leur date de
    retrait (décision 278) ; jamais de valeur."""
    doc = raw_settings() if doc is None else doc
    now = time.time() if now is None else now
    cid = doc.get("console_id") or ""
    rows = []
    for decl, e in sorted((doc.get("tf_keys_retired") or {}).items()):
        if not isinstance(e, dict):
            continue
        at = int(e.get("at") or 0)
        due = at if e.get("trial") else at + TF_KEY_KEEP_DAYS * 86400
        try:
            key = pol.tf_key(cid, decl)
        except ValueError:
            continue
        rows.append({"decl": decl, "key": key, "retired_at": at, "delete_after": due,
                     "due": now >= due, "trial": bool(e.get("trial")), "error": e.get("error")})
    return rows


def cmd_tf_keys(args):
    """`list` : clés gardées et leur date de retrait ; `delete DECL --confirm
    DECL` : retrait avant le délai (l'administrateur tape l'identifiant) ;
    `sweep` : retrait des clés dont le délai est passé (tâche de fond)."""
    s, doc = settings(), raw_settings()
    if args.what == "list":
        out({"ok": True, "keep_days": TF_KEY_KEEP_DAYS, "keys": tf_keys_view(doc)})
        return EXIT_OK
    if not ss.openbao_active(s):
        return refuse("openbao-inactive", "OpenBao is not active")
    rows = tf_keys_view(doc)
    if args.what == "delete":
        if not args.decl or args.confirm != args.decl:
            return refuse("confirmation-required", "type the declaration identifier again (--confirm)")
        targets = [r for r in rows if r["decl"] == args.decl]
        if not targets:
            return refuse("tf-key-not-retired", "no kept transit key for this declaration")
    else:
        targets = [r for r in rows if r["due"]]
    c = admin_client(s=s) if targets else None
    done, failed = [], []
    for r in targets:
        try:
            gone = c.transit_delete(r["key"])
            done.append(r["decl"])
            step(f"key-{r['decl']}", "done", f"{r['key']} " + ("removed" if gone else "already absent"))
        except oc.OpenBaoError as e:
            failed.append(r["decl"])
            step(f"key-{r['decl']}", "error", f"{r['key']}: {e.code}")
    doc = raw_settings()
    kept = dict(doc.get("tf_keys_retired") or {})
    for d in done:
        kept.pop(d, None)
    doc["tf_keys_retired"] = kept
    save_settings(doc)
    out({"ok": not failed, "removed": done, "failed": failed})
    return EXIT_OK if not failed else EXIT_FAIL


# ---------------------------------------------------------------------------
def parser():
    p = argparse.ArgumentParser(prog="harvester-secrets", description="Console secret store (OpenBao).")
    p.add_argument("--id", help="action id of the console (X-Hops-Action)")
    p.add_argument("--actor", help="person of the console (X-Hops-Actor)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("assistant")
    s.add_argument("--addr", required=True)
    s.add_argument("--console-cidr", required=True)
    s.add_argument("--mount", default=pol.DEFAULT_MOUNT)
    s.add_argument("--prefix")
    s = sub.add_parser("check")
    s.add_argument("--mode", choices=("a", "b1"), required=True)
    for o in ("--addr", "--ca", "--mount", "--prefix"):
        s.add_argument(o)
    s.add_argument("--wrapped-stdin", action="store_true")
    s.add_argument("--transit", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--spec", help=argparse.SUPPRESS)       # console : jeton d'enveloppe et CA par fichier privé
    s = sub.add_parser("enable")
    s.add_argument("--mode", choices=("a", "b1"), required=True)
    for o in ("--addr", "--role-id", "--ca", "--mount", "--prefix", "--bundle"):
        s.add_argument(o)
    s.add_argument("--wrapped-stdin", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--spec", help=argparse.SUPPRESS)
    s = sub.add_parser("disable")
    s.add_argument("--remove-b1", action="store_true")
    s.add_argument("--leave-plan-secrets", action="store_true",
                   help="OpenBao unreachable: disable anyway, the secrets of stopped plan runs stay on the server")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("migrate")
    s.add_argument("--to", choices=("openbao", "local"), required=True)
    s.add_argument("--class", dest="cls", action="append")
    s.add_argument("--name")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--replace-existing", action="store_true",
                   help="with --name: replace another value already in OpenBao (kept as the previous version)")
    s = sub.add_parser("confirm")
    s.add_argument("--class", dest="cls", required=True)
    s.add_argument("--older-than-days", type=int, help="only originals kept longer (background task)")
    sub.add_parser("outage-test")
    s = sub.add_parser("versions")
    s.add_argument("ref")
    s = sub.add_parser("rollback")
    s.add_argument("ref")
    s.add_argument("--version", type=int, required=True)
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("rotate")
    s.add_argument("what", choices=("secret-id", "unseal-key", "transit", "ssh-kvm"))
    s.add_argument("target", nargs="?")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("offline-copy", help="offline copy of the B1 unseal key (one download, then ack)")
    s.add_argument("what", choices=("export", "done", "ack"))
    s.add_argument("--to", help="export: write the key to this new file (0600), then erase the download copy")
    s.add_argument("--key-id")
    s = sub.add_parser("snapshot")
    s.add_argument("--dest")
    s.add_argument("--keep", type=int, help="snapshots kept in the directory (default: snapshot_keep, 14)")
    s.add_argument("--dry-run", action="store_true")
    sub.add_parser("snapshots", help="snapshots of the set directory and of the agent, no value")
    s = sub.add_parser("settings", help="settings without secret, changed at once")
    s.add_argument("--set", action="append", metavar="KEY=VALUE")
    s = sub.add_parser("sweep-orphans", help="OpenBao secrets no local reference points to")
    s.add_argument("--class", dest="cls", action="append")
    s.add_argument("--min-age-days", type=int, default=7)
    s.add_argument("--tf-dir")
    s.add_argument("--repl-db")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("restore")
    s.add_argument("file")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("bundle")
    s.add_argument("--registry", default=ob.DEFAULT_REGISTRY)
    s.add_argument("--repository", default=ob.DEFAULT_REPOSITORY)
    s.add_argument("--user", required=True)
    s.add_argument("--password-stdin", action="store_true")
    s.add_argument("--tag", default=ob.DEFAULT_TAG)
    s.add_argument("--cosign-key")
    s.add_argument("-o", required=True)
    s.add_argument("--plain-http", action="store_true", help=argparse.SUPPRESS)
    s = sub.add_parser("file")
    s.add_argument("ref")
    s = sub.add_parser("tf-encryption", help="OpenTofu state encryption: server check, Vault acknowledgement")
    s.add_argument("what", choices=("status", "ack-vault"))
    s = sub.add_parser("tf-entity-remove", help="identity entity of a removed encrypted declaration")
    s.add_argument("decl")
    s.add_argument("--done", action="store_true", help="mode a: the administrator removed it")
    s.add_argument("--trial", action="store_true", help="declaration of a dry run: its transit key goes now")
    s = sub.add_parser("tf-keys", help="transit keys of removed declarations (kept 30 days)")
    s.add_argument("what", choices=("list", "delete", "sweep"))
    s.add_argument("decl", nargs="?")
    s.add_argument("--confirm", help="delete: the declaration identifier typed again")
    return p


COMMANDS = {"status": cmd_status, "assistant": cmd_assistant, "check": cmd_check, "enable": cmd_enable,
            "disable": cmd_disable, "migrate": cmd_migrate, "confirm": cmd_confirm, "versions": cmd_versions,
            "rollback": cmd_rollback, "outage-test": cmd_outage_test, "rotate": cmd_rotate, "snapshot": cmd_snapshot, "restore": cmd_restore,
            "bundle": cmd_bundle, "file": cmd_file, "offline-copy": cmd_offline_copy,
            "tf-encryption": cmd_tf_encryption, "tf-entity-remove": cmd_tf_entity_remove,
            "tf-keys": cmd_tf_keys, "snapshots": cmd_snapshots, "settings": cmd_settings,
            "sweep-orphans": cmd_sweep_orphans}


def _load_spec(path):
    """Fichier de spécification de `_cli_action` (0600, même propriétaire, pas
    de lien) : le jeton d'enveloppe et la CA tapés dans l'interface ne passent
    jamais par la ligne de commande."""
    SPEC.clear()
    if not path:
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if st.st_mode & 0o077 or st.st_uid != os.geteuid():
            raise CliError("bad-input", "the spec file must be private (0600, same owner)", EXIT_REFUSED)
        doc = json.loads(f.read(1 << 20) or "{}")
    if isinstance(doc, dict):
        SPEC.update({k: v for k, v in doc.items() if k in ("wrap_token", "ca_pem") and isinstance(v, str)})


def main(argv=None):
    args = parser().parse_args(argv)
    ARGS.id, ARGS.actor = args.id, args.actor
    try:
        _load_spec(getattr(args, "spec", None))
    except (OSError, ValueError, CliError) as e:
        return refuse("bad-input", f"spec file unreadable ({type(e).__name__})")
    for k in [k for k in os.environ if k.startswith(("VAULT_", "BAO_"))]:
        os.environ.pop(k, None)
    if _is_root() and AGENT_CHANNEL is None:
        try:
            return _main_as_root(args)
        except CliError as e:
            return (refuse if e.exit_code == EXIT_REFUSED else fail)(e.code, str(e))
    return _dispatch(args)


def _dispatch(args):
    try:
        return COMMANDS[args.cmd](args)
    except CliError as e:
        return (refuse if e.exit_code == EXIT_REFUSED else fail)(e.code, str(e))
    except ss.SecretError as e:
        return fail(e.code, str(e))
    except oc.OpenBaoError as e:
        return fail(e.code, e.detail[:300])


if __name__ == "__main__":
    sys.exit(main())
