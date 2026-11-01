"""2.0.2 : revenir à une version précédente depuis l'onglet Mise à jour.

Même chemin qu'une mise à jour (archive signée préparée, agent de l'hôte) :
la cible doit être plus ancienne que la version qui tourne, la personne
retape son numéro, et la demande part avec allow_older, tracée par une
action à étapes. Réservé aux administrateurs."""
import base64
import json
from pathlib import Path

import pytest

from test_self_update_182 import console, keys, make_release, sign, upload  # noqa: F401

CURRENT = "1.81.0"   # version servie par la fixture console


def staged(c, keys, tmp_path, version, signed=True):
    (tmp_path / version).mkdir()
    a = make_release(tmp_path / version, version)
    upload(c, a)
    if signed:
        sign(keys, a)
        upload(c, Path(str(a) + ".sig"))
    return a


@pytest.fixture
def ready(console):
    wapp, state = console
    (state / "updates").mkdir(parents=True, exist_ok=True)
    (state / "updates" / "agent.json").write_text('{"installed": "1.81.0"}')
    return wapp, state


def request_file(state):
    return state / "updates" / "request.json"


def test_rollback_is_admin_only(console):
    wapp, _ = console
    assert wapp.required_role_for("/api/update/rollback", "POST") == "admin"


# admin passes the guard and meets the next refusal (nothing staged)
@pytest.mark.parametrize("user,code", [("curieux", 403), ("operatrice", 403), ("patronne", 404)])
def test_rollback_refused_below_admin(ready, tmp_path, monkeypatch, user, code):
    wapp, state = ready
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("")
    roles = tmp_path / "roles.yaml"
    roles.write_text("default_role: viewer\nusers:\n  patronne: admin\n  operatrice: operator\n  curieux: viewer\n")
    monkeypatch.setattr(wapp, "auth_configured", lambda: True)
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)
    monkeypatch.setattr(wapp, "ROLES_PATH", roles)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: True)
    token = base64.b64encode(f"{user}:x".encode()).decode()
    with wapp.app.test_client() as c:
        r = c.post("/api/update/rollback", json={"archive": "harvester-ops-1.80.0.tar.gz", "confirm": "1.80.0"},
                   headers={"Authorization": f"Basic {token}"})
    assert r.status_code == code
    assert not request_file(state).exists()


def test_rollback_without_an_older_signed_archive_is_refused(ready, keys, tmp_path):
    wapp, state = ready
    with wapp.app.test_client() as c:
        r = c.post("/api/update/rollback", json={})
        assert r.status_code == 409 and "no older signed release" in r.get_json()["error"]
        assert ".sig" in r.get_json()["hint"]
        staged(c, keys, tmp_path, "1.80.0", signed=False)      # plus ancienne mais non signée
        staged(c, keys, tmp_path, "1.82.0")                    # signée mais plus récente
        st = c.get("/api/update/status").get_json()
        assert st["rollback"] == []
        assert c.post("/api/update/rollback", json={}).status_code == 409
        r = c.post("/api/update/rollback", json={"archive": "harvester-ops-1.80.0.tar.gz", "confirm": "1.80.0"})
        assert r.status_code == 409 and "sig" in r.get_json()["error"]
        r = c.post("/api/update/rollback", json={"archive": "harvester-ops-1.82.0.tar.gz", "confirm": "1.82.0"})
        assert r.status_code == 409 and "not older" in r.get_json()["error"]
        r = c.post("/api/update/rollback", json={"archive": "harvester-ops-1.79.0.tar.gz", "confirm": "1.79.0"})
        assert r.status_code == 404
    assert not request_file(state).exists()


def test_same_version_is_not_a_rollback(ready, keys, tmp_path):
    wapp, state = ready
    with wapp.app.test_client() as c:
        staged(c, keys, tmp_path, CURRENT)
        assert c.get("/api/update/status").get_json()["rollback"] == []
        r = c.post("/api/update/rollback", json={"archive": f"harvester-ops-{CURRENT}.tar.gz", "confirm": CURRENT})
        assert r.status_code == 409
    assert not request_file(state).exists()


@pytest.mark.parametrize("typed", [None, "", "1.80", "1.80.0.", "v1.80.0", "1.81.0"])
def test_rollback_needs_the_target_version_typed(ready, keys, tmp_path, typed):
    wapp, state = ready
    with wapp.app.test_client() as c:
        staged(c, keys, tmp_path, "1.80.0")
        body = {"archive": "harvester-ops-1.80.0.tar.gz"}
        if typed is not None:
            body["confirm"] = typed
        r = c.post("/api/update/rollback", json=body)
        assert r.status_code == 400 and "type 1.80.0" in r.get_json()["error"]
    assert not request_file(state).exists()


def test_rollback_without_an_agent_is_refused(console, keys, tmp_path):
    wapp, state = console
    with wapp.app.test_client() as c:
        staged(c, keys, tmp_path, "1.80.0")
        r = c.post("/api/update/rollback", json={"archive": "harvester-ops-1.80.0.tar.gz", "confirm": "1.80.0"})
        assert r.status_code == 409 and "agent" in r.get_json()["error"]
    assert not request_file(state).exists()


def test_rollback_hands_an_older_release_to_the_agent(ready, keys, tmp_path):
    wapp, state = ready
    with wapp.app.test_client() as c:
        staged(c, keys, tmp_path, "1.79.0")
        staged(c, keys, tmp_path, "1.80.0")
        st = c.get("/api/update/status").get_json()
        assert st["rollback"] == ["1.79.0", "1.80.0"]
        assert [s["older"] for s in st["staged"]] == [True, True]
        r = c.post("/api/update/rollback", json={"archive": "harvester-ops-1.80.0.tar.gz", "confirm": " 1.80.0 "})
        assert r.status_code == 202, r.get_json()
        body = r.get_json()
        req = json.loads(request_file(state).read_text())
        assert req["archive"] == "harvester-ops-1.80.0.tar.gz" and req["version"] == "1.80.0"
        assert req["allow_older"] is True and req["kind"] == "rollback" and req["from"] == CURRENT
        run = wapp.ACTIONS[body["action_id"]]
        assert run.action == "console-update:rollback:1.80.0" and run.status == "done"
        steps = [e for e in run.events if e.get("type") == "step"]
        assert [s["step_id"] for s in steps] == ["check", "confirm", "request"]
        assert all(s["status"] == "done" for s in steps)
        # une seule demande à la fois
        r = c.post("/api/update/rollback", json={"archive": "harvester-ops-1.79.0.tar.gz", "confirm": "1.79.0"})
        assert r.status_code == 409 and "already requested" in r.get_json()["error"]


def test_rollback_waits_for_running_actions_unless_forced(ready, keys, tmp_path):
    wapp, state = ready
    with wapp.app.test_client() as c:
        staged(c, keys, tmp_path, "1.80.0")
        run = wapp.ActionRun("busy1", "shutdown", "harv1", [])
        run.status = "running"
        with wapp.ACTIONS_LOCK:
            wapp.ACTIONS["busy1"] = run
        body = {"archive": "harvester-ops-1.80.0.tar.gz", "confirm": "1.80.0"}
        r = c.post("/api/update/rollback", json=body)
        assert r.status_code == 409 and r.get_json()["busy"][0]["action"] == "shutdown"
        assert not request_file(state).exists()
        assert c.post("/api/update/rollback", json={**body, "force": True}).status_code == 202


def test_apply_keeps_its_contract(ready, keys, tmp_path):
    """La mise à jour ordinaire passe par le même dépôt de demande."""
    wapp, state = ready
    with wapp.app.test_client() as c:
        staged(c, keys, tmp_path, "1.82.0")
        r = c.post("/api/update/apply", json={"archive": "harvester-ops-1.82.0.tar.gz"})
        assert r.status_code == 202
        req = json.loads(request_file(state).read_text())
        assert req["allow_older"] is False and req["kind"] == "request"
        assert wapp.ACTIONS[r.get_json()["action_id"]].action == "console-update:request:1.82.0"
