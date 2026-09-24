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

TRANSFERRED_FROM = "harvester-ops.io/transferred-from"
RESYNC_ANNOTATION = "harvester-ops.io/resync"

IMAGE_DOWNLOAD = ("/api/v1/namespaces/harvester-system/services/https:harvester:8443/"
                  "proxy/v1/harvester/harvesterhci.io.virtualmachineimages/{ns}/{name}/download")

# Délais (secondes). Généreux : une sauvegarde ou une restauration de
# plusieurs centaines de gigaoctets se compte en heures.
TIMEOUTS = {
    "stop": 600, "start": 900, "backup": 6 * 3600, "sync": 900, "sync_nudge": 180,
    "restore": 6 * 3600, "export": 6 * 3600, "import": 12 * 3600,
    "first_hit": 300, "delete": 600,
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
                 source_cluster="", target_cluster=""):
        self.src, self.dst, self.req, self.tid = src, dst, req, tid
        self.emit, self.sleep, self.now = emit, sleep, now
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

def wait_for(ctx, sid, what, fn, timeout, every=5):
    """Attend que `fn()` rende True. Une chaîne est une progression (émise
    quand elle change, pas à chaque tour : le journal d'une action est
    borné), un `Fail` un échec définitif."""
    deadline = ctx.now() + timeout
    last = None
    while True:
        r = fn()
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

def make_backup(ctx, suffix):
    req = ctx.req
    ns = req["vm_ns"]
    name = _short(f"{req['vm_name']}-xfer-{ctx.tid}-{suffix}")
    ctx.emit("backup", "running", f"backup {name}")
    ctx.src.create(vt.backup_manifest(ns, req["vm_name"], name, ctx.tid))
    ctx.record("src", K_BACKUP, ns, name)
    ctx.backups.append((ns, name))

    def ready():
        b = ctx.src.get(K_BACKUP, ns, name) or {}
        st = b.get("status") or {}
        err = (st.get("error") or {}).get("message")
        if err:
            return Fail(f"backup {name}: {err}")
        if st.get("readyToUse"):
            return True
        return f"{st.get('progress', 0)}%"

    wait_for(ctx, "backup", f"backup {name}", ready, ctx.timeouts["backup"])
    ctx.emit("backup", "done", f"backup {name} ready")
    return ns, name


def wait_synced(ctx, ns, name):
    """La sauvegarde doit apparaître sur la cible, par la synchronisation des
    métadonnées de Harvester. Si elle tarde, on la relance en touchant le
    réglage `backup-target` de la cible (une annotation : la valeur, elle,
    ne change pas)."""
    ctx.emit("sync", "running", f"waiting for {name} on {ctx.target_cluster or 'the target'}")
    start = ctx.now()
    nudged = [False]

    def synced():
        b = ctx.dst.get(K_BACKUP, ns, name)
        if b is not None:
            if (b.get("status") or {}).get("readyToUse"):
                return True
            return "synced, not ready yet"
        if not nudged[0] and ctx.now() - start >= ctx.timeouts["sync_nudge"]:
            ctx.dst.patch(K_SETTING, None, "backup-target", {"metadata": {"annotations": {
                RESYNC_ANNOTATION: str(int(ctx.now()))}}})
            nudged[0] = True
            return "resync requested"
        return "not synced yet"

    wait_for(ctx, "sync", f"backup {name} on the target", synced, ctx.timeouts["sync"])
    # l'objet synchronisé est à nous aussi : à nettoyer comme l'original
    ctx.record("dst", K_BACKUP, ns, name)
    ctx.emit("sync", "done", f"{name} visible on the target")


def restore_on_target(ctx, backup_ns, backup_name):
    req = ctx.req
    ns, name = req["namespace"], req["name"]
    keep_mac = _keep_mac(req)
    m = vt.restore_manifest(backup_ns, backup_name, name, ns, keep_mac, ctx.tid)
    rname = m["metadata"]["name"]
    ctx.emit("restore", "running", f"restoring {ns}/{name}")
    ctx.dst.create(m)
    ctx.record("dst", K_RESTORE, ns, rname)
    ctx.restore = (ns, rname)
    ctx.target = (ns, name)

    def complete():
        r = ctx.dst.get(K_RESTORE, ns, rname) or {}
        st = r.get("status") or {}
        if st.get("complete"):
            return True
        err = _cond(r, "Failure")
        if err and err.get("status") == "True":
            return Fail(f"restore: {err.get('message') or err.get('reason')}")
        return f"{st.get('progress', 0)}%"

    wait_for(ctx, "restore", f"restore of {ns}/{name}", complete, ctx.timeouts["restore"])
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
    _vm, running = _source_state(ctx)
    keep_running = req.get("source") == "running"
    if req.get("mode") == "short" and running and not keep_running:
        make_backup(ctx, "a")           # VM en marche : le gros de la copie
    _stop_source_if_needed(ctx, running)
    ns, name = make_backup(ctx, "b" if req.get("mode") == "short" and running and not keep_running else "a")
    if req["namespace"] != ns:
        # la sauvegarde se synchronise dans son namespace d'origine
        ensure_namespace(ctx, "dst", ns)
    if req.get("create_namespace"):
        ensure_namespace(ctx, "dst", req["namespace"])
    wait_synced(ctx, ns, name)
    restore_on_target(ctx, ns, name)
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
    for d, img in exported:
        def imported(img=img):
            obj = ctx.src.get(K_IMAGE, ns, img) or {}
            st = obj.get("status") or {}
            c = _cond(obj, "Imported")
            if c and c.get("status") == "True":
                return True
            if c and c.get("status") == "False" and int(st.get("failed") or 0) >= 3:
                return Fail(f"export of {img}: {c.get('message') or c.get('reason')}")
            return f"{st.get('progress', 0)}%"
        wait_for(ctx, "export", f"export of {d['volume']}", imported, ctx.timeouts["export"])
    ctx.emit("export", "done", f"{len(exported)} disk(s) frozen")
    if running and req.get("source") == "running" and ctx.source_stopped:
        start_vm(ctx, ctx.src, ns, req["vm_name"], ctx.source_initial, sid="restart-source")
        ctx.source_stopped = False
    return exported


def image_stream(ctx, ns, img, attempts=3):
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
    for d, img in exported:
        ctx.emit("download", "running", f"{d['volume']}: downloading")
        info = writer.add_stream(f"disks/{d['volume']}.raw.gz",
                                 image_stream(ctx, req["vm_ns"], img))
        ctx.emit("download", "running",
                 f"{d['volume']}: {info['size'] // (1024 * 1024)} MiB compressed")
    writer.close()
    ctx.emit("download", "done", "archive complete")


def import_disks(ctx, disks, sources):
    """`sources` : {volume: (opener, taille ou None)}. Un DataVolume par
    disque, servi par le guichet ; rend {ancien PVC: nouveau PVC}."""
    req = ctx.req
    ns, name = req["namespace"], req["name"]
    classes = req.get("storage_classes") or {}
    claims, published = {}, []
    try:
        for d in disks:
            opener, size = sources[d["volume"]]
            path = ctx.server.publish(opener, size)
            published.append(path)
            claim = _short(f"{name}-{d['volume']}-{ctx.tid}")
            url = f"http://{ctx.advertise}{path}"
            sc = classes.get(d["storage_class"]) or d["storage_class"]
            ctx.emit("import", "running", f"{d['volume']}: importing into {sc}")
            ctx.dst.create(vt.datavolume_manifest(ns, claim, url, d, sc, ctx.tid))
            ctx.record("dst", K_PVC, ns, claim)
            ctx.record("dst", K_DV, ns, claim)
            claims[d["claim"]] = claim

            started = ctx.now()

            def done(claim=claim, path=path):
                dv = ctx.dst.get(K_DV, ns, claim) or {}
                st = dv.get("status") or {}
                phase = st.get("phase") or ""
                err = ctx.server.error(path)
                if err:
                    return Fail(f"source stream failed: {err}")
                if phase == "Succeeded":
                    return True
                if phase == "Failed":
                    return Fail(f"import failed: {(_cond(dv, 'Running') or {}).get('message') or phase}")
                if (ctx.server.hits(path) == 0
                        and ctx.now() - started >= ctx.timeouts["first_hit"]):
                    msg = (_cond(dv, "Running") or {}).get("message") or phase or "no request"
                    return Fail("the target cluster never fetched the disk from "
                                f"{ctx.advertise} ({msg}): check that its nodes can "
                                "reach this host on that port")
                return f"{phase or 'pending'} {st.get('progress', '')}".strip()

            wait_for(ctx, "import", f"import of {d['volume']}", done, ctx.timeouts["import"])
            # CDI ne ramasse pas le DataVolume, et le volume lui appartient :
            # le supprimer « orphelin » laisse un volume ordinaire (vérifié)
            ctx.dst.delete(K_DV, ns, claim, cascade="orphan")
            ctx.forget("dst", K_DV, ns, claim)
            ctx.emit("import", "running", f"{d['volume']}: imported")
    finally:
        for p in published:
            ctx.server.revoke(p)
    ctx.emit("import", "done", f"{len(claims)} disk(s) imported")
    return claims


def create_target_vm(ctx, clean_vm, secrets, claims):
    req = ctx.req
    ns, name = req["namespace"], req["name"]
    smap = {}
    for s in secrets:
        new = _short(f"{name}-{s['name']}-{ctx.tid}")
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
        delete_quiet(ctx.dst, K_RESTORE, *ctx.restore)
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
        if kind != K_VM:
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
    if ctx.restore and ctx.target:
        ns, name = ctx.target
        r = ctx.dst.get(K_RESTORE, *ctx.restore) or {}
        delete_quiet(ctx.dst, K_RESTORE, *ctx.restore)
        ctx.forget("dst", K_RESTORE, *ctx.restore)
        # la VM et les volumes que la restauration a créés : leur nom était
        # libre au contrôle, ils sont donc à nous
        delete_quiet(ctx.dst, K_VM, ns, name)
        for rs in ((r.get("status") or {}).get("restores") or []):
            pvc = ((rs.get("persistentVolumeClaimSpec") or {}).get("metadata") or {}).get("name")
            if pvc:
                delete_quiet(ctx.dst, K_PVC, ns, pvc)
    for side, kind, ns, name in reversed(list(ctx.created)):
        delete_quiet(ctx.kube(side), kind, ns, name)
    ctx.created.clear()
    if ctx.source_stopped:
        try:
            start_vm(ctx, ctx.src, ctx.req["vm_ns"], ctx.req["vm_name"],
                     ctx.source_initial or "Always", sid="rollback")
            ctx.source_stopped = False
        except TransferError as e:
            ctx.emit("rollback", "error", f"source not restarted: {e}")
    ctx.emit("rollback", "done", "transfer undone, source in its initial state")
