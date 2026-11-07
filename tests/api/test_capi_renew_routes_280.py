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


# --- revue des tâches 5 à 8 : reprise, abandon, exclusion mutuelle (I-1, I-9) -------

def held_lock(env, note):
    """Verrou de fichier du cluster tenu par un autre processus (ligne de
    commande) : un descripteur à part, rendu par l'appelant."""
    import fcntl
    import json
    import os
    d = env.state / "deploy.d"
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / "harvlab2.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.pwrite(fd, (json.dumps(note) + "\n").encode(), 0)
    return fd


def test_finish_resumes_through_the_tool(env):
    r = env.client.post(URL, json={"mode": "finish"})
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-renew:web/web"
    argv = calls(env)[-1]["argv"]
    assert "--finish" in argv and "--what" not in argv and f"--id={run.id}" in argv


def test_abandon_is_for_administrators(env):
    r = env.client.post(URL, json={"mode": "abandon"})
    assert r.status_code == 403 and r.get_json()["code"] == "renew-abandon-admin-only"
    assert calls(env) == []
    env.right["role"] = "admin"
    r = env.client.post(URL, json={"mode": "abandon"})
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-renew-abandon:web/web" and run.required_role == "admin"
    assert "--abandon" in calls(env)[-1]["argv"]


@pytest.mark.parametrize("body", [{"mode": "later"}, {"mode": "finish", "what": "ca"},
                                  {"mode": "abandon", "dry_run": True}, {"mode": "finish", "registries": []}])
def test_bad_modes_are_refused(env, body):
    env.right["role"] = "admin"
    r = env.client.post(URL, json=body)
    assert r.status_code == 400 and calls(env) == []


@pytest.mark.parametrize("note,code", [
    ({"run": "r1", "plan": "p1"}, "renew-plan-running"),                       # plan de la ligne de commande
    ({"run": "a2", "plan": "", "action": "capi-renew", "child": "web/other"}, "renew-busy"),
    ({"run": "u3", "plan": "", "action": "capi-uninstall"}, "capi-uninstall-running"),
])
@pytest.mark.parametrize("body", [{"what": "registries"}, {"what": "ca", "dry_run": True}, {"mode": "finish"}])
def test_refused_while_another_process_holds_the_cluster(env, note, code, body):
    """I-9 : la route relit le verrou de fichier (un déroulé lancé par
    `harvester-plans apply` n'est pas dans ACTIONS)."""
    import os
    fd = held_lock(env, note)
    try:
        r = env.client.post(URL, json=body)
    finally:
        os.close(fd)
    assert r.status_code == 409 and r.get_json()["code"] == code, r.get_json()
    assert calls(env) == []


def test_a_running_renewal_refuses_another_one(env):
    run = wapp.ActionRun("rn1rn1rn1rn1", "capi-renew:web/other", "harvlab2", [])
    run.status = "running"
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[run.id] = run
    r = env.client.post(URL, json={"what": "registries"})
    assert r.status_code == 409 and r.get_json()["code"] == "renew-busy"
    assert calls(env) == []


@pytest.mark.parametrize("source", ["memory", "lock"])
def test_a_running_renewal_refuses_plans_and_undo(env, source):
    """I-9 : un déroulé (et un défaire) ne démarre pas pendant un
    renouvellement, de cette console ou de la ligne de commande."""
    import os
    fd = None
    if source == "memory":
        run = wapp.ActionRun("rn2rn2rn2rn2", "capi-renew:web/web", "harvlab2", [])
        run.status = "running"
        with wapp.ACTIONS_LOCK:
            wapp.ACTIONS[run.id] = run
        holder = None
    else:
        holder = {"run": "cli-renew", "plan": "", "action": "capi-renew", "child": "web/web"}
        fd = held_lock(env, holder)
    try:
        body, status = wapp._capi_lock_refusal("harvlab2", holder)
    finally:
        if fd is not None:
            os.close(fd)
    assert status == 409 and body["code"] == "renew-running"
    assert wapp._capi_lock_refusal("harvlab2", {"run": "x", "plan": "p"}) is None or source == "memory"


def test_plan_launch_and_undo_call_the_shared_refusal():
    """I-9 : les lancements de déroulé et de défaire passent par la même
    garde (source)."""
    src = (ROOT / "web" / "app.py").read_text()
    launch = src[src.index("def _deploy_launch_checked("):src.index("def _deploy_launch_checked(") + 6000]
    assert launch.count("_capi_lock_refusal(cluster") == 2
    undo = src[src.index('label = f"undo:{res.get(\'plan\') or \'\'}"') - 400:]
    assert undo[:2500].count("_capi_lock_refusal(cluster") == 2
