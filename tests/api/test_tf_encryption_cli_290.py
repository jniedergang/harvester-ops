"""2.9.0, tâche 9 : parité en ligne de commande des états chiffrés.

* `harvester-secrets tf-encryption status | ack-vault` (point 270 B) :
  acquittement d'un Vault plus récent que 1.14, pour cette adresse et cette
  version ; refus sans objet ;
* `harvester-secrets tf-entity-remove DECL` : retrait par l'agent en B1
  (opération `tf-entity-remove` de la liste fermée), fiche du mode A sinon ;
* `harvester-secrets disable` refusé tant qu'un état est chiffré.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_harvester_secrets_cli_290 as tcli  # noqa: E402
import test_secrets_agent_290 as tagent  # noqa: E402
from test_harvester_secrets_cli_290 import cli, creds, env, read_settings, settings  # noqa: E402,F401

import openbao_policy as pol  # noqa: E402
import tf_encryption as tfe  # noqa: E402

CID = tcli.CID


def test_status_on_openbao_needs_no_ack(env, capsys):
    creds(env)
    settings(env)
    rc, doc, _ = cli(["tf-encryption", "status"], capsys=capsys)
    assert rc == 0 and doc["warning"] is None and doc["version"] == "2.0"
    rc, doc, _ = cli(["tf-encryption", "ack-vault"], capsys=capsys)
    assert rc == 2 and doc["code"] == "not-needed"
    assert read_settings(env).get("tf_vault_ack") is None


def test_ack_of_a_newer_vault_is_kept_for_its_address_and_version(env, capsys):
    creds(env)
    settings(env)
    env["srv"].seal_version = "1.17.6"
    rc, doc, _ = cli(["--actor", "alice", "tf-encryption", "status"], capsys=capsys)
    assert rc == 0 and doc["warning"] == tfe.WARN_VAULT_UNVERIFIED and doc["acknowledged"] is False
    rc, doc, err = cli(["--actor", "alice", "tf-encryption", "ack-vault"], capsys=capsys)
    assert rc == 0 and doc["acknowledged"] is True and "STEP_EVENT|ack|done" in err
    ack = read_settings(env)["tf_vault_ack"]
    assert ack["addr"] == env["srv"].addr and ack["version"] == "1.17" and ack["by"] == "alice"
    env["srv"].seal_version = "1.18.0"
    rc, doc, _ = cli(["tf-encryption", "status"], capsys=capsys)
    assert doc["acknowledged"] is False                 # une autre version demande un nouvel acquittement


@pytest.mark.parametrize("prep,code", [
    ("local", "openbao-inactive"),
    ("unreadable-version", "tf-encryption-version-unknown"),
])
def test_ack_refusals(env, capsys, prep, code):
    creds(env)
    settings(env, mode="local" if prep == "local" else "a")
    if prep == "unreadable-version":
        env["srv"].seal_version = ""
    rc, doc, _ = cli(["tf-encryption", "ack-vault"], capsys=capsys)
    assert rc == 2 and doc["code"] == code
    assert read_settings(env).get("tf_vault_ack") is None


def test_entity_removal_in_mode_a_is_listed_for_the_administrator(env, capsys):
    creds(env)
    settings(env)
    rc, doc, _ = cli(["tf-entity-remove", "d1d1d1d1d1d1"], capsys=capsys)
    assert rc == 0 and doc["mode"] == "a" and doc["alias"] == "tf-d1d1d1d1d1d1"
    assert any("identity/lookup/entity" in c and "tf-d1d1d1d1d1d1" in c for c in doc["commands"])
    assert read_settings(env)["tf_entities_pending"] == ["d1d1d1d1d1d1"]
    cli(["tf-entity-remove", "d1d1d1d1d1d1"], capsys=capsys)
    assert read_settings(env)["tf_entities_pending"] == ["d1d1d1d1d1d1"]      # une seule fois
    rc, doc, _ = cli(["tf-entity-remove", "d1d1d1d1d1d1", "--done"], capsys=capsys)
    assert rc == 0 and read_settings(env)["tf_entities_pending"] == []


@pytest.mark.parametrize("decl,mode,code", [("../x", "a", "bad-input"), ("d1", "local", "openbao-inactive")])
def test_entity_removal_refusals(env, capsys, decl, mode, code):
    settings(env, mode=mode)
    rc, doc, _ = cli(["tf-entity-remove", decl], capsys=capsys)
    assert rc == 2 and doc["code"] == code


def test_entity_removal_in_b1_goes_to_the_agent(env, capsys, monkeypatch):
    settings(env, mode="b1")
    seen = []
    monkeypatch.setattr(tcli.hsc, "run_agent", lambda req: seen.append(req) or {"state": "done",
                                                                               "message": "removed"})
    rc, doc, _ = cli(["tf-entity-remove", "d1d1d1d1d1d1"], capsys=capsys)
    assert rc == 0 and seen == [{"op": "tf-entity-remove", "decl": "d1d1d1d1d1d1"}]


def test_disable_refused_while_a_state_is_encrypted(env, capsys, tmp_path, monkeypatch):
    creds(env)
    settings(env)
    root = tmp_path / "tfws"
    monkeypatch.setenv("HARVESTER_OPS_TF_WORKSPACES", str(root))
    ws = root / "harv1" / "decls" / "d1d1d1d1d1d1"
    ws.mkdir(parents=True)
    (ws / "terraform.tfstate").write_text(json.dumps({"encrypted_data": "x", "encryption_version": "v0"}))
    kvm = root / ".kvm" / "k1" / "decls" / "e2e2e2e2e2e2"
    kvm.mkdir(parents=True)
    (kvm / "terraform.tfstate").write_text(json.dumps({"encrypted_data": "x", "encryption_version": "v0"}))
    plain = root / "harv1" / "decls" / "f3f3f3f3f3f3"
    plain.mkdir(parents=True)
    (plain / "terraform.tfstate").write_text(json.dumps({"version": 4, "resources": []}))
    rc, doc, _ = cli(["disable"], capsys=capsys)
    assert rc == 2 and doc["code"] == "tf-states-encrypted"
    assert doc["declarations"] == ["d1d1d1d1d1d1", "e2e2e2e2e2e2"]
    assert read_settings(env)["mode"] == "a"
    for w in (ws, kvm):
        (w / "terraform.tfstate").write_text(json.dumps({"version": 4, "resources": []}))
    rc, doc, _ = cli(["disable"], capsys=capsys)
    assert rc == 0 and read_settings(env)["mode"] == "local"


# --- opération de l'agent (B1) -------------------------------------------------
agent_env = tagent.env
bundle = tagent.bundle


def test_agent_removes_the_entity_of_a_declaration(agent_env):
    tagent.install(agent_env)
    srv = agent_env["srv"]
    c = agent_env["agent"]().console_client(addr=srv.addr)
    c.token_create_child(pol.tf_role(CID), [pol.tf_role(CID)], "60s", entity_alias=pol.tf_alias("d1d1d1d1d1d1"))
    assert srv.entities
    rc, st = tagent.request(agent_env, op="tf-entity-remove", decl="d1d1d1d1d1d1")
    assert rc == 0 and st["state"] == "done" and not srv.entities, st
    rc, st = tagent.request(agent_env, op="tf-entity-remove", decl="d1d1d1d1d1d1")
    assert rc == 0 and "already absent" in st["message"]


@pytest.mark.parametrize("req,code", [
    ({"decl": "../x"}, "bad-request"),
    ({"decl": "d1", "extra": 1}, "bad-request"),
])
def test_agent_entity_removal_refusals(agent_env, req, code):
    tagent.install(agent_env)
    rc, st = tagent.request(agent_env, op="tf-entity-remove", **req)
    assert rc == 1 and st["code"] == code


def test_agent_entity_removal_needs_b1(agent_env):
    rc, st = tagent.request(agent_env, op="tf-entity-remove", decl="d1")
    assert rc == 1 and st["code"] == "b1-not-installed"
