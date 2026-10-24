"""harvester-ops : lectures dérivées de la réplication (écrans de suivi).

Fonctions pures, sans base ni cluster : elles reçoivent des lignes déjà lues
(statistiques des passages, lignes de plan, santé relevée par le suiveur) et
rendent ce que lisent la console et la ligne de commande :

* le résumé des transferts d'un plan (dernier passage, débits, totaux sur
  24 h) ;
* les séries des graphiques, regroupées par tranche de temps ;
* les alertes en cours, calculées sur l'état présent (jamais un historique) ;
* la joignabilité des deux côtés vue par le suiveur ;
* les séries d'un lien entre deux clusters (débits passifs).

`replication_db` les appelle ; la console et `harvester-replication.py`
passent par `replication_db` : mêmes objets des deux côtés.

Bibliothèque standard seulement.
"""

import math
from datetime import datetime, timedelta, timezone

import replication as rp

# fenêtre -> (durée, taille d'une tranche), en secondes
SERIES_RANGES = {"1h": (3600, 60), "24h": (86400, 600), "7d": (7 * 86400, 3600), "30d": (30 * 86400, 21600)}
DAY_S = 86400
LINK_BUCKET_S = 3600

# Alertes : code -> niveau. Les raisons d'une ressource protégée viennent de
# `replication._resource_eval` ; `rpo-drift` et `no-point` n'en sont pas
# (passage long ponctuel, protection qui démarre).
ALERT_LEVELS = {
    "rpo-exceeded": "warn", "disk-never-backs-up": "warn", "source-vm-running-after-failover": "error",
    "backup-error": "error", "standby-degraded": "warn", "source-unreachable": "error", "plan-drift": "warn",
    "plan-error": "error", "selector-new-vm": "warn",
}
ALERT_CODES = tuple(ALERT_LEVELS)
_REASON_ALERTS = ("source-unreachable", "backup-error", "standby-degraded", "disk-never-backs-up", "plan-drift",
                  "rpo-exceeded")
_MESSAGES = {
    "rpo-exceeded": "RPO held {rpo} s, objective {objective} s",
    "disk-never-backs-up": "a disk shows no backup for several intervals: check the recurring job",
    "source-vm-running-after-failover": "the source VM runs again after the failover: stop it by hand",
    "backup-error": "a backup of this VM failed",
    "standby-degraded": "a standby volume on the target is not healthy",
    "source-unreachable": "the source cluster does not answer",
    "plan-drift": "the VM spec changed since protect: run protect again",
    "plan-error": "the plan is in error (from {origin}): see its journal",
    "selector-new-vm": "a VM matches the plan selector but is not protected yet: run protect",
}
_LEVEL_RANK = {"warn": 1, "error": 2}


def _t(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return rp._t(v)


def _epoch(v):
    return _t(v).timestamp()


def _iso(epoch):
    return rp.iso(datetime.fromtimestamp(epoch, timezone.utc))


def _passes(row):
    return max(1, int(row.get("passes") or 1))


def _wmean(rows, key):
    """Moyenne pondérée par le nombre de passages (une ligne réduite à
    l'heure en compte plusieurs) ; None sans valeur."""
    num = den = 0.0
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        w = _passes(r)
        num += float(v) * w
        den += w
    return num / den if den else None


def _max(rows, key):
    vals = [r.get(key) for r in rows if r.get(key) is not None]
    return max(vals) if vals else None


# ---------------------------------------------------------------------------
# Transferts et séries
# ---------------------------------------------------------------------------

def transfer_summary(stats, now):
    """Résumé des transferts d'un plan, depuis ses lignes `stats` : le
    dernier passage de n'importe quelle VM (date, octets, débits) et les
    totaux des 24 dernières heures (octets envoyés, passages)."""
    out = {"last_at": None, "last_bytes": 0, "upload_bps": None, "apply_bps": None, "bytes_24h": 0,
           "passes_24h": 0}
    rows = [r for r in stats or () if r.get("pass_at")]
    if not rows:
        return out
    last = max(rows, key=lambda r: (r["pass_at"], r.get("resource_uid") or ""))
    out.update(last_at=last["pass_at"], last_bytes=int(last.get("newly_uploaded") or 0),
               upload_bps=last.get("upload_bps"), apply_bps=last.get("apply_bps"))
    since = _epoch(now) - DAY_S
    day = [r for r in rows if _epoch(r["pass_at"]) >= since]
    out["bytes_24h"] = sum(int(r.get("newly_uploaded") or 0) for r in day)
    out["passes_24h"] = sum(_passes(r) for r in day)
    return out


def series_range(value):
    """(durée, tranche) d'une fenêtre ; ValueError si inconnue."""
    if value not in SERIES_RANGES:
        raise ValueError(f"range must be one of {', '.join(SERIES_RANGES)}")
    return SERIES_RANGES[value]


def bucket_series(stats, bucket_s, now, range_s):
    """Points d'un graphique : une tranche de `bucket_s` secondes (début de
    tranche aligné sur l'époque) par groupe de passages de la fenêtre ;
    tranches vides omises. RPO tenu et arriéré : le pire ; débits : la
    moyenne ; octets et passages : la somme."""
    since = _epoch(now) - range_s
    groups = {}
    for r in stats or ():
        if not r.get("pass_at"):
            continue
        e = _epoch(r["pass_at"])
        if e < since:
            continue
        groups.setdefault(int(math.floor(e / bucket_s)) * bucket_s, []).append(r)
    points = []
    for start in sorted(groups):
        rows = groups[start]
        backlog = _max(rows, "backlog")
        points.append({"t": _iso(start), "rpo_held_s": _max(rows, "rpo_held_s"),
                       "upload_bps": _wmean(rows, "upload_bps"), "apply_bps": _wmean(rows, "apply_bps"),
                       "bytes": sum(int(r.get("newly_uploaded") or 0) for r in rows),
                       "passes": sum(_passes(r) for r in rows), "backlog": int(backlog or 0)})
    return points


def link_passive(stats, now):
    """Débits passifs d'un lien (source, cible), depuis les statistiques de
    tous ses plans : dernier passage, médianes et totaux sur 24 h, série par
    heure."""
    rows = [r for r in stats or () if r.get("pass_at")]
    since = _epoch(now) - DAY_S
    day = [r for r in rows if _epoch(r["pass_at"]) >= since]
    t = transfer_summary(rows, now)
    series = [{"t": p["t"], "upload_bps": p["upload_bps"], "apply_bps": p["apply_bps"]}
              for p in bucket_series(day, LINK_BUCKET_S, now, DAY_S)]
    return {"last_at": t["last_at"], "upload_bps": t["upload_bps"], "apply_bps": t["apply_bps"],
            "upload_bps_p50_24h": rp.percentile([r.get("upload_bps") for r in day], 50),
            "apply_bps_p50_24h": rp.percentile([r.get("apply_bps") for r in day], 50),
            "bytes_24h": t["bytes_24h"], "passes_24h": t["passes_24h"], "series": series}


# ---------------------------------------------------------------------------
# Joignabilité et alertes
# ---------------------------------------------------------------------------

def _worst(values):
    """False l'emporte sur True, None (jamais lu) seulement sans valeur."""
    seen = [v for v in values if v is not None]
    if not seen:
        return None
    return all(bool(v) for v in seen)


def reach(health, health_at=()):
    """Joignabilité des deux côtés vue par le suiveur (`source_reachable`,
    `target_reachable` de la santé des ressources) : la pire valeur, None
    jamais lue ; `at` : le relevé le plus récent."""
    hs = list((health or {}).values())
    stamps = [h.get("checked_at") for h in hs if h.get("checked_at")] + [x for x in health_at if x]
    return {"source": _worst(h.get("source_reachable") for h in hs),
            "target": _worst(h.get("target_reachable") for h in hs),
            "at": max(stamps) if stamps else None}


def _alert(plan, code, level=None, uid="", namespace="", name="", since=None, **fmt):
    return {"plan": plan.get("id"), "plan_name": plan.get("name"), "resource_uid": uid or "",
            "namespace": namespace or "", "name": name or "", "source_host": plan.get("source_cluster"),
            "target_host": plan.get("target_cluster"), "code": code, "level": level or ALERT_LEVELS[code],
            "since": since, "message": _MESSAGES[code].format(**fmt)}


def plan_alerts(plan, row, health=None, pending=(), since=None):
    """Alertes en cours d'un plan, calculées sur l'état présent : raisons
    des ressources (`row["resources"][*]["reasons"]`), VM source revenue en
    marche après une bascule (santé `source_running_after_failover`), plan
    en `error`, VMs du sélecteur pas encore protégées (`pending`). `since` :
    {(uid, code): date} lue au journal (uid vide pour le plan)."""
    since = since or {}
    health = health or {}
    out = []
    if plan.get("state") == "error":
        out.append(_alert(plan, "plan-error", since=since.get(("", "error")),
                          origin=plan.get("error_origin") or "unknown"))
    for r in (row or {}).get("resources") or ():
        uid = r.get("resource_uid") or ""
        reasons = r.get("reasons") or ()
        for code in _REASON_ALERTS:
            if code not in reasons:
                continue
            at = since.get((uid, code)) or (since.get(("", code)) if code == "disk-never-backs-up" else None)
            fmt = {"rpo": r.get("rpo_held_s"), "objective": r.get("rpo_objective_s")} if code == "rpo-exceeded" \
                else {}
            out.append(_alert(plan, code, uid=uid, namespace=r.get("namespace"), name=r.get("name"), since=at,
                              **fmt))
        if (health.get(uid) or {}).get("source_running_after_failover"):
            code = "source-vm-running-after-failover"
            out.append(_alert(plan, code, uid=uid, namespace=r.get("namespace"), name=r.get("name"),
                              since=since.get((uid, code))))
    for p in pending or ():
        out.append(_alert(plan, "selector-new-vm", uid=p.get("uid"), namespace=p.get("namespace"),
                          name=p.get("name"), since=p.get("since") or since.get((p.get("uid") or "", "selector-new-vm"))))
    return out


def sort_alerts(alerts):
    """Erreurs d'abord, puis les plus anciennes."""
    return sorted(alerts, key=lambda a: (-_LEVEL_RANK.get(a["level"], 0), a.get("since") or "9999",
                                         a.get("plan_name") or "", a.get("namespace") or "", a.get("name") or "",
                                         a["code"]))


def window_start(now, range_s):
    return _t(now) - timedelta(seconds=range_s)
