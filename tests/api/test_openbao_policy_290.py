"""2.9.0 tâche 5 : politiques, assistant du mode A, configuration de B1
(bin/lib/openbao_policy.py).

Texte des politiques comparé à des fichiers attendus (tests/api/fixtures/
openbao_policy_290/), écrits d'après le plan et relus contre le banc : la
configuration de B1 rendue ici a été chargée par l'image de la collection
(2.7.2-5.4) en `--network host` sur 127.0.0.1:8210 (initialisation
complète, connexions AppRole de la console et de l'agent, capacités,
transit, jeton enfant, `sys/rotate/root`, instantané ; rapport des tâches
5 à 7).

Mutations jouées (rapport) : « `*` à la place du préfixe », « `max_versions`
juste après le montage », « `transit/encrypt` à la place de
`transit/datakey/plaintext` », « écoute sur 0.0.0.0 ».
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import openbao_policy as pol  # noqa: E402

FIX = ROOT / "tests" / "api" / "fixtures" / "openbao_policy_290"
CID = "abc123def456"
PREFIX = f"consoles/{CID}"
TPL = (ROOT / "config" / "openbao" / "openbao.hcl.tpl").read_text()


def rules_of(text):
    return re.findall(r'^path "([^"]+)" \{ capabilities = \[([^\]]*)\] \}$', text, re.M)


# ---------------------------------------------------------------------------
# Politiques
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,text", [
    ("console.hcl", pol.console_policy("harvester-ops", PREFIX, CID)),
    ("tf_child.hcl", pol.tf_child_policy(CID, "auth_token_58b9a1c2")),
    ("agent.hcl", pol.agent_policy(CID)),
])
def test_policy_text_is_the_reference(name, text):
    assert text == (FIX / name).read_text()


def test_b1_console_policy_adds_snapshot_read_only():
    a, b = pol.console_policy("harvester-ops", PREFIX, CID), pol.console_policy("harvester-ops", PREFIX, CID, b1=True)
    assert b == a + 'path "sys/storage/raft/snapshot" { capabilities = ["read"] }\n'


@pytest.mark.parametrize("text", [
    pol.console_policy("harvester-ops", PREFIX, CID),
    pol.console_policy("harvester-ops", PREFIX, CID, b1=True),
    pol.console_policy("kv-x", "team/a/consoles/" + CID, CID),
    pol.tf_child_policy(CID, "auth_token_58b9a1c2"),
])
def test_console_and_child_policies_never_administer(text):
    assert "sudo" not in text and "sys/polic" not in text and 'path "*"' not in text


def test_agent_sudo_only_on_root_rotation():
    sudo = [p for p, caps in rules_of(pol.agent_policy(CID)) if "sudo" in caps]
    assert sudo == ["sys/rotate/root"]


def test_agent_amends_only_its_hops_policies():
    """Décision 276 : l'agent ne corrige que les politiques `hops-*` de sa
    console (la sienne, celle des jetons d'OpenTofu), jamais celle de la
    console ni par motif ; les entités tf se retirent par alias."""
    rules = dict(pol.agent_rules(CID))
    acl = sorted(p for p in rules if p.startswith("sys/policies/"))
    assert acl == [f"sys/policies/acl/hops-agent-{CID}", f"sys/policies/acl/hops-tf-{CID}"]
    assert all("delete" not in rules[p] and "*" not in p for p in acl)
    assert not any(p.startswith("identity/entity/name/") for p in rules)    # ne désignait rien (essai P4)
    assert rules["identity/lookup/entity"] == ["update"] and rules["sys/auth"] == ["read"]


@pytest.mark.parametrize("decl,ok", [("d1", True), ("web_2", True), ("../x", False), ("a/b", False),
                                     ("*", False), ("", False), ("A", False)])
def test_tf_alias_is_validated(decl, ok):
    if ok:
        assert pol.tf_alias(decl) == f"tf-{decl}"
    else:
        with pytest.raises(ValueError):
            pol.tf_alias(decl)


@pytest.mark.parametrize("mount,prefix", [("harvester-ops", PREFIX), ("kv", "teams/x/consoles/" + CID)])
def test_kv_paths_always_under_the_prefix(mount, prefix):
    for path, _caps in rules_of(pol.console_policy(mount, prefix, CID, b1=True)):
        if path.startswith(f"{mount}/") and path != f"{mount}/config":
            kind = path.split("/")[1]
            assert kind in ("data", "metadata", "destroy") and path == f"{mount}/{kind}/{prefix}/*", path


def test_metadata_write_is_bounded_to_the_prefix():
    """Point 272 : create, update, patch sur metadata, sous le préfixe seul."""
    caps = dict(rules_of(pol.console_policy("harvester-ops", PREFIX, CID)))
    assert caps[f"harvester-ops/metadata/{PREFIX}/*"].count('"patch"') == 1
    assert not any(p.startswith("harvester-ops/metadata/") and PREFIX not in p for p in caps)


def test_transit_paths_bound_to_this_console():
    for path, _c in rules_of(pol.console_policy("harvester-ops", PREFIX, CID)):
        if path.startswith("transit/"):
            assert f"/hops-{CID}-" in path


def test_child_policy_datakey_and_decrypt_only():
    paths = [p for p, _c in rules_of(pol.tf_child_policy(CID, "auth_token_58b9a1c2"))]
    assert [p.split("/")[1] for p in paths] == ["datakey", "decrypt"]
    assert all(p.endswith(f"hops-{CID}-{{{{identity.entity.aliases.auth_token_58b9a1c2.name}}}}") for p in paths)


def test_required_capabilities_follow_the_policy():
    req = pol.required_capabilities("harvester-ops", PREFIX, CID, b1=True)
    assert f"harvester-ops/data/{PREFIX}/check-x" in req and "*" not in "".join(req)
    assert len(req) == len(pol.console_rules("harvester-ops", PREFIX, CID, b1=True))


@pytest.mark.parametrize("call", [
    lambda: pol.console_policy("harvester-ops", "*", CID),
    lambda: pol.console_policy("harvester-ops", "", CID),
    lambda: pol.console_policy("harvester-ops", "../x", CID),
    lambda: pol.console_policy("Harvester", PREFIX, CID),
    lambda: pol.console_policy("harvester-ops", PREFIX, "ABC123DEF456"),
    lambda: pol.console_policy("harvester-ops", PREFIX, "abc"),
    lambda: pol.tf_child_policy(CID, "auth_approle_x"),
    lambda: pol.tf_child_policy(CID, "x}} path \"*\""),
    lambda: pol.tf_key(CID, "Bad Decl"),
])
def test_bad_names_refused(call):
    with pytest.raises(ValueError):
        call()


def test_names():
    assert pol.console_role(CID) == f"harvester-ops-{CID}"
    assert pol.agent_role(CID) == f"hops-agent-{CID}"
    assert pol.tf_role(CID) == f"hops-tf-{CID}"
    assert pol.tf_key(CID, "d1") == f"hops-{CID}-tf-d1"
    assert pol.secret_id_path(CID) == f"auth/approle/role/harvester-ops-{CID}/secret-id"


# ---------------------------------------------------------------------------
# Mode A
# ---------------------------------------------------------------------------
def cmds(**kw):
    args = dict(addr="https://bao.example:8200", mount="harvester-ops", prefix=PREFIX, cid=CID,
                console_cidr="10.0.0.5/32")
    args.update(kw)
    return pol.mode_a_commands(args["addr"], args["mount"], args["prefix"], args["cid"], args["console_cidr"])


def test_mode_a_commands_carry_no_secret():
    text = "\n".join(cmds())
    assert not re.search(r"secret_id=|token=|password|BAO_TOKEN|hvs\.|\bs\.[A-Za-z0-9]{20}", text)
    assert cmds()[-1] == f"bao write -wrap-ttl=15m -f auth/approle/role/harvester-ops-{CID}/secret-id"
    assert cmds()[-2] == f"bao read -field=role_id auth/approle/role/harvester-ops-{CID}/role-id"


def test_mode_a_commands_policy_and_role():
    c = cmds()
    assert f"bao policy write harvester-ops-{CID} - <<'EOF'\n{pol.console_policy('harvester-ops', PREFIX, CID)}EOF" in c
    role = next(x for x in c if x.startswith(f"bao write auth/approle/role/harvester-ops-{CID} "))
    for part in ("token_period=1h", "token_ttl=1h", "token_max_ttl=0", "secret_id_ttl=0", "secret_id_num_uses=0",
                 "secret_id_bound_cidrs=10.0.0.5/32", "token_bound_cidrs=10.0.0.5/32"):
        assert part in role.split()
    tf = next(x for x in c if x.startswith(f"bao write auth/token/roles/hops-tf-{CID}"))
    assert 'allowed_entity_aliases=tf-*' in tf and "orphan=false" in tf and "renewable=false" in tf
    assert "ACCESSOR=$(bao read -field=accessor sys/auth/token)" in c
    assert any("{{identity.entity.aliases.$ACCESSOR.name}}" in x for x in c)
    assert f"bao write harvester-ops/config max_versions=10" in c
    assert "bao write sys/config/auditing/request-headers/X-Hops-Actor hmac=false" in c


@pytest.mark.parametrize("kw", [
    {"addr": "bao.example"}, {"addr": "ftp://x"}, {"addr": "https://x/; rm -rf /"},
    {"console_cidr": "10.0.0.5"}, {"console_cidr": "any"}, {"mount": "a b"}, {"prefix": "a/../b"},
])
def test_mode_a_bad_input_refused(kw):
    with pytest.raises(ValueError):
        cmds(**kw)


# ---------------------------------------------------------------------------
# Mode B1
# ---------------------------------------------------------------------------
def b1(**kw):
    args = dict(cid=CID, key_id="hops-20261108-1", template=TPL)
    args.update(kw)
    return pol.b1_config(args.pop("cid"), args.pop("key_id"), args.pop("template"), **args)


def initialize_of(text):
    return text[text.index('initialize "hops" {'):]


def test_b1_storage_listener_audit():
    text = b1()
    body = re.sub(r"#[^\n]*", "", text)
    assert 'storage "raft"' in body and 'storage "file"' not in body
    assert re.findall(r'address\s*=\s*"([^"]+)"', body) == ["127.0.0.1:8210", "127.0.0.1:8211"]
    assert 'file_path = "/openbao/audit/audit.log"' in body
    assert "__SEAL__" not in body and "__INITIALIZE__" not in body


def test_b1_max_versions_is_the_last_request_and_may_fail():
    names = [n for n, *_ in pol.initialize_requests(CID)]
    assert names[0] == "mount-kv" and names[-1] == "max-versions"
    last = initialize_of(b1()).split('request "max-versions"')[1]
    assert "allow_failure = true" in last and '"max_versions" = 10' in last
    assert initialize_of(b1()).count("allow_failure") == 1


def test_b1_initialize_has_no_token_nor_clear_value():
    init = initialize_of(b1())
    assert not re.search(r"^\s*token\s*=", init, re.M)
    for field in ('"secret_id" = {', '"id" = {'):
        for block in init.split(field)[1:]:
            assert block.lstrip().startswith('"eval_source" = "file"')
    files = re.findall(r'"path" = "(/run/hops-openbao-boot/[^"]+)"', init)
    assert sorted(files) == sorted(f"{pol.BOOT_DIR}/{f}" for f in pol.BOOT_FILES)


def test_b1_roles_bound_to_loopback():
    reqs = {n: d for n, _o, _p, d, _a in pol.initialize_requests(CID)}
    for r in ("role-console", "role-agent"):
        assert reqs[r]["secret_id_bound_cidrs"] == ["127.0.0.1/32"] == reqs[r]["token_bound_cidrs"]
    assert reqs["boot-token"]["ttl"] == "15m" and reqs["boot-token"]["no_parent"] is True


def test_b1_seal_and_rotation():
    text = b1()
    assert 'current_key = "file:///run/secrets/hops-openbao-unseal"' in text and "previous_key" not in text
    rot = b1(key_id="hops-20261209-1", previous_key_id="hops-20261108-1", init=False)
    assert 'previous_key_id = "hops-20261108-1"' in rot and "file:///run/secrets/hops-openbao-unseal-prev" in rot
    assert 'initialize "hops"' not in rot


def test_template_comment_placeholder_untouched():
    tpl = "# __SEAL__ and __INITIALIZE__ in a comment\n__SEAL__\n__INITIALIZE__\n" + TPL.split("__SEAL__")[0][-1:]
    out = pol.b1_config(CID, "hops-20261108-1", tpl + 'listener "tcp" { address = "127.0.0.1:8210" }\n')
    assert out.startswith("# __SEAL__ and __INITIALIZE__ in a comment\nseal ")


@pytest.mark.parametrize("change,why", [
    (lambda t: t.replace('storage "raft"', 'storage "file"'), "file storage"),
    (lambda t: t.replace('"127.0.0.1:8210"', '"0.0.0.0:8210"'), "listener"),
    (lambda t: t.replace("__SEAL__\n", ""), "without"),
    (lambda t: t.replace("__INITIALIZE__", "# __INITIALIZE__"), "without"),
    (lambda t: t + 'token = "s.abcdefghijklmnopqrstuvwx"\n', "token"),
])
def test_b1_bad_template_refused(change, why):
    with pytest.raises(ValueError, match=why.split()[0]):
        b1(template=change(TPL))


@pytest.mark.parametrize("kw", [
    {"key_id": "20261108-1"}, {"key_id": "hops-x"}, {"previous_key_id": "hops-20261108-1"},
    {"previous_key_id": "bad"}, {"cid": "nothex000000"},
])
def test_b1_bad_ids_refused(kw):
    with pytest.raises(ValueError):
        b1(**kw)


def test_logrotate_keeps_14_and_signals():
    text = pol.logrotate_conf("/var/lib/harvester-ops-openbao/audit")
    assert "rotate 14" in text and "kill --signal HUP harvester-ops-openbao" in text
    assert "create 0600 harvester-ops-openbao root" in text
    # revue M7 : logrotate (root) agit dans le répertoire du compte du conteneur sous ce compte
    assert "  su harvester-ops-openbao root\n" in text


def test_console_policy_has_no_mid_path_glob():
    """Revue M5 : un `*` au milieu d'un chemin n'est pas un motif pour OpenBao."""
    for path, _caps in pol.console_rules("harvester-ops", PREFIX, CID, b1=True):
        assert "*" not in path[:-1], path
