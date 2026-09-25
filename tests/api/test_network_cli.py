"""v1.49.0 : `harvester-network`, l'écriture des réseaux kube-ovn.

Un cluster en mémoire, peuplé des objets relevés sur harv1 : la création d'un
subnet avec son réseau overlay, l'attente de son état prêt, le retour en
arrière d'une création interrompue, les suppressions refusées.
"""

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
FIX = ROOT / "tests" / "fixtures" / "ovn"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_network", ROOT / "bin" / "harvester-network.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Kube:
    def __init__(self, cli, subnet_ready=True):
        self.cli = cli
        self.objs = {}
        self.deleted, self.applied = [], []
        self.subnet_ready = subnet_ready
        for kind, f in ((cli.K_VPC, "vpcs.json"), (cli.K_SUBNET, "subnets.json"),
                        (cli.K_NAD, "nads.json"), (cli.K_IP, "ips.json")):
            for o in json.loads((FIX / f).read_text()):
                self.objs[(kind, o["metadata"].get("namespace"), o["metadata"]["name"])] = o
        self.objs[("nodes", None, "harv1")] = {"metadata": {"name": "harv1"},
                                               "spec": {"podCIDRs": ["10.52.0.0/24"]},
                                               "status": {"addresses": [{"type": "InternalIP", "address": "172.16.3.11"}]}}
        for ns in ("default", "kube-system", "apps"):
            self.objs[("namespaces", None, ns)] = {"metadata": {"name": ns}}

    def run(self, *a, **k):
        return "{}"

    def list(self, kind, ns=None, selector=None):
        return [json.loads(json.dumps(o)) for (k, n, _), o in self.objs.items()
                if k == kind and (ns is None or n == ns)]

    def get(self, kind, ns, name):
        o = self.objs.get((kind, ns, name))
        return json.loads(json.dumps(o)) if o else None

    KINDS = {"Subnet": "subnets.kubeovn.io", "Vpc": "vpcs.kubeovn.io",
             "NetworkAttachmentDefinition": "network-attachment-definitions.k8s.cni.cncf.io"}

    def apply(self, docs, **kw):
        for d in docs:
            kind = self.KINDS[d["kind"]]
            ns = d["metadata"].get("namespace")
            o = json.loads(json.dumps(d))
            if d["kind"] == "Subnet" and self.subnet_ready:
                o["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
            if d["kind"] == "Vpc":
                o["status"] = {"standby": True}
            self.objs[(kind, ns, d["metadata"]["name"])] = o
            self.applied.append((d["kind"], ns, d["metadata"]["name"]))
        return ""

    def delete(self, kind, ns, name, cascade=None):
        self.deleted.append((kind, ns, name))
        self.objs.pop((kind, ns, name), None)


def run(cli, kube, monkeypatch, cmd, **kw):
    monkeypatch.setattr(cli, "kube_from", lambda a: kube)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return getattr(cli, f"cmd_{cmd}")(argparse.Namespace(cluster=None, kubeconfig=None, **kw))


def spec_file(tmp_path, body):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(body))
    return str(p)


def test_a_subnet_with_its_new_overlay_network(cli, monkeypatch, tmp_path, capsys):
    kube = Kube(cli)
    rc = run(cli, kube, monkeypatch, "apply", kind="subnet", update=False, spec=spec_file(tmp_path, {
        "name": "lab-a", "cidr": "10.200.0.0/24", "new_network": "default/lab-a", "nat": True}))
    assert rc == cli.EXIT_OK
    assert kube.applied == [("NetworkAttachmentDefinition", "default", "lab-a"), ("Subnet", None, "lab-a")]
    sub = kube.get(cli.K_SUBNET, None, "lab-a")
    assert sub["spec"]["provider"] == "lab-a.default.ovn" and sub["spec"]["natOutgoing"]
    err = capsys.readouterr().err
    assert "STEP_EVENT|apply|done|subnet lab-a ready" in err


def test_a_blocked_request_writes_nothing(cli, monkeypatch, tmp_path, capsys):
    kube = Kube(cli)
    rc = run(cli, kube, monkeypatch, "apply", kind="subnet", update=False, spec=spec_file(tmp_path, {
        "name": "lab-a", "cidr": "10.54.1.0/24", "new_network": "default/lab-a"}))
    assert rc == cli.EXIT_BLOCKED and kube.applied == []
    assert "cidr-overlap" in capsys.readouterr().err


def test_a_subnet_that_never_gets_ready_is_undone(cli, monkeypatch, tmp_path):
    kube = Kube(cli, subnet_ready=False)
    monkeypatch.setattr(cli, "_wait", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lab-a not ready")))
    rc = run(cli, kube, monkeypatch, "apply", kind="subnet", update=False, spec=spec_file(tmp_path, {
        "name": "lab-a", "cidr": "10.200.0.0/24", "new_network": "default/lab-a"}))
    assert rc == cli.EXIT_FAIL
    assert (cli.K_SUBNET, None, "lab-a") in kube.deleted
    assert (cli.K_NAD, "default", "lab-a") in kube.deleted


def test_serving_the_existing_orphan_overlay(cli, monkeypatch, tmp_path):
    """`default/ovn-overlay` de harv1 : un subnet lui donne enfin des adresses."""
    kube = Kube(cli)
    rc = run(cli, kube, monkeypatch, "apply", kind="subnet", update=False, spec=spec_file(tmp_path, {
        "name": "overlay-a", "cidr": "10.201.0.0/24", "network": "default/ovn-overlay"}))
    assert rc == cli.EXIT_OK and kube.applied == [("Subnet", None, "overlay-a")]
    assert kube.get(cli.K_SUBNET, None, "overlay-a")["spec"]["provider"] == "ovn-overlay.default.ovn"


def test_a_vpc(cli, monkeypatch, tmp_path):
    kube = Kube(cli)
    rc = run(cli, kube, monkeypatch, "apply", kind="vpc", update=False,
             spec=spec_file(tmp_path, {"name": "lab", "namespaces": ["apps"]}))
    assert rc == cli.EXIT_OK and kube.applied == [("Vpc", None, "lab")]


def test_deletion_keeps_a_network_the_console_did_not_create(cli, monkeypatch, tmp_path, capsys):
    kube = Kube(cli)
    run(cli, kube, monkeypatch, "apply", kind="subnet", update=False, spec=spec_file(tmp_path, {
        "name": "overlay-a", "cidr": "10.201.0.0/24", "network": "default/ovn-overlay"}))
    rc = run(cli, kube, monkeypatch, "delete", kind="subnet", name="overlay-a", with_network=True)
    assert rc == cli.EXIT_OK
    assert (cli.K_NAD, "default", "ovn-overlay") not in kube.deleted
    assert "kept (not created by the console)" in capsys.readouterr().err


def test_deletion_takes_the_console_network_along(cli, monkeypatch, tmp_path):
    kube = Kube(cli)
    run(cli, kube, monkeypatch, "apply", kind="subnet", update=False, spec=spec_file(tmp_path, {
        "name": "lab-a", "cidr": "10.200.0.0/24", "new_network": "default/lab-a"}))
    rc = run(cli, kube, monkeypatch, "delete", kind="subnet", name="lab-a", with_network=True)
    assert rc == cli.EXIT_OK and (cli.K_NAD, "default", "lab-a") in kube.deleted


@pytest.mark.parametrize("kind, name", [("subnet", "ovn-default"), ("vpc", "ovn-cluster")])
def test_system_objects_cannot_be_deleted(cli, monkeypatch, kind, name):
    kube = Kube(cli)
    assert run(cli, kube, monkeypatch, "delete", kind=kind, name=name, with_network=False) == cli.EXIT_BLOCKED
    assert kube.deleted == []


def test_inventory_without_kube_ovn(cli, monkeypatch):
    kube = Kube(cli)
    kube.objs = {k: v for k, v in kube.objs.items() if k[0] != cli.K_VPC}
    assert cli.inventory(kube) == {"unreachable": False, "kubeovn": False}
