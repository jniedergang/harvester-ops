"""harvester-ops : le registre des relevés de GPU (Usage, v1.95.0).

Une base SQLite de la console, `usage.db`, à côté de `bookings.db` (ou
HARVESTER_OPS_USAGE_DB), en mode WAL. L'échantillonneur de la console y
écrit à chaque tour une ligne par unité tenue (`samples`) et une par cluster
lu (`runs`, pour dire quand chaque cluster a été relevé et s'il était
joignable). Les relevés de plus de 400 jours sont effacés.

Rien n'est jamais écrit sur un cluster.
"""

import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import cluster_decl
import hv_bookings as hb
import hv_usage as hu

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts TEXT NOT NULL,
    cluster TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    workload TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 1,
    interval INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS runs (
    ts TEXT NOT NULL,
    cluster TEXT NOT NULL,
    state TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    interval INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_ts ON runs (ts);
"""
# un relevé vaut au plus deux intervalles (d'une heure au plus) : on lit
# autant avant et après la période
SLACK = timedelta(seconds=hu.GAP_FACTOR * hu.MAX_INTERVAL)
FIELDS = ("ts", "cluster", "unit", "type", "model", "kind", "namespace", "name", "workload", "count", "interval")


def default_path():
    """HARVESTER_OPS_USAGE_DB, sinon `usage.db` dans le répertoire d'état."""
    env = os.environ.get("HARVESTER_OPS_USAGE_DB")
    return Path(env) if env else cluster_decl.state_dir() / "usage.db"


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


def record(conn, ts, interval, cluster, state, samples=(), detail=""):
    """Range un tour d'un cluster : ses relevés et son état."""
    t = hb.iso(ts)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT INTO samples (ts, cluster, unit, type, model, kind, namespace, name, workload, count, interval)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(t, cluster, x["unit"], x["type"], x["model"], x["kind"], x["namespace"], x["name"],
              x.get("workload") or "", x["count"], interval) for x in samples or ()])
        conn.execute("INSERT INTO runs (ts, cluster, state, used, interval, detail) VALUES (?, ?, ?, ?, ?, ?)",
                     (t, cluster, state, sum(x["count"] for x in samples or ()), interval, (detail or "")[:300]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def prune(conn, before):
    """Efface relevés et tours d'avant `before` ; rend le nombre de relevés
    effacés."""
    t = hb.iso(before)
    n = conn.execute("DELETE FROM samples WHERE ts < ?", (t,)).rowcount
    conn.execute("DELETE FROM runs WHERE ts < ?", (t,))
    return n


def read_samples(conn, start, end):
    """Les relevés utiles à la période [start, end) (avec la marge d'un
    relevé qui déborde)."""
    s, e = hb.parse_time(start) - SLACK, hb.parse_time(end) + SLACK
    return [dict(r) for r in conn.execute(
        f"SELECT {', '.join(FIELDS)} FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts",
        (hb.iso(s), hb.iso(e)))]


def last_runs(conn):
    """Le dernier tour de chaque cluster : {cluster, ts, state, used,
    interval, detail}, et le premier relevé gardé."""
    rows = conn.execute(
        "SELECT r.* FROM runs r JOIN (SELECT cluster, MAX(ts) AS ts FROM runs GROUP BY cluster) m"
        " ON r.cluster = m.cluster AND r.ts = m.ts ORDER BY r.cluster").fetchall()
    first = conn.execute("SELECT MIN(ts) FROM runs").fetchone()[0]
    return [dict(r) for r in rows], first


def build_report(bookings_conn, usage_conn, start, end, tz, now):
    """Le rapport de la période, avec les quotas et l'état de
    l'échantillonneur (`sampler`: {clusters, first})."""
    import bookings_db as bdb
    since = min(hb.parse_time(start), hu.quota_month(start, end, now))
    bookings = bdb.list_bookings(bookings_conn, since=since)
    quotas = bdb.list_quotas(bookings_conn)
    rep = hu.report(bookings, read_samples(usage_conn, start, end), start, end, quotas=quotas, tz=tz, now=now)
    runs, first = last_runs(usage_conn)
    rep["sampler"] = {"clusters": runs, "first": first}
    rep["quotas"] = quotas
    return rep
