"""harvester-ops : la logique pure des dossiers (navigation 2.0).

Un dossier range des VMs (Harvester et KVM), des charges de conteneurs et
des hôtes. L'appartenance est écrite SUR l'objet (annotation
`harvester-ops.io/folder`, métadonnées libvirt, champ `folder` de la
déclaration d'hôte) ; la console garde l'arborescence et un cache des
rangements (bin/lib/folders_db.py). Règles :

- un objet est dans un seul dossier, sans héritage : ranger un hôte ne
  range pas ses VMs ;
- l'objet fait foi : une valeur lue sur l'objet l'emporte sur le cache ; un
  chemin inconnu de la base crée le dossier (origine « objects ») ;
- un objet sans valeur garde un rangement « console » (repli quand la
  personne n'a pas le droit d'écrire l'objet) ; un rangement « vm » qu'il ne
  porte plus est retiré (quelqu'un l'a enlevé sur l'objet) ;
- profondeur 8, chemin de 253 caractères, nom de 1 à 63 caractères
  (décision 4 du 25/10/2026).

Aucune entrée/sortie ici : tout est testé sur des données.
"""

import re

ANNOTATION = "harvester-ops.io/folder"
MAX_DEPTH = 8
MAX_PATH = 253
MAX_NAME = 63
UNFILED = "unfiled"
SOURCES = ("vm", "console")
ORIGINS = ("console", "objects")

# 1 à 63 caractères, sans `/` ni caractère de contrôle, ni `.` ni `..`
FOLDER_NAME_RE = re.compile(r"^(?!\.{1,2}$)[^/\x00-\x1f\x7f]{1,63}$")

# charges de conteneurs rangeables (décision 11) : sorte -> ressource kubectl
WORKLOAD_RESOURCES = {
    "Deployment": "deployments.apps",
    "StatefulSet": "statefulsets.apps",
    "DaemonSet": "daemonsets.apps",
    "CronJob": "cronjobs.batch",
}
OBJECT_KINDS = ("harvester", "kvm", "container", "host")
HOST_KINDS = ("harvester", "kvm")


def check_name(name):
    """Raison du refus d'un nom de dossier, None s'il est bon (une raison
    par règle, dans cet ordre)."""
    if not isinstance(name, str) or not name:
        return "the folder name is empty"
    if len(name) > MAX_NAME:
        return f"the folder name is longer than {MAX_NAME} characters"
    if "/" in name:
        return "the folder name contains '/'"
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "the folder name contains a control character"
    if name in (".", ".."):
        return "the folder name cannot be '.' or '..'"
    return None if FOLDER_NAME_RE.match(name) else "invalid folder name"


def split(path):
    """Segments d'un chemin déjà vérifié (`/A/B` -> ["A", "B"])."""
    return path.split("/")[1:]


def join(parts):
    return "/" + "/".join(parts)


def check_path(path):
    """Raison du refus d'un chemin de dossier, None s'il est bon : absolu,
    sans `/` final, chaque segment un nom valide, profondeur et longueur
    bornées."""
    if not isinstance(path, str) or not path:
        return "the folder path is empty"
    if not path.startswith("/"):
        return "the folder path must start with '/'"
    if len(path) > MAX_PATH:
        return f"the folder path is longer than {MAX_PATH} characters"
    if path == "/" or path.endswith("/"):
        return "the folder path must name a folder (no trailing '/')"
    parts = split(path)
    if len(parts) > MAX_DEPTH:
        return f"the folder path is deeper than {MAX_DEPTH} levels"
    for p in parts:
        why = check_name(p)
        if why:
            return f"{why} ({p!r})"
    return None


def parent_path(path):
    parts = split(path)
    return join(parts[:-1]) if len(parts) > 1 else None


def ancestors(path):
    """Chemins de chaque niveau, du plus haut au chemin lui-même."""
    parts = split(path)
    return [join(parts[:i]) for i in range(1, len(parts) + 1)]


def within(path, prefix):
    """True si `path` est `prefix` ou l'un de ses descendants."""
    return path == prefix or path.startswith(prefix + "/")


def object_key(kind, **p):
    """Clé d'un objet rangé :
    `harvester/<cluster>/<ns>/<nom>`, `kvm/<uuid>`,
    `container/<cluster>/<ns>/<sorte>/<nom>`, `host/<famille>/<nom>`.
    ValueError pour une sorte ou un champ manquant."""
    def need(*names):
        vals = [p.get(n) for n in names]
        if any(not isinstance(v, str) or not v or "/" in v for v in vals):
            raise ValueError(f"{kind} object needs {', '.join(names)}")
        return vals
    if kind == "harvester":
        return "harvester/" + "/".join(need("cluster", "namespace", "name"))
    if kind == "kvm":
        (uuid,) = need("uuid")
        return f"kvm/{uuid.lower()}"
    if kind == "container":
        cluster, ns, wk, name = need("cluster", "namespace", "workload_kind", "name")
        if wk not in WORKLOAD_RESOURCES:
            raise ValueError(f"workload kind {wk} cannot be filed "
                             f"(only {', '.join(WORKLOAD_RESOURCES)})")
        return f"container/{cluster}/{ns}/{wk.lower()}/{name}"
    if kind == "host":
        family, name = need("family", "name")
        if family not in HOST_KINDS:
            raise ValueError(f"unknown host family: {family}")
        return f"host/{family}/{name}"
    raise ValueError(f"unknown object kind: {kind}")


def key_kind(key):
    """Sorte d'un objet d'après sa clé (`harvester`, `kvm`, `container`, `host`)."""
    return (key or "").split("/", 1)[0]


def group_of(key):
    """Groupe d'un objet dans l'entrée « Dossiers » : vms, containers, hosts."""
    k = key_kind(key)
    return {"harvester": "vms", "kvm": "vms", "container": "containers", "host": "hosts"}.get(k)


# --- arborescence ----------------------------------------------------------
# `tree` : [{"id", "parent_id", "name", ...}] (parent_id None à la racine)

def paths(tree):
    """{id: chemin} de chaque dossier de l'arborescence."""
    by_id = {f["id"]: f for f in tree}
    out = {}

    def path_of(fid, seen=()):
        if fid in out:
            return out[fid]
        f = by_id[fid]
        if fid in seen:                          # boucle : base abîmée
            raise ValueError(f"folder {fid} is its own ancestor")
        parent = f.get("parent_id")
        p = ("/" + f["name"]) if parent is None else path_of(parent, seen + (fid,)) + "/" + f["name"]
        out[fid] = p
        return p
    for f in tree:
        path_of(f["id"])
    return out


def index(tree):
    """{chemin: id}."""
    return {p: fid for fid, p in paths(tree).items()}


def subtree_ids(tree, folder_id):
    """Identifiants du dossier et de tous ses descendants."""
    children = {}
    for f in tree:
        children.setdefault(f.get("parent_id"), []).append(f["id"])
    out, todo = [], [folder_id]
    while todo:
        fid = todo.pop()
        out.append(fid)
        todo.extend(children.get(fid, []))
    return out


def subtree_depth(tree, folder_id):
    """Niveaux sous le dossier, lui compris (1 pour une feuille)."""
    children = {}
    for f in tree:
        children.setdefault(f.get("parent_id"), []).append(f["id"])

    def depth(fid):
        return 1 + max((depth(c) for c in children.get(fid, [])), default=0)
    return depth(folder_id)


# --- lecture des objets ----------------------------------------------------

def reconcile(tree, rows, cache, now):
    """Réaligne le cache des rangements sur les objets lus.

    `rows` : [{"key", "folder" (valeur lue sur l'objet ou None), "scope"}]
    (un `folder` invalide doit avoir été écarté avant, par check_path) ;
    `cache` : {clé: {"folder_path", "source", "seen_at", "scope"}}.
    Rend (memberships, created_paths) : memberships {clé: rangement ou None
    (à retirer)} pour les clés lues seulement (les autres attendent la
    purge), created_paths les chemins absents de l'arborescence, ancêtres
    compris, du moins profond au plus profond (origine « objects »).

    Pas d'héritage : chaque objet ne prend que SA valeur ; un hôte rangé ne
    range pas ses VMs."""
    known = set(index(tree))
    created = []
    out = {}
    for r in rows:
        key = r.get("key")
        if not key:
            continue
        val = r.get("folder")
        old = cache.get(key)
        if val:
            out[key] = {"folder_path": val, "source": "vm", "seen_at": now, "scope": r.get("scope")}
            for a in ancestors(val):
                if a not in known:
                    known.add(a)
                    created.append(a)
        elif old and old.get("source") == "console":
            out[key] = dict(old, seen_at=now, scope=r.get("scope") or old.get("scope"))
        elif old:
            out[key] = None                       # retiré sur l'objet
    created.sort(key=lambda p: (p.count("/"), p))
    return out, created


def remap(path, renames, known):
    """Chemin lu sur un objet, ramené sur l'arborescence pendant un
    renommage ou un déplacement de dossier en cours (objets pas encore
    réécrits) : `renames` [(ancien, nouveau)] dans l'ordre du journal. Un
    ancien chemin de nouveau présent dans `known` (recréé exprès, ou remis
    par Annuler) n'est pas réécrit."""
    for old, new in renames:
        if old and new and old not in known and within(path, old):
            moved = new + path[len(old):]
            if check_path(moved) is None:
                path = moved
    return path


def effective(rows, cache):
    """(dossier, source) de chaque clé lue, après réalignement : la valeur
    de l'objet, sinon le repli « console » du cache."""
    out = {}
    for r in rows:
        key = r.get("key")
        if not key:
            continue
        if r.get("folder"):
            out[key] = (r["folder"], "vm")
        else:
            c = cache.get(key)
            out[key] = (c["folder_path"], "console") if c and c.get("source") == "console" else (None, None)
    return out


# --- plans des gestes --------------------------------------------------------

def _rewrite(members, old_prefix, new_prefix):
    plan = []
    for key, path in sorted(members.items()):
        if path and within(path, old_prefix):
            plan.append((key, path, new_prefix + path[len(old_prefix):]))
    return plan


def rename_plan(tree, members, folder_id, new_name):
    """[(clé, ancien chemin, nouveau chemin)] des objets du dossier renommé et
    de ses descendants. `members` : {clé: chemin}. ValueError pour un nom ou
    un chemin résultant invalide."""
    why = check_name(new_name)
    if why:
        raise ValueError(why)
    p = paths(tree)
    old = p[folder_id]
    parent = parent_path(old)
    new = (parent or "") + "/" + new_name
    _check_moved(tree, p, folder_id, old, new)
    return _rewrite(members, old, new)


def move_plan(tree, members, folder_id, new_parent_id):
    """Comme rename_plan, pour un dossier déplacé sous `new_parent_id`
    (None = racine). ValueError s'il irait sous lui-même."""
    p = paths(tree)
    old = p[folder_id]
    if new_parent_id is not None and new_parent_id in subtree_ids(tree, folder_id):
        raise ValueError("a folder cannot move into itself or one of its subfolders")
    name = split(old)[-1]
    new = (p[new_parent_id] if new_parent_id is not None else "") + "/" + name
    _check_moved(tree, p, folder_id, old, new)
    return _rewrite(members, old, new)


def _check_moved(tree, p, folder_id, old, new):
    """Chaque chemin du sous-arbre, une fois renommé ou déplacé, reste valide."""
    for fid in subtree_ids(tree, folder_id):
        moved = new + p[fid][len(old):]
        why = check_path(moved)
        if why:
            raise ValueError(why)


def undo_plan(moves, current):
    """Annulation d'une action : `moves` [{"object_key", "from_path",
    "to_path", "status"}] dans l'ordre du journal, `current` {clé: chemin
    actuel ou None}. Rend ([(clé, chemin actuel, chemin d'avant)],
    [conflits]) : un objet déplacé ailleurs depuis n'est pas remis, son
    conflit est dit ({"key", "expected", "current"})."""
    first, last = {}, {}
    for m in moves:
        if m.get("status") != "done":
            continue
        key = m["object_key"]
        first.setdefault(key, m.get("from_path"))
        last[key] = m.get("to_path")
    plan, conflicts = [], []
    for key in sorted(last):
        if current.get(key) != last[key]:
            conflicts.append({"key": key, "expected": last[key], "current": current.get(key)})
            continue
        if first[key] != last[key]:
            plan.append((key, last[key], first[key]))
    return plan, conflicts


def delete_check(tree, folder_id, members, visible=None):
    """Un dossier se supprime s'il est VIDE pour tout le monde : ni
    sous-dossier, ni objet rangé (tous rangements confondus). `visible` :
    clés que la personne voit (None = toutes). Rend {"empty", "count_visible",
    "subfolders", "hidden"} ; `hidden` dit qu'il reste des objets qu'elle ne
    voit pas, sans en donner le nombre."""
    p = paths(tree)
    path = p[folder_id]
    subfolders = len(subtree_ids(tree, folder_id)) - 1
    inside = [k for k, v in members.items() if v and within(v, path)]
    seen = inside if visible is None else [k for k in inside if k in visible]
    hidden = len(seen) < len(inside)
    return {"empty": not inside and subfolders == 0, "count_visible": len(seen),
            "subfolders": subfolders, "hidden": hidden}
