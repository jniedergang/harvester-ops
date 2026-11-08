"""2.9.0, tâche 9 : états OpenTofu chiffrés par le moteur transit d'OpenBao.

* bin/lib/tf_encryption.py (pur) : texte exact de `_encryption.tf`, nom de
  clé, environnement, verdict du serveur (point 270 B), décision, lecture
  de `tofu state list` ;
* console (web/app.py) : bloc écrit par la console, jeton enfant par action
  (rôle `hops-tf-<console>`, politique fixe, alias `tf-<décl>`, jamais
  d'écriture de politique), BAO_* dans l'environnement du seul processus
  `tofu`, révoqué en fin d'action (réussite et échec), migration d'un état
  clair par `apply -refresh-only`, retour en clair, Terraform et Vault non
  vérifié, `-state-out` refusé, lectures de l'état par `tofu state list` /
  `state pull` / `state push -`.

OpenBao est le faux serveur des essais (tests/api/fake_openbao.py) et
OpenTofu un faux binaire (tests/api/fake_tofu_290.py) qui appelle
`transit/datakey/plaintext` et `transit/decrypt` avec le jeton reçu.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import openbao_client as oc  # noqa: E402
import openbao_policy as pol  # noqa: E402
import secret_store as ss  # noqa: E402
import tf_encryption as tfe  # noqa: E402
from fake_openbao import FakeOpenBao  # noqa: E402

CID = "abc123def456"
DECL = "d1d1d1d1d1d1"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJdD pkg"
FAKE_TOFU = Path(__file__).resolve().parent / "fake_tofu_290.py"

ENFORCED = '''# Written by harvester-ops: OpenTofu state encryption (OpenBao transit).
# Never edit: the console rewrites or refuses this file.
terraform {
  encryption {
    key_provider "openbao" "hops" {
      key_name            = "hops-abc123def456-tf-d1d1d1d1d1d1"
      transit_engine_path = "/transit"
      key_length          = 32
    }
    method "aes_gcm" "hops" {
      keys = key_provider.openbao.hops
    }
    state {
      method   = method.aes_gcm.hops
      enforced = true
    }
    plan {
      method   = method.aes_gcm.hops
      enforced = true
    }
  }
}
'''


# ---------------------------------------------------------------------------
# Module pur
# ---------------------------------------------------------------------------
def test_enforced_block_is_exact():
    assert tfe.block(CID, DECL) == ENFORCED
    assert tfe.key_name(CID, DECL) == "hops-abc123def456-tf-d1d1d1d1d1d1"
    assert tfe.alias(DECL) == "tf-d1d1d1d1d1d1"


@pytest.mark.parametrize("mode,primary,fallback,enforced", [
    ("enforced", "method.aes_gcm.hops", None, "true"),
    ("migrate-in", "method.aes_gcm.hops", "method.unencrypted.migrate", "false"),
    ("migrate-out", "method.unencrypted.migrate", "method.aes_gcm.hops", "false"),
])
def test_block_modes(mode, primary, fallback, enforced):
    text = tfe.block(CID, DECL, mode)
    for target in ("state", "plan"):
        part = text.split(f"    {target} {{\n", 1)[1].split("\n    }\n", 1)[0]
        assert f"method   = {primary}" in part and f"enforced = {enforced}" in part
        assert (f"method = {fallback}" in part) == bool(fallback)
    assert ('method "unencrypted" "migrate" {}' in text) == bool(fallback)
    assert text.count('key_provider "openbao" "hops"') == 1


@pytest.mark.parametrize("cid,decl,mode", [
    ("ABC", DECL, "enforced"), (CID, "../x", "enforced"), (CID, "a*", "enforced"), (CID, DECL, "plain"),
])
def test_block_refuses_bad_input(cid, decl, mode):
    with pytest.raises(ValueError):
        tfe.block(cid, decl, mode)


@pytest.mark.parametrize("state,expected", [
    ({"encrypted_data": "x", "encryption_version": "v0", "meta": {}}, True),
    ('{"encrypted_data": "x", "encryption_version": "v0"}', True),
    ({"version": 4, "resources": []}, False),
    ({"encrypted_data": "x"}, False),
    ("not json", False),
    (None, False),
])
def test_is_encrypted(state, expected):
    assert tfe.is_encrypted(state) is expected


@pytest.mark.parametrize("mode,version,expected", [
    ("b1", "1.17.6", None),
    ("a", "2.0.0-HEAD", None),
    ("a", "2.7.2", None),
    ("a", "1.14.2", None),
    ("a", "1.14.10", None),
    ("a", "1.15.0", tfe.WARN_VAULT_UNVERIFIED),
    ("a", "1.17.6", tfe.WARN_VAULT_UNVERIFIED),
    ("a", "", tfe.WARN_VAULT_UNVERIFIED),
    ("a", None, tfe.WARN_VAULT_UNVERIFIED),
])
def test_server_verdict(mode, version, expected):
    assert tfe.server_verdict(mode, {"version": version}) == expected


@pytest.mark.parametrize("ack,addr,version,expected", [
    ({"addr": "https://v", "version": "1.17"}, "https://v", "1.17.6", True),
    ({"addr": "https://v", "version": "1.17"}, "https://v", "1.18.0", False),
    ({"addr": "https://v", "version": "1.17"}, "https://w", "1.17.6", False),
    (None, "https://v", "1.17.6", False),
    ({"addr": "https://v", "version": "1.17"}, "https://v", "", False),
])
def test_ack_matches(ack, addr, version, expected):
    assert tfe.ack_matches(ack, addr, {"version": version}) is expected


W, V = tfe.WARN_NEEDS_OPENTOFU, tfe.WARN_VAULT_UNVERIFIED


@pytest.mark.parametrize("flavor,active,encrypted,warning,acked,expected", [
    ("opentofu", True, False, None, False, (True, None, None)),
    ("opentofu", False, False, None, False, (False, None, None)),
    ("terraform", True, False, None, False, (False, W, None)),
    ("terraform", False, False, None, False, (False, None, None)),
    ("opentofu", True, False, V, False, (False, V, None)),
    ("opentofu", True, False, V, True, (True, V, None)),
    ("opentofu", True, True, None, False, (True, None, None)),
    ("opentofu", True, True, V, False, (True, None, None)),
    ("opentofu", False, True, None, False, (False, None, "tf-state-encrypted-openbao-inactive")),
    ("terraform", True, True, None, False, (False, None, W)),
])
def test_decide(flavor, active, encrypted, warning, acked, expected):
    d = tfe.decide(flavor, active, encrypted, warning, acked)
    assert (d["encrypt"], d["warning"], d["refuse"]) == expected


@pytest.mark.parametrize("text,expected", [
    ("harvester_ssh_key.a\nharvester_virtualmachine.b\n", ["harvester_ssh_key.a", "harvester_virtualmachine.b"]),
    ("terraform_data.y[0]\nterraform_data.y[1]\n", ["terraform_data.y"]),
    ('terraform_data.z["k"]\n', ["terraform_data.z"]),
    ("data.harvester_image.i\nmodule.m.harvester_ssh_key.a\n", []),
    ("", []),
    ("garbage line\n", []),
])
def test_parse_state_list(text, expected):
    assert tfe.parse_state_list(text) == expected


def test_child_policy_grants_datakey_and_decrypt_of_its_own_key_only():
    """Essai 8 : OpenTofu appelle transit/datakey/plaintext et transit/decrypt,
    jamais transit/encrypt ; la clé est désignée par l'alias du jeton."""
    text = pol.tf_child_policy(CID, "auth_token_abc123")
    tmpl = f"hops-{CID}-{{{{identity.entity.aliases.auth_token_abc123.name}}}}"
    assert text == (f'path "transit/datakey/plaintext/{tmpl}" {{ capabilities = ["update"] }}\n'
                    f'path "transit/decrypt/{tmpl}" {{ capabilities = ["update"] }}\n')
    assert tfe.key_name(CID, DECL) == f"hops-{CID}-" + tfe.alias(DECL)    # la clé que le gabarit désigne


def test_env_and_ttl():
    assert tfe.env("https://b:8210", "s.tok") == {"BAO_ADDR": "https://b:8210", "BAO_TOKEN": "s.tok"}
    assert tfe.env("https://b", "t", "/etc/ca.pem")["BAO_CACERT"] == "/etc/ca.pem"
    assert tfe.child_ttl(900) == 1200


# ---------------------------------------------------------------------------
# La console : faux OpenBao, faux OpenTofu, vrai _tf_run_cmd
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def wapp():
    import app
    return app


@pytest.fixture
def tfx(tmp_path, monkeypatch, wapp):
    srv = FakeOpenBao().start()
    admin = srv.add_token(policies=[pol.console_role(CID)])
    state, etc, run = tmp_path / "state", tmp_path / "etc", tmp_path / "run"
    for d in (state, etc, run):
        d.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setenv("HARVESTER_OPS_OPENBAO_DIR", str(etc))
    settings = {"mode": "a", "addr": srv.addr, "console_id": CID, "secret_files_dir": str(run),
                "dev_disk_secret_files": True}
    (state / ss.SETTINGS_FILE).write_text(json.dumps(settings))

    def factory(actor=None, action=None):
        c = oc.Client(srv.addr, actor=actor or "service", action=action, retry_delays=(0.01,))
        c.set_token(admin)
        return c
    st = ss.MultiStore(state, client_factory=factory)
    monkeypatch.setattr(ss, "_STORE", st)
    tofu = tmp_path / "tofu"
    shutil.copy(FAKE_TOFU, tofu)
    os.chmod(tofu, 0o755)
    monkeypatch.setattr(wapp, "TF_BIN", str(tofu))
    monkeypatch.setattr(wapp, "TF_FLAVOR", "opentofu")
    store = wapp._tfs.DeclStore(tmp_path / "tf.db")
    monkeypatch.setattr(wapp, "TF_DECLS", store)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path / "ws")
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init", lambda ws: "1.9.0")
    wapp._TF_SERVER_STATUS.clear()
    kc = tmp_path / "kc"
    kc.write_text("apiVersion: v1\n")
    removed = []
    monkeypatch.setattr(wapp, "_tf_entity_remove_start", lambda d: removed.append(d))
    x = type("X", (), {})()
    x.srv, x.state, x.store, x.kc, x.settings, x.removed, x.tmp = srv, state, store, str(kc), settings, removed, tmp_path
    x.ws = tmp_path / "ws" / "harv1" / "decls" / DECL
    x.shared = tmp_path / "ws" / "harv1"

    def set_settings(**kw):
        doc = dict(settings, **kw)
        (state / ss.SETTINGS_FILE).write_text(json.dumps(doc))
        st._checked = 0                          # relu tout de suite
        st.SETTINGS_RECHECK_S = 0
    x.set_settings = set_settings
    yield x
    srv.stop()


def sshkey(name="ops"):
    return {"id": "1" * 12, "kind": "ssh_key", "spec": {"name": name, "namespace": "default", "public_key": KEY}}


def decl(x):
    return x.store.create(DECL, "harv1", "web", resources=[sshkey()])


def run_decl(wapp, x, d, mode, use_saved=False):
    rendered, errors = wapp._tf_render_resources(d["resources"])
    assert not errors
    run = wapp.ActionRun("r" + mode[:4] + "0" * 7, "x", "harv1", [])
    wapp._tf_decl_runner(run, "harv1", x.kc, d, rendered, mode, wapp._tf_decl_hash(rendered), use_saved, "ju")
    return run


def calls(ws):
    p = Path(ws) / ".fake-tofu.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def steps(run):
    return {e["step_id"]: (e["status"], e.get("message", "")) for e in run.events if e.get("type") == "step"}


def child_tokens(srv):
    return [r for r in srv.log if r["path"].startswith("auth/token/create/")]


def test_new_declaration_is_encrypted_with_its_child_token(wapp, tfx):
    d = decl(tfx)
    run = run_decl(wapp, tfx, d, "apply")
    assert run.status == "done", run.error_summary
    assert (tfx.ws / "_encryption.tf").read_text() == tfe.block(CID, DECL)
    st = json.loads((tfx.ws / "terraform.tfstate").read_text())
    assert tfe.is_encrypted(st) and "MARKER" not in json.dumps(st)
    assert tfx.srv.datakeys and set(tfx.srv.datakeys) == {tfe.key_name(CID, DECL)}
    # jeton enfant : rôle et politique fixes, alias de la déclaration
    made = child_tokens(tfx.srv)
    assert len(made) == 1
    assert made[0]["path"] == f"auth/token/create/{pol.tf_role(CID)}"
    assert json.loads(made[0]["raw"])["policies"] == [pol.tf_role(CID)]
    assert json.loads(made[0]["raw"])["entity_alias"] == f"tf-{DECL}"
    assert json.loads(made[0]["raw"])["ttl"] == f"{tfe.child_ttl(wapp.TF_BAO_SESSION_TIMEOUT)}s"
    assert not [r for r in tfx.srv.log if r["path"].startswith(("sys/policies", "sys/policy"))]
    # dans l'environnement de tofu seulement, jamais en argument ni dans un fichier
    cs = calls(tfx.ws)
    tokens = {c["token"] for c in cs}
    assert len(tokens) == 1 and None not in tokens
    tok = tokens.pop()
    assert all("BAO_ADDR" in c["env"] and "BAO_TOKEN" in c["env"] for c in cs)
    assert not any(tok in " ".join(c["argv"]) for c in cs)
    for f in tfx.ws.rglob("*"):
        if f.is_file() and f.name != ".fake-tofu.jsonl":
            assert tok not in f.read_text(errors="ignore"), f
    assert tok not in json.dumps(list(run.events)) and tok not in json.dumps(run.result)
    # révoqué en fin d'action
    assert tok not in tfx.srv.tokens
    assert steps(run)["encryption"][0] == "done"


def test_child_token_revoked_when_the_action_fails(wapp, tfx):
    d = decl(tfx)
    tfx.ws.mkdir(parents=True)
    (tfx.ws / ".fake-fail-apply").write_text("")
    run = run_decl(wapp, tfx, d, "apply")
    assert run.status == "error"
    tok = calls(tfx.ws)[0]["token"]
    assert tok and tok not in tfx.srv.tokens


def test_child_token_revoked_on_an_exception(wapp, tfx, monkeypatch):
    d = decl(tfx)
    real = wapp._tf_run_cmd

    def boom(ws, kc, args, **kw):
        if args[0] == "plan":
            raise RuntimeError("boom")
        return real(ws, kc, args, **kw)
    monkeypatch.setattr(wapp, "_tf_run_cmd", boom)
    run = run_decl(wapp, tfx, d, "plan")
    assert run.status == "error"
    tok = calls(tfx.ws)[0]["token"]
    assert tok and tok not in tfx.srv.tokens


def test_plain_state_is_migrated_then_enforced(wapp, tfx):
    d = decl(tfx)
    tfx.ws.mkdir(parents=True)
    (tfx.ws / "terraform.tfstate").write_text(json.dumps({
        "version": 4, "serial": 3, "lineage": "l1", "resources": [
            {"mode": "managed", "type": "harvester_ssh_key", "name": "ops",
             "instances": [{"attributes": {"name": "MARKER-ops"}}]}]}))
    (tfx.ws / ".terraform").mkdir()
    run = run_decl(wapp, tfx, d, "plan")
    assert run.status == "done", run.error_summary
    cs = calls(tfx.ws)
    refresh = [c for c in cs if c["argv"][:2] == ["apply", "-refresh-only"]]
    assert len(refresh) == 1 and refresh[0]["enc"] == ["aes_gcm", "unencrypted"]
    assert steps(run)["encrypt-state"][0] == "done"
    assert tfe.is_encrypted((tfx.ws / "terraform.tfstate").read_text())
    assert "MARKER" not in (tfx.ws / "terraform.tfstate").read_text()
    # bloc définitif posé à la fin de l'action
    assert (tfx.ws / "_encryption.tf").read_text() == tfe.block(CID, DECL, "enforced")
    # l'action suivante lit l'état chiffré sous le bloc définitif
    run2 = run_decl(wapp, tfx, d, "apply")
    assert run2.status == "done", run2.error_summary
    assert run2.result["deployed"] == ["harvester_ssh_key.ops"]


def test_migration_failure_keeps_the_migration_block(wapp, tfx):
    d = decl(tfx)
    tfx.ws.mkdir(parents=True)
    (tfx.ws / "terraform.tfstate").write_text(json.dumps({"version": 4, "resources": []}))
    (tfx.ws / ".terraform").mkdir()
    (tfx.ws / ".fake-fail-apply").write_text("")
    run = run_decl(wapp, tfx, d, "plan")
    assert run.status == "error" and "encrypt-state" in steps(run)
    assert (tfx.ws / "_encryption.tf").read_text() == tfe.block(CID, DECL, "migrate-in")
    assert not tfe.is_encrypted((tfx.ws / "terraform.tfstate").read_text())


def test_terraform_flavor_warns_and_writes_no_block(wapp, tfx, monkeypatch):
    monkeypatch.setattr(wapp, "TF_FLAVOR", "terraform")
    d = decl(tfx)
    run = run_decl(wapp, tfx, d, "apply")
    assert run.status == "done", run.error_summary
    assert run.result["encryption_warning"] == tfe.WARN_NEEDS_OPENTOFU
    assert steps(run)["encryption"][0] == "warn"
    assert not (tfx.ws / "_encryption.tf").exists()
    assert not tfe.is_encrypted((tfx.ws / "terraform.tfstate").read_text())
    assert child_tokens(tfx.srv) == []
    assert all(c["token"] is None for c in calls(tfx.ws))


def test_vault_newer_than_114_warns_until_acknowledged(wapp, tfx):
    tfx.srv.seal_version = "1.17.6"
    d = decl(tfx)
    run = run_decl(wapp, tfx, d, "apply")
    assert run.status == "done", run.error_summary          # reste fonctionnel
    assert run.result["encryption_warning"] == tfe.WARN_VAULT_UNVERIFIED
    assert not (tfx.ws / "_encryption.tf").exists() and child_tokens(tfx.srv) == []
    tfx.set_settings(tf_vault_ack={"addr": tfx.srv.addr, "version": "1.17", "at": 1, "by": "admin"})
    run2 = run_decl(wapp, tfx, d, "apply")
    assert run2.status == "done", run2.error_summary
    assert tfe.is_encrypted((tfx.ws / "terraform.tfstate").read_text())
    assert (tfx.ws / "_encryption.tf").read_text() == tfe.block(CID, DECL)


def test_openbao_ca_reaches_tofu_as_bao_cacert(wapp, tfx):
    ca = tfx.tmp / "bao-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nnot used by plain http\n-----END CERTIFICATE-----\n")
    tfx.set_settings(ca_file=str(ca))
    d = decl(tfx)
    run = run_decl(wapp, tfx, d, "plan")
    assert run.status == "done", run.error_summary
    assert {c["cacert"] for c in calls(tfx.ws)} == {str(ca)}


@pytest.mark.parametrize("flavor,mode,code", [
    ("opentofu", "local", "tf-state-encrypted-openbao-inactive"),
    ("terraform", "a", tfe.WARN_NEEDS_OPENTOFU),
])
def test_encrypted_state_without_openbao_or_opentofu_is_refused(wapp, tfx, monkeypatch, flavor, mode, code):
    d = decl(tfx)
    assert run_decl(wapp, tfx, d, "apply").status == "done"
    monkeypatch.setattr(wapp, "TF_FLAVOR", flavor)
    tfx.set_settings(mode=mode)
    run = run_decl(wapp, tfx, d, "apply")
    assert run.status == "error" and code in run.error_summary
    assert tfe.is_encrypted((tfx.ws / "terraform.tfstate").read_text())


def test_an_expired_child_token_is_replaced_before_the_command(wapp, tfx):
    sess = wapp._TfBaoSession(DECL, 60)
    env1 = sess.env()
    sess.expires = time.time() - 1                        # la console compare elle-même l'heure
    env2 = sess.env()
    assert env1["BAO_TOKEN"] != env2["BAO_TOKEN"]
    assert env1["BAO_TOKEN"] not in tfx.srv.tokens          # l'ancien révoqué
    sess.revoke()
    assert env2["BAO_TOKEN"] not in tfx.srv.tokens


def test_state_out_is_refused(wapp, tfx):
    tfx.ws.mkdir(parents=True)
    rc, _out, err = wapp._tf_run_cmd(tfx.ws, "", ["state", "mv", "-state-out=../plain.tfstate", "a.b", "a.b"])
    assert rc == 126 and "-state-out" in err
    assert calls(tfx.ws) == []


def test_a_tampered_encryption_file_is_refused(wapp, tfx):
    d = decl(tfx)
    assert run_decl(wapp, tfx, d, "apply").status == "done"
    f = tfx.ws / "_encryption.tf"
    f.write_text(f.read_text().replace('"/transit"', '"/transit2"'))
    n = len(calls(tfx.ws))
    rc, _out, err = wapp._tf_run_cmd(tfx.ws, "", ["state", "list"])
    assert rc == 126 and "_encryption.tf" in err and len(calls(tfx.ws)) == n


def test_addresses_of_an_encrypted_state_come_from_state_list(wapp, tfx):
    d = decl(tfx)
    assert run_decl(wapp, tfx, d, "apply").status == "done"
    (tfx.ws / wapp.TF_ADDRESSES_CACHE).unlink()
    n = len(calls(tfx.ws))
    assert wapp._tf_addresses(tfx.ws) == ["harvester_ssh_key.ops"]
    new = calls(tfx.ws)[n:]
    assert [c["argv"][:2] for c in new] == [["state", "list"]] and new[0]["token"]
    assert new[0]["token"] not in tfx.srv.tokens          # jeton de la commande, révoqué
    # gardée tant que l'état ne change pas
    assert wapp._tf_addresses(tfx.ws) == ["harvester_ssh_key.ops"]
    assert len(calls(tfx.ws)) == n + 1
    os.utime(tfx.ws / "terraform.tfstate", ns=(1, 1))
    assert wapp._tf_addresses(tfx.ws) == ["harvester_ssh_key.ops"]
    assert len(calls(tfx.ws)) == n + 2


def test_unreadable_encrypted_state_raises_instead_of_empty(wapp, tfx):
    d = decl(tfx)
    assert run_decl(wapp, tfx, d, "apply").status == "done"
    (tfx.ws / wapp.TF_ADDRESSES_CACHE).unlink()
    tfx.srv.denied.append("transit/")
    with pytest.raises(wapp.TfStateUnreadable):
        wapp._tf_addresses(tfx.ws)
    # suppression refusée plutôt qu'un espace cru vide
    assert wapp._tf_decl_remove(DECL) == (False, "state-unreadable")
    assert tfx.store.get(DECL) is not None


def test_decrypt_state_returns_to_clear_text(wapp, tfx):
    d = decl(tfx)
    assert run_decl(wapp, tfx, d, "apply").status == "done"
    run = run_decl(wapp, tfx, d, "decrypt-state")
    assert run.status == "done", run.error_summary
    refresh = [c for c in calls(tfx.ws) if c["argv"][:2] == ["apply", "-refresh-only"]]
    assert refresh[-1]["enc"] == ["unencrypted", "aes_gcm"]
    assert not (tfx.ws / "_encryption.tf").exists()
    st = json.loads((tfx.ws / "terraform.tfstate").read_text())
    assert not tfe.is_encrypted(st) and st["resources"][0]["name"] == "ops"
    # remis en clair : OpenBao peut être désactivé, l'état reste lisible
    tfx.set_settings(mode="local")
    assert wapp._tf_addresses(tfx.ws) == ["harvester_ssh_key.ops"]


def test_decrypt_state_of_a_plain_state_is_refused(wapp, tfx, monkeypatch):
    monkeypatch.setattr(wapp, "TF_FLAVOR", "terraform")
    d = decl(tfx)
    assert run_decl(wapp, tfx, d, "apply").status == "done"
    monkeypatch.setattr(wapp, "TF_FLAVOR", "opentofu")
    run = run_decl(wapp, tfx, d, "decrypt-state")
    assert run.status == "error" and "tf-state-not-encrypted" in run.error_summary


def test_adoption_into_an_encrypted_workspace_uses_pull_and_push(wapp, tfx):
    tfx.shared.mkdir(parents=True)
    (tfx.shared / "terraform.tfstate").write_text(json.dumps({
        "version": 4, "serial": 5, "lineage": "shared", "resources": [
            {"mode": "managed", "type": "harvester_ssh_key", "name": "ops",
             "instances": [{"attributes": {"name": "MARKER-ops"}}]},
            {"mode": "managed", "type": "harvester_ssh_key", "name": "other", "instances": []}]}))
    d = decl(tfx)
    run = run_decl(wapp, tfx, d, "plan")
    assert run.status == "done", run.error_summary
    assert "adopt" in steps(run)
    cs = calls(tfx.ws)
    pushes = [c for c in cs if c["argv"][:3] == ["state", "push", "-"]]
    assert len(pushes) == 1 and json.loads(pushes[0]["stdin"])["resources"][0]["name"] == "ops"
    assert not any(a.startswith("-state-out") for c in cs for a in c["argv"])
    dst = (tfx.ws / "terraform.tfstate").read_text()
    assert tfe.is_encrypted(dst) and "MARKER" not in dst
    src = json.loads((tfx.shared / "terraform.tfstate").read_text())
    assert [r["name"] for r in src["resources"]] == ["other"]
    assert wapp._tf_addresses(tfx.ws) == ["harvester_ssh_key.ops"]


def test_destroy_keeps_the_encryption_block(wapp, tfx):
    d = decl(tfx)
    assert run_decl(wapp, tfx, d, "apply").status == "done"
    run = run_decl(wapp, tfx, d, "destroy")
    assert run.status == "done", run.error_summary
    assert run.result["deployed"] == []
    assert (tfx.ws / "_encryption.tf").read_text() == tfe.block(CID, DECL)


def test_removing_an_encrypted_declaration_asks_for_its_entity(wapp, tfx):
    d = decl(tfx)
    assert run_decl(wapp, tfx, d, "apply").status == "done"
    assert run_decl(wapp, tfx, d, "destroy").status == "done"
    assert wapp._tf_decl_remove(DECL) == (True, None)
    assert tfx.removed == [DECL]


def test_removing_a_plain_declaration_asks_nothing(wapp, tfx, monkeypatch):
    monkeypatch.setattr(wapp, "TF_FLAVOR", "terraform")
    d = decl(tfx)
    assert wapp._tf_decl_remove(d["id"]) == (True, None)
    assert tfx.removed == []


def test_declaration_view_tells_the_encryption(wapp, tfx):
    d = decl(tfx)
    assert run_decl(wapp, tfx, d, "apply").status == "done"
    v = wapp._tf_decl_view(tfx.store.get(DECL))
    assert v["encryption"] == {"state": "encrypted", "block": "enforced", "warning": None}
    assert v["deployed"] == ["harvester_ssh_key.ops"]


def test_launch_accepts_the_decrypt_mode(wapp, tfx, monkeypatch):
    decl(tfx)
    started = []
    monkeypatch.setattr(wapp, "_tf_decl_runner", lambda *a: started.append(a))
    run_id, _h = wapp._tf_decl_launch("harv1", tfx.kc, DECL, "decrypt-state", user="ju", role="admin")
    with wapp.ACTIONS_LOCK:
        run = wapp.ACTIONS.pop(run_id)
    assert run.action.startswith("tf-decrypt-state-decl:") and not run.dry_run
    with wapp.TF_DECL_RUNNING_LOCK:
        wapp.TF_DECL_RUNNING.discard(DECL)
        fd = wapp.TF_DECL_FDS.pop(DECL, None)
    wapp._flock_release(fd)


def test_routes_are_admin_and_rate_limited(wapp):
    import limits
    assert wapp.required_role_for("/api/tf-declarations/abc/decrypt-state", "POST") == "admin"
    assert wapp.required_role_for("/api/secrets/tf-encryption/ack", "POST") == "admin"
    rules = {r.rule: r.endpoint for r in wapp.app.url_map.iter_rules()}
    for rule in ("/api/tf-declarations/<decl_id>/decrypt-state", "/api/secrets/tf-encryption/ack"):
        fn = wapp.app.view_functions[rules[rule]]
        src = __import__("inspect").getsource(fn)
        assert "@requires_auth" in src and "@_rate_limit(" in src
        lim = src.split('@_rate_limit("', 1)[1].split('"', 1)[0]
        assert limits.parse_many(lim)
