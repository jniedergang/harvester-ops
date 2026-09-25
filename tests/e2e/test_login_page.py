"""v1.50.0 : la page de connexion, dans un navigateur.

Le serveur de test n'a ni htpasswd ni Rancher configuré : la page propose
d'ouvrir la console. Ce qui est vérifié : la page se traduit, un refus de
Rancher se dit dans la langue de l'interface (le code reste visible pour le
support), et le bouton de déconnexion ne s'affiche que pour une session
ouverte par Rancher.
"""

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


@pytest.fixture
def fr(context):
    context.add_init_script("localStorage.setItem('harvester_ops_language','fr');")
    return context


def test_the_login_page_speaks_the_interface_language(fr, flask_server):
    page = fr.new_page()
    page.goto(flask_server["base_url"] + "/login")
    expect(page.locator(".login-intro")).to_contain_text("Connectez-vous pour continuer")
    expect(page.locator(".login-actions .btn")).to_have_count(1)       # rien de configuré
    expect(page).to_have_title("harvester-ops : connexion")


@pytest.mark.parametrize("code, text", [
    ("state-unknown", "a pris trop de temps"),
    ("access_denied", "Rancher a refusé la connexion"),
    ("rancher-unreachable", "Rancher est injoignable"),
    ("token-nonce", "n'a pas pu être vérifiée"),
])
def test_a_refusal_is_explained(fr, flask_server, code, text):
    page = fr.new_page()
    page.goto(flask_server["base_url"] + f"/login?error={code}")
    expect(page.locator(".login-error")).to_contain_text(text)
    expect(page.locator(".login-code")).to_have_text(code)


def test_no_logout_button_without_a_rancher_session(fr, flask_server):
    page = fr.new_page()
    page.goto(flask_server["base_url"] + "/")
    page.wait_for_function("window.i18n && document.body.classList.length >= 0")
    page.wait_for_timeout(800)
    expect(page.locator("#btn-logout")).to_be_hidden()
    assert page.locator("#btn-logout").get_attribute("data-tip-i18n") == "session.logoutTip"
