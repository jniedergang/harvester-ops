"""harvester-ops : déroulé de la réplication asynchrone de VMs (v1.97.0).

Les opérations qui parlent aux clusters, au-dessus des décisions pures de
`replication` et du registre `replication_db` :

* relevés des deux clusters pour le contrôle (`collect_source`,
  `collect_target`), avec la place cible par groupe de disques (`pool`) ;
* mesure : `measure_start`, `measure_stop`, `measure_report`,
  `measure_formula` (sans cluster) ;
* `protect` : tâche récurrente par VM côté source, première sauvegarde,
  volumes de secours et backing images côté cible, carnet de reprise ;
* `failover` planifiée (VM arrêtée, sauvegarde finale appliquée avant
  l'activation : zéro perte) et non planifiée (source perdue : activation de
  ce qui est appliqué, écart entre disques contrôlé) ;
* `unprotect`, et `resume` qui reprend la commande interrompue d'après
  l'état rangé.

Règles tenues partout (docs/design/2026-10-24-replication-v1-plan.md) :

* chaque étape constate d'abord l'état réel et ne refait que ce qui
  manque : une commande relancée après un arrêt brutal converge, sans
  doublon (noms déterministes, étiquettes du plan) ;
* toute attente est bornée (`Ctx.timeouts`, réglable par le plan et par
  la ligne de commande) ;
* toute mutation est écrite au journal, une ligne par ressource touchée
  (`replication_db.record_event`) ;
* un objet sans nos étiquettes n'est jamais modifié ni supprimé. Seules
  exceptions, voulues : la VM source (arrêtée et redémarrée par la bascule,
  annotée), ses volumes Longhorn (étiquette de notre tâche posée et retirée)
  et les `BackupVolume` (relecture demandée par `syncRequestedAt`) ;
* annulation (`Cancelled`, levée par le gestionnaire de SIGTERM/SIGINT de
  la ligne de commande) : retour arrière de ce que la commande a créé, sauf
  après une activation, qui est définitive.

Les clusters ne sont vus qu'au travers d'un client à l'interface de
`kube.Kube` (`get`, `list`, `create(obj, dry_run)`, `replace`,
`patch(kind, ns, name, patch, dry_run)`, `delete`). Horloge, sommeil et
sortie sont injectés : tout se teste sans cluster.

Bibliothèque standard seulement (le script tourne sur un hôte airgap).
"""

import base64
import contextlib
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs

import longhorn_room
import replication as rp
import replication_db as db
import vm_transfer as vt
import vm_transfer_progress as vp
import vm_transfer_run as run
from vm_transfer_run import Fail, TransferError, wait_for

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

K_VM = run.K_VM
K_VMI = run.K_VMI
K_IMAGE = run.K_IMAGE
K_SETTING = run.K_SETTING
K_PVC = run.K_PVC
K_SECRET = run.K_SECRET
K_NS = run.K_NS
K_PV = "persistentvolumes"
K_SC = "storageclasses"
K_NAD = "network-attachment-definitions.k8s.cni.cncf.io"
K_LH_VOLUME = "volumes.longhorn.io"
K_LH_ENGINE = "engines.longhorn.io"
K_LH_BACKUP = "backups.longhorn.io"
K_LH_BV = "backupvolumes.longhorn.io"
K_LH_SNAPSHOT = "snapshots.longhorn.io"
K_LH_JOB = "recurringjobs.longhorn.io"
K_LH_BI = "backingimages.longhorn.io"
K_LH_BBI = "backupbackingimages.longhorn.io"
K_LH_NODE = "nodes.longhorn.io"
K_LH_SETTING = run.K_LH_SETTING
K_LH_TARGET = run.K_LH_TARGET

NS = rp.LONGHORN_NS
BACKUP_TARGET = "default"

EXIT_OK, EXIT_FAIL, EXIT_BLOCKED, EXIT_CANCELLED, EXIT_RPO = 0, 1, 2, 3, 4

# VM source arrêtée par une bascule : runStrategy d'origine, pour la
# redémarrer au retour arrière (même depuis une relance après arrêt brutal)
STOPPED_ANNOTATION = "harvester-ops.io/replication-stopped"
FAILED_OVER_TO = "harvester-ops.io/replication-failed-over-to"
CARNET_DIGEST = "harvester-ops.io/replication-carnet-digest"
MANAGED_BY = {"app.kubernetes.io/managed-by": "harvester-ops"}

# Délais internes, en plus de `replication.TIMEOUTS` (les helpers du
# transfert lisent `start`, `undo` et `delete`) ; `resync_nudge` : relecture
# redemandée tant qu'une sauvegarde attendue n'est pas visible
EXTRA_TIMEOUTS = {"undo": 300, "delete": 600, "resync_nudge": 30}

# messages de kubectl d'un lecteur refusé (relevés sur harv1, comme le transfert)
_DENIED = ("forbidden", "unauthorized", "provide credentials")
# erreur de dry-run d'une VM dont les PVC n'existent pas encore : à confirmer
# sur le banc (tâche 7), rendue en avertissement
_PVC_MISSING = ("persistentvolumeclaim", "not found")


class Cancelled(Exception):
    """Annulation demandée (SIGTERM/SIGINT), levée dans le déroulé."""


class Refused(Exception):
    """Refus avant toute écriture : sortie 2."""

    def __init__(self, code, message, findings=None, facts=None):
        super().__init__(message)
        self.code, self.findings, self.facts = code, findings or [], facts or {}


class RunError(Exception):
    """Échec d'une étape : `code` stable, message lisible (jamais de chemin
    ni de secret)."""

    def __init__(self, code, message, facts=None):
        super().__init__(message)
        self.code, self.facts = code, facts or {}


# ---------------------------------------------------------------------------
# Contexte
# ---------------------------------------------------------------------------

def effective_timeouts(plan, overrides=None):
    """Délais d'une commande : départ de `replication.TIMEOUTS`, puis
    réglages du plan (modifiables à chaud), puis `--timeout NOM=S`."""
    t = dict(rp.TIMEOUTS)
    t.update(((plan or {}).get("options") or {}).get("timeouts") or {})
    t.update(overrides or {})
    if not t.get("barrier"):
        t["barrier"] = max(1, int((plan or {}).get("interval_s") or 300) // 2)
    for k, v in EXTRA_TIMEOUTS.items():
        t.setdefault(k, v)
    t.setdefault("start", t["vm_start"])
    return t


class Ctx:
    """Compatible avec `vm_transfer_run.wait_for` (`emit`, `sleep`, `now`,
    `timeouts`, `created`, `record`, `forget`), plus le registre et le plan.

    `sleep` renouvelle le verrou du plan au moins tous les tiers de son
    délai : une longue attente ne le laisse pas reprendre par un autre."""

    def __init__(self, conn, plan_id, src, dst, *, emit=vt.step, sleep=time.sleep, now=time.time,
                 progress=vp.emit_line, timeouts=None, by="", action_id="", owner=None, host=None,
                 pid_alive=None, through_rancher=False):
        self.conn, self.plan_id = conn, plan_id
        self.src, self.dst = src, dst
        self.emit, self._sleep, self.now, self.progress = emit, sleep, now, progress
        self.by, self.action_id = by, action_id
        self.owner = owner or db.owner_id("cli")
        self.host, self.pid_alive = host, pid_alive
        self.through_rancher = through_rancher
        self.overrides = dict(timeouts or {})
        self.plan = db.get_plan(conn, plan_id)
        if self.plan is None:
            raise KeyError(plan_id)
        self.timeouts = effective_timeouts(self.plan, self.overrides)
        self.created = []           # (côté, kind, ns, nom) créés par cette commande
        self.undo_labels = []       # (côté, kind, ns, nom, {clé: None}) posées par elle
        self.steps = []             # [{"step", "seconds"}] pour `runs`
        self.locked = False
        self._beat = now()

    # -- compatibilité vm_transfer_run ---------------------------------------
    def kube(self, side):
        return self.src if side in ("src", "source") else self.dst

    def record(self, side, kind, ns, name):
        self.created.append((side, kind, ns, name))

    def forget(self, side, kind, ns, name):
        try:
            self.created.remove((side, kind, ns, name))
        except ValueError:
            pass

    def sleep(self, s):
        self._sleep(s)
        if self.locked and self.now() - self._beat >= db.LOCK_TTL_S / 3:
            db.lock_heartbeat(self.conn, self.plan_id, self.owner, now=self.dt())
            self._beat = self.now()

    # -- outils ---------------------------------------------------------------
    def dt(self):
        return datetime.fromtimestamp(self.now(), timezone.utc)

    def iso(self):
        return rp.iso(self.dt())

    def reload(self):
        self.plan = db.get_plan(self.conn, self.plan_id)
        self.timeouts = effective_timeouts(self.plan, self.overrides)
        return self.plan

    @property
    def gen(self):
        return int(self.plan["generation"])

    def cluster(self, side):
        return self.plan["source_cluster"] if side == "source" else self.plan["target_cluster"]

    def event(self, operation, step, uid="", side="", level="info", code="", message="", facts=None):
        return db.record_event(self.conn, operation, step=step, plan_id=self.plan_id, resource_uid=uid,
                               host=self.cluster(side) if side else "", side=side, level=level, code=code,
                               message=str(message)[:500], facts=facts, by=self.by,
                               action_id=self.action_id, now=self.dt())

    def tracker(self, phase, total):
        return vp.Progress(self.progress, phase, total, now=self.now)


def error_code(e):
    """Code stable d'une erreur : celui de RunError/Refused, `timeout` pour
    une attente dépassée, `failed` sinon."""
    code = getattr(e, "code", None)
    if code:
        return code
    return "timeout" if "timed out" in str(e) else "failed"


@contextlib.contextmanager
def stage(ctx, op, step, side, uids, message=""):
    """Une étape d'une opération : début, fin (durée) ou échec, écrits au
    journal pour chaque ressource touchée, et sur la sortie STEP_EVENT."""
    uids = [u for u in uids if u] or [""]
    t0 = ctx.now()
    ctx.emit(step, "running", message or step)
    for u in uids:
        ctx.event(op, step, u, side, code="start", message=message or step)
    try:
        yield
    except Cancelled as e:
        for u in uids:
            ctx.event(op, step, u, side, level="warn", code="cancelled", message=f"{step}: cancelled ({e})")
        ctx.emit(step, "error", "cancelled")
        raise
    except Exception as e:                      # noqa: BLE001
        code = error_code(e)
        for u in uids:
            ctx.event(op, step, u, side, level="error", code=code, message=f"{step}: {e}",
                      facts=getattr(e, "facts", None))
        ctx.emit(step, "error", str(e)[:400])
        raise
    secs = round(ctx.now() - t0, 3)
    ctx.steps.append({"step": step, "seconds": secs})
    for u in uids:
        ctx.event(op, step, u, side, code="done", message=message or step, facts={"duration_s": secs})
    ctx.emit(step, "done", message or step)


# ---------------------------------------------------------------------------
# Petites lectures
# ---------------------------------------------------------------------------

def _md(o):
    return (o or {}).get("metadata") or {}


def _labels(o):
    return _md(o).get("labels") or {}


def _spec(o):
    return (o or {}).get("spec") or {}


def _status(o):
    return (o or {}).get("status") or {}


def _owned(ctx, o):
    return rp.owned_by(_labels(o), ctx.plan_id)


def _setting_value(kube, kind, ns, name):
    s = kube.get(kind, ns, name) or {}
    return s.get("value") or s.get("default") or ""


def _target_available(kube):
    return bool(_status(kube.get(K_LH_TARGET, NS, BACKUP_TARGET)).get("available"))


def probe(ctx, side):
    """`ok`, `denied` (la cible répond mais refuse ce lecteur) ou
    `unreachable`, borné par le délai `probe` quand le client le permet."""
    kube = ctx.kube(side)
    old = getattr(kube, "timeout", None)
    try:
        if old is not None:
            kube.timeout = ctx.timeouts["probe"]
        kube.get(K_SETTING, None, "backup-target")
        return "ok"
    except Exception as e:                      # noqa: BLE001
        if any(m in str(e).lower() for m in _DENIED):
            return "denied"
        return "unreachable"
    finally:
        if old is not None:
            kube.timeout = old


def url_volume(url):
    """Volume source d'une URL de sauvegarde Longhorn (`...?backup=X&volume=Y`)."""
    q = parse_qs(str(url or "").partition("?")[2])
    return (q.get("volume") or [""])[0]


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def backups_by_volume(kube):
    """`Backup` de longhorn-system groupés par volume (`status.volumeName`),
    rangés par date d'instantané. Une seule liste par appel."""
    out = {}
    for b in kube.list(K_LH_BACKUP, NS):
        vol = _status(b).get("volumeName") or _labels(b).get("backup-volume") or ""
        out.setdefault(vol, []).append(b)
    for v in out.values():
        v.sort(key=lambda b: (_status(b).get("snapshotCreatedAt") or "", _md(b).get("name")))
    return out


def _completed(backups):
    return [b for b in backups or [] if _status(b).get("state") == "Completed"]


def engine_of(kube, volume):
    """Moteur d'un volume, trouvé par l'étiquette `longhornvolume` (jamais
    par un nom fabriqué). Celui qui a déjà restauré l'emporte."""
    items = kube.list(K_LH_ENGINE, NS, selector=f"{rp.ENGINE_VOLUME_LABEL}={volume}")
    items.sort(key=lambda e: 0 if _status(e).get("lastRestoredBackup") else 1)
    return items[0] if items else None


def find_backup_volume(kube, volume):
    """`BackupVolume` d'un volume par `spec.volumeName` et
    `spec.backupTargetName` (jamais par son nom : il diffère du volume
    depuis Longhorn 1.8)."""
    for bv in kube.list(K_LH_BV, NS):
        sp = _spec(bv)
        if sp.get("volumeName") == volume and sp.get("backupTargetName") in (None, "", BACKUP_TARGET):
            return bv
    return None


def _tick_from_backup(b, vm_uid, generation, state=None):
    st = _status(b)
    return {"generation": generation, "vm_uid": vm_uid, "source_volume": st.get("volumeName") or "",
            "backup": _md(b).get("name"), "snapshot_at": st.get("snapshotCreatedAt") or "",
            "backup_started_at": _md(b).get("creationTimestamp") or "",
            "backup_done_at": st.get("backupCreatedAt") or st.get("lastSyncedAt") or "",
            "newly_uploaded": _int(st.get("newlyUploadDataSize")), "size": _int(st.get("size")),
            "state": state or ("completed" if st.get("state") == "Completed"
                               else "error" if st.get("state") == "Error" else "in_progress")}


def op_name(plan_id, volume, generation, kind, token=""):
    """Nom déterministe d'un instantané ou d'une sauvegarde posé par la
    réplication (`first`, `final`) : une relance retrouve le même objet ; la
    bascule y ajoute l'identifiant de son exécution (`runs.id`)."""
    h = hashlib.sha256(f"{plan_id}|{volume}|{int(generation)}|{kind}|{token}".encode()).hexdigest()[:12]
    return f"hr-{h}-{kind}"


# ---------------------------------------------------------------------------
# Relevés
# ---------------------------------------------------------------------------

def _resolve_vms(ctx, kube):
    plan = ctx.plan
    if plan.get("vms"):
        out = []
        for ref in plan["vms"]:
            ns, name = ref.split("/")
            vm = kube.get(K_VM, ns, name)
            if vm is not None:
                out.append(vm)
        return out
    return kube.list(K_VM, plan["selector_namespace"], selector=plan["selector"])


def _vm_disks(kube, vm, classes):
    ns = _md(vm).get("namespace")
    out = []
    for vol in vt._tspec(vm).get("volumes") or []:
        claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
        if not claim:
            continue
        pvc = kube.get(K_PVC, ns, claim) or {}
        ps = _spec(pvc)
        pv_name = ps.get("volumeName")
        pv = kube.get(K_PV, None, pv_name) if pv_name else None
        lh_name = ((_spec(pv).get("csi") or {}).get("volumeHandle")) or pv_name or ""
        lh = kube.get(K_LH_VOLUME, NS, lh_name) if lh_name else None
        sc_name = ps.get("storageClassName")
        size = vt.parse_quantity(((ps.get("resources") or {}).get("requests") or {}).get("storage")) \
            or _int(_spec(lh).get("size"))
        out.append({"disk": vol.get("name"), "claim": claim, "size": size, "storage_class": sc_name,
                    "image": (_md(pvc).get("annotations") or {}).get(rp.IMAGE_ID_ANNOTATION) or None,
                    "lh_volume": lh, "pvc": pvc, "sc": classes.get(sc_name), "source_volume": lh_name,
                    "access_modes": list(ps.get("accessModes") or ["ReadWriteMany"]),
                    "volume_mode": ps.get("volumeMode") or "Block"})
    return out


def collect_source(ctx):
    """Faits de la source pour `replication.check` (forme dans sa doc)."""
    src = ctx.src
    st = probe(ctx, "src")
    facts = {"cluster": ctx.plan["source_cluster"], "reachable": st == "ok", "denied": st == "denied"}
    if st != "ok":
        return facts
    classes = {_md(s).get("name"): s for s in src.list(K_SC)}
    jobs = src.list(K_LH_JOB, NS)
    facts.update(
        backup_target=_setting_value(src, K_SETTING, None, "backup-target"),
        backup_target_available=_target_available(src),
        longhorn_version=_setting_value(src, K_LH_SETTING, NS, "current-longhorn-version"),
        jobs={_md(j)["name"]: {"task": _spec(j).get("task"), "groups": list(_spec(j).get("groups") or [])}
              for j in jobs},
        recurring_jobs={_md(j)["name"]: _labels(j) for j in jobs})
    vms, bis = [], {}
    for vm in _resolve_vms(ctx, src):
        md = _md(vm)
        disks = _vm_disks(src, vm, classes)
        vms.append({"uid": md.get("uid"), "namespace": md.get("namespace"), "name": md.get("name"), "vm": vm,
                    "firmware_uuid": rp.firmware_uuid(vm), "disks": disks})
        for d in disks:
            bi = _spec(d["lh_volume"]).get("backingImage")
            if bi and bi not in bis:
                o = src.get(K_LH_BI, NS, bi) or {}
                bis[bi] = {"checksum": _spec(o).get("checksum") or _status(o).get("checksum") or "",
                           "size": _int(_status(o).get("size"))}
    facts["vms"] = vms
    facts["backing_images"] = bis
    return facts


def class_pool(sc):
    """Groupe de disques d'une classe Longhorn : ses sélecteurs de disques
    et de nœuds. Deux classes aux mêmes sélecteurs puisent dans les mêmes
    disques (`replication._capacity`)."""
    p = (sc or {}).get("parameters") or {}

    def sel(v):
        return ",".join(sorted(x.strip() for x in str(v or "").split(",") if x.strip()))
    return f"disk={sel(p.get('diskSelector'))};node={sel(p.get('nodeSelector'))}"


def _longhorn_numbers(kube):
    out = {}
    for name, default in (("storage-over-provisioning-percentage", 200.0),
                          ("storage-minimal-available-percentage", 25.0)):
        try:
            out[name] = float((kube.get(K_LH_SETTING, NS, name) or {}).get("value"))
        except (TypeError, ValueError):
            out[name] = default
    return (kube.list(K_LH_NODE, NS), out["storage-over-provisioning-percentage"],
            out["storage-minimal-available-percentage"])


def collect_target(ctx, namespaces=()):
    """Faits de la cible pour `replication.check`, plus `classes_raw` (les
    paramètres de chaque classe, recopiés sur les volumes de secours)."""
    dst = ctx.dst
    st = probe(ctx, "dst")
    facts = {"cluster": ctx.plan["target_cluster"], "reachable": st != "unreachable", "denied": st == "denied"}
    if st != "ok":
        return facts
    scs = dst.list(K_SC)
    nodes, over, minimal = _longhorn_numbers(dst)
    room = longhorn_room.storage_room(nodes, scs, over, minimal)
    nodes_ok = room["schedulable_nodes"]
    classes, raw = {}, {}
    for sc in scs:
        n = _md(sc).get("name")
        if n not in room["classes"]:
            continue
        raw[n] = sc
        info = dict(room["classes"][n], nodes=nodes_ok, pool=class_pool(sc))
        if info["replicas"] > nodes_ok > 0:
            fake = dict(sc, parameters=dict(sc.get("parameters") or {}, numberOfReplicas=str(nodes_ok)))
            info["degraded_allocatable"] = longhorn_room.storage_room(
                nodes, [fake], over, minimal)["classes"][n]["allocatable"]
        classes[n] = info
    default_sc = next((_md(s)["name"] for s in scs if (_md(s).get("annotations") or {}).get(
        "storageclass.kubernetes.io/is-default-class") == "true"), None)
    vms = dst.list(K_VM)
    macs, vm_labels = {}, {}
    for v in vms:
        key = f"{_md(v).get('namespace')}/{_md(v).get('name')}"
        vm_labels[key] = _labels(v)
        for m in vt.vm_macs(v):
            macs[m] = key
    pvcs = {}
    for ns in sorted(set(namespaces)):
        for p in dst.list(K_PVC, ns):
            pvcs[f"{ns}/{_md(p)['name']}"] = _labels(p)
    facts.update(
        backup_target=_setting_value(dst, K_SETTING, None, "backup-target"),
        backup_target_available=_target_available(dst),
        longhorn_version=_setting_value(dst, K_LH_SETTING, NS, "current-longhorn-version"),
        networks=[f"{_md(n).get('namespace')}/{_md(n).get('name')}" for n in dst.list(K_NAD)],
        namespaces=[_md(n)["name"] for n in dst.list(K_NS)],
        storage_classes=classes, classes_raw=raw, default_storage_class=default_sc,
        images={f"{_md(i).get('namespace')}/{_md(i).get('name')}":
                {"storage_class": _status(i).get("storageClassName")} for i in dst.list(K_IMAGE)},
        backing_images={_md(b)["name"]: {"checksum": _spec(b).get("checksum") or _status(b).get("checksum"),
                                          "labels": _labels(b)} for b in dst.list(K_LH_BI, NS)},
        macs=macs, vms=vm_labels,
        volumes={_md(v)["name"]: _labels(v) for v in dst.list(K_LH_VOLUME, NS)},
        carnets={f"{rp.CARNET_NAMESPACE}/{_md(s)['name']}": _labels(s)
                 for s in dst.list(K_SECRET, rp.CARNET_NAMESPACE)},
        pvcs=pvcs, through_rancher=bool(ctx.through_rancher))
    return facts


def build_works(ctx, src_facts, dst_facts):
    """Le travail par VM : noms cibles, classe, paramètres, PVC cibles."""
    plan = ctx.plan
    pid, gen = ctx.plan_id, ctx.gen
    maps = plan.get("mappings") or {}
    works = []
    for vm in src_facts.get("vms") or []:
        res = {"resource_uid": vm["uid"], "namespace": vm["namespace"], "name": vm["name"]}
        tns, tname = rp.target_ref(plan, res)
        w = {"uid": vm["uid"], "namespace": vm["namespace"], "name": vm["name"], "vm": vm["vm"],
             "firmware_uuid": vm.get("firmware_uuid") or "", "tns": tns, "tname": tname,
             "job": rp.recurring_job_name(pid, vm["uid"]), "disks": []}
        for d in vm.get("disks") or []:
            sc, _f = rp._target_class(d, plan, dst_facts)
            params = ((dst_facts.get("classes_raw") or {}).get(sc) or {}).get("parameters") or {}
            spec = _spec(d.get("lh_volume"))
            image = d.get("image")
            mapped = (maps.get("images") or {}).get(image) if image else None
            w["disks"].append(dict(
                d, target_volume=rp.target_volume_name(pid, vm["uid"], d["disk"], gen, vm["name"]),
                target_claim=rp.target_claim(plan, vm["namespace"], d["claim"]), target_class=sc,
                class_params=params, target_replicas=_int(params.get("numberOfReplicas")) or 3,
                access_mode=spec.get("accessMode") or "rwx", migratable=bool(spec.get("migratable")),
                backing_image=spec.get("backingImage") or "", image_id=mapped))
        works.append(w)
    return works


def _record_works(ctx, works, state):
    """Ressources et volumes rangés ; ressource passée à `state`."""
    plan = ctx.plan
    for w in works:
        db.upsert_resource(ctx.conn, ctx.plan_id, {
            "generation": ctx.gen, "resource_uid": w["uid"], "source_host": plan["source_cluster"],
            "target_host": plan["target_cluster"], "namespace": w["namespace"], "name": w["name"],
            "target_namespace": w["tns"], "target_name": w["tname"],
            "vm_generation": rp.vm_spec_generation(w["vm"]),
            "vm_spec_digest": rp.vm_spec_digest(w["vm"], [{"disk": d["disk"], "size": d["size"],
                                                          "storage_class": d["storage_class"]}
                                                         for d in w["disks"]]),
            "firmware_uuid": w["firmware_uuid"]}, now=ctx.dt())
        db.set_resource_state(ctx.conn, ctx.plan_id, w["uid"], state, generation=ctx.gen, now=ctx.dt(),
                              by=ctx.by, action_id=ctx.action_id)
        for d in w["disks"]:
            db.upsert_volume(ctx.conn, ctx.plan_id, {
                "generation": ctx.gen, "vm_uid": w["uid"], "disk": d["disk"], "claim": d["claim"],
                "source_volume": d["source_volume"], "size": d["size"], "access_mode": d["access_mode"],
                "migratable": d["migratable"], "backing_image": d["backing_image"],
                "target_volume": d["target_volume"], "target_class": d["target_class"] or "",
                "target_replicas": d["target_replicas"]})


# ---------------------------------------------------------------------------
# Verrou, état, résultats
# ---------------------------------------------------------------------------

def _result(ctx, code, exit_code, message="", **extra):
    out = {"exit": exit_code, "plan": ctx.plan_id, "state": (ctx.reload() or {}).get("state"),
           "code": code, "message": message}
    out.update(extra)
    return out


def _refusal(ctx, op, e, uids=()):
    for u in list(uids) or [""]:
        ctx.event(op, "check", u, level="warn", code=e.code, message=str(e),
                  facts={"blocking": [f["code"] for f in e.findings if f.get("level") == "block"]} if e.findings
                  else e.facts)
    ctx.emit("check", "error", str(e))
    return _result(ctx, e.code, EXIT_BLOCKED, str(e), findings=e.findings, facts=e.facts)


def _precondition(ctx, command):
    plan = ctx.reload()
    if plan["state"] not in rp.COMMAND_FROM[command]:
        raise Refused("state", f"{command} cannot start from state {plan['state']}",
                      facts={"state": plan["state"]})
    if plan["state"] == "error":
        origin = db.error_origin(ctx.conn, ctx.plan_id)
        if rp.ACTION_STATE.get(command, command) not in rp.error_exits(origin):
            raise Refused("state", f"{command} cannot leave error (origin {origin})",
                          facts={"state": "error", "origin": origin})


def _lock(ctx, command):
    try:
        db.acquire_lock(ctx.conn, ctx.plan_id, command, ctx.owner, now=ctx.dt(),
                        **({"pid_alive": ctx.pid_alive} if ctx.pid_alive else {}),
                        **({"host": ctx.host} if ctx.host else {}))
    except db.Locked as e:
        raise Refused("locked", f"another command holds the plan: {e.holder.get('command')}",
                      facts={"command": e.holder.get("command")}) from None
    ctx.locked = True
    ctx._beat = ctx.now()


def _unlock(ctx):
    if ctx.locked:
        db.release_lock(ctx.conn, ctx.plan_id, ctx.owner)
        ctx.locked = False


def _set_state(ctx, new, message=""):
    cur = ctx.reload()["state"]
    db.set_state(ctx.conn, ctx.plan_id, cur, new, now=ctx.dt(), by=ctx.by, action_id=ctx.action_id,
                 message=message)
    return ctx.reload()


def _set_resources(ctx, uids, state, message=""):
    for u in uids:
        r = db.get_resource(ctx.conn, ctx.plan_id, u, ctx.gen)
        if r is None or r["state"] == state:
            continue
        db.set_resource_state(ctx.conn, ctx.plan_id, u, state, generation=ctx.gen, now=ctx.dt(),
                              by=ctx.by, action_id=ctx.action_id, message=message)


def _undo_created(ctx):
    """Retour arrière de ce que la commande a posé : étiquettes retirées,
    objets créés supprimés, dans l'ordre inverse. Rend ce qui reste."""
    left = []
    for side, kind, ns, name, patch in reversed(ctx.undo_labels):
        try:
            ctx.kube(side).patch(kind, ns, name, {"metadata": {"labels": patch}})
        except Exception:                       # noqa: BLE001
            left.append(f"labels of {kind} {name}")
    ctx.undo_labels.clear()
    for side, kind, ns, name in reversed(list(ctx.created)):
        if not run._undo_delete(ctx, ctx.kube(side), kind, ns, name):
            left.append(f"{kind} {ns + '/' if ns else ''}{name}")
    ctx.created.clear()
    return left


# ---------------------------------------------------------------------------
# Briques Longhorn
# ---------------------------------------------------------------------------

def _label_volume(ctx, volume, job, keep_default):
    """Étiquette de notre tâche (et du groupe `default` quand le volume y
    était implicitement, voir `replication.backup_membership`)."""
    lv = ctx.src.get(K_LH_VOLUME, NS, volume)
    if lv is None:
        raise RunError("source-volume-missing", f"Longhorn volume {volume} not found on the source")
    labels = _labels(lv)
    want = dict([rp.job_label(job)])
    if keep_default:
        want[rp.JOB_GROUP_LABEL_PREFIX + rp.DEFAULT_GROUP] = "enabled"
    add = {k: v for k, v in want.items() if labels.get(k) != v}
    if not add:
        return False
    ctx.src.patch(K_LH_VOLUME, NS, volume, {"metadata": {"labels": add}})
    ctx.undo_labels.append(("src", K_LH_VOLUME, NS, volume, {k: None for k in add}))
    return True


def _unlabel_volume(ctx, volume, job):
    """Retire notre étiquette de tâche (l'étiquette du groupe `default`
    reste : c'est l'appartenance implicite d'avant, rendue explicite)."""
    lv = ctx.src.get(K_LH_VOLUME, NS, volume)
    key = rp.job_label(job)[0]
    if lv is None or key not in _labels(lv):
        return False
    ctx.src.patch(K_LH_VOLUME, NS, volume, {"metadata": {"labels": {key: None}}})
    return True


def _ensure(ctx, side, obj, what):
    """Crée `obj` s'il manque ; le garde s'il existe avec nos étiquettes ;
    refuse s'il existe sans elles. Rend (objet, créé)."""
    kube = ctx.kube(side)
    md = obj["metadata"]
    kind = _kind_of(obj)
    cur = kube.get(kind, md.get("namespace"), md["name"])
    if cur is not None:
        if not _owned(ctx, cur):
            raise RunError(f"{what}-taken", f"{what} {md['name']} exists without the labels of this plan")
        return cur, False
    out = kube.create(obj)
    ctx.record(side, kind, md.get("namespace"), md["name"])
    return out, True


_KINDS = {"RecurringJob": K_LH_JOB, "Volume": K_LH_VOLUME, "Snapshot": K_LH_SNAPSHOT, "Backup": K_LH_BACKUP,
          "BackingImage": K_LH_BI, "PersistentVolume": K_PV, "PersistentVolumeClaim": K_PVC,
          "Secret": K_SECRET, "Namespace": K_NS, "VirtualMachine": K_VM}


def _kind_of(obj):
    return _KINDS[obj["kind"]]


def take_backups(ctx, op, items, kind, token=""):
    """Instantané puis sauvegarde de chaque volume (`items` : [(uid,
    volume)]), noms déterministes ; rend {volume: nom de la sauvegarde}."""
    labels = rp.plan_labels(ctx.plan_id, ctx.gen)
    names = {vol: op_name(ctx.plan_id, vol, ctx.gen, kind, token) for _u, vol in items}
    for uid, vol in items:
        _ensure(ctx, "src", rp.snapshot(vol, names[vol], labels=dict(labels, **{rp.LABEL_VM: uid})), "snapshot")

    def ready():
        for _u, vol in items:
            s = ctx.src.get(K_LH_SNAPSHOT, NS, names[vol])
            if s is None:
                return Fail(f"snapshot {names[vol]} disappeared")
            if _status(s).get("error"):
                return Fail(f"snapshot {names[vol]}: {_status(s)['error']}")
            if not _status(s).get("readyToUse"):
                return "snapshots being taken"
        return True
    wait_for(ctx, op, "snapshots ready", ready, ctx.timeouts["snapshot"], every=2)
    for uid, vol in items:
        bv = find_backup_volume(ctx.src, vol)
        _ensure(ctx, "src", rp.backup(vol, names[vol], backup_volume=_md(bv).get("name") or vol,
                                      target=BACKUP_TARGET, labels=dict(labels, **{rp.LABEL_VM: uid})),
                "backup")
    return names


def wait_backups(ctx, op, names, timeout_key):
    """Attend que chaque sauvegarde nommée soit `Completed` (une sauvegarde
    en erreur arrête). Rend {volume: objet Backup}."""
    out = {}

    def done():
        for vol, name in names.items():
            b = ctx.src.get(K_LH_BACKUP, NS, name)
            st = _status(b)
            if st.get("state") == "Error":
                return Fail(f"backup {name}: {st.get('error') or st.get('messages') or 'error'}")
            if st.get("state") != "Completed":
                return f"backup of {vol}: {st.get('progress') or 0}%"
            out[vol] = b
        return True
    wait_for(ctx, op, "backups completed", done, ctx.timeouts[timeout_key], every=2)
    return out


def request_resync(ctx, volumes, nudge_target=True):
    """Relecture forcée côté cible : `syncRequestedAt` de chaque
    `BackupVolume` (le seul objet sans nos étiquettes que la réplication
    écrit), et de la cible entière pour un volume pas encore connu."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ctx.now()))
    missing = False
    for vol in volumes:
        bv = find_backup_volume(ctx.dst, vol)
        if bv is None:
            missing = True
            continue
        ctx.dst.patch(K_LH_BV, NS, _md(bv)["name"], {"spec": {"syncRequestedAt": stamp}})
    if missing and nudge_target:
        ctx.dst.patch(K_LH_TARGET, NS, BACKUP_TARGET, {"spec": {"syncRequestedAt": stamp}})


def wait_synced(ctx, op, volumes, deadline):
    """Relecture demandée puis constatée (`status.lastSyncedAt` du
    `BackupVolume` postérieur à la demande), au plus jusqu'à `deadline` :
    la bascule non planifiée ne peut pas demander à la source quelle est sa
    dernière sauvegarde, elle prend la dernière que la cible a vue."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ctx.now()))
    request_resync(ctx, volumes)

    def synced():
        late = []
        for vol in volumes:
            bv = find_backup_volume(ctx.dst, vol)
            if bv is None or (_status(bv).get("lastSyncedAt") or "") < stamp:
                late.append(vol)
        return True if not late else f"{len(late)} backup volume(s) not synced yet"
    try:
        wait_for(ctx, op, "backup volumes synced", synced, max(1, deadline - ctx.now()), every=1)
        return True
    except TransferError:
        ctx.event("failover", op, level="warn", code="sync-late",
                  message="the target did not confirm its last sync in time: taking what it has")
        return False


def _snap_time(b):
    return _status(b).get("snapshotCreatedAt") or ""


def wait_applied(ctx, op, wanted, timeout_key, fail_on_timeout=True, on_poll=None):
    """Attend que le moteur de chaque volume de secours ait appliqué la
    sauvegarde voulue (ou une plus récente du même volume). `wanted` :
    {volume de secours: (volume source, nom de la sauvegarde)}. La relecture
    est redemandée tant qu'une sauvegarde manque côté cible. Rend
    {volume de secours: sauvegarde appliquée}."""
    last_nudge = [None]
    applied = {}

    def check():
        now = ctx.now()
        if on_poll:
            on_poll()
        tb = backups_by_volume(ctx.dst)
        behind = []
        for sb, (vol, name) in wanted.items():
            want = next((b for b in tb.get(vol, []) if _md(b).get("name") == name), None)
            eng = engine_of(ctx.dst, sb)
            lv = ctx.dst.get(K_LH_VOLUME, NS, sb)
            if _status(lv).get("robustness") == "faulted":
                return Fail(f"standby volume {sb} is faulted")
            last = _status(eng).get("lastRestoredBackup") or ""
            for st in (_status(eng).get("restoreStatus") or {}).values():
                if (st or {}).get("error"):
                    return Fail(f"restore of {sb}: {st['error']}")
            got = next((b for b in tb.get(vol, []) if _md(b).get("name") == last), None)
            if want is not None and last and (last == name or (got and _snap_time(got) > _snap_time(want))):
                applied[sb] = last
                continue
            behind.append(sb)
            if want is None:
                applied.pop(sb, None)
        if not behind:
            return True
        if last_nudge[0] is None or now - last_nudge[0] >= ctx.timeouts["resync_nudge"]:
            request_resync(ctx, [wanted[sb][0] for sb in behind])
            last_nudge[0] = now
        return f"{len(behind)} volume(s) not applied yet"
    try:
        wait_for(ctx, op, "backups applied on the standby volumes", check, ctx.timeouts[timeout_key], every=2)
    except TransferError:
        if fail_on_timeout:
            raise
    return applied


def activate(ctx, op, volumes):
    """Activation (définitive) : `activation_patch` sur chaque volume encore
    en attente, puis attente `isStandby == false` et `detached`."""
    for sb in volumes:
        lv = ctx.dst.get(K_LH_VOLUME, NS, sb)
        if lv is None:
            raise RunError("standby-missing", f"standby volume {sb} not found on the target")
        if not _owned(ctx, lv):
            raise RunError("target-volume-taken", f"volume {sb} has not the labels of this plan")
        if _spec(lv).get("Standby"):
            ctx.dst.patch(K_LH_VOLUME, NS, sb, rp.activation_patch())

    def active():
        for sb in volumes:
            st = _status(ctx.dst.get(K_LH_VOLUME, NS, sb))
            if st.get("isStandby") is not False or st.get("state") != "detached":
                return f"{sb} not active yet"
        return True
    wait_for(ctx, op, "volumes activated", active, ctx.timeouts["activate"], every=1)


def _is_activated(lv):
    return lv is not None and _spec(lv).get("Standby") is False


# ---------------------------------------------------------------------------
# Carnet de reprise
# ---------------------------------------------------------------------------

def _b64(text):
    return base64.b64encode(text.encode()).decode()


def carnet_manifest(plan_id, generation, work, vm, secrets, volumes, source_cluster):
    """Le carnet d'une VM : Secret `hr-<plan>-<vm>` dans CARNET_NAMESPACE,
    avec `vm.json` (VM cible déjà réécrite), `secrets.json` (secrets
    cloud-init de la source), `volumes.json` (volumes de secours, PVC,
    classes, tailles, modes, image) et `meta.json`. Son empreinte
    (annotation) dit s'il est à jour. Jamais dans replication.db."""
    meta = {"plan_id": plan_id, "generation": int(generation), "vm_uid": work["uid"],
            "source_cluster": source_cluster, "namespace": work["namespace"], "name": work["name"],
            "target_namespace": work["tns"], "target_name": work["tname"]}
    docs = {"meta.json": meta, "vm.json": vm, "secrets.json": secrets, "volumes.json": volumes}
    raw = {k: json.dumps(v, sort_keys=True) for k, v in docs.items()}
    digest = hashlib.sha256("\n".join(f"{k}={raw[k]}" for k in sorted(raw)).encode()).hexdigest()[:32]
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": rp.carnet_name(plan_id, work["uid"]), "namespace": rp.CARNET_NAMESPACE,
                         "labels": rp.plan_labels(plan_id, generation, work["uid"]),
                         "annotations": {CARNET_DIGEST: digest}},
            "data": {k: _b64(v) for k, v in raw.items()}}


def read_carnet(secret):
    """Le carnet décodé : {"meta", "vm", "secrets", "volumes"}."""
    data = (secret or {}).get("data") or {}
    out = {}
    for k in ("meta", "vm", "secrets", "volumes"):
        try:
            out[k] = json.loads(base64.b64decode(data.get(f"{k}.json") or "").decode() or "null")
        except ValueError:
            out[k] = None
    return out


def carnet_volumes(work):
    return [{"disk": d["disk"], "source_volume": d["source_volume"], "target_volume": d["target_volume"],
             "claim": d["claim"], "target_claim": d["target_claim"], "size": int(d["size"]),
             "volume_mode": d["volume_mode"], "access_modes": d["access_modes"],
             "storage_class": d["target_class"], "image_id": d.get("image_id"),
             "backing_image": d.get("backing_image") or ""} for d in work["disks"]]


def write_carnets(ctx, works):
    """Un carnet par VM, réécrit seulement si son empreinte a changé."""
    dst = ctx.dst
    if dst.get(K_NS, None, rp.CARNET_NAMESPACE) is None:
        # namespace commun à tous les plans : jamais supprimé
        dst.create({"apiVersion": "v1", "kind": "Namespace",
                    "metadata": {"name": rp.CARNET_NAMESPACE, "labels": dict(MANAGED_BY)}})
    for w in works:
        with stage(ctx, "protect", "carnet", "target", [w["uid"]]):
            secrets = []
            for name in vt.vm_inventory(w["vm"], {})["secrets"]:
                s = ctx.src.get(K_SECRET, w["namespace"], name)
                if s is None:
                    raise RunError("cloud-init-secret-missing", f"cloud-init secret {name} not found")
                secrets.append({"name": name, "type": s.get("type") or "Opaque", "data": dict(s.get("data") or {})})
            res = {"resource_uid": w["uid"], "namespace": w["namespace"], "name": w["name"],
                   "firmware_uuid": w["firmware_uuid"]}
            vm, _removed = rp.target_vm(w["vm"], res, ctx.plan.get("mappings") or {}, ctx.plan.get("options") or {},
                                        {d["claim"]: d["target_claim"] for d in w["disks"]}, ctx.plan_id, ctx.gen)
            m = carnet_manifest(ctx.plan_id, ctx.gen, w, vm, secrets, carnet_volumes(w), ctx.plan["source_cluster"])
            cur = dst.get(K_SECRET, rp.CARNET_NAMESPACE, m["metadata"]["name"])
            if cur is None:
                dst.create(m)
                ctx.record("dst", K_SECRET, rp.CARNET_NAMESPACE, m["metadata"]["name"])
            elif not _owned(ctx, cur):
                raise RunError("carnet-taken", f"carnet {m['metadata']['name']} exists without our labels")
            elif (_md(cur).get("annotations") or {}).get(CARNET_DIGEST) != m["metadata"]["annotations"][CARNET_DIGEST]:
                m["metadata"]["resourceVersion"] = _md(cur).get("resourceVersion")
                dst.replace(m)


# ---------------------------------------------------------------------------
# Mesure
# ---------------------------------------------------------------------------

def _measure_interval(conn, plan):
    for e in db.plan_events(conn, plan["id"], limit=200):
        if e.get("operation") == "measure" and e.get("step") == "start" and e.get("code") == "measuring":
            v = (e.get("facts") or {}).get("interval_s")
            if v:
                return int(v)
    return int(plan.get("interval_s") or 300)


def measure_start(ctx, interval=300):
    """`draft` -> `measuring` : tâches de sauvegarde posées sur les volumes
    source, rien sur la cible. La tâche porte le nom de celle de la
    protection : elle le deviendra (cron changé), et le `BackupVolume`
    ouvert par la première sauvegarde (complète) sert ensuite à
    l'incrémental."""
    try:
        _precondition(ctx, "measure-start")
        interval = rp.parse_duration(interval)
        if interval not in rp.CRON_STEPS or interval > 3600:
            raise Refused("bad-interval", "measure interval: a divisor of the hour, from 1 min")
        src = collect_source(ctx)
        if not src.get("reachable"):
            raise Refused("source-unreachable", "the source cluster does not answer")
        works = build_works(ctx, src, {})
        if not works:
            raise Refused("no-vm", "no VM matches the plan")
        blocks = [f for w in works for d in w["disks"]
                  for f in rp.volume_findings(d["lh_volume"], d["pvc"], d["sc"]) if f["level"] == "block"]
        if blocks:
            raise Refused("check", "a disk cannot be replicated", findings=blocks)
        _lock(ctx, "measure-start")
    except Refused as e:
        return _refusal(ctx, "measure", e)
    except Cancelled as e:
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e))
    uids = [w["uid"] for w in works]
    try:
        _set_state(ctx, "measuring")
        ctx.event("measure", "start", code="measuring", facts={"interval_s": interval})
        _record_works(ctx, works, "measuring")
        _ensure_jobs(ctx, "measure", works, rp.cron_for(interval), ctx.plan["retain"], src.get("jobs") or {})
        _unlock(ctx)
        return _result(ctx, "measuring", EXIT_OK, f"measuring every {interval} s", interval_s=interval,
                       vms=len(uids))
    except Cancelled as e:
        _undo_created(ctx)
        _error(ctx, uids, f"measure start cancelled: {e}")
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e))
    except Exception as e:                      # noqa: BLE001
        _error(ctx, uids, f"measure start failed: {e}")
        return _result(ctx, error_code(e), EXIT_FAIL, str(e))


def collect_measures(ctx):
    """Range les sauvegardes des volumes du plan comme passages de mesure
    (idempotent). Rend le nombre de passages lus."""
    vols = {v["source_volume"]: v for v in db.list_volumes(ctx.conn, ctx.plan_id, ctx.gen)}
    n = 0
    for vol, bs in backups_by_volume(ctx.src).items():
        if vol not in vols:
            continue
        for b in bs:
            if _status(b).get("state") not in ("Completed", "Error"):
                continue
            t = _tick_from_backup(b, vols[vol]["vm_uid"], ctx.gen)
            db.record_measure(ctx.conn, ctx.plan_id, t)
            n += 1
    return n


def measure_stop(ctx):
    """`measuring` -> `draft` : passages relevés, étiquettes retirées,
    tâches supprimées (seulement celles du plan)."""
    try:
        _precondition(ctx, "measure-stop")
        if probe(ctx, "src") != "ok":
            raise Refused("source-unreachable", "the source cluster does not answer")
        _lock(ctx, "measure-stop")
    except Refused as e:
        return _refusal(ctx, "measure", e)
    res = db.list_resources(ctx.conn, ctx.plan_id, ctx.gen)
    uids = [r["resource_uid"] for r in res]
    try:
        n = collect_measures(ctx)
        _remove_jobs(ctx, "measure", res)
        _set_resources(ctx, uids, "draft")
        _set_state(ctx, "draft")
        _unlock(ctx)
        return _result(ctx, "draft", EXIT_OK, "measure stopped", passes=n)
    except Exception as e:                      # noqa: BLE001
        _error(ctx, uids, f"measure stop failed: {e}")
        return _result(ctx, error_code(e), EXIT_FAIL, str(e))


def _epoch(v):
    return rp._t(v).timestamp()


def measure_report(conn, plan_id, margin=rp.DEFAULT_MARGIN):
    """Rapport de mesure, sans cluster. Par VM et par pas candidat
    (multiples du pas mesuré) : données envoyées (moyenne, p95, majorant),
    durée d'envoi, RPO atteignable, tenable ou non ; intervalle conseillé
    pour l'objectif du plan (`derive_interval`).

    Hypothèse écrite : sans volume de secours, l'application côté secours se
    mesure pas ; elle est prise égale à l'envoi au même débit (remplacée par
    la mesure réelle dès la protection). La première sauvegarde de chaque
    volume (complète) donne le débit et la durée de la première copie, pas
    les passages."""
    plan = db.get_plan(conn, plan_id)
    gen = int(plan["generation"])
    step = _measure_interval(conn, plan)
    vols = db.list_volumes(conn, plan_id, gen)
    sizes = {}
    for v in vols:
        sizes.setdefault(v["vm_uid"], 0)
        sizes[v["vm_uid"]] += int(v.get("size") or 0)
    ms = [m for m in db.list_ticks(conn, plan_id, gen, table="measures") if m.get("state") == "completed"]
    out = {"plan": plan_id, "measured_step_s": step, "objective_s": plan["rpo_objective_s"],
           "assumption": "apply-equals-send", "vms": [], "recommended_interval_s": None}
    choices = []
    for uid in sorted(sizes):
        mine = sorted((m for m in ms if m.get("vm_uid") == uid), key=lambda m: m.get("snapshot_at") or "")
        first, rest = {}, []
        for m in mine:
            if m["source_volume"] not in first:
                first[m["source_volume"]] = m
            else:
                rest.append(m)
        moved = sec = 0.0
        for m in mine:
            if m.get("backup_started_at") and m.get("backup_done_at"):
                d = _epoch(m["backup_done_at"]) - _epoch(m["backup_started_at"])
                if d > 0:
                    moved += int(m.get("newly_uploaded") or 0)
                    sec += d
        bps = moved / sec if sec > 0 else None
        slots = {}
        for m in rest:
            if m.get("snapshot_at"):
                k = math.floor(_epoch(m["snapshot_at"]) / step)
                slots[k] = slots.get(k, 0) + int(m.get("newly_uploaded") or 0)
        samples = [slots[k] for k in sorted(slots)]
        entry = {"vm_uid": uid, "passes": len(samples), "throughput_bps": bps,
                 "first_copy_bytes": sum(int(m.get("newly_uploaded") or 0) for m in first.values()),
                 "first_copy_eta_s": None, "steps": [], "recommended_interval_s": None, "tenable": None}
        if bps:
            entry["first_copy_eta_s"] = rp.first_copy_eta(entry["first_copy_bytes"], bps)
        if not samples or not bps:
            entry["insufficient"] = True
            out["vms"].append(entry)
            continue
        est = rp.StepEstimator(step, samples, bps, cap=sizes[uid] or None)
        for s in rp.CRON_STEPS:
            if s % step:
                continue
            factor = s // step
            agg = rp.aggregate_upper(samples, factor, sizes[uid] or None)
            send = est(s)
            rpo = s + 2 * send
            entry["steps"].append({"step_s": s, "mean_bytes": agg["mean"], "p95_bytes": agg["p95"],
                                   "upper_bytes": agg["max"], "estimate_bytes": est.bytes(s), "send_s": send,
                                   "apply_s": send, "rpo_s": rpo,
                                   "tenable": rpo <= plan["rpo_objective_s"] and 2 * send <= s * (1 - margin)})
        choice = rp.derive_interval(plan["rpo_objective_s"], est, est, margin)
        entry.update(recommended_interval_s=choice.step, rpo_s=choice.rpo, tenable=choice.tenable,
                     reason=choice.reason)
        choices.append(choice)
        out["vms"].append(entry)
    if choices:
        # la VM la plus exigeante décide
        worst = min(choices, key=lambda c: (c.step, -c.rpo))
        out.update(recommended_interval_s=worst.step, rpo_s=max(c.rpo for c in choices),
                   tenable=all(c.tenable for c in choices))
    return out


def measure_formula(size, writes, interval, block=rp.BLOCK_SIZE, throughput=None, write_size=4096):
    """Prévision sans cluster, quand on ne connaît que des compteurs
    d'écritures : bornes de `blocks_forecast` et, avec un débit, durée
    d'envoi et RPO atteignable (application prise égale à l'envoi)."""
    f = rp.blocks_forecast(size, writes, block, write_size)
    out = {"interval_s": int(interval), "forecast": f, "throughput_bps": throughput, "assumption": "apply-equals-send"}
    if throughput:
        for k in ("sequential", "scattered_estimate", "worst_case"):
            send = f[f"{k}_bytes"] / float(throughput)
            out[k] = {"send_s": send, "rpo_s": int(interval) + 2 * send}
    return out


# ---------------------------------------------------------------------------
# Protection
# ---------------------------------------------------------------------------

def _ensure_jobs(ctx, op, works, cron, retain, jobs):
    """Une tâche par VM (`concurrency` = nombre de disques) et son étiquette
    sur chaque volume. Une tâche du plan déjà là (mesure, relance) est mise
    à jour, jamais doublée."""
    for w in works:
        with stage(ctx, op, "recurring-job", "source", [w["uid"]], f"recurring job {w['job']} ({cron})"):
            want = rp.recurring_job(w["job"], cron, retain, max(1, len(w["disks"])), ctx.plan_id, w["uid"], ctx.gen)
            cur, created = _ensure(ctx, "src", want, "recurring-job")
            if not created:
                sp = _spec(cur)
                diff = {k: want["spec"][k] for k in ("cron", "retain", "concurrency", "labels")
                        if sp.get(k) != want["spec"][k]}
                if diff:
                    ctx.src.patch(K_LH_JOB, NS, w["job"], {"spec": diff})
            for d in w["disks"]:
                keep = rp.backup_membership(d["lh_volume"], jobs, w["job"])["keep_default_label"]
                _label_volume(ctx, d["source_volume"], w["job"], keep)


def _remove_jobs(ctx, op, resources):
    for r in resources:
        job = rp.recurring_job_name(ctx.plan_id, r["resource_uid"])
        with stage(ctx, op, "remove-job", "source", [r["resource_uid"]], f"recurring job {job} removed"):
            key = rp.job_label(job)[0]
            for lv in ctx.src.list(K_LH_VOLUME, NS, selector=f"{key}=enabled"):
                ctx.src.patch(K_LH_VOLUME, NS, _md(lv)["name"], {"metadata": {"labels": {key: None}}})
            cur = ctx.src.get(K_LH_JOB, NS, job)
            if cur is not None and _owned(ctx, cur):
                ctx.src.delete(K_LH_JOB, NS, job)
    _remove_snapshots(ctx, op, [r["resource_uid"] for r in resources])


def _remove_snapshots(ctx, op, uids):
    """Nos instantanés (première sauvegarde, sauvegarde finale) : ils
    occupent de la place sur les volumes source."""
    snaps = [s for s in ctx.src.list(K_LH_SNAPSHOT, NS, selector=f"{rp.LABEL_PLAN}={ctx.plan_id}")
             if _owned(ctx, s)]
    if not snaps:
        return
    with stage(ctx, op, "remove-snapshots", "source", uids):
        for snap in snaps:
            run.delete_quiet(ctx.src, K_LH_SNAPSHOT, NS, _md(snap)["name"])


def _choose_interval(ctx, interval):
    plan = ctx.plan
    if interval not in (None, ""):
        interval = rp.parse_duration(interval)
        if interval not in rp.CRON_STEPS:
            raise Refused("bad-interval", "interval: a divisor of the hour or of the day")
        return interval, "given"
    if db.list_ticks(ctx.conn, ctx.plan_id, ctx.gen, table="measures"):
        rep = measure_report(ctx.conn, ctx.plan_id)
        if rep.get("recommended_interval_s"):
            return rep["recommended_interval_s"], "measured"
    if plan.get("interval_s"):
        return plan["interval_s"], "plan"
    raise Refused("interval-required", "no measure: give --interval")


def first_backups(ctx, works):
    """Première sauvegarde de chaque volume qui n'en a aucune terminée,
    déclenchée tout de suite (instantané + `Backup` étiquetés). Rend
    {volume: dernière sauvegarde terminée}."""
    byvol = backups_by_volume(ctx.src)
    todo = [(w["uid"], d["source_volume"]) for w in works for d in w["disks"]
            if not _completed(byvol.get(d["source_volume"]))]
    if todo:
        names = take_backups(ctx, "first-backup", todo, "first")
        wait_backups(ctx, "first-backup", names, "final_backup")
        byvol = backups_by_volume(ctx.src)
    return {d["source_volume"]: _completed(byvol.get(d["source_volume"]))[-1]
            for w in works for d in w["disks"]}


def _wait_visible(ctx, op, wanted, timeout_key):
    """Attend que chaque sauvegarde soit visible côté cible ({volume: nom}),
    relecture redemandée. Rend {volume: Backup de la cible}."""
    last = [None]
    found = {}

    def visible():
        tb = backups_by_volume(ctx.dst)
        missing = []
        for vol, name in wanted.items():
            b = next((x for x in tb.get(vol, []) if _md(x).get("name") == name), None)
            if b is not None and _status(b).get("url"):
                found[vol] = b
            else:
                missing.append(vol)
        if not missing:
            return True
        if last[0] is None or ctx.now() - last[0] >= ctx.timeouts["resync_nudge"]:
            request_resync(ctx, missing)
            last[0] = ctx.now()
        return f"{len(missing)} backup(s) not visible on the target yet"
    wait_for(ctx, op, "backups visible on the target", visible, ctx.timeouts[timeout_key], every=2)
    return found


def ensure_backing_images(ctx, works, src_facts):
    """Backing images absentes de la cible : restaurées depuis la cible de
    sauvegarde (`BackupBackingImage` créé par Longhorn avec la première
    sauvegarde du volume). Une image de même nom et de même somme est
    réutilisée sans être touchée."""
    need = {}
    for w in works:
        for d in w["disks"]:
            if d["backing_image"]:
                need.setdefault(d["backing_image"], []).append(w["uid"])
    for bi, uids in sorted(need.items()):
        with stage(ctx, "protect", "backing-image", "target", sorted(set(uids)), f"backing image {bi}"):
            cur = ctx.dst.get(K_LH_BI, NS, bi)
            checksum = ((src_facts.get("backing_images") or {}).get(bi) or {}).get("checksum") or ""
            if cur is not None and not _owned(ctx, cur):
                if (_spec(cur).get("checksum") or _status(cur).get("checksum")) != checksum:
                    raise RunError("backing-image-conflict", f"backing image {bi} differs on the target")
                continue
            if cur is None:
                url = [None]
                last = [None]

                def bbi_visible():
                    for b in ctx.dst.list(K_LH_BBI, NS):
                        if bi in (_spec(b).get("backingImage"), _status(b).get("backingImage"), _md(b).get("name")) \
                                and _status(b).get("url"):
                            url[0] = _status(b)["url"]
                            return True
                    if last[0] is None or ctx.now() - last[0] >= ctx.timeouts["resync_nudge"]:
                        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ctx.now()))
                        ctx.dst.patch(K_LH_TARGET, NS, BACKUP_TARGET, {"spec": {"syncRequestedAt": stamp}})
                        last[0] = ctx.now()
                    return "backing image backup not visible yet"
                wait_for(ctx, "backing-image", f"backup of {bi}", bbi_visible, ctx.timeouts["backing_image"], every=5)
                ctx.dst.create(rp.backing_image_restore(bi, checksum, url[0], BACKUP_TARGET,
                                                        labels=rp.plan_labels(ctx.plan_id, ctx.gen)))
                ctx.record("dst", K_LH_BI, NS, bi)

            def ready():
                o = ctx.dst.get(K_LH_BI, NS, bi)
                files = (_status(o).get("diskFileStatusMap") or {}).values()
                if any((f or {}).get("state") == "ready" for f in files):
                    return True
                if any((f or {}).get("state") in ("failed", "failed-and-cleanup") for f in files):
                    return Fail(f"backing image {bi} failed to restore")
                return "restoring"
            wait_for(ctx, "backing-image", f"backing image {bi}", ready, ctx.timeouts["backing_image"], every=5)


def sync_first_backups(ctx, works, latest):
    """Relecture forcée côté cible jusqu'à y voir la dernière sauvegarde de
    chaque volume. Rend {volume: Backup de la cible}."""
    names = {d["source_volume"]: _md(latest[d["source_volume"]]).get("name") for w in works for d in w["disks"]}
    with stage(ctx, "protect", "resync", "target", [w["uid"] for w in works]):
        return _wait_visible(ctx, "resync", names, "resync_apply")


def ensure_standby(ctx, works, latest, visible):
    """Volumes de secours créés depuis la dernière sauvegarde (URL lue sur
    l'objet `Backup` synchronisé côté cible), puis attente de la
    restauration initiale avec progression."""
    names = {d["source_volume"]: _md(latest[d["source_volume"]]).get("name") for w in works for d in w["disks"]}
    for w in works:
        with stage(ctx, "protect", "standby-volume", "target", [w["uid"]]):
            for d in w["disks"]:
                sb, vol = d["target_volume"], d["source_volume"]
                cur = ctx.dst.get(K_LH_VOLUME, NS, sb)
                if cur is not None:
                    if not _owned(ctx, cur):
                        raise RunError("target-volume-taken", f"volume {sb} exists without our labels")
                    if url_volume(_spec(cur).get("fromBackup")) != vol:
                        raise RunError("standby-mismatch", f"standby volume {sb} restores another volume")
                else:
                    ctx.dst.create(rp.standby_volume(
                        sb, _status(visible[vol])["url"], d["size"], d["access_mode"], d["migratable"],
                        d["target_replicas"], d["backing_image"] or None, d["class_params"],
                        labels=rp.plan_labels(ctx.plan_id, ctx.gen, w["uid"])))
                    ctx.record("dst", K_LH_VOLUME, NS, sb)
                bv_src = find_backup_volume(ctx.src, vol)
                bv_dst = find_backup_volume(ctx.dst, vol)
                db.upsert_volume(ctx.conn, ctx.plan_id, {
                    "generation": ctx.gen, "vm_uid": w["uid"], "disk": d["disk"], "state": "restoring",
                    "backup_volume_source": _md(bv_src).get("name") or "",
                    "backup_volume_target": _md(bv_dst).get("name") or ""})
    total = sum(int(d["size"]) for w in works for d in w["disks"])
    prog = ctx.tracker("initial-restore", total)
    wanted = {d["target_volume"]: (d["source_volume"], names[d["source_volume"]]) for w in works for d in w["disks"]}
    sizes = {d["target_volume"]: int(d["size"]) for w in works for d in w["disks"]}

    def progress_hook():
        done = 0
        for sb in wanted:
            st = (_status(engine_of(ctx.dst, sb)).get("restoreStatus") or {}).values()
            pct = [float((s or {}).get("progress") or 0) for s in st]
            done += sizes[sb] * (sum(pct) / len(pct) / 100.0 if pct else 0)
        prog.update(int(done))

    with stage(ctx, "protect", "initial-restore", "target", [w["uid"] for w in works]):
        # la progression (octets restaurés des moteurs) est relevée à chaque tour
        applied = wait_applied(ctx, "initial-restore", wanted, "initial_restore", on_poll=progress_hook)
        prog.update(total)
        ctx.emit("initial-restore", "running", vp.summary(prog.phase, prog.finish()))
    now = ctx.dt()
    for w in works:
        for d in w["disks"]:
            b = latest[d["source_volume"]]
            db.record_tick(ctx.conn, ctx.plan_id, _tick_from_backup(b, w["uid"], ctx.gen))
            db.mark_applied(ctx.conn, ctx.plan_id, d["source_volume"], _md(b)["name"], now, ctx.gen)
            db.upsert_volume(ctx.conn, ctx.plan_id, {"generation": ctx.gen, "vm_uid": w["uid"], "disk": d["disk"],
                                                     "state": "standby"})
    return applied


def _dry_run_protect(ctx, works, src_facts, dst_facts):
    """Dry-run serveur de chaque objet cible (volumes de secours, backing
    images, carnet) et de la tâche source. Une URL de sauvegarde manquante
    (aucune sauvegarde encore) est remplacée par une valeur d'essai,
    signalée (`placeholder_url`)."""
    out = []
    tb = backups_by_volume(ctx.dst)
    labels = rp.plan_labels(ctx.plan_id, ctx.gen)

    def attempt(side, obj, what, **facts):
        try:
            ctx.kube(side).create(obj, dry_run=True)
            out.append({"object": what, "name": obj["metadata"]["name"], "ok": True, **facts})
        except Exception as e:                  # noqa: BLE001
            out.append({"object": what, "name": obj["metadata"]["name"], "ok": False,
                        "message": str(e)[:300], **facts})
    for w in works:
        attempt("src", rp.recurring_job(w["job"], rp.cron_for(ctx.plan.get("interval_s") or 300), ctx.plan["retain"],
                                        max(1, len(w["disks"])), ctx.plan_id, w["uid"], ctx.gen),
                "recurring-job")
        for d in w["disks"]:
            done = _completed(tb.get(d["source_volume"]))
            url = _status(done[-1]).get("url") if done else ""
            if (dst_facts.get("volumes") or {}).get(d["target_volume"]) is not None:
                out.append({"object": "standby-volume", "name": d["target_volume"], "ok": True, "exists": True})
                continue
            attempt("dst", rp.standby_volume(
                d["target_volume"], url or f"dry-run://?backup=none&volume={d['source_volume']}", d["size"],
                d["access_mode"], d["migratable"], d["target_replicas"], d["backing_image"] or None,
                d["class_params"], labels=dict(labels, **{rp.LABEL_VM: w["uid"]})),
                "standby-volume", placeholder_url=not url)
        res = {"resource_uid": w["uid"], "namespace": w["namespace"], "name": w["name"],
               "firmware_uuid": w["firmware_uuid"]}
        try:
            vm, _r = rp.target_vm(w["vm"], res, ctx.plan.get("mappings") or {}, ctx.plan.get("options") or {},
                                  {d["claim"]: d["target_claim"] for d in w["disks"]}, ctx.plan_id, ctx.gen)
            attempt("dst", carnet_manifest(ctx.plan_id, ctx.gen, w, vm, [], carnet_volumes(w),
                                           ctx.plan["source_cluster"]), "carnet")
        except ValueError as e:
            out.append({"object": "carnet", "name": rp.carnet_name(ctx.plan_id, w["uid"]), "ok": False,
                        "message": str(e)})
    return out


def protect(ctx, interval=None, dry_run=False):
    """(`draft`, `measuring`, `protecting`, `protected`, `error`) ->
    `protecting` -> `protected`. Rejouable : chaque sous-étape constate
    l'existant. Annulation : retour arrière de ce que cette exécution a
    créé. Échec : rien n'est défait (la relance reprend, une longue
    restauration initiale n'est pas perdue), plan en `error`."""
    try:
        _precondition(ctx, "protect")
        src = collect_source(ctx)
        dst = collect_target(ctx, [rp.target_ref(ctx.plan, {"namespace": v["namespace"], "name": v["name"]})[0]
                                   for v in src.get("vms") or []])
        findings = rp.check(src, dst, dict(ctx.plan, id=ctx.plan_id, phase="protect"))
        uids = [v["uid"] for v in src.get("vms") or []]
        if rp.blocking(findings):
            raise Refused("check", "the pre-check refuses this protection", findings=findings)
        works = build_works(ctx, src, dst)
        if dry_run:
            dry = _dry_run_protect(ctx, works, src, dst)
            ok = all(x["ok"] for x in dry)
            return _result(ctx, "dry-run", EXIT_OK if ok else EXIT_BLOCKED, "dry run", findings=findings, dry_run=dry)
        chosen, how = _choose_interval(ctx, interval)
        _lock(ctx, "protect")
    except Refused as e:
        return _refusal(ctx, "protect", e, [v["uid"] for v in (locals().get("src") or {}).get("vms") or []])
    except Cancelled as e:
        _unlock(ctx)
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e))
    try:
        if how == "plan" and not db.list_ticks(ctx.conn, ctx.plan_id, ctx.gen, table="measures"):
            ctx.event("protect", "interval", level="warn", code="interval-without-measure",
                      message=f"interval {chosen} s set without a measure")
        if chosen != ctx.plan.get("interval_s"):
            db.update_plan(ctx.conn, ctx.plan_id, now=ctx.dt(), interval_s=chosen)
            ctx.reload()
        ctx.event("protect", "interval", code=how, message=f"backup every {chosen} s ({how})",
                  facts={"interval_s": chosen, "how": how})
        _set_state(ctx, "protecting")
        _record_works(ctx, works, "protecting")
        for f in findings:
            if f["level"] == "warn":
                ctx.event("protect", "check", level="warn", code=f["code"], facts=f["facts"])
        _ensure_jobs(ctx, "protect", works, ctx.plan["cron"], ctx.plan["retain"], src.get("jobs") or {})
        with stage(ctx, "protect", "first-backup", "source", uids):
            latest = first_backups(ctx, works)
        visible = sync_first_backups(ctx, works, latest)
        ensure_backing_images(ctx, works, src)
        ensure_standby(ctx, works, latest, visible)
        write_carnets(ctx, works)
        _set_resources(ctx, uids, "protected")
        _set_state(ctx, "protected")
        ctx.event("protect", "done", code="protected", facts={"interval_s": chosen, "how": how, "vms": len(uids)})
        _unlock(ctx)
        return _result(ctx, "protected", EXIT_OK, f"{len(uids)} VM(s) protected", interval_s=chosen,
                       findings=findings)
    except Cancelled as e:
        left = _undo_created(ctx)
        _error(ctx, uids, f"protect cancelled: {e}", left)
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e), left=left)
    except Exception as e:                      # noqa: BLE001
        _error(ctx, uids, f"protect failed: {e}")
        return _result(ctx, error_code(e), EXIT_FAIL, str(e))


def _error(ctx, uids, message, left=None):
    """Plan et ressources en `error` (l'origine est rangée), verrou libéré."""
    try:
        for u in uids:
            r = db.get_resource(ctx.conn, ctx.plan_id, u, ctx.gen)
            if r and r["state"] != "error" and rp.transition_allowed(r["state"], "error", r.get("error_origin")):
                _set_resources(ctx, [u], "error", message)
        if ctx.reload()["state"] != "error":
            _set_state(ctx, "error", message[:500])
        if left:
            ctx.event("plan", "rollback", level="error", code="left", message="could not undo: " + "; ".join(left))
    finally:
        _unlock(ctx)


# ---------------------------------------------------------------------------
# Bascule
# ---------------------------------------------------------------------------

def _open_run(ctx, operation):
    for r in reversed(db.list_runs(ctx.conn, ctx.plan_id)):
        if r["operation"] == operation and not r["finished_at"] and int(r["generation"]) == ctx.gen:
            return r
    return None


def _carnets(ctx, uids):
    out = {}
    for s in ctx.dst.list(K_SECRET, rp.CARNET_NAMESPACE, selector=f"{rp.LABEL_PLAN}={ctx.plan_id}"):
        c = read_carnet(s)
        meta = c.get("meta") or {}
        if meta.get("vm_uid") in uids and int(meta.get("generation") or 0) == ctx.gen and _owned(ctx, s):
            out[meta["vm_uid"]] = c
    return out


def _select(ctx, vms):
    res = [r for r in db.list_resources(ctx.conn, ctx.plan_id, ctx.gen)
           if r["state"] in ("protected", "failing_over", "error")]
    if vms:
        want = set(vms)
        unknown = want - {f"{r['namespace']}/{r['name']}" for r in res}
        if unknown:
            raise Refused("unknown-vm", "not protected by this plan: " + ", ".join(sorted(unknown)))
        res = [r for r in res if f"{r['namespace']}/{r['name']}" in want]
    if not res:
        raise Refused("no-vm", "no VM of this plan can fail over")
    return res


def _unplanned_src_facts(ctx, carnets):
    vms = []
    for uid, c in carnets.items():
        meta = c["meta"]
        vms.append({"uid": uid, "namespace": meta["namespace"], "name": meta["name"], "vm": c["vm"],
                    "firmware_uuid": rp.firmware_uuid(c["vm"]),
                    "disks": [{"disk": v["disk"], "claim": v["claim"], "size": v["size"],
                               "storage_class": v["storage_class"], "image": None} for v in c["volumes"]]})
    return {"cluster": ctx.plan["source_cluster"], "reachable": False, "vms": vms}


def _preflight_targets(ctx, carnets, dst_facts):
    """Avant toute écriture : secrets, PV, PVC et VM cibles libres (ou à
    nous), puis dry-run serveur de ce qui manque. Une VM refusée parce que
    ses PVC n'existent pas encore avertit seulement (à confirmer sur le
    banc)."""
    findings, dry = [], []
    for uid, c in carnets.items():
        vm = c["vm"]
        tns = _md(vm)["namespace"]
        for s in c["secrets"] or []:
            cur = ctx.dst.get(K_SECRET, tns, s["name"])
            if cur is not None and not _owned(ctx, cur):
                findings.append({"code": "target-secret-taken", "level": "block", "facts": {"name": f"{tns}/{s['name']}"}})
        for v in c["volumes"] or []:
            pv = ctx.dst.get(K_PV, None, v["target_volume"])
            if pv is not None and not _owned(ctx, pv):
                findings.append({"code": "target-pv-taken", "level": "block", "facts": {"name": v["target_volume"]}})
            elif pv is None:
                try:
                    ctx.dst.create(_pv(ctx, uid, v), dry_run=True)
                    dry.append({"object": "pv", "name": v["target_volume"], "ok": True})
                except Exception as e:          # noqa: BLE001
                    dry.append({"object": "pv", "name": v["target_volume"], "ok": False, "message": str(e)[:300]})
                    findings.append({"code": "dry-run-refused", "level": "block",
                                     "facts": {"object": "pv", "name": v["target_volume"], "message": str(e)[:300]}})
            pvc = ctx.dst.get(K_PVC, tns, v["target_claim"])
            if pvc is None and tns in (dst_facts.get("namespaces") or []):
                try:
                    ctx.dst.create(_pvc(ctx, uid, tns, v), dry_run=True)
                    dry.append({"object": "pvc", "name": f"{tns}/{v['target_claim']}", "ok": True})
                except Exception as e:          # noqa: BLE001
                    dry.append({"object": "pvc", "name": f"{tns}/{v['target_claim']}", "ok": False,
                                "message": str(e)[:300]})
                    findings.append({"code": "dry-run-refused", "level": "block",
                                     "facts": {"object": "pvc", "name": v["target_claim"], "message": str(e)[:300]}})
        if ctx.dst.get(K_VM, tns, _md(vm)["name"]) is None and tns in (dst_facts.get("namespaces") or []):
            try:
                ctx.dst.create(vm, dry_run=True)
                dry.append({"object": "vm", "name": f"{tns}/{_md(vm)['name']}", "ok": True})
            except Exception as e:              # noqa: BLE001
                msg = str(e)[:300]
                pvc_missing = all(m in msg.lower() for m in _PVC_MISSING)
                dry.append({"object": "vm", "name": f"{tns}/{_md(vm)['name']}", "ok": pvc_missing, "message": msg})
                findings.append({"code": "vm-dry-run-pvc-missing" if pvc_missing else "dry-run-refused",
                                 "level": "warn" if pvc_missing else "block",
                                 "facts": {"object": "vm", "name": _md(vm)["name"], "message": msg}})
    return findings, dry


def _pv(ctx, uid, v):
    return rp.static_pv(v["target_volume"], v["size"], v["volume_mode"], v["access_modes"], v["storage_class"],
                        labels=rp.plan_labels(ctx.plan_id, ctx.gen, uid))


def _pvc(ctx, uid, tns, v):
    return rp.static_pvc(v["target_claim"], tns, v["target_volume"], v["size"], v["volume_mode"], v["access_modes"],
                         v["storage_class"], image_id=v.get("image_id"),
                         labels=rp.plan_labels(ctx.plan_id, ctx.gen, uid))


def _stop_source(ctx, uid, ns, name):
    """Arrête la VM source et note son runStrategy d'origine dans une
    annotation (lue par le retour arrière, même depuis une relance)."""
    vm = ctx.src.get(K_VM, ns, name)
    if vm is None:
        raise RunError("source-vm-missing", f"source VM {ns}/{name} not found")
    ann = _md(vm).get("annotations") or {}
    if STOPPED_ANNOTATION not in ann:
        sp = _spec(vm)
        initial = sp.get("runStrategy") or ("Always" if sp.get("running") else "Halted")
        ctx.src.patch(K_VM, ns, name, {"metadata": {"annotations": {STOPPED_ANNOTATION: initial}}})
    if ctx.src.get(K_VMI, ns, name) is not None or _spec(vm).get("runStrategy") != "Halted":
        run.stop_vm(ctx, ctx.src, ns, name, sid="stop-source")


def _restart_source(ctx, ns, name):
    vm = ctx.src.get(K_VM, ns, name)
    initial = (_md(vm).get("annotations") or {}).get(STOPPED_ANNOTATION)
    if vm is None or initial is None:
        return False
    if initial in ("Always", "RerunOnFailure"):
        run.start_vm(ctx, ctx.src, ns, name, initial, sid="restart-source")
    ctx.src.patch(K_VM, ns, name, {"metadata": {"annotations": {STOPPED_ANNOTATION: None}}})
    return True


def _rollback_failover(ctx, sel, carnets):
    """Avant l'activation, tout est réversible : la VM source repart comme
    elle tournait, l'étiquette de tâche revient, les instantanés finaux de
    cette exécution disparaissent (les sauvegardes finales restent : le
    volume de secours a pu les appliquer)."""
    left = []
    for r in sel:
        uid, ns, name = r["resource_uid"], r["namespace"], r["name"]
        try:
            for v in (carnets.get(uid) or {}).get("volumes") or []:
                _label_volume(ctx, v["source_volume"], rp.recurring_job_name(ctx.plan_id, uid), False)
            _restart_source(ctx, ns, name)
        except Exception as e:                  # noqa: BLE001
            left.append(f"{ns}/{name}: {str(e)[:200]}")
        ctx.event("failover", "rollback", uid, "source", level="warn" if not left else "error",
                  code="rolled-back", message="failover undone before activation")
    for side, kind, ns, name in reversed(list(ctx.created)):
        if kind == K_LH_SNAPSHOT:
            run._undo_delete(ctx, ctx.kube(side), kind, ns, name)
    ctx.created.clear()
    ctx.undo_labels.clear()
    return left


def _activated_volumes(ctx, carnet):
    out = {}
    for v in carnet["volumes"] or []:
        out[v["target_volume"]] = _is_activated(ctx.dst.get(K_LH_VOLUME, NS, v["target_volume"]))
    return out


def failover(ctx, vms=None, planned=True, accept_skew=False, force_unplanned=False, yes=False, dry_run=False):
    """(`protected`, `failing_over`, `error`) -> `failing_over` ->
    `failed_over` (ou `protected` si une partie seulement du plan bascule).

    Planifiée : la source répond ; VM arrêtée, sauvegarde finale de chaque
    disque appliquée avant l'activation (zéro perte). Non planifiée
    (`planned=False`, exige `yes`) : relecture finale bornée, activation de
    ce qui est appliqué, écart entre disques contrôlé (`accept_skew`).
    Avant l'activation, une erreur ou une annulation remet tout en place ;
    après, rien n'est défait et la relance reprend à l'étape suivante."""
    opts = ctx.plan.get("options") or {}
    tolerance = int(opts.get("skew_tolerance_s", rp.SKEW_TOLERANCE_S))
    try:
        if not planned and not yes and not dry_run:
            raise Refused("confirmation-required", "an unplanned failover needs --yes")
        _precondition(ctx, "failover")
        sel = _select(ctx, vms)
        uids = [r["resource_uid"] for r in sel]
        s_state, d_state = probe(ctx, "src"), probe(ctx, "dst")
        if d_state != "ok":
            raise Refused("target-denied" if d_state == "denied" else "target-unreachable",
                          f"the target cluster is {'refusing this reader' if d_state == 'denied' else 'unreachable'}")
        if planned and s_state != "ok":
            raise Refused("source-unreachable", "the source does not answer: use --unplanned",
                          facts={"hint": "--unplanned"})
        if not planned and s_state == "ok" and not force_unplanned:
            raise Refused("source-reachable", "the source answers: use a planned failover, or --force-unplanned")
        carnets = _carnets(ctx, set(uids))
        missing = [r for r in sel if r["resource_uid"] not in carnets]
        if missing:
            raise Refused("carnet-missing", "no recovery carnet for " +
                          ", ".join(f"{r['namespace']}/{r['name']}" for r in missing))
        tns_list = [_md(c["vm"])["namespace"] for c in carnets.values()]
        dst = collect_target(ctx, tns_list)
        if planned:
            src = collect_source(ctx)
            src["vms"] = [v for v in src.get("vms") or [] if v["uid"] in carnets]
            phase = "failover"
        else:
            src = _unplanned_src_facts(ctx, carnets)
            phase = "failover-unplanned"
        findings = rp.check(src, dst, dict(ctx.plan, id=ctx.plan_id, phase=phase))
        extra, dry = _preflight_targets(ctx, carnets, dst)
        findings = sorted(findings + extra, key=lambda f: rp._ORDER[f["level"]])
        if rp.blocking(findings):
            raise Refused("check", "the pre-check refuses this failover", findings=findings)
        if dry_run:
            return _result(ctx, "dry-run", EXIT_OK, "dry run", findings=findings, dry_run=dry)
        _lock(ctx, "failover")
    except Refused as e:
        return _refusal(ctx, "failover", e, [r["resource_uid"] for r in locals().get("sel") or []])
    except Cancelled as e:
        _unlock(ctx)
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e))

    op = "failover"
    mode = "planned" if planned else "unplanned"
    started = ctx.dt()
    prev = _open_run(ctx, "failover")
    run_id = prev["id"] if prev else db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen,
                                                    vms=[f"{r['namespace']}/{r['name']}" for r in sel],
                                                    points={"mode": mode}, by=ctx.by, action_id=ctx.action_id)
    points = dict((prev or {}).get("points") or {}, mode=mode)
    paused_before = bool(opts.get("paused"))
    if prev and "paused_before" in points:
        paused_before = points["paused_before"]
    points["paused_before"] = paused_before
    activated_any = False
    result = {"fenced": {}, "skew_s": {}, "refused_skew": [], "lost_after": {}}
    try:
        _set_state(ctx, "failing_over")
        _set_resources(ctx, uids, "failing_over")
        db.update_plan(ctx.conn, ctx.plan_id, now=ctx.dt(), options={"paused": True})
        ctx.reload()
        ctx.event(op, "pause-follower", code="paused", message="follower paused for this plan")

        state = {uid: _activated_volumes(ctx, carnets[uid]) for uid in uids}
        done_vms = [u for u in uids if state[u] and any(state[u].values())]
        activated_any = bool(done_vms)
        pre = [r for r in sel if r["resource_uid"] not in done_vms]
        wanted = {}                              # volume de secours -> (volume source, sauvegarde)
        if planned:
            for r in pre:
                uid = r["resource_uid"]
                with stage(ctx, op, "stop-source", "source", [uid], f"stopping {r['namespace']}/{r['name']}"):
                    _stop_source(ctx, uid, r["namespace"], r["name"])
                with stage(ctx, op, "remove-job-label", "source", [uid]):
                    for v in carnets[uid]["volumes"]:
                        _unlabel_volume(ctx, v["source_volume"], rp.recurring_job_name(ctx.plan_id, uid))
            items = [(r["resource_uid"], v["source_volume"]) for r in pre for v in carnets[r["resource_uid"]]["volumes"]]
            if items:
                with stage(ctx, op, "final-backup", "source", [r["resource_uid"] for r in pre]):
                    names = take_backups(ctx, "final-backup", items, "final", token=str(run_id))
                    finals = wait_backups(ctx, "final-backup", names, "final_backup")
                for uid, vol in items:
                    db.record_tick(ctx.conn, ctx.plan_id, _tick_from_backup(finals[vol], uid, ctx.gen))
                for r in pre:
                    for v in carnets[r["resource_uid"]]["volumes"]:
                        wanted[v["target_volume"]] = (v["source_volume"], names[v["source_volume"]])
                points["final"] = {vol: names[vol] for _u, vol in items}
                db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen, points=points,
                              vms=[f"{r['namespace']}/{r['name']}" for r in sel], run_id=run_id,
                              by=ctx.by, action_id=ctx.action_id)
                with stage(ctx, op, "final-resync", "target", [r["resource_uid"] for r in pre]):
                    wait_applied(ctx, "final-resync", wanted, "resync_apply")
                for uid, vol in items:
                    db.mark_applied(ctx.conn, ctx.plan_id, vol, names[vol], ctx.dt(), ctx.gen)
        else:
            vols = {v["target_volume"]: v["source_volume"] for r in pre for v in carnets[r["resource_uid"]]["volumes"]}
            with stage(ctx, op, "final-resync", "target", [r["resource_uid"] for r in pre]):
                deadline = ctx.now() + ctx.timeouts["unplanned_sync"]
                wait_synced(ctx, "final-resync", list(vols.values()), deadline)
                tb = backups_by_volume(ctx.dst)
                for sb, vol in vols.items():
                    c = _completed(tb.get(vol))
                    if c:
                        wanted[sb] = (vol, _md(c[-1])["name"])
                saved = ctx.timeouts["unplanned_sync"]
                ctx.timeouts["unplanned_sync"] = max(1, deadline - ctx.now())
                try:
                    wait_applied(ctx, "final-resync", wanted, "unplanned_sync", fail_on_timeout=False)
                finally:
                    ctx.timeouts["unplanned_sync"] = saved
        # points réellement appliqués et écart entre disques, par VM
        tb = backups_by_volume(ctx.dst)
        go = []
        for r in sel:
            uid = r["resource_uid"]
            pts = {}
            for v in carnets[uid]["volumes"]:
                eng = engine_of(ctx.dst, v["target_volume"])
                last = _status(eng).get("lastRestoredBackup") or ""
                b = next((x for x in tb.get(v["source_volume"], []) if _md(x).get("name") == last), None)
                pts[v["source_volume"]] = {"backup": last, "snapshot_at": _snap_time(b)}
            times = [rp._t(p["snapshot_at"]) for p in pts.values() if p["snapshot_at"]]
            skew = int((max(times) - min(times)).total_seconds()) if len(times) > 1 else 0
            result["skew_s"][f"{r['namespace']}/{r['name']}"] = skew
            if times:
                result["lost_after"][f"{r['namespace']}/{r['name']}"] = rp.iso(min(times))
            points.setdefault("applied", {})[uid] = pts
            if not planned and uid not in done_vms:
                if not all(p["backup"] for p in pts.values()):
                    result["refused_skew"].append(f"{r['namespace']}/{r['name']}")
                    ctx.event(op, "points", uid, "target", level="error", code="no-point",
                              message="a disk has no applied point", facts=pts)
                    continue
                if skew > tolerance and not accept_skew:
                    result["refused_skew"].append(f"{r['namespace']}/{r['name']}")
                    ctx.event(op, "points", uid, "target", level="warn", code="skew-exceeded",
                              message=f"disks {skew} s apart (tolerance {tolerance} s): --accept-skew",
                              facts={"skew_s": skew, "points": pts})
                    continue
            ctx.event(op, "points", uid, "target", code="points", facts={"skew_s": skew, "points": pts})
            go.append(r)
        if not go:
            raise Refused("skew-exceeded", "no VM can fail over without --accept-skew", facts=result)
        if not planned:
            # source revenue pendant la bascule : arrêt tenté au mieux
            if probe(ctx, "src") == "ok":
                for r in go:
                    key = f"{r['namespace']}/{r['name']}"
                    try:
                        ctx.src.patch(K_VM, r["namespace"], r["name"], {"spec": {"runStrategy": "Halted"}})
                        result["fenced"][key] = "source-fenced"
                    except Exception:           # noqa: BLE001
                        result["fenced"][key] = "source-not-fenced"
                    ctx.event(op, "fence", r["resource_uid"], "source", code=result["fenced"][key])
            else:
                for r in go:
                    result["fenced"][f"{r['namespace']}/{r['name']}"] = "source-not-fenced"
        # --- point de non-retour : l'activation est définitive ---
        for r in go:
            uid = r["resource_uid"]
            sbs = [v["target_volume"] for v in carnets[uid]["volumes"]]
            with stage(ctx, op, "activate", "target", [uid], "activating the standby volumes (definitive)"):
                activated_any = True
                activate(ctx, op, sbs)
            for v in carnets[uid]["volumes"]:
                db.upsert_volume(ctx.conn, ctx.plan_id, {"generation": ctx.gen, "vm_uid": uid, "disk": v["disk"],
                                                         "state": "activated"})
        for r in go:
            _bring_up(ctx, op, r, carnets[r["resource_uid"]], opts)
            if planned:
                ctx.src.patch(K_VM, r["namespace"], r["name"], {"metadata": {"annotations": {
                    FAILED_OVER_TO: f"{ctx.plan['target_cluster']}/{_md(carnets[r['resource_uid']]['vm'])['namespace']}/"
                                    f"{_md(carnets[r['resource_uid']]['vm'])['name']}",
                    STOPPED_ANNOTATION: None}}})
            _set_resources(ctx, [r["resource_uid"]], "failed_over")
        back = [r["resource_uid"] for r in sel if r not in go]
        _set_resources(ctx, back, "protected", "not failed over")
        rest = [x for x in db.list_resources(ctx.conn, ctx.plan_id, ctx.gen) if x["state"] != "failed_over"]
        _set_state(ctx, "protected" if rest else "failed_over")
        db.update_plan(ctx.conn, ctx.plan_id, now=ctx.dt(), options={"paused": paused_before})
        db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen, finished_at=ctx.dt(), result="ok",
                      vms=[f"{r['namespace']}/{r['name']}" for r in go], points=points, steps=ctx.steps,
                      by=ctx.by, action_id=ctx.action_id, run_id=run_id)
        _unlock(ctx)
        return _result(ctx, "failed-over", EXIT_OK, f"{len(go)} VM(s) failed over", run_id=run_id, mode=mode,
                       **result)
    except (Cancelled, Exception) as e:          # noqa: BLE001
        cancelled = isinstance(e, Cancelled)
        refused = isinstance(e, Refused)
        if not activated_any:
            left = _rollback_failover(ctx, sel, carnets) if planned else []
            _set_resources(ctx, uids, "protected", "failover undone")
            _set_state(ctx, "protected", f"failover undone: {e}"[:500])
            db.update_plan(ctx.conn, ctx.plan_id, now=ctx.dt(), options={"paused": paused_before})
            db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen, finished_at=ctx.dt(),
                          result="rolled-back", points=points, steps=ctx.steps, by=ctx.by,
                          action_id=ctx.action_id, run_id=run_id)
            _unlock(ctx)
            if left:
                ctx.event(op, "rollback", level="error", code="left", message="; ".join(left))
            if refused:
                return _result(ctx, e.code, EXIT_BLOCKED, str(e), findings=e.findings, **result)
            return _result(ctx, "cancelled" if cancelled else error_code(e),
                           EXIT_CANCELLED if cancelled else EXIT_FAIL, f"{e} (failover undone)", left=left, **result)
        # après l'activation : pas de retour arrière, reprise par relance
        msg = f"failover stopped after activation ({e}): run failover again to finish"
        db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen, result="interrupted", points=points,
                      steps=ctx.steps, by=ctx.by, action_id=ctx.action_id, run_id=run_id)
        _error(ctx, uids, msg)
        return _result(ctx, "interrupted", EXIT_CANCELLED if cancelled else EXIT_FAIL, msg, run_id=run_id, **result)


def _bring_up(ctx, op, r, carnet, opts):
    """Après l'activation : namespace, secrets cloud-init, PV puis PVC liés,
    VM (MAC contrôlée juste avant, dry-run serveur), démarrage selon
    `start`. Chaque objet est constaté avant d'être créé."""
    uid = r["resource_uid"]
    vm = carnet["vm"]
    tns, tname = _md(vm)["namespace"], _md(vm)["name"]
    labels = rp.plan_labels(ctx.plan_id, ctx.gen, uid)
    with stage(ctx, op, "claims", "target", [uid], f"volumes of {tns}/{tname}"):
        if ctx.dst.get(K_NS, None, tns) is None:
            if not opts.get("create_namespace"):
                raise RunError("namespace-missing", f"namespace {tns} missing on the target")
            _ensure(ctx, "dst", {"apiVersion": "v1", "kind": "Namespace",
                                 "metadata": {"name": tns, "labels": rp.plan_labels(ctx.plan_id, ctx.gen)}},
                    "namespace")
        for s in carnet["secrets"] or []:
            _ensure(ctx, "dst", {"apiVersion": "v1", "kind": "Secret", "type": s.get("type") or "Opaque",
                                 "metadata": {"name": s["name"], "namespace": tns, "labels": dict(labels)},
                                 "data": dict(s.get("data") or {})}, "target-secret")
        for v in carnet["volumes"]:
            _ensure(ctx, "dst", _pv(ctx, uid, v), "target-pv")
            _ensure(ctx, "dst", _pvc(ctx, uid, tns, v), "target-pvc")

        def bound():
            for v in carnet["volumes"]:
                if _status(ctx.dst.get(K_PVC, tns, v["target_claim"])).get("phase") != "Bound":
                    return f"{v['target_claim']} not bound yet"
            return True
        wait_for(ctx, op, "claims bound", bound, ctx.timeouts["bind"], every=1)
    with stage(ctx, op, "vm", "target", [uid], f"VM {tns}/{tname}"):
        cur = ctx.dst.get(K_VM, tns, tname)
        if cur is not None and not _owned(ctx, cur):
            raise RunError("target-vm-taken", f"VM {tns}/{tname} exists without our labels")
        if cur is None:
            if (opts.get("mac") or "keep") == "keep":
                mine = vt.vm_macs(vm)
                for other in ctx.dst.list(K_VM):
                    key = f"{_md(other).get('namespace')}/{_md(other).get('name')}"
                    clash = sorted(mine & vt.vm_macs(other))
                    if key != f"{tns}/{tname}" and clash:
                        raise RunError("mac-in-use", f"MAC {', '.join(clash)} used by {key}")
            ctx.dst.create(vm, dry_run=True)
            created = ctx.dst.create(vm) or {}
            ctx.record("dst", K_VM, tns, tname)
            vuid = _md(created).get("uid") or _md(ctx.dst.get(K_VM, tns, tname)).get("uid")
            if vuid:
                ref = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "name": tname, "uid": vuid}
                for s in carnet["secrets"] or []:
                    ctx.dst.patch(K_SECRET, tns, s["name"], {"metadata": {"ownerReferences": [ref]}})
    if (_spec(vm).get("runStrategy") or "Halted") != "Halted":
        with stage(ctx, op, "vm-start", "target", [uid], f"starting {tns}/{tname}"):
            def running():
                phase = run.vmi_phase(ctx.dst, tns, tname)
                if phase == "Failed":
                    return Fail(f"{tns}/{tname} failed to start")
                return True if phase == "Running" else (phase or "pending")
            wait_for(ctx, op, "VM running", running, ctx.timeouts["vm_start"], every=2)


# ---------------------------------------------------------------------------
# Déprotection
# ---------------------------------------------------------------------------

def unprotect(ctx, delete_backups=False, yes=False):
    """Tout état sauf `draft`, `failing_over`, `unprotected` ->
    `unprotecting` -> `unprotected`. Cible : volumes de secours non activés,
    carnets, backing images restaurées par le plan et inutilisées. Source :
    étiquettes de tâche, tâches. `delete_backups` (exige `yes`) : les
    `Backup` portant les étiquettes du plan, jamais un `BackupVolume`.
    Côté injoignable : noté « à faire », terminé à la relance. Les volumes
    activés, PV, PVC et VM cibles ne sont jamais supprimés (rendus dans
    `left`)."""
    try:
        if delete_backups and not yes:
            raise Refused("confirmation-required", "deleting backups needs --yes")
        _precondition(ctx, "unprotect")
        _lock(ctx, "unprotect")
    except Refused as e:
        return _refusal(ctx, "unprotect", e)
    op = "unprotect"
    started = ctx.dt()
    prev = _open_run(ctx, "unprotect")
    resources = db.list_resources(ctx.conn, ctx.plan_id)
    uids = sorted({r["resource_uid"] for r in resources if int(r["generation"]) == ctx.gen})
    delete_backups = bool(delete_backups or ((prev or {}).get("points") or {}).get("delete_backups"))
    run_id = prev["id"] if prev else db.record_run(ctx.conn, ctx.plan_id, "unprotect", started, ctx.gen,
                                                    points={"delete_backups": delete_backups}, by=ctx.by,
                                                    action_id=ctx.action_id)
    pending, left = [], []
    try:
        _set_state(ctx, "unprotecting")
        _set_resources(ctx, uids, "unprotecting")
        if not _touched_target(ctx):
            pass
        elif probe(ctx, "dst") == "ok":
            left += _unprotect_target(ctx, op, uids)
        else:
            pending.append("target")
        if probe(ctx, "src") == "ok":
            _remove_jobs(ctx, op, resources)
        else:
            pending.append("source")
        deleted = 0
        if delete_backups:
            with stage(ctx, op, "delete-backups", "", uids):
                for side in ("src", "dst"):
                    if probe(ctx, side) != "ok":
                        continue
                    for b in ctx.kube(side).list(K_LH_BACKUP, NS, selector=f"{rp.LABEL_PLAN}={ctx.plan_id}"):
                        if _owned(ctx, b):
                            if run.delete_quiet(ctx.kube(side), K_LH_BACKUP, NS, _md(b)["name"]):
                                deleted += 1
        if pending:
            for u in uids:
                ctx.event(op, "pending", u, level="warn", code="pending",
                          message="unreachable: " + ", ".join(pending) + "; run unprotect again")
            db.record_run(ctx.conn, ctx.plan_id, "unprotect", started, ctx.gen, result="pending",
                          points={"delete_backups": delete_backups, "pending": pending}, steps=ctx.steps,
                          by=ctx.by, action_id=ctx.action_id, run_id=run_id)
            _unlock(ctx)
            return _result(ctx, "pending", EXIT_FAIL, "to finish: " + ", ".join(pending), pending=pending, left=left)
        _set_resources(ctx, uids, "unprotected")
        _set_state(ctx, "unprotected")
        db.record_run(ctx.conn, ctx.plan_id, "unprotect", started, ctx.gen, finished_at=ctx.dt(), result="ok",
                      points={"delete_backups": delete_backups, "backups_deleted": deleted}, steps=ctx.steps,
                      by=ctx.by, action_id=ctx.action_id, run_id=run_id)
        _unlock(ctx)
        return _result(ctx, "unprotected", EXIT_OK, "plan unprotected", left=left, backups_deleted=deleted)
    except Cancelled as e:
        _unlock(ctx)
        return _result(ctx, "cancelled", EXIT_CANCELLED, f"{e}: run unprotect again to finish")
    except Exception as e:                      # noqa: BLE001
        _error(ctx, uids, f"unprotect failed: {e}")
        return _result(ctx, error_code(e), EXIT_FAIL, str(e))


def _touched_target(ctx):
    """La réplication a-t-elle déjà écrit sur la cible ? (un plan resté en
    mesure n'y a rien posé : sa déprotection n'attend pas la cible)"""
    row = ctx.conn.execute("SELECT 1 FROM events WHERE plan_id = ? AND side = 'target' LIMIT 1",
                           (ctx.plan_id,)).fetchone()
    return row is not None


def _unprotect_target(ctx, op, uids):
    left = []
    dst = ctx.dst
    with stage(ctx, op, "remove-target", "target", uids):
        for s in dst.list(K_SECRET, rp.CARNET_NAMESPACE, selector=f"{rp.LABEL_PLAN}={ctx.plan_id}"):
            if _owned(ctx, s):
                dst.delete(K_SECRET, rp.CARNET_NAMESPACE, _md(s)["name"])
        gone = []
        for lv in dst.list(K_LH_VOLUME, NS, selector=f"{rp.LABEL_PLAN}={ctx.plan_id}"):
            if not _owned(ctx, lv):
                continue
            name = _md(lv)["name"]
            if _is_activated(lv):
                left.append(f"volume {name} (activated)")
                continue
            dst.delete(K_LH_VOLUME, NS, name)
            gone.append(name)
        for kind, label in ((K_PV, "pv"), (K_PVC, "pvc"), (K_VM, "vm")):
            for o in dst.list(kind, None, selector=f"{rp.LABEL_PLAN}={ctx.plan_id}"):
                if _owned(ctx, o):
                    left.append(f"{label} {(_md(o).get('namespace') + '/') if _md(o).get('namespace') else ''}"
                                f"{_md(o)['name']}")
        if gone:
            wait_for(ctx, op, "standby volumes deleted",
                     lambda: True if not any(dst.get(K_LH_VOLUME, NS, n) for n in gone) else "deleting",
                     ctx.timeouts["delete"], every=2)
        used = {_spec(v).get("backingImage") for v in dst.list(K_LH_VOLUME, NS)}
        for bi in dst.list(K_LH_BI, NS, selector=f"{rp.LABEL_PLAN}={ctx.plan_id}"):
            if _owned(ctx, bi) and _md(bi)["name"] not in used:
                dst.delete(K_LH_BI, NS, _md(bi)["name"])
        for v in db.list_volumes(ctx.conn, ctx.plan_id):
            if v["target_volume"] in gone or (v["state"] in ("pending", "restoring", "standby")
                                              and dst.get(K_LH_VOLUME, NS, v["target_volume"]) is None):
                db.upsert_volume(ctx.conn, ctx.plan_id, {"generation": int(v["generation"]), "vm_uid": v["vm_uid"],
                                                         "disk": v["disk"], "state": "removed"})
    if left:
        ctx.event(op, "left", level="warn", code="left",
                  message=f"kept on the target (label {rp.LABEL_PLAN}={ctx.plan_id}): " + "; ".join(left))
    return left


# ---------------------------------------------------------------------------
# Reprise
# ---------------------------------------------------------------------------

def resume(ctx, yes=False):
    """Reprend la commande interrompue d'après l'état rangé (après un arrêt
    brutal, le verrou abandonné est repris par `acquire_lock`)."""
    plan = ctx.reload()
    state = plan["state"]
    origin = db.error_origin(ctx.conn, ctx.plan_id) if state == "error" else None
    eff = origin or state
    if eff == "protecting":
        return protect(ctx)
    if eff == "failing_over":
        r = _open_run(ctx, "failover") or {}
        pts = r.get("points") or {}
        planned = pts.get("mode", "planned") == "planned"
        return failover(ctx, vms=None, planned=planned, yes=yes or planned)
    if eff == "unprotecting":
        r = _open_run(ctx, "unprotect") or {}
        return unprotect(ctx, delete_backups=bool((r.get("points") or {}).get("delete_backups")), yes=True)
    if eff == "measuring" and state == "error":
        return measure_start(ctx, _measure_interval(ctx.conn, plan))
    return _result(ctx, "nothing-to-resume", EXIT_OK, f"nothing to resume from {state}")
