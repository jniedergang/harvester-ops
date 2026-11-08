"""2.9.0 tâche 8 : migration par classe vers OpenBao et retour en local.

Une ligne par classe et par sens, contre le faux serveur (fake_openbao_b1),
avec l'outil `harvester-secrets` joué dans le processus et les emplacements
réels de chaque classe (déclarations, fichiers 0600, replication.db).

Mutations jouées (rapport) : « cas absent », « original effacé avant la
relecture comparée », « secret_rollback vidé avant la confirmation »,
« délai de garde ignoré », « original effacé sans relire OpenBao »,
« référence posée sans garder l'original ».
"""

import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

spec = importlib.util.spec_from_file_location("hsc_mig", ROOT / "bin" / "harvester-secrets.py")
hsc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hsc)

import kvm_decl  # noqa: E402
import openbao_client as oc  # noqa: E402
import openbao_policy as pol  # noqa: E402
import replication_db as rdb  # noqa: E402
import replication_notify as rn  # noqa: E402
import secret_classes as sc  # noqa: E402
import secret_store as ss  # noqa: E402
from fake_openbao_b1 import FakeB1  # noqa: E402

CID = "abc123def456"
PREFIX = f"consoles/{CID}"
KC = "apiVersion: v1\nkind: Config\nclusters: []\n# kc-secret-290  \n"
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nkey-secret-290\n-----END OPENSSH PRIVATE KEY-----\n"
OIDC = "oidc-secret-290"                      # sans fin de ligne : octets rendus à l'identique
TOK = "tf-secret-290\n"
MX = "syt_mx_secret_290"
HDR = "hdr-secret-290"
HOOK = "Bearer hook-secret-290"


@pytest.fixture
def env(tmp_path, monkeypatch):
    state, etc, run = tmp_path / "state", tmp_path / "etc" / "openbao", tmp_path / "run"
    for d in (state, etc, run):
        d.mkdir(parents=True)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setenv("HARVESTER_OPS_OPENBAO_DIR", str(etc))
    for k in ("HARVESTER_OPS_TF_WORKSPACES", "HARVESTER_OPS_REPLICATION_DB", ss.SPAWNED_ENV, ss.FILES_ENV):
        monkeypatch.delenv(k, raising=False)
    srv = FakeB1(CID).start()
    rid = "rid-console-1"
    srv.role_ids[pol.console_role(CID)] = rid
    _r, sid = srv.add_role(rid)
    (etc / "role-id").write_text(rid)
    (etc / "secret-id").write_text(sid)
    (state / ss.SETTINGS_FILE).write_text(json.dumps(
        {"mode": "a", "addr": srv.addr, "console_id": CID, "secret_files_dir": str(run),
         "dev_disk_secret_files": True}))
    real = hsc.admin_client

    def quick(*a, **k):                        # pas d'attente entre les essais du client
        c = real(*a, **k)
        c._delays = (0.01,)
        return c
    monkeypatch.setattr(hsc, "admin_client", quick)
    seed(state)
    gate({"state": state})                     # barrière de la classe kubeconfig levée (point 255)
    yield {"state": state, "srv": srv, "run": run}
    srv.stop()


def seed(state):
    """Un secret de chaque classe à son emplacement d'aujourd'hui."""
    (state / "clusters.d").mkdir()
    for n in ("h1", "h2"):
        # écrite comme la console (_write_console_cluster)
        kvm_decl.write_private(state / "clusters.d" / f"{n}.yaml", yaml.safe_dump(
            {"name": n, "nodes": [], "kubeconfig": f"kubeconfigs/{n}.yaml", "ssh": {"user": "rancher",
                                                                                 "key": f"ssh/{n}_id"}},
            default_flow_style=False, sort_keys=False))
        kvm_decl.write_private(state / "kubeconfigs" / f"{n}.yaml", KC.replace("290", f"290-{n}"))
        kvm_decl.write_private(state / "ssh" / f"{n}_id", KEY.replace("290", f"290-{n}"))
    (state / "ssh" / "h1_id.pub").write_text("ssh-ed25519 AAAA public\n")
    kvm_decl.write(state, {"name": "k1", "address": "10.0.0.1", "user": "root", "ssh_key": "ssh/k1_id",
                           "known_hosts": "ssh/k1_known"})
    kvm_decl.write_private(state / "ssh" / "k1_id", KEY.replace("290", "290-k1"))
    kvm_decl.write_private(state / "rancher.d" / "r1.oidc-secret", OIDC)
    kvm_decl.write_private(state / "terraform" / ".kvm" / "k1" / "decls" / "d1" / ".secrets" / "tok", TOK)
    conn = rdb.connect(state / "replication.db")
    try:
        rdb.set_setting(conn, "notify", {"enabled": True, "min_level": "warn", "codes": None, "channels": [
            {"id": "mx-1", "name": "", "kind": "matrix", "homeserver": "https://mx.example", "room": "!r:mx",
             "token": MX},
            {"id": "wh", "name": "", "kind": "webhook", "url": "https://hook.example/x", "headers": {},
             "secret_headers": {"X-Api-Key": HDR}}]})
        conn.execute("INSERT INTO plans (id, name, source_cluster, target_cluster, rpo_objective_s, created_at,"
                     " updated_at, options) VALUES ('rp-1', 'p1', 'h1', 'h2', 600, '', '', ?)",
                     (json.dumps({"hooks": [{"name": "pre", "url": "https://hook.example/pre",
                                             "secret_headers": {"Authorization": HOOK}}]}),))
    finally:
        conn.close()


def cli(argv, capsys):
    sys.stdin = io.StringIO("")
    try:
        rc = hsc.main(["--id", "act-290", "--actor", "alice"] + argv)
    finally:
        sys.stdin = sys.__stdin__
    o = capsys.readouterr()
    try:
        return rc, json.loads(o.out), o.err
    except ValueError:
        return rc, o.out, o.err


def snapshot(state):
    """Octets et droits de tous les fichiers, et le JSON de la base."""
    files = {}
    for p in sorted(state.rglob("*")):
        if p.is_file() and not p.name.startswith(".") and "replication.db" not in p.name \
                and p.name not in (ss.SETTINGS_FILE, ss.ORIGINALS_FILE, ss.ORIGINALS_LOCK):
            files[str(p.relative_to(state))] = (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
    conn = rdb.connect(state / "replication.db")
    try:
        db = (rdb.get_setting(conn, "notify"), json.loads(conn.execute("SELECT options FROM plans").fetchone()[0]))
    finally:
        conn.close()
    return files, db


def kv(env, cls):
    return {p: e for (m, p), e in env["srv"].kv.items() if p.startswith(f"{PREFIX}/{cls}/")}


def fs(env):
    return ss.FileStore(env["state"])


def store(env):
    st = ss.MultiStore(env["state"], client_factory=lambda actor=None, action=None: _client(env))
    return st


def _client(env):
    d = Path(os.environ["HARVESTER_OPS_OPENBAO_DIR"])
    c = oc.Client(env["srv"].addr, timeout=2, retry_delays=(0.01,))
    c.login_approle((d / "role-id").read_text(), (d / "secret-id").read_text())
    return c


# valeur attendue de chaque secret, et sa lecture par le chemin des lecteurs
CLASSES = {
    "kubeconfig": [("h1", "kubeconfig", KC.replace("290", "290-h1")), ("h2", "kubeconfig", KC.replace("290", "290-h2"))],
    "ssh-cluster": [("h1", "private_key", KEY.replace("290", "290-h1")),
                    ("h2", "private_key", KEY.replace("290", "290-h2"))],
    "ssh-kvm": [("k1", "private_key", KEY.replace("290", "290-k1"))],
    "rancher-oidc": [("r1", "client_secret", OIDC)],
    "tf-kvm": [("k1/d1/tok", "value", TOK.strip())],
    "replication-notify": [("global", "mx_1__token", MX), ("global", sc.notify_field("wh", ("header", "X-Api-Key")), HDR)],
    "replication-hook": [("rp-1/pre", sc.header_field("Authorization"), HOOK)],
}


def migrate(cls, capsys, to="openbao", extra=()):
    return cli(["migrate", "--to", to, "--class", cls, *extra], capsys)


# ---------------------------------------------------------------------------
# Aller et retour, une ligne par classe
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cls", list(CLASSES))
def test_round_trip_identical_bytes(env, capsys, cls):
    before = snapshot(env["state"])
    rc, doc, err = migrate(cls, capsys)
    assert rc == 0, doc
    f = fs(env)
    ad = sc.adapter(cls)
    st = store(env)
    for path, field, value in CLASSES[cls]:
        ref = ss.make_ref(cls, path, field, "openbao")
        assert ad.read_local(f, path, field) == ref                       # la référence à la place
        assert st.get(ss.make_ref(cls, path, field)) == value              # suivie par le magasin
    assert len(ad.originals(f)) == len(CLASSES[cls])                       # originaux gardés
    assert cls in json.loads((env["state"] / ss.SETTINGS_FILE).read_text())["migrated"]
    for value in [v for _p, _f, v in CLASSES[cls]]:
        assert value.strip() not in err and value.strip() not in json.dumps(doc)
    rc, doc, _ = migrate(cls, capsys, "local")
    assert rc == 0, doc
    assert snapshot(env["state"]) == before                                # octets et droits d'avant
    assert not kv(env, cls) or all(not e["versions"] or e["current"] == 0 for e in kv(env, cls).values())
    assert ad.originals(f) == []
    assert cls not in json.loads((env["state"] / ss.SETTINGS_FILE).read_text()).get("migrated", {})


@pytest.mark.parametrize("cls", ["kubeconfig", "ssh-cluster", "ssh-kvm", "rancher-oidc", "tf-kvm"])
def test_originals_are_private_files(env, capsys, cls):
    assert migrate(cls, capsys)[0] == 0
    migrated = [p for p in env["state"].rglob("*" + ss.MIGRATED_SUFFIX)]
    assert migrated and all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in migrated)
    assert stat.S_IMODE((env["state"] / ss.ORIGINALS_FILE).stat().st_mode) == 0o600
    # aucune valeur dans le registre des originaux
    reg = (env["state"] / ss.ORIGINALS_FILE).read_text()
    assert all(v.strip() not in reg for _p, _f, v in CLASSES[cls])


def test_decl_file_classes_keep_the_public_key_and_remove_the_original(env, capsys):
    assert migrate("ssh-cluster", capsys)[0] == 0
    st = env["state"]
    assert not (st / "ssh" / "h1_id").exists() and (st / "ssh" / "h1_id.migrated").is_file()
    assert (st / "ssh" / "h1_id.pub").is_file()
    assert "openbao:ssh-cluster/h1#private_key" in (st / "clusters.d" / "h1.yaml").read_text()
    assert kvm_decl.load(st, "k1")["ssh_key"] == "ssh/k1_id"               # autre classe intacte


# ---------------------------------------------------------------------------
# Refus et pannes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cls", list(CLASSES))
def test_conflict_keeps_local(env, capsys, cls):
    path, field, _v = CLASSES[cls][0]
    env["srv"].put_raw("harvester-ops", f"{PREFIX}/{cls}/{path}", {field: "someone else"})
    before = snapshot(env["state"])
    rc, doc, _ = migrate(cls, capsys)
    assert rc == 1
    errs = doc["classes"][cls]["errors"]
    assert {"secret": f"{cls}/{path}#{field}", "code": "openbao-cas-conflict"} in errs
    assert sc.adapter(cls).read_local(fs(env), path, field) == CLASSES[cls][0][2]
    e = env["srv"].kv[("harvester-ops", f"{PREFIX}/{cls}/{path}")]
    assert e["versions"][e["current"]][field] == "someone else"           # jamais écrasée
    if len(CLASSES[cls]) == 1:
        assert snapshot(env["state"]) == before


def test_cas_zero_is_sent(env, capsys):
    """Mutation « cas absent » : sans `cas`, l'écriture écraserait la valeur
    d'un autre ; le faux serveur la refuse seulement avec `cas=0`."""
    assert migrate("rancher-oidc", capsys)[0] == 0
    posts = [r for r in env["srv"].log if r["method"] == "POST" and r["path"].endswith("/rancher-oidc/r1")
             and "/data/" in r["path"]]
    assert posts and json.loads(posts[0]["raw"])["options"] == {"cas": 0}


@pytest.mark.parametrize("cls", ["kubeconfig", "replication-notify"])
def test_readback_altered_stops_and_touches_nothing(env, capsys, cls):
    srv = env["srv"]
    real = srv._kv
    seen = {"posted": False}

    def altered(h, m, mount, rest, body, rec):
        if m == "POST" and rest.startswith("data/"):
            seen["posted"] = True
        if m == "GET" and rest.startswith("data/") and seen["posted"]:
            return h._send(200, {"data": {"data": {"x": "altered"}, "metadata": {"version": 1}}})
        return real(h, m, mount, rest, body, rec)
    srv._kv = altered
    before = snapshot(env["state"])
    rc, doc, _ = migrate(cls, capsys)
    assert rc == 1 and doc["classes"][cls]["errors"][0]["code"] == "migrate-readback-mismatch"
    assert snapshot(env["state"]) == before and sc.adapter(cls).originals(fs(env)) == []


def test_outage_in_the_middle_resumes(env, capsys):
    """Coupure après le premier cluster : h1 en référence, h2 intact ; la
    relance reprend h2 sans réécrire h1."""
    srv = env["srv"]
    real = srv._kv

    def cut(h, m, mount, rest, body, rec):
        if rest.endswith("/kubeconfig/h2"):
            return h._send(500, {"errors": ["internal error"]})
        return real(h, m, mount, rest, body, rec)
    srv._kv = cut
    rc, doc, _ = migrate("kubeconfig", capsys)
    assert rc == 1 and doc["classes"]["kubeconfig"]["errors"][0]["code"] == "openbao-internal"
    f = fs(env)
    assert ss.is_openbao_ref(f.read_local("kubeconfig", "h1", "kubeconfig"))
    assert f.read_local("kubeconfig", "h2", "kubeconfig") == CLASSES["kubeconfig"][1][2]
    mig = json.loads((env["state"] / ss.SETTINGS_FILE).read_text())["migrated"]["kubeconfig"]
    assert mig["complete"] is False                       # migrée en partie : disable refusé
    assert cli(["disable", "--dry-run"], capsys)[0] == 2
    srv._kv = real
    rc, doc, _ = migrate("kubeconfig", capsys)
    assert rc == 0, doc
    assert env["srv"].kv[("harvester-ops", f"{PREFIX}/kubeconfig/h1")]["current"] == 1
    assert ss.is_openbao_ref(f.read_local("kubeconfig", "h2", "kubeconfig"))


def gate(env):
    doc = json.loads((env["state"] / ss.SETTINGS_FILE).read_text())
    doc["migrated"] = dict(doc.get("migrated") or {}, **{"rancher-oidc": {"at": 1}})
    doc["outage_test"] = {"ok_at": 1}
    (env["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))


@pytest.mark.parametrize("migrated,outage,why", [
    ({}, None, "migrate another class first"),
    ({"rancher-oidc": {"at": 1}}, None, "simulated outage"),
    ({"kubeconfig": {"at": 1}}, {"ok_at": 1}, "migrate another class first"),
])
def test_kubeconfig_gate(env, capsys, migrated, outage, why):
    doc = json.loads((env["state"] / ss.SETTINGS_FILE).read_text())
    doc.update(migrated=migrated, outage_test=outage)
    (env["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    rc, out, _ = migrate("kubeconfig", capsys)
    assert rc == 2 and out["code"] == "kubeconfig-gate" and why in out["error"]
    assert not kv(env, "kubeconfig")


def test_kubeconfig_too_big_stays_local_others_migrate(env, capsys):
    gate(env)
    big = "a: " + "x" * (ss.VALUE_MAX + 1) + "\n"
    kvm_decl.write_private(env["state"] / "kubeconfigs" / "h2.yaml", big)
    rc, doc, _ = migrate("kubeconfig", capsys)
    assert rc == 1 and doc["classes"]["kubeconfig"]["errors"] == [
        {"secret": "kubeconfig/h2#kubeconfig", "code": "secret-too-big"}]
    f = fs(env)
    assert ss.is_openbao_ref(f.read_local("kubeconfig", "h1", "kubeconfig"))
    assert (env["state"] / "kubeconfigs" / "h2.yaml").read_text() == big


@pytest.mark.parametrize("decl,code", [
    ("name: h3\nnodes: []\nkubeconfig: /etc/other/h3.yaml\n", "secret-outside-state"),
    (None, "secret-no-declaration"),
])
def test_kubeconfig_kept_local(env, capsys, tmp_path, decl, code):
    gate(env)
    st = env["state"]
    outside = tmp_path / "op.yaml"
    outside.write_text(KC)
    if decl:
        (st / "clusters.d" / "h3.yaml").write_text(decl.replace("/etc/other/h3.yaml", str(outside)))
    else:
        kvm_decl.write_private(st / "kubeconfigs" / "h3.yaml", KC)       # kubeconfig sans déclaration
    rc, doc, _ = migrate("kubeconfig", capsys)
    assert {"secret": "kubeconfig/h3#kubeconfig", "code": code} in doc["classes"]["kubeconfig"]["errors"]
    assert outside.read_text() == KC and not kv(env, "kubeconfig").get(f"{PREFIX}/kubeconfig/h3")


def test_config_yaml_kubeconfigs_never_touched(env, capsys, tmp_path):
    gate(env)
    cfg_kc = tmp_path / "config-kubeconfigs" / "c0.yaml"
    cfg_kc.parent.mkdir()
    cfg_kc.write_text(KC)
    (tmp_path / "config.yaml").write_text(f"clusters:\n- name: c0\n  kubeconfig: {cfg_kc}\n")
    m0 = cfg_kc.stat().st_mtime_ns
    assert migrate("kubeconfig", capsys)[0] == 0
    assert cfg_kc.read_text() == KC and cfg_kc.stat().st_mtime_ns == m0
    assert not [p for p in kv(env, "kubeconfig") if p.endswith("/c0")]


# ---------------------------------------------------------------------------
# replication.db : références et originaux
# ---------------------------------------------------------------------------
def test_replication_db_references_and_mask(env, capsys):
    conn = rdb.connect(env["state"] / "replication.db")
    masked_before = rn.mask(rdb.get_setting(conn, "notify"))
    conn.close()
    assert migrate("replication-notify", capsys)[0] == 0
    assert migrate("replication-hook", capsys)[0] == 0
    conn = rdb.connect(env["state"] / "replication.db")
    try:
        cfg = rdb.get_setting(conn, "notify")
        assert cfg["channels"][0]["token"] == "openbao:replication-notify/global#mx_1__token"
        assert rn.mask(cfg) == masked_before                       # l'écran ne change pas
        opts = json.loads(conn.execute("SELECT options FROM plans").fetchone()[0])
        assert opts["hooks"][0]["secret_headers"]["Authorization"].startswith("openbao:replication-hook/rp-1/pre#")
        rows = rdb.secret_rollback_rows(conn)
        assert {(r["cls"], r["path"]) for r in rows} == {("replication-notify", "global"),
                                                          ("replication-hook", "rp-1/pre")}
        assert all("value" not in r for r in rows)
        vals = [r[0] for r in conn.execute("SELECT value FROM secret_rollback")]
        assert sorted(vals) == sorted([MX, HDR, HOOK])             # originaux gardés jusqu'à la confirmation
    finally:
        conn.close()
    st = store(env)
    assert st.get(cfg["channels"][0]["token"]) == MX


# ---------------------------------------------------------------------------
# Confirmation et délai de garde
# ---------------------------------------------------------------------------
def age(env, cls, days):
    """Vieillit les originaux d'une classe."""
    t = time.time() - days * 86400
    p = env["state"] / ss.ORIGINALS_FILE
    if cls.startswith("replication-"):
        conn = rdb.connect(env["state"] / "replication.db")
        try:
            conn.execute("UPDATE secret_rollback SET at = ? WHERE cls = ?", (t, cls))
        finally:
            conn.close()
        return
    doc = json.loads(p.read_text())
    for e in doc.values():
        if e["cls"] == cls:
            e["at"] = t
    p.write_text(json.dumps(doc))


@pytest.mark.parametrize("cls", ["rancher-oidc", "replication-notify", "ssh-kvm"])
def test_confirm_erases_originals(env, capsys, cls):
    assert migrate(cls, capsys)[0] == 0
    rc, doc, _ = cli(["confirm", "--class", cls], capsys)
    assert rc == 0 and doc["originals_erased"] == len(CLASSES[cls])
    assert sc.adapter(cls).originals(fs(env)) == [] and not list(env["state"].rglob("*" + ss.MIGRATED_SUFFIX))
    assert json.loads((env["state"] / ss.SETTINGS_FILE).read_text())["migrated"][cls]["confirmed"]
    st = store(env)
    for path, field, value in CLASSES[cls]:
        assert st.get(ss.make_ref(cls, path, field)) == value      # toujours lisible


@pytest.mark.parametrize("cls", ["rancher-oidc", "replication-hook"])
def test_keep_period(env, capsys, cls):
    assert migrate(cls, capsys)[0] == 0
    rc, doc, _ = cli(["confirm", "--class", cls, "--older-than-days", "7"], capsys)
    assert rc == 0 and doc["originals_erased"] == 0 and len(sc.adapter(cls).originals(fs(env))) == 1
    assert "confirmed" not in json.loads((env["state"] / ss.SETTINGS_FILE).read_text())["migrated"][cls]
    age(env, cls, 8)
    rc, doc, _ = cli(["confirm", "--class", cls, "--older-than-days", "7"], capsys)
    assert rc == 0 and doc["originals_erased"] == 1 and sc.adapter(cls).originals(fs(env)) == []


def test_confirm_keeps_an_original_when_openbao_lost_the_secret(env, capsys):
    assert migrate("rancher-oidc", capsys)[0] == 0
    env["srv"].kv.pop(("harvester-ops", f"{PREFIX}/rancher-oidc/r1"))
    rc, doc, _ = cli(["confirm", "--class", "rancher-oidc"], capsys)
    assert rc == 1 and doc["errors"][0]["code"] == "openbao-missing"
    assert (env["state"] / "rancher.d" / "r1.oidc-secret.migrated").read_text() == OIDC


def test_confirm_erases_a_leftover_original_of_an_interrupted_migration(env, capsys):
    """Coupure entre la référence posée et le retrait du fichier : le reste
    est effacé par la confirmation, jamais quand la déclaration n'a pas la
    référence."""
    assert migrate("ssh-kvm", capsys)[0] == 0
    leftover = env["state"] / "ssh" / "k1_id"
    leftover.write_text(KEY)
    assert cli(["confirm", "--class", "ssh-kvm"], capsys)[0] == 0
    assert not leftover.exists()


def test_restore_when_openbao_lost_the_secret_keeps_the_reference(env, capsys):
    assert migrate("rancher-oidc", capsys)[0] == 0
    env["srv"].kv.pop(("harvester-ops", f"{PREFIX}/rancher-oidc/r1"))
    rc, doc, _ = migrate("rancher-oidc", capsys, "local")
    assert rc == 1 and doc["classes"]["rancher-oidc"]["errors"][0]["code"] == "openbao-missing"
    assert ss.is_openbao_ref(fs(env).read_local("rancher-oidc", "r1", "client_secret"))
    assert (env["state"] / "rancher.d" / "r1.oidc-secret.migrated").read_text() == OIDC


def test_restore_after_confirm_writes_the_value(env, capsys):
    assert migrate("rancher-oidc", capsys)[0] == 0
    assert cli(["confirm", "--class", "rancher-oidc"], capsys)[0] == 0
    assert migrate("rancher-oidc", capsys, "local")[0] == 0
    p = env["state"] / "rancher.d" / "r1.oidc-secret"
    assert p.read_text() == OIDC + "\n" and stat.S_IMODE(p.stat().st_mode) == 0o600


def test_local_value_changed_during_migration_is_not_replaced(env, capsys, monkeypatch):
    """La valeur locale change entre la lecture et la pose de la référence :
    refus, la nouvelle valeur reste, rien n'est perdu."""
    real = ss.FileStore.write_ref

    def racing(self, cls, path, field, ref, value=None, action=None):
        kvm_decl.write_private(self.state_dir / "rancher.d" / "r1.oidc-secret", "newer")
        return real(self, cls, path, field, ref, value, action)
    monkeypatch.setattr(ss.FileStore, "write_ref", racing)
    rc, doc, _ = migrate("rancher-oidc", capsys)
    assert rc == 1 and doc["classes"]["rancher-oidc"]["errors"][0]["code"] == "local-reference-failed"
    assert (env["state"] / "rancher.d" / "r1.oidc-secret").read_text() == "newer"


# ---------------------------------------------------------------------------
# Panne simulée (point 255)
# ---------------------------------------------------------------------------
def test_outage_test(env, capsys):
    assert cli(["outage-test"], capsys)[0] == 2                        # aucune classe migrée
    assert migrate("rancher-oidc", capsys)[0] == 0
    rc, doc, _ = cli(["outage-test"], capsys)
    assert rc == 0, doc
    assert {c["id"]: c["ok"] for c in doc["checks"]} == {"cache": True, "cold-refused": True}
    assert json.loads((env["state"] / ss.SETTINGS_FILE).read_text())["outage_test"]["ok_at"]
    assert OIDC not in json.dumps(doc)


def test_no_value_in_outputs(env, capsys):
    gate(env)
    texts = []
    for cls in CLASSES:
        for to in ("openbao", "local"):
            rc, doc, err = migrate(cls, capsys, to)
            texts += [json.dumps(doc), err]
    blob = "\n".join(texts)
    for vals in CLASSES.values():
        for _p, _f, v in vals:
            assert v.strip() not in blob
    assert hashlib.sha256(OIDC.encode()).hexdigest() not in blob


# ---------------------------------------------------------------------------
# Secret nouveau d'une classe migrée : écrit dans OpenBao directement
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def wapp():
    sys.path.insert(0, str(ROOT / "web"))
    import app
    return app


@pytest.fixture
def appenv(env, wapp, monkeypatch, tmp_path):
    """Toutes les classes migrées, magasin du processus sur le faux serveur."""
    doc = json.loads((env["state"] / ss.SETTINGS_FILE).read_text())
    doc["migrated"] = {c: {"at": 1} for c in CLASSES}
    (env["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    st = ss.MultiStore(env["state"], client_factory=lambda actor=None, action=None: _client(env))
    monkeypatch.setattr(ss, "_STORE", st)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("clusters: []\n")
    monkeypatch.setattr(wapp, "CONFIG_PATH", cfg)
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "no-htpasswd")
    monkeypatch.setattr(wapp, "NOTES_DB", None, raising=False)
    return dict(env, store=st, wapp=wapp)


def bao_value(env, cls, path, field):
    e = env["srv"].kv[("harvester-ops", f"{PREFIX}/{cls}/{path}")]
    return e["versions"][e["current"]][field]


def test_new_cluster_upload_goes_to_openbao(appenv):
    w, st = appenv["wapp"], appenv["state"]
    payload = {"name": "h9", "nodes": [{"hostname": "n1", "ip": "10.0.0.9", "role": "control-plane"}],
               "ssh": {"user": "rancher"}}
    with w.app.test_client() as c:
        r = c.post("/api/clusters", data={"payload": json.dumps(payload),
                                          "kubeconfig": (io.BytesIO(KC.encode()), "kc.yaml"),
                                          "ssh_key": (io.BytesIO(KEY.encode()), "id")},
                   content_type="multipart/form-data")
        assert r.status_code == 201, r.get_json()
        body = json.dumps(r.get_json())
    decl = yaml.safe_load((st / "clusters.d" / "h9.yaml").read_text())
    assert decl["kubeconfig"] == "openbao:kubeconfig/h9#kubeconfig"
    assert decl["ssh"]["key"] == "openbao:ssh-cluster/h9#private_key"
    assert not (st / "kubeconfigs" / "h9.yaml").exists() and not (st / "ssh" / "h9_id").exists()
    assert bao_value(appenv, "kubeconfig", "h9", "kubeconfig") == KC
    assert bao_value(appenv, "ssh-cluster", "h9", "private_key") == KEY
    assert "kc-secret" not in body and "key-secret" not in body


def test_kubeconfig_upload_console_vs_config(appenv, tmp_path):
    w, st = appenv["wapp"], appenv["state"]
    op_kc = tmp_path / "op.yaml"
    op_kc.write_text(KC)
    w.CONFIG_PATH.write_text(f"clusters:\n- name: c0\n  kubeconfig: {op_kc}\n  nodes: []\n")
    with w.app.test_client() as c:
        r = c.post("/api/clusters/h1/kubeconfig", data={"file": (io.BytesIO(b"kind: Config\n# new\n"), "k")},
                   content_type="multipart/form-data")
        assert r.status_code == 200 and r.get_json()["kubeconfig"] == "openbao:kubeconfig/h1#kubeconfig"
        r = c.post("/api/clusters/c0/kubeconfig", data={"file": (io.BytesIO(b"kind: Config\n# c0\n"), "k")},
                   content_type="multipart/form-data")
    assert bao_value(appenv, "kubeconfig", "h1", "kubeconfig") == "kind: Config\n# new\n"
    # cluster de config.yaml : jamais dans OpenBao (le fichier de l'installateur reste local)
    assert not [k for k in appenv["srv"].kv if k[1].endswith("/kubeconfig/c0")]
    assert r.status_code == 200 and not r.get_json()["kubeconfig"].startswith("openbao:")


def test_cluster_delete_forgets_its_secrets(appenv):
    w = appenv["wapp"]
    st = appenv["state"]
    ref = appenv["store"].put("kubeconfig", "h2", "kubeconfig", KC)
    doc = yaml.safe_load((st / "clusters.d" / "h2.yaml").read_text())
    doc["kubeconfig"] = ref
    (st / "clusters.d" / "h2.yaml").write_text(yaml.safe_dump(doc))
    with w.app.test_client() as c:
        assert c.delete("/api/clusters/h2").status_code == 200
    e = appenv["srv"].kv.get(("harvester-ops", f"{PREFIX}/kubeconfig/h2"))
    assert e is None or not e["versions"].get(e["current"])


def test_bare_metal_keypair_and_declaration(appenv, monkeypatch):
    w, st = appenv["wapp"], appenv["state"]
    run = appenv["run"]
    monkeypatch.setattr(ss, "secret_files_dir", lambda settings=None, mountinfo=None, env=None: run)
    f = w._bm_cluster_files("bm1")
    assert f["key"].parent == run and f["kubeconfig"].parent == run       # jamais sur le disque persistant
    f = w._bm_cluster_keypair("bm1")
    assert f["pub"].parent == st / "ssh" and f["pub"].is_file()
    assert not (st / "ssh" / "bm1_id").exists()
    key = f["key"].read_text()
    assert bao_value(appenv, "ssh-cluster", "bm1", "private_key") == key    # rangée dès sa création
    f["kubeconfig"].write_text(KC)
    w._bm_declare_cluster("bm1", f["kubeconfig"], f["key"], "n1", "10.0.0.5")
    decl = yaml.safe_load((st / "clusters.d" / "bm1.yaml").read_text())
    assert decl["kubeconfig"] == "openbao:kubeconfig/bm1#kubeconfig"
    assert decl["ssh"] == dict(decl["ssh"], key="openbao:ssh-cluster/bm1#private_key", generated=True)
    assert not f["key"].exists() and not f["kubeconfig"].exists()           # copies tmpfs effacées
    assert bao_value(appenv, "kubeconfig", "bm1", "kubeconfig") == KC


def test_rancher_oidc_registration(appenv):
    import rancher_servers as rs
    st = appenv["state"]
    store = rs.Store(lambda: str(st))
    sid = store.create({}, {"label": "r9", "url": "https://r9.example"})["id"]
    assert sid == "r9"
    store.set_sso({}, "r9", "client-1", "oidc-new-secret", "https://cb", "c1")
    assert (st / "rancher.d" / "r9.oidc-secret").read_text() == "openbao:rancher-oidc/r9#client_secret\n"
    assert bao_value(appenv, "rancher-oidc", "r9", "client_secret") == "oidc-new-secret"
    assert rs.read_secret(str(st / "rancher.d" / "r9.oidc-secret")) == "oidc-new-secret"
    store.clear_sso({}, "r9")
    e = appenv["srv"].kv.get(("harvester-ops", f"{PREFIX}/rancher-oidc/r9"))
    assert e is None or not e["versions"].get(e["current"])


def test_tf_kvm_token_goes_to_openbao(appenv):
    w = appenv["wapp"]
    ws = appenv["state"] / "terraform" / ".kvm" / "k2" / "decls" / "d2"
    ws.mkdir(parents=True)
    v = w._tf_kvm_secret(ws, "token")
    assert (ws / ".secrets" / "token").read_text() == "openbao:tf-kvm/k2/d2/token#value\n"
    assert bao_value(appenv, "tf-kvm", "k2/d2/token", "value") == v and len(v) == 64
    assert w._tf_kvm_secret(ws, "token") == v                                  # relu, jamais tiré deux fois


def test_kvm_host_add_hands_the_key(appenv, monkeypatch):
    w = appenv["wapp"]
    seen = {}

    def fake_action(host, label, cmd, cleanup=None, cluster=None, waits=False, secret_files=None):
        seen.update(cmd=cmd, files=secret_files, cleanup=cleanup)
        return types_ns(id="a1"), None
    monkeypatch.setattr(w, "_kvm_action", fake_action)
    monkeypatch.setattr(w, "_kvm_unavailable", lambda: None)
    monkeypatch.setattr(w._kvm_cli, "read_private_key", lambda p: None)
    payload = {"name": "k9", "address": "10.0.0.9", "port": 22, "user": "root",
               "fingerprint": "SHA256:" + "A" * 43, "labels": {}}
    with w.app.test_client() as c:
        r = c.post("/api/kvm/hosts", data={"payload": json.dumps(payload), "key": (io.BytesIO(KEY.encode()), "k")},
                   content_type="multipart/form-data")
    assert r.status_code == 202, r.get_json()
    ref = "openbao:ssh-kvm/k9#private_key"
    assert "--key-in-store" in seen["cmd"] and list(seen["files"]) == [ref]
    assert bao_value(appenv, "ssh-kvm", "k9", "private_key") == KEY
    seen["cleanup"]()                          # ajout refusé (aucune déclaration) : la clé quitte OpenBao
    e = appenv["srv"].kv.get(("harvester-ops", f"{PREFIX}/ssh-kvm/k9"))
    assert e is None or not e["versions"].get(e["current"])


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def test_notify_set_hands_references(appenv, monkeypatch):
    w = appenv["wapp"]
    conn = rdb.connect(appenv["state"] / "replication.db")
    try:
        rdb.secret_to_ref(conn, "replication-notify", "global", "mx_1__token",
                          appenv["store"].put("replication-notify", "global", "mx_1__token", MX), MX)
    finally:
        conn.close()
    monkeypatch.setattr(w, "_replication_db_path", lambda: appenv["state"] / "replication.db")
    stored = w._repl_notify_stored()
    body = {"enabled": True, "min_level": "warn", "codes": None, "channels": [
        {"id": "mx-1", "name": "", "kind": "matrix", "homeserver": "https://mx.example", "room": "!r:mx",
         "token": rn.MASK},
        {"id": "wh", "name": "", "kind": "webhook", "url": "https://hook.example/x", "headers": {},
         "secret_headers": {"X-Api-Key": "brand-new-290"}},
        {"id": "sl", "name": "", "kind": "slack", "url": "https://hooks.slack.com/new-290"}]}
    checked = w._repl_notify_checked(body, stored)
    spec = w._repl_notify_to_refs(body, checked, stored)
    ch = {c["id"]: c for c in spec["channels"]}
    assert ch["mx-1"]["token"] == "openbao:replication-notify/global#mx_1__token"   # gardée, pas réécrite
    assert ch["wh"]["secret_headers"]["X-Api-Key"].startswith("openbao:replication-notify/global#wh__hdr_")
    assert ch["sl"]["url"] == "openbao:replication-notify/global#sl__url"
    assert "brand-new-290" not in json.dumps(spec) and "new-290" not in json.dumps(spec)
    e = appenv["srv"].kv[("harvester-ops", f"{PREFIX}/replication-notify/global")]
    assert e["current"] == 3                   # mise en place + deux secrets nouveaux, jeton jamais réécrit
    assert e["versions"][3]["mx_1__token"] == MX
    # l'outil accepte le réglage à références (adresse Slack gardée par sa référence)
    again = rn.normalize(spec, stored)
    rn.address_refusal(again, {"allow_private": False})
    assert again["channels"][2]["url"] == ch["sl"]["url"]


def test_hook_headers_to_references(appenv):
    w = appenv["wapp"]
    hooks = [{"name": "pre", "url": "https://h", "secret_headers": {"Authorization": "Bearer x-290",
                                                                     "X-Keep": w._rp.MASK}}]
    out = w._repl_hooks_to_refs(hooks, "rp-1")
    assert out[0]["secret_headers"]["X-Keep"] == w._rp.MASK
    ref = out[0]["secret_headers"]["Authorization"]
    assert ref == f"openbao:replication-hook/rp-1/pre#{sc.header_field('Authorization')}"
    assert bao_value(appenv, "replication-hook", "rp-1/pre", sc.header_field("Authorization")) == "Bearer x-290"
    assert hooks[0]["secret_headers"]["Authorization"] == "Bearer x-290"     # la demande d'origine intacte


def test_local_class_writers_unchanged(appenv):
    """Classe non migrée : les écrivains écrivent en local comme avant."""
    doc = json.loads((appenv["state"] / ss.SETTINGS_FILE).read_text())
    doc["migrated"] = {}
    (appenv["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    appenv["store"]._sig = None
    w = appenv["wapp"]
    assert w._secret_put("kubeconfig", "h1", "kubeconfig", KC) is None
    assert w._repl_hooks_to_refs([{"name": "a", "secret_headers": {"X": "v"}}], "rp-1")[0]["secret_headers"] == {"X": "v"}


# ---------------------------------------------------------------------------
# Lancements : copies de travail remises aux outils (revue I6, tâche 8)
# ---------------------------------------------------------------------------
def test_spawn_env_hands_kvm_key_refs_and_files(appenv, tmp_path):
    w, st = appenv["wapp"], appenv["state"]
    ref = appenv["store"].put("ssh-kvm", "k1", "private_key", KEY)
    doc = kvm_decl.load(st, "k1")
    doc["ssh_key"] = ref
    kvm_decl.write(st, doc)
    other = appenv["store"].put("replication-hook", "rp-1/pre", sc.header_field("A"), "hv")
    given = tmp_path / "given"
    given.write_text("x")
    env, mp = w._spawn_secret_env(["kvm:k1", ""], refs=[other], files={"openbao:ssh-kvm/k8#private_key": str(given)})
    try:
        m = json.loads(Path(env[ss.FILES_ENV]).read_text())
        assert set(m) == {ref, other, "openbao:ssh-kvm/k8#private_key"}
        assert Path(m[ref]).read_text() == KEY and Path(m[other]).read_text() == "hv"
    finally:
        mp.unlink()


def test_cli_action_records_secret_clusters(appenv, monkeypatch):
    w = appenv["wapp"]
    seen = {}
    monkeypatch.setattr(w, "_vm_transfer_runner", lambda run, cmd, **k: seen.update(run=run))
    with w.app.test_request_context():
        run, err = w._cli_action("h1", "t:1", ["python3", "x"], "x", check_busy=False,
                                 busy_clusters=("h1", "h2"), secret_clusters=("kvm:k1",), secret_refs=("r",))
    for _ in range(50):
        if seen:
            break
        time.sleep(0.02)
    assert run.secret_clusters == ("h1", "h2", "kvm:k1") and run.secret_refs == ("r",)


def test_follower_refs_include_plan_cluster_kubeconfigs(appenv, monkeypatch):
    w = appenv["wapp"]
    ref = appenv["store"].put("kubeconfig", "h2", "kubeconfig", KC)
    monkeypatch.setattr(w, "_replication_db_path", lambda: appenv["state"] / "replication.db")
    monkeypatch.setattr(w, "load_config_ro", lambda: {"clusters": [
        {"name": "h1", "kubeconfig": "/local/h1.yaml"}, {"name": "h2", "kubeconfig": ref},
        {"name": "h3", "kubeconfig": "openbao:kubeconfig/h3#kubeconfig"}]})
    refs = w._replication_secret_refs(clusters=True)
    assert ref in refs and "openbao:kubeconfig/h3#kubeconfig" not in refs     # h3 : dans aucun plan
    assert ref not in w._replication_secret_refs()


# ---------------------------------------------------------------------------
# Délai de garde : tâche de fond de la console, action suivie
# ---------------------------------------------------------------------------
def test_originals_sweep_launches_a_tracked_action(appenv, capsys, monkeypatch):
    w = appenv["wapp"]
    doc = json.loads((appenv["state"] / ss.SETTINGS_FILE).read_text())
    doc["migrated"] = {}
    (appenv["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    assert migrate("rancher-oidc", capsys)[0] == 0
    appenv["store"]._sig = None
    calls = []

    def fake_cli(cluster, label, cmd, tool, **kw):
        calls.append((label, cmd[2:]))
        return types_ns(id="x", action=label, status="running"), None
    monkeypatch.setattr(w, "_cli_action", fake_cli)
    assert w._secret_originals_sweep() == [] and calls == []               # trop récent
    age(appenv, "rancher-oidc", 8)
    started = w._secret_originals_sweep()
    assert len(started) == 1 and calls == [("secrets:expire-originals:rancher-oidc",
                                            ["--actor", "service", "confirm", "--class", "rancher-oidc",
                                             "--older-than-days", "7"])]


def test_restore_local_failure_keeps_openbao(env, capsys, monkeypatch):
    """Écriture locale impossible pendant le retour : le secret reste dans
    OpenBao, la référence reste en place (jamais détruit avant la relecture)."""
    assert migrate("rancher-oidc", capsys)[0] == 0

    def boom(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(ss.FileStore, "restore_local", boom)
    rc, doc, _ = migrate("rancher-oidc", capsys, "local")
    assert rc == 1 and doc["classes"]["rancher-oidc"]["errors"][0]["code"] == "restore-local-failed"
    assert bao_value(env, "rancher-oidc", "r1", "client_secret") == OIDC
    assert ss.is_openbao_ref(fs(env).read_local("rancher-oidc", "r1", "client_secret"))


def test_outage_test_fails_when_the_cut_is_not_an_outage(env, capsys, monkeypatch):
    """Une lecture à froid refusée pour une autre raison (403) n'est pas la
    panne attendue : le test de panne échoue et la barrière reste fermée."""
    assert migrate("rancher-oidc", capsys)[0] == 0
    other = FakeB1(CID).start()
    other.denied = [""]
    monkeypatch.setattr(hsc, "OUTAGE_ADDR", other.addr)
    doc = json.loads((env["state"] / ss.SETTINGS_FILE).read_text())
    doc.pop("outage_test", None)
    (env["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    try:
        rc, out, _ = cli(["outage-test"], capsys)
    finally:
        other.stop()
    assert rc == 1 and {c["id"]: c["ok"] for c in out["checks"]}["cold-refused"] is False
    assert "outage_test" not in json.loads((env["state"] / ss.SETTINGS_FILE).read_text())
