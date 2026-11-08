"""2.10.0 (tâche 8) : sortes d'External Secrets Operator dans
capi_manifests. `ExternalSecret` et `SecretStore` namespacées et admises ;
`PushSecret` refusée ; sortes de portée cluster refusées depuis un paquet
(un paquet ne crée pas de magasin qui traverse les namespaces)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_manifests as mf  # noqa: E402
import child_eso as ce  # noqa: E402

API = "external-secrets.io/v1"


def obj(kind, ns="team-a", api=API, name="x"):
    md = {"name": name}
    if ns:
        md["namespace"] = ns
    return {"apiVersion": api, "kind": kind, "metadata": md, "spec": {}}


@pytest.mark.parametrize("kind", ["ExternalSecret", "SecretStore"])
def test_namespaced_eso_kinds_admitted(kind):
    assert mf.check([obj(kind)], "c1") == []
    assert mf.scope_of(obj(kind)) is True


@pytest.mark.parametrize("kind,ns", [
    ("PushSecret", "team-a"),
    ("ClusterPushSecret", None),
    ("ClusterSecretStore", None),
    ("ClusterExternalSecret", None),
    ("ClusterSecretStore", "team-a"),          # namespace donné : toujours refusée
])
def test_refused_eso_kinds(kind, ns):
    found = mf.check([obj(kind, ns)], "c1")
    assert [f["code"] for f in found] == ["manifest-kind-refused"]
    assert found[0]["facts"]["reason"]


def test_refused_even_when_the_child_serves_the_kind():
    """La découverte du cluster enfant sert ces sortes (CRD du chart) : le
    refus vient de la console, pas de leur absence."""
    scopes = {("external-secrets.io", "PushSecret"): True, ("external-secrets.io", "ClusterSecretStore"): False}
    for kind, ns in (("PushSecret", "team-a"), ("ClusterSecretStore", None)):
        assert [f["code"] for f in mf.check([obj(kind, ns)], "c1", scopes=scopes)] == ["manifest-kind-refused"]


def test_same_kind_of_another_group_is_not_eso():
    """`PushSecret` d'un autre groupe : sorte inconnue, pas le refus d'ESO."""
    assert [f["code"] for f in mf.check([obj("PushSecret", api="example.io/v1")], "c1")] == ["manifest-kind-unknown"]


def test_store_of_a_system_namespace_refused():
    assert "manifest-system-namespace" in [f["code"] for f in mf.check([obj("SecretStore", "kube-system")], "c1")]


def test_console_store_objects_pass_the_manifest_rules():
    objs = ce.store_objects(["team-a", "team-b"], "https://172.16.1.11:8212", "harvester-ops",
                            "jwt-hops-abc123def456-eso-1d905023", "hops-openbao-abc123def456", "pem")
    assert mf.check(objs, "c1", ce.ITEM) == []
    assert mf.needs_eso(objs)
    assert not mf.needs_eso([obj("ConfigMap", api="v1")])


def test_removed_store_of_a_disabled_namespace():
    old = ce.store_objects(["team-a", "team-b"], "https://a:1", "kv", "jwt", "aud", None)
    new = ce.store_objects(["team-a"], "https://a:1", "kv", "jwt", "aud", None)
    assert sorted(r[1] for r in mf.removed(old, new)) == ["SecretStore", "ServiceAccount"]
    assert {r[2] for r in mf.removed(old, new)} == {"team-b"}
