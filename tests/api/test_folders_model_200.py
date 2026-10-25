"""2.0.0 (tâche 12) : la logique pure des dossiers (bin/lib/folders.py).

Noms et chemins bornés (décision 4 : profondeur 8, chemin 253, nom 1 à 63),
clés des objets, réalignement « l'objet fait foi », sans héritage, plans de
renommage, de déplacement et d'annulation, contrôle de suppression qui ne
compte que ce que la personne voit."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import folders as fm  # noqa: E402

NOW = "2026-10-25T08:00:00Z"


# ---------------------------------------------------------------------------
# Noms et chemins : un cas par règle
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,reason", [
    ("ERP", None), ("Production ERP", None), ("é-ü_1.2", None), ("a" * 63, None), ("...", None),
    (".hidden", None),
    ("", "the folder name is empty"),
    (None, "the folder name is empty"),
    ("a" * 64, "the folder name is longer than 63 characters"),
    ("a/b", "the folder name contains '/'"),
    ("a\tb", "the folder name contains a control character"),
    ("a\x7fb", "the folder name contains a control character"),
    (".", "the folder name cannot be '.' or '..'"),
    ("..", "the folder name cannot be '.' or '..'"),
])
def test_folder_names(name, reason):
    assert fm.check_name(name) == reason
    if reason is None:
        assert fm.FOLDER_NAME_RE.match(name)


@pytest.mark.parametrize("path,reason", [
    ("/A", None), ("/Production/ERP", None), ("/" + "/".join("abcdefgh"), None),
    ("/" + "x" * 63 + "/" + "y" * 63 + "/" + "z" * 63 + "/" + "w" * 60, None),          # 253 car.
    ("", "the folder path is empty"),
    ("A/B", "the folder path must start with '/'"),
    ("/" + "x" * 63 + "/" + "y" * 63 + "/" + "z" * 63 + "/" + "w" * 61,
     "the folder path is longer than 253 characters"),
    ("/", "the folder path must name a folder (no trailing '/')"),
    ("/A/", "the folder path must name a folder (no trailing '/')"),
    ("/" + "/".join("abcdefghi"), "the folder path is deeper than 8 levels"),
    ("/A//B", "the folder name is empty ('')"),
    ("/A/../B", "the folder name cannot be '.' or '..' ('..')"),
    ("/A/" + "b" * 64, f"the folder name is longer than 63 characters ('{'b' * 64}')"),
])
def test_folder_paths(path, reason):
    assert fm.check_path(path) == reason


@pytest.mark.parametrize("kind,parts,key", [
    ("harvester", {"cluster": "harv1", "namespace": "prod", "name": "erp"}, "harvester/harv1/prod/erp"),
    ("kvm", {"uuid": "851F88C6-88B0-4E6A-85A6-ACF4BB595771"}, "kvm/851f88c6-88b0-4e6a-85a6-acf4bb595771"),
    ("container", {"cluster": "harv1", "namespace": "apps", "workload_kind": "StatefulSet", "name": "pg"},
     "container/harv1/apps/statefulset/pg"),
    ("host", {"family": "kvm", "name": "node2"}, "host/kvm/node2"),
    ("host", {"family": "harvester", "name": "harv1"}, "host/harvester/harv1"),
])
def test_object_keys(kind, parts, key):
    assert fm.object_key(kind, **parts) == key


@pytest.mark.parametrize("kind,parts", [
    ("harvester", {"cluster": "harv1", "name": "erp"}),
    ("container", {"cluster": "c", "namespace": "n", "workload_kind": "Job", "name": "x"}),
    ("container", {"cluster": "c", "namespace": "n", "workload_kind": "Pod", "name": "x"}),
    ("host", {"family": "vmware", "name": "x"}),
    ("vm", {"name": "x"}),
    ("harvester", {"cluster": "a/b", "namespace": "n", "name": "x"}),
])
def test_object_key_refusals(kind, parts):
    with pytest.raises(ValueError):
        fm.object_key(kind, **parts)


def test_rangeable_workloads_are_the_four_of_decision_11():
    assert set(fm.WORKLOAD_RESOURCES) == {"Deployment", "StatefulSet", "DaemonSet", "CronJob"}


# ---------------------------------------------------------------------------
# Réalignement
# ---------------------------------------------------------------------------
TREE = [{"id": 1, "parent_id": None, "name": "A"}, {"id": 2, "parent_id": 1, "name": "B"},
        {"id": 3, "parent_id": None, "name": "C"}, {"id": 4, "parent_id": 2, "name": "D"}]


def test_paths_and_index():
    assert fm.paths(TREE) == {1: "/A", 2: "/A/B", 3: "/C", 4: "/A/B/D"}
    assert fm.index(TREE)["/A/B/D"] == 4
    assert sorted(fm.subtree_ids(TREE, 1)) == [1, 2, 4] and fm.subtree_depth(TREE, 1) == 3


def test_rebuild_everything_from_objects_alone():
    """Base perdue : les objets seuls recréent les dossiers et le cache."""
    rows = [{"key": "harvester/h/n/a", "folder": "/Production/ERP", "scope": "harvester/h"},
            {"key": "kvm/u1", "folder": "/Production/Web", "scope": "kvm/node2"},
            {"key": "host/kvm/node2", "folder": "/Lab", "scope": "hosts"},
            {"key": "kvm/u2", "folder": None, "scope": "kvm/node2"}]
    mem, created = fm.reconcile([], rows, {}, NOW)
    assert created == ["/Lab", "/Production", "/Production/ERP", "/Production/Web"]
    assert mem == {"harvester/h/n/a": {"folder_path": "/Production/ERP", "source": "vm", "seen_at": NOW,
                                       "scope": "harvester/h"},
                   "kvm/u1": {"folder_path": "/Production/Web", "source": "vm", "seen_at": NOW,
                              "scope": "kvm/node2"},
                   "host/kvm/node2": {"folder_path": "/Lab", "source": "vm", "seen_at": NOW, "scope": "hosts"}}


@pytest.mark.parametrize("obj,cached,expected", [
    # l'objet dit /A/B, le cache /C : l'objet gagne
    ("/A/B", {"folder_path": "/C", "source": "console"}, ("/A/B", "vm")),
    ("/A/B", {"folder_path": "/C", "source": "vm"}, ("/A/B", "vm")),
    # objet sans valeur, cache « console » : le cache reste
    (None, {"folder_path": "/C", "source": "console"}, ("/C", "console")),
    # objet sans valeur, cache « vm » : retiré sur l'objet, retiré du cache
    (None, {"folder_path": "/C", "source": "vm"}, None),
    (None, None, "absent"),
])
def test_the_object_wins(obj, cached, expected):
    cache = {"k": dict(cached, seen_at="2026-10-01T00:00:00Z", scope="s")} if cached else {}
    mem, created = fm.reconcile(TREE, [{"key": "k", "folder": obj, "scope": "s"}], cache, NOW)
    if expected == "absent":
        assert "k" not in mem
    elif expected is None:
        assert mem["k"] is None
    else:
        assert (mem["k"]["folder_path"], mem["k"]["source"], mem["k"]["seen_at"]) == (*expected, NOW)
    assert created == []


def reconcile_with_inheritance(tree, rows, cache, now):
    """Variante FAUTIVE (mutation volontaire) : les VMs d'un hôte rangé
    prennent son dossier."""
    host_folder = {r["key"].split("/", 2)[2]: r["folder"] for r in rows
                   if r["key"].startswith("host/kvm/") and r.get("folder")}
    rows = [dict(r, folder=r.get("folder") or host_folder.get(r.get("host"))) for r in rows]
    return fm.reconcile(tree, rows, cache, now)


def check_no_inheritance(reconcile):
    rows = [{"key": "host/kvm/node2", "folder": "/Lab", "scope": "hosts"},
            {"key": "kvm/u1", "folder": None, "host": "node2", "scope": "kvm/node2"}]
    mem, _ = reconcile([], rows, {}, NOW)
    assert mem.get("host/kvm/node2", {}).get("folder_path") == "/Lab"
    assert "kvm/u1" not in mem, "an unfiled VM of a filed host must stay unfiled"


def test_no_inheritance():
    check_no_inheritance(fm.reconcile)


def test_folders_mutation_inheritance():
    with pytest.raises(AssertionError):
        check_no_inheritance(reconcile_with_inheritance)


def test_effective_folder_and_source():
    rows = [{"key": "a", "folder": "/X"}, {"key": "b", "folder": None}, {"key": "c", "folder": None}]
    cache = {"b": {"folder_path": "/Y", "source": "console"}, "c": {"folder_path": "/Z", "source": "vm"}}
    assert fm.effective(rows, cache) == {"a": ("/X", "vm"), "b": ("/Y", "console"), "c": (None, None)}


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
MEMBERS = {"vm1": "/A", "vm2": "/A/B", "vm3": "/A/B/D", "vm4": "/C", "vm5": "/AB"}


def test_rename_plan_on_three_levels():
    assert fm.rename_plan(TREE, MEMBERS, 1, "Z") == [
        ("vm1", "/A", "/Z"), ("vm2", "/A/B", "/Z/B"), ("vm3", "/A/B/D", "/Z/B/D")]
    assert fm.rename_plan(TREE, MEMBERS, 2, "E") == [("vm2", "/A/B", "/A/E"), ("vm3", "/A/B/D", "/A/E/D")]


@pytest.mark.parametrize("fid,name", [(1, "a/b"), (1, ""), (1, ".."), (1, "x" * 64)])
def test_rename_plan_refuses_bad_names(fid, name):
    with pytest.raises(ValueError):
        fm.rename_plan(TREE, MEMBERS, fid, name)


def test_rename_plan_refuses_a_too_long_descendant():
    tree = [{"id": 1, "parent_id": None, "name": "a"}, {"id": 2, "parent_id": 1, "name": "b" * 63},
            {"id": 3, "parent_id": 2, "name": "c" * 63}, {"id": 4, "parent_id": 3, "name": "d" * 63}]
    with pytest.raises(ValueError, match="longer than 253"):
        fm.rename_plan(tree, {}, 1, "a" * 63)


def test_move_plan_and_own_subtree():
    assert fm.move_plan(TREE, MEMBERS, 2, 3) == [("vm2", "/A/B", "/C/B"), ("vm3", "/A/B/D", "/C/B/D")]
    assert fm.move_plan(TREE, MEMBERS, 2, None) == [("vm2", "/A/B", "/B"), ("vm3", "/A/B/D", "/B/D")]
    for target in (1, 2, 4):
        if target == 1:
            continue
        with pytest.raises(ValueError, match="into itself"):
            fm.move_plan(TREE, MEMBERS, 2, target)


def test_move_plan_refuses_too_deep():
    tree = [{"id": i, "parent_id": (i - 1) or None, "name": f"l{i}"} for i in range(1, 8)]
    tree += [{"id": 20, "parent_id": None, "name": "x"}, {"id": 21, "parent_id": 20, "name": "y"}]
    with pytest.raises(ValueError, match="deeper than 8"):
        fm.move_plan(tree, {}, 20, 7)


def test_undo_with_an_object_moved_since():
    moves = [{"object_key": "vm1", "from_path": "/A", "to_path": "/C", "status": "done"},
             {"object_key": "vm2", "from_path": None, "to_path": "/C", "status": "done"},
             {"object_key": "vm3", "from_path": "/A", "to_path": "/C", "status": "failed"},
             {"object_key": "vm4", "from_path": "/X", "to_path": None, "status": "done"}]
    current = {"vm1": "/C", "vm2": "/Elsewhere", "vm4": None}
    plan, conflicts = fm.undo_plan(moves, current)
    assert plan == [("vm1", "/C", "/A"), ("vm4", None, "/X")]
    assert conflicts == [{"key": "vm2", "expected": "/C", "current": "/Elsewhere"}]


def test_undo_of_two_moves_of_the_same_object_goes_back_to_the_first():
    moves = [{"object_key": "v", "from_path": "/A", "to_path": "/B", "status": "done"},
             {"object_key": "v", "from_path": "/B", "to_path": "/C", "status": "done"}]
    assert fm.undo_plan(moves, {"v": "/C"}) == ([("v", "/C", "/A")], [])


@pytest.mark.parametrize("fid,visible,expected", [
    (3, None, {"empty": False, "count_visible": 1, "subfolders": 0, "hidden": False}),
    (4, None, {"empty": False, "count_visible": 1, "subfolders": 0, "hidden": False}),
    # vide pour la personne mais pas pour une autre : refus, sans le nombre caché
    (4, {"vm1"}, {"empty": False, "count_visible": 0, "subfolders": 0, "hidden": True}),
    (2, {"vm2"}, {"empty": False, "count_visible": 1, "subfolders": 1, "hidden": True}),
    (5, None, {"empty": True, "count_visible": 0, "subfolders": 0, "hidden": False}),
])
def test_delete_check(fid, visible, expected):
    tree = TREE + [{"id": 5, "parent_id": None, "name": "Empty"}]
    assert fm.delete_check(tree, fid, MEMBERS, visible) == expected


@pytest.mark.parametrize("path,renames,known,expected", [
    ("/Old/x", [("/Old", "/New")], {"/New"}, "/New/x"),
    ("/Old", [("/Old", "/New")], {"/New"}, "/New"),
    ("/Older/x", [("/Old", "/New")], {"/New"}, "/Older/x"),               # préfixe de nom, pas de chemin
    ("/Old/x", [("/Old", "/New")], {"/Old", "/New"}, "/Old/x"),            # ancien chemin recréé exprès
    ("/A/x", [("/A", "/B"), ("/B", "/C")], {"/C"}, "/C/x"),                # renommages enchaînés
    ("/A/b/c/d/e/f/g/h", [("/A", "/Z/y")], set(), "/A/b/c/d/e/f/g/h"),         # dépasserait 8 niveaux
    ("/A/x", [], set(), "/A/x"),
])
def test_remap_of_a_path_during_a_rename(path, renames, known, expected):
    assert fm.remap(path, renames, known) == expected


@pytest.mark.parametrize("folder,target,want", [
    (1, None, []),               # /A vidé à la racine : son enfant B ne heurte rien
    (1, 3, ["B"]),               # /C a déjà un B
    (4, None, ["A"]),            # /D/A remonte à la racine où /A existe
    (2, 1, ["B"]),               # /A/B vidé dans /A : il existe encore, son enfant B le heurte
    (2, 3, ["B"]),
])
def test_empty_into_conflicts(folder, target, want):
    tree = [{"id": 1, "parent_id": None, "name": "A"}, {"id": 2, "parent_id": 1, "name": "B"},
            {"id": 3, "parent_id": None, "name": "C"}, {"id": 5, "parent_id": 3, "name": "B"},
            {"id": 4, "parent_id": None, "name": "D"}, {"id": 6, "parent_id": 4, "name": "A"},
            {"id": 7, "parent_id": 2, "name": "x"}, {"id": 8, "parent_id": 2, "name": "B"}]
    assert fm.empty_into_conflicts(tree, folder, target) == want
