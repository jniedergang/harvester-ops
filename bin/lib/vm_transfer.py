"""harvester-ops : décisions du transfert de VM entre clusters (v1.45.0).

Ce module ne parle à aucun cluster. Il reçoit des objets déjà lus (la VM,
ses volumes, les faits relevés sur chaque cluster) et décide :

* ce que la VM emporte (inventaire) et ce qu'il faut retirer de son
  manifeste pour qu'il vaille ailleurs (nettoyage, réécriture) ;
* quelles correspondances proposer pour ses réseaux et ses classes de
  stockage ;
* si le transfert est possible, et pourquoi pas (contrôle préalable) ;
* quel moteur employer : la cible de sauvegarde commune de Harvester si les
  deux clusters la partagent, sinon la copie qui passe par la console ;
* comment écrire et relire l'archive d'export.

Importé par `bin/harvester-vm-transfer.py` et par la console : bibliothèque
standard seulement, le script doit tourner sur un hôte airgap.
"""

import copy
import hashlib
import json
import os
import re
import sys
import tarfile
import time

TRANSFER_LABEL = "harvester-ops.io/transfer"
TRANSFERRED_TO = "harvester-ops.io/transferred-to"
FORMAT = 1
ARCHIVE_SUFFIX = ".hvx"
MANIFEST = "manifest.json"
SUMS = "SHA256SUMS"

# Harvester ramène les images avec une sauvegarde depuis la 1.4 seulement.
IMAGE_SYNC_SINCE = (1, 4, 0)

_BLOCK = 512

# Annotations posées par des contrôleurs, ou qui décrivent l'état d'un
# cluster précis : recopiées ailleurs, elles mentiraient (adresses IP,
# MAC attribuées, modèles de volumes déjà créés) ou bloqueraient le webhook.
_DROP_ANNOTATION_PREFIXES = ("kubevirt.io/", "kubectl.kubernetes.io/")
_DROP_ANNOTATIONS = {
    "harvesterhci.io/volumeClaimTemplates",
    "harvesterhci.io/mac-address",
    "harvesterhci.io/vmRunStrategy",
    "network.harvesterhci.io/ips",
    TRANSFERRED_TO,
}
_DROP_METADATA = ("uid", "resourceVersion", "creationTimestamp", "generation",
                  "managedFields", "ownerReferences", "finalizers",
                  "deletionTimestamp", "deletionGracePeriodSeconds", "selfLink")
_HOSTNAME_KEY = "kubernetes.io/hostname"
_NETWORK_AFFINITY_PREFIX = "network.harvesterhci.io/"


# ---------------------------------------------------------------------------
# Petits outils
# ---------------------------------------------------------------------------

def step(sid, status, msg=""):
    """Une étape pour la chaîne SSE de la console : une seule ligne sur
    stderr, au format lu par l'interface."""
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def version_tuple(v):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


_QUANTITY = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4,
             "Pi": 1024 ** 5, "k": 10 ** 3, "K": 10 ** 3, "M": 10 ** 6,
             "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15}


def parse_quantity(q):
    """Quantité Kubernetes (`10Gi`, `1G`, `1073741824`) en octets."""
    if q is None:
        return 0
    m = re.match(r"^\s*([0-9.]+)\s*([A-Za-z]*)\s*$", str(q))
    if not m:
        return 0
    return int(float(m.group(1)) * _QUANTITY.get(m.group(2), 1))


def normalize_backup_target(raw):
    """Valeur du réglage `backup-target` réduite à ce qui désigne le lieu.

    Deux clusters « partagent » leur cible quand ces tuples sont égaux. Les
    secrets d'accès n'entrent jamais dans la comparaison (ni dans une
    réponse)."""
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    kind = (raw.get("type") or "").lower()
    if kind == "nfs":
        ep = (raw.get("endpoint") or "").strip()
        if ep.startswith("nfs://"):
            ep = ep[len("nfs://"):]
        host, sep, path = ep.partition(":")
        if not sep or not host:
            return None
        return ("nfs", f"{host.lower()}:{path.rstrip('/') or '/'}")
    if kind == "s3":
        return ("s3", (raw.get("endpoint") or "").rstrip("/"),
                raw.get("bucketName") or "", raw.get("bucketRegion") or "")
    return None


# ---------------------------------------------------------------------------
# Inventaire, nettoyage, réécriture
# ---------------------------------------------------------------------------

def _tspec(vm):
    return ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}


def _node_terms(tspec):
    aff = (tspec.get("affinity") or {}).get("nodeAffinity") or {}
    req = aff.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    return req.get("nodeSelectorTerms") or []


def _pinned_to_node(tspec):
    if tspec.get("nodeSelector"):
        return True
    for term in _node_terms(tspec):
        for expr in term.get("matchExpressions") or []:
            if expr.get("key") == _HOSTNAME_KEY:
                return True
    return False


def _devices(tspec):
    devs = ((tspec.get("domain") or {}).get("devices")) or {}
    out = [f"hostDevice:{d.get('deviceName')}" for d in devs.get("hostDevices") or []]
    out += [f"gpu:{d.get('deviceName')}" for d in devs.get("gpus") or []]
    return out


def _cloudinit_secrets(tspec):
    names = []
    for vol in tspec.get("volumes") or []:
        for key in ("cloudInitNoCloud", "cloudInitConfigDrive"):
            ci = vol.get(key) or {}
            for ref in ("secretRef", "userDataSecretRef", "networkDataSecretRef"):
                n = (ci.get(ref) or {}).get("name")
                if n and n not in names:
                    names.append(n)
    return names


def vm_macs(vm):
    """Adresses MAC d'une VM : celles de sa spec et celles que le webhook de
    Harvester a notées dans son annotation. En minuscules."""
    out = set()
    for iface in ((_tspec(vm).get("domain") or {}).get("devices") or {}).get("interfaces") or []:
        if iface.get("macAddress"):
            out.add(iface["macAddress"].lower())
    try:
        ann = json.loads(((vm.get("metadata") or {}).get("annotations") or {})
                         .get("harvesterhci.io/mac-address") or "{}")
    except ValueError:
        ann = {}
    if isinstance(ann, dict):
        out.update(str(v).lower() for v in ann.values() if v)
    return out


def vm_inventory(vm, pvcs, used=None):
    """Ce que la VM emporte : disques, réseaux, secrets cloud-init,
    périphériques, épinglage à un nœud, état.

    `pvcs` : {nom du PVC: objet PVC}. `used` : {nom du PVC: octets réellement
    occupés} si connu (taille réelle Longhorn), pour estimer une archive."""
    tspec = _tspec(vm)
    disks = []
    for vol in tspec.get("volumes") or []:
        claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
        if not claim:
            continue
        p = pvcs.get(claim) or {}
        ps = p.get("spec") or {}
        ann = (p.get("metadata") or {}).get("annotations") or {}
        disks.append({
            "volume": vol.get("name"),
            "claim": claim,
            "size": parse_quantity(((ps.get("resources") or {}).get("requests") or {}).get("storage")),
            "storage_class": ps.get("storageClassName"),
            "access_modes": list(ps.get("accessModes") or ["ReadWriteMany"]),
            "volume_mode": ps.get("volumeMode") or "Block",
            "image": ann.get("harvesterhci.io/imageId") or None,
            "used": (used or {}).get(claim),
        })
    networks = [(n.get("multus") or {}).get("networkName") for n in tspec.get("networks") or []
                if (n.get("multus") or {}).get("networkName")]
    spec = vm.get("spec") or {}
    run_strategy = spec.get("runStrategy") or ("Always" if spec.get("running") else "Halted")
    status = vm.get("status") or {}
    return {
        "disks": disks,
        "networks": networks,
        "macs": sorted(vm_macs(sanitize_vm(vm))),
        "secrets": _cloudinit_secrets(tspec),
        "devices": _devices(tspec),
        "node_affinity": _pinned_to_node(tspec),
        "run_strategy": run_strategy,
        "running": bool(status.get("ready")) or status.get("printableStatus") == "Running",
    }


def sanitize_vm(vm):
    """Copie de la VM sans ce qui décrit son cluster d'origine.

    Les adresses MAC que le webhook de Harvester n'a notées que dans
    l'annotation `harvesterhci.io/mac-address` (VM créée hors de son
    interface) sont recopiées dans les interfaces avant que l'annotation
    disparaisse : c'est ce qui permet de les garder sur la cible."""
    out = {k: copy.deepcopy(v) for k, v in vm.items() if k != "status"}
    md = out.setdefault("metadata", {})
    try:
        macs = json.loads((md.get("annotations") or {}).get("harvesterhci.io/mac-address") or "{}")
    except ValueError:
        macs = {}
    if isinstance(macs, dict):
        for iface in ((_tspec(out).get("domain") or {}).get("devices") or {}).get("interfaces") or []:
            if not iface.get("macAddress") and macs.get(iface.get("name")):
                iface["macAddress"] = macs[iface["name"]]
    for k in _DROP_METADATA:
        md.pop(k, None)
    ann = {k: v for k, v in (md.get("annotations") or {}).items()
           if k not in _DROP_ANNOTATIONS and not k.startswith(_DROP_ANNOTATION_PREFIXES)}
    if ann:
        md["annotations"] = ann
    else:
        md.pop("annotations", None)
    labels = {k: v for k, v in (md.get("labels") or {}).items() if k != TRANSFER_LABEL}
    if labels:
        md["labels"] = labels
    else:
        md.pop("labels", None)
    return out


def retarget_vm(vm, *, name, namespace, networks, claims, secrets, keep_mac, transfer_id):
    """La VM nettoyée, réécrite pour la cible. Rend (vm, retraits).

    `networks`, `claims`, `secrets` : correspondances nom source -> nom
    cible. Les retraits sont ce qui ne peut pas voyager (périphériques d'un
    hôte, épinglage à un nœud) : l'assistant les a annoncés."""
    vm = copy.deepcopy(vm)
    removed = []
    md = vm.setdefault("metadata", {})
    md["name"], md["namespace"] = name, namespace
    md.setdefault("labels", {})[TRANSFER_LABEL] = transfer_id
    spec = vm.setdefault("spec", {})
    spec.pop("running", None)
    spec["runStrategy"] = "Halted"
    tmpl = spec.setdefault("template", {})
    tmd = tmpl.setdefault("metadata", {})
    tmd.setdefault("labels", {})["harvesterhci.io/vmName"] = name
    tspec = tmpl.setdefault("spec", {})

    for n in tspec.get("networks") or []:
        mul = n.get("multus")
        if mul and mul.get("networkName") in networks:
            mul["networkName"] = networks[mul["networkName"]]

    for vol in tspec.get("volumes") or []:
        pvc = vol.get("persistentVolumeClaim")
        if pvc and pvc.get("claimName") in claims:
            pvc["claimName"] = claims[pvc["claimName"]]
        for key in ("cloudInitNoCloud", "cloudInitConfigDrive"):
            ci = vol.get(key) or {}
            for ref in ("secretRef", "userDataSecretRef", "networkDataSecretRef"):
                r = ci.get(ref)
                if r and r.get("name") in secrets:
                    r["name"] = secrets[r["name"]]

    devs = (tspec.get("domain") or {}).get("devices") or {}
    if not keep_mac:
        for iface in devs.get("interfaces") or []:
            iface.pop("macAddress", None)
    removed += _devices(tspec)
    devs.pop("hostDevices", None)
    devs.pop("gpus", None)

    if tspec.pop("nodeSelector", None):
        removed.append("node-selector")
    aff = tspec.get("affinity") or {}
    na = aff.get("nodeAffinity") or {}
    req = na.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    terms, pinned = [], False
    for term in req.get("nodeSelectorTerms") or []:
        exprs = []
        for e in term.get("matchExpressions") or []:
            key = e.get("key") or ""
            if key == _HOSTNAME_KEY:
                pinned = True
                continue
            if key.startswith(_NETWORK_AFFINITY_PREFIX):
                continue          # recalculée par le webhook d'après les réseaux
            exprs.append(e)
        if exprs or term.get("matchFields"):
            t = dict(term)
            t["matchExpressions"] = exprs
            terms.append(t)
    if pinned:
        removed.append("node-affinity")
    if req:
        if terms:
            req["nodeSelectorTerms"] = terms
        else:
            na.pop("requiredDuringSchedulingIgnoredDuringExecution", None)
            if not na:
                aff.pop("nodeAffinity", None)
            if not aff:
                tspec.pop("affinity", None)
    return vm, removed


# ---------------------------------------------------------------------------
# Manifestes
# ---------------------------------------------------------------------------

def _meta(name, namespace, transfer_id, annotations=None):
    md = {"name": name, "namespace": namespace, "labels": {TRANSFER_LABEL: transfer_id}}
    if annotations:
        md["annotations"] = annotations
    return md


def restore_supports_halt(crd):
    """La CRD de restauration de la cible connaît-elle `haltAfterRestore` ?
    Lu dans son schéma : la 1.8 le refuse en décodage strict."""
    for ver in ((crd or {}).get("spec") or {}).get("versions") or []:
        props = ((((ver.get("schema") or {}).get("openAPIV3Schema") or {})
                  .get("properties") or {}).get("spec") or {}).get("properties") or {}
        if "haltAfterRestore" in props:
            return True
    return False


def backup_manifest(namespace, vm_name, name, transfer_id):
    return {"apiVersion": "harvesterhci.io/v1beta1", "kind": "VirtualMachineBackup",
            "metadata": _meta(name, namespace, transfer_id),
            "spec": {"type": "backup",
                     "source": {"apiGroup": "kubevirt.io", "kind": "VirtualMachine",
                                "name": vm_name}}}


def restore_manifest(backup_ns, backup_name, name, namespace, keep_mac, transfer_id,
                     halt=True):
    """`halt` : la cible connaît `haltAfterRestore` (Harvester 1.9 ; la 1.8
    le refuse, « unknown field », relevé sur harvlab2)."""
    spec = {"newVM": True,
            # retain : supprimer l'objet de restauration après coup ne doit
            # pas emporter la VM restaurée (même réglage que les
            # restaurations d'instantané de la console)
            "deletionPolicy": "retain",
            "keepMacAddress": bool(keep_mac),
            "target": {"apiGroup": "kubevirt.io", "kind": "VirtualMachine", "name": name},
            "virtualMachineBackupNamespace": backup_ns,
            "virtualMachineBackupName": backup_name}
    if halt:
        spec["haltAfterRestore"] = True
    return {"apiVersion": "harvesterhci.io/v1beta1", "kind": "VirtualMachineRestore",
            "metadata": _meta(f"{name}-{transfer_id}", namespace, transfer_id),
            "spec": spec}


def export_image_manifest(namespace, pvc, image_name, transfer_id, target_sc):
    return {"apiVersion": "harvesterhci.io/v1beta1", "kind": "VirtualMachineImage",
            "metadata": _meta(image_name, namespace, transfer_id),
            "spec": {"displayName": f"{image_name}.raw",
                     "sourceType": "export-from-volume",
                     "pvcName": pvc, "pvcNamespace": namespace,
                     "targetStorageClassName": target_sc,
                     # une seule réplique : l'image ne vit que le temps du
                     # téléchargement
                     "storageClassParameters": {"numberOfReplicas": "1",
                                                "staleReplicaTimeout": "30",
                                                "migratable": "true"},
                     "retry": 3}}


def datavolume_manifest(namespace, name, url, disk, storage_class, transfer_id):
    return {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
            "metadata": _meta(name, namespace, transfer_id, {
                # sans cela CDI attend le premier consommateur, qui n'existe
                # pas encore : la VM est créée après l'import
                "cdi.kubevirt.io/storage.bind.immediate.requested": "true"}),
            "spec": {"source": {"http": {"url": url}},
                     "storage": {"storageClassName": storage_class,
                                 "accessModes": list(disk.get("access_modes") or ["ReadWriteMany"]),
                                 "volumeMode": disk.get("volume_mode") or "Block",
                                 "resources": {"requests": {"storage": str(int(disk["size"]))}}}}}


def secret_manifest(secret, name, namespace, transfer_id):
    return {"apiVersion": "v1", "kind": "Secret",
            "metadata": _meta(name, namespace, transfer_id),
            "type": secret.get("type") or "Opaque",
            "data": dict(secret.get("data") or {})}


def namespace_manifest(name, transfer_id):
    return {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": name, "labels": {TRANSFER_LABEL: transfer_id}}}


# ---------------------------------------------------------------------------
# Correspondances, moteur, contrôle
# ---------------------------------------------------------------------------

def default_mappings(inv, target, engine):
    """Correspondances proposées : même nom s'il existe sur la cible.

    Moteur fichier : un disque né d'une image arrive entier, il va dans la
    classe par défaut de la cible (la classe d'image n'y existe pas, et
    n'aurait aucun sens pour un disque complet)."""
    nets = {n: (n if n in (target.get("networks") or []) else None) for n in inv["networks"]}
    classes = target.get("storage_classes") or {}
    default = target.get("default_storage_class")
    scs = {}
    for d in inv["disks"]:
        sc = d["storage_class"]
        if engine == "file" and d.get("image"):
            scs[sc] = default
        elif sc in classes and not classes[sc].get("image"):
            scs[sc] = sc
        else:
            scs[sc] = default
    return {"networks": nets, "storage_classes": scs}


def choose_engine(src, dst, req):
    if req.get("kind") in ("export", "import"):
        return "file", "file-requested"
    if req.get("engine") == "file":
        return "file", "forced"
    s, d = src.get("backup_target"), (dst or {}).get("backup_target")
    if not s or not src.get("backup_target_ok"):
        return "file", "source-no-target"
    if not d or not dst.get("backup_target_ok"):
        return "file", "target-no-target"
    if tuple(s) != tuple(d):
        return "file", "different-targets"
    return "backup", "shared-target"


def _finding(code, level, **facts):
    return {"code": code, "level": level, "facts": facts}


def blocking(findings):
    return any(f["level"] == "block" for f in findings)


def check(src, dst, req):
    """Le contrôle préalable : une liste de constats, les blocages d'abord.

    `src` : faits de la source (ou du manifeste d'une archive à importer) ;
    `dst` : faits de la cible, `None` pour un export ; `req` : la demande."""
    kind = req.get("kind", "migrate")
    out = []
    if kind != "export":
        if not dst or not dst.get("reachable"):
            return [_finding("target-unreachable", "block",
                             cluster=(dst or {}).get("cluster"))]
        if not dst.get("kubevirt"):
            out.append(_finding("kubevirt-missing", "block", cluster=dst.get("cluster")))
    engine, reason = choose_engine(src, dst, req)
    out.append(_finding("engine", "ok", engine=engine, reason=reason))
    inv = src["inventory"]

    if kind != "export":
        if version_tuple(dst.get("version")) < version_tuple(src.get("version")):
            out.append(_finding("version-older", "warn", source=src.get("version"),
                                target=dst.get("version")))
        ns = req.get("namespace")
        if ns not in (dst.get("namespaces") or []):
            out.append(_finding("namespace-missing",
                                "warn" if req.get("create_namespace") else "block",
                                namespace=ns, create=bool(req.get("create_namespace"))))
        if req.get("name") in (dst.get("vm_names") or []):
            out.append(_finding("vm-name-taken", "block", name=req.get("name"), namespace=ns))
        nets = req.get("networks") or {}
        for n in inv["networks"]:
            m = nets.get(n)
            if not m or m not in (dst.get("networks") or []):
                out.append(_finding("network-unmapped", "block", network=n, mapped=m))
        classes = dst.get("storage_classes") or {}
        needed = {}
        for d in inv["disks"]:
            sc = d["storage_class"]
            if engine == "file":
                m = (req.get("storage_classes") or {}).get(sc)
                if not m or m not in classes:
                    out.append(_finding("storage-class-unmapped", "block",
                                        storage_class=sc, mapped=m))
                    continue
                target_sc = m
            elif d.get("image"):
                # la classe d'image sera recréée par Harvester ; la place se
                # juge sur la classe par défaut, faute de mieux
                target_sc = dst.get("default_storage_class")
                if target_sc not in classes:
                    continue
            else:
                if sc not in classes:
                    out.append(_finding("storage-class-missing-backup", "block",
                                        storage_class=sc))
                    continue
                target_sc = sc
            needed[target_sc] = needed.get(target_sc, 0) + d["size"]
        for sc, n in sorted(needed.items()):
            info = classes.get(sc) or {}
            alloc = int(info.get("allocatable") or 0)
            # plus de répliques demandées que de nœuds : Longhorn crée le
            # volume quand même, dégradé (le quotidien d'un site mononœud)
            if not alloc and info.get("degraded_allocatable"):
                out.append(_finding("replicas-degraded", "warn", storage_class=sc,
                                    replicas=info.get("replicas"), nodes=info.get("nodes")))
                alloc = int(info["degraded_allocatable"])
            if n > alloc:
                out.append(_finding("capacity-short", "block", storage_class=sc,
                                    needed=n, allocatable=alloc))

        # Harvester refuse deux VMs avec la même MAC sur un réseau de cluster,
        # même arrêtée (vécu : la source d'origine, gardée arrêtée, bloquait
        # le retour de sa copie)
        keep = req.get("keep_mac", True) is not False and req.get("source") != "running"
        taken = dst.get("macs") or {}
        clash = sorted(m for m in inv.get("macs") or [] if m in taken)
        if keep and clash:
            out.append(_finding("mac-in-use", "block", macs=", ".join(clash),
                                vms=", ".join(sorted({taken[m] for m in clash}))))

    if inv.get("devices"):
        out.append(_finding("devices-removed", "warn", devices=list(inv["devices"])))
    if inv.get("node_affinity"):
        out.append(_finding("node-affinity-removed", "warn"))

    if engine == "backup":
        dst_images = dst.get("images") or {}
        by_display = {v.get("display"): k for k, v in dst_images.items()}
        src_images = src.get("images") or {}
        for img in sorted({d["image"] for d in inv["disks"] if d.get("image")}):
            info = src_images.get(img)
            if info is None:
                out.append(_finding("image-missing-source", "warn", image=img))
                continue
            there = dst_images.get(img)
            other = by_display.get(info.get("display"))
            if there is not None:
                if (there.get("size"), there.get("virtual_size")) != \
                        (info.get("size"), info.get("virtual_size")):
                    out.append(_finding("image-conflict", "block", image=img, existing=img))
            elif other is not None:
                out.append(_finding("image-conflict", "block", image=img, existing=other))
            elif version_tuple(dst.get("version")) < IMAGE_SYNC_SINCE:
                out.append(_finding("image-sync-unsupported", "block", image=img,
                                    target=dst.get("version")))
    else:
        if kind in ("migrate", "import") and not dst.get("cdi"):
            out.append(_finding("cdi-missing", "block", cluster=dst.get("cluster")))
        if kind in ("migrate", "export"):
            total = sum(d["size"] for d in inv["disks"])
            if total > int(src.get("room_one") or 0):
                out.append(_finding("source-room-short", "block", needed=total,
                                    allocatable=int(src.get("room_one") or 0)))

    if kind == "export":
        if req.get("store_free") is not None:
            est = sum(d.get("used") or d["size"] for d in inv["disks"])
            if est > req["store_free"]:
                out.append(_finding("store-room-short", "block", needed=est,
                                    free=req["store_free"]))
        if inv.get("secrets"):
            out.append(_finding("secrets-in-archive", "warn", count=len(inv["secrets"])))

    if req.get("mode") == "short" and engine != "backup" and kind == "migrate":
        out.append(_finding("short-mode-needs-backup", "block"))
    if kind == "migrate" and req.get("source") == "running":
        out.append(_finding("hostname-duplicate", "warn"))

    order = {"block": 0, "warn": 1, "ok": 2}
    return sorted(out, key=lambda f: order[f["level"]])


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def _tar_header(name, size, mtime):
    """En-tête tar de 512 octets exactement. Format GNU : au-delà de 8 Gio la
    taille s'écrit en base 256 DANS le même bloc, ce qui permet de réécrire
    l'en-tête après coup, une fois la taille connue."""
    ti = tarfile.TarInfo(name)
    ti.size = int(size)
    ti.mtime = int(mtime)
    ti.mode = 0o600
    ti.type = tarfile.REGTYPE
    buf = ti.tobuf(format=tarfile.GNU_FORMAT, encoding="utf-8", errors="surrogateescape")
    if len(buf) != _BLOCK:
        raise ValueError(f"member name too long for the archive: {name}")
    return buf


class ArchiveWriter:
    """Archive tar écrite en flux : manifeste, disques, sommes de contrôle.

    La taille d'un disque compressé n'est connue qu'à la fin : l'en-tête est
    écrit avec une taille nulle, puis réécrit. Le fichier est créé en 0600
    (il contient les secrets cloud-init) et jamais par-dessus un autre."""

    def __init__(self, path):
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.path = path
        self.f = os.fdopen(fd, "wb")
        self.sums = []

    def _pad(self, size):
        rest = size % _BLOCK
        if rest:
            self.f.write(b"\0" * (_BLOCK - rest))

    def add_bytes(self, name, data):
        self.f.write(_tar_header(name, len(data), time.time()))
        self.f.write(data)
        self._pad(len(data))
        self.sums.append((hashlib.sha256(data).hexdigest(), name))

    def add_json(self, name, obj):
        self.add_bytes(name, json.dumps(obj, indent=1, sort_keys=True).encode())

    def add_stream(self, name, chunks):
        pos = self.f.tell()
        now = time.time()
        self.f.write(_tar_header(name, 0, now))
        h, size = hashlib.sha256(), 0
        for chunk in chunks:
            if not chunk:
                continue
            self.f.write(chunk)
            h.update(chunk)
            size += len(chunk)
        self._pad(size)
        end = self.f.tell()
        self.f.seek(pos)
        self.f.write(_tar_header(name, size, now))
        self.f.seek(end)
        digest = h.hexdigest()
        self.sums.append((digest, name))
        return {"size": size, "sha256": digest}

    def close(self):
        body = "".join(f"{d}  {n}\n" for d, n in self.sums).encode()
        self.add_bytes(SUMS, body)
        self.f.write(b"\0" * (2 * _BLOCK))
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()


class _Member:
    """Lecture bornée d'un membre de l'archive."""

    def __init__(self, path, offset, size):
        self.f = open(path, "rb")
        self.f.seek(offset)
        self.left = size

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        if n is None or n < 0 or n > self.left:
            n = self.left
        data = self.f.read(n)
        self.left -= len(data)
        return data

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ArchiveReader:
    """Parcourt les en-têtes sans lire les disques. Tolère une archive
    coupée (export interrompu) : elle est alors `complete = False`."""

    def __init__(self, path):
        self.path = path
        self.members = {}
        with open(path, "rb") as f:
            total = os.fstat(f.fileno()).st_size
            pos = 0
            while pos + _BLOCK <= total:
                f.seek(pos)
                buf = f.read(_BLOCK)
                if buf == b"\0" * _BLOCK:
                    break
                try:
                    ti = tarfile.TarInfo.frombuf(buf, "utf-8", "surrogateescape")
                except tarfile.TarError:
                    break
                data = pos + _BLOCK
                if data + ti.size > total:
                    break
                self.members[ti.name] = (data, ti.size)
                pos = data + ((ti.size + _BLOCK - 1) // _BLOCK) * _BLOCK
        self.complete = SUMS in self.members

    def member(self, name):
        return self.members[name]

    def open_member(self, name):
        off, size = self.members[name]
        return _Member(self.path, off, size)

    def read_member(self, name):
        with self.open_member(name) as m:
            return m.read()

    def manifest(self):
        return json.loads(self.read_member(MANIFEST))

    def sums(self):
        out = {}
        if SUMS in self.members:
            for line in self.read_member(SUMS).decode().splitlines():
                digest, _, name = line.partition("  ")
                if name:
                    out[name] = digest
        return out

    def verify(self):
        bad = []
        for name, digest in self.sums().items():
            if name not in self.members:
                bad.append(name)
                continue
            h = hashlib.sha256()
            with self.open_member(name) as m:
                while True:
                    chunk = m.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
            if h.hexdigest() != digest:
                bad.append(name)
        return bad
