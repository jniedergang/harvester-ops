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


# ---------------------------------------------------------------------------
# v1.9.0 — pre-shutdown state memory + Wake-on-LAN power-on
# ---------------------------------------------------------------------------

STARTUP = (ROOT / "bin" / "harvester-startup.sh").read_text()


def test_shutdown_only_annotates_actually_running_vms():
    """A VM already Halted before the shutdown must NOT be annotated for
    restart (the old fallback tagged everything previous=Always, so the
    startup rebooted long-stopped VMs). Stale annotations from previous
    cycles are purged."""
    fresh = (ROOT / "bin" / "harvester-shutdown.sh").read_text()
    block = fresh.split("_stop_one_sync()", 1)[1].split("_process_group", 1)[0]
    assert '"${ANNOT_PREV_RUNSTRATEGY}-"' in block, "stale annotation purge missing"
    assert 'current_rs="Always"' not in block, "the annotate-everything fallback is back"


def test_startup_restarts_only_annotated_vms_and_consumes_annotation():
    fresh = (ROOT / "bin" / "harvester-startup.sh").read_text()
    sel = fresh.split("halted_vms=$(", 1)[1].split("local total", 1)[0]
    assert "vm_prev_runstrategy" in sel, "restart list must require the annotation"
    # jsonpath bracket form silently returns EMPTY for dotted/slashed
    # annotation keys (lived: startup restarted nothing) — banned.
    assert "annotations['" not in fresh
    common = (ROOT / "bin" / "lib" / "common.sh").read_text()
    assert "vm_prev_runstrategy()" in common and "go-template" in common
    assert "<no value>" in common
    fresh2 = (ROOT / "bin" / "harvester-startup.sh").read_text()
    start = fresh2.split("_start_one_sync()", 1)[1].split("_process_group", 1)[0]
    assert 'target_rs="Always"' not in start, "no more restart-everything fallback"
    assert '"${ANNOT_PREV_RUNSTRATEGY}-"' in start, "annotation must be consumed"


def test_wol_helpers_and_power_steps():
    common = (ROOT / "bin" / "lib" / "common.sh").read_text()
    assert "wol_send()" in common and "node_wol_mac()" in common
    # layered fallback: python3 (console host has it) then CLI tools
    assert "SO_BROADCAST" in common and "wakeonlan" in common and "ether-wake" in common
    assert "wol_mac" in common  # parsed from config
    # both power steps try WoL before falling back to the human prompt
    assert STARTUP.count("wol_send") >= 2
    assert "node_wol_mac" in STARTUP
    example = (ROOT / "config" / "config.yaml.example").read_text()
    assert "wol_mac" in example


def test_wol_is_resent_while_node_stays_down():
    """Race seen live: the OS kills networking before the actual
    power-off, so a magic packet sent on 'ping died' lands during
    shutdown and is ignored. The API wait loop must re-send WoL as
    long as the node does not answer pings."""
    fresh = (ROOT / "bin" / "harvester-startup.sh").read_text()
    wait = fresh.split("Attente de l'API Kubernetes", 1)[1].split("step_power_rest", 1)[0]
    assert "wol_send" in wait and "ping -c1" in wait


def test_vm_restart_waits_for_virt_api_webhook():
    """Nodes Ready does not mean KubeVirt ready: virt-api had zero
    endpoints two minutes after Ready and every runStrategy patch was
    rejected by the mutating webhook. The restart step must probe the
    real path (server-side dry-run patch) before the plan, and each
    patch retries."""
    fresh = (ROOT / "bin" / "harvester-startup.sh").read_text()
    step = fresh.split("step_restart_vms()", 1)[1]
    assert "--dry-run=server" in step, "virt-api probe missing"
    start = step.split("_start_one_sync()", 1)[1].split("_process_group", 1)[0]
    assert "for attempt in 1 2 3" in start, "patch retry missing"
