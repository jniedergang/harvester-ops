"""2.7.0 (m11) : l'espace de travail des outils de bin/ n'est jamais /tmp."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
LIB = ROOT / "bin" / "lib"
sys.path.insert(0, str(LIB))

import work_dir  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.delenv("HARVESTER_OPS_WORK_DIR", raising=False)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def test_without_the_variable_it_is_the_state_work_dir_in_0700(env):
    p = work_dir.path()
    assert p == env / "state" / "work"
    assert stat.S_IMODE(p.stat().st_mode) == 0o700


def test_a_given_state_dir_wins_over_nothing(env, monkeypatch):
    monkeypatch.delenv("HARVESTER_OPS_STATE_DIR")
    assert work_dir.path(env / "other") == env / "other" / "work"


def test_the_variable_wins_and_is_tightened(env, monkeypatch):
    d = env / "wd"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", str(d))
    assert work_dir.path() == d
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


@pytest.mark.parametrize("bad", ["/tmp", "/tmp/", "/var/tmp", "/dev/shm", "relative/dir", "."])
def test_shared_or_relative_directories_are_refused_even_given(env, monkeypatch, bad):
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", bad)
    with pytest.raises(ValueError):
        work_dir.path()


def test_files_are_private_and_never_in_tmp(env):
    f = Path(work_dir.private_file("secret", prefix="x-", suffix=".kc"))
    try:
        assert f.parent == env / "state" / "work"
        assert stat.S_IMODE(f.stat().st_mode) == 0o600 and f.read_text() == "secret"
    finally:
        f.unlink()
    d = work_dir.mkdtemp("hops-t-")
    assert d.parent == env / "state" / "work" and stat.S_IMODE(d.stat().st_mode) == 0o700


def test_harvester_capi_work_files_follow_the_work_dir(env):
    """Le kubeconfig d'identité de harvester-capi n'atterrit plus dans /tmp
    quand la variable manque (m11)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("hcapi_wd", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    f = Path(mod._work_file("kc"))
    try:
        assert f.parent == env / "state" / "work"
    finally:
        f.unlink()


def test_the_module_needs_only_the_standard_library():
    code = (f"import sys; sys.path.insert(0, {str(LIB)!r}); import work_dir, capi_manifests, child_policies; "
            "bad = [m for m in sys.modules if m.split('.')[0] in ('yaml', 'requests', 'flask')]; "
            "assert not bad, bad")
    subprocess.run([sys.executable, "-I", "-S", "-c", code], check=True)
