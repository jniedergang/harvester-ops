"""2.8.0 (tâche 2) : `harvester-capi rancher-import`, `rancher-unimport`,
`rancher-status`, et leur enchaînement par `create` et `delete`.

Un faux Rancher (API v1 et norman) répond comme Rancher v2.14.1 sur la
maison (plan 2.8.0, « Essais préalables (tâche 1) », points 1 et 2) :
`status.clusterName` posé, jeton d'enregistrement substitué, manifeste à une
image, retrait en quelques secondes. Le jeton n'apparaît nulle part.
"""

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_cluster as cc  # noqa: E402
import capi_stack as cs  # noqa: E402
import rancher_import as ri  # noqa: E402
from kube import KubeError  # noqa: E402

NS, CL, H = "rke2-banc", "rke2-banc", "harvlab2"
NAME = "harvlab2-rke2-banc"
CID = "c-m-nwsjn9p6"
TOKEN = "kq7x9w2b4d8v6n5m3c1z0l9k8j7h6g5f4d3s2a1p0o9i8u7y6"
SERVER = "rancher.home.zypp.fr"
MANIFEST = f"""\
apiVersion: v1
kind: Secret
metadata: {{name: cattle-credentials-d7634b342b, namespace: cattle-system}}
data: {{token: {TOKEN}}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: cattle-cluster-agent, namespace: cattle-system}}
spec:
  template:
    spec:
      containers: [{{name: a, image: registry.rancher.com/rancher/rancher-agent:v2.14.1}}]
"""


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_capi_ri", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Rancher:
    """Faux Rancher : collection des clusters de provisionnement, jetons
    d'enregistrement, clusters de gestion."""

    def __init__(self, can_create=True, existing=None, token_form="substituted", manifest=MANIFEST,
                 too_big=False, delete_refused=False):
        self.server = SERVER
        self.can = can_create
        self.prov = dict(existing or {})
        self.token_form, self.manifest, self.too_big = token_form, manifest, too_big
        self.delete_refused = delete_refused
        self.calls, self.fetched = [], []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if path == ri.SCHEMA:
            return 200, {"collectionMethods": ["GET", "POST"] if self.can else ["GET"]}
        if path.startswith(ri.API_V1 + "/fleet-default/"):
            name = path.rsplit("/", 1)[1]
            if method == "GET":
                return (200, self.prov[name]) if name in self.prov else (404, {"code": "NotFound"})
            if method == "DELETE":
                if self.delete_refused:
                    return 403, {}
                self.prov.pop(name, None)
                return 200, {}
        if path == ri.API_V1 and method == "POST":
            o = json.loads(json.dumps(body))
            o["status"] = {"clusterName": CID, "ready": True}
            self.prov[o["metadata"]["name"]] = o
            return 201, o
        if path.startswith("/v3/clusterregistrationtokens"):
            url = (f"https://{SERVER}/v3/import/{TOKEN}_{CID}.yaml" if self.token_form == "substituted"
                   else f"https://{SERVER}/v3/import/{{token}}_{CID}.yaml")
            return 200, {"data": [{"manifestUrl": url, "token": TOKEN}]}
        if path == f"/v3/clusters/{CID}":
            return (200, {"state": "active"}) if self.prov else (404, {})
        return 404, {}

    def fetch(self, path):
        self.fetched.append(path)
        if self.too_big:
            ri.check_size(ri.MANIFEST_MAX + 1)
        return self.manifest


class Mgmt:
    def __init__(self, objs=None):
        self.objs = dict(objs or {})
        self.kubeconfig = "/nonexistent/session.kubeconfig"
        self.patches = []

    def get(self, kind, ns, name):
        return self.objs.get((kind, ns, name))

    def list(self, kind, ns=None, selector=None):
        return [o for (k, s, _), o in self.objs.items() if k == kind and (ns is None or s == ns)]

    def patch(self, kind, ns, name, body, dry_run=False):
        self.patches.append(body)
        ann = self.objs[(kind, ns, name)].setdefault("metadata", {}).setdefault("annotations", {})
        for k, v in body["metadata"].get("annotations", {}).items():
            if v is None:
                ann.pop(k, None)
            else:
                ann[k] = v


class Child:
    """Cluster enfant : objets laissés par Rancher, jugement du serveur."""

    def __init__(self, objs=None, crd_objects=None, refuse=(), ns_objects=None, apply_error=None):
        self.objs = list(objs or [])
        self.crd_objects = crd_objects or {}
        self.ns_objects = ns_objects or {}
        self.refuse = set(refuse)
        self.apply_error = apply_error
        self.applied, self.deleted, self.dry = [], [], []

    def run(self, *args, **kw):
        if args[:2] == ("apply", "-f"):
            self.applied.append(Path(args[2]).read_text())
            if self.apply_error:
                raise KubeError(self.apply_error)
            return ""
        raise AssertionError(args)

    def list(self, kind, ns=None, selector=None, missing_ok=True):
        if kind == "jobs":
            return []
        if kind in self.crd_objects:
            return self.crd_objects[kind]
        if ns is not None:
            return self.ns_objects.get(ns, [])
        return [o for o in self.objs if o["kind"] == kind]

    def api_resources(self, namespaced=None, verbs=None):
        return "configmaps\nsecrets\n"

    def delete(self, kind, ns, name, cascade=None, dry_run=False):
        if dry_run:
            self.dry.append(name)
            if name in self.refuse:
                raise KubeError('admission webhook "deny" denied the request')
            return
        self.deleted.append(name)


def capi_cluster(labels=None, annotations=None):
    return {"metadata": {"namespace": NS, "name": CL, "labels": dict(labels or {}),
                         "annotations": dict(annotations or {})}}


@pytest.fixture
def world(tmp_path, monkeypatch, cli):
    wd = tmp_path / "work"
    wd.mkdir(mode=0o700)
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", str(wd))
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "RANCHER_WAIT_ID", 0)
    monkeypatch.setattr(cli, "RANCHER_WAIT_TOKEN", 0)
    monkeypatch.setattr(cli, "CLEANUP_WAIT", 0)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    box = {"session": {"url": f"https://{SERVER}", "cid": "c-sg2q6", "token": "session-token"}, "read": 0}

    def run(mgmt, rancher, child, argv):
        monkeypatch.setattr(cli, "kube_from", lambda a: (mgmt, mgmt.kubeconfig, None))
        monkeypatch.setattr(cli, "_session_of", lambda kc: box["session"])
        monkeypatch.setattr(cli, "RANCHER", lambda r: rancher)

        def read(k, ns, c):
            box["read"] += 1
            if box.get("forbidden"):
                raise cli.ca.ChildAccessError("child-admin-required", "no")
            return "apiVersion: v1"
        monkeypatch.setattr(cli.ca, "read", read)

        @contextlib.contextmanager
        def session(k, ns, c):
            yield "/nonexistent/child.kubeconfig"
        monkeypatch.setattr(cli.ca, "session", session)
        monkeypatch.setattr(cli, "Kube", lambda path, **kw: child)
        monkeypatch.setattr(cli, "_agent_images_found", box.get("images") or (lambda k, ns, c, imgs: ([], {})))
        return cli.main(argv)
    return {"run": run, "box": box, "wd": wd}


IMPORT = ["rancher-import", "--kubeconfig", "/x", "--harvester-name", H, "--name", f"{NS}/{CL}", "--by", "alice"]
UNIMPORT = ["rancher-unimport", "--kubeconfig", "/x", "--harvester-name", H, "--name", f"{NS}/{CL}"]


def events(capsys):
    cap = capsys.readouterr()
    return [ln.split("|", 3)[1:] for ln in cap.err.splitlines() if ln.startswith("STEP_EVENT|")], cap


def test_import_goes_through_every_step_and_never_shows_the_token(cli, world, capsys):
    mgmt, r, child = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster()}), Rancher(), Child()
    assert world["run"](mgmt, r, child, IMPORT) == 0
    evs, cap = events(capsys)
    assert [e[0] for e in evs if e[1] == "done"] == ["session", "create", "token", "images", "apply", "wait"]
    assert TOKEN not in cap.err + cap.out and TOKEN not in json.dumps(mgmt.patches)
    assert r.fetched == [f"/v3/import/{TOKEN}_{CID}.yaml"]                   # relu par l'adresse de la session
    assert child.applied and TOKEN in child.applied[0]                       # appliqué tel quel
    assert list(world["wd"].iterdir()) == []                                 # fichier 0600 effacé
    ann = mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["annotations"]
    assert ann[ri.A_MARK] == f"{SERVER}/{NAME}/{CID}"
    assert r.prov[NAME]["metadata"]["annotations"]["harvester-ops.io/applied-by"] == "alice"


def test_the_manifest_file_is_erased_when_the_apply_fails(cli, world, capsys):
    mgmt = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster()})
    child = Child(apply_error=f"error validating cattle-credentials {TOKEN}")
    assert world["run"](mgmt, Rancher(), child, IMPORT) == 1
    evs, cap = events(capsys)
    assert list(world["wd"].iterdir()) == []
    assert TOKEN not in cap.err + cap.out and "<hidden>" in cap.err


@pytest.mark.parametrize("case,code,needle", [
    ("local-account", 2, "rancher-session-needed"),
    ("turtles", 2, "rancher-import-conflict"),
    ("forbidden", 2, "rancher-forbidden"),
    ("name-taken", 2, "rancher-name-taken"),
    ("elsewhere", 2, "rancher-import-elsewhere"),
    ("token-unreadable", 1, "rancher-token-unreadable"),
    ("too-big", 1, "rancher-manifest-too-big"),
    ("no-child-right", 2, "child-admin-required"),
    ("agent-image", 2, "rancher-agent-image-missing"),
])
def test_import_refusals(cli, world, capsys, case, code, needle):
    labels = {cc.AUTO_IMPORT: "true"} if case == "turtles" else {}
    ann = {ri.A_MARK: f"other.rancher/{NAME}/{CID}"} if case == "elsewhere" else {}
    mgmt = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster(labels, ann)})
    existing = {NAME: {"metadata": {"name": NAME, "labels": {}}}} if case == "name-taken" else {}
    r = Rancher(can_create=case != "forbidden", existing=existing,
                token_form="literal" if case == "token-unreadable" else "substituted", too_big=case == "too-big")
    child = Child()
    if case == "local-account":
        world["box"]["session"] = None
    if case == "no-child-right":
        world["box"]["forbidden"] = True
    if case == "agent-image":
        world["box"]["images"] = lambda k, ns, c, imgs: (
            [{"code": "rancher-agent-image-missing", "level": "block", "facts": {"image": imgs[0]}}], {})
    try:
        got = world["run"](mgmt, r, child, IMPORT)
    except ri.RancherImportError as e:            # levée hors des refus attrapés par main
        got, msg = 1, e.code
    else:
        msg = ""
    evs, cap = events(capsys)
    assert got == code
    assert needle in cap.err + msg
    assert not child.applied
    if case in ("local-account", "turtles", "no-child-right"):
        assert r.calls == []                     # rien demandé à Rancher
    if case in ("forbidden", "name-taken", "elsewhere"):
        assert not [c for c in r.calls if c[0] in ("POST", "PUT", "DELETE")]


def test_a_cluster_of_ours_already_in_rancher_is_resumed(cli, world, capsys):
    body = ri.cluster_body(NAME, H, NS, CL, "bob")
    body["status"] = {"clusterName": CID, "ready": True}
    mgmt, r = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster()}), Rancher(existing={NAME: body})
    assert world["run"](mgmt, r, Child(), IMPORT) == 0
    assert ("POST", ri.API_V1) not in r.calls
    assert "already there, resumed" in capsys.readouterr().err


def test_mutation_dry_run_writes_nothing(cli, world, capsys):
    mgmt, r, child = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster()}), Rancher(), Child()
    assert world["run"](mgmt, r, child, IMPORT + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == {"rancher": SERVER, "name": NAME, "exists": False}
    assert not [c for c in r.calls if c[0] != "GET"] and not mgmt.patches and not child.applied


# ---------------------------------------------------------------------------
# Retrait
# ---------------------------------------------------------------------------
def _o(kind, name):
    return {"kind": kind, "metadata": {"name": name}}


LEFT = [_o("Namespace", "local"), _o("Namespace", "equipe-a"), _o("ClusterRole", "cattle-admin"),
        _o("ClusterRoleBinding", "crb-elp57iuebr"), _o("CustomResourceDefinition", "settings.management.cattle.io"),
        _o("CustomResourceDefinition", "helmcharts.helm.cattle.io")]


def imported_world(run=None):
    body = ri.cluster_body(NAME, H, NS, CL, "bob", run=run)
    body["status"] = {"clusterName": CID, "ready": True}
    mgmt = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster(annotations={ri.A_MARK: f"{SERVER}/{NAME}/{CID}"})})
    return mgmt, Rancher(existing={NAME: body})


def test_unimport_removes_rancher_then_the_known_leftovers(cli, world, capsys):
    mgmt, r = imported_world()
    child = Child(LEFT)
    assert world["run"](mgmt, r, child, UNIMPORT) == 0
    assert ("DELETE", f"{ri.API_V1}/fleet-default/{NAME}") in r.calls and NAME not in r.prov
    assert set(child.dry) == set(child.deleted) == {"local", "cattle-admin", "crb-elp57iuebr",
                                                    "settings.management.cattle.io"}
    assert ri.A_MARK not in mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["annotations"]


def test_unimport_keeps_a_namespace_that_holds_foreign_objects(cli, world, capsys):
    mgmt, r = imported_world()
    child = Child(LEFT, ns_objects={"local": [{"kind": "ConfigMap", "metadata": {"name": "mine", "uid": "1"}}]},
                  crd_objects={"settings.management.cattle.io": [{"metadata": {"name": "x"}}]})
    assert world["run"](mgmt, r, child, UNIMPORT) == 0
    err = capsys.readouterr().err
    assert "kept-in-use: Namespace local kept" in err and "kept-in-use: CustomResourceDefinition" in err
    assert "local" not in child.deleted and "settings.management.cattle.io" not in child.deleted


def test_unimport_refused_by_the_server_deletes_nothing(cli, world, capsys):
    mgmt, r = imported_world()
    child = Child(LEFT, refuse={"cattle-admin"})
    assert world["run"](mgmt, r, child, UNIMPORT) == 1
    assert child.deleted == [] and "the server refuses" in capsys.readouterr().err


@pytest.mark.parametrize("expect,code,needle", [
    ("ffffffffffff", 2, "undo-not-ours"),
    ("0123456789ab", 0, "no longer imported"),
])
def test_unimport_rereads_the_run_mark(cli, world, capsys, expect, code, needle):
    mgmt, r = imported_world(run="0123456789ab")
    child = Child([])
    assert world["run"](mgmt, r, child, UNIMPORT + ["--expect-run", expect]) == code
    assert needle in capsys.readouterr().err
    if code:
        assert NAME in r.prov and not [c for c in r.calls if c[0] == "DELETE"]


def test_unimport_without_session_or_mark(cli, world, capsys):
    mgmt = Mgmt({(cs.K_CLUSTER, NS, CL): capi_cluster()})
    assert world["run"](mgmt, Rancher(), Child(), UNIMPORT) == 0
    assert "is not imported" in capsys.readouterr().err
    mgmt, r = imported_world()
    world["box"]["session"] = None
    assert world["run"](mgmt, r, Child(), UNIMPORT) == 2
    assert "rancher-session-needed" in capsys.readouterr().err and NAME in r.prov


def test_unimport_dry_run_lists_without_writing(cli, world, capsys):
    mgmt, r = imported_world()
    child = Child(LEFT)
    assert world["run"](mgmt, r, child, UNIMPORT + ["--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["present"] and set(out["delete"]) == {"Namespace local", "ClusterRole cattle-admin",
                                                     "ClusterRoleBinding crb-elp57iuebr",
                                                     "CustomResourceDefinition settings.management.cattle.io"}
    assert child.deleted == [] and NAME in r.prov


def test_delete_removes_the_import_first(cli, world, capsys, monkeypatch):
    mgmt, r = imported_world()
    order = []
    monkeypatch.setattr(cli, "rancher_unimport", lambda *a, **k: order.append("unimport") or 2)
    monkeypatch.setattr(cli, "remove_cluster", lambda *a, **k: order.append("remove") or 0)
    assert world["run"](mgmt, r, Child(), ["delete", "--kubeconfig", "/x", "--name", f"{NS}/{CL}"]) == 2
    assert order == ["unimport"] and "still imported in Rancher" in capsys.readouterr().err


def test_status_lists_marked_clusters(cli, world, capsys):
    mgmt, r = imported_world()
    assert world["run"](mgmt, r, Child(), ["rancher-status", "--kubeconfig", "/x", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session"] == SERVER
    assert out["clusters"] == [{"namespace": NS, "cluster": CL, "imported": True, "turtles": False,
                                "rancher": SERVER, "name": NAME, "cid": CID, "state": "active", "ready": True}]


def test_create_refuses_rancher_external_without_session(cli, world):
    mgmt = Mgmt({})
    world["box"]["session"] = None
    import capi_cluster as c2
    spec = c2.normalize({"name": "x", "rancher_external": True})
    f = cli._session_findings(mgmt, spec)
    assert f == [{"code": "rancher-session-needed", "level": "block", "facts": {}}]
    assert cli._session_findings(mgmt, c2.normalize({"name": "x"})) == []
