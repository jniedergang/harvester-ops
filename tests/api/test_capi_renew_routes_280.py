"""2.8.0 (tâche 6) : route du renouvellement des machines. Opérateur ET
droit d'administrer le cluster enfant ; refusé pendant un déroulé de plan ;
essai en lecture ; action suivie `capi-renew:`. Outil simulé de
test_capi_artefact_routes_270."""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as wapp  # noqa: E402
from test_capi_artefact_routes_270 import calls, env, wait  # noqa: E402,F401

URL = "/api/capi/harvlab2/renew/web/web"


def test_role_and_limit():
    from limits import parse_many
    assert wapp.required_role_for(URL, "POST") == "operator"
    src = (ROOT / "web" / "app.py").read_text()
    i = src.index("def api_capi_renew(")
    head = src[src.rindex("@app.route", 0, i):i]
    assert "@requires_auth" in head
    m = re.findall(r'@_rate_limit\("([^"]+)"\)', head)
    assert len(m) == 1 and parse_many(m[0])


def test_renew_launches_the_tool(env):
    r = env.client.post(URL, json={"what": ["registries"], "registries": ["amorcage", "client"]})
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-renew:web/web" and run.status == "done"
    c = calls(env)[-1]
    assert c["argv"][0] == "renew" and c["argv"][c["argv"].index("--what") + 1] == "registries"
    assert c["argv"][c["argv"].index("--timeout") + 1] == "3600"
    assert c["spec"] == {"registries": ["amorcage", "client"]}


def test_dry_run_is_a_read(env):
    (env.fake / "renew.json").write_text('{"changes": [], "secrets_changed": true, "revision": 1}')
    r = env.client.post(URL, json={"what": "ca", "dry_run": True})
    assert r.status_code == 200 and r.get_json()["secrets_changed"] is True
    assert "--dry-run" in calls(env)[-1]["argv"]
    assert not [a for a in wapp.ACTIONS.values() if a.action == "capi-renew:web/web" and a.id not in env.before]


@pytest.mark.parametrize("case,status,code", [
    ("no-right", 403, "child-admin-required"),
    ("plan-running", 409, "renew-plan-running"),
    ("bad-what", 400, None),
    ("unknown-key", 400, None),
    ("spec-without-what", 400, None),
    ("timeout", 400, None),
])
def test_refusals_launch_nothing(env, monkeypatch, case, status, code):
    body = {"what": "registries"}
    if case == "no-right":
        env.right["answer"] = "no"
    if case == "plan-running":
        monkeypatch.setattr(wapp, "_deploy_busy", lambda c: SimpleNamespace(id="d1d1d1d1d1d1"))
    if case == "bad-what":
        body["what"] = "disks"
    if case == "unknown-key":
        body["force"] = True
    if case == "spec-without-what":
        body["oidc"] = {"issuer_url": "https://idp"}
    if case == "timeout":
        body["timeout"] = 30
    r = env.client.post(URL, json=body)
    assert r.status_code == status, r.get_json()
    if code:
        assert r.get_json()["code"] == code
    assert calls(env) == []
