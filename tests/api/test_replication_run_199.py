"""v1.99.0 : déroulé de la réplication (`replication_run`), sans cluster.

Deux clusters simulés en mémoire (`FakeKube`) qui partagent une cible de
sauvegarde (`Store`), avec ce que Longhorn, Harvester et KubeVirt font pour
de vrai (relevé sur le banc harvlab -> harvlab2, docs/design/
2026-10-22-replication-asynchrone.md) :

* un instantané devient prêt, une sauvegarde passe `Completed` et dépose sa
  donnée dans la cible commune (avec la backing image du volume) ;
* la cible ne voit une sauvegarde qu'après une relecture : demandée par
  `BackupVolume.spec.syncRequestedAt` (2 s), ou naturelle (150 s) ;
* le moteur d'un volume de secours (trouvé par `longhornvolume`) applique
  la dernière sauvegarde visible de son volume (`lastRestoredBackup`) ;
* un volume de secours suit la dernière sauvegarde visible de son volume
  (`status.lastBackup`, comme le contrôleur de volumes de Longhorn) ;
* l'activation (`Standby: false`, `frontend: blockdev`) rend le volume
  `detached` non `Standby`, et note ce que le moteur avait appliqué à cet
  instant (`activatedFrom`, pour le test « zéro perte ») ; avec
  `finish_on_activate`, Longhorn finit d'abord la restauration en cours ;
* un PV statique sans `claimRef` peut être pris par n'importe quel PVC en
  attente de la même classe ;
* le webhook de Longhorn refuse un volume dont la backing image manque ;
* un PVC lié à un PV statique passe `Bound`, une VM démarre ou s'arrête.

Horloge factice : des heures d'attente durent une fraction de seconde, les
délais sont vérifiés pour de bon. Le faux sait échouer à un appel choisi :
erreur, annulation (`Cancelled`), arrêt brutal (`Crash`, comme un kill -9 :
ni retour arrière ni verrou libéré), API momentanément injoignable.
"""

import base64
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import replication as rp  # noqa: E402
import replication_db as db  # noqa: E402
import replication_run as rr  # noqa: E402
import vm_transfer_progress as vp  # noqa: E402

GIB = 1024 ** 3
BT_VALUE = json.dumps({"type": "nfs", "endpoint": "nfs://172.16.0.5:/volume1/BACKUP/harvester-lab"})
BT_URL = "nfs://172.16.0.5:/volume1/BACKUP/harvester-lab/"


class Crash(BaseException):
    """kill -9 : rien ne s'exécute après (pas de retour arrière)."""


class Clock:
    def __init__(self):
        self.t = 1_792_000_000.0

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


def iso(t):
    return rp.iso(__import__("datetime").datetime.fromtimestamp(t, __import__("datetime").timezone.utc))


KIND_OF = dict(rr._KINDS, VirtualMachineInstance=rr.K_VMI)


def _terms(selector):
    """Termes d'un sélecteur, sans couper l'ensemble d'un `clé in (a,b)`."""
    out, cur, depth = [], "", 0
    for ch in selector:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


def _match(labels, selector):
    if not selector:
        return True
    for term in _terms(selector):
        term = term.strip()
        if " in (" in term:
            k, _, vals = term.partition(" in (")
            if labels.get(k.strip()) not in [v.strip() for v in vals.rstrip(")").split(",")]:
                return False
        elif "=" in term:
            k, v = term.split("=", 1)
            if labels.get(k) != v:
                return False
        elif term not in labels:
            return False
    return True


class Store:
    """La cible de sauvegarde commune."""

    def __init__(self):
        self.backups = {}       # nom -> {volume, url, snapshot_at, done_at, newly, size, labels}
        self.bbis = {}          # backing image -> url


class World:
    """Les deux clusters, le registre et le plan d'essai."""

    count = 0

    def __init__(self, tmp_path, vms=("vm-a", "vm-b"), options=None, mappings=None, interval="3m"):
        self.clock = Clock()
        self.store = Store()
        self.mutations = 0
        self.crash_at = None
        self.cancel_at = None
        self.fail_at = None
        self.src = FakeKube(self, "harvlab", "source")
        self.dst = FakeKube(self, "harvlab2", "target")
        self.log = []
        World.count += 1
        self.conn = db.connect(str(tmp_path / f"r-{World.count}.db"))
        seed_source(self.src, vms)
        seed_target(self.dst)
        req = {"name": "plan-a", "source_cluster": "harvlab", "target_cluster": "harvlab2", "rpo_objective_s": "15m",
               "interval_s": interval, "vms": [f"default/{v}" for v in vms],
               "mappings": mappings if mappings is not None else {"networks": {"default/prod": "default/lab-net"}},
               "options": dict({"cloud_init": "keep", "mac": "keep"}, **(options or {}))}
        self.pid = db.insert_plan(self.conn, rp.normalize_plan(req), plan_id="rp-0123456789ab")["id"]
        self.owner_n = 0
        self.alive = set()

    def ctx(self, **kw):
        self.owner_n += 1
        self.alive = {self.owner_n}
        kw.setdefault("timeouts", {})
        kw.setdefault("progress", lambda *a, **k: None)
        return rr.Ctx(self.conn, self.pid, self.src, self.dst, emit=lambda *a: self.log.append(a),
                      sleep=self.clock.sleep, now=self.clock.now,
                      owner=f"cli:testhost:{self.owner_n}", host="testhost",
                      pid_alive=lambda p: p in self.alive, **kw)

    def mutating(self, cluster, op, kind, name):
        self.mutations += 1
        n = self.mutations
        if self.crash_at == n:
            raise Crash(f"crash at {op} {kind} {name}")
        if self.cancel_at == n:
            raise rr.Cancelled("signal 15")
        if self.fail_at == n:
            raise RuntimeError(f"injected failure at {op} {kind} {name}")

    def plan(self):
        return db.get_plan(self.conn, self.pid)

    def owned(self, cluster):
        return sorted(k for k, o in cluster.objs.items()
                      if (o.get("metadata", {}).get("labels") or {}).get(rp.LABEL_PLAN) == self.pid)


class FakeKube:
    def __init__(self, world, name, role):
        self.world, self.name, self.role = world, name, role
        self.clock = world.clock
        self.store = world.store
        self.objs = {}
        self.calls = []
        self.timers = []
        self.down = False
        # cluster mort (et pas seulement coupé du client) : ses contrôleurs
        # Longhorn s'arrêtent aussi, une sauvegarde en cours ne finit jamais
        self.dead = False
        self.freeze = set()      # comportements suspendus (délais)
        self.snapshot_error = ""  # erreur posée sur chaque nouvel instantané
        self.hooks = {}          # (op, kind) -> fonction appelée avant l'appel
        self.last_natural = self.clock.now()
        self.backup_s = 5
        self.apply_s = 3
        self.restore_s = 10
        self.finish_on_activate = False
        # Longhorn 1.12.1 (banc, v1.99.0) : après l'activation, le moteur n'a
        # plus de `lastRestoredBackup` ni le volume de `status.lastBackup`
        self.clear_on_activate = False

    # -- outils de test --------------------------------------------------------
    def put(self, kind, obj):
        obj = copy.deepcopy(obj)
        md = obj["metadata"]
        md.setdefault("uid", f"uid-{md['name']}")
        md.setdefault("creationTimestamp", iso(self.clock.now()))
        self.objs[(kind, md.get("namespace"), md["name"])] = obj
        return obj

    def obj(self, kind, ns, name):
        return self.objs.get((kind, ns, name))

    def at(self, delay, fn):
        self.timers.append((self.clock.now() + delay, fn))

    def mutations(self, include_dry=False):
        return [c for c in self.calls if c[0] in ("create", "patch", "delete", "replace")
                and (include_dry or not c[-1] == "dry")]

    # -- simulation ------------------------------------------------------------
    def tick(self):
        if self.dead:
            return
        now = self.clock.now()
        while True:
            due = [t for t in self.timers if t[0] <= now]
            if not due:
                break
            self.timers = [t for t in self.timers if t[0] > now]
            for _, fn in sorted(due, key=lambda t: t[0]):
                fn()
        if self.role == "target":
            if "natural-sync" not in self.freeze and now - self.last_natural >= 150:
                self.last_natural = now
                self.sync_all()
            self.apply_standby(now)

    def _bv(self, vol):
        """BackupVolume d'un volume, lu sans passer par l'API du faux : les
        contrôleurs de Longhorn tournent même quand le client ne joint plus
        le cluster (`down`)."""
        for (k, _n, _m), o in self.objs.items():
            if k == rr.K_LH_BV and (o.get("spec") or {}).get("volumeName") == vol:
                return o
        return None

    def sync_volume(self, vol):
        bv = self._bv(vol)
        if bv is not None:
            self.objs[(rr.K_LH_BV, rr.NS, bv["metadata"]["name"])].setdefault("status", {})["lastSyncedAt"] = \
                iso(self.clock.now())
        # une sauvegarde effacée de la cible commune disparaît à la relecture
        for key, o in list(self.objs.items()):
            if key[0] == rr.K_LH_BACKUP and o["status"].get("volumeName") == vol and key[2] not in self.store.backups:
                self.objs.pop(key)
        for name, b in self.store.backups.items():
            if b["volume"] == vol and (rr.K_LH_BACKUP, rr.NS, name) not in self.objs:
                # comme Longhorn : la cible ne relit que les étiquettes de la
                # sauvegarde (`spec.labels` de la source), en `status.labels`
                self.put(rr.K_LH_BACKUP, {"metadata": {"name": name, "namespace": rr.NS,
                                                       "labels": {"backup-volume": vol, "backup-target": "default"}},
                                          "status": {"state": "Completed", "volumeName": vol, "url": b["url"],
                                                     "labels": dict(b["labels"]),
                                                     "snapshotCreatedAt": b["snapshot_at"],
                                                     "backupCreatedAt": b["done_at"],
                                                     "newlyUploadDataSize": str(b["newly"]), "size": str(b["size"])}})

    def ensure_bv(self, vol):
        if self._bv(vol) is None:
            # nom différent du volume, comme Longhorn depuis 1.8
            self.put(rr.K_LH_BV, {"metadata": {"name": f"bv-{abs(hash(vol)) % 10 ** 8:08d}", "namespace": rr.NS},
                                  "spec": {"volumeName": vol, "backupTargetName": "default"}})

    def sync_all(self):
        vols = {b["volume"] for b in self.store.backups.values()}
        vols |= {o["status"].get("volumeName") for k, o in self.objs.items() if k[0] == rr.K_LH_BACKUP}
        for vol in sorted(v for v in vols if v):
            self.ensure_bv(vol)
            self.sync_volume(vol)
        for bi, url in self.store.bbis.items():
            key = (rr.K_LH_BBI, rr.NS, f"bbi-{bi}")
            if key not in self.objs:
                self.put(rr.K_LH_BBI, {"metadata": {"name": f"bbi-{bi}", "namespace": rr.NS},
                                       "spec": {"backingImage": bi}, "status": {"url": url}})

    def apply_standby(self, now):
        if "apply" in self.freeze:
            return
        for (kind, ns, name), v in list(self.objs.items()):
            if kind != rr.K_LH_VOLUME or not v["spec"].get("Standby"):
                continue
            src_vol = rr.url_volume(v["spec"].get("fromBackup"))
            eng = self.objs.get((rr.K_LH_ENGINE, rr.NS, f"{name}-e-0"))
            if eng is None:
                continue
            done = sorted((o for (k, _n, _m), o in self.objs.items() if k == rr.K_LH_BACKUP
                           and o["status"].get("volumeName") == src_vol and o["status"].get("state") == "Completed"),
                          key=lambda o: o["status"]["snapshotCreatedAt"])
            if not done:
                continue
            latest = done[-1]["metadata"]["name"]
            v.setdefault("status", {})["lastBackup"] = latest
            st = eng["status"]
            if st.get("lastRestoredBackup") == latest:
                st.pop("_at", None)
                continue
            if st.get("_target") != latest:
                st["_target"] = latest
                st["_at"] = now + (self.apply_s if st.get("lastRestoredBackup") else self.restore_s)
                st["_from"] = now
            if now >= st["_at"]:
                st["lastRestoredBackup"] = latest
                st["restoreStatus"] = {"r1": {"progress": 100}}
            else:
                span = max(1, st["_at"] - st["_from"])
                st["restoreStatus"] = {"r1": {"progress": int(100 * (now - st["_from"]) / span)}}

    # -- interface client ----------------------------------------------------------
    def _guard(self, op, kind, name, mutating, dry=False):
        self.tick()
        # cluster coupé du client (`down`) : ses contrôleurs tournent
        # toujours au rythme de l'horloge commune ; mort (`dead`) : plus rien
        peer = self.world.dst if self is self.world.src else self.world.src
        if peer.down and peer is not self:
            peer.tick()
        if self.down or self.dead:
            raise RuntimeError("Unable to connect to the server: dial tcp: i/o timeout")
        hook = self.hooks.get(("dry" if dry else op, kind))
        if hook:
            hook(name)
        if mutating:
            self.world.mutating(self, op, kind, name)

    def get(self, kind, ns, name):
        self._guard("get", kind, name, False)
        self.tick()
        o = self.objs.get((kind, ns, name))
        return copy.deepcopy(o) if o is not None else None

    def list(self, kind, ns=None, selector=None):
        self._guard("list", kind, None, False)
        self.tick()
        self.calls.append(("list", kind, ns, selector, ""))
        return [copy.deepcopy(o) for (k, n, _), o in self.objs.items()
                if k == kind and (ns is None or n == ns)
                and _match((o.get("metadata") or {}).get("labels") or {}, selector)]

    def _validate(self, kind, obj, dry=False):
        md, spec = obj["metadata"], obj.get("spec") or {}
        if kind == rr.K_LH_VOLUME and spec.get("backingImage") and \
                (rr.K_LH_BI, rr.NS, spec["backingImage"]) not in self.objs:
            raise RuntimeError(f'admission webhook "validator.longhorn.io" denied the request: '
                               f'failed to get backing image {spec["backingImage"]}')
        if kind == rr.K_LH_BI:
            url = (spec.get("sourceParameters") or {}).get("backup-url")
            # dry-run avec une URL d'essai : accepté (à confirmer sur le banc)
            if url not in self.store.bbis.values() and not (dry and str(url).startswith("dry-run://")):
                raise RuntimeError("backing image backup not found")
        if kind == rr.K_LH_VOLUME and str(spec.get("fromBackup", "")).startswith("dry-run://"):
            # Longhorn 1.12.1 : « cannot get backup none » (vu sur le banc)
            raise RuntimeError('cannot get backup none: backup.longhorn.io "none" not found')
        if kind == rr.K_SECRET and md.get("namespace") == rp.CARNET_NAMESPACE and \
                (rr.K_NS, None, rp.CARNET_NAMESPACE) not in self.objs:
            raise RuntimeError(f'namespaces "{rp.CARNET_NAMESPACE}" not found')
        if kind == rr.K_LH_BACKUP and (rr.K_LH_SNAPSHOT, rr.NS, spec.get("snapshotName")) not in self.objs:
            raise RuntimeError("snapshot not found")
        hook = self.hooks.get(("validate", kind))
        if hook:
            hook(obj)
        if (kind, md.get("namespace"), md["name"]) in self.objs:
            raise RuntimeError(f"AlreadyExists {kind} {md['name']}")

    def create(self, obj, dry_run=False):
        kind = KIND_OF[obj["kind"]]
        md = obj["metadata"]
        self._guard("create", kind, md["name"], not dry_run, dry=dry_run)
        self.tick()
        self._validate(kind, obj, dry_run)
        self.calls.append(("create", kind, md.get("namespace"), md["name"], "dry" if dry_run else ""))
        if dry_run:
            return copy.deepcopy(obj)
        o = self.put(kind, obj)
        getattr(self, "_on_" + obj["kind"], lambda o: None)(o)
        return copy.deepcopy(o)

    def replace(self, obj):
        kind = KIND_OF[obj["kind"]]
        md = obj["metadata"]
        self._guard("replace", kind, md["name"], True)
        self.calls.append(("replace", kind, md.get("namespace"), md["name"], ""))
        key = (kind, md.get("namespace"), md["name"])
        if key not in self.objs:
            raise RuntimeError(f"NotFound {key}")
        self.objs[key] = copy.deepcopy(obj)
        return obj

    def patch(self, kind, ns, name, patch, dry_run=False):
        self._guard("patch", kind, name, not dry_run)
        self.tick()
        self.calls.append(("patch", kind, ns, name, json.dumps(patch, sort_keys=True)))
        key = (kind, ns, name)
        if key not in self.objs:
            raise RuntimeError(f"NotFound {key}")
        if dry_run:
            return
        merge(self.objs[key], patch)
        o = self.objs[key]
        if kind == rr.K_LH_BV and "syncRequestedAt" in (patch.get("spec") or {}) and "resync" not in self.freeze:
            vol = o["spec"]["volumeName"]
            self.at(2, lambda: self.sync_volume(vol))
        if kind == rr.K_LH_TARGET and "syncRequestedAt" in (patch.get("spec") or {}) and "resync" not in self.freeze:
            self.at(2, self.sync_all)
        if kind == rr.K_LH_VOLUME and (patch.get("spec") or {}).get("Standby") is False:
            eng = self.objs.get((rr.K_LH_ENGINE, rr.NS, f"{name}-e-0")) or {"status": {}}
            if self.finish_on_activate and eng["status"].get("_target"):
                # Longhorn termine la restauration en cours avant d'activer
                eng["status"]["lastRestoredBackup"] = eng["status"]["_target"]
            o["status"]["activatedFrom"] = eng["status"].get("lastRestoredBackup")
            if self.clear_on_activate:
                eng["status"]["lastRestoredBackup"] = ""
                o["status"]["lastBackup"] = ""
            if "activate" not in self.freeze:
                self.at(1, lambda: o["status"].update(isStandby=False, state="detached"))
        if kind == rr.K_VM and "runStrategy" in (patch.get("spec") or {}):
            self._run(ns, name, patch["spec"]["runStrategy"])

    def delete(self, kind, ns, name, cascade=None):
        self._guard("delete", kind, name, True)
        self.calls.append(("delete", kind, ns, name, ""))
        key = (kind, ns, name)
        if key not in self.objs:
            raise RuntimeError(f"NotFound {key}")
        self.objs.pop(key)
        if kind == rr.K_LH_VOLUME:
            self.objs.pop((rr.K_LH_ENGINE, rr.NS, f"{name}-e-0"), None)
        if kind == rr.K_LH_BACKUP:
            self.store.backups.pop(name, None)
        if kind == rr.K_VM:
            self.objs.pop((rr.K_VMI, ns, name), None)

    # -- contrôleurs ------------------------------------------------------------------
    def _on_Snapshot(self, o):
        o["status"] = {"readyToUse": False}
        if self.snapshot_error:
            # Longhorn 1.11.2, volume détaché (VM arrêtée) : erreur écrite le
            # temps d'attacher le volume, puis instantané pris (vu sur le banc)
            # ; une erreur définitive reste
            o["status"]["error"] = self.snapshot_error
            if "attached" not in self.snapshot_error:
                return
            self.at(2, lambda: o["status"].pop("error", None))
            self.at(3, lambda: o["status"].update(readyToUse=True, creationTime=iso(self.clock.now())))
            return
        if "snapshot" not in self.freeze:
            self.at(1, lambda: o["status"].update(readyToUse=True, creationTime=iso(self.clock.now())))

    def _on_Backup(self, o):
        """Comme le mutateur de longhorn-manager (webhook/resources/backup/
        mutator.go, v1.12.0) : étiquettes `backup-volume` (nom du VOLUME,
        celui de l'instantané s'il manque) et `backup-target` posées à la
        création. `status.volumeName` n'est rempli qu'à la fin
        (backup_controller.go, synchronisation d'une sauvegarde terminée) :
        une sauvegarde en cours ou en erreur n'a que ses étiquettes."""
        snap = self.objs[(rr.K_LH_SNAPSHOT, rr.NS, o["spec"]["snapshotName"])]
        labels = o["metadata"].setdefault("labels", {})
        vol = labels.setdefault("backup-volume", snap["spec"]["volume"])
        labels.setdefault("backup-target", "default")
        o["status"] = {"state": "InProgress", "progress": 10}
        if (rr.K_LH_VOLUME, rr.NS, vol) not in self.objs:
            # le contrôleur cherche le moteur du volume nommé par l'étiquette
            o["status"].update(state="Error", error=f"cannot find the engine of volume {vol}")
            return
        if "backup" in self.freeze:
            return
        self.at(self.backup_s, lambda: self.complete_backup(o, vol, snap["status"].get("creationTime")))

    def complete_backup(self, o, vol, snap_at, newly=25_174_351):
        name = o["metadata"]["name"]
        url = f"{BT_URL}?backup={name}&volume={vol}"
        o["status"].update(state="Completed", url=url, volumeName=vol,
                           labels=dict((o.get("spec") or {}).get("labels") or {}),
                           snapshotCreatedAt=snap_at or iso(self.clock.now()),
                           backupCreatedAt=iso(self.clock.now()), newlyUploadDataSize=str(newly), size=str(newly))
        self.store.backups[name] = {"volume": vol, "url": url, "snapshot_at": o["status"]["snapshotCreatedAt"],
                                    "done_at": o["status"]["backupCreatedAt"], "newly": newly, "size": newly,
                                    "labels": dict((o.get("spec") or {}).get("labels") or {})}
        self.ensure_bv(vol)
        lv = self.objs.get((rr.K_LH_VOLUME, rr.NS, vol))
        bi = (lv or {}).get("spec", {}).get("backingImage")
        if bi:
            self.store.bbis.setdefault(bi, f"{BT_URL}?backingImage={bi}")

    def make_backup(self, vol, name, snap_at=None, newly=25_174_351, labels=None, job_labels=None):
        """Sauvegarde déjà terminée (tâche récurrente, sauvegarde Harvester).
        `job_labels` : étiquettes de la tâche, que Longhorn met dans
        `spec.labels` et jamais dans `metadata.labels`."""
        labels = dict({"backup-volume": vol, "backup-target": "default"}, **(labels or {}))
        spec = {"snapshotName": name}
        if job_labels:
            spec["labels"] = dict(job_labels)
        o = self.put(rr.K_LH_BACKUP, {"metadata": {"name": name, "namespace": rr.NS, "labels": labels},
                                      "spec": spec, "status": {}})
        self.complete_backup(o, vol, snap_at or iso(self.clock.now()), newly)
        return o

    def _on_Volume(self, o):
        o["status"] = {"isStandby": bool(o["spec"].get("Standby")), "state": "detached", "robustness": "healthy"}
        self.put(rr.K_LH_ENGINE, {"metadata": {"name": f"{o['metadata']['name']}-e-0", "namespace": rr.NS,
                                               "labels": {"longhornvolume": o["metadata"]["name"]}},
                                  "status": {}})

    def _on_BackingImage(self, o):
        o["status"] = {"diskFileStatusMap": {"d1": {"state": "in-progress"}}}
        if "backing-image" not in self.freeze:
            self.at(5, lambda: o["status"].update(diskFileStatusMap={"d1": {"state": "ready"}}))

    def _on_PersistentVolume(self, o):
        """Contrôleur de liaison : un PV sans `claimRef` est pris par un PVC
        en attente (sans `volumeName`) de la même classe."""
        if (o.get("spec") or {}).get("claimRef"):
            return
        for (k, ns, n), p in self.objs.items():
            if k == rr.K_PVC and not p["spec"].get("volumeName") and \
                    p["spec"].get("storageClassName") == o["spec"].get("storageClassName"):
                o["spec"]["claimRef"] = {"namespace": ns, "name": n}
                p["spec"]["volumeName"] = o["metadata"]["name"]
                p.setdefault("status", {})["phase"] = "Bound"
                return

    def _on_PersistentVolumeClaim(self, o):
        o["status"] = {"phase": "Pending"}
        pv = self.objs.get((rr.K_PV, None, o["spec"].get("volumeName")))
        ref = ((pv or {}).get("spec") or {}).get("claimRef")
        mine = ref is None or (ref.get("namespace"), ref.get("name")) == (o["metadata"]["namespace"],
                                                                         o["metadata"]["name"])
        if "bind" not in self.freeze and pv is not None and mine:
            self.at(1, lambda: o["status"].update(phase="Bound"))

    def _on_VirtualMachine(self, o):
        self._run(o["metadata"]["namespace"], o["metadata"]["name"], o["spec"].get("runStrategy"))

    def _run(self, ns, name, strategy):
        if strategy == "Halted":
            if "stop" not in self.freeze:
                self.at(2, lambda: self.objs.pop((rr.K_VMI, ns, name), None))
        elif strategy in ("Always", "RerunOnFailure") and "start" not in self.freeze:
            self.at(3, lambda: self.put(rr.K_VMI, {"metadata": {"name": name, "namespace": ns},
                                                   "status": {"phase": "Running"}}))


# ---------------------------------------------------------------------------
# Les deux sites du banc
# ---------------------------------------------------------------------------

def _vm(name, uid, disks, running=True, secret=None, mac="52:54:00:00:00:01"):
    volumes = [{"name": d, "persistentVolumeClaim": {"claimName": f"{name}-{d}"}} for d in disks]
    if secret:
        volumes.append({"name": "cloudinit", "cloudInitNoCloud": {"secretRef": {"name": secret}}})
    return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
            "metadata": {"name": name, "namespace": "default", "uid": uid, "generation": 2},
            "spec": {"runStrategy": "Always" if running else "Halted", "template": {
                "metadata": {"labels": {"harvesterhci.io/vmName": name}},
                "spec": {"domain": {"firmware": {"uuid": f"fw-{uid}"},
                                    "devices": {"disks": [{"name": d, "disk": {"bus": "virtio"}} for d in disks],
                                                "interfaces": [{"name": "nic-1", "bridge": {}, "model": "virtio",
                                                                "macAddress": mac}]}},
                         "networks": [{"name": "nic-1", "multus": {"networkName": "default/prod"}}],
                         "volumes": volumes}}}}


VMS = {
    # une VM à disque d'image (backing image absente de la cible) et disque de données
    "vm-a": {"uid": "a1b2c3d4-0000-0000-0000-00000000000a", "disks": {"root": (1 * GIB, "vmi-img1"),
                                                                      "data": (2 * GIB, None)},
             "secret": "vm-a-cloudinit", "mac": "52:54:00:00:00:0a"},
    # une VM à trois disques
    "vm-b": {"uid": "b1b2c3d4-0000-0000-0000-00000000000b", "disks": {"d1": (2 * GIB, None), "d2": (2 * GIB, None),
                                                                      "d3": (2 * GIB, None)},
             "secret": None, "mac": "52:54:00:00:00:0b"},
    # une VM arrêtée
    "vm-c": {"uid": "c1b2c3d4-0000-0000-0000-00000000000c", "disks": {"data": (1 * GIB, None)},
             "secret": None, "mac": "52:54:00:00:00:0c", "stopped": True},
}


def _common(k, version):
    k.put(rr.K_SETTING, {"metadata": {"name": "backup-target"}, "value": BT_VALUE})
    k.put(rr.K_LH_TARGET, {"metadata": {"name": "default", "namespace": rr.NS}, "status": {"available": True}})
    k.put(rr.K_LH_SETTING, {"metadata": {"name": "current-longhorn-version", "namespace": rr.NS}, "value": version})
    k.put(rr.K_NS, {"metadata": {"name": "default"}})
    k.put(rr.K_SC, {"metadata": {"name": "harvester-longhorn",
                                 "annotations": {"storageclass.kubernetes.io/is-default-class": "true"}},
                    "provisioner": rp.LONGHORN_DRIVER,
                    "parameters": {"numberOfReplicas": "1", "staleReplicaTimeout": "30"}})
    k.put(rr.K_LH_NODE, {"metadata": {"name": "n1", "namespace": rr.NS},
                         "spec": {"disks": {"d": {"allowScheduling": True, "storageReserved": 0}}},
                         "status": {"diskStatus": {"d": {"conditions": [{"type": "Schedulable", "status": "True"},
                                                                         {"type": "Ready", "status": "True"}],
                                                         "storageMaximum": 1000 * GIB, "storageScheduled": 0,
                                                         "storageAvailable": 1000 * GIB}}}})


def seed_source(k, names):
    _common(k, "v1.11.2")
    k.put(rr.K_SC, {"metadata": {"name": "lh-img1"}, "provisioner": rp.LONGHORN_DRIVER,
                    "parameters": {"numberOfReplicas": "1", "backingImage": "vmi-img1"}})
    k.put(rr.K_LH_BI, {"metadata": {"name": "vmi-img1", "namespace": rr.NS},
                       "spec": {"checksum": "abc123"}, "status": {"size": 21_700_000}})
    # une tâche de l'exploitant dans le groupe `default` (appartenance implicite des volumes)
    k.put(rr.K_LH_JOB, {"metadata": {"name": "daily", "namespace": rr.NS},
                        "spec": {"task": "backup", "groups": ["default"], "cron": "0 3 * * *", "retain": 7}})
    for name in names:
        v = VMS[name]
        k.put(rr.K_VM, _vm(name, v["uid"], list(v["disks"]), not v.get("stopped"), v["secret"], v["mac"]))
        if not v.get("stopped"):
            k.put(rr.K_VMI, {"metadata": {"name": name, "namespace": "default"}, "status": {"phase": "Running"}})
        if v["secret"]:
            k.put(rr.K_SECRET, {"metadata": {"name": v["secret"], "namespace": "default"}, "type": "Opaque",
                                "data": {"userdata": base64.b64encode(b"#cloud-config\n").decode()}})
        for d, (size, bi) in v["disks"].items():
            pv = f"pvc-{name}-{d}"
            k.put(rr.K_PVC, {"metadata": {"name": f"{name}-{d}", "namespace": "default",
                                          "annotations": {"harvesterhci.io/imageId": "default/img1"} if bi else {}},
                             "spec": {"volumeName": pv, "storageClassName": "lh-img1" if bi else "harvester-longhorn",
                                      "accessModes": ["ReadWriteMany"], "volumeMode": "Block",
                                      "resources": {"requests": {"storage": rp.quantity(size)}}},
                             "status": {"phase": "Bound"}})
            k.put(rr.K_PV, {"metadata": {"name": pv}, "spec": {"csi": {"driver": rp.LONGHORN_DRIVER,
                                                                        "volumeHandle": pv}}})
            spec = {"size": str(size), "accessMode": "rwx", "migratable": True, "dataEngine": "v1",
                    "numberOfReplicas": 3}
            if bi:
                spec["backingImage"] = bi
            k.put(rr.K_LH_VOLUME, {"metadata": {"name": pv, "namespace": rr.NS, "labels": {}}, "spec": spec,
                                   "status": {"state": "attached"}})


def seed_target(k):
    _common(k, "v1.12.1")
    k.put(rr.K_NAD, {"metadata": {"name": "lab-net", "namespace": "default"}})
    k.put(rr.K_NAD, {"metadata": {"name": "prod", "namespace": "default"}})


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def protected(tmp_path, **kw):
    w = World(tmp_path, **kw)
    res = rr.protect(w.ctx())
    assert res["exit"] == 0, res
    return w


def events(w, **filt):
    rows = w.conn.execute("SELECT * FROM events WHERE plan_id = ? ORDER BY id", (w.pid,)).fetchall()
    out = [dict(r) for r in rows]
    for k, v in filt.items():
        out = [e for e in out if e[k] == v]
    return out


def mut_keys(cluster, op=None, kind=None):
    return [c for c in cluster.mutations() if (op is None or c[0] == op) and (kind is None or c[1] == kind)]


def snapshot_state(w):
    """Ce qui compte de l'état final, pour comparer deux déroulés."""
    def objs(k):
        out = {}
        for key, o in k.objs.items():
            if key[0] in (rr.K_LH_ENGINE, rr.K_LH_BV, rr.K_LH_BBI, rr.K_VMI):
                continue
            labels = (o.get("metadata") or {}).get("labels") or {}
            if labels.get(rp.LABEL_PLAN) or key[0] in (rr.K_LH_VOLUME, rr.K_LH_JOB, rr.K_VM):
                out[key] = (json.dumps(labels, sort_keys=True), json.dumps(o.get("spec"), sort_keys=True,
                                                                           default=str)[:400])
        return out
    vols = sorted((v["vm_uid"], v["disk"], v["state"], v["target_volume"]) for v in db.list_volumes(w.conn, w.pid))
    res = sorted((r["resource_uid"], r["state"]) for r in db.list_resources(w.conn, w.pid))
    return {"src": objs(w.src), "dst": objs(w.dst), "vols": vols, "res": res, "state": w.plan()["state"]}


# ---------------------------------------------------------------------------
# Petites briques
# ---------------------------------------------------------------------------

def test_kube_create_and_patch_take_a_server_dry_run(monkeypatch):
    import kube
    seen = []
    monkeypatch.setattr(kube.Kube, "run", lambda self, *a, **k: seen.append(a) or "{}")
    k = kube.Kube("/nowhere")
    k.create({"kind": "X"})
    k.create({"kind": "X"}, dry_run=True)
    k.patch("vm", "ns", "n", {"a": 1})
    k.patch("vm", "ns", "n", {"a": 1}, dry_run=True)
    assert ["--dry-run=server" in a for a in seen] == [False, True, False, True]


@pytest.mark.parametrize("plan_opts,overrides,expect", [
    ({}, {}, {"probe": 15, "activate": 300, "barrier": 90, "start": 900, "undo": 300}),
    ({"timeouts": {"activate": 60}}, {}, {"activate": 60}),
    ({"timeouts": {"activate": 60}}, {"activate": 30}, {"activate": 30}),
    ({"timeouts": {"vm_start": 120}}, {}, {"start": 120, "vm_start": 120}),
    ({"timeouts": {"barrier": 40}}, {}, {"barrier": 40}),
])
def test_effective_timeouts(plan_opts, overrides, expect):
    t = rr.effective_timeouts({"interval_s": 180, "options": plan_opts}, overrides)
    for k, v in expect.items():
        assert t[k] == v


@pytest.mark.parametrize("url,vol", [
    (f"{BT_URL}?backup=backup-1&volume=pvc-x", "pvc-x"),
    ("nfs://h:/p/?volume=pvc-y&backup=b", "pvc-y"),
    ("", ""), ("nfs://h:/p/", ""),
])
def test_url_volume(url, vol):
    assert rr.url_volume(url) == vol


def test_op_name_is_deterministic_and_run_scoped():
    a = rr.op_name("rp-0123456789ab", "pvc-1", 1, "final", "7")
    assert a == rr.op_name("rp-0123456789ab", "pvc-1", 1, "final", "7")
    assert a != rr.op_name("rp-0123456789ab", "pvc-1", 1, "final", "8")
    assert a != rr.op_name("rp-0123456789ab", "pvc-1", 2, "final", "7")
    assert rp.NAME_RE.match(a) and len(a) <= rp.VOLUME_NAME_MAX


@pytest.mark.parametrize("params,same", [
    ({"diskSelector": "ssd,fast", "nodeSelector": ""}, {"diskSelector": "fast, ssd"}),
    ({}, {"numberOfReplicas": "3"}),
])
def test_class_pool_groups_classes_by_selectors(params, same):
    assert rr.class_pool({"parameters": params}) == rr.class_pool({"parameters": same})
    assert rr.class_pool({"parameters": {"diskSelector": "hdd"}}) != rr.class_pool({"parameters": params})


def test_carnet_round_trip_and_digest():
    w = {"uid": "u1", "namespace": "default", "name": "vm", "tns": "dr", "tname": "vm2"}
    m = rr.carnet_manifest("rp-0123456789ab", 1, w, {"kind": "VirtualMachine"}, [{"name": "s"}],
                           [{"target_volume": "hr-x"}], "harvlab")
    assert m["metadata"]["namespace"] == rp.CARNET_NAMESPACE
    assert m["metadata"]["labels"] == rp.plan_labels("rp-0123456789ab", 1, "u1")
    c = rr.read_carnet(m)
    assert c["meta"]["target_namespace"] == "dr" and c["volumes"] == [{"target_volume": "hr-x"}]
    m2 = rr.carnet_manifest("rp-0123456789ab", 1, w, {"kind": "VirtualMachine", "x": 1}, [{"name": "s"}],
                            [{"target_volume": "hr-x"}], "harvlab")
    assert m2["metadata"]["annotations"] != m["metadata"]["annotations"]


# ---------------------------------------------------------------------------
# Protection
# ---------------------------------------------------------------------------

def test_protect_two_vms_creates_everything_with_our_labels(world):
    w = world
    res = rr.protect(w.ctx())
    assert res["exit"] == 0, res
    assert w.plan()["state"] == "protected" and w.plan()["interval_s"] == 180
    pid = w.pid
    # source : une tâche par VM, concurrence = disques, étiquettes du plan
    for name, n in (("vm-a", 2), ("vm-b", 3)):
        uid = VMS[name]["uid"]
        job = w.src.obj(rr.K_LH_JOB, rr.NS, rp.recurring_job_name(pid, uid))
        assert job["spec"]["concurrency"] == n and job["spec"]["cron"] == "*/3 * * * *"
        assert job["metadata"]["labels"] == rp.plan_labels(pid, 1, uid)
        for d in VMS[name]["disks"]:
            labels = w.src.obj(rr.K_LH_VOLUME, rr.NS, f"pvc-{name}-{d}")["metadata"]["labels"]
            assert labels[rp.job_label(job["metadata"]["name"])[0]] == "enabled"
            # appartenance implicite au groupe `default` rendue explicite (tâche `daily`)
            assert labels["recurring-job-group.longhorn.io/default"] == "enabled"
    # cible : volumes de secours, backing image restaurée, carnets
    for name in ("vm-a", "vm-b"):
        uid = VMS[name]["uid"]
        for d, (size, bi) in VMS[name]["disks"].items():
            sb = rp.target_volume_name(pid, uid, d, 1, name)
            v = w.dst.obj(rr.K_LH_VOLUME, rr.NS, sb)
            assert v["spec"]["Standby"] is True and v["spec"]["frontend"] == ""
            assert rr.url_volume(v["spec"]["fromBackup"]) == f"pvc-{name}-{d}"
            assert v["metadata"]["labels"] == rp.plan_labels(pid, 1, uid)
            assert v["spec"].get("backingImage") == bi and v["spec"]["size"] == str(size)
    bi = w.dst.obj(rr.K_LH_BI, rr.NS, "vmi-img1")
    assert bi["spec"]["sourceType"] == "restore" and bi["spec"]["checksum"] == "abc123"
    assert bi["metadata"]["labels"] == rp.plan_labels(pid, 1)
    carnet = w.dst.obj(rr.K_SECRET, rp.CARNET_NAMESPACE, rp.carnet_name(pid, VMS["vm-a"]["uid"]))
    c = rr.read_carnet(carnet)
    assert c["vm"]["spec"]["template"]["spec"]["networks"][0]["multus"]["networkName"] == "default/lab-net"
    assert c["vm"]["spec"]["template"]["spec"]["domain"]["firmware"]["uuid"] == f"fw-{VMS['vm-a']['uid']}"
    assert [s["name"] for s in c["secrets"]] == ["vm-a-cloudinit"]
    assert {v["disk"] for v in c["volumes"]} == {"root", "data"}
    # registre : points appliqués, volumes en attente, ressources protégées
    ticks = db.list_ticks(w.conn, pid)
    assert len(ticks) == 5 and all(t["state"] == "applied" for t in ticks)
    assert {v["state"] for v in db.list_volumes(w.conn, pid)} == {"standby"}
    assert all(v["backup_volume_target"] for v in db.list_volumes(w.conn, pid))
    assert {r["state"] for r in db.list_resources(w.conn, pid)} == {"protected"}
    r = db.get_resource(w.conn, pid, VMS["vm-a"]["uid"])
    assert r["vm_generation"] == 2 and r["vm_spec_digest"]
    # rien sur la cible sans nos étiquettes, sauf le namespace commun et les relectures
    for c in mut_keys(w.dst):
        assert c[1] in (rr.K_LH_VOLUME, rr.K_LH_BI, rr.K_SECRET, rr.K_NS, rr.K_LH_BV, rr.K_LH_TARGET)


def test_protect_journal_has_start_and_done_for_each_resource(world):
    w = world
    rr.protect(w.ctx())
    for uid in (VMS["vm-a"]["uid"], VMS["vm-b"]["uid"]):
        for step in ("recurring-job", "first-backup", "resync", "standby-volume", "initial-restore", "carnet"):
            codes = [e["code"] for e in events(w, resource_uid=uid, step=step)]
            assert codes == ["start", "done"], (uid, step, codes)
    bi = events(w, step="backing-image")
    assert {e["resource_uid"] for e in bi} == {VMS["vm-a"]["uid"]}
    assert all(e["host"] == "harvlab2" for e in events(w, step="standby-volume"))
    assert all(e["host"] == "harvlab" for e in events(w, step="recurring-job"))


def test_protect_twice_creates_nothing_new(tmp_path):
    w = protected(tmp_path)
    before = (len(mut_keys(w.src, "create")), len(mut_keys(w.dst, "create")))
    res = rr.protect(w.ctx())
    assert res["exit"] == 0
    assert (len(mut_keys(w.src, "create")), len(mut_keys(w.dst, "create"))) == before


def test_protect_refused_before_any_write_when_a_target_volume_is_taken(world):
    w = world
    sb = rp.target_volume_name(w.pid, VMS["vm-b"]["uid"], "d2", 1, "vm-b")
    w.dst.put(rr.K_LH_VOLUME, {"metadata": {"name": sb, "namespace": rr.NS, "labels": {"owner": "someone"}},
                               "spec": {}, "status": {}})
    res = rr.protect(w.ctx())
    assert res["exit"] == 2 and "target-volume-taken" in [f["code"] for f in res["findings"]]
    assert mut_keys(w.src) == [] and mut_keys(w.dst) == []
    assert w.plan()["state"] == "draft"
    assert events(w, step="check", resource_uid=VMS["vm-b"]["uid"])[0]["level"] == "warn"


def test_protect_pvc_present_warns_only(world):
    w = world
    w.dst.put(rr.K_PVC, {"metadata": {"name": "vm-a-data", "namespace": "default"}, "spec": {}})
    res = rr.protect(w.ctx())
    assert res["exit"] == 0
    assert "target-pvc-taken" in [f["code"] for f in res["findings"] if f["level"] == "warn"]


def test_protect_two_classes_on_the_same_disks_do_not_warn_a_shared_pool(tmp_path):
    w = World(tmp_path, mappings={"networks": {"default/prod": "default/lab-net"},
                                  "storage_classes": {"harvester-longhorn": "fast"}})
    w.dst.put(rr.K_SC, {"metadata": {"name": "fast"}, "provisioner": rp.LONGHORN_DRIVER,
                        "parameters": {"numberOfReplicas": "1"}})
    res = rr.protect(w.ctx(), dry_run=True)
    codes = [f["code"] for f in res["findings"]]
    # vm-a : disque d'image vers la classe par défaut, disque de données vers `fast`
    assert "capacity-shared-pool" not in codes, codes


@pytest.mark.parametrize("image,expected_class,expected_base", [
    # classe de base dans la spec de l'image (mutateur de Harvester)
    ({"spec": {"targetStorageClassName": "harvester-longhorn"}}, "fast", "harvester-longhorn"),
    # classe de base dans l'annotation seule
    ({"metadata": {"annotations": {"harvesterhci.io/storageClassName": "harvester-longhorn"}}},
     "fast", "harvester-longhorn"),
    # image absente : classe par défaut de la cible, comme avant
    (None, "harvester-longhorn", None),
])
def test_image_disk_without_image_mapping_takes_the_mapped_base_class(tmp_path, image, expected_class,
                                                                       expected_base):
    # vu sur le banc (v1.99.0) : un disque d'image sans correspondance d'image
    # tombait sur la classe par défaut de la cible sans passer par la
    # correspondance des classes du plan
    w = World(tmp_path, mappings={"networks": {"default/prod": "default/lab-net"},
                                  "storage_classes": {"harvester-longhorn": "fast"}})
    w.dst.put(rr.K_SC, {"metadata": {"name": "fast"}, "provisioner": rp.LONGHORN_DRIVER,
                        "parameters": {"numberOfReplicas": "1"}})
    if image is not None:
        obj = {"metadata": {"name": "img1", "namespace": "default"}, "spec": {}}
        obj["metadata"].update(image.get("metadata") or {})
        obj["spec"].update(image.get("spec") or {})
        w.src.put(rr.K_IMAGE, obj)
    ctx = w.ctx()
    src = rr.collect_source(ctx)
    dst = rr.collect_target(ctx, ["default"])
    disks = [d for vm in src["vms"] for d in vm["disks"] if d["image"]]
    assert disks and {d["image_class"] for d in disks} == {expected_base}
    works = rr.build_works(ctx, src, dst)
    assert {d["target_class"] for wk in works for d in wk["disks"] if d["image"]} == {expected_class}


@pytest.mark.parametrize("size,label", [
    # taille des données de la sauvegarde (Longhorn `Backup.status.size`)
    ("25174351", "initial-restore (backup data)"),
    # sans taille publiée : taille nominale, dite comme telle
    (None, "initial-restore (volume size)"),
])
def test_initial_restore_reports_backup_data_not_the_disk_size(tmp_path, size, label):
    # vu sur le banc (v1.99.0) : « 5.0 GiB en 14 s » pour environ 150 Mo restaurés
    w = World(tmp_path)
    if size is None:
        real = w.src.complete_backup

        def no_size(o, vol, snap_at, newly=25_174_351):
            real(o, vol, snap_at, newly)
            o["status"].pop("size", None)
        w.src.complete_backup = no_size
    snaps = []
    ctx = w.ctx(progress=lambda snap: snaps.append(snap))
    assert rr.protect(ctx)["exit"] == 0
    disks = db.list_volumes(w.conn, w.pid)
    nominal = sum(VMS[n]["disks"][d][0] for n in VMS for d in VMS[n]["disks"]
                  if VMS[n]["uid"] in {v["vm_uid"] for v in disks})
    expected = len(disks) * 25_174_351 if size else nominal
    lines = [a[2] for a in w.log if a[0] == "initial-restore" and a[1] == "running"]
    assert lines and lines[-1].startswith(f"{label}: {vp.fmt_bytes(expected)} in ")
    restore = [x for x in snaps if x.get("phase") == "initial-restore"]
    assert restore and restore[-1]["total"] == expected


def test_protect_dry_run_writes_nothing_and_defers_what_protect_creates_first(world):
    # vu sur le banc (harvlab -> harvlab2) : avant la première protection, le
    # namespace des carnets et la backing image à restaurer n'existent pas ;
    # le dry-run des objets qui en dépendent échouait toujours (exit 2)
    w = world
    res = rr.protect(w.ctx(), dry_run=True)
    assert mut_keys(w.src) == [] and mut_keys(w.dst) == []
    assert res["exit"] == 0, [d for d in res["dry_run"] if not d["ok"]]
    assert all(d["ok"] for d in res["dry_run"])
    sv = {d["name"]: d for d in res["dry_run"] if d["object"] == "standby-volume"}
    assert all(d.get("placeholder_url") for d in sv.values())
    # aucune sauvegarde encore : Longhorn refuse un `fromBackup` d'essai, rien n'est tenté
    assert sorted(d["deferred"] for d in sv.values()).count("backing-image") == 1
    assert {d["deferred"] for d in sv.values()} == {"backing-image", "first-backup"}
    assert not [c for c in w.dst.calls if c[1] == rr.K_LH_VOLUME and c[0] == "create"]
    assert [d["deferred"] for d in res["dry_run"] if d["object"] == "carnet"] == ["namespace", "namespace"]
    assert ("create", rr.K_NS, None, rp.CARNET_NAMESPACE, "dry") in w.dst.calls
    assert not [c for c in w.dst.calls if c[1] == rr.K_SECRET and c[0] == "create"]
    # la backing image absente de la cible passe aussi au dry-run serveur
    bi = [d for d in res["dry_run"] if d["object"] == "backing-image"]
    assert [(d["name"], d["ok"], d["placeholder_url"]) for d in bi] == [("vmi-img1", True, True)]
    assert ("create", rr.K_LH_BI, rr.NS, "vmi-img1", "dry") in w.dst.calls
    assert w.plan()["state"] == "draft"


def test_protect_without_interval_nor_measure_is_refused(tmp_path):
    w = World(tmp_path, interval=None)
    res = rr.protect(w.ctx())
    assert res["exit"] == 2 and res["code"] == "interval-required"
    assert mut_keys(w.src) == []


def test_protect_given_interval_wins_and_is_stored(world):
    res = rr.protect(world.ctx(), interval="5m")
    assert res["exit"] == 0 and world.plan()["interval_s"] == 300 and world.plan()["cron"] == "*/5 * * * *"


def test_protect_locked_by_a_live_command(world):
    w = world
    db.acquire_lock(w.conn, w.pid, "failover", "cli:otherhost:4242", ttl=600)
    res = rr.protect(w.ctx())
    assert res["exit"] == 2 and res["code"] == "locked"
    assert mut_keys(w.src) == []


def test_long_waits_renew_the_lock(world):
    w = world
    w.dst.restore_s = 900                     # restauration initiale de 15 min
    c = w.ctx()
    beats = []
    orig = db.lock_heartbeat
    db.lock_heartbeat = lambda *a, **k: beats.append(1) or orig(*a, **k)
    try:
        assert rr.protect(c)["exit"] == 0
    finally:
        db.lock_heartbeat = orig
    assert len(beats) >= 4


def _clean_mutation_count(tmp_path, fn):
    w = protected(tmp_path) if fn != "protect" else World(tmp_path)
    w.mutations = 0
    _run(w, fn)
    return w.mutations


def _run(w, fn, **kw):
    if fn == "protect":
        return rr.protect(w.ctx(), **kw)
    if fn == "failover":
        return rr.failover(w.ctx(), **kw)
    if fn == "unprotect":
        return rr.unprotect(w.ctx(), **kw)
    raise AssertionError(fn)


def test_protect_resumes_after_a_crash_at_every_step(tmp_path):
    """Arrêt brutal à chaque écriture possible, puis relance : même état
    final qu'un déroulé sans incident, et aucun doublon (le faux refuse une
    création en double)."""
    ref = World(tmp_path)
    assert rr.protect(ref.ctx())["exit"] == 0
    expected = snapshot_state(ref)
    n = ref.mutations
    assert n > 20
    for k in range(1, n + 1):
        w = World(tmp_path)
        w.crash_at = k
        with pytest.raises(Crash):
            rr.protect(w.ctx())
        w.crash_at = None
        assert db.get_lock(w.conn, w.pid) is not None          # kill -9 : verrou resté
        res = rr.resume(w.ctx())                                 # autre PID : verrou repris
        assert res["exit"] == 0, (k, res)
        got = snapshot_state(w)
        assert got == expected, k
        dup = [c for c in w.src.mutations() + w.dst.mutations() if c[0] == "create"]
        assert len(dup) == len({(c[1], c[2], c[3]) for c in dup}), k


def test_protect_cancel_undoes_only_what_this_run_created(world):
    w = world
    foreign = {"metadata": {"name": "keep-me", "namespace": rr.NS, "labels": {}}, "spec": {"x": 1}}
    w.dst.put(rr.K_LH_VOLUME, foreign)
    first_standby = [None]

    def hook(name):
        if first_standby[0] is None and name.startswith("hr-"):
            first_standby[0] = name
            raise rr.Cancelled("signal 15")
    w.dst.hooks[("create", rr.K_LH_VOLUME)] = hook
    res = rr.protect(w.ctx())
    assert res["exit"] == 3 and w.plan()["state"] == "error"
    assert db.error_origin(w.conn, w.pid) == "protecting"
    # les sauvegardes défaites disparaissent de la cible à sa relecture suivante
    w.clock.sleep(200)
    w.dst.list(rr.K_LH_BACKUP)
    assert w.owned(w.src) == [] and w.owned(w.dst) == []
    for name in ("vm-a", "vm-b"):
        for d in VMS[name]["disks"]:
            assert w.src.obj(rr.K_LH_VOLUME, rr.NS, f"pvc-{name}-{d}")["metadata"]["labels"] == {}
    assert w.dst.obj(rr.K_LH_VOLUME, rr.NS, "keep-me")["spec"] == {"x": 1}
    assert db.get_lock(w.conn, w.pid) is None
    # et la relance part de l'erreur
    w.dst.hooks.clear()
    assert rr.protect(w.ctx())["exit"] == 0 and w.plan()["state"] == "protected"


def test_protect_failure_keeps_objects_for_the_rerun(world):
    w = world
    w.dst.freeze.add("apply")                 # le moteur n'applique rien
    res = rr.protect(w.ctx(timeouts={"initial_restore": 120}))
    assert res["exit"] == 1 and res["code"] == "timeout"
    assert w.plan()["state"] == "error"
    assert len([k for k in w.owned(w.dst) if k[0] == rr.K_LH_VOLUME]) == 5
    errs = events(w, step="initial-restore", code="timeout")
    assert {e["resource_uid"] for e in errs} == {VMS["vm-a"]["uid"], VMS["vm-b"]["uid"]}
    w.dst.freeze.clear()
    creates = len(mut_keys(w.dst, "create"))
    assert rr.protect(w.ctx())["exit"] == 0
    # seuls les carnets (et leur namespace) restaient à créer
    assert sorted(c[1] for c in mut_keys(w.dst, "create")[creates:]) == [rr.K_NS, rr.K_SECRET, rr.K_SECRET]


@pytest.mark.parametrize("cluster,freeze,timeout,step", [
    ("src", "snapshot", "snapshot", "first-backup"),
    ("src", "backup", "final_backup", "first-backup"),
    ("dst", "backing-image", "backing_image", "backing-image"),
    ("dst", "resync", "resync_apply", "resync"),
    ("dst", "apply", "initial_restore", "initial-restore"),
])
def test_protect_every_wait_is_bounded(tmp_path, cluster, freeze, timeout, step):
    w = World(tmp_path)
    getattr(w, cluster).freeze.update({freeze, "natural-sync"})
    t0 = w.clock.now()
    res = rr.protect(w.ctx(timeouts={timeout: 100}))
    assert res["exit"] == 1 and res["code"] == "timeout", res
    assert w.clock.now() - t0 < 100 + 60
    assert events(w, step=step, code="timeout")


def test_api_blip_during_a_wait_does_not_fail(world):
    w = world
    w.dst.restore_s = 60
    w.clock_t0 = w.clock.now()

    def down_then_up(_name):
        if not getattr(w, "_blipped", False):
            w._blipped = True
            w.dst.down = True
            w.dst.at(20, lambda: setattr(w.dst, "down", False))
    w.dst.hooks[("list", rr.K_LH_ENGINE)] = down_then_up
    assert rr.protect(w.ctx())["exit"] == 0
    assert w._blipped


def test_an_owned_standby_volume_restoring_another_volume_is_refused(world):
    w = world
    sb = rp.target_volume_name(w.pid, VMS["vm-b"]["uid"], "d1", 1, "vm-b")
    w.dst.put(rr.K_LH_VOLUME, {"metadata": {"name": sb, "namespace": rr.NS,
                                            "labels": rp.plan_labels(w.pid, 1, VMS["vm-b"]["uid"])},
                               "spec": {"Standby": True, "fromBackup": f"{BT_URL}?backup=b&volume=pvc-other"},
                               "status": {}})
    res = rr.protect(w.ctx())
    assert res["exit"] == 1 and res["code"] == "standby-mismatch"
    assert events(w, step="standby-volume", code="standby-mismatch")


def test_default_group_label_only_where_membership_was_implicit(tmp_path):
    w = World(tmp_path, vms=("vm-b",))
    lv = w.src.obj(rr.K_LH_VOLUME, rr.NS, "pvc-vm-b-d2")
    lv["metadata"]["labels"] = {"recurring-job-group.longhorn.io/gold": "enabled"}
    assert rr.protect(w.ctx())["exit"] == 0
    l1 = w.src.obj(rr.K_LH_VOLUME, rr.NS, "pvc-vm-b-d1")["metadata"]["labels"]
    l2 = w.src.obj(rr.K_LH_VOLUME, rr.NS, "pvc-vm-b-d2")["metadata"]["labels"]
    assert l1.get("recurring-job-group.longhorn.io/default") == "enabled"
    assert "recurring-job-group.longhorn.io/default" not in l2


def test_an_object_without_our_labels_is_never_touched(world):
    """Course : un volume au nom du volume de secours apparaît entre le
    contrôle et la création. La protection s'arrête sans le modifier ni le
    supprimer."""
    w = world
    sb = rp.target_volume_name(w.pid, VMS["vm-b"]["uid"], "d1", 1, "vm-b")
    intruder = {"metadata": {"name": sb, "namespace": rr.NS, "labels": {"app": "other"}},
                "spec": {"fromBackup": "x"}, "status": {}}

    def race(name):
        if name == sb and w.dst.obj(rr.K_LH_VOLUME, rr.NS, sb) is None:
            w.dst.put(rr.K_LH_VOLUME, intruder)
    w.dst.hooks[("get", rr.K_LH_VOLUME)] = race
    res = rr.protect(w.ctx())
    assert res["exit"] == 1 and res["code"] == "target-volume-taken"
    o = w.dst.obj(rr.K_LH_VOLUME, rr.NS, sb)
    assert o["metadata"]["labels"] == {"app": "other"} and o["spec"] == {"fromBackup": "x"}
    assert not [c for c in w.dst.calls if c[3] == sb and c[0] in ("patch", "delete", "replace")]
    # la déprotection ne le touche pas davantage
    rr.unprotect(w.ctx())
    assert w.dst.obj(rr.K_LH_VOLUME, rr.NS, sb)["spec"] == {"fromBackup": "x"}


# ---------------------------------------------------------------------------
# Mesure
# ---------------------------------------------------------------------------

def test_measure_writes_nothing_on_the_target_and_hands_its_job_to_protect(world):
    w = world
    res = rr.measure_start(w.ctx(), "1m")
    assert res["exit"] == 0 and w.plan()["state"] == "measuring"
    assert mut_keys(w.dst) == []
    job = rp.recurring_job_name(w.pid, VMS["vm-a"]["uid"])
    assert w.src.obj(rr.K_LH_JOB, rr.NS, job)["spec"]["cron"] == "*/1 * * * *"
    # deux passages de la tâche (le premier complet), puis la protection
    for i, vol in enumerate(f"pvc-{n}-{d}" for n in ("vm-a", "vm-b") for d in VMS[n]["disks"]):
        w.src.make_backup(vol, f"backup-m0-{i}", newly=2 * GIB)
    w.clock.sleep(60)
    for i, vol in enumerate(f"pvc-{n}-{d}" for n in ("vm-a", "vm-b") for d in VMS[n]["disks"]):
        w.src.make_backup(vol, f"backup-m1-{i}", newly=25_174_351)
    creates = len(mut_keys(w.src, "create"))
    assert rr.protect(w.ctx())["exit"] == 0
    assert w.src.obj(rr.K_LH_JOB, rr.NS, job)["spec"]["cron"] == w.plan()["cron"]
    # même tâche (patch du cron), et aucune première sauvegarde : le BackupVolume sert déjà
    new = mut_keys(w.src, "create")[creates:]
    assert [c for c in new if c[1] in (rr.K_LH_JOB, rr.K_LH_SNAPSHOT, rr.K_LH_BACKUP)] == []
    assert ("patch", rr.K_LH_JOB, rr.NS, job) in [c[:4] for c in mut_keys(w.src, "patch")]


def test_measure_stop_records_passes_and_removes_jobs(world):
    w = world
    rr.measure_start(w.ctx(), "3m")
    w.src.make_backup("pvc-vm-a-data", "backup-x1", newly=2 * GIB)
    res = rr.measure_stop(w.ctx())
    assert res["exit"] == 0 and res["passes"] == 1 and w.plan()["state"] == "draft"
    assert w.owned(w.src) == []
    key = rp.job_label(rp.recurring_job_name(w.pid, VMS["vm-a"]["uid"]))[0]
    assert key not in w.src.obj(rr.K_LH_VOLUME, rr.NS, "pvc-vm-a-data")["metadata"]["labels"]
    assert {r["state"] for r in db.list_resources(w.conn, w.pid)} == {"draft"}


def _measures(w, step, per_pass, passes, first=2 * GIB, secs=5):
    vols = db.list_volumes(w.conn, w.pid)
    t = 1_792_000_000
    for v in vols:
        for i in range(passes + 1):
            at = t + i * step
            db.record_measure(w.conn, w.pid, {
                "generation": 1, "vm_uid": v["vm_uid"], "source_volume": v["source_volume"], "backup": f"b{i}",
                "snapshot_at": iso(at), "backup_started_at": iso(at + 1),
                "backup_done_at": iso(at + 1 + (30 if i == 0 else secs)),
                "newly_uploaded": first if i == 0 else per_pass, "state": "completed"})


def test_measure_report_with_the_bench_numbers(world):
    w = world
    rr.measure_start(w.ctx(), "3m")
    _measures(w, 180, 25_174_351, 6)
    rep = rr.measure_report(w.conn, w.pid)
    assert rep["measured_step_s"] == 180 and rep["assumption"] == "apply-equals-send"
    vm = next(v for v in rep["vms"] if v["vm_uid"] == VMS["vm-a"]["uid"])
    assert vm["passes"] == 6 and vm["throughput_bps"] > 0 and vm["first_copy_eta_s"] > 0
    steps = {s["step_s"]: s for s in vm["steps"]}
    assert set(steps) == {s for s in rp.CRON_STEPS if s % 180 == 0}
    assert steps[180]["p95_bytes"] == 2 * 25_174_351              # deux disques par passage
    assert steps[360]["upper_bytes"] == 4 * 25_174_351
    assert rep["recommended_interval_s"] in rp.CRON_STEPS and rep["tenable"] is True
    assert rep["rpo_s"] <= w.plan()["rpo_objective_s"]


def test_measure_report_without_passes_is_insufficient(world):
    rr.measure_start(world.ctx(), "3m")
    rep = rr.measure_report(world.conn, world.pid)
    assert rep["recommended_interval_s"] is None and all(v.get("insufficient") for v in rep["vms"])


def test_measure_formula_bench_numbers():
    f = rr.measure_formula(2 * GIB, 600, 180, throughput=72_000_000)
    assert round(f["forecast"]["scattered_estimate"]) == 454
    assert f["scattered_estimate"]["send_s"] == pytest.approx(454.06 * 2 * 1024 ** 2 / 72e6, rel=1e-3)
    seq = rr.measure_formula(2 * GIB, 3, 60, write_size=8 * 1024 ** 2)
    assert seq["forecast"]["sequential"] == 12 and "sequential" not in seq


def test_protect_uses_the_measured_interval(world):
    w = world
    rr.measure_start(w.ctx(), "3m")
    _measures(w, 180, 25_174_351, 6)
    rr.measure_stop(w.ctx())
    rep = rr.measure_report(w.conn, w.pid)
    assert rr.protect(w.ctx())["exit"] == 0
    assert w.plan()["interval_s"] == rep["recommended_interval_s"]
    assert not events(w, code="interval-without-measure")


# ---------------------------------------------------------------------------
# Bascule planifiée
# ---------------------------------------------------------------------------

def _final_names(w, run_id):
    return {f"pvc-{n}-{d}": rr.op_name(w.pid, f"pvc-{n}-{d}", 1, "final", str(run_id))
            for n in ("vm-a", "vm-b") for d in VMS[n]["disks"]}


def test_planned_failover_applies_the_final_backup_before_activation(tmp_path):
    w = protected(tmp_path)
    w.clock.sleep(400)                        # la tâche a tourné, la VM a écrit
    res = rr.failover(w.ctx())
    assert res["exit"] == 0, res
    finals = _final_names(w, res["run_id"])
    for n in ("vm-a", "vm-b"):
        for d in VMS[n]["disks"]:
            sb = rp.target_volume_name(w.pid, VMS[n]["uid"], d, 1, n)
            v = w.dst.obj(rr.K_LH_VOLUME, rr.NS, sb)
            # zéro perte : le moteur avait appliqué LA sauvegarde finale à l'activation
            assert v["status"]["activatedFrom"] == finals[f"pvc-{n}-{d}"]
            assert v["spec"]["Standby"] is False and v["spec"]["frontend"] == "blockdev"
    assert w.plan()["state"] == "failed_over" and w.plan()["options"]["paused"] is False
    assert {r["state"] for r in db.list_resources(w.conn, w.pid)} == {"failed_over"}
    assert {v["state"] for v in db.list_volumes(w.conn, w.pid)} == {"activated"}
    vm = w.dst.obj(rr.K_VM, "default", "vm-a")
    assert vm["metadata"]["labels"][rp.LABEL_PLAN] == w.pid
    assert vm["spec"]["template"]["spec"]["domain"]["firmware"]["uuid"] == f"fw-{VMS['vm-a']['uid']}"
    assert w.dst.obj(rr.K_VMI, "default", "vm-a")["status"]["phase"] == "Running"
    sec = w.dst.obj(rr.K_SECRET, "default", "vm-a-cloudinit")
    assert sec["metadata"]["ownerReferences"][0]["name"] == "vm-a"
    pvc = w.dst.obj(rr.K_PVC, "default", "vm-a-root")
    assert pvc["spec"]["volumeName"] == rp.target_volume_name(w.pid, VMS["vm-a"]["uid"], "root", 1, "vm-a")
    assert w.dst.obj(rr.K_PV, None, pvc["spec"]["volumeName"])["spec"]["csi"]["driver"] == "driver.longhorn.io"
    src_vm = w.src.obj(rr.K_VM, "default", "vm-a")
    assert src_vm["spec"]["runStrategy"] == "Halted"
    assert src_vm["metadata"]["annotations"][rr.FAILED_OVER_TO] == "harvlab2/default/vm-a"
    assert rr.STOPPED_ANNOTATION not in src_vm["metadata"]["annotations"]
    run = db.list_runs(w.conn, w.pid)[-1]
    assert run["result"] == "ok" and run["points"]["mode"] == "planned"
    assert {s["step"] for s in run["steps"]} >= {"stop-source", "final-backup", "final-resync", "activate", "vm"}


def test_planned_failover_call_order(tmp_path):
    w = protected(tmp_path, vms=("vm-b",))
    n_src, n_dst = len(w.src.calls), len(w.dst.calls)
    assert rr.failover(w.ctx())["exit"] == 0
    seq = []
    for c in w.src.calls[n_src:]:
        if c[0] == "patch" and c[1] == rr.K_VM and "Halted" in c[4]:
            seq.append("stop")
        elif c[0] == "patch" and c[1] == rr.K_LH_VOLUME:
            seq.append("unlabel")
        elif c[0] == "create" and c[1] == rr.K_LH_SNAPSHOT:
            seq.append("snapshot")
        elif c[0] == "create" and c[1] == rr.K_LH_BACKUP:
            seq.append("backup")
    for c in w.dst.calls[n_dst:]:
        if c[0] == "patch" and c[1] == rr.K_LH_BV:
            seq.append("resync")
        elif c[0] == "patch" and c[1] == rr.K_LH_VOLUME:
            seq.append("activate")
        elif c[0] == "create" and not c[4] and c[1] in (rr.K_PV, rr.K_PVC, rr.K_VM):
            seq.append(c[1])
    dedup = [x for i, x in enumerate(seq) if i == 0 or seq[i - 1] != x]
    assert dedup == ["stop", "unlabel", "snapshot", "backup", "resync", "activate",
                     rr.K_PV, rr.K_PVC, rr.K_PV, rr.K_PVC, rr.K_PV, rr.K_PVC, rr.K_VM]


def test_zero_loss_check_detects_an_activation_without_final_resync(tmp_path, monkeypatch):
    """Garde du test précédent : sans attendre l'application de la
    sauvegarde finale, le moteur a encore l'ancien point à l'activation."""
    w = protected(tmp_path, vms=("vm-b",))
    w.clock.sleep(400)
    monkeypatch.setattr(rr, "wait_applied", lambda *a, **k: {})
    res = rr.failover(w.ctx())
    finals = _final_names(w, res["run_id"]) if "run_id" in res else {}
    sb = rp.target_volume_name(w.pid, VMS["vm-b"]["uid"], "d1", 1, "vm-b")
    assert w.dst.obj(rr.K_LH_VOLUME, rr.NS, sb)["status"]["activatedFrom"] != finals.get("pvc-vm-b-d1")


@pytest.mark.parametrize("how,exit_code", [("cancel", 3), ("error", 1)])
def test_failover_interrupted_before_activation_is_undone(tmp_path, how, exit_code):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.freeze.add("natural-sync")

    def interrupt(name):
        if how == "cancel":
            raise rr.Cancelled("signal 15")
        raise RuntimeError("the server rejected the request")
    w.dst.hooks[("patch", rr.K_LH_BV)] = interrupt
    res = rr.failover(w.ctx())
    assert res["exit"] == exit_code and "undone" in res["message"]
    w.clock.sleep(10)
    assert w.src.obj(rr.K_VMI, "default", "vm-a")["status"]["phase"] == "Running"
    vm = w.src.obj(rr.K_VM, "default", "vm-a")
    assert vm["spec"]["runStrategy"] == "Always" and rr.STOPPED_ANNOTATION not in vm["metadata"].get("annotations", {})
    job = rp.recurring_job_name(w.pid, VMS["vm-a"]["uid"])
    for d in VMS["vm-a"]["disks"]:
        assert w.src.obj(rr.K_LH_VOLUME, rr.NS, f"pvc-vm-a-{d}")["metadata"]["labels"][rp.job_label(job)[0]] == "enabled"
    assert not [k for k in w.src.objs if k[0] == rr.K_LH_SNAPSHOT and k[2].endswith("-final")]
    assert w.plan()["state"] == "protected" and w.plan()["options"]["paused"] is False
    assert {r["state"] for r in db.list_resources(w.conn, w.pid)} == {"protected"}
    assert all(w.dst.obj(rr.K_LH_VOLUME, rr.NS, v["target_volume"])["spec"]["Standby"]
               for v in db.list_volumes(w.conn, w.pid))
    assert db.list_runs(w.conn, w.pid)[-1]["result"] == "rolled-back"
    assert events(w, step="rollback", resource_uid=VMS["vm-a"]["uid"])
    # une nouvelle bascule reprend proprement, avec une nouvelle sauvegarde finale
    w.dst.hooks.clear()
    assert rr.failover(w.ctx())["exit"] == 0


def test_failover_cancelled_after_activation_is_not_undone_and_resumes(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.hooks[("create", rr.K_PV)] = lambda name: (_ for _ in ()).throw(rr.Cancelled("signal 15"))
    res = rr.failover(w.ctx())
    assert res["exit"] == 3 and "run failover again" in res["message"]
    assert w.plan()["state"] == "error" and db.error_origin(w.conn, w.pid) == "failing_over"
    assert all(w.dst.obj(rr.K_LH_VOLUME, rr.NS, v["target_volume"])["spec"]["Standby"] is False
               for v in db.list_volumes(w.conn, w.pid))
    assert w.src.obj(rr.K_VM, "default", "vm-a")["spec"]["runStrategy"] == "Halted"
    assert w.src.obj(rr.K_VMI, "default", "vm-a") is None
    assert w.plan()["options"]["paused"] is True
    w.dst.hooks.clear()
    snaps = len(mut_keys(w.src, "create", rr.K_LH_SNAPSHOT))
    res = rr.failover(w.ctx())
    assert res["exit"] == 0, res
    assert len(mut_keys(w.src, "create", rr.K_LH_SNAPSHOT)) == snaps          # pas de seconde sauvegarde finale
    assert w.dst.obj(rr.K_VMI, "default", "vm-a")["status"]["phase"] == "Running"
    assert w.plan()["state"] == "failed_over"


def test_failover_resumes_after_a_crash_at_every_step(tmp_path):
    ref = protected(tmp_path, vms=("vm-a",))
    ref.mutations = 0
    assert rr.failover(ref.ctx())["exit"] == 0
    n = ref.mutations
    expected = snapshot_state(ref)
    for k in range(1, n + 1):
        w = protected(tmp_path, vms=("vm-a",))
        w.mutations = 0
        w.crash_at = k
        with pytest.raises(Crash):
            rr.failover(w.ctx())
        w.crash_at = None
        res = rr.resume(w.ctx())
        assert res["exit"] == 0, (k, res)
        got = snapshot_state(w)
        # les noms des sauvegardes finales dépendent du numéro d'exécution : mêmes ici
        assert got == expected, k
        dup = [c for c in w.src.mutations() + w.dst.mutations() if c[0] == "create"]
        assert len(dup) == len({(c[1], c[2], c[3]) for c in dup}), k


@pytest.mark.parametrize("freeze,timeout,step", [
    ("stop", "stop", "stop-source"),
    ("backup", "final_backup", "final-backup"),
    ("apply", "resync_apply", "final-resync"),
])
def test_failover_waits_before_activation_are_bounded_and_undone(tmp_path, freeze, timeout, step):
    w = protected(tmp_path, vms=("vm-a",))
    (w.dst if freeze == "apply" else w.src).freeze.add(freeze)
    res = rr.failover(w.ctx(timeouts={timeout: 50}))
    assert res["exit"] == 1 and "undone" in res["message"]
    assert events(w, step=step, code="timeout")
    assert w.plan()["state"] == "protected"


@pytest.mark.parametrize("freeze,timeout,step", [
    ("activate", "activate", "activate"),
    ("bind", "bind", "claims"),
    ("start", "vm_start", "vm-start"),
])
def test_failover_waits_after_activation_are_bounded_and_kept(tmp_path, freeze, timeout, step):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.freeze.add(freeze)
    res = rr.failover(w.ctx(timeouts={timeout: 50}))
    assert res["exit"] == 1 and "run failover again" in res["message"]
    assert events(w, step=step, code="timeout")
    assert w.plan()["state"] == "error"
    w.dst.freeze.clear()
    if freeze == "activate":
        for v in db.list_volumes(w.conn, w.pid):
            w.dst.obj(rr.K_LH_VOLUME, rr.NS, v["target_volume"])["status"].update(isStandby=False, state="detached")
    if freeze == "bind":
        for o in w.dst.objs.values():
            if o.get("kind") == "PersistentVolumeClaim":
                o["status"]["phase"] = "Bound"
    if freeze == "start":
        w.dst.put(rr.K_VMI, {"metadata": {"name": "vm-a", "namespace": "default"}, "status": {"phase": "Running"}})
    assert rr.failover(w.ctx())["exit"] == 0


def test_failover_refused_when_a_mac_is_taken(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.put(rr.K_VM, _vm("squatter", "uid-sq", [], mac=VMS["vm-a"]["mac"]))
    n = len(w.src.mutations()), len(w.dst.mutations())
    res = rr.failover(w.ctx())
    assert res["exit"] == 2 and "mac-in-use" in [f["code"] for f in res["findings"]]
    assert (len(w.src.mutations()), len(w.dst.mutations())) == n
    assert w.plan()["state"] == "protected"


def test_failover_refused_when_a_pvc_is_already_there(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.put(rr.K_PVC, {"metadata": {"name": "vm-a-data", "namespace": "default"}, "spec": {}})
    res = rr.failover(w.ctx())
    assert res["exit"] == 2 and "target-pvc-taken" in [f["code"] for f in res["findings"] if f["level"] == "block"]
    assert w.src.obj(rr.K_VMI, "default", "vm-a") is not None


def test_failover_of_part_of_the_plan_keeps_it_protected(tmp_path):
    w = protected(tmp_path)
    res = rr.failover(w.ctx(), vms=["default/vm-b"])
    assert res["exit"] == 0 and w.plan()["state"] == "protected"
    states = {r["name"]: r["state"] for r in db.list_resources(w.conn, w.pid)}
    assert states == {"vm-a": "protected", "vm-b": "failed_over"}
    assert w.src.obj(rr.K_VMI, "default", "vm-a") is not None
    assert w.plan()["options"]["paused"] is False
    assert rr.failover(w.ctx(), vms=["default/vm-zz"])["code"] == "unknown-vm"


def test_planned_failover_of_a_stopped_vm(tmp_path):
    w = protected(tmp_path, vms=("vm-c",), options={"start": "stopped"})
    res = rr.failover(w.ctx())
    assert res["exit"] == 0, res
    assert w.src.obj(rr.K_VM, "default", "vm-c")["spec"]["runStrategy"] == "Halted"
    assert w.dst.obj(rr.K_VM, "default", "vm-c")["spec"]["runStrategy"] == "Halted"
    assert w.dst.obj(rr.K_VMI, "default", "vm-c") is None


def test_failover_dry_run_writes_nothing(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.hooks[("validate", rr.K_VM)] = lambda obj: (_ for _ in ()).throw(
        RuntimeError('admission webhook denied: persistentvolumeclaim "vm-a-root" not found'))
    n = len(w.src.mutations()), len(w.dst.mutations())
    res = rr.failover(w.ctx(), dry_run=True)
    assert res["exit"] == 0 and (len(w.src.mutations()), len(w.dst.mutations())) == n
    assert "vm-dry-run-pvc-missing" in [f["code"] for f in res["findings"] if f["level"] == "warn"]
    assert {d["object"] for d in res["dry_run"]} == {"pv", "pvc", "vm"}


def test_failover_refused_by_a_vm_webhook_before_activation(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.hooks[("validate", rr.K_VM)] = lambda obj: (_ for _ in ()).throw(RuntimeError("cpu quota exceeded"))
    res = rr.failover(w.ctx())
    assert res["exit"] == 2 and "dry-run-refused" in [f["code"] for f in res["findings"]]
    assert w.src.obj(rr.K_VMI, "default", "vm-a") is not None


# ---------------------------------------------------------------------------
# Bascule non planifiée
# ---------------------------------------------------------------------------

def test_planned_failover_refused_when_the_source_is_lost(tmp_path):
    w = protected(tmp_path)
    w.src.down = True
    res = rr.failover(w.ctx())
    assert res["exit"] == 2 and res["code"] == "source-unreachable" and res["facts"]["hint"] == "--unplanned"
    assert rr.failover(w.ctx(), planned=False)["code"] == "confirmation-required"


def test_unplanned_failover_with_the_source_lost(tmp_path):
    w = protected(tmp_path)
    w.clock.sleep(30)
    for n in ("vm-a", "vm-b"):
        for d in VMS[n]["disks"]:
            w.src.make_backup(f"pvc-{n}-{d}", f"backup-late-{n}-{d}")
    w.src.down = True
    n_src = len(w.src.calls)
    res = rr.failover(w.ctx(), planned=False, yes=True)
    assert res["exit"] == 0, res
    assert res["fenced"] == {"default/vm-a": "source-not-fenced", "default/vm-b": "source-not-fenced"}
    assert set(res["lost_after"]) == {"default/vm-a", "default/vm-b"}
    sb = rp.target_volume_name(w.pid, VMS["vm-b"]["uid"], "d1", 1, "vm-b")
    # la relecture finale a ramené la dernière sauvegarde avant l'activation
    assert w.dst.obj(rr.K_LH_VOLUME, rr.NS, sb)["status"]["activatedFrom"] == "backup-late-vm-b-d1"
    assert w.plan()["state"] == "failed_over"
    assert db.list_runs(w.conn, w.pid)[-1]["points"]["mode"] == "unplanned"
    # rien n'a été écrit sur la source
    assert w.src.calls[n_src:] == []


@pytest.mark.parametrize("mode", ["partition", "dead"])
def test_unplanned_failover_with_backups_in_flight(tmp_path, mode):
    """Sauvegardes en cours au moment de la panne. Coupure réseau
    (`partition`) : Longhorn finit la sauvegarde sur la source, la cible la
    voit à la relecture finale. Cluster mort (`dead`) : elle ne finit
    jamais ; la bascule part du dernier point terminé et dit la perte."""
    w = protected(tmp_path, vms=("vm-b",))
    w.clock.sleep(30)
    for d in VMS["vm-b"]["disks"]:
        w.src.make_backup(f"pvc-vm-b-{d}", f"backup-done-vm-b-{d}")
    w.clock.sleep(30)
    for d in VMS["vm-b"]["disks"]:
        v = f"pvc-vm-b-{d}"
        o = w.src.put(rr.K_LH_BACKUP, {"metadata": {"name": f"backup-flight-{d}", "namespace": rr.NS,
                                                    "labels": {"backup-volume": v, "backup-target": "default"}},
                                       "spec": {"snapshotName": f"backup-flight-{d}"},
                                       "status": {"state": "InProgress"}})
        w.src.at(1, lambda o=o, v=v: w.src.complete_backup(o, v, iso(w.clock.now() - 1)))
    setattr(w.src, "down" if mode == "partition" else "dead", True)
    res = rr.failover(w.ctx(), planned=False, yes=True)
    assert res["exit"] == 0, res
    sb = rp.target_volume_name(w.pid, VMS["vm-b"]["uid"], "d1", 1, "vm-b")
    got = w.dst.obj(rr.K_LH_VOLUME, rr.NS, sb)["status"]["activatedFrom"]
    if mode == "dead":
        assert got == "backup-done-vm-b-d1"
        assert not [n for n in w.store.backups if n.startswith("backup-flight")]
    else:
        assert got == "backup-flight-d1"
    assert set(res["lost_after"]) == {"default/vm-b"}


def test_unplanned_failover_skew_between_disks(tmp_path):
    w = protected(tmp_path, vms=("vm-b",))
    t = w.clock.now()
    for i, d in enumerate(VMS["vm-b"]["disks"]):
        w.src.make_backup(f"pvc-vm-b-{d}", f"backup-skew-{d}", snap_at=iso(t + 30 * i))
    w.src.down = True
    res = rr.failover(w.ctx(), planned=False, yes=True)
    assert res["exit"] == 2 and res["code"] == "skew-exceeded" and res["skew_s"]["default/vm-b"] == 60
    assert w.plan()["state"] == "protected"
    assert all(w.dst.obj(rr.K_LH_VOLUME, rr.NS, v["target_volume"])["spec"]["Standby"]
               for v in db.list_volumes(w.conn, w.pid))
    assert events(w, code="skew-exceeded")
    res = rr.failover(w.ctx(), planned=False, yes=True, accept_skew=True)
    assert res["exit"] == 0 and w.plan()["state"] == "failed_over"


@pytest.mark.parametrize("case,warns", [
    ("caught", []),
    ("skew", ["skew-exceeded"]),
    ("late", ["catch-up-pending"]),
])
def test_unplanned_check_with_the_source_lost_says_what_it_found(tmp_path, case, warns):
    # vu sur le banc (v1.99.0) : `check --phase failover-unplanned`, source
    # injoignable, sortait 0 sans aucun constat
    w = protected(tmp_path, vms=("vm-b",))
    t = w.clock.now()
    if case in ("skew", "late"):
        if case == "late":
            w.dst.apply_s = 400
        for i, d in enumerate(VMS["vm-b"]["disks"]):
            w.src.make_backup(f"pvc-vm-b-{d}", f"backup-{case}-{d}", snap_at=iso(t + 30 * i))
            w.dst.sync_volume(f"pvc-vm-b-{d}")
        w.dst.list(rr.K_LH_ENGINE, rr.NS)          # le contrôleur voit les sauvegardes
        w.clock.sleep(60 if case == "late" else 600)
        w.dst.list(rr.K_LH_ENGINE, rr.NS)
    w.src.down = True
    n = len(w.src.mutations()) + len(w.dst.mutations())
    res = rr.failover(w.ctx(), planned=False, dry_run=True, record=False)
    assert res["exit"] == 0 and res["code"] == "dry-run", res
    assert len(w.src.mutations()) + len(w.dst.mutations()) == n
    got = {f["code"]: f for f in res["findings"]}
    pts = got["applied-points"]["facts"]
    assert pts["vm"] == "default/vm-b" and set(pts["points"]) == {f"pvc-vm-b-{d}" for d in VMS["vm-b"]["disks"]}
    assert pts["lost_after"] and pts["tolerance_s"] == rp.SKEW_TOLERANCE_S
    assert [f["code"] for f in res["findings"] if f["level"] == "warn"] == warns
    assert ("caught-up" in got) is (case != "late")
    if case == "skew":
        assert got["skew-exceeded"]["facts"]["skew_s"] == 60 == pts["skew_s"]
    if case == "late":
        assert {v["volume"] for v in got["catch-up-pending"]["facts"]["volumes"]} == set(pts["points"])


def test_unplanned_failover_refused_when_the_source_answers(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    res = rr.failover(w.ctx(), planned=False, yes=True)
    assert res["exit"] == 2 and res["code"] == "source-reachable"
    res = rr.failover(w.ctx(), planned=False, yes=True, force_unplanned=True)
    assert res["exit"] == 0 and res["fenced"] == {"default/vm-a": "source-fenced"}
    assert w.src.obj(rr.K_VM, "default", "vm-a")["spec"]["runStrategy"] == "Halted"


def test_unplanned_failover_source_comes_back_during_the_failover(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    w.src.down = True
    w.dst.at(1, lambda: setattr(w.src, "down", False))
    res = rr.failover(w.ctx(), planned=False, yes=True)
    assert res["exit"] == 0 and res["fenced"] == {"default/vm-a": "source-fenced"}


def test_unplanned_failover_works_without_the_database_volumes(tmp_path):
    """La bascule ne lit que la cible et le carnet."""
    w = protected(tmp_path, vms=("vm-a",))
    w.conn.execute("DELETE FROM plan_volumes")
    w.conn.commit()
    w.src.down = True
    assert rr.failover(w.ctx(), planned=False, yes=True)["exit"] == 0
    assert w.dst.obj(rr.K_VMI, "default", "vm-a")["status"]["phase"] == "Running"


# ---------------------------------------------------------------------------
# Déprotection
# ---------------------------------------------------------------------------

def test_unprotect_removes_our_objects_and_keeps_harvester_backups(tmp_path):
    w = protected(tmp_path)
    w.src.make_backup("pvc-vm-a-data", "backup-harvester-1", labels={"backup-volume": "pvc-vm-a-data"})
    bvs = sorted(k for k in w.src.objs if k[0] == rr.K_LH_BV)
    assert rr.unprotect(w.ctx(), delete_backups=True)["code"] == "confirmation-required"
    res = rr.unprotect(w.ctx(), delete_backups=True, yes=True)
    assert res["exit"] == 0, res
    assert res["backups_deleted"] >= 5
    assert w.owned(w.src) == [] and w.owned(w.dst) == []
    assert w.src.obj(rr.K_LH_BACKUP, rr.NS, "backup-harvester-1") is not None
    assert "backup-harvester-1" in w.store.backups
    assert sorted(k for k in w.src.objs if k[0] == rr.K_LH_BV) == bvs
    for n in ("vm-a", "vm-b"):
        for d in VMS[n]["disks"]:
            labels = w.src.obj(rr.K_LH_VOLUME, rr.NS, f"pvc-{n}-{d}")["metadata"]["labels"]
            assert not [k for k in labels if k.startswith(rp.JOB_LABEL_PREFIX)]
    assert w.dst.obj(rr.K_LH_BI, rr.NS, "vmi-img1") is None
    assert w.plan()["state"] == "unprotected"
    assert {v["state"] for v in db.list_volumes(w.conn, w.pid)} == {"removed"}
    assert {r["state"] for r in db.list_resources(w.conn, w.pid)} == {"unprotected"}
    for uid in (VMS["vm-a"]["uid"], VMS["vm-b"]["uid"]):
        assert [e["code"] for e in events(w, resource_uid=uid, step="remove-target")] == ["start", "done"]
        assert [e["code"] for e in events(w, resource_uid=uid, step="remove-job")] == ["start", "done"]


def test_unprotect_keeps_a_backing_image_still_in_use(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.put(rr.K_LH_VOLUME, {"metadata": {"name": "other", "namespace": rr.NS},
                               "spec": {"backingImage": "vmi-img1"}, "status": {}})
    assert rr.unprotect(w.ctx())["exit"] == 0
    assert w.dst.obj(rr.K_LH_BI, rr.NS, "vmi-img1") is not None


def test_unprotect_with_the_source_lost_then_rerun(tmp_path):
    w = protected(tmp_path)
    w.src.down = True
    res = rr.unprotect(w.ctx())
    assert res["exit"] == 1 and res["pending"] == ["source"]
    assert w.plan()["state"] == "unprotecting"
    assert [k for k in w.owned(w.dst) if k[0] != rr.K_LH_BACKUP] == []
    assert w.owned(w.src)                               # tâches encore là
    assert events(w, code="pending")
    w.src.down = False
    res = rr.unprotect(w.ctx())
    assert res["exit"] == 0 and w.plan()["state"] == "unprotected"
    assert [k for k in w.owned(w.src) if k[0] != rr.K_LH_BACKUP] == []


def test_unprotect_after_failover_never_touches_the_failed_over_vm(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    assert rr.failover(w.ctx())["exit"] == 0
    keep = {k: copy.deepcopy(o) for k, o in w.dst.objs.items()
            if k[0] in (rr.K_VM, rr.K_PVC, rr.K_PV, rr.K_LH_VOLUME)}
    res = rr.unprotect(w.ctx())
    assert res["exit"] == 0
    for k, o in keep.items():
        assert w.dst.objs.get(k) == o, k
    assert any(x.startswith("vm default/vm-a") for x in res["left"])
    assert w.dst.obj(rr.K_SECRET, rp.CARNET_NAMESPACE, rp.carnet_name(w.pid, VMS["vm-a"]["uid"])) is None


def test_unprotect_of_a_measuring_plan_does_not_need_the_target(world):
    w = world
    rr.measure_start(w.ctx(), "3m")
    w.dst.down = True
    res = rr.unprotect(w.ctx())
    assert res["exit"] == 0 and w.plan()["state"] == "unprotected" and w.owned(w.src) == []


def test_unprotect_resumes_after_a_crash_at_every_step(tmp_path):
    ref = protected(tmp_path)
    ref.mutations = 0
    assert rr.unprotect(ref.ctx(), delete_backups=True, yes=True)["exit"] == 0
    n = ref.mutations
    expected = snapshot_state(ref)
    for k in range(1, n + 1):
        w = protected(tmp_path)
        w.mutations = 0
        w.crash_at = k
        with pytest.raises(Crash):
            rr.unprotect(w.ctx(), delete_backups=True, yes=True)
        w.crash_at = None
        res = rr.resume(w.ctx())
        assert res["exit"] == 0, (k, res)
        assert snapshot_state(w) == expected, k


# ---------------------------------------------------------------------------
# Reprise et états
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["failover", "unprotect", "measure_stop"])
def test_commands_refused_from_a_draft_plan(world, command):
    fn = getattr(rr, command)
    res = fn(world.ctx())
    assert res["exit"] == 2 and res["code"] == "state"
    assert mut_keys(world.src) == []


def test_resume_with_nothing_to_resume(tmp_path):
    w = protected(tmp_path)
    assert rr.resume(w.ctx())["code"] == "nothing-to-resume"


# ---------------------------------------------------------------------------
# Corrections après relecture
# ---------------------------------------------------------------------------

def _interrupt_activation_after(w, n):
    """Annulation au (n+1)-ième patch d'activation sur la cible : n volumes
    activés, les suivants restent en attente."""
    seen = [0]

    def hook(name):
        seen[0] += 1
        if seen[0] > n:
            raise rr.Cancelled("signal 15")
    w.dst.hooks[("patch", rr.K_LH_VOLUME)] = hook


def _abandon_scenario(tmp_path, scenario):
    w = protected(tmp_path, vms=("vm-a",))
    if scenario == "half-activated":
        _interrupt_activation_after(w, 1)
        assert rr.failover(w.ctx())["exit"] == 3
    elif scenario == "activated-before-bring-up":
        w.dst.hooks[("create", rr.K_PV)] = lambda name: (_ for _ in ()).throw(rr.Cancelled("signal 15"))
        assert rr.failover(w.ctx())["exit"] == 3
    elif scenario == "failed-over":
        assert rr.failover(w.ctx())["exit"] == 0
    w.dst.hooks.clear()
    return w


@pytest.mark.parametrize("scenario,activated,carnet_kept,kept_kinds", [
    # disque 1 activé, disque 2 encore en attente : rien ne part de la cible
    ("half-activated", 1, True, {rr.K_LH_VOLUME, rr.K_LH_BI, rr.K_SECRET}),
    ("activated-before-bring-up", 2, True, {rr.K_LH_VOLUME, rr.K_LH_BI, rr.K_SECRET}),
    # bascule finie : la VM et ses secrets sont sur la cible, le carnet (copie) part
    ("failed-over", 2, False, {rr.K_LH_VOLUME, rr.K_LH_BI, rr.K_SECRET, rr.K_PV, rr.K_PVC, rr.K_VM}),
])
def test_abandoning_an_activated_failover_keeps_everything_on_the_target(tmp_path, scenario, activated,
                                                                         carnet_kept, kept_kinds):
    w = _abandon_scenario(tmp_path, scenario)
    vols = [v["target_volume"] for v in db.list_volumes(w.conn, w.pid)]
    assert sum(w.dst.obj(rr.K_LH_VOLUME, rr.NS, v)["spec"]["Standby"] is False for v in vols) == activated
    before = {k: copy.deepcopy(o) for k, o in w.dst.objs.items() if k in w.owned(w.dst)}
    n_src, n_dst = len(w.src.mutations()), len(w.dst.mutations())
    # --delete-backups refusé tant qu'une VM est (à moitié) activée, avant toute écriture
    res = rr.unprotect(w.ctx(), delete_backups=True, yes=True)
    assert res["exit"] == 2 and res["code"] == "backups-in-use", res
    assert res["facts"]["vms"] == ["default/vm-a"]
    assert (len(w.src.mutations()), len(w.dst.mutations())) == (n_src, n_dst)
    # abandon sans suppression des sauvegardes : rien ne part de la cible, sauf le carnet d'une VM montée
    res = rr.unprotect(w.ctx())
    assert res["exit"] == 0, res
    carnet = (rr.K_SECRET, rp.CARNET_NAMESPACE, rp.carnet_name(w.pid, VMS["vm-a"]["uid"]))
    for k, o in before.items():
        if k == carnet and not carnet_kept:
            continue
        assert w.dst.objs.get(k) == o, k
    assert (carnet in w.dst.objs) == carnet_kept
    deletes = [c for c in mut_keys(w.dst, "delete")]
    assert deletes == ([] if carnet_kept else [("delete", rr.K_SECRET, rp.CARNET_NAMESPACE, carnet[2], "")])
    for v in vols:
        assert any(x.startswith(f"volume {rr.NS}/{v}") for x in res["left"]), (v, res["left"])
    assert set(res["left_selectors"]) == kept_kinds
    assert set(res["left_selectors"].values()) == {f"{rp.LABEL_PLAN}={w.pid}"}
    assert w.plan()["state"] == "unprotected"
    # aucune ligne de volume n'est passée à `removed`
    assert "removed" not in {v["state"] for v in db.list_volumes(w.conn, w.pid)}
    # les sauvegardes du plan sont toujours là
    assert [k for k in w.owned(w.src) if k[0] == rr.K_LH_BACKUP]


def test_unprotect_deletes_the_backups_of_its_recurring_jobs(tmp_path):
    """Vu sur le banc : les sauvegardes d'une tâche récurrente n'ont les
    étiquettes du plan que dans `spec.labels` (et `status.labels` une fois
    relues par la cible) ; un sélecteur sur `metadata.labels` ne les trouvait
    pas et `--delete-backups` les laissait toutes."""
    w = protected(tmp_path, vms=("vm-a",))
    uid = VMS["vm-a"]["uid"]
    job = dict(rp.plan_labels(w.pid, 1, uid), RecurringJob=rp.recurring_job_name(w.pid, uid))
    w.src.make_backup("pvc-vm-a-data", "backup-cron-1", job_labels=job)
    w.src.make_backup("pvc-vm-a-data", "backup-client-1", job_labels={"RecurringJob": "daily"})
    w.dst.sync_all()
    synced = w.dst.obj(rr.K_LH_BACKUP, rr.NS, "backup-cron-1")
    assert rp.LABEL_PLAN not in synced["metadata"]["labels"] and synced["status"]["labels"][rp.LABEL_PLAN] == w.pid
    res = rr.unprotect(w.ctx(), delete_backups=True, yes=True)
    assert res["exit"] == 0, res
    assert "backup-cron-1" not in w.store.backups and w.src.obj(rr.K_LH_BACKUP, rr.NS, "backup-cron-1") is None
    assert "backup-client-1" in w.store.backups
    assert not [b for b in w.store.backups.values() if b["labels"].get(rp.LABEL_PLAN) == w.pid]


def test_unprotect_of_a_protected_plan_still_deletes_backups(tmp_path):
    """Témoin du tableau précédent : sans activation, tout part."""
    w = protected(tmp_path, vms=("vm-a",))
    res = rr.unprotect(w.ctx(), delete_backups=True, yes=True)
    assert res["exit"] == 0 and res["left"] == [] and res["backups_deleted"] >= 2
    assert w.owned(w.dst) == [] and w.owned(w.src) == []


def test_unprotect_rerun_remembers_the_held_vms(tmp_path):
    """Les ressources passent à `unprotecting` : la relance retrouve les VMs
    gardées dans la ligne `runs`, même si la base ne dit plus rien."""
    w = _abandon_scenario(tmp_path, "half-activated")
    w.src.down = True
    assert rr.unprotect(w.ctx())["exit"] == 1
    # la base a oublié l'état de bascule ; la cible aussi ne peut plus le dire
    w.conn.execute("UPDATE plan_volumes SET state = 'standby'")
    w.conn.commit()
    for v in db.list_volumes(w.conn, w.pid):
        w.dst.obj(rr.K_LH_VOLUME, rr.NS, v["target_volume"])["spec"]["Standby"] = True
    w.src.down = False
    res = rr.unprotect(w.ctx())
    assert res["exit"] == 0 and len([x for x in res["left"] if x.startswith("volume ")]) == 2


def test_failover_pv_is_prebound_to_its_claim(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    # un PVC étranger en attente, même classe que le disque de données
    w.dst.put(rr.K_PVC, {"metadata": {"name": "squatter", "namespace": "default"},
                         "spec": {"storageClassName": "harvester-longhorn"}, "status": {"phase": "Pending"}})
    res = rr.failover(w.ctx(timeouts={"bind": 30}))
    assert res["exit"] == 0, res
    for d in VMS["vm-a"]["disks"]:
        sb = rp.target_volume_name(w.pid, VMS["vm-a"]["uid"], d, 1, "vm-a")
        assert w.dst.obj(rr.K_PV, None, sb)["spec"]["claimRef"] == {"namespace": "default", "name": f"vm-a-{d}"}
    assert not w.dst.obj(rr.K_PVC, "default", "squatter")["spec"].get("volumeName")


def test_static_pv_claim_ref():
    pv = rp.static_pv("v", 1, "Block", ["ReadWriteMany"], "c", claim_ref=("ns", "pvc"))
    assert pv["spec"]["claimRef"] == {"namespace": "ns", "name": "pvc"}
    assert "claimRef" not in rp.static_pv("v", 1, "Block", ["ReadWriteMany"], "c")["spec"]


def _late_backups(w, vms, prefix, at=None):
    for n in vms:
        for d in VMS[n]["disks"]:
            w.src.make_backup(f"pvc-{n}-{d}", f"{prefix}-{n}-{d}", snap_at=at)


def test_unplanned_failover_records_the_points_read_before_activation(tmp_path):
    """Longhorn finit une restauration en cours au moment de l'activation.
    Après elle, Longhorn 1.12.1 ne publie plus de point (vu sur le banc,
    v1.99.0) : la lecture d'avant l'activation fait foi, `lost_after` est
    prudent (plus tôt que le point réellement activé), jamais optimiste."""
    w = protected(tmp_path, vms=("vm-b",))
    w.src.down = True
    w.dst.finish_on_activate = True
    race_at = w.clock.now() + 500
    fired = [False]

    def race(_name):
        # entre la lecture des points et l'activation, une sauvegarde plus récente arrive
        if not fired[0]:
            fired[0] = True
            w.clock.t = race_at
            w.src.down = False                # les outils du faux lisent la source
            _late_backups(w, ("vm-b",), "backup-race", at=iso(race_at))
            w.src.down = True
            for d in VMS["vm-b"]["disks"]:
                w.dst.sync_volume(f"pvc-vm-b-{d}")
    w.dst.hooks[("patch", rr.K_LH_VOLUME)] = race
    uid = VMS["vm-b"]["uid"]
    before = {p["backup"] for p in rr._applied_points(
        w.ctx(), {"volumes": [{"source_volume": f"pvc-vm-b-{d}",
                               "target_volume": rp.target_volume_name(w.pid, uid, d, 1, "vm-b")}
                              for d in VMS["vm-b"]["disks"]]}, rr.backups_by_volume(w.dst))[0].values()}
    res = rr.failover(w.ctx(), planned=False, yes=True)
    assert res["exit"] == 0, res
    for d in VMS["vm-b"]["disks"]:
        sb = rp.target_volume_name(w.pid, uid, d, 1, "vm-b")
        assert w.dst.obj(rr.K_LH_VOLUME, rr.NS, sb)["status"]["activatedFrom"] == f"backup-race-vm-b-{d}"
    applied = db.list_runs(w.conn, w.pid)[-1]["points"]["applied"][uid]
    assert {p["backup"] for p in applied.values()} == before
    assert not before & {f"backup-race-vm-b-{d}" for d in VMS["vm-b"]["disks"]}
    # prudent : la perte annoncée commence avant le point réellement activé
    assert rp._t(res["lost_after"]["default/vm-b"]) < rp._t(iso(race_at))
    done = events(w, step="done", code="failed-over", resource_uid=uid)
    assert len(done) == 1 and json.loads(done[0]["facts"])["lost_after"] == res["lost_after"]["default/vm-b"]


def test_unplanned_failover_waits_for_the_last_backup_before_activation(tmp_path):
    w = protected(tmp_path, vms=("vm-b",))
    _late_backups(w, ("vm-b",), "backup-late")
    w.src.down = True
    w.dst.apply_s = 400                       # le moteur met longtemps à appliquer la dernière
    res = rr.failover(w.ctx(timeouts={"unplanned_catchup": 60}), planned=False, yes=True)
    assert res["exit"] == 1 and res["code"] == "timeout" and "undone" in res["message"]
    assert events(w, step="catch-up", code="timeout")
    assert all(w.dst.obj(rr.K_LH_VOLUME, rr.NS, v["target_volume"])["spec"]["Standby"]
               for v in db.list_volumes(w.conn, w.pid))
    assert w.plan()["state"] == "protected"
    # avec le temps qu'il faut, la bascule active la dernière sauvegarde
    res = rr.failover(w.ctx(timeouts={"unplanned_catchup": 900}), planned=False, yes=True)
    assert res["exit"] == 0, res
    sb = rp.target_volume_name(w.pid, VMS["vm-b"]["uid"], "d1", 1, "vm-b")
    assert w.dst.obj(rr.K_LH_VOLUME, rr.NS, sb)["status"]["activatedFrom"] == "backup-late-vm-b-d1"


def test_forced_unplanned_failover_fences_the_live_source_first(tmp_path):
    w = protected(tmp_path, vms=("vm-b",))
    n_src, n_dst = len(w.src.calls), len(w.dst.calls)
    res = rr.failover(w.ctx(), planned=False, yes=True, force_unplanned=True)
    assert res["exit"] == 0, res
    assert len(w.dst.calls) > n_dst
    # ordre des étapes, lu dans le journal (une ligne par début d'étape)
    seq = []
    for e in events(w):
        if e["operation"] != "failover" or e["code"] != "start":
            continue
        if e["step"] not in seq:
            seq.append(e["step"])
    assert seq[:5] == ["stop-source", "remove-job-label", "final-resync", "catch-up", "activate"], seq
    job = rp.recurring_job_name(w.pid, VMS["vm-b"]["uid"])
    assert w.src.obj(rr.K_LH_JOB, rr.NS, job) is None
    for d in VMS["vm-b"]["disks"]:
        assert rp.job_label(job)[0] not in w.src.obj(rr.K_LH_VOLUME, rr.NS, f"pvc-vm-b-{d}")["metadata"]["labels"]
    vm = w.src.obj(rr.K_VM, "default", "vm-b")
    assert vm["spec"]["runStrategy"] == "Halted" and w.src.obj(rr.K_VMI, "default", "vm-b") is None
    assert vm["metadata"]["annotations"][rr.FAILED_OVER_TO] == "harvlab2/default/vm-b"
    assert rr.STOPPED_ANNOTATION not in vm["metadata"]["annotations"]
    assert res["fenced"] == {"default/vm-b": "source-fenced"}
    assert db.list_runs(w.conn, w.pid)[-1]["points"]["force_unplanned"] is True
    # la source a été clôturée avant que la cible ne relise quoi que ce soit
    src_fence = [i for i, c in enumerate(w.src.calls[n_src:]) if c[0] == "delete" and c[1] == rr.K_LH_JOB]
    assert src_fence and not [c for c in w.src.calls[n_src:][src_fence[0]:]
                              if c[0] in ("create", "patch") and c[1] in (rr.K_LH_SNAPSHOT, rr.K_LH_BACKUP)]


def test_forced_unplanned_failover_undone_before_activation_restores_the_source(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.hooks[("patch", rr.K_LH_BV)] = lambda name: (_ for _ in ()).throw(rr.Cancelled("signal 15"))
    res = rr.failover(w.ctx(), planned=False, yes=True, force_unplanned=True)
    assert res["exit"] == 3 and "undone" in res["message"]
    w.clock.sleep(10)
    job = rp.recurring_job_name(w.pid, VMS["vm-a"]["uid"])
    assert w.src.obj(rr.K_LH_JOB, rr.NS, job) is not None
    for d in VMS["vm-a"]["disks"]:
        assert w.src.obj(rr.K_LH_VOLUME, rr.NS, f"pvc-vm-a-{d}")["metadata"]["labels"][rp.job_label(job)[0]] == "enabled"
    assert w.src.obj(rr.K_VMI, "default", "vm-a")["status"]["phase"] == "Running"
    assert w.plan()["state"] == "protected"


@pytest.mark.parametrize("mode,source_after", [
    ("planned", "gone"), ("planned", "back"),
    ("unplanned", "gone"), ("unplanned", "back"),
    ("forced", "gone"), ("forced", "back"),
])
def test_resume_after_activation_needs_only_the_target(tmp_path, mode, source_after):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.clear_on_activate = True
    if mode == "unplanned":
        w.src.down = True
    w.dst.hooks[("create", rr.K_PV)] = lambda name: (_ for _ in ()).throw(rr.Cancelled("signal 15"))
    res = rr.failover(w.ctx(), planned=mode == "planned", yes=True, force_unplanned=mode == "forced")
    assert res["exit"] == 3 and "run failover again" in res["message"], res
    recorded = db.list_runs(w.conn, w.pid)[-1]["points"]["applied"][VMS["vm-a"]["uid"]]
    assert recorded and all(p["backup"] for p in recorded.values())
    w.dst.hooks.clear()
    w.src.down = source_after == "gone"
    res = rr.resume(w.ctx())
    assert res["exit"] == 0, res
    assert res["mode"] == ("planned" if mode == "planned" else "unplanned")
    # v1.99.0 : le moteur activé n'a plus de point, la lecture d'avant
    # l'activation reste celle du rapport et de l'exécution
    assert db.list_runs(w.conn, w.pid)[-1]["points"]["applied"][VMS["vm-a"]["uid"]] == recorded
    assert set(res["point_at"]) == {"default/vm-a"}
    assert bool(res["lost_after"]) is (mode != "planned")
    assert w.dst.obj(rr.K_VMI, "default", "vm-a")["status"]["phase"] == "Running"
    assert w.plan()["state"] == "failed_over"
    run = db.list_runs(w.conn, w.pid)[-1]
    assert run["result"] == "ok" and run["points"]["force_unplanned"] is (mode == "forced")
    if source_after == "back":
        vm = w.src.obj(rr.K_VM, "default", "vm-a")
        assert vm["metadata"]["annotations"][rr.FAILED_OVER_TO] == "harvlab2/default/vm-a"
        assert vm["spec"]["runStrategy"] == "Halted"


def test_resume_of_a_partial_failover_keeps_its_vms(tmp_path):
    w = protected(tmp_path)
    w.dst.hooks[("create", rr.K_PV)] = lambda name: (_ for _ in ()).throw(rr.Cancelled("signal 15"))
    assert rr.failover(w.ctx(), vms=["default/vm-b"])["exit"] == 3
    w.dst.hooks.clear()
    assert rr.resume(w.ctx())["exit"] == 0
    states = {r["name"]: r["state"] for r in db.list_resources(w.conn, w.pid)}
    assert states == {"vm-a": "protected", "vm-b": "failed_over"}
    assert w.src.obj(rr.K_VMI, "default", "vm-a") is not None


def test_protect_rerun_after_a_partial_failover_leaves_the_failed_over_vm_alone(tmp_path):
    w = protected(tmp_path)
    assert rr.failover(w.ctx(), vms=["default/vm-b"])["exit"] == 0
    n_src, n_dst = len(w.src.mutations()), len(w.dst.mutations())
    res = rr.protect(w.ctx())
    assert res["exit"] == 0, res
    job_b = rp.recurring_job_name(w.pid, VMS["vm-b"]["uid"])
    assert w.src.obj(rr.K_LH_JOB, rr.NS, job_b) is None
    for d in VMS["vm-b"]["disks"]:
        labels = w.src.obj(rr.K_LH_VOLUME, rr.NS, f"pvc-vm-b-{d}")["metadata"]["labels"]
        assert rp.job_label(job_b)[0] not in labels
    touched = {c[3] for c in w.src.mutations()[n_src:] + w.dst.mutations()[n_dst:]}
    assert not [n for n in touched if "vm-b" in n or job_b in n], touched
    states = {r["name"]: r["state"] for r in db.list_resources(w.conn, w.pid)}
    assert states == {"vm-a": "protected", "vm-b": "failed_over"} and w.plan()["state"] == "protected"
    assert events(w, step="skip", code="failed-over", resource_uid=VMS["vm-b"]["uid"])


def test_protect_never_reuses_an_activated_volume_as_a_standby(world):
    w = world
    sb = rp.target_volume_name(w.pid, VMS["vm-b"]["uid"], "d2", 1, "vm-b")
    w.dst.put(rr.K_LH_VOLUME, {"metadata": {"name": sb, "namespace": rr.NS,
                                            "labels": rp.plan_labels(w.pid, 1, VMS["vm-b"]["uid"])},
                               "spec": {"Standby": False, "fromBackup": f"{BT_URL}?backup=b&volume=pvc-vm-b-d2"},
                               "status": {"isStandby": False}})
    res = rr.protect(w.ctx())
    assert res["exit"] == 2 and res["code"] == "standby-activated" and res["facts"]["volumes"] == [sb]
    assert mut_keys(w.src) == [] and mut_keys(w.dst) == []


@pytest.mark.parametrize("mapping,code", [
    ({"default/vm-a-data": "vm-a-data-dr"}, None),
    ({"vm-a-data": "vm-a-data-dr"}, "bad-reference"),
    ({"default/vm-a-data": "dr/vm-a-data"}, "bad-mapping"),
    ({"default/vm-a-data": "Bad"}, "bad-mapping"),
])
def test_claims_mapping_is_keyed_by_namespace_and_claim(mapping, code):
    req = {"name": "p", "source_cluster": "a", "target_cluster": "b", "rpo_objective_s": "15m",
           "vms": ["default/vm-a"], "mappings": {"claims": mapping}}
    if code is None:
        assert rp.normalize_plan(req)["mappings"]["claims"] == mapping
    else:
        with pytest.raises(rp.PlanError) as e:
            rp.normalize_plan(req)
        assert e.value.code == code


CLAIMS = {"networks": {"default/prod": "default/lab-net"}, "claims": {"default/vm-a-data": "vm-a-data-dr"}}


@pytest.mark.parametrize("taken,warned", [("vm-a-data-dr", True), ("vm-a-data", False)])
def test_claims_mapping_reaches_the_pvc_check(tmp_path, taken, warned):
    w = World(tmp_path, vms=("vm-a",), mappings=CLAIMS)
    w.dst.put(rr.K_PVC, {"metadata": {"name": taken, "namespace": "default"}, "spec": {}})
    res = rr.protect(w.ctx(), dry_run=True)
    hits = [f for f in res["findings"] if f["code"] == "target-pvc-taken"]
    assert bool(hits) == warned and (not hits or hits[0]["facts"]["name"] == f"default/{taken}")


def test_claims_mapping_through_works_carnet_and_failover(tmp_path):
    w = World(tmp_path, vms=("vm-a",), mappings=CLAIMS)
    c = w.ctx()
    src = rr.collect_source(c)
    works = rr.build_works(c, src, rr.collect_target(c, ["default"]))
    assert {d["claim"]: d["target_claim"] for d in works[0]["disks"]} == \
        {"vm-a-root": "vm-a-root", "vm-a-data": "vm-a-data-dr"}
    assert rr.protect(w.ctx())["exit"] == 0
    carnet = rr.read_carnet(w.dst.obj(rr.K_SECRET, rp.CARNET_NAMESPACE, rp.carnet_name(w.pid, VMS["vm-a"]["uid"])))
    assert {v["claim"]: v["target_claim"] for v in carnet["volumes"]}["vm-a-data"] == "vm-a-data-dr"
    claims = {v["name"]: v["persistentVolumeClaim"]["claimName"]
              for v in carnet["vm"]["spec"]["template"]["spec"]["volumes"] if "persistentVolumeClaim" in v}
    assert claims == {"root": "vm-a-root", "data": "vm-a-data-dr"}
    assert rr.failover(w.ctx())["exit"] == 0
    assert w.dst.obj(rr.K_PVC, "default", "vm-a-data-dr")["status"]["phase"] == "Bound"
    assert w.dst.obj(rr.K_PVC, "default", "vm-a-data") is None


def test_left_lists_secrets_namespace_and_backing_images(tmp_path):
    w = protected(tmp_path, vms=("vm-a",), options={"create_namespace": True},
                  mappings={"networks": {"default/prod": "default/lab-net"}, "namespaces": {"default": "dr"}})
    assert rr.failover(w.ctx())["exit"] == 0
    res = rr.unprotect(w.ctx())
    assert res["exit"] == 0, res
    for item in ("secret dr/vm-a-cloudinit", "namespace dr", f"backingimage {rr.NS}/vmi-img1 (in use)", "vm dr/vm-a"):
        assert item in res["left"], (item, res["left"])
    assert {rr.K_SECRET, rr.K_NS, rr.K_LH_BI}.issubset(res["left_selectors"])
    left = events(w, step="left", code="left")
    assert left and left[-1]["host"] == "harvlab2"


def test_unprotect_removes_only_the_default_group_label_it_added(tmp_path):
    w = World(tmp_path, vms=("vm-b",))
    w.src.obj(rr.K_LH_VOLUME, rr.NS, "pvc-vm-b-d3")["metadata"]["labels"] = {rr.DEFAULT_GROUP_LABEL: "enabled"}
    assert rr.protect(w.ctx())["exit"] == 0
    flags = {v["disk"]: v["default_label_added"] for v in db.list_volumes(w.conn, w.pid)}
    assert flags == {"d1": 1, "d2": 1, "d3": 0}
    assert rr.unprotect(w.ctx())["exit"] == 0
    for d, kept in (("d1", False), ("d2", False), ("d3", True)):
        labels = w.src.obj(rr.K_LH_VOLUME, rr.NS, f"pvc-vm-b-{d}")["metadata"]["labels"]
        assert (rr.DEFAULT_GROUP_LABEL in labels) == kept, d


@pytest.mark.parametrize("command,prep", [
    ("protect", None), ("failover", "protect"), ("measure_start", None), ("unprotect", "protect"),
])
def test_preflight_read_failures_return_the_result_contract(tmp_path, command, prep):
    w = protected(tmp_path, vms=("vm-a",)) if prep else World(tmp_path, vms=("vm-a",))
    kube = w.src if command == "measure_start" else w.dst

    def boom(_name):
        raise RuntimeError("etcdserver: leader changed")
    kube.hooks[("list", rr.K_SC if command != "unprotect" else rr.K_LH_VOLUME)] = boom
    if command == "failover":
        w.dst.hooks[("list", rr.K_SC)] = boom
    n = len(w.src.mutations()), len(w.dst.mutations())
    res = getattr(rr, command)(w.ctx())
    assert isinstance(res, dict) and res["exit"] == 1 and res["code"] == "failed", res
    assert db.get_lock(w.conn, w.pid) is None
    assert (len(w.src.mutations()), len(w.dst.mutations())) == n
    assert events(w, step="check", code="failed")


def test_protect_cancel_restores_the_reused_measure_job(world):
    w = world
    assert rr.measure_start(w.ctx(), "1m")["exit"] == 0
    job = rp.recurring_job_name(w.pid, VMS["vm-a"]["uid"])
    before = copy.deepcopy(w.src.obj(rr.K_LH_JOB, rr.NS, job)["spec"])
    w.src.hooks[("create", rr.K_LH_SNAPSHOT)] = lambda name: (_ for _ in ()).throw(rr.Cancelled("signal 15"))
    res = rr.protect(w.ctx(), interval="5m")
    assert res["exit"] == 3
    after = w.src.obj(rr.K_LH_JOB, rr.NS, job)["spec"]
    assert after == before and after["cron"] == "*/1 * * * *"


def test_carnet_namespace_created_by_someone_else_at_the_same_time(world):
    w = world

    def race(_name):
        w.dst.put(rr.K_NS, {"metadata": {"name": rp.CARNET_NAMESPACE}})
    w.dst.hooks[("create", rr.K_NS)] = race
    assert rr.protect(w.ctx())["exit"] == 0


def test_plan_events_and_delete_backups_have_a_host(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    assert rr.unprotect(w.ctx(), delete_backups=True, yes=True)["exit"] == 0
    # les passages d'état sont écrits par le registre (replication_db), hors du déroulé
    rows = [e for e in events(w) if e["step"] != "state"]
    assert all(e["host"] for e in rows), [e for e in rows if not e["host"]][:3]
    assert {(e["side"], e["host"]) for e in events(w, step="delete-backups")} == \
        {("source", "harvlab"), ("target", "harvlab2")}


def test_failover_dry_runs_the_vm_once(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    assert rr.failover(w.ctx())["exit"] == 0
    assert len([c for c in w.dst.calls if c[0] == "create" and c[1] == rr.K_VM and c[4] == "dry"]) == 1


def test_unprotect_reads_the_target_when_the_database_forgot_the_failover(tmp_path):
    """Base restaurée d'avant la bascule : la cible dit seule qu'un volume
    est activé, et cela suffit à tout garder."""
    w = _abandon_scenario(tmp_path, "half-activated")
    w.conn.execute("UPDATE plan_resources SET state = 'protected', error_origin = ''")
    w.conn.execute("UPDATE plan_volumes SET state = 'standby'")
    w.conn.execute("UPDATE plans SET state = 'protected', error_origin = ''")
    w.conn.commit()
    res = rr.unprotect(w.ctx(), delete_backups=True, yes=True)
    assert res["exit"] == 2 and res["code"] == "backups-in-use"
    res = rr.unprotect(w.ctx())
    assert res["exit"] == 0 and mut_keys(w.dst, "delete") == []


# ---------------------------------------------------------------------------
# Revue de la tâche 5 : retour arrière borné, état relu sous le verrou,
# `paused` gardé, bascule ouverte, contrôle à blanc muet
# ---------------------------------------------------------------------------

LATE = "(undo deadline reached)"


@pytest.mark.parametrize("command,side,key,undo_total", [
    ("protect", "dst", ("create", rr.K_SECRET), None),
    ("protect", "dst", ("create", rr.K_SECRET), 60),
    ("failover", "src", ("create", rr.K_LH_SNAPSHOT), None),
    ("failover", "src", ("create", rr.K_LH_SNAPSHOT), 60),
])
def test_rollback_stops_at_one_overall_deadline(tmp_path, command, side, key, undo_total):
    """API morte pendant l'annulation : chaque suppression réessaierait
    jusqu'à 300 s ; une seule échéance (240 s par défaut, réglable) borne
    tout le retour arrière, et ce qui reste est listé."""
    w = World(tmp_path) if command == "protect" else protected(tmp_path)
    at = {"n": 0}

    def dead_then_cancel(name):
        # bascule : au dernier des 5 instantanés finaux (4 sont à défaire)
        at["n"] += 1
        if command == "failover" and at["n"] < 5:
            return
        at["t"] = w.clock.now()
        w.src.down = w.dst.down = True
        raise rr.Cancelled("signal 15")
    getattr(w, side).hooks[key] = dead_then_cancel
    ctx = w.ctx(timeouts={"undo_total": undo_total} if undo_total else {})
    res = rr.protect(ctx) if command == "protect" else rr.failover(ctx)
    total = undo_total or rp.TIMEOUTS["undo_total"]
    assert res["exit"] == 1 and res["code"] == "undo-timeout", res
    assert [x for x in res["left"] if x.endswith(LATE)], res["left"]
    assert w.clock.now() - at["t"] <= total + 10                 # une attente de 10 s au plus après l'échéance
    ev = events(w, code="undo-timeout")
    assert len(ev) == 1 and json.loads(ev[0]["facts"]) == {"left": res["left"], "undo_total_s": total}
    assert db.get_lock(w.conn, w.pid) is None
    if command == "failover":
        assert db.list_runs(w.conn, w.pid)[-1]["result"] == "undo-timeout"


def test_rollback_within_its_deadline_is_a_plain_cancel(tmp_path):
    w = World(tmp_path)
    w.dst.hooks[("create", rr.K_SECRET)] = lambda name: (_ for _ in ()).throw(rr.Cancelled("signal 15"))
    ctx = w.ctx()
    res = rr.protect(ctx)
    assert res["exit"] == 3 and res["code"] == "cancelled" and not ctx.undo_timed_out
    assert not [x for x in res.get("left") or [] if x.endswith(LATE)]


def _move_state(w, state):
    w.conn.execute("UPDATE plans SET state = ? WHERE id = ?", (state, w.pid))


@pytest.mark.parametrize("command,side,key,change", [
    # une autre commande a déplacé le plan entre la lecture et le verrou
    ("protect", "dst", ("list", rr.K_SC), lambda w: _move_state(w, "measuring")),
    # les VMs du plan ont changé
    ("protect", "dst", ("list", rr.K_SC), lambda w: db.update_plan(w.conn, w.pid, vms=["default/vm-a"])),
    # une génération nouvelle
    ("protect", "dst", ("list", rr.K_SC),
     lambda w: w.conn.execute("UPDATE plans SET generation = generation + 1 WHERE id = ?", (w.pid,))),
    ("measure-start", "src", ("get", rr.K_VM), lambda w: db.update_plan(w.conn, w.pid, vms=["default/vm-a"])),
    # la sélection de la bascule a changé (état d'une VM)
    ("failover", "dst", ("list", rr.K_SC),
     lambda w: db.set_resource_state(w.conn, w.pid, VMS["vm-b"]["uid"], "error", generation=1)),
    ("failover", "dst", ("list", rr.K_SC), lambda w: _move_state(w, "error")),
    ("unprotect", "dst", ("list", rr.K_LH_VOLUME), lambda w: _move_state(w, "error")),
])
def test_state_and_selection_are_checked_again_under_the_lock(tmp_path, command, side, key, change):
    w = World(tmp_path) if command in ("protect", "measure-start") else protected(tmp_path)
    kube = getattr(w, side)

    def once(name):
        kube.hooks.pop(key)
        change(w)
    kube.hooks[key] = once
    before = len(w.src.mutations()) + len(w.dst.mutations())
    ctx = w.ctx()
    res = {"protect": lambda: rr.protect(ctx), "measure-start": lambda: rr.measure_start(ctx, "1m"),
           "failover": lambda: rr.failover(ctx), "unprotect": lambda: rr.unprotect(ctx)}[command]()
    assert res["exit"] == 2 and res["code"] == "state-changed", res
    assert key not in kube.hooks                                  # le changement a bien eu lieu
    assert len(w.src.mutations()) + len(w.dst.mutations()) == before
    assert db.get_lock(w.conn, w.pid) is None


@pytest.mark.parametrize("paused_before,meanwhile,fail,expected,kept_event", [
    (False, None, False, False, False),
    (True, None, False, True, False),
    (True, False, False, False, True),            # changé pendant la bascule : gardé
    (True, False, True, False, True),             # idem quand la bascule est défaite
])
def test_failover_keeps_a_paused_change_made_meanwhile(tmp_path, paused_before, meanwhile, fail, expected,
                                                       kept_event):
    w = protected(tmp_path, vms=("vm-a",))
    db.update_plan(w.conn, w.pid, options={"paused": paused_before})
    key = ("create", rr.K_LH_SNAPSHOT)

    def hook(name):
        w.src.hooks.pop(key)
        if meanwhile is not None:
            db.update_plan(w.conn, w.pid, options={"paused": meanwhile})
        if fail:
            raise RuntimeError("injected failure")
    w.src.hooks[key] = hook
    res = rr.failover(w.ctx())
    assert res["exit"] == (1 if fail else 0), res
    assert w.plan()["options"]["paused"] is expected
    assert bool(events(w, code="paused-kept")) is kept_event


def test_an_open_failover_is_only_run_again_on_its_scope(tmp_path):
    w = protected(tmp_path, vms=("vm-a", "vm-b"))
    w.dst.hooks[("create", rr.K_PV)] = lambda name: (_ for _ in ()).throw(RuntimeError("apiserver gone"))
    assert rr.failover(w.ctx())["code"] == "interrupted"
    w.dst.hooks.clear()
    for kw in ({"vms": ["default/vm-a"]}, {"planned": False, "yes": True}):
        res = rr.failover(w.ctx(), **kw)
        assert res["exit"] == 2 and res["code"] == "open-run" and "resume" in res["message"], (kw, res)
        assert res["facts"] == {"vms": ["default/vm-a", "default/vm-b"], "mode": "planned", "hint": "resume"}
    res = rr.resume(w.ctx())
    assert res["exit"] == 0 and w.plan()["state"] == "failed_over", res


def test_failover_dry_run_without_record_writes_no_event(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    n = len(events(w))
    assert rr.failover(w.ctx(), dry_run=True, record=False)["code"] == "dry-run"
    assert rr.failover(w.ctx(), planned=False, dry_run=True, record=False)["code"] == "source-reachable"
    assert len(events(w)) == n
    assert rr.failover(w.ctx(), planned=False, dry_run=True)["code"] == "source-reachable"
    assert len(events(w)) > n                                    # la commande, elle, garde sa trace


def test_unprotect_cancelled_is_interrupted_not_undone(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    w.dst.hooks[("delete", rr.K_LH_VOLUME)] = lambda name: (_ for _ in ()).throw(rr.Cancelled("signal 15"))
    res = rr.unprotect(w.ctx())
    assert res["exit"] == 3 and res["code"] == "interrupted" and w.plan()["state"] == "unprotecting", res
    w.dst.hooks.clear()
    assert rr.resume(w.ctx())["code"] == "unprotected"


# ---------------------------------------------------------------------------
# Corrections après relecture (tâche 6) : pas d'élargissement silencieux
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call,exit_code,code", [
    # relance sans --delete-backups : refusée avant toute écriture
    (lambda c: rr.unprotect(c), 2, "needs-admin-resume"),
    (lambda c: rr.unprotect(c, yes=True), 2, "needs-admin-resume"),
    # demande explicite, ou reprise (qui relit la ligne `runs`) : terminée
    (lambda c: rr.unprotect(c, delete_backups=True, yes=True), 0, None),
    (lambda c: rr.resume(c), 0, None),
])
def test_an_interrupted_unprotect_with_backup_deletion_is_never_widened_silently(tmp_path, call, exit_code, code):
    w = protected(tmp_path, vms=("vm-a",))
    w.src.down = True
    assert rr.unprotect(w.ctx(), delete_backups=True, yes=True)["exit"] == 1     # source injoignable : à finir
    w.src.down = False
    n_src, n_dst = len(w.src.mutations()), len(w.dst.mutations())
    res = call(w.ctx())
    assert res["exit"] == exit_code, res
    if code:
        assert res["code"] == code and res["facts"] == {"delete_backups": True}
        assert (len(w.src.mutations()), len(w.dst.mutations())) == (n_src, n_dst)
        assert w.plan()["state"] == "unprotecting"
    else:
        assert w.plan()["state"] == "unprotected"
        assert not [k for k in w.owned(w.src) if k[0] == rr.K_LH_BACKUP]


def test_an_interrupted_plain_unprotect_still_finishes_without_the_flag(tmp_path):
    w = protected(tmp_path, vms=("vm-a",))
    w.src.down = True
    assert rr.unprotect(w.ctx())["exit"] == 1
    w.src.down = False
    res = rr.unprotect(w.ctx())
    assert res["exit"] == 0 and w.plan()["state"] == "unprotected"
    assert [k for k in w.owned(w.src) if k[0] == rr.K_LH_BACKUP]       # sauvegardes gardées


def test_protect_dry_run_tries_the_carnet_once_its_namespace_exists(world):
    w = world
    w.dst.put(rr.K_NS, {"metadata": {"name": rp.CARNET_NAMESPACE}})
    res = rr.protect(w.ctx(), dry_run=True)
    assert res["exit"] == 0
    carnets = [d for d in res["dry_run"] if d["object"] == "carnet"]
    assert carnets and all(d["ok"] and "deferred" not in d for d in carnets)
    assert not [d for d in res["dry_run"] if d["object"] == "namespace"]
    assert len([c for c in w.dst.calls if c[0] == "create" and c[1] == rr.K_SECRET and c[4] == "dry"]) == len(carnets)


def test_protect_dry_run_tries_a_standby_once_a_backup_exists(world):
    w = world
    for n in ("vm-a", "vm-b"):
        for d in VMS[n]["disks"]:
            w.src.make_backup(f"pvc-{n}-{d}", f"backup-pre-{n}-{d}")
    w.dst.sync_all()
    res = rr.protect(w.ctx(), dry_run=True)
    assert res["exit"] == 0
    sv = [d for d in res["dry_run"] if d["object"] == "standby-volume"]
    tried = [d for d in sv if "deferred" not in d]
    assert tried and all(d["ok"] and d["placeholder_url"] is False for d in tried)
    assert {d.get("deferred") for d in sv} == {None, "backing-image"}
    assert len([c for c in w.dst.calls if c[0] == "create" and c[1] == rr.K_LH_VOLUME and c[4] == "dry"]) == len(tried)


def test_protect_dry_run_of_a_protected_plan_passes(tmp_path):
    """Relance d'une protection (après un `plan-drift`) : la tâche et le
    carnet existent déjà, leur création au dry-run était refusée
    (AlreadyExists, vu sur le banc)."""
    w = protected(tmp_path)
    res = rr.protect(w.ctx(), dry_run=True)
    assert res["exit"] == 0, [d for d in res["dry_run"] if not d["ok"]]
    for what in ("recurring-job", "carnet", "standby-volume"):
        rows = [d for d in res["dry_run"] if d["object"] == what]
        assert rows and all(d["ok"] and d.get("exists") for d in rows), what


def _job_retains(w):
    return sorted(j["spec"]["retain"] for j in w.src.list(rr.K_LH_JOB, rr.NS)
                  if rp.LABEL_PLAN in (j["metadata"].get("labels") or {}))


# v1.99.0, décision 56 : protect --retain met à jour la tâche d'un plan protégé
@pytest.mark.parametrize("retain,exit_code,expected", [
    (None, 0, 6), (6, 0, 6), (9, 0, 9), ("12", 0, 12),
    (2, 2, 6), (51, 2, 6), ("x", 2, 6), (True, 2, 6), ("9 ", 0, 9),
])
def test_protect_retain_updates_the_job_of_a_protected_plan(tmp_path, retain, exit_code, expected):
    w = protected(tmp_path)
    assert w.plan()["retain"] == 6 and _job_retains(w) == [6, 6]
    before = len(w.src.mutations()) + len(w.dst.mutations())
    res = rr.protect(w.ctx(), retain=retain)
    assert res["exit"] == exit_code, res
    assert w.plan()["retain"] == expected and _job_retains(w) == [expected, expected]
    assert w.plan()["state"] == "protected"
    patches = [c for c in w.src.mutations() if c[0] == "patch" and c[1] == rr.K_LH_JOB]
    assert len(patches) == (2 if expected != 6 else 0)
    if exit_code == 2:
        assert res["code"] == "bad-retain"
        assert len(w.src.mutations()) + len(w.dst.mutations()) == before
    retain_events = [e for e in events(w, operation="protect") if e["step"] == "retain"]
    assert len(retain_events) == (1 if expected != 6 else 0)


def test_protect_retain_cancelled_restores_the_job_and_the_plan_row(tmp_path):
    w = protected(tmp_path)
    interval = w.plan()["interval_s"]
    seen = []

    def hook(name):
        seen.append(name)
        if len(seen) == 2:
            raise rr.Cancelled("signal 15")
    w.src.hooks[("patch", rr.K_LH_JOB)] = hook
    res = rr.protect(w.ctx(), retain=9, interval="10m")
    assert res["exit"] == 3, res
    w.src.hooks.clear()
    assert _job_retains(w) == [6, 6]
    assert (w.plan()["retain"], w.plan()["interval_s"]) == (6, interval) and interval != 600


def test_protect_dry_run_refuses_a_job_of_someone_else(world):
    w = world
    job = rp.recurring_job_name(w.pid, VMS["vm-a"]["uid"])
    w.src.put(rr.K_LH_JOB, {"metadata": {"name": job, "namespace": rr.NS}, "spec": {}})
    res = rr.protect(w.ctx(), dry_run=True)
    assert res["exit"] == 2, res
    # le pré-contrôle le refuse déjà ; le dry-run ne l'aurait pas laissé passer
    assert res["code"] == "check" and "recurring-job-taken" in [f["code"] for f in res["findings"]], res


@pytest.mark.parametrize("leftover, ok", [
    ({"markRemoved": True}, True),      # reste d'un protect annulé, recréé par Longhorn
    ({}, False),                        # instantané de quelqu'un d'autre : refusé
])
def test_protect_after_a_cancel_skips_the_removed_snapshot_longhorn_relists(world, leftover, ok):
    w = world
    uid = VMS["vm-a"]["uid"]
    vol = "pvc-vm-a-data"
    name = rr.op_name(w.pid, vol, 1, "first")
    w.src.put(rr.K_LH_SNAPSHOT, {"metadata": {"name": name, "namespace": rr.NS,
                                              "labels": {"longhornvolume": vol}},
                                 "spec": {"volume": vol, "createSnapshot": False, "labels": None},
                                 "status": dict({"readyToUse": False, "labels": {}}, **leftover)})
    res = rr.protect(w.ctx(), interval=300)
    if not ok:
        assert res["exit"] == 1 and res["code"] == "snapshot-taken", res
        return
    assert res["exit"] == 0, res
    first = [c[3] for c in mut_keys(w.src, "create") if c[1] == rr.K_LH_SNAPSHOT and vol in str(
        w.src.obj(rr.K_LH_SNAPSHOT, rr.NS, c[3]) or {})]
    assert first and name not in first
    assert w.src.obj(rr.K_LH_SNAPSHOT, rr.NS, first[0])["metadata"]["labels"][rp.LABEL_VM] == uid
    # une relance retrouve le même nom de remplacement : rien de nouveau
    before = len(mut_keys(w.src, "create"))
    assert rr.protect(w.ctx())["exit"] == 0
    assert len(mut_keys(w.src, "create")) == before


@pytest.mark.parametrize("error, ok", [
    # vu sur le banc : la VM arrêtée, Longhorn attache le volume lui-même
    ("failed to take snapshot because the volume engine pvc-x-e-0 is not running. "
     "Waiting for the volume to be attached", True),
    ("snapshot failed: no space left on device", False),
])
def test_planned_failover_waits_through_the_attach_error_of_a_stopped_vm(tmp_path, error, ok):
    w = protected(tmp_path, vms=("vm-a",))
    w.clock.sleep(400)
    w.src.snapshot_error = error
    res = rr.failover(w.ctx())
    if ok:
        assert res["exit"] == 0, res
        assert w.plan()["state"] == "failed_over"
    else:
        assert res["exit"] == 1 and "no space left" in res["message"], res
        assert w.plan()["state"] == "protected"
