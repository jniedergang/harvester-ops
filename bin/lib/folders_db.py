"""harvester-ops : la base des dossiers et des favoris (navigation 2.0).

Une base SQLite de la console, `folders.db`, à côté de `notes.db` (ou
HARVESTER_OPS_FOLDERS_DB), en mode WAL, comme `bookings.db`.

Les favoris : une préférence PERSONNELLE (une étoile sur un hôte du menu),
retrouvée sur un autre poste. Écrite par la console sous `/api/me/`, sans
action suivie ni ligne de commande (décision 5 du 25/10/2026, comme le mot
de passe de son propre compte).

Les dossiers (2.0.0, tables additives) :
- `folders` : l'arborescence, gardée par la console seule ; `origin` dit si
  un dossier a été créé par quelqu'un (« console ») ou recréé depuis les
  objets qui le nommaient (« objects », base perdue ou autre console) ;
- `memberships` : le cache des rangements. L'appartenance est écrite SUR
  l'objet (bin/lib/folders.py) ; `source` vaut « vm » (lue sur l'objet) ou
  « console » (repli : la personne n'avait pas le droit d'écrire l'objet).
  `scope` nomme la lecture qui l'a vue (`harvester/<cluster>`, `kvm/<hôte>`,
  `container/<cluster>`, `hosts`) : une entrée n'est purgée (30 jours sans
  être vue) que si sa lecture a réussi, jamais pour un hôte injoignable ;
- `moves` : le journal des écritures d'une action (une ligne par objet),
  qui sert à la reprise d'une action interrompue et à « Annuler ».

Seule la ligne de commande `bin/harvester-folders.py` modifie l'arborescence
et le journal ; la console y réaligne le cache pendant ses lectures (décision
4, exception documentée : pas d'action suivie pour un cache).
"""

import calendar
import os
import sqlite3
import time
from pathlib import Path

import cluster_decl
import folders as fm

# Familles d'hôtes : un cluster Harvester entier ou un hyperviseur KVM
HOST_KINDS = ("harvester", "kvm")

SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    user TEXT NOT NULL,
    host_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user, host_key)
);
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES folders(id),
    name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'console'
);
-- deux frères ne portent jamais le même nom (racine comprise : parent 0)
CREATE UNIQUE INDEX IF NOT EXISTS folders_sibling ON folders (IFNULL(parent_id, 0), name);
CREATE TABLE IF NOT EXISTS memberships (
    vm_key TEXT PRIMARY KEY,
    folder_path TEXT NOT NULL,
    source TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS memberships_path ON memberships (folder_path);
CREATE TABLE IF NOT EXISTS moves (
    action_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    object_key TEXT NOT NULL,
    from_path TEXT,
    to_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    message TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY (action_id, seq)
);
"""

# rangement réaligné au plus une fois par heure quand rien n'a changé : une
# lecture de flotte toutes les 30 s n'écrit pas des milliers de lignes
SEEN_REFRESH_S = 3600
PURGE_DAYS = 30
MOVE_STATUSES = ("pending", "done", "failed", "skipped")


class FolderError(ValueError):
    """Geste refusé : `kind` vaut invalid, conflict, notfound ou nonempty."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def default_path():
    """HARVESTER_OPS_FOLDERS_DB, sinon `folders.db` dans le répertoire d'état."""
    env = os.environ.get("HARVESTER_OPS_FOLDERS_DB")
    return Path(env) if env else cluster_decl.state_dir() / "folders.db"


def connect(path=None):
    path = Path(path or default_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    # le journal WAL et sa mémoire partagée suivent le umask : mêmes droits
    # que la base (ils portent les mêmes lignes)
    for f in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
    return conn


def host_key(kind, name):
    """Clé d'un hôte, la même que celle des rangements : `harvester/<nom>`."""
    if kind not in HOST_KINDS:
        raise ValueError(f"unknown host kind: {kind}")
    return f"{kind}/{name}"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def add_favorite(conn, user, kind, name):
    """Ajoute l'étoile ; une étoile déjà posée reste telle quelle (idempotent).
    Rend True si elle vient d'être posée."""
    cur = conn.execute("INSERT OR IGNORE INTO favorites (user, host_key, created_at) VALUES (?, ?, ?)",
                       (user, host_key(kind, name), _now()))
    return cur.rowcount == 1


def remove_favorite(conn, user, kind, name):
    """Retire l'étoile ; rend True si elle existait."""
    cur = conn.execute("DELETE FROM favorites WHERE user = ? AND host_key = ?",
                       (user, host_key(kind, name)))
    return cur.rowcount == 1


def favorites(conn, user):
    """Clés des hôtes favoris de cette personne (ensemble)."""
    return {r["host_key"] for r in conn.execute("SELECT host_key FROM favorites WHERE user = ?", (user,))}


# --- arborescence ------------------------------------------------------------

class _write:
    """Transaction d'écriture (BEGIN IMMEDIATE : un seul écrivain à la fois,
    les contrôles et l'écriture dans la même transaction)."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, *_):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def list_tree(conn):
    """[{"id", "parent_id", "name", "position", "path", "origin", "created_at",
    "created_by"}], triés par chemin."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, parent_id, name, position, created_at, created_by, origin FROM folders")]
    p = fm.paths(rows)
    for r in rows:
        r["path"] = p[r["id"]]
    return sorted(rows, key=lambda r: r["path"])


def path_of(conn, folder_id):
    """Chemin d'un dossier, None s'il n'existe pas."""
    return next((f["path"] for f in list_tree(conn) if f["id"] == folder_id), None)


def id_of(conn, path):
    """Identifiant du dossier de ce chemin, None s'il n'existe pas."""
    return next((f["id"] for f in list_tree(conn) if f["path"] == path), None)


def _get(conn, folder_id):
    tree = list_tree(conn)
    f = next((f for f in tree if f["id"] == folder_id), None)
    if f is None:
        raise FolderError("notfound", f"no folder {folder_id}")
    return tree, f


def _insert(conn, parent_id, name, by, origin):
    pos = conn.execute("SELECT IFNULL(MAX(position), -1) + 1 FROM folders WHERE IFNULL(parent_id, 0) = ?",
                       (parent_id or 0,)).fetchone()[0]
    try:
        cur = conn.execute("INSERT INTO folders (parent_id, name, position, created_at, created_by, origin) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (parent_id, name, pos, _now(), by or "", origin))
    except sqlite3.IntegrityError:
        raise FolderError("conflict", f"a folder named {name!r} already exists here") from None
    return cur.lastrowid


def create(conn, parent_id, name, by, origin="console"):
    """Crée un dossier sous `parent_id` (None = racine) ; rend son id.
    FolderError invalid (nom, profondeur, longueur), notfound (parent),
    conflict (frère du même nom, y compris créé au même moment ailleurs)."""
    why = fm.check_name(name)
    if why:
        raise FolderError("invalid", why)
    with _write(conn):
        parent = ""
        if parent_id is not None:
            _tree, f = _get(conn, parent_id)
            parent = f["path"]
        why = fm.check_path(parent + "/" + name)
        if why:
            raise FolderError("invalid", why)
        return _insert(conn, parent_id, name, by, origin)


def rename(conn, folder_id, name):
    """Renomme un dossier ; rend (ancien chemin, nouveau chemin)."""
    why = fm.check_name(name)
    if why:
        raise FolderError("invalid", why)
    with _write(conn):
        tree, f = _get(conn, folder_id)
        try:
            fm.rename_plan(tree, {}, folder_id, name)
        except ValueError as e:
            raise FolderError("invalid", str(e)) from None
        try:
            conn.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
        except sqlite3.IntegrityError:
            raise FolderError("conflict", f"a folder named {name!r} already exists here") from None
        parent = fm.parent_path(f["path"])
        return f["path"], (parent or "") + "/" + name


def move_folder(conn, folder_id, new_parent_id):
    """Déplace un dossier sous `new_parent_id` (None = racine) ; rend (ancien
    chemin, nouveau chemin). Refusé sous lui-même ou un descendant, ou si un
    chemin du sous-arbre dépasserait les bornes."""
    with _write(conn):
        tree, f = _get(conn, folder_id)
        if new_parent_id is not None:
            _get(conn, new_parent_id)
        try:
            fm.move_plan(tree, {}, folder_id, new_parent_id)
        except ValueError as e:
            raise FolderError("invalid", str(e)) from None
        try:
            conn.execute("UPDATE folders SET parent_id = ? WHERE id = ?", (new_parent_id, folder_id))
        except sqlite3.IntegrityError:
            raise FolderError("conflict", f"a folder named {f['name']!r} already exists there") from None
        new_parent = path_of(conn, new_parent_id) if new_parent_id is not None else ""
        return f["path"], new_parent + "/" + f["name"]


def delete_empty(conn, folder_id, visible=None):
    """Supprime un dossier VIDE (ni sous-dossier ni objet rangé, pour qui que
    ce soit) ; FolderError nonempty sinon, avec ce que la personne voit
    (`visible` : ses clés, None = toutes), jamais le nombre caché."""
    with _write(conn):
        tree, f = _get(conn, folder_id)
        check = fm.delete_check(tree, folder_id, _members(conn), visible)
        if not check["empty"]:
            what = []
            if check["subfolders"]:
                what.append(f"{check['subfolders']} subfolder(s)")
            if check["count_visible"]:
                what.append(f"{check['count_visible']} object(s)")
            if check["hidden"]:
                what.append("objects you cannot see")
            raise FolderError("nonempty", f"folder {f['path']} is not empty ({', '.join(what)}): "
                                          "move content to another folder first")
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        return f["path"]


def ensure_path(conn, path, origin="objects", by=""):
    """Crée chaque niveau absent du chemin (origine `origin`) ; rend l'id du
    dernier. Un frère créé au même moment par un autre est repris."""
    why = fm.check_path(path)
    if why:
        raise FolderError("invalid", why)
    with _write(conn):
        idx = fm.index(list_tree(conn))
        parent = None
        for a in fm.ancestors(path):
            fid = idx.get(a)
            if fid is None:
                fid = _insert(conn, parent, fm.split(a)[-1], by, origin)
                idx[a] = fid
            parent = fid
        return parent


# --- cache des rangements ------------------------------------------------------

def _members(conn):
    return {r["vm_key"]: r["folder_path"] for r in conn.execute("SELECT vm_key, folder_path FROM memberships")}


def memberships(conn, keys=None):
    """{clé: {"folder_path", "source", "seen_at", "scope"}} (toutes, ou ces clés)."""
    rows = conn.execute("SELECT vm_key, folder_path, source, seen_at, scope FROM memberships")
    out = {r["vm_key"]: {"folder_path": r["folder_path"], "source": r["source"],
                         "seen_at": r["seen_at"], "scope": r["scope"]} for r in rows}
    if keys is not None:
        keys = set(keys)
        out = {k: v for k, v in out.items() if k in keys}
    return out


def upsert_membership(conn, key, path, source, seen_at=None, scope=""):
    """Range `key` dans `path` (source « vm » ou « console ») ; `path` None
    retire le rangement."""
    if path is None:
        conn.execute("DELETE FROM memberships WHERE vm_key = ?", (key,))
        return
    if source not in fm.SOURCES:
        raise ValueError(f"unknown membership source: {source}")
    conn.execute("INSERT INTO memberships (vm_key, folder_path, source, seen_at, scope) VALUES (?, ?, ?, ?, ?) "
                 "ON CONFLICT(vm_key) DO UPDATE SET folder_path = excluded.folder_path, "
                 "source = excluded.source, seen_at = excluded.seen_at, "
                 "scope = CASE WHEN excluded.scope = '' THEN memberships.scope ELSE excluded.scope END",
                 (key, path, source, seen_at or _now(), scope or ""))


def _iso(t):
    return t if isinstance(t, str) else t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_s(older, newer):
    import datetime as _dt
    f = "%Y-%m-%dT%H:%M:%SZ"
    try:
        return (_dt.datetime.strptime(newer, f) - _dt.datetime.strptime(older, f)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def realign(conn, rows, now=None):
    """Réaligne le cache sur une lecture d'objets (folders.reconcile) et
    crée les dossiers nommés par les objets (origine « objects », sans
    plafond : décision 90 du propriétaire du produit). Rend {clé: (dossier,
    source)}. Écrit seulement ce qui a changé, ou un `seen_at` de plus d'une
    heure."""
    now = _iso(now) if now else _now()
    with _write(conn):
        tree = list_tree(conn)
        renames = active_renames(conn, now)
        if renames:
            # renommage en cours : un objet pas encore réécrit ne recrée pas
            # l'ancien dossier, il est montré sous le nouveau
            known = set(fm.index(tree))
            rows = [dict(r, folder=fm.remap(r["folder"], renames, known)) if r.get("folder") else r
                    for r in rows]
        cache = memberships(conn, [r["key"] for r in rows if r.get("key")])
        upd, created = fm.reconcile(tree, rows, cache, now)
        idx = fm.index(tree)
        for path in created:
            parent = idx.get(fm.parent_path(path)) if fm.parent_path(path) else None
            try:
                idx[path] = _insert(conn, parent, fm.split(path)[-1], "", "objects")
            except FolderError:                   # recréé au même moment : repris
                idx[path] = conn.execute(
                    "SELECT id FROM folders WHERE IFNULL(parent_id, 0) = ? AND name = ?",
                    (parent or 0, fm.split(path)[-1])).fetchone()[0]
        for key, m in upd.items():
            old = cache.get(key)
            if m is None:
                conn.execute("DELETE FROM memberships WHERE vm_key = ?", (key,))
                continue
            if old and old["folder_path"] == m["folder_path"] and old["source"] == m["source"] \
                    and old["scope"] == (m.get("scope") or old["scope"]) \
                    and _age_s(old["seen_at"], now) < SEEN_REFRESH_S:
                continue
            upsert_membership(conn, key, m["folder_path"], m["source"], now, m.get("scope") or "")
        return fm.effective(rows, memberships(conn, [r["key"] for r in rows if r.get("key")]))


# Une lecture de flotte partie avant la fin d'un renommage (kubectl, SSH :
# quelques secondes) rend encore l'ancien chemin après lui : il est ramené
# sur le nouveau pendant ce délai, au lieu de recréer l'ancien dossier.
RENAME_GRACE_S = 120


def active_renames(conn, now=None, grace_s=RENAME_GRACE_S):
    """[(ancien chemin, nouveau)] des renommages et déplacements de dossier
    faits dont des lignes d'objets de la même action restent à faire
    (`pending`, `failed`), ou faits depuis moins de `grace_s` secondes
    (lecture partie avant leur fin), dans l'ordre du journal."""
    t = time.time() if now is None else (calendar.timegm(time.strptime(now, "%Y-%m-%dT%H:%M:%SZ"))
                                         if isinstance(now, str) else now.timestamp())
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t - grace_s))
    return [(r["from_path"], r["to_path"]) for r in conn.execute(
        "SELECT f.from_path, f.to_path FROM moves f WHERE f.object_key LIKE 'folder/%' "
        "AND f.status = 'done' AND f.to_path IS NOT NULL AND (f.at >= ? OR EXISTS (SELECT 1 FROM moves o "
        "WHERE o.action_id = f.action_id AND o.object_key NOT LIKE 'folder/%' AND o.status IN ('pending', 'failed'))) "
        "ORDER BY f.at, f.rowid", (since,))]


def purge(conn, now=None, days=PURGE_DAYS, scopes=None):
    """Retire les rangements non vus depuis `days` jours. `scopes` : les
    lectures qui ont réussi (seules leurs entrées sont purgées : un hôte
    injoignable garde les siennes jusqu'à sa prochaine lecture) ; None =
    toutes. Rend le nombre d'entrées retirées."""
    import datetime as _dt
    now = _iso(now) if now else _now()
    cutoff = (_dt.datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") - _dt.timedelta(days=days)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    with _write(conn):
        if scopes is None:
            cur = conn.execute("DELETE FROM memberships WHERE seen_at < ?", (cutoff,))
        else:
            scopes = list(scopes)
            if not scopes:
                return 0
            marks = ",".join("?" * len(scopes))
            cur = conn.execute(f"DELETE FROM memberships WHERE seen_at < ? AND scope IN ({marks})",
                               (cutoff, *scopes))
        return cur.rowcount


# --- journal des écritures -------------------------------------------------------

def record_move(conn, action_id, seq, key, from_path, to_path, status="pending", message=""):
    """Ajoute (ou met à jour) la ligne `seq` du journal de l'action."""
    if status not in MOVE_STATUSES:
        raise ValueError(f"unknown move status: {status}")
    conn.execute("INSERT INTO moves (action_id, seq, object_key, from_path, to_path, status, message, at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(action_id, seq) DO UPDATE SET "
                 "status = excluded.status, message = excluded.message, at = excluded.at",
                 (action_id, seq, key, from_path, to_path, status, str(message)[:500], _now()))


def set_move_from(conn, action_id, seq, from_path):
    """Valeur d'avant d'une ligne, relue sur l'objet juste avant de l'écrire
    (« Annuler » remet la valeur exacte, pas celle du cache)."""
    conn.execute("UPDATE moves SET from_path = ? WHERE action_id = ? AND seq = ?",
                 (from_path, action_id, seq))


def set_move_status(conn, action_id, seq, status, message=""):
    record_move(conn, action_id, seq, "", None, None, status, message)


def moves_of(conn, action_id):
    """Lignes du journal d'une action, dans l'ordre."""
    return [dict(r) for r in conn.execute(
        "SELECT action_id, seq, object_key, from_path, to_path, status, message, at FROM moves "
        "WHERE action_id = ? ORDER BY seq", (action_id,))]


def unfinished(conn, action_id):
    """L'action a-t-elle encore une ligne `pending` ou `failed` (ce que
    `resume` reprend) ?"""
    return conn.execute("SELECT 1 FROM moves WHERE action_id = ? AND status IN ('pending', 'failed') LIMIT 1",
                        (action_id,)).fetchone() is not None


def pending_moves(conn, action_id):
    """Lignes encore `pending` d'une action (reprise après interruption)."""
    return [m for m in moves_of(conn, action_id) if m["status"] == "pending"]
