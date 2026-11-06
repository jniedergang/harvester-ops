#!/usr/bin/env python3
"""Registres des clusters enfants : déclarer, rattacher, vérifier.

    harvester-registry declare NOM --file r.yaml|- [--revision N]
    harvester-registry attach  NOM --cluster C [--kubeconfig K]     (identifiants JSON sur l'entrée standard)
    harvester-registry check   NOM --cluster C [--kubeconfig K]
    harvester-registry list
    harvester-registry show    NOM
    harvester-registry forget  NOM

Une déclaration (bin/lib/registries.py) ne porte aucun secret : adresse,
CA publique, usage (images, charts), registres d'origine miroités. Les
identifiants sont saisis au rattachement, sur l'entrée standard
(`{"push": {"username", "password"}, "pull": {...}}`), jamais en argument,
et posés en Secrets dans `harvester-ops-registry` du cluster Harvester.

Progression sur stderr au format STEP_EVENT|<étape>|<statut>|<message> ;
résultat en JSON sur stdout. Codes : 0 succès, 1 échec, 2 refus.

Voir docs/design/2026-11-05-artefacts-registre-260-plan.md (tâches 6 et 7).
"""

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import capi_cluster as cc  # noqa: E402
import cluster_decl  # noqa: E402
import deploy_plans as dp  # noqa: E402
import registries as rg  # noqa: E402
from kube import Kube, KubeError, cluster_config  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2


def step(sid, status, msg=""):
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def out(obj):
    print(json.dumps(obj, indent=2))


def state():
    return cluster_decl.state_dir()


def kube_from(args, need_entry=False):
    """`--kubeconfig` l'emporte (la console passe celui qu'elle a préparé,
    à l'identité de la personne) ; l'entrée de configuration de `--cluster`
    (nœuds et SSH) n'est lue que si elle sert (`need_entry`) ou s'il n'y a
    pas de kubeconfig."""
    entry = cluster_config(args.cluster) if args.cluster and (need_entry or not args.kubeconfig) else None
    kc = args.kubeconfig or (entry or {}).get("kubeconfig")
    if not kc:
        raise SystemExit("give --cluster (with a kubeconfig in the configuration) or --kubeconfig")
    return Kube(kc), entry


def _decl(name):
    d = rg.load(state(), name)
    if d is None:
        step("registry", "error", f"registry-unknown: {name}")
        out({"findings": [cc.finding("registry-unknown", "block", registry=name)], "blocked": True})
    return d


def _read_doc(path):
    text = sys.stdin.read() if path == "-" else Path(path).read_text()
    try:
        return json.loads(text)
    except ValueError:
        import yaml
        return yaml.safe_load(text)


def cmd_declare(args):
    try:
        doc = _read_doc(args.file)
        if isinstance(doc, dict) and doc.get("kind") == "bootstrap":
            raise dp.ProfileError([("kind", "a bootstrap registry is declared by deploy, not by hand")])
        saved = rg.save(state(), args.name, doc, args.revision)
    except dp.PlanConflict as e:
        step("declare", "error", f"the declaration changed in the meantime (revision {e.current_revision})")
        out({"error": "conflict", "current_revision": e.current_revision})
        return EXIT_REFUSED
    except dp.ProfileError as e:
        step("declare", "error", "; ".join(f"{w}: {r}" for w, r in e.errors))
        out({"error": "invalid", "errors": [{"field": w, "reason": r} for w, r in e.errors]})
        return EXIT_REFUSED
    except (OSError, ValueError) as e:
        step("declare", "error", str(e)[:300])
        return EXIT_REFUSED
    step("declare", "done", f"{args.name} (revision {saved['revision']})")
    out(saved)
    return EXIT_OK


def cmd_attach(args):
    d = _decl(args.name)
    if d is None:
        return EXIT_REFUSED
    try:
        text = sys.stdin.read() if args.spec == "-" else Path(args.spec).read_text()
        creds = rg.check_creds(json.loads(text or "null"))
    except ValueError as e:
        step("attach", "error", str(e))
        return EXIT_REFUSED
    kube, _ = kube_from(args)
    step("attach", "running", f"{args.name} on {args.cluster}: secrets in {rg.NAMESPACE}")
    try:
        res = rg.attach(kube, d, creds)
        rg.update(state(), args.name, lambda x: x.__setitem__(
            "clusters", list(dict.fromkeys((x.get("clusters") or []) + [args.cluster]))))
    except (KubeError, dp.PlanConflict, KeyError) as e:
        step("attach", "error", str(e)[:300])
        return EXIT_FAIL
    for f in res["findings"]:
        step("attach", "running", f"warning {f['code']}: the read account is the write account; "
                                  "it will be written on every child node")
    step("attach", "done", f"{', '.join(res['secrets'])}")
    out(res)
    return EXIT_OK


def cmd_check(args):
    d = rg.load(state(), args.name)
    findings = []
    if d is not None:
        kube, _ = kube_from(args)
        try:
            findings = rg.check(d, rg.facts(kube, d, args.cluster))
        except KubeError as e:
            findings = [cc.finding("cluster-unreachable", "block", error=str(e)[:200])]
    else:
        findings = rg.check(None, {}, args.name)
    blocked = bool(cc.blocking(findings))
    out({"registry": args.name, "cluster": args.cluster, "findings": findings, "blocked": blocked})
    return EXIT_REFUSED if blocked else EXIT_OK


def cmd_list(args):
    out(rg.list_decls(state()))
    return EXIT_OK


def cmd_show(args):
    d = rg.load(state(), args.name)
    if d is None:
        step("show", "error", f"registry-unknown: {args.name}")
        return EXIT_REFUSED
    out(d)
    return EXIT_OK


def cmd_forget(args):
    try:
        rg.forget(state(), args.name)
    except KeyError:
        step("forget", "error", f"registry-unknown: {args.name}")
        return EXIT_REFUSED
    except ValueError as e:
        step("forget", "error", str(e))
        return EXIT_REFUSED
    step("forget", "done", args.name)
    return EXIT_OK


def parser():
    p = argparse.ArgumentParser(prog="harvester-registry", allow_abbrev=False,
                                description="Registries of the child clusters.")
    p.add_argument("--state-dir", help="console state directory (default HARVESTER_OPS_STATE_DIR)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cluster_args(s):
        s.add_argument("--cluster", required=True)
        s.add_argument("--kubeconfig")

    s = sub.add_parser("declare", allow_abbrev=False)
    s.add_argument("name")
    s.add_argument("--file", required=True)
    s.add_argument("--revision", type=int)
    s.set_defaults(fn=cmd_declare)
    s = sub.add_parser("attach", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.add_argument("--spec", default="-", help="credentials JSON file (private), - for stdin")
    s.set_defaults(fn=cmd_attach)
    s = sub.add_parser("check", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("list", allow_abbrev=False)
    s.set_defaults(fn=cmd_list)
    for name, fn in (("show", cmd_show), ("forget", cmd_forget)):
        s = sub.add_parser(name, allow_abbrev=False)
        s.add_argument("name")
        s.set_defaults(fn=fn)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.state_dir:
        os.environ["HARVESTER_OPS_STATE_DIR"] = os.path.abspath(args.state_dir)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
