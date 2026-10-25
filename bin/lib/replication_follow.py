"""harvester-ops : le suiveur de la réplication asynchrone (v1.99.0).

Il raccourcit le RPO et tient les statistiques. Sans lui, un volume de
secours n'applique une nouvelle sauvegarde qu'à la relecture naturelle de
la cible par Longhorn (1 à 3 min) ; avec lui, la relecture est demandée dès
que tous les disques d'une VM ont fini leur sauvegarde du même passage, et
l'application suit en 9 à 10 s (banc du 22/10/2026,
docs/design/2026-10-22-replication-asynchrone.md).

Sa seule écriture sur un cluster : `BackupVolume.spec.syncRequestedAt` côté
cible. S'il s'arrête, la réplication continue au rythme de la relecture
naturelle : le RPO s'allonge, rien ne casse.

Une bibliothèque, utilisée par la ligne de commande (`follow`) et par le
processus de suivi que lance la console :

* `Follower.once()` (ou `follow_once`) : un tour sur tous les plans
  suivis dont ce suiveur tient le bail, avec des lectures groupées par
  cluster (une liste des `Backup` par cluster source, une liste des moteurs
  par cluster cible, quel que soit le nombre de plans) ;
* `follow_loop()` : la boucle, dont le rythme sort des plans (lecture
  rapide pendant un passage, sommeil jusqu'à la prochaine échéance du cron,
  santé toutes les minutes), arrêtée proprement par `Stopper` (SIGTERM,
  SIGINT) entre deux tours, baux rendus.

Règles tenues :

* bail par plan (`replication_db.take_lease`, durée trois tours de santé) :
  deux suiveurs ne travaillent jamais sur le même plan ; un bail perdu en
  cours de tour (`heartbeat` faux) arrête toute écriture sur ce plan ;
* barrière par VM : la relecture d'un passage n'est demandée que lorsque
  tous les disques de la VM ont fini leur sauvegarde de ce passage
  (instantanés pris dans la même échéance du cron), ou à l'expiration de
  `barrier` ; un disque en erreur est noté et ne bloque pas les autres ;
* RPO tenu = âge du point appliqué le plus ancien des disques d'une VM
  (`lastRestoredBackup` du moteur du volume de secours), calculé par
  `replication.plan_status` ;
* un cluster injoignable n'arrête pas le tour : les autres plans
  continuent, l'événement est écrit une fois (et son retour aussi) ;
* aucune exception ne sort d'un tour (`once_safe`) : une erreur inattendue
  devient un événement `follow-error`.

Le suiveur ignore la table `locks` (verrou des commandes protect, failover,
unprotect) : il tient un bail (`leases`) par plan, et sa seule écriture,
`syncRequestedAt`, ne fait que demander une relecture que Longhorn ferait de
toute façon. Elle ne gêne aucune commande (la bascule demande elle-même sa
relecture finale), et un `BackupVolume` supprimé par la déprotection donne
un NotFound, qui ne compte pas comme une panne (nom oublié, relu au tour
suivant).

Charge sur l'API (lectures groupées, sélecteurs bornés à SELECTOR_MAX une
fois encodés dans l'URL) : voir `_selectors`.

Bibliothèque standard seulement.
"""

import contextlib
import hashlib
import json
import math
import signal
import time
from datetime import datetime, timezone
from urllib.parse import quote

import replication as rp
import replication_db as db
import replication_run as rr
import vm_transfer as vt

FOLLOWED_STATES = ("measuring", "protecting", "protected")
# plan basculé : seule la VM source est surveillée (revenue en marche ?)
WATCHED_STATES = FOLLOWED_STATES + ("failed_over",)
LEASE_ROUNDS = 3
# rythme quand aucun plan n'est suivi : la base est relue (réglages à chaud)
IDLE_S = rp.TIMEOUTS["follow_health"]
PRUNE_EVERY_S = 86400
# constats propres à une sauvegarde : oubliés de la mémoire deux jours après
# (la base garde le journal)
PASS_FLAGS = ("backup-error", "apply-late")
MEMORY_S = 2 * 86400
DEGRADED = ("degraded", "faulted")
VOLUME_SKIPPED = ("activated", "removed")
# Longueur maximale d'un sélecteur d'étiquettes une fois encodé dans l'URL de
# la requête (kubectl le passe en `?labelSelector=`, et Rancher relaie
# l'URL) : bien sous les 8 Kio que coupent la plupart des mandataires. Au-delà
# la liste est découpée (SELECTOR_CHUNKS requêtes au plus), puis remplacée
# par une seule liste plus large filtrée côté client.
SELECTOR_MAX = 2048
SELECTOR_CHUNKS = 4
# erreurs qui disent que le cluster ne répond pas (le reste : il a répondu,
# et refusé ou n'a pas trouvé)
_UNREACHABLE = ("unable to connect", "connection refused", "connection reset", "i/o timeout", "timed out",
                "timeout", "deadline exceeded", "no such host", "no route to host", "network is unreachable",
                "unexpected eof", "tls handshake", "server is currently unable", "service unavailable",
                "bad gateway", "gateway timeout", "dial tcp")
# constat -> code de sa fin (relus au démarrage, `Follower._seed`)
_CLEARS = {"source-reachable": "source-unreachable", "target-reachable": "target-unreachable",
           "standby-healthy": "standby-degraded", "rpo-recovered": "rpo-exceeded",
           "disk-backs-up": "disk-never-backs-up"}
_UID_FLAGS = ("source-unreachable", "target-unreachable", "standby-degraded", "rpo-exceeded", "rpo-drift",
              "source-vm-missing", "source-vm-running-after-failover")


def _epoch(v):
    return rp._t(v).timestamp() if v else None


def _utc(t):
    return datetime.fromtimestamp(t, timezone.utc)


def _stamp(t):
    # forme attendue par Longhorn pour `syncRequestedAt` (comme replication_run)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _md(o):
    return (o or {}).get("metadata") or {}


def _status(o):
    return (o or {}).get("status") or {}


def _spec(o):
    return (o or {}).get("spec") or {}


def _in(key, values):
    """Sélecteur d'ensemble (`clé in (a,b)`), valeurs triées : même appel
    d'un tour à l'autre."""
    return f"{key} in ({','.join(sorted(values))})"


def _selectors(key, values, base="", limit=SELECTOR_MAX, chunks=SELECTOR_CHUNKS):
    """Sélecteurs d'une lecture groupée, chacun d'au plus `limit` caractères
    une fois encodé dans l'URL : `base,clé in (...)` découpé en morceaux ;
    au-delà de `chunks` morceaux, `base` seul (None sans base : tout le
    namespace). L'appelant filtre toujours le résultat côté client.

    Charge : jusqu'à environ 45 noms de volume (`pvc-<uuid>`) par requête ;
    au-delà de 4 requêtes (180 volumes), une seule liste de toutes les
    sauvegardes de la cible (ou de tous les moteurs) du namespace, plus
    lourde en octets mais bornée en nombre d'appels."""
    values = sorted(set(values))
    prefix = f"{base}," if base else ""
    out, cur = [], []
    for v in values:
        cand = cur + [v]
        if cur and len(quote(prefix + _in(key, cand))) > limit:
            out.append(cur)
            cur = [v]
        else:
            cur = cand
    if cur:
        out.append(cur)
    if len(out) > chunks:
        return [base or None]
    return [prefix + _in(key, c) for c in out]


def unreachable_error(e):
    """L'erreur dit-elle que le cluster ne répond pas ? (connexion, délai
    dépassé, déclaration absente) ; un refus ou un NotFound n'en est pas un."""
    if isinstance(e, (SystemExit, LookupError, OSError)):
        return True
    msg = str(e).lower()
    return any(m in msg for m in _UNREACHABLE)


def not_found_error(e):
    msg = str(e).lower()
    return "notfound" in msg.replace(" ", "") or "not found" in msg


def _short(e):
    return hashlib.sha256(str(e).encode()).hexdigest()[:12]


def _canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def _diff_paths(a, b, prefix="", out=None, limit=10):
    """Chemins (pointés) où deux documents diffèrent, `limit` au plus."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                _diff_paths(a.get(k), b.get(k), f"{prefix}.{k}" if prefix else str(k), out, limit)
    elif a != b:
        out.append(prefix or ".")
    return out


def rhythm(plan):
    """Délais et rythme d'un plan : `replication.TIMEOUTS`, puis les
    réglages du plan (lus à chaque tour : modifiables à chaud).
    `barrier` vaut la moitié de l'intervalle par défaut, `follow_window`
    min(intervalle, barrier + resync_apply) : la lecture rapide couvre la
    barrière et l'attente de l'application, jamais plus d'un intervalle."""
    t = rr.effective_timeouts(plan)
    interval = int(plan.get("interval_s") or 300)
    t["interval"] = interval
    t["follow_window"] = int(t.get("follow_window") or min(interval, int(t["barrier"]) + int(t["resync_apply"])))
    t["follow_never_intervals"] = max(1, int(t.get("follow_never_intervals") or 2))
    t["follow_down_rounds"] = max(1, int(t.get("follow_down_rounds") or 2))
    t["lease_ttl"] = LEASE_ROUNDS * int(t["follow_health"])
    return t


def pass_time(tick):
    """Date qui range une sauvegarde dans un passage : celle de son
    instantané, sinon celle de sa création (sauvegarde en cours)."""
    return tick.get("snapshot_at") or tick.get("backup_started_at") or ""


def slot_of(tick, interval):
    """Échéance du cron d'une sauvegarde (multiple de l'intervalle depuis
    l'époque, comme `replication.next_tick_at`, l'intervalle divisant le
    jour)."""
    t = _epoch(pass_time(tick))
    return None if t is None else int(math.floor(t / interval))


def vm_is_running(vm):
    st = _status(vm)
    if st.get("printableStatus"):
        return st["printableStatus"] == "Running"
    return bool(st.get("ready"))


def _stopped_since(vm):
    for c in _status(vm).get("conditions") or []:
        if c.get("type") == "Ready" and str(c.get("status")) == "False" and c.get("lastTransitionTime"):
            return c["lastTransitionTime"]
    return None


def digest_disks(vm, pvcs):
    """Disques d'une VM pour `replication.vm_spec_digest`, sous la forme
    rangée par la protection (`replication_run._record_works`) : nom du
    disque, taille demandée par le PVC source, classe du PVC source. `pvcs`
    : {(namespace, nom): PVC}, une seule liste par cluster."""
    ns = _md(vm).get("namespace")
    out = []
    for vol in vt._tspec(vm).get("volumes") or []:
        claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
        if not claim:
            continue
        ps = _spec(pvcs.get((ns, claim)))
        size = vt.parse_quantity(((ps.get("resources") or {}).get("requests") or {}).get("storage")) or 0
        out.append({"disk": vol.get("name"), "size": size, "storage_class": ps.get("storageClassName")})
    return out


class Interrupted(BaseException):
    """Signal d'arrêt reçu pendant un appel à un cluster : l'appel est
    abandonné. Pas une `Exception` : aucun `except Exception` du tour ne la
    retient, elle remonte jusqu'à `follow_loop`, qui rend les baux."""


class Stopper:
    """Arrêt propre de la boucle : posé par SIGTERM ou SIGINT, constaté
    entre deux tours et pendant le sommeil (par tranches d'une seconde).
    Reçu pendant un appel à un cluster (`call()`), il l'interrompt
    (`Interrupted`) : `kube.Kube` passe par `subprocess.run`, qui tue le
    kubectl en cours sur toute exception, au lieu d'attendre son délai."""

    def __init__(self):
        self.flag = False
        self.signum = None
        self.calling = 0

    def set(self, signum=None, _frame=None):
        self.flag = True
        self.signum = signum

    def _on_signal(self, signum, _frame):
        self.set(signum)
        if self.calling:
            raise Interrupted(signum)

    @contextlib.contextmanager
    def call(self):
        """Autour d'un appel à un cluster : interrompu par le signal."""
        if self.flag:
            raise Interrupted(self.signum)
        self.calling += 1
        try:
            yield
        finally:
            self.calling -= 1

    def is_set(self):
        return self.flag

    def wait(self, seconds, sleep):
        left = float(seconds)
        while left > 0 and not self.flag:
            s = min(1.0, left)
            sleep(s)
            left -= s

    def install(self, signals=(signal.SIGTERM, signal.SIGINT)):
        for s in signals:
            signal.signal(s, self._on_signal)
        return self


class _Lost(Exception):
    """Le bail du plan a été repris pendant le tour : plus aucune écriture."""


class Follower:
    """Le suiveur : un tour par appel à `once()`, mémoire d'un tour à
    l'autre (caches de noms, dernières lectures de santé, événements déjà
    écrits). Ce qui doit survivre à un redémarrage est en base : passages
    (relecture demandée, application), santé, statistiques, bail.

    `clusters` : {nom: client} ou une fonction nom -> client (interface de
    `kube.Kube` : `get`, `list`, `patch`). `modes` : valeurs de l'option
    `follow` des plans acceptées (`console`, `cli`) ; `off` n'est jamais
    suivi. `plan_ids` : limite aux plans nommés. `take_over` : reprend de
    force le bail de chaque plan au premier tour (`follow --take-over`)."""

    def __init__(self, conn, clusters, owner, *, now=time.time, modes=("console", "cli"), plan_ids=None,
                 take_over=False, emit=None, stopper=None):
        self.conn, self.owner, self.now = conn, owner, now
        self.stopper = stopper
        self._clusters = clusters
        self.modes = tuple(modes)
        self.plan_ids = set(plan_ids) if plan_ids else None
        self.take_over = take_over
        self.emit = emit or (lambda *a: None)
        self.clients = {}
        self.taken = set()           # plans repris de force une fois (take_over)
        self.held = set()            # plans dont ce suiveur tient le bail
        self.flags = {}              # constats déjà écrits au journal (une fois par apparition)
        self.flag_at = {}            # constat -> date où il a été posé
        self.bv = {}                 # cluster -> {volume: nom du BackupVolume}
        self.bv_at = {}              # cluster -> date de la dernière liste des BackupVolume
        self.vms = {}                # cluster source -> {uid: VM}
        self.vms_at = {}
        self.pvcs = {}               # cluster source -> {(ns, nom): PVC}
        self.health_at = {}          # plan -> date de la dernière santé
        self.spec_at = {}            # plan -> date de la dernière relecture des specs
        self.down = {}               # cluster -> code d'erreur (injoignable, après hystérésis)
        self.streak = {}             # cluster -> tours injoignables de suite
        self.gone = {}               # cluster -> BackupVolume vus NotFound (à relire)
        self.running_since = {}      # uid -> date où la VM a été vue redémarrer
        self.seeded = False
        self.nudged = {}             # (cluster, BackupVolume) -> date de la dernière demande
        self.stopped_seen = {}       # uid -> date où la VM a été vue arrêtée
        self.written_health = {}     # (plan, uid) -> santé déjà rangée
        self.stats_done = {}         # (plan, uid, passage) -> date : bilan rangé
        self.last_prune = None
        self.calls = 0               # lectures de ce tour (pour le rapport)

    # -- clients ----------------------------------------------------------------
    def client(self, name):
        if name not in self.clients:
            c = self._clusters.get(name) if isinstance(self._clusters, dict) else self._clusters(name)
            if c is None:
                raise LookupError(f"unknown cluster {name}")
            self.clients[name] = c
        return self.clients[name]

    def _read(self, cluster, fn):
        """Une lecture groupée sur un cluster : (résultat, None) ou (None,
        erreur). Un cluster déjà vu injoignable dans ce tour n'est pas
        relu."""
        if cluster in self._down_now:
            return None, self._down_now[cluster]
        try:
            self.calls += 1
            with self._calling():
                out = fn(self.client(cluster))
        except (Exception, SystemExit) as e:    # noqa: BLE001 - cluster_config sort par SystemExit
            self._down_now[cluster] = e
            if unreachable_error(e):
                self._conn_down.add(cluster)
            else:
                self._read_failed[cluster] = e
            return None, e
        self._ok_now.add(cluster)
        return out, None

    def _calling(self):
        return self.stopper.call() if self.stopper is not None else contextlib.nullcontext()

    # -- journal ------------------------------------------------------------------
    def event(self, plan, step, code, uid="", side="", level="info", message="", facts=None):
        host = plan["source_cluster"] if side == "source" else plan["target_cluster"]
        db.record_event(self.conn, "follow", step=step, plan_id=plan["id"], resource_uid=uid, host=host,
                        side=side, level=level, code=code, message=str(message)[:500], facts=facts,
                        by=self.owner, now=_utc(self.t))
        self.report["events"] += 1
        self.emit(code, level, message)

    def once_on(self, key, value=True):
        """Vrai la première fois que `key` prend `value` (un constat n'est
        écrit qu'à son apparition, pas à chaque tour)."""
        if self.flags.get(key) == value:
            return False
        self.flags[key] = value
        self.flag_at[key] = self.now()
        return True

    def clear(self, key):
        """Vrai si `key` était posé (le constat disparaît : retour écrit)."""
        self.flag_at.pop(key, None)
        return self.flags.pop(key, None) is not None

    # -- tour --------------------------------------------------------------------------
    def once(self):
        """Un tour. Rend un rapport : plans suivis, sautés (et pourquoi),
        relectures demandées, applications constatées, événements, lectures,
        et `next_wake` (date du prochain tour)."""
        self.t = t = self.now()
        self.report = {"at": rp.iso(_utc(t)), "followed": [], "skipped": {}, "resyncs": 0, "applied": 0,
                       "events": 0, "calls": 0, "unreachable": [], "next_wake": t + IDLE_S}
        self.calls = 0
        self._down_now = {}          # cluster -> erreur (plus aucun appel à ce tour)
        self._conn_down = set()      # clusters qui n'ont pas répondu à ce tour
        self._read_failed = {}       # cluster -> erreur d'une lecture refusée (il a répondu)
        self._ok_now = set()
        if not self.seeded:
            self._seed()
        self._prune()
        plans = [p for p in db.list_plans(self.conn)
                 if p["state"] in WATCHED_STATES and (self.plan_ids is None or p["id"] in self.plan_ids)]
        mine = []
        for p in plans:
            mode = (p.get("options") or {}).get("follow") or "console"
            if mode == "off" or mode not in self.modes:
                self.report["skipped"][p["id"]] = f"follow-{mode}"
                continue
            rh = rhythm(p)
            if not self._lease(p, rh):
                continue
            if (p.get("options") or {}).get("paused"):
                self.report["skipped"][p["id"]] = "paused"
                continue
            mine.append((p, rh))
        wakes = [t + IDLE_S]
        if mine:
            wakes = self._work(mine)
        self.report["calls"] = self.calls
        self.report["unreachable"] = sorted(self.down)
        self.report["next_wake"] = max(t + 1, min(wakes))
        return self.report

    def once_safe(self):
        """`once()` qui ne lève jamais : une erreur inattendue devient un
        événement (une fois par message) et le tour suivant est prévu."""
        try:
            return self.once()
        except Exception as e:                  # noqa: BLE001
            t = self.now()
            key = ("follow-error", _short(e))
            if self.once_on(key):
                try:
                    db.record_event(self.conn, "follow", step="round", level="error", code="follow-error",
                                    message=f"follower round failed: {e}"[:500], by=self.owner, now=_utc(t))
                except Exception:               # noqa: BLE001 - base momentanément verrouillée
                    pass
            return {"at": rp.iso(_utc(t)), "error": str(e)[:300], "next_wake": t + IDLE_S, "followed": [],
                    "skipped": {}, "resyncs": 0, "applied": 0, "events": 0, "calls": self.calls,
                    "unreachable": sorted(self.down)}

    def close(self):
        """Rend les baux tenus (arrêt propre) : un autre suiveur peut
        reprendre aussitôt, sans attendre leur expiration."""
        for pid in sorted(self.held):
            try:
                db.release_lease(self.conn, pid, self.owner)
            except Exception:                   # noqa: BLE001
                pass
        self.held.clear()

    # -- bail ----------------------------------------------------------------------------
    def _lease(self, p, rh):
        pid = p["id"]
        force = self.take_over and pid not in self.taken
        before = db.get_lease(self.conn, pid) if force else None
        if db.take_lease(self.conn, pid, self.owner, rh["lease_ttl"], take_over=force, now=_utc(self.t)):
            self.held.add(pid)
            self.clear(("lease-lost", pid))
            if force:
                self.taken.add(pid)
                if before and before["owner"] != self.owner:
                    self.event(p, "lease", "lease-taken-over", level="warn",
                               message=f"lease taken over from {before['owner']}",
                               facts={"previous_owner": before["owner"]})
            return True
        self.report["skipped"][pid] = "lease-held"
        if pid in self.held:
            self.held.discard(pid)
            if self.once_on(("lease-lost", pid)):
                lease = db.get_lease(self.conn, pid) or {}
                self.event(p, "lease", "lease-lost", level="warn",
                           message=f"another follower holds the plan: {lease.get('owner')}",
                           facts={"owner": lease.get("owner")})
        return False

    def _seed(self):
        """Constats encore en cours au démarrage, relus du journal (dernier
        événement par plan, ressource et code ; constats d'une sauvegarde
        sur MEMORY_S) : un redémarrage ne réécrit pas un constat déjà écrit.
        Un constat effacé puis revenu pendant l'arrêt n'est pas réécrit."""
        self.seeded = True
        try:
            rows = db.last_follow_events(self.conn, since_per_backup=_utc(self.now() - MEMORY_S))
        except Exception:                       # noqa: BLE001 - base momentanément verrouillée
            self.seeded = False
            return
        for e in rows:
            code = e.get("code") or ""
            if code in _CLEARS:
                key, _v = self._seed_key(dict(e, code=_CLEARS[code], facts={}))
                self.flags.pop(key, None)
                self.flag_at.pop(key, None)
                continue
            key, value = self._seed_key(e)
            if key is not None:
                self.flags[key] = value
                self.flag_at[key] = self.now()

    @staticmethod
    def _seed_key(e):
        c, pid, uid, f = e.get("code"), e.get("plan_id"), e.get("resource_uid") or "", e.get("facts") or {}
        if c in _UID_FLAGS:
            return (c, pid, uid), True
        if c in ("plan-drift", "carnet-stale"):
            return (c, pid, uid), f.get("seen_digest") or True
        if c == "barrier-timeout":
            return (c, pid, uid), tuple(sorted(f.get("volumes") or []))
        if c == "disk-never-backs-up":
            return (c, pid), tuple(sorted(f.get("volumes") or []))
        if c in ("backup-error", "apply-late"):
            return (c, pid, f.get("backup")), True
        if c == "backup-volume-missing":
            return (c, pid, f.get("volume")), True
        return None, None

    def _still_mine(self, p):
        """Avant chaque écriture sur un cluster : le bail est-il toujours à
        nous ? Sinon plus rien n'est écrit pour ce plan (`_Lost`). Le
        battement porte l'heure de l'écriture, pas celle du début du tour."""
        if db.heartbeat(self.conn, p["id"], self.owner, now=_utc(self.now())):
            return True
        self.held.discard(p["id"])
        if self.once_on(("lease-lost", p["id"])):
            lease = db.get_lease(self.conn, p["id"]) or {}
            self.event(p, "lease", "lease-lost", level="warn",
                       message=f"lease lost during the round: {lease.get('owner')}",
                       facts={"owner": lease.get("owner")})
        raise _Lost(p["id"])

    def _prune(self):
        if self.last_prune is not None and self.t - self.last_prune < PRUNE_EVERY_S:
            return
        self.last_prune = self.t
        # mémoire bornée pour un suiveur qui tourne des mois
        old = self.t - MEMORY_S
        for k in [k for k in self.flags if k[0] in PASS_FLAGS and self.flag_at.get(k, 0) < old]:
            self.flags.pop(k, None)
            self.flag_at.pop(k, None)
        self.stats_done = {k: at for k, at in self.stats_done.items() if at >= old}
        try:
            db.prune(self.conn, db.retention_cutoff(_utc(self.t)), now=_utc(self.t))
        except Exception:                       # noqa: BLE001 - la rétention attendra le jour suivant
            pass

    # -- travail du tour -------------------------------------------------------------------
    def _work(self, mine):
        t = self.t
        due_health = {p["id"] for p, rh in mine if t - self.health_at.get(p["id"], -1e18) >= rh["follow_health"]}
        due_spec = {p["id"] for p, rh in mine if p["state"] == "protected"
                    and t - self.spec_at.get(p["id"], -1e18) >= rh["follow_spec"]}
        # VMs source d'abord (santé, une fois par minute) : une VM déjà
        # basculée (annotation de la bascule) sort du suivi dès ce tour
        for c in sorted({p["source_cluster"] for p, _rh in mine if p["id"] in due_health or p["id"] in due_spec}):
            self._list_vms(c)
        # ce que chaque plan suit, lu en base (rien sur les clusters)
        views = [self._view(p, rh) for p, rh in mine]
        # lectures groupées par cluster
        sources, targets = {}, {}
        for v in views:
            p = v["plan"]
            if p["state"] in FOLLOWED_STATES and v["volumes"]:
                sources.setdefault(p["source_cluster"], []).append(v)
            if p["state"] == "protected" and v["standby"]:
                targets.setdefault(p["target_cluster"], []).append(v)
        backups = {c: self._list_backups(c, vs) for c, vs in sorted(sources.items())}
        engines = {c: self._list_engines(c, vs) for c, vs in sorted(targets.items())}
        # santé (toutes les minutes) : VMs source, volumes de secours ; specs (10 min) : PVC source
        health_targets = {v["plan"]["target_cluster"] for v in views
                          if v["plan"]["id"] in due_health and v["plan"]["state"] == "protected" and v["standby"]}
        spec_sources = {v["plan"]["source_cluster"] for v in views if v["plan"]["id"] in due_spec}
        standby = {}
        for c in sorted(health_targets):
            pids = {v["plan"]["id"] for v in views if v["plan"]["target_cluster"] == c}
            standby[c] = self._list_by_plan(c, rr.K_LH_VOLUME, rr.NS, pids)
        for c in sorted(spec_sources):
            self._list_pvcs(c)
        # carnets de reprise (specs, 10 min) : comparés à la VM source, jamais réécrits
        carnets = {}
        for c in sorted({v["plan"]["target_cluster"] for v in views
                         if v["plan"]["id"] in due_spec and v["plan"]["state"] == "protected" and v["follow"]}):
            pids = {v["plan"]["id"] for v in views if v["plan"]["target_cluster"] == c}
            got = self._list_by_plan(c, rr.K_SECRET, rp.CARNET_NAMESPACE, pids)
            if got[1] is None:
                carnets[c] = {_md(o).get("name"): o for o in got[0]}
        # VMs du sélecteur (specs, 10 min) : nouvelles et sorties, relevées seulement
        for v in views:
            if v["plan"]["id"] in due_spec and v["plan"].get("selector"):
                try:
                    self._selector_watch(v)
                except _Lost:
                    pass
                except Exception as e:          # noqa: BLE001 - jamais au prix du suivi
                    if self.once_on(("follow-error", v["plan"]["id"], _short(e))):
                        self.event(v["plan"], "selector", "follow-error", level="error",
                                   message=f"selector check failed: {e}")
        wakes = []
        for v in views:
            p = v["plan"]
            try:
                wake = self._plan(v, backups.get(p["source_cluster"]), engines.get(p["target_cluster"]),
                                  standby.get(p["target_cluster"]), p["id"] in due_health, p["id"] in due_spec,
                                  carnets.get(p["target_cluster"]))
                self.report["followed"].append(p["id"])
            except _Lost:
                self.report["skipped"][p["id"]] = "lease-lost"
                wake = t + v["rh"]["follow_health"]
            except Exception as e:              # noqa: BLE001 - un plan ne casse pas le tour des autres
                key = ("follow-error", p["id"], _short(e))
                if self.once_on(key):
                    self.event(p, "round", "follow-error", level="error", message=f"follower error: {e}")
                self.report["skipped"][p["id"]] = "error"
                wake = t + v["rh"]["follow_health"]
            wakes.append(wake)
            if p["id"] in due_health:
                self.health_at[p["id"]] = t
            if p["id"] in due_spec:
                self.spec_at[p["id"]] = t
        # joignabilité en fin de tour : les écritures du tour comptent aussi
        self._reachability(views)
        return wakes or [t + IDLE_S]

    def _view(self, p, rh):
        """Ce que le plan suit, lu en base : ressources (état rangé),
        volumes (hors activés et retirés), volumes de secours."""
        gen = int(p["generation"])
        res = db.list_resources(self.conn, p["id"], gen)
        if p["state"] == "failed_over":
            follow = []
        else:
            want = {"measuring": ("measuring",), "protecting": ("protecting",),
                    "protected": ("protected",)}[p["state"]]
            follow = [r for r in res if r["state"] in want]
        # VM source déjà basculée (annotation posée par la bascule) : jamais suivie
        vms = self.vms.get(p["source_cluster"]) or {}
        follow = [r for r in follow
                  if rr.FAILED_OVER_TO not in (_md(vms.get(r["resource_uid"])).get("annotations") or {})]
        uids = {r["resource_uid"] for r in follow}
        vols = [v for v in db.list_volumes(self.conn, p["id"], gen)
                if v["vm_uid"] in uids and v["state"] not in VOLUME_SKIPPED and v["source_volume"]]
        standby = [v for v in vols if v["target_volume"] and v["state"] in ("standby", "restoring")]
        return {"plan": p, "rh": rh, "gen": gen, "resources": res, "follow": follow, "volumes": vols,
                "standby": standby}

    # -- lectures groupées -------------------------------------------------------------------
    def _bv_names(self, cluster, volumes, known):
        """Noms des `BackupVolume` des volumes (cache, nom rangé par la
        protection, liste du cluster relue au plus une fois par minute s'il
        en manque un). Jamais un nom fabriqué : la correspondance vient de
        `spec.volumeName`."""
        cache = self.bv.setdefault(cluster, {})
        gone = self.gone.setdefault(cluster, set())
        for vol, name in known.items():
            if name and name not in gone:
                cache.setdefault(vol, name)
        missing = [v for v in volumes if v not in cache]
        if missing and self.t - self.bv_at.get(cluster, -1e18) >= IDLE_S:
            self.bv_at[cluster] = self.t
            items, err = self._read(cluster, lambda k: k.list(rr.K_LH_BV, rr.NS))
            if err is None:
                for bv in items:
                    sp = _spec(bv)
                    if sp.get("volumeName") and sp.get("backupTargetName") in (None, "", rr.BACKUP_TARGET):
                        cache[sp["volumeName"]] = _md(bv).get("name")
                        gone.discard(_md(bv).get("name"))
        return {v: cache[v] for v in volumes if v in cache}

    def _forget_bv(self, cluster, name):
        """BackupVolume vu NotFound à l'écriture : nom oublié (le nom rangé
        par la protection aussi), liste du cluster relue au tour suivant."""
        cache = self.bv.get(cluster) or {}
        for vol in [v for v, n in cache.items() if n == name]:
            cache.pop(vol)
        self.gone.setdefault(cluster, set()).add(name)
        self.bv_at.pop(cluster, None)

    def _list_by_plan(self, cluster, kind, ns, pids):
        """Objets d'un cluster portant l'étiquette de plan de l'un des plans
        (sélecteurs bornés, filtre côté client). Rend (liste, erreur)."""
        out = []
        for sel in _selectors(rp.LABEL_PLAN, pids):
            items, err = self._read(cluster, lambda k, sel=sel: k.list(kind, ns, selector=sel))
            if err is not None:
                return None, err
            out += [o for o in items if (_md(o).get("labels") or {}).get(rp.LABEL_PLAN) in pids]
        return out, None

    def _list_backups(self, cluster, views):
        """Les `Backup` du cluster source pour tous ses plans, en une liste
        (ou quelques morceaux, `_selectors`). Sélecteur serveur sur les
        étiquettes que le mutateur de Longhorn pose dès la création
        (webhook/resources/backup/mutator.go, v1.12.0) : `backup-target` (la
        cible commune du plan, `replication_run.BACKUP_TARGET`) et
        `backup-volume` (le nom du VOLUME). Le volume se lit sur l'étiquette
        (`replication_run.backup_volume_of`) : `status.volumeName` n'est
        rempli qu'à la fin, une sauvegarde en cours ou en erreur serait
        invisible. Rend ({volume: [Backup]}, erreur)."""
        vols = sorted({v["source_volume"] for view in views for v in view["volumes"]})
        target = rr.BACKUP_TARGET
        items = []
        for sel in _selectors("backup-volume", vols, base=f"backup-target={target}"):
            got, err = self._read(cluster, lambda k, sel=sel: k.list(rr.K_LH_BACKUP, rr.NS, selector=sel))
            if err is not None:
                return None, err
            items += got
        want = set(vols)
        out = {}
        for b in items:
            if (_md(b).get("labels") or {}).get("backup-target", target) != target:
                continue
            vol = rr.backup_volume_of(b)
            if vol in want:
                out.setdefault(vol, []).append(b)
        return out, None

    def _list_engines(self, cluster, views):
        sb = sorted({v["target_volume"] for view in views for v in view["standby"]})
        items = []
        for sel in _selectors(rp.ENGINE_VOLUME_LABEL, sb):
            got, err = self._read(cluster, lambda k, sel=sel: k.list(rr.K_LH_ENGINE, rr.NS, selector=sel))
            if err is not None:
                return None, err
            items += got
        want = set(sb)
        out = {}
        for e in items:
            vol = (_md(e).get("labels") or {}).get(rp.ENGINE_VOLUME_LABEL)
            if vol not in want:
                continue
            # le moteur qui a déjà restauré l'emporte (comme replication_run.engine_of)
            if vol and (vol not in out or (_status(e).get("lastRestoredBackup")
                                           and not _status(out[vol]).get("lastRestoredBackup"))):
                out[vol] = e
        return out, None

    def _list_vms(self, cluster):
        items, err = self._read(cluster, lambda k: k.list(rr.K_VM))
        if err is None:
            self.vms[cluster] = {_md(vm).get("uid"): vm for vm in items}
            self.vms_at[cluster] = self.t

    def _list_pvcs(self, cluster):
        items, err = self._read(cluster, lambda k: k.list(rr.K_PVC))
        if err is None:
            self.pvcs[cluster] = {(_md(o).get("namespace"), _md(o).get("name")): o for o in items}

    # -- joignabilité --------------------------------------------------------------------------
    def _reachability(self, views):
        """Fin de tour : compteur de tours injoignables par cluster
        (hystérésis), puis constats écrits une fois par ressource et par
        côté quand le compteur atteint `follow_down_rounds`, et leur retour.
        Une lecture refusée (le cluster a répondu) n'est pas une panne :
        `read-failed`, une fois par message."""
        for c in self._conn_down:
            self.streak[c] = self.streak.get(c, 0) + 1
        for c in self._read_ok():
            self.streak[c] = 0
        for v in views:
            p, rh = v["plan"], v["rh"]
            for side, cluster in (("source", p["source_cluster"]), ("target", p["target_cluster"])):
                rows = v["follow"] or [{"resource_uid": ""}]
                if cluster in self._conn_down and self.streak.get(cluster, 0) >= rh["follow_down_rounds"]:
                    e = self._down_now[cluster]
                    self.down[cluster] = str(e)[:200]
                    for r in rows:
                        if self.once_on((f"{side}-unreachable", p["id"], r["resource_uid"])):
                            self.event(p, "health", f"{side}-unreachable", r["resource_uid"], side, "warn",
                                       f"the {side} cluster {cluster} does not answer: {str(e)[:200]}",
                                       facts={"rounds": self.streak[cluster]})
                elif cluster in self._read_ok():
                    self.down.pop(cluster, None)
                    for r in rows:
                        if self.clear((f"{side}-unreachable", p["id"], r["resource_uid"])):
                            self.event(p, "health", f"{side}-reachable", r["resource_uid"], side, "info",
                                       f"the {side} cluster {cluster} answers again")
                if cluster in self._read_failed:
                    e = self._read_failed[cluster]
                    if self.once_on(("read-failed", p["id"], side), _short(e)):
                        self.event(p, "health", "read-failed", "", side, "warn",
                                   f"the {side} cluster {cluster} refused a read: {str(e)[:200]}")
                elif cluster in self._read_ok():
                    self.clear(("read-failed", p["id"], side))

    def _read_ok(self):
        # clusters lus avec succès pendant ce tour (et pas tombés depuis)
        return self._ok_now - set(self._down_now)

    def _reach(self, cluster, rh, prev):
        """Joignabilité d'un côté pour la santé : True lu à ce tour, False
        injoignable depuis `follow_down_rounds` tours (celui-ci compris),
        sinon la valeur connue (None : inconnue, jamais « joignable » par
        défaut)."""
        if cluster in self._conn_down:
            return False if self.streak.get(cluster, 0) + 1 >= rh["follow_down_rounds"] else prev
        if cluster in self._read_ok():
            return True
        return prev

    # -- un plan -----------------------------------------------------------------------------------
    def _plan(self, v, backups, engines, standby, due_health, due_spec, carnets=None):
        p, rh = v["plan"], v["rh"]
        if p["state"] == "failed_over" or any(r["state"] == "failed_over" for r in v["resources"]):
            if due_health:
                self._after_failover(v)
            if p["state"] == "failed_over":
                return self.t + rh["follow_health"]
        if p["state"] == "measuring":
            return self._measuring(v, backups)
        ticks = self._ticks(v)
        src_down = p["source_cluster"] in self._down_now
        # sauvegardes de la source lues à ce tour : sans elles, les passages
        # en cours connus sont des relevés anciens
        src_read = backups is not None and backups[0] is not None
        if src_read:
            self._record_backups(v, backups[0], ticks)
        v["never"] = set()
        if p["state"] == "protecting":
            # la protection mène sa propre relecture : ici, relevé seulement
            return self._wake(v, ticks, open_work=False)
        v["never"] = self._never(v, ticks, src_read)
        open_work = self._barrier(v, ticks, p["target_cluster"] in self._down_now, src_read)
        if engines is not None and engines[0] is not None:
            self._apply(v, ticks, engines[0])
        self._renudge(v, ticks, p["target_cluster"] in self._down_now)
        # relecture demandée, application pas encore vue : on regarde vite
        open_work = open_work or any(
            tk["state"] == "completed" and tk.get("resync_at")
            and self.t - _epoch(tk["resync_at"]) < rh["resync_apply"]
            for x in v["standby"] for tk in (ticks.get(x["source_volume"]) or {}).values())
        health = self._health(v, standby, due_health, due_spec, src_down, carnets)
        self._status(v, health)
        return self._wake(v, ticks, open_work)

    def _ticks(self, v):
        """Passages connus du plan : les récents et le dernier appliqué de
        chaque volume. {volume: {sauvegarde: passage}}."""
        p, rh = v["plan"], v["rh"]
        horizon = max(3600, (int(p.get("retain") or 6) + 2) * rh["interval"],
                      (rh["follow_never_intervals"] + 1) * rh["interval"])
        v["horizon"] = self.t - horizon
        since = _utc(self.t - horizon)
        rows = db.list_ticks(self.conn, p["id"], v["gen"], since=since)
        rows += db.last_applied_ticks(self.conn, p["id"], v["gen"])
        out = {}
        for r in rows:
            out.setdefault(r["source_volume"], {})[r["backup"]] = r
        return out

    def _floor(self, ticks, vol):
        """Date (époque) du dernier point appliqué d'un volume, None sans."""
        pts = [_epoch(pass_time(t)) for t in (ticks.get(vol) or {}).values()
               if t["state"] == "applied" and pass_time(t)]
        return max(pts) if pts else None

    def _record_backups(self, v, byvol, ticks):
        """Nouvelle sauvegarde (ou nouvel état d'une sauvegarde) : passage
        rangé. Une sauvegarde antérieure au dernier point appliqué de son
        volume (rétention, sauvegardes d'avant la protection) est ignorée."""
        p = v["plan"]
        res = {r["resource_uid"]: r for r in v["follow"]}
        for vol_row in v["volumes"]:
            vol, uid = vol_row["source_volume"], vol_row["vm_uid"]
            floor = self._floor(ticks, vol)
            if p["state"] == "protecting":
                floor = max(floor or 0, _epoch((res.get(uid) or {}).get("state_since")) or 0)
            known = ticks.setdefault(vol, {})
            for b in byvol.get(vol) or []:
                tk = rr._tick_from_backup(b, uid, v["gen"])
                old = known.get(tk["backup"])
                # antérieure au dernier point appliqué, ou plus vieille que ce
                # que la rétention garde (reste d'une sauvegarde en erreur)
                if old is None and pass_time(tk) and (_epoch(pass_time(tk)) < (floor or 0)
                                                      or _epoch(pass_time(tk)) < v["horizon"]):
                    continue
                if old is not None and (old["state"] in ("applied", "skipped")
                                        or (old["state"] == tk["state"]
                                            and old.get("backup_done_at") == tk["backup_done_at"])):
                    continue
                db.record_tick(self.conn, p["id"], tk)
                merged = dict(old or {}, **tk)
                if old:
                    merged["resync_at"] = old.get("resync_at") or ""
                    merged["applied_at"] = old.get("applied_at") or ""
                known[tk["backup"]] = merged
                if tk["state"] == "error" and self.once_on(("backup-error", p["id"], tk["backup"])):
                    self.event(p, "backup", "backup-error", uid, "source", "warn",
                               f"backup {tk['backup']} of {vol} failed",
                               facts={"volume": vol, "backup": tk["backup"],
                                      "error": str(_status(b).get("error") or "")[:200]})

    def _measuring(self, v, backups):
        """Phase de mesure : passages rangés dans `measures` (sauvegardes
        terminées ou en erreur depuis le début de la mesure), rien sur la
        cible."""
        p, rh = v["plan"], v["rh"]
        open_work = False
        if backups is not None and backups[0] is not None:
            since = {r["resource_uid"]: r.get("state_since") or "" for r in v["follow"]}
            have = {(m["source_volume"], m["backup"]): m["state"]
                    for m in db.list_ticks(self.conn, p["id"], v["gen"], table="measures",
                                           since=_utc(self.t - max(3600, 10 * rh["interval"])))}
            for vol_row in v["volumes"]:
                vol = vol_row["source_volume"]
                for b in backups[0].get(vol) or []:
                    tk = rr._tick_from_backup(b, vol_row["vm_uid"], v["gen"])
                    if tk["state"] == "in_progress":
                        open_work = True
                        continue
                    start = _epoch(since.get(vol_row["vm_uid"])) or 0
                    if (_epoch(pass_time(tk)) or 0) < start or have.get((vol, tk["backup"])) == tk["state"]:
                        continue
                    db.record_measure(self.conn, p["id"], tk)
        return self._wake(v, {}, open_work)

    # -- barrière et relecture ------------------------------------------------------------------------
    def _slots(self, v, ticks, uid):
        """Passages ouverts d'une VM : {échéance: {volume: passage}} (la
        sauvegarde la plus récente de chaque volume dans l'échéance), à partir
        du dernier point appliqué du volume."""
        interval = v["rh"]["interval"]
        out = {}
        for vol_row in v["volumes"]:
            if vol_row["vm_uid"] != uid:
                continue
            vol = vol_row["source_volume"]
            floor = self._floor(ticks, vol)
            for tk in (ticks.get(vol) or {}).values():
                if tk["state"] not in ("in_progress", "completed", "error", "applied") or not pass_time(tk):
                    continue
                # ce qui précède le dernier point appliqué est dépassé ; le
                # point lui-même compte comme fini dans son passage
                if floor is not None and _epoch(pass_time(tk)) < floor:
                    continue
                s = slot_of(tk, interval)
                cur = out.setdefault(s, {}).get(vol)
                if cur is None or pass_time(tk) > pass_time(cur):
                    out[s][vol] = tk
        return out

    def _never(self, v, ticks, src_read):
        """Disques d'une VM en marche sans aucune sauvegarde depuis
        `follow_never_intervals` intervalles (dernière sauvegarde vue, début
        de la protection, redémarrage de la VM vu par le suiveur) : volume
        exclu de la tâche, tâche cassée. Un seul constat agrégé par plan
        (`disk-never-backs-up`, écrit quand un disque s'y ajoute), raison de
        santé des VMs touchées ; ces disques ne retiennent plus la barrière
        et ne font plus lire vite. Rend l'ensemble des volumes."""
        p, rh = v["plan"], v["rh"]
        key = ("disk-never-backs-up", p["id"])
        prev = tuple(self.flags.get(key) or ())
        if not src_read:
            return set(prev)                    # rien de neuf sans la source
        limit = rh["follow_never_intervals"] * rh["interval"]
        res = {r["resource_uid"]: r for r in v["follow"]}
        out = set()
        for x in v["volumes"]:
            uid = x["vm_uid"]
            if uid not in res:
                continue
            h = self.written_health.get((p["id"], uid)) or res[uid].get("health") or {}
            if h.get("running") is not True:
                continue                        # VM arrêtée : pas de sauvegarde attendue
            seen = [_epoch(pass_time(tk)) for tk in (ticks.get(x["source_volume"]) or {}).values() if pass_time(tk)]
            ref = max(seen + [_epoch(res[uid].get("state_since")) or 0, self.running_since.get(uid, 0)])
            if self.t - ref >= limit:
                out.add(x["source_volume"])
        new = tuple(sorted(out))
        if set(new) - set(prev):
            self.flags[key] = new
            self.flag_at[key] = self.now()
            self.event(p, "backup", "disk-never-backs-up", "", "source", "warn",
                       f"{len(new)} disk(s) show no backup for {rh['follow_never_intervals']} intervals:"
                       f" check the recurring job", facts={"volumes": list(new),
                                                           "intervals": rh["follow_never_intervals"]})
        elif not new and prev:
            self.clear(key)
            self.event(p, "backup", "disk-backs-up", "", "source", "info", "every disk backs up again",
                       facts={"volumes": list(prev)})
        elif new != prev:
            self.flags[key] = new               # un disque est revenu : rien à écrire
        return out

    def _barrier(self, v, ticks, dst_down, src_read=True):
        """Barrière par VM : relecture demandée pour un passage quand tous les
        disques de la VM l'ont fini (terminé ou en erreur), ou à
        l'expiration de `barrier`. Les disques qui ne sauvegardent jamais
        (`_never`) ne comptent pas. Source muette à ce tour : un passage
        encore ouvert n'expire pas (ses relevés sont anciens ; le constat
        est `source-unreachable`). `barrier-timeout` est écrit une fois par
        série de passages en retard (mêmes disques), pas à chaque passage.
        Rend True s'il reste un passage ouvert (lecture rapide)."""
        p, rh = v["plan"], v["rh"]
        never = v.get("never") or set()
        open_work = False
        request = {}                                    # volume source -> [passages]
        for r in v["follow"]:
            uid = r["resource_uid"]
            vols = [x["source_volume"] for x in v["volumes"] if x["vm_uid"] == uid and x["source_volume"] not in never]
            floors = {vol: self._floor(ticks, vol) for vol in vols}
            closed = None                               # (passage, disques en retard) du dernier passage fermé
            for s, entries in sorted(self._slots(v, ticks, uid).items()):
                entries = {vol: tk for vol, tk in entries.items() if vol not in never}
                if not entries:
                    continue
                # un disque déjà appliqué à un passage plus récent n'est pas en retard sur celui-ci
                late = [vol for vol in vols
                        if (vol not in entries and (floors[vol] is None or floors[vol] < s * rh["interval"]))
                        or (vol in entries and entries[vol]["state"] == "in_progress")]
                first = min(_epoch(pass_time(tk)) for tk in entries.values())
                if late and self.t - first < rh["barrier"]:
                    open_work = True
                    continue
                if late and not src_read:
                    continue
                closed = (first, late)
                done = [tk for tk in entries.values() if tk["state"] in ("completed", "applied")]
                todo = [tk for tk in done if tk["state"] == "completed" and not tk.get("resync_at")]
                open_work = open_work or bool(late)
                if not todo:
                    continue
                pts = [_epoch(pass_time(tk)) for tk in done]
                skew = round(max(pts) - min(pts), 3) if len(pts) > 1 else 0.0
                for tk in todo:
                    request.setdefault(tk["source_volume"], []).append((tk, skew))
            key = ("barrier-timeout", p["id"], uid)
            if closed and closed[1]:
                if self.once_on(key, tuple(sorted(closed[1]))):
                    self.event(p, "barrier", "barrier-timeout", uid, "source", "warn",
                               f"{len(closed[1])} disk(s) did not finish the pass within {rh['barrier']} s",
                               facts={"volumes": sorted(closed[1]), "pass_at": rp.iso(_utc(closed[0]))})
            elif closed:
                self.clear(key)
        if request and not dst_down:
            self._request(v, request)
        elif request:
            open_work = True
        return open_work

    def _request(self, v, request):
        """Relecture forcée : `syncRequestedAt` de chaque `BackupVolume` cible
        concerné (le bail est vérifié avant chaque écriture), puis passages
        notés (`resync_at`, écart entre disques)."""
        p = v["plan"]
        cluster = p["target_cluster"]
        known = {x["source_volume"]: x.get("backup_volume_target") for x in v["volumes"]}
        names = self._bv_names(cluster, sorted(request), known)
        stamp = _stamp(self.t)
        for vol in sorted(request):
            bv = names.get(vol)
            if not bv:
                uid = request[vol][0][0].get("vm_uid") or ""
                if self.once_on(("backup-volume-missing", p["id"], vol)):
                    self.event(p, "resync", "backup-volume-missing", uid, "target", "warn",
                               f"no backup volume for {vol} on the target yet: natural sync only",
                               facts={"volume": vol})
                continue
            out = self._resync(p, cluster, bv, stamp)
            if out == "unreachable":
                return
            if out != "ok":
                continue
            for tk, skew in request[vol]:
                row = {"generation": v["gen"], "source_volume": vol, "backup": tk["backup"],
                       "state": tk["state"], "resync_at": rp.iso(_utc(self.t)), "skew_s": skew}
                db.record_tick(self.conn, p["id"], row)
                tk["resync_at"], tk["skew_s"] = row["resync_at"], skew

    def _resync(self, p, cluster, bv, stamp):
        """Une demande de relecture (`syncRequestedAt`). Rend `ok`,
        `unreachable` (le cluster ne répond pas : plus rien à ce tour),
        `notfound` (BackupVolume disparu : nom oublié, relu au tour suivant,
        pas une panne) ou `failed` (refus : `resync-failed`, une fois)."""
        self._still_mine(p)
        try:
            with self._calling():
                self.client(cluster).patch(rr.K_LH_BV, rr.NS, bv, {"spec": {"syncRequestedAt": stamp}})
        except (Exception, SystemExit) as e:    # noqa: BLE001
            if unreachable_error(e):
                self._down_now[cluster] = e
                self._conn_down.add(cluster)
                return "unreachable"
            if not_found_error(e):
                self._forget_bv(cluster, bv)
                self.nudged.pop((cluster, bv), None)
                return "notfound"
            if self.once_on(("resync-failed", p["id"], bv), _short(e)):
                self.event(p, "resync", "resync-failed", "", "target", "warn",
                           f"resync request on {bv} refused: {str(e)[:200]}", facts={"backup_volume": bv})
            return "failed"
        self.clear(("resync-failed", p["id"], bv))
        self.nudged[(cluster, bv)] = self.t
        self.report["resyncs"] += 1
        return "ok"

    def _renudge(self, v, ticks, dst_down):
        """Relecture redemandée pour un passage demandé et pas encore
        appliqué (`resync_nudge`), puis, passé `resync_apply`, événement
        `apply-late` et relance au rythme de la santé."""
        if dst_down:
            return
        p, rh = v["plan"], v["rh"]
        known = {x["source_volume"]: x.get("backup_volume_target") for x in v["volumes"]}
        names = self._bv_names(p["target_cluster"], [x["source_volume"] for x in v["volumes"]], known)
        stamp = _stamp(self.t)
        for x in v["volumes"]:
            vol = x["source_volume"]
            waiting = [tk for tk in (ticks.get(vol) or {}).values()
                       if tk["state"] == "completed" and tk.get("resync_at")]
            if not waiting or not names.get(vol):
                continue
            tk = max(waiting, key=pass_time)
            since = self.t - _epoch(tk["resync_at"])
            late = since >= rh["resync_apply"]
            if late and self.once_on(("apply-late", p["id"], tk["backup"])):
                self.event(p, "apply", "apply-late", x["vm_uid"], "target", "warn",
                           f"backup {tk['backup']} not applied on {x['target_volume']} {int(since)} s after the"
                           f" resync request", facts={"volume": vol, "backup": tk["backup"],
                                                      "standby": x["target_volume"], "waited_s": int(since)})
            period = rh["follow_health"] if late else rh["resync_nudge"]
            bv = names[vol]
            if self.t - self.nudged.get((p["target_cluster"], bv), -1e18) < period:
                continue
            if self._resync(p, p["target_cluster"], bv, stamp) == "unreachable":
                return

    # -- application -------------------------------------------------------------------------------------
    def _apply(self, v, ticks, engines):
        """Application constatée : `lastRestoredBackup` du moteur du volume
        de secours vaut une sauvegarde suivie. Les passages plus anciens de
        ce volume qui n'ont jamais été appliqués passent `skipped`. Un
        passage entièrement appliqué d'une VM donne sa ligne de
        statistiques."""
        p = v["plan"]
        now_iso = rp.iso(_utc(self.t))
        for x in v["standby"]:
            vol = x["source_volume"]
            eng = engines.get(x["target_volume"])
            last = _status(eng).get("lastRestoredBackup") or ""
            tk = (ticks.get(vol) or {}).get(last)
            if not last or tk is None or tk["state"] == "applied" or tk["state"] == "error":
                continue
            db.mark_applied(self.conn, p["id"], vol, last, _utc(self.t), v["gen"],
                            resync_at=rp._t(tk["resync_at"]) if tk.get("resync_at") else None)
            tk.update(state="applied", applied_at=now_iso)
            self.report["applied"] += 1
            for old in list((ticks.get(vol) or {}).values()):
                if old["state"] in ("completed", "in_progress") and pass_time(old) < pass_time(tk):
                    db.record_tick(self.conn, p["id"], {"generation": v["gen"], "source_volume": vol,
                                                        "backup": old["backup"], "state": "skipped"})
                    old["state"] = "skipped"

    # -- santé -------------------------------------------------------------------------------------------
    def _health(self, v, standby, due_health, due_spec, src_down, carnets=None):
        """Santé de chaque ressource suivie, rangée quand elle change :
        joignabilité (écritures du tour comprises, hystérésis), volume de
        secours dégradé (santé, une fois par minute), VM en marche, disques
        sans sauvegarde, spec relue et carnet comparé (10 min, dérive par
        empreinte)."""
        p, rh = v["plan"], v["rh"]
        never = v.get("never") or set()
        vms = self.vms.get(p["source_cluster"]) or {}
        lh = {}
        if standby is not None and standby[0] is not None:
            lh = {_md(o).get("name"): o for o in standby[0]}
        out = {}
        for r in v["follow"]:
            uid = r["resource_uid"]
            key = (p["id"], uid)
            prev = self.written_health.get(key) or (r.get("health") or {})
            h = dict(prev)
            h["source_reachable"] = self._reach(p["source_cluster"], rh, prev.get("source_reachable"))
            h["target_reachable"] = self._reach(p["target_cluster"], rh, prev.get("target_reachable"))
            h["never_backs_up"] = sorted(x["source_volume"] for x in v["volumes"]
                                         if x["vm_uid"] == uid and x["source_volume"] in never)
            vm = vms.get(uid)
            if vm is not None:
                running = vm_is_running(vm)
                if running and prev.get("running") is False:
                    # redémarrée : le délai sans sauvegarde repart d'ici
                    self.running_since[uid] = self.t
                h["running"] = running
                if running:
                    self.stopped_seen.pop(uid, None)
                    h["stopped_at"] = None
                else:
                    self.stopped_seen.setdefault(uid, _stopped_since(vm) or rp.iso(_utc(self.t)))
                    h["stopped_at"] = self.stopped_seen[uid]
            if due_health and standby is not None and standby[0] is not None:
                bad = []
                for x in v["standby"]:
                    if x["vm_uid"] != uid:
                        continue
                    o = lh.get(x["target_volume"])
                    st = _status(o)
                    if o is None or st.get("robustness") in DEGRADED or st.get("state") == "faulted":
                        bad.append({"volume": x["target_volume"], "robustness": st.get("robustness") or "missing"})
                h["standby_degraded"] = bool(bad)
                if bad and self.once_on(("standby-degraded", p["id"], uid)):
                    self.event(p, "health", "standby-degraded", uid, "target", "warn",
                               f"{len(bad)} standby volume(s) not healthy", facts={"volumes": bad})
                elif not bad and self.clear(("standby-degraded", p["id"], uid)):
                    self.event(p, "health", "standby-healthy", uid, "target", "info", "standby volumes healthy")
            if due_spec and p["source_cluster"] not in self._down_now:
                self._spec(v, r, vm, h)
                if carnets is not None and vm is not None:
                    self._carnet(v, r, vm, carnets.get(rp.carnet_name(p["id"], uid)), h)
            out[uid] = h
            if h != self.written_health.get(key):
                db.set_health(self.conn, p["id"], uid, v["gen"], h, now=_utc(self.t))
                self.written_health[key] = dict(h)
        return out

    def _spec(self, v, r, vm, h):
        """Spec de la VM source relue : dérive décidée par l'empreinte seule
        (`replication.vm_spec_digest`, disques lus sur les PVC source). Un
        disque ajouté ou retiré, une taille changée : `plan-drift`
        (« relancer protect »), jamais d'ajout automatique."""
        p = v["plan"]
        uid = r["resource_uid"]
        if vm is None:
            if self.once_on(("source-vm-missing", p["id"], uid)):
                self.event(p, "spec", "source-vm-missing", uid, "source", "warn",
                           f"source VM {r['namespace']}/{r['name']} not found")
            return
        self.clear(("source-vm-missing", p["id"], uid))
        disks = digest_disks(vm, self.pvcs.get(p["source_cluster"]) or {})
        # empreinte seule : la génération de la VM ne bouge pas quand seul
        # son PVC est agrandi. Elle est rangée sous un autre nom pour que
        # `spec_drifted` (ici et dans `plan_status`) décide sur l'empreinte
        seen = {"vm_spec_digest": rp.vm_spec_digest(vm, disks)}
        h.update(seen, vm_generation_seen=rp.vm_spec_generation(vm))
        if rp.spec_drifted({"vm_spec_digest": r.get("vm_spec_digest")}, seen):
            if self.once_on(("plan-drift", p["id"], uid), seen["vm_spec_digest"]):
                self.event(p, "spec", "plan-drift", uid, "source", "warn",
                           "the VM spec changed since protect (disks, sizes, networks, CPU, memory): run protect again",
                           facts={"known_digest": r.get("vm_spec_digest"), "seen_digest": seen["vm_spec_digest"],
                                  "known_generation": r.get("vm_generation"),
                                  "seen_generation": h["vm_generation_seen"]})
        else:
            self.clear(("plan-drift", p["id"], uid))

    def _carnet(self, v, r, vm, secret, h=None):
        """Le carnet de reprise est-il celui que la protection écrirait
        aujourd'hui ? La VM cible est recalculée depuis la VM source
        (`replication.target_vm`, comme `replication_run.write_carnets`) et
        comparée à son `vm.json`. Différent (CPU, mémoire, tout champ de la
        VM hors empreinte de dérive) : `carnet-stale`, une fois par contenu,
        et rien n'est écrit (relancer protect le réécrit). Les secrets
        cloud-init ne sont pas relus."""
        p = v["plan"]
        uid = r["resource_uid"]
        if secret is None:
            return
        carnet = rr.read_carnet(secret)
        if not isinstance(carnet.get("vm"), dict):
            return
        claims = {x.get("claim"): x.get("target_claim") for x in carnet.get("volumes") or [] if x.get("claim")}
        res = {"resource_uid": uid, "namespace": r["namespace"], "name": r["name"],
               "firmware_uuid": r.get("firmware_uuid") or ""}
        try:
            want, _removed = rp.target_vm(vm, res, p.get("mappings") or {}, p.get("options") or {}, claims,
                                          p["id"], v["gen"])
        except ValueError:
            return
        key = ("carnet-stale", p["id"], uid)
        h = h if h is not None else {}
        if _canon(want) == _canon(carnet["vm"]):
            h["carnet_stale"] = False
            self.clear(key)
            return
        # gardé dans la santé de la VM : alerte en cours et contrôle de la bascule
        h["carnet_stale"] = True
        digest = hashlib.sha256(_canon(want).encode()).hexdigest()[:16]
        if self.once_on(key, digest):
            self.event(p, "spec", "carnet-stale", uid, "target", "warn",
                       "the recovery carnet no longer matches the source VM: run protect again to refresh it",
                       facts={"fields": _diff_paths(carnet["vm"], want), "seen_digest": digest})

    def _selector_watch(self, v):
        """Plan à sélecteur : les VMs du namespace qui portent l'étiquette sans
        être dans le plan (`selector-new-vm`, une fois par VM, gardées en base
        pour `pending_vms`) et les VMs du plan qui l'ont perdue
        (`selector-vm-left`, une fois). Rien n'est protégé ici : le suiveur
        n'écrit que des relectures ; la protection automatique est un geste
        de la console (option `auto_protect`)."""
        p = v["plan"]
        cluster = p["source_cluster"]
        if cluster in self._down_now or self.vms_at.get(cluster) != self.t:
            return                              # VMs pas relues à ce tour : rien de neuf
        key, _, value = (p.get("selector") or "").partition("=")
        ns = p.get("selector_namespace") or ""
        vms = self.vms.get(cluster) or {}
        known = {r["resource_uid"]: r for r in v["resources"]}
        matching = {uid: vm for uid, vm in vms.items() if uid and _md(vm).get("namespace") == ns
                    and (_md(vm).get("labels") or {}).get(key) == value}
        prev = db.get_selector_watch(self.conn, p["id"])
        was = {x.get("uid"): x for x in prev["pending"]}
        now_iso = rp.iso(_utc(self.t))
        pending = []
        for uid in sorted(set(matching) - set(known)):
            md = _md(matching[uid])
            item = {"uid": uid, "namespace": md.get("namespace"), "name": md.get("name"),
                    "since": (was.get(uid) or {}).get("since") or now_iso}
            pending.append(item)
            if uid not in was:
                self.event(p, "selector", "selector-new-vm", uid, "source", "warn",
                           f"VM {item['namespace']}/{item['name']} matches the selector {p['selector']} but is"
                           f" not protected: run protect", facts={"vm": f"{item['namespace']}/{item['name']}"})
        left = sorted(uid for uid, r in known.items() if uid in vms and uid not in matching
                      and r["state"] in rp.STORED_RESOURCE_STATES and r["state"] not in rp.NOT_REPLICATING)
        for uid in left:
            if uid not in prev["left_uids"]:
                r = known[uid]
                self.event(p, "selector", "selector-vm-left", uid, "source", "info",
                           f"VM {r['namespace']}/{r['name']} no longer matches the selector {p['selector']}:"
                           f" it stays protected until unprotect", facts={"vm": f"{r['namespace']}/{r['name']}"})
        if [x["uid"] for x in pending] != [x.get("uid") for x in prev["pending"]] or left != prev["left_uids"] \
                or not prev.get("checked_at"):
            db.set_selector_watch(self.conn, p["id"], pending, left, now=_utc(self.t))

    def _after_failover(self, v):
        """Plan basculé (ou ressource basculée d'un plan encore protégé) : la
        VM source revenue en marche est signalée ; l'arrêter reste un geste
        de l'exploitant."""
        p = v["plan"]
        cluster = p["source_cluster"]
        if cluster not in self.vms_at or self.vms_at[cluster] != self.t:
            self._list_vms(cluster)
        if cluster in self._down_now:
            return
        vms = self.vms.get(cluster) or {}
        for r in v["resources"]:
            if r["state"] != "failed_over":
                continue
            vm = vms.get(r["resource_uid"])
            key = ("source-vm-running-after-failover", p["id"], r["resource_uid"])
            running = vm is not None and vm_is_running(vm)
            if running:
                if self.once_on(key):
                    self.event(p, "health", "source-vm-running-after-failover", r["resource_uid"], "source",
                               "warn", f"source VM {r['namespace']}/{r['name']} runs again after the failover:"
                                       f" stop it by hand", facts={"target": f"{r.get('target_namespace')}/"
                                                                             f"{r.get('target_name')}"})
            else:
                self.clear(key)
            # drapeau de santé lu par les alertes (`source-vm-running-after-failover`)
            hkey = (p["id"], r["resource_uid"])
            prev = self.written_health.get(hkey) or (r.get("health") or {})
            if bool(prev.get("source_running_after_failover")) != running:
                h = dict(prev, source_running_after_failover=running)
                db.set_health(self.conn, p["id"], r["resource_uid"], v["gen"], h, now=_utc(self.t))
                self.written_health[hkey] = h

    # -- état, statistiques, rythme -------------------------------------------------------------------------
    def _status(self, v, health):
        """RPO tenu, arriéré, dérive par VM (`replication.plan_status`) :
        `rpo-exceeded` et `rpo-drift` écrits à leur apparition, et une
        ligne de statistiques par passage entièrement appliqué."""
        p = v["plan"]
        ticks = db.list_ticks(self.conn, p["id"], v["gen"],
                              since=_utc(self.t - max(3600, (int(p.get("retain") or 6) + 2) * v["rh"]["interval"])))
        seen = {(t["source_volume"], t["backup"]) for t in ticks}
        ticks += [t for t in db.last_applied_ticks(self.conn, p["id"], v["gen"])
                  if (t["source_volume"], t["backup"]) not in seen]
        vm_running = {uid: {"running": h.get("running", True), "stopped_at": h.get("stopped_at")}
                      for uid, h in health.items() if "running" in h}
        st = rp.plan_status(p, v["follow"], v["volumes"], ticks, _utc(self.t), vm_running, health)
        for vm in st["vms"]:
            uid = vm["uid"]
            k = ("rpo-exceeded", p["id"], uid)
            if vm["exceeded"]:
                if self.once_on(k):
                    self.event(p, "status", "rpo-exceeded", uid, "target", "warn",
                               f"RPO held {vm['rpo_held_s']} s, objective {p['rpo_objective_s']} s",
                               facts={"rpo_held_s": vm["rpo_held_s"], "rpo_objective_s": p["rpo_objective_s"]})
            elif vm["rpo_held_s"] is not None and self.clear(k):
                self.event(p, "status", "rpo-recovered", uid, "target", "info",
                           f"RPO held {vm['rpo_held_s']} s, back under the objective",
                           facts={"rpo_held_s": vm["rpo_held_s"]})
            k = ("rpo-drift", p["id"], uid)
            if vm["drift"]:
                if self.once_on(k):
                    self.event(p, "status", "rpo-drift", uid, "target", "warn",
                               "a pass took longer than the interval between its snapshot and its application",
                               facts={"interval_s": p.get("interval_s"),
                                      "volumes": [d["source_volume"] for d in vm["disks"]]})
            else:
                self.clear(k)
            self._stats(v, vm, ticks)
        return st

    def _stats(self, v, vm, ticks):
        """Bilan d'un passage entièrement appliqué d'une VM (tous les disques
        qui l'ont terminé sont appliqués) : envoi, durée de sauvegarde,
        délai d'application, écart, RPO tenu, arriéré, débits."""
        p, interval = v["plan"], v["rh"]["interval"]
        uid = vm["uid"]
        vols = {x["source_volume"] for x in v["volumes"] if x["vm_uid"] == uid}
        slots = {}
        for t in ticks:
            if t["source_volume"] in vols and t.get("state") in ("applied", "completed", "in_progress") \
                    and pass_time(t):
                slots.setdefault(slot_of(t, interval), []).append(t)
        for s, items in sorted(slots.items()):
            if any(t["state"] != "applied" for t in items) or (p["id"], uid, s) in self.stats_done:
                continue
            got = {t["source_volume"] for t in items}
            if got != vols and self.t - min(_epoch(pass_time(t)) for t in items) < v["rh"]["barrier"]:
                continue
            self.stats_done[(p["id"], uid, s)] = self.t
            if not any(t.get("applied_at") and _epoch(t["applied_at"]) >= self.t - interval * 2 for t in items):
                continue                         # passage ancien relu après un redémarrage : déjà rangé
            pts = [_epoch(pass_time(t)) for t in items]

            def secs(a, b, t):
                return _epoch(t[b]) - _epoch(t[a]) if t.get(a) and t.get(b) else None
            backup_s = [x for x in (secs("backup_started_at", "backup_done_at", t) for t in items) if x is not None]
            lag = [x for x in (secs("backup_done_at", "applied_at", t) for t in items) if x is not None]
            r2a = [x for x in (secs("resync_at", "applied_at", t) for t in items) if x is not None]
            thr = vm.get("throughput") or {}
            db.record_stats(self.conn, p["id"], {
                "generation": v["gen"], "resource_uid": uid, "pass_at": rp.iso(_utc(min(pts))),
                "ts": rp.iso(_utc(self.t)), "disks": len(items),
                "newly_uploaded": sum(int(t.get("newly_uploaded") or 0) for t in items),
                "backup_s": max(backup_s) if backup_s else None, "apply_lag_s": max(lag) if lag else None,
                "resync_to_apply_s": max(r2a) if r2a else None,
                "skew_s": round(max(pts) - min(pts), 3), "rpo_held_s": vm["rpo_held_s"],
                "backlog": vm["backlog"], "upload_bps": (thr.get("upload") or {}).get("bps"),
                "apply_bps": (thr.get("apply") or {}).get("bps"), "state": vm["state"]})

    def _wake(self, v, ticks, open_work):
        """Prochain tour pour ce plan : lecture rapide (`follow_poll`) tant
        qu'un passage est ouvert, ou juste après une échéance du cron le
        temps que la tâche démarre ses sauvegardes (`follow_grace`), le tout
        borné par `follow_window` après l'échéance ; sinon la prochaine
        échéance, au plus tard la santé (`follow_health`)."""
        rh = v["rh"]
        t, interval = self.t, rh["interval"]
        slot_start = math.floor(t / interval) * interval
        in_window = t - slot_start < rh["follow_window"]
        if v["plan"]["state"] == "protected" and not open_work and t - slot_start < rh["follow_grace"]:
            # la tâche n'a encore rien montré pour ce passage : on regarde vite.
            # Pas pour une VM arrêtée : Longhorn ne sauvegarde pas un volume
            # détaché par une tâche récurrente (réglage par défaut)
            stopped = {uid for (pid, uid), h in self.written_health.items()
                       if pid == v["plan"]["id"] and h.get("running") is False}
            never = v.get("never") or set()
            for x in v["volumes"]:
                # ni VM arrêtée, ni disque qui ne sauvegarde jamais
                if x["vm_uid"] in stopped or x["source_volume"] in never:
                    continue
                seen = any(slot_of(tk, interval) == slot_start // interval
                           for tk in (ticks.get(x["source_volume"]) or {}).values())
                if not seen:
                    open_work = True
                    break
        if open_work and in_window:
            return t + rh["follow_poll"]
        nxt = _epoch(rp.next_tick_at(interval, _utc(t)))
        return min(nxt, t + rh["follow_health"])


def follow_once(conn, clusters, now=time.time, owner=None, **kw):
    """Un tour sans mémoire d'un tour à l'autre (ligne de commande
    `follow --once`). Rend le rapport du tour."""
    f = Follower(conn, clusters, owner or db.owner_id("cli"), now=now, **kw)
    try:
        return f.once_safe()
    finally:
        f.close()


def follow_loop(follower, stopper=None, sleep=time.sleep, max_rounds=None, on_round=None):
    """La boucle du suiveur : un tour, puis sommeil jusqu'au `next_wake` du
    rapport (par tranches d'une seconde pour entendre l'arrêt). S'arrête
    entre deux tours quand `stopper` est posé (SIGTERM, SIGINT), et rend
    ses baux. Ne lève pas sur une erreur de tour (`once_safe`)."""
    stopper = stopper or Stopper()
    # un signal reçu pendant un appel à un cluster l'interrompt (Stopper.call)
    follower.stopper = stopper
    rounds = 0
    try:
        while not stopper.is_set():
            report = follower.once_safe()
            rounds += 1
            if on_round:
                on_round(report)
            if max_rounds is not None and rounds >= max_rounds:
                break
            stopper.wait(max(0.0, report["next_wake"] - follower.now()), sleep)
    except Interrupted:
        rounds += 1                             # tour interrompu en plein appel
    finally:
        follower.close()
    return {"rounds": rounds, "stopped": stopper.is_set(), "signal": stopper.signum}
