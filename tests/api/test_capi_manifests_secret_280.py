"""2.8.0 (tâche 4, point 227) : ressource Secret de type
`addons.cluster.x-k8s.io/resource-set` dans le CRS d'un élément. Vu sur
harvlab2 (plan 2.8.0, « Essais préalables (tâche 1) », point 4) : appliquée
telle quelle ; empreinte du binding = sha256 des valeurs décodées ; le CRS
supprimé emporte ConfigMap ET Secret (d'où `--cascade=orphan`)."""

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import capi_manifests as mf  # noqa: E402
import capi_stack as cs  # noqa: E402
from test_capi_manifests_cli_270 import Child, Mgmt, cli, cluster, events, world  # noqa: E402,F401

NS, CL = "rke2-banc", "rke2-banc"
SA = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "hops-gitops-demo", "namespace": "hops-gitops"}}
SEC = {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
       "metadata": {"name": "hops-gitops-demo-pull", "namespace": "hops-gitops"},
       "stringData": {".dockerconfigjson": '{"auths": {"r": {"password": "PULL-PW-42"}}}'}}


def test_docs_put_credentials_in_the_secret_only():
    docs = mf.docs(NS, CL, "gitops-demo", "banc", "1.0.0", [SA], secrets=[SEC])
    assert [d["kind"] for d in docs] == ["ConfigMap", "Secret", "ClusterResourceSet"]
    cm, sec, crs = docs
    assert "PULL-PW-42" not in json.dumps(cm) and "PULL-PW-42" in json.dumps(sec)
    assert sec["type"] == "addons.cluster.x-k8s.io/resource-set" and sec["metadata"]["name"] == "art-gitops-demo"
    assert crs["spec"]["resources"] == [{"kind": "ConfigMap", "name": "art-gitops-demo"},
                                        {"kind": "Secret", "name": "art-gitops-demo"}]
    assert mf.secret_objects(sec) == mf.stamp([SEC], "gitops-demo", CL)
    assert len(mf.docs(NS, CL, "x", "p", "1", [SA])) == 2                     # sans Secret, comme en 2.7.0


def test_the_hash_of_a_secret_is_on_decoded_values():
    sec = mf.docs(NS, CL, "gitops-demo", "banc", "1.0.0", [SA], secrets=[SEC])[1]
    want = "sha256:" + hashlib.sha256(sec["stringData"]["objects.yaml"].encode()).hexdigest()
    assert mf.content_hash(sec) == want
    read_back = {"kind": "Secret", "data": {k: base64.b64encode(v.encode()).decode()
                                            for k, v in sec["stringData"].items()}}
    assert mf.content_hash(read_back) == want
    assert mf.secret_objects(read_back) == mf.secret_objects(sec)


def test_state_needs_both_resources_applied():
    cm, sec, crs = mf.docs(NS, CL, "gitops-demo", "banc", "1.0.0", [SA], secrets=[SEC])

    def binding(sec_hash):
        return {"spec": {"clusterName": CL, "bindings": [{"clusterResourceSetName": "art-gitops-demo", "resources": [
            {"kind": "ConfigMap", "name": "art-gitops-demo", "applied": True, "hash": mf.content_hash(cm)},
            {"kind": "Secret", "name": "art-gitops-demo", "applied": True, "hash": sec_hash}]}]}}
    assert mf.state(crs, [binding(mf.content_hash(sec))], cm, sec)["applied"]
    assert not mf.state(crs, [binding("sha256:old")], cm, sec)["applied"]


class MgmtS(Mgmt):
    """Le faux cluster de gestion range aussi les Secrets et les lie."""

    def apply(self, docs, **kw):
        for d in docs:
            if d["kind"] == "Secret":
                md = d["metadata"]
                self.objs[("secrets", md["namespace"], md["name"])] = json.loads(json.dumps(d))
        super().apply([d for d in docs if d["kind"] != "Secret"], **kw)
        for d in docs:
            if d["kind"] == "Secret":
                b = self.objs[(mf.K_CRSB, NS, CL)]["spec"]["bindings"]
                for x in b:
                    if x["clusterResourceSetName"] == d["metadata"]["name"]:
                        x["resources"].append({"kind": "Secret", "name": d["metadata"]["name"], "applied": True,
                                               "hash": mf.content_hash(d)})


def test_deliver_waits_for_the_secret_and_removal_deletes_it(cli, world, monkeypatch, capsys):
    mgmt = MgmtS({(cs.K_CLUSTER, NS, CL): cluster()})
    child = Child([])
    monkeypatch.setattr(cli, "Kube", lambda path, **kw: child)
    monkeypatch.setattr(cli, "kube_from", lambda a: (mgmt, None, None))
    assert cli.deliver(mgmt, NS, CL, "gitops-demo", "banc", "1.0.0", [SA], {}, "alice", 30, secrets=[SEC]) == 0
    assert ("secrets", NS, "art-gitops-demo") in mgmt.objs
    # retrait : CRS sans cascade, puis ConfigMap et Secret supprimés, objets du Secret retirés
    for o in mf.stamp([SA, SEC], "gitops-demo", CL):
        child.objs[(mf.resource_of(mf.ref_of(o)), mf.ref_of(o)[2] or None, o["metadata"]["name"])] = o
    import contextlib

    @contextlib.contextmanager
    def session(k, ns, c):
        yield "/nonexistent"
    monkeypatch.setattr(cli.ca, "session", session)
    assert cli.main(["manifest-remove", "--kubeconfig", "/x", "--name", f"{NS}/{CL}/gitops-demo"]) == 0
    assert ("delete", mf.K_CRS, "art-gitops-demo", "orphan") in mgmt.calls
    assert ("delete", "secrets", "art-gitops-demo", None) in mgmt.calls
    assert set(child.deleted) == {"hops-gitops-demo", "hops-gitops-demo-pull"}


def test_a_secret_that_left_the_content_is_pruned(cli, world, monkeypatch):
    mgmt = MgmtS({(cs.K_CLUSTER, NS, CL): cluster()})
    child = Child([])
    monkeypatch.setattr(cli, "Kube", lambda path, **kw: child)
    import contextlib

    @contextlib.contextmanager
    def session(k, ns, c):
        yield "/nonexistent"
    monkeypatch.setattr(cli.ca, "session", session)
    assert cli.deliver(mgmt, NS, CL, "gitops-demo", "banc", "1.0.0", [SA], {}, "", 30, secrets=[SEC]) == 0
    stamped = mf.stamp([SEC], "gitops-demo", CL)[0]
    child.objs[("Secret", "hops-gitops", "hops-gitops-demo-pull")] = stamped
    assert cli.deliver(mgmt, NS, CL, "gitops-demo", "banc", "1.0.1", [SA], {}, "", 30) == 0
    assert child.deleted == ["hops-gitops-demo-pull"]
    assert ("secrets", NS, "art-gitops-demo") not in mgmt.objs
