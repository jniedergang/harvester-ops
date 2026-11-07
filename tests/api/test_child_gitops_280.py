"""2.8.0 (tâche 4) : bin/lib/child_gitops.py. Choix de l'outil (point 223),
sources admises (226), verrouillage de Flux (225), identifiants hors du
ConfigMap (227) ; formes relevées sur harvlab2 et Rancher v2.14.1 (plan
2.8.0, « Essais préalables (tâche 1) », points 3 et 4)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_manifests as mf  # noqa: E402
import child_gitops as cg  # noqa: E402

OCI = {"type": "oci", "package": "banc-280", "version": "1.0.0", "item": "demo", "registry": "amorcage"}
GIT = {"type": "git", "url": "ssh://git@172.16.1.11:2222/jniedergang/app.git", "secret": "depot"}
CHART = {"type": "chart", "package": "banc-280", "version": "1.0.0", "item": "podinfo", "registry": "amorcage"}


def entry(**over):
    e = {"name": "demo", "namespace": "equipe-a", "source": dict(OCI)}
    e.update(over)
    return cg.normalize(e)


@pytest.mark.parametrize("tool,imported,want", [
    ("auto", True, "fleet"), ("auto", False, "flux"), ("flux", True, "flux"), ("fleet", True, "fleet"),
])
def test_choose(tool, imported, want):
    assert cg.choose(tool, imported) == want


def test_fleet_without_import_is_refused():
    """Mutation « fleet choisi sans import »."""
    with pytest.raises(cg.GitopsError) as e:
        cg.choose("fleet", False)
    assert e.value.code == "gitops-fleet-needs-rancher"


@pytest.mark.parametrize("over,needle", [
    ({"name": "Demo"}, "gitops.name"),
    ({"tool": "argo"}, "gitops.tool"),
    ({"privileges": "root"}, "gitops.privileges"),
    ({"interval_minutes": 0}, "gitops.interval_minutes"),
    ({"interval_minutes": 1441}, "gitops.interval_minutes"),
    ({"namespace": "Equipe"}, "gitops.namespace"),
    ({"source": {"type": "s3"}}, "gitops.source.type"),
    ({"source": dict(OCI, branch="main")}, "gitops.source.branch: unknown key"),
    ({"source": {"type": "oci", "package": "p"}}, "gitops.source.version: required"),
    ({"source": {"type": "git", "url": "http://x/y.git"}}, "gitops.source.url"),
    ({"source": {"type": "git", "url": "https://u:p@host/y.git"}}, "gitops.source.url"),
    ({"source": {"type": "git", "url": "https://host/y.git", "path": "../etc"}}, "gitops.source.path"),
    ({"source": {"type": "git", "url": "https://host/y.git", "branch": "a..b"}}, "gitops.source.branch"),
    ({"source": {"type": "git", "url": "https://host/y.git", "secret": "A B"}}, "gitops.source.secret"),
    ({"prune": "yes"}, "gitops.prune"),
    ({"extra": 1}, "gitops.extra: unknown key"),
])
def test_normalize_refusals(over, needle):
    e = {"name": "demo", "namespace": "equipe-a", "source": dict(OCI)}
    e.update(over)
    with pytest.raises(ValueError) as err:
        cg.normalize(e)
    assert needle in str(err.value)


def test_normalize_defaults():
    e = entry(source=dict(GIT))
    assert (e["tool"], e["privileges"], e["prune"], e["interval_minutes"]) == ("auto", "namespace", True, 10)
    assert e["source"] == {"type": "git", "url": GIT["url"], "branch": "main", "path": "./", "secret": "depot"}


FACTS = {"tool": "flux", "flux_ready": True, "role": "operator", "secrets": {"depot"}, "published": True}


@pytest.mark.parametrize("over,facts,code", [
    ({}, {"tool": "fleet"}, "gitops-source-unsupported"),                       # Fleet + oci
    ({"source": dict(CHART)}, {}, "gitops-source-unsupported"),                 # Flux + chart
    ({}, {"flux_ready": False}, "gitops-flux-missing"),
    ({"namespace": "kube-system"}, {}, "gitops-namespace-not-team"),
    ({"namespace": "calico-system"}, {}, "gitops-namespace-not-team"),
    ({"namespace": "default"}, {}, "gitops-namespace-not-team"),
    ({"namespace": "hops-gitops"}, {}, "gitops-namespace-not-team"),
    ({"privileges": "cluster"}, {}, "gitops-privileges-admin-only"),
    ({"name": "a" * 35}, {}, "gitops-name-too-long"),
    ({"source": dict(GIT)}, {"secrets": set()}, "gitops-secret-missing"),
    ({}, {"published": False}, "gitops-package-not-published"),
])
def test_check_refusals(over, facts, code):
    assert code in [f["code"] for f in cg.check(entry(**over), dict(FACTS, **facts))]


@pytest.mark.parametrize("over,facts", [
    ({}, {}),
    ({"privileges": "cluster"}, {"role": "admin"}),
    ({"source": dict(GIT)}, {"tool": "fleet"}),
    ({"source": dict(CHART)}, {"tool": "fleet"}),
])
def test_check_accepts(over, facts):
    """Mutation « privileges cluster admis pour un opérateur » : le cas
    administrateur passe, l'opérateur non (ci-dessus)."""
    assert cg.check(entry(**over), dict(FACTS, **facts)) == []


# découverte d'un cluster enfant où Flux est installé
SCOPES = mf.parse_api_resources("""\
serviceaccounts sa v1 true ServiceAccount
secrets  v1 true Secret
rolebindings  rbac.authorization.k8s.io/v1 true RoleBinding
clusterrolebindings  rbac.authorization.k8s.io/v1 false ClusterRoleBinding
ocirepositories ocirepo source.toolkit.fluxcd.io/v1 true OCIRepository
gitrepositories gitrepo source.toolkit.fluxcd.io/v1 true GitRepository
kustomizations ks kustomize.toolkit.fluxcd.io/v1 true Kustomization
""")
PULL = {"host": "172.16.2.76:5000", "username": "pull", "password": "S3CRET-PULL-PW"}
CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def test_flux_objects_of_an_oci_entry():
    objs, secrets = cg.flux_objects(entry(), "oci://172.16.2.76:5000/harvester-ops/gitops/banc-280/demo",
                                    {"pull": PULL, "ca": CA})
    kinds = [(o["kind"], o["metadata"].get("namespace")) for o in objs]
    assert kinds == [("ServiceAccount", "hops-gitops"), ("RoleBinding", "equipe-a"),
                     ("OCIRepository", "hops-gitops"), ("Kustomization", "hops-gitops")]
    rb, src, ks = objs[1], objs[2], objs[3]
    assert rb["roleRef"]["name"] == "edit"
    assert rb["subjects"] == [{"kind": "ServiceAccount", "name": "hops-gitops-demo", "namespace": "hops-gitops"}]
    assert src["spec"] == {"url": "oci://172.16.2.76:5000/harvester-ops/gitops/banc-280/demo",
                           "ref": {"tag": "1.0.0"}, "interval": "10m",
                           "secretRef": {"name": "hops-gitops-demo-pull"}, "certSecretRef": {"name": "hops-gitops-demo-ca"}}
    assert ks["spec"] == {"sourceRef": {"kind": "OCIRepository", "name": "demo"}, "path": "./", "prune": True,
                          "interval": "10m", "targetNamespace": "equipe-a", "serviceAccountName": "hops-gitops-demo"}
    assert [(s["metadata"]["name"], s["type"]) for s in secrets] == [
        ("hops-gitops-demo-pull", "kubernetes.io/dockerconfigjson"), ("hops-gitops-demo-ca", "Opaque")]
    # Mutation « mot de passe écrit dans le ConfigMap » : jamais dans les objets
    assert "S3CRET-PULL-PW" not in json.dumps(objs)
    assert "S3CRET-PULL-PW" in json.dumps(secrets)
    assert mf.check(objs, "c", cg.item_of("demo"), scopes=SCOPES) == []


def test_flux_objects_of_a_git_entry_and_cluster_privileges():
    e = entry(source=dict(GIT), privileges="cluster")
    objs, secrets = cg.flux_objects(e, GIT["url"], {"git": {"identity": "PRIVATE-KEY", "known_hosts": "h k"}})
    assert objs[1] == cg.cluster_binding(e) and objs[1]["roleRef"]["name"] == "cluster-admin"
    assert objs[2]["kind"] == "GitRepository" and objs[2]["spec"]["ref"] == {"branch": "main"}
    assert secrets[0]["stringData"] == {"identity": "PRIVATE-KEY", "known_hosts": "h k"}
    assert "PRIVATE-KEY" not in json.dumps(objs)
    # la liaison admin n'est admise que rendue par la console (allow)
    assert "manifest-crb-admin" in [f["code"] for f in mf.check(objs, "c", scopes=SCOPES)]
    assert mf.check(objs, "c", scopes=SCOPES, allow=[cg.cluster_binding(e)]) == []


def test_flux_values_impose_the_multitenancy_lockdown():
    """Mutation « verrouillage multi-locataire retiré des valeurs »."""
    v = cg.flux_values({"multitenancy": {"enabled": False, "privileged": True}, "logLevel": "debug"})
    assert v["multitenancy"] == {"enabled": True, "defaultServiceAccount": "hops-gitops", "privileged": False}
    assert v["imageAutomationController"] == {"create": False} and v["logLevel"] == "debug"
    assert cg.flux_locked(v) and not cg.flux_locked({"multitenancy": {"enabled": True, "privileged": True}})
    assert not cg.flux_locked({})


def test_fleet_objects():
    g = entry(source=dict(GIT))
    o = cg.fleet_object(g, "harvlab2-rke2-banc", "harvlab2", "rke2-banc", "rke2-banc", secret_name="s", run="0123456789ab")
    assert o["kind"] == "GitRepo" and o["metadata"]["name"] == "hops-harvlab2-demo"
    assert o["spec"] == {"repo": GIT["url"], "branch": "main", "paths": ["./"], "targetNamespace": "equipe-a",
                         "targets": [{"clusterName": "harvlab2-rke2-banc"}], "clientSecretName": "s"}
    assert cg.fleet_ours(o, "harvlab2", "rke2-banc", "rke2-banc", g, "0123456789ab")
    assert not cg.fleet_ours(o, "harvlab2", "rke2-banc", "rke2-banc", g, "ffffffffffff")
    h = cg.fleet_object(entry(source=dict(CHART)), "f", "harvlab2", "n", "c",
                        chart_repo="oci://172.16.2.76:5000/charts/podinfo", secret_name="p")
    assert h["kind"] == "HelmOp" and h["spec"]["helm"] == {"releaseName": "demo",
                                                            "repo": "oci://172.16.2.76:5000/charts/podinfo",
                                                            "version": "1.0.0"}
    assert h["spec"]["helmSecretName"] == "p" and h["spec"]["namespace"] == "equipe-a"
    with pytest.raises(cg.GitopsError):
        cg.fleet_object(entry(), "f", "h", "n", "c")


def cond(ok, msg=""):
    return {"status": {"conditions": [{"type": "Ready", "status": "True" if ok else "False", "message": msg}]}}


@pytest.mark.parametrize("src,ks,rev,ready,needle", [
    (None, None, None, False, "not delivered"),
    (cond(False, "auth failed"), cond(True), None, False, "source: auth failed"),
    (cond(True), cond(False, "Forbidden"), None, False, "kustomization: Forbidden"),
    (cond(True), dict(cond(True), status=dict(cond(True)["status"], lastAppliedRevision="1.0.0@sha256:ab")),
     "1.0.0", True, "applied"),
    (cond(True), dict(cond(True), status=dict(cond(True)["status"], lastAppliedRevision="1.0.0@sha256:ab")),
     "1.0.1", False, "waiting for 1.0.1"),
])
def test_state_flux(src, ks, rev, ready, needle):
    st = cg.state_flux(src, ks, rev)
    assert st["ready"] is ready and needle in st["message"]


@pytest.mark.parametrize("status,ready", [
    ({"summary": {"desiredReady": 1, "ready": 1}}, True),
    ({"summary": {"desiredReady": 1, "ready": 0}, "conditions": [{"type": "Ready", "status": "False",
                                                                  "message": "NotReady"}]}, False),
    ({"summary": {"desiredReady": 0, "ready": 0}}, False),
    ({}, False),
])
def test_state_fleet(status, ready):
    assert cg.state_fleet({"status": status})["ready"] is ready
