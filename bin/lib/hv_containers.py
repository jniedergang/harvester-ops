"""harvester-ops : les charges de conteneurs d'un cluster Harvester (v1.91.0).

Harvester 1.9.1 passe en GA les conteneurs bare-metal : des charges
Kubernetes ordinaires (Deployments, StatefulSets, DaemonSets, Jobs, CronJobs,
pods seuls) déployées sur le cluster, à côté des VM. La console les montre
comme elle montre les VM ; depuis 1.93.0 elle les crée, les met à
l'échelle, les redémarre et les supprime (fonctions en fin de fichier).

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

import copy
import re
import shlex
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

# 2.0.0 : annotation de dossier (bin/lib/folders.py, ANNOTATION)
FOLDER_ANNOTATION = "harvester-ops.io/folder"

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
            # 2.0.0 : valeur brute de l'annotation de dossier (contrôlée par la console)
            "folder_annotation": (meta.get("annotations") or {}).get(FOLDER_ANNOTATION),
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


# ---------------------------------------------------------------------------
# v1.93.0 : les gestes (créer, mettre à l'échelle, redémarrer, supprimer)
#
# Fonctions pures : la demande devient un manifeste, validé ici, sans
# cluster. L'écriture (contrôle à blanc du serveur, puis écriture) est faite
# par bin/harvester-containers.py, que la console lance en action suivie.
# ---------------------------------------------------------------------------

# Ce qu'on crée depuis le formulaire ; ce qu'on accepte d'un YAML collé.
CREATE_KINDS = ("Deployment", "Job")
MANIFEST_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod")
SCALABLE_KINDS = ("Deployment", "StatefulSet", "ReplicaSet")
RESTARTABLE_KINDS = ("Deployment", "StatefulSet", "DaemonSet")
_API_VERSIONS = {"Deployment": "apps/v1", "Job": "batch/v1"}

# Règle du projet : tout conteneur a une limite mémoire. Celle posée quand la
# demande n'en donne pas, et la demande de CPU par défaut.
DEFAULT_MEMORY = "256Mi"
DEFAULT_CPU_REQUEST = "100m"
DEFAULT_GPU_RESOURCE = "nvidia.com/gpu"
MAX_REPLICAS = 1000
MAX_GPUS = 64
MANAGED_BY = ("app.kubernetes.io/managed-by", "harvester-ops")
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_MEM_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?(?:[kMGTPE]i?)?$")
_CPU_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]+)?|[0-9]+m)$")
_GPU_RES_RE = re.compile(r"^(?:nvidia\.com/[a-z0-9](?:[-a-z0-9._]{0,61}[a-z0-9])?|amd\.com/gpu)$")


def kind_of(value):
    """Le type Kubernetes d'un nom donné en minuscules ou non
    (« deployment » -> « Deployment ») ; ValueError s'il est inconnu."""
    for k in KINDS:
        if str(value or "").lower() == k.lower():
            return k
    raise ValueError(f"unknown workload kind: {value}")


def resource_of(kind):
    """La ressource kubectl d'un type (« Deployment » -> « deployments.apps »)."""
    return {k: r for r, k in WORKLOAD_RESOURCES}[kind]


def check_namespace(ns):
    """Un namespace où la console accepte d'écrire : une étiquette RFC 1123,
    pas un namespace système (aucune charge dans cattle-system,
    harvester-system, longhorn-system...)."""
    if not ns or not _LABEL_RE.match(str(ns)):
        raise ValueError(f"invalid namespace: {ns!r} (RFC 1123 label: a-z, 0-9, '-', 63 characters at most)")
    if is_system_namespace(ns):
        raise ValueError(f"namespace {ns} is a system namespace: container workloads go elsewhere")
    return ns


def check_target(namespace, kind, name, allowed):
    """(namespace, type, nom) d'un geste sur une charge existante, validés."""
    check_namespace(namespace)
    k = kind_of(kind)
    if k not in allowed:
        raise ValueError(f"a {k} cannot take this action (allowed: {', '.join(allowed)})")
    if not name or not _SUBDOMAIN_RE.match(str(name)):
        raise ValueError(f"invalid name: {name!r}")
    return namespace, k, name


def _int_field(spec, key, default, lo, hi):
    v = spec.get(key)
    if v in (None, ""):
        return default
    if isinstance(v, bool) or isinstance(v, float):
        raise ValueError(f"{key} must be a whole number")
    try:
        n = int(str(v).strip())
    except ValueError:
        raise ValueError(f"{key} must be a whole number") from None
    if not lo <= n <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return n


def _command(value):
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError as e:
            raise ValueError(f"command: {e}") from None
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return list(value)
    raise ValueError("command must be a string or a list of strings")


def _env(value):
    """[{name, value}] depuis un dict, une liste de « K=V » ou de {name, value}."""
    if value in (None, "", [], {}):
        return []
    items = []
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        for x in value:
            if isinstance(x, str):
                k, sep, v = x.partition("=")
                if not sep:
                    raise ValueError(f"environment variable without '=': {x!r}")
                items.append((k, v))
            elif isinstance(x, dict) and "name" in x:
                items.append((x.get("name"), x.get("value", "")))
            else:
                raise ValueError("environment variables must be NAME=VALUE")
    else:
        raise ValueError("environment variables must be NAME=VALUE")
    out, seen = [], set()
    for k, v in items:
        k = str(k or "").strip()
        if not _ENV_RE.match(k):
            raise ValueError(f"invalid environment variable name: {k!r}")
        if k in seen:
            raise ValueError(f"environment variable {k} given twice")
        seen.add(k)
        out.append({"name": k, "value": "" if v is None else str(v)})
    return out


def _quantity(value, regex, what):
    v = str(value).strip()
    if not regex.match(v):
        raise ValueError(f"invalid {what}: {v!r}")
    return v


def ensure_limits(pod_spec, memory=DEFAULT_MEMORY, cpu_request=DEFAULT_CPU_REQUEST):
    """Pose sur chaque conteneur (et conteneur d'init) une limite mémoire et
    des demandes de mémoire et de CPU quand elles manquent. Rien de ce qui
    est écrit n'est remplacé. Rend la spec de pod, modifiée en place."""
    for c in (pod_spec.get("initContainers") or []) + (pod_spec.get("containers") or []):
        if not isinstance(c, dict):
            continue
        res = c.get("resources")
        if not isinstance(res, dict):
            res = c["resources"] = {}
        lim = res.setdefault("limits", {})
        req = res.setdefault("requests", {})
        lim.setdefault("memory", req.get("memory") or memory)
        req.setdefault("memory", lim["memory"])
        req.setdefault("cpu", lim.get("cpu") or cpu_request)
    return pod_spec


def normalize_spec(spec):
    """La demande du formulaire (ou des options de la ligne de commande),
    validée et complétée. ValueError au premier défaut, avec un message qui
    dit lequel."""
    if not isinstance(spec, dict):
        raise ValueError("the spec must be an object")
    name = str(spec.get("name") or "").strip()
    if not _LABEL_RE.match(name):
        raise ValueError(f"invalid name: {name!r} (RFC 1123 label: a-z, 0-9, '-', 63 characters at most)")
    ns = check_namespace(str(spec.get("namespace") or "").strip())
    kind = spec.get("kind") or "Deployment"
    if kind not in CREATE_KINDS:
        raise ValueError(f"kind must be one of {', '.join(CREATE_KINDS)}")
    image = str(spec.get("image") or "").strip()
    if not image or any(ch.isspace() for ch in image):
        raise ValueError("an image is required, without spaces")
    gres = str(spec.get("gpu_resource") or DEFAULT_GPU_RESOURCE).strip()
    if not _GPU_RES_RE.match(gres):
        raise ValueError(f"invalid GPU resource: {gres!r} (nvidia.com/... or amd.com/gpu)")
    cpu = spec.get("cpu")
    return {
        "name": name, "namespace": ns, "kind": kind, "image": image,
        "replicas": _int_field(spec, "replicas", 1, 0 if kind == "Deployment" else 1, MAX_REPLICAS),
        "command": _command(spec.get("command")),
        "env": _env(spec.get("env")),
        "gpus": _int_field(spec, "gpus", 0, 0, MAX_GPUS),
        "gpu_resource": gres,
        "cpu": _quantity(cpu, _CPU_RE, "CPU limit") if cpu not in (None, "") else None,
        "memory": _quantity(spec.get("memory") or DEFAULT_MEMORY, _MEM_RE, "memory limit"),
    }


def build_manifest(spec):
    """Le manifeste d'une charge depuis la demande du formulaire : un
    Deployment (`replicas` pods) ou un Job (`replicas` exécutions, autant en
    parallèle). Limite mémoire toujours posée, demandes de mémoire et de CPU
    aussi ; les GPU demandés sont une limite de la ressource étendue, que
    Kubernetes reprend comme demande."""
    s = normalize_spec(spec)
    limits = {"memory": s["memory"]}
    requests = {"memory": s["memory"], "cpu": s["cpu"] or DEFAULT_CPU_REQUEST}
    if s["cpu"]:
        limits["cpu"] = s["cpu"]
    if s["gpus"]:
        limits[s["gpu_resource"]] = str(s["gpus"])
    container = {"name": s["name"], "image": s["image"],
                 "resources": {"limits": limits, "requests": requests}}
    if s["command"]:
        container["command"] = s["command"]
    if s["env"]:
        container["env"] = s["env"]
    pod = {"containers": [container]}
    meta = {"name": s["name"], "namespace": s["namespace"],
            "labels": {"app": s["name"], MANAGED_BY[0]: MANAGED_BY[1]}}
    template = {"metadata": {"labels": {"app": s["name"]}}, "spec": pod}
    if s["kind"] == "Deployment":
        body = {"replicas": s["replicas"], "selector": {"matchLabels": {"app": s["name"]}},
                "template": template}
    else:
        pod["restartPolicy"] = "Never"
        body = {"completions": s["replicas"], "parallelism": s["replicas"], "template": template}
    return {"apiVersion": _API_VERSIONS[s["kind"]], "kind": s["kind"], "metadata": meta, "spec": body}


def _manifest_pod_spec(obj):
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("the manifest has no spec")
    if obj["kind"] == "Pod":
        pod = spec
    else:
        if obj["kind"] == "CronJob":
            spec = (spec.get("jobTemplate") or {}).get("spec") or {}
        pod = (spec.get("template") or {}).get("spec")
    if not isinstance(pod, dict) or not pod.get("containers"):
        raise ValueError("the manifest has no containers")
    return pod


def check_manifest(obj, namespace=None):
    """Un manifeste collé (une seule charge), validé et complété : type
    accepté, nom, namespace (celui du manifeste, sinon `namespace`, sinon
    `default`) qui n'est pas un namespace système, et la limite mémoire de
    chaque conteneur posée si elle manque. Rend une copie."""
    if not isinstance(obj, dict):
        raise ValueError("the manifest must be one Kubernetes object")
    if obj.get("kind") == "List" or "items" in obj:
        raise ValueError("one workload at a time: the manifest holds a list")
    kind = obj.get("kind")
    if kind not in MANIFEST_KINDS:
        raise ValueError(f"kind must be one of {', '.join(MANIFEST_KINDS)}")
    if not obj.get("apiVersion"):
        raise ValueError("the manifest has no apiVersion")
    out = copy.deepcopy(obj)
    meta = out.get("metadata")
    if meta is None:
        meta = out["metadata"] = {}
    if not isinstance(meta, dict):
        raise ValueError("invalid metadata")
    name = str(meta.get("name") or "")
    if not _SUBDOMAIN_RE.match(name):
        raise ValueError(f"invalid name: {name!r}")
    if namespace and meta.get("namespace") and meta["namespace"] != namespace:
        raise ValueError(f"the manifest says namespace {meta['namespace']}, the request {namespace}")
    meta["namespace"] = check_namespace(meta.get("namespace") or namespace or "default")
    ensure_limits(_manifest_pod_spec(out))
    return out


def restart_patch(now=None):
    """Le patch d'un redémarrage progressif, comme `kubectl rollout restart` :
    une annotation horodatée sur le modèle de pod, qui fait recréer les pods."""
    now = now or datetime.now(timezone.utc)
    return {"spec": {"template": {"metadata": {"annotations": {
        RESTARTED_AT: now.strftime("%Y-%m-%dT%H:%M:%SZ")}}}}}
