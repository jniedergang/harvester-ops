"""v1.91.0 : les routes de la section Containers (lecture seule).

/api/containers/<cluster>, /api/containers-all, le journal d'un pod et le YAML
des charges par la route générique. kubectl est simulé ; un cluster éteint,
qui refuse la lecture ou qui répond mal est dit, jamais une liste vide muette."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


def denied(res):
    return (f'Error from server (Forbidden): {res} is forbidden: User "u" cannot list resource '
            f'"{res.split(".")[0]}" in API group "apps" at the cluster scope')


class Done:
    def __init__(self, rc=0, out=None, err=""):
        self.returncode, self.stderr = rc, err
        self.stdout = out if isinstance(out, str) else (json.dumps(out) if out is not None else "")


def deploy(ns, name):
    return {"kind": "Deployment", "metadata": {"namespace": ns, "name": name,
                                               "creationTimestamp": "2026-10-10T10:00:00Z"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "web", "image": "nginx:1",
                 "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}}},
            "status": {"readyReplicas": 1, "updatedReplicas": 1, "observedGeneration": 1}}


def pod(ns, name, owner=None, labels=None):
    m = {"namespace": ns, "name": name, "labels": labels or {}}
    if owner:
        m["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": owner, "controller": True}]
    return {"kind": "Pod", "metadata": m, "spec": {"nodeName": "n1", "containers": [{"name": "web", "image": "nginx:1"}]},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}


def rs(ns, name, owner):
    return {"kind": "ReplicaSet", "metadata": {"namespace": ns, "name": name, "ownerReferences": [
        {"apiVersion": "apps/v1", "kind": "Deployment", "name": owner, "controller": True}]},
        "spec": {"replicas": 1}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    kcs = {n: tmp_path / f"{n}.yaml" for n in ("alpha", "bravo", "charlie", "delta", "echo")}
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")      # absent : mode ouvert
    monkeypatch.setattr(wapp, "load_config", lambda: {
        "clusters": [{"name": n, "kubeconfig": str(p)} for n, p in kcs.items()]})
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: "bravo" not in str(kc))
    alpha = [deploy("apps", "web"), rs("apps", "web-1a", "web"), pod("apps", "web-1a-x", "web-1a"),
             deploy("cattle-system", "rancher"),
             pod("apps", "virt-launcher-vm1-x", labels={"kubevirt.io": "virt-launcher"}),
             pod("default", "lonely")]
    calls = []

    def run(argv, *a, **k):
        calls.append(argv)
        kc = argv[argv.index("--kubeconfig") + 1]
        name = next(n for n in kcs if n in kc)
        if argv[3] == "logs":
            if "missing" in argv:
                return Done(1, err='Error from server (NotFound): pods "missing" not found')
            return Done(0, "line 1\nline 2\n")
        res = argv[4]
        if name == "charlie":                     # tout refusé
            return Done(1, err=denied(res))
        if name == "delta" and res == "deployments.apps":   # un type refusé : lu en partie
            return Done(1, err=denied(res))
        if name == "echo" and res == "pods":       # erreur qui n'est pas un refus
            return Done(1, err=f"error: stat {kc}: no such file or directory")
        kind = {"deployments.apps": "Deployment", "statefulsets.apps": "StatefulSet", "daemonsets.apps": "DaemonSet",
                "jobs.batch": "Job", "cronjobs.batch": "CronJob", "replicasets.apps": "ReplicaSet",
                "pods": "Pod"}[res]
        src = alpha if name == "alpha" else [deploy("lab", "api"), pod("lab", "solo")]
        return Done(0, {"items": [o for o in src if o["kind"] == kind]})
    monkeypatch.setattr(wapp.subprocess, "run", run)
    return calls


def test_one_cluster_lists_its_workloads_without_system_namespaces_nor_vm_pods(world):
    with wapp.app.test_client() as c:
        r = c.get("/api/containers/alpha?fresh=1")
    d = r.get_json()
    assert r.status_code == 200 and d["cluster"] == "alpha" and d["denials"] == []
    assert [(w["kind"], w["namespace"], w["name"]) for w in d["workloads"]] == [
        ("Deployment", "apps", "web"), ("Pod", "default", "lonely")]
    web = d["workloads"][0]
    assert [p["name"] for p in web["pods"]] == ["web-1a-x"] and web["gpus"] == 1 and web["state"] == "Running"
    # chaque type lu à part, sur tous les namespaces
    reads = sorted(a[4] for a in world if a[3] == "get")
    assert reads == sorted(r for r, _ in wapp._hct.WORKLOAD_RESOURCES)
    assert all("-A" in a for a in world if a[3] == "get")


def test_one_cluster_off_unknown_or_refused_is_said(world):
    with wapp.app.test_client() as c:
        off = c.get("/api/containers/bravo?fresh=1")
        assert off.status_code == 200 and off.get_json()["unreachable"] is True
        assert c.get("/api/containers/nope?fresh=1").status_code == 404
        no = c.get("/api/containers/charlie?fresh=1")
        assert no.status_code == 403
        assert json.loads(no.headers["X-Cluster-Denied"])[0]["verb"] == "list"


def test_a_partly_refused_read_is_listed_with_its_denials(world):
    with wapp.app.test_client() as c:
        r = c.get("/api/containers/delta?fresh=1")
    d = r.get_json()
    assert r.status_code == 200
    assert [w["name"] for w in d["workloads"]] == ["solo"]          # le Deployment n'a pas été lu
    assert len(d["denials"]) == 1 and "deployments" in d["denials"][0]
    assert "X-Cluster-Denied" in r.headers


def test_an_error_that_is_not_a_denial_fails_and_never_shows_the_kubeconfig(world, tmp_path):
    with wapp.app.test_client() as c:
        r = c.get("/api/containers/echo?fresh=1")
    assert r.status_code == 500
    body = r.get_data(as_text=True)
    assert str(tmp_path) not in body and "<kubeconfig>" in body


def test_all_clusters_in_one_list_with_their_state(world):
    with wapp.app.test_client() as c:
        d = c.get("/api/containers-all?fresh=1").get_json()
    st = {x["cluster"]: x for x in d["clusters"]}
    assert [x["cluster"] for x in d["clusters"]] == ["alpha", "bravo", "charlie", "delta", "echo"]
    assert (st["alpha"]["state"], st["alpha"]["count"]) == ("ok", 2)
    assert st["bravo"]["state"] == "unreachable"
    assert st["charlie"]["state"] == "denied"
    assert st["delta"]["state"] == "ok" and st["delta"]["denials"]
    assert st["echo"]["state"] == "error"
    assert [(w["cluster"], w["name"]) for w in d["workloads"]] == [
        ("alpha", "lonely"), ("delta", "solo"), ("alpha", "web")]


def test_a_cluster_that_crashes_does_not_take_the_view_down(world, monkeypatch):
    real = wapp._containers_collect

    def boom(name, kc):
        if name == "alpha":
            raise RuntimeError("bad answer")
        return real(name, kc)
    monkeypatch.setattr(wapp, "_containers_collect", boom)
    with wapp.app.test_client() as c:
        d = c.get("/api/containers-all?fresh=1").get_json()
    assert {x["cluster"]: x["state"] for x in d["clusters"]}["alpha"] == "error"
    assert {w["cluster"] for w in d["workloads"]} == {"delta"}


@pytest.mark.parametrize("query, tail, container", [
    ("", 500, None), ("?tail=50", 50, None), ("?tail=100000", 500, None), ("?tail=0", 1, None),
    ("?container=web&tail=20", 20, "web"),
])
def test_pod_logs_tail_is_capped_at_500(world, query, tail, container):
    with wapp.app.test_client() as c:
        r = c.get(f"/api/containers/alpha/apps/pod/web-1a-x/logs{query}")
    d = r.get_json()
    assert r.status_code == 200 and d["logs"] == "line 1\nline 2\n" and d["tail"] == tail
    argv = world[-1]
    assert argv[3:7] == ["logs", "web-1a-x", "-n", "apps"] and f"--tail={tail}" in argv
    assert (argv[argv.index("-c") + 1] if "-c" in argv else None) == container


@pytest.mark.parametrize("url, code", [
    ("/api/containers/alpha/apps/pod/web-1a-x/logs?tail=abc", 400),
    ("/api/containers/alpha/apps/pod/web-1a-x/logs?container=Bad_Name", 400),
    ("/api/containers/alpha/Apps/pod/web-1a-x/logs", 400),
    ("/api/containers/alpha/apps/pod/Bad_Pod/logs", 400),
    ("/api/containers/alpha/kube-system/pod/etcd-n1/logs", 400),
    ("/api/containers/alpha/cattle-system/pod/rancher-x/logs", 400),
    ("/api/containers/nope/apps/pod/web-1a-x/logs", 404),
    ("/api/containers/alpha/apps/pod/missing/logs", 404),
    ("/api/containers/bravo/apps/pod/web-1a-x/logs", 503),
])
def test_pod_logs_refusals(world, url, code):
    n = len(world)
    with wapp.app.test_client() as c:
        r = c.get(url)
    assert r.status_code == code
    if code == 400:
        assert len(world) == n                   # rien n'est parti vers le cluster


@pytest.mark.parametrize("kind", ["deployment", "statefulset", "daemonset", "replicaset", "job", "cronjob", "pod"])
def test_workload_yaml_is_read_only(world, monkeypatch, kind):
    obj = {"apiVersion": "apps/v1", "kind": "X", "metadata": {"name": "web", "namespace": "apps",
                                                              "managedFields": [{}]}, "spec": {}, "status": {}}
    monkeypatch.setattr(wapp, "_kubectl_json", lambda kc, *a, **k: dict(obj))
    with wapp.app.test_client() as c:
        r = c.get(f"/api/yaml/alpha/{kind}/web?namespace=apps")
        d = r.get_json()
        assert r.status_code == 200 and d["writable"] is False
        assert "managedFields" not in d["yaml"] and "status" not in d["yaml"]
        w = c.put(f"/api/yaml/alpha/{kind}/web?namespace=apps", json={"yaml": "kind: X"})
        assert w.status_code == 403 and w.get_json()["error"] == "read-only"


def test_workload_yaml_of_a_system_namespace_is_not_read(world, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_json", lambda kc, *a, **k: {
        "kind": "Deployment", "metadata": {"name": "rancher", "namespace": "cattle-system"}})
    with wapp.app.test_client() as c:
        assert c.get("/api/yaml/alpha/deployment/rancher?namespace=cattle-system").status_code == 404
