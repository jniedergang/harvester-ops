"""2.4.0 : déroulé d'un plan dans la console, action deploy:<cluster> (tâche 6).

Terraform simulé (comme test_tf_kvm_decl_230.py), lectures de disponibilité
simulées (fixture harv1, tâche 4), installation simulée (actions filles
factices) :

* chaîne disponibilité puis Terraform : déclaration créée avec son origine,
  plan puis apply, résultat (plan, révision, éléments) ;
* plan qui détruit : refusé (`plan-destroys`), rien appliqué ;
* plan vide : fait sans apply ;
* essai : plan seulement, aucune déclaration restante ;
* modèle en échec, indépendant poursuivi : phase `partial`, puis reprise :
  le modèle fait est revérifié et sauté, seul le manquant est appliqué ;
* arrêt transmis à l'installation ; installation enchaînée puis Terraform ;
* secrets effacés du magasin après succès, échec, arrêt et exception ;
  aucun secret dans les étapes, le résultat ni actions.db.

Mutations jouées (rapport des tâches 6-7) : `plan-destroys` non contrôlé,
essai qui applique, effacement des secrets hors du `finally`, garde 132
après la création de l'action.
"""

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import secret_store as ss  # noqa: E402
import test_deploy_routes_240 as tr  # noqa: E402

READS = json.loads((ROOT / "tests" / "fixtures" / "deploy" / "harv1-reads.json").read_text())
SECRET = "TOKEN-VALUE-240-runner"


class FakeTF:
    """Terraform simulé : `actions[nom de ressource]` dit ce que le plan
    montre ; `fail` : noms dont le plan échoue."""

    def __init__(self):
        self.calls, self.actions, self.fail = [], {}, set()

    def _names(self, ws):
        text = " ".join(p.read_text() for p in Path(ws).glob("*.tf") if p.name != "_providers.tf")
        return text

    def __call__(self, ws, kc, args, timeout=180, **_kw):
        text = self._names(ws)
        self.calls.append((args[0], text.count("harvester_ssh_key")))
        if args[0] == "plan" and any(f'"{n}"' in text for n in self.fail):
            return 1, "", "Error: cluster network not found"
        if args[0] == "show":
            act = "create"
            for n, a in self.actions.items():
                if f'"{n}"' in text:
                    act = a
            changes = [] if act == "noop" else [
                {"mode": "managed", "address": "harvester_ssh_key.k", "type": "harvester_ssh_key", "name": "k",
                 "change": {"actions": {"create": ["create"], "update": ["update"], "delete": ["delete"],
                                        "replace": ["delete", "create"]}[act], "before": None, "after": {}}}]
            return 0, json.dumps({"resource_changes": changes}), ""
        if args[0] == "plan" and "-out=tfplan" in args:
            (Path(ws) / "tfplan").write_text("plan")
        return 0, f"{args[0]} ok", ""

    def count(self, verb):
        return sum(1 for v, _n in self.calls if v == verb)


REAL_START = wapp._deploy_start
REAL_TRACK = wapp.track_action
env = tr.env


@pytest.fixture()
def world(env, tmp_path, monkeypatch):
    """La fixture des routes, avec le vrai lancement et Terraform simulé."""
    monkeypatch.setattr(wapp, "_deploy_start", REAL_START)
    monkeypatch.setattr(wapp, "track_action", REAL_TRACK)
    fake = FakeTF()
    monkeypatch.setattr(wapp, "_tf_run_cmd", fake)
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "1.9.0")
    monkeypatch.setattr(wapp, "_deploy_read_cluster", lambda kc, cluster: json.loads(json.dumps(READS)))
    monkeypatch.setattr(wapp, "DEPLOY_CHILD_POLL", 0.01)
    monkeypatch.setattr(wapp, "DEPLOY_POLL", 0.01)
    monkeypatch.setattr(wapp, "BM_BATCH_POLL", 0.01)
    monkeypatch.setattr(wapp, "ACTIONS_DB", tmp_path / "actions.db")
    wapp._actions_init_db()
    env["fake"] = fake
    env["db"] = tmp_path / "actions.db"
    return env


def wait(run_id, timeout=15):
    """Action finie ET rangée : le fil écrit le statut final en mémoire, puis
    ferme l'action (persistance dans actions.db), puis rend le verrou du
    cluster. Revenir au seul statut laissait la suite (reprise relue dans
    actions.db, défaire) courir contre la fermeture : tests instables sous
    `-n 12` (revue finale 2.10.0 m-9)."""
    end = time.time() + timeout
    while time.time() < end:
        run = wapp.ACTIONS.get(run_id)
        if run is not None and run.status in ("done", "error", "cancelled"):
            while time.time() < end:
                row, _ev = wapp._actions_db_get(run_id)
                if getattr(run, "_closed", False) and (row or {}).get("status") == run.status:
                    return run
                time.sleep(0.02)
            break
        time.sleep(0.02)
    raise AssertionError(f"action {run_id} did not end")


def start(w, plan="site", variables=None, dry_run=False):
    r = w["client"].post("/api/deploy/harvlab", json={"plan": plan, "variables": variables or {"equipe": "ops"},
                                                       "dry_run": dry_run})
    assert r.status_code == 202, r.get_json()
    return wait(r.get_json()["action_id"])


def steps(run):
    return [(e["step_id"], e["status"]) for e in run.events if e.get("type") == "step"]


def two_templates(w, fail_second=True):
    c = w["client"]
    tr.seed(c, plan=tr.plan_body(templates=("cles", "reseau")),
            templates={"cles": tr.template_body(), "reseau": tr.template_body(name="net-{{equipe}}")})
    if fail_second:
        w["fake"].fail.add("net-ops")


def test_chain_readiness_then_terraform(world):
    tr.seed(world["client"])
    run = start(world)
    assert run.status == "done" and run.exit_code == 0, run.result
    res = run.result
    assert res["plan"] == "site" and res["plan_revision"] == 1 and res["cluster"] == "harvlab"
    assert res["resumes"] is None and res["dry_run"] is False and res["status"] == "done"
    assert res["variables"] == {"equipe": "ops"}
    assert [p["id"] for p in res["phases"]] == ["readiness", "terraform", "capi"]
    assert res["phases"][2] == {"id": "capi", "status": "disabled", "message": "disabled in the plan"}
    [item] = res["phases"][1]["items"]
    d = world["store"].find_by_name("harvlab", "site-cles")
    assert item["template"] == "cles" and item["status"] == "done" and item["declaration_id"] == d["id"]
    assert item["revision_applied"] == 1 and len(item["actions"]) == 2
    # décision 161 : l'auteur du modèle ; 2.7.0 : l'exécution qui l'a créée
    assert d["origin"] == {"plan": "site", "template": "cles", "template_revision": 1, "plan_revision": 1,
                           "template_by": "alice", "run": run.id}
    assert item["created"] is True and item["undo"] == {"kind": "tf-declaration", "ref": d["id"]}
    assert d["last_applied_status"] == "done" and d["resources"][0]["spec"]["name"] == "ops-ops"
    assert world["fake"].count("plan") == 1 and world["fake"].count("apply") == 1
    assert ("phase-readiness", "done") in steps(run) and ("tf-cles", "done") in steps(run)
    assert ("phase-capi", "skipped") in steps(run)
    with wapp.ACTIONS_LOCK:
        children = [a for a in wapp.ACTIONS.values() if a.id in item["actions"]]
    assert sorted(a.action for a in children) == ["tf-apply-decl:site-cles:1", "tf-plan-decl:site-cles:1"]
    assert d["id"] not in wapp.TF_DECL_HELD and d["id"] not in wapp.TF_DECL_RUNNING
    # verrous de fichier rendus (revue B2-I2) : cluster et déclaration
    end = time.time() + 3
    while wapp._deploy_lock_holder("harvlab") is not None and time.time() < end:
        time.sleep(0.02)
    assert wapp._deploy_lock_holder("harvlab") is None
    assert d["id"] not in wapp.TF_DECL_FDS


@pytest.mark.parametrize("action", ["delete", "replace"])
def test_plan_that_destroys_is_refused(world, action):
    tr.seed(world["client"])
    world["fake"].actions["ops-ops"] = action
    run = start(world)
    assert run.status == "error" and run.exit_code == 1
    item = run.result["phases"][1]["items"][0]
    assert item["status"] == "error" and item["message"] == "plan-destroys"
    assert world["fake"].count("apply") == 0
    assert run.result["failed_phase"] == "terraform"


def test_empty_plan_is_done_without_apply(world):
    tr.seed(world["client"])
    world["fake"].actions["ops-ops"] = "noop"
    run = start(world)
    item = run.result["phases"][1]["items"][0]
    assert run.status == "done" and item["message"] == "nothing to change" and item["revision_applied"] == 1
    assert world["fake"].count("apply") == 0
    assert world["store"].find_by_name("harvlab", "site-cles")["last_applied_status"] == "done"


def test_dry_run_plans_only_and_leaves_no_declaration(world):
    tr.seed(world["client"])
    run = start(world, dry_run=True)
    assert run.status == "done" and run.dry_run and run.result["dry_run"] is True
    item = run.result["phases"][1]["items"][0]
    assert item["message"] == "planned (dry run)" and item["counts"]["create"] == 1
    assert world["fake"].count("apply") == 0
    assert world["store"].find_by_name("harvlab", "site-cles") is None
    assert ("cleanup", "done") in steps(run)


def test_dry_run_keeps_an_existing_declaration(world):
    tr.seed(world["client"])
    start(world)
    run = start(world, dry_run=True)
    assert run.status == "done"
    assert world["store"].find_by_name("harvlab", "site-cles") is not None


def test_partial_then_resume_applies_only_the_missing_template(world):
    two_templates(world)
    run = start(world)
    assert run.status == "error" and run.result["status"] == "partial"
    items = {i["template"]: i for i in run.result["phases"][1]["items"]}
    assert items["cles"]["status"] == "done" and items["reseau"]["status"] == "error"
    assert items["reseau"]["message"].startswith("plan failed (action ")
    applies = world["fake"].count("apply")
    # correction, puis reprise
    world["fake"].fail.clear()
    r = world["client"].post("/api/deploy/harvlab/resume", json={"action_id": run.id})
    assert r.status_code == 202, r.get_json()
    run2 = wait(r.get_json()["action_id"])
    assert run2.status == "done" and run2.result["resumes"] == run.id
    items2 = {i["template"]: i for i in run2.result["phases"][1]["items"]}
    assert items2["cles"].get("checked") is True and items2["reseau"]["status"] == "done"
    assert world["fake"].count("apply") == applies + 1            # seul le manquant
    assert ("tf-cles", "skipped") in steps(run2)


def test_resume_reads_the_persisted_result(world):
    """La reprise relit actions.db quand l'action n'est plus en mémoire
    (console redémarrée)."""
    two_templates(world)
    run = start(world)
    with wapp.ACTIONS_LOCK:
        del wapp.ACTIONS[run.id]
    world["fake"].fail.clear()
    r = world["client"].post("/api/deploy/harvlab/resume", json={"action_id": run.id})
    assert r.status_code == 202, r.get_json()
    assert wait(r.get_json()["action_id"]).status == "done"


def test_resume_applies_a_template_added_since(world):
    """Revue I3 en console : une phase Terraform faite, puis un modèle ajouté
    au plan : la reprise l'applique."""
    tr.seed(world["client"])
    run = start(world)
    c = world["client"]
    c.put("/api/baremetal/tf-templates/reseau", json=tr.template_body(name="net-{{equipe}}"))
    c.put("/api/baremetal/plans/site", json=dict(tr.plan_body(templates=("cles", "reseau")), revision=1))
    r = c.post("/api/deploy/harvlab/resume", json={"action_id": run.id})
    run2 = wait(r.get_json()["action_id"])
    items = {i["template"]: i for i in run2.result["phases"][1]["items"]}
    assert items["cles"]["checked"] and items["reseau"]["status"] == "done" and run2.result["plan_revision"] == 2


def test_stop_during_terraform_lets_the_running_template_finish(world, monkeypatch):
    two_templates(world, fail_second=False)
    gate = threading.Event()
    fake = world["fake"]

    def slow(ws, kc, args, timeout=180, **kw):
        if args[0] == "apply":
            gate.wait(5)
        return fake(ws, kc, args, timeout, **kw)
    monkeypatch.setattr(wapp, "_tf_run_cmd", slow)
    r = world["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops"}})
    rid = r.get_json()["action_id"]
    end = time.time() + 10
    while fake.count("show") < 1 and time.time() < end:
        time.sleep(0.02)
    assert world["client"].post("/api/deploy/harvlab/stop", json={"action_id": rid}).status_code == 202
    gate.set()
    run = wait(rid)
    assert run.status == "cancelled" and run.exit_code == 3
    items = {i["template"]: i for i in run.result["phases"][1]["items"]}
    assert items["cles"]["status"] == "done" and items["reseau"]["status"] == "skipped"
    assert "cancel_requested" not in run.result               # le statut le dit déjà


def test_stop_during_the_last_template_is_recorded(world, monkeypatch):
    """Revue m2 : un arrêt arrivé pendant le DERNIER modèle le laisse finir ;
    le statut reste vrai (fait), le résultat garde `cancel_requested`."""
    tr.seed(world["client"])
    gate = threading.Event()
    fake = world["fake"]

    def slow(ws, kc, args, timeout=180, **kw):
        if args[0] == "apply":
            gate.wait(5)
        return fake(ws, kc, args, timeout, **kw)
    monkeypatch.setattr(wapp, "_tf_run_cmd", slow)
    r = world["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops"}})
    rid = r.get_json()["action_id"]
    end = time.time() + 10
    while fake.count("show") < 1 and time.time() < end:
        time.sleep(0.02)
    assert world["client"].post("/api/deploy/harvlab/stop", json={"action_id": rid}).status_code == 202
    gate.set()
    run = wait(rid)
    assert run.status == "done" and run.result["cancel_requested"] is True


def test_secrets_never_in_steps_result_or_db(world):
    tr.seed(world["client"])
    run = start(world)
    blob = json.dumps(run.to_dict()) + json.dumps(list(run.events))
    con = sqlite3.connect(world["db"])
    rows = con.execute("SELECT result, events FROM actions").fetchall()
    con.close()
    assert rows and SECRET not in blob and all(SECRET not in (a or "") + (b or "") for a, b in rows)


# --- installation enchaînée ---------------------------------------------------

def _fake_child(status="done", gate=None, seen=None):
    def launch(rows, cluster_name, concurrency=None, parent=None):
        if seen is not None:
            seen.append([dict(r) for r in rows])
        run = wapp.ActionRun(wapp.uuid.uuid4().hex[:12], f"baremetal-batch:{cluster_name}", "(local)", [])
        run.status = "running"
        with wapp.ACTIONS_LOCK:
            wapp.ACTIONS[run.id] = run

        def finish():
            if gate is not None:
                while not gate.is_set() and not getattr(run, "_cancel", False):
                    time.sleep(0.01)
            run.status = "cancelled" if getattr(run, "_cancel", False) else status
            run.close()
        threading.Thread(target=finish, daemon=True).start()
        return run.id
    return launch


def _install(w, monkeypatch, status="done", gate=None, raise_in=None):
    seen = []
    monkeypatch.setattr(wapp, "_bm_batch_launch", _fake_child(status, gate, seen))
    monkeypatch.setattr(wapp, "_deploy_install_verify", lambda cluster, expected: (status == "done", expected))
    monkeypatch.setattr(wapp, "_deploy_console_kc", lambda cluster: w["kc"])
    if raise_in:
        monkeypatch.setattr(wapp, raise_in, lambda *a, **k: (_ for _ in ()).throw(RuntimeError(SECRET)))
    c = w["client"]
    tr.seed(c, plan=tr.plan_body(readiness=False))
    tr.t180.create(c)
    body = dict(tr.batch_body(), token=SECRET)
    r = c.post("/api/baremetal/profiles/rack-a/batch", json=body)
    assert r.status_code == 202, r.get_json()
    return r.get_json()["action_id"], seen


def _store_empty(rid):
    return ss.store().refs("plan-run", rid) == []


def test_install_then_terraform(world, monkeypatch):
    rid, seen = _install(world, monkeypatch)
    run = wait(rid)
    assert run.status == "done", run.result
    assert run.action == "deploy:rack-a" and run.required_role == "admin"
    assert [p["id"] for p in run.result["phases"]] == ["install", "readiness", "terraform", "capi"]
    assert run.result["phases"][1]["status"] == "disabled"
    assert run.result["install"]["hostnames"] == ["node1", "node2"]
    # la série a reçu ses secrets du magasin, au lancement
    assert [r["token"] for r in seen[0]] == [SECRET, SECRET]
    assert _store_empty(rid)
    blob = json.dumps(run.to_dict()) + json.dumps(list(run.events))
    assert SECRET not in blob and tr.t180.BMCPW not in blob


@pytest.mark.parametrize("status,final", [("error", "error")])
def test_install_failure_stops_the_chain_and_erases_secrets(world, monkeypatch, status, final):
    rid, _ = _install(world, monkeypatch, status=status)
    run = wait(rid)
    assert run.status == final and run.result["failed_phase"] == "install"
    assert world["fake"].count("plan") == 0
    assert _store_empty(rid)


def test_stop_is_forwarded_to_the_installation(world, monkeypatch):
    gate = threading.Event()
    rid, _ = _install(world, monkeypatch, gate=gate)
    time.sleep(0.2)
    assert world["client"].post("/api/deploy/rack-a/stop", json={"action_id": rid}).status_code == 202
    run = wait(rid)
    assert run.status == "cancelled" and run.exit_code == 3
    child = run.result["phases"][0]["actions"][0]
    assert wapp.ACTIONS[child].status == "cancelled"
    assert _store_empty(rid)


def test_exception_erases_secrets(world, monkeypatch):
    rid, _ = _install(world, monkeypatch, raise_in="_deploy_wait_child")
    run = wait(rid)
    assert run.status == "error" and SECRET not in json.dumps(run.to_dict())
    assert _store_empty(rid)


def test_resume_install_relaunches_only_missing_rows(world, monkeypatch):
    rid, _ = _install(world, monkeypatch, status="error")
    run = wait(rid)
    seen = []
    monkeypatch.setattr(wapp, "_bm_batch_launch", _fake_child("done", None, seen))
    monkeypatch.setattr(wapp, "_deploy_install_verify", lambda cluster, expected: (False, ["node1"]))
    monkeypatch.setattr(wapp, "_deploy_cluster_entry", lambda c: {"name": c, "kubeconfig": world["kc"]})
    monkeypatch.setattr(wapp, "_join_cluster", lambda url, c=None: "rack-a")
    r = world["client"].post("/api/deploy/rack-a/resume",
                             json={"action_id": run.id, "secrets": {"token": SECRET, "bmc_password": "B"}})
    assert r.status_code == 202, r.get_json()
    run2 = wait(r.get_json()["action_id"])
    assert [row["hostname"] for row in seen[0]] == ["node2"]
    assert seen[0][0]["mode"] == "join"
    assert _store_empty(run2.id)


# Revue B2-I3 : une installation dont tous les nœuds ont rejoint le cluster
# est faite, quel que soit l'état de sa phase dans le résultat repris (console
# redémarrée pendant l'installation : phase absente ; arrêt arrivé trop tard
# ou étape après l'installation en échec : phase `error`). La reprise ne
# demande alors aucun secret et termine la chaîne.
@pytest.mark.parametrize("phase", [None, "error", "cancelled"])
def test_resume_install_whose_nodes_all_joined(world, monkeypatch, phase):
    rid, _ = _install(world, monkeypatch, status="error")
    run = wait(rid)
    res = dict(run.result)
    res["phases"] = [p for p in res["phases"] if p["id"] != "install"]
    if phase:
        res["phases"].insert(0, {"id": "install", "status": phase, "actions": []})
    run.result = res
    launched = []
    monkeypatch.setattr(wapp, "_bm_batch_launch", lambda *a, **k: launched.append(a) or "x")
    monkeypatch.setattr(wapp, "_deploy_install_verify", lambda cluster, expected: (True, list(expected)))
    monkeypatch.setattr(wapp, "_deploy_cluster_entry", lambda c: {"name": c, "kubeconfig": world["kc"]})
    r = world["client"].post("/api/deploy/rack-a/resume", json={"action_id": run.id})
    assert r.status_code == 202, r.get_json()
    run2 = wait(r.get_json()["action_id"])
    assert run2.status == "done", run2.result
    install = run2.result["phases"][0]
    assert install["id"] == "install" and install["status"] in ("done", "skipped") and launched == []


# Revue B2-I6 : relancer le plan avec d'autres valeurs que celles de la
# déclaration reprise ne dit jamais « rien à changer ». Sans demande, la
# déclaration est gardée (elle a pu être retouchée dans Terraform, décision
# 149) et le résultat le DIT ; avec `update_differing`, elle est mise à jour
# puis planifiée et appliquée. Mutations jouées : comparaison retirée (le cas
# gardé redevient « nothing to change »), mise à jour ignorée.
def test_rerun_with_other_values_is_never_silent(world):
    c = world["client"]
    tr.seed(c)
    assert start(world, variables={"equipe": "ops"}).status == "done"
    d = world["store"].find_by_name("harvlab", "site-cles")
    world["fake"].actions["ops-ops"] = "noop"
    world["fake"].actions["dev-ops"] = "noop"
    r = c.post("/api/deploy/harvlab/check", json={"plan": "site", "variables": {"equipe": "dev"}})
    codes = {f["code"]: f for f in r.get_json()["findings"]}
    assert codes["decl-differs"]["level"] == "warning" and "only if you ask" in codes["decl-differs"]["detail"]
    run = start(world, variables={"equipe": "dev"})
    item = run.result["phases"][1]["items"][0]
    assert item["message"] == "kept: values differ from the declaration" and item["differs"] is True
    assert world["store"].get(d["id"])["resources"][0]["spec"]["name"] == "ops-ops"
    # même valeurs : rien ne diffère, rien n'est dit
    run = start(world, variables={"equipe": "ops"})
    item = run.result["phases"][1]["items"][0]
    assert item["message"] == "nothing to change" and "differs" not in item
    # mise à jour demandée
    world["fake"].actions.pop("dev-ops")
    r = c.post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "dev"},
                                            "update_differing": True})
    assert r.status_code == 202
    run = wait(r.get_json()["action_id"])
    item = run.result["phases"][1]["items"][0]
    assert run.status == "done" and item["updated"] is True and item["message"] == "applied"
    d2 = world["store"].get(d["id"])
    assert d2["resources"][0]["spec"]["name"] == "dev-ops" and d2["revision"] == 2
    assert item["revision_applied"] == 2 and run.result["update_differing"] is True


def test_resume_after_a_plan_change_says_so(world):
    """Revue m1 : le plan a changé depuis le déroulé repris : constat
    `plan-changed` (avertissement) dans la réponse de reprise."""
    two_templates(world)
    run = start(world)
    assert run.result["status"] == "partial"
    world["fake"].fail.clear()
    c = world["client"]
    body = tr.plan_body(templates=("cles", "reseau"), description="changé")
    assert c.put("/api/baremetal/plans/site", json=dict(body, revision=1)).status_code == 200
    r = c.post("/api/deploy/harvlab/resume", json={"action_id": run.id})
    assert r.status_code == 202
    [f] = [f for f in r.get_json()["findings"] if f["code"] == "plan-changed"]
    assert f["level"] == "warning" and "1 -> 2" in f["detail"]
    assert wait(r.get_json()["action_id"]).status == "done"


def test_apply_waits_for_the_plan_thread_to_release(world, monkeypatch):
    """Le statut d'une action Terraform est écrit avant que son fil rende la
    déclaration ; l'apply de la chaîne attend ce rendu au lieu d'échouer en
    « already running » (vu sous charge). Fil ralenti entre son statut final
    et son `finally` pour le montrer à coup sûr. Mutation jouée : attente
    retirée (l'élément tombe en erreur `running`)."""
    tr.seed(world["client"])
    real = wapp._tf_decl_runner_body

    def slow_body(*a, **k):
        out = real(*a, **k)
        time.sleep(0.4)
        return out
    monkeypatch.setattr(wapp, "_tf_decl_runner_body", slow_body)
    run = start(world)
    assert run.status == "done", run.result
