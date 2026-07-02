"""v1.6.5 — actions history persistence + kubectl error surfacing.

Two user-facing bugs are covered here:

1. "erreurs sans explications": `_vm_action_runner` used check_call with
   stderr=PIPE (never read — CalledProcessError.stderr stays None with
   check_call) and emitted str(e), i.e. the full command line WITH the
   kubeconfig path but WITHOUT the actual kubectl error. Now the last
   stderr line is surfaced as `error_summary` + step event message.

2. "l'historique doit être persistant": nothing ever read the SQLite
   actions DB after startup, so the Activity tab lost every run older
   than the 1h in-memory GC or a Flask restart. /api/activity now merges
   the DB, /api/action/<id> and /api/stream/<id> fall back to it.
"""

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
sys.path.insert(0, str(WEB_DIR))

import importlib
app_module = importlib.import_module("app")


@pytest.fixture()
def tmp_actions_db(tmp_path, monkeypatch):
    """Point the module at a scratch SQLite DB (empty, initialised)."""
    db = tmp_path / "actions.db"
    monkeypatch.setattr(app_module, "ACTIONS_DB", db)
    app_module._actions_init_db()
    return db


def _make_finished_run(run_id=None, status="error", exit_code=1,
                       error_summary=None, events=None):
    run = app_module.ActionRun(run_id or uuid.uuid4().hex[:12],
                               "vm-start:default/test-vm", "harv-fake", [])
    run.status = status
    run.exit_code = exit_code
    run.error_summary = error_summary
    run.ended_at = run.started_at + 1.5
    for ev in events or []:
        run.events.append(ev)
    return run


# ---------------------------------------------------------------------------
# 1. error surfacing — _vm_action_runner
# ---------------------------------------------------------------------------

class _FakeCompleted:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def test_vm_runner_surfaces_kubectl_stderr(monkeypatch, tmp_actions_db):
    """A failing patch must expose kubectl's stderr, not just 'exit 1'."""
    stderr = ('Error from server (InternalError): Internal error occurred: '
              'failed calling webhook "virtualmachines-mutator.kubevirt.io": '
              'no endpoints available for service "virt-api"')
    monkeypatch.setattr(app_module.subprocess, "run",
                        lambda *a, **kw: _FakeCompleted(1, stderr + "\n"))
    run = app_module.ActionRun("t" * 12, "vm-start:default/x", "harv-fake", [])
    app_module._vm_action_runner(run, "/nonexistent/kubeconfig", "default", "x", "Always")

    assert run.status == "error"
    assert run.exit_code == 1
    assert run.error_summary and "virt-api" in run.error_summary
    step_errors = [e for e in run.events
                   if e.get("type") == "step" and e.get("status") == "error"]
    assert step_errors, "a step error event must be emitted"
    assert "virt-api" in step_errors[-1]["message"]
    # The old str(e) leaked the kubeconfig path in the event message.
    assert "/nonexistent/kubeconfig" not in step_errors[-1]["message"]


def test_vm_runner_timeout_is_explained(monkeypatch, tmp_actions_db):
    def _boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="kubectl", timeout=15)
    monkeypatch.setattr(app_module.subprocess, "run", _boom)
    run = app_module.ActionRun("u" * 12, "vm-stop:default/x", "harv-fake", [])
    app_module._vm_action_runner(run, "/kc", "default", "x", "Halted")
    assert run.status == "error"
    assert "timed out" in (run.error_summary or "")


def test_script_runner_keeps_last_stderr_line(tmp_actions_db):
    """Bash engine runs: on non-zero exit, error_summary = last stderr line."""
    run = app_module.ActionRun("s" * 12, "shutdown", "harv-fake",
                               ["/usr/bin/env", "bash", "-c",
                                "echo step-ok; echo 'fatal: node unreachable' >&2; exit 3"])
    app_module.run_action_thread(run)
    assert run.status == "error"
    assert run.exit_code == 3
    assert run.error_summary == "fatal: node unreachable"


def test_script_runner_success_has_no_error_summary(tmp_actions_db):
    run = app_module.ActionRun("z" * 12, "status", "harv-fake",
                               ["/usr/bin/env", "bash", "-c", "echo ok"])
    app_module.run_action_thread(run)
    assert run.status == "done"
    assert run.error_summary is None


# ---------------------------------------------------------------------------
# 2. persistence — DB read-side helpers
# ---------------------------------------------------------------------------

def test_persist_then_read_back(tmp_actions_db):
    events = [{"type": "step", "step_id": "patch", "status": "error",
               "message": "no endpoints available", "ts": time.time()}]
    run = _make_finished_run(error_summary="no endpoints available",
                             events=events)
    app_module._actions_persist(run)

    rows = app_module._actions_db_recent(10)
    assert [r["id"] for r in rows] == [run.id]
    assert rows[0]["error_summary"] == "no endpoints available"
    assert rows[0]["dry_run"] is False
    assert "events" not in rows[0]          # list view stays lightweight

    d, evs = app_module._actions_db_get(run.id)
    assert d["id"] == run.id
    assert d["status"] == "error"
    assert evs == events


def test_db_get_absent_run(tmp_actions_db):
    d, evs = app_module._actions_db_get("doesnotexist")
    assert d is None and evs == []


# ---------------------------------------------------------------------------
# 3. persistence — HTTP surface (in-process Flask client, no htpasswd → open)
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_actions_db):
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _persist_ghost(run_id, error_summary=None, events=None):
    """A run that exists ONLY in SQLite (GC'd / previous process)."""
    run = _make_finished_run(run_id, error_summary=error_summary, events=events)
    app_module._actions_persist(run)
    with app_module.ACTIONS_LOCK:
        assert run_id not in app_module.ACTIONS
    return run


def test_activity_merges_sqlite_history(client):
    _persist_ghost("db00000001ab", error_summary="boom")
    data = client.get("/api/activity").get_json()
    ids = [a["id"] for a in data["actions_done"]]
    assert "db00000001ab" in ids
    row = next(a for a in data["actions_done"] if a["id"] == "db00000001ab")
    assert row["error_summary"] == "boom"


def test_activity_memory_wins_on_id_conflict(client):
    _persist_ghost("db00000002ab")
    run = _make_finished_run("db00000002ab", status="done", exit_code=0)
    with app_module.ACTIONS_LOCK:
        app_module.ACTIONS[run.id] = run
    try:
        data = client.get("/api/activity").get_json()
        rows = [a for a in data["actions_done"] if a["id"] == "db00000002ab"]
        assert len(rows) == 1               # deduplicated
        assert rows[0]["status"] == "done"  # in-memory version won
    finally:
        with app_module.ACTIONS_LOCK:
            app_module.ACTIONS.pop(run.id, None)


def test_activity_limit_param(client):
    for i in range(5):
        _persist_ghost(f"db1000000{i}ab")
    data = client.get("/api/activity?limit=3").get_json()
    assert len(data["actions_done"]) == 3
    assert client.get("/api/activity?limit=bogus").status_code == 200


def test_action_get_falls_back_to_db(client):
    _persist_ghost("db00000003ab", error_summary="kaput")
    r = client.get("/api/action/db00000003ab")
    assert r.status_code == 200
    body = r.get_json()
    assert body["error_summary"] == "kaput"
    assert client.get("/api/action/000000000000").status_code == 404


def test_stream_replays_persisted_events(client):
    events = [
        {"type": "step", "step_id": "patch", "status": "error",
         "message": "no endpoints available", "ts": 1.0},
        {"type": "status", "status": "error", "exit_code": 1, "ts": 2.0},
    ]
    _persist_ghost("db00000004ab", events=events)
    r = client.get("/api/stream/db00000004ab")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "event: step" in body
    assert "no endpoints available" in body
    assert "event: end" in body             # replay terminates the stream
