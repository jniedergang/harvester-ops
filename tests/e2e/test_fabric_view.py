"""v1.39.0 : la vue Fabrique, lue comme des vSwitch, dans un vrai navigateur.

Le graphe empilé des versions 1.35 à 1.38 a été jugé « pas très lisible et
pas pratique ». Il est remplacé par la présentation « Standard Switch »
d'ESXi : UN BLOC PAR SWITCH, lu de gauche à droite,

    réseaux (et leurs VMs)  |  switch  |  cartes physiques

Ce que ces tests tiennent, et que les tests de source ne voient pas : que
chaque switch se lit bien dans ce sens, que l'overlay est dit INTERNE (sans
uplink, ce que l'ancien graphe trahissait), que les VMs sont sous le réseau
qu'elles utilisent, et que le détail d'une carte ne coûte qu'un aller-retour
SSH par nœud.

Le réseau est intercepté : aucun test ne touche à un cluster.
"""

import json

import pytest

playwright = pytest.importorskip("playwright")


def lk(name, typ, state="up", master=None, layer=0, index=0):
    return {"node": "n1.lo", "name": name, "type": typ, "state": state,
            "mac": f"aa:bb:cc:dd:ee:{index:02x}", "index": index,
            "master_index": None, "master": master,
            "master_unresolved": False, "layer": layer,
            "fabric": "classic"}


def vm(name, network="default/production", status="Stopped"):
    return {"namespace": "default", "name": name, "status": status,
            "networks": [{"nic": "nic-1", "network": network, "pod": False}]}


FABRIC = {
    "cluster": "harv-fake",
    "full_linkmonitor": False,
    "nodes": [{"name": "n1.lo", "address": "10.0.0.1", "mgmt": True,
               "provider_bindings": {}}],
    "links": [
        lk("enp1s0", "device", master="mgmt-bo", layer=0, index=2),
        lk("eno2", "device", state="down", layer=0, index=3),
        lk("wlo1", "device", state="down", layer=0, index=4),
        lk("mgmt-bo", "bond", master="mgmt-br", layer=1, index=5),
        lk("mgmt-br", "bridge", layer=2, index=6),
        lk("vethabc", "veth", master="mgmt-br", layer=5, index=20),
    ],
    "cluster_networks": [{"name": "mgmt", "layer": 3}],
    "vlan_configs": [{"name": "vc", "cluster_network": "mgmt",
                      "nics": ["enp1s0"], "bond_mode": "active-backup",
                      "mtu": 1500, "layer": 1}],
    "networks": [
        {"namespace": "default", "name": "production", "cluster_network": "mgmt",
         "kind": "UntaggedNetwork", "ready": True, "cni": "bridge",
         "bridge": "mgmt-br", "vlan": None, "provider": None,
         "fabric": "classic", "layer": 4},
        {"namespace": "default", "name": "vlan42", "cluster_network": "mgmt",
         "kind": "L2VlanNetwork", "ready": False, "cni": "bridge",
         "bridge": "mgmt-br", "vlan": 42, "provider": None,
         "fabric": "classic", "layer": 4},
        {"namespace": "kube-system", "name": "external", "cluster_network": "mgmt",
         "kind": "OverlayNetwork", "ready": True, "cni": "kube-ovn",
         "bridge": None, "vlan": None, "provider": "external.kube-system.ovn",
         "fabric": "ovn", "layer": 4},
        {"namespace": "default", "name": "ovn-overlay", "cluster_network": "mgmt",
         "kind": "OverlayNetwork", "ready": True, "cni": "kube-ovn",
         "bridge": None, "vlan": None, "provider": "ovn-overlay.default.ovn",
         "fabric": "ovn", "layer": 4},
    ],
    "vms": [vm(f"idle-{i}") for i in range(8)]
           + [vm("web", status="Running"),
              vm("egress", network="kube-system/external", status="Running"),
              {"namespace": "default", "name": "podvm", "status": "Running",
               "networks": [{"nic": "default", "network": None, "pod": True}]}],
    "kubeovn": {
        "provider_networks": [{"name": "external", "default_interface": "eno2",
                               "ready": True, "ready_nodes": ["n1.lo"],
                               "vlans": ["external-vlan"]}],
        "vlans": [{"name": "external-vlan", "id": 0,
                   "provider_network": "external", "subnets": ["egress-external"]}],
        "subnets": [
            {"name": "egress-external", "vpc": "ovn-cluster", "cidr": "172.16.0.0/22",
             "gateway": "172.16.0.1", "nat": False,
             "provider": "external.kube-system.ovn", "vlan": "external-vlan",
             "overlay": False},
            {"name": "ovn-default", "vpc": "ovn-cluster", "cidr": "10.54.0.0/16",
             "gateway": "10.54.0.1", "nat": True, "provider": "ovn",
             "vlan": None, "overlay": True},
        ],
        "vpcs": [{"name": "ovn-cluster", "subnets": ["egress-external", "ovn-default"]}],
    },
}

NODE_DETAIL = {
    "cluster": "harv-fake", "node": "n1.lo",
    "links": [
        {"name": "enp1s0", "state": "UP", "mtu": 1500, "mac": "aa:bb",
         "master": "mgmt-bo", "kind": "device", "bond_mode": None,
         "bond_miimon": None, "flags": []},
        {"name": "mgmt-bo", "state": "UP", "mtu": 1500, "mac": "aa:bb",
         "master": "mgmt-br", "kind": "bond", "bond_mode": "active-backup",
         "bond_miimon": 100, "flags": []},
    ],
    "stats": {"enp1s0": {"rx_bytes": 2048, "rx_errors": 7, "rx_dropped": 0,
                         "tx_bytes": 99, "tx_errors": 0, "tx_dropped": 0}},
    "phys": {"enp1s0": {"speed_mbps": 1000, "duplex": "full", "carrier": 1,
                        "carrier_changes": 2}},
}


def open_fabric(page, base_url, fabric=None, lang="en", detail_calls=None,
                monitor_calls=None):
    payload = json.dumps(fabric if fabric is not None else FABRIC)

    def fabric_route(route, request):
        if monitor_calls is not None and request.url.endswith("/linkmonitor"):
            monitor_calls.append(request.method)
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"action_id": "x"}')
        return route.fulfill(status=200, content_type="application/json",
                             body=payload)

    def detail_route(route, request):
        if detail_calls is not None:
            detail_calls.append(request.url)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(NODE_DETAIL))

    page.route("**/api/network-fabric/**", fabric_route)
    page.route("**/api/network-fabric/*/node/*", detail_route)
    page.context.add_init_script(
        f"localStorage.setItem('harvester_ops_language','{lang}');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','overview');")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    page.click('[data-overview-tab="fabric"]')
    page.wait_for_selector('.vsw', timeout=10000)
    page.wait_for_timeout(1200)


def block(page, block_id):
    return page.locator(f'.vsw[data-block="{block_id}"]')


def test_one_block_per_switch(context, flask_server):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    open_fabric(page, flask_server["base_url"])
    ids = page.locator('.vsw[data-block]').evaluate_all(
        "els => els.map(e => e.dataset.block)")
    assert ids == ["cn-mgmt", "pn-external", "overlay"], ids
    # Un switch classique porte le nom de son BRIDGE, pris dans les NADs.
    assert block(page, "cn-mgmt").locator('.vsw-title').inner_text() == "mgmt-br"
    assert not errors, errors


def test_it_is_html_not_a_canvas(context, flask_server):
    """Le texte se sélectionne et se copie ; plus de diagonales."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    host = page.locator('.overview-subtab[data-subtab="fabric"]')
    assert host.locator('.topology-canvas').count() == 0
    assert host.locator('canvas').count() == 0


def test_a_switch_reads_left_to_right(context, flask_server):
    """Réseaux à gauche, le switch au milieu, les cartes à droite : c'est
    l'ordre dans lequel l'exploitant suit un paquet vers le câble."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    b = block(page, "cn-mgmt")
    net = b.locator('.vsw-pg').first.bounding_box()
    spine = b.locator('.vsw-spine').bounding_box()
    card = b.locator('.vsw-bond').first.bounding_box()
    assert net["x"] + net["width"] <= spine["x"] + 1
    assert spine["x"] + spine["width"] <= card["x"] + 1


def test_the_uplink_is_the_bond_holding_its_card(context, flask_server):
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    bond = block(page, "cn-mgmt").locator('.vsw-right .vsw-bond')
    assert bond.count() == 1
    assert "mgmt-bo" in bond.locator('.vsw-bond-head').inner_text()
    assert bond.locator('.vsw-nic[data-nic="enp1s0"]').count() == 1
    # Le port de charge n'est pas un uplink : il est COMPTÉ, pas dessiné.
    assert block(page, "cn-mgmt").locator('[data-nic="vethabc"]').count() == 0
    assert "1 workload port" in block(page, "cn-mgmt").locator('.vsw-head').inner_text()


def test_the_vswitch_header_carries_the_team_policy(context, flask_server):
    """Mode d'agrégation et MTU vivent dans le VlanConfig : ESXi les montre
    sur le switch, pas sur la carte."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    head = block(page, "cn-mgmt").locator('.vsw-head').inner_text()
    assert "active-backup" in head and "MTU 1500" in head


def test_a_vlan_network_shows_its_tag_and_its_trouble(context, flask_server):
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    pg = block(page, "cn-mgmt").locator('.vsw-pg', has_text="vlan42")
    assert "VLAN 42" in pg.inner_text()
    assert "not ready" in pg.inner_text()
    assert "warn" in pg.get_attribute("class")


def test_the_overlay_is_an_internal_switch(context, flask_server):
    """Le contresens que l'ancien graphe commettait : dessiner l'overlay
    comme s'il atteignait le cuivre."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    ov = block(page, "overlay")
    assert ov.locator('.vsw-internal').count() == 1
    assert ov.locator('.vsw-nic').count() == 0
    assert "Internal switch" in ov.locator('.vsw-kind').text_content()


def test_an_overlay_nad_without_subnet_is_flagged(context, flask_server):
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    pg = block(page, "overlay").locator('.vsw-pg', has_text="ovn-overlay")
    assert "no subnet bound" in pg.inner_text()


def test_the_underlay_goes_out_through_its_card(context, flask_server):
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    ul = block(page, "pn-external")
    nic = ul.locator('.vsw-nic[data-nic="eno2"]')
    assert nic.count() == 1
    # Carte sans lien : c'est ce qu'il faut voir en premier sur un uplink.
    assert "down" in nic.get_attribute("class")
    pg = ul.locator('.vsw-pg').first.inner_text()
    assert "egress-external" in pg and "172.16.0.0/22" in pg
    # VLAN 0 dans kube-ovn = sans étiquette, pas « VLAN 0 ».
    assert "untagged" in pg and "VLAN 0" not in pg
    assert "kube-system/external" in pg


def test_the_vms_sit_under_the_network_they_use(context, flask_server):
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    ul = block(page, "pn-external").locator('.vsw-vms').inner_text()
    assert "default/egress" in ul
    prod = block(page, "cn-mgmt").locator('.vsw-pg', has_text="production")
    assert "Virtual machines (9)" in prod.inner_text()


def test_running_vms_come_first_and_the_rest_unfolds(context, flask_server):
    """Neuf VMs sur un réseau : on montre les premières, démarrées d'abord,
    et le reste se déplie. Le dépliage doit survivre au rafraîchissement
    des 8 secondes, sinon il se referme sous les yeux."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    prod = block(page, "cn-mgmt").locator('.vsw-pg', has_text="production")
    rows = prod.locator('.vsw-vms li:not(.more)')
    assert rows.count() == 6
    assert "default/web" in rows.first.inner_text()
    prod.locator('[data-vsw-more]').click()
    page.wait_for_timeout(300)
    assert block(page, "cn-mgmt").locator('.vsw-pg', has_text="production") \
        .locator('.vsw-vms li:not(.more)').count() == 9
    page.evaluate("() => window.Fabric.refresh()")
    page.wait_for_timeout(800)
    assert block(page, "cn-mgmt").locator('.vsw-pg', has_text="production") \
        .locator('.vsw-vms li:not(.more)').count() == 9


def test_the_pod_network_is_told_apart(context, flask_server):
    """Il n'a pas de bridge : le rattacher à un switch serait un mensonge."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    pod = page.locator('.vsw-pod')
    assert pod.count() == 1
    assert "default/podvm" in pod.inner_text()


def test_an_adapter_on_no_switch_is_still_shown(context, flask_server):
    """Ne pas la montrer laisserait croire qu'on a tout vu."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    assert page.locator('.vsw-unused [data-nic="wlo1"]').count() == 1


def test_the_card_shows_its_speed_like_esxi(context, flask_server):
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    card = page.locator('.vsw-nic[data-nic="enp1s0"]').first
    assert "1000 Full" in card.inner_text()


def test_a_click_on_a_card_opens_its_detail(context, flask_server):
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    page.locator('.vsw-nic[data-nic="enp1s0"]').first.click()
    page.wait_for_timeout(300)
    side = page.locator('.fabric-detail').inner_text()
    assert "1000 Mb/s" in side and "1500" in side
    assert "7 err" in side                       # compteurs du nœud
    assert page.locator('.fabric-detail [data-fabric-lldp]').count() == 1


def test_the_switch_answer_survives_the_refresh(context, flask_server):
    """Relevé sur harvlab : la sonde écoute jusqu'à 35 s, la vue se
    rafraîchit toutes les 8 s et redessinait le détail ; la réponse (17 s
    sur harvlab) s'écrivait dans un élément déjà retiré, et « écoute en
    cours » disparaissait au bout d'une seconde. Rien ne s'affichait jamais."""
    page = context.new_page()
    held = []
    open_fabric(page, flask_server["base_url"])
    # Après open_fabric : la dernière interception déclarée l'emporte.
    page.route("**/lldp*", lambda r, q: held.append(r))
    page.locator('.vsw-nic[data-nic="enp1s0"]').first.click()
    page.wait_for_timeout(300)
    page.locator('.fabric-detail [data-fabric-lldp]').click()
    page.wait_for_timeout(300)
    page.evaluate("() => window.Fabric.refresh()")
    page.wait_for_timeout(500)
    out = page.locator('.fabric-detail .fabric-lldp-out')
    assert "Listening" in out.inner_text()
    assert page.locator('.fabric-detail [data-fabric-lldp]').is_disabled()
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps({
        "found": True, "fields": {"system_name": "node2-xl170r", "port_description": "br0"}}))
    page.wait_for_timeout(500)
    page.evaluate("() => window.Fabric.refresh()")
    page.wait_for_timeout(500)
    assert out.inner_text().splitlines() == ["Switch : node2-xl170r", "Port : br0"]
    assert page.locator('.fabric-detail [data-fabric-lldp]').is_enabled()


def test_a_bond_detail_shows_its_mode_but_offers_no_lldp(context, flask_server):
    """LLDP n'a de sens que sur une carte physique."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    page.locator('.vsw-bond-head[data-nic="mgmt-bo"]').click()
    page.wait_for_timeout(300)
    side = page.locator('.fabric-detail').inner_text()
    assert "active-backup" in side and "100 ms" in side
    assert page.locator('.fabric-detail [data-fabric-lldp]').count() == 0


def test_the_node_is_read_once_not_every_refresh(context, flask_server):
    """Un aller-retour SSH toutes les huit secondes serait intenable."""
    page = context.new_page()
    calls = []
    open_fabric(page, flask_server["base_url"], detail_calls=calls)
    for _ in range(3):
        page.evaluate("() => window.Fabric.refresh()")
        page.wait_for_timeout(300)
    assert len(calls) == 1, calls


def test_a_copy_click_does_not_select_the_card(context, flask_server):
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    card = page.locator('.vsw-nic[data-nic="enp1s0"]').first
    card.hover()
    card.locator('[data-copy]').first.click()
    page.wait_for_timeout(200)
    assert "selected" not in (card.get_attribute("class") or "")


def test_the_monitor_offer_calls_the_api(context, flask_server):
    page = context.new_page()
    calls = []
    open_fabric(page, flask_server["base_url"], monitor_calls=calls)
    page.click('[data-fabric-monitor="add"]')
    page.wait_for_timeout(500)
    assert calls == ["POST"]


def test_every_control_has_a_tooltip(context, flask_server):
    """Règle du projet : un contrôle sans bulle d'aide n'est pas livré. La
    vue se réécrit toutes les 8 s, elle doit poser ses bulles elle-même."""
    page = context.new_page()
    open_fabric(page, flask_server["base_url"])
    missing = page.evaluate("""() => [...document.querySelectorAll(
        '.overview-subtab[data-subtab="fabric"] button, .overview-subtab[data-subtab="fabric"] [role=button]')]
        .filter(el => !el.getAttribute('data-tip'))
        .map(el => el.className + ' ' + el.textContent.trim().slice(0, 20))""")
    assert not missing, missing


def test_it_renders_in_french_without_error(context, flask_server):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    open_fabric(page, flask_server["base_url"], lang="fr")
    assert "Switch interne" in block(page, "overlay").text_content()
    assert not errors, errors
