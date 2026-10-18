"""harvester-ops : les réservations de GPU (Bookings, v1.94.0).

Une réservation retient, sur un créneau, des unités GPU d'un cluster pour une
personne ou un groupe. Elle vit dans la console seulement : la version 1
n'écrit rien sur les clusters (ni attachement, ni quota, ni arrêt en fin de
créneau).

Ce qu'on réserve :

- une unité précise de l'inventaire (`unit`), pour les types `gpu` (GPU
  entier en passthrough) et `vgpu` (une fonction virtuelle d'un type de
  vGPU) : une unité, un créneau ;
- un nombre d'unités d'un type et d'un modèle (`model`) sur le cluster :
  `vgpu` d'un type (« NVIDIA H100-20C »), `mig` d'un profil (« MIG 3g.40gb »),
  `node` d'une ressource de conteneurs (« nvidia.com/gpu »), `gpu` d'un
  modèle de carte.

La capacité d'un type et d'un modèle vient de l'inventaire du cluster
(`hv_gpu.build`) : le nombre d'unités qui ne sont pas désactivées ; pour le
MIG, les instances CRÉÉES du profil (une instance possible mais pas créée ne
se donne à personne) ; pour les conteneurs, la ressource allouable des nœuds
non cordonnés. Une réservation d'une unité précise compte aussi dans la
capacité de son modèle.

Conflits refusés à la création : la même unité sur des créneaux qui se
chevauchent, ou, à un instant du créneau, plus d'unités d'un type et d'un
modèle réservées que le cluster n'en a. Les créneaux sont semi-ouverts
[début, fin) : deux créneaux bout à bout ne se chevauchent pas.

Fonctions pures, sans entrée-sortie ; `now` est toujours passé.
"""

import re
from datetime import datetime, timedelta, timezone

TYPES = ("gpu", "vgpu", "mig", "node")
UNIT_TYPES = ("gpu", "vgpu")          # réservables à l'unité
STATES = ("upcoming", "active", "ended", "cancelled")
HOLDER_KINDS = ("user", "group")
TARGET_KINDS = ("vm", "workload")
MAX_DAYS = 90
MAX_COUNT = 64
NOTE_MAX = 500
NAME_MAX = 128
SOON = timedelta(hours=24)            # « réservée bientôt » dans l'inventaire

CLUSTER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$")
K8S_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$")
ID_RE = re.compile(r"^bk-[a-f0-9]{8,32}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def parse_time(value):
    """Un horodatage ISO 8601 -> datetime UTC. Sans fuseau, il est lu en UTC
    (l'interface envoie toujours un `Z`). ValueError sinon."""
    if isinstance(value, datetime):
        dt = value
    else:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a date and time is expected (ISO 8601)")
        s = value.strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            raise ValueError(f"unreadable date and time: {value[:40]}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def iso(dt):
    """La forme rangée : `2026-10-18T09:00:00Z` (l'ordre des chaînes est celui
    du temps)."""
    return parse_time(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value, field, limit, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit or _CTRL_RE.search(value.replace("\n", "")):
        raise ValueError(f"{field} must be {limit} characters at most, without control characters")
    return value


def _ref(value, field):
    """`namespace/nom` -> (ns, nom), noms RFC 1123."""
    if isinstance(value, dict):
        ns, name = value.get("namespace"), value.get("name")
    else:
        ns, _, name = str(value or "").partition("/")
    if not (isinstance(ns, str) and K8S_NAME_RE.match(ns) and isinstance(name, str) and K8S_NAME_RE.match(name)):
        raise ValueError(f"{field} must be namespace/name (RFC 1123 names)")
    return ns, name


def normalize(req, now, max_days=MAX_DAYS):
    """Une demande de réservation -> la réservation à ranger (sans id ni
    auteur). ValueError avec la raison sinon.

    `req` : {cluster, type, unit | model, count, holder: {kind, name},
    start, end, target: {kind, namespace, name} | None, note}."""
    if not isinstance(req, dict):
        raise ValueError("a booking must be an object")
    cluster = req.get("cluster")
    if not isinstance(cluster, str) or not CLUSTER_RE.match(cluster):
        raise ValueError("invalid cluster name")
    typ = req.get("type")
    if typ not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    unit = _text(req.get("unit"), "unit", NAME_MAX)
    model = _text(req.get("model"), "model", NAME_MAX)
    count = req.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
        raise ValueError(f"count must be a whole number from 1 to {MAX_COUNT}")
    if unit:
        if typ not in UNIT_TYPES:
            raise ValueError("a specific unit is booked for whole GPUs and vGPUs only; "
                             "book MIG instances and container GPUs by count")
        if count != 1:
            raise ValueError("a specific unit is booked one at a time (count 1)")
    elif not model:
        raise ValueError("pick a specific unit, or a model (vGPU type, MIG profile, GPU resource) and a count")
    holder = req.get("holder") or {}
    if not isinstance(holder, dict) or holder.get("kind") not in HOLDER_KINDS:
        raise ValueError("holder must be a person (user) or a group")
    hname = _text(holder.get("name"), "holder", NAME_MAX, required=True)
    start, end = parse_time(req.get("start")), parse_time(req.get("end"))
    if end <= start:
        raise ValueError("the end must come after the start")
    if end - start > timedelta(days=max_days):
        raise ValueError(f"a booking lasts {max_days} days at most")
    if end <= parse_time(now):
        raise ValueError("the slot is already over")
    target = req.get("target")
    tgt = None
    if target:
        if not isinstance(target, dict) or target.get("kind") not in TARGET_KINDS:
            raise ValueError("target must be a VM or a container workload")
        ns, name = _ref(target, "target")
        tgt = {"kind": target["kind"], "namespace": ns, "name": name}
    return {"cluster": cluster, "type": typ, "unit": unit, "model": model, "count": count,
            "holder": {"kind": holder["kind"], "name": hname},
            "start": iso(start), "end": iso(end), "target": tgt,
            "note": _text(req.get("note"), "note", NOTE_MAX)}


# -- capacité, d'après l'inventaire du cluster ----------------------------------

def _usable(u):
    return u.get("state") != "disabled"


def catalog(units):
    """Ce qui se réserve sur un cluster, d'après ses unités (`hv_gpu.build`) :
    [{type, model, capacity, used, units: [{id, name, node, state}]}], un
    élément par type et modèle de capacité non nulle. `units` n'est rempli
    que pour les types réservables à l'unité."""
    out = {}

    def entry(typ, model):
        return out.setdefault((typ, model), {"type": typ, "model": model, "capacity": 0,
                                             "used": 0, "units": []})
    for u in units or []:
        if not isinstance(u, dict) or not _usable(u):
            continue
        typ = u.get("type")
        if typ in UNIT_TYPES:
            if not u.get("model"):
                continue
            e = entry(typ, u["model"])
            e["capacity"] += 1
            e["used"] += 1 if u.get("used") else 0
            e["units"].append({"id": u.get("id"), "name": u.get("name"), "node": u.get("node"),
                               "address": u.get("address") or "", "state": u.get("state")})
        elif typ == "mig":
            # les instances utilisées d'un GPU ne disent pas leur profil :
            # elles vont au premier profil créé, au plus ses instances
            left = int(u.get("used") or 0)
            for p in u.get("profiles") or []:
                created = int(p.get("created") or 0)
                if created <= 0 or not p.get("name"):
                    continue
                e = entry("mig", p["name"])
                e["capacity"] += created
                take = min(left, created)
                e["used"] += take
                left -= take
        elif typ == "node":
            key = u.get("resource") or u.get("model")
            if not key:
                continue
            e = entry("node", key)
            e["capacity"] += int(u.get("total") or 0)
            e["used"] += int(u.get("used") or 0)
    order = {t: i for i, t in enumerate(TYPES)}
    return sorted((e for e in out.values() if e["capacity"] > 0),
                  key=lambda e: (order[e["type"]], e["model"]))


def resolve(booking, units):
    """Complète une réservation d'une unité précise avec le modèle de cette
    unité (sa capacité est celle du modèle). ValueError si l'unité n'est pas
    dans l'inventaire, est désactivée, ou n'est pas du type demandé."""
    if not booking.get("unit"):
        return booking
    for u in units or []:
        if isinstance(u, dict) and u.get("id") == booking["unit"]:
            if u.get("type") != booking["type"]:
                raise ValueError(f"unit {booking['unit']} is a {u.get('type')}, not a {booking['type']}")
            if not _usable(u) or not u.get("model"):
                raise ValueError(f"unit {booking['unit']} is disabled on the cluster and cannot be booked")
            return dict(booking, model=u["model"])
    raise ValueError(f"unit {booking['unit']} is not in the GPU inventory of {booking['cluster']}")


def capacity(units, typ, model):
    """Le nombre d'unités d'un type et d'un modèle sur le cluster."""
    for e in catalog(units):
        if e["type"] == typ and e["model"] == model:
            return e["capacity"]
    return 0


# -- états et chevauchements ----------------------------------------------------

def state(b, now):
    """upcoming, active, ended ou cancelled, d'après l'heure et l'annulation."""
    if b.get("cancelled_at"):
        return "cancelled"
    t = iso(now)
    if t < b["start"]:
        return "upcoming"
    return "active" if t < b["end"] else "ended"


def overlaps(a, b):
    """Créneaux semi-ouverts : bout à bout ne se chevauchent pas."""
    return a["start"] < b["end"] and b["start"] < a["end"]


def same_pool(a, b):
    return a["cluster"] == b["cluster"] and a["type"] == b["type"] and a["model"] == b["model"]


def peak(bookings, start, end):
    """(nombre au plus fort, instant) des réservations sur [start, end), par
    balayage : +n à l'entrée dans la fenêtre, -n à la sortie ; à un même
    instant les sorties passent avant les entrées (bout à bout = pas de
    cumul)."""
    events = []
    for b in bookings:
        s, e = max(b["start"], start), min(b["end"], end)
        if s < e:
            events.append((s, 1, b["count"]))
            events.append((e, 0, -b["count"]))
    events.sort()
    cur = best = 0
    at = None
    for t, _order, delta in events:
        cur += delta
        if cur > best:
            best, at = cur, t
    return best, at


def live(bookings):
    return [b for b in bookings or [] if not b.get("cancelled_at")]


def conflicts(new, existing, cap):
    """Les raisons de refuser `new` (réservation normalisée, modèle résolu),
    vide si elle passe. `cap` : la capacité de son type et modèle sur le
    cluster. Chaque raison : {code, ...} avec code `unit-taken` (la même
    unité, `booking` la réservation qui la tient) ou `capacity` (`peak`
    réservées au plus fort avec celle-ci, `capacity`, `at` l'instant)."""
    out = []
    others = [b for b in live(existing) if b.get("id") != new.get("id") and overlaps(b, new)]
    if new.get("unit"):
        for b in others:
            if b["cluster"] == new["cluster"] and b.get("unit") == new["unit"]:
                out.append({"code": "unit-taken", "booking": b.get("id"), "holder": b["holder"],
                            "start": b["start"], "end": b["end"]})
    pool = [b for b in others if same_pool(b, new)]
    n, at = peak(pool + [new], new["start"], new["end"])
    if n > cap:
        out.append({"code": "capacity", "peak": n, "capacity": cap, "at": at or new["start"],
                    "type": new["type"], "model": new["model"]})
    return out


def booked_at(bookings, typ, model, at, cluster=None):
    """Combien d'unités d'un type et d'un modèle sont réservées à l'instant
    `at` (réservations non annulées)."""
    t = iso(at)
    return sum(b["count"] for b in live(bookings)
               if b["type"] == typ and b["model"] == model and b["start"] <= t < b["end"]
               and (cluster is None or b["cluster"] == cluster))


def describe(reason):
    """Une phrase anglaise pour une raison de refus (l'interface traduit
    depuis le code ; la ligne de commande et l'API rendent ceci)."""
    if reason["code"] == "unit-taken":
        h = reason["holder"]
        return (f"this unit is already booked by {h.get('kind')} {h.get('name')} "
                f"from {reason['start']} to {reason['end']}")
    if reason["code"] == "capacity":
        return (f"{reason['peak']} x {reason['type']} {reason['model']} would be booked at "
                f"{reason['at']}, the cluster has {reason['capacity']}")
    return reason.get("code", "refused")


def can_cancel(b, user, role):
    """Un opérateur annule les siennes (créées par lui, ou dont il est le
    titulaire), un admin celles de tout le monde ; un lecteur aucune."""
    if role == "admin":
        return True
    if role != "operator" or not user:
        return False
    return b.get("created_by") == user or (b.get("holder") or {}) == {"kind": "user", "name": user}


def is_mine(b, user):
    return bool(user) and (b.get("created_by") == user
                           or (b.get("holder") or {}) == {"kind": "user", "name": user})
