"""2.9.0, corrections de la revue de la tâche 8 (migration par classe).

Chaque cas a été vu rouge avant sa correction. Mutations jouées (rapport) :
« marque posée seulement avec une référence » (I3), « _set_decl ssh-kvm
sans verrou » (I1), « confirmation qui efface sans comparer » (I1),
« write_ref hors verrou » (I2), « marque posée en fin d'action » (I2),
« pas de seconde comparaison avant le remplacement » (I2).
"""

import fcntl
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_secret_migrate_290 import (  # noqa: E402,F401
    CLASSES, KC, KEY, OIDC, PREFIX, appenv, bao_value, cli, env, fs, gate, hsc, kv, kvm_decl, migrate, sc, ss,
    store, wapp)


def settings(env):
    return json.loads((env["state"] / ss.SETTINGS_FILE).read_text())


def _held(path):
    """Vrai si un autre détenteur tient le verrou `path` (flock)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# I3 : une classe vide est marquée migrée, ses secrets nouveaux vont dans OpenBao
# ---------------------------------------------------------------------------
EMPTY = {
    "ssh-kvm": (lambda st: [p.unlink() for p in (st / "kvm.d").glob("*.yaml")], "k7", "private_key", KEY),
    "rancher-oidc": (lambda st: (st / "rancher.d" / "r1.oidc-secret").unlink(), "r7", "client_secret", OIDC),
    "tf-kvm": (lambda st: (st / "terraform" / ".kvm" / "k1" / "decls" / "d1" / ".secrets" / "tok").unlink(),
               "k1/d7/tok", "value", "t"),
}


@pytest.mark.parametrize("cls", list(EMPTY))
def test_empty_class_is_marked_and_new_secrets_go_to_openbao(env, capsys, cls):
    empty, path, field, value = EMPTY[cls]
    empty(env["state"])
    rc, doc, _ = migrate(cls, capsys)
    assert rc == 0 and doc["classes"][cls] == {"done": [], "errors": []}
    mark = settings(env)["migrated"][cls]
    assert mark["complete"] is True and mark["by"] == "alice"
    ref = store(env).put(cls, path, field, value)
    assert ref == ss.make_ref(cls, path, field, "openbao") and bao_value(env, cls, path, field) == value
    # le retour en local retire la marque d'une classe sans référence restante
    store(env).delete(ref)
    assert migrate(cls, capsys, "local")[0] == 0
    assert cls not in settings(env).get("migrated", {})


def test_class_with_an_outage_is_not_marked(env, capsys):
    """Aucun secret n'a pu migrer (panne) : aucune référence, la marque posée
    au début de l'action est retirée."""
    srv = env["srv"]
    real = srv._kv

    def cut(h, m, mount, rest, body, rec):
        if "/ssh-kvm/" in rest:
            return h._send(503, {"errors": ["Vault is sealed"]})
        return real(h, m, mount, rest, body, rec)
    srv._kv = cut
    rc, doc, _ = migrate("ssh-kvm", capsys)
    assert rc == 1 and doc["classes"]["ssh-kvm"]["errors"]
    assert "ssh-kvm" not in settings(env).get("migrated", {})


# ---------------------------------------------------------------------------
# I1 et I2 : la référence est posée sous le verrou de la déclaration
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cls,lock", [
    ("kubeconfig", "clusters.d/.lock"),
    ("ssh-cluster", "clusters.d/.lock"),
    ("ssh-kvm", "kvm.d/.lock"),
])
def test_reference_is_set_under_the_declaration_lock(env, capsys, monkeypatch, cls, lock):
    """Lecture, comparaison, pose et retrait sous le verrou partagé avec la
    console (CONFIG_LOCK) et harvester-kvm (host_lock) : une écriture
    intercalée attend au lieu d'être perdue."""
    seen = []
    real_norm, real_set = ss.FileStore._norm, ss.FileStore._set_decl_locked

    def norm(self, c, data):
        seen.append(("read", _held(env["state"] / lock)))
        return real_norm(self, c, data)

    def set_decl(self, c, path, decl, key, value):
        seen.append(("set", _held(env["state"] / lock)))
        return real_set(self, c, path, decl, key, value)
    monkeypatch.setattr(ss.FileStore, "_norm", norm)
    monkeypatch.setattr(ss.FileStore, "_set_decl_locked", set_decl)
    assert migrate(cls, capsys)[0] == 0
    assert {k for k, _h in seen} == {"read", "set"} and all(h for _k, h in seen), seen


def test_kvm_restore_is_under_the_kvm_lock(env, capsys, monkeypatch):
    assert migrate("ssh-kvm", capsys)[0] == 0
    seen = []
    real = ss.FileStore._set_decl_locked

    def set_decl(self, *a):
        seen.append(_held(env["state"] / "kvm.d" / ".lock"))
        return real(self, *a)
    monkeypatch.setattr(ss.FileStore, "_set_decl_locked", set_decl)
    assert migrate("ssh-kvm", capsys, "local")[0] == 0
    assert seen == [True] and kvm_decl.load(env["state"], "k1")["ssh_key"] == "ssh/k1_id"


@pytest.mark.parametrize("cls", ["rancher-oidc", "tf-kvm"])
def test_file_class_is_marked_before_the_first_secret(env, capsys, monkeypatch, cls):
    """Classes sans verrou partagé avec leurs écrivains : la marque est posée
    avant la première référence, les écrivains vont alors dans OpenBao."""
    doc = settings(env)
    doc["migrated"] = {}
    (env["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    marks = []
    real = ss.FileStore.write_ref

    def probe(self, *a, **k):
        marks.append(cls in settings(env).get("migrated", {}))
        return real(self, *a, **k)
    monkeypatch.setattr(ss.FileStore, "write_ref", probe)
    assert migrate(cls, capsys)[0] == 0
    assert marks == [True]


def test_file_class_value_written_after_the_first_comparison_is_kept(env, capsys, monkeypatch):
    """Écriture intercalée après la première comparaison (copie `.migrated`
    déjà faite) : comparée encore avant le remplacement, rien n'est perdu
    et aucun original fantôme n'est laissé."""
    target = env["state"] / "rancher.d" / "r1.oidc-secret"
    real = kvm_decl.write_private

    def racing(path, data):
        out = real(path, data)
        if str(path).endswith(ss.MIGRATED_SUFFIX):
            real(target, "newer\n")
        return out
    monkeypatch.setattr(kvm_decl, "write_private", racing)
    rc, doc, _ = migrate("rancher-oidc", capsys)
    assert rc == 1 and doc["classes"]["rancher-oidc"]["errors"][0]["code"] == "local-reference-failed"
    assert target.read_text() == "newer\n"
    assert not target.with_name(target.name + ss.MIGRATED_SUFFIX).exists()
    assert sc.adapter("rancher-oidc").originals(fs(env)) == []


# ---------------------------------------------------------------------------
# I1 : la confirmation n'efface que l'original de la référence attendue
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case,local,erased", [
    ("decl-rewritten-file-absent", None, False),          # édition KVM concurrente : clé effacée
    ("decl-rewritten-other-value", "other", False),
    ("decl-rewritten-same-value", "original", True),      # la valeur est toujours là : copie inutile
    ("reference", "ref", True),
])
def test_confirm_checks_the_local_value(env, capsys, case, local, erased):
    st = env["state"]
    assert migrate("ssh-kvm", capsys)[0] == 0
    mig = st / "ssh" / "k1_id.migrated"
    original = mig.read_text()
    if local != "ref":
        doc = kvm_decl.load(st, "k1")
        doc["ssh_key"] = "ssh/k1_id"
        kvm_decl.write(st, doc)
        if local is not None:
            kvm_decl.write_private(st / "ssh" / "k1_id", original if local == "original" else "other")
    rc, doc, _ = cli(["confirm", "--class", "ssh-kvm"], capsys)
    assert mig.exists() is not erased
    if erased:
        assert rc == 0 and doc["originals_erased"] == 1
    else:
        assert rc == 1 and doc["errors"] == [{"secret": "ssh-kvm/k1#private_key",
                                              "code": "original-kept-local-changed"}]


def test_confirm_db_class_reference_replaced(env, capsys):
    """replication-notify : la valeur rangée n'est plus la référence (réglage
    réécrit) -> original gardé."""
    import replication_db as rdb
    assert migrate("replication-notify", capsys)[0] == 0
    conn = rdb.connect(env["state"] / "replication.db")
    try:
        cfg = rdb.get_setting(conn, "notify")
        cfg["channels"][0]["token"] = "typed-again"
        rdb.set_setting(conn, "notify", cfg)
    finally:
        conn.close()
    rc, doc, _ = cli(["confirm", "--class", "replication-notify"], capsys)
    assert rc == 1 and {"secret": "replication-notify/global#mx_1__token",
                        "code": "original-kept-local-changed"} in doc["errors"]
    assert len(sc.adapter("replication-notify").originals(fs(env))) == 1


# ---------------------------------------------------------------------------
# M8 : chemins du registre des originaux
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside", "ssh/../../outside"])
def test_originals_registry_paths_stay_in_the_state(env, capsys, tmp_path, bad):
    assert migrate("ssh-kvm", capsys)[0] == 0
    victim = tmp_path / "outside"
    victim.write_text("keep")
    p = env["state"] / ss.ORIGINALS_FILE
    doc = json.loads(p.read_text())
    doc["ssh-kvm/k1#private_key"]["file"] = bad
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        fs(env).erase_original("ssh-kvm", "k1", "private_key")
    assert victim.read_text() == "keep"


# ---------------------------------------------------------------------------
# C1 : secrets rangés par nom, renommage et propriété du chemin
# ---------------------------------------------------------------------------
import io  # noqa: E402

import yaml  # noqa: E402

KC2 = "apiVersion: v1\nkind: Config\nclusters: []\n# kc-other-cluster\n"


def _decl(st, name):
    return yaml.safe_load((st / "clusters.d" / f"{name}.yaml").read_text())


def _set_decl(st, decl_name, **kw):
    name = decl_name
    doc = _decl(st, name) if (st / "clusters.d" / f"{name}.yaml").exists() else {"name": name, "nodes": []}
    for k, v in kw.items():
        if k == "key":
            doc.setdefault("ssh", {})["key"] = v
        else:
            doc[k] = v
    kvm_decl.write_private(st / "clusters.d" / f"{name}.yaml", yaml.safe_dump(doc, sort_keys=False))


def _migrated_h1(appenv):
    """h1 avec kubeconfig et clé dans OpenBao (références dans la déclaration)."""
    st, s = appenv["state"], appenv["store"]
    kc = s.put("kubeconfig", "h1", "kubeconfig", KC)
    key = s.put("ssh-cluster", "h1", "private_key", KEY)
    _set_decl(st, "h1", kubeconfig=kc, key=key)
    (st / "kubeconfigs" / "h1.yaml").unlink()
    (st / "ssh" / "h1_id").unlink()


def _rename(c, old, new):
    return c.put(f"/api/clusters/{old}", json={
        "name": new, "nodes": [{"hostname": "n1", "ip": "10.0.0.1", "role": "control-plane"}],
        "ssh": {"user": "rancher"}})


def _gone(appenv, cls, path):
    e = appenv["srv"].kv.get(("harvester-ops", f"{PREFIX}/{cls}/{path}"))
    return e is None or not e["versions"].get(e["current"])


def test_rename_moves_openbao_secrets_and_the_old_name_is_free(appenv):
    w, st = appenv["wapp"], appenv["state"]
    _migrated_h1(appenv)
    with w.app.test_client() as c:
        r = _rename(c, "h1", "hb")
        assert r.status_code == 200, r.get_json()
        d = _decl(st, "hb")
        assert d["kubeconfig"] == "openbao:kubeconfig/hb#kubeconfig"
        assert d["ssh"]["key"] == "openbao:ssh-cluster/hb#private_key"
        assert bao_value(appenv, "kubeconfig", "hb", "kubeconfig") == KC
        assert bao_value(appenv, "ssh-cluster", "hb", "private_key") == KEY
        assert _gone(appenv, "kubeconfig", "h1") and _gone(appenv, "ssh-cluster", "h1")
        assert (st / "ssh" / "hb_id.pub").is_file() and not (st / "ssh" / "h1_id.pub").exists()
        # le nom libéré est repris : hb garde ses identifiants
        payload = {"name": "h1", "nodes": [{"hostname": "n1", "ip": "10.0.0.9", "role": "control-plane"}],
                   "ssh": {"user": "rancher"}}
        r = c.post("/api/clusters", data={"payload": json.dumps(payload),
                                          "kubeconfig": (io.BytesIO(KC2.encode()), "kc.yaml")},
                   content_type="multipart/form-data")
        assert r.status_code == 201, r.get_json()
        assert c.delete("/api/clusters/h1").status_code == 200
    assert bao_value(appenv, "kubeconfig", "hb", "kubeconfig") == KC


def test_rename_after_migration_moves_the_kept_original_and_restores(appenv, capsys):
    w, st = appenv["wapp"], appenv["state"]
    doc = settings(appenv)
    doc["migrated"] = {"rancher-oidc": {"at": 1}}
    (st / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    assert migrate("kubeconfig", capsys)[0] == 0
    appenv["store"]._sig = None
    with w.app.test_client() as c:
        assert _rename(c, "h1", "hb").status_code == 200
    reg = json.loads((st / ss.ORIGINALS_FILE).read_text())
    assert "kubeconfig/h1#kubeconfig" not in reg
    assert reg["kubeconfig/hb#kubeconfig"]["file"] == "kubeconfigs/hb.yaml.migrated"
    assert (st / "kubeconfigs" / "hb.yaml.migrated").is_file() and not (st / "kubeconfigs" / "h1.yaml.migrated").exists()
    # retour en local : hb reprend SES octets, à son emplacement
    assert migrate("kubeconfig", capsys, "local")[0] == 0
    assert (st / "kubeconfigs" / "hb.yaml").read_text() == CLASSES["kubeconfig"][0][2]
    assert _decl(st, "hb")["kubeconfig"] == "kubeconfigs/hb.yaml"


def test_rename_refused_when_the_new_name_holds_another_secret(appenv):
    w, st = appenv["wapp"], appenv["state"]
    _migrated_h1(appenv)
    appenv["srv"].put_raw("harvester-ops", f"{PREFIX}/kubeconfig/hb", {"kubeconfig": "orphan"})
    with w.app.test_client() as c:
        r = _rename(c, "h1", "hb")
    assert r.status_code == 409 and r.get_json()["code"] == "openbao-cas-conflict"
    assert _decl(st, "h1")["kubeconfig"] == "openbao:kubeconfig/h1#kubeconfig"   # rien changé
    assert bao_value(appenv, "kubeconfig", "h1", "kubeconfig") == KC
    assert bao_value(appenv, "kubeconfig", "hb", "kubeconfig") == "orphan"


def test_path_of_another_declaration_is_never_written_or_removed(appenv):
    """Déclaration d'avant la correction : hb désigne encore kubeconfig/h1.
    Créer h1 ou supprimer un autre cluster qui désigne ce chemin ne touche
    jamais au secret de hb."""
    w, st = appenv["wapp"], appenv["state"]
    _migrated_h1(appenv)
    os.rename(st / "clusters.d" / "h1.yaml", st / "clusters.d" / "hb.yaml")
    _set_decl(st, "hb", name="hb")
    with w.app.test_client() as c:
        payload = {"name": "h1", "nodes": [{"hostname": "n1", "ip": "10.0.0.9", "role": "control-plane"}],
                   "ssh": {"user": "rancher"}}
        r = c.post("/api/clusters", data={"payload": json.dumps(payload),
                                          "kubeconfig": (io.BytesIO(KC2.encode()), "kc.yaml")},
                   content_type="multipart/form-data")
        assert r.status_code == 409 and r.get_json()["code"] == "secret-path-owned"
        assert "kc-other" not in json.dumps(r.get_json())
        # une seconde déclaration qui désigne le même chemin, supprimée
        _set_decl(st, "hc", kubeconfig="openbao:kubeconfig/h1#kubeconfig")
        assert c.delete("/api/clusters/hc").status_code == 200
    assert bao_value(appenv, "kubeconfig", "h1", "kubeconfig") == KC


def test_hook_header_path_of_a_renamed_action_is_not_overwritten(appenv):
    w = appenv["wapp"]
    s = appenv["store"]
    import replication_db as rdb
    ref = s.put("replication-hook", "rp-1/pre", sc.header_field("Authorization"), "Bearer old")
    conn = rdb.connect(appenv["state"] / "replication.db")
    try:
        conn.execute("UPDATE plans SET options = ? WHERE id = 'rp-1'", (json.dumps(
            {"hooks": [{"name": "post", "url": "https://h", "secret_headers": {"Authorization": ref}}]}),))
    finally:
        conn.close()
    with pytest.raises(ss.SecretError) as e:
        w._repl_hooks_to_refs([{"name": "pre", "url": "https://h",
                                "secret_headers": {"Authorization": "Bearer new"}}], "rp-1")
    assert e.value.code == "secret-path-owned"
    assert bao_value(appenv, "replication-hook", "rp-1/pre", sc.header_field("Authorization")) == "Bearer old"


def test_bare_metal_name_taken_during_the_install(appenv, monkeypatch):
    w, st = appenv["wapp"], appenv["state"]
    run = appenv["run"]
    monkeypatch.setattr(ss, "secret_files_dir", lambda settings=None, mountinfo=None, env=None: run)
    kc = run / "bm-h1-kubeconfig"
    kc.write_text(KC2)
    key = run / "bm-h1-key"
    key.write_text(KEY)
    with pytest.raises(ValueError, match="already exists"):
        w._bm_declare_cluster("h1", kc, key, "n1", "10.0.0.5")
    assert not [k for k in appenv["srv"].kv if k[1].endswith("/kubeconfig/h1")]
    assert (st / "kubeconfigs" / "h1.yaml").read_text() == CLASSES["kubeconfig"][0][2]


def test_bare_metal_copy_not_taken_by_openbao_goes_to_the_persistent_place(appenv, monkeypatch):
    """Revue M6 : classe rendue au local pendant l'installation -> la copie
    tmpfs est écrite à l'emplacement persistant, jamais laissée sur tmpfs."""
    w, st = appenv["wapp"], appenv["state"]
    run = appenv["run"]
    kc = run / "bm-bm2-kubeconfig"
    kc.write_text(KC2)
    key = run / "bm-bm2-key"
    key.write_text(KEY)
    doc = settings(appenv)
    doc["migrated"] = {}
    (st / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    appenv["store"]._sig = None
    w._bm_declare_cluster("bm2", kc, key, "n1", "10.0.0.5")
    d = _decl(st, "bm2")
    assert (st / d["kubeconfig"]).read_text() == KC2 and (st / d["ssh"]["key"]).read_text() == KEY
    assert d["kubeconfig"] == "kubeconfigs/bm2.yaml" and d["ssh"]["key"] == "ssh/bm2_id"
    assert not kc.exists() and not key.exists()


@pytest.mark.parametrize("code,status", [("secret-too-big", 413), ("class-not-migratable", 400),
                                         ("secret-path-owned", 409), ("openbao-unreachable", 503)])
def test_secret_error_status(appenv, code, status):
    w = appenv["wapp"]
    with w.app.test_request_context():
        r, st = w._secret_store_refused(ss.SecretError(code, "kubeconfig", "h1"))
    assert st == status and r.get_json()["code"] == code


# ---------------------------------------------------------------------------
# I4 : outils de bin/ lancés seuls, classe migrée -> OpenBao, jamais le disque
# ---------------------------------------------------------------------------
import importlib.util  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / file)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def toolenv(env, monkeypatch):
    """Classes migrées, magasin du processus de l'outil sur le faux serveur."""
    from test_secret_migrate_290 import _client
    doc = settings(env)
    doc["migrated"] = {c: {"at": 1} for c in CLASSES}
    (env["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    st = ss.MultiStore(env["state"], client_factory=lambda actor=None, action=None: _client(env))
    monkeypatch.setattr(ss, "_STORE", st)
    return dict(env, store=st)


@pytest.mark.parametrize("outcome", ["added", "refused"])
def test_kvm_host_add_alone_puts_the_key_in_openbao(toolenv, capsys, monkeypatch, outcome):
    kvm = _load("hkvm_r8", "harvester-kvm.py")
    st = toolenv["state"]
    key = st.parent / "upload.key"
    key.write_text(KEY)
    monkeypatch.setattr(kvm, "check_private_key", lambda k, s: None)
    monkeypatch.setattr(kvm.kh, "control_path", lambda decl, state: None)   # état long sous xdist

    def fake_add(args, state, decl, k):
        if outcome == "refused":
            raise kvm.Failure("connection test failed")
        kvm_decl.write(state, decl)
        return 0
    monkeypatch.setattr(kvm, "_add_locked", fake_add)
    rc = kvm.main(["host", "add", "--name=k9", "--address=10.0.0.9", "--user=root", f"--ssh-key={key}",
                   "--fingerprint=SHA256:" + "A" * 43])
    capsys.readouterr()
    assert not (st / "ssh" / "k9_id").exists()                         # jamais sur le disque
    if outcome == "added":
        assert rc == 0 and kvm_decl.load(st, "k9")["ssh_key"] == "openbao:ssh-kvm/k9#private_key"
        assert bao_value(toolenv, "ssh-kvm", "k9", "private_key") == KEY
    else:
        assert rc != 0 and not kvm_decl.decl_path(st, "k9").exists()
        e = toolenv["srv"].kv.get(("harvester-ops", f"{PREFIX}/ssh-kvm/k9"))
        assert e is None or not e["versions"].get(e["current"])       # clé retirée avec l'ajout


def test_kvm_host_add_launched_by_the_console_never_logs_in(toolenv, monkeypatch):
    monkeypatch.setenv(ss.SPAWNED_ENV, "1")
    assert ss.cli_put("ssh-kvm", "k9", "private_key", KEY) is None
    assert not [k for k in toolenv["srv"].kv if k[1].endswith("/ssh-kvm/k9")]


def test_replication_notify_set_alone_stores_references(toolenv, capsys):
    import replication_db as rdb
    rep = _load("hrep_r8", "harvester-replication.py")
    spec = toolenv["state"].parent / "notify.json"
    spec.write_text(json.dumps({"enabled": True, "min_level": "warn", "codes": None, "channels": [
        {"id": "mx-1", "name": "", "kind": "matrix", "homeserver": "https://mx.example", "room": "!r:mx",
         "token": "syt_brand_new"},
        {"id": "wh", "name": "", "kind": "webhook", "url": "https://hook.example/x", "headers": {},
         "secret_headers": {"X-Api-Key": "openbao:replication-notify/global#wh__hdr_x"}}]}))
    rc = rep.main(["notify", "set", f"--db={toolenv['state'] / 'replication.db'}", f"--spec={spec}", "--json"])
    out = capsys.readouterr().out
    assert rc == 0 and "syt_brand_new" not in out
    conn = rdb.connect(toolenv["state"] / "replication.db")
    try:
        cfg = rdb.get_setting(conn, "notify")
    finally:
        conn.close()
    assert cfg["channels"][0]["token"] == "openbao:replication-notify/global#mx_1__token"
    assert cfg["channels"][1]["secret_headers"]["X-Api-Key"] == "openbao:replication-notify/global#wh__hdr_x"
    assert bao_value(toolenv, "replication-notify", "global", "mx_1__token") == "syt_brand_new"


def test_replication_plan_set_alone_stores_hook_headers(toolenv, capsys):
    import replication_db as rdb
    rep = _load("hrep_r8b", "harvester-replication.py")
    conn = rdb.connect(toolenv["state"] / "replication.db")
    try:
        conn.execute("UPDATE plans SET vms = ? WHERE id = 'rp-1'", (json.dumps(["default/vm1"]),))
    finally:
        conn.close()
    spec = toolenv["state"].parent / "plan.json"
    spec.write_text(json.dumps({"recovery": {"hooks": [
        {"name": "pre", "at": "start", "url": "https://hook.example/pre",
         "secret_headers": {"Authorization": "Bearer typed"}}]}}))
    rc = rep.main(["plan", "set", "p1", f"--db={toolenv['state'] / 'replication.db'}", f"--spec={spec}",
                   "--json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Bearer typed" not in out
    conn = rdb.connect(toolenv["state"] / "replication.db")
    try:
        opts = json.loads(conn.execute("SELECT options FROM plans WHERE id = 'rp-1'").fetchone()[0])
    finally:
        conn.close()
    ref = opts["hooks"][0]["secret_headers"]["Authorization"]
    assert ref == f"openbao:replication-hook/rp-1/pre#{sc.header_field('Authorization')}"
    assert bao_value(toolenv, "replication-hook", "rp-1/pre", sc.header_field("Authorization")) == "Bearer typed"


# ---------------------------------------------------------------------------
# Mineurs de la revue de la tâche 8
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("argv,rc,code", [
    (["--name", "r1", "--replace-existing"], 0, None),
    (["--replace-existing"], 2, "replace-needs-name"),                 # jamais en masse
    ([], 1, "openbao-cas-conflict"),
])
def test_conflict_escape_replaces_one_named_secret(env, capsys, argv, rc, code):
    """Revue M2 : un chemin qui garde une ancienne valeur ne bloque plus la
    reprise pour toujours ; le remplacement est explicite, nommé, et garde
    l'ancienne valeur comme version précédente."""
    env["srv"].put_raw("harvester-ops", f"{PREFIX}/rancher-oidc/r1", {"client_secret": "stale"})
    got, doc, _ = migrate("rancher-oidc", capsys, extra=argv)
    assert got == rc, doc
    e = env["srv"].kv[("harvester-ops", f"{PREFIX}/rancher-oidc/r1")]
    if rc == 0:
        assert e["versions"][e["current"]]["client_secret"] == OIDC
        assert any(v.get("client_secret") == "stale" for n, v in e["versions"].items() if n != e["current"])
        assert doc["classes"]["rancher-oidc"]["done"][0]["replaced"] is True
        assert ss.is_openbao_ref(fs(env).read_local("rancher-oidc", "r1", "client_secret"))
    else:
        assert (doc.get("code") or doc["classes"]["rancher-oidc"]["errors"][0]["code"]) == code
        assert e["versions"][e["current"]]["client_secret"] == "stale"
        assert fs(env).read_local("rancher-oidc", "r1", "client_secret") == OIDC


@pytest.mark.parametrize("changed,why", [
    ({"addr": "https://other.example:8200"}, "changed since the simulated outage"),
    ({"mode": "b1"}, "changed since the simulated outage"),
    ({}, None),
])
def test_outage_test_is_tied_to_the_server(env, capsys, changed, why):
    """Revue M7 : la panne simulée vaut pour le serveur et le mode d'alors."""
    assert migrate("rancher-oidc", capsys)[0] == 0
    assert cli(["outage-test"], capsys)[0] == 0
    doc = settings(env)
    assert doc["outage_test"]["addr"] == doc["addr"] and doc["outage_test"]["mode"] == "a"
    doc.update(changed)
    assert hsc._kubeconfig_gate(doc) == why or why in (hsc._kubeconfig_gate(doc) or "")


def test_originals_of_an_unmarked_class_still_expire(appenv, capsys, monkeypatch):
    """Revue M3 : une classe dont la marque est tombée garde ses originaux
    soumis au délai de garde."""
    w = appenv["wapp"]
    doc = settings(appenv)
    doc["migrated"] = {}
    (appenv["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    assert migrate("rancher-oidc", capsys)[0] == 0
    doc = settings(appenv)
    doc["migrated"].pop("rancher-oidc")                      # marque tombée
    (appenv["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    appenv["store"]._sig = None
    from test_secret_migrate_290 import age
    age(appenv, "rancher-oidc", 8)
    due, _days = w._secret_originals_due()
    assert "rancher-oidc" in due


@pytest.mark.parametrize("route,field,local", [
    ("kubeconfig", "kubeconfig", "kubeconfigs/h1.yaml"),
    ("sshkey", "ssh_key", "ssh/h1_id"),
])
def test_upload_into_migrated_class_erases_the_old_clear_file(appenv, route, field, local):
    """Revue M4 : cluster resté local d'une classe migrée en partie -> le
    téléversement pose la référence ET retire l'ancien fichier en clair."""
    w, st = appenv["wapp"], appenv["state"]
    assert (st / local).is_file()
    body = KC2.encode() if route == "kubeconfig" else KEY.encode()
    with w.app.test_client() as c:
        r = c.post(f"/api/clusters/h1/{route}", data={"file": (io.BytesIO(body), "f")},
                   content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()[field].startswith("openbao:")
    assert not (st / local).exists()


def test_kvm_host_add_copy_on_tmpfs_when_migrated(appenv, monkeypatch):
    """Revue M5 : classe ssh-kvm migrée -> la copie de travail de la clé est
    sur le répertoire tmpfs, jamais sous <état>/ssh ; clé non UTF-8 -> 400."""
    w, st, run = appenv["wapp"], appenv["state"], appenv["run"]
    monkeypatch.setattr(ss, "secret_files_dir", lambda settings=None, mountinfo=None, env=None: run)
    seen = {}

    def fake_action(host, label, cmd, cleanup=None, cluster=None, waits=False, secret_files=None):
        seen.update(cmd=cmd)
        return types_ns(id="a1"), None
    monkeypatch.setattr(w, "_kvm_action", fake_action)
    monkeypatch.setattr(w, "_kvm_unavailable", lambda: None)
    payload = {"name": "k9", "address": "10.0.0.9", "port": 22, "user": "root",
               "fingerprint": "SHA256:" + "A" * 43, "labels": {}}
    with w.app.test_client() as c:
        r = c.post("/api/kvm/hosts", data={"payload": json.dumps(payload),
                                           "key": (io.BytesIO(b"\xff\xfe-----BEGIN"), "k")},
                   content_type="multipart/form-data")
        assert r.status_code == 400
        r = c.post("/api/kvm/hosts", data={"payload": json.dumps(payload), "key": (io.BytesIO(KEY.encode()), "k")},
                   content_type="multipart/form-data")
    assert r.status_code == 202, r.get_json()
    key_arg = next(a for a in seen["cmd"] if a.startswith("--ssh-key="))[len("--ssh-key="):]
    assert Path(key_arg).parent.parent == run and not list((st / "ssh").glob("kvm-key-*"))


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


# ---------------------------------------------------------------------------
# M3 (seconde moitié) : jamais de `.migrated` inconnu du registre des originaux
# ---------------------------------------------------------------------------
def _registry(env):
    p = env["state"] / ss.ORIGINALS_FILE
    return json.loads(p.read_text()) if p.exists() else {}


@pytest.mark.parametrize("cls", ["rancher-oidc", "ssh-kvm"])
def test_kept_original_is_registered_before_it_exists(env, capsys, monkeypatch, cls):
    """A crash right after the `.migrated` copy is written must not leave a
    plaintext copy the registry (hence the keep delay and the confirmation)
    does not know: the entry is saved first."""
    real = kvm_decl.write_private
    seen = []

    def probe(path, data):
        if str(path).endswith(ss.MIGRATED_SUFFIX):
            rel = os.path.relpath(path, env["state"])
            seen.append(any(isinstance(e, dict) and Path(e.get("file", "")).name == Path(rel).name
                            for e in _registry(env).values()))
        return real(path, data)
    monkeypatch.setattr(kvm_decl, "write_private", probe)
    assert migrate(cls, capsys)[0] == 0
    assert seen and all(seen), seen


@pytest.mark.parametrize("cls", ["rancher-oidc", "ssh-kvm"])
def test_failed_copy_leaves_neither_entry_nor_file(env, capsys, monkeypatch, cls):
    real = kvm_decl.write_private

    def failing(path, data):
        if str(path).endswith(ss.MIGRATED_SUFFIX):
            real(path, data)
            raise OSError(28, "No space left on device")
        return real(path, data)
    monkeypatch.setattr(kvm_decl, "write_private", failing)
    migrate(cls, capsys)
    assert not [e for e in _registry(env).values() if isinstance(e, dict) and e.get("cls") == cls]
    assert not list(env["state"].rglob("*" + ss.MIGRATED_SUFFIX))
