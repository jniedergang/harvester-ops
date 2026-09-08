"""v1.8.9 — audit of the 2026-09-09 real shutdown run (action b715d2077962).

Three defects observed live and locked here:
  1. The etcd snapshot used /var/lib/rancher/rke2/bin/etcdctl — a path
     that does not exist on RKE2 (etcd runs as a static pod) — so the
     safety net failed instantly on every real run.
  2. With --yes, confirm() auto-approved the failure and the DESTRUCTIVE
     sequence continued (and exited 0). Safety-check failures must now
     abort in non-interactive mode unless --force is explicit.
  3. The Longhorn detach wait counted pod-attached volumes (monitoring,
     upgradelog fluentd) that can never detach by stopping VMs — burning
     the whole timeout and ending in a false consistency warning.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SH = (ROOT / "bin" / "harvester-shutdown.sh").read_text()
COMMON = (ROOT / "bin" / "lib" / "common.sh").read_text()

sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


def test_etcd_snapshot_uses_rke2_cli_not_phantom_etcdctl():
    assert "rke2/bin/etcdctl" not in SH, (
        "RKE2 ships no etcdctl at that path — the snapshot failed "
        "instantly on every real run")
    assert "etcd-snapshot save --name" in SH


def test_etcd_failure_aborts_unless_forced():
    branch = SH.split('log_error "Échec du snapshot etcd"', 1)[1] \
               .split("step_snapshot_vms", 1)[0]
    assert '"$FORCE" == "1"' in branch
    assert "exit 1" in branch
    # interactive keeps the human question
    assert "confirm" in branch


def test_longhorn_wait_counts_only_vm_volumes():
    block = SH.split("step_longhorn_maintenance", 1)[1].split("step_cordon", 1)[0]
    assert "virt-launcher" in block, (
        "the detach wait must key on virt-launcher workloads — pod "
        "volumes (monitoring, upgradelog) never detach by stopping VMs")
    assert "workloadsStatus" in block
    # still-attached VM volumes abort in non-interactive mode
    assert '"$FORCE" == "1"' in block and "exit 1" in block


def test_force_flag_is_parsed():
    assert "--force) FORCE=1" in COMMON
    assert ': "${FORCE:=0}"' in COMMON
    assert "--force" in SH.split("Usage", 1)[1][:2500], "usage must document --force"


def test_start_action_threads_force_to_cli(monkeypatch):
    """UI parity: the endpoint boolean must become the CLI flag."""
    class DummyThread:
        def __init__(self, *a, **k): pass
        def start(self): pass
    monkeypatch.setattr(wapp.threading, "Thread", DummyThread)
    monkeypatch.setattr(wapp, "load_config",
                        lambda: {"clusters": [{"name": "c1"}]})
    run = wapp.start_action("shutdown", "c1", force=True)
    assert "--force" in run.cmd
    run2 = wapp.start_action("shutdown", "c1")
    assert "--force" not in run2.cmd
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS.pop(run.id, None)
        wapp.ACTIONS.pop(run2.id, None)


def test_ui_exposes_force_checkbox_with_i18n():
    html = (ROOT / "web" / "templates" / "index.html").read_text()
    assert 'id="opt-force"' in html
    js = (ROOT / "web" / "static" / "js" / "app.js").read_text()
    assert "body.force = $('#opt-force')" in js
    i18n = (ROOT / "web" / "static" / "js" / "i18n.js").read_text()
    assert i18n.count("'shutdown.optForce'") >= 2
    assert i18n.count("'shutdown.optForceHint'") >= 2
