"""2.10.0 tâche 3 : identité des lectures du magasin de secrets.

Faux serveur OpenBao (méthode `jwt` de la tâche 2) : une personne connectée
par Rancher lit les classes personnelles avec SON jeton OpenBao, qui juge et
journalise ; le service lit le reste ; aucun repli silencieux ; cache des
personnes indexé par (entité, référence), servi à la même personne pendant
une panne seulement.

Mutations jouées (rapport des tâches 2 à 5) : « cache servi avant le
jugement d'OpenBao », « repli AppRole après un refus », « entrée de cache
partagée entre entités » (cache indexé par la référence seule).
"""

import os
import pickle
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import openbao_client as oc      # noqa: E402
import secret_store as ss        # noqa: E402
from fake_openbao import FakeOpenBao, fake_jwt  # noqa: E402

CID = "abc123def456"
PREFIX = f"consoles/{CID}"
KV = "harvester-ops"
JWT_MOUNT = f"jwt-hops-{CID}-rmgr"
ISS = "https://rancher.example/oidc"
AUD = "client-abc"
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nk1-person-2100\n-----END OPENSSH PRIVATE KEY-----\n"
REF = "openbao:ssh-kvm/k1#private_key"
REF_KC = "openbao:kubeconfig/h1#kubeconfig"


def jwt_for(sub, **kw):
    c = {"iss": ISS, "aud": [AUD], "sub": sub, "exp": int(time.time()) + 600, "iat": int(time.time()),
         "name": sub, "auth_provider": "local"}
    c.update(kw)
    return fake_jwt(c)


@pytest.fixture
def srv():
    s = FakeOpenBao().start()
    s.creds = s.add_role()
    s.add_jwt_mount(JWT_MOUNT, issuer=ISS, audience=AUD,
                    allow=[(f"{KV}/data/{PREFIX}/ssh-kvm/", {"read"}),
                           (f"{KV}/data/{PREFIX}/ssh-cluster/", {"read", "create", "update"}),
                           (f"{KV}/data/{PREFIX}/tf-kvm/", {"read", "create", "update"}),
                           (f"{KV}/metadata/{PREFIX}/tf-kvm/", {"read"}),
                           ("sys/capabilities-self", {"update"})])
    s.put_raw(KV, f"{PREFIX}/ssh-kvm/k1", {"private_key": KEY})
    s.put_raw(KV, f"{PREFIX}/kubeconfig/h1", {"kubeconfig": "kc-2100"})
    yield s
    s.stop()


class World:
    """Un support OpenBao avec son registre de jetons de personne."""

    def __init__(self, srv, tmp_path, clock=None):
        self.srv = srv
        base = oc.Client(srv.addr, timeout=2, retry_delays=(0.01,))
        base.login_approle(*srv.creds)
        self.base = base
        self.factory_calls = []

        def factory(actor=None, action=None):
            self.factory_calls.append((actor, action))
            return base.with_headers(action, actor)
        factory.bare = lambda: oc.Client(srv.addr, timeout=2, retry_delays=(0.01,)).for_token(None)
        self.tokens = {}              # session -> jeton d'identité
        self.in_request = [True]
        self.pt = ss.PersonTokens(guard=lambda: self.in_request[0], id_token=lambda sid: self.tokens.get(sid))
        work = tmp_path / "files"
        work.mkdir()
        kw = {"clock": clock} if clock else {}
        self.store = ss.OpenBaoStore({"mount": KV, "prefix": PREFIX, "cache_ttl_min": 15}, factory,
                                     files_dir=lambda: work, person_tokens=self.pt, **kw)

    def person(self, sid, sub):
        self.tokens[sid] = jwt_for(sub)
        return ss.Identity("person", f"{sub}@rancher", session_id=sid, mount=JWT_MOUNT)

    def approle_reads(self):
        """Requêtes KV parties avec le jeton du service."""
        svc = self.base._tok.value
        return [r for r in self.srv.log if r["path"].startswith(f"{KV}/data/")
                and r["headers"].get("x-vault-token") == svc]


@pytest.fixture
def w(srv, tmp_path):
    return World(srv, tmp_path)


def kv_reads(srv, path):
    return [r for r in srv.log if r["path"] == f"{KV}/data/{PREFIX}/{path}" and r["method"] == "GET"]


# ---------------------------------------------------------------------------
# Qui présente quel jeton
# ---------------------------------------------------------------------------
def test_person_class_read_with_person_token(w, srv):
    alice = w.person("s-a", "user-alice")
    assert w.store.get(REF, identity=alice) == KEY
    r = kv_reads(srv, "ssh-kvm/k1")[-1]
    tok = w.pt.get("s-a")._tok.value
    assert r["headers"]["x-vault-token"] == tok != w.base._tok.value
    assert r["headers"]["x-hops-actor"] == "user-alice@rancher"
    assert srv.jwt_logins == [(JWT_MOUNT, w.tokens["s-a"])]
    assert not w.approle_reads()


def test_service_class_same_session_reads_with_approle(w, srv):
    # identity_for rend « service » pour kubeconfig : l'en-tête garde la personne
    assert w.store.get(REF_KC, identity=ss.Identity("service", "user-alice@rancher")) == "kc-2100"
    r = kv_reads(srv, "kubeconfig/h1")[-1]
    assert r["headers"]["x-vault-token"] == w.base._tok.value
    assert r["headers"]["x-hops-actor"] == "user-alice@rancher"
    assert not srv.jwt_logins


@pytest.mark.parametrize("actor", [None, "admin", ss.SERVICE, ss.Identity("service", "bob")])
def test_service_identity_and_2_9_actor_string(w, srv, actor):
    """Compte local, connexion directe, réglage éteint : AppRole ; une
    chaîne (2.9.0) vaut l'identité de service."""
    assert w.store.get(REF, actor) == KEY
    assert kv_reads(srv, "ssh-kvm/k1")[-1]["headers"]["x-vault-token"] == w.base._tok.value
    assert not srv.jwt_logins


def test_person_denied_no_approle_after(w, srv):
    """Personne refusée : `openbao-denied`, AUCUNE requête AppRole ensuite,
    même avec la valeur au cache commun (pas de jugement par le cache)."""
    srv.put_raw(KV, f"{PREFIX}/ssh-kvm/k2", {"private_key": "k2"})
    ref2 = "openbao:ssh-kvm/k2#private_key"
    assert w.store.get(ref2) == "k2"                                 # cache commun chaud (service)
    srv.jwt[JWT_MOUNT]["allow"] = [(f"{KV}/data/{PREFIX}/ssh-cluster/", {"read"})]
    alice = w.person("s-a", "user-alice")
    n = len(w.approle_reads())
    with pytest.raises(ss.SecretError) as e:
        w.store.get(ref2, identity=alice)
    assert e.value.code == "openbao-denied" and e.value.cls == "ssh-kvm" and e.value.name == "k2"
    assert len(w.approle_reads()) == n
    with pytest.raises(ss.SecretError):
        w.store.shared_file(ref2, "ssh-kvm", identity=alice)
    with pytest.raises(ss.SecretError):
        with w.store.file(ref2, "ssh-kvm", identity=alice):
            pass
    assert len(w.approle_reads()) == n


def test_person_read_never_served_from_cache_before_judgement(w, srv):
    alice = w.person("s-a", "user-alice")
    w.store.get(REF, identity=alice)
    w.store.get(REF, identity=alice)
    assert len(kv_reads(srv, "ssh-kvm/k1")) == 2                    # une requête à chaque lecture


def test_outage_serves_same_person_only(w, srv):
    """A a lu, B lit pendant la panne : B refusée, A servie depuis le cache."""
    alice, bob = w.person("s-a", "user-alice"), w.person("s-b", "user-bob")
    assert w.store.get(REF, identity=alice) == KEY
    bob_client = None
    w.store.get("openbao:kubeconfig/h1#kubeconfig")            # rien à voir : cache commun du service
    # B se connecte avant la panne (entité connue) mais n'a jamais lu REF
    srv.put_raw(KV, f"{PREFIX}/ssh-kvm/k9", {"private_key": "k9"})
    assert w.store.get("openbao:ssh-kvm/k9#private_key", identity=bob) == "k9"
    bob_client = w.pt.get("s-b")
    srv.sealed = True
    assert w.store.get(REF, identity=alice) == KEY
    with pytest.raises(ss.SecretError) as e:
        w.store.get(REF, identity=bob)
    assert e.value.code == "openbao-sealed"
    assert bob_client is not None
    # une personne jamais connectée n'est pas servie non plus
    with pytest.raises(ss.SecretError) as e:
        w.store.get(REF, identity=w.person("s-c", "user-carol"))
    assert e.value.code in ss.DOWN_CODES
    # le service, lui, garde le cache commun de la 2.9.0
    assert w.store.get(REF) == KEY


def test_outage_cache_expires(srv, tmp_path):
    now = [1000.0]
    w = World(srv, tmp_path, clock=lambda: now[0])
    alice = w.person("s-a", "user-alice")
    w.store.get(REF, identity=alice)
    srv.fail_next.extend([(502, {"errors": ["bad gateway"]})] * 4)
    now[0] += 15 * 60 + 1
    with pytest.raises(ss.SecretError) as e:
        w.store.get(REF, identity=alice)
    assert e.value.code == "openbao-unreachable"


def test_two_people_two_requests_one_shared_copy(w, srv):
    alice, bob = w.person("s-a", "user-alice"), w.person("s-b", "user-bob")
    pa = w.store.shared_file(REF, "ssh-kvm", identity=alice)
    pb = w.store.shared_file(REF, "ssh-kvm", identity=bob)
    assert pa == pb and Path(pa).read_text() == KEY
    reads = kv_reads(srv, "ssh-kvm/k1")
    assert len(reads) == 2
    assert {r["headers"]["x-vault-token"] for r in reads} == {w.pt.get("s-a")._tok.value,
                                                              w.pt.get("s-b")._tok.value}


def test_drop_revokes_and_purges_entity(w, srv):
    alice, bob = w.person("s-a", "user-alice"), w.person("s-b", "user-bob")
    w.store.get(REF, identity=alice)
    w.store.get(REF, identity=bob)
    ent_a, ent_b = w.pt.entity("s-a"), w.pt.entity("s-b")
    tok = w.pt.get("s-a")._tok.value
    assert w.pt.drop("s-a") is True
    rev = srv.requests("auth/token/revoke-self")
    assert rev and rev[-1]["headers"]["x-vault-token"] == tok and tok not in srv.tokens
    assert not any(k[0] == ent_a for k in w.store._pcache)
    assert any(k[0] == ent_b for k in w.store._pcache)
    assert w.pt.get("s-a") is None and w.pt.drop("s-a") is False


def test_person_without_identity_token(w, srv):
    ident = ss.Identity("person", "x@rancher", session_id="s-old", mount=JWT_MOUNT)   # pas de jeton
    with pytest.raises(ss.SecretError) as e:
        w.store.get(REF, identity=ident)
    assert e.value.code == "openbao-person-no-token"
    assert not srv.jwt_logins and not w.approle_reads()


def test_person_login_refused(w, srv):
    alice = w.person("s-a", "user-alice")
    w.tokens["s-a"] = jwt_for("user-alice", aud=["other"])
    with pytest.raises(ss.SecretError) as e:
        w.store.get(REF, identity=alice)
    assert e.value.code == "openbao-person-login-failed"
    assert w.tokens["s-a"] not in str(e.value) and not w.approle_reads()


def test_person_outside_request_refused(w, srv):
    """Fil d'une action : aucun client de personne créé (tâche 5)."""
    alice = w.person("s-a", "user-alice")
    w.in_request[0] = False
    with pytest.raises(ss.SecretError) as e:
        w.store.get(REF, identity=alice)
    assert e.value.code == "openbao-person-no-token"
    assert not srv.jwt_logins and not w.approle_reads()


def test_person_token_expired_relogs_with_session_token(srv, tmp_path):
    now = [1000.0]
    w = World(srv, tmp_path)
    alice = w.person("s-a", "user-alice")
    w.store.get(REF, identity=alice)
    w.pt.get("s-a")._clock = lambda: now[0] + 10_000                # jeton échu côté client
    w.pt._d["s-a"]["client"]._tok.obtained = 0
    w.store.get(REF, identity=alice)
    assert len(srv.jwt_logins) == 2 and not w.approle_reads()


def test_person_token_revoked_server_side_relogs_once(w, srv):
    alice = w.person("s-a", "user-alice")
    w.store.get(REF, identity=alice)
    srv.tokens.pop(w.pt.get("s-a")._tok.value)                       # restauration, révocation ailleurs
    assert w.store.get(REF, identity=alice) == KEY
    assert len(srv.jwt_logins) == 2 and not w.approle_reads()


def test_renew_relogs_and_revokes_old(w, srv):
    alice = w.person("s-a", "user-alice")
    w.store.get(REF, identity=alice)
    old = w.pt.get("s-a")._tok.value
    new_jwt = jwt_for("user-alice", iat=int(time.time()) + 1)
    assert w.pt.renew("s-a", new_jwt) is True
    assert srv.jwt_logins[-1] == (JWT_MOUNT, new_jwt)
    assert old not in srv.tokens and w.pt.get("s-a")._tok.value != old
    assert w.pt.renew("s-unknown", new_jwt) is False


def test_renew_refused_drops(w, srv):
    alice = w.person("s-a", "user-alice")
    w.store.get(REF, identity=alice)
    old = w.pt.get("s-a")._tok.value
    assert w.pt.renew("s-a", jwt_for("user-alice", iss="https://evil/oidc")) is False
    assert w.pt.get("s-a") is None and old not in srv.tokens


@pytest.mark.parametrize("cls,path,field", [
    ("tf-kvm", "k1/d1/rke2_token", "value"),
    ("ssh-kvm", "k7", "private_key"),            # décision 309 : déclaration d'un hôte KVM
    ("ssh-cluster", "h7", "private_key"),
])
def test_person_write_metadata(w, srv, cls, path, field):
    """Écriture d'une personne avec le texte EXACT de la politique commune
    (openbao_policy.person_rules) : valeur et métadonnées d'attribution
    (`hops_actor`, `hops_identity`) écrites avec son jeton (revue M7, I3)."""
    import openbao_policy as pol
    srv.jwt[JWT_MOUNT]["allow"] = [(p.rstrip("*"), set(c)) for p, c in pol.person_rules(KV, PREFIX, CID)]
    alice = w.person("s-a", "user-alice")
    ref = w.store.put(cls, path, field, "val-2100", identity=alice, action="act-9")
    assert ref == f"openbao:{cls}/{path}#{field}"
    tok = w.pt.get("s-a")._tok.value
    for kind in ("data", "metadata"):
        puts = [r for r in srv.log if r["path"] == f"{KV}/{kind}/{PREFIX}/{cls}/{path}"
                and r["method"] in ("POST", "PUT")]
        assert puts and puts[-1]["headers"]["x-vault-token"] == tok, kind
    assert w.store.last_metadata_error is None
    custom = srv.kv[(KV, f"{PREFIX}/{cls}/{path}")]["custom"]
    assert custom["hops_identity"] == "person" and custom["hops_actor"] == "user-alice@rancher"
    assert not w.approle_reads()


def test_identity_mark_in_metadata(w, srv):
    cm = ss.version_metadata({}, 1, "tf-kvm", "alice", None, identity="person")
    assert cm["hops_identity"] == "person"
    assert "hops_identity" not in ss.version_metadata(cm, 2, "tf-kvm", "alice", None)
    assert w.store._identity_mark(ss.SERVICE) is None                      # 2.9.x : rien
    w.store.settings = dict(w.store.settings, person_token={"enabled": True})
    assert w.store._identity_mark(ss.SERVICE) == "service"


def test_late_read_hook(w, srv):
    seen = []
    with ss.action_reads(lambda cls, path: seen.append((cls, path))):
        w.store.get(REF)                       # service, classe personnelle, vraie requête
        w.store.get(REF)                       # cache : pas une lecture
        w.store.get(REF_KC)                    # classe de service : pas marquée
    w.store.get("openbao:ssh-kvm/k1#private_key")
    assert seen == [("ssh-kvm", "k1")]


# ---------------------------------------------------------------------------
# Rien de secret nulle part
# ---------------------------------------------------------------------------
def test_person_token_absent_from_repr_pickle_exceptions(w, srv):
    alice = w.person("s-a", "user-alice")
    path = w.store.shared_file(REF, "ssh-kvm", identity=alice)
    tok = w.pt.get("s-a")._tok.value
    jwt = w.tokens["s-a"]
    texts = [repr(w.pt), repr(w.store), repr(alice), str(alice), pickle.dumps(alice).decode("latin-1"),
             Path(path).read_text()]
    srv.jwt[JWT_MOUNT]["allow"] = []
    srv.tokens.clear()
    w.tokens["s-a"] = jwt_for("user-alice", aud=["x"])
    try:
        w.store.get(REF, identity=alice)
    except ss.SecretError as e:
        texts += [str(e), repr(e), pickle.dumps(e).decode("latin-1")]
    for t in texts:
        assert tok not in t and jwt not in t
    with pytest.raises(TypeError):
        pickle.dumps(w.pt)
    for f in Path(path).parent.iterdir():
        assert tok not in f.read_text() and jwt not in f.read_text()


@pytest.mark.parametrize("kw", [{"session_id": None, "mount": JWT_MOUNT}, {"session_id": "s", "mount": None}])
def test_person_identity_needs_session_and_mount(kw):
    with pytest.raises(ValueError):
        ss.Identity("person", "a", **kw)
    with pytest.raises(ValueError):
        ss.Identity("root")


def test_as_identity():
    assert ss.as_identity(None) == ss.SERVICE
    assert ss.as_identity("bob") == ss.Identity("service", "bob")
    i = ss.Identity("person", "a", session_id="s", mount="m")
    assert ss.as_identity(i) is i


def test_multistore_passes_identity(srv, tmp_path, monkeypatch):
    """Le magasin du processus porte l'identité jusqu'au support OpenBao
    (path_of, resolve_value, file), et le registre global par défaut."""
    import json
    st_dir = tmp_path / "state"
    st_dir.mkdir()
    work = tmp_path / "run"
    work.mkdir()
    (st_dir / ss.SETTINGS_FILE).write_text(json.dumps({"mode": "a", "addr": srv.addr, "console_id": CID,
                                                         "secret_files_dir": str(work / "s")}))
    base = oc.Client(srv.addr, timeout=2, retry_delays=(0.01,))
    base.login_approle(*srv.creds)
    f = lambda a=None, x=None: base.with_headers(x, a)          # noqa: E731
    f.bare = lambda: oc.Client(srv.addr, timeout=2).for_token(None)
    mi = "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n30 22 0:40 / %s rw - tmpfs tmpfs rw\n" % work
    pt = ss.PersonTokens(id_token=lambda sid: jwt_for("user-alice"))
    monkeypatch.setattr(ss, "PERSON_TOKENS", pt)
    st = ss.MultiStore(st_dir, client_factory=f, mountinfo=mi)
    alice = ss.Identity("person", "alice", session_id="s-a", mount=JWT_MOUNT)
    p = ss.path_of(REF, "ssh-kvm", st=st, identity=alice)
    assert Path(p).read_text() == KEY
    assert ss.resolve_value(REF, st=st, identity=alice) == KEY
    with st.file(REF, "ssh-kvm", identity=alice) as fp:
        assert Path(fp).read_text() == KEY
    reads = kv_reads(srv, "ssh-kvm/k1")
    assert len(reads) == 3 and all(r["headers"]["x-vault-token"] == pt.get("s-a")._tok.value for r in reads)
    st.purge()
