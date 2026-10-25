"""2.0.0 (tâche 12) : le registre `folders.db` (bin/lib/folders_db.py).

Arborescence (noms frères uniques, même créés au même moment par deux
connexions), cache des rangements réaligné sur les lectures, purge à 30
jours qui épargne un hôte injoignable, journal des écritures d'une action.
La table des favoris de l'étape 2a reste intacte (migration additive)."""

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import folders_db as fdb  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = fdb.connect(tmp_path / "folders.db")
    yield c
    c.close()


def test_default_path_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_FOLDERS_DB", str(tmp_path / "x.db"))
    assert fdb.default_path() == tmp_path / "x.db"


def test_wal_and_additive_schema_keeps_favourites(tmp_path):
    path = tmp_path / "folders.db"
    old = sqlite3.connect(path)                    # base de l'étape 2a : favoris seuls
    old.executescript("CREATE TABLE favorites (user TEXT NOT NULL, host_key TEXT NOT NULL, "
                      "created_at TEXT NOT NULL, PRIMARY KEY (user, host_key));"
                      "INSERT INTO favorites VALUES ('ju', 'kvm/node2', '2026-10-25T00:00:00Z');")
    old.commit()
    old.close()
    c = fdb.connect(path)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert fdb.favorites(c, "ju") == {"kvm/node2"}
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"favorites", "folders", "memberships", "moves"} <= tables
    cols = [r[1] for r in c.execute("PRAGMA table_info(folders)")]
    assert cols == ["id", "parent_id", "name", "position", "created_at", "created_by", "origin"]
    assert [r[1] for r in c.execute("PRAGMA table_info(memberships)")] == [
        "vm_key", "folder_path", "source", "seen_at", "scope"]
    assert [r[1] for r in c.execute("PRAGMA table_info(moves)")] == [
        "action_id", "seq", "object_key", "from_path", "to_path", "status", "message", "at"]


def test_create_rename_move_delete(conn):
    a = fdb.create(conn, None, "A", "ju")
    b = fdb.create(conn, a, "B", "ju")
    c = fdb.create(conn, None, "C", "ju")
    assert [f["path"] for f in fdb.list_tree(conn)] == ["/A", "/A/B", "/C"]
    assert fdb.id_of(conn, "/A/B") == b and fdb.path_of(conn, b) == "/A/B"
    assert fdb.rename(conn, a, "Z") == ("/A", "/Z") and fdb.path_of(conn, b) == "/Z/B"
    assert fdb.move_folder(conn, b, c) == ("/Z/B", "/C/B")
    assert fdb.move_folder(conn, b, None) == ("/C/B", "/B")
    assert fdb.delete_empty(conn, b) == "/B" and fdb.path_of(conn, b) is None
    tree = {f["path"]: f for f in fdb.list_tree(conn)}
    assert tree["/Z"]["created_by"] == "ju" and tree["/Z"]["origin"] == "console"


@pytest.mark.parametrize("op,kind,match", [
    (lambda c, ids: fdb.create(c, None, "a/b", "ju"), "invalid", "'/'"),
    (lambda c, ids: fdb.create(c, None, "x" * 64, "ju"), "invalid", "longer than 63"),
    (lambda c, ids: fdb.create(c, None, "..", "ju"), "invalid", "'.' or '..'"),
    (lambda c, ids: fdb.create(c, None, "A", "ju"), "conflict", "already exists"),
    (lambda c, ids: fdb.create(c, 999, "N", "ju"), "notfound", "no folder 999"),
    (lambda c, ids: fdb.create(c, ids["l8"], "nine", "ju"), "invalid", "deeper than 8"),
    (lambda c, ids: fdb.rename(c, ids["B"], "C"), "conflict", "already exists"),
    (lambda c, ids: fdb.rename(c, 999, "C"), "notfound", "no folder"),
    (lambda c, ids: fdb.move_folder(c, ids["A"], ids["B2"]), "invalid", "into itself"),
    (lambda c, ids: fdb.move_folder(c, ids["A"], ids["A"]), "invalid", "into itself"),
    (lambda c, ids: fdb.move_folder(c, ids["Cdup"], ids["A"]), "conflict", "already exists"),
    (lambda c, ids: fdb.delete_empty(c, ids["A"]), "nonempty", "move content to another folder first"),
    (lambda c, ids: fdb.ensure_path(c, "relative", "objects"), "invalid", "start with '/'"),
])
def test_every_refusal(conn, op, kind, match):
    ids = {"A": fdb.create(conn, None, "A", "ju")}
    ids["B"] = fdb.create(conn, ids["A"], "B", "ju")
    ids["B2"] = fdb.create(conn, ids["B"], "B2", "ju")
    fdb.create(conn, ids["A"], "C", "ju")
    ids["Cdup"] = fdb.create(conn, None, "C", "ju")       # /C : même nom que /A/C
    parent = None
    for i in range(1, 9):
        parent = fdb.create(conn, parent, f"l{i}", "ju")
    ids["l8"] = parent
    with pytest.raises(fdb.FolderError) as e:
        op(conn, ids)
    assert e.value.kind == kind and match in e.value.message


def test_delete_refused_for_hidden_objects_without_the_count(conn):
    a = fdb.create(conn, None, "A", "ju")
    fdb.upsert_membership(conn, "kvm/secret-1", "/A", "vm", scope="kvm/x")
    fdb.upsert_membership(conn, "kvm/secret-2", "/A", "vm", scope="kvm/x")
    with pytest.raises(fdb.FolderError) as e:
        fdb.delete_empty(conn, a, visible=set())
    assert e.value.kind == "nonempty" and "objects you cannot see" in e.value.message
    assert "2" not in e.value.message
    with pytest.raises(fdb.FolderError) as e:
        fdb.delete_empty(conn, a, visible={"kvm/secret-1"})
    assert "1 object(s)" in e.value.message and "objects you cannot see" in e.value.message


def test_two_connections_create_the_same_sibling_only_one_passes(tmp_path):
    path = tmp_path / "folders.db"
    fdb.connect(path).close()
    barrier = threading.Barrier(2)
    results = []

    def worker():
        c = fdb.connect(path)
        barrier.wait()
        try:
            results.append(("ok", fdb.create(c, None, "Same", "x")))
        except fdb.FolderError as e:
            results.append((e.kind, None))
        finally:
            c.close()
    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(r[0] for r in results) == ["conflict", "ok"]
    c = fdb.connect(path)
    assert [f["path"] for f in fdb.list_tree(c)] == ["/Same"]


def test_ensure_path_creates_missing_levels_with_their_origin(conn):
    a = fdb.create(conn, None, "A", "ju")
    leaf = fdb.ensure_path(conn, "/A/B/C", "objects")
    tree = {f["path"]: f for f in fdb.list_tree(conn)}
    assert tree["/A"]["id"] == a and tree["/A"]["origin"] == "console"
    assert tree["/A/B"]["origin"] == tree["/A/B/C"]["origin"] == "objects" and tree["/A/B/C"]["id"] == leaf
    assert fdb.ensure_path(conn, "/A/B/C") == leaf


def test_realign_writes_only_what_changed(conn):
    rows = [{"key": "kvm/u1", "folder": "/P", "scope": "kvm/n"}]
    assert fdb.realign(conn, rows, now="2026-10-25T08:00:00Z") == {"kvm/u1": ("/P", "vm")}
    assert fdb.memberships(conn)["kvm/u1"]["seen_at"] == "2026-10-25T08:00:00Z"
    fdb.realign(conn, rows, now="2026-10-25T08:30:00Z")           # moins d'une heure : rien
    assert fdb.memberships(conn)["kvm/u1"]["seen_at"] == "2026-10-25T08:00:00Z"
    fdb.realign(conn, rows, now="2026-10-25T09:00:01Z")           # plus d'une heure : rafraîchi
    assert fdb.memberships(conn)["kvm/u1"]["seen_at"] == "2026-10-25T09:00:01Z"
    fdb.realign(conn, [{"key": "kvm/u1", "folder": "/Q", "scope": "kvm/n"}], now="2026-10-25T09:00:02Z")
    assert fdb.memberships(conn)["kvm/u1"]["folder_path"] == "/Q"
    fdb.realign(conn, [{"key": "kvm/u1", "folder": None, "scope": "kvm/n"}])
    assert "kvm/u1" not in fdb.memberships(conn)


@pytest.mark.parametrize("age_days,purged", [(29, False), (31, True)])
def test_purge_at_29_and_31_days(conn, age_days, purged):
    import datetime as dt
    now = dt.datetime(2026, 10, 25, 8, 0, 0)
    seen = (now - dt.timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fdb.upsert_membership(conn, "kvm/u1", "/A", "console", seen, scope="kvm/node2")
    assert fdb.purge(conn, now, scopes=["kvm/node2"]) == (1 if purged else 0)
    assert ("kvm/u1" in fdb.memberships(conn)) is not purged


def test_an_unreachable_host_keeps_its_entries_until_it_is_read_again(conn):
    old = "2026-08-01T00:00:00Z"
    fdb.upsert_membership(conn, "kvm/u1", "/A", "console", old, scope="kvm/node2")
    fdb.upsert_membership(conn, "kvm/u2", "/A", "console", old, scope="kvm/off")
    # seul node2 a été lu : l'hôte injoignable garde ses entrées
    assert fdb.purge(conn, "2026-10-25T00:00:00Z", scopes=["kvm/node2"]) == 1
    assert set(fdb.memberships(conn)) == {"kvm/u2"}
    assert fdb.purge(conn, "2026-10-25T00:00:00Z", scopes=[]) == 0
    assert fdb.purge(conn, "2026-10-25T00:00:00Z", scopes=["kvm/off"]) == 1


def test_journal_of_an_action(conn):
    fdb.record_move(conn, "a1", 0, "kvm/u1", None, "/A")
    fdb.record_move(conn, "a1", 1, "kvm/u2", "/B", "/A")
    fdb.set_move_status(conn, "a1", 0, "done")
    assert [m["object_key"] for m in fdb.pending_moves(conn, "a1")] == ["kvm/u2"]
    m = fdb.moves_of(conn, "a1")
    assert (m[0]["status"], m[0]["object_key"], m[0]["to_path"]) == ("done", "kvm/u1", "/A")
    with pytest.raises(ValueError):
        fdb.record_move(conn, "a1", 2, "k", None, "/A", status="weird")


# ---------------------------------------------------------------------------
# Étape 3b (relecture M-8) : une lecture ne remplit pas l'arborescence partagée
# ---------------------------------------------------------------------------
def _rows(n, depth=1, prefix="/Flood"):
    return [{"key": f"harvester/c/ns/vm{i}", "folder": prefix + "".join(f"/L{i}-{d}" for d in range(depth)),
             "scope": "harvester/c"} for i in range(n)]


@pytest.mark.parametrize("rows,cap,created,overflow,cached", [
    # sous le plafond : tout est créé
    (_rows(3), 200, 4, 0, 3),
    # plafond atteint pile : /Flood + 199 dossiers
    (_rows(199), 200, 200, 0, 199),
    # au-delà : 200 créés, le reste attend ; les objets concernés gardent leur cache
    (_rows(250), 200, 200, 51, 199),
    # chemins profonds : un ancêtre non créé retient tout son sous-arbre
    (_rows(2, depth=3), 3, 3, 4, 0),
    # plafond 0 : rien n'est créé, rien n'est rangé
    (_rows(2), 0, 0, 3, 0),
])
def test_realign_caps_the_folders_created_by_one_read(conn, rows, cap, created, overflow, cached):
    stats = {}
    fdb.realign(conn, rows, max_create=cap, stats=stats)
    assert stats == {"created": created, "overflow": overflow}
    assert len(fdb.list_tree(conn)) == created
    assert all(f["origin"] == "objects" for f in fdb.list_tree(conn))
    assert len(fdb.memberships(conn)) == cached


def test_realign_cap_keeps_the_cache_of_an_object_left_waiting(conn):
    fdb.ensure_path(conn, "/Prod", "console")
    fdb.upsert_membership(conn, "harvester/c/ns/vm0", "/Prod", "console")
    fdb.realign(conn, _rows(1), max_create=0)
    assert fdb.memberships(conn)["harvester/c/ns/vm0"]["folder_path"] == "/Prod"     # rien de changé
    fdb.realign(conn, _rows(1))                                                       # plafond normal
    assert fdb.memberships(conn)["harvester/c/ns/vm0"]["folder_path"] == "/Flood/L0-0"


def test_realign_cap_default_is_200(conn):
    assert fdb.MAX_CREATED_PER_READ == 200
    stats = {}
    fdb.realign(conn, _rows(300), stats=stats)
    assert stats["created"] == 200
