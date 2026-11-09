"""2.10.0 (tâche 10) : carte « External Secrets » de Paramètres > Secrets, sur
routes simulées (aucun OpenBao interrogé).

* B1 : écouteur (adresse, allumé/éteint), enregistrer l'adresse, l'éteindre
  (grisé tant que des clusters enfants l'utilisent), port et pare-feu ;
* clusters enfants : clés changées (badge), « Mettre à jour les clés » ;
* mode A : le texte qui dit que l'administrateur d'OpenBao lance les
  commandes ; pas d'écouteur ;
* noms hostiles échappés.
"""

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_auto_tabs_220 as t220  # noqa: E402
import test_settings_secrets_290 as t290  # noqa: E402

fulfill = t220.fulfill
HOSTILE = t290.HOSTILE


def cluster(**kw):
    c = {"key": "harv-fake-apps-apps", "harvester": "harv-fake", "namespace": "apps", "cluster": "apps",
         "mount": "eso-x", "keys_sha256": "0123456789abcdef" * 4, "namespaces": ["equipe-a", "equipe-b"],
         "node_cidr": "172.16.2.0/24", "applied": True, "at": 1794100000, "by": HOSTILE, "keys_changed": False}
    c.update(kw)
    return c


def open_card(context, flask_server, eso, mode="b1"):
    st = {"status": t290.status_doc(mode=mode)}
    page = t290.open_secrets(context, flask_server, st)
    page.route(re.compile(r".*/api/secrets/eso$"), lambda r, q: fulfill(r, eso))
    body = t290.to_tab(page)
    card = body.locator(".sx-eso")
    expect(card).to_be_visible(timeout=5000)
    return page, card, st


def test_b1_card_listener_clusters_and_keys_changed(context, flask_server):
    eso = {"mode": "b1", "active": True, "listener": "172.16.1.11:8212", "address": "https://172.16.1.11:8212",
           "port": 8212, "clusters": [cluster(), cluster(key="harv-fake-web-web", namespace="web", cluster="web",
                                                         keys_changed=True, applied=False)]}
    page, card, st = open_card(context, flask_server, eso)
    expect(card.locator('[data-sx-eso-listener="on"]')).to_be_visible()
    expect(card).to_contain_text("172.16.1.11:8212")
    expect(card.locator(".sx-eso-firewall")).to_contain_text("8212/tcp")
    expect(card.locator(".sx-eso-firewall")).to_contain_text("never touches the firewall")
    rows = card.locator(".sx-eso-cluster")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("equipe-a, equipe-b")
    expect(rows.nth(0)).to_contain_text(HOSTILE)                        # texte, pas balise
    expect(rows.nth(0).locator("[data-sx-eso-changed]")).to_have_count(0)
    expect(rows.nth(1).locator("[data-sx-eso-changed]")).to_contain_text("keys changed")
    assert page.evaluate("window.__pwned") is None and card.locator("img").count() == 0
    # éteindre : grisé tant que des clusters enfants l'utilisent
    off = card.locator('[data-sx="eso-listener-off"]')
    expect(off).to_be_disabled()
    expect(off).to_have_attribute("data-tip", re.compile("still use the listener"))
    for b in card.locator("button").all():
        assert b.get_attribute("data-tip"), b.inner_text()
    rows.nth(1).locator("[data-sx-eso-keys]").click()
    expect(page.locator("#sx-feedback")).to_contain_text("Keys updated", timeout=5000)
    assert ("eso/keys", {"cluster": "harv-fake/web/web"}) in st["calls"]


def test_b1_save_address_and_turn_off(context, flask_server):
    eso = {"mode": "b1", "active": True, "listener": None, "address": None, "port": 8212, "clusters": []}
    page, card, st = open_card(context, flask_server, eso)
    expect(card.locator('[data-sx-eso-listener="off"]')).to_be_visible()
    expect(card.locator('[data-sx="eso-listener-off"]')).to_be_disabled()    # déjà éteint
    expect(card).to_contain_text("No child cluster uses External Secrets yet")
    card.locator("[data-sx-eso-addr]").fill("172.16.1.11:8212")
    card.locator('[data-sx="eso-listener-save"]').click()
    expect(page.locator("#sx-feedback")).to_contain_text("Listener address saved", timeout=5000)
    assert ("eso/listener", {"address": "172.16.1.11:8212"}) in st["calls"]
    # allumé, sans cluster enfant : on peut l'éteindre
    eso["listener"] = "172.16.1.11:8212"
    page.unroute(re.compile(r".*/api/secrets/eso$"))
    page.route(re.compile(r".*/api/secrets/eso$"), lambda r, q: fulfill(r, eso))
    page.locator('#secrets-settings-body [data-sx-tab="status"]').click()
    off = page.locator('#secrets-settings-body .sx-eso [data-sx="eso-listener-off"]')
    expect(off).to_be_enabled(timeout=5000)
    off.click()                                                          # confirm() accepté par le banc
    expect(page.locator("#sx-feedback")).to_contain_text("Listener turned off", timeout=5000)
    assert ("eso/listener", {"off": True}) in st["calls"]


def test_mode_a_explains_who_runs_the_commands(context, flask_server):
    eso = {"mode": "a", "active": True, "listener": None, "address": None, "port": 8212,
           "clusters": [cluster(applied=False)]}
    page, card, _ = open_card(context, flask_server, eso, mode="a")
    expect(card.locator(".sx-eso-mode-a")).to_contain_text("administrator runs")
    expect(card.locator("[data-sx-eso-addr]")).to_have_count(0)
    expect(card.locator(".sx-eso-cluster")).to_contain_text("waiting")


def test_no_card_when_openbao_is_not_active(context, flask_server):
    st = {"status": t290.status_doc(mode="local", active=False, migrated={}, warnings=[])}
    page = t290.open_secrets(context, flask_server, st)
    asked = []
    page.route(re.compile(r".*/api/secrets/eso$"), lambda r, q: (asked.append(1), fulfill(r, {}))[1])
    body = t290.to_tab(page)
    expect(body.locator('[data-sx="enable"]')).to_be_visible()
    assert body.locator(".sx-eso").count() == 0 and asked == []


SHOTS = Path("/tmp/bmpost/shots-2100")


@pytest.mark.parametrize("size", [(1440, 900), (390, 844)])
def test_screenshots_desktop_and_narrow(context, flask_server, size):
    eso = {"mode": "b1", "active": True, "listener": "172.16.1.11:8212", "address": "https://172.16.1.11:8212",
           "port": 8212, "clusters": [cluster(by="alice"), cluster(key="harv-fake-web-web", namespace="web", cluster="web",
                                                                   keys_changed=True, applied=False, by="bob")]}
    page, card, _ = open_card(context, flask_server, eso)
    page.set_viewport_size({"width": size[0], "height": size[1]})
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.add_style_tag(content="#bottom-dock { display: none !important; }")
    card.scroll_into_view_if_needed()
    page.screenshot(path=str(SHOTS / f"settings-eso-{size[0]}.png"))
    card.screenshot(path=str(SHOTS / f"settings-eso-card-{size[0]}.png"))
