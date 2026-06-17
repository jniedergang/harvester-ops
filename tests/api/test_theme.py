"""Source-level guards for the v1.4.34 theme switcher.

The runtime e2e test (test_theme_switcher_handles_all_5_themes) verifies
the palettes are *applied* to the DOM. These cheaper checks lock in the
*structure* so a missing block, typo'd selector or removed file fails
loudly without spinning up Playwright.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CSS = ROOT / "web" / "static" / "css" / "style.css"
INDEX = ROOT / "web" / "templates" / "index.html"
THEME_JS = ROOT / "web" / "static" / "js" / "theme.js"

THEMES = ["suse", "nord", "solarized", "catppuccin", "tokyo"]
MODES = ["dark", "light"]


def test_all_themes_have_a_palette_block():
    """Every (theme, mode) pair must have a [data-theme][data-mode]
    selector block in style.css. A missing block silently falls back
    to the :root default — the switcher would appear to do nothing."""
    css = CSS.read_text()
    for theme in THEMES:
        for mode in MODES:
            selector = f'[data-theme="{theme}"][data-mode="{mode}"]'
            assert selector in css, (
                f"missing palette block for {selector} in style.css"
            )


def test_every_palette_block_sets_bg_and_accent():
    """Each block must redefine at least --bg, --accent and --text —
    the bare minimum to look different. If only --bg is set, the rest
    inherits from the previous theme and the UI looks broken."""
    css = CSS.read_text()
    required = ("--bg:", "--accent:", "--text:", "--border:", "--tooltip-bg:")
    for theme in THEMES:
        for mode in MODES:
            sel = f'[data-theme="{theme}"][data-mode="{mode}"]'
            idx = css.find(sel)
            assert idx >= 0, f"selector {sel} not found"
            # Take the slice from this block's opening brace to the next
            # closing brace — a coarse but reliable scope.
            open_brace = css.find("{", idx)
            close_brace = css.find("}", open_brace)
            block = css[open_brace:close_brace]
            for var in required:
                assert var in block, (
                    f"{sel} block does not declare {var} — "
                    f"will inherit from default and break the switch"
                )


def test_theme_js_lists_match_css_blocks():
    """The Theme module's VALID_THEMES / VALID_MODES allow-lists must
    stay in sync with the CSS blocks above. A theme present in CSS but
    missing from JS is unreachable from the switcher."""
    js = THEME_JS.read_text()
    for theme in THEMES:
        assert f"'{theme}'" in js, f"theme {theme!r} not in theme.js allow-list"
    for mode in MODES:
        assert f"'{mode}'" in js, f"mode {mode!r} not in theme.js allow-list"


def test_index_html_has_fouc_free_boot_script():
    """The inline <script> in <head> must apply data-theme + data-mode
    BEFORE style.css loads. If it ever drops below the <link>, every
    page reload will flash the default SUSE-dark for one frame."""
    html = INDEX.read_text()
    script_idx = html.find("setAttribute('data-theme'")
    link_idx = html.find('href="/static/css/style.css"')
    assert script_idx >= 0, "boot-time data-theme apply missing from index.html"
    assert link_idx >= 0, "style.css <link> missing from index.html"
    assert script_idx < link_idx, (
        "FOUC risk: theme apply runs AFTER style.css <link> — move the "
        "<script> block above the stylesheet"
    )


def test_default_theme_chain_matches_boot_script():
    """Defensive: exactly one theme may also match :root / no-attr.
    If two of them listed themselves in the :root selector chain, the
    last one parsed would win and the switcher would lie about the
    current theme on first load.

    Default since v1.4.35 = Tokyo Night Day (light)."""
    css = CSS.read_text()
    chain_start = css.find(":root,")
    if chain_start < 0:
        return
    chain_end = css.find("{", chain_start)
    chain = css[chain_start:chain_end]
    assert '"tokyo"' in chain, "Tokyo Night must hold the :root default chain"
    for theme in ("suse", "nord", "solarized", "catppuccin"):
        assert f'"{theme}"' not in chain, (
            f"theme {theme!r} hijacked the :root default chain"
        )


def test_boot_script_defaults_match_css_default():
    """The inline <head> boot script must fall back to the same
    (theme, mode) pair that the CSS :root chain encodes — otherwise
    a fresh visit (empty localStorage) sets attrs the CSS doesn't
    actually serve, and the next reload may shift palettes."""
    html = INDEX.read_text()
    theme_default = "tokyo"
    mode_default = "light"
    # The fallback line in the catch{} branch is the most reliable
    # source of truth — it runs when localStorage is unavailable.
    fallback_marker = (
        f"setAttribute('data-theme', '{theme_default}')"
    )
    assert fallback_marker in html, (
        f"boot script does not fall back to {theme_default!r} — "
        f"out of sync with the CSS :root chain"
    )
    fallback_mode = f"setAttribute('data-mode',  '{mode_default}')"
    assert fallback_mode in html, (
        f"boot script does not fall back to mode {mode_default!r}"
    )
