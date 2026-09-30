"""Section « VM Import / Export » du menu (1.77.0) : VM Import, VMware
migrations et Migrations (all clusters) regroupées, dans cet ordre, hors du
groupe Cluster ; la tête du groupe ouvre la première entrée."""
from playwright.sync_api import expect


def test_vmio_group_holds_the_three_entries(page):
    group = page.locator("#tab-group-vmio")
    expect(group).to_have_count(1)
    tabs = group.locator(".tab-child").evaluate_all("els => els.map(e => e.dataset.tab)")
    assert tabs == ["vmimport", "forklift", "forkliftglobal"]
    for tab in tabs:
        assert page.locator(f'#tab-group-cluster .tab[data-tab="{tab}"]').count() == 0
        assert page.locator(f'nav > .tab[data-tab="{tab}"]').count() == 0
    # chaque entrée a son info-bulle
    for el in group.locator(".tab").all():
        assert el.get_attribute("title")


def test_vmio_head_opens_vm_import(page):
    page.locator('.tab-group-head[data-group="vmio"]').click()
    expect(page.locator("#tab-vmimport")).to_be_visible()


def test_active_entry_opens_its_folded_group(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_group_vmio_expanded','0');"
        "localStorage.setItem('harvester_ops_current_tab','forklift');")
    page = context.new_page()
    page.goto(flask_server["base_url"])
    page.wait_for_load_state("networkidle")
    expect(page.locator('#tab-group-vmio .tab[data-tab="forklift"]')).to_be_visible()
