"""v1.83.5 : messages de l'onglet « Mise à jour » sur une console de dev.

Vu en réel : une console 1.83.4 lancée depuis les sources affichait en rouge
« aucun agent » et « À jour : la source propose 1.83.3 » (vérification faite
avant la publication). Elle dit maintenant qu'une console des sources n'a pas
d'agent (sans alarme), qu'elle est plus récente que la source, et l'heure de la
vérification.
"""
import json
import time

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


def _state(**kw):
    st = {"current": "1.83.4", "agent": None, "source": "https://example.invalid/",
          "default_source": "https://example.invalid/", "trusted_keys": True, "staged": [], "pending": False,
          "last": None, "busy": 0, "can_apply": True, "from_sources": True,
          "check": {"ok": True, "newer": False, "ts": time.time(), "source": "https://example.invalid/",
                    "release": {"version": "1.83.3", "notes": []}}}
    st.update(kw)
    return st


def _open(context, flask_server, st):
    page = context.new_page()
    page.route("**/api/update/status", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(st)))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.ConsoleUpdate && window.i18n")
    page.evaluate("Versions.open()")
    page.click('#versions-modal [data-vpane="update"]')
    pane = page.locator("#versions-pane-update")
    expect(pane.locator(".upd-head")).to_be_visible()
    return page, pane


def test_source_console_is_not_an_error_and_ahead_is_said(context, flask_server):
    page, pane = _open(context, flask_server, _state())
    expect(pane.locator(".upd-head .upd-err")).to_have_count(0)
    expect(pane.locator(".upd-head")).to_contain_text(page.evaluate("i18n.t('upd.agent.sources')"))
    ahead = page.evaluate("i18n.t('upd.check.ahead', {v: '1.83.3', cur: '1.83.4'})")
    expect(pane).to_contain_text(ahead)
    expect(pane).not_to_contain_text(page.evaluate("i18n.t('upd.check.uptodate', {v: '1.83.3'})"))


def test_packaged_console_without_agent_is_still_an_error(context, flask_server):
    st = _state(from_sources=False)
    st["check"]["release"]["version"] = "1.83.4"
    page, pane = _open(context, flask_server, st)
    expect(pane.locator(".upd-head .upd-err")).to_have_text(page.evaluate("i18n.t('upd.agent.missing')"))
    expect(pane).to_contain_text(page.evaluate("i18n.t('upd.check.uptodate', {v: '1.83.4'})"))
