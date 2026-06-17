"""v1.4.39 — HTTP-surface tests for every /api/terraform/* route.

These exercise:
  - info                       (provider + CLI metadata)
  - <cluster>/state            (initialized / uninitialized)
  - <cluster>/apply            (dispatch, body validation, ack shape)
  - <cluster>/destroy          (whole-workspace destroy)
  - <cluster>/destroy_resource (targeted; validation guards)
  - <cluster>/clean_stale      (workspace .tf cleanup)
  - bundle/build               (job acknowledgement)

We mock `_tf_run_cmd` so terraform is never actually invoked — these
are HTTP contract tests, not integration tests. The renderer
correctness lives in `test_tf_render.py`; concurrency / workspace
mechanics in `test_tf_workspace.py`.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp


# ---------------------------------------------------------------------------
# /api/terraform/info
# ---------------------------------------------------------------------------

def test_info_returns_provider_metadata(api):
    status, payload = api("GET", "/api/terraform/info")
    assert status == 200
    # Schema contract — these keys must exist even when the provider
    # binary isn't bundled (then the values are "" / 0 / False).
    for k in ("provider_repo", "provider_version", "provider_binary",
              "provider_binary_size", "terraform_bin", "terraform_available",
              "workspaces_dir", "examples_dir", "example_resources"):
        assert k in payload, f"info payload missing key {k!r}: {payload}"
    assert isinstance(payload["example_resources"], list)
    assert isinstance(payload["terraform_available"], bool)


# ---------------------------------------------------------------------------
# /api/terraform/<cluster>/state
# ---------------------------------------------------------------------------

def test_state_unknown_cluster_404(api):
    status, payload = api("GET", "/api/terraform/does-not-exist/state",
                          expect_status=None)
    assert status == 404
    assert "does-not-exist" in (payload.get("error") or "")


def test_state_uninitialized_workspace_reports_so(api, tmp_path,
                                                   monkeypatch):
    """Brand-new cluster (workspace dir doesn't have .terraform) must
    return `initialized: false` with an empty resources list — NOT a
    crash. The UI relies on this to render the "workspace not
    initialized" badge."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster",
                        lambda c: "/dev/null")
    with wapp.app.test_client() as c:
        r = c.get("/api/terraform/harv-fake/state")
        assert r.status_code == 200
        d = r.get_json()
        assert d["initialized"] is False
        assert d["resources"] == []
        assert d["workspace"] == str(tmp_path)


def test_state_initialized_lists_resources(api, tmp_path, monkeypatch):
    """When the workspace has .terraform/, the endpoint shells out to
    `terraform state list` and returns each non-empty line."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    (tmp_path / ".terraform").mkdir()
    monkeypatch.setattr(
        wapp, "_tf_run_cmd",
        lambda ws, kc, cmd, timeout=30: (
            0,
            "harvester_virtualmachine.a\n"
            "\n"  # blank line must be filtered
            "harvester_image.b\n",
            "",
        ),
    )
    # Make _kubectl_for_cluster succeed so we don't 404
    monkeypatch.setattr(wapp, "_kubectl_for_cluster",
                        lambda c: "/dev/null")
    with wapp.app.test_client() as c:
        r = c.get("/api/terraform/whatever/state")
        assert r.status_code == 200
        d = r.get_json()
        assert d["initialized"] is True
        assert d["resources"] == [
            "harvester_virtualmachine.a",
            "harvester_image.b",
        ]
        assert d["resource_count"] == 2


# ---------------------------------------------------------------------------
# /api/terraform/<cluster>/apply
# ---------------------------------------------------------------------------

def test_apply_unknown_cluster_404(api):
    status, payload = api(
        "POST", "/api/terraform/does-not-exist/apply",
        {"kind": "vm", "spec": {"name": "x"}, "dry_run": True},
        expect_status=None,
    )
    assert status == 404
    assert "does-not-exist" in (payload.get("error") or "")


def test_apply_unknown_kind_returns_400_with_supported_list(api):
    """A typo in `kind` must surface a structured error so the UI can
    render the right hint, not a 500."""
    status, payload = api(
        "POST", "/api/terraform/harv-fake/apply",
        {"kind": "totally_bogus", "spec": {"name": "x"}, "dry_run": True},
        expect_status=None,
    )
    assert status == 400
    assert "totally_bogus" in (payload.get("error") or "")
    assert payload.get("supported") == ["vm", "image", "ssh_key", "raw"]


def test_apply_missing_required_fields_returns_400(api):
    """vm without disk / nic → `_render_tf_for_kind` returns "" → apply
    surfaces 400 with the same supported list."""
    status, payload = api(
        "POST", "/api/terraform/harv-fake/apply",
        {"kind": "vm", "spec": {"name": "v1"}, "dry_run": True},
        expect_status=None,
    )
    assert status == 400


def test_apply_valid_payload_returns_action_id(monkeypatch):
    """Happy path: backend acks with `action_id` and the runner is
    detached in a background thread. The thread itself never invokes
    terraform here (we stub _tf_run_cmd to no-op)."""
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "0.6.7")
    monkeypatch.setattr(wapp, "_tf_run_cmd",
                        lambda ws, kc, cmd, timeout=180: (0, "", ""))
    # Don't let the runner copy /dev/null to a real workspace
    import shutil
    monkeypatch.setattr(shutil, "copyfile", lambda *a, **kw: None)

    body = {
        "kind": "vm",
        "spec": {
            "name": "ack-test",
            "disk": [{"image": "default/img-x", "size": "10Gi"}],
            "network_interface": [{"network_name": "default/management"}],
        },
        "dry_run": True,
    }
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/harv-fake/apply", json=body)
        assert r.status_code == 201
        d = r.get_json()
        assert "action_id" in d
        assert len(d["action_id"]) == 12  # uuid4().hex[:12]


# ---------------------------------------------------------------------------
# /api/terraform/<cluster>/destroy
# ---------------------------------------------------------------------------

def test_destroy_workspace_unknown_cluster_404(api):
    status, payload = api(
        "POST", "/api/terraform/does-not-exist/destroy",
        {"dry_run": True}, expect_status=None,
    )
    assert status == 404


def test_destroy_workspace_returns_action_id(api):
    """The endpoint always returns 201 + action_id even when the
    workspace isn't initialized — the runner detects that and surfaces
    the error via the action stream."""
    status, payload = api(
        "POST", "/api/terraform/harv-fake/destroy",
        {"dry_run": True}, expect_status=None,
    )
    assert status == 201
    assert "action_id" in payload


# ---------------------------------------------------------------------------
# /api/terraform/<cluster>/clean_stale  (v1.4.38)
# ---------------------------------------------------------------------------

def test_clean_stale_workspace_does_not_exist_returns_404(api,
                                                           tmp_path,
                                                           monkeypatch):
    """A workspace that hasn't been created yet must 404 cleanly."""
    nowhere = tmp_path / "never-existed"
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: nowhere)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster",
                        lambda c: "/dev/null")
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/whatever/clean_stale",
                   json={"dry_run": True})
        assert r.status_code == 404
        assert "workspace" in (r.get_json().get("error") or "")


def test_clean_stale_empty_workspace_returns_empty_lists(api, tmp_path,
                                                          monkeypatch):
    """An initialised but empty workspace has nothing stale → dry-run
    yields would_remove=[]. UI displays the "already clean" message."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    (tmp_path / ".terraform").mkdir()
    (tmp_path / "_providers.tf").write_text(wapp._TF_HEADER)
    # `state list` returns nothing
    src_kc = tmp_path.parent / f"src-kc-{tmp_path.name}"
    src_kc.write_text("k")
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: str(src_kc))
    monkeypatch.setattr(
        wapp, "_tf_run_cmd",
        lambda ws, kc, cmd, timeout=30: (0, "", ""),
    )
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/x/clean_stale", json={"dry_run": True})
        assert r.status_code == 200
        d = r.get_json()
        assert d["would_remove"] == []
        assert d["in_state"] == []


# ---------------------------------------------------------------------------
# /api/terraform/<cluster>/destroy_resource (v1.4.38)
# ---------------------------------------------------------------------------

def test_destroy_resource_valid_address_returns_action_id(api):
    """Happy path: 201 + action_id (the runner does the real work
    async)."""
    status, payload = api(
        "POST", "/api/terraform/harv-fake/destroy_resource",
        {"address": "harvester_virtualmachine.foo", "dry_run": True},
        expect_status=None,
    )
    # workspace might not be initialized on harv-fake; the runner will
    # surface that. But the HTTP layer must accept and ack.
    assert status == 201, payload
    assert "action_id" in payload


def test_destroy_resource_missing_address_400(api):
    """Empty / absent `address` → 400, never reaches the runner."""
    status, payload = api(
        "POST", "/api/terraform/harv-fake/destroy_resource",
        {"address": "", "dry_run": True},
        expect_status=None,
    )
    assert status == 400


# ---------------------------------------------------------------------------
# /api/terraform/bundle/build
# ---------------------------------------------------------------------------

def test_bundle_build_returns_action_id(api):
    """The bundle endpoint launches a background job; the HTTP layer
    must ack with 201 + action_id even on the fake config."""
    status, payload = api(
        "POST", "/api/terraform/bundle/build", {}, expect_status=None,
    )
    assert status in (201, 200), payload
    assert "action_id" in payload
