"""v1.4.37 — fix for the "Duplicate required providers" terraform error.

Repro: in v1.4.36, every `_render_tf_for_kind()` output started with the
shared `terraform { required_providers }` + `provider "harvester"` blocks.
The runner wrote that to a per-resource <name>.tf file, so a workspace
with two resources had two declarations and `terraform plan` failed with:

    Error: Duplicate required providers configuration
      A module may have only one required providers configuration.

These tests lock in the v1.4.37 split: the header goes to _providers.tf
(written ONCE per workspace), the per-resource .tf carries only its
`resource { … }` block, and existing pre-1.4.37 .tf files get their
header stripped on the first apply post-upgrade.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp


def test_render_emits_header_so_strip_can_remove_it():
    """_render_tf_for_kind('vm') must start with _TF_HEADER (because the
    raw-HCL kind needs it self-contained), and the runner strips it
    before writing the per-resource file."""
    spec = {
        "name": "t1", "namespace": "default",
        "disk": [{"image": "default/img-x", "size": "10Gi"}],
        "network_interface": [{"network_name": "default/management"}],
    }
    out = wapp._render_tf_for_kind("vm", spec)
    assert out.startswith(wapp._TF_HEADER), out[:200]


def test_runner_writes_separate_providers_tf(tmp_path, monkeypatch):
    """Stub _tf_run_cmd so we don't actually invoke terraform; verify the
    workspace ends up with _providers.tf (header only) and the resource
    .tf (header-stripped)."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "0.6.7")
    monkeypatch.setattr(wapp, "_tf_run_cmd",
                        lambda ws, kc, cmd, timeout=180: (0, "Plan: 1 to add", ""))
    fake_kc = tmp_path / "kc"
    fake_kc.write_text("apiVersion: v1\nkind: Config\n")

    # Build a synthetic run object
    run = wapp.ActionRun("test-run", "tf-apply:vm:t1", "x", [], dry_run=True)

    spec = {
        "name": "t1", "namespace": "default",
        "disk": [{"image": "default/img-x", "size": "10Gi"}],
        "network_interface": [{"network_name": "default/management"}],
    }
    tf_content = wapp._render_tf_for_kind("vm", spec)
    wapp._tf_apply_runner(run, "x", str(fake_kc), tf_content,
                          dry_run=True, resource_name="t1")

    providers = tmp_path / "_providers.tf"
    resource  = tmp_path / "t1.tf"
    assert providers.exists(), "header file must be written"
    assert wapp._TF_HEADER in providers.read_text()
    assert resource.exists(), "resource file must be written"
    rtxt = resource.read_text()
    assert wapp._TF_HEADER not in rtxt, (
        "resource .tf must NOT carry the shared header — that's the v1.4.36 "
        "bug we are fixing"
    )
    assert 'resource "harvester_virtualmachine"' in rtxt


def test_runner_migrates_pre_v1_4_37_workspaces(tmp_path, monkeypatch):
    """When _providers.tf does not yet exist but old sibling .tf files
    carrying the inlined header are already on disk, the first apply
    post-upgrade must strip the duplicate headers."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "0.6.7")
    monkeypatch.setattr(wapp, "_tf_run_cmd",
                        lambda ws, kc, cmd, timeout=180: (0, "", ""))
    fake_kc = tmp_path / "kc"
    fake_kc.write_text("apiVersion: v1\nkind: Config\n")

    # Simulate two leftover resource .tf files from v1.4.36
    legacy_1 = tmp_path / "legacy_a.tf"
    legacy_2 = tmp_path / "legacy_b.tf"
    body_a = 'resource "harvester_virtualmachine" "a" { name = "a" }\n'
    body_b = 'resource "harvester_virtualmachine" "b" { name = "b" }\n'
    legacy_1.write_text(wapp._TF_HEADER + body_a)
    legacy_2.write_text(wapp._TF_HEADER + body_b)

    run = wapp.ActionRun("test-run", "tf-apply:vm:c", "x", [], dry_run=True)
    spec = {
        "name": "c",
        "disk": [{"image": "default/img-x"}],
        "network_interface": [{"network_name": "default/management"}],
    }
    wapp._tf_apply_runner(
        run, "x", str(fake_kc),
        wapp._render_tf_for_kind("vm", spec),
        dry_run=True, resource_name="c",
    )

    assert (tmp_path / "_providers.tf").exists()
    # Both legacy files must now be header-free
    for f in (legacy_1, legacy_2):
        txt = f.read_text()
        assert wapp._TF_HEADER not in txt, (
            f"{f.name} still carries the duplicate header after migration"
        )
        assert "resource " in txt, (
            f"{f.name} migration stripped too much — resource block gone"
        )


def test_disk_with_image_drops_storage_class_name(tmp_path, monkeypatch):
    """v1.4.38: the Harvester provider rejects storage_class_name on a
    disk that has `image` set ("the storage_class_name of an image can
    only be defined during image creation"). Our renderer must drop the
    field silently in that case — and keep it when image is absent
    (blank data disk)."""
    spec_image = {
        "name": "v",
        "disk": [{
            "image": "default/img-x",
            "storage_class_name": "longhorn",  # would conflict
        }],
        "network_interface": [{"network_name": "default/management"}],
    }
    out = wapp._render_tf_for_kind("vm", spec_image)
    assert 'image      = "default/img-x"' in out
    assert "storage_class_name" not in out, (
        "render emitted storage_class_name on an image-backed disk — "
        "provider will reject with the v1.4.36 conflict"
    )

    spec_blank = {
        "name": "v",
        "disk": [{"size": "10Gi", "storage_class_name": "longhorn"}],
        "network_interface": [{"network_name": "default/management"}],
    }
    out2 = wapp._render_tf_for_kind("vm", spec_blank)
    assert "storage_class_name" in out2, (
        "storage_class_name dropped from a blank data disk — that one "
        "is valid and should be emitted"
    )


def test_destroy_resource_rejects_invalid_address(api):
    """The targeted destroy endpoint validates the address format —
    `<resource_type>.<local_name>` (a..z + digits + underscores). An
    invalid form must 400 with a hint, never reach the runner."""
    status, payload = api(
        "POST", "/api/terraform/harv-fake/destroy_resource",
        {"address": "not-an-address"}, expect_status=None,
    )
    assert status == 400
    assert "address" in (payload.get("error") or "")
    assert payload.get("hint")


def test_destroy_resource_unknown_cluster_404(api):
    status, payload = api(
        "POST", "/api/terraform/does-not-exist/destroy_resource",
        {"address": "harvester_virtualmachine.x"}, expect_status=None,
    )
    assert status == 404
    assert "does-not-exist" in (payload.get("error") or "")


def test_clean_stale_lists_only_files_not_in_state(api, tmp_path,
                                                    monkeypatch, flask_server):
    """v1.4.38: stale-file cleanup. Workspace has 3 .tf files; the
    state lists only one of them; dry-run must report the other two
    as `would_remove` and the real call must remove them and leave the
    tracked one alone."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                          .resolve().parent.parent.parent / "web"))
    import app as wapp

    # Stage a synthetic workspace
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    (tmp_path / ".terraform").mkdir()  # mark as initialized
    (tmp_path / "_providers.tf").write_text(wapp._TF_HEADER)
    (tmp_path / "good.tf").write_text(
        'resource "harvester_virtualmachine" "good" { name = "good" }\n')
    (tmp_path / "stale1.tf").write_text(
        'resource "harvester_virtualmachine" "stale1" { name = "stale1" }\n')
    (tmp_path / "stale2.tf").write_text(
        'resource "harvester_virtualmachine" "stale2" { name = "stale2" }\n')
    # Force the cluster lookup to succeed; the source kubeconfig MUST
    # NOT be inside `tmp_path` (the workspace) because the endpoint
    # then copies it to `<ws>/kubeconfig` and that would be a self-copy.
    src_kc = tmp_path.parent / f"src-kubeconfig-{tmp_path.name}"
    src_kc.write_text("apiVersion: v1\nkind: Config\n")
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: str(src_kc))
    # Pretend `terraform state list` returns just the "good" resource
    monkeypatch.setattr(
        wapp, "_tf_run_cmd",
        lambda ws, kc, cmd, timeout=30: (
            (0, "harvester_virtualmachine.good\n", "")
            if cmd[:2] == ["state", "list"] else (0, "", "")
        ),
    )

    # Dry-run
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/whatever/clean_stale",
                   json={"dry_run": True})
        assert r.status_code == 200, r.get_data(as_text=True)
        d = r.get_json()
        assert sorted(d["would_remove"]) == ["stale1.tf", "stale2.tf"], d
        assert d["in_state"] == ["good"]

        # Real run
        r = c.post("/api/terraform/whatever/clean_stale",
                   json={"dry_run": False})
        assert r.status_code == 200
        d = r.get_json()
        assert sorted(d["removed"]) == ["stale1.tf", "stale2.tf"]

    assert (tmp_path / "good.tf").exists()
    assert not (tmp_path / "stale1.tf").exists()
    assert not (tmp_path / "stale2.tf").exists()
    assert (tmp_path / "_providers.tf").exists()


def test_second_apply_does_not_duplicate_providers(tmp_path, monkeypatch):
    """After the first apply created _providers.tf, a second apply (a
    different resource) must NOT touch the providers file — exactly the
    user scenario that triggered the bug report."""
    monkeypatch.setattr(wapp, "_tf_workspace_dir", lambda c: tmp_path)
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "0.6.7")
    monkeypatch.setattr(wapp, "_tf_run_cmd",
                        lambda ws, kc, cmd, timeout=180: (0, "", ""))
    fake_kc = tmp_path / "kc"
    fake_kc.write_text("apiVersion: v1\nkind: Config\n")

    def _spec(name):
        return {
            "name": name,
            "disk": [{"image": "default/img-x"}],
            "network_interface": [{"network_name": "default/management"}],
        }

    for resname in ("first", "second"):
        run = wapp.ActionRun(f"test-{resname}", f"tf-apply:vm:{resname}",
                             "x", [], dry_run=True)
        wapp._tf_apply_runner(
            run, "x", str(fake_kc),
            wapp._render_tf_for_kind("vm", _spec(resname)),
            dry_run=True, resource_name=resname,
        )

    # Only ONE _providers.tf with the header
    providers_text = (tmp_path / "_providers.tf").read_text()
    assert providers_text.count("required_providers") == 1
    # Neither resource file carries the header
    for resname in ("first", "second"):
        rtxt = (tmp_path / f"{resname}.tf").read_text()
        assert "required_providers" not in rtxt, (
            f"{resname}.tf re-introduced the duplicate header"
        )
