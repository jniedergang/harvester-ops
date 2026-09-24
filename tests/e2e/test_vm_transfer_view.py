"""v1.45.0 : la fenêtre « Migrer » et le magasin d'exports, dans un navigateur.

Une seule fenêtre, trois destinations (décision de l'exploitant) : un autre
nœud, un autre cluster, un fichier. Ce que les tests serveur ne voient pas :
que le contrôle préalable s'affiche dans la langue de l'interface, blocages
en tête, que « Lancer » reste grisé tant qu'il bloque, qu'une
correspondance choisie relance le contrôle et part avec la demande, et que
l'arrêt court n'est proposé qu'avec une cible de sauvegarde commune.

Le réseau est intercepté : aucun transfert réel ici (ils ont été faits sur
le banc harvlab / harvlab2, consignés dans le CHANGELOG).
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

VM = "/api/vm/harv-fake/default/vm1"


def blocked_check():
    return {"engine": "file", "reason": "different-targets", "blocked": True,
            "findings": [
                {"code": "network-unmapped", "level": "block",
                 "facts": {"network": "default/lab", "mapped": None}},
                {"code": "replicas-degraded", "level": "warn",
                 "facts": {"storage_class": "harvester-longhorn", "replicas": 3, "nodes": 1}},
                {"code": "engine", "level": "ok",
                 "facts": {"engine": "file", "reason": "different-targets"}}],
            "mappings": {"networks": {"default/lab": None},
                         "storage_classes": {"harv-rep1": "harvester-longhorn"}},
            "target": {"cluster": "other", "networks": ["default/other", "default/vlan10"],
                       "storage_classes": ["harvester-longhorn", "harv-rep1"],
                       "namespaces": ["default"]}}


def ok_check():
    d = blocked_check()
    d["blocked"] = False
    d["findings"] = [f for f in d["findings"] if f["level"] != "block"]
    return d


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def page_with_api(context, flask_server):
    def make(lang="en"):
        context.add_init_script(f"localStorage.setItem('harvester_ops_language','{lang}');")
        page = context.new_page()
        calls = {"check": [], "start": []}

        def check(route, request):
            body = json.loads(request.post_data or "{}")
            calls["check"].append(body)
            mapped = (body.get("networks") or {}).get("default/lab")
            fulfill(route, ok_check() if mapped else blocked_check())

        def start(route, request):
            calls["start"].append(json.loads(request.post_data or "{}"))
            fulfill(route, {"action_id": "abc123def456"}, 201)

        page.route("**/api/clusters", lambda r, q: fulfill(r, {"clusters": [
            {"name": "harv-fake"}, {"name": "other"}]}))
        page.route(f"**{VM}/migrate-info", lambda r, q: fulfill(r, {
            "current_node": "n1", "phase": "Running",
            "nodes": [{"name": "n1", "ready": "True", "schedulable": True, "current": True}],
            "migrations": []}))
        page.route(f"**{VM}/transfer/check", check)
        page.route(f"**{VM}/transfer", start)
        page.route("**/api/exports", lambda r, q: fulfill(r, {"free": 50 * 1024 ** 3, "exports": [
            {"name": "vm1-20260925.hvx", "size": 99_000_000, "complete": True, "vm": "vm1",
             "namespace": "default", "cluster": "harvlab", "version": "v1.8.2",
             "created": "2026-09-25T00:28:42Z", "disks": [], "networks": []}]}))
        page.goto(flask_server["base_url"], wait_until="domcontentloaded")
        page.wait_for_function("window.VMMigrate && window.VMTransfer && window.i18n")
        page.wait_for_timeout(500)
        return page, calls
    return make


def open_migrate(page):
    page.evaluate("VMMigrate.open('harv-fake', 'default', 'vm1')")
    panel = page.locator("#fp-vm-migrate-harv-fake-default-vm1")
    expect(panel).to_be_visible(timeout=5000)
    return panel


def test_three_destinations_node_first(page_with_api):
    page, _ = page_with_api()
    panel = open_migrate(page)
    dests = panel.locator("[data-dest]")
    expect(dests).to_have_count(3)
    expect(panel.locator('[data-dest="node"]')).to_have_class("sub-tab tip active")
    expect(panel.locator('[data-pane="node"]')).to_be_visible()
    expect(panel.locator('[data-pane="node"]')).to_contain_text("n1")
    expect(panel.locator('[data-pane="cluster"]')).to_be_hidden()


def test_a_blocked_transfer_cannot_start_until_mapped(page_with_api):
    page, calls = page_with_api()
    panel = open_migrate(page)
    panel.locator('[data-dest="cluster"]').click()
    pane = panel.locator('[data-pane="cluster"]')
    report = pane.locator('[data-x="report"]')
    expect(report).to_contain_text("has no counterpart on the target", timeout=5000)
    # le blocage passe avant l'avertissement
    expect(report.locator(".sto-finding").first).to_have_attribute("data-level", "block")
    expect(report).to_contain_text("will run degraded")
    expect(pane.locator('[data-x="start"]')).to_be_disabled()
    # sans cible commune, pas d'arrêt court
    expect(pane.locator('[data-x="mode"] option[value="short"]')).to_have_js_property("disabled", True)
    # la correspondance choisie relance le contrôle et débloque
    pane.locator('select[data-map="networks"][data-src="default/lab"]').select_option("default/vlan10")
    expect(pane.locator('[data-x="start"]')).to_be_enabled(timeout=5000)
    expect(report).to_contain_text("No blocker")
    assert calls["check"][-1]["networks"] == {"default/lab": "default/vlan10"}
    assert calls["check"][-1]["to"] == "other"
    pane.locator('[data-x="start"]').click()
    expect(pane.locator('[data-x="feedback"]')).to_contain_text("abc123def456", timeout=5000)
    sent = calls["start"][-1]
    assert sent["to"] == "other" and sent["networks"] == {"default/lab": "default/vlan10"}
    assert sent["storage_classes"] == {"harv-rep1": "harvester-longhorn"}


def test_keeping_the_source_running_forces_new_macs(page_with_api):
    page, calls = page_with_api()
    panel = open_migrate(page)
    panel.locator('[data-dest="cluster"]').click()
    pane = panel.locator('[data-pane="cluster"]')
    expect(pane.locator('[data-x="report"] .sto-finding').first).to_be_visible(timeout=5000)
    mac = pane.locator('[data-x="keep_mac"]')
    expect(mac).to_be_checked()
    pane.locator('[data-x="source"]').select_option("running")
    expect(mac).to_be_disabled()
    expect(mac).not_to_be_checked()


def test_export_destination_uses_the_same_check(page_with_api):
    page, calls = page_with_api()
    panel = open_migrate(page)
    panel.locator('[data-dest="file"]').click()
    pane = panel.locator('[data-pane="file"]')
    expect(pane.locator('[data-x="report"] .sto-finding').first).to_be_visible(timeout=5000)
    assert "to" not in calls["check"][-1]
    expect(pane.locator('[data-x="to"]')).to_have_count(0)


def test_the_window_speaks_french(page_with_api):
    page, _ = page_with_api("fr")
    panel = open_migrate(page)
    expect(panel.locator('[data-dest="cluster"]')).to_contain_text("Un autre cluster")
    panel.locator('[data-dest="cluster"]').click()
    expect(panel.locator('[data-pane="cluster"] [data-x="report"]')).to_contain_text(
        "n'a pas d'équivalent sur la cible", timeout=5000)


def test_the_store_lists_and_opens_an_import(page_with_api):
    page, _ = page_with_api()
    page.evaluate("VMTransfer.openStore('harv-fake')")
    store = page.locator("#fp-vm-exports")
    expect(store).to_contain_text("vm1-20260925.hvx", timeout=5000)
    expect(store).to_contain_text("harvlab")
    expect(store.locator('a[href*="/download"]')).to_have_count(1)
    store.locator('[data-act="import"]').click()
    expect(page.locator('#fp-vm-import-vm1-20260925\\.hvx')).to_be_visible(timeout=5000)


def test_mappings_follow_a_quickly_changed_target(context, flask_server):
    """Vécu en réel : choisir un autre cluster puis retoucher le nom aussitôt
    écartait la réponse du premier contrôle, et les correspondances restaient
    celles du cluster d'avant (« choisir... » alors que le réseau existait)."""
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()

    seen = []

    def check(route, request):
        body = json.loads(request.post_data or "{}")
        to = body.get("to")
        seen.append((to, body.get("name"), (body.get("networks") or {}).get("default/lab")))
        nets = ["default/lab"] if to == "second" else ["default/other"]
        mapped = "default/lab" if to == "second" else None
        page.wait_for_timeout(300 if to == "second" else 0)
        fulfill(route, {"engine": "backup", "reason": "shared-target", "blocked": False,
                        "findings": [{"code": "engine", "level": "ok", "facts": {}}],
                        "mappings": {"networks": {"default/lab": mapped}, "storage_classes": {}},
                        "target": {"cluster": to, "networks": nets, "storage_classes": []}})

    page.route("**/api/clusters", lambda r, q: fulfill(r, {"clusters": [
        {"name": "harv-fake"}, {"name": "first"}, {"name": "second"}]}))
    page.route(f"**{VM}/migrate-info", lambda r, q: fulfill(r, {"phase": "Running", "nodes": [], "migrations": []}))
    page.route(f"**{VM}/transfer/check", check)
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.VMMigrate && window.VMTransfer")
    page.evaluate("VMMigrate.open('harv-fake', 'default', 'vm1', 'cluster')")
    pane = page.locator('#fp-vm-migrate-harv-fake-default-vm1 [data-pane="cluster"]')
    expect(pane.locator('[data-x="report"] .sto-finding').first).to_be_visible(timeout=5000)
    pane.locator('[data-x="to"]').select_option("second")
    pane.locator('[data-x="name"]').fill("renamed")
    pane.locator('[data-x="name"]').dispatch_event("change")
    sel = pane.locator('select[data-map="networks"][data-src="default/lab"]')
    expect(sel).to_have_value("default/lab", timeout=5000)
