#!/usr/bin/env python3
"""harvester-network : VPC, subnets et réseaux overlay kube-ovn d'un cluster Harvester.

Toute écriture de la console sur les réseaux kube-ovn passe par ce script
(parité CLI) ; il s'utilise aussi seul.

  harvester-network inventory --cluster harv1 [--json]
  harvester-network check  --cluster harv1 --kind subnet --spec demande.json [--update]
  harvester-network apply  --cluster harv1 --kind subnet --spec demande.json [--update]
  harvester-network delete --cluster harv1 --kind subnet --name lab-a [--with-network]

v1.66.0, la suite du menu kube-ovn (underlay, NAT, politiques) :
  harvester-network apply  --cluster harv1 --kind provider|vlan|external|gateway|eip|snat|dnat|policy --spec d.json [--update]
  harvester-network delete --cluster harv1 --kind provider|vlan|external|gateway|eip|snat|dnat|policy --name N [--namespace ns]
  harvester-network state  --cluster harv1          (lignes des vues Underlay et NAT, JSON)

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
import ovn_extra as ox  # noqa: E402
from kube import Kube, KubeError, cluster_config  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_BLOCKED, EXIT_CANCELLED = 0, 1, 2, 3
K_VPC = "vpcs.kubeovn.io"
K_SUBNET = "subnets.kubeovn.io"
K_IP = "ips.kubeovn.io"
K_NAD = "network-attachment-definitions.k8s.cni.cncf.io"


class Cancelled(BaseException):
    # v1.99.0 : BaseException, comme le Cancelled de la réplication. Un
    # signal levé dans une lecture gardée par `except Exception` (configuration,
    # relance d'une API, nettoyage) y était avalé : l'outil continuait comme
    # si de rien n'était, ou concluait à une erreur au lieu d'une annulation.
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
    if kind in EXTRA_KINDS:
        return raw
    return on.normalize_vpc(raw) if kind == "vpc" else on.normalize_subnet(raw)


EXTRA_KINDS = ("provider", "vlan", "external", "gateway", "eip", "snat", "dnat", "policy", "repair")


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
    if args.kind in EXTRA_KINDS:
        return extra_apply(kube, args.kind, spec, args.update)
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
    # v1.99.0 : un second signal pendant le retour arrière ne doit pas le couper
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
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
    if args.kind in EXTRA_KINDS:
        return extra_delete(kube, args.kind, args.name, args.namespace)
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



# ---------------------------------------------------------------------------
# v1.66.0 : underlay, NAT, politiques (ovn_extra)
# ---------------------------------------------------------------------------

GRACE = 60      # s : premier refus d'un nœud souvent suivi du succès


def extra_state(kube):
    """Les lignes des vues Underlay et NAT, en un passage."""
    subnets = kube.list(ox.K_SUBNET)
    vlans = kube.list(ox.K_VLAN)
    eips = kube.list(ox.K_EIP)
    snats, dnats = kube.list(ox.K_SNAT), kube.list(ox.K_DNAT)
    pods = kube.list("pods", "kube-system", selector="ovn.kubernetes.io/vpc-nat-gw=true")
    sts = kube.list("statefulsets", "kube-system", selector="ovn.kubernetes.io/vpc-nat-gw=true")
    health = ox.ovn_health(kube.list("deployments", "kube-system"),
                           kube.list("pods", "kube-system", selector="app=kube-ovn-cni"), kube.list("nodes"))
    return {"health": health,
            "providers": ox.provider_rows(kube.list(ox.K_PN), vlans), "vlans": ox.vlan_rows(vlans, subnets),
            "externals": ox.external_rows(subnets, vlans, eips),
            "gateways": ox.gateway_rows(kube.list(ox.K_GW), pods, sts, eips),
            "eips": ox.eip_rows(eips, snats, dnats),
            "snats": ox.rule_rows(snats, "snat"), "dnats": ox.rule_rows(dnats, "dnat")}


def cmd_state(args):
    print(json.dumps(extra_state(kube_from(args))))
    return EXIT_OK


def _until(kube, kind, ns, name, done, timeout, label, sleep=time.sleep, now=time.time):
    """Relit l'objet jusqu'à `done(obj, écoulé)` : (True|False|None, message)."""
    t0 = now()
    last = None
    while now() - t0 < timeout:
        res, msg = done(kube.get(kind, ns, name), now() - t0)
        if msg and msg != last:
            step(label, "running", msg)
            last = msg
        if res is True:
            step(label, "done", msg or "ready")
            return EXIT_OK
        if res is False:
            step(label, "error", msg or "failed")
            return EXIT_FAIL
        sleep(3)
    step(label, "error", f"not ready after {timeout} s")
    return EXIT_FAIL


def _provider_done(o, elapsed):
    if o is None:
        return False, "the provider network disappeared"
    [row] = ox.provider_rows([o], [])
    if row["ready"] and row["ready_nodes"]:
        return True, f"ready on {', '.join(row['ready_nodes'])}"
    if row["errors"] and elapsed > GRACE:
        return False, "; ".join(row["errors"])
    return None, "; ".join(row["errors"]) or "waiting for the hosts"


def _gateway_done(kube, name, fix=True):
    def done(o, elapsed):
        if o is None:
            return False, "the gateway disappeared"
        pods = kube.list("pods", "kube-system", selector=f"app=vpc-nat-gw-{name}")
        sts = kube.list("statefulsets", "kube-system", selector="ovn.kubernetes.io/vpc-nat-gw=true")
        [row] = ox.gateway_rows([o], pods, sts, [])
        if not fix and row["broken"] and pods and (pods[0].get("metadata", {}).get("annotations") or {}).get(ox.GW_INIT) == "true":
            return True, "pod up; its pod network was replaced by the tenant network (kube-ovn before 1.16.1)"
        if row["ready"]:
            nics = ", ".join(f"{i['name']}={i['network']}" for i in row["interfaces"])
            return True, f"{row['pod']} on {row['node']} ({nics})"
        return None, f"pod {row['phase'] or 'pending'}"
    return done


def _ready_flag(what):
    def done(o, elapsed):
        if o is None:
            return False, f"the {what} disappeared"
        st = o.get("status") or {}
        if st.get("ready"):
            return True, f"{what} ready" + (f" ({st.get('ip') or st.get('v4ip')})" if st.get("ip") or st.get("v4ip") else "")
        return None, f"waiting for kube-ovn ({what})"
    return done


def gateway_repair(kube, name):
    """Retire l'annotation qui remplace le réseau des pods de la passerelle
    (kube-ovn avant 1.16.1), puis attend qu'elle se réinitialise."""
    sts = kube.get("statefulsets", "kube-system", f"vpc-nat-gw-{name}")
    if sts is None:
        raise ValueError(f"no gateway pod set vpc-nat-gw-{name}")
    patch = ox.gateway_fix_patch(sts)
    if not patch:
        step("gateway", "done", f"gateway {name}: its pods keep their own network, nothing to repair")
        return EXIT_OK
    kube.run("patch", "statefulset", f"vpc-nat-gw-{name}", "-n", "kube-system", "--type", "json", "-p", json.dumps(patch))
    step("gateway", "running", f"gateway {name}: pod network given back (kube-ovn before 1.16.1 replaced it, issue 6632)")
    return _until(kube, ox.K_GW, None, name, _gateway_done(kube, name), 600, "gateway")


def extra_apply(kube, kind, spec, update):
    if kind == "provider":
        lm = kube.get("linkmonitors.network.harvesterhci.io", None, "nic") or {}
        obj = ox.provider_network(spec, ox.taken_nics((lm.get("status") or {}).get("linkStatus")))
        if kube.get(ox.K_PN, None, obj["metadata"]["name"]) is not None:
            raise ValueError(f"the provider network {obj['metadata']['name']} already exists")
        kube.create(obj)
        step("provider", "running", f"provider network {obj['metadata']['name']} takes {obj['spec']['defaultInterface']}")
        return _until(kube, ox.K_PN, None, obj["metadata"]["name"], _provider_done, 300, "provider")
    if kind == "vlan":
        obj = ox.vlan(spec)
        if kube.get(ox.K_PN, None, obj["spec"]["provider"]) is None:
            raise ValueError(f"no provider network {obj['spec']['provider']}")
        kube.create(obj)
        name = obj["metadata"]["name"]

        def done(o, elapsed):
            pn = kube.get(ox.K_PN, None, obj["spec"]["provider"]) or {}
            if (o or {}).get("status", {}).get("conflict"):
                return False, "another VLAN has this ID on this provider network"
            return (True, f"VLAN {name} on {obj['spec']['provider']}") if name in ((pn.get("status") or {}).get("vlans") or []) \
                else (None, "waiting for kube-ovn")
        return _until(kube, ox.K_VLAN, None, name, done, 120, "vlan")
    if kind == "external":
        nad, sub = ox.external_network(spec)
        if kube.get(ox.K_VLAN, None, sub["spec"]["vlan"]) is None:
            raise ValueError(f"no VLAN {sub['spec']['vlan']}")
        if kube.get(ox.K_SUBNET, None, sub["metadata"]["name"]) is not None:
            raise ValueError(f"the subnet {sub['metadata']['name']} already exists")
        kube.apply([nad])
        step("external", "running", f"network kube-system/{nad['metadata']['name']} declared")
        kube.create(sub)

        def done(o, elapsed):
            ok = any(c.get("type") == "Ready" and c.get("status") == "True" for c in (o or {}).get("status", {}).get("conditions") or [])
            return (True, f"external network {sub['metadata']['name']} ready") if ok else (None, "waiting for kube-ovn")
        return _until(kube, ox.K_SUBNET, None, sub["metadata"]["name"], done, 120, "external")
    if kind == "gateway":
        subnets = kube.list(ox.K_SUBNET)
        vlans = {v["metadata"]["name"]: (v.get("spec") or {}).get("provider") for v in kube.list(ox.K_VLAN)}
        ext_pn = {s["metadata"]["name"]: vlans.get((s.get("spec") or {}).get("vlan")) for s in subnets if (s.get("spec") or {}).get("vlan")}
        obj = ox.nat_gateway(spec, subnets, ext_pn)
        name = obj["metadata"]["name"]
        if kube.get(ox.K_GW, None, name) is not None:
            raise ValueError(f"the gateway {name} already exists")
        vpc = kube.get(ox.K_VPC, None, obj["spec"]["vpc"])
        if vpc is None:
            raise ValueError(f"no VPC {obj['spec']['vpc']}")
        new_vpc = ox.vpc_with_route(vpc, obj["spec"]["lanIp"]) if spec.get("add_route", True) else None
        kube.create(obj)
        step("gateway", "running", f"gateway {name}: {obj['spec']['lanIp']} in {obj['spec']['subnet']}, out by {obj['spec']['externalSubnets'][0]}")
        if new_vpc:
            kube.replace(new_vpc)
            step("gateway", "running", f"VPC {obj['spec']['vpc']}: default route through {obj['spec']['lanIp']}")
        code = _until(kube, ox.K_GW, None, name, _gateway_done(kube, name, fix=False), 600, "gateway")
        return gateway_repair(kube, name) if code == EXIT_OK else code
    if kind == "repair":
        return gateway_repair(kube, spec.get("name"))
    if kind == "eip":
        subnets = kube.list(ox.K_SUBNET)
        if not spec.get("ip"):
            ext = next((s for s in subnets if s["metadata"]["name"] == spec.get("external")), None)
            used = [(e.get("status") or {}).get("ip") or (e.get("spec") or {}).get("v4ip") for e in kube.list(ox.K_EIP)]
            spec = {**spec, "ip": ox.next_eip(ext, used) if ext else None}
        obj = ox.eip(spec, subnets)
        if kube.get(ox.K_GW, None, obj["spec"]["natGwDp"]) is None:
            raise ValueError(f"no gateway {obj['spec']['natGwDp']}")
        kube.create(obj)
        return _until(kube, ox.K_EIP, None, obj["metadata"]["name"], _ready_flag("external IP"), 180, "eip")
    if kind in ("snat", "dnat"):
        obj = ox.snat(spec) if kind == "snat" else ox.dnat(spec)
        e = kube.get(ox.K_EIP, None, obj["spec"]["eip"])
        if e is None or not (e.get("status") or {}).get("ready"):
            raise ValueError(f"the external IP {obj['spec']['eip']} is not ready")
        kube.create(obj)
        return _until(kube, ox.K_SNAT if kind == "snat" else ox.K_DNAT, None, obj["metadata"]["name"],
                      _ready_flag(f"{kind.upper()} rule"), 120, kind)
    # policy
    obj = ox.network_policy(spec)
    ns, name = obj["metadata"]["namespace"], obj["metadata"]["name"]
    cur = kube.get(ox.K_NP, ns, name)
    if update:
        if cur is None:
            raise ValueError(f"no policy {ns}/{name}")
        obj["metadata"]["resourceVersion"] = cur["metadata"]["resourceVersion"]
        kube.replace(obj)
    else:
        if cur is not None:
            raise ValueError(f"the policy {ns}/{name} already exists")
        kube.create(obj)
    step("policy", "done", f"policy {ns}/{name}: {', '.join(obj['spec']['policyTypes'])} "
                           f"for {', '.join(spec.get('vms') or []) or 'every VM of ' + ns}")
    return EXIT_OK


def extra_delete(kube, kind, name, ns=None):
    state = extra_state(kube) if kind != "policy" else {}
    ox.delete_check(kind, name, state)
    res = {"provider": ox.K_PN, "vlan": ox.K_VLAN, "external": ox.K_SUBNET, "gateway": ox.K_GW,
           "eip": ox.K_EIP, "snat": ox.K_SNAT, "dnat": ox.K_DNAT, "policy": ox.K_NP}[kind]
    obj = kube.get(res, ns if kind == "policy" else None, name)
    if obj is None:
        raise ValueError(f"no {kind} {name}")
    kube.delete(res, ns if kind == "policy" else None, name)
    step(kind, "running", f"{kind} {name} deleted")

    def gone(o, elapsed):
        return (True, f"{kind} {name} is gone") if o is None else (None, "waiting for kube-ovn")
    code = _until(kube, res, ns if kind == "policy" else None, name, gone, 180, kind)
    if code == EXIT_OK and kind == "external":
        if kube.get(K_NAD, "kube-system", name) is not None:
            kube.delete(K_NAD, "kube-system", name)
            step("external", "done", f"network kube-system/{name} deleted")
    if code == EXIT_OK and kind == "gateway" and ((obj.get("metadata") or {}).get("annotations") or {}).get(ox.ROUTE_ADDED) == "true":
        vpc = kube.get(ox.K_VPC, None, (obj.get("spec") or {}).get("vpc"))
        if vpc is not None:
            kube.replace(ox.vpc_with_route(vpc, (obj.get("spec") or {}).get("lanIp"), add=False))
            step("gateway", "done", f"VPC {vpc['metadata']['name']}: default route through {(obj.get('spec') or {}).get('lanIp')} removed")
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvester-network", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("inventory", cmd_inventory), ("check", cmd_check),
                     ("apply", cmd_apply), ("delete", cmd_delete), ("state", cmd_state)):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        sp.add_argument("--cluster", help="cluster name in the configuration")
        sp.add_argument("--kubeconfig", help="kubeconfig of the Harvester cluster")
        if name == "inventory":
            sp.add_argument("--json", action="store_true")
        elif name == "check":
            sp.add_argument("--kind", choices=("vpc", "subnet"), required=True)
        elif name != "state":
            sp.add_argument("--kind", choices=("vpc", "subnet") + EXTRA_KINDS, required=True)
        if name in ("check", "apply"):
            sp.add_argument("--spec", required=True, help="JSON request, '-' for stdin")
            sp.add_argument("--update", action="store_true", help="change an existing object")
        if name == "delete":
            sp.add_argument("--name", required=True)
            sp.add_argument("--namespace", help="policy: its namespace")
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
