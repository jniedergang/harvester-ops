"""2.7.0 (tâche 5) : « Défaire le déploiement » dans la console.

L'exécution d'origine est écrite telle que la chaîne la laisse (artefacts,
cluster enfant, registre, déclaration Terraform marqués `created` et
`undo`) ; les outils sont simulés (lectures par `_deploy_capi_json`, gestes
par le relais `_vm_transfer_runner`), le lancement est le vrai.

* plan de destruction : ordre, cases par défaut (registre décoché),
  détails, limites dites ; administrateur seulement ;
* défaire : artefacts, cluster, Terraform, dans cet ordre, en actions
  filles ; `undone_by` sur l'exécution d'origine, persistant ;
* retrait d'un chart en échec : son cluster sauté, Terraform poursuivi,
  `partial` ; reprise : ce qui est parti revérifié absent et sauté ;
* gardes : nom tapé, rôle, déroulé en cours (verrou), essai, autre
  cluster, données du registre sur un second nom, pas de « défaire le
  défaire ».
"""

import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import registries as rg  # noqa: E402
import test_deploy_artefacts_270 as ta  # noqa: E402
import test_deploy_routes_240 as tr  # noqa: E402,F401
import test_deploy_capi_250 as tc  # noqa: E402
import test_deploy_runner_240 as trn  # noqa: E402

env = tc.env
world = tc.world
capi = tc.capi
art = ta.art


class FakeUndo(ta.FakeArt):
    def __init__(self, base):
        super().__init__()
        self.__dict__.update({k: v for k, v in base.__dict__.items() if k in ("clusters", "applied")})
        self.removals = []
        self.fail_remove = set()
        self.cmds = []

    def json(self, kc, sub, args, spec=None, timeout=180):
        if sub == "manifest-verify":
            ns, _c, item = args[args.index("--name") + 1].split("/")
            a = self.applied.get((ns, item)) or {}
            return {"present": (ns, item) in self.applied, "ok": (ns, item) in self.applied,
                    "origin": dict(a.get("origin") or {})}, 0, None
        if sub == "manifest-remove" and "--dry-run" in args:
            ns, c, item = args[args.index("--name") + 1].split("/")
            return {"item": item, "cluster": f"{ns}/{c}", "cluster_present": True,
                    "delete": ["ConfigMap equipe/conf"], "kept": [], "refused": []}, 0, None
        if sub == "delete" and "--dry-run" in args:
            ref = args[args.index("--name") + 1]
            return {"cluster": ref, "present": ref.split("/")[1] in self.clusters, "machines": 2,
                    "vms": ["web-cp-1", "web-w-1"], "volumes": ["web-cp-1-disk-0"], "loadbalancers": [],
                    "namespace_removed": True}, 0, None
        return super().json(kc, sub, args, spec, timeout)

    def runner(self, run, cmd, capture=False, summarize=None):
        tool, sub = Path(cmd[1]).name, cmd[2]
        args = cmd[3:]
        self.cmds.append((sub, list(args)))
        if tool == "harvester-registry.py":
            self.removals.append(("registry", args))
            rg.delete(self.state, args[0])
            return self._end(run, 0)
        if sub in ("service-remove", "manifest-remove"):
            ref = args[args.index("--name") + 1]
            self.removals.append((sub, ref))
            bits = ref.split("/")
            key = (bits[0], bits[-1])
            if bits[-1] in self.fail_remove:
                return self._end(run, 1)
            self.applied.pop(key, None)
            return self._end(run, 0)
        if sub == "delete":
            self.removals.append(("delete", args[args.index("--name") + 1]))
        return super().runner(run, cmd, capture, summarize)

    @staticmethod
    def _end(run, rc):
        run.exit_code = rc
        run.status = "done" if rc == 0 else "error"
        run.emit({"type": "status", "status": run.status, "exit_code": rc})
        run.close()


def item(kind, ref, **kw):
    return dict({"status": "done", "created": True, "undo": {"kind": kind, "ref": ref}}, **kw)


@pytest.fixture
def undo(art, monkeypatch):
    base = art["capi"]
    fake = FakeUndo(base)
    fake.state = art["state"]
    monkeypatch.setattr(wapp, "_deploy_capi_json", fake.json)
    monkeypatch.setattr(wapp, "_vm_transfer_runner", fake.runner)
    monkeypatch.setattr(wapp, "_registry_cmd", lambda verb: [sys.executable, "/x/harvester-registry.py", verb])
    fake.registry_origin = {"plan": "site", "run": "c0ffee000001"}

    def registry_json(kc, sub, args, timeout=120):
        assert sub == "registry:status", sub
        present = rg.load(art["state"], args[0]) is not None
        return {"registry": args[0], "declared": present, "present": present,
                "origin": dict(fake.registry_origin) if present else {}}, 0, None
    monkeypatch.setattr(wapp, "_deploy_registry_json", registry_json)
    destroys = []

    def tf_launch(cluster, kc, decl_id, mode, **kw):
        destroys.append((decl_id, mode))
        rid = uuid.uuid4().hex[:12]
        run = wapp.ActionRun(rid, f"tf-destroy-decl:{decl_id}", cluster, [])
        with wapp.ACTIONS_LOCK:
            wapp._register_action(run, kw.get("parent"))
        FakeUndo._end(run, 1 if decl_id in fake.fail_remove else 0)
        return rid, "h"
    monkeypatch.setattr(wapp, "_tf_decl_launch", tf_launch)
    # ce que la chaîne a laissé : un cluster enfant, deux artefacts, un registre, une déclaration
    fake.clusters["web"] = {"origin": {"plan": "site", "run": "c0ffee000001"}, "ready": True, "failed": False}
    for it, kind in (("podinfo", "chart"), ("regles", "manifest")):
        fake.applied[("web", it)] = {"type": kind, "cluster": "web", "package": "socle", "version": "2026.11",
                                     "origin": {"plan": "site", "run": "c0ffee000001"}, "ready": True}
    rg.delete(art["state"], "amorcage")
    rg.save(art["state"], "amorcage", {"kind": "bootstrap", "endpoint": "172.16.2.76", "usage": ["images"],
                                       "mirrors": [], "clusters": ["harvlab"], "cluster": "harvlab",
                                       "address": "172.16.2.76", "size_gib": 8, "storage_class": "harv-rep1"})
    decl = wapp.TF_DECLS.create("d1d1d1d1d1d1", "harvlab", "site-cles", description="x",
                                resources=[{"id": "aaaaaaaaaaa1", "kind": "ssh_key", "spec": {"name": "k", "namespace": "default",
                                                                          "public_key": ta.tr.KEY}}],
                                user="alice", origin={"plan": "site", "template": "cles", "template_revision": 1,
                                                      "plan_revision": 1, "run": "c0ffee000001"})
    orig = wapp.ActionRun("c0ffee000001", "deploy:harvlab", "harvlab", [])
    orig.status, orig.exit_code, orig.ended_at = "done", 0, time.time()
    orig.result = {"plan": "site", "cluster": "harvlab", "resumes": None, "dry_run": False, "status": "done",
                   "phases": [
                       {"id": "terraform", "items": [item("tf-declaration", decl["id"], template="cles",
                                                          declaration_id=decl["id"], revision_applied=1)]},
                       {"id": "registry", "items": [item("registry", "harvlab/amorcage", id="reg-amorcage")]},
                       {"id": "clusters", "items": [item("capi-cluster", "web/web", cluster="web")]},
                       {"id": "artefacts", "items": [item("manifest", "web/web/regles", id="art-web-regles"),
                                                     item("chart", "web/web/podinfo", id="art-web-podinfo")]}]}
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[orig.id] = orig
    wapp._actions_persist(orig)
    art.update(undo=fake, orig=orig, decl=decl, destroys=destroys)
    return art


def plan_of(w, **body):
    r = w["client"].post("/api/deploy/harvlab/undo/plan", json=dict({"action_id": w["orig"].id}, **body))
    return r


def launch(w, **body):
    r = w["client"].post("/api/deploy/harvlab/undo", json=dict({"action_id": w["orig"].id, "confirm": "harvlab"},
                                                                **body))
    return r


def finish(r):
    assert r.status_code == 202, r.get_json()
    return trn.wait(r.get_json()["action_id"])


def items_of(run):
    return {i["id"]: i for p in run.result["phases"] for i in p.get("items") or []}


def test_destruction_plan(undo):
    r = plan_of(undo)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert [(i["kind"], i["selected"]) for i in body["items"]] == [
        ("chart", True), ("manifest", True), ("capi-cluster", True), ("registry", False), ("tf-declaration", True)]
    by = {i["kind"]: i for i in body["items"]}
    assert by["manifest"]["details"]["delete"] == ["ConfigMap equipe/conf"]
    assert by["capi-cluster"]["details"]["vms"] == ["web-cp-1", "web-w-1"]
    assert by["registry"]["details"]["volume"] == "amorcage-data" and by["registry"]["note"]
    assert by["chart"]["details"]["present"] is True
    assert {lim["code"] for lim in body["limits"]} == {"volumes-claimed", "lb-dhcp", "ccm-kubernetes",
                                                       "release-namespace"}
    assert undo["undo"].removals == [] and undo["destroys"] == []            # lectures seules


def test_undo_runs_in_reverse_order_and_marks_the_original(undo):
    run = finish(launch(undo))
    assert run.status == "done", run.result
    fake = undo["undo"]
    assert fake.removals == [("service-remove", "web/podinfo"), ("manifest-remove", "web/web/regles"),
                             ("delete", "web/web")]                          # registre décoché : gardé
    assert undo["destroys"] == [(undo["decl"]["id"], "destroy")]
    assert wapp.TF_DECLS.get(undo["decl"]["id"]) is None                     # détruite puis retirée
    assert rg.load(undo["state"], "amorcage") is not None
    assert run.action == "deploy-undo:harvlab" and run.result["undoes"] == undo["orig"].id
    labels = {k.action for k in tc.children(run.id)}
    assert {"capi-service-remove:web/podinfo", "capi-manifest-remove:web/web/regles",
            "capi-cluster-delete:web/web"} <= labels
    orig = undo["orig"].result
    # le registre décoché reste : la chaîne n'est défaite qu'en partie (C1 b)
    assert orig["undone_by"] == run.id and orig["undone_status"] == "partial" and orig["undone_at"]
    conn = sqlite3.connect(str(undo["db"]))
    stored = json.loads(conn.execute("SELECT result FROM actions WHERE id = ?", (undo["orig"].id,)).fetchone()[0])
    conn.close()
    assert stored["undone_by"] == run.id


def test_a_failed_chart_keeps_its_cluster_and_terraform_then_resume(undo):
    fake = undo["undo"]
    fake.fail_remove.add("podinfo")
    run = finish(launch(undo))
    got = items_of(run)
    assert run.result["status"] == "partial"
    assert got["chart-web/web/podinfo"]["status"] == "error"
    assert got["capi-cluster-web/web"]["status"] == "skipped"
    assert got["capi-cluster-web/web"]["message"].startswith("depends on artefacts:chart-web/web/podinfo")
    # revue 4-8 I2 : le cluster resté là garde le contenu Terraform
    assert got["tf-declaration-" + undo["decl"]["id"]]["status"] == "skipped"
    assert undo["destroys"] == [] and wapp.TF_DECLS.get(undo["decl"]["id"]) is not None
    assert "web" in fake.clusters and undo["orig"].result["undone_status"] == "partial"
    # reprise : relancer le défaire sur la même exécution
    fake.fail_remove.clear()
    run2 = finish(launch(undo))
    got2 = items_of(run2)
    assert run2.status == "done", run2.result
    assert got2["manifest-web/web/regles"]["checked"] is True                # parti : revérifié, sauté
    assert got2["capi-cluster-web/web"]["status"] == "done" and "web" not in fake.clusters
    assert got2["tf-declaration-" + undo["decl"]["id"]]["status"] == "done"
    assert wapp.TF_DECLS.get(undo["decl"]["id"]) is None
    assert fake.removals.count(("manifest-remove", "web/web/regles")) == 1
    assert undo["orig"].result["undone_by"] == run2.id


def test_registry_data_needs_its_own_typed_name(undo):
    reg = "registry-harvlab/amorcage"
    r = launch(undo, include=[reg], delete_data=["amorcage"])
    assert r.status_code == 400 and r.get_json()["code"] == "confirm-name"
    run = finish(launch(undo, include=[reg]))
    assert run.status == "done", run.result
    assert "--delete-data" not in undo["undo"].removals[-1][1]
    assert items_of(run)[reg]["data_kept"] is True and rg.load(undo["state"], "amorcage") is None
    # vu en réel : le plan défait désigne son registre, il ne le retient pas
    args = undo["undo"].removals[-1][1]
    assert args[args.index("--ignore-plan") + 1] == "site"
    rg.delete(undo["state"], "amorcage")
    rg.save(undo["state"], "amorcage", {"kind": "bootstrap", "endpoint": "172.16.2.76", "usage": ["images"],
                                        "mirrors": [], "clusters": ["harvlab"], "cluster": "harvlab",
                                        "address": "172.16.2.76", "size_gib": 8, "storage_class": "harv-rep1"})
    for k in ("undone_by", "undone_at", "undone_status"):        # un déroulé neuf (C1 b : un défait ne se refait pas)
        undo["orig"].result.pop(k, None)
    finish(launch(undo, include=[reg], delete_data=["amorcage"], confirm_registry={"amorcage": "amorcage"}))
    assert "--delete-data" in undo["undo"].removals[-1][1]


@pytest.mark.parametrize("body,status,code", [
    ({"confirm": "harv"}, 400, "confirm-name"),
    ({"confirm": None}, 400, "confirm-name"),
    ({"include": ["nope"]}, 400, None),
    ({"include": []}, 400, "undo-empty"),
    ({"delete_data": ["amorcage"]}, 400, None),                              # registre non choisi
    ({"action_id": "zz"}, 400, None),
    ({"action_id": "0123456789ab"}, 404, None),
])
def test_refusals(undo, body, status, code):
    r = launch(undo, **body)
    assert r.status_code == status and (code is None or r.get_json().get("code") == code), r.get_json()
    assert undo["undo"].removals == []


@pytest.mark.parametrize("path", ["/api/deploy/harvlab/undo", "/api/deploy/harvlab/undo/plan"])
@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_only_an_administrator(undo, monkeypatch, path, role):
    monkeypatch.setattr(wapp, "current_role", lambda: role)
    r = undo["client"].post(path, json={"action_id": undo["orig"].id, "confirm": "harvlab"})
    assert r.status_code == 403


def test_not_while_a_deployment_holds_the_cluster(undo):
    fd, _h = wapp._deploy_cluster_lock("harvlab", "beefbeefbeef", "site")
    try:
        r = launch(undo)
        assert r.status_code == 409 and r.get_json()["code"] == "deploy-running"
    finally:
        wapp._flock_release(fd)


def test_a_dry_run_is_not_a_target(undo):
    undo["orig"].dry_run = True
    r = launch(undo)
    assert r.status_code == 400 and r.get_json()["code"] == "undo-dry-run-target"


def test_no_undo_of_the_undo(undo):
    run = finish(launch(undo))
    for path in ("/api/deploy/harvlab/undo", "/api/deploy/harvlab/undo/plan"):
        r = undo["client"].post(path, json={"action_id": run.id, "confirm": "harvlab"})
        assert r.status_code == 400 and r.get_json()["code"] == "undo-of-undo", r.get_json()


def test_a_run_already_fully_undone_is_refused(undo):
    """Revue 4-8 C1 (b) : un déroulé entièrement défait ne se défait plus."""
    run = finish(launch(undo, include=[u["id"] for u in plan_of(undo).get_json()["items"]]))
    assert undo["orig"].result["undone_status"] == "done"
    removals = list(undo["undo"].removals)
    r = launch(undo)
    assert r.status_code == 409 and r.get_json()["code"] == "undo-already-done", r.get_json()
    assert r.get_json()["undone_by"] == run.id
    assert undo["undo"].removals == removals


def test_undo_leaves_what_another_run_recreated(undo):
    """Revue 4-8 C1 (a) : un objet du même nom recréé par une autre exécution
    (ou à la main, sans marque) n'est jamais touché ; il est nommé."""
    fake = undo["undo"]
    fake.clusters["web"]["origin"] = {"plan": "site", "run": "beef00000002"}          # recréé par B
    fake.applied[("web", "podinfo")]["origin"] = {"plan": "site", "run": "beef00000002"}
    fake.applied[("web", "regles")]["origin"] = {}                                    # recréé à la main
    fake.registry_origin = {"plan": "site", "run": "beef00000002"}
    body = plan_of(undo, include=["chart-web/web/podinfo", "manifest-web/web/regles", "capi-cluster-web/web",
                                  "registry-harvlab/amorcage"]).get_json()
    by = {i["kind"]: i for i in body["items"]}
    for kind in ("chart", "manifest", "capi-cluster", "registry"):
        assert by[kind]["details"]["not_ours"] is True, (kind, by[kind]["details"])
    run = finish(launch(undo, include=["chart-web/web/podinfo", "manifest-web/web/regles", "capi-cluster-web/web",
                                       "registry-harvlab/amorcage"]))
    got = items_of(run)
    assert fake.removals == [] and undo["destroys"] == []
    assert got["chart-web/web/podinfo"]["status"] == "error"
    assert got["chart-web/web/podinfo"]["message"].startswith("undo-not-ours")
    assert got["manifest-web/web/regles"]["message"].startswith("undo-not-ours")
    assert got["capi-cluster-web/web"]["status"] in ("error", "skipped")
    assert got["registry-harvlab/amorcage"]["status"] == "skipped"            # I2 : le cluster est resté
    assert "web" in fake.clusters and rg.load(undo["state"], "amorcage") is not None
    assert run.result["status"] != "done"
    run2 = finish(launch(undo, include=["registry-harvlab/amorcage"]))
    assert items_of(run2)["registry-harvlab/amorcage"]["message"].startswith("undo-not-ours")
    assert fake.removals == [] and rg.load(undo["state"], "amorcage") is not None


def test_a_run_of_another_cluster(undo):
    undo["orig"].action = "deploy:autre"
    assert launch(undo).status_code == 400


def test_a_partial_selection_is_not_a_full_undo(undo):
    """Revue 4-8 C1 (b) : seule une exécution dont TOUT est défait est
    refusée ; un défaire d'une partie des éléments la laisse « en partie »."""
    run = finish(launch(undo, include=["chart-web/web/podinfo"]))
    assert run.status == "done", run.result
    assert undo["orig"].result["undone_status"] == "partial"
    run2 = finish(launch(undo))
    assert run2.status == "done", run2.result
    # le registre (décoché par défaut) reste : toujours « en partie »
    assert undo["orig"].result["undone_status"] == "partial"
    run3 = finish(launch(undo, include=["registry-harvlab/amorcage"]))
    assert run3.status == "done" and undo["orig"].result["undone_status"] == "done"
    assert launch(undo).status_code == 409


def test_a_failed_undo_after_a_partial_one_keeps_partial(undo):
    """Vu en réel : un défaire en échec qui suit un défaire en partie
    écrasait « partial » par « error », alors qu'une partie reste défaite."""
    finish(launch(undo, include=["chart-web/web/podinfo"]))
    assert undo["orig"].result["undone_status"] == "partial"
    undo["undo"].fail_remove.add(undo["decl"]["id"])
    undo["undo"].clusters.pop("web")            # I1 : sinon la garde refuse avant la destruction
    run = finish(launch(undo, include=["tf-declaration-" + undo["decl"]["id"]]))
    assert run.result["status"] == "error"
    assert undo["destroys"] == [(undo["decl"]["id"], "destroy")]
    assert undo["orig"].result["undone_status"] == "partial"


TF = "tf-declaration-d1d1d1d1d1d1"


@pytest.mark.parametrize("case", ["unticked", "foreign", "adopted"])
def test_terraform_content_is_kept_while_a_cluster_of_the_chain_stays(undo, case):
    """Revue finale 2.7.0 (I1) : un cluster de la chaîne resté là (décoché,
    recréé par une autre exécution ou repris sans marque) peut employer le
    réseau, la classe de stockage ou l'image d'une déclaration : elle n'est
    pas détruite (`undo-tf-carries-cluster`), et le plan de destruction le dit."""
    fake = undo["undo"]
    include = ["chart-web/web/podinfo", "manifest-web/web/regles", TF]
    if case == "foreign":
        fake.clusters["web"]["origin"] = {"plan": "site", "run": "beef00000002"}
        include = [TF]                              # l'ancien conseil du dépannage : décocher le cluster
    elif case == "adopted":
        fake.clusters["api"] = {"origin": {"plan": "site"}, "ready": True, "failed": False}
        undo["orig"].result["phases"][2]["items"].append(
            {"status": "done", "cluster": "api", "ref": "api/api", "created": False,
             "message": "already present, adopted"})
        include = None
    body = plan_of(undo, **({"include": include} if include else {})).get_json()
    tf = next(i for i in body["items"] if i["id"] == TF)
    expected = ["api/api"] if case == "adopted" else ["web/web"]
    assert tf["details"]["carried_by"] == expected, tf["details"]
    run = finish(launch(undo, **({"include": include} if include else {})))
    got = items_of(run)[TF]
    assert got["status"] == "error" and got["message"].startswith("undo-tf-carries-cluster"), got
    assert expected[0] in got["message"]
    assert undo["destroys"] == [] and wapp.TF_DECLS.get(undo["decl"]["id"]) is not None
    assert undo["orig"].result["undone_status"] in ("partial", "error")


def test_terraform_goes_once_the_cluster_of_the_same_undo_is_gone(undo):
    """Le cluster retiré par le même défaire ne retient plus rien : le plan
    ne le compte pas, le défaire détruit la déclaration après lui."""
    body = plan_of(undo).get_json()
    assert next(i for i in body["items"] if i["id"] == TF)["details"].get("carried_by", []) == []
    run = finish(launch(undo))
    assert items_of(run)[TF]["status"] == "done" and undo["destroys"] == [(undo["decl"]["id"], "destroy")]


def test_each_removal_tool_gets_the_run_it_must_find(undo):
    """Revue finale 2.7.0 (m-b) : chaque outil relit la marque juste avant
    d'écrire ; le Défaire lui passe l'exécution qu'il a jugée « à nous »."""
    run = finish(launch(undo, include=[u["id"] for u in plan_of(undo).get_json()["items"]]))
    assert run.status == "done", run.result
    subs = {sub: args for sub, args in undo["undo"].cmds if sub in ("service-remove", "manifest-remove", "delete")}
    subs["registry"] = next(args for sub, args in undo["undo"].cmds if sub == "delete" and args[0] == "amorcage")
    for sub in ("service-remove", "manifest-remove", "registry"):
        assert subs[sub][subs[sub].index("--expect-run") + 1] == "c0ffee000001", (sub, subs[sub])
    deletes = [args for sub, args in undo["undo"].cmds if sub == "delete" and "--name" in args]
    assert deletes and deletes[0][deletes[0].index("--expect-run") + 1] == "c0ffee000001"


def test_cancelling_an_undo_answers_the_real_flag(undo):
    """Revue finale 2.7.0 (m-c) : un défaire n'a pas de processus ; son
    annulation prend effet (`_cancel`) mais la réponse disait
    `cancel_requested: false`."""
    run = wapp.ActionRun("feed00000270", "deploy-undo:harvlab", "harvlab", [])
    run.status = "running"
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[run.id] = run
    try:
        r = undo["client"].delete(f"/api/action/{run.id}")
        assert r.status_code == 200 and r.get_json()["cancel_requested"] is True, r.get_json()
        assert run._cancel is True and run.status == "running"          # le défaire s'arrête de lui-même
    finally:
        with wapp.ACTIONS_LOCK:
            wapp.ACTIONS.pop(run.id, None)
