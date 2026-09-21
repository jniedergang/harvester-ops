"""
harvester-ops : pourquoi un volume Longhorn est dégradé, et quoi faire (v1.42.0).

Un volume « dégradé » a moins de répliques saines que demandé. Il reste
lisible, mais une panne de plus peut le rendre inaccessible, et Longhorn ne
dit pas POURQUOI il l'est. Ce module le dit, à partir des objets que la carte
du stockage lit déjà, sans appel de plus.

Fonction pure : ni Flask ni cluster. Elle rend des CODES et des FAITS, jamais
de texte ; le navigateur les traduit. Une correction n'est proposée que si
elle est sûre, et le serveur refait ce diagnostic au moment d'agir.

Constat :
    {"cause": str, "severity": "critical" | "action" | "watch" | "info",
     "facts": {...}, "fix": None | {"kind": str, "params": {...}}}

Causes, dans l'ordre où elles sont rendues (la plus grave d'abord, et
toujours le même ordre pour que l'écran ne saute pas d'un rafraîchissement
à l'autre) : voir ORDER.
"""

import json

REBUILD_LIMIT_SETTING = "concurrent-replica-rebuild-per-node-limit"
# La valeur que remet `harvester-startup.sh` après un arrêt gracieux, qui
# l'avait mise à 0. Si ce rétablissement échoue, les volumes restent
# dégradés sans fin : c'est l'une des causes que l'on reconnaît.
REBUILD_LIMIT_RESTORED = "5"

ORDER = ("faulted", "rebuild-disabled", "rebuilding", "replica-failed",
         "not-enough-nodes", "no-room", "node-unavailable", "unexplained")


def setting(settings, name, default=None, engine="v1"):
    """Valeur d'un réglage Longhorn. Certains sont par moteur de données
    (`{"v1":"3","v2":"3"}` sur Harvester 1.8), d'autres simples (`"5"`)."""
    raw = settings.get(name)
    if raw is None:
        return default
    raw = str(raw)
    if raw.startswith("{"):
        try:
            value = json.loads(raw).get(engine)
        except (ValueError, AttributeError):
            return default
        return default if value is None else str(value)
    return raw


def _conditions(obj):
    return {c.get("type"): c for c in ((obj.get("status") or {}).get("conditions") or [])}


def _is_true(conds, kind):
    return (conds.get(kind) or {}).get("status") == "True"


def node_state(lh_nodes):
    """État de planification de chaque nœud Longhorn et de ses disques.

    Un nœud est planifiable s'il est prêt, planifiable, autorisé, et porte
    au moins un disque prêt, planifiable et autorisé."""
    out = {}
    for n in lh_nodes:
        name = (n.get("metadata") or {}).get("name")
        spec = n.get("spec") or {}
        spec_disks = spec.get("disks") or {}
        disks = {}
        for dname, ds in (((n.get("status") or {}).get("diskStatus")) or {}).items():
            dc = {c.get("type"): c.get("status") for c in (ds.get("conditions") or [])}
            disks[ds.get("diskUUID") or dname] = {
                "name": dname,
                "ready": dc.get("Ready") == "True",
                "schedulable": (dc.get("Schedulable") == "True"
                                and (spec_disks.get(dname) or {}).get("allowScheduling", True)),
            }
        conds = _conditions(n)
        ready = _is_true(conds, "Ready")
        out[name] = {
            "ready": ready,
            "schedulable": (ready and _is_true(conds, "Schedulable")
                            and bool(spec.get("allowScheduling", True))
                            and any(d["ready"] and d["schedulable"] for d in disks.values())),
            "disks": disks,
        }
    return out


def soft_anti_affinity(volume, settings):
    """Deux répliques peuvent-elles partager un nœud ? Le réglage du volume
    prime ; `ignored` renvoie au réglage global."""
    per = (volume.get("spec") or {}).get("replicaSoftAntiAffinity") or "ignored"
    if per == "enabled":
        return True
    if per == "disabled":
        return False
    return setting(settings, "replica-soft-anti-affinity", "false") == "true"


def _name(obj):
    return (obj.get("metadata") or {}).get("name")


def diagnose(volume, replicas, engine, nodes, settings, disks):
    """Constats sur un volume.

    `replicas` : ses répliques ; `engine` : son moteur en marche (ou None,
    volume détaché) ; `nodes` : sortie de node_state() ; `disks` : place
    allouable par disque, telle que la calcule la carte du stockage."""
    spec = volume.get("spec") or {}
    status = volume.get("status") or {}
    robustness = status.get("robustness")
    degraded = robustness == "degraded"
    faulted = robustness == "faulted"
    detached = status.get("state") == "detached"
    try:
        wanted = int(spec.get("numberOfReplicas") or 0)
    except (TypeError, ValueError):
        wanted = 0
    try:
        size = int(spec.get("size") or 0)
    except (TypeError, ValueError):
        size = 0

    eng = (engine or {}).get("status") or {}
    modes = eng.get("replicaModeMap") or {}
    failed = [r for r in replicas if (r.get("spec") or {}).get("failedAt")]
    healthy = [r for r in replicas
               if not (r.get("spec") or {}).get("failedAt") and modes.get(_name(r)) == "RW"]
    rebuilding = sorted(n for n, m in modes.items() if m == "WO")
    sched_nodes = sorted(n for n, s in nodes.items() if s["schedulable"])
    found = {}

    if faulted:
        found["faulted"] = {"severity": "critical",
                            "facts": {"replicas": len(replicas), "failed": len(failed)},
                            "fix": None}

    limit = setting(settings, REBUILD_LIMIT_SETTING)
    if degraded and limit is not None and limit.strip() == "0":
        found["rebuild-disabled"] = {
            "severity": "action",
            "facts": {"setting": REBUILD_LIMIT_SETTING, "value": limit},
            "fix": {"kind": "enable-rebuild", "params": {"value": REBUILD_LIMIT_RESTORED}}}

    if rebuilding:
        addresses = eng.get("currentReplicaAddressMap") or {}
        progress = eng.get("rebuildStatus") or {}
        items = []
        for rn in rebuilding:
            addr = addresses.get(rn, "")
            # Selon les versions, la clé est `ip:port` ou `tcp://ip:port`.
            p = progress.get(addr) or progress.get("tcp://" + addr) or {}
            node = next(((r.get("spec") or {}).get("nodeID") for r in replicas
                         if _name(r) == rn), None)
            items.append({"replica": rn, "node": node, "progress": p.get("progress")})
        found["rebuilding"] = {"severity": "info", "facts": {"replicas": items}, "fix": None}

    if failed and not faulted:
        wait = setting(settings, "replica-replenishment-wait-interval", "600")
        # Supprimer une réplique en échec force Longhorn à en reconstruire une
        # tout de suite. Jamais s'il ne reste pas de copie saine : ce serait
        # jouer la donnée.
        fix = ({"kind": "rebuild-now", "params": {"replica": _name(failed[0])}}
               if healthy else None)
        found["replica-failed"] = {
            "severity": "watch",
            "facts": {"replicas": [{"replica": _name(r),
                                    "node": (r.get("spec") or {}).get("nodeID"),
                                    "failed_at": (r.get("spec") or {}).get("failedAt")}
                                   for r in failed],
                      "wait_seconds": int(wait) if str(wait).isdigit() else None,
                      "healthy": len(healthy)},
            "fix": fix}

    hard = not soft_anti_affinity(volume, settings)
    if hard and wanted > len(sched_nodes) and (degraded or detached):
        target = len(sched_nodes)
        found["not-enough-nodes"] = {
            "severity": "action" if degraded else "watch",
            "facts": {"wanted": wanted, "nodes": len(sched_nodes)},
            # Sans aucun nœud planifiable, réduire ne réglerait rien.
            "fix": ({"kind": "set-replicas", "params": {"replicas": target}}
                    if 1 <= target < wanted else None)}

    missing = wanted - len(healthy)
    if (degraded and missing > 0 and not rebuilding
            and "not-enough-nodes" not in found):
        with_room = {d["node"] for d in disks
                     if d.get("schedulable") and (d.get("room") or 0) >= size
                     and d["node"] in sched_nodes}
        if hard:
            # Une réplique par nœud : un nœud qui en porte déjà une saine
            # n'a pas besoin de place, les autres si.
            hosting = {(r.get("spec") or {}).get("nodeID") for r in healthy}
            short = len(hosting | with_room) < wanted
        else:
            short = not with_room
        if short:
            found["no-room"] = {"severity": "action",
                                "facts": {"size": size, "wanted": wanted,
                                          "nodes_with_room": len(with_room)},
                                "fix": None}

    down = []
    for r in replicas:
        rspec = r.get("spec") or {}
        node = nodes.get(rspec.get("nodeID"))
        if node is None or not node["ready"]:
            down.append({"replica": _name(r), "node": rspec.get("nodeID"), "why": "node"})
        elif rspec.get("diskID") in node["disks"] and not node["disks"][rspec["diskID"]]["ready"]:
            down.append({"replica": _name(r), "node": rspec.get("nodeID"), "why": "disk",
                         "disk": node["disks"][rspec["diskID"]]["name"]})
    if down and (degraded or faulted):
        found["node-unavailable"] = {"severity": "action", "facts": {"replicas": down},
                                     "fix": None}

    if degraded and not found:
        cond = _conditions(volume).get("Scheduled") or {}
        found["unexplained"] = {"severity": "watch",
                                "facts": {"reason": cond.get("reason") or None,
                                          "message": cond.get("message") or None,
                                          "healthy": len(healthy), "wanted": wanted},
                                "fix": None}

    if faulted:
        # Aucune écriture automatique sur un volume sans copie saine.
        for f in found.values():
            f["fix"] = None
    return [{"cause": c, **found[c]} for c in ORDER if c in found]


def health_of(volume, findings):
    robustness = (volume.get("status") or {}).get("robustness")
    if robustness == "faulted":
        return "faulted"
    if robustness == "degraded":
        return "degraded"
    if any(f["cause"] == "not-enough-nodes" and f["severity"] == "watch" for f in findings):
        return "at-risk"
    return "healthy" if robustness == "healthy" else "unknown"


def diagnose_all(volumes, replicas, engines, lh_nodes, settings, disks):
    """{nom du volume: {"health": ..., "findings": [...]}}."""
    nodes = node_state(lh_nodes)
    by_volume = {}
    for r in replicas:
        by_volume.setdefault((r.get("spec") or {}).get("volumeName"), []).append(r)
    # Seul un moteur EN MARCHE dit l'état des répliques ; pendant une
    # migration il y en a deux, et un moteur arrêté garde un état périmé.
    running = {}
    for e in engines:
        if (e.get("status") or {}).get("currentState") == "running":
            running[(e.get("spec") or {}).get("volumeName")] = e
    out = {}
    for v in volumes:
        name = _name(v)
        findings = diagnose(v, by_volume.get(name, []), running.get(name),
                            nodes, settings, disks)
        out[name] = {"health": health_of(v, findings), "findings": findings}
    return out
