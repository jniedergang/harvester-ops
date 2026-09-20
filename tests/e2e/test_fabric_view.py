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
