"""2.9.0 tâche 2 : client OpenBao (bin/lib/openbao_client.py), en HTTP réel
contre un faux serveur (tests/api/fake_openbao.py) qui rend les réponses
relevées sur le banc (codes et messages exacts, essais préalables).

Un cas par chemin d'erreur. Mutations jouées (rapport des tâches 2 à 4) :
« réessai sur 403 », « en-tête de jeton consigné dans l'exception »,
« lookup sauté avant unwrap », « environnement lu ».
"""

import json
import pickle
import shutil
import socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import openbao_client as oc   # noqa: E402
from fake_openbao import (FakeOpenBao, MSG_CAS, MSG_DENIED, MSG_INTERNAL, MSG_SEALED,  # noqa: E402
                          MSG_WRAP, new_token)

ROLE_PATH = "auth/approle/role/harvester-ops-abc123def456/secret-id"
MOUNT = "harvester-ops"


class Sleeps:
    def __init__(self):
        self.calls = []

    def __call__(self, s):
        self.calls.append(s)


@pytest.fixture
def srv():
    s = FakeOpenBao().start()
    yield s
    s.stop()


def client(srv, **kw):
    kw.setdefault("sleep", Sleeps())
    return oc.Client(srv.addr, timeout=kw.pop("timeout", 3), **kw)


def logged(srv, **kw):
    c = client(srv, **kw)
    rid, sid = srv.add_role()
    c.login_approle(rid, sid)
    return c


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------------------
# Codes d'erreur : une ligne par chemin
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("setup,call,code,status", [
    (lambda s: setattr(s, "sealed", True), lambda c: c.kv_get(MOUNT, "kubeconfig/a"), "openbao-sealed", 503),
    (lambda s: s.denied.append(f"{MOUNT}/data/kubeconfig"), lambda c: c.kv_get(MOUNT, "kubeconfig/a"),
     "openbao-denied", 403),
    (lambda s: s.put_raw(MOUNT, "kubeconfig/a", {"kubeconfig": "x"}),
     lambda c: c.kv_put(MOUNT, "kubeconfig/a", {"kubeconfig": "x"}, cas=0), "openbao-cas-conflict", 400),
    (lambda s: s.mounts.update({"old": 1}), lambda c: c.kv_get("old", "a"), "openbao-not-kv2", None),
    (lambda s: None, lambda c: c.kv_get("absent", "a"), "openbao-not-kv2", None),
    (lambda s: None, lambda c: c.kv_get(MOUNT, "kubeconfig/none"), "openbao-missing", 404),
    (lambda s: setattr(s, "audit_full", True), lambda c: c.kv_get(MOUNT, "kubeconfig/a"), "openbao-internal", 500),
    (lambda s: s.fail_next.extend([(502, {"errors": ["bad gateway"]})] * 4), lambda c: c.health(),
     "openbao-unreachable", 502),
    (lambda s: s.fail_next.append((400, {"errors": ["missing client token"]})),
     lambda c: c.capabilities(["x"]), "openbao-bad-request", 400),
])
def test_error_codes(srv, setup, call, code, status):
    c = logged(srv)
    setup(srv)
    with pytest.raises(oc.OpenBaoError) as e:
        call(c)
    assert e.value.code == code
    assert e.value.http_status == status


def test_unreachable_after_bounded_retries():
    sl = Sleeps()
    c = oc.Client(f"http://127.0.0.1:{free_port()}", timeout=1, sleep=sl)
    t0 = time.monotonic()
    with pytest.raises(oc.OpenBaoError) as e:
        c.health()
    assert e.value.code == "openbao-unreachable"
    assert sl.calls == [0.5, 1, 2]                 # trois réessais espacés
    assert time.monotonic() - t0 < 5


def test_timeout_is_unreachable(srv):
    srv.hang = 1.5
    sl = Sleeps()
    c = oc.Client(srv.addr, timeout=0.3, sleep=sl, retry_delays=(0.01,))
    t0 = time.monotonic()
    with pytest.raises(oc.OpenBaoError) as e:
        c.seal_status()
    assert e.value.code == "openbao-unreachable"
    assert len(srv.requests("sys/seal-status")) == 2
    assert time.monotonic() - t0 < 2


@pytest.mark.parametrize("status,body,attempts,ok", [
    (500, {"errors": [MSG_INTERNAL]}, 2, True),     # 5xx puis 200 : réussite au deuxième essai
    (503, {"errors": ["unavailable"]}, 2, True),
    (403, {"errors": [MSG_DENIED]}, 1, False),       # 4xx : aucun réessai
    (400, {"errors": ["bad"]}, 1, False),
    (404, {"errors": []}, 1, False),
    (503, {"errors": [MSG_SEALED]}, 1, False),       # scellé : réessayer ne sert à rien
])
def test_retry_policy(srv, status, body, attempts, ok):
    c = client(srv)
    srv.fail_next.append((status, body))
    if ok:
        c.seal_status()
    else:
        with pytest.raises(oc.OpenBaoError):
            c.seal_status()
    assert len(srv.requests("sys/seal-status")) == attempts


def test_internal_after_repeated_500(srv):
    c = client(srv)
    srv.fail_next.extend([(500, {"errors": [MSG_INTERNAL]})] * 4)
    with pytest.raises(oc.OpenBaoError) as e:
        c.seal_status()
    assert e.value.code == "openbao-internal"
    assert len(srv.requests("sys/seal-status")) == 4


def test_denied_reports_capabilities_without_body(srv):
    c = logged(srv)
    srv.denied.append(f"{MOUNT}/data/kubeconfig")
    with pytest.raises(oc.OpenBaoError) as e:
        c.kv_get(MOUNT, "kubeconfig/a")
    assert f"{MOUNT}/data/kubeconfig/a" in e.value.detail
    assert "capabilities: deny" in e.value.detail
    assert len(srv.requests("data/kubeconfig/a")) == 1        # pas de réessai


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl absent")
    import registry_seed as rs
    work = tmp_path_factory.mktemp("pki")
    good = rs.make_ca(work, "127.0.0.1", prefix="good")
    srvcert = rs.sign_server(work, "127.0.0.1", ca_prefix="good")
    (work / "srv.crt").write_text(srvcert["crt"])
    (work / "srv.key").write_text(srvcert["key"])
    other = rs.make_ca(work, "127.0.0.1", prefix="other")
    return {"good": good["ca"], "other": other["ca"], "cert": str(work / "srv.crt"), "key": str(work / "srv.key")}


def test_tls_good_ca(pki):
    s = FakeOpenBao(tls=(pki["cert"], pki["key"])).start()
    try:
        assert oc.Client(s.addr, ca_pem=pki["good"], timeout=3).seal_status()["sealed"] is False
    finally:
        s.stop()


def test_tls_other_ca_no_retry(pki):
    s = FakeOpenBao(tls=(pki["cert"], pki["key"])).start()
    sl = Sleeps()
    try:
        with pytest.raises(oc.OpenBaoError) as e:
            oc.Client(s.addr, ca_pem=pki["other"], timeout=3, sleep=sl).seal_status()
        assert e.value.code == "openbao-tls"
        assert sl.calls == []
    finally:
        s.stop()


# ---------------------------------------------------------------------------
# Enveloppes
# ---------------------------------------------------------------------------
def test_unwrap_ok_then_used(srv):
    c = client(srv)
    w = srv.wrap({"secret_id": "sid-1", "secret_id_accessor": "acc"}, ROLE_PATH)
    assert c.unwrap(w, ROLE_PATH)["secret_id"] == "sid-1"
    with pytest.raises(oc.OpenBaoError) as e:
        c.unwrap(w, ROLE_PATH)
    assert e.value.code == "openbao-unwrap-used"
    assert MSG_WRAP in e.value.detail


def test_unwrap_expired(srv):
    c = client(srv)
    w = srv.wrap({"secret_id": "sid-1"}, ROLE_PATH, ttl=-1)
    with pytest.raises(oc.OpenBaoError) as e:
        c.unwrap(w, ROLE_PATH)
    assert e.value.code == "openbao-unwrap-used"


def test_unwrap_wrong_creation_path_never_unwraps(srv):
    c = client(srv)
    w = srv.wrap({"secret_id": "sid-1"}, "auth/approle/role/other/secret-id")
    with pytest.raises(oc.OpenBaoError) as e:
        c.unwrap(w, ROLE_PATH)
    assert e.value.code == "openbao-unwrap-used"
    assert srv.requests("sys/wrapping/unwrap") == []
    assert w in srv.wraps                           # toujours déballable par son vrai destinataire


# ---------------------------------------------------------------------------
# Environnement ignoré
# ---------------------------------------------------------------------------
def test_environment_ignored(srv, monkeypatch):
    other = FakeOpenBao().start()
    try:
        env_tok = new_token()
        other.tokens[env_tok] = {"ttl": 3600, "policies": [], "created": time.time()}
        srv.tokens[env_tok] = {"ttl": 3600, "policies": [], "created": time.time()}
        for k in ("VAULT_ADDR", "BAO_ADDR"):
            monkeypatch.setenv(k, other.addr)
        for k in ("VAULT_TOKEN", "BAO_TOKEN"):
            monkeypatch.setenv(k, env_tok)
        monkeypatch.setenv("VAULT_NAMESPACE", "ns1")
        monkeypatch.setenv("HTTP_PROXY", other.addr)
        monkeypatch.setenv("http_proxy", other.addr)
        c = client(srv)
        with pytest.raises(oc.OpenBaoError) as e:
            c.kv_get(MOUNT, "kubeconfig/a")           # aucun jeton : pas connecté
        assert e.value.code == "openbao-login-failed"
        c.seal_status()
        assert other.log == []
        assert all(r["headers"].get("x-vault-token") != env_tok for r in srv.log)
        assert all("x-vault-namespace" not in r["headers"] for r in srv.log)
    finally:
        other.stop()


# ---------------------------------------------------------------------------
# Aucune valeur dans les messages et représentations
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("leak", [
    "hvs." + "A" * 92,
    "s." + "b" * 24,
    "hvb." + "c" * 30,
    "r." + "d" * 22,
])
def test_token_scrubbed_from_error(srv, leak):
    c = logged(srv)
    srv.fail_next.append((400, {"errors": [f"bad token {leak} given"]}))
    with pytest.raises(oc.OpenBaoError) as e:
        c.capabilities(["x"])
    text = str(e.value) + repr(e.value) + e.value.detail + pickle.dumps(e.value).decode("latin-1")
    assert leak not in text
    assert "[token]" in e.value.detail


def test_error_detail_truncated(srv):
    c = client(srv)
    srv.fail_next.append((400, {"errors": ["x" * 2000]}))
    with pytest.raises(oc.OpenBaoError) as e:
        c.seal_status()
    assert len(e.value.detail) <= oc.DETAIL_MAX


def test_client_token_never_in_exception(srv):
    c = logged(srv)
    tok = c._tok.value
    srv.denied.append(f"{MOUNT}/data")
    srv.fail_next.append((400, {"errors": ["oops"]}))
    for call in (lambda: c.capabilities(["x"]), lambda: c.kv_get(MOUNT, "kubeconfig/a")):
        with pytest.raises(oc.OpenBaoError) as e:
            call()
        assert tok not in str(e.value) + repr(e.value) + repr(e.value.args)


@pytest.mark.parametrize("dump", [
    repr, str,
    lambda c: pickle.dumps(c).decode("latin-1"),
    lambda c: json.dumps(c, default=lambda o: o.__getstate__()),
    lambda c: repr(c.with_headers("a1", "alice")),
    lambda c: repr(vars(c)),
])
def test_no_token_in_representations(srv, dump):
    c = logged(srv)
    tok, sid = c._tok.value, c._tok.secret_id
    text = dump(c)
    assert tok not in text and sid not in text


def test_pickle_round_trip_has_no_token(srv):
    c2 = pickle.loads(pickle.dumps(logged(srv)))
    assert c2.logged_in is False


# ---------------------------------------------------------------------------
# En-têtes de corrélation
# ---------------------------------------------------------------------------
def test_correlation_headers_on_every_request(srv):
    c = logged(srv, actor="alice", action="act-123")
    c.kv_put(MOUNT, "kubeconfig/a", {"kubeconfig": "x"}, custom_metadata={"hops_class": "kubeconfig"})
    c.kv_get(MOUNT, "kubeconfig/a")
    c.seal_status()
    c.health()
    c2 = c.with_headers("act-9", "bob")
    c2.kv_list(MOUNT, "")
    for r in srv.log:
        assert "x-hops-action" in r["headers"] and "x-hops-actor" in r["headers"]
    assert {r["headers"]["x-hops-actor"] for r in srv.log} == {"alice", "bob"}
    assert srv.log[-1]["headers"]["x-hops-action"] == "act-9"
    assert c.action == "act-123"                       # la copie ne change pas l'original


@pytest.mark.parametrize("actor,expected", [
    (None, "service"), ("", "service"), ("élodie", "_lodie"), ("a\r\nX-Evil: 1", "a__X-Evil:_1"),
    ("x" * 300, "x" * 128),
])
def test_header_values_sanitized(srv, actor, expected):
    client(srv, actor=actor).seal_status()
    assert srv.log[-1]["headers"]["x-hops-actor"] == expected
    assert "x-evil" not in srv.log[-1]["headers"]


def test_default_action_header(srv):
    client(srv).seal_status()
    assert srv.log[-1]["headers"]["x-hops-action"] == "bg-console"


# ---------------------------------------------------------------------------
# Jeton : renouvellement aux deux tiers, reconnexion sans boucle
# ---------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_renew_at_two_thirds(srv):
    clk = Clock()
    c = logged(srv, clock=clk)
    clk.t += 3600 * 0.5
    c.kv_list(MOUNT, "")
    assert srv.requests("renew-self") == []
    clk.t += 3600 * 0.2                                # 70 % : renouvellement
    c.kv_list(MOUNT, "")
    assert len(srv.requests("renew-self")) == 1
    assert len(srv.requests("approle/login")) == 1


def test_renew_failure_relogin_once(srv):
    clk = Clock()
    c = logged(srv, clock=clk)
    srv.renew_fails = True
    clk.t += 3000
    c.kv_list(MOUNT, "")
    assert len(srv.requests("renew-self")) == 1
    assert len(srv.requests("approle/login")) == 2      # une connexion de plus


def test_renew_and_relogin_failure_no_loop(srv):
    clk = Clock()
    c = logged(srv, clock=clk)
    srv.renew_fails = True
    srv.roles.clear()                                  # secret_id retiré : connexion refusée
    clk.t += 3000
    with pytest.raises(oc.OpenBaoError) as e:
        c.kv_list(MOUNT, "")
    assert e.value.code == "openbao-login-failed"
    assert len(srv.requests("renew-self")) == 1
    assert len(srv.requests("approle/login")) == 2


@pytest.mark.parametrize("secret_id,code", [("wrong", "openbao-login-failed"), (None, "openbao-login-failed")])
def test_login_refused(srv, secret_id, code):
    rid, _ = srv.add_role()
    c = client(srv)
    with pytest.raises(oc.OpenBaoError) as e:
        c.login_approle(rid if secret_id else "nobody", secret_id or "x")
    assert e.value.code == code
    assert len(srv.requests("approle/login")) == 1


def test_login_cidr_message(srv):
    msg = 'source address "10.89.17.7" unauthorized by CIDR restrictions on the role: %!w(<nil>)'
    srv.fail_next.append((400, {"errors": [msg]}))
    with pytest.raises(oc.OpenBaoError) as e:
        client(srv).login_approle("r", "s")
    assert e.value.code == "openbao-login-failed"
    assert "CIDR" in e.value.detail


def test_revoke_and_lookup(srv):
    c = logged(srv)
    assert c.token_lookup_self()["ttl"] == 3600
    c.renew_self()
    c.revoke_self()
    assert c.logged_in is False
    assert srv.tokens == {}


# ---------------------------------------------------------------------------
# KV v2, capacités, transit, jetons enfants, instantanés
# ---------------------------------------------------------------------------
def test_kv_round_trip_and_metadata(srv):
    c = logged(srv)
    v = c.kv_put(MOUNT, "consoles/abc/kubeconfig/h1", {"kubeconfig": "K"}, cas=0,
                 custom_metadata={"hops_action": "a1", "hops_actor": "alice", "hops_class": "kubeconfig"})
    assert v == 1 and v.metadata_error is None
    data, meta = c.kv_get(MOUNT, "consoles/abc/kubeconfig/h1")
    assert data == {"kubeconfig": "K"} and meta["version"] == 1
    assert c.kv_metadata(MOUNT, "consoles/abc/kubeconfig/h1")["custom_metadata"]["hops_actor"] == "alice"
    assert c.kv_put(MOUNT, "consoles/abc/kubeconfig/h1", {"kubeconfig": "K2"}, cas=1) == 2
    assert c.kv_get(MOUNT, "consoles/abc/kubeconfig/h1", version=1)[0] == {"kubeconfig": "K"}
    assert c.kv_list(MOUNT, "consoles/abc") == ["kubeconfig/"]
    assert c.kv_list(MOUNT, "consoles/none") == []
    c.kv_delete_all(MOUNT, "consoles/abc/kubeconfig/h1")
    with pytest.raises(oc.OpenBaoError):
        c.kv_get(MOUNT, "consoles/abc/kubeconfig/h1")
    c.kv_delete_all(MOUNT, "consoles/abc/kubeconfig/h1")      # déjà absent : sans erreur


def test_kv_metadata_failure_keeps_value(srv):
    c = logged(srv)
    srv.denied.append(f"{MOUNT}/metadata/")
    v = c.kv_put(MOUNT, "p/a", {"f": "v"}, custom_metadata={"hops_class": "x"})
    assert v == 1 and v.metadata_error == "openbao-denied"
    assert c.kv_get(MOUNT, "p/a")[0] == {"f": "v"}


def test_kv_mount_version_fallback_to_config(srv):
    c = logged(srv)
    srv.denied.append("sys/internal/ui/mounts/")
    assert c.kv_mount_version(MOUNT) == 2


@pytest.mark.parametrize("path", ["", "../x", "a//b", "/", "a/./b"])
def test_kv_bad_path(srv, path):
    with pytest.raises(ValueError):
        logged(srv).kv_get(MOUNT, path)


def test_capabilities(srv):
    c = logged(srv)
    srv.denied.append("sys/policies")
    assert c.capabilities(["sys/policies/acl/x"]) == {"sys/policies/acl/x": ["deny"]}
    caps = c.capabilities([f"{MOUNT}/data/a", "sys/policies/acl/y"])
    assert "read" in caps[f"{MOUNT}/data/a"] and caps["sys/policies/acl/y"] == ["deny"]


def test_transit(srv):
    c = logged(srv)
    c.transit_create("hops-abc-tf-d1")
    assert c.transit_roundtrip("hops-abc-tf-d1") is True
    c.transit_rotate("hops-abc-tf-d1")
    assert srv.transit["hops-abc-tf-d1"] == 2


def test_token_create_child(srv):
    c = logged(srv)
    t = c.token_create_child("hops-tf-abc", ["hops-tf-abc"], "10m", entity_alias="tf-d1")
    assert t.startswith("s.") and t in srv.tokens
    body = json.loads(srv.requests("auth/token/create/hops-tf-abc")[-1]["raw"])
    assert body["entity_alias"] == "tf-d1"
    with pytest.raises(oc.OpenBaoError) as e:
        c.token_create_child("hops-tf-abc", ["hops-tf-abc"], "10m", entity_alias="other")
    assert e.value.code == "openbao-bad-request" and "entity_alias" in e.value.detail


def test_raft_snapshot_and_restore(srv, tmp_path):
    c = logged(srv)
    dst = tmp_path / "openbao-20261108.snap"
    assert c.raft_snapshot(dst) == len(srv.snapshot)
    assert dst.read_bytes() == srv.snapshot
    assert (dst.stat().st_mode & 0o777) == 0o600
    c.raft_restore(dst)
    assert srv.restored == [srv.snapshot]
    srv.snapshot_key_ok = False
    with pytest.raises(oc.OpenBaoError) as e:
        c.raft_restore(dst)
    assert "different autoseal key" in e.value.detail
    c.raft_restore(dst, force=True)
    assert srv.requests("snapshot-force")


def test_raft_snapshot_failure_leaves_nothing(srv, tmp_path):
    c = logged(srv)
    srv.denied.append("sys/storage/raft")
    with pytest.raises(oc.OpenBaoError):
        c.raft_snapshot(tmp_path / "s.snap")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value,expected", [(None, 10), ("x", 10), (1, 5), (10, 10), (61, 60), ("30", 30)])
def test_clamp_timeout(value, expected):
    assert oc.clamp_timeout(value) == expected


@pytest.mark.parametrize("addr", ["", "ftp://x", "127.0.0.1:8210", None])
def test_bad_address(addr):
    with pytest.raises(ValueError):
        oc.Client(addr)


def test_health_sealed_is_not_an_error(srv):
    srv.sealed = True
    assert client(srv).health()["sealed"] is True


# ---------------------------------------------------------------------------
# Revue des tâches 2 à 4
# ---------------------------------------------------------------------------
class _Redirector:
    """Serveur qui renvoie 302 vers `location` et note les en-têtes reçus."""

    def __init__(self, location=None):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.seen, self.location = [], location
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                me.seen.append(dict(self.headers))
                if me.location:
                    self.send_response(302)
                    self.send_header("Location", me.location.format(port=self.server.server_address[1]))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    body = b'{"data": {"data": {"k": "v"}, "metadata": {"version": 1}}}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addr = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_redirect_to_another_origin_never_carries_the_token():
    """M2 : 302 vers un autre hôte en http : refusé, le jeton n'y va pas."""
    other = _Redirector()
    front = _Redirector(location=other.addr + "/v1/sys/health")
    try:
        c = oc.Client(front.addr, timeout=2, retry_delays=())
        c.set_token("s." + "T" * 24)
        c._kv2["harvester-ops"] = True
        with pytest.raises(oc.OpenBaoError) as e:
            c.kv_get("harvester-ops", "a")
        assert e.value.code == "openbao-unreachable" and e.value.http_status == 302
        assert "standby" in e.value.detail
        assert other.seen == []
    finally:
        front.close()
        other.close()


def test_redirect_same_origin_is_followed():
    front = _Redirector(location="http://127.0.0.1:{port}/v1/harvester-ops/data/b")
    try:
        c = oc.Client(front.addr, timeout=2, retry_delays=())
        c.set_token("s." + "T" * 24)
        c._kv2["harvester-ops"] = True
        with pytest.raises(oc.OpenBaoError):
            c.kv_get("harvester-ops", "a")          # boucle même origine bornée par urllib
        assert len(front.seen) > 1
    finally:
        front.close()


def test_relogin_once_after_token_revoked_elsewhere(srv):
    """M3 : jeton révoqué côté serveur (ou effacé par une restauration) :
    une reconnexion AppRole, la requête rejouée une fois."""
    c = logged(srv)
    srv.put_raw("harvester-ops", "a", {"k": "v"})
    assert c.kv_get("harvester-ops", "a")[0] == {"k": "v"}
    srv.tokens.clear()
    logins = len(srv.requests("auth/approle/login"))
    assert c.kv_get("harvester-ops", "a")[0] == {"k": "v"}
    assert len(srv.requests("auth/approle/login")) == logins + 1


def test_real_denial_does_not_relogin(srv):
    c = logged(srv)
    srv.denied.append("harvester-ops/data/secret")
    srv.put_raw("harvester-ops", "secret", {"k": "v"})
    logins = len(srv.requests("auth/approle/login"))
    with pytest.raises(oc.OpenBaoError) as e:
        c.kv_get("harvester-ops", "secret")
    assert e.value.code == "openbao-denied"
    assert len(srv.requests("auth/approle/login")) == logins


# ---------------------------------------------------------------------------
# 2.10.0 tâche 2 : codes du jeton par personne, client de service inchangé
# ---------------------------------------------------------------------------
def test_codes_2100_and_service_kind(srv):
    assert {"openbao-person-login-failed", "openbao-person-expired"} <= set(oc.CODES)
    c = logged(srv)
    assert c.kind == "service" and "service" in repr(c)
    assert c.with_headers("a", "b").kind == "service"


def test_service_still_relogs_after_revocation_2100(srv):
    """La reconnexion AppRole de la 2.9.0 (revue M3) reste celle du service."""
    c = logged(srv)
    srv.put_raw(MOUNT, "kubeconfig/a", {"kubeconfig": "x"})
    srv.tokens.pop(c._tok.value)
    assert c.kv_get(MOUNT, "kubeconfig/a")[0] == {"kubeconfig": "x"}
    assert len(srv.requests("auth/approle/login")) == 2
