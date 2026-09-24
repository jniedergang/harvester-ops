"""harvester-ops : place allouable Longhorn, par disque et par storage class.

Module sans dépendance hors bibliothèque standard, importé par la console
(`web/app.py`) et par le moteur de transfert de VM
(`bin/harvester-vm-transfer.py`), qui tourne aussi sur un hôte airgap sans
la console.
"""


def storage_room(lh_nodes, storage_classes, over, minimal):
    """Place allouable, par disque et par storage class Longhorn.

    Fonction pure, partagée par le panneau de création de VM, la vue
    Stockage et le transfert de VM entre clusters (v1.45.0, qui tourne aussi
    en ligne de commande sans la console) : deux calculs de « place
    restante » finiraient par diverger, et l'exploitant verrait deux
    chiffres pour la même question."""
    disks = []
    for node in lh_nodes:
        node_name = (node.get("metadata") or {}).get("name")
        spec_disks = (node.get("spec") or {}).get("disks") or {}
        for disk_name, ds in (((node.get("status") or {}).get("diskStatus")) or {}).items():
            conditions = {c.get("type"): c.get("status")
                          for c in (ds.get("conditions") or [])}
            spec_disk = spec_disks.get(disk_name) or {}
            schedulable = (conditions.get("Schedulable") == "True"
                           and conditions.get("Ready") == "True"
                           and spec_disk.get("allowScheduling", True))
            maximum = ds.get("storageMaximum") or 0
            scheduled = ds.get("storageScheduled") or 0
            available = ds.get("storageAvailable") or 0
            reserved = spec_disk.get("storageReserved") or 0
            room_over = (maximum - reserved) * over / 100.0 - scheduled
            room_free = available - maximum * minimal / 100.0
            room = max(0, int(min(room_over, room_free)))
            disks.append({
                "node": node_name, "disk": disk_name,
                "path": spec_disk.get("path") or ds.get("diskPath"),
                "tags": spec_disk.get("tags") or [],
                "schedulable": bool(schedulable),
                "maximum": maximum, "scheduled": scheduled,
                "available": available, "reserved": reserved,
                "room": room if schedulable else 0,
                # Dire LAQUELLE des deux contraintes serre : sinon un
                # opérateur qui voit un chiffre bas cherche de la place là
                # où il n'y a rien à gagner.
                "limited_by": "over-provisioning" if room_over < room_free else "free-space",
            })

    # Meilleure place par NODE : deux répliques ne vont pas sur le même.
    by_node = {}
    for d in disks:
        if d["schedulable"]:
            by_node[d["node"]] = max(by_node.get(d["node"], 0), d["room"])
    rooms = sorted(by_node.values(), reverse=True)

    classes = {}
    for sc in storage_classes:
        name = (sc.get("metadata") or {}).get("name")
        if sc.get("provisioner") != "driver.longhorn.io":
            continue
        try:
            replicas = int(((sc.get("parameters") or {}).get("numberOfReplicas")) or 3)
        except (TypeError, ValueError):
            replicas = 3
        if replicas <= 0:
            replicas = 1
        if len(rooms) < replicas:
            allocatable, reason = 0, "not enough schedulable nodes"
        else:
            allocatable, reason = rooms[replicas - 1], None
        classes[name] = {"replicas": replicas, "allocatable": allocatable,
                         "reason": reason}
    return {"disks": disks, "classes": classes, "schedulable_nodes": len(rooms)}
