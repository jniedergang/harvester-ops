"""v1.49.0 : la vue VPC (kube-ovn) et ses formulaires, dans un navigateur.

Le réseau est intercepté : relevé, contrôle, écriture et flux de l'action
sont simulés. Ce qui est vérifié : les blocs et les constats, les valeurs
proposées (CIDR libre, passerelle déduite, réseau nommé d'après le subnet,
NAT réservé au VPC par défaut), la demande envoyée, le refus d'une
suppression que la vue sait vouée à l'échec, et les boutons réservés aux
administrateurs.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


def subnet(name, vpc, cidr, **kw):
    base = {"name": name, "vpc": vpc, "cidr": cidr, "gateway": cidr.split("/")[0][:-1] + "1",
            "exclude": [], "provider": f"{name}.default.ovn", "nat": False, "private": False,
            "allow": [], "namespaces": [], "dhcp": True, "vlan": None, "underlay": False,
            "used": 1, "available": 252, "ready": True, "system": False, "managed": True,
            "network": f"default/{name}", "consumers": []}
    base.update(kw)
    return base


MODEL = {
    "unreachable": False, "kubeovn": True, "suggested_cidr": "10.200.2.0/24",
    "free_overlays": ["default/ovn-overlay"], "namespaces": ["default", "apps", "kube-system"],
    "node_ips": ["172.16.3.11"],
    "model": {
        "vpcs": [
            {"name": "ovn-cluster", "namespaces": [], "static_routes": [], "peerings": [],
             "subnets": [], "system": True, "managed": False, "subnet_list": [
                 subnet("ovn-default", "ovn-cluster", "10.54.0.0/16", system=True, provider="ovn",
                        network=None, nat=True, used=88, available=65445),
                 subnet("lab-a", "ovn-cluster", "10.200.0.0/24", nat=True, consumers=[
                     {"namespace": "default", "owner": "probe", "address": "10.200.0.2",
                      "kind": "VirtualMachine", "subnet": "lab-a"}])]},
            {"name": "lab", "namespaces": [], "static_routes": [], "peerings": [], "subnets": [],
             "system": False, "managed": True,
             "subnet_list": [subnet("lab-b", "lab", "10.200.1.0/24", used=1)]},
        ],
        "overlays": [],
        "findings": [{"code": "overlay-no-subnet", "level": "warn", "network": "default/ovn-overlay"}],
    },
}


def fulfill(route, body, status=200, ctype="application/json"):
    route.fulfill(status=status, content_type=ctype,
                  body=body if isinstance(body, str) else json.dumps(body))


@pytest.fixture
def board(context, flask_server):
    def make(role=None):
        context.add_init_script("localStorage.setItem('harvester_ops_language','fr');")
        page = context.new_page()
        calls = {"check": [], "apply": [], "delete": []}

        def on_check(route, request):
            calls["check"].append(json.loads(request.post_data))
            fulfill(route, {"blocked": False, "findings": [
                {"code": "vpc-isolated", "level": "ok", "facts": {"vpc": "lab"}}]})

        def on_apply(route, request):
            calls["apply"].append(json.loads(request.post_data))
            fulfill(route, {"action_id": "net000000001", "name": "x", "kind": "subnet"}, 202)

        def on_delete(route, request):
            calls["delete"].append(request.url)
            fulfill(route, {"action_id": "net000000001", "name": "x", "kind": "subnet"}, 202)

        page.route("**/api/kubeovn/harv-fake?*", lambda r, q: fulfill(r, MODEL))
        page.route("**/api/kubeovn/harv-fake", lambda r, q: fulfill(r, MODEL))
        page.route("**/api/kubeovn/harv-fake/check", on_check)
        page.route("**/api/kubeovn/harv-fake/apply", on_apply)
        page.route("**/api/kubeovn/harv-fake/subnet/**", on_delete)
        page.route("**/api/stream/net000000001", lambda r, q: fulfill(
            r, 'event: end\ndata: {"status": "done"}\n\n', ctype="text/event-stream"))
        page.goto(flask_server["base_url"], wait_until="domcontentloaded")
        page.wait_for_function("window.VpcBoard && window.i18n && window.Board")
        page.evaluate("""(role) => {
            document.querySelectorAll('.overview-subtab').forEach(p => p.hidden = p.dataset.subtab !== 'vpc');
            if (role) document.body.classList.add('roles-active', 'role-' + role);
            VpcBoard.start('harv-fake');
        }""", role)
        view = page.locator('.overview-subtab[data-subtab="vpc"]')
        expect(view.locator('.vpc-block[data-vpc="lab"]')).to_be_visible(timeout=5000)
        return page, view, calls
    return make


def test_blocks_findings_and_consumers(board):
    page, view, _ = board()
    expect(view.locator(".vpc-findings")).to_contain_text("default/ovn-overlay n'a pas de subnet")
    expect(view.locator('[data-vpc-serve="default/ovn-overlay"]')).to_be_visible()
    lab_a = view.locator('[data-subnet="lab-a"]')
    expect(lab_a).to_contain_text("default/probe")
    expect(lab_a).to_contain_text("10.200.0.2")
    # un subnet système se montre sans rien proposer de le changer
    assert view.locator('[data-subnet="ovn-default"] [data-vpc-edit-subnet]').count() == 0
    assert view.locator('[data-vpc-edit="ovn-cluster"]').count() == 0
    expect(view.locator('.vpc-block[data-vpc="lab"] .vsw-right')).to_contain_text("Isolé")
    for el in view.locator("button").all():
        assert el.get_attribute("data-tip") or el.get_attribute("data-tip-i18n"), el.inner_html()


def test_the_subnet_form_proposes_and_sends(board):
    page, view, calls = board()
    view.locator('[data-vpc-new-subnet="lab"]').click()
    m = page.locator(".vpc-modal")
    x = lambda k: m.locator(f'[data-x="{k}"]')
    expect(x("cidr")).to_have_value("10.200.2.0/24")
    expect(x("gateway")).to_have_value("10.200.2.1")
    expect(x("nat")).to_be_disabled()                       # VPC lab : pas de NAT
    x("name").fill("Web Front")
    expect(x("name")).to_have_value("web-front")
    expect(x("new_network")).to_have_value("default/web-front")
    x("cidr").fill("10.200.9.0/24")
    expect(x("gateway")).to_have_value("10.200.9.1")
    expect(x("report")).to_contain_text("aucune route vers l'extérieur", timeout=5000)
    body = calls["check"][-1]
    assert body["kind"] == "subnet" and body["update"] is False
    assert body["spec"]["vpc"] == "lab" and body["spec"]["new_network"] == "default/web-front"
    assert body["spec"]["nat"] is False and body["spec"]["dhcp"] is True
    m.locator("[data-vpc-save]").click()
    expect(view.locator(".vpc-feedback")).to_contain_text("Subnet web-front enregistré", timeout=5000)
    assert calls["apply"][-1]["spec"]["cidr"] == "10.200.9.0/24"


def test_serving_the_orphan_network(board):
    page, view, calls = board()
    view.locator('[data-vpc-serve="default/ovn-overlay"]').click()
    m = page.locator(".vpc-modal")
    expect(m.locator('[data-x="network_choice"]')).to_have_value("existing:default/ovn-overlay")
    expect(m.locator('[data-x="nat"]')).to_be_checked()       # VPC par défaut : NAT proposé
    m.locator('[data-x="name"]').fill("overlay-a")
    expect(m.locator('[data-x="report"]')).to_contain_text("Aucun blocage", timeout=5000)
    assert calls["check"][-1]["spec"]["network"] == "default/ovn-overlay"


def test_editing_keeps_what_kube_ovn_cannot_change(board):
    page, view, calls = board()
    view.locator('[data-vpc-edit-subnet="lab-b"]').click()
    m = page.locator(".vpc-modal")
    for k in ("name", "vpc", "cidr", "gateway", "network_choice"):
        expect(m.locator(f'[data-x="{k}"]')).to_be_disabled()
    m.locator('[data-x="dhcp"]').uncheck()
    for _ in range(50):                       # le contrôle part 500 ms après la saisie
        if calls["check"] and calls["check"][-1]["spec"]["dhcp"] is False:
            break
        page.wait_for_timeout(100)
    body = calls["check"][-1]
    assert body["update"] is True and body["spec"]["dhcp"] is False
    assert body["spec"]["network"] == "default/lab-b"


def test_a_used_subnet_is_not_even_sent_for_deletion(board):
    page, view, calls = board()
    page.on("dialog", lambda d: d.accept())
    view.locator('[data-vpc-delete-subnet="lab-a"]').click()
    expect(view.locator(".vpc-feedback")).to_contain_text("encore utilisé par default/probe")
    assert calls["delete"] == []
    view.locator('[data-vpc-delete-subnet="lab-b"]').click()
    expect(view.locator(".vpc-feedback")).to_contain_text("lab-b supprimé", timeout=5000)
    assert calls["delete"] and "with_network=1" in calls["delete"][-1]


def test_operators_are_not_offered_the_forms(board):
    page, view, _ = board(role="operator")
    assert view.locator(".needs-admin").count() > 0
    for el in view.locator(".needs-admin").all():
        expect(el).to_be_hidden()
