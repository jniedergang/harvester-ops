"""2.10.0 tâche 2 : connexion `jwt` du client OpenBao (jeton par personne).

En HTTP réel contre le faux serveur (tests/api/fake_openbao.py), étendu d'une
méthode `jwt` qui rend les réponses relevées à la tâche 1 du plan 2.10.0
(messages exacts, marge d'expiration, entité stable, plage d'adresses).

Un cas par chemin d'erreur. Mutations jouées (rapport des tâches 2 à 5) :
« reconnexion AppRole sur un client de personne », « JWT consigné dans
l'exception », « jeton de personne copié dans le client de service ».
"""

import pickle
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import openbao_client as oc   # noqa: E402
from fake_openbao import (FakeOpenBao, MSG_DENIED, MSG_JWT_AUD, MSG_JWT_EXP, MSG_JWT_ISS,  # noqa: E402
                          MSG_JWT_KEY, MSG_JWT_NBF, MSG_JWT_SIG, fake_jwt)

MOUNT_JWT = "jwt-hops-abc123def456-p-rmgr"
ISS = "https://rancher.example/oidc"
AUD = "client-abc"
KV = "harvester-ops"
PREFIX = "consoles/abc123def456"


class Sleeps:
    def __init__(self):
        self.calls = []

    def __call__(self, s):
        self.calls.append(s)


@pytest.fixture
def srv():
    s = FakeOpenBao().start()
    s.add_jwt_mount(MOUNT_JWT, issuer=ISS, audience=AUD,
                    allow=[(f"{KV}/data/{PREFIX}/ssh-kvm/", {"read"}),
                           (f"{KV}/data/{PREFIX}/tf-kvm/", {"read", "create", "update"}),
                           ("sys/capabilities-self", {"update"})])
    yield s
    s.stop()


def claims(**kw):
    c = {"iss": ISS, "aud": [AUD], "sub": "user-kk67j", "exp": int(time.time()) + 600,
         "iat": int(time.time()), "name": "Default Admin", "auth_provider": "local", "nonce": "n1"}
    c.update(kw)
    return {k: v for k, v in c.items() if v is not None}


def service(srv, **kw):
    c = oc.Client(srv.addr, timeout=3, sleep=Sleeps(), **kw)
    rid, sid = srv.add_role()
    c.login_approle(rid, sid)
    return c


def person(srv, svc=None):
    base = svc or oc.Client(srv.addr, timeout=3, sleep=Sleeps())
    return base.for_token(None)


# ---------------------------------------------------------------------------
# Connexion réussie
# ---------------------------------------------------------------------------
def test_login_keeps_token_in_object_only(srv):
    jwt = fake_jwt(claims())
    p = person(srv)
    info = p.login_jwt(MOUNT_JWT, "person", jwt)
    assert info["entity_id"] and info["ttl"] == 900
    assert info["policies"] == ["default", "hops-person-abc123def456"]
    assert set(info) == {"entity_id", "ttl", "policies", "accessor"}
    tok = p._tok.value
    assert tok and tok.startswith("s.")
    # jamais dans repr, pickle, ni la réponse
    for text in (repr(p), str(p), repr(info), pickle.dumps(p).decode("latin-1")):
        assert tok not in text and jwt not in text
    assert p.kind == "person" and p.entity_id == info["entity_id"]
    # la requête de connexion porte le JWT dans le corps, jamais en en-tête
    rec = srv.requests(f"auth/{MOUNT_JWT}/login")[0]
    assert "x-vault-token" not in rec["headers"]
    assert jwt.encode() in rec["raw"]
    # le jeton obtenu lit ce que sa politique permet
    srv.put_raw(KV, f"{PREFIX}/ssh-kvm/k1", {"private_key": "K"})
    assert p.kv_get(KV, f"{PREFIX}/ssh-kvm/k1")[0] == {"private_key": "K"}
    assert srv.log[-1]["headers"]["x-vault-token"] == tok


def test_same_person_same_entity(srv):
    a = person(srv).login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    b = person(srv).login_jwt(MOUNT_JWT, "person", fake_jwt(claims(nonce=None)))
    c = person(srv).login_jwt(MOUNT_JWT, "person", fake_jwt(claims(sub="u-lwpz9")))
    assert a["entity_id"] == b["entity_id"] != c["entity_id"]


def test_lookup_self_names_the_entity(srv):
    p = person(srv)
    info = p.login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    me = p.lookup_self()
    assert me["entity_id"] == info["entity_id"]
    assert me["display_name"] == f"{MOUNT_JWT}-user-kk67j"
    assert me["meta"] == {"name": "Default Admin", "provider": "local", "role": "person"}
    assert me["bound_cidrs"] == ["127.0.0.1"] and me["path"] == f"auth/{MOUNT_JWT}/login"
    assert p._tok.value not in repr(me)


# ---------------------------------------------------------------------------
# Refus de connexion : une ligne par chemin
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("make,status,msg", [
    (lambda: fake_jwt(claims(aud=["other-client"])), 400, MSG_JWT_AUD),
    (lambda: fake_jwt(claims(iss="https://other.example/oidc")), 400, MSG_JWT_ISS),
    (lambda: fake_jwt(claims(exp=int(time.time()) - 140)), 400, MSG_JWT_EXP),
    (lambda: fake_jwt(claims(), kid="rotated"), 400, MSG_JWT_KEY),
    (lambda: fake_jwt(claims(), key=b"forged"), 400, MSG_JWT_SIG),
    (lambda: fake_jwt(claims(iat=None)), 400, MSG_JWT_NBF),
], ids=["audience", "issuer", "expired", "unknown-key", "signature", "no-iat"])
def test_login_refused(srv, make, status, msg):
    jwt = make()
    p = person(srv)
    with pytest.raises(oc.OpenBaoError) as e:
        p.login_jwt(MOUNT_JWT, "person", jwt)
    assert e.value.code == "openbao-person-login-failed" and e.value.http_status == status
    assert msg in e.value.detail or msg[:DETAIL_CUT] in e.value.detail
    for text in (str(e.value), repr(e.value), e.value.detail, pickle.dumps(e.value).decode("latin-1")):
        assert jwt not in text and jwt.split(".")[1] not in text
    assert not p.logged_in


DETAIL_CUT = 120


def test_expired_jwt_accepted_within_leeway(srv):
    # essai 4 : exp + 30 s encore accepté (marge par défaut d'OpenBao)
    person(srv).login_jwt(MOUNT_JWT, "person", fake_jwt(claims(exp=int(time.time()) - 30)))


def test_login_refused_outside_bound_cidrs(srv):
    srv.jwt[MOUNT_JWT]["cidr_ok"] = False
    with pytest.raises(oc.OpenBaoError) as e:
        person(srv).login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    assert e.value.code == "openbao-person-login-failed" and e.value.http_status == 403
    assert MSG_DENIED in e.value.detail


def test_login_unreachable_keeps_its_code(srv):
    srv.fail_next.extend([(502, {"errors": ["bad gateway"]})] * 4)
    with pytest.raises(oc.OpenBaoError) as e:
        person(srv).login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    assert e.value.code == "openbao-unreachable"


def test_login_without_jwt(srv):
    with pytest.raises(oc.OpenBaoError) as e:
        person(srv).login_jwt(MOUNT_JWT, "person", "")
    assert e.value.code == "openbao-person-login-failed"
    assert not srv.requests("auth/")


def test_jwt_in_error_body_is_scrubbed(srv):
    """Un corps d'erreur qui recopie le JWT (proxy, version future) : le
    filtre retire tout motif `eyJ...` de plus de 20 caractères."""
    jwt = fake_jwt(claims())
    srv.fail_next.append((400, {"errors": [f"cannot parse {jwt} (header {jwt.split('.')[0]})"]}))
    with pytest.raises(oc.OpenBaoError) as e:
        person(srv).login_jwt(MOUNT_JWT, "person", jwt)
    for part in (jwt, jwt.split(".")[0], jwt.split(".")[1]):
        assert part not in str(e.value)
    assert "[jwt]" in e.value.detail


@pytest.mark.parametrize("text,kept", [
    ("eyJhbGciOiJSUzI1NiJ9", True),                   # 20 caractères : sous le seuil
    ("eyJhbGciOiJSUzI1NiIsImtpZCI6ImtleSJ9.eyJzdWIiOiJ1In0.c2ln", False),
    ("token eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9 end", False),
])
def test_scrub_jwt_threshold(text, kept):
    out = oc.scrub(text)
    assert (text in out) is kept


def test_login_jwt_refused_on_service_client(srv):
    svc = service(srv)
    with pytest.raises(ValueError):
        svc.login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    assert svc.kind == "service" and svc.logged_in


def test_person_client_refuses_approle(srv):
    rid, sid = srv.add_role()
    with pytest.raises(ValueError):
        person(srv).login_approle(rid, sid)


# ---------------------------------------------------------------------------
# for_token : copie qui porte son propre jeton
# ---------------------------------------------------------------------------
def test_for_token_leaves_service_token_untouched(srv):
    svc = service(srv, actor="alice", action="a1")
    before = svc._tok.value
    p = svc.for_token(None)
    p.login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    assert svc._tok.value == before and svc.kind == "service"
    assert p._tok.value != before and p._tok is not svc._tok
    assert p.actor == "alice" and p.action == "a1"          # en-têtes de corrélation gardés
    p.revoke_self()
    assert svc._tok.value == before and svc.logged_in
    # le service lit toujours
    srv.put_raw(KV, "x/y", {"f": "v"})
    assert svc.kv_get(KV, "x/y")[0] == {"f": "v"}


def test_for_token_with_given_token(srv):
    t = srv.add_token()
    svc = service(srv)
    p = svc.for_token(t, ttl=900)
    assert p._tok.value == t and p.kind == "person" and 899 <= p.remaining() <= 900
    q = p.with_headers("a2", "bob")
    assert q._tok is p._tok                                 # même jeton pour une action


def test_for_token_unknown_kind(srv):
    with pytest.raises(ValueError):
        service(srv).for_token(None, kind="root")


# ---------------------------------------------------------------------------
# Jeton de personne échu : jamais de reconnexion seule
# ---------------------------------------------------------------------------
def test_expired_person_token_is_returned_to_caller(srv):
    rid, sid = srv.add_role()
    clock = [1000.0]
    base = oc.Client(srv.addr, timeout=3, sleep=Sleeps(), clock=lambda: clock[0])
    base.login_approle(rid, sid)
    p = base.for_token(None)
    p.login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    srv.put_raw(KV, f"{PREFIX}/ssh-kvm/k1", {"private_key": "K"})
    n_login = len(srv.requests("auth/approle/login"))
    clock[0] += 901
    with pytest.raises(oc.OpenBaoError) as e:
        p.kv_get(KV, f"{PREFIX}/ssh-kvm/k1")
    assert e.value.code == "openbao-person-expired"
    assert len(srv.requests("auth/approle/login")) == n_login       # aucune reconnexion AppRole
    assert len(srv.requests(f"auth/{MOUNT_JWT}/login")) == 1        # ni jwt
    assert not srv.requests("auth/token/renew-self")


def test_person_token_revoked_server_side_no_approle(srv):
    """Jeton refusé par OpenBao (révoqué ailleurs, restauration) : 403
    rendu, aucune reconnexion AppRole (revue M3 de la 2.9.0 réservée au
    service)."""
    svc = service(srv)
    p = svc.for_token(None)
    p.login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    srv.tokens.pop(p._tok.value)
    srv.put_raw(KV, f"{PREFIX}/ssh-kvm/k1", {"private_key": "K"})
    n_login = len(srv.requests("auth/approle/login"))
    with pytest.raises(oc.OpenBaoError) as e:
        p.kv_get(KV, f"{PREFIX}/ssh-kvm/k1")
    assert e.value.code == "openbao-denied"
    assert len(srv.requests("auth/approle/login")) == n_login
    assert not p.token_valid()


def test_person_denied_by_policy(srv):
    p = person(srv)
    p.login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    srv.put_raw(KV, f"{PREFIX}/kubeconfig/h1", {"kubeconfig": "K"})
    with pytest.raises(oc.OpenBaoError) as e:
        p.kv_get(KV, f"{PREFIX}/kubeconfig/h1")
    assert e.value.code == "openbao-denied" and "capabilities: deny" in e.value.detail


def test_person_renew_refused(srv):
    p = person(srv)
    p.login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    with pytest.raises(oc.OpenBaoError) as e:
        p.renew_self()
    assert e.value.code == "openbao-person-expired"
    assert not srv.requests("renew-self")


# ---------------------------------------------------------------------------
# revoke-self
# ---------------------------------------------------------------------------
def test_revoke_self_person(srv):
    p = person(srv)
    p.login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    tok = p._tok.value
    p.revoke_self()
    assert srv.requests("auth/token/revoke-self")[-1]["headers"]["x-vault-token"] == tok
    assert tok not in srv.tokens and not p.logged_in


def test_revoke_self_person_forgets_even_on_403(srv):
    p = person(srv)
    p.login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    srv.tokens.pop(p._tok.value)                          # déjà échu côté OpenBao
    p.revoke_self()                                       # pas d'exception
    assert srv.requests("auth/token/revoke-self")
    assert not p.logged_in and p.entity_id is None


def test_revoke_self_person_forgets_when_unreachable(srv):
    p = person(srv)
    p.login_jwt(MOUNT_JWT, "person", fake_jwt(claims()))
    srv.fail_next.append((502, {"errors": ["bad gateway"]}))
    with pytest.raises(oc.OpenBaoError):
        p.revoke_self()
    assert not p.logged_in
