"""v1.79.0 : la page de connexion avec plusieurs Rancher réglés.

Le modèle login.html est rendu par un petit serveur Flask propre au test,
avec le contexte que la console lui passe (`rancher_servers`, `rancher_last`),
pour exercer chaque cas sans dépendre d'un Rancher réel : aucun Rancher, un
seul (pas de liste), deux (liste, dernier choix présélectionné), un Rancher
injoignable, le SSO seul. Le compte local reste en bas, et la page marche sans
JavaScript (chaque Rancher garde son formulaire).
"""

import threading
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from flask import Flask, render_template  # noqa: E402
from playwright.sync_api import expect  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

WEB = Path(__file__).resolve().parent.parent.parent / "web"

LAB = {"id": "lab", "label": "Rancher lab", "sso": False, "direct": True, "unavailable": False,
       "providers": [{"id": "local", "label": "Local"}, {"id": "openldap", "label": "OpenLDAP"}]}
CORP = {"id": "corp", "label": "Rancher <corp>", "sso": True, "direct": True, "unavailable": False,
        "providers": [{"id": "local", "label": "Local"}]}
DOWN = {"id": "down", "label": "Rancher down", "sso": True, "direct": True, "unavailable": True,
        "providers": [{"id": "local", "label": "Local"}]}
SSO_ONLY = {"id": "sso", "label": "Rancher SSO", "sso": True, "direct": False, "unavailable": False,
            "providers": []}


@pytest.fixture(scope="module")
def login_server():
    """Rend login.html avec le contexte posé par chaque test."""
    app = Flask("login179", template_folder=str(WEB / "templates"), static_folder=str(WEB / "static"))
    ctx = {}

    @app.route("/login")
    def login():
        base = {"version": "test", "local": True, "next": "/", "username": "", "error": "",
                "error_kind": "", "signed_out": False, "rancher": False, "rancher_label": "Rancher",
                "rancher_servers": [], "rancher_last": ""}
        base.update(ctx)
        return render_template("login.html", **base)

    @app.route("/auth/rancher/<sid>/direct", methods=["POST"])
    def direct(sid):
        from flask import request
        ctx["posted"] = (sid, dict(request.form))
        return "ok"

    srv = make_server("127.0.0.1", 0, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield {"base_url": f"http://127.0.0.1:{srv.server_port}", "ctx": ctx}
    srv.shutdown()


def _open(context, login_server, **ctx):
    login_server["ctx"].clear()
    login_server["ctx"].update(ctx)
    page = context.new_page()
    page.goto(login_server["base_url"] + "/login")
    return page


def test_no_rancher_shows_only_the_local_account(context, login_server):
    page = _open(context, login_server)
    expect(page.locator(".login-rancher-box")).to_have_count(0)
    expect(page.locator(".login-local")).to_be_visible()
    expect(page.locator(".login-or-local")).to_have_count(0)


def test_one_rancher_needs_no_list(context, login_server):
    page = _open(context, login_server, rancher_servers=[LAB])
    expect(page.locator("#login-rancher-select")).to_have_count(0)
    form = page.locator(".login-rancher-direct")
    expect(form).to_be_visible()
    expect(form).to_have_attribute("action", "/auth/rancher/lab/direct")
    expect(form.locator('select[name="provider"] option')).to_have_count(2)
    expect(form.locator(".login-rancher-submit")).to_contain_text("Rancher lab")
    expect(page.locator(".login-rancher-sso")).to_have_count(0)
    # le compte local reste en bas, après la connexion par Rancher
    assert page.evaluate("""() => {
        const r = document.querySelector('.login-rancher-box'), l = document.querySelector('.login-local');
        return !!(r.compareDocumentPosition(l) & Node.DOCUMENT_POSITION_FOLLOWING); }""")
    expect(page.locator(".login-or-local")).to_be_visible()
    # le premier champ du Rancher prend le focus
    assert page.evaluate("document.activeElement.closest('.login-rancher-direct') !== null")
    # jamais d'adresse libre
    assert page.locator('input[name="url"], input[type="url"]').count() == 0


def test_the_direct_form_posts_provider_and_credentials(context, login_server):
    page = _open(context, login_server, rancher_servers=[LAB], next="/#vms")
    form = page.locator(".login-rancher-direct")
    form.locator('select[name="provider"]').select_option("openldap")
    form.locator('input[name="username"]').fill("alice")
    form.locator('input[name="password"]').fill("pw-179")
    form.locator(".login-rancher-submit").click()
    expect(page.locator("body")).to_have_text("ok")
    sid, posted = login_server["ctx"]["posted"]
    assert sid == "lab"
    assert posted == {"provider": "openldap", "username": "alice", "password": "pw-179", "next": "/#vms"}


def test_two_ranchers_offer_a_list_with_the_last_choice(context, login_server):
    page = _open(context, login_server, rancher_servers=[LAB, CORP], rancher_last="corp")
    sel = page.locator("#login-rancher-select")
    expect(sel).to_be_visible()
    expect(sel).to_have_value("corp")
    corp = page.locator('.login-rancher-server[data-server="corp"]')
    lab = page.locator('.login-rancher-server[data-server="lab"]')
    expect(corp).to_be_visible()
    expect(lab).to_be_hidden()
    # un seul fournisseur : pas de liste, un champ caché
    expect(corp.locator('select[name="provider"]')).to_have_count(0)
    expect(corp.locator('input[type="hidden"][name="provider"]')).to_have_value("local")
    # SSO activé : le bouton mène au Rancher choisi ; le nom est échappé
    sso = corp.locator(".login-rancher-sso")
    expect(sso).to_have_attribute("href", "/auth/rancher/corp/login")
    expect(sso).to_contain_text("Rancher <corp>")
    expect(corp.locator(".login-note")).to_be_visible()
    expect(lab.locator(".login-note")).to_have_count(0)
    sel.select_option("lab")
    expect(lab).to_be_visible()
    expect(corp).to_be_hidden()
    assert page.evaluate("document.activeElement.closest('[data-server=\"lab\"]') !== null")
    expect(page.locator(".login-local")).to_be_visible()


def test_without_javascript_every_rancher_keeps_its_form(browser, login_server):
    ctx = browser.new_context(java_script_enabled=False)
    try:
        page = _open(ctx, login_server, rancher_servers=[LAB, CORP], rancher_last="corp")
        expect(page.locator("#login-rancher-select")).to_be_hidden()
        expect(page.locator(".login-rancher-direct")).to_have_count(2)
        for f in page.locator(".login-rancher-direct").all():
            expect(f).to_be_visible()
        expect(page.locator(".login-rancher-name")).to_have_count(2)
    finally:
        ctx.close()


def test_an_unreachable_rancher_says_so(context, login_server):
    page = _open(context, login_server, rancher_servers=[DOWN, LAB], rancher_last="down")
    down = page.locator('.login-rancher-server[data-server="down"]')
    expect(down.locator(".login-unavailable")).to_be_visible()
    expect(down.locator("form, a")).to_have_count(0)


def test_sso_only_is_the_main_button(context, login_server):
    page = _open(context, login_server, rancher_servers=[SSO_ONLY])
    expect(page.locator(".login-rancher-direct")).to_have_count(0)
    btn = page.locator(".login-rancher-sso")
    assert "btn-primary" in btn.get_attribute("class")
    expect(btn).to_have_attribute("href", "/auth/rancher/sso/login")


@pytest.fixture
def fr(context):
    context.add_init_script("localStorage.setItem('harvester_ops_language','fr');")
    return context


def test_the_rancher_part_speaks_the_interface_language(fr, login_server):
    page = _open(fr, login_server, rancher_servers=[LAB, CORP], rancher_last="lab")
    expect(page.locator(".login-or-local")).to_have_text("ou avec un compte de cette console")
    expect(page.locator('.login-rancher-server[data-server="lab"] .login-rancher-submit')).to_have_text(
        "Se connecter avec Rancher lab")
    expect(page.locator('.login-rancher-server[data-server="corp"] .login-rancher-sso')).to_contain_text(
        "Authentification unique avec Rancher <corp>")
    expect(page.locator("#login-rancher-select")).to_have_attribute("title", "Le Rancher par lequel vous vous connectez. Votre dernier choix est retenu.")


def test_too_many_attempts_is_said(fr, login_server):
    page = _open(fr, login_server, rancher_servers=[LAB], error="too-many", error_kind="throttled")
    expect(page.locator(".login-error")).to_contain_text("Trop de tentatives")
    expect(page.locator(".login-code")).to_have_text("too-many")
