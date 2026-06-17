"""v1.5.3 — sidecar fetch + state enrichment.

The sidecar `<safe>.json` written by apply_declaration is the canonical
way to repopulate a deployed resource's spec into the section UI for
editing. This module locks in:
  - GET /api/terraform/<cluster>/sidecar/<safe>:
      * 200 with the JSON content when the file exists,
      * 404 when missing, when the cluster doesn't exist,
      * 400 on invalid `safe` names (path traversal defense),
  - GET /api/terraform/<cluster>/state now also returns
    `resources_detail` with `has_sidecar` + `kind` per address.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp


# ---------------------------------------------------------------------------
# /sidecar/<safe>
# ---------------------------------------------------------------------------

def test_sidecar_unknown_cluster_returns_404(api):
    status, payload = api(
        "GET", "/api/terraform/does-not-exist/sidecar/whatever",
        expect_status=None,
    )
    assert status == 404


def test_sidecar_invalid_safe_name_returns_400(api):
    """Anything outside [a-zA-Z0-9_]{1,128} is rejected before disk
    access — basic path-traversal defence."""
    # urllib won't even ship URLs containing some chars (spaces, …),
    # so we test what the *route* can receive — slashes route to a
    # different path (→ 404), and the long string + dotted forms hit
    # the regex (→ 400).
    for bad, expected in (
        ("a" * 129, 400),                    # too long → 400
        ("foo-bar", 400),                    # hyphen → 400 (regex excludes -)
        ("evil.path", 400),                  # dot → 400
        ("../wat", 404),                     # slash → route doesn't match → 404
    ):
        status, payload = api(
            "GET", f"/api/terraform/harv-fake/sidecar/{bad}",
            expect_status=None,
        )
        assert status == expected, (bad, status, payload)


def test_sidecar_missing_returns_404(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    with wapp.app.test_client() as c:
        r = c.get("/api/terraform/x/sidecar/nope")
        assert r.status_code == 404
        d = r.get_json()
        assert "sidecar not found" in (d.get("error") or "")


def test_sidecar_returns_full_json_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    payload = {
        "kind": "vm",
        "spec": {"name": "myvm", "cpu": 4, "disk": [{"size": "20Gi"}]},
        "declaration_name": "lab1",
        "written_at": "2026-06-03T14:00:00Z",
        "schema_version": 1,
    }
    (tmp_path / "myvm.json").write_text(json.dumps(payload))
    with wapp.app.test_client() as c:
        r = c.get("/api/terraform/x/sidecar/myvm")
        assert r.status_code == 200
        got = r.get_json()
        assert got["kind"] == "vm"
        assert got["spec"]["cpu"] == 4
        assert got["spec"]["disk"][0]["size"] == "20Gi"
        assert got["declaration_name"] == "lab1"


def test_sidecar_corrupted_json_returns_500(tmp_path, monkeypatch):
    """If the file is on disk but isn't valid JSON, the endpoint must
    return 500 with a helpful error — not a stack trace."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    (tmp_path / "corrupt.json").write_text("{ this isn't json")
    with wapp.app.test_client() as c:
        r = c.get("/api/terraform/x/sidecar/corrupt")
        assert r.status_code == 500
        d = r.get_json()
        assert "parse failed" in (d.get("error") or "")


def test_sidecar_does_not_escape_workspace(tmp_path, monkeypatch):
    """Defensive: even if `safe` were to slip through the regex (it
    won't — the regex is fairly strict), the resolved file's parent
    must still be the workspace. We construct a tmp tree to verify."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    # Place a sidecar in tmp_path AND a "rogue" one outside the
    # workspace. Pointing `safe` at a name with no extension should
    # still resolve to tmp_path/<safe>.json which won't exist.
    outside = tmp_path.parent / "outside.json"
    outside.write_text(json.dumps({"kind": "vm", "spec": {}}))
    with wapp.app.test_client() as c:
        r = c.get("/api/terraform/x/sidecar/outside")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# /state with resources_detail
# ---------------------------------------------------------------------------

def test_state_returns_resources_detail_with_sidecar_flag(tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    (tmp_path / ".terraform").mkdir()
    (tmp_path / "alpha.json").write_text(json.dumps({
        "kind": "vm", "spec": {}, "declaration_name": "lab"
    }))
    # beta has no sidecar
    monkeypatch.setattr(
        wapp, "_tf_run_cmd",
        lambda ws, kc, cmd, timeout=30: (
            0,
            "harvester_virtualmachine.alpha\nharvester_image.beta\n",
            "",
        ),
    )
    with wapp.app.test_client() as c:
        r = c.get("/api/terraform/x/state")
        assert r.status_code == 200
        d = r.get_json()
        # Legacy `resources` list still present for backward compat
        assert d["resources"] == [
            "harvester_virtualmachine.alpha", "harvester_image.beta",
        ]
        # The new detail list carries has_sidecar per row
        detail = d["resources_detail"]
        assert len(detail) == 2
        alpha = next(x for x in detail
                     if x["address"] == "harvester_virtualmachine.alpha")
        beta = next(x for x in detail
                    if x["address"] == "harvester_image.beta")
        assert alpha["has_sidecar"] is True
        assert alpha["kind"] == "vm"
        assert alpha["declaration_name"] == "lab"
        assert beta["has_sidecar"] is False


def test_state_uninitialized_workspace_has_empty_detail(tmp_path,
                                                          monkeypatch):
    """The uninitialised-workspace branch must also return the new
    `resources_detail` key (empty list) — the UI iterates it
    unconditionally."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    with wapp.app.test_client() as c:
        r = c.get("/api/terraform/x/state")
        assert r.status_code == 200
        d = r.get_json()
        assert d["initialized"] is False
        assert d["resources"] == []
        assert d["resources_detail"] == []
