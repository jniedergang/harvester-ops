"""v1.46.0 : les bulles d'aide ne sont plus masquées.

Signalé par l'exploitant (capture) : la bulle d'un onglet de la fenêtre
« Migrer » passait sous la barre de titre de la fenêtre. Les bulles étaient
dessinées en CSS dans l'élément, toujours au-dessus, et tout conteneur qui
défile ou toute fenêtre flottante les coupait. Elles vivent désormais dans
un calque de <body>, au-dessus de tout, retournées sous l'élément quand la
place manque au-dessus.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


def fulfill(route, body):
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def page(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    p = context.new_page()
    p.route("**/api/clusters", lambda r, q: fulfill(r, {"clusters": [{"name": "harv-fake"}]}))
    p.route("**/api/vm/harv-fake/default/vm1/migrate-info",
            lambda r, q: fulfill(r, {"phase": "Running", "nodes": [], "migrations": []}))
    p.goto(flask_server["base_url"], wait_until="domcontentloaded")
    p.wait_for_function("window.VMMigrate && window.Tooltips && document.body.classList.contains('js-tips')")
    return p


def rect(page, sel):
    return page.evaluate(f"(() => {{ const r = document.querySelector({json.dumps(sel)}).getBoundingClientRect();"
                         " return {top: r.top, bottom: r.bottom, left: r.left, right: r.right}; })()")


def test_a_tab_tooltip_is_above_the_floating_window(page):
    page.evaluate("VMMigrate.open('harv-fake', 'default', 'vm1')")
    tab = page.locator('#fp-vm-migrate-harv-fake-default-vm1 [data-dest="file"]')
    expect(tab).to_be_visible(timeout=5000)
    tab.hover()
    layer = page.locator(".tip-layer")
    expect(layer).to_be_visible()
    expect(layer).to_contain_text("Export the VM")
    # dans <body>, au-dessus de tous les calques de la console
    assert page.evaluate("document.querySelector('.tip-layer').parentElement === document.body")
    z_tip = int(page.evaluate("getComputedStyle(document.querySelector('.tip-layer')).zIndex"))
    z_panel = int(page.evaluate("getComputedStyle(document.querySelector('#fp-vm-migrate-harv-fake-default-vm1')).zIndex") or 0)
    assert z_tip > z_panel
    t = rect(page, ".tip-layer")
    vw = page.evaluate("window.innerWidth")
    assert t["top"] >= 0 and t["left"] >= 0 and t["right"] <= vw
    # le pseudo-élément CSS n'est plus affiché
    assert page.evaluate("getComputedStyle(document.querySelector("
                         "'#fp-vm-migrate-harv-fake-default-vm1 [data-dest=\"file\"]'), '::after').display") == "none"


def test_a_tooltip_near_the_top_goes_below(page):
    page.evaluate("""() => {
        const b = document.createElement('button');
        b.className = 'tip'; b.id = 'probe'; b.textContent = 'x';
        b.setAttribute('data-tip', 'A long enough explanation to have some height.');
        b.style.cssText = 'position:fixed;top:2px;left:2px;z-index:20000';
        document.body.appendChild(b);
    }""")
    page.locator("#probe").hover()
    expect(page.locator(".tip-layer")).to_be_visible()
    t, b = rect(page, ".tip-layer"), rect(page, "#probe")
    assert t["top"] >= b["bottom"], (t, b)
    assert t["left"] >= 0


def test_tooltips_off_hides_the_layer(page):
    page.evaluate("document.body.classList.add('no-tooltips')")
    page.evaluate("VMMigrate.open('harv-fake', 'default', 'vm1')")
    tab = page.locator('#fp-vm-migrate-harv-fake-default-vm1 [data-dest="file"]')
    expect(tab).to_be_visible(timeout=5000)
    tab.hover()
    page.wait_for_timeout(300)
    assert not page.locator(".tip-layer").is_visible()


def test_a_missing_transfer_module_does_not_leave_a_blank_tab(context, flask_server):
    """Vécu : une console redémarrée à moitié (page en cache, scripts neufs)
    laissait les onglets « Un autre cluster » et « Un fichier » tout blancs."""
    context.add_init_script("localStorage.setItem('harvester_ops_language','fr');")
    p = context.new_page()
    p.route("**/static/js/vm-transfer.js*", lambda r, q: r.fulfill(status=404, body=""))
    p.route("**/api/vm/harv-fake/default/vm1/migrate-info",
            lambda r, q: fulfill(r, {"phase": "Running", "nodes": [], "migrations": []}))
    p.goto(flask_server["base_url"], wait_until="domcontentloaded")
    p.wait_for_function("window.VMMigrate")
    p.evaluate("VMMigrate.open('harv-fake', 'default', 'vm1', 'cluster')")
    expect(p.locator('#fp-vm-migrate-harv-fake-default-vm1 [data-pane="cluster"]')).to_contain_text(
        "rechargez la page", timeout=5000)
