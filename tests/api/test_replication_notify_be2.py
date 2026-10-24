"""Notifications externes de la réplication (`replication_notify`) et leur
ligne de commande (`notify show|set|test`, `follow --notify`).

Réglage contrôlé (un cas par refus), secrets masqués à chaque lecture et
gardés quand ils reviennent `********`, filtre niveau / codes, envois réels
vers un serveur HTTP local (Matrix, Slack, webhook) et un faux serveur SMTP,
reprises bornées, curseur du journal, envoi dans un fil à part."""

import http.server
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import replication_db as db  # noqa: E402
import replication_notify as rn  # noqa: E402

_spec = importlib.util.spec_from_file_location("harvester_replication_n", ROOT / "bin" / "harvester-replication.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

TOKEN, SLACK, AUTH, PASSWORD = "syt_matrix_secret_token", "https://hooks.slack.test/T0/B0/xyzsecret", \
    "Bearer hook-secret-1", "smtp-secret-pw"


def matrix(**kw):
    return dict({"id": "c1", "name": "ops room", "kind": "matrix", "homeserver": "https://matrix.example",
                 "room": "!abc:example", "token": TOKEN}, **kw)


def full():
    return {"enabled": True, "min_level": "warn", "codes": None, "channels": [
        matrix(), {"id": "c2", "kind": "slack", "url": SLACK},
        {"id": "c3", "kind": "webhook", "url": "https://hook.example/x", "headers": {"X-Env": "prod"},
         "secret_headers": {"Authorization": AUTH}},
        {"id": "c4", "kind": "email", "smtp_host": "smtp.example", "smtp_port": 587, "starttls": True,
         "username": "ops", "password": PASSWORD, "from": "hops@example", "to": ["ops@example"]}]}


SECRETS = (TOKEN, SLACK, AUTH, PASSWORD)


# ---------------------------------------------------------------------------
# Réglage
# ---------------------------------------------------------------------------

def test_normalize_full_and_defaults():
    cfg = rn.normalize(full())
    assert [c["id"] for c in cfg["channels"]] == ["c1", "c2", "c3", "c4"]
    assert rn.normalize({}) == {"enabled": True, "min_level": "warn", "codes": None, "channels": []}
    e = rn.normalize({"channels": [{"id": "m", "kind": "email", "smtp_host": "smtp.example", "from": "a@b.c",
                                    "to": ["d@e.f"]}]})["channels"][0]
    assert (e["smtp_port"], e["starttls"], e["username"], e["password"]) == (587, True, "", "")


def _bad(path, value):
    cfg = full()
    if path[0] == "root":
        cfg[path[1]] = value
    else:
        ch = cfg["channels"][path[0]]
        if value is KeyError:
            ch.pop(path[1])
        else:
            ch[path[1]] = value
    return cfg


@pytest.mark.parametrize("cfg,code", [
    ("x", "bad-notify"),
    (_bad(("root", "enabled"), "yes"), "bad-notify"),
    (_bad(("root", "min_level"), "info"), "bad-notify"),
    (_bad(("root", "codes"), []), "bad-notify"),
    (_bad(("root", "codes"), ["nope"]), "bad-notify"),
    (_bad(("root", "codes"), ["rpo-exceeded", "rpo-exceeded"]), "bad-notify"),
    (_bad(("root", "channels"), {"c1": {}}), "bad-notify"),
    (_bad(("root", "channels"), [matrix(id=f"c{i}") for i in range(21)]), "bad-notify"),
    (_bad(("root", "extra"), 1), "bad-notify"),
    (_bad((0, "kind"), "sms"), "bad-notify"),
    (_bad((0, "id"), "C 1"), "bad-notify"),
    (_bad((0, "id"), KeyError), "bad-notify"),
    (_bad((1, "id"), "c1"), "bad-notify"),                      # id en double
    (_bad((0, "homeserver"), "ftp://x"), "bad-notify"),
    (_bad((0, "room"), "abc"), "bad-notify"),
    (_bad((0, "token"), "a\nb"), "bad-notify"),
    (_bad((0, "token"), rn.MASK), "secret-missing"),           # rien de rangé à garder
    (_bad((0, "url"), "x"), "bad-notify"),                      # champ inconnu pour Matrix
    (_bad((1, "url"), "http://hooks.slack.test/x"), "bad-notify"),
    (_bad((2, "headers"), {"bad header": "x"}), "bad-notify"),
    (_bad((2, "headers"), {"Authorization": "x"}), "bad-notify"),  # à la fois clair et secret
    (_bad((2, "secret_headers"), ["x"]), "bad-notify"),
    (_bad((3, "smtp_port"), 0), "bad-notify"),
    (_bad((3, "smtp_port"), True), "bad-notify"),
    (_bad((3, "smtp_host"), "smtp example"), "bad-notify"),
    (_bad((3, "starttls"), "yes"), "bad-notify"),
    (_bad((3, "from"), "nobody"), "bad-notify"),
    (_bad((3, "to"), []), "bad-notify"),
    (_bad((3, "to"), ["x"]), "bad-notify"),
])
def test_normalize_refusals(cfg, code):
    with pytest.raises(rn.NotifyError) as e:
        rn.normalize(cfg)
    assert e.value.code == code
    for s in SECRETS:
        assert s not in str(e.value)


def test_mask_hides_every_secret_and_keeps_the_rest():
    shown = rn.mask(rn.normalize(full()))
    text = json.dumps(shown)
    for s in SECRETS:
        assert s not in text
    c1, c2, c3, c4 = shown["channels"]
    assert c1["token"] == rn.MASK and c2["url"] == rn.MASK and c4["password"] == rn.MASK
    assert c3["secret_headers"] == {"Authorization": rn.MASK} and c3["headers"] == {"X-Env": "prod"}
    assert c1["homeserver"] == "https://matrix.example" and c4["username"] == "ops"
    assert rn.mask(None) == {"enabled": False, "min_level": "warn", "codes": None, "channels": []}
    # un mot de passe vide reste vide (rien à cacher, et l'écran le dit)
    assert rn.mask({"channels": [dict(full()["channels"][3], password="")]})["channels"][0]["password"] == ""


def test_masked_values_keep_the_stored_secrets():
    stored = rn.normalize(full())
    again = rn.normalize(rn.mask(stored), stored)
    assert again == stored
    # un secret changé est pris, les autres gardés
    m = rn.mask(stored)
    m["channels"][0]["token"] = "new-token"
    assert rn.normalize(m, stored)["channels"][0]["token"] == "new-token"
    # même id, autre nature : rien n'est gardé
    m = rn.mask(stored)
    m["channels"][1] = {"id": "c1", "kind": "slack", "url": rn.MASK}
    m["channels"] = m["channels"][1:2]
    with pytest.raises(rn.NotifyError) as e:
        rn.normalize(m, stored)
    assert e.value.code == "secret-missing"


# ---------------------------------------------------------------------------
# Filtre
# ---------------------------------------------------------------------------

def ev(code, level="warn", op="follow", step="status", **kw):
    return dict({"id": 1, "operation": op, "step": step, "code": code, "level": level, "plan_id": "rp-0123456789ab",
                 "message": "m", "ts": "2026-10-25T10:00:00.000000Z"}, **kw)


@pytest.mark.parametrize("event,cfg,expect", [
    (ev("rpo-exceeded"), {}, "rpo-exceeded"),
    (ev("rpo-exceeded"), {"min_level": "error"}, None),
    (ev("backup-error", "error"), {"min_level": "error"}, "backup-error"),
    (ev("rpo-recovered", "info"), {"min_level": "error"}, "rpo-recovered"),      # résultat : sans niveau
    (ev("rpo-recovered", "info"), {"codes": ["rpo-exceeded"]}, None),
    (ev("failed-over", "info", op="failover", step="done"), {}, "failed-over"),
    (ev("failed-over", "info", op="protect", step="skip"), {}, None),
    (ev("interrupted", "warn", op="protect"), {}, "interrupted"),
    (ev("error", "error", op="plan", step="state"), {}, "plan-error"),
    (ev("protected", "info", op="plan", step="state"), {}, None),
    (ev("selector-new-vm"), {"codes": ["selector-new-vm"]}, "selector-new-vm"),
    (ev("lease-lost"), {}, None),
    (ev("notify-failed"), {}, None),                  # jamais d'envoi sur l'échec d'un envoi
    (ev("standby-degraded", "info"), {}, None),
])
def test_matches(event, cfg, expect):
    assert rn.matches(dict({"min_level": "warn", "codes": None}, **cfg), event) == expect


def test_format_text_caps_the_lines():
    items = [rn.item(ev("rpo-exceeded", id=i), "rpo-exceeded", {"rp-0123456789ab": {"name": "erp"}})
             for i in range(35)]
    text = rn.format_text(items)
    lines = text.splitlines()
    assert lines[0] == "harvester-ops replication: 35 event(s)" and lines[1] == "[warn] rpo-exceeded erp: m"
    assert len(lines) == 1 + rn.MESSAGE_LINES + 1 and lines[-1] == "... and 5 more"


# ---------------------------------------------------------------------------
# Envois réels : serveur HTTP local, faux SMTP
# ---------------------------------------------------------------------------

class _Hook(http.server.BaseHTTPRequestHandler):
    got = []
    status = 200

    def _handle(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Hook.got.append((self.command, self.path, dict(self.headers), json.loads(body or b"null")))
        self.send_response(_Hook.status)
        self.end_headers()
        self.wfile.write(b"{}")

    do_PUT = do_POST = _handle

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _Hook.got, _Hook.status = [], 200
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_matrix_put_with_bearer_and_txn(server):
    ch = matrix(homeserver=server, room="!abc:example.org")
    assert rn.send(ch, "hello") == 200
    [(method, path, headers, body)] = _Hook.got
    assert method == "PUT" and path.startswith("/_matrix/client/v3/rooms/%21abc%3Aexample.org/send/m.room.message/")
    assert len(path.rsplit("/", 1)[1]) == 32
    assert headers["Authorization"] == f"Bearer {TOKEN}" and body == {"msgtype": "m.text", "body": "hello"}
    rn.send(ch, "again")
    assert _Hook.got[0][1] != _Hook.got[1][1]            # identifiant de transaction neuf à chaque envoi


def test_slack_and_webhook(server):
    assert rn.send({"id": "s", "kind": "slack", "url": server + "/slack"}, "hi") == 200
    items = [{"code": "rpo-exceeded"}]
    rn.send({"id": "w", "kind": "webhook", "url": server + "/hook", "headers": {"X-Env": "prod"},
             "secret_headers": {"Authorization": AUTH}}, "hi", items)
    (m1, p1, _h1, b1), (m2, p2, h2, b2) = _Hook.got
    assert (m1, p1, b1) == ("POST", "/slack", {"text": "hi"})
    assert (m2, p2) == ("POST", "/hook") and h2["X-Env"] == "prod" and h2["Authorization"] == AUTH
    assert b2 == {"source": "harvester-ops", "kind": "replication", "text": "hi", "events": items}


def test_http_error_is_retried_and_never_shows_a_secret(server):
    _Hook.status = 500
    slept = []
    res = rn.deliver({"id": "s", "kind": "slack", "url": server + "/" + SLACK.rsplit("/", 1)[1]}, "x",
                     sleep=slept.append)
    assert res == {"channel": "s", "ok": False, "status": None, "tries": 3, "error": "HTTP 500"}
    assert len(_Hook.got) == 3 and slept == [1.0, 2.0]


def test_deliver_scrubs_secrets_from_errors_and_stops_at_first_success():
    calls = []

    def sender(ch, text, items, timeout):
        calls.append(timeout)
        if len(calls) < 2:
            raise RuntimeError(f"refused token {TOKEN}")
        return 200
    res = rn.deliver(matrix(), "x", sleep=lambda s: None, sender=sender)
    assert res["ok"] and res["tries"] == 2 and calls == [10, 10]
    res = rn.deliver(matrix(), "x", sleep=lambda s: None, sender=lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError(f"bad {TOKEN}")))
    assert TOKEN not in res["error"] and rn.MASK in res["error"]


def test_unreachable_server_is_a_failure_not_an_exception():
    res = rn.deliver({"id": "s", "kind": "webhook", "url": "http://127.0.0.1:9/x"}, "x", sleep=lambda s: None,
                     timeout=1)
    assert res["ok"] is False and res["error"].startswith("connection failed")


class FakeSMTP:
    sessions = []
    fail = None

    def __init__(self, host, port, timeout=None):
        self.log = [("connect", host, port, timeout)]
        FakeSMTP.sessions.append(self)
        if FakeSMTP.fail == "connect":
            raise ConnectionRefusedError("refused")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.log.append(("quit",))

    def starttls(self, context=None):
        self.log.append(("starttls", context is not None))

    def login(self, user, password):
        self.log.append(("login", user, password))
        if FakeSMTP.fail == "login":
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, msg):
        self.log.append(("send", msg["From"], msg["To"], msg["Subject"], msg.get_content()))


@pytest.mark.parametrize("starttls,username,expect", [
    (True, "ops", ["connect", "starttls", "login", "send", "quit"]),
    (False, "ops", ["connect", "login", "send", "quit"]),
    (True, "", ["connect", "starttls", "send", "quit"]),
])
def test_email(starttls, username, expect):
    FakeSMTP.sessions, FakeSMTP.fail = [], None
    ch = rn.normalize({"channels": [dict(full()["channels"][3], starttls=starttls, username=username)]})["channels"][0]
    assert rn.send(ch, "subject line\nbody", smtp=FakeSMTP) == 250
    [s] = FakeSMTP.sessions
    assert [x[0] for x in s.log] == expect
    assert s.log[0] == ("connect", "smtp.example", 587, 10)
    send = [x for x in s.log if x[0] == "send"][0]
    assert send[1:4] == ("hops@example", "ops@example", "subject line") and "body" in send[4]


@pytest.mark.parametrize("fail", ["connect", "login"])
def test_email_failures_are_retried_and_scrubbed(fail):
    FakeSMTP.sessions, FakeSMTP.fail = [], fail
    ch = rn.normalize({"channels": [full()["channels"][3]]})["channels"][0]
    res = rn.deliver(ch, "x", sleep=lambda s: None, sender=lambda c, t, i, timeout: rn.send(c, t, i, timeout,
                                                                                           smtp=FakeSMTP))
    assert res["ok"] is False and res["tries"] == 3 and len(FakeSMTP.sessions) == 3
    assert PASSWORD not in res["error"]


# ---------------------------------------------------------------------------
# Expéditeur du suiveur : curseur, lot, fil à part
# ---------------------------------------------------------------------------

class HeldThread:
    """Fil retenu : lancé à la main par le test."""
    made = []

    def __init__(self, target=None, daemon=None, name=None):
        self.target = target
        HeldThread.made.append(self)

    def start(self):
        pass

    def join(self, timeout=None):
        pass


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "n.db"))
    yield c
    c.close()


def test_notifier_starts_at_the_end_of_the_journal_then_sends_new_matching_events(conn):
    HeldThread.made = []
    sent = []
    n = rn.Notifier(deliver_fn=lambda ch, text, items: sent.append((ch["id"], text, items)) or
                    {"channel": ch["id"], "ok": True, "status": 200, "tries": 1, "error": ""},
                    thread_factory=HeldThread)
    db.record_event(conn, "follow", code="rpo-exceeded", level="warn", message="old")
    db.set_setting(conn, "notify", rn.normalize({"channels": [matrix(), {"id": "c2", "kind": "slack",
                                                                         "url": SLACK}]}))
    assert n.round(conn, db) == 0                       # premier tour : rien d'ancien
    top = db.get_setting(conn, n.CURSOR)
    assert top == db.max_event_id(conn)
    db.record_event(conn, "follow", code="lease-lost", level="warn", message="not notified")
    db.record_event(conn, "follow", code="rpo-exceeded", level="warn", message="late", plan_id="")
    assert n.round(conn, db) == 1 and len(HeldThread.made) == 1
    assert n.round(conn, db) == 0                       # lot en vol : jamais deux
    assert db.get_setting(conn, n.CURSOR) == top        # pas avancé avant la fin du lot
    HeldThread.made[0].target()                         # le fil envoie
    assert [s[0] for s in sent] == ["c1", "c2"] and sent[0][2][0]["message"] == "late"
    n.round(conn, db)
    assert db.get_setting(conn, n.CURSOR) == db.max_event_id(conn)


def test_notifier_failed_channel_is_journaled_and_cursor_moves_on(conn):
    HeldThread.made = []
    n = rn.Notifier(deliver_fn=lambda ch, text, items: {"channel": ch["id"], "ok": False, "status": None,
                                                         "tries": 3, "error": "HTTP 500"},
                    thread_factory=HeldThread)
    db.set_setting(conn, "notify", rn.normalize({"channels": [matrix()]}))
    n.round(conn, db)
    db.record_event(conn, "follow", code="backup-error", level="warn", message="b")
    n.round(conn, db)
    HeldThread.made[0].target()
    n.close(conn, db)
    fails = [e for e in db.events_after(conn, 0) if e["code"] == "notify-failed"]
    assert len(fails) == 1 and "HTTP 500" in fails[0]["message"] and fails[0]["facts"] == {"channel": "c1"}
    # l'échec de l'envoi n'est pas renvoyé lui-même : le curseur passe dessus
    assert n.round(conn, db) == 0 and db.get_setting(conn, n.CURSOR) == db.max_event_id(conn)


@pytest.mark.parametrize("cfg", [None, {"enabled": False, "channels": [matrix()]}, {"enabled": True, "channels": []}])
def test_notifier_disabled_skips_the_backlog(conn, cfg):
    n = rn.Notifier(deliver_fn=None, thread_factory=HeldThread)
    if cfg:
        db.set_setting(conn, "notify", cfg)
    n.round(conn, db)
    db.record_event(conn, "follow", code="rpo-exceeded", level="warn")
    assert n.round(conn, db) == 0 and db.get_setting(conn, n.CURSOR) == db.max_event_id(conn)


def test_notifier_cursor_beyond_the_journal_is_reset(conn):
    db.set_setting(conn, rn.Notifier.CURSOR, 999)
    rn.Notifier(thread_factory=HeldThread).round(conn, db)
    assert db.get_setting(conn, rn.Notifier.CURSOR) == db.max_event_id(conn)


def test_notifier_real_thread_never_blocks_the_round(conn):
    gate = threading.Event()

    def slow(ch, text, items):
        gate.wait(5)
        return {"channel": ch["id"], "ok": True, "status": 200, "tries": 1, "error": ""}
    n = rn.Notifier(deliver_fn=slow)
    db.set_setting(conn, "notify", rn.normalize({"channels": [matrix()]}))
    n.round(conn, db)
    db.record_event(conn, "follow", code="rpo-exceeded", level="warn")
    import time
    t0 = time.monotonic()
    assert n.round(conn, db) == 1
    assert time.monotonic() - t0 < 1.0                  # le tour rend la main tout de suite
    gate.set()
    n.close(conn, db, wait=5)
    assert db.get_setting(conn, n.CURSOR) == db.max_event_id(conn)


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def run(capsys, *argv):
    rc = cli.main(list(argv) + ["--json"])
    out, err = capsys.readouterr()
    return rc, json.loads(out), out + err


def test_cli_notify_set_show_test_never_print_a_secret(tmp_path, capsys, monkeypatch, server):
    dbp = tmp_path / "c.db"
    spec = tmp_path / "spec.json"
    cfg = full()
    cfg["channels"][1]["url"] = SLACK
    cfg["channels"].append({"id": "c5", "kind": "webhook", "url": server + "/hook",
                            "secret_headers": {"Authorization": AUTH}})
    spec.write_text(json.dumps(cfg))
    rc, data, text = run(capsys, "notify", "set", f"--db={dbp}", f"--spec={spec}", "--by=alice", "--id=a1")
    assert rc == 0 and data["ok"] and data["notify"]["channels"][0]["token"] == rn.MASK
    for s in SECRETS:
        assert s not in text
    rc, data, text = run(capsys, "notify", "show", f"--db={dbp}")
    assert rc == 0 and [c["id"] for c in data["notify"]["channels"]] == ["c1", "c2", "c3", "c4", "c5"]
    for s in SECRETS:
        assert s not in text
    # renvoyer l'écran tel quel garde les secrets rangés
    spec.write_text(json.dumps(dict(data["notify"], min_level="error")))
    rc, data, _ = run(capsys, "notify", "set", f"--db={dbp}", f"--spec={spec}")
    c = db.connect(str(dbp))
    stored = db.get_setting(c, "notify")
    assert rc == 0 and stored["min_level"] == "error" and stored["channels"][0]["token"] == TOKEN
    assert stored["channels"][4]["secret_headers"] == {"Authorization": AUTH}
    # journal : aucun secret, auteur et action liés
    evs = [e for e in db.events_after(c, 0) if e["code"] == "notify-set"]
    assert evs and evs[0]["by"] == "alice" and evs[0]["action_id"] == "a1"
    assert all(s not in json.dumps(evs) for s in SECRETS)
    c.close()
    rc, data, text = run(capsys, "notify", "test", f"--db={dbp}", "--channel=c5")
    assert rc == 0 and data["result"]["ok"] and _Hook.got[-1][2]["Authorization"] == AUTH
    assert AUTH not in text
    rc, data, _ = run(capsys, "notify", "test", f"--db={dbp}", "--channel=nope")
    assert rc == 2 and data["kind"] == "unknown-channel"


@pytest.mark.parametrize("spec,kind", [(None, "usage"), ("{not json", "usage"),
                                        (json.dumps({"channels": [matrix(token=rn.MASK)]}), "secret-missing"),
                                        (json.dumps({"min_level": "info"}), "bad-notify")])
def test_cli_notify_set_refusals(tmp_path, capsys, spec, kind):
    argv = ["notify", "set", f"--db={tmp_path / 'c.db'}"]
    if spec is not None:
        (tmp_path / "s.json").write_text(spec)
        argv.append(f"--spec={tmp_path / 's.json'}")
    rc, data, _ = run(capsys, *argv)
    assert rc == 2 and data["kind"] == kind


def test_cli_notify_test_failure_exits_1_without_secret(tmp_path, capsys, monkeypatch):
    dbp = tmp_path / "c.db"
    c = db.connect(str(dbp))
    db.set_setting(c, "notify", rn.normalize({"channels": [matrix(homeserver="http://127.0.0.1:9")]}))
    c.close()
    monkeypatch.setattr(cli, "SLEEP", lambda s: None)
    rc, data, text = run(capsys, "notify", "test", f"--db={dbp}", "--channel=c1")
    assert rc == 1 and data["kind"] == "notify-failed" and data["result"]["tries"] == 3
    assert TOKEN not in text


# ---------------------------------------------------------------------------
# `follow` : qui envoie
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv,sends", [
    (["--all", "--console"], True),
    (["--all", "--notify"], True),
    (["--all"], False),
    (["--all", "--console", "--once"], False),
])
def test_follow_sends_notifications_from_the_console_follower_only(tmp_path, capsys, monkeypatch, argv, sends):
    made = []

    class Rec:
        def __init__(self, by=None):
            self.calls = []
            made.append(self)

        def round(self, conn, dbm):
            self.calls.append("round")

        def close(self, conn, dbm):
            self.calls.append("close")

    def loop(follower, stopper, sleep=None, on_round=None):
        on_round({"followed": [], "skipped": {}, "resyncs": 0, "applied": 0, "events": 0, "unreachable": []})
        return {"rounds": 1, "stopped": True, "signal": None}
    monkeypatch.setattr(rn, "Notifier", Rec)
    monkeypatch.setattr(cli.rf, "follow_loop", loop)
    rc = cli.main(["follow", *argv, f"--db={tmp_path / 'f.db'}", "--json"])
    capsys.readouterr()
    assert rc == 0
    assert bool(made) is sends
    if sends:
        assert made[0].calls == ["round", "close"]


def test_follow_survives_a_failing_notifier(tmp_path, capsys, monkeypatch):
    class Boom:
        def __init__(self, by=None):
            pass

        def round(self, conn, dbm):
            raise RuntimeError("smtp down")

        def close(self, conn, dbm):
            pass
    rounds = []

    def loop(follower, stopper, sleep=None, on_round=None):
        on_round({"followed": [], "skipped": {}, "resyncs": 0, "applied": 0, "events": 0, "unreachable": []})
        rounds.append(1)
        return {"rounds": 1, "stopped": True, "signal": None}
    monkeypatch.setattr(rn, "Notifier", Boom)
    monkeypatch.setattr(cli.rf, "follow_loop", loop)
    rc = cli.main(["follow", "--all", "--console", f"--db={tmp_path / 'f.db'}", "--json"])
    _out, err = capsys.readouterr()
    assert rc == 0 and rounds == [1] and "notify-error" in err
