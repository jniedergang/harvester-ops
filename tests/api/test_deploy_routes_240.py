"""2.4.0 : routes des plans de déploiement (tâche 6).

* plans et modèles sous /api/baremetal/ : lecture lecteur, écriture
  administrateur, révision (409), corps invalides (un cas par champ),
  modèle tiré d'une déclaration ;
* /api/deploy/<cluster>/check : chaque code de constat, cluster inconnu,
  plan inconnu ;
* /api/deploy/<cluster> : constat bloquant = 400, rien de lancé ; second
  lancement sur le même cluster = 409 ; rôle opérateur ;
* série avec plan : machine présente sans nom = 409 inchangé et aucune
  action deploy: ; noms tapés et secrets jamais dans la réponse ni le
  résultat ;
* chaque route authentifiée, limite valide (piège flask-limiter).

Le déroulé lui-même (Terraform simulé) est dans test_deploy_runner_240.py.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import test_bm_profiles_180 as t180  # noqa: E402

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJdD pkg"
KC = ("apiVersion: v1\nkind: Config\ncurrent-context: c\ncontexts:\n- name: c\n  context: {cluster: k}\n"
      "clusters:\n- name: k\n  cluster: {server: 'https://172.16.2.60:6443'}\n")


def plan_body(templates=("cles",), variables=("equipe",), readiness=True, **over):
    b = {"description": "essai", "variables": list(variables),
         "phases": {"readiness": {"enabled": readiness, "timeout_minutes": 1},
                    "terraform": {"enabled": True, "templates": [{"template": t} for t in templates]},
                    "capi": {"enabled": False}}}
    b.update(over)
    return b


def template_body(name="{{equipe}}-ops", variables=("equipe",)):
    return {"description": "clés", "variables": list(variables),
            "resources": [{"kind": "ssh_key", "spec": {"name": name, "namespace": "default",
                                                        "public_key": KEY}}]}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    wapp.app.config["TESTING"] = True
    state = tmp_path / "state"
    kc = tmp_path / "kc"
    kc.write_text(KC)
    monkeypatch.setattr(wapp, "_bm_profiles_state", lambda: state)
    monkeypatch.setattr(wapp, "_BM_IMPORT_CACHE", {})
    monkeypatch.setattr(wapp, "current_user", lambda: "alice")
    monkeypatch.setattr(wapp, "current_role", lambda: "admin")
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    monkeypatch.setattr(wapp, "_safe_artifact_name", lambda n: n)
    monkeypatch.setattr(wapp, "load_config_ro",
                        lambda: {"clusters": [{"name": "harvlab", "kubeconfig": str(kc)}]})
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": []})
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: str(kc) if c == "harvlab" else None)
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda k, timeout=2.0: True)
    monkeypatch.setattr(wapp, "_deploy_tf_tools_missing", lambda: [])
    store = wapp._tfs.DeclStore(tmp_path / "tf.db")
    monkeypatch.setattr(wapp, "TF_DECLS", store)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path / "ws")
    started = []
    monkeypatch.setattr(wapp, "_deploy_start", lambda *a, **kw: started.append((a, kw)) or "d1d1d1d1d1d1")
    monkeypatch.setattr(wapp, "track_action",
                        lambda label, cluster, worker, *a: started.append((label, worker, a)) or "b1")
    with wapp.ACTIONS_LOCK:
        before = dict(wapp.ACTIONS)
    yield {"state": state, "client": wapp.app.test_client(), "store": store, "started": started, "kc": str(kc)}
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS.clear()
        wapp.ACTIONS.update(before)


def seed(c, plan=None, templates=None):
    for name, body in (templates or {"cles": template_body()}).items():
        assert c.put(f"/api/baremetal/tf-templates/{name}", json=body).status_code == 201
    assert c.put("/api/baremetal/plans/site", json=plan or plan_body()).status_code == 201


# --- plans et modèles ---------------------------------------------------------

def test_plan_crud_and_revision(env):
    c = env["client"]
    r = c.put("/api/baremetal/plans/site", json=plan_body())
    assert r.status_code == 201 and r.get_json()["revision"] == 1
    assert c.put("/api/baremetal/plans/site", json=plan_body()).status_code == 409   # sans révision lue
    r = c.put("/api/baremetal/plans/site", json=dict(plan_body(description="b"), revision=1))
    assert r.status_code == 200 and r.get_json()["revision"] == 2
    r = c.put("/api/baremetal/plans/site", json=dict(plan_body(), revision=1))
    assert r.status_code == 409 and r.get_json()["current_revision"] == 2
    lst = c.get("/api/baremetal/plans").get_json()
    assert [p["name"] for p in lst["plans"]] == ["site"] and lst["builtins"] == ["cluster", "vip"]
    assert c.get("/api/baremetal/plans/site").get_json()["description"] == "b"
    assert c.get("/api/baremetal/plans/nope").status_code == 404
    assert c.delete("/api/baremetal/plans/site").status_code == 204
    assert c.delete("/api/baremetal/plans/site").status_code == 404


@pytest.mark.parametrize("body,where", [
    ([1], "plan"),
    (plan_body(variables=["vip"]), "variables[vip]"),
    (plan_body(other=1), "other"),
    (dict(plan_body(), phases={"capi": {"enabled": True}}), "phases.capi"),
    (dict(plan_body(), phases={"readiness": {"timeout_minutes": 0}}), "phases.readiness.timeout_minutes"),
    (dict(plan_body(), revision="1"), "revision"),
])
def test_plan_put_refusals(env, body, where):
    r = env["client"].put("/api/baremetal/plans/site", json=body)
    assert r.status_code == 400 and where in r.get_json()["fields"], r.get_json()


@pytest.mark.parametrize("name", ["Site", "a_b", "-a"])
def test_plan_and_template_names_checked(env, name):
    c = env["client"]
    assert c.put(f"/api/baremetal/plans/{name}", json=plan_body()).status_code == 400
    assert c.put(f"/api/baremetal/tf-templates/{name}", json=template_body()).status_code == 400


def test_template_crud_removed_and_refused(env):
    c = env["client"]
    r = c.put("/api/baremetal/tf-templates/cles", json=template_body())
    assert r.status_code == 201 and r.get_json()["removed"] == []
    vm = {"kind": "vm", "spec": {"name": "v", "admin_token": "SECRET-T", "cloudinit": {
        "user_data": "#cloud-config\npassword: SECRET-P\n"}}}
    r = c.put("/api/baremetal/tf-templates/vms", json={"resources": [vm]})
    d = r.get_json()
    assert r.status_code == 201 and sorted(d["removed"]) == ["resources[0].spec.admin_token",
                                                              "resources[0].spec.cloudinit.user_data"]
    assert "SECRET" not in r.get_data(as_text=True)
    r = c.put("/api/baremetal/tf-templates/raw", json={"resources": [
        {"kind": "raw", "spec": {"tf": 'resource "x" "y" { token = "abc" }'}}]})
    assert r.status_code == 400 and r.get_json()["fields"] == ["resources[0].spec.tf"]
    assert [t["name"] for t in c.get("/api/baremetal/tf-templates").get_json()["templates"]] == ["cles", "vms"]
    assert c.delete("/api/baremetal/tf-templates/vms").status_code == 204
    assert c.get("/api/baremetal/tf-templates/vms").status_code == 404


def test_template_from_declaration(env):
    c, store = env["client"], env["store"]
    d = store.create("a" * 12, "harvlab", "web", resources=[
        {"id": "1" * 12, "kind": "ssh_key", "spec": {"name": "ops", "public_key": KEY}},
        {"id": "2" * 12, "kind": "vm", "spec": {"name": "v", "api_token": "SECRET-X"}}])
    r = c.post("/api/baremetal/tf-templates/from-declaration", json={"declaration_id": d["id"], "name": "web"})
    body = r.get_json()
    assert r.status_code == 201 and body["removed"] == ["resources[1].spec.api_token"]
    assert "SECRET-X" not in r.get_data(as_text=True)
    assert c.post("/api/baremetal/tf-templates/from-declaration",
                  json={"declaration_id": d["id"], "name": "web"}).status_code == 409
    assert c.post("/api/baremetal/tf-templates/from-declaration",
                  json={"declaration_id": "f" * 12, "name": "w2"}).status_code == 404
    kvm = store.create("b" * 12, "node2", "k", target_kind="kvm")
    assert c.post("/api/baremetal/tf-templates/from-declaration",
                  json={"declaration_id": kvm["id"], "name": "w3"}).status_code == 400


@pytest.mark.parametrize("role,method,path,status", [
    ("viewer", "GET", "/api/baremetal/plans", 200),
    ("viewer", "PUT", "/api/baremetal/plans/site", 403),
    ("operator", "PUT", "/api/baremetal/plans/site", 403),
    ("operator", "PUT", "/api/baremetal/tf-templates/cles", 403),
    ("operator", "POST", "/api/baremetal/tf-templates/from-declaration", 403),
    ("operator", "DELETE", "/api/baremetal/plans/site", 403),
    ("viewer", "POST", "/api/deploy/harvlab/check", 403),
    ("viewer", "POST", "/api/deploy/harvlab", 403),
    ("operator", "POST", "/api/deploy/harvlab/check", 200),
    ("operator", "POST", "/api/deploy/harvlab", 202),
])
def test_roles(env, monkeypatch, role, method, path, status):
    seed(env["client"])
    monkeypatch.setattr(wapp, "current_role", lambda: role)
    body = {"plan": "site", "variables": {"equipe": "ops"}} if "deploy" in path else plan_body()
    r = env["client"].open(path, method=method, json=body)
    assert r.status_code == status, r.get_json()


def test_every_route_is_authenticated_and_rate_limited():
    from limits import parse_many
    src = (ROOT / "web" / "app.py").read_text()
    routes = [("/api/baremetal/plans", "GET"), ("/api/baremetal/plans/<name>", "PUT"),
              ("/api/baremetal/plans/<name>", "DELETE"), ("/api/baremetal/tf-templates/<name>", "PUT"),
              ("/api/baremetal/tf-templates/<name>", "DELETE"),
              ("/api/baremetal/tf-templates/from-declaration", "POST"),
              ("/api/deploy/<cluster>/check", "POST"), ("/api/deploy/<cluster>", "POST"),
              ("/api/deploy/<cluster>/stop", "POST"), ("/api/deploy/<cluster>/resume", "POST")]
    for route, method in routes:
        head = src.split(f'@app.route("{route}", methods=["{method}"])', 1)[1].split("\ndef ", 1)[0]
        assert "@requires_auth" in head, route
        if method != "GET":
            assert parse_many(re.search(r'@_rate_limit\("([^"]+)"\)', head).group(1)), route


# --- constats -----------------------------------------------------------------

def codes(c, body=None):
    r = c.post("/api/deploy/harvlab/check", json=body or {"plan": "site", "variables": {"equipe": "ops"}})
    assert r.status_code == 200, r.get_json()
    return {f["code"]: f for f in r.get_json()["findings"]}, r.get_json()


def test_check_clean_plan(env):
    seed(env["client"])
    got, body = codes(env["client"])
    assert set(got) == {"readiness-later"} and body["blocking"] is False
    assert body["summary"]["declarations"] == [{"name": "site-cles", "template": "cles",
                                                "exists": False, "origin_match": False}]


@pytest.mark.parametrize("variables,code,detail", [
    ({}, "var-missing", "templates[cles].resources[0].spec.name: missing variable"),
    ({"equipe": "a\nb"}, "var-invalid", "variables.equipe: control character or line break"),
    ({"equipe": "ops", "autre": "x"}, "var-invalid", "variables.autre: not a variable of the plan"),
    ({"equipe": "ops", "cluster": "x"}, "var-invalid", "variables.cluster: built-in variable, set by the console"),
    ({"equipe": "https://u:pw@h"}, "var-invalid", "templates[cles].resources[0].spec.name: credentials in a URL"),
])
def test_check_variable_findings(env, variables, code, detail):
    seed(env["client"], templates={"cles": template_body(name="{{equipe}}")})
    got, body = codes(env["client"], {"plan": "site", "variables": variables})
    assert got[code]["detail"] == detail and got[code]["level"] == "blocking" and body["blocking"]


def test_check_template_missing_and_tools(env, monkeypatch):
    c = env["client"]
    assert c.put("/api/baremetal/plans/site", json=plan_body(templates=("absent",))).status_code == 201
    got, _ = codes(c)
    assert got["template-missing"]["detail"] == "absent"
    c.put("/api/baremetal/tf-templates/cles", json=template_body())
    c.put("/api/baremetal/plans/site", json=dict(plan_body(), revision=1))
    monkeypatch.setattr(wapp, "_deploy_tf_tools_missing", lambda: ["OpenTofu or Terraform"])
    got, _ = codes(c)
    assert got["tf-missing"]["detail"] == "OpenTofu or Terraform not found"


def test_check_unreachable(env, monkeypatch):
    seed(env["client"])
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda k, timeout=2.0: False)
    got, _ = codes(env["client"])
    assert got["cluster-unreachable"]["level"] == "blocking"


def test_check_declaration_states(env):
    c, store = env["client"], env["store"]
    seed(c)
    store.create("c" * 12, "harvlab", "site-cles", resources=[])
    got, _ = codes(c)
    assert "decl-name-taken" in got
    store.delete("c" * 12)
    store.create("c" * 12, "harvlab", "site-cles", resources=[],
                 origin={"plan": "site", "template": "cles", "template_revision": 0, "plan_revision": 1})
    got, body = codes(c)
    assert "decl-name-taken" not in got and got["decl-exists"]["level"] == "info"
    assert got["template-newer"]["level"] == "warning" and not body["blocking"]
    assert body["summary"]["declarations"][0]["origin_match"] is True


def test_check_hand_written_2_5_phase(env):
    d = env["state"] / "plans.d"
    d.mkdir(parents=True)
    (d / "site.yaml").write_text("name: site\nrevision: 1\nphases: {capi: {providers: {}}}\n")
    got, body = codes(env["client"])
    assert got["phase-not-available"]["detail"] == "phases.capi: coming in 2.5+: set enabled: false"
    assert body["blocking"]


@pytest.mark.parametrize("path,body,status", [
    ("/api/deploy/nope/check", {"plan": "site"}, 404),
    ("/api/deploy/harvlab/check", {"plan": "nope"}, 404),
    ("/api/deploy/harvlab/check", {"plan": "Bad"}, 400),
    ("/api/deploy/harvlab/check", {}, 400),
    ("/api/deploy/nope", {"plan": "site"}, 404),
    ("/api/deploy/harvlab", {"plan": "nope"}, 404),
])
def test_check_and_start_unknown(env, path, body, status):
    seed(env["client"])
    assert env["client"].post(path, json=body).status_code == status


# --- lancement ----------------------------------------------------------------

def test_blocking_finding_starts_nothing(env):
    seed(env["client"])
    with wapp.ACTIONS_LOCK:
        before = set(wapp.ACTIONS)
    r = env["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {}})
    assert r.status_code == 400 and r.get_json()["code"] == "blocking-findings"
    assert env["started"] == []
    with wapp.ACTIONS_LOCK:
        assert set(wapp.ACTIONS) == before


def test_start_passes_identity_and_values(env):
    seed(env["client"])
    r = env["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops"},
                                                         "dry_run": True})
    assert r.status_code == 202 and r.get_json()["action_id"] == "d1d1d1d1d1d1"
    (args, kw), = env["started"]
    cluster, kc, dctx = args
    assert cluster == "harvlab" and kc == env["kc"] and kw["dry_run"] is True and kw["user"] == "alice"
    assert dctx["values"] == {"equipe": "ops", "cluster": "harvlab", "vip": "172.16.2.60"}
    assert dctx["rendered"]["cles"][0]["spec"]["name"] == "ops-ops"
    assert kw["required_role"] == "operator"


def test_second_deploy_on_the_same_cluster_is_409(env):
    seed(env["client"])
    run = wapp.ActionRun("e" * 12, "deploy:harvlab", "harvlab", [], dry_run=True)
    run.status = "running"
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[run.id] = run
    r = env["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops"}})
    assert r.status_code == 409 and r.get_json() == {
        "error": "a deployment is already running on this cluster", "code": "deploy-running", "running": "e" * 12}
    assert env["started"] == []
    # supprimer le plan pendant qu'il tourne : refusé
    run.result = {"plan": "site"}
    assert env["client"].delete("/api/baremetal/plans/site").status_code == 409


def test_cluster_shutdown_blocks_a_deploy(env):
    seed(env["client"])
    run = wapp.ActionRun("f" * 12, "shutdown", "harvlab", [])
    run.status = "running"
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[run.id] = run
    r = env["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops"}})
    assert r.status_code == 409 and r.get_json()["code"] == "cluster-busy"


def test_stop_route(env, monkeypatch):
    c = env["client"]
    run = wapp.ActionRun("a" * 12, "deploy:harvlab", "harvlab", [])
    run.status, run.required_role = "running", "admin"
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[run.id] = run
    assert c.post("/api/deploy/other/stop", json={"action_id": run.id}).status_code == 404
    monkeypatch.setattr(wapp, "current_role", lambda: "operator")
    assert c.post("/api/deploy/harvlab/stop", json={"action_id": run.id}).status_code == 403
    monkeypatch.setattr(wapp, "current_role", lambda: "admin")
    r = c.post("/api/deploy/harvlab/stop", json={"action_id": run.id})
    assert r.status_code == 202 and run._cancel is True
    run.status = "done"
    assert c.post("/api/deploy/harvlab/stop", json={"action_id": run.id}).status_code == 409


@pytest.mark.parametrize("prev,status,code", [
    ({"action": "deploy:harvlab", "status": "running"}, 409, "deploy-running"),
    ({"action": "deploy:harvlab", "status": "error", "dry_run": True}, 400, "dry-run"),
    ({"action": "deploy:other", "status": "error"}, 400, None),
    ({"action": "shutdown", "status": "error"}, 400, None),
])
def test_resume_refusals(env, prev, status, code):
    run = wapp.ActionRun("a" * 12, prev["action"], "harvlab", [], dry_run=prev.get("dry_run", False))
    run.status = prev["status"]
    run.result = {"plan": "site", "phases": []}
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[run.id] = run
    # un déroulé vivant tient le verrou de son cluster (revue B2-I2)
    fd = wapp._deploy_cluster_lock("harvlab", run.id, "site")[0] if prev["status"] == "running" else None
    try:
        r = env["client"].post("/api/deploy/harvlab/resume", json={"action_id": run.id})
    finally:
        wapp._flock_release(fd)
    assert r.status_code == status and (code is None or r.get_json()["code"] == code), r.get_json()
    assert env["client"].post("/api/deploy/harvlab/resume", json={"action_id": "../x"}).status_code == 400
    assert env["client"].post("/api/deploy/harvlab/resume", json={"action_id": "9" * 12}).status_code == 404


def test_resume_of_an_install_chain_needs_admin_and_secrets(env, monkeypatch):
    run = wapp.ActionRun("a" * 12, "deploy:rack-a", "rack-a", [])
    run.status = "error"
    run.result = {"plan": "site", "install": {"kind": "batch", "profile": "rack-a", "hostnames": ["node1"],
                                              "batch": {"rows": []}},
                  "phases": [{"id": "install", "status": "error"}]}
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[run.id] = run
    monkeypatch.setattr(wapp, "_deploy_install_verify", lambda cluster, expected: (False, []))
    monkeypatch.setattr(wapp, "current_role", lambda: "operator")
    assert env["client"].post("/api/deploy/rack-a/resume", json={"action_id": run.id}).status_code == 403
    monkeypatch.setattr(wapp, "current_role", lambda: "admin")
    r = env["client"].post("/api/deploy/rack-a/resume", json={"action_id": run.id})
    assert r.status_code == 400 and r.get_json()["code"] == "secrets-required"


# --- série avec plan (garde 132) ----------------------------------------------

def batch_body(confirm=None, **over):
    b = dict(t180.batch(2), token=t180.TOKEN, bmc_password=t180.BMCPW, plan="site",
             plan_variables={"equipe": "ops"})
    if confirm:
        b["rows"][1]["system_confirm"] = confirm
    b.update(over)
    return b


@pytest.mark.real_presence
def test_batch_with_plan_present_machine_refused_nothing_created(env, monkeypatch):
    import test_bm_batch_presence_221 as t221
    states = {"198.51.100.1": "absent", "198.51.100.2": "present"}
    monkeypatch.setattr(wapp, "_bm_system_presence", t221.fake_presence(states))
    c = env["client"]
    seed(c)
    t180.create(c)
    r = c.post("/api/baremetal/profiles/rack-a/batch", json=batch_body())
    d = r.get_json()
    assert r.status_code == 409 and d["fields"] == ["system_confirm"] and d["refused"] == ["198.51.100.2"]
    assert env["started"] == []
    text = r.get_data(as_text=True)
    assert t180.TOKEN not in text and t180.BMCPW not in text


@pytest.mark.real_presence
def test_batch_with_plan_confirmed_starts_a_deploy_without_secrets_or_names(env, monkeypatch):
    import test_bm_batch_presence_221 as t221
    states = {"198.51.100.1": "absent", "198.51.100.2": "present"}
    monkeypatch.setattr(wapp, "_bm_system_presence", t221.fake_presence(states))
    c = env["client"]
    seed(c, plan=plan_body(readiness=False))
    t180.create(c)
    r = c.post("/api/baremetal/profiles/rack-a/batch", json=batch_body(confirm=t221.serial("198.51.100.2")))
    d = r.get_json()
    assert r.status_code == 202, d
    assert d == {"action_id": "d1d1d1d1d1d1", "cluster": "rack-a", "nodes": 2, "plan": "site"}
    (args, kw), = env["started"]
    install = kw["install"]
    assert kw["required_role"] == "admin" and install["hostnames"] == ["node1", "node2"]
    text = repr(install) + repr(kw.get("store"))
    for secret in (t180.TOKEN, t180.BMCPW, t221.serial("198.51.100.2")):
        assert secret not in repr(install["record"]) and secret not in repr(install["rows"])
    assert t180.TOKEN not in text
    # les secrets sont dans le magasin du déroulé, sous son identifiant
    store, rid = kw["store"], kw["run_id"]
    assert store.get(wapp._sst.make_ref("plan-run", f"{rid}/install/0", "token")) == t180.TOKEN
    assert store.get(wapp._sst.make_ref("plan-run", f"{rid}/bmc/1", "bmc_password")) == t180.BMCPW
    store.delete_tree("plan-run", rid)


def test_batch_with_unknown_plan(env):
    t180.create(env["client"])
    r = env["client"].post("/api/baremetal/profiles/rack-a/batch", json=batch_body(plan="nope"))
    assert r.status_code == 404 and env["started"] == []
    r = env["client"].post("/api/baremetal/profiles/rack-a/batch", json=batch_body(plan="Bad"))
    assert r.status_code == 400


@pytest.mark.real_presence
def test_plan_findings_come_before_any_bmc_read(env, monkeypatch):
    """Revue m5 : un plan qui ne peut pas démarrer (variable manquante) est
    refusé AVANT de lire les BMC de la série ou de la machine."""
    reads = []
    monkeypatch.setattr(wapp, "_bm_system_presence", lambda *a, **k: reads.append(a) or {"state": "absent"})
    c = env["client"]
    seed(c)
    t180.create(c)
    r = c.post("/api/baremetal/profiles/rack-a/batch", json=batch_body(plan_variables={}))
    d = r.get_json()
    assert r.status_code == 400 and d["code"] == "blocking-findings", d
    assert [f["code"] for f in d["findings"] if f["level"] == "blocking"] == ["var-missing"]
    assert reads == [] and env["started"] == []


# --- revues B2-I1 et B2-I2 : processus multiples, reprise après interruption ---

_HOLDER = r"""
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, sys.argv[2].encode())
print("held", flush=True)
sys.stdin.readline()
"""


@pytest.fixture
def other_process():
    import subprocess
    procs = []

    def hold(path, note):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path), note], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "held"
        procs.append(p)
        return p
    yield hold
    for p in procs:
        if p.poll() is None:
            p.communicate("\n", timeout=5)


def _lock(env, cluster="harvlab"):
    return env["state"] / "deploy.d" / f"{cluster}.lock"


def test_cluster_locked_by_another_process(env, other_process):
    """Un déroulé de la ligne de commande (autre processus) tient le cluster :
    la console refuse un second lancement et la suppression du plan ; libéré,
    le lancement passe. Mutation jouée : verrou de cluster non pris."""
    c = env["client"]
    seed(c)
    p = other_process(_lock(env), '{"run": "c0c0c0c0c0c0", "plan": "site"}')
    r = c.post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "a"}})
    assert r.status_code == 409 and r.get_json() == {
        "error": "a deployment is already running on this cluster", "code": "deploy-running",
        "running": "c0c0c0c0c0c0"}
    assert env["started"] == []
    r = c.delete("/api/baremetal/plans/site")
    assert r.status_code == 409 and r.get_json()["running"] == "c0c0c0c0c0c0"
    p.communicate("\n", timeout=5)
    r = c.post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "a"}})
    assert r.status_code == 202
    wapp._flock_release(env["started"][-1][1]["lock_fd"])


def _persist_deploy_row(run_id, status="running", result=None, dry_run=False):
    run = wapp.ActionRun(run_id, "deploy:harvlab", "harvlab", [], dry_run=dry_run)
    run.status = status
    run.result = dict({"plan": "site", "plan_revision": 1, "cluster": "harvlab", "variables": {"equipe": "a"},
                       "phases": [{"id": "readiness", "status": "done"}]}, **(result or {}))
    wapp._actions_persist(run)
    return run


@pytest.fixture
def actions_db(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "ACTIONS_DB", tmp_path / "actions.db")
    wapp._actions_init_db()
    return tmp_path / "actions.db"


def test_interrupted_run_is_resumable_after_a_restart(env, actions_db, other_process):
    """Revue B2-I1 : console redémarrée en plein déroulé, la ligne reste
    `running` dans actions.db et n'est plus en mémoire. Verrou libre = plus
    personne ne la fait tourner : reprise acceptée. Verrou tenu par elle dans
    un autre processus : 409. Mutation jouée : statut de la base pris tel quel."""
    c = env["client"]
    seed(c)
    _persist_deploy_row("e1e1e1e1e1e1")
    p = other_process(_lock(env), '{"run": "e1e1e1e1e1e1", "plan": "site"}')
    r = c.post("/api/deploy/harvlab/resume", json={"action_id": "e1e1e1e1e1e1"})
    assert r.status_code == 409 and r.get_json()["code"] == "deploy-running"
    p.communicate("\n", timeout=5)
    r = c.post("/api/deploy/harvlab/resume", json={"action_id": "e1e1e1e1e1e1"})
    assert r.status_code == 202 and r.get_json()["resumes"] == "e1e1e1e1e1e1", r.get_json()
    wapp._flock_release(env["started"][-1][1]["lock_fd"])


def test_orphan_rows_are_marked_interrupted(env, actions_db, other_process):
    """Au chargement : les lignes `deploy:` orphelines passent `interrupted`
    (ended_at posé : le ramasse-miettes les garde) ; celle d'un déroulé vivant
    ailleurs ne bouge pas ; les déclarations d'essai d'un dry-run interrompu
    sont retirées."""
    store = env["store"]
    trial = store.create("7" * 12, "harvlab", "site-cles", resources=[])
    _persist_deploy_row("f1f1f1f1f1f1")
    _persist_deploy_row("f2f2f2f2f2f2", dry_run=True, result={"trial_declarations": [trial["id"]]})
    _persist_deploy_row("f3f3f3f3f3f3", status="done")
    lock_other = env["state"] / "deploy.d" / "harvlab2.lock"
    run = wapp.ActionRun("f4f4f4f4f4f4", "deploy:harvlab2", "harvlab2", [])
    run.status, run.result = "running", {"plan": "site"}
    wapp._actions_persist(run)
    p = other_process(lock_other, '{"run": "f4f4f4f4f4f4", "plan": "site"}')
    t0 = wapp.time.time()
    assert sorted(wapp._deploy_mark_orphans()) == ["f1f1f1f1f1f1", "f2f2f2f2f2f2"]
    for rid, want in (("f1f1f1f1f1f1", "interrupted"), ("f2f2f2f2f2f2", "interrupted"),
                      ("f3f3f3f3f3f3", "done"), ("f4f4f4f4f4f4", "running")):
        d, _ev = wapp._actions_db_get(rid)
        assert d["status"] == want, (rid, d["status"])
        if want == "interrupted":
            assert d["ended_at"] >= t0
    assert store.get(trial["id"]) is None
    p.communicate("\n", timeout=5)


# --- F-I1 : le cloud-init d'un modèle se lit en opérateur, comme celui d'une
# déclaration (2.3.0) ------------------------------------------------------------

UD = "#cloud-config\nruncmd:\n  - curl -H 'X-Key: k-123' https://example.invalid\n"
ND = "version: 2\nethernets: {eth0: {dhcp4: true}}\n"
CI_TEMPLATE = {"description": "vms", "variables": [], "resources": [
    {"kind": "vm", "spec": {"name": "v1", "cloudinit": [{"user_data": UD, "network_data": ND}]}},
    {"kind": "vm", "spec": {"name": "v2", "cloudinit": {"user_data": UD, "network_data": ND}}},
    {"kind": "raw", "spec": {"tf": 'resource "harvester_cloudinit_secret" "c" {\n  name = "c"\n'
                                   '  namespace = "default"\n  network_data = "version: 2"\n}\n'}},
    {"kind": "ssh_key", "spec": {"name": "k", "namespace": "default", "public_key": KEY}},
]}


@pytest.mark.parametrize("role,hidden", [("viewer", True), ("operator", False), ("admin", False)])
def test_template_cloudinit_hidden_for_viewers(env, monkeypatch, role, hidden):
    c = env["client"]
    r = c.put("/api/baremetal/tf-templates/ci", json=CI_TEMPLATE)
    assert r.status_code == 201, r.get_json()
    monkeypatch.setattr(wapp, "current_role", lambda: role)
    r = c.get("/api/baremetal/tf-templates/ci")
    assert r.status_code == 200
    res = r.get_json()["resources"]
    lst, obj, raw, key = (x["spec"] for x in res)
    want_ud, want_nd = ("(hidden)", "(hidden)") if hidden else (UD, ND)
    assert (lst["cloudinit"][0]["user_data"], lst["cloudinit"][0]["network_data"]) == (want_ud, want_nd)
    assert (obj["cloudinit"]["user_data"], obj["cloudinit"]["network_data"]) == (want_ud, want_nd)
    assert (raw["tf"] == "(hidden)") is hidden
    assert key["public_key"] == KEY                      # le reste se lit toujours
    if hidden:
        assert "k-123" not in r.get_data(as_text=True)
    # la liste des modèles ne porte aucune ressource
    lst_body = c.get("/api/baremetal/tf-templates").get_data(as_text=True)
    assert "k-123" not in lst_body and "version: 2" not in lst_body
