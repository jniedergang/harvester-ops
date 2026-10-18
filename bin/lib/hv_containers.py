"""harvester-ops : les charges de conteneurs d'un cluster Harvester (v1.91.0).

Harvester 1.9.1 passe en GA les conteneurs bare-metal : des charges
Kubernetes ordinaires (Deployments, StatefulSets, DaemonSets, Jobs, CronJobs,
pods seuls) déployées sur le cluster, à côté des VM. La console les montre
comme elle montre les VM, en lecture pour commencer.

Fonctions pures, sans entrée-sortie : elles reçoivent les objets lus par
kubectl (une `List` par type) et rendent une ligne par charge, ses pods
rangés dessous. Ce qui n'est pas une charge de conteneurs est écarté :

- les namespaces système (`SYSTEM_NAMESPACES`), où Harvester, Rancher,
  Longhorn et Fleet font tourner les leurs ; `default` n'en est pas ;
- les pods des VM (`kubevirt.io=virt-launcher`, et les pods de disques
  branchés à chaud, propriété d'une VMI) ;
- les pods des imports et transferts de CDI (`cdi.kubevirt.io`).

Un pod qui appartient à une charge (ownerReferences) est rangé sous elle :
pod -> ReplicaSet -> Deployment, pod -> Job -> CronJob. Un pod seul est une
charge à lui.
"""

from datetime import datetime, timezone
from fnmatch import fnmatchcase

# Namespaces masqués : ceux de Kubernetes, Rancher, Harvester, Longhorn, Fleet
# et CDI. Une seule liste, partagée par la console et la ligne de commande.
# `*-system` : les contrôleurs posés par Rancher Turtles et les fournisseurs
# Cluster API (caphv-system, rke2-bootstrap-system...), vus sur harv1 ;
# `user-?????` : les namespaces de comptes que Rancher crée (user-2fhwx).
SYSTEM_NAMESPACES = ("kube-*", "cattle-*", "harvester-*", "longhorn-system",
                     "fleet-*", "cluster-fleet-*", "*-system", "kubeovn-*",
                     "user-?????", "cdi", "local")

# Les types lus, dans l'ordre d'affichage (ressource kubectl, type Kubernetes).
WORKLOAD_RESOURCES = (
    ("deployments.apps", "Deployment"),
    ("statefulsets.apps", "StatefulSet"),
    ("daemonsets.apps", "DaemonSet"),
    ("jobs.batch", "Job"),
    ("cronjobs.batch", "CronJob"),
    ("replicasets.apps", "ReplicaSet"),
    ("pods", "Pod"),
)
KINDS = tuple(k for _, k in WORKLOAD_RESOURCES)

STATES = ("Running", "Progressing", "Degraded", "Failed", "Completed", "Suspended")

# Ressources étendues comptées comme GPU (plugins de périphériques NVIDIA et AMD).
GPU_PREFIXES = ("nvidia.com/",)
GPU_NAMES = ("amd.com/gpu",)

# Les maillons suivis d'un propriétaire à l'autre : un ReplicaSet appartient à
# un Deployment, un Job à un CronJob. Tout autre propriétaire (un opérateur,
# une CRD) laisse la charge telle quelle : c'est elle qu'on montre.
_PARENT_KINDS = {"ReplicaSet": ("Deployment",), "Job": ("CronJob",), "Pod": (
    "ReplicaSet", "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")}


def is_system_namespace(ns):
    """Vrai pour un namespace masqué (motifs de `SYSTEM_NAMESPACES`)."""
    if not ns:
        return False
    return any(fnmatchcase(ns, pat) for pat in SYSTEM_NAMESPACES)


def is_vm_or_cdi_pod(pod):
    """Vrai pour un pod de VM (virt-launcher, disque branché à chaud) ou de CDI
    (import, téléversement, clonage d'un volume)."""
    meta = (pod or {}).get("metadata") or {}
    labels = meta.get("labels") or {}
    if labels.get("kubevirt.io") in ("virt-launcher", "hotplug-disk"):
        return True
    if "cdi.kubevirt.io" in labels or labels.get("app") == "containerized-data-importer":
        return True
    for ref in meta.get("ownerReferences") or []:
        api = str(ref.get("apiVersion") or "")
        if api.startswith("cdi.kubevirt.io/"):
            return True
        if api.startswith("kubevirt.io/") and ref.get("kind") == "VirtualMachineInstance":
            return True
    return False


def _controller_ref(obj, kinds):
    """La référence au propriétaire d'un des types `kinds` (le contrôleur
    d'abord), ou None."""
    refs = [r for r in ((obj.get("metadata") or {}).get("ownerReferences") or [])
            if isinstance(r, dict) and r.get("kind") in kinds and r.get("name")]
    refs.sort(key=lambda r: not r.get("controller"))
    return refs[0] if refs else None


def _pod_template(kind, obj):
    spec = obj.get("spec") or {}
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec") or {})
    return ((spec.get("template") or {}).get("spec") or {})


def gpu_requests(pod_spec):
    """{ressource GPU: nombre} demandés par les conteneurs d'un pod
    (`nvidia.com/*`, `amd.com/gpu`). Une ressource étendue peut n'être écrite
    qu'en limite : Kubernetes en fait alors la demande, on la lit donc à
    défaut. Une valeur illisible est ignorée."""
    out = {}
    for c in (pod_spec or {}).get("containers") or []:
        res = (c or {}).get("resources") or {}
        req = res.get("requests") or {}
        lim = res.get("limits") or {}
        for key in set(req) | set(lim):
            if not (key.startswith(GPU_PREFIXES) or key in GPU_NAMES):
                continue
            val = req.get(key, lim.get(key))
            try:
                out[key] = out.get(key, 0) + int(str(val))
            except (TypeError, ValueError):
                continue
    return out


def gpu_count(pod_spec):
    """Somme des GPU demandés par les conteneurs d'un pod (`gpu_requests`)."""
    return sum(gpu_requests(pod_spec).values())


def images(pod_spec):
    """Images des conteneurs, dans l'ordre, sans doublon."""
    out = []
    for c in (pod_spec or {}).get("containers") or []:
        img = (c or {}).get("image")
        if img and img not in out:
            out.append(img)
    return out


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _cond(obj, ctype):
    for c in ((obj.get("status") or {}).get("conditions") or []):
        if isinstance(c, dict) and c.get("type") == ctype:
            return c
    return None


def pod_state(pod):
    """(état lisible, prêt) d'un pod : la raison d'attente d'un conteneur
    (CrashLoopBackOff, ImagePullBackOff) passe avant la phase."""
    st = pod.get("status") or {}
    phase = st.get("phase") or "Unknown"
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return "Terminating", False
    ready = (_cond(pod, "Ready") or {}).get("status") == "True"
    for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
        waiting = ((cs or {}).get("state") or {}).get("waiting") or {}
        reason = waiting.get("reason")
        if reason and reason not in ("ContainerCreating", "PodInitializing"):
            return reason, False
    return phase, ready


def pod_row(pod):
    meta = pod.get("metadata") or {}
    st = pod.get("status") or {}
    state, ready = pod_state(pod)
    restarts = sum(_int((cs or {}).get("restartCount")) for cs in st.get("containerStatuses") or [])
    spec = pod.get("spec") or {}
    return {
        "name": meta.get("name") or "",
        "namespace": meta.get("namespace") or "",
        "node": spec.get("nodeName") or None,
        "phase": st.get("phase") or "Unknown",
        "state": state,
        "ready": ready,
        "restarts": restarts,
        "created": meta.get("creationTimestamp"),
        "containers": [c.get("name") for c in spec.get("containers") or [] if c.get("name")],
        "init_containers": [c.get("name") for c in spec.get("initContainers") or [] if c.get("name")],
    }


def _rollout_state(desired, ready, updated, stale):
    if desired == 0:
        return "Suspended"
    if ready >= desired and updated >= desired and not stale:
        return "Running"
    if stale or updated < desired:
        return "Progressing"
    return "Failed" if ready == 0 else "Degraded"


def workload_state(kind, obj):
    """(état, prêts, voulus) d'une charge. `voulus` vaut None quand la notion
    n'a pas de sens (CronJob)."""
    spec = obj.get("spec") or {}
    st = obj.get("status") or {}
    # le contrôleur n'a pas encore vu la dernière version de la charge
    observed = st.get("observedGeneration")
    stale = observed is not None and _int((obj.get("metadata") or {}).get("generation")) > _int(observed)
    if kind in ("Deployment", "StatefulSet", "ReplicaSet"):
        desired = _int(spec.get("replicas"), 1) if spec.get("replicas") is not None else 1
        ready = _int(st.get("readyReplicas"))
        updated = _int(st.get("updatedReplicas")) if kind != "ReplicaSet" else ready
        if kind == "StatefulSet" and st.get("updateRevision") and st.get("currentRevision") \
                and st.get("updateRevision") != st.get("currentRevision"):
            stale = True
        prog = _cond(obj, "Progressing") if kind == "Deployment" else None
        if desired and prog and prog.get("status") == "False":
            return "Failed", ready, desired          # ProgressDeadlineExceeded
        if kind == "Deployment" and spec.get("paused") and desired:
            return "Suspended", ready, desired
        return _rollout_state(desired, ready, updated, stale), ready, desired
    if kind == "DaemonSet":
        desired = _int(st.get("desiredNumberScheduled"))
        ready = _int(st.get("numberReady"))
        updated = _int(st.get("updatedNumberScheduled"), desired) if "updatedNumberScheduled" in st else desired
        return _rollout_state(desired, ready, updated, stale), ready, desired
    if kind == "Job":
        desired = _int(spec.get("completions"), 1) if spec.get("completions") is not None else 1
        done = _int(st.get("succeeded"))
        if (_cond(obj, "Failed") or {}).get("status") == "True":
            return "Failed", done, desired
        if (_cond(obj, "Complete") or {}).get("status") == "True":
            return "Completed", done, desired
        if spec.get("suspend"):
            return "Suspended", done, desired
        return ("Running" if _int(st.get("active")) else "Progressing"), done, desired
    if kind == "CronJob":
        active = len(st.get("active") or [])
        if spec.get("suspend"):
            return "Suspended", active, None
        return "Running", active, None
    if kind == "Pod":
        phase = (st.get("phase") or "Unknown")
        state, ready = pod_state(obj)
        if phase == "Succeeded":
            return "Completed", 1, 1
        if phase == "Failed":
            return "Failed", 0, 1
        if state == "Terminating" or state == phase == "Pending":
            return "Progressing", 0, 1
        if phase == "Pending":                    # bloqué : ImagePullBackOff, ErrImagePull...
            return "Failed", 0, 1
        if phase == "Running":
            return ("Running" if ready else "Degraded"), (1 if ready else 0), 1
        return "Degraded", 0, 1
    raise ValueError(f"unknown workload kind: {kind}")


def _key(kind, ns, name):
    return (kind, ns, name)


def build(lists):
    """Les charges d'un cluster.

    `lists` : {type Kubernetes: [objets]} (une entrée par type de
    `WORKLOAD_RESOURCES`, absente ou vide si le type n'a pas été lu). Rend la
    liste triée des charges, chacune avec ses pods. Un objet incomplet (sans
    nom ni namespace) est ignoré, jamais fatal."""
    objs = {}
    for kind in KINDS:
        for o in (lists or {}).get(kind) or []:
            meta = (o or {}).get("metadata") or {}
            ns, name = meta.get("namespace"), meta.get("name")
            if not ns or not name or is_system_namespace(ns):
                continue
            if kind == "Pod" and is_vm_or_cdi_pod(o):
                continue
            objs[_key(kind, ns, name)] = o

    def top(kind, ns, name, depth=0):
        """La charge qui porte cet objet, en remontant les propriétaires
        connus. Un propriétaire non lu (ReplicaSet absent de la liste) est
        reconstruit par son nom : un ReplicaSet s'appelle <deployment>-<hash>."""
        obj = objs.get(_key(kind, ns, name))
        if obj is None or depth > 4:
            return (kind, ns, name)
        ref = _controller_ref(obj, _PARENT_KINDS.get(kind, ()))
        if ref is None:
            return (kind, ns, name)
        pk, pn = ref["kind"], ref["name"]
        if _key(pk, ns, pn) in objs:
            return top(pk, ns, pn, depth + 1)
        if pk == "ReplicaSet" and kind == "Pod":
            h = ((obj.get("metadata") or {}).get("labels") or {}).get("pod-template-hash")
            if h and pn.endswith("-" + h) and _key("Deployment", ns, pn[:-len(h) - 1]) in objs:
                return ("Deployment", ns, pn[:-len(h) - 1])
        return (kind, ns, name)                   # propriétaire non lu : l'objet reste seul

    groups = {}
    for (kind, ns, name), o in objs.items():
        if kind == "Pod":
            groups.setdefault(top(kind, ns, name), []).append(o)
        elif kind in ("ReplicaSet", "Job"):
            t = top(kind, ns, name)
            if t != (kind, ns, name):
                continue                          # rangé sous son Deployment / CronJob
            groups.setdefault(t, [])
        else:
            groups.setdefault((kind, ns, name), [])

    out = []
    for (kind, ns, name), pods in groups.items():
        obj = objs[_key(kind, ns, name)]
        if kind == "ReplicaSet" and not pods and not _int((obj.get("spec") or {}).get("replicas")):
            continue                              # ancien ReplicaSet orphelin à zéro
        try:
            state, ready, desired = workload_state(kind, obj)
        except (ValueError, TypeError, AttributeError):
            state, ready, desired = "Degraded", 0, None
        tmpl = _pod_template(kind, obj)
        meta = obj.get("metadata") or {}
        out.append({
            "kind": kind,
            "namespace": ns,
            "name": name,
            "state": state,
            "ready": ready,
            "desired": desired,
            "images": images(tmpl),
            "gpus": gpu_count(tmpl),
            "created": meta.get("creationTimestamp"),
            "labels": meta.get("labels") or {},
            "pods": sorted((pod_row(p) for p in pods), key=lambda r: r["name"]),
        })
    out.sort(key=lambda w: (w["namespace"], w["name"], KINDS.index(w["kind"])))
    return out


def split_list(data):
    """Une `List` mêlée (kubectl get a,b,c -o json) rangée par type."""
    lists = {k: [] for k in KINDS}
    for it in (data or {}).get("items") or []:
        k = (it or {}).get("kind")
        if k in lists:
            lists[k].append(it)
    return lists


def age(ts, now=None):
    """« 5d », « 3h », « 12m », « 40s » depuis un horodatage RFC 3339 ; « ? »
    s'il est illisible. `now` est injectable pour les tests."""
    try:
        t = datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "?"
    now = now or datetime.now(timezone.utc)
    s = max(0, int((now - t).total_seconds()))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"
