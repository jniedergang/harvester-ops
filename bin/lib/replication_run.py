"""harvester-ops : déroulé de la réplication asynchrone de VMs (v1.99.0).

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
  après une activation, qui est définitive ;
* arrêt de la console (`Suspended`, décision 68, v1.99.0) : la commande
  s'arrête au prochain point sûr SANS retour arrière, son exécution est
  rangée `interrupted` avec l'étape atteinte, et `resume` la continue (une
  restauration initiale commencée n'est pas refaite).

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


class Cancelled(BaseException):
    """Annulation demandée (SIGTERM/SIGINT), levée dans le déroulé. Pas une
    `Exception` (comme `replication_follow.Interrupted`) : levée par le
    gestionnaire de signal n'importe où, elle était retenue par les
    `except Exception` des petites lectures (sonde, classe d'une image,
    masquage des chemins) et la commande allait au bout (vu sur le banc,
    v1.99.0 : `protect --retain` annulé sorti en 0 avec la nouvelle valeur)."""


class Suspended(BaseException):
    """Arrêt demandé par l'arrêt de la console (décision 68, v1.99.0), pas
    par une personne : la commande s'arrête au prochain point sûr
    (`Ctx.safe_point`), sans retour arrière, et reste à reprendre.

    Pas une sous-classe de `Cancelled` : chaque `except Cancelled` du
    déroulé lance un retour arrière, qu'un arrêt de la console ne doit
    justement pas déclencher (une restauration initiale de plusieurs heures
    serait perdue à chaque redémarrage ou mise à jour). Une `BaseException`,
    pour la même raison que `Cancelled` : jamais retenue par un `except
    Exception` de petite lecture."""


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
        self.undo_specs = []        # (côté, kind, ns, nom, patch inverse) : champs modifiés par elle
        self.undo_default = []      # (vm_uid, disque) : étiquette `default` notée posée par elle
        self.steps = []             # [{"step", "seconds"}] pour `runs`
        self.locked = False
        self._beat = now()
        self.quiet = False          # rien au journal (contrôle à blanc de `check`)
        self.undo_until = None      # échéance du retour arrière (`begin_undo`)
        self.undo_timed_out = False
        # arrêt de la console (décision 68) : fonction posée par la ligne de
        # commande, qui rend la raison de l'arrêt demandé (ou None). Lue aux
        # points sûrs seulement (`safe_point`) : jamais au milieu d'une suite
        # d'écritures, ni pendant un retour arrière
        self.suspend_requested = None
        self.suspended = False
        self.step = ""              # dernière étape commencée (rangée à l'arrêt)
        # appelé quand le résultat est construit (la ligne de commande y masque
        # ses signaux : une commande terminée n'est plus dite annulée)
        self.on_finish = None
        # un kubeconfig cité par une erreur de kubectl n'entre jamais au journal
        db.hide_paths(getattr(src, "kubeconfig", None), getattr(dst, "kubeconfig", None))

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

    def safe_point(self):
        """Point sûr : rien n'est à moitié écrit. Lève `Suspended` si l'arrêt
        de la console est demandé. Placé entre deux étapes (`stage`) et à
        chaque attente (`sleep`, où passent toutes les attentes bornées) :
        c'est là que la commande passe presque tout son temps (restauration
        initiale, sauvegardes), donc l'arrêt y est vu en un tour d'attente.
        Jamais pendant un retour arrière (une annulation déjà commencée va
        à son terme) ni deux fois."""
        if self.suspended or self.undo_until is not None or self.suspend_requested is None:
            return
        reason = self.suspend_requested()
        if reason:
            self.suspended = True
            raise Suspended(reason)

    def sleep(self, s):
        self.safe_point()
        self._sleep(s)
        self.safe_point()
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

    # -- retour arrière borné ------------------------------------------------------
    def begin_undo(self):
        """Ouvre l'échéance UNIQUE du retour arrière (`undo_total`, 240 s par
        défaut) : chaque attente est ensuite bornée par le temps qui reste,
        et une fois l'échéance passée plus rien n'est tenté."""
        if self.undo_until is None:
            self.undo_until = self.now() + int(self.timeouts.get("undo_total") or rp.TIMEOUTS["undo_total"])
            self.timeouts = _UndoTimeouts(self.timeouts, self)
        return self.undo_until

    def undo_expired(self):
        if self.undo_until is not None and self.now() >= self.undo_until:
            self.undo_timed_out = True
        return self.undo_timed_out

    def event(self, operation, step, uid="", side="", level="info", code="", message="", facts=None):
        if self.quiet:
            return None
        # un événement du plan entier (sans côté) est rangé sous la cible :
        # c'est d'elle que la réplication se regarde (vue par hôte)
        return db.record_event(self.conn, operation, step=step, plan_id=self.plan_id, resource_uid=uid,
                               host=self.cluster(side) if side else self.plan["target_cluster"], side=side,
                               level=level, code=code,
                               message=str(message)[:500], facts=facts, by=self.by,
                               action_id=self.action_id, now=self.dt())

    def tracker(self, phase, total):
        return vp.Progress(self.progress, phase, total, now=self.now)


class _UndoTimeouts(dict):
    """Délais pendant un retour arrière : jamais au-delà de son échéance."""

    def __init__(self, base, ctx):
        super().__init__(base)
        self._ctx = ctx

    def _cap(self, v):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and self._ctx.undo_until is not None:
            return min(v, max(1, int(self._ctx.undo_until - self._ctx.now())))
        return v

    def __getitem__(self, k):
        return self._cap(super().__getitem__(k))

    def get(self, k, default=None):
        return self._cap(super().get(k, default))


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
    # point sûr avant l'étape : l'étape rangée à l'arrêt est celle qui allait
    # commencer (rien n'en est encore écrit)
    ctx.step = step
    ctx.safe_point()
    t0 = ctx.now()
    ctx.emit(step, "running", message or step)
    for u in uids:
        ctx.event(op, step, u, side, code="start", message=message or step)
    try:
        yield
    except Suspended as e:
        for u in uids:
            ctx.event(op, step, u, side, level="warn", code="interrupted", message=f"{step}: stopped ({e})")
        ctx.emit(step, "error", "stopped by the console shutdown")
        raise
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


def backup_volume_of(b):
    """Volume d'une sauvegarde : étiquette `backup-volume` (nom du volume,
    posée par le mutateur dès la création), sinon `status.volumeName`.
    Ce dernier n'est rempli qu'une fois la sauvegarde terminée
    (longhorn-manager v1.10.0 et v1.12.0, controller/backup_controller.go,
    synchronisation d'une sauvegarde `Completed`) : une sauvegarde en cours
    ou en erreur ne se reconnaît qu'à son étiquette."""
    return _labels(b).get("backup-volume") or _status(b).get("volumeName") or ""


def backups_by_volume(kube):
    """`Backup` de longhorn-system groupés par volume (`backup_volume_of`),
    rangés par date d'instantané. Une seule liste par appel."""
    out = {}
    for b in kube.list(K_LH_BACKUP, NS):
        vol = backup_volume_of(b)
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
    return {"generation": generation, "vm_uid": vm_uid, "source_volume": backup_volume_of(b),
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


IMAGE_CLASS_ANNOTATION = "harvesterhci.io/storageClassName"


def _image_class(kube, image_id, cache):
    """Classe sur laquelle une image Harvester a été bâtie :
    `spec.targetStorageClassName`, sinon l'annotation
    `harvesterhci.io/storageClassName` (le mutateur de Harvester pose les deux,
    pkg/image/common/mutator.go `EnsureTargetSC`). None si l'image est
    illisible ou absente."""
    if image_id in cache:
        return cache[image_id]
    ns, _, name = (image_id or "").partition("/")
    img = None
    if ns and name:
        try:
            img = kube.get(K_IMAGE, ns, name)
        except Exception:                       # noqa: BLE001
            img = None
    cache[image_id] = (_spec(img).get("targetStorageClassName")
                       or (_md(img).get("annotations") or {}).get(IMAGE_CLASS_ANNOTATION)) if img else None
    return cache[image_id]


def _vm_disks(kube, vm, classes, images=None):
    ns = _md(vm).get("namespace")
    images = {} if images is None else images
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
        image = (_md(pvc).get("annotations") or {}).get(rp.IMAGE_ID_ANNOTATION) or None
        out.append({"disk": vol.get("name"), "claim": claim, "size": size, "storage_class": sc_name,
                    "image": image, "image_class": _image_class(kube, image, images) if image else None,
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
    vms, bis, images = [], {}, {}
    for vm in _resolve_vms(ctx, src):
        md = _md(vm)
        disks = _vm_disks(src, vm, classes, images)
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


# ---------------------------------------------------------------------------
# VMs candidates (fenêtre de création d'un plan, `candidates`)
# ---------------------------------------------------------------------------

# constat bloquant d'un disque -> raison montrée
_CANDIDATE_REASONS = {"not-longhorn": "not-longhorn", "data-engine-v2": "longhorn-v2",
                      "encrypted-volume": "encrypted", "rwx-share-manager": "rwx-not-migratable"}


def plans_holding(conn, cluster):
    """{uid ou `namespace/nom`: [identifiants de plan]} des plans dont
    `cluster` est la source et qui tiennent encore la VM (tout état sauf
    `unprotected`) : par ressource rangée, et par la liste de VMs du plan."""
    out = {}
    for p in db.list_plans(conn, host=cluster):
        if p["source_cluster"] != cluster or p["state"] == "unprotected":
            continue
        keys = set(p.get("vms") or [])
        for r in db.list_resources(conn, p["id"], p["generation"]):
            keys.add(r["resource_uid"])
        for k in keys:
            out.setdefault(k, [])
            if p["id"] not in out[k]:
                out[k].append(p["id"])
    return out


def candidates(kube, cluster, namespace=None, selector=None, held=None):
    """Les VMs d'un cluster source qu'un plan peut protéger, avec leurs
    disques et ce qui les rend inéligibles (mêmes constats que le contrôle,
    `replication.volume_findings` et `vm_disk_findings`). Quatre listes
    seulement (VMs, VMI, PVC, PV) plus les volumes Longhorn et les classes,
    quel que soit le nombre de VMs. `held` : `plans_holding`. Une VM est
    éligible si elle a au moins un disque PVC et qu'aucun ne bloque ; un
    disque que la réplication ne recopie pas (containerDisk...) est montré,
    inéligible, sans rendre la VM inéligible."""
    held = held or {}
    ns = namespace or None
    vms = kube.list(K_VM, ns, selector=selector or None)
    vmis = {(_md(v).get("namespace"), _md(v).get("name")): v for v in kube.list(K_VMI, ns)}
    pvcs = {(_md(p).get("namespace"), _md(p).get("name")): p for p in kube.list(K_PVC, ns)}
    pvs = {_md(p).get("name"): p for p in kube.list(K_PV)}
    lhv = {_md(v).get("name"): v for v in kube.list(K_LH_VOLUME, NS)}
    classes = {_md(c).get("name"): c for c in kube.list(K_SC)}
    out = []
    for vm in sorted(vms, key=lambda v: (_md(v).get("namespace") or "", _md(v).get("name") or "")):
        md = _md(vm)
        vns, name = md.get("namespace"), md.get("name")
        tspec = vt._tspec(vm)
        domain = tspec.get("domain") or {}
        cpu = domain.get("cpu") or {}
        res = domain.get("resources") or {}
        n_cpu = int(cpu.get("cores") or 1) * int(cpu.get("sockets") or 1) * int(cpu.get("threads") or 1) \
            if cpu else _int((res.get("limits") or {}).get("cpu")) or 1
        memory = (domain.get("memory") or {}).get("guest") or (res.get("limits") or {}).get("memory") or ""
        ci = next((("nocloud" if "cloudInitNoCloud" in v else "configdrive") for v in tspec.get("volumes") or []
                   if "cloudInitNoCloud" in v or "cloudInitConfigDrive" in v), "")
        disks, reasons = [], []
        for vol in tspec.get("volumes") or []:
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            kind = next((t for t in rp._DISK_TYPES_NOT_REPLICATED if t in vol), None)
            if kind:
                disks.append({"disk": vol.get("name"), "claim": "", "size": 0, "used": 0, "storage_class": "",
                              "image": "", "eligible": False, "reason": kind})
                reasons.append(kind)
                continue
            if not claim:
                continue
            pvc = pvcs.get((vns, claim)) or {}
            ps = _spec(pvc)
            pv = pvs.get(ps.get("volumeName")) if ps.get("volumeName") else None
            lh_name = ((_spec(pv).get("csi") or {}).get("volumeHandle")) or ps.get("volumeName") or ""
            lh = lhv.get(lh_name)
            sc = classes.get(ps.get("storageClassName"))
            blocks = [f["code"] for f in rp.volume_findings(lh, pvc or {"metadata": {"name": claim}}, sc)
                      if f["level"] == "block"]
            reason = _CANDIDATE_REASONS.get(blocks[0], blocks[0]) if blocks else ""
            if reason:
                reasons.append(reason)
            disks.append({"disk": vol.get("name"), "claim": claim,
                          "size": vt.parse_quantity(((ps.get("resources") or {}).get("requests") or {}).get(
                              "storage")) or _int(_spec(lh).get("size")),
                          "used": _int(_status(lh).get("actualSize")),
                          "storage_class": ps.get("storageClassName") or "",
                          "image": (_md(pvc).get("annotations") or {}).get(rp.IMAGE_ID_ANNOTATION) or "",
                          "eligible": not reason, "reason": reason})
        pvc_disks = [d for d in disks if d["claim"]]
        if not pvc_disks:
            reasons.append("no-disk")
        eligible = bool(pvc_disks) and all(d["eligible"] for d in pvc_disks)
        plans = sorted(set(held.get(md.get("uid"), [])) | set(held.get(f"{vns}/{name}", [])))
        out.append({"namespace": vns, "name": name, "uid": md.get("uid") or "",
                    "running": _status(vmis.get((vns, name))).get("phase") == "Running",
                    "labels": dict(md.get("labels") or {}), "cpu": n_cpu, "memory": memory,
                    "networks": vt.vm_inventory(vm, {})["networks"], "cloud_init": ci, "disks": disks,
                    "eligible": eligible, "reasons": sorted(set(reasons)), "plans": plans})
    return {"cluster": cluster, "vms": out}


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
        ref = f"{vm['namespace']}/{vm['name']}"
        for d in vm.get("disks") or []:
            sc, _f = rp._target_class(d, plan, dst_facts, ref)
            params = ((dst_facts.get("classes_raw") or {}).get(sc) or {}).get("parameters") or {}
            spec = _spec(d.get("lh_volume"))
            image = d.get("image")
            # une classe choisie pour ce disque l'emporte sur l'image mappée :
            # le PVC ne porte alors pas l'image de la cible (sa classe serait
            # une autre)
            mapped = (maps.get("images") or {}).get(image) \
                if image and not rp.disk_class_override(plan, ref, d["disk"]) else None
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
    if ctx.on_finish is not None:
        ctx.on_finish()
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


_GUARD_FIELDS = ("state", "error_origin", "generation", "vms", "selector", "selector_namespace", "mappings")


def _plan_guard(plan):
    """Ce qui, lu avant le verrou, ne doit pas avoir changé une fois le
    verrou pris : état (et origine), génération, sélection des VMs."""
    return json.dumps({k: (plan or {}).get(k) for k in _GUARD_FIELDS}, sort_keys=True, default=str)


def _sel_key(sel):
    return sorted((r["resource_uid"], r["state"]) for r in sel)


def _lock_checked(ctx, command, before, select=None, sel_before=None):
    """Le verrou, puis la précondition et la sélection refaites SOUS le
    verrou : une autre commande a pu changer le plan entre la lecture et la
    prise. Changé : verrou rendu, refus `state-changed` (rien n'est écrit)."""
    _lock(ctx, command)
    try:
        _precondition(ctx, command)
        changed = _plan_guard(ctx.plan) != before
        if not changed and select is not None:
            changed = select() != sel_before
    except Refused:
        changed = True
    if changed:
        _unlock(ctx)
        raise Refused("state-changed", f"the plan changed while {command} was starting (now "
                      f"{ctx.plan['state']}): read it again before running {command}",
                      facts={"state": ctx.plan["state"]})


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


_LATE = " (undo deadline reached)"


def _undo_delete(ctx, kube, kind, ns, name):
    """Suppression d'un retour arrière, dans son échéance : rien n'est
    tenté une fois l'échéance passée (False)."""
    if ctx.undo_expired():
        return False
    return run._undo_delete(ctx, kube, kind, ns, name)


def _undo_created(ctx):
    """Retour arrière de ce que la commande a posé : étiquettes retirées,
    objets créés supprimés, dans l'ordre inverse. Rend ce qui reste. Tout
    tient dans l'échéance de `ctx.begin_undo()` : passée, chaque objet pas
    encore défait est listé sans être tenté (`ctx.undo_timed_out`)."""
    ctx.begin_undo()
    left = []
    for side, kind, ns, name, patch in reversed(ctx.undo_labels):
        if ctx.undo_expired():
            left.append(f"labels of {kind} {name}" + _LATE)
            continue
        try:
            ctx.kube(side).patch(kind, ns, name, {"metadata": {"labels": patch}})
        except Exception:                       # noqa: BLE001
            left.append(f"labels of {kind} {name}")
    ctx.undo_labels.clear()
    for uid, disk in ctx.undo_default:
        db.upsert_volume(ctx.conn, ctx.plan_id, {"generation": ctx.gen, "vm_uid": uid, "disk": disk,
                                                 "default_label_added": 0})
    ctx.undo_default.clear()
    for side, kind, ns, name, patch in reversed(ctx.undo_specs):
        # objet créé par cette commande : il sera supprimé juste après
        if (side, kind, ns, name) in ctx.created:
            continue
        if ctx.undo_expired():
            left.append(f"spec of {kind} {name}" + _LATE)
            continue
        try:
            ctx.kube(side).patch(kind, ns, name, patch)
        except Exception:                       # noqa: BLE001
            left.append(f"spec of {kind} {name}")
    ctx.undo_specs.clear()
    for side, kind, ns, name in reversed(list(ctx.created)):
        if not _undo_delete(ctx, ctx.kube(side), kind, ns, name):
            left.append(f"{kind} {ns + '/' if ns else ''}{name}" + (_LATE if ctx.undo_timed_out else ""))
    ctx.created.clear()
    return left


def _undo_timeout(ctx, op, message, left):
    """Le retour arrière a dépassé son échéance : ce qui reste est écrit au
    journal, une ligne par objet (`plan/rollback`, code `undo-timeout`)."""
    total = int(dict.get(ctx.timeouts, "undo_total") or rp.TIMEOUTS["undo_total"])
    ctx.event(op, "rollback", level="error", code="undo-timeout",
              message="undo stopped at its deadline, left: " + "; ".join(left)[:400],
              facts={"left": left, "undo_total_s": total})
    ctx.emit("rollback", "error", "undo deadline reached: " + "; ".join(left)[:300])
    return _result(ctx, "undo-timeout", EXIT_FAIL, f"{message}; undo stopped at its deadline", left=left)


# ---------------------------------------------------------------------------
# Briques Longhorn
# ---------------------------------------------------------------------------

DEFAULT_GROUP_LABEL = rp.JOB_GROUP_LABEL_PREFIX + rp.DEFAULT_GROUP


def _label_volume(ctx, volume, job, keep_default, row=None):
    """Étiquette de notre tâche (et du groupe `default` quand le volume y
    était implicitement, voir `replication.backup_membership`). `row`
    (vm_uid, disque) : la ligne du volume où noter que l'étiquette `default`
    vient de nous (notée AVANT la pose : un arrêt brutal entre les deux ne
    fait pas oublier qu'on l'a posée), pour que la déprotection ne retire
    que celle-là."""
    lv = ctx.src.get(K_LH_VOLUME, NS, volume)
    if lv is None:
        raise RunError("source-volume-missing", f"Longhorn volume {volume} not found on the source")
    labels = _labels(lv)
    want = dict([rp.job_label(job)])
    if keep_default:
        want[DEFAULT_GROUP_LABEL] = "enabled"
    add = {k: v for k, v in want.items() if labels.get(k) != v}
    if not add:
        return False
    if DEFAULT_GROUP_LABEL in add and row:
        db.upsert_volume(ctx.conn, ctx.plan_id, {"generation": ctx.gen, "vm_uid": row[0], "disk": row[1],
                                                 "default_label_added": 1})
        ctx.undo_default.append(row)
    ctx.src.patch(K_LH_VOLUME, NS, volume, {"metadata": {"labels": add}})
    ctx.undo_labels.append(("src", K_LH_VOLUME, NS, volume, {k: None for k in add}))
    return True


def _unlabel_volume(ctx, volume, job):
    """Retire notre étiquette de tâche (l'étiquette du groupe `default`, si
    elle vient de nous, ne part qu'à la déprotection : `_remove_jobs`)."""
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


# Erreur passagère d'un instantané d'un volume détaché (VM arrêtée par la
# bascule) : Longhorn 1.11.2 l'écrit dans `status.error` le temps d'attacher
# le volume lui-même, puis prend l'instantané (vu sur le banc ; la bascule
# planifiée abandonnait là et défaisait tout).
SNAPSHOT_WAITING = ("waiting for the volume to be attached", "engine is not running")


def _snapshot_name(ctx, vol, kind, token, tries=8):
    """Nom déterministe de l'instantané, sauf s'il est tenu par le reste d'une
    exécution annulée : Longhorn recrée le `Snapshot` d'un instantané effacé
    (`status.markRemoved`, sans étiquettes, `createSnapshot: false`) tant que
    le moteur ne l'a pas purgé (vu sur Longhorn 1.11.2 après un `protect`
    annulé : la relance refusait `snapshot-taken`). Le nom suivant de la
    suite est alors pris ; une relance retrouve le sien de la même façon."""
    name = op_name(ctx.plan_id, vol, ctx.gen, kind, token)
    for i in range(1, tries):
        cur = ctx.src.get(K_LH_SNAPSHOT, NS, name)
        if cur is None or _owned(ctx, cur) or not _status(cur).get("markRemoved"):
            return name
        name = op_name(ctx.plan_id, vol, ctx.gen, kind, f"{token}#{i}")
    return name


def take_backups(ctx, op, items, kind, token=""):
    """Instantané puis sauvegarde de chaque volume (`items` : [(uid,
    volume)]), noms déterministes ; rend {volume: nom de la sauvegarde}."""
    labels = rp.plan_labels(ctx.plan_id, ctx.gen)
    names = {vol: _snapshot_name(ctx, vol, kind, token) for _u, vol in items}
    for uid, vol in items:
        _ensure(ctx, "src", rp.snapshot(vol, names[vol], labels=dict(labels, **{rp.LABEL_VM: uid})), "snapshot")

    def ready():
        for _u, vol in items:
            s = ctx.src.get(K_LH_SNAPSHOT, NS, names[vol])
            if s is None:
                return Fail(f"snapshot {names[vol]} disappeared")
            err = _status(s).get("error") or ""
            if err and not any(m in err.lower() for m in SNAPSHOT_WAITING):
                return Fail(f"snapshot {names[vol]}: {err}")
            if not _status(s).get("readyToUse"):
                return "snapshots being taken"
        return True
    wait_for(ctx, op, "snapshots ready", ready, ctx.timeouts["snapshot"], every=2)
    for uid, vol in items:
        # étiquette `backup-volume` = nom du VOLUME, jamais celui du
        # BackupVolume : le contrôleur des sauvegardes cherche le moteur du
        # volume qu'elle nomme (longhorn-manager v1.12.0,
        # controller/backup_controller.go getBackupVolumeName, et le
        # mutateur webhook/resources/backup/mutator.go)
        _ensure(ctx, "src", rp.backup(vol, names[vol], target=BACKUP_TARGET,
                                      labels=dict(labels, **{rp.LABEL_VM: uid})),
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
        # namespace commun à tous les plans : jamais supprimé ; un autre plan
        # peut le créer au même instant
        try:
            dst.create({"apiVersion": "v1", "kind": "Namespace",
                        "metadata": {"name": rp.CARNET_NAMESPACE, "labels": dict(MANAGED_BY)}})
        except Exception as e:                  # noqa: BLE001
            if "alreadyexists" not in str(e).lower().replace(" ", "") \
                    and dst.get(K_NS, None, rp.CARNET_NAMESPACE) is None:
                raise
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
        before = _plan_guard(ctx.plan)
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
        ctx.safe_point()
        _lock_checked(ctx, "measure-start", before)
    except Refused as e:
        return _refusal(ctx, "measure", e)
    except Suspended as e:
        return _not_started(ctx, "measure", e)
    except Cancelled as e:
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e))
    except Exception as e:                      # noqa: BLE001
        return _preflight_failed(ctx, "measure", e)
    uids = [w["uid"] for w in works]
    run_prev, started, steps_before = None, ctx.dt(), []
    try:
        run_prev, started, steps_before = _begin_run(ctx, "measure-start")
        _set_state(ctx, "measuring")
        ctx.event("measure", "start", code="measuring", facts={"interval_s": interval})
        _record_works(ctx, works, "measuring")
        _ensure_jobs(ctx, "measure", works, rp.cron_for(interval), ctx.plan["retain"], src.get("jobs") or {})
        _end_run(ctx, "measure-start", run_prev, started, steps_before, "ok")
        _unlock(ctx)
        return _result(ctx, "measuring", EXIT_OK, f"measuring every {interval} s", interval_s=interval,
                       vms=len(uids))
    except Suspended as e:
        return _suspended(ctx, "measure", e, uids, run_op="measure-start", prev=run_prev, started=started,
                          steps_before=steps_before, points={"interval_s": interval})
    except Cancelled as e:
        left = _undo_created(ctx)
        _end_run(ctx, "measure-start", run_prev, started, steps_before, "cancelled")
        _error(ctx, uids, f"measure start cancelled: {e}")
        if ctx.undo_timed_out:
            return _undo_timeout(ctx, "measure", f"measure start cancelled ({e})", left)
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e))
    except Exception as e:                      # noqa: BLE001
        _keep_run_open(ctx, "measure-start", run_prev, started, steps_before)
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
        before = _plan_guard(ctx.plan)
        if probe(ctx, "src") != "ok":
            raise Refused("source-unreachable", "the source cluster does not answer")
        ctx.safe_point()
        _lock_checked(ctx, "measure-stop", before)
    except Refused as e:
        return _refusal(ctx, "measure", e)
    except Suspended as e:
        return _not_started(ctx, "measure", e)
    except Cancelled as e:
        _unlock(ctx)
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e))
    except Exception as e:                      # noqa: BLE001
        return _preflight_failed(ctx, "measure", e)
    res = db.list_resources(ctx.conn, ctx.plan_id, ctx.gen)
    uids = [r["resource_uid"] for r in res]
    run_prev, started, steps_before = None, ctx.dt(), []
    try:
        run_prev, started, steps_before = _begin_run(ctx, "measure-stop")
        n = collect_measures(ctx)
        _remove_jobs(ctx, "measure", res)
        _set_resources(ctx, uids, "draft")
        _set_state(ctx, "draft")
        _end_run(ctx, "measure-stop", run_prev, started, steps_before, "ok")
        _unlock(ctx)
        return _result(ctx, "draft", EXIT_OK, "measure stopped", passes=n)
    except Suspended as e:
        # le plan reste `measuring` : `resume` termine l'arrêt de la mesure
        return _suspended(ctx, "measure", e, uids, run_op="measure-stop", prev=run_prev, started=started,
                          steps_before=steps_before)
    except Cancelled as e:
        # rien n'est défait : la relance termine l'arrêt de la mesure
        _end_run(ctx, "measure-stop", run_prev, started, steps_before, "cancelled")
        _error(ctx, uids, f"measure stop cancelled: {e}")
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e))
    except Exception as e:                      # noqa: BLE001
        # le plan passe en `error` : l'arrêt de la mesure ne part pas de là,
        # l'exécution reprise est close
        _end_run(ctx, "measure-stop", run_prev, started, steps_before, "failed")
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
                    # annulation : la tâche reprise (mesure, relance) retrouve ses valeurs
                    ctx.undo_specs.append(("src", K_LH_JOB, NS, w["job"],
                                           {"spec": {k: _inverse(sp.get(k), v) for k, v in diff.items()}}))
            for d in w["disks"]:
                keep = rp.backup_membership(d["lh_volume"], jobs, w["job"])["keep_default_label"]
                _label_volume(ctx, d["source_volume"], w["job"], keep, row=(w["uid"], d["disk"]))


def _inverse(old, new):
    """Patch de fusion qui rend `old` à un champ passé à `new` (clés
    ajoutées d'un dictionnaire remises à null)."""
    if isinstance(old, dict) and isinstance(new, dict):
        return {k: old.get(k) for k in set(old) | set(new)}
    return old


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
            # l'étiquette du groupe `default`, seulement là où la réplication l'a posée
            for v in db.list_volumes(ctx.conn, ctx.plan_id, vm_uid=r["resource_uid"]):
                if not v.get("default_label_added"):
                    continue
                lv = ctx.src.get(K_LH_VOLUME, NS, v["source_volume"]) if v["source_volume"] else None
                if lv is not None and DEFAULT_GROUP_LABEL in _labels(lv):
                    ctx.src.patch(K_LH_VOLUME, NS, v["source_volume"],
                                  {"metadata": {"labels": {DEFAULT_GROUP_LABEL: None}}})
                db.upsert_volume(ctx.conn, ctx.plan_id, {"generation": int(v["generation"]), "vm_uid": v["vm_uid"],
                                                         "disk": v["disk"], "default_label_added": 0})
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
                    if _is_activated(cur):
                        raise RunError("standby-activated", f"volume {sb} was activated: not a standby any more")
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
    # octets à restaurer : la taille des données de la sauvegarde
    # (`Backup.status.size` de Longhorn, blocs de la sauvegarde entière), pas
    # la taille nominale du disque (vu sur le banc, v1.99.0 : « 5.0 GiB en
    # 14 s » pour environ 150 Mo restaurés). Longhorn ne publie pas d'octets
    # restaurés : la progression vient du pourcentage des moteurs appliqué à
    # cette taille. Sans taille de sauvegarde, la taille nominale est prise et
    # le bilan le dit (« volume size »).
    sizes, nominal = {}, False
    for w in works:
        for d in w["disks"]:
            data = _int(_status(latest.get(d["source_volume"])).get("size"))
            if data <= 0:
                nominal = True
            sizes[d["target_volume"]] = data if data > 0 else int(d["size"])
    wanted = {d["target_volume"]: (d["source_volume"], names[d["source_volume"]]) for w in works for d in w["disks"]}
    # v1.99.0 : une relance sur un plan protégé trouve ses volumes de secours
    # déjà à jour ; ils ne comptent pas dans le bilan (vu au rejeu : « 348 MiB
    # en 2 s » sans rien restaurer)
    def up_to_date(sb, name):
        try:
            return (_status(engine_of(ctx.dst, sb)).get("lastRestoredBackup") or "") == name
        except Exception as e:                  # noqa: BLE001
            # API momentanément injoignable : compté comme à restaurer,
            # l'attente qui suit réessaie
            if not run.transient(e):
                raise
            return False
    todo = {sb: sizes[sb] for sb, (_vol, name) in wanted.items() if not up_to_date(sb, name)}
    total = sum(todo.values())
    label = "initial-restore (volume size)" if nominal else "initial-restore (backup data)"
    prog = ctx.tracker("initial-restore", total)

    # décision 68 : une reprise après un arrêt de la console trouve une
    # restauration déjà avancée (Longhorn a continué sans la console) ; ce
    # qui était fait au premier relevé est dit à part, rien n'est recopié
    already = [None]

    def progress_hook():
        done = 0
        for sb in todo:
            st = (_status(engine_of(ctx.dst, sb)).get("restoreStatus") or {}).values()
            pct = [float((s or {}).get("progress") or 0) for s in st]
            done += todo[sb] * (sum(pct) / len(pct) / 100.0 if pct else 0)
        if already[0] is None:
            already[0] = int(done)
        prog.update(int(done))

    with stage(ctx, "protect", "initial-restore", "target", [w["uid"] for w in works]):
        # la progression (octets restaurés des moteurs) est relevée à chaque tour
        applied = wait_applied(ctx, "initial-restore", wanted, "initial_restore", on_poll=progress_hook)
        prog.update(total)
        if todo:
            text = vp.summary(label, prog.finish())
            if already[0]:
                text += f" ({vp.fmt_bytes(already[0])} already restored before the interruption)"
                ctx.event("protect", "initial-restore", side="target", code="continued",
                          message="initial restore continued, not restarted",
                          facts={"already_restored_bytes": already[0], "total_bytes": total})
            ctx.emit("initial-restore", "running", text)
        else:
            prog.finish()
            ctx.emit("initial-restore", "running",
                     "standby volumes already hold the latest backups: nothing restored")
    now = ctx.dt()
    for w in works:
        for d in w["disks"]:
            b = latest[d["source_volume"]]
            db.record_tick(ctx.conn, ctx.plan_id, _tick_from_backup(b, w["uid"], ctx.gen))
            db.mark_applied(ctx.conn, ctx.plan_id, d["source_volume"], _md(b)["name"], now, ctx.gen,
                            applied_by="protect")
            db.upsert_volume(ctx.conn, ctx.plan_id, {"generation": ctx.gen, "vm_uid": w["uid"], "disk": d["disk"],
                                                     "state": "standby"})
    return applied


def _dry_run_protect(ctx, works, src_facts, dst_facts):
    """Dry-run serveur de chaque objet cible (volumes de secours, backing
    images, carnet) et de la tâche source. Une backing image sans sauvegarde
    visible passe avec une URL d'essai (`placeholder_url`, acceptée par
    Longhorn 1.12.1) ; un volume de secours sans sauvegarde, sans backing
    image ou sans namespace des carnets ne peut pas l'être : `deferred`."""
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
    # backing images absentes de la cible (étape 1 de la spec : tout objet
    # que la protection posera passe au dry-run serveur)
    bbis = {}
    for b in ctx.dst.list(K_LH_BBI, NS):
        for key in (_spec(b).get("backingImage"), _status(b).get("backingImage"), _md(b).get("name")):
            if key and _status(b).get("url"):
                bbis.setdefault(key, _status(b)["url"])
    for bi in sorted({d["backing_image"] for w in works for d in w["disks"] if d["backing_image"]}):
        if bi in (dst_facts.get("backing_images") or {}):
            out.append({"object": "backing-image", "name": bi, "ok": True, "exists": True})
            continue
        checksum = ((src_facts.get("backing_images") or {}).get(bi) or {}).get("checksum") or ""
        url = bbis.get(bi) or ""
        attempt("dst", rp.backing_image_restore(bi, checksum, url or f"dry-run://?backingImage={bi}",
                                                BACKUP_TARGET, labels=labels),
                "backing-image", placeholder_url=not url)
    # le namespace des carnets et les backing images à restaurer n'existent
    # qu'une fois la protection lancée : le dry-run d'un objet qui en dépend
    # échouerait toujours avant la première protection. Le namespace passe
    # lui-même au dry-run ; l'objet dépendant est reporté, signalé par
    # `deferred`.
    carnet_ns_missing = ctx.dst.get(K_NS, None, rp.CARNET_NAMESPACE) is None
    if carnet_ns_missing:
        attempt("dst", {"apiVersion": "v1", "kind": "Namespace",
                        "metadata": {"name": rp.CARNET_NAMESPACE, "labels": dict(MANAGED_BY)}}, "namespace")
    present_bis = set(dst_facts.get("backing_images") or {})

    def present(side, kind, ns, name, what):
        """Objet déjà posé par une protection précédente (relance après un
        `plan-drift`) : la création serait refusée (AlreadyExists), la
        protection le met à jour."""
        o = ctx.kube(side).get(kind, ns, name)
        if o is None:
            return False
        out.append({"object": what, "name": name, "ok": _owned(ctx, o), "exists": True,
                    **({} if _owned(ctx, o) else {"message": "exists and is not owned by this plan"})})
        return True
    for w in works:
        if not present("src", K_LH_JOB, NS, w["job"], "recurring-job"):
            attempt("src", rp.recurring_job(w["job"], rp.cron_for(ctx.plan.get("interval_s") or 300), ctx.plan["retain"],
                                            max(1, len(w["disks"])), ctx.plan_id, w["uid"], ctx.gen),
                    "recurring-job")
        for d in w["disks"]:
            done = _completed(tb.get(d["source_volume"]))
            url = _status(done[-1]).get("url") if done else ""
            if (dst_facts.get("volumes") or {}).get(d["target_volume"]) is not None:
                out.append({"object": "standby-volume", "name": d["target_volume"], "ok": True, "exists": True})
                continue
            if d["backing_image"] and d["backing_image"] not in present_bis:
                # Longhorn lit la backing image de la sauvegarde même sans
                # `backingImage` dans le volume (vu sur 1.12.1) : rien à
                # essayer avant sa restauration, que la ligne au-dessus a
                # passée au dry-run
                out.append({"object": "standby-volume", "name": d["target_volume"], "ok": True,
                            "deferred": "backing-image", "placeholder_url": not url})
                continue
            if not url:
                # Longhorn 1.12.1 cherche le `Backup` nommé dans `fromBackup`
                # (« cannot get backup none ») : un volume de secours ne passe
                # au dry-run qu'une fois la première sauvegarde faite
                out.append({"object": "standby-volume", "name": d["target_volume"], "ok": True,
                            "deferred": "first-backup", "placeholder_url": True})
                continue
            attempt("dst", rp.standby_volume(
                d["target_volume"], url, d["size"], d["access_mode"], d["migratable"], d["target_replicas"],
                d["backing_image"] or None, d["class_params"], labels=dict(labels, **{rp.LABEL_VM: w["uid"]})),
                "standby-volume", placeholder_url=False)
        res = {"resource_uid": w["uid"], "namespace": w["namespace"], "name": w["name"],
               "firmware_uuid": w["firmware_uuid"]}
        try:
            vm, _r = rp.target_vm(w["vm"], res, ctx.plan.get("mappings") or {}, ctx.plan.get("options") or {},
                                  {d["claim"]: d["target_claim"] for d in w["disks"]}, ctx.plan_id, ctx.gen)
            m = carnet_manifest(ctx.plan_id, ctx.gen, w, vm, [], carnet_volumes(w), ctx.plan["source_cluster"])
            if carnet_ns_missing:
                out.append({"object": "carnet", "name": m["metadata"]["name"], "ok": True, "deferred": "namespace"})
            elif not present("dst", K_SECRET, rp.CARNET_NAMESPACE, m["metadata"]["name"], "carnet"):
                attempt("dst", m, "carnet")
        except ValueError as e:
            out.append({"object": "carnet", "name": rp.carnet_name(ctx.plan_id, w["uid"]), "ok": False,
                        "message": str(e)})
    return out


def _choose_retain(retain):
    """`--retain` de protect (v1.99.0, décision 56) : None garde la valeur du
    plan ; sinon un entier entre RETAIN_MIN et RETAIN_MAX."""
    if retain in (None, ""):
        return None
    try:
        n = int(retain)
    except (TypeError, ValueError):
        n = None
    if isinstance(retain, bool) or n is None or str(n) != str(retain).strip() \
            or not rp.RETAIN_MIN <= n <= rp.RETAIN_MAX:
        raise Refused("bad-retain", f"retain: an integer between {rp.RETAIN_MIN} and {rp.RETAIN_MAX}")
    return n


def protect(ctx, interval=None, dry_run=False, retain=None):
    """(`draft`, `measuring`, `protecting`, `protected`, `error`) ->
    `protecting` -> `protected`. Rejouable : chaque sous-étape constate
    l'existant. Annulation : retour arrière de ce que cette exécution a
    créé. Échec : rien n'est défait (la relance reprend, une longue
    restauration initiale n'est pas perdue), plan en `error`.
    `retain` (v1.99.0, décision 56) : nombre de sauvegardes gardées par la
    tâche récurrente ; sur un plan déjà protégé, la tâche existante est mise
    à jour avec la ligne du plan (seul chemin pour changer la rétention)."""
    uids = []
    prev = {}
    try:
        _precondition(ctx, "protect")
        before = _plan_guard(ctx.plan)
        new_retain = _choose_retain(retain)
        src = collect_source(ctx)
        # une VM déjà basculée n'est plus protégée : ni tâche, ni étiquette sur
        # sa source arrêtée, ni volume de secours (son volume est activé)
        skipped = _failed_over(ctx, src)
        src["vms"] = [v for v in src.get("vms") or [] if v["uid"] not in skipped]
        uids = [v["uid"] for v in src.get("vms") or []]
        dst = collect_target(ctx, [rp.target_ref(ctx.plan, {"namespace": v["namespace"], "name": v["name"]})[0]
                                   for v in src.get("vms") or []])
        findings = rp.check(src, dst, dict(ctx.plan, id=ctx.plan_id, phase="protect"))
        if rp.blocking(findings):
            raise Refused("check", "the pre-check refuses this protection", findings=findings)
        works = build_works(ctx, src, dst)
        activated = sorted(d["target_volume"] for w in works for d in w["disks"]
                           if _is_activated(ctx.dst.get(K_LH_VOLUME, NS, d["target_volume"])))
        if activated:
            raise Refused("standby-activated", "a target volume was activated by a failover: it cannot be "
                          "a standby again", facts={"volumes": activated})
        for uid, why in sorted(skipped.items()):
            ctx.event("protect", "skip", uid, "source", code="failed-over",
                      message=f"failed over ({why}): not protected again")
        if dry_run:
            dry = _dry_run_protect(ctx, works, src, dst)
            ok = all(x["ok"] for x in dry)
            return _result(ctx, "dry-run", EXIT_OK if ok else EXIT_BLOCKED, "dry run", findings=findings, dry_run=dry)
        chosen, how = _choose_interval(ctx, interval)
        # arrêt de la console demandé pendant les lectures : la commande ne
        # commence pas (rien n'est écrit, rien à reprendre)
        ctx.safe_point()
        _lock_checked(ctx, "protect", before)
    except Refused as e:
        return _refusal(ctx, "protect", e, uids)
    except Suspended as e:
        return _not_started(ctx, "protect", e)
    except Cancelled as e:
        _unlock(ctx)
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e))
    except Exception as e:                      # noqa: BLE001
        return _preflight_failed(ctx, "protect", e, uids)
    run_prev, started, steps_before = None, ctx.dt(), []
    try:
        prev = {"interval_s": ctx.plan.get("interval_s"), "retain": ctx.plan.get("retain")}
        # reprise après un arrêt de la console (décision 68) : l'exécution
        # interrompue rend ses listes de retour arrière et les valeurs du plan
        # d'avant elle, pour qu'une annulation de la reprise défasse tout
        run_prev, started, steps_before = _begin_run(ctx, "protect")
        if run_prev is not None:
            prev = dict((run_prev.get("points") or {}).get("plan_prev") or prev)
        if new_retain is not None and new_retain != ctx.plan.get("retain"):
            db.update_plan(ctx.conn, ctx.plan_id, now=ctx.dt(), retain=new_retain)
            ctx.reload()
            ctx.event("protect", "retain", code="retain", message=f"{new_retain} backups kept",
                      facts={"retain": new_retain, "previous": prev["retain"]})
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
        # carnets réécrits avec les réglages du moment : plus rien d'en retard
        db.clear_plan_flag(ctx.conn, "carnet-stale", ctx.plan_id)
        _set_resources(ctx, uids, "protected")
        _set_state(ctx, "protected")
        ctx.event("protect", "done", code="protected", facts={"interval_s": chosen, "how": how, "vms": len(uids)})
        _end_run(ctx, "protect", run_prev, started, steps_before, "ok")
        _unlock(ctx)
        return _result(ctx, "protected", EXIT_OK, f"{len(uids)} VM(s) protected", interval_s=chosen,
                       findings=findings)
    except Suspended as e:
        # arrêt de la console : rien n'est défait, le plan reste `protecting`
        return _suspended(ctx, "protect", e, uids, run_op="protect", prev=run_prev, started=started,
                          steps_before=steps_before, points={"plan_prev": prev})
    except Cancelled as e:
        left = _undo_created(ctx)
        _end_run(ctx, "protect", run_prev, started, steps_before, "cancelled")
        # la tâche reprise retrouve ses valeurs (`undo_specs`) : la ligne du
        # plan aussi
        back = {k: v for k, v in prev.items() if v is not None and ctx.reload().get(k) != v}
        if back:
            db.update_plan(ctx.conn, ctx.plan_id, now=ctx.dt(), **back)
            ctx.reload()
        if ctx.undo_timed_out:
            _error(ctx, uids, f"protect cancelled: {e}")
            return _undo_timeout(ctx, "protect", f"protect cancelled ({e})", left)
        _error(ctx, uids, f"protect cancelled: {e}", left)
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e), left=left)
    except Exception as e:                      # noqa: BLE001
        _keep_run_open(ctx, "protect", run_prev, started, steps_before)
        _error(ctx, uids, f"protect failed: {e}")
        return _result(ctx, error_code(e), EXIT_FAIL, str(e))


def _failed_over(ctx, src_facts):
    """VMs déjà basculées : ressource `failed_over` en base, ou VM source
    annotée par une bascule. Rend {uid: raison}."""
    out = {r["resource_uid"]: "state" for r in db.list_resources(ctx.conn, ctx.plan_id, ctx.gen)
           if r["state"] == "failed_over"}
    for v in src_facts.get("vms") or []:
        if FAILED_OVER_TO in (_md(v.get("vm")).get("annotations") or {}):
            out.setdefault(v["uid"], "annotation")
    return out


def _preflight_failed(ctx, op, e, uids=()):
    """Échec d'une lecture ou d'un dry-run avant toute écriture : même
    contrat que les autres sorties (`exit` 1, `code`), jamais d'exception
    brute vers l'appelant."""
    code = error_code(e)
    for u in list(uids) or [""]:
        ctx.event(op, "check", u, level="error", code=code, message=f"pre-check failed: {e}")
    ctx.emit("check", "error", str(e)[:400])
    _unlock(ctx)
    return _result(ctx, code, EXIT_FAIL, f"pre-check failed: {e}")


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
# Arrêt de la console : exécution interrompue, reprise (décision 68)
# ---------------------------------------------------------------------------

# commandes qui s'arrêtent sans retour arrière à l'arrêt de la console ; leur
# exécution interrompue est rangée dans `runs` (result `interrupted`, sans
# `finished_at`) avec l'étape atteinte et de quoi défaire plus tard
SUSPENDABLE_RUNS = ("protect", "measure-start", "measure-stop")


def _undo_state(ctx):
    """Ce qu'une annulation devrait défaire, rangé avec l'exécution
    interrompue : une annulation de la REPRISE défait alors toute la
    commande, comme l'aurait fait une annulation avant l'arrêt."""
    return {"created": [list(x) for x in ctx.created],
            "labels": [list(x) for x in ctx.undo_labels],
            "specs": [list(x) for x in ctx.undo_specs],
            "default": [list(x) for x in ctx.undo_default]}


def _load_undo(ctx, saved):
    """Reprend les listes de retour arrière d'une exécution interrompue
    (sans doublon : la reprise ne recrée pas ce qui existe, elle ne le note
    donc pas une seconde fois)."""
    saved = saved or {}
    for key, attr in (("created", "created"), ("labels", "undo_labels"), ("specs", "undo_specs"),
                      ("default", "undo_default")):
        cur = getattr(ctx, attr)
        for x in saved.get(key) or []:
            t = tuple(x)
            if t not in cur:
                cur.append(t)


def _open_interrupted(ctx, operation):
    """Exécution interrompue par un arrêt de la console, encore ouverte."""
    r = _open_run(ctx, operation)
    return r if r and r.get("result") == "interrupted" else None


def _close_superseded(ctx, operations, keep=None):
    """Une autre commande prend la main sur le plan : les exécutions
    interrompues qu'elle remplace sont closes (`superseded`), `resume` ne
    les relancera plus."""
    for r in db.list_runs(ctx.conn, ctx.plan_id):
        if r["operation"] in operations and not r["finished_at"] and r["id"] != keep \
                and r.get("result") == "interrupted":
            db.record_run(ctx.conn, ctx.plan_id, r["operation"], r["started_at"], int(r["generation"]),
                          finished_at=ctx.dt(), result="superseded", vms=r.get("vms") or [],
                          points=r.get("points") or {}, steps=r.get("steps") or [], by=r.get("by") or "",
                          action_id=r.get("action_id") or "", run_id=r["id"])


def _begin_run(ctx, operation):
    """(exécution interrompue reprise ou None, début, étapes déjà faites) :
    la reprise retrouve son début, ses étapes et ses listes de retour
    arrière ; les autres exécutions interrompues sont closes."""
    prev = _open_interrupted(ctx, operation)
    _close_superseded(ctx, [o for o in SUSPENDABLE_RUNS], keep=prev["id"] if prev else None)
    if prev is None:
        return None, ctx.dt(), []
    pts = prev.get("points") or {}
    _load_undo(ctx, pts.get("undo"))
    ctx.event(operation.split("-")[0], "resume", code="resumed",
              message=f"resumed after the console shutdown, from step {pts.get('step') or 'start'}",
              facts={"from_step": pts.get("step") or "", "steps_done": pts.get("steps_done") or []})
    return prev, prev["started_at"], list(prev.get("steps") or [])


def _end_run(ctx, operation, prev, started, steps_before, result, points=None):
    """Clôt l'exécution reprise (réussite, annulation) ; rien sans reprise."""
    if prev is None:
        return
    db.record_run(ctx.conn, ctx.plan_id, operation, started, ctx.gen, finished_at=ctx.dt(), result=result,
                  points=dict(prev.get("points") or {}, **(points or {}), undo={}), steps=steps_before + ctx.steps,
                  by=ctx.by, action_id=ctx.action_id, run_id=prev["id"])


def _keep_run_open(ctx, operation, prev, started, steps_before):
    """Échec d'une reprise : l'exécution reste ouverte (`interrupted`), avec
    ce que cette reprise a ajouté à défaire."""
    if prev is None:
        return
    db.record_run(ctx.conn, ctx.plan_id, operation, started, ctx.gen, result="interrupted",
                  points=dict(prev.get("points") or {}, undo=_undo_state(ctx)), steps=steps_before + ctx.steps,
                  by=ctx.by, action_id=ctx.action_id, run_id=prev["id"])


def _suspended(ctx, op, e, uids, run_op=None, prev=None, started=None, steps_before=(), points=None,
               run_id=None):
    """Arrêt de la console pendant une commande : rien n'est défait, l'état
    du plan reste celui de la commande en cours (`protecting`,
    `measuring`, `unprotecting`), l'étape atteinte est rangée (journal et
    `runs`), le verrou est rendu. Sortie 3, code `interrupted` : `resume`
    continue la commande là où elle en était."""
    step = ctx.step or "start"
    done = [x["step"] for x in list(steps_before) + ctx.steps]
    facts = {"step": step, "steps_done": done, "reason": str(e)}
    msg = f"interrupted by the console shutdown at step {step}: nothing undone, resume the plan to continue"
    for u in list(uids) or [""]:
        ctx.event(op, "interrupted", u, level="warn", code="interrupted", message=msg, facts=facts)
    if run_op:
        rid = prev["id"] if prev else run_id
        db.record_run(ctx.conn, ctx.plan_id, run_op, started or ctx.dt(), ctx.gen, result="interrupted",
                      points=dict((prev or {}).get("points") or {}, **(points or {}), step=step, steps_done=done,
                                  reason=str(e), undo=_undo_state(ctx)),
                      steps=list(steps_before) + ctx.steps, by=ctx.by, action_id=ctx.action_id, run_id=rid)
    _unlock(ctx)
    return _result(ctx, "interrupted", EXIT_CANCELLED, msg, step=step, steps_done=done, suspended=True)


def _not_started(ctx, op, e):
    """Arrêt de la console avant la moindre écriture : la commande n'a pas
    commencé, il n'y a rien à reprendre (la relancer après le redémarrage)."""
    _unlock(ctx)
    ctx.emit("check", "error", f"not started: {e}")
    return _result(ctx, "not-started", EXIT_CANCELLED,
                   f"the console stopped ({e}) before this command changed anything: run it again", suspended=True)


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
                    ctx.dst.create(_pv(ctx, uid, tns, v), dry_run=True)
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


def _pv(ctx, uid, tns, v):
    # pré-lié à SON PVC (claimRef) : aucun autre PVC en attente ne le prend
    return rp.static_pv(v["target_volume"], v["size"], v["volume_mode"], v["access_modes"], v["storage_class"],
                        labels=rp.plan_labels(ctx.plan_id, ctx.gen, uid), claim_ref=(tns, v["target_claim"]))


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


def _restore_job(ctx, uid, disks):
    """Retour arrière de la clôture : la tâche de la VM revient (cron et
    rétention du plan)."""
    job = rp.recurring_job_name(ctx.plan_id, uid)
    if ctx.src.get(K_LH_JOB, NS, job) is None:
        ctx.src.create(rp.recurring_job(job, ctx.plan["cron"], ctx.plan["retain"], max(1, disks), ctx.plan_id,
                                        uid, ctx.gen))


def _rollback_failover(ctx, sel, carnets):
    """Avant l'activation, tout est réversible : la tâche et son étiquette
    reviennent, la VM source repart comme elle tournait, les instantanés
    finaux de cette exécution disparaissent (les sauvegardes finales
    restent : le volume de secours a pu les appliquer)."""
    ctx.begin_undo()
    left = []
    for r in sel:
        uid, ns, name = r["resource_uid"], r["namespace"], r["name"]
        vols = (carnets.get(uid) or {}).get("volumes") or []
        if ctx.undo_expired():
            left.append(f"{ns}/{name}: backup job, volume labels and source VM start not restored" + _LATE)
            continue
        try:
            _restore_job(ctx, uid, len(vols))
            for v in vols:
                _label_volume(ctx, v["source_volume"], rp.recurring_job_name(ctx.plan_id, uid), False)
            _restart_source(ctx, ns, name)
        except Exception as e:                  # noqa: BLE001
            left.append(f"{ns}/{name}: {str(e)[:200]}")
        ctx.event("failover", "rollback", uid, "source", level="warn" if not left else "error",
                  code="rolled-back", message="failover undone before activation")
    for side, kind, ns, name in reversed(list(ctx.created)):
        if kind == K_LH_SNAPSHOT and not _undo_delete(ctx, ctx.kube(side), kind, ns, name) and ctx.undo_timed_out:
            left.append(f"{kind} {ns}/{name}" + _LATE)
    ctx.created.clear()
    ctx.undo_labels.clear()
    return left


def _activated_volumes(ctx, carnet):
    out = {}
    for v in carnet["volumes"] or []:
        out[v["target_volume"]] = _is_activated(ctx.dst.get(K_LH_VOLUME, NS, v["target_volume"]))
    return out


def _fence_source(ctx, op, r, carnet):
    """Clôture de la source avant de figer les points : VM arrêtée (runStrategy
    d'origine noté), étiquette de tâche retirée, tâche de la VM supprimée.
    Plus aucune sauvegarde ne part après."""
    uid = r["resource_uid"]
    with stage(ctx, op, "stop-source", "source", [uid], f"stopping {r['namespace']}/{r['name']}"):
        _stop_source(ctx, uid, r["namespace"], r["name"])
    with stage(ctx, op, "remove-job-label", "source", [uid]):
        job = rp.recurring_job_name(ctx.plan_id, uid)
        for v in carnet["volumes"]:
            _unlabel_volume(ctx, v["source_volume"], job)
        cur = ctx.src.get(K_LH_JOB, NS, job)
        if cur is not None and _owned(ctx, cur):
            ctx.src.delete(K_LH_JOB, NS, job)


def _late_fence(ctx, op, r, carnet):
    """Source revenue pendant une bascule non planifiée : clôture tentée au
    mieux, sans attendre (runStrategy, étiquette, tâche)."""
    job = rp.recurring_job_name(ctx.plan_id, r["resource_uid"])
    try:
        ctx.src.patch(K_VM, r["namespace"], r["name"], {"spec": {"runStrategy": "Halted"}})
    except Exception:                           # noqa: BLE001
        return "source-not-fenced"
    for v in carnet["volumes"]:
        with contextlib.suppress(Exception):
            _unlabel_volume(ctx, v["source_volume"], job)
    with contextlib.suppress(Exception):
        cur = ctx.src.get(K_LH_JOB, NS, job)
        if cur is not None and _owned(ctx, cur):
            ctx.src.delete(K_LH_JOB, NS, job)
    return "source-fenced"


def _applied_points(ctx, carnet, tb):
    """Point appliqué par disque (moteur du volume de secours), écart entre
    disques et instant à partir duquel les écritures sont perdues."""
    pts = {}
    for v in carnet["volumes"]:
        last = _status(engine_of(ctx.dst, v["target_volume"])).get("lastRestoredBackup") or ""
        b = next((x for x in tb.get(v["source_volume"], []) if _md(x).get("name") == last), None)
        pts[v["source_volume"]] = {"backup": last, "snapshot_at": _snap_time(b)}
    times = [rp._t(p["snapshot_at"]) for p in pts.values() if p["snapshot_at"]]
    skew = int((max(times) - min(times)).total_seconds()) if len(times) > 1 else 0
    return pts, skew, (rp.iso(min(times)) if times else None)


# v1.99.0 : le contrôle à blanc juge le rattrapage d'après les sauvegardes
# que la cible connaît déjà (relue par Longhorn toutes les quelques minutes) ;
# vu au rejeu : « caught-up » alors qu'une sauvegarde plus récente existait
CATCH_UP_NOTE = ("against the backups the target sees now (may lag 1-4 min; "
                 "the real failover resyncs first)")


def _unplanned_dry_findings(ctx, sel, carnets):
    """Ce qu'une bascule non planifiée ferait, lu sur la cible seule, sans
    rien écrire (v1.99.0 : le contrôle à blanc ne disait rien). Par VM :
    points appliqués et écart entre disques (`applied-points`), refus
    attendu sans `--accept-skew` (`skew-exceeded`) ou faute de point
    (`no-point`), moteurs encore en retard sur la dernière sauvegarde connue
    (`catch-up-pending`, sinon `caught-up`). Rien n'est demandé à Longhorn :
    une relecture de la cible n'est faite que par la vraie bascule."""
    tolerance = int((ctx.plan.get("options") or {}).get("skew_tolerance_s", rp.SKEW_TOLERANCE_S))
    tb = backups_by_volume(ctx.dst)
    out = []
    for r in sel:
        uid, key = r["resource_uid"], f"{r['namespace']}/{r['name']}"
        carnet = carnets.get(uid)
        if not carnet or any(_activated_volumes(ctx, carnet).values()):
            continue
        pts, skew, lost = _applied_points(ctx, carnet, tb)
        out.append(rp._finding("applied-points", "ok", vm=key, points=pts, skew_s=skew, lost_after=lost,
                               tolerance_s=tolerance))
        if not all(p["backup"] for p in pts.values()):
            out.append(rp._finding("no-point", "warn", vm=key,
                                   volumes=sorted(v for v, p in pts.items() if not p["backup"])))
        elif skew > tolerance:
            out.append(rp._finding("skew-exceeded", "warn", vm=key, skew_s=skew, tolerance_s=tolerance,
                                   hint="--accept-skew"))
        late = []
        for v in carnet["volumes"]:
            last = _status(ctx.dst.get(K_LH_VOLUME, NS, v["target_volume"])).get("lastBackup") or ""
            applied = _status(engine_of(ctx.dst, v["target_volume"])).get("lastRestoredBackup") or ""
            if not last or applied != last:
                late.append({"volume": v["source_volume"], "applied": applied, "last_backup": last})
        out.append(rp._finding("catch-up-pending", "warn", vm=key, volumes=late, note=CATCH_UP_NOTE) if late
                   else rp._finding("caught-up", "ok", vm=key, note=CATCH_UP_NOTE))
    return out


def wait_caught_up(ctx, op, volumes, timeout_key):
    """Bascule non planifiée : avant l'activation, chaque moteur doit avoir
    appliqué la dernière sauvegarde connue de son volume
    (`engine.status.lastRestoredBackup == volume.status.lastBackup`, la
    condition de Longhorn pour activer un volume de secours)."""
    def caught_up():
        late = []
        for sb in volumes:
            lv = ctx.dst.get(K_LH_VOLUME, NS, sb)
            eng = engine_of(ctx.dst, sb)
            for st in (_status(eng).get("restoreStatus") or {}).values():
                if (st or {}).get("error"):
                    return Fail(f"restore of {sb}: {st['error']}")
            last = _status(lv).get("lastBackup") or ""
            if not last or _status(eng).get("lastRestoredBackup") != last:
                late.append(sb)
        return True if not late else f"{len(late)} volume(s) still restoring their last backup"
    wait_for(ctx, op, "last backups restored", caught_up, ctx.timeouts[timeout_key], every=2)


def _final_applied(ctx, carnet, finals):
    """Relance d'une bascule planifiée : la sauvegarde finale de chaque
    disque (nom rangé dans `runs.points.final`) est-elle déjà appliquée ?
    Alors la source n'est plus nécessaire pour cette VM."""
    for v in carnet["volumes"]:
        name = finals.get(v["source_volume"])
        if not name or _status(engine_of(ctx.dst, v["target_volume"])).get("lastRestoredBackup") != name:
            return False
    return True


def stale_carnet_finding(conn, plan_id, gen, sel, accepted=False):
    """Carnet de reprise d'avant les réglages du plan : marque `carnet-stale`
    (réglages changés sur le plan protégé, protect pas relancé) ou santé
    relevée par le suiveur (`carnet_stale` d'une VM). La bascule démarrerait
    ces VMs avec les réglages du dernier protect : refusé sauf accord
    explicite (`accept_stale_carnet`, alors un avertissement). None sinon."""
    flag = db.get_plan_flag(conn, "carnet-stale", plan_id) or {}
    health = db.health_map(conn, plan_id, gen)
    vms = sorted(f"{r['namespace']}/{r['name']}" for r in sel
                 if (health.get(r["resource_uid"]) or {}).get("carnet_stale"))
    if not flag.get("keys") and not vms:
        return None
    return rp._finding("carnet-stale", "warn" if accepted else "block", keys=list(flag.get("keys") or []),
                       vms=vms, since=flag.get("since"), accepted=bool(accepted))


def failover(ctx, vms=None, planned=True, accept_skew=False, force_unplanned=False, yes=False, dry_run=False,
             record=True, accept_stale_carnet=False):
    """(`protected`, `failing_over`, `error`) -> `failing_over` ->
    `failed_over` (ou `protected` si une partie seulement du plan bascule).

    Planifiée : la source répond ; VM arrêtée, sauvegarde finale de chaque
    disque appliquée avant l'activation (zéro perte). Non planifiée
    (`planned=False`, exige `yes`) : relecture finale bornée, chaque moteur
    à jour de la dernière sauvegarde connue, activation de ce qui est
    appliqué, écart entre disques contrôlé (`accept_skew`). Avec
    `force_unplanned` et une source vivante, la source est clôturée d'abord.
    Avant l'activation, une erreur ou une annulation remet tout en place ;
    après, rien n'est défait et la relance reprend à l'étape suivante, avec
    la cible seule. Une bascule restée ouverte (`runs`) ne se relance que
    sur le même périmètre et le même mode (`open-run` sinon : `resume`).
    `record=False` (à blanc seulement) : rien au journal."""
    uids = []
    if dry_run and not record:
        ctx.quiet = True
    try:
        if not planned and not yes and not dry_run:
            raise Refused("confirmation-required", "an unplanned failover needs --yes")
        _precondition(ctx, "failover")
        before = _plan_guard(ctx.plan)
        sel = _select(ctx, vms)
        uids = [r["resource_uid"] for r in sel]
        if not dry_run:
            _check_open_run(ctx, sel, planned)
        s_state, d_state = probe(ctx, "src"), probe(ctx, "dst")
        if d_state != "ok":
            raise Refused("target-denied" if d_state == "denied" else "target-unreachable",
                          f"the target cluster is {'refusing this reader' if d_state == 'denied' else 'unreachable'}")
        carnets = _carnets(ctx, set(uids))
        missing = [r for r in sel if r["resource_uid"] not in carnets]
        if missing:
            raise Refused("carnet-missing", "no recovery carnet for " +
                          ", ".join(f"{r['namespace']}/{r['name']}" for r in missing))
        # une VM déjà activée : la bascule ne peut que se terminer, avec la
        # cible seule (l'état de la source ne bloque plus rien)
        resuming = any(any(_activated_volumes(ctx, carnets[u]).values()) for u in uids)
        if not resuming:
            if planned and s_state != "ok":
                raise Refused("source-unreachable", "the source does not answer: use --unplanned",
                              facts={"hint": "--unplanned"})
            if not planned and s_state == "ok" and not force_unplanned:
                raise Refused("source-reachable", "the source answers: use a planned failover, or --force-unplanned")
        tns_list = [_md(c["vm"])["namespace"] for c in carnets.values()]
        dst = collect_target(ctx, tns_list)
        if planned and s_state == "ok":
            src = collect_source(ctx)
            src["vms"] = [v for v in src.get("vms") or [] if v["uid"] in carnets]
            phase = "failover"
        else:
            src = _unplanned_src_facts(ctx, carnets)
            phase = "failover-unplanned"
        findings = rp.check(src, dst, dict(ctx.plan, id=ctx.plan_id, phase=phase))
        if not resuming:
            # une VM déjà activée ne peut que finir : le contrôle ne vaut qu'au départ
            stale = stale_carnet_finding(ctx.conn, ctx.plan_id, ctx.gen, sel, accept_stale_carnet)
            if stale:
                findings.append(stale)
        extra, dry = _preflight_targets(ctx, carnets, dst)
        findings = sorted(findings + extra, key=lambda f: rp._ORDER[f["level"]])
        if rp.blocking(findings):
            raise Refused("check", "the pre-check refuses this failover", findings=findings)
        if dry_run:
            if phase == "failover-unplanned":
                findings = sorted(findings + _unplanned_dry_findings(ctx, sel, carnets),
                                  key=lambda f: rp._ORDER[f["level"]])
            # ordre de démarrage et actions annexes qu'aurait la bascule (aucun appel)
            opts = ctx.plan.get("options") or {}
            return _result(ctx, "dry-run", EXIT_OK, "dry run", findings=findings, dry_run=dry,
                           waves=rp.failover_waves(opts, [f"{r['namespace']}/{r['name']}" for r in sel]),
                           hooks=rp.hooks_summary(opts))
        ctx.safe_point()
        _lock_checked(ctx, "failover", before, lambda: _sel_key(_select(ctx, vms)), _sel_key(sel))
    except Refused as e:
        return _refusal(ctx, "failover", e, uids)
    except Suspended as e:
        return _not_started(ctx, "failover", e)
    except Cancelled as e:
        _unlock(ctx)
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e))
    except Exception as e:                      # noqa: BLE001
        return _preflight_failed(ctx, "failover", e, uids)

    # réglages relus sous le verrou
    opts = ctx.plan.get("options") or {}
    tolerance = int(opts.get("skew_tolerance_s", rp.SKEW_TOLERANCE_S))
    op = "failover"
    mode = "planned" if planned else "unplanned"
    # --force-unplanned avec une source vivante : clôture d'abord
    fence_live = not planned and s_state == "ok"
    started = ctx.dt()
    refs = [f"{r['namespace']}/{r['name']}" for r in sel]
    prev = _open_run(ctx, "failover")
    run_id = prev["id"] if prev else db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen, vms=refs,
                                                    points={"mode": mode, "force_unplanned": bool(force_unplanned),
                                                            "accept_stale_carnet": bool(accept_stale_carnet)},
                                                    by=ctx.by, action_id=ctx.action_id)
    points = dict((prev or {}).get("points") or {}, mode=mode, force_unplanned=bool(force_unplanned),
                  accept_stale_carnet=bool(accept_stale_carnet) or bool(((prev or {}).get("points") or {})
                                                                       .get("accept_stale_carnet")))
    paused_before = bool(opts.get("paused"))
    if prev and "paused_before" in points:
        paused_before = points["paused_before"]
    points["paused_before"] = paused_before
    activated_any = False
    source_touched = False
    result = {"fenced": {}, "skew_s": {}, "refused_skew": [], "lost_after": {}, "point_at": {}}
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
            finals = points.get("final") or {}
            ready = [r for r in pre if _final_applied(ctx, carnets[r["resource_uid"]], finals)]
            todo = [r for r in pre if r not in ready]
            for r in todo:
                source_touched = True
                _fence_source(ctx, op, r, carnets[r["resource_uid"]])
            items = [(r["resource_uid"], v["source_volume"]) for r in todo
                     for v in carnets[r["resource_uid"]]["volumes"]]
            names = {}
            if items:
                with stage(ctx, op, "final-backup", "source", [r["resource_uid"] for r in todo]):
                    names = take_backups(ctx, "final-backup", items, "final", token=str(run_id))
                    backups = wait_backups(ctx, "final-backup", names, "final_backup")
                for uid, vol in items:
                    db.record_tick(ctx.conn, ctx.plan_id, _tick_from_backup(backups[vol], uid, ctx.gen))
                points["final"] = dict(finals, **names)
                db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen, points=points, vms=refs,
                              run_id=run_id, by=ctx.by, action_id=ctx.action_id)
            for r in pre:
                for v in carnets[r["resource_uid"]]["volumes"]:
                    wanted[v["target_volume"]] = (v["source_volume"], points["final"][v["source_volume"]])
            if wanted:
                with stage(ctx, op, "final-resync", "target", [r["resource_uid"] for r in pre]):
                    wait_applied(ctx, "final-resync", wanted, "resync_apply")
                for sb, (vol, name) in wanted.items():
                    db.mark_applied(ctx.conn, ctx.plan_id, vol, name, ctx.dt(), ctx.gen, applied_by="failover")
        else:
            if fence_live:
                for r in pre:
                    source_touched = True
                    _fence_source(ctx, op, r, carnets[r["resource_uid"]])
                    result["fenced"][f"{r['namespace']}/{r['name']}"] = "source-fenced"
                    ctx.event(op, "fence", r["resource_uid"], "source", code="source-fenced")
            vols = {v["target_volume"]: v["source_volume"] for r in pre for v in carnets[r["resource_uid"]]["volumes"]}
            if vols:
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
                with stage(ctx, op, "catch-up", "target", [r["resource_uid"] for r in pre]):
                    wait_caught_up(ctx, "catch-up", sorted(vols), "unplanned_catchup")
        # points appliqués et écart entre disques, par VM (décision de basculer)
        tb = backups_by_volume(ctx.dst)
        go = []
        for r in sel:
            uid, key = r["resource_uid"], f"{r['namespace']}/{r['name']}"
            stored = (points.get("applied") or {}).get(uid)
            if uid in done_vms and stored:
                # reprise après l'activation : le moteur n'a plus de point
                # (voir plus bas), la lecture rangée avant l'activation fait foi
                pts = stored
                times = [rp._t(p["snapshot_at"]) for p in pts.values() if p.get("snapshot_at")]
                skew = int((max(times) - min(times)).total_seconds()) if len(times) > 1 else 0
                lost = rp.iso(min(times)) if times else None
            else:
                pts, skew, lost = _applied_points(ctx, carnets[uid], tb)
            result["skew_s"][key] = skew
            if lost:
                result["point_at"][key] = lost
                # planifiée : la source était arrêtée et son dernier point
                # appliqué, rien n'est perdu ; seule la non planifiée perd ce
                # qui suit le plus vieux point de ses disques
                if not planned:
                    result["lost_after"][key] = lost
            points.setdefault("applied", {})[uid] = pts
            if not planned and uid not in done_vms:
                if not all(p["backup"] for p in pts.values()):
                    result["refused_skew"].append(key)
                    ctx.event(op, "points", uid, "target", level="error", code="no-point",
                              message="a disk has no applied point", facts=pts)
                    continue
                if skew > tolerance and not accept_skew:
                    result["refused_skew"].append(key)
                    ctx.event(op, "points", uid, "target", level="warn", code="skew-exceeded",
                              message=f"disks {skew} s apart (tolerance {tolerance} s): --accept-skew",
                              facts={"skew_s": skew, "points": pts})
                    continue
            ctx.event(op, "points", uid, "target", code="points", facts={"skew_s": skew, "points": pts})
            go.append(r)
        if not go:
            raise Refused("skew-exceeded", "no VM can fail over without --accept-skew", facts=result)
        if not planned:
            # source revenue pendant la bascule (ou à sa reprise) : clôture
            # tentée au mieux des VMs pas encore clôturées
            came_back = probe(ctx, "src") == "ok"
            for r in go:
                key = f"{r['namespace']}/{r['name']}"
                if key in result["fenced"]:
                    continue
                result["fenced"][key] = _late_fence(ctx, op, r, carnets[r["resource_uid"]]) if came_back \
                    else "source-not-fenced"
                if came_back:
                    ctx.event(op, "fence", r["resource_uid"], "source", code=result["fenced"][key])
        # points lus avant l'activation, rangés avant elle : un arrêt brutal
        # pendant l'activation ne les perd pas (le moteur activé n'en a plus)
        db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen, points=points, vms=refs, run_id=run_id,
                      steps=ctx.steps, by=ctx.by, action_id=ctx.action_id)
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
        # points rangés : ceux lus AVANT l'activation. Après elle, Longhorn
        # 1.12.1 vide `lastRestoredBackup` du moteur et `status.lastBackup` du
        # volume (vu sur le banc, v1.99.0) : une relecture ne rend rien. Si
        # Longhorn finit une restauration en cours pendant l'activation, le
        # point réel est plus récent que celui rangé : `lost_after` reste
        # prudent (plus tôt que la réalité), jamais optimiste.
        src_back = probe(ctx, "src") == "ok"
        # démarrage par vagues (v1.99.0, écrans) : les VMs d'une vague, puis
        # sa condition de passage, puis ses actions annexes ; une VM listée
        # nulle part part dans la dernière vague, implicite. Les actions
        # `start` passent après l'activation, avant la première vague ; leur
        # échec avertit, rien n'est défait (l'activation est définitive)
        by_ref = {f"{r['namespace']}/{r['name']}": r for r in go}
        # périmètre de la bascule : à la reprise, les VMs déjà basculées par
        # elle comptent (les actions d'une vague interrompue restent dues)
        scope = list(dict.fromkeys(list(points.get("scope") or []) + list((prev or {}).get("vms") or [])
                                   + list(by_ref)))
        points["scope"] = scope
        hook_log = []
        run_hooks(ctx, op, "start", points, run_id, started, refs, hook_log)
        for wave in rp.failover_waves(opts, scope):
            if not wave["vms"]:
                continue                        # aucune VM de cette bascule : sautée, actions comprises
            if not any(ref in by_ref for ref in wave["vms"]):
                # reprise : vague déjà démarrée, seules ses actions non appelées restent
                run_hooks(ctx, op, f"wave:{wave['index']}", points, run_id, started, refs, hook_log)
                continue
            wave = dict(wave, vms=[ref for ref in wave["vms"] if ref in by_ref])
            ctx.step = "wave"
            facts = {"index": wave["index"], "name": wave["name"], "vms": wave["vms"], "gate": wave["gate"]}
            ctx.emit("wave", "running", f"wave {wave['index']}: {len(wave['vms'])} VM(s)")
            ctx.event(op, "wave", side="target", code="wave-start",
                      message=f"wave {wave['index']} {wave['name']}".rstrip(), facts=facts)
            for ref in wave["vms"]:
                r = by_ref[ref]
                uid, key = r["resource_uid"], ref
                _bring_up(ctx, op, r, carnets[uid], opts)
                if src_back:
                    tvm = _md(carnets[uid]["vm"])
                    try:
                        ctx.src.patch(K_VM, r["namespace"], r["name"], {"metadata": {"annotations": {
                            FAILED_OVER_TO: f"{ctx.plan['target_cluster']}/{tvm['namespace']}/{tvm['name']}",
                            STOPPED_ANNOTATION: None}}})
                    except Exception as e:          # noqa: BLE001
                        ctx.event(op, "annotate-source", uid, "source", level="warn", code="annotation-failed",
                                  message=str(e)[:300])
                _set_resources(ctx, [uid], "failed_over")
                ctx.event(op, "done", uid, "target", code="failed-over", message=f"{key} failed over ({mode})",
                          facts={"lost_after": result["lost_after"].get(key),
                                 "point_at": result["point_at"].get(key), "skew_s": result["skew_s"].get(key),
                                 "points": points["applied"].get(uid), "mode": mode})
            passed = wave_gate(ctx, op, wave, [carnets[by_ref[ref]["resource_uid"]] for ref in wave["vms"]])
            ctx.event(op, "wave", side="target", code="wave-passed" if passed else "wave-gate-timeout",
                      level="info" if passed else "warn",
                      message=f"wave {wave['index']} " + ("passed its gate" if passed else
                                                          "did not pass its gate in time: going on"),
                      facts=facts)
            ctx.emit("wave", "done", f"wave {wave['index']} " + ("passed" if passed else "gate timed out"))
            run_hooks(ctx, op, f"wave:{wave['index']}", points, run_id, started, refs, hook_log)
        run_hooks(ctx, op, "end", points, run_id, started, refs, hook_log)
        if hook_log:
            result["hook_results"] = hook_log
        back = [r["resource_uid"] for r in sel if r not in go]
        _set_resources(ctx, back, "protected", "not failed over")
        rest = [x for x in db.list_resources(ctx.conn, ctx.plan_id, ctx.gen) if x["state"] != "failed_over"]
        _set_state(ctx, "protected" if rest else "failed_over")
        _restore_paused(ctx, op, paused_before)
        db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen, finished_at=ctx.dt(), result="ok",
                      vms=[f"{r['namespace']}/{r['name']}" for r in go], points=points, steps=ctx.steps,
                      by=ctx.by, action_id=ctx.action_id, run_id=run_id)
        _unlock(ctx)
        return _result(ctx, "failed-over", EXIT_OK, f"{len(go)} VM(s) failed over", run_id=run_id, mode=mode,
                       **result)
    except (Cancelled, Suspended, Exception) as e:          # noqa: BLE001
        # Arrêt de la console (décision 68) pendant une bascule : traité comme
        # une annulation. Avant l'activation, une bascule est tout ou rien par
        # conception (même un échec remet tout en place) : la laisser à moitié
        # faite pendant que la console est arrêtée garderait la VM source
        # arrêtée, sans VM cible, et ferait d'une bascule planifiée une panne.
        # Après l'activation, rien n'est défait (déjà le cas) : `resume` la
        # termine. La console attend donc le retour arrière d'une bascule
        # (`kill_grace`), et seulement d'elle.
        cancelled = isinstance(e, (Cancelled, Suspended))
        refused = isinstance(e, Refused)
        if not activated_any:
            # planifiée : la source a pu être arrêtée par une exécution précédente
            left = _rollback_failover(ctx, sel, carnets) if planned or source_touched else []
            _set_resources(ctx, uids, "protected", "failover undone")
            _set_state(ctx, "protected", f"failover undone: {e}"[:500])
            _restore_paused(ctx, op, paused_before)
            db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen, finished_at=ctx.dt(),
                          result="undo-timeout" if ctx.undo_timed_out else "rolled-back", vms=refs,
                          points=points, steps=ctx.steps, by=ctx.by, action_id=ctx.action_id, run_id=run_id)
            _unlock(ctx)
            if ctx.undo_timed_out:
                return _undo_timeout(ctx, op, f"{e} (failover undone in part)", left)
            if left:
                ctx.event(op, "rollback", level="error", code="left", message="; ".join(left))
            if refused:
                return _result(ctx, e.code, EXIT_BLOCKED, str(e), findings=e.findings, **result)
            return _result(ctx, "cancelled" if cancelled else error_code(e),
                           EXIT_CANCELLED if cancelled else EXIT_FAIL, f"{e} (failover undone)", left=left, **result)
        # après l'activation : pas de retour arrière, reprise par relance
        msg = f"failover stopped after activation ({e}): run failover again to finish"
        db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen, result="interrupted", vms=refs,
                      points=points, steps=ctx.steps, by=ctx.by, action_id=ctx.action_id, run_id=run_id)
        # une VM déjà basculée par cette bascule le reste (la reprise ne
        # reprend que les autres) ; avant, elle passait en `error`, d'où
        # aucune sortie ne menait à la reprise
        _error(ctx, [u for u in uids
                     if (db.get_resource(ctx.conn, ctx.plan_id, u, ctx.gen) or {}).get("state") != "failed_over"],
               msg)
        return _result(ctx, "interrupted", EXIT_CANCELLED if cancelled else EXIT_FAIL, msg, run_id=run_id, **result)


def _restore_paused(ctx, op, paused_before):
    """Rend au suiveur l'état d'avant la bascule, seulement si personne n'a
    changé `paused` depuis que la bascule l'a posé (à True) : un changement
    venu d'ailleurs pendant la bascule est gardé."""
    cur = (ctx.reload().get("options") or {}).get("paused")
    if cur is True:
        db.update_plan(ctx.conn, ctx.plan_id, now=ctx.dt(), options={"paused": paused_before})
        ctx.reload()
    elif cur != paused_before:
        ctx.event(op, "pause-follower", level="warn", code="paused-kept",
                  message=f"paused was changed to {cur} during the failover: kept", facts={"paused": cur})


def _open_scope(ctx, run_):
    """VMs d'une bascule ouverte qui restent à basculer."""
    done = {f"{r['namespace']}/{r['name']}" for r in db.list_resources(ctx.conn, ctx.plan_id, ctx.gen)
            if r["state"] == "failed_over"}
    return sorted(v for v in run_.get("vms") or [] if v not in done)


def _check_open_run(ctx, sel, planned):
    prev = _open_run(ctx, "failover")
    have = _open_scope(ctx, prev) if prev else []
    if not have:
        return
    want = sorted(f"{r['namespace']}/{r['name']}" for r in sel)
    pmode = (prev.get("points") or {}).get("mode", "planned")
    if want != have or pmode != ("planned" if planned else "unplanned"):
        raise Refused("open-run", f"a {pmode} failover of {', '.join(have)} is not finished: "
                      "run resume to finish it", facts={"vms": have, "mode": pmode, "hint": "resume"})


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
            _ensure(ctx, "dst", _pv(ctx, uid, tns, v), "target-pv")
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
            # pas de dry-run ici : la création passe par les mêmes contrôles
            # d'admission ; celui du préalable a lieu avant toute écriture
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

# ---------------------------------------------------------------------------
# Vagues et actions annexes de la bascule
# ---------------------------------------------------------------------------

def _agent_connected(vmi):
    """Condition `AgentConnected` de la VMI (KubeVirt, posée quand l'agent
    invité qemu-guest-agent répond)."""
    for c in _status(vmi).get("conditions") or []:
        if c.get("type") == "AgentConnected":
            return str(c.get("status")) == "True"
    return False


def wave_gate(ctx, op, wave, carnets):
    """Condition de passage d'une vague, une fois ses VMs démarrées (le
    démarrage attend déjà `Running`, `_bring_up`) : `running` passe tout de
    suite, `agent` attend l'agent invité de chaque VM (au plus `seconds`),
    `delay` attend `seconds`. Une VM laissée arrêtée (`start: stopped`)
    n'attend rien. Rend False quand `agent` n'est pas venu à temps (la
    bascule continue : avertissement). Attentes au travers de `wait_for`,
    donc annulables et interrompues par l'arrêt de la console."""
    gate = wave.get("gate") or {}
    kind, secs = gate.get("kind") or "running", int(gate.get("seconds") or 0)
    ctx.step = "wave"                   # étape rangée si la console s'arrête ici
    started = [_md(c["vm"]) for c in carnets if (_spec(c["vm"]).get("runStrategy") or "Halted") != "Halted"]
    if kind == "running" or not started:
        return True
    if kind == "delay":
        until = ctx.now() + secs
        wait_for(ctx, "wave", f"wave {wave['index']} delay", lambda: True if ctx.now() >= until else "waiting",
                 secs + 1, every=min(5, max(1, secs)))
        return True

    def agents():
        late = [f"{m['namespace']}/{m['name']}" for m in started
                if not _agent_connected(ctx.dst.get(K_VMI, m["namespace"], m["name"]))]
        return True if not late else f"guest agent not connected yet on {len(late)} VM(s)"
    try:
        wait_for(ctx, "wave", f"wave {wave['index']} guest agents", agents, secs, every=2)
    except TransferError:
        return False
    return True


def call_hook(hook, allow=None, clock=time.monotonic, resolver=None, connector=None):
    """Appelle une action annexe HTTP par la garde anti-SSRF
    (`replication_http`) : adresse interne refusée sauf
    `replication.hooks.allow_private`, aucune redirection suivie (un en-tête
    secret ne part jamais ailleurs). Rend {"ok", "status", "seconds",
    "error"} ; jamais d'exception pour un échec de l'appel (refus, délai,
    statut >= 400). Les valeurs des en-têtes secrets sont retirées du
    message d'erreur ; le corps de la réponse n'est ni gardé ni montré."""
    import replication_http as rh
    import url_fetch_guard as ufg
    allow = rh.allow_for("hooks") if allow is None else allow
    headers = dict(hook.get("headers") or {})
    headers.update(hook.get("secret_headers") or {})
    body = hook.get("body") or ""
    method = hook.get("method") or "POST"
    data = body.encode() if body and method != "GET" else None
    kw = {"resolver": resolver}
    if connector is not None:
        kw["connector"] = connector
    t0 = clock()
    status, error = None, ""
    try:
        status = rh.request(method, hook["url"], headers=headers, body=data,
                            timeout=int(hook.get("timeout_s") or rp.HOOK_TIMEOUT_DEFAULT), allow=allow, **kw)
        if status >= 400:
            error = f"HTTP {status}"
    except ufg.FetchRefused as e:
        error = rh.refusal_text(e, "hooks")
    except Exception as e:                      # noqa: BLE001 : jamais d'exception vers la bascule
        error = type(e).__name__
    secs = round(clock() - t0, 3)
    for v in rp.hook_secret_values([hook]):
        error = error.replace(v, rp.MASK)
    ok = status is not None and 200 <= status < 300 and not error
    return {"ok": ok, "status": status, "seconds": secs, "error": error[:300]}


def hook_event(ctx, op, hook, res):
    """Le résultat d'un appel au journal : statut et durée, jamais d'en-tête."""
    ctx.event(op, "hook", side="target", level="info" if res["ok"] else "warn",
              code="hook-ok" if res["ok"] else "hook-failed",
              message=f"hook {hook['name']} ({hook['at']}): " + (f"HTTP {res['status']}" if res["ok"] else
                                                                (res["error"] or f"HTTP {res['status']}")),
              facts={"name": hook["name"], "at": hook["at"], "status": res["status"], "seconds": res["seconds"]})


def run_hooks(ctx, op, point, points, run_id, started, refs, log):
    """Actions annexes d'un point de la bascule. Une action déjà appelée par
    cette bascule (relance après une interruption) n'est pas rappelée :
    `runs.points.hooks_done`. Un échec avertit et la bascule continue."""
    hooks = rp.hooks_at(ctx.plan.get("options") or {}, point)
    done = points.setdefault("hooks_done", [])
    for h in hooks:
        if h["name"] in done:
            continue
        ctx.safe_point()
        ctx.step = "hook"
        ctx.emit("hook", "running", f"hook {h['name']} ({point})")
        res = call_hook(h)
        hook_event(ctx, op, h, res)
        ctx.emit("hook", "done" if res["ok"] else "error", f"hook {h['name']}: "
                 + (f"HTTP {res['status']}" if res["ok"] else res["error"] or f"HTTP {res['status']}"))
        log.append({"name": h["name"], "at": point, "ok": res["ok"], "status": res["status"],
                    "seconds": res["seconds"]})
        done.append(h["name"])
        db.record_run(ctx.conn, ctx.plan_id, "failover", started, ctx.gen, points=points, vms=refs, run_id=run_id,
                      steps=ctx.steps, by=ctx.by, action_id=ctx.action_id)


def hook_test(conn, plan, name, by="", action_id="", now=None, allow=None):
    """`hook test` : appelle une action annexe du plan tout de suite, résultat
    au journal (opération `plan`, étape `hook`). Rend (résultat, action) ;
    KeyError si l'action n'existe pas."""
    hook = next((h for h in (plan.get("options") or {}).get("hooks") or [] if h.get("name") == name), None)
    if hook is None:
        raise KeyError(name)
    res = call_hook(hook, allow=allow)
    db.record_event(conn, "plan", step="hook", plan_id=plan["id"], host=plan["target_cluster"],
                    side="target", level="info" if res["ok"] else "warn",
                    code="hook-ok" if res["ok"] else "hook-failed",
                    message=f"hook {name} tested: " + (f"HTTP {res['status']}" if res["ok"] else
                                                     (res["error"] or f"HTTP {res['status']}")),
                    facts={"name": name, "at": hook["at"], "status": res["status"], "seconds": res["seconds"],
                           "test": True}, by=by, action_id=action_id, now=now)
    return res, hook


def _held_vms(ctx, prev, dst_ok):
    """VMs dont la bascule a déjà touché la cible (décision 47) : un volume
    activé (cible relue, ou ligne `activated`), ou une ressource en bascule,
    basculée, ou en erreur pendant une bascule. Leur contenu cible est gardé
    en entier. Rangé dans la ligne `runs` de la déprotection : une relance
    retrouve la liste même après le passage des ressources à
    `unprotecting`. Rend {uid: raison}."""
    held = dict(((prev or {}).get("points") or {}).get("held") or {})
    # même règle que les actions proposées (`replication.held_vms`)
    for uid, why in rp.held_vms(db.list_resources(ctx.conn, ctx.plan_id, ctx.gen),
                                db.list_volumes(ctx.conn, ctx.plan_id, ctx.gen)).items():
        held.setdefault(uid, why)
    if dst_ok:
        for lv in ctx.dst.list(K_LH_VOLUME, NS, selector=f"{rp.LABEL_PLAN}={ctx.plan_id}"):
            uid = _labels(lv).get(rp.LABEL_VM)
            if uid and _owned(ctx, lv) and _is_activated(lv):
                held.setdefault(uid, "activated")
    return held


def unprotect(ctx, delete_backups=False, yes=False):
    """Tout état sauf `draft`, `failing_over`, `unprotected` ->
    `unprotecting` -> `unprotected`. Cible : volumes de secours, carnets et
    backing images restaurées par le plan, des VMs jamais activées. Une VM
    dont la bascule a déjà activé un volume garde TOUT sur la cible
    (décision 47 : abandonner la bascule ne supprime rien), listé dans
    `left` ; tant qu'il en reste une, `delete_backups` est refusé. Source :
    étiquettes de tâche, tâches. `delete_backups` (exige `yes`) : les
    `Backup` portant les étiquettes du plan, jamais un `BackupVolume`. Côté
    injoignable : noté « à faire », terminé à la relance. PV, PVC et VM
    cibles ne sont jamais supprimés."""
    try:
        if delete_backups and not yes:
            raise Refused("confirmation-required", "deleting backups needs --yes")
        _precondition(ctx, "unprotect")
        before = _plan_guard(ctx.plan)
        prev = _open_run(ctx, "unprotect")
        if ((prev or {}).get("points") or {}).get("delete_backups") and not delete_backups:
            # la déprotection interrompue supprimait aussi les sauvegardes :
            # une relance sans --delete-backups ne l'élargit pas en silence
            # (la console réserve ce geste à l'administrateur)
            raise Refused("needs-admin-resume", "the interrupted unprotect also deletes the plan's backups: "
                          "run unprotect again with --delete-backups --yes, or resume the plan",
                          facts={"delete_backups": True})
        delete_backups = bool(delete_backups)
        held = _held_vms(ctx, prev, probe(ctx, "dst") == "ok")
        if delete_backups and held:
            names = {r["resource_uid"]: f"{r['namespace']}/{r['name']}" for r in db.list_resources(ctx.conn, ctx.plan_id)}
            vms = sorted(names.get(u, u) for u in held)
            raise Refused("backups-in-use", "a failover already activated volumes of " + ", ".join(vms) +
                          ": the backups are kept; unprotect without --delete-backups",
                          facts={"vms": vms})
        ctx.safe_point()
        _lock_checked(ctx, "unprotect", before)
    except Refused as e:
        return _refusal(ctx, "unprotect", e)
    except Suspended as e:
        return _not_started(ctx, "unprotect", e)
    except Cancelled as e:
        _unlock(ctx)
        return _result(ctx, "cancelled", EXIT_CANCELLED, str(e))
    except Exception as e:                      # noqa: BLE001
        return _preflight_failed(ctx, "unprotect", e)
    op = "unprotect"
    started = ctx.dt()
    resources = db.list_resources(ctx.conn, ctx.plan_id)
    uids = sorted({r["resource_uid"] for r in resources if int(r["generation"]) == ctx.gen})
    base = {"delete_backups": delete_backups, "held": held}
    run_id = prev["id"] if prev else db.record_run(ctx.conn, ctx.plan_id, "unprotect", started, ctx.gen,
                                                    points=base, by=ctx.by, action_id=ctx.action_id)
    pending, left, selectors = [], [], {}
    try:
        # une protection ou une mesure interrompue n'est plus à reprendre
        _close_superseded(ctx, SUSPENDABLE_RUNS)
        _set_state(ctx, "unprotecting")
        _set_resources(ctx, uids, "unprotecting")
        if not _touched_target(ctx):
            pass
        elif probe(ctx, "dst") == "ok":
            left, selectors = _unprotect_target(ctx, op, uids, held)
        else:
            pending.append("target")
        if probe(ctx, "src") == "ok":
            _remove_jobs(ctx, op, resources)
        else:
            pending.append("source")
        deleted = 0
        if delete_backups:
            for side, name in (("src", "source"), ("dst", "target")):
                if probe(ctx, side) != "ok":
                    continue
                with stage(ctx, op, "delete-backups", name, uids):
                    # les étiquettes du plan ne sont pas dans `metadata.labels`
                    # d'une sauvegarde de tâche récurrente : pas de sélecteur
                    for b in ctx.kube(side).list(K_LH_BACKUP, NS, selector=f"backup-target={BACKUP_TARGET}"):
                        if rp.backup_owned_by(b, ctx.plan_id):
                            if run.delete_quiet(ctx.kube(side), K_LH_BACKUP, NS, _md(b)["name"]):
                                deleted += 1
        if pending:
            for u in uids:
                ctx.event(op, "pending", u, level="warn", code="pending",
                          message="unreachable: " + ", ".join(pending) + "; run unprotect again")
            db.record_run(ctx.conn, ctx.plan_id, "unprotect", started, ctx.gen, result="pending",
                          points=dict(base, pending=pending), steps=ctx.steps,
                          by=ctx.by, action_id=ctx.action_id, run_id=run_id)
            _unlock(ctx)
            return _result(ctx, "pending", EXIT_FAIL, "to finish: " + ", ".join(pending), pending=pending, left=left,
                           left_selectors=selectors)
        _set_resources(ctx, uids, "unprotected")
        _set_state(ctx, "unprotected")
        db.record_run(ctx.conn, ctx.plan_id, "unprotect", started, ctx.gen, finished_at=ctx.dt(), result="ok",
                      points=dict(base, backups_deleted=deleted, left=left), steps=ctx.steps,
                      by=ctx.by, action_id=ctx.action_id, run_id=run_id)
        _unlock(ctx)
        return _result(ctx, "unprotected", EXIT_OK, "plan unprotected", left=left, left_selectors=selectors,
                       backups_deleted=deleted)
    except Suspended as e:
        # arrêt de la console : comme une annulation, rien n'est défait ; la
        # ligne `runs` (déjà ouverte) garde l'étape atteinte
        return _suspended(ctx, op, e, uids, run_op="unprotect", started=started, run_id=run_id,
                          points=dict(base, pending=pending))
    except Cancelled as e:
        # rien n'est défait : la relance (ou `resume`) termine la déprotection
        _unlock(ctx)
        return _result(ctx, "interrupted", EXIT_CANCELLED, f"{e}: run unprotect again to finish")
    except Exception as e:                      # noqa: BLE001
        _error(ctx, uids, f"unprotect failed: {e}")
        return _result(ctx, error_code(e), EXIT_FAIL, str(e))


def _touched_target(ctx):
    """La réplication a-t-elle déjà écrit sur la cible ? (un plan resté en
    mesure n'y a rien posé : sa déprotection n'attend pas la cible)"""
    row = ctx.conn.execute("SELECT 1 FROM events WHERE plan_id = ? AND side = 'target' LIMIT 1",
                           (ctx.plan_id,)).fetchone()
    return row is not None


def _unprotect_target(ctx, op, uids, held):
    """Rend (left, sélecteurs) : ce qui reste sur la cible, une ligne par
    objet, et pour chaque genre le sélecteur d'étiquette qui le retrouve."""
    left, kinds = [], set()
    dst = ctx.dst
    sel = f"{rp.LABEL_PLAN}={ctx.plan_id}"

    def keep(kind, label, o):
        ns = _md(o).get("namespace")
        left.append(f"{label} {ns + '/' if ns else ''}{_md(o)['name']}")
        kinds.add(kind)
    with stage(ctx, op, "remove-target", "target", uids):
        vms = {(_md(o).get("namespace"), _md(o)["name"]) for o in dst.list(K_VM, None, selector=sel) if _owned(ctx, o)}
        for s in dst.list(K_SECRET, rp.CARNET_NAMESPACE, selector=sel):
            if not _owned(ctx, s):
                continue
            meta = read_carnet(s).get("meta") or {}
            if _labels(s).get(rp.LABEL_VM) in held and \
                    (meta.get("target_namespace"), meta.get("target_name")) not in vms:
                # bascule abandonnée avant la VM : le carnet est la seule recette pour la finir
                keep(K_SECRET, "carnet", s)
                continue
            dst.delete(K_SECRET, rp.CARNET_NAMESPACE, _md(s)["name"])
        gone = []
        for lv in dst.list(K_LH_VOLUME, NS, selector=sel):
            if not _owned(ctx, lv):
                continue
            name = _md(lv)["name"]
            if _is_activated(lv):
                keep(K_LH_VOLUME, "volume", lv)
                left[-1] += " (activated)"
                continue
            if _labels(lv).get(rp.LABEL_VM) in held:
                keep(K_LH_VOLUME, "volume", lv)
                left[-1] += " (standby of a failed-over VM)"
                continue
            dst.delete(K_LH_VOLUME, NS, name)
            gone.append(name)
        for kind, label in ((K_PV, "pv"), (K_PVC, "pvc"), (K_VM, "vm"), (K_NS, "namespace")):
            for o in dst.list(kind, None, selector=sel):
                if _owned(ctx, o):
                    keep(kind, label, o)
        # secrets cloud-init recopiés par la bascule (le carnet est à part)
        for o in dst.list(K_SECRET, None, selector=sel):
            if _md(o).get("namespace") != rp.CARNET_NAMESPACE and _owned(ctx, o):
                keep(K_SECRET, "secret", o)
        if gone:
            wait_for(ctx, op, "standby volumes deleted",
                     lambda: True if not any(dst.get(K_LH_VOLUME, NS, n) for n in gone) else "deleting",
                     ctx.timeouts["delete"], every=2)
        used = {_spec(v).get("backingImage") for v in dst.list(K_LH_VOLUME, NS)}
        for bi in dst.list(K_LH_BI, NS, selector=sel):
            if not _owned(ctx, bi):
                continue
            if _md(bi)["name"] in used:
                keep(K_LH_BI, "backingimage", bi)
                left[-1] += " (in use)"
                continue
            dst.delete(K_LH_BI, NS, _md(bi)["name"])
        for v in db.list_volumes(ctx.conn, ctx.plan_id):
            if v["target_volume"] in gone or (v["state"] in ("pending", "restoring", "standby")
                                              and v["vm_uid"] not in held
                                              and dst.get(K_LH_VOLUME, NS, v["target_volume"]) is None):
                db.upsert_volume(ctx.conn, ctx.plan_id, {"generation": int(v["generation"]), "vm_uid": v["vm_uid"],
                                                         "disk": v["disk"], "state": "removed"})
    selectors = {k: sel for k in sorted(kinds)}
    if left:
        ctx.event(op, "left", side="target", level="warn", code="left",
                  message="kept on the target: " + "; ".join(left)[:400], facts={"left": left, "selectors": selectors})
    return left, selectors


# ---------------------------------------------------------------------------
# Reprise
# ---------------------------------------------------------------------------

def resume(ctx, yes=False):
    """Reprend la commande interrompue d'après l'état rangé (après un arrêt
    brutal, le verrou abandonné est repris par `acquire_lock`). Une bascule
    reprend avec son mode, ses VMs et `force_unplanned` rangés dans sa ligne
    `runs` : la confirmation donnée au départ vaut pour sa reprise."""
    plan = ctx.reload()
    state = plan["state"]
    origin = db.error_origin(ctx.conn, ctx.plan_id) if state == "error" else None
    eff = origin or state
    # une commande arrêtée par l'arrêt de la console (décision 68) : sa ligne
    # `runs` ouverte dit laquelle, quel que soit l'état où elle a laissé le
    # plan (un arrêt avant son premier changement d'état le laisse en
    # `draft`) ; la plus récente l'emporte
    opened = [r for r in db.list_runs(ctx.conn, ctx.plan_id)
              if r["operation"] in SUSPENDABLE_RUNS and not r["finished_at"] and r.get("result") == "interrupted"
              and int(r["generation"]) == ctx.gen]
    last = opened[-1] if opened else None
    if last is not None and last["operation"] == "measure-stop" and state == "measuring":
        return measure_stop(ctx)
    if last is not None and last["operation"] == "measure-start":
        return measure_start(ctx, (last.get("points") or {}).get("interval_s") or _measure_interval(ctx.conn, plan))
    if eff == "protecting" or (last is not None and last["operation"] == "protect"):
        return protect(ctx)
    if eff == "failing_over":
        r = _open_run(ctx, "failover") or {}
        pts = r.get("points") or {}
        planned = pts.get("mode", "planned") == "planned"
        return failover(ctx, vms=(_open_scope(ctx, r) if r else None) or None, planned=planned,
                        force_unplanned=bool(pts.get("force_unplanned")), yes=yes or planned or bool(r),
                        accept_stale_carnet=bool(pts.get("accept_stale_carnet")))
    if eff == "unprotecting":
        r = _open_run(ctx, "unprotect") or {}
        return unprotect(ctx, delete_backups=bool((r.get("points") or {}).get("delete_backups")), yes=True)
    if eff == "measuring" and state == "error":
        return measure_start(ctx, _measure_interval(ctx.conn, plan))
    return _result(ctx, "nothing-to-resume", EXIT_OK, f"nothing to resume from {state}")
