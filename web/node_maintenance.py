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
