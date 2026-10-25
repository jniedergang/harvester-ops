"""Revue des écrans de réplication : répliques au-delà des nœuds de la cible
(I4), protection automatique qui recule après un échec (I5), carnet de
reprise en retard sur les réglages (I6), petites corrections (M1, M5)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_replication_model_199 import GiB, facts, plan_obj  # noqa: E402
from test_replication_routes_be2 import Clock, conn_of, make_auto  # noqa: E402
from test_replication_routes_199 import PID, base, client, db, env, only_run, rp, script_cmd, wapp  # noqa: E402,F401
from test_replication_run_199 import VMS, World  # noqa: E402
import replication_insight as ri  # noqa: E402
import replication_link as rl  # noqa: E402
import replication_run as rr  # noqa: E402


# ---------------------------------------------------------------------------
# I4 : répliques de la classe de secours face aux nœuds de la cible
# ---------------------------------------------------------------------------

THREE = {"replicas": 3, "allocatable": 0, "degraded_allocatable": 100 * GiB, "nodes": 1}
ONE = {"replicas": 1, "allocatable": 100 * GiB, "nodes": 1}


@pytest.mark.parametrize("classes,used,accepted,want", [
    ({"harvester-longhorn": THREE, "harv-rep1": ONE}, {"harvester-longhorn"}, False,
     [("harvester-longhorn", "block", 3, 1, ["harv-rep1"])]),
    ({"harvester-longhorn": THREE, "harv-rep1": ONE}, {"harvester-longhorn"}, True,
     [("harvester-longhorn", "warn", 3, 1, ["harv-rep1"])]),
    ({"harvester-longhorn": THREE, "harv-rep1": ONE}, {"harv-rep1"}, False, []),
    # deux classes en défaut, aucune qui tienne
    ({"a": THREE, "b": dict(THREE, replicas=2)}, {"a", "b"}, False,
     [("a", "block", 3, 1, []), ("b", "block", 2, 1, [])]),
    # nœuds inconnus (0) : rien d'affirmé
    ({"a": dict(THREE, nodes=0)}, {"a"}, False, []),
    # autant de répliques que de nœuds : tient
    ({"a": {"replicas": 3, "allocatable": GiB, "nodes": 3}}, {"a"}, False, []),
])
def test_replica_findings(classes, used, accepted, want):
    got = rp.replica_findings(used, classes, accepted)
    assert [(f["facts"]["storage_class"], f["level"], f["facts"]["replicas"], f["facts"]["nodes"],
             f["facts"]["suggest"]) for f in got] == want
    assert all(f["code"] == "standby-replicas-exceed-nodes" for f in got)


@pytest.mark.parametrize("phase,accepted,level", [
    ("protect", False, "block"), ("protect", True, "warn"), ("failover", False, None),
])
def test_check_reports_replicas_beyond_nodes_only_at_protect(phase, accepted, level):
    src, dst = facts(dst__storage_classes={"harvester-longhorn": THREE, "harv-rep1": ONE,
                                           "lh-cirros": ONE})
    kw = {"phase": phase}
    if accepted:
        kw["options"] = dict(rp.DEFAULT_OPTIONS, accept_degraded_standby=True)
    got = [f for f in rp.check(src, dst, plan_obj(**kw)) if f["code"] == "standby-replicas-exceed-nodes"]
    assert [f["level"] for f in got] == ([level] if level else [])


def test_accept_degraded_standby_is_a_recovery_key_and_a_boolean():
    assert "accept_degraded_standby" in rp.RECOVERY_KEYS
    with pytest.raises(rp.PlanError):
        rp.normalize_recovery({"accept_degraded_standby": 1}, ["default/vm"], "keep")
    assert rp.normalize_recovery({"accept_degraded_standby": True}, ["default/vm"], "keep") == \
        {"accept_degraded_standby": True}


# ---------------------------------------------------------------------------
# I5 : protection automatique, attente doublée après un échec
# ---------------------------------------------------------------------------

class Run:
    n = 0

    def __init__(self):
        Run.n += 1
        self.id = f"act{Run.n:09d}"
        self.status = "running"
        self.error_summary = ""


class Wall:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def _auto(env, runs):
    wall, clock = Wall(), Clock()

    def launch(p):
        r = Run()
        runs.append(r)
        return r
    return wapp.ReplicationAutoProtect(enabled=lambda: True, clock=clock, wall=wall, launch=launch), wall, clock


def _fail(run, why="the pre-check refuses this protect (disk-not-replicated)"):
    run.status, run.error_summary = "error", why


def _events(env, code):
    c = conn_of(env)
    try:
        return [e for e in db.events_after(c, 0) if e["code"] == code]
    finally:
        c.close()


def _flag(env):
    c = conn_of(env)
    try:
        return db.get_plan_flag(c, "auto-protect", PID)
    finally:
        c.close()


def _advance(wall, clock, s):
    wall.t += s
    clock.t += s


def test_a_failed_auto_protect_waits_longer_each_time_and_alerts_once(env):
    make_auto(env)
    runs = []
    ap, wall, clock = _auto(env, runs)
    every = wapp.REPLICATION_AUTO_PROTECT_EVERY_S
    assert ap.tick() == [PID]
    assert ap.tick() == []                                  # en cours : rien de plus
    _fail(runs[0])
    assert ap.tick() == []                                  # échec relevé, attente posée
    f = _flag(env)
    assert f["failures"] == 1 and f["next_at"] == wall.t + every and "disk-not-replicated" in f["error"]
    _advance(wall, clock, every - 1)
    assert ap.tick() == []
    _advance(wall, clock, 1)
    assert ap.tick() == [PID]
    _fail(runs[1])
    ap.tick()
    assert _flag(env)["failures"] == 2 and _flag(env)["next_at"] == wall.t + 2 * every
    _advance(wall, clock, 2 * every - 1)
    assert ap.tick() == []
    assert len(runs) == 2 and len(_events(env, "auto-protect-failed")) == 1     # une seule alerte par série


def test_the_wait_is_capped(env):
    make_auto(env)
    runs = []
    ap, wall, clock = _auto(env, runs)
    cap = wapp.REPLICATION_AUTO_PROTECT_BACKOFF_MAX_S
    ap.tick()
    _fail(runs[-1])
    waits = []
    for _ in range(8):
        ap.tick()                                           # relève l'échec
        waits.append(_flag(env)["next_at"] - wall.t)
        _advance(wall, clock, waits[-1])
        assert ap.tick() == [PID]
        _fail(runs[-1])
    every = wapp.REPLICATION_AUTO_PROTECT_EVERY_S
    assert waits == [min(every * 2 ** i, cap) for i in range(8)] and waits[-1] == cap


@pytest.mark.parametrize("change", ["pending", "plan"])
def test_a_new_vm_or_a_plan_change_starts_a_new_series(env, change):
    make_auto(env)
    runs = []
    ap, wall, clock = _auto(env, runs)
    ap.tick()
    _fail(runs[0])
    ap.tick()
    assert _flag(env)["failures"] == 1
    c = conn_of(env)
    if change == "pending":
        db.set_selector_watch(c, PID, [{"uid": "u-n", "namespace": "default", "name": "new", "since": "x"},
                                       {"uid": "u-m", "namespace": "default", "name": "more", "since": "y"}], [])
    else:
        db.update_plan(c, PID, now=rp._t("2030-01-01T00:00:00Z"), rpo_objective_s=1800)
    c.close()
    _advance(wall, clock, wapp.REPLICATION_AUTO_PROTECT_EVERY_S)        # au-delà de l'écart entre deux lancements
    assert ap.tick() == [PID] and _flag(env) is None


def test_a_successful_auto_protect_clears_the_series(env):
    make_auto(env)
    runs = []
    ap, wall, clock = _auto(env, runs)
    ap.tick()
    _fail(runs[0])
    ap.tick()
    _advance(wall, clock, wapp.REPLICATION_AUTO_PROTECT_EVERY_S)
    ap.tick()
    runs[1].status = "done"
    ap.tick()
    assert _flag(env) is None


def test_the_failed_series_is_a_current_alert(env):
    plan = {"id": PID, "name": "web", "source_cluster": "a", "target_cluster": "b", "state": "protected"}
    got = ri.plan_alerts(plan, {"resources": []}, auto_failed={"failures": 2, "since": "2030-01-01T00:00:00Z",
                                                               "retry": "20 min", "error": "refused"})
    assert [(a["code"], a["level"]) for a in got] == [("auto-protect-failed", "warn")]
    assert "20 min" in got[0]["message"] and "refused" in got[0]["message"]


def test_stop_waits_for_the_thread_so_no_launch_follows_the_shutdown_snapshot(env):
    import threading
    make_auto(env)
    ap, _wall, _clock = _auto(env, [])
    started = threading.Event()
    ap._thread = threading.Thread(target=lambda: (started.set(), ap._stop.wait(5)))
    ap._thread.start()
    started.wait(2)
    ap.stop(timeout=5)
    assert not ap._thread.is_alive() and ap.tick() == []


# ---------------------------------------------------------------------------
# I6 : carnet de reprise en retard sur les réglages
# ---------------------------------------------------------------------------

def _protected(tmp_path):
    w = World(tmp_path, vms=("vm-a",))
    res = rr.protect(w.ctx())
    assert res["exit"] == 0, res
    return w


def test_stale_settings_refuse_a_failover_unless_accepted_and_protect_clears_them(tmp_path):
    w = _protected(tmp_path)
    db.mark_carnet_stale(w.conn, w.pid, ["resize"], by="alice")
    db.mark_carnet_stale(w.conn, w.pid, ["network_data", "resize"], by="bob")
    assert db.get_plan_flag(w.conn, "carnet-stale", w.pid)["keys"] == ["network_data", "resize"]
    res = rr.failover(w.ctx(), dry_run=True)
    assert res["exit"] != 0 and res["code"] == "check", res
    [f] = [x for x in res["findings"] if x["code"] == "carnet-stale"]
    assert f["level"] == "block" and f["facts"]["keys"] == ["network_data", "resize"]
    res = rr.failover(w.ctx(), dry_run=True, accept_stale_carnet=True)
    assert res["exit"] == 0, res
    assert [x["level"] for x in res["findings"] if x["code"] == "carnet-stale"] == ["warn"]
    assert rr.protect(w.ctx())["exit"] == 0
    assert db.get_plan_flag(w.conn, "carnet-stale", w.pid) is None
    assert not [x for x in rr.failover(w.ctx(), dry_run=True)["findings"] if x["code"] == "carnet-stale"]


def test_a_carnet_seen_stale_by_the_follower_names_its_vm(tmp_path):
    w = _protected(tmp_path)
    uid = VMS["vm-a"]["uid"]
    db.set_health(w.conn, w.pid, uid, w.plan()["generation"], {"carnet_stale": True})
    res = rr.failover(w.ctx(), dry_run=True)
    [f] = [x for x in res["findings"] if x["code"] == "carnet-stale"]
    assert f["facts"]["vms"] == ["default/vm-a"] and f["facts"]["keys"] == []


def test_stale_carnet_is_a_current_alert_from_both_sources(tmp_path):
    w = _protected(tmp_path)
    db.mark_carnet_stale(w.conn, w.pid, ["cloud_init"])
    db.set_health(w.conn, w.pid, VMS["vm-a"]["uid"], w.plan()["generation"], {"carnet_stale": True})
    rows = db._plan_rows_alerts(w.conn, [w.plan()], rp._t("2030-01-01T00:00:00Z"))
    alerts = rows[0][1]
    got = sorted((a["code"], a["resource_uid"]) for a in alerts if a["code"] == "carnet-stale")
    assert got == [("carnet-stale", ""), ("carnet-stale", VMS["vm-a"]["uid"])]
    assert "cloud_init" in next(a["message"] for a in alerts if a["code"] == "carnet-stale" and not a["resource_uid"])


def test_plan_set_marks_the_stale_keys(tmp_path, capsys):
    from test_replication_cli_199 import run as cli_run
    from test_replication_cli_ui import spec_file
    w = _protected(tmp_path)
    rc, data, _ = cli_run(capsys, w, "plan", "set", w.plan()["name"],
                          spec_file(tmp_path, {"recovery": {"resize": {"default/vm-a": {"cpu": 1}}}}))
    assert rc == 0 and data.get("protect_needed") is True, data
    assert db.get_plan_flag(w.conn, "carnet-stale", w.pid)["keys"] == ["resize"]


def test_failover_route_passes_the_acknowledgement(client, env):
    r = client.post(f"/api/replication/plans/{PID}/failover", json={"all_vms": True, "dry_run": True,
                                                                     "accept_stale_carnet": True})
    assert r.status_code == 202, r.get_json()
    assert "--accept-stale-carnet" in script_cmd()
    r = client.post(f"/api/replication/plans/{PID}/failover", json={"all_vms": True, "dry_run": True,
                                                                     "accept_stale_carnet": "yes"})
    assert r.status_code == 400


def test_deleting_a_plan_removes_its_flags(tmp_path):
    w = World(tmp_path, vms=("vm-a",))
    db.mark_carnet_stale(w.conn, w.pid, ["mac"])
    db.set_plan_flag(w.conn, "auto-protect", w.pid, {"failures": 1})
    db.delete_plan(w.conn, w.pid)
    assert db.get_plan_flag(w.conn, "carnet-stale", w.pid) is None
    assert db.get_plan_flag(w.conn, "auto-protect", w.pid) is None


# ---------------------------------------------------------------------------
# M1 : le test de lien ne laisse pas son répertoire sur le partage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("other_file", [False, True])
def test_link_test_read_pod_removes_its_directory(tmp_path, other_file):
    """Le script de la pod de lecture, joué pour de vrai dans un répertoire
    local à la place du montage NFS : fichier et répertoire retirés, sauf si
    un autre essai y a encore son fichier."""
    import subprocess
    d = tmp_path / rl.DIR
    d.mkdir()
    (d / "t1.bin").write_bytes(b"x" * 4096)
    if other_file:
        (d / "t2.bin").write_bytes(b"y")
    script = rl.read_script("t1").replace(rl.MOUNT, str(tmp_path))
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "HOPS_LINK removed=1" in out.stdout and not (d / "t1.bin").exists()
    assert d.exists() is other_file


# ---------------------------------------------------------------------------
# M5 : les étapes d'une bascule gardées pendant les actions annexes
# ---------------------------------------------------------------------------

def test_run_hooks_keeps_the_steps_of_the_run(tmp_path, monkeypatch):
    w = _protected(tmp_path)
    ctx = w.ctx()
    ctx.steps = [{"step": "activate", "seconds": 3}]
    rid = db.record_run(w.conn, w.pid, "failover", rp._t("2030-01-01T00:00:00Z"), w.plan()["generation"])
    monkeypatch.setattr(rr, "call_hook", lambda h, **k: {"ok": True, "status": 204, "seconds": 0.1, "error": ""})
    ctx.plan = dict(ctx.plan, options=dict(ctx.plan.get("options") or {}, hooks=[
        {"name": "h", "at": "end", "method": "POST", "url": "https://h.example/", "timeout_s": 5}]))
    rr.run_hooks(ctx, "failover", "end", {}, rid, rp._t("2030-01-01T00:00:00Z"), ["default/vm-a"], [])
    run = next(r for r in db.list_runs(w.conn, w.pid) if r["id"] == rid)
    assert run["steps"] == [{"step": "activate", "seconds": 3}]


# ---------------------------------------------------------------------------
# M12 : action annexe d'une dernière vague vide
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vms,waves,selector,at,want", [
    (["default/a", "default/b"], [["default/a"], ["default/b"]], "", "wave:3", ["h"]),
    (["default/a", "default/b"], [["default/a"]], "", "wave:2", []),             # b dans la vague implicite
    (["default/a"], [["default/a"]], "", "end", []),
    ([], [["default/a"]], "dr=gold", "wave:2", []),                               # sélecteur : peut se remplir
    (["default/a"], [], "", "wave:1", []),
])
def test_hook_on_an_empty_implicit_wave_warns(vms, waves, selector, at, want):
    plan = {"vms": vms, "selector": selector}
    opts = {"waves": [{"vms": w} for w in waves], "hooks": [{"name": "h", "at": at}]}
    got = rp._empty_wave_hooks(plan, opts)
    assert [f["facts"]["hooks"] for f in got] == ([want] if want else [])
    assert all(f["code"] == "hook-wave-empty" and f["level"] == "warn" for f in got)
