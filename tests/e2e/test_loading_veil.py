"""v1.26.0 — le voile de chargement sur les onglets lents.

Signalé à l'usage sur Cluster API : le diagnostic interroge le cluster et
prend une dizaine de secondes sur harv1. Pendant ce temps la carte était
vidée et remplacée par un « Loading… » nu, ce qui donnait l'impression que
l'onglet s'était cassé, alors que la bascule de cluster et l'aperçu, eux,
affichent depuis la 1.20.0 un voile flouté par-dessus le contenu précédent.

La réponse du serveur est ralentie ici volontairement : le voile n'apparaît
qu'au-delà d'un seuil anti-clignotement, et un test qui dépendrait de la
vitesse réelle du cluster serait instable.
"""

import pytest

playwright = pytest.importorskip("playwright")

SLOW_MS = 1500


def open_automation(page, base_url, subtab):
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(900)
    # Le menu est un rail : ses enfants ne sont atteignables qu'une fois
    # déplié, et il se replie dès que le pointeur le quitte.
    page.locator('#sidebar').hover()
    page.wait_for_timeout(350)
    page.click('.tab-group-head[data-group="automation"]')
    page.wait_for_timeout(250)
    page.locator('#sidebar').hover()
    page.wait_for_timeout(200)
    page.click(f'.tab-child[data-subtab="{subtab}"]')


def veil_in(page, body_selector):
    return page.locator(body_selector).locator(
        'xpath=ancestor::div[contains(@class,"card")][1]').locator('.veil')


@pytest.mark.parametrize("subtab,body,slow_url", [
    ("capi", "#capi-status-body", "**/api/capi/**/diag"),
    ("terraform", "#tf-status-body", "**/api/terraform/info"),
])
def test_a_slow_tab_shows_the_blurred_veil(context, flask_server, subtab,
                                            body, slow_url):
    page = context.new_page()

    def slow(route):
        # Retarde la réponse sans la falsifier : c'est bien le vrai
        # chargement qu'on observe.
        page.wait_for_timeout(SLOW_MS)
        route.continue_()

    page.route(slow_url, slow)
    open_automation(page, flask_server["base_url"], subtab)

    veil = veil_in(page, body)
    page.wait_for_timeout(700)          # au-delà du seuil anti-clignotement
    assert veil.count() and veil.first.is_visible(), \
        f"aucun voile pendant le chargement de {subtab}"
    assert "harv-fake" in veil.first.inner_text(), \
        "le voile doit dire QUEL cluster est interrogé"

    # Et il repart : un voile qui reste avale tous les clics.
    page.wait_for_timeout(SLOW_MS + 2500)
    assert veil.locator('.visible').count() == 0 and not veil.first.is_visible(), \
        "voile resté à l'écran"


def test_a_fast_tab_does_not_flash_a_veil(context, flask_server):
    """Seuil anti-clignotement : sur une réponse immédiate, faire
    apparaître puis disparaître un voile est pire que ne rien montrer.

    L'assertion vise la carte Terraform et elle seule : la page porte
    d'autres voiles (l'aperçu se charge à l'arrivée) et les compter tous
    ferait échouer le test pour une raison sans rapport.
    """
    page = context.new_page()
    open_automation(page, flask_server["base_url"], "terraform")
    page.wait_for_timeout(200)
    veil = veil_in(page, "#tf-status-body")
    assert veil.count() == 0 or not veil.first.is_visible()
