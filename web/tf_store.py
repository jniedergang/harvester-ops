"""harvester-ops : les déclarations Terraform, gardées par la console (v1.54.0).

Jusqu'à la v1.53 une déclaration ne vivait que dans le navigateur qui l'avait
créée (localStorage) : ni partagée entre les opérateurs, ni sauvegardée avec
la console, et perdue avec le profil du navigateur. Elle est désormais une
ligne SQLite dans l'état de la console, à côté de l'historique des actions.

Une déclaration : un identifiant (12 caractères hexadécimaux, qui nomme aussi
son espace de travail Terraform), un cluster, un nom unique dans ce cluster
(le renommer ne touche ni l'état Terraform ni les ressources), une
description, des ressources, et un numéro de révision pour que deux
opérateurs qui modifient la même déclaration ne s'écrasent pas en silence.

Ce module ne connaît ni Flask ni Terraform : il est essayé seul.
"""

import json
import re
import sqlite3
import threading
import time

ID_RE = re.compile(r"^[0-9a-f]{12}$")
KINDS = ("vm", "image", "ssh_key", "raw")
MAX_NAME = 80
MAX_DESCRIPTION = 500
MAX_BODY = 1024 * 1024
MAX_RESOURCES = 200


class StoreError(Exception):
    """Refus de la demande : le message est destiné à l'opérateur."""

    def __init__(self, code, message, **facts):
        super().__init__(message)
        self.code = code
        self.facts = facts


class Conflict(StoreError):
    """La déclaration a changé depuis sa lecture (révision dépassée)."""


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def clean_name(name):
    name = " ".join(str(name or "").split())
    if not name:
        raise StoreError("invalid-name", "a declaration needs a name")
    if len(name) > MAX_NAME:
        raise StoreError("invalid-name", f"name longer than {MAX_NAME} characters")
    if any(ord(c) < 32 for c in name):
        raise StoreError("invalid-name", "control characters are not allowed")
    return name


def clean_resources(resources):
    if resources is None:
        return []
    if not isinstance(resources, list):
        raise StoreError("invalid-resources", "resources must be a list")
    if len(resources) > MAX_RESOURCES:
        raise StoreError("invalid-resources", f"more than {MAX_RESOURCES} resources")
    out, seen = [], set()
    for i, r in enumerate(resources):
        if not isinstance(r, dict):
            raise StoreError("invalid-resources", f"resource #{i + 1} is not an object")
        rid = str(r.get("id") or "")
        kind = r.get("kind")
        spec = r.get("spec") if isinstance(r.get("spec"), dict) else {}
        if not ID_RE.match(rid) or rid in seen:
            raise StoreError("invalid-resources", f"resource #{i + 1} has no valid id")
        if kind not in KINDS:
            raise StoreError("invalid-resources", f"resource #{i + 1}: unknown kind {kind!r}")
        seen.add(rid)
        out.append({"id": rid, "kind": kind, "spec": spec})
    if len(json.dumps(out)) > MAX_BODY:
        raise StoreError("invalid-resources", "declaration too large")
    return out


class DeclStore:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS tf_declarations (
                  id TEXT PRIMARY KEY,
                  cluster TEXT NOT NULL,
                  name TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  resources TEXT NOT NULL DEFAULT '[]',
                  revision INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL,
                  created_by TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL,
                  updated_by TEXT NOT NULL DEFAULT '',
                  last_applied_at TEXT,
                  last_applied_status TEXT,
                  last_applied_by TEXT
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS tf_decl_cluster ON tf_declarations(cluster)")

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    @staticmethod
    def _row(r):
        if r is None:
            return None
        d = dict(r)
        d["resources"] = json.loads(d["resources"] or "[]")
        return d

    # -- lecture -------------------------------------------------------------
    def list(self, cluster=None):
        with self._conn() as c:
            if cluster:
                rows = c.execute("SELECT * FROM tf_declarations WHERE cluster = ? "
                                 "ORDER BY lower(name)", (cluster,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM tf_declarations ORDER BY cluster, lower(name)").fetchall()
        return [self._row(r) for r in rows]

    def get(self, decl_id):
        if not ID_RE.match(str(decl_id or "")):
            return None
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM tf_declarations WHERE id = ?",
                                       (decl_id,)).fetchone())

    # -- écriture ------------------------------------------------------------
    def _name_taken(self, c, cluster, name, but=None):
        row = c.execute("SELECT id FROM tf_declarations WHERE cluster = ? AND lower(name) = lower(?)",
                        (cluster, name)).fetchone()
        return row is not None and row["id"] != but

    def create(self, decl_id, cluster, name, description="", resources=None, user=""):
        if not ID_RE.match(str(decl_id or "")):
            raise StoreError("invalid-id", "invalid declaration id")
        if not cluster:
            raise StoreError("invalid-cluster", "a declaration belongs to a cluster")
        name = clean_name(name)
        description = str(description or "")[:MAX_DESCRIPTION]
        resources = clean_resources(resources)
        now = _now()
        with self._lock, self._conn() as c:
            if c.execute("SELECT 1 FROM tf_declarations WHERE id = ?", (decl_id,)).fetchone():
                raise StoreError("id-taken", "a declaration with this id already exists", id=decl_id)
            if self._name_taken(c, cluster, name):
                raise StoreError("name-taken", f"a declaration named {name!r} already exists", name=name)
            c.execute("INSERT INTO tf_declarations (id, cluster, name, description, resources, revision, "
                      "created_at, created_by, updated_at, updated_by) VALUES (?,?,?,?,?,1,?,?,?,?)",
                      (decl_id, cluster, name, description, json.dumps(resources), now, user, now, user))
        return self.get(decl_id)

    def update(self, decl_id, revision, user="", name=None, description=None, resources=None):
        """Mise à jour à révision attendue : `Conflict` si quelqu'un est
        passé entre-temps (la version courante est dans `facts`)."""
        with self._lock, self._conn() as c:
            cur = self._row(c.execute("SELECT * FROM tf_declarations WHERE id = ?", (decl_id,)).fetchone())
            if cur is None:
                raise StoreError("not-found", "declaration not found", id=decl_id)
            if int(revision or 0) != cur["revision"]:
                raise Conflict("conflict", "the declaration changed in the meantime", current=cur)
            fields = {}
            if name is not None:
                n = clean_name(name)
                if self._name_taken(c, cur["cluster"], n, but=decl_id):
                    raise StoreError("name-taken", f"a declaration named {n!r} already exists", name=n)
                fields["name"] = n
            if description is not None:
                fields["description"] = str(description)[:MAX_DESCRIPTION]
            if resources is not None:
                fields["resources"] = json.dumps(clean_resources(resources))
            fields.update(revision=cur["revision"] + 1, updated_at=_now(), updated_by=user)
            sets = ", ".join(f"{k} = ?" for k in fields)
            c.execute(f"UPDATE tf_declarations SET {sets} WHERE id = ?", (*fields.values(), decl_id))
        return self.get(decl_id)

    def mark_applied(self, decl_id, status, user=""):
        """Le résultat du dernier apply ou destroy ; ne change pas la
        révision (le contenu, lui, n'a pas bougé)."""
        with self._lock, self._conn() as c:
            c.execute("UPDATE tf_declarations SET last_applied_at = ?, last_applied_status = ?, "
                      "last_applied_by = ? WHERE id = ?", (_now(), status, user, decl_id))
        return self.get(decl_id)

    def drop_resources(self, decl_id, resource_ids, user=""):
        """Retire des ressources sans condition de révision : sert quand une
        ressource vient d'être détruite à l'unité, pour que le prochain
        apply ne la recrée pas."""
        with self._lock, self._conn() as c:
            cur = self._row(c.execute("SELECT * FROM tf_declarations WHERE id = ?", (decl_id,)).fetchone())
            if cur is None:
                return None
            keep = [r for r in cur["resources"] if r["id"] not in set(resource_ids)]
            if len(keep) == len(cur["resources"]):
                return cur
            c.execute("UPDATE tf_declarations SET resources = ?, revision = ?, updated_at = ?, "
                      "updated_by = ? WHERE id = ?",
                      (json.dumps(keep), cur["revision"] + 1, _now(), user, decl_id))
        return self.get(decl_id)

    def delete(self, decl_id):
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM tf_declarations WHERE id = ?", (decl_id,)).rowcount > 0
