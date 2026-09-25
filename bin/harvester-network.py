#!/usr/bin/env python3
"""harvester-network : VPC, subnets et réseaux overlay kube-ovn d'un cluster Harvester.

Toute écriture de la console sur les réseaux kube-ovn passe par ce script
(parité CLI) ; il s'utilise aussi seul.

  harvester-network inventory --cluster harv1 [--json]
  harvester-network check  --cluster harv1 --kind subnet --spec demande.json [--update]
  harvester-network apply  --cluster harv1 --kind subnet --spec demande.json [--update]
  harvester-network delete --cluster harv1 --kind subnet --name lab-a [--with-network]

Une demande de subnet : {"name", "vpc", "cidr", "gateway", "exclude",
"network" (réseau overlay existant ns/nom) ou "new_network" (à créer),
"nat", "dhcp", "private", "allow", "namespaces"}. Une demande de VPC :
{"name", "namespaces", "static_routes", "peerings"}.

Sorties : 0 fait, 1 échec, 2 bloqué par le contrôle, 3 annulé. Les étapes
s'écrivent sur stderr en `STEP_EVENT|étape|statut|message`, que la console
relaie au dock. Voir docs/design/2026-09-26-reseaux-kubeovn.md.
"""

import argparse
import json
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import ovn_net as on  # noqa: E402
from kube import Kube, KubeError, cluster_config  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_BLOCKED, EXIT_CANCELLED = 0, 1, 2, 3
K_VPC = "vpcs.kubeovn.io"
K_SUBNET = "subnets.kubeovn.io"
K_IP = "ips.kubeovn.io"
K_NAD = "network-attachment-definitions.k8s.cni.cncf.io"


class Cancelled(Exception):
    pass


def step(sid, status, msg=""):
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def _on_signal(signum, frame):
    raise Cancelled(f"signal {signum}")


def kube_from(args):
    entry = cluster_config(args.cluster) if args.cluster else None
    kc = args.kubeconfig or (entry or {}).get("kubeconfig")
    if not kc:
        raise SystemExit("give --cluster (with a kubeconfig in the configuration) or --kubeconfig")
    return Kube(kc)


def spec_from(path, kind):
    raw = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text())
    return on.normalize_vpc(raw) if kind == "vpc" else on.normalize_subnet(raw)


# ---------------------------------------------------------------------------
# Relevé
# ---------------------------------------------------------------------------

def inventory(kube):
    """Le modèle de la vue et les faits du contrôle, en un passage."""
    try:
        kube.run("get", "--raw", "/version", timeout=10)
    except KubeError:
        return {"unreachable": True, "kubeovn": False}
    try:
        vpcs = kube.list(K_VPC)
    except KubeError:
        vpcs = []
    if not vpcs:
        # sans l'addon kube-ovn, la ressource n'existe pas : liste vide
        return {"unreachable": False, "kubeovn": False}
    subnets = kube.list(K_SUBNET)
    nads = kube.list(K_NAD)
    ips = kube.list(K_IP)
    nodes = kube.list("nodes")
    node_ips = [a.get("address") for n in nodes for a in (n.get("status") or {}).get("addresses") or []
                if a.get("type") == "InternalIP"]
    pod_cidrs = [c for n in nodes for c in (n.get("spec") or {}).get("podCIDRs") or []]
    namespaces = sorted(n["metadata"]["name"] for n in kube.list("namespaces"))
    m = on.model(vpcs, subnets, nads, ips, node_ips)
    facts = {"vpcs": m["vpcs"], "subnets": [s for v in m["vpcs"] for s in v["subnet_list"]],
             "overlays": m["overlays"], "node_ips": node_ips, "pod_cidrs": pod_cidrs,
             "namespaces": namespaces, "ips": [on.ip_info(i) for i in ips]}
    free = [f["network"] for f in m["findings"] if f["code"] == "overlay-no-subnet"]
    return {"unreachable": False, "kubeovn": True, "model": m, "facts": facts,
            "namespaces": namespaces, "suggested_cidr": on.suggest_cidr(facts),
            "free_overlays": free}


def _facts(kube):
    inv = inventory(kube)
    if inv.get("unreachable"):
        raise RuntimeError("cluster unreachable")
    if not inv.get("kubeovn"):
        raise RuntimeError("kube-ovn is not enabled on this cluster (addon kubeovn-operator)")
    return inv["facts"]


def _check(kind, spec, facts, update):
    return (on.check_vpc(spec, facts, updating=update) if kind == "vpc"
            else on.check_subnet(spec, facts, updating=update))


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------

def cmd_inventory(args):
    inv = inventory(kube_from(args))
    print(json.dumps(inv) if args.json else json.dumps(inv, indent=1))
    return EXIT_OK


def cmd_check(args):
    spec = spec_from(args.spec, args.kind)
    found = _check(args.kind, spec, _facts(kube_from(args)), args.update)
    blocked = bool(on.blocking(found))
    print(json.dumps({"blocked": blocked, "findings": found, "spec": spec}))
    return EXIT_BLOCKED if blocked else EXIT_OK


def _wait(kube, kind, name, ready, timeout=90, sleep=time.sleep):
    deadline = time.time() + timeout
    while time.time() < deadline:
        obj = kube.get(kind, None, name)
        if obj is not None and ready(obj):
            return obj
        sleep(3)
    raise RuntimeError(f"{name} not ready after {timeout} s")


def _subnet_ready(obj):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (obj.get("status") or {}).get("conditions") or [])


def _vpc_ready(obj):
    return bool((obj.get("status") or {}).get("standby"))


def cmd_apply(args):
    kube = kube_from(args)
    spec = spec_from(args.spec, args.kind)
    facts = _facts(kube)
    found = _check(args.kind, spec, facts, args.update)
    if on.blocking(found):
        for f in on.blocking(found):
            step("check", "error", f"{f['code']} {json.dumps(f['facts'])}")
        return EXIT_BLOCKED
    step("check", "done", f"{args.kind} {spec['name']}: no blocker")
    created_nad = None
    try:
        if args.kind == "vpc":
            kube.apply([on.vpc_manifest(spec)])
            step("apply", "running", f"VPC {spec['name']} declared")
            _wait(kube, K_VPC, spec["name"], _vpc_ready)
            step("apply", "done", f"VPC {spec['name']} ready")
            return EXIT_OK
        if spec["new_network"]:
            kube.apply([on.overlay_manifest(spec["new_network"])])
            created_nad = spec["new_network"]
            step("network", "done", f"overlay network {spec['new_network']} created")
        kube.apply([on.subnet_manifest(spec)])
        step("apply", "running", f"subnet {spec['name']} {spec['cidr']} declared in VPC {spec['vpc']}")
        _wait(kube, K_SUBNET, spec["name"], _subnet_ready)
        step("apply", "done", f"subnet {spec['name']} ready")
        return EXIT_OK
    except (Cancelled, KubeError, RuntimeError) as e:
        step("apply", "error", str(e)[:300])
        if not args.update:
            _undo(kube, args.kind, spec, created_nad)
        return EXIT_CANCELLED if isinstance(e, Cancelled) else EXIT_FAIL


def _undo(kube, kind, spec, nad=None):
    """Une création interrompue ne laisse ni VPC, ni subnet, ni réseau à
    moitié faits."""
    todo = [(K_VPC if kind == "vpc" else K_SUBNET, None, spec["name"])]
    if nad:
        todo.append((K_NAD, *nad.partition("/")[::2]))
    for kind_, ns, name in todo:
        try:
            if kube.get(kind_, ns, name) is not None:
                kube.delete(kind_, ns, name)
                step("undo", "done", f"{name} removed")
        except KubeError as e:
            step("undo", "error", f"could not remove {name}: {e}")


def cmd_delete(args):
    kube = kube_from(args)
    facts = _facts(kube)
    found = on.check_delete(args.kind, args.name, facts)
    if on.blocking(found):
        for f in found:
            step("check", "error", f"{f['code']} {json.dumps(f['facts'])}")
        return EXIT_BLOCKED
    if args.kind == "vpc":
        kube.delete(K_VPC, None, args.name)
        step("delete", "done", f"VPC {args.name} deleted")
        return EXIT_OK
    subnet = next(s for s in facts["subnets"] if s["name"] == args.name)
    kube.delete(K_SUBNET, None, args.name)
    step("delete", "running", f"subnet {args.name} deleted")
    if args.with_network and subnet.get("network"):
        nad = next((n for n in facts["overlays"] if n["ref"] == subnet["network"]), None)
        if nad and nad["managed"]:
            ns, _, name = nad["ref"].partition("/")
            kube.delete(K_NAD, ns, name)
            step("delete", "running", f"overlay network {nad['ref']} deleted")
        elif nad:
            step("delete", "running", f"overlay network {nad['ref']} kept (not created by the console)")
    step("delete", "done", f"subnet {args.name} deleted")
    return EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvester-network", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("inventory", cmd_inventory), ("check", cmd_check),
                     ("apply", cmd_apply), ("delete", cmd_delete)):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        sp.add_argument("--cluster", help="cluster name in the configuration")
        sp.add_argument("--kubeconfig", help="kubeconfig of the Harvester cluster")
        if name == "inventory":
            sp.add_argument("--json", action="store_true")
        else:
            sp.add_argument("--kind", choices=("vpc", "subnet"), required=True)
        if name in ("check", "apply"):
            sp.add_argument("--spec", required=True, help="JSON request, '-' for stdin")
            sp.add_argument("--update", action="store_true", help="change an existing object")
        if name == "delete":
            sp.add_argument("--name", required=True)
            sp.add_argument("--with-network", action="store_true",
                            help="also delete the subnet's overlay network if the console created it")
    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _on_signal)
    try:
        return args.fn(args)
    except ValueError as e:
        step("check", "error", str(e))
        return EXIT_BLOCKED
    except (KubeError, RuntimeError) as e:
        step(args.cmd, "error", str(e)[:300])
        return EXIT_FAIL
    except Cancelled:
        return EXIT_CANCELLED


if __name__ == "__main__":
    sys.exit(main())
