"""2.10.0 tâches 11 et 12 : routes de Paramètres > Secrets > Jetons
personnels, refus personnels en 403, bandeau, entités vues, révocation
immédiate quand le réglage s'éteint, surveillance des clés illisibles, garde
de retour sous la 2.10.0.

Chaque geste est une action suivie `harvester-secrets person-token ...`
(argv vérifié en tableau, un cas par refus) ; le jeton d'identité de
l'administrateur part par le fichier de spécification 0600, jamais en
argument ni dans une réponse ; émetteur, client et CA d'un Rancher viennent
de ses réglages, jamais de la requête.

Mutations jouées (rapport des tâches 11 et 12) : « jeton d'identité passé en
argument », « vérifications permises à un compte local », « refus personnel
rendu 503 », « réglage éteint sans révocation », « garde de retour sans le
cas B1 », « plage 0.0.0.0/0 acceptée ».
"""

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import openbao_policy as pol  # noqa: E402
import secret_store as ss  # noqa: E402
from test_secrets_routes_290 import CID, post, sx, wapp  # noqa: E402,F401

ISS = "https://rancher.example/oidc"
CLIENT = "client-7zjw5jtpx6"
IDT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyLWtrNjdqIn0.SIGNATUREpersonidtokenWITNESS"
UUID_A = "11111111-2222-3333-4444-555555555555"
UUID_B = "22222222-3333-4444-5555-666666666666"


class Sess:
    def __init__(self, sid="s1", kind="sso", id_token=IDT, server_id=None, user="user-kk67j"):
        self.sid, self.kind, self.id_token, self.server_id = sid, kind, id_token, server_id
        self.ident = {"id": user, "username": "alice", "name": "Alice Doe"}
        self.login = "alice@rancher"
        self.role = "admin"


@pytest.fixture
def pt(sx, wapp, monkeypatch, tmp_path):
    ca = tmp_path / "rancher-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    choices = [{"key": "config", "id": "rancher-example", "label": "Rancher", "url": "https://rancher.example",
                "origin": "config", "issuer": ISS, "client_id": CLIENT, "sso": True, "ca_file": None},
               {"key": "rmgr", "id": "rmgr", "label": "Lab <b>", "url": "https://lab.example",
                "origin": "console", "issuer": "https://lab.example/oidc", "client_id": "client-lab", "sso": True,
                "ca_file": str(ca)}]
    monkeypatch.setattr(wapp, "_person_rancher_choices", lambda: [dict(c) for c in choices])
    sess = {"s": None}
    monkeypatch.setattr(wapp, "_sso_session", lambda: sess["s"])
    monkeypatch.setattr(wapp, "_same_origin", lambda: True)            # écriture venue de la console
    monkeypatch.setattr(wapp, "_PERSON_NOTICE", {})
    monkeypatch.setattr(wapp, "_PERSON_KEYS", {"at": 0.0, "seen": {}, "fails": {}})
    sx.sess = sess
    sx.ca = ca
    return sx


def last(sx):
    return sx.launched[-1]


# ---------------------------------------------------------------------------
# Rôles : tout est à l'administrateur (préfixe /api/secrets/)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,method", [
    ("/api/secrets/person-token", "GET"),
    ("/api/secrets/person-token/assistant", "POST"),
    ("/api/secrets/person-token/check", "POST"),
    ("/api/secrets/person-token/enable", "POST"),
    ("/api/secrets/person-token/disable", "POST"),
    ("/api/secrets/person-token/groups", "POST"),
    ("/api/secrets/person-token/groups-only", "POST"),
    ("/api/secrets/person-token/keys", "POST"),
])
def test_roles_admin_only(wapp, path, method):
    assert wapp.required_role_for(path, method) == "admin"


# ---------------------------------------------------------------------------
# Lecture : état sans valeur
# ---------------------------------------------------------------------------
def test_state_without_values(wapp, pt):
    pt.set(person_token={"enabled": True, "groups_only": False,
                         "servers": {"config": {"mount": f"jwt-hops-{CID}-p-config", "client_id": CLIENT,
                                                "issuer": ISS, "keys": "discovery", "keys_sha256": "ab" * 32,
                                                "configured_at": 1, "configured_by": "alice", "checked_at": 2,
                                                "ca_pem": "-----BEGIN CERTIFICATE-----SECRETLESS"}},
                         "pending": {"gone1": {"mount": f"jwt-hops-{CID}-p-gone1", "keys": "static"}},
                         "groups": {"ops": {"classes": ["ssh-kvm"], "clusters": ["node2"], "members": [UUID_A],
                                            "at": 3, "by": "alice"}}})
    (pt.state / wapp.PERSON_ENTITIES_FILE).write_text(json.dumps({
        UUID_A: {"sub": "user-kk67j", "name": "Alice", "user": "alice@rancher", "rancher": "config", "last_at": 5},
        "not-an-entity": {"sub": "x"}}))
    pt.sess["s"] = Sess()
    status, doc = post(wapp, "/api/secrets/person-token", method="get")
    assert status == 200, doc
    assert doc["enabled"] is True and doc["mode"] == "a"
    by = {r["key"]: r for r in doc["ranchers"]}
    assert set(by) == {"config", "rmgr", "gone1"}
    assert by["config"]["configured"]["mount"] == f"jwt-hops-{CID}-p-config"
    assert by["rmgr"]["configured"] is None and by["rmgr"]["has_ca"] is True
    assert by["gone1"]["origin"] == "gone" and by["gone1"]["pending"]["keys"] == "static"
    assert [e["entity"] for e in doc["entities"]] == [UUID_A]
    assert doc["groups"]["ops"]["members"] == [UUID_A]
    assert doc["me"] == {"sso": True, "rancher": "config", "id_token": True, "kind": "sso"}
    assert doc["limits"] == {"ranchers": pol.MAX_PERSON_MOUNTS, "groups": pol.MAX_GROUPS,
                             "members": pol.MAX_GROUP_MEMBERS}
    blob = json.dumps(doc)
    assert IDT not in blob and "SECRETLESS" not in blob and "ca_file" not in blob


def test_state_of_a_local_account(wapp, pt):
    status, doc = post(wapp, "/api/secrets/person-token", method="get")
    assert status == 200 and doc["me"] == {"sso": False, "rancher": None, "id_token": False, "kind": "local"}
    assert doc["enabled"] is False and doc["entities"] == []


# ---------------------------------------------------------------------------
# Assistant : émetteur, client et CA des réglages du Rancher
# ---------------------------------------------------------------------------
def test_assistant_mode_a_argv_and_ca_by_file(wapp, pt):
    status, doc = post(wapp, "/api/secrets/person-token/assistant",
                       {"rancher": "rmgr", "keys": "static", "console_cidr": "10.0.0.5/32",
                        "issuer": "https://evil.example/oidc", "client_id": "evil"})
    assert status == 202, doc
    run = last(pt)
    assert run["label"] == "secrets:person-token-assistant:rmgr"
    assert run["argv"][2:] == ["person-token", "assistant", "--rancher", "rmgr", "--issuer", "https://lab.example/oidc",
                               "--client-id", "client-lab", "--keys", "static", "--console-cidr", "10.0.0.5/32",
                               "--spec", run["spec"]]
    assert run["spec_doc"] == {"rancher_ca": pt.ca.read_text()} and run["spec_mode"] == 0o600
    assert "evil" not in json.dumps(run["argv"])


def test_assistant_b1_without_ca(wapp, pt):
    pt.set(mode="b1")
    status, _ = post(wapp, "/api/secrets/person-token/assistant", {"rancher": "config"})
    assert status == 202
    run = last(pt)
    assert run["argv"][2:] == ["person-token", "assistant", "--rancher", "config", "--issuer", ISS, "--client-id",
                               CLIENT, "--keys", "discovery"] and run["spec"] is None


@pytest.mark.parametrize("body,mode,status,code", [
    ({"rancher": "nope"}, "a", 404, "rancher-unknown"),
    ({"rancher": "Bad/Key"}, "a", 400, "bad-input"),
    ({"rancher": "config", "keys": "both"}, "a", 400, "bad-input"),
    ({"rancher": "config", "keys": "static"}, "b1", 400, "person-token-static-mode-a"),
    ({"rancher": "config"}, "a", 400, "bad-input"),                                     # plage absente
    ({"rancher": "config", "console_cidr": "0.0.0.0/0"}, "a", 400, "bad-input"),       # revue 6-7 M-5
    ({"rancher": "config", "console_cidr": "10.0.0.0/7"}, "a", 400, "bad-input"),
    ({"rancher": "config", "console_cidr": "not-a-net"}, "a", 400, "bad-input"),
])
def test_assistant_refusals(wapp, pt, body, mode, status, code):
    pt.set(mode=mode)
    st, doc = post(wapp, "/api/secrets/person-token/assistant", body)
    assert (st, doc.get("code")) == (status, code)
    assert pt.launched == []


def test_assistant_refused_when_openbao_is_off(wapp, pt):
    pt.set(mode="local")
    st, doc = post(wapp, "/api/secrets/person-token/assistant", {"rancher": "config"})
    assert (st, doc["code"]) == (409, "openbao-inactive")


# ---------------------------------------------------------------------------
# Vérifications et activation : connexion réelle de l'administrateur
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("route", ["check", "enable"])
@pytest.mark.parametrize("sess,code", [
    (None, "openbao-person-no-token"),                                 # compte local
    (Sess(kind="direct", id_token=None), "openbao-person-no-token"),   # connexion directe
    (Sess(id_token=None), "openbao-person-no-token"),                  # session d'avant l'activation
    (Sess(server_id="rmgr"), "person-token-other-rancher"),            # connecté par un autre Rancher
])
def test_checks_need_a_rancher_sign_in(wapp, pt, route, sess, code):
    pt.sess["s"] = sess
    st, doc = post(wapp, f"/api/secrets/person-token/{route}", {"rancher": "config"})
    assert (st, doc["code"]) == (409, code)
    assert pt.launched == []


@pytest.mark.parametrize("route,label", [("check", "secrets:person-token-check:config"),
                                         ("enable", "secrets:person-token-enable:config")])
def test_checks_send_the_identity_token_by_file_only(wapp, pt, route, label):
    pt.sess["s"] = Sess()
    st, doc = post(wapp, f"/api/secrets/person-token/{route}", {"rancher": "config", "id_token": "eyJforged"})
    assert st == 202, doc
    run = last(pt)
    assert run["label"] == label
    assert run["argv"][2:] == ["person-token", route, "--rancher", "config", "--spec", run["spec"]]
    assert run["spec_doc"] == {"id_token": IDT} and run["spec_mode"] == 0o600
    assert IDT not in json.dumps(run["argv"]) and IDT not in json.dumps(doc)
    assert not Path(run["spec"]).exists()                              # effacé à la fin de l'action


# ---------------------------------------------------------------------------
# Désactivation : à chaud, jetons révoqués aussitôt (point 5 de la revue 2-5)
# ---------------------------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.revoked = 0

    def remaining(self):
        return 600

    def revoke_self(self):
        self.revoked += 1


@pytest.fixture
def tokens(monkeypatch):
    reg = ss.PersonTokens()
    monkeypatch.setattr(ss, "PERSON_TOKENS", reg)
    a, b = FakeClient(), FakeClient()
    reg.set("s1", a, UUID_A, mount=f"jwt-hops-{CID}-p-config")
    reg.set("s2", b, UUID_B, mount=f"jwt-hops-{CID}-p-rmgr")
    return reg, a, b


def test_disable_revokes_person_tokens_at_once(wapp, pt, tokens):
    reg, a, b = tokens
    pt.set(person_token={"enabled": True, "servers": {"config": {"mount": f"jwt-hops-{CID}-p-config"}}})

    def fake_cli(cluster, label, cmd, tool, after=None, **kw):
        pt.set(person_token={"enabled": False, "servers": {"config": {"mount": f"jwt-hops-{CID}-p-config"}}})
        pt.launched.append({"label": label, "argv": cmd[cmd.index("--actor"):]})
        if after:
            after()
        return wapp.ActionRun("act0001", label, "", []), None
    wapp_cli = wapp._cli_action
    try:
        wapp._cli_action = fake_cli
        st, doc = post(wapp, "/api/secrets/person-token/disable", {})
    finally:
        wapp._cli_action = wapp_cli
    assert st == 202, doc
    assert pt.launched[-1]["argv"][2:] == ["person-token", "disable"]
    assert (a.revoked, b.revoked) == (1, 1) and reg.count() == 0


def test_remove_one_rancher_revokes_its_sessions_only(wapp, pt, tokens):
    reg, a, b = tokens
    pt.set(person_token={"enabled": True, "servers": {"config": {"mount": f"jwt-hops-{CID}-p-config"},
                                                      "rmgr": {"mount": f"jwt-hops-{CID}-p-rmgr"}}})
    st, doc = post(wapp, "/api/secrets/person-token/disable", {"rancher": "rmgr", "remove": True, "confirm": CID})
    assert st == 202, doc
    assert last(pt)["argv"][2:] == ["person-token", "disable", "--rancher", "rmgr", "--remove"]
    assert last(pt)["label"] == "secrets:person-token-remove:rmgr"
    assert (a.revoked, b.revoked) == (0, 1) and reg.count() == 1


@pytest.mark.parametrize("body,status,code", [
    ({"remove": True}, 400, "confirmation-required"),
    ({"remove": True, "confirm": "wrong"}, 400, "confirmation-required"),
    ({"rancher": "Bad/K"}, 400, "bad-input"),
    ({"rancher": "unknown"}, 404, "person-token-unconfigured"),
])
def test_disable_refusals(wapp, pt, body, status, code):
    st, doc = post(wapp, "/api/secrets/person-token/disable", body)
    assert (st, doc["code"]) == (status, code) and pt.launched == []


def test_remove_everything_argv(wapp, pt):
    st, _ = post(wapp, "/api/secrets/person-token/disable", {"remove": True, "confirm": CID})
    assert st == 202 and last(pt)["argv"][2:] == ["person-token", "disable", "--remove"]
    assert last(pt)["label"] == "secrets:person-token-remove"


def test_sweep_revokes_when_the_setting_is_off_by_the_cli(wapp, pt, tokens):
    """`sudo harvester-secrets person-token disable` éteint le réglage sans
    passer par la console : la sonde de 30 s révoque les jetons restants."""
    reg, a, b = tokens
    pt.set(person_token={"enabled": True, "servers": {"config": {"mount": "x"}}})
    assert wapp._person_tokens_sweep() == 0 and reg.count() == 2
    pt.set(person_token={"enabled": False, "servers": {"config": {"mount": "x"}}})
    assert wapp._person_tokens_sweep() == 2 and (a.revoked, b.revoked) == (1, 1) and reg.count() == 0
    assert wapp._person_tokens_sweep() == 0


# ---------------------------------------------------------------------------
# Groupes : membres parmi les entités vues, bornes (revue 6-7 M-8)
# ---------------------------------------------------------------------------
def test_group_set_argv(wapp, pt):
    st, doc = post(wapp, "/api/secrets/person-token/groups",
                   {"name": "ops", "classes": ["ssh-kvm", "tf-kvm"], "clusters": ["node2"], "members": [UUID_A]})
    assert st == 202, doc
    assert last(pt)["argv"][2:] == ["person-token", "groups", "set", "ops", "--class", "ssh-kvm", "--class", "tf-kvm",
                                    "--cluster", "node2", "--member", UUID_A]


def test_group_remove_and_groups_only(wapp, pt):
    assert post(wapp, "/api/secrets/person-token/groups", {"name": "ops", "remove": True})[0] == 202
    assert last(pt)["argv"][2:] == ["person-token", "groups", "remove", "ops"]
    assert post(wapp, "/api/secrets/person-token/groups-only", {"on": True})[0] == 202
    assert last(pt)["argv"][2:] == ["person-token", "groups", "only", "on"]
    assert post(wapp, "/api/secrets/person-token/groups-only", {"on": "yes"})[0] == 400


@pytest.mark.parametrize("body,status,code", [
    ({"name": "Bad/Name", "classes": ["ssh-kvm"], "clusters": ["a"]}, 400, "bad-input"),
    ({"name": "ops", "classes": ["rancher-oidc"], "clusters": ["a"]}, 400, "bad-input"),
    ({"name": "ops", "classes": [], "clusters": ["a"]}, 400, "bad-input"),
    ({"name": "ops", "classes": ["ssh-kvm"], "clusters": ["*"]}, 400, "bad-input"),
    ({"name": "ops", "classes": ["ssh-kvm"], "clusters": []}, 400, "bad-input"),
    ({"name": "ops", "classes": ["eso"], "clusters": ["harv1"]}, 400, "bad-input"),     # clé ESO attendue
    ({"name": "ops", "classes": ["ssh-kvm"], "clusters": ["a"], "members": ["x"]}, 400, "bad-input"),
    ({"name": "ops", "classes": ["ssh-kvm"], "clusters": ["a"], "members": [UUID_A, UUID_A]}, 400, "bad-input"),
])
def test_group_refusals(wapp, pt, body, status, code):
    st, doc = post(wapp, "/api/secrets/person-token/groups", body)
    assert (st, doc["code"]) == (status, code) and pt.launched == []


def test_group_limits(wapp, pt):
    too_many = [f"{i:08x}-0000-4000-8000-000000000000" for i in range(pol.MAX_GROUP_MEMBERS + 1)]
    st, doc = post(wapp, "/api/secrets/person-token/groups",
                   {"name": "ops", "classes": ["ssh-kvm"], "clusters": ["a"], "members": too_many})
    assert (st, doc["code"]) == (400, "bad-input")
    pt.set(person_token={"groups": {f"g{i}": {"classes": ["ssh-kvm"], "clusters": ["a"], "members": []}
                                    for i in range(pol.MAX_GROUPS)}})
    st, doc = post(wapp, "/api/secrets/person-token/groups", {"name": "new", "classes": ["ssh-kvm"], "clusters": ["a"]})
    assert (st, doc["code"]) == (409, "limit-groups")
    st, _ = post(wapp, "/api/secrets/person-token/groups", {"name": "g1", "classes": ["ssh-kvm"], "clusters": ["b"]})
    assert st == 202                                                    # un groupe existant se modifie


def test_keys_argv(wapp, pt):
    wapp._PERSON_KEYS["seen"]["config"] = "x"
    wapp._PERSON_KEYS["fails"]["config"] = 5
    assert post(wapp, "/api/secrets/person-token/keys", {"rancher": "config"})[0] == 202
    assert last(pt)["argv"][2:] == ["person-token", "keys", "--rancher", "config", "--refresh"]
    assert "config" not in wapp._PERSON_KEYS["seen"] and "config" not in wapp._PERSON_KEYS["fails"]
    assert post(wapp, "/api/secrets/person-token/keys", {"rancher": "config", "confirm": True})[0] == 202
    assert last(pt)["argv"][-1] == "--confirm"
    assert post(wapp, "/api/secrets/person-token/keys", {"rancher": "x/y"})[0] == 400


# ---------------------------------------------------------------------------
# Revue M8 : un refus fait à une personne est un 403, une panne reste 503
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("err,status", [
    (ss.SecretError("openbao-denied", "ssh-kvm", "node2", person=True), 403),
    (ss.SecretError("openbao-denied", "ssh-kvm", "node2"), 503),                 # refus fait au service
    (ss.SecretError("openbao-person-login-failed"), 403),
    (ss.SecretError("openbao-person-no-token"), 403),
    (ss.SecretError("openbao-person-expired"), 403),
    (ss.SecretError("openbao-unreachable", "ssh-kvm", "node2", person=True), 503),
    (ss.SecretError("secret-too-big"), 413),
])
def test_secret_error_status(wapp, err, status):
    assert wapp._secret_error_status(err) == status


def test_person_refusal_reaches_the_banner_of_that_session_only(wapp, pt):
    pt.sess["s"] = Sess(sid="s1")
    with wapp.app.test_request_context("/api/x"):
        body, status = wapp._secret_store_refused(ss.SecretError("openbao-person-login-failed", detail="audience"))
    assert status == 403 and body.get_json()["code"] == "openbao-person-login-failed"
    st, b = post(wapp, "/api/secrets/banner", method="get")
    assert b["person"]["code"] == "openbao-person-login-failed"
    pt.sess["s"] = Sess(sid="s2")
    assert post(wapp, "/api/secrets/banner", method="get")[1]["person"] is None
    # une connexion réussie de s1 efface le refus
    wapp._PERSON_NOTICE["s1"]["at"] = time.time()
    wapp._person_seen("s1", "not-an-entity")
    pt.sess["s"] = Sess(sid="s1")
    assert post(wapp, "/api/secrets/banner", method="get")[1]["person"] is None


def test_person_error_flag_survives_pickling():
    import pickle
    e = pickle.loads(pickle.dumps(ss.SecretError("openbao-denied", "ssh-kvm", "n", "d", person=True)))
    assert e.person is True and e.code == "openbao-denied"


# ---------------------------------------------------------------------------
# Entités vues (membres de groupe proposés)
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, sessions):
        self.sessions = sessions

    def get(self, sid):
        return self.sessions.get(sid)


def test_person_seen_records_then_throttles(wapp, pt, monkeypatch):
    monkeypatch.setattr(wapp, "_sso_store", lambda: Store({"s1": Sess(sid="s1", server_id="rmgr")}))
    f = pt.state / wapp.PERSON_ENTITIES_FILE
    assert wapp._person_seen("s1", UUID_A, now=1000.0) is True
    doc = json.loads(f.read_text())
    assert doc[UUID_A] == {"sub": "user-kk67j", "name": "Alice Doe", "user": "alice@rancher", "rancher": "rmgr",
                           "last_at": 1000.0}
    assert (f.stat().st_mode & 0o777) == 0o640
    assert wapp._person_seen("s1", UUID_A, now=1000.0 + 60) is False          # moins d'une heure : rien réécrit
    assert wapp._person_seen("s1", UUID_A, now=1000.0 + 3700) is True
    assert wapp._person_seen("unknown", UUID_B) is False
    assert wapp._person_seen("s1", "../../etc") is False
    assert IDT not in f.read_text()


def test_person_seen_is_bounded(wapp, pt, monkeypatch):
    monkeypatch.setattr(wapp, "PERSON_ENTITIES_MAX", 3)
    monkeypatch.setattr(wapp, "_sso_store", lambda: Store({"s1": Sess(sid="s1")}))
    for i in range(5):
        wapp._person_seen("s1", f"{i:08x}-0000-4000-8000-000000000000", now=100.0 + i)
    doc = json.loads((pt.state / wapp.PERSON_ENTITIES_FILE).read_text())
    assert sorted(doc) == [f"{i:08x}-0000-4000-8000-000000000000" for i in (2, 3, 4)]


def test_person_tokens_call_on_login_after_a_real_login():
    """Le rappel est posé par la console sur le registre : il part après une
    connexion réussie, jamais avec un jeton."""
    seen = []
    reg = ss.PersonTokens(id_token=lambda sid: IDT)
    reg.on_login = lambda sid, entity: seen.append((sid, entity))

    class C:
        timeout = 5

        def for_token(self, _t):
            return self

        def login_jwt(self, mount, role, jwt, retry=True):
            return {"entity_id": UUID_A}
    reg.base = lambda: C()
    reg.login(ss.Identity("person", "alice", session_id="s1", mount="jwt-hops-x-p-config"))
    assert seen == [("s1", UUID_A)]


# ---------------------------------------------------------------------------
# Revue 6-7 M-7 : JWKS illisible plusieurs passages de suite
# ---------------------------------------------------------------------------
def test_unreadable_jwks_warns_after_three_hourly_failures(wapp, pt):
    pt.set(person_token={"enabled": True, "servers": {"config": {"mount": "m", "keys": "static", "issuer": ISS,
                                                                 "keys_sha256": "x"}}})

    def broken(url, ca):
        raise ValueError("connection refused")
    for i in range(wapp.PERSON_KEYS_UNREADABLE_AFTER):
        s = wapp._secrets_settings()
        assert not wapp._person_keys_unreadable(s)
        wapp._person_keys_tick(now=10_000.0 * (i + 1), fetch=broken)
    s = wapp._secrets_settings()
    assert wapp._person_keys_unreadable(s) == ["config"]
    codes = [w["code"] for w in wapp._secrets_warnings(s, {}, {"state": "ok"})]
    assert "openbao-jwt-keys-unreadable" in codes
    assert post(wapp, "/api/secrets/banner", method="get")[1]["jwt_keys_unreadable"] is True
    wapp._person_keys_tick(now=100_000.0, fetch=lambda url, ca: {"keys": [{"kid": "k", "kty": "RSA", "n": "AQ",
                                                                            "e": "AQAB"}]})
    assert not wapp._person_keys_unreadable(wapp._secrets_settings())


# ---------------------------------------------------------------------------
# Tâche 12 (point 291) : retour sous la 2.10.0
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("settings,expect", [
    ({"mode": "local"}, {}),
    ({"mode": "b1"}, {}),
    ({"mode": "b1", "person_token": {"servers": {"config": {}}}}, {"b1": {"jwt": ["config"]}}),
    ({"mode": "b1", "person_token": {"pending": {"rmgr": {}}}}, {"b1": {"jwt": ["rmgr"]}}),
    ({"mode": "b1", "person_token": {"groups": {"ops": {}}}}, {"b1": {"groups": ["ops"]}}),
    ({"mode": "b1", "eso": {"listener": "172.16.1.11:8212"}}, {"b1": {"eso_listener": "172.16.1.11:8212"}}),
    ({"mode": "b1", "eso": {"clusters": {"1d905023": {}}}}, {"b1": {"eso_clusters": ["1d905023"]}}),
    ({"mode": "a", "addr": "https://bao:8200", "person_token": {"servers": {"config": {}}}},
     {"warnings": {"jwt": ["config"]}}),
])
def test_person_token_blockers(tmp_path, settings, expect):
    (tmp_path / ss.SETTINGS_FILE).write_text(json.dumps(dict(settings, console_id=CID)))
    assert ss.person_token_blockers(tmp_path) == expect


class Update:
    def __init__(self, wapp, monkeypatch, tmp_path, version):
        self.req = tmp_path / "updates"
        self.req.mkdir()
        monkeypatch.setattr(wapp, "_updates_dir", lambda: self.req)
        monkeypatch.setattr(wapp, "_update_busy_actions", lambda: [])
        monkeypatch.setattr(wapp, "_harvester_ops_version", lambda: "2.10.0")
        self.chk = {"version": version, "older": True, "ok": True}


@pytest.mark.parametrize("version,settings,status,code", [
    ("2.9.1", {"mode": "b1", "person_token": {"servers": {"config": {}}}}, 409, "secrets-person-token-b1"),
    ("2.9.1", {"mode": "b1", "eso": {"listener": "172.16.1.11:8212"}}, 409, "secrets-person-token-b1"),
    ("2.10.0", {"mode": "b1", "person_token": {"servers": {"config": {}}}}, 202, None),
    ("2.9.1", {"mode": "b1"}, 202, None),
])
def test_rollback_below_2100_refused_while_b1_carries_jwt_or_eso(wapp, pt, monkeypatch, tmp_path, version,
                                                                   settings, status, code):
    monkeypatch.setattr(ss, "openbao_blockers", lambda *a, **k: {})          # garde de la 2.9.0 à part
    pt.set(**settings)
    u = Update(wapp, monkeypatch, tmp_path, version)
    with wapp.app.test_request_context("/api/update/rollback", method="POST"):
        resp = wapp._update_hand_over("a.tar.gz", u.chk, {}, "rollback", [])
    body, st = (resp[0], resp[1]) if isinstance(resp, tuple) else (resp, resp.status_code)
    doc = body.get_json()
    assert st == status, doc
    if code:
        assert doc["code"] == code and doc["steps"] == list(ss.PERSON_TOKEN_BACK_STEPS)
        assert "person-token disable --remove" in doc["hint"] and "eso remove --all" in doc["hint"]
        assert not (u.req / "request.json").exists()
    else:
        assert "warning" not in doc


def test_rollback_below_2100_in_mode_a_warns(wapp, pt, monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "openbao_blockers", lambda *a, **k: {})
    pt.set(person_token={"servers": {"config": {}}})
    u = Update(wapp, monkeypatch, tmp_path, "2.9.1")
    with wapp.app.test_request_context("/api/update/rollback", method="POST"):
        body, st = wapp._update_hand_over("a.tar.gz", u.chk, {}, "rollback", [])
    doc = body.get_json()
    assert st == 202 and "mode A" in doc["warning"]
    run = wapp.ACTIONS[doc["action_id"]]
    assert any(e.get("step_id") == "person-token" for e in run.events)
