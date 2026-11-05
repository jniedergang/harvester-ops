"""2.4.0 : origine d'une déclaration et lancement hors requête.

* `origin` exposée par la vue, jamais modifiable par `PUT` (champ ignoré,
  révision inchangée si seul `origin` est envoyé) ;
* `_tf_decl_launch` appelé depuis un fil sans contexte de requête
  (Terraform simulé), avec l'identité passée par l'appelant ;
* chaque refus de `_tf_decl_start` rendu à l'identique (404, 400 cible,
  400 cluster, 400 incomplet, 409 plan périmé, 503 provider KVM) ;
* le résumé du plan est lu dans le résultat de l'action fille
  (`run.result["plan"]`), pas dans l'espace partagé (revue I4) ;
* une seule exécution à la fois par déclaration, toute cible, et
  réservation par un déroulé de plan (revue I4).

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
    assert run.result["plan_hash"] == h
    assert run.result["plan"]["counts"]["create"] == 1
    # puis l'apply du plan revu, toujours hors requête
    run_id2, h2 = wapp._tf_decl_launch("harv1", kc, d["id"], "apply", plan_hash=h, user="ops-240")
    run2 = _wait(run_id2)
    assert run2.status == "done" and h2 == h
    assert fake.calls.count("plan") == 1 and "apply" in fake.calls
    assert store.get(d["id"])["last_applied_status"] == "done"
    assert run2.result["plan"]["counts"]["create"] == 1


def test_one_run_at_a_time_for_a_harvester_declaration(world, monkeypatch):
    """Revue I4 : la règle d'une exécution à la fois vaut pour toute cible."""
    store, kc, fake = world
    d = store.create("c" * 12, "harv1", "x", resources=[sshkey("3" * 12, "ops")])
    gate = threading.Event()

    def slow(ws, kcp, args, timeout=180, **kw):
        gate.wait(5)
        return fake(ws, kcp, args, timeout, **kw)
    monkeypatch.setattr(wapp, "_tf_run_cmd", slow)
    run_id, _h = wapp._tf_decl_launch("harv1", kc, d["id"], "plan")
    with pytest.raises(wapp.TfLaunchError) as e:
        wapp._tf_decl_launch("harv1", kc, d["id"], "plan")
    assert e.value.status == 409 and e.value.payload["code"] == "running"
    wapp.app.config["TESTING"] = True
    r = wapp.app.test_client().delete(f"/api/tf-declarations/{d['id']}")
    assert r.status_code == 409 and r.get_json()["code"] == "running"
    gate.set()
    assert _wait(run_id).status == "done"
    assert d["id"] not in wapp.TF_DECL_RUNNING
    run_id, _h = wapp._tf_decl_launch("harv1", kc, d["id"], "plan")        # libérée
    assert _wait(run_id).status == "done"


def test_held_declaration_launches_only_for_its_holder(world):
    """Revue I4 : réservée par un déroulé de plan entre son plan et son
    apply, la déclaration ne se lance que pour lui ; libérée ensuite."""
    store, kc, _ = world
    d = store.create("8" * 12, "harv1", "x", resources=[sshkey("7" * 12, "ops")])
    assert wapp._tf_decl_hold(d["id"], "aaaaaaaaaaaa")
    assert not wapp._tf_decl_hold(d["id"], "bbbbbbbbbbbb")
    with pytest.raises(wapp.TfLaunchError) as e:
        wapp._tf_decl_launch("harv1", kc, d["id"], "plan")
    assert e.value.status == 409
    run_id, _h = wapp._tf_decl_launch("harv1", kc, d["id"], "plan", holder="aaaaaaaaaaaa")
    assert _wait(run_id).status == "done"
    assert d["id"] in wapp.TF_DECL_HELD                    # le déroulé la garde
    wapp._tf_decl_release(d["id"], "bbbbbbbbbbbb")          # pas la sienne : sans effet
    assert d["id"] in wapp.TF_DECL_HELD
    wapp._tf_decl_release(d["id"], "aaaaaaaaaaaa")
    assert d["id"] not in wapp.TF_DECL_HELD
    run_id, _h = wapp._tf_decl_launch("harv1", kc, d["id"], "plan")
    assert _wait(run_id).status == "done"


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


# Revue B2-I2 : la ligne de commande charge ce même module dans un AUTRE
# processus ; la réservation et l'exécution d'une déclaration passent donc
# aussi par un verrou de fichier dans son espace (`.run.lock`). Un processus
# fils qui le tient bloque réservation, lancement et suppression ; libéré, tout
# repart. Mutation jouée : verrou de fichier non pris (les trois tombent).
_HOLDER = r"""
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.write(fd, b"other process\n")
print("held", flush=True)
sys.stdin.readline()
"""


@pytest.fixture
def other_process():
    import subprocess
    procs = []

    def hold(path):
        p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "held"
        procs.append(p)
        return p
    yield hold
    for p in procs:
        if p.poll() is None:
            p.communicate("\n", timeout=5)


def test_declaration_locked_by_another_process(world, other_process):
    store, kc, _ = world
    d = store.create("9" * 12, "harv1", "x", resources=[sshkey("7" * 12, "ops")])
    p = other_process(wapp._tf_decl_ws(d) / ".run.lock")
    assert not wapp._tf_decl_hold(d["id"], "aaaaaaaaaaaa")
    with pytest.raises(wapp.TfLaunchError) as e:
        wapp._tf_decl_launch("harv1", kc, d["id"], "plan")
    assert e.value.status == 409 and e.value.payload["code"] == "running"
    with wapp.app.test_client() as c:
        r = c.delete(f"/api/tf-declarations/{d['id']}")
    assert r.status_code == 409 and store.get(d["id"])
    p.communicate("\n", timeout=5)
    assert wapp._tf_decl_hold(d["id"], "aaaaaaaaaaaa")
    wapp._tf_decl_release(d["id"], "aaaaaaaaaaaa")
    run_id, _h = wapp._tf_decl_launch("harv1", kc, d["id"], "plan")
    assert _wait(run_id).status == "done"


def test_hold_and_run_keep_the_file_lock_until_released(world):
    """Pendant la réservation (plan puis apply d'un déroulé), un autre
    processus ne prend pas le verrou ; il le prend après la libération."""
    import subprocess
    store, kc, _ = world
    d = store.create("6" * 12, "harv1", "x", resources=[sshkey("7" * 12, "ops")])
    lock = wapp._tf_decl_ws(d) / ".run.lock"
    probe = ("import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
             "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); print('free')\n"
             "except OSError:\n    print('held')\n")

    def state():
        return subprocess.run([sys.executable, "-c", probe, str(lock)], capture_output=True,
                              text=True).stdout.strip()
    assert wapp._tf_decl_hold(d["id"], "aaaaaaaaaaaa")
    assert state() == "held"
    run_id, _h = wapp._tf_decl_launch("harv1", kc, d["id"], "plan", holder="aaaaaaaaaaaa")
    assert _wait(run_id).status == "done"
    assert state() == "held"                          # plan fini, réservation tenue
    wapp._tf_decl_release(d["id"], "aaaaaaaaaaaa")
    assert state() == "free"
    run_id, _h = wapp._tf_decl_launch("harv1", kc, d["id"], "plan")
    assert _wait(run_id).status == "done"
    assert state() == "free"                          # exécution finie : libéré
