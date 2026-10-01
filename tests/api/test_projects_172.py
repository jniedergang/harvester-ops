"""v1.72.0 : les projets Rancher d'un cluster Harvester : corps de l'API de
Rancher avec les règles de son webhook vérifiées avant d'écrire, quantités
écrites comme le formulaire de Rancher (millicœurs, Mi), appartenance d'un
namespace lue dans l'annotation (pas le label) et comparée au cluster, quota
de namespace borné par son projet, Rancher retrouvé dans le kubeconfig de
la session."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin" / "lib"))
import hv_projects as pj  # noqa: E402


def test_quantities_are_written_as_rancher_s_form_writes_them():
    assert pj.normalize("limitsCpu", "2") == "2000m" and pj.normalize("limitsCpu", "500m") == "500m"
    assert pj.normalize("limitsMemory", "4Gi") == "4096Mi" and pj.normalize("requestsStorage", "100Gi") == "102400Mi"
    assert pj.normalize("pods", "10") == "10"
    with pytest.raises(ValueError, match="not a quantity"):
        pj.normalize("limitsMemory", "four gigs")
    with pytest.raises(ValueError, match="unknown resource"):
        pj.normalize("gpus", "1")


def test_a_project_body_follows_the_rancher_webhook_rules():
    b = pj.project_body({"name": "Team A", "description": "web", "quota": {"limitsCpu": "4", "limitsMemory": "8Gi"},
                         "ns_default": {"limitsCpu": "2", "limitsMemory": "4Gi"},
                         "container": {"limitsCpu": "1", "requestsCpu": "250m"}}, "c-sg2q6")
    assert b == {"type": "project", "clusterId": "c-sg2q6", "name": "Team A", "description": "web",
                 "resourceQuota": {"limit": {"limitsCpu": "4000m", "limitsMemory": "8192Mi"}},
                 "namespaceDefaultResourceQuota": {"limit": {"limitsCpu": "2000m", "limitsMemory": "4096Mi"}},
                 "containerDefaultResourceLimit": {"limitsCpu": "1000m", "requestsCpu": "250m"}}
    plain = pj.project_body({"name": "Team B"}, "c-sg2q6")
    assert plain["resourceQuota"] is None and plain["namespaceDefaultResourceQuota"] is None


@pytest.mark.parametrize("spec, match", [
    ({"name": ""}, "project name"),
    ({"name": "a", "quota": {"limitsCpu": "4"}}, "go together"),
    ({"name": "a", "quota": {"limitsCpu": "4"}, "ns_default": {"limitsMemory": "1Gi"}}, "same resources"),
    ({"name": "a", "quota": {"limitsCpu": "2"}, "ns_default": {"limitsCpu": "3"}}, "exceeds the project limit"),
    ({"name": "a", "container": {"requestsMemory": "2Gi", "limitsMemory": "1Gi"}}, "requestsMemory exceeds limitsMemory"),
    ({"name": "a", "container": {"gpu": "1"}}, "VM default limit"),
])
def test_what_the_rancher_webhook_would_refuse_is_refused_first(spec, match):
    with pytest.raises(ValueError, match=match):
        pj.project_body(spec, "c-sg2q6")


PROJECTS = pj.project_rows([
    {"id": "c-sg2q6:p-nc8d2", "clusterId": "c-sg2q6", "name": "Default", "labels": {"authz.management.cattle.io/default-project": "true"}},
    {"id": "c-sg2q6:p-z22pm", "clusterId": "c-sg2q6", "name": "System", "labels": {"authz.management.cattle.io/system-project": "true"}},
    {"id": "c-sg2q6:p-abcde", "clusterId": "c-sg2q6", "name": "Team A",
     "resourceQuota": {"limit": {"limitsCpu": "4000m"}, "usedLimit": {"limitsCpu": "2000m"}},
     "namespaceDefaultResourceQuota": {"limit": {"limitsCpu": "2000m"}}},
    {"id": "c-other:p-zzzzz", "clusterId": "c-other", "name": "Elsewhere"}], "c-sg2q6")


def ns(name, ann=None, labels=None):
    return {"metadata": {"name": name, "annotations": ann or {}, "labels": labels or {}}}


def test_a_namespace_s_project_is_read_from_the_annotation_and_checked_against_the_cluster():
    assert [p["name"] for p in PROJECTS] == ["Default", "Team A", "System"]       # autre cluster écarté
    member = pj.ns_project(ns("apps", {pj.ANN_PROJECT: "c-sg2q6:p-abcde"}), "c-sg2q6", PROJECTS)
    assert member["state"] == "member" and member["name"] == "Team A"
    # vu sur harv1 : 19 namespaces annotés avec l'id d'un premier import
    stale = pj.ns_project(ns("default", {pj.ANN_PROJECT: "c-qt5jz:p-kcgzz"}), "c-sg2q6", PROJECTS)
    assert stale["state"] == "foreign" and stale["cluster"] == "c-qt5jz"
    assert pj.ns_project(ns("x", {pj.ANN_PROJECT: "c-sg2q6:p-gone1"}), "c-sg2q6", PROJECTS)["state"] == "unknown"
    assert pj.ns_project(ns("y"), "c-sg2q6", PROJECTS)["state"] == "none"
    assert pj.ns_project(ns("z", labels={pj.ANN_PROJECT: "p-abcde"}), "c-sg2q6", PROJECTS)["state"] == "member"
    status = json.dumps({"Conditions": [{"Type": "ResourceQuotaValidated", "Status": "False",
                                         "Message": "Resource quota [limitsCpu=3000m] exceeds project limit"}]})
    q = pj.ns_project(ns("q", {pj.ANN_PROJECT: "c-sg2q6:p-abcde", pj.ANN_QUOTA: '{"limit":{"limitsCpu":"3000m"}}',
                               pj.ANN_STATUS: status}), "c-sg2q6", PROJECTS)
    assert q["quota"] == {"limitsCpu": "3000m"} and q["quota_ok"] is False and "exceeds project limit" in q["quota_message"]


def test_a_namespace_quota_stays_within_its_project():
    team = next(p for p in PROJECTS if p["name"] == "Team A")
    assert pj.check_ns_quota({"limitsCpu": "3"}, team) == {"limitsCpu": "3000m"}
    with pytest.raises(ValueError, match="exceeds the project limit"):
        pj.check_ns_quota({"limitsCpu": "5"}, team)
    with pytest.raises(ValueError, match="not limited by the project"):
        pj.check_ns_quota({"limitsMemory": "1Gi"}, team)
    with pytest.raises(ValueError, match="has no quota"):
        pj.check_ns_quota({"limitsCpu": "1"}, PROJECTS[0])
    assert pj.ns_quota({"limitsCpu": "3"}) == '{"limit":{"limitsCpu":"3000m"}}' and pj.ns_quota({}) is None


def test_rancher_is_found_in_the_session_kubeconfig_only():
    kc = json.dumps({"clusters": [{"name": "rancher", "cluster": {"server": "https://rancher.lan/k8s/clusters/c-sg2q6",
                                                                   "certificate-authority": "/ca.pem"}}],
                     "users": [{"name": "u", "user": {"tokenFile": "/run/tok"}}]})
    assert pj.rancher_of(kc) == {"url": "https://rancher.lan", "cid": "c-sg2q6", "token_file": "/run/tok", "token": None,
                                 "ca_file": "/ca.pem", "insecure": False}
    direct = "clusters:\n- cluster: {server: 'https://172.16.3.100:6443'}\nusers:\n- user: {client-certificate-data: x}\n"
    assert pj.rancher_of(direct) is None


def test_rancher_s_schema_type_is_not_taken_for_a_resource():
    """Vu en réel : Rancher rend {limitsCpu: ..., type: /v3/schemas/resourceQuotaLimit} ;
    la console montrait « type » comme une ressource du quota."""
    [p] = pj.project_rows([{"id": "c-sg2q6:p-5cvwf", "clusterId": "c-sg2q6", "name": "hops-quota",
                            "resourceQuota": {"limit": {"limitsCpu": "4000m", "type": "/v3/schemas/resourceQuotaLimit"},
                                              "usedLimit": {"limitsCpu": "2000m", "type": "/v3/schemas/resourceQuotaLimit"}},
                            "namespaceDefaultResourceQuota": {"limit": {"limitsCpu": "2000m", "type": "/v3/schemas/resourceQuotaLimit"}},
                            "containerDefaultResourceLimit": {"limitsCpu": "1000m", "type": "/v3/schemas/containerResourceLimit"}}], "c-sg2q6")
    assert p["quota"] == {"limitsCpu": "4000m"} and p["used"] == {"limitsCpu": "2000m"}
    assert p["ns_default"] == {"limitsCpu": "2000m"} and p["container"] == {"limitsCpu": "1000m"}
