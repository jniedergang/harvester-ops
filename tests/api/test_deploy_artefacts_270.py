"""2.7.0 (tâche 4) : phase Artefacts d'un plan dans la console.

Outil `harvester-capi` simulé (lectures `services`, `manifests`,
`manifest-check`, `manifest-verify` ; actions `service-deploy` et
`manifest-deploy` par le relais), comme test_deploy_capi_250.py ; le
lancement est le vrai (`_capi_launch`, `_cli_action`).

* chaîne Clusters puis Artefacts : socle, manifeste, chart, dans cet ordre ;
  origine et personne passées ; `created` et `undo` seulement pour ce qui
  porte CETTE exécution ;
* élément repris d'une exécution précédente du plan : son exécution
  d'origine gardée, pas de `created` ; d'une autre origine : refusé ;
* un cluster en échec dans la phase Clusters : ses éléments sautés, les
  autres vont à leur terme ;
* reprise : élément fait revérifié (`manifest-verify`, pas seulement
  l'empreinte) puis sauté, élément en échec rejoué ;
* contrôle au début de la phase (chart dépublié depuis) : rien d'écrit ;
* essai sans écriture ; constats avant lancement ; marques Terraform.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import registries as rg  # noqa: E402
import test_deploy_capi_250 as tc  # noqa: E402
import test_deploy_routes_240 as tr  # noqa: E402
import test_deploy_runner_240 as trn  # noqa: E402

env = tc.env
world = tc.world
capi = tc.capi
OLD_RUN = "aaaaaaaaaaaa"


class FakeArt(tc.FakeCapi):
    def __init__(self):
        super().__init__()
        self.applied = {}             # (ns, élément) -> {type, cluster, package, version, origin, ready}
        self.fail = set()             # éléments dont l'application échoue
        self.verify = {}              # élément -> ok
        self.check_findings = []
        self.caaph = True
        self.specs = []
        self.on_create = None

    def json(self, kc, sub, args, spec=None, timeout=180):
        if sub == "services":
            self.reads.append((sub, list(args)))
            return {"caaph": self.caaph, "services": [
                {"namespace": ns, "name": it, "ready": a["ready"], "current": True,
                 "releases": [{"ready": a["ready"], "current": True}],
                 "artefact": {"item": it, "package": a["package"], "version": a["version"],
                              "cluster": a["cluster"], "origin": dict(a["origin"])}}
                for (ns, it), a in self.applied.items() if a["type"] == "chart"]}, 0, None
        if sub == "manifests":
            self.reads.append((sub, list(args)))
            return [{"namespace": ns, "item": it, "cluster": a["cluster"], "package": a["package"],
                     "version": a["version"], "origin": dict(a["origin"]), "state": {"applied": a["ready"]}}
                    for (ns, it), a in self.applied.items() if a["type"] == "manifest"], 0, None
        if sub == "manifest-check":
            self.reads.append((sub, spec))
            name = spec["cluster"].split("/")[1]
            c = self.clusters.get(name)
            if c is not None and not c.get("ready"):
                # revue 4-8 I1 : cluster en échec, son kubeconfig ne s'ouvre pas
                return {"blocked": True, "findings": [{"code": "child-kubeconfig-missing", "level": "block",
                                                        "facts": {}}]}, 2, None
            blocked = any(f["level"] == "block" for f in self.check_findings)
            return {"blocked": blocked, "findings": list(self.check_findings)}, 2 if blocked else 0, None
        if sub == "manifest-verify":
            item = args[args.index("--name") + 1].split("/")[2]
            self.reads.append((sub, item))
            return {"ok": self.verify.get(item, True)}, 0, None
        return super().json(kc, sub, args, spec, timeout)

    def runner(self, run, cmd, capture=False, summarize=None):
        sub = cmd[2]
        if sub not in ("service-deploy", "manifest-deploy"):
            if sub == "create" and self.on_create:
                self.on_create()
            return super().runner(run, cmd, capture, summarize)
        args = cmd[3:]
        spec = json.loads(Path(args[args.index("--spec") + 1]).read_text())
        self.launches.append((sub, args, run))
        self.specs.append((sub, spec))
        ns, child = spec["cluster"].split("/")
        item = spec.get("name") or spec.get("item") or "hops-policies"
        src = spec.get("source") or spec
        rc = 1 if item in self.fail else 0
        if rc == 0:
            self.applied[(ns, item)] = {"type": "chart" if sub == "service-deploy" else "manifest",
                                        "cluster": child, "package": src.get("package", "harvester-ops"),
                                        "version": src.get("version", "builtin"), "origin": dict(spec["origin"]),
                                        "ready": True}
        run.exit_code = rc
        run.status = "done" if rc == 0 else "error"
        run.emit({"type": "status", "status": run.status, "exit_code": rc})
        run.close()

    def deploys(self):
        return [(s, (sp.get("name") or sp.get("item") or "hops-policies"), sp["cluster"]) for s, sp in self.specs]


def package(state, items=None, trust="valid"):
    d = Path(state) / "artefacts.d" / "socle-2026.11"
    d.mkdir(parents=True, exist_ok=True)
    (d / "artefacts.yaml").write_text(json.dumps({"name": "socle", "version": "2026.11", "items": items or [
        {"name": "podinfo", "kind": "chart", "chart": "charts/podinfo-6.9.2.tgz"},
        {"name": "regles", "kind": "manifest", "files": ["manifests/regles/a.yaml"]},
        {"name": "net", "kind": "policy", "files": ["manifests/net/a.yaml"], "images": ["ghcr.io/x/y:1"]},
        {"name": "imgs", "kind": "images", "images": ["ghcr.io/x/y:1"]}]}))
    (d / "trust.json").write_text(json.dumps({"state": trust}))


def declare(state, published=True, clusters=("harvlab",)):
    rg.delete(state, "amorcage")
    rg.save(state, "amorcage", {"kind": "external", "endpoint": "172.16.2.76", "usage": ["images", "charts"],
                                "mirrors": [], "clusters": list(clusters),
                                "published": [{"package": "socle", "version": "2026.11"}] if published else []})


@pytest.fixture
def art(capi, monkeypatch):
    fake = FakeArt()
    monkeypatch.setattr(wapp, "_deploy_capi_json", fake.json)
    monkeypatch.setattr(wapp, "_vm_transfer_runner", fake.runner)
    capi["admin"] = {"ok": True}
    def admin(kc, ns, c):
        cl = fake.clusters.get(c)
        if cl is not None and not cl.get("ready"):
            return None, "no answer (cluster not ready)"          # revue 4-8 I1
        return (capi["admin"]["ok"], "") if capi["admin"]["ok"] is not None else (None, "no answer")
    monkeypatch.setattr(wapp, "_child_admin_allowed", admin)
    package(capi["state"])
    declare(capi["state"])
    capi["capi"] = fake
    return capi


CHART = {"package": "socle", "version": "2026.11", "item": "podinfo", "registry": "amorcage",
         "clusters": ["{{cluster}}-apps"]}
MAN = {"package": "socle", "version": "2026.11", "item": "regles", "clusters": ["{{cluster}}-apps"]}
POL = {"clusters": ["{{cluster}}-apps"], "base": True, "teams": [{"namespace": "equipe-a"}],
       "accounts": [{"namespace": "equipe-a", "name": "ci", "role": "edit"}]}


def plan(items=(CHART, MAN), policies=(POL,), names=("{{cluster}}-apps",)):
    p = tc.plan(names=names)
    p["phases"]["artefacts"] = {"enabled": True, "timeout_minutes": 5, "items": list(items),
                                "policies": list(policies)}
    return p


def arts(run):
    return {i["id"]: i for i in tc.phase(run, "artefacts").get("items") or []}


def test_chain_policies_then_manifests_then_charts(art):
    w, fake = art, art["capi"]
    tc.seed(w["client"], plan())
    run = trn.start(w)
    assert run.status == "done", run.result
    assert [p["id"] for p in run.result["phases"]] == ["readiness", "capi", "clusters", "artefacts"]
    # point 211 : socle, puis manifestes, puis charts, quel que soit l'ordre des entrées
    assert fake.deploys() == [("manifest-deploy", "hops-policies", "harvlab-apps/harvlab-apps"),
                              ("manifest-deploy", "regles", "harvlab-apps/harvlab-apps"),
                              ("service-deploy", "podinfo", "harvlab-apps/harvlab-apps")]
    got = arts(run)
    assert list(got) == ["pol-harvlab-apps", "art-harvlab-apps-regles", "art-harvlab-apps-podinfo"]
    assert got["pol-harvlab-apps"]["undo"] == {"kind": "policies", "ref": "harvlab-apps/harvlab-apps/hops-policies"}
    assert got["art-harvlab-apps-regles"]["undo"] == {"kind": "manifest", "ref": "harvlab-apps/harvlab-apps/regles"}
    assert got["art-harvlab-apps-podinfo"]["undo"] == {"kind": "chart", "ref": "harvlab-apps/harvlab-apps/podinfo"}
    assert all(i["created"] is True and i["status"] == "done" for i in got.values())
    for _sub, spec in fake.specs:
        assert spec["origin"] == {"plan": "site", "run": run.id} and spec["by"] == "alice"
    chart = fake.specs[2][1]
    assert chart["source"] == {"registry": "amorcage", "package": "socle", "version": "2026.11", "item": "podinfo"}
    timeouts = [a[a.index("--timeout") + 1] for _s, a, _r in fake.launches if _s.endswith("-deploy")]
    assert timeouts == ["300", "300", "300"]
    labels = {k.action for k in tc.children(run.id)}
    assert {"capi-policies:harvlab-apps/harvlab-apps", "capi-manifest-deploy:harvlab-apps/harvlab-apps:regles",
            "capi-service-deploy:harvlab-apps/harvlab-apps:podinfo"} <= labels
    assert ("art-harvlab-apps-podinfo", "done") in trn.steps(run)


def test_an_item_of_an_earlier_run_of_the_plan_is_adopted_without_mark(art):
    fake = art["capi"]
    fake.applied[("outils", "regles")] = {"type": "manifest", "cluster": "outils", "package": "socle",
                                          "version": "2026.11", "origin": {"plan": "site", "run": OLD_RUN},
                                          "ready": True}
    tc.seed(art["client"], plan(items=(dict(MAN, clusters=["outils/outils"]),), policies=()))
    run = trn.start(art)
    it = arts(run)["art-outils-regles"]
    assert it["status"] == "done" and it["message"] == "already present, updated"
    assert it["created"] is False and "undo" not in it
    assert fake.specs[0][1]["origin"] == {"plan": "site", "run": OLD_RUN}      # créateur gardé


@pytest.mark.parametrize("origin,cluster", [({"plan": "autre"}, "outils"), ({}, "outils"),
                                            ({"plan": "site"}, "autre")])
def test_an_item_of_another_origin_blocks_the_launch(art, origin, cluster):
    art["capi"].applied[("outils", "regles")] = {"type": "manifest", "cluster": cluster, "package": "socle",
                                                 "version": "2026.11", "origin": origin, "ready": True}
    tc.seed(art["client"], plan(items=(dict(MAN, clusters=["outils/outils"]),), policies=()))
    r = art["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops"}})
    assert r.status_code == 400 and "artefact-taken" in tc.found(r.get_json())
    assert art["capi"].specs == []


def test_a_failed_cluster_skips_its_items_only(art):
    """Revue 4-8 I1 : les rangées d'un cluster en échec ne passent plus par
    le `before` (leur kubeconfig ne s'ouvre pas), les autres vont au bout."""
    fake = art["capi"]
    fake.outcome["harvlab-b"] = "error"
    tc.seed(art["client"], plan(items=(dict(MAN, clusters=["{{cluster}}-a", "{{cluster}}-b"]),), policies=(),
                                names=("{{cluster}}-a", "{{cluster}}-b")))
    run = trn.start(art)
    assert run.result["status"] == "partial" and run.result["failed_phase"] == "clusters"
    got = arts(run)
    assert got["art-harvlab-a-regles"]["status"] == "done"
    assert got["art-harvlab-b-regles"]["status"] == "skipped"
    assert got["art-harvlab-b-regles"]["message"] == "depends on clusters:harvlab-b, not done"
    assert tc.phase(run, "artefacts")["status"] == "partial"
    assert fake.deploys() == [("manifest-deploy", "regles", "harvlab-a/harvlab-a")]


def test_resume_checks_done_items_and_replays_failed_ones(art):
    fake = art["capi"]
    fake.fail.add("podinfo")
    tc.seed(art["client"], plan())
    run = trn.start(art)
    assert {k: v["status"] for k, v in arts(run).items()} == {
        "pol-harvlab-apps": "done", "art-harvlab-apps-regles": "done", "art-harvlab-apps-podinfo": "error"}
    fake.fail.clear()
    fake.verify["regles"] = False            # un objet supprimé à la main : l'empreinte ne suffit pas
    run2 = tc.resume(art, run)
    got = arts(run2)
    assert got["pol-harvlab-apps"]["checked"] is True and got["pol-harvlab-apps"]["created"] is True
    assert got["art-harvlab-apps-regles"].get("checked") is None and got["art-harvlab-apps-regles"]["status"] == "done"
    assert got["art-harvlab-apps-podinfo"]["status"] == "done" and got["art-harvlab-apps-podinfo"]["created"] is True
    assert ("manifest-verify", "regles") in fake.reads
    # rejoué par la reprise : l'origine reste celle de l'exécution qui l'a
    # créé, et la marque est recopiée du maillon précédent
    regles = [sp for s, sp in fake.specs if sp.get("item") == "regles"]
    assert regles[-1]["origin"]["run"] == run.id and got["art-harvlab-apps-regles"]["created"] is True
    assert got["art-harvlab-apps-regles"]["undo"] == arts(run)["art-harvlab-apps-regles"]["undo"]


def test_a_chart_unpublished_since_the_check_stops_the_phase_before_any_write(art):
    fake = art["capi"]
    fake.on_create = lambda: declare(art["state"], published=False)
    tc.seed(art["client"], plan())
    run = trn.start(art)
    ph = tc.phase(run, "artefacts")
    assert ph["status"] == "error" and "artefact-not-published" in ph["message"]
    assert fake.specs == []


@pytest.mark.parametrize("setup,code,level", [
    (lambda a: declare(a["state"], published=False), "artefact-not-published", "blocking"),
    (lambda a: declare(a["state"], clusters=("harv4",)), "artefact-not-published", "blocking"),
    (lambda a: setattr(a["capi"], "caaph", False), "caaph-missing", "blocking"),
    (lambda a: package(a["state"], trust="unsigned"), "package-untrusted-strict", "blocking"),
    (lambda a: setattr(a["capi"], "check_findings", [{"code": "manifest-kind-refused", "level": "block",
                                                      "facts": {"kind": "CRD"}}]), "manifest-kind-refused",
     "blocking"),
])
def test_each_check_code(art, monkeypatch, setup, code, level):
    monkeypatch.setattr(wapp, "_art_settings", lambda: {"strict": True})
    setup(art)
    tc.seed(art["client"], plan(items=(CHART, dict(MAN, clusters=["outils/outils"]))))
    got = tc.found(tc.check(art["client"]))
    assert got[code]["level"] == level and got[code]["phase"] == "artefacts"


@pytest.mark.parametrize("answer,code", [(False, "child-admin-required"), (None, "artefact-check-failed")])
def test_the_child_admin_right_is_checked_for_existing_clusters(art, answer, code):
    art["admin"]["ok"] = answer
    tc.seed(art["client"], plan(items=(dict(MAN, clusters=["outils/outils"]),), policies=()))
    assert tc.found(tc.check(art["client"]))[code]["level"] == "blocking"


def test_clusters_made_by_the_plan_are_checked_later(art):
    art["admin"]["ok"] = False                  # jamais demandé pour un cluster à venir
    tc.seed(art["client"], plan(items=(MAN,), policies=()))
    body = tc.check(art["client"])
    assert not body["blocking"], body["findings"]
    assert body["summary"]["artefacts"] == [{"id": "art-harvlab-apps-regles", "type": "manifest",
                                             "ref": "harvlab-apps/harvlab-apps", "item": "regles",
                                             "package": "socle", "version": "2026.11", "registry": None}]


def test_images_not_published_warn_or_block_without_fallback(art, monkeypatch):
    declare(art["state"], published=False)
    item = {"package": "socle", "version": "2026.11", "item": "net", "clusters": ["outils/outils"]}
    tc.seed(art["client"], plan(items=(item,), policies=()))
    assert tc.found(tc.check(art["client"]))["artefact-images-not-published"]["level"] == "warning"


def test_dry_run_writes_nothing(art):
    tc.seed(art["client"], plan())
    run = trn.start(art, dry_run=True)
    assert run.status == "done", run.result
    assert art["capi"].specs == []
    assert all(i["message"].startswith("checked (dry run)") for i in arts(run).values())


def test_kind_refused_at_launch_when_the_package_arrived_after_saving(art):
    tc.seed(art["client"], plan(items=(dict(MAN, clusters=["outils/outils"]),), policies=()))
    package(art["state"], items=[{"name": "regles", "kind": "images", "images": ["a:1"]}])
    got = tc.found(tc.check(art["client"]))
    assert "published by the Registry phase" in got["artefact-invalid"]["detail"]


# --- marques Terraform (prérequis du Défaire) ----------------------------------
def test_terraform_declaration_made_by_the_run_is_marked_and_copied_on_resume(world):
    trn.two_templates(world)
    run = trn.start(world)
    items = {i["template"]: i for i in tc.phase(run, "terraform")["items"]}
    assert items["cles"]["created"] is True and items["cles"]["undo"]["kind"] == "tf-declaration"
    assert items["reseau"]["status"] == "error" and items["reseau"]["created"] is True
    world["fake"].fail.clear()
    r = world["client"].post("/api/deploy/harvlab/resume", json={"action_id": run.id})
    run2 = trn.wait(r.get_json()["action_id"])
    items2 = {i["template"]: i for i in tc.phase(run2, "terraform")["items"]}
    assert items2["reseau"]["status"] == "done" and items2["reseau"]["created"] is True
    assert items2["reseau"]["undo"] == items["reseau"]["undo"]
    decl = wapp.TF_DECLS.get(items2["reseau"]["undo"]["ref"])
    assert decl["origin"]["run"] == run.id                                   # l'exécution qui l'a créée


def test_dry_run_declarations_carry_no_mark(world):
    tr.seed(world["client"])
    run = trn.start(world, dry_run=True)
    item = tc.phase(run, "terraform")["items"][0]
    assert "created" not in item and "undo" not in item
