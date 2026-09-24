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
    def __init__(self, name, clock, store=None, auto_sync=True, fail=None):
        self.name, self.clock, self.store = name, clock, store
        self.auto_sync = auto_sync
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
            for (ns, name), (ready_at, obj) in list(self.store.backups.items()):
                if (run.K_BACKUP, ns, name) in self.objs:
                    continue
                synced = (self.auto_sync and now >= ready_at + 20) or \
                         (self.resync_at is not None and now >= self.resync_at)
                if synced and now >= ready_at:
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
        self.calls.append(("create", kind, md.get("namespace"), md["name"]))
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
        if kind == run.K_SETTING:
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
        self.calls.append(("raw", path))
        img = path.rstrip("/").split("/")[-2]
        data = f"disk-content-of-{img}".encode() * 1000
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
            if self.store is not None:
                self.store.backups[(ns, name)] = (self.clock.now(), copy.deepcopy(o))
        self.at(30, ready)

    def _on_virtualmachinerestores(self, obj):
        ns, rname = obj["metadata"]["namespace"], obj["metadata"]["name"]
        spec = obj["spec"]
        backup = self.objs[(run.K_BACKUP, spec["virtualMachineBackupNamespace"],
                            spec["virtualMachineBackupName"])]
        src_vm = backup["status"]["source"]

        def done():
            name = spec["target"]["name"]
            vm = copy.deepcopy(src_vm)
            vm["metadata"] = {"name": name, "namespace": ns, "resourceVersion": "7",
                              "labels": dict(src_vm["metadata"].get("labels") or {})}
            vm["spec"]["runStrategy"] = "Halted"
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
                    data = r.read()
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
    def __init__(self, shared=True, auto_sync=True, running=True, fail_dst=None, fail_src=None):
        self.clock = Clock()
        store = Store() if shared else None
        self.src = FakeCluster("harvlab", self.clock, store, fail=fail_src)
        self.dst = FakeCluster("harvlab2", self.clock, store, auto_sync=auto_sync, fail=fail_dst)
        seed_source(self.src, running)
        seed_target(self.dst)
        self.events = []
        self.server = None

    def ctx(self, req, advertise=None, serve=False):
        if serve:
            self.server = vs.DiskServer(bind="127.0.0.1", port=0)
            port = self.server.start()
            advertise = advertise or f"127.0.0.1:{port}"
        return run.Ctx(self.src, self.dst, req, "t1", emit=lambda *a: self.events.append(a),
                       sleep=self.clock.sleep, now=self.clock.now, server=self.server,
                       advertise=advertise, source_cluster="harvlab", target_cluster="harvlab2")

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
    # rien d'étiqueté ne reste, les sauvegardes et la restauration sont parties
    assert e.src.labelled() == [] and e.dst.labelled() == []
    assert not any(k[0] in (run.K_BACKUP, run.K_RESTORE) for k in list(e.src.objs) + list(e.dst.objs))


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
    assert not r      # nettoyée ; on vérifie le manifeste envoyé
    sent = [c for c in e.dst.calls if c[:2] == ("create", run.K_RESTORE)]
    assert sent


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


def test_sync_is_nudged_when_the_backup_does_not_show_up(env):
    e = env(auto_sync=False)
    ctx = e.ctx(request())
    run.run_backup(ctx)
    nudges = [c for c in e.dst.calls if c[0] == "patch" and c[1] == run.K_SETTING]
    assert len(nudges) == 1 and run.RESYNC_ANNOTATION in nudges[0][4]


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
