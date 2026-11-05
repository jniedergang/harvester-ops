"""v1.54.0 : les déclarations Terraform gardées par la console.

Elles vivaient dans le navigateur (localStorage) : ni partagées, ni
sauvegardées. Le magasin SQLite garde un nom unique par cluster (le renommage
contrôle les doublons), une révision contre les écrasements silencieux entre
opérateurs, et ne touche jamais à Terraform.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import tf_store as ts  # noqa: E402

A, B, C = "a" * 12, "b" * 12, "c" * 12
VM = {"id": "0123456789ab", "kind": "vm", "spec": {"name": "web-01"}}


@pytest.fixture
def store(tmp_path):
    return ts.DeclStore(tmp_path / "tf.db")


def test_create_list_get(store):
    d = store.create(A, "harv1", "  web   prod ", "front", [VM], user="ju")
    assert d["name"] == "web prod" and d["revision"] == 1 and d["created_by"] == "ju"
    assert d["resources"] == [VM]
    store.create(B, "harv3", "other")
    assert [x["id"] for x in store.list("harv1")] == [A]
    assert len(store.list()) == 2
    assert store.get("not-an-id") is None


@pytest.mark.parametrize("name", ["", "   ", "x" * 81, "bad\x07name"])
def test_invalid_names_are_refused(store, name):
    with pytest.raises(ts.StoreError) as e:
        store.create(A, "harv1", name)
    assert e.value.code == "invalid-name"


def test_names_are_unique_per_cluster_whatever_the_case(store):
    store.create(A, "harv1", "web")
    with pytest.raises(ts.StoreError) as e:
        store.create(B, "harv1", "WEB")
    assert e.value.code == "name-taken"
    store.create(C, "harv3", "web")                       # autre cluster : permis


def test_rename_checks_duplicates_and_bumps_the_revision(store):
    store.create(A, "harv1", "web")
    store.create(B, "harv1", "db")
    with pytest.raises(ts.StoreError) as e:
        store.update(B, 1, name="Web")
    assert e.value.code == "name-taken"
    d = store.update(A, 1, name="web-2026", user="ops-2")
    assert d["name"] == "web-2026" and d["revision"] == 2 and d["updated_by"] == "ops-2"
    assert store.update(A, 2, name="web-2026")["revision"] == 3   # même nom : permis


def test_a_stale_revision_is_a_conflict_with_the_current_version(store):
    store.create(A, "harv1", "web")
    store.update(A, 1, description="first")
    with pytest.raises(ts.Conflict) as e:
        store.update(A, 1, description="second")
    assert e.value.facts["current"]["description"] == "first"


def test_resources_are_validated(store):
    for bad in ("x", [1], [{"id": "short", "kind": "vm"}], [{"id": A, "kind": "lb"}],
                [dict(VM), dict(VM)]):
        with pytest.raises(ts.StoreError):
            store.create(A, "harv1", "web", resources=bad)


def test_marks_and_drops_do_not_need_the_revision(store):
    store.create(A, "harv1", "web", resources=[VM, {"id": B, "kind": "ssh_key", "spec": {}}])
    d = store.mark_applied(A, "done", user="ju")
    assert d["last_applied_status"] == "done" and d["revision"] == 1
    d = store.drop_resources(A, [VM["id"]])
    assert [r["kind"] for r in d["resources"]] == ["ssh_key"] and d["revision"] == 2
    assert store.delete(A) and not store.delete(A)


# ---------------------------------------------------------------------------
# 2.4.0 : origine d'une déclaration produite par un plan de déploiement
# ---------------------------------------------------------------------------
ORIGIN = {"plan": "site-standard", "template": "reseaux-site", "template_revision": 2, "plan_revision": 4}


def test_origin_written_at_creation_and_never_updated(store):
    d = store.create(A, "harv1", "site-standard-reseaux-site", resources=[VM], origin=ORIGIN)
    assert d["origin"] == ORIGIN
    assert store.create(B, "harv1", "hand-made")["origin"] is None
    d2 = store.update(A, 1, description="edited by hand")
    assert d2["origin"] == ORIGIN and d2["revision"] == 2


def test_update_without_any_field_keeps_the_revision(store):
    store.create(A, "harv1", "web")
    assert store.update(A, 1)["revision"] == 1
    with pytest.raises(ts.Conflict):
        store.update(A, 7)                           # révision toujours contrôlée


@pytest.mark.parametrize("origin,why", [
    ("site", "origin needs"),
    ({"plan": "p"}, "origin needs"),
    (dict(ORIGIN, extra=1), "origin needs"),
    (dict(ORIGIN, plan=""), "origin plan is not a name"),
    (dict(ORIGIN, template=3), "origin template is not a name"),
    (dict(ORIGIN, plan="p" * 64), "origin plan is not a name"),
    (dict(ORIGIN, plan="Site"), "origin plan is not a name"),         # revue M7 : RFC 1123
    (dict(ORIGIN, template="../x"), "origin template is not a name"),
    (dict(ORIGIN, template="-a"), "origin template is not a name"),
    (dict(ORIGIN, plan_revision="4"), "origin plan_revision is not a revision number"),
    (dict(ORIGIN, template_revision=True), "origin template_revision is not a revision number"),
    (dict(ORIGIN, template_revision=-1), "origin template_revision is not a revision number"),
])
def test_invalid_origins_are_refused(store, origin, why):
    with pytest.raises(ts.StoreError) as e:
        store.create(A, "harv1", "web", origin=origin)
    assert e.value.code == "invalid-origin" and str(e.value).startswith(why)
    assert store.get(A) is None


def test_find_by_name(store):
    store.create(A, "harv1", "Site-Web")
    store.create(B, "harv2", "site-web")
    store.create(C, "harv1", "site-web-kvm", target_kind="kvm")
    assert store.find_by_name("harv1", "site-web")["id"] == A        # casse ignorée
    assert store.find_by_name("harv2", "SITE-WEB")["id"] == B
    assert store.find_by_name("harv1", "site-web-kvm") is None       # autre famille
    assert store.find_by_name("harv1", "site-web-kvm", target_kind="kvm")["id"] == C
    assert store.find_by_name("harv3", "site-web") is None


def test_a_230_database_is_migrated_in_place(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE tf_declarations (
      id TEXT PRIMARY KEY, cluster TEXT NOT NULL, name TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '', resources TEXT NOT NULL DEFAULT '[]',
      revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
      created_by TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
      updated_by TEXT NOT NULL DEFAULT '', last_applied_at TEXT, last_applied_status TEXT,
      last_applied_by TEXT, target_kind TEXT NOT NULL DEFAULT 'harvester')""")
    c.execute("INSERT INTO tf_declarations (id, cluster, name, resources, revision, created_at, updated_at, "
              "last_applied_status) VALUES (?, 'harv1', 'old', '[]', 3, 'x', 'x', 'done')", (A,))
    c.commit()
    c.close()
    store = ts.DeclStore(path)
    d = store.get(A)
    assert d["origin"] is None and d["revision"] == 3 and d["last_applied_status"] == "done"
    ts.DeclStore(path)                                 # deuxième ouverture : rien à migrer
    assert store.create(B, "harv1", "new", origin=ORIGIN)["origin"] == ORIGIN


# 2.4.0 (revue B2-I2) : la console et la ligne de commande écrivent la même
# base ; l'unicité du nom tient même quand la vérification applicative de
# l'une ne voit pas encore l'écriture de l'autre.
def test_name_unique_in_the_database_itself(store, monkeypatch):
    store.create(A, "harv1", "Web")
    # vérification applicative court-circuitée : seul l'index arrête le doublon
    monkeypatch.setattr(store, "_name_taken", lambda *a, **k: False)
    with pytest.raises(ts.StoreError) as e:
        store.create(B, "harv1", "web")
    assert e.value.code == "name-taken"
    store.create(B, "harv2", "web")                       # autre cluster : permis
    store.create(C, "harv1", "web", target_kind="kvm")    # autre famille : permis
    d = store.create("d" * 12, "harv1", "other")
    with pytest.raises(ts.StoreError) as e:
        store.update(d["id"], 1, name="WEB")
    assert e.value.code == "name-taken"


def test_existing_duplicates_keep_the_database_usable(tmp_path):
    import sqlite3
    db = tmp_path / "tf.db"
    ts.DeclStore(db)
    c = sqlite3.connect(db)
    c.execute("DROP INDEX tf_decl_name_unique")
    for i in (A, B):
        c.execute("INSERT INTO tf_declarations (id, cluster, name, created_at, updated_at) "
                  "VALUES (?, 'harv1', 'dup', 'x', 'x')", (i,))
    c.commit()
    c.close()
    s = ts.DeclStore(db)                                  # ne lève pas
    assert len(s.list("harv1")) == 2
