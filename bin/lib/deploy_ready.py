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

2.5.0 (décision 155), si la phase `capi` ou `clusters` est activée :
- `harvester-too-old` : réglage `server-version` en dessous de v1.9 (pas de
  Turtles embarqué, décision 117) ;
- `rke2-traefik-no-address` : ni `kube-system/rke2-traefik` (installation
  neuve de 1.9) ni `kube-system/ingress-expose` (cluster mis à jour depuis
  1.8) ne donne l'adresse de Harvester (capi_stack.harvester_vip, d'où la
  console tire l'adresse de l'identité restreinte) ;
- `turtles-not-ready` : aucun `CAPIProvider` lisible, ou cœur Cluster API
  pas prêt. Règle de capi_stack.stack_status, appelée sur les objets lus
  (jamais recopiée). Une pile de fournisseurs absente n'est PAS un constat
  ici : la phase capi la déclare, `capi-stack-missing` le dit au lancement.
"""

import capi_stack
import hv_upgrade
from kube import KubeError

DEFAULT_CLASS_ANN = "storageclass.kubernetes.io/is-default-class"
READS = ("version", "nodes", "charts", "lh_nodes", "storageclasses")
CAPI_READS = ("server_version", "traefik", "ingress_expose", "capi_providers")
CAPI_PHASES = ("capi", "clusters")
CODES = ("api-unreachable", "read-failed", "nodes-missing", "nodes-not-ready", "charts-not-ready",
         "longhorn-not-schedulable", "no-default-storageclass",
         "harvester-too-old", "rke2-traefik-no-address", "turtles-not-ready")
MIN_HARVESTER = (1, 9)


class _ReadsKube:
    """Les objets déjà lus, servis à capi_stack.stack_status comme un
    client en lecture : sa règle s'applique telle quelle. Lecture absente
    (None) = KubeError, ce que stack_status reçoit d'une lecture refusée."""

    def __init__(self, reads):
        self.reads = reads

    def get(self, kind, ns, name):
        if kind == "settings.harvesterhci.io" and name == "server-version":
            v = self.reads.get("server_version")
            return {"value": v} if v else None
        if kind == "services" and ns == capi_stack.SHIM_NS and name == "rke2-traefik":
            return self.reads.get("traefik")
        if kind == "services" and ns == capi_stack.SHIM_NS and name == capi_stack.SHIM_NAME:
            return self.reads.get("ingress_expose")
        return None

    def list(self, kind, ns=None, selector=None):
        if kind == capi_stack.K_CAPIPROVIDER:
            got = self.reads.get("capi_providers")
            if got is None:
                raise KubeError("capiproviders not read")
            return got
        return []


def harvester_too_old(ver):
    """Vrai si la version lue est en dessous de v1.9 (décision 117) ; une
    version illisible n'est pas jugée ici."""
    parsed = hv_upgrade.parse_version(ver)
    return isinstance(parsed, tuple) and parsed[:2] < MIN_HARVESTER


def capi_unmet(reads):
    """Constats Cluster API de la disponibilité (voir l'en-tête)."""
    unmet = []
    ver = reads.get("server_version")
    parsed = hv_upgrade.parse_version(ver)
    if harvester_too_old(ver):
        unmet.append({"code": "harvester-too-old", "detail": f"Harvester {ver}: Cluster API needs v1.9 or later"})
    elif ver is None or parsed is None:
        unmet.append({"code": "read-failed", "detail": "server-version"})
    kube = _ReadsKube(reads)
    if not capi_stack.harvester_vip(kube):
        unmet.append({"code": "rke2-traefik-no-address",
                      "detail": "neither kube-system/rke2-traefik nor ingress-expose gives the Harvester address"})
    st = capi_stack.stack_status(kube)
    if not st["turtles"] or not (st["core"] or {}).get("ready"):
        unmet.append({"code": "turtles-not-ready",
                      "detail": "no CAPIProvider readable" if not st["turtles"]
                      else "the Cluster API core is not ready"})
    return unmet



def _name(o):
    return ((o or {}).get("metadata") or {}).get("name") or ""


def _precheck(rows, key):
    """Ligne `key` de hv_upgrade.prechecks : la règle est la sienne, pas une
    copie. Ligne absente (clé renommée là-bas) : jamais prise pour « prêt »."""
    return next((r for r in rows if r.get("key") == key),
                {"ok": False, "text": f"precheck {key} not found in hv_upgrade"})


def _cond_true(obj, ctype):
    return any(c.get("type") == ctype and str(c.get("status")) == "True"
               for c in ((obj or {}).get("status") or {}).get("conditions") or [])


def evaluate(reads, expected_nodes=None, phases=()):
    """{"ready": bool, "unmet": [{"code", "detail"}]}.

    `reads` : {"version", "nodes", "charts", "lh_nodes", "storageclasses"},
    `None` pour une lecture en échec. `expected_nodes` : noms d'hôtes des
    lignes de la série ; sans eux, les nœuds lus. `phases` : identifiants
    des phases activées : `capi` ou `clusters` ajoute les constats Cluster
    API (lectures `server_version`, `traefik`, `capi_providers`)."""
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
    if any(p in CAPI_PHASES for p in phases or ()):
        unmet.extend(capi_unmet(reads))
    return {"ready": not unmet, "unmet": unmet}
