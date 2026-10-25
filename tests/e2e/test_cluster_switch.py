"""v1.20.0 — bascule d'un cluster à l'autre.

Le défaut corrigé ici est sournois parce qu'il ne casse rien : en changeant
de cluster, l'écran gardait les données du PRÉCÉDENT. Seul l'aperçu se
rafraîchissait ; en restant sur Cluster API ou sur la liste des VMs, on
lisait donc un cluster en croyant en lire un autre — le meilleur moyen
d'agir sur la mauvaise machine.

Ces tests exercent la bascule dans un vrai navigateur, sur une page servie
par l'application, avec deux clusters déclarés.
"""

import json
import re

import pytest

playwright = pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto, settle  # noqa: E402


def _declare(flask_server, api, name):
    payload = {
        "name": name,
        "description": "second cluster (test de bascule)",
        "kubeconfig": str(flask_server["config"]["kubeconfig"]),
        "nodes": [{"hostname": "second-node1", "ip": "10.0.0.2",
                   "role": "control-plane"}],
    }
    status, _ = api("POST", "/api/clusters", payload, expect_status=None)
    if status not in (201, 409):
        pytest.skip(f"impossible de déclarer le second cluster: {status}")


@pytest.fixture
def two_clusters(flask_server, api):
    """Déclare un second cluster le temps du test.

    On ne touche pas au config.yaml partagé de la session : d'autres tests
    comptent les clusters déclarés.
    """
    _declare(flask_server, api, "harv-second")
    yield "harv-second"
    api("DELETE", "/api/clusters/harv-second", expect_status=None)


@pytest.fixture
def lab_cluster(flask_server, api):
    """Second cluster au nom admis par la déclaration mais pas par RFC 1123
    (majuscule et souligné) : l'adresse de ses écrans doit rester lisible."""
    _declare(flask_server, api, "Lab_1")
    yield "Lab_1"
    api("DELETE", "/api/clusters/Lab_1", expect_status=None)


def _goto(page, base_url, tab, cluster="harv-fake", sub=None):
    """2.0.0 : l'écran s'ouvre par son adresse (plus d'onglet retenu)."""
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        f"localStorage.setItem('harvester_ops_current_cluster','{cluster}');")
    goto(page, base_url, address(tab, cluster, sub=sub))


def _switch_host(page, name):
    """2.0.0 : le sélecteur de cluster est retiré ; un autre hôte se choisit
    dans le menu des hôtes (même écran sur un écran d'hôte)."""
    page.locator(f'#host-nav .hnav-host[data-key="harvester/{name}"] .hnav-host-name').click()


def _open_activity(page):
    page.locator('#host-nav [data-hnav-root="activity"]').click(timeout=5000)


def test_switching_clusters_shows_a_blurred_loading_veil(
        context, flask_server, two_clusters):
    page = context.new_page()
    _goto(page, flask_server["base_url"], "overview")
    expect(page.locator(f'#host-nav .hnav-host[data-key="harvester/{two_clusters}"]')).to_have_count(1)

    _switch_host(page, two_clusters)
    page.wait_for_timeout(80)
    veil = page.locator('.veil-fullscreen')
    assert veil.is_visible(), "aucun voile pendant la bascule"
    assert two_clusters in veil.inner_text(), "le voile ne nomme pas le cluster"
    assert "veil-blocking" in (page.locator('body').get_attribute('class') or "")

    page.wait_for_selector('.veil-fullscreen', state='hidden', timeout=30000)
    assert "veil-blocking" not in (page.locator('body').get_attribute('class') or "")


def test_the_veil_leaves_the_page_usable_afterwards(
        context, flask_server, two_clusters):
    """`display: flex` l'emporte sur l'attribut `hidden` : sans règle
    dédiée, le voile restait dans le flux, invisible mais avalant tous les
    clics — une interface morte après la première bascule."""
    page = context.new_page()
    _goto(page, flask_server["base_url"], "overview")
    expect(page.locator(f'#host-nav .hnav-host[data-key="harvester/{two_clusters}"]')).to_have_count(1)

    _switch_host(page, two_clusters)
    page.wait_for_selector('.veil-fullscreen', state='hidden', timeout=30000)
    _open_activity(page)
    page.wait_for_timeout(400)
    assert page.locator('.tab-content.active').get_attribute('id') == "tab-activity"


@pytest.mark.parametrize("tab,expected", [
    ("namespaces", "/api/vms/harv-second"),
    ("automation", "/api/capi/harv-second/"),
])
def test_the_visible_tab_is_reloaded_for_the_new_cluster(
        context, flask_server, two_clusters, tab, expected):
    """Le cœur du correctif : les onglets masqués se reconstruisent à leur
    activation, celui qui est à l'écran ne se reconstruisait jamais."""
    page = context.new_page()
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    _goto(page, flask_server["base_url"], tab)
    calls.clear()

    # 2.0.0 : VMs d'un hôte = un autre hôte du menu (même écran) ; Cluster
    # API (écran de flotte) = son cluster de gestion, choisi dans son en-tête
    if tab == "automation":
        page.locator("#mgmt-cluster-pick").select_option(two_clusters)
    else:
        _switch_host(page, two_clusters)
    page.wait_for_selector('.veil-fullscreen', state='hidden', timeout=30000)
    page.wait_for_timeout(800)

    assert any(expected in c for c in calls), (
        f"aucun appel {expected} après la bascule ; vus : "
        f"{[c.split('/api/')[-1] for c in calls if '/api/' in c][:8]}")
    assert not any("/harv-fake" in c for c in calls), (
        "des données de l'ancien cluster sont encore demandées")


# ---------------------------------------------------------------------------
# v1.21.0 — la topologie affichait le cluster précédent, et le voile de zone
# ---------------------------------------------------------------------------

def test_coming_back_to_the_overview_shows_the_current_cluster(
        context, flask_server, two_clusters):
    """Le symptôme signalé : cluster changé depuis Cluster API, retour sur
    Cluster/Aperçu, et la topologie affichait encore l'ancien cluster — le
    titre disait pourtant le nouveau."""
    page = context.new_page()
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    goto(page, flask_server["base_url"], address("overview", sub="cluster"))
    expect(page.locator(f'#host-nav .hnav-host[data-key="harvester/{two_clusters}"]')).to_have_count(1)

    # partir sur un autre onglet, basculer d'hôte, puis revenir
    _open_activity(page)
    page.wait_for_timeout(400)
    _switch_host(page, two_clusters)
    page.wait_for_selector('.veil-fullscreen', state='hidden', timeout=30000)
    calls.clear()
    # 2.0.0 : la vue en blocs est Paramètres > Nœuds, ouverte depuis l'hôte du menu
    page.click(f'#hnav-families .hnav-host-name:text-is("{two_clusters}")')
    page.click('#host-page-head .hp-sub-link[data-hp-sub="params"]')
    page.wait_for_timeout(2000)

    topo = [c for c in calls if "/api/topology/" in c]
    assert topo, "la topologie n'est pas rechargée au retour sur l'aperçu"
    assert all(two_clusters in c for c in topo), (
        f"topologie demandée pour le mauvais cluster : {topo}")


def test_leaving_the_overview_stops_polling_the_cluster(
        context, flask_server):
    """Le polling continuait en arrière-plan sur l'onglet quitté — et,
    après une bascule, sur l'ANCIEN cluster."""
    page = context.new_page()
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    goto(page, flask_server["base_url"], address("overview", sub="cluster"))
    page.wait_for_timeout(500)
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    _open_activity(page)
    calls.clear()
    # le rafraîchissement de la topologie a une période de 8 s : au-delà,
    # une requête signifierait que le minuteur tourne toujours.
    page.wait_for_timeout(11000)
    assert not [c for c in calls if "/api/topology/" in c], (
        "la topologie interroge encore le cluster alors qu'on a quitté l'aperçu")


def test_a_slow_view_veils_only_its_own_zone(context, flask_server):
    """La demande : que les affichages lents utilisent la même transition,
    mais seulement sur la zone concernée."""
    page = context.new_page()
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_overview_subtab','metrics');"
        # Ralentir la topologie DANS le navigateur : bloquer le pilote
        # Playwright fausserait la mesure du temps.
        "const _f = window.fetch;"
        "window.fetch = (...a) => String(a[0]).includes('/api/topology/')"
        "  ? new Promise(r => setTimeout(() => r(_f(...a)), 1800))"
        "  : _f(...a);")
    goto(page, flask_server["base_url"], address("overview"))
    # 2.0.0 : la vue en blocs des nœuds est Paramètres > Nœuds de l'hôte
    page.click('#host-page-head .hp-sub-link[data-hp-sub="params"]')
    page.wait_for_timeout(900)          # au-delà du seuil anti-clignotement

    veil = page.locator('.veil-scoped')
    assert veil.count() == 1 and veil.first.is_visible(), "pas de voile sur la zone lente"
    assert page.locator('.veil-fullscreen').count() == 0, (
        "une vue lente ne doit pas verrouiller toute la page")
    host = page.locator('.overview-subtab[data-subtab="cluster"] .topology-host')
    hb, vb = host.bounding_box(), veil.first.bounding_box()
    assert abs(hb["x"] - vb["x"]) < 3 and abs(hb["width"] - vb["width"]) < 3, (
        "le voile déborde de sa zone")
    # le reste de la page reste utilisable
    _open_activity(page)
    page.wait_for_timeout(400)
    assert page.locator('.tab-content.active').get_attribute('id') == "tab-activity"


def test_a_fast_view_shows_no_veil_at_all(context, flask_server):
    """Sans seuil, une réponse rapide produirait un clignotement plus
    gênant que l'attente qu'il annonce."""
    page = context.new_page()
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_overview_subtab','metrics');")
    goto(page, flask_server["base_url"], address("overview"))
    seen = []
    page.expose_function("_veilSeen", lambda: seen.append(1))
    page.evaluate("""() => {
      const orig = window.Veil.show;
      window.Veil.show = (t, o) => { window._veilSeen(); return orig(t, o); };
    }""")
    # 2.0.0 : la vue en blocs des nœuds est Paramètres > Nœuds de l'hôte
    page.click('#host-page-head .hp-sub-link[data-hp-sub="params"]')
    page.wait_for_timeout(2000)
    assert not seen, "un voile est apparu alors que la vue a chargé vite"


# ---------------------------------------------------------------------------
# v1.23.0 — filtres de l'onglet Activité
# ---------------------------------------------------------------------------

def test_activity_filters_drive_the_server_not_the_rendered_page(
        context, flask_server):
    """Filtrer la table déjà affichée répondrait « rien » sur un cluster
    dont l'activité est plus ancienne que la fenêtre. La requête doit
    repartir au serveur avec le filtre."""
    page = context.new_page()
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "if (!sessionStorage.getItem('act-init')) {"
        "localStorage.removeItem('harvester_ops_activity_filters');"
        "sessionStorage.setItem('act-init','1'); }")
    goto(page, flask_server["base_url"], address("activity"))
    page.wait_for_timeout(800)

    assert page.locator('#act-f-cluster option').count() >= 1
    assert page.locator('#btn-act-filter-reset').is_hidden(), (
        "sans filtre actif, rien à réinitialiser")
    calls.clear()

    # La recherche libre ne dépend d'aucune donnée présente : elle teste
    # le chemin filtre -> serveur quel que soit l'historique du serveur
    # de test, qui démarre vide.
    page.fill('#act-f-q', 'shutdown')
    page.wait_for_timeout(1000)
    assert any("/api/activity?" in c and "q=shutdown" in c for c in calls), (
        f"le filtre n'est pas parti au serveur : {[c for c in calls if 'activity' in c]}")
    assert page.locator('#btn-act-filter-reset').is_visible()

    page.click('#btn-act-filter-reset')
    page.wait_for_timeout(800)
    assert page.locator('#act-f-q').input_value() == ""
    assert page.locator('#btn-act-filter-reset').is_hidden()


def test_activity_filters_survive_a_reload(context, flask_server):
    """Un opérateur qui suit les échecs d'un cluster ne veut pas
    reconstruire son filtre à chaque rechargement."""
    page = context.new_page()
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');")
    goto(page, flask_server["base_url"], address("activity"))
    page.wait_for_timeout(800)
    page.fill('#act-f-q', 'shutdown')
    page.wait_for_timeout(900)

    page.reload(wait_until="domcontentloaded")
    settle(page)
    assert page.evaluate("location.hash").startswith("#activity")      # F5 rend l'Activité
    page.wait_for_timeout(800)
    assert page.locator('#act-f-q').input_value() == 'shutdown'
    assert page.locator('#btn-act-filter-reset').is_visible()
    # nettoyage pour ne pas polluer les tests suivants du même contexte
    page.click('#btn-act-filter-reset')
    page.wait_for_timeout(500)


def test_the_counter_says_how_much_is_hidden(context, flask_server):
    """La table est plafonnée : dire « N sur M » évite de conclure qu'il ne
    s'est rien passé de plus."""
    page = context.new_page()
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "if (!sessionStorage.getItem('act-init')) {"
        "localStorage.removeItem('harvester_ops_activity_filters');"
        "sessionStorage.setItem('act-init','1'); }")
    goto(page, flask_server["base_url"], address("activity"))
    page.wait_for_timeout(800)
    text = page.locator('#act-filter-count').inner_text()
    assert text.strip(), "aucun compteur affiché"
    assert "entries" in text


# ---------------------------------------------------------------------------
# Navigation 2.0, étape 1 : GPU et Migrations demandent l'hôte au routeur au
# lieu de piloter le sélecteur (qui disparaît à l'étape 2)
# ---------------------------------------------------------------------------

def _spy_veil_and_notice(page):
    page.evaluate("""() => {
      window.__veils = [];
      const orig = window.Veil.during;
      window.Veil.during = (t, o, w) => { if (!t) window.__veils.push(o && o.name); return orig(t, o, w); };
      AccessNotice.report([{verb: 'list', resource: 'pods', group: '', namespace: 'x'}]);
    }""")
    expect(page.locator("#access-notice")).to_be_visible()


@pytest.mark.parametrize("second", ["two_clusters", "lab_cluster"])
def test_gpu_open_addons_of_another_cluster_goes_there(context, flask_server, request, second):
    two_clusters = request.getfixturevalue(second)
    data = {"clusters": [{"cluster": "harv-fake", "state": "ok", "count": 0, "addon": True,
                          "available": {"addon": True, "gpu": True, "vgpu": True, "mig": True, "node": True}},
                         {"cluster": two_clusters, "state": "no-addon", "count": 0, "addon": False}],
            "units": [], "summary": {t: {"total": 0, "used": 0, "free": 0} for t in ("gpu", "vgpu", "mig", "node")}}
    page = context.new_page()
    page.route(re.compile(r".*/api/gpus-all(\?.*)?$"), lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(data)))
    _goto(page, flask_server["base_url"], "gpus")
    pane = page.locator("#tab-gpus")
    pane.locator('[data-gpu="cluster"]').select_option(two_clusters)
    btn = pane.locator(f'[data-gpu="open-addons"][data-cluster="{two_clusters}"]')
    expect(btn).to_be_visible(timeout=8000)
    _spy_veil_and_notice(page)
    btn.click()
    page.wait_for_function(f"App.getCurrentCluster() === '{two_clusters}'", timeout=10000)
    page.wait_for_selector('.veil-fullscreen', state='detached', timeout=30000)
    expect(page.locator('.tab-content.active')).to_have_id("tab-addons")
    assert page.evaluate("window.__veils") == [two_clusters], "pas de voile de bascule"
    expect(page.locator("#access-notice")).to_be_hidden()
    assert page.evaluate("location.hash") == f"#harvester/{two_clusters}/params/addons/list"
    # Précédent revient à l'inventaire des GPU
    page.go_back()
    expect(page.locator('.tab-content.active')).to_have_id("tab-gpus", timeout=10000)


def test_migrations_wave_of_another_cluster_goes_to_its_waves(context, flask_server, two_clusters):
    import test_forklift_global_176 as G
    page = G.open_global(context, flask_server, initial_cluster="harv-second")
    row = page.locator('#tab-forkliftglobal tr[data-fkg-wave="vague-2"]')
    expect(row).to_be_visible(timeout=10000)
    _spy_veil_and_notice(page)
    row.click()
    page.wait_for_function("App.getCurrentCluster() === 'harv-fake'", timeout=10000)
    page.wait_for_selector('.veil-fullscreen', state='detached', timeout=30000)
    expect(page.locator('.tab-content.active')).to_have_id("tab-forklift")
    expect(page.locator('#tab-forklift .section-pane[data-pane="waves"]')).to_be_visible()
    assert page.evaluate("window.__veils") == ["harv-fake"], "pas de voile de bascule"
    expect(page.locator("#access-notice")).to_be_hidden()
    assert page.evaluate("location.hash") == "#harvester/harv-fake/imports/forklift/waves"


def test_cluster_api_keeps_its_management_cluster(context, flask_server, two_clusters):
    """Le cluster de gestion choisi reste celui de Cluster API quand l'hôte
    courant change ; sans choix, il suit le dernier hôte visité."""
    page = context.new_page()
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    _goto(page, flask_server["base_url"], "automation")
    assert page.evaluate("App.managementCluster()") == "harv-fake"
    page.evaluate(f"App.setManagementCluster('{two_clusters}')")
    # 2.0.0 : l'hôte courant change par le menu des hôtes (plus de sélecteur)
    _switch_host(page, "harv-fake")
    settle(page)
    assert page.evaluate("App.getCurrentCluster()") == "harv-fake"
    assert page.evaluate("App.managementCluster()") == two_clusters
    calls.clear()
    page.evaluate("CAPI.refresh()")
    page.wait_for_timeout(800)
    capi = [c for c in calls if "/api/capi/" in c]
    assert capi and all(f"/api/capi/{two_clusters}/" in c for c in capi), capi
    page.evaluate("App.setManagementCluster(null)")
    assert page.evaluate("App.managementCluster()") == "harv-fake"
