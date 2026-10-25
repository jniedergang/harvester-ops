"""Prochaine version KVM : `-o LogLevel=ERROR` sur chaque ssh vers un hôte KVM.

node2 a un sshd durci avec une longue `Banner /etc/issue.net` : sans
LogLevel=ERROR, ssh l'écrit sur stderr à chaque connexion, là où kvm_host lit
ses messages d'erreur. Les sorties d'erreur ci-dessous ont été relevées en
réel sur node2 (OpenSSH 10.6p1) avec l'option : le classement doit tenir."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import kvm_host as kh  # noqa: E402

DOC = json.loads((ROOT / "tests" / "fixtures" / "kvm" /
                  "ssh-stderr-node2-loglevel-error.json").read_text())
SAMPLES = DOC["samples"]
DECL = {"name": "node2", "address": "172.16.1.12", "user": "ju", "ssh_key": "ssh/node2_id",
        "known_hosts": "ssh/node2_known"}


@pytest.fixture
def state():
    d = tempfile.mkdtemp(prefix="kvmb-", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _opts(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]


def test_loglevel_error_on_every_ssh_line(state):
    assert "LogLevel=ERROR" in _opts(kh.ssh_argv(DECL, state, ["true"]))
    assert "LogLevel=ERROR" in _opts(kh.ssh_argv(DECL, state, ["true"], fresh=True))
    assert "LogLevel=ERROR" in _opts(kh.stdio_forward_argv(DECL, state, "127.0.0.1", 5900))
    seen = []
    kh.close_master(DECL, state, runner=lambda argv, **kw: (
        seen.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1])
    assert "LogLevel=ERROR" in _opts(seen[0])


@pytest.mark.parametrize("label", [k for k, v in SAMPLES.items() if v["kind"]])
def test_real_stderr_samples_are_classified(state, label):
    sample = SAMPLES[label]
    stderr = sample["stderr"].replace("{state}", str(state))

    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, sample["rc"], "", stderr)
    h = kh.VirshHost(DECL, state, runner=runner, read_retries=0)
    with pytest.raises(kh.KvmError) as e:
        h.virsh("list", "--name", "--all", idempotent=True)
    assert e.value.kind == sample["kind"]
    assert str(state) not in e.value.message
    # aucune ligne de la bannière n'arrive : LogLevel=ERROR la retire. Sans
    # l'option, la bannière (~1,3 Kio) précédait l'erreur d'authentification ;
    # une empreinte refusée ou un hôte injoignable ne la reçoivent jamais
    assert "USG" not in stderr
    if sample["kind"] == "auth":
        assert sample["banner_without_loglevel_bytes"] > len(stderr) + 1000


def test_successful_command_output_is_unchanged(state):
    assert SAMPLES["success"]["stderr"] == ""

    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "vm1\nvm2\n\n", "")
    h = kh.VirshHost(DECL, state, runner=runner)
    assert h.virsh("list", "--name") == "vm1\nvm2\n\n"


@pytest.mark.parametrize("tail,kind", [
    ("ju@172.16.1.12: Permission denied (publickey,password,keyboard-interactive).\n", "auth"),
    ("Host key verification failed.\n", "hostkey"),
    ("ssh: connect to host 172.16.1.12 port 2: No route to host\n", "unreachable"),
])
def test_banner_before_an_error_would_not_change_the_kind(state, tail, kind):
    """Filet : même si un sshd envoyait sa bannière malgré l'option, le
    genre et la dernière ligne restent ceux de l'erreur."""
    stderr = DOC["banner"] + tail

    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 255, "", stderr)
    h = kh.VirshHost(DECL, state, runner=runner, read_retries=0)
    with pytest.raises(kh.KvmError) as e:
        h.virsh("list")
    assert e.value.kind == kind and "USG" not in e.value.message
