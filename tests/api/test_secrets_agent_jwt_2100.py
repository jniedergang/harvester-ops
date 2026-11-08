"""2.10.0 tâches 6 et 7 : opérations `b1-jwt`, `b1-jwt-remove` et `b1-groups`
de l'agent de l'hôte, revue d'un instantané restauré élargie.

Faux OpenBao de mode B1 (fake_openbao_b1.FakeB1) qui APPLIQUE la politique de
l'agent à ses jetons (`enforce_agent`, règles d'OpenBao : la plus précise
décide) : chaque requête de l'agent doit être permise par `agent_rules`, et
rien de plus. Faux hôte : aucun redémarrage d'OpenBao pour poser une méthode
jwt (essai 6). Les chemins de root sont joués sans privilège (bac à sable,
comme les tests de la 2.9.0).

Mutations jouées (rapport des tâches 6 et 7) : « `list` retiré sur
`auth/jwt-hops-*` », « revue qui accepte toute méthode jwt », « relecture du
rôle sautée », « membre de groupe non contrôlé ».
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_secrets_agent_290 import CID, bundle, env, hsa, install, no_secret_in, request  # noqa: E402,F401

import openbao_jwt as oj  # noqa: E402
import openbao_policy as pol  # noqa: E402
from fake_openbao_b1 import TOKEN_ACCESSOR, acl_allows  # noqa: E402

ISS = "https://rancher.example/oidc"
CLIENT = "client-7zjw5jtpx6"
MOUNT = f"jwt-hops-{CID}-p-rmgr"
PEM_A = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----"
PEM_B = "-----BEGIN PUBLIC KEY-----\nBBBB\n-----END PUBLIC KEY-----"
CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
UUID_A = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def b1(env):
    install(env)
    env["srv"].enforce_agent = True
    return env


def jwt_op(env, **kw):
    req = {"op": "b1-jwt", "rancher": "rmgr", "client_id": CLIENT, "issuer": ISS}
    req.update(kw)
    return request(env, **{k: v for k, v in req.items() if v is not None})


def test_b1_jwt_sets_everything_without_restart(b1):
    srv, host = b1["srv"], b1["host"]
    cmds = len(host.cmds)
    rc, st = jwt_op(b1, ca_pem=CA)
    assert rc == 0, st
    assert srv.auth_mounts[MOUNT + "/"]["type"] == "jwt"
    assert srv.jwt_config[MOUNT] == oj.mount_config(ISS, ca_pem=CA)
    assert srv.jwt_roles[MOUNT] == {"person": oj.person_role_params(CLIENT, pol.B1_CIDR, CID)}
    assert srv.policies[pol.person_policy_name(CID)] == pol.person_policy("harvester-ops", f"consoles/{CID}", CID)
    assert host.cmds[cmds:] == []                                 # ni systemctl ni podman : à chaud
    assert srv.agent_denied == []                                 # tout permis par agent_rules
    a = b1["agent"]().agent_state()["person_jwt"]["rmgr"]
    assert a["mount"] == MOUNT and a["keys"] == "discovery" and "by" in a
    no_secret_in(b1, st)


def test_b1_jwt_again_keeps_the_mount_and_updates_static_keys(b1):
    srv = b1["srv"]
    assert jwt_op(b1, pubkeys=[PEM_A])[0] == 0
    enables = [e for e in srv.events if e[0] == "auth-enable"]
    rc, st = jwt_op(b1, pubkeys=[PEM_A, PEM_B])               # rotation : les deux ensemble
    assert rc == 0, st
    assert [e for e in srv.events if e[0] == "auth-enable"] == enables
    assert srv.jwt_config[MOUNT]["jwt_validation_pubkeys"] == [PEM_A, PEM_B]
    st_ = b1["agent"]().agent_state()["person_jwt"]["rmgr"]
    assert st_["keys"] == "static" and st_["pubkeys_seen"] == [PEM_A, PEM_B]
    assert jwt_op(b1, pubkeys=[PEM_B])[0] == 0                 # confirmation : la nouvelle seule
    assert b1["agent"]().agent_state()["person_jwt"]["rmgr"]["pubkeys_seen"] == [PEM_B, PEM_A]


@pytest.mark.parametrize("kw,code", [
    ({"rancher": "../x"}, "bad-request"),
    ({"rancher": "Evil"}, "bad-request"),
    ({"issuer": "http://rancher.example/oidc"}, "bad-request"),
    ({"issuer": "https://rancher.example"}, "bad-request"),
    ({"client_id": "a b"}, "bad-request"),
    ({"pubkeys": ["not a pem"]}, "bad-request"),
    ({"pubkeys": [PEM_A] * 9}, "bad-request"),
    ({"pubkeys": [PEM_A], "ca_pem": CA}, "bad-request"),
    ({"ca_pem": "nope"}, "bad-request"),
    ({"groups_only": "yes"}, "bad-request"),
    ({"mount": "jwt-other"}, "bad-request"),
])
def test_b1_jwt_bad_request(b1, kw, code):
    rc, st = jwt_op(b1, **kw)
    assert rc == 1 and st["code"] == code, st
    assert MOUNT + "/" not in b1["srv"].auth_mounts


def test_b1_jwt_mount_used_by_another_method_refused(b1):
    b1["srv"].auth_mounts[MOUNT + "/"] = {"type": "userpass", "accessor": "auth_userpass_9"}
    rc, st = jwt_op(b1)
    assert rc == 1 and st["code"] == "jwt-mount-taken"
    assert b1["srv"].auth_mounts[MOUNT + "/"]["type"] == "userpass"


def test_b1_jwt_never_touches_an_eso_mount(b1):
    """Revue 6-7 I-3 : un Rancher nommé comme une clé ESO pose son propre
    montage `p-` ; le montage du cluster enfant, son rôle et sa configuration
    restent tels quels, retrait compris."""
    srv = b1["srv"]
    eso = oj.eso_mount(CID, "1d905023")
    srv.auth_mounts[eso + "/"] = {"type": "jwt", "accessor": "auth_jwt_e50e50"}
    srv.jwt_roles[eso] = {"eso": {"token_policies": [f"hops-eso-{CID}-1d905023"]}}
    srv.jwt_config[eso] = {"bound_issuer": "https://kubernetes.default.svc.cluster.local"}
    before = (dict(srv.jwt_roles[eso]), dict(srv.jwt_config[eso]))
    rc, st = jwt_op(b1, rancher="eso-1d905023")
    assert rc == 0, st
    assert oj.person_mount(CID, "eso-1d905023") + "/" in srv.auth_mounts
    assert (srv.jwt_roles[eso], srv.jwt_config[eso]) == before
    assert request(b1, op="b1-jwt-remove", rancher="eso-1d905023")[0] == 0
    assert eso + "/" in srv.auth_mounts and srv.jwt_roles[eso] == before[0]


def test_b1_jwt_foreign_role_removed(b1):
    srv = b1["srv"]
    assert jwt_op(b1)[0] == 0
    srv.jwt_roles[MOUNT]["admin"] = {"token_policies": ["root"]}
    assert jwt_op(b1)[0] == 0
    assert set(srv.jwt_roles[MOUNT]) == {"person"}


def test_b1_jwt_readback_mismatch_undoes_the_new_mount(b1, monkeypatch):
    srv = b1["srv"]
    real = srv._route_jwt_and_groups

    def widen(h, rec, path, body):
        if path == f"auth/{MOUNT}/role/person" and rec["method"] in ("POST", "PUT"):
            body = dict(body, token_policies=body["token_policies"] + ["evil"])
        return real(h, rec, path, body)
    monkeypatch.setattr(srv, "_route_jwt_and_groups", widen)
    rc, st = jwt_op(b1)
    assert rc == 1 and st["code"] == "jwt-readback", st
    assert MOUNT + "/" not in srv.auth_mounts                  # montage neuf retiré
    assert "rmgr" not in (b1["agent"]().agent_state().get("person_jwt") or {})


def test_agent_policy_is_exactly_what_b1_jwt_needs(b1):
    """Sans `list` sur `auth/jwt-hops-<id>-*`, la relecture des rôles est
    refusée (manque relevé à l'essai 6)."""
    srv = b1["srv"]
    text = srv.policies[pol.agent_role(CID)]
    srv.policies[pol.agent_role(CID)] = text.replace(
        f'path "auth/jwt-hops-{CID}-*" {{ capabilities = ["create", "update", "read", "delete", "list"] }}',
        f'path "auth/jwt-hops-{CID}-*" {{ capabilities = ["create", "update", "read", "delete"] }}')
    b1["agent"]().op_b1_policies = lambda *a, **k: None
    rc, st = jwt_op(b1)
    assert rc == 1 and ("LIST", f"auth/{MOUNT}/role") in srv.agent_denied


@pytest.mark.parametrize("path,cap,ok", [
    (f"sys/auth/{MOUNT}", "sudo", True),
    ("sys/auth/userpass", "update", False),
    (f"sys/auth/jwt-hops-000000000000-p-rmgr", "update", False),
    (f"auth/{MOUNT}/role", "list", True),
    (f"sys/policies/acl/hops-person-{CID}", "update", True),
    (f"sys/policies/acl/harvester-ops-{CID}", "update", False),
    (f"sys/policies/acl/hops-group-{CID}-ops", "update", True),
    (f"sys/policies/acl/hops-eso-{CID}-1d905023", "update", True),
    (f"identity/group/name/hops-{CID}-ops", "update", True),
    ("identity/group/name/evil", "update", False),
    ("identity/group", "update", False),                  # création par le corps : non couverte
    ("identity/group-alias", "update", False),            # décision 303 A
    ("identity/group/id/abc", "update", True),
])
def test_agent_rules_paths(path, cap, ok):
    assert acl_allows(pol.agent_policy(CID), path, cap) is ok


def test_b1_jwt_remove_keeps_the_policy_while_another_rancher_remains(b1):
    srv = b1["srv"]
    assert jwt_op(b1)[0] == 0
    assert jwt_op(b1, rancher="lab2")[0] == 0
    rc, st = request(b1, op="b1-jwt-remove", rancher="rmgr")
    assert rc == 0, st
    assert MOUNT + "/" not in srv.auth_mounts and pol.person_policy_name(CID) in srv.policies
    rc, st = request(b1, op="b1-jwt-remove", rancher="lab2")
    assert rc == 0 and pol.person_policy_name(CID) not in srv.policies
    assert not b1["agent"]().agent_state()["person_jwt"]
    assert srv.agent_denied == []


def _person_entity(srv, eid=UUID_A, policies=None, accessor=None):
    acc = accessor or srv.auth_mounts[MOUNT + "/"]["accessor"]
    srv.entities[eid] = {"name": f"entity_{eid[:8]}", "aliases": {(acc, "user-kk67j")}}
    if policies:
        srv.entity_policies[eid] = policies


def test_b1_groups_set_and_remove(b1):
    srv = b1["srv"]
    assert jwt_op(b1)[0] == 0
    _person_entity(srv)
    rc, st = request(b1, op="b1-groups", name="ops", classes=["ssh-kvm", "kubeconfig"], clusters=["node2"],
                     members=[UUID_A])
    assert rc == 0, st
    g = next(g for g in srv.group_docs.values() if g["name"] == f"hops-{CID}-ops")
    assert g["policies"] == [f"hops-group-{CID}-ops"] and g["member_entity_ids"] == [UUID_A]
    assert srv.policies[f"hops-group-{CID}-ops"] == pol.group_policy(
        "harvester-ops", f"consoles/{CID}", CID, "ops", ["ssh-kvm", "kubeconfig"], ["node2"])
    rc, st = request(b1, op="b1-groups", name="ops", remove=True)
    assert rc == 0 and not srv.group_docs and f"hops-group-{CID}-ops" not in srv.policies
    assert srv.agent_denied == []


@pytest.mark.parametrize("case,code", [
    ("unknown", "group-member-unknown"),
    ("foreign-alias", "group-member-refused"),
    ("with-policies", "group-member-refused"),
    ("bad-id", "bad-request"),
    ("bad-class", "bad-request"),
    ("glob-cluster", "bad-request"),
    ("bad-name", "bad-request"),
])
def test_b1_groups_refusals(b1, case, code):
    srv = b1["srv"]
    assert jwt_op(b1)[0] == 0
    req = {"name": "ops", "classes": ["ssh-kvm"], "clusters": ["node2"], "members": [UUID_A]}
    if case == "foreign-alias":
        _person_entity(srv, accessor=TOKEN_ACCESSOR)
    elif case == "with-policies":
        _person_entity(srv, policies=["hops-agent-" + CID])
    elif case == "bad-id":
        req["members"] = ["not-a-uuid"]
    elif case == "bad-class":
        req["classes"] = ["rancher-oidc"]
    elif case == "glob-cluster":
        req["clusters"] = ["*"]
    elif case == "bad-name":
        req["name"] = "Ops/../x"
    if case in ("bad-id", "bad-class", "glob-cluster", "bad-name"):
        _person_entity(srv)
    rc, st = request(b1, op="b1-groups", **req)
    assert rc == 1 and st["code"] == code, st
    assert not srv.group_docs


def test_b1_groups_only_rewrites_the_person_policy(b1):
    srv = b1["srv"]
    assert jwt_op(b1)[0] == 0
    rc, st = request(b1, op="b1-groups", groups_only=True)
    assert rc == 0, st
    assert srv.policies[pol.person_policy_name(CID)] == pol.person_policy("harvester-ops", f"consoles/{CID}", CID,
                                                                           groups_only=True)
    rc, st = request(b1, op="b1-groups", groups_only=True, name="x", classes=["ssh-kvm"], clusters=["a"])
    assert rc == 1 and st["code"] == "bad-request"


def test_b1_policies_refreshes_person_and_group_policies(b1):
    srv = b1["srv"]
    assert jwt_op(b1)[0] == 0
    _person_entity(srv)
    assert request(b1, op="b1-groups", name="ops", classes=["ssh-kvm"], clusters=["node2"], members=[])[0] == 0
    srv.policies[pol.person_policy_name(CID)] = "old"
    srv.policies[f"hops-group-{CID}-ops"] = "old"
    rc, st = request(b1, op="b1-policies")
    assert rc == 0, st
    assert srv.policies[pol.person_policy_name(CID)] != "old" and srv.policies[f"hops-group-{CID}-ops"] != "old"


# ---------------------------------------------------------------------------
# Revue d'un instantané restauré (élargie)
# ---------------------------------------------------------------------------
def _snap(env):
    d = env["p"].b1_data / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "openbao-20261108T020000Z.snap"
    f.write_bytes(b"SNAP" * 8)
    return f


def _restore_with(env, craft):
    srv = env["srv"]

    class Crafted(list):
        def append(self, raw):
            super().append(raw)
            craft(srv)
    srv.restored = Crafted()
    return request(env, op="restore-snapshot", file=_snap(env).name)


def _configured(b1):
    srv = b1["srv"]
    assert jwt_op(b1, ca_pem=CA)[0] == 0
    _person_entity(srv)
    assert request(b1, op="b1-groups", name="ops", classes=["ssh-kvm"], clusters=["node2"], members=[UUID_A])[0] == 0
    srv.enforce_agent = False
    return srv


def test_a_genuine_snapshot_with_jwt_groups_and_entities_passes(b1):
    srv = _configured(b1)
    srv.add_token(policies=("default", pol.person_policy_name(CID)))      # jeton de personne de l'instantané
    rc, st = _restore_with(b1, lambda s: None)
    assert rc == 0, st


def test_an_older_snapshot_without_the_mount_passes_and_prunes_the_state(b1):
    srv = _configured(b1)

    def older(s):
        s.auth_mounts.pop(MOUNT + "/")
        s.jwt_config.clear()
        s.jwt_roles.clear()
        s.group_docs.clear()
        s.groups.clear()
        s.policies.pop(f"hops-group-{CID}-ops", None)
        s.entities.pop(UUID_A, None)
    rc, st = _restore_with(b1, older)
    assert rc == 0, st
    a = b1["agent"]().agent_state()
    assert a["person_jwt"] == {} and a["person_groups"] == {}
    assert [x for x in st["steps"] if x["id"] == "person-state"]


def _role(s):
    return s.jwt_roles[MOUNT]["person"]


CRAFTS = {
    "jwt-mount-unknown": lambda s: s.auth_mounts.__setitem__(f"jwt-hops-{CID}-evil/",
                                                            {"type": "jwt", "accessor": "auth_jwt_ev"}),
    "userpass": lambda s: s.auth_mounts.__setitem__("userpass/", {"type": "userpass", "accessor": "auth_up"}),
    "jwt-extra-role": lambda s: s.jwt_roles[MOUNT].__setitem__("admin", {"token_policies": ["root"]}),
    "jwt-role-policies": lambda s: _role(s).__setitem__("token_policies", ["hops-person-" + CID, "evil"]),
    "jwt-role-cidr": lambda s: _role(s).__setitem__("token_bound_cidrs", ["0.0.0.0/0"]),
    "jwt-role-template-claims": lambda s: _role(s).__setitem__("token_policies_template_claims", True),
    "jwt-role-ttl": lambda s: _role(s).__setitem__("token_max_ttl", "768h"),
    "jwt-role-audience": lambda s: _role(s).__setitem__("bound_audiences", ["other"]),
    "jwt-config-issuer": lambda s: s.jwt_config[MOUNT].update(bound_issuer="https://evil/oidc",
                                                              oidc_discovery_url="https://evil/oidc"),
    "jwt-config-ca": lambda s: s.jwt_config[MOUNT].update(oidc_discovery_ca_pem="-----BEGIN CERTIFICATE-----\nX"),
    "jwt-config-keys": lambda s: s.jwt_config[MOUNT].update(oidc_discovery_url="", jwt_validation_pubkeys=[PEM_B]),
    "jwt-config-jwks-url": lambda s: s.jwt_config[MOUNT].update(jwks_url="https://evil/jwks"),
    "group-policy-widened": lambda s: s.policies.__setitem__(f"hops-group-{CID}-ops", 'path "*" { capabilities = ["sudo"] }\n'),
    "person-policy-widened": lambda s: s.policies.__setitem__(pol.person_policy_name(CID),
                                                              'path "*" { capabilities = ["read"] }\n'),
    "group-extra-policy": lambda s: next(iter(s.group_docs.values()))["policies"].append(pol.agent_role(CID)),
    "group-out-of-prefix": lambda s: (s.group_docs.__setitem__("grp-evil", {"id": "grp-evil", "name": "evil",
                                                                          "type": "internal", "policies": [],
                                                                          "member_entity_ids": [], "alias": {}}),
                                      s.groups.append("grp-evil")),
    "group-external": lambda s: next(iter(s.group_docs.values())).update(type="external"),
    "entity-on-jwt-with-policy": lambda s: s.entity_policies.__setitem__(UUID_A, [pol.agent_role(CID)]),
}


@pytest.mark.parametrize("craft", sorted(CRAFTS))
def test_a_crafted_snapshot_with_jwt_objects_is_refused(b1, craft):
    _configured(b1)
    marker = b1["p"].b1_data / "data" / "raft.db"
    marker.write_text("live")
    rc, st = _restore_with(b1, CRAFTS[craft])
    assert rc == 1 and st["code"] == "snapshot-untrusted", st
    assert marker.read_text() == "live"
