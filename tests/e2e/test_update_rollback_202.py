"""2.0.2 : fenêtre de retour à une version précédente (onglet Mise à jour).

La liste vient de l'état (archives signées plus anciennes). La fenêtre dit
ce qui est gardé et perdu, et le bouton ne s'active que quand la version
visée est retapée exactement ; la demande part vers /api/update/rollback."""
import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

OLD = {"name": "harvester-ops-1.101.3.tar.gz", "version": "1.101.3", "size": 1 << 29, "signed": True,
       "signature": "valid", "ok": True, "newer": False, "older": True}


def _state(**kw):
    st = {"current": "2.0.2", "agent": {"installed": "2.0.2"}, "source": "https://example.invalid/",
          "default_source": "https://example.invalid/", "trusted_keys": True, "staged": [OLD],
          "rollback": ["1.101.3"], "pending": False, "last": None, "busy": 0, "can_apply": True,
          "from_sources": False, "check": None}
    st.update(kw)
    return st


def _open(context, flask_server, st, posted=None, answer=(202, {"ok": True, "action_id": "a1", "version": "1.101.3"})):
    page = context.new_page()
    page.route("**/api/update/status", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(st)))

    def rollback(route, req):
        if posted is not None:
            posted.append(json.loads(req.post_data or "{}"))
        route.fulfill(status=answer[0], content_type="application/json", body=json.dumps(answer[1]))
    page.route("**/api/update/rollback", rollback)
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.ConsoleUpdate && window.i18n")
    page.evaluate("Versions.open()")
    page.click('#versions-modal [data-vpane="update"]')
    pane = page.locator("#versions-pane-update")
    expect(pane.locator(".upd-head")).to_be_visible()
    return page, pane


def test_dialog_needs_the_typed_version_then_hands_over(context, flask_server):
    posted = []
    page, pane = _open(context, flask_server, _state(), posted)
    btn = pane.locator('.upd-rollback [data-upd="rollback"]')
    expect(btn).to_have_text(page.evaluate("i18n.t('upd.rb.button', {v: '1.101.3'})"))
    assert btn.get_attribute("data-tip") and btn.get_attribute("title")
    btn.click()
    dialog = pane.locator(".upd-rb-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text(page.evaluate("i18n.t('upd.rb.dialogTitle', {v: '1.101.3', cur: '2.0.2'})"))
    # notes propres à une cible avant 2.0.0 et avant 2.0.2
    expect(dialog).to_contain_text(page.evaluate("i18n.t('upd.rb.before200')"))
    expect(dialog).to_contain_text(page.evaluate("i18n.t('upd.rb.before202', {cur: '2.0.2'})"))
    go = dialog.locator('[data-upd="rollback-go"]')
    expect(go).to_be_disabled()
    field = dialog.locator("#upd-rb-confirm")
    field.fill("1.101")
    expect(go).to_be_disabled()
    field.fill("1.101.3")
    expect(go).to_be_enabled()
    go.click()
    expect(pane.locator(".upd-follow")).to_be_visible()
    assert posted == [{"archive": OLD["name"], "confirm": "1.101.3"}]


def test_refusal_is_shown_in_the_dialog(context, flask_server):
    page, pane = _open(context, flask_server, _state(),
                       answer=(409, {"error": "an update is already requested"}))
    pane.locator('.upd-rollback [data-upd="rollback"]').click()
    pane.locator("#upd-rb-confirm").fill("1.101.3")
    pane.locator('[data-upd="rollback-go"]').click()
    expect(pane.locator("#upd-rb-msg")).to_have_text("an update is already requested")
    pane.locator('[data-upd="rollback-cancel"]').click()
    expect(pane.locator(".upd-rb-dialog")).to_have_count(0)


def test_no_older_archive_says_how_to_get_one(context, flask_server):
    page, pane = _open(context, flask_server, _state(staged=[], rollback=[]))
    card = pane.locator(".upd-rollback")
    expect(card).to_contain_text(page.evaluate("i18n.t('upd.rb.none')"))
    expect(card.locator('[data-upd="rollback"]')).to_have_count(0)


def test_not_offered_to_a_non_admin(context, flask_server):
    page, pane = _open(context, flask_server, _state(can_apply=False))
    expect(pane.locator(".upd-rollback")).to_have_count(0)


def test_recent_target_has_no_old_version_notes(context, flask_server):
    old = dict(OLD, name="harvester-ops-2.0.2.tar.gz", version="2.0.2")
    page, pane = _open(context, flask_server, _state(current="2.0.3", staged=[old], rollback=["2.0.2"]))
    pane.locator('.upd-rollback [data-upd="rollback"]').click()
    dialog = pane.locator(".upd-rb-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).not_to_contain_text(page.evaluate("i18n.t('upd.rb.before202', {cur: '2.0.3'})"))
