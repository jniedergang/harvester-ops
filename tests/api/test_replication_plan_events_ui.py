"""Écrans de réplication : journal d'un plan (onglet Journal de la vue d'un
contrat). Événements du plan et de ses ressources, du plus récent au plus
ancien, pagination par curseur, filtre des avertissements, rôles, refus."""

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_replication_routes_199 import PID, PID2, as_user, client, db, env, now_utc  # noqa: E402,F401


def _add(env, **kw):
    conn = db.connect(env["db"])
    try:
        return db.record_event(conn, kw.pop("operation", "follow"), **kw)
    finally:
        conn.close()


def test_plan_journal_mixes_plan_and_resource_events_newest_first(client, env):
    t = now_utc() + timedelta(hours=1)
    _add(env, step="plan-level", plan_id=PID, code="protected", now=t)
    _add(env, step="vm-level", plan_id=PID, resource_uid="u-b", level="warn", code="rpo-exceeded",
         now=t - timedelta(seconds=5))
    _add(env, step="other-plan", plan_id=PID2, now=t + timedelta(seconds=5))
    d = client.get(f"/api/replication/plans/{PID}/events?limit=500").get_json()
    steps = [e["step"] for e in d["events"]]
    assert steps[:2] == ["plan-level", "vm-level"]
    assert "other-plan" not in steps
    assert d["plan"] == PID and d["next_before"] is None


def test_plan_journal_pages_without_gap(client, env):
    full = client.get(f"/api/replication/plans/{PID}/events?limit=500").get_json()["events"]
    assert len(full) >= 4
    got, before = [], None
    while True:
        q = f"/api/replication/plans/{PID}/events?limit=1" + (f"&before={before}" if before else "")
        d = client.get(q).get_json()
        got += d["events"]
        before = d["next_before"]
        if before is None:
            break
    assert [e["id"] for e in got] == [e["id"] for e in full]


def test_plan_journal_warn_filter(client, env):
    _add(env, step="bad", plan_id=PID, resource_uid="u-a", level="error", code="backup-error")
    _add(env, step="meh", plan_id=PID, resource_uid="u-a", level="warn", code="rpo-drift")
    evs = client.get(f"/api/replication/plans/{PID}/events?level=warn").get_json()["events"]
    assert {e["step"] for e in evs} == {"bad", "meh"}
    assert all(e["level"] in ("warn", "error") for e in evs)


@pytest.mark.parametrize("query,status", [
    ("?limit=0", 400), ("?limit=501", 400), ("?limit=x", 400), ("?level=info", 400),
    ("?before=yesterday", 400), ("?limit=10", 200),
])
def test_plan_journal_refusals(client, query, status):
    assert client.get(f"/api/replication/plans/{PID}/events{query}").status_code == status


@pytest.mark.parametrize("plan,status", [("rp-000000000000", 404), ("web", 400), ("rp-XYZ", 400)])
def test_plan_journal_unknown_or_invalid_plan(client, plan, status):
    assert client.get(f"/api/replication/plans/{plan}/events").status_code == status


@pytest.mark.parametrize("user", ["curieux", "alice", "patronne"])
def test_plan_journal_readable_by_every_role(as_user, user):
    assert as_user(user, "GET", f"/api/replication/plans/{PID}/events").status_code == 200
