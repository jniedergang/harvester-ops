"""The system Python can be 3.6 while a supported version is installed."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def function(name):
    source = (ROOT / "install.sh").read_text()
    start = source.index(name + "() {\n")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


def fake_python(directory, name, supported):
    path = directory / name
    command = f'exec {shlex.quote(sys.executable)} "$@"' if supported else "exit 1"
    path.write_text("#!/bin/bash\n" + command + "\n")
    path.chmod(0o755)
    return path


def detect(directory):
    script = 'err() { printf "%s\\n" "$*" >&2; }\n' + function("detect_update_python")
    return subprocess.run(["/bin/bash", "-c", script + "\ndetect_update_python"],
                          env={**os.environ, "PATH": str(directory)}, text=True, capture_output=True)


def test_legacy_default_uses_supported_versioned_python(tmp_path):
    fake_python(tmp_path, "python3", False)
    fake_python(tmp_path, "python3.14", False)
    expected = fake_python(tmp_path, "python3.12", True)
    result = detect(tmp_path)
    assert result.returncode == 0 and result.stdout.strip() == str(expected)
    assert not result.stderr


def test_supported_default_is_kept(tmp_path):
    expected = fake_python(tmp_path, "python3", True)
    fake_python(tmp_path, "python3.12", True)
    result = detect(tmp_path)
    assert result.returncode == 0 and result.stdout.strip() == str(expected)


@pytest.mark.parametrize("names", [[], ["python3", "python3.8"], ["python3.9", "python3.12"]])
def test_missing_or_unsupported_python_fails_clearly(tmp_path, names):
    for name in names:
        fake_python(tmp_path, name, False)
    result = detect(tmp_path)
    assert result.returncode == 2 and not result.stdout
    assert "Python >= 3.9" in result.stderr


def test_generated_service_pins_the_selected_absolute_path(tmp_path):
    # Exercise the actual template renderer, including sed metacharacters.
    selected = tmp_path / "host&python|3.12"
    script = function("render_update_service") + '\nrender_update_service "$1"'
    result = subprocess.run(["/bin/bash", "-c", script, "test", str(selected)],
                            env={**os.environ, "SCRIPT_DIR": str(ROOT)},
                            text=True, capture_output=True, check=True)
    assert f"ExecStart={selected} /usr/local/bin/harvester-ops-update.py\n" in result.stdout
    assert "ExecStart=/usr/bin/python3 " not in result.stdout


def test_install_and_upgrade_validate_before_mutations():
    assert "detect_update_python" in function("check_deps")
    upgrade = function("upgrade")
    assert upgrade.index("detect_update_python") < upgrade.index("install_scripts")
    install = function("install_update_agent")
    assert install.index("detect_update_python") < install.index("install -d")
    assert 'render_update_service "$update_python" > /etc/systemd/system/harvester-ops-update.service' in install
