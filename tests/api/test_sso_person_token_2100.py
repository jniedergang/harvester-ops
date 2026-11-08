"""2.10.0 tâche 4 : sessions Rancher et rappels du jeton OpenBao par personne.

* le jeton d'identité est gardé dans la session (mémoire), remplacé à chaque
  rafraîchissement après la même vérification (émetteur, audience, `sub`
  inchangé, sans nonce), jamais dans public(), repr, les journaux ;
* l'ancien jeton de rafraîchissement est oublié dès réception du nouveau
  (Rancher l'accepte encore : essai 1) ;
* rappels « renouvelée » (reconnexion du jeton OpenBao avec le nouveau
  jeton d'identité, révocation de l'ancien) et « fermée » (revoke-self) ;
* portée `groups` jamais demandée (décision 310, revue I4) ;
* la console n'envoie JAMAIS le jeton d'accès à OpenBao (il porte la même
  audience : OpenBao l'accepterait, essai 1) : seul le jeton d'identité.

Mutations jouées (rapport des tâches 2 à 5) : « id_token dans public() »,
« sub non revérifié au rafraîchissement », « garder l'ancien jeton de
rafraîchissement », « envoyer le jeton d'accès au lieu du jeton d'identité ».
"""

import base64
import json
import logging
import pickle
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).parent))
import app as wapp  # noqa: E402
import openbao_client as oc  # noqa: E402
import rancher_admin as radm  # noqa: E402
import rancher_sso as rs  # noqa: E402
import secret_store as ss  # noqa: E402
from fake_openbao import FakeOpenBao, fake_jwt  # noqa: E402
from test_rancher_sso import URL, FakeRancher, jwt, settings  # noqa: E402

ISS = URL + "/oidc"
AUD = "client-abc"
CID = "abc123def456"
PREFIX = f"consoles/{CID}"
KV = "harvester-ops"
JWT_MOUNT = f"jwt-hops-{CID}-p-config"


class Rancher(FakeRancher):
    """Rafraîchissement réglable : jeton d'identité présent ou non, `sub`
    changé, émetteur faux ; trace des jetons de rafraîchissement reçus."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.refresh_seen = []
        self.id_mode = "ok"

    def request(self, method, url, headers=None, data=None):
        path = url[len(URL):]
        if path == "/oidc/.well-known/openid-configuration":
            return 200, {"issuer": ISS, "scopes_supported": list(getattr(self, "scopes", ["openid"]))}
        if path == "/oidc/token" and data["grant_type"] == "refresh_token":
            self.refresh_seen.append(data["refresh_token"])
            if data["refresh_token"] in self.revoked:
                return 400, {"error": "invalid_grant"}
            n = len(self.refresh_seen)
            claims = {"iss": ISS, "aud": [AUD], "sub": self.user, "exp": time.time() + 600,
                      "iat": time.time(), "auth_provider": "local", "n": n}
            if self.id_mode == "other-sub":
                claims["sub"] = "u-intruder"
            if self.id_mode == "bad-iss":
                claims["iss"] = "https://evil.example/oidc"
            if self.id_mode == "bad-aud":
                claims["aud"] = ["other-client"]
            out = {"access_token": jwt(dict(claims, token="token-oidc", scope=["openid"])),
                   "refresh_token": f"refresh-{n}", "expires_in": 600000000000}
            if self.id_mode != "absent":
                out["id_token"] = jwt(claims)
            return 200, out
        return super().request(method, url, headers=headers, data=data)


def make(tmp_path, **kw):
    s = settings(tmp_path)
    t = [time.time()]
    d = tmp_path / "ids"
    d.mkdir(exist_ok=True)
    sessions = rs.Sessions(lambda: d, now=lambda: t[0])
    idt = jwt({"iss": ISS, "aud": [AUD], "sub": "user-kk67j", "exp": t[0] + 600, "nonce": "n0"})
    acc = jwt({"exp": t[0] + 600, "token": "token-oidc"})
    sess = sessions.open({"id": "user-kk67j", "username": "admin"}, "admin", acc, "refresh-0", 43200,
                         id_token=idt)
    return s, t, sessions, sess, Rancher(**kw)


# ---------------------------------------------------------------------------
# Session : jeton d'identité gardé, jamais montré
# ---------------------------------------------------------------------------
def test_id_token_kept_never_shown(tmp_path):
    _s, _t, _sessions, sess, _f = make(tmp_path)
    assert sess.id_token
    pub = json.dumps(sess.public())
    for secret in (sess.id_token, sess.token, sess.refresh_token):
        assert secret not in pub and secret not in repr(sess) and secret not in str(sess)
    with pytest.raises(TypeError):
        pickle.dumps(sess)


def test_renew_keeps_new_id_token_and_forgets_old_refresh(tmp_path):
    s, t, sessions, sess, fake = make(tmp_path)
    old_id, old_refresh = sess.id_token, sess.refresh_token
    renewed = []
    sessions.on_renewed.append(lambda x: renewed.append(x.id_token))
    t[0] += 500
    assert sessions.renew(sess, fake, s)
    assert sess.id_token and sess.id_token != old_id and renewed == [sess.id_token]
    assert sess.refresh_token == "refresh-1"
    assert old_refresh not in json.dumps({k: v for k, v in vars(sess).items() if isinstance(v, str)})
    t[0] = sess.token_expires - 100
    assert sessions.renew(sess, fake, s)
    assert fake.refresh_seen == ["refresh-0", "refresh-1"]             # jamais l'ancien rejoué


def test_renew_without_id_token(tmp_path):
    """Rancher rend un jeton d'identité à chaque rafraîchissement (essai 1) ;
    s'il manquait, la session reste mais n'a plus de jeton d'identité (une
    lecture personnelle dira « reconnectez-vous »), et le rappel le voit."""
    s, t, sessions, sess, fake = make(tmp_path)
    fake.id_mode = "absent"
    seen = []
    sessions.on_renewed.append(lambda x: seen.append(x.id_token))
    t[0] += 500
    assert sessions.renew(sess, fake, s)
    assert sess.id_token is None and seen == [None]


@pytest.mark.parametrize("mode", ["other-sub", "bad-iss", "bad-aud"])
def test_renew_with_bad_id_token_closes(tmp_path, mode):
    s, t, sessions, sess, fake = make(tmp_path)
    fake.id_mode = mode
    renewed = []
    sessions.on_renewed.append(renewed.append)
    old_token = sess.token
    t[0] += 500
    assert sessions.renew(sess, fake, s) is False
    assert sess.token == old_token and not renewed                    # rien de repris


def test_check_id_token_refresh_needs_same_subject(tmp_path):
    s = settings(tmp_path)
    c = {"iss": ISS, "aud": [AUD], "sub": "user-kk67j", "exp": time.time() + 600}
    assert rs.check_id_token(c, s, None, subject="user-kk67j") == c
    with pytest.raises(rs.SSOError) as e:
        rs.check_id_token(c, s, None, subject="u-other")
    assert e.value.code == "identity-mismatch"
    with pytest.raises(rs.SSOError) as e:
        rs.check_id_token(c, s, None)                                  # connexion : nonce exigé
    assert e.value.code == "token-nonce"


def test_close_hook_and_tokens_forgotten(tmp_path):
    _s, _t, sessions, sess, _f = make(tmp_path)
    closed = []
    sessions.on_closed.append(lambda x: closed.append(x.sid))
    sessions.on_closed.append(lambda x: 1 / 0)                       # un rappel en échec ne casse rien
    assert sessions.close(sess.sid) is sess
    assert closed == [sess.sid] and sess.id_token is None and sess.refresh_token is None


# ---------------------------------------------------------------------------
# Portée `groups` : jamais demandée en 2.10.0 (décision 310, revue I4)
# ---------------------------------------------------------------------------
def test_groups_scope_never_requested(tmp_path):
    s = settings(tmp_path)
    u = rs.authorize_url(s, "https://hops/cb", "st", "no", "ch")
    assert dict(parse_qsl(urlparse(u).query))["scope"] == "openid profile offline_access"
    assert not hasattr(rs, "scopes_supported") and not hasattr(rs, "request_scopes")


def test_register_keeps_default_scopes():
    """Le client enregistré par la console garde les portées par défaut de
    Rancher (aucun `spec.scopes`)."""
    a = Admin()
    radm.register_oidc(a, "n", "https://hops/cb", sleep=lambda s: None)
    assert "scopes" not in a.sent[0][1]["spec"]


class Admin:
    def __init__(self, existing=None):
        self.sent = []
        self.existing = existing

    def send(self, method, path, body):
        self.sent.append((method, body))
        if method == "POST" and self.existing is not None:
            return 409, {}
        return 201, body

    def get(self, path):
        if path.endswith("secrets/client-1"):
            return 200, {"data": {"client-secret-1": base64.b64encode(b"sec").decode()}}
        cur = self.existing or {}
        return 200, dict(cur, status={"clientID": "client-1"})


# ---------------------------------------------------------------------------
# Console : callback, rappels, déconnexion, jeton présenté à OpenBao
# ---------------------------------------------------------------------------
@pytest.fixture
def console(tmp_path, monkeypatch):
    """Application réelle, OpenBao simulé avec la méthode jwt du Rancher de
    config.yaml, jeton par personne allumé, registre de jetons isolé."""
    bao = FakeOpenBao().start()
    bao.creds = bao.add_role()
    bao.add_jwt_mount(JWT_MOUNT, issuer=ISS, audience=AUD,
                      allow=[(f"{KV}/data/{PREFIX}/ssh-cluster/", {"read"}),
                             (f"{KV}/data/{PREFIX}/ssh-kvm/", {"read"})])
    bao.put_raw(KV, f"{PREFIX}/ssh-cluster/h1", {"private_key": "KEY-h1"})
    state, run = tmp_path / "state", tmp_path / "run"
    state.mkdir()
    run.mkdir()
    (state / ss.SETTINGS_FILE).write_text(json.dumps({
        "mode": "a", "addr": bao.addr, "console_id": CID, "secret_files_dir": str(run / "hops-secrets"),
        "person_token": {"enabled": True, "servers": {"config": {"mount": JWT_MOUNT}}}}))
    base = oc.Client(bao.addr, timeout=2, retry_delays=(0.01,))
    base.login_approle(*bao.creds)
    pt = ss.PersonTokens(guard=wapp.has_request_context, id_token=wapp._sso_person_id_token)
    monkeypatch.setattr(ss, "PERSON_TOKENS", pt)
    mi = f"22 1 8:1 / / rw - ext4 /dev/sda1 rw\n30 22 0:40 / {run} rw - tmpfs tmpfs rw\n"
    st = ss.MultiStore(state, client_factory=lambda actor=None, action=None: base.with_headers(action, actor),
                       mountinfo=mi)
    monkeypatch.setattr(ss, "_STORE", st)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    ids = tmp_path / "ids"
    ids.mkdir()
    monkeypatch.setattr(wapp, "_SSO_SESSIONS", rs.Sessions(lambda: ids))
    yield {"bao": bao, "pt": pt, "store": st, "base": base}
    st.purge()
    bao.stop()


def open_session(**kw):
    now = int(time.time())
    id_token = fake_jwt({"iss": ISS, "aud": [AUD], "sub": "user-kk67j", "exp": now + 600, "iat": now, "nonce": "n0",
                         "name": "Default Admin", "auth_provider": "local"})
    # même audience, porte un jeton Rancher : OpenBao l'accepterait (essai 1)
    access = fake_jwt({"iss": ISS, "aud": [AUD], "sub": "user-kk67j", "exp": now + 600, "iat": now,
                       "auth_provider": "local", "scope": ["openid"], "token": "token-abcde:rancher"})
    store = wapp._sso_store()
    sess = store.open({"id": "user-kk67j", "username": "admin", "name": "Default Admin"}, "admin",
                      access, "refresh-0", 3600, id_token=id_token, **kw)
    return sess


def read_key_as(sess):
    with wapp.app.test_request_context("/"):
        wapp.g.sso_session = sess
        return wapp._cluster_ssh_key_path({"key": "openbao:ssh-cluster/h1#private_key"})


def test_only_the_id_token_is_ever_sent_to_openbao(console):
    """Le jeton d'accès porte un jeton Rancher et la même audience : la
    console ne l'envoie jamais, seul le jeton d'identité part."""
    bao = console["bao"]
    sess = open_session()
    p = read_key_as(sess)
    assert Path(p).read_text() == "KEY-h1"
    assert [j for _m, j in bao.jwt_logins] == [sess.id_token]
    # rafraîchissement : nouveau jeton d'identité, reconnexion avec lui
    sess.id_token = fake_jwt({"iss": ISS, "aud": [AUD], "sub": "user-kk67j", "exp": int(time.time()) + 600,
                              "iat": int(time.time())})
    wapp._sso_person_renewed(sess)
    console["pt"].join()                         # reconnexion dans son fil (revue I5)
    read_key_as(sess)
    assert [j for _m, j in bao.jwt_logins] == [bao.jwt_logins[0][1], sess.id_token]
    for r in bao.log:
        blob = r["raw"].decode("latin-1") + json.dumps(r["headers"])
        assert sess.token not in blob and sess.token.split(".")[1] not in blob


def test_person_read_uses_person_token_and_header(console):
    bao = console["bao"]
    sess = open_session()
    read_key_as(sess)
    r = [x for x in bao.log if x["path"] == f"{KV}/data/{PREFIX}/ssh-cluster/h1"][-1]
    assert r["headers"]["x-vault-token"] == console["pt"].get(sess.sid)._tok.value
    assert r["headers"]["x-hops-actor"] == "admin@rancher"


def test_session_opened_before_activation_has_no_token(console):
    """Session sans jeton d'identité (ouverte avant la 2.10.0) : refus
    `openbao-person-no-token`, aucun repli par le service."""
    bao = console["bao"]
    sess = open_session()
    sess.id_token = None
    with pytest.raises(ss.SecretError) as e:
        read_key_as(sess)
    assert e.value.code == "openbao-person-no-token" and "Rancher" in str(e.value)
    assert not [x for x in bao.log if x["path"].startswith(f"{KV}/data/")]


def test_direct_login_and_local_account_read_by_service(console):
    bao = console["bao"]
    sess = open_session(kind="direct")
    p = read_key_as(sess)
    assert Path(p).read_text() == "KEY-h1" and not bao.jwt_logins
    with wapp.app.test_request_context("/"):
        wapp.g.sso_session = None
        assert wapp._secret_identity("ssh-cluster").kind == "service"


def test_close_revokes_person_token(console):
    bao = console["bao"]
    sess = open_session()
    read_key_as(sess)
    tok = console["pt"].get(sess.sid)._tok.value
    wapp._sso_store().close(sess.sid)
    assert tok not in bao.tokens and console["pt"].get(sess.sid) is None
    assert bao.requests("auth/token/revoke-self")[-1]["headers"]["x-vault-token"] == tok


def test_renewed_without_id_token_drops(console):
    bao = console["bao"]
    sess = open_session()
    read_key_as(sess)
    tok = console["pt"].get(sess.sid)._tok.value
    sess.id_token = None
    wapp._sso_person_renewed(sess)
    assert tok not in bao.tokens and console["pt"].get(sess.sid) is None


def test_renewer_thread_path_calls_hooks(console, tmp_path):
    """Le fil `rancher-token-renewer` passe par Sessions.renew / close : les
    rappels suivent (reconnexion, puis révocation au refus)."""
    bao = console["bao"]
    s = settings(tmp_path)
    fake = Rancher()
    sess = open_session()
    read_key_as(sess)
    first = console["pt"].get(sess.sid)._tok.value
    sess.token_expires = time.time() + 10
    # le faux Rancher signe en base64 sans clé : la méthode jwt du faux
    # OpenBao refuserait sa signature ; on lui substitue un jeton accepté
    orig = fake.request

    def signed(method, url, headers=None, data=None):
        st, out = orig(method, url, headers=headers, data=data)
        if "id_token" in out:
            out["id_token"] = fake_jwt(dict(rs.claims_of(out["id_token"])))
        return st, out
    fake.request = signed
    store = wapp._sso_store()
    assert store.renew(sess, fake, s, margin=180)
    console["pt"].join()                         # reconnexion dans son fil (revue I5)
    assert len(bao.jwt_logins) == 2 and bao.jwt_logins[-1][1] == sess.id_token
    assert first not in bao.tokens
    fake.revoked.append(sess.refresh_token)
    sess.token_expires = time.time() + 10
    assert store.renew(sess, fake, s, margin=180) is False
    store.close(sess.sid)
    assert console["pt"].get(sess.sid) is None


def test_callback_keeps_id_token_out_of_whoami_and_logs(tmp_path, monkeypatch, caplog):
    """`sso_callback` garde le jeton d'identité vérifié dans la session ;
    il n'apparaît ni dans /api/whoami ni dans les journaux. Même banc que
    les routes de la 1.79.0 (Rancher simulé)."""
    import test_rancher_routes_179 as r179
    state = tmp_path / "state"
    cfg = {"clusters": [{"name": "harv1", "kubeconfig": str(tmp_path / "kc")}]}
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("")
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: (u, p) in {("boss", "pw")})
    monkeypatch.setattr(wapp, "_htpasswd_users", lambda: ["boss"])
    ids = tmp_path / "ids"
    ids.mkdir()
    monkeypatch.setattr(wapp, "_SSO_SESSIONS", rs.Sessions(lambda: ids))
    monkeypatch.setattr(wapp, "_SSO_PENDING", rs.PendingLogins())
    monkeypatch.setattr(wapp, "_SSO_CLUSTER_IDS", {})
    fake = r179.Rancher()
    monkeypatch.setattr(wapp, "_sso_http", lambda s: fake)
    monkeypatch.setattr(wapp, "_rancher_http", lambda s, timeout=15: fake)
    caplog.set_level(logging.DEBUG)
    with wapp.app.test_client() as c:
        r179.add(c)
        wapp._RANCHER_STORE.set_sso(cfg, "lab", "client-abc", "s3cret",
                                    "http://localhost/auth/rancher/callback", "harvester-ops-localhost")
        r = c.get("/auth/rancher/lab/login")
        q = dict(parse_qsl(urlparse(r.headers["Location"]).query))
        assert q["scope"] == "openid profile offline_access"     # jamais `groups` (décision 310)
        assert not [c for c in getattr(fake, "calls", []) if "openid-configuration" in str(c)]
        fake.nonce = q["nonce"]
        r = c.get(f"/auth/rancher/callback?code=c1&state={q['state']}")
        assert r.status_code == 302
        sess = wapp._sso_store().all()[-1]
        assert sess.id_token and rs.claims_of(sess.id_token)["sub"] == fake.user
        assert not rs.claims_of(sess.id_token).get("token")             # jamais le jeton d'accès
        who = json.dumps(c.get("/api/whoami").get_json())
        assert sess.id_token not in who and sess.id_token.split(".")[1] not in who
        assert wapp._sso_person_renewed in wapp._sso_store().on_renewed
        assert wapp._sso_person_closed in wapp._sso_store().on_closed
    assert sess.id_token not in caplog.text and sess.id_token.split(".")[1] not in caplog.text
