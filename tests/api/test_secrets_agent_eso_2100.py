"""2.10.0 (tâche 9) : opérations ESO de l'agent de l'hôte en B1.

`b1-eso-listener` (second écouteur TLS sur une adresse de l'hôte, CA dédiée,
point 298), `b1-eso-cluster` (méthode `jwt` d'un cluster enfant, clés de ses
comptes de service, rôle `eso` aux namespaces activés, politique à gabarit,
point 295 et décision 304 B), `b1-eso-cluster-remove`, et la revue d'un
instantané restauré qui porte ces objets. Faux B1 qui applique la politique
de l'agent (`enforce_agent`)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_secrets_agent_290 import CID, bundle, env, hsa, install, no_secret_in, request  # noqa: E402,F401
from test_secrets_agent_jwt_2100 import _restore_with  # noqa: E402

import openbao_jwt as oj  # noqa: E402
import openbao_policy as pol  # noqa: E402

KEY = "1d905023"
MOUNT = f"jwt-hops-{CID}-eso-{KEY}"
ISS = "https://kubernetes.default.svc.cluster.local"
PEM_A = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----"
PEM_B = "-----BEGIN PUBLIC KEY-----\nBBBB\n-----END PUBLIC KEY-----"
LISTEN = "172.16.1.11:8212"
PREFIX = f"consoles/{CID}"


@pytest.fixture
def b1(env):
    install(env)
    env["srv"].enforce_agent = True
    return env


def listener(env, address=LISTEN):
    return request(env, op="b1-eso-listener", address=address)


def cluster(env, **kw):
    req = {"op": "b1-eso-cluster", "key": KEY, "harvester": "harvlab2", "namespace": "c210", "cluster": "c210",
           "issuer": ISS, "pubkeys": [PEM_A], "namespaces": ["team-a", "team-b"], "node_cidr": "172.16.2.0/24"}
    req.update(kw)
    return request(env, **{k: v for k, v in req.items() if v is not None})


def config_text(env):
    return (env["p"].b1_etc / "openbao.hcl").read_text()


# ---------------------------------------------------------------------------
# Écouteur
# ---------------------------------------------------------------------------
def test_listener_on_then_off(b1):
    host, p = b1["host"], b1["p"]
    rc, st = listener(b1)
    assert rc == 0, st
    text = config_text(b1)
    assert 'address       = "172.16.1.11:8212"' in text and pol.C_ESO_CERT in text
    assert text.count('listener "tcp"') == 2 and '127.0.0.1:8210' in text
    assert (p.b1_etc / "tls" / "eso.pem").is_file() and (p.b1_etc / "tls" / "eso.key").stat().st_mode & 0o777 == 0o400
    ca = (p.etc / "eso-ca.pem").read_text()
    assert "BEGIN CERTIFICATE" in ca and st["info"]["eso_ca_pem"] == ca
    assert ["systemctl", "restart", hsa.UNIT] in host.cmds
    assert b1["agent"]().agent_state()["eso_listener"] == LISTEN
    import subprocess
    out = subprocess.run(["openssl", "x509", "-in", str(p.b1_etc / "tls" / "eso.pem"), "-noout", "-ext",
                          "subjectAltName"], capture_output=True, text=True).stdout
    assert "IP Address:172.16.1.11" in out
    no_secret_in(b1, st)
    rc, st = listener(b1, None)
    assert rc == 0, st
    assert config_text(b1).count('listener "tcp"') == 1
    assert not (p.b1_etc / "tls" / "eso.pem").exists() and not (p.etc / "eso-ca.pem").exists()
    assert b1["agent"]().agent_state()["eso_listener"] is None


@pytest.mark.parametrize("address,code", [
    ("127.0.0.1:8212", "bad-request"),
    ("0.0.0.0:8212", "bad-request"),
    ("172.16.1.11:8210", "bad-request"),
    ("172.16.1.11:80", "bad-request"),
    ("172.16.1.11", "bad-request"),
    ("evil.example:8212", "bad-request"),
    ("172.16.9.9:8212", "eso-listener-not-local"),
])
def test_listener_address_refused(b1, address, code):
    """Mutation « adresse non locale admise »."""
    before = config_text(b1)
    rc, st = listener(b1, address)
    assert rc == 1 and st["code"] == code, st
    assert config_text(b1) == before


def test_listener_off_refused_while_a_cluster_uses_it(b1):
    assert listener(b1)[0] == 0
    assert cluster(b1)[0] == 0
    rc, st = listener(b1, None)
    assert rc == 1 and st["code"] == "eso-listener-in-use"
    assert b1["agent"]().agent_state()["eso_listener"] == LISTEN


def test_listener_takes_the_new_template_of_an_older_install(b1):
    """Modèle posé par un paquet d'avant 2.10.0 (sans `__ESO_LISTENER__`) :
    celui d'install.sh (/opt, root) le remplace ; sans lui, refus clair."""
    p = b1["p"]
    tpl = (p.b1_etc / "openbao.hcl.tpl").read_text()
    old = tpl.replace("__ESO_LISTENER__\n", "")
    (p.b1_etc / "openbao.hcl.tpl").write_text(old)
    rc, st = listener(b1)
    assert rc == 1 and st["code"] == "eso-listener-template"
    (p.unit_src.parent / "openbao.hcl.tpl").write_text(tpl)
    rc, st = listener(b1)
    assert rc == 0, st
    assert "__ESO_LISTENER__" in (p.b1_etc / "openbao.hcl.tpl").read_text()


def test_listener_restart_failure_puts_the_previous_config_back(b1):
    host = b1["host"]
    before = config_text(b1)
    host.fail["systemctl restart"] = 1
    rc, st = listener(b1)
    assert rc == 1
    assert config_text(b1) == before
    assert b1["agent"]().agent_state().get("eso_listener") is None


# ---------------------------------------------------------------------------
# Méthode jwt d'un cluster enfant
# ---------------------------------------------------------------------------
def test_cluster_needs_the_listener(b1):
    rc, st = cluster(b1)
    assert rc == 1 and st["code"] == "eso-listener-off"
    assert MOUNT + "/" not in b1["srv"].auth_mounts


def test_cluster_sets_mount_policy_config_and_role(b1):
    srv = b1["srv"]
    assert listener(b1)[0] == 0
    rc, st = cluster(b1)
    assert rc == 0, st
    acc = srv.auth_mounts[MOUNT + "/"]["accessor"]
    assert srv.jwt_config[MOUNT] == oj.eso_mount_config(ISS, [PEM_A])
    assert srv.jwt_roles[MOUNT] == {"eso": dict(oj.eso_role_params(CID, KEY, ["team-a", "team-b"], "172.16.2.0/24"),
                                                **hsa.ROLE_RESET)}
    assert srv.policies[pol.eso_policy_name(CID, KEY)] == pol.eso_policy("harvester-ops", PREFIX, CID, KEY, acc)
    assert srv.agent_denied == []
    e = b1["agent"]().agent_state()["eso_clusters"][KEY]
    assert e["mount"] == MOUNT and e["namespaces"] == ["team-a", "team-b"] and e["pubkeys"] == [PEM_A]
    assert st["info"]["eso_listener"] == LISTEN and "BEGIN CERTIFICATE" in st["info"]["eso_ca_pem"]


def test_cluster_again_rewrites_namespaces_and_keys_on_the_same_mount(b1):
    srv = b1["srv"]
    assert listener(b1)[0] == 0
    assert cluster(b1)[0] == 0
    enables = [e for e in srv.events if e[0] == "auth-enable"]
    rc, st = cluster(b1, namespaces=["team-a"], pubkeys=[PEM_A, PEM_B])
    assert rc == 0, st
    assert [e for e in srv.events if e[0] == "auth-enable"] == enables
    assert srv.jwt_roles[MOUNT]["eso"]["bound_claims"]["/kubernetes.io/namespace"] == ["team-a"]
    assert srv.jwt_config[MOUNT]["jwt_validation_pubkeys"] == [PEM_A, PEM_B]


@pytest.mark.parametrize("kw", [
    {"key": "1d905024"},                     # pas la clé de ce cluster
    {"key": "rmgr"},
    {"harvester": "../x"},
    {"pubkeys": []},
    {"pubkeys": ["not a pem"]},
    {"issuer": "http://k8s"},
    {"namespaces": []},
    {"namespaces": ["Team_A"]},
    {"node_cidr": "0.0.0.0/0/1"},
    {"mount": "jwt-other"},
])
def test_cluster_bad_request(b1, kw):
    assert listener(b1)[0] == 0
    rc, st = cluster(b1, **kw)
    assert rc == 1 and st["code"] == "bad-request", st
    assert MOUNT + "/" not in b1["srv"].auth_mounts


def test_cluster_readback_mismatch_undoes_the_new_mount(b1, monkeypatch):
    srv = b1["srv"]
    assert listener(b1)[0] == 0
    real = srv._route_jwt_and_groups

    def widen(h, rec, path, body):
        if path == f"auth/{MOUNT}/role/eso" and rec["method"] in ("POST", "PUT"):
            bc = dict(body["bound_claims"], **{"/kubernetes.io/namespace": ["*"]})
            body = dict(body, bound_claims=bc)
        return real(h, rec, path, body)
    monkeypatch.setattr(srv, "_route_jwt_and_groups", widen)
    rc, st = cluster(b1)
    assert rc == 1 and st["code"] == "jwt-readback", st
    assert MOUNT + "/" not in srv.auth_mounts
    assert KEY not in (b1["agent"]().agent_state().get("eso_clusters") or {})


def test_cluster_foreign_role_removed(b1):
    srv = b1["srv"]
    assert listener(b1)[0] == 0
    assert cluster(b1)[0] == 0
    srv.jwt_roles[MOUNT]["admin"] = {"token_policies": ["root"]}
    assert cluster(b1)[0] == 0
    assert set(srv.jwt_roles[MOUNT]) == {"eso"}


def test_cluster_remove(b1):
    srv = b1["srv"]
    assert listener(b1)[0] == 0
    assert cluster(b1)[0] == 0
    rc, st = request(b1, op="b1-eso-cluster-remove", key=KEY)
    assert rc == 0, st
    assert MOUNT + "/" not in srv.auth_mounts and pol.eso_policy_name(CID, KEY) not in srv.policies
    assert b1["agent"]().agent_state()["eso_clusters"] == {} and st["info"]["left"] == 0
    assert request(b1, op="b1-eso-cluster-remove", key=KEY)[0] == 0          # déjà parti : sans erreur
    assert request(b1, op="b1-eso-cluster-remove", key="zz")[1]["code"] == "bad-request"


def test_eso_and_person_gestures_never_cross(b1):
    """Revue 6-7 I-3 : un geste de personne ne touche pas un montage ESO."""
    srv = b1["srv"]
    assert listener(b1)[0] == 0
    assert cluster(b1)[0] == 0
    role = dict(srv.jwt_roles[MOUNT])
    assert request(b1, op="b1-jwt", rancher=f"eso-{KEY}", client_id="c", issuer="https://r.example/oidc")[0] == 0
    assert request(b1, op="b1-jwt-remove", rancher=f"eso-{KEY}")[0] == 0
    assert srv.jwt_roles[MOUNT] == role and MOUNT + "/" in srv.auth_mounts


def test_b1_policies_refreshes_the_eso_policy(b1):
    srv = b1["srv"]
    assert listener(b1)[0] == 0
    assert cluster(b1)[0] == 0
    srv.policies[pol.eso_policy_name(CID, KEY)] = "old"
    assert request(b1, op="b1-policies")[0] == 0
    assert srv.policies[pol.eso_policy_name(CID, KEY)] != "old"


def test_agent_policy_covers_the_eso_operations(b1):
    assert listener(b1)[0] == 0
    assert cluster(b1)[0] == 0
    assert request(b1, op="b1-eso-cluster-remove", key=KEY)[0] == 0
    assert b1["srv"].agent_denied == []


# ---------------------------------------------------------------------------
# Revue d'un instantané restauré
# ---------------------------------------------------------------------------
def _configured(b1):
    srv = b1["srv"]
    assert listener(b1)[0] == 0
    assert cluster(b1)[0] == 0
    acc = srv.auth_mounts[MOUNT + "/"]["accessor"]
    srv.entities["e5000000-0000-0000-0000-000000000000"] = {"name": "entity_e5", "aliases": {
        (acc, "system:serviceaccount:team-a:hops-eso")}}
    srv.enforce_agent = False
    return srv


def test_a_genuine_snapshot_with_an_eso_mount_passes(b1):
    srv = _configured(b1)
    srv.add_token(policies=("default", pol.eso_policy_name(CID, KEY)))   # jeton d'ESO de l'instantané
    rc, st = _restore_with(b1, lambda s: None)
    assert rc == 0, st
    assert "listener" in config_text(b1) and LISTEN in config_text(b1)


def test_an_older_snapshot_without_the_eso_mount_prunes_the_state(b1):
    _configured(b1)

    def older(s):
        s.auth_mounts.pop(MOUNT + "/")
        s.jwt_config.pop(MOUNT, None)
        s.jwt_roles.pop(MOUNT, None)
        s.policies.pop(pol.eso_policy_name(CID, KEY), None)
        s.entities.pop("e5000000-0000-0000-0000-000000000000", None)
    rc, st = _restore_with(b1, older)
    assert rc == 0, st
    assert b1["agent"]().agent_state()["eso_clusters"] == {}


def _eso_role(s):
    return s.jwt_roles[MOUNT]["eso"]


ESO_CRAFTS = {
    "eso-role-namespaces": lambda s: _eso_role(s)["bound_claims"].__setitem__("/kubernetes.io/namespace", ["*"]),
    "eso-role-account": lambda s: _eso_role(s)["bound_claims"].__setitem__("/kubernetes.io/serviceaccount/name", "*"),
    "eso-role-policies": lambda s: _eso_role(s).__setitem__("token_policies", [pol.agent_role(CID)]),
    "eso-extra-role": lambda s: s.jwt_roles[MOUNT].__setitem__("admin", {"token_policies": ["root"]}),
    "eso-config-keys": lambda s: s.jwt_config[MOUNT].__setitem__("jwt_validation_pubkeys", [PEM_B]),
    "eso-policy-widened": lambda s: s.policies.__setitem__(pol.eso_policy_name(CID, KEY),
                                                           'path "harvester-ops/*" { capabilities = ["read"] }\n'),
    "eso-entity-mixed": lambda s: s.entities["e5000000-0000-0000-0000-000000000000"]["aliases"].add(
        (s.auth_mounts["token/"]["accessor"], "tf-decl1")),
    "eso-mount-unknown": lambda s: s.auth_mounts.__setitem__(f"jwt-hops-{CID}-eso-00000000/",
                                                             {"type": "jwt", "accessor": "auth_jwt_x0"}),
}


@pytest.mark.parametrize("craft", sorted(ESO_CRAFTS))
def test_a_crafted_snapshot_with_eso_objects_is_refused(b1, craft):
    _configured(b1)
    marker = b1["p"].b1_data / "data" / "raft.db"
    marker.write_text("live")
    rc, st = _restore_with(b1, ESO_CRAFTS[craft])
    assert rc == 1 and st["code"] == "snapshot-untrusted", st
    assert marker.read_text() == "live"


def test_status_and_logs_carry_no_value(b1):
    assert listener(b1)[0] == 0
    rc, st = cluster(b1)
    assert rc == 0
    blob = json.dumps(st)
    assert "BEGIN PRIVATE" not in blob and "BEGIN EC PRIVATE" not in blob
    no_secret_in(b1, st)


# ---------------------------------------------------------------------------
# Revue 8-10 I-3 : rôle lié aux UID des comptes hops-eso, relu et revu
# ---------------------------------------------------------------------------
UID_A = "11111111-2222-3333-4444-555555555555"


def test_cluster_role_bound_to_account_uids(b1):
    srv = b1["srv"]
    assert listener(b1)[0] == 0
    rc, st = cluster(b1, sa_uids={"team-a": UID_A})
    assert rc == 0, st
    role = srv.jwt_roles[MOUNT]["eso"]
    assert role["bound_claims"]["/kubernetes.io/serviceaccount/uid"] == [UID_A]
    assert b1["agent"]().agent_state()["eso_clusters"][KEY]["sa_uids"] == {"team-a": UID_A}
    rc, st = cluster(b1, sa_uids={"team-z": UID_A})
    assert rc != 0 and st["code"] == "bad-request"
    rc, st = cluster(b1, sa_uids=["x"])
    assert rc != 0 and st["code"] == "bad-request"


def test_cluster_readback_sees_a_role_without_the_uid_binding(b1, monkeypatch):
    assert listener(b1)[0] == 0
    a = b1["agent"]()
    want = oj.eso_role_params(CID, KEY, ["team-a", "team-b"], "172.16.2.0/24", {"team-a": UID_A})
    got = dict(want, bound_claims={k: v for k, v in want["bound_claims"].items()
                                   if k != "/kubernetes.io/serviceaccount/uid"})
    assert "bound_claims" in a._eso_role_diff(got, want)
    assert a._eso_role_diff(want, want) == []
