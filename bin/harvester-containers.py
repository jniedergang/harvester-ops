#!/usr/bin/env python3
"""harvester-containers : les charges de conteneurs des clusters Harvester (v1.91.0, gestes v1.93.0).

Même modèle que la section Containers de la console
(bin/lib/hv_containers.py) : namespaces système masqués, pods des VM et de
CDI écartés, pods rangés sous leur charge.

  harvester-containers.py list [--cluster NAME | --kubeconfig FILE] [--json]
  harvester-containers.py create (--cluster NAME | --kubeconfig FILE)
        (--file MANIFEST | --name N --namespace NS --image IMG [--kind Deployment|Job]
         [--replicas N] [--command CMD] [--env K=V ...] [--gpus N] [--gpu-resource R]
         [--cpu Q] [--memory Q]) [--create-namespace] [--dry-run]
  harvester-containers.py scale   ... --namespace NS --kind K --name N --replicas N [--dry-run]
  harvester-containers.py restart ... --namespace NS --kind K --name N [--dry-run]
  harvester-containers.py delete  ... --namespace NS --kind K --name N [--dry-run]

Sans --cluster ni --kubeconfig, `list` lit tous les clusters déclarés (ceux
de config.yaml puis ceux de la console). Un cluster injoignable ou qui
refuse la lecture est dit, sans arrêter les autres.

Les gestes refusent les namespaces système, font contrôler chaque écriture
à blanc par le serveur (--dry-run=server) avant d'écrire, et posent une
limite mémoire sur tout conteneur créé (256Mi par défaut). Progression sur
stderr en STEP_EVENT|étape|statut|message.

Codes de sortie : 0 fait, 1 au moins un cluster non lu ou une écriture
refusée, 2 erreur d'usage ou demande refusée avant le cluster.
"""

import argparse
import json
import os
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import cluster_decl  # noqa: E402
import hv_containers as hc  # noqa: E402
from kube import Kube, KubeError, _read_yaml, cluster_config  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
DENIAL_MARKERS = ("forbidden", "is not allowed", "cannot list", "cannot get")


def declared_clusters():
    """Clusters de config.yaml suivis de ceux déclarés par la console."""
    cfg = os.environ.get("HARVESTER_OPS_CONFIG", "/etc/harvester-ops/config.yaml")
    data = _read_yaml(cfg) if Path(cfg).is_file() else None
    state = cluster_decl.state_dir()
    decls = [(p, _read_yaml(p)) for p in cluster_decl.decl_files(state)]
    return cluster_decl.merge((data or {}).get("clusters") or [], decls, state)


UNREACHABLE_MARKERS = ("unable to connect", "timed out", "no route to host", "connection refused",
                       "i/o timeout", "couldn't get current server api group list")


def reachable(kube, timeout=2.0):
    """True/False selon que le serveur d'API répond en TCP ; None si on ne
    sait pas le dire (kubeconfig illisible) : on lit alors quand même. Évite
    d'attendre les relances de kubectl sur un cluster éteint."""
    try:
        url = urlparse(kube.run("config", "view", "--minify", "-o",
                                "jsonpath={.clusters[0].cluster.server}").strip())
    except KubeError:
        return None
    if not url.hostname:
        return None
    try:
        with socket.create_connection((url.hostname, url.port or 443), timeout=timeout):
            return True
    except OSError:
        return False


def read_cluster(kube):
    """(charges, refus) d'un cluster, chaque type lu en parallèle ; KubeError
    si rien n'a pu être lu ou si un type est illisible pour une autre raison
    qu'un refus."""
    def one(item):
        res, kind = item
        try:
            return kind, (json.loads(kube.run("get", res, "-A", "-o", "json")) or {}).get("items") or [], None
        except KubeError as e:
            return kind, None, e
    with ThreadPoolExecutor(max_workers=len(hc.WORKLOAD_RESOURCES)) as pool:
        results = list(pool.map(one, hc.WORKLOAD_RESOURCES))
    lists, denials = {}, []
    for kind, items, err in results:
        if err is None:
            lists[kind] = items
        elif any(m in str(err).lower() for m in DENIAL_MARKERS):
            denials.append(str(err))
        else:
            raise err
    if not lists:
        raise KubeError(denials[0] if denials else "nothing could be read")
    return hc.build(lists), denials


def _short(msg):
    """La dernière ligne utile d'une erreur de kubectl."""
    lines = [x.strip() for x in str(msg).splitlines() if x.strip()]
    return (lines[-1] if lines else "")[:300]


def collect(targets, reader=None, probe=None):
    """[(nom, Kube)] -> (états des clusters, charges avec leur cluster)."""
    reader, probe = reader or read_cluster, probe or reachable
    clusters, rows = [], []
    for name, kube in targets:
        if probe(kube) is False:
            clusters.append({"cluster": name, "state": "unreachable"})
            continue
        try:
            workloads, denials = reader(kube)
        except KubeError as e:
            msg = str(e).lower()
            state = "denied" if any(m in msg for m in DENIAL_MARKERS) else (
                "unreachable" if any(m in msg for m in UNREACHABLE_MARKERS) else "error")
            clusters.append({"cluster": name, "state": state, "error": _short(e)})
            continue
        info = {"cluster": name, "state": "ok", "count": len(workloads)}
        if denials:
            info["denials"] = denials
        clusters.append(info)
        rows.extend(dict(w, cluster=name) for w in workloads)
    rows.sort(key=lambda w: (w["name"], w["cluster"], w["namespace"], w["kind"]))
    return clusters, rows


def table(clusters, rows, now=None):
    """Le tableau lisible : une ligne par charge, puis l'état des clusters."""
    head = ("CLUSTER", "NAMESPACE", "KIND", "NAME", "READY", "STATE", "GPU", "AGE", "IMAGES")
    lines = [head]
    for w in rows:
        ready = f"{w['ready']}/{w['desired']}" if w["desired"] is not None else str(w["ready"])
        lines.append((w["cluster"], w["namespace"], w["kind"], w["name"], ready, w["state"],
                      str(w["gpus"] or "-"), hc.age(w["created"], now), ",".join(w["images"]) or "-"))
    widths = [max(len(r[i]) for r in lines) for i in range(len(head) - 1)]
    out = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r[:-1])) + "  " + r[-1] for r in lines]
    if not rows:
        out.append("(no container workload outside the system namespaces)")
    for c in clusters:
        if c["state"] != "ok":
            out.append(f"cluster {c['cluster']}: {c['state']}" + (f" ({c['error']})" if c.get("error") else ""))
        elif c.get("denials"):
            out.append(f"cluster {c['cluster']}: partly read, refused: " + "; ".join(c["denials"]))
    return "\n".join(out)


def targets_from(args):
    if args.kubeconfig:
        return [(args.cluster or "cluster", Kube(args.kubeconfig, timeout=30))]
    if args.cluster:
        entry = cluster_config(args.cluster)
        if not entry.get("kubeconfig"):
            raise ValueError(f"cluster {args.cluster} has no kubeconfig in the configuration")
        return [(args.cluster, Kube(entry["kubeconfig"], timeout=30))]
    out = [(c["name"], Kube(c["kubeconfig"], timeout=30))
           for c in declared_clusters() if c.get("name") and c.get("kubeconfig")]
    if not out:
        raise ValueError("no cluster declared: give --cluster or --kubeconfig")
    return out


def cmd_list(args):
    clusters, rows = collect(targets_from(args))
    if args.json:
        print(json.dumps({"clusters": clusters, "workloads": rows}, indent=2))
    else:
        print(table(clusters, rows))
    return EXIT_OK if all(c["state"] == "ok" for c in clusters) else EXIT_FAIL


# ---------------------------------------------------------------------------
# v1.93.0 : les gestes. Chacun valide la demande sans cluster, refuse les
# namespaces système, fait contrôler l'écriture à blanc par le serveur
# (--dry-run=server : webhooks, quotas, droits), puis écrit. Rien n'est écrit
# si le contrôle échoue. La progression part sur stderr en STEP_EVENT.
# ---------------------------------------------------------------------------

def step(sid, status, msg=""):
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


class GestureError(Exception):
    """Une étape a échoué ; le message est déjà dit en STEP_EVENT."""


def one_target(args):
    """Le seul cluster d'un geste : --kubeconfig, ou --cluster déclaré."""
    if not args.kubeconfig and not args.cluster:
        raise ValueError("give --cluster or --kubeconfig")
    return targets_from(args)[0][1]


def _kube_call(kube, sid, *a, input=None):
    """kubectl, et l'échec dit sans le chemin du kubeconfig."""
    try:
        return kube.run(*a, input=input)
    except KubeError as e:
        msg = _short(str(e).replace(str(kube.kubeconfig), "<kubeconfig>"))
        step(sid, "error", msg)
        raise GestureError(msg) from None


def _checked_write(kube, label, *a, input=None, dry=False):
    """Le contrôle à blanc du serveur, puis l'écriture."""
    step("dry-run", "running", f"server-side check of {label}")
    _kube_call(kube, "dry-run", *a, "--dry-run=server", input=input)
    step("dry-run", "done", "accepted by the server")
    if dry:
        step("write", "skipped", "dry run: nothing written")
        return ""
    step("write", "running", label)
    out = _kube_call(kube, "write", *a, input=input)
    step("write", "done", label)
    return out


def _namespace_absent(kube, ns):
    """True si le namespace n'existe pas, False s'il existe, None si on ne
    peut pas le dire (lecture refusée : le contrôle à blanc tranchera)."""
    try:
        kube.run("get", "namespace", ns, "-o", "name")
        return False
    except KubeError as e:
        low = str(e).lower()
        if "notfound" in low or "not found" in low:
            return True
        return None


def load_spec(args):
    """Le manifeste à créer, depuis --spec (JSON de la console), --file
    (YAML ou JSON) ou les options."""
    if args.spec:
        try:
            data = json.loads(Path(args.spec).read_text())
        except (OSError, ValueError) as e:
            raise ValueError(f"unreadable spec: {e}") from None
        if isinstance(data, dict) and "manifest" in data:
            return hc.check_manifest(data["manifest"], args.namespace)
        return hc.build_manifest(data)
    if args.file:
        doc = _read_yaml(args.file)
        if doc is None:
            raise ValueError(f"unreadable manifest: {args.file}")
        return hc.check_manifest(doc, args.namespace)
    return hc.build_manifest({
        "name": args.name, "namespace": args.namespace, "kind": args.kind, "image": args.image,
        "replicas": args.replicas, "command": args.command, "env": args.env or [],
        "gpus": args.gpus, "gpu_resource": args.gpu_resource, "cpu": args.cpu, "memory": args.memory})


def cmd_create(args):
    step("validate", "running", "checking the request")
    manifest = load_spec(args)
    ns, name, kind = manifest["metadata"]["namespace"], manifest["metadata"]["name"], manifest["kind"]
    step("validate", "done", f"{kind} {ns}/{name}")
    kube = one_target(args)
    made_ns = False
    absent = _namespace_absent(kube, ns)
    if absent:
        if not args.create_namespace:
            step("namespace", "error", f"namespace {ns} does not exist (ask to create it)")
            raise GestureError(f"namespace {ns} does not exist")
        if args.dry_run:
            # le contrôle de la charge échouerait faute de namespace
            _checked_write(kube, f"namespace {ns}", "create", "namespace", ns, dry=args.dry_run)
            step("dry-run", "skipped", f"the {kind} is checked once the namespace exists")
            return EXIT_OK
        _checked_write(kube, f"namespace {ns}", "create", "namespace", ns, dry=args.dry_run)
        made_ns = True
    body = json.dumps(manifest)
    try:
        _checked_write(kube, f"{kind} {ns}/{name}", "create", "-f", "-", "-o", "name", input=body,
                       dry=args.dry_run)
    except GestureError:
        if made_ns:
            # rien de laissé à moitié : le namespace créé pour rien repart
            try:
                kube.run("delete", "namespace", ns, "--wait=false")
                step("namespace", "done", f"namespace {ns} removed again")
            except KubeError:
                step("namespace", "warn", f"namespace {ns} was created and could not be removed")
        raise
    step("create", "done", f"{kind} {ns}/{name} created")
    return EXIT_OK


def cmd_scale(args):
    if args.replicas is None or not 0 <= args.replicas <= hc.MAX_REPLICAS:
        raise ValueError(f"--replicas must be between 0 and {hc.MAX_REPLICAS}")
    ns, kind, name = hc.check_target(args.namespace, args.kind, args.name, hc.SCALABLE_KINDS)
    kube = one_target(args)
    _checked_write(kube, f"{kind} {ns}/{name} to {args.replicas}", "scale",
                   f"{hc.resource_of(kind)}/{name}", "-n", ns, f"--replicas={args.replicas}", dry=args.dry_run)
    step("scale", "done", f"{kind} {ns}/{name} scaled to {args.replicas}")
    return EXIT_OK


def cmd_restart(args, now=None):
    ns, kind, name = hc.check_target(args.namespace, args.kind, args.name, hc.RESTARTABLE_KINDS)
    kube = one_target(args)
    _checked_write(kube, f"rolling restart of {kind} {ns}/{name}", "patch", hc.resource_of(kind),
                   name, "-n", ns, "--type", "merge", "-p", json.dumps(hc.restart_patch(now)), dry=args.dry_run)
    step("restart", "done", f"{kind} {ns}/{name} restarting")
    return EXIT_OK


def cmd_delete(args):
    ns, kind, name = hc.check_target(args.namespace, args.kind, args.name, hc.KINDS)
    kube = one_target(args)
    _checked_write(kube, f"deletion of {kind} {ns}/{name}", "delete", hc.resource_of(kind),
                   name, "-n", ns, "--wait=false", dry=args.dry_run)
    step("delete", "done", f"{kind} {ns}/{name} deleted")
    return EXIT_OK


def _target_args(sp, write=True):
    sp.add_argument("--cluster", help="a declared cluster")
    sp.add_argument("--kubeconfig", help="a kubeconfig, instead of a declared cluster")
    if write:
        sp.add_argument("--dry-run", action="store_true",
                        help="the server-side check only, nothing written")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvester-containers",
                                 description="Container workloads of Harvester clusters.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("list", help="the container workloads, system namespaces hidden")
    sp.add_argument("--cluster", help="a declared cluster (default: every declared cluster)")
    sp.add_argument("--kubeconfig", help="a kubeconfig, instead of a declared cluster")
    sp.add_argument("--json", action="store_true", help="JSON output")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("create", help="create a Deployment or a Job (or a workload from a manifest)")
    _target_args(sp)
    src = sp.add_mutually_exclusive_group()
    src.add_argument("--file", help="a YAML or JSON manifest of one workload")
    src.add_argument("--spec", help=argparse.SUPPRESS)          # JSON écrit par la console
    sp.add_argument("--name")
    sp.add_argument("--namespace")
    sp.add_argument("--kind", default="Deployment", choices=hc.CREATE_KINDS)
    sp.add_argument("--image")
    sp.add_argument("--replicas", type=int, default=1, help="pods of a Deployment, runs of a Job")
    sp.add_argument("--command", help="the command, split like a shell would")
    sp.add_argument("--env", action="append", metavar="NAME=VALUE")
    sp.add_argument("--gpus", type=int, default=0, help="GPUs requested by the container")
    sp.add_argument("--gpu-resource", default=hc.DEFAULT_GPU_RESOURCE)
    sp.add_argument("--cpu", help="CPU limit (500m, 2)")
    sp.add_argument("--memory", default=hc.DEFAULT_MEMORY, help="memory limit (default 256Mi)")
    sp.add_argument("--create-namespace", action="store_true", help="create the namespace if it is missing")
    sp.set_defaults(fn=cmd_create)

    for verb, fn, helptext in (("scale", cmd_scale, "set the number of pods"),
                               ("restart", cmd_restart, "rolling restart (new pods)"),
                               ("delete", cmd_delete, "delete a workload")):
        sp = sub.add_parser(verb, help=helptext)
        _target_args(sp)
        sp.add_argument("--namespace", required=True)
        sp.add_argument("--kind", required=True, help="Deployment, StatefulSet, Job...")
        sp.add_argument("--name", required=True)
        if verb == "scale":
            sp.add_argument("--replicas", type=int, required=True)
        sp.set_defaults(fn=fn)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ValueError as e:
        if args.cmd != "list":
            step("validate", "error", str(e))
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except GestureError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
