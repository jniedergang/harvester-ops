"""harvester-ops : le registre des réservations (Bookings, v1.94.0).

Une base SQLite de la console, `bookings.db`, à côté de `notes.db` (ou
HARVESTER_OPS_BOOKINGS_DB), en mode WAL. La console la lit ; seule la ligne
de commande `bin/harvester-bookings.py` y écrit (parité CLI), et le contrôle
des conflits se fait DANS la transaction d'écriture (BEGIN IMMEDIATE) : deux
réservations lancées en même temps ne peuvent pas passer toutes les deux.

Rien n'est jamais écrit sur un cluster.
"""

import os
import sqlite3
import uuid
from pathlib import Path

import cluster_decl
import hv_bookings as hb
import hv_usage as hu

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    cluster TEXT NOT NULL,
    type TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 1,
    holder_kind TEXT NOT NULL,
    holder TEXT NOT NULL,
    start TEXT NOT NULL,
    "end" TEXT NOT NULL,
    target_kind TEXT NOT NULL DEFAULT '',
    target_namespace TEXT NOT NULL DEFAULT '',
    target_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    cancelled_at TEXT NOT NULL DEFAULT '',
    cancelled_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS bookings_slot ON bookings (cluster, type, "end");
-- v1.95.0 : quotas par personne ou par groupe (migration additive)
CREATE TABLE IF NOT EXISTS quotas (
    holder_kind TEXT NOT NULL,
    holder TEXT NOT NULL,
    monthly_hours REAL,
    max_units INTEGER,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (holder_kind, holder)
);
"""


def default_path():
    """HARVESTER_OPS_BOOKINGS_DB, sinon `bookings.db` dans le répertoire d'état."""
    env = os.environ.get("HARVESTER_OPS_BOOKINGS_DB")
    return Path(env) if env else cluster_decl.state_dir() / "bookings.db"


def connect(path=None):
    path = Path(path or default_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def new_id():
    return "bk-" + uuid.uuid4().hex[:12]


def _row(r):
    d = dict(r)
    kind, ns, name = d.pop("target_kind"), d.pop("target_namespace"), d.pop("target_name")
    d["target"] = {"kind": kind, "namespace": ns, "name": name} if kind else None
    d["holder"] = {"kind": d.pop("holder_kind"), "name": d.pop("holder")}
    return d


def list_bookings(conn, cluster=None, since=None):
    """Toutes les réservations (annulées comprises), les plus proches d'abord.
    `since` : seulement celles qui finissent après cet instant ISO."""
    q, args = "SELECT * FROM bookings WHERE 1=1", []
    if cluster:
        q += " AND cluster = ?"
        args.append(cluster)
    if since:
        q += ' AND "end" > ?'
        args.append(hb.iso(since))
    q += " ORDER BY start, id"
    return [_row(r) for r in conn.execute(q, args)]


def get(conn, bid):
    r = conn.execute("SELECT * FROM bookings WHERE id = ?", (bid,)).fetchone()
    return _row(r) if r else None


class Conflict(Exception):
    def __init__(self, reasons):
        super().__init__("; ".join(hb.describe(x) for x in reasons))
        self.reasons = reasons


def insert_checked(conn, booking, cap, created_by, now, bid=None):
    """Range une réservation normalisée et résolue, après le contrôle des
    conflits fait dans la même transaction. Conflict si elle est refusée ;
    rend la réservation rangée."""
    bid = bid or new_id()
    if not hb.ID_RE.match(bid):
        raise ValueError("invalid booking id")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if get(conn, bid):
            raise ValueError(f"booking {bid} already exists")
        existing = [b for b in list_bookings(conn, cluster=booking["cluster"], since=booking["start"])
                    if b["type"] == booking["type"]]
        reasons = hb.conflicts(dict(booking, id=bid), existing, cap)
        reasons += quota_reasons(conn, dict(booking, id=bid))
        if reasons:
            raise Conflict(reasons)
        t = booking.get("target") or {}
        conn.execute(
            'INSERT INTO bookings (id, cluster, type, unit, model, count, holder_kind, holder, start, "end",'
            ' target_kind, target_namespace, target_name, note, created_by, created_at)'
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bid, booking["cluster"], booking["type"], booking["unit"], booking["model"], booking["count"],
             booking["holder"]["kind"], booking["holder"]["name"], booking["start"], booking["end"],
             t.get("kind", ""), t.get("namespace", ""), t.get("name", ""), booking["note"],
             created_by or "", hb.iso(now)))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get(conn, bid)


def cancel(conn, bid, by, now):
    """Marque une réservation annulée (elle reste dans l'historique)."""
    conn.execute("UPDATE bookings SET cancelled_at = ?, cancelled_by = ? WHERE id = ? AND cancelled_at = ''",
                 (hb.iso(now), by or "", bid))
    return get(conn, bid)


# -- v1.95.0 : quotas ------------------------------------------------------------

def _quota_row(r):
    return {"holder": {"kind": r["holder_kind"], "name": r["holder"]}, "monthly_hours": r["monthly_hours"],
            "max_units": r["max_units"], "updated_by": r["updated_by"], "updated_at": r["updated_at"]}


def list_quotas(conn):
    return [_quota_row(r) for r in conn.execute("SELECT * FROM quotas ORDER BY holder_kind DESC, holder")]


def get_quota(conn, holder):
    r = conn.execute("SELECT * FROM quotas WHERE holder_kind = ? AND holder = ?",
                     (holder.get("kind"), holder.get("name"))).fetchone()
    return _quota_row(r) if r else None


def set_quota(conn, quota, by, now):
    """Range un quota normalisé (hv_usage.normalize_quota), le remplace s'il
    existe."""
    h = quota["holder"]
    conn.execute("INSERT INTO quotas (holder_kind, holder, monthly_hours, max_units, updated_by, updated_at)"
                 " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (holder_kind, holder) DO UPDATE SET"
                 " monthly_hours = excluded.monthly_hours, max_units = excluded.max_units,"
                 " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                 (h["kind"], h["name"], quota.get("monthly_hours"), quota.get("max_units"), by or "", hb.iso(now)))
    return get_quota(conn, h)


def delete_quota(conn, holder):
    cur = conn.execute("DELETE FROM quotas WHERE holder_kind = ? AND holder = ?",
                       (holder.get("kind"), holder.get("name")))
    return cur.rowcount > 0


def quota_reasons(conn, booking):
    """Les raisons de refuser une réservation au regard du quota de son
    titulaire (vide sans quota). Lit les réservations du titulaire sur les
    mois que touche le créneau, tous clusters confondus."""
    quota = get_quota(conn, booking["holder"])
    if not quota:
        return []
    since = hu.month_start(booking["start"])
    mine = [b for b in list_bookings(conn, since=since) if b["holder"] == booking["holder"]]
    return hu.quota_reasons(booking, mine, quota)
