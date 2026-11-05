"""Constats de disponibilité d'un cluster Harvester (2.4.0).

Phase « disponibilité » d'un plan de déploiement : le cluster est-il prêt à
recevoir du contenu ? Fonction pure sur des objets déjà lus : la console
fait les lectures (en parallèle, comme la mise à jour), le moteur de chaîne
relit toutes les 15 s jusqu'au délai du plan.

Constats (code, détail) :
- `api-unreachable` : la lecture de version a échoué ;
- `read-failed` : une autre lecture a échoué (jamais prise pour « prêt ») ;
- `nodes-missing` : un nœud attendu (lignes de la série) n'est pas lu ;
- `nodes-not-ready` : règle de hv_upgrade.prechecks (condition Ready) ;
- `charts-not-ready` : règle de hv_upgrade.prechecks (ManagedChart Ready) ;
- `longhorn-not-schedulable` : nœud Longhorn dont la condition
  `Schedulable` n'est pas True, ou que l'on a fermé (`allowScheduling:
  false`, que Longhorn ne reporte pas dans la condition), ou pas encore
  enregistré ;
- `no-default-storageclass` : aucune classe annotée
  `storageclass.kubernetes.io/is-default-class: "true"`.

`rke2-traefik` et Turtles relèvent de la phase Cluster API : évalués à
partir de 2.5.0 seulement (décision 155).
"""

import hv_upgrade

DEFAULT_CLASS_ANN = "storageclass.kubernetes.io/is-default-class"
READS = ("version", "nodes", "charts", "lh_nodes", "storageclasses")
CODES = ("api-unreachable", "read-failed", "nodes-missing", "nodes-not-ready", "charts-not-ready",
         "longhorn-not-schedulable", "no-default-storageclass")


def _name(o):
    return ((o or {}).get("metadata") or {}).get("name") or ""


def _precheck(rows, key):
    """Ligne `key` de hv_upgrade.prechecks : la règle est la sienne, pas une copie."""
    return next((r for r in rows if r.get("key") == key), {"ok": True, "text": ""})


def _cond_true(obj, ctype):
    return any(c.get("type") == ctype and str(c.get("status")) == "True"
               for c in ((obj or {}).get("status") or {}).get("conditions") or [])


def evaluate(reads, expected_nodes=None, phases=()):
    """{"ready": bool, "unmet": [{"code", "detail"}]}.

    `reads` : {"version", "nodes", "charts", "lh_nodes", "storageclasses"},
    `None` pour une lecture en échec. `expected_nodes` : noms d'hôtes des
    lignes de la série ; sans eux, les nœuds lus. `phases` : identifiants
    des phases activées (réservé aux constats de 2.5+)."""
    reads = reads or {}
    unmet = []
    if reads.get("version") is None:
        return {"ready": False, "unmet": [{"code": "api-unreachable",
                                           "detail": "the Kubernetes API did not answer"}]}
    failed = [k for k in READS[1:] if reads.get(k) is None]
    if failed:
        unmet.append({"code": "read-failed", "detail": ", ".join(failed)})
    nodes = reads.get("nodes")
    if nodes is not None:
        names = {_name(n) for n in nodes}
        if expected_nodes:
            missing = [h for h in expected_nodes if h not in names]
            if missing:
                unmet.append({"code": "nodes-missing", "detail": ", ".join(missing)})
            wanted = set(expected_nodes)
            nodes = [n for n in nodes if _name(n) in wanted]
        row = _precheck(hv_upgrade.prechecks(nodes=nodes), "nodes-ready")
        if not row["ok"]:
            unmet.append({"code": "nodes-not-ready", "detail": row["text"]})
    charts = reads.get("charts")
    if charts is not None:
        row = _precheck(hv_upgrade.prechecks(charts=charts), "charts")
        if not row["ok"]:
            unmet.append({"code": "charts-not-ready", "detail": row["text"]})
    lh = reads.get("lh_nodes")
    if lh is not None:
        # nœuds dont on attend Longhorn : attendus, sinon ceux de Kubernetes
        want = list(expected_nodes or [_name(n) for n in reads.get("nodes") or []])
        if expected_nodes:
            lh = [n for n in lh if _name(n) in set(expected_nodes)]
        bad = [_name(n) for n in lh
               if not _cond_true(n, "Schedulable") or ((n.get("spec") or {}).get("allowScheduling") is False)]
        seen = {_name(n) for n in lh}
        bad += [f"{h} (not registered)" for h in want if h and h not in seen]
        if bad:
            unmet.append({"code": "longhorn-not-schedulable", "detail": ", ".join(bad)})
    scs = reads.get("storageclasses")
    if scs is not None and not any(
            ((s.get("metadata") or {}).get("annotations") or {}).get(DEFAULT_CLASS_ANN) == "true" for s in scs):
        unmet.append({"code": "no-default-storageclass", "detail": "no storage class is marked as default"})
    return {"ready": not unmet, "unmet": unmet}
