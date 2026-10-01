"""v1.82.0 : onglet « Mise à jour » de la fenêtre des versions.

L'API est simulée (page.route) : vérification en ligne avec notes, archive
préparée signée ou non, installation confiée à l'agent, puis redémarrage
suivi (réponses en échec pendant le redémarrage) jusqu'à la nouvelle version.
"""
import json
import time

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


def _json(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def base_state():
    return {
        "current": "1.81.0", "agent": {"installed": "1.81.0"}, "source": "https://github.com/x/releases/latest/download/",
        "default_source": "https://github.com/x/releases/latest/download/", "trusted_keys": True,
        "staged": [
            {"name": "harvester-ops-1.82.0.tar.gz", "version": "1.82.0", "size": 640 << 20, "signed": True,
             "signature": "valid", "newer": True, "ok": True},
            {"name": "harvester-ops-1.83.0.tar.gz", "version": "1.83.0", "size": 1 << 20, "signed": False,
             "signature": "missing", "newer": True, "ok": False},
        ],
        "pending": False, "last": None, "check": None, "busy": 0, "can_apply": True,
    }


@pytest.fixture
def ui(context, flask_server):
    page = context.new_page()
    s = {"st": base_state(), "sent": [], "phase": "idle", "polls": 0}

    def api(route, req):
        path = req.url.split("/api/update/", 1)[1].split("?", 1)[0]
        body = req.post_data_json if req.method in ("POST", "PUT") and req.post_data else None
        s["sent"].append((req.method, path, body))
        if path == "status":
            if s["phase"] == "restarting":
                s["polls"] += 1
                if s["polls"] < 3:
                    return route.fulfill(status=502, body="")
                st = dict(s["st"], current="1.82.0")
                st["last"] = {"state": "done", "from": "1.81.0", "to": "1.82.0", "started": time.time(),
                              "steps": [{"id": "check", "status": "done", "message": "1.82.0 answers"}]}
                return _json(route, st)
            return _json(route, s["st"])
        if path == "check":
            s["st"]["check"] = {"ok": True, "newer": True, "source": s["st"]["source"], "release": {
                "version": "1.82.0", "date": "2026-10-01", "title": "Update from the <UI>",
                "notes": [{"version": "1.82.0", "title": "Update", "sections": [{"name": "Added", "items": ["the tab"]}]}]}}
            return _json(route, s["st"]["check"])
        if path == "source":
            s["st"]["source"] = body["url"] or s["st"]["default_source"]
            return _json(route, {"source": s["st"]["source"]})
        if path == "apply":
            s["phase"] = "restarting"
            return _json(route, {"ok": True, "action_id": "upd1", "version": "1.82.0"}, 202)
        if path.startswith("staged/"):
            s["st"]["staged"] = [x for x in s["st"]["staged"] if x["name"] != path.split("/", 1)[1]]
            return _json(route, {"ok": True})
        return _json(route, {"error": "unexpected"}, 404)

    page.route("**/api/update/**", api)
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.ConsoleUpdate && window.i18n")
    page.on("dialog", lambda d: d.accept())
    page.evaluate("Versions.open()")
    page.click('#versions-modal [data-vpane="update"]')
    expect(page.locator("#versions-pane-update .upd-staged tr")).to_have_count(2)
    return page, s


def test_the_tab_shows_version_agent_and_staged_releases(ui):
    page, _ = ui
    pane = page.locator("#versions-pane-update")
    expect(pane).to_contain_text("1.81.0")
    expect(page.locator("#versions-pane-history")).to_be_hidden()
    ok = pane.locator('tr[data-name="harvester-ops-1.82.0.tar.gz"]')
    unsigned = pane.locator('tr[data-name="harvester-ops-1.83.0.tar.gz"]')
    expect(ok.locator('[data-upd="install"]')).to_be_enabled()
    expect(unsigned.locator('[data-upd="install"]')).to_be_disabled()
    expect(unsigned).to_contain_text(".sig")
    for btn in pane.locator("button").all():
        assert btn.get_attribute("data-tip"), btn.inner_html()


def test_check_online_shows_the_new_version_escaped(ui):
    page, s = ui
    page.click('#versions-pane-update [data-upd="check"]')
    expect(page.locator("#versions-pane-update .upd-ok")).to_contain_text("1.82.0")
    expect(page.locator("#versions-pane-update .upd-ok")).to_contain_text("Update from the <UI>")
    assert page.locator("#versions-pane-update .upd-ok UI").count() == 0
    expect(page.locator('#versions-pane-update [data-upd="download"]')).to_be_visible()
    # la pastille du numéro de version
    assert "has-update" in (page.locator("#btn-version").get_attribute("class") or "")


def test_source_saved_and_reset(ui):
    page, s = ui
    page.fill("#upd-source", "https://mirror.example/hops/")
    # un rafraîchissement en arrière-plan ne doit pas effacer la saisie
    page.evaluate("ConsoleUpdate.load()")
    expect(page.locator("#upd-source")).to_have_value("https://mirror.example/hops/")
    page.click('#versions-pane-update [data-upd="save-source"]')
    expect(page.locator("#upd-source")).to_have_value("https://mirror.example/hops/")
    page.click('#versions-pane-update [data-upd="reset-source"]')
    expect(page.locator("#upd-source")).to_have_value(s["st"]["default_source"])
    assert ("PUT", "source", {"url": ""}) in s["sent"]


def test_install_follows_the_restart_then_reloads(ui):
    page, s = ui
    page.locator('tr[data-name="harvester-ops-1.82.0.tar.gz"] [data-upd="install"]').click()
    expect(page.locator(".upd-follow")).to_be_visible()
    assert ("POST", "apply", {"archive": "harvester-ops-1.82.0.tar.gz"}) in s["sent"]
    # pendant le redémarrage, puis la nouvelle version : la page le dit avant de se recharger
    expect(page.locator(".upd-follow h4")).to_contain_text("1.82.0", timeout=20000)


def test_delete_a_staged_release(ui):
    page, s = ui
    page.locator('tr[data-name="harvester-ops-1.83.0.tar.gz"] [data-upd="delete"]').click()
    expect(page.locator("#versions-pane-update .upd-staged tr")).to_have_count(1)
