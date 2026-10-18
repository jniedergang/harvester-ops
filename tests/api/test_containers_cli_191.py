"""v1.91.0 : bin/harvester-containers.py list, la même lecture en ligne de commande."""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
spec = importlib.util.spec_from_file_location("harvester_containers", ROOT / "bin" / "harvester-containers.py")
hcli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hcli)
from kube import KubeError  # noqa: E402

DEPLOY = {"kind": "Deployment", "metadata": {"namespace": "apps", "name": "web",
                                             "creationTimestamp": "2026-10-16T12:00:00Z"},
          "spec": {"replicas": 2, "template": {"spec": {"containers": [
              {"name": "w", "image": "nginx:1", "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}}},
          "status": {"readyReplicas": 1, "updatedReplicas": 2, "observedGeneration": 1}}
VMPOD = {"kind": "Pod", "metadata": {"namespace": "apps", "name": "virt-launcher-x",
                                     "labels": {"kubevirt.io": "virt-launcher"}}, "spec": {}, "status": {}}


class FakeKube:
    """Rend une liste par ressource ; `fail` : {ressource: message d'erreur}."""

    def __init__(self, items=(), fail=None):
        self.items, self.fail, self.calls = list(items), fail or {}, []

    def run(self, *args, **kw):
        self.calls.append(args)
        res = args[1]
        if res in self.fail:
            raise KubeError(self.fail[res])
        kind = dict(hcli.hc.WORKLOAD_RESOURCES)[res]
        return json.dumps({"items": [i for i in self.items if i["kind"] == kind]})


def test_read_cluster_reads_each_type_and_keeps_the_model():
    k = FakeKube([DEPLOY, VMPOD])
    rows, denials = hcli.read_cluster(k)
    assert [(w["kind"], w["name"], w["state"], w["gpus"]) for w in rows] == [("Deployment", "web", "Degraded", 1)]
    assert denials == [] and sorted(c[1] for c in k.calls) == sorted(r for r, _ in hcli.hc.WORKLOAD_RESOURCES)


def test_read_cluster_partial_refusal_total_refusal_and_other_errors():
    rows, denials = hcli.read_cluster(FakeKube([DEPLOY], fail={"pods": "pods is forbidden"}))
    assert [w["name"] for w in rows] == ["web"] and denials == ["pods is forbidden"]
    everything = {r: f"{r} is forbidden" for r, _ in hcli.hc.WORKLOAD_RESOURCES}
    with pytest.raises(KubeError, match="forbidden"):
        hcli.read_cluster(FakeKube(fail=everything))
    with pytest.raises(KubeError, match="boom"):
        hcli.read_cluster(FakeKube([DEPLOY], fail={"jobs.batch": "boom"}))


def test_collect_says_each_cluster_state():
    targets = [("a", FakeKube([DEPLOY])), ("b", FakeKube()), ("c", FakeKube()), ("d", FakeKube()),
               ("e", FakeKube())]
    msgs = {"c": "secrets is forbidden", "d": "E1018 noise\ndial tcp 1.2.3.4:6443: connect: no route to host",
            "e": "something odd"}

    def reader(kube):
        name = next(n for n, k in targets if k is kube)
        if name in msgs:
            raise KubeError(msgs[name])
        return hcli.read_cluster(kube)
    clusters, rows = hcli.collect(targets, reader=reader, probe=lambda k: False if k is targets[1][1] else True)
    st = {c["cluster"]: c for c in clusters}
    assert {n: c["state"] for n, c in st.items()} == {
        "a": "ok", "b": "unreachable", "c": "denied", "d": "unreachable", "e": "error"}
    assert st["d"]["error"] == "dial tcp 1.2.3.4:6443: connect: no route to host"     # dernière ligne seule
    assert [(w["cluster"], w["name"]) for w in rows] == [("a", "web")]


def test_table_is_readable_and_says_what_was_not_read():
    clusters = [{"cluster": "a", "state": "ok", "count": 1},
                {"cluster": "b", "state": "unreachable"},
                {"cluster": "c", "state": "ok", "count": 0, "denials": ["pods is forbidden"]}]
    rows = [dict(hcli.hc.build({"Deployment": [DEPLOY]})[0], cluster="a")]
    out = hcli.table(clusters, rows, now=datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc))
    lines = out.splitlines()
    assert lines[0].split() == ["CLUSTER", "NAMESPACE", "KIND", "NAME", "READY", "STATE", "GPU", "AGE", "IMAGES"]
    assert lines[1].split() == ["a", "apps", "Deployment", "web", "1/2", "Degraded", "1", "2d", "nginx:1"]
    assert "cluster b: unreachable" in out and "cluster c: partly read" in out
    assert "no container workload" in hcli.table([], [])


def test_main_json_with_a_kubeconfig(monkeypatch, capsys):
    fake = FakeKube([DEPLOY])
    monkeypatch.setattr(hcli, "Kube", lambda kc, timeout=30: fake)
    monkeypatch.setattr(hcli, "reachable", lambda kube, timeout=2.0: None)
    rc = hcli.main(["list", "--kubeconfig", "/nope/kc", "--json"])
    d = json.loads(capsys.readouterr().out)
    assert rc == 0 and d["clusters"] == [{"cluster": "cluster", "state": "ok", "count": 1}]
    assert d["workloads"][0]["cluster"] == "cluster" and d["workloads"][0]["name"] == "web"


def test_main_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(hcli, "Kube", lambda kc, timeout=30: FakeKube())
    monkeypatch.setattr(hcli, "reachable", lambda kube, timeout=2.0: False)
    assert hcli.main(["list", "--kubeconfig", "/nope/kc"]) == 1            # cluster non lu
    assert "unreachable" in capsys.readouterr().out
    monkeypatch.setattr(hcli, "declared_clusters", lambda: [])
    assert hcli.main(["list"]) == 2                                         # aucun cluster
    assert "no cluster declared" in capsys.readouterr().err


def test_declared_clusters_reads_config_then_console_declarations(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("clusters:\n  - name: one\n    kubeconfig: /k/one\n")
    state = tmp_path / "state"
    (state / "clusters.d").mkdir(parents=True)
    (state / "clusters.d" / "two.yaml").write_text("name: two\nkubeconfig: kc/two\n")
    monkeypatch.setenv("HARVESTER_OPS_CONFIG", str(cfg))
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    got = hcli.declared_clusters()
    assert [c["name"] for c in got] == ["one", "two"]
    assert got[1]["kubeconfig"] == str(state / "kc" / "two")
