"""v1.92.0 : bin/harvester-gpus.py list, le même inventaire en ligne de commande."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "tests" / "api"))
spec = importlib.util.spec_from_file_location("harvester_gpus", ROOT / "bin" / "harvester-gpus.py")
gcli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gcli)
from kube import KubeError  # noqa: E402
from test_gpu_model_192 import claim, node, pci, pod, vm  # noqa: E402

G = "n1-00003b000"
ITEMS = {
    "addons.harvesterhci.io": [{"metadata": {"namespace": "harvester-system", "name": "pcidevices-controller"},
                                "spec": {"enabled": True}}],
    "pcidevices.devices.harvesterhci.io": [pci("n1", "0000:3b:00.0")],
    "pcideviceclaims.devices.harvesterhci.io": [claim(G)],
    "nodes": [node("n2", alloc={"nvidia.com/gpu": "4"})],
    "pods": [pod("ml", "trainer", "n2", {"nvidia.com/gpu": "2"})],
    "virtualmachines.kubevirt.io": [vm("prod", "train", host=[(G, "x")])],
}


class FakeKube:
    """Rend une liste par ressource ; `fail` : {ressource: message d'erreur}."""

    def __init__(self, items=None, fail=None):
        self.items, self.fail, self.calls = ITEMS if items is None else items, fail or {}, []

    def run(self, *args, **kw):
        self.calls.append(args)
        res = args[1]
        if res in self.fail:
            raise KubeError(self.fail[res])
        return json.dumps({"items": self.items.get(res, [])})


def test_read_cluster_reads_every_object_and_keeps_the_model():
    k = FakeKube()
    inv, denials = gcli.read_cluster(k, "c1")
    assert [(u["type"], u["state"]) for u in inv["units"]] == [("gpu", "used"), ("node", "free")]
    assert denials == [] and len(k.calls) == len(gcli.READS)
    assert all(c[0] == "get" and c[-2:] == ("-o", "json") for c in k.calls)


@pytest.mark.parametrize("fail,absent_type", [
    ({"vgpudevices.devices.harvesterhci.io": 'error: the server doesn\'t have a resource type "vgpudevices"'}, "vgpu"),
    ({"migconfigurations.devices.harvesterhci.io": "the server could not find the requested resource"}, "mig"),
])
def test_an_absent_crd_is_not_available_not_an_error(fail, absent_type):
    inv, denials = gcli.read_cluster(FakeKube(fail=fail))
    assert inv["available"][absent_type] is False and denials == []


def test_partial_refusal_total_refusal_and_other_errors():
    inv, denials = gcli.read_cluster(FakeKube(fail={"pods": "pods is forbidden"}))
    assert denials == ["pods is forbidden"] and inv["units"][1]["used"] == 0
    everything = {args[1]: f"{args[1]} is forbidden" for _k, args in gcli.READS}
    with pytest.raises(KubeError, match="forbidden"):
        gcli.read_cluster(FakeKube(fail=everything))
    with pytest.raises(KubeError, match="boom"):
        gcli.read_cluster(FakeKube(fail={"nodes": "boom"}))
    # une CRD absente n'excuse pas une ressource de base absente
    with pytest.raises(KubeError):
        gcli.read_cluster(FakeKube(fail={"nodes": "the server doesn't have a resource type \"nodes\""}))


def test_collect_says_each_cluster_state():
    off = {"items": {"addons.harvesterhci.io": [{"metadata": {"namespace": "harvester-system",
                                                              "name": "pcidevices-controller"}, "spec": {"enabled": False}}]}}
    targets = [("a", FakeKube()), ("b", FakeKube()), ("c", FakeKube(fail={"nodes": "nodes is forbidden",
                                                                          **{a[1]: f"{a[1]} is forbidden" for _k, a in gcli.READS}})),
               ("d", FakeKube(**off)), ("e", FakeKube(fail={"nodes": "something odd"}))]
    clusters, units = gcli.collect(targets, probe=lambda k: k is not targets[1][1])
    st = {c["cluster"]: c["state"] for c in clusters}
    assert st == {"a": "ok", "b": "unreachable", "c": "denied", "d": "no-addon", "e": "error"}
    assert {u["cluster"] for u in units} == {"a"}


def test_table_is_readable_and_says_what_was_not_read():
    inv, _ = gcli.read_cluster(FakeKube(), "a")
    clusters = [{"cluster": "a", "state": "ok", "available": dict(inv["available"], mig=False)},
                {"cluster": "b", "state": "unreachable"}, {"cluster": "d", "state": "no-addon"}]
    out = gcli.table(clusters, inv["units"])
    lines = out.splitlines()
    assert lines[0].startswith("gpu: 1/1 used, 0 free") and "node: 2/4 used, 2 free" in lines[0]
    assert lines[2].split() == ["CLUSTER", "NODE", "TYPE", "NAME", "MODEL", "PARENT", "STATE", "USED", "CONSUMERS"]
    assert lines[3].split()[:4] == ["a", "n1", "gpu", G] and lines[3].endswith("vm:prod/train")
    assert lines[4].endswith("pod:ml/trainer x2")
    assert "cluster a: not available: mig" in out and "cluster b: unreachable" in out
    assert "cluster d: GPU devices add-on" in out
    assert "(no GPU)" in gcli.table([], [])


def test_main_json_with_a_kubeconfig(monkeypatch, capsys):
    fake = FakeKube()
    monkeypatch.setattr(gcli._hcl, "Kube", lambda kc, timeout=30: fake)
    monkeypatch.setattr(gcli, "reachable", lambda kube, timeout=2.0: None)
    rc = gcli.main(["list", "--kubeconfig", "/nope/kc", "--json"])
    d = json.loads(capsys.readouterr().out)
    assert rc == 0 and d["clusters"][0]["state"] == "ok" and d["clusters"][0]["count"] == 2
    assert d["summary"]["gpu"] == {"total": 1, "used": 1, "free": 0}
    assert {u["cluster"] for u in d["units"]} == {"cluster"}


def test_main_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(gcli._hcl, "Kube", lambda kc, timeout=30: FakeKube())
    monkeypatch.setattr(gcli, "reachable", lambda kube, timeout=2.0: False)
    assert gcli.main(["list", "--kubeconfig", "/nope/kc"]) == 1            # cluster non lu
    assert "unreachable" in capsys.readouterr().out
    monkeypatch.setattr(gcli._hcl, "declared_clusters", lambda: [])
    assert gcli.main(["list"]) == 2                                         # aucun cluster
    assert "no cluster declared" in capsys.readouterr().err
