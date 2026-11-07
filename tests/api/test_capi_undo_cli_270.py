"""2.7.0 (tâche 5) : ce que harvester-capi apporte au Défaire.

* `manifest-remove --dry-run` : objets jugés par le serveur, rien d'écrit ;
* `delete --dry-run` : machines, VMs, volumes et répartiteurs relevés ;
* `service-remove` d'un chart de paquet : le namespace de la release que
  CAAPH laisse (essai 2) est retiré s'il est né après le service et vide,
  gardé et nommé sinon.
"""

import argparse
import contextlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import capi_manifests as mf  # noqa: E402
import test_capi_manifests_cli_270 as tm  # noqa: E402
from test_capi_manifests_cli_270 import cli, world  # noqa: E402,F401
from test_capi_services_cli import Kube, cluster_obj  # noqa: E402

NS, CL = tm.NS, tm.CL


def test_manifest_remove_dry_run_writes_nothing(cli, world, capsys):
    kube = tm.deployed(cli, world, capsys)
    child = tm.Child([tm.stamped(tm.NSO), tm.obj("ConfigMap", "essai-cm", "essai"), tm.stamped(tm.DEP)])
    assert world["run"](kube, child, ["manifest-remove", "--name", f"{NS}/{CL}/essai", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    # le ConfigMap repris à la main est gardé, et avec lui son namespace
    assert out["delete"] == ["Deployment essai/essai-web"]
    assert out["kept"] == [{"object": "ConfigMap essai/essai-cm", "code": "kept-relabelled"},
                           {"object": "Namespace essai", "code": "kept-in-use"}]
    assert child.deleted == [] and kube.calls == []
    assert kube.get(mf.K_CRS, NS, "art-essai") is not None


@pytest.mark.parametrize("setup,expected", [
    (lambda k: k.objs.pop(("configmaps", NS, "art-essai")) and k.objs.pop((mf.K_CRS, NS, "art-essai")),
     {"absent": True}),
    (lambda k: k.objs.pop((tm.cs.K_CLUSTER, NS, CL)), {"cluster_present": False}),
])
def test_manifest_remove_dry_run_without_child_work(cli, world, capsys, setup, expected):
    kube = tm.deployed(cli, world, capsys)
    setup(kube)
    assert world["run"](kube, tm.Child([]), ["manifest-remove", "--name", f"{NS}/{CL}/essai", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {k: out[k] for k in expected} == expected and out["delete"] == []
    assert not any(c[0] == "delete" for c in kube.calls)


def test_delete_dry_run_lists_what_would_go(cli, monkeypatch, capsys):
    cs = cli.cs
    cl = cluster_obj("web", "web")
    cl["spec"] = {"topology": {"variables": [{"name": "targetNamespace", "value": "web-vms"}]}}
    mach = lambda n: {"metadata": {"name": n, "namespace": "web",                      # noqa: E731
                                   "labels": {"cluster.x-k8s.io/cluster-name": "web"}},
                      "spec": {"infrastructureRef": {"name": n}}}
    kube = Kube({(cs.K_CLUSTER, "web", "web"): cl, ("machines.cluster.x-k8s.io", "web", "web-cp"): mach("web-cp"),
                 ("persistentvolumeclaims", "web-vms", "web-cp-disk-0"): {"metadata": {"name": "web-cp-disk-0"}},
                 ("persistentvolumeclaims", "web-vms", "other"): {"metadata": {"name": "other"}},
                 ("namespaces", None, "web"): {"metadata": {"labels": {cs.MANAGED: "true"}}}})
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    assert cli.main(["delete", "--name", "web/web", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"cluster": "web/web", "present": True, "machines": 1, "target_namespace": "web-vms",
                   "vms": ["web-cp"], "volumes": ["web-cp-disk-0"], "loadbalancers": [], "namespace_removed": True}
    assert kube.deleted == []


class ChildNs:
    def __init__(self, ns_obj, inside=(), unlistable=False):
        self.ns_obj, self.inside, self.unlistable, self.deleted = ns_obj, list(inside), unlistable, []

    def get(self, kind, ns, name):
        return self.ns_obj

    def api_resources(self, namespaced=None, verbs=None):
        return "pods\nsecrets\nconfigmaps\n"

    def list(self, kinds, ns=None, selector=None, missing_ok=True):
        if self.unlistable:
            raise cli_mod().KubeError("the server doesn't have a resource type \"x\"")
        return self.inside

    def delete(self, kind, ns, name, **kw):
        self.deleted.append(name)


_CLI = {}


def cli_mod():
    return _CLI["m"]


@pytest.mark.parametrize("ns_born,inside,unlistable,deleted,needle", [
    ("2026-11-07T10:05:00Z", [], False, ["podinfo"], "removed from essai (created for the chart)"),
    ("2026-11-07T09:00:00Z", [], False, [], "existed before the chart"),
    ("2026-11-07T10:05:00Z", [{"kind": "Secret", "metadata": {"name": "x", "uid": "u"}}], False, [], "holds 1"),
    ("2026-11-07T10:05:00Z", [{"kind": "ServiceAccount", "metadata": {"name": "default"}}], False, ["podinfo"],
     "created for the chart"),
    ("2026-11-07T10:05:00Z", [], True, [], "kept on essai"),
])
def test_release_namespace_left_by_caaph(cli, monkeypatch, capsys, ns_born, inside, unlistable, deleted, needle):
    _CLI["m"] = cli
    sv, cs = cli.sv, cli.cs
    labels, ann = mf.meta_of("podinfo", "socle", "2026.11", "essai")
    hcp = {"metadata": {"namespace": "essai", "name": "podinfo", "labels": labels, "annotations": ann,
                        "creationTimestamp": "2026-11-07T10:00:00Z"}, "spec": {"namespace": "podinfo"}}
    kube = Kube({(cs.K_CLUSTER, "essai", "essai"): cluster_obj("essai", "essai"), (sv.K_HCP, "essai", "podinfo"): hcp})
    child = ChildNs({"metadata": {"name": "podinfo", "creationTimestamp": ns_born}}, inside, unlistable)
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    @contextlib.contextmanager
    def session(k, ns, c):
        yield "/nonexistent"
    monkeypatch.setattr(cli.ca, "session", session)
    monkeypatch.setattr(cli, "Kube", lambda path, **kw: child)
    assert cli.cmd_service_remove(argparse.Namespace(name="essai/podinfo", timeout=60)) == cli.EXIT_OK
    assert child.deleted == deleted
    assert needle in capsys.readouterr().err


def test_a_catalog_service_never_touches_the_child(cli, monkeypatch, capsys):
    sv, cs = cli.sv, cli.cs
    kube = Kube({(cs.K_CLUSTER, "essai", "essai"): cluster_obj("essai", "essai"),
                 (sv.K_HCP, "essai", "ingress"): {"metadata": {"namespace": "essai", "name": "ingress"},
                                                  "spec": {"namespace": "ingress"}}})
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    monkeypatch.setattr(cli.ca, "session", lambda *a: (_ for _ in ()).throw(AssertionError("child read")))
    assert cli.cmd_service_remove(argparse.Namespace(name="essai/ingress", timeout=60)) == cli.EXIT_OK
