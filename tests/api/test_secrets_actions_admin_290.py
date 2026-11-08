"""2.9.0, relecture finale I9 : les actions du magasin de secrets
(`secrets:*`) portent dans leur libellé, leurs étapes et leur résultat des
noms et chemins de secrets (balayage des orphelins : chaque classe/chemin ;
`secrets:rotate-ssh-kvm:<hôte>`, adresse du serveur, `snapshot_dir`,
`key_id`). Ces métadonnées sont réservées à l'administrateur, comme
`/api/secrets/secrets` et `/versions` : un lecteur ou un opérateur ne voit
ces actions ni dans la liste, ni dans le détail, ni dans le flux, ni dans
l'Activité (facettes comprises)."""
import base64
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "web"))

SECRET_PATH = "kubeconfig/hidden-cluster"


@pytest.fixture
def world(tmp_path, monkeypatch):
    import app as wapp
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("")
    roles = tmp_path / "roles.yaml"
    roles.write_text("default_role: viewer\nusers:\n  patronne: admin\n  operatrice: operator\n  curieux: viewer\n")
    monkeypatch.setattr(wapp, "auth_configured", lambda: True)
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)
    monkeypatch.setattr(wapp, "ROLES_PATH", roles)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: True)
    monkeypatch.setattr(wapp, "_RATELIMIT_DISABLED", True, raising=False)
    monkeypatch.setattr(wapp, "ACTIONS", {})
    monkeypatch.setattr(wapp, "ACTIONS_DB", tmp_path / "actions.db")
    wapp._actions_init_db()
    runs = {}
    for label in ("secrets:sweep-orphans", "secrets:rotate-ssh-kvm:node2", "vm-start:demo"):
        r = wapp.ActionRun(wapp.uuid.uuid4().hex[:12], label, "", [])
        r.result = {"output": {"orphans": [SECRET_PATH]}} if label.startswith("secrets:") else {}
        r.emit({"type": "step", "step_id": "orphans", "status": "done", "message": SECRET_PATH, "ts": time.time()})
        r.status, r.exit_code = "done", 0
        wapp.ACTIONS[r.id] = r
        r.close()
        runs[label] = r
    return wapp, runs


def get(wapp, user, path):
    token = base64.b64encode(f"{user}:x".encode()).decode()
    with wapp.app.test_client() as c:
        r = c.get(path + ("&" if "?" in path else "?") + "fresh=1", headers={"Authorization": f"Basic {token}"})
        return r.status_code, r.get_data(as_text=True)


@pytest.mark.parametrize("user", ["curieux", "operatrice"])
def test_secrets_actions_hidden_below_admin(world, user):
    wapp, runs = world
    st, body = get(wapp, user, "/api/actions")
    assert st == 200 and SECRET_PATH not in body and "node2" not in body and "secrets:" not in body
    assert runs["vm-start:demo"].id in body                    # les autres actions restent visibles
    st, body = get(wapp, user, "/api/activity")
    assert st == 200 and SECRET_PATH not in body and "secrets:" not in body
    for label in ("secrets:sweep-orphans", "secrets:rotate-ssh-kvm:node2"):
        rid = runs[label].id
        st, body = get(wapp, user, f"/api/action/{rid}")
        assert st == 404 and SECRET_PATH not in body
        st, body = get(wapp, user, f"/api/stream/{rid}")
        assert st == 404 and SECRET_PATH not in body


def test_secrets_actions_hidden_below_admin_after_a_restart(world):
    """Rangées en base seulement (mémoire purgée, console redémarrée)."""
    wapp, runs = world
    wapp.ACTIONS.clear()
    st, body = get(wapp, "curieux", "/api/activity")
    assert st == 200 and "secrets:" not in body and SECRET_PATH not in body
    rid = runs["secrets:sweep-orphans"].id
    assert get(wapp, "curieux", f"/api/action/{rid}")[0] == 404
    assert get(wapp, "curieux", f"/api/stream/{rid}")[0] == 404
    st, body = get(wapp, "patronne", f"/api/action/{rid}")
    assert st == 200 and json.loads(body)["action"] == "secrets:sweep-orphans"


def test_admin_sees_secrets_actions(world):
    wapp, runs = world
    st, body = get(wapp, "patronne", "/api/actions")
    assert st == 200 and "secrets:rotate-ssh-kvm:node2" in body
    st, body = get(wapp, "patronne", "/api/activity")
    assert "secrets:sweep-orphans" in body
    rid = runs["secrets:sweep-orphans"].id
    st, body = get(wapp, "patronne", f"/api/stream/{rid}")
    assert st == 200 and SECRET_PATH in body


@pytest.mark.parametrize("user,seen", [("curieux", False), ("operatrice", False), ("patronne", True)])
def test_secrets_agent_logs_are_for_admins(world, tmp_path, monkeypatch, user, seen):
    """Relecture des corrections finales m10 : les journaux de l'agent des
    secrets (identifiants de clé, instantanés, déclarations, politiques)
    suivent la règle des actions `secrets:*`."""
    wapp, _runs = world
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "secrets-agent-20261108-101010.log").write_text(f"key hops-20261108-1 {SECRET_PATH}\n")
    (logs / "20261108-101010-harv1-status.log").write_text("ordinary\n")
    monkeypatch.setattr(wapp, "LOG_DIR", logs)
    st, body = get(wapp, user, "/api/activity")
    assert st == 200 and ("secrets-agent-" in body) is seen and "harv1-status" in body
    st, body = get(wapp, user, "/api/logs/secrets-agent-20261108-101010.log")
    assert (st == 200) is seen and (SECRET_PATH in body) is seen
    assert get(wapp, user, "/api/logs/20261108-101010-harv1-status.log")[0] == 200
