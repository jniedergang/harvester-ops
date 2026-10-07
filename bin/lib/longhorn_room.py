"""harvester-ops : place allouable Longhorn, par disque et par storage class.

Module sans dépendance hors bibliothèque standard, importé par la console
(`web/app.py`) et par le moteur de transfert de VM
(`bin/harvester-vm-transfer.py`), qui tourne aussi sur un hôte airgap sans
la console.
"""


def storage_room(lh_nodes, storage_classes, over, minimal, new_volume=False):
    """Place allouable, par disque et par storage class Longhorn.

    Fonction pure, partagée par le panneau de création de VM, la vue
    Stockage et le transfert de VM entre clusters (v1.45.0, qui tourne aussi
    en ligne de commande sans la console) : deux calculs de « place
    restante » finiraient par diverger, et l'exploitant verrait deux
    chiffres pour la même question.

    `new_volume` (v1.87.0, vagues de migration) : la place qu'accepte
    l'ordonnanceur de Longhorn pour un volume NEUF. Il ne retranche pas la
    taille du volume de la place libre (rien n'y est encore écrit) : la
    place libre n'est qu'un seuil (le disque doit rester au-dessus de
    `minimal`), et seul le sur-provisionnement limite la taille. Vu en réel le
    07/10/2026 : un disque de 40 Gio placé avec 19,5 Gio de « place libre »
    mais 77,8 Gio de marge de sur-provisionnement, refusé à 28,9 Gio de
    marge. Le calcul par défaut, plus prudent, reste celui de la création et
    du transfert de VM, qui écrivent leurs données aussitôt."""
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
            if new_volume:
                room = max(0, int(room_over)) if room_free > 0 else 0
            else:
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
                "limited_by": ("free-space" if new_volume and room_free <= 0 else "over-provisioning"
                               if new_volume or room_over < room_free else "free-space"),
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
