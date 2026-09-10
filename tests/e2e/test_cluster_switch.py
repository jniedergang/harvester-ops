"""v1.20.0 — bascule d'un cluster à l'autre.

Le défaut corrigé ici est sournois parce qu'il ne casse rien : en changeant
de cluster, l'écran gardait les données du PRÉCÉDENT. Seul l'aperçu se
rafraîchissait ; en restant sur Cluster API ou sur la liste des VMs, on
lisait donc un cluster en croyant en lire un autre — le meilleur moyen
d'agir sur la mauvaise machine.

Ces tests exercent la bascule dans un vrai navigateur, sur une page servie
par l'application, avec deux clusters déclarés.
"""

import pytest

playwright = pytest.importorskip("playwright")


@pytest.fixture
def two_clusters(flask_server, api):
    """Déclare un second cluster le temps du test.

    On ne touche pas au config.yaml partagé de la session : d'autres tests
    comptent les clusters déclarés.
    """
    payload = {
        "name": "harv-second",
        "description": "second cluster (test de bascule)",
        "kubeconfig": str(flask_server["config"]["kubeconfig"]),
        "nodes": [{"hostname": "second-node1", "ip": "10.0.0.2",
                   "role": "control-plane"}],
    }
    status, _ = api("POST", "/api/clusters", payload, expect_status=None)
    if status not in (201, 409):
        pytest.skip(f"impossible de déclarer le second cluster: {status}")
    yield "harv-second"
    api("DELETE", "/api/clusters/harv-second", expect_status=None)


def _goto(page, base_url, tab, cluster="harv-fake"):
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        f"localStorage.setItem('harvester_ops_current_cluster','{cluster}');"
        f"localStorage.setItem('harvester_ops_current_tab','{tab}');")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)


def test_switching_clusters_shows_a_blurred_loading_veil(
        context, flask_server, two_clusters):
    page = context.new_page()
    _goto(page, flask_server["base_url"], "overview")
    if page.locator('#cluster-select option').count() < 2:
        pytest.skip("le second cluster n'est pas dans le sélecteur")

    page.select_option('#cluster-select', two_clusters)
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
    if page.locator('#cluster-select option').count() < 2:
        pytest.skip("le second cluster n'est pas dans le sélecteur")

    page.select_option('#cluster-select', two_clusters)
    page.wait_for_selector('.veil-fullscreen', state='hidden', timeout=30000)
    page.click('.tab[data-tab="activity"]', timeout=5000)
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

    page.select_option('#cluster-select', two_clusters)
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
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','overview');"
        "localStorage.setItem('harvester_ops_overview_subtab','cluster');")
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    if page.locator('#cluster-select option').count() < 2:
        pytest.skip("le second cluster n'est pas dans le sélecteur")

    # partir sur un autre onglet, basculer de cluster, puis revenir
    page.click('.tab[data-tab="activity"]')
    page.wait_for_timeout(400)
    page.select_option('#cluster-select', two_clusters)
    page.wait_for_selector('.veil-fullscreen', state='hidden', timeout=30000)
    calls.clear()
    page.click('.tab[data-tab="overview"]')
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
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','overview');"
        "localStorage.setItem('harvester_ops_overview_subtab','cluster');")
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    page.click('.tab[data-tab="activity"]')
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
        "localStorage.setItem('harvester_ops_current_tab','overview');"
        "localStorage.setItem('harvester_ops_overview_subtab','metrics');"
        # Ralentir la topologie DANS le navigateur : bloquer le pilote
        # Playwright fausserait la mesure du temps.
        "const _f = window.fetch;"
        "window.fetch = (...a) => String(a[0]).includes('/api/topology/')"
        "  ? new Promise(r => setTimeout(() => r(_f(...a)), 1800))"
        "  : _f(...a);")
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    page.click('[data-overview-tab="cluster"]')
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
    page.click('.tab[data-tab="activity"]', timeout=5000)
    assert page.locator('.tab-content.active').get_attribute('id') == "tab-activity"


def test_a_fast_view_shows_no_veil_at_all(context, flask_server):
    """Sans seuil, une réponse rapide produirait un clignotement plus
    gênant que l'attente qu'il annonce."""
    page = context.new_page()
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','overview');"
        "localStorage.setItem('harvester_ops_overview_subtab','metrics');")
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    seen = []
    page.expose_function("_veilSeen", lambda: seen.append(1))
    page.evaluate("""() => {
      const orig = window.Veil.show;
      window.Veil.show = (t, o) => { window._veilSeen(); return orig(t, o); };
    }""")
    page.click('[data-overview-tab="cluster"]')
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
        "localStorage.setItem('harvester_ops_current_tab','activity');"
        "localStorage.removeItem('harvester_ops_activity_filters');")
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(1800)

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
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_tab','activity');")
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(1800)
    page.fill('#act-f-q', 'shutdown')
    page.wait_for_timeout(900)

    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(1800)
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
        "localStorage.setItem('harvester_ops_current_tab','activity');"
        "localStorage.removeItem('harvester_ops_activity_filters');")
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(1800)
    text = page.locator('#act-filter-count').inner_text()
    assert text.strip(), "aucun compteur affiché"
    assert "entries" in text
