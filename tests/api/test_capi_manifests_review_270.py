"""2.7.0, corrections de la revue des tâches 1 à 3 (logique pure) :

* C1 : un Namespace livré n'est supprimé que si rien d'ailleurs n'y vit
  (volume d'un opérateur, Secret d'équipe, ressource personnalisée) ;
* I1 : la portée d'une sorte vient des tables intégrées ou de la découverte
  du cluster enfant, jamais de la présence d'un `metadata.namespace` ;
* I2 : le Namespace `default` ne se livre pas (il ne se supprime pas) ;
* I3 : ce qui sort du contenu est rangé sur le ConfigMap avant
  l'application, et le reste jusqu'au retrait.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_manifests as mf  # noqa: E402

CL, ITEM = "web", "socle"


def obj(kind, name, ns=None, api="v1", uid=None, owners=None, ours=False):
    md = {"name": name}
    if ns:
        md["namespace"] = ns
    if uid:
        md["uid"] = uid
    if owners:
        md["ownerReferences"] = [{"uid": u, "kind": "X", "name": "x"} for u in owners]
    if ours:
        md["labels"] = mf.marks(ITEM, CL)
    return {"apiVersion": api, "kind": kind, "metadata": md}


def codes(found):
    return [f["code"] for f in found]


# --- I1 : portée -------------------------------------------------------------
API_RESOURCES = """\
configmaps                          cm           v1                                     true         ConfigMap
namespaces                          ns           v1                                     false        Namespace
flowschemas                                      flowcontrol.apiserver.k8s.io/v1        false        FlowSchema
clusterissuers                                   cert-manager.io/v1                     false        ClusterIssuer
certificates                        cert,certs   cert-manager.io/v1                     true         Certificate
"""


def test_api_resources_are_read_from_the_right():
    scopes = mf.parse_api_resources(API_RESOURCES + "garbage line\n")
    assert scopes == {("", "ConfigMap"): True, ("", "Namespace"): False,
                      ("flowcontrol.apiserver.k8s.io", "FlowSchema"): False,
                      ("cert-manager.io", "ClusterIssuer"): False, ("cert-manager.io", "Certificate"): True}


SCOPES = mf.parse_api_resources(API_RESOURCES)


@pytest.mark.parametrize("o,scopes,expected", [
    # portée cluster déguisée par un namespace : refusée, découverte ou non
    (obj("FlowSchema", "f", "team", api="flowcontrol.apiserver.k8s.io/v1"), None, ["manifest-kind-refused"]),
    (obj("FlowSchema", "f", "team", api="flowcontrol.apiserver.k8s.io/v1"), SCOPES, ["manifest-kind-refused"]),
    (obj("MutatingAdmissionPolicy", "m", "team", api="admissionregistration.k8s.io/v1alpha1"), None,
     ["manifest-kind-refused"]),
    (obj("VolumeAttachment", "v", "team", api="storage.k8s.io/v1"), None, ["manifest-kind-refused"]),
    (obj("ClusterIssuer", "c", "team", api="cert-manager.io/v1"), SCOPES, ["manifest-kind-refused"]),
    # sorte personnalisée : inconnue sans découverte, acceptée si namespacée
    (obj("Certificate", "c", "team", api="cert-manager.io/v1"), None, ["manifest-kind-unknown"]),
    (obj("Certificate", "c", "team", api="cert-manager.io/v1"), SCOPES, []),
    (obj("Widget", "w", "team", api="example.io/v1"), SCOPES, ["manifest-kind-unknown"]),
    # la découverte l'emporte sur la table (même groupe et sorte)
    (obj("ConfigMap", "c", "team"), {("", "ConfigMap"): False}, ["manifest-kind-refused"]),
    (obj("ConfigMap", "c", "team"), None, []),
])
def test_scope_comes_from_tables_or_discovery(o, scopes, expected):
    assert codes(mf.check([o], CL, scopes=scopes)) == expected


def test_unknown_reason_says_who_decides():
    o = obj("Widget", "w", "team", api="example.io/v1")
    assert "when it is applied" in mf.check([o], CL)[0]["facts"]["reason"]
    assert "not served" in mf.check([o], CL, scopes=SCOPES)[0]["facts"]["reason"]


# --- I2 : namespaces qui ne se suppriment pas ---------------------------------
@pytest.mark.parametrize("o,expected", [
    (obj("Namespace", "default"), ["manifest-system-namespace"]),
    (obj("Namespace", "kube-node-lease"), ["manifest-system-namespace"]),
    (obj("ConfigMap", "c", "default"), []),                  # un objet DANS default reste permis
])
def test_immortal_namespaces(o, expected):
    assert codes(mf.check([o], CL)) == expected


# --- C1 : contenu d'un namespace avant sa suppression -------------------------
SS = obj("StatefulSet", "db", "team", api="apps/v1", uid="ss", ours=True)


@pytest.mark.parametrize("inside,foreign", [
    ([], []),
    # ce que Kubernetes pose partout
    ([obj("ServiceAccount", "default", "team"), obj("ConfigMap", "kube-root-ca.crt", "team")], []),
    # volume d'un opérateur (CloudNativePG) : propriétaire hors de l'élément
    ([obj("PersistentVolumeClaim", "pg-1", "team", uid="p", owners=["cnpg"])], ["pg-1"]),
    # Secret ou ConfigMap d'équipe, ressource personnalisée
    ([obj("Secret", "creds", "team", uid="s")], ["creds"]),
    ([obj("ConfigMap", "conf", "team", uid="c")], ["conf"]),
    ([obj("Cluster", "pg", "team", api="postgresql.cnpg.io/v1", uid="cr")], ["pg"]),
    # descendants de l'élément, de proche en proche : à lui
    ([SS, obj("Pod", "db-0", "team", uid="pod", owners=["ss"]),
      obj("PersistentVolumeClaim", "data-db-0", "team", uid="pvc", owners=["ss"])], []),
    ([obj("Deployment", "web", "team", api="apps/v1", uid="d", ours=True),
      obj("ReplicaSet", "web-1", "team", api="apps/v1", uid="rs", owners=["d"]),
      obj("Pod", "web-1-a", "team", uid="pa", owners=["rs"])], []),
    # un objet d'un AUTRE élément qui livre le même namespace
    ([{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": "other", "namespace": "team", "labels": mf.marks("autre", CL)}}], ["other"]),
    # même nom d'élément, autre cluster : pas à lui
    ([{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": "twin", "namespace": "team", "labels": mf.marks(ITEM, "autre")}}], ["twin"]),
    # un pod sans propriétaire qui ne porte rien
    ([obj("Pod", "debug", "team", uid="dbg")], ["debug"]),
])
def test_foreign_objects(inside, foreign):
    assert [o["metadata"]["name"] for o in mf.foreign(inside, ITEM, CL)] == foreign


def test_scan_resources_drops_only_what_kubernetes_makes():
    names = ["pods", "secrets", "configmaps", "events", "events.events.k8s.io", "endpoints",
             "endpointslices.discovery.k8s.io", "leases.coordination.k8s.io", "controllerrevisions.apps",
             "pods.metrics.k8s.io", "clusters.postgresql.cnpg.io", "persistentvolumeclaims", ""]
    assert mf.scan_resources(names) == ["pods", "secrets", "configmaps", "clusters.postgresql.cnpg.io",
                                        "persistentvolumeclaims"]


# --- I3 : retrait en attente ---------------------------------------------------
A = obj("ConfigMap", "a", "team")
B = obj("ConfigMap", "b", "team")
C = obj("Deployment", "c", "team", api="apps/v1")


def cm(objs, pending=None):
    doc = mf.docs("team", CL, ITEM, "p", "1", objs)[0]
    return mf.with_pending(doc, pending or [])


@pytest.mark.parametrize("old,new,expected", [
    (None, [A, B], []),                                         # premier déploiement
    (cm([A, B]), [A], [mf.ref_of(B)]),                          # B sort
    (cm([A], [mf.ref_of(B)]), [A], [mf.ref_of(B)]),             # B toujours en attente (reprise)
    (cm([A], [mf.ref_of(B)]), [A, B], []),                      # B revient : plus rien à retirer
    (cm([A, C], [mf.ref_of(B)]), [A], [mf.ref_of(B), mf.ref_of(C)]),
])
def test_pending_after(old, new, expected):
    assert mf.pending_after(old, new) == expected


def test_pending_round_trip_and_hash_unchanged():
    doc = cm([A])
    marked = mf.with_pending(doc, [mf.ref_of(B)])
    assert mf.pending_of(marked) == [mf.ref_of(B)]
    assert mf.content_hash(marked) == mf.content_hash(doc)      # l'empreinte du binding n'en dépend pas
    assert mf.A_PENDING not in mf.with_pending(marked, [])["metadata"]["annotations"]


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '[["a", "b"]]', '{"a": 1}'])
def test_pending_of_ignores_garbage(raw):
    assert mf.pending_of({"metadata": {"annotations": {mf.A_PENDING: raw}}}) == []
