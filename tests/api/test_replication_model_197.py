"""v1.97.0 : la logique pure de la réplication (bin/lib/replication.py).

Noms (déterministes, bornés, collision d'empreinte rattrapée), validation
d'un plan, constats d'éligibilité et de contrôle (un cas par code, plan à deux
VMs et trois disques), intervalle et prévision sur les nombres du banc des 22
et 23/10/2026, état d'un plan et de ses ressources, actions par état et par
côté, manifestes comparés aux objets du banc, VM cible en `keep` et `rerun`."""

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import replication as rp  # noqa: E402

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)
PID = "rp-0123456789ab"
GiB, MiB = 1024 ** 3, 1024 ** 2
UID_A = "fc8b3a33-ffa8-42db-b482-02e589a991c8"
UID_B = "3fff1a23-153d-44d0-8627-4e63480e918d"


def T(seconds):
    return rp.iso(NOW + timedelta(seconds=seconds))


# -- validation d'un plan -------------------------------------------------------

BASE = {"name": "web", "source_cluster": "harvlab", "target_cluster": "harvlab2", "rpo_objective_s": "15m",
        "vms": ["default/rep-a"]}


def test_normalize_plan_defaults():
    p = rp.normalize_plan(dict(BASE, interval_s="5m"))
    assert (p["rpo_objective_s"], p["interval_s"], p["cron"], p["retain"]) == (900, 300, "*/5 * * * *", 6)
    assert p["options"] == {"cloud_init": "keep", "mac": "keep", "create_namespace": False, "start": "started",
                            "follow": "console", "paused": False, "timeouts": {}}
    assert set(p["mappings"]) == set(rp.MAPPING_KINDS) and p["selector"] == ""
    s = rp.normalize_plan(dict(BASE, vms=[], selector="app=web", namespace="prod", retain=3,
                               options={"timeouts": {"stop": "20m"}, "cloud_init": "rerun"},
                               mappings={"networks": {"default/a": "default/lab-net"},
                                         "target_names": {"default/rep-a": "rep-a-dr"}}))
    assert (s["selector"], s["selector_namespace"], s["interval_s"], s["cron"]) == ("app=web", "prod", None, "")
    assert s["options"]["timeouts"] == {"stop": 1200} and s["mappings"]["target_names"] == {"default/rep-a": "rep-a-dr"}


@pytest.mark.parametrize("change,code", [
    ({"name": "Web"}, "bad-name"),
    ({"name": "a" * 64}, "bad-name"),
    ({"source_cluster": "bad name"}, "bad-cluster"),
    ({"target_cluster": "harvlab"}, "same-cluster"),
    ({"target_host_kind": "kvm"}, "bad-host-kind"),
    ({"rpo_objective_s": "soon"}, "bad-rpo"),
    ({"rpo_objective_s": "30s"}, "bad-rpo"),
    ({"rpo_objective_s": "8d"}, "bad-rpo"),
    ({"interval_s": "7m"}, "bad-interval"),
    ({"retain": 2}, "bad-retain"),
    ({"retain": 51}, "bad-retain"),
    ({"retain": True}, "bad-retain"),
    ({"vms": []}, "bad-selection"),
    ({"selector": "app=web", "namespace": "prod"}, "bad-selection"),     # les deux
    ({"vms": ["rep-a"]}, "bad-reference"),
    ({"vms": ["default/Rep"]}, "bad-reference"),
    ({"vms": ["default/a", "default/a"]}, "bad-selection"),
    ({"vms": [], "selector": "app", "namespace": "prod"}, "bad-selector"),
    ({"vms": [], "selector": "app=web"}, "bad-selection"),
    ({"mappings": {"networks": {"default/a": "lab-net"}}}, "bad-reference"),
    ({"mappings": {"storage_classes": {"a": "B"}}}, "bad-mapping"),
    ({"mappings": {"volumes": {}}}, "bad-mapping"),
    ({"mappings": {"target_names": {"default/a": "x/y"}}}, "bad-mapping"),
    ({"options": {"mac": "random"}}, "bad-option"),
    ({"options": {"paused": "yes"}}, "bad-option"),
    ({"options": {"consistency": "freeze"}}, "bad-option"),
    ({"options": {"timeouts": {"forever": 10}}}, "bad-timeout"),
    ({"options": {"timeouts": {"stop": 0}}}, "bad-timeout"),
    ({"options": {"timeouts": {"stop": "1y"}}}, "bad-duration"),
])
def test_normalize_plan_refusals(change, code):
    with pytest.raises(rp.PlanError) as e:
        rp.normalize_plan(dict(BASE, **change))
    assert e.value.code == code


@pytest.mark.parametrize("text,seconds", [("90", 90), ("90s", 90), ("15m", 900), ("1h", 3600), ("1d", 86400),
                                          (300, 300)])
def test_parse_duration(text, seconds):
    assert rp.parse_duration(text) == seconds


# -- noms -----------------------------------------------------------------------

def test_target_volume_name_is_deterministic_and_generation_bound():
    a = rp.target_volume_name(PID, UID_A, "disk-0", 1, "dr2-amp")
    assert a == rp.target_volume_name(PID, UID_A, "disk-0", 1, "dr2-amp")
    assert a.startswith("hr-dr2-amp-disk-0-") and len(a) == len("hr-dr2-amp-disk-0-") + 6
    assert rp.target_volume_name(PID, UID_A, "disk-0", 2, "dr2-amp") != a          # retour : autre génération
    assert rp.target_volume_name(PID, UID_A, "data", 1, "dr2-amp") != a
    assert rp.target_volume_name("rp-0000000000ff", UID_A, "disk-0", 1, "dr2-amp") != a


@pytest.mark.parametrize("vm,disk", [
    ("a" * 63, "b" * 63),
    ("vm-with-a-long-name-ending-in-dash-", "disk"),
    ("x", "a-disk-whose-twelfth-char-is-a-dash"),
    ("VM_Upper.Case", "Disk_0"),
    ("---", "..."),
])
def test_target_volume_name_limits_and_charset(vm, disk):
    n = rp.target_volume_name(PID, UID_A, disk, 1, vm)
    assert len(n) <= rp.VOLUME_NAME_MAX == 40
    assert rp.NAME_RE.match(n) and "--" not in n
    # le webhook de Longhorn : util.ValidateName
    import re
    assert re.match(r"^[a-z0-9][a-z0-9_.-]+$", n)


def test_recurring_job_and_carnet_names():
    assert rp.recurring_job_name(PID, UID_A) == "hr-01234567-fc8b3a33"
    assert rp.carnet_name(PID, UID_A) == rp.recurring_job_name(PID, UID_A)
    odd = rp.recurring_job_name(PID, "not-a-uuid")
    assert len(odd) == 20 and rp.NAME_RE.match(odd)


def plan_obj(**kw):
    p = rp.normalize_plan(dict(BASE, vms=["default/rep-a", "default/rep-b"]))
    p.update({"id": PID, "generation": 1, "state": "draft"})
    p.update(kw)
    return p


RESOURCES = [{"resource_uid": UID_A, "namespace": "default", "name": "rep-a"},
             {"resource_uid": UID_B, "namespace": "default", "name": "rep-b"}]
VOLUMES = [{"vm_uid": UID_A, "disk": "disk-0", "claim": "rep-a-root"},
           {"vm_uid": UID_A, "disk": "data", "claim": "rep-a-data"},
           {"vm_uid": UID_B, "disk": "disk-0", "claim": "rep-b-root"}]


def test_planned_objects_follow_the_mappings():
    p = plan_obj(mappings=dict(plan_obj()["mappings"], namespaces={"default": "dr"},
                               target_names={"default/rep-b": "rep-b-dr"},
                               claims={"default/rep-a-data": "rep-a-data-dr"}))
    o = rp.planned_objects(p, RESOURCES, VOLUMES)
    assert o["vms"] == ["dr/rep-a", "dr/rep-b-dr"]
    assert o["pvcs"] == ["dr/rep-a-root", "dr/rep-a-data-dr", "dr/rep-b-root"]
    assert len(set(o["volumes"])) == 3 and o["recurring_jobs"][0] == "hr-01234567-fc8b3a33"
    assert o["carnets"][0] == "harvester-ops-replication/hr-01234567-fc8b3a33"


def test_collisions_by_label_and_by_phase():
    p = plan_obj()
    planned = rp.planned_objects(p, RESOURCES, VOLUMES)
    mine = rp.plan_labels(PID, 1)
    facts = {"volumes": {planned["volumes"][0]: {}, planned["volumes"][1]: mine,
                         planned["volumes"][2]: {rp.LABEL_PLAN: "rp-ffffffffffff"}},
             "recurring_jobs": {planned["recurring_jobs"][0]: {"other": "x"}},
             "carnets": {planned["carnets"][1]: {}},
             "pvcs": {"default/rep-a-root": {}, "default/rep-a-data": mine},
             "vms": {"default/rep-b": {}}}
    got = [(f["code"], f["level"]) for f in rp.collisions(facts, p, RESOURCES, VOLUMES)]
    assert got == [("target-volume-taken", "block"), ("target-volume-taken", "block"),
                   ("recurring-job-taken", "block"), ("carnet-taken", "block"),
                   ("target-pvc-taken", "warn"), ("target-vm-taken", "warn")]
    at_failover = rp.collisions(facts, p, RESOURCES, VOLUMES, phase="failover")
    assert [f["level"] for f in at_failover if f["code"].startswith("target-p") or
            f["code"] == "target-vm-taken"] == ["block", "block"]
    assert rp.collisions({}, p, RESOURCES, VOLUMES) == []


def test_a_simulated_digest_collision_is_caught_by_the_labels(monkeypatch):
    # deux plans dont les noms se confondraient : le second voit le volume du
    # premier, étiqueté pour lui, et s'arrête
    monkeypatch.setattr(rp, "_digest", lambda text: "abcdef" * 11)
    first = rp.target_volume_name("rp-00000000000a", UID_A, "disk-0", 1, "rep-a")
    assert rp.target_volume_name("rp-00000000000b", UID_B, "disk-0", 1, "rep-a") == first
    other = plan_obj(id="rp-00000000000b")
    res = [{"resource_uid": UID_B, "namespace": "default", "name": "rep-a"}]
    vols = [{"vm_uid": UID_B, "disk": "disk-0", "claim": "c"}]
    facts = {"volumes": {first: rp.plan_labels("rp-00000000000a", 1, UID_A)}}
    assert [f["code"] for f in rp.collisions(facts, other, res, vols)] == ["target-volume-taken"]


# -- éligibilité ---------------------------------------------------------------

SC_LH = {"metadata": {"name": "harvester-longhorn"}, "provisioner": "driver.longhorn.io",
         "parameters": {"numberOfReplicas": "3"}}


def lhv(name="pvc-1", labels=None, **spec):
    base = {"dataEngine": "v1", "accessMode": "rwx", "migratable": True}
    base.update(spec)
    return {"metadata": {"name": name, "labels": labels or {}}, "spec": base}


PVC = {"metadata": {"name": "rep-a-data"}}


@pytest.mark.parametrize("vol,sc,jobs,expected", [
    (lhv(), SC_LH, (), []),
    (lhv(accessMode="rwo", migratable=False), SC_LH, (), []),
    (lhv(dataEngine="v2"), SC_LH, (), [("data-engine-v2", "block")]),
    (lhv(), {"provisioner": "lvm.driver.harvesterhci.io"}, (), [("not-longhorn", "block")]),
    (None, SC_LH, (), [("not-longhorn", "block")]),
    (lhv(encrypted=True), SC_LH, (), [("encrypted-volume", "block")]),
    (lhv(migratable=False), SC_LH, (), [("rwx-share-manager", "block")]),
    (lhv(), SC_LH, ("daily",), [("default-backup-job", "warn")]),                 # implicite : sans étiquette
    (lhv(labels={rp.JOB_GROUP_LABEL_PREFIX + "default": "enabled"}), SC_LH, ("daily",),
     [("default-backup-job", "warn")]),
    (lhv(labels={rp.JOB_LABEL_PREFIX + "mine": "enabled"}), SC_LH, ("daily",), []),
    (lhv(dataEngine="v2", encrypted=True, migratable=False), SC_LH, (),
     [("data-engine-v2", "block"), ("encrypted-volume", "block"), ("rwx-share-manager", "block")]),
])
def test_volume_findings(vol, sc, jobs, expected):
    got = rp.volume_findings(vol, PVC, sc, jobs)
    assert [(f["code"], f["level"]) for f in got] == expected


def test_default_backup_job_says_when_membership_is_implicit():
    f = rp.volume_findings(lhv(), PVC, SC_LH, ("daily",))[0]
    assert f["facts"]["implicit"] is True and f["facts"]["jobs"] == ["daily"]


def test_disks_not_replicated_and_block_size():
    vm = {"spec": {"template": {"spec": {"volumes": [
        {"name": "root", "persistentVolumeClaim": {"claimName": "c"}},
        {"name": "iso", "containerDisk": {"image": "x"}}, {"name": "h", "hostDisk": {}},
        {"name": "tmp", "emptyDisk": {}}, {"name": "ci", "cloudInitNoCloud": {}}]}}}}
    assert [(f["facts"]["disk"], f["facts"]["type"]) for f in rp.vm_disk_findings(vm)] == \
        [("iso", "containerDisk"), ("h", "hostDisk"), ("tmp", "emptyDisk")]
    assert rp.volume_block_size(lhv()) == 2 * MiB
    assert rp.volume_block_size(lhv(backupBlockSize="16777216")) == 16 * MiB
    assert rp.volume_block_size(lhv(backupBlockSize="4096")) == 2 * MiB


# -- contrôle d'un plan : deux VMs, trois disques ----------------------------------

def vm_obj(name, uid, claims, nets=("default/lab-net",), mac="52:54:00:aa:00:01", fw="fw-" + "1" * 8,
           devices=False, pinned=False):
    tspec = {"domain": {"devices": {"interfaces": [{"name": "nic-1", "bridge": {}, "macAddress": mac}]},
                        "firmware": {"uuid": fw} if fw else {}},
             "networks": [{"name": "nic-1", "multus": {"networkName": n}} for n in nets],
             "volumes": [{"name": d, "persistentVolumeClaim": {"claimName": c}} for d, c in claims]}
    if devices:
        tspec["domain"]["devices"]["hostDevices"] = [{"name": "g", "deviceName": "nvidia.com/A30"}]
    if pinned:
        tspec["nodeSelector"] = {"kubernetes.io/hostname": "n1"}
    return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
            "metadata": {"name": name, "namespace": "default", "uid": uid, "generation": 4,
                         "resourceVersion": "1000"},
            "spec": {"runStrategy": "RerunOnFailure", "template": {"spec": tspec}}}


def disk(name, claim, size, vol, sc="harvester-longhorn", image=None, backing=None):
    v = lhv(vol, **({"backingImage": backing} if backing else {}))
    return {"disk": name, "claim": claim, "size": size, "storage_class": sc, "image": image,
            "lh_volume": v, "pvc": {"metadata": {"name": claim}}, "sc": SC_LH}


def facts(**kw):
    bt = '{"type":"nfs","endpoint":"nfs://172.16.0.5:/volume1/BACKUP/harvester-lab/"}'
    src = {"cluster": "harvlab", "reachable": True, "backup_target": bt, "backup_target_available": True,
           "longhorn_version": "v1.11.2", "default_backup_jobs": [], "recurring_jobs": {},
           "backing_images": {"vmi-cirros": {"checksum": "c1", "size": 21_700_000}},
           "vms": [
               {"uid": UID_A, "namespace": "default", "name": "rep-a",
                "vm": vm_obj("rep-a", UID_A, [("disk-0", "rep-a-root"), ("data", "rep-a-data")]),
                "disks": [disk("disk-0", "rep-a-root", GiB, "pvc-a0", sc="lh-cirros", image="default/cirros",
                               backing="vmi-cirros"),
                          disk("data", "rep-a-data", 2 * GiB, "pvc-a1")]},
               {"uid": UID_B, "namespace": "default", "name": "rep-b",
                "vm": vm_obj("rep-b", UID_B, [("disk-0", "rep-b-root")], mac="52:54:00:aa:00:02",
                             fw="fw-" + "2" * 8),
                "disks": [disk("disk-0", "rep-b-root", 10 * GiB, "pvc-b0")]}]}
    dst = {"cluster": "harvlab2", "reachable": True, "backup_target": bt.replace("harvester-lab/", "harvester-lab"),
           "backup_target_available": True, "longhorn_version": "v1.12.1", "networks": ["default/lab-net"],
           "namespaces": ["default"], "default_storage_class": "harvester-longhorn",
           "storage_classes": {"harvester-longhorn": {"replicas": 1, "allocatable": 100 * GiB},
                               "harv-rep1": {"replicas": 1, "allocatable": 100 * GiB}},
           "images": {}, "backing_images": {}, "macs": {}, "through_rancher": False}
    for k, v in kw.items():
        side, key = k.split("__")
        (src if side == "src" else dst)[key] = v
    return src, dst


def codes(findings, level=None):
    return [f["code"] for f in findings if level is None or f["level"] == level]


def test_check_clean_plan_two_vms_three_disks():
    src, dst = facts()
    got = rp.check(src, dst, plan_obj())
    assert codes(got, "block") == [] and codes(got, "warn") == ["image-unmapped"]
    assert set(codes(got, "ok")) == {"longhorn-pair-verified", "backing-image-restore"}
    # blocages d'abord
    src, dst = facts(dst__networks=[])
    got = rp.check(src, dst, plan_obj())
    order = [rp._ORDER[f["level"]] for f in got]
    assert order == sorted(order)


def mutate_vm(src, i, **kw):
    src["vms"][i]["vm"] = vm_obj(src["vms"][i]["name"], src["vms"][i]["uid"],
                                 [(d["disk"], d["claim"]) for d in src["vms"][i]["disks"]], **kw)


@pytest.mark.parametrize("setup,plan_kw,code,level", [
    (lambda s, d: d.update(denied=True), {}, "target-denied", "block"),
    (lambda s, d: d.update(reachable=False), {}, "target-unreachable", "block"),
    (lambda s, d: s.update(reachable=False), {}, "source-unreachable", "block"),
    (lambda s, d: s.update(vms=[]), {}, "no-vm", "block"),
    (lambda s, d: d.update(backup_target='{"type":"nfs","endpoint":"nfs://10.0.0.9:/other"}'), {},
     "backup-target-differs", "block"),
    (lambda s, d: d.update(backup_target=None), {}, "backup-target-differs", "block"),
    (lambda s, d: d.update(backup_target_available=False), {}, "backup-target-unavailable", "block"),
    (lambda s, d: s.update(backup_target_available=False), {}, "backup-target-unavailable", "block"),
    (lambda s, d: d.update(longhorn_version="v1.12.0"), {}, "longhorn-pair-unverified", "warn"),
    (lambda s, d: d.update(longhorn_version="v1.10.3"), {}, "longhorn-target-older", "warn"),
    (lambda s, d: d.update(networks=["default/other"]), {}, "network-unmapped", "block"),
    (lambda s, d: d.update(storage_classes={"harv-rep1": {"replicas": 1, "allocatable": 100 * GiB}},
                           default_storage_class=None), {}, "storage-class-unmapped", "block"),
    (lambda s, d: d.update(namespaces=[]), {}, "namespace-missing", "block"),
    (lambda s, d: d.update(storage_classes={"harvester-longhorn": {"replicas": 3, "allocatable": 12 * GiB}}),
     {}, "capacity-short", "block"),
    (lambda s, d: d.update(storage_classes={"harvester-longhorn": {"replicas": 3, "allocatable": 0,
                                                                   "degraded_allocatable": 100 * GiB, "nodes": 1}}),
     {}, "replicas-degraded", "warn"),
    (lambda s, d: d.update(backing_images={"vmi-cirros": {"checksum": "other"}}), {}, "backing-image-conflict",
     "block"),
    (lambda s, d: d.update(backing_images={"vmi-cirros": {"checksum": "c1"}}), {}, "backing-image-reuse", "ok"),
    (lambda s, d: d.update(macs={"52:54:00:aa:00:02": "default/someone"}), {}, "mac-in-use", "warn"),
    (lambda s, d: d.update(macs={"52:54:00:aa:00:02": "default/someone"}), {"phase": "failover"}, "mac-in-use",
     "block"),
    (lambda s, d: mutate_vm(s, 1, mac="52:54:00:aa:00:02", fw=None), {}, "firmware-uuid-unknown", "block"),
    (lambda s, d: None, {"options": dict(rp.DEFAULT_OPTIONS, cloud_init="rerun")}, "cloud-init-rerun", "warn"),
    (lambda s, d: mutate_vm(s, 0, devices=True), {}, "devices-removed", "warn"),
    (lambda s, d: mutate_vm(s, 0, pinned=True), {}, "node-affinity-removed", "warn"),
    (lambda s, d: d.update(through_rancher=True), {}, "target-through-rancher", "warn"),
    (lambda s, d: s["vms"][1]["disks"][0]["lh_volume"]["spec"].update(dataEngine="v2"), {}, "data-engine-v2",
     "block"),
    (lambda s, d: s["vms"][0]["vm"]["spec"]["template"]["spec"]["volumes"].append(
        {"name": "iso", "containerDisk": {"image": "x"}}), {}, "disk-not-replicated", "warn"),
    (lambda s, d: d.update(volumes={rp.target_volume_name(PID, UID_B, "disk-0", 1, "rep-b"): {}}), {},
     "target-volume-taken", "block"),
    (lambda s, d: s.update(recurring_jobs={rp.recurring_job_name(PID, UID_A): {}}), {}, "recurring-job-taken",
     "block"),
    (lambda s, d: d.update(pvcs={"default/rep-b-root": {}}), {}, "target-pvc-taken", "warn"),
    (lambda s, d: d.update(vms={"default/rep-a": {}}), {"phase": "failover"}, "target-vm-taken", "block"),
])
def test_check_one_case_per_code(setup, plan_kw, code, level):
    src, dst = facts()
    setup(src, dst)
    got = rp.check(src, dst, plan_obj(**plan_kw))
    assert (code, level) in [(f["code"], f["level"]) for f in got], got


def test_check_capacity_counts_each_target_class_and_the_backing_image():
    # 1 Gio (image) + 2 Gio + 10 Gio dans harvester-longhorn, plus 21,7 Mo de
    # backing image à restaurer
    need = GiB + 2 * GiB + 10 * GiB + 21_700_000
    src, dst = facts(dst__storage_classes={"harvester-longhorn": {"replicas": 3, "allocatable": need}})
    assert "capacity-short" not in codes(rp.check(src, dst, plan_obj()))
    src, dst = facts(dst__storage_classes={"harvester-longhorn": {"replicas": 3, "allocatable": need - 1}})
    f = [x for x in rp.check(src, dst, plan_obj()) if x["code"] == "capacity-short"][0]
    assert f["facts"] == {"storage_class": "harvester-longhorn", "needed": need, "needed_total": 3 * need,
                          "allocatable": need - 1}


def test_check_mapped_image_uses_the_image_class_and_mac_of_our_own_target_vm_is_fine():
    src, dst = facts(dst__images={"default/cirros2": {"storage_class": "harv-rep1"}},
                     dst__macs={"52:54:00:aa:00:01": "default/rep-a"})
    p = plan_obj(mappings=dict(plan_obj()["mappings"], images={"default/cirros": "default/cirros2"}))
    got = rp.check(src, dst, p)
    assert "image-unmapped" not in codes(got) and "mac-in-use" not in codes(got)
    src, dst = facts()
    p = plan_obj(mappings=dict(plan_obj()["mappings"], images={"default/cirros": "default/absent"}))
    assert ("image-unmapped", "block") in [(f["code"], f["level"]) for f in rp.check(src, dst, p)]


def test_check_mac_new_skips_the_mac_check_and_create_namespace_is_ok():
    src, dst = facts(dst__macs={"52:54:00:aa:00:02": "default/x"}, dst__namespaces=[])
    p = plan_obj(options=dict(rp.DEFAULT_OPTIONS, mac="new", create_namespace=True))
    got = rp.check(src, dst, p)
    assert "mac-in-use" not in codes(got) and ("namespace-missing", "ok") in [(f["code"], f["level"]) for f in got]


# -- intervalle et prévision -----------------------------------------------------

def test_cron_steps_and_expressions():
    assert all((3600 % s == 0) if s < 3600 else (86400 % s == 0) for s in rp.CRON_STEPS)
    assert [rp.cron_for(s) for s in (60, 180, 1800, 3600, 21600, 86400)] == \
        ["*/1 * * * *", "*/3 * * * *", "*/30 * * * *", "0 */1 * * *", "0 */6 * * *", "0 */24 * * *"]
    with pytest.raises(ValueError):
        rp.cron_for(420)


@pytest.mark.parametrize("now,interval,expected", [
    ("2026-10-24T10:00:00Z", 180, "2026-10-24T10:03:00Z"),       # échéance pile : la suivante
    ("2026-10-24T10:01:30Z", 180, "2026-10-24T10:03:00Z"),
    ("2026-10-24T10:59:59Z", 720, "2026-10-24T11:00:00Z"),
    ("2026-10-24T23:30:00Z", 21600, "2026-10-25T00:00:00Z"),
])
def test_next_tick_at(now, interval, expected):
    assert rp.next_tick_at(interval, now) == expected


@pytest.mark.parametrize("objective,send,apply,step,rpo,tenable,reason", [
    # sauvegarde de 25 Mo en 5 s, application 10 s (banc du 22/10)
    (300, 5, 10, 240, 265, True, None),
    (900, 5, 10, 720, 745, True, None),
    (3600, 5, 10, 1800, 1825, True, None),
    # écritures dispersées : 952 Mo envoyés en 14 s, appliqués en 14 s
    (300, 14, 14, 240, 278, True, None),
    (60, 5, 10, 60, 85, False, "objective-too-short"),
    # 240 s de transfert : 240 s dériverait (240 > 0,8 x 240), 300 s non (marge
    # pile) mais donne 550 s, au-delà de l'objectif
    (500, 120, 120, 300, 550, False, "objective-too-short"),
    (600, 120, 120, 300, 550, True, None),
    (3600, 50000, 50000, 86400, 186410, False, "drift-at-every-step"),
])
def test_derive_interval_grid(objective, send, apply, step, rpo, tenable, reason):
    got = rp.derive_interval(objective, send, apply)
    s, r, ok = got
    assert (s, r, ok, got.reason) == (step, rpo, tenable, reason)


def test_derive_interval_margin_decides():
    # 50 s de transfert pour un pas de 60 s : dérive avec 20 % de marge
    assert rp.derive_interval(3600, 25, 25, margin=0.2).step == 1800
    assert rp.derive_interval(130, 25, 25, margin=0.2).tenable is False
    assert rp.derive_interval(130, 25, 25, margin=0.1) == (60, 120, True)


def test_blocks_forecast_scattered_writes_on_the_bench():
    # 600 écritures de 4 Kio dispersées sur 2 Gio : relevé 450 à 458 blocs,
    # 944 à 960 Mo envoyés ; prévision 454 blocs, 952,6 Mo
    f = rp.blocks_forecast(2 * GiB, 600)
    assert f["blocks"] == 1024 and round(f["upper_blocks"]) == 454
    assert round(f["upper_bytes"] / 1e6, 1) == 952.6
    assert 944e6 <= f["upper_bytes"] <= 960e6
    assert f["lower_blocks"] == 2 and f["bytes_written"] == 2_457_600


def test_blocks_forecast_sequential_writes_on_the_bench():
    # 3 x 8 Mio par passage : 12 blocs ; 25 174 351 octets envoyés (lz4 sur
    # des données aléatoires, à 0,04 % près)
    f = rp.blocks_forecast(2 * GiB, 3, write_size=8 * MiB)
    assert f["lower_blocks"] == 12 and f["lower_bytes"] == 25_165_824
    assert f["upper_blocks"] >= f["lower_blocks"]
    assert abs(25_174_351 - f["lower_bytes"]) / f["lower_bytes"] < 0.001


@pytest.mark.parametrize("size,writes,ws,block,lo,hi", [
    (2 * GiB, 0, 4096, 2 * MiB, 0, 0),
    (2 * GiB, 10 ** 6, 4096, 2 * MiB, 1024, 1024),               # plafonné au volume
    (2 * GiB, 600, 4096, 16 * MiB, 1, 127),                      # bloc de 16 Mio : 127 blocs sur 128
])
def test_blocks_forecast_bounds(size, writes, ws, block, lo, hi):
    f = rp.blocks_forecast(size, writes, block=block, write_size=ws)
    assert f["lower_blocks"] == lo and round(f["upper_blocks"]) == hi


def test_aggregate_upper_sums_consecutive_passes():
    m = [25, 25, 30, 20, 25]
    assert rp.aggregate_upper(m, 1)["max"] == 30
    a = rp.aggregate_upper(m, 2)
    assert a["samples"] == [50, 55, 50, 45] and a["max"] == 55 and a["p95"] == 55 and a["mean"] == 50
    assert rp.aggregate_upper(m, 5)["samples"] == [125]
    assert rp.aggregate_upper(m, 6)["samples"] == []
    assert rp.aggregate_upper([900, 900], 2, cap=1024)["samples"] == [1024]
    with pytest.raises(ValueError):
        rp.aggregate_upper(m, 0)


def test_first_copy_eta():
    # première sauvegarde complète du disque de 2 Gio : 30 s à 72 Mo/s
    assert round(rp.first_copy_eta(2 * GiB, 72e6)) == 30
    assert rp.first_copy_eta(2 * GiB, None) is None and rp.first_copy_eta(2 * GiB, 0) is None


# -- état d'un plan --------------------------------------------------------------

def vrow(uid, disk, vol):
    return {"vm_uid": uid, "disk": disk, "source_volume": vol, "generation": 1, "state": "standby",
            "target_volume": "hr-" + vol}


SVOLS = [vrow(UID_A, "disk-0", "pvc-a0"), vrow(UID_A, "data", "pvc-a1"), vrow(UID_B, "disk-0", "pvc-b0")]


def tick(vol, snap, state="applied", up=25_174_351, dur=5, resync=10, apply=9, backup=None):
    t = {"source_volume": vol, "backup": backup or f"b-{vol}-{snap}", "generation": 1,
         "snapshot_at": T(snap), "backup_started_at": T(snap + 1), "state": state, "newly_uploaded": up}
    if state != "in_progress":
        t["backup_done_at"] = T(snap + 1 + dur)
    if state == "applied":
        t["resync_at"] = T(snap + 1 + dur + 1)
        t["applied_at"] = T(snap + 1 + dur + 1 + apply)
    return t


def pplan(**kw):
    return plan_obj(**dict({"state": "protected", "interval_s": 180, "rpo_objective_s": 300}, **kw))


def res(uid, name, state="protected", **kw):
    return dict({"resource_uid": uid, "namespace": "default", "name": name, "state": state}, **kw)


RES2 = [res(UID_A, "rep-a"), res(UID_B, "rep-b")]


def test_plan_status_oldest_applied_point_and_stats():
    ticks = [tick("pvc-a0", -360), tick("pvc-a0", -180), tick("pvc-a1", -181), tick("pvc-b0", -180),
             tick("pvc-a1", 0, state="in_progress")]
    s = rp.plan_status(pplan(), RES2, SVOLS, ticks, NOW, {})
    a, b = s["vms"]
    assert (a["rpo_held_s"], a["last_applied_at"], a["skew_s"], a["backlog"]) == (181, T(-181), 1, 1)
    assert a["throughput"]["upload"]["bps"] == pytest.approx(25_174_351 / 5)
    assert a["throughput"]["apply"]["seconds"] == 9
    assert (b["rpo_held_s"], b["exceeded"], b["state"]) == (180, False, "protected")
    assert (s["rpo_held_s"], s["backlog"], s["next_tick_at"], s["exceeded"]) == (181, 1, T(180), False)
    assert s["resources_by_state"] == {"protected": 2}


def test_plan_status_without_any_tick():
    s = rp.plan_status(pplan(state="protecting"), [res(UID_A, "rep-a", "protecting")], SVOLS, [], NOW, {})
    assert s["rpo_held_s"] is None and s["vms"][0]["last_applied_at"] is None and s["vms"][0]["state"] == "protecting"


def test_plan_status_disk_late_is_the_vm_rpo_and_exceeds():
    ticks = [tick("pvc-a0", -60), tick("pvc-a1", -400)]
    v = rp.plan_status(pplan(interval_s=600), RES2[:1], SVOLS, ticks, NOW, {})["vms"][0]
    assert (v["rpo_held_s"], v["exceeded"], v["state"], v["reasons"]) == (400, True, "lagging", ["rpo-exceeded"])


def test_stopped_vm_since_before_its_point_is_up_to_date():
    ticks = [tick("pvc-a0", -3600), tick("pvc-a1", -3600)]
    plan = pplan(interval_s=600)
    stopped_before = {UID_A: {"running": False, "stopped_at": T(-4000)}}
    v = rp.plan_status(plan, RES2[:1], SVOLS, ticks, NOW, stopped_before)["vms"][0]
    assert (v["up_to_date"], v["rpo_held_s"], v["exceeded"], v["state"]) == (True, 0, False, "protected")
    stopped_after = {UID_A: {"running": False, "stopped_at": T(-1000)}}
    v = rp.plan_status(plan, RES2[:1], SVOLS, ticks, NOW, stopped_after)["vms"][0]
    assert (v["up_to_date"], v["rpo_held_s"], v["state"]) == (False, 3600, "lagging")
    # arrêtée sans date connue : pas de « à jour » présumé
    assert rp.plan_status(plan, RES2[:1], SVOLS, ticks, NOW, {UID_A: False})["vms"][0]["up_to_date"] is False


@pytest.mark.parametrize("ticks,drift", [
    ([tick("pvc-a0", -100), tick("pvc-a1", -100)], False),
    ([tick("pvc-a0", -300, apply=200), tick("pvc-a1", -100)], True),               # appliqué trop tard
    ([tick("pvc-a0", -100), tick("pvc-a1", -100), tick("pvc-a1", -250, state="completed", backup="late")], True),
    ([tick("pvc-a0", -100), tick("pvc-a1", -100), tick("pvc-a1", -20, state="in_progress")], False),
])
def test_drift_when_a_pass_takes_more_than_one_step(ticks, drift):
    v = rp.plan_status(pplan(rpo_objective_s=3600), RES2[:1], SVOLS, ticks, NOW, {})["vms"][0]
    assert v["drift"] is drift and (v["state"] == "drifting") is drift


def test_spec_drift_uses_metadata_generation_not_resource_version():
    vm = vm_obj("rep-a", UID_A, [("disk-0", "c")])
    r = res(UID_A, "rep-a", vm_generation=rp.vm_spec_generation(vm))
    ticks = [tick("pvc-a0", -10), tick("pvc-a1", -10)]
    # dossier de la console posé en annotation : resourceVersion bouge, pas la generation
    moved = copy.deepcopy(vm)
    moved["metadata"]["resourceVersion"] = "2000"
    moved["metadata"].setdefault("annotations", {})["harvester-ops.io/folder"] = "prod"
    st = rp.resource_state(pplan(), r, SVOLS, ticks, NOW, {}, {"vm_generation": rp.vm_spec_generation(moved)})
    assert st == "protected"
    changed = copy.deepcopy(vm)
    changed["metadata"]["generation"] = 5
    assert rp.resource_state(pplan(), r, SVOLS, ticks, NOW, {},
                             {"vm_generation": rp.vm_spec_generation(changed)}) == "drifting"


@pytest.mark.parametrize("health,ticks,state", [
    ({"source_reachable": False}, [tick("pvc-a0", -10), tick("pvc-a1", -10)], "source-unreachable"),
    ({"standby_degraded": True}, [tick("pvc-a0", -10), tick("pvc-a1", -10)], "degraded"),
    ({}, [tick("pvc-a0", -10), tick("pvc-a1", -10), tick("pvc-a1", -5, state="error", backup="e")], "degraded"),
    ({}, [tick("pvc-a0", -10), tick("pvc-a1", -10)], "protected"),
])
def test_resource_state_from_health(health, ticks, state):
    assert rp.resource_state(pplan(), res(UID_A, "rep-a"), SVOLS, ticks, NOW, {}, health) == state


@pytest.mark.parametrize("stored", ["draft", "measuring", "protecting", "failing_over", "failed_over",
                                    "unprotecting", "unprotected", "error"])
def test_lifecycle_states_are_returned_as_stored(stored):
    late = [tick("pvc-a0", -9000), tick("pvc-a1", -9000)]
    assert rp.resource_state(pplan(state=stored), res(UID_A, "rep-a", stored), SVOLS, late, NOW, {}) == stored


# -- actions ---------------------------------------------------------------------

P = ("pause",)
FULL = ("failover", "failover-unplanned", "pause", "unprotect", "unprotect-delete-backups")
SRC_FULL = ("pause", "unprotect", "unprotect-delete-backups")


@pytest.mark.parametrize("state,target,source", [
    ("draft", ("measure-start", "protect"), ("measure-start", "protect")),
    ("measuring", ("measure-stop", "protect", "pause", "unprotect"), ("measure-stop", "protect", "pause", "unprotect")),
    ("protecting", ("protect", "pause", "unprotect", "unprotect-delete-backups"),
     ("protect", "pause", "unprotect", "unprotect-delete-backups")),
    ("protected", FULL, SRC_FULL),
    ("lagging", FULL, SRC_FULL),
    ("drifting", ("protect",) + FULL, ("protect",) + SRC_FULL),
    ("degraded", FULL, SRC_FULL),
    ("source-unreachable", ("failover-unplanned", "pause", "unprotect"), ("failover-unplanned", "pause", "unprotect")),
    ("failing_over", ("failover",), ()),
    ("failed_over", ("unprotect", "unprotect-delete-backups"), ("unprotect", "unprotect-delete-backups")),
    ("unprotecting", ("unprotect", "unprotect-delete-backups"), ("unprotect", "unprotect-delete-backups")),
    ("unprotected", (), ()),
    ("error", ("protect", "unprotect", "unprotect-delete-backups"), ("protect", "unprotect", "unprotect-delete-backups")),
])
def test_actions_per_state_and_side(state, target, source):
    plan = pplan()
    r = res(UID_A, "rep-a", state)
    assert tuple(a["name"] for a in rp.allowed_actions(plan, r, "target")) == target
    assert tuple(a["name"] for a in rp.allowed_actions(plan, r, "source")) == source
    assert rp.allowed_actions(plan, r, "any") == rp.allowed_actions(plan, r, "target")
    for a in rp.allowed_actions(plan, r, "target"):
        assert a["role"] == rp.ACTION_ROLES[a["name"]] and a["allowed"] is True


def test_action_roles_and_role_filter():
    r = res(UID_A, "rep-a", "protected")
    acts = {a["name"]: a for a in rp.allowed_actions(pplan(), r, "target", role="operator")}
    assert (acts["failover"]["role"], acts["failover"]["allowed"], acts["failover"]["reason"]) == ("admin", False, "role")
    assert acts["unprotect-delete-backups"]["allowed"] is False and acts["unprotect"]["allowed"] is True
    assert all(not a["allowed"] for a in rp.allowed_actions(pplan(), r, "target", role="viewer"))
    assert all(a["allowed"] for a in rp.allowed_actions(pplan(), r, "target", role="admin"))
    assert all(not a["allowed"] for a in rp.allowed_actions(pplan(), r, "target", role="nobody"))


def test_paused_plan_offers_resume_and_error_after_failover_offers_failover():
    paused = pplan(options=dict(rp.DEFAULT_OPTIONS, paused=True))
    names = [a["name"] for a in rp.allowed_actions(paused, res(UID_A, "rep-a"), "target")]
    assert "resume" in names and "pause" not in names
    err = res(UID_A, "rep-a", "error", error_origin="failing_over")
    assert [a["name"] for a in rp.allowed_actions(pplan(), err, "target")][:2] == ["protect", "failover"]


def test_plan_actions_from_mixed_resource_states():
    rs = [res(UID_A, "rep-a", "protected"), res(UID_B, "rep-b", "source-unreachable"),
          res("u3", "rep-c", "failed_over")]
    acts = {a["name"]: a["count"] for a in rp.plan_actions(pplan(), "target", rs)}
    assert acts == {"failover": 1, "failover-unplanned": 2, "pause": 2, "unprotect": 3,
                    "unprotect-delete-backups": 2}
    src = {a["name"] for a in rp.plan_actions(pplan(), "source", rs)}
    assert "failover" not in src and "failover-unplanned" in src             # source injoignable
    draft = {a["name"] for a in rp.plan_actions(plan_obj(), "any", [])}
    assert draft == {"measure-start", "protect", "delete"}
    gone = rp.plan_actions(pplan(state="unprotected"), "any", [res(UID_A, "rep-a", "unprotected")])
    assert [a["name"] for a in gone] == ["delete"]


# -- lignes ----------------------------------------------------------------------

def test_plan_row_and_resource_row_share_one_shape():
    ticks = [tick("pvc-a0", -60), tick("pvc-a1", -60), tick("pvc-b0", -500)]
    events = [{"id": i, "ts": T(-i), "plan_id": PID, "resource_uid": UID_A, "step": f"s{i}"} for i in range(5)]
    plan = pplan(source_host_kind="harvester", target_host_kind="harvester")
    row = rp.plan_row(plan, RES2, SVOLS, ticks, events, NOW, {}, role="operator", side="target")
    assert (row["source_host"], row["target_host"], row["rpo_held_s"], row["resources_by_state"]) == \
        ("harvlab", "harvlab2", 500, {"protected": 1, "lagging": 1})
    assert [e["step"] for e in row["last_events"]] == ["s0", "s1", "s2"]
    direct = rp.resource_row(plan, RES2[0], "target", SVOLS, ticks, events, NOW, {}, role="operator")
    assert row["resources"][0] == direct
    assert set(direct) == {"plan", "plan_name", "resource_kind", "resource_uid", "namespace", "name", "side",
                           "host_kind", "peer_host", "state", "reasons", "state_since", "rpo_held_s",
                           "rpo_objective_s", "last_applied_at", "last_events", "actions"}
    assert (direct["peer_host"], direct["rpo_held_s"], direct["last_applied_at"]) == ("harvlab", 60, T(-60))
    assert rp.resource_row(plan, RES2[0], "source", SVOLS, ticks, events, NOW, {})["peer_host"] == "harvlab2"
    json.dumps(row)                                                     # sérialisable tel quel


# -- manifestes, comparés aux objets du banc (/tmp/drspike2) ----------------------

BENCH = yaml.safe_load_all("""
apiVersion: longhorn.io/v1beta2
kind: RecurringJob
metadata: {name: dr2-fb-bak, namespace: longhorn-system}
spec: {name: dr2-fb-bak, task: backup, cron: "*/3 * * * *", retain: 3, concurrency: 2, groups: [], labels: {}}
---
apiVersion: longhorn.io/v1beta2
kind: BackingImage
metadata: {name: vmi-bd54d41f-42e5-4871-8224-8b03b19f6765, namespace: longhorn-system, labels: {dr2-spike: "true"}}
spec:
  sourceType: restore
  sourceParameters:
    backup-target-name: default
    backup-url: "nfs://172.16.0.5:/volume1/BACKUP/harvester-lab/?backingImage=vmi-bd54d41f-42e5-4871-8224-8b03b19f6765"
    concurrent-limit: "2"
  checksum: 9a9bce0083a00939ec17c11febbfc767aa211aaa54f51e75c5a8b271a9b5637c77205a518b7a2007cb391d23cceb01e0e4e8d64832317151bc85b734b92a7be0
  dataEngine: v1
  minNumberOfCopies: 1
---
apiVersion: longhorn.io/v1beta2
kind: Volume
metadata: {name: dr2-fb-root-sb, namespace: longhorn-system}
spec:
  Standby: true
  fromBackup: "nfs://172.16.0.5:/volume1/BACKUP/harvester-lab/?backup=backup-b6494501ed6045e7&volume=pvc-3fff1a23-153d-44d0-8627-4e63480e918d"
  size: "1073741824"
  numberOfReplicas: 1
  accessMode: rwx
  migratable: true
  dataEngine: v1
  frontend: ""
  backingImage: vmi-4358f8a5-32ad-4e36-a07b-2ce072ab3153
---
apiVersion: longhorn.io/v1beta2
kind: Volume
metadata: {name: dr2-fb-data-ret, namespace: longhorn-system}
spec:
  Standby: true
  fromBackup: "nfs://172.16.0.5:/volume1/BACKUP/harvester-lab/?backup=backup-c309de3338914aa6&volume=dr2-fb-data-sb"
  size: "2147483648"
  numberOfReplicas: 2
  accessMode: rwx
  migratable: true
  dataEngine: v1
  frontend: ""
---
apiVersion: longhorn.io/v1beta2
kind: Snapshot
metadata: {name: dr2-fb-final-data, namespace: longhorn-system}
spec: {volume: dr2-fb-data-sb, createSnapshot: true}
---
apiVersion: longhorn.io/v1beta2
kind: Backup
metadata: {name: dr2-fb-final-data, namespace: longhorn-system, labels: {backup-volume: dr2-fb-data-sb, backup-target: default}}
spec: {snapshotName: dr2-fb-final-data}
---
apiVersion: v1
kind: PersistentVolume
metadata: {name: dr2-fb-data-sb}
spec:
  capacity: {storage: 2Gi}
  volumeMode: Block
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: harv-rep1
  csi: {driver: driver.longhorn.io, volumeHandle: dr2-fb-data-sb}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: dr2-fb-root, namespace: default, annotations: {harvesterhci.io/imageId: default/cirros}}
spec: {accessModes: [ReadWriteMany], volumeMode: Block, storageClassName: lh-4358f8a5-32ad-4e36-a07b-2ce072ab3153, volumeName: dr2-fb-root-sb, resources: {requests: {storage: 1Gi}}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: dr2-fb-data, namespace: default}
spec: {accessModes: [ReadWriteMany], volumeMode: Block, storageClassName: harv-rep1, volumeName: dr2-fb-data-sb, resources: {requests: {storage: 2Gi}}}
""")
BENCH = list(BENCH)
LABELS = rp.plan_labels(PID, 1, UID_A)


def strip_ours(obj):
    """L'objet sans nos étiquettes (le banc n'en posait pas)."""
    o = copy.deepcopy(obj)
    md = o["metadata"]
    labels = {k: v for k, v in (md.get("labels") or {}).items() if not k.startswith("harvester-ops.io/")}
    if labels:
        md["labels"] = labels
    else:
        md.pop("labels", None)
    return o


def same(ours, bench):
    """Égalité à l'octet près de la forme JSON (ordre des clés compris)."""
    assert json.dumps(ours) == json.dumps(bench)


def test_recurring_job_matches_the_bench():
    rj = rp.recurring_job("dr2-fb-bak", "*/3 * * * *", 3, 2, PID, UID_A)
    assert rj["metadata"]["labels"] == LABELS and rj["spec"]["labels"] == LABELS
    bare = strip_ours(rj)
    bare["spec"]["labels"] = {}
    same(bare, BENCH[0])
    with pytest.raises(ValueError):
        rp.recurring_job("x", "*/3 * * * *", 2, 1, PID, UID_A)
    with pytest.raises(ValueError):
        rp.recurring_job("x", "*/3 * * * *", 51, 1, PID, UID_A)
    with pytest.raises(ValueError):
        rp.recurring_job("x", "*/3 * * * *", 6, 0, PID, UID_A)
    assert rp.job_label("dr2-fb-bak") == ("recurring-job.longhorn.io/dr2-fb-bak", "enabled")


def test_backing_image_restore_matches_the_bench():
    b = BENCH[1]
    ours = rp.backing_image_restore(b["metadata"]["name"], b["spec"]["checksum"],
                                    b["spec"]["sourceParameters"]["backup-url"], labels={"dr2-spike": "true"})
    same(ours, b)


@pytest.mark.parametrize("i,backing", [(2, "vmi-4358f8a5-32ad-4e36-a07b-2ce072ab3153"), (3, None)])
def test_standby_volume_matches_the_bench(i, backing):
    b = BENCH[i]["spec"]
    ours = rp.standby_volume(BENCH[i]["metadata"]["name"], b["fromBackup"], int(b["size"]), "rwx", True,
                             b["numberOfReplicas"], backing, class_params={}, labels=LABELS)
    assert ours["metadata"]["labels"] == LABELS
    same(strip_ours(ours), BENCH[i])


def test_standby_volume_copies_the_target_class_placement():
    v = rp.standby_volume("v", "u", GiB, "rwo", False, 3, class_params={
        "numberOfReplicas": "3", "dataLocality": "best-effort", "diskSelector": "ssd, fast",
        "nodeSelector": "", "staleReplicaTimeout": "30", "migratable": "true"})
    s = v["spec"]
    assert (s["dataLocality"], s["diskSelector"], s["staleReplicaTimeout"], s["migratable"]) == \
        ("best-effort", ["ssd", "fast"], 30, False)
    assert "nodeSelector" not in s and "backingImage" not in s
    with pytest.raises(ValueError):
        rp.standby_volume("x" * 41, "u", GiB, "rwo", False, 1)


def test_snapshot_backup_and_activation_match_the_bench():
    same(strip_ours(rp.snapshot("dr2-fb-data-sb", "dr2-fb-final-data", labels=LABELS)), BENCH[4])
    bk = rp.backup("dr2-fb-data-sb", "dr2-fb-final-data", labels=LABELS)
    assert bk["metadata"]["labels"][rp.LABEL_PLAN] == PID
    same(strip_ours(bk), BENCH[5])
    assert rp.activation_patch() == {"spec": {"Standby": False, "frontend": "blockdev"}}


def test_static_pv_and_pvc_match_the_bench():
    same(strip_ours(rp.static_pv("dr2-fb-data-sb", 2 * GiB, "Block", ["ReadWriteMany"], "harv-rep1",
                                 labels=LABELS)), BENCH[6])
    same(rp.static_pvc("dr2-fb-root", "default", "dr2-fb-root-sb", GiB, "Block", ["ReadWriteMany"],
                       "lh-4358f8a5-32ad-4e36-a07b-2ce072ab3153", image_id="default/cirros"), BENCH[7])
    same(strip_ours(rp.static_pvc("dr2-fb-data", "default", "dr2-fb-data-sb", 2 * GiB, "Block", ["ReadWriteMany"],
                                  "harv-rep1", labels=LABELS)), BENCH[8])
    assert rp.quantity(1536 * MiB) == "1536Mi" and rp.quantity(1000) == "1000"


# -- VM cible -------------------------------------------------------------------

def test_target_vm_keep_copies_the_firmware_uuid_and_the_mac():
    src = vm_obj("rep-a", UID_A, [("disk-0", "rep-a-root")], nets=("default/prod",))
    src["metadata"]["annotations"] = {"kubevirt.io/latest-observed-api-version": "v1"}
    resource = res(UID_A, "rep-a", firmware_uuid="fw-11111111")
    maps = {"networks": {"default/prod": "default/lab-net"}, "namespaces": {"default": "dr"}}
    vm, removed = rp.target_vm(src, resource, maps, {"cloud_init": "keep", "mac": "keep", "start": "started"},
                               {"rep-a-root": "rep-a-root-dr"}, PID)
    tspec = vm["spec"]["template"]["spec"]
    assert (vm["metadata"]["namespace"], vm["metadata"]["name"]) == ("dr", "rep-a")
    assert vm["metadata"]["labels"] == rp.plan_labels(PID, 1, UID_A)
    assert "uid" not in vm["metadata"] and "annotations" not in vm["metadata"]
    assert tspec["domain"]["firmware"]["uuid"] == "fw-11111111"
    assert tspec["domain"]["devices"]["interfaces"][0]["macAddress"] == "52:54:00:aa:00:01"
    assert tspec["networks"][0]["multus"]["networkName"] == "default/lab-net"
    assert tspec["volumes"][0]["persistentVolumeClaim"]["claimName"] == "rep-a-root-dr"
    assert vm["spec"]["runStrategy"] == "RerunOnFailure" and removed == []
    assert src["metadata"]["uid"] == UID_A                                   # la source n'est pas touchée


def test_target_vm_rerun_drops_the_firmware_uuid_new_mac_and_stopped():
    src = vm_obj("rep-a", UID_A, [("disk-0", "c")], devices=True)
    vm, removed = rp.target_vm(src, res(UID_A, "rep-a"), {"target_names": {"default/rep-a": "rep-a-dr"}},
                               {"cloud_init": "rerun", "mac": "new", "start": "stopped"}, {}, PID, generation=2)
    tspec = vm["spec"]["template"]["spec"]
    assert "firmware" not in tspec["domain"]
    assert "macAddress" not in tspec["domain"]["devices"]["interfaces"][0]
    assert vm["spec"]["runStrategy"] == "Halted" and vm["metadata"]["name"] == "rep-a-dr"
    assert vm["metadata"]["labels"][rp.LABEL_GENERATION] == "2" and removed == ["hostDevice:nvidia.com/A30"]


def test_target_vm_keep_without_firmware_uuid_refuses():
    with pytest.raises(ValueError, match="firmware uuid"):
        rp.target_vm(vm_obj("rep-a", UID_A, [], fw=None), res(UID_A, "rep-a"), {}, {"cloud_init": "keep"}, {}, PID)
