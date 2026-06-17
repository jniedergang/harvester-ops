"""v1.6.2 — settings modal a11y guards.

Source-level checks that lock in the modal accessibility upgrades:
  - role="dialog" / aria-modal / aria-labelledby on the overlay
  - id matches the labelledby target
  - settings.js installs a Tab/Shift+Tab focus trap and restores the
    previously-focused element on close
  - aria-hidden flips between open/closed states
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_HTML = ROOT / "web" / "templates" / "index.html"
SETTINGS_JS = ROOT / "web" / "static" / "js" / "settings.js"


# ---------------------------------------------------------------------------
# Template: ARIA wiring on the dialog
# ---------------------------------------------------------------------------

def _modal_block():
    """Return the slice of index.html between <div ... id="settings-modal" ...>
    and the next closing of that container. Cheap textual extraction — we
    just need the few attributes on the overlay element."""
    html = INDEX_HTML.read_text()
    start = html.find('id="settings-modal"')
    assert start > 0, "settings-modal element not found in index.html"
    # Take 2KB after — enough to reach the title <h3>
    return html[max(0, start - 200): start + 2048]


def test_modal_has_role_dialog():
    assert 'role="dialog"' in _modal_block()


def test_modal_is_aria_modal():
    assert 'aria-modal="true"' in _modal_block()


def test_modal_labelledby_matches_title_id():
    block = _modal_block()
    assert 'aria-labelledby="settings-modal-title"' in block, (
        "overlay must reference the title element"
    )
    assert 'id="settings-modal-title"' in block, (
        "the <h3> must carry id=settings-modal-title (the labelledby target)"
    )


def test_modal_initially_aria_hidden():
    assert 'aria-hidden="true"' in _modal_block(), (
        "modal should start aria-hidden=true so it's invisible to AT until opened"
    )


def test_close_button_has_aria_label():
    block = _modal_block()
    assert 'id="btn-close-settings"' in block
    assert 'aria-label="Close"' in block, (
        "close button must carry aria-label so screen readers announce it"
    )


# ---------------------------------------------------------------------------
# settings.js: focus trap + previous focus restore
# ---------------------------------------------------------------------------

def test_settings_installs_focus_trap_on_open():
    src = SETTINGS_JS.read_text()
    assert "_installFocusTrap" in src
    assert "_removeFocusTrap" in src
    # The trap must intercept Tab (forward) and Shift+Tab (back).
    assert "e.key !== 'Tab'" in src or 'e.key !== "Tab"' in src, (
        "trap must key on Tab"
    )
    assert "e.shiftKey" in src, "trap must distinguish Shift+Tab"


def test_settings_saves_and_restores_previous_focus():
    src = SETTINGS_JS.read_text()
    assert "_prevFocus = document.activeElement" in src, (
        "must remember which element had focus before opening"
    )
    # Restoration on close
    assert "_prevFocus.focus()" in src, (
        "must restore focus on closeModal"
    )


def test_settings_toggles_aria_hidden():
    src = SETTINGS_JS.read_text()
    assert "setAttribute('aria-hidden', 'false')" in src, (
        "openModal must set aria-hidden=false"
    )
    assert "setAttribute('aria-hidden', 'true')" in src, (
        "closeModal must set aria-hidden=true"
    )


def test_settings_focuses_close_button_on_open():
    """Initial focus inside the modal should land on a safe button —
    we chose the close button (×) so Enter never triggers a destructive
    action."""
    src = SETTINGS_JS.read_text()
    assert "btn-close-settings" in src
    # Must call .focus() near the close button reference
    assert "closeBtn.focus()" in src
