"""harvester-ops : décisions de la réplication asynchrone de VMs (v1.99.0).

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
import json
import math
import re
from datetime import timedelta, timezone

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
SKEW_TOLERANCE_S = 10

# Paires Longhorn vérifiées de bout en bout sur le banc (22-23/10/2026)
VERIFIED_LONGHORN_PAIRS = {((1, 11, 2), (1, 12, 1)), ((1, 12, 1), (1, 11, 2))}

# Délais de départ (secondes) des attentes du déroulé ; `barrier` vaut la
# moitié de l'intervalle (None ici). Surchargés par `--timeout` et par les
# options du plan.
TIMEOUTS = {"probe": 15, "stop": 600, "snapshot": 300, "final_backup": 6 * 3600,
            "resync_apply": 600, "initial_restore": 12 * 3600, "backing_image": 2 * 3600,
            "activate": 300, "bind": 120, "vm_start": 900, "unplanned_sync": 120,
            "unplanned_catchup": 600, "barrier": None,
            # rythme du suiveur (`replication_follow`), réglable à chaud par
            # plan : lecture rapide pendant un passage, santé, relecture de
            # la spec des VMs, délai laissé à la tâche pour démarrer ses
            # sauvegardes après l'échéance, fenêtre de lecture rapide (None :
            # min(intervalle, barrier + resync_apply)). Deux compteurs, pas
            # des secondes : intervalles sans sauvegarde avant qu'un disque
            # soit dit « ne sauvegarde jamais », tours injoignables de suite
            # avant que le cluster soit dit injoignable (hystérésis)
            "follow_poll": 3, "follow_health": 60, "follow_spec": 600, "follow_grace": 60,
            "follow_window": None, "follow_never_intervals": 2, "follow_down_rounds": 2,
            # échéance UNIQUE de tout retour arrière (protect, bascule avant
            # activation, mesure) : passée, plus rien n'est défait, le reste
            # est listé (`undo-timeout`) ; délai d'un appel kubectl (1 s à 1 h)
            "undo_total": 240, "client": 30}
TIMEOUT_MAX = 7 * 86400
CLIENT_TIMEOUT_MAX = 3600

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
    # un brouillon n'a rien posé sur un cluster : il se supprime (`delete`),
    # il ne se déprotège pas
    "draft": ("measuring", "protecting", "error"),
    "measuring": ("measuring", "draft", "protecting", "unprotecting", "error"),
    "protecting": ("protecting", "protected", "unprotecting", "error"),
    # protect relancé après un `plan-drift` (disque ajouté, taille changée)
    "protected": ("protecting", "failing_over", "unprotecting", "error"),
    # avant l'activation tout est réversible : retour à `protected`
    "failing_over": ("failing_over", "protected", "failed_over", "error"),
    "failed_over": ("unprotecting", "error"),
    "unprotecting": ("unprotecting", "unprotected", "error"),
    "unprotected": (),
    # depuis `error`, seules les sorties de `error_exits(origine)`
    "error": ("measuring", "protecting", "failing_over", "unprotecting"),
}
# Sorties de `error` en plus de celles de l'état d'origine.
# Bascule en échec (origine `failing_over`) : sans cela, la seule issue serait
# de relancer la bascule. On permet `unprotect`, au sens « abandonner la
# bascule » (décision 47) : une VM dont un volume a déjà été activé garde
# TOUT sur la cible (volumes, secours restants compris, PV, PVC, VM, secrets,
# carnet, backing images), listé dans `left` ; la suppression est un geste
# séparé et explicite. La déprotection retire la tâche de la source et ce
# qui, sur la cible, appartient aux VMs jamais activées. La VM source, arrêtée
# par la bascule, se redémarre à la main. Pas de `failing_over ->
# unprotecting` direct : une bascule en cours tient le verrou, on n'abandonne
# qu'une bascule arrêtée en erreur.
ERROR_EXTRA_EXITS = {"failing_over": ("unprotecting",)}
# Origine inconnue (ligne d'avant la colonne `error_origin`) : seule la
# déprotection, qui sait partir de n'importe quel état, est sûre.
ERROR_UNKNOWN_EXITS = ("unprotecting",)


def error_exits(origin):
    """États permis en sortie de `error`, selon l'état d'où l'on y est entré :
    l'origine elle-même (reprise de la commande) et ses propres transitions,
    restreintes à celles de `error`, plus ERROR_EXTRA_EXITS. Source unique du
    registre (`set_state`, `set_resource_state`) et des actions proposées."""
    if origin not in PLAN_TRANSITIONS or origin == "error":
        return ERROR_UNKNOWN_EXITS
    own = set(PLAN_TRANSITIONS[origin]) | {origin} | set(ERROR_EXTRA_EXITS.get(origin, ()))
    return tuple(s for s in PLAN_TRANSITIONS["error"] if s in own)


def transition_allowed(current, new, origin=None):
    """`current -> new` permis ? Depuis `error`, selon `error_exits(origin)`."""
    if current == "error":
        return new in error_exits(origin)
    return new in PLAN_TRANSITIONS.get(current, ())


# États d'où part chaque commande (elle reprend depuis son état intermédiaire)
COMMAND_FROM = {
    "measure-start": ("draft", "measuring", "error"),
    "measure-stop": ("measuring",),
    "protect": ("draft", "measuring", "protecting", "protected", "error"),
    "failover": ("protected", "failing_over", "error"),
    "unprotect": tuple(s for s in PLAN_STATES if s not in ("draft", "failing_over", "unprotected")),
}

RESOURCE_STATES = ("draft", "measuring", "protecting", "protected", "lagging", "drifting",
                   "degraded", "source-unreachable", "failing_over", "failed_over",
                   "unprotecting", "unprotected", "error")
# rangés en base ; les quatre autres sont calculés à la lecture
STORED_RESOURCE_STATES = PLAN_STATES
_FOLLOWED = ("protected", "lagging", "drifting", "degraded", "source-unreachable")
# ressources dont rien n'est plus répliqué : ni RPO, ni dépassement, ni dérive
NOT_REPLICATING = ("failed_over", "unprotecting", "unprotected")

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
# État du plan où mène chaque action (celles qui en changent) : c'est par lui
# que les actions proposées depuis `error` sont dérivées des transitions.
ACTION_STATE = {
    "measure-start": "measuring", "measure-stop": "draft", "protect": "protecting",
    "failover": "failing_over", "failover-unplanned": "failing_over",
    "unprotect": "unprotecting", "unprotect-delete-backups": "unprotecting",
}
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
    # bascule interrompue : la reprendre, ou la finir en imprévue si la
    # source a disparu entre-temps
    "failing_over": ("failover", "failover-unplanned"),
    "failed_over": ("unprotect", "unprotect-delete-backups"),
    "unprotecting": ("unprotect", "unprotect-delete-backups"),
    "unprotected": (),
    # `error` : dérivé de l'origine (`error_actions`), jamais d'une liste fixe
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
        if kind in ("networks", "images", "target_names", "claims"):
            _ref(s, kind)
            if kind in ("target_names", "claims"):
                # clé `namespace/nom` (lue ainsi par `target_ref` et
                # `target_claim`), valeur : un nom seul, dans le namespace cible
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
        if not 1 <= s <= (CLIENT_TIMEOUT_MAX if k == "client" else TIMEOUT_MAX):
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
    """Une tâche par VM : `hr-<8 hex du plan>-<8 hex d'empreinte de la VM>`.

    La partie VM est une empreinte de l'uid, pas son début : deux uids qui
    partagent leurs 8 premiers caractères donnent deux noms. Une collision
    d'empreinte reste possible ; les étiquettes du plan la détectent
    (`collisions`, `recurring-job-taken`)."""
    return f"hr-{_hex8(plan_id[3:] if str(plan_id).startswith('rp-') else plan_id)}-{_digest(str(vm_uid))[:8]}"


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
                pid, uid, v["disk"], plan["generation"], r["name"]))
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


# longhorn-manager types/types.go : tâches qui produisent un `Backup`
# (`RecurringJobTypeBackup`, `RecurringJobTypeBackupForceCreate`)
BACKUP_TASKS = ("backup", "backup-force-create")
DEFAULT_GROUP = "default"


def backup_membership(lh_volume, jobs, own_job=None):
    """Ce que la pose de notre étiquette de tâche change aux tâches récurrentes
    existantes de la source.

    `jobs` : {nom: {"task", "groups": [...]}} (RecurringJob de la source).
    Longhorn range dans le groupe `default` un volume qui ne porte AUCUNE
    étiquette de tâche ni de groupe (appartenance implicite) ; son mutateur
    pose aussi souvent l'étiquette explicite
    `recurring-job-group.longhorn.io/default: enabled`.

    * implicite : notre étiquette sortirait le volume du groupe, ses tâches
      (`lost_jobs`) cesseraient de le couvrir sans bruit. On recommande de
      poser l'étiquette du groupe explicitement (`keep_default_label`) dès
      que le groupe a une tâche : la protection existante de l'exploitant
      (souvent une rétention plus longue) ne disparaît pas en silence.
    * explicite (étiquette du groupe, autres groupes, tâches nommées) : rien
      n'est retiré, mais les autres tâches de sauvegarde continuent
      d'écrire dans le même BackupVolume (`double_backup_jobs`).

    Rend {"implicit", "keep_default_label", "lost_jobs",
    "double_backup_jobs"} ; la tâche 3 applique `keep_default_label`."""
    labels = ((lh_volume or {}).get("metadata") or {}).get("labels") or {}
    jobs = {n: j for n, j in (jobs or {}).items() if n != own_job}
    enabled = {k for k, v in labels.items() if v == "enabled"}
    named = {k[len(JOB_LABEL_PREFIX):] for k in enabled if k.startswith(JOB_LABEL_PREFIX)}
    groups = {k[len(JOB_GROUP_LABEL_PREFIX):] for k in enabled if k.startswith(JOB_GROUP_LABEL_PREFIX)}
    named.discard(own_job)
    implicit = not any(k.startswith((JOB_LABEL_PREFIX, JOB_GROUP_LABEL_PREFIX)) for k in labels)
    default_jobs = sorted(n for n, j in jobs.items() if DEFAULT_GROUP in (j.get("groups") or []))
    lost = default_jobs if implicit else []
    keep = bool(lost) or DEFAULT_GROUP in groups
    covering_groups = groups | ({DEFAULT_GROUP} if keep else set())
    double = sorted(n for n, j in jobs.items() if j.get("task") in BACKUP_TASKS
                    and (n in named or covering_groups & set(j.get("groups") or [])))
    return {"implicit": implicit, "keep_default_label": keep, "lost_jobs": lost,
            "double_backup_jobs": double}


def volume_findings(lh_volume, pvc, sc, jobs=None, own_job=None):
    """Constats d'éligibilité d'un disque. `jobs` : tâches récurrentes de la
    source (voir `backup_membership`) ; `own_job` : la nôtre, ignorée."""
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
    if jobs:
        m = backup_membership(lh_volume, jobs, own_job)
        if m["lost_jobs"] or m["double_backup_jobs"]:
            out.append(_finding("backup-job-membership", "warn", volume=vol, claim=claim, **m))
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
    """`metadata.generation` de la VM : simple préfiltre de la dérive (même
    valeur = spec inchangée, rien à recalculer). Elle bouge aussi au
    démarrage et à l'arrêt (`runStrategy` est dans la spec) : la dérive se
    décide sur `vm_spec_digest`. Jamais `resourceVersion`, qui change à
    chaque annotation ou mise à jour d'état."""
    g = ((vm or {}).get("metadata") or {}).get("generation")
    try:
        return int(g) if g is not None else None
    except (TypeError, ValueError):
        return None


def firmware_uuid(vm):
    return (((vt._tspec(vm).get("domain") or {}).get("firmware")) or {}).get("uuid") or ""


_CLOUD_INIT_KINDS = ("cloudInitNoCloud", "cloudInitConfigDrive")


def _volume_source(vol):
    """Source d'un volume de VM, réduite à ce qui touche la réplication."""
    for kind in ("persistentVolumeClaim", "dataVolume"):
        if kind in vol:
            return {kind: (vol[kind] or {}).get("claimName") or (vol[kind] or {}).get("name")}
    for kind in _CLOUD_INIT_KINDS:
        if kind in vol:
            # la source de cloud-init : secret référencé, ou empreinte du
            # contenu en ligne (jamais le contenu lui-même)
            ci = vol[kind] or {}
            return {kind: {k: (v.get("name") if isinstance(v, dict) else _digest(str(v))[:16])
                           for k, v in sorted(ci.items())}}
    return {"other": sorted(vol)}


def vm_spec_digest(vm, disks=None):
    """Empreinte des parties de la spec qui comptent pour la réplication :
    volumes et leur source (PVC, cloud-init), disques (nom, bus), tailles et
    classes des disques (`disks` : [{"disk", "size", "storage_class"}], lus
    sur les PVC), réseaux, interfaces (nom, modèle, MAC, liaison),
    `firmware.uuid`. Ignore `runStrategy`/`running`, annotations et
    étiquettes : démarrer, arrêter ou classer la VM n'est pas une dérive.
    Rangée à la protection (`plan_resources.vm_spec_digest`), comparée par le
    suiveur (`health["vm_spec_digest"]`)."""
    tspec = vt._tspec(vm or {})
    devices = ((tspec.get("domain") or {}).get("devices")) or {}
    parts = {
        "volumes": sorted(([v.get("name"), _volume_source(v)] for v in tspec.get("volumes") or []),
                          key=lambda x: str(x[0])),
        "disks": sorted([d.get("name"), sorted((k, (v or {}).get("bus") if isinstance(v, dict) else v)
                                               for k, v in d.items() if k not in ("name", "bootOrder"))]
                        for d in devices.get("disks") or []),
        "sizes": sorted([str(d.get("disk")), int(d.get("size") or 0), d.get("storage_class") or ""]
                        for d in disks or []),
        "networks": sorted([n.get("name"), sorted((k, (v or {}).get("networkName") if isinstance(v, dict) else v)
                                                  for k, v in n.items() if k != "name")]
                           for n in tspec.get("networks") or []),
        "interfaces": sorted([i.get("name"), i.get("model") or "", (i.get("macAddress") or "").lower(),
                              sorted(k for k in i if k not in ("name", "model", "macAddress", "bootOrder"))]
                             for i in devices.get("interfaces") or []),
        "firmware_uuid": firmware_uuid(vm or {}),
    }
    return _digest(json.dumps(parts, sort_keys=True, default=str))[:32]


def spec_drifted(known, seen):
    """La spec a-t-elle changé depuis la protection ? `known` (rangé) et
    `seen` (relu) : {"vm_generation", "vm_spec_digest"}. La génération sert
    de préfiltre (égale : rien n'a changé) ; sinon l'empreinte décide. Sans
    empreinte des deux côtés, pas de dérive affirmée."""
    kg, sg = known.get("vm_generation"), seen.get("vm_generation")
    if kg is not None and sg is not None and int(kg) == int(sg):
        return False
    kd, sd = known.get("vm_spec_digest"), seen.get("vm_spec_digest")
    return bool(kd and sd and kd != sd)


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
        # volume. Sa classe propre (`lh-<uid>`, liée à l'image source) n'existe
        # pas sur la cible : le PVC prend la classe sur laquelle l'image a été
        # bâtie (`image_class`, annotation `harvesterhci.io/storageClassName`
        # de l'image, posée par le mutateur de Harvester), passée par la
        # correspondance des classes du plan ; à défaut, la classe par défaut
        # de la cible (v1.99.0)
        base = disk.get("image_class")
        if base:
            mb = (maps.get("storage_classes") or {}).get(base, base)
            if mb in classes:
                return mb, _finding("image-unmapped", "warn", image=image, storage_class=mb,
                                    image_class=base)
        d = dst.get("default_storage_class")
        if d in classes:
            return d, _finding("image-unmapped", "warn", image=image, storage_class=d)
    return None, _finding("storage-class-unmapped", "block", storage_class=sc, mapped=m)


PHASES = ("protect", "failover", "failover-unplanned")


def _version(v):
    """`v1.12.1` -> (1, 12, 1) ; None si illisible ou absente (version
    inconnue : jamais comparée)."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def _capacity(needed, classes):
    """Place sur la cible. `needed` : {classe: octets}. L'allouable de
    storage_room est la place du n-ième meilleur nœud (n = répliques) :
    chaque réplique y tient, la somme des tailles se compare donc à lui.

    Plusieurs classes puisent dans les mêmes disques : chacune ne dispose pas
    de tout l'allouable. Quand les faits donnent le groupe de disques d'une
    classe (`pool`, par exemple ses sélecteurs de disques et de nœuds), les
    classes d'un même groupe sont comptées ensemble contre le plus petit de
    leurs allouables (celui de la classe aux répliques les plus nombreuses :
    prudent). Sans ce renseignement pour toutes les classes en jeu, chaque
    classe est comptée seule et `capacity-shared-pool` avertit que la place
    peut manquer quand même."""
    out = []
    eff = {}
    for sc in sorted(needed):
        info = classes.get(sc) or {}
        alloc = int(info.get("allocatable") or 0)
        if not alloc and info.get("degraded_allocatable"):
            out.append(_finding("replicas-degraded", "warn", storage_class=sc,
                                replicas=info.get("replicas"), nodes=info.get("nodes")))
            alloc = int(info["degraded_allocatable"])
        eff[sc] = alloc
    used = sorted(sc for sc in needed if needed[sc])
    pools = {sc: (classes.get(sc) or {}).get("pool") for sc in used}
    if len(used) > 1 and all(pools.values()):
        groups = {}
        for sc in used:
            groups.setdefault(pools[sc], []).append(sc)
    else:
        groups = {sc: [sc] for sc in used}
        if len(used) > 1:
            out.append(_finding("capacity-shared-pool", "warn", storage_classes=used))
    for pool, members in sorted(groups.items()):
        n = sum(needed[sc] for sc in members)
        alloc = min(eff[sc] for sc in members)
        if n > alloc:
            facts = {"needed": n, "allocatable": alloc,
                     "needed_total": sum(needed[sc] * int((classes.get(sc) or {}).get("replicas") or 1)
                                         for sc in members)}
            if len(members) == 1:
                facts["storage_class"] = members[0]
            else:
                facts.update(storage_classes=members, pool=pool)
            out.append(_finding("capacity-short", "block", **facts))
    return out


def check(src, dst, plan):
    """Contrôle d'un plan : liste de constats {code, level, facts}, blocages
    d'abord.

    `src` : {"cluster", "reachable", "backup_target" (réglage brut),
    "backup_target_available", "longhorn_version", "backing_images": {nom:
    {"checksum", "size"}}, "jobs": {nom: {"task", "groups"}} (tâches
    récurrentes, voir `backup_membership`), "recurring_jobs": {nom:
    étiquettes}, "vms": [{"uid", "namespace", "name", "vm", "disks":
    [{"disk", "claim", "size", "storage_class", "image", "image_class",
    "lh_volume", "pvc", "sc"}]}]}. `image_class` : classe sur laquelle l'image
    d'un disque d'image a été bâtie (voir `_target_class`).
    `dst` : {"cluster", "reachable", "denied", "backup_target",
    "backup_target_available", "longhorn_version", "networks": [...],
    "namespaces": [...], "storage_classes": {nom: {"replicas", "allocatable",
    "degraded_allocatable", "nodes", "pool"}}, "default_storage_class",
    "images": {"ns/nom": {"storage_class"}}, "backing_images": {nom:
    {"checksum"}}, "macs": {mac: "ns/vm"}, "volumes"|"carnets"|"pvcs"|"vms":
    {nom: étiquettes}, "through_rancher"}.
    `plan` : plan normalisé avec `id`, `generation`, et `phase` :
    `protect`, `failover` (les deux sites vivants) ou `failover-unplanned`
    (source perdue : ses faits manquent, les VMs viennent du carnet ; ni la
    source injoignable, ni ce qui se lit sur elle ne bloque, et l'éligibilité
    des volumes, contrôlée à la protection, n'est pas refaite)."""
    phase = plan.get("phase", "protect")
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase}")
    unplanned = phase == "failover-unplanned"
    if dst.get("denied"):
        return [_finding("target-denied", "block", cluster=dst.get("cluster"))]
    if not dst.get("reachable"):
        return [_finding("target-unreachable", "block", cluster=dst.get("cluster"))]
    if not unplanned and not src.get("reachable"):
        return [_finding("source-unreachable", "block", cluster=src.get("cluster"))]
    pid, gen = plan["id"], plan["generation"]
    out = []
    vms = src.get("vms") or []
    if not vms:
        out.append(_finding("no-vm", "block"))

    s_bt, d_bt = vt.normalize_backup_target(src.get("backup_target")), \
        vt.normalize_backup_target(dst.get("backup_target"))
    if unplanned:
        if not d_bt:
            out.append(_finding("backup-target-differs", "block"))
    elif not s_bt or not d_bt or tuple(s_bt) != tuple(d_bt):
        out.append(_finding("backup-target-differs", "block"))
    for side, facts in (("source", src), ("target", dst)):
        if side == "source" and unplanned:
            continue
        if not facts.get("backup_target_available"):
            out.append(_finding("backup-target-unavailable", "block", side=side,
                                cluster=facts.get("cluster")))

    if not unplanned:
        sv, dv = _version(src.get("longhorn_version")), _version(dst.get("longhorn_version"))
        versions = {"source": src.get("longhorn_version"), "target": dst.get("longhorn_version")}
        if sv and dv and (sv, dv) in VERIFIED_LONGHORN_PAIRS:
            out.append(_finding("longhorn-pair-verified", "ok", **versions))
        else:
            out.append(_finding("longhorn-pair-unverified", "warn", unknown=not (sv and dv), **versions))
            if sv and dv and dv < sv:
                out.append(_finding("longhorn-target-older", "warn", **versions))

    maps = plan.get("mappings") or {}
    opts = plan.get("options") or {}
    nets = maps.get("networks") or {}
    needed, images_seen = {}, set()
    src_bi, dst_bi = src.get("backing_images") or {}, dst.get("backing_images") or {}
    dst_volumes = dst.get("volumes") or {}
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
        own_job = recurring_job_name(pid, uid)
        for d in vm.get("disks") or []:
            if not unplanned:
                out += volume_findings(d.get("lh_volume"), d.get("pvc"), d.get("sc"),
                                       src.get("jobs") or {}, own_job)
            volumes.append({"vm_uid": uid, "disk": d["disk"], "claim": d["claim"]})
            sc, f = _target_class(d, plan, dst)
            if f:
                out.append(f)
            if not sc:
                continue
            # un volume de secours déjà à nous (relance après l'ajout d'un
            # disque) occupe déjà sa place : il n'en demande pas davantage
            standby = target_volume_name(pid, uid, d["disk"], gen, vm["name"])
            if not unplanned and not owned_by(dst_volumes.get(standby), pid):
                needed[sc] = needed.get(sc, 0) + int(d.get("size") or 0)
            bi = ((d.get("lh_volume") or {}).get("spec") or {}).get("backingImage")
            if bi and bi not in images_seen:
                images_seen.add(bi)
                there, mine = dst_bi.get(bi), src_bi.get(bi) or {}
                if there is None:
                    out.append(_finding("backing-image-restore", "ok", backing_image=bi))
                    if not unplanned:
                        needed[sc] = needed.get(sc, 0) + int(mine.get("size") or 0)
                elif (mine or not unplanned) and there.get("checksum") != mine.get("checksum"):
                    out.append(_finding("backing-image-conflict", "block", backing_image=bi))
                else:
                    out.append(_finding("backing-image-reuse", "ok", backing_image=bi))
        if opts.get("mac", "keep") == "keep":
            taken = dst.get("macs") or {}
            clash = sorted(m for m in inv.get("macs") or [] if m in taken and taken[m] != f"{tns}/{tname}")
            if clash:
                out.append(_finding("mac-in-use", "warn" if phase == "protect" else "block",
                                    vm=f"{vm['namespace']}/{vm['name']}", macs=", ".join(clash),
                                    vms=", ".join(sorted({taken[m] for m in clash}))))
        if opts.get("cloud_init", "keep") == "keep" and not (vm.get("firmware_uuid") or firmware_uuid(obj)):
            out.append(_finding("firmware-uuid-unknown", "block", vm=f"{vm['namespace']}/{vm['name']}"))
        if inv.get("devices"):
            out.append(_finding("devices-removed", "warn", vm=f"{vm['namespace']}/{vm['name']}",
                                devices=list(inv["devices"])))
        if inv.get("node_affinity"):
            out.append(_finding("node-affinity-removed", "warn", vm=f"{vm['namespace']}/{vm['name']}"))

    out += _capacity(needed, dst.get("storage_classes") or {})

    if opts.get("cloud_init") == "rerun":
        out.append(_finding("cloud-init-rerun", "warn"))
    if dst.get("through_rancher"):
        out.append(_finding("target-through-rancher", "warn", cluster=dst.get("cluster")))
    facts = dict(dst)
    facts["recurring_jobs"] = {} if unplanned else (src.get("recurring_jobs") or {})
    out += collisions(facts, plan, resources, volumes, "protect" if phase == "protect" else "failover")
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
    """Prochaine échéance du cron, strictement après `now`, en UTC (l'heure
    du gestionnaire Longhorn) ; rendue sans microsecondes (`...:SSZ`). Les
    pas divisent le jour : l'échéance est un multiple du pas compté depuis
    minuit UTC."""
    if not interval_s:
        return None
    now = _t(now).astimezone(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    k = math.floor(_secs(midnight, now) / interval_s) + 1
    return iso(midnight + timedelta(seconds=k * interval_s))


class IntervalChoice(tuple):
    """(pas, rpo_atteignable, tenable), avec `.reason` quand intenable, et
    `.send_s` / `.apply_s` estimés pour ce pas."""

    def __new__(cls, step, rpo, tenable, reason=None, send_s=None, apply_s=None):
        self = super().__new__(cls, (step, rpo, tenable))
        self.reason, self.send_s, self.apply_s = reason, send_s, apply_s
        return self

    step = property(lambda self: self[0])
    rpo = property(lambda self: self[1])
    tenable = property(lambda self: self[2])


def _per_step(f):
    if callable(f):
        return f
    value = float(f or 0)
    return lambda step: value


def derive_interval(objective_s, send_for_step, apply_for_step, margin=DEFAULT_MARGIN):
    """Le plus grand pas tel que `pas + envoi(pas) + application(pas) <=
    objectif` et `envoi(pas) + application(pas) <= pas x (1 - marge)` (sinon
    le RPO dérive).

    `send_for_step` et `apply_for_step` : fonctions pas -> secondes (voir
    `StepEstimator`), car les blocs salis, donc les durées, croissent avec
    le pas. Un nombre vaut pour tous les pas (borne fixe).

    L'application mesurée va de la fin de la sauvegarde à l'application sur
    le volume de secours : elle comprend déjà la relecture que la console
    déclenche (9 à 10 s sur le banc, relecture comprise). Aucune relecture
    n'est donc ajoutée en plus.

    Intenable : le plus petit pas qui ne dérive pas, avec son RPO
    (`objective-too-short`) ; si tous dérivent, le plus grand pas
    (`drift-at-every-step`)."""
    send, apply_ = _per_step(send_for_step), _per_step(apply_for_step)

    def costs(step):
        s, a = float(send(step)), float(apply_(step))
        return s, a, step + s + a, s + a <= step * (1 - margin)

    for step in reversed(CRON_STEPS):
        s, a, rpo, steady = costs(step)
        if rpo <= objective_s and steady:
            return IntervalChoice(step, rpo, True, None, s, a)
    for step in CRON_STEPS:
        s, a, rpo, steady = costs(step)
        if steady:
            return IntervalChoice(step, rpo, False, "objective-too-short", s, a)
    step = CRON_STEPS[-1]
    s, a, rpo, _ = costs(step)
    return IntervalChoice(step, rpo, False, "drift-at-every-step", s, a)


def blocks_forecast(size, writes, block=BLOCK_SIZE, write_size=4096):
    """Blocs (et octets) qu'une sauvegarde enverra après `writes` écritures de
    `write_size` octets sur un volume de `size` octets. N = taille / bloc.

    * `scattered_estimate` : écritures dispersées au hasard, alignées sur
      leur taille (une écriture de 4 Kio ne chevauche jamais deux blocs de 2
      Mio) : N x (1 - (1 - k/N)^écritures), k = ceil(taille écrite / bloc).
      C'est la prévision vérifiée sur le banc (454 blocs pour 452 relevés).
    * `sequential` : écritures contiguës sans recouvrement : octets écrits /
      bloc, arrondi au-dessus. Des écritures qui se recouvrent peuvent
      envoyer moins : ce n'est pas un minimum absolu.
    * `worst_case` : chaque écriture non alignée touche au plus
      ceil((taille - 1) / bloc) + 1 blocs, aucune ne partage un bloc :
      min(N, écritures x ce nombre).

    Tous plafonnés à N ; l'estimation dispersée est gardée entre la valeur
    séquentielle et le pire cas."""
    n = max(1, int(size) // int(block))
    writes = max(0, int(writes))
    w, b = max(1, int(write_size)), int(block)
    k = min(n, math.ceil(w / b))
    k_unaligned = math.ceil((w - 1) / b) + 1
    sequential = min(n, math.ceil(writes * w / b))
    worst = min(n, writes * k_unaligned)
    scattered = n * (1 - (1 - k / n) ** writes) if writes else 0.0
    scattered = min(float(worst), max(scattered, float(sequential)))
    return {"blocks": n, "block": b, "bytes_written": writes * w,
            "scattered_estimate": scattered, "scattered_estimate_bytes": scattered * b,
            "sequential": sequential, "sequential_bytes": sequential * b,
            "worst_case": worst, "worst_case_bytes": worst * b}


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


class StepEstimator:
    """Durée d'un trajet (envoi ou application) pour un pas donné, tirée des
    passages mesurés : `est(pas) -> secondes`, `est.bytes(pas) -> octets`.

    Choix : on ne prolonge pas la formule des blocs (elle suppose des
    écritures dispersées au hasard, ce que la mesure ne dit pas), on part des
    octets réellement envoyés par passage (`newlyUploadDataSize`) :

    * pas plus petit que le pas mesuré : la valeur mesurée (p95). Les blocs
      salis ne font que croître avec le temps, elle majore donc le petit pas ;
    * pas plus grand : facteur f = ceil(pas / pas mesuré), p95 des sommes de
      f passages consécutifs (`aggregate_upper`) ; l'union des blocs salis est
      au plus la somme, c'est un majorant ;
    * moins de f passages mesurés : p95 d'un passage x f (même majorant,
      extrapolé) ;
    * jamais plus que `cap` (taille du volume).

    secondes = `fixed_s` + octets / `bps`. Pour l'application, `fixed_s`
    porte la part fixe mesurée (relecture déclenchée comprise)."""

    def __init__(self, measured_step, samples, bps, fixed_s=0.0, cap=None):
        self.measured_step = int(measured_step)
        self.samples = [int(x or 0) for x in samples or []]
        if self.measured_step <= 0:
            raise ValueError("measured step must be positive")
        if not self.samples:
            raise ValueError("at least one measured pass is needed")
        if not bps or bps <= 0:
            raise ValueError("throughput must be positive")
        self.bps, self.fixed_s, self.cap = float(bps), float(fixed_s), cap

    def bytes(self, step):
        factor = max(1, math.ceil(int(step) / self.measured_step))
        agg = aggregate_upper(self.samples, factor, self.cap)
        if agg["p95"] is not None:
            return agg["p95"]
        total = percentile(self.samples, 95) * factor
        return min(total, int(self.cap)) if self.cap else total

    def __call__(self, step):
        return self.fixed_s + self.bytes(step) / self.bps


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


def _tick_time(t):
    return t.get("snapshot_at") or t.get("backup_started_at") or t.get("backup_done_at") or ""


def _in_error(ticks):
    """Un volume est en erreur si un passage en erreur n'est suivi d'aucun
    passage réussi. Un passage en erreur sans aucune date (instantané
    refusé) compte comme le plus récent : il le reste jusqu'à la rétention."""
    ok = [_tick_time(t) for t in ticks if t.get("state") in ("completed", "applied")]
    for e in ticks:
        if e.get("state") != "error":
            continue
        te = _tick_time(e)
        if not te or not any(o > te for o in ok):
            return True
    return False


def _vm_eval(plan, uid, volumes, ticks, now, vm_running):
    gen = int(plan["generation"])
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
        if _in_error(ts):
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
    return {"disks": disks, "backlog": backlog, "drift": drift, "errors": errors, "complete": complete,
            "last_applied_at": iso(oldest) if oldest else None, "rpo_held_s": rpo,
            "up_to_date": up_to_date, "exceeded": bool(rpo is not None and objective and rpo > objective),
            "skew_s": int(_secs(min(last_points), max(last_points))) if len(last_points) > 1 else 0,
            "throughput": {"upload": upload, "apply": apply_}}


# Raison -> état calculé, et priorité des états (le premier l'emporte)
REASON_STATE = {"source-unreachable": "source-unreachable", "backup-error": "degraded",
                "standby-degraded": "degraded", "rpo-drift": "drifting", "plan-drift": "drifting",
                "rpo-exceeded": "lagging", "no-point": "lagging", "disk-never-backs-up": "degraded"}
COMPUTED_PRIORITY = ("source-unreachable", "degraded", "drifting", "lagging")


def _resource_eval(plan, resource, volumes, ticks, now, vm_running, health=None):
    ev = _vm_eval(plan, resource["resource_uid"], volumes, ticks, now, vm_running)
    stored = resource.get("state") or plan.get("state") or "draft"
    health = health or {}
    if stored in NOT_REPLICATING:
        # plus rien n'est répliqué : l'âge du dernier point ne mesure plus un
        # RPO (vu sur le banc : une VM basculée faisait dépasser l'objectif du
        # plan entier, `status` sortait en 4)
        ev.update(rpo_held_s=None, exceeded=False, drift=[], backlog=0)
    reasons = []
    state = stored
    if stored == "protected":
        # toutes les raisons sont relevées, l'état suit la plus grave
        if health.get("source_reachable") is False:
            reasons.append("source-unreachable")
        if ev["errors"]:
            reasons.append("backup-error")
        if health.get("standby_degraded"):
            reasons.append("standby-degraded")
        if health.get("never_backs_up"):
            # disques sans sauvegarde depuis plusieurs intervalles (suiveur)
            reasons.append("disk-never-backs-up")
        if ev["drift"]:
            reasons.append("rpo-drift")
        if spec_drifted(resource, health):
            reasons.append("plan-drift")
        if ev["exceeded"]:
            reasons.append("rpo-exceeded")
        if not ev["complete"]:
            # un disque sans point appliqué : jamais « protégé » tout court
            reasons.append("no-point")
        found = {REASON_STATE[r] for r in reasons}
        state = next((s for s in COMPUTED_PRIORITY if s in found), stored)
    ev["state"], ev["reasons"] = state, reasons
    return ev


def resource_state(plan, resource, volumes, ticks, now, vm_running, health=None):
    """État d'une ressource (RESOURCE_STATES). L'état rangé dit où en est
    le cycle de vie ; pour une ressource protégée, la lecture des passages
    affine en `lagging` (RPO tenu au-delà de l'objectif), `drifting` (un
    passage plus long qu'un pas, ou spec changée : `spec_drifted(resource,
    health)` sur `vm_generation` et `vm_spec_digest`), `degraded`
    (sauvegarde en erreur, ou `health["standby_degraded"]`),
    `source-unreachable` (`health["source_reachable"] is False`). Un disque
    sans point appliqué donne `lagging` (raison `no-point`). Toutes les
    raisons sont rendues par `plan_status` ; l'état suit COMPUTED_PRIORITY."""
    return _resource_eval(plan, resource, volumes, ticks, now, vm_running, health)["state"]


def _role_rank(role):
    return ROLES.index(role) if role in ROLES else -1


def _action(name, role):
    need = ACTION_ROLES[name]
    if role is None:
        return {"name": name, "role": need, "allowed": True, "reason": ""}
    ok = _role_rank(role) >= _role_rank(need)
    return {"name": name, "role": need, "allowed": ok, "reason": "" if ok else "role"}


def error_actions(origin):
    """Actions proposées en `error` : celles dont l'état d'arrivée
    (ACTION_STATE) est une sortie permise par `error_exits(origin)`. Le
    registre et l'interface lisent la même règle : aucune action proposée
    n'est refusée, aucune sortie permise n'est cachée."""
    exits = error_exits(origin)
    return [a for a in ACTION_ORDER if ACTION_STATE.get(a) in exits]


def allowed_actions(plan, resource, side, role=None):
    """Actions d'une ressource selon son état (`resource["state"]`, calculé ;
    en `error`, selon `resource["error_origin"]`, rangé) et le côté d'où l'on regarde (`source`, `target`, ou `any` pour la vue de
    tous les plans), chacune avec le rôle requis. Avec `role`, `allowed` dit
    si la personne peut la lancer (l'action reste listée)."""
    state = resource.get("state") or "draft"
    if state == "error":
        origin = resource.get("error_origin") or (plan.get("error_origin") if plan.get("state") == "error"
                                                  else None)
        names = error_actions(origin)
    else:
        names = list(STATE_ACTIONS.get(state, ()))
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


def recurring_job(name, cron, retain, concurrency, plan_id, vm_uid, generation):
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
    """Sauvegarde d'un instantané (sauvegarde finale de la bascule). Nos
    étiquettes vont aussi dans `spec.labels` : ce sont les seules que la
    cible commune garde, et donc les seules que la cible relit (`Backup`
    synchronisé : `status.labels`), comme pour les sauvegardes d'une tâche
    récurrente (vu sur Longhorn 1.11.2 et 1.12.1)."""
    md_labels = {"backup-volume": backup_volume or volume, "backup-target": target}
    md_labels.update(labels or {})
    spec = {"snapshotName": snapshot_name}
    if labels:
        spec["labels"] = dict(labels)
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
            "metadata": _lh_meta(name or snapshot_name, md_labels), "spec": spec}


def backup_owned_by(backup, plan_id):
    """Une sauvegarde est-elle du plan ? Celles d'une tâche récurrente ne
    portent les étiquettes de la tâche (`RecurringJob.spec.labels`) que dans
    `spec.labels` côté source et `status.labels` partout, jamais dans
    `metadata.labels` (qui n'a que `backup-volume` et `backup-target`) : vu
    sur Longhorn 1.11.2 (source) et 1.12.1 (cible synchronisée)."""
    for labels in ((backup.get("metadata") or {}).get("labels"), (backup.get("spec") or {}).get("labels"),
                   (backup.get("status") or {}).get("labels")):
        if owned_by(labels, plan_id):
            return True
    return False


def activation_patch():
    """Comme `VolumeManager.Activate` de longhorn-manager (manager/volume.go,
    l. 402 en v1.11.2, l. 448 en v1.12.1) : `Spec.Frontend` puis
    `Spec.Standby = false`. L'activation est définitive."""
    return {"spec": {"Standby": False, "frontend": "blockdev"}}


def static_pv(volume, size, mode, access_modes, storage_class, labels=None, claim_ref=None):
    """PV statique d'un volume activé. `claim_ref` (namespace, nom) le
    réserve d'avance au PVC voulu : aucun autre PVC en attente de la même
    classe ne peut le prendre (pré-liaison documentée de Kubernetes)."""
    md = {"name": volume}
    if labels:
        md["labels"] = dict(labels)
    spec = {"capacity": {"storage": quantity(size)}, "volumeMode": mode,
            "accessModes": list(access_modes), "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": storage_class,
            "csi": {"driver": LONGHORN_DRIVER, "volumeHandle": volume}}
    if claim_ref:
        spec["claimRef"] = {"namespace": claim_ref[0], "name": claim_ref[1]}
    return {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": md, "spec": spec}


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


def target_vm(source_vm, resource, mappings, options, claims, plan_id, generation):
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
