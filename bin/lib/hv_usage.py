"""harvester-ops : le décompte des GPU-heures et les quotas (Usage, v1.95.0).

Deux mesures, côte à côte, sur une période :

- **réservé** : les GPU-heures des réservations (nombre d'unités x durée),
  coupées aux bornes de la période. Une réservation annulée compte de son
  début jusqu'à son annulation si le créneau avait commencé, rien sinon ;
- **consommé** : les GPU-heures relevées par l'échantillonneur de la console.
  Un relevé (`sample`) dit qu'à l'instant `ts` un consommateur (VM ou pod)
  tenait `count` unités d'une unité GPU de l'inventaire. Il vaut le temps
  écoulé depuis le relevé précédent du même consommateur sur la même unité
  quand ce dernier date d'au plus deux intervalles, un intervalle sinon : un
  trou plus long (console arrêtée, cluster injoignable) n'est pas comblé.

Le consommateur est rattaché à un titulaire (personne ou groupe) par une
réservation qui le cible (la VM, ou la charge de conteneurs dont le pod fait
partie) sur le même cluster à cet instant ; sinon il reste « sans
réservation », rangé sous son namespace.

L'écart se lit par titulaire : le gaspillage (réservé et pas utilisé, compté
réservation par réservation, sur la partie déjà passée) et le hors règle (utilisé sans réservation, ou
au-delà du nombre d'unités réservées).

Quotas : par personne ou par groupe, un plafond de GPU-heures RÉSERVÉES par
mois calendaire (UTC) et, facultatif, un nombre maximal d'unités réservées
en même temps (tous clusters confondus). Ils s'appliquent à la création
d'une réservation.

Fonctions pures, sans entrée-sortie ; `now` est toujours passé.
"""

import csv
import io
import math
from datetime import datetime, timedelta, timezone

import hv_bookings as hb

HOLDER_KINDS = ("user", "group", "namespace")
QUOTA_KINDS = ("user", "group")
SAMPLED_TYPES = ("gpu", "vgpu", "node")   # une instance MIG est tenue par un vGPU, déjà relevé
DEFAULT_INTERVAL = 300
MIN_INTERVAL, MAX_INTERVAL = 30, 3600
GAP_FACTOR = 2                            # un trou de plus de deux intervalles n'est pas comblé
RETENTION_DAYS = 400
MAX_SPAN_DAYS = 400
MAX_TZ_MINUTES = 14 * 60
MAX_QUOTA_HOURS = 1_000_000
MAX_QUOTA_UNITS = 10_000
EPS = 1e-9


def _t(v):
    return hb.parse_time(v)


def _hours(s, e):
    return max(0.0, (e - s).total_seconds() / 3600.0)


def _r(v):
    return round(v + 0.0, 3)


def clip(s, e, start, end):
    """[s, e) coupé à [start, end), ou None s'il n'en reste rien."""
    s2, e2 = max(s, start), min(e, end)
    return (s2, e2) if s2 < e2 else None


def interval_from(value, default=DEFAULT_INTERVAL):
    """Un intervalle d'échantillonnage en secondes, borné à [30, 3600]."""
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(MIN_INTERVAL, min(MAX_INTERVAL, v))


def period(frm, to, now):
    """(début, fin) d'une période demandée ; par défaut le mois en cours
    (UTC). ValueError si elle est à l'envers ou trop longue."""
    now = _t(now)
    start = _t(frm) if frm else month_start(now)
    end = _t(to) if to else next_month(month_start(now))
    if end <= start:
        raise ValueError("the end of the period must come after its start")
    if end - start > timedelta(days=MAX_SPAN_DAYS):
        raise ValueError(f"a period lasts {MAX_SPAN_DAYS} days at most")
    return start, end


def tz_from(value):
    """Le décalage du fuseau de la personne, en minutes à l'est d'UTC
    (les jours du graphique sont les siens). 0 par défaut."""
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return v if -MAX_TZ_MINUTES <= v <= MAX_TZ_MINUTES else 0


def month_start(dt):
    dt = _t(dt)
    return datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)


def next_month(dt):
    dt = month_start(dt)
    return datetime(dt.year + (dt.month == 12), dt.month % 12 + 1, 1, tzinfo=timezone.utc)


# -- réservé -----------------------------------------------------------------------

def booking_window(b):
    """(début, fin) pendant lesquels une réservation compte, ou None. Annulée :
    de son début jusqu'à l'annulation si le créneau avait commencé, sinon
    rien."""
    s, e = _t(b["start"]), _t(b["end"])
    if b.get("cancelled_at"):
        e = min(e, _t(b["cancelled_at"]))      # annulée avant son début : il ne reste rien
    return (s, e) if s < e else None


def booking_hours(b, start, end):
    """GPU-heures réservées de `b` sur [start, end)."""
    w = booking_window(b)
    c = clip(w[0], w[1], _t(start), _t(end)) if w else None
    return int(b.get("count") or 1) * _hours(*c) if c else 0.0


def holder_key(b):
    h = b.get("holder") or {}
    return (h.get("kind") or "user", h.get("name") or "")


# -- consommé ----------------------------------------------------------------------

def samples_from_units(units, cluster):
    """Les relevés d'un tour pour un cluster, depuis son inventaire GPU
    (`hv_gpu.build`) : un par consommateur de chaque unité utilisée. Les
    unités MIG sont écartées (leurs instances sont tenues par des vGPU,
    relevés à part) ; un consommateur inconnu n'est pas relevé."""
    out = []
    for u in units or []:
        if not isinstance(u, dict) or u.get("type") not in SAMPLED_TYPES:
            continue
        for c in u.get("consumers") or []:
            if not isinstance(c, dict) or c.get("kind") not in ("vm", "pod") or not c.get("name"):
                continue
            n = c.get("count", 1)
            n = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1
            out.append({"cluster": cluster, "unit": str(u.get("id") or ""), "type": u["type"],
                        "model": str(u.get("model") or ""), "kind": c["kind"],
                        "namespace": str(c.get("namespace") or ""), "name": str(c["name"]),
                        "workload": str(c.get("workload") or ""), "count": n})
    return out


def _series_key(x):
    return (x.get("cluster"), x.get("unit"), x.get("kind"), x.get("namespace"), x.get("name"))


def segments(samples, start, end):
    """[(relevé, début, fin)] : le temps que vaut chaque relevé, coupé à la
    période. Il vaut l'écart avec le relevé précédent de la même série
    (cluster, unité, consommateur) si cet écart ne dépasse pas deux
    intervalles, un intervalle sinon ; un doublon au même instant ne compte
    pas deux fois."""
    start, end = _t(start), _t(end)
    series = {}
    for x in samples or []:
        series.setdefault(_series_key(x), []).append((_t(x["ts"]), x))
    out = []
    for items in series.values():
        items.sort(key=lambda p: p[0])
        prev = None
        for t, x in items:
            if prev is not None and t <= prev:
                continue
            d = timedelta(seconds=interval_from(x.get("interval")))
            if prev is not None and t - prev <= GAP_FACTOR * d:
                d = t - prev
            prev = t
            c = clip(t - d, t, start, end)
            if c:
                out.append((x, c[0], c[1]))
    return out


def targets(b, x):
    """Vrai si la réservation `b` cible le consommateur du relevé `x` : la
    même VM, ou un pod de la charge ciblée (ou le pod lui-même), sur le même
    cluster et dans le même namespace."""
    t = b.get("target")
    if not t or b.get("cluster") != x.get("cluster") or t.get("namespace") != x.get("namespace"):
        return False
    if t.get("kind") == "vm":
        return x.get("kind") == "vm" and x.get("name") == t.get("name")
    return x.get("kind") == "pod" and t.get("name") in (x.get("workload") or "", x.get("name"))


def attribute(x, at, bookings):
    """La réservation qui rattache ce consommateur à un titulaire à l'instant
    `at`, ou None. Entre plusieurs, celle du même type d'unité d'abord, puis
    la plus ancienne."""
    at = _t(at) if not isinstance(at, datetime) else at
    hits = []
    for b in bookings or []:
        if not targets(b, x):
            continue
        w = booking_window(b)
        if w and w[0] <= at < w[1]:
            hits.append(b)
    if not hits:
        return None
    hits.sort(key=lambda b: (b.get("type") != x.get("type"), b["start"], b.get("id") or ""))
    return hits[0]


def _days(s, e, tz):
    """[(jour local, heures)] de [s, e), jours du fuseau `tz` (minutes)."""
    off = timedelta(minutes=tz)
    cur = s
    while cur < e:
        local = cur + off
        midnight = datetime(local.year, local.month, local.day, tzinfo=timezone.utc) + timedelta(days=1) - off
        nxt = min(e, midnight)
        yield local.date().isoformat(), _hours(cur, nxt)
        cur = nxt


def day_list(start, end, tz=0):
    off = timedelta(minutes=tz)
    d, last = (_t(start) + off).date(), (_t(end) - timedelta(seconds=1) + off).date()
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# -- le rapport ----------------------------------------------------------------------

def quota_month(start, end, now):
    """Le mois dont le quota se montre : celui de la fin de la période,
    ramenée à maintenant si elle est dans le futur."""
    ref = min(_t(end) - timedelta(seconds=1), _t(now))
    ref = max(ref, _t(start))
    return month_start(ref)


def report(bookings, samples, start, end, quotas=(), tz=0, now=None):
    """Le décompte d'une période.

    `bookings` : les réservations (annulées comprises) ; `samples` : les
    relevés, avec `ts` et `interval` ; `quotas` : [{holder: {kind, name},
    monthly_hours, max_units}]. Rend {from, to, holders, days, totals,
    quota_month} ; une ligne par titulaire (personne, groupe, ou namespace
    « sans réservation ») avec réservé, consommé, gaspillage, hors règle,
    le détail par cluster et type d'unité, et son quota."""
    start, end = _t(start), _t(end)
    now = _t(now) if now else end
    rows = {}

    def row(key):
        if key not in rows:
            rows[key] = {"holder": {"kind": key[0], "name": key[1]}, "booked": 0.0, "consumed": 0.0,
                         "waste": 0.0, "out_of_rule": 0.0, "bookings": 0, "_br": {}}
        return rows[key]

    def part(r, cluster, typ):
        return r["_br"].setdefault((cluster, typ), {"cluster": cluster, "type": typ,
                                                    "booked": 0.0, "consumed": 0.0})

    days = {d: {"day": d, "booked": 0.0, "consumed": 0.0} for d in day_list(start, end, tz)}
    booked_of = {}
    for b in bookings or []:
        w = booking_window(b)
        c = clip(w[0], w[1], start, end) if w else None
        if not c:
            continue
        n = int(b.get("count") or 1)
        h = n * _hours(*c)
        r = row(holder_key(b))
        r["booked"] += h
        r["bookings"] += 1
        part(r, b["cluster"], b["type"])["booked"] += h
        # le gaspillage ne se compte que sur ce qui est passé
        past = clip(c[0], c[1], start, min(end, now))
        booked_of[b.get("id") or id(b)] = (b, n * _hours(*past) if past else 0.0)
        for d, hh in _days(c[0], c[1], tz):
            if d in days:
                days[d]["booked"] += n * hh

    groups = {}                               # (réservation, instant) -> [b, unités, heures]
    for x, s, e in segments(samples, start, end):
        b = attribute(x, s + (e - s) / 2, bookings)
        n = int(x.get("count") or 1)
        h = n * _hours(s, e)
        r = row(holder_key(b) if b else ("namespace", x.get("namespace") or ""))
        r["consumed"] += h
        part(r, x.get("cluster"), x.get("type"))["consumed"] += h
        for d, hh in _days(s, e, tz):
            if d in days:
                days[d]["consumed"] += n * hh
        if b is None:
            r["out_of_rule"] += h
        else:
            g = groups.setdefault((b.get("id") or id(b), x["ts"]), [b, 0, 0.0])
            g[1] += n
            g[2] += h
    used_of = {}
    for (bid, _ts), (b, n, h) in groups.items():
        within = h * min(n, int(b.get("count") or 1)) / n
        used_of[bid] = used_of.get(bid, 0.0) + within
        row(holder_key(b))["out_of_rule"] += h - within
    for bid, (b, h) in booked_of.items():
        row(holder_key(b))["waste"] += max(0.0, h - used_of.get(bid, 0.0))

    # les quotas : le mois de la période, même sans activité
    qm = quota_month(start, end, now)
    qm_end = next_month(qm)
    qmap = {(q["holder"]["kind"], q["holder"]["name"]): q for q in quotas or []}
    for key in qmap:
        row(key)
    out = []
    for key, r in rows.items():
        q = qmap.get(key)
        quota = None
        if q:
            month_booked = sum(booking_hours(b, qm, qm_end) for b in bookings or [] if holder_key(b) == key)
            cap = q.get("monthly_hours")
            quota = {"monthly_hours": cap, "max_units": q.get("max_units"), "month_booked": _r(month_booked),
                     "remaining": None if cap is None else _r(max(0.0, cap - month_booked)),
                     "over": cap is not None and month_booked > cap + EPS}
        out.append({"holder": r["holder"], "booked": _r(r["booked"]), "consumed": _r(r["consumed"]),
                    "waste": _r(r["waste"]), "out_of_rule": _r(r["out_of_rule"]), "bookings": r["bookings"],
                    "quota": quota,
                    "breakdown": sorted(({**p, "booked": _r(p["booked"]), "consumed": _r(p["consumed"])}
                                         for p in r["_br"].values()),
                                        key=lambda p: (str(p["cluster"]), str(p["type"])))})
    order = {k: i for i, k in enumerate(HOLDER_KINDS)}
    out.sort(key=lambda r: (order.get(r["holder"]["kind"], 9), r["holder"]["name"]))
    totals = {k: _r(sum(r[k] for r in out)) for k in ("booked", "consumed", "waste", "out_of_rule")}
    return {"from": hb.iso(start), "to": hb.iso(end), "tz": tz, "quota_month": qm.strftime("%Y-%m"),
            "holders": out, "totals": totals,
            "days": [{"day": d["day"], "booked": _r(d["booked"]), "consumed": _r(d["consumed"])}
                     for d in days.values()]}


CSV_HEAD = ("holder_kind", "holder", "cluster", "type", "booked_gpu_hours", "consumed_gpu_hours",
            "waste_gpu_hours", "out_of_rule_gpu_hours", "monthly_quota_hours", "quota_month_booked_hours")


def _cell(v):
    """Une cellule sans formule : un tableur lirait `=...` comme un calcul."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(rep):
    """Le rapport en CSV : une ligne par titulaire (cluster et type vides),
    puis une par cluster et type de son détail."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEAD)
    for r in rep.get("holders") or []:
        h, q = r["holder"], r.get("quota") or {}
        w.writerow([_cell(v) for v in (h["kind"], h["name"], "", "", r["booked"], r["consumed"], r["waste"],
                                       r["out_of_rule"], q.get("monthly_hours"), q.get("month_booked"))])
        for p in r.get("breakdown") or []:
            w.writerow([_cell(v) for v in (h["kind"], h["name"], p["cluster"], p["type"], p["booked"],
                                           p["consumed"], "", "", "", "")])
    return buf.getvalue()


# -- quotas --------------------------------------------------------------------------

def _num(value, field, integer, hi):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number") from None
    if math.isnan(v) or math.isinf(v) or v < 0 or v > hi:
        raise ValueError(f"{field} must be from 0 to {hi}")
    if integer:
        if v != int(v) or v < 1:
            raise ValueError(f"{field} must be a whole number from 1 to {hi}")
        return int(v)
    return round(v, 3)


def normalize_quota(req):
    """Une demande de quota -> {holder: {kind, name}, monthly_hours,
    max_units}. Au moins une des deux limites. ValueError sinon."""
    if not isinstance(req, dict):
        raise ValueError("a quota must be an object")
    h = req.get("holder") or {}
    if not isinstance(h, dict) or h.get("kind") not in QUOTA_KINDS:
        raise ValueError("a quota is for a person (user) or a group")
    name = hb._text(h.get("name"), "holder", hb.NAME_MAX, required=True)
    hours = _num(req.get("monthly_hours"), "monthly GPU-hours", False, MAX_QUOTA_HOURS)
    units = _num(req.get("max_units"), "maximum concurrent units", True, MAX_QUOTA_UNITS)
    if hours is None and units is None:
        raise ValueError("give a monthly GPU-hours cap, a maximum of concurrent units, or both")
    return {"holder": {"kind": h["kind"], "name": name}, "monthly_hours": hours, "max_units": units}


def quota_reasons(new, holder_bookings, quota):
    """Les raisons de refuser `new` (réservation normalisée) au regard du
    quota de son titulaire ; vide si elle passe ou sans quota.

    `quota-hours` : sur un mois que touche le créneau, les GPU-heures déjà
    réservées par le titulaire plus celles de `new` dépassent le plafond
    (`month`, `used`, `adding`, `cap`). `quota-units` : à un instant du
    créneau, le titulaire tiendrait plus d'unités réservées que permis
    (`peak`, `max`, `at`)."""
    if not quota:
        return []
    key = holder_key(new)
    others = [b for b in holder_bookings or [] if holder_key(b) == key and b.get("id") != new.get("id")]
    out = []
    cap = quota.get("monthly_hours")
    if cap is not None:
        s, e = _t(new["start"]), _t(new["end"])
        m = month_start(s)
        while m < e:
            m2 = next_month(m)
            adding = booking_hours(new, m, m2)
            used = sum(booking_hours(b, m, m2) for b in others)
            if adding > 0 and used + adding > cap + EPS:
                out.append({"code": "quota-hours", "holder": new["holder"], "month": m.strftime("%Y-%m"),
                            "used": _r(used), "adding": _r(adding), "cap": cap})
            m = m2
    mx = quota.get("max_units")
    if mx:
        n, at = hb.peak(hb.live(others) + [new], new["start"], new["end"])
        if n > mx:
            out.append({"code": "quota-units", "holder": new["holder"], "peak": n, "max": mx,
                        "at": at or new["start"]})
    return out
