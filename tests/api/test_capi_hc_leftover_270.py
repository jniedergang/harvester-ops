"""2.7.0 (m7, décision 216) : HarvesterCluster resté d'un cluster supprimé.

Essai 7 de la 2.7.0 : un HarvesterCluster sans propriétaire n'a aucun
finaliseur et la topologie l'adopte (simple avertissement) ; l'échec vient
d'un reste qui pointe encore vers l'ancien `Cluster` (autre uid, ou absent),
ou retenu en suppression par un finaliseur : `check` le dit bloquant, et
`harvester-capi delete` le supprime, attend 120 s, puis nomme le finaliseur
qui le retient. La console ne retire jamais un finaliseur.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import capi_cluster as cc  # noqa: E402

REF = "web/web"


def hc(owners=(), deleting=False, finalizers=()):
    md = {"name": "web", "namespace": "web",
          "ownerReferences": [{"kind": "Cluster", "name": n, "uid": u} for n, u in owners]
          + [{"kind": "Secret", "name": "x", "uid": "s"}]}
    if deleting:
        md["deletionTimestamp"] = "2026-11-07T10:00:00Z"
    if finalizers:
        md["finalizers"] = list(finalizers)
    return {"metadata": md}


@pytest.mark.parametrize("obj,uids,code,level", [
    (None, {}, None, None),
    (hc(owners=[("web", "u-old")]), {}, "harvester-cluster-leftover", "block"),             # Cluster parti
    (hc(owners=[("web", "u-old")]), {REF: "u-new"}, "harvester-cluster-leftover", "block"),  # autre uid
    (hc(deleting=True, finalizers=["harvestercluster.infrastructure.cluster.x-k8s.io"]), {},
     "harvester-cluster-leftover", "block"),                                                # retenu
    (hc(), {}, "harvester-cluster-orphan", "warn"),                                         # adopté
    (hc(deleting=True), {}, "harvester-cluster-orphan", "warn"),                            # part seul
    (hc(owners=[("web", "u-1")]), {REF: "u-1"}, None, None),                                # vivant
])
def test_leftover_rules(obj, uids, code, level):
    facts = {"harvester_clusters": {REF: cc.harvester_cluster_brief(obj)} if obj else {}, "cluster_uids": uids}
    got = cc.harvester_cluster_findings(REF, facts)
    assert [(f["code"], f["level"]) for f in got] == ([(code, level)] if code else [])


def test_the_leftover_names_its_finalizers_and_blocks_check():
    spec = cc.normalize({"namespace": "web", "name": "web", "image": "default/img", "network": "default/net",
                         "ssh_keypair": "default/key", "ip_pool": "pool"})
    facts = {"harvester_clusters": {REF: cc.harvester_cluster_brief(
        hc(owners=[("web", "u-old")], deleting=True, finalizers=["a.io/f", "b.io/g"]))}}
    got = [f for f in cc.check(spec, facts) if f["code"].startswith("harvester-cluster")]
    assert got == [{"code": "harvester-cluster-leftover", "level": "block",
                    "facts": {"name": REF, "finalizers": "a.io/f, b.io/g"}}]
    assert got[0] in cc.blocking(cc.check(spec, facts))
    other = {"harvester_clusters": {"web/autre": facts["harvester_clusters"][REF]}}
    assert not [f for f in cc.check(spec, other) if f["code"].startswith("harvester-cluster")]


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_capi_hc", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_facts_carry_the_harvester_clusters_of_the_inventory(cli):
    inv = {"stack": {}, "namespaces": [], "clusters": [], "images": [], "keypairs": [], "networks": [],
           "storage_classes": [], "pools": [], "versions": [], "version_names": [], "free_cpu_m": None,
           "free_memory": None, "overcommit": {}, "cluster_uids": {REF: "u"},
           "harvester_clusters": {REF: {"owners": [], "deleting": False, "finalizers": []}}}
    f = cli.facts_from(inv)
    assert f["cluster_uids"] == {REF: "u"} and REF in f["harvester_clusters"]


class Kube:
    """Un HarvesterCluster qui garde son finaliseur (`stuck`), ou part."""

    def __init__(self, cli, stuck, cluster=False, sa=False):
        self.cli, self.stuck, self.deleted, self.patched = cli, stuck, [], []
        self.hc = hc(owners=[("web", "u-old")], finalizers=["harvestercluster.infrastructure.cluster.x-k8s.io"])
        self.cluster = cluster

    def get(self, kind, ns, name):
        if kind == cc.K_HARVESTERCLUSTER and (ns, name) == ("web", "web"):
            return json.loads(json.dumps(self.hc)) if self.hc else None
        if kind == self.cli.cs.K_CLUSTER and self.cluster:
            return {"metadata": {"name": name, "namespace": ns}}
        return None

    def list(self, kind, ns=None, selector=None):
        return []

    def delete(self, kind, ns, name, **kw):
        self.deleted.append((kind, ns, name))
        if kind == cc.K_HARVESTERCLUSTER:
            if self.stuck:
                self.hc["metadata"]["deletionTimestamp"] = "2026-11-07T10:00:00Z"
            else:
                self.hc = None

    def patch(self, *a, **kw):
        self.patched.append(a)

    def apply(self, *a, **kw):
        self.patched.append(a)


def run_remove(cli, kube, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "step", lambda sid, status, msg="": seen.append((sid, status, msg)))
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s
    rc = cli.remove_cluster(kube, "web", "web", 2400, sleep=sleep, now=lambda: t["now"])
    return rc, seen, t["now"]


def test_delete_with_a_persistent_finalizer_names_it_and_leaves_it(cli, monkeypatch):
    k = Kube(cli, stuck=True)
    rc, seen, elapsed = run_remove(cli, k, monkeypatch)
    assert rc == cli.EXIT_FAIL
    assert seen[-1][:2] == ("delete", "error")
    assert "harvestercluster.infrastructure.cluster.x-k8s.io" in seen[-1][2]
    assert "never removes a finalizer" in seen[-1][2]
    assert k.hc["metadata"]["finalizers"] == ["harvestercluster.infrastructure.cluster.x-k8s.io"]
    assert k.patched == [] and cli.HC_LEFTOVER_TIMEOUT <= elapsed <= cli.HC_LEFTOVER_TIMEOUT + 10
    assert k.deleted == [(cc.K_HARVESTERCLUSTER, "web", "web")]          # rien d'autre touché


def test_delete_removes_a_leftover_then_finishes(cli, monkeypatch):
    k = Kube(cli, stuck=False)
    rc, seen, _ = run_remove(cli, k, monkeypatch)
    assert rc == cli.EXIT_OK and k.hc is None
    assert ("delete", "running", "HarvesterCluster web/web removed") in seen
    assert seen[-1] == ("delete", "done", "web/web deleted")


def test_a_live_cluster_keeps_its_harvestercluster(cli, monkeypatch):
    k = Kube(cli, stuck=False, cluster=True)
    monkeypatch.setattr(cli, "step", lambda *a: None)
    # le Cluster ne part jamais ici : on s'arrête au délai, le HarvesterCluster est intact
    t = {"now": 0.0}
    rc = cli.remove_cluster(k, "web", "web", 30, sleep=lambda s: t.__setitem__("now", t["now"] + s),
                            now=lambda: t["now"])
    assert rc == cli.EXIT_FAIL and (cc.K_HARVESTERCLUSTER, "web", "web") not in k.deleted


def test_cmd_delete_resumes_on_a_leftover_alone(cli, monkeypatch):
    k = Kube(cli, stuck=False)
    monkeypatch.setattr(cli, "kube_from", lambda args: (k, "kc", None))
    monkeypatch.setattr(cli, "step", lambda *a: None)
    assert cli.main(["delete", "--kubeconfig", "kc", "--name", "web/web"]) == cli.EXIT_OK
    assert k.hc is None
    k2 = Kube(cli, stuck=False)
    k2.hc = None
    monkeypatch.setattr(cli, "kube_from", lambda args: (k2, "kc", None))
    assert cli.main(["delete", "--kubeconfig", "kc", "--name", "web/web"]) == cli.EXIT_FAIL    # rien à retirer
