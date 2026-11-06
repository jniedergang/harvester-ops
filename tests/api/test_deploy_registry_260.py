"""2.6.0 (tâche 11) : phase Registre d'un plan, entre Terraform et Cluster API
(point 189).

Outils simulés : lectures de `harvester-registry` et `harvester-artefacts`
par `_deploy_registry_json`, actions (deploy, publish) par le relais
`_vm_transfer_runner`, comme `harvester-capi` dans test_deploy_capi_250.py.
Le lancement est le vrai (`_cli_action`, `_artefacts_publish_launch`).

* chaîne Registre, capi, clusters : registre d'amorçage posé puis paquet
  publié AVANT la création du cluster qui en tire (M-order) ;
* registre déjà présent avec cette origine : repris ; de même nom sans elle :
  refusé (M-adopt) ;
* publication en échec : phase partielle, clusters non lancés ;
* reprise : registre et publication revérifiés et sautés ;
* essai sans écriture ; `parent` posé ; `undo` seulement si `created` ;
  aucun identifiant dans le résultat ;
* constats avant lancement (un par code) ; usage des registres et des
  paquets avant suppression (revue m-15).
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import deploy_chain as dc  # noqa: E402
import registries as rg  # noqa: E402
import test_deploy_capi_250 as tc  # noqa: E402
import test_deploy_runner_240 as trn  # noqa: E402

env = tc.env
world = tc.world
capi = tc.capi
PULL_PW = "pull-Pw-31415-secret"


class FakeRegistry:
    """Les outils de registre vus de la chaîne."""

    def __init__(self, state, capi_fake):
        self.state = state
        self.capi = capi_fake
        self.present = {}            # nom -> {"origin", "ready"}
        self.deploy_findings = []
        self.publish_fails = set()
        self.published = set()       # (paquet, version, registre)
        self.order = []
        self.reads, self.launches = [], []
        self.origin_flip = None      # nom : l'origine change après la première lecture
        self.specs = []

    def json(self, kc, sub, args, timeout=120):
        self.reads.append((sub, list(args)))
        if sub == "registry:status":
            name = args[0]
            p = self.present.get(name)
            if p is None:
                return {"present": False, "origin": {}}, 0, None
            if self.origin_flip == name and any(s == "registry:status" and a[0] == name for s, a in self.reads[:-1]):
                p = dict(p, origin={"plan": "other"})
            return {"present": True, "ready": p["ready"], "origin": dict(p["origin"])}, 0, None
        if sub == "registry:deploy":
            blocked = any(f["level"] == "block" for f in self.deploy_findings)
            return {"findings": list(self.deploy_findings), "blocked": blocked}, 2 if blocked else 0, None
        if sub == "registry:check":
            return {"findings": [], "blocked": False}, 0, None
        if sub == "artefacts:publish":
            key = (args[0], args[1], args[args.index("--registry") + 1])
            todo = [{"kind": "image", "name": "ghcr.io/a/b:1", "state": "present" if key in self.published else "new"}]
            return {"findings": [], "blocked": False, "todo": todo}, 0, None
        raise AssertionError(sub)

    def runner(self, run, cmd, capture=False, summarize=None):
        tool = Path(cmd[1]).name
        if tool not in ("harvester-registry.py", "harvester-artefacts.py"):
            self.order.append("capi:" + cmd[2])
            if cmd[2] == "create":
                self.specs.append(json.loads(Path(cmd[cmd.index("--spec") + 1]).read_text()))
            return self.capi.runner(run, cmd, capture, summarize)
        sub = cmd[3]
        args = cmd[4:]
        self.launches.append((tool, sub, args, run))
        self.order.append(f"{tool}:{sub}")
        run.status = "running"
        rc = 0
        if sub == "deploy":
            name = args[0]
            origin = dict(p.split("=") for p in args[args.index("--origin") + 1].split(","))
            self.present[name] = {"origin": origin, "ready": True}
            rg.save(self.state, name, {"kind": "bootstrap", "endpoint": args[args.index("--address") + 1],
                                       "usage": ["images", "charts"], "mirrors": [], "clusters": ["harvlab"],
                                       "cluster": "harvlab", "address": args[args.index("--address") + 1],
                                       "size_gib": int(args[args.index("--size-gib") + 1]),
                                       "storage_class": args[args.index("--storage-class") + 1]})
        elif sub == "publish":
            key = (args[0], args[1], args[args.index("--registry") + 1])
            if key in self.publish_fails:
                rc = 1
            else:
                self.published.add(key)
                run.result = dict(run.result or {}, output={"images": 2, "charts": 1, "pushed_blobs": 5,
                                                            "skipped_blobs": 0, "mounted_blobs": 0, "bytes": 1024})
        run.exit_code = rc
        run.status = "done" if rc == 0 else "error"
        run.emit({"type": "status", "status": run.status, "exit_code": rc})
        run.close()


def package(state, name="socle", version="2026.11", trust="valid", size=1024):
    d = Path(state) / "artefacts.d" / f"{name}-{version}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "artefacts.yaml").write_text(json.dumps({"name": name, "version": version, "items": []}))
    (d / "trust.json").write_text(json.dumps({"state": trust}))
    (d / "blob").write_bytes(b"x" * size)


@pytest.fixture
def reg(capi, monkeypatch):
    fake = FakeRegistry(capi["state"], capi["capi"])
    monkeypatch.setattr(wapp, "_deploy_registry_json", fake.json)
    monkeypatch.setattr(wapp, "_vm_transfer_runner", fake.runner)
    package(capi["state"])
    capi["reg"] = fake
    return capi


def plan(publish=True, registries=None, cluster_regs=("amorcage",), clusters=True):
    phases = {"readiness": {"enabled": False},
              "registry": {"enabled": True, "registries": registries if registries is not None else [
                  {"name": "amorcage", "mode": "bootstrap", "address": "{{registre_ip}}", "size_gib": 8,
                   "storage_class": "harv-rep1", "mirrors": ["ghcr.io"]}],
                  "publish": [{"package": "socle", "version": "2026.11", "registry": "amorcage"}] if publish else []},
              "capi": {"enabled": True}}
    if clusters:
        phases["clusters"] = {"enabled": True, "items": [
            dict({"template": "rke2-petit", "name": "{{cluster}}-apps"},
                 **({"registries": list(cluster_regs)} if cluster_regs else {}))]}
    return {"description": "reg", "variables": ["equipe", "registre_ip"], "phases": phases}


VARS = {"equipe": "ops", "registre_ip": "172.16.2.76"}


def seed(w, body=None):
    tc.seed(w["client"], body or plan())


def start(w, **kw):
    return trn.start(w, variables=VARS, **kw)


def regitems(run):
    return {i["id"]: i for i in tc.phase(run, "registry").get("items") or []}


# --- déroulé -------------------------------------------------------------------

def test_registry_then_publication_then_the_cluster_that_pulls_from_it(reg):
    seed(reg)
    run = start(reg)
    assert run.status == "done", run.result
    assert [p["id"] for p in run.result["phases"]] == ["readiness", "registry", "capi", "clusters"]
    fake = reg["reg"]
    # M-order : le cluster qui tire du registre du plan est créé après lui
    assert fake.order.index("harvester-registry.py:deploy") < fake.order.index("harvester-artefacts.py:publish") \
        < fake.order.index("capi:create")
    it = regitems(run)
    r = it["reg-amorcage"]
    assert r["status"] == "done" and r["created"] is True and r["kind"] == "bootstrap"
    assert r["undo"] == {"kind": "registry", "ref": "harvlab/amorcage"} and r["endpoint"] == "172.16.2.76"
    p = it["pub-socle-2026.11-amorcage"]
    assert p["status"] == "done" and p["images"] == 2 and p["charts"] == 1 and p["pushed_blobs"] == 5
    [(tool, sub, args, child)] = [x for x in fake.launches if x[1] == "deploy"]
    assert args[args.index("--origin") + 1] == f"plan=site,run={run.id}"
    assert args[args.index("--address") + 1] == "172.16.2.76" and child.parent == run.id
    assert child.required_role == "admin"
    pub = [x for x in fake.launches if x[1] == "publish"][0]
    assert pub[3].parent == run.id and pub[2][pub[2].index("--registry") + 1] == "amorcage"
    # la demande du cluster porte le registre du plan
    [spec] = fake.specs
    assert spec["registries"] == [{"registry": "amorcage"}]
    assert ("reg-amorcage", "done") in trn.steps(run) and ("pub-socle-2026.11-amorcage", "done") in trn.steps(run)
    blob = json.dumps(run.result)
    assert "password" not in blob and PULL_PW not in blob and reg["kc"] not in blob


def test_a_registry_of_this_plan_is_adopted(reg):
    fake = reg["reg"]
    rg.save(reg["state"], "amorcage", {"kind": "bootstrap", "endpoint": "172.16.2.76", "usage": ["images", "charts"],
                                       "clusters": ["harvlab"], "cluster": "harvlab", "address": "172.16.2.76",
                                       "size_gib": 8, "storage_class": "harv-rep1"})
    fake.present["amorcage"] = {"origin": {"plan": "site", "run": "aaaaaaaaaaaa"}, "ready": True}
    seed(reg)
    run = start(reg)
    assert run.status == "done", run.result
    r = regitems(run)["reg-amorcage"]
    assert r["message"] == "already present, adopted" and r["created"] is False and "undo" not in r
    assert not [x for x in fake.launches if x[1] == "deploy"]


def test_a_registry_of_another_origin_is_never_adopted(reg):
    fake = reg["reg"]
    rg.save(reg["state"], "amorcage", {"kind": "bootstrap", "endpoint": "172.16.2.76", "usage": ["images", "charts"],
                                       "clusters": ["harvlab"], "cluster": "harvlab", "address": "172.16.2.76",
                                       "size_gib": 8, "storage_class": "harv-rep1"})
    fake.present["amorcage"] = {"origin": {}, "ready": True}
    seed(reg)
    body = reg["client"].post("/api/deploy/harvlab/check", json={"plan": "site", "variables": VARS}).get_json()
    assert "registry-name-taken" in {f["code"] for f in body["findings"]} and body["blocking"]
    # l'origine change entre le contrôle et la phase : refusé par la phase aussi (M-adopt)
    fake.present["amorcage"] = {"origin": {"plan": "site"}, "ready": True}
    fake.origin_flip = "amorcage"
    fake.reads = []
    run = start(reg)
    assert regitems(run)["reg-amorcage"]["message"] == "registry-name-taken"
    assert tc.phase(run, "clusters")["status"] == "skipped"


def test_a_failed_publication_stops_before_the_clusters(reg):
    reg["reg"].publish_fails.add(("socle", "2026.11", "amorcage"))
    seed(reg)
    run = start(reg)
    assert run.status == "error"
    ph = tc.phase(run, "registry")
    assert ph["status"] == "partial" and regitems(run)["reg-amorcage"]["status"] == "done"
    assert regitems(run)["pub-socle-2026.11-amorcage"]["status"] == "error"
    assert tc.phase(run, "clusters")["status"] == "skipped" and reg["capi"].subs("create") == []


def test_resume_rechecks_and_skips_what_is_done(reg):
    fake = reg["reg"]
    fake.publish_fails.add(("socle", "2026.11", "amorcage"))
    seed(reg)
    first = start(reg)
    assert first.status == "error"
    fake.publish_fails.clear()
    run = tc.resume(reg, first)
    assert run.status == "done", run.result
    it = regitems(run)
    assert it["reg-amorcage"].get("checked") is True and it["reg-amorcage"]["created"] is True     # créé avant
    assert it["pub-socle-2026.11-amorcage"]["message"] == "published"
    assert len([x for x in fake.launches if x[1] == "deploy"]) == 1
    # une troisième reprise ne publie plus rien : manifestes présents
    third = tc.resume(reg, run)
    assert regitems(third)["pub-socle-2026.11-amorcage"]["checked"] is True
    assert len([x for x in fake.launches if x[1] == "publish"]) == 2


def test_a_dry_run_writes_nothing(reg):
    seed(reg, plan(clusters=False))
    run = start(reg, dry_run=True)
    assert run.status == "done", run.result
    it = regitems(run)
    assert it["reg-amorcage"]["message"].startswith("checked (dry run)")
    assert it["pub-socle-2026.11-amorcage"]["message"] == "would publish (dry run)"
    assert reg["reg"].launches == [] and rg.load(reg["state"], "amorcage") is None


def test_an_external_registry_is_checked_only(reg):
    rg.save(reg["state"], "client", {"kind": "external", "endpoint": "h.example:443", "usage": ["images"],
                                     "clusters": ["harvlab"]})
    seed(reg, plan(publish=False, registries=[{"name": "client", "mode": "external"}], cluster_regs=("client",)))
    run = start(reg)
    assert run.status == "done", run.result
    r = regitems(run)["reg-client"]
    assert r["message"] == "checked" and r["created"] is False and "undo" not in r
    assert reg["reg"].launches == []


# --- contrôle avant lancement ------------------------------------------------------

def check(w):
    r = w["client"].post("/api/deploy/harvlab/check", json={"plan": "site", "variables": VARS})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


@pytest.mark.parametrize("setup,code,level", [
    ("ok", "publish-later", "info"),
    ("ok", "cluster-registry-later", "info"),
    ("external-unknown", "registry-unknown", "blocking"),
    ("package-missing", "artefact-package-missing", "blocking"),
    ("strict", "package-untrusted-strict", "blocking"),
    ("too-big", "registry-package-too-big", "blocking"),
    ("operator", "registry-admin", "blocking"),
    ("address", "registry-address-answers", "blocking"),
    ("var-missing", "registry-var-missing", "blocking"),
    ("publish-unknown", "registry-unknown", "blocking"),
])
def test_each_registry_finding(reg, monkeypatch, setup, code, level):
    body = plan()
    if setup == "external-unknown":
        body = plan(publish=False, registries=[{"name": "client", "mode": "external"}], cluster_regs=())
    elif setup == "package-missing":
        body["phases"]["registry"]["publish"][0]["version"] = "2026.12"
    elif setup == "strict":
        package(reg["state"], trust="unsigned")
        monkeypatch.setattr(wapp, "_art_settings", lambda: {"strict": True, "locked": False, "signers": []})
    elif setup == "too-big":
        body["phases"]["registry"]["registries"][0]["size_gib"] = 5
        monkeypatch.setattr(wapp, "_deploy_package_size", lambda n, v: 5 << 30)
    elif setup == "address":
        reg["reg"].deploy_findings = [{"code": "registry-address-answers", "level": "block",
                                       "facts": {"address": "172.16.2.76"}}]
    elif setup == "var-missing":
        body["variables"] = ["equipe", "registre_ip", "autre"]
        body["phases"]["registry"]["registries"][0]["storage_class"] = "{{autre}}"
    elif setup == "publish-unknown":
        body["phases"]["registry"]["publish"][0]["registry"] = "absent"
    reg["capi"].fleet.append({"code": "cluster-registry-not-attached", "level": "block",
                              "facts": {"registry": "amorcage", "cluster": "harvlab"},
                              "cluster": "harvlab-apps/harvlab-apps"})
    seed(reg, body)
    if setup == "operator":
        monkeypatch.setattr(wapp, "current_role", lambda: "operator")
    out = check(reg)
    got = {(f["code"], f["level"]) for f in out["findings"] if f["phase"] in ("registry", "clusters")}
    assert (code, level) in got, out["findings"]
    if level == "blocking":
        assert out["blocking"]


def test_the_summary_names_registries_and_packages(reg):
    seed(reg)
    s = check(reg)["summary"]
    assert s["registries"] == [{"name": "amorcage", "mode": "bootstrap", "address": "172.16.2.76", "size_gib": 8,
                                "exists": False}]
    [p] = s["publish"]
    assert (p["package"], p["version"], p["registry"]) == ("socle", "2026.11", "amorcage") and p["size"] > 1024


# --- usage avant suppression (revue m-15) -------------------------------------------

def test_a_registry_named_by_a_plan_cannot_be_forgotten(reg):
    rg.save(reg["state"], "client", {"kind": "external", "endpoint": "h.example:443", "usage": ["images"],
                                     "clusters": []})
    seed(reg, plan(publish=False, registries=[{"name": "client", "mode": "external"}], cluster_regs=()))
    r = reg["client"].delete("/api/registries/client")
    assert r.status_code == 409 and r.get_json()["code"] == "registry-in-use"
    assert "plan:site" in r.get_json()["error"]


def test_a_package_published_by_a_plan_cannot_be_removed(reg):
    seed(reg)
    r = reg["client"].delete("/api/artefacts/socle/2026.11")
    assert r.status_code == 409 and r.get_json()["code"] == "package-in-use"


def test_a_registry_used_by_a_child_cluster_is_not_deleted(reg, monkeypatch):
    import kube
    rg.save(reg["state"], "amorcage", {"kind": "bootstrap", "endpoint": "172.16.2.76", "usage": ["images"],
                                       "clusters": ["harvlab"], "cluster": "harvlab", "address": "172.16.2.76",
                                       "size_gib": 8, "storage_class": "harv-rep1"})
    monkeypatch.setattr(kube.Kube, "list", lambda self, kind, ns=None, selector=None: [
        {"metadata": {"namespace": "web", "labels": {rg.LABEL: "amorcage"}}}] if kind == "secrets" else [])
    r = reg["client"].delete("/api/registries/bootstrap/harvlab/amorcage", json={})
    assert r.status_code == 409 and r.get_json()["used_by"] == ["cluster:harvlab/web"]
    monkeypatch.setattr(kube.Kube, "list", lambda self, kind, ns=None, selector=None: (_ for _ in ()).throw(
        kube.KubeError("Forbidden")))
    r = reg["client"].delete("/api/registries/bootstrap/harvlab/amorcage", json={})
    assert r.status_code == 409 and r.get_json()["code"] == "registry-usage-unknown"


def test_the_order_puts_registry_before_cluster_api():
    assert dc.ORDER.index("terraform") < dc.ORDER.index("registry") < dc.ORDER.index("capi")
