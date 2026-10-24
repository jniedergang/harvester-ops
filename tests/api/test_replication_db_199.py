"""v1.99.0 : le registre de la réplication (bin/lib/replication_db.py).

Transitions d'un plan valides et refusées (et depuis `error`, celles de
l'état d'origine seulement), unicité du nom, liste par hôte des deux côtés,
journal d'une ressource paginé, bail pris / refusé / repris, verrou repris
quand le processus détenteur n'existe plus, migration additive, rétention."""

import sqlite3
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import replication as rp  # noqa: E402
import replication_db as db  # noqa: E402

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)
PID = "rp-0123456789ab"


def at(seconds):
    return NOW + timedelta(seconds=seconds)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "replication.db")
    yield c
    c.close()


def plan_req(name="web", src="harvlab", dst="harvlab2", **kw):
    req = {"name": name, "source_cluster": src, "target_cluster": dst, "rpo_objective_s": "15m",
           "vms": ["default/rep-a", "default/rep-b"]}
    req.update(kw)
    return rp.normalize_plan(req)


def new_plan(conn, name="web", src="harvlab", dst="harvlab2", pid=PID):
    return db.insert_plan(conn, plan_req(name, src, dst), created_by="alice", now=NOW, plan_id=pid)


def resource(uid, src="harvlab", dst="harvlab2", ns="default", name=None, **kw):
    return dict({"resource_uid": uid, "source_host": src, "target_host": dst, "namespace": ns,
                 "name": name or uid, "generation": 1}, **kw)


# -- connexion, plan -------------------------------------------------------------

def test_connect_creates_a_private_wal_database(tmp_path, monkeypatch):
    path = tmp_path / "state" / "replication.db"
    monkeypatch.setenv("HARVESTER_OPS_REPLICATION_DB", str(path))
    assert db.default_path() == path
    c = db.connect()
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        assert p.exists() and stat.S_IMODE(p.stat().st_mode) == 0o600, p
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(db.TABLES) <= tables


def test_insert_plan_keeps_the_normalized_request(conn):
    p = new_plan(conn)
    assert (p["id"], p["name"], p["state"], p["generation"], p["active_side"]) == (PID, "web", "draft", 1, "source")
    assert (p["source_host_kind"], p["target_host_kind"], p["rpo_objective_s"], p["retain"]) == \
        ("harvester", "harvester", 900, 6)
    assert p["vms"] == ["default/rep-a", "default/rep-b"] and p["options"]["cloud_init"] == "keep"
    assert p["mappings"]["networks"] == {} and p["created_by"] == "alice"
    assert p["created_at"] == p["updated_at"] == "2026-10-24T10:00:00Z"
    assert db.get_plan_by_name(conn, "web")["id"] == PID and db.get_plan(conn, "rp-ffffffffffff") is None
    assert rp.PLAN_ID_RE.match(db.new_plan_id())


def test_plan_name_is_unique(conn):
    new_plan(conn)
    with pytest.raises(db.NameTaken):
        new_plan(conn, pid="rp-0000000000ff")
    with pytest.raises(ValueError, match="invalid plan id"):
        db.insert_plan(conn, plan_req("other"), plan_id="rp-XYZ")
    assert [p["name"] for p in db.list_plans(conn)] == ["web"]


def test_update_plan_settings_and_refuses_the_state(conn):
    new_plan(conn)
    db.update_plan(conn, PID, options={"mac": "new"})
    p = db.update_plan(conn, PID, now=at(60), rpo_objective_s=300, options={"paused": True})
    # options fusionnées et normalisées : `mac` gardé, défauts présents
    assert p["rpo_objective_s"] == 300 and p["updated_at"] == "2026-10-24T10:01:00Z"
    assert p["options"] == dict(rp.DEFAULT_OPTIONS, paused=True, mac="new", timeouts={})
    with pytest.raises(ValueError, match="not settable: state"):
        db.update_plan(conn, PID, state="protected")
    with pytest.raises(ValueError, match="not settable: cron"):
        db.update_plan(conn, PID, cron="*/7 * * * *")
    with pytest.raises(KeyError):
        db.update_plan(conn, "rp-ffffffffffff", retain=4)


@pytest.mark.parametrize("fields,check", [
    ({"interval_s": "15m"}, lambda p: (p["interval_s"], p["cron"]) == (900, "*/15 * * * *")),
    ({"interval_s": None}, lambda p: (p["interval_s"], p["cron"]) == (None, "")),
    ({"selector": "app=web", "selector_namespace": "prod"},
     lambda p: (p["vms"], p["selector"], p["selector_namespace"]) == ([], "app=web", "prod")),
    ({"generation": 2, "active_side": "target"}, lambda p: (p["generation"], p["active_side"]) == (2, "target")),
])
def test_update_plan_goes_through_normalize_plan(conn, fields, check):
    new_plan(conn)
    assert check(db.update_plan(conn, PID, **fields))


@pytest.mark.parametrize("fields,err", [
    ({"interval_s": 420}, rp.PlanError),                  # pas qui ne divise pas l'heure
    ({"rpo_objective_s": 10}, rp.PlanError),
    ({"retain": 1}, rp.PlanError),
    ({"options": {"mac": "random"}}, rp.PlanError),
    ({"mappings": {"bogus": {}}}, rp.PlanError),
    ({"vms": [], "selector": ""}, rp.PlanError),          # plus aucune sélection
    ({"generation": 0}, ValueError),
    ({"active_side": "both"}, ValueError),
])
def test_update_plan_refuses_what_normalize_plan_refuses(conn, fields, err):
    new_plan(conn)
    before = db.get_plan(conn, PID)
    with pytest.raises(err):
        db.update_plan(conn, PID, **fields)
    assert db.get_plan(conn, PID) == before


# -- transitions ----------------------------------------------------------------

@pytest.mark.parametrize("path", [
    ["measuring", "draft"],                                          # mesure arrêtée
    ["measuring", "protecting", "protected"],
    ["protecting", "protecting", "protected"],                       # reprise de protect
    ["protecting", "protected", "protecting", "protected"],          # protect relancé après une dérive
    ["protecting", "protected", "failing_over", "protected"],        # bascule annulée avant activation
    ["protecting", "protected", "failing_over", "failing_over", "failed_over", "unprotecting", "unprotected"],
    ["measuring", "unprotecting", "unprotected"],
    ["protecting", "error", "protecting", "protected"],              # reprise depuis error
    ["protecting", "protected", "failing_over", "error", "failing_over", "failed_over"],
    ["protecting", "protected", "failing_over", "error", "unprotecting", "unprotected"],   # bascule abandonnée
])
def test_valid_transitions(conn, path):
    new_plan(conn)
    cur = "draft"
    for i, nxt in enumerate(path):
        p = db.set_state(conn, PID, cur, nxt, now=at(i), by="alice", action_id="act-1")
        assert p["state"] == nxt
        cur = nxt
    evs = db.plan_events(conn, PID, limit=100)
    assert [e["code"] for e in reversed(evs)] == path
    assert evs[0]["facts"] == {"from": path[-2] if len(path) > 1 else "draft", "to": path[-1]}
    assert evs[0]["by"] == "alice" and evs[0]["action_id"] == "act-1"


@pytest.mark.parametrize("path,expected,new,reason", [
    ([], "draft", "protected", "transition not allowed"),             # pas de protection sans copie
    ([], "draft", "failing_over", "transition not allowed"),
    ([], "draft", "draft", "transition not allowed"),                  # draft n'est pas intermédiaire
    ([], "draft", "unprotecting", "transition not allowed"),           # un brouillon se supprime
    ([], "protected", "failing_over", "state is draft"),               # état réel inattendu
    (["protecting", "protected"], "protected", "failed_over", "transition not allowed"),
    (["protecting", "protected"], "protected", "measuring", "transition not allowed"),
    (["protecting", "protected", "failing_over", "failed_over"], "failed_over", "protected", "transition not allowed"),
    (["protecting", "unprotecting", "unprotected"], "unprotected", "protecting", "transition not allowed"),
    (["protecting", "unprotecting", "unprotected"], "unprotected", "unprotecting", "transition not allowed"),
    # depuis error : seules les transitions de l'état d'origine
    (["protecting", "error"], "error", "failing_over", "transition not allowed"),
    (["protecting", "error"], "error", "measuring", "transition not allowed"),
    (["protecting", "protected", "failing_over", "failed_over", "error"], "error", "failing_over",
     "transition not allowed"),
    (["protecting", "protected", "failing_over", "error"], "error", "protecting", "transition not allowed"),
])
def test_refused_transitions(conn, path, expected, new, reason):
    new_plan(conn)
    cur = "draft"
    for i, nxt in enumerate(path):
        db.set_state(conn, PID, cur, nxt, now=at(i))
        cur = nxt
    with pytest.raises(db.StateConflict, match=reason):
        db.set_state(conn, PID, expected, new)
    assert db.get_plan(conn, PID)["state"] == cur


def test_set_state_accepts_several_expected_states_and_rejects_unknown(conn):
    new_plan(conn)
    assert db.set_state(conn, PID, rp.COMMAND_FROM["protect"], "protecting")["state"] == "protecting"
    with pytest.raises(ValueError, match="unknown state"):
        db.set_state(conn, PID, "protecting", "bogus")
    with pytest.raises(KeyError):
        db.set_state(conn, "rp-ffffffffffff", "draft", "measuring")
    db.set_state(conn, PID, "protecting", "error", now=at(5))
    assert db.error_origin(conn, PID) == "protecting"


def test_error_origin_lives_in_the_plan_row_not_in_the_journal(conn):
    new_plan(conn)
    db.set_state(conn, PID, "draft", "protecting", now=at(0))
    db.set_state(conn, PID, "protecting", "protected", now=at(1))
    db.set_state(conn, PID, "protected", "failing_over", now=at(2))
    p = db.set_state(conn, PID, "failing_over", "error", now=at(3))
    assert p["error_origin"] == "failing_over"
    # journal effacé (rétention) : l'origine reste, la sortie reste restreinte
    assert db.prune(conn, at(3600))["events"] == 4
    assert db.error_origin(conn, PID) == "failing_over"
    with pytest.raises(db.StateConflict):
        db.set_state(conn, PID, "error", "protecting")
    p = db.set_state(conn, PID, "error", "failing_over", now=at(5))
    assert (p["state"], p["error_origin"], db.error_origin(conn, PID)) == ("failing_over", "", None)


def test_error_with_an_unknown_origin_only_unprotects(conn):
    # ligne d'avant la colonne : origine vide, seule la déprotection sort
    new_plan(conn)
    db.set_state(conn, PID, "draft", "protecting")
    db.set_state(conn, PID, "protecting", "error")
    conn.execute("UPDATE plans SET error_origin = '' WHERE id = ?", (PID,))
    with pytest.raises(db.StateConflict):
        db.set_state(conn, PID, "error", "protecting")
    assert db.set_state(conn, PID, "error", "unprotecting")["state"] == "unprotecting"


def test_delete_plan_only_from_draft_or_unprotected_and_keeps_the_audit(conn):
    new_plan(conn)
    db.set_state(conn, PID, "draft", "protecting")
    with pytest.raises(db.StateConflict):
        db.delete_plan(conn, PID)
    db.upsert_resource(conn, PID, resource("u1"), now=NOW)
    db.upsert_volume(conn, PID, {"vm_uid": "u1", "disk": "d", "generation": 1})
    db.record_tick(conn, PID, {"source_volume": "pvc-1", "backup": "b1", "generation": 1})
    db.record_run(conn, PID, "unprotect", NOW, 1, finished_at=at(5), result="ok")
    db.set_state(conn, PID, "protecting", "unprotecting")
    db.set_state(conn, PID, "unprotecting", "unprotected")
    assert db.delete_plan(conn, PID) is True and db.get_plan(conn, PID) is None
    assert db.list_resources(conn, PID) == [] and db.list_volumes(conn, PID) == [] and db.list_ticks(conn, PID) == []
    # bascules/déprotections et journal gardés ensemble, pour l'audit
    assert [r["operation"] for r in db.list_runs(conn, PID)] == ["unprotect"]
    assert [e["code"] for e in db.plan_events(conn, PID)][:1] == ["unprotected"]
    assert db.delete_plan(conn, PID) is False


# -- ressources, volumes, liste par hôte ------------------------------------------

def test_resource_upsert_keeps_its_state_and_state_since(conn):
    new_plan(conn)
    r = db.upsert_resource(conn, PID, resource("u1", name="rep-a", vm_generation=3), now=NOW)
    assert (r["state"], r["state_since"], r["vm_generation"]) == ("draft", "2026-10-24T10:00:00Z", 3)
    db.set_resource_state(conn, PID, "u1", "protecting", now=at(10))
    r = db.upsert_resource(conn, PID, resource("u1", name="rep-a", vm_generation=4, vm_spec_digest="d1"), now=at(20))
    assert (r["state"], r["state_since"], r["vm_generation"], r["vm_spec_digest"]) == \
        ("protecting", "2026-10-24T10:00:10Z", 4, "d1")
    # même état : state_since ne bouge pas, rien au journal
    n = len(db.resource_events(conn, "u1"))
    assert db.set_resource_state(conn, PID, "u1", "protecting", now=at(30))["state_since"] == "2026-10-24T10:00:10Z"
    assert len(db.resource_events(conn, "u1")) == n


@pytest.mark.parametrize("path,refused", [
    (["protecting", "protected", "failing_over", "failed_over"], "protected"),
    (["protecting", "error"], "failing_over"),
    ([], "protected"),
    (["protecting", "protected", "failing_over", "error"], "protecting"),
])
def test_resource_transitions_are_checked_and_journaled(conn, path, refused):
    new_plan(conn)
    db.upsert_resource(conn, PID, resource("u1"), now=NOW)
    cur = "draft"
    for i, nxt in enumerate(path):
        r = db.set_resource_state(conn, PID, "u1", nxt, now=at(i), by="alice", action_id="a1")
        assert r["error_origin"] == (cur if nxt == "error" else "")
        cur = nxt
    evs = db.resource_events(conn, "u1", limit=50)
    assert [e["code"] for e in reversed(evs)] == path
    assert all(e["operation"] == "plan" and e["step"] == "state" and e["by"] == "alice" for e in evs)
    with pytest.raises(db.StateConflict, match="resource transition not allowed"):
        db.set_resource_state(conn, PID, "u1", refused)
    assert db.get_resource(conn, PID, "u1")["state"] == cur


def test_resource_state_expected_and_error_exit(conn):
    new_plan(conn)
    db.upsert_resource(conn, PID, resource("u1"), now=NOW)
    with pytest.raises(db.StateConflict, match="expected protecting"):
        db.set_resource_state(conn, PID, "u1", "protected", expected="protecting")
    for s in ("protecting", "protected", "failing_over", "error"):
        db.set_resource_state(conn, PID, "u1", s)
    r = db.set_resource_state(conn, PID, "u1", "unprotecting", expected=("error",))
    assert (r["state"], r["error_origin"]) == ("unprotecting", "")


@pytest.mark.parametrize("state", ["lagging", "drifting", "degraded", "source-unreachable", "bogus"])
def test_computed_resource_states_are_never_stored(conn, state):
    new_plan(conn)
    db.upsert_resource(conn, PID, resource("u1"), now=NOW)
    with pytest.raises(ValueError):
        db.set_resource_state(conn, PID, "u1", state)


def test_resource_refusals(conn):
    new_plan(conn)
    with pytest.raises(ValueError, match="resource kind"):
        db.upsert_resource(conn, PID, resource("u1", resource_kind="kvm-domain"))
    with pytest.raises(KeyError):
        db.set_resource_state(conn, PID, "nobody", "protecting")


def test_list_by_host_from_source_target_and_both_sides(conn):
    # harvlab est source du plan web et cible du plan back
    new_plan(conn, "web", "harvlab", "harvlab2", pid="rp-00000000000a")
    new_plan(conn, "back", "harvlab2", "harvlab", pid="rp-00000000000b")
    new_plan(conn, "other", "harv1", "harv4", pid="rp-00000000000c")
    db.upsert_resource(conn, "rp-00000000000a", resource("a1", "harvlab", "harvlab2", name="rep-a"))
    db.upsert_resource(conn, "rp-00000000000a", resource("a2", "harvlab", "harvlab2", name="rep-b"))
    db.upsert_resource(conn, "rp-00000000000b", resource("b1", "harvlab2", "harvlab", name="db"))
    db.upsert_resource(conn, "rp-00000000000c", resource("c1", "harv1", "harv4", name="x"))

    def rows(host, side):
        return [(r["resource_uid"], r["side"], r["peer_host"], r["plan_name"])
                for r in db.list_resources_by_host(conn, host, side)]

    assert rows("harvlab", "source") == [("a1", "source", "harvlab2", "web"), ("a2", "source", "harvlab2", "web")]
    assert rows("harvlab", "target") == [("b1", "target", "harvlab2", "back")]
    assert rows("harvlab", "any") == rows("harvlab", "source") + rows("harvlab", "target")
    assert rows("harvlab2", "any") == [("b1", "source", "harvlab", "back"),
                                       ("a1", "target", "harvlab", "web"), ("a2", "target", "harvlab", "web")]
    assert rows("nowhere", "any") == []
    assert {p["name"] for p in db.list_plans(conn, host="harvlab")} == {"web", "back"}
    with pytest.raises(ValueError):
        db.list_resources_by_host(conn, "harvlab", "both")


def test_list_by_host_reads_only_the_current_generation(conn):
    new_plan(conn)
    db.upsert_resource(conn, PID, resource("u1", generation=1))
    db.upsert_resource(conn, PID, resource("u1", "harvlab2", "harvlab", generation=2))
    db.update_plan(conn, PID, generation=2)
    assert [(r["generation"], r["side"]) for r in db.list_resources_by_host(conn, "harvlab")] == [(2, "target")]


def test_volume_upsert_and_list(conn):
    new_plan(conn)
    v = {"vm_uid": "u1", "disk": "data", "claim": "rep-a-data", "source_volume": "pvc-1", "size": 2 * 1024 ** 3,
         "access_mode": "rwx", "migratable": True, "target_volume": "hr-rep-a-data-abcdef", "generation": 1}
    assert db.upsert_volume(conn, PID, v)["state"] == "pending"
    got = db.upsert_volume(conn, PID, dict(v, state="standby", backup_volume_target="bv-1"))
    assert (got["state"], got["migratable"], got["backup_volume_target"]) == ("standby", True, "bv-1")
    assert len(db.list_volumes(conn, PID)) == 1
    with pytest.raises(ValueError, match="volume state"):
        db.upsert_volume(conn, PID, dict(v, state="gone"))
    with pytest.raises(ValueError, match="disk is required"):
        db.upsert_volume(conn, PID, {"vm_uid": "u1", "generation": 1})


def test_volume_upsert_without_state_keeps_the_stored_state(conn):
    new_plan(conn)
    v = {"vm_uid": "u1", "disk": "data", "source_volume": "pvc-1", "generation": 1}
    db.upsert_volume(conn, PID, dict(v, state="standby"))
    got = db.upsert_volume(conn, PID, dict(v, backup_volume_target="bv-1"))      # pas de `state`
    assert (got["state"], got["backup_volume_target"]) == ("standby", "bv-1")
    assert db.upsert_volume(conn, PID, dict(v, disk="new"))["state"] == "pending"


@pytest.mark.parametrize("call", [
    lambda c: db.upsert_resource(c, PID, dict(resource("u1"), generation=None)),
    lambda c: db.upsert_volume(c, PID, {"vm_uid": "u1", "disk": "d"}),
    lambda c: db.record_tick(c, PID, {"source_volume": "pvc-1", "backup": "b1"}),
    lambda c: db.record_measure(c, PID, {"source_volume": "pvc-1", "backup": "b1", "generation": True}),
    lambda c: db.mark_applied(c, PID, "pvc-1", "b1", NOW, None),
    lambda c: db.record_run(c, PID, "failover", NOW, 0),
])
def test_generation_is_required_where_it_is_part_of_the_key(conn, call):
    new_plan(conn)
    with pytest.raises(ValueError, match="generation is required"):
        call(conn)


# -- passages ---------------------------------------------------------------------

def test_record_tick_is_idempotent_and_never_unapplies(conn):
    new_plan(conn)
    tick = {"source_volume": "pvc-1", "backup": "backup-1", "vm_uid": "u1", "generation": 1,
            "snapshot_at": "2026-10-24T10:00:00Z",
            "backup_started_at": "2026-10-24T10:00:01Z", "backup_done_at": "2026-10-24T10:00:06Z",
            "newly_uploaded": 25174351, "size": 2 * 1024 ** 3}
    db.record_tick(conn, PID, tick)
    db.record_tick(conn, PID, tick)
    assert len(db.list_ticks(conn, PID)) == 1
    assert db.mark_applied(conn, PID, "pvc-1", "backup-1", at(15), 1, resync_at=at(6)) is True
    db.record_tick(conn, PID, dict(tick, state="completed"))         # relu par un tour suivant
    t = db.list_ticks(conn, PID)[0]
    assert (t["state"], t["applied_at"], t["resync_at"], t["newly_uploaded"]) == \
        ("applied", "2026-10-24T10:00:15Z", "2026-10-24T10:00:06Z", 25174351)
    assert db.mark_applied(conn, PID, "pvc-1", "backup-404", at(15), 1) is False
    with pytest.raises(ValueError, match="tick state"):
        db.record_tick(conn, PID, dict(tick, state="done"))
    with pytest.raises(ValueError, match="backup is required"):
        db.record_tick(conn, PID, {"source_volume": "pvc-1", "generation": 1})


def test_mark_applied_refuses_a_tick_in_error(conn):
    new_plan(conn)
    db.record_tick(conn, PID, {"source_volume": "pvc-1", "backup": "b1", "generation": 1, "state": "error"})
    with pytest.raises(ValueError, match="in error"):
        db.mark_applied(conn, PID, "pvc-1", "b1", at(15), 1)
    assert db.list_ticks(conn, PID)[0]["state"] == "error"


def test_measures_live_in_their_own_table(conn):
    new_plan(conn)
    db.record_measure(conn, PID, {"source_volume": "pvc-1", "backup": "b1", "snapshot_at": "2026-10-24T10:00:00Z",
                                  "newly_uploaded": 952_000_000, "generation": 1})
    assert db.list_ticks(conn, PID) == []
    assert [m["newly_uploaded"] for m in db.list_ticks(conn, PID, table="measures")] == [952_000_000]


# -- journal ----------------------------------------------------------------------

def test_resource_events_are_paginated_by_date_and_by_id(conn):
    new_plan(conn)
    for i in range(7):
        db.record_event(conn, "protect", step=f"s{i}", plan_id=PID, resource_uid="u1", host="harvlab",
                        side="source", message=f"m{i}", facts={"i": i}, now=at(i))
    db.record_event(conn, "follow", plan_id=PID, resource_uid="u2", host="harvlab2", side="target", now=at(3))
    page1 = db.resource_events(conn, "u1", limit=3)
    assert [e["step"] for e in page1] == ["s6", "s5", "s4"] and page1[0]["facts"] == {"i": 6}
    page2 = db.resource_events(conn, "u1", limit=3, before=page1[-1]["ts"])
    assert [e["step"] for e in page2] == ["s3", "s2", "s1"]
    page3 = db.resource_events(conn, "u1", limit=3, before=page2[-1]["id"])
    assert [e["step"] for e in page3] == ["s0"]
    # une chaîne de chiffres est un id, jamais une date
    assert db.resource_events(conn, "u1", limit=3, before=str(page2[-1]["id"])) == page3
    assert [e["step"] for e in db.resource_events(conn, "u1", limit=2, before=at(2))] == ["s1", "s0"]
    assert [e["resource_uid"] for e in db.host_events(conn, "harvlab2")] == ["u2"]


def test_events_sharing_a_timestamp_are_paged_exactly_by_id(conn):
    new_plan(conn)
    for i in range(4):
        db.record_event(conn, "failover", step=f"s{i}", plan_id=PID, resource_uid="u1", now=NOW)
    p1 = db.resource_events(conn, "u1", limit=2)
    p2 = db.resource_events(conn, "u1", limit=2, before=p1[-1]["id"])
    assert [e["step"] for e in p1 + p2] == ["s3", "s2", "s1", "s0"]


@pytest.mark.parametrize("before", ["yesterday", True, 1.5, "12a"])
def test_event_paging_refuses_an_unreadable_cursor(conn, before):
    with pytest.raises(ValueError, match="event id or an ISO date"):
        db.resource_events(conn, "u1", before=before)


@pytest.mark.parametrize("kw,msg", [
    ({"operation": "drill"}, "unknown operation"),
    ({"operation": "protect", "level": "fatal"}, "unknown level"),
    ({"operation": "protect", "side": "both"}, "side"),
])
def test_event_refusals(conn, kw, msg):
    with pytest.raises(ValueError, match=msg):
        db.record_event(conn, **kw)


def test_runs_record_points_and_step_durations(conn):
    new_plan(conn)
    rid = db.record_run(conn, PID, "failover", NOW, 1, vms=["u1"])
    db.record_run(conn, PID, "failover", NOW, 1, finished_at=at(67), result="ok", vms=["u1"],
                  points={"pvc-1": "backup-9"}, steps=[{"step": "activate", "seconds": 13}], run_id=rid)
    r = db.list_runs(conn, PID)
    assert len(r) == 1 and r[0]["points"] == {"pvc-1": "backup-9"} and r[0]["steps"][0]["seconds"] == 13
    with pytest.raises(ValueError):
        db.record_run(conn, PID, "drill", NOW, 1)


# -- bail et verrou ---------------------------------------------------------------

def test_lease_taken_refused_expired_and_taken_over(conn):
    console, cli = db.owner_id("console", "node1", 10), db.owner_id("cli", "node1", 20)
    assert console == "console:node1:10"
    assert db.take_lease(conn, PID, console, ttl=90, now=NOW) is True
    assert db.take_lease(conn, PID, cli, ttl=90, now=at(60)) is False             # bail encore valide
    assert db.heartbeat(conn, PID, console, now=at(80)) is True
    assert db.take_lease(conn, PID, cli, ttl=90, now=at(160)) is False            # renouvelé à +80
    assert db.take_lease(conn, PID, cli, ttl=90, now=at(171)) is True             # expiré
    assert db.heartbeat(conn, PID, console, now=at(172)) is False                 # perdu
    assert db.take_lease(conn, PID, console, ttl=90, take_over=True, now=at(173)) is True
    assert db.get_lease(conn, PID)["owner"] == console
    assert db.take_lease(conn, PID, console, ttl=90, now=at(174)) is True         # renouvellement
    assert db.release_lease(conn, PID, cli) is False and db.release_lease(conn, PID, console) is True
    with pytest.raises(ValueError):
        db.owner_id("daemon")


def no_start(pid):
    return ""


def test_lock_one_command_at_a_time_and_taken_back_from_a_dead_process(conn):
    alive = {111: True, 222: False}
    holder = db.owner_id("cli", "node1", 111)
    assert db.acquire_lock(conn, PID, "protect", holder, now=NOW, host="node1",
                           pid_alive=lambda p: alive.get(p, False), proc_start=no_start)
    with pytest.raises(db.Locked) as e:
        db.acquire_lock(conn, PID, "failover", db.owner_id("cli", "node1", 333), host="node1",
                        pid_alive=lambda p: alive.get(p, False), proc_start=no_start)
    assert e.value.holder["command"] == "protect"
    # le même détenteur relance
    assert db.acquire_lock(conn, PID, "protect", holder, host="node1", pid_alive=lambda p: True, proc_start=no_start)
    # détenteur mort sur cet hôte : repris
    alive[111] = False
    assert db.acquire_lock(conn, PID, "failover", db.owner_id("cli", "node1", 333), now=at(5), host="node1",
                           pid_alive=lambda p: alive.get(p, False), proc_start=no_start)
    assert db.get_lock(conn, PID)["command"] == "failover"
    ev = db.plan_events(conn, PID)[0]
    assert (ev["step"], ev["code"], ev["facts"]["previous_owner"]) == ("lock-reclaimed", "process-gone", holder)
    assert db.release_lock(conn, PID, "cli:node1:111") is False
    assert db.release_lock(conn, PID, "cli:node1:333") is True and db.get_lock(conn, PID) is None


def test_lock_held_by_a_reused_pid_is_taken_back(conn):
    starts = {111: "5000"}
    db.acquire_lock(conn, PID, "protect", "cli:node1:111", now=NOW, host="node1",
                    proc_start=lambda p: starts.get(p, ""))
    assert db.get_lock(conn, PID)["pid_start"] == "5000"
    take = dict(now=at(5), host="node1", pid_alive=lambda p: True, proc_start=lambda p: starts.get(p, ""))
    with pytest.raises(db.Locked):                                  # même processus, toujours vivant
        db.acquire_lock(conn, PID, "failover", "cli:node1:333", **take)
    starts[111] = "9999"                                            # PID 111 repris par un autre processus
    assert db.acquire_lock(conn, PID, "failover", "cli:node1:333", **take)
    assert db.plan_events(conn, PID)[0]["code"] == "pid-reused"


def test_lock_from_another_host_is_taken_back_after_its_ttl(conn):
    db.acquire_lock(conn, PID, "protect", "console:node9:222", now=NOW, host="node9", ttl=600, proc_start=no_start)
    other = dict(host="node1", pid_alive=lambda p: False, proc_start=no_start)
    with pytest.raises(db.Locked):                                  # son PID ne veut rien dire ici
        db.acquire_lock(conn, PID, "failover", "cli:node1:333", now=at(500), **other)
    assert db.lock_heartbeat(conn, PID, "console:node9:222", now=at(550)) is True
    with pytest.raises(db.Locked):                                  # renouvelé à +550
        db.acquire_lock(conn, PID, "failover", "cli:node1:333", now=at(1100), **other)
    assert db.acquire_lock(conn, PID, "failover", "cli:node1:333", now=at(1151), **other)
    assert db.plan_events(conn, PID)[0]["code"] == "ttl-expired"
    assert db.lock_heartbeat(conn, PID, "console:node9:222", now=at(1152)) is False


# -- migration et rétention ----------------------------------------------------

def test_additive_migration_on_a_base_from_the_previous_schema(tmp_path):
    path = tmp_path / "replication.db"
    old = sqlite3.connect(str(path))
    cols = [c for c in db.TABLES["plans"] if c[0] not in ("vms", "selector_namespace")]
    old.execute(db._create_sql("plans", cols))
    old.execute("INSERT INTO plans (id, name, source_cluster, target_cluster, rpo_objective_s, created_at,"
                " updated_at) VALUES (?, 'old', 'a', 'b', 900, 't', 't')", (PID,))
    old.execute("CREATE TABLE ticks (plan_id TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,"
                " source_volume TEXT NOT NULL, backup TEXT NOT NULL, PRIMARY KEY (plan_id, generation,"
                " source_volume, backup))")
    old.commit()
    old.close()
    c = db.connect(path)
    p = db.get_plan(c, PID)
    assert p["name"] == "old" and p["vms"] == [] and p["selector_namespace"] == "" and p["state"] == "draft"
    have = {r[1] for r in c.execute("PRAGMA table_info(ticks)")}
    assert {"applied_at", "skew_s", "vm_uid"} <= have
    assert db.migrate(c) == []                                    # rien de plus à la seconde ouverture


def test_prune_keeps_the_last_applied_point_of_each_volume(conn):
    new_plan(conn)
    old = NOW - timedelta(days=500)
    for i, state in enumerate(["applied", "applied", "completed"]):
        db.record_tick(conn, PID, {"source_volume": "pvc-1", "backup": f"b{i}", "state": state, "generation": 1,
                                   "snapshot_at": db._iso(old + timedelta(minutes=3 * i))})
    db.record_tick(conn, PID, {"source_volume": "pvc-1", "backup": "recent", "state": "completed",
                               "snapshot_at": db._iso(NOW), "generation": 1})
    db.record_measure(conn, PID, {"source_volume": "pvc-1", "backup": "m", "snapshot_at": db._iso(old),
                                  "generation": 1})
    db.record_event(conn, "follow", plan_id=PID, resource_uid="u1", now=old)
    db.record_event(conn, "follow", plan_id=PID, resource_uid="u1", now=NOW)
    assert db.prune(conn, db.retention_cutoff(NOW), now=NOW) == {
        "ticks": 2, "measures": 1, "events": 1, "stats": 0, "tick_hours": 0,
        "stats_downsampled": 0, "ticks_downsampled": 0}
    assert [t["backup"] for t in db.list_ticks(conn, PID)] == ["b1", "recent"]
    assert len(db.resource_events(conn, "u1")) == 1


def _tick(vol, backup, at, state="applied", newly=0, lag=None, vm="u1"):
    t = {"source_volume": vol, "backup": backup, "state": state, "generation": 1, "vm_uid": vm,
         "snapshot_at": db._iso(at), "newly_uploaded": newly}
    if lag is not None:
        t["backup_done_at"] = db._iso(at + timedelta(seconds=20))
        t["applied_at"] = db._iso(at + timedelta(seconds=20 + lag))
    return t


# v1.99.0, décision 55 : passages de plus de 30 jours réduits à l'heure par volume
@pytest.mark.parametrize("ticks,expected_hours,kept", [
    # trois passages dans la même heure, le dernier appliqué reste dans `ticks`
    ([("pvc-1", "a", 0, "applied", 10, 5), ("pvc-1", "b", 3, "skipped", 20, None),
      ("pvc-1", "c", 6, "error", 0, None), ("pvc-1", "d", 9, "applied", 40, 12)],
     [("pvc-1", "10", 3, 1, 1, 1, 30, 5.0)], ["d"]),
    # deux heures, deux volumes ; les passages récents ne bougent pas
    ([("pvc-1", "a", 0, "applied", 10, 5), ("pvc-1", "b", 61, "applied", 20, 9),
      ("pvc-1", "e", 62, "applied", 30, 7),
      ("pvc-2", "c", 2, "applied", 5, 3), ("pvc-2", "d", 4, "applied", 6, 4),
      ("pvc-1", "recent", 60 * 24 * 39, "completed", 1, None)],
     [("pvc-1", "10", 1, 1, 0, 0, 10, 5.0), ("pvc-1", "11", 1, 1, 0, 0, 20, 9.0),
      ("pvc-2", "10", 1, 1, 0, 0, 5, 3.0)], ["d", "e", "recent"]),
    # rien de plus de 30 jours : rien n'est réduit
    ([("pvc-1", "a", 60 * 24 * 35, "applied", 10, 5)], [], ["a"]),
])
def test_prune_downsamples_old_ticks_hourly_per_volume(conn, ticks, expected_hours, kept):
    new_plan(conn)
    base = NOW - timedelta(days=40)
    base = base.replace(hour=10, minute=0)
    for vol, backup, minutes, state, newly, lag in ticks:
        db.record_tick(conn, PID, _tick(vol, backup, base + timedelta(minutes=minutes), state, newly, lag))
    out = db.prune(conn, db.retention_cutoff(NOW), now=NOW)
    hours = [(h["source_volume"], h["hour"][11:13], h["count"], h["applied"], h["skipped"], h["errors"],
              h["newly_uploaded"], h["max_apply_lag_s"]) for h in db.list_tick_hours(conn, PID)]
    assert sorted(hours) == sorted(expected_hours)
    assert out["ticks_downsampled"] == sum(h[2] for h in expected_hours)
    assert sorted(t["backup"] for t in db.list_ticks(conn, PID)) == sorted(kept)
    # idempotent : une relance ne change rien
    again = db.prune(conn, db.retention_cutoff(NOW), now=NOW)
    assert again["ticks_downsampled"] == 0
    assert sorted((h["source_volume"], h["hour"][11:13], h["count"], h["applied"], h["skipped"], h["errors"],
                   h["newly_uploaded"], h["max_apply_lag_s"]) for h in db.list_tick_hours(conn, PID)) \
        == sorted(expected_hours)


def test_ticks_joining_an_hour_already_downsampled_add_up(conn):
    # le dernier appliqué gardé rejoint son heure dès qu'un plus récent l'a remplacé
    new_plan(conn)
    base = (NOW - timedelta(days=40)).replace(hour=10, minute=0)
    db.record_tick(conn, PID, _tick("pvc-1", "a", base, "applied", 10, 5))
    db.record_tick(conn, PID, _tick("pvc-1", "b", base + timedelta(minutes=3), "applied", 20, 8))
    db.prune(conn, db.retention_cutoff(NOW), now=NOW)
    assert [t["backup"] for t in db.list_ticks(conn, PID)] == ["b"]
    db.record_tick(conn, PID, _tick("pvc-1", "c", NOW, "applied", 1, 1))
    db.prune(conn, db.retention_cutoff(NOW), now=NOW)
    (h,) = db.list_tick_hours(conn, PID)
    assert (h["count"], h["applied"], h["newly_uploaded"], h["max_apply_lag_s"]) == (2, 2, 30, 8.0)
    assert [t["backup"] for t in db.list_ticks(conn, PID)] == ["c"]
    # passé la rétention, l'heure part aussi
    out = db.prune(conn, NOW + timedelta(days=1), now=NOW + timedelta(days=500))
    assert out["tick_hours"] == 1 and db.list_tick_hours(conn, PID) == []


# -- actions proposées en `error` = sorties acceptées par le registre ----------

PATH_TO = {"draft": [], "measuring": ["measuring"], "protecting": ["protecting"],
           "protected": ["protecting", "protected"],
           "failing_over": ["protecting", "protected", "failing_over"],
           "failed_over": ["protecting", "protected", "failing_over", "failed_over"],
           "unprotecting": ["protecting", "unprotecting"], None: ["protecting"]}


def plan_in_error(conn, origin, pid):
    db.insert_plan(conn, plan_req(name="p" + pid[-6:]), now=NOW, plan_id=pid)
    cur = "draft"
    for nxt in PATH_TO[origin]:
        db.set_state(conn, pid, cur, nxt)
        cur = nxt
    db.set_state(conn, pid, cur, "error")
    if origin is None:                                     # ligne d'avant la colonne
        conn.execute("UPDATE plans SET error_origin = '' WHERE id = ?", (pid,))
    return db.get_plan(conn, pid)


@pytest.mark.parametrize("origin", list(PATH_TO))
@pytest.mark.parametrize("side", ["target", "source", "any"])
def test_error_actions_offered_are_exactly_the_exits_the_store_accepts(conn, origin, side):
    seq = iter(range(10 ** 6))

    def fresh():
        return plan_in_error(conn, origin, f"rp-{next(seq):012x}")

    plan = fresh()
    offered = [a["name"] for a in rp.allowed_actions(plan, {"state": "error", "error_origin": plan["error_origin"]},
                                                     side, role="admin")]
    # chaque action proposée passe le registre
    for a in offered:
        p = fresh()
        assert db.set_state(conn, p["id"], "error", rp.ACTION_STATE[a])["state"] == rp.ACTION_STATE[a]
    # chaque sortie acceptée par le registre est proposée (sauf la bascule vue
    # de la source, réservée à la cible)
    accepted = set()
    for new in rp.PLAN_STATES:
        p = fresh()
        try:
            db.set_state(conn, p["id"], "error", new)
            accepted.add(new)
        except db.StateConflict:
            pass
    reached = {rp.ACTION_STATE[a] for a in offered}
    hidden = {"failing_over"} if side == "source" else set()
    assert reached == accepted - hidden
    assert accepted                                         # jamais d'impasse : une sortie au moins


# -- vue d'un plan (liste des plans, liste par hôte, état) ----------------------

def test_plan_view_reads_recent_ticks_the_last_point_and_three_events_each(conn):
    new_plan(conn)
    plan = db.update_plan(conn, PID, interval_s=180)
    for uid in ("u1", "u2"):
        db.upsert_resource(conn, PID, resource(uid), now=NOW)
    db.set_health(conn, PID, "u1", 1, {"running": False, "stopped_at": "2026-10-24T09:00:00Z"}, now=NOW)
    old = {"source_volume": "pvc-1", "vm_uid": "u1", "generation": 1, "backup": "b-old",
           "snapshot_at": "2026-10-20T10:00:00Z", "backup_done_at": "2026-10-20T10:00:05Z"}
    db.record_tick(conn, PID, old)
    db.mark_applied(conn, PID, "pvc-1", "b-old", at(-4 * 86400), 1)
    db.record_tick(conn, PID, dict(old, backup="b-mid", snapshot_at="2026-10-21T10:00:00Z"))   # ni récent ni appliqué
    db.record_tick(conn, PID, dict(old, backup="b-new", snapshot_at="2026-10-24T09:58:00Z"))
    for i in range(5):
        db.record_event(conn, "plan", step=f"p{i}", plan_id=PID, now=at(i))
        db.record_event(conn, "follow", step=f"u1-{i}", plan_id=PID, resource_uid="u1", now=at(i))
    db.record_event(conn, "follow", step="other-plan", plan_id="rp-ffffffffffff", resource_uid="u2", now=at(9))
    v = db.plan_view(conn, plan, at(10))
    assert sorted(t["backup"] for t in v["ticks"]) == ["b-new", "b-old"]
    assert [e["step"] for e in v["events"]] == ["p4", "p3", "p2", "u1-4", "u1-3", "u1-2"]
    assert [r["resource_uid"] for r in v["resources"]] == ["u1", "u2"]
    assert v["vm_running"] == {"u1": {"running": False, "stopped_at": "2026-10-24T09:00:00Z"}}
    assert set(v["health"]) == {"u1", "u2"}


# -- aucun chemin au registre (revue de la tâche 5) ------------------------------

SECRET = "/srv/sécret/person.kubeconfig"


@pytest.mark.parametrize("text,want", [
    (f'error loading config file "{SECRET}": permission denied',
     'error loading config file "<hidden>": permission denied'),
    ("open /var/lib/harvester-ops/replication.db failed", "open <hidden> failed"),
    ("read /root/.kube/config", "read <hidden>"),
    ("in /tmp/kc-123 and /home/bob/x.yaml", "in <hidden> and <hidden>"),
    ("bare /tmp", "bare <hidden>"),
    # rien d'autre n'est touché
    ("https://rancher.example/tmp/k8s/clusters/c-1", "https://rancher.example/tmp/k8s/clusters/c-1"),
    ("default/tmp-vm and ns/home", "default/tmp-vm and ns/home"),
    ("/var/library and /tmpfs", "/var/library and /tmpfs"),
    ("/srv/julia is another user, /srv/ju/.kube/config is home", "/srv/julia is another user, <hidden> is home"),
    ("ju owns it", "ju owns it"),
    ("nfs://nas:/var/lib/backups?x=1", "nfs://nas:/var/lib/backups?x=1"),        # URL de cible de sauvegarde
])
def test_scrub_text_hides_paths_only_where_they_are_paths(monkeypatch, text, want):
    monkeypatch.setattr(db, "_HIDDEN_PATHS", set())
    monkeypatch.setenv("HOME", "/srv/ju")
    db.hide_paths(SECRET)
    assert db.scrub_text(text) == want


def _lock_reclaim(conn, msg):
    # détenteur d'un autre hôte, silencieux depuis plus que son ttl : la
    # reprise écrit `plan/lock-reclaimed`, qui cite l'ancien détenteur
    db.acquire_lock(conn, PID, "protect", f"cli:{msg}:1", now=NOW, host="here")
    db.acquire_lock(conn, PID, "protect", "cli:here:2", now=at(db.LOCK_TTL_S + 60), host="here",
                    pid_alive=lambda p: True)


@pytest.mark.parametrize("write", [
    lambda c, m: db.record_event(c, "protect", plan_id=PID, message=m, facts={"error": m, m: 1}),
    lambda c, m: db.set_state(c, PID, "draft", "protecting", now=NOW, message=m),
    lambda c, m: (db.upsert_resource(c, PID, resource("u1"), now=NOW),
                  db.set_resource_state(c, PID, "u1", "protecting", generation=1, now=NOW, message=m)),
    lambda c, m: db.record_run(c, PID, "failover", NOW, 1, steps=[{"step": "x", "message": m}]),
    lambda c, m: db.record_run(c, PID, "failover", NOW, 1, run_id=db.record_run(c, PID, "failover", NOW, 1),
                               points={"error": m}),
    lambda c, m: (db.upsert_resource(c, PID, resource("u1"), now=NOW),
                  db.set_health(c, PID, "u1", 1, {"error": m}, now=NOW)),
    lambda c, m: _lock_reclaim(c, m),
], ids=["record_event", "set_state", "set_resource_state", "record_run", "record_run-update", "set_health",
        "lock-reclaimed"])
def test_every_write_path_of_the_registry_hides_paths(conn, monkeypatch, write):
    """Une écriture directe du registre (passage d'état, reprise de verrou)
    ne passe pas par la ligne de commande : le registre masque lui-même."""
    monkeypatch.setattr(db, "_HIDDEN_PATHS", set())
    db.hide_paths(SECRET)
    new_plan(conn)
    write(conn, f'error loading config file "{SECRET}": permission denied')
    dump = repr([dict(r) for t in ("events", "runs", "plans", "plan_resources", "locks")
                 for r in conn.execute(f'SELECT * FROM "{t}"')])
    assert "sécret" not in dump and "s\\u00e9cret" not in dump and "<hidden>" in dump


def test_backup_target_urls_are_stored_exactly(conn):
    new_plan(conn)
    url = "nfs://nas:/var/lib/backups/?backup=b-1&volume=v-1"
    db.upsert_volume(conn, PID, {"generation": 1, "vm_uid": "u1", "disk": "d1", "backup_volume_target": url})
    assert db.list_volumes(conn, PID)[0]["backup_volume_target"] == url



@pytest.mark.parametrize("holder_host, alive, taken", [
    ("here", False, True),      # processus disparu sur cet hôte : repris aussitôt
    ("here", True, False),      # toujours là : on attend son bail
    ("elsewhere", False, False),  # autre hôte : son PID ne veut rien dire ici
])
def test_take_lease_reclaims_the_lease_of_a_dead_process_on_this_host(conn, holder_host, alive, taken):
    dead = db.owner_id("console", host=holder_host, pid=4242)
    assert db.take_lease(conn, PID, dead, ttl=180, now=NOW, host="here") is True
    me = db.owner_id("console", host="here", pid=5151)
    got = db.take_lease(conn, PID, me, ttl=180, now=at(5), host="here", pid_alive=lambda pid: alive)
    assert got is taken
    assert db.get_lease(conn, PID)["owner"] == (me if taken else dead)


class _Interrupt(BaseException):
    """Annulation levée par un gestionnaire de signal (BaseException)."""


class _ConnAfterBegin:
    """Connexion dont l'appel qui suit le BEGIN est interrompu, comme un
    signal traité au retour de BEGIN IMMEDIATE (attente du verrou)."""

    def __init__(self, real, sql, before=False):
        self.real, self.sql, self.armed, self.before = real, sql, True, before

    @property
    def in_transaction(self):
        return self.real.in_transaction

    def execute(self, sql, *a):
        if self.before and self.armed and sql == self.sql:
            # l'appel lui-même échoue (verrou, disque) : rien n'est exécuté
            self.armed = False
            raise sqlite3.OperationalError("database is locked")
        out = self.real.execute(sql, *a)
        if self.armed and sql == self.sql:
            self.armed = False
            raise _Interrupt()
        return out


# v1.99.0 (banc) : `protect --retain` annulé, puis OperationalError (« cannot
# start a transaction within a transaction ») au retour arrière : la ligne du
# plan n'était jamais remise
@pytest.mark.parametrize("left_by", ["enter", "outside"])
def test_tx_after_an_interrupted_begin_still_writes(conn, left_by):
    pid = new_plan(conn)["id"]
    if left_by == "enter":
        with pytest.raises(_Interrupt):
            with db._tx(_ConnAfterBegin(conn, "BEGIN IMMEDIATE")):
                pass
        assert not conn.in_transaction
    else:
        # reste d'une transaction ouverte (signal à l'entrée de __exit__)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE plans SET retain = 40 WHERE id = ?", (pid,))
    db.update_plan(conn, pid, retain=9)
    assert not conn.in_transaction
    assert db.get_plan(conn, pid)["retain"] == 9


@pytest.mark.parametrize("before,exc,retain", [
    (False, _Interrupt, 11),                    # COMMIT fait, signal à son retour
    (True, sqlite3.OperationalError, 6),        # COMMIT refusé : annulé, rien d'ouvert
])
def test_tx_commit_interrupted_leaves_no_open_transaction(conn, before, exc, retain):
    pid = new_plan(conn)["id"]
    db.update_plan(conn, pid, retain=6)
    with pytest.raises(exc):
        with db._tx(_ConnAfterBegin(conn, "COMMIT", before=before)) as c:
            c.execute("UPDATE plans SET retain = 11 WHERE id = ?", (pid,))
    assert not conn.in_transaction and db.get_plan(conn, pid)["retain"] == retain
