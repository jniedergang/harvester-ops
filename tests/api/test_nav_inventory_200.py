"""2.0.0, tâche 10 : inventaire des écrans (tests/nav_inventory.py).

Rien ne se perd au retrait de l'ancien menu : chaque section `tab-*` du
modèle est atteinte par au moins une adresse de l'inventaire, chaque adresse
se résout (routes.js puis router.js, dans node) vers la section annoncée,
chaque ancien jeton de `Routes.LEGACY` a sa ligne et s'ouvre sur la même
section. L'ancien menu n'est plus dans la page. Mutation volontaire : une
section factice `tab-zzz` dans une copie du modèle doit être signalée."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from nav_inventory import SCREENS, address, orphans, sections  # noqa: E402

HTML = (ROOT / "web" / "templates" / "index.html").read_text(encoding="utf-8")
C, H = "harv-fake", "node2"

RUNNER = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
global.window = global;
const Routes = require('./web/static/js/routes.js');
const Router = require('./web/static/js/router.js');
const ctx = { harvester: [input.c], kvm: [input.h], lastHarvester: input.c, lastKvm: input.h };
const resolve = (h) => {
  let r = Routes.parse(h);
  if (r.name === 'unknown') r = Routes.fromLegacy(h, ctx);
  const t = r ? Router.resolve(r) : null;
  return t ? t.tab : null;
};
console.log(JSON.stringify({
  addresses: input.addresses.map(resolve),
  legacy: Object.keys(Routes.LEGACY).map((k) => [k, resolve('#' + k)]),
}));
"""


def node(addresses):
    try:
        r = subprocess.run(["node", "-e", RUNNER], input=json.dumps({"addresses": addresses, "c": C, "h": H}),
                           capture_output=True, text=True, cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node absent")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_every_section_of_the_template_has_an_address():
    assert len(sections(HTML)) >= 30
    assert orphans(HTML) == [], "écran sans adresse dans l'inventaire"


def test_the_inventory_names_only_real_sections():
    real = set(sections(HTML))
    assert {s[2] for s in SCREENS} <= real


def test_each_correspondence_line_of_the_design_has_its_entry():
    design = (ROOT / "docs" / "design" / "2026-10-24-navigation-hotes-vms.md").read_text(encoding="utf-8")
    table = design.split("## Correspondance des onglets", 1)[1].split("\n## ", 1)[0]
    lines = [ln for ln in table.splitlines() if ln.startswith("| ") and not ln.startswith("| Aujourd")]
    assert len(lines) == 31
    # une ligne de l'inventaire au moins par ligne de la conception (KVM > Hosts
    # et quelques autres en ont plusieurs : un écran par adresse)
    assert len(SCREENS) >= len(lines)


@pytest.mark.parametrize("entry", SCREENS, ids=[s[1] + " " + s[0] for s in SCREENS])
def test_each_address_resolves_to_its_section(entry):
    out = node([address(entry, C, H)])
    assert out["addresses"][0] == entry[2], entry


def test_every_legacy_token_has_its_line_and_opens_the_same_section():
    out = node([])
    by_token = {}
    for s in SCREENS:
        for tok in s[4]:
            by_token.setdefault(tok, set()).add(s[2])
    missing = [k for k, _ in out["legacy"] if k not in by_token]
    assert missing == [], f"ancien jeton sans ligne : {missing}"
    wrong = [(k, tab, by_token[k]) for k, tab in out["legacy"] if tab not in by_token[k]]
    assert wrong == []


def test_the_old_menu_is_gone_from_the_page():
    nav = HTML.split("<nav>", 1)[1].split("</nav>", 1)[0]
    assert 'id="cluster-select"' not in HTML and 'id="btn-add-cluster"' not in HTML
    assert "data-tab=" not in nav and 'class="tab-group' not in nav
    # les entrées du menu sont des liens appliqués par le routeur
    assert all('data-hnav=' in a for a in nav.split('class="tab ')[1:])


def test_the_shared_resources_keep_their_order_in_the_menu():
    shared = HTML.split('id="hnav-shared"', 1)[1].split("</div>", 1)[0]
    import re
    assert re.findall(r'data-hnav-root="(\w+)"', shared) == ["gpus", "bookings", "usage"]


def test_nav_inventory_mutation_orphan():
    """Mutation volontaire : une section ajoutée sans adresse est signalée."""
    copy = HTML.replace("</main>", '<section id="tab-zzz" class="tab-content"></section></main>', 1)
    assert copy != HTML
    assert orphans(copy) == ["zzz"]
    with pytest.raises(AssertionError):
        assert orphans(copy) == []


def test_no_code_listens_to_a_removed_menu_element():
    """Mineur 4 : un écouteur ou une règle de style visant un élément de
    l'ancien menu (onglet de sous-section d'Automatisation, sélecteur de
    cluster) ne se déclenche plus jamais ; il trompe le lecteur."""
    js = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / "web/static/js").glob("*.js")}
    dead = [n for n, src in js.items() if "#tab-automation .sub-tab[data-subtab=" in src]
    assert dead == []
    css = (ROOT / "web/static/css/style.css").read_text(encoding="utf-8")
    assert ".cluster-selector" not in css
    assert not any("cluster-selector" in src for src in js.values())


PANES_RUNNER = r"""
global.window = global;
global.document = { addEventListener() {} };
require('./web/static/js/sections.js');
const Routes = require('./web/static/js/routes.js');
const Router = require('./web/static/js/router.js');
const IMPORTS = ['vmimport', 'forklift'];
const out = [];
for (const [sec, spec] of Object.entries(window.Sections.DEF)) {
  for (const pane of Object.keys(spec.panes)) {
    const h = IMPORTS.includes(sec) ? `#harvester/c1/imports/${sec}/${pane}` : `#harvester/c1/params/${sec}/${pane}`;
    const t = Router.resolve(Routes.parse(h));
    const back = t ? Routes.format(Routes.parse(h)) : null;
    out.push([sec, pane, h, t ? t.tab : null, t ? t.pane : null, back]);
  }
}
console.log(JSON.stringify(out));
"""


def test_every_pane_of_every_section_has_its_own_address():
    """L'inventaire nomme un pane par section ; ici chaque pane de
    `Sections.DEF` a son adresse, qui se résout vers sa section ET ce pane,
    et s'écrit à l'identique (perdre l'adresse d'un seul pane, `network/lbs`
    par exemple, est vu)."""
    try:
        r = subprocess.run(["node", "-e", PANES_RUNNER], capture_output=True, text=True, cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node absent")
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert len(rows) >= 30
    bad = [row for row in rows if row[3] != row[0] or row[4] != row[1] or row[5] != row[2]]
    assert bad == [], bad


TABLE_RUNNER = r"""
global.window = global; global.document = { addEventListener() {} };
const Routes = require('./web/static/js/routes.js'); const Router = require('./web/static/js/router.js');
const inp = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify({
  legacy: Object.keys(Routes.LEGACY),
  news: inp.map((h) => { const r = Routes.parse(h); const t = r.name === 'unknown' ? null : Router.resolve(r);
                         return [h, t ? t.tab : null, t ? Routes.format(r) : null]; }),
}));
"""


def _changelog_table():
    import re
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    block = text[text.index("## [2.0.0]"):text.index("## [1.101.3]")]
    rows = [ln for ln in block.splitlines() if ln.startswith("  | ") and "---" not in ln and "Old token" not in ln]
    olds, news = set(), []
    for ln in rows:
        place, old, new = [c.strip() for c in re.split(r"(?<!\\)\|", ln.strip())[1:-1]]
        olds |= {t[1:] for t in re.findall(r"`(#[^`]+)`", old)}
        for tok in re.findall(r"`(#[^`]+)`", new):
            tok = tok.replace("\\|", "|")
            m = re.search(r"\[/([^\]]+)\]$", tok)
            base = re.sub(r"\[/[^\]]+\]$", "", tok)
            for v in [base] + ([f"{base}/{x}" for x in m.group(1).split("|")] if m else []):
                ident = "rp-0123456789ab" if v.startswith("#dr") else ("fa0000000001" if "action" in v else "3")
                news.append(v.replace("<c>", C).replace("<h>", H).replace("<id>", ident))
    return olds, news


def test_the_changelog_correspondence_table_is_complete_and_true():
    """CHANGELOG 2.0.0 : chaque ancien jeton de `Routes.LEGACY` a sa ligne,
    chaque adresse nouvelle (onglets optionnels compris) se résout vers un
    écran et s'écrit à l'identique."""
    olds, news = _changelog_table()
    assert len(news) >= 80
    try:
        r = subprocess.run(["node", "-e", TABLE_RUNNER], input=json.dumps(news), capture_output=True, text=True,
                           cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node absent")
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert set(d["legacy"]) - olds == set(), "ancien jeton absent du tableau du CHANGELOG"
    assert olds - set(d["legacy"]) == set(), "jeton du tableau inconnu de Routes.LEGACY"
    assert [n for n in d["news"] if n[1] is None or n[2] != n[0]] == []
