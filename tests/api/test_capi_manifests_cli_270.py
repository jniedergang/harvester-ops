"""2.7.0 (tâche 2) : `harvester-capi manifest-check/-deploy/-remove`, `manifests`.

Un faux cluster de gestion (Harvester) tient ConfigMaps, CRS, bindings et
Cluster ; un faux cluster enfant tient les objets livrés. Comportements
relevés sur harvlab2 et reproduits ici : le binding passe à l'empreinte du
nouveau ConfigMap en quelques secondes, Cluster API pose sur le ConfigMap
une référence de propriétaire vers le CRS (d'où `--cascade=orphan`), et ne
retire jamais un objet sorti du contenu.
"""

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_manifests as mf  # noqa: E402
import capi_stack as cs  # noqa: E402
from kube import KubeError  # noqa: E402

NS, CL = "rke2-banc", "rke2-banc"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_capi_mf", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RES = {"ConfigMap": "configmaps", "ClusterResourceSet": mf.K_CRS}


class Mgmt:
    """Cluster de gestion : la réconciliation des CRS est simulée à
    l'application (binding à l'empreinte du ConfigMap), sauf `stale`."""

    def __init__(self, objs=None, stale=False):
        self.objs = dict(objs or {})
        self.calls = []
        self.stale = stale

    def get(self, kind, ns, name):
        return self.objs.get((kind, ns, name))

    def list(self, kind, ns=None, selector=None):
        out = [o for (k, s, _), o in self.objs.items() if k == kind and (ns is None or s == ns)]
        if selector:
            out = [o for o in out if selector in ((o.get("metadata") or {}).get("labels") or {})]
        return out

    def apply(self, docs, **kw):
        self.calls.append(("apply", [d["kind"] for d in docs]))
        for d in docs:
            md = d["metadata"]
            self.objs[(RES[d["kind"]], md["namespace"], md["name"])] = json.loads(json.dumps(d))
        if not self.stale:
            for d in docs:
                if d["kind"] == "ConfigMap":
                    self._bind(d)

    def _bind(self, cmap):
        name = cmap["metadata"]["name"]
        b = self.objs.setdefault((mf.K_CRSB, NS, CL), {"metadata": {"name": CL, "namespace": NS},
                                                      "spec": {"clusterName": CL, "bindings": []}})
        b["spec"]["bindings"] = [x for x in b["spec"]["bindings"] if x["clusterResourceSetName"] != name]
        b["spec"]["bindings"].append({"clusterResourceSetName": name, "resources": [
            {"kind": "ConfigMap", "name": name, "applied": True, "hash": mf.content_hash(cmap)}]})

    def patch(self, kind, ns, name, body, dry_run=False):
        self.calls.append(("patch", kind, body["metadata"].get("labels", body["metadata"].get("annotations"))))
        for field in ("labels", "annotations"):
            if field not in body["metadata"]:
                continue
            cur = self.objs[(kind, ns, name)]["metadata"].setdefault(field, {})
            for k, v in body["metadata"][field].items():
                if v is None:
                    cur.pop(k, None)
                else:
                    cur[k] = v

    def delete(self, kind, ns, name, cascade=None, dry_run=False):
        self.calls.append(("delete", kind, name, cascade))
        self.objs.pop((kind, ns, name), None)


# découverte du cluster enfant (`kubectl api-resources --no-headers`)
API_RESOURCES = """\
configmaps        cm      v1        true    ConfigMap
secrets                   v1        true    Secret
serviceaccounts   sa      v1        true    ServiceAccount
namespaces        ns      v1        false   Namespace
deployments       deploy  apps/v1   true    Deployment
networkpolicies   netpol  networking.k8s.io/v1   true   NetworkPolicy
resourcequotas    quota   v1        true    ResourceQuota
limitranges       limits  v1        true    LimitRange
rolebindings              rbac.authorization.k8s.io/v1   true   RoleBinding
widgets                   example.io/v1   true   Widget
"""


class Child:
    def __init__(self, objs, refuse=(), foreign=None, missing=(), unlistable=False):
        self.objs = {(mf.resource_of(mf.ref_of(o)), mf.ref_of(o)[2] or None, o["metadata"]["name"]): o for o in objs}
        self.refuse, self.foreign = set(refuse), foreign or {}
        self.missing, self.unlistable = set(missing), unlistable
        self.deleted, self.dry, self.listed = [], [], []

    def get(self, res, ns, name):
        if res.split(".")[0] in self.missing:
            raise KubeError(f'error: the server doesn\'t have a resource type "{res}"')
        return self.objs.get((res, ns, name))

    def api_resources(self, namespaced=None, verbs=None):
        if verbs:
            return "pods\nconfigmaps\nsecrets\nevents\npersistentvolumeclaims\nwidgets.example.io\n"
        return API_RESOURCES

    def list(self, kinds, ns=None, selector=None, missing_ok=True):
        self.listed.append((kinds, ns, missing_ok))
        if self.unlistable:
            raise KubeError('error: the server doesn\'t have a resource type "widgets"')
        inside = [o for (_, s, _), o in self.objs.items() if s == ns]
        return inside + self.foreign.get(ns, [])

    def delete(self, res, ns, name, cascade=None, dry_run=False):
        if dry_run:
            self.dry.append(name)
            if name in self.refuse:
                raise KubeError('admission webhook "deny" denied the request')
            return
        self.deleted.append(name)
        self.objs.pop((res, ns, name), None)


def obj(kind, name, ns=None, api="v1", labels=None):
    md = {"name": name}
    if ns:
        md["namespace"] = ns
    if labels:
        md["labels"] = labels
    return {"apiVersion": api, "kind": kind, "metadata": md}


NSO = obj("Namespace", "essai")
CMO = obj("ConfigMap", "essai-cm", "essai")
DEP = obj("Deployment", "essai-web", "essai", api="apps/v1")


def cluster(labels=None):
    return {"metadata": {"namespace": NS, "name": CL, "labels": dict(labels or {})}}


def stamped(o, item="essai"):
    return mf.stamp([o], item, CL)[0]


@pytest.fixture
def world(tmp_path, monkeypatch, cli):
    """Un paquet `banc` 1.0.0 (manifeste `essai`, politique `regles`) dans
    le répertoire d'état, et les points d'entrée remplacés."""
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    def package(version, objs, item="essai", kind="manifest"):
        d = tmp_path / "artefacts.d" / f"banc-{version}"
        (d / "manifests" / item).mkdir(parents=True, exist_ok=True)
        (d / "manifests" / item / "a.yaml").write_text("\n---\n".join(json.dumps(o) for o in objs))
        items = [{"name": item, "kind": kind, "files": [f"manifests/{item}/a.yaml"]},
                 {"name": "podinfo", "kind": "chart", "chart": "charts/podinfo-6.9.2.tgz"},
                 {"name": "x" * 43, "kind": "manifest", "files": [f"manifests/{'x' * 43}/a.yaml"]},
                 {"name": "evil", "kind": "manifest", "files": ["manifests/evil/../../../etc/passwd"]}]
        (d / "artefacts.yaml").write_text(json.dumps({"name": "banc", "version": version, "items": items}))
    package("1.0.0", [NSO, CMO, DEP])
    package("1.0.1", [NSO, CMO])
    state = {"session": 0}

    def run(kube, child, argv):
        monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))

        @contextlib.contextmanager
        def session(k, ns, c):
            state["session"] += 1
            yield "/nonexistent/child.kubeconfig"
        monkeypatch.setattr(cli.ca, "session", session)
        monkeypatch.setattr(cli, "Kube", lambda path, **kw: child)
        return cli.main(argv)

    def spec(**over):
        body = {"cluster": f"{NS}/{CL}", "package": "banc", "version": "1.0.0", "item": "essai", "by": "alice"}
        body.update(over)
        p = tmp_path / "spec.json"
        p.write_text(json.dumps({k: v for k, v in body.items() if v is not None}))
        return str(p)
    return {"run": run, "spec": spec, "state": state, "tmp": tmp_path}


def events(capsys):
    return [ln.split("|", 3)[1:] for ln in capsys.readouterr().err.splitlines() if ln.startswith("STEP_EVENT|")]


def mgmt_with_cluster(**kw):
    return Mgmt({(cs.K_CLUSTER, NS, CL): cluster()}, **kw)


# ---------------------------------------------------------------------------
# Demande
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("body,needle", [
    ({"cluster": "a/b", "package": "p", "version": "1", "item": "i", "extra": 1}, "unknown key: extra"),
    ({"cluster": "a", "package": "p", "version": "1", "item": "i"}, "--name expects"),
    ({"cluster": "A/b", "package": "p", "version": "1", "item": "i"}, "--name expects"),
    ({"cluster": "a/b", "package": "p", "version": "1"}, "item: required"),
    ({"cluster": "a/b", "package": "p", "version": "1", "item": "i", "policies": {}}, "either policies"),
    ({"cluster": "a/b", "package": "p", "version": "1", "item": "i", "origin": {"run": "zz"}}, "invalid value"),
    ({"cluster": "a/b", "package": "p", "version": "1", "item": "i", "by": "a\nb"}, "by: one line"),
    ({"cluster": "a/b", "policies": {"base": "yes"}}, "base: true or false"),
])
def test_manifest_requests_are_checked(cli, tmp_path, body, needle):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(body))
    with pytest.raises(ValueError) as e:
        cli.manifest_spec_from(str(p))
    assert needle in str(e.value)


@pytest.mark.parametrize("item,code", [
    ("x" * 43, "artefact-name-too-long"),
    ("podinfo", "artefact-item-not-manifest"),
    ("absent", "artefact-item-not-manifest"),
    ("evil", "artefact-file-missing"),
])
def test_items_that_cannot_be_applied(cli, world, item, code):
    spec = cli.manifest_spec_from(world["spec"](item=item))
    _, found = cli.manifest_objects(spec)
    assert [f["code"] for f in found] == [code]


def test_a_policy_item_is_read_like_a_manifest(cli, world, tmp_path):
    d = tmp_path / "artefacts.d" / "banc-1.0.0"
    idx = json.loads((d / "artefacts.yaml").read_text())
    idx["items"][0]["kind"] = "policy"
    (d / "artefacts.yaml").write_text(json.dumps(idx))
    objs, found = cli.manifest_objects(cli.manifest_spec_from(world["spec"]()))
    assert found == [] and [o["kind"] for o in objs] == ["Namespace", "ConfigMap", "Deployment"]


def test_the_policies_request_renders_the_builtin_item(cli, world):
    spec = cli.manifest_spec_from(world["spec"](package=None, version=None, item=None, policies={
        "base": True, "teams": [{"namespace": "equipe-a"}]}))
    assert (spec["item"], spec["package"], spec["version"]) == ("hops-policies", "harvester-ops", "builtin")
    objs, found = cli.manifest_objects(spec)
    assert found == [] and len(objs) == 7


# ---------------------------------------------------------------------------
# Déploiement
# ---------------------------------------------------------------------------
def test_first_deploy_declares_selects_and_waits(cli, world, capsys):
    kube, child = mgmt_with_cluster(), Child([])
    assert world["run"](kube, child, ["manifest-deploy", "--spec", world["spec"]()]) == cli.EXIT_OK
    cmap = kube.get("configmaps", NS, "art-essai")
    assert [o["kind"] for o in mf.objects_of(cmap)] == ["Namespace", "ConfigMap", "Deployment"]
    assert kube.get(mf.K_CRS, NS, "art-essai")["spec"]["strategy"] == "Reconcile"
    assert kube.get(cs.K_CLUSTER, NS, CL)["metadata"]["labels"] == {"harvester-ops.io/art-essai": "on"}
    ev = events(capsys)
    assert [e[0] for e in ev] == ["check", "apply", "reconcile", "prune"]
    assert world["state"]["session"] == 1                       # contrôles seulement : rien à retirer
    assert cmap["metadata"]["annotations"][mf.A_BY] == "alice"


def test_an_update_prunes_only_what_left_and_still_carries_the_labels(cli, world, capsys):
    kube, child = mgmt_with_cluster(), Child([])
    world["run"](kube, child, ["manifest-deploy", "--spec", world["spec"]()])
    capsys.readouterr()
    child = Child([stamped(NSO), stamped(CMO), stamped(DEP)])
    assert world["run"](kube, child, ["manifest-deploy", "--spec", world["spec"](version="1.0.1")]) == cli.EXIT_OK
    assert child.dry == ["essai-web"] and child.deleted == ["essai-web"]
    ev = events(capsys)
    assert ev[-1][:2] == ["prune", "done"] and "1 object(s)" in ev[-1][2]


def test_an_object_relabelled_by_hand_is_kept(cli, world, capsys):
    kube = mgmt_with_cluster()
    world["run"](kube, Child([]), ["manifest-deploy", "--spec", world["spec"]()])
    capsys.readouterr()
    child = Child([stamped(NSO), stamped(CMO), obj("Deployment", "essai-web", "essai", api="apps/v1")])
    assert world["run"](kube, child, ["manifest-deploy", "--spec", world["spec"](version="1.0.1")]) == cli.EXIT_OK
    assert child.deleted == [] and ("Deployment.v1.apps", "essai", "essai-web") in child.objs
    assert any("kept-relabelled" in e[2] for e in events(capsys))


def test_no_prune_before_the_binding_carries_the_new_content(cli, world, capsys):
    """Mutation « prune avant reconcile » : un binding qui reste à l'ancienne
    empreinte arrête l'action en échec, sans rien supprimer."""
    kube = mgmt_with_cluster()
    world["run"](kube, Child([]), ["manifest-deploy", "--spec", world["spec"]()])
    kube.stale = True
    child = Child([stamped(NSO), stamped(CMO), stamped(DEP)])
    rc = world["run"](kube, child, ["manifest-deploy", "--spec", world["spec"](version="1.0.1"), "--timeout", "0"])
    assert rc == cli.EXIT_FAIL and child.deleted == [] and child.dry == []
    assert ["reconcile", "error"] == events(capsys)[-1][:2]


@pytest.mark.parametrize("setup,code", [
    (lambda k: k.objs.pop((cs.K_CLUSTER, NS, CL)), "manifest-cluster-missing"),
    (lambda k: k.objs.__setitem__((mf.K_CRS, NS, "art-essai"), {"metadata": {"labels": {
        mf.L_CLUSTER: "autre", mf.L_PACKAGE: "banc"}}}), "artefact-taken"),
    (lambda k: k.objs.__setitem__((mf.K_CRS, NS, "art-essai"), {"metadata": {"labels": {
        mf.L_CLUSTER: CL, mf.L_PACKAGE: "autre"}}}), "artefact-taken"),
])
def test_blocked_deploys_write_nothing(cli, world, capsys, setup, code):
    kube = mgmt_with_cluster()
    setup(kube)
    assert world["run"](kube, Child([]), ["manifest-deploy", "--spec", world["spec"]()]) == cli.EXIT_BLOCKED
    assert not any(c[0] in ("apply", "patch", "delete") for c in kube.calls)
    assert any(code in e[2] for e in events(capsys))


def test_a_team_namespace_taken_blocks_the_policies(cli, world, capsys):
    kube = mgmt_with_cluster()
    child = Child([obj("Namespace", "equipe-a")])
    spec = world["spec"](package=None, version=None, item=None,
                         policies={"base": True, "teams": [{"namespace": "equipe-a"}]})
    assert world["run"](kube, child, ["manifest-deploy", "--spec", spec]) == cli.EXIT_BLOCKED
    assert any("team-namespace-taken" in e[2] for e in events(capsys)) and kube.calls == []
    child = Child([mf.stamp([obj("Namespace", "equipe-a")], "hops-policies", CL)[0]])
    assert world["run"](kube, child, ["manifest-deploy", "--spec", spec]) == cli.EXIT_OK


def test_check_reports_without_writing(cli, world, capsys):
    kube = mgmt_with_cluster()
    assert world["run"](kube, Child([]), ["manifest-check", "--spec", world["spec"]()]) == cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out == {"blocked": False, "findings": [], "item": "essai", "objects": 3} and kube.calls == []


# ---------------------------------------------------------------------------
# Retrait
# ---------------------------------------------------------------------------
def deployed(cli, world, capsys, version="1.0.0"):
    kube = mgmt_with_cluster()
    world["run"](kube, Child([]), ["manifest-deploy", "--spec", world["spec"](version=version)])
    kube.calls.clear()
    capsys.readouterr()
    return kube


def test_remove_crs_first_then_objects_in_order_then_configmap(cli, world, capsys):
    kube = deployed(cli, world, capsys)
    child = Child([stamped(NSO), stamped(CMO), stamped(DEP)])
    order = []
    child_delete, mgmt_delete = child.delete, kube.delete
    child.delete = lambda *a, **k: (order.append(("child", a[2], k.get("dry_run", False))), child_delete(*a, **k))
    kube.delete = lambda *a, **k: (order.append(("mgmt", a[0], k.get("cascade"))), mgmt_delete(*a, **k))
    assert world["run"](kube, child, ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == cli.EXIT_OK
    writes = [o for o in order if not (o[0] == "child" and o[2])]
    assert writes == [("mgmt", mf.K_CRS, "orphan"), ("child", "essai-cm", False), ("child", "essai-web", False),
                      ("child", "essai", False), ("mgmt", "configmaps", None)]
    assert [o[1] for o in order if o[0] == "child" and o[2]] == ["essai-cm", "essai-web", "essai"]  # jugés avant
    assert kube.get(cs.K_CLUSTER, NS, CL)["metadata"]["labels"] == {}
    assert kube.get("configmaps", NS, "art-essai") is None


def test_remove_keeps_an_object_without_the_labels(cli, world, capsys):
    kube = deployed(cli, world, capsys)
    child = Child([stamped(NSO), obj("ConfigMap", "essai-cm", "essai"), stamped(DEP)])
    assert world["run"](kube, child, ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == cli.EXIT_OK
    assert ("ConfigMap", "essai", "essai-cm") in child.objs and "essai-cm" not in child.deleted
    assert any("kept-relabelled" in e[2] and "essai-cm" in e[2] for e in events(capsys))


def test_remove_keeps_a_namespace_that_holds_foreign_work(cli, world, capsys):
    kube = deployed(cli, world, capsys)
    child = Child([stamped(NSO), stamped(CMO), stamped(DEP)],
                  foreign={"essai": [obj("Deployment", "team-app", "essai", api="apps/v1"),
                                     {"metadata": {"name": "owned-pod", "ownerReferences": [{"kind": "ReplicaSet"}]}}]})
    assert world["run"](kube, child, ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == cli.EXIT_OK
    assert child.deleted == ["essai-cm", "essai-web"]
    # le pod d'un ReplicaSet qui n'est pas de l'élément compte aussi (revue 1-3, C1)
    assert any("kept-in-use" in e[2] and "2 object(s)" in e[2] for e in events(capsys))


def test_remove_of_a_deleted_cluster_never_reads_its_kubeconfig(cli, world, capsys):
    kube = deployed(cli, world, capsys)
    kube.objs.pop((cs.K_CLUSTER, NS, CL))
    before = world["state"]["session"]
    assert world["run"](kube, Child([]), ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == cli.EXIT_OK
    assert world["state"]["session"] == before
    assert kube.get(mf.K_CRS, NS, "art-essai") is None and kube.get("configmaps", NS, "art-essai") is None
    assert any("left with it" in e[2] for e in events(capsys))


def test_a_refused_server_dry_run_deletes_nothing(cli, world, capsys):
    kube = deployed(cli, world, capsys)
    child = Child([stamped(NSO), stamped(CMO), stamped(DEP)], refuse={"essai-web"})
    assert world["run"](kube, child, ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == cli.EXIT_FAIL
    assert child.deleted == [] and kube.get(mf.K_CRS, NS, "art-essai") is not None
    assert not any(c[0] == "delete" for c in kube.calls)
    assert "nothing deleted" in events(capsys)[-1][2]


def test_remove_after_the_crs_left_finishes_from_the_configmap(cli, world, capsys):
    """Reprise : CRS déjà supprimé (orphan), ConfigMap resté : les objets
    sont retirés quand même."""
    kube = deployed(cli, world, capsys)
    kube.objs.pop((mf.K_CRS, NS, "art-essai"))
    child = Child([stamped(NSO), stamped(CMO), stamped(DEP)])
    assert world["run"](kube, child, ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == cli.EXIT_OK
    assert sorted(child.deleted) == ["essai", "essai-cm", "essai-web"]


@pytest.mark.parametrize("labels", [{mf.L_CLUSTER: "autre", mf.L_ITEM: "essai"},
                                    {mf.L_CLUSTER: CL, mf.L_ITEM: "autre"}])
def test_remove_refuses_an_item_of_someone_else(cli, world, capsys, labels):
    kube = mgmt_with_cluster()
    kube.objs[(mf.K_CRS, NS, "art-essai")] = {"metadata": {"labels": labels}}
    assert world["run"](kube, Child([]), ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == cli.EXIT_BLOCKED
    assert kube.calls == []


def test_remove_of_nothing_is_done(cli, world, capsys):
    assert world["run"](mgmt_with_cluster(), Child([]), ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == 0


def test_manifests_lists_the_state(cli, world, capsys):
    kube = deployed(cli, world, capsys)
    assert world["run"](kube, Child([]), ["manifests", "--json"]) == cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1 and out[0]["item"] == "essai" and out[0]["cluster"] == CL and out[0]["objects"] == 3
    assert out[0]["state"]["applied"] is True and out[0]["applied_by"] == "alice"


# ---------------------------------------------------------------------------
# Corrections de la revue des tâches 1 à 3
# ---------------------------------------------------------------------------
def test_c1_a_namespace_whose_content_cannot_be_listed_is_kept(cli, world, capsys):
    kube = deployed(cli, world, capsys)
    child = Child([stamped(NSO), stamped(CMO), stamped(DEP)], unlistable=True)
    assert world["run"](kube, child, ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == cli.EXIT_OK
    assert child.deleted == ["essai-cm", "essai-web"]
    assert child.listed and child.listed[0][2] is False                      # échec fermé, jamais []
    assert any("kept-in-use" in e[2] and "could not be listed" in e[2] for e in events(capsys))


@pytest.mark.parametrize("other", [
    {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
     "metadata": {"name": "pg-1", "namespace": "essai", "uid": "p", "ownerReferences": [{"uid": "cnpg"}]}},
    {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "creds", "namespace": "essai", "uid": "s"}},
    {"apiVersion": "example.io/v1", "kind": "Widget", "metadata": {"name": "w", "namespace": "essai", "uid": "w"}},
])
def test_c1_team_data_keeps_the_namespace(cli, world, capsys, other):
    kube = deployed(cli, world, capsys)
    child = Child([stamped(NSO), stamped(CMO), stamped(DEP)], foreign={"essai": [other]})
    assert world["run"](kube, child, ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == cli.EXIT_OK
    assert "essai" not in child.deleted
    scanned = child.listed[0][0].split(",")
    assert "secrets" in scanned and "widgets.example.io" in scanned and "events" not in scanned


def test_i5_a_kind_no_longer_served_counts_as_gone(cli, world, capsys):
    kube = deployed(cli, world, capsys)
    child = Child([stamped(NSO), stamped(CMO)], missing={"Deployment"})
    assert world["run"](kube, child, ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == cli.EXIT_OK
    assert sorted(child.deleted) == ["essai", "essai-cm"]
    assert any("no longer served" in e[2] for e in events(capsys))
    assert kube.get("configmaps", NS, "art-essai") is None


@pytest.mark.parametrize("present", [
    obj("ConfigMap", "essai-cm", "essai"),                                    # objet d'équipe homonyme
    obj("Namespace", "essai"),                                                # namespace existant
    obj("ConfigMap", "essai-cm", "essai", labels=mf.marks("autre", CL)),      # d'un autre élément
])
def test_i2_an_object_already_there_blocks(cli, world, capsys, present):
    kube = mgmt_with_cluster()
    child = Child([present])
    assert world["run"](kube, child, ["manifest-deploy", "--spec", world["spec"]()]) == cli.EXIT_BLOCKED
    assert kube.calls == []
    assert any("manifest-object-taken" in e[2] for e in events(capsys))
    assert world["run"](kube, child, ["manifest-check", "--spec", world["spec"]()]) == cli.EXIT_BLOCKED
    assert "manifest-object-taken" in [f["code"] for f in json.loads(capsys.readouterr().out)["findings"]]


def test_i2_our_own_objects_do_not_block_an_update(cli, world, capsys):
    kube = deployed(cli, world, capsys)
    child = Child([stamped(NSO), stamped(CMO), stamped(DEP)])
    assert world["run"](kube, child, ["manifest-deploy", "--spec", world["spec"](version="1.0.1")]) == cli.EXIT_OK


def test_i1_the_child_discovery_judges_custom_kinds(cli, world, capsys, tmp_path):
    d = tmp_path / "artefacts.d" / "banc-1.0.0"
    flow = {"apiVersion": "flowcontrol.apiserver.k8s.io/v1", "kind": "FlowSchema",
            "metadata": {"name": "f", "namespace": "essai"}}
    widget = obj("Widget", "w", "essai", api="example.io/v1")
    for objs, rc in (([widget], cli.EXIT_OK), ([flow], cli.EXIT_BLOCKED),
                     ([obj("Gadget", "g", "essai", api="example.io/v1")], cli.EXIT_BLOCKED)):
        (d / "manifests" / "essai" / "a.yaml").write_text(json.dumps(objs[0]))
        kube = mgmt_with_cluster()
        assert world["run"](kube, Child([]), ["manifest-deploy", "--spec", world["spec"]()]) == rc
    # sans le cluster enfant, le contrôle dit « inconnue » au lieu de trancher
    kube = mgmt_with_cluster()
    kube.objs.pop((cs.K_CLUSTER, NS, CL))
    world["run"](kube, Child([]), ["manifest-check", "--spec", world["spec"]()])
    out = json.loads(capsys.readouterr().out)
    assert {"manifest-cluster-missing", "manifest-kind-unknown"} <= {f["code"] for f in out["findings"]}


def test_m1_the_child_admin_right_is_needed_for_a_package_item(cli, world, capsys, monkeypatch):
    kube = mgmt_with_cluster()

    @contextlib.contextmanager
    def refused(k, ns, c):
        raise cli.ca.ChildAccessError("child-admin-required", "forbidden")
        yield
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    monkeypatch.setattr(cli.ca, "session", refused)
    assert cli.main(["manifest-deploy", "--spec", world["spec"]()]) == cli.EXIT_BLOCKED
    assert kube.calls == []
    assert cli.main(["manifest-check", "--spec", world["spec"]()]) == cli.EXIT_BLOCKED
    out = json.loads(capsys.readouterr().out)
    assert [f["code"] for f in out["findings"]] == ["child-admin-required"]


def test_i3_a_stopped_update_prunes_on_resume(cli, world, capsys):
    kube = deployed(cli, world, capsys)
    kube.stale = True
    child = Child([stamped(NSO), stamped(CMO), stamped(DEP)])
    rc = world["run"](kube, child, ["manifest-deploy", "--spec", world["spec"](version="1.0.1"), "--timeout", "0"])
    assert rc == cli.EXIT_FAIL and child.deleted == []
    cmap = kube.get("configmaps", NS, "art-essai")
    assert mf.pending_of(cmap) == [mf.ref_of(DEP)]                           # écrit AVANT l'application
    kube.stale = False
    assert world["run"](kube, child, ["manifest-deploy", "--spec", world["spec"](version="1.0.1")]) == cli.EXIT_OK
    assert child.deleted == ["essai-web"]
    assert mf.A_PENDING not in kube.get("configmaps", NS, "art-essai")["metadata"]["annotations"]


def test_i3_remove_also_takes_what_was_pending(cli, world, capsys):
    kube = deployed(cli, world, capsys)
    kube.stale = True
    child = Child([stamped(NSO), stamped(CMO), stamped(DEP)])
    world["run"](kube, child, ["manifest-deploy", "--spec", world["spec"](version="1.0.1"), "--timeout", "0"])
    assert world["run"](kube, child, ["manifest-remove", "--name", f"{NS}/{CL}/essai"]) == cli.EXIT_OK
    assert sorted(child.deleted) == ["essai", "essai-cm", "essai-web"]
