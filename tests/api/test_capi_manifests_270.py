"""2.7.0 (tâche 2) : manifestes livrés par ClusterResourceSet (logique pure).

Contrôle des objets (sortes, namespaces système, taille, doublons), rendu du
ConfigMap et du CRS, étiquettes injectées, objets à retirer, ordre de
suppression, état d'après le binding. Formes relevées sur harvlab2 le
07/11/2026 (Cluster API v1.13.3 de Turtles) : voir « Essais préalables
(tâche 1) » du plan 2.7.0.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_manifests as mf  # noqa: E402
import capi_stack as cs  # noqa: E402


def obj(kind, name, ns=None, api="v1", **extra):
    md = {"name": name}
    if ns:
        md["namespace"] = ns
    o = {"apiVersion": api, "kind": kind, "metadata": md}
    o.update(extra)
    return o


DEPLOY = obj("Deployment", "web", "equipe", api="apps/v1", spec={"replicas": 1})
CM = obj("ConfigMap", "conf", "equipe", data={"k": "v"})


def codes(found):
    return [f["code"] for f in found]


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("files,expected", [
    ({"a.yaml": "kind: ConfigMap\napiVersion: v1\nmetadata: {name: a, namespace: x}\n---\n---\n"
                "kind: ConfigMap\napiVersion: v1\nmetadata: {name: b, namespace: x}\n"}, ["a", "b"]),
    ({"l.yaml": "apiVersion: v1\nkind: List\nitems:\n- {apiVersion: v1, kind: ConfigMap, metadata: {name: c, namespace: x}}\n"
                "- {apiVersion: v1, kind: Secret, metadata: {name: d, namespace: x}}\n"}, ["c", "d"]),
    ({"b.json": json.dumps(CM), "a.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: first, namespace: x}\n"},
     ["first", "conf"]),
])
def test_parse_reads_documents_and_lists(files, expected):
    assert [o["metadata"]["name"] for o in mf.parse(files)] == expected


def test_parse_refuses_a_document_that_is_not_an_object():
    with pytest.raises(ValueError):
        mf.parse({"a.yaml": "- 1\n- 2\n"})


# ---------------------------------------------------------------------------
# Contrôle
# ---------------------------------------------------------------------------
# Liste du plan 2.7.0 (point 204), recopiée : la retirer du module doit
# faire échouer ce test, même pour une sorte posée avec un namespace.
REFUSED = ("CustomResourceDefinition", "APIService", "MutatingWebhookConfiguration",
           "ValidatingWebhookConfiguration", "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding",
           "Node", "PersistentVolume", "CSIDriver", "RuntimeClass")


@pytest.mark.parametrize("kind", REFUSED)
@pytest.mark.parametrize("ns", [None, "equipe"])
def test_each_refused_kind_is_refused(kind, ns):
    found = mf.check([obj(kind, "x", ns)], "c")
    assert codes(found) == ["manifest-kind-refused"] and "reason" not in found[0]["facts"]


@pytest.mark.parametrize("o,code", [
    (obj("ClusterRoleBinding", "b", api="rbac.authorization.k8s.io/v1",
         roleRef={"name": "cluster-admin"}, subjects=[{"kind": "Group", "name": "devs"}]), "manifest-crb-admin"),
    (obj("ClusterRoleBinding", "b", api="rbac.authorization.k8s.io/v1",
         roleRef={"name": "view"}, subjects=[{"kind": "Group", "name": "system:masters"}]), "manifest-crb-admin"),
    (obj("ConfigMap", "a", "kube-system"), "manifest-system-namespace"),
    (obj("ConfigMap", "a", "cattle-system"), "manifest-system-namespace"),
    (obj("ConfigMap", "a", "calico-system"), "manifest-system-namespace"),
    (obj("ConfigMap", "a", "tigera-operator"), "manifest-system-namespace"),
    (obj("Namespace", "kube-public"), "manifest-system-namespace"),
    (obj("Secret", "t", "equipe", type="kubernetes.io/service-account-token"), "manifest-sa-token-refused"),
    (obj("ConfigMap", "", "equipe"), "manifest-no-name"),
    (obj("ClusterIssuer", "x", api="cert-manager.io/v1"), "manifest-kind-refused"),   # portée cluster hors liste
    (obj("ConfigMap", "nons"), "manifest-kind-refused"),                              # namespacé sans namespace
])
def test_check_refusals(o, code):
    assert codes(mf.check([o], "c")) == [code]


@pytest.mark.parametrize("o", [
    obj("Namespace", "equipe"), obj("ClusterRole", "r", api="rbac.authorization.k8s.io/v1"),
    obj("ClusterRoleBinding", "b", api="rbac.authorization.k8s.io/v1", roleRef={"name": "view"},
        subjects=[{"kind": "Group", "name": "devs"}]),
    obj("StorageClass", "s", api="storage.k8s.io/v1"), obj("PriorityClass", "p", api="scheduling.k8s.io/v1"),
    obj("IngressClass", "i", api="networking.k8s.io/v1"), DEPLOY, CM,
    obj("Secret", "s", "equipe", type="Opaque"), obj("Widget", "w", "equipe", api="example.io/v1"),
])
def test_accepted_objects(o):
    assert mf.check([o], "c") == []


def test_duplicates_and_too_many():
    assert codes(mf.check([CM, copy.deepcopy(CM)], "c")) == ["manifest-duplicate"]
    many = [obj("ConfigMap", f"c{i}", "equipe") for i in range(mf.MAX_OBJECTS + 1)]
    assert codes(mf.check(many, "c")) == ["manifest-too-many"]
    assert mf.check(many[:mf.MAX_OBJECTS], "c") == []


def _sized(n):
    """Un ConfigMap dont le contenu rangé pèse exactement `n` octets (UTF-8,
    caractères de deux octets compris)."""
    o = obj("ConfigMap", "big", "equipe", data={"k": ""})
    base = mf.data_size(mf.data_of([o]))
    pad = n - base
    o["data"]["k"] = "é" * (pad // 2) + "x" * (pad % 2)
    assert mf.data_size(mf.data_of([o])) == n
    return o


@pytest.mark.parametrize("size,refused", [(cs.CM_LIMIT, False), (cs.CM_LIMIT + 1, True)])
def test_size_limit_in_bytes(size, refused):
    found = mf.check([_sized(size)], "c")
    assert codes(found) == (["manifest-too-big"] if refused else [])


def test_size_is_measured_with_the_labels_when_the_item_is_known():
    o = _sized(cs.CM_LIMIT - 10)
    assert mf.check([o], "c") == []
    assert codes(mf.check([o], "c", item="essai")) == ["manifest-too-big"]


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------
def test_stamp_keeps_the_original_labels_and_adds_three():
    o = obj("ConfigMap", "a", "equipe")
    o["metadata"]["labels"] = {"app": "web"}
    out = mf.stamp([o], "essai", "rke2-banc")[0]
    assert out["metadata"]["labels"] == {"app": "web", mf.MANAGED: "true", mf.L_ITEM: "essai",
                                         mf.L_CLUSTER: "rke2-banc"}
    assert o["metadata"]["labels"] == {"app": "web"}                    # copie


def test_docs_render_configmap_and_reconcile_crs():
    cmap, crs = mf.docs("ns1", "c1", "essai", "socle", "1.0.0", [CM, DEPLOY],
                        origin={"plan": "p", "run": "0123456789ab"}, by="alice")
    assert cmap["metadata"]["name"] == crs["metadata"]["name"] == "art-essai"
    for d in (cmap, crs):
        assert d["metadata"]["labels"] == {mf.MANAGED: "true", mf.L_ITEM: "essai", mf.L_PACKAGE: "socle",
                                           mf.L_CLUSTER: "c1", "harvester-ops.io/plan": "p"}
        assert d["metadata"]["annotations"] == {mf.A_VERSION: "1.0.0", mf.A_BY: "alice",
                                                "harvester-ops.io/plan-run": "0123456789ab"}
    assert crs["apiVersion"] == "addons.cluster.x-k8s.io/v1beta1"
    assert crs["spec"] == {"clusterSelector": {"matchLabels": {"harvester-ops.io/art-essai": "on"}},
                           "resources": [{"kind": "ConfigMap", "name": "art-essai"}], "strategy": "Reconcile"}
    back = mf.objects_of(cmap)
    assert [o["metadata"]["name"] for o in back] == ["conf", "web"]
    assert all(mf.is_ours(o, "essai", "c1") for o in back)


# ---------------------------------------------------------------------------
# Retrait
# ---------------------------------------------------------------------------
def test_removed_lists_what_left_the_content():
    old = [CM, DEPLOY, obj("Service", "svc", "equipe")]
    renamed = obj("Service", "svc2", "equipe")
    new = [CM, renamed]
    assert mf.removed(old, new) == [("apps/v1", "Deployment", "equipe", "web"), ("v1", "Service", "equipe", "svc")]


def test_removed_ignores_an_api_version_change():
    moved = copy.deepcopy(DEPLOY)
    moved["apiVersion"] = "apps/v2"
    assert mf.removed([DEPLOY], [moved]) == []


def test_delete_order_namespaced_then_cluster_then_namespaces():
    refs = [mf.ref_of(o) for o in (obj("Namespace", "equipe"), DEPLOY,
                                   obj("ClusterRole", "r", api="rbac.authorization.k8s.io/v1"), CM)]
    assert [r[1] for r in mf.delete_order(refs)] == ["Deployment", "ConfigMap", "ClusterRole", "Namespace"]


@pytest.mark.parametrize("ref,res", [
    (("apps/v1", "Deployment", "x", "w"), "Deployment.v1.apps"),
    (("v1", "ConfigMap", "x", "c"), "ConfigMap"),                  # « ConfigMap.v1 » est refusé par kubectl
    (("rbac.authorization.k8s.io/v1", "ClusterRole", "", "r"), "ClusterRole.v1.rbac.authorization.k8s.io"),
])
def test_resource_of(ref, res):
    assert mf.resource_of(ref) == res


@pytest.mark.parametrize("labels,ours", [
    ({mf.MANAGED: "true", mf.L_ITEM: "essai", mf.L_CLUSTER: "c1"}, True),
    ({mf.MANAGED: "true", mf.L_ITEM: "essai"}, False),
    ({mf.MANAGED: "true", mf.L_ITEM: "autre", mf.L_CLUSTER: "c1"}, False),
    ({mf.MANAGED: "true", mf.L_ITEM: "essai", mf.L_CLUSTER: "c2"}, False),
    ({}, False),
])
def test_is_ours(labels, ours):
    assert mf.is_ours({"metadata": {"labels": labels}}, "essai", "c1") is ours


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------
def test_content_hash_matches_cluster_api():
    """Empreinte relevée dans le binding de harvlab2 pour le ConfigMap
    kube-vip-addon de la 1.52.0 : même calcul (valeurs, clés triées)."""
    cm1 = {"data": {"b": "2", "a": "1"}}
    import hashlib
    assert mf.content_hash(cm1) == "sha256:" + hashlib.sha256(b"12").hexdigest()


def _binding(cluster, name, hash_, applied=True):
    return {"metadata": {"name": cluster}, "spec": {"clusterName": cluster, "bindings": [
        {"clusterResourceSetName": "crs-kube-vip", "resources": [{"kind": "ConfigMap", "name": "kube-vip-addon",
                                                                 "applied": True, "hash": "sha256:x"}]},
        {"clusterResourceSetName": name, "resources": [{"kind": "ConfigMap", "name": name,
                                                       "applied": applied, "hash": hash_}]}]}}


@pytest.mark.parametrize("make,applied,message", [
    (lambda c, h: [_binding("c1", "art-essai", h)], True, "applied"),
    (lambda c, h: [_binding("c1", "art-essai", "sha256:old")], False, "being applied"),
    (lambda c, h: [_binding("c1", "art-essai", h, applied=False)], False, "being applied"),
    (lambda c, h: [], False, "not applied to any cluster yet"),
    (lambda c, h: [_binding("c1", "art-other", h)], False, "not applied to any cluster yet"),
])
def test_state_from_bindings(make, applied, message):
    cmap, crs = mf.docs("ns", "c1", "essai", "p", "1", [CM])
    st = mf.state(crs, make(cmap, mf.content_hash(cmap)), cmap)
    assert (st["applied"], st["message"]) == (applied, message)


def test_state_without_crs_or_configmap():
    cmap, crs = mf.docs("ns", "c1", "essai", "p", "1", [CM])
    assert mf.state(None, [], cmap)["message"] == "no ClusterResourceSet"
    assert mf.state(crs, [], None)["message"] == "no ConfigMap"


@pytest.mark.parametrize("item,ok", [("a" * 42, True), ("a" * 43, False), ("Essai", False), ("essai-1", True)])
def test_applicable_item_names(item, ok):
    assert mf.item_name_ok(item) is ok
