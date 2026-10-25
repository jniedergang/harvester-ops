#!/usr/bin/env python3
"""harvester-folders : les dossiers de la console (navigation 2.0, 2.0.0).

Un dossier range des VMs Harvester et KVM, des charges de conteneurs et des
hôtes. L'arborescence vit dans `folders.db` (bin/lib/folders_db.py) ;
l'appartenance est écrite SUR l'objet, qui fait foi :

- VM Harvester et charge de conteneurs (Deployment, StatefulSet, DaemonSet,
  CronJob) : annotation `harvester-ops.io/folder` (merge patch, `null` pour
  retirer), contrôlée à blanc par le serveur avant d'écrire ;
- domaine KVM : métadonnées libvirt
  `<hops:folder xmlns:hops="https://harvester-ops.io/xmlns/folder/1" path="..."/>`
  (`--config`, plus `--live` s'il tourne ; `--live` seul pour un transitoire,
  rangement gardé aussi dans la console car il se perd à l'arrêt) ;
- hôte : champ `folder` de sa déclaration (`clusters.d/<nom>.yaml`,
  `kvm.d/<nom>.yaml`) ; un cluster de config.yaml (lecture seule) est rangé
  dans la console seulement.

Sans le droit d'écrire l'objet (`"write": false` dans la demande, ou refus
de la RBAC à l'écriture), le rangement est gardé dans la console seulement
(`source=console`, montré par une pastille).

  harvester-folders.py [--db F] [--json] [--as USER] [--role R] [--id A] tree
  ... create --parent <id|/> --name N
  ... rename --id I --name N [--spec F] [--rate N]
  ... move-folder --id I --parent <id|/> [--spec F] [--rate N]
  ... delete --id I [--spec F]
  ... assign --to <chemin|unfiled> --spec F [--rate N]
  ... apply-rename --id I --spec F [--rate N]
  ... resume --action A --spec F [--rate N]
  ... undo --action A --spec F [--rate N]
  ... write-back --folder I --spec F [--rate N]

La demande (`--spec`, fichier privé écrit par la console) :
  {"objects": [{"key", "kind", "cluster", "namespace", "name", "workload_kind", "uuid",
                "host", "domain", "family", "write": true|false}],
   "visible": [clés que la personne voit],
   "kubeconfigs": {"<cluster>": "<kubeconfig préparé pour la personne>"}}
Sans `kubeconfigs`, le kubeconfig du cluster déclaré est lu.

Chaque objet touché a sa ligne dans le journal `moves` de l'action (`--id`,
lié à l'Activité) : `resume` reprend les lignes `pending` (action
interrompue) et `failed` (hôte injoignable...) sans réécrire celles déjà
faites, `undo` rejoue l'état d'avant (un objet déplacé ailleurs depuis
n'est pas remis : conflit dit).

Rôle (`--role`, posé par la console) : viewer en lecture seule, operator
pour tout geste. Progression sur stderr en STEP_EVENT|étape|statut|message.

Codes de sortie : 0 fait, 1 refus ou au moins un objet en échec,
2 erreur d'usage ou demande invalide.
"""

import argparse
import contextlib
import fcntl
import getpass
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import cluster_decl  # noqa: E402
import folders as fm  # noqa: E402
import folders_db as fdb  # noqa: E402
import kvm_decl  # noqa: E402
import kvm_host as kh  # noqa: E402
import kvm_ops  # noqa: E402
from kube import Kube, KubeError, cluster_config  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
ROLES = ("viewer", "operator", "admin")
DENIAL_MARKERS = ("forbidden", "is not allowed", "cannot patch", "cannot update")
DEFAULT_RATE = 5.0
CONSOLE_ONLY = "kept in the console only"
SLEEP = time.sleep
ACTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class UsageError(Exception):
    pass


class Refused(Exception):
    pass


def step(sid, status, msg=""):
    clean = str(msg).replace("\n", " ").replace("|", "/")[:500]
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


# --- accès aux objets (remplaçables par les tests) ------------------------------

def make_kube(cluster, spec):
    """Kubeconfig préparé par la console pour la personne, sinon celui du
    cluster déclaré."""
    kc = (spec.get("kubeconfigs") or {}).get(cluster)
    if not kc:
        try:
            kc = cluster_config(cluster).get("kubeconfig")
        except SystemExit as e:
            raise RuntimeError(str(e)) from None
    if not kc:
        raise RuntimeError(f"no kubeconfig for cluster {cluster}")
    return Kube(kc, timeout=30)


def make_host(name):
    state = cluster_decl.state_dir()
    try:
        return kh.VirshHost(kvm_decl.load(state, name), state)
    except FileNotFoundError:
        raise RuntimeError(f"KVM host {name} is not declared") from None


def config_clusters():
    """Noms des clusters de config.yaml (lecture seule pour la console)."""
    from kube import _read_yaml
    cfg = os.environ.get("HARVESTER_OPS_CONFIG", "/etc/harvester-ops/config.yaml")
    data = _read_yaml(cfg) if Path(cfg).is_file() else None
    return {c.get("name") for c in (data or {}).get("clusters") or [] if isinstance(c, dict)}


# --- écritures sur les objets ---------------------------------------------------

class ConsoleOnly(Exception):
    """L'objet ne peut pas porter le rangement : il reste dans la console."""


def _denied(e):
    low = str(e).lower()
    return any(m in low for m in DENIAL_MARKERS)


def write_kube(obj, path, spec, kubes):
    kind = obj["kind"]
    if kind == "harvester":
        resource = "virtualmachines.kubevirt.io"
    else:
        resource = fm.WORKLOAD_RESOURCES.get(obj.get("workload_kind"))
        if not resource:
            raise UsageError(f"workload kind {obj.get('workload_kind')} cannot be filed")
    cluster = obj["cluster"]
    if cluster not in kubes:
        kubes[cluster] = make_kube(cluster, spec)
    kube = kubes[cluster]
    patch = {"metadata": {"annotations": {fm.ANNOTATION: path}}}
    try:
        kube.patch(resource, obj["namespace"], obj["name"], patch, dry_run=True)
        kube.patch(resource, obj["namespace"], obj["name"], patch)
    except KubeError as e:
        if _denied(e):
            raise ConsoleOnly(f"no write permission on the object: {str(e)[:200]}") from None
        raise


def write_kvm(obj, path, hosts):
    host = obj["host"]
    if host not in hosts:
        hosts[host] = make_host(host)
    res = kvm_ops.set_folder(hosts[host], obj["domain"], path)
    if not res["persistent"]:
        # domaine transitoire : le rangement se perd à son arrêt, la console le garde
        return "transient"
    return None


@contextlib.contextmanager
def _decl_lock(state, wait=10.0):
    d = kvm_decl.decl_dir(state)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > end:
                    raise RuntimeError("another declaration change is in progress: try again") from None
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


_FOLDER_LINE_RE = re.compile(r"^folder\s*:")


def set_yaml_folder(text, path):
    """Pose ou retire la clé de premier niveau `folder` d'une déclaration
    YAML, sans toucher au reste du fichier (aucune bibliothèque YAML ici).
    La valeur est une chaîne JSON, que YAML lit telle quelle."""
    lines = text.splitlines()
    out, skip = [], False
    for ln in lines:
        if skip and ln[:1] in (" ", "\t"):
            continue                              # suite d'une valeur repliée
        skip = False
        if _FOLDER_LINE_RE.match(ln):
            skip = True
            continue
        out.append(ln)
    if path is not None:
        out.append(f"folder: {json.dumps(path)}")
    return "\n".join(out) + "\n"


def write_host(obj, path):
    family, name = obj["family"], obj["name"]
    state = cluster_decl.state_dir()
    if family == "kvm":
        with _decl_lock(state):
            try:
                decl = kvm_decl.load(state, name)
            except FileNotFoundError:
                raise RuntimeError(f"KVM host {name} is not declared") from None
            if path is None:
                decl.pop("folder", None)
            else:
                decl["folder"] = path
            kvm_decl.write(state, decl)
        return
    if name in config_clusters():
        raise ConsoleOnly("declared in config.yaml (read-only for the console)")
    p = cluster_decl.decl_path(state, name)
    if p is None or not p.is_file():
        raise RuntimeError(f"cluster {name} is not declared")
    with _decl_lock(state):
        kvm_decl.write_private(p, set_yaml_folder(p.read_text(), path))


def write_object(obj, path, spec, ctx):
    """Écrit le rangement sur l'objet. Rend la source du rangement (« vm »,
    ou « console » s'il reste dans la console), ou lève une exception."""
    kind = obj["kind"]
    if kind in ("harvester", "container"):
        write_kube(obj, path, spec, ctx.setdefault("kubes", {}))
        return "vm"
    if kind == "kvm":
        return "console" if write_kvm(obj, path, ctx.setdefault("hosts", {})) == "transient" else "vm"
    if kind == "host":
        write_host(obj, path)
        return "vm"
    raise UsageError(f"unknown object kind: {kind}")


# --- demande --------------------------------------------------------------------

def read_spec(path):
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise UsageError(f"unreadable spec: {type(e).__name__}") from None
    if not isinstance(data, dict):
        raise UsageError("the spec must be a JSON object")
    return data


def check_object(o):
    """Objet de la demande normalisé, clé recalculée ; UsageError sinon."""
    if not isinstance(o, dict):
        raise UsageError("each object must be a JSON object")
    kind = o.get("kind")
    try:
        if kind == "harvester":
            key = fm.object_key("harvester", cluster=o.get("cluster"), namespace=o.get("namespace"),
                                name=o.get("name"))
        elif kind == "container":
            key = fm.object_key("container", cluster=o.get("cluster"), namespace=o.get("namespace"),
                                workload_kind=o.get("workload_kind"), name=o.get("name"))
        elif kind == "kvm":
            key = fm.object_key("kvm", uuid=o.get("uuid") or (o.get("key") or "")[4:])
            kvm_ops.check_vm_name(o.get("domain"))
            if not kvm_decl.NAME_RE.match(o.get("host") or ""):
                raise ValueError("invalid KVM host name")
        elif kind == "host":
            key = fm.object_key("host", family=o.get("family"), name=o.get("name"))
        else:
            raise ValueError(f"unknown object kind: {kind}")
    except (ValueError, kvm_ops.OpError) as e:
        raise UsageError(str(getattr(e, "message", e))) from None
    if o.get("key") and o["key"] != key:
        raise UsageError(f"object key {o['key']} does not match its fields ({key})")
    return dict(o, key=key, write=o.get("write", True) is not False)


def objects_of(spec):
    objs = spec.get("objects") or []
    if not isinstance(objs, list):
        raise UsageError("objects must be a list")
    out = {}
    for o in objs:
        c = check_object(o)
        out[c["key"]] = c
    return out


def need_operator(args):
    if args.role == "viewer":
        raise Refused(f"{args.as_user} (viewer) may only read folders: the operator role is needed")


def folder_ref(conn, ref):
    """`/` (racine) -> None ; un identifiant -> int (le dossier doit exister)."""
    if ref in ("/", "", None):
        return None
    try:
        fid = int(ref)
    except (TypeError, ValueError):
        raise UsageError(f"invalid folder id: {ref}") from None
    if fdb.path_of(conn, fid) is None:
        raise Refused(f"no folder {fid}")
    return fid


# --- exécution d'un lot de déplacements ---------------------------------------------

def run_lines(conn, args, action, lines, objects, spec):
    """Exécute les lignes du journal (`pending`) : écrit chaque objet, met le
    cache et la ligne à jour. Rend le nombre d'échecs. Débit borné par
    `--rate` écritures par seconde."""
    ctx, failed = {}, 0
    rate = args.rate if args.rate and args.rate > 0 else DEFAULT_RATE
    for n, m in enumerate(lines):
        key, to = m["object_key"], m["to_path"]
        sid = f"obj-{m['seq']}"
        what = f"{key}: {m['from_path'] or 'unfiled'} -> {to or 'unfiled'}"
        if key.startswith("folder/"):
            fdb.record_move(conn, action, m["seq"], key, m["from_path"], to, "done")
            continue
        obj = objects.get(key)
        if obj is None:
            failed += 1
            fdb.record_move(conn, action, m["seq"], key, m["from_path"], to, "failed",
                            "not in the request (not visible to you)")
            step(sid, "error", f"{what}: not in the request (not visible to you)")
            continue
        if n:
            SLEEP(1.0 / rate)
        cached = fdb.memberships(conn, [key]).get(key)
        try:
            if not obj["write"]:
                raise ConsoleOnly("no write permission on the object")
            source = write_object(obj, to, spec, ctx)
            message = "" if source == "vm" else "transient domain: " + CONSOLE_ONLY
        except ConsoleOnly as e:
            if cached and cached["source"] == "vm":
                # l'objet porte son dossier et la personne ne peut pas l'écrire :
                # un rangement de la console serait effacé à la prochaine lecture
                failed += 1
                msg = f"{e}; the object carries its folder {cached['folder_path']} and keeps it"
                fdb.record_move(conn, action, m["seq"], key, m["from_path"], to, "failed", msg)
                step(sid, "error", f"{what}: {msg}")
                continue
            source = "console"
            message = f"{e}: {CONSOLE_ONLY}" if to is not None else f"{e}: removed from the console only"
        except UsageError:
            raise
        except Exception as e:                     # noqa: BLE001 : un objet n'arrête pas le lot
            failed += 1
            msg = str(getattr(e, "message", e))[:300]
            fdb.record_move(conn, action, m["seq"], key, m["from_path"], to, "failed", msg)
            step(sid, "error", f"{what}: {msg}")
            continue
        fdb.upsert_membership(conn, key, to, source, scope=(cached or {}).get("scope", ""))
        fdb.record_move(conn, action, m["seq"], key, m["from_path"], to, "done", message)
        step(sid, "done", f"{what}" + (f" ({message})" if message else ""))
    return failed


def start_lines(conn, action, plan, first_seq=None):
    """Range le plan [(clé, de, vers)] dans le journal (`pending`) ; rend les lignes."""
    seq = first_seq if first_seq is not None else len(fdb.moves_of(conn, action))
    lines = []
    for key, src, dst in plan:
        fdb.record_move(conn, action, seq, key, src, dst, "pending")
        lines.append({"seq": seq, "object_key": key, "from_path": src, "to_path": dst})
        seq += 1
    return lines


def finish(args, data, failed, text):
    if args.json:
        print(json.dumps(dict(data, failed=failed), indent=2))
    else:
        print(text)
    return EXIT_FAIL if failed else EXIT_OK


# --- sous-commandes -------------------------------------------------------------------

def cmd_tree(args, conn):
    tree = fdb.list_tree(conn)
    members = fdb.memberships(conn)
    counts = {}
    for m in members.values():
        counts[m["folder_path"]] = counts.get(m["folder_path"], 0) + 1
    for f in tree:
        f["count"] = counts.get(f["path"], 0)
    if args.json:
        print(json.dumps({"folders": tree}, indent=2))
    else:
        for f in tree:
            print(f"{f['id']:>5}  {f['path']}  ({f['count']})")
    return EXIT_OK


def cmd_create(args, conn):
    need_operator(args)
    parent = folder_ref(conn, args.parent)
    fid = fdb.create(conn, parent, args.name, args.as_user)
    path = fdb.path_of(conn, fid)
    step("create", "done", f"folder {path} created")
    return finish(args, {"id": fid, "path": path}, 0, f"{fid} {path}")


def _rewrite_folder(args, conn, folder_id, change, label):
    """Renommage ou déplacement d'un dossier, puis réécriture des objets
    qu'il contient (lui et ses sous-dossiers), dans la même action."""
    need_operator(args)
    spec = read_spec(args.spec)
    objects = objects_of(spec)
    members = {k: v["folder_path"] for k, v in fdb.memberships(conn).items()}
    old, new = change()
    plan = fm._rewrite(members, old, new)
    lines = start_lines(conn, args.id, [(f"folder/{folder_id}", old, new)])
    fdb.record_move(conn, args.id, lines[0]["seq"], f"folder/{folder_id}", old, new, "done")
    step(label, "done", f"folder {old} -> {new}")
    failed = run_lines(conn, args, args.id, start_lines(conn, args.id, plan), objects, spec)
    return finish(args, {"action": args.id, "from": old, "to": new, "objects": len(plan)}, failed,
                  f"{old} -> {new}: {len(plan) - failed}/{len(plan)} object(s) rewritten")


def cmd_rename(args, conn):
    fid = folder_ref(conn, args.folder_id)
    if fid is None:
        raise UsageError("the root cannot be renamed")
    return _rewrite_folder(args, conn, fid, lambda: fdb.rename(conn, fid, args.name), "rename")


def cmd_move_folder(args, conn):
    fid = folder_ref(conn, args.folder_id)
    if fid is None:
        raise UsageError("the root cannot be moved")
    parent = folder_ref(conn, args.parent)
    return _rewrite_folder(args, conn, fid, lambda: fdb.move_folder(conn, fid, parent), "move")


def cmd_delete(args, conn):
    need_operator(args)
    fid = folder_ref(conn, args.folder_id)
    if fid is None:
        raise UsageError("the root cannot be deleted")
    spec = read_spec(args.spec)
    visible = set(spec["visible"]) if isinstance(spec.get("visible"), list) else None
    try:
        path = fdb.delete_empty(conn, fid, visible)
    except fdb.FolderError as e:
        step("delete", "error", e.message)
        raise Refused(e.message) from None
    step("delete", "done", f"folder {path} deleted")
    return finish(args, {"id": fid, "path": path}, 0, f"{path} deleted")


def cmd_assign(args, conn):
    need_operator(args)
    to = None if args.to == fm.UNFILED else args.to
    if to is not None:
        why = fm.check_path(to)
        if why:
            raise UsageError(why)
        if fdb.id_of(conn, to) is None:
            raise Refused(f"no folder {to}: create it first")
    spec = read_spec(args.spec)
    objects = objects_of(spec)
    if not objects:
        raise UsageError("no object to file")
    cache = fdb.memberships(conn, objects)
    plan = [(k, (cache.get(k) or {}).get("folder_path"), to) for k in objects]
    failed = run_lines(conn, args, args.id, start_lines(conn, args.id, plan), objects, spec)
    return finish(args, {"action": args.id, "to": to, "objects": len(plan)}, failed,
                  f"{len(plan) - failed}/{len(plan)} object(s) filed in {to or 'unfiled'}")


def cmd_resume(args, conn):
    need_operator(args)
    spec = read_spec(args.spec)
    moves = fdb.moves_of(conn, args.action)
    if not moves:
        raise Refused(f"no action {args.action} in the folder journal")
    # lignes pas encore faites : `pending` (action interrompue) et `failed`
    # (hôte injoignable, refus) ; une ligne `done` n'est jamais réécrite
    pending = [m for m in moves if m["status"] in ("pending", "failed")]
    failed = run_lines(conn, args, args.action, pending, objects_of(spec), spec)
    return finish(args, {"action": args.action, "resumed": len(pending)}, failed,
                  f"{len(pending) - failed}/{len(pending)} pending line(s) done")


def cmd_apply_rename(args, conn):
    """Réécrit les objets restés sous l'ancien chemin d'un dossier renommé ou
    déplacé (dernier renommage du journal), sans toucher à l'arborescence."""
    need_operator(args)
    fid = folder_ref(conn, args.folder_id)
    row = conn.execute("SELECT from_path, to_path FROM moves WHERE object_key = ? AND status = 'done' "
                       "ORDER BY at DESC, rowid DESC LIMIT 1", (f"folder/{fid}",)).fetchone()
    if row is None:
        raise Refused(f"folder {fid} has not been renamed or moved")
    spec = read_spec(args.spec)
    members = {k: v["folder_path"] for k, v in fdb.memberships(conn).items()}
    plan = fm._rewrite(members, row["from_path"], row["to_path"])
    failed = run_lines(conn, args, args.id, start_lines(conn, args.id, plan), objects_of(spec), spec)
    return finish(args, {"action": args.id, "objects": len(plan)}, failed,
                  f"{len(plan) - failed}/{len(plan)} object(s) rewritten")


def cmd_undo(args, conn):
    need_operator(args)
    moves = fdb.moves_of(conn, args.action)
    if not moves:
        raise Refused(f"no action {args.action} in the folder journal")
    spec = read_spec(args.spec)
    objects = objects_of(spec)
    failed = 0
    # l'arborescence d'abord (dans l'ordre inverse), pour que les chemins
    # d'avant existent quand les objets y reviennent
    for m in reversed([m for m in moves if m["object_key"].startswith("folder/") and m["status"] == "done"]):
        fid = int(m["object_key"].split("/", 1)[1])
        cur = fdb.path_of(conn, fid)
        if cur != m["to_path"]:
            failed += 1
            step(f"folder-{fid}", "error", f"folder {fid} is now {cur}, not {m['to_path']}: left as is")
            continue
        before_parent = fm.parent_path(m["from_path"])
        parent_id = fdb.id_of(conn, before_parent) if before_parent else None
        if fm.parent_path(cur) != before_parent:
            fdb.move_folder(conn, fid, parent_id)
        if fm.split(cur)[-1] != fm.split(m["from_path"])[-1]:
            fdb.rename(conn, fid, fm.split(m["from_path"])[-1])
        fdb.record_move(conn, args.id, len(fdb.moves_of(conn, args.id)), m["object_key"],
                        m["to_path"], m["from_path"], "done")
        step(f"folder-{fid}", "done", f"folder {m['to_path']} -> {m['from_path']}")
    # les objets portent encore le chemin écrit par l'action : ils y sont
    # repris pour revenir à celui d'avant (conflit s'ils ont bougé depuis)
    current = {k: v["folder_path"] for k, v in fdb.memberships(conn).items()}
    obj_moves = [m for m in moves if not m["object_key"].startswith("folder/")]
    plan, conflicts = fm.undo_plan(obj_moves, current)
    for c in conflicts:
        failed += 1
        step("conflict", "error", f"{c['key']} is now in {c['current'] or 'unfiled'}, "
                                  f"not {c['expected'] or 'unfiled'}: left as is")
    failed += run_lines(conn, args, args.id, start_lines(conn, args.id, plan), objects, spec)
    return finish(args, {"action": args.id, "undone": args.action, "objects": len(plan),
                         "conflicts": conflicts}, failed,
                  f"{len(plan)} object(s) put back, {len(conflicts)} conflict(s)")


def cmd_write_back(args, conn):
    """Écrit sur les objets les rangements « console » du dossier (et de ses
    sous-dossiers) que la personne a désormais le droit d'écrire."""
    need_operator(args)
    fid = folder_ref(conn, args.folder_id)
    path = fdb.path_of(conn, fid)
    spec = read_spec(args.spec)
    objects = {k: o for k, o in objects_of(spec).items() if o["write"]}
    plan = [(k, m["folder_path"], m["folder_path"]) for k, m in sorted(fdb.memberships(conn).items())
            if m["source"] == "console" and fm.within(m["folder_path"], path) and k in objects]
    failed = run_lines(conn, args, args.id, start_lines(conn, args.id, plan), objects, spec)
    return finish(args, {"action": args.id, "objects": len(plan)}, failed,
                  f"{len(plan) - failed}/{len(plan)} object(s) now carry their folder")


def parse_args(argv=None):
    """L'identifiant d'ACTION s'écrit `--id=<action>` (forme avec `=`, celle
    qu'ajoute la console en fin de ligne) ou `--id <action>` avant la
    sous-commande ; dans une sous-commande, `--id <n>` (forme avec espace) ou
    `--folder <n>` désigne le DOSSIER."""
    argv = list(sys.argv[1:] if argv is None else argv)
    action_ids = [a.split("=", 1)[1] for a in argv if a.startswith("--id=")]
    argv = [a for a in argv if not a.startswith("--id=")]
    p = argparse.ArgumentParser(prog="harvester-folders.py", description=__doc__.split("\n")[0])
    p.add_argument("--db", help="folders.db (HARVESTER_OPS_FOLDERS_DB by default)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--as", dest="as_user", default=None, help="person on whose behalf")
    p.add_argument("--role", choices=ROLES, default="admin")
    p.add_argument("--id", default=None, help="action id (journal linked to the Activity)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_spec(sp, required=False):
        sp.add_argument("--spec", required=required, help="request file (JSON)")
        sp.add_argument("--rate", type=float, default=DEFAULT_RATE, help="writes per second")
        return sp

    def with_folder(sp):
        sp.add_argument("--folder", "--id", dest="folder_id", required=True, help="folder id")
        return sp
    sub.add_parser("tree")
    sp = sub.add_parser("create")
    sp.add_argument("--parent", required=True)
    sp.add_argument("--name", required=True)
    sp = with_folder(with_spec(sub.add_parser("rename")))
    sp.add_argument("--name", required=True)
    sp = with_folder(with_spec(sub.add_parser("move-folder")))
    sp.add_argument("--parent", required=True)
    sp = with_folder(sub.add_parser("delete"))
    sp.add_argument("--spec")
    sp = with_spec(sub.add_parser("assign"), required=True)
    sp.add_argument("--to", required=True)
    with_folder(with_spec(sub.add_parser("apply-rename")))
    sp = with_spec(sub.add_parser("resume"))
    sp.add_argument("--action", required=True)
    sp = with_spec(sub.add_parser("undo"))
    sp.add_argument("--action", required=True)
    with_folder(with_spec(sub.add_parser("write-back")))
    args = p.parse_args(argv)
    if action_ids:
        args.id = action_ids[-1]
    return args


COMMANDS = {"tree": cmd_tree, "create": cmd_create, "rename": cmd_rename,
            "move-folder": cmd_move_folder, "delete": cmd_delete, "assign": cmd_assign,
            "apply-rename": cmd_apply_rename, "resume": cmd_resume, "undo": cmd_undo,
            "write-back": cmd_write_back}


def main(argv=None):
    try:
        args = parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    args.as_user = args.as_user or getpass.getuser()
    args.id = args.id or "fa-" + uuid.uuid4().hex[:12]
    for a in ("id", "action"):
        if getattr(args, a, None) is not None and not ACTION_ID_RE.match(getattr(args, a)):
            print(f"error: invalid {a}", file=sys.stderr)
            return EXIT_USAGE
    try:
        conn = fdb.connect(args.db)
        return COMMANDS[args.cmd](args, conn)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except fdb.FolderError as e:
        print(f"error: {e.message}", file=sys.stderr)
        return EXIT_USAGE if e.kind == "invalid" else EXIT_FAIL
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
