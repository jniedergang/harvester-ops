"""2.8.0 (tâche 4) : `harvester-capi gitops-check`, `gitops-deploy`,
`gitops-remove`, `gitops-status`. Flux : objets par le CRS de l'entrée
(`art-gitops-<nom>`), identifiants par sa ressource Secret ; Fleet : objet
écrit dans le Rancher de la session."""

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_services as sv  # noqa: E402
import capi_stack as cs  # noqa: E402
import child_gitops as cg  # noqa: E402
import rancher_import as ri  # noqa: E402
import registries as rg  # noqa: E402

NS, CL, H = "rke2-banc", "rke2-banc", "harvlab2"
PULL_PW = "PULL-PASSWORD-31415"
SSH_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nKEYDATA-2718\n-----END OPENSSH PRIVATE KEY-----\n"
SERVER = "rancher.home.zypp.fr"
FLEET_NAME = f"hops-{H}-{CL}-demo"


@pytest.fixture(scope="module")
def cli():
    s = importlib.util.spec_from_file_location("harvester_capi_gitops", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


def capi_cluster(imported=False):
    ann = {ri.A_MARK: f"{SERVER}/{H}-{CL}/c-m-abcdef12"} if imported else {}
    return {"metadata": {"namespace": NS, "name": CL, "labels": {}, "annotations": ann}}


def flux_hcp(locked=True):
    values = cg.flux_values() if locked else {"multitenancy": {"enabled": False}}
    import yaml
    return {"metadata": {"name": "flux", "namespace": NS}, "spec": {"valuesTemplate": yaml.safe_dump(values)}}


FLUX_RELEASE = {"metadata": {"namespace": NS, "ownerReferences": [{"kind": "HelmChartProxy", "name": "flux"}]},
                "spec": {"clusterRef": {"name": CL}},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


class Mgmt:
    def __init__(self, objs, releases=()):
        self.objs = dict(objs)
        self.releases = list(releases)
        self.kubeconfig = "/nonexistent"

    def get(self, kind, ns, name):
        return self.objs.get((kind, ns, name))

    def list(self, kind, ns=None, selector=None):
        if kind == sv.K_HRP:
            return self.releases
        return [o for (k, s, _), o in self.objs.items() if k == kind and (ns is None or s == ns)]


class Rancher:
    def __init__(self, existing=None):
        self.server = SERVER
        self.objs = dict(existing or {})
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        name = path.split("?")[0].rsplit("/", 1)[-1]
        if method == "GET" and "?labelSelector=" in path:
            plural = path.split("?")[0].rsplit("/", 1)[-1]
            return 200, {"items": [o for k, o in self.objs.items() if k[0] == plural]}
        plural = path.split("/")[-2] if method != "POST" else path.rsplit("/", 1)[-1]
        if method == "GET":
            o = self.objs.get((plural, name))
            return (200, o) if o else (404, {})
        if method in ("POST", "PUT"):
            o = json.loads(json.dumps(body))
            if plural in ("gitrepos", "helmops"):
                o["status"] = {"summary": {"desiredReady": 1, "ready": 1}}
            self.objs[(plural, o["metadata"]["name"])] = o
            return 201, o
        if method == "DELETE":
            self.objs.pop((plural, name), None)
            return 200, {}
        return 404, {}


@pytest.fixture
def world(tmp_path, monkeypatch, cli):
    state = tmp_path / "state"
    (state / "registries.d").mkdir(parents=True)
    pkg = state / "artefacts.d" / "banc-280-1.0.0"
    pkg.mkdir(parents=True)
    (pkg / "artefacts.yaml").write_text(json.dumps({"name": "banc-280", "version": "1.0.0", "items": [
        {"name": "demo", "kind": "gitops", "files": ["manifests/demo/a.yaml"]},
        {"name": "regles", "kind": "manifest", "files": ["manifests/regles/a.yaml"]}]}))
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_state", lambda: state)
    decl = {"name": "amorcage", "endpoint": "172.16.2.76:5000", "ca_pem": "CA-PEM", "usage": ["images", "charts"],
            "published": [{"package": "banc-280", "version": "1.0.0"}], "clusters": [H]}
    monkeypatch.setattr(cli.rg, "load", lambda st, n: decl if n == "amorcage" else None)
    monkeypatch.setattr(cli.rg, "read_creds", lambda k, n, role: {"username": "pull", "password": PULL_PW})
    box = {"session": {"url": f"https://{SERVER}", "cid": "c-sg2q6", "token": "t"}, "delivered": []}

    def run(mgmt, argv, spec=None, rancher=None, child_ready=True):
        if spec is not None:
            p = tmp_path / "spec.json"
            p.write_text(json.dumps(spec))
            argv = argv + ["--spec", str(p)]
        monkeypatch.setattr(cli, "kube_from", lambda a: (mgmt, "/x", None))
        monkeypatch.setattr(cli, "_session_of", lambda kc: box["session"])
        monkeypatch.setattr(cli, "RANCHER", lambda r: rancher)

        @contextlib.contextmanager
        def session(k, ns, c):
            yield "/nonexistent"
        monkeypatch.setattr(cli.ca, "session", session)
        monkeypatch.setattr(cli, "Kube", lambda path, **kw: box["child"])
        monkeypatch.setattr(cli, "deliver", lambda *a, **k: box["delivered"].append((a, k)) or 0)
        return cli.main(argv)
    return {"run": run, "box": box, "tmp": tmp_path}


class Child:
    API = """serviceaccounts sa v1 true ServiceAccount
secrets  v1 true Secret
namespaces ns v1 false Namespace
rolebindings  rbac.authorization.k8s.io/v1 true RoleBinding
clusterrolebindings  rbac.authorization.k8s.io/v1 false ClusterRoleBinding
ocirepositories ocirepo source.toolkit.fluxcd.io/v1 true OCIRepository
gitrepositories gitrepo source.toolkit.fluxcd.io/v1 true GitRepository
kustomizations ks kustomize.toolkit.fluxcd.io/v1 true Kustomization
"""

    def __init__(self, team=True):
        self.team, self.applied = team, []
        ok = {"status": {"conditions": [{"type": "Ready", "status": "True"}], "lastAppliedRevision": "1.0.0@sha256:a"}}
        self.flux = {"ocirepositories.source.toolkit.fluxcd.io": ok, "gitrepositories.source.toolkit.fluxcd.io": ok,
                     "kustomizations.kustomize.toolkit.fluxcd.io": ok}

    def api_resources(self, namespaced=None, verbs=None):
        return self.API

    def get(self, res, ns, name):
        if res == "Namespace":
            return {"metadata": {"name": name}} if self.team else None
        return self.flux.get(res)

    def apply(self, docs, **kw):
        self.applied.append(docs)


def entry(**over):
    e = {"name": "demo", "namespace": "equipe-a",
         "source": {"type": "oci", "package": "banc-280", "version": "1.0.0", "item": "demo", "registry": "amorcage"}}
    e.update(over)
    return e


def req(**over):
    r = {"cluster": f"{NS}/{CL}", "entry": entry(), "role": "operator", "by": "alice"}
    r.update(over)
    return r


def flux_world(locked=True, ready=True):
    return Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster(), (sv.K_HCP, NS, "flux"): flux_hcp(locked)},
                [FLUX_RELEASE] if ready else [])


@pytest.mark.parametrize("mgmt,over,code", [
    (lambda: Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster()}), {}, "gitops-flux-missing"),
    (lambda: flux_world(locked=False), {}, "gitops-flux-not-locked"),
    (lambda: flux_world(), {"entry": entry(tool="fleet")}, "gitops-fleet-needs-rancher"),
    (lambda: flux_world(), {"entry": entry(privileges="cluster")}, "gitops-privileges-admin-only"),
    (lambda: flux_world(), {"entry": entry(namespace="kube-system")}, "gitops-namespace-not-team"),
    (lambda: flux_world(), {"entry": entry(source={"type": "git", "url": "https://h/r.git", "secret": "s"})},
     "gitops-secret-missing"),
    (lambda: flux_world(), {"entry": entry(source=dict(entry()["source"], version="9.9.9"))},
     "gitops-package-not-published"),
    (lambda: Mgmt({}), {}, "gitops-cluster-missing"),
    # revue 1-4 (m-6) : l'élément d'un paquet publié doit être de sorte gitops
    (lambda: flux_world(), {"entry": entry(source=dict(entry()["source"], item="regles"))},
     "gitops-package-not-published"),
    (lambda: Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster(imported=True)}), {}, "gitops-source-unsupported"),
])
def test_check_findings(cli, world, capsys, mgmt, over, code):
    assert world["run"](mgmt(), ["gitops-check", "--kubeconfig", "/x"], req(**over)) == 2
    assert code in [f["code"] for f in json.loads(capsys.readouterr().out)["findings"]]


def test_a_fleet_entry_needs_a_rancher_session(cli, world, capsys):
    world["box"]["session"] = None
    mgmt = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster(imported=True)})
    g = entry(source={"type": "git", "url": "https://h/r.git"})
    assert world["run"](mgmt, ["gitops-check", "--kubeconfig", "/x"], req(entry=g)) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["tool"] == "fleet" and [f["code"] for f in out["findings"]] == ["gitops-fleet-admin-only",
                                                                                 "rancher-session-needed"]


def test_flux_deploy_delivers_objects_and_credentials_apart(cli, world, capsys):
    world["box"]["child"] = Child()
    assert world["run"](flux_world(), ["gitops-deploy", "--kubeconfig", "/x"], req()) == 0
    (args, kw), = world["box"]["delivered"]
    _kube, ns, c, item, pkg, ver, objs, origin, by, _t = args
    assert (ns, c, item, pkg, ver, by) == (NS, CL, "gitops-demo", "banc-280", "1.0.0", "alice")
    assert PULL_PW not in json.dumps(objs) and PULL_PW in json.dumps(kw["secrets"])
    src = next(o for o in objs if o["kind"] == "OCIRepository")
    assert src["spec"]["url"] == "oci://172.16.2.76:5000/harvester-ops/gitops/banc-280/demo"
    ns_doc = world["box"]["child"].applied[0][0]
    assert ns_doc["metadata"] == {"name": "hops-gitops", "labels": {"harvester-ops.io/managed": "true"}}
    err = capsys.readouterr().err
    assert PULL_PW not in err and "applied revision 1.0.0@sha256:a" in err


def test_flux_deploy_refuses_a_missing_team_namespace(cli, world, capsys):
    world["box"]["child"] = Child(team=False)
    assert world["run"](flux_world(), ["gitops-deploy", "--kubeconfig", "/x"], req()) == 2
    assert "gitops-namespace-missing" in capsys.readouterr().err and world["box"]["delivered"] == []


def test_flux_git_deploy_takes_the_secret_of_the_plan(cli, world, capsys):
    world["box"]["child"] = Child()
    g = entry(source={"type": "git", "url": "ssh://git@172.16.1.11:2222/x/app.git", "secret": "depot"})
    spec = req(entry=g, secrets={"depot": {"identity": SSH_KEY, "known_hosts": "h k"}})
    assert world["run"](flux_world(), ["gitops-deploy", "--kubeconfig", "/x"], spec) == 0
    (args, kw), = world["box"]["delivered"]
    assert args[4:6] == ("harvester-ops", "git")
    assert "KEYDATA-2718" in json.dumps(kw["secrets"]) and "KEYDATA-2718" not in json.dumps(args[6])
    assert "KEYDATA-2718" not in capsys.readouterr().err


def test_fleet_deploy_writes_a_gitrepo_with_its_secret(cli, world, capsys):
    mgmt = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster(imported=True)})
    r = Rancher()
    g = entry(source={"type": "git", "url": "ssh://git@172.16.1.11:2222/x/app.git", "secret": "depot"})
    spec = req(entry=g, secrets={"depot": {"identity": SSH_KEY, "known_hosts": "h k"}}, role="admin")
    assert world["run"](mgmt, ["gitops-deploy", "--kubeconfig", "/x", "--harvester-name", H], spec, rancher=r) == 0
    repo = r.objs[("gitrepos", FLEET_NAME)]
    assert repo["spec"]["targets"] == [{"clusterName": f"{H}-{CL}"}]
    assert repo["spec"]["clientSecretName"] == f"{FLEET_NAME}-auth"
    sec = r.objs[("secrets", f"{FLEET_NAME}-auth")]
    assert sec["type"] == "kubernetes.io/ssh-auth" and sec["stringData"]["ssh-privatekey"] == SSH_KEY
    assert "KEYDATA-2718" not in capsys.readouterr().err


def test_fleet_deploy_refuses_a_foreign_object(cli, world, capsys):
    mgmt = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster(imported=True)})
    r = Rancher({("gitrepos", FLEET_NAME): {"metadata": {"name": FLEET_NAME, "labels": {}}}})
    g = entry(source={"type": "git", "url": "https://h/r.git"})
    assert world["run"](mgmt, ["gitops-deploy", "--kubeconfig", "/x", "--harvester-name", H],
                        req(entry=g, role="admin"), rancher=r) == 2
    assert "gitops-name-taken" in capsys.readouterr().err
    assert not [c for c in r.calls if c[0] in ("POST", "PUT")]


def test_remove_of_a_flux_entry_is_the_removal_of_its_item(cli, world, monkeypatch):
    import capi_manifests as mf
    mgmt = Mgmt({(mf.K_CRS, NS, "art-gitops-demo"): {"metadata": {}}})
    got = []
    monkeypatch.setattr(cli, "cmd_manifest_remove", lambda a: got.append(a.name) or 0)
    assert world["run"](mgmt, ["gitops-remove", "--kubeconfig", "/x", "--name", f"{NS}/{CL}/demo"]) == 0
    assert got == [f"{NS}/{CL}/gitops-demo"]


@pytest.mark.parametrize("expect,code", [("", 0), ("ffffffffffff", 2)])
def test_remove_of_a_fleet_entry_rereads_its_marks(cli, world, capsys, expect, code):
    e = cg.normalize(entry(source={"type": "git", "url": "https://h/r.git"}))
    obj = cg.fleet_object(e, "f", H, NS, CL, run="0123456789ab")
    r = Rancher({("gitrepos", obj["metadata"]["name"]): obj})
    argv = ["gitops-remove", "--kubeconfig", "/x", "--harvester-name", H, "--name", f"{NS}/{CL}/demo"]
    if expect:
        argv += ["--expect-run", expect]
    assert world["run"](Mgmt({}), argv, rancher=r) == code
    assert (("gitrepos", obj["metadata"]["name"]) in r.objs) is bool(code)


def test_status_lists_flux_and_fleet_entries(cli, world, capsys):
    import capi_manifests as mf
    crs = {"metadata": {"name": "art-gitops-demo", "namespace": NS,
                        "labels": {mf.L_ITEM: "gitops-demo", mf.L_CLUSTER: CL}}}
    other = {"metadata": {"name": "art-essai", "namespace": NS, "labels": {mf.L_ITEM: "essai", mf.L_CLUSTER: CL}}}
    mgmt = Mgmt({(mf.K_CRS, NS, "art-gitops-demo"): crs, (mf.K_CRS, NS, "art-essai"): other})
    e = cg.normalize(entry(name="web", source={"type": "git", "url": "https://h/r.git"}))
    fleet = cg.fleet_object(e, "f", H, NS, CL)
    fleet["status"] = {"summary": {"desiredReady": 1, "ready": 1}}
    r = Rancher({("gitrepos", fleet["metadata"]["name"]): fleet})
    assert world["run"](mgmt, ["gitops-status", "--kubeconfig", "/x", "--harvester-name", H, "--json"],
                        rancher=r) == 0
    out = json.loads(capsys.readouterr().out)
    assert [(x["tool"], x["entry"]) for x in out["entries"]] == [("flux", "demo"), ("fleet", "web")]
    assert out["entries"][1]["state"]["ready"] is True


def test_fleet_deploy_writes_no_secret_over_a_foreign_one(cli, world, capsys):
    """Revue 1-4 (I-5) : un Secret du même nom d'un autre usage n'est jamais
    écrasé ; rien n'est écrit dans Rancher."""
    mgmt = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster(imported=True)})
    r = Rancher({("secrets", f"{FLEET_NAME}-auth"): {"metadata": {"name": f"{FLEET_NAME}-auth",
                                                                  "labels": {"app": "x"}}}})
    g = entry(source={"type": "git", "url": "ssh://git@h:2222/x/app.git", "secret": "depot"})
    spec = req(entry=g, secrets={"depot": {"identity": SSH_KEY, "known_hosts": "h k"}}, role="admin")
    assert world["run"](mgmt, ["gitops-deploy", "--kubeconfig", "/x", "--harvester-name", H], spec, rancher=r) == 2
    assert not [c for c in r.calls if c[0] in ("POST", "PUT", "DELETE")]
    assert r.objs[("secrets", f"{FLEET_NAME}-auth")]["metadata"]["labels"] == {"app": "x"}
    assert "gitops-name-taken" in capsys.readouterr().err


def test_fleet_deploy_checks_the_object_before_writing_its_secret(cli, world, capsys):
    """Revue 1-4 (I-5) : l'objet d'un autre cluster ou d'une autre origine
    arrête le geste avant l'écriture du Secret (clé SSH)."""
    mgmt = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster(imported=True)})
    r = Rancher({("gitrepos", FLEET_NAME): {"metadata": {"name": FLEET_NAME, "labels": {}}}})
    g = entry(source={"type": "git", "url": "ssh://git@h:2222/x/app.git", "secret": "depot"})
    spec = req(entry=g, secrets={"depot": {"identity": SSH_KEY, "known_hosts": "h k"}}, role="admin")
    assert world["run"](mgmt, ["gitops-deploy", "--kubeconfig", "/x", "--harvester-name", H], spec, rancher=r) == 2
    assert not [c for c in r.calls if c[0] in ("POST", "PUT")]
    assert ("secrets", f"{FLEET_NAME}-auth") not in r.objs and "KEYDATA" not in json.dumps(list(r.objs.values()))
