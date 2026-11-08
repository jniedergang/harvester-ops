#!/usr/bin/env python3
"""Magasin de secrets de la console : OpenBao (2.9.0, tâche 7).

    harvester-secrets status [--json]
    harvester-secrets assistant --addr URL --console-cidr CIDR [--mount M] [--prefix P]
    harvester-secrets check --mode a|b1 [--addr URL --ca FILE --mount M --prefix P]
                            [--wrapped-stdin] [--transit] [--dry-run]
    harvester-secrets enable --mode a --addr URL --role-id ID --wrapped-stdin [--ca FILE]
                             [--mount M] [--prefix P] [--dry-run]
    harvester-secrets enable --mode b1 --bundle FILE --ack-offline-copy [--dry-run]
    harvester-secrets disable [--remove-b1] [--dry-run]
    harvester-secrets migrate --to openbao|local [--class C ...] [--name N] [--dry-run]
    harvester-secrets confirm --class C
    harvester-secrets versions REF
    harvester-secrets rollback REF --version N [--dry-run]
    harvester-secrets rotate secret-id | unseal-key --ack-offline-copy | transit DECL | ssh-kvm HOST
    harvester-secrets snapshot [--dest DIR] [--dry-run]
    harvester-secrets restore FILE [--dry-run]
    harvester-secrets bundle --registry R --user U --password-stdin [--repository P] [--tag T]
                             [--cosign-key FILE] -o DIR|FILE
    harvester-secrets file REF

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
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
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
    p = state() / ss.SETTINGS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


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
    """Une opération de l'agent ; rend son état final. En root, l'outil la
    joue lui-même (même code, même demande) ; sinon il dépose la demande
    (refus si une autre attend) et suit status.json."""
    timeout = timeout or AGENT_TIMEOUT.get(req["op"], 600)
    req = dict(req, id=secrets.token_hex(8), requested_by=ARGS.actor or os.environ.get("USER") or "cli")
    d = state() / "secrets-agent"
    rp = d / "request.json"
    in_process = _is_root()
    if not in_process and not (d / "agent.json").is_file():
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
    if in_process:
        _agent_module().Agent().process()
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
    return args.addr or s["addr"], (Path(args.ca).read_text() if args.ca else ca_of()), args.mount or s["mount"]


def cmd_check(args):
    s = settings()
    doc = raw_settings()
    cid = ensure_console_id(doc)
    addr, ca, mount = _mode_target(args, s)
    prefix = args.prefix or s["prefix"] or f"consoles/{cid}"
    wrap = sys.stdin.readline().strip() if args.wrapped_stdin else None
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
    if not args.addr or not args.role_id or not args.wrapped_stdin:
        return refuse("bad-input", "mode a needs --addr, --role-id and --wrapped-stdin")
    wrap = sys.stdin.readline().strip()
    if not wrap:
        return refuse("bad-input", "no wrapping token on stdin")
    ca = Path(args.ca).read_text() if args.ca else None
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


def _enable_b1(args, doc):
    if not args.bundle:
        return refuse("bad-input", "mode b1 needs --bundle")
    if not args.ack_offline_copy:
        return refuse("offline-copy-ack-required", "keep an offline copy of the unseal key "
                      "(/etc/harvester-ops-openbao/unseal.key) once installed, then pass --ack-offline-copy")
    src = Path(args.bundle)
    step("bundle", "running", "checking the OpenBao bundle")
    with tempfile.TemporaryDirectory(prefix="openbao-bundle-") as tmp:
        try:
            info = ob.verify(src, tmp)
        except ob.BundleError as e:
            return refuse(e.code, str(e))
    step("bundle", "done", f"OpenBao {info['version']} ({info['tag']}, {info['index_digest'][:19]})")
    if args.dry_run:
        out({"ok": True, "dry_run": True, "version": info["version"], "tag": info["tag"],
             "would": ["stage the bundle", "install OpenBao B1 (agent)", "run every check", "activate mode b1"]})
        return EXIT_OK
    cid = ensure_console_id(doc)
    staged = state() / "secrets-agent" / "staged"
    staged.mkdir(parents=True, exist_ok=True)
    if src.resolve().parent != staged.resolve():
        shutil.copyfile(src, staged / src.name)
    agent_ok(run_agent({"op": "b1-install", "console_id": cid, "bundle": src.name, "ack_offline_copy": True}))
    prefix = f"consoles/{cid}"
    res = run_checks(pol.B1_ADDR, ca_of(), pol.DEFAULT_MOUNT, prefix, cid, b1=True, transit=True)
    if not all_green(res):
        out({"ok": False, "code": "check-failed", "checks": res, "note": "B1 installed, OpenBao NOT active"})
        return EXIT_FAIL
    doc.update(mode="b1", addr=pol.B1_ADDR, mount=pol.DEFAULT_MOUNT, prefix=prefix)
    save_settings(doc)
    out({"ok": True, "mode": "b1", "addr": pol.B1_ADDR, "console_id": cid, "version": info["version"],
         "checks": res})
    return EXIT_OK


def cmd_disable(args):
    doc = raw_settings()
    migrated = sorted((doc.get("migrated") or {}))
    if migrated:
        return refuse("classes-still-migrated", "bring these classes back first (migrate --to local): "
                      + ", ".join(migrated), classes=migrated)
    was = doc.get("mode") or "local"
    if args.remove_b1 and was != "b1":
        return refuse("bad-input", "--remove-b1 only in mode b1")
    if args.dry_run:
        out({"ok": True, "dry_run": True, "mode": was, "would": ["back to local files"]
             + (["remove OpenBao B1 (agent, data kept aside)"] if args.remove_b1 else [])})
        return EXIT_OK
    doc.update(mode="local", addr=None)
    save_settings(doc)
    step("disable", "done", "local files")
    if args.remove_b1:
        agent_ok(run_agent({"op": "b1-remove"}))
    out({"ok": True, "mode": "local", "was": was})
    return EXIT_OK


def _sha(v):
    return hashlib.sha256(v.encode()).hexdigest()


def migrate_class(bao, fs, cls, name=None, dry_run=False, actor=None, action=None):
    """Migration d'une classe vers OpenBao (plan, « Migration ») : pour
    chaque secret, valeur déjà là identique -> reprise sans écriture ;
    différente -> conflit, local intact ; sinon écriture (cas), relecture
    hors cache, SHA-256 comparés, PUIS la valeur locale devient la
    référence (l'adaptateur garde l'original). Rend (faits, erreurs)."""
    ad = sc.adapter(cls)
    done, errors = [], []
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
        data, _m = bao._read(cls, path, actor, action)
        if data and field in data:
            if _sha(data[field]) != _sha(value):
                errors.append({"secret": label, "code": "openbao-cas-conflict"})
                step(f"migrate-{cls}", "error", f"{label}: another value already in OpenBao, local kept")
                continue
            resumed = True
        else:
            resumed = False
        if dry_run:
            done.append({"secret": label, "would": "resume" if resumed else "write"})
            continue
        if not resumed:
            ref = bao.put(cls, path, field, value, actor=actor, action=action)
        else:
            ref = ss.make_ref(cls, path, field, "openbao")
        with bao._lock:
            for r in [r for r in bao._cache if r.startswith(f"openbao:{cls}/{path}#")]:
                del bao._cache[r]
        back, _m = bao._read(cls, path, actor, action)
        if not back or _sha(back.get(field, "")) != _sha(value):
            errors.append({"secret": label, "code": "migrate-readback-mismatch"})
            step(f"migrate-{cls}", "error", f"{label}: read back differs, local kept, class stopped")
            break
        try:
            ad.write_ref(fs, path, field, ref)
        except NotImplementedError:
            errors.append({"secret": label, "code": "class-adapter-pending"})
            step(f"migrate-{cls}", "error", f"{label}: written in OpenBao, local reference not supported yet")
            break
        done.append({"secret": label, "resumed": resumed})
        step(f"migrate-{cls}", "running", f"{label}: {'already there' if resumed else 'migrated'}")
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
        value = bao.get(ref, actor)
        try:
            ad.restore_local(fs, path, field, value)
        except NotImplementedError:
            errors.append({"secret": ref, "code": "class-adapter-pending"})
            break
        if ad.read_local(fs, path, field) != value:
            errors.append({"secret": ref, "code": "restore-readback-mismatch"})
            break
        bao.delete(ref, actor=actor, action=action)
        done.append({"secret": ref})
    return done, errors


def cmd_migrate(args):
    s = settings()
    classes = args.cls or [c for c in sc.MIGRATION_ORDER if c != "plan-run"]
    for c in classes:
        try:
            sc.require_migratable(c)
        except (sc.NotMigratable, ValueError):
            return refuse("class-not-migratable", f"{c} cannot move to OpenBao")
    bao = admin_store(s)
    fs = ss.FileStore(state())
    report, all_errors = {}, []
    doc = raw_settings()
    for c in classes:
        fn = migrate_class if args.to == "openbao" else restore_class
        done, errors = fn(bao, fs, c, args.name, args.dry_run, ARGS.actor, ARGS.id)
        report[c] = {"done": done, "errors": errors}
        all_errors += errors
        if not args.dry_run and not errors and not args.name:
            mig = doc.setdefault("migrated", {})
            if args.to == "openbao":
                mig[c] = {"at": int(time.time()), "by": ARGS.actor or "cli"}
            else:
                mig.pop(c, None)
            save_settings(doc)
    out({"ok": not all_errors, "to": args.to, "dry_run": args.dry_run, "classes": report})
    return EXIT_OK if not all_errors else EXIT_FAIL


def cmd_confirm(args):
    doc = raw_settings()
    if args.cls not in (doc.get("migrated") or {}):
        return refuse("class-not-migrated", f"{args.cls} is not migrated")
    confirm = getattr(sc, "confirm_class", None)
    if confirm is None:
        return fail("class-adapter-pending", "erasing the originals of this class is not supported yet")
    n = confirm(args.cls, ss.FileStore(state()))
    doc["migrated"][args.cls]["confirmed"] = int(time.time())
    save_settings(doc)
    out({"ok": True, "class": args.cls, "originals_erased": n})
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


def cmd_rotate(args):
    s = settings()
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
        if not args.ack_offline_copy:
            return refuse("offline-copy-ack-required", "keep an offline copy of the new key, then --ack-offline-copy")
        if args.dry_run:
            out({"ok": True, "dry_run": True, "would": "new key, restart, sys/rotate/root, snapshot, old key retired"})
            return EXIT_OK
        st = agent_ok(run_agent({"op": "rotate-unseal-key", "ack_offline_copy": True}))
        out({"ok": True, "rotated": "unseal-key", "message": st.get("message")})
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
    return refuse("not-available", "KVM host key rotation is not available in this build")


def cmd_snapshot(args):
    s = settings()
    if s["mode"] != "b1" or not ss.openbao_active(s):
        return refuse("b1-only", "snapshots are taken in mode B1 (in mode A the server belongs to you)")
    dest = Path(args.dest or raw_settings().get("snapshot_dir") or "")
    if not dest.is_absolute() or not dest.is_dir():
        return refuse("bad-input", "give --dest (an existing absolute directory) or set snapshot_dir")
    try:
        dest.resolve().relative_to(state().resolve())
        return refuse("snapshot-in-state-dir", "never in the state directory (it is backed up with the console)")
    except ValueError:
        pass
    name = f"openbao-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.snap"
    if args.dry_run:
        out({"ok": True, "dry_run": True, "file": name})
        return EXIT_OK
    n = admin_client(s=s).raft_snapshot(str(dest / name))
    out({"ok": True, "file": name, "bytes": n})
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
    key = Path(args.cosign_key) if args.cosign_key else COSIGN_KEY
    cos = _cosign_runner(key, args.registry, args.user, password) if key.is_file() else None
    src = op.Registry(args.registry, {"username": args.user, "password": password}, plain_http=args.plain_http)
    step("pull", "running", f"{args.registry}/{args.repository}:{args.tag}")
    try:
        got = ob.build(src, args.registry, args.repository, args.tag, (HERE.parent / "config" / "openbao" /
                                                                       "openbao.hcl.tpl").read_text(),
                       out_dir, cosign=cos)
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
    s = sub.add_parser("enable")
    s.add_argument("--mode", choices=("a", "b1"), required=True)
    for o in ("--addr", "--role-id", "--ca", "--mount", "--prefix", "--bundle"):
        s.add_argument(o)
    s.add_argument("--wrapped-stdin", action="store_true")
    s.add_argument("--ack-offline-copy", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("disable")
    s.add_argument("--remove-b1", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("migrate")
    s.add_argument("--to", choices=("openbao", "local"), required=True)
    s.add_argument("--class", dest="cls", action="append")
    s.add_argument("--name")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("confirm")
    s.add_argument("--class", dest="cls", required=True)
    s = sub.add_parser("versions")
    s.add_argument("ref")
    s = sub.add_parser("rollback")
    s.add_argument("ref")
    s.add_argument("--version", type=int, required=True)
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("rotate")
    s.add_argument("what", choices=("secret-id", "unseal-key", "transit", "ssh-kvm"))
    s.add_argument("target", nargs="?")
    s.add_argument("--ack-offline-copy", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("snapshot")
    s.add_argument("--dest")
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
    return p


COMMANDS = {"status": cmd_status, "assistant": cmd_assistant, "check": cmd_check, "enable": cmd_enable,
            "disable": cmd_disable, "migrate": cmd_migrate, "confirm": cmd_confirm, "versions": cmd_versions,
            "rollback": cmd_rollback, "rotate": cmd_rotate, "snapshot": cmd_snapshot, "restore": cmd_restore,
            "bundle": cmd_bundle, "file": cmd_file}


def main(argv=None):
    args = parser().parse_args(argv)
    ARGS.id, ARGS.actor = args.id, args.actor
    for k in [k for k in os.environ if k.startswith(("VAULT_", "BAO_"))]:
        os.environ.pop(k, None)
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
