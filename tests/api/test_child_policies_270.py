"""2.7.0 (tâche 3) : socle de politiques, rôles par groupe, comptes
d'automatisation (logique pure). Valeurs exactes des Contraintes du plan
2.7.0 ; sélecteur du contrôleur d'ingress relevé sur rke2-banc (harvlab2)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_manifests as mf  # noqa: E402
import child_policies as cp  # noqa: E402


def by_kind(objs):
    out = {}
    for o in objs:
        out.setdefault(o["kind"], []).append(o)
    return out


def test_the_base_is_unchecked_by_default():
    spec = cp.normalize({"teams": [{"namespace": "a"}, {"namespace": "b"}]})
    assert spec["base"] is False
    assert cp.render(spec)["base"] == [] and cp.team_namespaces(spec) == []


def test_the_base_with_defaults_has_the_exact_values():
    spec = cp.normalize({"base": True, "teams": [{"namespace": "equipe-a"}]})
    objs = cp.render(spec)["base"]
    k = by_kind(objs)
    assert [o["kind"] for o in objs] == ["Namespace", "ResourceQuota", "LimitRange"] + ["NetworkPolicy"] * 4
    assert k["Namespace"][0]["metadata"] == {"name": "equipe-a", "labels": {"harvester-ops.io/team": "true"}}
    assert k["ResourceQuota"][0]["metadata"]["name"] == "hops-quota"
    assert k["ResourceQuota"][0]["spec"]["hard"] == {
        "requests.cpu": "4", "requests.memory": "8Gi", "limits.cpu": "8", "limits.memory": "16Gi",
        "pods": "50", "persistentvolumeclaims": "10", "requests.storage": "200Gi", "services.loadbalancers": "2"}
    assert k["LimitRange"][0]["metadata"]["name"] == "hops-limits"
    assert k["LimitRange"][0]["spec"]["limits"] == [{
        "type": "Container", "default": {"cpu": "500m", "memory": "512Mi"},
        "defaultRequest": {"cpu": "100m", "memory": "128Mi"}, "max": {"cpu": "2", "memory": "4Gi"}}]
    nps = {o["metadata"]["name"]: o["spec"] for o in k["NetworkPolicy"]}
    assert nps["hops-default-deny"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    assert nps["hops-allow-same-namespace"]["ingress"] == [{"from": [{"podSelector": {}}]}]
    assert nps["hops-allow-same-namespace"]["egress"] == [{"to": [{"podSelector": {}}]}]
    assert nps["hops-allow-dns"]["egress"] == [{
        "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]
    assert nps["hops-allow-ingress"]["ingress"] == [{"from": [{
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "rke2-ingress-nginx",
                                        "app.kubernetes.io/component": "controller"}}}]}]
    assert all(o["metadata"].get("namespace") == "equipe-a" for o in objs[1:])
    assert mf.check(objs, "c") == []                        # livrable tel quel par capi_manifests


@pytest.mark.parametrize("network,names", [
    ({"default_deny": False}, []),
    ({"allow_ingress": False}, ["hops-default-deny", "hops-allow-same-namespace", "hops-allow-dns"]),
    ({}, ["hops-default-deny", "hops-allow-same-namespace", "hops-allow-dns", "hops-allow-ingress"]),
])
def test_network_switches(network, names):
    spec = cp.normalize({"base": True, "teams": [{"namespace": "a", "network": network}]})
    assert [o["metadata"]["name"] for o in cp.render(spec)["base"] if o["kind"] == "NetworkPolicy"] == names


def test_quota_and_limits_can_be_tuned():
    spec = cp.normalize({"base": True, "teams": [{"namespace": "a", "quota": {"pods": "10", "requests.cpu": "500m"},
                                                  "limits": {"max": {"memory": "8Gi"}}}]})
    t = spec["teams"][0]
    assert t["quota"]["pods"] == "10" and t["quota"]["requests.cpu"] == "500m" and t["quota"]["limits.cpu"] == "8"
    assert t["limits"]["max"] == {"cpu": "2", "memory": "8Gi"}


@pytest.mark.parametrize("raw,field", [
    ({"base": True, "teams": [{"namespace": "a", "quota": {"requests.cpu": "-1"}}]}, "teams[0].quota.requests.cpu"),
    ({"base": True, "teams": [{"namespace": "a", "quota": {"pods": "abc"}}]}, "teams[0].quota.pods"),
    ({"base": True, "teams": [{"namespace": "a", "quota": {"requests.memory": "8 GB"}}]},
     "teams[0].quota.requests.memory"),
    ({"base": True, "teams": [{"namespace": "a", "quota": {"gpus": "1"}}]}, "teams[0].quota.gpus"),
    ({"base": True, "teams": [{"namespace": "a", "limits": {"max": {"cpu": "x"}}}]}, "teams[0].limits.max.cpu"),
    ({"base": True, "teams": [{"namespace": "a", "network": {"default_deny": "no"}}]},
     "teams[0].network.default_deny"),
    ({"base": True, "teams": [{"namespace": f"t{i}"} for i in range(17)]}, "teams: 16"),
    ({"base": True, "teams": [{"namespace": "kube-public"}]}, "teams[0].namespace"),
    ({"base": True, "teams": [{"namespace": "default"}]}, "teams[0].namespace"),
    ({"base": True, "teams": [{"namespace": "calico-system"}]}, "teams[0].namespace"),
    ({"base": True, "teams": [{"namespace": "a"}, {"namespace": "a"}]}, "appears twice"),
    ({"base": True, "teams": []}, "one team"),
    ({"teams": [{"namespace": "a"}], "roles": [{"namespace": "a", "group": "g", "role": "cluster-admin"}]},
     "roles[0].role"),
    ({"teams": [{"namespace": "a"}], "roles": [{"namespace": "b", "group": "g", "role": "view"}]},
     "roles[0].namespace"),
    ({"teams": [{"namespace": "a"}], "roles": [{"namespace": "a", "group": "x" * 129, "role": "view"}]},
     "roles[0].group"),
    ({"teams": [{"namespace": "a"}], "roles": [{"namespace": "a", "group": "system:masters", "role": "view"}]},
     "roles[0].group"),
    ({"teams": [{"namespace": "a"}], "accounts": [{"namespace": "b", "name": "ci", "role": "edit"}]},
     "accounts[0].namespace"),
    ({"teams": [{"namespace": "a"}], "accounts": [{"namespace": "a", "name": "ci", "role": "admin"}]},
     "accounts[0].role"),
    ({"teams": [{"namespace": "a"}], "accounts": [{"namespace": "a", "name": "CI", "role": "edit"}]},
     "accounts[0].name"),
    ({"teams": [{"namespace": "a"}], "accounts": [{"namespace": "a", "name": f"c{i}", "role": "view"}
                                                  for i in range(17)]}, "accounts: 16"),
    ({"extra": 1}, "policies.extra"),
])
def test_refusals_name_the_field(raw, field):
    with pytest.raises(ValueError) as e:
        cp.normalize(raw)
    assert field in str(e.value)


def test_roles_and_accounts_without_the_base():
    spec = cp.normalize({"teams": [{"namespace": "a"}],
                         "roles": [{"namespace": "a", "group": "devs", "role": "edit"}],
                         "accounts": [{"namespace": "a", "name": "ci", "role": "view"}]})
    r = cp.render(spec)
    assert r["base"] == []
    rb = r["roles"][0]
    assert rb["kind"] == "RoleBinding" and rb["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io",
                                                           "kind": "ClusterRole", "name": "edit"}
    assert rb["subjects"] == [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": "devs"}]
    sa, sab = r["accounts"]
    assert sa == {"apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
                  "metadata": {"name": "ci", "namespace": "a"}}
    assert sab["metadata"]["name"] == "hops-sa-ci" and sab["roleRef"]["name"] == "view"
    assert sab["subjects"] == [{"kind": "ServiceAccount", "name": "ci", "namespace": "a"}]


def test_no_cluster_role_binding_and_no_token_secret_ever():
    spec = cp.normalize({"base": True, "teams": [{"namespace": "a"}, {"namespace": "b"}],
                         "roles": [{"namespace": "a", "group": "g", "role": "admin"}],
                         "accounts": [{"namespace": "a", "name": "ci", "role": "edit"},
                                      {"namespace": "b", "name": "ci", "role": "view"}]})
    objs = cp.objects(spec)
    assert not [o for o in objs if o["kind"] in ("ClusterRoleBinding", "Secret")]
    assert [o["kind"] for o in objs][:1] == ["Namespace"]       # socle, puis rôles, puis comptes
    kinds = [o["kind"] for o in objs]
    assert kinds.index("RoleBinding") > kinds.index("NetworkPolicy")
    assert kinds[-2:] == ["ServiceAccount", "RoleBinding"]


def test_role_binding_names_are_stable_and_distinct():
    assert cp.role_binding_name("edit", "devs") == cp.role_binding_name("edit", "devs")
    assert cp.role_binding_name("edit", "devs") != cp.role_binding_name("edit", "ops")
    assert len(cp.role_binding_name("admin", "x" * 128)) <= 63


@pytest.mark.parametrize("limits,field", [
    ({"defaultRequest": {"cpu": "600m"}}, "teams[0].limits.defaultRequest.cpu"),          # > default 500m
    ({"default": {"memory": "8Gi"}}, "teams[0].limits.default.memory"),                   # > max 4Gi
    ({"max": {"cpu": "200m"}}, "teams[0].limits.default.cpu"),                            # default 500m > max
    ({"defaultRequest": {"memory": "1Gi"}, "default": {"memory": "1024Mi"}}, None),       # égalité acceptée
    ({"max": {"cpu": "0.5"}, "default": {"cpu": "500m"}}, None),
])
def test_limits_must_be_ordered(limits, field):
    """Revue 1-3 m6 : defaultRequest <= default <= max, sinon l'API refuse
    le LimitRange et le binding ne s'applique jamais (délai de 900 s)."""
    raw = {"base": True, "teams": [{"namespace": "a", "limits": limits}]}
    if field is None:
        cp.normalize(raw)
        return
    with pytest.raises(ValueError) as e:
        cp.normalize(raw)
    assert str(e.value).startswith(field), str(e.value)
