"""v1.48.0 : le script `harvester-capi` (suppression, relevés).

La suppression ne laisse rien derrière elle.

Vu sur harv1 le 25/09/2026 : après la suppression d'essai-capi, son espace de
noms gardait le secret d'identité (un kubeconfig du cluster Harvester), la
ClusterClass, les gabarits et les compléments. La suppression retire désormais
tout ce que le rendu a étiqueté, mais seulement quand plus aucun cluster ne
les partage.
"""

import argparse
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_capi", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Kube:
    def __init__(self, objs):
        self.objs = objs              # {(kind, ns, name): labels}
        self.deleted = []

    def get(self, kind, ns, name):
        return {"metadata": {"name": name, "labels": self.objs[(kind, ns, name)]}} \
            if (kind, ns, name) in self.objs else None

    def list(self, kind, ns=None, selector=None):
        key, _, val = (selector or "").partition("=")
        return [{"metadata": {"name": n, "labels": lab}} for (k, s, n), lab in self.objs.items()
                if k == kind and s == ns and (not key or lab.get(key) == val)]

    def delete(self, kind, ns, name, cascade=None):
        self.deleted.append((kind, ns, name))
        self.objs.pop((kind, ns, name), None)


def world(cli, managed=True, neighbour=False):
    cc, cs = cli.cc, cli.cs
    gen = {cc.GENERATED: "true"}
    objs = {
        ("namespaces", None, "web"): {cs.MANAGED: "true"} if managed else {},
        (cs.K_CLUSTER, "web", "web"): {},
        ("secrets", "web", "hv-identity-secret"): gen,
        ("clusterclasses.cluster.x-k8s.io", "web", "harvester-rke2"): gen,
        ("configmaps", "web", "calico-helm-config"): gen,
        ("configmaps", "web", "kube-root-ca.crt"): {},        # pas à nous
        ("secrets", "web", "someone-else"): {},               # pas à nous
    }
    if neighbour:
        objs[(cs.K_CLUSTER, "web", "other")] = {}
    return Kube(objs)


def run(cli, kube, monkeypatch):
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return cli.cmd_delete(argparse.Namespace(name="web/web", timeout=60,
                                             cluster=None, kubeconfig=None))


def test_the_last_cluster_takes_its_generated_objects_along(cli, monkeypatch):
    kube = world(cli, managed=False)
    assert run(cli, kube, monkeypatch) == cli.EXIT_OK
    left = {(k, n) for (k, _, n) in kube.objs}
    assert ("secrets", "hv-identity-secret") not in left
    assert ("clusterclasses.cluster.x-k8s.io", "harvester-rke2") not in left
    assert ("configmaps", "kube-root-ca.crt") in left and ("secrets", "someone-else") in left
    assert ("namespaces", "web") in left                      # pas créé par la console


def test_a_console_namespace_goes_too(cli, monkeypatch):
    kube = world(cli, managed=True)
    run(cli, kube, monkeypatch)
    assert ("namespaces", None, "web") in kube.deleted


def test_shared_objects_stay_while_a_neighbour_uses_them(cli, monkeypatch):
    kube = world(cli, neighbour=True)
    run(cli, kube, monkeypatch)
    assert kube.deleted == [(cli.cs.K_CLUSTER, "web", "web")]


# -- relevés ------------------------------------------------------------------

def image(name, display, os_hint=None, iso=False):
    return {"metadata": {"namespace": "default", "name": name},
            "spec": {"displayName": display, "url": display if iso else ""},
            "status": {"progress": 100}}


def test_an_image_is_referenced_by_its_object_name(cli):
    """Vu sur harv1 : « Rocky Linux 8 GenericCloud » (des espaces) faisait
    refuser la demande ; CAPHV accepte aussi le nom de l'objet."""
    info = cli._image_info(image("rocky8-genericcloud", "Rocky Linux 8 GenericCloud"))
    assert info["ref"] == "default/rocky8-genericcloud"
    assert info["display_name"] == "Rocky Linux 8 GenericCloud" and info["os"] == "rocky"
    assert cli.cc.IMAGE_RE.match(info["ref"])


@pytest.mark.parametrize("display, os_", [
    ("openSUSE Tumbleweed Cloud", "opensuse"), ("openSUSE-Leap-15.6-Minimal-VM-Cloud.qcow2", "opensuse"),
    ("leap156-fips.qcow2", "opensuse"), ("sles15-sp7-minimal-vm.x86_64-cloud-qu2.qcow2", "sles"),
    ("rhel-9.7-x86_64-kvm.qcow2", "rhel"), ("win2025-core-autounattend", None),
])
def test_the_os_hint(cli, display, os_):
    assert cli._image_info(image("x", display))["os"] == os_


def test_suse_images_come_first_and_isos_last(cli):
    imgs = [cli._image_info(image(n, d, iso=d.endswith(".iso"))) for n, d in (
        ("a", "Rocky Linux 8 GenericCloud"), ("b", "debian.iso"),
        ("c", "sles15-sp7.qcow2"), ("d", "openSUSE Tumbleweed Cloud"))]
    assert [i["name"] for i in sorted(imgs, key=cli._image_rank)] == ["c", "d", "a", "b"]


def test_a_request_may_name_the_image_either_way(cli):
    facts = cli._image_facts([cli._image_info(image("image-nhtf9", "sles15-sp7.qcow2"))])
    assert set(facts) == {"default/image-nhtf9", "default/sles15-sp7.qcow2"}


def test_only_tested_versions_escape_the_warning(cli):
    inv = dict(cli.EMPTY_INVENTORY, stack=None, free_cpu_m=0, free_memory=0, overcommit={},
               versions=[{"version": "v1.34.11", "tested": False},
                         {"version": "v1.31.14", "tested": True}],
               version_names=["v1.34.11", "v1.31.14"])
    assert cli.facts_from(inv)["versions"] == ["v1.31.14"]
    inv.update(versions=["v1.34.11"], version_names=["v1.34.11"])   # ancien format
    assert cli.facts_from(inv)["versions"] == ["v1.34.11"]


# --- 2.5.0 (tâche 7) : state, analyse des options ------------------------------

def _cluster_obj(cli, origin=None, ready=True, phase="Provisioned"):
    labels = {cli.cc.ORIGIN_LABELS[k]: v for k, v in (origin or {}).items() if k in cli.cc.ORIGIN_LABELS}
    ann = {cli.cc.ORIGIN_ANNOTATIONS[k]: v for k, v in (origin or {}).items() if k in cli.cc.ORIGIN_ANNOTATIONS}
    return {"metadata": {"name": "apps", "namespace": "apps", "labels": labels, "annotations": ann},
            "spec": {"topology": {"controlPlane": {"replicas": 1},
                                  "workers": {"machineDeployments": [{"replicas": 1}]}}},
            "status": {"phase": phase, "controlPlane": {"desiredReplicas": 1, "availableReplicas": 1 if ready else 0},
                       "workers": {"desiredReplicas": 1, "availableReplicas": 1 if ready else 0},
                       "conditions": [{"type": "Available", "status": "True" if ready else "False"}]}}


class OneCluster:
    def __init__(self, obj):
        self.obj, self.calls = obj, []

    def get(self, kind, ns, name):
        self.calls.append(("get", kind, ns, name))
        return self.obj if (ns, name) == ("apps", "apps") else None

    def list(self, *a, **kw):
        self.calls.append(("list",) + a)
        return []


@pytest.mark.parametrize("obj,code,expect", [
    ("origin", 0, {"present": True, "origin": {"plan": "site", "template": "rke2-petit", "run": "a1b2c3d4e5f6"},
                   "ready": True}),
    ("plain", 0, {"present": True, "origin": {}, "ready": True}),
    ("pending", 0, {"present": True, "origin": {}, "ready": False}),
    ("failed", 0, {"present": True, "origin": {}, "ready": False, "failed": True}),
    (None, 1, {"present": False}),
])
def test_state_one_read_json(cli, monkeypatch, capsys, obj, code, expect):
    import json
    built = {"origin": _cluster_obj(cli, {"plan": "site", "template": "rke2-petit", "run": "a1b2c3d4e5f6"}),
             "plain": _cluster_obj(cli), "pending": _cluster_obj(cli, ready=False, phase="Provisioning"),
             "failed": _cluster_obj(cli, ready=False, phase="Failed"), None: None}[obj]
    kube = OneCluster(built)
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    rc = cli.main(["state", "--cluster", "h", "--name", "apps/apps", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == code and out["present"] is expect["present"] and out["namespace"] == "apps"
    if expect["present"]:
        assert out["origin"] == expect["origin"] and out["state"]["ready"] is expect["ready"]
        assert out["state"]["failed"] is expect.get("failed", False)
        assert kube.calls == [("get", cli.cs.K_CLUSTER, "apps", "apps")]       # une seule lecture
    else:
        assert set(out) == {"present", "namespace", "name", "namespace_present"}
        assert out["namespace_present"] is False
        assert kube.calls == [("get", cli.cs.K_CLUSTER, "apps", "apps"), ("get", "namespaces", None, "apps")]


@pytest.mark.parametrize("argv,why", [
    (["state", "--cluster", "h", "--name", "apps"], "--name expects namespace/name"),
    (["state", "--cluster", "h", "--name", "Apps/apps"], "--name expects namespace/name"),
    (["create", "--cluster", "h", "--spec", "-", "--origin", "owner=me"], "--origin: unknown key 'owner'"),
    (["create", "--cluster", "h", "--spec", "-", "--origin", "plan=Site"], "--origin: invalid value for 'plan'"),
    (["create", "--cluster", "h", "--spec", "-", "--origin", "plan=a_b"], "--origin: invalid value for 'plan'"),
    (["create", "--cluster", "h", "--spec", "-", "--origin", "run=xyz"], "--origin: invalid value for 'run'"),
    (["create", "--cluster", "h", "--spec", "-", "--origin", "plan=a,plan=b"], "--origin: 'plan' given twice"),
])
def test_refused_values_exit_2_before_any_write(cli, monkeypatch, capsys, argv, why):
    kube = OneCluster(None)
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    assert cli.main(argv) == cli.EXIT_BLOCKED
    assert why in capsys.readouterr().err
    assert not [c for c in kube.calls if c[0] not in ("get", "list")]


@pytest.mark.parametrize("argv", [
    ["create", "--cluster", "h", "--spec", "-", "--identity", "admin"],
    ["check", "--cluster", "h", "--spec", "-", "--identity", "full"],
    ["state", "--cluster", "h", "--nam", "apps/apps"],                 # abréviation refusée
    ["create", "--cluster", "h", "--spec", "-", "--ident", "console"],
    ["stat", "--cluster", "h", "--name", "apps/apps"],
])
def test_parser_refuses_unknown_and_abbreviated(cli, argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2
