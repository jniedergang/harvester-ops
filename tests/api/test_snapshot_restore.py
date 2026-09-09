"""v1.7.1 — snapshot restore: deletionPolicy fix + error surfacing.

Live incident: an in-place restore failed with the Harvester webhook error
"delete policy with backup type snapshot for replacing VM is not supported"
(our manifest carried no deletionPolicy, and the default is rejected), and
the dock showed a bare exit 1 because this runner predated the v1.6.5
error_summary contract.
"""

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


class _FakeRun:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def client():
    wapp.app.config["TESTING"] = True
    return wapp.app.test_client()


def _wait_action(aid, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with wapp.ACTIONS_LOCK:
            run = wapp.ACTIONS.get(aid)
        if run and run.status in ("done", "error"):
            return run
        time.sleep(0.05)
    raise AssertionError("restore action did not finish in time")


def test_restore_manifest_has_retain_policy(client, monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        if "vmi" in cmd:
            return _FakeRun(1, stderr=b"NotFound")   # VM arrêtée: pas de VMI
        if "apply" in cmd:
            captured["manifest"] = json.loads(kw["input"].decode())
            return _FakeRun(1, stderr=b"boom\n")   # stop the runner right away
        return _FakeRun(0, stdout=b"{}")

    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    r = client.post("/api/vm/c1/ns1/vm1/restore", json={"snapshot": "s1"})
    assert r.status_code in (200, 201, 202)
    _wait_action(r.get_json()["action_id"])
    spec = captured["manifest"]["spec"]
    assert spec["deletionPolicy"] == "retain", (
        "Harvester rejects the default policy for in-place snapshot restores")
    assert spec["virtualMachineBackupName"] == "s1"


def test_restore_failure_carries_error_summary(client, monkeypatch):
    webhook_err = (b'Error from server: admission webhook denied: The request is '
                   b'invalid: spec.target.name: Please stop the VM "vm1" before '
                   b'doing a restore\n')

    def fake_run(cmd, **kw):
        if "vmi" in cmd:
            return _FakeRun(1, stderr=b"NotFound")   # VM arrêtée: pas de VMI
        if "apply" in cmd:
            return _FakeRun(1, stderr=webhook_err)
        return _FakeRun(0, stdout=b"{}")

    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    r = client.post("/api/vm/c1/ns1/vm1/restore", json={"snapshot": "s1"})
    run = _wait_action(r.get_json()["action_id"])
    assert run.status == "error"
    assert run.error_summary and "stop the VM" in run.error_summary
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS.pop(run.id, None)


def test_restore_refused_with_clear_message_while_vm_runs(client, monkeypatch):
    """v1.10.1 — user report: restoring while the VM runs surfaced the raw
    webhook error ("The request is invalid: ... Please stop the VM").
    The endpoint now pre-checks the VMI and returns an actionable 409
    BEFORE spawning any action."""
    import app as wapp

    class FakeProbe:
        returncode = 0
        stdout = "rhel9-test   10m   Running   10.52.0.9   harv1\n"
        stderr = ""
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: FakeProbe())
    r = client.post("/api/vm/harv1/default/rhel9-test/restore",
                 json={"snapshot": "s1", "new_vm": False})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "vm-running"
    assert "stopped" in body["detail"]


def test_snapshot_panel_has_no_dead_progress_column():
    """VirtualMachineBackup type=snapshot never carries status.progress
    (verified live, including during creation) — the panel used to show
    a misleading permanent 0%."""
    js = (ROOT / "web" / "static" / "js" / "vm-snapshots.js").read_text()
    assert "s.progress" not in js
    assert "<th>Progress</th>" not in js
    assert 'colspan="5"' not in js
    # the friendly stopped-VM message is i18n'd in all five languages
    i18n = (ROOT / "web" / "static" / "js" / "i18n.js").read_text()
    assert i18n.count("'snap.needsStopped'") == 5


def test_guided_restore_snapshots_then_stops_then_restores(client, monkeypatch):
    """v1.11.0 — guided flow: safety snapshot of the CURRENT state first
    (taken while the VM still runs), then stop, then restore. Order is
    the contract: snapshotting after the stop would lose the live state
    the user wants to be able to come back to."""
    calls = []

    def fake_run(cmd, **kw):
        joined = " ".join(cmd)
        if "get" in cmd and "vmi" in cmd:
            # running until a stop patch was issued
            stopped = any("patch" in c for c in calls)
            calls.append("probe-vmi")
            return _FakeRun(1 if stopped else 0,
                            stdout=b"" if stopped else b"vm1 Running\n")
        if "apply" in cmd:
            manifest = json.loads(kw["input"].decode())
            calls.append("apply-" + manifest["kind"])
            return _FakeRun(0, stdout=b"ok")
        if "patch" in cmd:
            calls.append("patch")
            return _FakeRun(0)
        calls.append(joined[:30])
        return _FakeRun(0, stdout=b"{}")

    def fake_check_output(cmd, **kw):
        if "virtualmachinebackups.harvesterhci.io" in cmd:
            calls.append("wait-presnap")
            return b"true"
        if "virtualmachinerestores.harvesterhci.io" in cmd:
            calls.append("wait-restore")
            return json.dumps({"status": {"conditions": [
                {"type": "Complete", "status": "True"}]}}).encode()
        return b"{}"

    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    monkeypatch.setattr(wapp.subprocess, "check_output", fake_check_output)
    r = client.post("/api/vm/c1/ns1/vm1/restore",
                    json={"snapshot": "s1", "pre_snapshot": True, "stop_vm": True})
    assert r.status_code == 202, r.get_json()
    run = _wait_action(r.get_json()["action_id"])
    assert run.status == "done"
    order = [c for c in calls if c in
             ("apply-VirtualMachineBackup", "patch", "apply-VirtualMachineRestore")]
    assert order == ["apply-VirtualMachineBackup", "patch",
                     "apply-VirtualMachineRestore"], calls


def test_guided_restore_ui_offers_both_options():
    js = (ROOT / "web" / "static" / "js" / "vm-snapshots.js").read_text()
    assert "snap-opt-pre" in js and "snap-opt-stop" in js
    assert "pre_snapshot: pre" in js and "stop_vm: stop" in js
    i18n = (ROOT / "web" / "static" / "js" / "i18n.js").read_text()
    for k in ("snap.optPre", "snap.optStop", "snap.restoreGo", "snap.restoreTitle"):
        assert i18n.count(f"'{k}'") == 5, k


def test_restore_completion_recognises_harvester18_ready_condition(client, monkeypatch):
    """v1.11.0 — Harvester 1.8 signals a finished restore with Ready=True
    / InProgress=False, NOT a Complete condition. The old poll loop only
    matched Complete and spun until the 1200s deadline. The action must
    now finish promptly on the Ready scheme."""
    def fake_run(cmd, **kw):
        if "get" in cmd and "vmi" in cmd:
            return _FakeRun(1, stderr=b"NotFound")  # already stopped
        return _FakeRun(0, stdout=b"ok")
    def fake_check_output(cmd, **kw):
        return json.dumps({"status": {"conditions": [
            {"type": "InProgress", "status": "False"},
            {"type": "Ready", "status": "True"}]}}).encode()
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    monkeypatch.setattr(wapp.subprocess, "check_output", fake_check_output)
    r = client.post("/api/vm/c1/ns1/vm1/restore", json={"snapshot": "s1"})
    run = _wait_action(r.get_json()["action_id"], timeout=8)
    assert run.status == "done", run.error_summary
