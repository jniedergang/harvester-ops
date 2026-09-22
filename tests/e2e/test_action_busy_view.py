"""v1.44.10 — un arrêt refusé parce qu'un démarrage tourne déjà se VOIT.

Avant, `api()` levait « HTTP 409 » dans une promesse que personne
n'attrapait : le bouton semblait ne rien faire. Le refus s'écrit désormais
dans le journal de l'onglet, dans la langue de l'interface.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


def _refuse(route, request):
    route.fulfill(status=409, content_type="application/json", body=json.dumps({
        "error": "a startup is already running on harv-fake (action abc123def456)",
        "running": "abc123def456", "running_action": "startup"}))


@pytest.mark.parametrize("lang,needle", [("en", "a startup is already running"),
                                         ("fr", "un démarrage tourne déjà")])
def test_a_refused_shutdown_is_written_in_the_log(context, flask_server, lang, needle):
    context.add_init_script(f"localStorage.setItem('harvester_ops_language','{lang}');")
    page = context.new_page()
    page.goto(flask_server["base_url"])
    page.wait_for_load_state("networkidle")
    page.route("**/api/action", _refuse)
    page.on("dialog", lambda d: d.accept())
    page.locator('.tab[data-tab="shutdown"]').first.click()
    page.locator("#btn-shutdown").click()
    log = page.locator("#shutdown-log")
    expect(log).to_contain_text(needle, timeout=5000)
    expect(log).to_contain_text("abc123def456")
    # Le bouton redevient utilisable : l'opérateur peut réessayer plus tard.
    expect(page.locator("#btn-shutdown")).to_be_enabled()
