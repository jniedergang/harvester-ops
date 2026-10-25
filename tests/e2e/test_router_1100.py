"""Navigation 2.0, étape 1 : le routeur branché sur l'interface actuelle.

Chargement à froid de chaque adresse servie par l'étape 1 et de chaque
ancien jeton : l'écran attendu est actif, le cluster affiché est celui de
l'adresse, et aucune requête ne part vers l'autre cluster. Puis F5 et
Précédent, les adresses qui ne s'ouvrent pas (message, écran par défaut),
le lien d'un autre cluster collé dans l'onglet, et la mutation qui prouve
que l'ordre « cluster puis onglet » est vérifié.
"""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto, settle  # noqa: E402
from test_cluster_switch import two_clusters  # noqa: E402,F401  (fixture)

A, B = "harv-fake", "harv-second"
KVM = "kvm-a"


def open_cold(context, flask_server, hash, saved=A, tab="activity", extra_js="", routes=None):
    """Nouvel onglet : dernier cluster `saved`, dernier onglet `tab`, puis
    l'adresse `hash` à froid. Rend (page, urls des requêtes)."""
    context.add_init_script(
        "if (!sessionStorage.getItem('rt-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        f"localStorage.setItem('harvester_ops_current_cluster','{saved}');"
        f"localStorage.setItem('harvester_ops_current_tab','{tab}');"
        "localStorage.removeItem('harvester_ops_automation_subtab');"
        "sessionStorage.setItem('rt-init','1'); }" + extra_js)
    page = context.new_page()
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    page.route("**/api/kvm/hosts?declared=1", lambda r, q: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"hosts": [{"name": KVM}], "warnings": [], "role": "admin", "gestures": {}})))
    for pattern, handler in (routes or {}).items():
        page.route(pattern, handler)
    goto(page, flask_server["base_url"], hash)
    return page, calls


def active_tab(page):
    return page.locator(".tab-content.active").get_attribute("id")


def cluster_shown(page):
    return page.locator("#tab-overview .cluster-name").inner_text().strip()


def other_cluster_calls(calls, other):
    return [c.split("/api/")[-1] for c in calls if "/api/" in c and f"/{other}" in c]


# (adresse, onglet attendu, pane ou sous-onglet attendu, cluster attendu)
HOST_ROWS = [
    (f"#harvester/{B}", "overview", ("overview", "metrics"), B),
    (f"#harvester/{B}/overview/events", "overview", ("overview", "events"), B),
    (f"#harvester/{B}/params/nodes", "overview", ("overview", "cluster"), B),
    (f"#harvester/{B}/vms", "namespaces", None, B),
    (f"#harvester/{B}/containers", "containers", None, B),
    (f"#harvester/{B}/dr", "clusterdr", None, B),
    (f"#harvester/{B}/imports/vmimport", "vmimport", None, B),
    (f"#harvester/{B}/imports/vmimport/ova", "vmimport", ("pane", "ova"), B),
    (f"#harvester/{B}/imports/forklift/waves", "forklift", ("pane", "waves"), B),
    (f"#harvester/{B}/params/storage/images", "storage", ("pane", "images"), B),
    (f"#harvester/{B}/params/network/overlay", "network", ("pane", "overlay"), B),
    (f"#harvester/{B}/params/addons", "addons", None, B),
    (f"#harvester/{B}/params/security/sshkeys", "security", ("pane", "sshkeys"), B),
    (f"#harvester/{B}/params/monlog/alerts", "monlog", ("pane", "alerts"), B),
    (f"#harvester/{B}/params/advanced/pci", "advanced", ("pane", "pci"), B),
    (f"#harvester/{B}/params/power", "startup", None, B),
    (f"#harvester/{B}/params/power/startup", "startup", None, B),
    (f"#harvester/{B}/params/power/shutdown", "shutdown", None, B),
    # 2.0.0 : Mise à jour et Connexion ont leur écran dans la page d'hôte
    (f"#harvester/{B}/params/upgrade", "hupgrade", None, B),
    (f"#harvester/{B}/params/conn", "hconn", None, B),
    (f"#vms/host/harvester/{B}", "namespaces", None, B),
    (f"#activity/host/harvester/{B}", "activity", None, A),
]

FLEET_ROWS = [
    ("#vms", "allvms", None), ("#vms/folder/unfiled", "allvms", None), ("#vms/folder/12", "allvms", None),
    ("#vms/host/harvester", "allvms", None), ("#vms/host/kvm", "kvmvms", None),
    (f"#vms/host/kvm/{KVM}", "kvmvms", None),
    ("#folders", "allvms", None), ("#folders/12", "allvms", None),
    ("#containers", "allcontainers", None), ("#containers/folder/12", "allcontainers", None),
    ("#hosts", "kvmhosts", None), ("#hosts/kvm", "kvmhosts", None), ("#hosts/folder/12", "kvmhosts", None),
    ("#hosts/harvester", "overview", None),
    ("#dr", "drplans", None), ("#dr/links", "drlinks", None), ("#dr/rp-0123456789ab", "drplans", None),
    ("#migrations", "forkliftglobal", None), ("#gpus", "gpus", None), ("#bookings", "bookings", None),
    ("#usage", "usage", None), ("#activity", "activity", None),
    ("#automation/capi", "automation", ("auto", "capi")),
    ("#automation/terraform", "automation", ("auto", "terraform")),
    ("#automation/pxe", "automation", ("auto", "pxe")),
    (f"#kvm/{KVM}", "kvmhosts", None), (f"#kvm/{KVM}/vms", "kvmvms", None),
    (f"#kvm/{KVM}/params/system", "kvmhosts", None), (f"#kvm/{KVM}/params/pools", "kvmstorage", None),
    (f"#kvm/{KVM}/params/networks", "kvmnetworks", None), (f"#kvm/{KVM}/params/labels", "kvmhosts", None),
    (f"#kvm/{KVM}/params/conn", "kvmhosts", None), (f"#kvm/{KVM}/dr", "drplans", None),
    (f"#activity/host/kvm/{KVM}", "activity", None),
]

# ancien jeton -> (onglet, adresse réécrite) ; le dernier cluster est A
LEGACY_ROWS = [
    ("#overview", "overview", f"#harvester/{A}"),
    ("#cluster", "overview", f"#harvester/{A}"),
    ("#storage", "storage", f"#harvester/{A}/params/storage/volumes"),
    ("#network", "network", f"#harvester/{A}/params/network/vmnets"),
    ("#addons", "addons", f"#harvester/{A}/params/addons/list"),
    ("#security", "security", f"#harvester/{A}/params/security/secrets"),
    ("#monlog", "monlog", f"#harvester/{A}/params/monlog/metrics"),
    ("#advanced", "advanced", f"#harvester/{A}/params/advanced/settings"),
    ("#startup", "startup", f"#harvester/{A}/params/power/startup"),
    ("#shutdown", "shutdown", f"#harvester/{A}/params/power/shutdown"),
    ("#namespaces", "namespaces", f"#harvester/{A}/vms"),
    ("#allvms", "allvms", "#vms"),
    ("#containers", "allcontainers", "#containers"),   # décision 3 : racine de flotte
    ("#allcontainers", "allcontainers", "#containers"),
    ("#kvmhosts", "kvmhosts", "#hosts/kvm"),
    ("#kvm-hosts", "kvmhosts", "#hosts/kvm"),
    ("#kvmvms", "kvmvms", "#vms/host/kvm"),
    ("#kvmstorage", "kvmstorage", "#kvmstorage"),
    ("#kvmnetworks", "kvmnetworks", "#kvmnetworks"),
    ("#vmimport", "vmimport", f"#harvester/{A}/imports/vmimport/imports"),
    ("#forklift", "forklift", f"#harvester/{A}/imports/forklift/prep"),
    ("#vmio", "vmimport", f"#harvester/{A}/imports/vmimport/imports"),
    ("#forkliftglobal", "forkliftglobal", "#migrations"),
    ("#automation", "automation", "#automation/capi"),
    ("#clusterdr", "clusterdr", f"#harvester/{A}/dr"),
    ("#drplans", "drplans", "#dr"),
    ("#drlinks", "drlinks", "#dr/links"),
    ("#gpus", "gpus", "#gpus"),
    ("#bookings", "bookings", "#bookings"),
    ("#usage", "usage", "#usage"),
    ("#activity", "activity", "#activity"),
]


def check_detail(page, tab, detail):
    if not detail:
        return
    kind, value = detail
    if kind == "overview":
        # 2.0.0 : plus de sous-onglets d'aperçu ; le mode est porté par l'App
        # (métriques et événements, ou vue en blocs des nœuds)
        assert page.evaluate("App._nav.screen().overview") == value
        board = page.locator('#tab-overview .overview-subtab[data-subtab="cluster"]')
        if value == "cluster":
            expect(board).to_be_visible()
        else:
            expect(board).to_be_hidden()
            expect(page.locator('#tab-overview .overview-subtab[data-subtab="events"]')).to_be_visible()
    elif kind == "pane":
        expect(page.locator(f'#tab-{tab} [data-section-tab="{value}"]')).to_have_class(re.compile(r"\bactive\b"))
        expect(page.locator(f'#tab-{tab} .section-pane[data-pane="{value}"]')).to_be_visible()
    elif kind == "auto":
        expect(page.locator(f'#tab-group-automation .tab-child[data-subtab="{value}"]')).to_have_class(
            re.compile(r"\bsub-active\b"))


@pytest.mark.parametrize("hash,tab,detail,cluster", HOST_ROWS, ids=[r[0] for r in HOST_ROWS])
def test_cold_load_of_a_host_address(context, flask_server, two_clusters, hash, tab, detail, cluster):
    page, calls = open_cold(context, flask_server, hash)
    assert active_tab(page) == f"tab-{tab}"
    check_detail(page, tab, detail)
    assert cluster_shown(page) == cluster
    page.wait_for_timeout(800)            # requêtes de fond du premier écran
    other = A if cluster == B else B
    assert other_cluster_calls(calls, other) == [], f"requêtes vers {other}"


@pytest.mark.parametrize("hash,tab,detail", FLEET_ROWS, ids=[r[0] for r in FLEET_ROWS])
def test_cold_load_of_a_fleet_address(context, flask_server, two_clusters, hash, tab, detail):
    page, calls = open_cold(context, flask_server, hash)
    assert active_tab(page) == f"tab-{tab}"
    check_detail(page, tab, detail)
    assert cluster_shown(page) == A          # écran de flotte : le dernier cluster reste
    page.wait_for_timeout(800)
    assert other_cluster_calls(calls, B) == []
    assert page.evaluate("location.hash") == hash   # l'adresse de la 2.0 reste telle quelle
    assert page.locator("#access-notice").is_hidden()


@pytest.mark.parametrize("hash,tab,rewritten", LEGACY_ROWS, ids=[r[0] for r in LEGACY_ROWS])
def test_cold_load_of_a_legacy_token(context, flask_server, two_clusters, hash, tab, rewritten):
    page, calls = open_cold(context, flask_server, hash, saved=A)
    assert active_tab(page) == f"tab-{tab}"
    assert cluster_shown(page) == A
    assert page.evaluate("location.hash") == rewritten
    page.wait_for_timeout(800)
    assert other_cluster_calls(calls, B) == []


def test_empty_address_still_restores_the_last_tab(context, flask_server, two_clusters):
    """Étape 1 : sans adresse, l'ancien démarrage (46 fichiers e2e en dépendent)."""
    page, calls = open_cold(context, flask_server, "", saved=B, tab="security")
    assert active_tab(page) == "tab-security"
    assert cluster_shown(page) == B
    assert page.evaluate("location.hash") == ""      # rien n'est écrit sans geste


def test_f5_after_a_pane_click_gives_the_same_pane(context, flask_server):
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/network")
    page.locator('#tab-network [data-section-tab="overlay"]').click()
    assert page.evaluate("location.hash") == f"#harvester/{A}/params/network/overlay"
    # la mémoire du pane dit autre chose : l'adresse doit l'emporter
    page.evaluate("localStorage.setItem('harvester_ops_section_network','vmnets');"
                  "localStorage.setItem('harvester_ops_current_tab','activity')")
    page.reload(wait_until="domcontentloaded")
    settle(page)
    assert active_tab(page) == "tab-network"
    check_detail(page, "network", ("pane", "overlay"))


def test_a_menu_click_writes_the_address_without_history(context, flask_server):
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/storage")
    n = page.evaluate("history.length")
    page.click('.tab[data-tab="activity"]')
    assert page.evaluate("location.hash") == "#activity"
    assert page.evaluate("history.length") == n
    page.click('[data-group="automation"]')
    page.click('.tab-child[data-subtab="pxe"]')
    expect(page.locator("#tab-automation")).to_have_class(re.compile(r"\bactive\b"))
    assert page.evaluate("location.hash") == "#automation/pxe"


def test_back_returns_to_the_previous_screen(context, flask_server, two_clusters):
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/storage/images")
    page.evaluate(f"() => Router.go('#harvester/{B}/params/network/overlay')")
    settle(page)
    assert active_tab(page) == "tab-network" and cluster_shown(page) == B
    page.go_back()
    page.wait_for_function(f"() => location.hash === '#harvester/{A}/params/storage/images'")
    settle(page)
    page.wait_for_function("() => document.querySelector('#tab-storage').classList.contains('active')")
    assert cluster_shown(page) == A
    check_detail(page, "storage", ("pane", "images"))


NOT_OPENED = [
    ("#harvester/nope/params/storage", "nope", "opens no screen"),
    (f"#kvm/zzz/params/pools", "zzz", "opens no screen"),
    ("#harvester/-a", "-a", "opens no screen"),
    ("#xen/a", "#xen/a", "opens no screen"),
    ("#harvester/a/params/storage/zzz", "zzz", "opens no screen"),
]


@pytest.mark.parametrize("hash,name,text", NOT_OPENED, ids=[h for h, _, _ in NOT_OPENED])
def test_an_address_that_cannot_open_says_so(context, flask_server, hash, name, text):
    page, calls = open_cold(context, flask_server, hash, tab="activity")
    assert active_tab(page) == "tab-activity"                 # écran par défaut (étape 1)
    notice = page.locator("#access-notice")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text(text)
    expect(notice).to_contain_text(hash)
    assert page.evaluate("location.hash") == ""
    assert cluster_shown(page) == A


XSS_ADDRESS = "#harvester/<img src=x onerror=window.__xss=1>"


@pytest.mark.parametrize("how", ["cold", "go"])
def test_a_crafted_address_is_shown_escaped(context, flask_server, how):
    """Une adresse forgée n'atteint le bandeau qu'échappée : texte visible,
    aucun élément créé, aucun script exécuté (à froid, et par Router.go qui
    passe la chaîne brute, sans l'encodage de l'URL)."""
    if how == "cold":
        page, _ = open_cold(context, flask_server, XSS_ADDRESS)
    else:
        page, _ = open_cold(context, flask_server, "")
        page.evaluate(f"Router.go({json.dumps(XSS_ADDRESS)})")
        settle(page)
    notice = page.locator("#access-notice")
    expect(notice).to_be_visible()
    if how == "go":
        expect(notice).to_contain_text("<img src=x onerror=window.__xss=1>")
    assert notice.locator("img").count() == 0
    page.wait_for_timeout(300)
    assert page.evaluate("window.__xss") is None


def test_a_legacy_token_without_any_host_says_so(context, flask_server):
    """Ancien jeton KVM alors qu'aucun hôte KVM n'est visible : l'onglet
    d'avant s'ouvre (étape 1) et le bandeau dit pourquoi."""
    empty = {"hosts": [], "warnings": [], "role": "admin", "gestures": {}}
    page, _ = open_cold(context, flask_server, "#kvmstorage", routes={
        "**/api/kvm/hosts?declared=1": lambda r, q: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(empty))})
    assert active_tab(page) == "tab-kvmstorage"
    notice = page.locator("#access-notice")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("#kvmstorage")
    expect(notice).to_contain_text("none is visible")


def test_the_cluster_selector_adds_no_history_entry(context, flask_server, two_clusters):
    """Le sélecteur est un geste comme un clic : l'adresse suit sans entrée
    d'historique (comme avant le routeur)."""
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/storage")
    before = page.evaluate("history.length")
    page.select_option("#cluster-select", B)
    page.wait_for_function(f"() => App.getCurrentCluster() === '{B}'")
    settle(page)
    assert page.evaluate("location.hash") == f"#harvester/{B}/params/storage/volumes"
    assert page.evaluate("history.length") == before


def test_a_failing_navigation_is_logged_and_the_next_one_still_runs(context, flask_server):
    """Une navigation qui lève ne laisse pas de promesse rejetée sans
    gestionnaire, et la file continue."""
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/storage")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.evaluate("""() => { const orig = App._nav.showRoute; window.__origShow = orig;
      App._nav.showRoute = () => { App._nav.showRoute = orig; throw new Error('boom'); }; }""")
    page.evaluate("() => { Router.go('#gpus'); }")   # promesse non attendue, comme un appelant
    page.wait_for_timeout(300)
    page.evaluate("Router.go('#bookings')")
    settle(page)
    assert active_tab(page) == "tab-bookings"
    assert errors == [], errors


def test_a_cluster_hidden_by_rancher_is_explained(context, flask_server):
    def clusters(route, req):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "clusters": [{"name": A, "description": "", "node_count": 1, "origin": "config"}],
            "config_writable": True, "hidden": [{"name": "secret", "reason": "no-access"}]}))
    page, calls = open_cold(context, flask_server, "#harvester/secret/vms", tab="activity",
                            routes={"**/api/clusters": clusters})
    assert active_tab(page) == "tab-activity"
    notice = page.locator("#access-notice")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("secret: Rancher manages it but gives your account no access to it.")
    assert not [c for c in calls if "/secret" in c and "/api/" in c and "clusters" not in c]


def spy_veil(page):
    page.evaluate("""() => {
      window.__veils = [];
      const orig = window.Veil.during;
      window.Veil.during = (t, o, w) => { if (!t) window.__veils.push(o && o.name); return orig(t, o, w); };
    }""")


def test_a_link_of_another_cluster_pasted_in_this_tab(context, flask_server, two_clusters):
    page, calls = open_cold(context, flask_server, f"#harvester/{A}/params/storage")
    spy_veil(page)
    page.evaluate("AccessNotice.report([{verb: 'list', resource: 'pods', group: '', namespace: 'x'}])")
    expect(page.locator("#access-notice")).to_be_visible()
    calls.clear()
    page.evaluate(f"location.hash = '#harvester/{B}/params/storage/images'")
    page.wait_for_function(f"() => App.getCurrentCluster() === '{B}'")
    settle(page)
    assert page.evaluate("window.__veils") == [B]
    expect(page.locator("#access-notice")).to_be_hidden()
    check_detail(page, "storage", ("pane", "images"))
    page.wait_for_timeout(600)
    assert other_cluster_calls(calls, A) == []


def test_switching_cluster_on_the_overview_to_metrics_stops_the_old_board(
        context, flask_server, two_clusters):
    """Aperçu ouvert sur la vue en blocs de A, puis adresse de B en mode
    Métriques (Précédent, lien collé, Router.go) : la vue de A doit être
    arrêtée, sinon elle continue d'interroger A sous un aperçu titré B."""
    page, calls = open_cold(context, flask_server, f"#harvester/{A}/params/nodes")
    page.wait_for_selector('.overview-subtab[data-subtab="cluster"] .fabric-body', timeout=8000)
    assert [c for c in calls if f"/api/topology/{A}" in c]
    calls.clear()
    page.evaluate(f"Router.go('#harvester/{B}')")
    page.wait_for_function(f"() => App.getCurrentCluster() === '{B}'")
    settle(page)
    check_detail(page, "overview", ("overview", "metrics"))
    # la vue en blocs se rafraîchit toutes les 8 s (cluster-map.js REFRESH_MS)
    page.wait_for_timeout(9000)
    assert [c for c in calls if f"/api/topology/{A}" in c] == [], "la vue de A tourne encore"


def test_router_cold_load_detects_order(context, flask_server):
    """Mutation volontaire : un routeur qui pose l'onglet AVANT le cluster
    (défaut de 1.4.31) laisse la toile des nœuds vide ; le test qui suit
    l'ordre réel voit la toile remplie. Le contrôle attrape donc la régression."""
    hash = f"#harvester/{A}/params/nodes"
    good, good_calls = open_cold(context, flask_server, hash)
    good.wait_for_selector('.overview-subtab[data-subtab="cluster"] .fabric-body', timeout=8000)
    assert [c for c in good_calls if "/api/topology/" in c]
    good.close()

    ctx2 = context.browser.new_context(viewport={"width": 1400, "height": 900})
    try:
        bad, bad_calls = open_cold(ctx2, flask_server, hash, extra_js="window.__routerOrder = 'tab-first';")
        bad.wait_for_timeout(2500)
        assert active_tab(bad) == "tab-overview"
        assert not [c for c in bad_calls if "/api/topology/" in c], "l'ordre inversé a quand même monté la toile"
        assert bad.locator('.overview-subtab[data-subtab="cluster"] .fabric-body').count() == 0
    finally:
        ctx2.close()


def test_router_current_follows_the_screen(context, flask_server):
    page, _ = open_cold(context, flask_server, f"#harvester/{A}/params/security/sshkeys")
    cur = page.evaluate("Router.current()")
    assert (cur["name"], cur["kind"], cur["host"], cur["tab"], cur["pane"]) == ("host", "harvester", A, "security", "sshkeys")
    page.click('.tab[data-tab="gpus"]')
    assert page.evaluate("Router.current().name") == "gpus"
