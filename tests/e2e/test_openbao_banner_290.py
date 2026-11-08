"""2.9.0 tâche 12 : bandeau global de panne d'OpenBao, objet protégé
« Magasin de secrets » de la vue d'ensemble Disaster Recovery, état chiffré
des déclarations Terraform, sur routes simulées.

Mutation jouée (rapport des tâches 11-12) : « échappement retiré » (bandeau).
"""

import json
import re
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_settings_secrets_290 import HOSTILE, fulfill, open_secrets, shot, status_doc  # noqa: E402
import test_replication_ui as trui  # noqa: E402
import test_tf_decl_view as ttfd  # noqa: E402

ui_factory = trui.ui_factory


# ---------------------------------------------------------------------------
# Bandeau global de panne
# ---------------------------------------------------------------------------
def test_banner_tells_until_when_the_cache_holds(context, flask_server):
    until = time.time() + 600
    st = {"status": status_doc(), "banner": {"active": True, "mode": "b1", "state": "degraded",
                                             "down_since": time.time() - 120, "cache_until": until,
                                             "error": "openbao-sealed"}}
    page = open_secrets(context, flask_server, st)
    b = page.locator("#secrets-banner")
    expect(b).to_be_visible()
    hhmm = page.evaluate(f"() => new Date({until * 1000}).toLocaleTimeString([], {{hour: '2-digit', minute: '2-digit'}})")
    expect(b).to_contain_text(f"served from memory until {hhmm}")
    expect(b).to_contain_text("openbao-sealed")
    shot(page, "secrets-banner-degraded.png")


def test_banner_hidden_when_reachable_or_inactive(context, flask_server):
    st = {"status": status_doc(), "banner": {"active": True, "mode": "b1", "state": "ok"}}
    page = open_secrets(context, flask_server, st)
    page.wait_for_timeout(300)
    expect(page.locator("#secrets-banner")).to_be_hidden()


def test_banner_down_escapes_the_error(context, flask_server):
    st = {"status": status_doc(), "banner": {"active": True, "mode": "a", "state": "down",
                                             "down_since": time.time() - 60, "cache_until": None, "error": HOSTILE}}
    page = open_secrets(context, flask_server, st)
    expect(page.locator("#secrets-banner")).to_contain_text("are refused")
    assert page.evaluate("() => window.__pwned") is None


# ---------------------------------------------------------------------------
# Vue d'ensemble Disaster Recovery : objet protégé « Magasin de secrets »
# ---------------------------------------------------------------------------
def _dr(ui_factory, status):
    page, sent, dialogs = ui_factory("admin")
    page.route(re.compile(r".*/api/secrets/status$"), lambda r, q: fulfill(r, status))
    pane = trui.open_plans(page)
    return page, pane


def test_dr_overview_shows_the_secret_store_with_its_last_snapshot(ui_factory):
    page, pane = _dr(ui_factory, status_doc())
    card = pane.locator('[data-rp="secstore"] .sx-dr')
    expect(card).to_contain_text("Secret store")
    expect(card).to_contain_text("14 kept, daily at 02:30")
    restore = card.locator("[data-sx-dr-open]")
    assert restore.get_attribute("data-tip")
    shot(page, "secrets-dr-overview.png")
    restore.click()
    expect(page.locator("#secrets-settings-body [data-sx-tab=\"snapshots\"]")).to_have_class(re.compile("active"))


def test_dr_overview_warns_on_a_stale_snapshot_and_hides_when_inactive(ui_factory):
    page, pane = _dr(ui_factory, status_doc(warnings=[{"code": "snapshot-stale", "level": "warn"}]))
    expect(pane.locator('[data-sx-dr="warn"]')).to_contain_text("older than 2 days")


def test_dr_overview_without_openbao(ui_factory):
    page, pane = _dr(ui_factory, status_doc(mode="local", active=False))
    page.wait_for_timeout(300)
    expect(pane.locator('[data-rp="secstore"]')).to_be_hidden()


# ---------------------------------------------------------------------------
# Déclarations Terraform : état chiffré, avertissement Terraform, remise en clair
# ---------------------------------------------------------------------------
def _decl_view(page, encryption, state_error=None):
    def decls(route, req):
        if req.method != "GET":
            return route.continue_()
        resp = route.fetch()
        body = resp.json()
        for d in body.get("declarations", []):
            d["encryption"] = encryption
            if state_error:
                d["state_error"] = state_error
        route.fulfill(response=resp, json=body)
    page.route(re.compile(r".*/api/tf-declarations$"), decls)
    ttfd.open_view(page, lang="en")
    ttfd.seed(page, "web-enc", [ttfd.KEYRES])


def test_encrypted_state_badge_and_put_back_in_clear(page):
    posted = []
    page.route(re.compile(r".*/api/tf-declarations/[a-f0-9]+/decrypt-state$"),
               lambda r, q: (posted.append(q.url), fulfill(r, {"action_id": "d1"}, 202)))
    page.on("dialog", lambda d: d.accept())
    _decl_view(page, {"state": "encrypted", "block": "enforced", "warning": None})
    badge = page.locator('[data-tfd-enc="encrypted"]')
    expect(badge).to_contain_text("Encrypted state")
    assert badge.get_attribute("data-tip")
    btn = page.locator(".tfd-decrypt")
    assert "Before deactivating OpenBao" in btn.get_attribute("data-tip")
    btn.click()
    page.wait_for_timeout(300)
    assert posted and posted[0].endswith("/decrypt-state")


def test_terraform_flavor_warns_and_migration_notice(page):
    _decl_view(page, {"state": "plain", "block": "migrate-in", "warning": "tf-encryption-needs-opentofu"},
               state_error="tf-state-unreadable")
    expect(page.locator('[data-tfd-enc="needs-opentofu"]')).to_contain_text("State not encrypted")
    notes = page.locator("[data-tfd-enc-notice]")
    expect(notes.first).to_contain_text("migration of this state is not finished")
    expect(notes.nth(1)).to_contain_text("tf-state-unreadable")
    assert page.locator(".tfd-decrypt").count() == 0
