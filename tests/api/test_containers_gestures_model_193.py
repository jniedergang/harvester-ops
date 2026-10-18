"""v1.93.0 : la demande d'une charge devient un manifeste (fonctions pures).

Deployment ou Job du formulaire, manifeste collé, cibles des gestes. Règles :
limite mémoire toujours posée (256Mi par défaut), demandes de mémoire et de
CPU aussi, GPU en limite de la ressource étendue, namespaces système refusés,
noms RFC 1123."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import hv_containers as hc  # noqa: E402

BASE = {"name": "web", "namespace": "apps", "image": "nginx:1.27"}


def container(m):
    return m["spec"]["template"]["spec"]["containers"][0]


@pytest.mark.parametrize("extra,kind,api,body", [
    ({}, "Deployment", "apps/v1", {"replicas": 1}),
    ({"replicas": 3}, "Deployment", "apps/v1", {"replicas": 3}),
    ({"replicas": 0}, "Deployment", "apps/v1", {"replicas": 0}),
    ({"kind": "Job", "replicas": 2}, "Job", "batch/v1", {"completions": 2, "parallelism": 2}),
    ({"kind": "Job"}, "Job", "batch/v1", {"completions": 1, "parallelism": 1}),
])
def test_deployment_and_job(extra, kind, api, body):
    m = hc.build_manifest(dict(BASE, **extra))
    assert (m["kind"], m["apiVersion"]) == (kind, api)
    assert m["metadata"] == {"name": "web", "namespace": "apps",
                             "labels": {"app": "web", "app.kubernetes.io/managed-by": "harvester-ops"}}
    for k, v in body.items():
        assert m["spec"][k] == v
    assert m["spec"]["template"]["metadata"]["labels"] == {"app": "web"}
    if kind == "Deployment":
        assert m["spec"]["selector"] == {"matchLabels": {"app": "web"}}
        assert "restartPolicy" not in m["spec"]["template"]["spec"]
    else:
        assert m["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert container(m)["image"] == "nginx:1.27" and container(m)["name"] == "web"


@pytest.mark.parametrize("extra,limits,requests", [
    ({}, {"memory": "256Mi"}, {"memory": "256Mi", "cpu": "100m"}),
    ({"memory": "2Gi"}, {"memory": "2Gi"}, {"memory": "2Gi", "cpu": "100m"}),
    ({"cpu": "500m"}, {"memory": "256Mi", "cpu": "500m"}, {"memory": "256Mi", "cpu": "500m"}),
    ({"gpus": 2}, {"memory": "256Mi", "nvidia.com/gpu": "2"}, {"memory": "256Mi", "cpu": "100m"}),
    ({"gpus": 1, "gpu_resource": "amd.com/gpu"}, {"memory": "256Mi", "amd.com/gpu": "1"},
     {"memory": "256Mi", "cpu": "100m"}),
    ({"gpus": 1, "gpu_resource": "nvidia.com/mig-1g.10gb"}, {"memory": "256Mi", "nvidia.com/mig-1g.10gb": "1"},
     {"memory": "256Mi", "cpu": "100m"}),
    ({"gpus": 0}, {"memory": "256Mi"}, {"memory": "256Mi", "cpu": "100m"}),
])
def test_resources_memory_limit_always_set(extra, limits, requests):
    res = container(hc.build_manifest(dict(BASE, **extra)))["resources"]
    assert res == {"limits": limits, "requests": requests}


def test_the_gpus_are_counted_back_by_the_reading_model():
    m = hc.build_manifest(dict(BASE, gpus=3))
    assert hc.gpu_count(m["spec"]["template"]["spec"]) == 3


@pytest.mark.parametrize("command,env,want_cmd,want_env", [
    ("sleep infinity", None, ["sleep", "infinity"], None),
    ("sh -c 'echo a b'", ["A=1", "B=x=y"], ["sh", "-c", "echo a b"],
     [{"name": "A", "value": "1"}, {"name": "B", "value": "x=y"}]),
    (["python", "-m", "http.server"], {"PORT": 8000}, ["python", "-m", "http.server"],
     [{"name": "PORT", "value": "8000"}]),
    ("", [{"name": "E", "value": ""}], None, [{"name": "E", "value": ""}]),
])
def test_command_and_env(command, env, want_cmd, want_env):
    c = container(hc.build_manifest(dict(BASE, command=command, env=env)))
    assert c.get("command") == want_cmd
    assert c.get("env") == want_env


@pytest.mark.parametrize("extra,msg", [
    ({"name": "Web"}, "invalid name"),
    ({"name": "web_1"}, "invalid name"),
    ({"name": "a" * 64}, "invalid name"),
    ({"name": ""}, "invalid name"),
    ({"namespace": ""}, "invalid namespace"),
    ({"namespace": "Apps"}, "invalid namespace"),
    ({"namespace": "kube-system"}, "system namespace"),
    ({"namespace": "cattle-system"}, "system namespace"),
    ({"namespace": "harvester-system"}, "system namespace"),
    ({"namespace": "longhorn-system"}, "system namespace"),
    ({"namespace": "caphv-system"}, "system namespace"),
    ({"kind": "StatefulSet"}, "kind must be"),
    ({"image": ""}, "image is required"),
    ({"image": "nginx 1"}, "image is required"),
    ({"replicas": -1}, "between 0 and 1000"),
    ({"replicas": 1001}, "between 0 and 1000"),
    ({"replicas": "two"}, "whole number"),
    ({"replicas": 1.5}, "whole number"),
    ({"replicas": True}, "whole number"),
    ({"kind": "Job", "replicas": 0}, "between 1 and 1000"),
    ({"gpus": -1}, "between 0 and 64"),
    ({"gpus": 65}, "between 0 and 64"),
    ({"gpu_resource": "intel.com/gpu"}, "invalid GPU resource"),
    ({"gpu_resource": "nvidia.com/"}, "invalid GPU resource"),
    ({"cpu": "lots"}, "invalid CPU limit"),
    ({"memory": "256MB"}, "invalid memory limit"),
    ({"memory": "-1Gi"}, "invalid memory limit"),
    ({"env": ["NOEQUAL"]}, "without '='"),
    ({"env": ["1BAD=x"]}, "invalid environment variable name"),
    ({"env": ["A=1", "A=2"]}, "given twice"),
    ({"env": 3}, "NAME=VALUE"),
    ({"command": "sh -c 'open"}, "command"),
    ({"command": 3}, "command must be"),
])
def test_invalid_requests_are_refused(extra, msg):
    with pytest.raises(ValueError, match=msg):
        hc.build_manifest(dict(BASE, **extra))


def test_a_normalized_spec_builds_the_same_manifest():
    spec = dict(BASE, env=["A=1"], command="sleep 5", cpu="1", gpus=1)
    assert hc.build_manifest(hc.normalize_spec(spec)) == hc.build_manifest(spec)


def test_the_spec_must_be_an_object():
    with pytest.raises(ValueError, match="object"):
        hc.build_manifest(["web"])


MANIFEST = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "api"},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "api"}}, "template": {
                "metadata": {"labels": {"app": "api"}},
                "spec": {"initContainers": [{"name": "init", "image": "busybox"}],
                         "containers": [{"name": "a", "image": "nginx"},
                                        {"name": "b", "image": "redis", "resources": {"limits": {"memory": "1Gi", "cpu": "2"}}}]}}}}


def test_a_pasted_manifest_gets_its_namespace_and_memory_limits():
    out = hc.check_manifest(MANIFEST, "apps")
    assert out["metadata"]["namespace"] == "apps"
    pod = out["spec"]["template"]["spec"]
    assert pod["initContainers"][0]["resources"] == {"limits": {"memory": "256Mi"},
                                                     "requests": {"memory": "256Mi", "cpu": "100m"}}
    assert pod["containers"][0]["resources"]["limits"] == {"memory": "256Mi"}
    # ce qui est écrit n'est jamais remplacé
    assert pod["containers"][1]["resources"] == {"limits": {"memory": "1Gi", "cpu": "2"},
                                                 "requests": {"memory": "1Gi", "cpu": "2"}}
    assert "namespace" not in MANIFEST["metadata"]                       # copie, pas l'original
    assert "resources" not in MANIFEST["spec"]["template"]["spec"]["containers"][0]


@pytest.mark.parametrize("obj,ns,want", [
    ({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p"},
      "spec": {"containers": [{"name": "c", "image": "i"}]}}, None, "default"),
    ({"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "c", "namespace": "ml"},
      "spec": {"schedule": "* * * * *", "jobTemplate": {"spec": {"template": {"spec": {
          "containers": [{"name": "c", "image": "i"}]}}}}}}, None, "ml"),
])
def test_manifest_kinds_and_default_namespace(obj, ns, want):
    out = hc.check_manifest(obj, ns)
    assert out["metadata"]["namespace"] == want


@pytest.mark.parametrize("obj,ns,msg", [
    ("text", None, "one Kubernetes object"),
    ({"kind": "List", "items": []}, None, "one workload at a time"),
    ({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "x"}}, None, "kind must be"),
    ({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "x"}}, None, "kind must be"),
    ({"kind": "Pod", "metadata": {"name": "p"}, "spec": {"containers": [{}]}}, None, "apiVersion"),
    ({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "P"}, "spec": {"containers": [{}]}}, None, "invalid name"),
    ({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p", "namespace": "kube-system"},
      "spec": {"containers": [{}]}}, None, "system namespace"),
    ({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p"}, "spec": {"containers": [{}]}},
     "cattle-system", "system namespace"),
    ({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p", "namespace": "a"},
      "spec": {"containers": [{}]}}, "b", "the request b"),
    ({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p"}, "spec": {}}, None, "no containers"),
    ({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "d"}}, None, "no spec"),
    ({"apiVersion": "v1", "kind": "Pod", "metadata": "x", "spec": {"containers": [{}]}}, None, "metadata"),
])
def test_invalid_manifests_are_refused(obj, ns, msg):
    with pytest.raises(ValueError, match=msg):
        hc.check_manifest(obj, ns)


@pytest.mark.parametrize("ns,kind,name,allowed,want", [
    ("apps", "deployment", "web", hc.SCALABLE_KINDS, ("apps", "Deployment", "web")),
    ("apps", "StatefulSet", "db", hc.RESTARTABLE_KINDS, ("apps", "StatefulSet", "db")),
    ("apps", "pod", "web-7d9f-abcde", hc.KINDS, ("apps", "Pod", "web-7d9f-abcde")),
    ("apps", "cronjob", "nightly", hc.KINDS, ("apps", "CronJob", "nightly")),
])
def test_targets(ns, kind, name, allowed, want):
    assert hc.check_target(ns, kind, name, allowed) == want


@pytest.mark.parametrize("ns,kind,name,allowed,msg", [
    ("kube-system", "deployment", "coredns", hc.KINDS, "system namespace"),
    ("harvester-system", "deployment", "x", hc.KINDS, "system namespace"),
    ("apps", "virtualmachine", "vm1", hc.KINDS, "unknown workload kind"),
    ("apps", "job", "j", hc.SCALABLE_KINDS, "cannot take this action"),
    ("apps", "daemonset", "d", hc.SCALABLE_KINDS, "cannot take this action"),
    ("apps", "job", "j", hc.RESTARTABLE_KINDS, "cannot take this action"),
    ("apps", "deployment", "Bad", hc.KINDS, "invalid name"),
    ("apps", "deployment", "", hc.KINDS, "invalid name"),
])
def test_targets_refused(ns, kind, name, allowed, msg):
    with pytest.raises(ValueError, match=msg):
        hc.check_target(ns, kind, name, allowed)


def test_restart_patch_is_the_rollout_restart_annotation():
    now = datetime(2026, 10, 18, 9, 30, 0, tzinfo=timezone.utc)
    assert hc.restart_patch(now) == {"spec": {"template": {"metadata": {"annotations": {
        "kubectl.kubernetes.io/restartedAt": "2026-10-18T09:30:00Z"}}}}}


def test_resources_of_kinds():
    assert hc.resource_of("Deployment") == "deployments.apps"
    assert hc.resource_of("Job") == "jobs.batch"
    assert hc.resource_of("Pod") == "pods"
