"""2.9.0 (second pass of the review fixes): root never writes, creates or
hands over a file by following a link planted in a directory the console
account can write, for the secret store steps of install.sh and for the
root secrets agent (bin/harvester-secrets-agent.py).

Same method as test_root_writes_no_follow_283.py: everything runs
unprivileged in temporary directories, each case plants a link (or swaps an
entry at the worst moment) and checks that the file or directory outside is
untouched; a chown is recorded (the inode it would change), not done.
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_root_writes_no_follow_283 import (shell_fn, plant_dir_link, plant_file_link,  # noqa: E402
                                            chowns, ino)  # noqa: F401

BUNDLE = "harvester-ops-openbao-2.7.2-2.7.2-5.4.tar.gz"


# ---------------------------------------------------------------------------
# install.sh : étapes des secrets, rejouées à chaque --upgrade
# ---------------------------------------------------------------------------
def release(tmp_path):
    """Livrable minimal : config, VERSION, safe_fs, paquet OpenBao avec sa somme."""
    d = tmp_path / "livrable"
    (d / "config").mkdir(parents=True)
    subprocess.run(["cp", "-r", str(ROOT / "config" / "systemd"), str(ROOT / "config" / "openbao"),
                    str(d / "config")], check=True)
    (d / "VERSION").write_text("2.9.0\n")
    (d / "bin" / "lib").mkdir(parents=True)
    (d / "bin" / "lib" / "safe_fs.py").symlink_to(ROOT / "bin" / "lib" / "safe_fs.py")
    b = d / "bundles" / "openbao"
    b.mkdir(parents=True)
    (b / BUNDLE).write_bytes(b"bundle")
    (b / f"{BUNDLE}.sha256").write_text(f"{hashlib.sha256(b'bundle').hexdigest()}  {BUNDLE}\n")
    return d


def run_install(tmp_path, call, script_dir, state):
    src = (ROOT / "install.sh").read_text()
    names = ["detect_update_python", "render_python_unit", "install_secrets_agent", "install_bundles", "_safe_fs"]
    body = "".join(shell_fn(src, n) for n in names)
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    script = ("set -eo pipefail\nok(){ echo \"OK $*\"; }\nwarn(){ echo \"WARN $*\"; }\n"
              "err(){ echo \"ERR $*\" >&2; }\nsystemctl(){ :; }\n"
              f"SCRIPT_DIR={script_dir}\nSTATE_DIR={state}\nINSTALL_DIR={tmp_path / 'opt'}\n"
              f"SECRETS_UNIT_DIR={units}\n{body}\n{call}\n")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


# (fonction, chemin planté sous l'état, nature du lien, ce qui doit exister ensuite)
INSTALL_CASES = [
    ("install_secrets_agent", "secrets-agent", "dir", "secrets-agent/agent.json"),
    ("install_secrets_agent", "secrets-agent/staged", "dir", "secrets-agent/staged"),
    ("install_secrets_agent", "secrets-agent/agent.json", "file", "secrets-agent/agent.json"),
    ("install_bundles", "secrets-agent", "dir", f"secrets-agent/staged/{BUNDLE}"),
    ("install_bundles", "secrets-agent/staged", "dir", f"secrets-agent/staged/{BUNDLE}"),
    ("install_bundles", f"secrets-agent/staged/{BUNDLE}", "file", f"secrets-agent/staged/{BUNDLE}"),
    ("install_bundles", f"secrets-agent/staged/{BUNDLE}.sha256", "file", f"secrets-agent/staged/{BUNDLE}.sha256"),
]


@pytest.mark.parametrize("fn,rel,kind,expect", INSTALL_CASES, ids=[f"{c[0]}:{c[1]}" for c in INSTALL_CASES])
def test_install_secrets_steps_never_follow_a_planted_link(tmp_path, fn, rel, kind, expect):
    state = tmp_path / "state"
    state.mkdir()
    script_dir = release(tmp_path)
    if kind == "dir":
        victim = plant_dir_link(state, rel, tmp_path)
        before = (stat.S_IMODE(victim.stat().st_mode), sorted(os.listdir(victim)))
    else:
        victim = plant_file_link(state, rel, tmp_path)
        before = (stat.S_IMODE(victim.stat().st_mode), victim.read_text())
    r = run_install(tmp_path, fn, script_dir, state)
    if kind == "dir":
        after = (stat.S_IMODE(victim.stat().st_mode), sorted(os.listdir(victim)))
    else:
        after = (stat.S_IMODE(victim.stat().st_mode), victim.read_text())
    assert after == before, (r.stdout, r.stderr)
    assert r.returncode == 0, r.stderr
    p = state / expect
    assert not p.is_symlink() and p.exists(), (r.stdout, r.stderr)
    for parent in p.relative_to(state).parents:
        assert not (state / parent).is_symlink()


def test_install_secrets_agent_marker_and_modes(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    r = run_install(tmp_path, "install_secrets_agent", release(tmp_path), state)
    assert r.returncode == 0, r.stderr
    d = state / "secrets-agent"
    assert json.loads((d / "agent.json").read_text())["installed"] == "2.9.0"
    assert stat.S_IMODE((d / "agent.json").stat().st_mode) == 0o644
    assert stat.S_IMODE(d.stat().st_mode) == 0o750 and stat.S_IMODE((d / "staged").stat().st_mode) == 0o750
    # la clé de la collection, pour la vérification à la pose, dans /opt (root)
    key = tmp_path / "opt" / "openbao" / "application-collection-cosign.pub"
    assert key.read_bytes() == (ROOT / "config" / "openbao" / "application-collection-cosign.pub").read_bytes()


def test_install_bundles_stages_the_openbao_bundle_through_the_helper(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    r = run_install(tmp_path, "install_bundles", release(tmp_path), state)
    assert r.returncode == 0, r.stderr
    staged = state / "secrets-agent" / "staged"
    assert (staged / BUNDLE).read_bytes() == b"bundle"
    assert stat.S_IMODE((staged / BUNDLE).stat().st_mode) == 0o640
    assert "could not give" not in r.stdout


@pytest.mark.parametrize("name", ["install_secrets_agent", "install_bundles"])
def test_secrets_steps_touch_the_state_only_through_the_helper(name):
    """Source check, as in 2.8.3: nothing under $STATE_DIR by a redirection,
    install(1), chown/chgrp/chmod, cp or mv."""
    body = shell_fn((ROOT / "install.sh").read_text(), name)
    offenders = [line.strip() for line in body.splitlines()
                 if "STATE_DIR" in line and not line.strip().startswith("#")
                 and not line.strip().startswith("_safe_fs")
                 and line.strip() != 'install -d -m 0750 "$STATE_DIR"'
                 and any(tok in line for tok in (">", "install ", "chown", "chgrp", "chmod", "cp ", "mv "))]
    assert offenders == [], offenders
