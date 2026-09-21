"""v1.35.0 : la vue Fabrique, dans un vrai navigateur.

Ce que les tests de source ne voient pas : que l'empilement se DESSINE, que
la couche 0 est bien en bas, et que le bandeau qui propose le moniteur de
liens fait ce qu'il annonce.

Le réseau est intercepté : aucun test ne touche à un cluster.
"""

import json

import pytest

playwright = pytest.importorskip("playwright")


FABRIC = {
    "cluster": "harv-fake",
    "full_linkmonitor": False,
    "nodes": [{"name": "n1.lo", "address": "10.0.0.1", "mgmt": True,
               "ovn_role": "master", "provider_bindings": {}}],
    "links": [
        {"node": "n1.lo", "name": "enp1s0", "type": "device", "state": "up",
         "mac": "aa:bb", "index": 2, "master_index": 5, "master": "mgmt-bo",
         "master_unresolved": False, "layer": 0, "fabric": "classic"},
        {"node": "n1.lo", "name": "eno2", "type": "device", "state": "down",
         "mac": "aa:cc", "index": 3, "master_index": 7, "master": "ovs-system",
         "master_unresolved": False, "layer": 0, "fabric": "ovn"},
        {"node": "n1.lo", "name": "mgmt-bo", "type": "bond", "state": "up",
         "mac": "aa:bb", "index": 5, "master_index": 6, "master": "mgmt-br",
         "master_unresolved": False, "layer": 1, "fabric": "classic"},
        {"node": "n1.lo", "name": "mgmt-br", "type": "bridge", "state": "up",
         "mac": "dd:ee", "index": 6, "master_index": None, "master": None,
         "master_unresolved": False, "layer": 2, "fabric": "classic"},
        {"node": "n1.lo", "name": "ovs-system", "type": "openvswitch",
         "state": "down", "mac": "ff:00", "index": 7, "master_index": None,
         "master": None, "master_unresolved": False, "layer": 2, "fabric": "ovn"},
        {"node": "n1.lo", "name": "veth1_h", "type": "veth", "state": "up",
         "mac": "11:22", "index": 16, "master_index": 7, "master": "ovs-system",
         "master_unresolved": False, "layer": 5, "fabric": "ovn"},
    ],
    "cluster_networks": [{"name": "mgmt", "layer": 3}],
    "vlan_configs": [{"name": "vc", "cluster_network": "mgmt",
                      "nics": ["enp1s0"], "bond_mode": "active-backup",
                      "mtu": 1500, "node_selector": {}, "matched_nodes": [],
                      "layer": 1}],
    "networks": [{"namespace": "default", "name": "production",
                  "cluster_network": "mgmt", "kind": "UntaggedNetwork",
                  "ready": True, "cni": "bridge", "bridge": "mgmt-br",
                  "vlan": None, "provider": None, "fabric": "classic",
                  "layer": 4}],
    "kubeovn": {"provider_networks": [], "vpcs": [], "subnets": []},
}

NODE_DETAIL = {
    "cluster": "harv-fake", "node": "n1.lo",
    "links": [{"name": "enp1s0", "state": "UP", "mtu": 1500, "mac": "aa:bb",
               "master": "mgmt-bo", "kind": "device", "bond_mode": None,
               "bond_miimon": None, "flags": []}],
    "stats": {"enp1s0": {"rx_bytes": 42, "rx_errors": 7, "rx_dropped": 0,
                         "tx_bytes": 99, "tx_errors": 0, "tx_dropped": 0}},
}


def open_fabric(page, base_url, fabric=None, lang="en"):
    payload = json.dumps(fabric if fabric is not None else FABRIC)
    page.route("**/api/network-fabric/*", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=payload))
    page.route("**/api/network-fabric/*/node/*", lambda r, q: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(NODE_DETAIL)))
    page.context.add_init_script(
        f"localStorage.setItem('harvester_ops_language','{lang}');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','overview');")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    page.click('[data-overview-tab="fabric"]')
    page.wait_for_timeout(2500)


def positions(page):
    return page.evaluate("""() => {
      const cy = window.Topology && window.Topology._cy && window.Topology._cy();
      if (!cy) return null;
      const out = {};
      cy.nodes().forEach(n => { out[n.data('label')] =
        { y: n.position('y'), x: n.position('x'), layer: n.data('layer'),
          fabric: n.data('fabric'), kind: n.data('kind') }; });
      return out; }""")


def test_the_tab_exists_and_draws(context, flask_server):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    open_fabric(page, flask_server["base_url"])
    pos = positions(page)
    assert pos, "rien n'a été dessiné"
    assert "enp1s0" in pos and "mgmt-br" in pos
    assert not errors, f"erreurs JS : {errors}"


def test_physical_interfaces_sit_at_the_bottom(context, flask_server):
    """L'axe y descend en Cytoscape : la couche 0 doit porter le plus GRAND
    y. Inverser l'empilement mettrait les cartes au plafond."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    pos = positions(page)
    assert pos["enp1s0"]["y"] > pos["mgmt-bo"]["y"] > pos["mgmt-br"]["y"]
    assert pos["mgmt-br"]["y"] > pos["mgmt"]["y"] > pos["production"]["y"]


def test_the_bond_has_its_own_floor(context, flask_server):
    """C'est la couche où vit le VlanConfig ; la fondre avec le switch
    cacherait la seule qu'on configure vraiment."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    pos = positions(page)
    assert pos["mgmt-bo"]["layer"] == 1
    assert pos["enp1s0"]["layer"] == 0 and pos["mgmt-br"]["layer"] == 2
    assert len({pos["enp1s0"]["y"], pos["mgmt-bo"]["y"], pos["mgmt-br"]["y"]}) == 3


def test_the_two_fabrics_stand_in_separate_columns(context, flask_server):
    """Sur un vrai cluster elles n'utilisent même pas la même carte : les
    fondre en une rangée effacerait ce qu'on vient voir."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    pos = positions(page)
    assert pos["eno2"]["fabric"] == "ovn"
    assert pos["enp1s0"]["fabric"] == "classic"
    assert pos["eno2"]["x"] != pos["enp1s0"]["x"]


def test_workload_ports_are_folded_into_one_box(context, flask_server):
    """83 veth sur un seul nœud noieraient la vue."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    pos = positions(page)
    folded = [k for k, v in pos.items() if v["kind"] == "fab-ports"]
    assert len(folded) == 1, folded
    assert "veth1_h" not in pos
    assert folded[0].startswith("1 ")


def test_clicking_an_interface_shows_what_the_api_knows(context, flask_server):
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    page.evaluate("""() => { const cy = window.Topology._cy();
        cy.nodes().filter(n => n.data('label') === 'enp1s0')[0].emit('tap'); }""")
    page.wait_for_timeout(600)
    txt = page.locator('.overview-subtab[data-subtab="fabric"] '
                       '.topology-detail').inner_text()
    assert "aa:bb" in txt
    assert "mgmt-bo" in txt


def test_the_fine_detail_is_fetched_only_when_asked(context, flask_server):
    """Un SSH par rendu coûterait un aller-retour toutes les huit secondes.
    Il ne part qu'au clic sur le bouton."""
    page = context.new_page()
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "/node/" in r.url else None)
    open_fabric(page, flask_server["base_url"])
    page.evaluate("""() => { const cy = window.Topology._cy();
        cy.nodes().filter(n => n.data('label') === 'enp1s0')[0].emit('tap'); }""")
    page.wait_for_timeout(600)
    assert not calls, "le détail est parti sans qu'on le demande"
    page.click('.overview-subtab[data-subtab="fabric"] [data-fabric-detail]')
    page.wait_for_timeout(1200)
    assert calls, "le bouton n'a rien demandé"
    deep = page.locator('.overview-subtab[data-subtab="fabric"] '
                        '.fabric-deep').inner_text()
    assert "1500" in deep and "7" in deep


def test_the_notice_offers_the_monitor_when_the_map_is_incomplete(context, flask_server):
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    notice = page.locator('.overview-subtab[data-subtab="fabric"] .fabric-notice')
    assert notice.is_visible()
    assert "Open vSwitch" in notice.inner_text()
    assert notice.locator('[data-fabric-monitor="add"]').count() == 1


def test_the_notice_offers_to_remove_it_once_in_place(context, flask_server):
    """Une écriture sur le cluster doit pouvoir se défaire du même endroit."""
    page = context.new_page()
    full = dict(FABRIC, full_linkmonitor=True)
    open_fabric(page, flask_server["base_url"], fabric=full)
    notice = page.locator('.overview-subtab[data-subtab="fabric"] .fabric-notice')
    assert notice.locator('[data-fabric-monitor="remove"]').count() == 1


def test_installing_the_monitor_posts_and_does_not_delete(context, flask_server):
    page = context.new_page()
    sent = []
    page.route("**/api/network-fabric/*/linkmonitor", lambda r, q: (
        sent.append(q.method),
        r.fulfill(status=202, content_type="application/json",
                  body=json.dumps({"action_id": "a1"}))))
    open_fabric(page, flask_server["base_url"])
    page.click('.overview-subtab[data-subtab="fabric"] [data-fabric-monitor="add"]')
    page.wait_for_timeout(900)
    assert sent == ["POST"], sent


def test_the_summary_line_counts_what_this_view_shows(context, flask_server):
    """Le résumé des autres vues parle de VMs et de volumes, que la fabrique
    n'a pas : le lui appliquer jetait une exception affichée en bandeau."""
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    open_fabric(page, flask_server["base_url"])
    meta = page.locator('.overview-subtab[data-subtab="fabric"] '
                        '.topology-meta').inner_text()
    assert "links" in meta and "networks" in meta
    assert "undefined" not in meta
    assert not errors


# ---------------------------------------------------------------------------
# v1.37.0 : kube-ovn loge quatre niveaux dans la couche 3
# ---------------------------------------------------------------------------

OVN = dict(FABRIC, kubeovn={
    "provider_networks": [{"name": "external", "default_interface": "eno2",
                           "ready": True, "ready_nodes": ["n1.lo"],
                           "vlans": ["external-vlan"], "layer": 3, "rank": 0}],
    "vlans": [{"name": "external-vlan", "id": 0,
               "provider_network": "external",
               "subnets": ["egress-external"], "layer": 3, "rank": 1}],
    "subnets": [
        {"name": "egress-external", "vpc": "ovn-cluster",
         "cidr": "172.16.0.0/22", "gateway": "172.16.0.1", "nat": False,
         "provider": "external.kube-system.ovn", "vlan": "external-vlan",
         "available_ips": 6, "overlay": False, "layer": 3, "rank": 2},
        {"name": "ovn-default", "vpc": "ovn-cluster", "cidr": "10.54.0.0/16",
         "gateway": "10.54.0.1", "nat": True, "provider": "ovn", "vlan": None,
         "available_ips": 65000, "overlay": True, "layer": 3, "rank": 2}],
    "vpcs": [{"name": "ovn-cluster",
              "subnets": ["egress-external", "ovn-default"],
              "layer": 3, "rank": 3}],
})


def boxes(page):
    return page.evaluate("""() => { const cy = window.Topology._cy();
      if (!cy) return []; return cy.nodes().map(n => ({
        l: n.data('label'), x: n.position('x'), y: n.position('y'),
        w: n.data('width'), h: n.data('height'), rank: n.data('rank') })); }""")


def test_no_two_boxes_sit_on_top_of_each_other(context, flask_server):
    """Quatre rangs dans une bande de hauteur fixe se chevauchaient : les
    boîtes font 44 px et le pas n'en faisait que 26."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"], fabric=OVN)
    ns = boxes(page)
    assert ns, "rien dessiné"
    over = [(a["l"], b["l"]) for i, a in enumerate(ns) for b in ns[i + 1:]
            if abs(a["x"] - b["x"]) < (a["w"] + b["w"]) / 2 - 4
            and abs(a["y"] - b["y"]) < (a["h"] + b["h"]) / 2 - 4]
    assert not over, f"boîtes superposées : {over}"


def test_the_kube_ovn_levels_stack_in_the_right_order(context, flask_server):
    """Du plus proche du cuivre au plus abstrait : provider network, VLAN,
    subnet, VPC. Les aligner effaçait la hiérarchie."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"], fabric=OVN)
    pos = {b["l"]: b["y"] for b in boxes(page)}
    assert pos["external"] > pos["external-vlan"] > pos["egress-external"] \
        > pos["ovn-cluster"], pos


def test_an_overlay_subnet_is_told_apart(context, flask_server):
    """Il ne sort pas par un uplink physique : le montrer comme les autres
    est le pire contresens de cette vue."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"], fabric=OVN)
    colors = page.evaluate("""() => { const cy = window.Topology._cy();
      const g = l => cy.nodes().filter(n => n.data('label') === l)[0].data('color');
      return { overlay: g('ovn-default'), underlay: g('egress-external') }; }""")
    assert colors["overlay"] != colors["underlay"]


def test_a_subnet_reaches_the_wire_through_its_vlan(context, flask_server):
    """Et non en sautant directement sur la carte."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"], fabric=OVN)
    edges = page.evaluate("""() => { const cy = window.Topology._cy();
      const l = id => cy.getElementById(id).data('label');
      return cy.edges().map(e => l(e.data('source')) + '>' + l(e.data('target'))); }""")
    assert "egress-external>external-vlan" in edges
    assert "external-vlan>external" in edges
    assert not any(e.startswith("egress-external>eno2") for e in edges)


def test_the_cluster_network_hangs_from_its_bridge(context, flask_server):
    """Pas de la carte physique : l'arête sautait le bridge et le bond,
    traversait tout le schéma, et laissait croire à un second chemin."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"], fabric=OVN)
    edges = page.evaluate("""() => { const cy = window.Topology._cy();
      const l = id => cy.getElementById(id).data('label');
      return cy.edges().map(e => l(e.data('source')) + '>' + l(e.data('target'))); }""")
    assert "mgmt>mgmt-br" in edges
    assert "mgmt>enp1s0" not in edges


# ---------------------------------------------------------------------------
# v1.38.0 : bandes nommées et switch physique
#
# Inspiré des schémas vSphere, où chaque bande porte son nom (« Uplink port
# group ») et où le switch physique ferme le dessin en bas.
# ---------------------------------------------------------------------------

def test_each_band_says_what_it_is(context, flask_server):
    """Sans libellé il faut déduire chaque niveau du contenu de ses boîtes,
    ce qu'un exploitant pressé ne fera pas."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"], fabric=OVN)
    bands = page.evaluate("""() => { const cy = window.Topology._cy();
      return cy.nodes().filter(n => n.data('kind') === 'fab-band')
               .map(n => n.data('label')); }""")
    for expected in ("Physical interfaces", "Aggregation", "Virtual switches",
                     "Attachable networks", "Physical switch"):
        assert expected in bands, bands


def test_the_band_labels_stay_out_of_the_columns(context, flask_server):
    """Posés à droite de leur ancre, ils recouvraient la première colonne."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"], fabric=OVN)
    out = page.evaluate("""() => { const cy = window.Topology._cy();
      const band = cy.nodes().filter(n => n.data('kind') === 'fab-band')[0];
      const other = cy.nodes().filter(n => n.data('kind') !== 'fab-band');
      return { halign: band.style('text-halign'),
               bandX: band.position('x'),
               minX: Math.min(...other.map(n => n.position('x'))) }; }""")
    assert out["halign"] == "left"
    assert out["bandX"] < out["minX"]


def test_the_chain_ends_at_a_switch_even_an_unknown_one(context, flask_server):
    """La question de l'exploitant ne s'arrête pas au cuivre (« sur quelle
    prise ? »). Montrer le switch, même inconnu, ferme le schéma et donne à
    LLDP un endroit où se poser."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"], fabric=OVN)
    out = page.evaluate("""() => { const cy = window.Topology._cy();
      const sw = cy.nodes().filter(n => n.data('kind') === 'fab-switchport');
      const nic = cy.nodes().filter(n => n.data('label') === 'enp1s0')[0];
      return { count: sw.length,
               below: sw.length ? sw[0].position('y') > nic.position('y') : false,
               iface: sw.length ? sw[0].data('raw').interface : null }; }""")
    assert out["count"] == 1, "un switch par carte en service"
    assert out["below"], "le switch doit être SOUS la carte"
    assert out["iface"] == "enp1s0"


def test_a_dead_card_gets_no_switch(context, flask_server):
    """`eno2` n'a pas de porteuse : lui dessiner un switch laisserait croire
    qu'il est branché."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"], fabric=OVN)
    ifaces = page.evaluate("""() => { const cy = window.Topology._cy();
      return cy.nodes().filter(n => n.data('kind') === 'fab-switchport')
               .map(n => n.data('raw').interface); }""")
    assert "eno2" not in ifaces


def test_the_view_still_renders_without_a_style_error(context, flask_server):
    """Une icône vide produisait `background-image: ` invalide, ce qui
    faisait échouer le parseur de styles et donc le rendu ENTIER, avec une
    erreur venue des entrailles de Cytoscape et non de notre code."""
    page = context.new_page()
    bad = []
    page.on("console", lambda m: bad.append(m.text[:120])
            if (m.type == "warning" and "invalid" in m.text.lower()) else None)
    page.on("pageerror", lambda e: bad.append("ERR " + str(e)[:120]))
    open_fabric(page, flask_server["base_url"], fabric=OVN)
    assert page.evaluate("() => !!window.Topology._cy()"), "rien rendu"
    assert not bad, bad
