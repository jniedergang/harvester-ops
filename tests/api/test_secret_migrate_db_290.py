"""2.9.0 tâche 8 : table `secret_rollback` de replication.db (migration
additive) et remplacement des secrets de notification et des en-têtes des
actions annexes par leur référence OpenBao.

Mutation jouée (rapport) : « secret_rollback vidé avant la confirmation »
(secret_to_ref sans insertion) : test_to_ref_keeps_the_original rouge.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import replication_db as rdb  # noqa: E402
import secret_classes as sc  # noqa: E402

NOTIFY = {"enabled": True, "min_level": "warn", "codes": None, "channels": [
    {"id": "mx-1", "kind": "matrix", "homeserver": "https://mx", "room": "!r:mx", "token": "T0K"},
    {"id": "sl", "kind": "slack", "url": "https://hooks.slack/abc"},
    {"id": "ml", "kind": "email", "smtp_host": "smtp", "password": "PW"},
    {"id": "wh", "kind": "webhook", "url": "https://w", "secret_headers": {"X-Key": "HK"}}]}
HOOKS = {"hooks": [{"name": "pre", "secret_headers": {"Authorization": "AUTH", "X-Other": "OTHER"}}]}


@pytest.fixture
def conn(tmp_path):
    c = rdb.connect(tmp_path / "replication.db")
    rdb.set_setting(c, "notify", NOTIFY)
    c.execute("INSERT INTO plans (id, name, source_cluster, target_cluster, rpo_objective_s, created_at, updated_at,"
              " options) VALUES ('rp-1', 'p1', 'a', 'b', 600, '', '', ?)", (json.dumps(HOOKS),))
    yield c
    c.close()


def test_additive_migration_of_an_older_base(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT 'null')")
    c.execute("INSERT INTO settings (key, value) VALUES ('notify', '{\"channels\": []}')")
    c.commit()
    c.close()
    c = rdb.connect(p)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(secret_rollback)")]
        assert cols == ["cls", "path", "field", "value", "ref", "at", "action_id"]
        assert rdb.get_setting(c, "notify") == {"channels": []}         # rien perdu
    finally:
        c.close()


SLOTS = [
    ("replication-notify", "global", "mx_1__token", "T0K", lambda c: rdb.get_setting(c, "notify")["channels"][0]["token"]),
    ("replication-notify", "global", "sl__url", "https://hooks.slack/abc",
     lambda c: rdb.get_setting(c, "notify")["channels"][1]["url"]),
    ("replication-notify", "global", "ml__password", "PW", lambda c: rdb.get_setting(c, "notify")["channels"][2]["password"]),
    ("replication-notify", "global", sc.notify_field("wh", ("header", "X-Key")), "HK",
     lambda c: rdb.get_setting(c, "notify")["channels"][3]["secret_headers"]["X-Key"]),
    ("replication-hook", "rp-1/pre", sc.header_field("Authorization"), "AUTH",
     lambda c: json.loads(c.execute("SELECT options FROM plans").fetchone()[0])["hooks"][0]["secret_headers"]["Authorization"]),
]


@pytest.mark.parametrize("cls,path,field,value,read", SLOTS)
def test_to_ref_keeps_the_original(conn, cls, path, field, value, read):
    ref = f"openbao:{cls}/{path}#{field}"
    assert rdb.secret_to_ref(conn, cls, path, field, ref, value, "act-1") is True
    assert read(conn) == ref
    rows = rdb.secret_rollback_rows(conn, cls)
    assert [(r["path"], r["field"], r["ref"], r["action_id"]) for r in rows] == [(path, field, ref, "act-1")]
    assert conn.execute("SELECT value FROM secret_rollback").fetchone()[0] == value
    assert rdb.secret_to_ref(conn, cls, path, field, ref, value) is False          # reprise : rien de plus
    rdb.secret_from_ref(conn, cls, path, field, value)
    assert read(conn) == value and rdb.secret_rollback_rows(conn) == []


def test_other_values_untouched(conn):
    rdb.secret_to_ref(conn, "replication-hook", "rp-1/pre", sc.header_field("Authorization"),
                      "openbao:replication-hook/rp-1/pre#x", "AUTH")
    opts = json.loads(conn.execute("SELECT options FROM plans").fetchone()[0])
    assert opts["hooks"][0]["secret_headers"]["X-Other"] == "OTHER"
    assert rdb.get_setting(conn, "notify") == NOTIFY


@pytest.mark.parametrize("cls,path,field,value,exc", [
    ("replication-notify", "global", "mx_1__token", "changed", ValueError),      # valeur changée entre-temps
    ("replication-notify", "global", "nope__token", "T0K", KeyError),            # canal absent
    ("replication-notify", "global", "mx_1__password", "T0K", KeyError),         # secret d'une autre nature
    ("replication-hook", "rp-2/pre", sc.header_field("Authorization"), "AUTH", KeyError),  # plan absent
    ("replication-hook", "rp-1/post", sc.header_field("Authorization"), "AUTH", KeyError),  # action absente
    ("replication-hook", "rp-1/pre", sc.header_field("X-Missing"), "AUTH", KeyError),       # en-tête absent
    ("tf-kvm", "a/b/c", "value", "x", ValueError),
])
def test_refusals_touch_nothing(conn, cls, path, field, value, exc):
    before = (rdb.get_setting(conn, "notify"), conn.execute("SELECT options FROM plans").fetchone()[0])
    with pytest.raises(exc):
        rdb.secret_to_ref(conn, cls, path, field, f"openbao:{cls}/{path}#{field}", value)
    assert (rdb.get_setting(conn, "notify"), conn.execute("SELECT options FROM plans").fetchone()[0]) == before
    assert rdb.secret_rollback_rows(conn) == [] and not conn.in_transaction


def test_rollback_delete(conn):
    rdb.secret_to_ref(conn, "replication-notify", "global", "mx_1__token", "openbao:replication-notify/global#mx_1__token",
                      "T0K")
    assert rdb.secret_rollback_delete(conn, "replication-notify", "global", "mx_1__token") == 1
    assert rdb.secret_rollback_delete(conn, "replication-notify", "global", "mx_1__token") == 0
    assert rdb.get_setting(conn, "notify")["channels"][0]["token"].startswith("openbao:")     # la référence reste
