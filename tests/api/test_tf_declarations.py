"""v1.54.0 : les déclarations Terraform gardées par la console, chacune avec
son propre état.

Audit du 26/09/2026 : toutes les déclarations d'un cluster partageaient un
seul état (appliquer l'une appliquait tout le cluster, deux VMs du même nom
s'écrasaient), un Dry-run modifiait les fichiers, une ressource détruite à
l'unité restait dans sa déclaration et revenait au prochain apply. Terraform
est simulé ici ; l'essai réel est fait sur harv1.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402
import tf_state as st  # noqa: E402

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJdD pkg"


def sshkey(rid, name):
    return {"id": rid, "kind": "ssh_key", "spec": {"name": name, "namespace": "default", "public_key": KEY}}


def vm(rid, name):
    return {"id": rid, "kind": "vm", "spec": {
        "name": name, "namespace": "default", "cpu": 1, "memory": "1Gi",
        "disk": [{"name": "rootdisk", "image": "default/img", "size": "10Gi"}],
        "network_interface": [{"name": "nic-1", "network_name": "default/production"}],
        "cloudinit": [{"type": "noCloud", "user_data": "#cloud-config"}]}}


# ---------------------------------------------------------------------------
# Les routes (serveur de test)
# ---------------------------------------------------------------------------
def test_create_rename_and_conflicts(api):
    status, d = api("POST", "/api/tf-declarations", expect_status=None,
                    json_body={"cluster": "harv-fake", "name": "web", "resources": [sshkey("0" * 12, "ops")]})
    assert status == 201, d
    assert d["revision"] == 1 and d["resources"][0]["address"] == "harvester_ssh_key.ops"
    assert d["deployed"] == [] and d["last_plan"] is None
    status, _ = api("POST", "/api/tf-declarations", expect_status=None,
                    json_body={"cluster": "harv-fake", "name": "WEB"})
    assert status == 409                                   # doublon, quelle que soit la casse
    status, r = api("PUT", f"/api/tf-declarations/{d['id']}", expect_status=None,
                    json_body={"revision": 1, "name": "web-2026"})
    assert status == 200 and r["name"] == "web-2026" and r["revision"] == 2
    status, c = api("PUT", f"/api/tf-declarations/{d['id']}", expect_status=None,
                    json_body={"revision": 1, "description": "late"})
    assert status == 409 and c["code"] == "conflict" and c["current"]["name"] == "web-2026"
    status, lst = api("GET", "/api/tf-declarations?cluster=harv-fake")
    assert d["id"] in [x["id"] for x in lst["declarations"]]
    assert api("DELETE", f"/api/tf-declarations/{d['id']}", expect_status=None)[0] == 200


def test_malformed_requests(api):
    assert api("POST", "/api/tf-declarations", expect_status=None,
               json_body={"cluster": "nope", "name": "x"})[0] == 404
    assert api("POST", "/api/tf-declarations", expect_status=None,
               json_body={"cluster": "harv-fake", "name": ""})[0] == 400
    assert api("GET", "/api/tf-declarations/not-an-id", expect_status=None)[0] == 404
    assert api("PUT", "/api/tf-declarations/..%2F..", expect_status=None)[0] in (400, 404)


def test_a_deployed_declaration_cannot_be_deleted(api, test_config):
    status, d = api("POST", "/api/tf-declarations", expect_status=None,
                    json_body={"cluster": "harv-fake", "name": "deployed", "resources": [sshkey("1" * 12, "k")]})
    ws = test_config["root"] / "terraform" / "harv-fake" / "decls" / d["id"]
    ws.mkdir(parents=True)
    (ws / st.STATE_FILE).write_text(json.dumps({"version": 4, "resources": [
        {"mode": "managed", "type": "harvester_ssh_key", "name": "k", "instances": []}]}))
    status, out = api("DELETE", f"/api/tf-declarations/{d['id']}", expect_status=None)
    assert status == 409 and out["deployed"] == ["harvester_ssh_key.k"]
    status, g = api("GET", f"/api/tf-declarations/{d['id']}")
    assert g["deployed"] == ["harvester_ssh_key.k"]
    (ws / st.STATE_FILE).unlink()
    assert api("DELETE", f"/api/tf-declarations/{d['id']}", expect_status=None)[0] == 200
    assert not ws.exists()


def test_the_live_view_joins_every_declaration_state(api, test_config):
    status, d = api("POST", "/api/tf-declarations", expect_status=None,
                    json_body={"cluster": "harv-fake", "name": "live-join"})
    root = test_config["root"] / "terraform" / "harv-fake"
    for ws, name in ((root, "legacy"), (root / "decls" / d["id"], "mine")):
        ws.mkdir(parents=True, exist_ok=True)
        (ws / st.STATE_FILE).write_text(json.dumps({"version": 4, "resources": [
            {"mode": "managed", "type": "harvester_ssh_key", "name": name, "instances": []}]}))
    status, out = api("GET", "/api/terraform/harv-fake/state")
    by = {x["address"]: x for x in out["resources_detail"]}
    assert by["harvester_ssh_key.legacy"]["workspace"] == "shared"
    assert by["harvester_ssh_key.mine"]["declaration_id"] == d["id"]
    assert by["harvester_ssh_key.mine"]["declaration_name"] == "live-join"
    (root / st.STATE_FILE).unlink()
    (root / "decls" / d["id"] / st.STATE_FILE).unlink()
    api("DELETE", f"/api/tf-declarations/{d['id']}")


# ---------------------------------------------------------------------------
# Le runner (dans le processus, Terraform simulé)
# ---------------------------------------------------------------------------
class FakeTF:
    """Rejoue plan / show / apply / destroy et note les commandes."""

    def __init__(self, show=None, rc=0):
        self.calls, self.show, self.rc = [], show or {"resource_changes": []}, rc

    def __call__(self, ws, kc, args, timeout=180):
        self.calls.append(args[0])
        if args[0] == "show":
            return 0, json.dumps(self.show), ""
        if args[0] == "plan" and "-out=tfplan" in args:
            (Path(ws) / "tfplan").write_text("plan")
        return self.rc, f"{args[0]} ok", ""


@pytest.fixture
def world(tmp_path, monkeypatch):
    store = wapp._tfs.DeclStore(tmp_path / "tf.db")
    monkeypatch.setattr(wapp, "TF_DECLS", store)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path / "ws")
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "1.9.0")
    kc = tmp_path / "kc"
    kc.write_text("apiVersion: v1\n")
    return store, str(kc), tmp_path / "ws" / "harv1"


def run_decl(decl, kc, mode, fake, monkeypatch, plan_hash=None):
    monkeypatch.setattr(wapp, "_tf_run_cmd", fake)
    rendered, errors = wapp._tf_render_resources(decl["resources"])
    assert not errors
    run = wapp.ActionRun("t" + mode, "x", "harv1", [])
    h = wapp._tf_decl_hash(rendered)
    wapp._tf_decl_runner(run, "harv1", kc, decl, rendered, mode, h, bool(plan_hash), "ju")
    return run, h


def test_a_plan_writes_only_its_own_workspace_and_summarises(world, monkeypatch):
    store, kc, shared = world
    d = store.create("a" * 12, "harv1", "web", resources=[vm("1" * 12, "web-01"), sshkey("2" * 12, "ops")])
    fake = FakeTF(show={"resource_changes": [
        {"mode": "managed", "address": "harvester_virtualmachine.web_01", "type": "harvester_virtualmachine",
         "name": "web_01", "change": {"actions": ["create"], "before": None, "after": {"name": "web-01"}}}]})
    run, h = run_decl(d, kc, "plan", fake, monkeypatch)
    assert run.status == "done", run.error_summary
    ws = shared / "decls" / d["id"]
    assert sorted(f.name for f in ws.glob("*.tf")) == ["_providers.tf", "ops.tf", "web_01.tf"]
    assert not list(shared.glob("*.tf"))                       # l'espace partagé n'est pas touché
    assert "apply" not in fake.calls
    assert run.result["plan"]["counts"]["create"] == 1 and run.result["plan_hash"] == h
    meta = json.loads((ws / "tfplan.meta").read_text())
    assert meta["hash"] == h and meta["revision"] == 1


def test_resources_deployed_before_are_taken_over_from_the_shared_state(world, monkeypatch):
    store, kc, shared = world
    shared.mkdir(parents=True)
    (shared / st.STATE_FILE).write_text(json.dumps({"version": 4, "serial": 3, "lineage": "l", "resources": [
        {"mode": "managed", "type": "harvester_ssh_key", "name": "ops", "instances": []},
        {"mode": "managed", "type": "harvester_ssh_key", "name": "someone_else", "instances": []}]}))
    (shared / "ops.tf").write_text("x")
    (shared / "ops.json").write_text("{}")
    d = store.create("b" * 12, "harv1", "keys", resources=[sshkey("3" * 12, "ops")])
    run, _ = run_decl(d, kc, "plan", FakeTF(), monkeypatch)
    assert run.status == "done"
    assert st.addresses(shared / "decls" / d["id"]) == ["harvester_ssh_key.ops"]
    assert st.addresses(shared) == ["harvester_ssh_key.someone_else"]
    assert not (shared / "ops.tf").exists() and not (shared / "ops.json").exists()


def test_a_resource_removed_from_the_declaration_leaves_its_workspace(world, monkeypatch):
    store, kc, shared = world
    d = store.create("c" * 12, "harv1", "two", resources=[sshkey("4" * 12, "a"), sshkey("5" * 12, "b")])
    run_decl(d, kc, "plan", FakeTF(), monkeypatch)
    d = store.update(d["id"], 1, resources=[sshkey("4" * 12, "a")])
    run_decl(d, kc, "plan", FakeTF(), monkeypatch)
    ws = shared / "decls" / d["id"]
    assert not (ws / "b.tf").exists() and not (ws / "b.json").exists()   # l'apply le détruira


def test_applying_the_reviewed_plan_does_not_plan_again(world, monkeypatch):
    store, kc, shared = world
    d = store.create("d" * 12, "harv1", "reviewed", resources=[sshkey("6" * 12, "k")])
    _, h = run_decl(d, kc, "plan", FakeTF(), monkeypatch)
    fake = FakeTF()
    run, _ = run_decl(d, kc, "apply", fake, monkeypatch, plan_hash=h)
    assert run.status == "done" and "plan" not in fake.calls and fake.calls[-1] == "apply"
    assert store.get(d["id"])["last_applied_status"] == "done"
    assert not (shared / "decls" / d["id"] / "tfplan").exists()


def test_a_plan_of_another_content_is_not_applied(world, monkeypatch):
    store, kc, shared = world
    d = store.create("e" * 12, "harv1", "moved", resources=[sshkey("7" * 12, "k")])
    run_decl(d, kc, "plan", FakeTF(), monkeypatch)
    d = store.update(d["id"], 1, resources=[sshkey("7" * 12, "k2")])
    fake = FakeTF()
    run, _ = run_decl(d, kc, "apply", fake, monkeypatch, plan_hash="0" * 16)
    assert run.status == "error" and "outdated" in run.error_summary
    assert "apply" not in fake.calls


def test_destroy_takes_the_whole_declaration_state_and_marks_it(world, monkeypatch):
    store, kc, shared = world
    d = store.create("f" * 12, "harv1", "gone", resources=[sshkey("8" * 12, "k")])
    run_decl(d, kc, "plan", FakeTF(), monkeypatch)
    fake = FakeTF()
    run, _ = run_decl(d, kc, "destroy", fake, monkeypatch)
    assert run.status == "done" and "destroy" in fake.calls
    assert "-target" not in json.dumps(fake.calls)
    assert store.get(d["id"])["last_applied_status"] == "destroyed"
    assert not (shared / "decls" / d["id"] / "k.tf").exists()


def test_raw_resources_are_named_after_their_block():
    rendered, _ = wapp._tf_render_resources([{"kind": "raw", "spec": {
        "tf": 'resource "harvester_ssh_key" "team_key" {\n  name = "t"\n}\n'}}])
    assert rendered[0][0] == "team_key" and rendered[0][4] == "harvester_ssh_key.team_key"


def test_a_provider_change_reinitialises_declaration_workspaces_too(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path)
    d = tmp_path / "harv1" / "decls" / ("a" * 12)
    (d / ".terraform").mkdir(parents=True)
    (tmp_path / "harv1" / ".terraform").mkdir()
    touched = wapp._tf_workspaces_invalidate()
    assert set(touched) == {"harv1", "harv1/decls/" + "a" * 12}
    assert not (d / ".terraform").exists()


def test_the_routes_take_a_declaration_by_its_id_alone(api):
    """Vu à l'essai réel : la route refusait `{declaration: {id}}` (« resources
    must be a non-empty list ») avant même de regarder l'identifiant."""
    status, d = api("POST", "/api/tf-declarations", expect_status=None,
                    json_body={"cluster": "harv-fake", "name": "by-id", "resources": [sshkey("9" * 12, "k")]})
    for path in ("apply_declaration", "destroy_declaration"):
        status, out = api("POST", f"/api/terraform/harv-fake/{path}", expect_status=None,
                          json_body={"declaration": {"id": d["id"]}, "dry_run": True})
        assert status == 201 and out["action_id"], (path, out)
    status, out = api("POST", "/api/terraform/harv-fake/apply_declaration", expect_status=None,
                      json_body={"declaration": {"id": d["id"]}, "dry_run": False, "plan_hash": "0" * 16})
    assert status == 409 and "outdated" in out["error"]
    status, out = api("POST", "/api/terraform/harv3/apply_declaration", expect_status=None,
                      json_body={"declaration": {"id": d["id"]}, "dry_run": True})
    assert status in (400, 404)                                 # pas le cluster de la déclaration
    status, out = api("POST", "/api/terraform/harv-fake/apply_declaration", expect_status=None,
                      json_body={"declaration": {"id": "f" * 12}, "dry_run": True})
    assert status == 404


def test_code_history_and_fingerprint(api):
    """v1.55.0 : l'onglet Code, l'historique, l'empreinte du contenu."""
    import time
    status, d = api("POST", "/api/tf-declarations", expect_status=None,
                    json_body={"cluster": "harv-fake", "name": "code-hist", "resources": [sshkey("8" * 12, "ops")]})
    assert d["content_hash"] and d["incomplete"] == []
    status, code = api("GET", f"/api/tf-declarations/{d['id']}/code")
    names = [f["name"] for f in code["files"]]
    assert names == ["_providers.tf", "ops.tf"]
    ops = code["files"][1]
    assert 'resource "harvester_ssh_key" "ops"' in ops["content"] and ops["address"] == "harvester_ssh_key.ops"
    assert "required_providers" not in ops["content"]                  # l'en-tête n'est qu'une fois
    status, out = api("POST", "/api/terraform/harv-fake/apply_declaration", expect_status=None,
                      json_body={"declaration": {"id": d["id"]}, "dry_run": True})
    assert status == 201
    for _ in range(40):
        status, h = api("GET", f"/api/tf-declarations/{d['id']}/history")
        if h["runs"] and h["runs"][0]["status"] not in ("running", "starting"):
            break
        time.sleep(0.5)
    run = h["runs"][0]
    assert run["id"] == out["action_id"] and run["mode"] == "plan"
    # une ressource incomplète : pas d'empreinte, le rang de la ressource dit
    status, r = api("PUT", f"/api/tf-declarations/{d['id']}", expect_status=None,
                    json_body={"revision": d["revision"], "resources": [{"id": "9" * 12, "kind": "vm", "spec": {}}]})
    assert r["content_hash"] is None and r["incomplete"] == [0]
    assert api("GET", "/api/tf-declarations/zzz/history", expect_status=None)[0] == 400
    api("DELETE", f"/api/tf-declarations/{d['id']}")
