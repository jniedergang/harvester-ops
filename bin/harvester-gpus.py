#!/usr/bin/env python3
"""harvester-gpus : l'inventaire des GPU des clusters Harvester (v1.92.0).

Lecture seule. Même modèle que l'onglet Shared resources > GPU de la console
(bin/lib/hv_gpu.py) : GPU entiers, vGPU, instances MIG et ressources GPU
des nœuds pour les conteneurs, avec leur état (libre, utilisée, désactivée)
et leur consommateur (VM ou pod).

  harvester-gpus.py list [--cluster NAME | --kubeconfig FILE] [--json]

Sans --cluster ni --kubeconfig, tous les clusters déclarés sont lus. Un
cluster injoignable ou qui refuse la lecture est dit, sans arrêter les
autres ; une CRD absente (add-on des périphériques coupé, vGPU ou MIG non
installés) est dite « non disponible », pas une erreur.

Codes de sortie : 0 tout est lu, 1 au moins un cluster n'a pas été lu,
2 erreur d'usage.
"""

import argparse
import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import hv_devices as hd  # noqa: E402
import hv_gpu as hg  # noqa: E402
from kube import KubeError  # noqa: E402

# la découverte des clusters, la sonde et le choix des cibles sont ceux de
# harvester-containers : une seule copie
_spec = importlib.util.spec_from_file_location("harvester_containers", HERE / "harvester-containers.py")
_hcl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hcl)
reachable, targets_from, _short = _hcl.reachable, _hcl.targets_from, _hcl._short
DENIAL_MARKERS, UNREACHABLE_MARKERS = _hcl.DENIAL_MARKERS, _hcl.UNREACHABLE_MARKERS

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
ABSENT_MARKERS = ("doesn't have a resource type", "could not find the requested resource")
READS = tuple((k, ("get", res)) for res, k in hg.DEVICE_RESOURCES) + (
    ("addon", ("get", "addons.harvesterhci.io", "-n", hd.ADDON[0])),
    ("nodes", ("get", "nodes")),
    ("pods", ("get", "pods", "-A")),
    ("vms", ("get", "virtualmachines.kubevirt.io", "-A")),
    ("vmis", ("get", "virtualmachineinstances.kubevirt.io", "-A")),
)
OPTIONAL = {k for _res, k in hg.DEVICE_RESOURCES} | {"addon"}


def read_cluster(kube, cluster=""):
    """(inventaire, refus) d'un cluster ; KubeError si rien n'a pu être lu ou
    si un objet est illisible pour une autre raison qu'un refus ou une CRD
    absente."""
    def one(item):
        key, args = item
        try:
            return key, (json.loads(kube.run(*args, "-o", "json")) or {}).get("items") or [], None
        except KubeError as e:
            return key, None, e
    with ThreadPoolExecutor(max_workers=len(READS)) as pool:
        results = list(pool.map(one, READS))
    lists, absent, denials = {}, set(), []
    for key, items, err in results:
        if err is None:
            lists[key] = items
            continue
        low = str(err).lower()
        if key in OPTIONAL and any(m in low for m in ABSENT_MARKERS):
            absent.add(key)
        elif any(m in low for m in DENIAL_MARKERS):
            denials.append(str(err))
        else:
            raise err
    if not lists:
        raise KubeError(denials[0] if denials else "nothing could be read")
    addon = hd.addon_enabled(lists.get("addon"))
    return hg.build(lists, cluster=cluster, absent=absent, addon=addon), denials


def collect(targets, reader=None, probe=None):
    """[(nom, Kube)] -> (états des clusters, unités avec leur cluster)."""
    reader, probe = reader or read_cluster, probe or reachable
    clusters, units = [], []
    for name, kube in targets:
        if probe(kube) is False:
            clusters.append({"cluster": name, "state": "unreachable"})
            continue
        try:
            inv, denials = reader(kube, name)
        except KubeError as e:
            msg = str(e).lower()
            state = "denied" if any(m in msg for m in DENIAL_MARKERS) else (
                "unreachable" if any(m in msg for m in UNREACHABLE_MARKERS) else "error")
            clusters.append({"cluster": name, "state": state, "error": _short(e)})
            continue
        addon = inv["available"]["addon"]
        info = {"cluster": name, "state": "ok" if inv["units"] or addon else "no-addon",
                "count": len(inv["units"]), "addon": addon, "available": inv["available"]}
        if denials:
            info["denials"] = denials
        clusters.append(info)
        units.extend(inv["units"])
    return clusters, units


def _who(u):
    out = []
    for c in u["consumers"]:
        ref = f"{c['kind']}:{c['namespace']}/{c['name']}"
        if c.get("count", 1) > 1:
            ref += f" x{c['count']}"
        out.append(ref)
    return ",".join(out) or "-"


def table(clusters, units):
    """Le tableau lisible : le résumé par type, une ligne par unité, puis
    l'état des clusters."""
    s = hg.summarize(units)
    out = ["  ".join(f"{t}: {s[t]['used']}/{s[t]['total']} used, {s[t]['free']} free" for t in hg.TYPES), ""]
    head = ("CLUSTER", "NODE", "TYPE", "NAME", "MODEL", "PARENT", "STATE", "USED", "CONSUMERS")
    lines = [head]
    for u in units:
        state = u["state"] + (f" ({u['reason']})" if u.get("reason") else "") \
            + (f" [{u['sync']}]" if u.get("sync") and u["sync"] != "synced" else "")
        lines.append((u["cluster"], u["node"], u["type"], u["name"], u["model"] or "-", u["parent"] or "-",
                      state, f"{u['used']}/{u['total']}", _who(u)))
    if units:
        widths = [max(len(r[i]) for r in lines) for i in range(len(head) - 1)]
        out += ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r[:-1])) + "  " + r[-1] for r in lines]
    else:
        out.append("(no GPU)")
    for c in clusters:
        if c["state"] == "no-addon":
            out.append(f"cluster {c['cluster']}: GPU devices add-on (pcidevices-controller) not enabled")
        elif c["state"] != "ok":
            out.append(f"cluster {c['cluster']}: {c['state']}" + (f" ({c['error']})" if c.get("error") else ""))
        else:
            off = [t for t in ("gpu", "vgpu", "mig") if not c["available"].get(t)]
            if off:
                out.append(f"cluster {c['cluster']}: not available: {', '.join(off)}")
            if c.get("denials"):
                out.append(f"cluster {c['cluster']}: partly read, refused: " + "; ".join(c["denials"]))
    return "\n".join(out)


def cmd_list(args):
    clusters, units = collect(targets_from(args))
    if args.json:
        print(json.dumps({"clusters": clusters, "units": units, "summary": hg.summarize(units)}, indent=2))
    else:
        print(table(clusters, units))
    return EXIT_OK if all(c["state"] in ("ok", "no-addon") for c in clusters) else EXIT_FAIL


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvester-gpus",
                                 description="GPU inventory of Harvester clusters (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("list", help="GPUs, vGPUs, MIG instances and container GPU resources")
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
