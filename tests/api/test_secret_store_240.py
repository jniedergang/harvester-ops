"""2.4.0 : magasin de secrets interne (bin/lib/secret_store.py).

* références `local:<classe>/<chemin>#<champ>` : bien formées et refus ;
* put / get / delete, `SecretMissing` sans valeur ni chemin d'état ;
* `delete_tree` n'efface que son arbre (`plan-run/a1` ne touche pas
  `plan-run/a10`) ;
* repr, pickle et json d'un magasin rempli ne portent aucune valeur ;
* fils concurrents.

Mutations jouées (voir le rapport de la tâche 1) : préfixe comparé sans
séparateur (le cas a1/a10 casse), `__repr__` qui montre le dictionnaire
(les cas repr cassent).
"""

import json
import pickle
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import secret_store as ss    # noqa: E402

VALUE = "S3CRET-value-240"


@pytest.mark.parametrize("ref,ok", [
    ("local:plan-run/a1b2c3d4e5f6/install#token", True),
    ("local:plan-run/a1/bmc/0#password", True),
    ("local:plan-run/a1#token", True),
    ("local:plan-run/" + "a" * 200 + "#f", True),
    ("local:plan-run/" + "a" * 201 + "#f", False),        # 201 caractères
    ("local:other/a1#token", False),                       # classe inconnue
    ("local:plan-run/a1/../b#token", False),               # remontée
    ("local:plan-run/../b#token", False),
    ("local:plan-run/./b#token", False),
    ("local:plan-run//etc/x#token", False),                # chemin absolu
    ("local:plan-run/a1//b#token", False),                 # segment vide
    ("local:plan-run/a1/#token", False),
    ("local:plan-run/a1#Token", False),                    # champ en majuscules
    ("local:plan-run/A1#token", False),                    # chemin en majuscules
    ("local:plan-run/a1#" + "f" * 65, False),
    ("local:plan-run/a1", False),                          # pas de champ
    ("openbao:kv/a1#token", False),                        # autre support
    ("", False),
    (None, False),
    (42, False),
])
def test_ref_shape(ref, ok):
    assert ss.is_ref(ref) is ok
    if ok:
        assert ss.parse_ref(ref)[0] == "plan-run"
    else:
        with pytest.raises(ValueError):
            ss.parse_ref(ref)


@pytest.mark.parametrize("cls,path,field", [
    ("nope", "a1", "token"),
    ("plan-run", "/a1", "token"),
    ("plan-run", "a1/..", "token"),
    ("plan-run", "a1", "TOKEN"),
    ("plan-run", "", "token"),
])
def test_put_refuses_bad_refs(cls, path, field):
    st = ss.LocalStore()
    with pytest.raises(ValueError):
        st.put(cls, path, field, VALUE)
    assert st.refs("plan-run", "a1") == []


def test_put_get_delete():
    st = ss.LocalStore()
    ref = st.put("plan-run", "a1/install", "token", VALUE)
    assert ref == "local:plan-run/a1/install#token"
    assert st.get(ref) == VALUE
    assert st.delete(ref) is True
    assert st.delete(ref) is False
    with pytest.raises(ss.SecretMissing):
        st.get(ref)


def test_put_refuses_non_string():
    with pytest.raises(TypeError):
        ss.LocalStore().put("plan-run", "a1", "token", b"bytes")


def test_persistent_class_not_implemented(monkeypatch):
    monkeypatch.setitem(ss.CLASSES, "kept", {"persist": True})
    with pytest.raises(NotImplementedError):
        ss.LocalStore().put("kept", "a1", "token", VALUE)


@pytest.mark.parametrize("ref", ["local:plan-run/a1#token", "garbage", None])
def test_secret_missing_message(ref, tmp_path):
    st = ss.LocalStore()
    st.put("plan-run", "a2", "token", VALUE)
    with pytest.raises(ss.SecretMissing) as e:
        st.get(ref)
    text = str(e.value) + repr(e.value)
    assert VALUE not in text
    assert str(tmp_path) not in text
    assert "/home" not in text and "/var" not in text


@pytest.mark.parametrize("tree,gone,kept", [
    ("a1", ["a1/install#token", "a1/bmc/0#password", "a1#x"], ["a10/install#token", "a1b#x", "b/a1#x"]),
    ("a1/bmc", ["a1/bmc/0#password"], ["a1/install#token", "a1#x", "a10/install#token"]),
    ("zz", [], ["a1#x"]),
])
def test_delete_tree_only_its_prefix(tree, gone, kept):
    st = ss.LocalStore()
    all_refs = {}
    for rel in gone + kept:
        path, field = rel.split("#")
        all_refs[rel] = st.put("plan-run", path, field, VALUE + rel)
    assert sorted(st.refs("plan-run", tree)) == sorted(all_refs[r] for r in gone)
    assert st.delete_tree("plan-run", tree) == len(gone)
    for rel in kept:
        assert st.get(all_refs[rel]) == VALUE + rel
    for rel in gone:
        with pytest.raises(ss.SecretMissing):
            st.get(all_refs[rel])


@pytest.mark.parametrize("cls,path", [("nope", "a1"), ("plan-run", "../a1"), ("plan-run", "")])
def test_delete_tree_refuses(cls, path):
    with pytest.raises(ValueError):
        ss.LocalStore().delete_tree(cls, path)


def _filled():
    st = ss.LocalStore()
    st.put("plan-run", "a1/install", "token", VALUE)
    st.put("plan-run", "a1/install", "password", VALUE + "2")
    return st


@pytest.mark.parametrize("dump", [
    repr, str,
    lambda st: pickle.dumps(st).decode("latin-1"),
    lambda st: json.dumps(st, default=lambda o: o.__getstate__()),
    lambda st: json.dumps(st, default=repr),
    lambda st: f"{st!r} {st}",
])
def test_no_value_in_representations(dump):
    st = _filled()
    assert VALUE not in dump(st)


def test_pickle_round_trip_is_empty():
    st2 = pickle.loads(pickle.dumps(_filled()))
    assert st2.refs("plan-run", "a1") == []


def test_store_is_process_wide_local():
    assert isinstance(ss.store(), ss.LocalStore)
    assert ss.store() is ss.store("/some/state")


def test_concurrent_threads():
    st = ss.LocalStore()
    errors = []

    def worker(n):
        try:
            for i in range(200):
                ref = st.put("plan-run", f"r{n}/item", f"f{i}", f"v{n}-{i}")
                assert st.get(ref) == f"v{n}-{i}"
            assert len(st.refs("plan-run", f"r{n}")) == 200
            assert st.delete_tree("plan-run", f"r{n}") == 200
        except Exception as e:     # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert all(st.refs("plan-run", f"r{n}") == [] for n in range(8))
