"""2.9.0 tâche 6 : agent de l'hôte pour les secrets (bin/harvester-secrets-agent.py).

Faux OpenBao (fake_openbao_b1.FakeB1), faux hôte (commandes notées,
openssl réel), paquet construit contre le faux registre. Chaque chemin
d'erreur a son cas ; aucune valeur secrète dans status.json ni le journal.

Mutations jouées (rapport des tâches 5 à 7) : « opération hors liste
acceptée », « ancien secret_id détruit avant la connexion d'essai »,
« ancienne clé retirée sans sys/rotate/root », « volume effacé après un
premier descellement », « demande traitée sans être effacée ».
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

spec = importlib.util.spec_from_file_location("hsa", ROOT / "bin" / "harvester-secrets-agent.py")
hsa = importlib.util.module_from_spec(spec)
sys.modules["hsa"] = hsa
spec.loader.exec_module(hsa)

import openbao_policy as pol  # noqa: E402
import openbao_client as oc  # noqa: E402
from fake_openbao_b1 import FakeB1, FakeHost, TOKEN_ACCESSOR, make_bundle  # noqa: E402

CID = "abc123def456"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return make_bundle(tmp_path_factory.mktemp("bundle"))


@pytest.fixture
def env(tmp_path, bundle):
    e = {"HARVESTER_OPS_STATE_DIR": str(tmp_path / "var-lib-hops"), "HARVESTER_OPS_ETC": str(tmp_path / "etc-hops"),
         "HARVESTER_OPS_OPENBAO_ETC": str(tmp_path / "etc-bao"), "HARVESTER_OPS_OPENBAO_DATA": str(tmp_path / "bao-data"),
         "HARVESTER_OPS_OPENBAO_BOOT": str(tmp_path / "run-boot"), "HARVESTER_OPS_UNIT_DIR": str(tmp_path / "units"),
         "HARVESTER_OPS_OPT": str(tmp_path / "opt"), "HARVESTER_OPS_LOGROTATE_DIR": str(tmp_path / "logrotate"),
         "HARVESTER_SECRETS_AGENT_WORK": str(tmp_path / "work"), "HARVESTER_OPS_LOG_DIR": str(tmp_path / "logs")}
    p = hsa.Paths(e)
    (p.state / "staged").mkdir(parents=True)
    path, info = bundle
    (p.state / "staged" / path.name).write_bytes(path.read_bytes())
    p.unit_src.parent.mkdir(parents=True)
    p.unit_src.write_text((ROOT / "config" / "systemd" / hsa.UNIT).read_text())
    srv = FakeB1(CID).start()
    host = FakeHost(p, digest=info["manifest_digest"])
    host.on_start = lambda: srv.initialize(p.boot) if p.boot.exists() else None
    clock = {"t": time.time()}

    def agent():
        a = hsa.Agent(paths=p, host=host, client=lambda addr, ca_pem=None: oc.Client(
            srv.addr, timeout=2, retry_delays=(0.01,)), sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
            now=lambda: clock["t"], b1_addr=srv.addr)
        a._need_podman = lambda: None
        return a
    yield {"p": p, "srv": srv, "host": host, "agent": agent, "bundle": path, "info": info, "clock": clock}
    srv.stop()


def request(env, **req):
    req.setdefault("id", "0123abcd")
    (env["p"].state / hsa.REQUEST).write_text(json.dumps(req))
    rc = env["agent"]().process()
    return rc, json.loads((env["p"].state / hsa.STATUS).read_text())


def install(env):
    rc, st = request(env, op="b1-install", console_id=CID, bundle=env["bundle"].name, ack_offline_copy=True)
    assert rc == 0, st
    return st


def mode(path):
    return path.stat().st_mode & 0o777


def no_secret_in(env, st, extra=()):
    blob = json.dumps(st) + "".join(p.read_text() for p in env["p"].logs.glob("*.log"))
    srv = env["srv"]
    values = [s for r in srv.roles.values() for s in r["secret_ids"]] + list(srv.tokens) + list(extra)
    for v in values:
        assert v not in blob


# ---------------------------------------------------------------------------
# Demande
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("req,code", [
    ({"op": "rm-rf"}, "unknown-operation"),
    ({"op": "exec", "cmd": "id"}, "unknown-operation"),
    ({"op": "status", "extra": 1}, "bad-request"),
    ({"op": "b1-install", "console_id": "nothex", "bundle": "x", "ack_offline_copy": True}, "bad-request"),
    ({"op": "status", "id": "../x"}, "bad-request"),
])
def test_request_outside_the_closed_list_refused(env, req, code):
    rc, st = request(env, **req)
    assert rc == 1 and st["state"] == "failed" and st["code"] == code
    assert not (env["p"].state / hsa.REQUEST).exists()
    assert env["host"].cmds == []


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "x" * (hsa.REQUEST_MAX + 10)])
def test_unreadable_request_refused_and_removed(env, raw):
    (env["p"].state / hsa.REQUEST).write_text(raw)
    assert env["agent"]().process() == 1
    st = json.loads((env["p"].state / hsa.STATUS).read_text())
    assert st["code"] == "bad-request" and not (env["p"].state / hsa.REQUEST).exists()


def test_symlinked_request_refused(env, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"op": "status"}))
    (env["p"].state / hsa.REQUEST).symlink_to(target)
    assert env["agent"]().process() == 1
    assert target.exists() and not (env["p"].state / hsa.REQUEST).is_symlink()


def test_status_has_no_value(env):
    install(env)
    rc, st = request(env, op="status")
    assert rc == 0 and st["info"]["b1_installed"] and st["info"]["current_key"].startswith("hops-")
    no_secret_in(env, st)


# ---------------------------------------------------------------------------
# store-secret-id
# ---------------------------------------------------------------------------
def deposit(env, wrap, cid=CID, role_id=None):
    srv = env["srv"]
    rid = role_id or srv.role_ids.setdefault(pol.console_role(cid), "rid-console-1")
    srv.roles.setdefault(rid, {"secret_ids": set(), "ttl": 3600})
    return request(env, op="store-secret-id", console_id=cid, addr=srv.addr, role_id=rid, wrap_token=wrap)


def wrapped(env, cid=CID, path=None):
    sid = "sid-" + os.urandom(6).hex()
    rid = env["srv"].role_ids.setdefault(pol.console_role(cid), "rid-console-1")
    env["srv"].add_role(rid, sid)
    return env["srv"].wrap({"secret_id": sid, "secret_id_accessor": "acc-1"},
                           path or pol.secret_id_path(cid)), sid


def test_store_secret_id(env):
    wrap, sid = wrapped(env)
    rc, st = deposit(env, wrap)
    assert rc == 0, st
    e = env["p"].etc
    assert (e / "secret-id").read_text().strip() == sid and mode(e / "secret-id") == 0o640
    assert mode(e / "role-id") == 0o640 and mode(e / "secret-id-accessor") == 0o600
    no_secret_in(env, st, [wrap, sid])


@pytest.mark.parametrize("case", ["used", "expired", "other-path"])
def test_store_secret_id_bad_wrap_writes_nothing(env, case):
    wrap, sid = wrapped(env, path=f"auth/approle/role/someone-else/secret-id" if case == "other-path" else None)
    srv = env["srv"]
    if case == "used":
        srv.wraps.pop(wrap)
    elif case == "expired":
        srv.wraps[wrap]["expires"] = time.time() - 1
    rc, st = deposit(env, wrap)
    assert rc == 1 and st["code"] == "openbao-unwrap-used" and "someone else may have read" in st["message"]
    assert not env["p"].etc.exists()
    if case == "other-path":
        assert not srv.requests("sys/wrapping/unwrap")


@pytest.mark.parametrize("field,value", [("role_id", "x"), ("wrap_token", ""), ("ca_pem", "not a cert")])
def test_store_secret_id_bad_input(env, field, value):
    wrap, _ = wrapped(env)
    req = dict(op="store-secret-id", console_id=CID, addr=env["srv"].addr, role_id="rid-console-1", wrap_token=wrap)
    req[field] = value
    rc, st = request(env, **req)
    assert rc == 1 and st["code"] == "bad-request" and not env["p"].etc.exists()


# ---------------------------------------------------------------------------
# b1-install
# ---------------------------------------------------------------------------
def test_b1_install_files_and_rights(env):
    st = install(env)
    p, srv = env["p"], env["srv"]
    assert [s["id"] for s in st["steps"]] == ["bundle", "image", "account", "directories", "tls", "unseal-key",
                                             "boot", "config", "start", "settings", "verify", "credentials"]
    assert mode(p.etc / "role-id") == mode(p.etc / "secret-id") == 0o640 and mode(p.etc / "ca.pem") == 0o644
    assert mode(p.etc / "secret-id-accessor") == 0o600
    assert mode(p.b1_etc) == 0o700 and mode(p.b1_etc / "unseal.key") == 0o400
    assert mode(p.b1_etc / "agent-secret-id") == 0o400 and mode(p.b1_etc / "agent.json") == 0o600
    assert all(mode(k) == 0o400 for k in (p.b1_etc / "keys").iterdir())
    assert mode(p.b1_etc / "tls" / "server.key") == 0o400
    assert len((p.b1_etc / "unseal.key").read_bytes()) == 32
    assert env["host"].secrets[hsa.SECRET] == (p.b1_etc / "unseal.key").read_bytes()
    assert not p.boot.exists()                                   # fichiers d'amorce effacés
    assert not (p.work / "pki").exists()                         # clé de la CA effacée
    assert srv.policies[pol.tf_role(CID)] == pol.tf_child_policy(CID, TOKEN_ACCESSOR)
    assert pol.boot_policy_name(CID) not in srv.policies         # politique d'amorce retirée
    assert not any(v["policies"] == [pol.boot_policy_name(CID)] for v in srv.tokens.values())   # révoqué
    cfg = (p.b1_etc / "openbao.hcl").read_text()
    assert 'initialize "hops"' not in cfg and 'seal "static"' in cfg
    unit = (p.units / hsa.UNIT)
    assert unit.exists() and env["host"].ran("systemctl", "enable", "--now", hsa.UNIT)
    no_secret_in(env, st)


def test_b1_install_sets_max_versions_when_initialize_could_not(env, monkeypatch):
    calls = []
    real = env["srv"]._kv

    def kv(h, m, mount, rest, body, rec):
        if rest == "config" and m == "GET":
            calls.append("get")
            return h._send(200, {"data": {"max_versions": 0}})
        if rest == "config":
            calls.append("set")
        return real(h, m, mount, rest, body, rec)
    env["srv"]._kv = kv
    install(env)
    assert calls == ["get", "set"]


def test_b1_install_needs_the_offline_copy_ack(env):
    rc, st = request(env, op="b1-install", console_id=CID, bundle=env["bundle"].name, ack_offline_copy=False)
    assert rc == 1 and st["code"] == "offline-copy-ack-required" and env["host"].cmds == []


def test_b1_install_refuses_an_existing_volume(env):
    d = env["p"].b1_data / "data"
    d.mkdir(parents=True)
    (d / "raft.db").write_text("x")
    rc, st = request(env, op="b1-install", console_id=CID, bundle=env["bundle"].name, ack_offline_copy=True)
    assert rc == 1 and st["code"] == "b1-volume-exists" and (d / "raft.db").exists()


@pytest.mark.parametrize("name,code", [("../../etc/passwd", "bundle-bad-name"),
                                       ("harvester-ops-openbao-9.9.9-nope.tar.gz", "bundle-missing")])
def test_b1_install_bundle_outside_staging_refused(env, name, code):
    rc, st = request(env, op="b1-install", console_id=CID, bundle=name, ack_offline_copy=True)
    assert rc == 1 and st["code"] == code


def test_b1_install_wrong_image_digest_refused(env):
    env["host"].digest = "sha256:" + "0" * 64
    rc, st = request(env, op="b1-install", console_id=CID, bundle=env["bundle"].name, ack_offline_copy=True)
    assert rc == 1 and st["code"] == "bundle-digest-mismatch"


@pytest.mark.parametrize("fail_at", ["start", "settings", "verify"])
def test_b1_install_failure_rolls_everything_back(env, fail_at):
    p, srv, host = env["p"], env["srv"], env["host"]
    if fail_at == "start":
        host.on_start = lambda: setattr(host, "active", "failed")
        srv.initialized = False
    elif fail_at == "settings":
        srv.fail_paths["sys/auth"] = (403, "permission denied")
    else:
        real = srv.initialize

        def init(boot):
            real(boot)
            srv.roles[srv.role_ids[pol.agent_role(CID)]]["secret_ids"].clear()
        host.on_start = lambda: init(p.boot)
    rc, st = request(env, op="b1-install", console_id=CID, bundle=env["bundle"].name, ack_offline_copy=True)
    assert rc == 1 and st["state"] == "failed"
    assert host.ran("systemctl", "disable", "--now", hsa.UNIT)
    assert hsa.SECRET not in host.secrets
    assert not p.boot.exists() and not (p.b1_data / "data").exists() and not p.logrotate.exists()
    assert not (p.etc / "secret-id").exists() and not (p.b1_etc / "agent.json").exists()
    assert not (p.b1_etc / "openbao.hcl").exists() and not (p.b1_etc / "tls").exists()
    # acquittement donné : la clé reste et resservira, la copie hors ligne reste valable
    key = (p.b1_etc / "unseal.key").read_bytes()
    host.on_start = lambda: srv.initialize(p.boot)
    srv.initialized = True
    install(env)
    assert (p.b1_etc / "unseal.key").read_bytes() == key


def test_b1_install_failure_journal_kept(env):
    env["host"].on_start = lambda: setattr(env["host"], "active", "failed")
    env["srv"].initialized = False
    rc, st = request(env, op="b1-install", console_id=CID, bundle=env["bundle"].name, ack_offline_copy=True)
    assert st["code"] == "b1-start-failed" and "wiped" in st["message"]
    assert env["host"].ran("journalctl", "-u", hsa.UNIT)


def test_volume_never_wiped_once_unsealed(env):
    install(env)
    a = env["agent"]()
    with pytest.raises(hsa.AgentError) as e:
        a._wipe_new_volume(whole=True)
    assert e.value.code == "b1-volume-unsealed" and (env["p"].b1_data / "data").exists()
    rc, st = request(env, op="b1-install", console_id=CID, bundle=env["bundle"].name, ack_offline_copy=True)
    assert rc == 1 and st["code"] == "b1-already-installed"


# ---------------------------------------------------------------------------
# rotate-secret-id
# ---------------------------------------------------------------------------
def settings(env, **kw):
    doc = {"mode": "b1", "addr": env["srv"].addr, "console_id": CID, **kw}
    (env["p"].console_state / "secrets-settings.json").write_text(json.dumps(doc))


def test_rotate_secret_id(env):
    install(env)
    settings(env)
    p, srv = env["p"], env["srv"]
    old, old_acc = (p.etc / "secret-id").read_text().strip(), (p.etc / "secret-id-accessor").read_text().strip()
    rc, st = request(env, op="rotate-secret-id")
    assert rc == 0, st
    new = (p.etc / "secret-id").read_text().strip()
    assert new != old and mode(p.etc / "secret-id") == 0o640
    rid = srv.role_ids[pol.console_role(CID)]
    assert old not in srv.roles[rid]["secret_ids"] and new in srv.roles[rid]["secret_ids"]
    assert ("destroy-accessor", old_acc) in srv.events
    no_secret_in(env, st, [old, new])


def test_rotate_secret_id_failed_test_login_keeps_the_old(env):
    install(env)
    settings(env)
    p, srv = env["p"], env["srv"]
    old = (p.etc / "secret-id").read_text().strip()
    rid = srv.role_ids[pol.console_role(CID)]
    real = srv._route

    def route(h, rec, path, body, raw):
        if path == "auth/approle/login" and body.get("secret_id") != old:
            return h._err(400, "invalid secret id")
        return real(h, rec, path, body, raw)
    srv._route = route
    rc, st = request(env, op="rotate-secret-id")
    assert rc == 1
    assert (p.etc / "secret-id").read_text().strip() == old and old in srv.roles[rid]["secret_ids"]
    assert not [e for e in srv.events if e[0] == "destroy-accessor"]


def test_rotate_secret_id_needs_openbao_active(env):
    rc, st = request(env, op="rotate-secret-id")
    assert rc == 1 and st["code"] == "openbao-inactive"


def test_timer_rotates_after_30_days_only(env):
    install(env)
    settings(env)
    sid = env["p"].etc / "secret-id"
    before = sid.read_text()
    assert env["agent"]().process() == 0 and sid.read_text() == before
    os.utime(sid, (env["clock"]["t"] - hsa.SECRET_ID_MAX_AGE - 1,) * 2)
    assert env["agent"]().process() == 0 and sid.read_text() != before


def test_housekeeping_prunes_old_volumes_and_retired_keys(env):
    install(env)
    p, clock = env["p"], env["clock"]
    old = p.b1_data.with_name(p.b1_data.name + ".old-20261001-000000")
    old.mkdir()
    os.utime(old, (clock["t"] - 8 * 86400,) * 2)
    a = env["agent"]()
    st = a.agent_state()
    (p.b1_etc / "keys" / "hops-20261001-1.key").write_bytes(b"k" * 32)
    st["keys"].append({"id": "hops-20261001-1", "created": 0, "retired": clock["t"] - 15 * 86400})
    a.save_agent_state(st)
    a.process()
    assert not old.exists() and not (p.b1_etc / "keys" / "hops-20261001-1.key").exists()
    assert [k["id"] for k in a.agent_state()["keys"]] == [st["current_key"]]


# ---------------------------------------------------------------------------
# rotate-unseal-key
# ---------------------------------------------------------------------------
def test_rotate_unseal_key_rotates_root_before_dropping_the_old_key(env):
    install(env)
    p, srv, host = env["p"], env["srv"], env["host"]
    old = env["agent"]().agent_state()["current_key"]
    renders = []
    host.on_restart = lambda: renders.append(("restart", (p.b1_etc / "openbao.hcl").read_text().count("previous_key_id"),
                                              len([e for e in srv.events if e[0] == "rotate-root"])))
    env["clock"]["t"] += 86400
    rc, st = request(env, op="rotate-unseal-key", ack_offline_copy=True)
    assert rc == 0, st
    assert renders == [("restart", 1, 0), ("restart", 0, 1)]   # 2 clés sans rotation, puis 1 clé après
    a = env["agent"]().agent_state()
    assert a["current_key"] != old and [k["id"] for k in a["keys"]] == [old, a["current_key"]]
    assert next(k for k in a["keys"] if k["id"] == old)["retired"]
    assert (p.b1_etc / "unseal.key").read_bytes() == (p.b1_etc / "keys" / f"{a['current_key']}.key").read_bytes()
    assert host.secrets[hsa.SECRET] == (p.b1_etc / "unseal.key").read_bytes() and hsa.SECRET_PREV not in host.secrets
    assert list((p.b1_data / "snapshots").glob("openbao-*.snap"))


def test_rotate_unseal_key_failed_rotate_root_keeps_the_old_key(env):
    install(env)
    p, srv, host = env["p"], env["srv"], env["host"]
    old = env["agent"]().agent_state()["current_key"]
    srv.fail_paths["sys/rotate/root"] = (403, "permission denied")
    rc, st = request(env, op="rotate-unseal-key", ack_offline_copy=True)
    assert rc == 1
    assert env["agent"]().agent_state()["current_key"] == old
    assert host.secrets[hsa.SECRET] == (p.b1_etc / "keys" / f"{old}.key").read_bytes()
    assert "previous_key" not in (p.b1_etc / "openbao.hcl").read_text()
    assert len(list((p.b1_etc / "keys").iterdir())) == 1


def test_rotate_unseal_key_needs_ack(env):
    install(env)
    rc, st = request(env, op="rotate-unseal-key")
    assert rc == 1 and st["code"] == "offline-copy-ack-required"


# ---------------------------------------------------------------------------
# restore-snapshot
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,code", [("../openbao-x.snap", "snapshot-bad-name"), ("/etc/shadow", "snapshot-bad-name"),
                                       ("openbao-x.snap.sh", "snapshot-bad-name"),
                                       ("openbao-absent.snap", "snapshot-missing")])
def test_restore_bad_file_refused(env, name, code):
    install(env)
    rc, st = request(env, op="restore-snapshot", file=name)
    assert rc == 1 and st["code"] == code
    assert not env["host"].ran("systemctl", "stop")


def test_restore_symlinked_snapshot_refused(env, tmp_path):
    install(env)
    (tmp_path / "secret").write_text("x")
    (env["p"].b1_data / "snapshots" / "openbao-link.snap").symlink_to(tmp_path / "secret")
    rc, st = request(env, op="restore-snapshot", file="openbao-link.snap")
    assert rc == 1 and st["code"] == "snapshot-missing"


def test_restore_snapshot_with_the_current_key(env):
    install(env)
    p, srv, host = env["p"], env["srv"], env["host"]
    snap = p.b1_data / "snapshots" / "openbao-20261108T020000Z.snap"
    snap.write_bytes(b"SNAP" * 10)
    rc, st = request(env, op="restore-snapshot", file=snap.name)
    assert rc == 0, st
    assert srv.restored and srv.restored[-1] == b"SNAP" * 10
    assert not srv.requests("snapshot-force")
    assert host.ran("systemctl", "stop", hsa.CONSOLE_UNIT) and host.ran("systemctl", "start", hsa.CONSOLE_UNIT)
    assert list(p.b1_data.parent.glob(p.b1_data.name + ".old-*"))
    assert not [e for e in srv.events if e[0] == "rotate-root"]


class _RestoreDropsTheSecretId(list):
    """Restauration d'un instantané pris AVANT la dernière rotation du
    secret_id : le secret_id courant n'y est pas."""

    def __init__(self, srv, rid, sid):
        super().__init__()
        self.srv, self.rid, self.sid = srv, rid, sid

    def append(self, raw):
        super().append(raw)
        self.srv.roles[self.rid]["secret_ids"].discard(self.sid)
        for a, (r, s) in list(self.srv.accessors.items()):
            if (r, s) == (self.rid, self.sid):
                del self.srv.accessors[a]


@pytest.mark.parametrize("snapshot,custom", [
    ("taken-after-the-rotation", False),      # le cas courant : secret_id déjà dans l'instantané
    ("taken-before-the-rotation", True),
])
def test_restore_puts_the_console_secret_id_back_only_when_missing(env, snapshot, custom):
    """Revue I3 des tâches 5-7 : OpenBao 2.7.2 refuse en 500 un
    `custom-secret-id` déjà enregistré ; la connexion est essayée d'abord,
    l'accesseur relu ensuite."""
    install(env)
    p, srv = env["p"], env["srv"]
    rid = srv.role_ids[pol.console_role(CID)]
    sid = (p.etc / "secret-id").read_text().strip()
    if custom:
        srv.restored = _RestoreDropsTheSecretId(srv, rid, sid)
    (p.etc / "secret-id-accessor").write_text("acc-stale\n")
    snap = p.b1_data / "snapshots" / "openbao-20261108T020000Z.snap"
    snap.write_bytes(b"SNAP")
    rc, st = request(env, op="restore-snapshot", file=snap.name)
    assert rc == 0, st
    assert (("custom-secret-id", pol.console_role(CID)) in srv.events) is custom
    acc = (p.etc / "secret-id-accessor").read_text().strip()
    assert srv.accessors[acc] == (rid, sid)                  # accesseur du secret_id en service
    no_secret_in(env, st)


def test_fake_refuses_a_duplicate_custom_secret_id(env):
    """Le faux serveur reproduit OpenBao 2.7.2 (essai P4 de la revue)."""
    install(env)
    srv, p = env["srv"], env["p"]
    a = env["agent"]().agent_client()
    with pytest.raises(oc.OpenBaoError) as e:
        a._call("POST", f"auth/approle/role/{pol.console_role(CID)}/custom-secret-id",
                {"secret_id": (p.etc / "secret-id").read_text().strip()})
    assert "already registered" in str(e.value.detail)


def test_restore_snapshot_of_a_retired_key(env):
    install(env)
    p, srv = env["p"], env["srv"]
    env["clock"]["t"] += 86400
    request(env, op="rotate-unseal-key", ack_offline_copy=True)
    a = env["agent"]().agent_state()
    old = a["keys"][0]
    snap = p.b1_data / "snapshots" / "openbao-old.snap"
    snap.write_bytes(b"OLD")
    os.utime(snap, (old["created"] + 10,) * 2)
    rotations = len([e for e in srv.events if e[0] == "rotate-root"])
    rc, st = request(env, op="restore-snapshot", file=snap.name)
    assert rc == 0, st
    assert len([e for e in srv.events if e[0] == "rotate-root"]) == rotations + 1
    assert "previous_key" not in (p.b1_etc / "openbao.hcl").read_text()


def test_restore_failure_puts_the_volume_back(env):
    install(env)
    p, srv = env["p"], env["srv"]
    marker = p.b1_data / "data" / "raft.db"
    marker.write_text("live")
    snap = p.b1_data / "snapshots" / "openbao-bad.snap"
    snap.write_bytes(b"BAD")
    srv.snapshot_key_ok = False
    rc, st = request(env, op="restore-snapshot", file=snap.name)
    assert rc == 1
    assert marker.read_text() == "live" and not list(p.b1_data.parent.glob(p.b1_data.name + ".old-*"))
    assert env["host"].ran("systemctl", "start", hsa.CONSOLE_UNIT)


# ---------------------------------------------------------------------------
# b1-upgrade, b1-remove
# ---------------------------------------------------------------------------
def test_b1_upgrade_rolls_back_when_the_new_image_fails(env):
    install(env)
    srv, host = env["srv"], env["host"]
    seq = iter([True, False])
    host.on_restart = lambda: setattr(srv, "sealed", next(seq, False))
    rc, st = request(env, op="b1-upgrade", bundle=env["bundle"].name)
    assert rc == 1 and st["code"] == "b1-upgrade-failed"
    assert host.ran("podman", "tag", f"{hsa.IMAGE}:previous", f"{hsa.IMAGE}:current")


def test_b1_upgrade(env):
    install(env)
    rc, st = request(env, op="b1-upgrade", bundle=env["bundle"].name)
    assert rc == 0, st
    assert env["host"].ran("podman", "tag", f"{hsa.IMAGE}:current", f"{hsa.IMAGE}:previous")


def test_b1_remove_keeps_data_aside(env):
    install(env)
    p = env["p"]
    rc, st = request(env, op="b1-remove")
    assert rc == 0
    assert not p.b1_data.exists() and list(p.b1_data.parent.glob(p.b1_data.name + ".removed-*"))
    assert list(p.b1_etc.parent.glob(p.b1_etc.name + ".removed-*")) and not (p.etc / "secret-id").exists()
    assert not (p.units / hsa.UNIT).exists()


def test_b1_ops_need_an_installed_b1(env):
    for op in ("b1-remove", "rotate-unseal-key", "restore-snapshot"):
        extra = {"ack_offline_copy": True} if op == "rotate-unseal-key" else (
            {"file": "openbao-x.snap"} if op == "restore-snapshot" else {})
        rc, st = request(env, op=op, **extra)
        assert rc == 1 and st["code"] == "b1-not-installed", op


# ---------------------------------------------------------------------------
# Décision 276 (revue I5) : politiques hops-* corrigées après la pose, entités
# tf retirées par leur alias
# ---------------------------------------------------------------------------
def test_tf_entity_removed_by_its_alias(env):
    install(env)
    srv, p = env["srv"], env["p"]
    c = env["agent"]().console_client(addr=srv.addr)
    c.token_create_child(pol.tf_role(CID), [pol.tf_role(CID)], "60s", entity_alias=pol.tf_alias("d1"))
    assert [e["name"] for e in srv.entities.values()] and all(
        e["name"].startswith("entity_") for e in srv.entities.values())
    a = env["agent"]()
    # l'ancienne règle `identity/entity/name/tf-*` ne désignait rien
    t = a.agent_client()
    with pytest.raises(oc.OpenBaoError):
        t._call("GET", "identity/entity/name/tf-d1")
    assert a.remove_tf_entity("d1") is True and not srv.entities
    assert a.remove_tf_entity("d1") is False
    with pytest.raises(ValueError):
        a.remove_tf_entity("../d1")


def test_console_refuses_a_bad_entity_alias(env):
    install(env)
    c = env["agent"]().console_client(addr=env["srv"].addr)
    for bad in ("tf-a/../b", "tf-*", "x-d1"):
        with pytest.raises(oc.OpenBaoError):
            c.token_create_child(pol.tf_role(CID), [pol.tf_role(CID)], "60s", entity_alias=bad)


def test_b1_policies_updates_only_hops_policies(env):
    install(env)
    srv = env["srv"]
    srv.policies.pop(pol.agent_role(CID), None)
    srv.events.clear()
    rc, st = request(env, op="b1-policies")
    assert rc == 0, st
    written = [e[1] for e in srv.events if e[0] == "policy-post"]
    assert sorted(written) == sorted([pol.agent_role(CID), pol.tf_role(CID)])
    assert srv.policies[pol.agent_role(CID)] == pol.agent_policy(CID)
    assert "auth_token_" in srv.policies[pol.tf_role(CID)]
    no_secret_in(env, st)
