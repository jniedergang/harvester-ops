"""2.8.0 (tâche 2) : bin/lib/rancher_import.py, logique pure de l'import
dans un Rancher externe. Formes relevées sur Rancher v2.14.1 (plan 2.8.0,
« Essais préalables (tâche 1) », points 1 et 2)."""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_cluster as cc  # noqa: E402
import rancher_import as ri  # noqa: E402

TOKEN = "kq7x9w2b4d8v6n5m3c1z0l9k8j7h6g5f4d3s2a1p0o9i8u7y6"
URL_214 = f"https://rancher.home.zypp.fr/v3/import/{TOKEN}_c-m-nwsjn9p6.yaml"
MANIFEST = """\
apiVersion: v1
kind: Namespace
metadata: {name: cattle-system}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: cattle-cluster-agent, namespace: cattle-system}
spec:
  template:
    spec:
      initContainers: [{name: init, image: registry.rancher.com/rancher/rancher-agent:v2.14.1}]
      containers:
      - {name: cluster-register, image: registry.rancher.com/rancher/rancher-agent:v2.14.1}
      - {name: side, image: busybox:1.37}
---
apiVersion: v1
kind: Service
metadata: {name: cattle-cluster-agent, namespace: cattle-system}
"""


@pytest.mark.parametrize("h,ns,c,want", [
    ("harvlab2", "rke2-banc", "rke2-banc", "harvlab2-rke2-banc"),
    ("harvlab2", "equipes", "web", "harvlab2-equipes-web"),
    ("HarvLab2", "a", "a", "harvlab2-a"),
])
def test_rancher_names(h, ns, c, want):
    assert ri.rancher_name(h, ns, c) == want


def test_a_long_name_is_cut_with_a_stable_digest():
    ns, c = "n" * 30, "c" * 30
    full = f"harvlab2-{ns}-{c}"
    got = ri.rancher_name("harvlab2", ns, c)
    assert len(full) > 63 and len(got) == 63
    assert got == full[:54] + "-" + hashlib.sha256(full.encode()).hexdigest()[:8]
    assert got == ri.rancher_name("harvlab2", ns, c) and cc.K8S_NAME_RE.match(got)


def test_the_body_carries_the_origin_marks():
    b = ri.cluster_body("harvlab2-x", "harvlab2", "x", "x", "alice", run="0123456789ab")
    assert b["spec"] == {} and b["metadata"]["namespace"] == "fleet-default"
    assert b["metadata"]["labels"] == {"harvester-ops.io/managed": "true", "harvester-ops.io/harvester": "harvlab2",
                                       "harvester-ops.io/child-namespace": "x", "harvester-ops.io/child": "x"}
    assert b["metadata"]["annotations"] == {"harvester-ops.io/applied-by": "alice",
                                            "harvester-ops.io/plan-run": "0123456789ab"}
    assert "harvester-ops.io/plan-run" not in ri.cluster_body("n", "h", "x", "x", "a")["metadata"]["annotations"]


@pytest.mark.parametrize("labels,run,expected,ok", [
    ("ours", None, None, True),
    ("ours", "0123456789ab", "0123456789ab", True),
    ("ours", "0123456789ab", "ffffffffffff", False),
    ("ours", None, "0123456789ab", False),
    ("other-child", None, None, False),
    ("none", None, None, False),
])
def test_ours(labels, run, expected, ok):
    body = ri.cluster_body("n", "harvlab2", "x", "x", "a", run=run)
    if labels == "other-child":
        body["metadata"]["labels"]["harvester-ops.io/child"] = "y"
    if labels == "none":
        body["metadata"]["labels"] = {}
    assert ri.ours(body, "harvlab2", "x", "x", expected) is ok


@pytest.mark.parametrize("tokens,want", [
    ({"data": [{"manifestUrl": URL_214}]}, URL_214),                         # 2.14 : jeton substitué
    ([{"manifestUrl": "https://r/v3/import/{token}_c-m-x.yaml"}, {"manifestUrl": URL_214}], URL_214),
])
def test_the_manifest_address_with_its_token(tokens, want):
    assert ri.registration_manifest_url(tokens) == want


@pytest.mark.parametrize("tokens", [
    {"data": []},
    {"data": [{"manifestUrl": "https://r/v3/import/{token}_c-m-x.yaml"}]},   # forme du CRD de 2.15
    {"data": [{"manifestUrl": "https://r/v3/import/%7Btoken%7D_c-m-x.yaml"}]},
    {"data": [{"token": "x"}]},
])
def test_a_manifest_address_without_its_token_is_refused(tokens):
    with pytest.raises(ri.RancherImportError) as e:
        ri.registration_manifest_url(tokens)
    assert e.value.code == "rancher-token-unreadable"


@pytest.mark.parametrize("url,ok", [
    (URL_214, True),
    ("https://evil.example/v3/import/abcdefgh_c-m-x.yaml", True),            # l'hôte est ignoré
    ("https://r/v3/import/../../etc/passwd", False),
    ("https://r/v1/secrets", False),
    ("https://r/v3/import/abc.yaml", False),
])
def test_only_the_import_path_is_kept(url, ok):
    if ok:
        assert ri.manifest_path(url).startswith("/v3/import/")
    else:
        with pytest.raises(ri.RancherImportError) as e:
            ri.manifest_path(url)
        assert e.value.code == "rancher-manifest-path"


def test_a_manifest_over_10_mib_is_refused():
    ri.check_size(ri.MANIFEST_MAX)
    with pytest.raises(ri.RancherImportError) as e:
        ri.check_size(ri.MANIFEST_MAX + 1)
    assert e.value.code == "rancher-manifest-too-big"


def test_images_of_the_manifest():
    assert ri.manifest_images(MANIFEST) == ["registry.rancher.com/rancher/rancher-agent:v2.14.1", "busybox:1.37"]


@pytest.mark.parametrize("image,want", [
    ("busybox:1.37", ("docker.io", "library/busybox", "1.37")),
    ("rancher/rancher-agent:v2.14.1", ("docker.io", "rancher/rancher-agent", "v2.14.1")),
    ("registry.rancher.com/rancher/rancher-agent:v2.14.1", ("registry.rancher.com", "rancher/rancher-agent", "v2.14.1")),
    ("172.16.2.76:5000/x/y@sha256:" + "a" * 64, ("172.16.2.76:5000", "x/y", "sha256:" + "a" * 64)),
    ("alpine", ("docker.io", "library/alpine", "latest")),
])
def test_split_image(image, want):
    assert ri.split_image(image) == want


DOC = {"mirrors": {"registry.rancher.com": {"endpoint": ["https://172.16.2.76:5000"],
                                            "rewrite": {"^(.*)$": "registry.rancher.com/$1"}}}}


def test_probe_targets_follow_the_rewrite_of_the_mirror():
    assert ri.probe_targets("registry.rancher.com/rancher/rancher-agent:v2.14.1", DOC) == [
        ("172.16.2.76:5000", "registry.rancher.com/rancher/rancher-agent", "v2.14.1")]
    assert ri.probe_targets("docker.io/x/y:1", DOC) == []


@pytest.mark.parametrize("found,no_fallback,want", [
    ({"registry.rancher.com/rancher/rancher-agent:v2.14.1": True}, True, []),
    ({"registry.rancher.com/rancher/rancher-agent:v2.14.1": False}, True, [("rancher-agent-image-missing", "block")]),
    ({"registry.rancher.com/rancher/rancher-agent:v2.14.1": False}, False, [("rancher-agent-image-fallback", "warn")]),
    ({}, True, [("rancher-agent-image-missing", "block")]),
])
def test_agent_image_findings(found, no_fallback, want):
    imgs = ["registry.rancher.com/rancher/rancher-agent:v2.14.1", "busybox:1.37"]      # busybox : sans miroir
    got = ri.image_findings(imgs, DOC, found, no_fallback)
    assert [(f["code"], f["level"]) for f in got] == want


@pytest.mark.parametrize("text,want", [
    ("rancher.home.zypp.fr/harvlab2-x/c-m-nwsjn9p6", {"server": "rancher.home.zypp.fr", "name": "harvlab2-x",
                                                      "cid": "c-m-nwsjn9p6"}),
    ("r:8443/n/c-sg2q6", {"server": "r:8443", "name": "n", "cid": "c-sg2q6"}),
    ("r/n", None), ("r/N/c-m-x1y", None), ("r/n/x-1", None), ("", None), (None, None),
])
def test_marks(text, want):
    assert ri.parse_mark(text) == want
    if want:
        assert ri.mark(want["server"], want["name"], want["cid"]) == text


def test_server_id():
    assert ri.server_id("https://Rancher.Home.Zypp.FR") == "rancher.home.zypp.fr"
    assert ri.server_id("https://r:8443/") == "r:8443"


def _o(kind, name):
    return {"kind": kind, "metadata": {"name": name}}


def test_leftovers_are_the_ones_seen_on_rancher_2_14():
    objs = [_o("Namespace", "local"), _o("Namespace", "equipe-a"), _o("Namespace", "cattle-system"),
            _o("ClusterRole", "cattle-admin"), _o("ClusterRole", "cluster-owner"), _o("ClusterRole", "p-p5bmg-namespaces-edit"),
            _o("ClusterRole", "admin"), _o("ClusterRole", "system:rke2-metrics-server"),
            _o("ClusterRoleBinding", "crb-elp57iuebr"), _o("ClusterRoleBinding", "globaladmin-user-kk67j"),
            _o("ClusterRoleBinding", "proxy-role-binding-kubernetes-master"), _o("ClusterRoleBinding", "hops-oidc-admins"),
            _o("CustomResourceDefinition", "settings.management.cattle.io"),
            _o("CustomResourceDefinition", "plans.upgrade.cattle.io"),
            _o("CustomResourceDefinition", "helmcharts.helm.cattle.io"),
            _o("CustomResourceDefinition", "addons.k3s.cattle.io"),
            _o("CustomResourceDefinition", "kustomizations.kustomize.toolkit.fluxcd.io")]
    got = ri.leftovers(objs)
    assert ("CustomResourceDefinition", "", "helmcharts.helm.cattle.io") not in got      # à RKE2
    assert ("CustomResourceDefinition", "", "addons.k3s.cattle.io") not in got
    assert {r[2] for r in got} == {"local", "cattle-admin", "cluster-owner", "p-p5bmg-namespaces-edit",
                                   "crb-elp57iuebr", "globaladmin-user-kk67j", "proxy-role-binding-kubernetes-master",
                                   "settings.management.cattle.io", "plans.upgrade.cattle.io"}
    assert got[-1] == ("Namespace", "", "local")                                       # namespaces en dernier


@pytest.mark.parametrize("prov,mgmt,ok", [
    ({"status": {"ready": True}}, {"state": "active"}, True),
    ({"status": {"ready": True}}, {"state": "pending"}, False),
    ({"status": {}}, {"state": "active"}, False),
    (None, None, False),
])
def test_ready(prov, mgmt, ok):
    assert ri.ready(prov, mgmt) is ok


def test_cluster_id_and_rights():
    assert ri.cluster_id({"status": {"clusterName": "c-m-nwsjn9p6"}}) == "c-m-nwsjn9p6"
    assert ri.cluster_id({"status": {"clusterName": "local"}}) == ""
    assert ri.can_create({"collectionMethods": ["GET", "POST"]}) and not ri.can_create({"collectionMethods": ["GET"]})


def test_standard_library_only():
    """Bibliothèque standard seulement (preuve par python3 -I -S)."""
    code = ("import sys; sys.path.insert(0, %r); import rancher_import, child_oidc, child_gitops, flux_artifact"
            % str(ROOT / "bin" / "lib"))
    p = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_secret_values_to_hide():
    import base64
    url_b64 = base64.b64encode(b"https://rancher.home.zypp.fr").decode()
    text = ("apiVersion: v1\nkind: Secret\nmetadata: {name: cattle-credentials-x}\n"
            f"data: {{url: {url_b64}, token: {base64.b64encode(TOKEN.encode()).decode()}}}\n")
    hidden = ri.secret_values(text, f"/v3/import/{TOKEN}_c-m-nwsjn9p6.yaml")
    assert TOKEN in hidden and f"{TOKEN}_c-m-nwsjn9p6" in hidden
    assert base64.b64encode(TOKEN.encode()).decode() in hidden and "https://rancher.home.zypp.fr" in hidden
