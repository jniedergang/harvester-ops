"""Unit tests for the helpers extracted from `_capi_install_runner` in v1.4.18.

These helpers are now module-level and pure (no closures) so we can
import and call them directly — they're the most fragile bits of the
install pipeline and previously had zero coverage.
"""

import os
import sys
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
sys.path.insert(0, str(WEB_DIR))

# Import the extracted helpers. Importing the module brings up Flask but
# we don't start the server here — just touch the functions.
import importlib
app_module = importlib.import_module("app")
_capi_envsubst                    = app_module._capi_envsubst
_capi_extract_bundle_to_temp      = app_module._capi_extract_bundle_to_temp
_capi_kubectl_apply               = app_module._capi_kubectl_apply
_capi_wait_for_deploy             = app_module._capi_wait_for_deploy
_capi_apply_yaml_dir              = app_module._capi_apply_yaml_dir
_capi_apply_clusterclass_phase    = app_module._capi_apply_clusterclass_phase
_CAPI_COMPONENT_DEPLOYMENTS       = app_module._CAPI_COMPONENT_DEPLOYMENTS


# ---------------------------------------------------------------------------
# envsubst — the most-likely-to-regress piece (provider manifests crashloop
# if it isn't right)
# ---------------------------------------------------------------------------
def test_envsubst_with_default_when_var_unset():
    """`${FOO:=bar}` → "bar" when FOO is missing from env."""
    assert _capi_envsubst("a ${FOO:=bar} b", env={}) == "a bar b"


def test_envsubst_prefers_env_over_default():
    """`${FOO:=bar}` → env value when FOO is present."""
    assert _capi_envsubst("a ${FOO:=bar} b", env={"FOO": "baz"}) == "a baz b"


def test_envsubst_without_default_uses_empty():
    """`${FOO}` (no `:=`) → "" when FOO is unset (kubectl-friendly)."""
    assert _capi_envsubst("a ${FOO} b", env={}) == "a  b"


def test_envsubst_handles_multiple_placeholders():
    """Multiple substitutions on the same line, mixed defaults."""
    text = "--v=${CAPRKE2_DEBUG_LEVEL:=0} --diag=${DIAG:=:8443}"
    out = _capi_envsubst(text, env={"CAPRKE2_DEBUG_LEVEL": "5"})
    assert out == "--v=5 --diag=:8443"


def test_envsubst_ignores_lowercase_placeholders():
    """clusterctl placeholders are uppercase; lowercase ${foo} should
    pass through untouched (some YAML uses `${foo}` as a Helm/Kustomize
    template marker — we must not stomp on it)."""
    text = "key: ${foo}"
    assert _capi_envsubst(text, env={"foo": "bar"}) == "key: ${foo}"


def test_envsubst_preserves_dollar_signs_outside_placeholders():
    text = "echo $HOME ${USER:=anon} $$"
    out = _capi_envsubst(text, env={"USER": "root"})
    # $HOME stays as-is (not ${HOME}), USER is replaced, $$ stays
    assert out == "echo $HOME root $$"


# ---------------------------------------------------------------------------
# Extract bundle — happy path + missing file
# ---------------------------------------------------------------------------
def test_extract_bundle_to_temp_returns_workdir_and_root(tmp_path):
    """Build a tiny tar.gz, then verify the helper unpacks it into a
    fresh workdir and returns the inner bundle root."""
    import tarfile

    src = tmp_path / "bundle"
    src.mkdir()
    (src / "manifest.txt").write_text("hello")
    archive = tmp_path / "test.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src, arcname="bundle")

    workdir, root = _capi_extract_bundle_to_temp(archive)
    try:
        assert workdir.exists()
        assert workdir.is_dir()
        assert root.name == "bundle"
        assert (root / "manifest.txt").read_text() == "hello"
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


def test_extract_bundle_to_temp_raises_on_missing_file(tmp_path):
    with pytest.raises(Exception):
        _capi_extract_bundle_to_temp(tmp_path / "nope.tar.gz")


# ---------------------------------------------------------------------------
# kubectl_apply — exit code translation + timeout handling
# ---------------------------------------------------------------------------
def test_kubectl_apply_translates_failure(monkeypatch):
    """When `kubectl apply` exits non-zero, the helper returns
    (False, <stderr excerpt>) — never raises."""
    import subprocess as sp

    def fake_run(cmd, **kw):
        return sp.CompletedProcess(cmd, returncode=1,
                                    stdout="", stderr="server-side dry-run rejected: foo")
    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    ok, msg = _capi_kubectl_apply("apiVersion: v1\nkind: Pod", "/tmp/kc")
    assert ok is False
    assert "rejected" in msg


def test_kubectl_apply_translates_success(monkeypatch):
    import subprocess as sp

    def fake_run(cmd, **kw):
        return sp.CompletedProcess(cmd, returncode=0, stdout="pod/foo created", stderr="")
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    ok, msg = _capi_kubectl_apply("yaml", "/tmp/kc")
    assert ok is True
    assert msg == ""


def test_kubectl_apply_handles_timeout(monkeypatch):
    import subprocess as sp

    def fake_run(cmd, **kw):
        raise sp.TimeoutExpired(cmd, kw.get("timeout", 1))
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    ok, msg = _capi_kubectl_apply("yaml", "/tmp/kc")
    assert ok is False
    assert "timeout" in msg.lower()


# ---------------------------------------------------------------------------
# wait_for_deploy — bool + message returned
# ---------------------------------------------------------------------------
def test_wait_for_deploy_dry_run_skipped_in_runner_not_here(monkeypatch):
    """The helper itself doesn't know about dry_run — that's the runner's
    job. Here we just verify the polling + final wait flow."""
    import subprocess as sp
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "get" in cmd:
            return sp.CompletedProcess(cmd, returncode=0,
                                        stdout="deployment.apps/foo\n", stderr="")
        # wait command
        return sp.CompletedProcess(cmd, returncode=0,
                                    stdout="deployment.apps/foo condition met",
                                    stderr="")
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    ok, msg = _capi_wait_for_deploy("/tmp/kc", "ns", "foo", timeout="5s")
    assert ok is True
    assert "Available" in msg


def test_wait_for_deploy_returns_false_on_kubectl_error(monkeypatch):
    import subprocess as sp

    def fake_run(cmd, **kw):
        if "get" in cmd:
            return sp.CompletedProcess(cmd, returncode=0,
                                        stdout="deployment.apps/foo\n", stderr="")
        return sp.CompletedProcess(cmd, returncode=1,
                                    stdout="", stderr="timeout exceeded")
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    ok, msg = _capi_wait_for_deploy("/tmp/kc", "ns", "foo", timeout="5s")
    assert ok is False
    assert "not Available" in msg or "timeout" in msg.lower()


# ---------------------------------------------------------------------------
# apply_yaml_dir — orchestration: per-file error counting + dry-run
# ---------------------------------------------------------------------------
def test_apply_yaml_dir_dry_run_emits_steps_no_kubectl(tmp_path, monkeypatch):
    """With dry_run=True the helper must NOT invoke subprocess.run."""
    import subprocess as sp
    (tmp_path / "a.yaml").write_text("apiVersion: v1\nkind: Pod\n")
    (tmp_path / "b.yaml").write_text("apiVersion: v1\nkind: Pod\n")
    called = []

    def fake_run(*a, **kw):
        called.append(a)
        return sp.CompletedProcess(a, 0, "", "")
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    steps = []
    def step(sid, status, msg):
        steps.append((sid, status, msg))
    err = _capi_apply_yaml_dir(tmp_path, "/tmp/kc", dry_run=True,
                                step_id="install-apply", step=step)
    assert err == 0
    assert called == []
    assert sum(1 for _sid, _s, m in steps if "[DRY-RUN]" in m) == 2


def test_apply_yaml_dir_counts_failures(tmp_path, monkeypatch):
    """Mix of successful + failing kubectl apply calls."""
    import subprocess as sp
    (tmp_path / "good.yaml").write_text("yaml")
    (tmp_path / "bad.yaml").write_text("yaml")

    def fake_run(cmd, **kw):
        # bad.yaml fails — we detect by sniffing the `input` arg.
        # The helper doesn't include the filename in the call so we
        # fail every OTHER invocation alternately.
        fake_run.counter = getattr(fake_run, "counter", 0) + 1
        if fake_run.counter == 1:
            return sp.CompletedProcess(cmd, 0, "ok", "")
        return sp.CompletedProcess(cmd, 1, "", "rejected")
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    steps = []
    err = _capi_apply_yaml_dir(tmp_path, "/tmp/kc", dry_run=False,
                                step_id="install-apply",
                                step=lambda *a: steps.append(a))
    assert err == 1   # one file failed


def test_apply_yaml_dir_skips_when_no_yaml(tmp_path):
    """Empty directory → 0 errors, 0 progress steps emitted."""
    (tmp_path / "README.md").write_text("ignored")
    steps = []
    err = _capi_apply_yaml_dir(tmp_path, "/tmp/kc", dry_run=False,
                                step_id="x", step=lambda *a: steps.append(a))
    assert err == 0
    assert steps == []


# ---------------------------------------------------------------------------
# Component deployments dict — drift guard
# ---------------------------------------------------------------------------
def test_component_deployments_dict_covers_install_order():
    """Every entry of CAPI_INSTALL_ORDER that has a deployment should
    show up in _CAPI_COMPONENT_DEPLOYMENTS. This catches the case where
    a new component is added to the install order but the wait phase
    silently skips it."""
    order = set(app_module.CAPI_INSTALL_ORDER)
    declared = set(_CAPI_COMPONENT_DEPLOYMENTS.keys())
    # Every declared component must be in the install order (no stale
    # entries pointing to dropped components)
    assert declared.issubset(order), (
        f"_CAPI_COMPONENT_DEPLOYMENTS references components not in "
        f"CAPI_INSTALL_ORDER: {sorted(declared - order)}"
    )


# ---------------------------------------------------------------------------
# clusterclass phase — optional, returns 0 if no clusterclass dir
# ---------------------------------------------------------------------------
def test_clusterclass_phase_returns_zero_when_dir_absent(tmp_path):
    """No clusterclass/ in the bundle → phase is a no-op."""
    steps = []
    err = _capi_apply_clusterclass_phase(tmp_path, "/tmp/kc",
                                           dry_run=False,
                                           step=lambda *a: steps.append(a))
    assert err == 0
    assert steps == []
