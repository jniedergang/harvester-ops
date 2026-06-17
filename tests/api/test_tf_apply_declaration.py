"""v1.5.0 — POST /api/terraform/<cluster>/apply_declaration tests.

Locks in the multi-resource apply contract:
  - validation rejects empty / incomplete declarations BEFORE any
    background work;
  - the runner writes <safe>.tf AND <safe>.json (sidecar) for every
    resource;
  - the sidecar carries enough metadata (kind, spec, declaration_name,
    written_at) for v1.5.1 to reconstruct the spec when the user edits
    a deployed resource;
  - _providers.tf is created once, regardless of how many resources;
  - clashing safe names (two VMs called "node") are auto-disambiguated.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp


def _vm(name, image="default/img-x"):
    return {
        "kind": "vm",
        "spec": {
            "name": name,
            "namespace": "default",
            "cpu": 2,
            "memory": "4Gi",
            "disk": [{"image": image, "size": "10Gi"}],
            "network_interface": [{"network_name": "default/management"}],
        },
    }


def _image(name="img-1"):
    return {
        "kind": "image",
        "spec": {
            "name": name, "display_name": name,
            "source_type": "download",
            "url": "https://example.com/x.qcow2",
        },
    }


def _sshkey(name="k1"):
    return {
        "kind": "ssh_key",
        "spec": {
            "name": name, "namespace": "default",
            "public_key": "ssh-ed25519 AAAA my@host",
        },
    }


# ---------------------------------------------------------------------------
# HTTP-surface validation (no runner)
# ---------------------------------------------------------------------------

def test_unknown_cluster_returns_404(api):
    status, payload = api(
        "POST", "/api/terraform/does-not-exist/apply_declaration",
        {"declaration": {"name": "x", "resources": [_vm("a")]}, "dry_run": True},
        expect_status=None,
    )
    assert status == 404


def test_empty_resources_returns_400(api):
    status, payload = api(
        "POST", "/api/terraform/harv-fake/apply_declaration",
        {"declaration": {"name": "x", "resources": []}, "dry_run": True},
        expect_status=None,
    )
    assert status == 400
    assert "non-empty" in (payload.get("error") or "")


def test_missing_declaration_field_returns_400(api):
    status, _ = api(
        "POST", "/api/terraform/harv-fake/apply_declaration",
        {"dry_run": True}, expect_status=None,
    )
    assert status == 400


def test_invalid_resource_returns_400_with_details(api):
    """A VM without disk/nic renders to "" — endpoint must surface this
    with `errors: [{index, kind, name, error}]` BEFORE spawning the
    runner."""
    bad = {"kind": "vm", "spec": {"name": "no-disk"}}
    status, payload = api(
        "POST", "/api/terraform/harv-fake/apply_declaration",
        {"declaration": {"name": "x",
                          "resources": [_vm("good"), bad, _sshkey("k")]},
         "dry_run": True},
        expect_status=None,
    )
    assert status == 400
    assert "errors" in payload
    assert len(payload["errors"]) == 1
    err = payload["errors"][0]
    assert err["index"] == 1
    assert err["kind"] == "vm"
    assert err["name"] == "no-disk"


def test_valid_payload_returns_action_id(api):
    """Happy path: the endpoint acks with `action_id`; the runner spins
    up async (and likely fails on the fake cluster but that's not what
    we're checking)."""
    status, payload = api(
        "POST", "/api/terraform/harv-fake/apply_declaration",
        {"declaration": {"name": "bundle1",
                          "resources": [_vm("a"), _sshkey("k1")]},
         "dry_run": True},
        expect_status=None,
    )
    assert status == 201, payload
    assert "action_id" in payload
    assert len(payload["action_id"]) == 12


# ---------------------------------------------------------------------------
# Runner behaviour — we stub _tf_run_cmd so terraform is never actually
# invoked, and inspect the files the runner wrote.
# ---------------------------------------------------------------------------

def test_runner_writes_tf_and_json_sidecar_per_resource(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "0.6.7")
    monkeypatch.setattr(wapp, "_tf_run_cmd",
                        lambda ws, kc, cmd, timeout=180: (0, "ok", ""))
    src_kc = tmp_path.parent / f"src-kc-{tmp_path.name}"
    src_kc.write_text("apiVersion: v1\nkind: Config\n")

    rendered = []
    for spec in (_vm("alpha")["spec"], _sshkey("k1")["spec"],
                  _image("img-1")["spec"]):
        kind = "vm" if "disk" in spec else ("ssh_key" if "public_key" in spec
                                              else "image")
        hcl = wapp._render_tf_for_kind(kind, spec)
        rendered.append((spec["name"].replace("-", "_"), kind, spec, hcl))

    run = wapp.ActionRun("test-decl", "tf-apply-decl:bundle1:3",
                          "x", [], dry_run=True)
    wapp._tf_apply_declaration_runner(run, "x", str(src_kc), rendered,
                                        dry_run=True,
                                        declaration_name="bundle1")

    # _providers.tf exists with the header EXACTLY once
    providers = (tmp_path / "_providers.tf").read_text()
    assert "required_providers" in providers
    # Each resource has its .tf AND its .json sidecar
    for safe in ("alpha", "k1", "img_1"):
        tf = tmp_path / f"{safe}.tf"
        side = tmp_path / f"{safe}.json"
        assert tf.exists(), f"{safe}.tf missing"
        assert side.exists(), f"{safe}.json missing"
        body = tf.read_text()
        assert wapp._TF_HEADER not in body, (
            f"{safe}.tf still carries the shared header"
        )
        meta = json.loads(side.read_text())
        for k in ("kind", "spec", "declaration_name", "written_at",
                  "schema_version"):
            assert k in meta, f"sidecar missing {k!r}"
        assert meta["declaration_name"] == "bundle1"


def test_sidecar_carries_full_spec_for_v1_5_1_edit(tmp_path, monkeypatch):
    """v1.5.1 will load the sidecar to repopulate the form. That means
    every field the user typed has to round-trip through the JSON."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "0.6.7")
    monkeypatch.setattr(wapp, "_tf_run_cmd",
                        lambda *a, **k: (0, "", ""))
    src_kc = tmp_path.parent / f"src-kc-{tmp_path.name}"
    src_kc.write_text("k")

    spec = {
        "name": "fullvm",
        "namespace": "default",
        "cpu": 4,
        "memory": "16Gi",
        "run_strategy": "Always",
        "hostname": "fullhost",
        "efi": True, "secure_boot": True,
        "ssh_keys": ["default/k1", "default/k2"],
        "description": "long-running",
        "disk": [
            {"name": "root", "image": "default/img-x", "size": "30Gi"},
            {"name": "data", "size": "200Gi", "storage_class_name": "longhorn"},
        ],
        "network_interface": [{"network_name": "default/mgmt"}],
        "cloudinit": [{"user_data": "#cloud-config\nhostname: fullhost"}],
    }
    hcl = wapp._render_tf_for_kind("vm", spec)
    assert hcl
    rendered = [("fullvm", "vm", spec, hcl)]

    run = wapp.ActionRun("run-id", "lbl", "x", [], dry_run=True)
    wapp._tf_apply_declaration_runner(run, "x", str(src_kc), rendered,
                                        dry_run=True,
                                        declaration_name="edit-test")

    side = json.loads((tmp_path / "fullvm.json").read_text())
    s = side["spec"]
    # Every meaningful field present
    assert s["cpu"] == 4
    assert s["ssh_keys"] == ["default/k1", "default/k2"]
    assert s["efi"] is True
    assert s["disk"][0]["image"] == "default/img-x"
    assert s["disk"][1]["storage_class_name"] == "longhorn"
    assert s["cloudinit"][0]["user_data"].startswith("#cloud-config")


def test_runner_disambiguates_clashing_safe_names(tmp_path, monkeypatch):
    """Two VMs both named "node" produce the same safe filename. The
    endpoint pre-suffixes the second one (_2, _3, …)."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "0.6.7")
    monkeypatch.setattr(wapp, "_tf_run_cmd", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(wapp, "_kubectl_for_cluster",
                        lambda c: "/dev/null")
    import shutil as _sh
    monkeypatch.setattr(_sh, "copyfile", lambda *a, **k: None)
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/whatever/apply_declaration", json={
            "declaration": {
                "name": "twins",
                "resources": [_vm("node"), _vm("node")],
            },
            "dry_run": True,
        })
        assert r.status_code == 201, r.get_data(as_text=True)
    # The runner runs in a background thread — give it a moment, then
    # verify both files exist.
    import time as _time
    for _ in range(20):
        if (tmp_path / "node.tf").exists() and \
           (tmp_path / "node_2.tf").exists():
            break
        _time.sleep(0.1)
    assert (tmp_path / "node.tf").exists()
    assert (tmp_path / "node_2.tf").exists()
    assert (tmp_path / "node.json").exists()
    assert (tmp_path / "node_2.json").exists()


# ---------------------------------------------------------------------------
# destroy_declaration (v1.5.1)
# ---------------------------------------------------------------------------

def test_destroy_declaration_unknown_cluster_returns_404(api):
    status, payload = api(
        "POST", "/api/terraform/does-not-exist/destroy_declaration",
        {"declaration": {"name": "d", "resources": [_vm("a")]}, "dry_run": False},
        expect_status=None,
    )
    assert status == 404


def test_destroy_declaration_empty_resources_returns_400(api):
    status, _ = api(
        "POST", "/api/terraform/harv-fake/destroy_declaration",
        {"declaration": {"name": "d", "resources": []}, "dry_run": False},
        expect_status=None,
    )
    assert status == 400


def test_destroy_declaration_returns_action_id(api):
    """Happy path: HTTP acks even when the workspace isn't initialised
    on the fake cluster — the runner surfaces that via SSE."""
    status, payload = api(
        "POST", "/api/terraform/harv-fake/destroy_declaration",
        {"declaration": {"name": "d", "resources": [_vm("a"), _sshkey("k")]},
         "dry_run": False},
        expect_status=None,
    )
    assert status == 201, payload
    assert "action_id" in payload


def test_destroy_declaration_addresses_match_kinds():
    """The kind → terraform-type mapping is what makes -target=<addr>
    work. Lock it in; a typo here would silently destroy nothing."""
    assert wapp._KIND_TO_TF_TYPE["vm"] == "harvester_virtualmachine"
    assert wapp._KIND_TO_TF_TYPE["image"] == "harvester_image"
    assert wapp._KIND_TO_TF_TYPE["ssh_key"] == "harvester_ssh_key"
    # Address computed from (kind, safe_name)
    assert wapp._tf_address_for_resource("vm", "node1") == \
           "harvester_virtualmachine.node1"
    assert wapp._tf_address_for_resource("ssh_key", "lab_key") == \
           "harvester_ssh_key.lab_key"
    # Unknown kind → None (causes a 400 endpoint-side)
    assert wapp._tf_address_for_resource("unknown", "x") is None


def test_destroy_declaration_address_from_raw_hcl():
    """For `raw` we grep the HCL itself to find resource <type> <name>."""
    hcl = (
        'resource "harvester_virtualmachine" "rawvm" {\n'
        '  name = "rawvm"\n}\n'
    )
    addr = wapp._tf_address_for_resource("raw", "ignored", hcl=hcl)
    assert addr == "harvester_virtualmachine.rawvm"


def test_destroy_declaration_runner_targets_only_resources_in_state(
        tmp_path, monkeypatch):
    """The runner runs `terraform state list`, intersects it with the
    declaration's addresses, and only -target=<addr>'s those. A
    resource not in state is logged as `skip <addr>` and not
    destroyed."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    (tmp_path / ".terraform").mkdir()
    # Stage the .tf + .json sidecars that exist on disk for the
    # in-state resource. The other one (not in state) should not get
    # its files removed either.
    (tmp_path / "alpha.tf").write_text(
        'resource "harvester_virtualmachine" "alpha" {}\n')
    (tmp_path / "alpha.json").write_text(json.dumps({"kind": "vm"}))
    (tmp_path / "beta.tf").write_text(
        'resource "harvester_virtualmachine" "beta" {}\n')

    # state list returns only `alpha`; beta is "not in state"
    calls = []
    def fake_run_cmd(ws, kc, cmd, timeout=180):
        calls.append(list(cmd))
        if cmd[:2] == ["state", "list"]:
            return (0, "harvester_virtualmachine.alpha\n", "")
        return (0, "Destroy complete!", "")
    monkeypatch.setattr(wapp, "_tf_run_cmd", fake_run_cmd)
    src_kc = tmp_path.parent / f"src-kc-{tmp_path.name}"
    src_kc.write_text("k")
    planned = [
        ("alpha", "harvester_virtualmachine.alpha"),
        ("beta",  "harvester_virtualmachine.beta"),
    ]
    run = wapp.ActionRun("rid", "lbl", "x", [], dry_run=False)
    wapp._tf_destroy_declaration_runner(run, "x", str(src_kc), planned,
                                          dry_run=False,
                                          declaration_name="d")
    # destroy was invoked with -target=alpha ONLY (the in-state one).
    destroy_call = [c for c in calls if c and c[0] == "destroy"]
    assert destroy_call, calls
    args = destroy_call[0]
    assert "-target" in args
    targets = [args[i + 1] for i, a in enumerate(args) if a == "-target"]
    assert targets == ["harvester_virtualmachine.alpha"], targets
    # alpha's files are gone; beta's are preserved (it was skipped)
    assert not (tmp_path / "alpha.tf").exists()
    assert not (tmp_path / "alpha.json").exists()
    assert (tmp_path / "beta.tf").exists()


def test_destroy_declaration_runner_no_state_match_short_circuits(
        tmp_path, monkeypatch):
    """If none of the declaration's resources are in state, the runner
    must NOT call `terraform destroy` (that would attempt to destroy
    everything in the workspace). It returns `done` with an explanatory
    log line."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    (tmp_path / ".terraform").mkdir()
    src_kc = tmp_path.parent / f"src-kc-{tmp_path.name}"
    src_kc.write_text("k")
    calls = []
    def fake_run_cmd(ws, kc, cmd, timeout=180):
        calls.append(list(cmd))
        if cmd[:2] == ["state", "list"]:
            return (0, "", "")
        return (0, "Destroy complete!", "")
    monkeypatch.setattr(wapp, "_tf_run_cmd", fake_run_cmd)
    planned = [("alpha", "harvester_virtualmachine.alpha")]
    run = wapp.ActionRun("rid", "lbl", "x", [], dry_run=False)
    wapp._tf_destroy_declaration_runner(run, "x", str(src_kc), planned,
                                          dry_run=False,
                                          declaration_name="d")
    # state list was called, destroy was NOT
    assert any(c[:2] == ["state", "list"] for c in calls)
    assert not any(c and c[0] == "destroy" for c in calls)
    assert run.status == "done"


def test_destroy_declaration_dry_run_keeps_files(tmp_path, monkeypatch):
    """Dry-run must NOT unlink .tf / .json (it's a plan only)."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    (tmp_path / ".terraform").mkdir()
    (tmp_path / "alpha.tf").write_text(
        'resource "harvester_virtualmachine" "alpha" {}\n')
    (tmp_path / "alpha.json").write_text(json.dumps({"kind": "vm"}))
    src_kc = tmp_path.parent / f"src-kc-{tmp_path.name}"
    src_kc.write_text("k")
    def fake_run_cmd(ws, kc, cmd, timeout=180):
        if cmd[:2] == ["state", "list"]:
            return (0, "harvester_virtualmachine.alpha\n", "")
        return (0, "Plan: 1 to destroy", "")
    monkeypatch.setattr(wapp, "_tf_run_cmd", fake_run_cmd)
    planned = [("alpha", "harvester_virtualmachine.alpha")]
    run = wapp.ActionRun("rid", "lbl", "x", [], dry_run=True)
    wapp._tf_destroy_declaration_runner(run, "x", str(src_kc), planned,
                                          dry_run=True,
                                          declaration_name="d")
    # Files preserved
    assert (tmp_path / "alpha.tf").exists()
    assert (tmp_path / "alpha.json").exists()


def test_runner_does_not_duplicate_providers_across_runs(tmp_path,
                                                          monkeypatch):
    """Two consecutive apply_declaration calls share the same _providers.tf
    — never duplicate it."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "0.6.7")
    monkeypatch.setattr(wapp, "_tf_run_cmd", lambda *a, **k: (0, "", ""))
    src_kc = tmp_path.parent / f"src-kc-{tmp_path.name}"
    src_kc.write_text("k")

    def _run(name):
        spec = _vm(name)["spec"]
        hcl = wapp._render_tf_for_kind("vm", spec)
        rendered = [(name, "vm", spec, hcl)]
        run = wapp.ActionRun(f"r-{name}", "lbl", "x", [], dry_run=True)
        wapp._tf_apply_declaration_runner(run, "x", str(src_kc), rendered,
                                            dry_run=True,
                                            declaration_name=f"d-{name}")
    _run("first")
    _run("second")
    providers = (tmp_path / "_providers.tf").read_text()
    assert providers.count("required_providers") == 1
