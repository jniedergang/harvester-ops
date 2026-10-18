"""v1.91.0 : le modèle des charges de conteneurs (bin/lib/hv_containers.py).

Fonctions pures, sans cluster : namespaces système masqués, pods des VM et de
CDI écartés, pods rangés sous leur charge (pod -> ReplicaSet -> Deployment,
pod -> Job -> CronJob), état dérivé, GPU demandés, objets incomplets."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import hv_containers as hc  # noqa: E402


def meta(ns, name, owner=None, labels=None, **extra):
    m = {"namespace": ns, "name": name, "creationTimestamp": "2026-10-10T10:00:00Z"}
    if owner:
        kind, oname = owner
        api = {"ReplicaSet": "apps/v1", "Deployment": "apps/v1", "Job": "batch/v1",
               "CronJob": "batch/v1", "StatefulSet": "apps/v1", "DaemonSet": "apps/v1"}.get(kind, "v1")
        m["ownerReferences"] = [{"apiVersion": api, "kind": kind, "name": oname, "controller": True}]
    if labels:
        m["labels"] = labels
    m.update(extra)
    return m


def containers(*specs):
    out = []
    for i, (image, res) in enumerate(specs):
        c = {"name": f"c{i}", "image": image}
        if res is not None:
            c["resources"] = res
        out.append(c)
    return out


def deploy(ns, name, replicas=1, ready=1, updated=None, gen=1, observed=1, conds=None, images=(("nginx:1", None),),
           paused=False):
    st = {"readyReplicas": ready, "updatedReplicas": ready if updated is None else updated,
          "observedGeneration": observed}
    if conds:
        st["conditions"] = conds
    spec = {"replicas": replicas, "template": {"spec": {"containers": containers(*images)}}}
    if paused:
        spec["paused"] = True
    return {"kind": "Deployment", "metadata": meta(ns, name, generation=gen), "spec": spec, "status": st}


def rs(ns, name, owner, replicas=1):
    return {"kind": "ReplicaSet", "metadata": meta(ns, name, owner=("Deployment", owner)),
            "spec": {"replicas": replicas}, "status": {"readyReplicas": replicas}}


def pod(ns, name, owner=None, phase="Running", ready=True, labels=None, node="n1", restarts=0,
        waiting=None, images=(("nginx:1", None),), owner_api=None):
    m = meta(ns, name, owner=owner, labels=labels)
    if owner_api:
        m["ownerReferences"][0]["apiVersion"] = owner_api
    cs = {"name": "c0", "restartCount": restarts, "state": {"running": {}}}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting}}
    return {"kind": "Pod", "metadata": m,
            "spec": {"nodeName": node, "containers": containers(*images)},
            "status": {"phase": phase, "containerStatuses": [cs],
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def job(ns, name, owner=None, conds=None, succeeded=0, active=0, completions=None, suspend=False):
    spec = {"template": {"spec": {"containers": containers(("busybox", None))}}}
    if completions is not None:
        spec["completions"] = completions
    if suspend:
        spec["suspend"] = True
    st = {"succeeded": succeeded, "active": active}
    if conds:
        st["conditions"] = [{"type": t, "status": s} for t, s in conds]
    return {"kind": "Job", "metadata": meta(ns, name, owner=("CronJob", owner) if owner else None),
            "spec": spec, "status": st}


def cron(ns, name, suspend=False, active=0):
    return {"kind": "CronJob", "metadata": meta(ns, name),
            "spec": {"suspend": suspend, "jobTemplate": {"spec": {"template": {"spec": {
                "containers": containers(("busybox:1.36", None))}}}}},
            "status": {"active": [{"name": f"j{i}"} for i in range(active)]}}


def lists(*objs):
    return hc.split_list({"items": list(objs)})


# -- namespaces système --------------------------------------------------------
@pytest.mark.parametrize("ns, system", [
    ("kube-system", True), ("kube-public", True), ("cattle-system", True), ("cattle-fleet-system", True),
    ("harvester-system", True), ("harvester-public", True), ("longhorn-system", True),
    ("fleet-local", True), ("fleet-default", True), ("cdi", True), ("local", True),
    ("default", False), ("apps", False), ("longhorn", False), ("my-kube-app", False),
    ("cdi-imports", False), ("", False), (None, False),
    # vus sur harv1 (Harvester 1.9.0, Turtles, fournisseurs Cluster API)
    ("caphv-system", True), ("caaph-system", True), ("rke2-bootstrap-system", True),
    ("rke2-control-plane-system", True), ("cluster-fleet-local-local-1a3d67d0a899", True),
    ("kubeovn-owner-namespace", True), ("user-2fhwx", True),
    ("user-team", False), ("system", False), ("my-system-app", False),
])
def test_system_namespaces(ns, system):
    assert hc.is_system_namespace(ns) is system


def test_the_shared_list_is_the_one_of_the_spec():
    assert hc.SYSTEM_NAMESPACES == ("kube-*", "cattle-*", "harvester-*", "longhorn-system",
                                    "fleet-*", "cluster-fleet-*", "*-system", "kubeovn-*",
                                    "user-?????", "cdi", "local")


# -- pods écartés --------------------------------------------------------------
@pytest.mark.parametrize("p, excluded", [
    (pod("default", "virt-launcher-vm1-abcde", labels={"kubevirt.io": "virt-launcher"}), True),
    (pod("default", "hp-volume-x", labels={"kubevirt.io": "hotplug-disk"}), True),
    (pod("default", "virt-x", owner=("VirtualMachineInstance", "vm1"), owner_api="kubevirt.io/v1"), True),
    (pod("default", "importer-prime-x", labels={"app": "containerized-data-importer",
                                                "cdi.kubevirt.io": "importer"}), True),
    (pod("default", "cdi-upload-x", owner=("DataVolume", "dv1"), owner_api="cdi.kubevirt.io/v1beta1"), True),
    (pod("default", "web-1", labels={"app": "web"}), False),
    (pod("default", "web-2", labels={"kubevirt.io": "something-else"}), False),
])
def test_vm_and_cdi_pods(p, excluded):
    assert hc.is_vm_or_cdi_pod(p) is excluded


def test_vm_pods_and_system_namespaces_never_become_workloads():
    got = hc.build(lists(
        pod("default", "virt-launcher-vm1-abcde", labels={"kubevirt.io": "virt-launcher"}),
        pod("default", "importer-prime-1", labels={"app": "containerized-data-importer"}),
        deploy("cattle-system", "rancher"), pod("kube-system", "etcd-n1"),
        deploy("longhorn-system", "longhorn-ui"), pod("default", "lonely")))
    assert [(w["kind"], w["namespace"], w["name"]) for w in got] == [("Pod", "default", "lonely")]


# -- rangement sous la charge --------------------------------------------------
def test_pods_go_under_their_deployment_through_the_replicaset():
    got = hc.build(lists(
        deploy("apps", "web", replicas=2, ready=2),
        rs("apps", "web-5d4f", "web", replicas=2), rs("apps", "web-old", "web", replicas=0),
        pod("apps", "web-5d4f-aaaaa", owner=("ReplicaSet", "web-5d4f")),
        pod("apps", "web-5d4f-bbbbb", owner=("ReplicaSet", "web-5d4f"), node="n2", restarts=3)))
    assert len(got) == 1
    w = got[0]
    assert (w["kind"], w["name"], w["ready"], w["desired"], w["state"]) == ("Deployment", "web", 2, 2, "Running")
    assert [(p["name"], p["node"], p["restarts"]) for p in w["pods"]] == [
        ("web-5d4f-aaaaa", "n1", 0), ("web-5d4f-bbbbb", "n2", 3)]


def test_without_replicasets_the_deployment_is_found_by_the_pod_template_hash():
    got = hc.build(lists(deploy("apps", "api"),
                         pod("apps", "api-7c9-xyz12", owner=("ReplicaSet", "api-7c9"),
                             labels={"pod-template-hash": "7c9"})))
    assert [(w["kind"], w["name"], len(w["pods"])) for w in got] == [("Deployment", "api", 1)]


def test_cronjob_gathers_its_jobs_and_their_pods():
    got = hc.build(lists(
        cron("ops", "backup", active=1),
        job("ops", "backup-1", owner="backup", conds=[("Complete", "True")], succeeded=1),
        job("ops", "backup-2", owner="backup", active=1),
        pod("ops", "backup-1-abc", owner=("Job", "backup-1"), phase="Succeeded", ready=False),
        pod("ops", "backup-2-def", owner=("Job", "backup-2")),
        job("ops", "migrate", conds=[("Complete", "True")], succeeded=1),
        pod("ops", "migrate-xyz", owner=("Job", "migrate"), phase="Succeeded", ready=False)))
    rows = {(w["kind"], w["name"]): w for w in got}
    assert set(rows) == {("CronJob", "backup"), ("Job", "migrate")}
    assert [p["name"] for p in rows[("CronJob", "backup")]["pods"]] == ["backup-1-abc", "backup-2-def"]
    assert rows[("CronJob", "backup")]["desired"] is None and rows[("CronJob", "backup")]["ready"] == 1
    assert rows[("Job", "migrate")]["state"] == "Completed"


def test_statefulset_and_daemonset_pods():
    sts = {"kind": "StatefulSet", "metadata": meta("db", "pg"), "spec": {"replicas": 3, "template": {"spec": {
        "containers": containers(("postgres:16", None))}}},
        "status": {"readyReplicas": 2, "updatedReplicas": 3, "observedGeneration": 1}}
    ds = {"kind": "DaemonSet", "metadata": meta("mon", "agent"), "spec": {"template": {"spec": {
        "containers": containers(("agent:2", None))}}},
        "status": {"desiredNumberScheduled": 2, "numberReady": 2, "updatedNumberScheduled": 2}}
    got = hc.build(lists(sts, ds, pod("db", "pg-0", owner=("StatefulSet", "pg")),
                         pod("db", "pg-1", owner=("StatefulSet", "pg"), ready=False),
                         pod("mon", "agent-x", owner=("DaemonSet", "agent"))))
    rows = {w["name"]: w for w in got}
    assert (rows["pg"]["state"], rows["pg"]["ready"], rows["pg"]["desired"], len(rows["pg"]["pods"])) == \
        ("Degraded", 2, 3, 2)
    assert (rows["agent"]["state"], len(rows["agent"]["pods"])) == ("Running", 1)


def test_a_pod_whose_owner_is_unknown_stays_alone():
    # un opérateur (CRD) possède le pod : on montre le pod, pas un fantôme
    p = pod("apps", "worker-0", owner=("Workflow", "wf1"), owner_api="argoproj.io/v1alpha1")
    got = hc.build(lists(p))
    assert [(w["kind"], w["name"]) for w in got] == [("Pod", "worker-0")]


def test_same_name_in_two_namespaces_are_two_workloads():
    got = hc.build(lists(deploy("a", "web"), deploy("b", "web")))
    assert [(w["namespace"], w["name"]) for w in got] == [("a", "web"), ("b", "web")]


# -- états -----------------------------------------------------------------------
@pytest.mark.parametrize("obj, state, ready, desired", [
    (deploy("x", "d", replicas=2, ready=2), "Running", 2, 2),
    (deploy("x", "d", replicas=0, ready=0), "Suspended", 0, 0),
    (deploy("x", "d", replicas=2, ready=1, updated=2), "Degraded", 1, 2),
    (deploy("x", "d", replicas=2, ready=0, updated=2), "Failed", 0, 2),
    (deploy("x", "d", replicas=2, ready=2, updated=1), "Progressing", 2, 2),
    (deploy("x", "d", replicas=1, ready=1, gen=3, observed=2), "Progressing", 1, 1),
    (deploy("x", "d", replicas=1, ready=0, conds=[{"type": "Progressing", "status": "False",
                                                   "reason": "ProgressDeadlineExceeded"}]), "Failed", 0, 1),
    (deploy("x", "d", replicas=1, ready=1, paused=True), "Suspended", 1, 1),
    (job("x", "j", conds=[("Complete", "True")], succeeded=1), "Completed", 1, 1),
    (job("x", "j", conds=[("Failed", "True")]), "Failed", 0, 1),
    (job("x", "j", conds=[("Suspended", "False")], active=1, completions=3), "Running", 0, 3),
    (job("x", "j", suspend=True), "Suspended", 0, 1),
    (job("x", "j"), "Progressing", 0, 1),
    (cron("x", "c", suspend=True), "Suspended", 0, None),
    (cron("x", "c", active=2), "Running", 2, None),
    (pod("x", "p"), "Running", 1, 1),
    (pod("x", "p", ready=False, waiting="CrashLoopBackOff"), "Degraded", 0, 1),
    (pod("x", "p", phase="Pending", ready=False), "Progressing", 0, 1),
    (pod("x", "p", phase="Pending", ready=False, waiting="ImagePullBackOff"), "Failed", 0, 1),
    (pod("x", "p", phase="Succeeded", ready=False), "Completed", 1, 1),
    (pod("x", "p", phase="Failed", ready=False), "Failed", 0, 1),
])
def test_workload_state(obj, state, ready, desired):
    assert hc.workload_state(obj["kind"], obj) == (state, ready, desired)


def test_daemonset_states():
    def ds(desired, ready, updated):
        return {"kind": "DaemonSet", "metadata": meta("x", "d"), "spec": {},
                "status": {"desiredNumberScheduled": desired, "numberReady": ready,
                           "updatedNumberScheduled": updated}}
    assert hc.workload_state("DaemonSet", ds(3, 3, 3)) == ("Running", 3, 3)
    assert hc.workload_state("DaemonSet", ds(3, 3, 1)) == ("Progressing", 3, 3)
    assert hc.workload_state("DaemonSet", ds(3, 1, 3)) == ("Degraded", 1, 3)
    assert hc.workload_state("DaemonSet", ds(0, 0, 0)) == ("Suspended", 0, 0)


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        hc.workload_state("Widget", {})


def test_pod_state_reads_the_waiting_reason_first():
    p = pod("x", "p", ready=False, waiting="ErrImagePull")
    assert hc.pod_state(p) == ("ErrImagePull", False)
    p = pod("x", "p", ready=False, waiting="ContainerCreating", phase="Pending")
    assert hc.pod_state(p) == ("Pending", False)
    p = pod("x", "p")
    p["metadata"]["deletionTimestamp"] = "2026-10-10T10:00:00Z"
    assert hc.pod_state(p) == ("Terminating", False)


# -- GPU et images -----------------------------------------------------------------
@pytest.mark.parametrize("res, gpus", [
    (None, 0),
    ({"requests": {"cpu": "1"}}, 0),
    ({"limits": {"nvidia.com/gpu": "1"}}, 1),
    ({"requests": {"nvidia.com/gpu": 2}, "limits": {"nvidia.com/gpu": 2}}, 2),
    ({"limits": {"nvidia.com/mig-3g.40gb": "1", "nvidia.com/gpu": "1"}}, 2),
    ({"limits": {"amd.com/gpu": "4"}}, 4),
    ({"limits": {"amd.com/cpu": "4", "intel.com/gpu": "1"}}, 0),
    ({"limits": {"nvidia.com/gpu": "lots"}}, 0),
])
def test_gpu_count_one_container(res, gpus):
    assert hc.gpu_count({"containers": containers(("img", res))}) == gpus


def test_gpu_count_sums_every_container_and_images_keep_their_order():
    spec = {"containers": containers(("a:1", {"limits": {"nvidia.com/gpu": "1"}}),
                                     ("b:1", {"limits": {"amd.com/gpu": "2"}}), ("a:1", None))}
    assert hc.gpu_count(spec) == 3
    assert hc.images(spec) == ["a:1", "b:1"]
    w = hc.build(lists(deploy("ai", "train", images=(("cuda:12", {"limits": {"nvidia.com/gpu": "2"}}),
                                                       ("sidecar:1", None)))))[0]
    assert (w["gpus"], w["images"]) == (2, ["cuda:12", "sidecar:1"])


# -- objets incomplets -------------------------------------------------------------
def test_incomplete_objects_are_skipped_never_fatal():
    got = hc.build({
        "Deployment": [{"metadata": {"name": "noname-ns"}}, {"kind": "Deployment"}, None,
                       {"metadata": {"namespace": "a", "name": "bare"}}],
        "Pod": [{"metadata": {"namespace": "a", "name": "p1"}}],
        "CronJob": [{"metadata": {"namespace": "a", "name": "c1"}}],
    })
    rows = {(w["kind"], w["name"]): w for w in got}
    assert set(rows) == {("Deployment", "bare"), ("Pod", "p1"), ("CronJob", "c1")}
    assert rows[("Deployment", "bare")]["state"] == "Progressing"    # sans status : pas encore vue par le contrôleur
    assert rows[("Pod", "p1")]["state"] == "Degraded"                # phase inconnue
    assert rows[("Pod", "p1")]["images"] == [] and rows[("Pod", "p1")]["gpus"] == 0
    assert hc.build({}) == [] and hc.build(None) == []


def test_split_list_ignores_foreign_kinds():
    got = hc.split_list({"items": [{"kind": "Service"}, {"kind": "Pod"}, None]})
    assert len(got["Pod"]) == 1 and all(not got[k] for k in got if k != "Pod")
    assert hc.split_list(None)["Deployment"] == []


@pytest.mark.parametrize("ts, out", [
    ("2026-10-18T12:00:00Z", "0s"), ("2026-10-18T11:59:20Z", "40s"), ("2026-10-18T11:48:00Z", "12m"),
    ("2026-10-18T09:00:00Z", "3h"), ("2026-10-13T12:00:00Z", "5d"), ("bad", "?"), (None, "?"),
])
def test_age(ts, out):
    assert hc.age(ts, datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)) == out
