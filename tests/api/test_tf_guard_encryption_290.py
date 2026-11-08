"""2.9.0, tâche 9 : le chiffrement de l'état appartient à la console.

* un bloc `encryption`, `key_provider` ou `method` dans le code d'une
  déclaration (premier niveau ou dans `terraform`) est refusé avec le code
  `tf-encryption-reserved` ; idem dans `_providers.tf` ;
* `_encryption.tf` n'est accepté dans un espace que s'il porte exactement un
  des textes que la console écrit pour cette déclaration ;
* une ressource ne peut pas prendre le nom du fichier réservé ;
* ce que tf_guard refusait avant reste refusé.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import tf_encryption as tfe  # noqa: E402
import tf_guard as g  # noqa: E402

CID, DECL = "abc123def456", "d1d1d1d1d1d1"
RES = 'resource "harvester_ssh_key" "k" {\n  name = "k"\n  public_key = "ssh-ed25519 AAAA"\n}\n'


def codes(vs):
    return [v.as_dict().get("code") for v in vs]


@pytest.mark.parametrize("code", [
    'encryption {\n  method "aes_gcm" "x" {}\n}\n',
    'key_provider "openbao" "x" {\n  key_name = "k"\n}\n',
    'method "aes_gcm" "x" {\n  keys = key_provider.openbao.x\n}\n',
    'terraform {\n  encryption {\n    state {\n      enforced = false\n    }\n  }\n}\n',
    'terraform {\n  key_provider "openbao" "x" {}\n}\n',
])
def test_encryption_blocks_in_declaration_code_are_reserved(code):
    vs = g.check_resources(RES + code, "harvester")
    assert g.ENCRYPTION_RESERVED in codes(vs), vs
    assert g.check_resources(RES, "harvester") == []


def test_a_terraform_block_without_encryption_keeps_its_old_refusal():
    vs = g.check_resources(RES + 'terraform {\n  backend "local" {}\n}\n', "harvester")
    assert vs and g.ENCRYPTION_RESERVED not in codes(vs)
    assert "belong to the console" in str(vs[0])


def test_encryption_in_the_providers_header_is_reserved():
    header = ('terraform {\n  required_providers {\n    harvester = { source = "harvester/harvester" }\n  }\n'
              '  encryption {\n  }\n}\n')
    assert g.ENCRYPTION_RESERVED in codes(g.check_header(header))


def ws_with(tmp_path, text):
    ws = tmp_path / "decls" / DECL
    ws.mkdir(parents=True)
    (ws / "k.tf").write_text(RES)
    if text is not None:
        (ws / g.ENCRYPTION_FILE).write_text(text)
    return ws


@pytest.mark.parametrize("mode", tfe.MODES)
def test_a_console_written_block_passes(tmp_path, mode):
    ws = ws_with(tmp_path, tfe.block(CID, DECL, mode))
    assert g.check_workspace(ws, known_encryption=tfe.all_blocks(CID, DECL)) == []


@pytest.mark.parametrize("text,known", [
    (tfe.block(CID, DECL).replace("enforced = true", "enforced = false"), "decl"),
    (tfe.block(CID, "other0000000"), "decl"),
    (tfe.block(CID, DECL) + 'resource "harvester_ssh_key" "x" {}\n', "decl"),
    (tfe.block(CID, DECL), "none"),
    ("", "decl"),
])
def test_any_other_encryption_file_is_refused(tmp_path, text, known):
    ws = ws_with(tmp_path, text)
    vs = g.check_workspace(ws, known_encryption=tfe.all_blocks(CID, DECL) if known == "decl" else ())
    assert codes(vs) == [g.ENCRYPTION_RESERVED], vs


def test_an_encryption_file_as_a_link_is_refused(tmp_path):
    ws = ws_with(tmp_path, None)
    target = tmp_path / "elsewhere.tf"
    target.write_text(tfe.block(CID, DECL))
    (ws / g.ENCRYPTION_FILE).symlink_to(target)
    vs = g.check_workspace(ws, known_encryption=tfe.all_blocks(CID, DECL))
    assert vs and "not a regular file" in str(vs[0])


def test_older_refusals_unchanged(tmp_path):
    ws = ws_with(tmp_path, None)
    (ws / "x.tf").write_text('resource "harvester_ssh_key" "y" {\n  provisioner "local-exec" {}\n}\n')
    (ws / "evil.tf.json").write_text("{}")
    vs = g.check_workspace(ws, known_encryption=tfe.all_blocks(CID, DECL))
    assert {v.construct.split()[0] for v in vs} >= {"file", "provisioner"}


def test_a_resource_cannot_take_the_reserved_file_name():
    sys.path.insert(0, str(ROOT / "web"))
    import app as wapp
    raw = {"id": "1" * 12, "kind": "raw",
           "spec": {"tf": 'resource "harvester_ssh_key" "_encryption" {\n  name = "k"\n  public_key = "x"\n}\n'}}
    rendered, errors = wapp._tf_render_resources([raw])
    assert not errors and rendered[0][0] != "_encryption"
