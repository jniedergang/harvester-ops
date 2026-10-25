"""harvester-ops : la base des dossiers et des favoris (navigation 2.0).

Une base SQLite de la console, `folders.db`, à côté de `notes.db` (ou
HARVESTER_OPS_FOLDERS_DB), en mode WAL, comme `bookings.db`.

Ici, la table des favoris : une préférence PERSONNELLE (une étoile sur un
hôte du menu), retrouvée sur un autre poste. Écrite par la console sous
`/api/me/`, sans action suivie ni ligne de commande (décision 5 du
25/10/2026, comme le mot de passe de son propre compte). L'arborescence des
dossiers et le cache des rangements viennent s'ajouter ici (étape 3), par
des tables additives.

Rien n'est jamais écrit sur un cluster ou un hyperviseur.
"""

import os
import sqlite3
import time
from pathlib import Path

import cluster_decl

# Familles d'hôtes : un cluster Harvester entier ou un hyperviseur KVM
HOST_KINDS = ("harvester", "kvm")

SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    user TEXT NOT NULL,
    host_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user, host_key)
);
"""


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
    try:
        os.chmod(path, 0o600)
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
