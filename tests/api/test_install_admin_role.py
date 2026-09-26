"""v1.57.0 : le compte créé par l'installeur administre la console.

Avant, roles.yaml naissait avec `users: {}` et `default_role: viewer` : le seul
compte de la console n'était qu'un lecteur. La fonction d'install.sh est
lancée telle quelle, sur des fichiers d'essai.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def grant(conf, user):
    src = (ROOT / "install.sh").read_text()
    fn = src[src.index("grant_admin_role() {"):src.index("\n}\n", src.index("grant_admin_role() {")) + 3]
    script = f'ok() {{ echo "ok $*"; }}\nCONF_DIR="{conf}"\n{fn}\ngrant_admin_role "{user}"\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def test_the_installer_account_becomes_administrator(tmp_path):
    roles = tmp_path / "roles.yaml"
    roles.write_text("# commentaire\ndefault_role: viewer\nusers: {}\n")
    r = grant(tmp_path, "admin")
    assert r.returncode == 0, r.stderr
    assert "users:\n  admin: admin\n" in roles.read_text()
    assert "# commentaire" in roles.read_text()


def test_a_choice_already_written_is_kept(tmp_path):
    roles = tmp_path / "roles.yaml"
    roles.write_text("default_role: viewer\nusers:\n  admin: operator\n  bob: viewer\n")
    grant(tmp_path, "admin")
    assert roles.read_text() == "default_role: viewer\nusers:\n  admin: operator\n  bob: viewer\n"


def test_an_existing_list_gains_the_account(tmp_path):
    roles = tmp_path / "roles.yaml"
    roles.write_text("default_role: viewer\nusers:\n  bob: viewer\n")
    grant(tmp_path, "ops")
    text = roles.read_text()
    assert "  ops: admin" in text and "  bob: viewer" in text
    import yaml
    assert yaml.safe_load(text)["users"] == {"ops": "admin", "bob": "viewer"}


def test_the_installer_calls_it_after_creating_the_account():
    src = (ROOT / "install.sh").read_text()
    body = src[src.index("setup_basic_auth() {"):]
    assert 'grant_admin_role "$user"' in body.split("\n}\n", 1)[0]
