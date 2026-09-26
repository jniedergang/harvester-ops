"""harvester-ops : les objets rangés sous Cluster (v1.57.0).

Storage > Images et Storage Classes, Security > Secrets et SSH Keys, Add-ons :
chaque liste est lue d'un seul appel kubectl groupé, puis mise en regard des
VMs et des volumes pour dire qui s'en sert. Fonctions pures, sans kubectl :
la console leur passe ce qu'elle a lu.

Un Secret n'est JAMAIS rendu avec ses valeurs : seuls le nom de ses clés, son
type et qui l'utilise sortent d'ici.
"""

import json

KINDS = ("images", "storageclasses", "sshkeys", "secrets", "addons")

# Ce que la console lit pour chaque vue (un seul `kubectl get` groupé), et ce
# qu'il lui faut en plus pour dire qui s'en sert.
FETCH = {
    "images": "virtualmachineimages.harvesterhci.io",
    "storageclasses": "storageclasses.storage.k8s.io",
    "sshkeys": "keypairs.harvesterhci.io",
    "secrets": "secrets",
    "addons": "addons.harvesterhci.io",
}
NEEDS_USAGE = {"images": ("vm", "pvc"), "storageclasses": ("pvc", "vmimage"),
               "sshkeys": ("vm",), "secrets": ("vm",), "addons": ()}

# Secrets de fonctionnement du cluster lui-même : cachés par défaut (plus de
# 250 sur harv1, contre une poignée écrite par des personnes).
SYSTEM_SECRET_TYPES = ("kubernetes.io/service-account-token", "helm.sh/release.v1",
                       "helmcharts.helm.cattle.io/values", "bootstrap.kubernetes.io/token",
                       "rke.cattle.io/", "rke2.cattle.io/", "fleet.cattle.io/",
                       "cluster.x-k8s.io/secret")
SYSTEM_NAMESPACE_PREFIXES = ("kube-", "cattle-", "fleet-", "harvester-system", "longhorn-system",
                             "caaph-",
                             "cert-manager", "capi-", "caphv-", "rke2-", "local",
                             "harvester-public", "kube-ovn")


def _meta(o):
    return (o or {}).get("metadata") or {}


def _ref(o):
    m = _meta(o)
    return f"{m.get('namespace')}/{m.get('name')}" if m.get("namespace") else m.get("name")


def _created(o):
    return _meta(o).get("creationTimestamp")


def _vm_spec(vm):
    return (((vm.get("spec") or {}).get("template") or {}).get("spec")) or {}


def vm_claims(vms):
    """{ns/pvc: [vm refs]} : les volumes que chaque VM porte."""
    out = {}
    for vm in vms:
        ns = _meta(vm).get("namespace")
        for v in _vm_spec(vm).get("volumes") or []:
            claim = ((v.get("persistentVolumeClaim") or {}).get("claimName")
                     or (v.get("dataVolume") or {}).get("name"))
            if claim:
                out.setdefault(f"{ns}/{claim}", []).append(_ref(vm))
    return out


def _state_from_conditions(status):
    """ready | failed | in progress, et le message d'échec d'une image."""
    conds = (status or {}).get("conditions") or []
    for c in conds:
        if c.get("type") == "RetryLimitExceeded" and str(c.get("status")) == "True":
            return "failed", c.get("message") or c.get("reason") or ""
    for c in conds:
        if c.get("type") == "Imported" and str(c.get("status")) == "False" and c.get("message"):
            return "failed", c.get("message")
    if (status or {}).get("progress") == 100 and any(
            c.get("type") == "Imported" and str(c.get("status")) == "True" for c in conds):
        return "ready", ""
    return "importing", ""


def images(items, vms=(), pvcs=()):
    claims = vm_claims(vms)
    by_image = {}
    for p in pvcs:
        img = (_meta(p).get("annotations") or {}).get("harvesterhci.io/imageId")
        if img:
            by_image.setdefault(img, []).append(p)
    out = []
    for it in items:
        spec, status = it.get("spec") or {}, it.get("status") or {}
        ref = _ref(it)
        used = sorted({vm for p in by_image.get(ref, []) for vm in claims.get(_ref(p), [])})
        state, message = _state_from_conditions(status)
        out.append({
            "namespace": _meta(it).get("namespace"), "name": _meta(it).get("name"),
            "display_name": spec.get("displayName") or _meta(it).get("name"),
            "source_type": spec.get("sourceType"), "url": spec.get("url") or None,
            "backend": spec.get("backend"), "size": status.get("size"),
            "virtual_size": status.get("virtualSize"), "progress": status.get("progress"),
            "storage_class": status.get("storageClassName"), "state": state, "message": message,
            "volumes": len(by_image.get(ref, [])), "used_by": used, "created": _created(it),
        })
    return out


def storage_classes(items, pvcs=(), vmimages=()):
    count = {}
    for p in pvcs:
        sc = (p.get("spec") or {}).get("storageClassName")
        if sc:
            count[sc] = count.get(sc, 0) + 1
    image_of = {}
    for im in vmimages:
        sc = (im.get("status") or {}).get("storageClassName")
        if sc:
            image_of[sc] = {"ref": _ref(im), "display_name": (im.get("spec") or {}).get("displayName")}
    out = []
    for it in items:
        m = _meta(it)
        params = it.get("parameters") or {}
        ann = m.get("annotations") or {}
        out.append({
            "name": m.get("name"), "provisioner": it.get("provisioner"),
            "is_default": ann.get("storageclass.kubernetes.io/is-default-class") == "true",
            "reclaim_policy": it.get("reclaimPolicy"), "binding": it.get("volumeBindingMode"),
            "expansion": bool(it.get("allowVolumeExpansion")),
            "replicas": params.get("numberOfReplicas"),
            "stale_timeout": params.get("staleReplicaTimeout"),
            "migratable": params.get("migratable"),
            "disk_selector": params.get("diskSelector") or None,
            "node_selector": params.get("nodeSelector") or None,
            "data_locality": params.get("dataLocality") or None,
            "image": image_of.get(m.get("name")), "volumes": count.get(m.get("name"), 0),
            "parameters": params, "created": _created(it),
        })
    return out


def _vm_ssh_names(vm):
    raw = (_meta(vm).get("annotations") or {}).get("harvesterhci.io/sshNames")
    try:
        names = json.loads(raw) if raw else []
    except ValueError:
        return []
    ns = _meta(vm).get("namespace")
    return [n if "/" in n else f"{ns}/{n}" for n in names if isinstance(n, str)]


def ssh_keys(items, vms=()):
    users = {}
    for vm in vms:
        for n in _vm_ssh_names(vm):
            users.setdefault(n, []).append(_ref(vm))
    out = []
    for it in items:
        status = it.get("status") or {}
        conds = status.get("conditions") or []
        valid = any(c.get("type") == "validated" and str(c.get("status")) == "True" for c in conds)
        out.append({
            "namespace": _meta(it).get("namespace"), "name": _meta(it).get("name"),
            "fingerprint": status.get("fingerPrint"),
            "public_key": (it.get("spec") or {}).get("publicKey"),
            "validated": valid, "used_by": sorted(users.get(_ref(it), [])),
            "created": _created(it),
        })
    return out


def _vm_secret_refs(vm):
    ns = _meta(vm).get("namespace")
    refs = []
    for v in _vm_spec(vm).get("volumes") or []:
        for src in ("cloudInitNoCloud", "cloudInitConfigDrive"):
            ci = v.get(src) or {}
            for k in ("secretRef", "networkDataSecretRef"):
                name = (ci.get(k) or {}).get("name")
                if name:
                    refs.append(f"{ns}/{name}")
    return refs


def is_system_secret(item):
    t = item.get("type") or ""
    ns = _meta(item).get("namespace") or ""
    return t.startswith(SYSTEM_SECRET_TYPES) or ns.startswith(SYSTEM_NAMESPACE_PREFIXES)


def secrets(items, vms=(), include_system=False):
    users = {}
    for vm in vms:
        for r in _vm_secret_refs(vm):
            users.setdefault(r, []).append(_ref(vm))
    out, hidden = [], 0
    for it in items:
        system = is_system_secret(it)
        if system and not include_system:
            hidden += 1
            continue
        labels = _meta(it).get("labels") or {}
        keys = sorted(set((it.get("data") or {}).keys()) | set((it.get("stringData") or {}).keys()))
        out.append({
            "namespace": _meta(it).get("namespace"), "name": _meta(it).get("name"),
            "type": it.get("type"), "keys": keys, "system": system,
            "cloud_init": "harvesterhci.io/cloud-init-template" in labels,
            "used_by": sorted(set(users.get(_ref(it), []))), "created": _created(it),
        })
    return out, hidden


def addons(items):
    out = []
    for it in items:
        spec, status = it.get("spec") or {}, it.get("status") or {}
        failed = ""
        for c in status.get("conditions") or []:
            if c.get("type") == "OperationFailed" and str(c.get("status")) == "True":
                failed = c.get("message") or c.get("reason") or ""
        out.append({
            "namespace": _meta(it).get("namespace"), "name": _meta(it).get("name"),
            "chart": spec.get("chart"), "version": spec.get("version"),
            "enabled": bool(spec.get("enabled")), "status": status.get("status") or "",
            "message": failed, "created": _created(it),
        })
    return out
