"""harvester-ops : décisions de la réplication asynchrone de VMs (v1.97.0).

Ce module ne parle à aucun cluster. Il reçoit des objets déjà lus (plan,
ressources, volumes, passages relevés, faits des deux clusters) et décide :

* les noms des objets créés sur la cible (déterministes : une relance
  retrouve les mêmes) et les collisions avec l'existant ;
* si un disque est éligible, et si un plan peut être protégé ou basculé
  (contrôle préalable, même forme de constats que `vm_transfer.check`) ;
* l'intervalle des sauvegardes tiré de l'objectif de RPO et des mesures, la
  prévision des données envoyées par passage ;
* l'état d'un plan et de chacune de ses ressources, et les actions permises
  depuis chaque côté (source unique pour la ligne de commande, la console et
  le futur sous-menu « Disaster Recovery » de chaque hôte) ;
* les manifestes Longhorn, Kubernetes et KubeVirt à poser.

Mécanisme (voir docs/design/2026-10-22-replication-asynchrone.md) : une tâche
récurrente Longhorn sauvegarde chaque volume de la VM vers une cible commune ;
un volume de secours (`Standby`) se restaure de façon incrémentale côté
cible ; la bascule active ces volumes, pose PV et PVC, puis recrée la VM.

Bibliothèque standard seulement : importé par `bin/harvester-replication.py`,
qui tourne aussi sur un hôte airgap sans la console. `now` est toujours passé.
"""

import hashlib
import math
import re
from datetime import timedelta

import hv_bookings as hb
import vm_transfer as vt

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

LABEL_PLAN = "harvester-ops.io/replication-plan"
LABEL_GENERATION = "harvester-ops.io/replication-generation"
LABEL_VM = "harvester-ops.io/replication-vm"

LONGHORN_NS = "longhorn-system"
LONGHORN_DRIVER = "driver.longhorn.io"
CARNET_NAMESPACE = "harvester-ops-replication"
# longhorn-manager types/types.go : LonghornLabelVolume = "longhornvolume"
# (l. 213 en v1.12.1) ; jamais de nom de moteur fabriqué (`<volume>-e-0`)
ENGINE_VOLUME_LABEL = "longhornvolume"
# étiquette de tâche sur un volume, gardée par Harvester (vérifié sur le banc)
JOB_LABEL_PREFIX = "recurring-job.longhorn.io/"
JOB_GROUP_LABEL_PREFIX = "recurring-job-group.longhorn.io/"
IMAGE_ID_ANNOTATION = "harvesterhci.io/imageId"

PLAN_ID_RE = re.compile(r"^rp-[a-f0-9]{12}$")
NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")       # RFC 1123, étiquette
CLUSTER_RE = hb.CLUSTER_RE
_LABEL_KEY_RE = re.compile(r"^(?:[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?/)?"
                           r"[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
_LABEL_VALUE_RE = re.compile(r"^(?:[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")

# longhorn-manager datastore/longhorn.go : `NameMaximumLength = 40` (l. 43 en
# v1.11.2, l. 44 en v1.12.1), contrôlé par `CheckVolume` (l. 1176 en v1.11.2,
# l. 1285 en v1.12.1 : `if len(v.Name) > NameMaximumLength`) en plus de
# `validation.IsDNS1123Label`, et pour les tâches récurrentes (l. 5668 / 6201).
# Le webhook ne contrôle que `util.ValidateName` (`^[a-z0-9][a-z0-9_.-]+$`).
VOLUME_NAME_MAX = 40
JOB_NAME_MAX = 40

# longhorn-manager types/types.go `ValidateBackupBlockSize` (l. 1207-1213 en
# v1.12.1) et k8s/pkg/apis/longhorn/v1beta2/volume.go `BackupBlockSize`
# (l. 311-315 en v1.11.2, l. 382-386 en v1.12.1) : 2 Mio ou 16 Mio, fixée à la
# création du volume. 2 Mio relevés sur les deux versions du banc.
BLOCK_SIZE = 2 * 1024 ** 2
BLOCK_SIZES = (2 * 1024 ** 2, 16 * 1024 ** 2)

HOST_KINDS = ("harvester",)              # `kvm` viendra plus tard, même modèle
RESOURCE_KINDS = ("vm",)                 # `kvm-domain` plus tard
SIDES = ("source", "target")

# Pas permis : diviseurs de l'heure puis du jour. Un `*/7` repartirait à zéro
# chaque heure (passage de 4 min entre :56 et :00).
CRON_STEPS = (60, 120, 180, 240, 300, 360, 600, 720, 900, 1200, 1800,
              3600, 7200, 10800, 14400, 21600, 28800, 43200, 86400)
RPO_MIN, RPO_MAX = 60, 7 * 86400
DEFAULT_RETAIN, RETAIN_MIN, RETAIN_MAX = 6, 3, 50
DEFAULT_MARGIN = 0.2
DEFAULT_RESYNC_S = 10                    # relecture déclenchée : 9 à 10 s sur le banc
SKEW_TOLERANCE_S = 10

# Paires Longhorn vérifiées de bout en bout sur le banc (22-23/10/2026)
VERIFIED_LONGHORN_PAIRS = {((1, 11, 2), (1, 12, 1)), ((1, 12, 1), (1, 11, 2))}

# Délais de départ (secondes) des attentes du déroulé ; `barrier` vaut la
# moitié de l'intervalle (None ici). Surchargés par `--timeout` et par les
# options du plan.
TIMEOUTS = {"probe": 15, "stop": 600, "snapshot": 300, "final_backup": 6 * 3600,
            "resync_apply": 600, "initial_restore": 12 * 3600, "backing_image": 2 * 3600,
            "activate": 300, "bind": 120, "vm_start": 900, "unplanned_sync": 120,
            "barrier": None}
TIMEOUT_MAX = 7 * 86400

OPTION_CHOICES = {"cloud_init": ("keep", "rerun"), "mac": ("keep", "new"),
                  "start": ("started", "stopped"), "follow": ("console", "cli", "off")}
DEFAULT_OPTIONS = {"cloud_init": "keep", "mac": "keep", "create_namespace": False,
                   "start": "started", "follow": "console", "paused": False, "timeouts": {}}
MAPPING_KINDS = ("networks", "storage_classes", "namespaces", "images", "claims", "target_names")

# -- états ------------------------------------------------------------------

PLAN_STATES = ("draft", "measuring", "protecting", "protected", "failing_over",
               "failed_over", "unprotecting", "unprotected", "error")
# Une transition vers soi-même n'est permise que pour un état intermédiaire :
# c'est la reprise d'une commande interrompue.
PLAN_TRANSITIONS = {
    "draft": ("measuring", "protecting", "unprotecting", "error"),
    "measuring": ("measuring", "draft", "protecting", "unprotecting", "error"),
    "protecting": ("protecting", "protected", "unprotecting", "error"),
    # protect relancé après un `plan-drift` (disque ajouté, taille changée)
    "protected": ("protecting", "failing_over", "unprotecting", "error"),
    # avant l'activation tout est réversible : retour à `protected`
    "failing_over": ("failing_over", "protected", "failed_over", "error"),
    "failed_over": ("unprotecting", "error"),
    "unprotecting": ("unprotecting", "unprotected", "error"),
    "unprotected": (),
    # depuis `error`, le registre restreint aux transitions de l'état d'origine
    "error": ("measuring", "protecting", "failing_over", "unprotecting"),
}
# États d'où part chaque commande (elle reprend depuis son état intermédiaire)
COMMAND_FROM = {
    "measure-start": ("draft", "measuring", "error"),
    "measure-stop": ("measuring",),
    "protect": ("draft", "measuring", "protecting", "protected", "error"),
    "failover": ("protected", "failing_over", "error"),
    "unprotect": tuple(s for s in PLAN_STATES if s not in ("failing_over", "unprotected")),
}

RESOURCE_STATES = ("draft", "measuring", "protecting", "protected", "lagging", "drifting",
                   "degraded", "source-unreachable", "failing_over", "failed_over",
                   "unprotecting", "unprotected", "error")
# rangés en base ; les quatre autres sont calculés à la lecture
STORED_RESOURCE_STATES = PLAN_STATES
_FOLLOWED = ("protected", "lagging", "drifting", "degraded", "source-unreachable")

VOLUME_STATES = ("pending", "restoring", "standby", "activated", "removed")
TICK_STATES = ("in_progress", "completed", "error", "applied", "skipped")

# -- actions ----------------------------------------------------------------

ROLES = ("viewer", "operator", "admin")
ACTION_ROLES = {
    "measure-start": "operator", "measure-stop": "operator", "protect": "operator",
    "failover": "admin", "failover-unplanned": "admin", "unprotect": "operator",
    "unprotect-delete-backups": "admin", "pause": "operator", "resume": "operator",
    "delete": "operator",
}
ACTION_ORDER = ("measure-start", "measure-stop", "protect", "failover", "failover-unplanned",
                "pause", "resume", "unprotect", "unprotect-delete-backups", "delete")
_PAUSE = "pause|resume"
_PROTECTED_ACTIONS = ("failover", "failover-unplanned", _PAUSE, "unprotect", "unprotect-delete-backups")
# Une action pas encore livrée (`drill`, `failback`) s'ajoutera ici seulement.
STATE_ACTIONS = {
    "draft": ("measure-start", "protect"),
    "measuring": ("measure-stop", "protect", _PAUSE, "unprotect"),
    "protecting": ("protect", _PAUSE, "unprotect", "unprotect-delete-backups"),
    "protected": _PROTECTED_ACTIONS,
    "lagging": _PROTECTED_ACTIONS,
    "drifting": ("protect",) + _PROTECTED_ACTIONS,
    "degraded": _PROTECTED_ACTIONS,
    "source-unreachable": ("failover-unplanned", _PAUSE, "unprotect"),
    "failing_over": ("failover",),
    "failed_over": ("unprotect", "unprotect-delete-backups"),
    "unprotecting": ("unprotect", "unprotect-delete-backups"),
    "unprotected": (),
    "error": ("protect", "unprotect", "unprotect-delete-backups"),
}
# Bascule : regardée depuis la cible (ou la vue de tous les plans) ; depuis la
# source seulement quand la source est injoignable.
_TARGET_ONLY = ("failover", "failover-unplanned")

_DISK_TYPES_NOT_REPLICATED = ("containerDisk", "hostDisk", "emptyDisk")


class PlanError(ValueError):
    """Demande de plan refusée : `code` stable, message lisible."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Petits outils
# ---------------------------------------------------------------------------

def _t(v):
    return hb.parse_time(v)


def _opt_t(v):
    return hb.parse_time(v) if v else None


def _secs(a, b):
    return (b - a).total_seconds()


def iso(v):
    return hb.iso(v)


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value):
    """`90`, `90s`, `15m`, `1h`, `1d` -> secondes. PlanError sinon."""
    if isinstance(value, bool):
        raise PlanError("bad-duration", "a duration is expected (90s, 15m, 1h)")
    if isinstance(value, int):
        return value
    m = _DURATION_RE.match(str(value or ""))
    if not m:
        raise PlanError("bad-duration", f"unreadable duration: {str(value)[:20]}")
    return int(m.group(1)) * _UNITS[m.group(2)]


def percentile(values, p):
    """Rang le plus proche (pas d'interpolation) ; None sans valeur."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(1, math.ceil(p / 100.0 * len(vals)))
    return vals[k - 1]


def quantity(n):
    """Octets -> quantité Kubernetes la plus courte exacte (`2Gi`, `512Mi`)."""
    n = int(n)
    for unit, mult in (("Ti", 1024 ** 4), ("Gi", 1024 ** 3), ("Mi", 1024 ** 2), ("Ki", 1024)):
        if n and n % mult == 0:
            return f"{n // mult}{unit}"
    return str(n)


def plan_labels(plan_id, generation, vm_uid=None):
    out = {LABEL_PLAN: plan_id, LABEL_GENERATION: str(int(generation))}
    if vm_uid:
        out[LABEL_VM] = vm_uid
    return out


def owned_by(labels, plan_id):
    return (labels or {}).get(LABEL_PLAN) == plan_id


# ---------------------------------------------------------------------------
# Validation des entrées d'un plan
# ---------------------------------------------------------------------------

def _ref(value, field):
    """`namespace/nom`, deux noms RFC 1123."""
    if not isinstance(value, str) or value.count("/") != 1:
        raise PlanError("bad-reference", f"{field}: namespace/name expected")
    ns, name = value.split("/")
    if not NAME_RE.match(ns) or not NAME_RE.match(name):
        raise PlanError("bad-reference", f"{field}: invalid name {value[:80]}")
    return value


def _selector(value):
    if not isinstance(value, str) or value.count("=") != 1:
        raise PlanError("bad-selector", "selector: key=value expected")
    k, v = value.split("=")
    if not _LABEL_KEY_RE.match(k) or not _LABEL_VALUE_RE.match(v):
        raise PlanError("bad-selector", f"selector: invalid label {value[:80]}")
    return value


def _mapping(kind, raw):
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise PlanError("bad-mapping", f"{kind}: a mapping is expected")
    out = {}
    for s, d in raw.items():
        if kind in ("networks", "images", "target_names"):
            _ref(s, kind)
            if kind == "target_names":
                if not isinstance(d, str) or not NAME_RE.match(d):
                    raise PlanError("bad-mapping", f"{kind}: invalid target name for {s}")
            else:
                _ref(d, kind)
        else:
            for x in (s, d):
                if not isinstance(x, str) or not NAME_RE.match(x):
                    raise PlanError("bad-mapping", f"{kind}: invalid name {str(x)[:80]}")
        out[s] = d
    return out


def normalize_timeouts(raw):
    """Surcharges de délais : noms connus, entiers de 1 s à 7 jours."""
    out = {}
    for k, v in (raw or {}).items():
        if k not in TIMEOUTS:
            raise PlanError("bad-timeout", f"unknown timeout: {str(k)[:30]}")
        s = parse_duration(v)
        if not 1 <= s <= TIMEOUT_MAX:
            raise PlanError("bad-timeout", f"timeout {k} out of range")
        out[k] = s
    return out


def normalize_plan(req):
    """Une demande de création de plan, contrôlée et complétée de ses valeurs
    par défaut. PlanError au premier défaut. Rien n'est lu sur un cluster."""
    name = req.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise PlanError("bad-name", "name: a lowercase RFC 1123 name is expected")
    src, dst = req.get("source_cluster"), req.get("target_cluster")
    for field, v in (("source_cluster", src), ("target_cluster", dst)):
        if not isinstance(v, str) or not CLUSTER_RE.match(v):
            raise PlanError("bad-cluster", f"{field}: a cluster name is expected")
    if src == dst:
        raise PlanError("same-cluster", "source and target must be two different clusters")
    kinds = {}
    for field in ("source_host_kind", "target_host_kind"):
        k = req.get(field) or "harvester"
        if k not in HOST_KINDS:
            raise PlanError("bad-host-kind", f"{field}: {k} is not supported yet")
        kinds[field] = k

    try:
        rpo = parse_duration(req.get("rpo_objective_s"))
    except PlanError:
        raise PlanError("bad-rpo", "rpo: a duration is expected (15m, 1h)") from None
    if not RPO_MIN <= rpo <= RPO_MAX:
        raise PlanError("bad-rpo", f"rpo: between {RPO_MIN} s and 7 days")

    interval = req.get("interval_s")
    if interval not in (None, ""):
        interval = parse_duration(interval)
        if interval not in CRON_STEPS:
            raise PlanError("bad-interval", "interval: one of 1-6, 10, 12, 15, 20, 30 min or 1-4, 6, 8, 12, 24 h")
    else:
        interval = None

    retain = req.get("retain", DEFAULT_RETAIN)
    if isinstance(retain, bool) or not isinstance(retain, int) or not RETAIN_MIN <= retain <= RETAIN_MAX:
        raise PlanError("bad-retain", f"retain: an integer between {RETAIN_MIN} and {RETAIN_MAX}")

    vms, selector, sel_ns = req.get("vms") or [], req.get("selector") or "", req.get("namespace") or ""
    if bool(vms) == bool(selector):
        raise PlanError("bad-selection", "give either VMs (namespace/name) or a selector with a namespace")
    if vms:
        if not isinstance(vms, list):
            raise PlanError("bad-selection", "vms: a list is expected")
        vms = [_ref(v, "vms") for v in vms]
        if len(set(vms)) != len(vms):
            raise PlanError("bad-selection", "vms: listed twice")
    else:
        _selector(selector)
        if not NAME_RE.match(sel_ns):
            raise PlanError("bad-selection", "selector: a namespace is expected")

    mappings = {}
    raw_maps = req.get("mappings") or {}
    if not isinstance(raw_maps, dict):
        raise PlanError("bad-mapping", "mappings: a mapping is expected")
    for k in raw_maps:
        if k not in MAPPING_KINDS:
            raise PlanError("bad-mapping", f"unknown mapping: {str(k)[:30]}")
    for k in MAPPING_KINDS:
        mappings[k] = _mapping(k, raw_maps.get(k))

    opts = dict(DEFAULT_OPTIONS, timeouts={})
    raw_opts = req.get("options") or {}
    for k, v in raw_opts.items():
        if k in OPTION_CHOICES:
            if v not in OPTION_CHOICES[k]:
                raise PlanError("bad-option", f"{k}: one of {', '.join(OPTION_CHOICES[k])}")
            opts[k] = v
        elif k in ("create_namespace", "paused"):
            if not isinstance(v, bool):
                raise PlanError("bad-option", f"{k}: true or false")
            opts[k] = v
        elif k == "timeouts":
            opts[k] = normalize_timeouts(v)
        elif k == "skew_tolerance_s":
            s = parse_duration(v)
            if not 0 <= s <= 3600:
                raise PlanError("bad-option", "skew_tolerance_s: 0 to 3600")
            opts[k] = s
        else:
            raise PlanError("bad-option", f"unknown option: {str(k)[:30]}")

    return {"name": name, "source_cluster": src, "target_cluster": dst, **kinds,
            "rpo_objective_s": rpo, "interval_s": interval,
            "cron": cron_for(interval) if interval else "", "retain": retain,
            "vms": vms, "selector": selector, "selector_namespace": sel_ns if selector else "",
            "mappings": mappings, "options": opts}


# ---------------------------------------------------------------------------
# Noms
# ---------------------------------------------------------------------------

def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _clean(text, limit):
    s = re.sub(r"[^a-z0-9-]+", "-", str(text or "").lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:limit].rstrip("-")


def target_volume_name(plan_id, vm_uid, disk, generation, vm_name):
    """`hr-<vm>-<disque>-<6 hex>`, au plus VOLUME_NAME_MAX caractères.

    L'empreinte porte sur `plan_id|vm_uid|disk|generation` : une relance
    retrouve le même nom, une nouvelle génération (retour au site principal)
    en donne un autre. Les parties lisibles ne sont qu'une aide."""
    h = _digest(f"{plan_id}|{vm_uid}|{disk}|{int(generation)}")[:6]
    room = VOLUME_NAME_MAX - len("hr---") - len(h)           # 29 caractères lisibles
    d = _clean(disk, 12) or "disk"
    v = _clean(vm_name, room - len(d)) or "vm"
    return f"hr-{v}-{d}-{h}"


def _hex8(value):
    hx = re.sub(r"[^a-f0-9]", "", str(value or "").lower())
    return hx[:8] if len(hx) >= 8 else _digest(str(value))[:8]


def recurring_job_name(plan_id, vm_uid):
    """Une tâche par VM : `hr-<8 hex du plan>-<8 hex de la VM>`."""
    return f"hr-{_hex8(plan_id[3:] if str(plan_id).startswith('rp-') else plan_id)}-{_hex8(vm_uid)}"


def carnet_name(plan_id, vm_uid):
    """Le carnet de reprise d'une VM (Secret dans CARNET_NAMESPACE)."""
    return recurring_job_name(plan_id, vm_uid)


def target_ref(plan, resource):
    """(namespace, nom) de la VM cible : namespace mappé, nom source sauf
    correspondance donnée."""
    maps = plan.get("mappings") or {}
    ns, name = resource["namespace"], resource["name"]
    tns = (maps.get("namespaces") or {}).get(ns, ns)
    tname = (maps.get("target_names") or {}).get(f"{ns}/{name}", name)
    return tns, tname


def target_claim(plan, namespace, claim):
    """Nom du PVC cible : celui de la source, sauf correspondance donnée."""
    return ((plan.get("mappings") or {}).get("claims") or {}).get(f"{namespace}/{claim}", claim)


def planned_objects(plan, resources, volumes):
    """Ce que le plan posera, par genre : volumes et PV (même nom), tâches,
    carnets, PVC et VM cibles."""
    pid = plan["id"]
    out = {"volumes": [], "recurring_jobs": [], "carnets": [], "pvcs": [], "vms": []}
    for r in resources:
        uid = r["resource_uid"]
        tns, tname = target_ref(plan, r)
        out["recurring_jobs"].append(recurring_job_name(pid, uid))
        out["carnets"].append(f"{CARNET_NAMESPACE}/{carnet_name(pid, uid)}")
        out["vms"].append(f"{tns}/{tname}")
        for v in volumes:
            if v.get("vm_uid") != uid or v.get("state") == "removed":
                continue
            out["volumes"].append(v.get("target_volume") or target_volume_name(
                pid, uid, v["disk"], plan.get("generation", 1), r["name"]))
            out["pvcs"].append(f"{tns}/{target_claim(plan, r['namespace'], v['claim'])}")
    return out


def collisions(target_facts, plan, resources, volumes, phase="protect"):
    """Objets déjà présents sous le nom que le plan veut poser.

    `target_facts` : {"volumes"|"recurring_jobs"|"carnets"|"pvcs"|"vms":
    {nom: étiquettes}} (les tâches sont relues sur la SOURCE, le reste sur la
    cible). Un volume, une tâche ou un carnet sans nos étiquettes (ou avec un
    autre plan) bloque ; un PVC ou une VM avertit à la protection et bloque à
    la bascule. Les backing images ne passent pas ici : une image de même
    nom et de même somme est réutilisée (voir `check`)."""
    pid = plan["id"]
    planned = planned_objects(plan, resources, volumes)
    out = []
    for kind, code in (("volumes", "target-volume-taken"), ("recurring_jobs", "recurring-job-taken"),
                       ("carnets", "carnet-taken")):
        existing = target_facts.get(kind) or {}
        for name in planned[kind]:
            if name in existing and not owned_by(existing[name], pid):
                out.append(_finding(code, "block", name=name,
                                    plan=(existing[name] or {}).get(LABEL_PLAN) or ""))
    level = "block" if phase == "failover" else "warn"
    for kind, code in (("pvcs", "target-pvc-taken"), ("vms", "target-vm-taken")):
        existing = target_facts.get(kind) or {}
        for name in planned[kind]:
            if name in existing and not owned_by(existing[name], pid):
                out.append(_finding(code, level, name=name))
    return out


# ---------------------------------------------------------------------------
# Éligibilité et contrôle
# ---------------------------------------------------------------------------

def _finding(code, level, **facts):
    return {"code": code, "level": level, "facts": facts}


_ORDER = {"block": 0, "warn": 1, "ok": 2}


def blocking(findings):
    return any(f["level"] == "block" for f in findings)


def volume_block_size(lh_volume):
    """Taille de bloc des sauvegardes du volume (`spec.backupBlockSize`,
    Longhorn récent), 2 Mio sinon."""
    try:
        v = int(((lh_volume or {}).get("spec") or {}).get("backupBlockSize") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v in BLOCK_SIZES else BLOCK_SIZE


def _in_default_group(lh_volume):
    """Longhorn range dans le groupe `default` un volume sans aucune
    étiquette de tâche ni de groupe, ou qui porte celle du groupe."""
    labels = ((lh_volume or {}).get("metadata") or {}).get("labels") or {}
    if labels.get(JOB_GROUP_LABEL_PREFIX + "default") == "enabled":
        return True, False
    if not any(k.startswith((JOB_LABEL_PREFIX, JOB_GROUP_LABEL_PREFIX)) for k in labels):
        return True, True
    return False, False


def volume_findings(lh_volume, pvc, sc, default_backup_jobs=()):
    """Constats d'éligibilité d'un disque. `default_backup_jobs` : noms des
    tâches de sauvegarde du groupe `default` sur la source."""
    claim = ((pvc or {}).get("metadata") or {}).get("name")
    if (sc or {}).get("provisioner") != LONGHORN_DRIVER or not lh_volume:
        return [_finding("not-longhorn", "block", claim=claim,
                         provisioner=(sc or {}).get("provisioner"))]
    spec = lh_volume.get("spec") or {}
    vol = (lh_volume.get("metadata") or {}).get("name")
    out = []
    if spec.get("dataEngine") == "v2":
        out.append(_finding("data-engine-v2", "block", volume=vol, claim=claim))
    if spec.get("encrypted"):
        out.append(_finding("encrypted-volume", "block", volume=vol, claim=claim, verified=False))
    if spec.get("accessMode") == "rwx" and not spec.get("migratable"):
        out.append(_finding("rwx-share-manager", "block", volume=vol, claim=claim, verified=False))
    if default_backup_jobs:
        member, implicit = _in_default_group(lh_volume)
        if member:
            # implicite : la pose de notre étiquette sort le volume du groupe
            out.append(_finding("default-backup-job", "warn", volume=vol, claim=claim,
                                jobs=sorted(default_backup_jobs), implicit=implicit))
    return out


def vm_disk_findings(vm):
    """Disques que la réplication ne recopie pas (ni PVC, ni cloud-init)."""
    out = []
    for vol in vt._tspec(vm).get("volumes") or []:
        for t in _DISK_TYPES_NOT_REPLICATED:
            if t in vol:
                out.append(_finding("disk-not-replicated", "warn", disk=vol.get("name"), type=t))
    return out


def vm_spec_generation(vm):
    """Version de la spec d'une VM, pour la dérive : `metadata.generation`,
    qui ne bouge qu'à un changement de spec. Jamais `resourceVersion`, qui
    change à chaque annotation (dossier de la console) ou mise à jour d'état
    et ferait passer la VM pour modifiée."""
    g = ((vm or {}).get("metadata") or {}).get("generation")
    try:
        return int(g) if g is not None else None
    except (TypeError, ValueError):
        return None


def firmware_uuid(vm):
    return (((vt._tspec(vm).get("domain") or {}).get("firmware")) or {}).get("uuid") or ""


def _target_class(disk, plan, dst):
    """Classe cible d'un disque et constat éventuel."""
    maps = plan.get("mappings") or {}
    classes = dst.get("storage_classes") or {}
    image = disk.get("image")
    if image:
        mapped = (maps.get("images") or {}).get(image)
        if mapped:
            sc = ((dst.get("images") or {}).get(mapped) or {}).get("storage_class")
            if sc in classes:
                return sc, None
            return None, _finding("image-unmapped", "block", image=image, mapped=mapped)
    sc = disk.get("storage_class")
    m = (maps.get("storage_classes") or {}).get(sc, sc)
    if m in classes:
        return m, None
    if image:
        # disque d'image sans image correspondante : la backing image suit le
        # volume, le PVC prend la classe par défaut de la cible
        d = dst.get("default_storage_class")
        if d in classes:
            return d, _finding("image-unmapped", "warn", image=image, storage_class=d)
    return None, _finding("storage-class-unmapped", "block", storage_class=sc, mapped=m)


def check(src, dst, plan):
    """Contrôle d'un plan : liste de constats {code, level, facts}, blocages
    d'abord.

    `src` : {"cluster", "reachable", "backup_target" (réglage brut),
    "backup_target_available", "longhorn_version", "backing_images": {nom:
    {"checksum", "size"}}, "default_backup_jobs": [...], "recurring_jobs":
    {nom: étiquettes}, "vms": [{"uid", "namespace", "name", "vm", "disks":
    [{"disk", "claim", "size", "storage_class", "image", "lh_volume", "pvc",
    "sc"}]}]}.
    `dst` : {"cluster", "reachable", "denied", "backup_target",
    "backup_target_available", "longhorn_version", "networks": [...],
    "namespaces": [...], "storage_classes": {nom: {"replicas", "allocatable",
    "degraded_allocatable", "nodes"}}, "default_storage_class", "images":
    {"ns/nom": {"storage_class"}}, "backing_images": {nom: {"checksum"}},
    "macs": {mac: "ns/vm"}, "volumes"|"carnets"|"pvcs"|"vms": {nom:
    étiquettes}, "through_rancher"}.
    `plan` : plan normalisé avec `id`, `generation`, et `phase`
    (`protect` ou `failover`)."""
    phase = plan.get("phase", "protect")
    if dst.get("denied"):
        return [_finding("target-denied", "block", cluster=dst.get("cluster"))]
    if not dst.get("reachable"):
        return [_finding("target-unreachable", "block", cluster=dst.get("cluster"))]
    if not src.get("reachable"):
        return [_finding("source-unreachable", "block", cluster=src.get("cluster"))]
    out = []
    vms = src.get("vms") or []
    if not vms:
        out.append(_finding("no-vm", "block"))

    s_bt, d_bt = vt.normalize_backup_target(src.get("backup_target")), \
        vt.normalize_backup_target(dst.get("backup_target"))
    if not s_bt or not d_bt or tuple(s_bt) != tuple(d_bt):
        out.append(_finding("backup-target-differs", "block"))
    for side, facts in (("source", src), ("target", dst)):
        if not facts.get("backup_target_available"):
            out.append(_finding("backup-target-unavailable", "block", side=side,
                                cluster=facts.get("cluster")))

    sv, dv = vt.version_tuple(src.get("longhorn_version")), vt.version_tuple(dst.get("longhorn_version"))
    if (sv, dv) in VERIFIED_LONGHORN_PAIRS:
        out.append(_finding("longhorn-pair-verified", "ok", source=src.get("longhorn_version"),
                            target=dst.get("longhorn_version")))
    else:
        out.append(_finding("longhorn-pair-unverified", "warn", source=src.get("longhorn_version"),
                            target=dst.get("longhorn_version")))
        if dv < sv:
            out.append(_finding("longhorn-target-older", "warn", source=src.get("longhorn_version"),
                                target=dst.get("longhorn_version")))

    maps = plan.get("mappings") or {}
    opts = plan.get("options") or {}
    nets = maps.get("networks") or {}
    needed, images_seen = {}, set()
    src_bi, dst_bi = src.get("backing_images") or {}, dst.get("backing_images") or {}
    namespaces_seen = set()
    resources, volumes = [], []
    for vm in vms:
        uid = vm["uid"]
        obj = vm.get("vm") or {}
        inv = vt.vm_inventory(obj, {})
        resources.append({"resource_uid": uid, "namespace": vm["namespace"], "name": vm["name"]})
        tns, tname = target_ref(plan, resources[-1])
        if tns not in namespaces_seen:
            namespaces_seen.add(tns)
            if tns not in (dst.get("namespaces") or []):
                out.append(_finding("namespace-missing", "ok" if opts.get("create_namespace") else "block",
                                    namespace=tns, create=bool(opts.get("create_namespace"))))
        for n in inv["networks"]:
            m = nets.get(n, n)
            if m not in (dst.get("networks") or []):
                out.append(_finding("network-unmapped", "block", vm=f"{vm['namespace']}/{vm['name']}",
                                    network=n, mapped=m))
        out += vm_disk_findings(obj)
        for d in vm.get("disks") or []:
            out += volume_findings(d.get("lh_volume"), d.get("pvc"), d.get("sc"),
                                   src.get("default_backup_jobs") or ())
            volumes.append({"vm_uid": uid, "disk": d["disk"], "claim": d["claim"]})
            sc, f = _target_class(d, plan, dst)
            if f:
                out.append(f)
            if not sc:
                continue
            needed[sc] = needed.get(sc, 0) + int(d.get("size") or 0)
            bi = ((d.get("lh_volume") or {}).get("spec") or {}).get("backingImage")
            if bi and bi not in images_seen:
                images_seen.add(bi)
                there, mine = dst_bi.get(bi), src_bi.get(bi) or {}
                if there is None:
                    out.append(_finding("backing-image-restore", "ok", backing_image=bi))
                    needed[sc] += int(mine.get("size") or 0)
                elif there.get("checksum") != mine.get("checksum"):
                    out.append(_finding("backing-image-conflict", "block", backing_image=bi))
                else:
                    out.append(_finding("backing-image-reuse", "ok", backing_image=bi))
        if opts.get("mac", "keep") == "keep":
            taken = dst.get("macs") or {}
            clash = sorted(m for m in inv.get("macs") or [] if m in taken and taken[m] != f"{tns}/{tname}")
            if clash:
                out.append(_finding("mac-in-use", "block" if phase == "failover" else "warn",
                                    vm=f"{vm['namespace']}/{vm['name']}", macs=", ".join(clash),
                                    vms=", ".join(sorted({taken[m] for m in clash}))))
        if opts.get("cloud_init", "keep") == "keep" and not (vm.get("firmware_uuid") or firmware_uuid(obj)):
            out.append(_finding("firmware-uuid-unknown", "block", vm=f"{vm['namespace']}/{vm['name']}"))
        if inv.get("devices"):
            out.append(_finding("devices-removed", "warn", vm=f"{vm['namespace']}/{vm['name']}",
                                devices=list(inv["devices"])))
        if inv.get("node_affinity"):
            out.append(_finding("node-affinity-removed", "warn", vm=f"{vm['namespace']}/{vm['name']}"))

    # Place : un volume de secours occupe sa taille dès sa création. L'allouable
    # de storage_room est la place du n-ième meilleur nœud (n = répliques) :
    # chaque réplique y tient, la somme des tailles se compare donc à lui.
    classes = dst.get("storage_classes") or {}
    for sc, n in sorted(needed.items()):
        info = classes.get(sc) or {}
        alloc = int(info.get("allocatable") or 0)
        if not alloc and info.get("degraded_allocatable"):
            out.append(_finding("replicas-degraded", "warn", storage_class=sc,
                                replicas=info.get("replicas"), nodes=info.get("nodes")))
            alloc = int(info["degraded_allocatable"])
        if n > alloc:
            out.append(_finding("capacity-short", "block", storage_class=sc, needed=n,
                                needed_total=n * int(info.get("replicas") or 1), allocatable=alloc))

    if opts.get("cloud_init") == "rerun":
        out.append(_finding("cloud-init-rerun", "warn"))
    if dst.get("through_rancher"):
        out.append(_finding("target-through-rancher", "warn", cluster=dst.get("cluster")))
    facts = dict(dst)
    facts["recurring_jobs"] = src.get("recurring_jobs") or {}
    out += collisions(facts, plan, resources, volumes, phase)
    return sorted(out, key=lambda f: _ORDER[f["level"]])


# ---------------------------------------------------------------------------
# Intervalle et prévision
# ---------------------------------------------------------------------------

def cron_for(interval_s):
    """`*/N * * * *` sous l'heure, `0 */H * * *` au-delà. ValueError hors
    CRON_STEPS."""
    if interval_s not in CRON_STEPS:
        raise ValueError(f"interval {interval_s} s is not a divisor of the hour or the day")
    if interval_s < 3600:
        return f"*/{interval_s // 60} * * * *"
    return f"0 */{interval_s // 3600} * * *"


def next_tick_at(interval_s, now):
    """Prochaine échéance du cron, strictement après `now` (UTC, l'heure du
    gestionnaire Longhorn). Les pas divisent le jour : l'échéance est un
    multiple du pas compté depuis minuit."""
    if not interval_s:
        return None
    now = _t(now)
    midnight = now.replace(hour=0, minute=0, second=0)
    k = math.floor(_secs(midnight, now) / interval_s) + 1
    return iso(midnight + timedelta(seconds=k * interval_s))


class IntervalChoice(tuple):
    """(pas, rpo_atteignable, tenable), avec `.reason` quand intenable."""

    def __new__(cls, step, rpo, tenable, reason=None):
        self = super().__new__(cls, (step, rpo, tenable))
        self.reason = reason
        return self

    step = property(lambda self: self[0])
    rpo = property(lambda self: self[1])
    tenable = property(lambda self: self[2])


def derive_interval(objective_s, send_p95_s, apply_p95_s, resync_s=DEFAULT_RESYNC_S, margin=DEFAULT_MARGIN):
    """Le plus grand pas tel que `pas + envoi + application + relecture <=
    objectif` et `envoi + application <= pas x (1 - marge)` (sinon le RPO
    dérive).

    Intenable : le plus petit pas qui ne dérive pas, avec son RPO
    (`objective-too-short`) ; si tous dérivent, le plus grand pas
    (`drift-at-every-step`)."""
    transfer = float(send_p95_s or 0) + float(apply_p95_s or 0)
    for step in reversed(CRON_STEPS):
        rpo = step + transfer + resync_s
        if rpo <= objective_s and transfer <= step * (1 - margin):
            return IntervalChoice(step, rpo, True)
    for step in CRON_STEPS:
        if transfer <= step * (1 - margin):
            return IntervalChoice(step, step + transfer + resync_s, False, "objective-too-short")
    step = CRON_STEPS[-1]
    return IntervalChoice(step, step + transfer + resync_s, False, "drift-at-every-step")


def blocks_forecast(size, writes, block=BLOCK_SIZE, write_size=4096):
    """Blocs (et octets) qu'une sauvegarde enverra après `writes` écritures de
    `write_size` octets sur un volume de `size` octets.

    Borne haute (écritures dispersées) : N x (1 - (1 - k/N)^écritures), N =
    taille / bloc, k = blocs touchés par une écriture (1 pour 4 Kio). Borne
    basse (écritures séquentielles) : octets écrits / bloc, arrondi au-dessus.
    Les deux plafonnées à N ; la haute jamais sous la basse. Écritures
    supposées alignées sur le bloc."""
    n = max(1, int(size) // int(block))
    writes = max(0, int(writes))
    k = min(n, max(1, math.ceil(int(write_size) / int(block))))
    lower = min(n, math.ceil(writes * int(write_size) / int(block)))
    upper = n * (1 - (1 - k / n) ** writes) if writes else 0.0
    upper = min(float(n), max(upper, float(lower)))
    return {"blocks": n, "block": int(block), "bytes_written": writes * int(write_size),
            "upper_blocks": upper, "upper_bytes": upper * block,
            "lower_blocks": lower, "lower_bytes": lower * block}


def aggregate_upper(measures, factor, cap=None):
    """Majorant pour un pas `factor` fois le pas mesuré : somme des envois de
    `factor` passages consécutifs (fenêtres glissantes). L'union des blocs
    salis est au plus la somme : la mesure au petit pas majore les grands.
    `cap` : taille du volume (un passage n'envoie jamais plus). Rend {samples,
    mean, p95, max}, listes vides si trop peu de mesures."""
    vals = [int(m or 0) for m in measures]
    factor = int(factor)
    if factor < 1:
        raise ValueError("factor must be at least 1")
    sums = [sum(vals[i:i + factor]) for i in range(0, len(vals) - factor + 1)]
    if cap:
        sums = [min(s, int(cap)) for s in sums]
    if not sums:
        return {"samples": [], "mean": None, "p95": None, "max": None}
    return {"samples": sums, "mean": sum(sums) / len(sums), "p95": percentile(sums, 95), "max": max(sums)}


def first_copy_eta(used_bytes, throughput):
    """Durée (s) de la première copie ; None tant qu'aucune sauvegarde n'a
    donné de débit (octets/s)."""
    if not throughput or throughput <= 0:
        return None
    return int(used_bytes or 0) / float(throughput)


# ---------------------------------------------------------------------------
# État d'un plan et de ses ressources
# ---------------------------------------------------------------------------

def _running_info(vm_running, uid):
    """vm_running[uid] : booléen, ou {"running": bool, "stopped_at": iso}."""
    v = (vm_running or {}).get(uid)
    if isinstance(v, dict):
        return bool(v.get("running", True)), _opt_t(v.get("stopped_at"))
    if v is None:
        return True, None
    return bool(v), None


def _vm_eval(plan, uid, volumes, ticks, now, vm_running):
    gen = int(plan.get("generation") or 1)
    interval = plan.get("interval_s") or 0
    now = _t(now)
    vols = [v for v in volumes if v.get("vm_uid") == uid and int(v.get("generation") or gen) == gen
            and v.get("state") != "removed"]
    names = {v["source_volume"] for v in vols}
    mine = sorted((t for t in ticks if t.get("source_volume") in names
                   and int(t.get("generation") or gen) == gen), key=lambda t: t.get("snapshot_at") or "")
    disks, backlog, drift, errors = [], 0, [], []
    last_points = []
    upload, apply_ = None, None
    for v in vols:
        ts = [t for t in mine if t["source_volume"] == v["source_volume"]]
        applied = [t for t in ts if t.get("state") == "applied"]
        last_applied = applied[-1] if applied else None
        pending = [t for t in ts if t.get("state") in ("completed", "in_progress")]
        backlog += len(pending)
        if ts and ts[-1].get("state") == "error":
            errors.append(v["source_volume"])
        if interval:
            if last_applied and last_applied.get("applied_at") and \
                    _secs(_t(last_applied["snapshot_at"]), _t(last_applied["applied_at"])) > interval:
                drift.append(v["source_volume"])
            elif any(_secs(_t(t["snapshot_at"]), now) > interval for t in pending if t.get("snapshot_at")):
                drift.append(v["source_volume"])
        if last_applied:
            last_points.append(_t(last_applied["snapshot_at"]))
        disks.append({"disk": v.get("disk"), "source_volume": v["source_volume"],
                      "target_volume": v.get("target_volume"),
                      "last_applied_at": last_applied["snapshot_at"] if last_applied else None,
                      "backlog": len(pending), "error": v["source_volume"] in errors})
        done = [t for t in ts if t.get("backup_started_at") and t.get("backup_done_at")]
        if done:
            t = done[-1]
            dur = _secs(_t(t["backup_started_at"]), _t(t["backup_done_at"]))
            if dur > 0 and (upload is None or t["snapshot_at"] > upload["at"]):
                upload = {"at": t["snapshot_at"], "bytes": int(t.get("newly_uploaded") or 0), "seconds": dur,
                          "bps": int(t.get("newly_uploaded") or 0) / dur}
        if last_applied and last_applied.get("applied_at"):
            t = last_applied
            start = t.get("resync_at") or t.get("backup_done_at")
            if start:
                dur = _secs(_t(start), _t(t["applied_at"]))
                if dur > 0 and (apply_ is None or t["snapshot_at"] > apply_["at"]):
                    apply_ = {"at": t["snapshot_at"], "bytes": int(t.get("newly_uploaded") or 0),
                              "seconds": dur, "bps": int(t.get("newly_uploaded") or 0) / dur}
    complete = bool(vols) and len(last_points) == len(vols)
    oldest = min(last_points) if complete else None
    running, stopped_at = _running_info(vm_running, uid)
    up_to_date = bool(complete and not running and stopped_at and stopped_at <= oldest)
    rpo = None
    if complete:
        rpo = 0 if up_to_date else int(_secs(oldest, now))
    objective = plan.get("rpo_objective_s") or 0
    return {"disks": disks, "backlog": backlog, "drift": drift, "errors": errors,
            "last_applied_at": iso(oldest) if oldest else None, "rpo_held_s": rpo,
            "up_to_date": up_to_date, "exceeded": bool(rpo is not None and objective and rpo > objective),
            "skew_s": int(_secs(min(last_points), max(last_points))) if len(last_points) > 1 else 0,
            "throughput": {"upload": upload, "apply": apply_}}


def _resource_eval(plan, resource, volumes, ticks, now, vm_running, health=None):
    ev = _vm_eval(plan, resource["resource_uid"], volumes, ticks, now, vm_running)
    stored = resource.get("state") or plan.get("state") or "draft"
    health = health or {}
    reasons = []
    state = stored
    seen, known = health.get("vm_generation"), resource.get("vm_generation")
    spec_changed = seen is not None and known is not None and int(seen) != int(known)
    if stored == "protected":
        if health.get("source_reachable") is False:
            state, reasons = "source-unreachable", ["source-unreachable"]
        elif ev["errors"] or health.get("standby_degraded"):
            state = "degraded"
            reasons = (["backup-error"] if ev["errors"] else []) + \
                      (["standby-degraded"] if health.get("standby_degraded") else [])
        elif ev["drift"] or spec_changed:
            state = "drifting"
            reasons = (["rpo-drift"] if ev["drift"] else []) + (["plan-drift"] if spec_changed else [])
        elif ev["exceeded"]:
            state, reasons = "lagging", ["rpo-exceeded"]
    ev["state"], ev["reasons"] = state, reasons
    return ev


def resource_state(plan, resource, volumes, ticks, now, vm_running, health=None):
    """État d'une ressource (RESOURCE_STATES). L'état rangé dit où en est
    le cycle de vie ; pour une ressource protégée, la lecture des passages
    affine en `lagging` (RPO tenu au-delà de l'objectif), `drifting` (un
    passage plus long qu'un pas, ou spec changée : `health["vm_generation"]`
    différent de celui vu à la protection), `degraded` (sauvegarde en
    erreur, ou `health["standby_degraded"]`), `source-unreachable`
    (`health["source_reachable"] is False`)."""
    return _resource_eval(plan, resource, volumes, ticks, now, vm_running, health)["state"]


def _role_rank(role):
    return ROLES.index(role) if role in ROLES else -1


def _action(name, role):
    need = ACTION_ROLES[name]
    if role is None:
        return {"name": name, "role": need, "allowed": True, "reason": ""}
    ok = _role_rank(role) >= _role_rank(need)
    return {"name": name, "role": need, "allowed": ok, "reason": "" if ok else "role"}


def allowed_actions(plan, resource, side, role=None):
    """Actions d'une ressource selon son état (`resource["state"]`, calculé)
    et le côté d'où l'on regarde (`source`, `target`, ou `any` pour la vue de
    tous les plans), chacune avec le rôle requis. Avec `role`, `allowed` dit
    si la personne peut la lancer (l'action reste listée)."""
    state = resource.get("state") or "draft"
    names = list(STATE_ACTIONS.get(state, ()))
    if state == "error" and resource.get("error_origin") == "failing_over":
        names.insert(1, "failover")
    paused = bool((plan.get("options") or {}).get("paused"))
    out = []
    for n in names:
        if n == _PAUSE:
            n = "resume" if paused else "pause"
        if n in _TARGET_ONLY and side == "source" and state != "source-unreachable":
            continue
        out.append(n)
    out.sort(key=ACTION_ORDER.index)
    return [_action(n, role) for n in out]


def plan_actions(plan, side, resources=(), role=None):
    """Actions du plan entier, dérivées des états de ses ressources : une
    action est rendue si elle vaut pour au moins une ressource (`count`).
    `delete` s'ajoute pour un plan `draft` ou `unprotected`."""
    counts = {}
    for r in resources:
        for a in allowed_actions(plan, r, side):
            counts[a["name"]] = counts.get(a["name"], 0) + 1
    if not resources and plan.get("state") == "draft":
        counts.update({"measure-start": 0, "protect": 0})
    if plan.get("state") in ("draft", "unprotected"):
        counts["delete"] = len(list(resources))
    out = []
    for n in sorted(counts, key=ACTION_ORDER.index):
        a = _action(n, role)
        a["count"] = counts[n]
        out.append(a)
    return out


def plan_status(plan, resources, volumes, ticks, now, vm_running, health=None):
    """État calculé d'un plan : RPO tenu par VM (âge du point appliqué le
    plus ancien de ses disques), arriéré, dérive, débits des deux trajets,
    écart entre disques, prochain passage. `health` : {uid: santé} (voir
    `resource_state`)."""
    vms = []
    for r in resources:
        ev = _resource_eval(plan, r, volumes, ticks, now, vm_running, (health or {}).get(r["resource_uid"]))
        vms.append({"uid": r["resource_uid"], "namespace": r.get("namespace"), "name": r.get("name"),
                    "state": ev["state"], "reasons": ev["reasons"], "rpo_held_s": ev["rpo_held_s"],
                    "last_applied_at": ev["last_applied_at"], "up_to_date": ev["up_to_date"],
                    "exceeded": ev["exceeded"], "backlog": ev["backlog"], "drift": bool(ev["drift"]),
                    "skew_s": ev["skew_s"], "throughput": ev["throughput"], "disks": ev["disks"]})
    held = [v["rpo_held_s"] for v in vms if v["rpo_held_s"] is not None]
    by_state = {}
    for v in vms:
        by_state[v["state"]] = by_state.get(v["state"], 0) + 1
    return {"plan_id": plan.get("id"), "state": plan.get("state"),
            "rpo_objective_s": plan.get("rpo_objective_s"),
            "rpo_held_s": max(held) if held else None,
            "exceeded": any(v["exceeded"] for v in vms),
            "backlog": sum(v["backlog"] for v in vms),
            "drift": any(v["drift"] for v in vms),
            "skew_s": max((v["skew_s"] for v in vms), default=0),
            "interval_s": plan.get("interval_s"),
            "next_tick_at": next_tick_at(plan.get("interval_s"), now)
            if plan.get("state") in ("measuring", "protecting", "protected") else None,
            "resources_by_state": by_state, "vms": vms}


def _last_events(events, n=3):
    evs = sorted(events or [], key=lambda e: (e.get("ts") or "", e.get("id") or 0), reverse=True)
    return evs[:n]


def resource_row(plan, resource, side, volumes, ticks, events, now, vm_running, role=None, health=None):
    """La ligne d'une ressource, forme unique de la liste des plans, de la
    liste par hôte et de la ligne de commande. `side` : côté de l'hôte qui
    regarde (`source`, `target`) ou `any` (vue de tous les plans)."""
    ev = _resource_eval(plan, resource, volumes, ticks, now, vm_running, health)
    r = dict(resource, state=ev["state"])
    if side == "target":
        peer, host_kind = plan.get("source_cluster"), plan.get("target_host_kind") or "harvester"
    else:
        peer, host_kind = plan.get("target_cluster"), plan.get("source_host_kind") or "harvester"
    if side == "any":
        peer = None
    return {"plan": plan.get("id"), "plan_name": plan.get("name"),
            "resource_kind": resource.get("resource_kind") or "vm",
            "resource_uid": resource["resource_uid"], "namespace": resource.get("namespace"),
            "name": resource.get("name"), "side": side, "host_kind": host_kind, "peer_host": peer,
            "state": ev["state"], "reasons": ev["reasons"], "state_since": resource.get("state_since"),
            "rpo_held_s": ev["rpo_held_s"], "rpo_objective_s": plan.get("rpo_objective_s"),
            "last_applied_at": ev["last_applied_at"],
            "last_events": _last_events([e for e in events or []
                                         if e.get("resource_uid") == resource["resource_uid"]]),
            "actions": allowed_actions(plan, r, side, role)}


def plan_row(plan, resources, volumes, ticks, events, now, vm_running, role=None, health=None, side="any"):
    """La ligne d'un plan (liste des plans, `plan list --json`), avec les
    lignes de ses ressources produites par `resource_row`."""
    health = health or {}
    rows = [resource_row(plan, r, side, volumes, ticks, events, now, vm_running, role,
                         health.get(r["resource_uid"])) for r in resources]
    held = [r["rpo_held_s"] for r in rows if r["rpo_held_s"] is not None]
    by_state = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    computed = [dict(r, state=row["state"]) for r, row in zip(resources, rows)]
    return {"id": plan.get("id"), "name": plan.get("name"),
            "source_host": plan.get("source_cluster"), "target_host": plan.get("target_cluster"),
            "source_host_kind": plan.get("source_host_kind") or "harvester",
            "target_host_kind": plan.get("target_host_kind") or "harvester",
            "state": plan.get("state"), "generation": plan.get("generation"),
            "active_side": plan.get("active_side"),
            "rpo_objective_s": plan.get("rpo_objective_s"), "rpo_held_s": max(held) if held else None,
            "interval_s": plan.get("interval_s"),
            "next_tick_at": next_tick_at(plan.get("interval_s"), now)
            if plan.get("state") in ("measuring", "protecting", "protected") else None,
            "resources_by_state": by_state,
            "last_events": _last_events([e for e in events or [] if e.get("plan_id") in (None, plan.get("id"))]),
            "actions": plan_actions(plan, side, computed, role), "resources": rows}


# ---------------------------------------------------------------------------
# Manifestes
# ---------------------------------------------------------------------------

def _lh_meta(name, labels):
    md = {"name": name, "namespace": LONGHORN_NS}
    if labels:
        md["labels"] = dict(labels)
    return md


def job_label(job_name):
    """Étiquette qui attache un volume à une tâche récurrente."""
    return JOB_LABEL_PREFIX + job_name, "enabled"


def recurring_job(name, cron, retain, concurrency, plan_id, vm_uid, generation=1):
    """Tâche de sauvegarde d'une VM. `groups: []` : jamais appliquée par
    groupe ; `labels` recopiées par Longhorn sur chaque `Backup` (pour
    retrouver nos sauvegardes)."""
    if not RETAIN_MIN <= int(retain) <= RETAIN_MAX:
        raise ValueError(f"retain must be between {RETAIN_MIN} and {RETAIN_MAX}")
    if int(concurrency) < 1:
        raise ValueError("concurrency must be at least 1")
    if len(name) > JOB_NAME_MAX:
        raise ValueError("recurring job name too long")
    labels = plan_labels(plan_id, generation, vm_uid)
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "RecurringJob", "metadata": _lh_meta(name, labels),
            "spec": {"name": name, "task": "backup", "cron": cron, "retain": int(retain),
                     "concurrency": int(concurrency), "groups": [], "labels": dict(labels)}}


def backing_image_restore(name, checksum, backup_url, target_name="default", labels=None,
                          concurrent_limit=2, min_copies=1):
    """Backing image restaurée depuis la cible de sauvegarde (Longhorn la
    sauvegarde avec la première sauvegarde du volume)."""
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "BackingImage", "metadata": _lh_meta(name, labels),
            "spec": {"sourceType": "restore",
                     "sourceParameters": {"backup-target-name": target_name, "backup-url": backup_url,
                                          "concurrent-limit": str(int(concurrent_limit))},
                     "checksum": checksum, "dataEngine": "v1", "minNumberOfCopies": int(min_copies)}}


_CLASS_PARAMS = ("dataLocality", "diskSelector", "nodeSelector", "staleReplicaTimeout")


def standby_volume(name, from_backup, size, access_mode, migratable, replicas, backing_image=None,
                   class_params=None, labels=None):
    """Volume de secours : restauré de façon incrémentale, sans frontal.
    Placement recopié des paramètres de la classe cible."""
    if len(name) > VOLUME_NAME_MAX:
        raise ValueError("volume name too long")
    spec = {"Standby": True, "fromBackup": from_backup, "size": str(int(size)),
            "numberOfReplicas": int(replicas), "accessMode": access_mode,
            "migratable": bool(migratable), "dataEngine": "v1", "frontend": ""}
    params = class_params or {}
    for k in _CLASS_PARAMS:
        v = params.get(k)
        if v in (None, ""):
            continue
        if k in ("diskSelector", "nodeSelector"):
            spec[k] = [x.strip() for x in str(v).split(",") if x.strip()] if isinstance(v, str) else list(v)
        elif k == "staleReplicaTimeout":
            spec[k] = int(v)
        else:
            spec[k] = v
    if backing_image:
        spec["backingImage"] = backing_image
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume", "metadata": _lh_meta(name, labels),
            "spec": spec}


def snapshot(volume, name, labels=None):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot", "metadata": _lh_meta(name, labels),
            "spec": {"volume": volume, "createSnapshot": True}}


def backup(volume, snapshot_name, backup_volume=None, target="default", name=None, labels=None):
    """Sauvegarde d'un instantané (sauvegarde finale de la bascule)."""
    md_labels = {"backup-volume": backup_volume or volume, "backup-target": target}
    md_labels.update(labels or {})
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
            "metadata": _lh_meta(name or snapshot_name, md_labels), "spec": {"snapshotName": snapshot_name}}


def activation_patch():
    """Comme `VolumeManager.Activate` de longhorn-manager (manager/volume.go,
    l. 402 en v1.11.2, l. 448 en v1.12.1) : `Spec.Frontend` puis
    `Spec.Standby = false`. L'activation est définitive."""
    return {"spec": {"Standby": False, "frontend": "blockdev"}}


def static_pv(volume, size, mode, access_modes, storage_class, labels=None):
    md = {"name": volume}
    if labels:
        md["labels"] = dict(labels)
    return {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": md,
            "spec": {"capacity": {"storage": quantity(size)}, "volumeMode": mode,
                     "accessModes": list(access_modes), "persistentVolumeReclaimPolicy": "Retain",
                     "storageClassName": storage_class,
                     "csi": {"driver": LONGHORN_DRIVER, "volumeHandle": volume}}}


def static_pvc(name, namespace, volume, size, mode, access_modes, storage_class, image_id=None, labels=None):
    """PVC lié au PV statique. `image_id` seulement si l'image est mappée sur
    une image de la cible."""
    md = {"name": name, "namespace": namespace}
    if image_id:
        md["annotations"] = {IMAGE_ID_ANNOTATION: image_id}
    if labels:
        md["labels"] = dict(labels)
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": md,
            "spec": {"accessModes": list(access_modes), "volumeMode": mode, "storageClassName": storage_class,
                     "volumeName": volume, "resources": {"requests": {"storage": quantity(size)}}}}


def target_vm(source_vm, resource, mappings, options, claims, plan_id, generation=1):
    """La VM cible : `sanitize_vm` puis `retarget_vm` (réseaux, PVC, MAC selon
    `mac`), `firmware.uuid` recopié (`keep`) ou retiré (`rerun` : KubeVirt
    prend alors un nouvel `instance-id`, `cloudInitUUIDFromVMI`, et cloud-init
    rejoue), `runStrategy` selon `start`. Rend (vm, retraits)."""
    tns, tname = target_ref({"mappings": mappings}, resource)
    original = source_vm.get("spec") or {}
    vm, removed = vt.retarget_vm(vt.sanitize_vm(source_vm), name=tname, namespace=tns,
                                 networks=(mappings or {}).get("networks") or {}, claims=claims or {},
                                 secrets={}, keep_mac=(options or {}).get("mac", "keep") == "keep",
                                 transfer_id="")
    md = vm["metadata"]
    labels = {k: v for k, v in (md.get("labels") or {}).items() if k != vt.TRANSFER_LABEL}
    labels.update(plan_labels(plan_id, generation, resource["resource_uid"]))
    md["labels"] = labels
    domain = vm["spec"]["template"]["spec"].setdefault("domain", {})
    if (options or {}).get("cloud_init", "keep") == "keep":
        uid = resource.get("firmware_uuid") or firmware_uuid(source_vm)
        if not uid:
            raise ValueError("firmware uuid unknown: cloud-init would run again")
        domain.setdefault("firmware", {})["uuid"] = uid
    else:
        fw = domain.get("firmware") or {}
        fw.pop("uuid", None)
        if fw:
            domain["firmware"] = fw
        else:
            domain.pop("firmware", None)
    if (options or {}).get("start", "started") == "started":
        rs = original.get("runStrategy") or ("Always" if original.get("running") else "")
        vm["spec"]["runStrategy"] = rs if rs in ("Always", "RerunOnFailure") else "Always"
    else:
        vm["spec"]["runStrategy"] = "Halted"
    return vm, removed
