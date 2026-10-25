"""2.0.0 : les vues de tous les hôtes KVM, onglets de la famille KVM.

La 1.96-1.100 montrait sur un seul écran les tuiles de capacité de tous les
hôtes côte à côte, les pools et le catalogue d'images de tous les hôtes, et
leurs réseaux. Rien n'est perdu : `#hosts/kvm` porte les onglets Hôtes |
Capacité | Pools | Réseaux, chaque bloc d'hôte mène à la page de l'hôte, et
les anciens jetons `#kvmstorage` et `#kvmnetworks` y arrivent. API simulée
(fixture `ui` de test_kvm_196 : node2 joignable, ghost injoignable)."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from test_kvm_196 import ui  # noqa: E402,F401  (fixture)

ACTIVE = re.compile(r"\bactive\b")

# (adresse, section, onglet de la barre)
FLEET = [
    ("#hosts/kvm", "hosts", "hosts"),
    ("#hosts/kvm/capacity", "kvmhosts", "capacity"),
    ("#hosts/kvm/pools", "kvmstorage", "pools"),
    ("#hosts/kvm/networks", "kvmnetworks", "networks"),
]


def go(page, address):
    page.evaluate(f"Router.go({json.dumps(address)})")
    page.evaluate("Router.idle()")


def bar(page):
    return page.locator("#host-page-head .hp-kvm-fleet")


@pytest.mark.parametrize("address,section,tab", FLEET, ids=[f[0] for f in FLEET])
def test_each_fleet_tab_has_its_address_and_the_tab_bar(ui, address, section, tab):
    page, _, _ = ui
    go(page, address)
    expect(page.locator(f"#tab-{section}")).to_have_class(ACTIVE)
    tabs = bar(page).locator("[data-hp-tab]")
    assert tabs.evaluate_all("els => els.map(e => e.dataset.hpTab)") == ["hosts", "capacity", "pools", "networks"]
    expect(bar(page).locator(f'[data-hp-tab="{tab}"]')).to_have_class(ACTIVE)
    hrefs = tabs.evaluate_all("els => els.map(e => e.getAttribute('href'))")
    assert hrefs == ["#hosts/kvm", "#hosts/kvm/capacity", "#hosts/kvm/pools", "#hosts/kvm/networks"]
    for el in tabs.all():
        assert el.get_attribute("data-tip")


def test_capacity_shows_every_host_side_by_side_with_links(ui):
    page, _, _ = ui
    go(page, "#hosts/kvm/capacity")
    pane = page.locator("#tab-kvmhosts")
    expect(pane.locator("[data-kvm-host]")).to_have_count(2)
    tile = pane.locator('[data-kvm-host="node2"]')
    expect(tile).to_contain_text("28")
    expect(tile.locator(".kvm-host-facts")).to_be_visible()
    expect(pane.locator('[data-kvm-host="ghost"] .badge.fail')).to_be_visible()
    # le filtre d'étiquette et « Déclarer » de la 1.x sont là (aucun hôte imposé)
    expect(pane.locator('[data-kvm-filter="label"]')).to_be_visible()
    expect(pane.locator('[data-kvm="declare"]')).to_be_visible()
    link = tile.locator("a[data-kvm-open-host]")
    assert link.get_attribute("href") == "#kvm/node2"
    link.click()
    page.wait_for_function("() => location.hash === '#kvm/node2'")
    page.evaluate("Router.idle()")
    expect(bar(page)).to_have_count(0)                        # page d'hôte : pas la barre de flotte
    expect(page.locator("#host-page-head .hp-name")).to_have_text("node2")


def test_pools_and_networks_of_every_host_one_block_each(ui):
    page, state, _ = ui
    go(page, "#hosts/kvm/pools")
    pane = page.locator("#tab-kvmstorage")
    expect(pane.locator("[data-kvm-block]")).to_have_count(2)
    expect(pane.locator('[data-kvm-block="node2"] tr[data-kvm-pool="default"]')).to_contain_text("100.0 GiB")
    expect(pane.locator('[data-kvm-block="ghost"] .badge.fail')).to_have_text("unreachable")
    # filtre d'hôte et sous-onglet Images de la 1.x revenus
    expect(pane.locator('[data-kvm-filter="host"]')).to_be_visible()
    expect(pane.locator('[data-kvm="sub"][data-sub="images"]')).to_be_visible()
    assert pane.locator('[data-kvm-block="node2"] a[data-kvm-open-host]').get_attribute("href") == "#kvm/node2/params/pools"
    go(page, "#hosts/kvm/networks")
    pane = page.locator("#tab-kvmnetworks")
    expect(pane.locator("[data-kvm-block]")).to_have_count(2)
    expect(pane.locator('[data-kvm-block="node2"] tr[data-kvm-net="default"]')).to_contain_text("virbr0")
    link = pane.locator('[data-kvm-block="node2"] a[data-kvm-open-host]')
    assert link.get_attribute("href") == "#kvm/node2/params/networks"
    link.click()
    page.wait_for_function("() => location.hash === '#kvm/node2/params/networks'")
    page.evaluate("Router.idle()")
    expect(page.locator("#tab-kvmnetworks")).to_have_class(ACTIVE)
    expect(page.locator('#tab-kvmnetworks [data-kvm-block]')).to_have_count(1)      # hôte imposé
    expect(page.locator('#tab-kvmnetworks a[data-kvm-open-host]')).to_have_count(0)


@pytest.mark.parametrize("token,address,section", [
    ("#kvmstorage", "#hosts/kvm/pools", "kvmstorage"),
    ("#kvmnetworks", "#hosts/kvm/networks", "kvmnetworks"),
    ("#kvmhosts", "#hosts/kvm", "hosts"),
])
def test_old_tokens_open_the_fleet_views(ui, token, address, section):
    page, _, _ = ui
    page.goto(page.base_url + "/" + token, wait_until="domcontentloaded")
    page.wait_for_function(f"() => location.hash === {json.dumps(address)}")
    page.evaluate("Router.idle()")
    expect(page.locator(f"#tab-{section}")).to_have_class(ACTIVE)


def test_a_tab_click_is_a_real_link_and_f5_keeps_it(ui):
    page, _, _ = ui
    go(page, "#hosts/kvm")
    bar(page).locator('[data-hp-tab="pools"]').click()
    page.wait_for_function("() => location.hash === '#hosts/kvm/pools'")
    page.evaluate("Router.idle()")
    expect(page.locator("#tab-kvmstorage")).to_have_class(ACTIVE)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => !!(window.Router && Router.current())")
    page.evaluate("Router.idle()")
    expect(page.locator("#tab-kvmstorage")).to_have_class(ACTIVE)
    assert page.evaluate("location.hash") == "#hosts/kvm/pools"
    page.go_back()
    page.wait_for_function("() => location.hash === '#hosts/kvm'")


def test_the_harvester_family_index_has_no_kvm_tabs(ui):
    page, _, _ = ui
    go(page, "#hosts/harvester")
    expect(bar(page)).to_have_count(0)
    go(page, "#hosts")
    expect(bar(page)).to_have_count(0)
