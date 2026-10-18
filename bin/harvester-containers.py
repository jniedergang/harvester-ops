#!/usr/bin/env python3
"""harvester-containers : les charges de conteneurs des clusters Harvester (v1.91.0).

Lecture seule. Même modèle que la section Containers de la console
(bin/lib/hv_containers.py) : namespaces système masqués, pods des VM et de
CDI écartés, pods rangés sous leur charge.

  harvester-containers.py list [--cluster NAME | --kubeconfig FILE] [--json]

Sans --cluster ni --kubeconfig, tous les clusters déclarés sont lus (ceux de
config.yaml puis ceux de la console). Un cluster injoignable ou qui refuse
la lecture est dit, sans arrêter les autres.

Codes de sortie : 0 tout est lu, 1 au moins un cluster n'a pas été lu,
2 erreur d'usage.
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvester-containers",
                                 description="Container workloads of Harvester clusters (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("list", help="the container workloads, system namespaces hidden")
    sp.add_argument("--cluster", help="a declared cluster (default: every declared cluster)")
    sp.add_argument("--kubeconfig", help="a kubeconfig, instead of a declared cluster")
    sp.add_argument("--json", action="store_true", help="JSON output")
    sp.set_defaults(fn=cmd_list)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
