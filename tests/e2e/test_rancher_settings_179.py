"""v1.79.0 : Réglages > Connexion par Rancher.

L'API est simulée (page.route) : la liste des Rancher avec leur origine, un
Rancher de config.yaml en lecture seule, l'ajout, la modification, la
suppression, le test de connexion, les fenêtres d'authentification unique et
du chart Harvester RBAC. Les identifiants d'administrateur partent dans la
requête et ne restent pas dans la page après l'envoi ; l'installation du
chart devient une action suivie dans le dock.
"""

import copy
import json
import time

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

SECRET = "S3cret-admin-pw-179"
ACTION = "rbac000000179"

BASE = [
    {"id": "corp", "label": "Rancher corp", "url": "https://rancher.corp.example", "origin": "config",
     "editable": False, "insecure": False, "has_ca": True, "default_role": "viewer", "session_hours": 8,
     "direct_enabled": True, "sso": {"enabled": True, "client_id": "client-corp", "registered_at": 1}},
    {"id": "lab", "label": "Rancher <lab>", "url": "https://rancher.lab.example", "origin": "console",
     "editable": True, "insecure": True, "has_ca": False, "default_role": "operator", "session_hours": 4,
     "direct_enabled": False, "sso": {"enabled": False, "client_id": "", "registered_at": None}},
]
PROVIDERS = [{"id": "local", "type": "localProvider", "enabled": True, "password": True},
             {"id": "openldap", "type": "openLdapProvider", "enabled": True, "password": True},
             {"id": "keycloakoidc", "type": "keyCloakOIDCProvider", "enabled": True, "password": False}]


def _json(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    page = context.new_page()
    state = {"servers": copy.deepcopy(BASE), "sent": [], "installed": False}

    def api(route, req):
        path = req.url.split("/api/rancher/servers", 1)[1].split("?", 1)[0]
        body = req.post_data_json if req.method in ("POST", "PUT") else None
        state["sent"].append((req.method, path, body))
        parts = [p for p in path.split("/") if p]
        if not parts:
            if req.method == "GET":
                return _json(route, {"servers": state["servers"], "config_writable": False})
            new = {"id": "new1", "origin": "console", "editable": True, "has_ca": bool(body.get("ca")),
                   "sso": {"enabled": False, "client_id": "", "registered_at": None}, **body}
            new.pop("ca", None)
            state["servers"].append(new)
            return _json(route, {"server": new}, 201)
        sid = parts[0]
        srv = next((s for s in state["servers"] if s["id"] == sid), None)
        if len(parts) == 1:
            if req.method == "DELETE":
                if srv["origin"] == "config":
                    return _json(route, {"error": "set in config.yaml"}, 409)
                state["servers"].remove(srv)
                return _json(route, {"ok": True})
            srv.update({k: v for k, v in body.items() if k != "ca"})
            return _json(route, {"server": srv})
        tail = "/".join(parts[1:])
        if tail == "test":
            return _json(route, {"ok": True, "version": "v2.14.1", "providers": PROVIDERS, "error": None})
        if tail == "sso/register":
            srv["sso"] = {"enabled": True, "client_id": "client-new", "registered_at": 2}
            return _json(route, {"client_id": "client-new"})
        if tail == "sso/unregister":
            srv["sso"] = {"enabled": False, "client_id": "", "registered_at": None}
            return _json(route, {"ok": True})
        if tail == "rbac/status":
            return _json(route, {"installed": False, "version": "", "available_version": "109.0.0+up0.1.1",
                                 "compatible": True, "reason": ""})
        if tail == "rbac/install":
            state["installed"] = True
            return _json(route, {"action_id": ACTION}, 202)
        return _json(route, {"error": "unexpected"}, 404)

    def activity(route, req):
        now = time.time()
        running = [{"id": ACTION, "action": "rancher-rbac-install", "cluster": "lab", "status": "running",
                    "started_at": now - 2, "ended_at": None, "exit_code": None}] if state["installed"] else []
        _json(route, {"in_progress": running, "actions_done": []})

    page.route("**/api/rancher/servers**", api)
    page.route("**/api/activity*", activity)
    page.route(f"**/api/stream/{ACTION}", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.RancherServers && window.i18n")
    page.on("dialog", lambda d: d.accept())
    page.click("#btn-settings")
    page.click('.settings-tab[data-stab="rancher"]')
    expect(page.locator("#rsrv-list .rsrv-card")).to_have_count(2)
    return page, state


def _card(page, sid):
    return page.locator(f'#rsrv-list .rsrv-card[data-id="{sid}"]')


def test_the_list_says_origin_sso_direct_and_role(ui):
    page, _ = ui
    corp, lab = _card(page, "corp"), _card(page, "lab")
    expect(corp.locator(".cluster-origin")).to_have_attribute("data-origin", "config")
    expect(lab.locator(".cluster-origin")).to_have_attribute("data-origin", "console")
    expect(corp.locator(".rsrv-sso")).to_have_attribute("data-on", "1")
    expect(lab.locator(".rsrv-sso")).to_have_attribute("data-on", "0")
    expect(corp.locator(".rsrv-direct")).to_have_attribute("data-on", "1")
    expect(lab.locator(".rsrv-direct")).to_have_attribute("data-on", "0")
    # un nom venu du serveur est échappé, jamais interprété
    expect(lab.locator("h5")).to_contain_text("Rancher <lab>")
    assert lab.locator("h5 lab").count() == 0
    # chaque contrôle a sa bulle
    for btn in page.locator("#stab-rancher button").all():
        assert btn.get_attribute("data-tip"), btn.inner_html()


def test_a_config_entry_is_read_only(ui):
    page, _ = ui
    corp, lab = _card(page, "corp"), _card(page, "lab")
    for act in ("edit", "delete", "sso-off"):
        expect(corp.locator(f'[data-rsrv="{act}"]')).to_be_disabled()
    assert "config.yaml" in corp.locator(".rsrv-readonly").get_attribute("data-tip")
    expect(corp.locator('[data-rsrv="test"]')).to_be_enabled()
    expect(corp.locator('[data-rsrv="rbac"]')).to_be_enabled()
    for act in ("edit", "delete", "sso-on"):
        expect(lab.locator(f'[data-rsrv="{act}"]')).to_be_enabled()


def test_the_test_button_shows_version_and_providers(ui):
    page, _ = ui
    corp = _card(page, "corp")
    corp.locator('[data-rsrv="test"]').click()
    res = corp.locator(".rsrv-result")
    expect(res).to_contain_text("v2.14.1")
    expect(res.locator(".rsrv-provider")).to_have_count(3)
    expect(res).to_contain_text("openldap")


def test_add_a_rancher(ui):
    page, state = ui
    page.locator('#stab-rancher [data-rsrv="add"]').click()
    f = page.locator("#rsrv-form-modal form")
    f.locator('[name="label"]').fill("Rancher new")
    f.locator('[name="url"]').fill("https://rancher.new.example")
    f.locator('[name="ca"]').fill("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----")
    f.locator('[name="default_role"]').select_option("admin")
    f.locator('[name="session_hours"]').fill("12")
    f.locator('button[type="submit"]').click()
    expect(page.locator("#rsrv-form-modal")).to_have_count(0)
    expect(page.locator("#rsrv-list .rsrv-card")).to_have_count(3)
    method, path, body = [s for s in state["sent"] if s[0] == "POST" and s[1] == ""][-1]
    assert body["label"] == "Rancher new" and body["url"] == "https://rancher.new.example"
    assert body["ca"].startswith("-----BEGIN CERTIFICATE-----")
    assert body["insecure"] is False and body["default_role"] == "admin"
    assert body["session_hours"] == 12 and body["direct_enabled"] is True


def test_session_hours_out_of_range_is_refused(ui):
    page, state = ui
    page.locator('#stab-rancher [data-rsrv="add"]').click()
    f = page.locator("#rsrv-form-modal form")
    f.locator('[name="label"]').fill("x")
    f.locator('[name="url"]').fill("https://x.example")
    f.locator('[name="session_hours"]').evaluate("el => { el.removeAttribute('max'); el.value = '30'; }")
    f.locator('button[type="submit"]').click()
    expect(page.locator("#rsrv-form-modal .res-error")).to_be_visible()
    assert not [s for s in state["sent"] if s[0] == "POST" and s[1] == ""]


def test_edit_a_rancher_keeps_its_ca_when_left_empty(ui):
    page, state = ui
    _card(page, "lab").locator('[data-rsrv="edit"]').click()
    f = page.locator("#rsrv-form-modal form")
    expect(f.locator('[name="label"]')).to_have_value("Rancher <lab>")
    expect(f.locator('[name="insecure"]')).to_be_checked()
    expect(f.locator('[name="ca"]')).to_be_disabled()
    f.locator('[name="insecure"]').uncheck()
    expect(f.locator('[name="ca"]')).to_be_enabled()
    f.locator('[name="direct_enabled"]').check()
    f.locator('button[type="submit"]').click()
    expect(page.locator("#rsrv-form-modal")).to_have_count(0)
    method, path, body = [s for s in state["sent"] if s[0] == "PUT"][-1]
    assert path == "/lab" and "ca" not in body
    assert body["insecure"] is False and body["direct_enabled"] is True
    expect(_card(page, "lab").locator(".rsrv-direct")).to_have_attribute("data-on", "1")


def test_delete_a_rancher(ui):
    page, state = ui
    _card(page, "lab").locator('[data-rsrv="delete"]').click()
    expect(page.locator("#rsrv-list .rsrv-card")).to_have_count(1)
    assert ("DELETE", "/lab", None) in state["sent"]


def _no_secret_left(page):
    assert SECRET not in page.content()
    values = page.evaluate("[...document.querySelectorAll('input')].map(i => i.value)")
    assert SECRET not in values
    assert SECRET not in json.dumps(page.evaluate("Object.assign({}, localStorage)"))


def test_enable_single_sign_on(ui):
    page, state = ui
    _card(page, "lab").locator('[data-rsrv="sso-on"]').click()
    m = page.locator("#rsrv-sso-modal")
    expect(m.locator(".rsrv-creds-note")).to_be_visible()
    # les fournisseurs à mot de passe du Rancher, lus par le test
    expect(m.locator('[name="provider"] option')).to_have_count(2)
    m.locator('[name="provider"]').select_option("openldap")
    m.locator('[name="admin_user"]').fill("admin")
    m.locator('[name="admin_password"]').fill(SECRET)
    m.locator('button[type="submit"]').click()
    expect(m.locator(".rsrv-done")).to_contain_text("client-new")
    _no_secret_left(page)
    method, path, body = [s for s in state["sent"] if s[1] == "/lab/sso/register"][-1]
    assert body == {"admin_user": "admin", "admin_password": SECRET, "provider": "openldap"}
    expect(_card(page, "lab").locator(".rsrv-sso")).to_have_attribute("data-on", "1")
    expect(page.locator("#rsrv-sso-modal")).to_have_count(0, timeout=4000)


def test_disable_single_sign_on(ui):
    page, state = ui
    # le Rancher du laboratoire a déjà l'authentification unique
    state["servers"][1]["sso"] = {"enabled": True, "client_id": "client-lab", "registered_at": 2}
    page.locator('#stab-rancher [data-rsrv="refresh"]').click()
    expect(_card(page, "lab").locator('[data-rsrv="sso-off"]')).to_be_visible()
    _card(page, "lab").locator('[data-rsrv="sso-off"]').click()
    m = page.locator("#rsrv-sso-modal")
    m.locator('[name="admin_user"]').fill("admin")
    m.locator('[name="admin_password"]').fill(SECRET)
    m.locator('button[type="submit"]').click()
    expect(m.locator(".rsrv-done")).to_be_visible()
    _no_secret_left(page)
    assert [s for s in state["sent"] if s[1] == "/lab/sso/unregister"][-1][2]["admin_password"] == SECRET
    expect(_card(page, "lab").locator(".rsrv-sso")).to_have_attribute("data-on", "0")


def test_the_rbac_chart_window_checks_then_installs_in_the_dock(ui):
    page, state = ui
    _card(page, "corp").locator('[data-rsrv="rbac"]').click()
    m = page.locator("#rsrv-rbac-modal")
    m.locator('[name="admin_user"]').fill("admin")
    m.locator('[name="admin_password"]').fill(SECRET)
    m.locator('button[type="submit"]').click()
    st = m.locator(".rsrv-rbac-status")
    expect(st.locator('[data-k="available"]')).to_have_text("109.0.0+up0.1.1")
    expect(st.locator('[data-k="version"]')).to_have_text("-")
    _no_secret_left(page)
    m.locator('[data-rsrv-rbac="install"]').click()
    expect(m.locator(".rsrv-rbac-progress")).not_to_be_empty()
    sent = [s for s in state["sent"] if s[1] == "/corp/rbac/install"]
    assert len(sent) == 1 and sent[0][2]["admin_password"] == SECRET and sent[0][2]["admin_user"] == "admin"
    expect(page.locator(f"#dock-card-{ACTION}")).to_have_count(1, timeout=5000)
    _no_secret_left(page)


def test_an_incompatible_rancher_cannot_install(ui):
    page, _ = ui
    page.route("**/api/rancher/servers/corp/rbac/status", lambda r, q: _json(r, {
        "installed": False, "version": "", "available_version": "109.0.0+up0.1.1",
        "compatible": False, "reason": "Rancher v2.13 is below 2.14.0"}))
    _card(page, "corp").locator('[data-rsrv="rbac"]').click()
    m = page.locator("#rsrv-rbac-modal")
    m.locator('[name="admin_user"]').fill("admin")
    m.locator('[name="admin_password"]').fill(SECRET)
    m.locator('button[type="submit"]').click()
    expect(m.locator(".rsrv-rbac-reason")).to_contain_text("below 2.14.0")
    expect(m.locator('[data-rsrv-rbac="install"]')).to_be_disabled()
