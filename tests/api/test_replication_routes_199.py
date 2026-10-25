"""v1.99.0 : la réplication dans la console, sans écran.

Lectures droit au registre par les fonctions mêmes de la ligne de commande
(`replication_db.plan_rows` / `host_rows`) : le menu racine et le sous-menu
d'un hôte rendent les mêmes lignes pour le même côté. Toute écriture lance
bin/harvester-replication.py en action suivie, valeurs en `--option=valeur`,
kubeconfigs de la personne pour les deux clusters, commande montrée sans
chemin. Rôles : lecture viewer, bascule et suppression des sauvegardes
admin, le reste operator. Relais : annulée et défaite, interrompue à
reprendre, retour arrière arrivé à échéance. Délai de retour arrière avant
SIGKILL (annulation, arrêt de la console). Fil de suivi : démarrage unique,
relance avec délai croissant, aucune relance après un arrêt demandé, arrêt
gracieux, interrupteurs."""

import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")
import app as wapp  # noqa: E402

SRC = (ROOT / "web" / "app.py").read_text()
rp, db = wapp._rp, wapp._rdb
PID, PID2 = "rp-0123456789ab", "rp-ba9876543210"
SCRIPT = str(wapp.BIN_DIR / wapp.REPLICATION_SCRIPT)
KC = {"harvlab": "/srv/kc/harvlab.yaml", "harvlab2": "/srv/kc/harvlab2.yaml", "harvlab3": "/srv/kc/harvlab3.yaml"}
HOSTILE = ["a;rm -rf ~", "$(id)", "-x", "<img src=x onerror=alert(1)>", "--json", "q'uote\"d"]

_RealThread = threading.Thread


class _NoThread:
    """Retient le fil de travail d'une action (`work`) sans le lancer."""
    started = []

    def __new__(cls, target=None, args=(), **k):
        if getattr(target, "__name__", "") != "work":
            return _RealThread(target=target, args=args, **k)
        return super().__new__(cls)

    def __init__(self, target=None, args=(), **k):
        self.target, self.args = target, args

    def start(self):
        _NoThread.started.append(self)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(conn):
    """Plan `web` harvlab -> harvlab2 protégé (rep-a deux disques, rep-b un),
    plan `db` harvlab2 -> harvlab3 en brouillon (rep-c) : harvlab2 est cible
    de l'un et source de l'autre."""
    n = now_utc()
    req = rp.normalize_plan({"name": "web", "source_cluster": "harvlab", "target_cluster": "harvlab2",
                             "rpo_objective_s": "15m", "interval_s": "3m",
                             "vms": ["default/rep-a", "default/rep-b"],
                             "options": {"timeouts": {"undo_total": 100, "client": 20}}})
    db.insert_plan(conn, req, created_by="alice", plan_id=PID)
    for uid, name in (("u-a", "rep-a"), ("u-b", "rep-b")):
        db.upsert_resource(conn, PID, {"resource_uid": uid, "source_host": "harvlab", "target_host": "harvlab2",
                                       "namespace": "default", "name": name, "generation": 1})
    for uid, disk, vol in (("u-a", "disk-0", "pvc-a0"), ("u-a", "disk-1", "pvc-a1"), ("u-b", "disk-0", "pvc-b0")):
        db.upsert_volume(conn, PID, {"generation": 1, "vm_uid": uid, "disk": disk, "source_volume": vol,
                                     "state": "standby"})
    db.set_state(conn, PID, "draft", "protecting")
    db.set_state(conn, PID, "protecting", "protected")
    for uid in ("u-a", "u-b"):
        db.set_resource_state(conn, PID, uid, "protecting")
        db.set_resource_state(conn, PID, uid, "protected")
    for vol, uid, age in (("pvc-a0", "u-a", 200), ("pvc-a1", "u-a", 230), ("pvc-b0", "u-b", 3000)):
        snap = n - timedelta(seconds=age)
        db.record_tick(conn, PID, {"source_volume": vol, "vm_uid": uid, "generation": 1, "backup": f"b-{vol}",
                                   "snapshot_at": iso(snap), "backup_done_at": iso(snap + timedelta(seconds=5)),
                                   "state": "completed"})
        db.mark_applied(conn, PID, vol, f"b-{vol}", snap + timedelta(seconds=15), 1)
    for i in range(4):
        db.record_event(conn, "follow", step=f"s{i}", plan_id=PID, resource_uid="u-a", host="harvlab2",
                        code="rpo-recovered", message=f"event {i}", action_id="abc123abc123")
    req2 = rp.normalize_plan({"name": "db", "source_cluster": "harvlab2", "target_cluster": "harvlab3",
                              "rpo_objective_s": "1h", "vms": ["default/rep-c"]})
    db.insert_plan(conn, req2, created_by="alice", plan_id=PID2)
    db.upsert_resource(conn, PID2, {"resource_uid": "u-c", "source_host": "harvlab2", "target_host": "harvlab3",
                                    "namespace": "default", "name": "rep-c", "generation": 1})
    # écrans de suivi : un canal de notification rangé (essai d'envoi)
    db.set_setting(conn, "notify", {"enabled": True, "min_level": "warn", "codes": None, "channels": [
        {"id": "c1", "name": "ops", "kind": "matrix", "homeserver": "https://matrix.example", "room": "!a:example",
         "token": "stored-matrix-token"}]})


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    dbp = state / "replication.db"
    monkeypatch.setenv("HARVESTER_OPS_REPLICATION_DB", str(dbp))
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"clusters": [
        {"name": n, "kubeconfig": p} for n, p in KC.items()]})
    access = dict(KC)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: access.get(c))
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    monkeypatch.setattr(wapp.threading, "Thread", _NoThread)
    conn = db.connect(dbp)
    seed(conn)
    conn.close()
    _NoThread.started = []
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    yield {"state": state, "db": dbp, "access": access}
    wapp.ACTIONS.clear()
    wapp.ACTIONS.update(saved)


@pytest.fixture
def client(env, monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")        # mode ouvert : admin
    return wapp.app.test_client()


@pytest.fixture
def as_user(env, monkeypatch, tmp_path):
    (tmp_path / "htpasswd").write_text("")
    roles = tmp_path / "roles.yaml"
    roles.write_text("default_role: viewer\nusers:\n  patronne: admin\n  alice: operator\n  curieux: viewer\n")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    monkeypatch.setattr(wapp, "ROLES_PATH", roles)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: True)

    def call(user, method, path, **kw):
        token = base64.b64encode(f"{user}:x".encode()).decode()
        with wapp.app.test_client() as c:
            return c.open(path, method=method, headers={"Authorization": f"Basic {token}"}, **kw)
    return call


def script_cmd():
    t = _NoThread.started[-1]
    cell = dict(zip(t.target.__code__.co_freevars, t.target.__closure__ or ()))
    return cell["cmd"].cell_contents


def only_run():
    [run] = wapp.ACTIONS.values()
    return run


def base(env, *sub, user=None):
    out = [sys.executable, SCRIPT, *sub, "--json", f"--state-dir={env['state']}", f"--db={env['db']}"]
    return out + ([f"--by={user}"] if user else [])


KUBE = [f"--from-kubeconfig={KC['harvlab']}", f"--to-kubeconfig={KC['harvlab2']}"]


# --- lignes : menu racine et sous-menu d'un hôte ------------------------------------

def test_root_view_lists_every_plan_with_its_resources_rpo_actions_and_events(client):
    r = client.get("/api/replication/plans")
    assert r.status_code == 200
    d = r.get_json()
    by = {p["id"]: p for p in d["plans"]}
    assert set(by) == {PID, PID2} and d["side"] == "any" and d["role"] == "admin"
    web = by[PID]
    assert web["state"] == "protected" and web["rpo_objective_s"] == 900
    assert web["rpo_held_s"] is not None and web["rpo_held_s"] > 900           # rep-b en retard
    assert web["resources_by_state"] == {"protected": 1, "lagging": 1}
    rows = {x["name"]: x for x in web["resources"]}
    assert rows["rep-b"]["state"] == "lagging" and rows["rep-a"]["state"] == "protected"
    assert [e["step"] for e in rows["rep-a"]["last_events"]] == ["s3", "s2", "s1"]
    acts = {a["name"]: a for a in web["actions"]}
    assert acts["failover"]["allowed"] is True and acts["failover"]["role"] == "admin"
    assert d["follower"]["running"] is False


def test_root_rows_equal_the_cli_rows(client, env):
    conn = db.connect(env["db"])
    try:
        cli = db.plan_rows(conn, db.list_plans(conn), now_utc(), role="admin")
    finally:
        conn.close()
    got = client.get("/api/replication/plans").get_json()["plans"]
    strip = lambda rows: [{k: v for k, v in p.items() if k not in ("rpo_held_s", "resources", "next_tick_at")}
                          for p in rows]
    assert strip(got) == strip(cli)                # même fonction, à l'horloge près


@pytest.mark.parametrize("host,side", [("harvlab2", "target"), ("harvlab", "source"), ("harvlab2", "source")])
def test_host_view_rows_are_the_root_rows_for_the_same_side(client, host, side):
    root = client.get(f"/api/replication/plans?side={side}&host={host}").get_json()["plans"]
    want = [r for p in root for r in p["resources"]
            if (p["target_host"] if side == "target" else p["source_host"]) == host]
    got = client.get(f"/api/replication/hosts/{host}/resources?side={side}").get_json()
    assert got["host"] == host and got["side"] == side and got["host_kind"] == "harvester"
    assert want and [_nocl(x) for x in got["resources"]] == [_nocl(x) for x in want]


def _nocl(row):
    # `rpo_held_s` suit l'horloge entre deux lectures
    return {k: v for k, v in row.items() if k != "rpo_held_s"}


def test_host_view_any_differs_from_the_root_only_by_side_peer_and_actions(client):
    root = {r["resource_uid"]: r for p in client.get("/api/replication/plans").get_json()["plans"]
            for r in p["resources"]}
    got = client.get("/api/replication/hosts/harvlab2/resources?side=any").get_json()["resources"]
    assert sorted((x["resource_uid"], x["side"]) for x in got) == [("u-a", "target"), ("u-b", "target"),
                                                                   ("u-c", "source")]
    for x in got:
        same = {k for k in x if k not in ("side", "peer_host", "actions", "rpo_held_s")}
        assert {k: x[k] for k in same} == {k: root[x["resource_uid"]][k] for k in same}


@pytest.mark.parametrize("user,allowed", [("curieux", False), ("alice", False), ("patronne", True)])
def test_failover_is_offered_on_the_target_side_with_the_role_of_the_person(as_user, user, allowed):
    tgt = as_user(user, "GET", "/api/replication/hosts/harvlab2/resources?side=target").get_json()["resources"]
    src = as_user(user, "GET", "/api/replication/hosts/harvlab/resources?side=source").get_json()["resources"]
    a = {x["name"]: {y["name"]: y for y in x["actions"]} for x in tgt}["rep-a"]
    assert a["failover"]["allowed"] is allowed and (allowed or a["failover"]["reason"] == "role")
    assert a["unprotect"]["allowed"] is (user != "curieux")
    assert all("failover" not in {y["name"] for y in x["actions"]} for x in src)


@pytest.mark.parametrize("path", ["/api/replication/plans?side=both", "/api/replication/hosts/harvlab/resources?side=x",
                                  "/api/replication/hosts/bad%20host/resources", "/api/replication/plans?host=a%20b",
                                  "/api/replication/plans/web", "/api/replication/plans/rp-0123456789ab/stats?limit=0",
                                  "/api/replication/plans/rp-0123456789ab/stats?since=yesterday",
                                  "/api/replication/plans/rp-0123456789ab/stats?resource=a%20b",
                                  "/api/replication/resources/u-a/events?limit=501",
                                  "/api/replication/resources/u-a/events?limit=x",
                                  "/api/replication/resources/u-a/events?before=not-a-date",
                                  "/api/replication/resources/%20x/events"])
def test_reads_refuse_bad_parameters(client, path):
    assert client.get(path).status_code == 400


def test_plan_detail_unknown_is_404(client):
    r = client.get("/api/replication/plans/rp-ffffffffffff")
    assert r.status_code == 404 and r.get_json()["code"] == "unknown-plan"


def test_plan_detail_carries_status_volumes_runs_lock_and_lease(client):
    d = client.get(f"/api/replication/plans/{PID}").get_json()
    assert set(d) == {"plan", "row", "status", "volumes", "runs", "lock", "lease", "role", "carnet_stale"}
    assert d["plan"]["id"] == PID and d["row"]["id"] == PID and len(d["volumes"]) == 3
    assert d["status"]["exceeded"] is True and d["lock"] is None and d["lease"] is None
    assert {v["name"]: v["state"] for v in d["status"]["vms"]} == {"rep-a": "protected", "rep-b": "lagging"}


def test_events_are_paged_by_id_and_by_date(client, env):
    conn = db.connect(env["db"])
    for i in range(4, 7):
        db.record_event(conn, "failover", step=f"s{i}", plan_id=PID, resource_uid="u-a", level="warn")
    conn.close()
    full = client.get("/api/replication/resources/u-a/events?limit=500").get_json()
    assert full["next_before"] is None and len(full["events"]) == 9      # 7 du suiveur + 2 changements d'état
    p1 = client.get("/api/replication/resources/u-a/events?limit=3").get_json()
    assert [e["step"] for e in p1["events"]] == ["s6", "s5", "s4"] and p1["next_before"] == p1["events"][-1]["id"]
    p2 = client.get(f"/api/replication/resources/u-a/events?limit=3&before={p1['next_before']}").get_json()
    assert [e["step"] for e in p2["events"]] == ["s3", "s2", "s1"]
    p3 = client.get(f"/api/replication/resources/u-a/events?limit=3&before={p2['next_before']}").get_json()
    p4 = client.get(f"/api/replication/resources/u-a/events?limit=3&before={p3['next_before']}").get_json()
    assert p1["events"] + p2["events"] + p3["events"] + p4["events"] == full["events"] and p4["next_before"] is None
    by_date = client.get(f"/api/replication/resources/u-a/events?limit=2&before={p1['events'][-1]['ts']}").get_json()
    assert [e["step"] for e in by_date["events"]] == ["s3", "s2"]
    e = p2["events"][0]
    assert e["action_id"] == "abc123abc123" and e["operation"] == "follow" and isinstance(e["facts"], dict)
    late = client.get("/api/replication/resources/u-a/events?before=2000-01-01T00:00:00Z").get_json()
    assert late["events"] == [] and late["next_before"] is None


def test_stats_are_read_oldest_first_and_filtered(client, env):
    conn = db.connect(env["db"])
    n = now_utc()
    for uid in ("u-a", "u-b"):
        for i in range(3):
            db.record_stats(conn, PID, {"resource_uid": uid, "generation": 1,
                                        "pass_at": iso(n - timedelta(minutes=10 - i)), "disks": 1})
    conn.close()
    d = client.get(f"/api/replication/plans/{PID}/stats?resource=u-a").get_json()
    assert [s["resource_uid"] for s in d["stats"]] == ["u-a"] * 3
    assert [s["pass_at"] for s in d["stats"]] == sorted(s["pass_at"] for s in d["stats"])
    assert len(client.get(f"/api/replication/plans/{PID}/stats?limit=2").get_json()["stats"]) == 2


# --- écritures : ligne de commande exacte --------------------------------------------

CASES = [
    ("check", "POST", f"/api/replication/plans/{PID}/check", {"phase": "failover", "force_unplanned": True},
     ["check", PID, "--phase=failover", "--force-unplanned"] , True, "harvlab"),
    ("measure-start", "POST", f"/api/replication/plans/{PID}/measure", {"op": "start", "interval": "-5m"},
     ["measure", "start", PID, "--interval=-5m"], True, "harvlab"),
    ("measure-stop", "POST", f"/api/replication/plans/{PID}/measure", {"op": "stop"},
     ["measure", "stop", PID], True, "harvlab"),
    ("protect", "POST", f"/api/replication/plans/{PID}/protect", {"interval": "5m", "dry_run": True},
     ["protect", PID, "--interval=5m", "--dry-run"], True, "harvlab"),
    # v1.99.0, décision 56 : la rétention d'un plan protégé passe par protect
    ("protect", "POST", f"/api/replication/plans/{PID}/protect", {"retain": 9},
     ["protect", PID, "--retain=9"], True, "harvlab"),
    ("failover", "POST", f"/api/replication/plans/{PID}/failover",
     {"vms": ["default/rep-a"], "confirm": "web"}, ["failover", PID, "--vm=default/rep-a", "--yes"], True, "harvlab2"),
    ("failover", "POST", f"/api/replication/plans/{PID}/failover",
     {"all_vms": True, "mode": "unplanned", "force_unplanned": True, "accept_skew": True, "confirm": "web"},
     ["failover", PID, "--all-vms", "--unplanned", "--force-unplanned", "--accept-skew", "--yes"], True, "harvlab2"),
    ("failover", "POST", f"/api/replication/plans/{PID}/failover", {"vms": ["default/rep-b"], "dry_run": True},
     ["failover", PID, "--vm=default/rep-b", "--dry-run"], True, "harvlab2"),
    ("resume", "POST", f"/api/replication/plans/{PID}/resume", None, ["resume", PID], True, "harvlab2"),
    ("unprotect", "POST", f"/api/replication/plans/{PID}/unprotect", {"confirm": "web"},
     ["unprotect", PID, "--yes"], True, "harvlab"),
    ("unprotect-delete-backups", "POST", f"/api/replication/plans/{PID}/unprotect",
     {"confirm": "web", "delete_backups": True}, ["unprotect", PID, "--yes", "--delete-backups"], True, "harvlab"),
    ("settings", "PATCH", f"/api/replication/plans/{PID}/settings",
     {"rpo": "30m", "paused": True, "follow": "cli", "skew_tolerance": "20s",
      "timeouts": {"undo_total": 300, "client": None}},
     ["plan", "set", PID, "--rpo=30m", "--skew-tolerance=20s", "--paused=true", "--follow=cli",
      "--timeout=client=", "--timeout=undo_total=300"], False, "harvlab"),
    ("delete", "DELETE", f"/api/replication/plans/{PID2}", None, ["plan", "delete", PID2], False, "harvlab2"),
]


@pytest.mark.parametrize("op,method,path,body,argv,kube,cluster", CASES)
def test_each_mutation_spawns_the_exact_command_as_a_tracked_action(client, env, op, method, path, body, argv,
                                                                    kube, cluster):
    r = client.open(path, method=method, json=body) if body is not None else client.open(path, method=method)
    assert r.status_code == 202, r.get_json()
    run = only_run()
    plan_id = argv[argv.index("delete") + 1] if op == "delete" else PID
    assert r.get_json()["action_id"] == run.id and r.get_json()["plan"] == plan_id
    sub = [a for a in argv if not a.startswith("--")]
    opts = [a for a in argv if a.startswith("--")]
    kube_args = []
    if kube:
        kube_args = KUBE
    want = base(env, *sub) + opts + kube_args + [f"--id={run.id}"]
    assert script_cmd() == want
    assert run.action == f"replication:{plan_id}:{op}" and run.cluster == cluster
    assert run.kill_grace == (100 + 20 + 15 if plan_id == PID else 240 + 30 + 15)
    # décision 68 : à l'arrêt de la console, seule une bascule attend son
    # retour arrière ; le reste s'arrête au point sûr (un appel kubectl + marge)
    assert run.shutdown_grace == (run.kill_grace if op == "failover" else (20 if plan_id == PID else 30) + 15)
    assert run.required_role == ("admin" if op in ("failover", "unprotect-delete-backups") else "operator")
    # commande montrée : ni kubeconfig, ni base, ni répertoire d'état
    shown = " ".join(run.cmd)
    assert run.cmd[0] == "harvester-replication" and "/srv" not in shown and str(env["state"]) not in shown
    assert run.dry_run is (op == "check" or "--dry-run" in argv)


def test_create_validates_like_the_script_and_spawns_plan_create(client, env):
    body = {"name": "web2", "from": "harvlab", "to": "harvlab2", "rpo": "15m", "vms": ["default/rep-a", "ns2/x"],
            "map_net": {"default/lab-net": "default/lab-net2"}, "map_sc": {"fast": "slow"},
            "target_name": {"default/rep-a": "rep-a-dr"}, "cloud_init": "rerun", "mac": "new",
            "create_namespace": True, "start": "stopped", "retain": 4, "interval": "5m", "follow": "console",
            "skew_tolerance": "15s", "timeouts": {"client": 45}}
    r = client.post("/api/replication/plans", json=body)
    assert r.status_code == 202, r.get_json()
    run = only_run()
    assert script_cmd() == base(env, "plan", "create") + [
        "--name=web2", "--from=harvlab", "--to=harvlab2", "--rpo=15m", "--cloud-init=rerun", "--mac=new",
        "--start=stopped", "--retain=4", "--interval=5m", "--follow=console", "--skew-tolerance=15s",
        "--vm=default/rep-a", "--vm=ns2/x", "--map-net=default/lab-net=default/lab-net2", "--map-sc=fast=slow",
        "--target-name=default/rep-a=rep-a-dr", "--create-namespace", "--timeout=client=45", f"--id={run.id}"]
    assert run.action == "replication:create:web2" and run.required_role == "operator"
    assert r.get_json()["name"] == "web2"


@pytest.mark.parametrize("body,code", [
    ({"name": "Bad Name", "from": "harvlab", "to": "harvlab2", "rpo": "15m", "vms": ["default/a"]}, "bad-name"),
    ({"name": "x", "from": "harvlab", "to": "harvlab", "rpo": "15m", "vms": ["default/a"]}, "same-cluster"),
    ({"name": "x", "from": "harvlab", "to": "nowhere", "rpo": "15m", "vms": ["default/a"]}, "unknown-cluster"),
    ({"name": "x", "from": "harvlab", "to": "harvlab2", "rpo": "soon", "vms": ["default/a"]}, "bad-rpo"),
    ({"name": "x", "from": "harvlab", "to": "harvlab2", "rpo": "15m"}, "bad-selection"),
    ({"name": "x", "from": "harvlab", "to": "harvlab2", "rpo": "15m", "vms": "default/a"}, None),
    ({"name": "x", "from": "harvlab", "to": "harvlab2", "rpo": "15m", "vms": ["default/a"], "map_net": {"a=b": "c"}},
     None),
    ({"name": "x", "from": "harvlab", "to": "harvlab2", "rpo": "15m", "vms": ["default/a"], "extra": 1}, None),
    ({"name": "x", "from": "harvlab", "to": "harvlab2", "rpo": "15m", "vms": ["default/a"],
      "create_namespace": "yes"}, None),
])
def test_create_refusals_answer_400_without_an_action(client, body, code):
    r = client.post("/api/replication/plans", json=body)
    assert r.status_code == 400 and r.get_json().get("code") == code
    assert not wapp.ACTIONS


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_plan_names_never_reach_a_label(client, name):
    r = client.post("/api/replication/plans", json={"name": name, "from": "harvlab", "to": "harvlab2",
                                                    "rpo": "15m", "vms": ["default/a"]})
    assert r.status_code == 400 and not wapp.ACTIONS


@pytest.mark.parametrize("vm", HOSTILE)
def test_hostile_vm_names_stay_one_argument_and_out_of_the_label(client, env, vm):
    r = client.post(f"/api/replication/plans/{PID}/failover", json={"vms": [vm], "dry_run": True})
    assert r.status_code == 202
    cmd = script_cmd()
    assert cmd.count(f"--vm={vm}") == 1 and cmd.count(vm) == (1 if vm == "--json" else 0)
    assert only_run().action == f"replication:{PID}:failover"


def test_every_replication_label_is_built_from_checked_tokens():
    labels = re.findall(r'label = f"(replication:[^"]+)"', SRC) + re.findall(r'f"(replication:create:[^"]+)"', SRC)
    assert labels == ["replication:{plan['id']}:{op}", "replication:create:{plan['name']}"]
    assert "plan, argv, recovery = _repl_create_request(body)" in SRC          # le nom passe par normalize_plan


@pytest.mark.parametrize("path,body,code", [
    (f"/api/replication/plans/{PID}/failover", {"vms": ["default/rep-a"]}, "confirmation-required"),
    (f"/api/replication/plans/{PID}/failover", {"vms": ["default/rep-a"], "confirm": "WEB"}, "confirmation-required"),
    (f"/api/replication/plans/{PID}/failover", {"all_vms": True, "mode": "unplanned"}, "confirmation-required"),
    (f"/api/replication/plans/{PID}/failover", {"confirm": "web"}, "bad-selection"),
    (f"/api/replication/plans/{PID}/failover", {"vms": ["default/rep-a"], "all_vms": True, "confirm": "web"},
     "bad-selection"),
    (f"/api/replication/plans/{PID}/failover", {"vms": ["default/rep-a"], "accept_skew": True, "confirm": "web"},
     "bad-option"),
    (f"/api/replication/plans/{PID}/failover", {"vms": ["default/rep-a"], "force_unplanned": True,
                                                "confirm": "web"}, "bad-option"),
    (f"/api/replication/plans/{PID}/failover", {"vms": ["default/rep-a"], "mode": "now", "confirm": "web"}, None),
    (f"/api/replication/plans/{PID}/unprotect", {}, "confirmation-required"),
    (f"/api/replication/plans/{PID}/unprotect", {"delete_backups": True, "confirm": "nope"}, "confirmation-required"),
    (f"/api/replication/plans/{PID}/unprotect", {"delete_backups": "yes", "confirm": "web"}, None),
    (f"/api/replication/plans/{PID}/measure", {"op": "go"}, None),
    (f"/api/replication/plans/{PID}/measure", {"op": "stop", "interval": "5m"}, None),
    (f"/api/replication/plans/{PID}/check", {"phase": "drill"}, None),
    (f"/api/replication/plans/{PID}/protect", {"dry_run": 1}, None),
    (f"/api/replication/plans/{PID}/protect", {"interval": "5m\n--json"}, None),
    ("/api/replication/plans/rp-ffffffffffff/protect", {}, "unknown-plan"),
])
def test_refusals_answer_before_any_action(client, path, body, code):
    r = client.post(path, json=body)
    assert r.status_code in (400, 404) and r.get_json().get("code") == code
    assert not wapp.ACTIONS


@pytest.mark.parametrize("body,code", [({"interval": "5m"}, "not-settable"), ({"retain": 3}, "not-settable"),
                                       ({}, None), ({"follow": "always"}, None), ({"paused": "true"}, None),
                                       ({"colour": "red"}, None), ({"timeouts": {"Bad-Name": 3}}, None)])
def test_settings_refusals(client, body, code):
    r = client.patch(f"/api/replication/plans/{PID}/settings", json=body)
    assert r.status_code == 400 and r.get_json().get("code") == code and not wapp.ACTIONS
    if code == "not-settable":
        assert "protect the plan again" in r.get_json()["error"]


@pytest.mark.parametrize("retain", [0, 10_000, "9", True, 2.5])
def test_protect_refuses_a_bad_retain_before_any_action(client, retain):
    r = client.post(f"/api/replication/plans/{PID}/protect", json={"retain": retain})
    assert r.status_code == 400 and "retain" in r.get_json()["error"] and not wapp.ACTIONS


def test_a_cluster_this_session_cannot_see_is_refused_never_replaced(client, env):
    env["access"].pop("harvlab2")
    r = client.post(f"/api/replication/plans/{PID}/protect", json={})
    assert r.status_code == 403 and r.get_json()["code"] == "target-denied" and not wapp.ACTIONS
    env["access"]["harvlab2"] = KC["harvlab2"]
    env["access"].pop("harvlab")
    r = client.post(f"/api/replication/plans/{PID}/protect", json={})
    assert r.status_code == 403 and r.get_json()["code"] == "source-denied"


def test_a_source_no_longer_declared_gets_no_kubeconfig(client, env, monkeypatch):
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"clusters": [{"name": "harvlab2", "kubeconfig": "x"}]})
    r = client.post(f"/api/replication/plans/{PID}/failover",
                    json={"all_vms": True, "mode": "unplanned", "confirm": "web"})
    assert r.status_code == 202
    cmd = script_cmd()
    assert f"--to-kubeconfig={KC['harvlab2']}" in cmd and not any(a.startswith("--from-kubeconfig") for a in cmd)


def _running(action, cluster, dry_run=False):
    run = wapp.ActionRun(f"r{len(wapp.ACTIONS)}", action, cluster, ["x"], dry_run=dry_run)
    run.status = "running"
    wapp.ACTIONS[run.id] = run
    return run


@pytest.mark.parametrize("busy,blocked", [
    (("shutdown", "harvlab"), True), (("startup", "harvlab2"), True), (("shutdown", "harvlab3"), False),
    ((f"replication:{PID}:failover", "harvlab2"), True), ((f"replication:{PID2}:protect", "harvlab2"), False),
    ((f"replication:{PID}:check", "harvlab"), False),                  # contrôle : essai, ne bloque rien
])
def test_busy_on_either_cluster_or_on_the_plan(client, busy, blocked):
    _running(*busy, dry_run=busy[0].endswith(":check"))
    r = client.post(f"/api/replication/plans/{PID}/protect", json={})
    assert r.status_code == (409 if blocked else 202)


def test_a_check_and_a_dry_run_are_not_blocked_by_a_running_failover(client):
    _running(f"replication:{PID}:failover", "harvlab2")
    assert client.post(f"/api/replication/plans/{PID}/check", json={}).status_code == 202
    assert client.post(f"/api/replication/plans/{PID}/protect", json={"dry_run": True}).status_code == 202
    assert client.patch(f"/api/replication/plans/{PID}/settings", json={"rpo": "1h"}).status_code == 409


def test_by_carries_the_person(as_user, env):
    r = as_user("alice", "POST", f"/api/replication/plans/{PID}/protect", json={})
    assert r.status_code == 202
    assert script_cmd() == base(env, "protect", PID, user="alice") + KUBE + [f"--id={only_run().id}"]


# --- rôles ----------------------------------------------------------------------------

ROLE_ROUTES = [
    ("GET", "/api/replication/plans", None, "viewer"),
    ("GET", f"/api/replication/plans/{PID}", None, "viewer"),
    ("GET", f"/api/replication/plans/{PID}/stats", None, "viewer"),
    ("GET", "/api/replication/hosts/harvlab/resources", None, "viewer"),
    ("GET", "/api/replication/resources/u-a/events", None, "viewer"),
    ("GET", "/api/replication/follower", None, "viewer"),
    ("POST", "/api/replication/plans", {"name": "w3", "from": "harvlab", "to": "harvlab2", "rpo": "15m",
                                        "vms": ["default/a"]}, "operator"),
    ("POST", f"/api/replication/plans/{PID}/check", {}, "operator"),
    ("POST", f"/api/replication/plans/{PID}/measure", {"op": "start"}, "operator"),
    ("POST", f"/api/replication/plans/{PID}/protect", {}, "operator"),
    ("POST", f"/api/replication/plans/{PID}/resume", {}, "operator"),
    ("POST", f"/api/replication/plans/{PID}/unprotect", {"confirm": "web"}, "operator"),
    ("PATCH", f"/api/replication/plans/{PID}/settings", {"rpo": "1h"}, "operator"),
    ("DELETE", f"/api/replication/plans/{PID2}", None, "operator"),
    ("POST", f"/api/replication/plans/{PID}/failover", {"vms": ["default/rep-a"], "confirm": "web"}, "admin"),
    ("POST", f"/api/replication/plans/{PID}/failover", {"vms": ["default/rep-a"], "dry_run": True}, "admin"),
    ("POST", f"/api/replication/plans/{PID}/unprotect", {"confirm": "web", "delete_backups": True}, "admin"),
    # écrans de suivi
    ("GET", f"/api/replication/plans/{PID}/series", None, "viewer"),
    ("GET", "/api/replication/alerts", None, "viewer"),
    ("GET", "/api/replication/links", None, "viewer"),
    ("GET", "/api/replication/notify", None, "operator"),
    ("PUT", "/api/replication/notify", {"enabled": True, "channels": []}, "admin"),
    ("POST", "/api/replication/notify/test", {"channel": "c1"}, "admin"),
    ("POST", "/api/replication/links/test", {"from": "harvlab", "to": "harvlab2", "size_mib": 64}, "operator"),
]
USERS = {"curieux": "viewer", "alice": "operator", "patronne": "admin"}


@pytest.mark.parametrize("method,path,body,need", ROLE_ROUTES)
@pytest.mark.parametrize("user", list(USERS))
def test_roles_per_route(as_user, user, method, path, body, need):
    wapp.ACTIONS.clear()
    r = as_user(user, method, path, json=body) if body is not None else as_user(user, method, path)
    ok = wapp.ROLE_RANK[USERS[user]] >= wapp.ROLE_RANK[need]
    if ok:
        assert r.status_code in (200, 202), (user, path, r.get_json())
    else:
        d = r.get_json()
        assert r.status_code == 403 and not wapp.ACTIONS
        # la suppression des sauvegardes se contrôle dans la route : un lecteur
        # bute d'abord sur le garde central (operator)
        assert d["required"] == ("operator" if USERS[user] == "viewer" and "/unprotect" in path else need)


@pytest.mark.parametrize("op,body,need", [
    ("protect", {}, "operator"), ("failover", {"vms": ["default/rep-a"], "confirm": "web"}, "admin"),
    ("unprotect", {"confirm": "web", "delete_backups": True}, "admin"), ("unprotect", {"confirm": "web"}, "operator")])
def test_cancelling_needs_the_role_that_started_it(as_user, op, body, need):
    as_user("patronne", "POST", f"/api/replication/plans/{PID}/{op}", json=body)
    run = only_run()
    assert run.required_role == need
    r = as_user("alice", "DELETE", f"/api/action/{run.id}")
    assert r.status_code == (200 if need == "operator" else 403)


def test_every_replication_route_requires_auth_and_mutations_have_a_valid_rate_limit():
    from limits import parse_many
    blocks = re.split(r"\n(?=@app\.route\()", SRC)
    routes = [b for b in blocks if b.startswith('@app.route("/api/replication/')]
    assert len(routes) == 26     # 2.0.0 : route d'hôte avec sa famille (point 9)
    for b in routes:
        head = b.split("\ndef ", 1)[0]
        assert "@requires_auth" in head, head
        if "methods=" in head:
            [spec] = re.findall(r'@_rate_limit\("([^"]+)"\)', head)
            assert parse_many(spec), head
        else:
            # suiveur et réglage des notifications : jamais en lecture partagée
            assert "@_rate_limit" not in head and "@shared_read(scope=REPLICATION_SCOPE)" in head \
                or "/follower" in head or "/candidates/" in head or '"/api/replication/notify"' in head


# --- relais : statut et résumé ----------------------------------------------------------

def _run_tool(rc, data, summarize=wapp._repl_summarize, stderr=""):
    code = (f"import json,sys; sys.stderr.write({stderr!r}); "
            f"print(json.dumps({json.dumps(data)!r} and json.loads({json.dumps(data)!r}))); sys.exit({rc})")
    run = wapp.ActionRun("t1", f"replication:{PID}:failover", "harvlab2", ["harvester-replication"])
    wapp._vm_transfer_runner(run, [sys.executable, "-c", code], capture=True, summarize=summarize)
    return run


@pytest.mark.parametrize("rc,data,status,summary", [
    (0, {"ok": True}, "done", None),
    (3, {"ok": False, "kind": "cancelled", "code": "cancelled", "resume_required": False, "left": []},
     "cancelled", "cancelled, changes of this run undone"),
    (3, {"ok": False, "kind": "cancelled", "resume_required": False, "left": ["vm default/rep-a (not restarted)"]},
     "cancelled", "cancelled, changes of this run undone; left: vm default/rep-a (not restarted)"),
    (3, {"ok": False, "kind": "interrupted", "code": "interrupted", "resume_required": True},
     "cancelled", "interrupted, nothing undone: resume the plan to finish"),
    (1, {"ok": False, "kind": "interrupted", "code": "interrupted", "resume_required": True, "error": "vm refused"},
     "error", "interrupted (vm refused), nothing undone: resume the plan to finish"),
    (1, {"ok": False, "kind": "undo-timeout", "code": "undo-timeout", "plan": PID, "left": ["volume x"]},
     "error", f"undo deadline reached, clean up by hand what carries harvester-ops.io/replication-plan={PID}; "
              "left: volume x"),
    (2, {"ok": False, "kind": "state-changed", "error": "the plan changed while waiting for its lock"},
     "error", "the plan changed while waiting for its lock"),
    (3, {}, "cancelled", "cancelled, no result from the command: check the plan"),
    # décision 68 : arrêt de la console, pas une annulation
    (3, {"ok": False, "kind": "interrupted", "code": "interrupted", "resume_required": True, "suspended": True,
         "step": "initial-restore"},
     "interrupted", "interrupted by the console shutdown at step initial-restore, nothing undone: "
                    "resume the plan to continue"),
    (3, {"ok": False, "kind": "not-started", "code": "not-started", "resume_required": False, "suspended": True},
     "interrupted", "stopped by the console shutdown before any change: run the command again"),
])
def test_runner_wording_follows_the_kind(rc, data, status, summary):
    run = _run_tool(rc, data)
    assert (run.status, run.error_summary, run.exit_code) == (status, summary, rc)
    assert run.result.get("resume_required", False) is bool(data.get("resume_required"))


def test_other_tools_keep_their_texts():
    run = _run_tool(3, {"ok": False}, summarize=None)
    assert (run.status, run.error_summary) == ("cancelled", "cancelled, transfer undone")
    run = _run_tool(1, {}, summarize=None, stderr="STEP_EVENT|x|error|boom\n")
    assert (run.status, run.error_summary) == ("error", "boom")


# --- délai de retour arrière avant SIGKILL ------------------------------------------------

# enfants de test : SIGTERM (annulation) et SIGUSR2 (arrêt de la console,
# décision 68) ignorés, ou suivis d'une sortie 3 ; le signal reçu est écrit
STUBBORN = ("import signal,sys,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "signal.signal(signal.SIGUSR2, signal.SIG_IGN)\n"
            "print('ready', flush=True)\ntime.sleep(30)\n")
POLITE = ("import signal,sys,time\ndef h(n, f):\n    print(signal.Signals(n).name, flush=True); "
          "time.sleep(0.5); sys.exit(3)\n"
          "signal.signal(signal.SIGTERM, h)\nsignal.signal(signal.SIGUSR2, h)\n"
          "print('ready', flush=True)\ntime.sleep(30)\n")


def _child(code):
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    return p


def test_grace_is_undo_total_plus_client_plus_margin():
    assert wapp._repl_grace({"options": {}}) == 240 + 30 + 15
    assert wapp._repl_grace({"options": {"timeouts": {"undo_total": 600, "client": 60}}}) == 675


@pytest.mark.parametrize("code,grace,want_rc", [(STUBBORN, 1, -9), (POLITE, 5, 3)])
def test_cancel_kills_only_after_the_grace(code, grace, want_rc):
    run = wapp.ActionRun("g1", f"replication:{PID}:protect", "harvlab", ["x"])
    run.proc, run.kill_grace = _child(code), grace
    t0 = time.monotonic()
    run.proc.terminate()
    wapp._kill_after_grace(run).join(10)
    took = time.monotonic() - t0
    assert run.proc.returncode == want_rc
    assert (took >= grace) if want_rc == -9 else (took < grace)


def test_cancel_route_escalates_only_for_actions_with_a_grace(client, monkeypatch):
    seen = []
    monkeypatch.setattr(wapp, "_kill_after_grace", lambda run: seen.append(run.id))
    for rid, grace in (("a1", 30), ("a2", None)):
        run = wapp.ActionRun(rid, "x", "harvlab", ["x"])
        run.proc, run.kill_grace, run.status = _child(POLITE), grace, "running"
        wapp.ACTIONS[rid] = run
        assert client.delete(f"/api/action/{rid}").status_code == 200
        run.proc.wait(5)
    assert seen == ["a1"]


def test_console_shutdown_waits_each_grace_then_kills(monkeypatch):
    monkeypatch.setattr(wapp, "REPLICATION_FOLLOWER", None)
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    try:
        runs = {}
        for rid, code, grace in (("s1", STUBBORN, 1), ("s2", POLITE, 20), ("s3", POLITE, None)):
            run = wapp.ActionRun(rid, "x", "harvlab", ["x"])
            run.proc, run.kill_grace, run.status = _child(code), grace, "done"
            wapp.ACTIONS[rid] = runs[rid] = run
        t0 = time.monotonic()
        codes = wapp._replication_shutdown()
        assert codes == {"s1": -9, "s2": 3} and 1 <= time.monotonic() - t0 < 10
        assert runs["s3"].proc.poll() is None          # pas une commande de réplication : intacte
        runs["s3"].proc.kill()
        runs["s3"].proc.wait()
    finally:
        wapp.ACTIONS.clear()
        wapp.ACTIONS.update(saved)


@pytest.mark.parametrize("plan,op,want", [
    ({"state": "protecting", "options": {}}, "protect", 30 + 15),
    ({"state": "measuring", "options": {"timeouts": {"client": 60}}}, "measure-stop", 60 + 15),
    ({"state": "draft", "options": {}}, "measure-start", 30 + 15),
    ({"state": "unprotecting", "options": {}}, "unprotect", 30 + 15),
    ({"state": "protecting", "options": {}}, "resume", 30 + 15),
    ({"state": "error", "error_origin": "protecting", "options": {}}, "resume", 30 + 15),
    ({"state": "protected", "options": {}}, "failover", 240 + 30 + 15),
    ({"state": "failing_over", "options": {}}, "resume", 240 + 30 + 15),
    ({"state": "error", "error_origin": "failing_over", "options": {"timeouts": {"undo_total": 100}}}, "resume",
     100 + 30 + 15),
])
def test_shutdown_grace_is_a_safe_point_except_for_a_failover(plan, op, want):
    """Décision 68 : une protection, une mesure, une déprotection (et leur
    reprise) ne sont plus défaites à l'arrêt de la console ; seule une
    bascule pas encore activée garde son délai de retour arrière."""
    assert wapp._repl_shutdown_grace(plan, op) == want


def _readlines_until(proc, n=1, timeout=5):
    out, end = [], time.monotonic() + timeout
    while len(out) < n and time.monotonic() < end:
        line = proc.stdout.readline()
        if not line:
            break
        out.append(line.strip())
    return out


def test_console_shutdown_suspends_running_commands_and_lets_a_cancel_finish(monkeypatch):
    """L'arrêt de la console envoie SIGUSR2 (« arrête au point sûr, garde
    l'état ») à une commande en cours, jamais SIGTERM, et ne la marque pas
    annulée ; une annulation déjà demandée par une personne (SIGTERM déjà
    reçu, retour arrière en cours) ne reçoit rien de plus et garde son délai
    de retour arrière. Le délai d'arrêt (`shutdown_grace`) remplace le délai
    de retour arrière pour les autres : un enfant qui ne s'arrête pas est
    tué après lui, pas après `kill_grace`."""
    monkeypatch.setattr(wapp, "REPLICATION_FOLLOWER", None)
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    try:
        runs = {}
        for rid, code, cancelled, grace, kill in (("p1", POLITE, False, 5, 300), ("p2", POLITE, True, None, 20),
                                                 ("p3", STUBBORN, False, 1, 300)):
            run = wapp.ActionRun(rid, f"replication:{PID}:protect", "harvlab", ["x"])
            run.proc, run.kill_grace, run.shutdown_grace, run.status = _child(code), kill, grace, "done"
            if cancelled:
                run.cancel_requested = True
                run.proc.terminate()                      # l'annulation du dock, avant l'arrêt
            wapp.ACTIONS[rid] = runs[rid] = run
        t0 = time.monotonic()
        codes = wapp._replication_shutdown()
        took = time.monotonic() - t0
        assert codes == {"p1": 3, "p2": 3, "p3": -9}
        assert 1 <= took < 10                             # p3 tué après son délai d'arrêt (1 s), pas 300 s
        assert _readlines_until(runs["p1"].proc) == ["SIGUSR2"]
        assert _readlines_until(runs["p2"].proc) == ["SIGTERM"]       # rien de plus
        assert runs["p1"].cancel_requested is False and runs["p1"].shutdown_requested is True
        assert runs["p2"].shutdown_requested is False
    finally:
        wapp.ACTIONS.clear()
        wapp.ACTIONS.update(saved)


def test_sigterm_handler_goes_through_the_replication_shutdown():
    assert "signal.signal(signal.SIGTERM, _console_sigterm)" in SRC
    assert "def _console_sigterm(signum, frame):\n    try:\n        _replication_shutdown()" in SRC


def test_service_unit_waits_long_enough_at_stop():
    unit = (ROOT / "config" / "systemd" / "harvester-ops.service").read_text()
    stop = int(re.search(r"podman stop -t (\d+)", unit).group(1))
    timeout = int(re.search(r"TimeoutStopSec=(\d+)s", unit).group(1))
    assert stop >= wapp._repl_grace({"options": {}}) and timeout > stop
    assert "HARVESTER_OPS_REPLICATION_FOLLOW=$${HARVESTER_OPS_REPLICATION_FOLLOW:-1}" in unit


# --- fil de suivi -------------------------------------------------------------------------

class FakeProc:
    n = 0

    def __init__(self, obey=True):
        FakeProc.n += 1
        self.pid, self.obey, self.rc, self.terms, self.killed, self.waits = 1000 + FakeProc.n, obey, None, 0, 0, []

    def poll(self):
        return self.rc

    @property
    def returncode(self):
        return self.rc

    def terminate(self):
        self.terms += 1
        if self.obey:
            self.rc = 0

    def kill(self):
        self.killed += 1
        self.rc = -9

    def send_signal(self, sig):
        self.signals = getattr(self, "signals", []) + [sig]

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.rc is None:
            raise subprocess.TimeoutExpired("follow", timeout)
        return self.rc


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def sup(tmp_path):
    clock, procs, flags = Clock(), [], {"on": True, "obey": True, "fail": False}
    log = tmp_path / "logs" / "replication-follow.log"

    def popen(cmd, **kw):
        if flags["fail"]:
            raise OSError("no python")
        kw["stderr"].write("2026-10-24T10:00:00Z info round: 0 plan(s)\n")
        p = FakeProc(flags["obey"])
        p.cmd, p.kw = cmd, kw
        procs.append(p)
        return p
    s = wapp.ReplicationFollowerSupervisor(lambda: ["follow", "--all", "--console"], lambda: log,
                                           lambda: flags["on"], lambda: 42, popen=popen, clock=clock,
                                           wall=lambda: 5.0)
    return {"s": s, "clock": clock, "procs": procs, "flags": flags, "log": log}


def test_follower_starts_once_and_writes_its_journal_to_the_log(sup):
    s, procs = sup["s"], sup["procs"]
    s.tick()
    s.tick()
    assert len(procs) == 1 and procs[0].cmd == ["follow", "--all", "--console"]
    assert procs[0].kw["start_new_session"] is True and procs[0].kw["stdout"] is subprocess.DEVNULL
    h = s.health()
    assert h["running"] and h["pid"] == procs[0].pid and h["state"] == "running" and h["restarts"] == 0
    assert "round: 0 plan(s)" in sup["log"].read_text()


def test_follower_restarts_with_a_growing_backoff_then_resets_after_a_stable_run(sup):
    s, procs, clock = sup["s"], sup["procs"], sup["clock"]
    s.tick()
    delays = []
    for _ in range(4):
        procs[-1].rc = 1
        s.tick()
        start = clock.t
        assert s.health()["state"] == "backoff" and s.health()["last_exit"] == 1
        n = len(procs)
        while len(procs) == n:
            clock.t += 1
            s.tick()
        delays.append(clock.t - start)
    assert delays == [5, 15, 30, 60] and s.restarts == 4
    assert s.last_error.startswith("exit 1: ") and "round: 0 plan(s)" in s.last_error
    clock.t += s.STABLE_S
    procs[-1].rc = -9
    s.tick()
    assert s.health()["next_restart_in_s"] == 5


def test_an_unrequested_clean_exit_is_restarted_but_a_requested_stop_is_not(sup):
    s, procs, clock = sup["s"], sup["procs"], sup["clock"]
    s.tick()
    procs[-1].rc = 0                 # SIGTERM venu d'ailleurs : le suiveur sort en 0
    s.tick()
    clock.t += 5
    s.tick()
    assert len(procs) == 2
    s.stop()
    assert procs[-1].terms == 1 and procs[-1].waits == [42] and procs[-1].killed == 0
    for _ in range(5):
        clock.t += 400
        s.tick()
    assert len(procs) == 2 and s.health()["state"] == "stopped" and s.restarts == 1


def test_follower_stop_kills_after_the_grace_when_it_does_not_exit(sup):
    s, procs = sup["s"], sup["procs"]
    sup["flags"]["obey"] = False
    s.tick()
    s.stop()
    assert procs[0].waits[0] == 42 and procs[0].killed == 1 and "killed" in s.last_error


def test_kill_switch_stops_gracefully_and_restarts_at_once_when_back(sup):
    s, procs, flags = sup["s"], sup["procs"], sup["flags"]
    s.tick()
    flags["on"] = False
    s.tick()
    assert procs[0].terms == 1 and procs[0].waits == [42] and s.health()["state"] == "disabled"
    s.tick()
    assert len(procs) == 1 and not s.health()["enabled"]
    flags["on"] = True
    s.tick()
    assert len(procs) == 2 and s.restarts == 0


def test_a_spawn_failure_is_retried_with_backoff(sup):
    s, procs, clock, flags = sup["s"], sup["procs"], sup["clock"], sup["flags"]
    flags["fail"] = True
    s.tick()
    assert s.health()["state"] == "backoff" and "OSError" in s.last_error
    flags["fail"] = False
    clock.t += 4
    s.tick()
    assert not procs
    clock.t += 1
    s.tick()
    assert len(procs) == 1


@pytest.mark.parametrize("env_value,config,on", [
    ("1", {}, True), ("0", {}, False), ("false", {}, False), ("1", {"replication": {"follow": False}}, False),
    ("1", {"replication": {"follow": True}}, True), ("1", {"replication": "junk"}, True)])
def test_follow_switches(monkeypatch, env_value, config, on):
    monkeypatch.setenv("HARVESTER_OPS_REPLICATION_FOLLOW", env_value)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: config)
    assert wapp._replication_follow_enabled() is on


def test_the_follower_thread_starts_once_and_never_in_a_reader(monkeypatch):
    started = []
    monkeypatch.setattr(wapp.ReplicationFollowerSupervisor, "start", lambda self: started.append(self))
    monkeypatch.setattr(wapp, "REPLICATION_FOLLOWER", None)
    monkeypatch.setenv("HARVESTER_OPS_REPLICATION_FOLLOW", "0")
    assert wapp._start_replication_follower() is None
    monkeypatch.setenv("HARVESTER_OPS_REPLICATION_FOLLOW", "1")
    monkeypatch.setattr(wapp, "IS_READ_WORKER", True)
    assert wapp._start_replication_follower() is None
    monkeypatch.setattr(wapp, "IS_READ_WORKER", False)
    a = wapp._start_replication_follower()
    assert wapp._start_replication_follower() is a and started == [a]


def test_follower_command_follows_the_console_plans_with_the_console_state(monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "_state_dir", lambda: tmp_path)
    monkeypatch.delenv("HARVESTER_OPS_REPLICATION_DB", raising=False)
    monkeypatch.setattr(wapp, "LOG_DIR", tmp_path / "logs")
    assert wapp._replication_follow_cmd() == [sys.executable, SCRIPT, "follow", "--all", "--console", "--json",
                                              f"--state-dir={tmp_path}", f"--db={tmp_path / 'replication.db'}",
                                              f"--log-file={tmp_path / 'logs' / 'replication-follow.log'}"]


def test_follower_grace_is_the_longest_client_timeout(env):
    assert wapp._replication_follow_grace() == 30 + 15
    conn = db.connect(env["db"])
    db.update_plan(conn, PID2, options={"timeouts": {"client": 90}})
    conn.close()
    assert wapp._replication_follow_grace() == 90 + 15


@pytest.mark.skipif(not wapp._PROMETHEUS_AVAILABLE, reason="prometheus_client absent")
def test_metrics_expose_the_follower_and_the_rpo_per_plan(client, env, monkeypatch):
    from prometheus_client import REGISTRY
    fake = type("S", (), {"running": lambda self: True})()
    monkeypatch.setattr(wapp, "REPLICATION_FOLLOWER", fake)
    text = client.get("/metrics").get_data(as_text=True)
    assert "harvester_ops_replication_follower_up 1.0" in text
    labels = {"plan": PID, "source": "harvlab", "target": "harvlab2"}
    assert 'name="web"' not in text and 'name="db"' not in text          # jamais un nom de plan
    assert REGISTRY.get_sample_value("harvester_ops_replication_rpo_objective_seconds", labels) == 900
    assert REGISTRY.get_sample_value("harvester_ops_replication_rpo_held_seconds", labels) > 900
    assert REGISTRY.get_sample_value("harvester_ops_replication_plans_followed") == 1    # web suivi, db brouillon
    conn = db.connect(env["db"])
    db.update_plan(conn, PID, options={"paused": True})
    conn.close()
    # v1.99.0 : relevé gardé REPLICATION_METRICS_TTL secondes
    client.get("/metrics")
    assert REGISTRY.get_sample_value("harvester_ops_replication_plans_followed") == 1
    wapp._replication_metrics_invalidate()
    client.get("/metrics")
    assert REGISTRY.get_sample_value("harvester_ops_replication_plans_followed") == 0


def test_follower_route_reports_off_without_a_supervisor(client, monkeypatch):
    monkeypatch.setattr(wapp, "REPLICATION_FOLLOWER", None)
    d = client.get("/api/replication/follower").get_json()
    assert d["state"] == "off" and d["running"] is False and d["pid"] is None


# --- corrections après relecture --------------------------------------------------------

def _plan_state(env, state, origin="", run=None):
    """Pose l'état du plan `web` et, au besoin, une ligne `runs` :
    run = (opération, points, terminée, génération)."""
    conn = db.connect(env["db"])
    conn.execute("UPDATE plans SET state = ?, error_origin = ? WHERE id = ?", (state, origin, PID))
    conn.commit()
    if run:
        op, points, finished, gen = run
        db.record_run(conn, PID, op, now_utc(), max(gen, 1), finished_at=now_utc() if finished else None,
                      points=points)
        if gen == 0:                         # ligne d'une génération passée
            conn.execute("UPDATE plans SET generation = 2 WHERE id = ?", (PID,))
            conn.commit()
    conn.close()


UNPROTECT_DEL = ("unprotect", {"delete_backups": True, "held": {}}, False, 1)
RESUME_ROLES = [
    # état, origine de l'erreur, ligne `runs`, rôle exigé
    ("protecting", "", None, "operator"),
    ("error", "protecting", None, "operator"),
    ("error", "measuring", None, "operator"),
    ("failing_over", "", ("failover", {"mode": "planned"}, False, 1), "admin"),
    ("error", "failing_over", ("failover", {"mode": "unplanned"}, False, 1), "admin"),
    ("error", "failing_over", None, "admin"),                    # même sans ligne : c'est une bascule
    ("unprotecting", "", UNPROTECT_DEL, "admin"),
    ("error", "unprotecting", UNPROTECT_DEL, "admin"),
    ("unprotecting", "", ("unprotect", {"delete_backups": False, "held": {}}, False, 1), "operator"),
    ("unprotecting", "", ("unprotect", {"delete_backups": True}, True, 1), "operator"),       # terminée
    ("unprotecting", "", ("unprotect", {"delete_backups": True}, False, 0), "operator"),      # autre génération
]


@pytest.mark.parametrize("state,origin,run,need", RESUME_ROLES)
@pytest.mark.parametrize("user", ["alice", "patronne"])
def test_resume_needs_the_role_of_the_gesture_it_finishes(as_user, env, user, state, origin, run, need):
    _plan_state(env, state, origin, run)
    r = as_user(user, "POST", f"/api/replication/plans/{PID}/resume", json={})
    if wapp.ROLE_RANK[USERS[user]] < wapp.ROLE_RANK[need]:
        assert r.status_code == 403 and r.get_json()["required"] == need and not wapp.ACTIONS
        return
    assert r.status_code == 202, r.get_json()
    assert r.get_json()["role"] == need
    run_ = only_run()
    assert run_.required_role == need
    # annuler la reprise demande le même rôle
    c = as_user("alice", "DELETE", f"/api/action/{run_.id}")
    assert c.status_code == (200 if need == "operator" else 403)


@pytest.mark.parametrize("user,body,status,code,flag", [
    ("alice", {"confirm": "web"}, 403, None, False),
    ("alice", {"confirm": "web", "delete_backups": True}, 403, None, False),
    ("patronne", {"confirm": "web"}, 400, "needs-admin-resume", False),
    ("patronne", {"confirm": "web", "delete_backups": True}, 202, None, True),
])
def test_unprotect_inheriting_a_backup_deletion_needs_an_explicit_admin(as_user, env, user, body, status, code,
                                                                         flag):
    _plan_state(env, "unprotecting", "", UNPROTECT_DEL)
    r = as_user(user, "POST", f"/api/replication/plans/{PID}/unprotect", json=body)
    assert r.status_code == status, r.get_json()
    if status == 403:
        assert r.get_json()["required"] == "admin" and not wapp.ACTIONS
    elif status == 400:
        assert r.get_json()["code"] == code and not wapp.ACTIONS
    else:
        assert ("--delete-backups" in script_cmd()) is flag and only_run().required_role == "admin"


def test_unprotect_without_an_open_deletion_stays_operator(as_user, env):
    _plan_state(env, "unprotecting", "", ("unprotect", {"delete_backups": False}, False, 1))
    r = as_user("alice", "POST", f"/api/replication/plans/{PID}/unprotect", json={"confirm": "web"})
    assert r.status_code == 202 and "--delete-backups" not in script_cmd()
    assert only_run().required_role == "operator"


# --- fil de suivi : jamais à l'import, un seul par répertoire d'état -------------------

IMPORT_PROBE = """
import importlib.util, sys, threading
spec = importlib.util.spec_from_file_location("bm", sys.argv[1])
bm = importlib.util.module_from_spec(spec); spec.loader.exec_module(bm)
app = bm._load_web_module("app")
import time; time.sleep(0.3)
names = [t.name for t in threading.enumerate()]
print(app.REPLICATION_FOLLOWER is None, "replication-follower" in names)
"""


def test_importing_the_console_starts_no_follower(tmp_path):
    """Comme bin/harvester-baremetal.py, qui charge web/app.py : même avec
    l'interrupteur à 1, aucun fil ni processus de suivi."""
    env = dict(os.environ, HARVESTER_OPS_REPLICATION_FOLLOW="1", HARVESTER_OPS_STATE_DIR=str(tmp_path),
               HARVESTER_OPS_REPLICATION_DB=str(tmp_path / "replication.db"), HARVESTER_OPS_WATCH="0",
               HARVESTER_OPS_READ_WORKERS="0", HARVESTER_OPS_WEB_DIR=str(ROOT / "web"))
    p = subprocess.run([sys.executable, "-c", IMPORT_PROBE, str(ROOT / "bin" / "harvester-baremetal.py")],
                       capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))
    assert p.returncode == 0, p.stderr[-2000:]
    assert p.stdout.split()[-2:] == ["True", "False"], p.stdout
    assert not (tmp_path / "replication-follow.lock").exists()


def test_the_follower_starts_only_from_the_server_entry_point():
    main = SRC.split('if __name__ == "__main__":', 1)
    assert "_start_replication_follower()" in main[1]
    calls = [ln for ln in main[0].splitlines() if ln.strip() == "_start_replication_follower()"]
    assert calls == []                                       # aucun appel au niveau du module
    bm = (ROOT / "bin" / "harvester-baremetal.py").read_text()
    assert bm.index('os.environ["HARVESTER_OPS_REPLICATION_FOLLOW"] = "0"') < bm.index('_load_web_module("app")\n')


HOLD_LOCK = """
import sys
sys.path.insert(0, sys.argv[1])
import os
os.environ["HARVESTER_OPS_REPLICATION_FOLLOW"] = "0"
import app
fd, holder = app._replication_follow_lock(sys.argv[2])
print("held" if fd is not None else "busy", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def follower_globals(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(wapp.ReplicationFollowerSupervisor, "start", lambda self: started.append(self))
    monkeypatch.setattr(wapp, "REPLICATION_FOLLOWER", None)
    monkeypatch.setattr(wapp, "REPLICATION_FOLLOWER_ELSEWHERE", None)
    monkeypatch.setattr(wapp, "_REPLICATION_FOLLOW_LOCK_FD", None)
    monkeypatch.setattr(wapp, "IS_READ_WORKER", False)
    monkeypatch.setenv("HARVESTER_OPS_REPLICATION_FOLLOW", "1")
    monkeypatch.setattr(wapp, "_state_dir", lambda: tmp_path)
    yield started
    if wapp._REPLICATION_FOLLOW_LOCK_FD is not None:
        os.close(wapp._REPLICATION_FOLLOW_LOCK_FD)


def test_a_second_console_on_the_same_state_dir_starts_no_follower(follower_globals, tmp_path):
    holder = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, str(ROOT / "web"),
                               str(tmp_path / "replication-follow.lock")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                              env=dict(os.environ, HARVESTER_OPS_WATCH="0", HARVESTER_OPS_READ_WORKERS="0"))
    try:
        assert holder.stdout.readline().strip() == "held"
        assert wapp._start_replication_follower() is None and follower_globals == []
        h = wapp._replication_follower_health()
        assert h["state"] == "elsewhere" and str(holder.pid) in h["last_error"]
    finally:
        holder.stdin.close()
        holder.wait(30)
    # l'autre console partie (même tuée), le verrou est libre
    sup = wapp._start_replication_follower()
    assert sup is not None and follower_globals == [sup]
    assert (tmp_path / "replication-follow.lock").read_text().strip() == str(os.getpid())


def test_the_same_console_keeps_its_own_lock(follower_globals):
    a = wapp._start_replication_follower()
    assert wapp._start_replication_follower() is a and follower_globals == [a]


# --- rotation du journal du suiveur sans redémarrage -------------------------------------

def test_follower_log_is_rotated_while_the_child_runs(sup):
    s, procs, log = sup["s"], sup["procs"], sup["log"]
    s.tick()
    s.tick()
    assert s.rotations == 0 and not getattr(procs[0], "signals", None)       # petit journal : rien
    log.write_bytes(b"x" * (s.LOG_MAX + 1))
    s.tick()
    assert s.rotations == 1 and procs[0].signals == [wapp.signal.SIGUSR1]
    assert log.with_name(log.name + ".1").stat().st_size == s.LOG_MAX + 1 and not log.exists()
    assert len(procs) == 1 and s.restarts == 0 and procs[0].terms == 0      # aucun redémarrage
    assert s.health()["log_rotations"] == 1


ROTATING_CHILD = """
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("hr", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.install_log_reopen(sys.argv[2])
for i in range(400):
    sys.stderr.write(f"line {i:04d} " + "y" * 90 + "\\n"); sys.stderr.flush()
    time.sleep(0.01)
time.sleep(30)
"""


def test_follower_log_rotation_with_a_real_child(tmp_path):
    """Superviseur réel, enfant réel qui rouvre son journal (le gestionnaire
    même de la ligne de commande) : rotation, même pid, aucune ligne perdue."""
    log = tmp_path / "logs" / "replication-follow.log"
    cmd = [sys.executable, "-c", ROTATING_CHILD, str(ROOT / "bin" / "harvester-replication.py"), str(log)]
    s = wapp.ReplicationFollowerSupervisor(lambda: cmd, lambda: log, lambda: True, lambda: 5)
    s.LOG_MAX = 8000
    s.tick()
    pid = s.proc.pid
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            s.tick()
            text = log.read_text() if log.exists() else ""
            if "line 0399" in text:
                break
            time.sleep(0.05)
        assert s.rotations >= 1 and s.proc.pid == pid and s.restarts == 0
        old = log.with_name(log.name + ".1").read_text()
        assert old and "line 0399" in log.read_text()
        assert len(log.read_text()) <= s.LOG_MAX + 2000          # le fichier courant reste borné
    finally:
        s.stop()


# --- budget d'arrêt -----------------------------------------------------------------------

def test_console_shutdown_clamps_every_grace_to_the_stop_budget(monkeypatch):
    seen = {}
    fake = type("S", (), {"stop": lambda self, grace=None: seen.update(grace=grace)})()
    monkeypatch.setattr(wapp, "REPLICATION_FOLLOWER", fake)
    monkeypatch.setattr(wapp, "_replication_follow_grace", lambda: 900)
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    try:
        run = wapp.ActionRun("b1", f"replication:{PID}:protect", "harvlab", ["x"])
        run.proc, run.kill_grace, run.status = _child(STUBBORN), 900, "done"
        wapp.ACTIONS["b1"] = run
        t0 = time.monotonic()
        codes = wapp._replication_shutdown(budget=1)
        assert codes == {"b1": -9} and time.monotonic() - t0 < 10
        assert seen["grace"] == 1
    finally:
        wapp.ACTIONS.clear()
        wapp.ACTIONS.update(saved)


def test_stop_budget_fits_the_unit_and_the_default_grace():
    unit = (ROOT / "config" / "systemd" / "harvester-ops.service").read_text()
    stop = int(re.search(r"podman stop -t (\d+)", unit).group(1))
    assert wapp._repl_grace({"options": {}}) <= wapp.REPLICATION_STOP_BUDGET < stop - 10


@pytest.mark.parametrize("timeouts,follow,status", [
    ({"undo_total": "550", "client": "20"}, None, 202),          # 550 + 20 + 15 = 585 : tient
    ({"undo_total": "551", "client": "20"}, None, 400),
    ({"undo_total": "2h"}, "console", 400),
    ({"undo_total": "2h"}, "cli", 202),                           # pas lancé par la console au suivi
    ({"undo_total": "2h"}, "off", 202),
])
def test_create_refuses_a_rollback_beyond_the_stop_budget(client, timeouts, follow, status):
    body = {"name": "w9", "from": "harvlab", "to": "harvlab2", "rpo": "15m", "vms": ["default/a"],
            "timeouts": timeouts}
    if follow:
        body["follow"] = follow
    r = client.post("/api/replication/plans", json=body)
    assert r.status_code == status, r.get_json()
    if status == 400:
        assert r.get_json()["code"] == "stop-budget" and "585" in r.get_json()["error"] and not wapp.ACTIONS


@pytest.mark.parametrize("body,status", [
    ({"timeouts": {"undo_total": "550"}}, 202),                  # client 20 du plan : 585
    ({"timeouts": {"undo_total": "560"}}, 400),
    ({"timeouts": {"client": "200"}}, 202),                       # 100 + 200 + 15
    ({"timeouts": {"client": "600"}}, 400),
    ({"timeouts": {"undo_total": "1h"}, "follow": "cli"}, 202),
    ({"timeouts": {"undo_total": None, "client": None}}, 202),    # retour aux défauts (285)
    ({"timeouts": {"undo_total": "nope"}}, 400),
])
def test_settings_refuse_a_rollback_beyond_the_stop_budget(client, body, status):
    r = client.patch(f"/api/replication/plans/{PID}/settings", json=body)
    assert r.status_code == status, r.get_json()
    assert bool(wapp.ACTIONS) is (status == 202)


def test_switching_a_large_plan_back_to_console_follow_is_refused(client, env):
    conn = db.connect(env["db"])
    db.update_plan(conn, PID, options={"follow": "cli", "timeouts": {"undo_total": 3600, "client": 20}})
    conn.close()
    r = client.patch(f"/api/replication/plans/{PID}/settings", json={"follow": "console"})
    assert r.status_code == 400 and r.get_json()["code"] == "stop-budget"
    assert client.patch(f"/api/replication/plans/{PID}/settings", json={"rpo": "1h"}).status_code == 202


# --- annulation : l'action reste en cours jusqu'à la sortie de l'outil --------------------

FINISH_ON_TERM = ("import json,signal,sys,time\n"
                  "def h(*a):\n    time.sleep(0.6); print(json.dumps(OUT)); sys.exit(RC)\n"
                  "signal.signal(signal.SIGTERM, h)\nprint('ready', file=sys.stderr, flush=True)\n"
                  "time.sleep(30)\n")


@pytest.mark.parametrize("rc,out,status", [
    (0, {"ok": True}, "done"),                                    # déjà finie : jamais réécrite
    (3, {"ok": False, "kind": "cancelled", "left": []}, "cancelled"),
    (3, {"ok": False, "kind": "interrupted", "resume_required": True}, "cancelled"),
])
def test_cancel_keeps_a_replication_action_running_until_the_tool_says(client, rc, out, status):
    run = wapp.ActionRun("c1", f"replication:{PID}:protect", "harvlab", ["x"])
    run.required_role, run.kill_grace, run.cancel_waits = "operator", 30, True
    wapp.ACTIONS["c1"] = run
    code = FINISH_ON_TERM.replace("OUT", repr(out)).replace("RC", str(rc))
    t = _RealThread(target=wapp._vm_transfer_runner, args=(run, [sys.executable, "-c", code]),
                    kwargs={"capture": True, "summarize": wapp._repl_summarize})
    t.start()
    deadline = time.monotonic() + 10
    while run.proc is None and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.3)
    r = client.delete("/api/action/c1")
    d = r.get_json()
    assert r.status_code == 200 and d["status"] == "running" and d["cancel_requested"] is True
    # toujours vue comme occupée : pas de second geste sur le plan, pas de mise à jour
    assert client.post(f"/api/replication/plans/{PID}/protect", json={}).status_code == 409
    assert "c1" in {a["id"] for a in wapp._update_busy_actions()}
    t.join(15)
    assert run.status == status and run.cancel_requested is True
    assert wapp._actions_db_get("c1")[0]["status"] == status          # Activité : le statut de l'outil


def test_a_cancel_before_the_process_exists_never_starts_it(client):
    run = wapp.ActionRun("c2", f"replication:{PID}:protect", "harvlab", ["x"])
    run.required_role, run.kill_grace, run.cancel_waits = "operator", 30, True
    wapp.ACTIONS["c2"] = run
    assert client.delete("/api/action/c2").get_json()["cancel_requested"] is True
    assert run.status == "starting"
    marker = Path(os.environ.get("TMPDIR", "/tmp")) / f"never-started-{os.getpid()}"
    wapp._vm_transfer_runner(run, [sys.executable, "-c", f"open({str(marker)!r}, 'w')"], capture=True,
                             summarize=wapp._repl_summarize)
    assert run.status == "cancelled" and run.proc is None and not marker.exists()
    assert run.error_summary == "cancelled before the command started: nothing done"


def test_other_actions_are_still_cancelled_at_once(client):
    run = wapp.ActionRun("c3", "vm-transfer", "harvlab", ["x"])
    run.proc, run.status = _child(POLITE), "running"
    wapp.ACTIONS["c3"] = run
    d = client.delete("/api/action/c3").get_json()
    assert d["status"] == "cancelled" and d["cancel_requested"] is False
    run.proc.wait(5)


# --- pages du journal : curseur composé (ts, id) ------------------------------------------

def test_event_pages_hold_when_ts_and_id_disagree_at_a_page_edge(client, env):
    """Un événement rangé plus tard (id plus grand) mais daté plus tôt : la
    page s'ordonne par (ts, id), le curseur aussi ; rien n'est sauté ni
    rendu deux fois."""
    conn = db.connect(env["db"])
    t = now_utc() + timedelta(hours=1)
    ids = {}
    for name, dt in (("late", t), ("mid", t - timedelta(seconds=10)), ("old", t - timedelta(seconds=20))):
        ids[name] = db.record_event(conn, "follow", step=name, resource_uid="u-z", now=dt)
    # rangé en dernier, daté entre mid et old
    ids["backdated"] = db.record_event(conn, "follow", step="backdated", resource_uid="u-z",
                                       now=t - timedelta(seconds=15))
    conn.close()
    full = client.get("/api/replication/resources/u-z/events?limit=500").get_json()["events"]
    assert [e["step"] for e in full] == ["late", "mid", "backdated", "old"]
    pages, before = [], None
    while True:
        q = "/api/replication/resources/u-z/events?limit=2" + (f"&before={before}" if before else "")
        d = client.get(q).get_json()
        pages += d["events"]
        before = d["next_before"]
        if before is None:
            break
    assert [e["id"] for e in pages] == [e["id"] for e in full]
    # la même chose au registre, page de 1
    conn = db.connect(env["db"])
    got, cur = [], None
    for _ in range(6):
        page = db.resource_events(conn, "u-z", limit=1, before=cur)
        if not page:
            break
        got.append(page[0]["step"])
        cur = page[0]["id"]
    conn.close()
    assert got == ["late", "mid", "backdated", "old"]


# --- métriques : opération seulement, jamais un plan ---------------------------------------

@pytest.mark.parametrize("action,label", [
    (f"replication:{PID}:protect", "replication:protect"),
    (f"replication:{PID2}:measure-start", "replication:measure-start"),
    (f"replication:{PID}:unprotect-delete-backups", "replication:unprotect-delete-backups"),
    ("replication:create:web", "replication:create"),
    ("shutdown", "shutdown"),
    (None, "unknown"),
])
def test_action_metric_label_keeps_only_the_operation(action, label):
    assert wapp._metric_action(action) == label


@pytest.mark.skipif(not wapp._PROMETHEUS_AVAILABLE, reason="prometheus_client absent")
def test_closed_replication_actions_count_by_operation(client):
    from prometheus_client import REGISTRY
    key = {"action": "replication:settings", "status": "done"}
    before = REGISTRY.get_sample_value("harvester_ops_actions_total", key) or 0
    run = wapp.ActionRun("m1", f"replication:{PID}:settings", "harvlab", ["x"])
    run.status, run.ended_at = "done", time.time()
    run.close()
    assert REGISTRY.get_sample_value("harvester_ops_actions_total", key) == before + 1
    text = client.get("/metrics").get_data(as_text=True)
    assert PID not in "\n".join(ln for ln in text.splitlines() if ln.startswith("harvester_ops_action"))


# --- l'action est réglée avant son départ, le contrôle d'occupation est atomique ---------

def test_role_and_grace_are_set_before_the_action_thread_starts(client, monkeypatch):
    seen = []

    class Probe(_NoThread):
        def start(self):
            [run] = wapp.ACTIONS.values()
            seen.append((run.required_role, run.kill_grace, run.cancel_waits))
            _NoThread.start(self)
    monkeypatch.setattr(wapp.threading, "Thread", Probe)
    assert client.post(f"/api/replication/plans/{PID}/failover",
                       json={"vms": ["default/rep-a"], "confirm": "web"}).status_code == 202
    assert seen == [("admin", 100 + 20 + 15, True)]


def test_two_simultaneous_gestures_on_one_plan_let_only_one_through(client, monkeypatch):
    real = wapp._transfer_busy

    def slow(*a, **k):
        r = real(*a, **k)
        time.sleep(0.3)                    # élargit la fenêtre entre contrôle et ajout
        return r
    monkeypatch.setattr(wapp, "_transfer_busy", slow)
    codes = []
    barrier = threading.Barrier(2)

    def hit():
        with wapp.app.test_client() as c:
            barrier.wait()
            codes.append(c.post(f"/api/replication/plans/{PID}/protect", json={}).status_code)
    ts = [_RealThread(target=hit) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    assert sorted(codes) == [202, 409] and len(wapp.ACTIONS) == 1


# --- v1.99.0 : /metrics lit un relevé gardé, exposé sous verrou -------------------------

from threading import Thread as _RealThread  # noqa: E402  (env remplace threading.Thread)


@pytest.mark.skipif(not wapp._PROMETHEUS_AVAILABLE, reason="prometheus_client absent")
def test_metrics_do_not_recompute_plan_rows_at_every_scrape(client, env, monkeypatch):
    calls = []
    real = wapp._rdb.plan_rows
    monkeypatch.setattr(wapp._rdb, "plan_rows", lambda *a, **k: calls.append(1) or real(*a, **k))
    clock = [1000.0]
    monkeypatch.setattr(wapp.time, "monotonic", lambda: clock[0])
    wapp._replication_metrics_invalidate()
    for _ in range(5):
        assert client.get("/metrics").status_code == 200
    assert len(calls) == 1
    clock[0] += wapp.REPLICATION_METRICS_TTL - 1
    client.get("/metrics")
    assert len(calls) == 1
    clock[0] += 2
    client.get("/metrics")
    assert len(calls) == 2


@pytest.mark.skipif(not wapp._PROMETHEUS_AVAILABLE, reason="prometheus_client absent")
def test_an_unreadable_registry_is_not_reread_at_every_scrape(client, env, monkeypatch):
    calls = []

    def boom(path):
        calls.append(1)
        raise sqlite3.DatabaseError("file is not a database")
    monkeypatch.setattr(wapp, "_replication_metrics_snapshot", boom)
    wapp._replication_metrics_invalidate()
    for _ in range(3):
        assert client.get("/metrics").status_code == 200
    assert len(calls) == 1


@pytest.mark.skipif(not wapp._PROMETHEUS_AVAILABLE, reason="prometheus_client absent")
def test_a_concurrent_scrape_never_sees_half_the_series(client, env, monkeypatch):
    """Le relevé A repose 40 séries ; au milieu, un relevé B arrive. B
    n'expose jamais pendant que A repose ses séries (il attend), et voit les
    40 nouvelles."""
    def snap(prefix):
        return (0, tuple(((("plan", f"{prefix}-{i:02d}"), ("source", "s"), ("target", "t")), 60, 30)
                         for i in range(40)))
    current = {"snap": snap("a")}
    monkeypatch.setattr(wapp, "_replication_metrics_snapshot", lambda path: current["snap"])
    wapp._replication_metrics_invalidate()
    client.get("/metrics")
    current["snap"] = snap("b")
    wapp._replication_metrics_invalidate()
    middle, done, overlaps = threading.Event(), [], []
    applying = {}                       # fil -> séries reposées depuis son clear()
    real_gauge = wapp.metric_repl_rpo_objective
    real_generate = wapp.generate_latest

    class Slow:
        def clear(self):
            applying[threading.get_ident()] = 0
            real_gauge.clear()

        def labels(self, **kw):
            me = threading.get_ident()
            applying[me] += 1
            if applying[me] == 10 and not middle.is_set():
                middle.set()
                time.sleep(0.3)            # B a le temps d'exposer s'il le peut
            if applying[me] == 40:
                del applying[me]
            return real_gauge.labels(**kw)

    def generate(*a, **k):
        me = threading.get_ident()
        overlaps.extend(t for t in applying if t != me)
        return real_generate(*a, **k)
    monkeypatch.setattr(wapp, "metric_repl_rpo_objective", Slow())
    monkeypatch.setattr(wapp, "generate_latest", generate)

    def scrape_b():
        middle.wait(5)
        done.append(wapp.app.test_client().get("/metrics").get_data(as_text=True))
    t = _RealThread(target=scrape_b)
    t.start()
    client.get("/metrics")
    t.join(10)
    assert overlaps == []
    plans = re.findall(r'harvester_ops_replication_rpo_objective_seconds\{plan="([ab])-', done[0])
    assert len(plans) == 40 and set(plans) == {"b"}


# --- 2.0.0 : « Reprendre la commande » décidé par le serveur -----------------------------

def _resume_action(as_user, user):
    d = as_user(user, "GET", f"/api/replication/plans/{PID}").get_json()
    found = [a for a in d["row"]["actions"] if a["name"] == "resume-command"]
    assert len(found) <= 1
    return found[0] if found else None


@pytest.mark.parametrize("state,origin,run,need", RESUME_ROLES)
@pytest.mark.parametrize("user", ["alice", "patronne"])
def test_plan_row_offers_resume_with_the_role_the_route_requires(as_user, env, user, state, origin, run, need):
    """La ligne du contrat porte l'action de reprise avec le rôle que la
    route POST .../resume exigera : un opérateur ne voit plus un bouton
    actif que le serveur refuse."""
    _plan_state(env, state, origin, run)
    a = _resume_action(as_user, user)
    assert a is not None and a["role"] == need
    ok = wapp.ROLE_RANK[USERS[user]] >= wapp.ROLE_RANK[need]
    assert a["allowed"] is ok and a["reason"] == ("" if ok else "role")
    assert bool(a["hint"]) is (not ok)


@pytest.mark.parametrize("state,origin,run", [
    ("protected", "", None),
    ("draft", "", None),
    ("measuring", "", None),                     # mesure en cours, rien d'interrompu
    ("unprotected", "", None),
    ("error", "", None),                         # erreur sans origine connue
    ("draft", "", ("measure-start", {}, True, 1)),   # terminée
])
def test_plan_row_has_no_resume_when_nothing_is_interrupted(as_user, env, state, origin, run):
    _plan_state(env, state, origin, run)
    assert _resume_action(as_user, "alice") is None
    assert _resume_action(as_user, "patronne") is None


def test_plan_row_offers_resume_for_a_command_stopped_by_the_console(as_user, env):
    """Décision 68 : une mesure arrêtée par l'arrêt de la console avant son
    premier changement d'état laisse le plan en `draft` ; la reprise reste
    proposée (à l'opérateur)."""
    _plan_state(env, "draft")
    conn = db.connect(env["db"])
    db.record_run(conn, PID, "measure-start", now_utc(), 1, result="interrupted", points={"interval_s": 60})
    conn.close()
    a = _resume_action(as_user, "alice")
    assert a == {"name": "resume-command", "role": "operator", "allowed": True, "reason": "", "hint": ""}


@pytest.mark.parametrize("state,origin,interrupted,expect", [
    ("protecting", None, (), True), ("failing_over", None, (), True), ("unprotecting", None, (), True),
    ("error", "measuring", (), True), ("measuring", None, (), False), ("error", None, (), False),
    ("protected", None, (), False), ("draft", None, ("measure-start",), True),
    ("draft", None, ("measure-stop",), False), ("measuring", None, ("measure-stop",), True),
])
def test_resumable_reads_like_resume(state, origin, interrupted, expect):
    runs = [{"operation": op, "finished_at": None, "result": "interrupted", "generation": 1} for op in interrupted]
    assert wapp._rr.resumable(state, origin, runs, 1) is expect
    # une ligne d'une autre génération ne compte pas
    other = [dict(r, generation=2) for r in runs]
    if interrupted and state == "draft":
        assert wapp._rr.resumable(state, origin, other, 1) is False


def test_plan_view_has_no_resumable_state_list_in_the_browser():
    js = (ROOT / "web" / "static" / "js" / "replication-plan.js").read_text()
    assert "'failing_over'].includes" not in js and "resumable" not in js
    assert "U.action(row, 'resume-command')" in js and "resumeCmd.allowed" in js
