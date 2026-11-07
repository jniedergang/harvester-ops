"""2.7.0 (tâche 6, point 208) : route du jeton d'automatisation. Le jeton
est rendu une fois (200, `Cache-Control: no-store`) ; l'action `capi-token:`
garde l'étape sans la valeur. La valeur n'est jamais dans `actions.db`, le
journal de la console, les événements de l'action, son résultat ni sa
ligne de commande publique."""

import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as wapp  # noqa: E402
from test_capi_artefact_routes_270 import calls, env, wait  # noqa: E402,F401

TOKEN = "eyJhbGciOiJSUzI1NiJ9.SECRET-TOKEN-VALUE-271828.sig"


@pytest.fixture
def store(env, tmp_path, monkeypatch):
    """actions.db, répertoire de journaux et journal de la console à part,
    pour y chercher la valeur après l'appel."""
    monkeypatch.setenv("FAKE_TOKEN", TOKEN)
    db = tmp_path / "actions.db"
    monkeypatch.setattr(wapp, "ACTIONS_DB", db)
    wapp._actions_init_db()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(wapp, "LOG_DIR", logs)
    log_file = logs / "console.log"
    handler = logging.FileHandler(log_file)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield {"db": db, "logs": logs, "tmp": tmp_path}
    root.removeHandler(handler)
    root.setLevel(old)
    handler.close()


def ask(env, **over):
    b = {"cluster": "web/web", "account": "equipe-a/ci", "seconds": 600}
    b.update(over)
    return env.client.post("/api/capi/harvlab2/token", json={k: v for k, v in b.items() if v is not None})


def files_containing(root, needle):
    hits = []
    for p in Path(root).rglob("*"):
        if p.is_file() and p.name != "calls.jsonl" and needle.encode() in p.read_bytes():
            hits.append(p.name)
    return hits


def test_the_token_is_answered_once_and_never_kept(env, store):
    r = ask(env)
    assert r.status_code == 200, r.get_json()
    out = r.get_json()
    assert out["token"] == TOKEN and out["expires_at"] == "2026-11-07T15:00:00Z" and out["seconds"] == 600
    assert "no-store" in r.headers["Cache-Control"]
    run = wait(out["action_id"])
    assert run.action == "capi-token:web/web" and run.status == "done"
    dump = json.dumps([list(run.events), run.result, run.cmd, run.error_summary, run.to_dict()])
    assert TOKEN not in dump
    assert [e["message"] for e in run.events if e.get("type") == "step"][0].startswith("token issued for")
    argv = calls(env)[-1]["argv"]
    assert argv[0] == "token" and argv[argv.index("--name") + 1] == "web/web"
    assert argv[argv.index("--account") + 1] == "equipe-a/ci" and argv[argv.index("--seconds") + 1] == "600"
    assert argv[argv.index("--by") + 1] == "alice"
    # base (avec son journal WAL) et journaux : la valeur n'y est pas
    with sqlite3.connect(store["db"]) as con:
        rows = con.execute("select * from actions where id = ?", (run.id,)).fetchall()
    assert rows and TOKEN not in json.dumps(rows, default=str)
    assert files_containing(store["tmp"], TOKEN) == []


def test_the_default_duration_is_one_hour(env, store):
    r = ask(env, seconds=None)
    assert r.status_code == 200
    argv = calls(env)[-1]["argv"]
    assert argv[argv.index("--seconds") + 1] == "3600"


@pytest.mark.parametrize("over,status,code", [
    ({"seconds": 599}, 400, None),
    ({"seconds": 86401}, 400, None),
    ({"seconds": "une heure"}, 400, None),
    ({"cluster": "Web/web"}, 400, None),
    ({"account": "ci"}, 400, None),
    ({"account": "equipe-a/CI"}, 400, None),
    ({"extra": 1}, 400, None),
])
def test_refusals_start_nothing(env, store, over, status, code):
    r = ask(env, **over)
    assert r.status_code == status
    assert calls(env) == [] and "asked" not in env.right


@pytest.mark.parametrize("seconds", [600, 86400])
def test_the_bounds_are_accepted(env, store, seconds):
    assert ask(env, seconds=seconds).status_code == 200


def test_a_viewer_is_refused(env, store):
    env.right["role"] = "viewer"
    r = ask(env)
    assert r.status_code == 403 and r.get_json()["required"] == "operator"
    assert calls(env) == []


@pytest.mark.parametrize("answer,status", [("no", 403), ("", 502)])
def test_an_operator_without_the_secret_right_is_refused(env, store, answer, status):
    env.right["answer"] = answer
    r = ask(env)
    assert r.status_code == status
    if status == 403:
        assert r.get_json()["code"] == "child-admin-required"
    assert calls(env) == [] and TOKEN not in r.get_data(as_text=True)


def test_a_failed_tool_answers_an_error_without_a_token(env, store, monkeypatch):
    monkeypatch.setenv("FAKE_TOKEN_RC", "2")
    monkeypatch.setenv("FAKE_TOKEN", "")
    r = ask(env)
    assert r.status_code == 502 and "token" not in r.get_json()
    assert wait(r.get_json()["action_id"]).status == "error"


def test_the_sink_never_reaches_the_result_even_with_capture(store):
    """Le relais : avec `sink`, la sortie standard ne va jamais dans
    `run.result` (chemin de `capture`)."""
    got = []
    run = wapp.ActionRun("abcdefabcdef", "capi-token:web/web", "harvlab2", ["harvester-capi"])
    cmd = [sys.executable, "-c", f"import json;print(json.dumps({{'token': {TOKEN!r}}}))"]
    wapp._vm_transfer_runner(run, cmd, capture=True, sink=got.append)
    assert got == [{"token": TOKEN}] and run.status == "done"
    assert TOKEN not in json.dumps([run.result, list(run.events)])
    assert files_containing(store["tmp"], TOKEN) == []
