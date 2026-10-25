"""Seconde revue des écrans de réplication : carnet en retard jugé sur les
VALEURS (IMP-1), adresses masquées jusqu'au chemin (I3/m6), secret gardé
pour la même requête seulement (m1), refus des répliques expliqué et
drapeaux de reprise (m3, m4)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_replication_cli_199 import run as cli_run  # noqa: E402
from test_replication_cli_ui import spec_file  # noqa: E402
from test_replication_routes_199 import PID, client, db, env, rp, script_cmd, wapp  # noqa: E402,F401
from test_replication_run_199 import World  # noqa: E402
import replication_run as rr  # noqa: E402


# ---------------------------------------------------------------------------
# IMP-1 : carnet en retard = une VALEUR du carnet a changé
# ---------------------------------------------------------------------------

ND = {"default/vm-a": "version: 2\n"}
RESIZE = {"default/vm-a": {"cpu": 2, "memory": "2Gi"}}
NETS = {"default/prod": "default/lab-net"}
HOOK = {"name": "dns", "at": "end", "method": "POST", "url": "https://hooks.example/ok"}


def _protected(tmp_path):
    w = World(tmp_path, vms=("vm-a", "vm-b"), mappings={"networks": dict(NETS)},
              options={"cloud_init": "rerun", "network_data": dict(ND), "resize": json.loads(json.dumps(RESIZE))})
    res = rr.protect(w.ctx())
    assert res["exit"] == 0, res
    return w


def _ui_save(**over):
    """Ce que l'onglet Réglages envoie à chaque enregistrement : tout."""
    rec = {"waves": [], "hooks": [], "network_data": dict(ND), "resize": json.loads(json.dumps(RESIZE))}
    rec.update(over)
    return rec


SAME = ["--cloud-init=rerun", "--mac=keep", "--start=started"]


@pytest.mark.parametrize("argv,spec,keys", [
    # enregistrement sans changement (l'interface renvoie tout)
    (SAME, {"recovery": _ui_save()}, None),
    # seule la case des volumes de secours dégradés
    (SAME, {"recovery": _ui_save(accept_degraded_standby=True)}, None),
    # seule une action annexe
    (SAME, {"recovery": _ui_save(hooks=[HOOK])}, None),
    ([], {"recovery": {"accept_degraded_standby": True}}, None),
    ([], {"mappings": {"networks": dict(NETS)}}, None),
    ([], {"mappings": {"drill_networks": {"default/prod": "default/drill"}}}, None),
    # une valeur changée par clé du carnet : la marque la nomme, elle seule
    (["--mac=new"], None, ["mac"]),
    (["--start=stopped"], None, ["start"]),
    (["--cloud-init=keep"], {"recovery": {"network_data": {}}}, ["cloud_init", "network_data"]),
    (SAME, {"recovery": _ui_save(network_data={"default/vm-a": "version: 2\n# eth1\n"})}, ["network_data"]),
    (SAME, {"recovery": _ui_save(network_data=dict(ND, **{"default/vm-b": "version: 2\n"}))}, ["network_data"]),
    (SAME, {"recovery": _ui_save(resize={"default/vm-a": {"cpu": 2, "memory": "4Gi"}})}, ["resize"]),
    (SAME, {"recovery": _ui_save(resize=dict(RESIZE, **{"default/vm-b": {"cpu": 1}}))}, ["resize"]),
    ([], {"mappings": {"networks": {"default/prod": "default/lab-net2"}}}, ["networks"]),
])
def test_only_a_changed_carnet_value_marks_the_plan_stale(tmp_path, capsys, argv, spec, keys):
    w = _protected(tmp_path)
    extra = [spec_file(tmp_path, spec)] if spec else []
    rc, data, _ = cli_run(capsys, w, "plan", "set", "plan-a", *argv, *extra)
    assert rc == 0, data
    flag = db.get_plan_flag(w.conn, "carnet-stale", w.pid)
    assert (flag or {}).get("keys") == keys
    assert data.get("protect_needed", False) is bool(keys)
    blocked = [f for f in rr.failover(w.ctx(), dry_run=True)["findings"] if f["code"] == "carnet-stale"]
    assert bool(blocked) is bool(keys)


def test_undo_back_to_the_protected_values_clears_the_mark(tmp_path, capsys):
    w = _protected(tmp_path)
    assert cli_run(capsys, w, "plan", "set", "plan-a", "--mac=new")[0] == 0
    rc, data, _ = cli_run(capsys, w, "plan", "set", "plan-a",
                          spec_file(tmp_path, {"recovery": {"resize": {"default/vm-a": {"cpu": 4}}}}))
    assert db.get_plan_flag(w.conn, "carnet-stale", w.pid)["keys"] == ["mac", "resize"]
    # Annuler le premier : seul resize reste en retard
    assert cli_run(capsys, w, "plan", "set", "plan-a", "--mac=keep")[0] == 0
    assert db.get_plan_flag(w.conn, "carnet-stale", w.pid)["keys"] == ["resize"]
    # Annuler le second : plus rien, la bascule passe
    rc, data, _ = cli_run(capsys, w, "plan", "set", "plan-a",
                          spec_file(tmp_path, {"recovery": {"resize": RESIZE}}, "u.json"))
    assert rc == 0 and "protect_needed" not in data, data
    assert db.get_plan_flag(w.conn, "carnet-stale", w.pid) is None
    assert not [f for f in rr.failover(w.ctx(), dry_run=True)["findings"] if f["code"] == "carnet-stale"]


def test_a_mark_without_baseline_keeps_its_keys(tmp_path, capsys):
    w = _protected(tmp_path)
    db.mark_carnet_stale(w.conn, w.pid, ["start"])       # marque d'avant la comparaison par valeurs
    assert cli_run(capsys, w, "plan", "set", "plan-a", "--mac=new")[0] == 0
    flag = db.get_plan_flag(w.conn, "carnet-stale", w.pid)
    assert flag["keys"] == ["mac", "start"] and "baseline" not in flag


@pytest.mark.parametrize("before,after,want", [
    ({}, {}, []),
    (rp.carnet_values({"cloud_init": "keep"}), rp.carnet_values({}), []),
    (rp.carnet_values({"resize": {}}), rp.carnet_values({}), []),
    (rp.carnet_values({"resize": RESIZE}), rp.carnet_values({"resize": {"default/vm-a": {"cpu": 2}}}), ["resize"]),
    (rp.carnet_values({}, {"networks": NETS}), rp.carnet_values({}, {"networks": {}}), ["networks"]),
    (rp.carnet_values({"mac": "keep", "start": "started"}), rp.carnet_values({"mac": "new", "start": "stopped"}),
     ["mac", "start"]),
])
def test_carnet_changes_compare_values(before, after, want):
    assert rp.carnet_changes(before, after) == want


@pytest.mark.parametrize("change,need", [(False, False), (True, True)])
def test_settings_route_hint_compares_values(client, env, change, need):
    conn = db.connect(env["db"])
    try:
        o = db.get_plan(conn, PID)["options"]
    finally:
        conn.close()
    body = {"cloud_init": o["cloud_init"], "mac": o["mac"], "start": o["start"],
            "recovery": {"waves": [], "hooks": [], "resize": {}, "network_data": {}, "accept_degraded_standby": True}}
    if change:
        body["mac"] = "new" if o["mac"] == "keep" else "keep"
    r = client.patch(f"/api/replication/plans/{PID}/settings", json=body)
    assert r.status_code == 202, r.get_json()
    assert r.get_json().get("protect_needed", False) is need


def test_plan_view_never_shows_the_baseline(client, env):
    conn = db.connect(env["db"])
    try:
        db.mark_carnet_stale(conn, PID, ["mac"], baseline={"mac": "keep"})
    finally:
        conn.close()
    got = client.get(f"/api/replication/plans/{PID}").get_json()["carnet_stale"]
    assert got["keys"] == ["mac"] and "baseline" not in got


# ---------------------------------------------------------------------------
# m1 : la route refuse un secret masqué pour un autre chemin
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [("url", "https://hooks.example/other"), ("method", "DELETE")])
def test_settings_route_refuses_a_masked_secret_for_another_request(client, env, field, value):
    from test_replication_routes_ui import HOOK, _store_hooks
    _store_hooks(env, [HOOK])
    got = client.get(f"/api/replication/plans/{PID}").get_json()["plan"]["options"]["hooks"]
    assert got[0]["url"] == "https://hooks.example/********"
    got[0][field] = value
    r = client.patch(f"/api/replication/plans/{PID}/settings", json={"recovery": {"hooks": got}})
    assert r.status_code == 400 and r.get_json()["code"] == "secret-missing", r.get_json()


# ---------------------------------------------------------------------------
# m3 : répliques au-delà des nœuds, refus dit en clair et drapeau
# ---------------------------------------------------------------------------

def _three_replicas(w):
    sc = w.dst.obj(rr.K_SC, None, "harvester-longhorn")
    sc["parameters"]["numberOfReplicas"] = "3"
    w.dst.put(rr.K_SC, sc)


def test_resume_of_a_protect_interrupted_under_199_is_refused_then_accepted(tmp_path, capsys):
    from test_replication_suspend_199 import at_step
    w = World(tmp_path, vms=("vm-a",))
    ctx, _ = at_step(w, ("initial-restore", "running"))
    assert rr.protect(ctx)["code"] == "interrupted"
    _three_replicas(w)                   # la règle de 1.100 s'applique à la reprise
    rc, data, err = cli_run(capsys, w, "resume", "plan-a")
    assert rc == rr.EXIT_BLOCKED and data["kind"] == "check", data
    assert "standby-replicas-exceed-nodes" in [f["code"] for f in data["findings"]]
    assert "--accept-degraded-standby" in data["error"] and "Settings tab" in data["error"]
    assert w.plan()["state"] == "protecting"
    assert [r["result"] for r in db.list_runs(w.conn, w.pid) if r["operation"] == "protect"][-1] == "interrupted"
    rc, data, err = cli_run(capsys, w, "resume", "plan-a", "--accept-degraded-standby")
    assert rc == 0, data
    assert w.plan()["state"] == "protected"
    assert not w.plan()["options"].get("accept_degraded_standby")      # pour cette exécution seulement


def test_protect_flag_accepts_degraded_standby_for_this_run(tmp_path, capsys):
    w = World(tmp_path, vms=("vm-a",))
    _three_replicas(w)
    rc, data, _ = cli_run(capsys, w, "protect", "plan-a", "--interval=5m")
    assert rc == rr.EXIT_BLOCKED and "--accept-degraded-standby" in data["error"], data
    rc, data, _ = cli_run(capsys, w, "protect", "plan-a", "--interval=5m", "--accept-degraded-standby")
    assert rc == 0, data


@pytest.mark.parametrize("flag", ["--accept-degraded-standby", "--accept-stale-carnet"])
def test_resume_refuses_an_acknowledgement_for_another_command(tmp_path, capsys, flag):
    w = World(tmp_path, vms=("vm-a",))
    rc, data, _ = cli_run(capsys, w, "resume", "plan-a", flag)
    assert rc == rr.EXIT_BLOCKED and data["kind"] == "bad-option", data


def test_refusal_hint_names_the_way_out():
    f = rp.replica_findings({"harvester-longhorn"}, {"harvester-longhorn": {"replicas": 3, "nodes": 1},
                                                     "harv-rep1": {"replicas": 1, "nodes": 1}}, False)
    hint = rp.refusal_hint(f)
    assert "harvester-longhorn" in hint and "harv-rep1" in hint and "--accept-degraded-standby" in hint
    assert rp.refusal_hint(rp.replica_findings({"harvester-longhorn"}, {"harvester-longhorn": {
        "replicas": 3, "nodes": 1}}, True)) == ""


# ---------------------------------------------------------------------------
# m4 : reprise d'une bascule avec l'accord sur des réglages en retard
# ---------------------------------------------------------------------------

def test_resume_of_a_failover_carries_the_stale_settings_acknowledgement(tmp_path):
    from test_replication_run_199 import Crash, protected
    w = protected(tmp_path, vms=("vm-a",))
    w.mutations, w.crash_at = 0, 1                 # arrêt brutal avant toute activation
    with pytest.raises(Crash):
        rr.failover(w.ctx())
    w.crash_at = None
    assert w.plan()["state"] == "failing_over"
    db.mark_carnet_stale(w.conn, w.pid, ["resize"])   # vu après le départ (suiveur, ou départ sous 1.99)
    res = rr.resume(w.ctx())
    assert res["exit"] == rr.EXIT_BLOCKED and "carnet-stale" in [f["code"] for f in res["findings"]], res
    assert w.plan()["state"] == "failing_over"
    res = rr.resume(w.ctx(), accept_stale_carnet=True)
    assert res["exit"] == 0 and w.plan()["state"] == "failed_over", res
