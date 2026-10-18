"""v1.94.0 : le registre des réservations (bookings_db) et la ligne de
commande bin/harvester-bookings.py (list, check, create, cancel).

Base temporaire à chaque test ; l'inventaire GPU vient d'un fichier
(--inventory) ou de la demande (`inventory` de --spec, comme la console le
passe) : aucun cluster n'est lu."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import bookings_db as bdb  # noqa: E402
import hv_bookings as hb  # noqa: E402

spec = importlib.util.spec_from_file_location("harvester_bookings", ROOT / "bin" / "harvester-bookings.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

NOW = datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)


def unit(uid, typ, model, state="free", total=1, used=0, **extra):
    return dict({"id": uid, "type": typ, "cluster": "gpudemo", "node": "n1", "name": uid, "model": model,
                 "state": state, "total": total, "used": used, "free": total - used}, **extra)


UNITS = [unit("v1", "vgpu", "NVIDIA H100-20C", "used", used=1), unit("v2", "vgpu", "NVIDIA H100-20C"),
         unit("n3/nvidia.com/gpu", "node", "nvidia.com/gpu", total=4, resource="nvidia.com/gpu"),
         dict(unit("x1", "vgpu", "NVIDIA H100-20C"), cluster="other")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "now", lambda: NOW)
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps({"units": UNITS}))
    db = tmp_path / "bookings.db"
    return {"db": str(db), "inv": str(inv), "tmp": tmp_path}


def T(h, day=20):
    return f"2026-10-{day:02d}T{h:02d}:00:00Z"


def args(env, *extra, typ="vgpu", model="NVIDIA H100-20C", count=1, start=T(9), end=T(13)):
    out = ["--db", env["db"], "--inventory", env["inv"], "--cluster", "gpudemo", "--type", typ,
           "--start", start, "--end", end]
    if model:
        out += ["--model", model, "--count", str(count)]
    return out + list(extra)


def rows(env):
    conn = bdb.connect(env["db"])
    return bdb.list_bookings(conn)


def test_create_list_cancel_round_trip(env, capsys):
    assert cli.main(["create", *args(env, count=2, model="NVIDIA H100-20C"), "--as", "alice",
                     "--target-vm", "ml/notebook", "--note", "training"]) == 0
    out = capsys.readouterr()
    bid = out.out.strip()
    assert hb.ID_RE.match(bid)
    assert "STEP_EVENT|book|done|booked vgpu 2 x NVIDIA H100-20C on gpudemo" in out.err
    assert "not enforced on the cluster" in out.err
    [b] = rows(env)
    assert b["holder"] == {"kind": "user", "name": "alice"} and b["created_by"] == "alice"
    assert b["target"] == {"kind": "vm", "namespace": "ml", "name": "notebook"} and b["note"] == "training"
    assert cli.main(["list", "--db", env["db"], "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["bookings"]
    assert [(x["id"], x["state"]) for x in listed] == [(bid, "upcoming")]
    assert cli.main(["list", "--db", env["db"]]) == 0
    assert "2 x NVIDIA H100-20C" in capsys.readouterr().out
    assert cli.main(["cancel", "--db", env["db"], "--id", bid, "--as", "alice"]) == 0
    assert rows(env)[0]["cancelled_by"] == "alice"
    # annulée : elle reste dans l'historique, et une seconde annulation est refusée
    assert cli.main(["cancel", "--db", env["db"], "--id", bid, "--as", "alice"]) == 1
    assert "already cancelled" in capsys.readouterr().err


def test_check_says_ok_or_the_conflict_and_writes_nothing(env, capsys):
    assert cli.main(["create", *args(env, count=2), "--as", "alice"]) == 0
    capsys.readouterr()
    assert cli.main(["check", *args(env, start=T(12), end=T(14))]) == 1
    assert "conflict: 3 x vgpu NVIDIA H100-20C would be booked at 2026-10-20T12:00:00Z, the cluster has 2" \
        in capsys.readouterr().out
    assert cli.main(["check", *args(env, start=T(13), end=T(14)), "--json"]) == 0      # bout à bout
    d = json.loads(capsys.readouterr().out)
    assert d["ok"] is True and d["capacity"] == 2
    assert len(rows(env)) == 1


def test_create_refuses_a_conflict_and_a_unit_already_taken(env, capsys):
    assert cli.main(["create", *args(env, "--unit", "v2", model=""), "--as", "alice"]) == 0
    capsys.readouterr()
    assert cli.main(["create", *args(env, "--unit", "v2", model="", start=T(12), end=T(14)), "--as", "bob"]) == 1
    err = capsys.readouterr().err
    assert "STEP_EVENT|check|error|refused: this unit is already booked by user alice" in err
    assert len(rows(env)) == 1
    # l'unité v1 reste libre ; puis la capacité du modèle (2) est atteinte
    assert cli.main(["create", *args(env, "--unit", "v1", model=""), "--as", "bob"]) == 0
    assert cli.main(["create", *args(env, count=1, start=T(10), end=T(11)), "--as", "carol"]) == 1


def test_create_from_the_consoles_spec_with_its_inventory(env, capsys):
    spec_file = env["tmp"] / "spec.json"
    spec_file.write_text(json.dumps({"cluster": "gpudemo", "type": "node", "model": "nvidia.com/gpu", "count": 4,
                                     "holder": {"kind": "group", "name": "ml-team"}, "start": T(9), "end": T(13),
                                     "target": {"kind": "workload", "namespace": "ml", "name": "trainer"},
                                     "inventory": UNITS}))
    assert cli.main(["create", "--db", env["db"], "--spec", str(spec_file), "--as", "alice",
                     "--id", "bk-0123456789ab"]) == 0
    [b] = rows(env)
    assert b["id"] == "bk-0123456789ab" and b["holder"] == {"kind": "group", "name": "ml-team"} and b["count"] == 4


@pytest.mark.parametrize("extra,code,msg", [
    (["--unit", "nope"], 2, "not in the GPU inventory of gpudemo"),
    (["--unit", "x1"], 2, "not in the GPU inventory"),          # unité d'un autre cluster
])
def test_create_refuses_unknown_units(env, capsys, extra, code, msg):
    assert cli.main(["create", *args(env, *extra, model=""), "--as", "a"]) == code
    assert msg in capsys.readouterr().err
    assert rows(env) == []


@pytest.mark.parametrize("over,msg", [
    ({"start": T(13), "end": T(9)}, "end must come after"),
    ({"start": T(9), "end": "2027-02-01T00:00:00Z"}, "90 days at most"),
    ({"count": 0}, "count must be"),
])
def test_create_refuses_invalid_slots(env, capsys, over, msg):
    kw = {"start": T(9), "end": T(13), "count": 1}
    kw.update(over)
    assert cli.main(["create", *args(env, start=kw["start"], end=kw["end"], count=kw["count"]), "--as", "a"]) == 2
    assert msg in capsys.readouterr().err


def test_cancel_permissions(env, capsys):
    assert cli.main(["create", *args(env), "--as", "alice", "--holder", "dave"]) == 0
    bid = capsys.readouterr().out.strip()
    assert cli.main(["cancel", "--db", env["db"], "--id", bid, "--as", "bob"]) == 1
    assert "may cancel only their own" in capsys.readouterr().err
    assert cli.main(["cancel", "--db", env["db"], "--id", bid, "--as", "dave"]) == 0      # le titulaire
    assert cli.main(["create", *args(env, start=T(14), end=T(15)), "--as", "alice"]) == 0
    bid2 = capsys.readouterr().out.strip()
    assert cli.main(["cancel", "--db", env["db"], "--id", bid2, "--as", "bob", "--role", "admin"]) == 0
    assert cli.main(["cancel", "--db", env["db"], "--id", "bk-ffffffff", "--as", "bob"]) == 1
    assert cli.main(["cancel", "--db", env["db"], "--id", "x;rm", "--as", "bob"]) == 2


def test_list_mine_and_state_filters(env, capsys):
    assert cli.main(["create", *args(env), "--as", "alice"]) == 0
    assert cli.main(["create", *args(env, start=T(14), end=T(15)), "--as", "bob"]) == 0
    capsys.readouterr()
    cli.main(["list", "--db", env["db"], "--mine", "bob", "--json"])
    assert [b["created_by"] for b in json.loads(capsys.readouterr().out)["bookings"]] == ["bob"]
    cli.main(["list", "--db", env["db"], "--state", "active", "--json"])
    assert json.loads(capsys.readouterr().out)["bookings"] == []


def test_usage_errors(env, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["create", "--db", env["db"], "--cluster", "gpudemo"])
    assert e.value.code == 2


def test_db_is_wal_private_and_checks_inside_the_write_transaction(env):
    conn = bdb.connect(env["db"])
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert (Path(env["db"]).stat().st_mode & 0o777) == 0o600
    b = hb.normalize({"cluster": "gpudemo", "type": "vgpu", "model": "NVIDIA H100-20C", "count": 2,
                      "holder": {"kind": "user", "name": "a"}, "start": T(9), "end": T(13)}, NOW)
    bdb.insert_checked(conn, b, 2, "a", NOW)
    # une seconde connexion (un autre processus) voit la première et refuse
    other = bdb.connect(env["db"])
    with pytest.raises(bdb.Conflict) as e:
        bdb.insert_checked(other, dict(b, count=1), 2, "b", NOW)
    assert e.value.reasons[0]["code"] == "capacity"
    assert len(bdb.list_bookings(other)) == 1
    with pytest.raises(ValueError, match="invalid booking id"):
        bdb.insert_checked(other, b, 9, "b", NOW, bid="nope")


def test_default_path_follows_the_env_then_the_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_BOOKINGS_DB", str(tmp_path / "x.db"))
    assert bdb.default_path() == tmp_path / "x.db"
    monkeypatch.delenv("HARVESTER_OPS_BOOKINGS_DB")
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path / "state"))
    assert bdb.default_path() == tmp_path / "state" / "bookings.db"


def test_the_script_runs_as_a_program(env):
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "harvester-bookings.py"), "list", "--db", env["db"]],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "(no booking)" in r.stdout


def test_old_bookings_are_kept_with_the_schema(env):
    bdb.connect(env["db"])
    cols = [r[1] for r in sqlite3.connect(env["db"]).execute("PRAGMA table_info(bookings)")]
    assert {"id", "cluster", "type", "unit", "model", "count", "holder_kind", "holder", "start", "end",
            "target_kind", "note", "created_by", "created_at", "cancelled_at"} <= set(cols)
