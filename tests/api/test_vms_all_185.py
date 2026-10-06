"""v1.85.0 : toutes les VMs de tous les clusters, en une liste.

Chaque cluster est lu en parallèle avec l'identité de la personne ; un
cluster éteint ou qui refuse la lecture est dit, sans retenir les autres.
La liste d'un seul cluster (/api/vms/<cluster>) garde sa forme."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402

DENIED = ('Error from server (Forbidden): virtualmachines.kubevirt.io is forbidden: User "u" '
          'cannot list resource "virtualmachines" in API group "kubevirt.io" at the cluster scope')


def vm(ns, name, rs="Always"):
    return {"metadata": {"namespace": ns, "name": name, "annotations": {}, "labels": {}},
            "spec": {"runStrategy": rs, "template": {"spec": {"domain": {
                "cpu": {"cores": 2}, "memory": {"guest": "4Gi"}}}}}}


def vmi(ns, name, ip):
    return {"metadata": {"namespace": ns, "name": name},
            "status": {"phase": "Running", "nodeName": "n1", "conditions": [],
                       "interfaces": [{"ipAddress": ip}]}}


class Done:
    def __init__(self, rc=0, out=None, err=""):
        self.returncode, self.stdout, self.stderr = rc, json.dumps(out) if out is not None else "", err


@pytest.fixture
def world(tmp_path, monkeypatch):
    kcs = {n: tmp_path / f"{n}.yaml" for n in ("alpha", "bravo", "charlie")}
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")      # absent : mode ouvert
    monkeypatch.setattr(wapp, "load_config", lambda: {
        "clusters": [{"name": n, "kubeconfig": str(p)} for n, p in kcs.items()]})
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: "bravo" not in str(kc))
    data = {
        "alpha": ({"items": [vm("default", "web-2"), vm("prod", "db", "Halted")]},
                  {"items": [vmi("default", "web-2", "10.0.0.5")]}),
        "charlie": None,
    }

    def run(argv, *a, **k):
        kc = argv[argv.index("--kubeconfig") + 1]
        name = next(n for n in kcs if n in kc)
        if data.get(name) is None:
            return Done(1, err=DENIED)
        vms, vmis = data[name]
        return Done(0, vmis if "vmi" in argv else vms)
    monkeypatch.setattr(wapp.subprocess, "run", run)
    return data


def test_every_cluster_in_one_list_with_its_state(world):
    with wapp.app.test_client() as c:
        d = c.get("/api/vms-all").get_json()
    assert [x["cluster"] for x in d["clusters"]] == ["alpha", "bravo", "charlie"]
    st = {x["cluster"]: x for x in d["clusters"]}
    assert st["alpha"]["state"] == "ok" and st["alpha"]["count"] == 2
    assert st["bravo"]["state"] == "unreachable"
    assert st["charlie"]["state"] == "denied"
    rows = [(v["cluster"], v["namespace"], v["name"], v["phase"]) for v in d["vms"]]
    assert rows == [("alpha", "prod", "db", "Stopped"), ("alpha", "default", "web-2", "Running")]
    web = d["vms"][1]
    assert web["ips"] == ["10.0.0.5"] and web["node"] == "n1" and web["cpu"] == 2 and web["memory"] == "4Gi"


def test_the_single_cluster_list_keeps_its_shape(world):
    with wapp.app.test_client() as c:
        d = c.get("/api/vms/alpha").get_json()
        assert d["cluster"] == "alpha" and {v["name"] for v in d["vms"]} == {"web-2", "db"}
        assert "cluster" not in d["vms"][0]
        r = c.get("/api/vms/charlie")
        assert r.status_code == 403          # refus de la RBAC, dit comme tel
        assert json.loads(r.headers["X-Cluster-Denied"])[0]["resource"] == "virtualmachines"


def test_a_cluster_that_crashes_does_not_take_the_view_down(world, monkeypatch):
    real = wapp._vms_collect

    def boom(name, kc):
        if name == "alpha":
            raise RuntimeError("bad answer")
        return real(name, kc)
    monkeypatch.setattr(wapp, "_vms_collect", boom)
    with wapp.app.test_client() as c:
        d = c.get("/api/vms-all").get_json()
    assert {x["cluster"]: x["state"] for x in d["clusters"]} == {
        "alpha": "error", "bravo": "unreachable", "charlie": "denied"}
    assert d["vms"] == []
