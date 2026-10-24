"""Écrans de la réplication : lectures dérivées (`replication_insight`) et
leur lecture au registre (`replication_db`).

Résumé des transferts, séries des graphiques par tranches, joignabilité,
alertes en cours (calculées sur l'état présent), VMs du sélecteur rangées
par le suiveur, réglages, essais de lien, migrations additives. Fonctions
pures en tableaux ; un cas par chemin."""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import replication as rp  # noqa: E402
import replication_db as db  # noqa: E402
import replication_insight as ri  # noqa: E402

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
PID = "rp-0123456789ab"


def at(seconds_ago):
    return rp.iso(NOW - timedelta(seconds=seconds_ago))


def st(ago, uid="u1", newly=100, up=None, ap=None, rpo=None, backlog=0, passes=1):
    return {"pass_at": at(ago), "resource_uid": uid, "newly_uploaded": newly, "upload_bps": up, "apply_bps": ap,
            "rpo_held_s": rpo, "backlog": backlog, "passes": passes}


# ---------------------------------------------------------------------------
# Résumé des transferts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rows,expect", [
    ([], {"last_at": None, "last_bytes": 0, "upload_bps": None, "apply_bps": None, "bytes_24h": 0,
          "passes_24h": 0}),
    # le dernier passage de n'importe quelle VM, sommes sur 24 h
    ([st(600, "u1", 10, 5.0, 6.0), st(300, "u2", 20, 7.0, 8.0), st(90000, "u1", 999, 1.0, 1.0)],
     {"last_at": at(300), "last_bytes": 20, "upload_bps": 7.0, "apply_bps": 8.0, "bytes_24h": 30,
      "passes_24h": 2}),
    # une ligne réduite à l'heure compte ses passages
    ([st(3600, newly=50, passes=12)], {"last_at": at(3600), "last_bytes": 50, "upload_bps": None,
                                       "apply_bps": None, "bytes_24h": 50, "passes_24h": 12}),
    # seulement des passages plus vieux que 24 h : dernier passage, totaux nuls
    ([st(2 * 86400, newly=7, up=3.0)], {"last_at": at(2 * 86400), "last_bytes": 7, "upload_bps": 3.0,
                                        "apply_bps": None, "bytes_24h": 0, "passes_24h": 0}),
])
def test_transfer_summary(rows, expect):
    assert ri.transfer_summary(rows, NOW) == expect


def test_transfer_summary_boundary_is_inclusive_at_24h():
    assert ri.transfer_summary([st(86400, newly=5)], NOW)["bytes_24h"] == 5
    assert ri.transfer_summary([st(86401, newly=5)], NOW)["bytes_24h"] == 0


# ---------------------------------------------------------------------------
# Séries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,range_s,bucket", [("1h", 3600, 60), ("24h", 86400, 600), ("7d", 604800, 3600),
                                                 ("30d", 2592000, 21600)])
def test_series_ranges(name, range_s, bucket):
    assert ri.series_range(name) == (range_s, bucket)


@pytest.mark.parametrize("bad", ["2h", "", None, "24H"])
def test_series_range_refuses_unknown(bad):
    with pytest.raises(ValueError):
        ri.series_range(bad)


def test_bucket_series_aggregates_per_bucket_and_omits_empty_ones():
    rows = [
        # tranche de 11:50 (600 s) : deux passages
        st(590, "u1", 100, up=10.0, ap=20.0, rpo=100, backlog=1),
        st(500, "u2", 300, up=30.0, ap=None, rpo=400, backlog=3),
        # tranche de 11:30 : une ligne réduite (4 passages) et une simple
        st(1500, "u1", 40, up=8.0, ap=4.0, rpo=None, passes=4),
        st(1400, "u1", 10, up=18.0, ap=None, rpo=50),
        # hors fenêtre
        st(90000, "u1", 999, up=1.0),
    ]
    pts = ri.bucket_series(rows, 600, NOW, 86400)
    assert [p["t"] for p in pts] == ["2026-10-25T11:30:00Z", "2026-10-25T11:50:00Z"]
    a, b = pts
    # moyenne pondérée par les passages : (8*4 + 18*1) / 5 = 10
    assert a == {"t": "2026-10-25T11:30:00Z", "rpo_held_s": 50, "upload_bps": 10.0, "apply_bps": 4.0,
                 "bytes": 50, "passes": 5, "backlog": 0}
    assert b == {"t": "2026-10-25T11:50:00Z", "rpo_held_s": 400, "upload_bps": 20.0, "apply_bps": 20.0,
                 "bytes": 400, "passes": 2, "backlog": 3}


def test_bucket_series_all_null_values_stay_null():
    [p] = ri.bucket_series([st(30, rpo=None, up=None, ap=None)], 60, NOW, 3600)
    assert (p["rpo_held_s"], p["upload_bps"], p["apply_bps"], p["backlog"]) == (None, None, None, 0)


def test_bucket_start_is_aligned_on_the_epoch():
    [p] = ri.bucket_series([st(3 * 3600 + 5)], 21600, NOW, 30 * 86400)
    assert p["t"] == "2026-10-25T06:00:00Z"


# ---------------------------------------------------------------------------
# Joignabilité
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("health,expect", [
    ({}, {"source": None, "target": None, "at": None}),
    ({"u1": {"checked_at": "2026-10-25T11:00:00Z"}}, {"source": None, "target": None, "at": "2026-10-25T11:00:00Z"}),
    ({"u1": {"source_reachable": True, "target_reachable": True, "checked_at": "2026-10-25T11:00:00Z"},
      "u2": {"source_reachable": False, "target_reachable": None, "checked_at": "2026-10-25T11:05:00Z"}},
     {"source": False, "target": True, "at": "2026-10-25T11:05:00Z"}),
    ({"u1": {"source_reachable": None, "target_reachable": False}}, {"source": None, "target": False, "at": None}),
])
def test_reach_takes_the_worst_value(health, expect):
    assert ri.reach(health) == expect


# ---------------------------------------------------------------------------
# Alertes
# ---------------------------------------------------------------------------

PLAN = {"id": PID, "name": "erp", "source_cluster": "harvlab", "target_cluster": "harvlab2", "state": "protected"}


def res_row(uid, name, reasons, rpo=None):
    return {"resource_uid": uid, "namespace": "default", "name": name, "reasons": reasons, "rpo_held_s": rpo,
            "rpo_objective_s": 600}


@pytest.mark.parametrize("plan,resources,health,pending,expect", [
    (PLAN, [res_row("u1", "web", [])], {}, [], []),
    (PLAN, [res_row("u1", "web", ["rpo-exceeded"], 900)], {}, [], [("rpo-exceeded", "warn", "u1")]),
    # raisons qui ne sont pas des alertes
    (PLAN, [res_row("u1", "web", ["rpo-drift", "no-point"])], {}, [], []),
    (PLAN, [res_row("u1", "web", ["source-unreachable", "backup-error", "standby-degraded"])], {}, [],
     [("source-unreachable", "error", "u1"), ("backup-error", "error", "u1"), ("standby-degraded", "warn", "u1")]),
    (PLAN, [res_row("u1", "web", ["disk-never-backs-up", "plan-drift"])], {}, [],
     [("disk-never-backs-up", "warn", "u1"), ("plan-drift", "warn", "u1")]),
    (PLAN, [res_row("u1", "web", [])], {"u1": {"source_running_after_failover": True}}, [],
     [("source-vm-running-after-failover", "error", "u1")]),
    (PLAN, [res_row("u1", "web", [])], {"u1": {"source_running_after_failover": False}}, [], []),
    (dict(PLAN, state="error", error_origin="protecting"), [], {}, [], [("plan-error", "error", "")]),
    (PLAN, [], {}, [{"uid": "u9", "namespace": "default", "name": "new", "since": "2026-10-25T10:00:00Z"}],
     [("selector-new-vm", "warn", "u9")]),
])
def test_plan_alerts(plan, resources, health, pending, expect):
    got = ri.plan_alerts(plan, {"resources": resources}, health, pending, {})
    assert [(a["code"], a["level"], a["resource_uid"]) for a in got] == expect
    for a in got:
        assert set(a) == {"plan", "plan_name", "resource_uid", "namespace", "name", "source_host", "target_host",
                          "code", "level", "since", "message"}
        assert a["plan"] == PID and a["source_host"] == "harvlab" and a["target_host"] == "harvlab2"
        assert "{" not in a["message"] and a["message"]


def test_plan_alerts_since_comes_from_the_journal_and_the_watch():
    since = {("u1", "rpo-exceeded"): "2026-10-25T09:00:00Z", ("", "disk-never-backs-up"): "2026-10-25T08:00:00Z",
             ("", "error"): "2026-10-25T07:00:00Z"}
    rows = {"resources": [res_row("u1", "web", ["rpo-exceeded", "disk-never-backs-up", "plan-drift"], 900)]}
    got = {a["code"]: a for a in ri.plan_alerts(dict(PLAN, state="error"), rows, {}, [
        {"uid": "u9", "namespace": "default", "name": "n", "since": "2026-10-25T10:00:00Z"}], since)}
    assert got["rpo-exceeded"]["since"] == "2026-10-25T09:00:00Z"
    assert got["disk-never-backs-up"]["since"] == "2026-10-25T08:00:00Z"   # constat du plan entier
    assert got["plan-drift"]["since"] is None
    assert got["plan-error"]["since"] == "2026-10-25T07:00:00Z"
    assert got["selector-new-vm"]["since"] == "2026-10-25T10:00:00Z"
    assert got["rpo-exceeded"]["message"] == "RPO held 900 s, objective 600 s"
    assert "(from unknown)" in got["plan-error"]["message"]


def test_sort_alerts_errors_first_then_oldest():
    a = [{"level": "warn", "since": "2026-10-25T01:00:00Z", "plan_name": "a", "namespace": "", "name": "", "code": "x"},
         {"level": "error", "since": None, "plan_name": "b", "namespace": "", "name": "", "code": "y"},
         {"level": "error", "since": "2026-10-25T05:00:00Z", "plan_name": "c", "namespace": "", "name": "",
          "code": "z"}]
    assert [x["code"] for x in ri.sort_alerts(a)] == ["z", "y", "x"]


def test_link_passive():
    rows = [st(7200, up=10.0, ap=20.0, newly=5), st(3500, up=30.0, ap=None, newly=7), st(90000, up=99.0)]
    out = ri.link_passive(rows, NOW)
    assert out["last_at"] == at(3500) and out["upload_bps"] == 30.0 and out["apply_bps"] is None
    assert out["upload_bps_p50_24h"] == 10.0 and out["apply_bps_p50_24h"] == 20.0
    assert out["bytes_24h"] == 12 and out["passes_24h"] == 2
    assert [p["t"] for p in out["series"]] == ["2026-10-25T10:00:00Z", "2026-10-25T11:00:00Z"]
    assert set(out["series"][0]) == {"t", "upload_bps", "apply_bps"}


# ---------------------------------------------------------------------------
# Registre : lignes de plan, séries, alertes, VMs du sélecteur, réglages, liens
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "r.db"))
    req = rp.normalize_plan({"name": "erp", "source_cluster": "harvlab", "target_cluster": "harvlab2",
                             "rpo_objective_s": "10m", "interval_s": "3m", "selector": "dr=gold",
                             "namespace": "default"})
    db.insert_plan(c, req, plan_id=PID)
    db.upsert_resource(c, PID, {"resource_uid": "u1", "source_host": "harvlab", "target_host": "harvlab2",
                                "namespace": "default", "name": "web", "generation": 1})
    yield c
    c.close()


def test_plan_rows_carry_transfer_pending_alerts_and_reach(conn):
    db.record_stats(conn, PID, {"generation": 1, "resource_uid": "u1", "pass_at": at(400), "ts": at(390),
                                "newly_uploaded": 1000, "upload_bps": 50.0, "apply_bps": 60.0})
    db.record_stats(conn, PID, {"generation": 1, "resource_uid": "u1", "pass_at": at(200), "ts": at(190),
                                "newly_uploaded": 3000, "upload_bps": 70.0, "apply_bps": 80.0})
    db.set_selector_watch(conn, PID, [{"uid": "u9", "namespace": "default", "name": "new-vm", "since": at(100)}], [])
    db.set_health(conn, PID, "u1", 1, {"source_reachable": True, "target_reachable": False}, now=NOW)
    [row] = db.plan_rows(conn, db.list_plans(conn), NOW)
    assert row["transfer"] == {"last_at": at(200), "last_bytes": 3000, "upload_bps": 70.0, "apply_bps": 80.0,
                               "bytes_24h": 4000, "passes_24h": 2}
    assert row["pending_vms"] == ["default/new-vm"]
    assert row["alerts"] == 1                    # la VM du sélecteur (plan en brouillon : pas de raison)
    assert row["reach"] == {"source": True, "target": False, "at": rp.iso(NOW)}


def test_pending_vms_only_for_selector_plans(conn):
    db.set_selector_watch(conn, PID, [{"uid": "u9", "namespace": "default", "name": "x", "since": at(1)}], [])
    conn.execute("UPDATE plans SET selector = '', vms = '[\"default/web\"]' WHERE id = ?", (PID,))
    [row] = db.plan_rows(conn, db.list_plans(conn), NOW)
    assert row["pending_vms"] == [] and row["alerts"] == 0


def test_alerts_read_and_host_filter(conn):
    db.set_selector_watch(conn, PID, [{"uid": "u9", "namespace": "default", "name": "x", "since": at(5)}], [])
    db.set_state(conn, PID, "draft", "protecting")
    db.set_state(conn, PID, "protecting", "error", message="boom")
    out = db.alerts(conn, NOW)
    assert out["count"] == 2 and [a["code"] for a in out["alerts"]] == ["plan-error", "selector-new-vm"]
    assert out["alerts"][0]["since"] is not None              # date de l'événement plan/state
    assert db.alerts(conn, NOW, host="harvlab2")["count"] == 2
    assert db.alerts(conn, NOW, host="other") == {"count": 0, "alerts": []}


def test_stats_series_reads_the_window_and_the_resource(conn):
    for uid, ago in (("u1", 30), ("u2", 50), ("u1", 4000)):
        db.record_stats(conn, PID, {"generation": 1, "resource_uid": uid, "pass_at": at(ago), "newly_uploaded": 1,
                                    "rpo_held_s": ago})
    out = db.stats_series(conn, db.get_plan(conn, PID), "1h", NOW)
    assert out["plan"] == PID and out["range"] == "1h" and out["bucket_s"] == 60 and out["rpo_objective_s"] == 600
    assert sum(p["passes"] for p in out["points"]) == 2
    out = db.stats_series(conn, db.get_plan(conn, PID), "24h", NOW, resource_uid="u1")
    assert sum(p["passes"] for p in out["points"]) == 2 and max(p["rpo_held_s"] for p in out["points"]) == 4000
    with pytest.raises(ValueError):
        db.stats_series(conn, db.get_plan(conn, PID), "2d", NOW)


def test_selector_watch_round_trip_and_delete_with_the_plan(conn):
    assert db.get_selector_watch(conn, PID) == {"plan_id": PID, "pending": [], "left_uids": [], "checked_at": ""}
    w = db.set_selector_watch(conn, PID, [{"uid": "u9", "namespace": "default", "name": "x", "since": "s",
                                           "extra": "dropped"}], ["u2", "u1", "u1"], now=NOW)
    assert w["pending"] == [{"uid": "u9", "namespace": "default", "name": "x", "since": "s"}]
    assert w["left_uids"] == ["u1", "u2"] and w["checked_at"] == rp.iso(NOW)
    db.delete_plan(conn, PID)
    assert db.get_selector_watch(conn, PID)["pending"] == []


def test_settings_round_trip(conn):
    assert db.get_setting(conn, "notify") is None and db.get_setting(conn, "notify", {}) == {}
    db.set_setting(conn, "notify", {"enabled": True}, by="alice", now=NOW)
    db.set_setting(conn, "notify_cursor", 7)
    assert db.get_setting(conn, "notify") == {"enabled": True} and db.get_setting(conn, "notify_cursor") == 7
    r = conn.execute("SELECT updated_by FROM settings WHERE key = 'notify'").fetchone()
    assert r["updated_by"] == "alice"


def test_events_after_and_max_event_id(conn):
    top = db.max_event_id(conn)
    a = db.record_event(conn, "follow", code="x", plan_id=PID)
    b = db.record_event(conn, "follow", code="y", plan_id=PID)
    assert db.max_event_id(conn) == b and [e["id"] for e in db.events_after(conn, top)] == [a, b]
    assert [e["id"] for e in db.events_after(conn, a)] == [b]


def test_link_tests_last_per_pair_and_links_view(conn, tmp_path):
    db.record_stats(conn, PID, {"generation": 1, "resource_uid": "u1", "pass_at": at(100), "newly_uploaded": 9,
                                "upload_bps": 5.0, "apply_bps": 6.0})
    db.record_link_test(conn, {"source": "harvlab", "target": "harvlab2", "by": "a", "size_bytes": 1,
                               "error": "old"}, now=NOW - timedelta(hours=1))
    db.record_link_test(conn, {"source": "harvlab", "target": "harvlab2", "by": "b", "size_bytes": 2,
                               "write_bps": 3.0, "read_bps": 4.0, "write_s": 1.0, "read_s": 2.0,
                               "backup_target": "nfs://nas:/b", "action_id": "act1"}, now=NOW)
    db.record_link_test(conn, {"source": "x1", "target": "x2", "error": f"failed in {tmp_path}/kc"}, now=NOW)
    out = db.links(conn, NOW)["links"]
    assert [(x["source"], x["target"]) for x in out] == [("harvlab", "harvlab2"), ("x1", "x2")]
    a, b = out
    assert a["plans"] == [{"id": PID, "name": "erp"}] and a["backup_target"] == "nfs://nas:/b"
    assert a["active"] == {"at": rp.iso(NOW), "by": "b", "action_id": "act1", "size_bytes": 2, "write_bps": 3.0,
                           "read_bps": 4.0, "write_s": 1.0, "read_s": 2.0, "error": ""}
    assert a["passive"]["bytes_24h"] == 9 and a["passive"]["upload_bps"] == 5.0
    assert b["plans"] == [] and b["backup_target"] is None and str(tmp_path) not in b["active"]["error"]
    with pytest.raises(ValueError):
        db.record_link_test(conn, {"source": "x1"})


def test_migration_adds_the_new_tables_to_an_old_database(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    for t in ("plans", "events", "stats"):
        old.execute(db._create_sql(t, db.TABLES[t]))
    old.commit()
    old.close()
    c = db.connect(str(path))
    have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"selector_watch", "settings", "link_tests"} <= have
    assert db.migrate(c) == []                  # idempotente


# ---------------------------------------------------------------------------
# Ligne de commande : `alerts --json`, `plan list --json`
# ---------------------------------------------------------------------------

def _cli():
    import importlib.util
    spec = importlib.util.spec_from_file_location("harvester_replication_i", ROOT / "bin" / "harvester-replication.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_cli_alerts_and_plan_list_use_the_same_functions(tmp_path, capsys):
    import json
    cli = _cli()
    path = tmp_path / "a.db"
    c = db.connect(str(path))
    req = rp.normalize_plan({"name": "erp", "source_cluster": "harvlab", "target_cluster": "harvlab2",
                             "rpo_objective_s": "10m", "selector": "dr=gold", "namespace": "default"})
    db.insert_plan(c, req, plan_id=PID)
    db.set_selector_watch(c, PID, [{"uid": "u9", "namespace": "default", "name": "new-vm", "since": "s"}], [])
    c.close()
    assert cli.main(["alerts", f"--db={path}", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] and data["count"] == 1 and data["alerts"][0]["code"] == "selector-new-vm"
    assert cli.main(["alerts", f"--db={path}", "--host=other", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0
    assert cli.main(["alerts", f"--db={path}", "--host=a;b", "--json"]) == 2
    capsys.readouterr()
    assert cli.main(["plan", "list", f"--db={path}", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)["plans"]
    assert row["pending_vms"] == ["default/new-vm"] and row["alerts"] == 1 and row["transfer"]["passes_24h"] == 0
    assert cli.main(["alerts", f"--db={path}"]) == 0
    assert "selector-new-vm erp default/new-vm" in capsys.readouterr().out
