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
import re
import socket
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cluster_decl
import replication as rp

RETENTION_DAYS = 400
# Statistiques gardées passage par passage pendant 30 jours, puis réduites à
# une ligne par heure (et par plan, génération, ressource) jusqu'à
# RETENTION_DAYS. Volume : une ligne par VM et par passage, soit 480 par jour
# à 3 min d'intervalle ; avec la réduction, environ 30 x 480 + 370 x 24 =
# 23 280 lignes par VM au lieu de 192 000.
STATS_FULL_DAYS = 30
# Verrou d'une commande : le détenteur le renouvelle (`lock_heartbeat`) au
# moins toutes les LOCK_TTL_S / 3 secondes ; sans quoi un autre hôte le
# reprend passé ce délai.
LOCK_TTL_S = 600

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
        # état d'où le plan est entré en `error` (vide hors `error`) : rangé
        # ici, le journal étant effacé après 400 jours
        ("error_origin", "TEXT NOT NULL DEFAULT ''"),
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
        # empreinte de la spec vue à la protection (`replication.vm_spec_digest`)
        ("vm_spec_digest", "TEXT NOT NULL DEFAULT ''"),
        ("error_origin", "TEXT NOT NULL DEFAULT ''"),
        # santé relevée par le suiveur (`replication_follow`) : source et
        # cible joignables, volume de secours dégradé, spec relue, VM en
        # marche ; lue par `status` et la console (`health_map`)
        ("health", "TEXT NOT NULL DEFAULT '{}'"),
        ("health_at", "TEXT NOT NULL DEFAULT ''"),
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
        # 1 : l'étiquette `recurring-job-group.longhorn.io/default` a été posée
        # par la réplication (appartenance implicite rendue explicite) ; la
        # déprotection ne retire que celle-là
        ("default_label_added", "INTEGER NOT NULL DEFAULT 0"),
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
    # une ligne par VM et par passage appliqué, écrite par le suiveur : la
    # série que lira la vue de suivi (RPO tenu, arriéré, débits)
    "stats": [
        ("plan_id", "TEXT NOT NULL"),
        ("generation", "INTEGER NOT NULL DEFAULT 1"),
        ("resource_uid", "TEXT NOT NULL"),
        ("pass_at", "TEXT NOT NULL"),
        ("ts", "TEXT NOT NULL"),
        ("disks", "INTEGER NOT NULL DEFAULT 0"),
        ("newly_uploaded", "INTEGER NOT NULL DEFAULT 0"),
        ("backup_s", "REAL"),
        ("apply_lag_s", "REAL"),
        ("resync_to_apply_s", "REAL"),
        ("skew_s", "REAL"),
        ("rpo_held_s", "INTEGER"),
        ("backlog", "INTEGER NOT NULL DEFAULT 0"),
        ("upload_bps", "REAL"),
        ("apply_bps", "REAL"),
        ("state", "TEXT NOT NULL DEFAULT ''"),
        # passages agrégés dans la ligne : 1, ou plus après la réduction à
        # l'heure des statistiques de plus de STATS_FULL_DAYS jours
        ("passes", "INTEGER NOT NULL DEFAULT 1"),
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
        # date de démarrage du processus détenteur (/proc/<pid>/stat) : un PID
        # réutilisé par un autre processus ne garde pas le verrou
        ("pid_start", "TEXT NOT NULL DEFAULT ''"),
        # détenteur sur un autre hôte : repris quand heartbeat + ttl_s est passé
        ("heartbeat", "TEXT NOT NULL DEFAULT ''"),
        ("ttl_s", "INTEGER NOT NULL DEFAULT 0"),
    ],
}
KEYS = {
    "plan_resources": ("plan_id", "generation", "resource_uid"),
    "plan_volumes": ("plan_id", "generation", "vm_uid", "disk"),
    "ticks": ("plan_id", "generation", "source_volume", "backup"),
    "measures": ("plan_id", "generation", "source_volume", "backup"),
    "stats": ("plan_id", "generation", "resource_uid", "pass_at"),
}
INDEXES = (
    "CREATE INDEX IF NOT EXISTS resources_source ON plan_resources (source_host)",
    "CREATE INDEX IF NOT EXISTS resources_target ON plan_resources (target_host)",
    "CREATE INDEX IF NOT EXISTS events_resource ON events (resource_uid, ts)",
    "CREATE INDEX IF NOT EXISTS events_host ON events (host, ts)",
    "CREATE INDEX IF NOT EXISTS events_plan ON events (plan_id, ts)",
    "CREATE INDEX IF NOT EXISTS ticks_plan ON ticks (plan_id, generation, snapshot_at)",
    "CREATE INDEX IF NOT EXISTS measures_plan ON measures (plan_id, generation, snapshot_at)",
    "CREATE INDEX IF NOT EXISTS stats_plan ON stats (plan_id, ts)",
    "CREATE INDEX IF NOT EXISTS stats_resource ON stats (resource_uid, ts)",
)
_JSON = {"plans": ("mappings", "options", "vms"), "events": ("facts",), "plan_resources": ("health",),
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


def _private(path):
    """0600 sur la base et ses fichiers -wal et -shm (SQLite les crée avec
    les droits de la base, on le vérifie quand même)."""
    for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def connect(path=None):
    path = Path(path or default_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    # créée 0600 avant la première ouverture : jamais lisible par d'autres,
    # même un instant
    os.close(os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600))
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
    _private(path)
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


def _generation(value):
    """Génération exigée là où elle fait partie de la clé : jamais de 1 par
    défaut, qui rangerait en silence une ligne dans la mauvaise génération."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("generation is required (a positive integer)")
    return value


# ---------------------------------------------------------------------------
# Aucun chemin au registre
# ---------------------------------------------------------------------------

HIDDEN = "<hidden>"
# chemins connus du processus (kubeconfigs, base, fichiers temporaires...),
# ajoutés par `hide_paths` ; s'y ajoutent à chaque masquage le répertoire
# personnel, le répertoire temporaire, le répertoire d'état et la configuration
_HIDDEN_PATHS = set()
_PATH_TAIL = r"[^\s\"'<>|,;:()\[\]{}]*"
# tout chemin absolu sous /home, /root, /var/lib ou /tmp, même inconnu
_GENERIC_PATH_RE = re.compile(r"(?<![\w.~/:@-])/(?:home|root|var/lib|tmp)(?![\w.-])" + _PATH_TAIL)


_SCRUBBED_TABLES = ("events", "runs")


def hide_paths(*paths):
    """Chemins à ne jamais écrire au registre ni montrer (forme donnée,
    absolue et résolue)."""
    for p in paths:
        if not p or not isinstance(p, (str, os.PathLike)):
            continue
        s = os.fspath(p)
        for v in (s, os.path.abspath(s), os.path.realpath(s)):
            if len(v) > 1 and v != "/":
                _HIDDEN_PATHS.add(v)


def _process_paths():
    out = set(_HIDDEN_PATHS)
    home = os.path.expanduser("~")
    for p in (home if home not in ("", "/") else None, tempfile.gettempdir(),
              os.environ.get("HARVESTER_OPS_CONFIG"), os.environ.get("HARVESTER_OPS_REPLICATION_DB")):
        if p and len(p) > 1:
            out.add(p)
    try:
        out.add(str(cluster_decl.state_dir()))
    except Exception:                           # noqa: BLE001
        pass
    return out


def scrub_text(text, extra=()):
    """Le texte sans aucun chemin : chaque chemin connu (seulement là où il
    apparaît comme chemin, jamais au milieu d'un mot ou d'un autre chemin),
    puis tout chemin absolu sous /home, /root, /var/lib et /tmp, devient
    `<hidden>`."""
    s = str(text)
    if "/" not in s and not extra:
        return s
    for h in sorted(_process_paths() | {str(x) for x in extra if x and len(str(x)) > 1}, key=len, reverse=True):
        if h in s:
            # le chemin et ce qui le prolonge (`<rép>/fichier`), jamais `<rép>x`
            s = re.sub(r"(?<![\w.~-])" + re.escape(h) + r"(?:/" + _PATH_TAIL + r")?(?![\w.-])", HIDDEN, s)
    return _GENERIC_PATH_RE.sub(HIDDEN, s)


def scrub_value(v, extra=()):
    """`scrub_text` sur chaque chaîne (clés comprises) d'une valeur JSON."""
    if isinstance(v, str):
        return scrub_text(v, extra)
    if isinstance(v, dict):
        return {scrub_value(k, extra) if isinstance(k, str) else k: scrub_value(x, extra) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [scrub_value(x, extra) for x in v]
    return v


def _insert(conn, table, values, upsert=False):
    # messages et détails (journal, bascules) : masquage des chemins ; les
    # autres tables ne rangent que des noms, des états et des URL de cible
    # de sauvegarde, qui doivent rester exactes
    if table in _SCRUBBED_TABLES:
        values = {k: scrub_value(v) for k, v in values.items()}
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


_SETTABLE = ("rpo_objective_s", "interval_s", "retain", "mappings", "options",
             "selector", "selector_namespace", "vms", "generation", "active_side")


def update_plan(conn, plan_id, now=None, **fields):
    """Réglages modifiables (lus à chaud par le suiveur). L'état ne passe
    jamais par ici (voir `set_state`).

    Le plan modifié repasse par `replication.normalize_plan` (PlanError au
    premier défaut) : `cron` suit `interval_s`, les options gardent leurs
    valeurs par défaut. `options` se fusionne aux options rangées (mettre
    `paused` ne touche pas au reste) ; `mappings` remplace. Donner `vms`
    retire le sélecteur, et inversement."""
    bad = [k for k in fields if k not in _SETTABLE]
    if bad:
        raise ValueError(f"not settable: {', '.join(bad)}")
    with _tx(conn):
        p = _row("plans", conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone())
        if not p:
            raise KeyError(plan_id)
        if not fields:
            return p
        req = {k: p[k] for k in ("name", "source_cluster", "target_cluster", "source_host_kind",
                                 "target_host_kind", "rpo_objective_s", "interval_s", "retain", "vms",
                                 "selector", "mappings")}
        req["namespace"] = p["selector_namespace"]
        req["options"] = dict(p["options"] or {})
        for k, v in fields.items():
            if k == "options":
                req["options"].update(v or {})
            elif k == "selector_namespace":
                req["namespace"] = v
            elif k not in ("generation", "active_side"):
                req[k] = v
        if fields.get("vms"):
            req["selector"], req["namespace"] = "", ""
        elif fields.get("selector"):
            req["vms"] = []
        n = rp.normalize_plan(req)
        vals = {k: n[k] for k in ("rpo_objective_s", "interval_s", "cron", "retain", "mappings", "options",
                                  "selector", "selector_namespace", "vms")}
        if "generation" in fields:
            vals["generation"] = _generation(fields["generation"])
        if "active_side" in fields:
            if fields["active_side"] not in rp.SIDES:
                raise ValueError("active_side: source or target")
            vals["active_side"] = fields["active_side"]
        sets = ", ".join(f'"{k}" = ?' for k in vals)
        args = [json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v for v in vals.values()]
        conn.execute(f"UPDATE plans SET {sets}, updated_at = ? WHERE id = ?", args + [_iso(_now(now)), plan_id])
    return get_plan(conn, plan_id)


def delete_plan(conn, plan_id):
    """Efface un plan `draft` ou `unprotected` et ses lignes de travail
    (ressources, volumes, passages, mesures, bail, verrou). Les bascules et
    déprotections (`runs`) et le journal (`events`) restent, pour l'audit :
    ils disent ce qui a été fait, par qui, même plan effacé. Les deux sont
    gardés ensemble, jamais l'un sans l'autre ; le journal part avec `prune`
    (400 jours)."""
    with _tx(conn):
        p = conn.execute("SELECT state FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not p:
            return False
        if p["state"] not in ("draft", "unprotected"):
            raise StateConflict(plan_id, p["state"], "deleted", "only a draft or unprotected plan can be deleted")
        for t in ("plan_resources", "plan_volumes", "ticks", "measures", "leases", "locks"):
            conn.execute(f'DELETE FROM "{t}" WHERE plan_id = ?', (plan_id,))
        conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    return True


def error_origin(conn, plan_id):
    """L'état d'où le plan est entré en `error` (colonne `error_origin`) ;
    None hors `error` ou si inconnue."""
    r = conn.execute("SELECT state, error_origin FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if not r or r["state"] != "error":
        return None
    return r["error_origin"] or None


def _state_event(conn, t, plan_id, resource_uid, cur, new, by, action_id, message):
    _insert(conn, "events", {"ts": _event_ts(t), "plan_id": plan_id, "resource_uid": resource_uid or "",
                             "host": "", "side": "", "operation": "plan", "step": "state",
                             "level": "error" if new == "error" else "info", "code": new,
                             "message": (message or f"{cur} to {new}")[:500],
                             "facts": {"from": cur, "to": new}, "by": by or "", "action_id": action_id or ""})


def set_state(conn, plan_id, expected, new, now=None, by="", action_id="", message=""):
    """Passe le plan de `expected` (un état ou une liste) à `new`, dans une
    seule transaction. StateConflict si l'état réel n'est pas attendu ou si
    la transition n'existe pas (`replication.transition_allowed` : depuis
    `error`, `error_exits(error_origin)`). Entrer en `error` range l'état
    d'origine dans `error_origin`, en sortir l'efface. Chaque passage écrit
    un événement `plan/state` (code = nouvel état, facts {from, to})."""
    if new not in rp.PLAN_STATES:
        raise ValueError(f"unknown state {new}")
    exp = (expected,) if isinstance(expected, str) else tuple(expected)
    with _tx(conn):
        r = conn.execute("SELECT state, error_origin FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not r:
            raise KeyError(plan_id)
        cur = r["state"]
        if cur not in exp:
            raise StateConflict(plan_id, cur, new, f"state is {cur}, expected {'|'.join(exp)}")
        if not rp.transition_allowed(cur, new, r["error_origin"] or None):
            raise StateConflict(plan_id, cur, new, "transition not allowed")
        t = _now(now)
        conn.execute("UPDATE plans SET state = ?, error_origin = ?, updated_at = ? WHERE id = ?",
                     (new, cur if new == "error" else "", _iso(t), plan_id))
        _state_event(conn, t, plan_id, "", cur, new, by, action_id, message)
    return get_plan(conn, plan_id)


# ---------------------------------------------------------------------------
# Ressources et volumes
# ---------------------------------------------------------------------------

def upsert_resource(conn, plan_id, resource, now=None):
    """Range (ou met à jour) une ressource protégée ; clé (plan, génération,
    uid). L'état n'est changé que par `set_resource_state`, sauf à la
    création (`draft` par défaut). `generation` est exigée (celle du plan)."""
    gen = _generation(resource.get("generation"))
    uid = resource["resource_uid"]
    vals = {"plan_id": plan_id, "generation": gen, "resource_kind": resource.get("resource_kind") or "vm",
            "resource_uid": uid, "source_host": resource["source_host"], "target_host": resource["target_host"],
            "namespace": resource["namespace"], "name": resource["name"],
            "target_namespace": resource.get("target_namespace") or "",
            "target_name": resource.get("target_name") or "", "vm_generation": resource.get("vm_generation"),
            "firmware_uuid": resource.get("firmware_uuid") or ""}
    if "vm_spec_digest" in resource:
        vals["vm_spec_digest"] = resource.get("vm_spec_digest") or ""
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


def set_resource_state(conn, plan_id, resource_uid, state, generation=None, now=None, by="", action_id="",
                       message="", expected=None):
    """Change l'état rangé d'une ressource (états du cycle de vie seulement :
    `lagging`, `drifting`, `degraded` et `source-unreachable` se calculent).
    Même état : rien ne change (ni `state_since`, ni journal). Sinon la
    transition est contrôlée comme celle du plan (`transition_allowed`,
    `error_origin` de la ressource), `expected` (un état ou une liste)
    éventuel est vérifié, `error_origin` est rangé à l'entrée en `error` et
    effacé à la sortie, et un événement `plan/state` est écrit pour la
    ressource. Rend la ressource."""
    if state not in rp.STORED_RESOURCE_STATES:
        raise ValueError(f"not a stored resource state: {state}")
    with _tx(conn):
        r = get_resource(conn, plan_id, resource_uid, generation)
        if not r:
            raise KeyError(resource_uid)
        cur = r["state"]
        if expected is not None:
            exp = (expected,) if isinstance(expected, str) else tuple(expected)
            if cur not in exp:
                raise StateConflict(plan_id, cur, state, f"resource state is {cur}, expected {'|'.join(exp)}")
        if cur != state:
            if not rp.transition_allowed(cur, state, r.get("error_origin") or None):
                raise StateConflict(plan_id, cur, state, "resource transition not allowed")
            t = _now(now)
            conn.execute("UPDATE plan_resources SET state = ?, state_since = ?, error_origin = ? WHERE plan_id = ?"
                         " AND generation = ? AND resource_uid = ?",
                         (state, _iso(t), cur if state == "error" else "", plan_id, r["generation"],
                          resource_uid))
            _state_event(conn, t, plan_id, resource_uid, cur, state, by, action_id, message)
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
    disque). Sans `state`, l'état rangé est gardé (`pending` à la création),
    comme `upsert_resource`. `generation` est exigée."""
    state = volume.get("state")
    if state is not None and state not in rp.VOLUME_STATES:
        raise ValueError(f"unknown volume state {state}")
    vals = {"plan_id": plan_id, "generation": _generation(volume.get("generation"))}
    for col, _decl in TABLES["plan_volumes"]:
        if col in ("plan_id", "generation"):
            continue
        if col in volume:
            vals[col] = int(bool(volume[col])) if col == "migratable" else volume[col]
    vals.pop("state", None)
    for k in ("vm_uid", "disk"):
        if not vals.get(k):
            raise ValueError(f"volume: {k} is required")
    with _tx(conn):
        exists = conn.execute("SELECT 1 FROM plan_volumes WHERE plan_id = ? AND generation = ? AND vm_uid = ?"
                              " AND disk = ?", (plan_id, vals["generation"], vals["vm_uid"], vals["disk"])).fetchone()
        if state is not None or not exists:
            vals["state"] = state or "pending"
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
    vals = {"plan_id": plan_id, "generation": _generation(tick.get("generation"))}
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


def mark_applied(conn, plan_id, source_volume, backup, applied_at, generation, resync_at=None):
    """Note qu'une sauvegarde est appliquée sur le volume de secours. Rend
    False si le passage est inconnu ; ValueError s'il est en erreur (une
    sauvegarde en échec ne s'applique pas)."""
    generation = _generation(generation)
    sets, args = ["state = 'applied'", "applied_at = ?"], [_iso(applied_at)]
    if resync_at:
        sets.append("resync_at = ?")
        args.append(_iso(resync_at))
    with _tx(conn):
        old = conn.execute("SELECT state FROM ticks WHERE plan_id = ? AND generation = ? AND source_volume = ?"
                           " AND backup = ?", (plan_id, generation, source_volume, backup)).fetchone()
        if old and old["state"] == "error":
            raise ValueError(f"tick {backup} is in error and cannot be applied")
        cur = conn.execute(f"UPDATE ticks SET {', '.join(sets)} WHERE plan_id = ? AND generation = ? AND"
                           " source_volume = ? AND backup = ?",
                           args + [plan_id, generation, source_volume, backup])
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


def last_applied_ticks(conn, plan_id, generation):
    """Le dernier passage appliqué de chaque volume (une requête) : avec les
    passages récents, de quoi calculer le RPO tenu sans relire tout
    l'historique, même quand l'application est arrêtée depuis longtemps."""
    generation = _generation(generation)
    rows = conn.execute(
        "SELECT t.* FROM ticks t JOIN (SELECT source_volume, MAX(snapshot_at) AS m FROM ticks"
        " WHERE plan_id = ? AND generation = ? AND state = 'applied' GROUP BY source_volume) k"
        " ON t.source_volume = k.source_volume AND t.snapshot_at = k.m"
        " WHERE t.plan_id = ? AND t.generation = ? AND t.state = 'applied'",
        (plan_id, generation, plan_id, generation)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Santé et statistiques du suiveur
# ---------------------------------------------------------------------------

def set_health(conn, plan_id, resource_uid, generation, health, now=None):
    """Range la santé d'une ressource relevée par le suiveur (fusionnée avec
    la précédente : une clé absente garde sa valeur). Rend False si la
    ressource est inconnue."""
    generation = _generation(generation)
    with _tx(conn):
        r = conn.execute("SELECT health FROM plan_resources WHERE plan_id = ? AND generation = ? AND"
                         " resource_uid = ?", (plan_id, generation, resource_uid)).fetchone()
        if r is None:
            return False
        try:
            cur = json.loads(r["health"] or "{}") or {}
        except ValueError:
            cur = {}
        cur.update(scrub_value(health or {}))
        conn.execute("UPDATE plan_resources SET health = ?, health_at = ? WHERE plan_id = ? AND generation = ?"
                     " AND resource_uid = ?", (json.dumps(cur, sort_keys=True), _iso(_now(now)), plan_id,
                                               generation, resource_uid))
    return True


def health_map(conn, plan_id, generation=None):
    """{uid: santé} des ressources d'un plan (forme de `health` attendue par
    `replication.plan_status`)."""
    return {r["resource_uid"]: dict(r.get("health") or {}, checked_at=r.get("health_at") or None)
            for r in list_resources(conn, plan_id, generation)}


VIEW_EVENTS = 3


def plan_view(conn, plan, now):
    """Ce que lisent les vues d'un plan (liste des plans, liste par hôte,
    état) pour `replication.plan_row`, `resource_row` et `plan_status` :
    ressources et volumes de la génération courante, passages récents plus
    le dernier point appliqué de chaque volume (même lecture que le suiveur),
    derniers événements du plan et de chaque ressource, santé et marche des
    VMs relevées par le suiveur. La ligne de commande et la console passent
    par ici : un hôte montre exactement ce que montre la liste des plans."""
    pid, gen = plan["id"], int(plan["generation"])
    interval = int(plan.get("interval_s") or 300)
    t = rp._t(now)
    since = t - timedelta(seconds=max(3600, (int(plan.get("retain") or rp.DEFAULT_RETAIN) + 2) * interval))
    ticks = list_ticks(conn, pid, gen, since=since)
    seen = {(x["source_volume"], x["backup"]) for x in ticks}
    ticks += [x for x in last_applied_ticks(conn, pid, gen) if (x["source_volume"], x["backup"]) not in seen]
    resources = list_resources(conn, pid, gen)
    events = _events(conn, "plan_id = ? AND resource_uid = ''", [pid], VIEW_EVENTS, None)
    for r in resources:
        events += _events(conn, "plan_id = ? AND resource_uid = ?", [pid, r["resource_uid"]], VIEW_EVENTS, None)
    health = health_map(conn, pid, gen)
    vm_running = {uid: {"running": h.get("running", True), "stopped_at": h.get("stopped_at")}
                  for uid, h in health.items() if "running" in h}
    return {"resources": resources, "volumes": list_volumes(conn, pid, gen), "ticks": ticks,
            "events": events, "health": health, "vm_running": vm_running}


def plan_rows(conn, plans, now, role=None, side="any"):
    """Lignes de plans (`replication.plan_row`) lues par `plan_view` : la
    ligne de commande (`plan list`) et la console (menu racine) passent par
    ici, et rendent donc les mêmes objets."""
    rows = []
    for p in plans:
        v = plan_view(conn, p, now)
        rows.append(rp.plan_row(p, v["resources"], v["volumes"], v["ticks"], v["events"], now, v["vm_running"],
                                role=role, health=v["health"], side=side))
    return rows


def host_rows(conn, host, side, now, role=None):
    """Lignes de ressource d'un hôte, vu comme source, cible ou les deux
    (`resources --host`, sous-menu d'un hôte) : même `resource_row` et même
    `plan_view` que `plan_rows`, une lecture par plan."""
    rows, views, plans = [], {}, {}
    for r in list_resources_by_host(conn, host, side):
        pid = r["plan_id"]
        if pid not in plans:
            plans[pid] = get_plan(conn, pid)
            views[pid] = plan_view(conn, plans[pid], now)
        p, v = plans[pid], views[pid]
        res = next((x for x in v["resources"] if x["resource_uid"] == r["resource_uid"]), r)
        rows.append(rp.resource_row(p, res, r["side"], v["volumes"], v["ticks"], v["events"], now, v["vm_running"],
                                    role=role, health=v["health"].get(r["resource_uid"])))
    return rows


def record_stats(conn, plan_id, row):
    """Range (idempotent) le bilan d'un passage appliqué d'une VM ; clé
    (plan, génération, ressource, `pass_at`)."""
    vals = {"plan_id": plan_id, "generation": _generation(row.get("generation"))}
    for col, _decl in TABLES["stats"]:
        if col not in ("plan_id", "generation") and col in row:
            vals[col] = row[col]
    for k in ("resource_uid", "pass_at"):
        if not vals.get(k):
            raise ValueError(f"stats: {k} is required")
    vals.setdefault("ts", _iso(_now(None)))
    with _tx(conn):
        _insert(conn, "stats", vals, upsert=True)


def list_stats(conn, plan_id, resource_uid=None, since=None, limit=1000):
    q, args = "SELECT * FROM stats WHERE plan_id = ?", [plan_id]
    if resource_uid:
        q += " AND resource_uid = ?"
        args.append(resource_uid)
    if since:
        q += " AND pass_at >= ?"
        args.append(_iso(since))
    q += " ORDER BY pass_at DESC, resource_uid LIMIT ?"
    return [dict(r) for r in conn.execute(q, args + [max(1, min(int(limit or 1000), 100000))])][::-1]


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
             step or "", level, code or "", scrub_text(message or "")[:500],
             json.dumps(scrub_value(facts or {}), sort_keys=True), by or "", action_id or ""))
    return cur.lastrowid


def _before(before):
    """Curseur de page : ("id", n) ou ("ts", date). Un entier, ou une chaîne
    de chiffres, est un id d'événement ; une chaîne ISO 8601 ou un datetime,
    une date. ValueError sinon (jamais de devinette)."""
    if isinstance(before, bool):
        raise ValueError("before: an event id or an ISO date")
    if isinstance(before, int):
        return "id", before
    if isinstance(before, datetime):
        return "ts", _event_ts(before)
    if isinstance(before, str):
        b = before.strip()
        if b.isdigit():
            return "id", int(b)
        try:
            # à la microseconde : la date du dernier rendu ne saute pas les
            # événements de la même seconde (`rp._t` tronque à la seconde)
            return "ts", _event_ts(b)
        except ValueError:
            raise ValueError("before: an event id or an ISO date") from None
    raise ValueError("before: an event id or an ISO date")


def _events(conn, where, args, limit, before):
    limit = max(1, min(int(limit or 50), 500))
    q = f"SELECT * FROM events WHERE {where}"
    if before is not None and before != "":
        kind, value = _before(before)
        row = (conn.execute("SELECT ts FROM events WHERE id = ?", (value,)).fetchone()
               if kind == "id" else None)
        if row is not None:
            # curseur composé (ts, id), dans l'ordre même de la page : un
            # événement daté plus tôt mais rangé plus tard n'est ni sauté ni
            # rendu deux fois
            q += " AND (ts < ? OR (ts = ? AND id < ?))"
            args = args + [row["ts"], row["ts"], value]
        else:
            # id disparu (purge) : le plus proche possible ; date : stricte
            q += " AND id < ?" if kind == "id" else " AND ts < ?"
            args = args + [value]
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    return [_row("events", r) for r in conn.execute(q, args + [limit])]


def resource_events(conn, resource_uid, limit=50, before=None):
    """Le journal d'une ressource, du plus récent au plus ancien. `before` :
    l'id du dernier événement rendu (entier ou chaîne de chiffres : curseur
    exact quand plusieurs partagent la même date), ou une date ISO 8601 /
    datetime (page suivante : la date du dernier rendu)."""
    return _events(conn, "resource_uid = ?", [resource_uid], limit, before)


def plan_events(conn, plan_id, limit=50, before=None):
    return _events(conn, "plan_id = ?", [plan_id], limit, before)


def host_events(conn, host, limit=50, before=None):
    return _events(conn, "host = ?", [host], limit, before)


# ---------------------------------------------------------------------------
# Bascules et déprotections
# ---------------------------------------------------------------------------

def record_run(conn, plan_id, operation, started_at, generation, finished_at=None, result="",
               vms=(), points=None, steps=(), by="", action_id="", run_id=None):
    """Range (ou complète, avec `run_id`) une bascule ou une déprotection :
    points de reprise par volume, durée de chaque étape. Rend l'id."""
    if operation not in ("failover", "unprotect"):
        raise ValueError("operation: failover or unprotect")
    vals = {"plan_id": plan_id, "generation": _generation(generation), "operation": operation,
            "started_at": _iso(started_at), "finished_at": _iso(finished_at) if finished_at else "",
            "result": result or "", "vms": list(vms), "points": points or {}, "steps": list(steps),
            "by": by or "", "action_id": action_id or ""}
    with _tx(conn):
        if run_id:
            sets = ", ".join(f'"{k}" = ?' for k in vals)
            conn.execute(f"UPDATE runs SET {sets} WHERE id = ?",
                         [json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
                          for v in scrub_value(vals).values()] + [run_id])
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


def take_lease(conn, plan_id, owner, ttl, take_over=False, now=None, pid_alive=None, host=None):
    """Prend (ou renouvelle) le bail d'un plan pour `ttl` secondes. Refusé
    (False) si un autre détenteur l'a renouvelé depuis moins de son `ttl`,
    sauf `take_over`, ou si ce détenteur était sur CET hôte et que son
    processus n'existe plus : vu sur le banc, le suiveur relancé par la
    console après un `kill -9` attendait les 180 s du bail de son
    prédécesseur sans rien appliquer."""
    t = _now(now)
    alive = pid_alive or _pid_alive
    with _tx(conn):
        r = conn.execute("SELECT * FROM leases WHERE plan_id = ?", (plan_id,)).fetchone()
        if r and r["owner"] != owner and not take_over:
            expires = rp._t(r["heartbeat"]) + timedelta(seconds=int(r["ttl_s"]))
            h, pid = _parse_owner(r["owner"])
            gone = h == (host or socket.gethostname()) and pid is not None and not alive(pid)
            if expires > rp._t(t) and not gone:
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


def _proc_start(pid):
    """Date de démarrage d'un processus (champ 22 de /proc/<pid>/stat, en
    tops d'horloge depuis le démarrage) ; "" si illisible."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        return raw[raw.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return ""


def acquire_lock(conn, plan_id, command, owner, now=None, pid_alive=_pid_alive, host=None,
                 proc_start=_proc_start, ttl=LOCK_TTL_S):
    """Une seule commande mutative à la fois par plan. Locked sinon.

    Le même détenteur le reprend (relance dans le même processus). Un
    verrou abandonné est repris :
    * détenteur sur CET hôte : son processus n'existe plus, ou son PID a été
      réutilisé (date de démarrage différente de celle rangée) ;
    * détenteur sur un autre hôte (son PID ne veut rien dire ici) : son
      dernier renouvellement (`lock_heartbeat`, sinon la prise) date de plus
      de son `ttl_s`.
    Chaque reprise est écrite au journal (`plan/lock-reclaimed`)."""
    here = host or socket.gethostname()
    t = _now(now)
    my_host, my_pid = _parse_owner(owner)
    my_start = proc_start(my_pid) if my_host == here and my_pid is not None else ""
    with _tx(conn):
        r = conn.execute("SELECT * FROM locks WHERE plan_id = ?", (plan_id,)).fetchone()
        if r and r["owner"] != owner:
            h, pid = _parse_owner(r["owner"])
            reason = None
            if h == here and pid is not None:
                if not pid_alive(pid):
                    reason = "process-gone"
                elif r["pid_start"] and proc_start(pid) not in ("", r["pid_start"]):
                    reason = "pid-reused"
            else:
                beat = r["heartbeat"] or r["started_at"]
                if int(r["ttl_s"] or 0) > 0 and rp._t(beat) + timedelta(seconds=int(r["ttl_s"])) < rp._t(t):
                    reason = "ttl-expired"
            if not reason:
                raise Locked(plan_id, dict(r))
            _insert(conn, "events", {"ts": _event_ts(t), "plan_id": plan_id, "operation": "plan",
                                     "step": "lock-reclaimed", "level": "warn", "code": reason,
                                     "message": f"lock of {r['command']} taken back from {r['owner']}"[:500],
                                     "facts": {"previous_owner": r["owner"], "command": r["command"],
                                               "reason": reason}, "by": owner})
        conn.execute("INSERT INTO locks (plan_id, command, owner, started_at, pid_start, heartbeat, ttl_s)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (plan_id) DO UPDATE SET"
                     " command = excluded.command, owner = excluded.owner, started_at = excluded.started_at,"
                     " pid_start = excluded.pid_start, heartbeat = excluded.heartbeat, ttl_s = excluded.ttl_s",
                     (plan_id, command, owner, _iso(t), my_start, _iso(t), int(ttl)))
    return True


def lock_heartbeat(conn, plan_id, owner, now=None):
    """Renouvelle un verrou tenu ; False s'il a été repris."""
    with _tx(conn):
        cur = conn.execute("UPDATE locks SET heartbeat = ? WHERE plan_id = ? AND owner = ?",
                           (_iso(_now(now)), plan_id, owner))
    return cur.rowcount > 0


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

_STATS_MAX = ("disks", "backup_s", "apply_lag_s", "resync_to_apply_s", "skew_s", "rpo_held_s", "backlog")
_STATS_MEAN = ("upload_bps", "apply_bps")


def downsample_stats(conn, before):
    """Statistiques d'avant `before` réduites à une ligne par heure (plan,
    génération, ressource) : pires valeurs (durées, écart, RPO tenu,
    arriéré, disques), somme des octets envoyés, moyenne des débits pondérée
    par les passages, dernier état. Idempotent (une heure déjà réduite n'a
    plus qu'une ligne). Rend le nombre de lignes retirées."""
    rows = [dict(r) for r in conn.execute(
        "SELECT rowid AS rid, * FROM stats WHERE pass_at < ? ORDER BY plan_id, generation, resource_uid, pass_at",
        (_iso(before),))]
    groups = {}
    for r in rows:
        groups.setdefault((r["plan_id"], r["generation"], r["resource_uid"], r["pass_at"][:13]), []).append(r)
    removed = 0
    with _tx(conn):
        for items in groups.values():
            if len(items) < 2:
                continue
            agg = {k: items[0][k] for k in ("plan_id", "generation", "resource_uid", "pass_at")}
            agg["ts"] = max(r["ts"] for r in items)
            agg["state"] = items[-1]["state"]
            agg["passes"] = sum(int(r.get("passes") or 1) for r in items)
            agg["newly_uploaded"] = sum(int(r["newly_uploaded"] or 0) for r in items)
            for k in _STATS_MAX:
                vals = [r[k] for r in items if r[k] is not None]
                agg[k] = max(vals) if vals else None
            for k in _STATS_MEAN:
                vals = [(r[k], int(r.get("passes") or 1)) for r in items if r[k] is not None]
                agg[k] = sum(v * w for v, w in vals) / sum(w for _v, w in vals) if vals else None
            conn.execute(f"DELETE FROM stats WHERE rowid IN ({','.join('?' for _ in items)})",
                         [r["rid"] for r in items])
            _insert(conn, "stats", agg)
            removed += len(items) - 1
    return removed


def prune(conn, before, now=None):
    """Efface passages, mesures et événements d'avant `before` (400 jours
    en usage normal), et réduit à l'heure les statistiques de plus de
    STATS_FULL_DAYS jours (`now` : maintenant par défaut). Le dernier
    passage appliqué de chaque volume est gardé : sans lui, le RPO d'un plan
    figé deviendrait inconnu. Rend {"ticks", "measures", "events", "stats",
    "stats_downsampled"} (lignes effacées)."""
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
        n_stats = conn.execute("DELETE FROM stats WHERE pass_at < ?", (t,)).rowcount
    n_down = downsample_stats(conn, _now(now) - timedelta(days=STATS_FULL_DAYS))
    return {"ticks": n_ticks, "measures": n_meas, "events": n_ev, "stats": n_stats, "stats_downsampled": n_down}


# Constats du suiveur et leur fin (`replication_follow`) : relus au démarrage
# pour ne pas réécrire un constat toujours en cours
FOLLOW_PER_BACKUP = ("backup-error", "apply-late", "backup-volume-missing")


def last_follow_events(conn, since_per_backup=None):
    """Dernier événement du suiveur par (plan, ressource, code), plus tous
    ceux des constats propres à une sauvegarde ou un volume
    (FOLLOW_PER_BACKUP) depuis `since_per_backup`. Rend une liste de lignes
    (faits décodés), plus anciennes d'abord."""
    rows = conn.execute(
        "SELECT e.* FROM events e JOIN (SELECT MAX(id) AS id FROM events WHERE operation = 'follow'"
        " GROUP BY plan_id, resource_uid, code) m ON e.id = m.id ORDER BY e.id").fetchall()
    out = [_row("events", r) for r in rows]
    if since_per_backup is not None:
        q = ("SELECT * FROM events WHERE operation = 'follow' AND ts >= ? AND code IN ("
             + ",".join("?" for _ in FOLLOW_PER_BACKUP) + ") ORDER BY id")
        seen = {r["id"] for r in out}
        out += [_row("events", r) for r in conn.execute(q, [_event_ts(since_per_backup), *FOLLOW_PER_BACKUP])
                if r["id"] not in seen]
    return sorted(out, key=lambda r: r["id"])


def retention_cutoff(now=None):
    return _now(now) - timedelta(days=RETENTION_DAYS)
