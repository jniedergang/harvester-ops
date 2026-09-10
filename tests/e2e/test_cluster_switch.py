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
    veil = page.locator('#cluster-switch-overlay')
    assert veil.is_visible(), "aucun voile pendant la bascule"
    assert two_clusters in veil.inner_text(), "le voile ne nomme pas le cluster"
    assert "cluster-switching" in (page.locator('body').get_attribute('class') or "")

    page.wait_for_selector('#cluster-switch-overlay', state='hidden', timeout=30000)
    assert "cluster-switching" not in (page.locator('body').get_attribute('class') or "")


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
    page.wait_for_selector('#cluster-switch-overlay', state='hidden', timeout=30000)
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
    page.wait_for_selector('#cluster-switch-overlay', state='hidden', timeout=30000)
    page.wait_for_timeout(800)

    assert any(expected in c for c in calls), (
        f"aucun appel {expected} après la bascule ; vus : "
        f"{[c.split('/api/')[-1] for c in calls if '/api/' in c][:8]}")
    assert not any("/harv-fake" in c for c in calls), (
        "des données de l'ancien cluster sont encore demandées")
