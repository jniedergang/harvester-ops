"""2.5.0 : phases capi et clusters d'un plan dans la console (tâche 6).

Outil `harvester-capi` simulé : ses lectures (`status`, `state`, `check
--many`) par `_deploy_capi_json`, ses actions par le relais
`_vm_transfer_runner`. Le lancement lui-même est le vrai (`_capi_launch`,
`_cli_action` : parent, libellé, identité figée, fichier de demande).

* chaîne Terraform, capi, clusters ; pile déjà prête (aucune action fille) ;
  pile d'une autre version ou Harvester trop ancien (bloqué avant toute
  action) ;
* trois clusters dont le deuxième échoue (troisième fait, phase partielle) ;
  dépendance sur un cluster en échec (sautée) ;
* reprise : cluster fait revérifié et sauté, cluster laissé après délai
  repris et attendu, cluster en échec en `cluster-failed` et jamais
  supprimé ;
* arrêt pendant le premier cluster (la création va à son terme, le
  deuxième est sauté) ; essai sans écriture ;
* `parent` sur chaque fille ; `undo` seulement pour `created: true` ; aucun
  chemin ni kubeconfig dans le résultat et actions.db ;
* constats du contrôle (un par code), résumé, rôles ;
* `check --many` rejoué au début de la phase (réseau créé par Terraform) ;
* `_capi_launch` sans contexte de requête.

Mutations jouées (rapport des tâches 5 à 8) : M-recheck (pas de `check
--many` au début de la phase), M-adopt (cluster d'une autre origine repris),
M-request-ctx (`_capi_launch` qui relit `current_cluster_identity()`).
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
import capi_cluster as cc  # noqa: E402
import test_deploy_routes_240 as tr  # noqa: E402
import test_deploy_runner_240 as trn  # noqa: E402

env = tr.env
world = trn.world
CAPI_READS = {k: v for k, v in json.loads(
    (ROOT / "tests" / "fixtures" / "deploy" / "harv1-capi-reads.json").read_text()).items()
    if not k.startswith("_")}
PROVIDERS = [{"type": "bootstrap", "name": "rke2", "version": "v0.25.2"},
             {"type": "controlPlane", "name": "rke2", "version": "v0.25.2"},
             {"type": "infrastructure", "name": "harvester", "version": "v0.10.1"},
             {"type": "addon", "name": "helm", "version": "v0.6.4", "optional": True}]
BUNDLE = Path("/srv/hops-bundles/capi-bundle-20261105.tar.gz")
COMPONENTS = Path("/srv/hops-bundles/.components/c1")


class FakeCapi:
    """Le cluster Harvester vu par harvester-capi, simulé."""

    def __init__(self):
        self.stack = {"turtles": True, "ready": True, "harvester_version": "v1.9.0", "differs": [],
                      "missing": []}
        self.clusters = {}            # nom -> {"origin", "ready", "failed"}
        self.namespaces = set()
        self.missing_networks = set()
        self.fleet = []               # constats de flotte ajoutés par check --many
        self.outcome = {}             # nom -> done | error | timeout | cancel
        self.gate = {}                # nom -> Event : la création l'attend
        self.ready_after_polls = {}   # nom -> lectures avant d'être prêt
        self.reads, self.launches = [], []

    # lectures ------------------------------------------------------------
    def json(self, kc, sub, args, spec=None, timeout=180):
        self.reads.append((sub, list(args)))
        if sub == "status":
            return dict(self.stack), 0 if self.stack["ready"] else 1, None
        if sub == "state":
            name = args[args.index("--name") + 1].split("/")[0]
            c = self.clusters.get(name)
            if c is None:
                return {"present": False, "namespace": name, "name": name,
                        "namespace_present": name in self.namespaces}, 1, None
            left = self.ready_after_polls.get(name)
            if left is not None:
                if left <= 0:
                    c["ready"] = True
                self.ready_after_polls[name] = left - 1
            st = {"phase": "Failed" if c["failed"] else "Provisioned" if c["ready"] else "Provisioning",
                  "ready": c["ready"], "failed": c["failed"], "cp": [1, 1], "workers": [1, 1]}
            return {"present": True, "namespace": name, "name": name, "origin": dict(c["origin"]),
                    "state": st}, 0, None
        if sub == "check":
            out = []
            for s in spec:
                ref = f"{s['namespace']}/{s['name']}"
                out.append({"code": "endpoint-dhcp", "level": "ok", "facts": {}, "cluster": ref})
                out.append({"code": "cp-single", "level": "warn", "facts": {}, "cluster": ref})
                if s.get("network") in self.missing_networks:
                    out.append({"code": "network-missing", "level": "block", "facts": {"network": s["network"]},
                                "cluster": ref})
            out += [dict(f) for f in self.fleet]
            out.append({"code": "fleet-sizes", "level": "ok", "facts": {"clusters": len(spec)}})
            blocked = any(f["level"] == "block" for f in out)
            return {"findings": out, "blocked": blocked}, 2 if blocked else 0, None
        raise AssertionError(sub)

    # actions (relais de _cli_action) ---------------------------------------
    def runner(self, run, cmd, capture=False, summarize=None):
        sub = cmd[2]
        args = cmd[3:]
        self.launches.append((sub, args, run))
        run.status = "running"
        rc = 0
        if sub == "install":
            self.stack.update(ready=True, missing=[])
        elif sub == "create":
            spec = json.loads(Path(args[args.index("--spec") + 1]).read_text())
            name = spec["name"]
            if "--dry-run" not in args:
                if name in self.gate:
                    self.gate[name].wait(10)
                origin = cc.parse_origin(args[args.index("--origin") + 1])
                out = self.outcome.get(name, "done")
                if out == "cancel":
                    rc = 3
                else:
                    self.clusters[name] = {"origin": origin, "ready": out == "done", "failed": out == "error"}
                    rc = 0 if out == "done" else 1
        elif sub == "delete":
            self.clusters.pop(args[args.index("--name") + 1].split("/")[0], None)
        run.exit_code = rc
        run.status = {0: "done", 3: "cancelled"}.get(rc, "error")
        run.ended_at = time.time()
        run.emit({"type": "status", "status": run.status, "exit_code": rc, "ts": time.time()})
        run.close()

    def subs(self, sub):
        return [x for x in self.launches if x[0] == sub]


@pytest.fixture()
def capi(world, monkeypatch, tmp_path):
    fake = FakeCapi()
    monkeypatch.setattr(wapp, "_deploy_capi_json", fake.json)
    monkeypatch.setattr(wapp, "_vm_transfer_runner", fake.runner)
    monkeypatch.setattr(wapp, "_capi_script", lambda: Path(sys.executable))
    monkeypatch.setattr(wapp, "_capi_bundle_active_path", lambda: BUNDLE)
    monkeypatch.setattr(wapp, "_capi_components_dir", lambda: COMPONENTS)
    monkeypatch.setattr(wapp, "_capi_bundle_providers", lambda: [dict(p) for p in PROVIDERS])
    monkeypatch.setattr(wapp, "_deploy_read_capi", lambda kc, cluster: json.loads(json.dumps(CAPI_READS)))
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", str(tmp_path / "work"))
    world["capi"] = fake
    return world


def template(network="default/production", variables=()):
    return {"description": "petit", "variables": list(variables),
            "spec": {"k8s_version": "v1.34.11", "cp_replicas": 1, "worker_replicas": 1, "cpu": 2,
                     "memory": "4Gi", "disk_size": "20Gi", "image": "default/leap",
                     "ssh_keypair": "default/ju", "network": network, "gateway": "172.16.0.1",
                     "subnet_mask": "255.255.0.0", "ip_pool": "capi-vm-pool"}}


def plan(names=("{{cluster}}-apps",), deps=None, capi_on=True, terraform=False, readiness=False,
         tmpl="rke2-petit"):
    items = []
    for n in names:
        it = {"template": tmpl, "name": n}
        if deps and n in deps:
            it["depends_on"] = deps[n]
        items.append(it)
    phases = {"readiness": {"enabled": readiness, "timeout_minutes": 1},
              "capi": {"enabled": capi_on, "timeout_minutes": 10},
              "clusters": {"enabled": True, "timeout_minutes": 30, "items": items}}
    if terraform:
        phases["terraform"] = {"enabled": True, "templates": [{"template": "cles"}]}
    return {"description": "capi", "variables": ["equipe"], "phases": phases}


def seed(c, body=None, tmpl=None, tf=False):
    if tf:
        assert c.put("/api/baremetal/tf-templates/cles", json=tr.template_body()).status_code == 201
    r = c.put("/api/baremetal/capi-templates/rke2-petit", json=tmpl or template())
    assert r.status_code == 201, r.get_json()
    r = c.put("/api/baremetal/plans/site", json=body or plan())
    assert r.status_code == 201, r.get_json()


def check(c):
    r = c.post("/api/deploy/harvlab/check", json={"plan": "site", "variables": {"equipe": "ops"}})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def found(body):
    return {f["code"]: f for f in body["findings"]}


def phase(run, pid):
    return next(p for p in run.result["phases"] if p["id"] == pid)


def items(run):
    return {i["cluster"]: i for i in phase(run, "clusters").get("items") or []}


def children(run_id):
    with wapp.ACTIONS_LOCK:
        return [a for a in wapp.ACTIONS.values() if a.parent == run_id]


# --- déroulé -------------------------------------------------------------------

def test_chain_terraform_capi_then_clusters(capi):
    w, fake = capi, capi["capi"]
    fake.stack.update(ready=False, missing=["infrastructure/harvester"])
    seed(w["client"], plan(terraform=True), tf=True)
    run = trn.start(w)
    assert run.status == "done", run.result
    assert [p["id"] for p in run.result["phases"]] == ["readiness", "terraform", "capi", "clusters"]
    cap = phase(run, "capi")
    [inst] = fake.subs("install")
    assert cap["status"] == "done" and cap["installed"] is True and cap["actions"] == [inst[2].id]
    assert cap["providers"] == ["bootstrap/rke2 v0.25.2", "controlPlane/rke2 v0.25.2",
                                "infrastructure/harvester v0.10.1", "addon/helm v0.6.4"]
    # 2.6.0 : répertoire d'état et nom du cluster Harvester (registres rattachés)
    assert inst[1] == ["--kubeconfig", w["kc"], f"--state-dir={wapp._bm_profiles_state()}", "--harvester-name=harvlab",
                       "--cluster", "harvlab", "--components", str(BUNDLE),
                       "--timeout", "600"]                                  # pas de nœuds : pas d'images
    [create] = fake.subs("create")
    a = create[1]
    assert a[a.index("--identity") + 1] == "restricted" and a[a.index("--timeout") + 1] == "1800"
    assert a[a.index("--origin") + 1] == f"plan=site,template=rke2-petit,run={run.id}"
    assert a[a.index("--components") + 1] == str(COMPONENTS)
    it = items(run)["harvlab-apps"]
    assert it == {"cluster": "harvlab-apps", "ref": "harvlab-apps/harvlab-apps", "template": "rke2-petit",
                  "template_revision": 1, "identity": "restricted", "created": True, "namespace_created": True,
                  "actions": [create[2].id], "undo": {"kind": "capi-cluster", "ref": "harvlab-apps/harvlab-apps"},
                  "status": "done", "message": "created"}
    kids = children(run.id)
    assert {k.action for k in kids} >= {"capi-install:harvlab", "capi-cluster-create:harvlab-apps/harvlab-apps",
                                        "tf-plan-decl:site-cles:1", "tf-apply-decl:site-cles:1"}
    assert all(k.parent == run.id for k in kids) and set(run.children) >= {k.id for k in kids}
    assert ("phase-capi", "done") in trn.steps(run) and ("k8s-harvlab-apps", "done") in trn.steps(run)


def test_stack_ready_means_no_child_action(capi):
    seed(capi["client"])
    run = trn.start(capi)
    cap = phase(run, "capi")
    assert cap["installed"] is False and cap["actions"] == [] and cap["message"] == "stack ready at the bundle versions"
    assert capi["capi"].subs("install") == []


def test_nodes_declared_push_the_images(capi, monkeypatch):
    capi["capi"].stack.update(ready=False, missing=["infrastructure/harvester"])
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"clusters": [
        {"name": "harvlab", "kubeconfig": capi["kc"], "nodes": [{"ip": "172.16.2.61"}]}]})
    seed(capi["client"])
    trn.start(capi)
    assert capi["capi"].subs("install")[0][1][-1] == "--push-images"


@pytest.mark.parametrize("stack,code", [
    ({"differs": [{"provider": "infrastructure/harvester", "installed": "v0.9.0", "bundle": "v0.10.1"}],
      "ready": False}, "capi-version-differs"),
    ({"harvester_version": "v1.8.2", "ready": False, "turtles": False}, "capi-needs-harvester-1.9"),
])
def test_blocking_stack_starts_nothing(capi, stack, code):
    capi["capi"].stack.update(stack)
    seed(capi["client"])
    with wapp.ACTIONS_LOCK:
        before = set(wapp.ACTIONS)
    r = capi["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops"}})
    assert r.status_code == 400 and code in found(r.get_json())
    assert found(r.get_json())[code]["level"] == "blocking" and found(r.get_json())[code]["phase"] == "capi"
    assert capi["capi"].launches == []
    with wapp.ACTIONS_LOCK:
        assert set(wapp.ACTIONS) == before


def test_three_clusters_second_fails_third_done(capi):
    fake = capi["capi"]
    fake.outcome["harvlab-b"] = "error"
    seed(capi["client"], plan(names=("{{cluster}}-a", "{{cluster}}-b", "{{cluster}}-c")))
    run = trn.start(capi)
    assert run.status == "error" and run.result["status"] == "partial"
    got = items(run)
    assert [got[n]["status"] for n in ("harvlab-a", "harvlab-b", "harvlab-c")] == ["done", "error", "done"]
    assert got["harvlab-b"]["message"].startswith("creation failed (action ")
    # en échec mais créé par cette exécution : Défaire (2.7.0) saura le retirer
    assert got["harvlab-b"]["created"] is True and got["harvlab-b"]["undo"]["ref"] == "harvlab-b/harvlab-b"
    assert phase(run, "clusters")["status"] == "partial"
    assert [x[0] for x in fake.launches] == ["create", "create", "create"]       # séquentiel, jamais delete


def test_dependency_on_a_failed_cluster_is_skipped(capi):
    capi["capi"].outcome["harvlab-a"] = "error"
    seed(capi["client"], plan(names=("{{cluster}}-a", "{{cluster}}-b"), deps={"{{cluster}}-b": ["{{cluster}}-a"]}))
    run = trn.start(capi)
    got = items(run)
    assert got["harvlab-b"]["status"] == "skipped" and got["harvlab-b"]["message"] == "depends on harvlab-a, not done"
    assert len(capi["capi"].subs("create")) == 1


def resume(w, run):
    r = w["client"].post("/api/deploy/harvlab/resume", json={"action_id": run.id})
    assert r.status_code == 202, r.get_json()
    return trn.wait(r.get_json()["action_id"])


def test_resume_checks_done_adopts_left_and_keeps_failed(capi):
    fake = capi["capi"]
    fake.outcome.update({"harvlab-b": "timeout", "harvlab-c": "error"})
    seed(capi["client"], plan(names=("{{cluster}}-a", "{{cluster}}-b", "{{cluster}}-c")))
    run = trn.start(capi)
    assert {n: i["status"] for n, i in items(run).items()} == {"harvlab-a": "done", "harvlab-b": "error",
                                                               "harvlab-c": "error"}
    fake.ready_after_polls["harvlab-b"] = 3     # contrôle, début de phase, élément : puis disponible
    run2 = resume(capi, run)
    got = items(run2)
    assert got["harvlab-a"]["checked"] is True and got["harvlab-a"]["undo"]["ref"] == "harvlab-a/harvlab-a"
    assert got["harvlab-b"]["status"] == "done" and got["harvlab-b"]["message"] == "adopted, now available"
    assert got["harvlab-b"]["created"] is True                       # créé par l'exécution reprise
    assert got["harvlab-c"]["status"] == "error" and got["harvlab-c"]["message"] == "cluster-failed"
    assert len(fake.subs("create")) == 3 and fake.subs("delete") == []    # rien recréé, rien supprimé
    assert "harvlab-c" in fake.clusters
    assert ("k8s-harvlab-a", "skipped") in trn.steps(run2)


def test_adopted_cluster_without_previous_creation_has_no_undo(capi):
    capi["capi"].clusters["harvlab-apps"] = {"origin": {"plan": "site", "template": "rke2-petit"},
                                             "ready": True, "failed": False}
    seed(capi["client"])
    body = check(capi["client"])
    assert found(body)["cluster-exists"]["level"] == "info"
    run = trn.start(capi)
    it = items(run)["harvlab-apps"]
    assert it["status"] == "done" and it["created"] is False and "undo" not in it
    assert it["message"] == "already present, adopted" and capi["capi"].subs("create") == []


def test_stop_during_the_first_cluster_lets_it_finish(capi):
    fake = capi["capi"]
    fake.gate["harvlab-a"] = threading.Event()
    seed(capi["client"], plan(names=("{{cluster}}-a", "{{cluster}}-b")))
    r = capi["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops"}})
    rid = r.get_json()["action_id"]
    end = time.time() + 10
    while not fake.subs("create") and time.time() < end:
        time.sleep(0.02)
    assert capi["client"].post("/api/deploy/harvlab/stop", json={"action_id": rid}).status_code == 202
    child = fake.subs("create")[0][2]
    assert not getattr(child, "_cancel", False)          # décision 175 : jamais transmis
    fake.gate["harvlab-a"].set()
    run = trn.wait(rid)
    got = items(run)
    assert run.status == "cancelled" and got["harvlab-a"]["status"] == "done"
    assert got["harvlab-b"]["status"] == "skipped" and got["harvlab-b"]["message"] == "cancelled"
    assert child.status == "done" and len(fake.subs("create")) == 1


def test_dry_run_writes_nothing(capi):
    fake = capi["capi"]
    seed(capi["client"])
    run = trn.start(capi, dry_run=True)
    assert run.status == "done", run.result
    [create] = fake.subs("create")
    assert "--dry-run" in create[1] and create[2].dry_run is True
    assert fake.clusters == {} and items(run)["harvlab-apps"]["message"] == "checked and rendered (dry run)"
    assert "undo" not in items(run)["harvlab-apps"]


def test_dry_run_with_a_stack_to_declare(capi):
    fake = capi["capi"]
    fake.stack.update(ready=False, missing=["infrastructure/harvester", "bootstrap/rke2",
                                            "compatibility/ingress-expose"])     # harv1, 05/11/2026
    seed(capi["client"])
    run = trn.start(capi, dry_run=True)
    assert run.status == "done"
    assert phase(run, "capi")["message"] == ("would declare 2 provider(s) and restore "
                                             "compatibility/ingress-expose") and fake.launches == []
    assert phase(run, "clusters")["message"] == "dry run: the clusters are checked once the stack is declared"


def test_no_path_nor_kubeconfig_in_result_or_db(capi):
    seed(capi["client"])
    run = trn.start(capi)
    con = sqlite3.connect(capi["db"])
    rows = con.execute("SELECT cmd, result, events FROM actions").fetchall()
    con.close()
    blob = json.dumps(run.to_dict()) + "".join(str(x) for r in rows for x in r)
    for kid in children(run.id):
        blob += json.dumps(kid.to_dict())
    assert rows and capi["kc"] not in blob and "/work/" not in blob and "--kubeconfig" not in blob


# --- contrôle repris au début de la phase ------------------------------------------

def test_network_made_by_terraform_is_rechecked_at_phase_start(capi, monkeypatch):
    """Au contrôle, le réseau désigné par variable n'existe pas encore :
    `refs-later` (information). La phase Terraform le crée ; au début de la
    phase clusters, `check --many` est rejoué et passe."""
    fake = capi["capi"]
    fake.missing_networks.add("default/ops-net")
    seed(capi["client"], plan(terraform=True), tmpl=template(network="default/{{equipe}}-net",
                                                              variables=("equipe",)), tf=True)
    f = found(check(capi["client"]))
    assert f["refs-later"]["level"] == "info" and "network-missing" not in f
    assert "check=network-missing" in f["refs-later"]["detail"]
    real = wapp._tf_decl_launch

    def tf_launch(*a, **kw):
        if a[3] == "apply":
            fake.missing_networks.clear()             # la déclaration crée le réseau
        return real(*a, **kw)
    monkeypatch.setattr(wapp, "_tf_decl_launch", tf_launch)
    run = trn.start(capi)
    assert run.status == "done", run.result
    checks = [r for r in fake.reads if r[0] == "check"]
    assert len(checks) == 3                # contrôle, lancement, puis début de la phase (réseau là)


def test_network_still_missing_at_phase_start_blocks_before_any_write(capi):
    """M-recheck : sans le contrôle rejoué, la création partirait."""
    fake = capi["capi"]
    fake.missing_networks.add("default/ops-net")
    seed(capi["client"], plan(terraform=True), tmpl=template(network="default/{{equipe}}-net",
                                                              variables=("equipe",)), tf=True)
    run = trn.start(capi)
    ph = phase(run, "clusters")
    assert run.status == "error" and ph["status"] == "error"
    assert ph["message"] == "blocked by the pre-check: network-missing"
    assert ph["blocked_by"][0]["code"] == "network-missing" and fake.subs("create") == []


def test_capacity_rechecked_at_phase_start_ignores_existing_clusters(capi):
    """Reprise : un cluster déjà présent n'est pas recompté dans la capacité."""
    fake = capi["capi"]
    fake.outcome["harvlab-b"] = "error"
    seed(capi["client"], plan(names=("{{cluster}}-a", "{{cluster}}-b")))
    run = trn.start(capi)
    fake.clusters.pop("harvlab-b")                     # supprimé par la personne (décision 176)
    fake.outcome.clear()
    fake.reads.clear()
    run2 = resume(capi, run)
    assert run2.status == "done", run2.result
    rechecked = [r for r in fake.reads if r[0] == "check"]
    assert rechecked                                     # seul harvlab-b, absent, est contrôlé


def test_fleet_finding_blocks_the_phase(capi):
    capi["capi"].fleet.append({"code": "fleet-memory-short", "level": "block",
                               "facts": {"needed": 2, "free": 1, "clusters": 1}})
    seed(capi["client"])
    body = check(capi["client"])
    f = found(body)
    assert f["fleet-memory-short"]["level"] == "blocking" and f["fleet-memory-short"]["phase"] == "clusters"
    assert body["blocking"] is True


# --- constats du contrôle -------------------------------------------------------------

def test_check_summary_and_estimate(capi):
    capi["capi"].stack.update(ready=False, missing=["infrastructure/harvester"])
    capi["capi"].clusters["harvlab-b"] = {"origin": {"plan": "site", "template": "rke2-petit"},
                                          "ready": True, "failed": False}
    seed(capi["client"], plan(names=("{{cluster}}-a", "{{cluster}}-b")))
    body = check(capi["client"])
    s = body["summary"]
    assert s["clusters"] == [{"name": "harvlab-a", "template": "rke2-petit", "vms": 2, "exists": False},
                             {"name": "harvlab-b", "template": "rke2-petit", "vms": 2, "exists": True}]
    assert s["capi"] == {"providers": [f"{p['type']}/{p['name']} {p['version']}" for p in PROVIDERS],
                         "ready": False, "installed_version_differs": False}
    assert s["estimate_minutes"] == 2 + 5 + 10              # pile, un cluster à créer avec un worker
    f = found(body)
    assert f["cp-single"]["level"] == "warning" and "endpoint-dhcp" not in f   # informations par cluster omises
    assert f["fleet-sizes"]["level"] == "info"


@pytest.mark.parametrize("setup,code,level,phase_id", [
    ("no-bundle", "capi-bundle-missing", "blocking", "capi"),
    ("no-turtles", "capi-bundle-no-turtles", "blocking", "capi"),
    ("old", "capi-needs-harvester-1.9", "blocking", "capi"),
    ("differs", "capi-version-differs", "blocking", "capi"),
    ("stack-missing", "capi-stack-missing", "blocking", "clusters"),
    ("template-missing", "capi-template-missing", "blocking", "clusters"),
    ("var-missing", "cluster-var-missing", "blocking", "clusters"),
    ("name-invalid", "var-invalid", "blocking", "clusters"),
    ("taken", "cluster-name-taken", "blocking", "clusters"),
    ("exists", "cluster-exists", "info", "clusters"),
    ("failed", "cluster-failed", "warning", "clusters"),
    ("identity", "identity-rules-unknown", "blocking", "clusters"),
    ("unreadable", "capi-check-failed", "blocking", "capi"),
])
def test_each_check_code(capi, monkeypatch, setup, code, level, phase_id):
    fake = capi["capi"]
    body = plan()
    tmpl = template()
    if setup == "no-bundle":
        monkeypatch.setattr(wapp, "_capi_bundle_active_path", lambda: None)
        monkeypatch.setattr(wapp, "_capi_bundle_providers", lambda: [])
    elif setup == "no-turtles":
        monkeypatch.setattr(wapp, "_capi_bundle_providers", lambda: [])
    elif setup == "old":
        fake.stack.update(harvester_version="v1.8.2")
    elif setup == "differs":
        fake.stack["differs"] = [{"provider": "bootstrap/rke2", "installed": "v0.24.0", "bundle": "v0.25.2"}]
    elif setup == "stack-missing":
        fake.stack.update(ready=False, missing=["infrastructure/harvester"])
        body = plan(capi_on=False)
    elif setup == "template-missing":
        body = plan(tmpl="absent")
    elif setup == "var-missing":
        body = plan()
        tmpl = template(network="{{reseau}}", variables=("reseau",))
    elif setup == "name-invalid":
        body = plan(names=("{{equipe}}-{{cluster}}-x",))
    elif setup in ("taken", "exists", "failed"):
        fake.clusters["harvlab-apps"] = {
            "origin": {"plan": "other"} if setup == "taken" else {"plan": "site", "template": "rke2-petit"},
            "ready": setup == "exists", "failed": setup == "failed"}
    elif setup == "identity":
        fake.fleet.append({"code": "identity-rules-unknown", "level": "block", "facts": {"version": "v0.11.0"},
                           "cluster": "harvlab-apps/harvlab-apps"})
    elif setup == "unreadable":
        real = fake.json
        monkeypatch.setattr(wapp, "_deploy_capi_json",
                            lambda kc, sub, args, spec=None, timeout=180: (None, None, "timed out reading")
                            if sub == "status" else real(kc, sub, args, spec, timeout))
    seed(capi["client"], body, tmpl=tmpl)
    variables = {"equipe": "Ops"} if setup == "name-invalid" else {"equipe": "ops"}
    r = capi["client"].post("/api/deploy/harvlab/check", json={"plan": "site", "variables": variables})
    f = found(r.get_json())
    assert code in f, r.get_json()["findings"]
    assert f[code]["level"] == level and f[code]["phase"] == phase_id
    if level == "blocking":
        assert r.get_json()["blocking"] is True


def test_capi_later_when_the_chain_installs_the_cluster(capi):
    seed(capi["client"])
    with wapp.app.test_request_context():
        findings, summary, ctx = wapp._deploy_prepare("newone", "site", {"equipe": "ops"}, vip="172.16.2.70",
                                                      install=True)
    f = {x["code"]: x for x in findings}
    assert f["capi-later"]["level"] == "info"
    assert capi["capi"].reads == []                           # rien lu : le cluster n'existe pas
    assert [e["name"] for e in ctx["clusters"]] == ["newone-apps"]
    assert summary["clusters"] == [{"name": "newone-apps", "template": "rke2-petit", "vms": 2, "exists": False}]


@pytest.mark.parametrize("role,status", [("viewer", 403), ("operator", 202), ("admin", 202)])
def test_roles_for_a_clusters_plan(capi, monkeypatch, role, status):
    seed(capi["client"])
    monkeypatch.setattr(wapp, "current_role", lambda: role)
    r = capi["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops"}})
    assert r.status_code == status, r.get_json()
    if status == 202:
        trn.wait(r.get_json()["action_id"])


def test_readiness_reads_cluster_api_when_enabled(capi, monkeypatch):
    seen = []
    monkeypatch.setattr(wapp, "_deploy_read_capi", lambda kc, cluster: seen.append(cluster) or {
        "server_version": "v1.9.0", "traefik": None, "capi_providers": CAPI_READS["capi_providers"]})
    seed(capi["client"], plan(readiness=True))
    loaded = wapp._dpl.load_plan(capi["state"], "site")

    class Ctx:
        def emit_step(self, *a):
            pass
    ph = wapp._deploy_readiness_phase("harvlab", lambda: capi["kc"], loaded)
    out = ph.execute(Ctx(), {})
    assert seen == ["harvlab"] and out.status == "retry"
    assert [u["code"] for u in out.data["unmet"]] == ["rke2-traefik-no-address"]
    # sans phase Cluster API, aucune de ces lectures
    loaded["phases"]["capi"]["enabled"] = loaded["phases"]["clusters"]["enabled"] = False
    seen.clear()
    assert wapp._deploy_readiness_phase("harvlab", lambda: capi["kc"], loaded).execute(Ctx(), {}).status == "done"
    assert seen == []


def test_plan_put_returns_the_warnings_of_a_240_plan(capi):
    c = capi["client"]
    doc = {"description": "x", "phases": {"readiness": {"enabled": True},
                                          "capi": {"enabled": False, "providers": {"caaph": True}}}}
    r = c.put("/api/baremetal/plans/old", json=doc)
    assert r.status_code == 201
    assert r.get_json()["warnings"] == ["phases.capi.providers: unknown key, ignored (phase disabled)"]
    assert "warnings" not in c.get("/api/baremetal/plans/old").get_json()
    assert c.get("/api/baremetal/plans").get_json()["available"] == ["readiness", "terraform", "registry", "capi",
                                                                      "clusters", "artefacts"]


def test_bundles_route_lists_the_active_providers(capi, monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "CAPI_BUNDLE_DIR", tmp_path / "bundles")
    r = capi["client"].get("/api/capi/bundles")
    assert r.get_json()["active_providers"] == ["bootstrap/rke2 v0.25.2", "controlPlane/rke2 v0.25.2",
                                                "infrastructure/harvester v0.10.1", "addon/helm v0.6.4"]
    monkeypatch.setattr(wapp, "_capi_bundle_providers", lambda: [])
    assert capi["capi"] and capi["client"].get("/api/capi/bundles").get_json()["active_providers"] == []


# --- lancement hors requête ---------------------------------------------------------------

def test_capi_launch_outside_any_request(capi, monkeypatch):
    """M-request-ctx : `_capi_launch` ne lit ni la requête ni l'identité de
    la personne ; lancé depuis un fil nu, il passe et garde l'identité
    figée qu'on lui donne."""
    def boom():
        raise AssertionError("current_cluster_identity read outside a request")
    monkeypatch.setattr(wapp, "current_cluster_identity", boom)
    got = {}

    def work():
        try:
            got["out"] = wapp._capi_launch("harvlab", capi["kc"], "delete", ["--name", "a/a"], parent=None,
                                           cluster_user="bob", label="capi-cluster-delete:a/a")
        except BaseException as e:               # noqa: BLE001
            got["exc"] = e
    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    assert "exc" not in got, got.get("exc")
    run, err = got["out"]
    assert err is None and run.cluster_user == "bob" and run.action == "capi-cluster-delete:a/a"
    trn.wait(run.id)
    assert capi["capi"].subs("delete")[0][1] == ["--kubeconfig", capi["kc"], f"--state-dir={wapp._bm_profiles_state()}",
                                                 "--harvester-name=harvlab", "--name", "a/a"]


def test_capi_launch_without_the_tool(capi, monkeypatch):
    monkeypatch.setattr(wapp, "_capi_script", lambda: None)
    run, err = wapp._capi_launch("harvlab", capi["kc"], "install", [], cluster_user=None)
    assert run is None and err == {"error": "harvester-capi.py not deployed"}


def test_a_cluster_of_another_origin_is_never_adopted(capi, monkeypatch):
    """M-adopt (déroulé) : un cluster du même nom apparu entre le contrôle et
    la phase, posé par autre chose, reste en erreur et n'est pas touché."""
    fake = capi["capi"]
    seed(capi["client"], plan(terraform=True), tf=True)
    real = wapp._tf_decl_launch

    def tf_launch(*a, **kw):
        if a[3] == "apply":
            fake.clusters["harvlab-apps"] = {"origin": {"plan": "site", "template": "other"},
                                             "ready": True, "failed": False}
        return real(*a, **kw)
    monkeypatch.setattr(wapp, "_tf_decl_launch", tf_launch)
    run = trn.start(capi)
    it = items(run)["harvlab-apps"]
    assert it["status"] == "error" and it["message"] == "cluster-name-taken" and it["created"] is False
    assert fake.subs("create") == [] and fake.subs("delete") == []


# --- revue finale 2.5.0 (I3, D3) : installer la pile est un geste d'admin ------

@pytest.mark.parametrize("role,ready,blocked", [
    ("operator", False, True),     # la pile est à installer : réservé à l'admin
    ("operator", True, False),     # déjà prête : la phase ne fait que la vérifier
    ("admin", False, False),
])
def test_installing_the_stack_needs_the_admin_role(capi, monkeypatch, role, ready, blocked):
    capi["capi"].stack.update(ready=ready, missing=[] if ready else ["infrastructure/harvester"])
    seed(capi["client"])
    monkeypatch.setattr(wapp, "current_role", lambda: role)
    got = found(check(capi["client"]))
    assert ("capi-install-admin" in got) is blocked
    if blocked:
        assert got["capi-install-admin"]["level"] == "blocking" and got["capi-install-admin"]["phase"] == "capi"


def test_operator_launch_is_refused_when_the_stack_must_be_installed(capi, monkeypatch):
    capi["capi"].stack.update(ready=False, missing=["infrastructure/harvester"])
    seed(capi["client"])
    monkeypatch.setattr(wapp, "current_role", lambda: "operator")
    r = capi["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops"}})
    assert r.status_code == 400 and "capi-install-admin" in found(r.get_json())
    assert capi["capi"].launches == []


def test_an_install_chain_of_an_operator_is_told(capi):
    seed(capi["client"])
    with wapp.app.test_request_context():
        findings, _s, _c = wapp._deploy_prepare("newone", "site", {"equipe": "ops"}, vip="172.16.2.70",
                                                install=True, role="operator")
    assert "capi-install-admin" in {f["code"] for f in findings}


@pytest.mark.parametrize("role,installs", [("operator", False), ("admin", True), (None, True)])
def test_the_capi_phase_refuses_the_install_itself(capi, role, installs):
    """Rejoué dans la phase : la pile a pu disparaître depuis le contrôle
    (reprise, chaîne)."""
    fake = capi["capi"]
    fake.stack.update(ready=False, missing=["infrastructure/harvester"])
    seed(capi["client"])
    run = wapp.ActionRun("t" * 12, "deploy:harvlab", "harvlab", [], dry_run=False)
    run.launch_role = role
    plan_doc = {"phases": {"capi": {"enabled": True, "timeout_minutes": 1}}}
    out = wapp._deploy_capi_phase(run, "harvlab", lambda: capi["kc"], plan_doc).execute(None, None)
    if installs:
        assert out.status == "done" and fake.subs("install")
    else:
        assert out.status == "error" and out.message == "capi-install-admin" and fake.subs("install") == []


def test_the_optional_provider_is_counted_in_the_message(capi):
    """Revue finale n5 : CAAPH (facultatif) est déclaré avec la pile."""
    capi["capi"].stack.update(ready=False, missing=["bootstrap/rke2", "controlPlane/rke2",
                                                    "infrastructure/harvester"],
                              optional=[{"provider": "addon/helm", "ready": False}])
    seed(capi["client"])
    run = trn.start(capi)
    assert phase(run, "capi")["message"] == "declared 4 provider(s)"
