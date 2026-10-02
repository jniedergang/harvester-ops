"""v1.76.0 : les commandes de vagues de harvester-forklift sur un cluster en
mémoire et un vCenter factice : composer (refus d'une VM bloquée, d'un réseau
ou d'un datastore non mappé, d'une VM déjà prise), lancer, basculer, suivre,
revenir à la source (rejouable), clore (seuls les instantanés de Forklift),
supprimer, importeur CDI aller-retour, intervalle des copies.

Objets de départ : ceux relevés sur le banc (harvlab2 + vmwlab, 29/09/2026)."""

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import hv_forklift as hf  # noqa: E402
import vsphere_api as vs  # noqa: E402

_spec = importlib.util.spec_from_file_location("hfk_b2", ROOT / "bin" / "harvester-forklift.py")
hfk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hfk)

PASSWORD = "Very-S3cret!pw"
KIND = {"NetworkMap": hf.K_NETWORKMAP, "StorageMap": hf.K_STORAGEMAP, "Plan": hf.K_PLAN,
        "Migration": hf.K_MIGRATION}


def load(name):
    return json.loads((FIX / name).read_text())


def merge(dst, patch):
    """Patch de fusion (RFC 7386) : None retire la clé."""
    for k, v in patch.items():
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


class FakeKube:
    """Forklift installé, un fournisseur vmwlab et son secret, le namespace
    cible mig-b2. `plan_status` : statut posé par « Forklift » sur un plan
    appliqué ; `vmi_gets` : lectures de la VMI avant qu'elle disparaisse
    après l'arrêt de la VM."""

    def __init__(self):
        self.objs, self.calls, self.run_args = {}, [], []
        self.plan_status = {"observedGeneration": 1,
                            "conditions": [{"type": "Ready", "status": "True", "category": "Required"}]}
        self.vmi_gets = 2
        self.vmi_never_goes = False
        for n in hf.CERT_MANAGER[1]:
            self.dep(hf.CERT_MANAGER[0], n)
        for n in (hf.OPERATOR_DEPLOY,) + hf.COMPONENTS:
            self.dep(hf.NS, n)
        self.put(hf.K_ADDON, hf.NS, hf.ADDON[1], {"metadata": {"name": hf.ADDON[1], "namespace": hf.NS,
                                                               "labels": {hf.L_MANAGED: "true"}},
                                                  "spec": {"enabled": True},
                                                  "status": {"status": "AddonDeploySuccessful"}})
        self.put(hf.K_CONTROLLER, hf.NS, hf.CONTROLLER_NAME, load("forklift_b2_controller_176.json"))
        self.put("serviceaccounts", hf.NS, hf.INVENTORY_SA, {"metadata": {"name": hf.INVENTORY_SA}})
        self.put("namespaces", None, "mig-b2", {"metadata": {"name": "mig-b2"}})
        self.put(hf.K_PROVIDER, "default", "vmwlab", {
            "metadata": {"name": "vmwlab", "namespace": "default", "uid": "u-123", "labels": {hf.L_MANAGED: "true"}},
            "spec": {"type": "vsphere", "url": "https://172.16.2.81/sdk",
                     "secret": {"name": "vmwlab-vsphere", "namespace": "default"}}})
        import base64
        b = lambda v: base64.b64encode(v.encode()).decode()  # noqa: E731
        self.put("secrets", "default", "vmwlab-vsphere", {
            "metadata": {"name": "vmwlab-vsphere", "namespace": "default"},
            "data": {"user": b("administrator@vsphere.local"), "password": b(PASSWORD),
                     "url": b("https://172.16.2.81/sdk"), "insecureSkipVerify": b("true")}})

    def put(self, kind, ns, name, obj):
        self.objs[(kind, ns, name)] = obj

    def dep(self, ns, name, ready=True):
        self.put(hf.K_DEPLOY, ns, name, {"metadata": {"name": name, "namespace": ns, "generation": 1},
                                         "spec": {"replicas": 1},
                                         "status": {"observedGeneration": 1, "updatedReplicas": 1,
                                                    "availableReplicas": 1 if ready else 0}})

    def get(self, kind, ns, name):
        o = self.objs.get((kind, ns, name))
        if kind == hfk.K_VMI and o is not None:
            vm = self.objs.get((hfk.K_VM, ns, name)) or {}
            if (vm.get("spec") or {}).get("runStrategy") == "Halted" and not self.vmi_never_goes:
                self.vmi_gets -= 1
                if self.vmi_gets <= 0:
                    self.objs.pop((kind, ns, name))
                    return None
        return copy.deepcopy(o)

    def list(self, kind, ns=None, selector=None):
        out = []
        for (k, n, _), o in self.objs.items():
            if k != kind or (ns is not None and n != ns):
                continue
            if selector and not self._matches(o, selector):
                continue
            out.append(copy.deepcopy(o))
        return out

    @staticmethod
    def _matches(obj, selector):
        labels = ((obj.get("metadata") or {}).get("labels")) or {}
        for pair in selector.split(","):
            k, _, v = pair.partition("=")
            if labels.get(k) != v:
                return False
        return True

    def create(self, obj):
        o = copy.deepcopy(obj)
        o.setdefault("status", {})
        if obj["kind"] == "Migration":
            o["status"] = {"started": "2026-09-29T20:00:00Z"}
        self.put(KIND[obj["kind"]], o["metadata"]["namespace"], o["metadata"]["name"], o)
        self.calls.append(("create", obj["kind"], obj["metadata"]["name"]))
        return o

    def apply(self, docs, field_manager="harvester-ops", timeout=None):
        for d in docs:
            o = copy.deepcopy(d)
            if d["kind"] == "Plan":
                o["metadata"]["generation"] = 1
                o["status"] = copy.deepcopy(self.plan_status)
            self.put(KIND[d["kind"]], d["metadata"]["namespace"], d["metadata"]["name"], o)
            self.calls.append(("apply", d["kind"], d["metadata"]["name"]))
        return ""

    def patch(self, kind, ns, name, patch):
        self.calls.append(("patch", kind, name, json.dumps(patch, sort_keys=True)))
        o = merge(self.objs[(kind, ns, name)], patch)
        md = o.get("metadata") or {}
        if md.get("deletionTimestamp") and not md.get("finalizers"):
            self.objs.pop((kind, ns, name))     # plus rien ne retient l'objet
        if kind == hf.K_CONTROLLER:
            # l'opérateur redéploie le contrôleur avec la nouvelle valeur
            d = self.objs[(hf.K_DEPLOY, hf.NS, "forklift-controller")]
            d["metadata"]["generation"] += 1
            d["status"]["observedGeneration"] = d["metadata"]["generation"]
            d["spec"]["template"] = {"spec": {"containers": [{"name": "main", "env": [
                {"name": "PRECOPY_INTERVAL", "value": str(patch["spec"]["controller_precopy_interval"])}]}]}}

    def delete(self, kind, ns, name, cascade=None):
        self.calls.append(("delete", kind, name, cascade))
        self.objs.pop((kind, ns, name), None)

    def run(self, *args, input=None, timeout=None):
        self.run_args.append(args)
        if args[:2] == ("create", "token"):
            return "tok-123\n"
        if args[0] == "patch" and "--type" in args and args[args.index("--type") + 1] == "strategic":
            kind, name, ns = args[1], args[2], args[args.index("-n") + 1]
            patch = json.loads(args[args.index("-p") + 1])
            d = self.objs[(kind, ns, name)]
            for pc in patch["spec"]["template"]["spec"]["containers"]:
                c = next(c for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == pc["name"])
                for pe in pc["env"]:
                    next(e for e in c["env"] if e["name"] == pe["name"])["value"] = pe["value"]
            merge(d.setdefault("metadata", {}), patch.get("metadata") or {})
            # le cdi-operator relance cdi-deployment avec la nouvelle image
            cdi = self.objs[(hf.K_DEPLOY, ns, hfk.CDI_DEPLOY)]
            cdi["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = pe["value"]
            return ""
        raise AssertionError(args)

    def port_forward(self, ns, target, port):
        test = self

        class PF:
            def __enter__(self):
                test.pf = (ns, target, port)
                return 40123

            def __exit__(self, *a):
                return False
        return PF()


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def ns(**kw):
    a = dict(cluster=None, kubeconfig="kc", timeout=300, spec=None)
    a.update(kw)
    return argparse.Namespace(**a)


def managed(obj, wave):
    o = copy.deepcopy(obj)
    md = o["metadata"]
    md["namespace"] = hf.NS
    md["labels"] = {hf.L_MANAGED: "true", hf.L_WAVE: wave}
    md.pop("annotations", None)
    if o["kind"] == "Plan":
        o["spec"]["provider"]["source"] = {"namespace": "default", "name": "vmwlab"}
    else:
        o["spec"]["plan"]["namespace"] = hf.NS
    return o


def real(kind_file, name):
    return next(o for o in load(kind_file)["items"] if o["metadata"]["name"] == name)


def with_wave(k, plan_name="vague-1", migs=("vague-1-m3", "vague-1-m4"), plan_conditions=None):
    plan = managed(real("forklift_b2_plans_176.json", plan_name), plan_name)
    if plan_conditions is not None:
        plan["status"]["conditions"] = plan_conditions
        plan["status"]["migration"].pop("vms", None)
        plan["status"]["migration"].pop("completed", None)
    k.put(hf.K_PLAN, hf.NS, plan_name, plan)
    for m in migs:
        k.put(hf.K_MIGRATION, hf.NS, m, managed(real("forklift_b2_migrations_176.json", m), plan_name))
    return plan


def running_copy(k, name="vague-1-m4"):
    """La Migration m4 remise en copie : ni condition de fin ni bascule commencée."""
    m = k.objs[(hf.K_MIGRATION, hf.NS, name)]
    m["status"]["conditions"] = [c for c in m["status"]["conditions"] if c["type"] == "Ready"]
    m["spec"].pop("cutover", None)
    for vm in m["status"].get("vms") or []:
        vm.pop("completed", None)
        vm["conditions"] = []
        for s in vm.get("pipeline") or []:
            if s["name"] != "Initialize":
                s.pop("started", None)
                s.pop("completed", None)
                s["phase"] = "Pending"
    return m


def failed_before_cutover(k, name="vague-1-m4"):
    """La Migration m4 en échec pendant DiskTransfer (VDDK), avant toute
    bascule : ni `spec.cutover` ni l'étape Cutover n'ont jamais commencé."""
    m = running_copy(k, name)
    m["status"]["conditions"] = [c for c in m["status"]["conditions"] if c["type"] == "Ready"] + [
        {"type": "Failed", "status": "True", "category": "Advisory", "message": "The migration has FAILED."}]
    for vm in m["status"].get("vms") or []:
        vm["error"] = {"reasons": ["Unable to connect to vddk data source"]}
        for s in vm.get("pipeline") or []:
            if s["name"] == "DiskTransfer":
                s["phase"] = "Running"
                s["error"] = {"reasons": ["Unable to connect to vddk data source"]}
    return m


def harvester_vm(k, name="vmwlab-src-1", strategy="Always", labels=None):
    """La VM Harvester créée par Forklift. `labels` : les étiquettes réelles
    (`vmID`, `plan`) pour un nom que target_vm_name ne devine pas."""
    md = {"name": name, "namespace": "mig-b2"}
    if labels:
        md["labels"] = labels
    k.put(hfk.K_VM, "mig-b2", name, {"metadata": md, "spec": {"runStrategy": strategy}})
    k.put(hfk.K_VMI, "mig-b2", name, {"metadata": {"name": name, "namespace": "mig-b2"}})


# --- inventaire et demande de vague -------------------------------------------

def inventory_items(tools=True):
    items = load("forklift_inventory_vms_175.json")
    for v in items:
        if tools:
            v["guestNameFromVmwareTools"] = v.get("guestName") or "Linux"
    return items


def fetcher(items, seen=None):
    def fetch(url, token):
        if seen is not None:
            seen.update(url=url, token=token)
        return copy.deepcopy(items)
    return fetch


WAVE = {"name": "vague-3", "target_namespace": "mig-b2", "provider": {"namespace": "default", "name": "vmwlab"},
        "vms": ["vm-16"], "networks": [{"source": "network-13", "destination": "pod"}],
        "storages": [{"source": "datastore-12", "storage_class": "harvester-longhorn"}]}


def apply_wave(k, spec, items, tmp_path, seen=None):
    f = tmp_path / "wave.json"
    f.write_text(json.dumps(spec))
    c = Clock()
    return hfk.cmd_wave_apply(ns(spec=str(f)), kube=k, fetch=fetcher(items, seen), sleep=c.sleep, now=c.now)


def test_wave_apply_reads_the_inventory_then_applies_maps_and_plan(tmp_path, capsys):
    k, seen = FakeKube(), {}
    assert apply_wave(k, WAVE, inventory_items(), tmp_path, seen) == hfk.EXIT_OK
    assert seen["url"] == "https://127.0.0.1:40123/providers/vsphere/u-123/vms?detail=4"
    assert [c for c in k.calls if c[0] == "apply"] == [("apply", "NetworkMap", "vague-3-net"),
                                                       ("apply", "StorageMap", "vague-3-sto"),
                                                       ("apply", "Plan", "vague-3")]
    plan = k.objs[(hf.K_PLAN, hf.NS, "vague-3")]
    assert plan["spec"]["warm"] is True and plan["spec"]["targetNamespace"] == "mig-b2"
    assert plan["metadata"]["labels"] == {hf.L_MANAGED: "true", hf.L_WAVE: "vague-3"}
    assert "STEP_EVENT|wave|done|plan ready" in capsys.readouterr().err


def test_wave_apply_refuses_a_vm_without_vmware_tools(tmp_path, capsys):
    k = FakeKube()
    assert apply_wave(k, WAVE, inventory_items(tools=False), tmp_path) == hfk.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "vm-16 (vmwlab-src-1): VMware Tools are not running" in err
    assert not [c for c in k.calls if c[0] == "apply"]


def test_wave_apply_refuses_a_vm_without_cbt(tmp_path, capsys):
    items = inventory_items()
    items[0]["changeTrackingEnabled"] = False
    assert apply_wave(FakeKube(), WAVE, items, tmp_path) == hfk.EXIT_REFUSED
    assert "Changed Block Tracking is off" in capsys.readouterr().err


def test_wave_apply_refuses_an_unmapped_network_and_datastore(tmp_path, capsys):
    k = FakeKube()
    spec = dict(WAVE, networks=[{"source": "network-99", "destination": "pod"}],
                storages=[{"source": "datastore-99", "storage_class": "harvester-longhorn"}])
    assert apply_wave(k, spec, inventory_items(), tmp_path) == hfk.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "network network-13 is not mapped" in err and "datastore datastore-12 is not mapped" in err
    assert not [c for c in k.calls if c[0] == "apply"]


def test_wave_apply_refuses_a_vm_missing_from_the_inventory(tmp_path, capsys):
    assert apply_wave(FakeKube(), dict(WAVE, vms=["vm-404"]), inventory_items(), tmp_path) == hfk.EXIT_REFUSED
    assert "vm-404: not in the inventory" in capsys.readouterr().err


def test_wave_apply_refuses_a_vm_taken_by_another_open_wave(tmp_path, capsys):
    k = FakeKube()
    with_wave(k)                       # vague-1 porte vm-16, non close
    assert apply_wave(k, WAVE, inventory_items(), tmp_path) == hfk.EXIT_REFUSED
    assert "vm-16 is already in wave vague-1" in capsys.readouterr().err


def test_wave_apply_refuses_to_change_a_wave_with_a_migration_running(tmp_path, capsys):
    k = FakeKube()
    with_wave(k)
    running_copy(k)
    spec = dict(WAVE, name="vague-1")
    assert apply_wave(k, spec, inventory_items(), tmp_path) == hfk.EXIT_REFUSED
    assert "has a migration running" in capsys.readouterr().err


def test_wave_apply_says_forklift_s_critical_condition_as_is(tmp_path, capsys):
    k = FakeKube()
    k.plan_status = {"observedGeneration": 1, "conditions": [
        {"type": "VMStorageNotMapped", "status": "True", "category": "Critical",
         "message": "VM has unmapped storage."}]}
    assert apply_wave(k, WAVE, inventory_items(), tmp_path) == hfk.EXIT_FAIL
    assert "STEP_EVENT|wave|error|VM has unmapped storage." in capsys.readouterr().err


def test_wave_apply_refuses_a_missing_target_namespace(tmp_path, capsys):
    assert apply_wave(FakeKube(), dict(WAVE, target_namespace="nowhere"), inventory_items(), tmp_path) \
        == hfk.EXIT_REFUSED
    assert "target namespace nowhere does not exist" in capsys.readouterr().err


def test_wave_apply_refuses_a_plan_made_by_another_tool(tmp_path):
    k = FakeKube()
    k.put(hf.K_PLAN, hf.NS, "vague-3", {"metadata": {"name": "vague-3", "namespace": hf.NS}, "spec": {}})
    with pytest.raises(ValueError, match="not made by harvester-ops"):
        apply_wave(k, WAVE, inventory_items(), tmp_path)


# --- lancer, basculer, suivre ---------------------------------------------------

def test_wave_start_creates_the_migration_after_the_plan_history(capsys):
    k = FakeKube()
    with_wave(k, migs=("vague-1-m3",), plan_conditions=[{"type": "Ready", "status": "True"}])
    c = Clock()
    assert hfk.cmd_wave_start(ns(wave="vague-1"), kube=k, sleep=c.sleep, now=c.now) == hfk.EXIT_OK
    assert ("create", "Migration", "vague-1-m5") in k.calls          # m1..m4 dans l'historique
    m = k.objs[(hf.K_MIGRATION, hf.NS, "vague-1-m5")]
    assert m["spec"]["plan"] == {"namespace": hf.NS, "name": "vague-1"}
    assert m["metadata"]["labels"][hf.L_WAVE] == "vague-1"


def test_wave_start_refuses_while_a_migration_is_running(capsys):
    k = FakeKube()
    with_wave(k)
    running_copy(k)
    assert hfk.cmd_wave_start(ns(wave="vague-1"), kube=k) == hfk.EXIT_REFUSED
    assert "migration vague-1-m4 of wave vague-1 is still running" in capsys.readouterr().err
    assert not [c for c in k.calls if c[0] == "create"]


def test_wave_start_refuses_a_succeeded_wave(capsys):
    k = FakeKube()
    with_wave(k)
    assert hfk.cmd_wave_start(ns(wave="vague-1"), kube=k) == hfk.EXIT_REFUSED
    assert "already succeeded" in capsys.readouterr().err


def test_wave_cutover_sets_the_time_on_the_running_migration(capsys):
    k = FakeKube()
    with_wave(k)
    running_copy(k)
    assert hfk.cmd_wave_cutover(ns(wave="vague-1", at="2099-01-01T10:00:00+02:00"), kube=k) == hfk.EXIT_OK
    assert k.objs[(hf.K_MIGRATION, hf.NS, "vague-1-m4")]["spec"]["cutover"] == "2099-01-01T08:00:00Z"
    assert json.loads(capsys.readouterr().out) == {"migration": "vague-1-m4", "cutover": "2099-01-01T08:00:00Z"}


def test_wave_cutover_defaults_to_now_and_refuses_without_a_running_migration(capsys):
    k = FakeKube()
    with_wave(k)
    assert hfk.cmd_wave_cutover(ns(wave="vague-1", at=None), kube=k) == hfk.EXIT_REFUSED
    running_copy(k)
    assert hfk.cmd_wave_cutover(ns(wave="vague-1", at=None), kube=k) == hfk.EXIT_OK
    assert k.objs[(hf.K_MIGRATION, hf.NS, "vague-1-m4")]["spec"]["cutover"].endswith("Z")
    assert "(now)" in capsys.readouterr().err


def test_wave_status_and_waves_print_the_wave_state(capsys):
    k = FakeKube()
    with_wave(k)
    with_wave(k, "vague-2", ("vague-2-m1",))
    assert hfk.cmd_wave_status(ns(wave="vague-1"), kube=k) == hfk.EXIT_OK
    st = json.loads(capsys.readouterr().out)
    assert st["name"] == "vague-1" and st["state"] == "succeeded" and st["vms"][0]["id"] == "vm-16"
    assert hfk.cmd_waves(ns(), kube=k) == hfk.EXIT_OK
    assert [w["name"] for w in json.loads(capsys.readouterr().out)] == ["vague-1", "vague-2"]


def test_an_unknown_wave_is_a_refusal(monkeypatch, capsys):
    k = FakeKube()
    monkeypatch.setattr(hfk, "kube_from", lambda a: k)
    assert hfk.main(["wave-status", "--kubeconfig", "kc", "--wave", "nope"]) == hfk.EXIT_REFUSED
    assert "STEP_EVENT|wave-status|error|no wave nope" in capsys.readouterr().err


# --- retour à la source ------------------------------------------------------------

class FakeVSphere:
    def __init__(self, creds, on=()):
        self.creds, self.on, self.powered, self.closed = creds, set(on), [], False
        self.checked = []
        self.question = {}         # vm -> question VMware en attente

    def power_state(self, vm):
        self.checked.append(vm)
        return "POWERED_ON" if vm in self.on else "POWERED_OFF"

    def power_on(self, vm):
        if vm in self.on:
            return False
        if vm in self.question:
            raise hfk.vs.VSphereQuestion(f"power on {vm}: VMware waits for an answer", self.question[vm])
        self.on.add(vm)
        self.powered.append(vm)
        return True

    def close(self):
        self.closed = True


def rollback(k, box, on=(), **kw):
    def factory(creds):
        box["client"] = FakeVSphere(creds, on)
        box["client"].question.update(kw.get("question") or {})
        return box["client"]
    c = Clock()
    return hfk.cmd_wave_rollback(ns(wave="vague-1", vm=kw.get("vm", [])), kube=k, vsphere=factory,
                                 sleep=c.sleep, now=c.now)


def test_wave_rollback_halts_the_harvester_vm_before_powering_the_source_on(capsys):
    k, box = FakeKube(), {}
    with_wave(k)
    harvester_vm(k)
    assert rollback(k, box) == hfk.EXIT_OK
    assert k.objs[(hfk.K_VM, "mig-b2", "vmwlab-src-1")]["spec"]["runStrategy"] == "Halted"
    assert (hfk.K_VMI, "mig-b2", "vmwlab-src-1") not in k.objs
    assert box["client"].powered == ["vm-16"] and box["client"].closed
    assert box["client"].creds["user"] == "administrator@vsphere.local" and box["client"].creds["insecure"] is True
    plan = k.objs[(hf.K_PLAN, hf.NS, "vague-1")]
    assert plan["metadata"]["annotations"][hf.A_ROLLED_BACK] == "vm-16"
    out, err = capsys.readouterr()
    assert "STEP_EVENT|rollback|done|vm-16: source powered on" in err
    assert PASSWORD not in out + err and "administrator" not in out + err
    assert json.loads(out)["rolled_back"] == ["vm-16"]
    assert hf.wave_state(plan, [])["state"] == "rolled-back"


def test_wave_rollback_is_idempotent(capsys):
    k, box = FakeKube(), {}
    with_wave(k)
    harvester_vm(k)
    assert rollback(k, box) == hfk.EXIT_OK
    k.calls.clear()
    box.clear()
    assert rollback(k, box) == hfk.EXIT_OK
    assert not [c for c in k.calls if c[0] == "patch"] and "client" not in box
    assert "vm-16: already rolled back" in capsys.readouterr().err


def test_wave_rollback_leaves_a_source_already_on_alone(capsys):
    k, box = FakeKube(), {}
    with_wave(k)
    harvester_vm(k)
    assert rollback(k, box, on=("vm-16",)) == hfk.EXIT_OK
    assert box["client"].powered == []
    assert "vm-16: source already on, left as is" in capsys.readouterr().err
    assert k.objs[(hf.K_PLAN, hf.NS, "vague-1")]["metadata"]["annotations"][hf.A_ROLLED_BACK] == "vm-16"


def test_wave_rollback_never_powers_the_source_on_while_the_harvester_vm_runs(capsys):
    k, box = FakeKube(), {}
    with_wave(k)
    harvester_vm(k)
    k.vmi_never_goes = True
    assert rollback(k, box) == hfk.EXIT_FAIL
    assert box["client"].powered == []
    assert hf.A_ROLLED_BACK not in (k.objs[(hf.K_PLAN, hf.NS, "vague-1")]["metadata"].get("annotations") or {})
    assert "still running: source left off" in capsys.readouterr().err


def test_wave_rollback_refuses_a_wave_still_copying(capsys):
    k, box = FakeKube(), {}
    with_wave(k, plan_conditions=[{"type": "Ready", "status": "True"}])
    running_copy(k)
    assert rollback(k, box) == hfk.EXIT_REFUSED
    assert "client" not in box


def test_wave_rollback_refuses_a_vm_that_never_reached_switchover(capsys):
    """Le bug corrigé : une vague en échec pendant DiskTransfer (VDDK), avant
    toute bascule, ne doit jamais se laisser marquer revenue à la source :
    plus rien ne pourrait alors relancer la vague."""
    k, box = FakeKube(), {}
    with_wave(k, plan_conditions=[{"type": "Ready", "status": "True"}])
    failed_before_cutover(k)
    assert rollback(k, box) == hfk.EXIT_REFUSED
    assert "client" not in box   # jamais de session vCenter ouverte
    err = capsys.readouterr().err
    assert "no switchover has started" in err and "vm-16" in err
    plan = k.objs[(hf.K_PLAN, hf.NS, "vague-1")]
    assert hf.A_ROLLED_BACK not in (plan["metadata"].get("annotations") or {})
    assert hf.wave_state(plan, [k.objs[(hf.K_MIGRATION, hf.NS, "vague-1-m4")]])["state"] == "failed"


def test_wave_rollback_refuses_a_vm_outside_the_wave():
    k = FakeKube()
    with_wave(k)
    with pytest.raises(ValueError, match="not in the wave: vm-99"):
        rollback(k, {}, vm=["vm-99"])


def test_a_vcenter_error_is_a_step_without_the_password(monkeypatch, capsys):
    k = FakeKube()
    with_wave(k)
    harvester_vm(k)

    class Down(FakeVSphere):
        def power_on(self, vm):
            raise vs.VSphereError("vCenter 172.16.2.81: power on vm-16: unreachable")
    monkeypatch.setattr(hfk, "kube_from", lambda a: k)
    monkeypatch.setattr(hfk, "make_vsphere", lambda creds: Down(creds))
    monkeypatch.setattr(hfk.time, "sleep", lambda s: None)
    assert hfk.main(["wave-rollback", "--kubeconfig", "kc", "--wave", "vague-1"]) == hfk.EXIT_FAIL
    out, err = capsys.readouterr()
    assert "STEP_EVENT|rollback|error|vm-16: vCenter 172.16.2.81: power on vm-16: unreachable" in err
    assert PASSWORD not in out + err


def test_wave_rollback_checks_vcenter_before_halting_any_harvester_vm(monkeypatch, capsys):
    """VSphere est paresseux : la panne doit apparaître à la lecture d'avant,
    jamais après qu'une VM Harvester a déjà été arrêtée."""
    k = FakeKube()
    with_wave(k)
    harvester_vm(k)

    class Unreachable(FakeVSphere):
        def power_state(self, vm):
            raise vs.VSphereError("vCenter 172.16.2.81: power state of vm-16: unreachable (timed out)")
    monkeypatch.setattr(hfk, "kube_from", lambda a: k)
    monkeypatch.setattr(hfk, "make_vsphere", lambda creds: Unreachable(creds))
    monkeypatch.setattr(hfk.time, "sleep", lambda s: None)
    assert hfk.main(["wave-rollback", "--kubeconfig", "kc", "--wave", "vague-1"]) == hfk.EXIT_FAIL
    out, err = capsys.readouterr()
    assert "STEP_EVENT|rollback|error|vCenter check before stopping any Harvester VM" in err
    assert PASSWORD not in out + err and "administrator" not in out + err
    # rien n'a été arrêté côté Harvester ni marqué côté plan
    assert k.objs[(hfk.K_VM, "mig-b2", "vmwlab-src-1")]["spec"]["runStrategy"] == "Always"
    assert (hfk.K_VMI, "mig-b2", "vmwlab-src-1") in k.objs
    assert hf.A_ROLLED_BACK not in (k.objs[(hf.K_PLAN, hf.NS, "vague-1")]["metadata"].get("annotations") or {})


def test_wave_rollback_finds_the_harvester_vm_by_forklift_s_labels_when_the_name_differs(capsys):
    """Nom réel relevé sur le banc (mig-b2, 29/09/2026) : Forklift étiquette
    la VM qu'il crée avec vmID=<id de la source> et plan=<uid du Plan>, pas
    forcément le nom que le plan ou la migration laissent deviner."""
    k, box = FakeKube(), {}
    plan = with_wave(k)
    plan_uid = plan["metadata"]["uid"]
    harvester_vm(k, name="vague-1-vm16-renamed", labels={"vmID": "vm-16", "plan": plan_uid, "guestConverted": "true"})
    assert rollback(k, box) == hfk.EXIT_OK
    assert k.objs[(hfk.K_VM, "mig-b2", "vague-1-vm16-renamed")]["spec"]["runStrategy"] == "Halted"
    assert (hfk.K_VMI, "mig-b2", "vague-1-vm16-renamed") not in k.objs
    assert box["client"].powered == ["vm-16"]


def test_wave_rollback_refuses_to_power_on_a_source_whose_copy_cannot_be_found(monkeypatch, capsys):
    """La vague est réussie (VirtualMachineCreation terminée) mais aucune VM
    Harvester ne porte ce nom ni ces étiquettes : rallumer la source
    risquerait de laisser deux machines avec la même IP et la même MAC."""
    k = FakeKube()
    with_wave(k)                       # succeeded, sans harvester_vm : rien sur Harvester
    box = {}
    monkeypatch.setattr(hfk.time, "sleep", lambda s: None)
    assert rollback(k, box) == hfk.EXIT_FAIL
    err = capsys.readouterr().err
    assert "vm-16: no Harvester VM found" in err and "wave shows it was created" in err
    assert box["client"].powered == []
    assert hf.A_ROLLED_BACK not in (k.objs[(hf.K_PLAN, hf.NS, "vague-1")]["metadata"].get("annotations") or {})


def test_wave_rollback_still_powers_on_a_source_never_created_on_harvester(capsys):
    """Contre-épreuve : une vague en échec avant la création de la VM (étape
    VirtualMachineCreation jamais atteinte) n'a rien à arrêter, la source est
    donc rallumée comme avant ce correctif."""
    k, box = FakeKube(), {}
    # plan_conditions retire le statut figé du plan (celui du vrai relevé,
    # une bascule m4 déjà réussie) : le statut de la VM revient à celui de
    # sa seule Migration, m3, qui a échoué avant VirtualMachineCreation.
    with_wave(k, migs=("vague-1-m3",), plan_conditions=[{"type": "Ready", "status": "True"}])
    assert hf.wave_state(k.objs[(hf.K_PLAN, hf.NS, "vague-1")], [k.objs[(hf.K_MIGRATION, hf.NS, "vague-1-m3")]])[
        "state"] == "failed"
    assert rollback(k, box) == hfk.EXIT_OK
    assert box["client"].powered == ["vm-16"]
    assert "vm-16: no Harvester VM found: nothing to stop" in capsys.readouterr().err


# --- clôture -------------------------------------------------------------------------

class SnapVSphere(vs.VSphere):
    """Le vrai clear_forklift_snapshots sur un arbre en mémoire : racine de
    l'exploitant, deux instantanés de Forklift, une branche de l'exploitant
    sous le premier."""

    def __init__(self, creds):
        super().__init__(creds["url"], creds["user"], creds["password"], insecure=creds["insecure"])
        self.trees = {"vm-16": [{"id": "snapshot-900", "name": "before-migration", "created": "", "children": [
            {"id": "snapshot-1001", "name": vs.FORKLIFT_SNAPSHOT, "created": "", "children": [
                {"id": "snapshot-1002", "name": vs.FORKLIFT_SNAPSHOT, "created": "", "children": []},
                {"id": "snapshot-1500", "name": "debug-point", "created": "", "children": []}]}]}]}
        self.removed = []

    def snapshots(self, vm_id):
        return copy.deepcopy(self.trees.get(vm_id, []))

    def remove_snapshot(self, snap_id, consolidate=True):
        self.removed.append(snap_id)
        return f"task-{snap_id}"

    def wait_task(self, task_id, timeout=600, poll=2.0):
        return "success"

    def close(self):
        pass


def test_wave_close_archives_and_cleans_only_forklift_snapshots(capsys):
    k, box = FakeKube(), {}
    with_wave(k)

    def factory(creds):
        box["c"] = SnapVSphere(creds)
        return box["c"]
    assert hfk.cmd_wave_close(ns(wave="vague-1", clean_snapshots=True), kube=k, vsphere=factory) == hfk.EXIT_OK
    plan = k.objs[(hf.K_PLAN, hf.NS, "vague-1")]
    assert plan["spec"]["archived"] is True and plan["metadata"]["annotations"][hf.A_CLOSED]
    assert box["c"].removed == ["snapshot-1002", "snapshot-1001"]
    out, err = capsys.readouterr()
    assert json.loads(out)["snapshots_removed"] == {"vm-16": ["snapshot-1002", "snapshot-1001"]}
    assert "STEP_EVENT|snapshots|done|vm-16: 2 Forklift snapshots removed" in err
    assert hf.wave_state(plan, [])["state"] == "closed"


def test_wave_close_without_cleaning_never_reaches_vcenter(capsys):
    k = FakeKube()
    with_wave(k)

    def factory(creds):
        raise AssertionError("no vCenter call")
    assert hfk.cmd_wave_close(ns(wave="vague-1", clean_snapshots=False), kube=k, vsphere=factory) == hfk.EXIT_OK
    assert hfk.cmd_wave_close(ns(wave="vague-1", clean_snapshots=False), kube=k, vsphere=factory) == hfk.EXIT_OK
    assert "was already closed" in capsys.readouterr().err


def test_wave_close_accepts_being_asked_to_clean_snapshots_on_an_already_closed_wave(capsys):
    """L'onglet Vagues propose « Retirer les instantanés Forklift » sur une
    vague déjà close : reclore avec le nettoyage demandé doit fonctionner."""
    k, box = FakeKube(), {}
    with_wave(k)

    def factory(creds):
        box["c"] = SnapVSphere(creds)
        return box["c"]
    assert hfk.cmd_wave_close(ns(wave="vague-1", clean_snapshots=False), kube=k, vsphere=factory) == hfk.EXIT_OK
    assert "wave vague-1 closed" in capsys.readouterr().err
    assert hfk.cmd_wave_close(ns(wave="vague-1", clean_snapshots=True), kube=k, vsphere=factory) == hfk.EXIT_OK
    err = capsys.readouterr().err
    assert "was already closed" in err
    assert box["c"].removed == ["snapshot-1002", "snapshot-1001"]


def test_wave_close_refuses_while_a_migration_is_running(capsys):
    k = FakeKube()
    with_wave(k)
    running_copy(k)
    assert hfk.cmd_wave_close(ns(wave="vague-1", clean_snapshots=True), kube=k) == hfk.EXIT_REFUSED
    assert not [c for c in k.calls if c[0] == "patch"]


# --- suppression ------------------------------------------------------------------------

def test_wave_delete_removes_plan_migrations_and_maps_but_not_the_vms(capsys):
    k = FakeKube()
    plan = with_wave(k)
    harvester_vm(k)
    # une VM qui porterait la migration comme propriétaire : détachée, gardée
    mig_uid = k.objs[(hf.K_MIGRATION, hf.NS, "vague-1-m3")]["metadata"]["uid"]
    k.objs[(hfk.K_VM, "mig-b2", "vmwlab-src-1")]["metadata"]["ownerReferences"] = [
        {"kind": "Migration", "name": "vague-1-m3", "uid": mig_uid},
        {"kind": "Other", "name": "keep-me", "uid": "u-other"}]
    k.put("persistentvolumeclaims", "mig-b2", "disk-0", {"metadata": {
        "name": "disk-0", "ownerReferences": [{"kind": "Plan", "name": "vague-1", "uid": plan["metadata"]["uid"]}]}})
    for kind, n in ((hf.K_NETWORKMAP, "vague-1-net"), (hf.K_STORAGEMAP, "vague-1-sto")):
        k.put(kind, hf.NS, n, {"metadata": {"name": n, "labels": {hf.L_MANAGED: "true", hf.L_WAVE: "vague-1"}}})
    c = Clock()
    assert hfk.cmd_wave_delete(ns(wave="vague-1", timeout=120), kube=k, sleep=c.sleep, now=c.now) == hfk.EXIT_OK
    left = {(kind, n) for (kind, _, n) in k.objs if kind in KIND.values()}
    assert left == set()
    vm = k.objs[(hfk.K_VM, "mig-b2", "vmwlab-src-1")]
    assert vm["metadata"]["ownerReferences"] == [{"kind": "Other", "name": "keep-me", "uid": "u-other"}]
    assert "ownerReferences" not in k.objs[("persistentvolumeclaims", "mig-b2", "disk-0")]["metadata"]
    # jamais de suppression « orphan » : le webhook de Forklift la bloque
    deletes = [c for c in k.calls if c[0] == "delete"]
    assert ("delete", hf.K_PLAN, "vague-1", None) in deletes and all(c[3] is None for c in deletes)
    assert "2 object(s) of mig-b2 detached from the wave" in capsys.readouterr().err


def test_wave_delete_unblocks_a_deletion_stuck_on_the_orphan_finalizer(capsys):
    """Vu sur harvlab2 : plans et migrations supprimés en « orphan » avant
    1.84.0, restés en suppression avec leur finaliseur pour toujours."""
    k = FakeKube()
    with_wave(k)
    for key in [x for x in k.objs if x[0] in (hf.K_PLAN, hf.K_MIGRATION)]:
        md = k.objs[key]["metadata"]
        md["deletionTimestamp"], md["finalizers"] = "2026-10-01T21:11:37Z", ["orphan"]
    c = Clock()
    assert hfk.cmd_wave_delete(ns(wave="vague-1", timeout=120), kube=k, sleep=c.sleep, now=c.now) == hfk.EXIT_OK
    assert not [x for x in k.objs if x[0] in (hf.K_PLAN, hf.K_MIGRATION)]
    assert not [c for c in k.calls if c[0] == "delete" and c[1] in (hf.K_PLAN, hf.K_MIGRATION)]
    assert ("patch", hf.K_PLAN, "vague-1", json.dumps({"metadata": {"finalizers": None}})) in k.calls


def test_wave_delete_refuses_while_a_migration_is_running(capsys):
    k = FakeKube()
    with_wave(k)
    running_copy(k)
    assert hfk.cmd_wave_delete(ns(wave="vague-1", timeout=120), kube=k) == hfk.EXIT_REFUSED
    assert not [c for c in k.calls if c[0] == "delete"]


# --- importeur CDI -------------------------------------------------------------------------

def cdi_world(version="1.65.0"):
    k = FakeKube()
    op = load("forklift_b2_cdi_operator_176.json")
    for e in op["spec"]["template"]["spec"]["containers"][0]["env"]:
        if e["name"] in hf.CDI_IMAGE_ENVS:
            e["value"] = "registry.suse.com/suse/sles/16.0/cdi-importer:1.65.0"
    op["status"]["updatedReplicas"] = 1
    k.put(hf.K_DEPLOY, *hf.CDI_OPERATOR, op)
    k.put(hf.K_DEPLOY, hf.CDI_OPERATOR[0], hfk.CDI_DEPLOY, {
        "metadata": {"name": hfk.CDI_DEPLOY, "generation": 1}, "spec": {"replicas": 1, "template": {"spec": {
            "containers": [{"name": "cdi-controller", "env": [
                {"name": "IMPORTER_IMAGE", "value": "registry.suse.com/suse/sles/16.0/cdi-importer:1.65.0"}]}]}}},
        "status": {"observedGeneration": 1, "updatedReplicas": 1, "availableReplicas": 1}})
    k.put(hfk.K_CDI, None, "cdi", {"metadata": {"name": "cdi"}, "status": {"observedVersion": version}})
    return k


def cdi(k, **kw):
    a = dict(show=False, upstream=False, original=False, image=None, timeout=600)
    a.update(kw)
    c = Clock()
    return hfk.cmd_cdi_importer(ns(**a), kube=k, sleep=c.sleep, now=c.now)


def test_cdi_importer_upstream_then_original_round_trip(capsys):
    k = cdi_world()
    assert cdi(k, show=True) == hfk.EXIT_OK
    st = json.loads(capsys.readouterr().out)
    assert st["kind"] == "suse-no-vddk" and st["upstream_image"] == "quay.io/kubevirt/cdi-importer:v1.65.0"
    assert cdi(k, upstream=True) == hfk.EXIT_OK
    st = json.loads(capsys.readouterr().out)
    assert st["kind"] == "upstream" and st["image"] == "quay.io/kubevirt/cdi-importer:v1.65.0"
    assert st["original"] == "registry.suse.com/suse/sles/16.0/cdi-importer:1.65.0"
    envs = {e["name"]: e.get("value") for e in
            k.objs[(hf.K_DEPLOY, *hf.CDI_OPERATOR)]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert envs["IMPORTER_IMAGE"] == envs["OVIRT_POPULATOR_IMAGE"] == "quay.io/kubevirt/cdi-importer:v1.65.0"
    assert envs["CONTROLLER_IMAGE"].startswith("registry.suse.com/")           # le reste intact
    assert any(a[:1] == ("patch",) and "strategic" in a for a in k.run_args)
    assert cdi(k, original=True) == hfk.EXIT_OK
    st = json.loads(capsys.readouterr().out)
    assert st == dict(st, kind="suse-no-vddk", image="registry.suse.com/suse/sles/16.0/cdi-importer:1.65.0",
                      original="")


def test_cdi_importer_takes_a_mirror_and_a_v_prefixed_version(capsys):
    k = cdi_world("v1.65.0")
    assert cdi(k, upstream=True, image="172.16.1.11:3000/ju/cdi-importer:v1.65.0") == hfk.EXIT_OK
    st = json.loads(capsys.readouterr().out)
    assert st["image"] == "172.16.1.11:3000/ju/cdi-importer:v1.65.0" and st["kind"] == "other"
    assert st["upstream_image"] == "quay.io/kubevirt/cdi-importer:v1.65.0"


def test_cdi_importer_original_without_a_record_is_refused(capsys):
    k = cdi_world()
    assert cdi(k, original=True) == hfk.EXIT_REFUSED
    assert "no original importer image recorded" in capsys.readouterr().err


def test_cdi_importer_image_needs_upstream():
    with pytest.raises(ValueError, match="--image goes with --upstream"):
        cdi(cdi_world(), image="r.lan/x/cdi-importer:v1")


def test_cdi_importer_on_the_cli():
    ap, _ = hfk.build_parser()
    a = ap.parse_args(["cdi-importer", "--kubeconfig", "kc", "--upstream", "--image", "r.lan/x/cdi-importer:v1"])
    assert a.upstream and a.image == "r.lan/x/cdi-importer:v1"
    with pytest.raises(SystemExit):
        ap.parse_args(["cdi-importer", "--kubeconfig", "kc", "--upstream", "--original"])


# --- intervalle des copies --------------------------------------------------------------------

def test_precopy_interval_patches_the_controller_and_waits_for_its_restart(capsys):
    k = FakeKube()
    c = Clock()
    assert hfk.cmd_precopy_interval(ns(minutes="15", timeout=600), kube=k, sleep=c.sleep, now=c.now) == hfk.EXIT_OK
    assert k.objs[(hf.K_CONTROLLER, hf.NS, hf.CONTROLLER_NAME)]["spec"]["controller_precopy_interval"] == 15
    assert json.loads(capsys.readouterr().out) == {"minutes": 15}


def test_precopy_interval_already_set_does_nothing(capsys):
    k = FakeKube()                     # le contrôleur réel du banc : 5 min
    assert hfk.cmd_precopy_interval(ns(minutes="5"), kube=k) == hfk.EXIT_OK
    assert not [c for c in k.calls if c[0] == "patch"]


@pytest.mark.parametrize("minutes", ["4", "1441", "abc"])
def test_precopy_interval_bounds(monkeypatch, capsys, minutes):
    k = FakeKube()
    monkeypatch.setattr(hfk, "kube_from", lambda a: k)
    assert hfk.main(["precopy-interval", "--kubeconfig", "kc", minutes]) == hfk.EXIT_REFUSED
    assert "precopy interval: 5 to 1440 minutes" in capsys.readouterr().err
    assert not [c for c in k.calls if c[0] == "patch"]


def test_the_wave_commands_are_on_the_command_line():
    ap, _ = hfk.build_parser()
    for argv in (["wave-apply", "--kubeconfig", "kc", "--spec", "f"],
                 ["wave-start", "--kubeconfig", "kc", "--wave", "w"],
                 ["wave-cutover", "--kubeconfig", "kc", "--wave", "w", "--at", "2099-01-01T00:00:00Z"],
                 ["wave-status", "--kubeconfig", "kc", "--wave", "w"],
                 ["waves", "--kubeconfig", "kc"],
                 ["wave-rollback", "--kubeconfig", "kc", "--wave", "w", "--vm", "vm-16", "--vm", "vm-18"],
                 ["wave-close", "--kubeconfig", "kc", "--wave", "w", "--clean-snapshots"],
                 ["wave-delete", "--kubeconfig", "kc", "--wave", "w"]):
        a = ap.parse_args(argv)
        assert a.fn.__name__.startswith("cmd_wave")
    assert ap.parse_args(["wave-rollback", "--kubeconfig", "kc", "--wave", "w", "--vm", "vm-16",
                          "--vm", "vm-18"]).vm == ["vm-16", "vm-18"]


def test_no_secret_goes_on_the_command_line():
    """Aucune option ne prend un mot de passe : les identifiants du vCenter
    sont relus dans le Secret du fournisseur."""
    ap, sub = hfk.build_parser()
    for name, sp in sub.choices.items():
        if name.startswith("wave") or name in ("cdi-importer", "precopy-interval"):
            opts = {o for a in sp._actions for o in a.option_strings}
            assert not {o for o in opts if "pass" in o or "user" in o or "token" in o}, name


# --- conflit de MAC (v1.83.2, vu en réel sur harvlab2) ------------------------
SRC_VM = [{"id": "vm-16", "name": "vmwlab-src-1", "powerState": "poweredOn", "changeTrackingEnabled": True,
           "nics": [{"network": {"id": "network-13"}, "mac": "00:50:56:B7:AB:28", "order": 0}]}]


def dest_vm(k, name="vmwlab-src-1", namespace="mig-ui", mac="00:50:56:b7:ab:28"):
    k.put(hfk.K_VM, namespace, name, {"metadata": {"name": name, "namespace": namespace},
                                     "spec": {"template": {"spec": {"domain": {"devices": {"interfaces": [
                                         {"name": "nic-1", "macAddress": mac}]}}}}}})


def test_mac_conflicts_pure():
    rows = hf.inventory_rows("vms", SRC_VM)
    assert rows[0]["macs"] == ["00:50:56:b7:ab:28"]
    vms = [{"metadata": {"name": "old", "namespace": "mig-ui"},
            "spec": {"template": {"spec": {"domain": {"devices": {"interfaces": [{"macAddress": "00:50:56:B7:AB:28"}]}}}}}}]
    assert hf.mac_conflicts(rows, vms, ["vm-16"]) == [("vmwlab-src-1", "00:50:56:b7:ab:28", "mig-ui/old")]
    assert hf.mac_conflicts(rows, vms, ["vm-99"]) == []
    msg = hf.mac_refusal(hf.mac_conflicts(rows, vms, ["vm-16"]))
    assert "mig-ui/old" in msg and "delete" in msg


def test_cutover_refuses_a_mac_already_used_on_the_cluster(capsys):
    """La bascule arrête la source : avec un conflit, Forklift ne crée pas la
    VM d'arrivée (vu en réel) ; la console refuse avant."""
    k = FakeKube()
    with_wave(k)
    running_copy(k)
    dest_vm(k)
    assert hfk.cmd_wave_cutover(ns(wave="vague-1", at=None), kube=k, fetch=fetcher(SRC_VM)) == hfk.EXIT_REFUSED
    assert "mig-ui/vmwlab-src-1" in capsys.readouterr().err
    assert "cutover" not in (k.objs[(hf.K_MIGRATION, hf.NS, "vague-1-m4")]["spec"])


def test_cutover_passes_when_the_macs_are_free(capsys):
    k = FakeKube()
    with_wave(k)
    running_copy(k)
    dest_vm(k, mac="00:50:56:00:00:01")            # une autre MAC : pas de conflit
    assert hfk.cmd_wave_cutover(ns(wave="vague-1", at=None), kube=k, fetch=fetcher(SRC_VM)) == hfk.EXIT_OK


# --- question VMware à la mise sous tension (1.84.0) --------------------------------

SERIAL_Q = {"id": "3026", "text": 'The serial port output file "/vmfs/volumes/x/serial.log" already exists.',
            "choices": ["Append", "Replace", "Cancel"]}


def test_wave_rollback_reports_a_vmware_question_instead_of_a_running_source(capsys):
    k, box = FakeKube(), {}
    with_wave(k)
    harvester_vm(k)
    assert rollback(k, box, question={"vm-16": SERIAL_Q}) == hfk.EXIT_FAIL
    err = capsys.readouterr().err
    assert "left as is" not in err and "source powered on" not in err
    assert "STEP_EVENT|rollback|error|vm-16: source not running, VMware waits for an answer" in err
    assert "serial.log" in err and "(choices: Append, Replace, Cancel)" in err
    assert "Answer it in vCenter, then run the rollback again" in err
    # pas marquée revenue : la relancer après la réponse confirme
    assert hf.A_ROLLED_BACK not in (k.objs[(hf.K_PLAN, hf.NS, "vague-1")]["metadata"].get("annotations") or {})


def test_wave_rollback_rerun_after_the_answer_completes(capsys):
    k, box = FakeKube(), {}
    with_wave(k)
    harvester_vm(k)
    assert rollback(k, box, question={"vm-16": SERIAL_Q}) == hfk.EXIT_FAIL
    capsys.readouterr()
    # réponse donnée dans le vCenter : la VM tourne
    assert rollback(k, box, on=("vm-16",)) == hfk.EXIT_OK
    assert "vm-16: source already on, left as is" in capsys.readouterr().err
    assert k.objs[(hf.K_PLAN, hf.NS, "vague-1")]["metadata"]["annotations"][hf.A_ROLLED_BACK] == "vm-16"
