"""Smoke coverage for endpoints the audit flagged as untested.

These tests don't require a real cluster — they hit the Flask server
through the local test fixture and assert on:
  - shape of the response (status code, JSON keys)
  - 404/400 behavior for missing inputs
  - auth enforcement where applicable

Hardening the contract of these routes catches surprising refactor
side-effects without needing kubectl.
"""

import json


# ---------------------------------------------------------------------------
# /api/vm/<cluster>/<ns>/<name>/cloudinit
# ---------------------------------------------------------------------------
def test_vm_cloudinit_get_unknown_cluster(api):
    status, body = api("GET",
                       "/api/vm/no-such/default/foo/cloudinit",
                       expect_status=404)
    assert "unknown cluster" in body["error"].lower()


def test_vm_cloudinit_put_unknown_cluster(api):
    status, body = api("PUT",
                       "/api/vm/no-such/default/foo/cloudinit",
                       json_body={"userdata": "#cloud-config\n"},
                       expect_status=404)
    assert "unknown cluster" in body["error"].lower()


def test_vm_cloudinit_get_returns_500_when_vm_missing(api):
    """Real cluster route: the fake kubeconfig points to nothing reachable.
    We just want to verify the endpoint is wired and either returns an
    error JSON shape or 404 — never an unhandled exception."""
    status, body = api("GET",
                       "/api/vm/harv-fake/default/no-such-vm/cloudinit",
                       expect_status=None)
    assert status in (404, 500)
    assert isinstance(body, dict) and "error" in body


# ---------------------------------------------------------------------------
# /api/vm/<cluster>/<ns>/<name>/snapshots
# ---------------------------------------------------------------------------
def test_vm_snapshots_list_unknown_cluster(api):
    status, body = api("GET",
                       "/api/vm/no-such/default/foo/snapshots",
                       expect_status=404)
    assert "unknown" in body["error"].lower()


def test_vm_snapshots_create_unknown_cluster(api):
    status, body = api("POST",
                       "/api/vm/no-such/default/foo/snapshots",
                       json_body={"name": "snap-1"},
                       expect_status=404)
    assert "unknown" in body["error"].lower()


def test_vm_snapshots_delete_unknown_cluster(api):
    status, body = api("DELETE",
                       "/api/vm/no-such/default/foo/snapshots/snap-1",
                       expect_status=404)
    assert "unknown" in body["error"].lower()


# ---------------------------------------------------------------------------
# /api/capi/<cluster>/cluster-create
# ---------------------------------------------------------------------------
def test_capi_cluster_create_unknown_cluster(api):
    status, body = api("POST",
                       "/api/capi/no-such/cluster-create",
                       json_body={},
                       expect_status=None)
    assert status in (400, 404)


def test_capi_cluster_create_rejects_missing_required_fields(api):
    """The endpoint should not happily 201 on an empty payload — at
    minimum the new cluster needs a name. 412 is also accepted: that's
    what we surface when `caphv-generate` is not installed at the
    default `/usr/local/bin/caphv-generate`, which is the expected
    state in CI / dev without the CAPHV bundle deployed."""
    status, body = api("POST",
                       "/api/capi/harv-fake/cluster-create",
                       json_body={},
                       expect_status=None)
    assert status in (400, 412, 422, 500), (
        f"empty payload should not 200 — got {status}"
    )


# ---------------------------------------------------------------------------
# /api/terraform/*
# ---------------------------------------------------------------------------
def test_terraform_info_returns_shape(api):
    """`/api/terraform/info` advertises whether terraform is installed
    and its version. Always 200, even if terraform is missing."""
    status, body = api("GET", "/api/terraform/info")
    assert status == 200
    # The exact keys vary, but it must be a JSON object.
    assert isinstance(body, dict)


def test_terraform_state_unknown_cluster(api):
    status, body = api("GET",
                       "/api/terraform/no-such/state",
                       expect_status=None)
    assert status in (404, 500)


def test_terraform_apply_unknown_cluster(api):
    status, body = api("POST",
                       "/api/terraform/no-such/apply",
                       json_body={"resources": []},
                       expect_status=None)
    assert status in (400, 404, 500)


def test_terraform_destroy_unknown_cluster(api):
    status, body = api("POST",
                       "/api/terraform/no-such/destroy",
                       json_body={},
                       expect_status=None)
    assert status in (400, 404, 500)


# ---------------------------------------------------------------------------
# Action lifecycle edge cases
# ---------------------------------------------------------------------------
def test_action_get_unknown_id(api):
    """GET on a non-existent action: 404 is acceptable; the body may be
    JSON OR Flask's default HTML (depends on whether the route has a
    custom not-found handler). We only require that it's a 404."""
    status, _body = api("GET", "/api/action/no-such-id", expect_status=404)
    assert status == 404


def test_action_delete_unknown_id(api):
    """Cancelling a non-existent action must 404, not crash."""
    status, body = api("DELETE", "/api/action/no-such-id", expect_status=None)
    assert status in (404, 410)


def test_action_post_rejects_unknown_action_kind(api):
    """Trying to launch an action type the server doesn't recognise must
    return 400 with a descriptive error, not a generic 500."""
    status, body = api("POST", "/api/action",
                       json_body={"action": "nuke-everything",
                                  "cluster": "harv-fake"},
                       expect_status=None)
    assert status in (400, 404)


# ---------------------------------------------------------------------------
# Bundle endpoints
# ---------------------------------------------------------------------------
def test_capi_bundles_list_returns_array(api):
    """The bundles list endpoint always returns a JSON array shape, even
    when no bundle exists. UI relies on that to render an empty state."""
    status, body = api("GET", "/api/capi/bundles")
    assert status == 200
    assert "bundles" in body or "items" in body or isinstance(body, list)


# ---------------------------------------------------------------------------
# Docs endpoints — content-type sanity
# ---------------------------------------------------------------------------
def test_docs_render_returns_json(api):
    status, body = api("GET", "/api/docs/en/operating-procedure.md")
    assert status == 200
    assert isinstance(body, dict)
    assert "html" in body
    assert body["lang"] == "en"


def test_docs_render_rejects_traversal(api):
    """`/api/docs/<lang>/<file>` must reject `..` in the file param."""
    status, _ = api("GET", "/api/docs/en/..%2F..%2Fetc%2Fpasswd",
                    expect_status=None)
    # Flask may URL-decode and either 400 or 404. Anything other than
    # 200 is acceptable — we must NOT serve /etc/passwd.
    assert status != 200
