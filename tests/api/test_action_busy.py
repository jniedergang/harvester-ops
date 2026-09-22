"""v1.44.10 — un seul arrêt ou démarrage à la fois par cluster.

Vécu sur le banc harvlab en filmant : un démarrage resté bloqué tournait
encore quand un nouvel arrêt a été lancé sur le même cluster. L'un
isolait les nœuds et arrêtait les VMs, l'autre attendait de les relancer.
Deux opérateurs, ou un double clic, feraient la même chose.

Deux verrous, parce que la ligne de commande ne passe pas par la console :
la console refuse avec un 409, les scripts refusent par `flock`.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")
import app as wapp  # noqa: E402

COMMON = ROOT / "bin" / "lib" / "common.sh"


class _NoThread:
    """On ne lance aucun script : seule la décision nous intéresse."""

    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(wapp, "load_config",
                        lambda: {"clusters": [{"name": "c1"}, {"name": "c2"}]})
    monkeypatch.setattr(wapp.threading, "Thread", _NoThread)
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    yield wapp.ACTIONS
    wapp.ACTIONS.clear()
    wapp.ACTIONS.update(saved)


def _running(action, cluster, dry_run=False, status="running"):
    run = wapp.ActionRun("busy0000001", action, cluster, ["true"], dry_run=dry_run)
    run.status = status
    wapp.ACTIONS[run.id] = run
    return run


def test_a_shutdown_is_refused_while_a_startup_runs(registry):
    _running("startup", "c1")
    with pytest.raises(wapp.ActionBusy) as e:
        wapp.start_action("shutdown", "c1")
    assert e.value.run.id == "busy0000001"


def test_a_second_startup_is_refused_too(registry):
    _running("startup", "c1", status="starting")
    with pytest.raises(wapp.ActionBusy):
        wapp.start_action("startup", "c1")


def test_another_cluster_is_not_blocked(registry):
    _running("startup", "c1")
    assert wapp.start_action("shutdown", "c2").cluster == "c2"


def test_a_dry_run_neither_blocks_nor_is_blocked(registry):
    _running("startup", "c1")
    assert wapp.start_action("shutdown", "c1", dry_run=True).dry_run
    registry.clear()
    _running("shutdown", "c1", dry_run=True)
    assert wapp.start_action("startup", "c1").action == "startup"


def test_a_finished_sequence_does_not_block(registry):
    _running("shutdown", "c1", status="error")
    assert wapp.start_action("startup", "c1").action == "startup"


def test_the_endpoint_answers_409_with_the_running_action(registry):
    _running("startup", "c1")
    with wapp.app.test_client() as c:
        r = c.post("/api/action", json={"action": "shutdown", "cluster": "c1"})
    assert r.status_code == 409
    body = r.get_json()
    assert body["running"] == "busy0000001" and body["running_action"] == "startup"


# ---------------------------------------------------------------------------
# Le verrou des scripts (ligne de commande)
# ---------------------------------------------------------------------------

def _try_lock(tmp_path, dry_run="0"):
    script = f'''
source "{COMMON}"
HARVESTER_OPS_LOG_DIR="{tmp_path}"
CLUSTER_NAME=c1
DRY_RUN={dry_run}
acquire_cluster_lock shutdown
echo ACQUIS
'''
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=dict(os.environ, NO_COLOR="1"), timeout=30)


def test_the_script_lock_refuses_a_second_sequence(tmp_path):
    holder = subprocess.Popen(["flock", str(tmp_path / ".lock-c1"), "sleep", "20"])
    try:
        time.sleep(0.5)
        r = _try_lock(tmp_path)
        assert r.returncode == 3, r.stdout + r.stderr
        assert "ACQUIS" not in r.stdout
    finally:
        holder.kill()
        holder.wait()


def test_the_script_lock_is_free_once_released(tmp_path):
    r = _try_lock(tmp_path)
    assert r.returncode == 0 and "ACQUIS" in r.stdout
    assert "shutdown pid" in (tmp_path / ".lock-c1").read_text()


def test_a_dry_run_script_does_not_take_the_lock(tmp_path):
    holder = subprocess.Popen(["flock", str(tmp_path / ".lock-c1"), "sleep", "20"])
    try:
        time.sleep(0.5)
        r = _try_lock(tmp_path, dry_run="1")
        assert r.returncode == 0 and "ACQUIS" in r.stdout
    finally:
        holder.kill()
        holder.wait()
