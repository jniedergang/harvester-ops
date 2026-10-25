"""v1.66.0 : les fenêtres kube-ovn dans un navigateur : santé dite en tête,
réseaux fournisseurs (carte d'un bond refusée), passerelle (subnets de la VPC,
IP LAN proposée), IP externe (adresse suivante proposée), DNAT, politiques
(règles, modification, suppression avec le namespace)."""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto  # noqa: E402

STATE = {"kubeovn": True,
         "health": {"healthy": False, "problems": ["ovn-central 0/3 ready"]},
         "providers": [{"name": "ext", "interface": "enp4s0", "ready": True, "ready_nodes": ["n1", "n2"], "errors": [], "vlans": ["ext-v0"]}],
         "vlans": [{"name": "ext-v0", "id": 0, "provider": "ext", "conflict": False, "subnets": ["ext-lan"]}],
         "externals": [{"name": "ext-lan", "cidr": "172.16.0.0/16", "gateway": "172.16.0.1", "vlan": "ext-v0", "provider": "ext",
                        "free": "172.16.2.90..172.16.2.95", "available": 1, "using": 2, "ready": True, "message": "",
                        "next_eip": "172.16.2.92"}],
         "gateways": [{"name": "gw1", "vpc": "vpc1", "subnet": "sn1", "lan_ip": "10.1.0.254", "external": "ext-lan", "ready": True,
                       "pod": "vpc-nat-gw-gw1-0", "node": "n1", "phase": "Running", "interfaces": [], "route": True, "eips": ["e1"]}],
         "eips": [{"name": "e1", "gateway": "gw1", "external": "ext-lan", "ip": "172.16.2.91", "ready": True, "nat": "dnat", "rules": ["d1"]}],
         "snats": [], "dnats": [{"name": "d1", "kind": "dnat", "eip": "e1", "ready": True, "ip": "172.16.2.91", "protocol": "tcp",
                                 "external_port": "2222", "internal_ip": "10.1.0.10", "internal_port": "22"}],
         "vpcs": ["ovn-cluster", "vpc1", "vpc2"],
         "tenant_subnets": [{"name": "sn1", "vpc": "vpc1", "cidr": "10.1.0.0/24", "gateway": "10.1.0.1", "network": "default/sn1"},
                            {"name": "sn2", "vpc": "vpc2", "cidr": "10.2.0.0/24", "gateway": "10.2.0.1", "network": "default/sn2"}],
         "nodes": ["n1", "n2"], "nics": [{"name": "enp1s0", "taken_on": ["n1", "n2"]}, {"name": "enp4s0", "taken_on": []}]}
POLICIES = {"items": [{"namespace": "default", "name": "np1", "target": "VMs web", "vms": ["web"], "types": ["Ingress"],
                       "rules": {"ingress": [{"peers": ["172.16.0.0/16"], "ports": ["TCP 22"]}]}, "lax": True, "editable": True,
                       "spec": {"name": "np1", "namespace": "default", "vms": ["web"], "lax": True,
                                "ingress": [{"peers": [{"kind": "cidr", "cidr": "172.16.0.0/16"}], "ports": [{"port": 22, "protocol": "TCP"}]}]}}],
            "vms": {"default": ["web", "db"]}}


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
        sent.append((req.method, req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "ovn000000166"}, 202)
    page.route("**/api/kubeovn/harv-fake/extra", lambda r, q: fulfill(r, STATE))
    page.route("**/api/kubeovn/harv-fake/policies", lambda r, q: fulfill(r, POLICIES))
    page.route("**/api/kubeovn/harv-fake/extra/**", writes)
    page.route("**/api/stream/ovn000000166", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address("namespaces"))
    page.wait_for_function("window.OvnExtra && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    return page, sent


def test_an_unhealthy_kube_ovn_is_said_and_a_bonded_nic_refused(ui):
    page, sent = ui
    page.evaluate("OvnExtra.open('underlay', 'harv-fake')")
    w = page.locator("#fp-ox-underlay-harv-fake")
    expect(w.locator('[data-ox="health"]')).to_contain_text("ovn-central 0/3 ready", timeout=8000)
    expect(w.locator('[data-ox-del="provider"][data-name="ext"]')).to_be_disabled()      # des VLANs l'utilisent
    w.locator('[data-ox="new"]').click()
    f = w.locator('[data-ox="form"] form')
    expect(f.locator('[name="interface"] option[value="enp1s0"]')).to_have_attribute("disabled", "")
    f.locator('[name="name"]').fill("ext2")
    f.locator('[name="exclude"][value="n2"]').check()
    f.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("POST", "api/kubeovn/harv-fake/extra/provider",
                        {"spec": {"name": "ext2", "interface": "enp4s0", "exclude_nodes": ["n2"]}})


def test_a_gateway_form_follows_the_vpc_and_proposes_its_lan_ip(ui):
    page, sent = ui
    page.evaluate("OvnExtra.open('nat', 'harv-fake')")
    w = page.locator("#fp-ox-nat-harv-fake")
    expect(w.locator('[data-ox-del="gateway"][data-name="gw1"]')).to_be_disabled(timeout=8000)   # une IP externe l'utilise
    w.locator('[data-ox="new"]').click()
    f = w.locator('[data-ox="form"] form')
    expect(f.locator('[name="vpc"] option[value="ovn-cluster"]')).to_have_count(0)
    f.locator('[name="vpc"]').select_option("vpc2")
    expect(f.locator('[name="subnet"]')).to_have_value("sn2")
    expect(f.locator('[name="lan_ip"]')).to_have_value("10.2.0.254")
    f.locator('[name="name"]').fill("gw2")
    f.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][2]["spec"] == {"name": "gw2", "vpc": "vpc2", "subnet": "sn2", "lan_ip": "10.2.0.254",
                                   "external": "ext-lan", "add_route": True}


def test_an_external_ip_gets_the_next_reserved_address_and_a_dnat_its_ports(ui):
    page, sent = ui
    page.evaluate("OvnExtra.open('nat', 'harv-fake')")
    w = page.locator("#fp-ox-nat-harv-fake")
    w.locator('[data-ox-tab="eips"]').click()
    expect(w.locator('[data-ox-del="eip"][data-name="e1"]')).to_be_disabled(timeout=8000)
    w.locator('[data-ox="new"]').click()
    f = w.locator('[data-ox="form"] form')
    expect(f.locator('[name="ip"]')).to_have_value("172.16.2.92")
    f.locator('[name="name"]').fill("e2")
    f.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][2]["spec"] == {"name": "e2", "gateway": "gw1", "external": "ext-lan", "ip": "172.16.2.92"}
    w.locator('[data-ox="cancel"]').click() if w.locator('[data-ox="cancel"]').count() else None
    w.locator('[data-ox-tab="dnats"]').click()
    expect(w.locator('tr', has_text="172.16.2.91:2222")).to_have_count(1, timeout=8000)
    w.locator('[data-ox="new"]').click()
    f = w.locator('[data-ox="form"] form')
    f.locator('[name="name"]').fill("d2")
    f.locator('[name="external_port"]').fill("8080")
    f.locator('[name="internal_ip"]').fill("10.1.0.11")
    f.locator('[name="internal_port"]').fill("80")
    f.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][2]["spec"] == {"name": "d2", "eip": "e1", "protocol": "tcp", "external_port": "8080",
                                   "internal_ip": "10.1.0.11", "internal_port": "80"}


def test_a_policy_is_written_edited_and_deleted_with_its_namespace(ui):
    page, sent = ui
    page.evaluate("OvnExtra.openPolicies('harv-fake')")
    w = page.locator("#fp-ox-policies-harv-fake")
    expect(w.locator('tr[data-pol-ref="default/np1"]')).to_contain_text("172.16.0.0/16", timeout=8000)
    w.locator('[data-pol="new"]').click()
    f = w.locator('[data-pol="form"] form')
    f.locator('[name="name"]').fill("np2")
    f.locator('[name="vm"][value="db"]').check()
    f.locator('[name="use-egress"]').check()
    f.locator('[data-rule-add="egress"]').click()
    f.locator('.ox-rule [name="peer-kind"]').select_option("vms")
    f.locator('.ox-rule [name="peer-value"]').fill("web")
    f.locator('.ox-rule [name="ports"]').fill("5432/tcp, 53/udp")
    f.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("POST", "api/kubeovn/harv-fake/extra/policy", {"spec": {
        "name": "np2", "namespace": "default", "vms": ["db"], "lax": True,
        "egress": [{"peers": [{"kind": "vms", "vms": ["web"]}], "ports": [{"port": 5432, "protocol": "TCP"}, {"port": 53, "protocol": "UDP"}]}]},
        "update": False})
    w.locator('[data-pol="cancel"]').click() if w.locator('[data-pol="cancel"]').count() else None
    w.locator('tr[data-pol-ref="default/np1"] [data-pol-act="edit"]').click()
    f = w.locator('[data-pol="form"] form')
    expect(f.locator('.ox-rule [name="peer-value"]')).to_have_value("172.16.0.0/16")
    expect(f.locator('.ox-rule [name="ports"]')).to_have_value("22/tcp")
    f.locator('.ox-rule [name="peer-value"]').fill("10.0.0.0/8")
    f.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    method, path, body = sent[-1]
    assert body["update"] is True and body["spec"]["ingress"][0]["peers"] == [{"kind": "cidr", "cidr": "10.0.0.0/8"}]
    w.locator('tr[data-pol-ref="default/np1"] [data-pol-act="delete"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][:2] == ("DELETE", "api/kubeovn/harv-fake/extra/policy/np1?namespace=default")


def test_a_gateway_with_its_pod_network_replaced_offers_repair(ui):
    page, sent = ui
    broken = json.loads(json.dumps(STATE))
    broken["gateways"][0].update(broken=True, ready=False)
    page.unroute("**/api/kubeovn/harv-fake/extra")
    page.route("**/api/kubeovn/harv-fake/extra", lambda r, q: fulfill(r, broken))
    page.evaluate("OvnExtra.open('nat', 'harv-fake')")
    w = page.locator("#fp-ox-nat-harv-fake")
    expect(w.locator('tr', has_text="gw1").locator(".badge.fail")).to_have_count(1, timeout=8000)
    w.locator('[data-ox-repair="gw1"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("POST", "api/kubeovn/harv-fake/extra/repair", {"spec": {"name": "gw1"}})
