"""2.8.0 (tâche 5) : phase GitOps d'un plan, import des clusters dans le
Rancher de la session par la phase Clusters, et leur « Défaire ».

Outil `harvester-capi` simulé (lectures `gitops-status`, `gitops-check`,
`state`, `rancher-status` ; gestes `gitops-deploy`, `gitops-remove`,
`rancher-import`, `rancher-unimport`), comme test_deploy_artefacts_270 ; le
lancement est le vrai (`_capi_launch`, `_cli_action`).

* chaîne Clusters, Artefacts (chart flux), GitOps : l'entrée attend le
  chart de Flux de son cluster ; origine, rôle, personne passés ; `created`
  et `undo` pour ce qui porte CETTE exécution ;
* secret Git du plan saisi au lancement : jusqu'à l'outil, jamais dans le
  résultat ni actions.db, effacé à la fin ; redemandé à la reprise ;
* constats : secret absent, Flux absent, Flux du plan (information),
  Fleet réservé à l'administrateur, entrée d'une autre origine ;
* reprise : entrée faite revérifiée par `gitops-status` puis sautée ;
* phase Clusters : import marqué `undo_more`, import fait par la reprise
  d'un cluster repris ;
* Défaire : GitOps, chart, import, cluster, dans cet ordre.

Mutations jouées (rapport des tâches 5 à 8) : « dépendance au chart flux
ignorée », « secret Git rangé dans le résultat », « import retiré après le
cluster ».
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import test_deploy_artefacts_270 as ta  # noqa: E402
import test_deploy_capi_250 as tc  # noqa: E402
import test_deploy_runner_240 as trn  # noqa: E402

env = tc.env
world = tc.world
capi = tc.capi
art = ta.art
SSH_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nGITKEY-1618\n-----END OPENSSH PRIVATE KEY-----\n"
OLD_RUN = "aaaaaaaaaaaa"


class FakeGit(ta.FakeArt):
    def __init__(self):
        super().__init__()
        self.entries = {}             # (ns, c, entrée) -> {tool, origin, state, name}
        self.imports = {}             # nom du cluster -> exécution écrite dans Rancher
        self.gfail = set()
        self.gspecs = []
        self.gcheck = []
        self.removals = []
        self.session = True           # sans session Rancher, les entrées Fleet ne se lisent pas

    def json(self, kc, sub, args, spec=None, timeout=180):
        if sub == "gitops-status":
            ns, c = args[args.index("--name") + 1].split("/")
            self.reads.append((sub, f"{ns}/{c}"))
            return {"entries": [dict(v, namespace=k[0], cluster=k[1], entry=k[2])
                                for k, v in self.entries.items() if k[:2] == (ns, c)
                                and (self.session or v.get("tool") != "fleet")],
                    "session": self.session}, 0, None
        if sub == "gitops-check":
            self.reads.append((sub, spec))
            c = spec["cluster"].split("/")[1]
            tool = "fleet" if c in self.imports else "flux"
            blocked = any(f["level"] == "block" for f in self.gcheck)
            return {"blocked": blocked, "tool": tool, "findings": list(self.gcheck),
                    "entry": spec["entry"]["name"]}, 2 if blocked else 0, None
        if sub == "state":
            out, rc, err = super().json(kc, sub, args, spec, timeout)
            name = args[args.index("--name") + 1].split("/")[0]
            if out.get("present"):
                out["rancher"] = ({"server": "rancher.home.zypp.fr", "name": f"harvlab-{name}", "cid": "c-m-abc123"}
                                  if name in self.imports else None)
            return out, rc, err
        if sub == "rancher-status":
            name = args[args.index("--name") + 1].split("/")[1]
            row = {"namespace": name, "cluster": name, "imported": name in self.imports}
            if name in self.imports:
                row.update(present=True, origin={"run": self.imports[name]})
            return {"session": "rancher.home.zypp.fr", "clusters": [row]}, 0, None
        if sub == "delete" and "--dry-run" in args:
            ref = args[args.index("--name") + 1]
            return {"cluster": ref, "present": ref.split("/")[1] in self.clusters, "machines": 2}, 0, None
        if sub == "rancher-unimport" and "--dry-run" in args:
            return {"rancher": "rancher.home.zypp.fr", "present": True, "delete": [], "kept": []}, 0, None
        return super().json(kc, sub, args, spec, timeout)

    def runner(self, run, cmd, capture=False, summarize=None):
        sub, args = cmd[2], cmd[3:]
        if sub == "gitops-deploy":
            spec = json.loads(Path(args[args.index("--spec") + 1]).read_text())
            self.gspecs.append(spec)
            ns, c = spec["cluster"].split("/")
            rc = 1 if spec["entry"]["name"] in self.gfail else 0
            if rc == 0:
                self.entries[(ns, c, spec["entry"]["name"])] = {
                    "tool": "fleet" if c in self.imports else "flux", "origin": dict(spec["origin"]),
                    "state": {"applied": True, "ready": True}, "name": spec["entry"]["name"]}
            return self._end(run, rc)
        if sub == "rancher-import":
            name = args[args.index("--name") + 1].split("/")[1]
            self.imports[name] = args[args.index("--run") + 1]
            return self._end(run, 0)
        if sub in ("gitops-remove", "rancher-unimport"):
            ref = args[args.index("--name") + 1]
            self.removals.append((sub, ref))
            bits = ref.split("/")
            if sub == "gitops-remove":
                self.entries.pop(tuple(bits), None)
            else:
                self.imports.pop(bits[1], None)
            return self._end(run, 0)
        if sub in ("service-remove", "manifest-remove"):
            ref = args[args.index("--name") + 1]
            self.removals.append((sub, ref))
            bits = ref.split("/")
            self.applied.pop((bits[0], bits[-1]), None)
            return self._end(run, 0)
        if sub == "delete":
            self.removals.append((sub, args[args.index("--name") + 1]))
        if sub == "create":
            spec = json.loads(Path(args[args.index("--spec") + 1]).read_text())
            origin = wapp._cc.parse_origin(args[args.index("--origin") + 1]) if "--origin" in args else {}
            # l'import est écrit AVANT la fin de l'action fille, comme le vrai
            # `create` (point 223) : posé après, le fil du déroulé qui relit
            # l'état dès la fin le manquait parfois (revue finale 2.10.0 m-9)
            if spec.get("rancher_external") and "--dry-run" not in args \
                    and self.outcome.get(spec["name"], "done") == "done":
                self.imports[spec["name"]] = origin.get("run")
            ta.FakeArt.runner(self, run, cmd, capture, summarize)
            return None
        return super().runner(run, cmd, capture, summarize)

    @staticmethod
    def _end(run, rc):
        run.exit_code = rc
        run.status = "done" if rc == 0 else "error"
        run.emit({"type": "status", "status": run.status, "exit_code": rc})
        run.close()


ITEMS = [{"name": "flux", "kind": "chart", "chart": "charts/flux2-2.19.1.tgz"},
         {"name": "apps", "kind": "gitops", "files": ["manifests/apps/a.yaml"]},
         {"name": "regles", "kind": "manifest", "files": ["manifests/regles/a.yaml"]}]
FLUX = {"package": "socle", "version": "2026.11", "item": "flux", "registry": "amorcage",
        "clusters": ["{{cluster}}-apps"]}
OCI = {"type": "oci", "package": "socle", "version": "2026.11", "item": "apps", "registry": "amorcage"}
GIT = {"type": "git", "url": "ssh://git@172.16.1.11:2222/x/app.git", "secret": "depot"}


def entry(**over):
    e = {"name": "apps", "clusters": ["{{cluster}}-apps"], "target_namespace": "equipe-a", "source": dict(OCI)}
    e.update(over)
    return e


def plan(entries=None, flux=True, names=("{{cluster}}-apps",)):
    p = tc.plan(names=names)
    p["phases"]["artefacts"] = {"enabled": True, "timeout_minutes": 5, "items": [FLUX] if flux else [],
                                "policies": []}
    p["phases"]["gitops"] = {"enabled": True, "timeout_minutes": 5, "entries": entries or [entry()]}
    return p


@pytest.fixture
def git(art, monkeypatch):
    fake = FakeGit()
    monkeypatch.setattr(wapp, "_deploy_capi_json", fake.json)
    monkeypatch.setattr(wapp, "_vm_transfer_runner", fake.runner)
    ta.package(art["state"], items=ITEMS)
    art["capi"] = fake
    return art


def seed(w, body, rancher_external=False):
    t = tc.template()
    if rancher_external:
        t["spec"]["rancher_external"] = True
    tc.seed(w["client"], body, tmpl=t)


def gitops_items(run):
    return {i["id"]: i for i in tc.phase(run, "gitops").get("items") or []}


def launch(w, **body):
    return w["client"].post("/api/deploy/harvlab", json=dict({"plan": "site", "variables": {"equipe": "ops"}}, **body))


def test_chain_clusters_flux_then_gitops(git):
    fake = git["capi"]
    seed(git, plan())
    run = trn.start(git)
    assert run.status == "done", run.result
    assert [p["id"] for p in run.result["phases"]] == ["readiness", "capi", "clusters", "artefacts", "gitops"]
    assert fake.deploys() == [("service-deploy", "flux", "harvlab-apps/harvlab-apps")]
    [spec] = fake.gspecs
    assert spec["cluster"] == "harvlab-apps/harvlab-apps" and spec["entry"]["namespace"] == "equipe-a"
    assert spec["entry"]["source"] == OCI and spec["origin"] == {"plan": "site", "run": run.id}
    assert spec["role"] == "admin" and spec["by"] == "alice" and spec["secrets"] == {}
    it = gitops_items(run)["gitops-harvlab-apps-apps"]
    assert it["status"] == "done" and it["created"] is True and it["tool"] == "flux"
    assert it["undo"] == {"kind": "gitops", "ref": "harvlab-apps/harvlab-apps/apps", "tool": "flux"}
    labels = {k.action for k in tc.children(run.id)}
    assert "capi-gitops:harvlab-apps/harvlab-apps:apps" in labels


def test_the_entry_waits_for_the_flux_chart_of_its_cluster(git):
    """Mutation « dépendance au chart flux ignorée » : le chart en échec,
    l'entrée est sautée et rien n'est posé."""
    fake = git["capi"]
    fake.fail.add("flux")
    seed(git, plan())
    run = trn.start(git)
    it = gitops_items(run)["gitops-harvlab-apps-apps"]
    assert it["status"] == "skipped" and it["message"] == "depends on artefacts:art-harvlab-apps-flux, not done"
    assert fake.gspecs == []


def test_a_git_secret_reaches_the_tool_and_never_the_result(git):
    """Mutation « secret Git rangé dans le résultat »."""
    fake = git["capi"]
    seed(git, plan([entry(source=dict(GIT))]))
    r = launch(git, gitops_secrets={"depot": {"identity": SSH_KEY, "known_hosts": "h ssh-ed25519 AAAA"}})
    assert r.status_code == 202, r.get_json()
    run = trn.wait(r.get_json()["action_id"])
    assert run.status == "done", run.result
    assert fake.gspecs[0]["secrets"] == {"depot": {"identity": SSH_KEY, "known_hosts": "h ssh-ed25519 AAAA"}}
    assert "GITKEY-1618" not in json.dumps(run.result) and "GITKEY-1618" not in json.dumps(
        [e for e in run.events])
    conn = sqlite3.connect(str(git["db"]))
    rows = conn.execute("SELECT * FROM actions").fetchall()
    conn.close()
    assert rows and "GITKEY-1618" not in json.dumps(rows, default=str)
    assert wapp._sst.store().refs("plan-run", run.id) == []            # effacé à la fin


@pytest.mark.parametrize("reads_with_value,where", [
    (0, "before"),          # m-4 : le contrôle de la phase lit le magasin, pas la liste du plan
    (1, "item"),            # m-3 : la valeur disparue entre le contrôle et l'élément
])
def test_a_git_secret_missing_from_the_run_store_is_never_sent_empty(git, monkeypatch, reads_with_value, where):
    """Revue 5-8 m-3, m-4 : un secret absent du magasin du déroulé n'est
    jamais envoyé vide (Flux retirerait son Secret, Fleet écrirait un compte
    vide sur un compte qui marche)."""
    fake = git["capi"]
    seed(git, plan([entry(source=dict(GIT))]))
    real = wapp._deploy_gitops_secret_get
    count = {"n": 0}

    def get(store, run_id, name):
        count["n"] += 1
        return real(store, run_id, name) if count["n"] <= reads_with_value else {}
    monkeypatch.setattr(wapp, "_deploy_gitops_secret_get", get)
    r = launch(git, gitops_secrets={"depot": {"identity": SSH_KEY}})
    assert r.status_code == 202, r.get_json()
    run = trn.wait(r.get_json()["action_id"])
    assert run.status == "error" and fake.gspecs == []
    ph = tc.phase(run, "gitops")
    text = json.dumps(ph)
    assert "gitops-secret-missing" in text
    if where == "item":
        assert gitops_items(run)["gitops-harvlab-apps-apps"]["status"] == "error"


@pytest.mark.parametrize("body,code", [
    ({}, "gitops-secret-missing"),
    ({"gitops_secrets": {"autre": {"username": "u", "password": "p"}}}, "gitops-secret-missing"),
])
def test_a_missing_git_secret_blocks_the_launch(git, body, code):
    seed(git, plan([entry(source=dict(GIT))]))
    r = launch(git, **body)
    assert r.status_code == 400 and code in tc.found(r.get_json())
    assert git["capi"].gspecs == []


@pytest.mark.parametrize("raw", [{"depot": "key"}, {"depot": {"identity": SSH_KEY, "token": "x"}},
                                 {"Depot": {"identity": SSH_KEY}}, {"depot": {"username": "u"}}])
def test_malformed_git_secrets_are_refused(git, raw):
    seed(git, plan([entry(source=dict(GIT))]))
    r = launch(git, gitops_secrets=raw)
    assert r.status_code == 400 and r.get_json()["fields"] == ["gitops_secrets"]


def test_flux_absent_from_the_plan_and_the_cluster_blocks(git):
    seed(git, plan(flux=False))
    r = launch(git)
    assert r.status_code == 400 and "gitops-flux-missing" in tc.found(r.get_json())


def test_flux_of_the_plan_is_said_before_launch(git):
    seed(git, plan())
    body = tc.check(git["client"])
    f = tc.found(body)
    assert f["gitops-flux-in-plan"]["level"] == "info" and not body["blocking"]
    assert body["summary"]["gitops"] == [{"id": "gitops-harvlab-apps-apps", "ref": "harvlab-apps/harvlab-apps",
                                          "entry": "apps", "tool": "flux", "source": "oci"}]


def test_an_existing_cluster_with_flux_is_checked_by_the_tool(git):
    fake = git["capi"]
    fake.clusters["outils"] = {"origin": {}, "ready": True, "failed": False}
    seed(git, plan([entry(clusters=["outils/outils"])], flux=False))
    run = trn.start(git)
    assert run.status == "done", run.result
    assert ("gitops-check", fake.reads[[r[0] for r in fake.reads].index("gitops-check")][1]) in fake.reads
    assert fake.gspecs[0]["cluster"] == "outils/outils"
    fake.gcheck = [{"code": "gitops-flux-missing", "level": "block", "facts": {"service": "flux"}}]
    r = launch(git)
    assert r.status_code == 400 and "gitops-flux-missing" in tc.found(r.get_json())


def test_an_entry_of_another_origin_blocks(git):
    fake = git["capi"]
    fake.clusters["outils"] = {"origin": {}, "ready": True, "failed": False}
    fake.entries[("outils", "outils", "apps")] = {"tool": "flux", "origin": {"plan": "autre"}, "state": {}}
    seed(git, plan([entry(clusters=["outils/outils"])], flux=False))
    r = launch(git)
    assert r.status_code == 400 and "gitops-taken" in tc.found(r.get_json())


def test_a_fleet_entry_of_another_plan_blocks(git):
    """Revue 5-8 I-11 : une entrée Fleet posée par un AUTRE plan (une
    exécution, mais pas celle de ce plan) n'est jamais reprise."""
    fake = git["capi"]
    fake.clusters["outils"] = {"origin": {}, "ready": True, "failed": False}
    fake.entries[("outils", "outils", "apps")] = {"tool": "fleet", "origin": {"plan": "autre", "run": OLD_RUN},
                                                  "state": {}}
    seed(git, plan([entry(clusters=["outils/outils"])], flux=False))
    r = launch(git)
    assert r.status_code == 400 and "gitops-taken" in tc.found(r.get_json())


@pytest.mark.parametrize("origin,ours", [
    ({"plan": "site", "run": OLD_RUN}, True),
    ({"plan": "autre", "run": OLD_RUN}, False),
    ({"run": OLD_RUN}, False),                       # Fleet d'avant la marque du plan
    ({}, False),
])
@pytest.mark.parametrize("tool", ["flux", "fleet"])
def test_gitops_ours_compares_the_plan(origin, ours, tool):
    assert wapp._deploy_gitops_ours({"tool": tool, "origin": origin}, "site") is ours


def test_an_entry_of_an_earlier_run_is_adopted_without_mark(git):
    fake = git["capi"]
    fake.clusters["outils"] = {"origin": {}, "ready": True, "failed": False}
    fake.entries[("outils", "outils", "apps")] = {"tool": "flux", "origin": {"plan": "site", "run": OLD_RUN},
                                                  "state": {"applied": True}}
    seed(git, plan([entry(clusters=["outils/outils"])], flux=False))
    run = trn.start(git)
    it = gitops_items(run)["gitops-outils-apps"]
    assert it["message"] == "already present, updated" and it["created"] is False and "undo" not in it
    assert fake.gspecs[0]["origin"] == {"plan": "site", "run": OLD_RUN}


def test_resume_checks_done_entries_and_replays_failed_ones(git):
    fake = git["capi"]
    fake.gfail.add("infra")
    seed(git, plan([entry(), entry(name="infra", source=dict(OCI))]))
    run = trn.start(git)
    got = gitops_items(run)
    assert got["gitops-harvlab-apps-apps"]["status"] == "done" and got["gitops-harvlab-apps-infra"]["status"] == "error"
    fake.gfail.clear()
    run2 = tc.resume(git, run)
    got2 = gitops_items(run2)
    assert got2["gitops-harvlab-apps-apps"]["checked"] is True and got2["gitops-harvlab-apps-apps"]["created"] is True
    assert got2["gitops-harvlab-apps-infra"]["status"] == "done" and got2["gitops-harvlab-apps-infra"]["created"]
    assert [s["entry"]["name"] for s in fake.gspecs] == ["apps", "infra", "infra"]


def test_the_git_secret_is_asked_again_on_resume(git):
    fake = git["capi"]
    fake.gfail.add("apps")
    seed(git, plan([entry(source=dict(GIT))]))
    r = launch(git, gitops_secrets={"depot": {"identity": SSH_KEY}})
    run = trn.wait(r.get_json()["action_id"])
    fake.gfail.clear()
    r = git["client"].post("/api/deploy/harvlab/resume", json={"action_id": run.id})
    assert r.status_code == 400 and "gitops-secret-missing" in tc.found(r.get_json())
    r = git["client"].post("/api/deploy/harvlab/resume", json={"action_id": run.id,
                                                                "gitops_secrets": {"depot": {"identity": SSH_KEY}}})
    assert r.status_code == 202
    trn.wait(r.get_json()["action_id"])
    assert fake.gspecs[-1]["secrets"]["depot"]["identity"] == SSH_KEY


# --- Fleet et import ----------------------------------------------------------

def test_a_cluster_with_rancher_external_is_imported_and_marked(git):
    fake = git["capi"]
    seed(git, plan([entry(source={"type": "git", "url": "https://git.client/x.git"})], flux=False),
         rancher_external=True)
    run = trn.start(git)
    assert run.status == "done", run.result
    it = tc.items(run)["harvlab-apps"]
    assert it["undo_more"] == [{"kind": "rancher-import", "ref": "harvlab-apps/harvlab-apps"}]
    assert fake.imports["harvlab-apps"] == run.id
    assert gitops_items(run)["gitops-harvlab-apps-apps"]["tool"] == "fleet"


def test_fleet_is_reserved_to_the_administrator(git, monkeypatch):
    seed(git, plan([entry(source={"type": "git", "url": "https://git.client/x.git"})], flux=False),
         rancher_external=True)
    monkeypatch.setattr(wapp, "current_role", lambda: "operator")
    r = launch(git)
    assert r.status_code == 400 and "gitops-fleet-admin-only" in tc.found(r.get_json())


def test_an_adopted_cluster_without_import_is_imported_by_the_resume(git):
    fake = git["capi"]
    fake.clusters["harvlab-apps"] = {"origin": {"plan": "site", "template": "rke2-petit", "run": OLD_RUN},
                                     "ready": True, "failed": False}
    p = plan(flux=False)
    p["phases"].pop("gitops")
    p["phases"].pop("artefacts")
    seed(git, p, rancher_external=True)
    run = trn.start(git)
    it = tc.items(run)["harvlab-apps"]
    assert it["status"] == "done" and it["message"] == "already present, adopted; imported into Rancher"
    assert it["created"] is False and it["undo_more"] == [{"kind": "rancher-import", "ref": "harvlab-apps/harvlab-apps"}]
    assert fake.imports["harvlab-apps"] == run.id


# --- Défaire ------------------------------------------------------------------

def test_undo_removes_gitops_then_chart_then_import_then_cluster(git):
    """Mutation « import retiré après le cluster »."""
    fake = git["capi"]
    seed(git, plan([entry(source={"type": "git", "url": "https://git.client/x.git"})]), rancher_external=True)
    run = trn.start(git)
    assert run.status == "done", run.result
    r = git["client"].post("/api/deploy/harvlab/undo/plan", json={"action_id": run.id})
    assert [(i["kind"], i["selected"]) for i in r.get_json()["items"]] == [
        ("gitops", True), ("chart", True), ("rancher-import", True), ("capi-cluster", True)]
    r = git["client"].post("/api/deploy/harvlab/undo", json={"action_id": run.id, "confirm": "harvlab"})
    assert r.status_code == 202, r.get_json()
    u = trn.wait(r.get_json()["action_id"])
    assert u.status == "done", u.result
    # revue 5-8 m-1 : le cluster part dans le même défaire, sa suppression
    # retire l'import (rien lu dans le cluster enfant) : pas d'étape à part
    assert [x[0] for x in fake.removals] == ["gitops-remove", "service-remove", "delete"]
    assert fake.removals[0] == ("gitops-remove", "harvlab-apps/harvlab-apps/apps")
    got = {i["id"]: i for p in u.result["phases"] for i in p.get("items") or []}
    assert got["rancher-import-harvlab-apps/harvlab-apps"]["message"] == "left to the removal of its cluster"
    labels = {k.action for k in tc.children(u.id)}
    assert "capi-gitops-remove:harvlab-apps/harvlab-apps:apps" in labels
    assert not [x for x in labels if x.startswith("capi-rancher-unimport:")]


def test_undo_of_an_import_alone_still_unimports(git):
    """Revue 5-8 m-1 : sans son cluster dans le défaire, l'import part par
    son propre geste."""
    fake = git["capi"]
    seed(git, plan([entry(source={"type": "git", "url": "https://git.client/x.git"})]), rancher_external=True)
    run = trn.start(git)
    r = git["client"].post("/api/deploy/harvlab/undo", json={"action_id": run.id, "confirm": "harvlab",
                                                             "include": ["rancher-import-harvlab-apps/harvlab-apps"]})
    assert r.status_code == 202, r.get_json()
    u = trn.wait(r.get_json()["action_id"])
    assert ("rancher-unimport", "harvlab-apps/harvlab-apps") in fake.removals, u.result


def test_undo_never_touches_an_entry_recreated_by_another_run(git):
    fake = git["capi"]
    seed(git, plan())
    run = trn.start(git)
    fake.entries[("harvlab-apps", "harvlab-apps", "apps")]["origin"] = {"plan": "site", "run": OLD_RUN}
    r = git["client"].post("/api/deploy/harvlab/undo", json={"action_id": run.id, "confirm": "harvlab"})
    u = trn.wait(r.get_json()["action_id"])
    got = {i["id"]: i for p in u.result["phases"] for i in p.get("items") or []}
    assert got["gitops-harvlab-apps/harvlab-apps/apps"]["status"] == "error"
    assert "undo-not-ours" in got["gitops-harvlab-apps/harvlab-apps/apps"]["message"]
    assert ("gitops-remove", "harvlab-apps/harvlab-apps/apps") not in fake.removals
    # le chart de Flux et le cluster restent : ils attendaient l'entrée
    assert got["chart-harvlab-apps/harvlab-apps/flux"]["status"] == "skipped"


@pytest.mark.parametrize("tool,removed", [("fleet", False), ("flux", True)])
def test_undo_without_a_rancher_session(git, tool, removed):
    """Revue 5-8 I-8 : sans session Rancher, une entrée Fleet ne se voit pas ;
    le Défaire dit « non retirée, session Rancher nécessaire », jamais
    « déjà partie ». Une entrée Flux se juge sans session."""
    fake = git["capi"]
    if tool == "fleet":
        seed(git, plan([entry(source={"type": "git", "url": "https://git.client/x.git"})], flux=False),
             rancher_external=True)
    else:
        seed(git, plan())
    run = trn.start(git)
    assert run.status == "done", run.result
    assert gitops_items(run)["gitops-harvlab-apps-apps"]["undo"]["tool"] == tool
    fake.session = False
    r = git["client"].post("/api/deploy/harvlab/undo", json={"action_id": run.id, "confirm": "harvlab",
                                                             "include": ["gitops-harvlab-apps/harvlab-apps/apps"]})
    assert r.status_code == 202, r.get_json()
    u = trn.wait(r.get_json()["action_id"])
    got = {i["id"]: i for p in u.result["phases"] for i in p.get("items") or []}
    item = got["gitops-harvlab-apps/harvlab-apps/apps"]
    if removed:
        assert item["status"] == "done" and ("gitops-remove", "harvlab-apps/harvlab-apps/apps") in fake.removals
    else:
        assert item["status"] == "error" and "rancher-session-needed" in item["message"]
        assert ("gitops-remove", "harvlab-apps/harvlab-apps/apps") not in fake.removals
        assert u.status != "done"


def test_the_command_line_reads_the_git_secrets_from_the_secrets_file(tmp_path):
    """Parité : `harvester-plans apply --secrets-file` (clé gitops_secrets)."""
    import web_loader
    f = tmp_path / "s.yaml"
    f.write_text("gitops_secrets:\n  depot: {username: u, password: p}\n")
    f.chmod(0o600)
    got, err = wapp._deploy_gitops_secrets(web_loader.read_secrets(str(f)).get("gitops_secrets"))
    assert err is None and got == {"depot": {"username": "u", "password": "p"}}


@pytest.mark.parametrize("external,tool,codes", [
    (True, "auto", {("clusters", "rancher-session-in-chain"), ("gitops", "rancher-session-in-chain")}),
    (True, "fleet", {("clusters", "rancher-session-in-chain"), ("gitops", "rancher-session-in-chain")}),
    (False, "auto", set()),
])
def test_a_chain_that_installs_the_cluster_refuses_rancher_at_launch(git, external, tool, codes):
    """Revue finale 2.8.0 I-3 : une chaîne qui installe le cluster Harvester
    n'a pas de session Rancher ; l'import externe et Fleet sont refusés au
    lancement, pas des heures plus tard."""
    seed(git, plan([entry(tool=tool)]), rancher_external=external)
    with wapp.app.test_request_context():
        res = wapp._deploy_install_precheck("site", {"equipe": "ops"}, "harvlab", "172.16.2.70")
    found = set()
    if res is not None:
        body, status = res
        assert status == 400
        found = {(f["phase"], f["code"]) for f in body.get_json()["findings"]
                 if f["code"] == "rancher-session-in-chain"}
        assert all(f["level"] == "blocking" for f in body.get_json()["findings"]
                   if f["code"] == "rancher-session-in-chain")
    assert found == codes


@pytest.mark.parametrize("run_id,origin_run,expect", [
    (None, None, {"plan": "site"}),                       # contrôle d'avant lancement
    ("0123456789ab", None, {"plan": "site", "run": "0123456789ab"}),
    ("0123456789ab", "ba9876543210", {"plan": "site", "run": "ba9876543210"}),
])
def test_the_gitops_request_origin_is_accepted_by_the_tool(run_id, origin_run, expect):
    """Banc 2.8.0 : le contrôle d'un plan sur un cluster enfant existant
    envoyait `run: None`, que l'outil refuse ; toutes les entrées étaient
    bloquées (`gitops-check-failed`)."""
    row = {"ref": "c28/c28", "entry": {"name": "apps"}}
    spec = wapp._deploy_gitops_spec(row, {"name": "site"}, run_id, "", "admin", {}, origin_run=origin_run)
    assert spec["origin"] == expect
    text = ",".join(f"{k}={v}" for k, v in spec["origin"].items())
    assert wapp._cc.parse_origin(text) == expect
