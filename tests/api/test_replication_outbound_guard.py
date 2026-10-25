"""Appels sortants de la réplication (actions annexes, notifications) par la
garde anti-SSRF, et secrets jamais renvoyés ailleurs (revue des écrans de
réplication : C1, I1, I2, I3).

- adresse interne refusée (bouclage, métadonnées, privée, IPv6 mappée,
  nom qui résout vers une adresse interne), sauf
  `replication.hooks.allow_private` / `replication.notifications.allow_private` ;
- aucune redirection suivie : l'en-tête secret n'atteint jamais l'autre
  serveur ;
- un secret masqué n'est gardé que vers la même adresse (schéma, hôte, port) ;
- une adresse se montre sans sa requête (lectures, sorties, journal) ;
- un mot de passe SMTP exige STARTTLS ou TLS implicite.
"""

import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin" / "lib"))
import replication as rp  # noqa: E402
import replication_http as rh  # noqa: E402
import replication_notify as rn  # noqa: E402
import replication_run as rr  # noqa: E402
import url_fetch_guard as ufg  # noqa: E402

SECRET = "Bearer gu4rd-s3cr3t"
TOKEN = "syt_guard_matrix_token"
QTOKEN = "qs-t0ken-123"


# ---------------------------------------------------------------------------
# Serveurs locaux : une cible qui redirige, une seconde qui note tout
# ---------------------------------------------------------------------------

class _Rec(http.server.BaseHTTPRequestHandler):
    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.server.seen.append({"method": self.command, "path": self.path,
                                 "headers": {k.lower(): v for k, v in self.headers.items()}})
        loc = getattr(self.server, "redirect_to", None)
        if loc:
            self.send_response(302)
            self.send_header("Location", loc)
        else:
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = _any

    def log_message(self, *a):
        pass


def _serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Rec)
    srv.seen = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    return srv


@pytest.fixture
def servers(monkeypatch):
    for v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(v, raising=False)
    a, b = _serve(), _serve()
    # deux hôtes distincts pour la garde : 127.0.0.1 et localhost (même machine)
    b.url = f"http://localhost:{b.server_address[1]}"
    yield a, b
    for s in (a, b):
        s.shutdown()
        s.server_close()


LOOP = ufg.parse_allowlist({"iso_fetch": {"allow_private": ["127.0.0.0/8"]}})


def _hook(url, **kw):
    return dict({"name": "dns", "at": "end", "method": "POST", "url": url, "headers": {},
                 "secret_headers": {"Authorization": SECRET}, "body": "{}", "timeout_s": 3}, **kw)


def _resolver(addr):
    return lambda host, port, *a: [(2, 1, 6, "", (addr, port))]


# ---------------------------------------------------------------------------
# C1 : actions annexes par la garde
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,resolver,allow,reason", [
    ("http://127.0.0.1:8200/v1/sys/health", None, (), "loopback"),
    ("http://169.254.169.254/latest/meta-data/", None, (), "metadata"),
    # métadonnées refusées même dans un réseau autorisé
    ("http://169.254.169.254/latest/meta-data/", None,
     ufg.parse_allowlist({"iso_fetch": {"allow_private": ["169.254.0.0/16"]}}), "metadata"),
    ("http://10.1.2.3/api", None, (), "private"),
    ("http://[::ffff:127.0.0.1]/", None, (), "loopback"),
    ("http://[fe80::1]/", None, (), "link-local"),
    ("http://internal.example/", _resolver("172.16.0.5"), (), "private"),
    ("http://10.1.2.3/api", None, LOOP, "private"),            # autorisation d'un autre réseau
])
def test_hook_to_an_internal_address_is_refused_before_any_connection(url, resolver, allow, reason):
    made = []

    def connector(*a):
        made.append(a)
        raise AssertionError("no connection may be opened")
    res = rr.call_hook(_hook(url), allow=allow, resolver=resolver, connector=connector)
    assert res["ok"] is False and res["status"] is None and made == []
    assert reason in res["error"]
    if reason not in ufg.ALWAYS_REFUSED:
        assert "replication.hooks.allow_private" in res["error"] and "iso_fetch" not in res["error"]


def test_hook_on_an_allowed_network_is_called_with_its_secret(servers):
    a, _b = servers
    res = rr.call_hook(_hook(a.url + "/ok"), allow=LOOP)
    assert res["ok"] and res["status"] == 200
    assert a.seen[0]["headers"]["authorization"] == SECRET


@pytest.mark.parametrize("target", ["other-host", "private", "same-host"])
def test_a_redirect_is_never_followed_and_the_secret_never_leaves(servers, target):
    a, b = servers
    a.redirect_to = {"other-host": b.url + "/steal", "private": "http://10.9.9.9/x",
                     "same-host": a.url + "/elsewhere"}[target]
    res = rr.call_hook(_hook(a.url + "/hook"), allow=LOOP)
    assert res["ok"] is False and "redirect" in res["error"] and res["status"] is None
    assert len(a.seen) == 1 and b.seen == []


def test_hooks_read_the_allowlist_from_config_yaml(servers, monkeypatch, tmp_path):
    a, _b = servers
    cfg = tmp_path / "config.yaml"
    monkeypatch.setenv("HARVESTER_OPS_CONFIG", str(cfg))
    cfg.write_text("replication:\n  notifications:\n    allow_private: [127.0.0.0/8]\n")
    assert rr.call_hook(_hook(a.url))["ok"] is False                     # pas pour les actions annexes
    cfg.write_text("replication:\n  hooks:\n    allow_private: [127.0.0.0/8, not-a-cidr]\n")
    assert rr.call_hook(_hook(a.url))["ok"] is True
    monkeypatch.setenv("HARVESTER_OPS_CONFIG", str(tmp_path / "absent.yaml"))
    assert rh.allow_for("hooks") == []


@pytest.mark.parametrize("url,allow,resolver,code", [
    ("http://127.0.0.1/x", (), None, "hook-address"),
    ("http://metadata.example/", (), _resolver("169.254.169.254"), "hook-address"),
    ("http://127.0.0.1/x", LOOP, None, None),
    ("https://not-resolvable.invalid/x", (), None, None),          # revérifié à l'appel
    ("https://hooks.example/x", (), _resolver("93.184.216.34"), None),
])
def test_hook_addresses_are_checked_when_saved(url, allow, resolver, code):
    hooks = [_hook(url)]
    if code is None:
        rp.hook_address_refusal(hooks, allow, resolver=resolver)
        return
    with pytest.raises(rp.PlanError) as e:
        rp.hook_address_refusal(hooks, allow, resolver=resolver)
    assert e.value.code == code


# ---------------------------------------------------------------------------
# I1 / m1 : secret masqué gardé seulement pour la même requête (adresse
# exacte et méthode)
# ---------------------------------------------------------------------------

STORED = _hook("https://dns.example:8443/api/switch?key=" + QTOKEN)


@pytest.mark.parametrize("url,method,ok", [
    (STORED["url"], "POST", True),                                         # inchangée
    ("https://DNS.example:8443/api/switch?key=" + QTOKEN, "POST", True),   # casse de l'hôte
    ("https://dns.example:443/api/switch?key=" + QTOKEN, "POST", False),   # port explicite différent
    ("https://dns.example:8443/api/v2?key=" + QTOKEN, "POST", False),      # autre chemin, même hôte
    ("https://dns.example:8443/api/switch/?key=" + QTOKEN, "POST", False),  # barre finale
    ("https://dns.example:8443/api/switch", "POST", False),                # requête retirée
    ("https://dns.example:8443/api/switch?key=other", "POST", False),      # autre requête
    (STORED["url"], "DELETE", False),                                      # autre méthode
    ("https://attacker.example:8443/api/switch?key=" + QTOKEN, "POST", False),  # autre hôte
    ("https://dns.example:9443/api/switch?key=" + QTOKEN, "POST", False),       # autre port
    ("http://dns.example:8443/api/switch?key=" + QTOKEN, "POST", False),        # autre schéma
])
def test_a_masked_secret_follows_the_hook_only_to_the_same_request(url, method, ok):
    new = [dict(STORED, url=url, method=method, secret_headers={"Authorization": rp.MASK})]
    if ok:
        assert rp.keep_hook_secrets(new, [STORED])[0]["secret_headers"] == {"Authorization": SECRET}
    else:
        with pytest.raises(rp.PlanError) as e:
            rp.keep_hook_secrets(new, [STORED])
        assert e.value.code == "secret-missing" and "changed" in str(e.value)


def test_a_new_secret_value_may_go_with_a_new_address():
    new = [dict(STORED, url="https://new.example/x", secret_headers={"Authorization": "Bearer fresh"})]
    assert rp.keep_hook_secrets(new, [STORED])[0]["secret_headers"] == {"Authorization": "Bearer fresh"}


def test_the_masked_url_sent_back_keeps_the_stored_url_and_an_edited_one_is_refused():
    shown = rp.mask_options({"hooks": [STORED]})["hooks"][0]
    assert QTOKEN not in json.dumps(shown) and shown["url"] == "https://dns.example:8443/api/********?********"
    kept = rp.keep_hook_secrets([shown], [STORED])[0]
    assert kept["url"] == STORED["url"] and kept["secret_headers"] == {"Authorization": SECRET}
    edited = dict(shown, url="https://dns.example:8443/api/v2/********?********")
    got = rp.keep_hook_secrets([dict(edited, secret_headers={})], [STORED])
    with pytest.raises(rp.PlanError) as e:
        rp._hooks(got, 0)
    assert "type the full url again" in str(e.value)


def _matrix(**kw):
    return dict({"id": "m", "kind": "matrix", "homeserver": "https://matrix.example", "room": "!r:example",
                 "token": TOKEN}, **kw)


def _webhook(**kw):
    return dict({"id": "w", "kind": "webhook", "url": f"https://hook.example/in?t={QTOKEN}",
                 "secret_headers": {"Authorization": SECRET}}, **kw)


def _mail(**kw):
    return dict({"id": "e", "kind": "email", "smtp_host": "smtp.example", "smtp_port": 587, "starttls": True,
                 "username": "ops", "password": "pw-s3cr3t", "from": "a@example", "to": ["b@example"]}, **kw)


@pytest.mark.parametrize("stored,sent,code", [
    (_matrix(), _matrix(token=rn.MASK), None),
    (_matrix(), _matrix(homeserver="https://attacker.example", token=rn.MASK), "secret-missing"),
    (_matrix(), _matrix(homeserver="https://attacker.example", token="new-token"), None),
    (_webhook(), _webhook(url="https://hook.example/********?********", secret_headers={"Authorization": rn.MASK}),
     None),
    (_webhook(), _webhook(url="https://hook.example/other", secret_headers={"Authorization": rn.MASK}),
     "secret-missing"),
    (_webhook(), _webhook(url="https://hook.example/other", secret_headers={"Authorization": "fresh"}), None),
    (_webhook(), _webhook(url="https://evil.example/in", secret_headers={"Authorization": rn.MASK}),
     "secret-missing"),
    (_webhook(), _webhook(url="https://hook.example/x?********"), "bad-notify"),
    (_mail(), _mail(password=rn.MASK), None),
    (_mail(), _mail(smtp_host="mx.attacker.example", password=rn.MASK), "secret-missing"),
    (_mail(), _mail(smtp_port=2525, password=rn.MASK), "secret-missing"),
])
def test_notification_secrets_are_kept_only_for_the_same_server(stored, sent, code):
    old = rn.normalize({"channels": [stored]})
    if code is None:
        got = rn.normalize({"channels": [sent]}, old)["channels"][0]
        for f in ("token", "password"):
            if f in stored and sent.get(f) == rn.MASK:
                assert got[f] == stored[f]
        if stored["kind"] == "webhook":
            masked = rn.MASK in sent["url"]
            assert got["secret_headers"] == {"Authorization": SECRET if masked else "fresh"}
            assert got["url"] == (stored["url"] if masked else sent["url"])
        return
    with pytest.raises(rn.NotifyError) as e:
        rn.normalize({"channels": [sent]}, old)
    assert e.value.code == code


# ---------------------------------------------------------------------------
# I2 : notifications par la garde, SMTP chiffré
# ---------------------------------------------------------------------------

def test_a_notification_to_an_internal_address_is_refused_without_the_allowlist(servers):
    a, _b = servers
    res = rn.deliver(_matrix(homeserver=a.url), "x", sleep=lambda s: None, sender=lambda c, t, i, timeout:
                     rn.send(c, t, i, timeout, allow=()))
    assert res["ok"] is False and "replication.notifications.allow_private" in res["error"] and a.seen == []
    assert TOKEN not in res["error"]


def test_a_notification_redirect_never_carries_the_matrix_token(servers):
    a, b = servers
    a.redirect_to = b.url + "/steal"
    res = rn.deliver(_matrix(homeserver=a.url), "x", sleep=lambda s: None, sender=lambda c, t, i, timeout:
                     rn.send(c, t, i, timeout, allow=LOOP))
    assert res["ok"] is False and "redirect" in res["error"] and b.seen == []
    assert all(s["headers"].get("authorization") == f"Bearer {TOKEN}" for s in a.seen)
    # refus de la garde : pas d'autre essai (il donnerait le même refus)
    assert res["tries"] == 1 and len(a.seen) == 1


@pytest.mark.parametrize("chan,code", [
    (_mail(starttls=False), "bad-notify"),                          # mot de passe en clair
    (_mail(starttls=True, implicit_tls=True), "bad-notify"),        # l'un ou l'autre
    (_mail(implicit_tls="yes"), "bad-notify"),
    (_mail(starttls=False, password=""), None),                     # sans mot de passe : permis
    (_mail(starttls=False, implicit_tls=True, smtp_port=465), None),
])
def test_smtp_password_needs_tls(chan, code):
    if code is None:
        rn.normalize({"channels": [chan]})
        return
    with pytest.raises(rn.NotifyError) as e:
        rn.normalize({"channels": [chan]})
    assert e.value.code == code


def test_a_stored_plaintext_password_is_never_sent():
    legacy = dict(rn.normalize({"channels": [_mail()]})["channels"][0], starttls=False)
    opened = []
    res = rn.deliver(legacy, "x", sleep=lambda s: None, sender=lambda c, t, i, timeout:
                     rn.send(c, t, i, timeout, smtp=lambda *a, **k: opened.append(a)))
    assert res["ok"] is False and "not sent in clear" in res["error"]
    assert opened == [] and "pw-s3cr3t" not in res["error"]


def test_smtp_server_goes_through_the_guard():
    ch = rn.normalize({"channels": [_mail(smtp_host="127.0.0.1")]})["channels"][0]
    res = rn.deliver(ch, "x", sleep=lambda s: None, sender=lambda c, t, i, timeout:
                     rn.send(c, t, i, timeout, allow=()))
    assert res["ok"] is False and "loopback" in res["error"] and "pw-s3cr3t" not in res["error"]


@pytest.mark.parametrize("chan,allow,code", [
    (_matrix(homeserver="http://127.0.0.1:8008"), (), "notify-address"),
    (_webhook(url="http://169.254.169.254/x"), LOOP, "notify-address"),
    (_mail(smtp_host="10.0.0.25"), (), "notify-address"),
    (_matrix(homeserver="http://127.0.0.1:8008"), LOOP, None),
    (_webhook(url="https://unresolvable.invalid/x"), (), None),
])
def test_notification_addresses_are_checked_when_saved(chan, allow, code):
    cfg = rn.normalize({"channels": [chan]})
    if code is None:
        rn.address_refusal(cfg, allow)
        return
    with pytest.raises(rn.NotifyError) as e:
        rn.address_refusal(cfg, allow)
    assert e.value.code == code


# ---------------------------------------------------------------------------
# I3 / m6 : adresses montrées sans requête ni jeton de chemin
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,shown", [
    ("https://h.example/a/b?token=x&y=2", "https://h.example/a/********?********"),
    ("https://h.example:8443/a", "https://h.example:8443/********"),
    ("http://h.example", "http://h.example"),
    ("http://h.example/", "http://h.example/"),
    ("https://h.example/hooks/", "https://h.example/hooks/********"),
    ("https://h.example/a%2Fb/c%20d", "https://h.example/a%2Fb/********"),
    ("https://user:pw@h.example/a?x=1#frag", "https://h.example/********?********"),
    ("http://[2001:db8::1]:8080/p?q=1", "http://[2001:db8::1]:8080/********?********"),
    # forme Slack, Discord, Mattermost, Teams : le jeton ne se montre jamais
    ("https://hooks.slack.com/services/T0/B0/XXX", "https://hooks.slack.com/services/********"),
    ("https://discord.com/api/webhooks/1/tok", "https://discord.com/api/********"),
    ("https://mm.corp/hooks/abc", "https://mm.corp/hooks/********"),
    ("https://x.webhook.office.com/webhookb2/g-u-i-d/IncomingWebhook/a/b",
     "https://x.webhook.office.com/webhookb2/********"),
    ("https://h/notify", "https://h/********"),
    ("not a url", rh.MASK),
    ("", ""),
])
def test_mask_url(url, shown):
    assert rh.mask_url(url) == shown


def test_a_masked_path_round_trips_and_an_edited_one_is_refused():
    stored = dict(STORED, url="https://hooks.slack.com/services/T0/B0/XXX")
    shown = rp.mask_options({"hooks": [stored]})["hooks"][0]
    assert "T0" not in json.dumps(shown) and "XXX" not in json.dumps(shown)
    kept = rp.keep_hook_secrets([shown], [stored])[0]
    assert kept["url"] == stored["url"] and kept["secret_headers"] == {"Authorization": SECRET}
    edited = dict(shown, url="https://hooks.slack.com/services/T1/********", secret_headers={})
    with pytest.raises(rp.PlanError) as e:
        rp._hooks(rp.keep_hook_secrets([edited], [stored]), 0)
    assert "type the full url again" in str(e.value)


def test_hooks_summary_and_notify_mask_hide_the_query():
    opts = {"hooks": [STORED]}
    assert QTOKEN not in json.dumps(rp.hooks_summary(opts)) and QTOKEN not in json.dumps(rp.mask_options(opts))
    shown = rn.mask(rn.normalize({"channels": [_webhook()]}))
    assert QTOKEN not in json.dumps(shown) and shown["channels"][0]["url"] == "https://hook.example/********?********"


# ---------------------------------------------------------------------------
# Routes et ligne de commande : refus tout de suite, rien en clair
# ---------------------------------------------------------------------------

from test_replication_routes_199 import PID, _NoThread, as_user, client, db, env, wapp  # noqa: E402,F401

CREATE = {"name": "erp", "from": "harvlab", "to": "harvlab2", "vms": ["default/web-1"], "rpo": "15m"}


def test_create_and_settings_refuse_an_internal_hook_address_at_once(client, env):
    bad = _hook("http://127.0.0.1:8200/v1/sys/health")
    r = client.post("/api/replication/plans", json=dict(CREATE, recovery={"hooks": [bad]}))
    assert r.status_code == 400 and r.get_json()["code"] == "hook-address", r.get_json()
    r = client.patch(f"/api/replication/plans/{PID}/settings", json={"recovery": {"hooks": [bad]}})
    assert r.status_code == 400 and r.get_json()["code"] == "hook-address", r.get_json()
    assert _NoThread.started == []


def test_settings_refuse_a_masked_secret_sent_to_another_host(client, env):
    conn = db.connect(env["db"])
    db.update_plan(conn, PID, options={"hooks": [STORED]})
    conn.close()
    shown = client.get(f"/api/replication/plans/{PID}").get_json()["plan"]["options"]["hooks"][0]
    shown["url"] = "https://attacker.example/x"
    r = client.patch(f"/api/replication/plans/{PID}/settings", json={"recovery": {"hooks": [shown]}})
    assert r.status_code == 400 and r.get_json()["code"] == "secret-missing" and _NoThread.started == []


def test_a_viewer_never_reads_the_query_of_a_hook_url(as_user, env):
    conn = db.connect(env["db"])
    db.update_plan(conn, PID, options={"hooks": [STORED]})
    conn.close()
    for path in (f"/api/replication/plans/{PID}", "/api/replication/plans"):
        text = as_user("curieux", "GET", path).get_data(as_text=True)
        assert QTOKEN not in text and SECRET not in text
