"""2.6.0 (tâche 10) : un service de Cluster API > Services tiré d'un chart
publié dans un registre déclaré : `oci://<registre>/charts`, compte de
LECTURE au format `config.json` et CA du registre (essais préalables :
`credentials` et `tlsConfig.caSecret` de CAAPH v0.6.4), valeurs du paquet
puis du formulaire, Secrets retirés avec le dernier service qui s'en sert."""

import argparse
import base64
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import capi_cluster as cc  # noqa: E402
import capi_services as sv  # noqa: E402
import registries as rg  # noqa: E402
from test_capi_services_cli import Kube, cli, cluster_obj, release  # noqa: E402,F401

PULL_PW, PUSH_PW = "pull-Pw-31415", "push-Pw-27182"
CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
SRC = {"registry": "amorcage", "package": "socle", "version": "2026.11", "item": "podinfo"}


def spec(**kw):
    raw = {"cluster": "web/web", "source": dict(SRC)}
    raw.update(kw)
    return sv.normalize(raw)


# ---------------------------------------------------------------------------
# Logique pure
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("source", [
    {"registry": "a"},                                   # clés manquantes
    dict(SRC, extra="x"),                                # clé inconnue
    dict(SRC, item=""),
    "amorcage/socle",
])
def test_a_malformed_source_is_refused(source):
    with pytest.raises(ValueError, match="source"):
        sv.normalize({"cluster": "web/web", "source": source})


def test_a_source_makes_a_registry_service_named_after_its_item():
    s = spec()
    assert s["service"] == sv.REGISTRY and s["name"] == "podinfo" and s["source"] == SRC
    assert sv.normalize({"service": "registry", "cluster": "web/web"})["service"] == "registry"
    found = sv.check(sv.normalize({"service": "registry", "cluster": "web/web"}), {"caaph": True})
    assert "service-unknown" in [f["code"] for f in found], "registry without a source"


def test_the_source_resolves_to_an_oci_chart_with_merged_values():
    s = sv.resolve_source(spec(values="ui:\n  color: red\nreplicas: 2\n"), "172.16.2.76",
                          {"name": "podinfo", "version": "6.9.2"},
                          {"ui": {"message": "hi", "color": "blue"}, "service": {"type": "ClusterIP"}})
    assert (s["repo"], s["chart"], s["version"]) == ("oci://172.16.2.76/charts", "podinfo", "6.9.2")
    assert yaml.safe_load(s["values"]) == {"ui": {"message": "hi", "color": "red"},
                                          "service": {"type": "ClusterIP"}, "replicas": 2}
    with pytest.raises(ValueError, match="mapping"):
        sv.resolve_source(spec(values="- a\n"), "h", {"name": "p", "version": "1"}, {})


@pytest.mark.parametrize("ca", [True, False])
def test_the_manifest_carries_credentials_and_the_ca(ca):
    s = dict(spec(), repo="oci://h/charts", chart="podinfo", version="6.9.2")
    m = sv.manifest(s, {"name": "amorcage", "ca": ca})
    assert m["spec"]["credentials"] == {"secret": {"name": "web-chart-amorcage", "namespace": "web"},
                                        "key": "config.json"}
    if ca:                                              # M-ca-missing
        assert m["spec"]["tlsConfig"] == {"caSecret": {"name": "web-registry-amorcage-ca", "namespace": "web"}}
    else:
        assert "tlsConfig" not in m["spec"]
    assert "insecureSkipTLSVerify" not in json.dumps(m)
    assert m["metadata"]["annotations"]["harvester-ops.io/source"] == "amorcage/socle/2026.11/podinfo"


def test_a_catalog_service_is_unchanged():
    s = sv.normalize({"service": "podinfo", "cluster": "web/web"})
    m = sv.manifest(s)
    assert "credentials" not in m["spec"] and "tlsConfig" not in m["spec"]
    assert m["metadata"]["annotations"] == {"harvester-ops.io/service": "podinfo"}


def test_the_chart_secret_is_a_docker_config_with_the_read_account():
    docs = sv.registry_secrets(spec(), "amorcage", "172.16.2.76", {"username": "pull", "password": PULL_PW}, CA,
                               labels={cc.GENERATED: "true", rg.LABEL: "amorcage"})
    auth, ca = docs
    conf = json.loads(auth["stringData"]["config.json"])
    assert base64.b64decode(conf["auths"]["172.16.2.76"]["auth"]).decode() == f"pull:{PULL_PW}"
    assert ca["stringData"] == {"ca.crt": CA} and ca["type"] == "Opaque"
    for d in docs:
        assert d["metadata"]["labels"] == {cc.GENERATED: "true", rg.LABEL: "amorcage",
                                           "harvester-ops.io/svc-podinfo": "on"}
    assert len(sv.registry_secrets(spec(), "amorcage", "h", {"username": "u", "password": "p"})) == 1


def _hcp(name, auth="web-chart-amorcage", ca="web-registry-amorcage-ca"):
    sp = {}
    if auth:
        sp["credentials"] = {"secret": {"name": auth}}
    if ca:
        sp["tlsConfig"] = {"caSecret": {"name": ca}}
    return {"metadata": {"name": name}, "spec": sp}


def _sec(name, svc=True):
    return {"metadata": {"name": name, "labels": dict({cc.GENERATED: "true"},
                                                      **({"harvester-ops.io/svc-x": "on"} if svc else {}))}}


@pytest.mark.parametrize("remaining,secrets,gone", [
    ([], [_sec("web-chart-amorcage"), _sec("web-registry-amorcage-ca")],
     ["web-chart-amorcage", "web-registry-amorcage-ca"]),                       # le dernier service
    ([_hcp("other")], [_sec("web-chart-amorcage"), _sec("web-registry-amorcage-ca")], []),   # partagés
    ([], [_sec("web-chart-amorcage"), _sec("web-registry-amorcage-ca", svc=False)],
     ["web-chart-amorcage"]),                                                   # CA du cluster gardée
    ([_hcp("other", ca=None)], [_sec("web-chart-amorcage"), _sec("web-registry-amorcage-ca")],
     ["web-registry-amorcage-ca"]),
    ([], [_sec("autre")], []),                                                  # pas désigné
])
def test_registry_secrets_leave_with_their_last_service(remaining, secrets, gone):
    assert sv.secrets_to_remove(_hcp("podinfo"), remaining, secrets) == gone


# ---------------------------------------------------------------------------
# harvester-capi : constats et déploiement
# ---------------------------------------------------------------------------
def _chart_tgz(path, name="podinfo", version="6.9.2"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = f"apiVersion: v2\nname: {name}\nversion: {version}\n".encode()
        info = tarfile.TarInfo(f"{name}/Chart.yaml")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


@pytest.fixture
def world(tmp_path, monkeypatch, cli):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    pkg = tmp_path / "artefacts.d" / "socle-2026.11"
    _chart_tgz(pkg / "charts" / "podinfo-6.9.2.tgz")
    (pkg / "artefacts.yaml").write_text(json.dumps({"name": "socle", "version": "2026.11", "items": [
        {"name": "podinfo", "kind": "chart", "chart": "charts/podinfo-6.9.2.tgz", "values": {"ui": {"color": "blue"}}},
        {"name": "imgs", "kind": "images", "images": []}]}))

    def declare(**over):
        d = {"kind": "external", "endpoint": "172.16.2.76", "usage": ["images", "charts"], "ca_pem": CA,
             "mirrors": ["ghcr.io"], "clusters": ["harvlab2"],
             "published": [{"package": "socle", "version": "2026.11"}]}
        d.update(over)
        if "ca_pem" in d and d["ca_pem"] is None:
            d.pop("ca_pem")
        rg.delete(tmp_path, "amorcage")
        rg.save(tmp_path, "amorcage", d)
    monkeypatch.setattr(rg, "ca_readable", lambda pem: True)
    declare()
    return declare


def _secret(user, pw):
    return {"data": {"username": base64.b64encode(user.encode()).decode(),
                     "password": base64.b64encode(pw.encode()).decode()}}


def _kube(cli, mirrored=True, attached=True, on_apply=None):
    cs = cli.cs
    objs = {(cs.K_CLUSTER, "web", "web"): cluster_obj("web", "web")}
    if attached:
        objs[("secrets", rg.NAMESPACE, "amorcage-pull")] = _secret("pull", PULL_PW)
        objs[("secrets", rg.NAMESPACE, "amorcage-push")] = _secret("push", PUSH_PW)
    if mirrored:
        objs[("secrets", "web", "web-registry-amorcage-auth")] = {"metadata": {"name": "web-registry-amorcage-auth"}}
        objs[("secrets", "web", "web-registry-amorcage-ca")] = {"metadata": {"name": "web-registry-amorcage-ca"}}
    return Kube(objs, on_apply=on_apply)


@pytest.mark.parametrize("setup,source,codes", [
    ({}, SRC, []),
    ({"usage": ["images"]}, SRC, [("service-registry-no-charts", "block")]),
    ({"clusters": ["harv4"]}, SRC, [("service-registry-not-attached", "block")]),
    ({}, dict(SRC, registry="absent"), [("service-registry-unknown", "block")]),
    ({}, dict(SRC, version="2026.12"), [("service-package-missing", "block")]),
    ({}, dict(SRC, item="imgs"), [("service-item-not-chart", "block")]),
    ({}, dict(SRC, item="nope"), [("service-item-not-chart", "block")]),
    ({"published": []}, SRC, [("service-chart-not-published", "warn")]),
    ({"mirrored": False}, SRC, [("service-registry-not-mirrored", "warn")]),
])
def test_service_source_findings(cli, world, setup, source, codes):
    setup = dict(setup)
    mirrored = setup.pop("mirrored", True)
    world(**setup)
    s = sv.normalize({"cluster": "web/web", "source": source})
    resolved, found, _ = cli.service_source(_kube(cli, mirrored=mirrored), s, "harvlab2")
    assert [(f["code"], f["level"]) for f in found] == codes
    if not codes:
        assert resolved["repo"] == "oci://172.16.2.76/charts" and resolved["chart"] == "podinfo"
        assert resolved["version"] == "6.9.2" and yaml.safe_load(resolved["values"]) == {"ui": {"color": "blue"}}


def _deploy(cli, monkeypatch, tmp_path, kube, **body):
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    monkeypatch.setattr(cli, "_caaph_ready", lambda k: True)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    p = tmp_path / "svc.json"
    p.write_text(json.dumps(dict({"cluster": "web/web", "source": SRC}, **body)))
    return cli.cmd_service_deploy(argparse.Namespace(spec=str(p), timeout=60, harvester_name="harvlab2"))


@pytest.mark.parametrize("mirrored,ca_posed", [(True, False), (False, True)])
def test_deploy_poses_the_read_account_and_reuses_the_cluster_ca(cli, world, monkeypatch, tmp_path, mirrored,
                                                                 ca_posed):
    kube = _kube(cli, mirrored=mirrored, on_apply=lambda k: k.objs.__setitem__(
        (cli.sv.K_HRP, "web", "r"), release("web", "podinfo", "web")))
    assert _deploy(cli, monkeypatch, tmp_path, kube) == cli.EXIT_OK
    kinds = [(d["kind"], d["metadata"]["name"]) for d in kube.applied]
    assert ("Secret", "web-chart-amorcage") in kinds
    assert (("Secret", "web-registry-amorcage-ca") in kinds) is ca_posed
    blob = json.dumps(kube.applied)
    assert PUSH_PW not in blob, "M-push-creds: the write account went to CAAPH"
    auth = base64.b64encode(f"pull:{PULL_PW}".encode()).decode()
    assert auth in blob
    hcp = next(d for d in kube.applied if d["kind"] == "HelmChartProxy")
    assert hcp["spec"]["repoURL"] == "oci://172.16.2.76/charts" and hcp["spec"]["version"] == "6.9.2"
    assert hcp["spec"]["tlsConfig"]["caSecret"]["name"] == "web-registry-amorcage-ca"


def test_a_blocked_source_writes_nothing(cli, world, monkeypatch, tmp_path):
    kube = _kube(cli, attached=False)
    assert _deploy(cli, monkeypatch, tmp_path, kube) == cli.EXIT_BLOCKED
    assert kube.applied == []


def test_remove_takes_the_service_secrets_away(cli, monkeypatch):
    sv_, cs = cli.sv, cli.cs
    label = sv_.label_of("podinfo")
    lab = {cc.GENERATED: "true", label: "on"}
    kube = Kube({(cs.K_CLUSTER, "web", "web"): cluster_obj("web", "web", {label: "on"}),
                 (sv_.K_HCP, "web", "podinfo"): dict(_hcp("podinfo"), metadata={"namespace": "web", "name": "podinfo"}),
                 ("secrets", "web", "web-chart-amorcage"): {"metadata": {"name": "web-chart-amorcage", "labels": lab}},
                 ("secrets", "web", "web-registry-amorcage-ca"): {"metadata": {"name": "web-registry-amorcage-ca",
                                                                              "labels": {cc.GENERATED: "true"}}}})
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.cmd_service_remove(argparse.Namespace(name="web/podinfo", timeout=60)) == cli.EXIT_OK
    assert ("secrets", "web", "web-chart-amorcage") in kube.deleted
    assert ("secrets", "web", "web-registry-amorcage-ca") not in kube.deleted, "the cluster CA stays"
