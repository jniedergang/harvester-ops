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
      "person_token": {"enabled": True, "servers": {"rmgr": {"mount": f"jwt-hops-{CID}-p-rmgr"},
                                                     "config": {"mount": f"jwt-hops-{CID}-p-config"}}}}


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
        assert idt.mount == (f"jwt-hops-{CID}-p-config" if session.server_id is None else f"jwt-hops-{CID}-p-rmgr")


def test_who_acts_local_actor():
    assert oj.identity_for("ssh-kvm", None, ON, actor="admin") == ss.Identity("service", "admin")


def test_person_classes_match_table():
    assert set(sc.PERSON) == PERSON


@pytest.mark.parametrize("rid,mount", [
    ("rmgr", f"jwt-hops-{CID}-p-rmgr"),
    ("abcdefghijkl", f"jwt-hops-{CID}-p-abcdefghijkl"),          # 12 : gardé
])
def test_person_mount(rid, mount):
    assert oj.person_mount(CID, rid) == mount


@pytest.mark.parametrize("rid", ["eso-1d905023", "eso-1d905023ab", "eso-abcdef0123456", "eso-x",
                                 "p-eso-1d905023"])
def test_person_and_eso_mounts_never_collide(rid):
    """Revue 6-7 I-3 (décision 314 A) : un Rancher nommé comme une clé ESO,
    tronqué ou non, ne donne jamais le montage d'un cluster enfant ; les deux
    familles se reconnaissent à leur préfixe."""
    m = oj.person_mount(CID, rid)
    assert m.startswith(f"jwt-hops-{CID}-p-")
    assert not m.startswith(f"jwt-hops-{CID}-eso-")
    assert oj.is_person_mount(CID, m) and not oj.is_eso_mount(CID, m)
    e = oj.eso_mount(CID, "1d905023")
    assert e != m and oj.is_eso_mount(CID, e) and not oj.is_person_mount(CID, e)


def test_person_mount_long_ids_are_distinct():
    a, b = oj.person_mount(CID, "rancher-prod-paris"), oj.person_mount(CID, "rancher-prod-lyon")
    assert a != b
    for m in (a, b):
        tail = m[len(f"jwt-hops-{CID}-p-"):]
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


# ---------------------------------------------------------------------------
# Tâche 7 : clés statiques tirées du JWKS de Rancher
# ---------------------------------------------------------------------------
def _b64i(i):
    import base64
    return base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


def test_jwk_to_pem_equals_openssl(tmp_path):
    """La conversion est celle d'openssl (clé RSA 2048 tirée pour l'essai)."""
    import subprocess
    key, pub = tmp_path / "k.pem", tmp_path / "k.pub"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    mod = subprocess.run(["openssl", "rsa", "-in", str(key), "-noout", "-modulus"], check=True,
                         capture_output=True, text=True).stdout.strip().split("=")[1]
    jwk = {"kty": "RSA", "kid": "key", "use": "sig", "n": _b64i(int(mod, 16)), "e": "AQAB"}
    assert oj.jwk_to_pem(jwk).strip() == pub.read_text().strip()


@pytest.mark.parametrize("jwk", [
    {"kty": "EC", "crv": "P-256", "x": "AA", "y": "BB"},
    {"kty": "RSA", "n": _b64i((1 << 1023) + 1), "e": "AQAB"},               # 1024 bits
    {"kty": "RSA", "e": "AQAB"},
    {"kty": "RSA", "n": "!!", "e": "AQAB"},
    {"kty": "RSA", "use": "enc", "n": _b64i((1 << 2047) + 1), "e": "AQAB"},
    "not a dict",
])
def test_jwk_to_pem_refusals(jwk):
    with pytest.raises(ValueError):
        oj.jwk_to_pem(jwk)


def test_pubkeys_of_order_and_bounds():
    k1 = {"kty": "RSA", "kid": "b", "n": _b64i((1 << 2047) + 3), "e": "AQAB"}
    k2 = {"kty": "RSA", "kid": "a", "n": _b64i((1 << 2047) + 5), "e": "AQAB"}
    assert oj.pubkeys_of({"keys": [k1, k2]}) == [oj.jwk_to_pem(k2), oj.jwk_to_pem(k1)]
    with pytest.raises(ValueError):
        oj.pubkeys_of({"keys": [k1] * 9})
    with pytest.raises(ValueError):
        oj.pubkeys_of({"keys": []})


@pytest.mark.parametrize("old,new,want", [
    (["A"], ["B"], ["B", "A"]),
    (["A", "B"], ["B"], ["B", "A"]),
    (None, ["A"], ["A"]),
    (["A"], ["A"], ["A"]),
])
def test_merge_pubkeys(old, new, want):
    assert oj.merge_pubkeys(old, new) == want


@pytest.mark.parametrize("doc,ok", [
    ({"issuer": "https://r/oidc", "jwks_uri": "https://r/oidc/.well-known/jwks.json",
      "id_token_signing_alg_values_supported": ["RS256"]}, [True, True, True]),
    ({"issuer": "https://other/oidc", "jwks_uri": "x", "id_token_signing_alg_values_supported": ["RS256"]},
     [False, True, True]),
    ({"issuer": "https://r/oidc/", "id_token_signing_alg_values_supported": ["ES256"]}, [True, False, False]),
    ("not a dict", [False, False, False]),
])
def test_check_discovery(doc, ok):
    assert [o for _r, o in oj.check_discovery(doc, "https://r/oidc")] == ok


def test_fetch_json_https_only_and_objects_only():
    with pytest.raises(ValueError):
        oj.fetch_json("http://r/oidc/.well-known/jwks.json")

    class R:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return self.body

    class Opener:
        def __init__(self, body):
            self.body = body

        def open(self, req, timeout=None):
            return R(self.body)
    assert oj.fetch_json("https://r/x", opener=Opener(b'{"keys": []}')) == {"keys": []}
    for bad in (b"[1]", b"not json"):
        with pytest.raises(ValueError):
            oj.fetch_json("https://r/x", opener=Opener(bad))


# ---------------------------------------------------------------------------
# 2.10.0 (tâche 9) : méthode jwt d'un cluster enfant (ESO)
# ---------------------------------------------------------------------------
PEM = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----"


def test_eso_role_params_exact():
    """Rôle de l'essai 7 et de la décision 304 B (namespaces activés)."""
    assert oj.eso_role_params(CID, "1d905023", ["team-b", "team-a", "team-a"], "172.16.2.0/24") == {
        "role_type": "jwt", "user_claim": "sub", "bound_audiences": [f"hops-openbao-{CID}"],
        "bound_claims_type": "glob",
        "bound_claims": {"/kubernetes.io/serviceaccount/name": "hops-eso", "/kubernetes.io/namespace": ["team-a", "team-b"]},
        "claim_mappings": {"/kubernetes.io/namespace": "namespace"},
        "token_policies": [f"hops-eso-{CID}-1d905023"], "token_type": "service", "token_ttl": "10m",
        "token_max_ttl": "10m", "token_num_uses": 0, "token_no_default_policy": False,
        "verbose_oidc_logging": False, "token_bound_cidrs": ["172.16.2.0/24"]}
    assert "token_bound_cidrs" not in oj.eso_role_params(CID, "1d905023", ["a"])


@pytest.mark.parametrize("args", [
    (CID, "1d905023", []), (CID, "1d905023", ["Team_A"]), (CID, "xyz", ["a"]), ("ABC", "1d905023", ["a"]),
    (CID, "1d905023", ["a"], "nope"),
])
def test_eso_role_params_refused(args):
    with pytest.raises(ValueError):
        oj.eso_role_params(*args)


def test_eso_mount_config_static_keys_only():
    c = oj.eso_mount_config("https://kubernetes.default.svc.cluster.local/", [PEM])
    assert c == {"bound_issuer": "https://kubernetes.default.svc.cluster.local", "jwt_validation_pubkeys": [PEM],
                 "jwt_supported_algs": ["RS256"], "default_role": "eso"}
    for bad in (("http://k8s", [PEM]), ("https://k8s", []), ("https://k8s", ["nope"]), ("https://k8s", [PEM] * 9)):
        with pytest.raises(ValueError):
            oj.eso_mount_config(*bad)


def test_eso_settings_of():
    assert oj.eso_settings_of({}) == {"listener": None, "address": None, "clusters": {}}
    got = oj.eso_settings_of({"eso": {"listener": "1.2.3.4:8212", "clusters": {"1d905023": {"mount": "m"},
                                                                               "bad": {"mount": "x"}}}})
    assert got["listener"] == "1.2.3.4:8212" and list(got["clusters"]) == ["1d905023"]


# Revue 8-10 I-3 : rôle lié aussi aux UID des comptes hops-eso
U1, U2 = "11111111-2222-3333-4444-555555555555", "66666666-7777-4888-9999-000000000000"


@pytest.mark.parametrize("uids,bound", [
    (None, None), ({}, None),
    ({"team-a": U1}, [U1]),                                      # team-b attend la relecture de son compte
    ({"team-b": U2, "team-a": U1}, sorted([U1, U2])),
])
def test_eso_role_bound_to_account_uids(uids, bound):
    bc = oj.eso_role_params(CID, "1d905023", ["team-a", "team-b"], sa_uids=uids)["bound_claims"]
    assert bc.get("/kubernetes.io/serviceaccount/uid") == bound
    assert bc["/kubernetes.io/namespace"] == ["team-a", "team-b"]


@pytest.mark.parametrize("uids", [{"team-c": U1}, {"team-a": "not-a-uid"}, {"team-a": "ABCDEF01-2222-3333-4444-555555555555"}, {"team-a": 3}])
def test_eso_account_uids_refused(uids):
    with pytest.raises(ValueError):
        oj.eso_role_params(CID, "1d905023", ["team-a", "team-b"], sa_uids=uids)
