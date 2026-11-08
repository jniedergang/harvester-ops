"""2.10.0 tâche 3 : règles pures du jeton par personne (bin/lib/openbao_jwt.py).

Table « Qui agit » ligne par ligne, montage par Rancher, paramètres exacts
du rôle `person` et de la configuration (plan 2.10.0, « Méthode `jwt` d'une
personne »), empreinte d'un JWKS.
"""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import openbao_jwt as oj         # noqa: E402
import secret_classes as sc      # noqa: E402
import secret_store as ss        # noqa: E402

CID = "abc123def456"
ON = {"mode": "b1", "addr": "https://127.0.0.1:8210", "prefix": f"consoles/{CID}", "console_id": CID,
      "person_token": {"enabled": True, "servers": {"rmgr": {"mount": f"jwt-hops-{CID}-rmgr"},
                                                     "config": {"mount": f"jwt-hops-{CID}-config"}}}}


def sess(kind="sso", server="rmgr", sid="s1", login="alice@rancher"):
    return types.SimpleNamespace(kind=kind, server_id=server, sid=sid, login=login)


ALL = ("kubeconfig", "ssh-cluster", "ssh-kvm", "rancher-oidc", "replication-notify", "replication-hook",
       "tf-kvm", "plan-run", "eso")
PERSON = {"ssh-cluster", "ssh-kvm", "tf-kvm", "eso"}


@pytest.mark.parametrize("cls", ALL)
@pytest.mark.parametrize("case,session,settings,expect", [
    ("sso", sess(), ON, "class"),
    ("sso-config-server", sess(server=None), ON, "class"),
    ("direct", sess(kind="direct"), ON, "service"),
    ("local-account", None, ON, "service"),
    ("setting-off", sess(), dict(ON, person_token={"enabled": False, "servers": ON["person_token"]["servers"]}),
     "service"),
    ("setting-absent", sess(), {k: v for k, v in ON.items() if k != "person_token"}, "service"),
    ("openbao-local", sess(), dict(ON, mode="local"), "service"),
    ("rancher-not-configured", sess(server="other"), ON, "service"),
    ("enabled-not-true", sess(), dict(ON, person_token={"enabled": "yes", "servers": ON["person_token"]["servers"]}),
     "service"),
])
def test_who_acts(cls, case, session, settings, expect):
    idt = oj.identity_for(cls, session, settings)
    want_person = expect == "class" and cls in PERSON
    assert idt.kind == ("person" if want_person else "service"), case
    if session is not None:
        assert idt.actor == "alice@rancher"            # X-Hops-Actor = la personne, toujours
    if want_person:
        assert idt.session_id == "s1" and idt.role == "person"
        assert idt.mount == (f"jwt-hops-{CID}-config" if session.server_id is None else f"jwt-hops-{CID}-rmgr")


def test_who_acts_local_actor():
    assert oj.identity_for("ssh-kvm", None, ON, actor="admin") == ss.Identity("service", "admin")


def test_person_classes_match_table():
    assert set(sc.PERSON) == PERSON


@pytest.mark.parametrize("rid,mount", [
    ("rmgr", f"jwt-hops-{CID}-rmgr"),
    ("abcdefghijkl", f"jwt-hops-{CID}-abcdefghijkl"),          # 12 : gardé
])
def test_person_mount(rid, mount):
    assert oj.person_mount(CID, rid) == mount


def test_person_mount_long_ids_are_distinct():
    a, b = oj.person_mount(CID, "rancher-prod-paris"), oj.person_mount(CID, "rancher-prod-lyon")
    assert a != b
    for m in (a, b):
        tail = m[len(f"jwt-hops-{CID}-"):]
        assert len(tail) <= 12 and tail.startswith("ranche")


@pytest.mark.parametrize("cid,rid", [("ABC", "rmgr"), (CID, "Bad_Id"), (CID, ""), (CID, "-x"), ("", "rmgr")])
def test_person_mount_refused(cid, rid):
    with pytest.raises(ValueError):
        oj.person_mount(cid, rid)


def test_person_role_params_exact():
    assert oj.person_role_params("client-7zjw5jtpx6", "127.0.0.1/32", CID) == {
        "role_type": "jwt",
        "user_claim": "sub",
        "bound_audiences": ["client-7zjw5jtpx6"],
        "claim_mappings": {"name": "name", "auth_provider": "provider"},
        "token_policies": [f"hops-person-{CID}"],
        "token_type": "service",
        "token_ttl": "15m",
        "token_max_ttl": "15m",
        "token_num_uses": 0,
        "token_no_default_policy": False,
        "token_bound_cidrs": ["127.0.0.1/32"],
        "verbose_oidc_logging": False,
    }


@pytest.mark.parametrize("args,kw", [
    (("client", "127.0.0.1/32", CID), {"groups_claim": "groups"}),     # décision 303
    (("", "127.0.0.1/32", CID), {}),
    (("cli ent", "127.0.0.1/32", CID), {}),
    (("client", "127.0.0.1", CID), {}),                                 # CIDR attendu
    (("client", "127.0.0.1/32", "nope"), {}),
])
def test_person_role_params_refused(args, kw):
    with pytest.raises(ValueError):
        oj.person_role_params(*args, **kw)


def test_role_never_carries_groups_claim():
    assert "groups_claim" not in oj.person_role_params("c", "10.0.0.0/24", CID)


def test_mount_config_discovery():
    assert oj.mount_config("https://rancher.example/oidc", ca_pem="-----BEGIN CERTIFICATE-----\nX\n") == {
        "oidc_discovery_url": "https://rancher.example/oidc",
        "oidc_discovery_ca_pem": "-----BEGIN CERTIFICATE-----\nX\n",
        "bound_issuer": "https://rancher.example/oidc",
        "jwt_supported_algs": ["RS256"],
        "default_role": "person"}
    assert "oidc_discovery_ca_pem" not in oj.mount_config("https://rancher.example/oidc/")


def test_mount_config_static_keys():
    pem = "-----BEGIN PUBLIC KEY-----\nAAA\n-----END PUBLIC KEY-----"
    cfg = oj.mount_config("https://rancher.example/oidc", pubkeys=[pem, pem + "\n"])
    assert cfg["jwt_validation_pubkeys"] == [pem, pem]
    assert "oidc_discovery_url" not in cfg and cfg["bound_issuer"] == "https://rancher.example/oidc"


@pytest.mark.parametrize("args,kw", [
    (("https://rancher.example/oidc",), {"ca_pem": "x", "pubkeys": ["-----BEGIN PUBLIC KEY-----"]}),
    (("http://rancher.example/oidc",), {}),
    (("https://rancher.example",), {}),
    (("https://rancher.example/oidc",), {"pubkeys": ["not a pem"]}),
])
def test_mount_config_refused(args, kw):
    with pytest.raises(ValueError):
        oj.mount_config(*args, **kw)


def test_keys_sha256_stable_and_sensitive():
    k1 = {"kid": "key", "kty": "RSA", "n": "abc", "e": "AQAB", "use": "sig"}
    k2 = {"kid": "new", "kty": "RSA", "n": "def", "e": "AQAB"}
    a = oj.keys_sha256({"keys": [k1]})
    assert a == oj.keys_sha256({"keys": [dict(k1, use=None, alg="RS256")]})     # champ décoratif
    assert oj.keys_sha256({"keys": [k1, k2]}) == oj.keys_sha256({"keys": [k2, k1]})   # ordre
    assert oj.keys_sha256({"keys": [k1, k2]}) != a                                     # clé ajoutée
    assert len(a) == 64


@pytest.mark.parametrize("jwks", [None, {}, {"keys": []}, {"keys": "x"}, []])
def test_keys_sha256_refused(jwks):
    with pytest.raises(ValueError):
        oj.keys_sha256(jwks)


def test_useful_claims_and_access_token():
    idt = {"iss": "i", "sub": "u", "aud": ["c"], "exp": 1, "iat": 0, "name": "N", "auth_provider": "local",
           "nonce": "n"}
    acc = dict(idt, token="token-abc:xyz", scope=["openid"])
    assert oj.useful_claims(acc) == {k: v for k, v in idt.items() if k != "nonce"}
    assert oj.is_access_token(acc) and not oj.is_access_token(idt)


def test_settings_of_defaults():
    assert oj.settings_of({}) == {"enabled": False, "servers": {}, "groups_only": False}
    assert oj.settings_of({"person_token": {"enabled": True, "servers": {"a": {"mount": "m"}, "b": "bad"}}}) == {
        "enabled": True, "servers": {"a": {"mount": "m"}}, "groups_only": False}
    assert ss.load_settings("/nonexistent")["person_token"] == {"enabled": False, "servers": {},
                                                                 "groups_only": False}
