"""2.8.0 (tâche 2) : routes de l'import dans un Rancher externe. Écrire dans
Rancher = opérateur ET droit d'administrer le cluster enfant ET session
Rancher (points 221, 222) ; compte local : 409 `rancher-session-needed`
sans rien lancer. Outil simulé de test_capi_artefact_routes_270."""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as wapp  # noqa: E402
from test_capi_artefact_routes_270 import calls, env, wait  # noqa: E402,F401

SESSION = {"url": "https://rancher.home.zypp.fr", "cid": "c-sg2q6", "token_file": "/nonexistent"}


@pytest.fixture
def session(env, monkeypatch):
    box = {"r": dict(SESSION)}
    monkeypatch.setattr(wapp, "_session_rancher", lambda c: (("/kc" if c == "harvlab2" else None), box["r"]))
    return box


@pytest.mark.parametrize("method,path,role", [
    ("POST", "/api/capi/harvlab2/rancher-import", "operator"),
    ("DELETE", "/api/capi/harvlab2/rancher-import/web/web", "operator"),
    ("GET", "/api/capi/harvlab2/rancher-status", "viewer"),
    ("GET", "/api/capi/harvlab2/oidc-kubeconfig/web/web", "viewer"),
    ("POST", "/api/capi/harvlab2/gitops", "operator"),
    ("DELETE", "/api/capi/harvlab2/gitops/web/web/demo", "operator"),
    ("GET", "/api/capi/harvlab2/gitops", "viewer"),
])
def test_roles(method, path, role):
    assert wapp.required_role_for(path, method) == role


def test_limits_of_every_new_write_route_are_readable():
    from limits import parse_many
    src = (ROOT / "web" / "app.py").read_text()
    for fn in ("api_capi_rancher_import", "api_capi_rancher_unimport", "api_capi_gitops", "api_capi_gitops_remove"):
        i = src.index(f"def {fn}(")
        head = src[src.rindex("@app.route", 0, i):i]
        assert "@requires_auth" in head, fn
        m = re.findall(r'@_rate_limit\("([^"]+)"\)', head)
        assert len(m) == 1 and parse_many(m[0]), fn
    for fn in ("api_capi_rancher_status", "api_capi_oidc_kubeconfig", "api_capi_gitops_list"):
        i = src.index(f"def {fn}(")
        assert "@requires_auth" in src[src.rindex("@app.route", 0, i):i], fn


def test_import_launches_the_tool_with_the_person(env, session):
    r = env.client.post("/api/capi/harvlab2/rancher-import", json={"cluster": "web/web"})
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-rancher-import:web/web" and run.status == "done"
    argv = calls(env)[-1]["argv"]
    assert argv[0] == "rancher-import" and argv[argv.index("--name") + 1] == "web/web"
    assert argv[argv.index("--by") + 1] == "alice" and "--harvester-name=harvlab2" in argv
    assert env.right["asked"][-1][-4:] == ["get", "secrets/web-kubeconfig", "-n", "web"]


@pytest.mark.parametrize("case,status,code", [
    ("local-account", 409, "rancher-session-needed"),
    ("no-right", 403, "child-admin-required"),
    ("bad-name", 400, None),
    ("unknown-key", 400, None),
])
def test_import_refusals_launch_nothing(env, session, case, status, code):
    body = {"cluster": "web/web"}
    if case == "local-account":
        session["r"] = None
    if case == "no-right":
        env.right["answer"] = "no"
    if case == "bad-name":
        body = {"cluster": "Web"}
    if case == "unknown-key":
        body["run"] = "x"
    r = env.client.post("/api/capi/harvlab2/rancher-import", json=body)
    assert r.status_code == status
    if code:
        assert r.get_json()["code"] == code
    assert calls(env) == []


def test_unimport_launches_the_tool(env, session):
    r = env.client.delete("/api/capi/harvlab2/rancher-import/web/web")
    assert r.status_code == 202
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-rancher-unimport:web/web"
    assert calls(env)[-1]["argv"][0] == "rancher-unimport"
    session["r"] = None
    r = env.client.delete("/api/capi/harvlab2/rancher-import/web/web")
    assert r.status_code == 409 and r.get_json()["code"] == "rancher-session-needed"


def test_status_is_a_read(env, session):
    (env.fake / "rancher-status.json").write_text('{"session": null, "clusters": []}')
    r = env.client.get("/api/capi/harvlab2/rancher-status")
    assert r.status_code == 200 and r.get_json() == {"session": None, "clusters": []}
    assert calls(env)[-1]["argv"][0] == "rancher-status"
