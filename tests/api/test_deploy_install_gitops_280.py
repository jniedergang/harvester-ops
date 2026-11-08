"""Revue des tâches 5 à 8 (I-10) : un plan enchaîné à une installation
bare-metal (série ou machine seule) reçoit les secrets Git de sa phase
GitOps au lancement ; ils vont au magasin en mémoire du déroulé (jamais
dans la série, le disque ni actions.db) et en sont effacés à la fin.

Fixtures des routes et du déroulé de la 2.4.0 (installation simulée)."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as wapp  # noqa: E402
import test_deploy_routes_240 as tr  # noqa: E402
import test_deploy_runner_240 as trn  # noqa: E402
from test_deploy_routes_240 import env  # noqa: E402,F401
from test_deploy_runner_240 import world  # noqa: E402,F401

GIT_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nGITKEY-I10-4242\n-----END OPENSSH PRIVATE KEY-----\n"
GSEC = {"depot": {"identity": GIT_KEY, "known_hosts": "h k"}}


def _batch(w, monkeypatch, **body):
    monkeypatch.setattr(wapp, "_bm_batch_launch", trn._fake_child("done"))
    monkeypatch.setattr(wapp, "_deploy_install_verify", lambda cluster, expected: (True, expected))
    monkeypatch.setattr(wapp, "_deploy_console_kc", lambda cluster: w["kc"])
    c = w["client"]
    tr.seed(c, plan=tr.plan_body(readiness=False))
    tr.t180.create(c)
    return c.post("/api/baremetal/profiles/rack-a/batch", json=dict(tr.batch_body(), token=trn.SECRET, **body))


def test_git_secrets_reach_the_run_store_and_never_the_disk(world, monkeypatch):
    seen = {}
    real_put = wapp._deploy_gitops_secrets_put

    def put(store, run_id, secrets_):
        seen["secrets"] = json.loads(json.dumps(secrets_ or {}))
        seen["refs"] = None
        real_put(store, run_id, secrets_)
        seen["refs"] = store.refs("plan-run", run_id)
    monkeypatch.setattr(wapp, "_deploy_gitops_secrets_put", put)
    r = _batch(world, monkeypatch, gitops_secrets=GSEC)
    assert r.status_code == 202, r.get_json()
    run = trn.wait(r.get_json()["action_id"])
    assert run.status == "done", run.result
    assert seen["secrets"] == GSEC and seen["refs"]                              # dans le magasin du déroulé
    assert trn._store_empty(run.id)                                             # effacés à la fin
    con = sqlite3.connect(world["db"])
    rows = con.execute("SELECT * FROM actions").fetchall()
    con.close()
    assert rows and all("GITKEY-I10-4242" not in "".join(str(x) for x in row) for row in rows)
    assert "GITKEY-I10-4242" not in json.dumps(run.to_dict()) + json.dumps(list(run.events))


@pytest.mark.parametrize("body,status", [
    ({"gitops_secrets": {"depot": {"identity": ""}}}, 400),                    # ni clé, ni compte
    ({"gitops_secrets": {"Bad_Name": {"identity": GIT_KEY}}}, 400),
    ({"gitops_secrets": "nope"}, 400),
    ({"gitops_secrets": GSEC, "plan": None}, 400),                             # sans plan
])
def test_bad_git_secrets_are_refused_before_anything(world, monkeypatch, body, status):
    launched = []
    monkeypatch.setattr(wapp, "_deploy_with_install", lambda *a, **k: launched.append(1) or ({}, 202))
    r = _batch(world, monkeypatch, **body)
    assert r.status_code == status and launched == []
    assert "GITKEY-I10-4242" not in r.get_data(as_text=True)


def test_the_install_precheck_counts_the_given_secret_names(world):
    """Avant : `gitops-secret-missing` bloquait toute chaîne dont une entrée
    Git a un secret."""
    names = []
    real = wapp._deploy_prepare

    def prepare(*a, **k):
        names.append(k.get("gitops_secrets"))
        return real(*a, **k)
    wapp_prepare = wapp._deploy_prepare
    wapp._deploy_prepare = prepare
    try:
        tr.seed(world["client"], plan=tr.plan_body(readiness=False))
        with wapp.app.test_request_context():
            wapp._deploy_install_precheck("site", {"equipe": "ops"}, "rack-a", "172.16.2.70", gitops_secrets=GSEC)
    finally:
        wapp._deploy_prepare = wapp_prepare
    assert names == [["depot"]]                                                 # noms seulement


def test_the_single_install_route_takes_the_secrets_too():
    """Machine seule : mêmes contrôles et même passage (lecture de la
    source, la route demande une lecture de BMC)."""
    src = (ROOT / "web" / "app.py").read_text()
    body = src[src.index("def api_baremetal_install("):]
    body = body[:body.index("\ndef ")]
    assert 'data.pop("gitops_secrets", None)' in body
    assert "_deploy_install_precheck(plan_name, plan_vars, cluster, vip, gitops_secrets=gsec)" in body
    assert "gitops_secrets=gsec)" in body[body.index("_deploy_with_install("):]
