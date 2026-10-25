"""v1.65.0 : le menu Networks de Harvester dans un navigateur : l'onglet
Cluster networks (réglages réseau, réseaux et configurations), les fenêtres
de configuration, de migration, de réglage, d'équilibreur, de pool, de réseau
d'hôte, de réseau de VM (trunk, modification), et ce qu'elles envoient."""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402
from nav import address, goto  # noqa: E402

NET = {"cluster": "harv-fake", "nodes": [{"name": "n1", "witness": False}, {"name": "n2", "witness": False}],
       "settings": [{"name": "storage-network", "enabled": False, "share_storage": False, "cluster_network": "", "vlan": 0,
                     "range": "", "exclude": [], "exclusive_vlan": False, "configured": None, "reason": "", "message": ""},
                    {"name": "vm-migration-network", "enabled": True, "share_storage": False, "cluster_network": "data",
                     "vlan": 166, "range": "10.166.0.0/24", "exclude": [], "exclusive_vlan": False, "configured": "True",
                     "reason": "", "message": ""},
                    {"name": "rwx-network", "enabled": False, "share_storage": False, "cluster_network": "", "vlan": 0,
                     "range": "", "exclude": [], "exclusive_vlan": False, "configured": None, "reason": "", "message": ""}],
       "cluster_networks": [
           {"name": "mgmt", "description": "", "ready": True, "message": "", "mtu": None, "protected": True, "configs": [],
            "networks": []},
           {"name": "data", "description": "", "ready": True, "message": "", "mtu": 1500, "protected": False, "networks": [],
            "configs": [{"name": "data-all", "description": "", "nics": ["enp4s0"], "bond_mode": "active-backup",
                         "miimon": None, "mtu": 1500, "selector": {}, "nodes": ["n1", "n2"], "managed": False,
                         "status": {"n1": {"ready": True, "message": ""}, "n2": {"ready": False, "message": "enp4s0 has been enslaved"}}}]},
           {"name": "data2", "description": "", "ready": None, "message": "", "mtu": None, "protected": False,
            "configs": [], "networks": []}]}
NICS = {"nodes": ["n1", "n2"], "monitor": True,
        "nics": [{"name": "enp1s0", "usable": False, "why": "enslaved", "enslaved_on": ["n1", "n2"], "down_on": [], "state": {}},
                 {"name": "enp4s0", "usable": True, "why": "", "enslaved_on": [], "down_on": [], "state": {"n1": "up", "n2": "up"}},
                 {"name": "enp5s0", "usable": True, "why": "", "enslaved_on": [], "down_on": [], "state": {"n1": "up", "n2": "up"}}]}
POOLS = {"items": [{"name": "lan", "description": "", "ranges": [{"subnet": "10.0.0.0/24"}], "network": "", "priority": 0,
                    "scope": [{"namespace": "*"}], "total": 253, "available": 252, "allocated": {"10.0.0.7": "default/old"},
                    "ready": True, "message": "", "global": True, "created": None}]}
LBS = {"items": [{"namespace": "default", "name": "web", "description": "", "workload": "vm", "ipam": "dhcp", "ip_pool": "",
                  "address": "172.16.10.50", "backends": ["10.52.0.9"], "listeners": [{"name": "ssh", "port": 22, "protocol": "TCP", "backendPort": 22}],
                  "selector": {"app": ["web"]}, "health": {}, "ready": True, "message": "", "created": None}]}
VMNET = {"namespace": "default", "name": "v20", "type": "L2VlanNetwork", "cluster_network": "data", "vlan": 20, "ranges": [],
         "route": {"mode": "auto", "dhcp_server": "", "cidr": "", "gateway": "", "connectivity": "true"}, "description": "",
         "running": []}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','fr');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    sent = []

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "net000000165"}, 202)
    page.route("**/api/net-admin/harv-fake", lambda r, q: fulfill(r, NET))
    page.route("**/api/net-admin/harv-fake/nics**", lambda r, q: fulfill(r, NICS))
    page.route("**/api/net-admin/harv-fake/vmnet/**", lambda r, q: fulfill(r, VMNET))
    page.route("**/api/net-admin/harv-fake/do/**", writes)
    page.route("**/api/cluster-objects/harv-fake/ippools", lambda r, q: fulfill(r, POOLS))
    page.route("**/api/cluster-objects/harv-fake/loadbalancers", lambda r, q: fulfill(r, LBS))
    page.route("**/api/cluster-objects/harv-fake/hostnetworks", lambda r, q: fulfill(r, {"items": []}))
    page.route("**/api/namespaces/harv-fake", lambda r, q: fulfill(r, [{"name": "default"}]))
    page.route("**/api/network-fabric/harv-fake", lambda r, q: fulfill(r, {"cluster_networks": [{"name": "mgmt"}, {"name": "data"}],
                                                                           "networks": [{"namespace": "default", "name": "v20"}]}))
    page.route("**/api/stream/net000000165", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address("namespaces"))
    page.wait_for_function("window.NetAdmin && window.Sections && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    return page, sent


def cn_tab(page):
    page.evaluate("Sections.open('network', 'clusternets')")
    body = page.locator('#tab-network .section-pane[data-pane="clusternets"] [data-na="body"]')
    expect(body.locator("[data-cn]")).to_have_count(3, timeout=8000)
    return body


def test_the_cluster_networks_tab_shows_settings_networks_and_node_states(ui):
    page, _ = ui
    body = cn_tab(page)
    expect(body.locator(".na-tile")).to_have_count(3)
    expect(body.locator(".na-tile").nth(1)).to_contain_text("data")
    row = body.locator('[data-cn="data"] tr[data-cfg="data-all"]')
    expect(row.locator(".badge.ok")).to_have_count(1)
    expect(row.locator(".badge.fail")).to_have_count(1)                    # le refus de l'agent, dit
    expect(body.locator('[data-cn="mgmt"] [data-na="delete-cn"]')).to_have_count(0)
    expect(body.locator('[data-cn="data"] [data-na="delete-cn"]')).to_be_disabled()   # encore une configuration
    expect(body.locator('[data-cn="data2"] [data-na="delete-cn"]')).to_be_enabled()


def test_a_configuration_offers_only_free_nics_and_sends_the_form(ui):
    page, sent = ui
    body = cn_tab(page)
    body.locator('[data-cn="data2"] [data-na="new-cfg"]').click()
    w = page.locator("#fp-na-cfg-harv-fake-new")
    expect(w.locator('[name="nic"]')).to_have_count(3, timeout=8000)
    expect(w.locator('[name="nic"][value="enp1s0"]')).to_be_disabled()
    w.locator('[name="name"]').fill("data2-all")
    w.locator('[name="nic"][value="enp5s0"]').check()
    w.locator('[name="bond_mode"]').select_option("802.3ad")
    w.locator('[name="mtu"]').fill("9000")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, b = sent[-1]
    assert path == "api/net-admin/harv-fake/do/config-create"
    assert b["spec"]["cluster_network"] == "data2" and b["spec"]["nics"] == ["enp5s0"]
    assert b["spec"]["bond_mode"] == "802.3ad" and b["spec"]["mtu"] == "9000" and b["spec"]["nodes"]["mode"] == "all"


def test_migrating_a_configuration_offers_the_other_networks(ui):
    page, sent = ui
    body = cn_tab(page)
    body.locator('tr[data-cfg="data-all"] [data-na="migrate-cfg"]').click()
    w = page.locator("#fp-na-mig-harv-fake-data-all")
    expect(w.locator('[name="target"] option')).to_have_count(1)          # ni mgmt, ni data
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/net-admin/harv-fake/do/config-migrate", {"name": "data-all", "target": "data2"})


def test_a_network_setting_can_be_dedicated_or_cleared(ui):
    page, sent = ui
    body = cn_tab(page)
    body.locator('[data-na="setting"][data-name="storage-network"]').click()
    w = page.locator("#fp-na-set-harv-fake-storage-network")
    expect(w.locator('[data-f="range"]')).to_be_hidden()
    w.locator('[name="mode"]').select_option("dedicated")
    w.locator('[name="cluster_network"]').select_option("data")
    w.locator('[name="vlan"]').fill("167")
    w.locator('[name="range"]').fill("10.167.0.0/24")
    w.locator('[name="exclude"]').fill("10.167.0.0/28")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/net-admin/harv-fake/do/setting-set", {"name": "storage-network", "spec": {
        "cluster_network": "data", "vlan": "167", "range": "10.167.0.0/24", "exclude": ["10.167.0.0/28"], "exclusive_vlan": False}})
    w.locator('[data-action="close"]').click()
    body.locator('[data-na="setting"][data-name="vm-migration-network"]').click()
    w = page.locator("#fp-na-set-harv-fake-vm-migration-network")
    w.locator('[name="mode"]').select_option("mgmt")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/net-admin/harv-fake/do/setting-clear", {"name": "vm-migration-network"})


def test_a_load_balancer_list_and_its_form(ui):
    page, sent = ui
    page.evaluate("Sections.open('network', 'lbs')")
    pane = page.locator('#tab-network .section-pane[data-pane="lbs"]')
    expect(pane.locator('tr[data-id="default/web"]')).to_contain_text("172.16.10.50", timeout=8000)
    pane.locator(".res-new").click()
    w = page.locator("#fp-na-lb-harv-fake-new")
    w.locator('[name="name"]').wait_for(timeout=8000)
    expect(w.locator('[data-f="ip_pool"]')).to_be_hidden()                 # DHCP par défaut, comme Harvester
    w.locator('[name="name"]').fill("api")
    w.locator('[name="l-port"]').fill("443")
    w.locator('[name="l-back"]').fill("8443")
    w.locator('[name="selector"]').fill("app=api,api2")
    w.locator('[name="hc"]').check()
    w.locator('[name="hc_port"]').fill("8443")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, b = sent[-1]
    assert path == "api/net-admin/harv-fake/do/lb-create"
    assert b["spec"]["listeners"] == [{"name": "", "port": "443", "protocol": "TCP", "backend_port": "8443"}]
    assert b["spec"]["ipam"] == "dhcp" and b["spec"]["selector"] == "app=api,api2" and b["spec"]["health"]["port"] == "8443"


def test_an_ip_pool_form_and_releasing_an_orphan_address(ui):
    page, sent = ui
    page.evaluate("Sections.open('network', 'pools')")
    pane = page.locator('#tab-network .section-pane[data-pane="pools"]')
    row = pane.locator('tr[data-id="/lan"]')
    expect(row).to_contain_text("252 / 253", timeout=8000)
    expect(row.locator('[data-act="delete"]')).to_be_disabled()           # une adresse encore allouée
    row.locator('[data-act="edit"]').click()
    w = page.locator("#fp-na-pool-harv-fake-lan")
    w.locator('[data-release="10.0.0.7"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/net-admin/harv-fake/do/pool-release", {"name": "lan", "ip": "10.0.0.7"})
    pane.locator(".res-new").click()
    w = page.locator("#fp-na-pool-harv-fake-new")
    w.locator('[name="name"]').fill("lab")
    w.locator('[name="r-subnet"]').fill("172.16.2.0/24")
    w.locator('[name="r-start"]').fill("172.16.2.90")
    w.locator('[name="r-end"]').fill("172.16.2.95")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, b = sent[-1]
    assert path == "api/net-admin/harv-fake/do/pool-create"
    assert b["spec"]["ranges"] == [{"subnet": "172.16.2.0/24", "start": "172.16.2.90", "end": "172.16.2.95", "gateway": ""}]
    assert b["spec"]["scope"] == [{"namespace": "*"}]


def test_a_static_host_network(ui):
    page, sent = ui
    page.evaluate("Sections.open('network', 'hostnets')")
    pane = page.locator('#tab-network .section-pane[data-pane="hostnets"]')
    pane.locator(".res-new").wait_for(timeout=8000)
    pane.locator(".res-new").click()
    w = page.locator("#fp-na-hnc-harv-fake-new")
    w.locator('[name="name"]').wait_for(timeout=8000)
    w.locator('[name="name"]').fill("stor")
    w.locator('[name="cluster_network"]').select_option("data")
    w.locator('[name="vlan"]').fill("165")
    expect(w.locator('[name="ip-n1"]')).to_be_hidden()
    w.locator('[name="mode"]').select_option("static")
    w.locator('[name="ip-n1"]').fill("10.165.0.61/24")
    w.locator('[name="ip-n2"]').fill("10.165.0.62/24")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, b = sent[-1]
    assert path == "api/net-admin/harv-fake/do/hostnet-create"
    assert b["spec"]["ips"] == {"n1": "10.165.0.61/24", "n2": "10.165.0.62/24"} and b["spec"]["nodes"] == []


def test_a_trunk_vm_network_and_editing_a_vlan(ui):
    page, sent = ui
    page.route("**/api/objects/harv-fake/network", lambda r, q: (sent.append(("objects", q.post_data_json)),
                                                                 fulfill(r, {"action_id": "net000000165"}, 202)))
    page.evaluate("ObjectForms.openNew('network', 'harv-fake', {})")
    w = page.locator("#fp-new-network-harv-fake")
    w.locator('[name="name"]').wait_for(timeout=8000)
    w.locator('[name="name"]').fill("tr1")
    w.locator('[name="type"]').select_option("trunk")
    expect(w.locator('[data-f="route_mode"]')).to_be_hidden()
    w.locator('[name="ranges"]').fill("100-199, 300")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    spec = sent[-1][1]["spec"]
    assert spec["type"] == "trunk" and spec["ranges"] == [{"min": 100, "max": 199}, {"min": 300, "max": 300}]
    assert "route_mode" not in spec
    page.evaluate("NetAdmin.editVmNet('harv-fake', 'default', 'v20', () => {})")
    e = page.locator("#fp-na-vmnet-harv-fake-default-v20")
    e.locator('[name="vlan"]').fill("21")
    e.locator('[name="route_mode"]').select_option("manual")
    e.locator('[name="cidr"]').fill("10.0.21.0/24")
    e.locator('[name="gateway"]').fill("10.0.21.1")
    e.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/net-admin/harv-fake/do/vmnet-update", {"namespace": "default", "name": "v20", "spec": {
        "description": "", "vlan": "21", "route_mode": "manual", "dhcp_server": "", "cidr": "10.0.21.0/24", "gateway": "10.0.21.1"}})
