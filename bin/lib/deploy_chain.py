"""Moteur de la chaîne d'un plan de déploiement (2.4.0).

Moteur pur : toutes les entrées-sorties sont injectées. La console le lance
dans le fil d'une action suivie `deploy:<cluster>` ; la ligne de commande
(bin/harvester-plans.py) le charge dans son propre processus. Même code,
deux appelants (décision 146).

    run(ctx, phases, previous=None, sleep=time.sleep, now=time.time, secrets=None) -> dict

`ctx` fournit :
    ctx.run_id                          identifiant de l'action (arbre des secrets)
    ctx.emit_step(step_id, status, message)
    ctx.set_result(dict)                résultat partiel, à chaque changement
    ctx.cancelled() -> bool

`phases` : liste de Phase (l'ordre est celui de ORDER, pas celui de la
liste ; une phase absente n'émet rien).

Une phase simple a `execute(ctx, state) -> PhaseOutcome`. Le statut
`retry` d'une phase à `timeout` (disponibilité) la fait rappeler toutes les
`poll` secondes jusqu'au délai, puis `error`. Une phase à éléments
(Terraform : un élément par modèle) a `items` et `run_item(ctx, state,
item) -> ItemOutcome` : le moteur tient l'ordre, les dépendances, l'arrêt
entre éléments et la reprise élément par élément. 2.5.0 : `before(ctx,
state)` d'une phase à éléments est appelé une fois avant le premier
élément (contrôle rejoué au début de la phase : capacité de N clusters) ;
un PhaseOutcome rendu termine la phase sans lancer d'élément (les éléments
de l'action reprise y sont recopiés tels quels, rien n'est perdu pour la
reprise suivante), None la laisse continuer.

`state` : {"phase": id, "previous": entrée de l'action reprise ou None,
"results": {id: entrée déjà produite par ce déroulé}}.

Règles :
- échec (`error`) : la chaîne s'arrête ; `partial` (des éléments faits,
  d'autres en échec) l'arrête aussi, les phases suivantes dépendant du
  contenu ; un élément dont une dépendance n'est pas faite est sauté ;
- arrêt : consulté avant chaque phase, chaque élément et chaque relecture ;
  un élément en cours va à son terme, les suivants sont `skipped` ;
- reprise : une phase simple `done` de l'action reprise passe par
  `verify` ; vrai, elle est sautée (« already done (checked) ») ; faux, ou
  sans `verify`, elle est rejouée. Une phase à éléments est TOUJOURS
  reprise élément par élément, même `done` : les éléments faits sont
  revérifiés un à un (`verify_item`) et sautés, un élément ajouté au plan
  depuis est exécuté ;
- exception d'une phase ou d'un élément : `error`, avec le seul NOM de
  l'exception (règle de la série : aucun détail qui porterait un secret) ;
- secrets du déroulé (`plan-run/<run_id>/...`) effacés à la fin, quelle
  qu'en soit l'issue, sauf `keep_secrets` (2.9.0 : déroulé reprenable,
  secrets gardés dans OpenBao) ; `ctx.run_id` (12 caractères
  hexadécimaux) est contrôlé avant tout ;
- les messages des phases et des éléments vont dans les étapes et le
  résultat : l'appelant n'y met jamais une valeur, seulement des noms et
  des codes.

Résultat rendu et passé à `ctx.set_result` :
    {"status": "done" | "error" | "partial" | "cancelled",
     "phases": [{"id", "status", "message", "seconds", ...données de la phase,
                 "items": [{<item_key>: id, "status", "message", ...}]}],
     "error_summary": nom d'exception ou message court (en échec seulement),
     "failed_phase": id (en échec seulement)}
La console y ajoute plan, révision, cluster, `resumes` et `dry_run`.
"""

import re
import time
from dataclasses import dataclass, field

import secret_store

# 2.6.0 (point 189) : Registre après Terraform (une classe de stockage qu'il
# crée peut servir) et avant Cluster API (les clusters ont besoin de leurs
# registres à la création). La reprise repère les phases par `id`.
# 2.8.0 : GitOps en dernier (le chart de Flux est un artefact, plan 2.8.0 point 223).
ORDER = ("install", "readiness", "terraform", "registry", "capi", "clusters", "artefacts", "gitops")
# phases du format pas encore livrées (aucune depuis la 2.7.0)
COMING = ()
CHECKED_MESSAGE = "already done (checked)"
POLL_SECONDS = 15
SECRET_CLASS = "plan-run"
EXIT_CODES = {"done": 0, "error": 1, "partial": 1, "cancelled": 3}
# 2.9.0 : issues d'un déroulé qu'une reprise peut continuer
RESUMABLE = ("error", "partial", "cancelled")
RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")


@dataclass
class PhaseOutcome:
    status: str                       # done | error | partial | retry
    items: list = None                # éléments produits (phase simple)
    message: str = ""
    data: dict = field(default_factory=dict)   # versé dans l'entrée (actions, unmet...)


@dataclass
class ItemOutcome:
    status: str                       # done | error
    message: str = ""
    data: dict = field(default_factory=dict)   # versé dans l'entrée de l'élément


@dataclass
class Item:
    id: str
    depends_on: tuple = ()


@dataclass
class Phase:
    id: str
    enabled: bool = True
    execute: object = None            # execute(ctx, state) -> PhaseOutcome
    verify: object = None             # verify(ctx, state) -> bool
    timeout: float = 0                # secondes ; > 0 : `retry` relu jusque-là
    poll: float = POLL_SECONDS
    items: list = None                # [Item] : phase à éléments
    run_item: object = None           # run_item(ctx, state, item) -> ItemOutcome
    verify_item: object = None        # verify_item(ctx, state, entrée précédente) -> bool
    item_key: str = "id"              # clé de l'identifiant dans l'entrée (template)
    item_step: str = "item-"          # préfixe des étapes d'élément (tf-)
    before: object = None             # before(ctx, state) -> PhaseOutcome | None (2.5.0)
    # 2.7.0 : phases dont l'échec (`partial` ou `error`) n'arrête pas
    # celle-ci : les artefacts des clusters créés vont à leur terme, ceux
    # d'un cluster en échec sont sautés par leur dépendance
    # `<phase>:<élément>` ; chaque phase du Défaire va à son terme
    after_failure_of: tuple = ()


def _exc_name(e):
    return type(e).__name__


def _safe_call(fn, *args):
    """(valeur, None) ou (None, nom de l'exception)."""
    try:
        return fn(*args), None
    except Exception as e:                       # noqa: BLE001 : jamais le texte
        return None, _exc_name(e)


def _verified(fn, *args):
    if fn is None:
        return False
    ok, err = _safe_call(fn, *args)
    return bool(ok) and err is None


def _run_simple(ctx, ph, state, sleep, now):
    start = now()
    while True:
        out, err = _safe_call(ph.execute, ctx, state)
        if err:
            return PhaseOutcome("error", message=err), err
        if not isinstance(out, PhaseOutcome):
            return PhaseOutcome("error", message="TypeError"), "TypeError"
        if out.status != "retry":
            return out, None
        if not ph.timeout or now() - start + ph.poll > ph.timeout:
            return PhaseOutcome("error", items=out.items, message=out.message or "timed out",
                                data=dict(out.data, timed_out=True)), None
        if ctx.cancelled():
            return PhaseOutcome("cancelled", items=out.items, message="cancelled", data=out.data), None
        sleep(ph.poll)
        if ctx.cancelled():
            return PhaseOutcome("cancelled", items=out.items, message="cancelled", data=out.data), None


def done_elsewhere(state, dep):
    """Dépendance `<phase>:<élément>` vers un élément d'une phase déjà
    déroulée : faite si cet élément y est `done`."""
    pid, _, iid = dep.partition(":")
    entry = (state.get("results") or {}).get(pid) or {}
    key = entry.get("item_key") or "id"
    return any(it.get(key) == iid and it.get("status") == "done" for it in entry.get("items") or [])


def _run_items(ctx, ph, state, previous, progress=None):
    prev_items = {}
    if previous:
        for it in previous.get("items") or []:
            prev_items[it.get(ph.item_key)] = it
    if ph.before is not None:
        out, err = _safe_call(ph.before, ctx, state)
        if err or (out is not None and not isinstance(out, PhaseOutcome)):
            out = PhaseOutcome("error", message=err or "TypeError")
        if out is not None:
            kept = [dict(it) for it in (previous or {}).get("items") or []]
            return PhaseOutcome(out.status if out.status in ("done", "error", "cancelled") else "error",
                                items=kept, message=out.message, data=out.data)
    entries, status_of = [], {}
    cancelled = False
    for item in ph.items or []:
        sid = f"{ph.item_step}{item.id}"
        entry = {ph.item_key: item.id}
        if not cancelled and ctx.cancelled():
            cancelled = True
        if cancelled:
            entry.update(status="skipped", message="cancelled")
        else:
            prev = prev_items.get(item.id)
            blocked = [d for d in item.depends_on
                       if (not done_elsewhere(state, d) if ":" in d else status_of.get(d) != "done")]
            if blocked:
                entry.update(status="skipped", message=f"depends on {', '.join(blocked)}, not done")
            elif prev and prev.get("status") == "done" and _verified(ph.verify_item, ctx, state, prev):
                entry = dict(prev, status="done", message=CHECKED_MESSAGE, checked=True)
                ctx.emit_step(sid, "skipped", CHECKED_MESSAGE)
                status_of[item.id] = "done"
                entries.append(entry)
                if progress:
                    progress(entries)
                continue
            else:
                ctx.emit_step(sid, "running", "")
                out, err = _safe_call(ph.run_item, ctx, state, item)
                if err or not isinstance(out, ItemOutcome) or out.status not in ("done", "error"):
                    entry.update(status="error", message=err or "TypeError")
                else:
                    entry.update(out.data or {})
                    entry.update(status=out.status, message=out.message)
        status_of[item.id] = entry["status"]
        ctx.emit_step(sid, entry["status"], entry.get("message", ""))
        entries.append(entry)
        if progress:
            progress(entries)
    done = sum(1 for e in entries if e["status"] == "done")
    errors = sum(1 for e in entries if e["status"] == "error")
    # sauté pour une dépendance : derrière un échec de cette phase, ou
    # (2.7.0) d'une phase précédente lancée en `partial`
    skipped = sum(1 for e in entries if e["status"] == "skipped")
    if cancelled:
        status = "cancelled"
    elif errors or skipped:
        status = "partial" if done else "error"
    else:
        status = "done"
    msg = f"{done}/{len(entries)} done" + (f", {errors} failed" if errors else "") \
        + (f", {skipped} skipped" if skipped and not errors else "")
    return PhaseOutcome(status, items=entries, message=msg)


def run(ctx, phases, previous=None, sleep=time.sleep, now=time.time, secrets=None, order=ORDER,
        keep_secrets=None):
    """Déroule les phases ; rend le résultat (voir l'en-tête du module).
    `order` (2.7.0) : ordre des phases d'une autre chaîne (le Défaire).
    `keep_secrets(statut)` (2.9.0, décision 141) : vrai pour un déroulé
    arrêté reprenable dont les secrets sont gardés dans OpenBao jusqu'à sa
    reprise (balayés au plus tard après le délai réglé) ; sinon, et
    toujours sur exception, les secrets sont effacés."""
    if not isinstance(getattr(ctx, "run_id", None), str) or not RUN_ID_RE.match(ctx.run_id):
        # avant tout secret : l'effacement final ne pourrait pas le désigner
        raise ValueError("run_id must be 12 hexadecimal characters")
    by_id = {p.id: p for p in phases}
    prev_by_id = {p.get("id"): p for p in ((previous or {}).get("phases") or [])}
    result = {"status": "running", "phases": []}
    results = {}
    stop = None                                   # statut qui arrête la chaîne
    try:
        for pid in order:
            ph = by_id.get(pid)
            if ph is None:
                continue
            sid = f"phase-{pid}"
            if pid in COMING:
                entry = {"id": pid, "status": "disabled", "message": "coming in a later release"}
                ctx.emit_step(sid, "skipped", entry["message"])
            elif not ph.enabled:
                entry = {"id": pid, "status": "disabled", "message": "disabled in the plan"}
                ctx.emit_step(sid, "skipped", "disabled in the plan")
            elif stop is not None and not (stop in ("partial", "error")
                                           and result.get("failed_phase") in ph.after_failure_of):
                entry = {"id": pid, "status": "skipped",
                         "message": "cancelled" if stop == "cancelled" else "an earlier phase did not end"}
                ctx.emit_step(sid, "skipped", entry["message"])
            elif ctx.cancelled():
                stop = "cancelled"
                entry = {"id": pid, "status": "skipped", "message": "cancelled"}
                ctx.emit_step(sid, "skipped", "cancelled")
            else:
                prev = prev_by_id.get(pid)
                state = {"phase": pid, "previous": prev, "results": results}
                # une phase à éléments n'est jamais sautée d'un bloc (revue I3)
                if (ph.items is None and prev and prev.get("status") == "done"
                        and _verified(ph.verify, ctx, state)):
                    entry = dict(prev, message=CHECKED_MESSAGE, checked=True)
                    ctx.emit_step(sid, "skipped", CHECKED_MESSAGE)
                else:
                    ctx.emit_step(sid, "running", "")
                    t0 = now()
                    if ph.items is not None:
                        def progress(entries, _pid=pid, _t0=t0):
                            # avancée d'une longue phase Terraform consignée
                            # élément par élément (revue M2)
                            live = {"id": _pid, "status": "running", "items": [dict(e) for e in entries]}
                            ctx.set_result(dict(result, phases=result["phases"] + [live]))
                        out, err = _run_items(ctx, ph, state, prev, progress), None
                    else:
                        out, err = _run_simple(ctx, ph, state, sleep, now)
                    status = out.status if out.status in ("done", "error", "partial", "cancelled") else "error"
                    entry = {"id": pid, "status": status}
                    entry.update(out.data or {})
                    # les données d'une phase ne remplacent ni id, ni statut, ni message (M1)
                    entry.update(id=pid, status=status, message=out.message or "")
                    if out.items is not None:
                        entry["items"] = out.items
                        entry["item_key"] = ph.item_key
                    entry["seconds"] = round(now() - t0, 1)
                    ctx.emit_step(sid, status, entry["message"])
                    if status != "done" and stop is None:
                        stop = status
                        result["failed_phase"] = pid
                        result["error_summary"] = err or entry["message"]
                    elif status == "cancelled" or (status == "error" and stop != "partial"):
                        # phase lancée derrière un échec (after_failure_of).
                        # 2.7.1 (m1) : derrière une phase `partial`, une
                        # erreur laisse le déroulé `partial` : la phase
                        # d'avant a fait quelque chose (des clusters créés)
                        stop = status
            results[pid] = entry
            result["phases"].append(entry)
            ctx.set_result(dict(result))
        result["status"] = stop or "done"
        if result["status"] == "cancelled":
            result.pop("failed_phase", None)
            result.pop("error_summary", None)
        ctx.set_result(dict(result))
        return result
    finally:
        # Secrets du déroulé effacés quelle qu'en soit l'issue (décision 129),
        # sauf un déroulé reprenable qui les garde dans OpenBao (décision 141)
        store = secrets if secrets is not None else secret_store.store()
        kept = (keep_secrets is not None and result.get("status") in RESUMABLE
                and keep_secrets(result["status"]))
        if kept and hasattr(store, "forget_memory"):
            # gardés pour la reprise : dans OpenBao seulement ; une copie en
            # mémoire (OpenBao coupé à la saisie) ne serait jamais relue
            # (revue m6 des tâches 9-10)
            store.forget_memory(SECRET_CLASS, ctx.run_id)
        if not kept:
            # revue I3 des tâches 9-10 : un effacement refusé (secret_id
            # tourné, droit retiré) ne change pas l'issue du déroulé ; il est
            # dit en étape (le code seul) et le balayage des déroulés finis
            # s'en charge
            try:
                store.delete_tree(SECRET_CLASS, ctx.run_id)
            except Exception as e:                        # noqa: BLE001
                code = getattr(e, "code", None) or type(e).__name__
                ctx.emit_step("secrets-cleanup", "error",
                              f"secrets of the run not erased ({code}); the background sweep will retry")
