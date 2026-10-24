#!/usr/bin/env python3
"""harvester-replication : réplication asynchrone de VMs entre clusters (v1.99.0).

Seul chemin d'écriture de la réplication : la console lance ces commandes en
actions suivies, et son fil de suivi lance `follow --all --console`.

  harvester-replication plan create --name N --from A --to B
        (--vm ns/nom ... | --selector k=v --namespace ns) --rpo 15m
        [--map-net S=D] [--map-sc S=D] [--map-ns S=D] [--map-image S=D]
        [--map-claim ns/pvc=nom] [--target-name ns/vm=nom] [--cloud-init rerun|keep]
        [--mac keep|new] [--create-namespace] [--start started|stopped]
        [--retain N] [--interval 5m] [--follow console|cli|off]
        [--skew-tolerance 10s] [--timeout NOM=S ...]
  harvester-replication plan list [--host H] [--role R]
  harvester-replication plan show P [--role R]
  harvester-replication plan delete P
  harvester-replication plan set P [--rpo D] [--paused true|false] [--follow M]
        [--skew-tolerance D] [--timeout NOM=S ...]   (NOM= retire la surcharge ;
        --interval et --retain refusés : `protect --interval`, `protect --retain`)
  harvester-replication check P [--phase protect|failover|failover-unplanned] [--force-unplanned]
  harvester-replication measure start P [--interval 5m]
  harvester-replication measure stop P
  harvester-replication measure report P [--margin 0.2]
  harvester-replication measure formula --size 2Gi --writes N --interval 5m
        [--block 2Mi] [--throughput 72Mi] [--write-size 4096]
  harvester-replication protect P [--dry-run] [--interval 5m] [--retain N]
  harvester-replication follow (--plan P | --all) [--once] [--take-over] [--console] [--log-file=PATH]
        [--timeout client=S]
  harvester-replication status [P] [--host H]
  harvester-replication resources --host H [--side source|target|any] [--role R]
  harvester-replication events (--resource UID | --plan P | --host H) [--limit N] [--before ID|DATE]
  harvester-replication failover P (--vm ns/nom ... | --all-vms --yes) [--unplanned --yes]
        [--force-unplanned] [--accept-skew] [--dry-run]   (les deux dernières avec --unplanned)
  harvester-replication resume P
  harvester-replication unprotect P --yes [--delete-backups]

P : l'identifiant (`rp-<12 hex>`) ou le nom du plan.

Options communes : --json (sortie JSON sur stdout), --db (sinon
HARVESTER_OPS_REPLICATION_DB, sinon `replication.db` du répertoire d'état),
--state-dir (défaut HARVESTER_OPS_STATE_DIR), --spec FICHIER (JSON, `-`
pour l'entrée standard : chaque clé est le nom long d'une option, la valeur
celle qu'elle prendrait ; les options de la ligne de commande l'emportent).
Commandes qui parlent aux clusters : --from-kubeconfig / --to-kubeconfig
(kubeconfig de la personne, passé par la console ; sinon celui du cluster
nommé dans la configuration ou les déclarations de la console), --timeout
NOM=S (répétable : délais de `replication.TIMEOUTS`, plus `client`, le délai
d'un appel kubectl, 30 s par défaut), --by (auteur écrit au journal, sinon
l'utilisateur système), --id (action de la console, lien vers Activité).

Progression sur stderr en STEP_EVENT|étape|statut|message (le message peut
contenir `|` : découper avec maxsplit=3) et PROGRESS_EVENT|phase|json.

Avec --json, CHAQUE sortie écrit exactement un objet JSON sur stdout. Une
erreur d'usage ou un refus avant le registre donne {"ok": false, "error",
"kind"} ; le résultat d'une commande porte toujours `ok`, `error` et `kind`
(null en cas de réussite) en plus de ses champs. Une erreur imprévue rend un
message générique, sans trace. Aucun chemin (kubeconfig, base, répertoire
d'état) ni secret n'apparaît dans une sortie ou au journal.

Les valeurs se passent sous la forme `--option=valeur` (ce que construit la
console) : une valeur qui commence par un tiret reste une valeur.

Codes de sortie : 0 réussite, 1 échec (dont `undo-timeout` : un retour
arrière a atteint son échéance unique `undo_total`, 240 s par défaut, ce qui
reste est listé dans `left`), 2 refus du contrôle préalable ou erreur
d'usage, 3 annulation (SIGTERM, SIGHUP ou SIGINT) : kind `cancelled` quand
`protect`, la mesure ou une bascule pas encore activée ont défait ce qu'elles
avaient posé, kind `interrupted` (`resume_required: true`) après une
activation ou pendant une déprotection, où rien n'est défait et `resume`
termine ; 4 objectif de RPO dépassé (`status` seulement, pour une sonde, avec
`ok: true`). Après le premier signal, les suivants sont ignorés jusqu'à
l'échéance du retour arrière, puis reprennent leur effet. `follow` arrêté
par un signal sort en 0 après avoir rendu ses baux : c'est sa fin normale.

La ligne de commande agit avec les droits du kubeconfig qu'elle reçoit ; les
rôles de la console (lecture, opérateur, administrateur) sont appliqués par
la console avant de la lancer. `--role` sur les lectures ne fait que remplir
`allowed` des actions rendues.

Bibliothèque standard seulement : le script tourne sur un hôte airgap.
"""

import argparse
import contextlib
import getpass
import json
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import cluster_decl  # noqa: E402
import replication as rp  # noqa: E402
import replication_db as db  # noqa: E402
import replication_follow as rf  # noqa: E402
import replication_run as rr  # noqa: E402
import vm_transfer as vt  # noqa: E402
from kube import Kube, cluster_config  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_CANCELLED, EXIT_RPO = 0, 1, 2, 3, 4
CANCEL_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
# délai d'un appel kubectl : un cluster pendu coûte au plus 30 s par appel
# (et par tour au suiveur, qui ne relit pas un cluster tombé dans le tour) ;
# réglable par plan (`--timeout client=S` de plan create/set) et par commande
CLIENT_TIMEOUT = rp.TIMEOUTS["client"]
ROLES = rp.ROLES

# points d'injection des tests : clients des clusters, horloge, attente
KUBE_FACTORY = None            # fn(nom du cluster, kubeconfig ou None) -> client ou None
NOW = time.time
SLEEP = time.sleep

# ce qu'une sortie ne doit jamais montrer (chemins), rempli au démarrage
_HIDDEN = []
# signal reçu, échéance du retour arrière, contexte de la commande en cours
_CANCEL = {"signal": None, "deadline": None, "ctx": None, "finished": False}


class Cancelled(rr.Cancelled):
    """SIGTERM, SIGHUP ou SIGINT : levée dans le fil principal. Sous-classe
    de `replication_run.Cancelled` : le déroulé la reconnaît et fait son
    retour arrière (sauf après une activation)."""


class UsageError(ValueError):
    """Demande refusée avant tout appel à un cluster (code 2)."""

    def __init__(self, message, kind="usage"):
        super().__init__(message)
        self.kind = kind


class Failure(Exception):
    """Échec hors du déroulé (base illisible, par exemple) : code 1."""

    def __init__(self, message, kind="failed"):
        super().__init__(message)
        self.kind = kind


class Parser(argparse.ArgumentParser):
    """argparse qui lève UsageError au lieu de quitter : même chemin (et
    même JSON) que les autres refus."""

    def error(self, message):
        raise UsageError(message)


# ---------------------------------------------------------------------------
# Sorties
# ---------------------------------------------------------------------------

def scrub(text):
    """Retire d'un texte sorti les chemins connus (kubeconfigs, base,
    répertoire d'état, configuration, répertoire personnel, temporaires),
    seulement là où ils apparaissent comme chemins, et tout chemin absolu
    sous /home, /root, /var/lib, /tmp (`replication_db.scrub_text`, la même
    fonction que le registre)."""
    return db.scrub_text(text, _HIDDEN)


def _scrub_obj(obj):
    return db.scrub_value(obj, _HIDDEN)


def _hide(*paths):
    for p in paths:
        if p:
            _HIDDEN.append(str(p))
            try:
                _HIDDEN.append(os.path.abspath(str(p)))
            except (TypeError, ValueError):
                pass
    # le registre les connaît aussi : rien ne passe par une écriture directe
    db.hide_paths(*[p for p in paths if p])


def step(sid, status, msg=""):
    # une ligne ; le message peut contenir `|` (découpe maxsplit=3)
    clean = " ".join(scrub(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def print_json(obj):
    # masqué AVANT la sérialisation : un chemin non ASCII serait échappé
    # (\uXXXX) dans le texte JSON et échapperait au masquage
    plain = json.loads(json.dumps(obj, default=str))
    print(json.dumps(_scrub_obj(plain), indent=1))


def fail_exit(want_json, sid, message, kind, code, data=None):
    """Un échec : STEP_EVENT, message sur stderr, et avec --json un seul
    objet {"ok": false, "error", "kind"} sur stdout."""
    if sid:
        step(sid, "error", message)
    print(f"error: {scrub(message)}", file=sys.stderr)
    if want_json:
        print_json({**(data or {}), "ok": False, "error": message, "kind": kind})
    return code


def emit(args, data, text_lines=()):
    """Une réussite (ou un résultat) : l'objet avec --json, sinon les lignes."""
    if args.json:
        print_json(data)
    else:
        for line in text_lines:
            print(scrub(line))


@contextlib.contextmanager
def scrubbed_journal():
    """Le journal de la réplication ne garde aucun chemin : un message
    d'erreur de kubectl peut citer le kubeconfig."""
    orig = db.record_event

    def record_event(conn, operation, *a, **kw):
        if "message" in kw:
            kw["message"] = scrub(kw["message"])
        if kw.get("facts"):
            kw["facts"] = _scrub_obj(kw["facts"])
        return orig(conn, operation, *a, **kw)
    db.record_event = record_event
    try:
        yield
    finally:
        db.record_event = orig


def _undo_total():
    ctx = _CANCEL.get("ctx")
    t = dict.get(ctx.timeouts, "undo_total") if ctx is not None else None
    return int(t or rp.TIMEOUTS["undo_total"])


def _on_cancel_signal(signum, _frame):
    """Premier signal : `Cancelled` dans le fil principal (le déroulé défait
    ce qu'il a posé). Les suivants, et tout signal pendant un retour arrière,
    sont ignorés jusqu'à l'échéance de ce retour arrière (`undo_total`) ;
    passée l'échéance, un signal reprend son effet par défaut."""
    if _CANCEL.get("finished"):
        return                      # résultat construit : la commande n'est plus annulable
    name = signal.Signals(signum).name
    ctx = _CANCEL.get("ctx")
    undoing = ctx is not None and ctx.undo_until is not None
    if _CANCEL["signal"] is None and not undoing:
        _CANCEL["signal"] = name
        _CANCEL["deadline"] = NOW() + _undo_total()
        raise Cancelled(name)
    deadline = ctx.undo_until if undoing else _CANCEL["deadline"]
    if deadline is not None and NOW() < deadline:
        return
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _block_signals():
    """Le résultat de la commande est construit : un signal qui arrive
    maintenant reste en attente, puis est jeté (`cancel_on_signals`). Une
    commande terminée n'est jamais dite annulée. Le drapeau couvre un
    processus à plusieurs fils, où le signal peut arriver par un autre fil."""
    _CANCEL["finished"] = True
    if threading.current_thread() is threading.main_thread():
        signal.pthread_sigmask(signal.SIG_BLOCK, CANCEL_SIGNALS)


@contextlib.contextmanager
def cancel_on_signals():
    """Gestionnaire posé le temps de la commande (fil principal seulement :
    la console peut charger ce module sans appeler main)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    saved = {sig: signal.signal(sig, _on_cancel_signal) for sig in CANCEL_SIGNALS}
    try:
        yield
    finally:
        # un signal resté en attente après la fin est jeté (SIG_IGN avant le
        # déblocage), puis les gestionnaires d'avant reviennent
        for sig in CANCEL_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, CANCEL_SIGNALS)
        for sig, h in saved.items():
            signal.signal(sig, h)
        _CANCEL.update(ctx=None, finished=False)


# ---------------------------------------------------------------------------
# Registre, plans, clusters
# ---------------------------------------------------------------------------

def open_db(args):
    try:
        return db.connect(args.db or None)
    except Exception as e:                      # noqa: BLE001
        raise Failure(f"cannot open the replication database ({type(e).__name__})") from None


def find_plan(conn, ref):
    plan = db.get_plan(conn, ref) if rp.PLAN_ID_RE.match(ref or "") else db.get_plan_by_name(conn, ref)
    if plan is None:
        raise UsageError(f"unknown plan: {str(ref)[:80]}", "unknown-plan")
    return plan


class Unreachable:
    """Client d'un cluster que cette console ne connaît pas (ou plus) :
    chaque appel échoue comme un cluster injoignable. La déprotection et la
    bascule non planifiée continuent alors avec l'autre côté."""

    def __init__(self, name):
        self.name = name

    def _fail(self, *_a, **_k):
        raise RuntimeError(f"Unable to connect to the server: cluster {self.name} is not declared on this console")

    get = list = create = replace = patch = delete = _fail


def cluster_client(name, kubeconfig=None, timeout=CLIENT_TIMEOUT):
    """Client kubectl d'un cluster : le kubeconfig donné, sinon celui de la
    configuration ou des déclarations de la console. None s'il est inconnu."""
    if KUBE_FACTORY is not None:
        return KUBE_FACTORY(name, kubeconfig)
    if not kubeconfig:
        try:
            kubeconfig = cluster_config(name).get("kubeconfig")
        except SystemExit:
            # son message cite les chemins de la configuration : jamais montré
            return None
        if not kubeconfig:
            return None
    _hide(kubeconfig)
    return Kube(kubeconfig, timeout=timeout)


_RANCHER_RE = re.compile(r"server:\s*['\"]?https?://\S+/k8s/clusters/")


def through_rancher(kubeconfig, client):
    """La cible est-elle jointe par le mandataire de Rancher ? (avertissement
    `target-through-rancher` du contrôle)"""
    if getattr(client, "through_rancher", None) is not None:
        return bool(client.through_rancher)
    path = kubeconfig or getattr(client, "kubeconfig", None)
    if not path:
        return False
    try:
        return bool(_RANCHER_RE.search(Path(path).read_text(errors="replace")))
    except OSError:
        return False


def _pairs(values, kind, what):
    out = {}
    for v in values or []:
        a, sep, b = str(v).partition("=")
        if not sep or not a:
            raise UsageError(f"{what}: expected source=target, got {str(v)[:80]!r}", kind)
        out[a] = b
    return out


def parse_timeouts(values):
    """`--timeout NOM=S` d'une commande : (surcharges, délai client ou None
    s'il n'est pas donné)."""
    raw = _pairs(values, "bad-timeout", "--timeout")
    client = None
    if "client" in raw:
        try:
            client = rp.parse_duration(raw.pop("client"))
        except rp.PlanError:
            raise UsageError("--timeout client: a duration is expected", "bad-timeout") from None
        if not 1 <= client <= 3600:
            raise UsageError("--timeout client: 1 s to 1 h", "bad-timeout")
    try:
        return rp.normalize_timeouts(raw), client
    except rp.PlanError as e:
        raise UsageError(str(e), e.code) from None


def system_user():
    try:
        return getpass.getuser()
    except Exception:                           # noqa: BLE001
        return ""


def plan_client_timeout(plan):
    """Délai d'un appel kubectl réglé sur le plan (`timeouts.client`)."""
    return int(((plan.get("options") or {}).get("timeouts") or {}).get("client") or CLIENT_TIMEOUT)


def make_ctx(args, conn, plan):
    overrides, client_timeout = parse_timeouts(getattr(args, "timeout", None))
    client_timeout = client_timeout or plan_client_timeout(plan)
    fk, tk = getattr(args, "from_kubeconfig", None), getattr(args, "to_kubeconfig", None)
    _hide(fk, tk)
    src = cluster_client(plan["source_cluster"], fk, client_timeout) or Unreachable(plan["source_cluster"])
    dst = cluster_client(plan["target_cluster"], tk, client_timeout) or Unreachable(plan["target_cluster"])
    ctx = rr.Ctx(conn, plan["id"], src, dst, emit=step, sleep=SLEEP, now=NOW, timeouts=overrides,
                 by=args.by or system_user(), action_id=args.id or "",
                 through_rancher=through_rancher(tk, dst))
    ctx.on_finish = _block_signals
    _CANCEL["ctx"] = ctx
    return ctx


def utcnow():
    return datetime.fromtimestamp(NOW(), timezone.utc)


def _text_result(res):
    lines = [f"{res.get('code')}: {res.get('message') or ''}".rstrip(": ")]
    for f in res.get("findings") or []:
        facts = ", ".join(f"{k}={v}" for k, v in (f.get("facts") or {}).items())
        lines.append(f"  [{f.get('level')}] {f.get('code')}" + (f": {facts}" if facts else ""))
    for a in res.get("advisories") or []:
        lines.append(f"  [{a.get('level')}] {a.get('code')}: {(a.get('facts') or {}).get('advice', '')}")
    for d in res.get("dry_run") or []:
        lines.append(f"  dry-run {d.get('object')} {d.get('name')}: {'ok' if d.get('ok') else d.get('message')}")
    for k in ("left", "pending"):
        for x in res.get(k) or []:
            lines.append(f"  {k}: {x}")
    lost = res.get("lost_after") or {}
    for vm, at in lost.items():
        lines.append(f"  {vm}: writes after {at} are lost (skew {(res.get('skew_s') or {}).get(vm, 0)} s)")
    for vm, at in (res.get("point_at") or {}).items():
        if vm not in lost:
            # bascule planifiée : la cible porte l'état de la source à son arrêt
            lines.append(f"  {vm}: target holds the source state at its stop (last point {at})")
    return lines


def _kind(code, res):
    """`kind` d'un résultat en échec. En sortie 3 : `cancelled` (ce qui
    avait été posé est défait) ou `interrupted` (après une activation, ou une
    déprotection commencée : rien n'est défait, `resume` termine)."""
    if res.get("code") == "interrupted":
        return "interrupted"
    if code == EXIT_CANCELLED:
        return "cancelled"
    return res.get("code") or "failed"


def finish(args, res):
    """Le résultat d'une commande du déroulé : code de sortie et objet."""
    _block_signals()                # la commande est finie : plus d'annulation
    code = int(res.get("exit", EXIT_FAIL))
    if _CANCEL["signal"] and code == EXIT_FAIL and res.get("code") != "undo-timeout":
        # annulation reçue pendant une étape qui ne la distingue pas d'un échec
        code = EXIT_CANCELLED
    ok = code == EXIT_OK
    out = {"ok": ok, "error": None if ok else (res.get("message") or res.get("code") or "failed"),
           "kind": None if ok else _kind(code, res), "resume_required": res.get("code") == "interrupted"}
    out.update({k: v for k, v in res.items() if k != "exit"})
    if not ok:
        print(f"error: {scrub(out['error'])}", file=sys.stderr)
    emit(args, out, _text_result(res))
    return code


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def _plan_event(conn, plan, step_name, code, message, facts, args):
    db.record_event(conn, "plan", step=step_name, plan_id=plan["id"], host=plan["target_cluster"],
                    code=code, message=message, facts=facts, by=args.by or system_user(),
                    action_id=args.id or "")


def _known(name):
    try:
        return cluster_client(name) is not None
    except Exception:                           # noqa: BLE001
        return False


def plan_request(args):
    """Demande de `plan create`, telle que la lit `replication.normalize_plan`."""
    mappings = {"networks": _pairs(args.map_net, "bad-mapping", "--map-net"),
                "storage_classes": _pairs(args.map_sc, "bad-mapping", "--map-sc"),
                "namespaces": _pairs(args.map_ns, "bad-mapping", "--map-ns"),
                "images": _pairs(args.map_image, "bad-mapping", "--map-image"),
                "claims": _pairs(args.map_claim, "bad-mapping", "--map-claim"),
                "target_names": _pairs(args.target_name, "bad-mapping", "--target-name")}
    options = {}
    for key, val in (("cloud_init", args.cloud_init), ("mac", args.mac), ("start", args.start),
                     ("follow", args.follow), ("skew_tolerance_s", args.skew_tolerance)):
        if val is not None:
            options[key] = val
    if args.create_namespace:
        options["create_namespace"] = True
    if args.timeout:
        options["timeouts"] = _pairs(args.timeout, "bad-timeout", "--timeout")
    req = {"name": args.name, "source_cluster": args.source, "target_cluster": args.target,
           "rpo_objective_s": args.rpo, "interval_s": args.interval, "vms": args.vm or [],
           "selector": args.selector or "", "namespace": args.namespace or "",
           "mappings": {k: v for k, v in mappings.items() if v}, "options": options}
    if args.retain is not None:
        req["retain"] = args.retain
    return req


def cmd_plan_create(args):
    req = plan_request(args)
    try:
        plan = rp.normalize_plan(req)
    except rp.PlanError as e:
        raise UsageError(str(e), e.code) from None
    for side in ("source_cluster", "target_cluster"):
        if not _known(plan[side]):
            raise UsageError(f"unknown cluster: {plan[side]}", "unknown-cluster")
    conn = open_db(args)
    try:
        stored = db.insert_plan(conn, plan, created_by=args.by or system_user(), now=utcnow())
    except db.NameTaken as e:
        raise UsageError(str(e), "name-taken") from None
    _plan_event(conn, stored, "create", "draft", f"plan {stored['name']} created", {"name": stored["name"]}, args)
    step("plan", "done", f"plan {stored['name']} created ({stored['id']})")
    emit(args, {"ok": True, "error": None, "kind": None, "plan": stored},
         [f"plan {stored['name']} created: {stored['id']} ({stored['source_cluster']} to "
          f"{stored['target_cluster']}, RPO objective {stored['rpo_objective_s']} s)"])
    return EXIT_OK


def _role(args):
    return getattr(args, "role", None) or None


def plan_rows(conn, plans, role=None, side="any"):
    return db.plan_rows(conn, plans, utcnow(), role=role, side=side)


def cmd_plan_list(args):
    conn = open_db(args)
    rows = plan_rows(conn, db.list_plans(conn, host=args.host or None), _role(args))
    emit(args, {"ok": True, "error": None, "kind": None, "plans": rows},
         [f"{r['name']:24} {r['id']}  {r['state']:13} {r['source_host']} to {r['target_host']}  "
          f"RPO {r['rpo_held_s'] if r['rpo_held_s'] is not None else '-'}/{r['rpo_objective_s']} s  "
          f"{len(r['resources'])} VM(s)" for r in rows] or ["no plan"])
    return EXIT_OK


def cmd_plan_show(args):
    conn = open_db(args)
    plan = find_plan(conn, args.plan)
    now = utcnow()
    v = db.plan_view(conn, plan, now)
    row = rp.plan_row(plan, v["resources"], v["volumes"], v["ticks"], v["events"], now, v["vm_running"],
                      role=_role(args), health=v["health"])
    status = rp.plan_status(plan, v["resources"], v["volumes"], v["ticks"], now, v["vm_running"], v["health"])
    runs = db.list_runs(conn, plan["id"])[-10:]
    data = {"ok": True, "error": None, "kind": None, "plan": plan, "row": row, "status": status,
            "volumes": v["volumes"], "runs": runs, "lock": db.get_lock(conn, plan["id"]),
            "lease": db.get_lease(conn, plan["id"])}
    lines = [f"{plan['name']} ({plan['id']}): {plan['state']}, {plan['source_cluster']} to {plan['target_cluster']}",
             f"  RPO objective {plan['rpo_objective_s']} s, interval {plan.get('interval_s') or '-'} s, "
             f"retain {plan['retain']}"]
    for r in row["resources"]:
        lines.append(f"  {r['namespace']}/{r['name']}: {r['state']}, RPO held "
                     f"{r['rpo_held_s'] if r['rpo_held_s'] is not None else '-'} s")
    emit(args, data, lines)
    return EXIT_OK


@contextlib.contextmanager
def plan_lock(conn, plan, command):
    """Le verrou des commandes du plan, le temps d'une écriture du registre :
    refus `locked` (code 2) tant qu'une autre commande le tient."""
    owner = db.owner_id("cli")
    try:
        db.acquire_lock(conn, plan["id"], command, owner, now=utcnow())
    except db.Locked as e:
        raise UsageError(f"plan {plan['name']} is busy: {e.holder.get('command')} is running", "locked") from None
    try:
        yield
    finally:
        db.release_lock(conn, plan["id"], owner)


def cmd_plan_delete(args):
    conn = open_db(args)
    plan = find_plan(conn, args.plan)
    try:
        with plan_lock(conn, plan, "plan-delete"):
            db.delete_plan(conn, plan["id"])
    except db.StateConflict as e:
        raise UsageError(f"plan {plan['name']} is {e.current}: only a draft or unprotected plan can be deleted",
                         "state") from None
    _plan_event(conn, plan, "delete", "deleted", f"plan {plan['name']} deleted", {"name": plan["name"]}, args)
    step("plan", "done", f"plan {plan['name']} deleted")
    emit(args, {"ok": True, "error": None, "kind": None, "plan": plan["id"], "deleted": True},
         [f"plan {plan['name']} deleted"])
    return EXIT_OK


def _bool(value, what):
    v = str(value).lower()
    if v in ("true", "yes", "on", "1"):
        return True
    if v in ("false", "no", "off", "0"):
        return False
    raise UsageError(f"{what}: true or false", "bad-option")


def cmd_plan_set(args):
    if args.interval is not None:
        raise UsageError("the interval lives in the source backup jobs: change it with "
                         "`protect P --interval D` (plan set cannot)", "not-settable")
    if args.retain is not None:
        raise UsageError("retain lives in the source backup jobs: change it with "
                         "`protect P --retain N` (plan set cannot)", "not-settable")
    conn = open_db(args)
    plan = find_plan(conn, args.plan)
    fields, options = {}, {}
    if args.rpo is not None:
        fields["rpo_objective_s"] = args.rpo
    if args.paused is not None:
        options["paused"] = _bool(args.paused, "--paused")
    if args.follow is not None:
        options["follow"] = args.follow
    if args.skew_tolerance is not None:
        options["skew_tolerance_s"] = args.skew_tolerance
    timeouts = _pairs(args.timeout, "bad-timeout", "--timeout") if args.timeout else None
    if not fields and not options and timeouts is None:
        raise UsageError("nothing to set: give --rpo, --paused, --follow, --skew-tolerance or --timeout")
    with plan_lock(conn, plan, "plan-set"):
        if timeouts is not None:
            # relu sous le verrou : `NOM=` retire une surcharge du plan tel qu'il est
            plan = db.get_plan(conn, plan["id"]) or plan
            cur = dict((plan.get("options") or {}).get("timeouts") or {})
            for k, v in timeouts.items():
                if v == "":
                    cur.pop(k, None)           # NOM= : retour à la valeur par défaut
                else:
                    cur[k] = v
            options["timeouts"] = cur
        if options:
            fields["options"] = options
        try:
            new = db.update_plan(conn, plan["id"], now=utcnow(), **fields)
        except rp.PlanError as e:
            raise UsageError(str(e), e.code) from None
    changed = {k: new[k] for k in ("rpo_objective_s",) if "rpo_objective_s" in fields}
    changed.update({k: (new.get("options") or {}).get(k) for k in options})
    _plan_event(conn, new, "set", "settings", "settings changed: " + ", ".join(sorted(changed)), changed, args)
    step("plan", "done", "settings changed: " + ", ".join(sorted(changed)))
    emit(args, {"ok": True, "error": None, "kind": None, "plan": new, "changed": changed},
         [f"{plan['name']}: " + ", ".join(f"{k}={v}" for k, v in sorted(changed.items()))])
    return EXIT_OK


# ---------------------------------------------------------------------------
# check, mesure, protection, bascule, déprotection, reprise
# ---------------------------------------------------------------------------

def _plan_ctx(args):
    conn = open_db(args)
    plan = find_plan(conn, args.plan)
    return conn, plan, make_ctx(args, conn, plan)


def console_placement(plan):
    """Conseil (jamais bloquant) rendu par `check` : la console qui suit et
    bascule doit tourner du côté du secours ou hors des deux sites. Sur le
    site source, elle disparaît avec lui au moment où il faut basculer."""
    return {"code": "console-placement", "level": "info",
            "facts": {"source": plan["source_cluster"], "target": plan["target_cluster"],
                      "advice": "run the console that follows and fails over this plan on the backup side "
                                f"({plan['target_cluster']}) or outside both sites, never only on the source "
                                "site: it would be lost with it"}}


def cmd_check(args):
    if args.force_unplanned and args.phase != "failover-unplanned":
        raise UsageError("--force-unplanned goes with --phase failover-unplanned")
    _conn, plan, ctx = _plan_ctx(args)
    if args.phase != "protect":
        # la bascule à blanc : contrôles, carnets et dry-run serveur, rien
        # d'écrit, ni sur les clusters ni au journal
        res = rr.failover(ctx, planned=args.phase == "failover", force_unplanned=args.force_unplanned,
                          dry_run=True, record=False)
        return finish(args, dict(res, advisories=[console_placement(plan)]))
    step("check", "running", "reading both clusters")
    try:
        src = rr.collect_source(ctx)
        skipped = rr._failed_over(ctx, src)
        src["vms"] = [v for v in src.get("vms") or [] if v["uid"] not in skipped]
        dst = rr.collect_target(ctx, [rp.target_ref(ctx.plan, {"namespace": v["namespace"], "name": v["name"]})[0]
                                      for v in src.get("vms") or []])
        findings = rp.check(src, dst, dict(ctx.plan, id=ctx.plan_id, phase="protect"))
    except Exception as e:                      # noqa: BLE001
        if isinstance(e, rr.Cancelled):
            raise
        step("check", "error", str(e)[:400])
        return finish(args, {"exit": EXIT_FAIL, "plan": plan["id"], "state": plan["state"],
                             "code": rr.error_code(e), "message": f"pre-check failed: {e}",
                             "advisories": [console_placement(plan)]})
    _block_signals()                # lecture finie : plus d'annulation
    blocked = rp.blocking(findings)
    step("check", "error" if blocked else "done",
         "the pre-check refuses this protection" if blocked else "no blocker")
    return finish(args, {"exit": EXIT_USAGE if blocked else EXIT_OK, "plan": plan["id"], "state": plan["state"],
                         "code": "check" if blocked else "ok", "phase": "protect",
                         "message": "the pre-check refuses this protection" if blocked else "no blocker",
                         "findings": findings, "skipped": sorted(skipped),
                         "advisories": [console_placement(plan)]})


def cmd_measure_start(args):
    _conn, _plan, ctx = _plan_ctx(args)
    return finish(args, rr.measure_start(ctx, interval=args.interval or 300))


def cmd_measure_stop(args):
    _conn, _plan, ctx = _plan_ctx(args)
    return finish(args, rr.measure_stop(ctx))


def cmd_measure_report(args):
    conn = open_db(args)
    plan = find_plan(conn, args.plan)
    if not 0 <= args.margin < 1:
        raise UsageError("--margin: from 0 to below 1")
    rep = rr.measure_report(conn, plan["id"], margin=args.margin)
    lines = [f"measured every {rep['measured_step_s']} s, objective {rep['objective_s']} s, "
             f"recommended interval {rep.get('recommended_interval_s') or '-'} s"]
    for vm in rep["vms"]:
        lines.append(f"  {vm['vm_uid']}: {vm['passes']} pass(es), interval {vm.get('recommended_interval_s') or '-'}"
                     f" s, tenable {vm.get('tenable')}" + (" (insufficient data)" if vm.get("insufficient") else ""))
    emit(args, {"ok": True, "error": None, "kind": None, "plan": plan["id"], "report": rep}, lines)
    return EXIT_OK


def _quantity(value, what):
    n = vt.parse_quantity(str(value))
    if not n or n <= 0:
        raise UsageError(f"{what}: a size is expected (2Gi, 512Mi, 1073741824)")
    return int(n)


def cmd_measure_formula(args):
    size = _quantity(args.size, "--size")
    block = _quantity(args.block, "--block")
    if block not in rp.BLOCK_SIZES:
        raise UsageError("--block: 2Mi or 16Mi")
    try:
        interval = rp.parse_duration(args.interval)
    except rp.PlanError:
        raise UsageError("--interval: a duration is expected (5m)", "bad-interval") from None
    if interval <= 0:
        raise UsageError("--interval: a duration is expected (5m)", "bad-interval")
    if args.writes < 0 or args.write_size <= 0:
        raise UsageError("--writes and --write-size: positive numbers")
    throughput = _quantity(args.throughput, "--throughput") if args.throughput else None
    f = rr.measure_formula(size, args.writes, interval, block, throughput, args.write_size)
    fc = f["forecast"]
    lines = [f"blocks of {block} B over {size} B: sequential {fc['sequential_bytes']} B, scattered "
             f"{fc['scattered_estimate_bytes']} B, worst case {fc['worst_case_bytes']} B"]
    if throughput:
        for k in ("sequential", "scattered_estimate", "worst_case"):
            lines.append(f"  {k}: send {f[k]['send_s']:.1f} s, RPO {f[k]['rpo_s']:.0f} s")
    emit(args, {"ok": True, "error": None, "kind": None, "formula": f}, lines)
    return EXIT_OK


def cmd_protect(args):
    _conn, _plan, ctx = _plan_ctx(args)
    return finish(args, rr.protect(ctx, interval=args.interval, dry_run=args.dry_run, retain=args.retain))


def _vm_refs(values):
    try:
        return [rp._ref(v, "--vm") for v in values or []] or None
    except rp.PlanError as e:
        raise UsageError(str(e), e.code) from None


def cmd_failover(args):
    vms = _vm_refs(args.vm)
    if args.force_unplanned and not args.unplanned:
        raise UsageError("--force-unplanned goes with --unplanned")
    if args.accept_skew and not args.unplanned:
        raise UsageError("--accept-skew goes with --unplanned")
    if vms and args.all_vms:
        raise UsageError("--vm and --all-vms: choose one")
    if not args.dry_run:
        if not vms and not args.all_vms:
            raise UsageError("failing every VM of the plan over needs --all-vms and --yes (or name them with --vm)",
                             "confirmation-required")
        if args.all_vms and not args.yes:
            raise UsageError("--all-vms needs --yes", "confirmation-required")
    _conn, _plan, ctx = _plan_ctx(args)
    return finish(args, rr.failover(ctx, vms=vms, planned=not args.unplanned, accept_skew=args.accept_skew,
                                    force_unplanned=args.force_unplanned, yes=args.yes, dry_run=args.dry_run))


def cmd_resume(args):
    # pas de --yes : la reprise refait la commande rangée avec la confirmation
    # donnée à son départ (mode, VMs, --force-unplanned de sa ligne `runs`)
    _conn, _plan, ctx = _plan_ctx(args)
    return finish(args, rr.resume(ctx))


def cmd_unprotect(args):
    if not args.yes:
        # les volumes de secours de la cible sont supprimés
        raise UsageError("unprotect deletes the standby volumes on the target: it needs --yes",
                         "confirmation-required")
    _conn, _plan, ctx = _plan_ctx(args)
    return finish(args, rr.unprotect(ctx, delete_backups=args.delete_backups, yes=args.yes))


# ---------------------------------------------------------------------------
# Suiveur
# ---------------------------------------------------------------------------

def follow_client_timeout(conn, name, override=None):
    """Délai d'un appel kubectl du suiveur vers un cluster : `follow
    --timeout client=S`, sinon le plus grand `timeouts.client` des plans où
    ce cluster est source ou cible, sinon 30 s."""
    if override:
        return override
    vals = [plan_client_timeout(p) for p in db.list_plans(conn, host=name)]
    return max(vals) if vals else CLIENT_TIMEOUT


def _round_line(report):
    if report.get("error"):
        return f"round failed: {report['error']}"
    return (f"followed {len(report.get('followed') or [])}, skipped {len(report.get('skipped') or {})}, "
            f"resyncs {report.get('resyncs', 0)}, applied {report.get('applied', 0)}, "
            f"events {report.get('events', 0)}, unreachable {', '.join(report.get('unreachable') or []) or 'none'}")


REOPEN_SIGNAL = signal.SIGUSR1


def redirect_stderr(path):
    """Branche le descripteur 2 sur `path` (ajout, 0600 à la création) : le
    journal du suiveur ET ce qui s'écrit hors journal (trace d'une erreur)."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        sys.stderr.flush()
        os.dup2(fd, 2)
    finally:
        os.close(fd)


def install_log_reopen(path):
    """v1.99.0 : la console renomme le journal trop gros puis envoie
    SIGUSR1 ; le suiveur rouvre le fichier sans redémarrer (SIGHUP reste un
    signal d'arrêt). Rend l'ancien gestionnaire."""
    redirect_stderr(path)

    def reopen(_signum, _frame):
        try:
            redirect_stderr(path)
        except OSError:
            pass                                # garde l'ancien fichier plutôt que de tomber
    return signal.signal(REOPEN_SIGNAL, reopen)


def cmd_follow(args):
    if args.log_file and threading.current_thread() is threading.main_thread():
        # au plus tôt : un SIGUSR1 ne doit jamais trouver l'action par défaut
        install_log_reopen(args.log_file)
    overrides, client_override = parse_timeouts(args.timeout)
    if overrides:
        raise UsageError("follow: only --timeout client=S (the plan wait settings are set with plan set)",
                         "bad-timeout")
    conn = open_db(args)
    plan_ids = None
    if args.plan:
        plan_ids = [find_plan(conn, args.plan)["id"]]
        modes = ("console", "cli")
    else:
        # le fil de la console prend les plans `follow: console`, un --all à
        # la main ceux en `cli` ; --plan P suit l'un ou l'autre
        modes = ("console",) if args.console else ("cli",)
    owner = db.owner_id("console" if args.console else "cli")

    def log(code, level, message):
        if args.console:
            sys.stderr.write(f"{utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')} {level} {code}: "
                             f"{' '.join(scrub(message).split())}\n")
            sys.stderr.flush()

    def client(name):
        c = cluster_client(name, timeout=follow_client_timeout(conn, name, client_override))
        if c is None:
            raise LookupError(f"unknown cluster {name}")
        return c

    def refresh_timeouts():
        # réglage du plan changé à chaud : pris au tour suivant
        for name, c in follower.clients.items():
            if hasattr(c, "timeout"):
                c.timeout = follow_client_timeout(conn, name, client_override)

    follower = rf.Follower(conn, client, owner, now=NOW, modes=modes, plan_ids=plan_ids,
                           take_over=args.take_over, emit=log)
    stopper = rf.Stopper()
    saved = {s: signal.getsignal(s) for s in CANCEL_SIGNALS}
    if threading.current_thread() is threading.main_thread():
        stopper.install(CANCEL_SIGNALS)
    try:
        if args.once:
            follower.stopper = stopper
            try:
                report = follower.once_safe()
            except rf.Interrupted:
                follower.close()
                return fail_exit(args.json, "follow", "follow cancelled during a cluster call", "cancelled",
                                 EXIT_CANCELLED)
            follower.close()
            step("follow", "error" if report.get("error") else "done", _round_line(report))
            data = {"ok": not report.get("error"), "error": report.get("error"),
                    "kind": "failed" if report.get("error") else None, "report": report}
            emit(args, data, [_round_line(report)])
            return EXIT_FAIL if report.get("error") else EXIT_OK

        def on_round(report):
            refresh_timeouts()
            if args.console:
                log("round", "error" if report.get("error") else "info", _round_line(report))
            else:
                step("follow", "error" if report.get("error") else "done", _round_line(report))
            on_round.last = report
        on_round.last = None
        res = rf.follow_loop(follower, stopper, sleep=SLEEP, on_round=on_round)
        sig = signal.Signals(res["signal"]).name if res.get("signal") else None
        step("follow", "done", f"stopped after {res['rounds']} round(s)" + (f" ({sig})" if sig else ""))
        emit(args, {"ok": True, "error": None, "kind": None, "rounds": res["rounds"], "stopped": res["stopped"],
                    "signal": sig, "last": on_round.last},
             [f"follower stopped after {res['rounds']} round(s)"])
        return EXIT_OK
    finally:
        if threading.current_thread() is threading.main_thread():
            for s, h in saved.items():
                signal.signal(s, h)


# ---------------------------------------------------------------------------
# Lectures : état, ressources, journal
# ---------------------------------------------------------------------------

def cmd_status(args):
    conn = open_db(args)
    plans = [find_plan(conn, args.plan)] if args.plan else db.list_plans(conn, host=args.host or None)
    if args.plan and args.host and args.host not in (plans[0]["source_cluster"], plans[0]["target_cluster"]):
        plans = []
    now = utcnow()
    out, lines = [], []
    for p in plans:
        v = db.plan_view(conn, p, now)
        st = rp.plan_status(p, v["resources"], v["volumes"], v["ticks"], now, v["vm_running"], v["health"])
        st.update(name=p["name"], source_host=p["source_cluster"], target_host=p["target_cluster"],
                  last_events=db.plan_events(conn, p["id"], limit=db.VIEW_EVENTS))
        out.append(st)
        lines.append(f"{p['name']}: {p['state']}, RPO held {st['rpo_held_s'] if st['rpo_held_s'] is not None else '-'}"
                     f"/{st['rpo_objective_s']} s, backlog {st['backlog']}, skew {st['skew_s']} s"
                     + (", OBJECTIVE EXCEEDED" if st["exceeded"] else ""))
        for vm in st["vms"]:
            lines.append(f"  {vm['namespace']}/{vm['name']}: {vm['state']}, RPO held "
                         f"{vm['rpo_held_s'] if vm['rpo_held_s'] is not None else '-'} s")
    exceeded = any(s["exceeded"] for s in out)
    emit(args, {"ok": True, "error": None, "kind": None, "exceeded": exceeded, "plans": out}, lines or ["no plan"])
    return EXIT_RPO if exceeded else EXIT_OK


def _action_text(a):
    """Une action d'une ressource ; refusée, avec sa raison (v1.99.0 : le
    texte proposait `unprotect-delete-backups` que la déprotection refuse)."""
    if a.get("allowed", True):
        return a["name"]
    return f"{a['name']} (not allowed: {a.get('reason') or 'role'})"


def cmd_resources(args):
    conn = open_db(args)
    now = utcnow()
    rows = db.host_rows(conn, args.host, args.side, now, role=_role(args))
    emit(args, {"ok": True, "error": None, "kind": None, "host": args.host, "side": args.side, "resources": rows},
         [f"{x['plan_name']:20} {x['side']:6} {x['namespace']}/{x['name']}: {x['state']}, peer "
          f"{x['peer_host']}, actions {', '.join(_action_text(a) for a in x['actions']) or '-'}" for x in rows]
         or [f"no resource of {args.host} in a plan"])
    return EXIT_OK


def cmd_events(args):
    conn = open_db(args)
    try:
        if args.resource:
            evs = db.resource_events(conn, args.resource, limit=args.limit, before=args.before)
        elif args.plan:
            evs = db.plan_events(conn, find_plan(conn, args.plan)["id"], limit=args.limit, before=args.before)
        else:
            evs = db.host_events(conn, args.host, limit=args.limit, before=args.before)
    except ValueError as e:
        raise UsageError(str(e)) from None
    nxt = evs[-1]["id"] if len(evs) >= max(1, min(args.limit, 500)) else None
    emit(args, {"ok": True, "error": None, "kind": None, "events": evs, "next_before": nxt},
         [f"{e['ts']} {e['level']:5} {e['operation']}/{e['step']} {e['code']}: {e['message']}" for e in evs]
         or ["no event"])
    return EXIT_OK


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def _spec_tokens(path):
    """Les options d'un fichier --spec, en `--option=valeur`."""
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text()
        data = json.loads(text)
    except (OSError, ValueError):
        raise UsageError("--spec: unreadable JSON request") from None
    if not isinstance(data, dict):
        raise UsageError("--spec: a JSON object is expected")
    out = []
    for key, val in data.items():
        if not isinstance(key, str) or not re.match(r"^[a-z][a-z0-9_-]*$", key) or key in ("spec", "json"):
            raise UsageError(f"--spec: invalid key {str(key)[:40]!r}")
        opt = "--" + key.replace("_", "-")
        for v in (val if isinstance(val, list) else [val]):
            if isinstance(v, bool):
                if v:
                    out.append(opt)
            elif isinstance(v, (str, int, float)):
                out.append(f"{opt}={v}")
            elif v is not None:
                raise UsageError(f"--spec: {key}: a string, number, boolean or list is expected")
    return out


EPILOG = """exit codes:
  0  ok
  1  failure; also kind undo-timeout: a rollback reached its deadline (timeout
     undo_total, 240 s by default), what is left is listed in `left`
  2  usage error or refusal (pre-check, confirmation, state, state-changed,
     locked, open-run)
  3  cancelled by SIGTERM, SIGHUP or SIGINT: kind cancelled (what the command
     had set is undone) or kind interrupted (after an activation, or during
     unprotect: nothing undone, resume_required true, run resume)
  4  status only: an RPO objective is exceeded (the JSON still has ok: true)

After the first signal, further signals are ignored until the rollback
deadline (undo_total); after it, a signal ends the process."""


def build_parser():
    common = Parser(add_help=False)
    common.add_argument("--json", action="store_true", help="JSON output on stdout")
    common.add_argument("--db", help="replication database (default: replication.db in the state directory)")
    common.add_argument("--state-dir", help="console state directory (default HARVESTER_OPS_STATE_DIR)")
    common.add_argument("--spec", help="JSON request: option names as keys ('-' for stdin)")
    common.add_argument("--by", help="author written to the journal (default: the system user)")
    common.add_argument("--id", help="console action id (link to Activity)")

    clusters = Parser(add_help=False)
    clusters.add_argument("--from-kubeconfig", help="kubeconfig for the source cluster")
    clusters.add_argument("--to-kubeconfig", help="kubeconfig for the target cluster")
    clusters.add_argument("--timeout", action="append", metavar="NAME=SECONDS",
                          help="bounded wait override (repeatable), or client=S for each kubectl call")

    role = Parser(add_help=False)
    role.add_argument("--role", choices=ROLES, help="fill `allowed` of the actions for this console role")

    ap = Parser(prog="harvester-replication", description="Asynchronous VM replication between Harvester clusters.",
                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def leaf(subs, name, fn, parents, words, help_):
        sp = subs.add_parser(name, parents=parents, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    pp = sub.add_parser("plan", help="replication plans kept by the console").add_subparsers(dest="pcmd",
                                                                                            required=True)
    sp = leaf(pp, "create", cmd_plan_create, [common], 2, "create a plan (no write on the clusters)")
    sp.add_argument("--name", required=True)
    sp.add_argument("--from", dest="source", required=True, help="source cluster name")
    sp.add_argument("--to", dest="target", required=True, help="target cluster name")
    sp.add_argument("--vm", action="append", metavar="NS/NAME", help="VM to protect (repeatable)")
    sp.add_argument("--selector", metavar="KEY=VALUE", help="VM label selector, resolved at each protect")
    sp.add_argument("--namespace", help="namespace of the selector")
    sp.add_argument("--rpo", required=True, help="RPO objective (15m, 1h)")
    for opt, what in (("--map-net", "network ns/name=ns/name"), ("--map-sc", "storage class"),
                      ("--map-ns", "namespace"), ("--map-image", "image ns/name=ns/name"),
                      ("--map-claim", "PVC ns/claim=name"), ("--target-name", "VM ns/name=name")):
        sp.add_argument(opt, action="append", metavar="SRC=DST", help=f"{what} mapping (repeatable)")
    sp.add_argument("--cloud-init", choices=rp.OPTION_CHOICES["cloud_init"])
    sp.add_argument("--mac", choices=rp.OPTION_CHOICES["mac"])
    sp.add_argument("--create-namespace", action="store_true")
    sp.add_argument("--start", choices=rp.OPTION_CHOICES["start"], help="state of the VM after a failover")
    sp.add_argument("--retain", type=int, help=f"backups kept ({rp.RETAIN_MIN} to {rp.RETAIN_MAX})")
    sp.add_argument("--interval", help="backup interval (5m); derived from a measure otherwise")
    sp.add_argument("--follow", choices=rp.OPTION_CHOICES["follow"], help="who follows the plan")
    sp.add_argument("--skew-tolerance", help="accepted skew between disks at an unplanned failover (10s)")
    sp.add_argument("--timeout", action="append", metavar="NAME=SECONDS", help="plan wait setting (repeatable)")

    sp = leaf(pp, "list", cmd_plan_list, [common, role], 2, "all plans with their resources")
    sp.add_argument("--host", help="plans where this cluster is source or target")
    sp = leaf(pp, "show", cmd_plan_show, [common, role], 2, "one plan, its status and runs")
    sp.add_argument("plan")
    sp = leaf(pp, "delete", cmd_plan_delete, [common], 2, "delete a draft or unprotected plan")
    sp.add_argument("plan")
    sp = leaf(pp, "set", cmd_plan_set, [common], 2, "hot settings, read by the follower at its next round")
    sp.add_argument("plan")
    sp.add_argument("--rpo")
    sp.add_argument("--paused", help="true or false")
    sp.add_argument("--follow", choices=rp.OPTION_CHOICES["follow"])
    sp.add_argument("--skew-tolerance")
    sp.add_argument("--timeout", action="append", metavar="NAME=SECONDS",
                    help="plan wait setting (repeatable); NAME= removes it")
    sp.add_argument("--interval", help="refused: use `protect P --interval D`")
    sp.add_argument("--retain", help="refused: use `protect P --retain N`")

    sp = leaf(sub, "check", cmd_check, [common, clusters], 1, "full pre-check, nothing written")
    sp.add_argument("plan")
    sp.add_argument("--phase", choices=rp.PHASES, default="protect")
    sp.add_argument("--force-unplanned", action="store_true")

    mp = sub.add_parser("measure", help="measure the changes before protecting").add_subparsers(dest="mcmd",
                                                                                               required=True)
    sp = leaf(mp, "start", cmd_measure_start, [common, clusters], 2, "backup jobs on the source only")
    sp.add_argument("plan")
    sp.add_argument("--interval", help="measured interval (default 5m)")
    sp = leaf(mp, "stop", cmd_measure_stop, [common, clusters], 2, "record the passes and remove the jobs")
    sp.add_argument("plan")
    sp = leaf(mp, "report", cmd_measure_report, [common], 2, "per candidate interval, from the database")
    sp.add_argument("plan")
    sp.add_argument("--margin", type=float, default=rp.DEFAULT_MARGIN)
    sp = leaf(mp, "formula", cmd_measure_formula, [common], 2, "forecast from write counters, no cluster")
    sp.add_argument("--size", required=True, help="disk size (2Gi)")
    sp.add_argument("--writes", type=int, required=True, help="writes per interval")
    sp.add_argument("--interval", required=True, help="interval (5m)")
    sp.add_argument("--block", default="2Mi", help="backup block size (2Mi or 16Mi)")
    sp.add_argument("--throughput", help="bytes per second to the target (72Mi)")
    sp.add_argument("--write-size", type=int, default=4096, help="bytes per write")

    sp = leaf(sub, "protect", cmd_protect, [common, clusters], 1, "protect the plan's VMs")
    sp.add_argument("plan")
    sp.add_argument("--dry-run", action="store_true", help="check and server dry-run, nothing written")
    sp.add_argument("--interval", help="backup interval (required without a measure)")
    sp.add_argument("--retain", help=f"backups kept by the recurring job ({rp.RETAIN_MIN} to {rp.RETAIN_MAX}); "
                    "on a protected plan, updates the job")

    sp = leaf(sub, "follow", cmd_follow, [common], 1, "the follower loop (resync after each backup, stats)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan")
    g.add_argument("--all", action="store_true")
    sp.add_argument("--once", action="store_true", help="one round, then exit")
    sp.add_argument("--take-over", action="store_true", help="take the plans' lease from another follower")
    sp.add_argument("--console", action="store_true", help="console child: timestamped journal on stderr")
    sp.add_argument("--log-file", metavar="PATH",
                    help="send stderr to this file, reopened on SIGUSR1 (the console rotates it)")
    sp.add_argument("--timeout", action="append", metavar="client=SECONDS",
                    help="kubectl call timeout (default: the largest plan `client` setting, else 30 s)")

    sp = leaf(sub, "status", cmd_status, [common], 1, "RPO held against the objective (exit 4 if exceeded)")
    sp.add_argument("plan", nargs="?")
    sp.add_argument("--host")

    sp = leaf(sub, "resources", cmd_resources, [common, role], 1, "resources of a host, as source or target")
    sp.add_argument("--host", required=True)
    sp.add_argument("--side", choices=("source", "target", "any"), default="any")

    sp = leaf(sub, "events", cmd_events, [common], 1, "the persistent journal, newest first")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--resource", help="source VM uid")
    g.add_argument("--plan")
    g.add_argument("--host")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--before", help="event id or ISO date of the last event of the previous page")

    sp = leaf(sub, "failover", cmd_failover, [common, clusters], 1, "fail the plan's VMs over to the target")
    sp.add_argument("plan")
    sp.add_argument("--vm", action="append", metavar="NS/NAME", help="fail these VMs over (repeatable)")
    sp.add_argument("--all-vms", action="store_true", help="fail every VM of the plan over (needs --yes)")
    sp.add_argument("--unplanned", action="store_true", help="the source is lost (needs --yes)")
    sp.add_argument("--force-unplanned", action="store_true",
                    help="unplanned although the source answers (with --unplanned)")
    sp.add_argument("--accept-skew", action="store_true",
                    help="accept a skew between disks above the tolerance (with --unplanned)")
    sp.add_argument("--yes", action="store_true", help="confirm --all-vms or an unplanned failover")
    sp.add_argument("--dry-run", action="store_true")

    sp = leaf(sub, "resume", cmd_resume, [common, clusters], 1,
              "finish the interrupted command of a plan, with the confirmation given at its start")
    sp.add_argument("plan")

    sp = leaf(sub, "unprotect", cmd_unprotect, [common, clusters], 1, "stop protecting the plan")
    sp.add_argument("plan")
    sp.add_argument("--delete-backups", action="store_true", help="also delete the plan's backups")
    sp.add_argument("--yes", action="store_true", help="confirm (the target standby volumes are deleted)")
    return ap


def _spec_arg(argv):
    """Valeur de --spec (`--spec=F` ou `--spec F`), lue avant l'analyse : les
    options obligatoires peuvent venir du fichier."""
    for i, a in enumerate(argv):
        if a.startswith("--spec="):
            return a.split("=", 1)[1]
        if a == "--spec" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def parse(argv):
    ap = build_parser()
    spec = _spec_arg(argv)
    if spec:
        # options du fichier juste après la sous-commande : celles de la
        # ligne de commande, plus loin, l'emportent
        n = 2 if argv and argv[0] in ("plan", "measure") else 1
        argv = argv[:n] + _spec_tokens(spec) + argv[n:]
    return ap.parse_args(argv)


def configure(args):
    if args.state_dir:
        os.environ["HARVESTER_OPS_STATE_DIR"] = str(Path(args.state_dir).resolve())
    _HIDDEN.clear()
    _hide(args.db, os.environ.get("HARVESTER_OPS_REPLICATION_DB"), cluster_decl.state_dir(),
          os.environ.get("HARVESTER_OPS_CONFIG", "/etc/harvester-ops/config.yaml"),
          getattr(args, "spec", None) if args.spec != "-" else None)
    home = os.path.expanduser("~")
    if home and home != "/":
        _hide(home)
    _CANCEL.update(signal=None, deadline=None, ctx=None, finished=False)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    want_json = "--json" in argv
    try:
        with scrubbed_journal():
            args = parse(argv)
            want_json = args.json
            configure(args)
            if args.fn is cmd_follow:
                return args.fn(args)            # le suiveur a son propre arrêt (rend ses baux)
            with cancel_on_signals():
                try:
                    return args.fn(args)
                except rr.Cancelled as e:
                    return fail_exit(want_json, "cancel", f"cancelled ({e})", "cancelled", EXIT_CANCELLED)
    except UsageError as e:
        return fail_exit(want_json, "validate", str(e), e.kind, EXIT_USAGE)
    except Failure as e:
        return fail_exit(want_json, "failed", str(e), e.kind, EXIT_FAIL)
    except Exception as e:                      # noqa: BLE001 : ni trace ni chemin à l'appelant
        return fail_exit(want_json, "internal", f"unexpected internal error ({type(e).__name__})", "failed",
                         EXIT_FAIL)


if __name__ == "__main__":
    sys.exit(main())
