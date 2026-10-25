"""Imports et migrations (1.77.0, déplacé en 2.0.0) : l'ancienne section
« VM Import / Export » du menu est devenue l'entrée « Imports et migrations »
de la page d'un hôte Harvester (deux onglets, Import de VM puis VMware
migrations, hors des Paramètres) et la racine « Migrations » du menu des
hôtes (toutes les vagues). L'entrée du sous-menu ouvre le premier onglet,
l'hôte ouvert reste visible dans une famille repliée, et une famille se
replie puis se déplie."""
import re

from playwright.sync_api import expect

from nav import address, goto, settle


def test_imports_hold_the_two_tabs_and_migrations_is_a_root(page, flask_server):
    goto(page, flask_server["base_url"], address("vmimport"))
    head = page.locator("#host-page-head")
    tabs = head.locator(".hp-tabs a[data-hp-tab]").evaluate_all("els => els.map(e => e.dataset.hpTab)")
    assert tabs == ["vmimport", "forklift"]
    # aucun des deux dans les Paramètres de l'hôte, ni au premier niveau du menu
    page.evaluate("Router.go('#harvester/harv-fake/params/nodes')")
    settle(page)
    params = head.locator(".hp-tabs a[data-hp-tab]").evaluate_all("els => els.map(e => e.dataset.hpTab)")
    assert "vmimport" not in params and "forklift" not in params
    assert page.locator('#host-nav > a[href*="imports"]').count() == 0
    root = page.locator('#host-nav [data-hnav-root="migrations"]')
    expect(root).to_have_count(1)
    # chaque entrée a son info-bulle, traduite
    goto(page, flask_server["base_url"], address("forklift"))
    for el in head.locator(".hp-tabs a").all() + [root]:
        assert el.get_attribute("title")
    page.evaluate("i18n.setLang('fr')")
    expect(root).to_have_attribute("title", re.compile(r"\S"))
    assert root.get_attribute("title") == page.evaluate("i18n.t('hnav.migrationsTip')")
    page.evaluate("i18n.setLang('en')")


def test_imports_entry_opens_vm_import(page, flask_server):
    goto(page, flask_server["base_url"], address("overview"))
    page.locator('#host-page-head .hp-sub-link[data-hp-sub="imports"]').click()
    page.wait_for_function("() => location.hash === '#harvester/harv-fake/imports/vmimport'")
    settle(page)
    expect(page.locator("#tab-vmimport")).to_be_visible()


def test_a_folded_family_hides_even_the_open_host(context, flask_server):
    """2.0.1 (responsable produit, règle inversée) : repliée, la famille ne
    montre aucune ligne, pas même l'hôte ouvert ; dépliée, l'hôte ouvert et
    son entrée Imports et migrations sont là et actifs."""
    context.add_init_script("localStorage.setItem('harvester_ops_hnav_collapsed_harvester','1');")
    page = context.new_page()
    goto(page, flask_server["base_url"], address("forklift"))
    expect(page.locator('#host-nav .hnav-fam[data-family="harvester"]')).to_have_class(re.compile("collapsed"))
    expect(page.locator('#host-nav .hnav-host[data-key="harvester/harv-fake"]')).to_have_count(0)
    expect(page.locator("#hnav-families .hnav-sub")).to_have_count(0)
    page.locator('#host-nav .hnav-fam-head[data-fam="harvester"]').click()
    expect(page.locator('#host-nav .hnav-host[data-key="harvester/harv-fake"]')).to_be_visible()
    expect(page.locator('#hnav-families .hnav-sub a[data-sub="imports"]')).to_have_class(re.compile("active"))


def test_family_heads_still_fold(page, flask_server):
    # Relecture de la 1.77.0 : un clic sur la tête doit replier puis déplier
    goto(page, flask_server["base_url"], "#hosts")
    for fam in ("harvester", "kvm"):
        head = page.locator(f'#host-nav .hnav-fam-head[data-fam="{fam}"]')
        grp = lambda: page.locator(f'#host-nav .hnav-fam[data-family="{fam}"]')  # noqa: E731 (redessiné)
        before = "collapsed" in (grp().get_attribute("class") or "")
        head.click()
        assert ("collapsed" in (grp().get_attribute("class") or "")) is not before, fam
        page.locator(f'#host-nav .hnav-fam-head[data-fam="{fam}"]').click()
        assert ("collapsed" in (grp().get_attribute("class") or "")) is before, fam
