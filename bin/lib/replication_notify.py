"""harvester-ops : notifications externes de la réplication.

Matrix (API client-serveur), Slack (webhook entrant), webhook JSON
générique et courriel (smtplib ; STARTTLS ou TLS implicite exigé dès
qu'un mot de passe est donné).

Appels sortants par la garde anti-SSRF (`replication_http`) : adresse
interne refusée sauf `replication.notifications.allow_private`, aucune
redirection suivie (le jeton Matrix et les en-têtes secrets ne partent
jamais ailleurs), serveur SMTP vérifié de la même façon. Le réglage vit dans
replication.db (table `settings`, clé `notify`) ; seule la ligne de commande
l'écrit (`notify set --spec FICHIER`), la console le lit masqué.

Secrets (`token` Matrix, `url` Slack, valeurs de `secret_headers`,
`password` du courriel) : rangés dans la base privée, remplacés par
`********` à CHAQUE lecture (API, sortie de la ligne de commande, journal,
Activité). À l'écriture, la valeur `********` garde celle rangée.

Envoi : par le processus du suiveur (`follow --console`), à chaque tour.
Les nouveaux événements du journal (id au-delà du curseur rangé) qui
passent le filtre (niveau, codes) sont groupés en un message par tour et
par canal, envoyés dans un fil à part (délai de 10 s par appel, 3 essais) :
le suivi n'attend jamais un serveur de messagerie. Le curseur avance une
fois le lot traité (envoyé ou abandonné après 3 essais), au tour suivant ;
un arrêt pendant l'envoi fait renvoyer le lot au redémarrage (au moins une
fois, jamais perdu en silence).

Bibliothèque standard seulement.
"""

import copy
import json
import re
import ssl
import threading
import time
import urllib.error
import uuid
from email.message import EmailMessage
from urllib.parse import quote, urlparse

import replication_http as rh
import replication_insight as ri
import url_fetch_guard as ufg

MASK = "********"
KINDS = ("matrix", "slack", "webhook", "email")
LEVELS = ("warn", "error")
# codes qu'un réglage peut choisir : les alertes, plus les résultats de
# bascule et le retour sous l'objectif
RESULT_CODES = ("failed-over", "interrupted", "rpo-recovered")
NOTIFY_CODES = ri.ALERT_CODES + RESULT_CODES
# résultats : envoyés quel que soit `min_level` (un retour à la normale est
# `info` au journal, et c'est justement ce qu'on veut savoir)
LEVEL_FREE = ("failed-over", "rpo-recovered")
SECRET_FIELDS = {"matrix": ("token",), "slack": ("url",), "webhook": (), "email": ("password",)}
CHANNEL_FIELDS = {
    "matrix": ("id", "name", "kind", "homeserver", "room", "token"),
    "slack": ("id", "name", "kind", "url"),
    "webhook": ("id", "name", "kind", "url", "headers", "secret_headers"),
    "email": ("id", "name", "kind", "smtp_host", "smtp_port", "starttls", "implicit_tls", "username", "password",
              "from", "to"),
}
MAX_CHANNELS = 20
TIMEOUT_S = 10
TRIES = 3
BATCH_EVENTS = 500
MESSAGE_LINES = 30
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$|^\[[0-9A-Fa-f:.]+\]$")
_MAIL_RE = re.compile(r"^[^@\s<>]{1,64}@[A-Za-z0-9.-]{1,253}$")
_ROOM_RE = re.compile(r"^[!#][^\s]{1,254}$")


class NotifyError(ValueError):
    """Réglage refusé : `code` stable, message lisible (sans secret)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Réglage : contrôle, masquage
# ---------------------------------------------------------------------------

def _text(v, field, limit=512, empty=False):
    if not isinstance(v, str) or (not v and not empty) or len(v) > limit or \
            any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise NotifyError("bad-notify", f"{field}: invalid value")
    return v


def _url(v, field, schemes=("http", "https")):
    _text(v, field, 2048)
    u = urlparse(v)
    if u.scheme not in schemes or not u.netloc:
        raise NotifyError("bad-notify", f"{field}: an {' or '.join(schemes)} URL is expected")
    return v


def _headers(raw, field):
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > 20:
        raise NotifyError("bad-notify", f"{field}: an object of at most 20 headers is expected")
    out = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not _HEADER_RE.match(k):
            raise NotifyError("bad-notify", f"{field}: invalid header name")
        out[k] = _text(v, f"{field}.{k}", 4096, empty=True)
    return out


def _secret(value, stored, field):
    """Valeur d'un secret : `********` garde celle rangée (refus s'il n'y en
    a pas)."""
    if value == MASK:
        if stored in (None, ""):
            raise NotifyError("secret-missing", f"{field}: no stored value to keep")
        return stored
    return value


def _channel(raw, stored):
    if not isinstance(raw, dict):
        raise NotifyError("bad-notify", "channels: objects are expected")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise NotifyError("bad-notify", f"channel kind: one of {', '.join(KINDS)}")
    unknown = set(raw) - set(CHANNEL_FIELDS[kind])
    if unknown:
        raise NotifyError("bad-notify", f"channel: unknown field {sorted(unknown)[0][:40]}")
    cid = raw.get("id")
    if not isinstance(cid, str) or not _ID_RE.match(cid):
        raise NotifyError("bad-notify", "channel id: lowercase letters, digits and dashes (40 at most)")
    # secrets gardés seulement d'un canal de même id ET de même nature, et
    # pour un jeton, vers le même serveur Matrix (schéma, hôte, port), pour
    # des en-têtes, vers la même adresse exacte (chemin et requête compris) :
    # sinon le secret partirait ailleurs
    old = stored if stored and stored.get("kind") == kind else {}
    out = {"id": cid, "name": _text(raw.get("name") or "", "name", 80, empty=True), "kind": kind}
    if kind == "matrix":
        out["homeserver"] = _url(raw.get("homeserver"), "homeserver").rstrip("/")
        out["room"] = _text(raw.get("room"), "room", 255)
        if not _ROOM_RE.match(out["room"]):
            raise NotifyError("bad-notify", "room: a room id (!...) or alias (#...) is expected")
        kept_token = old.get("token") if rh.same_origin(out["homeserver"], old.get("homeserver")) else None
        if raw.get("token") == MASK and old.get("token") and kept_token is None:
            raise NotifyError("secret-missing", "token: the homeserver changed, type the token again")
        out["token"] = _text(_secret(raw.get("token"), kept_token, "token"), "token", 1024)
    elif kind == "slack":
        out["url"] = _url(_secret(raw.get("url"), old.get("url"), "url"), "url", ("https",))
    elif kind == "webhook":
        url = raw.get("url")
        if old.get("url") and url == rh.mask_url(old["url"]):
            url = old["url"]                    # adresse renvoyée masquée : celle rangée
        out["url"] = _url(url, "url")
        if MASK in out["url"]:
            raise NotifyError("bad-notify", "url: the stored url is partly hidden, type the full url again")
        out["headers"] = _headers(raw.get("headers"), "headers")
        sh = raw.get("secret_headers") or {}
        if not isinstance(sh, dict):
            raise NotifyError("bad-notify", "secret_headers: an object is expected")
        same = rh.same_target(out["url"], old.get("url"))
        if not same and MASK in sh.values() and old.get("secret_headers"):
            raise NotifyError("secret-missing", "secret_headers: the url changed, type the values again")
        old_sh = (old.get("secret_headers") or {}) if same else {}
        kept = {k: _secret(v, old_sh.get(k), f"secret_headers.{k}") for k, v in sh.items()}
        out["secret_headers"] = _headers(kept, "secret_headers")
        if set(out["headers"]) & set(out["secret_headers"]):
            raise NotifyError("bad-notify", "a header cannot be both plain and secret")
    else:
        out["smtp_host"] = _text(raw.get("smtp_host"), "smtp_host", 253)
        if not _HOST_RE.match(out["smtp_host"]):
            raise NotifyError("bad-notify", "smtp_host: a host name is expected")
        port = raw.get("smtp_port", 587)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise NotifyError("bad-notify", "smtp_port: 1 to 65535")
        out["smtp_port"] = port
        st = raw.get("starttls", True)
        if not isinstance(st, bool):
            raise NotifyError("bad-notify", "starttls: true or false")
        imp = raw.get("implicit_tls", False)
        if not isinstance(imp, bool):
            raise NotifyError("bad-notify", "implicit_tls: true or false")
        if imp and st:
            raise NotifyError("bad-notify", "starttls and implicit_tls: choose one (implicit TLS is port 465)")
        out["starttls"] = st
        out["implicit_tls"] = imp
        out["username"] = _text(raw.get("username") or "", "username", 256, empty=True)
        pw = raw.get("password") or ""
        # le mot de passe garde sa valeur rangée seulement vers le même serveur
        same = (old.get("smtp_host"), old.get("smtp_port")) == (out["smtp_host"], out["smtp_port"])
        if pw == MASK and old.get("password") and not same:
            raise NotifyError("secret-missing", "password: the SMTP server changed, type the password again")
        out["password"] = _text(_secret(pw, old.get("password") if same else None, "password") if pw else "",
                                "password", 1024, empty=True)
        if out["password"] and not (st or imp):
            raise NotifyError("bad-notify", "password: needs starttls or implicit_tls, so that it is never sent "
                                            "in clear")
        out["from"] = _text(raw.get("from"), "from", 320)
        if not _MAIL_RE.match(out["from"]):
            raise NotifyError("bad-notify", "from: an e-mail address is expected")
        to = raw.get("to")
        if not isinstance(to, list) or not 1 <= len(to) <= 20:
            raise NotifyError("bad-notify", "to: a list of 1 to 20 e-mail addresses is expected")
        for a in to:
            if not isinstance(a, str) or not _MAIL_RE.match(a):
                raise NotifyError("bad-notify", "to: an e-mail address is expected")
        out["to"] = list(to)
    return out


def normalize(raw, stored=None):
    """Un réglage `notify` contrôlé et complété de ses valeurs par défaut ;
    `stored` : le réglage rangé (secrets à garder). NotifyError sinon."""
    if not isinstance(raw, dict):
        raise NotifyError("bad-notify", "a JSON object is expected")
    unknown = set(raw) - {"enabled", "min_level", "codes", "channels"}
    if unknown:
        raise NotifyError("bad-notify", f"unknown field: {sorted(unknown)[0][:40]}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise NotifyError("bad-notify", "enabled: true or false")
    level = raw.get("min_level", "warn")
    if level not in LEVELS:
        raise NotifyError("bad-notify", "min_level: warn or error")
    codes = raw.get("codes")
    if codes is not None:
        if not isinstance(codes, list) or not codes:
            raise NotifyError("bad-notify", "codes: null (every code) or a non-empty list")
        for c in codes:
            if c not in NOTIFY_CODES:
                raise NotifyError("bad-notify", f"codes: unknown code {str(c)[:40]}")
        if len(set(codes)) != len(codes):
            raise NotifyError("bad-notify", "codes: listed twice")
    chans = raw.get("channels") or []
    if not isinstance(chans, list) or len(chans) > MAX_CHANNELS:
        raise NotifyError("bad-notify", f"channels: a list of at most {MAX_CHANNELS}")
    old = {c.get("id"): c for c in ((stored or {}).get("channels") or []) if isinstance(c, dict)}
    out, seen = [], set()
    for c in chans:
        cid = c.get("id") if isinstance(c, dict) else None
        if cid in seen:
            raise NotifyError("bad-notify", "channel id: listed twice")
        seen.add(cid)
        out.append(_channel(c, old.get(cid)))
    return {"enabled": enabled, "min_level": level, "codes": list(codes) if codes is not None else None,
            "channels": out}


def mask(cfg):
    """Le réglage sans aucun secret : chaque valeur secrète non vide devient
    `********`. Rend un réglage vide (désactivé) pour None."""
    if not cfg:
        return {"enabled": False, "min_level": "warn", "codes": None, "channels": []}
    out = copy.deepcopy(cfg)
    for c in out.get("channels") or []:
        for f in SECRET_FIELDS.get(c.get("kind"), ()):
            if c.get(f):
                c[f] = MASK
        if c.get("secret_headers"):
            c["secret_headers"] = {k: MASK for k in c["secret_headers"]}
        if c.get("kind") == "webhook" and c.get("url"):
            c["url"] = rh.mask_url(c["url"])    # un jeton dans la requête reste caché
    return out


def secrets_of(cfg):
    """Toutes les valeurs secrètes (pour les retirer d'un message d'erreur)."""
    out = []
    for c in (cfg or {}).get("channels") or []:
        for f in SECRET_FIELDS.get(c.get("kind"), ()):
            if c.get(f):
                out.append(str(c[f]))
        out += [str(v) for v in (c.get("secret_headers") or {}).values() if v]
    return out


def scrub_secrets(text, secrets):
    s = str(text)
    for v in sorted(set(secrets), key=len, reverse=True):
        if len(v) >= 4:
            s = s.replace(v, MASK)
    return s


# ---------------------------------------------------------------------------
# Événements -> notifications
# ---------------------------------------------------------------------------

def notify_code(event):
    """Code de notification d'un événement du journal (None : jamais
    notifié). Passage du plan en erreur : `plan-error` ; bascule réussie :
    `failed-over` (bascule seulement, pas le saut d'une VM déjà basculée à
    la protection)."""
    op, code = event.get("operation"), event.get("code") or ""
    if op == "plan" and event.get("step") == "state":
        return "plan-error" if code == "error" else None
    if code == "failed-over":
        return code if op == "failover" else None
    if code in NOTIFY_CODES:
        return code
    return None


def matches(cfg, event):
    code = notify_code(event)
    if code is None:
        return None
    codes = cfg.get("codes")
    if codes is not None and code not in codes:
        return None
    if code not in LEVEL_FREE:
        lv = event.get("level")
        if lv not in LEVELS or LEVELS.index(lv) < LEVELS.index(cfg.get("min_level") or "warn"):
            return None
    return code


def item(event, code, plans):
    p = plans.get(event.get("plan_id")) or {}
    return {"id": event.get("id"), "ts": event.get("ts"), "code": code, "level": event.get("level"),
            "plan": event.get("plan_id") or "", "plan_name": p.get("name") or "", "host": event.get("host") or "",
            "resource_uid": event.get("resource_uid") or "", "message": event.get("message") or ""}


def format_text(items, title="harvester-ops replication"):
    lines = [f"{title}: {len(items)} event(s)"]
    for x in items[:MESSAGE_LINES]:
        where = x["plan_name"] or x["plan"] or "-"
        lines.append(f"[{x['level']}] {x['code']} {where}: {x['message']}")
    if len(items) > MESSAGE_LINES:
        lines.append(f"... and {len(items) - MESSAGE_LINES} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Envoi
# ---------------------------------------------------------------------------

class SendRefused(Exception):
    """Envoi refusé avant tout appel (adresse, TLS) : message sans secret."""


class HTTPStatusError(Exception):
    """Réponse hors 2xx."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def _http(method, url, body, headers, timeout, allow, requester):
    data = json.dumps(body).encode()
    try:
        status = requester(method, url, headers=dict({"Content-Type": "application/json"}, **headers),
                           body=data, timeout=timeout, allow=allow)
    except ufg.FetchRefused as e:
        if e.reason in ("connect", "tls", "dns", "timeout"):
            raise ConnectionError(f"connection failed: {e.message}") from None
        raise SendRefused(rh.refusal_text(e, "notifications")) from None
    if not 200 <= int(status) < 300:
        raise HTTPStatusError(int(status))
    return int(status)


def _smtp_factory(allow):
    def make(host, port, timeout=None, implicit_tls=False):
        try:
            return rh.pinned_smtp(host, port, timeout, allow, implicit_tls=implicit_tls)
        except ufg.FetchRefused as e:
            raise SendRefused(rh.refusal_text(e, "notifications")) from None
    return make


def send(channel, text, items=(), timeout=TIMEOUT_S, allow=None, requester=None, smtp=None):
    """Un envoi sur un canal (un seul essai). Rend le statut ; lève sinon.
    `allow` : réseaux internes autorisés (par défaut
    `replication.notifications.allow_private` de config.yaml)."""
    allow = rh.allow_for("notifications") if allow is None else allow
    requester = requester or rh.request
    kind = channel.get("kind")
    if kind == "matrix":
        txn = uuid.uuid4().hex
        url = (f"{channel['homeserver']}/_matrix/client/v3/rooms/{quote(channel['room'], safe='')}"
               f"/send/m.room.message/{txn}")
        return _http("PUT", url, {"msgtype": "m.text", "body": text},
                     {"Authorization": f"Bearer {channel['token']}"}, timeout, allow, requester)
    if kind == "slack":
        return _http("POST", channel["url"], {"text": text}, {}, timeout, allow, requester)
    if kind == "webhook":
        headers = dict(channel.get("headers") or {}, **(channel.get("secret_headers") or {}))
        return _http("POST", channel["url"], {"source": "harvester-ops", "kind": "replication", "text": text,
                                              "events": list(items)}, headers, timeout, allow, requester)
    if kind == "email":
        implicit = bool(channel.get("implicit_tls"))
        starttls = bool(channel.get("starttls", True)) and not implicit
        if channel.get("password") and not (starttls or implicit):
            # réglage rangé avant la règle : jamais de mot de passe en clair
            raise SendRefused("password set without starttls or implicit_tls: not sent in clear")
        msg = EmailMessage()
        msg["Subject"] = text.splitlines()[0][:200]
        msg["From"] = channel["from"]
        msg["To"] = ", ".join(channel["to"])
        msg.set_content(text)
        port = int(channel.get("smtp_port") or (465 if implicit else 587))
        make = smtp or _smtp_factory(allow)
        kw = {"timeout": timeout}
        if implicit:
            kw["implicit_tls"] = True
        with make(channel["smtp_host"], port, **kw) as s:
            if starttls:
                s.starttls(context=ssl.create_default_context())
            if channel.get("username"):
                s.login(channel["username"], channel.get("password") or "")
            s.send_message(msg)
        return 250
    raise ValueError(f"unknown channel kind {kind}")


def address_refusal(cfg, allow, resolver=None):
    """Contrôle à l'enregistrement des adresses des canaux par la garde
    anti-SSRF : NotifyError `notify-address` pour une adresse interne non
    autorisée (`replication.notifications.allow_private`). Un nom qui ne se
    résout pas encore passe (revérifié à l'envoi)."""
    for c in (cfg or {}).get("channels") or []:
        kind = c.get("kind")
        try:
            if kind == "email":
                host = str(c.get("smtp_host") or "")
                rh.check_address(f"http://{host}:{int(c.get('smtp_port') or 587)}/", allow, resolver=resolver,
                                 strict_dns=False)
            else:
                rh.check_address(c.get("homeserver") if kind == "matrix" else c.get("url"), allow,
                                 resolver=resolver, strict_dns=False)
        except ufg.FetchRefused as e:
            raise NotifyError("notify-address", f"channel {c.get('id')}: {rh.refusal_text(e, 'notifications')}") \
                from None


def _reason(e):
    if isinstance(e, ConnectionError) and str(e).startswith("connection failed"):
        return str(e)
    if isinstance(e, SendRefused):
        return f"refused: {e}"
    if isinstance(e, HTTPStatusError):
        return str(e)
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"connection failed: {e.reason}"
    return f"{type(e).__name__}: {e}"


def deliver(channel, text, items=(), tries=TRIES, timeout=TIMEOUT_S, sleep=time.sleep, sender=send):
    """Envoi avec reprises (3 essais, 1 s puis 2 s d'attente). Rend
    {"channel", "ok", "status", "tries", "error"} ; jamais d'exception, ni
    de secret dans `error`."""
    last = ""
    for n in range(1, tries + 1):
        try:
            status = sender(channel, text, items, timeout=timeout)
            return {"channel": channel.get("id"), "ok": True, "status": status, "tries": n, "error": ""}
        except Exception as e:                  # noqa: BLE001 - le canal suivant passe quand même
            last = scrub_secrets(_reason(e), secrets_of({"channels": [channel]}))[:300]
            if isinstance(e, SendRefused):
                # refus de la garde (adresse, redirection, TLS exigé) : un autre
                # essai donnerait le même refus
                return {"channel": channel.get("id"), "ok": False, "status": None, "tries": n, "error": last}
        if n < tries:
            sleep(float(n))
    return {"channel": channel.get("id"), "ok": False, "status": None, "tries": tries, "error": last}


class Notifier:
    """L'envoi des notifications par le processus du suiveur : `round(conn)`
    à chaque tour (fil principal, seul à toucher la base), envoi dans un fil
    à part. Tout est injectable pour les tests."""

    CURSOR = "notify_cursor"

    def __init__(self, deliver_fn=deliver, thread_factory=threading.Thread, by="follower"):
        self.deliver = deliver_fn
        self.thread_factory = thread_factory
        self.by = by
        self.thread = None
        self.batch = None           # {"upto": id, "results": [...] | None}

    def _absorb(self, conn, db):
        """Lot fini : curseur avancé, canaux en échec écrits au journal."""
        b = self.batch
        if b is None or b.get("results") is None:
            return
        db.set_setting(conn, self.CURSOR, b["upto"], by=self.by)
        for r in b["results"]:
            if not r["ok"]:
                db.record_event(conn, "follow", step="notify", level="warn", code="notify-failed",
                                message=f"notification to channel {r['channel']} failed after {r['tries']}"
                                        f" tries: {r['error']}", facts={"channel": r["channel"]}, by=self.by)
        self.batch, self.thread = None, None

    def round(self, conn, db):
        """Un tour : range le lot précédent s'il est fini, puis lance le
        suivant. Rend le nombre d'événements du lot lancé (0 sinon)."""
        self._absorb(conn, db)
        if self.batch is not None:
            return 0                            # lot encore en vol : jamais deux à la fois
        cursor = db.get_setting(conn, self.CURSOR)
        top = db.max_event_id(conn)
        if not isinstance(cursor, int) or cursor > top:
            # premier tour (ou base remplacée) : rien d'ancien n'est envoyé
            db.set_setting(conn, self.CURSOR, top, by=self.by)
            return 0
        cfg = db.get_setting(conn, "notify") or {}
        if not cfg.get("enabled") or not cfg.get("channels"):
            if top != cursor:
                db.set_setting(conn, self.CURSOR, top, by=self.by)
            return 0
        events = db.events_after(conn, cursor, BATCH_EVENTS)
        if not events:
            return 0
        upto = events[-1]["id"]
        plans = {}
        items = []
        for e in events:
            code = matches(cfg, e)
            if code is None:
                continue
            pid = e.get("plan_id")
            if pid and pid not in plans:
                plans[pid] = db.get_plan(conn, pid) or {}
            items.append(item(e, code, plans))
        if not items:
            db.set_setting(conn, self.CURSOR, upto, by=self.by)
            return 0
        text = format_text(items)
        channels = list(cfg["channels"])
        batch = {"upto": upto, "results": None}

        def work():
            res = []
            for ch in channels:
                res.append(self.deliver(ch, text, items))
            batch["results"] = res
        self.batch = batch
        self.thread = self.thread_factory(target=work, daemon=True, name="replication-notify")
        self.thread.start()
        return len(items)

    def close(self, conn, db, wait=2.0):
        """Arrêt du suiveur : un lot fini est rangé ; un lot encore en vol
        après `wait` secondes sera renvoyé au redémarrage."""
        if self.thread is not None and hasattr(self.thread, "join"):
            self.thread.join(wait)
        try:
            self._absorb(conn, db)
        except Exception:                       # noqa: BLE001 - jamais au prix de l'arrêt
            pass


def sample_text():
    return "harvester-ops replication: test notification"
