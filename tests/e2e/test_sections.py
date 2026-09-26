"""v1.57.0 : les sections de Harvester sous Cluster, dans un navigateur.

Storage (Volumes, Images, Storage Classes), Network (VM Networks, Overlay,
Underlay), Add-ons et Security (Secrets, SSH Keys). Les listes sont servies
par une route simulée, comme les renverrait harv1.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

IMAGES = {"cluster": "harv-fake", "kind": "images", "items": [
    {"namespace": "default", "name": "image-b", "display_name": "zeta.qcow2", "source_type": "download",
     "url": "https://x/zeta.qcow2", "backend": "backingimage", "size": 700 * 2**20, "virtual_size": 10 * 2**30,
     "progress": 100, "storage_class": "lh-1", "state": "ready", "message": "", "volumes": 2,
     "used_by": ["default/web", "default/db"], "created": "2026-07-20T07:40:11Z"},
    {"namespace": "default", "name": "image-a", "display_name": "alpha.iso", "source_type": "upload",
     "url": None, "backend": "backingimage", "size": 2**30, "virtual_size": 2**30, "progress": 40,
     "storage_class": "lh-2", "state": "importing", "message": "", "volumes": 0, "used_by": [],
     "created": "2026-09-20T07:40:11Z"}]}
SECRETS = {"cluster": "harv-fake", "kind": "secrets", "system_hidden": 290, "items": [
    {"namespace": "default", "name": "web-ci", "type": "Opaque", "keys": ["networkdata", "userdata"],
     "system": False, "cloud_init": True, "used_by": ["default/web"], "created": "2026-09-01T00:00:00Z"}]}
ADDONS = {"cluster": "harv-fake", "kind": "addons", "items": [
    {"namespace": "harvester-system", "name": "harvester-seeder", "chart": "harvester-seeder", "version": "1.9.0",
     "enabled": False, "status": "AddonDisabled", "message": "", "created": None},
    {"namespace": "kube-system", "name": "descheduler", "chart": "descheduler", "version": "0.36.0",
     "enabled": True, "status": "AddonDeployFailed", "message": "chart not found", "created": None}]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def open_section(context, flask_server, tab, pane=None, lang="fr"):
    context.add_init_script(
        f"localStorage.setItem('harvester_ops_language','{lang}');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        f"localStorage.setItem('harvester_ops_current_tab','{tab}');"
        + (f"localStorage.setItem('harvester_ops_section_{tab}','{pane}');" if pane else ""))
    page = context.new_page()
    calls = []

    def objects(route, req):
        calls.append(req.url)
        kind = req.url.split("/api/cluster-objects/harv-fake/")[1].split("?")[0]
        body = {"images": IMAGES, "secrets": SECRETS, "addons": ADDONS}.get(kind, {"items": []})
        if kind == "secrets" and "all=1" in req.url:
            body = dict(SECRETS, system_hidden=0, items=SECRETS["items"] + [
                {"namespace": "kube-system", "name": "sa-token", "type": "kubernetes.io/service-account-token",
                 "keys": ["token"], "system": True, "cloud_init": False, "used_by": [], "created": None}])
        fulfill(route, body)
    page.route("**/api/cluster-objects/**", objects)
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.Sections && window.ResourceViews")
    return page, calls


def test_the_sidebar_has_the_harvester_sections(page):
    for tab in ("storage", "network", "addons", "security"):
        link = page.locator(f'.tab[data-tab="{tab}"]')
        assert link.count() == 1 and link.get_attribute("data-i18n-title"), tab
    # l'aperçu ne garde que Métriques et Cluster
    assert page.locator("[data-overview-tab]").evaluate_all("els => els.map(e => e.dataset.overviewTab)") \
        == ["metrics", "cluster"]


def test_images_list_sorts_and_filters(context, flask_server):
    page, _ = open_section(context, flask_server, "storage", "images")
    table = page.locator("#tab-storage .section-pane[data-pane='images'] .res-table")
    expect(table).to_be_visible(timeout=10000)
    names = lambda: table.locator("tbody tr td:first-child strong").all_inner_texts()  # noqa: E731
    assert names() == ["zeta.qcow2", "alpha.iso"]
    head = table.locator("th[data-sort='0']")
    assert head.get_attribute("data-tip")
    head.click()
    assert names() == ["alpha.iso", "zeta.qcow2"]
    table.locator("th[data-sort='0']").click()
    assert names() == ["zeta.qcow2", "alpha.iso"]
    table.locator("th[data-sort='2']").click()                  # taille : la plus petite d'abord
    assert names() == ["alpha.iso", "zeta.qcow2"]
    # le tri survit au rechargement
    page.reload()
    page.wait_for_function("window.ResourceViews")
    expect(page.locator("#tab-storage .res-table th.is-sorted")).to_have_attribute("data-sort", "2", timeout=10000)
    page.locator("#tab-storage .res-filter").fill("zeta")
    expect(page.locator("#tab-storage .res-table tbody tr")).to_have_count(1)
    expect(page.locator("#tab-storage .res-count")).to_have_text("1 affichés")


def test_an_image_row_opens_its_details_and_its_vms(context, flask_server):
    page, _ = open_section(context, flask_server, "storage", "images")
    row = page.locator("#tab-storage .res-row", has_text="zeta.qcow2")
    expect(row).to_be_visible(timeout=10000)
    assert row.locator("[data-vm='default/web']").get_attribute("data-tip")
    row.click()
    expect(page.locator("#tab-storage .res-details")).to_contain_text("https://x/zeta.qcow2")


def test_secrets_never_show_values_and_system_ones_on_demand(context, flask_server):
    page, calls = open_section(context, flask_server, "security", "secrets")
    body = page.locator("#tab-security .section-pane[data-pane='secrets']")
    expect(body.locator(".res-table")).to_be_visible(timeout=10000)
    expect(body.locator(".res-count")).to_contain_text("290 secrets système cachés")
    expect(body.locator(".res-key")).to_have_text(["networkdata", "userdata"])
    body.locator(".res-system-box").check()
    expect(body.locator(".res-table tbody tr")).to_have_count(2)
    assert any("all=1" in u for u in calls)


def test_addons_toggle_through_an_action(context, flask_server):
    page, _ = open_section(context, flask_server, "addons")
    posted = []

    def toggle(route, req):
        posted.append((req.url, req.post_data_json))
        fulfill(route, {"action_id": "add000000001", "addon": "harvester-system/harvester-seeder", "enabled": True}, 202)
    page.route("**/api/addons/harv-fake/**", toggle)
    page.route("**/api/stream/add000000001", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    view = page.locator("#tab-addons")
    expect(view.locator(".res-table")).to_be_visible(timeout=10000)
    # un échec se dit, avec son message
    expect(view.locator("tr", has_text="descheduler").locator(".badge.fail")).to_have_attribute("data-tip", "chart not found")
    btn = view.locator('[data-addon="harvester-system/harvester-seeder"]')
    assert btn.get_attribute("data-tip")
    btn.click()
    assert dialogs and "harvester-seeder" in dialogs[0]
    expect(view.locator(".res-feedback")).to_contain_text("harvester-seeder est activé", timeout=5000)
    assert posted[0][0].endswith("/api/addons/harv-fake/harvester-system/harvester-seeder")
    assert posted[0][1] == {"enabled": True}


def test_the_section_tab_is_remembered(context, flask_server):
    page, _ = open_section(context, flask_server, "network", "overlay")
    expect(page.locator('#tab-network [data-section-tab="overlay"]')).to_have_class("sub-tab tip active", timeout=10000)
    assert page.locator('#tab-network .section-pane[data-pane="overlay"]').is_visible()
    page.locator('#tab-network [data-section-tab="underlay"]').click()
    # (le script d'ouverture du test réécrit l'onglet à chaque chargement :
    # on lit donc ce qui est gardé plutôt que de recharger)
    assert page.evaluate("localStorage.getItem('harvester_ops_section_network')") == "underlay"
    assert page.locator('#tab-network .section-pane[data-pane="underlay"]').is_visible()
    assert page.locator('#tab-network .section-pane[data-pane="overlay"]').is_hidden()


def test_every_list_control_has_a_tooltip(context, flask_server):
    page, _ = open_section(context, flask_server, "storage", "images")
    expect(page.locator("#tab-storage .res-table")).to_be_visible(timeout=10000)
    missing = page.evaluate("""() => [...document.querySelectorAll(
        '#tab-storage .res-card button, #tab-storage .res-card input, #tab-storage .res-card th[data-sort]')]
        .filter(el => !el.getAttribute('data-tip')).map(el => el.outerHTML.slice(0, 60))""")
    assert missing == []
