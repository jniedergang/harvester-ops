"""v1.52.0 (B2) : les services sur les clusters Cluster API, la logique.

Un service est un HelmChartProxy de CAAPH (v0.6.4) qui vise un cluster par
une étiquette ; ce module rend le manifeste, contrôle la demande et lit
l'état (HelmChartProxy + HelmReleaseProxy).
"""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import capi_services as sv  # noqa: E402

FACTS = {"caaph": True, "clusters": ["essai/essai"], "services": []}


def req(**over):
    base = {"service": "coredns", "cluster": "essai/essai"}
    base.update(over)
    return sv.normalize(base)


def codes(found, level=None):
    return [f["code"] for f in found if level is None or f["level"] == level]


def test_the_catalog_is_pinned():
    cat = {c["key"]: c for c in sv.catalog()}
    assert set(cat) == {"coredns", "podinfo"}
    assert all(c["version"] and c["repo"].startswith("https://") for c in cat.values())


def test_a_catalog_request_is_filled_from_the_catalog():
    s = req(params={"upstream": "172.16.0.1"})
    assert s["name"] == "coredns" and s["chart"] == "coredns" and s["version"] == "1.47.1"
    assert s["namespace"] == "dns" and s["params"]["upstream"] == "172.16.0.1"
    assert s["params"]["ipam"] == "dhcp"
    with pytest.raises(ValueError):
        sv.normalize({"service": "coredns", "chartName": "x"})


def test_dns_values_render_with_and_without_local_records():
    v = yaml.safe_load(sv.render_values(req(params={"upstream": "172.16.0.1",
                                                    "hosts": "172.16.3.60 web.lab\n172.16.3.61 db.lab"})))
    plugins = {p["name"]: p for p in v["servers"][0]["plugins"]}
    assert plugins["forward"]["parameters"] == ". 172.16.0.1"
    assert plugins["hosts"]["configBlock"] == "172.16.3.60 web.lab\n172.16.3.61 db.lab\nfallthrough"
    assert v["serviceType"] == "LoadBalancer"
    assert v["servers"][0]["zones"][0]["use_tcp"] is True      # DNS en TCP aussi (grosses réponses)
    assert v["service"]["annotations"]["cloudprovider.harvesterhci.io/ipam"] == "dhcp"
    v = yaml.safe_load(sv.render_values(req()))
    assert {p["name"]: p for p in v["servers"][0]["plugins"]}["hosts"]["configBlock"] == "fallthrough"


def test_a_good_request_passes():
    assert sv.blocking(sv.check(req(), FACTS)) == []


@pytest.mark.parametrize("over, facts, code", [
    ({}, {"caaph": False}, "caaph-missing"),
    ({"service": "nope"}, {}, "service-unknown"),
    ({"name": "Bad Name"}, {}, "invalid-name"),
    ({"cluster": "other/other"}, {}, "cluster-missing"),
    ({"service": "custom", "repo": "ftp://x", "chart": "a"}, {}, "invalid-repo"),
    ({"service": "custom", "repo": "https://charts.example", "chart": ""}, {}, "invalid-chart"),
    ({"namespace": "Bad_NS"}, {}, "invalid-namespace"),
    ({"params": {"ipam": "static"}}, {}, "invalid-ipam"),
    ({"values": "a: [unclosed"}, {}, "invalid-values"),
])
def test_each_blocker(over, facts, code):
    assert code in codes(sv.check(req(**over), dict(FACTS, **facts)), "block")


def test_warnings():
    found = sv.check(req(service="custom", repo="https://charts.example", chart="app", version=""),
                     dict(FACTS, services=["essai/custom"]))
    assert {"version-floating", "service-exists"} <= set(codes(found, "warn"))


def test_the_manifest_selects_the_cluster_by_label():
    m = sv.manifest(req(params={"upstream": "172.16.0.1"}))
    assert m["apiVersion"] == "addons.cluster.x-k8s.io/v1alpha1" and m["kind"] == "HelmChartProxy"
    assert m["metadata"]["namespace"] == "essai" and m["metadata"]["name"] == "coredns"
    assert m["spec"]["clusterSelector"] == {"matchLabels": {"harvester-ops.io/svc-coredns": "on"}}
    assert m["spec"]["chartName"] == "coredns" and m["spec"]["version"] == "1.47.1"
    assert m["spec"]["options"]["install"]["createNamespace"] is True
    assert "172.16.0.1" in m["spec"]["valuesTemplate"]
    assert "{{ .Cluster.metadata.name }}" in sv.manifest(req(service="podinfo"))["spec"]["valuesTemplate"]


def test_state_reads_proxies_and_releases():
    hcp = {"metadata": {"name": "coredns", "namespace": "essai", "labels": {sv.MANAGED: "true"},
                        "annotations": {"harvester-ops.io/service": "coredns"}},
           "spec": {"chartName": "coredns", "version": "1.47.1", "repoURL": "https://coredns.github.io/helm",
                    "namespace": "dns"},
           "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    hrp = {"metadata": {"name": "coredns-essai-x", "namespace": "essai",
                        "ownerReferences": [{"kind": "HelmChartProxy", "name": "coredns"}]},
           "spec": {"clusterRef": {"name": "essai"}},
           "status": {"status": "deployed", "revision": 2,
                      "conditions": [{"type": "Ready", "status": "True"}]}}
    other = {"metadata": {"name": "x", "namespace": "else",
                          "ownerReferences": [{"kind": "HelmChartProxy", "name": "coredns"}]},
             "spec": {"clusterRef": {"name": "else"}}, "status": {}}
    st = sv.state([hcp], [hrp, other])
    assert st[0]["ready"] and st[0]["service"] == "coredns" and st[0]["managed"]
    assert st[0]["releases"] == [{"cluster": "essai", "status": "deployed", "revision": 2,
                                  "ready": True, "current": True, "message": ""}]
    # relu par CAAPH ou pas encore
    hrp["metadata"]["generation"], hrp["status"]["observedGeneration"] = 3, 2
    assert sv.state([hcp], [hrp])[0]["releases"][0]["current"] is False
    assert sv.current({"metadata": {"generation": 1}, "status": {}}) is False
    assert sv.current({"metadata": {"generation": 2}, "status": {"observedGeneration": 2}}) is True


# ---------------------------------------------------------------------------
# Les points d'entrée de la console
# ---------------------------------------------------------------------------
sys.path.insert(0, str(ROOT / "web"))


def test_the_services_view_says_when_the_cluster_cannot_be_reached(api):
    status, body = api("GET", "/api/capi/harv-fake/services")
    assert status == 200, body
    assert body["unreachable"] is True and body["caaph"] is False
    assert {c["key"] for c in body["catalog"]} == {"coredns", "podinfo"}


def test_malformed_service_requests_are_refused_before_the_cluster(api):
    for body in ({"service": "coredns", "chartName": "x"}, ["not", "an", "object"]):
        status, out = api("POST", "/api/capi/harv-fake/service-check", json_body=body, expect_status=None)
        assert status == 400, (body, out)
        status, out = api("POST", "/api/capi/harv-fake/service-deploy", json_body=body, expect_status=None)
        assert status == 400, (body, out)
    status, _ = api("DELETE", "/api/capi/harv-fake/service/Bad_NS/coredns", expect_status=None)
    assert status == 400


def test_service_writes_need_an_operator():
    import app as wapp
    assert wapp.required_role_for("/api/capi/harv1/services", "GET") == "viewer"
    for method, path in (("POST", "/api/capi/harv1/service-check"), ("POST", "/api/capi/harv1/service-deploy"),
                         ("DELETE", "/api/capi/harv1/service/essai/coredns")):
        assert wapp.required_role_for(path, method) == "operator"


def test_caaph_is_an_optional_provider_of_the_bundle():
    comps = yaml.safe_load((ROOT / "scripts" / "capi-components.yaml").read_text())
    text = yaml.safe_dump(comps)
    assert "cluster-api-helm-controller:v0.6.4" in text and "caaph-system" in text


def test_a_cluster_without_kube_vip_is_told_it_will_get_it():
    found = sv.check(req(), dict(FACTS, no_lb=["essai/essai"]))
    assert ("lb-added", "ok") in [(f["code"], f["level"]) for f in found]
    assert sv.blocking(found) == []
