"""harvester-ops : déroulé d'un transfert de VM entre clusters (v1.45.0).

Les deux moteurs de la spec `docs/design/2026-09-24-migration-vm.md` :

* **sauvegarde** : `VirtualMachineBackup` vers la cible que les deux
  clusters partagent, attente de sa synchronisation sur la cible,
  `VirtualMachineRestore` en nouvelle VM, correspondances appliquées ;
* **fichier** : chaque disque exporté en image temporaire sur la source,
  lu en flux (`kubectl get --raw`), écrit dans une archive ou servi
  directement à un `DataVolume` CDI de la cible.

Les clusters ne sont vus qu'au travers d'un client à l'interface ci-dessous,
ce qui rend tout le déroulé testable sans cluster (horloge et sommeil
injectés) :

    get(kind, ns, name) -> dict|None     list(kind, ns=None, selector=None) -> list
    create(obj) -> dict                  replace(obj) -> dict
    patch(kind, ns, name, patch)         delete(kind, ns, name, cascade=None)
    raw_stream(path) -> itérable d'octets

Tout ce qu'un transfert crée porte l'étiquette `harvester-ops.io/transfer`
et s'inscrit dans `ctx.created` : un échec ou une annulation le supprime
dans l'ordre inverse (`rollback`), et remet la source dans son état de
départ. En fin de transfert réussi, l'étiquette est retirée de ce qui reste
(la VM cible, ses volumes, ses secrets).
"""

import time

import vm_transfer as vt
import vm_transfer_progress as vp

K_VM = "virtualmachines.kubevirt.io"
K_VMI = "virtualmachineinstances.kubevirt.io"
K_BACKUP = "virtualmachinebackups.harvesterhci.io"
K_RESTORE = "virtualmachinerestores.harvesterhci.io"
K_IMAGE = "virtualmachineimages.harvesterhci.io"
K_SETTING = "settings.harvesterhci.io"
K_DV = "datavolumes.cdi.kubevirt.io"
K_PVC = "persistentvolumeclaims"
K_SECRET = "secrets"
K_NS = "namespaces"
K_LH_TARGET = "backuptargets.longhorn.io"
K_LH_SETTING = "settings.longhorn.io"

# Profil « maximal » : concurrence de Longhorn relevée le temps du transfert
# (2 par défaut, relevé sur harv1), puis rétablie.
BOOST_SETTINGS = (("src", "backup-concurrent-limit"), ("dst", "restore-concurrent-limit"))
BOOST_VALUE = 8

TRANSFERRED_FROM = "harvester-ops.io/transferred-from"
# Harvester ne relit sa cible de sauvegarde que si cette annotation du
# réglage `backup-target` manque (ou si la valeur change) ; avec
# refreshIntervalInSeconds à 0, jamais d'elle-même. La retirer est sans
# risque : il la réécrit après la relecture.
HASH_ANNOTATION = "harvesterhci.io/hash"

IMAGE_DOWNLOAD = ("/api/v1/namespaces/harvester-system/services/https:harvester:8443/"
                  "proxy/v1/harvester/harvesterhci.io.virtualmachineimages/{ns}/{name}/download")

# Délais (secondes). Généreux : une sauvegarde ou une restauration de
# plusieurs centaines de gigaoctets se compte en heures.
TIMEOUTS = {
    # la synchronisation ramène d'abord les images, qui peuvent peser
    # plusieurs gigaoctets : le délai est large, la relance fréquente
    "stop": 600, "start": 900, "backup": 6 * 3600, "sync": 2 * 3600, "sync_nudge": 20,
    "restore": 6 * 3600, "export": 6 * 3600, "import": 12 * 3600,
    "first_hit": 300, "delete": 600, "undo": 300,
}


class TransferError(Exception):
    def __init__(self, sid, message):
        super().__init__(message)
        self.sid = sid


class Fail:
    """Rendu par une condition d'attente : échec définitif, inutile d'attendre."""

    def __init__(self, message):
        self.message = message


class Ctx:
    def __init__(self, src, dst, req, tid, *, emit=vt.step, sleep=time.sleep,
                 now=time.time, server=None, advertise=None, timeouts=None,
                 source_cluster="", target_cluster="", progress=vp.emit_line):
        self.src, self.dst, self.req, self.tid = src, dst, req, tid
        self.emit, self.sleep, self.now = emit, sleep, now
        self.progress = progress
        self.boosted = []               # (côté, réglage Longhorn, valeur d'origine)
        self.server, self.advertise = server, advertise
        self.timeouts = dict(TIMEOUTS, **(timeouts or {}))
        self.source_cluster, self.target_cluster = source_cluster, target_cluster
        self.created = []               # (côté, kind, ns, nom)
        self.source_initial = None      # runStrategy de départ
        self.source_stopped = False     # arrêtée par nous, à relancer en cas d'échec
        self.backups = []               # (ns, nom) des sauvegardes du transfert
        self.restore = None             # (ns, nom) de l'objet de restauration
        self.target = None              # (ns, nom) de la VM cible
        self.removed = []               # ce que la réécriture a retiré

    def kube(self, side):
        return self.src if side == "src" else self.dst

    def tracker(self, phase, total, items_total=None):
        return vp.Progress(self.progress, phase, total, now=self.now, items_total=items_total)

    def record(self, side, kind, ns, name):
        self.created.append((side, kind, ns, name))

    def forget(self, side, kind, ns, name):
        try:
            self.created.remove((side, kind, ns, name))
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Attente et petites opérations
# ---------------------------------------------------------------------------

# Erreurs d'une API momentanément injoignable : vécu sur le banc, la VIP
# d'un cluster a décroché quelques secondes pendant un long transfert.
_TRANSIENT = ("Unable to connect to the server", "connection refused", "no route to host",
              "i/o timeout", "ServiceUnavailable", "TLS handshake timeout",
              "connection reset by peer", "EOF", "the server is currently unable",
              "timed out")


def transient(e):
    msg = str(e)
    return any(t in msg for t in _TRANSIENT)


def wait_for(ctx, sid, what, fn, timeout, every=5):
    """Attend que `fn()` rende True. Une chaîne est une progression (émise
    quand elle change, pas à chaque tour : le journal d'une action est
    borné), un `Fail` un échec définitif. Une API momentanément injoignable
    n'arrête pas l'attente : on réessaie jusqu'au délai."""
    deadline = ctx.now() + timeout
    last = None
    while True:
        try:
            r = fn()
        except Exception as e:                  # noqa: BLE001
            if not transient(e):
                raise
            r = f"API unreachable, retrying ({str(e)[:120]})"
        if r is True:
            return
        if isinstance(r, Fail):
            raise TransferError(sid, r.message)
        if isinstance(r, str) and r != last:
            ctx.emit(sid, "running", f"{what}: {r}")
            last = r
        if ctx.now() >= deadline:
            raise TransferError(sid, f"timed out waiting for {what}")
        ctx.sleep(every)


def _close(ctx, sid, prog):
    """Point final d'une phase, et son bilan gardé comme message d'étape."""
    ctx.emit(sid, "done", vp.summary(prog.phase, prog.finish()))


def _pct(obj):
    try:
        return max(0.0, min(100.0, float(((obj or {}).get("status") or {}).get("progress") or 0)))
    except (TypeError, ValueError):
        return 0.0


def _vm_disk_total(ctx, vm):
    """Somme des tailles des disques de la VM source (pour les phases dont
    la mesure est un pourcentage)."""
    total = 0
    tspec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
    for vol in tspec.get("volumes") or []:
        claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
        if claim:
            p = ctx.src.get(K_PVC, ctx.req["vm_ns"], claim) or {}
            total += vt.parse_quantity((((p.get("spec") or {}).get("resources") or {})
                                        .get("requests") or {}).get("storage"))
    return total


def boost_longhorn(ctx):
    """Profil maximal : plus de fils par sauvegarde (source) et par
    restauration (cible). Coûte du CPU et du réseau à tous les nœuds, et
    profite à toute autre sauvegarde en cours : rétabli dès la fin."""
    for side, name in BOOST_SETTINGS:
        kube = ctx.kube(side)
        s = kube.get(K_LH_SETTING, "longhorn-system", name)
        old = str((s or {}).get("value") or "")
        if not old.isdigit() or int(old) >= BOOST_VALUE:
            continue
        kube.patch(K_LH_SETTING, "longhorn-system", name, {"value": str(BOOST_VALUE)})
        ctx.boosted.append((side, name, old))
    if ctx.boosted:
        ctx.emit("speed", "done", "Longhorn concurrency raised to "
                 f"{BOOST_VALUE}: " + ", ".join(f"{n} ({s}, was {o})" for s, n, o in ctx.boosted))


def restore_longhorn(ctx):
    for side, name, old in reversed(ctx.boosted):
        try:
            ctx.kube(side).patch(K_LH_SETTING, "longhorn-system", name, {"value": old})
        except Exception as e:                  # noqa: BLE001
            ctx.emit("speed", "error", f"{name} not restored to {old} on {side}: {e}")
            continue
    if ctx.boosted:
        ctx.emit("speed", "done", "Longhorn concurrency restored")
    ctx.boosted.clear()


def _cond(obj, ctype):
    for c in ((obj or {}).get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c
    return None


def vmi_phase(kube, ns, name):
    vmi = kube.get(K_VMI, ns, name)
    return ((vmi or {}).get("status") or {}).get("phase")


def stop_vm(ctx, kube, ns, name, sid="stop-source"):
    ctx.emit(sid, "running", f"stopping {ns}/{name}")
    kube.patch(K_VM, ns, name, {"spec": {"runStrategy": "Halted"}})
    wait_for(ctx, sid, "VM stopped", lambda: kube.get(K_VMI, ns, name) is None,
             ctx.timeouts["stop"])
    ctx.emit(sid, "done", f"{ns}/{name} stopped")


def start_vm(ctx, kube, ns, name, strategy="Always", sid="start"):
    ctx.emit(sid, "running", f"starting {ns}/{name}")
    kube.patch(K_VM, ns, name, {"spec": {"runStrategy": strategy}})

    def running():
        phase = vmi_phase(kube, ns, name)
        if phase == "Failed":
            return Fail(f"{ns}/{name} failed to start")
        return True if phase == "Running" else (phase or "pending")

    wait_for(ctx, sid, "VM running", running, ctx.timeouts["start"])
    ctx.emit(sid, "done", f"{ns}/{name} running")


def _short(name, limit=63):
    """Nom Kubernetes borné, sans tiret final."""
    return name[:limit].rstrip("-.")


def delete_quiet(kube, kind, ns, name, cascade=None):
    try:
        kube.delete(kind, ns, name, cascade=cascade)
        return True
    except Exception:                          # noqa: BLE001
        return False


def _undo_delete(ctx, kube, kind, ns, name, cascade=None):
    """Supprime pour un retour arrière : réessaie tant que l'API est
    momentanément injoignable (vécu : le retour arrière d'un transfert
    interrompu par une API tombée échouait en silence). Rend False si la
    ressource n'a pas pu être supprimée."""
    deadline = ctx.now() + ctx.timeouts["undo"]
    while True:
        try:
            kube.delete(kind, ns, name, cascade=cascade)
            return True
        except Exception as e:                  # noqa: BLE001
            msg = str(e)
            if "NotFound" in msg or "not found" in msg:
                return True
            if not transient(e) or ctx.now() >= deadline:
                return False
            ctx.sleep(10)


def ensure_namespace(ctx, side, ns):
    kube = ctx.kube(side)
    if kube.get(K_NS, None, ns) is None:
        kube.create(vt.namespace_manifest(ns, ctx.tid))
        ctx.record(side, K_NS, None, ns)
        ctx.emit("namespace", "done", f"namespace {ns} created")


def _source_state(ctx):
    req = ctx.req
    vm = ctx.src.get(K_VM, req["vm_ns"], req["vm_name"])
    if vm is None:
        raise TransferError("check", f"VM {req['vm_ns']}/{req['vm_name']} not found")
    spec = vm.get("spec") or {}
    ctx.source_initial = spec.get("runStrategy") or ("Always" if spec.get("running") else "Halted")
    running = vmi_phase(ctx.src, req["vm_ns"], req["vm_name"]) == "Running"
    return vm, running


def _stop_source_if_needed(ctx, running):
    req = ctx.req
    if running and req.get("source") != "running":
        stop_vm(ctx, ctx.src, req["vm_ns"], req["vm_name"])
        ctx.source_stopped = True


# ---------------------------------------------------------------------------
# Moteur « sauvegarde »
# ---------------------------------------------------------------------------

def make_backup(ctx, suffix, total=0):
    req = ctx.req
    ns = req["vm_ns"]
    name = _short(f"{req['vm_name']}-xfer-{ctx.tid}-{suffix}")
    ctx.emit("backup", "running", f"backup {name}")
    ctx.src.create(vt.backup_manifest(ns, req["vm_name"], name, ctx.tid))
    ctx.record("src", K_BACKUP, ns, name)
    ctx.backups.append((ns, name))
    prog = ctx.tracker("backup", total)

    def ready():
        b = ctx.src.get(K_BACKUP, ns, name) or {}
        st = b.get("status") or {}
        err = (st.get("error") or {}).get("message")
        if err:
            return Fail(f"backup {name}: {err}")
        if st.get("readyToUse"):
            prog.update(total)
            return True
        prog.update(int(total * _pct(b) / 100))
        return "in progress"

    wait_for(ctx, "backup", f"backup {name}", ready, ctx.timeouts["backup"], every=2)
    _close(ctx, "backup", prog)
    return ns, name


def _images(kube):
    return {(i["metadata"].get("namespace"), i["metadata"]["name"]): i
            for i in kube.list(K_IMAGE)}


def wait_synced(ctx, ns, name):
    """La sauvegarde doit apparaître sur la cible, par la synchronisation des
    métadonnées de Harvester. Relevé sur harvlab2 : la relecture n'a lieu
    que si l'annotation `harvesterhci.io/hash` manque, et son premier passage
    ne fait que ramener les images dont la VM est née ; la sauvegarde n'est
    visible qu'à un passage suivant, une fois leur classe de stockage créée.
    On relance donc à intervalle régulier jusqu'à la voir."""
    ctx.emit("sync", "running", f"waiting for {name} on {ctx.target_cluster or 'the target'}")
    before = set(_images(ctx.dst))
    last = [None]
    img_prog = [None]

    def synced():
        b = ctx.dst.get(K_BACKUP, ns, name)
        if b is not None:
            if (b.get("status") or {}).get("readyToUse"):
                return True
            return "synced, not ready yet"
        now = ctx.now()
        if last[0] is None or now - last[0] >= ctx.timeouts["sync_nudge"]:
            # Longhorn d'abord : Harvester saute une sauvegarde dont Longhorn
            # n'a pas encore vu les volumes (« longhorn backup is not found »),
            # et Longhorn ne relit sa cible que toutes les 5 minutes
            try:
                ctx.dst.patch(K_LH_TARGET, "longhorn-system", "default", {"spec": {
                    "syncRequestedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     time.gmtime(ctx.now()))}})
            except Exception:                   # noqa: BLE001
                pass                            # une version sans ce champ : on attend
            ctx.dst.patch(K_SETTING, None, "backup-target",
                          {"metadata": {"annotations": {HASH_ANNOTATION: None}}})
            last[0] = now
        brought = {k: i for k, i in _images(ctx.dst).items()
                   if k not in before and (i.get("spec") or {}).get("sourceType") == "restore"}
        if brought:
            sizes = {k: int((i.get("status") or {}).get("virtualSize") or 0)
                     for k, i in brought.items()}
            if img_prog[0] is None or img_prog[0].total != sum(sizes.values()):
                img_prog[0] = ctx.tracker("images", sum(sizes.values()), items_total=len(brought))
            img_prog[0].update(int(sum(sizes[k] * _pct(i) / 100 for k, i in brought.items())),
                               items_done=sum(1 for i in brought.values() if _pct(i) >= 100))
            return "images being restored: " + ", ".join(sorted(k[1] for k in brought))
        return "not synced yet"

    try:
        wait_for(ctx, "sync", f"backup {name} on the target", synced, ctx.timeouts["sync"],
                 every=5)
        if img_prog[0] is not None:
            _close(ctx, "sync", img_prog[0])
    finally:
        # les images que Harvester a ramenées pour ce transfert : à défaire
        # avec lui s'il échoue
        for k, i in _images(ctx.dst).items():
            if k not in before and (i.get("spec") or {}).get("sourceType") == "restore":
                ctx.record("dst", K_IMAGE, k[0], k[1])
    # l'objet synchronisé est à nous aussi : à nettoyer comme l'original
    ctx.record("dst", K_BACKUP, ns, name)
    ctx.emit("sync", "done", f"{name} visible on the target")


def restore_on_target(ctx, backup_ns, backup_name, total=0):
    req = ctx.req
    ns, name = req["namespace"], req["name"]
    keep_mac = _keep_mac(req)
    halt = req.get("restore_halt", True) is not False
    m = vt.restore_manifest(backup_ns, backup_name, name, ns, keep_mac, ctx.tid, halt=halt)
    rname = m["metadata"]["name"]
    ctx.emit("restore", "running", f"restoring {ns}/{name}")
    ctx.dst.create(m)
    ctx.record("dst", K_RESTORE, ns, rname)
    ctx.restore = (ns, rname)
    ctx.target = (ns, name)
    prog = ctx.tracker("restore", total)

    def complete():
        r = ctx.dst.get(K_RESTORE, ns, rname) or {}
        st = r.get("status") or {}
        if st.get("complete"):
            prog.update(total)
            return True
        err = _cond(r, "Failure")
        if err and err.get("status") == "True":
            return Fail(f"restore: {err.get('message') or err.get('reason')}")
        prog.update(int(total * _pct(r) / 100))
        return "in progress"

    wait_for(ctx, "restore", f"restore of {ns}/{name}", complete, ctx.timeouts["restore"], every=2)
    _close(ctx, "restore", prog)
    if not halt:
        # Harvester 1.8 ne connaît pas haltAfterRestore : la VM restaurée
        # démarre d'elle-même, avec les réseaux d'origine. On l'arrête avant
        # d'appliquer les correspondances.
        stop_vm(ctx, ctx.dst, ns, name, sid="restore")
    ctx.emit("restore", "done", f"{ns}/{name} restored")


def remap_restored(ctx):
    """La restauration reprend les réseaux d'origine : on applique les
    correspondances, et on retire ce qui ne voyage pas."""
    ns, name = ctx.target
    vm = ctx.dst.get(K_VM, ns, name)
    if vm is None:
        raise TransferError("remap", f"restored VM {ns}/{name} not found")
    rv = (vm.get("metadata") or {}).get("resourceVersion")
    new, removed = vt.retarget_vm(vm, name=name, namespace=ns,
                                  networks=ctx.req.get("networks") or {}, claims={},
                                  secrets={}, keep_mac=True, transfer_id=ctx.tid)
    new.pop("status", None)
    if rv:
        new["metadata"]["resourceVersion"] = rv
    ctx.dst.replace(new)
    ctx.removed = removed
    ctx.emit("remap", "done", "networks mapped" + (f", removed: {', '.join(removed)}" if removed else ""))


def run_backup(ctx):
    req = ctx.req
    vm, running = _source_state(ctx)
    total = _vm_disk_total(ctx, vm)
    if req.get("boost"):
        boost_longhorn(ctx)
    keep_running = req.get("source") == "running"
    if req.get("mode") == "short" and running and not keep_running:
        make_backup(ctx, "a", total)    # VM en marche : le gros de la copie
    _stop_source_if_needed(ctx, running)
    ns, name = make_backup(ctx, "b" if req.get("mode") == "short" and running and not keep_running else "a",
                           total)
    if req["namespace"] != ns:
        # la sauvegarde se synchronise dans son namespace d'origine
        ensure_namespace(ctx, "dst", ns)
    if req.get("create_namespace"):
        ensure_namespace(ctx, "dst", req["namespace"])
    wait_synced(ctx, ns, name)
    restore_on_target(ctx, ns, name, total)
    remap_restored(ctx)
    return ctx.target


# ---------------------------------------------------------------------------
# Moteur « fichier »
# ---------------------------------------------------------------------------

def _keep_mac(req):
    if req.get("source") == "running":
        return False                    # deux VM en marche, deux adresses
    return req.get("keep_mac", True) is not False


def _read_source(ctx):
    req = ctx.req
    vm, running = _source_state(ctx)
    pvcs = {}
    tspec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
    for vol in tspec.get("volumes") or []:
        claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
        if claim:
            pvcs[claim] = ctx.src.get(K_PVC, req["vm_ns"], claim) or {}
    inv = vt.vm_inventory(vm, pvcs)
    secrets = []
    for s in inv["secrets"]:
        obj = ctx.src.get(K_SECRET, req["vm_ns"], s)
        if obj is not None:
            secrets.append({"name": s, "type": obj.get("type") or "Opaque",
                            "data": dict(obj.get("data") or {})})
    return vm, running, inv, secrets


def export_images(ctx, inv, running, export_class):
    """Arrête la source, fige chaque disque dans une image temporaire, et
    rallume la source aussitôt si elle doit rester en marche."""
    req = ctx.req
    ns = req["vm_ns"]
    if running:
        # toujours arrêtée ici, même en copie : un disque en cours d'écriture
        # ne se fige pas proprement ; elle repart dès l'image prête
        stop_vm(ctx, ctx.src, ns, req["vm_name"])
        ctx.source_stopped = True
    exported = []
    for d in inv["disks"]:
        img = _short(f"xfer-{ctx.tid}-{d['volume']}")
        ctx.emit("export", "running", f"freezing {d['volume']} into {img}")
        ctx.src.create(vt.export_image_manifest(ns, d["claim"], img, ctx.tid, export_class))
        ctx.record("src", K_IMAGE, ns, img)
        exported.append((d, img))
    # tous les disques se figent en même temps (Longhorn copie chaque
    # volume de son côté) : une attente commune, une progression commune
    prog = ctx.tracker("freeze", sum(d["size"] for d, _ in exported), items_total=len(exported))

    def all_imported():
        done, ready = 0, 0
        for d, img in exported:
            obj = ctx.src.get(K_IMAGE, ns, img) or {}
            st = obj.get("status") or {}
            c = _cond(obj, "Imported")
            if c and c.get("status") == "True":
                done += d["size"]
                ready += 1
                continue
            if c and c.get("status") == "False" and int(st.get("failed") or 0) >= 3:
                return Fail(f"export of {img}: {c.get('message') or c.get('reason')}")
            done += int(d["size"] * _pct(obj) / 100)
        prog.update(done, items_done=ready)
        return True if ready == len(exported) else "in progress"

    wait_for(ctx, "export", "disks frozen", all_imported, ctx.timeouts["export"], every=2)
    _close(ctx, "export", prog)
    if running and req.get("source") == "running" and ctx.source_stopped:
        start_vm(ctx, ctx.src, ns, req["vm_name"], ctx.source_initial, sid="restart-source")
        ctx.source_stopped = False
    return exported


def image_stream(ctx, ns, img, attempts=5):
    """Le téléchargement d'une image, repris s'il est refusé AVANT son
    premier octet. Vécu sur harvlab2 : le proxy de l'API a répondu une fois
    « ServiceUnavailable ... tls: unrecognized name », puis a servi le même
    téléchargement une minute plus tard. Une coupure en cours de route, elle,
    remonte : reprendre au milieu donnerait un disque faux."""
    path = IMAGE_DOWNLOAD.format(ns=ns, name=img)
    for attempt in range(1, attempts + 1):
        it = iter(ctx.src.raw_stream(path))
        try:
            first = next(it)
        except StopIteration:
            return
        except Exception as e:                  # noqa: BLE001
            if attempt == attempts:
                raise
            ctx.emit("download", "running", f"{img}: download refused ({e}), retrying")
            ctx.sleep(15 * attempt)
            continue
        yield first
        yield from it
        return


def delete_export_images(ctx, exported):
    ns = ctx.req["vm_ns"]
    for _d, img in exported:
        if delete_quiet(ctx.src, K_IMAGE, ns, img):
            ctx.forget("src", K_IMAGE, ns, img)


def write_archive(ctx, writer, vm, inv, secrets, exported, source_version=""):
    req = ctx.req
    disks = []
    for d, _img in exported:
        disks.append(dict(d, member=f"disks/{d['volume']}.raw.gz"))
    writer.add_json(vt.MANIFEST, {
        "format": vt.FORMAT,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ctx.now())),
        "source": {"cluster": ctx.source_cluster, "version": source_version,
                   "namespace": req["vm_ns"], "name": req["vm_name"]},
        "vm": vt.sanitize_vm(vm),
        "inventory": dict(inv, disks=disks),
        "secrets": secrets,
    })
    prog = ctx.tracker("download", sum(d["size"] for d, _ in exported),
                       items_total=len(exported))
    for i, (d, img) in enumerate(exported):
        prog.update(prog.done, item=d["volume"], items_done=i)
        for attempt in range(1, 4):
            # un disque coupé en cours de route est réécrit depuis le début,
            # au même endroit de l'archive (vécu : la VIP de la source a
            # décroché pendant un téléchargement)
            pos, before = writer.f.tell(), (prog.done, prog.wire)
            try:
                writer.add_stream(f"disks/{d['volume']}.raw.gz",
                                  _counted(image_stream(ctx, req["vm_ns"], img), prog))
                break
            except Exception as e:              # noqa: BLE001
                if attempt == 3 or not transient(e):
                    raise
                writer.f.seek(pos)
                writer.f.truncate()
                prog.update(before[0], wire=before[1])
                ctx.emit("download", "running",
                         f"{d['volume']}: download broke ({str(e)[:120]}), starting it again")
                ctx.sleep(15 * attempt)
    prog.update(prog.done, items_done=len(exported))
    writer.close()
    _close(ctx, "download", prog)


def _counted(chunks, prog):
    """Laisse passer un flux gzip en comptant ses octets bruts et transmis."""
    counter = vp.GzipCounter()
    for chunk in chunks:
        prog.add(counter.feed(chunk), len(chunk))
        yield chunk


def import_disks(ctx, disks, sources):
    """`sources` : {volume: (opener, taille ou None)}. Un DataVolume par
    disque, servi par le guichet ; rend {ancien PVC: nouveau PVC}.

    Les disques s'importent en même temps, par lots de `req["parallel"]`
    (tous par défaut) : Longhorn compresse chaque téléchargement sur un seul
    cœur, plusieurs flux vont donc plus vite qu'un seul. Le compte d'un
    disque repart de zéro à chaque nouvelle requête : la première connexion
    de CDI n'est qu'une sonde, coupée après quelques octets."""
    req = ctx.req
    ns, name = req["namespace"], req["name"]
    classes = req.get("storage_classes") or {}
    size_of = {d["volume"]: int(d.get("size") or 0) for d in disks}
    prog = ctx.tracker("import", sum(size_of.values()), items_total=len(disks))
    counts = {}                          # volume -> [brut, transmis] de la requête en cours
    finished = set()

    def counted(vol, opener):
        def run():
            counts[vol] = [0, 0]
            counter = vp.GzipCounter()
            for chunk in opener():
                counts[vol][0] += counter.feed(chunk)
                counts[vol][1] += len(chunk)
                yield chunk
        return run

    def report():
        raw = sum(size_of[v] if v in finished else c[0] for v, c in counts.items())
        raw += sum(size_of[v] for v in finished if v not in counts)
        prog.update(min(raw, prog.total or raw), wire=sum(c[1] for c in counts.values()),
                    items_done=len(finished))

    claims, published = {}, []
    broken = {}                          # chemin -> coupures déjà signalées
    batch = int(req.get("parallel") or 0) or len(disks)
    try:
        for start in range(0, len(disks), batch):
            group = []
            for d in disks[start:start + batch]:
                opener, size = sources[d["volume"]]
                path = ctx.server.publish(counted(d["volume"], opener), size)
                published.append(path)
                claim = _short(f"{name}-{d['volume']}-{ctx.tid}")
                url = f"http://{ctx.advertise}{path}"
                sc = classes.get(d["storage_class"]) or d["storage_class"]
                ctx.emit("import", "running", f"{d['volume']}: importing into {sc}")
                ctx.dst.create(vt.datavolume_manifest(ns, claim, url, d, sc, ctx.tid))
                ctx.record("dst", K_PVC, ns, claim)
                ctx.record("dst", K_DV, ns, claim)
                claims[d["claim"]] = claim
                group.append((d, claim, path, ctx.now()))

            def all_done(group=group):
                pending = 0
                for d, claim, path, started in group:
                    if d["volume"] in finished:
                        continue
                    dv = ctx.dst.get(K_DV, ns, claim) or {}
                    phase = (dv.get("status") or {}).get("phase") or ""
                    # une coupure du flux de la source n'est pas fatale : CDI
                    # redemande le disque, et on le resert depuis le début ;
                    # trois coupures, en revanche, arrêtent le transfert
                    errs = ctx.server.errors(path)
                    if errs >= 3:
                        return Fail(f"source stream failed: {ctx.server.error(path)}")
                    if errs > broken.get(path, 0):
                        broken[path] = errs
                        ctx.emit("import", "running",
                                 f"{d['volume']}: source stream broke ({ctx.server.error(path)}), "
                                 "the target fetches it again")
                    if phase == "Succeeded":
                        finished.add(d["volume"])
                        # CDI ne ramasse pas le DataVolume, et le volume lui
                        # appartient : le supprimer « orphelin » laisse un
                        # volume ordinaire (vérifié)
                        ctx.dst.delete(K_DV, ns, claim, cascade="orphan")
                        ctx.forget("dst", K_DV, ns, claim)
                        ctx.emit("import", "running", f"{d['volume']}: imported")
                        continue
                    if phase == "Failed":
                        return Fail(f"import failed: {(_cond(dv, 'Running') or {}).get('message') or phase}")
                    if (ctx.server.hits(path) == 0
                            and ctx.now() - started >= ctx.timeouts["first_hit"]):
                        msg = (_cond(dv, "Running") or {}).get("message") or phase or "no request"
                        return Fail("the target cluster never fetched the disk from "
                                    f"{ctx.advertise} ({msg}): check that its nodes can "
                                    "reach this host on that port")
                    pending += 1
                report()
                return True if not pending else "in progress"

            wait_for(ctx, "import", "disks imported", all_done, ctx.timeouts["import"], every=2)
    finally:
        for p in published:
            ctx.server.revoke(p)
    report()
    _close(ctx, "import", prog)
    return claims


def create_target_vm(ctx, clean_vm, secrets, claims):
    req = ctx.req
    ns, name = req["namespace"], req["name"]
    smap = {}
    for i, s in enumerate(secrets):
        # nom court et stable : repris du nom d'origine, il s'allongeait à
        # chaque transfert (xfer-back-xfer-test-xfer-test-cloudinit-...)
        suffix = "cloudinit" if len(secrets) == 1 else f"cloudinit{i}"
        new = _short(f"{name}-{suffix}-{ctx.tid}")
        ctx.dst.create(vt.secret_manifest(s, new, ns, ctx.tid))
        ctx.record("dst", K_SECRET, ns, new)
        smap[s["name"]] = new
    vm, removed = vt.retarget_vm(clean_vm, name=name, namespace=ns,
                                 networks=req.get("networks") or {}, claims=claims,
                                 secrets=smap, keep_mac=_keep_mac(req),
                                 transfer_id=ctx.tid)
    ctx.dst.create(vm)
    ctx.record("dst", K_VM, ns, name)
    ctx.target = (ns, name)
    ctx.removed = removed
    ctx.emit("vm", "done", f"{ns}/{name} created"
             + (f", removed: {', '.join(removed)}" if removed else ""))
    return ctx.target


def run_import(ctx, manifest, sources):
    req = ctx.req
    if req.get("create_namespace"):
        ensure_namespace(ctx, "dst", req["namespace"])
    claims = import_disks(ctx, manifest["inventory"]["disks"], sources)
    return create_target_vm(ctx, manifest["vm"], manifest.get("secrets") or [], claims)


def run_export(ctx, writer, export_class, source_version=""):
    vm, running, inv, secrets = _read_source(ctx)
    exported = export_images(ctx, inv, running, export_class)
    try:
        write_archive(ctx, writer, vm, inv, secrets, exported, source_version)
    finally:
        delete_export_images(ctx, exported)
    return exported


def run_direct(ctx, export_class):
    """Sans cible commune ni fichier : chaque disque passe du téléchargement
    sur la source au DataVolume de la cible, en flux."""
    vm, running, inv, secrets = _read_source(ctx)
    exported = export_images(ctx, inv, running, export_class)
    try:
        sources = {d["volume"]: ((lambda img=img: image_stream(ctx, ctx.req["vm_ns"], img)), None)
                   for d, img in exported}
        manifest = {"vm": vt.sanitize_vm(vm), "inventory": inv, "secrets": secrets}
        if ctx.req.get("create_namespace"):
            ensure_namespace(ctx, "dst", ctx.req["namespace"])
        claims = import_disks(ctx, inv["disks"], sources)
        target = create_target_vm(ctx, manifest["vm"], secrets, claims)
    finally:
        delete_export_images(ctx, exported)
    return target


# ---------------------------------------------------------------------------
# Fin : états finaux, nettoyage, retour arrière
# ---------------------------------------------------------------------------

def _unlabel(kube, kind, ns, name):
    try:
        kube.patch(kind, ns, name, {"metadata": {"labels": {vt.TRANSFER_LABEL: None}}})
    except Exception:                          # noqa: BLE001
        pass


def _target_claims(ctx):
    ns, name = ctx.target
    vm = ctx.dst.get(K_VM, ns, name) or {}
    tspec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
    claims = [(v.get("persistentVolumeClaim") or {}).get("claimName")
              for v in tspec.get("volumes") or []]
    return [c for c in claims if c]


def target_strategy(ctx):
    """La cible repart comme la source tournait ; une source arrêtée donne le
    choix par défaut de Harvester."""
    s = ctx.source_initial or ctx.req.get("run_strategy")
    return s if s in ("Always", "RerunOnFailure") else "RerunOnFailure"


def verify_target(ctx):
    req = ctx.req
    ns, name = ctx.target
    if req.get("target") == "started":
        start_vm(ctx, ctx.dst, ns, name, target_strategy(ctx), sid="start-target")
        return
    for c in _target_claims(ctx):
        def bound(c=c):
            p = ctx.dst.get(K_PVC, ns, c)
            return True if ((p or {}).get("status") or {}).get("phase") == "Bound" else "pending"
        wait_for(ctx, "verify", f"volume {c}", bound, ctx.timeouts["start"])
    ctx.emit("verify", "done", f"{ns}/{name} ready, left stopped")


def settle_source(ctx):
    req = ctx.req
    ns, name = req["vm_ns"], req["vm_name"]
    dest = f"{ctx.target_cluster}/{ctx.target[0]}/{ctx.target[1]}"
    want = req.get("source")
    if want == "running":
        if ctx.source_stopped:
            start_vm(ctx, ctx.src, ns, name, ctx.source_initial or "Always", sid="source")
            ctx.source_stopped = False
        ctx.emit("source", "done", f"{ns}/{name} left running")
    elif want == "deleted":
        vm = ctx.src.get(K_VM, ns, name) or {}
        tspec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
        claims = [(v.get("persistentVolumeClaim") or {}).get("claimName")
                  for v in tspec.get("volumes") or []]
        ctx.src.delete(K_VM, ns, name)
        wait_for(ctx, "source", "source VM deleted",
                 lambda: ctx.src.get(K_VM, ns, name) is None, ctx.timeouts["delete"])
        for c in claims:
            if c:
                delete_quiet(ctx.src, K_PVC, ns, c)
        ctx.source_stopped = False
        ctx.emit("source", "done", f"{ns}/{name} deleted with its volumes")
    else:
        ctx.src.patch(K_VM, ns, name, {"spec": {"runStrategy": "Halted"},
                                        "metadata": {"annotations": {vt.TRANSFERRED_TO: dest}}})
        ctx.source_stopped = False
        ctx.emit("source", "done", f"{ns}/{name} left stopped, marked as moved to {dest}")


def cleanup(ctx):
    """Ce qui ne servait qu'au transfert : sauvegardes (sauf demande
    contraire), objet de restauration ; et l'étiquette de transfert retirée
    de ce qui reste."""
    req = ctx.req
    if ctx.restore:
        # Harvester lie la restauration à la VM restaurée pour toute sa vie
        # (« The restore can't be removed because the restored VM exists ») :
        # elle reste, sans l'étiquette du transfert
        _unlabel(ctx.dst, K_RESTORE, *ctx.restore)
        ctx.forget("dst", K_RESTORE, *ctx.restore)
    for side, kind, ns, name in list(ctx.created):
        if kind == K_BACKUP:
            if req.get("keep_backups"):
                _unlabel(ctx.kube(side), kind, ns, name)
            else:
                delete_quiet(ctx.kube(side), kind, ns, name)
            ctx.forget(side, kind, ns, name)
    ns, name = ctx.target
    ctx.dst.patch(K_VM, ns, name, {"metadata": {
        "labels": {vt.TRANSFER_LABEL: None},
        "annotations": {TRANSFERRED_FROM: f"{ctx.source_cluster}/{req['vm_ns']}/{req['vm_name']}"}}})
    for side, kind, kns, kname in list(ctx.created):
        if kind not in (K_VM, K_IMAGE):
            _unlabel(ctx.kube(side), kind, kns, kname)
    ctx.created.clear()
    ctx.emit("cleanup", "done", "transfer resources removed")


def finalize(ctx):
    """Transfert ou import : la cible est vérifiée AVANT que la source soit
    touchée (arrêtée pour de bon ou supprimée)."""
    verify_target(ctx)
    if ctx.req.get("kind", "migrate") == "migrate":
        settle_source(ctx)
    cleanup(ctx)
    restore_longhorn(ctx)


def finish_export(ctx):
    """Un export n'est pas un déplacement : la source n'est ni annotée ni
    supprimée. Rallumée plus tôt si elle devait rester en marche, sinon
    laissée arrêtée."""
    ctx.source_stopped = False
    ctx.created.clear()
    ctx.emit("source", "done", "source left " + (
        "running" if ctx.req.get("source") == "running" else "stopped"))


def rollback(ctx):
    """Défait ce que le transfert a créé, dans l'ordre inverse, et remet la
    source dans son état de départ. Ne supprime jamais la source."""
    ctx.emit("rollback", "running", "undoing the transfer")
    restore_longhorn(ctx)
    if ctx.restore and ctx.target:
        ns, name = ctx.target
        r = ctx.dst.get(K_RESTORE, *ctx.restore) or {}
        # la VM et les volumes que la restauration a créés : leur nom était
        # libre au contrôle, ils sont donc à nous. La VM d'abord : Harvester
        # refuse de supprimer la restauration tant qu'elle existe
        if _undo_delete(ctx, ctx.dst, K_VM, ns, name):
            try:
                wait_for(ctx, "rollback", "restored VM deleted",
                         lambda: ctx.dst.get(K_VM, ns, name) is None, ctx.timeouts["delete"])
            except TransferError as e:
                ctx.emit("rollback", "error", str(e))
        _undo_delete(ctx, ctx.dst, K_RESTORE, *ctx.restore)
        ctx.forget("dst", K_RESTORE, *ctx.restore)
        for rs in ((r.get("status") or {}).get("restores") or []):
            pvc = ((rs.get("persistentVolumeClaimSpec") or {}).get("metadata") or {}).get("name")
            if pvc:
                _undo_delete(ctx, ctx.dst, K_PVC, ns, pvc)
    left = []
    for side, kind, ns, name in reversed(list(ctx.created)):
        if not _undo_delete(ctx, ctx.kube(side), kind, ns, name):
            cluster = ctx.source_cluster if side == "src" else ctx.target_cluster
            left.append(f"{kind} {ns + '/' if ns else ''}{name} ({cluster or side})")
    ctx.created.clear()
    if left:
        # dire exactement ce qui reste : tout porte l'étiquette du transfert
        ctx.emit("rollback", "error", "could not undo, remove by hand (label "
                 f"{vt.TRANSFER_LABEL}={ctx.tid}): " + "; ".join(left))
    if ctx.source_stopped:
        try:
            start_vm(ctx, ctx.src, ctx.req["vm_ns"], ctx.req["vm_name"],
                     ctx.source_initial or "Always", sid="rollback")
            ctx.source_stopped = False
        except TransferError as e:
            ctx.emit("rollback", "error", f"source not restarted: {e}")
    ctx.emit("rollback", "done", "transfer undone, source in its initial state")
