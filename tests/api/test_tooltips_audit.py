"""v1.7.1 — tooltip/i18n audit of interactive controls.

Born from a user report: console-toolbar buttons rendered with the .tip
text ornament (dashed underline + help cursor) and several surfaces still
carried hardcoded English `title="…"` strings — untranslated and outside
the styled tooltip system.

Contract enforced here:
  1. `.tip`'s help ornament never applies to <button> elements.
  2. High-traffic surfaces (console toolbar, VM actions row) use i18n
     data-tips exclusively.
  3. The remaining hardcoded-title debt only shrinks (baseline), so new
     controls must ship with `data-tip="${i18n.t(…)}"` from day one.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS_DIR = ROOT / "web" / "static" / "js"
CSS = (ROOT / "web" / "static" / "css" / "style.css").read_text()

# A literal English title= (not an interpolated `title="${…}"`).
HARDCODED_TITLE_RE = re.compile(r'title="(?!\$\{)[A-Za-z][^"]*"')


def _debt():
    out = {}
    for f in sorted(JS_DIR.glob("*.js")):
        n = len(HARDCODED_TITLE_RE.findall(f.read_text()))
        if n:
            out[f.name] = n
    return out


def test_tip_ornament_not_on_buttons():
    assert ".tip:not(button)" in CSS, (
        "the dashed-underline/help-cursor ornament must be scoped to "
        "non-button .tip elements")


def test_console_toolbar_fully_tipped():
    js = (JS_DIR / "vm-console.js").read_text()
    bar = js.split("vm-console-bar", 1)[1].split("vm-console-screen", 1)[0]
    for btn in re.findall(r"<button[^>]*>", bar):
        assert "data-tip=" in btn, f"console toolbar button without data-tip: {btn}"
    assert 'title="' not in bar


def test_vm_actions_row_fully_tipped():
    js = (JS_DIR / "app.js").read_text()
    cell = js.split("vm-actions-cell", 1)[1].split("</td>", 1)[0]
    assert 'title="' not in cell, "vm-actions row must use i18n data-tips"
    assert cell.count("data-tip=") >= 6


def test_hardcoded_title_debt_only_shrinks():
    """Snapshot of the remaining debt (2026-09-08). Advanced surfaces —
    converted opportunistically; daily surfaces are already clean. When
    you translate a file, lower its number; never raise one."""
    BASELINE = {
        # v1.8.5: debt CLEARED — every surface (bmc, capi, notes,
        # terraform, tf-*) now uses i18n data-tips (or an i18n-interpolated
        # native title on pattern-validated inputs). Keep everything at 0.
        # i18n.js: one literal inside a doc comment, not a control.
        "i18n.js": 1,
    }
    debt = _debt()
    for fname, n in debt.items():
        allowed = BASELINE.get(fname, 0)
        assert n <= allowed, (
            f"{fname}: {n} hardcoded English title= (baseline {allowed}) — "
            f"new controls must use data-tip=\"${{i18n.t('…')}}\" with EN+FR keys")


def test_template_titles_are_i18n_wired():
    """v1.8.5: every literal title= in index.html must sit next to a
    data-i18n-title so applyTranslations() replaces the English fallback
    at startup (sidebar tabs, sort headers, sub-tabs, refresh buttons)."""
    html = (ROOT / "web" / "templates" / "index.html").read_text()
    bad = []
    for tag in re.findall(r"<[^>]*\btitle=\"[A-Za-z][^\"]*\"[^>]*>", html):
        if "data-i18n-title" not in tag:
            bad.append(tag.strip()[:90])
    assert not bad, "template titles without data-i18n-title:\n" + "\n".join(bad)
