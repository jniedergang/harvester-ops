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
