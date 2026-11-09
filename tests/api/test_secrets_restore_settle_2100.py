"""2.10.0 (tâche 13, vu sur le poste client hops-upd) : une restauration B1
lancée depuis la console arrête la console, et l'outil qui l'a lancée avec
elle, avant qu'il range les réglages (méthodes jwt, groupes, clusters ESO,
écouteur absents de l'instantané). La console relancée lit l'état final de
l'agent et lance `harvester-secrets restore-settle` (action suivie), une
seule fois par restauration ; rien sinon."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import app as wapp  # noqa: E402

INFO = {"person_jwt": [], "person_groups": [], "eso_clusters": [], "eso_listener": None}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "_state_dir", lambda: tmp_path)
    (tmp_path / "secrets-agent").mkdir()
    launched = []
    monkeypatch.setattr(wapp, "_secrets_tool_action", lambda label, args, **kw:
                        launched.append((label, args, kw)) or (SimpleNamespace(id="a1"), None))

    def put(status=None, settled=None):
        if status is not None:
            (tmp_path / "secrets-agent" / "status.json").write_text(json.dumps(status))
        doc = {"mode": "b1"}
        if settled is not None:
            doc["restore_settled"] = settled
        (tmp_path / "secrets-settings.json").write_text(json.dumps(doc))
    return SimpleNamespace(put=put, launched=launched)


@pytest.mark.parametrize("status,settled,runs", [
    ({"op": "restore-snapshot", "state": "done", "started": 10.0, "info": INFO}, None, True),
    ({"op": "restore-snapshot", "state": "done", "started": 10.0, "info": INFO}, 9.0, True),
    ({"op": "restore-snapshot", "state": "done", "started": 10.0, "info": INFO}, 10.0, False),   # déjà rangé
    ({"op": "restore-snapshot", "state": "failed", "started": 10.0, "info": INFO}, None, False),
    ({"op": "restore-snapshot", "state": "done", "started": 10.0}, None, False),                 # agent d'avant
    ({"op": "b1-policies", "state": "done", "started": 10.0, "info": INFO}, None, False),
    (None, None, False),                                                                          # pas d'agent
])
def test_settle_after_a_restore(state, status, settled, runs):
    state.put(status, settled)
    wapp._secrets_restore_settle_watch(timeout=1, step=0)
    assert bool(state.launched) is runs
    if runs:
        label, args, kw = state.launched[0]
        assert label == "secrets:restore-settle" and args == ["restore-settle"]
        assert kw.get("clear_cache") is True and kw.get("after_fn") is not None


def test_settle_waits_for_the_final_state(state, monkeypatch):
    seq = [{"op": "restore-snapshot", "state": "running", "started": 10.0},
           {"op": "restore-snapshot", "state": "done", "started": 10.0, "info": INFO}]
    state.put(seq[0])
    calls = {"n": 0}
    real = wapp._secrets_agent_status

    def status():
        calls["n"] += 1
        if calls["n"] == 2:
            state.put(seq[1])
        return real()
    monkeypatch.setattr(wapp, "_secrets_agent_status", status)
    wapp._secrets_restore_settle_watch(timeout=5, step=0)
    assert [x[0] for x in state.launched] == ["secrets:restore-settle"]


def test_the_watch_starts_from_the_entry_point_only():
    """Vu en réel : parti à l'import, le fil appelait des fonctions définies
    plus bas (NameError) ; il part du point d'entrée, comme la sonde."""
    src = (ROOT / "web" / "app.py").read_text()
    main = src[src.rindex('if __name__ == "__main__"'):]
    assert "_start_secrets_restore_settle()" in main
    assert src.count("threading.Thread(target=_secrets_restore_settle_watch") == 1
    i = src.index("threading.Thread(target=_secrets_restore_settle_watch")
    assert src.rindex("\ndef ", 0, i) == src.index("\ndef _start_secrets_restore_settle")
