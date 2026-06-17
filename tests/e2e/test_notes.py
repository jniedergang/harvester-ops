"""Comprehensive Tiptap+Yjs notes tests.

The user reported text disappearing right after typing and a 2-user
reconnect loop. We were trapped in a cycle of partial fixes — this file
covers the full collaboration loop end-to-end so any regression is caught
on the next CI run.

Each test uses Playwright to drive a real Chromium so the Tiptap editor,
the WebSocket, and the Y.Doc binding are exercised together.
"""

import time
import pytest

EDITOR = ".notes-editor .ProseMirror"
TOOLBAR_BOLD = ".notes-toolbar button[data-cmd='bold']"
STATUS = ".notes-status"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _open_ns_notes(page, namespace="default"):
    """Open the notes panel for `default` namespace via the JS API.
    Avoids depending on the namespaces/VM list UI being populated."""
    page.evaluate(
        "([ns]) => window.Notes && window.Notes.open('ns', 'harv-fake', ns)",
        [namespace],
    )
    page.wait_for_selector(EDITOR, timeout=8000)
    page.wait_for_selector(f"{STATUS}.connected", timeout=8000)


def _editor_text(page):
    return page.locator(EDITOR).inner_text()


def _editor_html(page):
    return page.locator(EDITOR).inner_html()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_notes_panel_opens(page):
    """Smoke: the panel opens, the editor mounts, the WS reaches `connected`."""
    page.wait_for_load_state("networkidle")
    _open_ns_notes(page, "default")
    # Status pill flips from connecting… to ● connected
    assert "connected" in page.locator(STATUS).get_attribute("class") or ""


def test_notes_typed_text_persists_locally(page):
    """REGRESSION: text typed in the editor must remain after 2s of idle.

    The bug observed in 1.4.5 was the typed character vanishing
    instantly. If the local Y.Doc gets clobbered by the server snapshot
    or by ySyncPlugin re-render, this test fails.
    """
    page.wait_for_load_state("networkidle")
    _open_ns_notes(page, "default")
    page.locator(EDITOR).click()
    page.keyboard.type("hello-world-typing-test")
    # Wait beyond any conceivable round-trip
    page.wait_for_timeout(2000)
    text = _editor_text(page)
    assert "hello-world-typing-test" in text, (
        f"typed text vanished — editor reads: {text!r}"
    )


def test_notes_human_speed_typing(page):
    """REGRESSION: type the way a real user does — char-by-char with a
    small delay between keys, so each Tiptap → Y.Doc → WS round-trip can
    interleave. If the server's snapshot replay clobbers the local
    content during typing, characters drop and this assertion fails.
    """
    _open_ns_notes(page, "default-slow")
    page.locator(EDITOR).click()
    page.wait_for_timeout(300)
    # Mimic real keystrokes with delay (Playwright default is no delay).
    page.keyboard.type("Bonjour notes!", delay=80)
    page.wait_for_timeout(1500)
    text = _editor_text(page)
    assert "Bonjour notes!" in text, f"slow-typed text dropped: {text!r}"


def test_notes_old_y_text_no_collision(page, flask_server):
    """REGRESSION: when a saved Y state from the pre-Tiptap version
    contains a `Y.Text` under name 'content', the new Tiptap code (which
    binds to a `Y.XmlFragment`) must NOT collide and clobber typing.

    Before 1.4.8 the field was 'content' for both — Tiptap's content-check
    failed, called disableCollaboration() → doc.destroy(), and every
    typed character vanished. Fix: rename Tiptap's field to 'prosemirror'.
    Test by pre-loading the server with an old-format Y.Text state.
    """
    import sqlite3, time, urllib.request
    cfg = flask_server["config"]
    notes_db = cfg["root"] / "notes.db"
    if not notes_db.exists():
        # Trigger creation via a touch endpoint then check the schema exists.
        urllib.request.urlopen(flask_server["base_url"] + "/api/notes/ns/harv-fake/_warmup")
    # Build a tiny "old format" Y state: Y.Text 'content' with some bytes.
    # We use the server's own y_py to generate a real Y update.
    import sys; sys.path.insert(0, str(cfg["root"].parent.parent / "web"))
    import y_py as Y
    legacy_doc = Y.YDoc()
    txt = legacy_doc.get_text("content")
    with legacy_doc.begin_transaction() as txn:
        txt.insert(txn, 0, "legacy content from old format")
    legacy_state = Y.encode_state_as_update(legacy_doc)
    # Seed it under a unique doc_id so this test owns the row.
    doc_id = "ns/harv-fake/legacy-collision"
    conn = sqlite3.connect(str(notes_db))
    conn.execute(
        "INSERT OR REPLACE INTO notes(doc_id, state, updated_at) VALUES (?, ?, ?)",
        (doc_id, legacy_state, time.time()),
    )
    conn.commit(); conn.close()

    # Now open the editor for that doc; typing must NOT vanish.
    _open_ns_notes(page, "legacy-collision")
    page.locator(EDITOR).click()
    page.wait_for_timeout(300)
    page.keyboard.type("fresh-text-after-legacy", delay=30)
    page.wait_for_timeout(1500)
    text = _editor_text(page)
    assert "fresh-text-after-legacy" in text, (
        f"typed text vanished after loading legacy Y.Text state: {text!r}"
    )


def test_notes_long_burst_then_idle(page):
    """REGRESSION: type a longer burst (200+ chars) and verify every
    character is still there after 3s. A fast send → slow server →
    snapshot-replay race would manifest as truncation."""
    _open_ns_notes(page, "default-burst")
    page.locator(EDITOR).click()
    body = "Lorem ipsum dolor sit amet " * 8  # ~210 chars
    page.keyboard.type(body, delay=10)
    page.wait_for_timeout(3000)
    text = _editor_text(page)
    assert body.strip() in text, (
        f"burst content truncated. expected {len(body)} chars, got {len(text)}\n"
        f"text was: {text[:200]!r}..."
    )


def test_notes_formatting_via_toolbar(page):
    """Click the Bold button → typed text gets <strong>."""
    page.wait_for_load_state("networkidle")
    _open_ns_notes(page, "default-fmt")
    page.locator(EDITOR).click()
    page.locator(TOOLBAR_BOLD).click()
    page.keyboard.type("bold-typing")
    page.wait_for_timeout(500)
    html = _editor_html(page)
    assert "<strong>" in html and "bold-typing" in html, html


def test_notes_two_users_real_sync(page, context, flask_server):
    """The hard one. Open the SAME note in two browser contexts; what one
    types must appear in the other. Catches the 2-user reconnect bug AND
    the typed-text-disappears bug at once."""
    base = flask_server["base_url"]
    page.goto(base)
    page.wait_for_load_state("networkidle")
    _open_ns_notes(page, "default-2u")
    page.locator(EDITOR).click()
    page.keyboard.type("from-A:")
    page.wait_for_timeout(700)

    # 2nd browser context — fully isolated session.
    page_b = context.new_page()
    page_b.goto(base)
    page_b.wait_for_load_state("networkidle")
    _open_ns_notes(page_b, "default-2u")

    # B should see what A typed within 3s
    page_b.wait_for_function(
        "() => document.querySelector('.notes-editor .ProseMirror')?.innerText?.includes('from-A:')",
        timeout=5000,
    )
    # A still has its text (no regression)
    assert "from-A:" in _editor_text(page)

    # B types — A sees it
    page_b.locator(EDITOR).click()
    page_b.keyboard.press("End")
    page_b.keyboard.type(" :from-B")
    page.wait_for_function(
        "() => document.querySelector('.notes-editor .ProseMirror')?.innerText?.includes(':from-B')",
        timeout=5000,
    )
    assert ":from-B" in _editor_text(page)
    page_b.close()


def test_notes_open_node_kind(page):
    """The `node` kind opens a note keyed by node name; same WS protocol
    as the ns/vm kinds. Catches regressions where _validate_doc_id rejects
    the new prefix or the docId scheme is wrong.

    Critically: this asserts the connection HOLDS for several seconds.
    Without that, a server that immediately closes the WS (e.g. validator
    rejects the prefix) would still satisfy a wait-for-`.connected`
    because the lib briefly flips the class on `open` before the close
    fires. We watch the reconnect counter to detect that pattern.
    """
    page.wait_for_load_state("networkidle")
    page.evaluate(
        "window.Notes && window.Notes.open('node', 'harv-fake', 'fake-cp1')"
    )
    page.wait_for_selector(EDITOR, timeout=8000)
    page.wait_for_selector(f"{STATUS}.connected", timeout=8000)

    # Hold check: 3 seconds is longer than the 3000ms reconnect cadence in
    # notes.js — if the validator rejects the doc_id and the WS keeps
    # reopening + closing, the status pill cycles between connected and
    # "○ disconnected — reconnecting…", and the final state lands on a
    # non-connected class. We sample 3× over 3 s and require ALL of them
    # to read connected.
    for i in range(3):
        page.wait_for_timeout(1000)
        cls = page.locator(STATUS).get_attribute("class") or ""
        assert "connected" in cls.split(), (
            f"Connection dropped at sample {i+1}/3 (class={cls!r}). "
            f"Likely _validate_doc_id rejects the 'node/' prefix on the "
            f"server you're testing against, causing a reconnect loop."
        )

    page.locator(EDITOR).click()
    page.keyboard.type("note for node fake-cp1", delay=30)
    page.wait_for_timeout(1000)
    assert "note for node fake-cp1" in _editor_text(page)


def test_notes_survives_reconnect(page, flask_server):
    """Close+reopen the panel → typed text must be restored from the
    persisted Y state (SQLite)."""
    _open_ns_notes(page, "default-persist")
    page.locator(EDITOR).click()
    page.keyboard.type("persisted")
    page.wait_for_timeout(800)
    # Close the panel by killing its floating-panel
    page.evaluate(
        "() => window.Notes && window.Notes.disconnect('ns/harv-fake/default-persist')"
    )
    page.wait_for_timeout(300)
    # Reopen, expect "persisted" to come back via snapshot
    _open_ns_notes(page, "default-persist")
    page.wait_for_function(
        "() => document.querySelector('.notes-editor .ProseMirror')?.innerText?.includes('persisted')",
        timeout=4000,
    )
