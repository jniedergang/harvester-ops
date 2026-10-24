"""Écrans de la réplication dans la console : séries, alertes, liens,
notifications, essai actif d'un lien, protection automatique.

Lectures droit au registre par les fonctions de la ligne de commande ;
écritures en actions suivies qui lancent bin/harvester-replication.py.
Secrets : masqués à chaque lecture, jamais dans la commande (fichier privé
`--spec`), `********` garde la valeur rangée. Protection automatique : geste
de la console (auteur `console`), hors de toute requête, borné à une fois
toutes les 10 minutes par plan, interrupteur `replication.auto_protect`."""

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_replication_routes_199 import (  # noqa: E402,F401
    KC, PID, PID2, as_user, base, client, db, env, iso, now_utc, only_run, rp, script_cmd, wapp)

STORED_TOKEN = "stored-matrix-token"


def conn_of(env):
    return db.connect(env["db"])


# ---------------------------------------------------------------------------
# Lectures
# ---------------------------------------------------------------------------

def test_plan_rows_carry_the_new_fields(client):
    web = {p["id"]: p for p in client.get("/api/replication/plans").get_json()["plans"]}[PID]
    assert set(web["transfer"]) == {"last_at", "last_bytes", "upload_bps", "apply_bps", "bytes_24h", "passes_24h"}
    assert web["pending_vms"] == [] and isinstance(web["alerts"], int) and web["alerts"] >= 1   # rep-b en retard
    assert set(web["reach"]) == {"source", "target", "at"}
    one = client.get(f"/api/replication/plans/{PID}").get_json()["row"]
    assert one["alerts"] == web["alerts"] and "transfer" in one


def test_series_route(client, env):
    c = conn_of(env)
    n = now_utc()
    for ago, rpo in ((30, 100), (90, 300), (7200, 50)):
        db.record_stats(c, PID, {"generation": 1, "resource_uid": "u-a", "pass_at": iso(n - timedelta(seconds=ago)),
                                 "newly_uploaded": 10, "rpo_held_s": rpo, "upload_bps": 5.0})
    c.close()
    d = client.get(f"/api/replication/plans/{PID}/series?range=1h").get_json()
    assert d["plan"] == PID and d["range"] == "1h" and d["bucket_s"] == 60 and d["rpo_objective_s"] == 900
    assert sum(p["passes"] for p in d["points"]) == 2
    d = client.get(f"/api/replication/plans/{PID}/series").get_json()        # 24 h par défaut
    assert d["range"] == "24h" and sum(p["bytes"] for p in d["points"]) == 30
    assert client.get(f"/api/replication/plans/{PID}/series?resource=u-b").get_json()["points"] == []


@pytest.mark.parametrize("path,status", [
    (f"/api/replication/plans/{PID}/series?range=2h", 400),
    (f"/api/replication/plans/{PID}/series?resource=../x", 400),
    ("/api/replication/plans/rp-ffffffffffff/series", 404),
    ("/api/replication/plans/nope/series", 400),
    ("/api/replication/alerts?host=a;b", 400),
])
def test_read_refusals(client, path, status):
    assert client.get(path).status_code == status


def test_alerts_route_and_host_filter(client, env):
    d = client.get("/api/replication/alerts").get_json()
    assert d["count"] == len(d["alerts"]) >= 1
    lag = [a for a in d["alerts"] if a["name"] == "rep-b"]
    assert lag and lag[0]["code"] == "rpo-exceeded" and lag[0]["plan"] == PID and lag[0]["level"] == "warn"
    assert client.get("/api/replication/alerts?host=harvlab").get_json()["count"] == d["count"]
    assert client.get("/api/replication/alerts?host=elsewhere").get_json() == {"count": 0, "alerts": []}
    c = conn_of(env)
    try:
        assert d == json.loads(json.dumps(db.alerts(c, now_utc())))    # même fonction que la ligne de commande
    finally:
        c.close()


def test_links_route(client, env):
    c = conn_of(env)
    db.record_link_test(c, {"source": "harvlab", "target": "harvlab2", "by": "alice", "size_bytes": 5,
                            "backup_target": "nfs://nas:/b"})
    c.close()
    d = client.get("/api/replication/links").get_json()
    pairs = {(x["source"], x["target"]): x for x in d["links"]}
    assert set(pairs) == {("harvlab", "harvlab2"), ("harvlab2", "harvlab3")}
    assert pairs[("harvlab", "harvlab2")]["active"]["by"] == "alice"
    assert pairs[("harvlab2", "harvlab3")]["active"] is None
    assert pairs[("harvlab2", "harvlab3")]["plans"] == [{"id": PID2, "name": "db"}]


def test_notify_read_is_masked(client):
    d = client.get("/api/replication/notify").get_json()
    assert d["notify"]["channels"][0]["token"] == "********" and STORED_TOKEN not in json.dumps(d)
    assert "selector-new-vm" in d["codes"] and d["kinds"] == ["matrix", "slack", "webhook", "email"]


# ---------------------------------------------------------------------------
# Écritures : réglage des notifications, essai d'envoi, essai d'un lien
# ---------------------------------------------------------------------------

def test_notify_put_sends_the_settings_through_a_private_spec_file(client, env):
    body = {"enabled": True, "min_level": "error", "codes": None, "channels": [
        {"id": "c1", "name": "ops", "kind": "matrix", "homeserver": "https://matrix.example", "room": "!a:example",
         "token": "********"},
        {"id": "c2", "kind": "slack", "url": "https://hooks.slack.test/new-secret"}]}
    r = client.put("/api/replication/notify", json=body)
    assert r.status_code == 202, r.get_json()
    cmd = script_cmd()
    i = cmd.index("--spec")
    assert cmd[:i] == base(env, "notify", "set")
    spec = Path(cmd[i + 1])
    assert json.loads(spec.read_text()) == body and (spec.stat().st_mode & 0o077) == 0
    joined = " ".join(cmd)
    assert "new-secret" not in joined and STORED_TOKEN not in joined
    run = only_run()
    assert run.action == "replication:notify:set" and run.required_role == "admin"
    assert "new-secret" not in json.dumps(run.cmd) and str(spec) not in json.dumps(run.cmd)
    spec.unlink()


@pytest.mark.parametrize("body,code", [
    ({"enabled": "yes"}, "bad-notify"),
    ({"channels": [{"id": "c9", "kind": "matrix", "homeserver": "https://m", "room": "!a:b", "token": "********"}]},
     "secret-missing"),
    ({"channels": [{"id": "c1", "kind": "slack", "url": "********"}]}, "secret-missing"),   # autre nature
])
def test_notify_put_refusals_answer_400_before_any_action(client, body, code):
    r = client.put("/api/replication/notify", json=body)
    assert r.status_code == 400 and r.get_json()["code"] == code and not wapp.ACTIONS


def test_notify_put_rejects_a_non_object(client):
    assert client.put("/api/replication/notify", data="[1]", content_type="application/json").status_code == 400


def test_notify_test_route(client, env):
    r = client.post("/api/replication/notify/test", json={"channel": "c1"})
    assert r.status_code == 202 and r.get_json()["channel"] == "c1"
    assert script_cmd() == base(env, "notify", "test") + ["--channel=c1", f"--id={only_run().id}"]
    assert only_run().required_role == "admin"


@pytest.mark.parametrize("body,code", [({"channel": "c9"}, "unknown-channel"), ({"channel": "C 1"}, None),
                                       ({}, None)])
def test_notify_test_refusals(client, body, code):
    r = client.post("/api/replication/notify/test", json=body)
    assert r.status_code == 400 and r.get_json().get("code") == code and not wapp.ACTIONS


def test_link_test_route_uses_the_person_kubeconfigs(client, env):
    r = client.post("/api/replication/links/test", json={"from": "harvlab", "to": "harvlab2", "size_mib": 64})
    assert r.status_code == 202, r.get_json()
    run = only_run()
    assert script_cmd() == base(env, "link", "test") + [
        "--from=harvlab", "--to=harvlab2", "--size-mib=64", f"--from-kubeconfig={KC['harvlab']}",
        f"--to-kubeconfig={KC['harvlab2']}", f"--id={run.id}"]
    assert run.action == "replication:link-test:harvlab:harvlab2" and run.required_role == "operator"
    assert run.kill_grace == wapp.REPLICATION_LINK_KILL_GRACE and run.cancel_waits is True
    assert run.shutdown_grace == wapp.REPLICATION_LINK_SHUTDOWN_GRACE
    assert not any("/srv/kc" in a for a in run.cmd)
    # un essai à la fois sur ce lien
    r = client.post("/api/replication/links/test", json={"from": "harvlab", "to": "harvlab2"})
    assert r.status_code == 409 and r.get_json()["running"] == run.id and len(wapp.ACTIONS) == 1


def test_link_test_default_size(client):
    assert client.post("/api/replication/links/test", json={"from": "harvlab", "to": "harvlab2"}).status_code == 202
    assert "--size-mib=512" in script_cmd()


@pytest.mark.parametrize("body,status,code", [
    ({"from": "harvlab", "to": "harvlab"}, 400, "same-cluster"),
    ({"from": "harvlab", "to": "nowhere"}, 400, "unknown-cluster"),
    ({"from": "harvlab", "to": "harvlab2", "size_mib": 63}, 400, "bad-size"),
    ({"from": "harvlab", "to": "harvlab2", "size_mib": 4097}, 400, "bad-size"),
    ({"from": "harvlab", "to": "harvlab2", "size_mib": "512"}, 400, "bad-size"),
    ({"from": "harvlab", "to": "harvlab2", "size_mib": True}, 400, "bad-size"),
    ({"from": "a;b", "to": "harvlab2"}, 400, None),
    ({"to": "harvlab2"}, 400, None),
])
def test_link_test_refusals(client, body, status, code):
    r = client.post("/api/replication/links/test", json=body)
    assert r.status_code == status and r.get_json().get("code") == code and not wapp.ACTIONS


@pytest.mark.parametrize("hidden,code", [("harvlab", "source-denied"), ("harvlab2", "target-denied")])
def test_link_test_needs_access_to_both_clusters(client, env, hidden, code):
    env["access"].pop(hidden)
    r = client.post("/api/replication/links/test", json={"from": "harvlab", "to": "harvlab2"})
    assert r.status_code == 403 and r.get_json()["code"] == code and not wapp.ACTIONS


def test_required_role_table():
    f = wapp.required_role_for
    assert f("/api/replication/notify", "GET") == "operator"
    assert f("/api/replication/notify", "PUT") == "admin"
    assert f("/api/replication/notify/test", "POST") == "admin"
    assert f("/api/replication/links/test", "POST") == "operator"
    for path in ("/api/replication/alerts", "/api/replication/links", f"/api/replication/plans/{PID}/series",
                 "/api/replication/plans"):
        assert f(path, "GET") == "viewer"
    assert f(f"/api/replication/plans/{PID}/failover", "POST") == "admin"
    assert f(f"/api/replication/plans/{PID}/protect", "POST") == "operator"


# ---------------------------------------------------------------------------
# Protection automatique (geste de la console)
# ---------------------------------------------------------------------------

def make_auto(env, auto=True, state=None, pending=True, paused=False):
    c = conn_of(env)
    try:
        p = db.get_plan(c, PID)
        opts = dict(p["options"], auto_protect=auto, paused=paused)
        c.execute("UPDATE plans SET selector = 'dr=gold', selector_namespace = 'default', vms = '[]', options = ?"
                  " WHERE id = ?", (json.dumps(opts), PID))
        if state:
            c.execute("UPDATE plans SET state = ? WHERE id = ?", (state, PID))
        if pending:
            db.set_selector_watch(c, PID, [{"uid": "u-n", "namespace": "default", "name": "new", "since": "x"}], [])
    finally:
        c.close()


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def test_auto_protect_launches_a_tracked_protect_by_the_console_outside_a_request(env):
    make_auto(env)
    ap = wapp.ReplicationAutoProtect(enabled=lambda: True, clock=Clock())
    assert ap.tick() == [PID]
    run = only_run()
    assert run.action == f"replication:{PID}:protect" and run.required_role == "operator"
    assert script_cmd() == base(env, "protect", PID, user="console") + [f"--id={run.id}"]
    assert not any(a.startswith(("--from-kubeconfig", "--to-kubeconfig")) for a in script_cmd())
    assert run.result["by"] == "console" and run.result["auto"] is True


def test_auto_protect_is_rate_limited_per_plan(env):
    make_auto(env)
    clock = Clock()
    launched = []
    ap = wapp.ReplicationAutoProtect(enabled=lambda: True, clock=clock, launch=lambda p: launched.append(p["id"]) or True)
    assert ap.tick() == [PID]
    clock.t += wapp.REPLICATION_AUTO_PROTECT_EVERY_S - 1
    assert ap.tick() == []
    clock.t += 1
    assert ap.tick() == [PID] and launched == [PID, PID]


def test_auto_protect_refused_launch_is_retried_next_tick(env):
    make_auto(env)
    answers = [False, True]
    ap = wapp.ReplicationAutoProtect(enabled=lambda: True, clock=Clock(), launch=lambda p: answers.pop(0))
    assert ap.tick() == [] and ap.tick() == [PID]


def test_auto_protect_busy_plan_launches_nothing(env):
    make_auto(env)
    ap = wapp.ReplicationAutoProtect(enabled=lambda: True, clock=Clock())
    assert ap.tick() == [PID]
    ap.last.clear()
    assert ap.tick() == [] and len(wapp.ACTIONS) == 1          # un geste à la fois par plan


@pytest.mark.parametrize("kw", [dict(auto=False), dict(state="failed_over"), dict(pending=False),
                                dict(paused=True)])
def test_auto_protect_conditions(env, kw):
    make_auto(env, **kw)
    ap = wapp.ReplicationAutoProtect(enabled=lambda: True, clock=Clock(), launch=lambda p: True)
    assert ap.tick() == []


def test_auto_protect_skips_a_locked_plan(env):
    make_auto(env)
    c = conn_of(env)
    db.acquire_lock(c, PID, "protect", "cli:otherhost:1", host="here")
    c.close()
    ap = wapp.ReplicationAutoProtect(enabled=lambda: True, clock=Clock(), launch=lambda p: True)
    assert ap.tick() == []


def test_auto_protect_named_vms_plan_is_never_candidate(env):
    c = conn_of(env)
    p = db.get_plan(c, PID)
    c.execute("UPDATE plans SET options = ? WHERE id = ?", (json.dumps(dict(p["options"], auto_protect=True)), PID))
    db.set_selector_watch(c, PID, [{"uid": "u-n", "namespace": "default", "name": "new", "since": "x"}], [])
    c.close()
    assert wapp.ReplicationAutoProtect(enabled=lambda: True, clock=Clock(), launch=lambda p: True).tick() == []


def test_auto_protect_kill_switch(env, monkeypatch):
    make_auto(env)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"replication": {"auto_protect": False}})
    assert wapp._replication_auto_protect_enabled() is False
    assert wapp.ReplicationAutoProtect(clock=Clock(), launch=lambda p: True).tick() == []
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"replication": {"follow": False}})
    assert wapp._replication_auto_protect_enabled() is True
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {})
    assert wapp._replication_auto_protect_enabled() is True


def test_shutdown_stops_auto_protect(env, monkeypatch):
    ap = wapp.ReplicationAutoProtect(enabled=lambda: True, clock=Clock(), launch=lambda p: True)
    monkeypatch.setattr(wapp, "REPLICATION_AUTO_PROTECT", ap)
    monkeypatch.setattr(wapp, "REPLICATION_FOLLOWER", None)
    wapp._replication_shutdown(budget=1)
    assert ap._stop.is_set()
