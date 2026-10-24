"""Réseaux libvirt d'un hôte KVM (prochaine version KVM).

Créer (NAT, routé, isolé, pont vers un pont existant de l'hôte), démarrer,
arrêter, autostart, supprimer. Le XML est fabriqué ici et part sur l'entrée
standard (`net-define --validate --file /dev/stdin`).

Garde-fous : un sous-réseau qui chevauche une adresse ou une route de
l'hôte (`ip -j addr`, `ip -j route`, lus par le compte SSH) ou un autre
réseau libvirt est refusé (un NAT 172.16.0.0/16 sur node2 couperait le LAN
de la maison) ; arrêter un réseau qu'une VM en marche utilise, ou supprimer
un réseau qu'une définition cite, est refusé.
"""

import hashlib
import ipaddress
import json
import re
import xml.etree.ElementTree as ET

import kvm_host as kh
import kvm_ops as ko
import kvm_storage as ks

OpError = ko.OpError

NET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NET_MODES = ("nat", "route", "isolated", "bridge")
_IFNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                        r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
# types d'interface virtuels : jamais proposés comme carte physique (macvtap)
_VIRTUAL_KINDS = ("bridge", "veth", "tun", "tap", "macvtap", "macvlan", "vlan", "bond",
                  "dummy", "vxlan", "wireguard", "ipvlan", "geneve", "team")


def check_network_name(name, new=False):
    if new:
        if not isinstance(name, str) or not NET_NAME_RE.match(name):
            raise OpError("invalid", "invalid network name: letters, digits, '.', '_' and '-', "
                          "64 characters at most, starting with a letter or digit")
        return name
    try:
        return ko.check_vm_name(name)
    except OpError:
        raise OpError("invalid", "invalid network name") from None


# --- liens de l'hôte -----------------------------------------------------------

def parse_links(link_json, addr_json, route_json):
    """Ponts, cartes physiques, adresses et routes de l'hôte, d'après
    `ip -j -d link show`, `ip -j addr`, `ip -j route`."""
    links = json.loads(link_json or "[]")
    addrs = json.loads(addr_json or "[]")
    routes = json.loads(route_json or "[]")
    bridges, physical, names = [], [], []
    for ln in links:
        name = ln.get("ifname")
        if not name:
            continue
        names.append(name)
        kind = (ln.get("linkinfo") or {}).get("info_kind")
        if kind == "bridge":
            bridges.append({"name": name, "state": ln.get("operstate")})
        elif ln.get("link_type") == "ether" and kind not in _VIRTUAL_KINDS:
            physical.append({"name": name, "state": ln.get("operstate"),
                             "master": ln.get("master")})
    networks = []
    for a in addrs:
        for info in a.get("addr_info") or []:
            if info.get("family") != "inet":
                continue
            try:
                networks.append({"dev": a.get("ifname"), "source": "address",
                                 "network": str(ipaddress.ip_network(
                                     f"{info['local']}/{info['prefixlen']}", strict=False))})
            except (KeyError, ValueError):
                continue
    for r in routes:
        dst = r.get("dst")
        if not dst or dst == "default":
            continue
        try:
            net = ipaddress.ip_network(dst if "/" in dst else f"{dst}/32", strict=False)
        except ValueError:
            continue
        if net.version == 4:
            networks.append({"dev": r.get("dev"), "source": "route", "network": str(net)})
    return {"bridges": bridges, "physical": physical, "links": names, "networks": networks}


def host_links(host):
    """Ponts et cartes de l'hôte, adresses et routes IPv4, en UN appel ssh
    (lecture, rejouable). OpError `failed` si `ip` ne répond pas."""
    res = ko._sh_batch(host, [["ip", "-j", "-d", "link", "show"], ["ip", "-j", "addr"],
                              ["ip", "-j", "route"]], idempotent=True)
    bad = [i for i, (rc, _) in enumerate(res) if rc != 0]
    if bad:
        raise OpError("failed", f"{host.name}: cannot read the host network ({ko._last_line(res[bad[0]][1])})")
    try:
        return parse_links(*(text for _, text in res))
    except ValueError:
        raise OpError("failed", f"{host.name}: unreadable output of ip -j") from None


def list_networks(host):
    """[{name, state, active, autostart, persistent, forward, bridge, ips...}]."""
    rows = kh.parse_net_list(host.virsh("net-list", "--all", idempotent=True))
    cmds = {r["name"]: ("net-dumpxml", "--network", r["name"]) for r in rows}
    out = host.batch(cmds, idempotent=True) if cmds else {}
    nets = []
    for r in rows:
        v = out[r["name"]]
        if not v.ok:
            if v.error.kind == "notfound":
                continue
            raise v.error
        nets.append({**r, **kh.parse_net_xml(str(v))})
    return nets


# --- XML et contrôles ------------------------------------------------------------

def _subnet(spec):
    net = ipaddress.ip_network(spec["subnet"], strict=False)
    if net.version != 4:
        raise ValueError("IPv4 only")
    return net


def default_dhcp(net):
    hosts = list(net.hosts())
    return (str(hosts[1]), str(hosts[-1])) if len(hosts) >= 2 else (None, None)


def check_network(spec, links, networks):
    """Raisons de refus d'un réseau à créer. `spec` : {name, mode (nat |
    route | isolated | bridge), bridge, subnet, dhcp (bool, défaut vrai),
    dhcp_start, dhcp_end, domain, autostart}. `links` : host_links() ;
    `networks` : list_networks()."""
    reasons = []
    if not isinstance(spec, dict):
        return ["the network spec must be an object"]
    try:
        check_network_name(spec.get("name"), new=True)
    except OpError as e:
        reasons.append(e.message)
    if any(n.get("name") == spec.get("name") for n in networks):
        reasons.append(f"a network named {spec.get('name')} already exists")
    mode = spec.get("mode")
    if mode not in NET_MODES:
        reasons.append("mode must be nat, route, isolated or bridge")
        return reasons
    br = spec.get("bridge")
    taken = set(links.get("links") or []) | {n.get("bridge") for n in networks if n.get("bridge")}
    if mode == "bridge":
        if not isinstance(br, str) or not _IFNAME_RE.match(br):
            reasons.append("a bridge network needs the name of an existing bridge of the host")
        elif br not in {b["name"] for b in links.get("bridges") or []}:
            reasons.append(f"{br} is not a bridge of the host")
        if spec.get("subnet"):
            reasons.append("a bridge network has no subnet: the bridge's LAN gives the addresses")
        return reasons
    if br is not None:
        if not isinstance(br, str) or not _IFNAME_RE.match(br):
            reasons.append("invalid bridge name (15 characters at most)")
        elif br in taken:
            reasons.append(f"the bridge name {br} is already used on the host")
    try:
        net = _subnet(spec)
    except (KeyError, TypeError, ValueError):
        reasons.append("invalid IPv4 subnet (for example 192.168.150.0/24)")
        return reasons
    if not 8 <= net.prefixlen <= 30:
        reasons.append("the subnet prefix must be between /8 and /30")
    if not net.is_private:
        reasons.append("the subnet must be a private range")
    seen = set()
    for item in links.get("networks") or []:
        other = ipaddress.ip_network(item["network"])
        if other.network_address.is_loopback or item["network"] in seen:
            continue
        if net.overlaps(other):
            seen.add(item["network"])       # une fois par réseau (adresse et route)
            reasons.append(f"the subnet overlaps {item['network']} ({item['source']} of "
                           f"{item.get('dev') or 'the host'})")
    for n in networks:
        for ip in n.get("ips") or []:
            if ip.get("family", "ipv4") != "ipv4" or not ip.get("address") or ip.get("prefix") is None:
                continue
            other = ipaddress.ip_network(f"{ip['address']}/{ip['prefix']}", strict=False)
            if net.overlaps(other):
                reasons.append(f"the subnet overlaps network {n['name']} ({other})")
    if spec.get("dhcp", True):
        start, end = spec.get("dhcp_start"), spec.get("dhcp_end")
        if start is None and end is None:
            start, end = default_dhcp(net)
        try:
            s, e = ipaddress.ip_address(start), ipaddress.ip_address(end)
            gw = next(net.hosts())
            if s not in net or e not in net or s > e:
                reasons.append("the DHCP range must be inside the subnet, start before end")
            elif gw >= s and gw <= e:
                reasons.append(f"the DHCP range must not include the gateway {gw}")
            elif s == net.network_address or e == net.broadcast_address:
                reasons.append("the DHCP range must not include the network or broadcast address")
        except (TypeError, ValueError, StopIteration):
            reasons.append("invalid DHCP range")
    dom = spec.get("domain")
    if dom is not None and (not isinstance(dom, str) or not _DOMAIN_RE.match(dom)):
        reasons.append("invalid domain name")
    if "autostart" in spec and not isinstance(spec["autostart"], bool):
        reasons.append("autostart must be true or false")
    return reasons


def network_xml(spec):
    """XML d'un réseau NAT, routé, isolé ou pont (spec contrôlé avant)."""
    root = ET.Element("network")
    ET.SubElement(root, "name").text = spec["name"]
    mode = spec["mode"]
    if mode == "bridge":
        ET.SubElement(root, "forward", mode="bridge")
        ET.SubElement(root, "bridge", name=spec["bridge"])
    else:
        if mode in ("nat", "route"):
            ET.SubElement(root, "forward", mode=mode)
        attrs = {"stp": "on", "delay": "0"}
        if spec.get("bridge"):
            attrs = {"name": spec["bridge"], **attrs}
        ET.SubElement(root, "bridge", attrs)
        if spec.get("domain"):
            ET.SubElement(root, "domain", name=spec["domain"])
        net = _subnet(spec)
        gw = next(net.hosts())
        ip = ET.SubElement(root, "ip", address=str(gw), prefix=str(net.prefixlen))
        if spec.get("dhcp", True):
            start, end = spec.get("dhcp_start"), spec.get("dhcp_end")
            if start is None and end is None:
                start, end = default_dhcp(net)
            dhcp = ET.SubElement(ip, "dhcp")
            ET.SubElement(dhcp, "range", start=str(start), end=str(end))
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


# --- gestes ----------------------------------------------------------------------

# modes où le pont appartient au réseau libvirt (créé à son démarrage) ; en
# mode `bridge`, le pont est celui de l'hôte et vit sans le réseau
_OWNED_BRIDGE_MODES = ("nat", "route", "isolated", "open")


def _users(domains, network, live_only=False, bridge=None):
    """Domaines dont une carte cite ce réseau : `type='network'` sur son nom,
    ou `type='bridge'` sur `bridge`, le pont créé par le réseau (une carte
    branchée directement sur virbrN tombe aussi quand le réseau s'arrête)."""
    users = []
    for name, e in domains["domains"].items():
        if live_only and not e["active"]:
            continue
        vms = [e["live"]] if live_only else [e["inactive"], e["live"]]
        for vm in vms:
            if any((n.get("type") == "network" and n.get("source") == network)
                   or (bridge and n.get("type") == "bridge" and n.get("source") == bridge)
                   for n in (vm or {}).get("nics") or []):
                users.append(name)
                break
    return sorted(users)


def _own_bridge(net):
    """Pont créé par le réseau (None en mode `bridge` : pont de l'hôte). Une
    carte `type='direct'` (macvtap) vise une carte de l'hôte, jamais le
    réseau : elle n'en dépend pas."""
    return net.get("bridge") if net.get("forward") in _OWNED_BRIDGE_MODES else None


def _net(host, name):
    for n in list_networks(host):
        if n["name"] == name:
            return n
    raise OpError("notfound", f"{host.name}: network {name} not found")


def define_network(host, spec, step=None, dry_run=False):
    """Crée un réseau : contrôles (liens et réseaux relus ici), `net-define
    --validate --file /dev/stdin`, `net-start`, autostart si demandé. Retour
    arrière au mieux sur échec. `dry_run` : contrôles et XML seulement."""
    def st(sid, status, msg=""):
        if step:
            step(sid, status, msg)

    st("plan", "running", str(spec.get("name") if isinstance(spec, dict) else "?"))
    links = host_links(host)
    networks = list_networks(host)
    reasons = check_network(spec, links, networks)
    if reasons:
        st("plan", "failed", "; ".join(reasons))
        raise OpError("refused", "; ".join(reasons), {"refusals": reasons})
    xml = network_xml(spec)
    st("plan", "done", f"{spec['mode']} network")
    if dry_run:
        return {"name": spec["name"], "dry_run": True, "xml": xml}
    name, done = spec["name"], []
    try:
        st("define", "running", name)
        ks.virsh_input(host, ("net-define", "--validate", "--file", "/dev/stdin"), xml)
        done.append("define")
        host.virsh("net-start", "--network", name)
        done.append("start")
        if spec.get("autostart"):
            host.virsh("net-autostart", "--network", name)
        st("define", "done", name)
    except BaseException as exc:
        leftovers = []
        for verb in reversed(done):
            try:
                host.virsh("net-destroy" if verb == "start" else "net-undefine", "--network", name)
            except kh.KvmError as e:
                leftovers.append({"kind": "network", "name": name, "step": verb,
                                  "error": e.message})
        exc.leftovers = leftovers
        if isinstance(exc, OpError):
            exc.data["leftovers"] = leftovers
        raise
    return {"name": name, "dry_run": False, "network": _net(host, name)}


def start_network(host, name):
    check_network_name(name)
    host.virsh("net-start", "--network", name)
    return _net(host, name)


def autostart_network(host, name, enable=True):
    check_network_name(name)
    host.virsh(*(["net-autostart", "--network", name] + ([] if enable else ["--disable"])))
    return _net(host, name)


def stop_network(host, name):
    """`net-destroy` ; refus si une VM en marche y a une carte."""
    check_network_name(name)
    net = _net(host, name)
    doms = ks.read_domains(host)
    users = _users(doms, name, live_only=True, bridge=_own_bridge(net))
    if users:
        raise OpError("refused", f"a running VM has a card on it ({', '.join(users)})",
                      {"refusals": [f"used by {u}" for u in users]})
    if doms["unknown"]:
        raise OpError("refused", f"{ks.REASON_UNPROVEN}: unreadable VM definition(s) "
                      f"{', '.join(doms['unknown'])}")
    host.virsh("net-destroy", "--network", name)
    return _net(host, name)


def delete_network_plan(host, name):
    check_network_name(name)
    net = _net(host, name)
    doms = ks.read_domains(host)
    refusals = []
    users = _users(doms, name, bridge=_own_bridge(net))
    if users:
        refusals.append(f"used by {', '.join(users)}")
    if doms["unknown"]:
        refusals.append(f"{ks.REASON_UNPROVEN}: unreadable VM definition(s) "
                        f"{', '.join(doms['unknown'])}")
    plan = {"name": name, "network": net, "refusals": refusals}
    plan["digest"] = network_digest(plan)
    return plan


def network_digest(plan):
    """Empreinte d'un plan de suppression de réseau : nom, uuid, mode, pont
    et adresses. Un réseau recréé ou changé entre l'aperçu et l'exécution
    n'a plus la même."""
    net = plan.get("network") or {}
    doc = json.dumps({"name": plan.get("name"), "uuid": net.get("uuid"),
                      "forward": net.get("forward"), "bridge": net.get("bridge"),
                      "ips": net.get("ips")}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(doc.encode()).hexdigest()


def delete_network(host, name, step=None, expect_digest=None):
    """`net-destroy` (s'il est actif) puis `net-undefine`, après un plan
    recalculé ; refus si une définition de domaine le cite. `expect_digest` :
    `digest` de l'aperçu montré, sinon OpError `plan-changed`."""
    ko.check_digest(expect_digest)

    def st(sid, status, msg=""):
        if step:
            step(sid, status, msg)

    st("plan", "running", name)
    plan = delete_network_plan(host, name)
    if plan["refusals"]:
        st("plan", "failed", "; ".join(plan["refusals"]))
        raise OpError("refused", "; ".join(plan["refusals"]), {"refusals": plan["refusals"]})
    if expect_digest is not None and expect_digest != plan["digest"]:
        msg = f"network {name} changed since the preview: nothing was done; preview again"
        st("plan", "failed", msg)
        raise OpError("plan-changed", msg, {"refusals": []})
    st("plan", "done", "nothing uses it")
    st("delete", "running", name)
    if plan["network"]["active"]:
        host.virsh("net-destroy", "--network", name)
    if plan["network"].get("persistent", True):
        host.virsh("net-undefine", "--network", name)
    st("delete", "done", name)
    return {"name": name, "deleted": True}
