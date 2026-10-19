"""v1.95.0 : la ligne de commande des quotas et du décompte
(bin/harvester-bookings.py usage, quota set|list|delete) et le refus d'une
réservation au-delà du quota de son titulaire (même chemin que les
conflits, dans la transaction d'écriture).

Bases temporaires à chaque test ; aucun cluster n'est lu."""

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import bookings_db as bdb  # noqa: E402
import usage_db as udb  # noqa: E402

spec = importlib.util.spec_from_file_location("harvester_bookings_195", ROOT / "bin" / "harvester-bookings.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

NOW = datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)
UNITS = [{"id": "n3/nvidia.com/gpu", "type": "node", "cluster": "gpudemo", "node": "n3", "name": "nvidia.com/gpu",
          "model": "nvidia.com/gpu", "resource": "nvidia.com/gpu", "state": "free", "total": 8, "used": 0, "free": 8}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "now", lambda: NOW)
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps({"units": UNITS}))
    return {"db": str(tmp_path / "bookings.db"), "udb": str(tmp_path / "usage.db"), "inv": str(inv)}


def T(h, day=20):
    return f"2026-10-{day:02d}T{h:02d}:00:00Z"


def book(env, *who, count=1, start=T(9), end=T(13), extra=()):
    return cli.main(["create", "--db", env["db"], "--inventory", env["inv"], "--cluster", "gpudemo", "--type", "node",
                     "--model", "nvidia.com/gpu", "--count", str(count), "--start", start, "--end", end,
                     "--as", "alice", *who, *extra])


def test_quota_set_list_replace_delete(env, capsys):
    assert cli.main(["quota", "list", "--db", env["db"]]) == 0
    assert "(no quota)" in capsys.readouterr().out
    assert cli.main(["quota", "set", "--db", env["db"], "--group", "ml-team", "--monthly-hours", "10",
                     "--max-units", "2", "--as", "boss"]) == 0
    err = capsys.readouterr().err
    assert "STEP_EVENT|quota|done|quota of group ml-team: monthly GPU-hours 10.00, max concurrent units 2" in err
    assert cli.main(["quota", "set", "--db", env["db"], "--group", "ml-team", "--monthly-hours", "20"]) == 0
    capsys.readouterr()
    assert cli.main(["quota", "list", "--db", env["db"], "--json"]) == 0
    [q] = json.loads(capsys.readouterr().out)["quotas"]
    assert (q["holder"], q["monthly_hours"], q["max_units"]) == ({"kind": "group", "name": "ml-team"}, 20.0, None)
    assert cli.main(["quota", "delete", "--db", env["db"], "--group", "ml-team"]) == 0
    assert "removed" in capsys.readouterr().err
    assert cli.main(["quota", "delete", "--db", env["db"], "--group", "ml-team"]) == 1
    assert "STEP_EVENT|quota|error|no quota for group ml-team" in capsys.readouterr().err


@pytest.mark.parametrize("argv,msg", [
    (["quota", "set", "--holder", "a"], "monthly GPU-hours cap"),
    (["quota", "set", "--holder", "a", "--group", "b", "--monthly-hours", "1"], "--holder USER or --group"),
    (["quota", "set", "--holder", "a", "--max-units", "0"], "whole number"),
    (["quota", "delete"], "--holder USER or --group"),
])
def test_quota_refusals(env, capsys, argv, msg):
    assert cli.main(argv + ["--db", env["db"]]) == 2
    assert msg in capsys.readouterr().err


def test_booking_over_the_monthly_quota_is_refused_with_its_reason(env, capsys):
    cli.main(["quota", "set", "--db", env["db"], "--holder", "alice", "--monthly-hours", "6"])
    assert book(env) == 0                                                      # 4 h
    capsys.readouterr()
    assert book(env, start=T(14), end=T(17)) == 1                              # +3 h > 6
    err = capsys.readouterr().err
    assert ("refused: over the quota of user alice: 4.0 GPU-hours already booked in 2026-10, "
            "this booking adds 3.0, the monthly cap is 6.0") in err
    assert "STEP_EVENT|check|error|refused: over the quota" in err
    assert len(bdb.list_bookings(bdb.connect(env["db"]))) == 1
    assert book(env, start=T(14), end=T(16)) == 0                              # +2 h = 6 : passe
    # un autre mois repart de zéro
    assert book(env, start="2026-11-02T09:00:00Z", end="2026-11-02T13:00:00Z") == 0


def test_booking_over_the_concurrent_units_quota_and_check_says_it(env, capsys):
    cli.main(["quota", "set", "--db", env["db"], "--group", "ml-team", "--max-units", "2"])
    assert book(env, "--group", "ml-team", count=2) == 0
    capsys.readouterr()
    base = ["--db", env["db"], "--inventory", env["inv"], "--cluster", "gpudemo", "--type", "node",
            "--model", "nvidia.com/gpu", "--group", "ml-team", "--start", T(12), "--end", T(14)]
    assert cli.main(["check", *base, "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert [c["code"] for c in out["conflicts"]] == ["quota-units"]
    assert book(env, "--group", "ml-team", start=T(12), end=T(14)) == 1
    assert "3 units would be booked at the same time at 2026-10-20T12:00:00Z, the maximum is 2" in capsys.readouterr().err
    # la personne qui crée ne compte pas : le quota est celui du titulaire
    assert book(env, "--holder", "bob", start=T(12), end=T(14)) == 0


def test_usage_report_text_json_csv(env, capsys):
    book(env, "--holder", "alice", count=2, extra=("--target-workload", "ml/trainer"))
    cli.main(["quota", "set", "--db", env["db"], "--holder", "alice", "--monthly-hours", "100"])
    conn = udb.connect(env["udb"])
    s = {"unit": "n3/nvidia.com/gpu", "type": "node", "model": "nvidia.com/gpu", "kind": "pod", "namespace": "ml",
         "name": "trainer-7b-x", "workload": "trainer", "count": 2}
    for m in (5, 10, 15):
        udb.record(conn, f"2026-10-20T09:{m:02d}:00Z", 300, "gpudemo", "ok", [s])
    udb.record(conn, "2026-10-20T09:05:00Z", 300, "gpudemo", "ok", [dict(s, name="web-1", workload="web")])
    capsys.readouterr()
    rng = ["--from", "2026-10-20T00:00:00Z", "--to", "2026-10-21T00:00:00Z"]
    assert cli.main(["usage", "--db", env["db"], "--usage-db", env["udb"], *rng]) == 0
    out = capsys.readouterr().out
    assert "HOLDER" in out and "user:alice" in out and "namespace:ml" in out and "TOTAL" in out
    assert cli.main(["usage", "--db", env["db"], "--usage-db", env["udb"], *rng, "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    got = {r["holder"]["name"]: r for r in rep["holders"]}
    assert (got["alice"]["booked"], got["alice"]["consumed"]) == (8.0, 0.5)
    assert got["alice"]["quota"]["remaining"] == 92.0
    assert got["ml"]["out_of_rule"] == round(2 * 5 / 60, 3)
    assert rep["sampler"]["clusters"][0]["cluster"] == "gpudemo"
    assert cli.main(["usage", "--db", env["db"], "--usage-db", env["udb"], *rng, "--csv"]) == 0
    assert capsys.readouterr().out.splitlines()[1].startswith("user,alice,,,8.0,0.5")


def test_usage_empty_and_bad_period(env, capsys):
    assert cli.main(["usage", "--db", env["db"], "--usage-db", env["udb"]]) == 0
    assert "(no booking and no GPU use in this period)" in capsys.readouterr().out
    assert cli.main(["usage", "--db", env["db"], "--usage-db", env["udb"], "--from", T(10), "--to", T(9)]) == 2
    assert "after its start" in capsys.readouterr().err


def test_the_quota_table_is_an_additive_migration(env):
    """Une base de 1.94 (sans table quotas) s'ouvre, garde ses réservations et
    gagne la table."""
    raw = sqlite3.connect(env["db"])
    raw.executescript(bdb.SCHEMA.split("-- v1.95.0")[0])
    raw.execute("INSERT INTO bookings (id, cluster, type, holder_kind, holder, start, \"end\", created_at)"
                " VALUES ('bk-aaaaaaaa0001', 'c', 'node', 'user', 'a', ?, ?, ?)", (T(9), T(10), T(8)))
    raw.commit()
    raw.close()
    conn = bdb.connect(env["db"])
    assert [b["id"] for b in bdb.list_bookings(conn)] == ["bk-aaaaaaaa0001"]
    assert bdb.list_quotas(conn) == []
