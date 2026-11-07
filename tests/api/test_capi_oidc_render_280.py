"""2.8.0 (tâche 3) : l'option `oidc` du cluster enfant dans le rendu
(capi_cluster.postprocess), le groupe administrateur (child_policies) et
son passage par le socle `hops-policies`, jamais par un paquet."""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_cluster as cc  # noqa: E402
import capi_manifests as mf  # noqa: E402
import capi_stack as cs  # noqa: E402
import child_oidc as co  # noqa: E402
import child_policies as cp  # noqa: E402

OIDC = {"issuer_url": "https://idp.example/dex", "audience": "hops-child", "admin_group": "hops-admins"}
DOCS = [
    {"apiVersion": "cluster.x-k8s.io/v1beta1", "kind": "Cluster", "metadata": {"name": "c", "namespace": "c"},
     "spec": {"topology": {"variables": []}}},
    {"apiVersion": "controlplane.cluster.x-k8s.io/v1beta2", "kind": "RKE2ControlPlaneTemplate",
     "metadata": {"name": "harvester-rke2-control-plane", "namespace": "c"},
     "spec": {"template": {"spec": {"serverConfig": {"cni": "calico", "cloudProviderName": "external"}}}}},
    {"apiVersion": "bootstrap.cluster.x-k8s.io/v1beta2", "kind": "RKE2ConfigTemplate",
     "metadata": {"name": "harvester-rke2-worker", "namespace": "c"}, "spec": {"template": {"spec": {}}}},
]


def spec(**over):
    s = cc.normalize(dict({"name": "c", "k8s_version": "v1.34.11"}, **over))
    return s


def by_kind(docs, kind):
    return next(d for d in docs if d["kind"] == kind)


def test_oidc_goes_to_the_control_plane_template_only():
    out = cc.postprocess(copy.deepcopy(DOCS), spec(oidc=OIDC))
    cpt = by_kind(out, "RKE2ControlPlaneTemplate")["spec"]["template"]["spec"]
    assert cpt["serverConfig"]["kubeAPIServer"] == {"extraArgs": [f"authentication-config={co.FILE}"]}
    assert [f["path"] for f in cpt["files"]] == [co.FILE]
    assert by_kind(out, "RKE2ConfigTemplate")["spec"]["template"]["spec"] == {}          # workers inchangés
    ann = by_kind(out, "Cluster")["metadata"]["annotations"]
    assert json.loads(ann[co.ANNOTATION])["admin_group"] == "hops-admins"


def test_without_oidc_nothing_changes():
    out = cc.postprocess(copy.deepcopy(DOCS), spec())
    assert "kubeAPIServer" not in by_kind(out, "RKE2ControlPlaneTemplate")["spec"]["template"]["spec"]["serverConfig"]
    assert co.ANNOTATION not in (by_kind(out, "Cluster")["metadata"].get("annotations") or {})


def test_oidc_and_registries_files_live_together():
    regs = {"files": [{"path": "/etc/rancher/rke2/registries.yaml", "content": "{}"}], "secrets": []}
    out = cc.postprocess(copy.deepcopy(DOCS), spec(oidc=OIDC), registries=regs)
    cpt = by_kind(out, "RKE2ControlPlaneTemplate")["spec"]["template"]["spec"]
    assert [f["path"] for f in cpt["files"]] == [co.FILE, "/etc/rancher/rke2/registries.yaml"]
    assert [f["path"] for f in by_kind(out, "RKE2ConfigTemplate")["spec"]["template"]["spec"]["files"]] == \
        ["/etc/rancher/rke2/registries.yaml"]


@pytest.mark.parametrize("over,needle", [
    ({"oidc": dict(OIDC, username_prefix="")}, "oidc-prefix-required"),
    ({"oidc": OIDC, "k8s_version": "v1.29.4"}, "oidc-version-too-old"),
])
def test_a_refused_oidc_option_stops_the_render(over, needle):
    with pytest.raises(ValueError) as e:
        cc.postprocess(copy.deepcopy(DOCS), spec(**over))
    assert needle in str(e.value)


def test_an_argument_already_in_the_template_is_a_conflict():
    docs = copy.deepcopy(DOCS)
    docs[1]["spec"]["template"]["spec"]["serverConfig"]["kubeAPIServer"] = {"extraArgs": ["authentication-config=/x"]}
    with pytest.raises(ValueError) as e:
        cc.postprocess(docs, spec(oidc=OIDC))
    assert str(e.value).startswith("oidc-arg-conflict")


@pytest.mark.parametrize("over,codes", [
    ({"rancher_import": True, "rancher_external": True}, {"rancher-import-conflict"}),
    ({"rancher_external": True, "oidc": OIDC}, {"oidc-with-rancher"}),
    ({"oidc": dict(OIDC, issuer_url="http://x")}, {"oidc-issuer-invalid"}),
    ({"rancher_import": True}, set()),
    ({"rancher_external": "true"}, set()),
])
def test_access_findings(over, codes):
    assert {f["code"] for f in cc.access_findings(spec(**over))} == codes


def test_oidc_must_be_an_object():
    with pytest.raises(ValueError):
        cc.normalize({"name": "c", "oidc": "https://x"})


def test_admin_binding_is_the_only_cluster_admin_binding_and_only_with_a_group():
    b = cp.admin_binding("oidc:", "hops-admins")
    assert b["roleRef"]["name"] == "cluster-admin"
    assert b["subjects"] == [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": "oidc:hops-admins"}]
    assert cp.admin_binding("oidc:", None) is None and cp.admin_binding("oidc:", "") is None
    assert cp.group_with_prefix("equipe-a", "oidc:") == "oidc:equipe-a"
    assert cp.group_with_prefix("oidc:equipe-a", "oidc:") == "oidc:equipe-a"


def test_a_package_cluster_admin_binding_is_still_refused():
    """Mutation « CRB admin admise depuis un paquet » : seule la liaison
    rendue par la console, identique, passe."""
    admin = cp.admin_binding("oidc:", "hops-admins")
    assert [f["code"] for f in mf.check([admin], "c")] == ["manifest-crb-admin"]
    assert mf.check([admin], "c", allow=[admin]) == []
    forged = copy.deepcopy(admin)
    forged["subjects"][0]["name"] = "oidc:someone-else"
    assert [f["code"] for f in mf.check([forged], "c", allow=[admin])] == ["manifest-crb-admin"]


@pytest.fixture(scope="module")
def cli():
    s = importlib.util.spec_from_file_location("harvester_capi_oidc", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


class Mgmt:
    def __init__(self, cluster):
        self.cluster = cluster
        self.kubeconfig = "/nonexistent"

    def get(self, kind, ns, name):
        return self.cluster if kind == cs.K_CLUSTER else None


def annotated():
    cfg, _ = co.check(OIDC, "v1.34.11")
    return {"metadata": {"name": "c", "namespace": "c", "annotations": {co.ANNOTATION: co.public(cfg)}}}


def test_policies_of_an_oidc_cluster_carry_the_admin_binding(cli, tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"cluster": "c/c", "policies": {}}))
    s = cli._with_admin(Mgmt(annotated()), cli.manifest_spec_from(str(p)))
    objs, found = cli.manifest_objects(s)
    assert found == [] and objs == [cp.admin_binding("oidc:", "hops-admins")]
    s = cli._with_admin(Mgmt({"metadata": {}}), cli.manifest_spec_from(str(p)))
    assert cli.manifest_objects(s) == ([], [])


def test_a_package_item_never_gets_the_admin_binding(cli, tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"cluster": "c/c", "package": "banc", "version": "1.0.0", "item": "essai"}))
    s = cli._with_admin(Mgmt(annotated()), cli.manifest_spec_from(str(p)))
    assert "admin_binding" not in s


def test_create_delivers_the_admin_group_once_available(cli, monkeypatch):
    got = []
    monkeypatch.setattr(cli, "deliver", lambda kube, ns, c, item, pkg, ver, objs, origin, by, t, secrets=():
                        got.append((ns, c, item, objs)) or 0)
    monkeypatch.setattr(cli, "rancher_import", lambda *a, **k: pytest.fail("no import asked"))
    assert cli._after_create(Mgmt(annotated()), spec(oidc=OIDC), "harvlab2", {}, 900) == 0
    assert got == [("c", "c", cp.ITEM, [cp.admin_binding("oidc:", "hops-admins")])]
    got.clear()
    assert cli._after_create(Mgmt(annotated()), spec(oidc={k: v for k, v in OIDC.items() if k != "admin_group"}),
                             "harvlab2", {}, 900) == 0
    assert got == []
