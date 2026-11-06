"""2.6.0 (tâche 10) : « Depuis un registre » dans Cluster API > Services
(routes simulées) : choix d'un chart publié, demande envoyée avec sa
source (le serveur en tire dépôt, chart et version), constat
d'avertissement redit en français, nom de chart hostile échappé."""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402
from test_capi_services_view import STATE, fulfill  # noqa: E402

HOSTILE = '<img src=x onerror="window.__xss=1">'
CHARTS = [
    {"registry": "amorcage", "package": "socle", "version": "2026.11", "item": "podinfo", "chart": "podinfo",
     "chart_version": "6.9.2"},
    {"registry": "client", "package": "outils", "version": "1.0.0", "item": "evil", "chart": HOSTILE,
     "chart_version": "1.0.0"},
]


@pytest.fixture
def view(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','fr');")
    page = context.new_page()
    calls = {"check": [], "deploy": []}

    def on_check(route, request):
        calls["check"].append(json.loads(request.post_data or "{}"))
        fulfill(route, {"blocked": False, "values": "ui:\n  color: blue\n", "findings": [
            {"code": "service-registry-not-mirrored", "level": "warn",
             "facts": {"registry": "amorcage", "cluster": "essai/essai"}}]})

    def on_deploy(route, request):
        calls["deploy"].append(json.loads(request.post_data or "{}"))
        fulfill(route, {"action_id": "svc000000002", "name": "podinfo", "cluster": "essai/essai"}, 202)
    page.route("**/api/capi/harv-fake/services*", lambda r, q: fulfill(r, dict(STATE, registry_charts=CHARTS)))
    page.route("**/api/capi/harv-fake/service-check", on_check)
    page.route("**/api/capi/harv-fake/service-deploy", on_deploy)
    page.route("**/api/stream/svc000000002", lambda r, q: fulfill(r, "", ctype="text/event-stream"))
    goto(page, flask_server["base_url"], "#automation/capi/services")
    page.wait_for_function("window.CapiServices && window.i18n")
    host = page.locator("#capi-services-body")
    expect(host.locator(".svc-card").first).to_be_visible(timeout=8000)
    return page, host, calls


def test_a_published_chart_is_deployed_from_its_registry(view):
    page, host, calls = view
    card = host.locator('[data-svc-deploy="registry"]')
    expect(card).to_be_enabled()
    expect(host.locator(".svc-card").nth(2)).to_contain_text("2 chart(s)")
    card.click()
    modal = page.locator(".svc-modal")
    source = modal.locator('[data-x="source"]')
    expect(source.locator("option")).to_have_count(2)
    expect(source.locator("option").nth(1)).to_contain_text(HOSTILE)           # texte, jamais du HTML
    assert page.evaluate("window.__xss") is None
    expect(modal.locator('[data-x="name"]')).to_have_value("podinfo")
    expect(modal.locator('[data-x="version"]')).to_have_value("6.9.2")
    expect(modal.locator('[data-code="service-registry-not-mirrored"]')).to_contain_text(
        "ne tire pas ses images de amorcage", timeout=5000)
    body = calls["check"][-1]
    assert body["source"] == {"registry": "amorcage", "package": "socle", "version": "2026.11", "item": "podinfo"}
    assert "service" not in body and "version" not in body and "values" not in body
    assert all(t.get_attribute("data-tip") for t in modal.locator(".tip").all())
    modal.locator("[data-svc-save]").click()
    expect(modal).to_have_count(0, timeout=5000)
    assert calls["deploy"][-1]["source"]["item"] == "podinfo"
