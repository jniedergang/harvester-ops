"""v1.44.0 : ce qui change pendant un redémarrage de la console n'est plus perdu.

La surveillance du cluster compare chaque photo à la précédente et inscrit
les différences dans le dock et l'activité. Au démarrage, elle n'avait pas
de photo précédente : elle en prenait une de référence, qui contenait déjà
ce qui avait changé pendant l'arrêt, et ne le signalait jamais (constaté sur
harv1 : un volume créé juste avant un redémarrage n'est jamais apparu).

La dernière photo est désormais gardée sur disque, à côté de l'historique
des actions, et le premier tour après un démarrage se compare à elle.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "web"))
import app as wapp  # noqa: E402

PVC = "persistentvolumeclaims"
VM = "virtualmachines.kubevirt.io"
IMG = "virtualmachineimages.harvesterhci.io"
CLUSTER = "cw-test"


def pvc(name, rv="1"):
    return {"rv": rv, "name": f"default/{name}", "extra": {}}


def vm(name, status="Running", rv="1"):
    return {"rv": rv, "name": f"default/{name}",
            "extra": {"run_strategy": "Always", "ready": status == "Running",
                      "printable_status": status}}


def img(name, progress, imported=False):
    return {"rv": str(progress), "name": f"default/{name}",
            "extra": {"progress": progress, "display_name": name,
                      "imported": imported, "failed": False}}


@pytest.fixture()
def watch(monkeypatch, tmp_path):
    """Un watcher isolé : photos fournies par le test, état sur tmp_path,
    événements relevés au lieu d'être inscrits."""
    monkeypatch.setattr(wapp, "WATCH_STATE_DIR", tmp_path / "watch")
    monkeypatch.setattr(wapp, "_cluster_watch_state", {})
    monkeypatch.setattr(wapp, "_watch_state_saved", {})
    monkeypatch.setattr(wapp, "_watch_resumed", {})
    monkeypatch.setattr(wapp, "_image_upload_actions", {})
    events = []
    monkeypatch.setattr(wapp, "_record_cluster_event",
                        lambda cluster, kind, op, name, while_down=False:
                        events.append((kind, op, name, while_down)))

    class W:
        dir = tmp_path / "watch"

        def tick(self, snaps):
            monkeypatch.setattr(wapp, "_cluster_snapshot_all", lambda kc, res: snaps)
            wapp._cluster_watch_iteration(CLUSTER, "/kc")

        def restart(self):
            """Un nouveau processus : plus rien en mémoire, le disque reste."""
            for d in (wapp._cluster_watch_state, wapp._watch_state_saved,
                      wapp._watch_resumed, wapp._image_upload_actions):
                d.clear()

    w = W()
    w.events = events
    return w


def uploads(name):
    return [a for a in list(wapp.ACTIONS.values())
            if a.to_dict().get("action") == f"harvester:vm-image-upload:default/{name}"]


def test_the_snapshot_is_kept_on_disk(watch):
    watch.tick({PVC: {"u1": pvc("data")}, VM: {"v1": vm("web")}})
    path = watch.dir / f"{CLUSTER}.json"
    saved = json.loads(path.read_text())
    assert saved["kinds"][PVC]["u1"]["name"] == "default/data"
    assert saved["kinds"][VM]["v1"]["extra"]["printable_status"] == "Running"
    # la version de ressource change sans cesse et ne sert pas à comparer
    assert "rv" not in saved["kinds"][PVC]["u1"]
    # un inventaire du cluster : lisible par le seul compte du service
    assert (path.stat().st_mode & 0o777) == 0o600


def test_changes_made_while_the_console_was_stopped_are_reported(watch):
    watch.tick({PVC: {"u1": pvc("old")}, VM: {"v1": vm("web")}})
    assert watch.events == []
    watch.restart()
    watch.tick({PVC: {"u2": pvc("new")}, VM: {"v1": vm("web", "Stopped")}})
    assert sorted(watch.events) == sorted([
        ("pvc", "created", "default/new", True),
        ("pvc", "deleted", "default/old", True),
        ("vm", "phase-stopped", "default/web", True),
    ])


def test_after_the_first_round_events_are_live_again(watch):
    watch.tick({PVC: {"u1": pvc("a")}})
    watch.restart()
    watch.tick({PVC: {"u1": pvc("a")}})
    watch.tick({PVC: {"u1": pvc("a"), "u2": pvc("b")}})
    assert watch.events == [("pvc", "created", "default/b", False)]


def test_without_a_saved_snapshot_the_first_round_is_a_baseline(watch):
    watch.tick({PVC: {"u1": pvc("a")}, VM: {"v1": vm("web")}})
    assert watch.events == []


def test_a_corrupt_snapshot_is_ignored_and_rewritten(watch):
    watch.dir.mkdir(parents=True)
    (watch.dir / f"{CLUSTER}.json").write_text("{not json")
    watch.tick({PVC: {"u1": pvc("a")}})
    assert watch.events == []
    saved = json.loads((watch.dir / f"{CLUSTER}.json").read_text())
    assert "u1" in saved["kinds"][PVC]


def test_a_malformed_snapshot_is_ignored(watch):
    watch.dir.mkdir(parents=True)
    (watch.dir / f"{CLUSTER}.json").write_text(json.dumps(
        {"kinds": {PVC: ["not", "a", "dict"], VM: {"v1": "nope"}}}))
    watch.tick({PVC: {"u1": pvc("a")}, VM: {"v1": vm("web")}})
    assert watch.events == []


def test_a_damaged_type_is_discarded_whole_and_alone(watch):
    """Écarter la seule entrée abîmée ferait passer la VM « b », au premier
    tour, pour une VM créée pendant l'arrêt : le type entier repart d'une
    photo de référence. Les autres types, intacts, restent comparés."""
    watch.dir.mkdir(parents=True)
    (watch.dir / f"{CLUSTER}.json").write_text(json.dumps(
        {"kinds": {PVC: {"u1": {"name": "default/a", "extra": {}}},
                   VM: {"v1": {"name": "default/web", "extra": {}}, "v2": {"name": 42}}}}))
    watch.tick({PVC: {"u2": pvc("b")}, VM: {"v1": vm("web"), "v2": vm("b")}})
    assert sorted(watch.events) == sorted([("pvc", "created", "default/b", True),
                                           ("pvc", "deleted", "default/a", True)])


def test_a_type_that_is_not_a_table_does_not_spoil_the_others(watch):
    watch.dir.mkdir(parents=True)
    (watch.dir / f"{CLUSTER}.json").write_text(json.dumps(
        {"kinds": {PVC: ["not", "a", "dict"],
                   VM: {"v1": {"name": "default/web",
                               "extra": {"printable_status": "Running"}}}}}))
    watch.tick({PVC: {"u1": pvc("a")}, VM: {"v1": vm("web", "Stopped")}})
    assert watch.events == [("vm", "phase-stopped", "default/web", True)]


def test_nothing_is_written_when_nothing_changed(watch):
    """Toutes les 15 s : la version de ressource bouge sans cesse, la photo
    gardée ne doit changer (et le disque être écrit) qu'avec l'inventaire."""
    watch.tick({PVC: {"u1": pvc("a", rv="1")}})
    path = watch.dir / f"{CLUSTER}.json"
    first = path.stat().st_mtime_ns
    os.utime(path, ns=(first - 10**9, first - 10**9))
    watch.tick({PVC: {"u1": pvc("a", rv="2")}})
    assert path.stat().st_mtime_ns == first - 10**9
    watch.tick({PVC: {"u1": pvc("a", rv="3"), "u2": pvc("b")}})
    assert path.stat().st_mtime_ns != first - 10**9
    assert not list(watch.dir.glob("*.tmp")), "écriture atomique : aucun reste"


def test_an_upload_in_progress_is_followed_across_a_restart(watch):
    watch.tick({IMG: {"i1": img("iso", 40)}})
    assert len(uploads("iso")) == 1
    watch.restart()
    watch.tick({IMG: {"i1": img("iso", 60)}})
    rid = wapp._image_upload_actions["i1"]
    run = wapp.ACTIONS[rid]
    assert run.status == "running"
    assert "60%" in run.events[-1]["message"]
    # l'action du processus précédent n'est pas comptée : une seule nouvelle
    assert len([a for a in uploads("iso") if a is not wapp.ACTIONS.get(rid)]) == 1


def test_an_upload_started_while_down_opens_one_action(watch):
    watch.tick({IMG: {}})
    watch.restart()
    before = len(uploads("fresh"))
    watch.tick({IMG: {"i9": img("fresh", 10)}})
    assert ("vm-image", "created", "default/fresh", True) in watch.events
    assert len(uploads("fresh")) == before + 1


def test_the_file_name_cannot_escape_the_directory(watch):
    for name in ("../evil", "a/b", "..", ""):
        assert wapp._watch_state_path(name).parent == watch.dir


def test_an_unwritable_directory_does_not_stop_the_watcher(watch, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(wapp, "WATCH_STATE_DIR", blocker / "watch")
    watch.tick({PVC: {"u1": pvc("a")}})
    watch.tick({PVC: {"u1": pvc("a"), "u2": pvc("b")}})
    assert watch.events == [("pvc", "created", "default/b", False)]


def test_the_event_says_it_happened_while_the_console_was_stopped(monkeypatch):
    runs = []
    monkeypatch.setattr(wapp.ActionRun, "close", lambda self: runs.append(self))
    wapp._record_cluster_event(CLUSTER, "pvc", "created", "default/x", while_down=True)
    wapp._record_cluster_event(CLUSTER, "pvc", "created", "default/y")
    down, live = (r.events[0]["message"] for r in runs)
    assert "while the console was not watching" in down
    assert "while the console was not watching" not in live


def test_the_state_lives_next_to_the_action_history():
    """Même persistance que l'historique : si l'un survit à un redémarrage,
    l'autre aussi."""
    assert wapp.WATCH_STATE_DIR.parent == wapp.ACTIONS_DB.parent
