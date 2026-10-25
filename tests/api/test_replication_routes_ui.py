"""Écrans de réplication (BE1) : routes de la console.

Création (`map_disk_class`, `map_drill_net`, `recovery` par le fichier
privé `--spec`), réglages (`recovery`, `mappings`, `cloud_init`, `mac`,
`start`, règles d'état, `protect_needed`), essai d'une action annexe
(opérateur, action suivie), VMs candidates (lecteur, kubeconfig de la
personne, 403 `source-denied`). Aucun secret dans la ligne de commande ni
dans une réponse."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_replication_routes_199 import (KC, KUBE, PID, PID2, _NoThread, as_user, base, client, db, env,  # noqa: E402,F401
                                         only_run, rp, script_cmd, wapp)

SECRET = "Bearer r0ute-s3cr3t"
HOOK = {"name": "dns", "at": "start", "method": "POST", "url": "https://hooks.example/dns",
        "secret_headers": {"Authorization": SECRET}}
CREATE = {"name": "erp", "from": "harvlab", "to": "harvlab2", "vms": ["default/web-1", "default/db-1"], "rpo": "15m"}


def spec_of(cmd):
    i = cmd.index("--spec")
    return json.loads(Path(cmd[i + 1]).read_text())


def no_secret(*things):
    text = json.dumps([t if isinstance(t, (dict, list, str)) else repr(t) for t in things], default=str)
    return SECRET not in text and "r0ute" not in text


# ---------------------------------------------------------------------------
# Création
# ---------------------------------------------------------------------------

def test_create_passes_recovery_through_the_spec_file_and_maps_on_the_command_line(client, env):
    body = dict(CREATE, map_disk_class={"default/web-1/rootdisk": "fast"},
                map_drill_net={"default/prod": "default/drill"},
                recovery={"waves": [{"name": "db", "vms": ["default/db-1"], "gate": {"kind": "agent"}}],
                          "hooks": [HOOK], "resize": {"default/web-1": {"cpu": 1}}, "auto_protect": False})
    r = client.post("/api/replication/plans", json=body)
    assert r.status_code == 202, r.get_json()
    cmd = script_cmd()
    assert "--map-disk-class=default/web-1/rootdisk=fast" in cmd
    assert "--map-drill-net=default/prod=default/drill" in cmd
    assert spec_of(cmd) == {"recovery": body["recovery"]}
    assert no_secret([a for a in cmd if not a.startswith("/")], only_run().cmd, r.get_json())


def test_create_without_recovery_has_no_spec_file(client):
    assert client.post("/api/replication/plans", json=CREATE).status_code == 202
    assert "--spec" not in script_cmd()


@pytest.mark.parametrize("extra,code", [
    ({"recovery": "fast"}, None),
    ({"recovery": {"retries": 1}}, None),
    ({"recovery": {"waves": [{"vms": ["default/other"]}]}}, "bad-option"),
    ({"recovery": {"hooks": [dict(HOOK, secret_headers={"Authorization": "********"})]}}, "bad-option"),
    ({"recovery": {"hooks": [dict(HOOK, url="https://u:p@h/")]}}, "bad-option"),
    ({"recovery": {"network_data": {"default/web-1": "v"}}}, "bad-option"),
    ({"recovery": {"auto_protect": "yes"}}, "bad-option"),
    ({"map_disk_class": {"default/web-1": "fast"}}, "bad-mapping"),
    ({"map_disk_class": ["x"]}, None),
    ({"map_drill_net": {"default/prod": "drill"}}, "bad-reference"),
])
def test_create_refusals(client, extra, code):
    r = client.post("/api/replication/plans", json=dict(CREATE, **extra))
    assert r.status_code == 400 and r.get_json().get("code") == code, r.get_json()
    assert _NoThread.started == [] and no_secret(r.get_json())


# ---------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------

def _set_state(env, *states):
    conn = db.connect(env["db"])
    try:
        cur = db.get_plan(conn, PID)["state"]
        for s in states:
            db.set_state(conn, PID, cur, s)
            cur = s
    finally:
        conn.close()


def _store_hooks(env, hooks, plan_id=PID):
    conn = db.connect(env["db"])
    try:
        db.update_plan(conn, plan_id, options={"hooks": hooks})
    finally:
        conn.close()


@pytest.mark.parametrize("body,argv,spec,need", [
    ({"recovery": {"waves": [{"vms": ["default/rep-b"]}], "accept_degraded_standby": True}}, [],
     {"recovery": {"waves": [{"vms": ["default/rep-b"]}], "accept_degraded_standby": True}}, False),
    ({"recovery": {"resize": {"default/rep-a": {"memory": "2Gi"}}}}, [],
     {"recovery": {"resize": {"default/rep-a": {"memory": "2Gi"}}}}, True),
    ({"cloud_init": "rerun", "mac": "new", "start": "stopped"},
     ["--cloud-init=rerun", "--mac=new", "--start=stopped"], None, True),
    ({"mappings": {"networks": {"default/prod": "default/lab-net"}}}, [],
     {"mappings": {"networks": {"default/prod": "default/lab-net"}}}, True),
    ({"mappings": {"drill_networks": {"default/prod": "default/drill"}}}, [],
     {"mappings": {"drill_networks": {"default/prod": "default/drill"}}}, False),
    ({"rpo": "30m"}, ["--rpo=30m"], None, False),
])
def test_settings_accepted_on_a_protected_plan(client, body, argv, spec, need):
    r = client.patch(f"/api/replication/plans/{PID}/settings", json=body)
    assert r.status_code == 202, r.get_json()
    assert r.get_json().get("protect_needed", False) is need
    cmd = script_cmd()
    for a in argv:
        assert a in cmd
    if spec:
        assert spec_of(cmd) == spec
    else:
        assert "--spec" not in cmd
    assert only_run().action == f"replication:{PID}:settings"


def test_settings_mappings_of_a_draft_plan(client):
    body = {"mappings": {"disk_classes": {"default/rep-c/disk-0": "fast"}, "storage_classes": {"a": "b"}}}
    r = client.patch(f"/api/replication/plans/{PID2}/settings", json=body)
    assert r.status_code == 202 and "protect_needed" not in r.get_json()
    assert spec_of(script_cmd()) == body


@pytest.mark.parametrize("states,body,status,code", [
    ((), {"mappings": {"disk_classes": {"default/rep-a/disk-0": "fast"}}}, 400, "state-forbids"),
    ((), {"mappings": {"namespaces": {"default": "dr"}}}, 400, "state-forbids"),
    (("failing_over",), {"recovery": {"auto_protect": True}}, 400, "state-forbids"),
    (("failing_over",), {"start": "stopped"}, 400, "state-forbids"),
    (("failing_over",), {"mappings": {"drill_networks": {"default/a": "default/b"}}}, 400, "state-forbids"),
    ((), {"mappings": {"volumes": {}}}, 400, None),
    ((), {"mappings": {}}, 400, None),
    ((), {"mappings": "x"}, 400, None),
    ((), {"recovery": [1]}, 400, None),
    ((), {"recovery": {"bogus": 1}}, 400, None),
    ((), {"recovery": {"waves": [{"vms": ["default/rep-z"]}]}}, 400, "bad-option"),
    ((), {"recovery": {"network_data": {"default/rep-a": "v"}}}, 400, "bad-option"),
    ((), {"recovery": {"hooks": [dict(HOOK, secret_headers={"X-New": "********"})]}}, 400, "secret-missing"),
    ((), {"mappings": {"networks": {"default/prod": "lab"}}}, 400, "bad-reference"),
    ((), {"cloud_init": "always"}, 400, None),
    ((), {"mac": True}, 400, None),
])
def test_settings_refusals(client, env, states, body, status, code):
    _set_state(env, *states)
    r = client.patch(f"/api/replication/plans/{PID}/settings", json=body)
    assert r.status_code == status and r.get_json().get("code") == code, r.get_json()
    assert _NoThread.started == []


def test_settings_keep_a_masked_secret_and_never_show_it(client, env):
    _store_hooks(env, [HOOK])
    got = client.get(f"/api/replication/plans/{PID}").get_json()["plan"]["options"]["hooks"]
    assert got[0]["secret_headers"] == {"Authorization": "********"}
    got[0]["url"] = "https://hooks.example/v2"
    r = client.patch(f"/api/replication/plans/{PID}/settings", json={"recovery": {"hooks": got}})
    assert r.status_code == 202, r.get_json()
    cmd = script_cmd()
    # le masque part tel quel dans le fichier privé ; le script le remplace sous verrou
    assert spec_of(cmd)["recovery"]["hooks"][0]["secret_headers"] == {"Authorization": "********"}
    assert no_secret([a for a in cmd if not a.startswith("/")], r.get_json(), Path(cmd[cmd.index("--spec") + 1])
                     .read_text())


def test_get_plan_masks_every_secret(client, env):
    _store_hooks(env, [HOOK, dict(HOOK, name="two", secret_headers={"X-A": "r0ute-other"})])
    d = client.get(f"/api/replication/plans/{PID}").get_json()
    assert [h["secret_headers"] for h in d["plan"]["options"]["hooks"]] == [{"Authorization": "********"},
                                                                            {"X-A": "********"}]
    assert no_secret(d) and no_secret(client.get("/api/replication/plans").get_json())


# ---------------------------------------------------------------------------
# Essai d'une action annexe
# ---------------------------------------------------------------------------

def test_hook_test_route_launches_a_tracked_action(client, env):
    _store_hooks(env, [HOOK])
    r = client.post(f"/api/replication/plans/{PID}/hooks/test", json={"name": "dns"})
    assert r.status_code == 202 and r.get_json() == {"action_id": only_run().id, "plan": PID, "name": "dns"}
    assert script_cmd()[:-1] == base(env, "hook", "test", PID) + ["--name=dns"]
    run = only_run()
    assert run.action == f"replication:{PID}:hook-test" and run.required_role == "operator"
    assert no_secret(run.cmd, script_cmd())


@pytest.mark.parametrize("body,status,code", [
    ({"name": "nope"}, 404, "unknown-hook"),
    ({"name": "Bad"}, 400, "bad-option"),
    ({}, 400, "bad-option"),
    ({"name": ["dns"]}, 400, "bad-option"),
])
def test_hook_test_route_refusals(client, env, body, status, code):
    _store_hooks(env, [HOOK])
    r = client.post(f"/api/replication/plans/{PID}/hooks/test", json=body)
    assert r.status_code == status and r.get_json().get("code") == code
    assert _NoThread.started == []


@pytest.mark.parametrize("user,status", [("curieux", 403), ("alice", 202), ("patronne", 202)])
def test_hook_test_needs_the_operator_role(as_user, env, user, status):
    _store_hooks(env, [HOOK])
    r = as_user(user, "POST", f"/api/replication/plans/{PID}/hooks/test", json={"name": "dns"})
    assert r.status_code == status


# ---------------------------------------------------------------------------
# VMs candidates
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_candidates(monkeypatch):
    seen = {}

    def fake(kube, cluster, namespace=None, selector=None, held=None):
        seen.update(kube=kube, cluster=cluster, namespace=namespace, selector=selector, held=held)
        if seen.get("raise"):
            raise RuntimeError(seen["raise"])
        return {"cluster": cluster, "vms": []}
    monkeypatch.setattr(wapp._rr, "candidates", fake)
    return seen


def test_candidates_read_with_the_persons_kubeconfig(client, fake_candidates):
    r = client.get("/api/replication/candidates/harvlab?namespace=default&selector=dr=gold")
    assert r.status_code == 200 and r.get_json() == {"cluster": "harvlab", "vms": []}
    assert fake_candidates["kube"].kubeconfig == KC["harvlab"]
    assert (fake_candidates["namespace"], fake_candidates["selector"]) == ("default", "dr=gold")
    # plans du registre qui tiennent déjà ces VMs (plan `web` : source harvlab)
    assert fake_candidates["held"]["u-a"] == [PID] and fake_candidates["held"]["default/rep-b"] == [PID]
    assert _NoThread.started == []


@pytest.mark.parametrize("path,status,code,error", [
    ("/api/replication/candidates/nowhere", 404, "unknown-cluster", None),
    ("/api/replication/candidates/harvlab?selector=dr=gold", 400, "bad-selection", None),
    ("/api/replication/candidates/harvlab?namespace=default&selector=dr", 400, "bad-selector", None),
    ("/api/replication/candidates/harvlab?namespace=Bad_NS", 400, None, None),
    ("/api/replication/candidates/harvlab", 403, "source-denied", "Error from server (Forbidden): forbidden"),
    ("/api/replication/candidates/harvlab", 502, "source-unreachable", "dial tcp /srv/kc/harvlab.yaml: timeout"),
])
def test_candidates_refusals(client, fake_candidates, path, status, code, error):
    fake_candidates["raise"] = error
    r = client.get(path)
    assert r.status_code == status and r.get_json().get("code") == code, r.get_json()
    assert "/srv/kc" not in r.get_data(as_text=True)


def test_candidates_refused_when_the_session_cannot_see_the_cluster(client, env, fake_candidates):
    env["access"].pop("harvlab")
    r = client.get("/api/replication/candidates/harvlab")
    assert r.status_code == 403 and r.get_json()["code"] == "source-denied" and "kube" not in fake_candidates


def test_candidates_are_a_viewer_read(as_user, fake_candidates):
    assert as_user("curieux", "GET", "/api/replication/candidates/harvlab").status_code == 200


def test_candidates_end_to_end_on_the_simulated_cluster(client, env, monkeypatch, tmp_path):
    from test_replication_run_199 import World
    import kube as kube_mod
    w = World(tmp_path, vms=("vm-a",))
    monkeypatch.setattr(kube_mod, "Kube", lambda kc, timeout=None: w.src)
    d = client.get("/api/replication/candidates/harvlab").get_json()
    assert [v["name"] for v in d["vms"]] == ["vm-a"] and d["vms"][0]["eligible"] is True
    assert {x["disk"] for x in d["vms"][0]["disks"]} == {"root", "data"}
