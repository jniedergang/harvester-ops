"""v1.6.2 — SSE reconnect helper guards.

Source-level checks that lock in the reconnect contract. The helper itself
is browser-only (EventSource is a DOM API), so we test what we can verify
from the source: the helper module exists, exposes `SSEReconnect.connect`,
implements exponential backoff, suppresses retry after a clean `end`, and
that every SSE consumer in the codebase has been migrated to it (no raw
`new EventSource(` outside the helper).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS_DIR = ROOT / "web" / "static" / "js"
HELPER = JS_DIR / "sse-reconnect.js"
INDEX = ROOT / "web" / "templates" / "index.html"


# ---------------------------------------------------------------------------
# Helper module exists and exposes the expected API
# ---------------------------------------------------------------------------

def test_helper_module_exists():
    assert HELPER.is_file(), "web/static/js/sse-reconnect.js must exist"


def test_helper_exposes_global_singleton():
    src = HELPER.read_text()
    assert "root.SSEReconnect" in src, "SSEReconnect must be attached to window"
    assert "function connect(" in src, "connect() must be exported"


def test_helper_implements_exponential_backoff():
    src = HELPER.read_text()
    assert "Math.pow(2, attempt)" in src, "must use exponential backoff"
    assert "maxRetries" in src and "maxDelay" in src, (
        "must cap retries + delay"
    )
    assert "baseDelay" in src, "must allow tuning baseDelay"


def test_helper_marks_end_as_terminal():
    """Clean stream termination via 'end' event must not trigger retry."""
    src = HELPER.read_text()
    assert "endedNormally" in src, "must track normal termination"
    # The end-handling branch must set endedNormally before calling user fn
    # so that the subsequent EventSource auto-close doesn't fire retry.
    assert re.search(r"endedNormally\s*=\s*true", src), (
        "endedNormally must be set when 'end' arrives"
    )


def test_helper_lifecycle_fires_status_events():
    src = HELPER.read_text()
    for state in ("connecting", "open", "retry", "dead", "closed"):
        assert f"'{state}'" in src, (
            f"helper must fire onStatus({state}) for caller introspection"
        )


def test_helper_loaded_before_consumers_in_template():
    """sse-reconnect.js must be loaded before any consumer (terraform.js,
    dock.js, support.js, capi.js, app.js). Otherwise SSEReconnect is
    undefined at load time."""
    html = INDEX.read_text()
    pos_helper = html.find('src="/static/js/sse-reconnect.js"')
    assert pos_helper > 0, "sse-reconnect.js must be referenced in index.html"
    for consumer in ("app.js", "dock.js", "support.js", "terraform.js", "capi.js"):
        pos = html.find(f'src="/static/js/{consumer}"')
        assert pos > pos_helper, (
            f"{consumer} must be loaded after sse-reconnect.js (got "
            f"helper@{pos_helper}, {consumer}@{pos})"
        )


# ---------------------------------------------------------------------------
# No raw EventSource left outside the helper
# ---------------------------------------------------------------------------

CONSUMERS = ["app.js", "dock.js", "support.js", "terraform.js", "capi.js"]


def test_no_raw_eventsource_in_migrated_consumers():
    """All SSE consumers must go through SSEReconnect — a raw
    `new EventSource(` in one of these files would mean the reconnect
    work was left half-done."""
    for name in CONSUMERS:
        src = (JS_DIR / name).read_text()
        # Strip line comments (// …) so a comment mentioning EventSource
        # doesn't trip the check. Keep block-comment text — we want the
        # docstring at the top of sse-reconnect.js to keep referencing
        # the underlying primitive.
        stripped = re.sub(r"//[^\n]*", "", src)
        assert "new EventSource(" not in stripped, (
            f"{name} still uses raw `new EventSource(` — migrate to "
            f"SSEReconnect.connect()"
        )


def test_consumers_call_sse_reconnect():
    """Conversely, every consumer should actually USE the helper. If a
    file lost its SSE entry-point during refactor, this catches it."""
    for name in CONSUMERS:
        src = (JS_DIR / name).read_text()
        assert "SSEReconnect.connect(" in src, (
            f"{name} no longer calls SSEReconnect.connect() — did the "
            f"SSE entry-point disappear during refactor?"
        )
