"""v1.35.0 : la fabrique réseau de l'hôte, vue par le bas.

La vue « network » existante regarde le réseau par le haut (quelles VMs sur
quel réseau). Celle-ci le regarde du côté de l'exploitant : quelle carte
physique porte quoi, et par quelle pile.

Le modèle est un EMPILEMENT, établi en lisant harv1 plutôt qu'en le
supposant :

    5  VMs et ports de charges
    4  réseaux attachables (NetworkAttachmentDefinition)
    3  ClusterNetwork  |  ProviderNetwork + VLAN + Subnet/VPC
    2  switch virtuel : bridge Linux  |  Open vSwitch
    1  bond
    0  interfaces physiques

Quatre pièges, tous payés en testant contre le vrai cluster :

  * la couche BOND n'est pas cosmétique : c'est là que vit le VlanConfig
    (mode d'agrégation, MTU), donc la redondance d'uplink ;
  * DEUX fabriques coexistent au-dessus des cartes et ne se confondent pas.
    Sur harv1 elles n'utilisent même pas la même carte ;
  * un même lien est rapporté par CHAQUE moniteur qui le capte : poser un
    moniteur permissif faisait sortir `enp1s0` et `mgmt-br` en double ;
  * la fabrique d'un lien se lit sur sa CHAÎNE de maîtres, pas sur son nom.
    Une veth `5a9ba3611271_h` ne dit rien d'elle-même, mais elle pend
    d'`ovs-system`, donc elle est du côté kube-ovn. Classées sur leur nom,
    81 des 83 veth de harv1 étaient rangées du mauvais côté.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


def link(name, index, typ, master=None, node="n1", state="up"):
    return {"name": name, "index": index, "type": typ, "state": state,
            "masterIndex": master, "mac": f"00:00:00:00:00:{index:02x}"}


def monitor(name, node, links):
    return {"kind": "LinkMonitor", "metadata": {"name": name},
            "status": {"linkStatus": {node: links}}}


def node_obj(name="n1", labels=None, ip="10.0.0.1"):
    return {"kind": "Node",
            "metadata": {"name": name, "labels": labels or {}, "annotations": {}},
            "status": {"addresses": [{"type": "InternalIP", "address": ip}]}}


def build(items, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_json",
                        lambda kc, *a, **k: {"items": items})
    return wapp._build_fabric("c", "/kc")


# ---------------------------------------------------------------------------
# L'empilement
# ---------------------------------------------------------------------------

def test_each_kind_of_link_lands_on_its_own_floor(monkeypatch):
    items = [node_obj(), monitor("all", "n1", [
        link("enp1s0", 2, "device", master=5),
        link("mgmt-bo", 5, "bond", master=6),
        link("mgmt-br", 6, "bridge"),
    ])]
    layers = {l["name"]: l["layer"] for l in build(items, monkeypatch)["links"]}
    assert layers == {"enp1s0": 0, "mgmt-bo": 1, "mgmt-br": 2}


def test_workload_ports_do_not_crowd_the_switch_floor(monkeypatch):
    """83 paires veth sur un seul nœud noyaient la couche des switchs, et le
    peu qu'on voulait montrer disparaissait."""
    items = [node_obj(), monitor("all", "n1",
                                 [link("mgmt-br", 6, "bridge")]
                                 + [link(f"v{i}_h", 20 + i, "veth", master=7)
                                    for i in range(40)])]
    links = build(items, monkeypatch)["links"]
    assert [l["name"] for l in links if l["layer"] == 2] == ["mgmt-br"]
    assert len([l for l in links if l["layer"] == 5]) == 40


def test_the_bond_floor_exists_between_nic_and_switch(monkeypatch):
    """C'est la couche où vit le VlanConfig. La masquer cacherait la seule
    qu'on configure vraiment."""
    items = [node_obj(), monitor("all", "n1", [
        link("enp1s0", 2, "device", master=5), link("mgmt-bo", 5, "bond")])]
    out = build(items, monkeypatch)
    nic = next(l for l in out["links"] if l["name"] == "enp1s0")
    assert nic["layer"] == 0 and nic["master"] == "mgmt-bo"
    assert next(l for l in out["links"] if l["name"] == "mgmt-bo")["layer"] == 1


# ---------------------------------------------------------------------------
# Deux fabriques, pas une
# ---------------------------------------------------------------------------

def test_fabric_is_read_from_the_master_chain_not_the_name(monkeypatch):
    """Une veth nommée au hasard ne dit rien d'elle-même ; son maître si."""
    items = [node_obj(), monitor("all", "n1", [
        link("5a9ba3611271_h", 16, "veth", master=7),
        link("ovs-system", 7, "openvswitch"),
        link("vethabc", 17, "veth", master=6),
        link("mgmt-br", 6, "bridge"),
    ])]
    fab = {l["name"]: l["fabric"] for l in build(items, monkeypatch)["links"]}
    assert fab["5a9ba3611271_h"] == "ovn", "classée sur son nom seul"
    assert fab["vethabc"] == "classic"


def test_a_physical_nic_follows_the_switch_it_feeds(monkeypatch):
    """Sur harv1 `eno2` alimente Open vSwitch : la ranger côté classique
    ferait croire à deux uplinks pour la même pile."""
    items = [node_obj(), monitor("all", "n1", [
        link("eno2", 3, "device", master=7, state="down"),
        link("ovs-system", 7, "openvswitch"),
        link("enp1s0", 2, "device", master=5),
        link("mgmt-bo", 5, "bond", master=6),
        link("mgmt-br", 6, "bridge"),
    ])]
    fab = {l["name"]: l["fabric"] for l in build(items, monkeypatch)["links"]}
    assert fab["eno2"] == "ovn"
    assert fab["enp1s0"] == "classic"


def test_a_chain_that_loops_does_not_hang(monkeypatch):
    items = [node_obj(), monitor("all", "n1", [
        link("a", 1, "veth", master=2), link("b", 2, "veth", master=1)])]
    assert {l["fabric"] for l in build(items, monkeypatch)["links"]} == {"classic"}


# ---------------------------------------------------------------------------
# Le même lien vu par plusieurs moniteurs
# ---------------------------------------------------------------------------

def test_a_link_seen_by_two_monitors_appears_once(monkeypatch):
    """Poser le moniteur permissif faisait sortir `enp1s0`, `mgmt-bo` et
    `mgmt-br` en double, chacun rapporté par deux moniteurs."""
    l1 = link("enp1s0", 2, "device", master=5)
    items = [node_obj(),
             monitor("nic", "n1", [l1]),
             monitor("harvester-ops-fabric", "n1", [l1, link("mgmt-bo", 5, "bond")])]
    names = [l["name"] for l in build(items, monkeypatch)["links"]]
    assert names.count("enp1s0") == 1, names


def test_the_same_index_on_two_nodes_is_two_links(monkeypatch):
    """La clé de vérité est (nœud, index), pas l'index seul : sinon un
    cluster à trois nœuds perdrait deux cartes sur trois."""
    l = link("enp1s0", 2, "device")
    items = [node_obj("n1"), node_obj("n2"),
             monitor("nic", "n1", [l]), monitor("nic", "n2", [l])]
    assert len(build(items, monkeypatch)["links"]) == 2


# ---------------------------------------------------------------------------
# Ce que l'on ne sait pas, on le DIT
# ---------------------------------------------------------------------------

def test_an_unreported_master_is_flagged_not_invented(monkeypatch):
    """Sans moniteur permissif, les bridges Open vSwitch ne sont rapportés
    par aucun moniteur et la chaîne s'arrête. Deviner le maître donnerait
    une topologie fausse avec l'aplomb d'une vraie."""
    items = [node_obj(), monitor("nic", "n1",
                                 [link("eno2", 3, "device", master=7)])]
    l = build(items, monkeypatch)["links"][0]
    assert l["master"] is None
    assert l["master_unresolved"] is True
    assert l["master_index"] == 7


def test_a_link_without_any_master_is_not_flagged(monkeypatch):
    items = [node_obj(), monitor("nic", "n1", [link("wlo1", 4, "device")])]
    l = build(items, monkeypatch)["links"][0]
    assert l["master"] is None and l["master_unresolved"] is False


def test_the_payload_says_whether_the_full_monitor_is_in_place(monkeypatch):
    """L'écran propose de le poser : il doit savoir s'il y est déjà."""
    items = [node_obj(), monitor("nic", "n1", [])]
    assert build(items, monkeypatch)["full_linkmonitor"] is False
    items.append(monitor(wapp.FABRIC_LINKMONITOR, "n1", []))
    assert build(items, monkeypatch)["full_linkmonitor"] is True


def test_the_node_carries_the_address_the_detail_needs(monkeypatch):
    """Le nom Kubernetes (`harv1.home.lo`) n'est pas le nom déclaré dans la
    config (`harv1-node1`) : sans adresse, l'écran ne saurait à qui demander
    le détail."""
    out = build([node_obj("harv1.home.lo", ip="172.16.3.11")], monkeypatch)
    assert out["nodes"][0]["address"] == "172.16.3.11"


# ---------------------------------------------------------------------------
# Le reste de l'empilement
# ---------------------------------------------------------------------------

def test_the_declarative_layers_are_reduced(monkeypatch):
    items = [
        node_obj(labels={"network.harvesterhci.io/mgmt": "true",
                         "external.provider-network.kubernetes.io/interface": "eno2",
                         "external.provider-network.kubernetes.io/ready": "true"}),
        {"kind": "ClusterNetwork", "metadata": {"name": "mgmt"}},
        {"kind": "VlanConfig", "metadata": {"name": "vc"},
         "spec": {"clusterNetwork": "mgmt",
                  "uplink": {"nics": ["enp1s0"],
                             "bondOptions": {"mode": "active-backup"},
                             "linkAttributes": {"mtu": 1500}}}},
        {"kind": "NetworkAttachmentDefinition",
         "metadata": {"namespace": "default", "name": "production",
                      "labels": {"network.harvesterhci.io/clusternetwork": "mgmt",
                                 "network.harvesterhci.io/type": "UntaggedNetwork"}},
         "spec": {"config": '{"type":"bridge","bridge":"mgmt-br"}'}},
        {"kind": "Subnet", "metadata": {"name": "ovn-default"},
         "spec": {"vpc": "ovn-cluster", "cidrBlock": "10.54.0.0/16",
                  "natOutgoing": True}},
    ]
    out = build(items, monkeypatch)
    assert out["cluster_networks"][0]["name"] == "mgmt"
    vc = out["vlan_configs"][0]
    assert vc["nics"] == ["enp1s0"] and vc["bond_mode"] == "active-backup"
    assert vc["mtu"] == 1500 and vc["layer"] == 1
    nad = out["networks"][0]
    assert nad["bridge"] == "mgmt-br" and nad["fabric"] == "classic"
    assert out["kubeovn"]["subnets"][0]["nat"] is True
    assert out["nodes"][0]["provider_bindings"]["external"]["interface"] == "eno2"


def test_a_kube_ovn_network_is_not_called_classic(monkeypatch):
    items = [node_obj(), {
        "kind": "NetworkAttachmentDefinition",
        "metadata": {"namespace": "default", "name": "ovn-overlay", "labels": {}},
        "spec": {"config": '{"type":"kube-ovn","provider":"ovn-overlay.default.ovn"}'}}]
    assert build(items, monkeypatch)["networks"][0]["fabric"] == "ovn"


def test_a_broken_nad_config_does_not_sink_the_view(monkeypatch):
    items = [node_obj(), {
        "kind": "NetworkAttachmentDefinition",
        "metadata": {"namespace": "d", "name": "n", "labels": {}},
        "spec": {"config": "pas du json"}}]
    assert build(items, monkeypatch)["networks"][0]["bridge"] is None


def test_a_failed_kubectl_gives_none_rather_than_half_a_map(monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_json", lambda kc, *a, **k: None)
    assert wapp._build_fabric("c", "/kc") is None


# ---------------------------------------------------------------------------
# Le moniteur permissif est une ÉCRITURE : elle se trace et se retire
# ---------------------------------------------------------------------------

def test_the_proposed_monitor_matches_every_link():
    """Le CRD documente « empty value means matching all » : c'est ce qui
    fait apparaître les bridges Open vSwitch."""
    import json
    m = json.loads(wapp._fabric_linkmonitor_manifest())
    assert m["kind"] == "LinkMonitor"
    assert m["spec"]["targetLinkRule"] == {}
    assert m["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "harvester-ops"


def test_posing_the_monitor_is_a_tracked_action():
    for method in ("POST", "DELETE"):
        assert wapp.required_role_for(
            "/api/network-fabric/c/linkmonitor", method) != "viewer"


def test_reading_the_fabric_needs_no_special_role():
    assert wapp.required_role_for("/api/network-fabric/c", "GET") == "viewer"


# ---------------------------------------------------------------------------
# Le détail pris sur le nœud
# ---------------------------------------------------------------------------

def test_node_detail_parses_links_and_counters():
    text = ('[{"ifname":"mgmt-bo","operstate":"UP","mtu":1500,'
            '"address":"aa:bb","master":"mgmt-br",'
            '"linkinfo":{"info_kind":"bond","info_data":{"mode":"active-backup",'
            '"miimon":100}}}]\n---\n'
            '[{"ifname":"mgmt-bo","stats64":{"rx":{"bytes":10,"errors":1,'
            '"dropped":2},"tx":{"bytes":20,"errors":0,"dropped":0}}}]')
    links, stats, phys = wapp._parse_fabric_detail(text)
    assert links[0]["kind"] == "bond"
    assert links[0]["bond_mode"] == "active-backup"
    assert links[0]["bond_miimon"] == 100
    assert stats["mgmt-bo"]["rx_errors"] == 1
    assert stats["mgmt-bo"]["tx_bytes"] == 20


def test_node_detail_survives_an_empty_or_broken_answer():
    assert wapp._parse_fabric_detail("") == ([], {}, {})
    assert wapp._parse_fabric_detail("pas du json\n---\nnon plus") == ([], {}, {})


def test_node_detail_reads_speed_duplex_and_carrier():
    """Ni l'API Kubernetes ni `ip link` ne donnent le débit négocié : il
    vient de /sys. `carrier_changes` est le plus parlant des trois, un lien
    qui bat est un lien qui va tomber."""
    text = "[]\n---\n[]\n---\nenp1s0\t1000\tfull\t1\t2\neno2\t-1\tunknown\t0\t1\n"
    _, _, phys = wapp._parse_fabric_detail(text)
    assert phys["enp1s0"] == {"speed_mbps": 1000, "duplex": "full",
                              "carrier": 1, "carrier_changes": 2}
    # Une carte sans porteuse rapporte -1 : l'afficher tel quel donnerait
    # « -1 Mb/s » au lieu de « pas de lien ».
    assert phys["eno2"]["speed_mbps"] is None
    assert phys["eno2"]["carrier"] == 0
