"""2.10.0 (tâche 8) : bin/lib/child_eso.py. Valeurs imposées du chart
(noms relevés dans le chart 2.14.0, essai 7), magasin rendu exact (essai 7 :
`SecretStore` `hops-openbao` `Valid`), namespaces système refusés, état
sans valeur, retrait sans perte des Secrets (point 300)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_cluster as cc  # noqa: E402
import child_eso as ce  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "child_eso_2100"
CA = "-----BEGIN CERTIFICATE-----\nMIIBbanc\n-----END CERTIFICATE-----\n"
ARGS = ("https://172.16.1.11:8212", "harvester-ops", "jwt-hops-abc123def456-eso-1d905023",
        "hops-openbao-abc123def456", CA)
WITNESS = "valeur-temoin-tres-secrete-2100"


def test_store_rendered_exactly():
    """Magasin de l'essai 7 (même forme que celui qui était `Valid`)."""
    want = json.loads((FIX / "team-a.json").read_text())
    assert ce.namespace_objects("team-a", *ARGS) == want


def test_store_account_has_no_token_and_no_rights():
    sa, store = ce.namespace_objects("team-a", *ARGS)
    assert sa["kind"] == "ServiceAccount" and sa["automountServiceAccountToken"] is False
    assert "secrets" not in sa
    auth = store["spec"]["provider"]["vault"]["auth"]["jwt"]
    assert auth["kubernetesServiceAccountToken"]["serviceAccountRef"] == {"name": "hops-eso"}
    assert "namespace" not in auth["kubernetesServiceAccountToken"]["serviceAccountRef"]


@pytest.mark.parametrize("ns,code", [
    ("kube-system", "eso-system-namespace"),
    ("cattle-system", "eso-system-namespace"),
    ("calico-system", "eso-system-namespace"),
    ("tigera-operator", "eso-system-namespace"),
    ("external-secrets", "eso-system-namespace"),
    ("Team_A", "eso-namespace-invalid"),
    ("", "eso-namespace-invalid"),
])
def test_system_namespace_refused(ns, code):
    """Mutation « namespace système admis »."""
    assert [f["code"] for f in ce.check_namespace(ns)] == [code]
    with pytest.raises((ce.EsoError, ValueError)):
        ce.namespace_objects(ns, *ARGS)


@pytest.mark.parametrize("ns", ["team-a", "default", "app-1"])
def test_team_namespace_admitted(ns):
    assert ce.check_namespace(ns) == []


def test_normalize_namespaces_sorted_unique_and_refuses_system():
    assert ce.normalize_namespaces(["b", "a", "b"]) == ["a", "b"]
    assert ce.normalize_namespaces(None) == []
    with pytest.raises(ce.EsoError) as e:
        ce.normalize_namespaces(["team-a", "kube-system"])
    assert e.value.code == "eso-system-namespace"
    with pytest.raises(ValueError):
        ce.normalize_namespaces("team-a")


def test_cluster_key_of_trial_7():
    assert ce.cluster_key("harvlab2", "c210", "c210") == "1d905023"
    assert ce.cluster_key("harvlab2", "c210", "c211") != "1d905023"


# ---------------------------------------------------------------------------
# Valeurs du chart
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("user,path", [
    ({"processClusterStore": True}, ("processClusterStore",)),
    ({"processPushSecret": True}, ("processPushSecret",)),
    ({"processClusterPushSecret": True}, ("processClusterPushSecret",)),
    ({"processClusterExternalSecret": True}, ("processClusterExternalSecret",)),
    ({"installCRDs": False}, ("installCRDs",)),
    ({"webhook": {"create": False, "replicaCount": 2}}, ("webhook", "create")),
    ({"certController": {"create": False}}, ("certController", "create")),
    ({"bitwarden-sdk-server": {"enabled": True}}, ("bitwarden-sdk-server", "enabled")),
    ({"serviceMonitor": {"enabled": True}}, ("serviceMonitor", "enabled")),
    ({"vault": {"enableTokenCache": True}}, ("vault", "enableTokenCache")),
    ({"webhook": "off"}, ("webhook", "create")),
])
def test_imposed_values_cannot_be_removed_by_the_package(user, path):
    out = ce.chart_values(user)
    want = ce.VALUES
    have = out
    for p in path:
        want, have = want[p], have[p]
    assert have is want
    assert ce.chart_locked(out)
    assert not ce.chart_locked(user)


def test_package_values_kept_beside_imposed_ones():
    out = ce.chart_values({"webhook": {"replicaCount": 2}, "resources": {"limits": {"memory": "256Mi"}}})
    assert out["webhook"] == {"replicaCount": 2, "create": True}
    assert out["resources"] == {"limits": {"memory": "256Mi"}}


def test_chart_locked_requires_every_value():
    for k in ce.VALUES:
        v = ce.chart_values()
        v.pop(k)
        assert not ce.chart_locked(v), k


# ---------------------------------------------------------------------------
# État sans valeur
# ---------------------------------------------------------------------------
def _es(ns, name, uid, ready=True, extra=None):
    o = {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
         "metadata": {"namespace": ns, "name": name, "uid": uid, "finalizers": [ce.FINALIZER]},
         "spec": {"secretStoreRef": {"kind": "SecretStore", "name": "hops-openbao"}, "target": {"name": name},
                  "data": [{"secretKey": "password", "remoteRef": {"key": "k", "property": WITNESS}}]},
         "status": {"refreshTime": "2026-11-08T10:00:00Z", "binding": {"name": name},
                    "conditions": [{"type": "Ready", "status": "True" if ready else "False",
                                    "reason": "SecretSynced" if ready else "SecretSyncedError",
                                    "message": "Secret was synced" if ready else "permission denied"}]}}
    if extra:
        o.update(extra)
    return o


def test_status_carries_no_value():
    """La fixture porte une valeur témoin dans le spec et des données : rien
    ne ressort."""
    es = _es("team-a", "db", "u1", extra={"data": {"password": WITNESS}})
    out = ce.status([es, _es("team-b", "db", "u2", ready=False)])
    assert WITNESS not in json.dumps(out)
    assert out == [
        {"ns": "team-a", "name": "db", "ready": True, "reason": "SecretSynced", "message": "Secret was synced",
         "synced_at": "2026-11-08T10:00:00Z", "store": "hops-openbao", "target": "db"},
        {"ns": "team-b", "name": "db", "ready": False, "reason": "SecretSyncedError",
         "message": "permission denied", "synced_at": "2026-11-08T10:00:00Z", "store": "hops-openbao",
         "target": "db"}]


def test_stores_ready():
    def store(ns, ok):
        return {"metadata": {"namespace": ns, "name": "hops-openbao"},
                "status": {"conditions": [{"type": "Ready", "status": "True" if ok else "False"}]}}
    assert ce.stores_ready([store("a", True), store("b", True)], ["a", "b"]) == (True, [])
    assert ce.stores_ready([store("a", True), store("b", False)], ["a", "b"]) == (False, ["b"])
    assert ce.stores_ready([store("a", True)], ["a", "c"]) == (False, ["c"])


# ---------------------------------------------------------------------------
# Retrait sans perte
# ---------------------------------------------------------------------------
def _secret(ns, name, refs):
    return {"metadata": {"namespace": ns, "name": name, "ownerReferences": refs}, "data": {"password": "eA=="}}


ES_REF = {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret", "name": "db", "uid": "u1",
          "controller": True}
OTHER_REF = {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web", "uid": "d1"}


@pytest.mark.parametrize("refs,want", [
    ([ES_REF], None),
    ([ES_REF, OTHER_REF], [OTHER_REF]),
    ([dict(ES_REF, uid="u-other")], "untouched"),           # ExternalSecret non lu (autre cluster, autre ns)
    ([dict(ES_REF, apiVersion="example.io/v1")], "untouched"),  # même sorte, autre groupe
    ([OTHER_REF], "untouched"),
    ([], "untouched"),
])
def test_orphan_patches_only_external_secret_owners(refs, want):
    """Mutation « `ownerReferences` d'un autre propriétaire retirées »."""
    out = ce.orphan_patches([_secret("team-a", "db", refs)], [_es("team-a", "db", "u1")])
    if want == "untouched":
        assert out == []
    else:
        assert out == [("team-a", "db", {"metadata": {"ownerReferences": want}})]


def test_orphan_patches_many_secrets():
    secrets = [_secret("team-a", "db", [ES_REF]), _secret("team-b", "db", [dict(ES_REF, uid="u2")]),
               _secret("team-b", "app", [OTHER_REF])]
    out = ce.orphan_patches(secrets, [_es("team-a", "db", "u1"), _es("team-b", "db", "u2")])
    assert [(n, m) for n, m, _p in out] == [("team-a", "db"), ("team-b", "db")]
    assert WITNESS not in json.dumps(out) and "eA==" not in json.dumps(out)


def test_stuck_patches_remove_only_the_eso_finalizer():
    es = _es("team-a", "db", "u1")
    es["metadata"]["finalizers"] = ["other/keep", ce.FINALIZER]
    assert ce.stuck_patches([es, _es("team-b", "x", "u2")]) == [
        ("team-a", "db", {"metadata": {"finalizers": ["other/keep"]}}),
        ("team-b", "x", {"metadata": {"finalizers": None}})]
    es2 = _es("team-c", "y", "u3")
    es2["metadata"]["finalizers"] = ["other/keep"]
    assert ce.stuck_patches([es2]) == []


# ---------------------------------------------------------------------------
# Écriture d'une valeur, exemple
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("args,ok", [
    (("1d905023", "team-a", "db"), True),
    (("1d90502", "team-a", "db"), False),
    (("1d905023", "kube-system", "db"), False),
    (("1d905023", "team-a", "../x"), False),
    (("1d905023", "team-a", "DB"), False),
])
def test_value_path(args, ok):
    if ok:
        assert ce.value_path(*args) == "1d905023/team-a/db"
    else:
        with pytest.raises(ValueError):
            ce.value_path(*args)


@pytest.mark.parametrize("fields,ok", [
    ({"password": "x"}, True),
    ({"a.b_c-d": "x", "user": "y"}, True),
    ({}, False),
    ({"pass word": "x"}, False),
    ({"password": ""}, False),
    ({"password": 3}, False),
    ({f"k{i}": "x" for i in range(33)}, False),
])
def test_check_fields(fields, ok):
    if ok:
        assert ce.check_fields(fields) == fields
    else:
        with pytest.raises(ValueError) as e:
            ce.check_fields(fields)
        assert "x" != str(e.value)


def test_example_refreshes_hourly_and_aims_at_the_namespace_path():
    ex = ce.example("team-a", "1d905023", "consoles/abc123def456", "db")
    assert ex["spec"]["refreshInterval"] == "1h"
    assert ex["spec"]["dataFrom"] == [{"extract": {"key": "consoles/abc123def456/eso/1d905023/team-a/db"}}]
    assert ex["spec"]["secretStoreRef"] == {"kind": "SecretStore", "name": "hops-openbao"}


# ---------------------------------------------------------------------------
# Option `external_secrets` du cluster enfant (capi_cluster)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    (True, {"namespaces": []}),
    ("true", {"namespaces": []}),
    (False, None),
    ({"namespaces": ["b", "a"]}, {"namespaces": ["a", "b"]}),
    ({"namespaces": ["a"], "chart": {"registry": "r", "package": "p", "version": "1"}},
     {"namespaces": ["a"], "chart": {"registry": "r", "package": "p", "version": "1", "item": "eso"}}),
])
def test_cluster_option(raw, want):
    spec = cc.normalize({"name": "c1", "external_secrets": raw})
    assert spec.get("external_secrets") == want


@pytest.mark.parametrize("raw,field", [
    ({"namespaces": ["kube-system"]}, "external_secrets.namespaces"),
    ({"other": 1}, "external_secrets.other"),
    ({"chart": {"registry": "r", "package": "p"}}, "external_secrets.chart.version"),
    ({"chart": {"registry": "r", "package": "p", "version": "1", "item": "flux"}}, "external_secrets.chart.item"),
    ("maybe", "external_secrets"),
])
def test_cluster_option_refusals(raw, field):
    with pytest.raises(ValueError) as e:
        cc.normalize({"name": "c1", "external_secrets": raw})
    assert str(e.value).startswith(field)
