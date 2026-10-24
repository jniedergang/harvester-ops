"""Suiveur : VMs d'un plan à sélecteur et VM source revenue en marche après
une bascule (écrans de suivi).

Banc simulé du déroulé (`test_replication_run_199.World`), protégé pour de
vrai, puis le plan passe en sélecteur `dr=gold` : une VM étiquetée hors du
plan donne `selector-new-vm` une fois (gardée pour `pending_vms`), une VM du
plan qui perd l'étiquette donne `selector-vm-left` une fois ; un
redémarrage du suiveur ne les réécrit pas. Le suiveur ne protège jamais."""

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import replication as rp  # noqa: E402
import replication_db as db  # noqa: E402
import replication_follow as rf  # noqa: E402
import replication_run as rr  # noqa: E402
from test_replication_follow_199 import OWNER, follower, protected, set_running  # noqa: E402


def to_selector(w, labelled=("vm-b",)):
    w.conn.execute("UPDATE plans SET selector = 'dr=gold', selector_namespace = 'default', vms = '[]' WHERE id = ?",
                   (w.pid,))
    for name in labelled:
        label(w, name, "gold")


def label(w, name, value):
    vm = w.src.obj(rr.K_VM, "default", name)
    labels = vm["metadata"].setdefault("labels", {})
    if value is None:
        labels.pop("dr", None)
    else:
        labels["dr"] = value


def add_vm(w, name, uid, ns="default", value="gold"):
    vm = copy.deepcopy(w.src.obj(rr.K_VM, "default", "vm-b"))
    vm["metadata"].update(name=name, uid=uid, namespace=ns, labels={"dr": value} if value else {})
    w.src.put(rr.K_VM, vm)


def codes(w, *wanted):
    rows = w.conn.execute("SELECT code, resource_uid, level FROM events WHERE plan_id = ? AND operation = 'follow'"
                          " ORDER BY id", (w.pid,)).fetchall()
    return [(r["code"], r["resource_uid"], r["level"]) for r in rows if r["code"] in wanted]


SEL = ("selector-new-vm", "selector-vm-left")


def test_new_labelled_vm_is_reported_once_and_kept_pending(tmp_path):
    w = protected(tmp_path)
    to_selector(w)
    add_vm(w, "vm-new", "uid-new")
    add_vm(w, "vm-other-ns", "uid-ns", ns="prod")          # autre namespace : hors sélecteur
    add_vm(w, "vm-silver", "uid-silver", value="silver")    # autre valeur
    before = len(w.src.mutations())
    f = follower(w)
    f.once()
    assert codes(w, *SEL) == [("selector-new-vm", "uid-new", "warn")]
    watch = db.get_selector_watch(w.conn, w.pid)
    assert [(x["uid"], x["namespace"], x["name"]) for x in watch["pending"]] == [("uid-new", "default", "vm-new")]
    since = watch["pending"][0]["since"]
    [row] = db.plan_rows(w.conn, [w.plan()], rp.iso(rf._utc(w.clock.now())))
    assert row["pending_vms"] == ["default/vm-new"] and row["alerts"] >= 1
    # relecture des specs suivante : rien de réécrit, date d'apparition gardée
    w.clock.sleep(rp.TIMEOUTS["follow_spec"] + 1)
    f.once()
    assert len(codes(w, *SEL)) == 1 and db.get_selector_watch(w.conn, w.pid)["pending"][0]["since"] == since
    # redémarrage du suiveur : la mémoire est en base
    w.clock.sleep(1)
    follower(w, owner=OWNER).once()
    assert len(codes(w, *SEL)) == 1
    # rien d'écrit sur la source : le suiveur ne protège jamais
    assert len(w.src.mutations()) == before


def test_pending_vm_leaves_the_list_when_protected_or_unlabelled(tmp_path):
    w = protected(tmp_path)
    to_selector(w)
    add_vm(w, "vm-new", "uid-new")
    f = follower(w)
    f.once()
    assert db.get_selector_watch(w.conn, w.pid)["pending"]
    label(w, "vm-new", None)
    w.clock.sleep(rp.TIMEOUTS["follow_spec"] + 1)
    f.once()
    assert db.get_selector_watch(w.conn, w.pid)["pending"] == []
    # étiquette revenue : nouvelle apparition, nouvel événement
    label(w, "vm-new", "gold")
    w.clock.sleep(rp.TIMEOUTS["follow_spec"] + 1)
    f.once()
    assert [c[0] for c in codes(w, *SEL)] == ["selector-new-vm", "selector-new-vm"]
    # protégée (ressource du plan) : plus en attente
    db.upsert_resource(w.conn, w.pid, {"resource_uid": "uid-new", "source_host": "harvlab", "target_host": "harvlab2",
                                       "namespace": "default", "name": "vm-new", "generation": 1})
    w.clock.sleep(rp.TIMEOUTS["follow_spec"] + 1)
    f.once()
    assert db.get_selector_watch(w.conn, w.pid)["pending"] == []


def test_vm_that_lost_the_label_is_reported_once(tmp_path):
    w = protected(tmp_path)
    to_selector(w)
    f = follower(w)
    f.once()
    assert codes(w, *SEL) == []
    label(w, "vm-b", None)
    w.clock.sleep(rp.TIMEOUTS["follow_spec"] + 1)
    f.once()
    uid = w.src.obj(rr.K_VM, "default", "vm-b")["metadata"]["uid"]
    assert codes(w, *SEL) == [("selector-vm-left", uid, "info")]
    assert db.get_selector_watch(w.conn, w.pid)["left_uids"] == [uid]
    w.clock.sleep(rp.TIMEOUTS["follow_spec"] + 1)
    f.once()
    follower(w).once()
    assert len(codes(w, *SEL)) == 1
    # toujours protégée : la ressource reste dans le plan
    assert db.get_resource(w.conn, w.pid, uid)["state"] == "protected"


def test_plan_with_named_vms_is_not_watched(tmp_path):
    w = protected(tmp_path)
    add_vm(w, "vm-new", "uid-new")
    follower(w).once()
    assert codes(w, *SEL) == [] and db.get_selector_watch(w.conn, w.pid)["checked_at"] == ""


def test_source_down_writes_nothing(tmp_path):
    w = protected(tmp_path)
    to_selector(w)
    add_vm(w, "vm-new", "uid-new")
    w.src.down = True
    follower(w).once()
    assert codes(w, *SEL) == [] and db.get_selector_watch(w.conn, w.pid)["pending"] == []


def test_selector_watch_error_never_stops_following(tmp_path, monkeypatch):
    w = protected(tmp_path)
    to_selector(w)

    def boom(*a, **k):
        raise RuntimeError("watch broken")
    monkeypatch.setattr(db, "set_selector_watch", boom)
    add_vm(w, "vm-new", "uid-new")
    rep = follower(w).once()
    assert w.pid in rep["followed"]
    assert [c[0] for c in codes(w, "follow-error")] == ["follow-error"]


# ---------------------------------------------------------------------------
# VM source revenue en marche après une bascule : drapeau de santé
# ---------------------------------------------------------------------------

def test_source_running_after_failover_health_flag(tmp_path):
    w = protected(tmp_path)
    uid = w.src.obj(rr.K_VM, "default", "vm-b")["metadata"]["uid"]
    w.conn.execute("UPDATE plan_resources SET state = 'failed_over' WHERE plan_id = ? AND resource_uid = ?",
                   (w.pid, uid))
    set_running(w, "vm-b", True)
    f = follower(w)
    f.once()
    h = db.health_map(w.conn, w.pid)[uid]
    assert h["source_running_after_failover"] is True
    al = db.alerts(w.conn, rp.iso(rf._utc(w.clock.now())))["alerts"]
    assert [a["code"] for a in al if a["resource_uid"] == uid] == ["source-vm-running-after-failover"]
    assert al[0]["since"] is not None                    # date de l'événement du suiveur
    set_running(w, "vm-b", False)
    w.clock.sleep(rp.TIMEOUTS["follow_health"] + 1)
    f.once()
    assert db.health_map(w.conn, w.pid)[uid]["source_running_after_failover"] is False
    assert db.alerts(w.conn, rp.iso(rf._utc(w.clock.now())))["count"] == 0
