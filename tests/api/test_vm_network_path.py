"""v1.36.0 : le chemin de connexion d'une VM.

« Cette VM est-elle branchée sur le bon réseau, par les bonnes cartes ? »
La réponse est une chaîne, de la VM jusqu'au cuivre, et elle se construit
en croisant trois sources : la VM (le DÉCLARÉ), le VMI (le RÉEL) et la
fabrique de l'hôte.

Quatre pièges payés en la construisant contre harv1 :

  * `podInterfaceName` du VMI NE désigne PAS un port de l'hôte : c'est une
    interface dans l'espace de noms du pod. Pire, les deux VMs qui tournaient
    portaient le MÊME nom (`pod8fe0d3f1ac5`) dans leurs pods respectifs, donc
    il ne discrimine rien. La chaîne part du bridge que nomme le NAD ;
  * depuis la VM on DESCEND vers l'uplink (bridge, bond, carte). Remonter
    vers le maître partait dans le vide, un bridge n'en ayant pas ;
  * les veth sont écartées du chemin : ce sont les ports des AUTRES charges ;
  * une VM arrêtée n'a pas de VMI, donc pas de nœud, et tous les maillons
    devenaient « non rapporté » comme si le cluster était cassé. Le chemin
    déclaré, lui, existe toujours.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


NODE = "n1.lo"

FABRIC = {
    "nodes": [{"name": NODE, "address": "10.0.0.1"}],
    "full_linkmonitor": True,
    "networks": [{"namespace": "default", "name": "production",
                  "kind": "UntaggedNetwork", "cni": "bridge",
                  "bridge": "mgmt-br", "vlan": None, "ready": True,
                  "fabric": "classic"}],
    "links": [
        {"node": NODE, "name": "mgmt-br", "type": "bridge", "state": "up",
         "mac": "dd:ee", "layer": 2, "fabric": "classic", "master": None},
        {"node": NODE, "name": "mgmt-bo", "type": "bond", "state": "up",
         "mac": "aa:bb", "layer": 1, "fabric": "classic", "master": "mgmt-br"},
        {"node": NODE, "name": "enp1s0", "type": "device", "state": "up",
         "mac": "aa:bb", "layer": 0, "fabric": "classic", "master": "mgmt-bo"},
        # Le port d'une AUTRE charge, sur le même bridge.
        {"node": NODE, "name": "vethzzz", "type": "veth", "state": "up",
         "mac": "11:22", "layer": 5, "fabric": "classic", "master": "mgmt-br"},
    ],
}


def vm_obj(mac="b6:c3:e4:bb:40:b7", network="default/production"):
    return {"spec": {"template": {"spec": {
        "domain": {"devices": {"interfaces": [
            {"name": "nic-1", "bridge": {}, "macAddress": mac,
             "model": "virtio"}]}},
        "networks": [{"name": "nic-1", "multus": {"networkName": network}}],
    }}}}


def vmi_obj(mac="b6:c3:e4:bb:40:b7"):
    return {"status": {"nodeName": NODE, "interfaces": [
        {"name": "nic-1", "interfaceName": "eth0", "ipAddress": "172.16.3.2",
         "ipAddresses": ["172.16.3.2"], "linkState": "up", "mac": mac,
         "podInterfaceName": "pod8fe0d3f1ac5", "infoSource": "guest-agent"}]}}


# Sentinelle : `vmi=None` doit vouloir dire « VM arrêtée », pas « prends le
# défaut ». Sans elle, les deux tests de VM arrêtée testaient une VM qui
# tourne et passaient pour la mauvaise raison.
_DEFAULT = object()


def build(monkeypatch, vm=_DEFAULT, vmi=_DEFAULT, fabric=None):
    calls = {"vm": vm_obj() if vm is _DEFAULT else vm,
             "vmi": vmi_obj() if vmi is _DEFAULT else vmi}

    def fake_json(kc, *args, **kw):
        # L'ordre compte : "vmi" contient "vm" seulement en sous-chaîne, mais
        # `in args` compare des éléments entiers, donc pas de piège ici.
        if "vmi" in args:
            return calls["vmi"]
        if "vm" in args:
            return calls["vm"]
        return None

    monkeypatch.setattr(wapp, "_kubectl_json", fake_json)
    monkeypatch.setattr(wapp, "_build_fabric",
                        lambda c, k: fabric if fabric is not None else FABRIC)
    return wapp._vm_network_path("c", "/kc", "default", "vm1")


# ---------------------------------------------------------------------------
# La chaîne
# ---------------------------------------------------------------------------

def test_the_chain_goes_down_to_the_physical_card(monkeypatch):
    """C'est tout l'objet de la vue : voir par quel cuivre la VM sort."""
    d = build(monkeypatch)
    assert [c["name"] for c in d["nics"][0]["chain"]] == \
        ["mgmt-br", "mgmt-bo", "enp1s0"]


def test_the_chain_ignores_the_ports_of_other_workloads(monkeypatch):
    """Une veth pendue au même bridge est le port d'une AUTRE VM : la
    suivre mènerait chez le voisin au lieu du monde extérieur.

    Le cas qui compte est un bridge SANS uplink : avec un bond présent, le
    tri suffirait à l'emporter et le test passerait sans rien prouver, ce
    qu'un sabotage a montré."""
    fabric = dict(FABRIC, links=[
        {"node": NODE, "name": "mgmt-br", "type": "bridge", "state": "up",
         "mac": "dd:ee", "layer": 2, "fabric": "classic", "master": None},
        {"node": NODE, "name": "vethzzz", "type": "veth", "state": "up",
         "mac": "11:22", "layer": 5, "fabric": "classic", "master": "mgmt-br"},
    ])
    names = [c["name"] for c in
             build(monkeypatch, fabric=fabric)["nics"][0]["chain"]]
    assert names == ["mgmt-br"], names
    # Et le cas nominal reste juste.
    assert "vethzzz" not in [c["name"] for c
                             in build(monkeypatch)["nics"][0]["chain"]]


def test_the_chain_starts_at_the_bridge_the_network_names(monkeypatch):
    """Et surtout PAS à `podInterfaceName` : il nomme une interface dans
    l'espace de noms du pod, et deux VMs différentes y portent le même."""
    d = build(monkeypatch)
    assert d["nics"][0]["chain"][0]["name"] == "mgmt-br"
    assert d["nics"][0]["live"]["host_port"] == "pod8fe0d3f1ac5"


def test_a_link_nobody_reports_is_flagged_not_dropped(monkeypatch):
    """Sans le moniteur permissif la chaîne s'arrête sur un bridge Open
    vSwitch. L'escamoter ferait croire à un chemin complet."""
    fabric = dict(FABRIC, links=[], full_linkmonitor=False)
    d = build(monkeypatch, fabric=fabric)
    chain = d["nics"][0]["chain"]
    assert chain == [{"name": "mgmt-br", "known": False}]


def test_a_chain_that_loops_stops(monkeypatch):
    fabric = dict(FABRIC, links=[
        {"node": NODE, "name": "a", "type": "bridge", "state": "up",
         "mac": "", "layer": 2, "fabric": "classic", "master": "b"},
        {"node": NODE, "name": "b", "type": "bridge", "state": "up",
         "mac": "", "layer": 2, "fabric": "classic", "master": "a"},
    ], networks=[dict(FABRIC["networks"][0], bridge="a")])
    assert len(build(monkeypatch, fabric=fabric)["nics"][0]["chain"]) <= 10


# ---------------------------------------------------------------------------
# Déclaré contre réel
# ---------------------------------------------------------------------------

def test_a_stopped_vm_still_shows_its_declared_path(monkeypatch):
    """Sans VMI il n'y a plus de nœud, et tous les maillons devenaient
    « non rapporté » comme si le cluster était cassé. Le chemin déclaré
    existe pourtant toujours."""
    d = build(monkeypatch, vmi=None)
    assert d["running"] is False
    assert d["chain_is_live"] is False
    assert [c["name"] for c in d["nics"][0]["chain"]] == \
        ["mgmt-br", "mgmt-bo", "enp1s0"]
    assert d["nics"][0]["live"]["ip"] is None


def test_a_running_vm_says_its_chain_is_live(monkeypatch):
    d = build(monkeypatch)
    assert d["running"] is True and d["chain_is_live"] is True
    assert d["nics"][0]["live"]["ip"] == "172.16.3.2"
    assert d["nics"][0]["live"]["guest_interface"] == "eth0"


def test_a_stopped_vm_on_a_multi_node_cluster_does_not_guess(monkeypatch):
    """Sur plusieurs nœuds, rien ne dit où elle démarrera : choisir au
    hasard afficherait une carte physique qui n'est pas la bonne."""
    fabric = dict(FABRIC, nodes=[{"name": "n1.lo"}, {"name": "n2.lo"}])
    d = build(monkeypatch, vmi=None, fabric=fabric)
    assert d["node"] is None
    assert d["nics"][0]["chain"] == []


def test_a_mac_that_drifted_is_reported(monkeypatch):
    """Un écart entre la MAC déclarée et celle qui tourne est exactement ce
    que l'exploitant vient chercher."""
    d = build(monkeypatch, vmi=vmi_obj(mac="00:11:22:33:44:55"))
    assert d["nics"][0]["mac_matches"] is False
    assert build(monkeypatch)["nics"][0]["mac_matches"] is True


def test_the_declared_side_survives_without_any_vmi(monkeypatch):
    d = build(monkeypatch, vmi=None)
    dec = d["nics"][0]["declared"]
    assert dec["binding"] == "bridge"
    assert dec["network"] == "default/production"


def test_a_network_named_without_a_namespace_gets_the_vm_one(monkeypatch):
    d = build(monkeypatch, vm=vm_obj(network="production"))
    assert d["nics"][0]["declared"]["network"] == "default/production"


def test_an_absent_vm_is_not_an_empty_path(monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_json", lambda kc, *a, **k: None)
    monkeypatch.setattr(wapp, "_build_fabric", lambda c, k: FABRIC)
    assert wapp._vm_network_path("c", "/kc", "default", "nope") is None


# ---------------------------------------------------------------------------
# Identification côté switch
# ---------------------------------------------------------------------------

def test_lldp_fields_are_read_from_the_capture():
    """NON VÉRIFIÉ contre une vraie trame : aucun équipement du réseau
    d'essai n'émet de LLDP (40 s d'écoute, zéro trame). Le décodage suit la
    sortie documentée de tcpdump."""
    text = ("12:00:00.1 LLDP, length 100\n"
            "  Chassis ID TLV (1), length 7: switch-a\n"
            "  Port ID TLV (2), length 5: Gi1/0/7\n"
            "  System Name TLV (5), length 8: switch-a\n"
            "  Port Description TLV (4), length 9: uplink-7\n")
    f = wapp._parse_lldp(text)
    assert f["system_name"] == "switch-a"
    assert f["port_id"] == "Gi1/0/7"
    assert f["port_description"] == "uplink-7"


def test_no_lldp_frame_is_not_an_error():
    """Beaucoup de switchs non administrables n'en émettent jamais :
    l'exploitant doit le savoir plutôt que de croire à une panne."""
    assert wapp._parse_lldp("0 packets captured") == {}
    assert wapp._parse_lldp("") == {}


def test_the_host_port_lookup_refuses_junk():
    """La MAC et le bridge partent dans une commande shell sur le nœud."""
    with wapp.app.test_client() as c:
        r = c.get("/api/vm-network-path/x/default/v/hostport"
                  "?mac=pasunemac&bridge=mgmt-br&node=n1")
        assert r.status_code == 400
        r = c.get("/api/vm-network-path/x/default/v/hostport"
                  "?mac=aa:bb:cc:dd:ee:ff&bridge=a;rm%20-rf&node=n1")
        assert r.status_code == 400


def test_the_lldp_probe_refuses_a_junk_interface():
    with wapp.app.test_client() as c:
        assert c.get("/api/network-fabric/x/node/n1/lldp"
                     "?iface=eth0;reboot").status_code == 400


def test_reading_a_path_needs_no_special_role():
    assert wapp.required_role_for(
        "/api/vm-network-path/c/default/v", "GET") == "viewer"


def test_the_unverified_limitation_is_written_down():
    """LLDP n'a pas pu être confronté à une vraie trame : le code doit le
    dire, sinon personne ne saura que c'est à reprendre."""
    src = (ROOT / "web" / "app.py").read_text()
    block = src.split("# Identification côté switch", 1)[1][:1400]
    assert "NON VÉRIFIÉ" in block
