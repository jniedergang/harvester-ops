"""harvester-ops : renouvellement des machines d'un cluster enfant (2.8.0).

Le geste « Changer les registres » promis par la décision 190, et le même
mécanisme pour changer l'OIDC de l'API et pour faire confiance à une
nouvelle CA de registre (point 233). Rien de ce que `caphv-generate` a
produit n'est réécrit : les gabarits `RKE2ControlPlaneTemplate` et
`RKE2ConfigTemplate` du cluster sont copiés en `<nom>-r<N>` (N = révision
suivante, annotation `harvester-ops.io/template-revision`), la copie reçoit
le nouveau contenu, la `ClusterClass` du namespace est repointée ; la
topologie remplace les machines (`RollingUpdate`, `maxSurge: 1`). Les
anciens gabarits restent tant qu'une machine s'y rapporte (retour arrière),
puis sont supprimés : Cluster API ne le fait pas (essai 6 de la tâche 1).

Vu sur rke2-banc (plan 2.8.0, « Essais préalables (tâche 1) », point 6) :
la modification en place est acceptée par les webhooks, la rotation est un
choix ; plan de contrôle et worker remplacés ensemble, API injoignable
environ 65 s au passage d'un plan de contrôle unique, une adresse de plus
prise pendant le renouvellement.

Une machine se rapporte aux anciens gabarits si elle a été créée avant la
bascule (annotation `harvester-ops.io/renewed-at` des copies, secondes
UTC) : les machines d'un plan de contrôle ne portent pas le nom de leur
gabarit, leur date de création si.

Bibliothèque standard seulement ; ce module ne parle à aucun cluster
(`current` lit par un `kube` injecté).
"""

import calendar
import copy
import re
import time

K_CLASS = "clusterclasses.cluster.x-k8s.io"
K_CP_TEMPLATE = "rke2controlplanetemplates.controlplane.cluster.x-k8s.io"
K_CONFIG_TEMPLATE = "rke2configtemplates.bootstrap.cluster.x-k8s.io"
K_MACHINE = "machines.cluster.x-k8s.io"
KINDS = {"RKE2ControlPlaneTemplate": K_CP_TEMPLATE, "RKE2ConfigTemplate": K_CONFIG_TEMPLATE}
A_REVISION = "harvester-ops.io/template-revision"
A_RENEWED = "harvester-ops.io/renewed-at"
L_CLUSTER = "cluster.x-k8s.io/cluster-name"
WHATS = ("registries", "oidc", "ca")
TIMEOUT = (60, 10800, 3600)
REV_RE = re.compile(r"^(?P<base>.+?)-r(?P<n>[0-9]{1,4})$")
# métadonnées que le serveur pose et qu'une copie ne garde pas
_SERVER_META = ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields", "ownerReferences",
                "selfLink", "deletionTimestamp", "deletionGracePeriodSeconds")


class RenewError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def finding(code, level="block", **facts):
    return {"code": code, "level": level, "facts": facts}


# ---------------------------------------------------------------------------
# Références de la ClusterClass (v1beta1 `ref`, v1beta2 `templateRef`)
# ---------------------------------------------------------------------------
def _ref_holder(node):
    """(dictionnaire qui porte la référence, clé) : `templateRef` (v1beta2)
    ou `ref` (v1beta1)."""
    node = node or {}
    for k in ("templateRef", "ref"):
        if isinstance(node.get(k), dict):
            return node, k
    return None, None


def template_refs(cls):
    """[(rôle, référence)] des gabarits RKE2 d'une ClusterClass : le plan de
    contrôle, puis le gabarit d'amorçage de chaque classe de workers."""
    spec = (cls or {}).get("spec") or {}
    out = []
    holder, key = _ref_holder(spec.get("controlPlane"))
    if holder:
        out.append(("controlPlane", holder[key]))
    for md in ((spec.get("workers") or {}).get("machineDeployments")) or []:
        boot = md.get("bootstrap") or ((md.get("template") or {}).get("bootstrap")) or {}
        h, k = _ref_holder(boot)
        if h:
            out.append((f"workers/{md.get('class')}", h[k]))
    return out


def class_name_of(cluster):
    topo = ((cluster or {}).get("spec") or {}).get("topology") or {}
    return (topo.get("classRef") or {}).get("name") or topo.get("class")


def revision_of(doc):
    try:
        return int((((doc or {}).get("metadata") or {}).get("annotations") or {}).get(A_REVISION) or 0)
    except ValueError:
        return 0


def current(kube, ns, cluster):
    """{"class", "templates": [(rôle, document)], "revision"} du cluster :
    ClusterClass de sa topologie et ses gabarits RKE2. RenewError
    `renew-cluster-missing` / `renew-class-missing` / `renew-template-missing`."""
    cl = kube.get("clusters.cluster.x-k8s.io", ns, cluster)
    if cl is None:
        raise RenewError("renew-cluster-missing", f"{ns}/{cluster} not found")
    name = class_name_of(cl)
    cls = kube.get(K_CLASS, ns, name) if name else None
    if cls is None:
        raise RenewError("renew-class-missing", f"ClusterClass {ns}/{name} not found")
    templates = []
    for role, ref in template_refs(cls):
        res = KINDS.get(ref.get("kind"))
        if res is None:
            continue
        doc = kube.get(res, ns, ref.get("name"))
        if doc is None:
            raise RenewError("renew-template-missing", f"{ref.get('kind')} {ns}/{ref.get('name')} not found")
        templates.append((role, doc))
    return {"class": cls, "templates": templates, "cluster": cl,
            "revision": max([revision_of(d) for _r, d in templates] or [0])}


# ---------------------------------------------------------------------------
# Différences, copies, ClusterClass
# ---------------------------------------------------------------------------
def _walk(a, b, path, out):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            _walk(a.get(k), b.get(k), f"{path}.{k}" if path else k, out)
    elif a != b:
        out.append(path)


def diff(current_docs, wanted_docs):
    """[{"template", "paths"}] des gabarits dont `spec.template.spec`
    change, dans l'ordre ; vide : rien à renouveler. Documents appariés par
    position (même ordre que `current`)."""
    out = []
    for cur, want in zip(current_docs, wanted_docs):
        a = ((cur.get("spec") or {}).get("template") or {}).get("spec") or {}
        b = ((want.get("spec") or {}).get("template") or {}).get("spec") or {}
        paths = []
        _walk(a, b, "spec.template.spec", paths)
        if paths:
            out.append({"template": cur["metadata"]["name"], "kind": cur.get("kind"), "paths": paths})
    return out


def base_name(name):
    m = REV_RE.match(name or "")
    return m.group("base") if m else name


def rotated_name(name, revision):
    """`<base>-r<N>` (63 caractères au plus : la base est raccourcie)."""
    suffix = f"-r{revision}"
    return base_name(name)[:63 - len(suffix)].rstrip("-") + suffix


def rotated(docs, revision, now=None):
    """Copies `<nom>-r<revision>` des gabarits voulus, métadonnées du
    serveur retirées, annotations de révision et d'heure de bascule."""
    stamp = str(int(now if now is not None else time.time()))
    out = []
    for d in docs:
        c = copy.deepcopy(d)
        md = c.setdefault("metadata", {})
        for k in _SERVER_META:
            md.pop(k, None)
        md["name"] = rotated_name(md.get("name"), revision)
        ann = md.setdefault("annotations", {})
        ann.pop("kubectl.kubernetes.io/last-applied-configuration", None)
        ann[A_REVISION] = str(revision)
        ann[A_RENEWED] = stamp
        c.pop("status", None)
        out.append(c)
    return out


def class_patch(cls, names):
    """Patch de fusion de la ClusterClass : chaque référence dont le nom est
    une clé de `names` ({ancien: nouveau}) prend le nouveau nom. La liste des
    classes de workers est rendue entière (un patch de fusion remplace une
    liste)."""
    spec = copy.deepcopy((cls or {}).get("spec") or {})
    patch = {}
    holder, key = _ref_holder(spec.get("controlPlane"))
    if holder and holder[key].get("name") in names:
        holder[key]["name"] = names[holder[key]["name"]]
        patch["controlPlane"] = spec["controlPlane"]
    mds = ((spec.get("workers") or {}).get("machineDeployments")) or []
    changed = False
    for md in mds:
        boot = md.get("bootstrap") if isinstance(md.get("bootstrap"), dict) else (md.get("template") or {}).get(
            "bootstrap")
        h, k = _ref_holder(boot)
        if h and h[k].get("name") in names:
            h[k]["name"] = names[h[k]["name"]]
            changed = True
    if changed:
        patch["workers"] = dict(spec.get("workers") or {}, machineDeployments=mds)
    return {"spec": patch}


# ---------------------------------------------------------------------------
# Progression
# ---------------------------------------------------------------------------
def _created(machine):
    ts = ((machine or {}).get("metadata") or {}).get("creationTimestamp") or ""
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return 0


def _machine_ready(m):
    for c in ((m.get("status") or {}).get("conditions")) or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return (m.get("status") or {}).get("phase") == "Running"


def since(templates):
    """Heure de bascule des gabarits voulus (la plus ancienne), ou None."""
    stamps = []
    for d in templates or []:
        try:
            stamps.append(int((((d.get("metadata") or {}).get("annotations") or {}).get(A_RENEWED))))
        except (TypeError, ValueError):
            continue
    return min(stamps) if stamps else None


def progress(machines, wanted_templates):
    """{"done", "old", "new", "message"} : machines créées avant la bascule
    (anciens gabarits) et après ; fait quand plus aucune ancienne machine ne
    reste et que les nouvelles sont prêtes. Sans heure de bascule (aucun
    renouvellement fait par la console) : fait."""
    t0 = since(wanted_templates)
    live = [m for m in machines or [] if not ((m.get("metadata") or {}).get("deletionTimestamp"))]
    going = [m for m in machines or [] if (m.get("metadata") or {}).get("deletionTimestamp")]
    if t0 is None:
        return {"done": True, "old": 0, "new": len(live), "message": "no renewal recorded"}
    old = [m for m in live + going if _created(m) < t0]
    new = [m for m in live if _created(m) >= t0]
    ready = [m for m in new if _machine_ready(m)]
    done = not old and bool(new) and len(ready) == len(new)
    msg = (f"{len(ready)}/{len(new)} new machine(s) ready, {len(old)} old left" if not done
           else f"{len(new)} machine(s) renewed")
    return {"done": done, "old": len(old), "new": len(new), "message": msg}


def busy(machines, templates):
    """Renouvellement en cours : des machines d'avant la dernière bascule
    restent (machines de deux révisions)."""
    p = progress(machines, templates)
    return since(templates) is not None and p["old"] > 0


def old_templates(current_templates, wanted_names):
    """Gabarits d'une révision précédente du même nom de base, à supprimer
    une fois le renouvellement fait (jamais un gabarit encore désigné)."""
    keep = set(wanted_names)
    bases = {base_name(n) for n in wanted_names}
    return [d for d in current_templates
            if d["metadata"]["name"] not in keep and base_name(d["metadata"]["name"]) in bases]


def room_findings(pool, free_cpu_m, free_memory, cpu, memory_bytes, overcommit=None, vm_overhead=0):
    """Constats de la machine en plus (2.5.0 rejoués pour UNE machine) :
    adresse libre dans l'IPPool (`renew-pool-short`, bloquant), processeur
    et mémoire (avertissements, comme la création)."""
    out = []
    if pool is None:
        out.append(finding("renew-pool-missing", "block"))
    elif (pool.get("available") or 0) < 1:
        out.append(finding("renew-pool-short", "block", pool=pool.get("name"), available=pool.get("available") or 0))
    oc = overcommit or {}
    need_cpu = int(cpu * 1000 * 100 / max(100, int(oc.get("cpu") or 100)))
    need_mem = int(memory_bytes * 100 / max(100, int(oc.get("memory") or 100)) + vm_overhead)
    if free_cpu_m is not None and need_cpu > free_cpu_m:
        out.append(finding("cpu-short", "warn", needed=round(need_cpu / 1000, 2), free=round(free_cpu_m / 1000, 2)))
    if free_memory is not None and need_mem > free_memory:
        out.append(finding("memory-short", "warn", needed=need_mem, free=free_memory))
    return out


def check_whats(raw):
    """Liste ordonnée des gestes demandés ; ValueError."""
    items = [x.strip() for x in str(raw or "").split(",") if x.strip()]
    if not items or any(x not in WHATS for x in items):
        raise ValueError(f"--what: one or more of {', '.join(WHATS)}")
    return [w for w in WHATS if w in items]
