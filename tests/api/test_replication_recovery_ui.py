"""Écrans de réplication (BE1) : modèle de la reprise, sans cluster.

Correspondances par disque (`disk_classes`) et réseaux d'exercice
(`drill_networks`) ; options de reprise (`waves`, `hooks`, `network_data`,
`resize`, `auto_protect`) contrôlées par `normalize_plan` ; secrets des
actions annexes masqués à la lecture et gardés à la mise à jour ; classe
cible d'un disque ; VM cible retaillée et reconfigurée ; règles d'état des
réglages ; ordre des vagues d'une bascule."""

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import replication as rp  # noqa: E402

VMS = ["default/web-1", "default/web-2", "default/db-1"]


def req(vms=VMS, selector="", options=None, mappings=None):
    r = {"name": "erp", "source_cluster": "harvlab", "target_cluster": "harvlab2", "rpo_objective_s": "15m",
         "vms": list(vms) if vms else [], "selector": selector, "namespace": "default" if selector else "",
         "options": options or {}, "mappings": mappings or {}}
    return r


HOOK = {"name": "dns-switch", "at": "start", "method": "POST", "url": "https://hooks.example/dns",
        "headers": {"Content-Type": "application/json"}, "secret_headers": {"Authorization": "Bearer s3cr3t"},
        "body": "{\"zone\": \"erp\"}", "timeout_s": 20}


# ---------------------------------------------------------------------------
# Correspondances
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,raw,ok", [
    ("disk_classes", {"default/web-1/rootdisk": "fast"}, True),
    ("disk_classes", {"default/web-1": "fast"}, False),                 # disque manquant
    ("disk_classes", {"default/web-1/root/x": "fast"}, False),
    ("disk_classes", {"default/Web-1/rootdisk": "fast"}, False),
    ("disk_classes", {"default/web-1/rootdisk": "Fast"}, False),
    ("disk_classes", {"default/web-1/rootdisk": 3}, False),
    ("drill_networks", {"default/prod": "default/drill"}, True),
    ("drill_networks", {"default/prod": "drill"}, False),
    ("drill_networks", {"prod": "default/drill"}, False),
])
def test_new_mapping_kinds_are_validated(kind, raw, ok):
    if ok:
        assert rp.normalize_plan(req(mappings={kind: raw}))["mappings"][kind] == raw
    else:
        with pytest.raises(rp.PlanError) as e:
            rp.normalize_plan(req(mappings={kind: raw}))
        assert e.value.code in ("bad-mapping", "bad-reference")


def test_every_plan_has_every_mapping_kind():
    n = rp.normalize_plan(req())
    assert set(n["mappings"]) == set(rp.MAPPING_KINDS) and "disk_classes" in n["mappings"]


DST = {"storage_classes": {"harvester-longhorn": {}, "fast": {}, "img-sc": {}},
       "images": {"default/img-t": {"storage_class": "img-sc"}}, "default_storage_class": "harvester-longhorn"}


@pytest.mark.parametrize("disk,maps,vm_ref,want,finding", [
    # classe choisie pour ce disque : l'emporte sur l'image et la classe mappées
    ({"disk": "root", "storage_class": "lh-x", "image": "default/img-s"},
     {"images": {"default/img-s": "default/img-t"}, "disk_classes": {"default/web-1/root": "fast"}},
     "default/web-1", "fast", None),
    ({"disk": "data", "storage_class": "slow"},
     {"storage_classes": {"slow": "harvester-longhorn"}, "disk_classes": {"default/web-1/data": "fast"}},
     "default/web-1", "fast", None),
    # autre disque, autre VM : la correspondance par disque ne s'applique pas
    ({"disk": "data", "storage_class": "slow"},
     {"storage_classes": {"slow": "harvester-longhorn"}, "disk_classes": {"default/web-2/data": "fast"}},
     "default/web-1", "harvester-longhorn", None),
    # sans VM donnée (appelant d'avant) : comportement d'avant
    ({"disk": "data", "storage_class": "slow"}, {"disk_classes": {"default/web-1/data": "fast"}},
     None, None, "storage-class-unmapped"),
    # classe choisie absente de la cible : blocage, avec le disque
    ({"disk": "data", "storage_class": "slow"}, {"disk_classes": {"default/web-1/data": "gold"}},
     "default/web-1", None, "storage-class-unmapped"),
    ({"disk": "root", "storage_class": "lh-x", "image": "default/img-s"},
     {"images": {"default/img-s": "default/img-t"}}, "default/web-1", "img-sc", None),
])
def test_target_class_honours_disk_classes(disk, maps, vm_ref, want, finding):
    sc, f = rp._target_class(disk, {"mappings": maps}, DST, vm_ref)
    assert sc == want
    assert (f or {}).get("code") == finding
    if finding and vm_ref:
        assert f["level"] == "block" and f["facts"]["disk"] == f"{vm_ref}/{disk['disk']}"


def test_check_passes_the_vm_ref_to_the_class_choice():
    """`check` : une classe choisie absente de la cible bloque le contrôle."""
    vm = {"uid": "u1", "namespace": "default", "name": "web-1", "vm": {"spec": {"template": {"spec": {}}}},
          "firmware_uuid": "fw", "disks": [{"disk": "data", "claim": "c", "size": 1, "storage_class": "harvester-longhorn",
                                            "image": None, "lh_volume": None, "pvc": {}, "sc": {}}]}
    src = {"cluster": "harvlab", "reachable": True, "backup_target": "nfs://x:/y", "backup_target_available": True,
           "vms": [vm]}
    dst = dict(DST, cluster="harvlab2", reachable=True, backup_target="nfs://x:/y", backup_target_available=True,
               networks=[], namespaces=["default"])
    plan = dict(rp.normalize_plan(req(vms=["default/web-1"], mappings={"disk_classes": {"default/web-1/data": "gold"}})),
                id="rp-0123456789ab", generation=1, phase="failover-unplanned")
    codes = [(f["code"], f["facts"].get("disk")) for f in rp.check(src, dst, plan) if f["level"] == "block"]
    assert ("storage-class-unmapped", "default/web-1/data") in codes


# ---------------------------------------------------------------------------
# Options de reprise
# ---------------------------------------------------------------------------

def test_recovery_defaults_keep_the_old_options_shape():
    opts = rp.normalize_plan(req())["options"]
    assert not set(opts) & set(rp.RECOVERY_KEYS)


def test_recovery_normalized():
    opts = rp.normalize_plan(req(options={
        "cloud_init": "rerun",
        "waves": [{"name": "databases", "vms": ["default/db-1"], "gate": {"kind": "agent"}},
                  {"vms": ["default/web-1"], "gate": {"kind": "delay", "seconds": 30}},
                  {"vms": ["default/web-2"]}],
        "hooks": [dict(HOOK, timeout_s=None, method=None)],
        "network_data": {"default/web-1": "version: 2\n"},
        "resize": {"default/db-1": {"cpu": 2, "memory": "4Gi"}, "default/web-1": {"memory": "512Mi"}},
        "accept_degraded_standby": True}))["options"]
    assert opts["waves"] == [
        {"name": "databases", "vms": ["default/db-1"], "gate": {"kind": "agent", "seconds": 300}},
        {"name": "", "vms": ["default/web-1"], "gate": {"kind": "delay", "seconds": 30}},
        {"name": "", "vms": ["default/web-2"], "gate": {"kind": "running", "seconds": 0}}]
    assert opts["hooks"][0]["method"] == "POST" and opts["hooks"][0]["timeout_s"] == 15
    assert opts["resize"]["default/web-1"] == {"memory": "512Mi"} and opts["accept_degraded_standby"] is True


def _wave(vms, **kw):
    return dict({"vms": vms}, **kw)


@pytest.mark.parametrize("options,message", [
    ({"waves": "db first"}, "waves: a list"),
    ({"waves": [_wave(["default/db-1"])] * 11}, "at most 10"),
    ({"waves": [_wave([])]}, "non-empty"),
    ({"waves": [_wave(["default/db-1"]), _wave(["default/db-1"])]}, "listed twice"),
    ({"waves": [_wave(["default/db-1", "default/db-1"])]}, "listed twice"),
    ({"waves": [_wave(["default/other"])]}, "not a VM of this plan"),
    ({"waves": [_wave(["db-1"])]}, "namespace/name"),
    ({"waves": [_wave(["default/db-1"], name="x" * 41)]}, "name"),
    ({"waves": [_wave(["default/db-1"], name="a\nb")]}, "control character"),
    ({"waves": [_wave(["default/db-1"], extra=1)]}, "expected"),
    ({"waves": [_wave(["default/db-1"], gate={"kind": "ping"})]}, "gate kind"),
    ({"waves": [_wave(["default/db-1"], gate={"kind": "agent", "seconds": 5})]}, "agent gate seconds"),
    ({"waves": [_wave(["default/db-1"], gate={"kind": "agent", "seconds": 4000})]}, "agent gate seconds"),
    ({"waves": [_wave(["default/db-1"], gate={"kind": "delay"})]}, "delay gate seconds"),
    ({"waves": [_wave(["default/db-1"], gate={"kind": "delay", "seconds": True})]}, "delay gate seconds"),
    ({"waves": [_wave(["default/db-1"], gate={"kind": "delay", "seconds": 3601})]}, "delay gate seconds"),
    ({"waves": [_wave(["default/db-1"], gate={"kind": "running", "every": 1})]}, "gate"),
    ({"hooks": {"name": "x"}}, "hooks: a list"),
    ({"hooks": [dict(HOOK, name=f"h{i}") for i in range(21)]}, "at most 20"),
    ({"hooks": [HOOK, HOOK]}, "used twice"),
    ({"hooks": [dict(HOOK, name="Bad")]}, "RFC 1123"),
    ({"hooks": [dict(HOOK, name="a" * 41)]}, "RFC 1123"),
    ({"hooks": [dict(HOOK, at="later")]}, "at, one of"),
    ({"hooks": [dict(HOOK, at="wave:2")]}, "wave:1"),          # sans vague : seule la vague implicite 1
    ({"hooks": [dict(HOOK, at="wave:0")]}, "at, one of"),
    ({"hooks": [dict(HOOK, method="TRACE")]}, "method"),
    ({"hooks": [dict(HOOK, url="ftp://x/y")]}, "http:// or https://"),
    ({"hooks": [dict(HOOK, url="https://")]}, "http:// or https://"),
    ({"hooks": [dict(HOOK, url="https://user:pw@hooks.example/")]}, "no credentials"),
    ({"hooks": [dict(HOOK, url="https://h.example/a b")]}, "without spaces"),
    ({"hooks": [dict(HOOK, url="https://h.example:99999/")]}, "unreadable url"),
    ({"hooks": [dict(HOOK, url="https://h/" + "a" * 2050)]}, "url"),
    ({"hooks": [dict(HOOK, headers={"Bad Name": "x"})]}, "invalid header name"),
    ({"hooks": [dict(HOOK, headers={"X-A": "a\r\nb"})]}, "invalid value"),
    ({"hooks": [dict(HOOK, headers={"X-A": 1})]}, "invalid value"),
    ({"hooks": [dict(HOOK, secret_headers={"authorization": "x"}, headers={"Authorization": "y"})]},
     "both plain and secret"),
    ({"hooks": [dict(HOOK, secret_headers={"Authorization": rp.MASK})]}, "no stored value"),
    ({"hooks": [dict(HOOK, body="x" * (16 * 1024 + 1))]}, "body"),
    ({"hooks": [dict(HOOK, timeout_s=0)]}, "timeout_s"),
    ({"hooks": [dict(HOOK, timeout_s=121)]}, "timeout_s"),
    ({"hooks": [dict(HOOK, retries=3)]}, "unknown field"),
    ({"network_data": {"default/web-1": "version: 2"}}, "network_data needs cloud_init rerun"),
    ({"cloud_init": "rerun", "network_data": {"default/web-1": ""}}, "network_data"),
    ({"cloud_init": "rerun", "network_data": {"default/web-1": "x" * (16 * 1024 + 1)}}, "network_data"),
    ({"cloud_init": "rerun", "network_data": {"default/zzz": "v"}}, "not a VM of this plan"),
    ({"cloud_init": "rerun", "network_data": ["v"]}, "network_data"),
    ({"resize": {"default/web-1": {}}}, "cpu and/or memory"),
    ({"resize": {"default/web-1": {"cpu": 0}}}, "cpu"),
    ({"resize": {"default/web-1": {"cpu": 257}}}, "cpu"),
    ({"resize": {"default/web-1": {"cpu": "2"}}}, "cpu"),
    ({"resize": {"default/web-1": {"memory": "4G"}}}, "memory"),
    ({"resize": {"default/web-1": {"memory": "0Gi"}}}, "memory"),
    ({"resize": {"default/web-1": {"memory": 4096}}}, "memory"),
    ({"resize": {"default/web-1": {"disk": "1Gi"}}}, "cpu and/or memory"),
    ({"resize": {"default/zzz": {"cpu": 1}}}, "not a VM of this plan"),
    ({"resize": [1]}, "resize"),
    ({"auto_protect": "yes"}, "auto_protect"),
    ({"auto_protect": True}, "needs a plan by label selector"),                 # liste de VMs : sans effet
    ({"accept_degraded_standby": "yes"}, "accept_degraded_standby"),
])
def test_recovery_refusals(options, message):
    with pytest.raises(rp.PlanError) as e:
        rp.normalize_plan(req(options=options))
    assert e.value.code == "bad-option" and message in str(e.value), str(e.value)


def test_hook_at_a_declared_wave_or_the_implicit_last_one():
    waves = [_wave(["default/db-1"]), _wave(["default/web-1"])]
    for at in ("wave:1", "wave:2", "wave:3", "start", "end"):
        rp.normalize_plan(req(options={"waves": waves, "hooks": [dict(HOOK, at=at)]}))
    with pytest.raises(rp.PlanError):
        rp.normalize_plan(req(options={"waves": waves, "hooks": [dict(HOOK, at="wave:4")]}))


def test_a_selector_plan_may_name_vms_that_do_not_exist_yet():
    opts = rp.normalize_plan(req(vms=None, selector="dr=gold", options={
        "waves": [_wave(["default/future"])], "resize": {"other/vm": {"cpu": 1}}, "auto_protect": True}))["options"]
    assert opts["waves"][0]["vms"] == ["default/future"] and opts["auto_protect"] is True


# ---------------------------------------------------------------------------
# Secrets des actions annexes
# ---------------------------------------------------------------------------

def test_mask_plan_hides_every_secret_value_and_keeps_the_rest():
    plan = {"id": "rp-x", "options": rp.normalize_plan(req(options={"hooks": [HOOK]}))["options"]}
    shown = rp.mask_plan(plan)
    assert shown["options"]["hooks"][0]["secret_headers"] == {"Authorization": rp.MASK}
    assert shown["options"]["hooks"][0]["headers"] == HOOK["headers"]
    assert plan["options"]["hooks"][0]["secret_headers"]["Authorization"] == "Bearer s3cr3t"     # copie
    assert "s3cr3t" not in repr(shown)
    assert rp.mask_plan({"id": "x"}) == {"id": "x"} and rp.mask_options({"paused": True}) == {"paused": True}


@pytest.mark.parametrize("new,want", [
    ({"Authorization": rp.MASK}, {"Authorization": "Bearer s3cr3t"}),          # masque : valeur rangée
    ({"Authorization": "Bearer new"}, {"Authorization": "Bearer new"}),        # nouvelle valeur
    ({"Authorization": rp.MASK, "X-Key": "k"}, {"Authorization": "Bearer s3cr3t", "X-Key": "k"}),
    ({}, {}),                                                                  # en-tête retiré
])
def test_keep_hook_secrets(new, want):
    got = rp.keep_hook_secrets([dict(HOOK, secret_headers=new)], [HOOK])
    assert got[0]["secret_headers"] == want


@pytest.mark.parametrize("hook,why", [
    (dict(HOOK, secret_headers={"X-Other": rp.MASK}), "X-Other has no stored value"),      # rien de rangé
    (dict(HOOK, name="other", secret_headers={"Authorization": rp.MASK}), "new or renamed"),  # par nom
])
def test_keep_hook_secrets_refuses_a_mask_without_stored_value(hook, why):
    with pytest.raises(rp.PlanError) as e:
        rp.keep_hook_secrets([hook], [HOOK])
    assert e.value.code == "secret-missing" and why in str(e.value)


def test_hook_secret_values():
    assert rp.hook_secret_values([HOOK, dict(HOOK, secret_headers={"A": rp.MASK, "B": ""})]) == ["Bearer s3cr3t"]


# ---------------------------------------------------------------------------
# VM cible : taille et données réseau
# ---------------------------------------------------------------------------

def _src_vm(hotplug=False, reserved=None, guest="3996Mi", limit="4Gi", requests=True, ci=True):
    ann = {}
    if hotplug:
        ann[rp.HOTPLUG_ANNOTATION] = "true"
    if reserved:
        ann[rp.RESERVED_MEMORY_ANNOTATION] = reserved
    cpu = {"cores": 1, "sockets": 4, "threads": 1, "maxSockets": 8} if hotplug else {"cores": 4, "sockets": 1,
                                                                                       "threads": 1}
    mem = {"guest": guest}
    if hotplug:
        mem["maxGuest"] = "8Gi"
    res = {"limits": {"cpu": "4", "memory": limit}}
    if requests:
        res["requests"] = {"cpu": "250m", "memory": "2730Mi"}
    vols = [{"name": "rootdisk", "persistentVolumeClaim": {"claimName": "web-1-root"}}]
    if ci:
        vols.append({"name": "cloudinitdisk", "cloudInitNoCloud": {
            "secretRef": {"name": "web-1-ci"}, "networkDataSecretRef": {"name": "web-1-ci"}}})
    return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
            "metadata": {"name": "web-1", "namespace": "default", "uid": "u1", "annotations": ann},
            "spec": {"runStrategy": "Always", "template": {"spec": {
                "domain": {"cpu": cpu, "memory": mem, "resources": res, "firmware": {"uuid": "fw-1"},
                           "devices": {"disks": [{"name": "rootdisk", "disk": {"bus": "virtio"}}]}},
                "volumes": vols}}}}


def _target(vm, options):
    res = {"resource_uid": "u1", "namespace": "default", "name": "web-1", "firmware_uuid": "fw-1"}
    out, _ = rp.target_vm(vm, res, {}, options, {}, "rp-0123456789ab", 1)
    return out["spec"]["template"]["spec"]


@pytest.mark.parametrize("vm,size,want_cpu,want_res,want_mem", [
    # écart limite - invité de la source (100 Mio, réserve historique de Harvester) gardé
    (_src_vm(), {"cpu": 2, "memory": "2Gi"}, {"cores": 2, "sockets": 1, "threads": 1},
     {"limits": {"cpu": "2", "memory": "2Gi"}}, {"guest": "1948Mi"}),
    # invité égal à la limite (réglage de surcoût posé, cas de harv1) : invité = mémoire
    (_src_vm(guest="4Gi"), {"memory": "2Gi"}, {"cores": 4, "sockets": 1, "threads": 1},
     {"limits": {"cpu": "4", "memory": "2Gi"}, "requests": {"cpu": "250m"}}, {"guest": "2Gi"}),
    # annotation de réserve : elle décide
    (_src_vm(reserved="256Mi"), {"memory": "1Gi"}, None, {"limits": {"cpu": "4", "memory": "1Gi"},
                                                           "requests": {"cpu": "250m"}}, {"guest": "768Mi"}),
    # processeur seul : la mémoire ne bouge pas
    (_src_vm(), {"cpu": 1}, {"cores": 1, "sockets": 1, "threads": 1},
     {"limits": {"cpu": "1", "memory": "4Gi"}, "requests": {"memory": "2730Mi"}}, {"guest": "3996Mi"}),
    # branchement à chaud : sockets et invité, plafonds relevés au besoin
    (_src_vm(hotplug=True, guest="4Gi"), {"cpu": 12, "memory": "16Gi"},
     {"cores": 1, "sockets": 12, "threads": 1, "maxSockets": 12},
     {"limits": {"cpu": "12", "memory": "16Gi"}}, {"guest": "16Gi", "maxGuest": "16Gi"}),
    (_src_vm(requests=False), {"memory": "8Gi"}, None, {"limits": {"cpu": "4", "memory": "8Gi"}},
     {"guest": "8092Mi"}),
])
def test_resize_the_harvester_way(vm, size, want_cpu, want_res, want_mem):
    tspec = _target(vm, {"resize": {"default/web-1": size}})
    dom = tspec["domain"]
    if want_cpu:
        assert dom["cpu"] == want_cpu
    assert dom["resources"] == want_res
    assert dom["memory"] == want_mem


def test_resize_only_touches_its_vm():
    vm = _src_vm()
    assert _target(copy.deepcopy(vm), {"resize": {"default/web-2": {"cpu": 1}}})["domain"]["cpu"]["cores"] == 4


def test_network_data_replaces_the_secret_reference():
    tspec = _target(_src_vm(), {"cloud_init": "rerun", "network_data": {"default/web-1": "version: 2\n"}})
    ci = next(v for v in tspec["volumes"] if "cloudInitNoCloud" in v)["cloudInitNoCloud"]
    assert ci == {"secretRef": {"name": "web-1-ci"}, "networkData": "version: 2\n"}
    assert "uuid" not in (tspec["domain"].get("firmware") or {})


def test_network_data_on_a_config_drive_and_without_cloud_init():
    vm = _src_vm()
    vol = vm["spec"]["template"]["spec"]["volumes"][1]
    vol["cloudInitConfigDrive"] = {"userData": "#cloud-config\n", "networkDataBase64": "eA=="}
    del vol["cloudInitNoCloud"]
    ci = next(v for v in _target(vm, {"cloud_init": "rerun", "network_data": {"default/web-1": "n"}})["volumes"]
              if "cloudInitConfigDrive" in v)["cloudInitConfigDrive"]
    assert ci == {"userData": "#cloud-config\n", "networkData": "n"}
    tspec = _target(_src_vm(ci=False), {"cloud_init": "rerun", "network_data": {"default/web-1": "n"}})
    assert {"name": "cloudinitdisk", "cloudInitNoCloud": {"networkData": "n"}} in tspec["volumes"]
    assert {"name": "cloudinitdisk", "disk": {"bus": "virtio"}} in tspec["domain"]["devices"]["disks"]


# ---------------------------------------------------------------------------
# Réglages : états, carnet
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state,recovery,options,mappings,refused", [
    ("protected", True, {}, {}, False),
    ("failed_over", True, {"start": "stopped"}, {"networks": {}}, False),
    ("failing_over", True, {}, {}, True),
    ("failing_over", False, {"mac": "new"}, {}, True),
    ("failing_over", False, {}, {"drill_networks": {}}, True),
    ("failing_over", False, {}, {}, False),
    ("draft", False, {}, {"disk_classes": {}, "storage_classes": {}}, False),
    ("protected", False, {}, {"disk_classes": {}}, True),
    ("measuring", False, {}, {"namespaces": {}}, True),
    ("protected", False, {}, {"networks": {}, "drill_networks": {}}, False),
    ("error", False, {}, {"target_names": {}}, True),
])
def test_settings_refusal(state, recovery, options, mappings, refused):
    assert bool(rp.settings_refusal(state, recovery, options, mappings)) is refused


@pytest.mark.parametrize("state,stale,need", [
    ("protected", ["resize"], True),
    ("protected", [], False),
    ("draft", ["mac"], False),
    ("failed_over", ["resize"], False),
])
def test_protect_needed(state, stale, need):
    assert rp.protect_needed(state, stale) is need


# ---------------------------------------------------------------------------
# Vagues d'une bascule
# ---------------------------------------------------------------------------

def test_failover_waves_order_numbering_and_implicit_last_wave():
    opts = rp.normalize_plan(req(options={"waves": [
        {"name": "db", "vms": ["default/db-1"], "gate": {"kind": "agent", "seconds": 60}},
        {"name": "front", "vms": ["default/web-1"]}]}))["options"]
    waves = rp.failover_waves(opts, ["default/web-2", "default/web-1", "default/db-1"])
    assert [(w["index"], w["name"], w["vms"], w["gate"]["kind"], w.get("implicit", False)) for w in waves] == [
        (1, "db", ["default/db-1"], "agent", False), (2, "front", ["default/web-1"], "running", False),
        (3, "", ["default/web-2"], "running", True)]
    # bascule d'une partie du plan : la vague 1 reste numérotée 1, vide
    part = rp.failover_waves(opts, ["default/web-1"])
    assert [(w["index"], w["vms"]) for w in part] == [(1, []), (2, ["default/web-1"])]
    # sans vague : une seule, implicite
    assert rp.failover_waves({}, ["default/a", "default/b"]) == [
        {"index": 1, "name": "", "vms": ["default/a", "default/b"], "gate": {"kind": "running", "seconds": 0},
         "implicit": True}]


def test_hooks_at_and_summary():
    hooks = [dict(HOOK, name="a", at="start"), dict(HOOK, name="b", at="wave:1"), dict(HOOK, name="c", at="start")]
    opts = {"hooks": hooks}
    assert [h["name"] for h in rp.hooks_at(opts, "start")] == ["a", "c"]
    assert rp.hooks_at(opts, "end") == []
    assert rp.hooks_summary(opts)[1] == {"name": "b", "at": "wave:1", "method": "POST", "url": HOOK["url"]}
    assert "secret_headers" not in rp.hooks_summary(opts)[0]
