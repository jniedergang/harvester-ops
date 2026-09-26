"""v1.50.0 : se connecter par Rancher, la logique (`web/rancher_sso.py`).

Un Rancher simulé répond comme celui de la maquette du 25/09/2026 (Rancher
Prime 2.14.1) : jeton d'identité avec `sub` = id Rancher, jeton d'accès
utilisable sur l'API et le mandataire, renouvelé par le jeton de
rafraîchissement (Rancher refuse d'en dériver un plus long), `expires_in` en
nanosecondes.
"""

import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import rancher_sso as rs  # noqa: E402

URL = "https://rancher.example.com"


def jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.c2lnbmF0dXJl"


def settings(tmp_path, **over):
    secret = tmp_path / "secret"
    secret.write_text("s3cret\n")
    cfg = {"rancher": dict({"url": URL + "/", "client_id": "client-abc",
                            "client_secret_file": str(secret)}, **over)}
    return rs.settings(cfg)


class FakeRancher:
    """Ce que le vrai Rancher a répondu pendant la maquette."""

    def __init__(self, user="user-kk67j", username="admin", roles=("admin",), groups=(),
                 clusters=None, nonce=None):
        self.user, self.username, self.roles, self.groups = user, username, roles, groups
        self.clusters = clusters or {"c-sg2q6": "uid-harv1", "local": "uid-local"}
        self.nodes = {"c-sg2q6": ["node-uid-1"], "local": ["node-uid-local"]}
        # v1.56.0 : un membre du cluster, vu en réel, ne lit pas kube-system
        # mais lit les nœuds ; et Rancher peut refuser un cluster à un compte
        self.member = False
        self.no_access = set()
        self.nonce = nonce
        self.calls = []
        self.revoked = []

    def request(self, method, url, headers=None, data=None):
        self.calls.append((method, url, dict(headers or {}), data))
        path = url[len(URL):]
        if path == "/oidc/token":
            assert headers["Authorization"] == "Basic " + base64.b64encode(b"client-abc:s3cret").decode()
            claims = {"iss": URL + "/oidc", "aud": ["client-abc"], "sub": self.user,
                      "exp": time.time() + 600, "nonce": self.nonce, "auth_provider": "local"}
            if data["grant_type"] == "refresh_token":
                if data["refresh_token"] in self.revoked:
                    return 400, {"error": "invalid_grant"}
                self.refreshes = getattr(self, "refreshes", 0) + 1
                return 200, {"access_token": jwt(dict(claims, token="token-oidc", n=self.refreshes)),
                             "id_token": jwt(claims), "refresh_token": f"refresh-{self.refreshes}",
                             "expires_in": 600000000000}
            assert data["grant_type"] == "authorization_code" and data["code_verifier"]
            # vu sur Rancher 2.14 : expires_in en NANOSECONDES
            return 200, {"access_token": jwt(dict(claims, token="token-oidc")), "id_token": jwt(claims),
                         "refresh_token": "refresh-0", "expires_in": 600000000000}
        if path == "/v3/users?me=true":
            return 200, {"data": [{"id": self.user, "username": self.username, "name": "Default Admin"}]}
        if path.startswith("/v3/globalrolebindings"):
            return 200, {"data": [{"userId": self.user, "globalRoleId": r} for r in self.roles]}
        if path == "/v3/principals":
            return 200, {"data": [{"id": f"local://{self.user}", "principalType": "user"}]
                         + [{"id": g, "principalType": "group"} for g in self.groups]}
        if path.startswith("/v3/tokens"):
            # vu en réel : un jeton OIDC ne peut ni créer ni supprimer de jeton
            return 401, {"code": "Unauthorized", "message": "failed to retrieve auth token"}
        if path == "/v3/clusters":
            return 200, {"data": [{"id": c, "state": "active"} for c in self.clusters]
                         + [{"id": "c-down", "state": "unavailable"}]}
        if path.startswith("/k8s/clusters/c-down/"):
            raise AssertionError("an unavailable cluster must not be probed")
        for cid, uid in self.clusters.items():
            if cid in self.no_access and (path.startswith(f"/k8s/clusters/{cid}/")
                                          or path == f"/v3/clusters/{cid}"):
                return 403, {"message": f'clusters.management.cattle.io "{cid}" is forbidden'}
            if path == f"/v3/clusters/{cid}":
                return 200, {"id": cid, "state": "active"}
            if path == f"/k8s/clusters/{cid}/api/v1/namespaces/kube-system":
                if self.member:
                    return 403, {"message": 'namespaces "kube-system" is forbidden'}
                return 200, {"metadata": {"uid": uid}}
            if path == f"/k8s/clusters/{cid}/api/v1/nodes":
                return 200, {"items": [{"metadata": {"uid": u}} for u in self.nodes.get(cid, [])]}
        return 404, {}


# -- configuration -----------------------------------------------------------

def test_settings_need_url_client_and_a_readable_secret(tmp_path):
    s = settings(tmp_path)
    assert s["url"] == URL and s["issuer"] == URL + "/oidc" and s["client_secret"] == "s3cret"
    assert s["default_role"] == "operator" and s["session_seconds"] == 12 * 3600
    assert rs.settings({}) is None
    assert rs.settings({"rancher": {"url": URL, "client_id": "x", "client_secret_file": "/nope"}}) is None
    assert settings(tmp_path, default_role="root")["default_role"] == "operator"


# -- le code d'autorisation ------------------------------------------------------

def test_a_login_attempt_is_single_use_and_bound_to_the_browser(tmp_path):
    p = rs.PendingLogins()
    state, nonce, challenge, browser = p.start("/#vms")
    with pytest.raises(rs.SSOError) as e:
        p.take(state, "another-browser")
    assert e.value.code == "state-browser"
    state, nonce, challenge, browser = p.start()
    assert p.take(state, browser)["nonce"] == nonce
    with pytest.raises(rs.SSOError) as e:
        p.take(state, browser)                     # rejoué
    assert e.value.code == "state-unknown"


def test_a_login_attempt_expires():
    t = [1000.0]
    p = rs.PendingLogins(now=lambda: t[0])
    state, _, _, browser = p.start()
    t[0] += rs.PENDING_TTL + 1
    with pytest.raises(rs.SSOError):
        p.take(state, browser)


def test_the_authorize_url_uses_pkce(tmp_path):
    s = settings(tmp_path)
    verifier, challenge = rs.pkce_pair()
    assert base64.urlsafe_b64encode(__import__("hashlib").sha256(verifier.encode()).digest()).rstrip(b"=").decode() == challenge
    u = rs.authorize_url(s, "https://hops/auth/rancher/callback", "st", "no", challenge)
    q = dict(__import__("urllib.parse").parse.parse_qsl(u.split("?", 1)[1]))
    assert u.startswith(URL + "/oidc/authorize?")
    assert q["code_challenge_method"] == "S256" and q["client_id"] == "client-abc"
    assert q["response_type"] == "code" and "openid" in q["scope"]


@pytest.mark.parametrize("change, code", [
    ({"iss": "https://evil/oidc"}, "token-issuer"),
    ({"aud": ["other"]}, "token-audience"),
    ({"exp": 10}, "token-expired"),
    ({"nonce": "other"}, "token-nonce"),
    ({"sub": ""}, "token-subject"),
])
def test_the_id_token_is_checked(tmp_path, change, code):
    s = settings(tmp_path)
    claims = dict({"iss": URL + "/oidc", "aud": ["client-abc"], "exp": time.time() + 60,
                   "nonce": "n1", "sub": "user-x"}, **change)
    with pytest.raises(rs.SSOError) as e:
        rs.check_id_token(claims, s, "n1")
    assert e.value.code == code


def test_a_refused_code_is_said(tmp_path):
    class Refuse(FakeRancher):
        def request(self, method, url, headers=None, data=None):
            return 400, {"error": "invalid_grant"}
    with pytest.raises(rs.SSOError) as e:
        rs.exchange_code(Refuse(), settings(tmp_path), "c", "v", "https://hops/cb")
    assert e.value.code == "token-refused" and "invalid_grant" in e.value.detail


# -- identité et rôle --------------------------------------------------------------

def test_the_identity_is_read_back_from_rancher(tmp_path):
    s = settings(tmp_path)
    fake = FakeRancher(groups=("keycloakoidc_group://rancher-admins",))
    ident = rs.rancher_identity(fake, s, "tok")
    assert ident == {"id": "user-kk67j", "username": "admin", "name": "Default Admin",
                     "groups": ["keycloakoidc_group://rancher-admins"],
                     "global_roles": ["admin"], "admin": True}
    assert all(c[2]["Authorization"] == "Bearer tok" for c in fake.calls)


def test_console_roles(tmp_path):
    s = settings(tmp_path, admin_groups=["keycloakoidc_group://ops"], default_role="viewer")
    assert rs.console_role(s, {"admin": True, "groups": []}) == "admin"
    assert rs.console_role(s, {"admin": False, "groups": ["keycloakoidc_group://ops"]}) == "admin"
    assert rs.console_role(s, {"admin": False, "groups": []}) == "viewer"


def test_the_access_token_is_renewed_before_it_expires(tmp_path):
    """Le jeton OIDC de Rancher vit dix minutes et ne peut pas créer de jeton
    plus long (401, vu en réel) : la session le renouvelle, et réécrit ses
    kubeconfigs pour les actions en cours."""
    s = settings(tmp_path)
    fake = FakeRancher()
    t = [time.time()]
    d = tmp_path / "ids"
    d.mkdir()
    sessions = rs.Sessions(lambda: d, now=lambda: t[0])
    first = jwt({"exp": t[0] + 600, "token": "token-oidc"})
    sess = sessions.open({"id": "u", "username": "a"}, "operator", first, "refresh-0", 43200)
    path = sessions.kubeconfig(sess, s, "c-sg2q6")
    assert sessions.renew(sess, fake, s) and sess.token == first          # encore valable
    t[0] += 500                                                           # 100 s avant la fin
    assert sessions.renew(sess, fake, s) and sess.token != first
    assert sess.refresh_token == "refresh-1" and sess.token_expires > t[0]
    token_file = json.loads(Path(path).read_text())["users"][0]["user"]["tokenFile"]
    assert Path(token_file).read_text() == sess.token
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(token_file).st_mode & 0o777) == "0o600"
    fake.revoked.append("refresh-1")                                      # Rancher refuse
    t[0] = sess.token_expires
    assert sessions.renew(sess, fake, s) is False


def test_expires_in_is_not_believed(tmp_path):
    tok = jwt({"exp": 1234.0})
    assert rs.expiry_of(tok) == 1234.0
    assert rs.expiry_of("not-a-jwt") > time.time()


# -- clusters ------------------------------------------------------------------------

def test_the_rancher_cluster_is_found_by_its_kube_system_uid(tmp_path):
    s = settings(tmp_path)
    fake = FakeRancher()
    assert rs.discover_cluster_id(fake, s, "tok", "uid-harv1") == "c-sg2q6"
    assert rs.discover_cluster_id(fake, s, "tok", "uid-harv3") is None
    assert rs.discover_cluster_id(fake, s, "tok", None) is None


def test_a_session_kubeconfig_goes_through_rancher(tmp_path):
    s = settings(tmp_path, ca_file="/etc/ssl/rancher-ca.pem")
    d = tmp_path / "ids"
    d.mkdir()
    sessions = rs.Sessions(lambda: d)
    sess = sessions.open({"id": "user-x", "username": "ana"}, "operator", "token-1:zz", "refresh-0", 3600)
    path = sessions.kubeconfig(sess, s, "c-sg2q6")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    kc = json.loads(Path(path).read_text())
    assert kc["clusters"][0]["cluster"] == {"server": URL + "/k8s/clusters/c-sg2q6",
                                            "certificate-authority": "/etc/ssl/rancher-ca.pem"}
    token_file = kc["users"][0]["user"]["tokenFile"]
    assert Path(token_file).read_text() == "token-1:zz" and "token" not in kc["users"][0]["user"]
    assert sess.login == "ana@rancher" and "token" not in json.dumps(sess.public()).lower().replace("expires", "")
    sessions.close(sess.sid)
    assert not os.path.exists(path) and not os.path.exists(token_file)
    assert sessions.get(sess.sid) is None


def test_an_expired_session_is_gone():
    t = [0.0]
    sessions = rs.Sessions(lambda: None, now=lambda: t[0])
    sess = sessions.open({"id": "u", "username": "a"}, "viewer", "t:x", "r", 10)
    assert sessions.get(sess.sid) is sess
    t[0] = 11
    assert sessions.expired() == [sess.sid]
    assert sessions.get(sess.sid) is None


# -- v1.56.0 -------------------------------------------------------------------------

def test_a_long_action_follows_the_renewed_token(tmp_path):
    """Audit D18 : Terraform recopie le kubeconfig dans son espace et un apply
    dure plus que les dix minutes d'un jeton. Le kubeconfig désigne donc un
    fichier de jeton que le renouvellement réécrit (client-go le relit) ;
    le kubeconfig lui-même ne change pas, une copie reste valable."""
    s = settings(tmp_path)
    fake = FakeRancher()
    t = [time.time()]
    d = tmp_path / "ids"
    d.mkdir()
    sessions = rs.Sessions(lambda: d, now=lambda: t[0])
    sess = sessions.open({"id": "u", "username": "a"}, "operator",
                         jwt({"exp": t[0] + 600, "token": "token-oidc"}), "refresh-0", 43200)
    path = sessions.kubeconfig(sess, s, "c-sg2q6")
    copy = tmp_path / "workspace-kubeconfig"
    copy.write_bytes(Path(path).read_bytes())            # ce que fait _stage_kubeconfig
    before = Path(path).read_bytes()
    token_file = json.loads(copy.read_text())["users"][0]["user"]["tokenFile"]
    first = Path(token_file).read_text()
    t[0] += 500
    assert sessions.renew(sess, fake, s)
    assert Path(path).read_bytes() == before              # le kubeconfig ne bouge pas
    assert Path(token_file).read_text() == sess.token != first
    # un second cluster de la même session partage le même jeton
    other = sessions.kubeconfig(sess, s, "local")
    assert json.loads(Path(other).read_text())["users"][0]["user"]["tokenFile"] == token_file


def test_a_cluster_member_is_found_by_the_nodes(tmp_path):
    """Vu en réel (compte « membre du cluster » de harv1) : kube-system est
    refusé, les nœuds sont lisibles. Sans ce chemin le membre ne voyait
    aucun cluster."""
    s = settings(tmp_path)
    fake = FakeRancher()
    fake.member = True
    assert rs.discover_cluster_id(fake, s, "tok", "uid-harv1") is None
    assert rs.discover_cluster_id(fake, s, "tok", "uid-harv1", {"node-uid-1"}) == "c-sg2q6"
    assert rs.discover_cluster_id(fake, s, "tok", None, {"node-uid-1"}) == "c-sg2q6"
    assert rs.discover_cluster_id(fake, s, "tok", "uid-harv1", {"node-uid-9"}) is None
    # kube-system lisible et d'un autre cluster : on ne compare pas les nœuds
    fake.member = False
    fake.nodes["local"] = ["node-uid-1"]
    assert rs.discover_cluster_id(fake, s, "tok", "uid-harv1", {"node-uid-1"}) == "c-sg2q6"


def test_access_to_a_known_cluster_is_asked_to_rancher(tmp_path):
    """Connaître l'id Rancher d'un cluster ne dit pas que cette personne y a
    droit : Rancher est interrogé, la réponse gardée cinq minutes, et un
    Rancher muet ne donne pas d'accès qu'il n'a jamais confirmé."""
    s = settings(tmp_path)
    fake = FakeRancher()
    t = [0.0]
    sessions = rs.Sessions(lambda: None, now=lambda: t[0])
    sess = sessions.open({"id": "u", "username": "a"}, "viewer", "t:x", "r", 3600)
    assert sessions.has_access(sess, fake, s, "c-sg2q6") is True
    fake.no_access.add("c-sg2q6")
    assert sessions.has_access(sess, fake, s, "c-sg2q6") is True        # gardé
    t[0] += 301
    assert sessions.has_access(sess, fake, s, "c-sg2q6") is False
    other = sessions.open({"id": "v", "username": "b"}, "viewer", "t:y", "r", 3600)

    class Down:
        def request(self, *a, **k):
            raise rs.SSOError("rancher-unreachable", "down")
    assert sessions.has_access(other, Down(), s, "c-sg2q6") is False
    sessions.grant(other, "c-sg2q6")
    assert sessions.has_access(other, Down(), s, "c-sg2q6") is True
