"""v1.43.0 : isoler un nœud, le mettre en maintenance.

Les règles sont celles de Harvester v1.8.0, relevées dans son code
(`drainhelper.DrainPossible`, `nodedrain.FindNonMigratableVMS`) : le
contrôle préalable de la console doit refuser exactement ce que Harvester
refuserait, et le dire AVANT, pas après une maintenance avortée.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "web"))
import node_maintenance as nm  # noqa: E402


def node(name, cp=True, ready=True, unschedulable=False, annotations=None, labels=None,
         taints=None):
    lab = dict(labels or {})
    if cp:
        lab["node-role.kubernetes.io/control-plane"] = "true"
        lab["node-role.kubernetes.io/etcd"] = "true"
    return {"metadata": {"name": name, "labels": lab, "annotations": annotations or {}},
            "spec": {"unschedulable": unschedulable, "taints": taints or []},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def vmi(name, node_name, ns="default", migratable=True, reason="DisksNotLiveMigratable",
        strategy=None, node_selector=None, affinity=None, eviction="LiveMigrateIfPossible"):
    """Par défaut, un VMI comme Harvester le crée : `LiveMigrateIfPossible`."""
    labels = {"kubevirt.io/nodeName": node_name}
    if strategy:
        labels["harvesterhci.io/maintain-mode-strategy"] = strategy
    conds = [] if migratable else [{"type": "LiveMigratable", "status": "False",
                                    "reason": reason}]
    spec = {"evictionStrategy": eviction} if eviction else {}
    if node_selector:
        spec["nodeSelector"] = node_selector
    if affinity:
        spec["affinity"] = affinity
    return {"metadata": {"name": name, "namespace": ns, "labels": labels,
                         "ownerReferences": [{"kind": "VirtualMachine",
                                              "apiVersion": "kubevirt.io/v1", "name": name}]},
            "spec": spec, "status": {"nodeName": node_name, "conditions": conds}}


def lh_volume(name, vmi_name, ns="default", robustness="healthy"):
    return {"metadata": {"name": name},
            "status": {"robustness": robustness,
                       "kubernetesStatus": {"namespace": ns, "workloadsStatus": [
                           {"workloadName": vmi_name, "workloadType": "VirtualMachineInstance"}]}}}


def lh_replica(vol, node_name, started=True):
    return {"metadata": {"name": vol + "-r-" + node_name},
            "spec": {"volumeName": vol, "nodeID": node_name},
            "status": {"started": started}}


HA = [node("n1"), node("n2"), node("n3")]


# ---------------------------------------------------------------------------
# Le plan de contrôle
# ---------------------------------------------------------------------------

def test_a_single_control_plane_cannot_enter_maintenance():
    """harv1 : un seul nœud, qui porte le plan de contrôle. Harvester
    refuse, et c'est ce que l'essai réel vérifiera."""
    assert nm.drain_possible(node("harv1"), [node("harv1")]) == "single-control-plane"


def test_ha_needs_three_control_planes_out_of_maintenance():
    busy = node("n3", annotations={nm.MAINTAIN_STATUS: "completed"})
    assert nm.drain_possible(HA[0], [HA[0], HA[1], busy]) == "control-plane-busy"
    assert nm.drain_possible(HA[0], HA) is None


def test_a_worker_is_never_blocked_by_the_control_plane_rule():
    worker = node("w1", cp=False)
    assert nm.drain_possible(worker, [node("harv1"), worker]) is None


# ---------------------------------------------------------------------------
# Les VMs qui ne migreront pas
# ---------------------------------------------------------------------------

def test_a_vm_kubevirt_says_is_not_migratable():
    out = nm.non_migratable(HA[0], HA, [vmi("gpu", "n1", migratable=False,
                                            reason="HostDeviceNotLiveMigratable")], [], [])
    assert out == {"HostDeviceNotLiveMigratable": ["default/gpu"]}


def test_a_vm_whose_last_healthy_replica_is_here():
    """Migrer la VM laisserait sa dernière copie saine sur un nœud en
    maintenance : Harvester la classe non migrable."""
    vols = [lh_volume("pvc-a", "db")]
    reps = [lh_replica("pvc-a", "n1"), lh_replica("pvc-a", "n2", started=False)]
    out = nm.non_migratable(HA[0], HA, [vmi("db", "n2")], vols, reps)
    assert out == {"LastHealthyReplica": ["default/db"]}


def test_two_healthy_replicas_are_fine():
    vols = [lh_volume("pvc-a", "db")]
    reps = [lh_replica("pvc-a", "n1"), lh_replica("pvc-a", "n2")]
    assert nm.non_migratable(HA[0], HA, [vmi("db", "n1")], vols, reps) == {}


def test_a_vm_pinned_to_this_node_cannot_go_anywhere():
    pinned = vmi("pin", "n1", node_selector={"kubernetes.io/hostname": "n1"})
    nodes = [node(n, labels={"kubernetes.io/hostname": n}) for n in ("n1", "n2", "n3")]
    out = nm.non_migratable(nodes[0], nodes, [pinned], [], [])
    assert out == {"NodeSchedulingRequirementsNotMet": ["default/pin"]}


def test_a_required_affinity_is_honoured():
    aff = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
        "nodeSelectorTerms": [{"matchExpressions": [
            {"key": "zone", "operator": "In", "values": ["a"]}]}]}}}
    nodes = [node("n1", labels={"zone": "a"}), node("n2", labels={"zone": "a"}),
             node("n3", labels={"zone": "b"})]
    assert nm.non_migratable(nodes[0], nodes, [vmi("v", "n1", affinity=aff)], [], []) == {}
    nodes[1] = node("n2", labels={"zone": "b"})
    out = nm.non_migratable(nodes[0], nodes, [vmi("v", "n1", affinity=aff)], [], [])
    assert out == {"NodeSchedulingRequirementsNotMet": ["default/v"]}


def test_a_cordoned_or_down_node_is_no_destination():
    nodes = [node("n1"), node("n2", unschedulable=True), node("n3", ready=False)]
    out = nm.non_migratable(nodes[0], nodes, [vmi("v", "n1")], [], [])
    assert out == {"NodeSchedulingRequirementsNotMet": ["default/v"]}


# ---------------------------------------------------------------------------
# Le plan complet
# ---------------------------------------------------------------------------

def test_the_plan_says_what_migrates_and_what_stops():
    vmis = [vmi("web", "n1"), vmi("batch", "n1", strategy="Shutdown"),
            vmi("elsewhere", "n2")]
    plan = nm.plan(HA[0], HA, vmis, [], [], force=False)
    assert plan["refusal"] is None and plan["blocked"] is False
    assert plan["vms_on_node"] == ["default/batch", "default/web"]
    assert plan["migrate"] == ["default/web"]
    assert plan["will_stop"] == ["default/batch"]


def test_a_vm_without_a_live_migration_eviction_is_stopped_by_the_drain():
    """Constaté sur harvlab : une VM sans `evictionStrategy` (créée par
    kubectl, Terraform ou la console avant 1.44.3) est ARRÊTÉE par le drain
    au lieu de migrer ; KubeVirt ne migre à l'éviction que si on le lui
    demande. Le plan le dit, au lieu de l'annoncer comme migrée."""
    vmis = [vmi("web", "n1"), vmi("legacy", "n1", eviction=None),
            vmi("ext", "n1", eviction="External")]
    plan = nm.plan(HA[0], HA, vmis, [], [], force=False)
    assert plan["migrate"] == ["default/web"]
    assert plan["drain_stops"] == [{"vm": "default/ext", "restarts": False},
                                   {"vm": "default/legacy", "restarts": False}]
    assert plan["blocked"] is False


def test_an_always_vm_comes_back_on_another_node():
    """`Always` : KubeVirt la relance ailleurs (arrêt, pas migration) ;
    `RerunOnFailure` : un arrêt propre n'est pas une panne, elle reste arrêtée."""
    vmis = [vmi("always", "n1", eviction=None), vmi("rerun", "n1", eviction=None)]
    vms = [{"metadata": {"name": "always", "namespace": "default"},
            "spec": {"runStrategy": "Always"}},
           {"metadata": {"name": "rerun", "namespace": "default"},
            "spec": {"runStrategy": "RerunOnFailure"}}]
    plan = nm.plan(HA[0], HA, vmis, [], [], force=False, vms=vms)
    assert plan["drain_stops"] == [{"vm": "default/always", "restarts": True},
                                   {"vm": "default/rerun", "restarts": False}]


def test_the_cluster_default_eviction_counts():
    vmis = [vmi("legacy", "n1", eviction=None)]
    plan = nm.plan(HA[0], HA, vmis, [], [], force=False, default_eviction="LiveMigrate")
    assert plan["migrate"] == ["default/legacy"] and plan["drain_stops"] == []


def test_forcing_stops_only_the_non_migratable():
    """Harvester n'honore l'étiquette « arrêter pendant la maintenance »
    qu'SANS forçage ; forcer arrête les seules VMs non migrables."""
    vmis = [vmi("gpu", "n1", migratable=False), vmi("batch", "n1", strategy="Shutdown")]
    forced = nm.plan(HA[0], HA, vmis, [], [], force=True)
    assert forced["will_stop"] == ["default/gpu"]
    assert forced["migrate"] == ["default/batch"]
    normal = nm.plan(HA[0], [HA[0], HA[1], HA[2]], [vmis[1]], [], [], force=False)
    assert normal["will_stop"] == ["default/batch"]


def test_a_vm_whose_volume_is_not_healthy_is_flagged():
    """Constaté sur harvlab : Longhorn annule la migration d'un volume dont
    une réplique attend sa reconstruction (« Need to revert rather than
    starting migration »). KubeVirt réessaie, la maintenance piétine un
    quart d'heure. La VM migrera, mais le plan le dit AVANT."""
    vmis = [vmi("web", "n1"), vmi("db", "n1"), vmi("far", "n2")]
    vols = [lh_volume("pvc-web", "web", robustness="degraded"),
            lh_volume("pvc-db", "db"), lh_volume("pvc-far", "far", robustness="degraded")]
    reps = [lh_replica("pvc-web", n) for n in ("n1", "n2", "n3")]
    plan = nm.plan(HA[0], HA, vmis, vols, reps, force=False)
    assert plan["volume_waits"] == [{"vm": "default/web", "volume": "pvc-web"}]
    assert "default/web" in plan["migrate"] and plan["blocked"] is False


def test_a_pod_volume_whose_last_replica_is_here_holds_the_drain():
    """Relevé sur harvlab : un volume attaché à un POD, dont la seule réplique
    saine est sur le nœud, bloque le drain pour de bon (Longhorn refuse
    d'évincer son instance-manager). Harvester ne regarde que les volumes
    des VMs ; le plan le dit, pour tout consommateur."""
    pod_vol = {"metadata": {"name": "pvc-db"},
               "status": {"state": "attached", "robustness": "healthy",
                          "kubernetesStatus": {"namespace": "default", "pvcName": "db-data",
                                               "workloadsStatus": [{"podName": "db-0",
                                                                    "workloadType": "Pod"}]}}}
    reps = [lh_replica("pvc-db", "n1")]
    plan = nm.plan(HA[0], HA, [], [pod_vol], reps, force=False)
    assert plan["stuck_volumes"] == [{"volume": "pvc-db", "claim": "default/db-data",
                                      "pods": ["db-0"]}]
    # une seconde réplique saine ailleurs : plus de blocage
    reps.append(lh_replica("pvc-db", "n2"))
    assert nm.plan(HA[0], HA, [], [pod_vol], reps, force=False)["stuck_volumes"] == []
    # détaché : rien ne le retient
    pod_vol["status"]["state"] = "detached"
    assert nm.plan(HA[0], HA, [], [pod_vol], reps[:1], force=False)["stuck_volumes"] == []


def test_a_vm_volume_stays_under_its_vm():
    """Pour une VM, c'est déjà « LastHealthyReplica » (non migrable)."""
    vol = dict(lh_volume("pvc-web", "web"))
    vol["status"] = dict(vol["status"], state="attached")
    plan = nm.plan(HA[0], HA, [vmi("web", "n1")], [vol], [lh_replica("pvc-web", "n1")])
    assert plan["stuck_volumes"] == []
    assert plan["non_migratable"] == {"LastHealthyReplica": ["default/web"]}


def test_non_migratable_vms_block_unless_forced():
    vmis = [vmi("gpu", "n1", migratable=False)]
    plan = nm.plan(HA[0], HA, vmis, [], [], force=False)
    assert plan["blocked"] is True and plan["migrate"] == []
    forced = nm.plan(HA[0], HA, vmis, [], [], force=True)
    assert forced["blocked"] is False
    assert forced["will_stop"] == ["default/gpu"]


def test_a_node_already_in_maintenance_is_refused():
    busy = node("n1", annotations={nm.DRAIN_REQUESTED: "true"})
    assert nm.plan(busy, [busy, HA[1], HA[2]], [], [], [], False)["refusal"] == "already"


def test_the_single_control_plane_refusal_comes_first():
    plan = nm.plan(node("harv1"), [node("harv1")], [vmi("web", "harv1")], [], [], False)
    assert plan["refusal"] == "single-control-plane"


# ---------------------------------------------------------------------------
# Le dernier nœud disponible (webhook de Harvester, relevé en réel sur harv1 :
# « can't enable maintenance mode or cordon on the last available node »)
# ---------------------------------------------------------------------------

def test_the_only_node_is_the_last_available():
    assert nm.last_available(node("harv1"), [node("harv1")]) is True


def test_another_schedulable_node_makes_it_possible():
    assert nm.last_available(HA[0], HA) is False


def test_cordoned_or_maintained_nodes_do_not_count():
    """Règle exacte du webhook : un autre nœud compte s'il n'est ni isolé ni
    porteur de `maintain-status`. Sa préparation (Ready) n'entre pas en
    jeu, et une simple demande de maintenance non plus."""
    others = [node("n2", unschedulable=True),
              node("n3", annotations={nm.MAINTAIN_STATUS: "completed"})]
    assert nm.last_available(HA[0], [HA[0]] + others) is True
    requested = node("n3", annotations={nm.DRAIN_REQUESTED: "true"})
    assert nm.last_available(HA[0], [HA[0], others[0], requested]) is False
    not_ready = node("n3", ready=False)
    assert nm.last_available(HA[0], [HA[0], others[0], not_ready]) is False


def test_maintenance_of_the_last_available_worker_is_refused():
    """Un nœud de travail échappe à la règle du plan de contrôle, pas à
    celle du dernier nœud disponible."""
    nodes = [node("w1", cp=False), node("w2", cp=False, unschedulable=True)]
    assert nm.plan(nodes[0], nodes, [], [], [], False)["refusal"] == "last-available-node"
    nodes[1] = node("w2", cp=False)
    assert nm.plan(nodes[0], nodes, [], [], [], False)["refusal"] is None


# ---------------------------------------------------------------------------
# Ce qui est écrit sur le nœud
# ---------------------------------------------------------------------------

def test_entering_only_annotates_as_harvester_does():
    assert nm.enter_patch(False) == {"metadata": {"annotations": {nm.DRAIN_REQUESTED: "true"}}}
    assert nm.enter_patch(True) == {"metadata": {"annotations": {
        nm.DRAIN_REQUESTED: "true", nm.DRAIN_FORCED: "true"}}}


def test_leaving_uncordons_drops_the_drain_taint_and_the_annotations():
    n = node("n1", unschedulable=True, annotations={nm.MAINTAIN_STATUS: "completed"},
             taints=[{"key": "kubevirt.io/drain", "effect": "NoSchedule"},
                     {"key": "other", "effect": "NoSchedule"}])
    patch = nm.leave_patch(n)
    assert patch["spec"] == {"unschedulable": False,
                             "taints": [{"key": "other", "effect": "NoSchedule"}]}
    assert patch["metadata"]["annotations"] == {nm.DRAIN_REQUESTED: None,
                                                nm.DRAIN_FORCED: None,
                                                nm.MAINTAIN_STATUS: None}


def test_leaving_restarts_only_the_vms_this_node_shut_down():
    def vm(name, strategy, node_name, run="RerunOnFailure"):
        return {"metadata": {"name": name, "namespace": "default",
                             "labels": {"harvesterhci.io/maintain-mode-strategy": strategy},
                             "annotations": {"harvesterhci.io/maintain-mode-strategy-node-name": node_name,
                                             "harvesterhci.io/vmRunStrategy": run}}}
    vms = [vm("a", "ShutdownAndRestartAfterDisable", "n1", run="Always"),
           vm("b", "ShutdownAndRestartAfterDisable", "n2"),
           vm("c", "Shutdown", "n1")]
    assert nm.vms_to_restart("n1", vms) == [("default", "a", "Always")]


# ---------------------------------------------------------------------------
# Les points d'accès
# ---------------------------------------------------------------------------
import app as wapp  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/kc")
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    wapp.app.config["TESTING"] = True
    return wapp.app.test_client()


def cluster_state(monkeypatch, nodes, vmis=(), vms=(), volumes=(), replicas=()):
    items = ([dict(n, kind="Node") for n in nodes] + [dict(v, kind="VirtualMachineInstance") for v in vmis]
             + [dict(v, kind="VirtualMachine") for v in vms] + [dict(v, kind="Volume") for v in volumes]
             + [dict(r, kind="Replica") for r in replicas])
    wapp._node_maint_missing.clear()
    monkeypatch.setattr(wapp, "_kubectl_json", lambda kc, *a, **k: {"items": items})


def tracked(monkeypatch):
    runs = []
    monkeypatch.setattr(wapp, "track_action",
                        lambda label, cluster, worker, *args: runs.append((label, worker, args)) or "act1")
    return runs


def test_roles_cordon_is_operator_maintenance_is_admin():
    """Isoler se défait d'un clic ; une maintenance déplace ou arrête des
    VMs, comme l'arrêt d'un cluster."""
    assert wapp.required_role_for("/api/node/c/n1/cordon", "POST") == "operator"
    assert wapp.required_role_for("/api/node/c/n1/uncordon", "POST") == "operator"
    assert wapp.required_role_for("/api/node/c/n1/maintenance", "POST") == "admin"
    assert wapp.required_role_for("/api/node/c/n1/maintenance", "DELETE") == "admin"
    assert wapp.required_role_for("/api/node/c/n1/maintenance-check", "GET") == "viewer"


def test_cordon_is_a_tracked_patch(client, monkeypatch):
    cluster_state(monkeypatch, HA)
    runs = tracked(monkeypatch)
    r = client.post("/api/node/c1/n1/cordon")
    assert r.status_code == 201
    label, _worker, args = runs[0]
    assert label == "node-cordon:n1"
    assert {"spec": {"unschedulable": True}} in args


def test_cordon_refused_when_already_cordoned_or_in_maintenance(client, monkeypatch):
    cluster_state(monkeypatch, [node("n1", unschedulable=True), HA[1], HA[2]])
    tracked(monkeypatch)
    assert client.post("/api/node/c1/n1/cordon").status_code == 409
    cluster_state(monkeypatch, [node("n1", annotations={nm.MAINTAIN_STATUS: "running"}),
                                HA[1], HA[2]])
    r = client.post("/api/node/c1/n1/uncordon")
    assert r.status_code == 409 and "maintenance" in r.get_json()["detail"]


def test_cordon_refused_on_the_last_available_node(client, monkeypatch):
    """Harvester le refuserait par son webhook ; la console le dit avant,
    sans lancer d'action vouée à l'échec."""
    cluster_state(monkeypatch, [node("harv1")])
    runs = tracked(monkeypatch)
    r = client.post("/api/node/c1/harv1/cordon")
    assert r.status_code == 409 and r.get_json()["error"] == "last-available-node"
    assert runs == []


def test_uncordon_refused_when_not_cordoned(client, monkeypatch):
    cluster_state(monkeypatch, HA)
    tracked(monkeypatch)
    assert client.post("/api/node/c1/n1/uncordon").status_code == 409


def test_the_check_returns_the_plan(client, monkeypatch):
    cluster_state(monkeypatch, HA, vmis=[vmi("web", "n1")])
    d = client.get("/api/node/c1/n1/maintenance-check").get_json()
    assert d["refusal"] is None and d["migrate"] == ["default/web"]


def test_maintenance_refused_on_a_single_control_plane(client, monkeypatch):
    """Le cas de harv1 : refusé ici, avant d'écrire quoi que ce soit."""
    # Aucune VM : seul le plan de contrôle peut refuser, pas un blocage.
    cluster_state(monkeypatch, [node("harv1")])
    runs = tracked(monkeypatch)
    r = client.post("/api/node/c1/harv1/maintenance", json={"force": True})
    assert r.status_code == 409
    assert (r.get_json()["error"], r.get_json()["refusal"]) == ("refused", "single-control-plane")
    assert not runs


def test_the_check_reads_the_cluster_default_eviction(client, monkeypatch):
    """Le défaut vient de la ressource KubeVirt, dans le même appel groupé."""
    kv = {"kind": "KubeVirt", "metadata": {"name": "kubevirt"},
          "spec": {"configuration": {"evictionStrategy": "LiveMigrate"}}}
    assert "kubevirts.kubevirt.io" in wapp.NODE_MAINT_KINDS
    items = [dict(n, kind="Node") for n in HA] + [dict(vmi("legacy", "n1", eviction=None),
                                                     kind="VirtualMachineInstance"), kv]
    wapp._node_maint_missing.clear()
    monkeypatch.setattr(wapp, "_kubectl_json", lambda kc, *a, **k: {"items": items})
    plan = client.get("/api/node/c1/n1/maintenance-check").get_json()
    assert plan["migrate"] == ["default/legacy"] and plan["drain_stops"] == []


def test_non_migratable_vms_need_force(client, monkeypatch):
    cluster_state(monkeypatch, HA, vmis=[vmi("gpu", "n1", migratable=False)])
    runs = tracked(monkeypatch)
    r = client.post("/api/node/c1/n1/maintenance", json={"force": False})
    assert r.status_code == 409 and r.get_json()["blocked"] is True
    assert not runs
    r = client.post("/api/node/c1/n1/maintenance", json={"force": True})
    assert r.status_code == 201
    label, _w, args = runs[0]
    assert label == "node-maintenance-enter:n1"
    assert args[-1] is True


def test_force_must_be_a_real_boolean(client, monkeypatch):
    """« force: "false" » est une chaîne non vide : lue comme vraie, elle
    arrêterait des VMs par erreur."""
    cluster_state(monkeypatch, HA, vmis=[vmi("gpu", "n1", migratable=False)])
    runs = tracked(monkeypatch)
    r = client.post("/api/node/c1/n1/maintenance", json={"force": "false"})
    assert r.status_code == 409 and not runs


def test_leaving_needs_a_node_in_maintenance(client, monkeypatch):
    cluster_state(monkeypatch, HA)
    tracked(monkeypatch)
    assert client.delete("/api/node/c1/n1/maintenance").status_code == 409


def test_leaving_patches_and_lists_the_vms_to_restart(client, monkeypatch):
    busy = node("n1", unschedulable=True, annotations={nm.MAINTAIN_STATUS: "completed"})
    vm = {"metadata": {"name": "a", "namespace": "default",
                       "labels": {nm.STRATEGY_LABEL: "ShutdownAndRestartAfterDisable"},
                       "annotations": {nm.STRATEGY_NODE_ANNOTATION: "n1"}}}
    cluster_state(monkeypatch, [busy, HA[1], HA[2]], vms=[vm])
    runs = tracked(monkeypatch)
    r = client.delete("/api/node/c1/n1/maintenance")
    assert r.status_code == 201
    label, _w, args = runs[0]
    assert label == "node-maintenance-leave:n1"
    assert args[-1] == [("default", "a", "RerunOnFailure")]
    assert args[-2]["spec"]["unschedulable"] is False


def test_an_unknown_node_is_404(client, monkeypatch):
    cluster_state(monkeypatch, HA)
    tracked(monkeypatch)
    assert client.post("/api/node/c1/n9/cordon").status_code == 404


# ---------------------------------------------------------------------------
# Le suivi de l'entrée en maintenance
# ---------------------------------------------------------------------------

class FakeRun:
    def __init__(self):
        self.events, self.status, self.exit_code = [], None, None
        self.error_summary, self.ended_at, self.closed = None, None, False

    def emit(self, ev):
        self.events.append(ev)

    def close(self):
        self.closed = True


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def follow(monkeypatch, states, vmis_left=0, cordoned=False):
    """Chaque relecture du nœud rend l'état suivant de la liste.

    `cordoned` dit si Harvester tient encore le nœud isolé : c'est ce qui
    sépare « il vide » de « il a renoncé »."""
    it = iter(states)
    last = {"v": None}

    def read(kc, name):
        try:
            last["v"] = next(it)
        except StopIteration:
            pass
        return last["v"], vmis_left, cordoned
    monkeypatch.setattr(wapp, "_node_maint_read", read)
    monkeypatch.setattr(wapp, "NODE_MAINT_POLL", 0)
    # Une régression doit échouer vite, pas au bout des dix minutes réelles.
    monkeypatch.setattr(wapp, "NODE_MAINT_TIMEOUT", 0.5)
    # Le trou entre le retrait de la demande et la pose du statut est toléré
    # 30 s en vrai ; les tests n'attendent pas si longtemps.
    monkeypatch.setattr(wapp, "NODE_MAINT_WITHDRAW_GRACE", 0.05)
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Proc(0))


def test_entering_is_followed_until_completed(monkeypatch):
    follow(monkeypatch, [{nm.DRAIN_REQUESTED: "true"}, {nm.MAINTAIN_STATUS: "running"},
                         {nm.MAINTAIN_STATUS: "completed"}])
    run = FakeRun()
    wapp._maintenance_enter_runner(run, "/kc", "n1", False)
    assert run.status == "done" and run.closed


def test_a_refusal_by_harvester_is_reported(monkeypatch):
    """Le contrôleur retire l'annotation sans poser de statut quand il
    refuse : la console doit le dire, pas attendre dix minutes."""
    follow(monkeypatch, [{nm.DRAIN_REQUESTED: "true"}, {}, {}, {}, {}])
    run = FakeRun()
    wapp._maintenance_enter_runner(run, "/kc", "n1", False)
    assert run.status == "error" and "refused" in run.error_summary


def _pdb(name, ns, labels, allowed):
    return {"metadata": {"name": name, "namespace": ns},
            "spec": {"selector": {"matchLabels": labels}},
            "status": {"disruptionsAllowed": allowed}}


def _pod(name, ns, labels):
    return {"metadata": {"name": name, "namespace": ns, "labels": labels}}


def test_a_pod_held_by_a_full_disruption_budget_is_named():
    """v1.44.9 : quand Harvester renonce à une maintenance il n'en donne pas
    la raison. Sur un cluster de trois nœuds elle est toujours la même, le
    gestionnaire d'instances Longhorn ne peut pas être évincé ; la console
    va donc la chercher pour la dire."""
    pods = [_pod("instance-manager-abc", "longhorn-system", {"longhorn.io/component": "instance-manager"}),
            _pod("virt-launcher-web", "default", {"kubevirt.io": "virt-launcher"})]
    pdbs = [_pdb("instance-manager-abc", "longhorn-system",
                 {"longhorn.io/component": "instance-manager"}, 0)]
    assert nm.eviction_blockers(pods, pdbs) == [
        {"pod": "instance-manager-abc", "namespace": "longhorn-system",
         "pdb": "instance-manager-abc"}]


def test_a_budget_that_still_allows_a_disruption_is_not_a_blocker():
    pods = [_pod("p", "ns", {"app": "x"})]
    assert nm.eviction_blockers(pods, [_pdb("b", "ns", {"app": "x"}, 1)]) == []


def test_a_budget_of_another_namespace_does_not_count():
    pods = [_pod("p", "ns", {"app": "x"})]
    assert nm.eviction_blockers(pods, [_pdb("b", "autre", {"app": "x"}, 0)]) == []


def test_a_selector_we_do_not_read_is_ignored_not_fatal():
    """Un sélecteur à expressions ne doit pas casser le diagnostic."""
    pdb = {"metadata": {"name": "b", "namespace": "ns"},
           "spec": {"selector": {"matchExpressions": [{"key": "app", "operator": "Exists"}]}},
           "status": {"disruptionsAllowed": 0}}
    assert nm.eviction_blockers([_pod("p", "ns", {"app": "x"})], [pdb]) == []


def test_a_volume_is_named_once_even_with_several_workload_entries():
    """v1.44.9 : Longhorn garde une entrée de charge par pod ayant monté le
    volume ; après trois migrations, la même VM y figurait trois fois et le
    panneau listait trois fois le même disque."""
    node = {"metadata": {"name": "n1"}}
    vmi = {"metadata": {"name": "web", "namespace": "default"},
           "status": {"nodeName": "n1"}}
    vol = {"metadata": {"name": "pvc-1"},
           "status": {"robustness": "degraded",
                      "kubernetesStatus": {"namespace": "default", "workloadsStatus": [
                          {"workloadType": "VirtualMachineInstance", "workloadName": "web"},
                          {"workloadType": "VirtualMachineInstance", "workloadName": "web"},
                          {"workloadType": "VirtualMachineInstance", "workloadName": "web"}]}}}
    assert nm.volume_waits(node, [vmi], [vol]) == [{"vm": "default/web", "volume": "pvc-1"}]


def test_a_drain_still_running_is_not_a_refusal(monkeypatch):
    """v1.44.9, mesuré sur harvlab : la marque `drain-requested` disparaît
    AVANT la fin de la vidange, qui peut buter plusieurs minutes sur le
    budget de perturbation des gestionnaires Longhorn. Tant que Harvester
    garde le nœud isolé, il travaille ; il ne le rend au cluster que s'il
    renonce."""
    follow(monkeypatch, [{nm.DRAIN_REQUESTED: "true"}, {}, {}, {}, {},
                         {nm.MAINTAIN_STATUS: "completed"}], cordoned=True)
    monkeypatch.setattr(wapp, "NODE_MAINT_WITHDRAW_GRACE", 0.01)
    monkeypatch.setattr(wapp, "NODE_MAINT_TIMEOUT", 5.0)
    run = FakeRun()
    wapp._maintenance_enter_runner(run, "/kc", "n1", False)
    assert run.status == "done", run.error_summary
    assert any("drain in progress" in (e.get("message") or "") for e in run.events)


def test_the_gap_before_the_status_is_not_a_refusal(monkeypatch):
    """v1.44.9, mesuré sur harvlab : Harvester RETIRE `drain-requested` puis
    pose `maintain-status` dans une seconde écriture. Un relevé tombé entre
    les deux faisait échouer une maintenance qui se déroulait bien (vu dans
    le dock : « Harvester refused the maintenance » une seconde avant que le
    nœud passe en maintenance)."""
    follow(monkeypatch, [{nm.DRAIN_REQUESTED: "true"}, {},
                         {nm.MAINTAIN_STATUS: "running"},
                         {nm.MAINTAIN_STATUS: "completed"}])
    monkeypatch.setattr(wapp, "NODE_MAINT_WITHDRAW_GRACE", 30.0)
    monkeypatch.setattr(wapp, "NODE_MAINT_TIMEOUT", 5.0)
    run = FakeRun()
    wapp._maintenance_enter_runner(run, "/kc", "n1", False)
    assert run.status == "done", run.error_summary


def test_a_request_that_comes_back_resets_the_grace(monkeypatch):
    """Harvester réessaie : la demande peut réapparaître après avoir disparu.
    Le compte à rebours du refus doit repartir de zéro, sinon une reprise
    normale finirait en erreur."""
    follow(monkeypatch, [{nm.DRAIN_REQUESTED: "true"}, {},
                         {nm.DRAIN_REQUESTED: "true"}, {},
                         {nm.MAINTAIN_STATUS: "completed"}])
    monkeypatch.setattr(wapp, "NODE_MAINT_WITHDRAW_GRACE", 0.4)
    monkeypatch.setattr(wapp, "NODE_MAINT_TIMEOUT", 5.0)
    monkeypatch.setattr(wapp, "NODE_MAINT_POLL", 0.15)
    run = FakeRun()
    wapp._maintenance_enter_runner(run, "/kc", "n1", False)
    assert run.status == "done", run.error_summary


def test_leaving_restarts_the_vms_it_shut_down(monkeypatch):
    calls = []
    monkeypatch.setattr(wapp.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or Proc(0))
    run = FakeRun()
    wapp._maintenance_leave_runner(run, "/kc", "n1", nm.leave_patch(node("n1")),
                                   [("default", "a", "Always")])
    assert run.status == "done"
    node_patch = next(c for c in calls if "node" in c)
    vm_patch = next(c for c in calls if "vm" in c or "virtualmachines.kubevirt.io" in c)
    assert "n1" in node_patch
    assert '"runStrategy": "Always"' in vm_patch[-1]
    assert nm.STRATEGY_NODE_ANNOTATION in vm_patch[-1]
