"""2.8.0 (tâche 6) : renouvellement des machines, partie pure (capi_renew)
et rendu des gabarits RKE2 d'une demande (capi_cluster.renew_templates).

Vu sur rke2-banc (plan 2.8.0, « Essais préalables (tâche 1) », point 6) :
copies `-r1` puis ClusterClass repointée, plan de contrôle et worker
remplacés ensemble, anciens gabarits jamais supprimés par Cluster API.

Mutations jouées (rapport des tâches 5 à 8) : « gabarit modifié en place »,
« anciens gabarits supprimés avant la fin » (test_capi_renew_cli_280).
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_cluster as cc  # noqa: E402
import capi_renew as cr  # noqa: E402
import child_oidc as co  # noqa: E402
import registries as rg  # noqa: E402

NS = "web"
OLD, NEW = "2020-01-01T00:00:00Z", "2099-01-01T00:00:00Z"


def cp_template(name="harvester-rke2-control-plane", files=None, args=None, rev=None):
    spec = {"serverConfig": {"cni": "calico", "cloudProviderName": "external"}}
    if files is not None:
        spec["files"] = files
    if args is not None:
        spec["serverConfig"]["kubeAPIServer"] = {"extraArgs": args}
    md = {"name": name, "namespace": NS, "labels": {cc.GENERATED: "true"}, "resourceVersion": "42", "uid": "u1"}
    if rev:
        md["annotations"] = {cr.A_REVISION: str(rev)}
    return {"apiVersion": "controlplane.cluster.x-k8s.io/v1beta2", "kind": "RKE2ControlPlaneTemplate",
            "metadata": md, "spec": {"template": {"spec": spec}}}


def cfg_template(name="harvester-rke2-worker", files=None):
    spec = {"agentConfig": {}}
    if files is not None:
        spec["files"] = files
    return {"apiVersion": "bootstrap.cluster.x-k8s.io/v1beta2", "kind": "RKE2ConfigTemplate",
            "metadata": {"name": name, "namespace": NS, "labels": {cc.GENERATED: "true"}},
            "spec": {"template": {"spec": spec}}}


def cluster_class(v1beta2=True, cp="harvester-rke2-control-plane", worker="harvester-rke2-worker"):
    if v1beta2:
        return {"metadata": {"name": "harvester-rke2", "namespace": NS}, "spec": {
            "controlPlane": {"templateRef": {"apiVersion": "controlplane.cluster.x-k8s.io/v1beta2",
                                             "kind": "RKE2ControlPlaneTemplate", "name": cp}},
            "workers": {"machineDeployments": [{"class": "default-worker", "bootstrap": {"templateRef": {
                "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta2", "kind": "RKE2ConfigTemplate", "name": worker}},
                "infrastructure": {"templateRef": {"kind": "HarvesterMachineTemplate", "name": "hmt"}}}]}}}
    return {"metadata": {"name": "harvester-rke2", "namespace": NS}, "spec": {
        "controlPlane": {"ref": {"kind": "RKE2ControlPlaneTemplate", "name": cp}},
        "workers": {"machineDeployments": [{"class": "default-worker", "template": {
            "bootstrap": {"ref": {"kind": "RKE2ConfigTemplate", "name": worker}},
            "infrastructure": {"ref": {"kind": "HarvesterMachineTemplate", "name": "hmt"}}}}]}}}


def machine(name, created, ready=True, deleting=False):
    md = {"name": name, "namespace": NS, "creationTimestamp": created, "labels": {cr.L_CLUSTER: "web"}}
    if deleting:
        md["deletionTimestamp"] = NEW
    return {"metadata": md, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


# --- références, différences, copies ---------------------------------------------

@pytest.mark.parametrize("v1beta2", [True, False])
def test_template_refs_of_both_api_versions(v1beta2):
    refs = cr.template_refs(cluster_class(v1beta2))
    assert [(r, x["kind"], x["name"]) for r, x in refs] == [
        ("controlPlane", "RKE2ControlPlaneTemplate", "harvester-rke2-control-plane"),
        ("workers/default-worker", "RKE2ConfigTemplate", "harvester-rke2-worker")]


@pytest.mark.parametrize("before,after,paths", [
    (cp_template(), cp_template(), []),
    (cp_template(), cp_template(files=[{"path": "/x"}]), ["spec.template.spec.files"]),
    (cp_template(), cp_template(args=["a=b"]), ["spec.template.spec.serverConfig.kubeAPIServer"]),
    (cp_template(args=["a=b"]), cp_template(args=["a=b", "c=d"]),
     ["spec.template.spec.serverConfig.kubeAPIServer.extraArgs"]),
])
def test_diff(before, after, paths):
    got = cr.diff([before], [after])
    assert [p for d in got for p in d["paths"]] == paths
    assert (got == []) is (paths == [])


def test_rotated_copies_take_the_next_revision():
    one = cr.rotated([cp_template()], 1, now=1000)[0]
    assert one["metadata"]["name"] == "harvester-rke2-control-plane-r1"
    assert one["metadata"]["annotations"] == {cr.A_REVISION: "1", cr.A_RENEWED: "1000"}
    assert "resourceVersion" not in one["metadata"] and "uid" not in one["metadata"]
    assert one["metadata"]["labels"] == {cc.GENERATED: "true"}
    two = cr.rotated([one], 2, now=2000)[0]
    assert two["metadata"]["name"] == "harvester-rke2-control-plane-r2"          # pas -r1-r2
    long = cr.rotated([cp_template(name="x" * 63)], 12)[0]
    assert len(long["metadata"]["name"]) == 63 and long["metadata"]["name"].endswith("-r12")


@pytest.mark.parametrize("v1beta2", [True, False])
def test_class_patch_repoints_both_templates(v1beta2):
    cls = cluster_class(v1beta2)
    before = copy.deepcopy(cls)
    patch = cr.class_patch(cls, {"harvester-rke2-control-plane": "harvester-rke2-control-plane-r1",
                                 "harvester-rke2-worker": "harvester-rke2-worker-r1"})
    assert cls == before                                                       # rien modifié sur place
    assert [x["name"] for _r, x in cr.template_refs({"spec": patch["spec"]})] == [
        "harvester-rke2-control-plane-r1", "harvester-rke2-worker-r1"]
    mds = patch["spec"]["workers"]["machineDeployments"]
    assert mds[0]["class"] == "default-worker"                                 # liste rendue entière
    only_cp = cr.class_patch(cls, {"harvester-rke2-control-plane": "cp-r1"})
    assert "workers" not in only_cp["spec"]


# --- progression ------------------------------------------------------------------

NEW_TPL = cr.rotated([cp_template()], 1, now=1700000000)


@pytest.mark.parametrize("machines,done,old,new", [
    ([machine("a", OLD), machine("b", OLD)], False, 2, 0),
    ([machine("a", OLD), machine("c", NEW, ready=False)], False, 1, 1),
    ([machine("a", OLD, deleting=True), machine("c", NEW)], False, 1, 1),   # ancienne encore là
    ([machine("c", NEW, ready=False), machine("d", NEW)], False, 0, 2),
    ([machine("c", NEW), machine("d", NEW)], True, 0, 2),
])
def test_progress(machines, done, old, new):
    p = cr.progress(machines, NEW_TPL)
    assert (p["done"], p["old"], p["new"]) == (done, old, new)


def test_busy_only_after_a_recorded_renewal():
    assert cr.busy([machine("a", OLD)], NEW_TPL)
    assert not cr.busy([machine("c", NEW)], NEW_TPL)
    assert not cr.busy([machine("a", OLD)], [cp_template()])                  # jamais renouvelé
    assert cr.progress([machine("a", OLD)], [cp_template()])["done"]


def test_old_templates_never_the_designated_ones():
    cur = [cp_template(), cfg_template(), cp_template(name="harvester-rke2-control-plane-r1", rev=1),
           cp_template(name="autre-cluster")]
    got = cr.old_templates(cur, ["harvester-rke2-control-plane-r2", "harvester-rke2-worker-r2"])
    assert [d["metadata"]["name"] for d in got] == ["harvester-rke2-control-plane", "harvester-rke2-worker",
                                                    "harvester-rke2-control-plane-r1"]
    keep = cr.old_templates(cur, ["harvester-rke2-control-plane-r1"])
    assert [d["metadata"]["name"] for d in keep] == ["harvester-rke2-control-plane"]


@pytest.mark.parametrize("pool,cpu,mem,codes", [
    ({"name": "p", "available": 1}, None, None, []),
    ({"name": "p", "available": 0}, None, None, ["renew-pool-short"]),
    (None, None, None, ["renew-pool-missing"]),
    ({"name": "p", "available": 3}, 1000, 1 << 40, ["cpu-short"]),
    ({"name": "p", "available": 3}, 10 ** 6, 1 << 20, ["memory-short"]),
])
def test_room_for_the_extra_machine(pool, cpu, mem, codes):
    got = cr.room_findings(pool, cpu, mem, 2, 4 << 30)
    assert [f["code"] for f in got] == codes
    assert all(f["level"] == "warn" for f in got if f["code"].endswith("-short") and f["code"] != "renew-pool-short")


@pytest.mark.parametrize("raw,ok", [("registries", ["registries"]), ("ca,oidc", ["oidc", "ca"]),
                                    ("", None), ("disks", None), ("registries,disks", None)])
def test_check_whats(raw, ok):
    if ok is None:
        with pytest.raises(ValueError):
            cr.check_whats(raw)
    else:
        assert cr.check_whats(raw) == ok


def test_standard_library_only():
    code = "import sys; sys.path.insert(0, %r); import capi_renew" % str(ROOT / "bin" / "lib")
    p = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


# --- gabarits rendus (capi_cluster.renew_templates) ---------------------------------

def reg_files(secret="web-registries-r1", ca=("amorcage",), cut=False):
    out = [{"path": rg.REGISTRIES_FILE, "owner": "root:root", "permissions": "0600",
            "contentFrom": {"secret": {"name": secret, "key": "registries.yaml"}}}]
    out += [{"path": f"{rg.CA_DIR}/{n}/ca.crt", "contentFrom": {"secret": {"name": secret, "key": f"ca-{n}.crt"}}}
            for n in ca]
    if cut:
        out.append({"path": rg.NO_FALLBACK_FILE, "content": "disable-default-registry-endpoint: true\n"})
    return out


OWN = {"path": "/etc/motd", "content": "bienvenue\n"}
OIDC = {"issuer_url": "https://idp.client/realms/k8s", "audience": "kubernetes"}


def test_registries_replace_the_registry_files_of_both_templates_only():
    docs = [cp_template(files=[OWN] + reg_files("web-registries", ("ancien",), cut=True)),
            cfg_template(files=reg_files("web-registries", ("ancien",), cut=True))]
    got = cc.renew_templates(docs, {"name": "web"}, ["registries"], {"files": reg_files()})
    for d in got:
        paths = [f["path"] for f in d["spec"]["template"]["spec"]["files"]]
        assert rg.NO_FALLBACK_FILE not in paths and f"{rg.CA_DIR}/ancien/ca.crt" not in paths
        assert f"{rg.CA_DIR}/amorcage/ca.crt" in paths
    assert OWN in got[0]["spec"]["template"]["spec"]["files"]                 # un fichier à soi reste
    assert docs[0]["spec"]["template"]["spec"]["files"][0] == OWN and len(docs[0]["spec"]["template"]["spec"]["files"]) == 4


def test_no_registry_left_removes_the_files():
    got = cc.renew_templates([cfg_template(files=reg_files())], {"name": "web"}, ["registries"], None)
    assert "files" not in got[0]["spec"]["template"]["spec"]


def test_oidc_is_replaced_in_the_control_plane_template_only():
    first = cc.renew_templates([cp_template(args=["profile=cis"]), cfg_template()],
                               {"name": "web", "k8s_version": "v1.34.11+rke2r1", "oidc": OIDC}, ["oidc"])
    api = first[0]["spec"]["template"]["spec"]["serverConfig"]["kubeAPIServer"]
    assert api["extraArgs"] == ["profile=cis", f"{co.ARG}={co.FILE}"]
    assert "files" not in first[1]["spec"]["template"]["spec"]
    changed = dict(OIDC, audience="k8s-2")
    second = cc.renew_templates(first, {"name": "web", "k8s_version": "v1.34.11", "oidc": changed}, ["oidc"])
    files = second[0]["spec"]["template"]["spec"]["files"]
    assert len(files) == 1 and json.loads(files[0]["content"])["jwt"][0]["issuer"]["audiences"] == ["k8s-2"]
    removed = cc.renew_templates(first, {"name": "web", "k8s_version": "v1.34.11"}, ["oidc"])
    assert removed[0]["spec"]["template"]["spec"]["serverConfig"]["kubeAPIServer"]["extraArgs"] == ["profile=cis"]
    assert "files" not in removed[0]["spec"]["template"]["spec"]


def test_nodes_secret_names():
    assert rg.nodes_secret_from([cp_template(files=reg_files("web-registries")),
                                 cp_template(files=reg_files("web-registries-r2"), rev=2)]) == "web-registries-r2"
    assert rg.nodes_secret_from([cp_template()]) is None
    assert rg.is_nodes_secret_of("web-registries-r3", "web") and rg.is_nodes_secret_of("web-registries", "web")
    assert not rg.is_nodes_secret_of("web2-registries", "web") and not rg.is_nodes_secret_of("web-registry-a-ca", "web")
