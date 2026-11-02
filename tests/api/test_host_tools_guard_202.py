"""2.0.2 : les tests n'atteignent jamais le podman, le docker ni le systemctl
réels de la machine qui les lance (un test de désinstallation avait retiré
l'étiquette localhost/harvester-ops:latest de node1). tests/conftest.py pose
des gardes devant le PATH ; ces tests prouvent qu'elles sont actives."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

GUARD_DIR = os.environ["HARVESTER_OPS_TEST_TOOLS_GUARD"]
GUARD_LOG = GUARD_DIR + ".log"
GUARDED = ("podman", "docker", "systemctl")

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.mark.parametrize("tool", ["podman", "docker", "systemctl"])
def test_a_host_tool_resolves_to_the_guard(tool):
    assert tool in GUARDED
    found = shutil.which(tool)
    assert found == os.path.join(GUARD_DIR, tool)


@pytest.mark.parametrize("tool,argv", [
    ("podman", ["rmi", "localhost/harvester-ops:latest"]),
    ("docker", ["rm", "-f", "harvester-ops"]),
    ("systemctl", ["restart", "harvester-ops"]),
])
def test_a_guarded_call_never_reaches_the_host(tool, argv):
    before = Path(GUARD_LOG).read_text() if os.path.exists(GUARD_LOG) else ""
    result = subprocess.run([tool, *argv], capture_output=True, text=True)
    assert result.returncode == 97
    assert "test guard" in result.stderr
    after = Path(GUARD_LOG).read_text()
    assert after[len(before):].splitlines()[-1] == f"{tool} {' '.join(argv)}"


def test_the_flask_server_of_the_tests_inherits_the_guard():
    source = (ROOT / "tests/conftest.py").read_text()
    block = source[source.index("def flask_server("):]
    assert '**os.environ' in block and 'os.environ.get("PATH"' in block


ABSOLUTE = re.compile(r"""\[\s*["'](/usr)?/s?bin/(podman|docker|systemctl)["']""")


def test_no_test_calls_a_host_tool_by_its_absolute_path():
    offenders = []
    for path in (ROOT / "tests").rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if ABSOLUTE.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert offenders == []
