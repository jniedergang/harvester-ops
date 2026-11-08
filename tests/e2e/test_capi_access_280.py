"""2.8.0 (tâche 9) : accès humain aux clusters enfants, dans un navigateur.

* création : menu Accès (aucun, Rancher intégré déconseillé, Rancher
  externe grisé sans session Rancher, bulle qui le dit), OIDC (préfixe vide
  refusé à la saisie, sans attendre le serveur), sans Internet ;
* conflit Turtles signalé au menu Accès (constat du serveur) ;
* modèle de cluster : options d'accès relues et rendues ;
* fenêtre Accès d'un cluster enfant : import grisé sans session, import
  lancé avec la session (action suivie), kubeconfig OIDC téléchargé sans
  identifiant, membres du cluster enfant dans Rancher ;
* noms hostiles échappés.

Routes simulées : aucun cluster ni Rancher n'est interrogé.
"""

import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_auto_tabs_220 as t220  # noqa: E402
import test_capi_create_view as tcv  # noqa: E402

HOSTILE = '<img src=x onerror="window.__pwned=1">'
fulfill = t220.fulfill
SESSION = "https://rancher.example"
DIAG = {"components": [], "capi_clusters": [
    {"namespace": "apps", "name": "apps", "phase": "Provisioned", "ready": True, "clusterClass": "apps", "k8sVersion": "v1.34.11"}]}
OIDC = {"issuer_url": "https://idp.example/realms/main", "audience": "kubernetes", "username_prefix": "oidc:",
        "groups_prefix": "oidc:", "admin_group": "k8s-admins"}
KUBECONFIG = ("apiVersion: v1\nkind: Config\nclusters:\n- name: apps\n  cluster: {server: 'https://172.16.3.200:6443'}\n"
              "users:\n- name: oidc\n  user:\n    exec: {apiVersion: client.authentication.k8s.io/v1, command: kubectl,"
              " args: [oidc-login, get-token]}\n")


def sse(events):
    return "".join(f"event: {t}\ndata: {json.dumps(d)}\n\n" for t, d in events)


# --- fenêtre de création -------------------------------------------------------------

from test_capi_create_view import form  # noqa: E402,F401  (fixture de la fenêtre de création)


@pytest.fixture
def create(context, form):
    def make(session=None, check=None):
        context.route(re.compile(r".*/api/capi/harv-fake/rancher-status$"),
                      lambda r, q: fulfill(r, {"session": session, "clusters": []}))
        return form(check=check)
    return make


def menu(host, sec):
    tcv.menu(host, sec)


def test_external_rancher_is_greyed_without_a_session_and_says_why(create):
    page, host, calls = create(session=None)
    menu(host, "access")
    ext = host.locator('[data-access="external"]')
    expect(ext).to_be_disabled(timeout=5000)
    wrap = host.locator('[data-access-wrap="external"]')
    expect(wrap).to_have_attribute("data-tip", re.compile("compte local"))
    expect(host.locator('[data-x-wrap="rancher-note"]')).to_contain_text("compte local")
    expect(host.locator('[data-access="none"]')).to_be_checked()
    # les trois modes s'expliquent au survol
    for m in ("none", "turtles", "external"):
        assert host.locator(f'[data-access-wrap="{m}"]').get_attribute("data-tip")


def test_external_rancher_with_a_session_reaches_the_request(create):
    page, host, calls = create(session=SESSION)
    tcv.x(host, "name").fill("web")
    menu(host, "access")
    ext = host.locator('[data-access="external"]')
    expect(ext).to_be_enabled(timeout=5000)
    expect(host.locator('[data-access-wrap="external"]')).to_have_attribute("data-tip", re.compile("rancher.example"))
    ext.check()
    page.wait_for_timeout(900)
    body = calls["check"][-1]
    assert body["rancher_external"] is True and body["rancher_import"] is False
    assert "oidc" not in body and "offline_artifacts" not in body


def test_turtles_conflict_is_signalled_on_the_access_menu(create):
    page, host, calls = create(session=SESSION, check={"blocked": True, "findings": [
        {"code": "rancher-import-conflict", "level": "block", "facts": {}}]})
    tcv.x(host, "name").fill("web")
    menu(host, "check")
    expect(tcv.x(host, "report")).to_contain_text("Turtles", timeout=5000)
    count = host.locator('[data-count="access"]')
    expect(count).to_have_text("1")
    expect(count).to_have_class(re.compile("is-block"))
    expect(tcv.x(host, "create")).to_be_disabled()


def test_an_empty_prefix_is_refused_while_typing(create):
    page, host, calls = create(session=None)
    tcv.x(host, "name").fill("web")
    menu(host, "access")
    host.locator("[data-o-on]").check()
    host.locator('[data-o="issuer_url"]').fill("https://idp.example/realms/main")
    host.locator('[data-o="audience"]').fill("kubernetes")
    page.wait_for_timeout(900)
    before = len(calls["check"])
    assert calls["check"][-1]["oidc"]["username_prefix"] == "oidc:"
    pre = host.locator('[data-o="username_prefix"]')
    pre.fill("")
    err = host.locator('[data-o-err="username_prefix"]')
    expect(err).to_be_visible()
    expect(err).to_contain_text("username_prefix")
    expect(pre).to_have_attribute("aria-invalid", "true")
    pre.dispatch_event("change")
    page.wait_for_timeout(900)
    # refusé sans attendre le serveur : aucune demande avec un préfixe vide
    assert len(calls["check"]) == before
    expect(tcv.x(host, "create")).to_be_disabled()
    expect(host.locator('[data-count="access"]')).to_have_text("1")
    pre.fill("system:")
    expect(err).to_contain_text("system:")
    pre.fill("corp:")
    expect(err).to_be_hidden()
    pre.dispatch_event("change")
    page.wait_for_timeout(900)
    assert calls["check"][-1]["oidc"] == {"issuer_url": "https://idp.example/realms/main", "audience": "kubernetes",
                                          "username_claim": "email", "username_prefix": "corp:",
                                          "groups_claim": "groups", "groups_prefix": "oidc:"}


def test_offline_shows_its_mode_and_reaches_the_request(create):
    page, host, calls = create(session=None)
    tcv.x(host, "name").fill("web")
    menu(host, "registries")
    expect(host.locator('[data-x-wrap="offline_artifacts"]')).to_be_hidden()
    tcv.x(host, "offline").check()
    expect(host.locator('[data-x-wrap="offline_artifacts"]')).to_be_visible()
    tcv.x(host, "offline_artifacts").select_option("image")
    page.wait_for_timeout(900)
    body = calls["check"][-1]
    assert body["offline"] is True and body["offline_artifacts"] == "image"
    # 2.8.1 (relecture I-2) : dépôts de l'image gardés ou mis de côté
    expect(host.locator('[data-x-wrap="offline_packages"]')).to_be_visible()
    assert body["offline_packages"] == "skip"
    tcv.x(host, "offline_packages").select_option("keep")
    page.wait_for_timeout(900)
    assert calls["check"][-1]["offline_packages"] == "keep"
    tcv.x(host, "offline").uncheck()
    expect(host.locator('[data-x-wrap="offline_packages"]')).to_be_hidden()
    page.wait_for_timeout(900)
    assert "offline_packages" not in calls["check"][-1] and "offline_artifacts" not in calls["check"][-1]


def test_the_template_editor_reads_and_gives_back_the_access_options(context, flask_server):
    t220.open_auto(context, flask_server, "#automation/capi")
    page = context.pages[-1]
    page.wait_for_function("window.CapiCreate")
    out = page.evaluate("""() => {
      const box = document.createElement('div'); document.body.appendChild(box);
      const spec = { k8s_version: 'v1.34.11', rancher_external: true, offline: true, offline_artifacts: 'fetch',
                     offline_packages: 'keep',
                     oidc: { issuer_url: 'https://{{idp}}/realms/main', audience: 'k8s', username_prefix: 'oidc:',
                             groups_prefix: 'corp:', admin_group: 'ops' } };
      const f = CapiCreate.templateForm(box, {}, '', spec);
      const ext = box.querySelector('[data-access="external"]');
      return { read: f.read(), extDisabled: ext.disabled, extChecked: ext.checked,
               modeVisible: !box.querySelector('[data-x-wrap="offline_artifacts"]').hidden };
    }""")
    assert out["extChecked"] and not out["extDisabled"]          # un modèle ne lit pas la session
    assert out["modeVisible"]
    r = out["read"]
    assert r["rancher_external"] is True and r["rancher_import"] is False and r["offline"] is True
    assert r["offline_packages"] == "keep"
    assert r["oidc"]["issuer_url"] == "https://{{idp}}/realms/main" and r["oidc"]["groups_prefix"] == "corp:"
    assert r["oidc"]["admin_group"] == "ops"


# --- fenêtre Accès d'un cluster enfant ------------------------------------------------

def open_access(context, flask_server, calls, session=None, row=None, oidc=OIDC, role="operator"):
    context.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(
        r, {"role": role, "roles_active": True, "user": "u", "auth": "local"}))
    page = t220.open_auto(context, flask_server, "#automation/capi", diag=DIAG)
    rows = [row] if row else []
    page.route(re.compile(r".*/api/capi/harv-fake/rancher-status$"),
               lambda r, q: fulfill(r, {"session": session, "clusters": rows}))
    page.route(re.compile(r".*/api/capi/harv-fake/cluster/apps/apps/details$"),
               lambda r, q: fulfill(r, {"name": "apps", "namespace": "apps", "oidc": oidc, "machines": [], "conditions": []}))

    def imp(route, req):
        calls.setdefault("import", []).append((req.method, json.loads(req.post_data or "{}")))
        fulfill(route, {"action_id": "acc000000001", "cluster": "apps/apps"}, 202)
    page.route(re.compile(r".*/api/capi/harv-fake/rancher-import(/apps/apps)?$"), imp)
    page.route(re.compile(r".*/api/stream/acc000000001$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body=sse([
            ("step", {"step_id": "import", "status": "running", "message": "waiting for Rancher (Active)"}),
            ("end", {"status": "done"})])))
    page.route(re.compile(r".*/api/capi/harv-fake/oidc-kubeconfig/apps/apps$"),
               lambda r, q: (calls.setdefault("kubeconfig", []).append(1),
                             fulfill(r, {"kubeconfig": KUBECONFIG, "issuer_url": OIDC["issuer_url"]})))

    def members(route, req):
        calls.setdefault("members", []).append(req.url)
        fulfill(route, {"managed": True, "scope": "cluster", "roles": [{"id": "cluster-owner", "name": "Cluster Owner"}],
                        "members": [{"id": "crtb-1", "name": HOSTILE, "kind": "user", "provider": "local",
                                     "role_name": "Cluster Owner", "system": False}]})
    page.route(re.compile(r".*/api/rancher-members/harv-fake\?.*"), members)
    page.evaluate("window.confirm = () => true")
    btn = page.locator('.capi-cluster-access[data-name="apps"]')
    expect(btn).to_be_visible(timeout=5000)
    assert btn.get_attribute("data-tip")
    btn.click()
    win = page.locator("#fp-capi-access-apps-apps")
    expect(win).to_be_visible()
    return page, win


def test_import_is_greyed_without_a_session(context, flask_server):
    calls = {}
    page, win = open_access(context, flask_server, calls, session=None)
    pane = win.locator('[data-win-pane="rancher"]')
    expect(pane.locator('[data-x="import"]')).to_be_disabled(timeout=5000)
    expect(pane.locator('[data-x="import-wrap"]')).to_have_attribute("data-tip", re.compile("local account"))
    expect(pane.locator('[data-x="unimport"]')).to_be_disabled()
    win.locator('[data-win-tab="members"]').click()
    expect(win.locator('[data-x="no-members"]')).to_contain_text("local account")
    assert "import" not in calls and "members" not in calls


def test_import_with_a_session_is_followed(context, flask_server):
    calls = {}
    page, win = open_access(context, flask_server, calls, session=SESSION)
    pane = win.locator('[data-win-pane="rancher"]')
    go = pane.locator('[data-x="import"]')
    expect(go).to_be_enabled(timeout=5000)
    go.click()
    expect(pane.locator('[data-x="line"]')).to_contain_text("acc000000001", timeout=5000)
    assert calls["import"][0] == ("POST", {"cluster": "apps/apps"})


def test_oidc_kubeconfig_is_downloaded_without_credentials(context, flask_server):
    calls = {}
    page, win = open_access(context, flask_server, calls, session=None)
    win.locator('[data-win-tab="oidc"]').click()
    pane = win.locator('[data-win-pane="oidc"]')
    expect(pane).to_contain_text("idp.example")
    expect(win.locator('[data-win-pane="rancher"]')).to_be_hidden()     # un onglet à la fois
    with page.expect_download() as dl:
        pane.locator('[data-x="dl"]').click()
    d = dl.value
    assert d.suggested_filename == "apps-oidc.kubeconfig"
    text = Path(d.path()).read_text()
    assert "oidc-login" in text
    for word in ("token:", "password", "client-key", "client-secret", "client-certificate-data"):
        assert word not in text
    # jamais affiché dans la fenêtre
    assert "oidc-login get-token" not in win.inner_html()
    expect(pane.locator('[data-x="dl-line"]')).to_contain_text("apps-oidc.kubeconfig")


def test_no_oidc_says_how_to_add_it(context, flask_server):
    calls = {}
    page, win = open_access(context, flask_server, calls, oidc=None)
    win.locator('[data-win-tab="oidc"]').click()
    expect(win.locator('[data-x="no-oidc"]')).to_contain_text("Renew")
    assert win.locator('[data-x="dl"]').count() == 0


def test_members_of_the_imported_child_and_hostile_names_escaped(context, flask_server):
    calls = {}
    row = {"namespace": "apps", "cluster": "apps", "imported": True, "turtles": False, "rancher": SESSION,
           "name": "harv-fake-apps", "cid": "c-m-abc123", "state": HOSTILE, "ready": False, "present": True,
           "origin": {"run": "0123456789ab"}}
    page, win = open_access(context, flask_server, calls, session=SESSION, row=row)
    pane = win.locator('[data-win-pane="rancher"]')
    expect(pane).to_contain_text("c-m-abc123", timeout=5000)
    expect(pane.locator('[data-x="import"]')).to_be_disabled()
    expect(pane.locator('[data-x="unimport"]')).to_be_enabled()
    expect(pane.locator('a[href="#activity/action/0123456789ab"]')).to_be_visible()
    win.locator('[data-win-tab="members"]').click()
    expect(win.locator(".rm-table")).to_be_visible(timeout=5000)
    assert "child=apps%2Fapps" in calls["members"][0] and "scope=cluster" in calls["members"][0]
    assert page.evaluate("window.__pwned") is None
    assert win.locator("img[src=x]").count() == 0
    assert all(t.get_attribute("data-tip") for t in win.locator(".tip").all())
    win.locator('[data-win-tab="rancher"]').click()
    pane.locator('[data-x="unimport"]').click()
    expect(pane.locator('[data-x="line"]')).to_contain_text("acc000000001", timeout=5000)
    assert calls["import"][-1][0] == "DELETE"
