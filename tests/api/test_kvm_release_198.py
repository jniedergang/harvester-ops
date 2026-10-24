"""v1.98.0 : derniers points de la relecture finale du module KVM.

La copie longue d'un volume part sur sa propre connexion SSH (un plantage de
sshd sous sessions multiplexées simultanées ne la coupe plus), et seul le
message d'erreur, jamais le chemin cité, dit qu'une lecture a été refusée
pour permission.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import kvm_host as kh  # noqa: E402
import kvm_ops as ko  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    ("qemu-img: Could not open '/data/a.qcow2': Permission denied", "permission"),
    ("stat: cannot statx '/data/a.qcow2': Permission denied", "permission"),
    ("stat: cannot statx '/data/Permission denied/a.qcow2': Input/output error", "I/O error"),
    ("stat: cannot statx '/srv/EACCES.qcow2': Stale file handle", "stale file handle"),
    ("qemu-img: Could not open '/data/Permission denied.qcow2': No medium found", "read error"),
    ("", "timeout"),
])
def test_permission_is_read_from_the_error_not_from_the_path(text, expected):
    assert ko._failure_class(text) == expected


def test_run_input_forwards_a_fresh_connection():
    seen = {}

    def fake_ssh(remote_argv, timeout, data=None, fresh=False):
        seen["fresh"] = fresh
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    host = kh.VirshHost.__new__(kh.VirshHost)
    host._ssh = fake_ssh
    assert host.run_input(["true"], "x", fresh=True) == "ok"
    assert seen["fresh"] is True
    host.run_input(["true"], "x")
    assert seen["fresh"] is False


def test_the_long_volume_copy_uses_its_own_connection():
    src = (ROOT / "bin" / "lib" / "kvm_ops.py").read_text()
    i = src.index("vol_create_args(e)")
    call = src[i:src.index(")", src.index("timeout=left", i)) + 1]
    assert "fresh=True" in call
