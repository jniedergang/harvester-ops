"""2.8.0 (tâche 4) : routes GitOps. Opérateur ET droit d'administrer le
cluster enfant ; `privileges: cluster` = administrateur (refusé avant tout
lancement) ; les secrets saisis partent dans la demande privée de l'outil,
jamais dans l'action, sa ligne de commande ni actions.db."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as wapp  # noqa: E402
from test_capi_artefact_routes_270 import calls, env, wait  # noqa: E402,F401

KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nROUTE-KEY-1618\n-----END OPENSSH PRIVATE KEY-----\n"
ENTRY = {"name": "demo", "namespace": "equipe-a",
         "source": {"type": "git", "url": "ssh://git@172.16.1.11:2222/x/app.git", "secret": "depot"}}


def post(env, **over):
    b = {"cluster": "web/web", "entry": dict(ENTRY), "secrets": {"depot": {"identity": KEY}}}
    b.update(over)
    return env.client.post("/api/capi/harvlab2/gitops", json=b)


def test_deploy_launches_the_tool_and_keeps_no_secret(env, tmp_path, monkeypatch):
    db = tmp_path / "actions.db"
    monkeypatch.setattr(wapp, "ACTIONS_DB", db)
    wapp._actions_init_db()
    r = post(env)
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-gitops:web/web:demo" and run.status == "done"
    c = calls(env)[-1]
    assert c["argv"][0] == "gitops-deploy"
    assert c["spec"]["role"] == "operator" and c["spec"]["by"] == "alice"
    assert c["spec"]["secrets"] == {"depot": {"identity": KEY}}
    assert c["spec"]["entry"]["source"]["branch"] == "main"                 # normalisée par la route
    dump = json.dumps([list(run.events), run.result, run.cmd, run.to_dict()])
    assert "ROUTE-KEY-1618" not in dump
    with sqlite3.connect(db) as con:
        rows = con.execute("select * from actions where id = ?", (run.id,)).fetchall()
    assert "ROUTE-KEY-1618" not in json.dumps(rows, default=str)


@pytest.mark.parametrize("case,status,code", [
    ("privileges-operator", 403, "gitops-privileges-admin-only"),
    ("no-right", 403, "child-admin-required"),
    ("bad-entry", 400, None),
    ("bad-secrets", 400, None),
    ("unknown-key", 400, None),
])
def test_deploy_refusals_launch_nothing(env, case, status, code):
    over = {}
    if case == "privileges-operator":
        over["entry"] = dict(ENTRY, privileges="cluster")
    if case == "no-right":
        env.right["answer"] = "no"
    if case == "bad-entry":
        over["entry"] = dict(ENTRY, name="Bad Name")
    if case == "bad-secrets":
        over["secrets"] = {"depot": "x"}
    if case == "unknown-key":
        over["tool"] = "flux"
    r = post(env, **over)
    assert r.status_code == status
    if code:
        assert r.get_json()["code"] == code
    assert calls(env) == []


def test_an_admin_may_ask_cluster_privileges(env):
    env.right["role"] = "admin"
    r = post(env, entry=dict(ENTRY, privileges="cluster"))
    assert r.status_code == 202
    wait(r.get_json()["action_id"])
    assert calls(env)[-1]["spec"]["role"] == "admin"


def test_remove_and_list(env):
    r = env.client.delete("/api/capi/harvlab2/gitops/web/web/demo")
    assert r.status_code == 202
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-gitops-remove:web/web:demo"
    argv = calls(env)[-1]["argv"]
    assert argv[0] == "gitops-remove" and argv[argv.index("--name") + 1] == "web/web/demo"
    assert env.client.delete("/api/capi/harvlab2/gitops/web/web/Bad_").status_code == 400
    (env.fake / "gitops-status.json").write_text('{"entries": [], "session": false}')
    r = env.client.get("/api/capi/harvlab2/gitops")
    assert r.status_code == 200 and r.get_json() == {"entries": [], "session": False}
