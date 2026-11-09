"""2.10.0 (tâche 10) : routes External Secrets d'un cluster enfant et carte
de Paramètres > Secrets. Poser, activer un namespace, retirer :
administrateur ET droit sur le cluster enfant (outil simulé, actions
suivies) ; écrire une valeur : opérateur avec le droit, jeton de la
personne, jamais relue ni rendue, absente d'actions.db."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as wapp  # noqa: E402
import child_eso as ce  # noqa: E402
from test_capi_artefact_routes_270 import calls, env, wait  # noqa: E402,F401

KEY = ce.cluster_key("harvlab2", "web", "web")
WITNESS = "valeur-temoin-route-2100"
BASE = "/api/capi/harvlab2/eso/web/web"


def status_doc(env, namespaces=("team-a",), auth=True):
    doc = {"cluster": "web/web", "key": KEY, "namespaces": list(namespaces),
           "auth": {"mount": "m", "applied": True} if auth else None}
    (env.fake / "eso-status.json").write_text(json.dumps(doc))


@pytest.fixture
def admin(env):
    env.right["role"] = "admin"
    return env


@pytest.fixture
def bao(env, monkeypatch):
    """OpenBao actif, réglage ESO du cluster ; le magasin réel n'est pas
    appelé (put_write_only enregistré)."""
    s = {"mode": "b1", "addr": "https://127.0.0.1:8210", "prefix": "consoles/abc123def456",
         "console_id": "abc123def456",
         "eso": {"listener": "172.16.1.11:8212", "clusters": {KEY: {"harvester": "harvlab2", "namespace": "web",
                                                                     "cluster": "web", "namespaces": ["team-a"],
                                                                     "keys_sha256": "ab", "mount": "m"}}}}
    monkeypatch.setattr(wapp, "_secrets_settings", lambda: s)
    writes = []

    class Store:
        last_metadata_error = None

        def put_write_only(self, cls, path, fields, identity=None, action=None):
            writes.append((cls, path, dict(fields), identity, action))
            if WITNESS + "-deny" in fields.values():
                raise wapp._sst.SecretError("openbao-denied", cls, path, "permission denied")
            return 3
    monkeypatch.setattr(wapp._sst, "store", lambda *a, **k: Store())
    return {"settings": s, "writes": writes}


# ---------------------------------------------------------------------------
# Rôles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,path,role", [
    ("GET", BASE, "viewer"),
    ("POST", BASE, "operator"),
    ("DELETE", BASE, "operator"),
    ("POST", BASE + "/namespaces/team-b", "operator"),
    ("POST", BASE + "/secrets", "operator"),
    ("GET", "/api/secrets/eso", "admin"),
    ("POST", "/api/secrets/eso/listener", "admin"),
    ("POST", "/api/secrets/eso/keys", "admin"),
])
def test_roles(method, path, role):
    assert wapp.required_role_for(path, method) == role


def test_rate_limits_are_valid():
    import limits
    for fn in (wapp.api_capi_eso_deploy, wapp.api_capi_eso_remove, wapp.api_capi_eso_namespace,
               wapp.api_capi_eso_secret, wapp.api_secrets_eso_listener, wapp.api_secrets_eso_keys):
        assert fn is not None
    for text in ("20/minute", "10/minute", "30/minute", "5/minute"):
        assert limits.parse_many(text)


# ---------------------------------------------------------------------------
# Poser, vérifier, retirer
# ---------------------------------------------------------------------------
def test_deploy_launches_eso_deploy(admin):
    r = admin.client.post(BASE, json={"namespaces": ["team-b", "team-a"],
                                      "chart": {"registry": "amorcage", "package": "eso", "version": "2.14.0"}})
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-eso:web/web" and run.status == "done"
    c = calls(admin)[-1]
    assert c["argv"][0] == "eso-deploy"
    assert c["spec"] == {"cluster": "web/web", "namespaces": ["team-a", "team-b"], "by": "alice", "role": "admin",
                         "chart": {"registry": "amorcage", "package": "eso", "version": "2.14.0", "item": "eso"}}


@pytest.mark.parametrize("case,status,code", [
    ("operator", 403, "eso-admin-only"),
    ("no-right", 403, "child-admin-required"),
    ("system-ns", 400, None),
    ("no-ns", 400, "eso-no-namespace"),
    ("unknown-key", 400, None),
    ("bad-name", 400, None),
])
def test_deploy_refusals_launch_nothing(env, case, status, code):
    env.right["role"] = "admin"
    body, path = {"namespaces": ["team-a"]}, BASE
    if case == "operator":
        env.right["role"] = "operator"
    if case == "no-right":
        env.right["answer"] = "no"
    if case == "system-ns":
        body["namespaces"] = ["kube-system"]
    if case == "no-ns":
        body["namespaces"] = []
    if case == "unknown-key":
        body["extra"] = 1
    if case == "bad-name":
        path = "/api/capi/harvlab2/eso/web/Bad_Name"
    r = env.client.post(path, json=body)
    assert r.status_code == status, r.get_json()
    if code:
        assert r.get_json()["code"] == code
    assert calls(env) == []


def test_dry_run_returns_the_findings_with_the_child_right(env):
    (env.fake / "eso-check.json").write_text(json.dumps({"blocked": False, "findings": [], "key": KEY}))
    env.right["answer"] = "no"
    r = env.client.post(BASE, json={"namespaces": ["team-a"], "dry_run": True})
    assert r.status_code == 200
    d = r.get_json()
    assert d["blocked"] is True and [f["code"] for f in d["findings"]] == ["child-admin-required"]
    assert calls(env)[-1]["argv"][0] == "eso-check"
    assert calls(env)[-1]["spec"]["role"] == "operator"


def test_remove(admin):
    r = admin.client.delete(BASE + "?keep_chart=1")
    assert r.status_code == 202
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-eso-remove:web/web"
    argv = calls(admin)[-1]["argv"]
    assert argv[0] == "eso-remove" and argv[argv.index("--name") + 1] == "web/web" and "--keep-chart" in argv
    assert argv[argv.index("--role") + 1] == "admin"


def test_remove_is_for_administrators(env):
    assert env.client.delete(BASE).status_code == 403 and calls(env) == []


def test_status(env):
    status_doc(env)
    r = env.client.get(BASE)
    assert r.status_code == 200 and r.get_json()["key"] == KEY
    assert calls(env)[-1]["argv"][:3] == ["eso-status", "--name", "web/web"] or "eso-status" in calls(env)[-1]["argv"]


# ---------------------------------------------------------------------------
# Namespaces (décision 304 B)
# ---------------------------------------------------------------------------
def test_enable_a_namespace_rewrites_the_list(admin):
    status_doc(admin, ["team-a"])
    r = admin.client.post(BASE + "/namespaces/team-b")
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-eso-namespace:web/web:add:team-b"
    assert calls(admin)[-1]["spec"]["namespaces"] == ["team-a", "team-b"]


def test_disable_a_namespace(admin):
    status_doc(admin, ["team-a", "team-b"])
    r = admin.client.delete(BASE + "/namespaces/team-b")
    assert r.status_code == 202
    wait(r.get_json()["action_id"])
    assert calls(admin)[-1]["spec"]["namespaces"] == ["team-a"]


@pytest.mark.parametrize("method,team,doc,status,code", [
    ("DELETE", "team-a", (["team-a"], True), 409, "eso-last-namespace"),
    ("POST", "team-b", (["team-a"], False), 409, "eso-not-set"),
    ("POST", "kube-system", (["team-a"], True), 400, "eso-system-namespace"),
    ("POST", "team-a", (["team-a"], True), 200, None),
])
def test_namespace_refusals(admin, method, team, doc, status, code):
    status_doc(admin, doc[0], doc[1])
    r = admin.client.open(BASE + f"/namespaces/{team}", method=method)
    assert r.status_code == status, r.get_json()
    if code:
        assert r.get_json()["code"] == code
    assert [c for c in calls(admin) if c["argv"][0] == "eso-deploy"] == []


# ---------------------------------------------------------------------------
# Écrire une valeur
# ---------------------------------------------------------------------------
def test_write_a_value_with_the_identity_of_the_request(env, bao, tmp_path, monkeypatch):
    db = tmp_path / "actions.db"
    monkeypatch.setattr(wapp, "ACTIONS_DB", db)
    wapp._actions_init_db()
    seen = []
    monkeypatch.setattr(wapp, "_secret_identity", lambda cls: seen.append(cls) or wapp._sst.Identity("person", "alice",
                                                                                                      session_id="s1",
                                                                                                      mount="jwt-m"))
    r = env.client.post(BASE + "/secrets", json={"namespace": "team-a", "name": "db",
                                                 "fields": {"password": WITNESS, "user": "app"}})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["version"] == 3 and d["identity"] == "person" and WITNESS not in json.dumps(d)
    cls, path, fields, identity, action = bao["writes"][0]
    assert (cls, path) == ("eso", f"{KEY}/team-a/db") and fields == {"password": WITNESS, "user": "app"}
    assert identity.kind == "person" and seen == ["eso"] and action == d["action_id"]
    run = wapp.ACTIONS[d["action_id"]]
    assert run.status == "done" and run.secret_identity == "person"
    dump = json.dumps([list(run.events), run.result, run.cmd, run.to_dict()])
    assert WITNESS not in dump and "password" in dump
    with sqlite3.connect(db) as con:
        rows = con.execute("select * from actions where id = ?", (run.id,)).fetchall()
    assert WITNESS not in json.dumps(rows, default=str)


@pytest.mark.parametrize("case,status,code", [
    ("viewer", 403, None),
    ("no-right", 403, "child-admin-required"),
    ("not-enabled", 409, "eso-namespace-not-enabled"),
    ("system", 400, None),
    ("no-fields", 400, None),
    ("bad-field", 400, None),
    ("denied", 403, "openbao-denied"),
])
def test_write_refusals(env, bao, case, status, code):
    body = {"namespace": "team-a", "name": "db", "fields": {"password": WITNESS}}
    if case == "viewer":
        env.right["role"] = "viewer"
    if case == "no-right":
        env.right["answer"] = "no"
    if case == "not-enabled":
        body["namespace"] = "team-b"
    if case == "system":
        body["namespace"] = "kube-system"
    if case == "no-fields":
        body["fields"] = {}
    if case == "bad-field":
        body["fields"] = {"pass word": "x"}
    if case == "denied":
        body["fields"] = {"password": WITNESS + "-deny"}
    r = env.client.post(BASE + "/secrets", json=body)
    assert r.status_code == status, r.get_json()
    assert WITNESS not in json.dumps(r.get_json())
    if code:
        assert r.get_json()["code"] == code
    if case != "denied":
        assert bao["writes"] == []


def test_write_needs_openbao(env, monkeypatch):
    monkeypatch.setattr(wapp, "_secrets_settings", lambda: {"mode": "local"})
    r = env.client.post(BASE + "/secrets", json={"namespace": "team-a", "name": "db", "fields": {"a": "b"}})
    assert r.status_code == 409 and r.get_json()["code"] == "eso-needs-openbao"


# ---------------------------------------------------------------------------
# Carte de Paramètres > Secrets
# ---------------------------------------------------------------------------
def test_settings_card(admin, bao):
    env = admin
    wapp._ESO_KEYS["seen"] = {"harvlab2/web/web": True}
    try:
        r = env.client.get("/api/secrets/eso")
    finally:
        wapp._ESO_KEYS["seen"] = {}
    d = r.get_json()
    assert d["listener"] == "172.16.1.11:8212" and d["port"] == 8212
    assert d["clusters"][0]["key"] == KEY and d["clusters"][0]["keys_changed"] is True
    assert "ca_pem" not in json.dumps(d)


def test_settings_listener_and_keys_launch_the_tool(admin, bao, monkeypatch):
    env = admin
    seen = []
    monkeypatch.setattr(wapp, "_secrets_tool_action", lambda label, args, **kw: seen.append((label, args)) or
                        (type("R", (), {"id": "abc"})(), None))
    assert env.client.post("/api/secrets/eso/listener", json={"address": "172.16.1.11:8212"}).status_code == 202
    assert env.client.post("/api/secrets/eso/listener", json={"off": True}).status_code == 202
    assert env.client.post("/api/secrets/eso/listener", json={"address": "0.0.0.0:8212"}).status_code == 400
    assert env.client.post("/api/secrets/eso/keys", json={"cluster": "harvlab2/web/web"}).status_code == 202
    assert env.client.post("/api/secrets/eso/keys", json={"cluster": "web/web"}).status_code == 400
    assert seen[0] == ("secrets:eso-listener", ["eso", "listener", "--addr", "172.16.1.11:8212"])
    assert seen[1] == ("secrets:eso-listener", ["eso", "listener", "--off"])
    label, args = seen[2]
    assert label == "secrets:eso-keys:harvlab2/web/web"
    assert args[:6] == ["eso", "keys", "--cluster", "harvlab2/web/web", "--refresh", "--kubeconfig"]


def test_keys_watch_is_read_only_and_hourly(env, bao, monkeypatch):
    runs = []
    monkeypatch.setattr(wapp, "_deploy_console_kc", lambda c: "/kc")
    wapp._ESO_KEYS.update(at=0.0, seen={})
    try:
        out = wapp._eso_keys_tick(now=10_000, runner=lambda argv: runs.append(argv) or {"ok": True, "changed": True})
        assert out == {"harvlab2/web/web": True}
        assert "--refresh" not in runs[0] and runs[0][-4:] == ["--cluster", "harvlab2/web/web", "--kubeconfig", "/kc"]
        assert wapp._eso_keys_tick(now=10_100, runner=lambda argv: runs.append(argv)) == out    # pas avant 1 h
        assert len(runs) == 1
        assert wapp._secrets_banner()["eso_keys_changed"] is True
    finally:
        wapp._ESO_KEYS.update(at=0.0, seen={})


def test_cluster_create_with_eso_is_for_administrators(env, bao):
    body = {"name": "c1", "namespace": "c1", "external_secrets": {"namespaces": ["team-a"]}}
    r = env.client.post("/api/capi/harvlab2/cluster-create", json=body)
    assert r.status_code == 403 and r.get_json()["code"] == "eso-admin-only"
    assert calls(env) == []
