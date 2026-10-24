"""v1.99.0, décision 68 : un arrêt de la console n'annule pas une commande.

L'arrêt de la console (redémarrage, arrêt de l'hôte, mise à jour forcée)
demande à la commande en cours de s'arrêter au prochain point sûr
(`Ctx.safe_point`, exception `Suspended`) SANS retour arrière : la commande
est rangée `interrupted` avec l'étape atteinte, et `resume` la continue sans
refaire ce qui est fait (une restauration initiale commencée n'est pas
reprise de zéro). Seule l'annulation d'une personne (`Cancelled`) défait.

Mêmes clusters simulés que le déroulé (`test_replication_run_199.World`) :
la demande d'arrêt est posée à une étape choisie (sortie STEP_EVENT), comme
la ligne de commande la pose à la réception de SIGUSR2.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import replication_db as db  # noqa: E402
import replication_run as rr  # noqa: E402
from test_replication_run_199 import VMS, World, events, mut_keys, protected, snapshot_state  # noqa: E402

REASON = "console shutdown (SIGUSR2)"


def at_step(w, trigger, mode="suspend", **kw):
    """Contexte dont l'arrêt de la console (`suspend`) ou l'annulation d'une
    personne (`cancel`) arrive à l'étape `trigger` = (étape, statut).
    L'annulation est levée sur place (comme le gestionnaire de SIGTERM) ;
    l'arrêt est seulement demandé, le déroulé le voit à son point sûr."""
    ctx = w.ctx(**kw)
    asked = {}
    emit = ctx.emit

    def wrapped(sid, status, msg=""):
        emit(sid, status, msg)
        if (sid, status) == trigger and not asked:
            asked["at"] = (sid, status)
            if mode == "cancel":
                raise rr.Cancelled("signal 15")
            asked["reason"] = REASON
    ctx.emit = wrapped
    ctx.suspend_requested = lambda: asked.get("reason")
    return ctx, asked


def deletes(w):
    return mut_keys(w.src, "delete") + mut_keys(w.dst, "delete")


def creates(w):
    return [c for c in w.src.mutations() + w.dst.mutations() if c[0] == "create"]


def runs(w, op=None):
    return [r for r in db.list_runs(w.conn, w.pid) if op is None or r["operation"] == op]


# (déclencheur, étape rangée) : l'étape rangée est celle où le déroulé a vu la
# demande, au point sûr suivant (deux VMs : après les tâches de vm-a, le
# point sûr suivant est l'entrée de l'étape de vm-b)
PROTECT_STEPS = [
    pytest.param(("recurring-job", "done"), "recurring-job", id="jobs-created"),
    pytest.param(("first-backup", "running"), "first-backup", id="first-backup-in-progress"),
    pytest.param(("standby-volume", "done"), "standby-volume", id="standby-created"),
    pytest.param(("initial-restore", "running"), "initial-restore", id="initial-restore-in-progress"),
]


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    """État final d'une protection sans incident."""
    w = World(tmp_path_factory.mktemp("ref"))
    assert rr.protect(w.ctx())["exit"] == 0
    return snapshot_state(w)


@pytest.mark.parametrize("trigger,step", PROTECT_STEPS)
def test_shutdown_during_protect_keeps_everything_then_resume_finishes(tmp_path, reference, trigger, step):
    w = World(tmp_path)
    ctx, asked = at_step(w, trigger)
    res = rr.protect(ctx)
    assert asked["at"] == trigger
    # interrompue, rien de défait, plan toujours en protection, verrou rendu
    assert res["exit"] == rr.EXIT_CANCELLED and res["code"] == "interrupted", res
    assert res["step"] == step and res["suspended"] is True
    assert w.plan()["state"] == "protecting" and db.get_lock(w.conn, w.pid) is None
    assert deletes(w) == []
    assert w.owned(w.src), "the recurring jobs stay"
    open_run = runs(w, "protect")[-1]
    assert open_run["result"] == "interrupted" and not open_run["finished_at"]
    assert open_run["points"]["step"] == step
    assert {e["code"] for e in events(w, step="interrupted")} == {"interrupted"}
    kept = {k for k in w.owned(w.dst)}
    first = len(creates(w))
    engines = {k: dict(o["status"]) for k, o in w.dst.objs.items() if k[0] == rr.K_LH_ENGINE}
    # la console est arrêtée un moment : Longhorn continue sans elle
    w.clock.sleep(4)
    res2 = rr.resume(w.ctx())
    assert res2["exit"] == 0 and w.plan()["state"] == "protected", res2
    assert snapshot_state(w) == reference
    # aucun objet créé deux fois, rien de ce qui existait n'est recréé
    allc = creates(w)
    assert len(allc) == len({c[1:4] for c in allc})
    assert not any(c[1:4] in {(k[0], k[1], k[2]) for k in kept} for c in allc[first:])
    if step == "initial-restore":
        # la restauration commencée continue : même départ, aucun volume de
        # secours recréé pendant la reprise
        assert not [c for c in allc[first:] if c[1] == rr.K_LH_VOLUME]
        for k, st in engines.items():
            if st.get("_from") is not None:
                assert w.dst.objs[k]["status"].get("_from") == st["_from"]
        assert "already restored" in " ".join(str(x) for x in w.log if x[0] == "initial-restore")
    closed = runs(w, "protect")[-1]
    assert closed["id"] == open_run["id"] and closed["result"] == "ok" and closed["finished_at"]
    assert events(w, step="resume", code="resumed")[0]["facts"] and deletes(w) == []


@pytest.mark.parametrize("trigger,step", PROTECT_STEPS)
def test_operator_cancel_during_protect_still_rolls_back(tmp_path, trigger, step):
    w = World(tmp_path)
    ctx, _ = at_step(w, trigger, mode="cancel")
    res = rr.protect(ctx)
    assert res["exit"] == rr.EXIT_CANCELLED and res["code"] == "cancelled", res
    assert w.plan()["state"] == "error" and db.error_origin(w.conn, w.pid) == "protecting"
    w.clock.sleep(200)
    w.dst.list(rr.K_LH_BACKUP)
    assert w.owned(w.src) == [] and w.owned(w.dst) == []
    for name in ("vm-a", "vm-b"):
        for d in VMS[name]["disks"]:
            assert w.src.obj(rr.K_LH_VOLUME, rr.NS, f"pvc-{name}-{d}")["metadata"]["labels"] == {}
    assert runs(w, "protect") == []


def test_cancel_of_the_resumed_protect_undoes_the_whole_protection(tmp_path):
    """La reprise hérite de ce que l'exécution interrompue avait posé : une
    annulation de la reprise défait toute la protection, comme une
    annulation avant l'arrêt de la console."""
    w = World(tmp_path)
    ctx, _ = at_step(w, ("initial-restore", "running"))
    assert rr.protect(ctx)["code"] == "interrupted"
    ctx2, _ = at_step(w, ("carnet", "running"), mode="cancel")
    res = rr.resume(ctx2)
    assert res["code"] == "cancelled", res
    w.clock.sleep(200)
    w.dst.list(rr.K_LH_BACKUP)
    assert w.owned(w.src) == [] and w.owned(w.dst) == []
    assert runs(w, "protect")[-1]["result"] == "cancelled"


def test_interrupted_protect_restores_the_plan_row_on_a_later_cancel(tmp_path):
    w = protected(tmp_path)
    before = w.plan()["retain"]
    ctx, _ = at_step(w, ("recurring-job", "done"))
    assert rr.protect(ctx, retain=before + 3)["code"] == "interrupted"
    assert w.plan()["retain"] == before + 3
    ctx2, _ = at_step(w, ("recurring-job", "running"), mode="cancel")
    assert rr.resume(ctx2)["code"] == "cancelled"
    assert w.plan()["retain"] == before


def test_a_failure_of_the_resume_keeps_the_run_open_for_the_next_one(tmp_path, reference):
    w = World(tmp_path)
    ctx, _ = at_step(w, ("standby-volume", "done"))
    assert rr.protect(ctx)["code"] == "interrupted"
    w.dst.freeze.add("apply")
    assert rr.resume(w.ctx(timeouts={"initial_restore": 60}))["code"] == "timeout"
    assert w.plan()["state"] == "error" and runs(w, "protect")[-1]["result"] == "interrupted"
    w.dst.freeze.clear()
    assert rr.resume(w.ctx())["exit"] == 0
    assert snapshot_state(w) == reference and runs(w, "protect")[-1]["result"] == "ok"


def test_shutdown_before_any_write_does_not_start_the_command(tmp_path):
    w = World(tmp_path)
    ctx = w.ctx()
    ctx.suspend_requested = lambda: REASON
    res = rr.protect(ctx)
    assert res["exit"] == rr.EXIT_CANCELLED and res["code"] == "not-started", res
    assert w.src.mutations() == [] and w.dst.mutations() == []
    assert w.plan()["state"] == "draft" and runs(w) == [] and db.get_lock(w.conn, w.pid) is None
    assert rr.resume(w.ctx())["code"] == "nothing-to-resume"


def test_no_safe_point_during_a_rollback(tmp_path):
    """Une annulation déjà commencée va à son terme : la demande d'arrêt
    arrivée pendant le retour arrière n'est pas vue."""
    w = World(tmp_path)
    ctx, asked = at_step(w, ("initial-restore", "running"), mode="cancel")
    orig = ctx.begin_undo

    def begin():
        asked["reason"] = REASON                    # l'arrêt arrive pendant le retour arrière
        return orig()
    ctx.begin_undo = begin
    res = rr.protect(ctx)
    assert res["code"] == "cancelled"
    w.clock.sleep(200)
    w.dst.list(rr.K_LH_BACKUP)
    assert w.owned(w.src) == [] and w.owned(w.dst) == []


def test_measure_start_and_stop_interrupted_then_resumed(tmp_path):
    w = World(tmp_path)
    ctx, _ = at_step(w, ("recurring-job", "done"))
    res = rr.measure_start(ctx, "5m")
    assert res["code"] == "interrupted" and res["step"] == "recurring-job", res
    assert w.plan()["state"] == "measuring" and deletes(w) == []
    assert runs(w, "measure-start")[-1]["points"]["interval_s"] == 300
    assert rr.resume(w.ctx())["exit"] == 0
    jobs = [k for k in w.owned(w.src) if k[0] == rr.K_LH_JOB]
    assert len(jobs) == 2 and runs(w, "measure-start")[-1]["result"] == "ok"
    ctx, _ = at_step(w, ("remove-job", "done"))
    res = rr.measure_stop(ctx)
    assert res["code"] == "interrupted" and res["step"] == "remove-job", res
    assert w.plan()["state"] == "measuring"
    assert rr.resume(w.ctx())["exit"] == 0 and w.plan()["state"] == "draft"
    assert [k for k in w.owned(w.src) if k[0] == rr.K_LH_JOB] == []
    assert runs(w, "measure-stop")[-1]["result"] == "ok"


def test_unprotect_interrupted_then_resumed(tmp_path):
    w = protected(tmp_path)
    ctx, _ = at_step(w, ("remove-target", "running"))
    res = rr.unprotect(ctx, yes=True)
    # la cible est vidée sans attente (le faux supprime tout de suite) : le
    # point sûr suivant est l'entrée de l'étape de la source
    assert res["code"] == "interrupted" and res["step"] == "remove-job", res
    assert w.plan()["state"] == "unprotecting" and db.get_lock(w.conn, w.pid) is None
    r = runs(w, "unprotect")[-1]
    assert r["result"] == "interrupted" and r["points"]["step"] == "remove-job" and not r["finished_at"]
    assert rr.resume(w.ctx())["exit"] == 0 and w.plan()["state"] == "unprotected"


def test_failover_before_activation_is_undone_at_shutdown(tmp_path):
    """Décision 68, choix motivé dans le code : une bascule est tout ou rien
    avant l'activation ; l'arrêt de la console la défait (la VM source
    repart) au lieu de la laisser source arrêtée et cible absente."""
    w = protected(tmp_path, vms=("vm-a",))
    ctx, _ = at_step(w, ("final-backup", "running"))
    res = rr.failover(ctx, yes=True)
    assert res["exit"] == rr.EXIT_CANCELLED and res["code"] == "cancelled", res
    assert w.plan()["state"] == "protected" and runs(w, "failover")[-1]["result"] == "rolled-back"
    assert w.src.obj(rr.K_VM, "default", "vm-a")["spec"]["runStrategy"] == "Always"


def test_another_command_supersedes_an_interrupted_run(tmp_path):
    w = World(tmp_path)
    ctx, _ = at_step(w, ("recurring-job", "done"))
    assert rr.measure_start(ctx, "5m")["code"] == "interrupted"
    assert rr.protect(w.ctx())["exit"] == 0
    assert runs(w, "measure-start")[-1]["result"] == "superseded"
    assert rr.resume(w.ctx())["code"] == "nothing-to-resume"
