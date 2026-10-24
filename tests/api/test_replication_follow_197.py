"""v1.97.0 : le suiveur de la réplication (`replication_follow`), sans cluster.

Les deux clusters simulés du déroulé (`test_replication_run_197.World`),
protégés pour de vrai par `replication_run.protect`, puis une tâche
récurrente simulée : chaque disque d'une VM part à la même échéance du cron
et finit sa sauvegarde à son heure (`start_pass`). La cible n'applique une
sauvegarde qu'après une relecture (demandée : 2 s, puis 3 s d'application ;
naturelle : 150 s, coupée dans les essais de barrière).

Horloge factice partagée : les tours, les sommeils de la boucle et les délais
sont exacts, sans attendre.
"""

import math
import os
import signal
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import replication as rp  # noqa: E402
import replication_db as db  # noqa: E402
import replication_follow as rf  # noqa: E402
import replication_run as rr  # noqa: E402
from test_replication_run_197 import VMS, World, iso  # noqa: E402

INTERVAL = 180
OWNER = "cli:testhost:900"


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def protected(tmp_path, vms=("vm-b",), **kw):
    w = World(tmp_path, vms=vms, **kw)
    for name in vms:
        set_running(w, name, not VMS[name].get("stopped"))
    res = rr.protect(w.ctx())
    assert res["exit"] == 0, res
    return w


def set_running(w, name, running, since=None):
    vm = w.src.obj(rr.K_VM, "default", name)
    vm["status"] = {"printableStatus": "Running" if running else "Stopped", "ready": running}
    if not running:
        vm["status"]["conditions"] = [{"type": "Ready", "status": "False",
                                       "lastTransitionTime": since or iso(w.clock.now() - 3600)}]


def follower(w, owner=OWNER, **kw):
    return rf.Follower(w.conn, {"harvlab": w.src, "harvlab2": w.dst}, owner, now=w.clock.now, **kw)


def align(w, offset=1):
    """Juste après la prochaine échéance du cron."""
    w.clock.t = (math.floor(w.clock.t / INTERVAL) + 1) * INTERVAL + offset


def vol(vm, d):
    return f"pvc-{vm}-{d}"


def bv_name(k, volume):
    return rr.find_backup_volume(k, volume)["metadata"]["name"]


def start_pass(w, vm, finish, newly=None, snap=None, errors=(), label="volume"):
    """Une échéance de la tâche récurrente : une sauvegarde par disque de la
    VM, en cours ; chacune finit `finish[disque]` secondes plus tard
    (absente : jamais), en erreur pour les disques de `errors`. `label` :
    valeur de l'étiquette `backup-volume` (nom du volume, ou du
    BackupVolume)."""
    t0 = w.clock.now()
    names = {}
    for d in VMS[vm]["disks"]:
        v = vol(vm, d)
        name = f"rb-{vm}-{d}-{int(t0)}"
        at = iso(t0 + (snap or {}).get(d, 0))
        lab = v if label == "volume" else bv_name(w.src, v)
        o = w.src.put(rr.K_LH_BACKUP, {"metadata": {"name": name, "namespace": rr.NS,
                                                    "labels": {"backup-volume": lab}},
                                       "spec": {"snapshotName": name},
                                       "status": {"state": "InProgress", "volumeName": v, "snapshotCreatedAt": at}})
        names[d] = name
        if d not in finish:
            continue
        if d in errors:
            w.src.at(finish[d], lambda o=o: o["status"].update(state="Error", error="nfs: i/o timeout"))
        else:
            n = (newly or {}).get(d, 25_174_351)
            w.src.at(finish[d], lambda o=o, v=v, at=at, n=n: w.src.complete_backup(o, v, at, n))
    return names


def watch_patches(w):
    seen = []
    w.dst.hooks[("patch", rr.K_LH_BV)] = lambda name: seen.append((w.clock.now(), name))
    return seen


def run_until(w, f, until):
    reports = []
    while w.clock.now() < until:
        r = f.once()
        reports.append((w.clock.now(), r))
        w.clock.sleep(r["next_wake"] - w.clock.now())
    return reports


def ticks(w, **filt):
    out = db.list_ticks(w.conn, w.pid)
    return [t for t in out if all(t.get(k) == v for k, v in filt.items())]


def tick(w, backup):
    return next(t for t in db.list_ticks(w.conn, w.pid) if t["backup"] == backup)


def follow_events(w, code=None):
    rows = [dict(r) for r in w.conn.execute("SELECT * FROM events WHERE plan_id = ? AND operation = 'follow'"
                                            " ORDER BY id", (w.pid,))]
    return [e for e in rows if code is None or e["code"] == code]


def lists(k):
    return [c for c in k.calls if c[0] == "list"]


def status(w):
    p = w.plan()
    return rp.plan_status(p, db.list_resources(w.conn, w.pid), db.list_volumes(w.conn, w.pid),
                          db.list_ticks(w.conn, w.pid), rp.iso(rf._utc(w.clock.now())),
                          {u: {"running": h.get("running", True), "stopped_at": h.get("stopped_at")}
                           for u, h in db.health_map(w.conn, w.pid).items() if "running" in h},
                          db.health_map(w.conn, w.pid))


# ---------------------------------------------------------------------------
# Petites briques
# ---------------------------------------------------------------------------

BASE = rp._t("2026-10-24T10:03:00Z").timestamp() // 180      # échéance de 10:03


@pytest.mark.parametrize("snapshot,started,interval,slot", [
    ("2026-10-24T10:03:01Z", "", 180, BASE),
    ("", "2026-10-24T10:05:59Z", 180, BASE),
    ("2026-10-24T10:06:00Z", "2026-10-24T10:05:59Z", 180, BASE + 1),
    ("2026-10-24T10:02:59Z", "", 180, BASE - 1),
    ("", "", 180, None),
])
def test_slot_of_groups_backups_by_cron_tick(snapshot, started, interval, slot):
    assert rf.slot_of({"snapshot_at": snapshot, "backup_started_at": started}, interval) == slot


@pytest.mark.parametrize("vm,running", [
    ({"status": {"printableStatus": "Running"}}, True),
    ({"status": {"printableStatus": "Stopped", "ready": True}}, False),
    ({"status": {"ready": True}}, True),
    ({}, False),
])
def test_vm_is_running(vm, running):
    assert rf.vm_is_running(vm) is running


def test_rhythm_follows_the_plan_and_its_hot_settings():
    r = rf.rhythm({"interval_s": 300, "options": {}})
    assert (r["follow_poll"], r["follow_health"], r["follow_window"], r["barrier"], r["lease_ttl"]) == \
        (3, 60, 300, 150, 180)
    r = rf.rhythm({"interval_s": 300, "options": {"timeouts": {"follow_health": 30, "follow_window": 120,
                                                               "barrier": 20}}})
    assert (r["follow_health"], r["follow_window"], r["barrier"], r["lease_ttl"]) == (30, 120, 20, 90)
    assert rp.normalize_timeouts({"follow_poll": "5s"}) == {"follow_poll": 5}


def test_stopper_wait_slices_and_stops():
    s = rf.Stopper()
    slept = []

    def sleep(x):
        slept.append(x)
        if len(slept) == 3:
            s.set(signal.SIGTERM)
    s.wait(10, sleep)
    assert slept == [1.0, 1.0, 1.0] and s.is_set() and s.signum == signal.SIGTERM


# ---------------------------------------------------------------------------
# Registre : santé, statistiques, dernier point appliqué
# ---------------------------------------------------------------------------

def test_store_health_stats_and_last_applied(tmp_path):
    w = protected(tmp_path)
    uid = VMS["vm-b"]["uid"]
    assert db.set_health(w.conn, w.pid, uid, 1, {"running": True, "source_reachable": True})
    assert db.set_health(w.conn, w.pid, uid, 1, {"source_reachable": False})
    h = db.health_map(w.conn, w.pid)[uid]
    assert h["running"] is True and h["source_reachable"] is False and h["checked_at"]
    assert db.set_health(w.conn, w.pid, "nope", 1, {}) is False
    with pytest.raises(ValueError):
        db.set_health(w.conn, w.pid, uid, None, {})
    row = {"generation": 1, "resource_uid": uid, "pass_at": "2026-10-24T10:03:00Z", "ts": "2026-10-24T10:03:20Z",
           "newly_uploaded": 3, "rpo_held_s": 20}
    db.record_stats(w.conn, w.pid, row)
    db.record_stats(w.conn, w.pid, dict(row, rpo_held_s=21))
    st = db.list_stats(w.conn, w.pid, uid)
    assert len(st) == 1 and st[0]["rpo_held_s"] == 21
    with pytest.raises(ValueError):
        db.record_stats(w.conn, w.pid, {"generation": 1, "resource_uid": uid})
    last = db.last_applied_ticks(w.conn, w.pid, 1)
    assert sorted(t["source_volume"] for t in last) == sorted(vol("vm-b", d) for d in VMS["vm-b"]["disks"])
    assert db.prune(w.conn, rp._t("2027-01-01T00:00:00Z"))["stats"] == 1


# ---------------------------------------------------------------------------
# Barrière par VM
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["late", "error", "timeout"])
def test_barrier_three_disks(tmp_path, case):
    """Trois disques : le troisième finit en retard, en erreur, ou jamais.
    La relecture n'est jamais demandée avant la fin de la barrière."""
    w = protected(tmp_path)
    w.dst.freeze.add("natural-sync")
    f = follower(w)
    f.once()
    align(w)
    t0 = w.clock.now()
    seen = watch_patches(w)
    finish = {"d1": 5, "d2": 6, "d3": 40}
    if case == "timeout":
        finish.pop("d3")
    names = start_pass(w, "vm-b", finish, snap={"d1": 0, "d2": 1, "d3": 2},
                       errors=("d3",) if case == "error" else ())
    run_until(w, f, t0 + 150)
    closes = {"late": t0 + 40, "error": t0 + 40, "timeout": t0 + rf.rhythm(w.plan())["barrier"]}[case]
    assert seen, "a resync must be requested"
    assert min(t for t, _ in seen) >= closes
    patched = {n for _, n in seen}
    applied = {"late": ("d1", "d2", "d3"), "error": ("d1", "d2"), "timeout": ("d1", "d2")}[case]
    assert patched == {bv_name(w.dst, vol("vm-b", d)) for d in applied}
    for d in applied:
        t = tick(w, names[d])
        assert t["state"] == "applied" and t["resync_at"] and t["skew_s"] == (2.0 if case == "late" else 1.0)
    if case == "error":
        assert tick(w, names["d3"])["state"] == "error"
        assert [e["facts"] for e in follow_events(w, "backup-error")][0].count("nfs") == 1
    if case == "timeout":
        assert tick(w, names["d3"])["state"] == "in_progress"
        ev = follow_events(w, "barrier-timeout")
        assert len(ev) == 1 and vol("vm-b", "d3") in ev[0]["facts"]
    else:
        assert not follow_events(w, "barrier-timeout")


def test_barrier_is_per_vm(tmp_path):
    """Deux VMs : celle qui a fini n'attend pas l'autre."""
    w = protected(tmp_path, vms=("vm-a", "vm-b"))
    w.dst.freeze.add("natural-sync")
    f = follower(w)
    f.once()
    align(w)
    t0 = w.clock.now()
    seen = watch_patches(w)
    start_pass(w, "vm-a", {"root": 4, "data": 5})
    start_pass(w, "vm-b", {"d1": 5, "d2": 6, "d3": 60})
    run_until(w, f, t0 + 100)
    first_a = min(t for t, n in seen if n == bv_name(w.dst, vol("vm-a", "data")))
    first_b = min(t for t, n in seen if n == bv_name(w.dst, vol("vm-b", "d1")))
    assert first_a < t0 + 10 and first_b >= t0 + 60


# ---------------------------------------------------------------------------
# Statistiques, RPO tenu, rythme de la boucle
# ---------------------------------------------------------------------------

def test_stats_of_an_applied_pass_are_exact(tmp_path):
    w = protected(tmp_path)
    w.dst.freeze.add("natural-sync")
    f = follower(w)
    f.once()
    align(w)
    t0 = w.clock.now()
    names = start_pass(w, "vm-b", {"d1": 5, "d2": 7, "d3": 9}, newly={"d1": 1000, "d2": 2000, "d3": 3000},
                       snap={"d2": 1})
    run_until(w, f, t0 + 60)
    uid = VMS["vm-b"]["uid"]
    rows = [s for s in db.list_stats(w.conn, w.pid, uid) if s["pass_at"] == iso(t0)]
    assert len(rows) == 1
    s = rows[0]
    applied = [tick(w, n) for n in names.values()]
    assert s["disks"] == 3 and s["newly_uploaded"] == 6000 and s["skew_s"] == 1.0
    assert s["backup_s"] == pytest.approx(9.0)
    lag = max(rp._t(t["applied_at"]).timestamp() - rp._t(t["backup_done_at"]).timestamp() for t in applied)
    assert s["apply_lag_s"] == pytest.approx(lag) and 0 < lag <= 15
    # RPO tenu au moment du bilan : âge du point le plus ancien des trois disques
    oldest = min(rp._t(t["snapshot_at"]).timestamp() for t in applied)
    assert s["rpo_held_s"] == int(rp._t(s["ts"]).timestamp() - oldest)
    assert s["backlog"] == 0 and s["state"] == "protected"
    assert s["upload_bps"] and s["apply_bps"]


def test_rpo_held_is_the_age_of_the_oldest_applied_disk(tmp_path):
    """d1 et d2 appliqués à chaque passage, d3 en erreur depuis la
    protection : le RPO tenu suit d3 et finit par dépasser l'objectif."""
    w = protected(tmp_path)
    f = follower(w)
    first = min(rp._t(t["snapshot_at"]).timestamp() for t in ticks(w, source_volume=vol("vm-b", "d3")))
    for _ in range(6):
        align(w)
        start_pass(w, "vm-b", {"d1": 5, "d2": 6, "d3": 7}, errors=("d3",))
        run_until(w, f, w.clock.now() + 170)
    vm = status(w)["vms"][0]
    assert vm["rpo_held_s"] == int(w.clock.now() - first)
    ev = follow_events(w, "rpo-exceeded")
    assert len(ev) == 1 and ev[0]["resource_uid"] == VMS["vm-b"]["uid"]
    assert ev[0]["facts"].count('"rpo_objective_s": 900') == 1


def test_loop_timing_fast_during_a_pass_then_sleeps_until_the_next_tick(tmp_path):
    """Lecture toutes les 3 s pendant le passage, application vue au plus
    15 s après la fin de la dernière sauvegarde, puis sommeil jusqu'à la
    prochaine échéance (au plus la santé, 60 s)."""
    w = protected(tmp_path)
    f = follower(w)
    f.once()
    align(w)
    t0 = w.clock.now()
    names = start_pass(w, "vm-b", {"d1": 5, "d2": 6, "d3": 8})
    reports = []
    rf.follow_loop(f, sleep=w.clock.sleep, max_rounds=40,
                   on_round=lambda r: reports.append((w.clock.now(), r["next_wake"])))
    applied_at = max(rp._t(tick(w, n)["applied_at"]).timestamp() for n in names.values())
    assert applied_at - (t0 + 8) <= 15
    during = [nw - at for at, nw in reports if at < applied_at]
    assert during and all(g == pytest.approx(3) for g in during)
    after = [(at, nw) for at, nw in reports if at > applied_at + 60]
    assert after and all(nw - at <= 60 for at, nw in after)
    assert any(nw == pytest.approx(t0 - 1 + INTERVAL) for at, nw in reports if at > applied_at)
    # boucle terminée : bail rendu
    assert db.get_lease(w.conn, w.pid) is None


def test_stopped_vm_is_up_to_date_and_never_lagging(tmp_path):
    w = protected(tmp_path, vms=("vm-c",))
    set_running(w, "vm-c", False, since=iso(w.clock.now() - 7200))
    f = follower(w)
    run_until(w, f, w.clock.now() + 3 * 3600)
    h = db.health_map(w.conn, w.pid)[VMS["vm-c"]["uid"]]
    assert h["running"] is False and h["stopped_at"]
    vm = status(w)["vms"][0]
    assert vm["up_to_date"] and vm["rpo_held_s"] == 0 and vm["state"] == "protected"
    assert not follow_events(w, "rpo-exceeded")
    # VM arrêtée, aucune sauvegarde : pas de lecture rapide au-delà du délai de démarrage de la tâche
    calls = len(lists(w.src))
    run_until(w, f, w.clock.now() + 3600)
    assert len(lists(w.src)) - calls < 3600 / 3 / 4


# ---------------------------------------------------------------------------
# Appels kubectl par tour
# ---------------------------------------------------------------------------

def _two_plans(tmp_path):
    w = World(tmp_path, vms=("vm-a", "vm-b"))
    set_running(w, "vm-a", True)
    set_running(w, "vm-b", True)
    db.update_plan(w.conn, w.pid, vms=["default/vm-a"])
    res = rr.protect(w.ctx())
    assert res["exit"] == 0, res
    req = {"name": "plan-b", "source_cluster": "harvlab", "target_cluster": "harvlab2", "rpo_objective_s": "15m",
           "interval_s": "3m", "vms": ["default/vm-b"], "mappings": {"networks": {"default/prod": "default/lab-net"}},
           "options": {"cloud_init": "keep", "mac": "keep"}}
    pid2 = db.insert_plan(w.conn, rp.normalize_plan(req), plan_id="rp-ba9876543210")["id"]
    ctx = rr.Ctx(w.conn, pid2, w.src, w.dst, emit=lambda *a: None, sleep=w.clock.sleep, now=w.clock.now,
                 progress=lambda *a, **k: None, owner="cli:testhost:77", host="testhost", pid_alive=lambda p: True)
    assert rr.protect(ctx)["exit"] == 0
    return w, pid2


def test_kubectl_lists_per_round_do_not_grow_with_the_plans(tmp_path):
    w, pid2 = _two_plans(tmp_path)
    counts = {}
    for label, ids in (("one", {w.pid}), ("two", {w.pid, pid2})):
        f = follower(w, owner=f"cli:testhost:{label}", plan_ids=ids)
        for k in (w.src, w.dst):
            k.calls.clear()
        f.once()                                      # tour de santé et de specs
        health = (len(lists(w.src)), len(lists(w.dst)))
        w.clock.sleep(3)
        for k in (w.src, w.dst):
            k.calls.clear()
        f.once()                                      # tour rapide
        fast = (len(lists(w.src)), len(lists(w.dst)))
        counts[label] = (health, fast)
        assert set(f.report["followed"]) == ids
        f.close()
    assert counts["one"] == counts["two"]
    assert counts["two"][1] == (1, 1)                  # Backups de la source, moteurs de la cible
    assert counts["two"][0] == (3, 2)                  # + VMs et PVC source, volumes de secours cible


# ---------------------------------------------------------------------------
# Relecture appliquée en retard, renouvelée et bornée
# ---------------------------------------------------------------------------

def test_resync_not_applied_is_renudged_then_apply_late(tmp_path):
    w = protected(tmp_path)
    w.dst.freeze |= {"natural-sync", "resync"}
    f = follower(w)
    f.once()
    align(w)
    t0 = w.clock.now()
    seen = watch_patches(w)
    names = start_pass(w, "vm-b", {"d1": 5, "d2": 5, "d3": 5})
    run_until(w, f, t0 + 900)
    late = follow_events(w, "apply-late")
    assert len(late) == 3                                       # une fois par disque
    assert all(e["ts"] >= iso(t0 + 5 + 600)[:19] for e in late)
    per_bv = {}
    for t, n in seen:
        per_bv.setdefault(n, []).append(t)
    for times in per_bv.values():
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert all(g >= 30 - 1e-6 for g in gaps)                 # jamais plus d'une demande par 30 s
        assert len(times) <= 600 / 30 + 300 / 60 + 2
    # la relecture revient : application constatée
    w.dst.freeze.discard("resync")
    run_until(w, f, w.clock.now() + 200)              # relance au rythme de la santé une fois en retard
    assert all(tick(w, n)["state"] == "applied" for n in names.values())


# ---------------------------------------------------------------------------
# Cluster injoignable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side", ["source", "target"])
def test_unreachable_cluster_never_crashes_and_comes_back(tmp_path, side):
    w = protected(tmp_path)
    w.dst.freeze.add("natural-sync")
    f = follower(w)
    f.once()
    k = w.src if side == "source" else w.dst
    align(w)
    t0 = w.clock.now()
    names = start_pass(w, "vm-b", {"d1": 5, "d2": 6, "d3": 7})
    k.down = True
    reports = run_until(w, f, t0 + 120)
    assert all("error" not in r for _, r in reports)
    assert all(w.pid in r["followed"] for _, r in reports)
    ev = follow_events(w, f"{side}-unreachable")
    assert len(ev) == 1 and ev[0]["side"] == side and ev[0]["resource_uid"] == VMS["vm-b"]["uid"]
    h = db.health_map(w.conn, w.pid)[VMS["vm-b"]["uid"]]
    assert h[f"{side}_reachable"] is False
    if side == "source":
        assert not any(t["backup"] in names.values() for t in ticks(w))
    else:
        # source lue : passages rangés, relecture en attente de la cible
        assert {tick(w, n)["state"] for n in names.values()} == {"completed"}
        assert not any(tick(w, n)["resync_at"] for n in names.values())
    assert status(w)["vms"][0]["state"] == ("source-unreachable" if side == "source" else "protected")
    k.down = False
    run_until(w, f, w.clock.now() + 60)
    assert len(follow_events(w, f"{side}-reachable")) == 1
    assert all(tick(w, n)["state"] == "applied" for n in names.values())
    assert db.health_map(w.conn, w.pid)[VMS["vm-b"]["uid"]][f"{side}_reachable"] is True


def test_one_cluster_down_does_not_stop_the_plans_of_other_clusters(tmp_path):
    """Un plan vers un cluster inconnu (déclaration absente) : les autres
    plans continuent, l'erreur devient un événement."""
    w = protected(tmp_path)
    req = {"name": "plan-x", "source_cluster": "harvlab", "target_cluster": "ghost", "rpo_objective_s": "15m",
           "interval_s": "3m", "vms": ["default/vm-b"], "options": {"cloud_init": "keep", "mac": "keep"}}
    other = db.insert_plan(w.conn, rp.normalize_plan(req), plan_id="rp-00000000000f")["id"]
    db.set_state(w.conn, other, "draft", "protecting")
    db.set_state(w.conn, other, "protecting", "protected")
    db.upsert_resource(w.conn, other, {"generation": 1, "resource_uid": "u-x", "source_host": "harvlab",
                                       "target_host": "ghost", "namespace": "default", "name": "vm-b",
                                       "state": "protected"})
    db.upsert_volume(w.conn, other, {"generation": 1, "vm_uid": "u-x", "disk": "d1",
                                     "source_volume": vol("vm-b", "d1"), "target_volume": "hr-x", "state": "standby"})

    def clusters(name):
        if name == "ghost":
            raise SystemExit("unknown cluster: ghost")
        return {"harvlab": w.src, "harvlab2": w.dst}[name]
    f = rf.Follower(w.conn, clusters, OWNER, now=w.clock.now)
    align(w)
    names = start_pass(w, "vm-b", {"d1": 5, "d2": 6, "d3": 7})
    run_until(w, f, w.clock.now() + 60)
    assert all(tick(w, n)["state"] == "applied" for n in names.values())
    ev = [dict(r) for r in w.conn.execute("SELECT * FROM events WHERE plan_id = ? AND code = 'target-unreachable'",
                                          (other,))]
    assert len(ev) == 1 and ev[0]["host"] == "ghost"


def test_bad_data_in_one_plan_is_an_event_not_a_crash(tmp_path):
    w = protected(tmp_path)
    f = follower(w)
    f.once()
    align(w)
    o = w.src.put(rr.K_LH_BACKUP, {"metadata": {"name": "rb-bad", "namespace": rr.NS,
                                                "labels": {"backup-volume": vol("vm-b", "d1")}},
                                   "status": {"state": "Completed", "volumeName": vol("vm-b", "d1"),
                                              "snapshotCreatedAt": "not-a-date"}})
    assert o
    r = f.once_safe()
    assert r["skipped"].get(w.pid) == "error"
    assert len(follow_events(w, "follow-error")) == 1
    f.once_safe()
    assert len(follow_events(w, "follow-error")) == 1           # écrit une fois


# ---------------------------------------------------------------------------
# Bail
# ---------------------------------------------------------------------------

def test_two_followers_never_work_on_the_same_plan(tmp_path):
    w = protected(tmp_path)
    a, b = follower(w, "cli:testhost:1"), follower(w, "console:testhost:2")
    assert w.pid in a.once()["followed"]
    w.clock.sleep(3)
    before = len(w.dst.mutations())
    rb = b.once()
    assert rb["skipped"][w.pid] == "lease-held" and rb["calls"] == 0
    assert len(w.dst.mutations()) == before
    # reprise forcée (`follow --take-over`)
    tb = follower(w, "console:testhost:2", take_over=True)
    assert w.pid in tb.once()["followed"]
    assert db.get_lease(w.conn, w.pid)["owner"] == "console:testhost:2"
    assert len(follow_events(w, "lease-taken-over")) == 1
    ra = a.once()
    assert ra["skipped"][w.pid] == "lease-held"
    assert len(follow_events(w, "lease-lost")) == 1
    # arrêt propre : bail rendu, l'autre reprend aussitôt
    tb.close()
    assert w.pid in a.once()["followed"]


def test_lease_expires_after_three_health_rounds(tmp_path):
    w = protected(tmp_path)
    a, b = follower(w, "cli:testhost:1"), follower(w, "cli:testhost:2")
    a.once()                               # a s'arrête brutalement (pas de close)
    w.clock.sleep(179)
    assert b.once()["skipped"][w.pid] == "lease-held"
    w.clock.sleep(2)
    assert w.pid in b.once()["followed"]


def test_no_write_after_losing_the_lease_mid_round(tmp_path):
    """Un autre suiveur reprend le bail pendant le tour (après les
    lectures) : aucune relecture n'est plus demandée pour ce plan."""
    w = protected(tmp_path)
    w.dst.freeze.add("natural-sync")
    f = follower(w)
    f.once()
    align(w)
    start_pass(w, "vm-b", {"d1": 1, "d2": 1, "d3": 1})
    w.clock.sleep(5)
    seen = watch_patches(w)
    w.dst.hooks[("list", rr.K_LH_ENGINE)] = lambda _n: db.take_lease(w.conn, w.pid, "console:other:1", 180,
                                                                     take_over=True)
    r = f.once()
    assert r["skipped"][w.pid] == "lease-lost"
    assert seen == []
    assert len(follow_events(w, "lease-lost")) == 1


def test_paused_plan_is_not_touched(tmp_path):
    w = protected(tmp_path)
    db.update_plan(w.conn, w.pid, options={"paused": True})
    f = follower(w)
    align(w)
    start_pass(w, "vm-b", {"d1": 5, "d2": 5, "d3": 5})
    for k in (w.src, w.dst):
        k.calls.clear()
    reports = run_until(w, f, w.clock.now() + 60)
    assert all(r["skipped"][w.pid] == "paused" for _, r in reports)
    assert lists(w.src) == [] and w.dst.calls == []
    # la reprise est lue à chaud au tour suivant
    db.update_plan(w.conn, w.pid, options={"paused": False})
    run_until(w, f, w.clock.now() + 60)
    assert {t["state"] for t in ticks(w) if t["backup"].startswith("rb-")} == {"applied"}


@pytest.mark.parametrize("state,mode,modes", [
    ("draft", "console", ("console",)),
    ("unprotected", "console", ("console",)),
    ("protected", "off", ("console", "cli")),
    ("protected", "cli", ("console",)),
])
def test_no_work_when_nothing_is_followed(tmp_path, state, mode, modes):
    w = World(tmp_path, vms=("vm-b",))
    if state != "draft":
        if state == "protected":
            assert rr.protect(w.ctx())["exit"] == 0
        else:
            db.set_state(w.conn, w.pid, "draft", "measuring")
            db.set_state(w.conn, w.pid, "measuring", "unprotecting")
            db.set_state(w.conn, w.pid, "unprotecting", "unprotected")
    db.update_plan(w.conn, w.pid, options={"follow": mode})
    for k in (w.src, w.dst):
        k.calls.clear()
    f = follower(w, modes=modes)
    r = f.once()
    assert r["calls"] == 0 and w.src.calls == [] and w.dst.calls == []
    assert r["next_wake"] == pytest.approx(w.clock.now() + 60)
    assert db.get_lease(w.conn, w.pid) is None


# ---------------------------------------------------------------------------
# Dérive de la spec, VM basculée
# ---------------------------------------------------------------------------

def _vm_change(w, how):
    vm = w.src.obj(rr.K_VM, "default", "vm-b")
    vm["metadata"]["generation"] = vm["metadata"]["generation"] + 1
    if how == "run-strategy":
        vm["spec"]["runStrategy"] = "Halted"
    elif how == "annotation":
        vm["metadata"].setdefault("annotations", {})["x"] = "y"
    elif how == "memory":
        vm["spec"]["template"]["spec"]["domain"]["memory"] = {"guest": "8Gi"}
    elif how == "disk-added":
        vm["spec"]["template"]["spec"]["volumes"].append({"name": "d4", "persistentVolumeClaim":
                                                          {"claimName": "vm-b-d4"}})
        vm["spec"]["template"]["spec"]["domain"]["devices"]["disks"].append({"name": "d4", "disk": {"bus": "virtio"}})
    elif how == "size":
        # la VM n'a pas changé (même génération), son PVC si
        w.src.obj(rr.K_PVC, "default", "vm-b-d1")["spec"]["resources"]["requests"]["storage"] = "4Gi"
        vm["metadata"]["generation"] -= 1
    elif how == "network":
        vm["spec"]["template"]["spec"]["networks"][0]["multus"]["networkName"] = "default/other"


@pytest.mark.parametrize("how,drift", [
    ("none", False), ("run-strategy", False), ("annotation", False), ("memory", False),
    ("disk-added", True), ("size", True), ("network", True),
])
def test_spec_drift_by_digest(tmp_path, how, drift):
    w = protected(tmp_path)
    f = follower(w)
    f.once()
    assert not follow_events(w, "plan-drift")              # juste après protect : aucune dérive
    if how != "none":
        _vm_change(w, how)
    run_until(w, f, w.clock.now() + 1300)                   # deux relectures de spec
    ev = follow_events(w, "plan-drift")
    assert len(ev) == (1 if drift else 0)
    st = status(w)["vms"][0]
    assert ("plan-drift" in st["reasons"]) is drift
    h = db.health_map(w.conn, w.pid)[VMS["vm-b"]["uid"]]
    assert h["vm_spec_digest"] and h["vm_generation_seen"]


def test_failed_over_vm_is_not_followed_and_its_return_is_flagged(tmp_path):
    w = protected(tmp_path, vms=("vm-a", "vm-b"))
    res = rr.failover(w.ctx(), vms=["default/vm-a"])
    assert res["exit"] == 0, res
    assert w.plan()["state"] == "protected"
    set_running(w, "vm-a", False)                 # arrêtée par la bascule
    f = follower(w)
    f.once()
    align(w)
    seen = watch_patches(w)
    start_pass(w, "vm-a", {"root": 5, "data": 5})
    start_pass(w, "vm-b", {"d1": 5, "d2": 5, "d3": 5})
    run_until(w, f, w.clock.now() + 60)
    a_bvs = {bv_name(w.dst, vol("vm-a", d)) for d in VMS["vm-a"]["disks"]}
    assert seen and not a_bvs & {n for _, n in seen}
    assert not follow_events(w, "source-vm-running-after-failover")
    set_running(w, "vm-a", True)
    run_until(w, f, w.clock.now() + 130)
    ev = follow_events(w, "source-vm-running-after-failover")
    assert len(ev) == 1 and ev[0]["resource_uid"] == VMS["vm-a"]["uid"] and ev[0]["side"] == "source"


def test_source_vm_annotated_as_failed_over_is_skipped_even_if_the_registry_says_protected(tmp_path):
    """Base restaurée d'avant la bascule (ou bascule par une autre console) :
    l'annotation posée sur la VM source suffit à la sortir du suivi."""
    w = protected(tmp_path, vms=("vm-a", "vm-b"))
    w.src.obj(rr.K_VM, "default", "vm-a")["metadata"]["annotations"] = {rr.FAILED_OVER_TO: "harvlab2/default/vm-a"}
    f = follower(w)
    f.once()
    align(w)
    seen = watch_patches(w)
    start_pass(w, "vm-a", {"root": 5, "data": 5})
    start_pass(w, "vm-b", {"d1": 5, "d2": 5, "d3": 5})
    run_until(w, f, w.clock.now() + 60)
    a_bvs = {bv_name(w.dst, vol("vm-a", d)) for d in VMS["vm-a"]["disks"]}
    assert seen and not a_bvs & {n for _, n in seen}
    assert not [t for t in ticks(w) if t["backup"].startswith("rb-vm-a")]


def test_failed_over_plan_watches_the_source_vm_only(tmp_path):
    w = protected(tmp_path)
    assert rr.failover(w.ctx())["exit"] == 0
    assert w.plan()["state"] == "failed_over"
    set_running(w, "vm-b", False)
    f = follower(w)
    for k in (w.src, w.dst):
        k.calls.clear()
    f.once()
    assert w.dst.calls == [] and [c[1] for c in lists(w.src)] == [rr.K_VM]
    set_running(w, "vm-b", True)
    run_until(w, f, w.clock.now() + 120)
    assert len(follow_events(w, "source-vm-running-after-failover")) == 1


# ---------------------------------------------------------------------------
# Mesure, signal d'arrêt
# ---------------------------------------------------------------------------

def test_measuring_records_passes_and_never_reads_the_target(tmp_path):
    w = World(tmp_path, vms=("vm-b",))
    set_running(w, "vm-b", True)
    assert rr.measure_start(w.ctx(), interval="3m")["exit"] == 0
    f = follower(w)
    w.dst.calls.clear()
    align(w)
    names = start_pass(w, "vm-b", {"d1": 5, "d2": 6, "d3": 9}, errors=("d3",))
    run_until(w, f, w.clock.now() + 60)
    got = {m["backup"]: m["state"] for m in db.list_ticks(w.conn, w.pid, table="measures")}
    assert got == {names["d1"]: "completed", names["d2"]: "completed", names["d3"]: "error"}
    assert w.dst.calls == []


def test_backups_labelled_with_the_backup_volume_name_are_found(tmp_path):
    w = protected(tmp_path)
    f = follower(w)
    f.once()
    align(w)
    names = start_pass(w, "vm-b", {"d1": 5, "d2": 6, "d3": 7}, label="bv")
    run_until(w, f, w.clock.now() + 60)
    assert all(tick(w, n)["state"] == "applied" for n in names.values())


def test_sigterm_stops_the_loop_between_rounds_and_releases_the_lease(tmp_path):
    w = protected(tmp_path)
    f = follower(w)
    stopper = rf.Stopper()
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        stopper.install()
        seen = []

        def on_round(r):
            seen.append(r)
            if len(seen) == 2:
                os.kill(os.getpid(), signal.SIGTERM)
        out = rf.follow_loop(f, stopper, sleep=w.clock.sleep, on_round=on_round)
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    assert out == {"rounds": 2, "stopped": True, "signal": signal.SIGTERM}
    assert db.get_lease(w.conn, w.pid) is None


def test_memory_of_a_long_running_follower_is_bounded(tmp_path):
    w = protected(tmp_path)
    f = follower(w)
    for _ in range(4):
        align(w)
        start_pass(w, "vm-b", {"d1": 5, "d2": 6, "d3": 7}, errors=("d3",))
        run_until(w, f, w.clock.now() + 170)
    assert len(f.stats_done) >= 4 and any(k[0] == "backup-error" for k in f.flags)
    w.clock.sleep(3 * 86400)
    f.once()
    # seuls restent les derniers points appliqués relus à ce tour
    assert len(f.stats_done) <= 3 and all(at == w.clock.now() for at in f.stats_done.values())
    assert not any(k[0] == "backup-error" for k in f.flags)
    assert len(follow_events(w, "backup-error")) == 4


def test_follow_once_is_a_single_round_without_memory(tmp_path):
    w = protected(tmp_path)
    r = rf.follow_once(w.conn, {"harvlab": w.src, "harvlab2": w.dst}, now=w.clock.now, owner=OWNER)
    assert w.pid in r["followed"] and db.get_lease(w.conn, w.pid) is None
