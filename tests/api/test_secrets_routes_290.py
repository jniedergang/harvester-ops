"""2.9.0 tâches 11-12 : routes de l'onglet Secrets, état, tâches de fond.

Chaque geste est une action suivie `harvester-secrets ...` (argv vérifié en
tableau, un cas par refus) ; les valeurs tapées (jeton d'enveloppe, CA)
partent par un fichier 0600 sur tmpfs effacé à la fin, jamais en argument ;
les lectures ne rendent aucune valeur. Tâches de fond : instantané planifié
à l'heure réglée, réessai horaire après un échec, balayages quotidiens.

Mutations jouées (rapport des tâches 11-12) : « jeton d'enveloppe passé en
argument », « instantané relancé à chaque passage », « version rendue avec
la valeur », « route de lecture des noms ouverte aux lecteurs ».
"""

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import openbao_client as oc  # noqa: E402
import openbao_policy as pol  # noqa: E402
import secret_store as ss  # noqa: E402
from fake_openbao import FakeOpenBao  # noqa: E402

CID = "abc123def456"
PREFIX = f"consoles/{CID}"
WRAP = "s.WRAPPINGTOKEN0123456789abcdef"


@pytest.fixture(scope="module")
def wapp():
    import app
    return app


@pytest.fixture
def sx(tmp_path, monkeypatch, wapp):
    srv = FakeOpenBao().start()
    admin = srv.add_token(policies=[pol.console_role(CID)])
    state, etc, run = tmp_path / "state", tmp_path / "etc", tmp_path / "run"
    for d in (state, etc, run):
        d.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setenv("HARVESTER_OPS_OPENBAO_DIR", str(etc))
    doc = {"mode": "a", "addr": srv.addr, "console_id": CID, "secret_files_dir": str(run),
           "dev_disk_secret_files": True}
    (state / ss.SETTINGS_FILE).write_text(json.dumps(doc))

    def factory(actor=None, action=None):
        c = oc.Client(srv.addr, actor=actor or "service", action=action, retry_delays=(0.01,))
        c.set_token(admin)
        return c
    st = ss.MultiStore(state, client_factory=factory)
    st.SETTINGS_RECHECK_S = 0
    monkeypatch.setattr(ss, "_STORE", st)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path / "ws")
    monkeypatch.setenv("HARVESTER_OPS_REPLICATION_DB", str(tmp_path / "replication.db"))
    launched = []

    def fake_cli(cluster, label, cmd, tool, after=None, **kw):
        spec = cmd[cmd.index("--spec") + 1] if "--spec" in cmd else None
        launched.append({"label": label, "argv": cmd[cmd.index("--actor"):], "spec": spec,
                         "spec_doc": json.loads(Path(spec).read_text()) if spec else None,
                         "spec_mode": stat.S_IMODE(os.stat(spec).st_mode) if spec else None,
                         "kw": kw})
        if after:
            after()
        r = wapp.ActionRun(f"act{len(launched):04d}", label, "", [])
        return r, None
    monkeypatch.setattr(wapp, "_cli_action", fake_cli)
    x = type("X", (), {})()
    x.srv, x.state, x.etc, x.run, x.launched, x.doc, x.store = srv, state, etc, run, launched, doc, st

    def set_settings(**kw):
        d = dict(json.loads((state / ss.SETTINGS_FILE).read_text()), **kw)
        (state / ss.SETTINGS_FILE).write_text(json.dumps(d))
    x.set = set_settings
    yield x
    srv.stop()


def post(wapp, path, body=None, method="post"):
    with wapp.app.test_client() as c:
        r = getattr(c, method)(path, json=body or {})
        return r.status_code, r.get_json()


# ---------------------------------------------------------------------------
# Rôles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,method,role", [
    ("/api/secrets/banner", "GET", "viewer"),
    ("/api/secrets/status", "GET", "operator"),
    ("/api/secrets/classes", "GET", "admin"),
    ("/api/secrets/secrets", "GET", "admin"),
    ("/api/secrets/versions", "GET", "admin"),
    ("/api/secrets/rotation", "GET", "admin"),
    ("/api/secrets/snapshots", "GET", "admin"),
    ("/api/secrets/tf-keys", "GET", "admin"),
    ("/api/secrets/assistant", "GET", "admin"),
    ("/api/secrets/check", "POST", "admin"),
    ("/api/secrets/enable", "POST", "admin"),
    ("/api/secrets/migrate", "POST", "admin"),
    ("/api/secrets/settings", "PUT", "admin"),
    ("/api/secrets/restore", "POST", "admin"),
    ("/api/secrets/restore/preflight", "GET", "admin"),
    ("/api/secrets/cache/clear", "POST", "admin"),
])
def test_roles(wapp, path, method, role):
    assert wapp.required_role_for(path, method) == role


def test_every_secrets_route_is_rate_limited_and_authenticated(wapp):
    src = (ROOT / "web" / "app.py").read_text()
    import re
    for m in re.finditer(r'@app\.route\("(/api/secrets/[^"]+)", methods=\["(POST|PUT)"\]\)\n(@[^\n]+\n)+', src):
        block = m.group(0)
        assert "@requires_auth" in block and "@_rate_limit(" in block, m.group(1)


# ---------------------------------------------------------------------------
# Gestes : argv exact, un cas par refus
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,body,label,tail", [
    ("/api/secrets/migrate", {"class": "rancher-oidc", "to": "openbao"}, "secrets:migrate-openbao:rancher-oidc",
     ["migrate", "--to", "openbao", "--class", "rancher-oidc"]),
    ("/api/secrets/migrate", {"class": "kubeconfig", "to": "local", "name": "h1"}, "secrets:migrate-local:kubeconfig",
     ["migrate", "--to", "local", "--class", "kubeconfig", "--name", "h1"]),
    ("/api/secrets/confirm", {"class": "tf-kvm"}, "secrets:confirm:tf-kvm", ["confirm", "--class", "tf-kvm"]),
    ("/api/secrets/outage-test", {}, "secrets:outage-test", ["outage-test"]),
    ("/api/secrets/rollback", {"ref": "openbao:rancher-oidc/r1#client_secret", "version": 2}, "secrets:rollback",
     ["rollback", "openbao:rancher-oidc/r1#client_secret", "--version", "2"]),
    ("/api/secrets/rotate", {"what": "secret-id"}, "secrets:rotate-secret-id", ["rotate", "secret-id"]),
    ("/api/secrets/rotate", {"what": "transit", "target": "d1d1d1d1d1d1"}, "secrets:rotate-transit:d1d1d1d1d1d1",
     ["rotate", "transit", "d1d1d1d1d1d1"]),
    ("/api/secrets/rotate", {"what": "ssh-kvm", "target": "node2"}, "secrets:rotate-ssh-kvm:node2",
     ["rotate", "ssh-kvm", "node2"]),
    ("/api/secrets/rotate", {"what": "unseal-key", "confirm": CID}, "secrets:rotate-unseal-key",
     ["rotate", "unseal-key"]),
    ("/api/secrets/snapshot", {}, "secrets:snapshot", ["snapshot"]),
    ("/api/secrets/restore", {"file": "openbao-20261108T023000Z-hops-20261108-1.snap", "confirm": CID},
     "secrets:restore:openbao-20261108T023000Z-hops-20261108-1.snap",
     ["restore", "openbao-20261108T023000Z-hops-20261108-1.snap"]),
    ("/api/secrets/disable", {"confirm": CID, "remove_b1": True}, "secrets:disable", ["disable", "--remove-b1"]),
    ("/api/secrets/tf-keys/delete", {"decl": "d1d1d1d1d1d1", "confirm": "d1d1d1d1d1d1"},
     "secrets:tf-key-delete:d1d1d1d1d1d1", ["tf-keys", "delete", "d1d1d1d1d1d1", "--confirm", "d1d1d1d1d1d1"]),
    ("/api/secrets/tf-entity-done", {"decl": "d1d1d1d1d1d1"}, "secrets:tf-entity-done:d1d1d1d1d1d1",
     ["tf-entity-remove", "d1d1d1d1d1d1", "--done"]),
])
def test_gestures_launch_the_tool(wapp, sx, path, body, label, tail):
    code, doc = post(wapp, path, body)
    assert code == 202 and doc["action_id"], doc
    got = sx.launched[-1]
    assert got["label"] == label and got["argv"][2:] == tail
    if label == "secrets:rotate-ssh-kvm:node2":
        assert got["kw"]["secret_clusters"] == ("kvm:node2",)          # copie de la clé remise


@pytest.mark.parametrize("path,body,code", [
    ("/api/secrets/migrate", {"class": "accounts", "to": "openbao"}, "bad-input"),
    ("/api/secrets/migrate", {"class": "kubeconfig", "to": "nowhere"}, "bad-input"),
    ("/api/secrets/migrate", {"class": "kubeconfig", "to": "local", "name": "../x"}, "bad-input"),
    ("/api/secrets/confirm", {"class": "plan-run"}, "bad-input"),
    ("/api/secrets/rollback", {"ref": "local:kubeconfig/h1#kubeconfig", "version": 1}, "bad-input"),
    ("/api/secrets/rollback", {"ref": "openbao:kubeconfig/../x#kubeconfig", "version": 1}, "bad-input"),
    ("/api/secrets/rollback", {"ref": "openbao:kubeconfig/h1#kubeconfig", "version": 0}, "bad-input"),
    ("/api/secrets/rotate", {"what": "everything"}, "bad-input"),
    ("/api/secrets/rotate", {"what": "ssh-kvm", "target": "a b"}, "bad-input"),
    ("/api/secrets/rotate", {"what": "unseal-key"}, "confirmation-required"),
    ("/api/secrets/restore", {"file": "../etc/shadow", "confirm": CID}, "snapshot-bad-name"),
    ("/api/secrets/restore", {"file": "openbao-x.snap", "confirm": "nope"}, "confirmation-required"),
    ("/api/secrets/disable", {}, "confirmation-required"),
    ("/api/secrets/tf-keys/delete", {"decl": "d1d1d1d1d1d1", "confirm": "other"}, "confirmation-required"),
    ("/api/secrets/check", {"mode": "c"}, "bad-input"),
    ("/api/secrets/check", {"mode": "a", "addr": "file:///etc/x"}, "bad-input"),
    ("/api/secrets/check", {"mode": "a", "addr": "https://bao:8200", "mount": "../x"}, "bad-input"),
    ("/api/secrets/enable", {"mode": "a", "addr": "https://bao:8200", "role_id": "rid-12345678"}, "bad-input"),
    ("/api/secrets/enable", {"mode": "b1", "bundle": "../../x.tar.gz"}, "bad-input"),
    ("/api/secrets/enable", {"mode": "b1", "bundle": "harvester-ops-openbao-2.7.2-2.7.2-5.4.tar.gz"}, "bad-input"),
])
def test_gesture_refusals(wapp, sx, path, body, code):
    status, doc = post(wapp, path, body)
    assert status == 400 and doc["code"] == code, doc
    assert not sx.launched


def test_settings_route(wapp, sx):
    status, doc = post(wapp, "/api/secrets/settings", {"snapshot_time": "03:15", "snapshot_dir": None,
                                                       "ignored": 1}, method="put")
    assert status == 202
    assert sx.launched[-1]["argv"][2:] == ["settings", "--set", "snapshot_dir=", "--set", "snapshot_time=03:15"]
    status, doc = post(wapp, "/api/secrets/settings", {}, method="put")
    assert status == 400


# ---------------------------------------------------------------------------
# Valeurs tapées : fichier 0600 sur tmpfs, jamais en argument, effacé
# ---------------------------------------------------------------------------
def test_wrapping_token_and_ca_go_by_a_private_file(wapp, sx):
    status, doc = post(wapp, "/api/secrets/enable", {"mode": "a", "addr": "https://bao.example:8200",
                                                     "role_id": "rid-12345678", "wrap_token": WRAP,
                                                     "ca_pem": "-----BEGIN CERTIFICATE-----\nX\n"})
    assert status == 202, doc
    got = sx.launched[-1]
    assert WRAP not in json.dumps(got["argv"])
    assert got["spec_doc"] == {"wrap_token": WRAP, "ca_pem": "-----BEGIN CERTIFICATE-----\nX\n"}
    assert got["spec_mode"] == 0o600 and str(got["spec"]).startswith(str(sx.run))
    assert not Path(got["spec"]).exists()                    # effacé par `after`
    assert got["argv"][2:7] == ["enable", "--mode", "a", "--role-id", "rid-12345678"]
    status, doc = post(wapp, "/api/secrets/check", {"mode": "a", "addr": "https://bao.example:8200",
                                                    "wrap_token": WRAP, "transit": True})
    assert status == 202 and WRAP not in json.dumps(sx.launched[-1]["argv"])
    assert "--transit" in sx.launched[-1]["argv"]


def test_enable_b1_takes_a_deposited_bundle(wapp, sx):
    staged = sx.state / "secrets-agent" / "staged"
    staged.mkdir(parents=True)
    name = "harvester-ops-openbao-2.7.2-2.7.2-5.4.tar.gz"
    (staged / name).write_bytes(b"x")
    status, doc = post(wapp, "/api/secrets/bundles", method="get")
    assert doc["bundles"] == [name]
    status, doc = post(wapp, "/api/secrets/enable", {"mode": "b1", "bundle": name})
    assert status == 202
    assert sx.launched[-1]["argv"][2:6] == ["enable", "--mode", "b1", "--bundle"]


def test_cache_clear_is_a_tracked_inline_action(wapp, sx):
    status, doc = post(wapp, "/api/secrets/cache/clear")
    assert status == 202
    with wapp.ACTIONS_LOCK:
        run = wapp.ACTIONS.pop(doc["action_id"])
    assert run.action == "secrets:cache-clear" and run.status == "done"


# ---------------------------------------------------------------------------
# Lectures : aucune valeur
# ---------------------------------------------------------------------------
def test_versions_carry_authors_never_values(wapp, sx):
    sx.set(migrated={"rancher-oidc": {"at": 1}})
    st = sx.store
    st.put("rancher-oidc", "r1", "client_secret", "VALUE-1-secret", actor="alice", action="a1")
    st.put("rancher-oidc", "r1", "client_secret", "VALUE-2-secret", actor="bob", action="a2")
    status, doc = post(wapp, "/api/secrets/versions?ref=openbao:rancher-oidc/r1%23client_secret", method="get")
    assert status == 200, doc
    assert [(v["version"], v["actor"]) for v in doc["versions"]][-2:] == [(1, "alice"), (2, "bob")]
    assert "VALUE" not in json.dumps(doc)
    status, doc = post(wapp, "/api/secrets/versions?ref=openbao:rancher-oidc/../x%23client_secret", method="get")
    assert status == 400


def test_status_and_banner_without_values(wapp, sx):
    sx.set(migrated={"rancher-oidc": {"at": 1}})
    sx.store.put("rancher-oidc", "r1", "client_secret", "VALUE-secret", actor="alice")
    status, doc = post(wapp, "/api/secrets/status", method="get")
    assert status == 200 and doc["mode"] == "a" and doc["active"] is True
    assert doc["store"]["state"] == "ok" and "VALUE" not in json.dumps(doc)
    status, b = post(wapp, "/api/secrets/banner", method="get")
    assert set(b) == {"active", "mode", "state", "down_since", "cache_until", "error"}


def test_banner_tells_until_when_the_cache_holds(wapp, sx):
    sx.set(migrated={"rancher-oidc": {"at": 1}})
    st = sx.store
    st.put("rancher-oidc", "r1", "client_secret", "VALUE-secret")
    st.get("openbao:rancher-oidc/r1#client_secret")
    sx.srv.sealed = True
    st.openbao.probe()
    status, b = post(wapp, "/api/secrets/banner", method="get")
    assert b["state"] == "degraded" and b["error"] == "openbao-sealed"
    assert b["cache_until"] and b["cache_until"] > time.time() + 60


@pytest.mark.parametrize("prep,code", [
    ({"mode": "b1", "snapshot_dir": "/tmp", "snapshots": {"last_ok_at": 1}}, "snapshot-stale"),
    ({"mode": "b1"}, "snapshot-dir-unset"),
    ({"mode": "b1", "snapshot_dir": "/tmp", "snapshots": {"last_ok_at": 10, "last_error": "openbao-sealed",
                                                         "last_error_at": 20}}, "snapshot-failed"),
    ({"mode": "b1", "offline_copy": {"pending": True}}, "offline-copy-pending"),
    ({"tf_entities_pending": ["d1d1d1d1d1d1"]}, "tf-entities-pending"),
])
def test_warnings(wapp, sx, prep, code):
    sx.set(**prep)
    status, doc = post(wapp, "/api/secrets/status", method="get")
    assert code in [w["code"] for w in doc["warnings"]]


def test_audit_space_and_secret_id_age_warnings(wapp, sx):
    sx.set(mode="b1", snapshot_dir="/tmp", snapshots={"last_ok_at": time.time()})
    d = sx.state / "secrets-agent"
    d.mkdir()
    (d / "health.json").write_text(json.dumps({"audit": {"free": 500 << 20, "total": 100 << 30}}))
    (sx.etc / "secret-id").write_text("x")
    old = time.time() - 40 * 86400
    os.utime(sx.etc / "secret-id", (old, old))
    status, doc = post(wapp, "/api/secrets/status", method="get")
    codes = [w["code"] for w in doc["warnings"]]
    assert "audit-space-low" in codes and "secret-id-overdue" in codes and "snapshot-stale" not in codes


def test_classes_table_and_kubeconfig_gate(wapp, sx):
    status, doc = post(wapp, "/api/secrets/classes", method="get")
    rows = {r["class"]: r for r in doc["classes"]}
    assert doc["order"][-1] == "kubeconfig" and rows["kubeconfig"]["gate"] == "migrate-another-first"
    sx.set(migrated={"rancher-oidc": {"at": 1, "by": "alice", "complete": True}})
    status, doc = post(wapp, "/api/secrets/classes", method="get")
    rows = {r["class"]: r for r in doc["classes"]}
    assert rows["kubeconfig"]["gate"] == "outage-test-first"
    assert rows["rancher-oidc"]["migrated"]["by"] == "alice"
    sx.set(outage_test={"ok_at": 5, "addr": sx.srv.addr, "mode": "a"})
    status, doc = post(wapp, "/api/secrets/classes", method="get")
    assert {r["class"]: r for r in doc["classes"]}["kubeconfig"]["gate"] is None


def test_snapshots_listing(wapp, sx, tmp_path):
    d = tmp_path / "snaps"
    d.mkdir()
    (d / "openbao-20261101T023000Z-hops-20261101-1.snap").write_bytes(b"xx")
    (d / "readme.txt").write_text("x")
    os.symlink("/etc/hostname", d / "openbao-link.snap")
    sx.set(mode="b1", snapshot_dir=str(d))
    status, doc = post(wapp, "/api/secrets/snapshots", method="get")
    assert [f["file"] for f in doc["files"]] == ["openbao-20261101T023000Z-hops-20261101-1.snap"]
    assert doc["keep"] == 14 and doc["time"] == "02:30"


def test_tf_keys_listing_with_deletion_dates(wapp, sx):
    sx.set(tf_keys_retired={"d1d1d1d1d1d1": {"at": 1000, "trial": False}, "e2e2e2e2e2e2": {"at": 50, "trial": True}})
    status, doc = post(wapp, "/api/secrets/tf-keys", method="get")
    rows = {r["decl"]: r for r in doc["keys"]}
    assert rows["d1d1d1d1d1d1"]["delete_after"] == 1000 + 30 * 86400 and rows["d1d1d1d1d1d1"]["due"] is True
    assert rows["e2e2e2e2e2e2"]["delete_after"] == 50
    assert rows["d1d1d1d1d1d1"]["key"] == f"hops-{CID}-tf-d1d1d1d1d1d1"


def test_rotation_view_flags_old_kvm_keys(wapp, sx):
    sd = sx.state
    (sd / "kvm.d").mkdir()
    (sd / "ssh").mkdir()
    for name, age in (("old", 400), ("new", 10)):
        (sd / "kvm.d" / f"{name}.yaml").write_text(json.dumps({
            "name": name, "address": "10.0.0.2", "user": "ju", "port": 22, "ssh_key": f"ssh/{name}_id",
            "known_hosts": f"ssh/{name}_known", "uri": "qemu:///system"}))
        f = sd / "ssh" / f"{name}_id"
        f.write_text("PRIVATE")
        t = time.time() - age * 86400
        os.utime(f, (t, t))
    status, doc = post(wapp, "/api/secrets/rotation", method="get")
    kvm = {k["host"]: k for k in doc["kvm"]}
    assert kvm["old"]["old"] is True and kvm["new"]["old"] is False
    assert "PRIVATE" not in json.dumps(doc)


# ---------------------------------------------------------------------------
# Tâches de fond
# ---------------------------------------------------------------------------
@pytest.fixture
def bg(sx, wapp, monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(wapp, "_secrets_service_action", lambda label, args: started.append((label, args)) or
                        wapp.ActionRun(f"bg{len(started)}", label, "", []))
    monkeypatch.setattr(wapp, "_secrets_running", lambda label: False)
    wapp._SECRETS_DAILY["at"] = time.time()                 # balayages quotidiens déjà faits
    d = tmp_path / "snaps"
    d.mkdir()
    sx.set(mode="b1", snapshot_dir=str(d), snapshot_time="02:30")
    return started


def _at(hh, mm, day_offset=0):
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset, hh, mm, 0, 0, 0, -1))


def test_snapshot_taken_once_after_its_time(wapp, sx, bg):
    sx.set(snapshots={"last_ok_at": _at(2, 31, -1)})        # celui d'hier
    wapp._secrets_housekeeping_tick(now=_at(2, 20))
    assert bg == []                                         # pas encore l'heure
    wapp._secrets_housekeeping_tick(now=_at(2, 35))
    assert bg == [("secrets:snapshot", ["snapshot"])]
    sx.set(snapshots={"last_ok_at": _at(2, 36)})
    wapp._secrets_housekeeping_tick(now=_at(3, 0))
    assert len(bg) == 1                                     # une fois par jour


def test_failed_snapshot_retried_hourly(wapp, sx, bg):
    sx.set(snapshots={"last_ok_at": _at(2, 31, -1), "last_error": "openbao-sealed", "last_error_at": _at(2, 35)})
    wapp._secrets_housekeeping_tick(now=_at(3, 0))
    assert bg == []
    wapp._secrets_housekeeping_tick(now=_at(3, 40))
    assert bg == [("secrets:snapshot", ["snapshot"])]


def test_no_snapshot_in_mode_a_or_without_a_directory(wapp, sx, bg):
    sx.set(mode="a")
    wapp._secrets_housekeeping_tick(now=_at(3, 0))
    sx.set(mode="b1", snapshot_dir=None)
    wapp._secrets_housekeeping_tick(now=_at(3, 0))
    assert bg == []


def test_daily_sweeps(wapp, sx, bg):
    sx.set(mode="a", tf_keys_retired={"d1d1d1d1d1d1": {"at": 1, "trial": False}})
    wapp._SECRETS_DAILY["at"] = 0
    wapp._secrets_housekeeping_tick(now=time.time())
    labels = [b[0] for b in bg]
    assert labels == ["secrets:tf-keys-sweep", "secrets:sweep-orphans"]
    orphan = dict(bg)["secrets:sweep-orphans"]
    assert orphan[0] == "sweep-orphans" and any(a.startswith("--tf-dir=") for a in orphan)
    bg.clear()
    wapp._secrets_housekeeping_tick(now=time.time() + 3600)
    assert bg == []                                          # une fois par jour


def test_probe_sets_the_metric_and_the_banner_state(wapp, sx, monkeypatch):
    seen = []
    monkeypatch.setattr(wapp.metric_openbao_up, "set", lambda v: seen.append(v))
    assert wapp._secrets_probe_tick() is True and seen[-1] == 1
    sx.srv.sealed = True
    assert wapp._secrets_probe_tick() is False and seen[-1] == 0
    assert wapp._secrets_banner()["error"] == "openbao-sealed"


def test_status_tells_a_newer_vault_needs_the_acknowledgement(wapp, sx, monkeypatch):
    monkeypatch.setattr(wapp, "TF_FLAVOR", "opentofu")
    wapp._TF_SERVER_STATUS.clear()
    sx.srv.seal_version = "1.17.6"
    status, doc = post(wapp, "/api/secrets/status", method="get")
    assert doc["tf_server"]["warning"] == "tf-encryption-vault-unverified" and doc["tf_server"]["acknowledged"] is False
    wapp._TF_SERVER_STATUS.clear()
    sx.srv.seal_version = "2.0.0-HEAD"
    status, doc = post(wapp, "/api/secrets/status", method="get")
    assert doc["tf_server"]["warning"] is None


def test_a_failed_snapshot_that_records_nothing_is_not_relaunched_every_tick(wapp, sx, bg):
    """Relecture finale I8 : un outil qui échoue sans écrire last_error_at
    (plantage, version ancienne) n'est relancé qu'après le délai de réessai."""
    sx.set(snapshots={"last_ok_at": _at(2, 31, -1)})
    wapp._SECRETS_SNAPSHOT_TRIED["at"] = 0.0
    wapp._secrets_housekeeping_tick(now=_at(2, 35))
    wapp._secrets_housekeeping_tick(now=_at(2, 40))
    wapp._secrets_housekeeping_tick(now=_at(3, 30))
    assert len(bg) == 1
    wapp._secrets_housekeeping_tick(now=_at(3, 36))
    assert len(bg) == 2


# Relecture finale I10 : après une rotation ssh-kvm, un retour de version ou
# une restauration lancés par la console, son cache (et les copies tmpfs
# partagées) est vidé ; sinon elle servait l'ancienne clé, que l'hôte refuse.
@pytest.mark.parametrize("path,body,clears", [
    ("/api/secrets/rotate", {"what": "ssh-kvm", "target": "node2"}, True),
    ("/api/secrets/rollback", {"ref": "openbao:ssh-kvm/node2#private_key", "version": 1}, True),
    ("/api/secrets/restore", {"file": "openbao-20261108T023000Z-hops-20261108-1.snap", "confirm": CID,
                              "ack_risks": True, "force": True}, True),
    ("/api/secrets/rotate", {"what": "secret-id"}, False),
])
def test_cache_emptied_after_actions_that_change_secret_values(wapp, sx, monkeypatch, path, body, clears):
    cleared = []
    monkeypatch.setattr(sx.store, "clear_cache", lambda: cleared.append(True))
    status, doc = post(wapp, path, body)
    assert status == 202, doc
    assert bool(cleared) is clears


# Tâche 13, essai réel R5/R8 : les actions lancées par la console passent
# leur identifiant à l'outil (X-Hops-Action du journal d'audit, métadonnées
# `hops_action` des versions) ; avant, l'audit disait « bg-console ».
@pytest.mark.parametrize("path,body", [
    ("/api/secrets/migrate", {"class": "ssh-kvm", "to": "openbao"}),
    ("/api/secrets/rotate", {"what": "ssh-kvm", "target": "node2"}),
    ("/api/secrets/snapshot", {}),
])
def test_tool_actions_carry_their_action_id(wapp, sx, path, body):
    status, doc = post(wapp, path, body)
    assert status == 202, doc
    assert sx.launched[-1]["kw"].get("id_arg") is True


def test_originals_sweep_and_entity_removal_carry_their_action_id():
    src = (Path(__file__).resolve().parents[2] / "web" / "app.py").read_text()
    for fn in ("def _secret_originals_sweep", "def _tf_entity_remove_start"):
        body = src[src.index(fn):]
        body = body[:body.index("\ndef ", 1)]
        assert "id_arg=True" in body, fn


# Relecture finale I5 : un identifiant de déclaration venu du navigateur
# (reprise localStorage d'avant la v1.54) qui désigne une déclaration
# supprimée dont la clé transit est gardée n'est pas repris : la nouvelle
# déclaration reçoit un identifiant tiré par le serveur.
@pytest.mark.parametrize("requested,kept", [
    ("d1d1d1d1d1d1", False),       # retirée, clé gardée : identifiant neuf
    ("e2e2e2e2e2e2", True),        # inconnue : reprise telle quelle
    (None, False),                 # rien demandé : tiré par le serveur
])
def test_declaration_id_of_a_retired_key_is_not_reused(wapp, sx, requested, kept):
    sx.set(tf_keys_retired={"d1d1d1d1d1d1": {"at": 1, "trial": False}})
    got = wapp._tf_decl_id_for_create(requested)
    assert (got == requested) is kept
    assert wapp._tfs.ID_RE.match(got) if hasattr(wapp._tfs, "ID_RE") else len(got) == 12


# ---------------------------------------------------------------------------
# Relecture finale I2 : contrôle avant restauration, avant tout arrêt
# ---------------------------------------------------------------------------
SNAP = "openbao-20261108T023000Z-hops-20261108-1.snap"
SNAP_AT = 1794105000          # 2026-11-08T02:30:00Z


@pytest.fixture
def pre(wapp, sx, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setattr(wapp, "TF_WORKSPACES", ws)
    monkeypatch.setattr(wapp, "ACTIONS", {})
    monkeypatch.setattr(wapp, "ACTIONS_DB", tmp_path / "actions.db")
    wapp._actions_init_db()
    decls = {}
    monkeypatch.setattr(wapp, "TF_DECLS", type("D", (), {"get": staticmethod(lambda i: decls.get(i))})())
    sx.set(mode="b1")

    def encrypted(decl, created, written=None, kvm=False):
        # `written` : date d'écriture de l'état (sa création par défaut)
        d = (ws / ".kvm" / "h1" if kvm else ws / "c1") / "decls" / decl
        d.mkdir(parents=True)
        f = d / "terraform.tfstate"
        f.write_text(json.dumps({"encrypted_data": "x", "encryption_version": "v0"}))
        os.utime(f, (written or created,) * 2)
        decls[decl] = {"id": decl, "name": f"n-{decl}", "created_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(created))}

    def done(label, at):
        r = wapp.ActionRun(wapp.uuid.uuid4().hex[:12], label, "", [])
        r.started_at = at
        r.status, r.exit_code = "done", 0
        r.close()
        wapp.ACTIONS.pop(r.id, None)
    return type("P", (), {"encrypted": staticmethod(encrypted), "done": staticmethod(done)})


def test_preflight_lists_what_the_restore_would_break(wapp, sx, pre):
    pre.encrypted("old0decl0001", SNAP_AT - 86400)          # écrit avant l'instantané : lisible
    pre.encrypted("new0decl0001", SNAP_AT + 3600)           # créée après : clé absente de l'instantané
    # tournée après l'instantané, état réécrit avec la nouvelle version
    pre.encrypted("rot0decl0001", SNAP_AT - 86400, written=SNAP_AT + 90)
    pre.done("secrets:rotate-transit:rot0decl0001", SNAP_AT + 60)
    pre.done("secrets:rotate-ssh-kvm:node2", SNAP_AT + 120)           # clé KVM tournée après
    pre.done("secrets:rotate-ssh-kvm:node3", SNAP_AT - 120)           # avant : sans effet
    sx.set(mode="b1", migrated={"kubeconfig": {"at": SNAP_AT + 30, "by": "alice"},
                                "rancher-oidc": {"at": SNAP_AT - 30, "by": "alice"}})
    status, doc = post(wapp, f"/api/secrets/restore/preflight?file={SNAP}", method="get")
    assert status == 200, doc
    assert doc["snapshot_at"] == SNAP_AT
    assert sorted(d["id"] for d in doc["tf_declarations"]) == ["new0decl0001", "rot0decl0001"]
    assert [h["host"] for h in doc["kvm_hosts"]] == ["node2"]
    assert doc["classes"] == ["kubeconfig"]
    assert doc["risks"] is True and doc["busy"] == []


@pytest.mark.parametrize("kvm", [False, True])
def test_preflight_lists_a_state_first_encrypted_after_the_snapshot(wapp, sx, pre, kvm):
    """Relecture des corrections finales IMP-2 : déclaration créée bien avant
    l'instantané, état migré (clé transit créée) après lui. Sa clé n'existe
    pas dans l'instantané : l'état serait illisible après la restauration."""
    pre.encrypted("mig0decl0001", SNAP_AT - 90 * 86400, written=SNAP_AT + 86400, kvm=kvm)
    pre.encrypted("old0decl0001", SNAP_AT - 90 * 86400, written=SNAP_AT - 60, kvm=kvm)
    status, doc = post(wapp, f"/api/secrets/restore/preflight?file={SNAP}", method="get")
    assert status == 200, doc
    assert [d["id"] for d in doc["tf_declarations"]] == ["mig0decl0001"]
    assert doc["tf_declarations"][0]["reason"] == "state written after the snapshot"
    assert doc["risks"] is True
    status, doc = post(wapp, "/api/secrets/restore", {"file": SNAP, "confirm": CID})
    assert status == 409 and doc["code"] == "restore-risks"


def test_restore_refused_while_actions_run_or_risks_are_not_acknowledged(wapp, sx, pre):
    busy = wapp.ActionRun(wapp.uuid.uuid4().hex[:12], "tf-apply:c1/n", "c1", [])
    busy.status = "running"
    wapp.ACTIONS[busy.id] = busy
    body = {"file": SNAP, "confirm": CID}
    status, doc = post(wapp, "/api/secrets/restore", body)
    assert status == 409 and doc["code"] == "actions-running" and doc["busy"][0]["id"] == busy.id
    busy.status = "done"
    pre.encrypted("new0decl0001", SNAP_AT + 3600)
    status, doc = post(wapp, "/api/secrets/restore", body)
    assert status == 409 and doc["code"] == "restore-risks" and doc["tf_declarations"][0]["id"] == "new0decl0001"
    assert not sx.launched
    status, doc = post(wapp, "/api/secrets/restore", dict(body, ack_risks=True))
    assert status == 202, doc
    assert sx.launched[-1]["label"] == f"secrets:restore:{SNAP}"


def test_restore_without_risk_needs_no_acknowledgement(wapp, sx, pre):
    pre.encrypted("old0decl0001", SNAP_AT - 86400)
    status, doc = post(wapp, "/api/secrets/restore", {"file": SNAP, "confirm": CID})
    assert status == 202, doc
