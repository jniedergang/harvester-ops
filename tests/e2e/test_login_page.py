"""v1.50.0 : la page de connexion, dans un navigateur.

Le serveur de test n'a ni htpasswd ni Rancher configuré : la page propose
d'ouvrir la console. Ce qui est vérifié : la page se traduit, un refus de
Rancher se dit dans la langue de l'interface (le code reste visible pour le
support), le menu du compte sans déconnexion sur une console ouverte, et la
page qui confirme la déconnexion.
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


def test_an_open_console_offers_no_sign_out(fr, flask_server):
    """v1.56.0 : la déconnexion vit dans le menu du compte, en haut à droite.
    Sur une console ouverte (ni htpasswd ni Rancher) il n'y a rien dont se
    déconnecter, et le menu le dit au lieu d'offrir un bouton sans effet."""
    page = fr.new_page()
    page.goto(flask_server["base_url"] + "/")
    page.wait_for_function("window.UserMenu && window.i18n")
    page.wait_for_timeout(800)
    assert page.locator("#btn-logout").count() == 0          # l'ancien bouton du menu latéral
    btn = page.locator("#btn-user")
    assert btn.get_attribute("data-tip-i18n") == "user.menuTip"
    btn.click()
    menu = page.locator("#user-menu")
    expect(menu).to_be_visible()
    expect(menu.locator(".um-auth")).to_contain_text("Console ouverte")
    expect(menu.locator("#user-signout")).to_have_count(0)
    expect(menu.locator(".um-note")).to_contain_text("rien dont se déconnecter")
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()


def test_the_login_page_says_you_are_signed_out(fr, flask_server):
    page = fr.new_page()
    page.goto(flask_server["base_url"] + "/login?signed_out=1")
    expect(page.locator(".login-signed-out")).to_have_text("Vous êtes déconnecté.")
