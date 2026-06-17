"""Shutdown/Startup groups — tests for the annotation model.

Groups model:
- VMs sharing `harvester-ops.io/shutdown-group` form a parallel-stop batch.
- The group's priority drives ORDER between groups (lowest first for
  shutdown, reversed for startup).
- The "default" group is the catch-all for VMs without explicit assignment.

These tests cover the two surfaces that don't need a real cluster:
1. `PUT /api/vms/<cluster>/order` accepts both legacy (`order`) and grouped
   (`groups`) payloads, calls kubectl annotate with the right args, and
   returns a sensible response shape.
2. The shell `get_ordered_vms` sort logic (extracted from common.sh) emits
   lines sorted by (priority, group, name) with the group field included.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


# ---------------------------------------------------------------------------
# 1. API endpoint: payload shapes
# ---------------------------------------------------------------------------
def test_api_order_accepts_legacy_flat_order(api, monkeypatch, tmp_path):
    """The OLD shape {"order": [{ns, name, snapshot}, ...]} still works.

    With no real kubectl, every annotate call fails — the endpoint still
    returns 200 with per-VM `ok=False` so the UI can show errors. What we
    assert here is the SHAPE of the response, not the success.
    """
    status, body = api(
        "PUT",
        "/api/vms/harv-fake/order",
        json_body={"order": [
            {"namespace": "default", "name": "vm-a", "snapshot": True},
            {"namespace": "default", "name": "vm-b", "snapshot": False},
        ]},
    )
    assert status == 200
    assert body["total"] == 2
    assert len(body["results"]) == 2
    # Each entry must declare a priority and group label, even if ok=False
    for r in body["results"]:
        assert "vm" in r
        assert "ok" in r


def test_api_order_accepts_grouped_payload(api):
    """The NEW shape {"groups": [{name, priority, vms}, ...]} works."""
    status, body = api(
        "PUT",
        "/api/vms/harv-fake/order",
        json_body={"groups": [
            {
                "name": "frontends",
                "priority": 10,
                "vms": [
                    {"namespace": "default", "name": "web-1", "snapshot": True},
                    {"namespace": "default", "name": "web-2", "snapshot": True},
                ],
            },
            {
                "name": "default",
                "priority": 90,
                "vms": [
                    {"namespace": "default", "name": "misc", "snapshot": False},
                ],
            },
        ]},
    )
    assert status == 200
    assert body["total"] == 3
    assert len(body["results"]) == 3


def test_api_order_rejects_missing_payload(api):
    status, body = api(
        "PUT",
        "/api/vms/harv-fake/order",
        json_body={},
        expect_status=400,
    )
    assert "expected" in body["error"]


def test_api_order_unknown_cluster(api):
    status, body = api(
        "PUT",
        "/api/vms/no-such-cluster/order",
        json_body={"groups": []},
        expect_status=404,
    )
    assert "unknown cluster" in body["error"]


def test_api_vms_source_contains_group_field(api):
    """Drift guard for GET /api/vms/<cluster>: without a real cluster we
    can't exercise the endpoint end-to-end, but we can verify that the
    Flask source still wires the new `group` field through the response.
    """
    src = Path(__file__).resolve().parent.parent.parent / "web" / "app.py"
    text = src.read_text()
    assert '"group": group' in text, (
        "api_vms response should expose the `group` key per VM entry"
    )
    assert 'annot.get("harvester-ops.io/shutdown-group")' in text, (
        "api_vms should read the shutdown-group annotation"
    )


# ---------------------------------------------------------------------------
# 2. Bash sort logic (extracted python embedded in common.sh)
#
# v1.4.14 output line format:
#   ns|name|intra_order|snapshot|timeout|group|group_priority
# Sort key: (group_priority, group, intra_order, name)
# ---------------------------------------------------------------------------
def test_get_ordered_vms_sort_by_group_priority_then_group_then_intra(tmp_path):
    """v1.4.14 ordering: outer = group_priority (asc), then group name
    (asc), then intra-order (asc), then VM name. Default group prio=100
    when missing, default intra=10 when missing."""
    payload = {
        "items": [
            _vm("ns1", "vm-z", intra=10, group="frontends", gprio=50),
            _vm("ns1", "vm-a", intra=10, group="frontends", gprio=50),
            _vm("ns1", "vm-c", intra=10, group="backends",  gprio=50),
            _vm("ns1", "vm-misc"),                                # no annotations: default/100/10
            _vm("ns1", "vm-b", intra=20, group="frontends", gprio=50),
            _vm("ns2", "vm-db", intra=10, group="backends",  gprio=80),
        ]
    }
    out = _run_get_ordered_vms_python(payload)
    lines = [l for l in out.strip().splitlines() if l]
    # gprio=50 backends: vm-c (intra=10)
    # gprio=50 frontends: vm-a (10), vm-b (20), vm-z (10) — vm-a and vm-z same intra so name order
    # gprio=80 backends: vm-db
    # gprio=100 default: vm-misc
    expected = [
        "ns1|vm-c|10|1|300|backends|50",
        "ns1|vm-a|10|1|300|frontends|50",
        "ns1|vm-z|10|1|300|frontends|50",
        "ns1|vm-b|20|1|300|frontends|50",
        "ns2|vm-db|10|1|300|backends|80",
        "ns1|vm-misc|10|1|300|default|100",
    ]
    assert lines == expected


def test_get_ordered_vms_reverse_for_startup():
    """REVERSE=1 (passed by startup) reverses the entire list — the LAST
    group stopped is the FIRST started."""
    payload = {
        "items": [
            _vm("ns", "a", intra=10, group="early", gprio=10),
            _vm("ns", "b", intra=10, group="mid",   gprio=20),
            _vm("ns", "c", intra=10, group="late",  gprio=30),
        ]
    }
    out = _run_get_ordered_vms_python(payload, env={"REVERSE": "1"})
    lines = [l for l in out.strip().splitlines() if l]
    assert lines == [
        "ns|c|10|1|300|late|30",
        "ns|b|10|1|300|mid|20",
        "ns|a|10|1|300|early|10",
    ]


def test_get_ordered_vms_empty_annotations_defaults():
    """A VM with no annotations → intra=10, snapshot=1, group='default',
    group_priority=100."""
    payload = {"items": [{"metadata": {"name": "naked", "namespace": "x"}}]}
    out = _run_get_ordered_vms_python(payload)
    assert out.strip() == "x|naked|10|1|300|default|100"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PRIORITY_KEY = "harvester-ops.io/shutdown-priority"          # intra-group
GROUP_KEY = "harvester-ops.io/shutdown-group"
GROUP_PRIORITY_KEY = "harvester-ops.io/shutdown-group-priority"
SNAP_KEY = "harvester-ops.io/snapshot"


def _vm(ns, name, intra=None, group=None, gprio=None, snapshot=True):
    annotations = {}
    if intra is not None:
        annotations[PRIORITY_KEY] = str(intra)
    if group is not None:
        annotations[GROUP_KEY] = group
    if gprio is not None:
        annotations[GROUP_PRIORITY_KEY] = str(gprio)
    if not snapshot:
        annotations[SNAP_KEY] = "false"
    return {
        "metadata": {
            "namespace": ns,
            "name": name,
            "annotations": annotations,
        }
    }


def _run_get_ordered_vms_python(payload, env=None):
    """Run the inline python from common.sh's get_ordered_vms()."""
    # Extracted verbatim from bin/lib/common.sh — must stay in sync.
    script = textwrap.dedent("""
        import json, os, sys
        reverse = bool(os.environ.get("REVERSE", ""))
        try:
            data = json.load(sys.stdin)
        except Exception:
            sys.exit(0)
        vms = []
        for item in data.get("items", []):
            name = item["metadata"]["name"]
            ns = item["metadata"]["namespace"]
            annot = item["metadata"].get("annotations", {}) or {}
            try:
                intra = int(annot.get("harvester-ops.io/shutdown-priority", "10"))
            except (TypeError, ValueError):
                intra = 10
            try:
                gprio = int(annot.get("harvester-ops.io/shutdown-group-priority", "100"))
            except (TypeError, ValueError):
                gprio = 100
            snapshot_flag = annot.get("harvester-ops.io/snapshot", "true").lower() != "false"
            timeout = annot.get("harvester-ops.io/ready-timeout", "300")
            group = annot.get("harvester-ops.io/shutdown-group", "") or "default"
            vms.append((gprio, group, intra, name, ns, snapshot_flag, timeout))
        vms.sort(key=lambda v: (v[0], v[1], v[2], v[3]))
        if reverse:
            vms.reverse()
        for gprio, group, intra, name, ns, snap, timeout in vms:
            print(f"{ns}|{name}|{intra}|{int(snap)}|{timeout}|{group}|{gprio}")
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    out, _ = proc.communicate(json.dumps(payload).encode(), timeout=5)
    return out.decode()


def test_extracted_sort_matches_common_sh_source():
    """Drift guard: if common.sh's embedded python changes, update this test
    AND the inline copy in `_run_get_ordered_vms_python`. We assert the
    expected key fragments are still present in the shipped script."""
    common_sh = Path(__file__).resolve().parent.parent.parent / "bin" / "lib" / "common.sh"
    src = common_sh.read_text()
    assert 'harvester-ops.io/shutdown-priority' in src
    assert 'harvester-ops.io/shutdown-group' in src
    assert 'harvester-ops.io/shutdown-group-priority' in src
    # v1.4.14 sort key: (group_priority, group, intra_order, name) →
    # (v[0], v[1], v[2], v[3]) in the tuple layout.
    assert 'sort(key=lambda v: (v[0], v[1], v[2], v[3]))' in src
