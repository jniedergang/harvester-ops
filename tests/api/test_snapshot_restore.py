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
