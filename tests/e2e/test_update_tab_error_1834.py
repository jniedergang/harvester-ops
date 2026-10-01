"""v1.83.4 : l'onglet « Mise à jour » dit pourquoi il ne charge pas.

Vu en réel : une console de dev lancée avant la 1.82.0 servait la nouvelle
page mais pas la route /api/update/status (404) ; l'onglet restait sur
« Chargement… » à vie. Il affiche désormais la raison et un bouton Réessayer.
"""
import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

STATE = {
    "current": "1.83.4", "agent": None, "source": "https://example.invalid/", "default_source": "https://example.invalid/",
    "trusted_keys": True, "staged": [], "pending": False, "last": None, "check": None, "busy": 0, "can_apply": True,
}


def test_missing_route_is_explained_and_retry_recovers(context, flask_server):
    page = context.new_page()
    s = {"status": 404}

    def api(route, req):
        if s["status"] == 200:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(STATE))
        return route.fulfill(status=s["status"], content_type="text/html", body="<h1>Not Found</h1>")

    page.route("**/api/update/status", api)
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.ConsoleUpdate && window.i18n")
    page.evaluate("Versions.open()")
    page.click('#versions-modal [data-vpane="update"]')
    pane = page.locator("#versions-pane-update")
    expect(pane.locator(".upd-err")).to_have_text(page.evaluate("i18n.t('upd.load.missing')"))
    expect(pane.locator('[data-upd="reload"]')).to_be_visible()

    s["status"] = 500
    pane.locator('[data-upd="reload"]').click()
    expect(pane.locator(".upd-err")).to_contain_text("500")

    s["status"] = 200
    pane.locator('[data-upd="reload"]').click()
    expect(pane.locator(".upd-head")).to_contain_text("1.83.4")
    expect(pane.locator('[data-upd="reload"]')).to_have_count(0)
