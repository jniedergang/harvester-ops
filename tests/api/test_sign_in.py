"""v1.57.0 : la console exige une connexion.

Demandé par l'exploitant en voyant le menu « Console ouverte, aucune
connexion configurée » : « on ne peut pas laisser n'importe qui interagir avec
les clusters ». Plus de mode ouvert par défaut ; au premier démarrage, le
premier administrateur se crée par /setup avec un jeton lu sur le disque du
serveur ; les comptes locaux se connectent par un formulaire (session, cookie
HttpOnly) et se déconnectent.
"""

import base64
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import accounts as acc  # noqa: E402
import app as wapp  # noqa: E402

ORIGIN = {"Origin": "http://localhost"}
PW = "correct horse battery"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """Une console neuve : aucun compte, pas de mode ouvert demandé."""
    monkeypatch.setattr(wapp, "AUTH_OPEN_ALLOWED", False)
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "no-htpasswd")
    monkeypatch.setattr(wapp, "ROLES_PATH", tmp_path / "no-roles.yaml")
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    monkeypatch.setattr(wapp, "ACCOUNTS_PATH", tmp_path / "state" / "accounts.json")
    monkeypatch.setattr(wapp, "_ACCOUNTS", {"store": None})
    monkeypatch.setattr(wapp, "_LOCAL_SESSIONS", acc.LocalSessions())
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [{"name": "harv1", "kubeconfig": "/kc"}]})
    return tmp_path


def setup_admin(c, fresh, name="alice"):
    c.get("/setup")
    token = (fresh / "state" / "setup-token").read_text().strip()
    return c.post("/setup", data={"token": token, "username": name, "password": PW, "confirm": PW},
                  headers=ORIGIN)


def sign_in(c, name, pw=PW):
    return c.post("/login/local", data={"username": name, "password": pw}, headers=ORIGIN)


# -- premier démarrage ------------------------------------------------------------

def test_a_console_without_accounts_waits_for_its_administrator(fresh):
    with wapp.app.test_client() as c:
        assert c.get("/").headers["Location"].endswith("/setup")
        assert c.get("/login").headers["Location"].endswith("/setup")
        r = c.get("/api/clusters")
        assert r.status_code == 401 and r.get_json()["error"] == "setup required"
        page = c.get("/setup").get_data(as_text=True)
        tok = fresh / "state" / "setup-token"
        assert str(tok) in page and tok.exists()
        assert oct(os.stat(tok).st_mode & 0o777) == "0o600"
        assert tok.read_text().strip() not in page              # le jeton ne s'affiche jamais


def test_the_setup_needs_the_token_from_the_disk(fresh):
    with wapp.app.test_client() as c:
        c.get("/setup")
        r = c.post("/setup", data={"token": "guess", "username": "alice", "password": PW, "confirm": PW},
                   headers=ORIGIN)
        assert r.status_code == 401 and not wapp._accounts().exists()
        token = (fresh / "state" / "setup-token").read_text().strip()
        r = c.post("/setup", data={"token": token, "username": "alice", "password": PW, "confirm": "other"},
                   headers=ORIGIN)
        assert r.status_code == 400 and 'data-code="mismatch"' in r.get_data(as_text=True)
        r = c.post("/setup", data={"token": token, "username": "alice", "password": "short", "confirm": "short"},
                   headers=ORIGIN)
        assert r.status_code == 400 and 'data-code="password-short"' in r.get_data(as_text=True)
        r = c.post("/setup", data={"token": token, "username": "alice", "password": PW, "confirm": PW},
                   headers={"Origin": "https://evil.example"})
        assert r.status_code == 403


def test_the_first_administrator_is_signed_in_and_the_door_closes(fresh):
    with wapp.app.test_client() as c:
        r = setup_admin(c, fresh)
        assert r.status_code == 302 and r.headers["Location"] == "/"
        cookie = [h for h in r.headers.getlist("Set-Cookie") if h.startswith(acc.COOKIE + "=")][0]
        assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
        assert not (fresh / "state" / "setup-token").exists()
        who = c.get("/api/whoami").get_json()
        assert who["user"] == "alice" and who["role"] == "admin" and who["auth_via"] == "session"
        assert who["password_managed"] is True
        assert oct(os.stat(fresh / "state" / "accounts.json").st_mode & 0o777) == "0o600"
        assert PW not in (fresh / "state" / "accounts.json").read_text()
        assert c.get("/setup").headers["Location"].endswith("/login")
        assert c.get("/login").headers["Location"] == "/"      # déjà connecté


# -- connexion, déconnexion ---------------------------------------------------------

def test_a_browser_is_sent_to_the_login_page_not_a_basic_prompt(fresh):
    wapp._accounts().create("alice", PW, "admin")
    with wapp.app.test_client() as c:
        r = c.get("/")
        assert r.status_code == 302 and r.headers["Location"].endswith("/login")
        r = c.get("/api/clusters")
        assert r.status_code == 401 and "WWW-Authenticate" not in r.headers
        assert r.get_json()["login"] == "/login"
        # un client d'API qui présente de mauvais identifiants reçoit le défi
        bad = {"Authorization": "Basic " + base64.b64encode(b"alice:nope").decode()}
        assert "WWW-Authenticate" in c.get("/api/clusters", headers=bad).headers
        ok = {"Authorization": "Basic " + base64.b64encode(f"alice:{PW}".encode()).decode()}
        assert c.get("/api/clusters", headers=ok).status_code == 200
        # un navigateur qui garde un mot de passe Basic (avant la 1.57) est
        # reconnu comme tel : sa déconnexion lui fera aussi l'oublier
        assert c.get("/api/whoami", headers=ok).get_json()["auth_via"] == "basic"


def test_sign_in_then_sign_out(fresh):
    wapp._accounts().create("alice", PW, "operator")
    with wapp.app.test_client() as c:
        r = sign_in(c, "alice", "wrong-password-x")
        assert r.status_code == 401 and 'data-code="bad-credentials"' in r.get_data(as_text=True)
        r = c.post("/login/local", data={"username": "alice", "password": PW, "next": "//evil.example/x"},
                   headers=ORIGIN)
        assert r.headers["Location"] == "/"                   # jamais hors de la console
        assert c.get("/api/whoami").get_json()["role"] == "operator"
        r = c.post("/logout", headers=ORIGIN)
        assert r.get_json()["login"] == "/login?signed_out=1"
        assert c.get("/api/whoami").status_code == 401
        assert "login-signed-out" in c.get("/login?signed_out=1").get_data(as_text=True)


def test_a_session_write_from_another_site_is_refused(fresh):
    wapp._accounts().create("alice", PW, "admin")
    with wapp.app.test_client() as c:
        sign_in(c, "alice")
        r = c.post("/api/users", json={"name": "bob", "password": PW, "role": "viewer"},
                   headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and not wapp._accounts().has("bob")
        assert c.post("/logout", headers={"Origin": "https://evil.example"}).status_code == 403


def test_the_open_mode_must_be_asked_and_ends_with_the_first_account(fresh, monkeypatch):
    monkeypatch.setattr(wapp, "AUTH_OPEN_ALLOWED", True)
    with wapp.app.test_client() as c:
        assert c.get("/api/whoami").get_json()["auth"] == "open"
        wapp._accounts().create("alice", PW, "admin")
        assert c.get("/api/whoami").status_code == 401


# -- comptes ----------------------------------------------------------------------------

def test_an_administrator_manages_the_accounts(fresh):
    with wapp.app.test_client() as c:
        setup_admin(c, fresh)
        r = c.post("/api/users", json={"name": "bob", "password": PW, "role": "operator"}, headers=ORIGIN)
        assert r.status_code == 201 and r.get_json()["action_id"]
        assert c.post("/api/users", json={"name": "bob", "password": PW, "role": "viewer"},
                      headers=ORIGIN).get_json()["code"] == "name-taken"
        assert c.post("/api/users", json={"name": "carl", "password": "short", "role": "viewer"},
                      headers=ORIGIN).get_json()["code"] == "password-short"
        names = {a["name"]: a for a in c.get("/api/users").get_json()["accounts"]}
        assert names["bob"]["role"] == "operator" and "hash" not in json.dumps(names)
        assert c.patch("/api/users/bob", json={"role": "viewer"}, headers=ORIGIN).status_code == 200
        # le dernier administrateur ne se retire pas, ni son rôle
        assert c.patch("/api/users/alice", json={"role": "viewer"}, headers=ORIGIN).get_json()["code"] == "last-admin"
        assert c.delete("/api/users/alice", headers=ORIGIN).get_json()["code"] == "self"
        acts = [a.action for a in wapp.ACTIONS.values() if str(a.action).startswith("account:")]
        assert "account:create:bob" in acts and "account:update:bob" in acts
        # aucun mot de passe dans l'activité
        assert all(PW not in json.dumps(a.to_dict()) for a in wapp.ACTIONS.values())


def test_a_reset_or_a_deletion_closes_the_sessions_of_the_account(fresh):
    wapp._accounts().create("alice", PW, "admin")
    wapp._accounts().create("bob", PW, "viewer")
    # deux navigateurs à la fois : clients sans « with », dont les contextes
    # de requête s'emboîteraient mal
    admin, bob = wapp.app.test_client(), wapp.app.test_client()
    if True:
        sign_in(admin, "alice")
        sign_in(bob, "bob")
        assert bob.get("/api/whoami").status_code == 200
        admin.patch("/api/users/bob", json={"password": "another long password"}, headers=ORIGIN)
        assert bob.get("/api/whoami").status_code == 401
        assert sign_in(bob, "bob", "another long password").status_code == 302
        admin.delete("/api/users/bob", headers=ORIGIN)
        assert bob.get("/api/whoami").status_code == 401


def test_a_viewer_changes_its_own_password_only(fresh):
    wapp._accounts().create("alice", PW, "admin")
    wapp._accounts().create("vic", PW, "viewer")
    c, other = wapp.app.test_client(), wapp.app.test_client()
    if True:
        sign_in(c, "vic")
        sign_in(other, "vic")
        assert c.get("/api/users").status_code == 403                  # la liste est aux admins
        r = c.post("/api/me/password", json={"current": "nope", "new": "brand new password"}, headers=ORIGIN)
        assert r.status_code == 403 and r.get_json()["code"] == "bad-current"
        r = c.post("/api/me/password", json={"current": PW, "new": "brand new password"}, headers=ORIGIN)
        assert r.status_code == 200
        assert c.get("/api/whoami").status_code == 200                 # cette session reste
        assert other.get("/api/whoami").status_code == 401             # les autres tombent
        assert wapp._accounts().verify("vic", "brand new password")


def test_an_installer_account_keeps_its_role_and_is_not_managed_here(fresh):
    from passlib.apache import HtpasswdFile
    ht = HtpasswdFile(str(fresh / "no-htpasswd"), new=True)
    ht.set_password("ops", PW)
    ht.save()
    (fresh / "no-roles.yaml").write_text("default_role: viewer\nusers:\n  ops: admin\n")
    with wapp.app.test_client() as c:
        assert sign_in(c, "ops").status_code == 302
        who = c.get("/api/whoami").get_json()
        assert who["role"] == "admin" and who["password_managed"] is False
        rows = {a["name"]: a for a in c.get("/api/users").get_json()["accounts"]}
        assert rows["ops"]["source"] == "htpasswd" and rows["ops"]["role"] == "admin"
        r = c.post("/api/me/password", json={"current": PW, "new": "brand new password"}, headers=ORIGIN)
        assert r.get_json()["code"] == "not-managed"


def test_the_session_ends_after_its_lifetime():
    t = [0.0]
    s = acc.LocalSessions(ttl=100, now=lambda: t[0])
    sid = s.open("alice")
    assert s.get(sid)["user"] == "alice"
    t[0] = 101
    assert s.get(sid) is None
