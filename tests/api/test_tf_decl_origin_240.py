"""2.4.0 : origine d'une déclaration et lancement hors requête.

* `origin` exposée par la vue, jamais modifiable par `PUT` (champ ignoré,
  révision inchangée si seul `origin` est envoyé) ;
* `_tf_decl_launch` appelé depuis un fil sans contexte de requête
  (Terraform simulé), avec l'identité passée par l'appelant ;
* chaque refus de `_tf_decl_start` rendu à l'identique (404, 400 cible,
  400 cluster, 400 incomplet, 409 plan périmé, 503 provider KVM) ;
* `_tf_plan_meta` lit le dernier plan de l'espace.

Mutation jouée (rapport de la tâche 3) : `_tf_decl_launch` qui relit
`current_user()` (le test sans contexte casse).
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJdD pkg"
ORIGIN = {"plan": "site-standard", "template": "cles", "template_revision": 1, "plan_revision": 2}


def sshkey(rid, name):
    return {"id": rid, "kind": "ssh_key", "spec": {"name": name, "namespace": "default", "public_key": KEY}}


class FakeTF:
    def __init__(self):
        self.calls = []

    def __call__(self, ws, kc, args, timeout=180, **_kw):
        self.calls.append(args[0])
        if args[0] == "show":
            return 0, json.dumps({"resource_changes": [
                {"mode": "managed", "address": "harvester_ssh_key.ops", "type": "harvester_ssh_key",
                 "name": "ops", "change": {"actions": ["create"], "before": None, "after": {}}}]}), ""
        if args[0] == "plan" and "-out=tfplan" in args:
            (Path(ws) / "tfplan").write_text("plan")
        return 0, f"{args[0]} ok", ""


@pytest.fixture
def world(tmp_path, monkeypatch):
    store = wapp._tfs.DeclStore(tmp_path / "tf.db")
    monkeypatch.setattr(wapp, "TF_DECLS", store)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path / "ws")
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "1.9.0")
    fake = FakeTF()
    monkeypatch.setattr(wapp, "_tf_run_cmd", fake)
    kc = tmp_path / "kc"
    kc.write_text("apiVersion: v1\n")
    with wapp.ACTIONS_LOCK:
        before = dict(wapp.ACTIONS)
    yield store, str(kc), fake
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS.clear()
        wapp.ACTIONS.update(before)


def _wait(run_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        run = wapp.ACTIONS.get(run_id)
        if run is not None and run.status in ("done", "error"):
            return run
        time.sleep(0.02)
    raise AssertionError("action did not end")


def test_origin_is_shown_and_never_changed_by_put(world, monkeypatch):
    store, _kc, _ = world
    d = store.create("a" * 12, "harv1", "site-standard-cles", resources=[sshkey("1" * 12, "ops")], origin=ORIGIN)
    assert wapp._tf_decl_view(d)["origin"] == ORIGIN
    wapp.app.config["TESTING"] = True
    client = wapp.app.test_client()
    r = client.put(f"/api/tf-declarations/{d['id']}",
                   json={"revision": 1, "origin": {"plan": "other", "template": "x",
                                                   "template_revision": 9, "plan_revision": 9}})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["origin"] == ORIGIN and r.get_json()["revision"] == 1
    r = client.put(f"/api/tf-declarations/{d['id']}", json={"revision": 1, "description": "hand", "origin": None})
    assert r.get_json()["origin"] == ORIGIN and r.get_json()["revision"] == 2
    assert store.get(d["id"])["origin"] == ORIGIN


def test_launch_from_a_thread_without_request_context(world):
    store, kc, fake = world
    d = store.create("b" * 12, "harv1", "site-standard-cles", resources=[sshkey("2" * 12, "ops")], origin=ORIGIN)
    out = {}

    def worker():
        try:
            out["ret"] = wapp._tf_decl_launch("harv1", kc, d["id"], "plan", user="ops-240",
                                              cluster_user="ops-240@harv1")
        except Exception as e:          # noqa: BLE001
            out["err"] = e
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert "err" not in out, repr(out.get("err"))
    run_id, h = out["ret"]
    run = _wait(run_id)
    assert run.status == "done", run.error_summary
    assert run.result["by"] == "ops-240" and run.result["declaration_id"] == d["id"]
    assert run.cluster_user == "ops-240@harv1"
    assert run.action == "tf-plan-decl:site-standard-cles:1" and run.dry_run
    meta = wapp._tf_plan_meta(store.get(d["id"]))
    assert meta["hash"] == h and meta["revision"] == 1
    assert meta["summary"]["counts"]["create"] == 1
    # puis l'apply du plan revu, toujours hors requête
    run_id2, h2 = wapp._tf_decl_launch("harv1", kc, d["id"], "apply", plan_hash=h, user="ops-240")
    run2 = _wait(run_id2)
    assert run2.status == "done" and h2 == h
    assert fake.calls.count("plan") == 1 and "apply" in fake.calls
    assert store.get(d["id"])["last_applied_status"] == "done"
    assert wapp._tf_plan_meta(store.get(d["id"])) is None          # retiré par l'apply


def test_plan_meta_absent_or_unreadable(world):
    store, _kc, _ = world
    d = store.create("c" * 12, "harv1", "x", resources=[sshkey("3" * 12, "ops")])
    assert wapp._tf_plan_meta(d) is None
    ws = wapp._tf_decl_ws(d)
    (ws / "tfplan.meta").write_text("{not json")
    assert wapp._tf_plan_meta(d) is None


def _cases(store, monkeypatch):
    ok = store.create("d" * 12, "harv1", "ok", resources=[sshkey("4" * 12, "ops")])
    bad = store.create("e" * 12, "harv1", "bad", resources=[{"id": "5" * 12, "kind": "vm", "spec": {}}])
    kvm = store.create("f" * 12, "host1", "kvm", target_kind="kvm")
    return [
        ("0" * 12, "harv1", "plan", None, "harvester", 404,
         {"error": "declaration not found", "id": "0" * 12}),
        (kvm["id"], "host1", "plan", None, "harvester", 400,
         {"error": "declaration targets a kvm host, not a harvester one"}),
        (ok["id"], "harv2", "plan", None, "harvester", 400,
         {"error": "declaration belongs to another cluster", "cluster": "harv1"}),
        (bad["id"], "harv1", "plan", None, "harvester", 400,
         {"error": "one or more resources are incomplete",
          "errors": [{"index": 0, "kind": "vm", "name": None, "error": "missing required fields"}],
          "supported": ["vm", "image", "ssh_key", "raw"]}),
        (ok["id"], "harv1", "apply", "stale", "harvester", 409,
         {"error": "plan outdated", "hint": "the declaration changed since this plan; plan again"}),
        (kvm["id"], "host1", "destroy", None, "kvm", 503,
         {"error": "the libvirt Terraform provider is not installed",
          "hint": "it ships with the harvester-ops package; see the Installation tab"}),
    ]


@pytest.mark.parametrize("index", range(6))
def test_every_refusal_is_rendered_identically(world, monkeypatch, index):
    store, kc, _ = world
    monkeypatch.setattr(wapp, "_tf_libvirt_binary", lambda: None)
    decl_id, cluster, mode, plan_hash, kind, status, body = _cases(store, monkeypatch)[index]
    with wapp.ACTIONS_LOCK:
        before = set(wapp.ACTIONS)
    with pytest.raises(wapp.TfLaunchError) as e:
        wapp._tf_decl_launch(cluster, kc, decl_id, mode, plan_hash=plan_hash, target_kind=kind)
    assert (e.value.status, e.value.payload) == (status, body)
    with wapp.app.test_request_context():
        resp, code = wapp._tf_decl_start(cluster, kc, decl_id, mode, plan_hash=plan_hash, target_kind=kind)
        assert (code, resp.get_json()) == (status, body)
    with wapp.ACTIONS_LOCK:
        assert set(wapp.ACTIONS) == before                      # rien de lancé


def test_route_wrapper_answers_201_with_the_session_identity(world, monkeypatch):
    store, kc, _ = world
    d = store.create("9" * 12, "harv1", "w", resources=[sshkey("6" * 12, "ops")])
    monkeypatch.setattr(wapp, "current_user", lambda: "alice")
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: {"user": "alice@harv1"})
    with wapp.app.test_request_context():
        resp, code = wapp._tf_decl_start("harv1", kc, d["id"], "plan")
        body = resp.get_json()
    assert code == 201 and set(body) == {"action_id", "plan_hash"}
    run = _wait(body["action_id"])
    assert run.result["by"] == "alice" and run.cluster_user == "alice@harv1"
