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

Revue des tâches 5 à 8 (I-1, I-2) : seuls les gabarits dont le contenu
change sont copiés (un gabarit recopié sans changement de `spec` est
corrigé en place par la topologie, qui ne remplace alors aucune machine :
l'attente ne finissait jamais). Les machines anciennes sont celles des
rôles touchés, relevées par leur UID AVANT la bascule et rangées dans la
marque `harvester-ops.io/renewal` du `Cluster` : aucune horloge n'est
comparée (celle de la console contre celle du serveur). La marque dit
aussi qu'un renouvellement n'est pas fini (`renew-busy`) ; `renew
--finish` reprend l'attente et le ménage, `renew --abandon` (réservé aux
administrateurs) retire la marque sans rien supprimer.

Bibliothèque standard seulement ; ce module ne parle à aucun cluster
(`current` lit par un `kube` injecté).
"""

import copy
import json
import re
import time

K_CLASS = "clusterclasses.cluster.x-k8s.io"
K_CP_TEMPLATE = "rke2controlplanetemplates.controlplane.cluster.x-k8s.io"
K_CONFIG_TEMPLATE = "rke2configtemplates.bootstrap.cluster.x-k8s.io"
K_MACHINE = "machines.cluster.x-k8s.io"
KINDS = {"RKE2ControlPlaneTemplate": K_CP_TEMPLATE, "RKE2ConfigTemplate": K_CONFIG_TEMPLATE}
A_REVISION = "harvester-ops.io/template-revision"
A_RENEWED = "harvester-ops.io/renewed-at"
A_RENEWAL = "harvester-ops.io/renewal"
L_CLUSTER = "cluster.x-k8s.io/cluster-name"
L_CONTROL_PLANE = "cluster.x-k8s.io/control-plane"
L_TOPOLOGY_MD = "topology.cluster.x-k8s.io/deployment-name"
CONTROL_PLANE = "controlPlane"
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
# Rôles touchés, marque du renouvellement, progression
# ---------------------------------------------------------------------------
def changed(current_templates, wanted_docs):
    """[(rôle, document actuel, document voulu)] des gabarits dont
    `spec.template.spec` change (`current_templates` : [(rôle, document)]
    de `current`, `wanted_docs` dans le même ordre). Revue 5-8 (I-1) : seuls
    ceux-là sont copiés et seuls leurs rôles sont attendus."""
    out = []
    for (role, cur), want in zip(current_templates, wanted_docs):
        if diff([cur], [want]):
            out.append((role, cur, want))
    return out


def role_of(machine, cluster=None):
    """Rôle d'une machine : `controlPlane` (étiquette du plan de contrôle),
    `workers/<classe>` (classe du MachineDeployment de la topologie nommé
    par son étiquette), `workers/?` sans moyen de le dire."""
    labels = ((machine or {}).get("metadata") or {}).get("labels") or {}
    if L_CONTROL_PLANE in labels:
        return CONTROL_PLANE
    md = labels.get(L_TOPOLOGY_MD)
    topo = (((cluster or {}).get("spec") or {}).get("topology") or {})
    for d in ((topo.get("workers") or {}).get("machineDeployments")) or []:
        if md and d.get("name") == md and d.get("class"):
            return f"workers/{d['class']}"
    return "workers/?"


def in_roles(machine, roles, cluster=None):
    """La machine relève-t-elle d'un des rôles ? Une machine de worker dont
    la classe ne se lit pas compte pour toute classe touchée (prudence)."""
    r = role_of(machine, cluster)
    return r in roles or (r == "workers/?" and any(x.startswith("workers/") for x in roles))


def machine_id(m):
    md = (m or {}).get("metadata") or {}
    return md.get("uid") or md.get("name") or ""


def marker(revision, names, roles, old, secrets):
    """Marque du renouvellement (annotation `A_RENEWAL` du Cluster) :
    révision, {ancien gabarit: nouveau}, rôles attendus, UID des machines
    d'avant la bascule, Secrets à retirer à la fin. Aucun secret."""
    return {"revision": int(revision), "names": dict(names), "roles": sorted(roles), "old": sorted(old),
            "secrets": sorted(set(secrets))}


def marker_value(mark):
    return json.dumps(mark, sort_keys=True, separators=(",", ":"))


def marker_of(cluster):
    """Marque d'un renouvellement pas fini, ou None. Une marque illisible
    compte comme présente (jamais prise pour « fini »)."""
    raw = (((cluster or {}).get("metadata") or {}).get("annotations") or {}).get(A_RENEWAL)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return {"revision": 0, "names": {}, "roles": [], "old": [], "secrets": [], "unreadable": True}
    if not isinstance(d, dict):
        return {"revision": 0, "names": {}, "roles": [], "old": [], "secrets": [], "unreadable": True}
    return {"revision": int(d.get("revision") or 0), "names": dict(d.get("names") or {}),
            "roles": list(d.get("roles") or []), "old": list(d.get("old") or []),
            "secrets": list(d.get("secrets") or [])}


def _machine_ready(m):
    for c in ((m.get("status") or {}).get("conditions")) or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return (m.get("status") or {}).get("phase") == "Running"


def progress(machines, mark, cluster=None):
    """{"done", "old", "new", "message"} des machines des rôles de la
    marque : anciennes = UID relevés avant la bascule (encore là, même en
    suppression) ; nouvelles = les autres, vivantes. Fait quand plus aucune
    ancienne ne reste et que les nouvelles sont prêtes. Sans rôle touché
    (seuls des Secrets ont changé) : fait."""
    roles = (mark or {}).get("roles") or []
    olds = set((mark or {}).get("old") or [])
    mine = [m for m in machines or [] if in_roles(m, roles, cluster)]
    old = [m for m in mine if machine_id(m) in olds]
    new = [m for m in mine if machine_id(m) not in olds and not (m.get("metadata") or {}).get("deletionTimestamp")]
    if not roles:
        return {"done": True, "old": 0, "new": len(new), "message": "no machine to replace"}
    ready = [m for m in new if _machine_ready(m)]
    done = not old and bool(new) and len(ready) == len(new)
    msg = (f"{len(ready)}/{len(new)} new machine(s) ready, {len(old)} old left" if not done
           else f"{len(new)} machine(s) renewed")
    return {"done": done, "old": len(old), "new": len(new), "message": msg}


def old_templates(current_templates, wanted_names, replaced=(), designated=()):
    """Gabarits d'une révision précédente du même nom de base, et ceux que
    la marque dit remplacés (`replaced` : un nom raccourci n'a plus la même
    base, revue 5-8 m-10), à supprimer une fois le renouvellement fait ;
    jamais un gabarit voulu ni encore désigné par la ClusterClass."""
    keep = set(wanted_names) | set(designated)
    bases = {base_name(n) for n in wanted_names}
    return [d for d in current_templates
            if d["metadata"]["name"] not in keep
            and (base_name(d["metadata"]["name"]) in bases or d["metadata"]["name"] in set(replaced))]


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
