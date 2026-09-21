"""
harvester-ops : isoler un nœud, le mettre en maintenance (v1.43.0).

Reprend la mécanique de Harvester v1.8.0, relevée dans son code plutôt que
supposée (`pkg/api/node/formatter.go`, `pkg/util/drainhelper/helper.go`,
`pkg/controller/master/nodedrain/nodedrain_controller.go`) :

  * ISOLER / RÉINTÉGRER : `spec.unschedulable` du nœud ;
  * ENTRER EN MAINTENANCE : annotation `harvesterhci.io/drain-requested`
    (et `drain-forced` pour forcer). Le contrôleur de Harvester isole le
    nœud, migre les VMs, puis pose `maintain-status` à `running` puis
    `completed`. Il REFAIT la vérification quand l'annotation est posée
    directement, et la retire s'il refuse ;
  * SORTIR : réintégrer, retirer le taint `kubevirt.io/drain` et les trois
    annotations, redémarrer les VMs arrêtées par la maintenance.

Et le garde-fou de son webhook (`pkg/webhook/resources/node/validator.go`,
relevé en réel sur harv1) : ni isoler ni mettre en maintenance le DERNIER
nœud disponible.

Module pur : ni Flask ni cluster, pour que les règles se testent seules.
"""

DRAIN_REQUESTED = "harvesterhci.io/drain-requested"
DRAIN_FORCED = "harvesterhci.io/drain-forced"
MAINTAIN_STATUS = "harvesterhci.io/maintain-status"
DRAIN_TAINT = "kubevirt.io/drain"


def maintenance_state(node):
    """None, "requested", "running" ou "completed"."""
    ann = (node.get("metadata") or {}).get("annotations") or {}
    status = ann.get(MAINTAIN_STATUS)
    if status in ("running", "completed"):
        return status
    if DRAIN_REQUESTED in ann:
        return "requested"
    return None

STRATEGY_LABEL = "harvesterhci.io/maintain-mode-strategy"
STRATEGY_NODE_ANNOTATION = "harvesterhci.io/maintain-mode-strategy-node-name"
RUN_STRATEGY_ANNOTATION = "harvesterhci.io/vmRunStrategy"
SHUTDOWN_STRATEGIES = ("ShutdownAndRestartAfterEnable", "ShutdownAndRestartAfterDisable",
                       "Shutdown")
_CP_LABELS = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/etcd")


def _meta(obj):
    return obj.get("metadata") or {}


def is_control_plane(node):
    labels = _meta(node).get("labels") or {}
    return any(k in labels for k in _CP_LABELS)


def node_ready(node):
    """Une destination possible : prête ET planifiable (isNodeReady de
    Harvester, qui écarte aussi un nœud isolé)."""
    if (node.get("spec") or {}).get("unschedulable"):
        return False
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in ((node.get("status") or {}).get("conditions") or []))


def last_available(node, nodes):
    """Vrai si aucun AUTRE nœud n'est disponible : ni isolé, ni porteur de
    `maintain-status` (règle exacte de validateCordonAndMaintenanceMode ;
    l'état Ready n'y entre pas). Harvester refuse alors d'isoler ce nœud
    comme de le mettre en maintenance."""
    name = _meta(node).get("name")
    return not any(
        _meta(n).get("name") != name
        and not (n.get("spec") or {}).get("unschedulable")
        and MAINTAIN_STATUS not in (_meta(n).get("annotations") or {})
        for n in nodes)


def drain_possible(node, nodes):
    """None si le plan de contrôle permet la maintenance, sinon la raison
    (règles de drainhelper.DrainPossible)."""
    if not is_control_plane(node):
        return None
    cps = [n for n in nodes if is_control_plane(n)]
    if len(cps) == 1:
        return "single-control-plane"
    available = [n for n in cps
                 if MAINTAIN_STATUS not in (_meta(n).get("annotations") or {})]
    if len(available) != 3:
        return "control-plane-busy"
    return None


def _vmi_node(vmi):
    labels = _meta(vmi).get("labels") or {}
    return labels.get("kubevirt.io/nodeName") or (vmi.get("status") or {}).get("nodeName")


def _vm_of(vmi):
    """ns/nom de la VM qui possède ce VMI (findVM de Harvester)."""
    meta = _meta(vmi)
    owner = next((o.get("name") for o in meta.get("ownerReferences") or []
                  if o.get("kind") == "VirtualMachine"), None)
    return f"{meta.get('namespace')}/{owner or meta.get('name')}"


def _expr_matches(labels, expr):
    key, op = expr.get("key"), expr.get("operator")
    values = expr.get("values") or []
    if op == "In":
        return labels.get(key) in values
    if op == "NotIn":
        return labels.get(key) not in values
    if op == "Exists":
        return key in labels
    if op == "DoesNotExist":
        return key not in labels
    if op in ("Gt", "Lt"):
        try:
            a, b = int(labels.get(key)), int(values[0])
        except (TypeError, ValueError, IndexError):
            return False
        return a > b if op == "Gt" else a < b
    return False


def _fits(node, vmi):
    """Le nœud satisfait-il l'affinité requise et le sélecteur du VMI ?"""
    labels = _meta(node).get("labels") or {}
    spec = vmi.get("spec") or {}
    selector = spec.get("nodeSelector") or {}
    if any(labels.get(k) != v for k, v in selector.items()):
        return False
    required = (((spec.get("affinity") or {}).get("nodeAffinity") or {})
                .get("requiredDuringSchedulingIgnoredDuringExecution") or {})
    terms = required.get("nodeSelectorTerms") or []
    if not terms:
        return True
    # Termes : OU ; expressions d'un terme : ET.
    return any(all(_expr_matches(labels, e) for e in (t.get("matchExpressions") or []))
               for t in terms)


def non_migratable(node, nodes, vmis, volumes, replicas):
    """{raison: [ns/vm]} des VMs qui ne migreront pas (FindNonMigratableVMS)."""
    name = _meta(node).get("name")
    out = {}

    def add(reason, vm):
        if vm not in out.setdefault(reason, []):
            out[reason].append(vm)

    # 1. Dernière réplique saine sur ce nœud.
    started = {}
    for r in replicas:
        vol = (r.get("spec") or {}).get("volumeName")
        started.setdefault(vol, []).append(r)
    by_name = {f"{_meta(v).get('namespace')}/{_meta(v).get('name')}": v for v in vmis}
    for v in volumes:
        reps = started.get(_meta(v).get("name"), [])
        healthy = [r for r in reps if (r.get("status") or {}).get("started")]
        if len(healthy) <= 1 and any((r.get("spec") or {}).get("nodeID") == name
                                     for r in healthy):
            ks = (v.get("status") or {}).get("kubernetesStatus") or {}
            for w in ks.get("workloadsStatus") or []:
                if w.get("workloadType") == "VirtualMachineInstance":
                    key = f"{ks.get('namespace')}/{w.get('workloadName')}"
                    add("LastHealthyReplica", _vm_of(by_name[key]) if key in by_name else key)

    here = [v for v in vmis if _vmi_node(v) == name]
    # 2. KubeVirt le dit non migrable.
    for v in here:
        for c in ((v.get("status") or {}).get("conditions") or []):
            if c.get("type") == "LiveMigratable" and c.get("status") == "False":
                add(c.get("reason") or "NotLiveMigratable", _vm_of(v))
    # 3. Aucune autre destination ne lui convient.
    destinations = [n for n in nodes if _meta(n).get("name") != name and node_ready(n)]
    for v in here:
        if not any(_fits(n, v) for n in destinations):
            add("NodeSchedulingRequirementsNotMet", _vm_of(v))
    return {k: sorted(v) for k, v in out.items()}


def plan(node, nodes, vmis, volumes, replicas, force=False):
    """Ce que ferait la mise en maintenance, dit AVANT de la demander."""
    name = _meta(node).get("name")
    refusal = ("already" if maintenance_state(node)
               else drain_possible(node, nodes)
               or ("last-available-node" if last_available(node, nodes) else None))
    here = [v for v in vmis if _vmi_node(v) == name]
    on_node = sorted({_vm_of(v) for v in here})
    blocked_by = non_migratable(node, nodes, vmis, volumes, replicas)
    stuck = {vm for vms in blocked_by.values() for vm in vms}
    labelled = {_vm_of(v) for v in here
                if (_meta(v).get("labels") or {}).get(STRATEGY_LABEL) in SHUTDOWN_STRATEGIES}
    # Forcer arrête toutes les VMs non migrables, y compris celle d'un autre
    # nœud dont la dernière copie saine est ici : Harvester arrête la liste
    # entière.
    will_stop = labelled | (stuck if force else set())
    return {
        "node": name,
        "refusal": refusal,
        "force": bool(force),
        "vms_on_node": on_node,
        "non_migratable": blocked_by,
        "blocked": bool(blocked_by) and not force,
        "will_stop": sorted(will_stop),
        "migrate": sorted(set(on_node) - will_stop - stuck),
    }


def enter_patch(force):
    ann = {DRAIN_REQUESTED: "true"}
    if force:
        ann[DRAIN_FORCED] = "true"
    return {"metadata": {"annotations": ann}}


def leave_patch(node):
    """Sortie de maintenance (disableMaintenanceMode de Harvester) : patch de
    fusion ; la liste des taints se remplace entière, sans celui du drain."""
    taints = [t for t in ((node.get("spec") or {}).get("taints") or [])
              if t.get("key") != DRAIN_TAINT]
    return {"spec": {"unschedulable": False, "taints": taints},
            "metadata": {"annotations": {DRAIN_REQUESTED: None, DRAIN_FORCED: None,
                                         MAINTAIN_STATUS: None}}}


def vms_to_restart(node_name, vms):
    """[(ns, nom, runStrategy)] des VMs que cette maintenance a arrêtées et
    qui doivent repartir à la sortie."""
    out = []
    for vm in vms:
        meta = _meta(vm)
        if ((meta.get("labels") or {}).get(STRATEGY_LABEL) == "ShutdownAndRestartAfterDisable"
                and (meta.get("annotations") or {}).get(STRATEGY_NODE_ANNOTATION) == node_name):
            out.append((meta.get("namespace"), meta.get("name"),
                        (meta.get("annotations") or {}).get(RUN_STRATEGY_ANNOTATION)
                        or "RerunOnFailure"))
    return out
