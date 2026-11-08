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
    # (les connexions AppRole créent aussi les entités de la console et de
    # l'agent, comme OpenBao : seules les entités `tf-*` comptent ici)
    def tf_entities():
        return [e for e in srv.entities.values() if any(n.startswith("tf-") for _a, n in e["aliases"])]
    assert tf_entities()
    rc, st = tagent.request(agent_env, op="tf-entity-remove", decl="d1d1d1d1d1d1")
    assert rc == 0 and st["state"] == "done" and not tf_entities(), st
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


# ---------------------------------------------------------------------------
# Décision 278 et revue I2 des tâches 9-10 : clés transit des déclarations
# supprimées gardées 30 jours (essai : retirée tout de suite)
# ---------------------------------------------------------------------------
D1 = "d1d1d1d1d1d1"


def test_removed_declaration_keeps_its_transit_key_30_days(env, capsys, monkeypatch):
    creds(env)
    settings(env)
    key = pol.tf_key(CID, D1)
    env["srv"].transit[key] = 1
    rc, doc, err = cli(["tf-entity-remove", D1], capsys=capsys)
    assert rc == 0 and "kept 30 days" in doc["transit_key"]
    assert key in env["srv"].transit
    rc, doc, _ = cli(["tf-keys", "list"], capsys=capsys)
    row = doc["keys"][0]
    assert row["decl"] == D1 and row["key"] == key and row["delete_after"] - row["retired_at"] == 30 * 86400
    assert row["due"] is False
    # le balayage avant le délai ne retire rien
    rc, doc, _ = cli(["tf-keys", "sweep"], capsys=capsys)
    assert rc == 0 and doc["removed"] == [] and key in env["srv"].transit
    # 31 jours plus tard
    real = tcli.hsc.time.time
    monkeypatch.setattr(tcli.hsc.time, "time", lambda: real() + 31 * 86400)
    rc, doc, err = cli(["tf-keys", "sweep"], capsys=capsys)
    assert rc == 0 and doc["removed"] == [D1] and key not in env["srv"].transit
    assert read_settings(env)["tf_keys_retired"] == {}


def test_trial_declaration_key_removed_now(env, capsys):
    creds(env)
    settings(env)
    key = pol.tf_key(CID, D1)
    env["srv"].transit[key] = 1
    rc, doc, _ = cli(["tf-entity-remove", D1, "--trial"], capsys=capsys)
    assert rc == 0 and "removed" in doc["transit_key"] and key not in env["srv"].transit
    assert read_settings(env).get("tf_keys_retired") == {}
    # jamais chiffrée : clé absente, rien d'anormal
    rc, doc, _ = cli(["tf-entity-remove", "e2e2e2e2e2e2", "--trial"], capsys=capsys)
    assert rc == 0 and "already absent" in doc["transit_key"]


@pytest.mark.parametrize("confirm,code", [(None, "confirmation-required"), ("other", "confirmation-required")])
def test_early_key_deletion_needs_the_typed_identifier(env, capsys, confirm, code):
    creds(env)
    settings(env)
    key = pol.tf_key(CID, D1)
    env["srv"].transit[key] = 1
    cli(["tf-entity-remove", D1], capsys=capsys)
    argv = ["tf-keys", "delete", D1] + (["--confirm", confirm] if confirm else [])
    rc, doc, _ = cli(argv, capsys=capsys)
    assert rc == 2 and doc["code"] == code and key in env["srv"].transit


def test_early_key_deletion(env, capsys):
    creds(env)
    settings(env)
    key = pol.tf_key(CID, D1)
    env["srv"].transit[key] = 1
    cli(["tf-entity-remove", D1], capsys=capsys)
    rc, doc, _ = cli(["tf-keys", "delete", D1, "--confirm", D1], capsys=capsys)
    assert rc == 0 and doc["removed"] == [D1] and key not in env["srv"].transit
    rc, doc, _ = cli(["tf-keys", "delete", D1, "--confirm", D1], capsys=capsys)
    assert rc == 2 and doc["code"] == "tf-key-not-retired"


def test_key_deletion_failure_keeps_the_key_listed(env, capsys, monkeypatch):
    creds(env)
    settings(env)
    key = pol.tf_key(CID, D1)
    env["srv"].transit[key] = 1
    cli(["tf-entity-remove", D1], capsys=capsys)
    env["srv"].denied.append("transit/keys/")
    rc, doc, err = cli(["tf-keys", "delete", D1, "--confirm", D1], capsys=capsys)
    assert rc == 1 and doc["failed"] == [D1] and "openbao-denied" in err
    assert D1 in read_settings(env)["tf_keys_retired"]


# Relecture finale I5 : une déclaration supprimée puis recréée avec le même
# identifiant reprend la même clé ; le balayage (ou un retrait anticipé) ne
# la détruit jamais tant que la déclaration vit (espace avec un état, ou
# ligne dans la base des déclarations) : l'état deviendrait illisible.
def _live_by_workspace(env):
    ws = env["state"] / "terraform" / "c1" / "decls" / D1
    ws.mkdir(parents=True)
    (ws / "terraform.tfstate").write_text("{}")


def _live_by_db(env):
    import sqlite3
    c = sqlite3.connect(str(env["state"] / "tf-declarations.db"))
    c.execute("CREATE TABLE tf_declarations (id TEXT PRIMARY KEY, cluster TEXT, name TEXT)")
    c.execute("INSERT INTO tf_declarations VALUES (?, 'c1', 'n')", (D1,))
    c.commit()
    c.close()


@pytest.mark.parametrize("live", [_live_by_workspace, _live_by_db], ids=["workspace", "database"])
@pytest.mark.parametrize("how", ["sweep", "delete"])
def test_key_of_a_live_recreated_declaration_is_never_removed(env, capsys, monkeypatch, live, how):
    monkeypatch.delenv("HARVESTER_OPS_TF_WORKSPACES", raising=False)
    monkeypatch.delenv("HARVESTER_OPS_TF_DB", raising=False)
    creds(env)
    settings(env)
    key = pol.tf_key(CID, D1)
    env["srv"].transit[key] = 1
    cli(["tf-entity-remove", D1], capsys=capsys)
    live(env)                                            # recréée avec le même identifiant
    real = tcli.hsc.time.time
    monkeypatch.setattr(tcli.hsc.time, "time", lambda: real() + 31 * 86400)
    argv = ["tf-keys", "sweep"] if how == "sweep" else ["tf-keys", "delete", D1, "--confirm", D1]
    rc, doc, err = cli(argv, capsys=capsys)
    assert key in env["srv"].transit
    assert doc.get("removed", []) == [] and D1 in doc.get("alive", [D1])
    assert D1 not in read_settings(env).get("tf_keys_retired", {})       # plus en attente de retrait
