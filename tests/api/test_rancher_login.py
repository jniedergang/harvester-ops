"""v1.50.0 : se connecter à la console par Rancher, de bout en bout.

L'application réelle, un Rancher simulé (celui de `test_rancher_sso`) : la
redirection vers Rancher, le retour, la session, le rôle, le kubeconfig qui
passe par le mandataire de Rancher, l'alimentation refusée, l'écriture
d'une autre origine refusée, la déconnexion qui révoque le jeton.
"""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).parent))
import app as wapp  # noqa: E402
import rancher_sso as rs  # noqa: E402
from test_rancher_sso import URL, FakeRancher  # noqa: E402

ORIGIN = {"Origin": "http://localhost"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    secret.write_text("s3cret")
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("")
    cfg = {"clusters": [{"name": "harv1", "kubeconfig": str(tmp_path / "kc")}],
           "rancher": {"url": URL, "client_id": "client-abc", "client_secret_file": str(secret),
                       "admin_groups": ["keycloakoidc_group://ops"]}}
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)
    ids = tmp_path / "ids"
    ids.mkdir()
    monkeypatch.setattr(wapp, "_SSO_SESSIONS", rs.Sessions(lambda: ids))
    monkeypatch.setattr(wapp, "_SSO_PENDING", rs.PendingLogins())
    monkeypatch.setattr(wapp, "_SSO_CLUSTER_IDS", {})
    monkeypatch.setattr(wapp, "_kube_system_uid", lambda entry: "uid-harv1")
    fake = FakeRancher()
    monkeypatch.setattr(wapp, "_sso_http", lambda s: fake)
    return {"fake": fake, "cfg": cfg, "ids": ids}


def sign_in(client, fake, **kw):
    r = client.get("/auth/rancher/login")
    assert r.status_code == 302
    loc = r.headers["Location"]
    q = dict(parse_qsl(urlparse(loc).query))
    fake.nonce = q["nonce"]
    for k, v in kw.items():
        setattr(fake, k, v)
    return client.get(f"/auth/rancher/callback?code=c1&state={q['state']}"), q


def test_without_a_session_the_page_goes_to_the_login(world):
    with wapp.app.test_client() as c:
        r = c.get("/")
        assert r.status_code == 302 and r.headers["Location"].endswith("/login")
        page = c.get("/login").get_data(as_text=True)
        assert "/auth/rancher/login" in page and "/login/local" in page


def test_the_redirect_to_rancher(world):
    with wapp.app.test_client() as c:
        r = c.get("/auth/rancher/login")
        loc = urlparse(r.headers["Location"])
        q = dict(parse_qsl(loc.query))
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == URL + "/oidc/authorize"
        assert q["redirect_uri"] == "http://localhost/auth/rancher/callback"
        assert q["code_challenge_method"] == "S256"
        assert "HttpOnly" in r.headers["Set-Cookie"] and rs.PENDING_COOKIE in r.headers["Set-Cookie"]


def test_a_rancher_administrator_signs_in(world):
    fake = world["fake"]
    with wapp.app.test_client() as c:
        r, _ = sign_in(c, fake)
        assert r.status_code == 302 and r.headers["Location"] == "/"
        cookie = [h for h in r.headers.getlist("Set-Cookie") if h.startswith(rs.COOKIE + "=")][0]
        assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
        who = c.get("/api/whoami").get_json()
        assert who["auth"] == "rancher" and who["user"] == "admin@rancher" and who["role"] == "admin"
        assert "token" not in json.dumps(who["session"]).replace("expires", "")
        # l'identité a été relue auprès de Rancher avec le jeton lui-même
        assert any(u.endswith("/v3/users?me=true") for m, u, *_ in fake.calls)


def test_the_cluster_is_reached_through_rancher_with_the_users_token(world):
    fake = world["fake"]
    with wapp.app.test_client() as c:
        sign_in(c, fake)
        sid = c.get_cookie(rs.COOKIE).value
    with wapp.app.test_request_context("/api/x", headers={"Cookie": f"{rs.COOKIE}={sid}"}):
        kc = json.loads(Path(wapp._kubectl_for_cluster("harv1")).read_text())
        assert kc["clusters"][0]["cluster"]["server"] == URL + "/k8s/clusters/c-sg2q6"
        assert rs.claims_of(kc["users"][0]["user"]["token"])["token"] == "token-oidc"
        assert wapp.identity_env(cluster="harv1")["HARVESTER_OPS_KUBECONFIG"]


def test_a_cluster_rancher_does_not_manage_is_not_offered(world, monkeypatch):
    monkeypatch.setattr(wapp, "_kube_system_uid", lambda entry: "uid-harv3")
    with wapp.app.test_client() as c:
        sign_in(c, world["fake"])
        sid = c.get_cookie(rs.COOKIE).value
    with wapp.app.test_request_context("/api/x", headers={"Cookie": f"{rs.COOKIE}={sid}"}):
        assert wapp._kubectl_for_cluster("harv1") is None


def test_roles_follow_rancher(world):
    fake = world["fake"]
    with wapp.app.test_client() as c:
        sign_in(c, fake, roles=("user",), username="ana")
        assert c.get("/api/whoami").get_json()["role"] == "operator"
    with wapp.app.test_client() as c:
        sign_in(c, fake, roles=("user",), groups=("keycloakoidc_group://ops",))
        assert c.get("/api/whoami").get_json()["role"] == "admin"


def test_power_sequencing_needs_a_local_account(world):
    with wapp.app.test_client() as c:
        sign_in(c, world["fake"])
        r = c.post("/api/action", json={"action": "shutdown", "cluster": "harv1"}, headers=ORIGIN)
        assert r.status_code == 403 and r.get_json()["code"] == "power-needs-local-account"


def test_a_write_from_another_origin_is_refused(world):
    with wapp.app.test_client() as c:
        sign_in(c, world["fake"])
        r = c.post("/api/action", json={"action": "ns-stop", "cluster": "harv1"},
                   headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and "cross-origin" in r.get_json()["hint"]
        r = c.post("/api/action", json={"action": "ns-stop", "cluster": "harv1"})
        assert r.status_code == 403                  # ni Origin ni Referer


def test_logout_forgets_the_session(world):
    fake = world["fake"]
    with wapp.app.test_client() as c:
        sign_in(c, fake)
        sid = c.get_cookie(rs.COOKIE).value
        assert c.post("/logout", headers={"Origin": "https://evil.example"}).status_code == 403
        r = c.post("/logout", headers=ORIGIN)
        assert r.status_code == 200 and wapp._sso_store().get(sid) is None
        assert list(world["ids"].iterdir()) == []   # kubeconfigs de la session effacés
        assert c.get("/").status_code == 302


@pytest.mark.parametrize("tamper, error", [
    ("state", "state-unknown"),
    ("nonce", "token-nonce"),
])
def test_a_tampered_return_is_refused(world, tamper, error):
    fake = world["fake"]
    with wapp.app.test_client() as c:
        r = c.get("/auth/rancher/login")
        q = dict(parse_qsl(urlparse(r.headers["Location"]).query))
        fake.nonce = "other" if tamper == "nonce" else q["nonce"]
        state = "forged" if tamper == "state" else q["state"]
        r = c.get(f"/auth/rancher/callback?code=c1&state={state}")
        assert r.headers["Location"] == f"/login?error={error}"
        assert c.get_cookie(rs.COOKIE) is None
        page = c.get(r.headers["Location"]).get_data(as_text=True)
        assert "login-error" in page


def test_a_local_account_still_works(world, monkeypatch):
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: u == "local")
    import base64
    with wapp.app.test_client() as c:
        h = {"Authorization": "Basic " + base64.b64encode(b"local:x").decode()}
        assert c.get("/api/whoami", headers=h).get_json()["auth"] == "local"
        assert c.get("/login/local", headers=h).status_code == 302
        assert c.get("/login/local").status_code == 401


def test_clusters_rancher_does_not_show_are_left_out_and_refused(world, monkeypatch):
    world["cfg"]["clusters"].append({"name": "harv3", "kubeconfig": "/nope"})
    monkeypatch.setattr(wapp, "_kube_system_uid",
                        lambda entry: "uid-harv1" if entry["name"] == "harv1" else "uid-harv3")
    with wapp.app.test_client() as c:
        sign_in(c, world["fake"])
        names = [x["name"] for x in c.get("/api/clusters").get_json()["clusters"]]
        assert names == ["harv1"]
        r = c.get("/api/status/harv3")
        assert r.status_code == 403 and r.get_json()["code"] == "cluster-not-in-rancher"


def test_a_support_bundle_needs_a_rancher_administrator(world):
    fake = world["fake"]
    with wapp.app.test_client() as c:
        sign_in(c, fake, roles=("user",))
        r = c.post("/api/support-bundle", json={}, headers=ORIGIN)
        assert r.status_code == 403 and r.get_json()["code"] == "bundle-needs-admin"


def test_the_status_script_acts_through_rancher(world, monkeypatch):
    seen = {}

    def fake_check_output(cmd, **kw):
        seen["env"] = kw.get("env", {})
        return b"{}"
    monkeypatch.setattr(wapp.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, timeout=2.0: True)
    with wapp.app.test_client() as c:
        sign_in(c, world["fake"])
        assert c.get("/api/status/harv1").status_code == 200
    kc = json.loads(Path(seen["env"]["HARVESTER_OPS_KUBECONFIG"]).read_text())
    assert kc["clusters"][0]["cluster"]["server"].endswith("/k8s/clusters/c-sg2q6")
