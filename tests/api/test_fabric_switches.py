"""v1.39.0 : la fabrique lue comme des vSwitch, côté serveur.

Deux choses que la vue par switch demande à l'API et que le graphe empilé
ne demandait pas :

  * QUI est branché sur chaque réseau. C'est ce qu'un exploitant cherche en
    premier sous un port group, et ce qu'ESXi montre. Les VMs viennent dans
    le MÊME appel kubectl groupé, pas dans un appel de plus ;
  * un cluster SANS kube-ovn. `kubectl get a,b,c` échoue EN BLOC quand l'un
    des types n'existe pas : un Harvester sans l'addon n'a pas ses CRD, et
    toute la fabrique passait pour « cluster injoignable ».
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_memory():
    wapp._fabric_missing.clear()
    yield
    wapp._fabric_missing.clear()


def node_obj(name="n1"):
    return {"kind": "Node",
            "metadata": {"name": name, "labels": {}, "annotations": {}},
            "status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.1"}]}}


def vm_obj(name, ns="default", networks=None, status="Running"):
    return {"kind": "VirtualMachine",
            "metadata": {"name": name, "namespace": ns},
            "spec": {"template": {"spec": {"networks": networks or []}}},
            "status": {"printableStatus": status}}


def build(items, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_json",
                        lambda kc, *a, **k: {"items": items})
    return wapp._build_fabric("c", "/kc")


# ---------------------------------------------------------------------------
# Qui est branché où
# ---------------------------------------------------------------------------

def test_the_vms_come_in_the_same_grouped_call():
    """Un appel de plus par rafraîchissement, toutes les 8 s, c'est 0,7 s
    de démarrage de kubectl à chaque fois."""
    assert "virtualmachines.kubevirt.io" in wapp.FABRIC_KINDS


def test_a_vm_attachment_names_the_network_it_uses(monkeypatch):
    items = [node_obj(), vm_obj("web", networks=[
        {"name": "nic-1", "multus": {"networkName": "default/production"}}])]
    vm = build(items, monkeypatch)["vms"][0]
    assert (vm["namespace"], vm["name"], vm["status"]) == ("default", "web", "Running")
    net = vm["networks"][0]
    assert (net["nic"], net["network"], net["pod"]) == ("nic-1", "default/production", False)


def test_a_bare_network_name_belongs_to_the_vm_namespace(monkeypatch):
    """KubeVirt accepte `networkName: production` et le résout dans le
    namespace de la VM. Le comparer tel quel à `ns/nom` ratait la moitié
    des rattachements."""
    items = [node_obj(), vm_obj("web", ns="team-a", networks=[
        {"name": "nic-1", "multus": {"networkName": "production"}}])]
    net = build(items, monkeypatch)["vms"][0]["networks"][0]
    assert net["network"] == "team-a/production"


def test_the_pod_network_is_flagged_not_invented(monkeypatch):
    """Le réseau de pod n'a pas de NAD : il sort par le routage du nœud. Le
    rattacher à un switch serait un mensonge."""
    items = [node_obj(), vm_obj("web", networks=[{"name": "default", "pod": {}}])]
    net = build(items, monkeypatch)["vms"][0]["networks"][0]
    assert (net["nic"], net["network"], net["pod"]) == ("default", None, True)


def test_a_stopped_vm_is_listed_with_its_state(monkeypatch):
    items = [node_obj(), vm_obj("idle", status="Stopped", networks=[
        {"name": "nic-1", "multus": {"networkName": "default/production"}}])]
    assert build(items, monkeypatch)["vms"][0]["status"] == "Stopped"


def vmi_obj(name, ns="default", node="n1", interfaces=None):
    return {"kind": "VirtualMachineInstance",
            "metadata": {"name": name, "namespace": ns},
            "status": {"nodeName": node, "interfaces": interfaces or []}}


def running_web(interfaces, decl=None):
    vm = vm_obj("web", networks=[
        {"name": "nic-1", "multus": {"networkName": "default/production"}}])
    vm["spec"]["template"]["spec"]["domain"] = {"devices": {"interfaces": [
        decl or {"name": "nic-1", "bridge": {}, "model": "virtio",
                 "macAddress": "02:00:00:00:00:01"}]}}
    return [node_obj(), vm, vmi_obj("web", interfaces=interfaces)]


def test_the_vms_live_interfaces_come_in_the_same_call():
    """La vue Réseau montre la MAC et les adresses que la VM a VRAIMENT.
    Elles sont dans le VMI, qui vient dans le même appel groupé."""
    assert "virtualmachineinstances.kubevirt.io" in wapp.FABRIC_KINDS


def test_a_running_vm_shows_what_it_really_has(monkeypatch):
    items = running_web([{"name": "nic-1", "mac": "ce:0f:db:1f:33:ee",
                          "ipAddress": "172.16.3.43",
                          "ipAddresses": ["172.16.3.43", "fe80::1"],
                          "interfaceName": "eth0", "linkState": "up"}])
    vm = build(items, monkeypatch)["vms"][0]
    net = vm["networks"][0]
    assert vm["node"] == "n1"
    # La MAC vue par la VM prime sur la déclarée : c'est elle qu'on cherche
    # dans la table d'un switch.
    assert net["mac"] == "ce:0f:db:1f:33:ee"
    assert net["ips"] == ["172.16.3.43", "fe80::1"]
    assert (net["guest_iface"], net["link_state"]) == ("eth0", "up")
    assert (net["model"], net["binding"]) == ("virtio", "bridge")


def test_a_stopped_vm_keeps_its_declared_mac(monkeypatch):
    items = running_web([])[:2]                 # pas de VMI
    net = build(items, monkeypatch)["vms"][0]["networks"][0]
    assert net["mac"] == "02:00:00:00:00:01"
    assert net["ips"] == [] and net["link_state"] is None


def test_an_interface_known_only_to_the_guest_is_kept_apart(monkeypatch):
    """docker0 et consorts n'ont pas de nom KubeVirt : ils ne sortent par
    aucun réseau du cluster. Les ranger sous `production` serait faux."""
    items = running_web([
        {"name": "nic-1", "mac": "ce:0f", "ipAddress": "172.16.3.43",
         "interfaceName": "eth0"},
        {"interfaceName": "docker0", "mac": "ca:df", "ipAddress": "172.17.0.1",
         "ipAddresses": ["172.17.0.1"]}])
    vm = build(items, monkeypatch)["vms"][0]
    assert len(vm["networks"]) == 1
    assert vm["guest_only"] == [{"iface": "docker0", "mac": "ca:df",
                                 "ips": ["172.17.0.1"]}]


# ---------------------------------------------------------------------------
# Un cluster sans kube-ovn
# ---------------------------------------------------------------------------

KUBEOVN = {"provider-networks.kubeovn.io", "subnets.kubeovn.io",
           "vpcs.kubeovn.io", "vlans.kubeovn.io"}


def fake_cluster(absent, calls, nodes_ok=True):
    """kubectl simulé : échoue sur tout appel qui touche un type absent,
    comme le vrai (« the server doesn't have a resource type »)."""
    def run(kc, *args, **kw):
        kinds = args[args.index("-A") + 1].split(",")
        calls.append(kinds)
        if any(k in absent for k in kinds):
            return None
        if not nodes_ok and "nodes" in kinds:
            return None
        items = []
        if "nodes" in kinds:
            items.append(node_obj())
        if "clusternetworks.network.harvesterhci.io" in kinds:
            items.append({"kind": "ClusterNetwork", "metadata": {"name": "mgmt"}})
        return {"items": items}
    return run


def test_a_cluster_without_kubeovn_still_has_a_fabric(monkeypatch):
    calls = []
    monkeypatch.setattr(wapp, "_kubectl_json", fake_cluster(KUBEOVN, calls))
    out = wapp._build_fabric("plain", "/kc")
    assert out is not None, "un type absent faisait passer le cluster pour mort"
    assert [n["name"] for n in out["nodes"]] == ["n1"]
    assert out["cluster_networks"] == [{"name": "mgmt", "layer": 3}]
    assert out["kubeovn"]["subnets"] == []


def test_the_missing_kinds_are_remembered(monkeypatch):
    """Sans mémoire, chaque rafraîchissement paierait l'appel groupé raté
    PLUS un appel par type : dix démarrages de kubectl toutes les 8 s."""
    calls = []
    monkeypatch.setattr(wapp, "_kubectl_json", fake_cluster(KUBEOVN, calls))
    wapp._build_fabric("plain", "/kc")
    calls.clear()
    wapp._build_fabric("plain", "/kc")
    assert len(calls) == 1, calls
    assert not KUBEOVN & set(calls[0])


def test_the_memory_expires(monkeypatch):
    """Un addon activé plus tard doit finir par apparaître."""
    calls = []
    monkeypatch.setattr(wapp, "_kubectl_json", fake_cluster(KUBEOVN, calls))
    wapp._build_fabric("plain", "/kc")
    missing, ts = wapp._fabric_missing["plain"]
    wapp._fabric_missing["plain"] = (missing, ts - wapp.FABRIC_MISSING_TTL - 1)
    calls.clear()
    wapp._build_fabric("plain", "/kc")
    assert KUBEOVN <= set(calls[0]), "la mémoire n'expirait jamais"


def test_the_memory_is_per_cluster(monkeypatch):
    calls = []
    monkeypatch.setattr(wapp, "_kubectl_json", fake_cluster(KUBEOVN, calls))
    wapp._build_fabric("plain", "/kc")
    calls.clear()
    monkeypatch.setattr(wapp, "_kubectl_json", fake_cluster(set(), calls))
    wapp._build_fabric("full", "/kc")
    assert KUBEOVN <= set(calls[0])


def test_unreadable_nodes_mean_the_cluster_is_down(monkeypatch):
    """Sans les nœuds il n'y a rien à dessiner : c'est bien le cluster qui
    ne répond pas, et il faut le dire comme tel."""
    calls = []
    monkeypatch.setattr(wapp, "_kubectl_json",
                        fake_cluster(set(), calls, nodes_ok=False))
    assert wapp._build_fabric("down", "/kc") is None
    assert "down" not in wapp._fabric_missing


def test_a_healthy_cluster_costs_one_call(monkeypatch):
    calls = []
    monkeypatch.setattr(wapp, "_kubectl_json", fake_cluster(set(), calls))
    wapp._build_fabric("full", "/kc")
    assert len(calls) == 1
    assert calls[0] == wapp.FABRIC_KINDS
