"""v1.42.0 : corriger un volume dégradé, sans croire la page.

Au clic, le serveur relit le cluster, refait le diagnostic et calcule
lui-même ce qu'il applique. Une page restée ouverte, ou une requête
fabriquée, ne peut rien obtenir que le diagnostic du moment ne propose pas.
"""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


def entry(findings, health="degraded", wanted=3, longhorn="pvc-1"):
    return {"longhorn": longhorn, "health": health, "replicas_wanted": wanted,
            "findings": findings, "pvc_namespace": "default", "pvc_name": "disk"}


def finding(cause, fix=None, severity="action", facts=None):
    return {"cause": cause, "severity": severity, "facts": facts or {}, "fix": fix}


NOT_ENOUGH = finding("not-enough-nodes", {"kind": "set-replicas", "params": {"replicas": 1}},
                     facts={"wanted": 3, "nodes": 1})
REBUILD_OFF = finding("rebuild-disabled", {"kind": "enable-rebuild", "params": {"value": "5"}})
FAILED = finding("replica-failed", {"kind": "rebuild-now", "params": {"replica": "pvc-1-r-2"}},
                 severity="watch", facts={"healthy": 1})


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/kc")
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    wapp.app.config["TESTING"] = True
    return wapp.app.test_client()


def fresh_map(monkeypatch, entries):
    calls = []

    def build(cluster, kc):
        calls.append(cluster)
        return {"volumes": entries}
    monkeypatch.setattr(wapp, "_build_storage_map", build)
    return calls


def tracked(monkeypatch):
    runs = []
    monkeypatch.setattr(wapp, "track_action",
                        lambda label, cluster, worker, *args: runs.append((label, args)) or "act1")
    return runs


# ---------------------------------------------------------------------------
# Le plan : pur, testable sans Flask
# ---------------------------------------------------------------------------

def test_set_replicas_is_computed_by_the_server():
    plan, why = wapp._volume_fix_plan(entry([NOT_ENOUGH]), "set-replicas", "c1")
    assert why is None
    assert plan["args"] == ["-n", "longhorn-system", "patch", "volumes.longhorn.io", "pvc-1",
                            "--type", "merge", "-p", '{"spec": {"numberOfReplicas": 1}}']
    assert plan["params"] == {"replicas": 1}


@pytest.mark.parametrize("target", [0, 3, 4])
def test_set_replicas_stays_between_one_and_the_current_value(target):
    bad = finding("not-enough-nodes", {"kind": "set-replicas", "params": {"replicas": target}})
    plan, why = wapp._volume_fix_plan(entry([bad], wanted=3), "set-replicas", "c1")
    assert plan is None and why


def test_nothing_is_done_on_a_faulted_volume():
    plan, why = wapp._volume_fix_plan(entry([NOT_ENOUGH], health="faulted"),
                                      "set-replicas", "c1")
    assert plan is None and "faulted" in why


def test_a_fix_the_diagnosis_no_longer_offers_is_refused():
    plan, why = wapp._volume_fix_plan(entry([]), "set-replicas", "c1")
    assert plan is None and why


def test_rebuild_now_needs_a_healthy_replica():
    none_left = finding("replica-failed", {"kind": "rebuild-now",
                                           "params": {"replica": "pvc-1-r-2"}},
                        facts={"healthy": 0})
    plan, why = wapp._volume_fix_plan(entry([none_left]), "rebuild-now", "c1")
    assert plan is None and why
    plan, _ = wapp._volume_fix_plan(entry([FAILED]), "rebuild-now", "c1")
    assert plan["args"] == ["-n", "longhorn-system", "delete", "replicas.longhorn.io",
                            "pvc-1-r-2", "--wait=false"]


def test_rebuild_is_not_enabled_during_a_cluster_shutdown(monkeypatch):
    """L'arrêt gracieux coupe la reconstruction exprès : la rallumer sous
    ses pieds déferait ce qu'il est en train de faire."""
    monkeypatch.setattr(wapp, "_power_action_running", lambda c: True)
    plan, why = wapp._volume_fix_plan(entry([REBUILD_OFF]), "enable-rebuild", "c1")
    assert plan is None and why
    monkeypatch.setattr(wapp, "_power_action_running", lambda c: False)
    plan, _ = wapp._volume_fix_plan(entry([REBUILD_OFF]), "enable-rebuild", "c1")
    assert plan["args"] == ["-n", "longhorn-system", "patch", "settings.longhorn.io",
                            "concurrent-replica-rebuild-per-node-limit", "--type", "merge",
                            "-p", '{"value": "5"}']


def test_a_power_action_is_seen_only_while_it_runs():
    class Run:
        def __init__(self, action, cluster, status):
            self.action, self.cluster, self.status = action, cluster, status
    saved = dict(wapp.ACTIONS)
    try:
        wapp.ACTIONS.clear()
        wapp.ACTIONS["a"] = Run("shutdown", "c1", "done")
        wapp.ACTIONS["b"] = Run("startup", "c2", "running")
        assert wapp._power_action_running("c1") is False
        wapp.ACTIONS["c"] = Run("shutdown", "c1", "running")
        assert wapp._power_action_running("c1") is True
    finally:
        wapp.ACTIONS.clear()
        wapp.ACTIONS.update(saved)


# ---------------------------------------------------------------------------
# Le point d'accès
# ---------------------------------------------------------------------------

def test_an_unknown_kind_is_refused(client, monkeypatch):
    fresh_map(monkeypatch, [entry([NOT_ENOUGH])])
    r = client.post("/api/volume-health/c1/pvc-1/fix", json={"kind": "delete-volume"})
    assert r.status_code == 400


def test_an_unknown_volume_is_404(client, monkeypatch):
    fresh_map(monkeypatch, [entry([NOT_ENOUGH])])
    r = client.post("/api/volume-health/c1/pvc-9/fix", json={"kind": "set-replicas"})
    assert r.status_code == 404


def test_the_page_cannot_choose_the_value(client, monkeypatch):
    """Un nombre de répliques envoyé par la page est ignoré : c'est le
    diagnostic du moment qui décide."""
    fresh_map(monkeypatch, [entry([NOT_ENOUGH])])
    runs = tracked(monkeypatch)
    r = client.post("/api/volume-health/c1/pvc-1/fix",
                    json={"kind": "set-replicas", "params": {"replicas": 0}})
    assert r.status_code == 201
    label, args = runs[0]
    assert label == "volume-fix:set-replicas:pvc-1"
    plan = args[-1]
    assert plan["params"] == {"replicas": 1}


def test_the_cluster_is_read_again_not_the_cache(client, monkeypatch):
    wapp._storage_map_cache["c1"] = {"ts": time.time(), "data": {"volumes": [entry([NOT_ENOUGH])]}}
    calls = fresh_map(monkeypatch, [entry([])])     # entre-temps, redevenu sain
    tracked(monkeypatch)
    r = client.post("/api/volume-health/c1/pvc-1/fix", json={"kind": "set-replicas"})
    assert calls == ["c1"]
    assert r.status_code == 409
    wapp._storage_map_cache.clear()


def test_a_fix_no_longer_valid_says_why(client, monkeypatch):
    fresh_map(monkeypatch, [entry([REBUILD_OFF])])
    tracked(monkeypatch)
    r = client.post("/api/volume-health/c1/pvc-1/fix", json={"kind": "set-replicas"})
    assert r.status_code == 409
    assert r.get_json()["error"] == "not-applicable" and r.get_json()["detail"]


def test_an_unreachable_cluster_is_503(client, monkeypatch):
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: False)
    r = client.post("/api/volume-health/c1/pvc-1/fix", json={"kind": "set-replicas"})
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# L'action tracée : appliquer, puis constater
# ---------------------------------------------------------------------------

class FakeRun:
    def __init__(self):
        self.events, self.status, self.exit_code = [], None, None
        self.error_summary, self.ended_at, self.closed = None, None, False

    def emit(self, ev):
        self.events.append(ev)

    def close(self):
        self.closed = True

    def step(self, sid):
        return [e for e in self.events if e.get("step_id") == sid]


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


PLAN = {"kind": "set-replicas", "args": ["-n", "longhorn-system", "patch"],
        "summary": "3 -> 1", "params": {"replicas": 1}}


def test_the_runner_reports_success_once_the_volume_is_healthy(monkeypatch):
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Proc(0))
    states = iter([{"health": "degraded", "findings": [NOT_ENOUGH]},
                   {"health": "healthy", "findings": []}])
    monkeypatch.setattr(wapp, "_volume_now", lambda kc, cluster, volume: next(states))
    monkeypatch.setattr(wapp, "VOLUME_FIX_POLL", 0)
    run = FakeRun()
    wapp._volume_fix_runner(run, "/kc", "c1", "pvc-1", PLAN)
    assert run.status == "done" and run.closed
    assert run.step("observe")[-1]["status"] == "done"


def test_the_runner_does_not_claim_success_when_nothing_changed(monkeypatch):
    """Appliqué sans effet visible : l'action le dit, elle n'annonce pas un
    succès."""
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Proc(0))
    monkeypatch.setattr(wapp, "_volume_now",
                        lambda kc, cluster, volume: {"health": "degraded",
                                                     "findings": [NOT_ENOUGH]})
    monkeypatch.setattr(wapp, "VOLUME_FIX_POLL", 0)
    monkeypatch.setattr(wapp, "VOLUME_FIX_OBSERVE", 0.05)
    run = FakeRun()
    wapp._volume_fix_runner(run, "/kc", "c1", "pvc-1", PLAN)
    assert run.status == "error" and run.error_summary
    assert run.step("observe")[-1]["status"] == "warn"


def test_a_rebuild_that_started_counts_as_progress(monkeypatch):
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Proc(0))
    monkeypatch.setattr(wapp, "_volume_now", lambda kc, cluster, volume: {
        "health": "degraded", "findings": [finding("rebuilding", severity="info")]})
    monkeypatch.setattr(wapp, "VOLUME_FIX_POLL", 0)
    run = FakeRun()
    wapp._volume_fix_runner(run, "/kc", "c1", "pvc-1",
                            {**PLAN, "kind": "rebuild-now"})
    assert run.status == "done"


def test_a_failed_kubectl_stops_the_action(monkeypatch):
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Proc(1, err="forbidden"))
    run = FakeRun()
    wapp._volume_fix_runner(run, "/kc", "c1", "pvc-1", PLAN)
    assert run.status == "error" and "forbidden" in run.error_summary
    assert not run.step("observe")
