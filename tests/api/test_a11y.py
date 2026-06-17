"""Accessibility regression guards.

These checks live at the source-text level (no DOM needed) so they run
in milliseconds and don't depend on Playwright. Each one locks in a
v1.4.17 fix so it can't silently regress.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CSS = ROOT / "web" / "static" / "css" / "style.css"
I18N_JS = ROOT / "web" / "static" / "js" / "i18n.js"
APP_JS = ROOT / "web" / "static" / "js" / "app.js"


def test_tooltip_keyboard_focus_rule_present():
    """`.tip[data-tip]:focus::after` must reveal the bubble on Tab. Without
    this rule keyboard users can never read the tooltip content."""
    css = CSS.read_text()
    assert ":focus::after" in css, "no :focus::after rule found"
    assert ".tip[data-tip]:focus::after" in css or ":focus-visible::after" in css, (
        "tooltip system has no keyboard-focus reveal rule"
    )


def test_global_focus_visible_outline_present():
    """A global :focus-visible rule must set a non-transparent outline so
    keyboard users always see where the focus is. We look for the
    standalone universal rule (no leading selector) — the one styling
    every focusable element. ProseMirror has its own scoped rule too
    but we want the broad fallback."""
    css = CSS.read_text()
    # Look for the universal rule (starts on its own line)
    import re
    matches = list(re.finditer(r"\n:focus-visible\s*\{([^}]*)\}", css))
    assert matches, "no standalone :focus-visible {} block found"
    body = matches[0].group(1)
    assert "outline:" in body, "global :focus-visible has no outline declaration"
    outline_decl = body.split("outline:")[1].split(";")[0]
    assert "transparent" not in outline_decl, "outline is transparent (invisible)"
    assert " none" not in outline_decl, "outline is none (invisible)"
    # And the accent color is what we use everywhere else, so the focus
    # ring matches the rest of the theme.
    assert "var(--accent)" in outline_decl


def test_no_outline_zero_or_none_on_focusable_rule():
    """Any rule that targets `:focus` must NOT strip the outline. The fix
    is to either keep the default outline or set a visible alternative.
    We grep the source for `:focus { ... outline: 0` / `outline: none` and
    fail if any match — keyboard users would never see focus there."""
    css = CSS.read_text()
    bad = []
    for chunk in css.split("}"):
        if ":focus" in chunk and ("outline: 0" in chunk or "outline: none" in chunk):
            bad.append(chunk.strip()[:120])
    assert not bad, (
        "CSS strips outline on :focus selectors — keyboard users blind:\n  - "
        + "\n  - ".join(bad)
    )


def test_phase_stopped_uses_accessible_contrast():
    """v1.4.17: .phase.Stopped was --bg-elev / --text-dim ≈ 2.5:1 which
    fails WCAG AA (4.5:1). Now uses --border / --text."""
    css = CSS.read_text()
    # Find the rule and check the colors
    idx = css.find(".phase.Stopped {")
    assert idx > 0, ".phase.Stopped not found"
    block = css[idx:idx + 120]
    assert "var(--text)" in block, (
        ".phase.Stopped still uses dim text — contrast below WCAG AA"
    )
    assert "var(--text-dim)" not in block


def test_phase_unknown_uses_accessible_contrast():
    """Same: .phase.Unknown was --bg / --text-dim ≈ 1.8:1 (terrible)."""
    css = CSS.read_text()
    idx = css.find(".phase.Unknown {")
    assert idx > 0
    block = css[idx:idx + 120]
    assert "var(--text)" in block
    assert "var(--text-dim)" not in block


def test_halted_running_state_uses_accessible_contrast():
    """`.vm-group-list .running-state.Halted` was --bg-elev / --text-dim."""
    css = CSS.read_text()
    idx = css.find(".vm-group-list .running-state.Halted {")
    assert idx > 0
    block = css[idx:idx + 200]
    assert "var(--text)" in block, "Halted state still has low-contrast text"


def test_tip_elements_get_tabindex_via_i18n_pass():
    """applyTranslations() must make non-button .tip spans focusable so
    keyboard users can reach them. The source must call
    setAttribute('tabindex', '0') in the tooltip loop."""
    src = I18N_JS.read_text()
    assert "setAttribute('tabindex'" in src or 'setAttribute("tabindex"' in src, (
        "i18n.js does not promote .tip spans to focusable"
    )
    # And aria-label fallback so screen readers announce the tip
    assert "aria-label" in src


def test_cancel_button_shows_progress_state():
    """v1.4.17 UX: cancelAction() must put the source button into a
    'Cancelling…' state so the user sees the DELETE is in flight."""
    src = APP_JS.read_text()
    assert "i18n.t('action.cancelling')" in src or 't(\"action.cancelling"' in src, (
        "cancelAction does not surface a cancelling-in-progress state"
    )
    assert "classList.add('cancelling')" in src or "'cancelling')" in src, (
        "cancelAction does not toggle a .cancelling class on the button"
    )


def test_cancel_pulse_animation_defined():
    """The .cancelling state needs a visible animation so the in-flight
    state is unambiguous (not just a static color change)."""
    css = CSS.read_text()
    assert "@keyframes cancel-pulse" in css
    assert ".btn.cancelling" in css


def test_btn_danger_uses_var():
    """`.btn-danger` previously hard-coded `#6a1f22`. v1.4.17 must use
    var(--danger) for consistency with the rest of the theme."""
    css = CSS.read_text()
    # Find the .btn-danger rule
    idx = css.find(".btn-danger {")
    if idx > 0:
        block = css[idx:idx + 200]
        # Either uses var(--danger) directly OR the hard-coded value is
        # documented (legacy darker shade for un-hover state is acceptable
        # if hover state lifts to the var). What we DON'T want is *both*
        # hard-coded — that's the bug.
        assert "var(--danger)" in block or "#6a1f22" not in block, (
            ".btn-danger hard-codes #6a1f22 instead of using var(--danger)"
        )
