"""2.9.0 tâche 2 : client OpenBao contre un vrai serveur de banc (`--live`).

Jamais le Vault du homelab : adresse en boucle locale seulement, port 8200
refusé, `VAULT_*` et `BAO_*` vidés. Le banc fournit (variables d'environnement
du lanceur, jamais de valeur en argument) :

    HOPS_BAO_BENCH_ADDR        https://127.0.0.1:8210
    HOPS_BAO_BENCH_CA          fichier PEM de la CA du banc
    HOPS_BAO_BENCH_TOKEN_FILE  fichier 0600 d'un jeton d'administration du banc

Le test monte un KV v2 et un transit jetables, crée un AppRole, rejoue les
cas réels (enveloppe, cas, capacités, métadonnées, liste, suppression) puis
retire tout ce qu'il a créé.
"""

import os
import secrets
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import openbao_client as oc   # noqa: E402

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def bench():
    addr = os.environ.get("HOPS_BAO_BENCH_ADDR")
    ca = os.environ.get("HOPS_BAO_BENCH_CA")
    tok_file = os.environ.get("HOPS_BAO_BENCH_TOKEN_FILE")
    if not (addr and tok_file):
        pytest.skip("no OpenBao bench (HOPS_BAO_BENCH_*)")
    u = urllib.parse.urlparse(addr)
    if u.port == 8200 or u.hostname not in ("127.0.0.1", "localhost"):
        pytest.fail("refused: bench must be on the loopback, never port 8200")
    for k in list(os.environ):
        if k.startswith(("VAULT_", "BAO_")):
            os.environ.pop(k)
    admin = oc.Client(addr, ca_pem=Path(ca).read_text() if ca else None, actor="bench", action="live-290")
    admin.set_token(Path(tok_file).read_text().strip())
    tag = secrets.token_hex(4)
    mount, role, pol = f"hops-live-{tag}", f"hops-live-{tag}", f"hops-live-{tag}"
    admin._call("POST", f"sys/mounts/{mount}", {"type": "kv", "options": {"version": "2"}})
    admin._call("POST", f"sys/mounts/{mount}-v1", {"type": "kv", "options": {"version": "1"}})
    admin._call("POST", f"sys/mounts/{mount}-tr", {"type": "transit"})
    rules = (f'path "{mount}/data/consoles/{tag}/*" {{ capabilities = ["create","read","update","delete","list"] }}\n'
             f'path "{mount}/metadata/consoles/{tag}/*" {{ capabilities = ["create","read","update","patch","list","delete"] }}\n'
             f'path "{mount}/config" {{ capabilities = ["read"] }}\n'
             f'path "{mount}-tr/*" {{ capabilities = ["create","update"] }}\n'
             f'path "auth/approle/role/{role}/secret-id" {{ capabilities = ["update"] }}\n')
    admin._call("POST", f"sys/policies/acl/{pol}", {"policy": rules})
    try:
        admin._call("POST", "sys/auth/approle", {"type": "approle"})
    except oc.OpenBaoError:
        pass                                    # déjà actif
    admin._call("POST", f"auth/approle/role/{role}", {"token_policies": [pol], "token_period": "1h"})
    _, r = admin._call("GET", f"auth/approle/role/{role}/role-id")
    role_id = r["data"]["role_id"]
    yield {"admin": admin, "addr": addr, "ca": Path(ca).read_text() if ca else None, "mount": mount,
           "role": role, "role_id": role_id, "tag": tag}
    for p in (f"auth/approle/role/{role}", f"sys/policies/acl/{pol}", f"sys/mounts/{mount}",
              f"sys/mounts/{mount}-v1", f"sys/mounts/{mount}-tr"):
        try:
            admin._call("DELETE", p)
        except oc.OpenBaoError:
            pass


def _console(b):
    """Connexion AppRole par un secret_id emballé puis déballé (lookup)."""
    _, w = b["admin"]._request("POST", f"auth/approle/role/{b['role']}/secret-id", {},
                               extra={"X-Vault-Wrap-TTL": "5m"})
    wrap = w["wrap_info"]["token"]
    c = oc.Client(b["addr"], ca_pem=b["ca"], actor="alice", action="act-live")
    sid = c.unwrap(wrap, f"auth/approle/role/{b['role']}/secret-id")["secret_id"]
    with pytest.raises(oc.OpenBaoError) as e:
        c.unwrap(wrap, f"auth/approle/role/{b['role']}/secret-id")
    assert e.value.code == "openbao-unwrap-used"
    c.login_approle(b["role_id"], sid)
    return c


def test_live_round_trip(bench):
    b = bench
    c = _console(b)
    assert c.seal_status()["sealed"] is False
    assert c.kv_mount_version(b["mount"]) == 2
    base = f"consoles/{b['tag']}"
    v = c.kv_put(b["mount"], f"{base}/kubeconfig/h1", {"kubeconfig": "K1"}, cas=0,
                 custom_metadata={"hops_action": "act-live", "hops_actor": "alice", "hops_class": "kubeconfig"})
    assert v == 1 and v.metadata_error is None
    with pytest.raises(oc.OpenBaoError) as e:
        c.kv_put(b["mount"], f"{base}/kubeconfig/h1", {"kubeconfig": "K1"}, cas=0)
    assert e.value.code == "openbao-cas-conflict"
    assert c.kv_get(b["mount"], f"{base}/kubeconfig/h1")[0] == {"kubeconfig": "K1"}
    assert c.kv_metadata(b["mount"], f"{base}/kubeconfig/h1")["custom_metadata"]["hops_actor"] == "alice"
    assert c.kv_list(b["mount"], base) == ["kubeconfig/"]
    with pytest.raises(oc.OpenBaoError) as e:
        c.kv_get(b["mount"], f"consoles/other-{b['tag']}/kubeconfig/h1")
    assert e.value.code == "openbao-denied" and "deny" in e.value.detail
    c.kv_delete_all(b["mount"], f"{base}/kubeconfig/h1")
    assert c.kv_list(b["mount"], base) == []
    c.transit_create("hops-live-k", mount=f"{b['mount']}-tr")
    c.renew_self()
    c.revoke_self()


def test_live_kv1_refused(bench):
    c = oc.Client(bench["addr"], ca_pem=bench["ca"])
    c.set_token(bench["admin"]._tok.value)
    with pytest.raises(oc.OpenBaoError) as e:
        c.kv_get(f"{bench['mount']}-v1", "x")
    assert e.value.code == "openbao-not-kv2"


# ---------------------------------------------------------------------------
# 2.10.0 tâche 2 : connexion jwt d'une personne contre le vrai serveur. Le
# JWT est signé RS256 par une clé de banc (openssl, fichier temporaire), la
# méthode configurée en clés statiques avec les paramètres exacts de
# openbao_jwt ; tout est retiré à la fin (montage, politique, entité).
# ---------------------------------------------------------------------------
def _b64u(data):
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(key_file, claims, kid="key"):
    import json
    import subprocess
    head = _b64u(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    body = _b64u(json.dumps(claims).encode())
    sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key_file)], input=f"{head}.{body}".encode(),
                         capture_output=True, check=True).stdout
    return f"{head}.{body}.{_b64u(sig)}"


def test_live_jwt_person(bench, tmp_path):
    import shutil
    import subprocess
    import time
    sys.path.insert(0, str(ROOT / "bin" / "lib"))
    import openbao_jwt as oj
    if not shutil.which("openssl"):
        pytest.skip("openssl missing")
    b, admin = bench, bench["admin"]
    key = tmp_path / "bench-jwt.key"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out",
                    str(key)], check=True, capture_output=True)
    pem = subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout"], check=True, capture_output=True,
                         text=True).stdout
    cid = (b["tag"] * 2)[:12]
    mount = oj.person_mount(cid, "live")
    iss, aud = "https://rancher.bench.invalid/oidc", f"client-{b['tag']}"
    pol = oj.person_policy_name(cid)
    base = f"consoles/{b['tag']}"
    entity = None
    try:
        admin._call("POST", f"sys/auth/{mount}", {"type": "jwt"})
        admin._call("POST", f"auth/{mount}/config", oj.mount_config(iss, pubkeys=[pem]))
        admin._call("POST", f"sys/policies/acl/{pol}",
                    {"policy": f'path "{b["mount"]}/data/{base}/ssh-kvm/*" {{ capabilities = ["read"] }}\n'})
        admin._call("POST", f"auth/{mount}/role/person", oj.person_role_params(aud, "127.0.0.1/32", cid))
        admin._call("POST", f"{b['mount']}/data/{base}/ssh-kvm/k1", {"data": {"private_key": "K-live"}})
        admin._call("POST", f"{b['mount']}/data/{base}/kubeconfig/h1", {"data": {"kubeconfig": "KC"}})
        now = int(time.time())
        claims = {"iss": iss, "aud": [aud], "sub": "user-live", "exp": now + 600, "iat": now,
                  "name": "Live Person", "auth_provider": "local"}
        jwt = _sign(key, claims)
        base_client = oc.Client(b["addr"], ca_pem=b["ca"], actor="user-live@rancher", action="live-2100")
        p = base_client.for_token(None)
        info = p.login_jwt(mount, "person", jwt)
        entity = info["entity_id"]
        assert set(info["policies"]) == {"default", pol} and 0 < info["ttl"] <= 900
        me = p.lookup_self()
        assert me["display_name"] == f"{mount}-user-live" and me["entity_id"] == entity
        assert me["meta"].get("name") == "Live Person" and me["meta"].get("provider") == "local"
        assert p.kv_get(b["mount"], f"{base}/ssh-kvm/k1")[0] == {"private_key": "K-live"}
        with pytest.raises(oc.OpenBaoError) as e:
            p.kv_get(b["mount"], f"{base}/kubeconfig/h1")
        assert e.value.code == "openbao-denied"
        # même entité à la connexion suivante
        assert base_client.for_token(None).login_jwt(mount, "person", jwt)["entity_id"] == entity
        # le jeton d'accès (même audience, claim `token`) serait accepté :
        # la garantie vient de la console (essai 1)
        acc = _sign(key, dict(claims, token="token-bench:x", scope=["openid"]))
        assert base_client.for_token(None).login_jwt(mount, "person", acc)["entity_id"] == entity
        from fake_openbao import MSG_JWT_AUD, MSG_JWT_EXP, MSG_JWT_ISS, MSG_JWT_KEY, MSG_JWT_NBF
        for bad, msg in ((dict(claims, aud=["other"]), MSG_JWT_AUD), (dict(claims, iss="https://x/oidc"), MSG_JWT_ISS),
                         (dict(claims, exp=now - 200), MSG_JWT_EXP),
                         ({k: v for k, v in claims.items() if k != "iat"}, MSG_JWT_NBF)):
            with pytest.raises(oc.OpenBaoError) as e:
                base_client.for_token(None).login_jwt(mount, "person", _sign(key, bad))
            assert e.value.code == "openbao-person-login-failed" and e.value.http_status == 400, msg
            assert msg[:100] in e.value.detail          # le faux serveur rend le même message
            assert "eyJ" not in str(e.value)
        # marge d'expiration : exp - 30 s encore accepté (essai 4)
        base_client.for_token(None).revoke_self()
        late = base_client.for_token(None)
        late.login_jwt(mount, "person", _sign(key, dict(claims, exp=now - 30)))
        late.revoke_self()
        # clés statiques : le `kid` ne compte pas, seule la clé qui signe
        other = tmp_path / "other.key"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out",
                        str(other)], check=True, capture_output=True)
        with pytest.raises(oc.OpenBaoError) as e:
            base_client.for_token(None).login_jwt(mount, "person", _sign(other, claims))
        assert e.value.code == "openbao-person-login-failed"
        assert MSG_JWT_KEY[:100] in e.value.detail
        p.revoke_self()
        assert not p.logged_in
    finally:
        for path in (f"auth/{mount}/role/person", f"sys/auth/{mount}", f"sys/policies/acl/{pol}",
                     f"identity/entity/id/{entity}" if entity else None):
            if path:
                try:
                    admin._call("DELETE", path)
                except oc.OpenBaoError:
                    pass


def test_live_store_person_2100(bench, tmp_path):
    """Magasin de secrets (OpenBaoStore + PersonTokens) contre le vrai
    serveur : lecture d'une personne avec son jeton, refus sans repli par
    le service, copie partagée, révocation à la déconnexion."""
    import contextlib
    import shutil
    import subprocess
    import time
    sys.path.insert(0, str(ROOT / "bin" / "lib"))
    import openbao_jwt as oj
    import secret_store as ss
    if not shutil.which("openssl"):
        pytest.skip("openssl missing")
    b, admin = bench, bench["admin"]
    key = tmp_path / "k.key"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out",
                    str(key)], check=True, capture_output=True)
    pem = subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout"], check=True, capture_output=True,
                         text=True).stdout
    cid = (b["tag"] * 2)[:12]
    mount, pol = oj.person_mount(cid, "store"), oj.person_policy_name(cid)
    iss, aud = "https://rancher.bench.invalid/oidc", f"client-{b['tag']}"
    prefix = f"consoles/{b['tag']}"
    entities = []
    try:
        admin._call("POST", f"sys/auth/{mount}", {"type": "jwt"})
        admin._call("POST", f"auth/{mount}/config", oj.mount_config(iss, pubkeys=[pem]))
        admin._call("POST", f"sys/policies/acl/{pol}", {"policy": (
            f'path "{b["mount"]}/data/{prefix}/ssh-kvm/*" {{ capabilities = ["read"] }}\n'
            f'path "sys/capabilities-self" {{ capabilities = ["update"] }}\n')})
        admin._call("POST", f"auth/{mount}/role/person", oj.person_role_params(aud, "127.0.0.1/32", cid))
        admin._call("POST", f"{b['mount']}/data/{prefix}/ssh-kvm/k1", {"data": {"private_key": "K-store"}})
        admin._call("POST", f"{b['mount']}/data/{prefix}/kubeconfig/h1", {"data": {"kubeconfig": "KC"}})
        now = int(time.time())
        # `iat` exigé en pratique : sans lui OpenBao 2.7.2 répond « invalid not before (nbf) claim »
        jwt = _sign(key, {"iss": iss, "aud": [aud], "sub": "user-store", "exp": now + 600, "iat": now, "name": "S",
                          "auth_provider": "local"})
        svc = _console(b)
        f = lambda a=None, x=None: svc.with_headers(x, a)            # noqa: E731
        f.bare = lambda: oc.Client(b["addr"], ca_pem=b["ca"]).for_token(None)
        pt = ss.PersonTokens(id_token=lambda sid: jwt)
        work = tmp_path / "files"
        work.mkdir()
        st = ss.OpenBaoStore({"mount": b["mount"], "prefix": prefix}, f, files_dir=lambda: work,
                             person_tokens=pt)
        who = ss.Identity("person", "user-store@rancher", session_id="s1", mount=mount)
        assert st.get("openbao:ssh-kvm/k1#private_key", identity=who) == "K-store"
        entities.append(pt.entity("s1"))
        p = st.shared_file("openbao:ssh-kvm/k1#private_key", "ssh-kvm", identity=who)
        assert Path(p).read_text() == "K-store"
        with pytest.raises(ss.SecretError) as e:
            st.get("openbao:kubeconfig/h1#kubeconfig", identity=who)
        assert e.value.code == "openbao-denied"
        tok = pt.get("s1")._tok.value
        pt.drop("s1")
        probe = oc.Client(b["addr"], ca_pem=b["ca"]).for_token(tok)
        assert probe.token_valid() is False                            # revoke-self effectif
        with contextlib.suppress(Exception):
            st.purge()
    finally:
        for path in [f"auth/{mount}/role/person", f"sys/auth/{mount}", f"sys/policies/acl/{pol}"] + \
                [f"identity/entity/id/{e}" for e in entities if e]:
            try:
                admin._call("DELETE", path)
            except oc.OpenBaoError:
                pass
