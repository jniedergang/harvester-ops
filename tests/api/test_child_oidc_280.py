"""2.8.0 (tâche 3) : bin/lib/child_oidc.py. Configuration d'authentification
structurée vue sur rke2-banc (RKE2 v1.34.11, plan 2.8.0, « Essais préalables
(tâche 1) », point 5) : `apiserver.config.k8s.io/v1`, préfixes appliqués,
fichier monté par RKE2 lui-même (aucun `extraMounts`)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import child_oidc as co  # noqa: E402

# CA publique de banc (Dex de la tâche 1), lisible par ssl
CA = """-----BEGIN CERTIFICATE-----
MIIBjjCCATOgAwIBAgIUTWiTBpfMvQqP7fv3juLHHHkbIMgwCgYIKoZIzj0EAwIw
HDEaMBgGA1UEAwwRaG9wcy1kZXgtYmVuY2gtY2EwHhcNMjYxMTA3MTkwMjA1WhcN
MjYxMTA5MTkwMjA1WjAcMRowGAYDVQQDDBFob3BzLWRleC1iZW5jaC1jYTBZMBMG
ByqGSM49AgEGCCqGSM49AwEHA0IABOF3snb94iCCw+mgrozIP84YGKdwiBraU7bF
TBQvzhnhf1mD3fwtbC1fsR1oHyegy7HApqigPPUI7Pc08ohnKpWjUzBRMB0GA1Ud
DgQWBBQ5VdOKVqN40lySJsnLBlydbDMQjDAfBgNVHSMEGDAWgBQ5VdOKVqN40lyS
JsnLBlydbDMQjDAPBgNVHRMBAf8EBTADAQH/MAoGCCqGSM49BAMCA0kAMEYCIQDs
e84/ZSFLokkgcZDchO9MPfUvFCLvhj7To/SZPgiyMAIhAPp8it3YX1HL7gJYxagj
dvZjpkNjmqMvq5cCWtOIjNmx
-----END CERTIFICATE-----
"""
GOOD = {"issuer_url": "https://172.16.1.11:39215/dex", "audience": "hops-child"}


def codes(cfg, version="v1.34.11", rancher=False):
    return [f["code"] for f in co.check(cfg, version, rancher)[1]]


@pytest.mark.parametrize("over,code", [
    ({"issuer_url": "http://idp/dex"}, "oidc-issuer-invalid"),
    ({"issuer_url": "https://u:p@idp/dex"}, "oidc-issuer-invalid"),
    ({"issuer_url": "https://idp/dex?x=1"}, "oidc-issuer-invalid"),
    ({"issuer_url": "https://idp/" + "a" * 2050}, "oidc-issuer-invalid"),
    ({"audience": ""}, "oidc-audience-invalid"),
    ({"audience": "a b"}, "oidc-audience-invalid"),
    ({"audience": "a" * 257}, "oidc-audience-invalid"),
    ({"username_prefix": ""}, "oidc-prefix-required"),
    ({"groups_prefix": "  "}, "oidc-prefix-required"),
    ({"username_prefix": "system:x"}, "oidc-prefix-system"),
    ({"groups_prefix": "system:"}, "oidc-prefix-system"),
    ({"username_claim": "e mail"}, "oidc-claim-invalid"),
    ({"ca_pem": "-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----"}, "oidc-ca-unreadable"),
    ({"ca_pem": "x" * 70000}, "oidc-ca-unreadable"),
    ({"admin_group": "a\nb"}, "oidc-admin-group-invalid"),
    ({"admin_group": "g" * 129}, "oidc-admin-group-invalid"),
    ({"extra": 1}, "oidc-invalid"),
])
def test_refusals(over, code):
    assert code in codes(dict(GOOD, **over))


def test_defaults_and_a_good_config():
    cfg, found = co.check(dict(GOOD, ca_pem=CA, admin_group="hops-admins"), "v1.34.11")
    assert found == []
    assert (cfg["username_claim"], cfg["username_prefix"], cfg["groups_claim"], cfg["groups_prefix"]) == \
        ("email", "oidc:", "groups", "oidc:")


@pytest.mark.parametrize("version,want", [
    ("v1.29.9", None), ("v1.30.0", "apiserver.config.k8s.io/v1beta1"), ("v1.31.4", "apiserver.config.k8s.io/v1beta1"),
    ("v1.33.5", "apiserver.config.k8s.io/v1beta1"), ("v1.34.11", "apiserver.config.k8s.io/v1"),
    ("v1.35.0", "apiserver.config.k8s.io/v1"), ("garbage", None),
])
def test_api_version_by_kubernetes_version(version, want):
    assert co.api_version(version) == want
    assert ("oidc-version-too-old" in codes(GOOD, version)) is (want is None)


def test_rancher_and_oidc_together_warn():
    _, found = co.check(GOOD, "v1.34.11", rancher=True)
    assert [(f["code"], f["level"]) for f in found] == [("oidc-with-rancher", "warn")]


def test_auth_config_maps_claims_and_prefixes():
    cfg, _ = co.check(dict(GOOD, ca_pem=CA), "v1.34.11")
    ac = co.auth_config(cfg, "v1.34.11")
    assert ac["kind"] == "AuthenticationConfiguration" and ac["apiVersion"] == "apiserver.config.k8s.io/v1"
    jwt = ac["jwt"]
    assert len(jwt) == 1                                   # un seul fournisseur (point 228)
    assert jwt[0]["issuer"] == {"url": GOOD["issuer_url"], "audiences": ["hops-child"], "certificateAuthority": CA}
    assert jwt[0]["claimMappings"] == {"username": {"claim": "email", "prefix": "oidc:"},
                                       "groups": {"claim": "groups", "prefix": "oidc:"}}


def test_the_patch_is_a_root_0600_file_and_one_argument_without_oidc_flags():
    cfg, _ = co.check(GOOD, "v1.34.11")
    p = co.template_patch(cfg, "v1.34.11")
    f = p["files"][0]
    assert (f["path"], f["owner"], f["permissions"]) == (co.FILE, "root:root", "0600")
    assert json.loads(f["content"]) == co.auth_config(cfg, "v1.34.11")       # YAML relu (JSON) égal
    assert p["extraArgs"] == [f"authentication-config={co.FILE}"]
    assert set(p) == {"files", "extraArgs"}                                     # aucun extraMounts
    assert not [a for a in p["extraArgs"] if a.startswith(("oidc-", "--oidc-"))]


@pytest.mark.parametrize("existing", [
    {"serverConfig": {"kubeAPIServer": {"extraArgs": ["authentication-config=/x"]}}},
    {"serverConfig": {"kubeAPIServer": {"extraArgs": ["--oidc-issuer-url=https://x"]}}},
    {"files": [{"path": co.FILE, "content": "x"}]},
])
def test_an_existing_argument_or_file_is_never_overwritten(existing):
    cfg, _ = co.check(GOOD, "v1.34.11")
    with pytest.raises(ValueError) as e:
        co.apply_patch(json.loads(json.dumps(existing)), co.template_patch(cfg, "v1.34.11"))
    assert str(e.value).startswith("oidc-arg-conflict")


def test_apply_patch_keeps_what_is_there():
    cfg, _ = co.check(GOOD, "v1.34.11")
    spec = {"serverConfig": {"cni": "calico", "kubeAPIServer": {"extraArgs": ["audit-log-maxage=7"]}},
            "files": [{"path": "/etc/rancher/rke2/registries.yaml"}]}
    co.apply_patch(spec, co.template_patch(cfg, "v1.34.11"))
    assert spec["serverConfig"]["kubeAPIServer"]["extraArgs"] == ["audit-log-maxage=7",
                                                                  f"authentication-config={co.FILE}"]
    assert [f["path"] for f in spec["files"]] == ["/etc/rancher/rke2/registries.yaml", co.FILE]
    assert "extraMounts" not in spec["serverConfig"]["kubeAPIServer"]


def test_the_kubeconfig_of_people_has_no_credential():
    cfg, _ = co.check(GOOD, "v1.34.11")
    text = co.kubeconfig(cfg, "https://172.16.11.26:6443", "Q0E=", "rke2-banc")
    kc = json.loads(text)
    user = kc["users"][0]["user"]
    assert set(user) == {"exec"}
    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from keys(v)
    for k in ("token", "password", "client-key-data", "client-certificate-data", "tokenFile", "username"):
        assert k not in set(keys(kc))
    assert "client-secret" not in text
    assert user["exec"]["command"] == "kubectl" and user["exec"]["args"][:2] == ["oidc-login", "get-token"]
    assert f"--oidc-issuer-url={GOOD['issuer_url']}" in user["exec"]["args"]
    assert "--oidc-client-id=hops-child" in user["exec"]["args"]
    assert kc["clusters"][0]["cluster"] == {"server": "https://172.16.11.26:6443", "certificate-authority-data": "Q0E="}


def test_public_settings_round_trip_without_the_ca():
    cfg, _ = co.check(dict(GOOD, ca_pem=CA, admin_group="ops"), "v1.34.11")
    obj = {"metadata": {"annotations": {co.ANNOTATION: co.public(cfg)}}}
    got = co.of_cluster(obj)
    assert got == {"issuer_url": GOOD["issuer_url"], "audience": "hops-child", "username_prefix": "oidc:",
                   "groups_prefix": "oidc:", "admin_group": "ops"}
    assert "BEGIN" not in co.public(cfg)
    assert co.of_cluster({"metadata": {"annotations": {co.ANNOTATION: "{"}}}) is None
