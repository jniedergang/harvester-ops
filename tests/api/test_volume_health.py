"""v1.42.0 : pourquoi un volume Longhorn est dégradé, et quoi faire.

Le diagnostic est une fonction pure : ces tests lui donnent des objets au
format relevé sur harv1 (volume, réplique, moteur, nœud Longhorn) et
vérifient chaque cause, chaque correction proposée, et surtout chaque
garde-fou : une correction proposée à tort est pire que pas de correction.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "web"))
import volume_health as vh  # noqa: E402

GIB = 1024 ** 3


# ---------------------------------------------------------------------------
# Fabriques, au format des objets de harv1
# ---------------------------------------------------------------------------

def volume(name="pvc-1", replicas=3, robustness="degraded", state="attached",
           soft="ignored", size=10 * GIB, scheduled=("True", "", "")):
    return {"metadata": {"name": name},
            "spec": {"numberOfReplicas": replicas, "replicaSoftAntiAffinity": soft,
                     "size": str(size)},
            "status": {"robustness": robustness, "state": state,
                       "conditions": [{"type": "Scheduled", "status": scheduled[0],
                                       "reason": scheduled[1], "message": scheduled[2]}]}}


def replica(name, vol="pvc-1", node="n1", disk="uuid-n1", failed="", running=True):
    return {"metadata": {"name": name},
            "spec": {"volumeName": vol, "nodeID": node, "diskID": disk, "failedAt": failed},
            "status": {"currentState": "running" if running else "stopped"}}


def engine(vol="pvc-1", modes=None, addresses=None, rebuild=None):
    return {"metadata": {"name": vol + "-e-0"},
            "spec": {"volumeName": vol},
            "status": {"currentState": "running", "replicaModeMap": modes or {},
                       "currentReplicaAddressMap": addresses or {},
                       "rebuildStatus": rebuild or {}}}


def lh_node(name="n1", ready=True, schedulable=True, allow=True, disk_ready=True,
            disk_sched=True):
    c = lambda t, ok: {"type": t, "status": "True" if ok else "False"}  # noqa: E731
    return {"metadata": {"name": name},
            "spec": {"allowScheduling": allow, "disks": {"d1": {"allowScheduling": True}}},
            "status": {"conditions": [c("Ready", ready), c("Schedulable", schedulable)],
                       "diskStatus": {"d1": {"diskUUID": f"uuid-{name}",
                                             "conditions": [c("Ready", disk_ready),
                                                            c("Schedulable", disk_sched)]}}}}


def disks(*nodes, room=500 * GIB, schedulable=True):
    return [{"node": n, "disk": "d1", "schedulable": schedulable, "room": room}
            for n in nodes]


SETTINGS = {"concurrent-replica-rebuild-per-node-limit": "5",
            "replica-soft-anti-affinity": "false",
            "replica-replenishment-wait-interval": "600"}


def run(vol, reps, eng=None, nodes=None, settings=None, dks=None):
    nodes = nodes if nodes is not None else [lh_node("n1")]
    return vh.diagnose(vol, reps, eng, vh.node_state(nodes),
                       {**SETTINGS, **(settings or {})},
                       dks if dks is not None else disks(*[n["metadata"]["name"] for n in nodes]))


def causes(findings):
    return [f["cause"] for f in findings]


def fix_of(findings, cause):
    return next(f["fix"] for f in findings if f["cause"] == cause)


# ---------------------------------------------------------------------------
# Un volume sain ne dit rien
# ---------------------------------------------------------------------------

def test_a_healthy_volume_has_no_finding():
    f = run(volume(replicas=1, robustness="healthy"),
            [replica("r1")], engine(modes={"r1": "RW"}))
    assert f == []
    assert vh.health_of(volume(robustness="healthy"), f) == "healthy"


# ---------------------------------------------------------------------------
# Pas assez de nœuds : le cas de harv1 (3 répliques, un nœud)
# ---------------------------------------------------------------------------

def test_three_replicas_on_one_node_is_not_enough_nodes():
    f = run(volume(replicas=3), [replica("r1")], engine(modes={"r1": "RW"}))
    assert causes(f) == ["not-enough-nodes"]
    finding = f[0]
    assert finding["severity"] == "action"
    assert finding["facts"] == {"wanted": 3, "nodes": 1}
    assert finding["fix"] == {"kind": "set-replicas", "params": {"replicas": 1}}


def test_the_target_is_the_number_of_schedulable_nodes():
    nodes = [lh_node("n1"), lh_node("n2"), lh_node("n3", schedulable=False)]
    f = run(volume(replicas=3), [replica("r1"), replica("r2", node="n2", disk="uuid-n2")],
            engine(modes={"r1": "RW", "r2": "RW"}), nodes=nodes)
    assert fix_of(f, "not-enough-nodes")["params"]["replicas"] == 2


@pytest.mark.parametrize("node", [
    lh_node("n1", ready=False), lh_node("n1", schedulable=False),
    lh_node("n1", allow=False), lh_node("n1", disk_ready=False),
    lh_node("n1", disk_sched=False),
])
def test_a_node_is_schedulable_only_if_everything_says_so(node):
    assert vh.node_state([node])["n1"]["schedulable"] is False


def test_no_schedulable_node_means_no_replica_count_to_offer():
    """Ramener à 1 réplique sans aucun nœud pour la porter ne réglerait rien."""
    f = run(volume(replicas=3), [replica("r1")], engine(modes={"r1": "RW"}),
            nodes=[lh_node("n1", schedulable=False)])
    assert fix_of(f, "not-enough-nodes") is None


@pytest.mark.parametrize("soft,global_soft", [("enabled", "false"), ("ignored", "true")])
def test_soft_anti_affinity_lifts_the_node_limit(soft, global_soft):
    """Avec l'anti-affinité souple, deux répliques peuvent partager un nœud :
    le nombre de nœuds n'est plus la limite."""
    f = run(volume(replicas=3, soft=soft), [replica("r1")], engine(modes={"r1": "RW"}),
            settings={"replica-soft-anti-affinity": global_soft})
    assert "not-enough-nodes" not in causes(f)


def test_the_volume_setting_wins_over_the_global_one():
    f = run(volume(replicas=3, soft="disabled"), [replica("r1")], engine(modes={"r1": "RW"}),
            settings={"replica-soft-anti-affinity": "true"})
    assert "not-enough-nodes" in causes(f)


def test_a_detached_volume_that_would_start_degraded_is_at_risk():
    vol = volume(replicas=3, robustness="unknown", state="detached")
    f = run(vol, [replica("r1", running=False)], None)
    assert causes(f) == ["not-enough-nodes"]
    assert f[0]["severity"] == "watch"
    assert f[0]["fix"]["kind"] == "set-replicas"
    assert vh.health_of(vol, f) == "at-risk"


def test_a_detached_volume_within_capacity_is_just_unknown():
    vol = volume(replicas=1, robustness="unknown", state="detached")
    f = run(vol, [replica("r1", running=False)], None)
    assert f == [] and vh.health_of(vol, f) == "unknown"


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------

def test_rebuild_disabled_is_offered_back_only_when_degraded():
    f = run(volume(replicas=1), [replica("r1")], engine(modes={"r1": "RW"}),
            settings={vh.REBUILD_LIMIT_SETTING: "0"})
    assert fix_of(f, "rebuild-disabled") == {"kind": "enable-rebuild",
                                             "params": {"value": "5"}}
    healthy = run(volume(replicas=1, robustness="healthy"), [replica("r1")],
                  engine(modes={"r1": "RW"}), settings={vh.REBUILD_LIMIT_SETTING: "0"})
    assert healthy == []


@pytest.mark.parametrize("key", ["10.54.0.9:10060", "tcp://10.54.0.9:10060"])
def test_a_rebuild_in_progress_shows_its_progress(key):
    nodes = [lh_node("n1"), lh_node("n2")]
    eng = engine(modes={"r1": "RW", "r2": "WO"},
                 addresses={"r1": "10.54.0.8:10054", "r2": "10.54.0.9:10060"},
                 rebuild={key: {"progress": 42, "isRebuilding": True, "state": "in_progress"}})
    f = run(volume(replicas=2), [replica("r1"), replica("r2", node="n2", disk="uuid-n2")],
            eng, nodes=nodes)
    assert causes(f) == ["rebuilding"]
    assert f[0]["severity"] == "info" and f[0]["fix"] is None
    assert f[0]["facts"]["replicas"] == [{"replica": "r2", "node": "n2", "progress": 42}]


# ---------------------------------------------------------------------------
# Réplique en échec
# ---------------------------------------------------------------------------

def test_a_failed_replica_can_be_rebuilt_now_while_a_healthy_one_remains():
    nodes = [lh_node("n1"), lh_node("n2")]
    reps = [replica("r1"), replica("r2", node="n2", disk="uuid-n2",
                                   failed="2026-09-21T10:00:00Z", running=False)]
    f = run(volume(replicas=2), reps, engine(modes={"r1": "RW", "r2": "ERR"}), nodes=nodes)
    assert "replica-failed" in causes(f)
    finding = next(x for x in f if x["cause"] == "replica-failed")
    assert finding["fix"] == {"kind": "rebuild-now", "params": {"replica": "r2"}}
    assert finding["facts"]["wait_seconds"] == 600
    assert finding["facts"]["healthy"] == 1


def test_no_rebuild_now_without_a_healthy_replica():
    """Supprimer la seule copie restante, même en échec, serait jouer la
    donnée : jamais proposé."""
    reps = [replica("r1", failed="2026-09-21T10:00:00Z", running=False)]
    f = run(volume(replicas=1), reps, engine(modes={"r1": "ERR"}))
    assert fix_of(f, "replica-failed") is None


# ---------------------------------------------------------------------------
# Place, nœuds, et le cas grave
# ---------------------------------------------------------------------------

def test_enough_nodes_but_no_disk_with_room():
    nodes = [lh_node("n1"), lh_node("n2")]
    f = run(volume(replicas=2, size=100 * GIB), [replica("r1")], engine(modes={"r1": "RW"}),
            nodes=nodes, dks=disks("n1", "n2", room=10 * GIB))
    assert causes(f) == ["no-room"]
    assert f[0]["fix"] is None
    assert f[0]["facts"]["size"] == 100 * GIB


def test_a_node_that_already_hosts_a_healthy_replica_needs_no_room():
    """n1 est plein mais porte déjà la réplique saine : il ne manque de la
    place que pour la seconde, que n2 peut accueillir."""
    nodes = [lh_node("n1"), lh_node("n2")]
    dks = [{"node": "n1", "disk": "d1", "schedulable": True, "room": 0},
           {"node": "n2", "disk": "d1", "schedulable": True, "room": 500 * GIB}]
    f = run(volume(replicas=2), [replica("r1")], engine(modes={"r1": "RW"}),
            nodes=nodes, dks=dks)
    assert "no-room" not in causes(f)


def test_a_replica_on_a_node_that_is_down():
    nodes = [lh_node("n1"), lh_node("n2", ready=False), lh_node("n3")]
    reps = [replica("r1"), replica("r2", node="n2", disk="uuid-n2", running=False)]
    f = run(volume(replicas=2), reps, engine(modes={"r1": "RW"}), nodes=nodes)
    down = next(x for x in f if x["cause"] == "node-unavailable")
    assert down["facts"]["replicas"] == [{"replica": "r2", "node": "n2", "why": "node"}]


def test_a_replica_on_a_disk_that_is_not_ready():
    nodes = [lh_node("n1"), lh_node("n2", disk_ready=False, disk_sched=False), lh_node("n3")]
    reps = [replica("r1"), replica("r2", node="n2", disk="uuid-n2", running=False)]
    f = run(volume(replicas=2), reps, engine(modes={"r1": "RW"}), nodes=nodes)
    down = next(x for x in f if x["cause"] == "node-unavailable")
    assert down["facts"]["replicas"][0]["why"] == "disk"


def test_faulted_offers_no_fix_at_all():
    """Plus aucune réplique saine : toute écriture automatique risquerait
    d'effacer la dernière chance de récupérer les données."""
    # Le nœud qui portait les données est tombé, un autre est sain.
    nodes = [lh_node("n1", ready=False), lh_node("n2")]
    reps = [replica("r1", failed="2026-09-21T10:00:00Z", running=False)]
    # Longhorn détache un volume faulted : sans garde-fou, la règle « volume
    # détaché à risque » proposerait de ramener ses répliques.
    f = run(volume(replicas=3, robustness="faulted", state="detached"), reps, None,
            nodes=nodes, settings={vh.REBUILD_LIMIT_SETTING: "0"})
    assert causes(f)[0] == "faulted" and f[0]["severity"] == "critical"
    assert "not-enough-nodes" in causes(f)
    assert all(x["fix"] is None for x in f), f
    assert vh.health_of(volume(robustness="faulted"), f) == "faulted"


def test_a_degraded_volume_with_no_known_cause_says_so():
    nodes = [lh_node("n1"), lh_node("n2"), lh_node("n3")]
    vol = volume(replicas=2, scheduled=("False", "ReplicaSchedulingFailure", "something odd"))
    f = run(vol, [replica("r1")], engine(modes={"r1": "RW"}), nodes=nodes)
    assert causes(f) == ["unexplained"]
    assert f[0]["facts"]["reason"] == "ReplicaSchedulingFailure"
    assert f[0]["facts"]["message"] == "something odd"


def test_causes_come_in_a_stable_order():
    """Plusieurs causes à la fois : la plus grave d'abord, toujours dans le
    même ordre, pour que l'écran ne saute pas d'un rafraîchissement à
    l'autre."""
    nodes = [lh_node("n1")]
    reps = [replica("r1"), replica("r2", failed="2026-09-21T10:00:00Z", running=False)]
    f = run(volume(replicas=3), reps, engine(modes={"r1": "RW", "r2": "ERR"}), nodes=nodes,
            settings={vh.REBUILD_LIMIT_SETTING: "0"})
    assert causes(f) == ["rebuild-disabled", "replica-failed", "not-enough-nodes"]


# ---------------------------------------------------------------------------
# Réglages et assemblage
# ---------------------------------------------------------------------------

def test_a_per_engine_setting_is_read_for_v1():
    assert vh.setting({"default-replica-count": '{"v1":"3","v2":"2"}'},
                      "default-replica-count") == "3"
    assert vh.setting({"x": "5"}, "x") == "5"
    assert vh.setting({}, "x", "d") == "d"
    assert vh.setting({"x": "{broken"}, "x", "d") == "d"


def test_diagnose_all_joins_replicas_and_the_running_engine():
    vols = [volume("a", replicas=3), volume("b", replicas=1, robustness="healthy")]
    reps = [replica("ra", vol="a"), replica("rb", vol="b")]
    # Pendant une migration, un moteur arrêté garde un état périmé : ici il
    # prétendrait que la réplique de b est en reconstruction.
    stale = engine("b", modes={"rb": "WO"})
    stale["status"]["currentState"] = "stopped"
    engs = [engine("a", modes={"ra": "RW"}), engine("b", modes={"rb": "RW"}), stale]
    out = vh.diagnose_all(vols, reps, engs, [lh_node("n1")], SETTINGS, disks("n1"))
    assert out["a"]["health"] == "degraded"
    assert causes(out["a"]["findings"]) == ["not-enough-nodes"]
    assert out["b"] == {"health": "healthy", "findings": []}
