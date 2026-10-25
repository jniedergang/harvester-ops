"""2.0.0 : le menu latéral des hôtes (host-nav.js).

La liste des hôtes (route /api/hosts interceptée : 60 clusters, dont 2 en
alerte, et 40 hôtes KVM) : hauteur bornée, tête de famille au format
traduit, recherche par nom et par étiquette, « / » qui donne le focus,
Entrée qui ouvre l'hôte, étoile gardée par la console (appel PUT vu,
retrouvée après rechargement), alertes visibles dans une famille repliée,
clavier, info-bulle sur chaque contrôle, fenêtre « Ajouter un hôte »,
« Aller à la VM ».
"""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto, settle  # noqa: E402

REAL = "harv-fake"          # le seul cluster que le serveur de test déclare


def fleet():
    hosts = []
    for i in range(1, 60):
        state = "unreachable" if i in (17, 41) else "ok"
        hosts.append({"kind": "harvester", "name": f"harv-site-{i:02d}", "state": state, "error": None,
                      "vm_count": i % 23, "labels": {"site": ["nantes", "lille", "lyon", "rennes"][i % 4]},
                      "version": "v1.9.0", "favorite": False, "folder": None, "origin": "console"})
    hosts.append({"kind": "harvester", "name": REAL, "state": "ok", "error": None, "vm_count": 3,
                  "labels": {}, "version": "v1.9.0", "favorite": False, "folder": None, "origin": "config"})
    for i in range(1, 41):
        hosts.append({"kind": "kvm", "name": f"kvm-node-{i:02d}", "state": "ok", "error": None, "vm_count": i % 9,
                      "labels": {"site": "paris"}, "version": "12.7.0", "favorite": False, "folder": None,
                      "origin": "console"})
    return hosts


@pytest.fixture
def menu(context, flask_server):
    """Page sur l'aperçu de harv-fake, /api/hosts et les favoris simulés."""
    state = {"hosts": fleet(), "puts": [], "deletes": []}

    def hosts_route(route, request):
        fams = {"harvester": {"ok": 0, "alert": 0, "unknown": 0}, "kvm": {"ok": 0, "alert": 0, "unknown": 0}}
        for h in state["hosts"]:
            fams[h["kind"]]["alert" if h["state"] != "ok" else "ok"] += 1
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"families": fams, "hosts": state["hosts"]}))

    def fav_route(route, request):
        kind, name = request.url.split("/api/me/favorites/")[1].split("/")
        for h in state["hosts"]:
            if (h["kind"], h["name"]) == (kind, name):
                h["favorite"] = request.method == "PUT"
        state["puts" if request.method == "PUT" else "deletes"].append(f"{kind}/{name}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))

    context.add_init_script("localStorage.setItem('harvester_ops_language', localStorage.getItem('t-lang') || 'en');"
                            "localStorage.setItem('harvester_ops_sidebar_pinned','1');")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/api/hosts", hosts_route)
    page.route("**/api/me/favorites/**", fav_route)
    goto(page, flask_server["base_url"], f"#harvester/{REAL}")
    page.wait_for_selector("#hnav-families .hnav-host")
    yield page, state, errors
    assert errors == []


def fam(page, kind):
    return page.locator(f'#hnav-families .hnav-fam[data-family="{kind}"]')


def names(page, kind):
    return fam(page, kind).locator(".hnav-host-name").all_inner_texts()


def test_a_hundred_hosts_listed_in_a_bounded_menu(menu):
    page, _, _ = menu
    assert len(names(page, "harvester")) == 60 and len(names(page, "kvm")) == 40
    box = page.locator("#hnav-families").bounding_box()
    assert box["height"] <= 0.6 * 900
    assert page.locator("#hnav-families").evaluate("e => e.scrollHeight > e.clientHeight")
    # aucun plafond, aucun lien « Voir les N »
    assert page.locator("#host-nav").inner_text().find("See the") < 0


@pytest.mark.parametrize("lang,text", [("en", "58 ok, 2 in alert"), ("fr", "58 ok, 2 en alerte"),
                                       ("de", "58 ok, 2 mit Alarm")])
def test_family_head_summary_is_translated(context, flask_server, lang, text):
    context.add_init_script(f"localStorage.setItem('t-lang','{lang}');")
    page = context.new_page()
    page.route("**/api/hosts", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps(
        {"families": {}, "hosts": fleet()})))
    context.add_init_script("localStorage.setItem('harvester_ops_language', localStorage.getItem('t-lang') || 'en');")
    goto(page, flask_server["base_url"], f"#harvester/{REAL}")
    expect(fam(page, "harvester").locator(".hnav-fam-sum")).to_have_text(text)
    expect(fam(page, "harvester").locator(".hnav-fam-total")).to_have_text("(60)")
    expect(fam(page, "harvester").locator(".hnav-fam-sum")).to_have_class(re.compile("has-alert"))


def test_the_open_host_unfolds_its_sub_menu_and_the_rest_stay_folded(menu):
    page, _, _ = menu
    rows = page.locator("#hnav-families .hnav-host.open")
    expect(rows).to_have_count(1)
    expect(rows.locator(".hnav-host-name")).to_have_text(REAL)
    subs = page.locator("#hnav-families .hnav-sub a").evaluate_all("els => els.map(e => e.dataset.sub)")
    assert subs == ["overview", "vms", "containers", "imports", "params", "dr"]
    hrefs = page.locator("#hnav-families .hnav-sub a").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    assert hrefs == [f"#harvester/{REAL}", f"#harvester/{REAL}/vms", f"#harvester/{REAL}/containers",
                     f"#harvester/{REAL}/imports/vmimport", f"#harvester/{REAL}/params/nodes", f"#harvester/{REAL}/dr"]
    expect(page.locator('#hnav-families .hnav-sub a[data-sub="overview"]')).to_have_class(re.compile("active"))
    # un clic sur « Machines virtuelles » de l'hôte : l'écran et l'entrée suivent
    page.locator('#hnav-families .hnav-sub a[data-sub="vms"]').click()
    settle(page)
    expect(page.locator("#tab-namespaces")).to_have_class(re.compile("active"))
    expect(page.locator('#hnav-families .hnav-sub a[data-sub="vms"]')).to_have_class(re.compile("active"))


@pytest.mark.parametrize("query,expect_harv,expect_kvm", [
    ("site-07", ["harv-site-07"], []),
    ("site=lyon", [f"harv-site-{i:02d}" for i in range(1, 60) if i % 4 == 2], []),
    ("kvm-node-3", [], [f"kvm-node-{i}" for i in range(30, 40)]),
    ("paris", [], [f"kvm-node-{i:02d}" for i in range(1, 41)]),
])
def test_search_by_name_label_and_family(menu, query, expect_harv, expect_kvm):
    page, _, _ = menu
    page.fill("#hnav-q", query)
    assert sorted(names(page, "harvester")) == sorted(expect_harv)
    assert sorted(names(page, "kvm")) == sorted(expect_kvm)
    if not expect_harv:
        expect(fam(page, "harvester").locator(".hnav-empty")).to_be_visible()


def test_slash_focuses_the_search_and_enter_opens_the_first_host(menu):
    page, _, _ = menu
    page.locator("#tab-overview h1").click()
    page.keyboard.press("/")
    expect(page.locator("#hnav-q")).to_be_focused()
    page.keyboard.type(REAL)
    page.keyboard.press("Enter")
    settle(page)
    assert page.evaluate("location.hash") == f"#harvester/{REAL}"
    # « / » tapé dans un champ reste un caractère
    page.fill("#hnav-q", "")
    page.locator("#hnav-q").press("/")
    assert page.input_value("#hnav-q") == "/"


def test_star_is_saved_by_the_console_and_found_again(menu):
    page, state, _ = menu
    star = page.locator('[data-star="harvester/harv-site-05"]')
    expect(star).to_have_attribute("aria-pressed", "false")
    assert star.get_attribute("title")
    star.click()
    expect(page.locator('[data-star="harvester/harv-site-05"]')).to_have_attribute("aria-pressed", "true")
    assert state["puts"] == ["harvester/harv-site-05"]
    page.reload()
    settle(page)
    page.wait_for_selector("#hnav-families .hnav-host")
    expect(page.locator('[data-star="harvester/harv-site-05"]')).to_have_attribute("aria-pressed", "true")
    # le favori monte en tête de sa famille
    assert names(page, "harvester")[0] == "harv-site-05"
    page.locator('[data-star="harvester/harv-site-05"]').click()
    expect(page.locator('[data-star="harvester/harv-site-05"]')).to_have_attribute("aria-pressed", "false")
    assert state["deletes"] == ["harvester/harv-site-05"]


def test_a_refused_star_is_put_back_and_says_why(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_sidebar_pinned','1');")
    page = context.new_page()
    page.route("**/api/hosts", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                      body=json.dumps({"families": {}, "hosts": fleet()[:3]})))
    page.route("**/api/me/favorites/**", lambda r, q: r.fulfill(status=404, content_type="application/json",
                                                                body=json.dumps({"error": "host x is not declared"})))
    goto(page, flask_server["base_url"], f"#harvester/{REAL}")
    page.locator('[data-star="harvester/harv-site-01"]').click()
    star = page.locator('[data-star="harvester/harv-site-01"]')
    expect(star).to_have_attribute("aria-pressed", "false")
    expect(star).to_have_attribute("title", re.compile("host x is not declared"))


def test_a_folded_family_keeps_its_alerts_and_the_open_host(menu):
    page, _, _ = menu
    fam(page, "harvester").locator(".hnav-fam-head").click()
    expect(fam(page, "harvester").locator(".hnav-fam-head")).to_have_attribute("aria-expanded", "false")
    assert sorted(names(page, "harvester")) == sorted(["harv-site-17", "harv-site-41", REAL])
    fam(page, "kvm").locator(".hnav-fam-head").click()
    assert names(page, "kvm") == []
    # repli gardé par le navigateur
    page.reload()
    settle(page)
    page.wait_for_selector("#hnav-families .hnav-host")
    assert sorted(names(page, "harvester")) == sorted(["harv-site-17", "harv-site-41", REAL])
    page.evaluate("localStorage.removeItem('harvester_ops_hnav_collapsed_harvester');"
                  "localStorage.removeItem('harvester_ops_hnav_collapsed_kvm')")


def test_keyboard_reaches_a_host_and_opens_it(menu):
    page, _, _ = menu
    page.locator("#hnav-q").focus()
    target = None
    for _ in range(12):
        page.keyboard.press("Tab")
        target = page.evaluate("document.activeElement.textContent.trim()")
        if page.evaluate("document.activeElement.dataset.hnav === 'root'") and target.startswith("Virtual"):
            break
    assert target.startswith("Virtual")
    page.keyboard.press("Enter")
    settle(page)
    assert page.evaluate("location.hash") == "#vms"
    expect(page.locator('#host-nav [data-hnav-root="vms"]')).to_have_class(re.compile("active"))


def test_every_control_of_the_menu_has_a_tooltip(menu):
    page, _, _ = menu
    missing = page.evaluate("""() => [...document.querySelectorAll(
        '#host-nav a, #host-nav button, #host-nav input, #host-add-modal button')]
      .filter(e => !(e.getAttribute('title') || e.dataset.tip)).map(e => e.outerHTML.slice(0, 80))""")
    assert missing == []


def test_add_a_host_offers_the_two_families(menu):
    page, _, _ = menu
    page.click("#hnav-add")
    modal = page.locator("#host-add-modal")
    expect(modal).to_have_class(re.compile("active"))
    expect(modal).to_have_attribute("aria-hidden", "false")
    assert modal.locator("[data-add-kind]").evaluate_all("els => els.map(e => e.dataset.addKind)") == ["harvester", "kvm"]
    page.keyboard.press("Escape")
    expect(modal).not_to_have_class(re.compile("active"))
    page.click("#hnav-add")
    modal.locator('[data-add-kind="kvm"]').click()
    expect(page.locator(".floating-panel .kvm-declare")).to_be_visible()


def test_search_offers_a_vm_already_read_and_goes_to_it(menu):
    page, _, _ = menu
    page.route("**/api/vms-all", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps({
        "clusters": [{"cluster": REAL, "state": "ok", "count": 1}],
        "vms": [{"cluster": REAL, "namespace": "prod", "name": "erp-db-01", "phase": "Running"}]})))
    page.goto(flask_server_url(page) + "#vms")
    settle(page)
    page.wait_for_function("() => window.AllVMs && AllVMs.cached() && AllVMs.cached().vms.length === 1",
                           timeout=10000)
    page.fill("#hnav-q", "erp-db")
    link = page.locator("#hnav-results .hnav-vm")
    expect(link).to_have_count(1)
    expect(link).to_have_attribute("href", f"#harvester/{REAL}/vms")
    assert "erp-db-01" in link.get_attribute("title")
    # le cluster de test est injoignable : sa liste de VMs est simulée
    page.route(f"**/api/vms/{REAL}", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps({
        "cluster": REAL, "vms": [{"namespace": "default", "name": "web", "phase": "Running", "runStrategy": "Always"},
                                 {"namespace": "prod", "name": "erp-db-01", "phase": "Running", "runStrategy": "Always"}]})))
    link.click()
    settle(page)
    expect(page.locator("#tab-namespaces")).to_have_class(re.compile("active"))
    page.wait_for_function("() => document.querySelector('#ns-vm-filter').value === 'erp-db-01'", timeout=10000)
    expect(page.locator("#ns-dropdown")).to_have_value("prod")


def flask_server_url(page):
    return page.url.split("#")[0]


def test_the_old_menu_still_works_alongside(menu):
    """Jusqu'au retrait de l'ancien menu, ses entrées restent utilisables et
    ne sont pas confondues avec celles du menu des hôtes."""
    page, _, _ = menu
    page.locator('.tab[data-tab="storage"]').click()
    settle(page)
    expect(page.locator("#tab-storage")).to_have_class(re.compile("active"))
    assert page.locator("#host-nav .tab.active").count() == 1          # l'hôte ouvert, rien d'autre
    expect(page.locator('#hnav-families .hnav-sub a[data-sub="params"]')).to_have_class(re.compile("active"))
