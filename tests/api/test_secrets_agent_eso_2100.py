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


# ---------------------------------------------------------------------------
# Revue 8-10 I-4 (et point 3 des tâches 8-10) : jamais un modèle qui perd
# l'écouteur ESO ; rendu d'essai avant le premier geste
# ---------------------------------------------------------------------------
def _old_template(b1, keep_opt=False):
    """Le modèle posé est d'avant 2.10.0 (sans `__ESO_LISTENER__`) ; celui
    d'install.sh (/opt) aussi, sauf `keep_opt`."""
    p = b1["p"]
    tpl = (p.b1_etc / "openbao.hcl.tpl").read_text()
    old = tpl.replace("__ESO_LISTENER__\n", "")
    (p.b1_etc / "openbao.hcl.tpl").write_text(old)
    (p.unit_src.parent / "openbao.hcl.tpl").write_text(tpl if keep_opt else old)
    return tpl, old


def test_upgrade_refuses_an_old_template_while_the_listener_is_on(b1, monkeypatch):
    assert listener(b1)[0] == 0
    real = hsa.Agent.staged_bundle

    def old_bundle(self, name):
        info = real(self, name)
        return dict(info, template=info["template"].replace("__ESO_LISTENER__\n", ""))
    monkeypatch.setattr(hsa.Agent, "staged_bundle", old_bundle)
    before = (b1["p"].b1_etc / "openbao.hcl.tpl").read_text()
    rc, st = request(b1, op="b1-upgrade", bundle=b1["bundle"].name)
    assert rc == 1 and st["code"] == "eso-listener-template", st
    assert "eso remove --all" in st["message"]
    assert (b1["p"].b1_etc / "openbao.hcl.tpl").read_text() == before
    assert not b1["host"].ran("podman", "tag", f"{hsa.IMAGE}:current", f"{hsa.IMAGE}:previous")


def test_upgrade_with_an_old_template_is_fine_without_the_listener(b1, monkeypatch):
    real = hsa.Agent.staged_bundle
    monkeypatch.setattr(hsa.Agent, "staged_bundle", lambda self, name: dict(
        real(self, name), template=real(self, name)["template"].replace("__ESO_LISTENER__\n", "")))
    rc, st = request(b1, op="b1-upgrade", bundle=b1["bundle"].name)
    assert rc == 0, st


def test_rotation_with_an_old_template_refused_before_switching_the_secrets(b1):
    assert listener(b1)[0] == 0
    _old_template(b1)
    host, p = b1["host"], b1["p"]
    secrets_before = dict(host.secrets)
    keys_before = sorted(x.name for x in (p.b1_etc / "keys").iterdir())
    cfg_before = config_text(b1)
    rc, st = request(b1, op="rotate-unseal-key")
    assert rc == 1 and st["code"] == "eso-listener-template", st
    assert host.secrets == secrets_before
    assert sorted(x.name for x in (p.b1_etc / "keys").iterdir()) == keys_before
    assert config_text(b1) == cfg_before and LISTEN in cfg_before


def test_rotation_takes_the_template_of_install_and_keeps_the_listener(b1):
    assert listener(b1)[0] == 0
    _old_template(b1, keep_opt=True)
    b1["clock"]["t"] += 86400
    rc, st = request(b1, op="rotate-unseal-key")
    assert rc == 0, st
    assert LISTEN in config_text(b1)
    assert "__ESO_LISTENER__" in (b1["p"].b1_etc / "openbao.hcl.tpl").read_text()


def test_restore_with_an_old_template_refused_before_stopping_anything(b1):
    import test_secrets_agent_jwt_2100 as tj
    assert listener(b1)[0] == 0
    name = tj._snap(b1).name
    _old_template(b1)
    host = b1["host"]
    n = len(host.cmds)
    rc, st = request(b1, op="restore-snapshot", file=name)
    assert rc == 1 and st["code"] == "eso-listener-template", st
    assert not any(c[:2] == ["systemctl", "stop"] for c in host.cmds[n:])
    assert LISTEN in config_text(b1)


# ---------------------------------------------------------------------------
# Revue 8-10 I-5 : l'écouteur ESO expose l'API ; jetons d'OpenTofu liés à
# 127.0.0.1/32, points de rekey non authentifiés fermés
# ---------------------------------------------------------------------------
def test_tf_token_role_bound_to_the_loopback_in_b1():
    reqs = {name: data for name, _op, _path, data, _a in pol.initialize_requests(CID)}
    assert reqs["token-role-tf"]["token_bound_cidrs"] == [pol.B1_CIDR]
    assert "token_bound_cidrs" not in pol.tf_token_role_params(CID)          # mode A : inchangé


def test_eso_listener_closes_the_unauthenticated_rekey_endpoints():
    block = pol.eso_listener_block(LISTEN)
    assert "disable_unauthed_rekey_endpoints = true" in block
    assert "seuls les rôles `eso`" not in block                               # commentaire corrigé


def test_b1_policies_binds_the_tf_token_role_of_an_older_b1(b1):
    srv = b1["srv"]
    srv.token_roles[pol.tf_role(CID)] = pol.tf_token_role_params(CID)         # posé avant la 2.10.0
    rc, st = request(b1, op="b1-policies")
    assert rc == 0, st
    assert srv.token_roles[pol.tf_role(CID)]["token_bound_cidrs"] == [pol.B1_CIDR]
    assert srv.agent_denied == []


@pytest.mark.parametrize("path,cap,ok", [
    (f"auth/token/roles/{pol.tf_role(CID)}", "update", True),
    ("auth/token/roles/other", "update", False),
    ("auth/token/roles/other", "read", True),
])
def test_agent_rules_token_roles(path, cap, ok):
    from fake_openbao_b1 import acl_allows
    assert acl_allows(pol.agent_policy(CID), path, cap) is ok


@pytest.mark.parametrize("cidrs,refused", [(None, False), ([pol.B1_CIDR], False), (["0.0.0.0/0"], True),
                                           (["172.16.1.11/32"], True)])
def test_review_of_the_tf_token_role_binding(b1, cidrs, refused):
    def craft(srv):
        r = dict(pol.tf_token_role_params(CID))
        if cidrs:
            r["token_bound_cidrs"] = cidrs
        srv.token_roles[pol.tf_role(CID)] = r
    rc, st = _restore_with(b1, craft)
    if refused:
        assert rc == 1 and st["code"] == "snapshot-untrusted", st
        return
    assert rc == 0, st
    assert b1["srv"].token_roles[pol.tf_role(CID)]["token_bound_cidrs"] == [pol.B1_CIDR]   # reposé


# ---------------------------------------------------------------------------
# Revue 8-10 I-6 : configuration de repli sans l'écouteur ESO, adresse relue
# par l'unité à chaque démarrage, santé qui dit une adresse partie
# ---------------------------------------------------------------------------
def test_render_keeps_a_loopback_only_configuration_and_the_listener_address(b1):
    p = b1["p"]
    assert listener(b1)[0] == 0
    local = (p.b1_etc / "openbao-local.hcl").read_text()
    assert LISTEN in config_text(b1) and LISTEN not in local and "127.0.0.1:8210" in local
    assert (p.b1_etc / "eso-listener.ip").read_text().strip() == LISTEN.rpartition(":")[0]
    assert listener(b1, None)[0] == 0
    assert not (p.b1_etc / "eso-listener.ip").exists()
    assert (p.b1_etc / "openbao-local.hcl").read_text() == config_text(b1)


def test_health_says_when_the_listener_address_left_the_host(b1):
    assert listener(b1)[0] == 0
    a = b1["agent"]()
    h = a.health()
    assert h["eso_listener"] == LISTEN and h["eso_listener_present"] is True
    b1["host"].addrs = [x for x in b1["host"].addrs if x != LISTEN.rpartition(":")[0]]
    h = b1["agent"]().health()
    assert h["eso_listener_present"] is False
