"""v1.96.0 : derniers points de la relecture finale du module KVM.

Une erreur de lecture ne porte jamais le chemin du répertoire d'état, la copie
de la clé envoyée par la console va dans ssh/ (0700), l'installation pose
l'alias `harvester-kvm` comme pour les autres outils.
"""
import importlib.util
import re
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
spec = importlib.util.spec_from_file_location("harvester_kvm_release", ROOT / "bin" / "harvester-kvm.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
import kvm_decl  # noqa: E402


def test_unreadable_declaration_warning_names_no_path():
    state = Path(tempfile.mkdtemp(prefix="kvmr-"))
    kvm_decl.decl_dir(state).mkdir(parents=True, mode=0o700)
    (kvm_decl.decl_dir(state) / "broken.yaml").write_text("{not json")
    seen = []
    assert kvm_decl.load_all(state, warn=seen.append) == []
    assert len(seen) == 1 and "broken.yaml" in seen[0]
    assert str(state) not in seen[0]


def test_unexpected_read_error_names_no_path():
    class Boom:
        name = "lab"
        decl = {"address": "10.0.0.5"}
    secret = "/var/lib/harvester-ops/ssh/lab_id"
    orig = cli.kh.host_inventory
    cli.kh.host_inventory = lambda host, details=False: (_ for _ in ()).throw(OSError(2, "missing", secret))
    try:
        [entry] = cli.read_all([Boom()])
    finally:
        cli.kh.host_inventory = orig
    assert entry["error"]["kind"] == "failed"
    assert secret not in entry["error"]["message"]
    assert entry["error"]["message"] == "lab: failed: FileNotFoundError"


def test_console_key_copy_goes_to_the_private_ssh_dir():
    src = (ROOT / "web" / "app.py").read_text()
    body = src[src.index('prefix="kvm-key-"') - 600:src.index('prefix="kvm-key-"') + 80]
    assert "_kd.ensure_ssh_dir(state)" in body
    assert "_capi_work_dir()" not in body


def test_ensure_ssh_dir_is_private():
    state = Path(tempfile.mkdtemp(prefix="kvmr-"))
    d = Path(kvm_decl.ensure_ssh_dir(state))
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_install_links_the_kvm_tool_like_the_others():
    text = (ROOT / "install.sh").read_text()
    assert re.search(r'ln -sf "\$PREFIX/harvester-kvm\.py" "\$PREFIX/harvester-kvm"', text)
