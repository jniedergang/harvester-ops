"""2.8.0 (tâche 9) : contrôles de source de registries.js (échéance du
certificat, Renouveler le certificat, Nœuds Harvester)."""

import re
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parents[2] / "web" / "static" / "js" / "registries.js").read_text()


@pytest.mark.parametrize("code", ["registry-ca-key-missing", "registry-ca-transition-pending",
                                  "harvester-nodes-mirror-conflict", "harvester-rke2-restart",
                                  "harvester-nodes-self-hosted"])  # 2.8.1
def test_new_finding_codes_have_a_literal_translation(code):
    assert re.search(rf"'{re.escape(code)}': \(v\) => tr\('reg\.fd\.{re.escape(code)}'", JS)


@pytest.mark.parametrize("route", ["/renew-cert`", "/harvester-nodes`"])
def test_each_gesture_is_dry_run_before_its_launch(route):
    calls = [m.start() for m in re.finditer(re.escape(route), JS)]
    assert len(calls) == 2
    first = JS[calls[0]:calls[0] + 200]
    assert "dry_run: true" in first


def test_gestures_are_admin_only_and_greyed_with_their_reason():
    assert "!can('admin') || d.cluster !== c" in JS
    assert "!can('admin') || !attached" in JS


def test_rke2_acceptance_gates_the_launch():
    assert "if (!v('rke2').checked)" in JS
    assert "if (!last.changed)" in JS


def test_follow_goes_through_sse_reconnect():
    seg = JS[JS.index("function followAction"):JS.index("function openRenewCert")]
    assert "SSEReconnect.connect(" in seg and "new EventSource" not in seg


def test_previous_and_new_values_are_escaped():
    assert "esc(d.previous || '-')" in JS and "esc(d.value || '-')" in JS
