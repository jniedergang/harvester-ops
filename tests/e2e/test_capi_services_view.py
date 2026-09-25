"""v1.52.0 (B2) : l'onglet Services de Cluster API, dans un navigateur.

Le réseau est intercepté : relevé des services, contrôle, déploiement,
retrait et flux de l'action sont simulés. On vérifie le catalogue, l'état
des releases, le formulaire (réglages propres à chaque service, contrôle en
français, aperçu des valeurs), l'envoi et le suivi.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

CATALOG = [
    {"key": "coredns", "title": "DNS (CoreDNS)", "repo": "https://coredns.github.io/helm", "chart": "coredns",
     "version": "1.47.1", "namespace": "dns", "params": {"upstream": "1.1.1.1 9.9.9.9", "hosts": "", "ipam": "dhcp"},
     "values": "servers:\n  - plugins:\n      - name: forward\n        parameters: . __UPSTREAM__\n"},
    {"key": "podinfo", "title": "podinfo", "repo": "https://stefanprodan.github.io/podinfo", "chart": "podinfo",
     "version": "6.15.0", "namespace": "podinfo", "params": {"ipam": "dhcp"},
     "values": "service:\n  type: LoadBalancer\n"},
]
SERVICES = [{"namespace": "essai", "name": "podinfo", "service": "podinfo", "chart": "podinfo", "version": "6.15.0",
             "repo": "https://stefanprodan.github.io/podinfo", "release_namespace": "podinfo", "managed": True,
             "ready": True, "message": "",
             "releases": [{"cluster": "essai", "status": "deployed", "revision": 1, "ready": True, "message": ""}]}]
STATE = {"caaph": True, "clusters": ["essai/essai", "lab/lab"], "catalog": CATALOG, "services": SERVICES}


def fulfill(route, body, status=200, ctype="application/json"):
    route.fulfill(status=status, content_type=ctype,
                  body=body if isinstance(body, str) else json.dumps(body))


def sse(events):
    return "".join(f"event: {t}\ndata: {json.dumps(d)}\n\n" for t, d in events)


@pytest.fixture
def view(context, flask_server):
    def make(state=STATE, check=None, stream=()):
        context.add_init_script("""localStorage.setItem('harvester_ops_language','fr');
          localStorage.setItem('harvester_ops_current_tab','automation');
          localStorage.setItem('harvester_ops_automation_subtab','capi');
          localStorage.setItem('harvester_ops_capi_subtab','services');""")
        page = context.new_page()
        calls = {"check": [], "deploy": [], "delete": []}

        def on_check(route, request):
            calls["check"].append(json.loads(request.post_data or "{}"))
            fulfill(route, check or {"blocked": False, "values": "forward: . 172.16.0.1\n", "findings": [
                {"code": "service-exists", "level": "warn", "facts": {"name": "coredns"}}]})

        def on_deploy(route, request):
            calls["deploy"].append(json.loads(request.post_data or "{}"))
            fulfill(route, {"action_id": "svc000000001", "name": "coredns", "cluster": "essai/essai"}, 202)

        def on_delete(route, request):
            calls["delete"].append(request.url)
            fulfill(route, {"action_id": "svc000000001", "name": "podinfo"}, 202)

        page.route("**/api/capi/harv-fake/services*", lambda r, q: fulfill(r, state))
        page.route("**/api/capi/harv-fake/service-check", on_check)
        page.route("**/api/capi/harv-fake/service-deploy", on_deploy)
        page.route("**/api/capi/harv-fake/service/**", on_delete)
        page.route("**/api/stream/svc000000001",
                   lambda r, q: fulfill(r, sse(list(stream)), ctype="text/event-stream"))
        page.goto(flask_server["base_url"], wait_until="domcontentloaded")
        page.wait_for_function("window.CapiServices && window.i18n")
        page.evaluate("document.querySelector('.tab[data-tab=\"automation\"]')?.click()")
        page.evaluate("document.querySelector('#tab-automation .sub-tab[data-capi-tab=\"services\"]')?.click()")
        host = page.locator("#capi-services-body")
        expect(host.locator(".svc-card").first).to_be_visible(timeout=8000)
        return page, host, calls
    return make


def x(root, key):
    return root.locator(f'[data-x="{key}"]')


def test_catalog_and_deployed_services(view):
    page, host, _ = view()
    cards = host.locator(".svc-card")
    expect(cards).to_have_count(3)
    expect(cards.nth(0)).to_contain_text("Serveur DNS (CoreDNS)")
    expect(cards.nth(2)).to_contain_text("Chart Helm libre")
    row = host.locator('tr[data-svc="essai/podinfo"]')
    expect(row).to_contain_text("Application témoin (podinfo)")
    expect(row.locator(".badge.ok")).to_contain_text("essai · deployed · r1")
    for b in host.locator("button").all():
        assert b.get_attribute("data-tip"), b.inner_text()
    assert all(not b.is_disabled() for b in host.locator("[data-svc-deploy]").all())


def test_without_caaph_the_view_says_so_and_deploy_is_off(view):
    page, host, _ = view(state=dict(STATE, caaph=False, services=[]))
    expect(host.locator('[data-code="caaph-missing"]')).to_contain_text("CAAPH")
    assert all(b.is_disabled() for b in host.locator("[data-svc-deploy]").all())
    expect(host).to_contain_text("Aucun service déployé")
    host.locator("[data-svc-goto]").click()
    expect(page.locator('#tab-automation .sub-tab[data-capi-tab="install"]')).to_have_class("sub-tab active")


def test_the_form_checks_previews_and_deploys(view):
    page, host, calls = view(stream=[
        ("step", {"step": "install", "status": "running", "message": "release deployed (revision 1)"}),
        ("end", {"status": "done"})])
    host.locator('[data-svc-deploy="coredns"]').click()
    modal = page.locator(".svc-modal")
    expect(modal).to_be_visible()
    expect(x(modal, "name")).to_have_value("coredns")
    expect(x(modal, "namespace")).to_have_value("dns")
    expect(x(modal, "version")).to_have_value("1.47.1")
    for el in modal.locator("input[data-x], select[data-x], textarea[data-x], [data-p]").all():
        assert el.get_attribute("data-tip"), el.get_attribute("data-x") or el.get_attribute("data-p")
    modal.locator('[data-p="upstream"]').fill("172.16.0.1")
    modal.locator('[data-p="hosts"]').fill("172.16.3.60 web.lab")
    # le contrôle part 500 ms après la dernière frappe
    for _ in range(50):
        if calls["check"] and calls["check"][-1]["params"].get("hosts"):
            break
        page.wait_for_timeout(100)
    expect(x(modal, "report")).to_contain_text("existe déjà", timeout=5000)
    expect(x(modal, "preview")).to_have_text("forward: . 172.16.0.1\n")
    body = calls["check"][-1]
    assert body["service"] == "coredns" and body["cluster"] == "essai/essai"
    assert body["params"] == {"upstream": "172.16.0.1", "hosts": "172.16.3.60 web.lab", "ipam": "dhcp"}
    assert "values" not in body                 # les valeurs du catalogue restent au serveur
    save = modal.locator("[data-svc-save]")
    expect(save).to_be_enabled()
    save.click()
    expect(host.locator(".svc-feedback")).to_contain_text("coredns installé sur essai/essai", timeout=5000)
    assert calls["deploy"][-1]["params"]["upstream"] == "172.16.0.1"


def test_a_free_chart_asks_for_its_repository(view):
    page, host, calls = view(check={"blocked": True, "values": "", "findings": [
        {"code": "invalid-repo", "level": "block", "facts": {"repo": ""}}]})
    host.locator('[data-svc-deploy="coredns"]').click()
    modal = page.locator(".svc-modal")
    x(modal, "service").select_option("custom")
    expect(x(modal, "repo")).to_be_visible()
    expect(x(modal, "name")).to_have_value("")
    assert modal.locator("[data-p]").count() == 0
    expect(x(modal, "report")).to_contain_text("n'est pas l'adresse d'un dépôt", timeout=5000)
    expect(modal.locator("[data-svc-save]")).to_be_disabled()
    assert calls["check"][-1]["service"] == "custom" and "values" in calls["check"][-1]


def test_removal_asks_then_follows(view):
    page, host, calls = view(stream=[("end", {"status": "done"})])
    asked = []
    page.on("dialog", lambda d: (asked.append(d.message), d.accept()))
    host.locator('[data-svc-remove="essai/podinfo"]').click()
    expect(host.locator(".svc-feedback")).to_contain_text("podinfo retiré", timeout=5000)
    assert "CAAPH" in asked[0]
    assert calls["delete"][-1].endswith("/api/capi/harv-fake/service/essai/podinfo")
