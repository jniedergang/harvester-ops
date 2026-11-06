#!/usr/bin/env python3
"""Registres des clusters enfants : déclarer, rattacher, vérifier.

    harvester-registry declare NOM --file r.yaml|- [--revision N]
    harvester-registry attach  NOM --cluster C [--kubeconfig K]     (identifiants JSON sur l'entrée standard)
    harvester-registry check   NOM --cluster C [--kubeconfig K]
    harvester-registry list
    harvester-registry show    NOM
    harvester-registry forget  NOM
    harvester-registry deploy  NOM --cluster C --address IP --size-gib N --storage-class S
                               [--dns-name N] [--mirrors h1,h2] [--cert F --key F --ca F]
                               [--spec F] [--timeout S] [--dry-run]
    harvester-registry status  NOM --cluster C [--kubeconfig K]
    harvester-registry delete  NOM --cluster C [--kubeconfig K] [--delete-data]

Une déclaration (bin/lib/registries.py) ne porte aucun secret : adresse,
CA publique, usage (images, charts), registres d'origine miroités. Les
identifiants sont saisis au rattachement, sur l'entrée standard
(`{"push": {"username", "password"}, "pull": {...}}`), jamais en argument,
et posés en Secrets dans `harvester-ops-registry` du cluster Harvester.

`deploy` pose le registre d'amorçage (bin/lib/registry_seed.py) : constats,
certificat (CA générée par openssl, ou celui du client : fichiers, ou
`--spec` JSON privé {cert, key, ca}), comptes `push` et `pull` (mots de
passe aléatoires, htpasswd par l'entrée standard), images du paquet interne
importées sur chaque nœud par ctr, objets appliqués avec l'identité de la
personne, attente de `/v2/` (401 sans compte, puis 200 avec le compte de
lecture), déclaration et Secrets posés. `delete` garde le volume sauf
`--delete-data` (point 193).

Progression sur stderr au format STEP_EVENT|<étape>|<statut>|<message> ;
résultat en JSON sur stdout. Codes : 0 succès, 1 échec, 2 refus.

Voir docs/design/2026-11-05-artefacts-registre-260-plan.md (tâches 6 et 7).
"""

import argparse
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import capi_cluster as cc  # noqa: E402
import cluster_decl  # noqa: E402
import deploy_plans as dp  # noqa: E402
import registries as rg  # noqa: E402
import registry_seed as rs  # noqa: E402
from kube import Kube, KubeError, cluster_config  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2
POLL = float(os.environ.get("HARVESTER_OPS_REGISTRY_POLL", "5"))


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


# ---------------------------------------------------------------------------
# Registre d'amorçage
# ---------------------------------------------------------------------------
def _answers(address, port=443, timeout=2.0):
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def deploy_facts(kube, spec, answers=_answers):
    """Ce que registry_seed.check lit du cluster et de l'installation."""
    import longhorn_room as lr
    pools = [cc.pool_info(p) for p in kube.list("ippools.loadbalancer.harvesterhci.io")]
    replicas = {}
    for sc in kube.list("storageclasses"):
        if sc.get("provisioner") == "driver.longhorn.io":
            try:
                n = int(((sc.get("parameters") or {}).get("numberOfReplicas")) or 3)
            except (TypeError, ValueError):
                n = 3
            replicas[sc["metadata"]["name"]] = max(1, n)
    try:
        lh_nodes, over, minimal = lr.read_numbers(kube)
        longhorn = {"nodes": lh_nodes, "over": over, "minimal": minimal}
    except KubeError:
        longhorn = None
    taken = rg.load(state(), spec["name"]) is not None or \
        kube.get("deployments", rs.NAMESPACE, spec["name"]) is not None
    return {"pools": pools, "vip": kube.server_host(), "address_answers": answers(spec["address"]),
            "longhorn": longhorn, "class_replicas": replicas, "bundle": rs.bundle(state()) is not None,
            "tools_missing": rs.tools_missing(), "name_taken": taken}


def _client_tls(args):
    """Certificat du client : fichiers (--cert/--key/--ca) ou demande privée
    (--spec, JSON {cert, key, ca}) ; None pour une CA générée."""
    if args.spec:
        doc = json.loads(Path(args.spec).read_text() or "{}")
        if doc.get("cert") or doc.get("key"):
            if not (doc.get("cert") and doc.get("key") and doc.get("ca")):
                raise ValueError("a client certificate needs cert, key and ca")
            return {"crt": doc["cert"], "key": doc["key"], "ca": doc["ca"]}
        return None
    if args.cert or args.key or args.ca:
        if not (args.cert and args.key and args.ca):
            raise ValueError("--cert, --key and --ca go together")
        return {"crt": Path(args.cert).read_text(), "key": Path(args.key).read_text(),
                "ca": Path(args.ca).read_text()}
    return None


def wait_ready(decl, pull, timeout, registry_cls=None, clock=time.monotonic, sleep=time.sleep):
    """`/v2/` : 401 sans compte (authentification active), puis 200 avec le
    compte de lecture. Rend la dernière raison d'attente, None si prêt."""
    import oci_push as op
    cls = registry_cls or op.Registry
    end, why = clock() + timeout, "no answer yet"
    while True:
        try:
            anon = cls(decl["endpoint"], None, ca_pem=decl["ca_pem"], timeout=10)
            anon.request("GET", "/v2/", "")
            why = "the registry answered without authentication"
        except op.RegistryError as e:
            if "authentication required" in str(e):
                reach, _, ok = rg.probe(decl, pull, "pull", registry_cls=cls)
                if ok:
                    return None
                why = "the read account is refused" if reach else "no answer with the read account"
            else:
                why = str(e)[:200]
        if clock() >= end:
            return why
        sleep(POLL)


def cmd_deploy(args):
    spec = {"name": args.name, "address": args.address, "size_gib": args.size_gib,
            "storage_class": args.storage_class}
    if args.dns_name:
        spec["dns_name"] = args.dns_name
    if not dp.NAME_RE.match(args.name) or len(args.name) > rg.NAME_MAX or args.name in rg.RESERVED:
        step("check", "error", f"invalid registry name (RFC 1123, {rg.NAME_MAX} characters at most)")
        return EXIT_REFUSED
    if not 5 <= args.size_gib <= 2048:
        step("check", "error", "size: an integer from 5 to 2048 GiB")
        return EXIT_REFUSED
    kube, entry = kube_from(args, need_entry=not args.dry_run)
    step("check", "running", f"{args.name} on {args.cluster} at {args.address}")
    try:
        findings = rs.check(spec, deploy_facts(kube, spec))
    except KubeError as e:
        findings = [cc.finding("cluster-unreachable", "block", error=str(e)[:200])]
    blocked = bool(cc.blocking(findings))
    if args.dry_run or blocked:
        step("check", "error" if blocked else "done",
             ", ".join(f["code"] for f in cc.blocking(findings)) or "no blocking finding")
        out({"registry": args.name, "cluster": args.cluster, "findings": findings, "blocked": blocked})
        return EXIT_REFUSED if blocked else EXIT_OK
    step("check", "done", "no blocking finding")
    try:
        client_tls = _client_tls(args)
    except (OSError, ValueError) as e:
        step("tls", "error", str(e))
        return EXIT_REFUSED
    work = Path(tempfile.mkdtemp(prefix="hops-registry-", dir=os.environ.get("HARVESTER_OPS_WORK_DIR")))
    try:
        step("tls", "running", "client certificate" if client_tls else "CA and server certificate")
        tls = client_tls or rs.make_tls(work, args.address, args.dns_name, args.name)
        step("tls", "done", "CA of the console (10 years), certificate of 2 years" if not client_tls
             else "client certificate")
        creds = {r: {"username": r, "password": secrets.token_urlsafe(32)} for r in ("push", "pull")}
        step("accounts", "running", "push (write) and pull (read)")
        push_line = rs.make_htpasswd("push", creds["push"]["password"])
        htp = {"read": push_line + rs.make_htpasswd("pull", creds["pull"]["password"]), "write": push_line}
        step("accounts", "done", "bcrypt entries; the pull account cannot write (read-only instance)")
        decl = rs.declaration(spec, tls["ca"], args.cluster, [m for m in args.mirrors.split(",") if m])
        rg.check_decl(decl)                       # CA du client illisible, miroir mal écrit : avant tout geste
        decl["name"] = args.name
        b = rs.bundle(state())
        spec["image"] = b["images"]["registry"]
        import node_images
        node_images.import_on_nodes(entry, [b["tar"]], step, sid="images")
        step("apply", "running", f"namespace {rs.NAMESPACE}: pod, volume of {args.size_gib} GiB, address "
                                 f"{args.address}")
        kube.apply(rs.manifests(spec, tls, htp) + rg.attach_manifests(decl, creds)[1:])
        step("apply", "done", f"{args.name}: deployment, service, volume, secrets")
        step("ready", "running", f"waiting for https://{args.address}/v2/")
        why = wait_ready(decl, creds["pull"], args.timeout)
        if why:
            step("ready", "error", f"not ready after {args.timeout} s: {why}")
            return EXIT_FAIL
        step("ready", "done", "authentication required without an account, read account accepted")
        saved = rg.save(state(), args.name, {k: v for k, v in decl.items() if k != "name"})
        step("declare", "done", f"{args.name} (bootstrap, revision {saved['revision']})")
    except dp.ProfileError as e:
        step("deploy", "error", "; ".join(f"{w}: {r}" for w, r in e.errors))
        return EXIT_REFUSED
    except (KubeError, RuntimeError, OSError, dp.PlanConflict) as e:
        step("deploy", "error", str(e)[:300])
        return EXIT_FAIL
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out({"registry": args.name, "kind": "bootstrap", "created": True, "endpoint": args.address,
         "push_endpoint": decl["push_endpoint"], "cluster": args.cluster})
    return EXIT_OK


def cmd_status(args):
    d = rg.load(state(), args.name)
    kube, _ = kube_from(args)
    try:
        dep = kube.get("deployments", rs.NAMESPACE, args.name)
        pvc = kube.get("persistentvolumeclaims", rs.NAMESPACE, f"{args.name}-data")
        svc = kube.get("services", rs.NAMESPACE, args.name)
        present = {r: kube.get("secrets", rs.NAMESPACE, n) is not None for r, n in rg.secret_names(args.name).items()}
        used = None
        vol = ((pvc or {}).get("spec") or {}).get("volumeName")
        if vol:
            lv = kube.get("volumes.longhorn.io", "longhorn-system", vol)
            used = ((lv or {}).get("status") or {}).get("actualSize")
    except KubeError as e:
        step("status", "error", str(e)[:300])
        return EXIT_FAIL
    st = (dep or {}).get("status") or {}
    containers = (((dep or {}).get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
    ingress = ((((svc or {}).get("status") or {}).get("loadBalancer") or {}).get("ingress") or [{}])[0]
    repos = None
    if d and dep:
        pull = rg.read_creds(kube, args.name, "pull")
        if pull:
            try:
                import oci_push as op
                _, _, body = op.Registry(d["endpoint"], pull, ca_pem=d.get("ca_pem"), timeout=15).request(
                    "GET", "/v2/_catalog?n=1000", "registry:catalog:*")
                repos = len(json.loads(body).get("repositories") or [])
            except (op.RegistryError, ValueError):
                repos = None
    out({"registry": args.name, "declared": d is not None, "present": dep is not None,
         "ready": bool(st.get("readyReplicas")), "image": containers[0].get("image") if containers else None,
         "address": ingress.get("ip") or (d or {}).get("address"),
         "volume": {"size": (((pvc or {}).get("spec") or {}).get("resources") or {}).get("requests", {}).get("storage"),
                    "phase": ((pvc or {}).get("status") or {}).get("phase"), "used_bytes": used} if pvc else None,
         "accounts": present, "repositories": repos})
    return EXIT_OK


def cmd_delete(args):
    d = rg.load(state(), args.name)
    if d is not None and d.get("kind") != "bootstrap":
        step("delete", "error", f"{args.name} is not a bootstrap registry (use forget)")
        return EXIT_REFUSED
    kube, _ = kube_from(args)
    names = rg.secret_names(args.name)
    objs = [("deployments", args.name), ("services", args.name), ("configmaps", f"{args.name}-config"),
            ("secrets", f"{args.name}-tls"), ("secrets", f"{args.name}-htpasswd"),
            ("secrets", names["push"]), ("secrets", names["pull"]), ("secrets", names["ca"])]
    if args.delete_data:
        objs.append(("persistentvolumeclaims", f"{args.name}-data"))
    try:
        for kind, n in objs:
            if kube.get(kind, rs.NAMESPACE, n) is not None:
                kube.delete(kind, rs.NAMESPACE, n)
                step("delete", "running", f"{kind} {n}")
    except KubeError as e:
        step("delete", "error", str(e)[:300])
        return EXIT_FAIL
    if d is not None:
        rg.delete(state(), args.name)
    kept = "" if args.delete_data else f"; volume {args.name}-data kept (its images stay for a new deployment)"
    step("delete", "done", f"{args.name} removed{kept}")
    out({"registry": args.name, "deleted": True, "data_kept": not args.delete_data})
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
    s = sub.add_parser("deploy", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.add_argument("--address", required=True)
    s.add_argument("--size-gib", type=int, default=20)
    s.add_argument("--storage-class", required=True)
    s.add_argument("--dns-name")
    s.add_argument("--mirrors", default="", help="origin registries it mirrors, comma separated")
    s.add_argument("--cert")
    s.add_argument("--key")
    s.add_argument("--ca")
    s.add_argument("--spec", help="private JSON file {cert, key, ca} (client certificate)")
    s.add_argument("--timeout", type=int, default=600)
    s.add_argument("--dry-run", action="store_true", help="checks only")
    s.set_defaults(fn=cmd_deploy)
    s = sub.add_parser("status", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("delete", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.add_argument("--delete-data", action="store_true")
    s.set_defaults(fn=cmd_delete)
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
