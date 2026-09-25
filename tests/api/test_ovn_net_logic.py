"""v1.49.0 : la logique des réseaux kube-ovn (VPC, subnets, overlay).

Les objets viennent de harv1 (Harvester v1.9.0, kube-ovn v1.16.2), relevés
le 25/09/2026 : un VPC, trois subnets dont un underlay VLAN, et le réseau
overlay `default/ovn-overlay` qu'aucun subnet ne sert.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import ovn_net as on  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "ovn"


def load(name):
    return json.loads((FIX / name).read_text())


def harv1():
    return on.model(load("vpcs.json"), load("subnets.json"), load("nads.json"),
                    load("ips.json"), ["172.16.3.11"])


def facts(**over):
    m = harv1()
    f = {"vpcs": m["vpcs"], "subnets": [s for v in m["vpcs"] for s in v["subnet_list"]],
         "overlays": m["overlays"], "node_ips": ["172.16.3.11"], "pod_cidrs": ["10.52.0.0/24"],
         "namespaces": ["default", "kube-system", "apps"],
         "ips": [on.ip_info(i) for i in load("ips.json")]}
    f.update(over)
    return f


def codes(found, level=None):
    return [f["code"] for f in found if level is None or f["level"] == level]


def req(**over):
    base = {"name": "lab-a", "cidr": "10.200.0.0/24", "new_network": "default/lab-a"}
    base.update(over)
    return on.normalize_subnet(base)


# -- lecture de harv1 ---------------------------------------------------------

def test_the_real_cluster_reads_as_one_vpc_with_three_subnets():
    m = harv1()
    assert [v["name"] for v in m["vpcs"]] == ["ovn-cluster"] and m["vpcs"][0]["system"]
    subs = {s["name"]: s for s in m["vpcs"][0]["subnet_list"]}
    assert set(subs) == {"egress-external", "join", "ovn-default"}
    assert subs["ovn-default"]["nat"] and subs["ovn-default"]["system"]
    assert subs["egress-external"]["underlay"] and subs["egress-external"]["system"]
    assert subs["egress-external"]["network"] == "kube-system/external"


def test_the_overlay_without_subnet_is_found():
    """`default/ovn-overlay` : une VM qui s'y branche n'aurait aucune adresse."""
    found = harv1()["findings"]
    assert {"code": "overlay-no-subnet", "level": "warn", "network": "default/ovn-overlay"} in found
    assert "cidr-overlap" not in codes(found)


def test_vms_on_a_subnet_are_named():
    m = harv1()
    ovn_default = next(s for s in m["vpcs"][0]["subnet_list"] if s["name"] == "ovn-default")
    assert ovn_default["consumers"] and all(c["kind"] == "VirtualMachine" for c in ovn_default["consumers"])


# -- propositions ------------------------------------------------------------

def test_a_free_cidr_is_suggested_away_from_everything():
    f = facts()
    assert on.suggest_cidr(f) == "10.200.0.0/24"
    f["subnets"] = f["subnets"] + [{"name": "x", "cidr": "10.200.0.0/23"}]
    assert on.suggest_cidr(f) == "10.200.2.0/24"


def test_gateway_and_exclusion_are_derived():
    s = req(cidr="10.200.5.7/24")
    assert s["cidr"] == "10.200.5.0/24" and s["gateway"] == "10.200.5.1"
    assert s["exclude"] == ["10.200.5.1"]
    assert req(gateway="10.200.0.254", exclude="10.200.0.10..10.200.0.20")["exclude"] == [
        "10.200.0.254", "10.200.0.10..10.200.0.20"]


def test_unknown_keys_are_refused():
    with pytest.raises(ValueError):
        on.normalize_subnet({"name": "a", "cidrBlock": "10.0.0.0/24"})
    with pytest.raises(ValueError):
        on.normalize_vpc({"name": "a", "enableExternal": True})


# -- contrôle d'une demande de subnet ------------------------------------------

def test_a_good_request_passes():
    found = on.check_subnet(req(nat=True), facts())
    assert on.blocking(found) == [], found


@pytest.mark.parametrize("over, code", [
    ({"name": "Lab A"}, "invalid-name"),
    ({"name": "ovn-default"}, "subnet-exists"),
    ({"vpc": "nope"}, "vpc-missing"),
    ({"cidr": "10.200.0.0/33"}, "invalid-cidr"),
    ({"cidr": "10.200.0.0/30"}, "cidr-too-small"),
    ({"cidr": "10.54.3.0/24"}, "cidr-overlap"),            # ovn-default
    ({"cidr": "10.53.0.0/24"}, "cidr-overlap"),            # services
    ({"cidr": "172.16.3.0/24"}, "cidr-overlap"),           # egress-external
    ({"gateway": "10.201.0.1"}, "gateway-outside"),
    ({"exclude": "10.9.9.9"}, "exclude-outside"),
    ({"network": "default/ovn-overlay", "new_network": "default/x"}, "network-both"),
    ({"new_network": "", "network": "default/nope"}, "network-missing"),
    ({"new_network": "", "network": "kube-system/external"}, "network-taken"),
    ({"new_network": "default/ovn-overlay"}, "network-exists"),
    ({"new_network": "nope/lab"}, "namespace-missing"),
    ({"new_network": "Default/lab"}, "invalid-network-name"),
    ({"allow": "not-a-cidr"}, "invalid-allow"),
])
def test_each_blocker(over, code):
    assert code in codes(on.check_subnet(req(**over), facts()), "block")


def test_a_cidr_over_the_nodes_is_refused():
    f = facts(subnets=[], pod_cidrs=[])
    assert "cidr-covers-nodes" in codes(on.check_subnet(req(cidr="172.16.0.0/16"), f), "block")


def test_nat_only_in_the_default_vpc():
    f = facts()
    f["vpcs"] = f["vpcs"] + [on.vpc_info({"metadata": {"name": "lab"}, "spec": {}})]
    assert "nat-custom-vpc" in codes(on.check_subnet(req(vpc="lab", nat=True), f), "block")
    found = on.check_subnet(req(vpc="lab"), f)
    assert on.blocking(found) == [] and "vpc-isolated" in codes(found, "ok")


def test_the_existing_free_overlay_can_be_served():
    found = on.check_subnet(req(new_network="", network="default/ovn-overlay"), facts())
    assert on.blocking(found) == []


def test_an_update_cannot_move_a_subnet():
    f = facts()
    f["subnets"] = f["subnets"] + [on.subnet_info({"metadata": {"name": "lab-a"}, "spec": {
        "vpc": "ovn-cluster", "cidrBlock": "10.200.0.0/24", "gateway": "10.200.0.1",
        "provider": "lab-a.default.ovn"}})]
    f["overlays"] = f["overlays"] + [on.nad_info(on.overlay_manifest("default/lab-a"))]
    ok = on.check_subnet(req(new_network="", network="default/lab-a", nat=True), f, updating=True)
    assert on.blocking(ok) == [], ok
    moved = on.check_subnet(req(new_network="", network="default/lab-a", cidr="10.200.1.0/24"),
                            f, updating=True)
    assert "subnet-immutable" in codes(moved, "block")


def test_system_subnets_are_read_only():
    found = on.check_subnet(on.normalize_subnet({"name": "ovn-default", "cidr": "10.54.0.0/16"}),
                            facts(), updating=True)
    assert "subnet-system" in codes(found, "block")


# -- VPC ---------------------------------------------------------------------

def test_vpc_checks():
    f = facts()
    assert on.blocking(on.check_vpc(on.normalize_vpc({"name": "lab", "namespaces": "apps"}), f)) == []
    bad = on.check_vpc(on.normalize_vpc({
        "name": "lab", "namespaces": "nope", "static_routes": ["0.0.0.0/0>notip"],
        "peerings": ["ghost@10.255.0.1/30"]}), f)
    assert set(codes(bad, "block")) >= {"namespace-missing", "invalid-route", "peer-missing"}
    assert "vpc-system" in codes(on.check_vpc(on.normalize_vpc({"name": "ovn-cluster"}), f,
                                              updating=True))


# -- suppressions --------------------------------------------------------------

def test_deleting_what_is_used_is_refused():
    f = facts()
    assert "vpc-system" in codes(on.check_delete("vpc", "ovn-cluster", f))
    assert "subnet-system" in codes(on.check_delete("subnet", "ovn-default", f))
    f["vpcs"] = f["vpcs"] + [on.vpc_info({"metadata": {"name": "lab"}})]
    f["subnets"] = f["subnets"] + [{"name": "lab-a", "vpc": "lab", "system": False}]
    assert "vpc-has-subnets" in codes(on.check_delete("vpc", "lab", f))
    f["ips"] = f["ips"] + [{"subnet": "lab-a", "owner": "web", "namespace": "apps"}]
    found = on.check_delete("subnet", "lab-a", f)
    assert found[0]["code"] == "subnet-in-use" and found[0]["facts"]["users"] == "apps/web"
    f["ips"] = []
    assert on.check_delete("subnet", "lab-a", f) == []


# -- manifestes ----------------------------------------------------------------

def test_the_overlay_manifest_is_what_harvester_recognises():
    """Même forme que `default/ovn-overlay`, créé par Harvester et reconnu par
    son contrôleur (étiquette OverlayNetwork, provider nom.ns.ovn)."""
    m = on.overlay_manifest("default/lab-a")
    conf = json.loads(m["spec"]["config"])
    assert m["metadata"]["labels"][on.NET_TYPE] == "OverlayNetwork"
    assert conf == {"cniVersion": "0.3.1", "type": "kube-ovn",
                    "server_socket": "/run/openvswitch/kube-ovn-daemon.sock",
                    "provider": "lab-a.default.ovn"}


def test_the_subnet_manifest():
    m = on.subnet_manifest(req(nat=True))
    sp = m["spec"]
    assert sp["provider"] == "lab-a.default.ovn" and sp["cidrBlock"] == "10.200.0.0/24"
    assert sp["gateway"] == "10.200.0.1" and sp["excludeIps"] == ["10.200.0.1"]
    assert sp["natOutgoing"] and sp["enableDHCP"] and sp["vpc"] == "ovn-cluster"
    assert m["metadata"]["labels"][on.MANAGED] == "true"


def test_the_vpc_manifest():
    m = on.vpc_manifest(on.normalize_vpc({"name": "lab", "namespaces": ["apps"],
                                          "static_routes": [{"cidr": "0.0.0.0/0", "next_hop": "10.0.1.254"}],
                                          "peerings": [{"remote": "ovn-cluster", "local_ip": "10.255.0.1/30"}]}))
    assert m["spec"]["staticRoutes"] == [{"cidr": "0.0.0.0/0", "nextHopIP": "10.0.1.254", "policy": "policyDst"}]
    assert m["spec"]["vpcPeerings"] == [{"remoteVpc": "ovn-cluster", "localConnectIP": "10.255.0.1/30"}]
