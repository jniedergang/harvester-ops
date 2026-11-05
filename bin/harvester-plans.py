#!/usr/bin/env python3
"""harvester-plans : plans de déploiement de la console en ligne de commande (2.4.0).

  harvester-plans [--state-dir D] [--json] plan list | show NOM | save NOM --file plan.yaml [--revision N] | delete NOM
  harvester-plans [--state-dir D] [--json] template list | show NOM | save NOM --file m.yaml [--revision N] | delete NOM
  harvester-plans [--state-dir D] [--json] capi-template list | show NOM | save NOM --file m.yaml [--revision N]
                  | delete NOM
  harvester-plans [--state-dir D] [--json] check  PLAN --cluster C [--vars vars.yaml]
  harvester-plans [--state-dir D] [--json] apply  PLAN --cluster C [--vars vars.yaml] [--dry-run]
                  [--update-declarations]
  harvester-plans [--state-dir D] [--json] resume ACTION_ID [--secrets-file s.yaml | --secrets-stdin]
                  [--confirm-system HOST=NAME]...
  harvester-plans [--state-dir D] [--json] install PLAN --profile P --nodes nodes.csv --cluster-name C --vip V
                  [--iso I] [--vars vars.yaml] [--concurrency N] [--confirm-system HOST=NAME]...
                  (--secrets-file s.yaml | --secrets-stdin)

`apply`, `resume` et `install` déroulent la même chaîne que la console
(bin/lib/deploy_chain.py, branchée par web/app.py chargé dans ce
processus) : disponibilité, contenu Terraform, pile Cluster API et clusters
enfants (2.5.0, sans option nouvelle : `check` en montre les constats,
capacité des N clusters comprise), et pour `install` la série
d'installations d'un profil d'abord (même garde que `harvester-baremetal
profile apply` pour une machine qui porte déjà un système). L'action est
enregistrée dans l'historique du répertoire d'état.

Les secrets (`resume` et `install` seulement : un plan appliqué à un cluster
existant n'en demande aucun) viennent d'un fichier privé (0600) ou de
l'entrée standard, en YAML : `token`, `password` (OS, facultatif),
`bmc_password` (commun), `bmc_passwords` (par hôte de BMC). Jamais de la
ligne de commande. `--vars` : YAML des valeurs des variables du plan.

Étapes en `STEP_EVENT|étape|état|message` sur la sortie d'erreur, résultat
JSON sur la sortie standard. `--json` : un document JSON sur la sortie
standard à CHAQUE sortie, refus compris ({"error", "code", "errors"}).
Codes : 0 fait, 1 échec, 2 usage ou refus, 3 annulé.
"""

import argparse
import getpass
import json
import os
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))

import bm_profiles  # noqa: E402
import cluster_decl  # noqa: E402
import deploy_plans  # noqa: E402
import web_loader  # noqa: E402

EXIT_DONE, EXIT_FAILED, EXIT_USAGE, EXIT_CANCELLED = 0, 1, 2, 3


class Refused(Exception):
    """Refus avant tout lancement : code de sortie 2."""

    def __init__(self, error, code="refused", errors=None, **extra):
        super().__init__(error)
        self.body = {"error": error, "code": code, **({"errors": errors} if errors else {}), **extra}


def _step(sid, status, msg=""):
    print(f"STEP_EVENT|{sid}|{status}|{msg}", file=sys.stderr, flush=True)


def _out(doc):
    print(json.dumps(doc, indent=2, default=str))


def _state(args):
    if args.state_dir:
        os.environ["HARVESTER_OPS_STATE_DIR"] = os.path.abspath(args.state_dir)
    return cluster_decl.state_dir()


def _read_yaml(path, what):
    import yaml
    try:
        doc = yaml.safe_load(Path(path).read_text())
    except OSError as e:
        raise Refused(f"{what}: {e.strerror or e}", "unreadable") from None
    except yaml.YAMLError:
        raise Refused(f"{what}: invalid YAML", "invalid-yaml") from None
    return doc


def _vars(args):
    if not getattr(args, "vars", None):
        return {}
    doc = _read_yaml(args.vars, "--vars") or {}
    if not isinstance(doc, dict):
        raise Refused("--vars: expected a mapping of variable values", "invalid-vars")
    out = {}
    for k, v in doc.items():
        # revue m6 : comme la console, une valeur est une chaîne (un nombre
        # ou un booléen YAML se lit comme son texte), jamais None, liste ou dict
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (int, float)):
            v = str(v)
        elif not isinstance(v, str):
            raise Refused(f"--vars: {k}: not a string", "invalid-vars")
        out[str(k)] = v
    return out


def _secrets(args):
    if not (args.secrets_file or args.secrets_stdin):
        return {}
    try:
        return web_loader.read_secrets(None if args.secrets_stdin else args.secrets_file,
                                       sys.stdin if args.secrets_stdin else None)
    except web_loader.SecretsError as e:
        raise Refused(str(e).replace("the secrets file", "--secrets-file"), "secrets") from None
    except OSError as e:
        raise Refused(f"--secrets-file: {e.strerror or e}", "secrets") from None


def _confirms(args):
    out = {}
    for item in args.confirm_system or []:
        host, sep, typed = item.partition("=")
        if not sep or not host.strip():
            raise Refused(f"--confirm-system {item}: expected HOST=NAME", "usage")
        out[host.strip()] = typed
    return out


# --- plans et modèles : sans console -------------------------------------------

def _errors(e):
    return [[w, r] for w, r in e.errors]


def _save_tf_template(state, name, doc, revision):
    return deploy_plans.save_template(state, name, doc, revision, by=f"cli:{getpass.getuser()}")[0]


# genre -> (nom affiché, lister, lire, écrire, supprimer)
STORES = {
    "plan": ("plan", deploy_plans.list_plans, deploy_plans.load_plan,
             deploy_plans.save_plan, deploy_plans.delete_plan),
    "template": ("template", deploy_plans.list_templates, deploy_plans.load_template,
                 _save_tf_template, deploy_plans.delete_template),
    # 2.5.0 : modèles de cluster (Cluster API)
    "capi-template": ("cluster template", deploy_plans.list_capi_templates,
                      deploy_plans.load_capi_template,
                      lambda st, n, d, r: deploy_plans.save_capi_template(st, n, d, r,
                                                                          by=f"cli:{getpass.getuser()}"),
                      deploy_plans.delete_capi_template),
}


def cmd_store(args, state):
    what, lister, loader, saver, deleter = STORES[args.cmd]
    code = what.replace(" ", "-")
    if args.scmd == "list":
        _out({f"{code.replace('-', '_')}s": lister(state)})
        return EXIT_DONE
    if not deploy_plans.NAME_RE.match(args.name):
        raise Refused(f"invalid {what} name (RFC 1123): {args.name}", "invalid-name")
    if args.scmd == "show":
        obj = loader(state, args.name)
        if obj is None:
            raise Refused(f"no such {what}: {args.name}", "not-found")
        _out(obj)
        return EXIT_DONE
    if args.scmd == "delete":
        used = deploy_plans.plans_using_capi_template(state, args.name) if args.cmd == "capi-template" else []
        if used:
            raise Refused(f"saved plans use this {what}", "template-in-use", plans=used)
        if not deleter(state, args.name):
            raise Refused(f"no such {what}: {args.name}", "not-found")
        _out({"deleted": args.name})
        return EXIT_DONE
    doc = _read_yaml(args.file, "--file")
    if isinstance(doc, dict):
        doc.pop("revision", None)
    try:
        out = saver(state, args.name, doc, args.revision)
    except deploy_plans.PlanConflict as e:
        raise Refused(f"the {what} changed in the meantime", "conflict",
                      current_revision=e.current_revision) from None
    except deploy_plans.ProfileError as e:
        raise Refused(f"invalid {what}", f"invalid-{code}", _errors(e)) from None
    _out(out)
    return EXIT_DONE


# --- chaîne : console chargée ---------------------------------------------------

def _console(state):
    web_loader.prepare_console_env(state)
    app = web_loader.load("app")
    if app is None:
        raise Refused("web/app.py not found (set HARVESTER_OPS_WEB_DIR)", "no-console")
    return app


def _cluster_kc(app, cluster):
    kc = app._deploy_console_kc(cluster)
    if not kc:
        raise Refused(f"unknown cluster: {cluster}", "unknown-cluster")
    return kc


def _follow(app, action_id):
    """Suit l'action jusqu'à sa fin (étapes sur la sortie d'erreur) ; SIGINT
    et SIGTERM demandent l'arrêt. Rend le code de sortie."""
    with app.ACTIONS_LOCK:
        run = app.ACTIONS[action_id]
    stop = lambda *a: setattr(run, "_cancel", True)      # noqa: E731
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    seq = 0
    while True:
        events, seq = run.events_since(seq)
        for ev in events:
            if ev.get("type") == "step":
                _step(ev.get("step_id"), ev.get("status"), ev.get("message", ""))
        if run.status in ("done", "error", "cancelled") and not run.events_since(seq)[0]:
            break
        time.sleep(0.5)
    _out({"action_id": action_id, "status": run.status, "exit_code": run.exit_code,
          "result": run.result})
    return {"done": EXIT_DONE, "cancelled": EXIT_CANCELLED}.get(run.status, EXIT_FAILED)


def _launched(app, body, status):
    if status != 202:
        raise Refused(body.get("error") or "refused", body.get("code") or "refused",
                      body.get("errors"), **{k: v for k, v in body.items()
                                             if k not in ("error", "code", "errors")})
    return _follow(app, body["action_id"])


def cmd_check(args, state):
    app = _console(state)
    kc = _cluster_kc(app, args.cluster)
    findings, summary, _ctx = app._deploy_prepare(args.cluster, args.plan, _vars(args), kc=kc,
                                                  update_differing=bool(args.update_declarations))
    if findings is None:
        raise Refused(f"no such plan: {args.plan}", "not-found")
    blocking = bool(app._deploy_blocking(findings))
    for f in findings:
        print(f"{f['level']}: {f['code']} ({f['phase']}) {f['detail']}", file=sys.stderr)
    _out({"findings": findings, "summary": summary, "blocking": blocking})
    return EXIT_USAGE if blocking else EXIT_DONE


def cmd_apply(args, state):
    app = _console(state)
    kc = _cluster_kc(app, args.cluster)
    body, status = app._deploy_launch_checked(args.cluster, kc, args.plan, _vars(args), bool(args.dry_run),
                                              getpass.getuser(), None,
                                              update_differing=bool(args.update_declarations))
    return _launched(app, body, status)


def cmd_resume(args, state):
    app = _console(state)
    prev, err = app._deploy_previous(args.action_id)
    if err:
        raise Refused(err[0]["error"], err[0].get("code") or "refused")
    if prev["status"] in ("starting", "running"):
        raise Refused("this run is still in progress", "deploy-running")
    if prev["dry_run"]:
        raise Refused("a dry run is not resumed: start it again", "dry-run")
    cluster = prev["action"][len(app.DEPLOY_PREFIX):]
    result = prev["result"] or {}
    install, store, run_id, kc, vip = None, None, None, None, None
    if result.get("install"):
        install, ierr = app._deploy_resume_install(result, cluster, _secrets(args), _confirms(args))
        if ierr:
            body = ierr[0]
            for m in body.get("machines") or []:
                if not m.get("confirmed"):
                    print(f"row {m['row']} {m['bmc_host']}: a system seems present; to resume, add "
                          f"--confirm-system {m['bmc_host']}={m['confirm_name']}", file=sys.stderr)
            raise Refused(body["error"], body.get("code") or "refused", body.get("errors"))
        store = app._sst.store()
        run_id = app.uuid.uuid4().hex[:12]
        app._deploy_install_secrets_put(store, run_id, install["rows"])
        vip = ((result.get("install") or {}).get("batch") or {}).get("vip") or ""
    else:
        kc = _cluster_kc(app, cluster)
    body, status = app._deploy_launch_checked(
        cluster, kc, result.get("plan") or "", result.get("variables") or {}, False, getpass.getuser(), None,
        previous=result, resumes=args.action_id, install=install, vip=vip, run_id=run_id, store=store,
        required_role="admin" if install else "operator",
        update_differing=result.get("update_differing") is True)
    if status != 202 and store is not None:
        store.delete_tree("plan-run", run_id)
    return _launched(app, body, status)


def cmd_install(args, state):
    if not (args.secrets_file or args.secrets_stdin):
        raise Refused("install needs --secrets-file or --secrets-stdin", "usage")
    prof = bm_profiles.load_profile(state, args.profile)
    if prof is None:
        raise Refused(f"no such profile: {args.profile}", "not-found")
    if deploy_plans.load_plan(state, args.plan) is None:
        raise Refused(f"no such plan: {args.plan}", "not-found")
    try:
        rows = bm_profiles.parse_nodes_csv(Path(args.nodes).read_text(), prof["variables"])
    except bm_profiles.ProfileError as e:
        raise Refused("invalid nodes CSV", "invalid-nodes", _errors(e)) from None
    except OSError as e:
        raise Refused(f"--nodes: {e.strerror or e}", "unreadable") from None
    if any("bmc_password" in r for r in rows):
        raise Refused("the nodes CSV must not hold passwords: use bmc_passwords in the secrets", "invalid-nodes")
    sec = _secrets(args)
    per_host = sec.get("bmc_passwords") or {}
    for r in rows:
        if per_host.get(r.get("bmc_host")):
            r["bmc_password"] = str(per_host[r["bmc_host"]])
    confirms = _confirms(args)
    vars_ = _vars(args)
    app = _console(state)
    batch, secrets_ = app._bm_batch_input({
        "cluster_name": args.cluster_name, "vip": args.vip, "iso": args.iso, "rows": rows,
        "token": sec.get("token"), "password": sec.get("password"), "bmc_password": sec.get("bmc_password")})
    plan, errors, machines = app._bm_batch_prepare(prof, batch, secrets_, confirms)
    if errors:
        raise Refused("invalid batch", "invalid-batch", [[w, r] for w, r in errors])
    if machines:
        for m in machines:
            line = f"row {m['row']} {m['bmc_host']}: {m['state']}"
            if not m.get("confirmed"):
                line += (f", a system seems present; to install, add "
                         f"--confirm-system {m['bmc_host']}={m['confirm_name']}")
            print(line, file=sys.stderr)
        raise Refused("a system seems present on some machines (nothing was started)", "system-present",
                      refused=[m["bmc_host"] for m in machines if not m.get("confirmed")])
    record = app._deploy_install_record(args.profile, batch, [], args.concurrency)
    body, status = app._deploy_with_install(args.plan, vars_, batch["cluster_name"], batch["vip"], plan,
                                            "batch", record, args.concurrency, user=getpass.getuser())
    return _launched(app, body, status)


def build_parser():
    p = argparse.ArgumentParser(prog="harvester-plans", allow_abbrev=False,
                                description="Deployment plans of harvester-ops.")
    p.add_argument("--state-dir", help="console state directory (default: the console one)")
    p.add_argument("--json", action="store_true", help="one JSON document on stdout at every exit")
    sub = p.add_subparsers(dest="cmd", required=True)
    for what in ("plan", "template", "capi-template"):
        sp = sub.add_parser(what, allow_abbrev=False)
        ss = sp.add_subparsers(dest="scmd", required=True)
        ss.add_parser("list", allow_abbrev=False)
        for verb in ("show", "delete"):
            ss.add_parser(verb, allow_abbrev=False).add_argument("name")
        sv = ss.add_parser("save", allow_abbrev=False)
        sv.add_argument("name")
        sv.add_argument("--file", required=True)
        sv.add_argument("--revision", type=int, help="revision read before editing (absent: creation)")
    for verb in ("check", "apply"):
        sp = sub.add_parser(verb, allow_abbrev=False)
        sp.add_argument("plan")
        sp.add_argument("--cluster", required=True)
        sp.add_argument("--vars", help="YAML file of the plan variable values")
        sp.add_argument("--update-declarations", action="store_true",
                        help="update an existing declaration of the plan whose values differ")
        if verb == "apply":
            sp.add_argument("--dry-run", action="store_true", help="plan only; trial declarations removed")

    def secrets_args(sp):
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--secrets-file", help="YAML secrets file, mode 0600")
        g.add_argument("--secrets-stdin", action="store_true", help="read the YAML secrets on stdin")
        sp.add_argument("--confirm-system", action="append", metavar="HOST=NAME",
                        help="typed name of a machine that already holds a system")
    sp = sub.add_parser("resume", allow_abbrev=False)
    sp.add_argument("action_id")
    secrets_args(sp)
    sp = sub.add_parser("install", allow_abbrev=False)
    sp.add_argument("plan")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--nodes", required=True, help="CSV of the nodes (no password)")
    sp.add_argument("--cluster-name", required=True)
    sp.add_argument("--vip", required=True)
    sp.add_argument("--iso")
    sp.add_argument("--vars", help="YAML file of the plan variable values")
    sp.add_argument("--concurrency", type=int, default=bm_profiles.DEFAULT_CONCURRENCY)
    secrets_args(sp)
    return p


COMMANDS = {"plan": cmd_store, "template": cmd_store, "capi-template": cmd_store,
            "check": cmd_check, "apply": cmd_apply,
            "resume": cmd_resume, "install": cmd_install}


def main(argv=None):
    parser = build_parser()
    as_json = "--json" in (sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        if as_json and e.code:
            _out({"error": "invalid arguments", "code": "usage"})
        return EXIT_USAGE if e.code else EXIT_DONE
    try:
        return COMMANDS[args.cmd](args, _state(args))
    except Refused as e:
        print(e.body["error"], file=sys.stderr)
        for w, r in e.body.get("errors") or []:
            print(f"  {w}: {r}", file=sys.stderr)
        for f in e.body.get("findings") or []:
            print(f"  {f['level']}: {f['code']} {f['detail']}", file=sys.stderr)
        _out(e.body)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
