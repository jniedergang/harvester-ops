"""2.0.0 : l'adresse ouverte avant la connexion est rouverte après.

Un lien `#harvester/harv-fake/params/storage/images` envoyé à quelqu'un qui
n'est pas connecté : la page de connexion s'ouvre, puis, la connexion faite,
l'écran demandé (et pas l'écran par défaut). Une adresse que la grammaire ne
comprend pas est oubliée, jamais écrite dans la page."""

import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import settle  # noqa: E402
from test_user_menu_and_versions import local_server  # noqa: E402,F401  (fixture)


def sign_in(page):
    page.fill('.login-form [name="username"]', "alice")
    page.fill('.login-form [name="password"]', "wonderland")
    page.click(".login-form .login-submit")


@pytest.mark.parametrize("address,tab,final", [
    ("#harvester/harv-fake/params/storage/images", "tab-storage", "#harvester/harv-fake/params/storage/images"),
    ("#storage", "tab-storage", None),                 # ancien jeton, traduit après connexion
    ("#activity", "tab-activity", "#activity"),
])
def test_the_address_survives_the_login(browser, local_server, address, tab, final):
    ctx = browser.new_context()
    ctx.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = ctx.new_page()
    page.goto(local_server + "/" + address)
    page.wait_for_url(re.compile(r"/login"))
    assert page.evaluate("sessionStorage.getItem('harvester_ops_after_login')") == address
    sign_in(page)
    page.wait_for_url(re.compile(re.escape(local_server) + r"/(#.*)?$"))
    settle(page)
    expect(page.locator(f"#{tab}")).to_have_class("tab-content active")
    if final:
        assert page.evaluate("location.hash") == final
    else:
        assert page.evaluate("location.hash").startswith("#harvester/harv-fake/params/storage")
    assert page.evaluate("sessionStorage.getItem('harvester_ops_after_login')") is None
    ctx.close()


def test_an_unknown_or_crafted_address_is_forgotten(browser, local_server):
    ctx = browser.new_context()
    ctx.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = ctx.new_page()
    page.goto(local_server + "/#harvester/<img src=x onerror=window.__xss=1>")
    page.wait_for_url(re.compile(r"/login"))
    assert page.evaluate("sessionStorage.getItem('harvester_ops_after_login')") is None
    assert page.locator("img[src=x]").count() == 0
    sign_in(page)
    page.wait_for_url(re.compile(re.escape(local_server) + r"/(#.*)?$"))
    settle(page)
    assert page.evaluate("location.hash") in ("#vms", "#harvester/harv-fake")   # 2.0.0 : #vms par défaut
    assert page.evaluate("window.__xss") is None
    ctx.close()


def test_a_plain_login_page_forgets_an_old_address(browser, local_server):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(local_server + "/#vms")
    page.wait_for_url(re.compile(r"/login"))
    page.goto(local_server + "/login")             # on revient plus tard, sans lien
    assert page.evaluate("sessionStorage.getItem('harvester_ops_after_login')") is None
    ctx.close()
