"""v1.45.0 : la barre d'actions groupées de la vue Machines virtuelles.

Signalé par l'exploitant (capture) : sur le fond d'accent, les options du
menu « Set strategy » s'écrivaient en blanc sur la liste blanche du
navigateur, illisibles, et le bouton principal se fondait dans la barre.
On mesure les contrastes pour de bon, dans les deux thèmes.
"""

import pytest

pytest.importorskip("playwright")

CONTRAST = """
(el) => {
  const rgb = (c) => (c.match(/\\d+(\\.\\d+)?/g) || []).slice(0, 3).map(Number);
  const lum = ([r, g, b]) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const bgOf = (n) => {
    // une option sans fond propre s'affiche dans la liste native du
    // navigateur, blanche : c'est là que ses lettres blanches disparaissaient
    if (n.tagName === 'OPTION') {
      const own = getComputedStyle(n).backgroundColor;
      return /rgba\(\s*0,\s*0,\s*0,\s*0\s*\)|transparent/.test(own) ? 'rgb(255,255,255)' : own;
    }
    while (n) {
      const b = getComputedStyle(n).backgroundColor;
      if (b && !/rgba\\(\\s*0,\\s*0,\\s*0,\\s*0\\s*\\)|transparent/.test(b)) return b;
      n = n.parentElement;
    }
    return 'rgb(255,255,255)';
  };
  const a = lum(rgb(getComputedStyle(el).color)), b = lum(rgb(bgOf(el)));
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}
"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_bulk_toolbar_is_readable(context, flask_server, theme):
    context.add_init_script(f"document.documentElement.setAttribute('data-theme','{theme}');"
                            f"localStorage.setItem('harvester_ops_theme','{theme}');")
    page = context.new_page()
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    page.evaluate("document.getElementById('bulk-toolbar').style.display='flex'")
    page.evaluate("document.querySelector('[data-section=\"namespaces\"], #section-namespaces')"
                  "?.classList.add('active')")
    for sel in ("#bulk-strategy-select option[value='RerunOnFailure']",
                "#bulk-strategy-select option[value='Halted']",
                "#bulk-strategy-select", ".bulk-toolbar .bulk-count"):
        ratio = page.locator(sel).first.evaluate(CONTRAST)
        assert ratio >= 4.5, f"{theme} {sel}: contraste {ratio:.2f}"
    # le bouton principal se détache de la barre
    bar_bg = page.locator("#bulk-toolbar").evaluate("e => getComputedStyle(e).backgroundColor")
    start_bg = page.locator("#btn-bulk-start").evaluate("e => getComputedStyle(e).backgroundColor")
    assert bar_bg != start_bg
