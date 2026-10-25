"""2.0.0 : la page d'un hôte Harvester (host-page.js).

Chaque adresse d'hôte chargée à froid : l'onglet attendu est actif dans le
bandeau, la section existante est visible, et le cluster de l'adresse est le
seul que les appels réseau visent. Puis : changer d'hôte depuis une page de
Paramètres garde l'onglet (avec le voile), le bouton Upgrade de l'aperçu
mène à Paramètres > Mise à jour, « Joindre un nœud » ouvre Bare-metal avec
le cluster prérempli, un lecteur ne voit pas les boutons d'écriture, et la
mutation qui retire un onglet de Paramètres fait échouer sa ligne."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto, settle  # noqa: E402
from test_cluster_switch import two_clusters  # noqa: E402,F401  (fixture)

A, B = "harv-fake", "harv-second"


def open_cold(context, flask_server, hash, extra_js="", saved=A):
    context.add_init_script(
        "if (!sessionStorage.getItem('hp-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        f"localStorage.setItem('harvester_ops_current_cluster','{saved}');"
        "localStorage.setItem('harvester_ops_current_tab','activity');"
        "localStorage.setItem('harvester_ops_sidebar_pinned','1');"
        "sessionStorage.setItem('hp-init','1'); }" + extra_js)
    page = context.new_page()
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors
    goto(page, flask_server["base_url"], hash)
    return page, calls


def other_cluster_calls(calls, other):
    return [c.split("/api/")[-1] for c in calls if "/api/" in c and re.search(rf"/{re.escape(other)}(/|\?|$)", c)]


# (adresse, sous-menu actif, onglet de Paramètres/Imports actif, sélecteur visible attendu)
ROWS = [
    (f"#harvester/{B}", "overview", None, '#tab-overview .overview-subtab[data-subtab="metrics"]'),
    (f"#harvester/{B}/overview/events", "overview", None, '#tab-overview .overview-subtab[data-subtab="events"]'),
    (f"#harvester/{B}/params/nodes", "params", "nodes", '#tab-overview .overview-subtab[data-subtab="cluster"]'),
    (f"#harvester/{B}/params/storage", "params", "storage", "#tab-storage"),
    (f"#harvester/{B}/params/storage/images", "params", "storage", '#tab-storage .section-pane[data-pane="images"]'),
    (f"#harvester/{B}/params/network/overlay", "params", "network", '#tab-network .section-pane[data-pane="overlay"]'),
    (f"#harvester/{B}/params/addons", "params", "addons", "#tab-addons"),
    (f"#harvester/{B}/params/security", "params", "security", '#tab-security .section-pane[data-pane="secrets"]'),
    (f"#harvester/{B}/params/security/accounts", "params", "security", "#tab-security #husers-body"),
    (f"#harvester/{B}/params/monlog/alerts", "params", "monlog", '#tab-monlog .section-pane[data-pane="alerts"]'),
    (f"#harvester/{B}/params/advanced/pci", "params", "advanced", '#tab-advanced .section-pane[data-pane="pci"]'),
    (f"#harvester/{B}/params/upgrade", "params", "upgrade", "#tab-hupgrade .upg-win"),
    (f"#harvester/{B}/params/power/startup", "params", "power", "#tab-startup #btn-startup"),
    (f"#harvester/{B}/params/power/shutdown", "params", "power", "#tab-shutdown #btn-shutdown"),
    (f"#harvester/{B}/params/conn", "params", "conn", "#tab-hconn .hp-conn"),
    (f"#harvester/{B}/vms", "vms", None, "#tab-namespaces #btn-vm-backups"),
    (f"#harvester/{B}/containers", "containers", None, "#tab-containers"),
    (f"#harvester/{B}/imports/vmimport/ova", "imports", "vmimport", '#tab-vmimport .section-pane[data-pane="ova"]'),
    (f"#harvester/{B}/imports/forklift/waves", "imports", "forklift", '#tab-forklift .section-pane[data-pane="waves"]'),
]


def check_row(page, sub, tab, selector):
    head = page.locator("#host-page-head")
    expect(head).to_be_visible()
    expect(head.locator(".hp-name")).to_have_text(B)
    expect(head.locator(f'.hp-sub-link[data-hp-sub="{sub}"]')).to_have_class(re.compile(r"\bactive\b"))
    if tab:
        expect(head.locator(f'.hp-tabs [data-hp-tab="{tab}"]').first).to_have_class(re.compile(r"\bactive\b"))
    else:
        assert head.locator(".hp-tabs").count() == 0
    expect(page.locator(selector).first).to_be_visible()
    assert page.evaluate("App.getCurrentCluster()") == B


@pytest.mark.parametrize("hash,sub,tab,selector", ROWS, ids=[r[0].split(B)[1] or "/" for r in ROWS])
def test_cold_load_of_each_host_screen(context, flask_server, two_clusters, hash, sub, tab, selector):
    page, calls = open_cold(context, flask_server, hash)
    check_row(page, sub, tab, selector)
    page.wait_for_timeout(800)
    assert other_cluster_calls(calls, A) == [], "requêtes vers l'autre cluster"
    assert page.errors == []


def test_the_parameters_bar_lists_every_tab_in_order(context, flask_server):
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/storage")
    tabs = page.locator("#host-page-head .hp-tabs [data-hp-tab]").evaluate_all("els => els.map(e => e.dataset.hpTab)")
    assert tabs == page.evaluate("Routes.HARVESTER_PARAMS")
    hrefs = page.locator("#host-page-head .hp-tabs [data-hp-tab]").evaluate_all(
        "els => els.map(e => e.getAttribute('href'))")
    assert hrefs == [f"#harvester/{A}/params/{t}" for t in tabs]
    for el in page.locator("#host-page-head a").all():
        assert el.get_attribute("title"), el.inner_text()
    # un clic sur un onglet du bandeau ouvre la section
    page.locator('#host-page-head [data-hp-tab="network"]').click()
    settle(page)
    expect(page.locator("#tab-network")).to_have_class(re.compile("active"))
    assert page.evaluate("location.hash").startswith(f"#harvester/{A}/params/network")


def test_the_overview_has_no_sub_tabs_and_shows_events_below_metrics(context, flask_server):
    page, _ = open_cold(context, flask_server, f"#harvester/{A}")
    assert page.locator("[data-overview-tab]").count() == 0
    expect(page.locator('#tab-overview .overview-subtab[data-subtab="metrics"]')).to_be_visible()
    expect(page.locator('#tab-overview .overview-subtab[data-subtab="events"]')).to_be_visible()
    act = page.locator("#host-page-head .hp-activity")
    expect(act).to_have_attribute("href", f"#activity/host/harvester/{A}")


def test_switching_host_from_a_parameters_page_keeps_the_tab(context, flask_server, two_clusters):
    page, calls = open_cold(context, flask_server, f"#harvester/{A}/params/storage/images")
    page.wait_for_selector(f'#hnav-families .hnav-host-name:text-is("{B}")')
    page.evaluate("""() => {
      window.__veils = [];
      const orig = window.Veil.during;
      window.Veil.during = (t, o, w) => { if (!t) window.__veils.push(o && o.name); return orig(t, o, w); };
    }""")
    link = page.locator(f'#hnav-families .hnav-host-name:text-is("{B}")')
    expect(link).to_have_attribute("href", f"#harvester/{B}/params/storage/images")
    calls.clear()
    link.click()
    page.wait_for_function(f"() => App.getCurrentCluster() === '{B}'")
    settle(page)
    assert page.evaluate("window.__veils") == [B]
    assert page.evaluate("location.hash") == f"#harvester/{B}/params/storage/images"
    check_row(page, "params", "storage", '#tab-storage .section-pane[data-pane="images"]')
    page.wait_for_timeout(600)
    assert other_cluster_calls(calls, A) == []


def test_the_upgrade_button_of_the_overview_opens_the_upgrade_tab(context, flask_server):
    page, _ = open_cold(context, flask_server, f"#harvester/{A}")
    page.route(f"**/api/upgrade/{A}", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps(
        {"cluster": A, "current": "v1.8.2", "checker": True, "running": None, "history": 0, "upgrade": None,
         "versions": [], "isos": [], "os_images": [], "prechecks": []})))
    page.click("#btn-upgrade")
    settle(page)
    assert page.evaluate("location.hash") == f"#harvester/{A}/params/upgrade"
    expect(page.locator("#tab-hupgrade")).to_have_class(re.compile("active"))
    expect(page.locator('#tab-hupgrade [data-upg="head"]')).to_contain_text("v1.8.2")
    assert page.locator(".floating-panel .upg-win").count() == 0       # pas de fenêtre en plus
    # quitter l'onglet arrête la relecture du contenu
    page.evaluate(f"Router.go('#harvester/{A}/params/storage')")
    settle(page)
    n = page.evaluate("performance.getEntriesByType('resource').filter(e => e.name.includes('/api/upgrade/')).length")
    page.wait_for_timeout(9000)
    assert page.evaluate("performance.getEntriesByType('resource').filter(e => e.name.includes('/api/upgrade/')).length") == n


def _open_join(context, flask_server, join_url):
    from test_bm_config_177 import HOST, ISOS, NODE, fulfill
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/nodes")
    installs, reads = [], []

    def install(route, req):
        installs.append(req.post_data_json)
        fulfill(route, {"action_id": "bm0000000200", "hostname": "x"}, 202)

    def join_read(route, req):
        reads.append(req.url)
        fulfill(route, {"cluster": A, "join_url": join_url, "reason": None if join_url else "proxied"})
    page.route("**/api/isos", lambda r, q: fulfill(r, ISOS))
    page.route("**/api/bmc/discover", lambda r, q: fulfill(r, {"nodes": [NODE]}))
    page.route("**/api/baremetal/install", install)
    page.route(f"**/api/clusters/{A}/join-url", join_read)
    page.on("dialog", lambda d: d.accept())
    page.click("#btn-join-node")
    page.wait_for_function("() => location.hash === '#automation/pxe'")
    settle(page)
    assert len(reads) == 1                    # lue à l'ouverture, dans le cluster
    page.mouse.move(1100, 450)
    form = page.locator("#bmc-discover-form")
    form.locator('[name="hosts"]').fill(HOST)
    form.locator('[name="password"]').fill("bmc-secret")
    form.locator('button[type="submit"]').click()
    page.locator(f'.bmc-install[data-host="{HOST}"]').click()
    win = page.locator(f'[id="fp-bm-install-{HOST}"]')
    expect(win.locator("#bm-install-form")).to_be_visible()
    return page, win, installs


def test_join_a_node_opens_bare_metal_with_the_address_read_from_the_cluster(context, flask_server):
    from test_bm_config_177 import FIXTURE
    page, win, installs = _open_join(context, flask_server, "https://192.0.2.100:443")
    banner = page.locator("#bm-join-target")
    expect(banner).to_contain_text(A)
    expect(banner).to_contain_text("https://192.0.2.100:443")
    assert win.locator('[name="mode"]').input_value() == "join"
    url = win.locator('[name="server_url"]')
    expect(url).to_be_visible()                                       # montrée, modifiable
    expect(url).to_be_editable()
    assert url.input_value() == "https://192.0.2.100:443"
    assert url.get_attribute("title")                                 # info-bulle
    confirm = win.locator('[name="join_confirm"]')
    expect(confirm).to_be_visible()
    expect(win.locator(".bm-join-confirm")).to_contain_text(A)
    expect(win.locator(".bm-vm-network")).to_be_visible()             # partie propre à la jonction
    win.locator('[data-bm="file"]').set_input_files(str(FIXTURE))
    expect(win.locator(".bm-imported")).to_be_visible()
    assert win.locator('[name="mode"]').input_value() == "join"       # l'import ne repasse pas en création
    submit = win.locator('#bm-install-form button[type="submit"]')
    submit.click()
    page.wait_for_timeout(500)
    assert installs == []                                             # pas sans confirmation
    confirm.check()
    url.fill("https://192.0.2.101:443")                               # adresse changée : à reconfirmer
    assert not confirm.is_checked()
    url.fill("https://192.0.2.100:443")
    confirm.check()
    submit.click()
    expect(win.locator("#bm-install-result")).to_contain_text("bm0000000200")
    body = installs[-1]
    assert (body["mode"], body["server_url"], body["join_cluster"]) == ("join", "https://192.0.2.100:443", A)
    assert "join_confirm" not in body and "vip" not in body
    # Annuler rend l'installation d'un nouveau cluster
    banner.locator('[data-bm="join-cancel"]').click()
    expect(page.locator("#bm-join-target")).to_have_count(0)


def test_join_without_a_readable_address_asks_for_it(context, flask_server):
    page, win, installs = _open_join(context, flask_server, None)
    expect(page.locator("#bm-join-target")).to_contain_text("could not be read from the cluster")
    url = win.locator('[name="server_url"]')
    expect(url).to_be_visible()
    assert url.input_value() == ""                                    # jamais devinée
    assert url.evaluate("e => e.required")
    assert win.locator('[name="join_confirm"]').evaluate("e => e.required")


def test_the_connection_tab_tests_the_kubeconfig(context, flask_server):
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/conn")
    box = page.locator("#hconn-host")
    expect(box.locator(".cluster-origin")).to_have_attribute("data-origin", "config")
    expect(box.locator(".hp-conn-nodes tbody tr")).to_have_count(1)
    expect(box.locator(".hp-conn-nodes")).to_contain_text("fake-cp1")
    box.locator('[data-hp-conn="test-kc"]').click()
    expect(box.locator(".hp-conn-result .summary-bar")).to_be_visible(timeout=15000)
    box.locator('[data-hp-conn="settings"]').click()
    expect(page.locator("#settings-modal")).to_have_class(re.compile("active"))
    expect(page.locator('.settings-tab[data-stab="clusters"]')).to_have_class(re.compile("active"))


def test_cluster_accounts_moved_from_settings_to_security(context, flask_server):
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/security/accounts")
    assert page.locator('.settings-tab[data-stab="husers"]').count() == 0
    assert page.locator("#stab-husers").count() == 0
    expect(page.locator("#tab-security #husers-body")).not_to_contain_text("…", timeout=15000)


def test_the_account_menu_opens_cluster_accounts_of_the_current_host(context, flask_server):
    page, _ = open_cold(context, flask_server, "#activity")
    page.wait_for_function(f"window.App && App.getCurrentCluster() === '{A}'")
    page.click("#btn-user")
    item = page.locator('#user-menu [data-um="accounts"]')
    assert item.get_attribute("data-tip")
    item.click()
    page.wait_for_function(f"() => location.hash === '#harvester/{A}/params/security/accounts'")
    expect(page.locator("#tab-security #husers-body")).to_be_visible()
    expect(page.locator("#settings-modal")).not_to_have_class(re.compile("active"))


@pytest.mark.parametrize("age,expect_text", [(300, "5 min"), (20, "less than a minute")])
def test_the_band_says_the_age_of_a_reused_vm_count(context, flask_server, age, expect_text):
    hosts = {"families": {"harvester": {"ok": 1, "alert": 0, "unknown": 0}, "kvm": {"ok": 0, "alert": 0, "unknown": 0}},
             "hosts": [{"kind": "harvester", "name": A, "state": "ok", "error": None, "vm_count": 4,
                        "vm_count_age": age, "labels": {}, "version": "v1.9.0", "favorite": False,
                        "folder": None, "origin": "config"}]}
    context.route("**/api/hosts", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                         body=json.dumps(hosts)))
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/storage")
    count = page.locator("#host-page-head .hp-count")
    expect(count).to_contain_text("4 VMs")
    assert expect_text in count.get_attribute("title")


def test_a_viewer_sees_no_write_buttons(context, flask_server):
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/nodes", extra_js="""
      const f = window.fetch;
      window.fetch = (u, o) => String(u).startsWith('/api/whoami')
        ? Promise.resolve(new Response(JSON.stringify({user: 'curieux', role: 'viewer', roles_active: true, auth: 'local'}),
                                       {status: 200, headers: {'Content-Type': 'application/json'}}))
        : f(u, o);""")
    page.wait_for_function("document.body.classList.contains('role-viewer')")
    expect(page.locator("#btn-join-node")).to_be_hidden()
    expect(page.locator("#hnav-add")).to_be_hidden()
    page.evaluate(f"Router.go('#harvester/{A}/params/conn')")
    settle(page)
    page.wait_for_selector("#hconn-host .hp-conn")
    expect(page.locator('#hconn-host [data-hp-conn="test-kc"]')).to_be_hidden()
    expect(page.locator('#hconn-host [data-hp-conn="test-ssh"]')).to_be_hidden()
    # la lecture reste : nœuds déclarés et bandeau
    expect(page.locator("#hconn-host .hp-conn-nodes")).to_be_visible()


# écouteur du document posé avant celui de l'App : il passe avant Router.start
MUTATION = """document.addEventListener('DOMContentLoaded', () => {
  const i = Routes.HARVESTER_PARAMS.indexOf('advanced');
  if (i >= 0) Routes.HARVESTER_PARAMS.splice(i, 1);
});"""


def test_host_page_mutation_lost_view(context, flask_server, two_clusters):
    """Retirer `advanced` de Routes.HARVESTER_PARAMS doit faire échouer la
    ligne params/advanced/pci : un onglet de Paramètres perdu se voit."""
    row = next(r for r in ROWS if r[0].endswith("/params/advanced/pci"))
    page, _ = open_cold(context, flask_server, row[0], extra_js=MUTATION)
    with pytest.raises(AssertionError):
        check_row(page, row[1], row[2], row[3])
