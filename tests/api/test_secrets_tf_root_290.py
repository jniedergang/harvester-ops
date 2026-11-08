"""2.9.0, revue m12 des tâches 9-10 : un outil lancé à la main (shell de
l'administrateur, sans HARVESTER_OPS_TF_WORKSPACES) regarde la même racine
des espaces Terraform que la console. La console écrit la racine qu'elle
emploie dans `<état>/console-paths.json` au démarrage ; l'outil la relit.
Sans cela, `disable` ne voyait aucun état chiffré et passait."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import secret_store as ss  # noqa: E402

spec = importlib.util.spec_from_file_location("hsc_tfroot", ROOT / "bin" / "harvester-secrets.py")
hsc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hsc)

ENCRYPTED = json.dumps({"encrypted_data": "x", "encryption_version": "v0", "meta": {}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.delenv("HARVESTER_OPS_TF_WORKSPACES", raising=False)
    return state


def _encrypted_decl(root, decl="d1", kvm=False):
    base = root / ".kvm" / "c1" if kvm else root / "c1"
    d = base / "decls" / decl
    d.mkdir(parents=True)
    (d / "terraform.tfstate").write_text(ENCRYPTED)


@pytest.mark.parametrize("kvm", [False, True])
def test_hand_run_reads_the_root_recorded_by_the_console(env, tmp_path, kvm):
    root = tmp_path / "elsewhere" / "terraform"
    _encrypted_decl(root, kvm=kvm)
    ss.record_console_paths(env, tf_workspaces=root)
    assert hsc.encrypted_states() == ["d1"]
    assert ss.FileStore(env).tf_dir == root


def test_environment_wins_over_the_recorded_root(env, tmp_path, monkeypatch):
    rec, other = tmp_path / "rec", tmp_path / "env"
    _encrypted_decl(rec, "from-file")
    _encrypted_decl(other, "from-env")
    ss.record_console_paths(env, tf_workspaces=rec)
    monkeypatch.setenv("HARVESTER_OPS_TF_WORKSPACES", str(other))
    assert hsc.encrypted_states() == ["from-env"]
    assert ss.FileStore(env).tf_dir == other


@pytest.mark.parametrize("recorded", [
    None,                          # pas de fichier
    "relative/terraform",          # chemin relatif
    "/nonexistent-hops-290/tf",    # répertoire absent
    123,                           # pas une chaîne
])
def test_unusable_record_falls_back_to_the_state(env, recorded):
    if recorded is not None:
        (env / ss.CONSOLE_PATHS_FILE).write_text(json.dumps({"tf_workspaces": recorded}))
    _encrypted_decl(env / "terraform", "default")
    assert ss.tf_workspaces_dir() == env / "terraform"
    assert hsc.encrypted_states() == ["default"]


def test_unreadable_record_falls_back_to_the_state(env):
    (env / ss.CONSOLE_PATHS_FILE).write_text("{not json")
    assert ss.tf_workspaces_dir() == env / "terraform"


def test_record_is_private_and_not_rewritten_when_unchanged(env, tmp_path):
    root = tmp_path / "tf"
    root.mkdir()
    assert ss.record_console_paths(env, tf_workspaces=root) is True
    p = env / ss.CONSOLE_PATHS_FILE
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert json.loads(p.read_text()) == {"tf_workspaces": str(root)}
    ino = p.stat().st_ino
    assert ss.record_console_paths(env, tf_workspaces=root) is False
    assert p.stat().st_ino == ino


def test_record_refuses_a_link_in_place_of_the_file(env, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (env / ss.CONSOLE_PATHS_FILE).symlink_to(victim)
    root = tmp_path / "tf"
    root.mkdir()
    ss.record_console_paths(env, tf_workspaces=root)
    assert victim.read_text() == "keep"


def test_disable_by_hand_refused_with_an_encrypted_state_elsewhere(env, tmp_path, capsys):
    root = tmp_path / "console-tf"
    _encrypted_decl(root, "enc1")
    ss.record_console_paths(env, tf_workspaces=root)
    (env / ss.SETTINGS_FILE).write_text(json.dumps({"mode": "a", "console_id": "abc123def456"}))
    rc = hsc.main(["disable", "--dry-run"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 2 and doc["code"] == "tf-states-encrypted" and doc["declarations"] == ["enc1"]


def test_console_records_its_root_at_startup():
    src = (ROOT / "web" / "app.py").read_text()
    main = src[src.index('if __name__ == "__main__":'):]
    assert "_record_console_paths()" in main
    body = src[src.index("def _record_console_paths"):]
    body = body[:body.index("\ndef ", 1)]
    assert "TF_WORKSPACES" in body and "record_console_paths" in body


def test_store_rereads_settings_at_once_when_the_state_changes(tmp_path, monkeypatch):
    """Les réglages relus moins d'une seconde après ceux d'un autre état
    (variable changée entre deux commandes d'un même processus) : avant, les
    réglages du premier état servaient encore."""
    a, b = tmp_path / "a", tmp_path / "b"
    for d, mode in ((a, "local"), (b, "a")):
        d.mkdir()
        (d / ss.SETTINGS_FILE).write_text(json.dumps({"mode": mode, "migrated": {"x": {"at": 1}}}))
    monkeypatch.setattr(ss, "_STORE", None)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(a))
    st = ss.store()
    st._refresh()
    assert st.settings["mode"] == "local"
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(b))
    st._refresh()
    assert st.settings["mode"] == "a"
    monkeypatch.setattr(ss, "_STORE", None)
