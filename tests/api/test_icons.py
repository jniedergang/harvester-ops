"""v1.14.0 — inline SVG icon set.

User report on the VM actions row: the emoji icons read as a jumble —
some rendered as full-colour images (📸 📝), others as thin glyphs (■ ⚙),
with sizes varying per platform and poor legibility at 16 px. They are
replaced by a hand-drawn monochrome set that inherits `currentColor`, so
one set covers the five themes and both light/dark modes.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"
ICONS = (JS / "icons.js").read_text()

# Emoji ranges we do NOT want as action-button icons any more.
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def test_icon_set_is_monochrome_and_theme_aware():
    assert "stroke=\"currentColor\"" in ICONS, (
        "icons must inherit the button colour so one set fits every theme")
    assert "viewBox=\"0 0 24 24\"" in ICONS
    # every action of the VM row has an icon
    for name in ("play", "stop", "snapshot", "migrate", "settings",
                 "console", "notes", "restore", "trash", "restart"):
        assert f"{name}:" in ICONS, f"missing icon: {name}"


def test_vm_actions_row_uses_svg_not_emoji():
    app = (JS / "app.js").read_text()
    cell = app.split("vm-actions-cell", 1)[1].split("</td>", 1)[0]
    assert not EMOJI_RE.search(cell), (
        f"emoji left in the VM actions row: {EMOJI_RE.findall(cell)}")
    assert cell.count("Icons.svg(") >= 6


def test_console_toolbar_uses_the_same_icons():
    js = (JS / "vm-console.js").read_text()
    bar = js.split("vm-console-bar", 1)[1].split("vm-console-screen", 1)[0]
    assert not EMOJI_RE.search(bar), (
        f"emoji left in the console toolbar: {EMOJI_RE.findall(bar)}")
    assert bar.count("Icons.svg(") >= 4


def test_icons_script_loads_before_its_users():
    html = (ROOT / "web" / "templates" / "index.html").read_text()
    assert "/static/js/icons.js" in html
    assert html.index("/static/js/icons.js") < html.index("/static/js/app.js"), (
        "icons.js must load before the modules that call Icons.svg()")


def test_panel_header_keeps_svg_labels_unescaped():
    """A header action can carry an SVG label; escaping it would print
    the raw markup."""
    fp = (JS / "floating-panels.js").read_text()
    assert "<\\/svg>$/.test(a.label" in fp or "svg" in fp.split("data-header-action", 1)[1][:400]
