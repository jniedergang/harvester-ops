"""v1.79.0 : Rancher réglés dans l'interface, de bout en bout (application réelle,
Rancher simulé).

Points d'entrée (droits, limites, secrets jamais rendus), test d'un Rancher,
connexion directe (succès, refus, origine), déconnexion qui supprime le jeton
dans Rancher, enregistrement et désenregistrement SSO, chart Harvester RBAC
(état, installation suivie, refus de version), données de la page de
connexion à plusieurs Rancher, prise en compte à chaud.

Les réponses simulées reprennent ce que Rancher Prime v2.14.1 a rendu en réel
le 01/10/2026 (voir web/rancher_admin.py).
"""

import base64
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).parent))
import app as wapp  # noqa: E402
import rancher_admin as radm  # noqa: E402
import rancher_sso as rs  # noqa: E402
from test_rancher_sso import URL, FakeRancher  # noqa: E402

ORIGIN = {"Origin": "http://localhost"}
BOSS = {"Authorization": "Basic " + base64.b64encode(b"boss:pw").decode()}
OIDC = "/k8s/clusters/local/apis/management.cattle.io/v3/oidcclients"


class Rancher(FakeRancher):
    """Le Rancher de la maquette, plus ce que la 1.79 lui demande."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.passwords = {"admin": "adminpw", "ana": "anapw"}
        self.tokens = {}                 # jeton -> (nom, utilisateur)
        self.logged_out = []
        self.version = "v2.14.1"
        self.kube = "v1.33.7+rke2r1"
        self.oidc = {}
        self.apps = []
        self.installs = []
        self.reachable = True

    def request(self, method, url, headers=None, data=None):
        if not self.reachable:
            raise rs.SSOError("rancher-unreachable", "connection refused")
        path = url[len(URL):]
        auth = (headers or {}).get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else None
        body = json.loads(data) if isinstance(data, str) and data else data
        if path == "/v3-public/authProviders":
            return 200, {"data": [{"id": "keycloakoidc", "type": "keyCloakOIDCProvider"},
                                  {"id": "local", "type": "localProvider"}]}
        if path == "/rancherversion":
            return 200, {"Version": self.version, "RancherPrime": "true"}
        if path == "/v3-public/localProviders/local?action=login":
            self.calls.append((method, url, {}, {"username": body["username"]}))
            if self.passwords.get(body["username"]) != body["password"]:
                return 401, {"code": "Unauthorized", "message": "authentication failed"}
            name = f"token-{len(self.tokens) + 1:05d}"
            self.tokens[f"{name}:secret"] = (name, body["username"])
            self.last_ttl = body.get("ttl")
            return 201, {"token": f"{name}:secret", "id": name, "type": "token",
                         "expiresAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 86400))}
        if path == "/v3/tokens?action=logout":
            self.logged_out.append(self.tokens.get(tok, (tok,))[0])
            self.tokens.pop(tok, None)
            return 200, {}
        if tok in self.tokens:
            user = self.tokens[tok][1]
            # identité : le compte de la connexion directe
            if path == "/v3/users?me=true":
                return 200, {"data": [{"id": "u-" + user, "username": user, "name": user}]}
            if path.startswith("/v3/globalrolebindings"):
                roles = ["admin"] if user == "admin" else ["user"]
                return 200, {"data": [{"userId": "u-" + user, "globalRoleId": r} for r in roles]}
            if user != "admin" and (path.startswith(OIDC) or path.startswith("/v1/catalog")):
                return 403, {"message": "forbidden"}
        if path == OIDC and method == "POST":
            name = body["metadata"]["name"]
            if name in self.oidc:
                return 409, {"message": "exists"}
            self.oidc[name] = dict(body, status={"clientID": "client-new1"})
            return 201, body
        if path.startswith(OIDC + "/"):
            name = path[len(OIDC) + 1:]
            if method == "GET":
                return (200, self.oidc[name]) if name in self.oidc else (404, {})
            if method == "PUT":
                self.oidc[name] = body
                return 200, body
            if method == "DELETE":
                return (200, {}) if self.oidc.pop(name, None) else (404, {})
        if path == "/k8s/clusters/local/api/v1/namespaces/cattle-oidc-client-secrets/secrets/client-new1":
            return 200, {"data": {"client-secret-1": base64.b64encode(b"generated-secret").decode()}}
        if path == "/v3/clusters/local":
            return 200, {"id": "local", "version": {"gitVersion": self.kube}}
        if path.startswith("/v1/catalog.cattle.io.apps?"):
            return 200, {"data": self.apps}
        if path == "/v1/catalog.cattle.io.clusterrepos/rancher-charts?link=index":
            return 200, {"entries": {"harvester-rbac": [{
                "name": "harvester-rbac", "version": "109.0.0+up0.1.1",
                "annotations": {"catalog.cattle.io/rancher-version": ">= 2.14.0-0 < 2.15.0-0",
                                "catalog.cattle.io/kube-version": ">= 1.23.0-0 < 1.36.0-0",
                                "catalog.cattle.io/release-name": "harvester-rbac"}}]}}
        if path == "/v1/catalog.cattle.io.clusterrepos/rancher-charts?action=install":
            self.installs.append(body)
            self.apps.append({"metadata": {"name": "harvester-rbac", "namespace": body["namespace"]},
                              "spec": {"chart": {"metadata": {"name": "harvester-rbac",
                                                              "version": body["charts"][0]["version"]}}},
                              "status": {"summary": {"state": "deployed"}}})
            return 201, {"operationName": "helm-operation-x", "operationNamespace": "cattle-system"}
        if path.startswith("/v1/catalog.cattle.io.apps/"):
            ns, name = path.split("/")[-2:]
            for a in self.apps:
                if a["metadata"]["name"] == name and a["metadata"]["namespace"] == ns:
                    return 200, a
            return 404, {}
        if path.startswith("/v1/catalog.cattle.io.operations/"):
            return 200, {"metadata": {"state": {"name": "successful"}}}
        if path.startswith("/v1/management.cattle.io.roletemplates"):
            installed = bool(self.apps)
            rts = [{"metadata": {"name": "rt-other"}, "displayName": "Cluster Owner"}]
            if installed:
                rts += [{"metadata": {"name": n, "annotations": {"meta.helm.sh/release-name": "harvester-rbac"}},
                         "displayName": d, "context": c} for n, d, c in (
                    ("virt-view-c", "View Virtualization Resources", "cluster"),
                    ("virt-manage-c", "Manage Virtualization Resources", "cluster"),
                    ("virt-view-p", "View Virtualization Resources", "project"),
                    ("virt-manage-p", "Manage Virtualization Resources", "project"))]
            return 200, {"data": rts}
        return super().request(method, url, headers=headers, data=data)


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    cfg = {"clusters": [{"name": "harv1", "kubeconfig": str(tmp_path / "kc")}]}
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("")
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: (u, p) in {("boss", "pw"), ("reader", "pw")})
    monkeypatch.setattr(wapp, "_htpasswd_users", lambda: ["boss", "reader"])
    ids = tmp_path / "ids"
    ids.mkdir()
    monkeypatch.setattr(wapp, "_SSO_SESSIONS", rs.Sessions(lambda: ids))
    monkeypatch.setattr(wapp, "_SSO_PENDING", rs.PendingLogins())
    monkeypatch.setattr(wapp, "_SSO_CLUSTER_IDS", {})
    monkeypatch.setattr(wapp, "_RANCHER_PROBES", {})
    monkeypatch.setattr(wapp, "_kube_system_uid", lambda entry: "uid-harv1")
    monkeypatch.setattr(wapp, "_node_uids", lambda entry: frozenset({"node-uid-1"}))
    fake = Rancher()
    monkeypatch.setattr(wapp, "_sso_http", lambda s: fake)
    monkeypatch.setattr(wapp, "_rancher_http", lambda s, timeout=15: fake)
    monkeypatch.setattr(radm.time, "sleep", lambda s: None)
    return {"fake": fake, "cfg": cfg, "state": state, "tmp": tmp_path, "ids": ids}


def add(c, **over):
    body = dict({"label": "Lab", "url": URL}, **over)
    r = c.post("/api/rancher/servers", json=body, headers=BOSS)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["server"]


def direct(c, user="admin", pw="adminpw", sid="lab", nxt="/#vms", headers=ORIGIN):
    return c.post(f"/auth/rancher/{sid}/direct", headers=headers,
                  data={"provider": "local", "username": user, "password": pw, "next": nxt})


# -- points d'entrée -------------------------------------------------------------

def test_crud_and_no_secret_in_answers(env, monkeypatch):
    with wapp.app.test_client() as c:
        assert c.get("/api/rancher/servers").status_code == 401
        x = add(c, ca="-----BEGIN CERTIFICATE-----\nAAA=\n-----END CERTIFICATE-----", session_hours=8)
        assert x["has_ca"] and x["session_hours"] == 8 and x["origin"] == "console" and x["editable"]
        d = c.get("/api/rancher/servers", headers=BOSS).get_json()
        assert [s["id"] for s in d["servers"]] == ["lab"] and "config_writable" in d
        r = c.put("/api/rancher/servers/lab", json={"default_role": "viewer", "direct_enabled": False}, headers=BOSS)
        assert r.status_code == 200 and r.get_json()["server"]["default_role"] == "viewer"
        assert c.put("/api/rancher/servers/lab", json={"session_hours": 0}, headers=BOSS).status_code == 400
        assert c.put("/api/rancher/servers/nope", json={}, headers=BOSS).status_code == 404
        text = json.dumps(c.get("/api/rancher/servers", headers=BOSS).get_json())
        assert "BEGIN CERTIFICATE" not in text and "ca_file" not in text and str(env["tmp"]) not in text
        assert c.delete("/api/rancher/servers/lab", headers=BOSS).get_json()["ok"]
        assert c.get("/api/rancher/servers", headers=BOSS).get_json()["servers"] == []


def test_writers_are_admins_only(env, monkeypatch):
    with wapp.app.test_client() as c:
        add(c)
        monkeypatch.setattr(wapp, "current_role", lambda: "operator")
        r = c.post("/api/rancher/servers", json={"label": "x", "url": "https://x.example.com"}, headers=BOSS)
        assert r.status_code == 403 and r.get_json()["required"] == "admin"
        for m, path in (("put", "/api/rancher/servers/lab"), ("delete", "/api/rancher/servers/lab"),
                        ("post", "/api/rancher/servers/lab/test"),
                        ("post", "/api/rancher/servers/lab/sso/register"),
                        ("post", "/api/rancher/servers/lab/rbac/install")):
            assert getattr(c, m)(path, json={}, headers=BOSS).status_code == 403, path
        # lire reste permis
        assert c.get("/api/rancher/servers", headers=BOSS).status_code == 200


def test_rate_limits_are_valid_strings():
    from limits import parse_many
    import inspect
    src = inspect.getsource(wapp)
    start = src.index("v1.79.0 : les Rancher réglés dans l'interface (Réglages")
    for spec in __import__("re").findall(r'_rate_limit\("([^"]+)"\)', src[start:start + 20000]):
        assert parse_many(spec)


def test_config_server_is_shown_read_only(env, tmp_path):
    secret = tmp_path / "sec"
    secret.write_text("s3cret")
    env["cfg"]["rancher"] = {"url": URL, "client_id": "client-abc", "client_secret_file": str(secret)}
    with wapp.app.test_client() as c:
        d = c.get("/api/rancher/servers", headers=BOSS).get_json()
        [x] = d["servers"]
        assert x["origin"] == "config" and not x["editable"] and x["sso"]["enabled"]
        assert "s3cret" not in json.dumps(d)
        r = c.delete(f"/api/rancher/servers/{x['id']}", headers=BOSS)
        assert r.status_code == 409
        assert r.get_json()["error"] == "declared by the operator in config.yaml, read-only for the console"


def test_the_test_endpoint(env):
    with wapp.app.test_client() as c:
        add(c)
        d = c.post("/api/rancher/servers/lab/test", headers=BOSS).get_json()
        assert d == {"ok": True, "version": "v2.14.1", "error": None,
                     "providers": [{"id": "keycloakoidc", "type": "keyCloakOIDCProvider", "enabled": True, "password": False},
                                   {"id": "local", "type": "localProvider", "enabled": True, "password": True}]}
        env["fake"].reachable = False
        d = c.post("/api/rancher/servers/lab/test", headers=BOSS).get_json()
        assert d["ok"] is False and "connection refused" in d["error"]
        assert c.post("/api/rancher/test", json={"url": "http://x"}, headers=BOSS).status_code == 400


# -- connexion directe -------------------------------------------------------------

def test_direct_login_opens_a_session_like_sso(env):
    fake = env["fake"]
    with wapp.app.test_client() as c:
        add(c, session_hours=6)
        r = direct(c)
        assert r.status_code == 302 and r.headers["Location"] == "/#vms"
        assert c.get_cookie(wapp.RANCHER_LAST_COOKIE).value == "lab"
        assert fake.last_ttl == 6 * 3600 * 1000
        who = c.get("/api/whoami").get_json()
        assert who["auth"] == "rancher" and who["user"] == "admin@rancher" and who["role"] == "admin"
        assert who["session"]["kind"] == "direct" and who["session"]["renewable"] is False
        assert who["session"]["server"] == "lab"
        assert "secret" not in json.dumps(who)
        sid = c.get_cookie(rs.COOKIE).value
    # le cluster passe par le mandataire de Rancher avec le jeton de la personne
    with wapp.app.test_request_context("/api/x", headers={"Cookie": f"{rs.COOKIE}={sid}"}):
        kc = json.loads(Path(wapp._kubectl_for_cluster("harv1")).read_text())
        assert kc["clusters"][0]["cluster"]["server"] == URL + "/k8s/clusters/c-sg2q6"
        assert Path(kc["users"][0]["user"]["tokenFile"]).read_text() == "token-00001:secret"
    assert wapp._SSO_CLUSTER_IDS == {"harv1@lab": "c-sg2q6"}


def test_direct_login_role_follows_the_server_default(env):
    with wapp.app.test_client() as c:
        add(c, default_role="viewer")
        direct(c, user="ana", pw="anapw")
        assert c.get("/api/whoami").get_json()["role"] == "viewer"


def test_direct_login_refusals_do_not_leak(env, caplog):
    with wapp.app.test_client() as c:
        add(c)
        r = direct(c, pw="wrong")
        assert r.status_code == 302
        q = dict(parse_qsl(urlparse(r.headers["Location"]).query))
        assert q["error"] == "bad-credentials" and q["rancher"] == "lab"
        assert c.get_cookie(rs.COOKIE) is None
        # un compte inconnu reçoit la même réponse
        r2 = direct(c, user="ghost", pw="x")
        assert dict(parse_qsl(urlparse(r2.headers["Location"]).query))["error"] == "bad-credentials"
        # ni Origin ni Referer : refusé
        assert direct(c, headers={}).status_code == 403
        assert direct(c, headers={"Origin": "https://evil.example"}).status_code == 403
        # Rancher inconnu ou connexion directe coupée
        assert "rancher-unknown" in direct(c, sid="nope").headers["Location"]
        c.put("/api/rancher/servers/lab", json={"direct_enabled": False}, headers=BOSS)
        assert "direct-disabled" in direct(c).headers["Location"]
    assert "wrong" not in caplog.text and "adminpw" not in caplog.text


def test_logout_deletes_the_direct_token_in_rancher(env):
    fake = env["fake"]
    with wapp.app.test_client() as c:
        add(c)
        direct(c)
        assert c.post("/logout", headers=ORIGIN).status_code == 200
        assert fake.logged_out == ["token-00001"] and fake.tokens == {}
        assert c.get("/").status_code == 302


def test_a_direct_session_ends_with_its_token(env):
    with wapp.app.test_client() as c:
        add(c)
        direct(c)
        sess = wapp._sso_store().all()[0]
        sess.token_expires = time.time() - 1
        r = c.get("/api/whoami")
        assert r.status_code == 401 and r.get_json()["error"] == "session expired"
        assert wapp._sso_store().all() == []


def test_removing_a_server_closes_its_sessions(env):
    with wapp.app.test_client() as c:
        add(c)
        direct(c)
        assert c.get("/api/whoami").get_json()["auth"] == "rancher"
        (env["state"] / "rancher.d" / "lab.yaml").unlink()          # à chaud
        assert c.get("/api/whoami").status_code == 401


# -- SSO par Rancher réglé ---------------------------------------------------------

def test_sso_register_login_and_unregister(env, caplog):
    fake = env["fake"]
    with wapp.app.test_client() as c:
        add(c)
        r = c.post("/api/rancher/servers/lab/sso/register", headers=dict(BOSS, Origin="https://harvops.example"),
                   json={"admin_user": "admin", "admin_password": "adminpw"})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert d["client_id"] == "client-new1" and d["redirect_uri"] == "https://harvops.example/auth/rancher/callback"
        assert "generated-secret" not in json.dumps(d)
        oc = fake.oidc["harvester-ops-harvops-example"]
        assert oc["spec"]["redirectURIs"] == ["https://harvops.example/auth/rancher/callback"]
        assert oc["spec"]["refreshTokenExpirationSeconds"] == 12 * 3600
        assert fake.tokens == {} and fake.logged_out == ["token-00001"]     # jeton d'admin supprimé
        sec = env["state"] / "rancher.d" / "lab.oidc-secret"
        assert sec.read_text().strip() == "generated-secret"
        x = c.get("/api/rancher/servers", headers=BOSS).get_json()["servers"][0]
        assert x["sso"]["enabled"] and x["sso"]["client_id"] == "client-new1" and x["sso"]["registered_at"]
        # le SSO de ce Rancher : le state porte son id
        r = c.get("/auth/rancher/lab/login")
        assert r.status_code == 302
        q = dict(parse_qsl(urlparse(r.headers["Location"]).query))
        assert q["client_id"] == "client-new1"
        assert q["redirect_uri"] == "https://harvops.example/auth/rancher/callback"
        assert wapp._SSO_PENDING._d[q["state"]]["server"] == "lab"
        # désenregistrer
        r = c.post("/api/rancher/servers/lab/sso/unregister", headers=BOSS,
                   json={"admin_user": "admin", "admin_password": "adminpw"})
        assert r.status_code == 200 and r.get_json()["removed_from_rancher"] is True
        assert fake.oidc == {} and not sec.exists()
        assert "sso-disabled" in c.get("/auth/rancher/lab/login").headers["Location"]
    assert "adminpw" not in caplog.text and "generated-secret" not in caplog.text


def test_sso_callback_of_a_console_server(env, monkeypatch):
    """Le retour du SSO retrouve le Rancher par le `state`."""
    fake = env["fake"]
    with wapp.app.test_client() as c:
        add(c)
        wapp._RANCHER_STORE.set_sso(env["cfg"], "lab", "client-abc", "s3cret",
                                    "http://localhost/auth/rancher/callback", "harvester-ops-localhost")
        r = c.get("/auth/rancher/lab/login?next=/%23hosts")
        q = dict(parse_qsl(urlparse(r.headers["Location"]).query))
        fake.nonce = q["nonce"]
        r = c.get(f"/auth/rancher/callback?code=c1&state={q['state']}")
        assert r.status_code == 302 and r.headers["Location"] == "/#hosts"
        who = c.get("/api/whoami").get_json()
        assert who["session"]["server"] == "lab" and who["session"]["kind"] == "sso"
        assert c.get_cookie(wapp.RANCHER_LAST_COOKIE).value == "lab"


def test_sso_register_refusals(env, tmp_path):
    with wapp.app.test_client() as c:
        add(c)
        r = c.post("/api/rancher/servers/lab/sso/register", headers=BOSS,
                   json={"admin_user": "admin", "admin_password": "nope"})
        assert r.status_code == 401 and "nope" not in json.dumps(r.get_json())
        r = c.post("/api/rancher/servers/lab/sso/register", headers=BOSS,
                   json={"admin_user": "ana", "admin_password": "anapw"})
        assert r.status_code == 403 and "administrator" in r.get_json()["error"]
        assert c.post("/api/rancher/servers/lab/sso/register", headers=BOSS, json={}).status_code == 400
        secret = tmp_path / "s"
        secret.write_text("x")
        env["cfg"]["rancher"] = {"url": "https://other.example.com", "client_id": "c", "client_secret_file": str(secret)}
        r = c.post("/api/rancher/servers/other-example-com/sso/register", headers=BOSS,
                   json={"admin_user": "admin", "admin_password": "adminpw"})
        assert r.status_code == 409


# -- chart Harvester RBAC ---------------------------------------------------------

ADMIN = {"admin_user": "admin", "admin_password": "adminpw"}


def test_rbac_status_and_install(env):
    fake = env["fake"]
    with wapp.app.test_client() as c:
        add(c)
        st = c.post("/api/rancher/servers/lab/rbac/status", json=ADMIN, headers=BOSS).get_json()
        assert st["installed"] is False and st["available_version"] == "109.0.0+up0.1.1"
        assert st["compatible"] is True and st["reason"] is None and st["namespace"] == "default"
        assert st["rancher_version"] == "v2.14.1" and st["kube_version"] == "v1.33.7+rke2r1"
        assert "entry" not in st
        r = c.post("/api/rancher/servers/lab/rbac/install", json=ADMIN, headers=BOSS)
        assert r.status_code == 202
        aid = r.get_json()["action_id"]
        run = wapp.ACTIONS[aid]
        for _ in range(200):
            if run.status in ("done", "error"):
                break
            time.sleep(0.02)
        assert run.status == "done", run.error_summary
        assert run.action == "rancher-rbac-install:lab"
        assert [x["display_name"] for x in run.result["roles"]].count("View Virtualization Resources") == 2
        [inst] = fake.installs
        assert inst["namespace"] == "default"
        assert inst["charts"][0] == {"chartName": "harvester-rbac", "version": "109.0.0+up0.1.1",
                                     "releaseName": "harvester-rbac",
                                     "annotations": inst["charts"][0]["annotations"], "values": {}}
        assert fake.tokens == {}                    # jetons d'admin supprimés
        st = c.post("/api/rancher/servers/lab/rbac/status", json=ADMIN, headers=BOSS).get_json()
        assert st["installed"] and st["version"] == "109.0.0+up0.1.1" and len(st["roles"]) == 4
        r = c.post("/api/rancher/servers/lab/rbac/install", json=ADMIN, headers=BOSS)
        assert r.status_code == 409 and "already installed" in r.get_json()["error"]


@pytest.mark.parametrize("attr, value, word", [("version", "v2.13.4", "Rancher v2.13.4"),
                                               ("kube", "v1.36.3+rke2r1", "Kubernetes v1.36.3")])
def test_rbac_refused_on_a_version_mismatch(env, attr, value, word):
    setattr(env["fake"], attr, value)
    with wapp.app.test_client() as c:
        add(c)
        st = c.post("/api/rancher/servers/lab/rbac/status", json=ADMIN, headers=BOSS).get_json()
        assert st["compatible"] is False and word in st["reason"]
        r = c.post("/api/rancher/servers/lab/rbac/install", json=ADMIN, headers=BOSS)
        assert r.status_code == 409 and word in r.get_json()["error"]
        assert env["fake"].installs == [] and env["fake"].tokens == {}


# -- page de connexion ------------------------------------------------------------

def test_login_page_lists_the_servers(env, monkeypatch):
    seen = {}
    real = wapp.render_template

    def spy(name, **ctx):
        seen.update(ctx)
        return real(name, **ctx)
    monkeypatch.setattr(wapp, "render_template", spy)
    with wapp.app.test_client() as c:
        add(c)
        add(c, label="Second", url="https://rancher.example.com/second")
        wapp._RANCHER_STORE.set_sso(env["cfg"], "second", "client-x", "s", "http://localhost/auth/rancher/callback", "n")
        c.put("/api/rancher/servers/second", json={"direct_enabled": False}, headers=BOSS)
        c.set_cookie(wapp.RANCHER_LAST_COOKIE, "second")
        assert c.get("/login").status_code == 200
    assert seen["rancher_last"] == "second"
    assert seen["rancher_servers"] == [
        {"id": "lab", "label": "Lab", "sso": False, "direct": True,
         "providers": [{"id": "local", "label": "Local"}], "available": True, "unavailable": False},
        {"id": "second", "label": "Second", "sso": True, "direct": False, "providers": [],
         "available": True, "unavailable": False}]


def test_an_unreachable_rancher_does_not_block_the_login_page(env, monkeypatch):
    seen = {}
    real = wapp.render_template
    monkeypatch.setattr(wapp, "render_template", lambda n, **ctx: (seen.update(ctx), real(n, **ctx))[1])

    def slow(server, timeout=3, force=False):
        time.sleep(10)
    monkeypatch.setattr(wapp, "_rancher_probe", slow)
    monkeypatch.setattr(wapp, "LOGIN_PROBE_TIMEOUT", 0.2)
    with wapp.app.test_client() as c:
        add(c)
        t0 = time.time()
        assert c.get("/login").status_code == 200
        assert time.time() - t0 < 2
    assert seen["rancher_servers"][0]["unavailable"] is True and seen["rancher_servers"][0]["direct"] is False


def test_hot_reload_without_restart(env):
    with wapp.app.test_client() as c:
        assert c.get("/api/whoami", headers=BOSS).get_json()["rancher_login"] is False
        add(c)
        assert c.get("/api/whoami", headers=BOSS).get_json()["rancher_login"] is True
        r = direct(c)
        assert r.status_code == 302 and "error" not in r.headers["Location"]
