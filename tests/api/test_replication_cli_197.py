"""v1.97.0 : bin/harvester-replication.py, la ligne de commande de la réplication.

Les deux clusters simulés du déroulé (`test_replication_run_197.World`) sont
branchés par le point d'injection `KUBE_FACTORY` ; l'horloge et l'attente
sont celles du faux (des heures durent une fraction de seconde). Les
annulations passent par un vrai processus qui reçoit un vrai SIGTERM : ce
fichier sert aussi de harnais (`python3 test_replication_cli_197.py harness
...`), qui monte le banc simulé, lance la commande, et range l'état final.
"""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
_spec = importlib.util.spec_from_file_location("harvester_replication", ROOT / "bin" / "harvester-replication.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)
import replication as rp  # noqa: E402
import replication_db as db  # noqa: E402
import replication_run as rr  # noqa: E402
from test_replication_run_197 import GIB, VMS, World, protected  # noqa: E402

PID = "rp-0123456789ab"
FAILURE_KEYS = {"ok", "error", "kind"}
RESULT_KEYS = {"ok", "error", "kind", "plan", "state", "code", "message"}


def db_path(w):
    return w.conn.execute("PRAGMA database_list").fetchone()[2]


def wire(w):
    cli.KUBE_FACTORY = lambda name, kc: {"harvlab": w.src, "harvlab2": w.dst}.get(name)
    cli.NOW, cli.SLEEP = w.clock.now, w.clock.sleep


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    for name in ("KUBE_FACTORY", "NOW", "SLEEP"):
        monkeypatch.setattr(cli, name, getattr(cli, name))
    yield


def run(capsys, w, *argv, as_json=True):
    """(code, objet JSON ou texte, stderr) d'une commande sur le banc `w`."""
    if w is not None:
        wire(w)
        argv = list(argv) + [f"--db={db_path(w)}"]
    rc = cli.main(list(argv) + (["--json"] if as_json else []))
    out, err = capsys.readouterr()
    if as_json:
        assert out.count("\n{") == 0 and out.lstrip().startswith("{"), out
        return rc, json.loads(out), err
    return rc, out, err


def draft_world(tmp_path, **kw):
    return World(tmp_path, **kw)


# ---------------------------------------------------------------------------
# Refus d'usage : un seul objet {"ok", "error", "kind"}, code 2
# ---------------------------------------------------------------------------

CREATE = ["plan", "create", "--name=plan-b", "--from=harvlab", "--to=harvlab2", "--vm=default/vm-a", "--rpo=15m"]


@pytest.mark.parametrize("argv,kind", [
    (["plan", "create", "--name=x"], "usage"),
    (CREATE[:-1] + ["--rpo=soon"], "bad-rpo"),
    (CREATE + ["--map-net=default/prod"], "bad-mapping"),
    (CREATE + ["--map-net=bad=default/lab-net"], "bad-reference"),
    (CREATE + ["--timeout=probe"], "bad-timeout"),
    (CREATE + ["--timeout=nope=5"], "bad-timeout"),
    (CREATE + ["--interval=7m"], "bad-interval"),
    (CREATE + ["--retain=x"], "usage"),
    (CREATE + ["--selector=app=x"], "bad-selection"),
    (["plan", "create", "--name=plan-b", "--from=harvlab", "--to=harvlab", "--vm=default/vm-a", "--rpo=15m"],
     "same-cluster"),
    (["plan", "create", "--name=plan-b", "--from=harvlab", "--to=nowhere", "--vm=default/vm-a", "--rpo=15m"],
     "unknown-cluster"),
    (["plan", "create", "--name=plan-a", "--from=harvlab", "--to=harvlab2", "--vm=default/vm-a", "--rpo=15m"],
     "name-taken"),
    (["protect", "nope"], "unknown-plan"),
    (["protect", "rp-ffffffffffff"], "unknown-plan"),
    (["protect", "plan-a", "--timeout=probe=soon"], "bad-duration"),
    (["protect", "plan-a", "--timeout=client=0"], "bad-timeout"),
    (["failover", "plan-a", "--force-unplanned"], "usage"),
    (["failover", "plan-a", "--vm=vm-a"], "bad-reference"),
    (["check", "plan-a", "--force-unplanned"], "usage"),
    (["check", "plan-a", "--phase=later"], "usage"),
    (["plan", "set", "plan-a"], "usage"),
    (["plan", "set", "plan-a", "--paused=maybe"], "bad-option"),
    (["plan", "set", "plan-a", "--rpo=1s"], "bad-rpo"),
    (["events", "--plan=plan-a", "--before=yesterday"], "usage"),
    (["events"], "usage"),
    (["follow", "--once"], "usage"),
    (["measure", "formula", "--size=2Gi", "--writes=1", "--interval=5m", "--block=4Mi"], "usage"),
    (["measure", "formula", "--size=big", "--writes=1", "--interval=5m"], "usage"),
    (["nothing"], "usage"),
    (["status", "--bogus"], "usage"),
])
def test_usage_errors_are_one_failure_object(tmp_path, capsys, argv, kind):
    w = draft_world(tmp_path, vms=("vm-a",))
    rc, data, err = run(capsys, w, *argv)
    assert rc == 2, data
    assert set(data) == FAILURE_KEYS and data["ok"] is False and data["kind"] == kind, data
    assert "Traceback" not in err
    assert db.list_plans(w.conn)[0]["id"] == PID and len(db.list_plans(w.conn)) == 1
    assert w.src.mutations() == [] and w.dst.mutations() == []


def test_usage_error_without_json_writes_nothing_on_stdout(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a",))
    rc, out, err = run(capsys, w, "protect", "nope", as_json=False)
    assert rc == 2 and out == "" and "unknown plan" in err


def test_unexpected_error_hides_its_details(tmp_path, capsys, monkeypatch):
    w = draft_world(tmp_path, vms=("vm-a",))

    def boom(*_a, **_k):
        raise RuntimeError(f"secret detail in {db_path(w)}")
    monkeypatch.setattr(rr, "measure_report", boom)
    rc, data, err = run(capsys, w, "measure", "report", "plan-a")
    assert rc == 1 and data == {"ok": False, "kind": "failed", "error": "unexpected internal error (RuntimeError)"}
    assert "secret detail" not in err and "Traceback" not in err


# ---------------------------------------------------------------------------
# --opt=valeur : une valeur qui commence par un tiret reste une valeur
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv,kind", [
    (["plan", "create", "--name=-x", "--from=harvlab", "--to=harvlab2", "--vm=default/vm-a", "--rpo=15m"],
     "bad-name"),
    (CREATE[:-2] + ["--vm=-default/vm-a", "--rpo=15m"], "bad-reference"),
    (CREATE[:-1] + ["--rpo=-15m"], "bad-rpo"),
    (CREATE + ["--map-net=-default/prod=default/lab-net"], "bad-reference"),
    (CREATE + ["--interval=-5m"], "bad-duration"),
    (["failover", "plan-a", "--vm=-default/vm-a"], "bad-reference"),
    (["events", "--plan=plan-a", "--before=-1"], "usage"),
])
def test_dash_values_reach_our_validation(tmp_path, capsys, argv, kind):
    w = draft_world(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, *argv)
    assert rc == 2 and data["kind"] == kind, data
    assert "expected one argument" not in data["error"]


def test_dash_values_are_kept(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, *CREATE, "--by=-alice", "--target-name=default/vm-a=-x")
    assert rc == 2 and data["kind"] == "bad-mapping"           # le nom cible est contrôlé, pas avalé
    rc, data, _ = run(capsys, w, *CREATE, "--by=-alice", "--id=-42")
    assert rc == 0, data
    assert data["plan"]["created_by"] == "-alice"
    ev = db.plan_events(w.conn, data["plan"]["id"])[0]
    assert ev["by"] == "-alice" and ev["action_id"] == "-42"


# ---------------------------------------------------------------------------
# plan : create, list, show, set, delete ; --spec
# ---------------------------------------------------------------------------

PLAN_ROW_KEYS = {"id", "name", "source_host", "target_host", "source_host_kind", "target_host_kind", "state",
                 "generation", "active_side", "rpo_objective_s", "rpo_held_s", "interval_s", "next_tick_at",
                 "resources_by_state", "last_events", "actions", "resources"}
RESOURCE_ROW_KEYS = {"plan", "plan_name", "resource_kind", "resource_uid", "namespace", "name", "side",
                     "host_kind", "peer_host", "state", "reasons", "state_since", "rpo_held_s", "rpo_objective_s",
                     "last_applied_at", "last_events", "actions"}


def test_plan_create_list_show_schemas(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a", "vm-b"))
    rc, data, err = run(capsys, w, *CREATE, "--vm=default/vm-b", "--map-net=default/prod=default/lab-net",
                        "--map-sc=harvester-longhorn=harvester-longhorn", "--map-claim=default/vm-a-data=a-data",
                        "--target-name=default/vm-b=vm-b-dr", "--cloud-init=rerun", "--mac=new",
                        "--create-namespace", "--start=stopped", "--retain=8", "--interval=5m", "--follow=cli",
                        "--skew-tolerance=20s", "--timeout=probe=20")
    assert rc == 0, data
    assert set(data) == FAILURE_KEYS | {"plan"} and data["ok"] is True and data["error"] is None
    p = data["plan"]
    assert p["state"] == "draft" and p["vms"] == ["default/vm-a", "default/vm-b"] and p["retain"] == 8
    assert p["interval_s"] == 300 and p["cron"] == "*/5 * * * *"
    assert p["mappings"]["claims"] == {"default/vm-a-data": "a-data"}
    assert p["mappings"]["target_names"] == {"default/vm-b": "vm-b-dr"}
    assert p["options"] == dict(rp.DEFAULT_OPTIONS, cloud_init="rerun", mac="new", create_namespace=True,
                                start="stopped", follow="cli", skew_tolerance_s=20, timeouts={"probe": 20})
    assert "STEP_EVENT|plan|done|" in err
    assert w.src.mutations() == [] and w.dst.mutations() == []          # aucune écriture sur les clusters
    rc, data, _ = run(capsys, w, "plan", "list")
    assert rc == 0 and set(data) == FAILURE_KEYS | {"plans"}
    assert [r["name"] for r in data["plans"]] == ["plan-a", "plan-b"]
    assert all(set(r) == PLAN_ROW_KEYS for r in data["plans"])
    rc, data, _ = run(capsys, w, "plan", "show", "plan-b")
    assert rc == 0 and set(data) == FAILURE_KEYS | {"plan", "row", "status", "volumes", "runs", "lock", "lease"}
    assert set(data["row"]) == PLAN_ROW_KEYS and data["status"]["state"] == "draft"
    # identifiant ou nom
    rc, by_id, _ = run(capsys, w, "plan", "show", data["plan"]["id"])
    assert by_id["plan"] == data["plan"]


def test_plan_list_by_host_and_role(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, "plan", "list", "--host=harvlab2", "--role=viewer")
    assert rc == 0 and [r["id"] for r in data["plans"]] == [PID]
    acts = {a["name"]: a for a in data["plans"][0]["resources"][0]["actions"]}
    assert acts["failover"]["allowed"] is False and acts["failover"]["reason"] == "role"
    rc, data, _ = run(capsys, w, "plan", "list", "--host=elsewhere")
    assert data["plans"] == []


def test_spec_is_equivalent_to_the_options(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a", "vm-b"))
    spec = tmp_path / "req.json"
    spec.write_text(json.dumps({"name": "plan-s", "from": "harvlab", "to": "harvlab2",
                                "vm": ["default/vm-a", "default/vm-b"], "rpo": "15m",
                                "map-net": ["default/prod=default/lab-net"], "create_namespace": True,
                                "retain": 7, "cloud-init": "rerun", "timeout": ["probe=20"]}))
    rc, a, _ = run(capsys, w, "plan", "create", f"--spec={spec}")
    assert rc == 0, a
    rc, b, _ = run(capsys, w, "plan", "create", "--name=plan-o", "--from=harvlab", "--to=harvlab2",
                   "--vm=default/vm-a", "--vm=default/vm-b", "--rpo=15m", "--map-net=default/prod=default/lab-net",
                   "--create-namespace", "--retain=7", "--cloud-init=rerun", "--timeout=probe=20")
    assert rc == 0, b

    def strip(p):
        return {k: v for k, v in p.items() if k not in ("id", "name", "created_at", "updated_at")}
    assert strip(a["plan"]) == strip(b["plan"])


def test_spec_loses_to_the_command_line_and_refuses_unknown_keys(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a",))
    spec = tmp_path / "req.json"
    spec.write_text(json.dumps({"name": "plan-s", "from": "harvlab", "to": "harvlab2", "vm": ["default/vm-a"],
                                "rpo": "15m", "retain": 7}))
    rc, data, _ = run(capsys, w, "plan", "create", f"--spec={spec}", "--retain=9")
    assert rc == 0 and data["plan"]["retain"] == 9
    for bad in ({"name": "x", "spaceship": 1}, {"name": {"a": 1}}, ["name"], {"Name": "x"}):
        spec.write_text(json.dumps(bad))
        rc, data, _ = run(capsys, w, "plan", "create", f"--spec={spec}")
        assert rc == 2 and data["kind"] == "usage" and set(data) == FAILURE_KEYS, bad
    spec.write_text("{not json")
    rc, data, _ = run(capsys, w, "plan", "create", f"--spec={spec}")
    assert rc == 2 and str(tmp_path) not in data["error"]


def test_spec_from_stdin_for_a_command(tmp_path, capsys, monkeypatch):
    import io
    w = protected(tmp_path, vms=("vm-a",))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"unplanned": True})))
    rc, data, _ = run(capsys, w, "failover", "plan-a", "--spec=-")
    assert rc == 2 and data["kind"] == "confirmation-required"


def test_plan_set_hot_settings(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, "plan", "set", "plan-a", "--paused=true", "--follow=off", "--rpo=30m",
                      "--timeout=probe=20", "--timeout=follow_poll=5", "--by=ops", "--id=a1")
    assert rc == 0, data
    assert set(data) == FAILURE_KEYS | {"plan", "changed"}
    p = w.plan()
    assert p["options"]["paused"] is True and p["options"]["follow"] == "off" and p["rpo_objective_s"] == 1800
    assert p["options"]["timeouts"] == {"probe": 20, "follow_poll": 5}
    ev = db.plan_events(w.conn, PID)[0]
    assert (ev["operation"], ev["step"], ev["by"], ev["action_id"]) == ("plan", "set", "ops", "a1")
    rc, data, _ = run(capsys, w, "plan", "set", "plan-a", "--timeout=probe=", "--paused=false")
    assert rc == 0 and w.plan()["options"]["timeouts"] == {"follow_poll": 5}
    assert w.plan()["options"]["paused"] is False


@pytest.mark.parametrize("state,rc,kind", [("draft", 0, None), ("protected", 2, "state")])
def test_plan_delete_only_draft_or_unprotected(tmp_path, capsys, state, rc, kind):
    w = draft_world(tmp_path, vms=("vm-a",)) if state == "draft" else protected(tmp_path, vms=("vm-a",))
    got, data, _ = run(capsys, w, "plan", "delete", "plan-a")
    assert got == rc and data["kind"] == kind, data
    assert (db.get_plan(w.conn, PID) is None) == (rc == 0)
    if rc == 0:
        assert db.plan_events(w.conn, PID)[0]["step"] == "delete"   # le journal reste pour l'audit


# ---------------------------------------------------------------------------
# check, protect, measure
# ---------------------------------------------------------------------------

def test_check_without_blocker_writes_nothing(tmp_path, capsys):
    w = draft_world(tmp_path)
    events_before = len(db.plan_events(w.conn, PID, limit=500))
    rc, data, err = run(capsys, w, "check", "plan-a")
    assert rc == 0, data
    assert set(data) == RESULT_KEYS | {"phase", "findings", "skipped"} and data["code"] == "ok"
    assert {f["code"] for f in data["findings"]} >= {"backing-image-restore"}
    assert w.src.mutations(include_dry=True) == [] and w.dst.mutations(include_dry=True) == []
    assert len(db.plan_events(w.conn, PID, limit=500)) == events_before
    assert "STEP_EVENT|check|done|no blocker" in err


def test_check_blocker_is_exit_2(tmp_path, capsys):
    w = draft_world(tmp_path)
    name = rp.target_volume_name(PID, VMS["vm-b"]["uid"], "d1", 1, "vm-b")
    w.dst.put(rr.K_LH_VOLUME, {"metadata": {"name": name, "namespace": rr.NS, "labels": {}}, "spec": {}})
    rc, data, _ = run(capsys, w, "check", "plan-a")
    assert rc == 2 and data["ok"] is False and data["kind"] == "check"
    assert "target-volume-taken" in {f["code"] for f in data["findings"] if f["level"] == "block"}


def test_check_failover_phase_is_a_dry_run(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a",))
    n = len(w.src.mutations()) + len(w.dst.mutations())
    rc, data, _ = run(capsys, w, "check", "plan-a", "--phase=failover")
    assert rc == 0 and data["code"] == "dry-run", data
    assert len(w.src.mutations()) + len(w.dst.mutations()) == n
    assert w.plan()["state"] == "protected"
    rc, data, _ = run(capsys, w, "check", "plan-a", "--phase=failover-unplanned")
    assert rc == 2 and data["kind"] == "source-reachable"


def test_protect_result_schema_and_cluster_objects(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a",))
    rc, data, err = run(capsys, w, "protect", "plan-a", "--interval=3m", "--by=ops", "--id=act-1")
    assert rc == 0, data
    assert set(data) == RESULT_KEYS | {"interval_s", "findings"}
    assert data["state"] == "protected" and data["code"] == "protected" and data["interval_s"] == 180
    assert w.owned(w.dst) and w.owned(w.src)
    assert "STEP_EVENT|first-backup|done|" in err
    evs = [e for e in db.plan_events(w.conn, PID, limit=500) if e["operation"] == "protect"]
    assert evs and all(e["by"] == "ops" and e["action_id"] == "act-1" for e in evs)
    # relancée : rien de neuf
    n = len(w.src.mutations()) + len(w.dst.mutations())
    rc, data, _ = run(capsys, w, "protect", "plan-a")
    assert rc == 0 and len(w.src.mutations()) + len(w.dst.mutations()) == n


def test_protect_refusal_codes_pass_through(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a",), interval=None)
    rc, data, _ = run(capsys, w, "protect", "plan-a")
    assert rc == 2 and data["kind"] == "interval-required" and set(data) >= RESULT_KEYS
    rc, data, _ = run(capsys, w, "protect", "plan-a", "--interval=7m")
    assert rc == 2 and data["kind"] == "bad-interval"


def test_protect_dry_run(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, "protect", "plan-a", "--dry-run")
    assert data["code"] == "dry-run" and "dry_run" in data and rc in (0, 2)
    assert w.src.mutations() == [] and w.dst.mutations() == []


def test_measure_start_stop_report(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, "measure", "start", "plan-a", "--interval=1m")
    assert rc == 0 and data["state"] == "measuring" and data["interval_s"] == 60, data
    assert w.dst.mutations() == []
    rc, data, _ = run(capsys, w, "measure", "start", "plan-a", "--interval=7m")
    assert rc == 2 and data["kind"] == "bad-interval"
    rc, data, _ = run(capsys, w, "measure", "report", "plan-a")
    assert rc == 0 and set(data) == FAILURE_KEYS | {"plan", "report"}
    assert data["report"]["measured_step_s"] == 60 and data["report"]["assumption"] == "apply-equals-send"
    rc, data, _ = run(capsys, w, "measure", "stop", "plan-a")
    assert rc == 0 and data["state"] == "draft" and "passes" in data
    rc, data, _ = run(capsys, w, "measure", "stop", "plan-a")
    assert rc == 2 and data["kind"] == "state"


def test_measure_formula_bench_numbers(capsys):
    rc, data, _ = run(capsys, None, "measure", "formula", "--size=2Gi", "--writes=600", "--interval=3m",
                      "--throughput=72000000")
    assert rc == 0 and set(data) == FAILURE_KEYS | {"formula"}
    assert data["formula"] == json.loads(json.dumps(rr.measure_formula(2 * GIB, 600, 180, rp.BLOCK_SIZE, 72_000_000)))
    assert round(data["formula"]["forecast"]["scattered_estimate"]) == 454


# ---------------------------------------------------------------------------
# bascule, reprise, déprotection
# ---------------------------------------------------------------------------

FAILOVER_KEYS = RESULT_KEYS | {"run_id", "mode", "fenced", "skew_s", "refused_skew", "lost_after"}


def test_planned_failover(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, "failover", "plan-a", "--vm=default/vm-a")
    assert rc == 0, data
    assert set(data) == FAILOVER_KEYS and data["state"] == "failed_over" and data["mode"] == "planned"
    assert w.dst.obj(rr.K_VM, "default", "vm-a") is not None


@pytest.mark.parametrize("argv,rc,kind", [
    (["--unplanned"], 2, "confirmation-required"),
    (["--unplanned", "--yes"], 2, "source-reachable"),
    (["--vm=default/vm-z"], 2, "unknown-vm"),
])
def test_failover_refusals(tmp_path, capsys, argv, rc, kind):
    w = protected(tmp_path, vms=("vm-a",))
    got, data, _ = run(capsys, w, "failover", "plan-a", *argv)
    assert got == rc and data["kind"] == kind and data["ok"] is False and set(data) >= RESULT_KEYS, data
    assert w.plan()["state"] == "protected"


def test_unplanned_failover_with_the_source_lost(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a",))
    w.src.down = True
    rc, data, _ = run(capsys, w, "failover", "plan-a")
    assert rc == 2 and data["kind"] == "source-unreachable" and "--unplanned" in data["message"]
    rc, data, _ = run(capsys, w, "failover", "plan-a", "--unplanned", "--yes")
    assert rc == 0 and data["mode"] == "unplanned" and data["lost_after"], data


def test_failover_of_an_undeclared_source_goes_on_with_the_target(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, "status")       # banc branché
    cli.KUBE_FACTORY = lambda name, kc: {"harvlab2": w.dst}.get(name)
    rc = cli.main(["failover", "plan-a", "--unplanned", "--yes", "--json", f"--db={db_path(w)}"])
    data = json.loads(capsys.readouterr()[0])
    assert rc == 0 and data["state"] == "failed_over", data


def test_unprotect_refusals_and_left(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, "unprotect", "plan-a", "--delete-backups")
    assert rc == 2 and data["kind"] == "confirmation-required"
    assert run(capsys, w, "failover", "plan-a")[0] == 0
    rc, data, _ = run(capsys, w, "unprotect", "plan-a", "--delete-backups", "--yes")
    assert rc == 2 and data["kind"] == "backups-in-use" and data["facts"]["vms"] == ["default/vm-a"]
    rc, data, _ = run(capsys, w, "unprotect", "plan-a")
    assert rc == 0, data
    assert set(data) == RESULT_KEYS | {"left", "left_selectors", "backups_deleted"}
    assert data["state"] == "unprotected" and any(x.startswith("vm default/vm-a") for x in data["left"])
    assert w.dst.obj(rr.K_VM, "default", "vm-a") is not None          # décision 47 : rien de supprimé


def test_unprotect_with_delete_backups(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, "unprotect", "plan-a", "--delete-backups", "--yes")
    assert rc == 0 and data["backups_deleted"] > 0, data


def test_resume_after_an_interrupted_failover(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.hooks[("create", rr.K_PV)] = lambda name: (_ for _ in ()).throw(RuntimeError("apiserver gone"))
    rc, data, _ = run(capsys, w, "failover", "plan-a")
    assert rc == 1 and data["kind"] == "interrupted" and "run failover again" in data["error"]
    w.dst.hooks.clear()
    rc, data, _ = run(capsys, w, "resume", "plan-a")
    assert rc == 0 and data["state"] == "failed_over", data
    rc, data, _ = run(capsys, w, "resume", "plan-a")
    assert rc == 0 and data["code"] == "nothing-to-resume"


# ---------------------------------------------------------------------------
# status, resources, events
# ---------------------------------------------------------------------------

def test_status_and_its_exit_code(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, "status", "plan-a")
    assert rc == 0 and set(data) == FAILURE_KEYS | {"exceeded", "plans"} and data["exceeded"] is False
    st = data["plans"][0]
    assert st["name"] == "plan-a" and st["vms"][0]["rpo_held_s"] is not None and "last_events" in st
    w.clock.sleep(20 * 60)                        # objectif 15 min, plus rien d'appliqué
    rc, data, _ = run(capsys, w, "status")
    assert rc == 4 and data["ok"] is True and data["exceeded"] is True
    rc, data, _ = run(capsys, w, "status", "--host=elsewhere")
    assert rc == 0 and data["plans"] == []


def test_resources_by_host_match_the_plan_rows(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a", "vm-b"))
    rc, plans, _ = run(capsys, w, "plan", "list")
    root = {r["resource_uid"]: r for r in plans["plans"][0]["resources"]}
    for host, side in (("harvlab", "source"), ("harvlab2", "target")):
        rc, data, _ = run(capsys, w, "resources", f"--host={host}")
        assert rc == 0 and set(data) == FAILURE_KEYS | {"host", "side", "resources"}
        assert len(data["resources"]) == 2
        for row in data["resources"]:
            assert set(row) == RESOURCE_ROW_KEYS and row["side"] == side
            same = {k: v for k, v in row.items() if k not in ("side", "peer_host", "actions")}
            assert same == {k: v for k, v in root[row["resource_uid"]].items()
                            if k not in ("side", "peer_host", "actions")}
            names = {a["name"] for a in row["actions"]}
            assert ("failover" in names) == (side == "target")
            assert row["peer_host"] == ("harvlab2" if side == "source" else "harvlab")
    rc, data, _ = run(capsys, w, "resources", "--host=harvlab", "--side=target")
    assert data["resources"] == []


def test_events_paging_by_id_and_date(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a",))
    uid = VMS["vm-a"]["uid"]
    rc, p1, _ = run(capsys, w, "events", f"--resource={uid}", "--limit=5")
    assert rc == 0 and set(p1) == FAILURE_KEYS | {"events", "next_before"} and len(p1["events"]) == 5
    assert p1["next_before"] == p1["events"][-1]["id"]
    rc, p2, _ = run(capsys, w, "events", f"--resource={uid}", "--limit=5", f"--before={p1['next_before']}")
    assert {e["id"] for e in p2["events"]}.isdisjoint({e["id"] for e in p1["events"]})
    assert max(e["id"] for e in p2["events"]) < min(e["id"] for e in p1["events"])
    rc, p3, _ = run(capsys, w, "events", f"--resource={uid}", "--limit=500",
                    f"--before={p1['events'][-1]['ts']}")
    assert p3["events"] and all(e["ts"] < p1["events"][-1]["ts"] for e in p3["events"])
    assert p3["next_before"] is None
    rc, hp, _ = run(capsys, w, "events", "--host=harvlab2", "--limit=3")
    assert rc == 0 and all(e["host"] == "harvlab2" for e in hp["events"])
    rc, pp, _ = run(capsys, w, "events", "--plan=plan-a", "--limit=2")
    assert rc == 0 and len(pp["events"]) == 2


# ---------------------------------------------------------------------------
# follow : choix des plans, bail
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("follow_opt,argv,followed", [
    ("console", ["--all", "--console"], True),
    ("console", ["--all"], False),
    ("cli", ["--all"], True),
    ("cli", ["--all", "--console"], False),
    ("console", ["--plan=plan-a"], True),
    ("cli", ["--plan=plan-a"], True),
    ("off", ["--plan=plan-a"], False),
])
def test_follow_takes_the_plans_of_its_kind(tmp_path, capsys, follow_opt, argv, followed):
    w = protected(tmp_path, vms=("vm-a",))
    db.update_plan(w.conn, PID, options={"follow": follow_opt})
    rc, data, err = run(capsys, w, "follow", *argv, "--once")
    assert rc == 0 and set(data) == FAILURE_KEYS | {"report"}, data
    assert (PID in data["report"]["followed"]) == followed, data["report"]
    if not followed:
        assert data["report"]["skipped"][PID] == f"follow-{follow_opt}"
    assert db.get_lease(w.conn, PID) is None                    # bail rendu après --once
    assert "STEP_EVENT|follow|done|" in err


def test_follow_respects_a_held_lease_unless_take_over(tmp_path, capsys):
    w = protected(tmp_path, vms=("vm-a",))
    from datetime import datetime, timezone
    db.take_lease(w.conn, PID, "console:otherhost:1", 10 ** 6, now=datetime.fromtimestamp(w.clock.now(), timezone.utc))
    rc, data, _ = run(capsys, w, "follow", "--plan=plan-a", "--once")
    assert rc == 0 and data["report"]["skipped"][PID] == "lease-held"
    rc, data, _ = run(capsys, w, "follow", "--plan=plan-a", "--once", "--take-over")
    assert rc == 0 and PID in data["report"]["followed"]


# ---------------------------------------------------------------------------
# Aucun chemin dans les sorties ni au journal
# ---------------------------------------------------------------------------

class LeakyKube:
    """Un client qui répond à la sonde mais dont les autres erreurs citent le
    kubeconfig, comme kubectl."""

    def __init__(self, inner, path):
        self.inner, self.path = inner, path

    def get(self, *a, **k):
        return self.inner.get(*a, **k)

    def _fail(self, *_a, **_k):
        raise RuntimeError(f"error loading config file \"{self.path}\": permission denied")

    list = create = replace = patch = delete = _fail


@pytest.mark.parametrize("command", [["protect", "plan-a", "--interval=3m"], ["check", "plan-a"]])
def test_no_path_in_outputs_nor_in_the_journal(tmp_path, capsys, command):
    w = draft_world(tmp_path, vms=("vm-a",))
    kc = tmp_path / "secret-place" / "person.kubeconfig"
    wire(w)
    cli.KUBE_FACTORY = lambda name, k: LeakyKube(w.dst, k) if k else {"harvlab": w.src}.get(name)
    rc = cli.main(command + [f"--to-kubeconfig={kc}", "--json", f"--db={db_path(w)}"])
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert rc == 1 and data["kind"] == "failed" and "permission denied" in data["error"], data
    assert "<hidden>" in data["error"]
    for text in (out, err):
        assert str(kc) not in text and "secret-place" not in text and db_path(w) not in text
    journal = json.dumps([dict(r) for r in w.conn.execute("SELECT * FROM events")])
    assert "secret-place" not in journal


def test_a_signal_during_a_step_that_reports_failure_is_a_cancel(tmp_path, capsys):
    """measure stop ne distingue pas l'annulation d'un échec : le signal reçu
    fait quand même sortir en 3, kind `cancelled`."""
    w = draft_world(tmp_path, vms=("vm-a",))
    assert run(capsys, w, "measure", "start", "plan-a")[0] == 0
    w.src.hooks[("delete", rr.K_LH_JOB)] = lambda name: os.kill(os.getpid(), signal.SIGTERM)
    rc, data, _ = run(capsys, w, "measure", "stop", "plan-a")
    assert rc == 3 and data["kind"] == "cancelled" and data["ok"] is False, data
    assert signal.getsignal(signal.SIGTERM) is not cli._on_cancel_signal          # gestionnaire rendu


def test_unknown_cluster_message_shows_no_configuration_path(tmp_path, capsys, monkeypatch):
    cfg = tmp_path / "etc" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("clusters: []\n")
    monkeypatch.setenv("HARVESTER_OPS_CONFIG", str(cfg))
    state = tmp_path / "state"
    rc = cli.main(CREATE + ["--json", f"--state-dir={state}"])
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert rc == 2 and data["kind"] == "unknown-cluster"
    assert str(tmp_path) not in out + err


@pytest.mark.parametrize("argv", [
    ["plan", "list"], ["plan", "show", "plan-a"], ["status"], ["resources", "--host=harvlab"],
    ["events", "--plan=plan-a", "--limit=3"], ["protect", "plan-a"], ["check", "plan-a"],
    ["measure", "report", "plan-a"], ["follow", "--plan=plan-a", "--once"],
    ["measure", "formula", "--size=2Gi", "--writes=600", "--interval=3m", "--throughput=72Mi"],
])
def test_text_output_of_each_command(tmp_path, capsys, argv):
    w = protected(tmp_path, vms=("vm-a",))
    rc, out, err = run(capsys, w, *argv, as_json=False)
    assert rc == 0 and out.strip() and not out.lstrip().startswith("{"), (out, err)
    assert "Traceback" not in err and db_path(w) not in out


def test_step_event_message_keeps_pipes_on_one_line(capsys):
    cli.step("x", "done", "a|b\nc")
    assert capsys.readouterr().err == "STEP_EVENT|x|done|a|b c\n"


def test_install_links_the_replication_tool_like_the_others():
    import re
    text = (ROOT / "install.sh").read_text()
    assert re.search(r'ln -sf "\$PREFIX/harvester-replication\.py" "\$PREFIX/harvester-replication"', text)
    assert os.access(ROOT / "bin" / "harvester-replication.py", os.X_OK)


# ---------------------------------------------------------------------------
# Annulation : un vrai processus, un vrai SIGTERM
# ---------------------------------------------------------------------------

TRIGGERS = {
    "protect": ("first-backup", "running"),
    "failover-before": ("final-backup", "running"),
    "failover-after": ("activate", "done"),
    "follow": ("follow", "done"),
}


def harness(case, workdir, outfile, resume):
    """Processus fils : banc simulé, commande lancée par `main`, et à l'étape
    choisie une pause réelle pendant laquelle le parent envoie SIGTERM."""
    w = World(Path(workdir), vms=("vm-a",))
    if case != "protect":
        assert rr.protect(w.ctx())["exit"] == 0
    wire(w)
    trigger = TRIGGERS[case]
    orig = cli.step

    def step(sid, status, msg=""):
        orig(sid, status, msg)
        if (sid, status) == trigger:
            sys.stderr.write("HARNESS_READY\n")
            sys.stderr.flush()
            # le suiveur constate l'arrêt entre deux tours : une courte pause suffit
            deadline = time.monotonic() + (1 if case == "follow" else 20)
            while time.monotonic() < deadline:
                time.sleep(0.05)               # le signal arrive ici, en pleine étape
    cli.step = step
    argv = {"protect": ["protect", "plan-a", "--interval=3m"],
            "follow": ["follow", "--plan=plan-a"]}.get(case, ["failover", "plan-a"])
    rc = cli.main(argv + ["--json", f"--db={db_path(w)}"])
    cli.step = orig
    w.clock.sleep(200)                         # les sauvegardes défaites quittent la cible à sa relecture
    w.dst.list(rr.K_LH_BACKUP)
    state = {"rc": rc, "plan_state": w.plan()["state"], "origin": db.error_origin(w.conn, w.pid),
             "lock": db.get_lock(w.conn, w.pid), "owned_src": [list(k) for k in w.owned(w.src)],
             "owned_dst": [list(k) for k in w.owned(w.dst)],
             "src_labels": {k[2]: (o["metadata"].get("labels") or {}) for k, o in w.src.objs.items()
                            if k[0] == rr.K_LH_VOLUME},
             "src_vm": (w.src.obj(rr.K_VM, "default", "vm-a") or {}).get("spec", {}).get("runStrategy"),
             "src_vmi": bool(w.src.obj(rr.K_VMI, "default", "vm-a")),
             "standby": {v["target_volume"]: (w.dst.obj(rr.K_LH_VOLUME, rr.NS, v["target_volume"]) or {})
                         .get("spec", {}).get("Standby") for v in db.list_volumes(w.conn, w.pid)},
             "lease": db.get_lease(w.conn, w.pid),
             "dst_vm": bool(w.dst.obj(rr.K_VM, "default", "vm-a")), "runs": [r["result"] for r in
                                                                          db.list_runs(w.conn, w.pid)]}
    if resume:
        with open(os.devnull, "w") as null:
            saved, sys.stdout = sys.stdout, null
            try:
                state["resume_rc"] = cli.main(["resume", "plan-a", "--json", f"--db={db_path(w)}"])
            finally:
                sys.stdout = saved
        state["resume_state"] = w.plan()["state"]
        state["resume_dst_vm"] = bool(w.dst.obj(rr.K_VM, "default", "vm-a"))
    Path(outfile).write_text(json.dumps(state, default=str))


def run_cancel(tmp_path, case, resume=False, sig=signal.SIGTERM):
    out = tmp_path / "state.json"
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.Popen([sys.executable, __file__, "harness", case, str(work), str(out), str(int(resume))],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        lines = []
        while True:
            line = proc.stderr.readline()
            if not line:
                break
            lines.append(line)
            if line.strip() == "HARNESS_READY":
                proc.send_signal(sig)
                break
        stdout, rest = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    err = "".join(lines) + rest
    assert proc.returncode == 0, err
    return json.loads(stdout), json.loads(out.read_text()), err


def test_cancel_protect_rolls_back_what_it_created(tmp_path):
    data, st, err = run_cancel(tmp_path, "protect")
    assert st["rc"] == 3 and data["ok"] is False and data["kind"] == "cancelled", (data, err)
    assert set(data) >= RESULT_KEYS and data["code"] == "cancelled"
    assert st["plan_state"] == "error" and st["origin"] == "protecting" and st["lock"] is None
    assert st["owned_src"] == [] and st["owned_dst"] == []
    assert all(labels == {} for labels in st["src_labels"].values())
    assert "SIGTERM" in data["error"] and "Traceback" not in err


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_cancel_failover_before_activation_is_undone(tmp_path, sig):
    data, st, err = run_cancel(tmp_path, "failover-before", sig=sig)
    assert st["rc"] == 3 and data["kind"] == "cancelled" and "undone" in data["error"], (data, err)
    assert st["plan_state"] == "protected" and st["lock"] is None
    assert st["src_vm"] == "Always" and st["src_vmi"] is True
    assert st["standby"] and all(v is True for v in st["standby"].values())
    assert st["dst_vm"] is False and st["runs"][-1] == "rolled-back"


def test_cancel_failover_after_activation_keeps_everything_and_resumes(tmp_path):
    data, st, err = run_cancel(tmp_path, "failover-after", resume=True)
    assert st["rc"] == 3 and data["kind"] == "cancelled" and data["code"] == "interrupted", (data, err)
    assert "run failover again" in data["error"]
    assert st["plan_state"] == "error" and st["origin"] == "failing_over" and st["lock"] is None
    assert st["standby"] and all(v is False for v in st["standby"].values())      # activés, gardés
    assert st["src_vm"] == "Halted" and st["runs"][-1] == "interrupted"
    assert st["resume_rc"] == 0 and st["resume_state"] == "failed_over" and st["resume_dst_vm"] is True


def test_follow_loop_stops_on_sigterm_and_releases_its_lease(tmp_path):
    data, st, err = run_cancel(tmp_path, "follow")
    assert st["rc"] == 0 and data["ok"] is True and data["stopped"] is True and data["signal"] == "SIGTERM", err
    assert set(data) == FAILURE_KEYS | {"rounds", "stopped", "signal", "last"} and data["rounds"] >= 1
    assert PID in data["last"]["followed"]
    assert st["lease"] is None and st["plan_state"] == "protected"


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "harness":
    harness(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] == "1")
