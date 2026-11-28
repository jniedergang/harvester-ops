"""2.10.2 : une déclaration dont l'action vient de finir n'est plus dite occupée.

Vu au banc 2.10.1 : un destroy lancé juste après son dry-run, rendu « done »,
recevait 409 `running`. Le fil d'une exécution écrit son statut final avant
son `finally` (fin de la session OpenBao, finalisation du chiffrement,
nettoyage KVM), qui seul rend la déclaration. Le lancement attend désormais
ce `finally` quand la seule action de la déclaration est finie ; une action
encore en cours ou une réservation par un autre déroulé est refusée sans
attendre.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402
import tf_store as ts  # noqa: E402

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJdD pkg"


@pytest.fixture
def decl(tmp_path, monkeypatch):
    store = ts.DeclStore(tmp_path / "tf.db")
    monkeypatch.setattr(wapp, "TF_DECLS", store)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path / "ws")
    started = []
    monkeypatch.setattr(wapp, "_tf_decl_runner", lambda run, *a: started.append(run.id))
    d = store.create("c" * 12, "harv1", "settle", resources=[
        {"id": "0" * 12, "kind": "ssh_key", "spec": {"name": "k", "namespace": "default", "public_key": KEY}}])
    yield d, started
    with wapp.TF_DECL_RUNNING_LOCK:
        wapp.TF_DECL_RUNNING.discard(d["id"])
        wapp.TF_DECL_HELD.pop(d["id"], None)
        wapp._flock_release(wapp.TF_DECL_FDS.pop(d["id"], None))
    with wapp.ACTIONS_LOCK:
        for k in [k for k, r in wapp.ACTIONS.items() if (r.result or {}).get("declaration_id") == d["id"]]:
            wapp.ACTIONS.pop(k, None)


def previous(d, status):
    """L'action précédente de la déclaration, au statut donné, et la
    déclaration encore tenue par son fil."""
    run = wapp.ActionRun("p" + d["id"][:11], f"tf-destroy-decl:{d['name']}:1", "harv1", [], dry_run=True)
    run.result = {"declaration_id": d["id"], "mode": "destroy-plan"}
    run.status = status
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[run.id] = run
    with wapp.TF_DECL_RUNNING_LOCK:
        wapp.TF_DECL_RUNNING.add(d["id"])
    return run


def release_after(d, seconds):
    def go():
        time.sleep(seconds)
        with wapp.TF_DECL_RUNNING_LOCK:
            wapp.TF_DECL_RUNNING.discard(d["id"])
            wapp._tf_decl_unlock_if_idle(d["id"])
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def launch(d, holder=None):
    return wapp._tf_decl_launch("harv1", "/kc", d["id"], "destroy", user="ju", role="admin", holder=holder)


@pytest.mark.parametrize("status", ["done", "error", "cancelled"])
def test_a_launch_right_after_a_finished_run_waits_for_its_finally(decl, status):
    d, started = decl
    previous(d, status)
    t = release_after(d, 0.3)
    run_id, _h = launch(d)
    t.join()
    assert started == [run_id]


@pytest.mark.parametrize("case", ["running", "never-released", "held-elsewhere"])
def test_a_busy_declaration_is_still_refused(decl, monkeypatch, case):
    d, started = decl
    monkeypatch.setattr(wapp, "TF_DECL_SETTLE_S", 0.4)
    if case == "running":
        previous(d, "running")
    elif case == "never-released":
        previous(d, "done")
    else:
        previous(d, "done")               # action finie, mais un autre déroulé réserve la déclaration
        with wapp.TF_DECL_RUNNING_LOCK:
            wapp.TF_DECL_HELD[d["id"]] = "plan-run-other"
    t0 = time.time()
    with pytest.raises(wapp.TfLaunchError) as e:
        launch(d, holder="plan-run-mine")
    took = time.time() - t0
    assert e.value.status == 409 and e.value.payload["code"] == "running"
    assert started == []
    if case == "never-released":
        assert 0.35 <= took < 2           # attendu le temps borné, pas davantage
    else:
        assert took < 0.3                 # refus immédiat, aucune attente
