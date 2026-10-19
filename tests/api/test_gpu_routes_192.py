"""v1.92.0 : /api/gpus/<cluster> et /api/gpus-all (lecture seule).

kubectl est simulé. Une CRD absente (add-on coupé, vGPU ou MIG non
installés) est dite « non disponible », jamais une erreur ; un cluster
éteint, qui refuse la lecture ou qui répond mal est dit, jamais une liste
vide muette."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "tests" / "api"))
import app as wapp  # noqa: E402
from test_gpu_model_192 import claim, node, pci, pod, vm, vmi  # noqa: E402

CLUSTERS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
ABSENT = 'error: the server doesn\'t have a resource type "{}"'


def denied(res):
    return (f'Error from server (Forbidden): {res} is forbidden: User "u" cannot list resource '
            f'"{res.split(".")[0]}" in API group "x" at the cluster scope')


class Done:
    def __init__(self, rc=0, out=None, err=""):
        self.returncode, self.stderr = rc, err
        self.stdout = out if isinstance(out, str) else (json.dumps(out) if out is not None else "")


def addon(enabled):
    return {"metadata": {"namespace": "harvester-system", "name": "pcidevices-controller"}, "spec": {"enabled": enabled}}


G = "n1-00003b000"
ALPHA = {
    "addons.harvesterhci.io": [addon(True)],
    "pcidevices.devices.harvesterhci.io": [pci("n1", "0000:3b:00.0"), pci("n1", "0000:00:02.0", cls="0300", vendor="8086")],
    "pcideviceclaims.devices.harvesterhci.io": [claim(G)],
    "nodes": [node("n2", alloc={"nvidia.com/gpu": "2"})],
    "pods": [pod("ml", "trainer", "n2", {"nvidia.com/gpu": "2"})],
    "virtualmachines.kubevirt.io": [vm("prod", "train", host=[(G, "nvidia.com/GA107GL_A2")])],
    "virtualmachineinstances.kubevirt.io": [vmi("prod", "train")],
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    kcs = {n: tmp_path / f"{n}.yaml" for n in CLUSTERS}
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")      # absent : mode ouvert
    monkeypatch.setattr(wapp, "load_config", lambda: {
        "clusters": [{"name": n, "kubeconfig": str(p)} for n, p in kcs.items()]})
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: "bravo" not in str(kc))
    calls = []

    def run(argv, *a, **k):
        calls.append(argv)
        kc = argv[argv.index("--kubeconfig") + 1]
        name = next(n for n in kcs if n in kc)
        res = argv[4]
        if name == "charlie":                                  # tout refusé
            return Done(1, err=denied(res))
        if name == "delta":                                    # add-on coupé : CRD absentes
            if res.endswith(".devices.harvesterhci.io"):
                return Done(1, err=ABSENT.format(res))
            if res == "addons.harvesterhci.io":
                return Done(0, {"items": [addon(False)]})
            return Done(0, {"items": []})
        if name == "echo" and res == "pods":                   # erreur qui n'est pas un refus
            return Done(1, err=f"error: stat {kc}: no such file or directory")
        if name == "foxtrot":                                  # vGPU/MIG absents, pods refusés
            if res in ("vgpudevices.devices.harvesterhci.io", "migconfigurations.devices.harvesterhci.io"):
                return Done(1, err="Error from server (NotFound): the server could not find the requested resource")
            if res == "pods":
                return Done(1, err=denied(res))
            if res == "addons.harvesterhci.io":
                return Done(0, {"items": [addon(True)]})
            return Done(0, {"items": []})
        return Done(0, {"items": ALPHA.get(res, [])})
    monkeypatch.setattr(wapp.subprocess, "run", run)
    return calls


def test_one_cluster_inventory(world):
    with wapp.app.test_client() as c:
        r = c.get("/api/gpus/alpha?fresh=1")
    d = r.get_json()
    assert r.status_code == 200 and d["cluster"] == "alpha" and d["addon"] is True and d["denials"] == []
    assert [(u["type"], u["id"], u["state"]) for u in d["units"]] == [
        ("gpu", G, "used"), ("node", "n2/nvidia.com/gpu", "used")]
    assert d["units"][0]["consumers"] == [{"kind": "vm", "cluster": "alpha", "namespace": "prod", "name": "train"}]
    assert d["units"][1]["consumers"][0]["kind"] == "pod"
    assert d["summary"]["gpu"] == {"total": 1, "used": 1, "free": 0}
    assert d["available"] == {"addon": True, "gpu": True, "vgpu": True, "mig": True, "node": True}
    # chaque objet lu à part, en JSON ; rien n'est écrit
    reads = sorted(a[4] for a in world)
    assert reads == sorted([r for r, _ in wapp._hgpu.DEVICE_RESOURCES] + [
        "addons.harvesterhci.io", "nodes", "pods", "virtualmachines.kubevirt.io",
        "virtualmachineinstances.kubevirt.io"])
    assert all(a[3] == "get" and a[-2:] == ["-o", "json"] for a in world)


def test_addon_off_is_said_not_an_error(world):
    with wapp.app.test_client() as c:
        r = c.get("/api/gpus/delta?fresh=1")
    d = r.get_json()
    assert r.status_code == 200 and d["addon"] is False and d["units"] == []
    assert d["available"]["gpu"] is False and d["available"]["vgpu"] is False


def test_missing_vgpu_and_mig_crds_and_a_partial_read(world):
    with wapp.app.test_client() as c:
        r = c.get("/api/gpus/foxtrot?fresh=1")
    d = r.get_json()
    assert r.status_code == 200
    assert (d["available"]["vgpu"], d["available"]["mig"], d["available"]["gpu"]) == (False, False, True)
    assert len(d["denials"]) == 1 and "pods" in d["denials"][0]
    assert "X-Cluster-Denied" in r.headers


def test_one_cluster_off_unknown_or_refused_is_said(world):
    with wapp.app.test_client() as c:
        off = c.get("/api/gpus/bravo?fresh=1")
        assert off.status_code == 200 and off.get_json()["unreachable"] is True
        assert c.get("/api/gpus/nope?fresh=1").status_code == 404
        no = c.get("/api/gpus/charlie?fresh=1")
        assert no.status_code == 403
        assert json.loads(no.headers["X-Cluster-Denied"])[0]["verb"] == "list"


def test_an_error_that_is_not_a_denial_fails_and_never_shows_the_kubeconfig(world, tmp_path):
    with wapp.app.test_client() as c:
        r = c.get("/api/gpus/echo?fresh=1")
    assert r.status_code == 500
    body = r.get_data(as_text=True)
    assert str(tmp_path) not in body and "<kubeconfig>" in body


def test_all_clusters_with_their_state(world):
    with wapp.app.test_client() as c:
        d = c.get("/api/gpus-all?fresh=1").get_json()
    st = {x["cluster"]: x for x in d["clusters"]}
    assert [x["cluster"] for x in d["clusters"]] == list(CLUSTERS)
    assert (st["alpha"]["state"], st["alpha"]["count"]) == ("ok", 2)
    assert st["bravo"]["state"] == "unreachable"
    assert st["charlie"]["state"] == "denied"
    assert st["delta"]["state"] == "no-addon"
    assert st["echo"]["state"] == "error"
    assert st["foxtrot"]["state"] == "ok" and st["foxtrot"]["denials"]
    assert [(u["cluster"], u["type"]) for u in d["units"]] == [("alpha", "gpu"), ("alpha", "node")]
    assert d["summary"]["node"] == {"total": 2, "used": 2, "free": 0}
    # v1.94.1 : les lots réservables, même calcul que le formulaire de réservation
    assert d["pools"] == [dict(cluster="alpha", type=e["type"], model=e["model"], capacity=e["capacity"],
                               used=e["used"]) for e in wapp._hbk.catalog(d["units"])]
    assert {(p["type"], p["capacity"]) for p in d["pools"]} == {("gpu", 1), ("node", 2)}


def test_a_cluster_that_crashes_does_not_take_the_view_down(world, monkeypatch):
    real = wapp._gpus_collect

    def boom(name, kc):
        if name == "alpha":
            raise RuntimeError("bad answer")
        return real(name, kc)
    monkeypatch.setattr(wapp, "_gpus_collect", boom)
    with wapp.app.test_client() as c:
        d = c.get("/api/gpus-all?fresh=1").get_json()
    assert {x["cluster"]: x["state"] for x in d["clusters"]}["alpha"] == "error"
    assert d["units"] == []


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_no_write_route(world, method):
    with wapp.app.test_client() as c:
        assert getattr(c, method)("/api/gpus/alpha").status_code == 405
        assert getattr(c, method)("/api/gpus-all").status_code == 405
