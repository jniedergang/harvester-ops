"""v1.45.0 : le déroulé des deux moteurs de transfert, sans cluster.

Deux clusters simulés en mémoire, avec ce que leurs contrôleurs font pour de
vrai (relevé sur harv1) : une VM s'arrête quand sa VMI disparaît, une
sauvegarde devient prête puis apparaît sur l'autre cluster qui partage la
cible, une restauration crée la VM et des volumes `restore-*`, une image
exportée d'un volume devient `Imported`, un DataVolume va CHERCHER son
disque par HTTP sur le guichet (requête réelle) puis possède son volume.

L'horloge est factice : un transfert de plusieurs heures dure une fraction
de seconde, et les délais sont vérifiés pour de bon.
"""

import copy
import gzip
import json
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import vm_transfer as vt  # noqa: E402
import vm_transfer_run as run  # noqa: E402
import vm_transfer_serve as vs  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "vm_transfer"

KINDS = {
    ("kubevirt.io", "VirtualMachine"): run.K_VM,
    ("harvesterhci.io", "VirtualMachineBackup"): run.K_BACKUP,
    ("harvesterhci.io", "VirtualMachineRestore"): run.K_RESTORE,
    ("harvesterhci.io", "VirtualMachineImage"): run.K_IMAGE,
    ("cdi.kubevirt.io", "DataVolume"): run.K_DV,
    ("", "Secret"): run.K_SECRET,
    ("", "Namespace"): run.K_NS,
    ("", "PersistentVolumeClaim"): run.K_PVC,
}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def merge(dst, patch):
    for k, v in patch.items():
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


class Store:
    """La cible de sauvegarde commune : ce qu'une sauvegarde y dépose."""

    def __init__(self):
        self.backups = {}      # (ns, nom) -> (prête à, objet)


class FakeCluster:
    def __init__(self, name, clock, store=None, auto_sync=True, fail=None, no_halt=False):
        self.name, self.clock, self.store = name, clock, store
        self.auto_sync = auto_sync
        self.no_halt = no_halt          # Harvester 1.8 : pas de haltAfterRestore
        self.resync_at = None
        self.fail = fail or {}
        self.objs = {}
        self.calls = []
        self.timers = []

    # -- outils de test ----------------------------------------------------
    def put(self, kind, obj):
        md = obj["metadata"]
        self.objs[(kind, md.get("namespace"), md["name"])] = copy.deepcopy(obj)

    def at(self, delay, fn):
        self.timers.append((self.clock.now() + delay, fn))

    def tick(self):
        now = self.clock.now()
        due = [t for t in self.timers if t[0] <= now]
        self.timers = [t for t in self.timers if t[0] > now]
        for _, fn in due:
            fn()
        if self.store is not None:
            # comme Harvester (relevé sur harvlab2) : une synchronisation
            # ramène d'abord les images manquantes, et saute la sauvegarde
            # tant que la classe de stockage de l'image n'existe pas ; il
            # faut un passage suivant pour la voir
            one_shot = self.resync_at is not None and now >= self.resync_at
            if one_shot:
                self.resync_at = None
            for (ns, name), (ready_at, obj, needs) in list(self.store.backups.items()):
                if (run.K_BACKUP, ns, name) in self.objs:
                    continue
                if not ((self.auto_sync and now >= ready_at + 20) or one_shot):
                    continue
                missing = [sc for sc in needs if ("storageclasses", None, sc) not in self.objs]
                if missing:
                    for sc in missing:
                        img = ("virtualmachineimages.harvesterhci.io", "default", f"img-{sc}")
                        if img not in self.objs:
                            self.objs[img] = {"metadata": {"name": f"img-{sc}", "namespace": "default"},
                                              "spec": {"sourceType": "restore"}}
                            self.at(30, lambda sc=sc: self.objs.__setitem__(
                                ("storageclasses", None, sc), {"metadata": {"name": sc}}))
                    continue
                self.objs[(run.K_BACKUP, ns, name)] = copy.deepcopy(obj)

    def labelled(self):
        return sorted(k for k, o in self.objs.items()
                      if vt.TRANSFER_LABEL in ((o.get("metadata") or {}).get("labels") or {}))

    # -- interface client --------------------------------------------------
    def get(self, kind, ns, name):
        self.tick()
        o = self.objs.get((kind, ns, name))
        return copy.deepcopy(o) if o is not None else None

    def list(self, kind, ns=None, selector=None):
        self.tick()
        return [copy.deepcopy(o) for (k, n, _), o in self.objs.items()
                if k == kind and (ns is None or n == ns)]

    def create(self, obj):
        group = obj["apiVersion"].split("/")[0] if "/" in obj["apiVersion"] else ""
        kind = KINDS[(group, obj["kind"])]
        md = obj["metadata"]
        key = (kind, md.get("namespace"), md["name"])
        if key in self.objs:
            raise RuntimeError(f"AlreadyExists {key}")
        if self.fail.get(("create", kind)):
            raise RuntimeError(self.fail[("create", kind)])
        if self.no_halt and kind == run.K_RESTORE and "haltAfterRestore" in obj["spec"]:
            raise RuntimeError('strict decoding error: unknown field "spec.haltAfterRestore"')
        self.calls.append(("create", kind, md.get("namespace"), md["name"]))
        obj = copy.deepcopy(obj)
        obj["metadata"].setdefault("uid", f"uid-{md['name']}")
        self.objs[key] = copy.deepcopy(obj)
        getattr(self, "_on_" + kind.split(".")[0], lambda o: None)(self.objs[key])
        return copy.deepcopy(obj)

    def replace(self, obj):
        md = obj["metadata"]
        key = (run.K_VM, md["namespace"], md["name"])
        self.calls.append(("replace", run.K_VM, md["namespace"], md["name"]))
        self.objs[key] = copy.deepcopy(obj)
        return obj

    def patch(self, kind, ns, name, patch):
        self.calls.append(("patch", kind, ns, name, json.dumps(patch, sort_keys=True)))
        key = (kind, ns, name)
        if kind == run.K_LH_TARGET:
            return
        if kind == run.K_SETTING:
            ann = ((patch.get("metadata") or {}).get("annotations") or {})
            if "harvesterhci.io/hash" in ann and ann["harvesterhci.io/hash"] is None:
                self.resync_at = self.clock.now() + 5
            return
        if key not in self.objs:
            raise RuntimeError(f"NotFound {key}")
        merge(self.objs[key], patch)
        if kind == run.K_VM and "runStrategy" in (patch.get("spec") or {}):
            self._run_strategy(ns, name, patch["spec"]["runStrategy"])

    def delete(self, kind, ns, name, cascade=None):
        self.calls.append(("delete", kind, ns, name, cascade))
        key = (kind, ns, name)
        if key not in self.objs:
            raise RuntimeError(f"NotFound {key}")
        if kind == run.K_RESTORE:
            # Harvester (relevé sur harvlab2) : « The restore can't be removed
            # because the restored VM exists »
            target = self.objs[key]["spec"]["target"]["name"]
            if (run.K_VM, ns, target) in self.objs:
                raise RuntimeError("The restore can't be removed because the restored VM exists")
        self.objs.pop(key)
        if kind == run.K_BACKUP and self.store is not None:
            # supprimer une sauvegarde efface ses données de la cible
            # commune : elle ne revient pas par la synchronisation
            self.store.backups.pop((ns, name), None)
        if kind == run.K_DV and cascade != "orphan":
            self.objs.pop((run.K_PVC, ns, name), None)
        if kind == run.K_DV and cascade == "orphan":
            pvc = self.objs.get((run.K_PVC, ns, name))
            if pvc:
                pvc["metadata"].pop("ownerReferences", None)
        if kind == run.K_VM:
            self.objs.pop((run.K_VMI, ns, name), None)

    def raw_stream(self, path):
        # comme Harvester : toujours du gzip
        self.calls.append(("raw", path))
        img = path.rstrip("/").split("/")[-2]
        data = gzip.compress(f"disk-content-of-{img}".encode() * 1000)
        for i in range(0, len(data), 4096):
            yield data[i:i + 4096]

    # -- contrôleurs simulés ---------------------------------------------
    def _run_strategy(self, ns, name, strategy):
        if strategy == "Halted":
            self.at(10, lambda: self.objs.pop((run.K_VMI, ns, name), None))
        else:
            def up():
                if self.fail.get("start"):
                    phase = "Failed"
                else:
                    phase = "Running"
                self.objs[(run.K_VMI, ns, name)] = {
                    "metadata": {"name": name, "namespace": ns},
                    "status": {"phase": phase}}
            self.at(20, up)

    def _on_virtualmachinebackups(self, obj):
        ns, name = obj["metadata"]["namespace"], obj["metadata"]["name"]
        vm = copy.deepcopy(self.objs.get((run.K_VM, ns, obj["spec"]["source"]["name"])))

        def ready():
            o = self.objs.get((run.K_BACKUP, ns, name))
            if o is None:
                return
            o["status"] = {"readyToUse": True, "progress": 100, "source": vm}
            needs = set()
            for v in (vm or {}).get("spec", {}).get("template", {}).get("spec", {}).get("volumes", []):
                claim = (v.get("persistentVolumeClaim") or {}).get("claimName")
                pvc = self.objs.get((run.K_PVC, ns, claim)) if claim else None
                if pvc and (pvc["metadata"].get("annotations") or {}).get("harvesterhci.io/imageId"):
                    needs.add(pvc["spec"]["storageClassName"])
            if self.store is not None:
                self.store.backups[(ns, name)] = (self.clock.now(), copy.deepcopy(o), needs)
        self.at(30, ready)

    def _on_virtualmachinerestores(self, obj):
        ns, rname = obj["metadata"]["namespace"], obj["metadata"]["name"]
        spec = obj["spec"]
        halt = spec.get("haltAfterRestore", False)
        backup = self.objs[(run.K_BACKUP, spec["virtualMachineBackupNamespace"],
                            spec["virtualMachineBackupName"])]
        src_vm = backup["status"]["source"]

        def done():
            name = spec["target"]["name"]
            vm = copy.deepcopy(src_vm)
            vm["metadata"] = {"name": name, "namespace": ns, "resourceVersion": "7",
                              "labels": dict(src_vm["metadata"].get("labels") or {})}
            if halt:
                vm["spec"]["runStrategy"] = "Halted"
            elif vm["spec"].get("runStrategy") != "Halted":
                self.objs[(run.K_VMI, ns, name)] = {"metadata": {"name": name, "namespace": ns},
                                                    "status": {"phase": "Running"}}
            pvc = f"restore-{spec['virtualMachineBackupName']}-uid-disk-0"
            for v in vm["spec"]["template"]["spec"]["volumes"]:
                if "persistentVolumeClaim" in v:
                    v["persistentVolumeClaim"]["claimName"] = pvc
            self.objs[(run.K_VM, ns, name)] = vm
            self.objs[(run.K_PVC, ns, pvc)] = {"metadata": {"name": pvc, "namespace": ns},
                                               "status": {"phase": "Bound"}}
            o = self.objs.get((run.K_RESTORE, ns, rname))
            if o is not None:
                o["status"] = {"complete": True, "restores": [{
                    "persistentVolumeClaimSpec": {"metadata": {"name": pvc, "namespace": ns}}}]}
        self.at(40, done)

    def _on_virtualmachineimages(self, obj):
        ns, name = obj["metadata"]["namespace"], obj["metadata"]["name"]

        def imported():
            o = self.objs.get((run.K_IMAGE, ns, name))
            if o is not None:
                o["status"] = {"progress": 100, "conditions": [
                    {"type": "Imported", "status": "True"}]}
        self.at(30, imported)

    def _on_datavolumes(self, obj):
        ns, name = obj["metadata"]["namespace"], obj["metadata"]["name"]
        url = obj["spec"]["source"]["http"]["url"]
        self.objs[(run.K_PVC, ns, name)] = {
            "metadata": {"name": name, "namespace": ns,
                         "labels": dict(obj["metadata"]["labels"]),
                         "ownerReferences": [{"kind": "DataVolume", "name": name}]},
            "spec": {"storageClassName": obj["spec"]["storage"]["storageClassName"]},
            "status": {"phase": "Bound"}}
        obj["status"] = {"phase": "ImportScheduled"}

        def fetch():
            o = self.objs.get((run.K_DV, ns, name))
            if o is None:
                return
            try:
                # comme CDI (relevé sur harvlab2) : une première connexion
                # pour reconnaître le format, coupée, puis la vraie
                with urllib.request.urlopen(url, timeout=5) as r:
                    r.read(16)
                with urllib.request.urlopen(url, timeout=5) as r:
                    data = gzip.decompress(r.read())
                self.objs[(run.K_PVC, ns, name)]["data"] = data
                o["status"] = {"phase": "Succeeded", "progress": "100.0%"}
            except Exception as e:           # noqa: BLE001
                o["status"] = {"phase": "ImportInProgress", "conditions": [
                    {"type": "Running", "status": "False", "message": f"Unable to connect: {e}"}]}
                self.at(30, fetch)
        self.at(15, fetch)


# ---------------------------------------------------------------------------
# Mise en place
# ---------------------------------------------------------------------------

def leap156(run_strategy="Always"):
    vm = json.loads((FIX / "vm-leap156.json").read_text())
    vm["spec"]["runStrategy"] = run_strategy
    vm.pop("status", None)
    return vm


def seed_source(c, running=True):
    c.put(run.K_VM, leap156("Always" if running else "Halted"))
    if running:
        c.put(run.K_VMI, {"metadata": {"name": "leap156", "namespace": "default"},
                          "status": {"phase": "Running"}})
    c.put(run.K_PVC, {"metadata": {"name": "leap156-disk-0-gfcec", "namespace": "default",
                                   "annotations": {"harvesterhci.io/imageId": "default/opensuse-leap-cloud"}},
                      "spec": {"accessModes": ["ReadWriteMany"], "volumeMode": "Block",
                               "storageClassName": "longhorn-opensuse-leap-cloud",
                               "resources": {"requests": {"storage": "10Gi"}}},
                      "status": {"phase": "Bound"}})
    c.put(run.K_SECRET, {"metadata": {"name": "leap156-hd4ry", "namespace": "default"},
                         "type": "secret", "data": {"userdata": "I2Nsb3VkLWNvbmZpZw=="}})


def seed_target(c):
    for ns in ("default", "lab"):
        c.put(run.K_NS, {"metadata": {"name": ns}})


def request(**kw):
    r = {"kind": "migrate", "vm_ns": "default", "vm_name": "leap156",
         "name": "leap156", "namespace": "default", "mode": "stop",
         "source": "stopped", "target": "started", "keep_mac": True,
         "networks": {"default/production": "lab/vlan10"},
         "storage_classes": {"longhorn-opensuse-leap-cloud": "harvester-longhorn"},
         "create_namespace": False, "keep_backups": False}
    r.update(kw)
    return r


class Env:
    def __init__(self, shared=True, auto_sync=True, running=True, fail_dst=None, fail_src=None,
                 no_halt=False):
        self.clock = Clock()
        store = Store() if shared else None
        self.src = FakeCluster("harvlab", self.clock, store, fail=fail_src)
        self.dst = FakeCluster("harvlab2", self.clock, store, auto_sync=auto_sync, fail=fail_dst,
                               no_halt=no_halt)
        seed_source(self.src, running)
        seed_target(self.dst)
        self.events = []
        self.progress = []
        self.server = None

    def ctx(self, req, advertise=None, serve=False):
        if serve:
            self.server = vs.DiskServer(bind="127.0.0.1", port=0)
            port = self.server.start()
            advertise = advertise or f"127.0.0.1:{port}"
        return run.Ctx(self.src, self.dst, req, "t1", emit=lambda *a: self.events.append(a),
                       sleep=self.clock.sleep, now=self.clock.now, server=self.server,
                       advertise=advertise, source_cluster="harvlab", target_cluster="harvlab2",
                       progress=self.progress.append)

    def close(self):
        if self.server:
            self.server.stop()

    def steps(self):
        return [e[0] for e in self.events]

    def calls(self, cluster, verb, kind=None):
        return [c for c in cluster.calls if c[0] == verb and (kind is None or c[1] == kind)]


@pytest.fixture
def env():
    envs = []

    def make(**kw):
        e = Env(**kw)
        envs.append(e)
        return e
    yield make
    for e in envs:
        e.close()


# ---------------------------------------------------------------------------
# Moteur « sauvegarde »
# ---------------------------------------------------------------------------

def test_backup_engine_stop_mode(env):
    e = env()
    ctx = e.ctx(request())
    target = run.run_backup(ctx)
    run.finalize(ctx)
    assert target == ("default", "leap156")
    # une seule sauvegarde, prise VM arrêtée
    backups = e.calls(e.src, "create", run.K_BACKUP)
    assert [b[3] for b in backups] == ["leap156-xfer-t1-a"]
    order = [c[:2] for c in e.src.calls]
    assert order.index(("patch", run.K_VM)) < order.index(("create", run.K_BACKUP))
    # la VM restaurée : réseaux remappés, démarrée, sans étiquette de transfert
    vm = e.dst.objs[(run.K_VM, "default", "leap156")]
    assert vm["spec"]["template"]["spec"]["networks"][0]["multus"]["networkName"] == "lab/vlan10"
    assert vm["spec"]["runStrategy"] == "Always"
    assert vt.TRANSFER_LABEL not in vm["metadata"].get("labels", {})
    assert vm["metadata"]["annotations"][run.TRANSFERRED_FROM] == "harvlab/default/leap156"
    assert e.dst.objs[(run.K_VMI, "default", "leap156")]["status"]["phase"] == "Running"
    # la source : arrêtée et annotée
    src = e.src.objs[(run.K_VM, "default", "leap156")]
    assert src["spec"]["runStrategy"] == "Halted"
    assert src["metadata"]["annotations"][vt.TRANSFERRED_TO] == "harvlab2/default/leap156"
    # rien d'étiqueté ne reste, les sauvegardes sont parties ; la restauration
    # reste, Harvester la lie à la VM restaurée pour toute sa vie
    assert e.src.labelled() == [] and e.dst.labelled() == []
    assert not any(k[0] == run.K_BACKUP for k in list(e.src.objs) + list(e.dst.objs))
    assert [k for k in e.dst.objs if k[0] == run.K_RESTORE]


def test_backup_engine_short_mode_takes_a_second_backup_after_the_stop(env):
    e = env()
    ctx = e.ctx(request(mode="short"))
    run.run_backup(ctx)
    run.finalize(ctx)
    seq = [(c[0], c[1]) for c in e.src.calls if c[1] in (run.K_BACKUP, run.K_VM)]
    first_b = seq.index(("create", run.K_BACKUP))
    stop = seq.index(("patch", run.K_VM))
    second_b = len(seq) - 1 - seq[::-1].index(("create", run.K_BACKUP))
    assert first_b < stop < second_b
    names = [b[3] for b in e.calls(e.src, "create", run.K_BACKUP)]
    assert names == ["leap156-xfer-t1-a", "leap156-xfer-t1-b"]
    # la restauration part de la seconde
    restore = e.calls(e.dst, "create", run.K_RESTORE)
    assert restore


def test_a_copy_takes_one_backup_without_stopping(env):
    e = env()
    ctx = e.ctx(request(source="running", name="leap156-copy"))
    run.run_backup(ctx)
    run.finalize(ctx)
    assert not [c for c in e.src.calls if c[0] == "patch" and c[1] == run.K_VM
                and "Halted" in c[4]]
    assert len(e.calls(e.src, "create", run.K_BACKUP)) == 1
    assert e.src.objs[(run.K_VMI, "default", "leap156")]["status"]["phase"] == "Running"
    # deux VM en marche : jamais la même adresse MAC
    r = [o for (k, _, _), o in e.dst.objs.items() if k == run.K_RESTORE]
    assert len(r) == 1 and r[0]["spec"]["keepMacAddress"] is False


def test_restore_keeps_mac_only_when_asked(env):
    for keep in (True, False):
        e = env()
        created = []
        orig = e.dst.create

        def spy(obj, orig=orig):
            created.append(obj)
            return orig(obj)
        e.dst.create = spy
        ctx = e.ctx(request(keep_mac=keep))
        run.run_backup(ctx)
        m = [o for o in created if o["kind"] == "VirtualMachineRestore"][0]
        assert m["spec"]["keepMacAddress"] is keep
        assert m["spec"]["haltAfterRestore"] is True


def test_sync_is_nudged_until_the_backup_shows_up(env):
    """Relevé sur harvlab2 (refreshIntervalInSeconds à 0) : Harvester ne
    relit la cible que si l'annotation `harvesterhci.io/hash` du réglage
    manque. Une autre annotation ne déclenche rien. Et le premier passage ne
    fait que ramener l'image : il en faut un second pour la sauvegarde."""
    e = env(auto_sync=False)
    ctx = e.ctx(request())
    run.run_backup(ctx)
    nudges = [c for c in e.dst.calls if c[0] == "patch" and c[1] == run.K_SETTING]
    assert len(nudges) >= 2
    assert all('"harvesterhci.io/hash": null' in n[4] for n in nudges)
    # et Longhorn relit sa cible tout de suite, au lieu de ses 5 minutes
    lh = [c for c in e.dst.calls if c[0] == "patch" and c[1] == run.K_LH_TARGET]
    assert lh and "syncRequestedAt" in lh[0][4] and lh[0][2] == "longhorn-system"
    assert (run.K_VM, "default", "leap156") in e.dst.objs


def test_restore_on_a_target_without_halt_after_restore(env):
    """Relevé sur harvlab2 (Harvester 1.8.2) : `haltAfterRestore` n'existe
    pas (« strict decoding error: unknown field »), et la VM restaurée
    démarre d'elle-même. On l'arrête avant d'appliquer les correspondances."""
    e = env(no_halt=True)
    ctx = e.ctx(request(restore_halt=False, target="stopped"))
    run.run_backup(ctx)
    run.finalize(ctx)
    sent = [c for c in e.dst.calls if c[:2] == ("create", run.K_RESTORE)]
    assert sent
    assert (run.K_VMI, "default", "leap156") not in e.dst.objs
    vm = e.dst.objs[(run.K_VM, "default", "leap156")]
    assert vm["spec"]["runStrategy"] == "Halted"
    assert vm["spec"]["template"]["spec"]["networks"][0]["multus"]["networkName"] == "lab/vlan10"


def test_rollback_removes_the_images_harvester_brought_for_the_transfer(env):
    e = env(fail_dst={"start": True})
    ctx = e.ctx(request())
    run.run_backup(ctx)
    assert [k for k in e.dst.objs if k[0] == "virtualmachineimages.harvesterhci.io"]
    with pytest.raises(run.TransferError):
        run.finalize(ctx)
    run.rollback(ctx)
    assert not [k for k in e.dst.objs if k[0] == "virtualmachineimages.harvesterhci.io"]


def test_sync_that_never_comes_fails_and_rolls_back(env):
    e = env(auto_sync=False)
    e.dst.patch = lambda *a, **k: None          # la relance ne sert à rien
    ctx = e.ctx(request(), )
    ctx.timeouts["sync"] = 400
    with pytest.raises(run.TransferError) as ei:
        run.run_backup(ctx)
    assert ei.value.sid == "sync"
    run.rollback(ctx)
    assert e.src.labelled() == []
    # la source était en marche : elle repart
    assert e.src.objs[(run.K_VMI, "default", "leap156")]["status"]["phase"] == "Running"


def test_a_target_that_does_not_start_rolls_everything_back(env):
    e = env(fail_dst={"start": True})
    ctx = e.ctx(request())
    run.run_backup(ctx)
    with pytest.raises(run.TransferError):
        run.finalize(ctx)
    run.rollback(ctx)
    assert (run.K_VM, "default", "leap156") not in e.dst.objs
    assert not [k for k in e.dst.objs if k[0] == run.K_PVC and k[2].startswith("restore-")]
    # la restauration ne se supprime qu'une fois la VM partie : elle est partie
    assert not [k for k in e.dst.objs if k[0] == run.K_RESTORE]
    # la source n'a jamais été supprimée, et elle repart
    assert (run.K_VM, "default", "leap156") in e.src.objs
    assert e.src.objs[(run.K_VMI, "default", "leap156")]["status"]["phase"] == "Running"
    assert e.src.labelled() == [] and e.dst.labelled() == []


def test_delete_source_only_after_the_target_is_verified(env):
    e = env()
    ctx = e.ctx(request(source="deleted"))
    run.run_backup(ctx)
    run.finalize(ctx)
    assert (run.K_VM, "default", "leap156") not in e.src.objs
    assert (run.K_PVC, "default", "leap156-disk-0-gfcec") not in e.src.objs
    start_target = [i for i, ev in enumerate(e.events) if ev[0] == "start-target" and ev[1] == "done"][0]
    source_done = [i for i, ev in enumerate(e.events) if ev[0] == "source" and ev[1] == "done"][0]
    assert start_target < source_done


def test_keep_backups_leaves_them_unlabelled(env):
    e = env()
    ctx = e.ctx(request(keep_backups=True))
    run.run_backup(ctx)
    run.finalize(ctx)
    assert (run.K_BACKUP, "default", "leap156-xfer-t1-a") in e.src.objs
    assert e.src.labelled() == [] and e.dst.labelled() == []


def test_target_left_stopped_is_verified_by_its_volumes(env):
    e = env()
    ctx = e.ctx(request(target="stopped"))
    run.run_backup(ctx)
    run.finalize(ctx)
    assert (run.K_VMI, "default", "leap156") not in e.dst.objs
    assert e.dst.objs[(run.K_VM, "default", "leap156")]["spec"]["runStrategy"] == "Halted"
    assert ("verify", "done") in [(ev[0], ev[1]) for ev in e.events]


# ---------------------------------------------------------------------------
# Moteur « fichier »
# ---------------------------------------------------------------------------

def test_direct_transfer_streams_each_disk_to_cdi(env):
    e = env(shared=False)
    ctx = e.ctx(request(), serve=True)
    target = run.run_direct(ctx, "harv-rep1")
    run.finalize(ctx)
    assert target == ("default", "leap156")
    claim = "leap156-disk-0-t1"
    pvc = e.dst.objs[(run.K_PVC, "default", claim)]
    # le disque est bien passé par le guichet, en flux, depuis la source
    assert pvc["data"] == b"disk-content-of-xfer-t1-disk-0" * 1000
    assert pvc["spec"]["storageClassName"] == "harvester-longhorn"
    # DataVolume supprimé en orphelin : le volume reste, sans propriétaire
    assert (run.K_DV, "default", claim) not in e.dst.objs
    assert "ownerReferences" not in pvc["metadata"]
    assert [c for c in e.dst.calls if c[0] == "delete" and c[1] == run.K_DV] == \
        [("delete", run.K_DV, "default", claim, "orphan")]
    # VM cible : disque et secret réécrits, démarrée
    vm = e.dst.objs[(run.K_VM, "default", "leap156")]
    vols = {v["name"]: v for v in vm["spec"]["template"]["spec"]["volumes"]}
    assert vols["disk-0"]["persistentVolumeClaim"]["claimName"] == claim
    secret = vols["cloudinitdisk"]["cloudInitNoCloud"]["secretRef"]["name"]
    assert secret == "leap156-cloudinit-t1"
    assert e.dst.objs[(run.K_SECRET, "default", secret)]["data"] == {"userdata": "I2Nsb3VkLWNvbmZpZw=="}
    # images temporaires supprimées, rien d'étiqueté ne reste
    assert not [k for k in e.src.objs if k[0] == run.K_IMAGE]
    assert e.src.labelled() == [] and e.dst.labelled() == []


def test_export_restarts_the_source_before_the_download(env, tmp_path):
    e = env(shared=False)
    w = vt.ArchiveWriter(tmp_path / "leap156.hvx")
    ctx = e.ctx(request(kind="export", source="running"))
    run.run_export(ctx, w, "harv-rep1", "v1.8.2")
    run.finish_export(ctx)
    calls = e.src.calls
    restart = [i for i, c in enumerate(calls) if c[0] == "patch" and c[1] == run.K_VM
               and "Always" in c[4]][0]
    first_raw = [i for i, c in enumerate(calls) if c[0] == "raw"][0]
    assert restart < first_raw
    r = vt.ArchiveReader(tmp_path / "leap156.hvx")
    assert r.complete and r.verify() == []
    m = r.manifest()
    assert m["source"] == {"cluster": "harvlab", "version": "v1.8.2",
                           "namespace": "default", "name": "leap156"}
    assert m["inventory"]["disks"][0]["member"] == "disks/disk-0.raw.gz"
    assert m["secrets"][0]["name"] == "leap156-hd4ry"
    assert "status" not in m["vm"]
    assert not [k for k in e.src.objs if k[0] == run.K_IMAGE]


def test_export_leaves_a_stopped_source_stopped(env, tmp_path):
    e = env(shared=False)
    ctx = e.ctx(request(kind="export", source="stopped"))
    run.run_export(ctx, vt.ArchiveWriter(tmp_path / "a.hvx"), "harv-rep1")
    run.finish_export(ctx)
    assert (run.K_VMI, "default", "leap156") not in e.src.objs
    assert vt.TRANSFERRED_TO not in (e.src.objs[(run.K_VM, "default", "leap156")]["metadata"]
                                     .get("annotations") or {})


def test_export_images_are_deleted_even_when_the_download_fails(env, tmp_path):
    e = env(shared=False)

    def broken(path):
        yield b"abc"
        raise OSError("connection reset")
    e.src.raw_stream = broken
    ctx = e.ctx(request(kind="export"))
    with pytest.raises(OSError):
        run.run_export(ctx, vt.ArchiveWriter(tmp_path / "a.hvx"), "harv-rep1")
    assert not [k for k in e.src.objs if k[0] == run.K_IMAGE]


def test_import_from_an_archive(env, tmp_path):
    e = env(shared=False)
    path = tmp_path / "leap156.hvx"
    ctx = e.ctx(request(kind="export"))
    run.run_export(ctx, vt.ArchiveWriter(path), "harv-rep1", "v1.8.2")
    r = vt.ArchiveReader(path)
    m = r.manifest()
    e2 = env(shared=False)
    sources = {}
    for d in m["inventory"]["disks"]:
        def opener(member=d["member"]):
            with r.open_member(member) as f:
                while True:
                    b = f.read(65536)
                    if not b:
                        return
                    yield b
        sources[d["volume"]] = (opener, r.member(d["member"])[1])
    ctx2 = e2.ctx(request(kind="import", namespace="imported", create_namespace=True,
                          name="leap156"), serve=True)
    run.run_import(ctx2, m, sources)
    run.finalize(ctx2)
    assert (run.K_NS, None, "imported") in e2.dst.objs
    pvc = e2.dst.objs[(run.K_PVC, "imported", "leap156-disk-0-t1")]
    assert pvc["data"] == b"disk-content-of-xfer-t1-disk-0" * 1000
    vm = e2.dst.objs[(run.K_VM, "imported", "leap156")]
    assert vm["metadata"]["namespace"] == "imported"
    assert e2.dst.labelled() == []
    # v1.47.0 : le secret cloud-init appartient à la VM, comme chez Harvester
    secrets = [o for (k, n, _), o in e2.dst.objs.items() if k == run.K_SECRET and n == "imported"]
    assert secrets
    for sec in secrets:
        assert sec["metadata"]["ownerReferences"] == [{
            "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
            "name": "leap156", "uid": vm["metadata"]["uid"]}]
    # un import ne touche à aucune source
    assert not [c for c in e2.src.calls if c[0] != "raw"]


def test_a_target_that_cannot_reach_the_console_is_explained(env):
    e = env(shared=False)
    ctx = e.ctx(request(), serve=True, advertise="127.0.0.1:9")    # rien n'écoute
    ctx.timeouts["first_hit"] = 120
    with pytest.raises(run.TransferError) as ei:
        run.run_direct(ctx, "harv-rep1")
    msg = str(ei.value)
    assert "never fetched the disk" in msg and "127.0.0.1:9" in msg
    run.rollback(ctx)
    assert e.src.labelled() == [] and e.dst.labelled() == []
    assert not [k for k in e.dst.objs if k[0] in (run.K_PVC, run.K_DV, run.K_VM)]
    assert e.src.objs[(run.K_VMI, "default", "leap156")]["status"]["phase"] == "Running"


@pytest.mark.parametrize("fail_kind", [run.K_DV, run.K_SECRET, run.K_VM])
def test_nothing_labelled_remains_after_a_failure_at_any_step(env, fail_kind):
    e = env(shared=False, fail_dst={("create", fail_kind): "webhook denied"})
    ctx = e.ctx(request(), serve=True)
    with pytest.raises(Exception):
        run.run_direct(ctx, "harv-rep1")
    run.rollback(ctx)
    assert e.src.labelled() == [] and e.dst.labelled() == []
    assert not [k for k in e.src.objs if k[0] == run.K_IMAGE]
    assert e.src.objs[(run.K_VMI, "default", "leap156")]["status"]["phase"] == "Running"


def test_progress_is_emitted_on_change_only(env):
    e = env()
    ctx = e.ctx(request())
    run.run_backup(ctx)
    msgs = [ev for ev in e.events if ev[0] == "backup" and ev[1] == "running"]
    assert len(msgs) == len(set(m[2] for m in msgs))


def test_target_starts_like_the_source_ran(env):
    e = env()
    src = e.src.objs[(run.K_VM, "default", "leap156")]
    src["spec"]["runStrategy"] = "RerunOnFailure"
    ctx = e.ctx(request())
    run.run_backup(ctx)
    run.finalize(ctx)
    assert e.dst.objs[(run.K_VM, "default", "leap156")]["spec"]["runStrategy"] == "RerunOnFailure"


def test_a_download_refused_before_its_first_byte_is_retried(env, tmp_path):
    """Vécu sur harvlab2 : le proxy de l'API a répondu une fois
    « ServiceUnavailable ... tls: unrecognized name », puis a servi le même
    téléchargement une minute plus tard. Un tel accroc ne doit pas faire
    échouer tout un transfert."""
    e = env(shared=False)
    orig = e.src.raw_stream
    fails = {"n": 0}

    def flaky(path):
        if fails["n"] < 2:
            fails["n"] += 1
            raise RuntimeError("ServiceUnavailable: tls: unrecognized name")
        yield from orig(path)
    e.src.raw_stream = flaky
    ctx = e.ctx(request(kind="export"))
    run.run_export(ctx, vt.ArchiveWriter(tmp_path / "a.hvx"), "harv-rep1")
    assert vt.ArchiveReader(tmp_path / "a.hvx").verify() == []
    assert any("retrying" in ev[2] for ev in e.events)


def test_a_download_that_breaks_midway_is_not_silently_retried(env, tmp_path):
    e = env(shared=False)

    def broken(path):
        yield b"abc"
        raise OSError("connection reset")
    e.src.raw_stream = broken
    ctx = e.ctx(request(kind="export"))
    with pytest.raises(OSError):
        run.run_export(ctx, vt.ArchiveWriter(tmp_path / "a.hvx"), "harv-rep1")


def test_a_download_refused_three_times_fails(env, tmp_path):
    e = env(shared=False)

    def refused(path):
        raise RuntimeError("ServiceUnavailable")
        yield b""                                  # noqa: unreachable
    e.src.raw_stream = refused
    ctx = e.ctx(request(kind="export"))
    with pytest.raises(RuntimeError):
        run.run_export(ctx, vt.ArchiveWriter(tmp_path / "a.hvx"), "harv-rep1")


# ---------------------------------------------------------------------------
# Progression et vitesse (v1.46.0)
# ---------------------------------------------------------------------------

GIB = 1024 ** 3


def finals(e, phase):
    return [p for p in e.progress if p["phase"] == phase and p["final"]]


def add_second_disk(c):
    vm = c.objs[(run.K_VM, "default", "leap156")]
    tspec = vm["spec"]["template"]["spec"]
    tspec["volumes"].append({"name": "disk-1", "persistentVolumeClaim": {"claimName": "leap156-data"}})
    tspec["domain"]["devices"]["disks"].append({"name": "disk-1", "disk": {"bus": "virtio"}})
    c.put(run.K_PVC, {"metadata": {"name": "leap156-data", "namespace": "default"},
                      "spec": {"accessModes": ["ReadWriteMany"], "volumeMode": "Block",
                               "storageClassName": "harvester-longhorn",
                               "resources": {"requests": {"storage": "5Gi"}}},
                      "status": {"phase": "Bound"}})


def test_direct_transfer_publishes_freeze_and_import(env):
    e = env(shared=False)
    ctx = e.ctx(request(), serve=True)
    run.run_direct(ctx, "harv-rep1")
    fr, im = finals(e, "freeze"), finals(e, "import")
    assert len(fr) == 1 and fr[0]["total"] == 10 * GIB and fr[0]["done"] == 10 * GIB
    assert len(im) == 1 and im[0]["total"] == 10 * GIB
    # le bilan de chaque phase reste dans les étapes, pour l'Activité
    msgs = [ev[2] for ev in e.events if ev[1] == "done"]
    assert any(m.startswith("freeze: ") for m in msgs)
    assert any(m.startswith("import: ") and "sent" in m for m in msgs)


def test_the_cdi_probe_does_not_count_twice(env):
    """Le faux CDI lit 16 octets puis coupe, puis retélécharge tout (comme le
    vrai) : le compte de ce disque repart de zéro à la seconde requête."""
    e = env(shared=False)
    ctx = e.ctx(request(), serve=True)
    run.run_direct(ctx, "harv-rep1")
    sent_once = len(gzip.compress(b"disk-content-of-xfer-t1-disk-0" * 1000))
    last = finals(e, "import")[0]
    # disque importé en entier : compté pour sa taille, et ses octets
    # transmis une seule fois (la sonde a été remise à zéro)
    assert last["done"] == 10 * GIB
    assert last["wire"] == sent_once


def test_export_publishes_the_download_with_raw_and_sent_bytes(env, tmp_path):
    e = env(shared=False)
    ctx = e.ctx(request(kind="export"))
    run.run_export(ctx, vt.ArchiveWriter(tmp_path / "a.hvx"), "harv-rep1")
    dl = finals(e, "download")
    assert len(dl) == 1 and dl[0]["total"] == 10 * GIB and dl[0]["items_total"] == 1
    raw = len(b"disk-content-of-xfer-t1-disk-0" * 1000)
    assert dl[0]["done"] == raw and dl[0]["wire"] < raw


def test_backup_engine_publishes_backup_and_restore(env):
    e = env()
    ctx = e.ctx(request())
    run.run_backup(ctx)
    assert finals(e, "backup")[0]["total"] == 10 * GIB
    assert finals(e, "restore")[0]["done"] == 10 * GIB
    # l'image ramenée par la synchronisation a aussi sa progression
    assert finals(e, "images")


def test_disks_are_imported_together_by_default(env):
    e = env(shared=False)
    add_second_disk(e.src)
    ctx = e.ctx(request(storage_classes={"longhorn-opensuse-leap-cloud": "harv-rep1",
                                         "harvester-longhorn": "harv-rep1"}), serve=True)
    run.run_direct(ctx, "harv-rep1")
    seq = [c[:2] for c in e.dst.calls if c[1] == run.K_DV]
    assert seq[:2] == [("create", run.K_DV)] * 2, seq
    assert finals(e, "import")[0]["total"] == 15 * GIB


def test_parallel_one_imports_disk_after_disk(env):
    e = env(shared=False)
    add_second_disk(e.src)
    ctx = e.ctx(request(parallel=1, storage_classes={"longhorn-opensuse-leap-cloud": "harv-rep1",
                                                     "harvester-longhorn": "harv-rep1"}), serve=True)
    run.run_direct(ctx, "harv-rep1")
    seq = [c[:2] for c in e.dst.calls if c[1] == run.K_DV]
    assert seq == [("create", run.K_DV), ("delete", run.K_DV)] * 2, seq


def lh_settings(c, value="2"):
    for n in ("backup-concurrent-limit", "restore-concurrent-limit"):
        c.put(run.K_LH_SETTING, {"metadata": {"name": n, "namespace": "longhorn-system"},
                                 "value": value})


def test_boost_raises_longhorn_concurrency_then_restores_it(env):
    e = env()
    lh_settings(e.src)
    lh_settings(e.dst)
    ctx = e.ctx(request(boost=True))
    run.run_backup(ctx)
    raised = [c for c in e.src.calls if c[:2] == ("patch", run.K_LH_SETTING)]
    assert raised and '"8"' in raised[0][4] and raised[0][3] == "backup-concurrent-limit"
    assert [c for c in e.dst.calls if c[:2] == ("patch", run.K_LH_SETTING)][0][3] == \
        "restore-concurrent-limit"
    assert e.src.objs[(run.K_LH_SETTING, "longhorn-system", "backup-concurrent-limit")]["value"] == "8"
    run.finalize(ctx)
    assert e.src.objs[(run.K_LH_SETTING, "longhorn-system", "backup-concurrent-limit")]["value"] == "2"
    assert e.dst.objs[(run.K_LH_SETTING, "longhorn-system", "restore-concurrent-limit")]["value"] == "2"


def test_boost_is_undone_on_failure(env):
    e = env(fail_dst={"start": True})
    lh_settings(e.src)
    lh_settings(e.dst)
    ctx = e.ctx(request(boost=True))
    run.run_backup(ctx)
    with pytest.raises(run.TransferError):
        run.finalize(ctx)
    run.rollback(ctx)
    assert e.src.objs[(run.K_LH_SETTING, "longhorn-system", "backup-concurrent-limit")]["value"] == "2"
    assert e.dst.objs[(run.K_LH_SETTING, "longhorn-system", "restore-concurrent-limit")]["value"] == "2"


def test_boost_leaves_a_higher_value_alone(env):
    e = env()
    lh_settings(e.src, "12")
    lh_settings(e.dst, "12")
    ctx = e.ctx(request(boost=True))
    run.run_backup(ctx)
    assert not [c for c in e.src.calls if c[:2] == ("patch", run.K_LH_SETTING)]


# ---------------------------------------------------------------------------
# Coupures passagères (v1.46.0, vécu sur le banc : la VIP de harvlab a
# décroché pendant un téléchargement, « no route to host »)
# ---------------------------------------------------------------------------

def test_wait_for_rides_out_a_transient_api_error(env):
    e = env()
    ctx = e.ctx(request())
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Unable to connect to the server: dial tcp 172.16.2.60:6443: "
                               "connect: no route to host")
        return True
    run.wait_for(ctx, "x", "thing", fn, 600)
    assert calls["n"] == 3
    assert any("API unreachable" in ev[2] for ev in e.events)


def test_wait_for_still_raises_a_real_error(env):
    e = env()
    ctx = e.ctx(request())

    def fn():
        raise RuntimeError("admission webhook denied the request")
    with pytest.raises(RuntimeError):
        run.wait_for(ctx, "x", "thing", fn, 600)


def test_an_archive_disk_broken_midway_is_written_again(env, tmp_path):
    e = env(shared=False)
    orig = e.src.raw_stream
    state = {"n": 0}

    def flaky(path):
        state["n"] += 1
        it = orig(path)
        yield next(it)
        if state["n"] == 1:
            raise RuntimeError("Unable to connect to the server: no route to host")
        yield from it
    e.src.raw_stream = flaky
    ctx = e.ctx(request(kind="export"))
    path = tmp_path / "a.hvx"
    run.run_export(ctx, vt.ArchiveWriter(path), "harv-rep1")
    r = vt.ArchiveReader(path)
    assert r.complete and r.verify() == []
    with r.open_member("disks/disk-0.raw.gz") as f:
        assert gzip.decompress(f.read()) == b"disk-content-of-xfer-t1-disk-0" * 1000
    # compté une seule fois
    assert finals(e, "download")[0]["done"] == len(b"disk-content-of-xfer-t1-disk-0" * 1000)
    assert any("again" in ev[2] for ev in e.events)


def test_a_source_stream_broken_once_is_served_again(env):
    """Copie directe : le flux de la source casse ; CDI redemande le disque
    (le faux CDI recommence au bout de 30 s) et on le resert depuis le début."""
    e = env(shared=False)
    orig = e.src.raw_stream
    state = {"n": 0}

    def flaky(path):
        state["n"] += 1
        it = orig(path)
        yield next(it)
        if state["n"] == 2:                 # la vraie requête, après la sonde
            raise RuntimeError("Unable to connect to the server: no route to host")
        yield from it
    e.src.raw_stream = flaky
    ctx = e.ctx(request(), serve=True)
    run.run_direct(ctx, "harv-rep1")
    pvc = e.dst.objs[(run.K_PVC, "default", "leap156-disk-0-t1")]
    assert pvc["data"] == b"disk-content-of-xfer-t1-disk-0" * 1000


def test_a_source_that_keeps_breaking_fails_the_transfer(env):
    e = env(shared=False)

    def broken(path):
        yield gzip.compress(b"x")[:5]
        raise RuntimeError("Unable to connect to the server: no route to host")
    e.src.raw_stream = broken
    ctx = e.ctx(request(), serve=True)
    with pytest.raises(run.TransferError) as ei:
        run.run_direct(ctx, "harv-rep1")
    assert "source stream failed" in str(ei.value)


def test_rollback_retries_while_the_api_is_down(env):
    """Vécu sur le banc : le retour arrière a tourné pendant que l'API de la
    source était injoignable ; ses suppressions ont échoué en silence et les
    images temporaires sont restées."""
    e = env(shared=False)
    ctx = e.ctx(request(), serve=True, advertise="127.0.0.1:9")
    ctx.timeouts["first_hit"] = 60
    with pytest.raises(run.TransferError):
        run.run_direct(ctx, "harv-rep1")
    # les images ont été supprimées par run_direct (finally) ; on en recrée
    # une étiquetée, et l'API refuse deux fois avant de répondre
    e.src.put(run.K_IMAGE, {"metadata": {"name": "xfer-t1-disk-0", "namespace": "default",
                                         "labels": {vt.TRANSFER_LABEL: "t1"}}})
    ctx.record("src", run.K_IMAGE, "default", "xfer-t1-disk-0")
    orig, n = e.src.delete, {"k": 0}

    def flaky(kind, ns, name, cascade=None):
        if kind == run.K_IMAGE and n["k"] < 2:
            n["k"] += 1
            raise RuntimeError("Unable to connect to the server: no route to host")
        return orig(kind, ns, name, cascade)
    e.src.delete = flaky
    run.rollback(ctx)
    assert e.src.labelled() == []


def test_rollback_names_what_it_could_not_undo(env):
    e = env(shared=False)
    ctx = e.ctx(request())
    e.src.put(run.K_IMAGE, {"metadata": {"name": "stuck", "namespace": "default",
                                         "labels": {vt.TRANSFER_LABEL: "t1"}}})
    ctx.record("src", run.K_IMAGE, "default", "stuck")

    def refuse(kind, ns, name, cascade=None):
        raise RuntimeError("admission webhook denied the request")
    e.src.delete = refuse
    run.rollback(ctx)
    errs = [ev for ev in e.events if ev[0] == "rollback" and ev[1] == "error"]
    assert errs and "virtualmachineimages.harvesterhci.io default/stuck" in errs[-1][2]
