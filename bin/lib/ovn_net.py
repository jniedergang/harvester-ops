"""harvester-ops : réseaux kube-ovn d'un cluster Harvester, logique pure (v1.49.0).

VPC, subnets et réseaux overlay : ce qu'une VM Harvester sait utiliser. Ce
module ne parle pas au cluster ; il lit des objets déjà relevés, vérifie une
demande, propose des valeurs, rédige les manifestes et dit ce qui cloche.
`bin/harvester-network.py` s'en sert pour écrire, la console pour montrer.

Voir docs/design/2026-09-26-reseaux-kubeovn.md.
"""

import ipaddress
import json
import re

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
NS_RE = NAME_RE
DEFAULT_VPC = "ovn-cluster"
# Posés par kube-ovn ou Harvester : lus, jamais modifiés par la console.
SYSTEM_VPCS = {DEFAULT_VPC}
SYSTEM_SUBNETS = {"ovn-default", "join", "node-local"}
MANAGED = "harvester-ops.io/managed"
NET_TYPE = "network.harvesterhci.io/type"
OVERLAY = "OverlayNetwork"
KUBE_OVN_SOCKET = "/run/openvswitch/kube-ovn-daemon.sock"
# Réseaux internes de Harvester (RKE2) : pods et services. Ni l'un ni l'autre
# n'est publié par l'API ; ce sont les valeurs de l'installeur.
RESERVED = (("pods", "10.52.0.0/16"), ("services", "10.53.0.0/16"))
SUGGEST_FROM = "10.200.0.0/16"
FULL_RATIO = 0.9


def _net(cidr):
    try:
        return ipaddress.ip_network(str(cidr), strict=False)
    except (ValueError, TypeError):
        return None


def provider_of(namespace, name):
    """Ce qu'un subnet cite pour être servi à travers un réseau overlay."""
    return f"{name}.{namespace}.ovn"


def gateway_of(cidr):
    """La passerelle d'usage : le premier hôte du réseau."""
    net = _net(cidr)
    if net is None or net.num_addresses < 4:
        return None
    return str(next(net.hosts()))


# ---------------------------------------------------------------------------
# Lecture des objets relevés
# ---------------------------------------------------------------------------

def nad_info(nad):
    md = nad.get("metadata") or {}
    try:
        conf = json.loads((nad.get("spec") or {}).get("config") or "{}")
    except ValueError:
        conf = {}
    labels = md.get("labels") or {}
    return {"namespace": md.get("namespace"), "name": md.get("name"),
            "ref": f"{md.get('namespace')}/{md.get('name')}",
            "type": conf.get("type"), "provider": conf.get("provider"),
            "overlay": conf.get("type") == "kube-ovn",
            "harvester_type": labels.get(NET_TYPE),
            "managed": labels.get(MANAGED) == "true"}


def subnet_info(s):
    md, sp, st = s.get("metadata") or {}, s.get("spec") or {}, s.get("status") or {}
    name = md.get("name")
    used = st.get("v4usingIPs") or 0
    avail = st.get("v4availableIPs") or 0
    conds = {c.get("type"): c.get("status") for c in st.get("conditions") or []}
    underlay = bool(sp.get("vlan"))
    return {"name": name, "vpc": sp.get("vpc") or DEFAULT_VPC,
            "cidr": sp.get("cidrBlock"), "gateway": sp.get("gateway"),
            "exclude": sp.get("excludeIps") or [], "provider": sp.get("provider") or "ovn",
            "nat": bool(sp.get("natOutgoing")), "private": bool(sp.get("private")),
            "allow": sp.get("allowSubnets") or [], "namespaces": sp.get("namespaces") or [],
            "dhcp": bool(sp.get("enableDHCP")), "vlan": sp.get("vlan"),
            "underlay": underlay, "used": int(used), "available": int(avail),
            "ready": conds.get("Ready") == "True",
            "system": name in SYSTEM_SUBNETS or underlay,
            "managed": ((md.get("labels") or {}).get(MANAGED) == "true")}


def vpc_info(v):
    md, sp, st = v.get("metadata") or {}, v.get("spec") or {}, v.get("status") or {}
    name = md.get("name")
    return {"name": name, "namespaces": sp.get("namespaces") or [],
            "static_routes": [{"cidr": r.get("cidr"), "next_hop": r.get("nextHopIP")}
                              for r in sp.get("staticRoutes") or []],
            "peerings": [{"remote": p.get("remoteVpc"), "local_ip": p.get("localConnectIP")}
                         for p in sp.get("vpcPeerings") or []],
            "subnets": st.get("subnets") or [],
            "standby": bool(st.get("standby", True)),
            "system": name in SYSTEM_VPCS,
            "managed": ((md.get("labels") or {}).get(MANAGED) == "true")}


def ip_info(ip):
    sp = ip.get("spec") or {}
    return {"subnet": sp.get("subnet"), "address": sp.get("ipAddress") or sp.get("v4IpAddress"),
            "mac": sp.get("macAddress"), "owner": sp.get("podName"),
            "namespace": sp.get("namespace"), "kind": sp.get("podType") or "Pod"}


# ---------------------------------------------------------------------------
# Le modèle de la vue et ses constats
# ---------------------------------------------------------------------------

def model(vpcs, subnets, nads, ips=(), node_ips=()):
    """Un bloc par VPC, ses subnets, leur réseau overlay, qui s'en sert."""
    V = [vpc_info(v) for v in vpcs]
    S = [subnet_info(s) for s in subnets]
    N = [nad_info(n) for n in nads if nad_info(n)["overlay"]]
    I = [ip_info(i) for i in ips]
    by_provider = {n["provider"]: n for n in N}
    for s in S:
        s["network"] = (by_provider.get(s["provider"]) or {}).get("ref")
        s["consumers"] = [i for i in I if i["subnet"] == s["name"]
                          and i["kind"] == "VirtualMachine"]
    blocks = []
    for v in sorted(V, key=lambda x: (not x["system"], x["name"])):
        v["subnet_list"] = sorted((s for s in S if s["vpc"] == v["name"]), key=lambda s: s["name"])
        blocks.append(v)
    return {"vpcs": blocks, "overlays": N, "findings": findings(S, N, node_ips)}


def findings(subnets, overlays, node_ips=()):
    out = []
    served = {s["provider"] for s in subnets}
    for n in overlays:
        if n["provider"] not in served:
            out.append({"code": "overlay-no-subnet", "level": "warn", "network": n["ref"]})
    providers = {n["provider"] for n in overlays}
    for s in subnets:
        if s["provider"] not in ("ovn", None) and not s["underlay"] and s["provider"] not in providers:
            out.append({"code": "subnet-network-missing", "level": "warn", "subnet": s["name"],
                        "provider": s["provider"]})
        total = s["used"] + s["available"]
        if total and s["used"] / total > FULL_RATIO:
            out.append({"code": "subnet-full", "level": "warn", "subnet": s["name"],
                        "used": s["used"], "total": total})
    for a, b, *_ in overlaps([(s["name"], s["cidr"]) for s in subnets]):
        out.append({"code": "cidr-overlap", "level": "warn", "subnet": a, "other": b})
    return out


def overlaps(named):
    """[(a, b, cidr_a, cidr_b)] pour chaque paire de réseaux qui se chevauchent."""
    nets = [(n, c, _net(c)) for n, c in named if _net(c) is not None]
    out = []
    for i, (a, ca, na) in enumerate(nets):
        for b, cb, nb in nets[i + 1:]:
            if na.version == nb.version and na.overlaps(nb):
                out.append((a, b, ca, cb))
    return out


def taken_ranges(facts):
    """Tout ce qu'un nouveau subnet ne doit pas recouvrir, nommé."""
    out = [(f"subnet {s['name']}", s["cidr"]) for s in facts.get("subnets") or []]
    out += [(label, cidr) for label, cidr in RESERVED]
    out += [(f"pods {c}", c) for c in facts.get("pod_cidrs") or []]
    return out


def suggest_cidr(facts, size=24):
    """Le premier /24 libre de 10.200.0.0/16, loin des nœuds et des pods."""
    base = _net(SUGGEST_FROM)
    taken = [_net(c) for _, c in taken_ranges(facts)]
    nodes = [ipaddress.ip_address(i) for i in facts.get("node_ips") or [] if _is_ip(i)]
    for cand in base.subnets(new_prefix=size):
        if any(t is not None and t.version == cand.version and t.overlaps(cand) for t in taken):
            continue
        if any(ip in cand for ip in nodes):
            continue
        return str(cand)
    return None


def _is_ip(v):
    try:
        ipaddress.ip_address(str(v))
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Demandes : normalisation et contrôle
# ---------------------------------------------------------------------------

SUBNET_KEYS = {"name", "vpc", "cidr", "gateway", "exclude", "network", "new_network",
               "nat", "dhcp", "private", "allow", "namespaces"}
VPC_KEYS = {"name", "namespaces", "static_routes", "peerings"}


def _list(v):
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,\s]+", v) if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def normalize_subnet(raw):
    raw = dict(raw or {})
    unknown = set(raw) - SUBNET_KEYS
    if unknown:
        raise ValueError("unknown option(s): " + ", ".join(sorted(unknown)))
    cidr = str(raw.get("cidr") or "").strip()
    net = _net(cidr)
    spec = {
        "name": str(raw.get("name") or "").strip().lower(),
        "vpc": str(raw.get("vpc") or DEFAULT_VPC).strip(),
        "cidr": str(net) if net is not None else cidr,
        "gateway": str(raw.get("gateway") or "").strip() or gateway_of(cidr),
        "exclude": _list(raw.get("exclude")),
        "network": str(raw.get("network") or "").strip(),       # ns/nom existant
        "new_network": str(raw.get("new_network") or "").strip(),  # ns/nom à créer
        "nat": bool(raw.get("nat", False)),
        "dhcp": bool(raw.get("dhcp", True)),
        "private": bool(raw.get("private", False)),
        "allow": _list(raw.get("allow")),
        "namespaces": _list(raw.get("namespaces")),
    }
    if spec["gateway"] and spec["gateway"] not in spec["exclude"]:
        spec["exclude"].insert(0, spec["gateway"])
    return spec


def normalize_vpc(raw):
    raw = dict(raw or {})
    unknown = set(raw) - VPC_KEYS
    if unknown:
        raise ValueError("unknown option(s): " + ", ".join(sorted(unknown)))
    routes = []
    for r in raw.get("static_routes") or []:
        if isinstance(r, str):
            cidr, _, hop = r.partition(">")
            r = {"cidr": cidr.strip(), "next_hop": hop.strip()}
        routes.append({"cidr": str(r.get("cidr") or "").strip(),
                       "next_hop": str(r.get("next_hop") or "").strip()})
    peerings = []
    for p in raw.get("peerings") or []:
        if isinstance(p, str):
            remote, _, ip = p.partition("@")
            p = {"remote": remote.strip(), "local_ip": ip.strip()}
        peerings.append({"remote": str(p.get("remote") or "").strip(),
                         "local_ip": str(p.get("local_ip") or "").strip()})
    return {"name": str(raw.get("name") or "").strip().lower(),
            "namespaces": _list(raw.get("namespaces")),
            "static_routes": routes, "peerings": peerings}


def finding(code, level, **facts):
    return {"code": code, "level": level, "facts": facts}


def check_subnet(spec, facts, updating=False):
    """Constats sur une demande de subnet : [finding]. `facts` : vpcs,
    subnets (subnet_info), overlays (nad_info), node_ips, pod_cidrs,
    namespaces."""
    out = []
    subnets = {s["name"]: s for s in facts.get("subnets") or []}
    vpcs = {v["name"]: v for v in facts.get("vpcs") or []}
    overlays = {n["ref"]: n for n in facts.get("overlays") or []}
    if not NAME_RE.match(spec["name"]):
        out.append(finding("invalid-name", "block", name=spec["name"]))
    current = subnets.get(spec["name"])
    if current and not updating:
        out.append(finding("subnet-exists", "block", subnet=spec["name"]))
    if updating and current and current["system"]:
        out.append(finding("subnet-system", "block", subnet=spec["name"]))
    if updating and current:
        # kube-ovn refuse de déplacer un subnet : CIDR, VPC et réseau sont fixés
        # à la création ; le reste se modifie.
        ref = spec["new_network"] or spec["network"]
        prov = provider_of(*ref.partition("/")[::2]) if ref else current["provider"]
        changed = [k for k, a, b in (("cidr", spec["cidr"], current["cidr"]),
                                     ("vpc", spec["vpc"], current["vpc"]),
                                     ("network", prov, current["provider"])) if a != b]
        if changed:
            out.append(finding("subnet-immutable", "block", subnet=spec["name"],
                               fields=", ".join(changed)))
    if spec["vpc"] not in vpcs:
        out.append(finding("vpc-missing", "block", vpc=spec["vpc"]))
    net = _net(spec["cidr"])
    if net is None:
        out.append(finding("invalid-cidr", "block", cidr=spec["cidr"]))
    else:
        if net.num_addresses < 8:
            out.append(finding("cidr-too-small", "block", cidr=spec["cidr"]))
        for label, cidr in taken_ranges(facts):
            other = _net(cidr)
            if label == f"subnet {spec['name']}":
                continue
            if other is not None and other.version == net.version and other.overlaps(net):
                out.append(finding("cidr-overlap", "block", cidr=spec["cidr"], other=label,
                                   other_cidr=cidr))
        hits = [i for i in facts.get("node_ips") or [] if _is_ip(i) and ipaddress.ip_address(i) in net]
        if hits:
            out.append(finding("cidr-covers-nodes", "block", cidr=spec["cidr"], ips=", ".join(hits)))
        gw = spec["gateway"]
        if gw and (not _is_ip(gw) or ipaddress.ip_address(gw) not in net):
            out.append(finding("gateway-outside", "block", gateway=gw, cidr=spec["cidr"]))
        for e in spec["exclude"]:
            first, _, last = e.partition("..")
            if not all(_is_ip(x) and ipaddress.ip_address(x) in net for x in filter(None, (first, last))):
                out.append(finding("exclude-outside", "block", value=e, cidr=spec["cidr"]))
    # le réseau overlay : un existant libre, ou un nouveau
    if spec["network"] and spec["new_network"]:
        out.append(finding("network-both", "block"))
    elif spec["network"]:
        n = overlays.get(spec["network"])
        if n is None:
            out.append(finding("network-missing", "block", network=spec["network"]))
        else:
            holder = next((s for s in subnets.values() if s["provider"] == n["provider"]
                           and s["name"] != spec["name"]), None)
            if holder:
                out.append(finding("network-taken", "block", network=spec["network"],
                                   subnet=holder["name"]))
    elif spec["new_network"]:
        ns, _, nm = spec["new_network"].partition("/")
        if not (NS_RE.match(ns or "") and NAME_RE.match(nm or "")):
            out.append(finding("invalid-network-name", "block", network=spec["new_network"]))
        elif spec["new_network"] in overlays:
            out.append(finding("network-exists", "block", network=spec["new_network"]))
        elif ns not in set(facts.get("namespaces") or [ns]):
            out.append(finding("namespace-missing", "block", namespace=ns))
    else:
        out.append(finding("network-none", "warn"))
    if spec["nat"] and spec["vpc"] != DEFAULT_VPC:
        out.append(finding("nat-custom-vpc", "block", vpc=spec["vpc"]))
    if not spec["nat"] and spec["vpc"] == DEFAULT_VPC:
        out.append(finding("no-nat", "ok"))
    if spec["vpc"] != DEFAULT_VPC:
        v = vpcs.get(spec["vpc"]) or {}
        if not v.get("static_routes") and not v.get("peerings"):
            out.append(finding("vpc-isolated", "ok", vpc=spec["vpc"]))
    for a in spec["allow"]:
        if _net(a) is None:
            out.append(finding("invalid-allow", "block", value=a))
    if spec["allow"] and not spec["private"]:
        out.append(finding("allow-ignored", "warn"))
    if not spec["dhcp"]:
        out.append(finding("no-dhcp", "warn"))
    return out


def check_vpc(spec, facts, updating=False):
    out = []
    vpcs = {v["name"]: v for v in facts.get("vpcs") or []}
    if not NAME_RE.match(spec["name"]):
        out.append(finding("invalid-name", "block", name=spec["name"]))
    if spec["name"] in vpcs and not updating:
        out.append(finding("vpc-exists", "block", vpc=spec["name"]))
    if spec["name"] in SYSTEM_VPCS:
        out.append(finding("vpc-system", "block", vpc=spec["name"]))
    known_ns = set(facts.get("namespaces") or [])
    for ns in spec["namespaces"]:
        if known_ns and ns not in known_ns:
            out.append(finding("namespace-missing", "block", namespace=ns))
    for r in spec["static_routes"]:
        if _net(r["cidr"]) is None or not _is_ip(r["next_hop"]):
            out.append(finding("invalid-route", "block", cidr=r["cidr"], next_hop=r["next_hop"]))
    for p in spec["peerings"]:
        if p["remote"] not in vpcs or p["remote"] == spec["name"]:
            out.append(finding("peer-missing", "block", vpc=p["remote"]))
        if _net(p["local_ip"]) is None or "/" not in p["local_ip"]:
            out.append(finding("invalid-peer-ip", "block", value=p["local_ip"]))
    return out


def check_delete(kind, name, facts):
    """Ce qui interdit une suppression : [finding]."""
    if kind == "vpc":
        v = next((v for v in facts.get("vpcs") or [] if v["name"] == name), None)
        if v is None:
            return [finding("vpc-missing", "block", vpc=name)]
        if v["system"]:
            return [finding("vpc-system", "block", vpc=name)]
        left = [s["name"] for s in facts.get("subnets") or [] if s["vpc"] == name]
        return [finding("vpc-has-subnets", "block", vpc=name, subnets=", ".join(left))] if left else []
    s = next((s for s in facts.get("subnets") or [] if s["name"] == name), None)
    if s is None:
        return [finding("subnet-missing", "block", subnet=name)]
    if s["system"]:
        return [finding("subnet-system", "block", subnet=name)]
    users = [f"{i['namespace']}/{i['owner']}" for i in facts.get("ips") or []
             if i["subnet"] == name]
    if users:
        return [finding("subnet-in-use", "block", subnet=name, users=", ".join(sorted(set(users))))]
    return []


def blocking(findings_):
    return [f for f in findings_ if f["level"] == "block"]


# ---------------------------------------------------------------------------
# Manifestes
# ---------------------------------------------------------------------------

def overlay_manifest(ref):
    ns, _, name = ref.partition("/")
    return {"apiVersion": "k8s.cni.cncf.io/v1", "kind": "NetworkAttachmentDefinition",
            "metadata": {"name": name, "namespace": ns,
                         "labels": {NET_TYPE: OVERLAY, MANAGED: "true"}},
            "spec": {"config": json.dumps({
                "cniVersion": "0.3.1", "type": "kube-ovn", "server_socket": KUBE_OVN_SOCKET,
                "provider": provider_of(ns, name)}, separators=(",", ":"))}}


def subnet_manifest(spec, overlays=()):
    ref = spec["new_network"] or spec["network"]
    ns, _, name = ref.partition("/")
    body = {"vpc": spec["vpc"], "cidrBlock": spec["cidr"], "gateway": spec["gateway"],
            "excludeIps": spec["exclude"], "protocol": "IPv4",
            "provider": provider_of(ns, name) if ref else "ovn",
            "natOutgoing": spec["nat"], "private": spec["private"],
            "allowSubnets": spec["allow"], "namespaces": spec["namespaces"],
            "enableDHCP": spec["dhcp"]}
    return {"apiVersion": "kubeovn.io/v1", "kind": "Subnet",
            "metadata": {"name": spec["name"], "labels": {MANAGED: "true"}},
            "spec": body}


def vpc_manifest(spec):
    return {"apiVersion": "kubeovn.io/v1", "kind": "Vpc",
            "metadata": {"name": spec["name"], "labels": {MANAGED: "true"}},
            "spec": {"namespaces": spec["namespaces"],
                     "staticRoutes": [{"cidr": r["cidr"], "nextHopIP": r["next_hop"],
                                       "policy": "policyDst"} for r in spec["static_routes"]],
                     "vpcPeerings": [{"remoteVpc": p["remote"], "localConnectIP": p["local_ip"]}
                                     for p in spec["peerings"]]}}
