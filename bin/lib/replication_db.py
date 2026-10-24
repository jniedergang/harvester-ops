"""harvester-ops : le registre de la réplication asynchrone (v1.97.0).

Une base SQLite de la console, `replication.db`, dans le répertoire d'état
(ou HARVESTER_OPS_REPLICATION_DB), en mode WAL, fichier 0600. Seule la ligne
de commande `bin/harvester-replication.py` y écrit ; la console la lit (et son
fil de suivi lance la ligne de commande).

Migrations additives seulement : chaque table est décrite par ses colonnes,
créée si elle manque, et complétée des colonnes qui manquent à une base créée
par une version précédente (ALTER TABLE ADD COLUMN, valeur par défaut).

Les secrets ne sont jamais rangés ici (le carnet de reprise vit sur la
cible). Rien n'est jamais écrit sur un cluster.
"""

import json
import os
import socket
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cluster_decl
import replication as rp

RETENTION_DAYS = 400

# (nom, déclaration). Une colonne ajoutée plus tard va EN FIN de liste, avec
# une valeur par défaut : c'est ce que la migration sait poser.
TABLES = {
    "plans": [
        ("id", "TEXT PRIMARY KEY"),
        ("name", "TEXT NOT NULL UNIQUE"),
        ("source_host_kind", "TEXT NOT NULL DEFAULT 'harvester'"),
        ("target_host_kind", "TEXT NOT NULL DEFAULT 'harvester'"),
        ("source_cluster", "TEXT NOT NULL"),
        ("target_cluster", "TEXT NOT NULL"),
        ("state", "TEXT NOT NULL DEFAULT 'draft'"),
        ("generation", "INTEGER NOT NULL DEFAULT 1"),
        ("active_side", "TEXT NOT NULL DEFAULT 'source'"),
        ("rpo_objective_s", "INTEGER NOT NULL"),
        ("interval_s", "INTEGER"),
        ("cron", "TEXT NOT NULL DEFAULT ''"),
        ("retain", "INTEGER NOT NULL DEFAULT 6"),
        ("mappings", "TEXT NOT NULL DEFAULT '{}'"),
        ("options", "TEXT NOT NULL DEFAULT '{}'"),
        ("selector", "TEXT NOT NULL DEFAULT ''"),
        ("created_by", "TEXT NOT NULL DEFAULT ''"),
        ("created_at", "TEXT NOT NULL"),
        ("updated_at", "TEXT NOT NULL"),
        # VMs nommées à la création (`--vm ns/nom`), et namespace du sélecteur
        ("vms", "TEXT NOT NULL DEFAULT '[]'"),
        ("selector_namespace", "TEXT NOT NULL DEFAULT ''"),
    ],
    "plan_resources": [
        ("plan_id", "TEXT NOT NULL"),
        ("generation", "INTEGER NOT NULL DEFAULT 1"),
        ("resource_kind", "TEXT NOT NULL DEFAULT 'vm'"),
        ("resource_uid", "TEXT NOT NULL"),
        ("source_host", "TEXT NOT NULL"),
        ("target_host", "TEXT NOT NULL"),
        ("namespace", "TEXT NOT NULL"),
        ("name", "TEXT NOT NULL"),
        ("target_namespace", "TEXT NOT NULL DEFAULT ''"),
        ("target_name", "TEXT NOT NULL DEFAULT ''"),
        ("vm_generation", "INTEGER"),
        ("firmware_uuid", "TEXT NOT NULL DEFAULT ''"),
        ("state", "TEXT NOT NULL DEFAULT 'draft'"),
        ("state_since", "TEXT NOT NULL DEFAULT ''"),
    ],
    "plan_volumes": [
        ("plan_id", "TEXT NOT NULL"),
        ("generation", "INTEGER NOT NULL DEFAULT 1"),
        ("vm_uid", "TEXT NOT NULL"),
        ("disk", "TEXT NOT NULL"),
        ("claim", "TEXT NOT NULL DEFAULT ''"),
        ("source_volume", "TEXT NOT NULL DEFAULT ''"),
        ("size", "INTEGER NOT NULL DEFAULT 0"),
        ("access_mode", "TEXT NOT NULL DEFAULT ''"),
        ("migratable", "INTEGER NOT NULL DEFAULT 0"),
        ("backing_image", "TEXT NOT NULL DEFAULT ''"),
        ("target_volume", "TEXT NOT NULL DEFAULT ''"),
        ("target_class", "TEXT NOT NULL DEFAULT ''"),
        ("target_replicas", "INTEGER NOT NULL DEFAULT 0"),
        ("backup_volume_source", "TEXT NOT NULL DEFAULT ''"),
        ("backup_volume_target", "TEXT NOT NULL DEFAULT ''"),
        ("state", "TEXT NOT NULL DEFAULT 'pending'"),
    ],
    "ticks": [
        ("plan_id", "TEXT NOT NULL"),
        ("generation", "INTEGER NOT NULL DEFAULT 1"),
        ("source_volume", "TEXT NOT NULL"),
        ("backup", "TEXT NOT NULL"),
        ("snapshot_at", "TEXT NOT NULL DEFAULT ''"),
        ("backup_started_at", "TEXT NOT NULL DEFAULT ''"),
        ("backup_done_at", "TEXT NOT NULL DEFAULT ''"),
        ("newly_uploaded", "INTEGER NOT NULL DEFAULT 0"),
        ("size", "INTEGER NOT NULL DEFAULT 0"),
        ("resync_at", "TEXT NOT NULL DEFAULT ''"),
        ("applied_at", "TEXT NOT NULL DEFAULT ''"),
        ("state", "TEXT NOT NULL DEFAULT 'completed'"),
        ("skew_s", "REAL"),
        ("vm_uid", "TEXT NOT NULL DEFAULT ''"),
    ],
    "measures": [
        ("plan_id", "TEXT NOT NULL"),
        ("generation", "INTEGER NOT NULL DEFAULT 1"),
        ("source_volume", "TEXT NOT NULL"),
        ("backup", "TEXT NOT NULL"),
        ("snapshot_at", "TEXT NOT NULL DEFAULT ''"),
        ("backup_started_at", "TEXT NOT NULL DEFAULT ''"),
        ("backup_done_at", "TEXT NOT NULL DEFAULT ''"),
        ("newly_uploaded", "INTEGER NOT NULL DEFAULT 0"),
        ("size", "INTEGER NOT NULL DEFAULT 0"),
        ("state", "TEXT NOT NULL DEFAULT 'completed'"),
        ("skew_s", "REAL"),
        ("vm_uid", "TEXT NOT NULL DEFAULT ''"),
    ],
    "events": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("ts", "TEXT NOT NULL"),
        ("plan_id", "TEXT NOT NULL DEFAULT ''"),
        ("resource_uid", "TEXT NOT NULL DEFAULT ''"),
        ("host", "TEXT NOT NULL DEFAULT ''"),
        ("side", "TEXT NOT NULL DEFAULT ''"),
        ("operation", "TEXT NOT NULL"),
        ("step", "TEXT NOT NULL DEFAULT ''"),
        ("level", "TEXT NOT NULL DEFAULT 'info'"),
        ("code", "TEXT NOT NULL DEFAULT ''"),
        ("message", "TEXT NOT NULL DEFAULT ''"),
        ("facts", "TEXT NOT NULL DEFAULT '{}'"),
        ("by", "TEXT NOT NULL DEFAULT ''"),
        ("action_id", "TEXT NOT NULL DEFAULT ''"),
    ],
    "runs": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("plan_id", "TEXT NOT NULL"),
        ("generation", "INTEGER NOT NULL DEFAULT 1"),
        ("operation", "TEXT NOT NULL"),
        ("started_at", "TEXT NOT NULL"),
        ("finished_at", "TEXT NOT NULL DEFAULT ''"),
        ("result", "TEXT NOT NULL DEFAULT ''"),
        ("vms", "TEXT NOT NULL DEFAULT '[]'"),
        ("points", "TEXT NOT NULL DEFAULT '{}'"),
        ("steps", "TEXT NOT NULL DEFAULT '[]'"),
        ("by", "TEXT NOT NULL DEFAULT ''"),
        ("action_id", "TEXT NOT NULL DEFAULT ''"),
    ],
    "leases": [
        ("plan_id", "TEXT PRIMARY KEY"),
        ("owner", "TEXT NOT NULL"),
        ("heartbeat", "TEXT NOT NULL"),
        ("ttl_s", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "locks": [
        ("plan_id", "TEXT PRIMARY KEY"),
        ("command", "TEXT NOT NULL"),
        ("owner", "TEXT NOT NULL"),
        ("started_at", "TEXT NOT NULL"),
    ],
}
KEYS = {
    "plan_resources": ("plan_id", "generation", "resource_uid"),
    "plan_volumes": ("plan_id", "generation", "vm_uid", "disk"),
    "ticks": ("plan_id", "generation", "source_volume", "backup"),
    "measures": ("plan_id", "generation", "source_volume", "backup"),
}
INDEXES = (
    "CREATE INDEX IF NOT EXISTS resources_source ON plan_resources (source_host)",
    "CREATE INDEX IF NOT EXISTS resources_target ON plan_resources (target_host)",
    "CREATE INDEX IF NOT EXISTS events_resource ON events (resource_uid, ts)",
    "CREATE INDEX IF NOT EXISTS events_host ON events (host, ts)",
    "CREATE INDEX IF NOT EXISTS events_plan ON events (plan_id, ts)",
    "CREATE INDEX IF NOT EXISTS ticks_plan ON ticks (plan_id, generation, snapshot_at)",
    "CREATE INDEX IF NOT EXISTS measures_plan ON measures (plan_id, generation, snapshot_at)",
)
_JSON = {"plans": ("mappings", "options", "vms"), "events": ("facts",),
         "runs": ("vms", "points", "steps")}


class StateConflict(Exception):
    """Transition refusée : l'état réel n'est pas celui attendu, ou la
    transition n'existe pas."""

    def __init__(self, plan_id, current, new, reason):
        super().__init__(f"plan {plan_id}: {reason} ({current} to {new})")
        self.plan_id, self.current, self.new, self.reason = plan_id, current, new, reason


class NameTaken(ValueError):
    pass


class Locked(Exception):
    """Une autre commande mutative tient le plan."""

    def __init__(self, plan_id, holder):
        super().__init__(f"plan {plan_id} is busy: {holder.get('command')} by {holder.get('owner')}")
        self.plan_id, self.holder = plan_id, holder


# ---------------------------------------------------------------------------
# Connexion et schéma
# ---------------------------------------------------------------------------

def default_path():
    """HARVESTER_OPS_REPLICATION_DB, sinon `replication.db` dans le
    répertoire d'état."""
    env = os.environ.get("HARVESTER_OPS_REPLICATION_DB")
    return Path(env) if env else cluster_decl.state_dir() / "replication.db"


def _create_sql(table, cols):
    parts = [f'"{n}" {decl}' for n, decl in cols]
    if table in KEYS:
        parts.append("PRIMARY KEY (" + ", ".join(f'"{k}"' for k in KEYS[table]) + ")")
    return f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(parts)})'


def _add_sql(table, name, decl):
    # ADD COLUMN refuse PRIMARY KEY et UNIQUE : seules les colonnes ajoutées
    # après coup (avec une valeur par défaut) passent par ici
    return f'ALTER TABLE "{table}" ADD COLUMN "{name}" {decl}'


def migrate(conn):
    """Crée les tables qui manquent et ajoute les colonnes qui manquent.
    Rend la liste des colonnes ajoutées (`table.colonne`)."""
    added = []
    for table, cols in TABLES.items():
        conn.execute(_create_sql(table, cols))
        have = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        for name, decl in cols:
            if name not in have:
                conn.execute(_add_sql(table, name, decl))
                added.append(f"{table}.{name}")
    for sql in INDEXES:
        conn.execute(sql)
    return added


def connect(path=None):
    path = Path(path or default_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("BEGIN IMMEDIATE")
    try:
        migrate(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


class _tx:
    """BEGIN IMMEDIATE ... COMMIT, ROLLBACK sur toute exception."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def _now(now):
    return now if now is not None else datetime.now(timezone.utc)


def _iso(dt):
    return rp.iso(dt)


def _event_ts(dt):
    """Horodatage d'événement à la microseconde (plusieurs étapes par
    seconde) ; même forme pour toutes les lignes : l'ordre des chaînes est
    celui du temps."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(table, r):
    if r is None:
        return None
    d = dict(r)
    for k in _JSON.get(table, ()):
        if k in d:
            try:
                d[k] = json.loads(d[k] or "null")
            except ValueError:
                d[k] = None
    if table == "plan_volumes" and "migratable" in d:
        d["migratable"] = bool(d["migratable"])
    return d


def _insert(conn, table, values, upsert=False):
    cols = list(values)
    sql = (f'INSERT INTO "{table}" (' + ", ".join(f'"{c}"' for c in cols) + ") VALUES ("
           + ", ".join("?" for _ in cols) + ")")
    if upsert:
        keys = KEYS[table]
        rest = [c for c in cols if c not in keys]
        sql += " ON CONFLICT (" + ", ".join(f'"{k}"' for k in keys) + ") DO " + (
            "UPDATE SET " + ", ".join(f'"{c}" = excluded."{c}"' for c in rest) if rest else "NOTHING")
    conn.execute(sql, [json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
                       for v in values.values()])


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def new_plan_id():
    return "rp-" + uuid.uuid4().hex[:12]


def insert_plan(conn, plan, created_by="", now=None, plan_id=None):
    """Range un plan normalisé (`replication.normalize_plan`), état `draft`,
    génération 1. NameTaken si le nom existe déjà."""
    pid = plan_id or new_plan_id()
    if not rp.PLAN_ID_RE.match(pid):
        raise ValueError("invalid plan id")
    t = _iso(_now(now))
    values = {
        "id": pid, "name": plan["name"],
        "source_host_kind": plan.get("source_host_kind") or "harvester",
        "target_host_kind": plan.get("target_host_kind") or "harvester",
        "source_cluster": plan["source_cluster"], "target_cluster": plan["target_cluster"],
        "state": "draft", "generation": 1, "active_side": "source",
        "rpo_objective_s": int(plan["rpo_objective_s"]), "interval_s": plan.get("interval_s"),
        "cron": plan.get("cron") or "", "retain": int(plan.get("retain") or rp.DEFAULT_RETAIN),
        "mappings": plan.get("mappings") or {}, "options": plan.get("options") or {},
        "selector": plan.get("selector") or "", "selector_namespace": plan.get("selector_namespace") or "",
        "vms": plan.get("vms") or [], "created_by": created_by or "", "created_at": t, "updated_at": t,
    }
    with _tx(conn):
        if conn.execute("SELECT 1 FROM plans WHERE name = ?", (plan["name"],)).fetchone():
            raise NameTaken(f"plan name {plan['name']} is already used")
        _insert(conn, "plans", values)
    return get_plan(conn, pid)


def get_plan(conn, plan_id):
    return _row("plans", conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone())


def get_plan_by_name(conn, name):
    return _row("plans", conn.execute("SELECT * FROM plans WHERE name = ?", (name,)).fetchone())


def list_plans(conn, host=None):
    """Tous les plans, par nom ; `host` : ceux dont il est la source ou la
    cible."""
    q, args = "SELECT * FROM plans", []
    if host:
        q += " WHERE source_cluster = ? OR target_cluster = ?"
        args = [host, host]
    return [_row("plans", r) for r in conn.execute(q + " ORDER BY name", args)]


_SETTABLE = ("rpo_objective_s", "interval_s", "cron", "retain", "mappings", "options",
             "selector", "selector_namespace", "vms", "generation", "active_side")


def update_plan(conn, plan_id, now=None, **fields):
    """Réglages modifiables (lus à chaud par le suiveur). L'état ne passe
    jamais par ici (voir `set_state`)."""
    bad = [k for k in fields if k not in _SETTABLE]
    if bad:
        raise ValueError(f"not settable: {', '.join(bad)}")
    if not fields:
        return get_plan(conn, plan_id)
    sets = ", ".join(f'"{k}" = ?' for k in fields)
    vals = [json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v for v in fields.values()]
    with _tx(conn):
        cur = conn.execute(f"UPDATE plans SET {sets}, updated_at = ? WHERE id = ?",
                           vals + [_iso(_now(now)), plan_id])
        if not cur.rowcount:
            raise KeyError(plan_id)
    return get_plan(conn, plan_id)


def delete_plan(conn, plan_id):
    """Efface un plan `draft` ou `unprotected` et ses lignes (le journal
    reste, il est effacé par `prune` après 400 jours)."""
    with _tx(conn):
        p = conn.execute("SELECT state FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not p:
            return False
        if p["state"] not in ("draft", "unprotected"):
            raise StateConflict(plan_id, p["state"], "deleted", "only a draft or unprotected plan can be deleted")
        for t in ("plan_resources", "plan_volumes", "ticks", "measures", "runs", "leases", "locks"):
            conn.execute(f'DELETE FROM "{t}" WHERE plan_id = ?', (plan_id,))
        conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    return True


def error_origin(conn, plan_id):
    """L'état d'avant le dernier passage en `error` (journal du plan)."""
    r = conn.execute("SELECT facts FROM events WHERE plan_id = ? AND operation = 'plan' AND step = 'state'"
                     " AND code = 'error' ORDER BY ts DESC, id DESC LIMIT 1", (plan_id,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r["facts"] or "{}").get("from")
    except ValueError:
        return None


def set_state(conn, plan_id, expected, new, now=None, by="", action_id="", message=""):
    """Passe le plan de `expected` (un état ou une liste) à `new`, dans une
    seule transaction. StateConflict si l'état réel n'est pas attendu ou si
    la transition n'existe pas ; depuis `error`, seules les transitions de
    l'état d'origine (ou son retour) sont permises. Chaque passage écrit un
    événement `plan/state` (code = nouvel état, facts {from, to})."""
    if new not in rp.PLAN_STATES:
        raise ValueError(f"unknown state {new}")
    exp = (expected,) if isinstance(expected, str) else tuple(expected)
    with _tx(conn):
        r = conn.execute("SELECT state, source_cluster FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not r:
            raise KeyError(plan_id)
        cur = r["state"]
        if cur not in exp:
            raise StateConflict(plan_id, cur, new, f"state is {cur}, expected {'|'.join(exp)}")
        allowed = rp.PLAN_TRANSITIONS.get(cur, ())
        if cur == "error":
            origin = error_origin(conn, plan_id)
            if origin in rp.PLAN_TRANSITIONS:
                allowed = tuple(s for s in allowed if s == origin or s in rp.PLAN_TRANSITIONS[origin])
        if new not in allowed:
            raise StateConflict(plan_id, cur, new, "transition not allowed")
        t = _now(now)
        conn.execute("UPDATE plans SET state = ?, updated_at = ? WHERE id = ?", (new, _iso(t), plan_id))
        _insert(conn, "events", {"ts": _event_ts(t), "plan_id": plan_id, "resource_uid": "",
                                 "host": "", "side": "", "operation": "plan", "step": "state",
                                 "level": "error" if new == "error" else "info", "code": new,
                                 "message": (message or f"{cur} to {new}")[:500],
                                 "facts": {"from": cur, "to": new}, "by": by or "", "action_id": action_id or ""})
    return get_plan(conn, plan_id)


# ---------------------------------------------------------------------------
# Ressources et volumes
# ---------------------------------------------------------------------------

def upsert_resource(conn, plan_id, resource, now=None):
    """Range (ou met à jour) une ressource protégée ; clé (plan, génération,
    uid). L'état n'est changé que par `set_resource_state`, sauf à la
    création (`draft` par défaut)."""
    gen = int(resource.get("generation") or 1)
    uid = resource["resource_uid"]
    vals = {"plan_id": plan_id, "generation": gen, "resource_kind": resource.get("resource_kind") or "vm",
            "resource_uid": uid, "source_host": resource["source_host"], "target_host": resource["target_host"],
            "namespace": resource["namespace"], "name": resource["name"],
            "target_namespace": resource.get("target_namespace") or "",
            "target_name": resource.get("target_name") or "", "vm_generation": resource.get("vm_generation"),
            "firmware_uuid": resource.get("firmware_uuid") or ""}
    if vals["resource_kind"] not in rp.RESOURCE_KINDS:
        raise ValueError(f"unknown resource kind {vals['resource_kind']}")
    with _tx(conn):
        exists = conn.execute("SELECT 1 FROM plan_resources WHERE plan_id = ? AND generation = ? AND"
                              " resource_uid = ?", (plan_id, gen, uid)).fetchone()
        if not exists:
            state = resource.get("state") or "draft"
            if state not in rp.STORED_RESOURCE_STATES:
                raise ValueError(f"unknown resource state {state}")
            vals.update(state=state, state_since=_iso(_now(now)))
        _insert(conn, "plan_resources", vals, upsert=True)
    return get_resource(conn, plan_id, uid, gen)


def get_resource(conn, plan_id, resource_uid, generation=None):
    q = "SELECT * FROM plan_resources WHERE plan_id = ? AND resource_uid = ?"
    args = [plan_id, resource_uid]
    if generation is not None:
        q += " AND generation = ?"
        args.append(int(generation))
    return _row("plan_resources", conn.execute(q + " ORDER BY generation DESC LIMIT 1", args).fetchone())


def list_resources(conn, plan_id, generation=None):
    q, args = "SELECT * FROM plan_resources WHERE plan_id = ?", [plan_id]
    if generation is not None:
        q += " AND generation = ?"
        args.append(int(generation))
    return [_row("plan_resources", r) for r in conn.execute(q + " ORDER BY namespace, name", args)]


def set_resource_state(conn, plan_id, resource_uid, state, generation=None, now=None):
    """Change l'état rangé d'une ressource (états du cycle de vie seulement :
    `lagging`, `drifting`, `degraded` et `source-unreachable` se calculent).
    `state_since` ne bouge que si l'état change. Rend la ressource."""
    if state not in rp.STORED_RESOURCE_STATES:
        raise ValueError(f"not a stored resource state: {state}")
    with _tx(conn):
        r = get_resource(conn, plan_id, resource_uid, generation)
        if not r:
            raise KeyError(resource_uid)
        if r["state"] != state:
            conn.execute("UPDATE plan_resources SET state = ?, state_since = ? WHERE plan_id = ? AND"
                         " generation = ? AND resource_uid = ?",
                         (state, _iso(_now(now)), plan_id, r["generation"], resource_uid))
    return get_resource(conn, plan_id, resource_uid, generation)


def list_resources_by_host(conn, host, side="any"):
    """Les ressources d'un hôte, vu comme source, comme cible ou des deux
    côtés. Chaque ligne porte `side` (côté de cet hôte), `peer_host`, et le
    plan (nom, état). Seule la dernière génération de chaque plan compte."""
    if side not in ("source", "target", "any"):
        raise ValueError("side: source, target or any")
    out = []
    for s, col, peer in (("source", "source_host", "target_host"), ("target", "target_host", "source_host")):
        if side not in (s, "any"):
            continue
        rows = conn.execute(
            f"SELECT r.*, p.name AS plan_name, p.state AS plan_state FROM plan_resources r"
            f" JOIN plans p ON p.id = r.plan_id AND p.generation = r.generation"
            f" WHERE r.{col} = ? ORDER BY p.name, r.namespace, r.name", (host,)).fetchall()
        for r in rows:
            d = _row("plan_resources", r)
            d.update(side=s, peer_host=d[peer])
            out.append(d)
    return out


def upsert_volume(conn, plan_id, volume):
    """Range (ou met à jour) un disque protégé ; clé (plan, génération, VM,
    disque)."""
    state = volume.get("state") or "pending"
    if state not in rp.VOLUME_STATES:
        raise ValueError(f"unknown volume state {state}")
    vals = {"plan_id": plan_id, "generation": int(volume.get("generation") or 1)}
    for col, _decl in TABLES["plan_volumes"]:
        if col in ("plan_id", "generation"):
            continue
        if col in volume:
            vals[col] = int(bool(volume[col])) if col == "migratable" else volume[col]
    vals["state"] = state
    for k in ("vm_uid", "disk"):
        if not vals.get(k):
            raise ValueError(f"volume: {k} is required")
    with _tx(conn):
        _insert(conn, "plan_volumes", vals, upsert=True)
    return list_volumes(conn, plan_id, vals["generation"], vm_uid=vals["vm_uid"], disk=vals["disk"])[0]


def list_volumes(conn, plan_id, generation=None, vm_uid=None, disk=None):
    q, args = "SELECT * FROM plan_volumes WHERE plan_id = ?", [plan_id]
    for col, v in (("generation", generation), ("vm_uid", vm_uid), ("disk", disk)):
        if v is not None:
            q += f" AND {col} = ?"
            args.append(v)
    return [_row("plan_volumes", r) for r in conn.execute(q + " ORDER BY vm_uid, disk", args)]


# ---------------------------------------------------------------------------
# Passages (ticks) et mesures
# ---------------------------------------------------------------------------

def _record_pass(conn, table, plan_id, tick):
    state = tick.get("state") or "completed"
    if state not in rp.TICK_STATES:
        raise ValueError(f"unknown tick state {state}")
    vals = {"plan_id": plan_id, "generation": int(tick.get("generation") or 1)}
    for col, _decl in TABLES[table]:
        if col in ("plan_id", "generation", "state"):
            continue
        if col in tick and tick[col] is not None:
            vals[col] = tick[col]
    vals["state"] = state
    for k in ("source_volume", "backup"):
        if not vals.get(k):
            raise ValueError(f"tick: {k} is required")
    with _tx(conn):
        # un passage déjà appliqué ne redescend jamais (relecture d'un
        # Backup déjà vu par un tour précédent)
        old = conn.execute(f'SELECT state FROM "{table}" WHERE plan_id = ? AND generation = ? AND'
                           " source_volume = ? AND backup = ?",
                           (plan_id, vals["generation"], vals["source_volume"], vals["backup"])).fetchone()
        if old and old["state"] == "applied" and table == "ticks":
            vals.pop("state")
            vals.pop("applied_at", None)
        _insert(conn, table, vals, upsert=True)


def record_tick(conn, plan_id, tick):
    """Range (idempotent) une sauvegarde suivie ; clé (plan, génération,
    volume source, sauvegarde)."""
    _record_pass(conn, "ticks", plan_id, tick)


def record_measure(conn, plan_id, measure):
    """Range un passage de la phase de mesure (même forme, sans cible)."""
    _record_pass(conn, "measures", plan_id, measure)


def mark_applied(conn, plan_id, source_volume, backup, applied_at, generation=1, resync_at=None):
    """Note qu'une sauvegarde est appliquée sur le volume de secours. Rend
    False si le passage est inconnu."""
    sets, args = ["state = 'applied'", "applied_at = ?"], [_iso(applied_at)]
    if resync_at:
        sets.append("resync_at = ?")
        args.append(_iso(resync_at))
    with _tx(conn):
        cur = conn.execute(f"UPDATE ticks SET {', '.join(sets)} WHERE plan_id = ? AND generation = ? AND"
                           " source_volume = ? AND backup = ?",
                           args + [plan_id, int(generation), source_volume, backup])
    return cur.rowcount > 0


def list_ticks(conn, plan_id, generation=None, since=None, table="ticks"):
    if table not in ("ticks", "measures"):
        raise ValueError("table: ticks or measures")
    q, args = f'SELECT * FROM "{table}" WHERE plan_id = ?', [plan_id]
    if generation is not None:
        q += " AND generation = ?"
        args.append(int(generation))
    if since:
        q += " AND snapshot_at >= ?"
        args.append(_iso(since))
    return [dict(r) for r in conn.execute(q + " ORDER BY snapshot_at, source_volume", args)]


# ---------------------------------------------------------------------------
# Journal persistant par ressource
# ---------------------------------------------------------------------------

EVENT_OPERATIONS = ("protect", "measure", "follow", "failover", "unprotect", "plan")
EVENT_LEVELS = ("info", "warn", "error")


def record_event(conn, operation, step="", plan_id="", resource_uid="", host="", side="", level="info",
                 code="", message="", facts=None, by="", action_id="", now=None):
    """Une ligne du journal : une étape d'une opération pour une ressource
    (ou le plan entier si `resource_uid` est vide). Rend son id."""
    if operation not in EVENT_OPERATIONS:
        raise ValueError(f"unknown operation {operation}")
    if level not in EVENT_LEVELS:
        raise ValueError(f"unknown level {level}")
    if side not in ("", "source", "target"):
        raise ValueError("side: source or target")
    with _tx(conn):
        cur = conn.execute(
            'INSERT INTO events (ts, plan_id, resource_uid, host, side, operation, step, level, code, message,'
            ' facts, "by", action_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (_event_ts(_now(now)), plan_id or "", resource_uid or "", host or "", side or "", operation,
             step or "", level, code or "", (message or "")[:500], json.dumps(facts or {}, sort_keys=True),
             by or "", action_id or ""))
    return cur.lastrowid


def _events(conn, where, args, limit, before):
    limit = max(1, min(int(limit or 50), 500))
    q = f"SELECT * FROM events WHERE {where}"
    if isinstance(before, int) and not isinstance(before, bool):
        q += " AND id < ?"
        args = args + [before]
    elif before:
        q += " AND ts < ?"
        args = args + [_event_ts(before)]
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    return [_row("events", r) for r in conn.execute(q, args + [limit])]


def resource_events(conn, resource_uid, limit=50, before=None):
    """Le journal d'une ressource, du plus récent au plus ancien. `before` :
    une date (page suivante : la date du dernier rendu) ou l'id du dernier
    événement rendu (curseur exact quand plusieurs partagent la même date)."""
    return _events(conn, "resource_uid = ?", [resource_uid], limit, before)


def plan_events(conn, plan_id, limit=50, before=None):
    return _events(conn, "plan_id = ?", [plan_id], limit, before)


def host_events(conn, host, limit=50, before=None):
    return _events(conn, "host = ?", [host], limit, before)


# ---------------------------------------------------------------------------
# Bascules et déprotections
# ---------------------------------------------------------------------------

def record_run(conn, plan_id, operation, started_at, generation=1, finished_at=None, result="",
               vms=(), points=None, steps=(), by="", action_id="", run_id=None):
    """Range (ou complète, avec `run_id`) une bascule ou une déprotection :
    points de reprise par volume, durée de chaque étape. Rend l'id."""
    if operation not in ("failover", "unprotect"):
        raise ValueError("operation: failover or unprotect")
    vals = {"plan_id": plan_id, "generation": int(generation), "operation": operation,
            "started_at": _iso(started_at), "finished_at": _iso(finished_at) if finished_at else "",
            "result": result or "", "vms": list(vms), "points": points or {}, "steps": list(steps),
            "by": by or "", "action_id": action_id or ""}
    with _tx(conn):
        if run_id:
            sets = ", ".join(f'"{k}" = ?' for k in vals)
            conn.execute(f"UPDATE runs SET {sets} WHERE id = ?",
                         [json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
                          for v in vals.values()] + [run_id])
            return run_id
        _insert(conn, "runs", vals)
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def list_runs(conn, plan_id):
    return [_row("runs", r) for r in conn.execute("SELECT * FROM runs WHERE plan_id = ? ORDER BY started_at, id",
                                                  (plan_id,))]


# ---------------------------------------------------------------------------
# Bail du suiveur et verrou des commandes
# ---------------------------------------------------------------------------

def owner_id(kind, host=None, pid=None):
    """`console:<hôte>:<pid>` ou `cli:<hôte>:<pid>`."""
    if kind not in ("console", "cli"):
        raise ValueError("owner kind: console or cli")
    return f"{kind}:{host or socket.gethostname()}:{pid if pid is not None else os.getpid()}"


def _parse_owner(owner):
    parts = str(owner or "").split(":")
    if len(parts) < 3:
        return None, None
    try:
        return ":".join(parts[1:-1]), int(parts[-1])
    except ValueError:
        return None, None


def take_lease(conn, plan_id, owner, ttl, take_over=False, now=None):
    """Prend (ou renouvelle) le bail d'un plan pour `ttl` secondes. Refusé
    (False) si un autre détenteur l'a renouvelé depuis moins de son `ttl`,
    sauf `take_over`."""
    t = _now(now)
    with _tx(conn):
        r = conn.execute("SELECT * FROM leases WHERE plan_id = ?", (plan_id,)).fetchone()
        if r and r["owner"] != owner and not take_over:
            expires = rp._t(r["heartbeat"]) + timedelta(seconds=int(r["ttl_s"]))
            if expires > rp._t(t):
                return False
        conn.execute("INSERT INTO leases (plan_id, owner, heartbeat, ttl_s) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT (plan_id) DO UPDATE SET owner = excluded.owner,"
                     " heartbeat = excluded.heartbeat, ttl_s = excluded.ttl_s",
                     (plan_id, owner, _iso(t), int(ttl)))
    return True


def heartbeat(conn, plan_id, owner, now=None):
    """Renouvelle un bail tenu ; False s'il a été repris par un autre."""
    with _tx(conn):
        cur = conn.execute("UPDATE leases SET heartbeat = ? WHERE plan_id = ? AND owner = ?",
                           (_iso(_now(now)), plan_id, owner))
    return cur.rowcount > 0


def release_lease(conn, plan_id, owner):
    with _tx(conn):
        cur = conn.execute("DELETE FROM leases WHERE plan_id = ? AND owner = ?", (plan_id, owner))
    return cur.rowcount > 0


def get_lease(conn, plan_id):
    r = conn.execute("SELECT * FROM leases WHERE plan_id = ?", (plan_id,)).fetchone()
    return dict(r) if r else None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(conn, plan_id, command, owner, now=None, pid_alive=_pid_alive, host=None):
    """Une seule commande mutative à la fois par plan. Repris si le
    détenteur tournait sur CET hôte et que son processus n'existe plus ;
    Locked sinon. Le même détenteur peut le reprendre (relance dans le même
    processus)."""
    here = host or socket.gethostname()
    with _tx(conn):
        r = conn.execute("SELECT * FROM locks WHERE plan_id = ?", (plan_id,)).fetchone()
        if r and r["owner"] != owner:
            h, pid = _parse_owner(r["owner"])
            if not (h == here and pid is not None and not pid_alive(pid)):
                raise Locked(plan_id, dict(r))
        conn.execute("INSERT INTO locks (plan_id, command, owner, started_at) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT (plan_id) DO UPDATE SET command = excluded.command,"
                     " owner = excluded.owner, started_at = excluded.started_at",
                     (plan_id, command, owner, _iso(_now(now))))
    return True


def release_lock(conn, plan_id, owner):
    with _tx(conn):
        cur = conn.execute("DELETE FROM locks WHERE plan_id = ? AND owner = ?", (plan_id, owner))
    return cur.rowcount > 0


def get_lock(conn, plan_id):
    r = conn.execute("SELECT * FROM locks WHERE plan_id = ?", (plan_id,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# Rétention
# ---------------------------------------------------------------------------

def prune(conn, before):
    """Efface passages, mesures et événements d'avant `before` (400 jours
    en usage normal). Le dernier passage appliqué de chaque volume est
    gardé : sans lui, le RPO d'un plan figé deviendrait inconnu. Rend
    {"ticks", "measures", "events"} (lignes effacées)."""
    t, te = _iso(before), _event_ts(before)
    with _tx(conn):
        n_ticks = conn.execute(
            "DELETE FROM ticks WHERE snapshot_at < ? AND rowid NOT IN ("
            " SELECT t.rowid FROM ticks t JOIN (SELECT plan_id, generation, source_volume,"
            "  MAX(snapshot_at) AS m FROM ticks WHERE state = 'applied'"
            "  GROUP BY plan_id, generation, source_volume) k"
            " ON t.plan_id = k.plan_id AND t.generation = k.generation AND t.source_volume = k.source_volume"
            " AND t.snapshot_at = k.m AND t.state = 'applied')", (t,)).rowcount
        n_meas = conn.execute("DELETE FROM measures WHERE snapshot_at < ?", (t,)).rowcount
        n_ev = conn.execute("DELETE FROM events WHERE ts < ?", (te,)).rowcount
    return {"ticks": n_ticks, "measures": n_meas, "events": n_ev}


def retention_cutoff(now=None):
    return _now(now) - timedelta(days=RETENTION_DAYS)
