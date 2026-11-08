"""2.9.0 tâche 3 : magasin à supports multiples (bin/lib/secret_store.py).

* références `openbao:` refusées tant qu'OpenBao n'est pas actif ;
* support fichier : chaque classe lue à son emplacement d'aujourd'hui, même
  valeur que l'ancien lecteur ; une valeur locale remplacée par sa
  référence est suivie ;
* support OpenBao (faux serveur HTTP) : cache en mémoire, état dégradé,
  aucune copie locale de repli, métadonnées, suppression d'un arbre ;
* aucune valeur dans repr, pickle ni les exceptions, pour les deux supports.

Mutations jouées (rapport des tâches 2 à 4) : « cache écrit sur disque »,
« copie tmpfs non effacée en cas d'exception », « repli sur fichier local
quand OpenBao est coupé ».
"""

import json
import os
import pickle
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import openbao_client as oc      # noqa: E402
import secret_classes as sc      # noqa: E402
import secret_store as ss        # noqa: E402
from fake_openbao import FakeOpenBao  # noqa: E402

VALUE = "S3CRET-value-290"
CID = "abc123def456"
PREFIX = f"consoles/{CID}"


def tmpfs_mountinfo(*paths):
    """mountinfo où chaque chemin donné est un tmpfs, le reste en ext4."""
    lines = ["22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw"]
    for i, p in enumerate(paths):
        lines.append(f"{30 + i} 22 0:{40 + i} / {p} rw,nosuid shared:{9 + i} - tmpfs tmpfs rw")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Fixtures : un état de console avec un secret de chaque classe
# ---------------------------------------------------------------------------
def _w(p, text, mode=0o600):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    os.chmod(p, mode)
    return p


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.delenv("HARVESTER_OPS_TF_WORKSPACES", raising=False)
    monkeypatch.delenv("HARVESTER_OPS_REPLICATION_DB", raising=False)
    st = tmp_path / "state"
    _w(st / "kubeconfigs" / "h1.yaml", "apiVersion: v1\nkind: Config\n# kc " + VALUE + "\n")
    _w(st / "clusters.d" / "h1.yaml", "name: h1\nkubeconfig: kubeconfigs/h1.yaml\nssh:\n  key: ssh/h1_id\n")
    _w(st / "ssh" / "h1_id", "-----BEGIN OPENSSH PRIVATE KEY-----\nh1" + VALUE + "\n-----END OPENSSH PRIVATE KEY-----\n")
    _w(st / "kvm.d" / "k1.yaml", json.dumps({"name": "k1", "ssh_key": "ssh/k1_id"}))
    _w(st / "ssh" / "k1_id", "-----BEGIN OPENSSH PRIVATE KEY-----\nk1" + VALUE + "\n-----END OPENSSH PRIVATE KEY-----\n")
    _w(st / "rancher.d" / "r1.oidc-secret", "oidc-" + VALUE + "\n")
    _w(st / "terraform" / ".kvm" / "k1" / "decls" / "d1" / ".secrets" / "rke2_token", "tok-" + VALUE + "\n")
    import replication_db as db
    conn = db.connect(st / "replication.db")
    db.set_setting(conn, "notify", {"enabled": True, "channels": [
        {"id": "mx-1", "kind": "matrix", "homeserver": "https://m", "room": "!r", "token": "mx-" + VALUE},
        {"id": "sl", "kind": "slack", "url": "https://hooks/slack/" + VALUE},
        {"id": "wh", "kind": "webhook", "url": "https://wh", "headers": {},
         "secret_headers": {"X-Api-Key": "hdr-" + VALUE}},
        {"id": "ml", "kind": "email", "smtp_host": "s", "password": "pw-" + VALUE}]}, by="t")
    conn.execute("INSERT INTO plans (id, name, source_cluster, target_cluster, rpo_objective_s, options,"
                 " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("rp-0123456789ab", "p1", "a", "b", 60,
                  json.dumps({"hooks": [{"name": "pre", "secret_headers": {"Authorization": "Bearer hk-" + VALUE}}]}),
                  "now", "now"))
    conn.close()
    return st


# ---------------------------------------------------------------------------
# Références
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ref", [
    "openbao:kubeconfig/h1#kubeconfig",
    "openbao:plan-run/a1/install#token",
    "openbao:rancher-oidc/r1#client_secret",
])
def test_openbao_ref_inactive(state, ref):
    st = ss.MultiStore(state)
    with pytest.raises(ss.SecretError) as e:
        st.get(ref)
    assert e.value.code == "openbao-inactive"
    with pytest.raises(ss.SecretError):
        with st.file(ref, "x"):
            pass


@pytest.mark.parametrize("ref", [
    "local:nope/h1#kubeconfig",
    "local:kubeconfig/../h1#kubeconfig",
    "local:kubeconfig/h1/..#kubeconfig",
    "openbao:kubeconfig/./h1#kubeconfig",
    "local:kubeconfig//etc/passwd#kubeconfig",
    "local:plan-run/A1#token",                  # 2.4.0 : déroulé en minuscules
    "local:kubeconfig/h1#Kubeconfig",
])
def test_bad_refs_read_nothing(state, ref, monkeypatch):
    opened = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: opened.append(self) or real(self, *a, **k))
    assert ss.is_ref(ref) is False
    with pytest.raises((ValueError, ss.SecretMissing)):
        ss.MultiStore(state).get(ref)
    assert not [p for p in opened if "kubeconfigs" in str(p) or "passwd" in str(p)]


@pytest.mark.parametrize("ref", ["local:kubeconfig/Lab_1#kubeconfig", "openbao:ssh-kvm/Node2.lan#private_key"])
def test_persistent_names_keep_case(ref):
    assert ss.is_ref(ref)


def test_unknown_field_for_class(state):
    with pytest.raises(ValueError):
        ss.MultiStore(state).get("local:kubeconfig/h1#token")


# ---------------------------------------------------------------------------
# Support fichier : emplacements d'aujourd'hui, même valeur que l'ancien lecteur
# ---------------------------------------------------------------------------
def _old_readers(st):
    """Ce que les lecteurs d'avant la 2.9.0 lisaient."""
    import replication_db as db
    conn = db.connect(st / "replication.db")
    notify = db.get_setting(conn, "notify")
    plan = db.get_plan(conn, "rp-0123456789ab")
    conn.close()
    ch = {c["id"]: c for c in notify["channels"]}
    return {
        "local:kubeconfig/h1#kubeconfig": (st / "kubeconfigs" / "h1.yaml").read_text(),
        "local:ssh-cluster/h1#private_key": (st / "ssh" / "h1_id").read_text(),
        "local:ssh-kvm/k1#private_key": (st / "ssh" / "k1_id").read_text(),
        "local:rancher-oidc/r1#client_secret": (st / "rancher.d" / "r1.oidc-secret").read_text().strip(),
        "local:tf-kvm/k1/d1/rke2_token#value":
            (st / "terraform/.kvm/k1/decls/d1/.secrets/rke2_token").read_text().strip(),
        "local:replication-notify/global#mx_1__token": ch["mx-1"]["token"],
        "local:replication-notify/global#sl__url": ch["sl"]["url"],
        "local:replication-notify/global#ml__password": ch["ml"]["password"],
        f"local:replication-notify/global#wh__{sc.header_field('X-Api-Key')}": ch["wh"]["secret_headers"]["X-Api-Key"],
        f"local:replication-hook/rp-0123456789ab/pre#{sc.header_field('Authorization')}":
            plan["options"]["hooks"][0]["secret_headers"]["Authorization"],
    }


def test_filestore_reads_current_locations(state):
    st = ss.MultiStore(state)
    old = _old_readers(state)
    for ref, expected in old.items():
        assert st.get(ref) == expected, ref
        assert VALUE in expected


def test_filestore_lists_each_class(state):
    fs = ss.FileStore(state)
    listed = {f"local:{c}/{p}#{f}" for c in sc.MIGRATABLE if c != "plan-run" for p, f in fs.list_local(c)}
    assert listed == set(_old_readers(state))


@pytest.mark.parametrize("ref", [
    "local:kubeconfig/h1#kubeconfig", "local:ssh-cluster/h1#private_key", "local:ssh-kvm/k1#private_key",
    "local:rancher-oidc/r1#client_secret", "local:tf-kvm/k1/d1/rke2_token#value",
])
def test_filestore_file_is_the_file_itself(state, ref):
    st = ss.MultiStore(state)
    with st.file(ref, "kubectl") as p:
        assert p.is_file() and VALUE in p.read_text()
        assert str(p).startswith(str(state))
    assert p.is_file()                          # rien n'est effacé
    assert st.shared_file(ref, "kubectl") == p


def test_filestore_follows_decl_path(state, tmp_path):
    other = _w(tmp_path / "elsewhere" / "kc.yaml", "kind: Config\n# other\n")
    (state / "clusters.d" / "h1.yaml").write_text(f"name: h1\nkubeconfig: {other}\n")
    assert ss.MultiStore(state).get("local:kubeconfig/h1#kubeconfig") == other.read_text()


@pytest.mark.parametrize("ref", [
    "local:kubeconfig/none#kubeconfig", "local:rancher-oidc/none#client_secret",
    "local:replication-notify/global#zz__token", "local:replication-hook/rp-000000000000/x#hdr_0000000000",
    "local:tf-kvm/k1/d1/none#value",
])
def test_filestore_missing(state, ref):
    with pytest.raises(ss.SecretMissing) as e:
        ss.MultiStore(state).get(ref)
    assert str(state) not in str(e.value)


def test_filestore_missing_db(tmp_path):
    with pytest.raises(ss.SecretMissing):
        ss.MultiStore(tmp_path).get("local:replication-notify/global#a__token")
    assert not (tmp_path / "replication.db").exists()          # jamais créée par une lecture


@pytest.mark.parametrize("ref,value", [
    ("local:rancher-oidc/r2#client_secret", "new-oidc"),
    ("local:tf-kvm/k1/d2/v#value", "new-tok"),
    ("local:ssh-kvm/k2#private_key", "KEY\n"),
])
def test_filestore_put_writes_current_location(state, ref, value):
    st = ss.MultiStore(state)
    _b, cls, path, field = ss.parse_ref_full(ref)
    assert st.put(cls, path, field, value) == ref
    assert st.get(ref) == value
    f = st.files.local_file(cls, path, field)
    assert (f.stat().st_mode & 0o777) == 0o600


def test_filestore_put_db_classes_refused(state):
    with pytest.raises(NotImplementedError):
        ss.MultiStore(state).put("replication-notify", "global", "a__token", "x")


# ---------------------------------------------------------------------------
# Support OpenBao
# ---------------------------------------------------------------------------
@pytest.fixture
def bao():
    s = FakeOpenBao().start()
    rid, sid = s.add_role()
    s.creds = (rid, sid)
    yield s
    s.stop()


def factory_for(srv, counter=None):
    base = oc.Client(srv.addr, timeout=2, retry_delays=(0.01,))
    base.login_approle(*srv.creds)

    def f(actor=None, action=None):
        if counter is not None:
            counter.append((actor, action))
        return base.with_headers(action, actor)
    return f


def settings_file(state, addr, **extra):
    doc = {"mode": "a", "addr": addr, "console_id": CID, **extra}
    _w(state / ss.SETTINGS_FILE, json.dumps(doc))


@pytest.fixture
def active(state, bao, tmp_path):
    work = tmp_path / "run"
    work.mkdir()
    settings_file(state, bao.addr, secret_files_dir=str(work / "hops-secrets"))
    st = ss.MultiStore(state, client_factory=factory_for(bao), mountinfo=tmpfs_mountinfo(str(work)))
    st.work = work
    return st


def test_put_get_with_metadata(active, bao):
    ref = active.openbao.put("kubeconfig", "h1", "kubeconfig", VALUE, actor="alice", action="act-1")
    assert ref == "openbao:kubeconfig/h1#kubeconfig"
    e = bao.kv[("harvester-ops", f"{PREFIX}/kubeconfig/h1")]
    assert e["versions"][1] == {"kubeconfig": VALUE}
    assert e["custom"] == {"hops_action": "act-1", "hops_actor": "alice", "hops_class": "kubeconfig",
                           "hops_v1_actor": "alice", "hops_v1_action": "act-1"}
    w = [r for r in bao.requests(f"/data/{PREFIX}/kubeconfig/h1") if r["method"] == "POST"][-1]
    assert json.loads(w["raw"])["options"] == {"cas": 0}
    assert w["headers"]["x-hops-actor"] == "alice"
    active.clear_cache()
    assert active.get(ref) == VALUE


def test_put_keeps_other_fields(active, bao):
    b = active.openbao
    b.put("replication-notify", "global", "a__token", "one")
    b.put("replication-notify", "global", "b__url", "two")
    assert bao.kv[("harvester-ops", f"{PREFIX}/replication-notify/global")]["versions"][2] == {
        "a__token": "one", "b__url": "two"}
    writes = [r for r in bao.requests(f"/data/{PREFIX}/replication-notify/global") if r["method"] == "POST"]
    assert [json.loads(w["raw"])["options"] for w in writes] == [{"cas": 0}, {"cas": 1}]


def test_migrated_class_writes_to_openbao(active, bao, state):
    settings_file(state, bao.addr, migrated={"rancher-oidc": {"at": "now"}},
                  secret_files_dir=str(active.work / "hops-secrets"))
    active._checked = 0
    active._sig = None
    ref = active.put("rancher-oidc", "r9", "client_secret", VALUE)
    assert ref.startswith("openbao:")
    assert not (state / "rancher.d" / "r9.oidc-secret").exists()


@pytest.mark.parametrize("cls", ["accounts", "web-auth", "public-keys", "openbao-credentials", "config-kubeconfig"])
def test_class_not_migratable(active, cls):
    with pytest.raises(ss.SecretError) as e:
        active.openbao.put(cls, "x", "value", VALUE)
    assert e.value.code == "class-not-migratable"
    with pytest.raises(sc.NotMigratable):
        sc.adapter(cls)


def test_secret_too_big(active):
    with pytest.raises(ss.SecretError) as e:
        active.openbao.put("kubeconfig", "big", "kubeconfig", "x" * (ss.VALUE_MAX + 1))
    assert e.value.code == "secret-too-big" and e.value.name == "big"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_cache_ttl(bao, tmp_path):
    clk = Clock()
    b = ss.OpenBaoStore({"mount": "harvester-ops", "prefix": PREFIX, "cache_ttl_min": 15},
                        factory_for(bao), files_dir=lambda: tmp_path, clock=clk)
    bao.put_raw("harvester-ops", f"{PREFIX}/kubeconfig/h1", {"kubeconfig": VALUE})
    ref = "openbao:kubeconfig/h1#kubeconfig"
    assert b.get(ref) == VALUE
    n = len(bao.requests(f"/data/{PREFIX}/kubeconfig/h1"))
    clk.t += 14 * 60
    assert b.get(ref) == VALUE
    assert len(bao.requests(f"/data/{PREFIX}/kubeconfig/h1")) == n          # servi par le cache
    clk.t += 2 * 60
    assert b.get(ref) == VALUE
    assert len(bao.requests(f"/data/{PREFIX}/kubeconfig/h1")) == n + 1      # relu après 15 min


def test_down_warm_cache_degraded(bao, tmp_path):
    clk = Clock()
    b = ss.OpenBaoStore({"mount": "harvester-ops", "prefix": PREFIX}, factory_for(bao),
                        files_dir=lambda: tmp_path, clock=clk)
    bao.put_raw("harvester-ops", f"{PREFIX}/kubeconfig/h1", {"kubeconfig": VALUE})
    assert b.get("openbao:kubeconfig/h1#kubeconfig") == VALUE
    bao.stop()
    assert b.probe() is False
    assert b.get("openbao:kubeconfig/h1#kubeconfig") == VALUE
    s = b.status()
    assert s["state"] == "degraded" and s["error"] == "openbao-unreachable" and s["cache_left_s"] > 800


def test_down_cold_cache_refused_no_local_fallback(active, bao, state):
    active.openbao.put("kubeconfig", "h1", "kubeconfig", VALUE)
    # la valeur locale a été remplacée par la référence ; l'ancienne copie reste à côté
    (state / "clusters.d" / "h1.yaml").write_text("name: h1\nkubeconfig: openbao:kubeconfig/h1#kubeconfig\n")
    active.clear_cache()
    bao.stop()
    for call in (lambda: active.get("openbao:kubeconfig/h1#kubeconfig"),
                 lambda: active.get("local:kubeconfig/h1#kubeconfig"),
                 lambda: active.shared_file("local:kubeconfig/h1#kubeconfig", "kubectl")):
        with pytest.raises(ss.SecretError) as e:
            call()
        assert e.value.code == "openbao-unreachable"
        assert (e.value.cls, e.value.name) == ("kubeconfig", "h1")
    assert active.status()["state"] == "down"


def test_local_value_replaced_by_ref_is_followed(active, bao, state):
    active.openbao.put("rancher-oidc", "r1", "client_secret", "from-bao")
    (state / "rancher.d" / "r1.oidc-secret").write_text("openbao:rancher-oidc/r1#client_secret\n")
    assert active.get("local:rancher-oidc/r1#client_secret") == "from-bao"
    assert ss.read_value_file(str(state / "rancher.d" / "r1.oidc-secret"), st=active) == "from-bao"


def test_file_and_shared_file_on_tmpfs(active, bao):
    ref = active.openbao.put("ssh-kvm", "k1", "private_key", VALUE)
    work = active.work / "hops-secrets"
    with active.file(ref, "ssh") as p:
        assert p.parent == work and p.read_text() == VALUE
        assert (p.stat().st_mode & 0o777) == 0o600 and (work.stat().st_mode & 0o777) == 0o700
    assert not p.exists()
    with pytest.raises(RuntimeError):
        with active.file(ref, "ssh") as p2:
            raise RuntimeError("boom")
    assert not p2.exists()
    s1 = active.shared_file(ref, "kubectl")
    assert active.shared_file(ref, "kubectl") == s1 and s1.read_text() == VALUE
    active.clear_cache()
    assert s1.exists()                       # ce fil la tient encore (revue I1)
    s2 = active.shared_file(ref, "kubectl")  # il en prend une neuve : l'ancienne est rendue
    assert s2 != s1 and not s1.exists()


def test_shared_file_expires(bao, tmp_path):
    clk = Clock()
    b = ss.OpenBaoStore({"mount": "harvester-ops", "prefix": PREFIX}, factory_for(bao),
                        files_dir=lambda: tmp_path, clock=clk)
    bao.put_raw("harvester-ops", f"{PREFIX}/kubeconfig/h1", {"kubeconfig": VALUE})
    p = b.shared_file("openbao:kubeconfig/h1#kubeconfig", "kubectl")
    clk.t += 16 * 60
    p2 = b.shared_file("openbao:kubeconfig/h1#kubeconfig", "kubectl")
    assert not p.exists() and p2.exists() and p2 != p


def test_no_cache_on_disk(active, bao, state):
    ref = active.openbao.put("kubeconfig", "h9", "kubeconfig", VALUE + "-disk")
    active.clear_cache()
    assert active.get(ref).endswith("-disk")
    for root in (state, active.work):
        for p in root.rglob("*"):
            if p.is_file():
                assert (VALUE + "-disk").encode() not in p.read_bytes(), p


def test_delete_tree_walks_leaves(active, bao):
    b = active.openbao
    for name in ("a1/install", "a1/bmc/0", "a1/bmc/1", "a10/install"):
        b.put("plan-run", name, "token", VALUE)
    assert active.refs("plan-run", "a1", backend="openbao") == [
        "openbao:plan-run/a1/bmc/0#token", "openbao:plan-run/a1/bmc/1#token", "openbao:plan-run/a1/install#token"]
    assert active.delete_tree("plan-run", "a1", backend="openbao") == 3
    assert [k for (_m, k) in bao.kv] == [f"{PREFIX}/plan-run/a10/install"]
    deletes = [r for r in bao.log if r["method"] == "DELETE"]
    assert len(deletes) == 3 and all("/metadata/" in r["path"] for r in deletes)


def test_delete_field_then_leaf(active, bao):
    b = active.openbao
    b.put("replication-notify", "global", "a__token", "1")
    b.put("replication-notify", "global", "b__token", "2")
    assert active.delete("openbao:replication-notify/global#a__token") is True
    assert bao.kv[("harvester-ops", f"{PREFIX}/replication-notify/global")]["versions"][3] == {"b__token": "2"}
    assert active.delete("openbao:replication-notify/global#b__token") is True
    assert ("harvester-ops", f"{PREFIX}/replication-notify/global") not in bao.kv
    assert active.delete("openbao:replication-notify/global#b__token") is False


def test_versions(active, bao):
    b = active.openbao
    b.put("kubeconfig", "h1", "kubeconfig", "v1", actor="alice", action="a1")
    b.put("kubeconfig", "h1", "kubeconfig", "v2", actor="bob", action="a2")
    vs = active.versions("openbao:kubeconfig/h1#kubeconfig")
    assert [v["version"] for v in vs] == [1, 2]
    assert vs[-1]["actor"] == "bob" and vs[-1]["action"] == "a2"
    assert vs[0]["actor"] == "alice" and vs[0]["action"] == "a1"       # auteur par version
    assert all(VALUE not in json.dumps(v) and "v2" != v.get("value") for v in vs)
    assert active.versions("local:kubeconfig/h1#kubeconfig") == []


def test_settings_change_hot(state, bao, tmp_path):
    st = ss.MultiStore(state, client_factory=factory_for(bao), mountinfo=tmpfs_mountinfo(str(tmp_path)))
    assert st.active() is False and st.status() == {"mode": "local", "state": "local"}
    settings_file(state, bao.addr)
    st._checked = 0
    assert st.active() is True
    (state / ss.SETTINGS_FILE).unlink()
    st._checked = 0
    assert st.active() is False


@pytest.mark.parametrize("raw,ttl", [({}, 15), ({"cache_ttl_min": 0}, 1), ({"cache_ttl_min": 500}, 120),
                                     ({"cache_ttl_min": "x"}, 15)])
def test_settings_cache_ttl_bounds(tmp_path, raw, ttl):
    _w(tmp_path / ss.SETTINGS_FILE, json.dumps(raw))
    assert ss.load_settings(tmp_path)["cache_ttl_min"] == ttl


def test_settings_prefix_from_console_id(tmp_path):
    _w(tmp_path / ss.SETTINGS_FILE, json.dumps({"mode": "b1", "addr": "https://127.0.0.1:8210", "console_id": CID}))
    s = ss.load_settings(tmp_path)
    assert s["prefix"] == PREFIX and ss.openbao_active(s)


# ---------------------------------------------------------------------------
# Aucune valeur dans les représentations et les erreurs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dump", [
    repr, str,
    lambda st: pickle.dumps(st).decode("latin-1"),
    lambda st: json.dumps(st, default=lambda o: o.__getstate__()),
    lambda st: repr(st.openbao) + repr(st.files),
])
def test_no_value_in_representations(active, bao, dump):
    active.put("plan-run", "a1", "token", VALUE + "-mem")
    active.openbao.put("kubeconfig", "h1", "kubeconfig", VALUE + "-bao")
    active.get("local:kubeconfig/h1#kubeconfig")
    text = dump(active)
    assert VALUE not in text


def test_no_value_in_errors(active, bao):
    active.openbao.put("kubeconfig", "h1", "kubeconfig", VALUE)
    active.clear_cache()
    bao.denied.append(f"harvester-ops/data/{PREFIX}/kubeconfig")
    with pytest.raises(ss.SecretError) as e:
        active.get("openbao:kubeconfig/h1#kubeconfig")
    assert e.value.code == "openbao-denied"
    for text in (str(e.value), repr(e.value), pickle.dumps(e.value).decode("latin-1")):
        assert VALUE not in text
    with pytest.raises(ss.SecretMissing) as m:
        active.get("openbao:rancher-oidc/none#client_secret")
    assert VALUE not in str(m.value)


def test_openbao_store_does_not_pickle(active):
    with pytest.raises(TypeError):
        pickle.loads(pickle.dumps(active.openbao))


def test_process_store_is_multi(tmp_path):
    assert isinstance(ss.store(), ss.MultiStore)
    assert isinstance(ss.store(), ss.LocalStore)


# ---------------------------------------------------------------------------
# Aides des lecteurs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["/etc/x/kc.yaml", "kubeconfigs/a.yaml", "", None, "openbao:nope"])
def test_path_of_and_resolve_value_pass_through(value, state):
    st = ss.MultiStore(state)
    assert ss.path_of(value, "kubectl", st=st) == value
    assert ss.resolve_value(value, st=st) == value


def test_path_of_local_ref(state):
    st = ss.MultiStore(state)
    assert ss.path_of("local:kubeconfig/h1#kubeconfig", "kubectl", st=st) == str(state / "kubeconfigs" / "h1.yaml")


def test_read_value_file(state):
    st = ss.MultiStore(state)
    assert ss.read_value_file(str(state / "rancher.d" / "r1.oidc-secret"), st=st) == "oidc-" + VALUE
    assert ss.read_value_file(str(state / "none"), st=st) is None
    assert ss.read_value_file(None, st=st) is None
    assert ss.read_value_file("local:rancher-oidc/r1#client_secret", st=st) == "oidc-" + VALUE
    assert ss.read_value_file("local:rancher-oidc/zz#client_secret", st=st) is None


def test_db_read_only(state):
    db = state / "replication.db"
    before = db.stat().st_mtime_ns
    ss.MultiStore(state).get("local:replication-notify/global#sl__url")
    assert db.stat().st_mtime_ns == before
    sqlite3.connect(db).close()


# ---------------------------------------------------------------------------
# Revue des tâches 2 à 4
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("how", ["soft-delete", "destroy"])
def test_put_after_latest_version_deleted(active, bao, how):
    """I5 : la dernière version supprimée (douce) ou détruite rend 404 à la
    lecture ; l'écriture suivante prend `cas` = version courante des
    métadonnées, sinon cas-conflict permanent."""
    import urllib.request
    b = active.openbao
    ref = b.put("kubeconfig", "h1", "kubeconfig", "v1")
    key = f"{PREFIX}/kubeconfig/h1"
    if how == "soft-delete":
        req = urllib.request.Request(f"{bao.addr}/v1/harvester-ops/data/{key}", method="DELETE")
    else:
        req = urllib.request.Request(f"{bao.addr}/v1/harvester-ops/destroy/{key}", method="POST",
                                     data=b'{"versions": [1]}', headers={"Content-Type": "application/json"})
    tok = next(iter(bao.tokens))
    req.add_header("X-Vault-Token", tok)
    urllib.request.urlopen(req).read()
    b.clear_cache()
    assert b.put("kubeconfig", "h1", "kubeconfig", "v2") == ref
    w = [r for r in bao.requests(f"/data/{key}") if r["method"] == "POST"][-1]
    assert json.loads(w["raw"])["options"] == {"cas": 1}
    b.clear_cache()
    assert active.get(ref) == "v2"


def _held_in_thread(fn):
    """Lance `fn` dans un fil qui garde son résultat jusqu'à `release()`."""
    import threading
    box, go = {}, threading.Event()

    def run():
        box["v"] = fn()
        go.wait(10)
    t = threading.Thread(target=run)
    t.start()
    while "v" not in box:
        pass

    def release():
        go.set()
        t.join()
    return box["v"], release


def test_shared_copy_held_by_a_live_action_survives_expiry_and_clear(bao, tmp_path):
    """I1 : une action (fil) qui a reçu la copie partagée la garde jusqu'à
    sa fin, même après l'expiration du cache ou « Vider le cache »."""
    clk = Clock()
    b = ss.OpenBaoStore({"mount": "harvester-ops", "prefix": PREFIX}, factory_for(bao),
                        files_dir=lambda: tmp_path, clock=clk)
    bao.put_raw("harvester-ops", f"{PREFIX}/kubeconfig/h1", {"kubeconfig": VALUE})
    ref = "openbao:kubeconfig/h1#kubeconfig"
    p, release = _held_in_thread(lambda: b.shared_file(ref, "kubectl"))
    clk.t += 16 * 60
    p2 = b.shared_file(ref, "kubectl")                 # un poller : nouvelle copie
    assert p2 != p and p.exists() and p.read_text() == VALUE
    b.clear_cache()
    assert p.exists()
    release()                                          # l'action se termine
    p3 = b.shared_file(ref, "kubectl")                 # balayage au passage suivant
    assert not p.exists() and p3.exists()
    b.purge()
    assert not p3.exists() and not p2.exists()


def test_shared_copy_hold_is_bounded(bao, tmp_path):
    clk = Clock()
    b = ss.OpenBaoStore({"mount": "harvester-ops", "prefix": PREFIX}, factory_for(bao),
                        files_dir=lambda: tmp_path, clock=clk)
    bao.put_raw("harvester-ops", f"{PREFIX}/kubeconfig/h1", {"kubeconfig": VALUE})
    ref = "openbao:kubeconfig/h1#kubeconfig"
    p, release = _held_in_thread(lambda: b.shared_file(ref, "kubectl"))
    try:
        b.clear_cache()
        clk.t += ss.SHARED_HOLD_MAX_S + 1
        b.shared_file(ref, "kubectl")
        assert not p.exists()
    finally:
        release()


def test_shared_copy_concurrent_miss_leaves_one_file(bao, tmp_path, monkeypatch):
    """M4 : deux échecs de cache simultanés ne laissent qu'une copie."""
    import threading
    b = ss.OpenBaoStore({"mount": "harvester-ops", "prefix": PREFIX}, factory_for(bao),
                        files_dir=lambda: tmp_path)
    bao.put_raw("harvester-ops", f"{PREFIX}/kubeconfig/h1", {"kubeconfig": VALUE})
    ref = "openbao:kubeconfig/h1#kubeconfig"
    bar = threading.Barrier(2)
    real = b.get

    def slow_get(r, actor=None):
        v = real(r, actor)
        bar.wait(5)
        return v
    monkeypatch.setattr(b, "get", slow_get)
    out = []
    ts = [threading.Thread(target=lambda: out.append(b.shared_file(ref, "kubectl"))) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out[0] == out[1]
    assert [p.name for p in tmp_path.iterdir()] == [out[0].name]


def test_copies_of_a_dropped_store_wait_for_their_holders(active, bao, state):
    """I1 : désactivation pendant une action, sa copie reste jusqu'à sa fin."""
    ref = active.openbao.put("kubeconfig", "h1", "kubeconfig", VALUE)
    p, release = _held_in_thread(lambda: active.shared_file(ref, "kubectl"))
    _w(state / ss.SETTINGS_FILE, json.dumps({"mode": "local"}))
    active._checked, active._sig = 0, None
    assert not active.active() and p.exists()
    active._sweep_old()
    assert p.exists()
    release()
    active._sweep_old()
    assert not p.exists() and active._old_bao == []


def test_version_authors_pruned_to_kept_versions(active, bao):
    """Auteur de chaque version gardé dans custom_metadata, borné aux
    versions que garde le montage (max_versions = 10)."""
    b = active.openbao
    for i in range(1, 13):
        b.put("kubeconfig", "h1", "kubeconfig", f"v{i}", actor=f"u{i}", action=f"a{i}")
    custom = bao.kv[("harvester-ops", f"{PREFIX}/kubeconfig/h1")]["custom"]
    kept = sorted(int(k[6:].split("_")[0]) for k in custom if k.startswith("hops_v") and k.endswith("_actor"))
    assert kept == list(range(3, 13))
    vs = active.versions("openbao:kubeconfig/h1#kubeconfig")
    assert [(v["version"], v["actor"]) for v in vs] == [(n, f"u{n}") for n in range(3, 13)]


def test_put_lost_reply_then_cas_conflict_is_a_success(bao, tmp_path):
    """M1 : écriture appliquée mais réponse perdue (délai), réessai refusé
    par cas : relue et comparée, ce n'est pas un conflit."""
    c = oc.Client(bao.addr, timeout=0.3, retry_delays=(1.0,))
    c.login_approle(*bao.creds)
    b = ss.OpenBaoStore({"mount": "harvester-ops", "prefix": PREFIX}, lambda a=None, x=None: c,
                        files_dir=lambda: tmp_path)
    bao.lose_reply, bao.lose_sleep = 1, 0.8
    ref = b.put("kubeconfig", "h1", "kubeconfig", VALUE, actor="alice", action="act-9")
    e = bao.kv[("harvester-ops", f"{PREFIX}/kubeconfig/h1")]
    assert e["current"] == 1 and e["versions"][1] == {"kubeconfig": VALUE}
    assert e["custom"]["hops_v1_actor"] == "alice"
    b.clear_cache()
    assert b.get(ref) == VALUE


def test_put_real_cas_conflict_still_refused(active, bao):
    b = active.openbao
    b.put("kubeconfig", "h1", "kubeconfig", "mine")
    real = bao.put_raw

    class Racer:                    # une autre écriture arrive entre la lecture et l'écriture
        done = False
    orig = bao._kv

    def racing(h, m, mount, rest, body, rec):
        if m == "POST" and rest.startswith("data/") and not Racer.done:
            Racer.done = True
            real(mount, rest[5:], {"kubeconfig": "theirs"})
        return orig(h, m, mount, rest, body, rec)
    bao._kv = racing
    with pytest.raises(ss.SecretError) as e:
        b.put("kubeconfig", "h1", "kubeconfig", "again")
    assert e.value.code == "openbao-cas-conflict"


def test_write_private_copy_leaves_nothing_on_failure(tmp_path):
    """M5 : une écriture qui échoue ne laisse pas de copie partielle."""
    with pytest.raises(UnicodeEncodeError):
        ss.write_private_copy(tmp_path, "x", "ok\udcff")
    assert list(tmp_path.iterdir()) == []


def test_ca_file_setting_is_loaded_and_unreadable_ca_is_a_client_error(tmp_path, monkeypatch):
    """M7 : `ca_file` est gardé par load_settings ; une CA illisible donne
    un code du client, pas une OSError brute."""
    _w(tmp_path / ss.SETTINGS_FILE, json.dumps({"mode": "a", "addr": "https://127.0.0.1:1",
                                                 "console_id": CID, "ca_file": str(tmp_path / "ca.pem")}))
    s = ss.load_settings(tmp_path)
    assert s["ca_file"] == str(tmp_path / "ca.pem")
    d = tmp_path / "ca.pem"
    d.mkdir()                                   # illisible comme fichier : IsADirectoryError
    monkeypatch.setattr(ss.Path, "is_file", lambda self: True)
    with pytest.raises(oc.OpenBaoError) as e:
        ss.default_client_factory(s)()
    assert e.value.code == "openbao-tls"


def test_tool_copies_erased_on_sigterm(tmp_path):
    """M6 : un outil tué par SIGTERM efface quand même ses copies tmpfs."""
    import signal
    import subprocess
    import textwrap
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT / 'bin' / 'lib')!r})
        import secret_store as ss
        b = ss.OpenBaoStore({{"mount": "m", "prefix": "p"}}, None, files_dir=lambda: {str(tmp_path)!r})
        b.get = lambda ref, actor=None: "v"
        ss._STORE = type("S", (), {{"purge": b.purge}})()
        print(b.shared_file("openbao:kubeconfig/h1#kubeconfig", "x"), flush=True)
        time.sleep(30)
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                         env={k: v for k, v in os.environ.items() if k != ss.SPAWNED_ENV})
    path = Path(p.stdout.readline().strip())
    assert path.exists()
    p.send_signal(signal.SIGTERM)
    p.wait(10)
    assert not path.exists()
