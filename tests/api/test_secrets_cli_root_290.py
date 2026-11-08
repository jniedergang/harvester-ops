"""2.9.0, third pass (review of pass 2, I1 and minors): `sudo harvester-secrets`
must not trust the console's directory.

As root, the tool only makes the in-memory call to the agent; every other
step runs in a child that dropped to the `harvester-ops` account before it
reads the state. In this unprivileged sandbox the drop is recorded, not done:
the cases check that it happens before any access to the state, and the
writes that stay in the console's directory never follow a link.
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_harvester_secrets_cli_290 import (  # noqa: E402,F401
    CID, cli, creds, env, hsa, hsc, settings, ss)
from fake_openbao_b1 import make_bundle, cosign_test_key  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return make_bundle(tmp_path_factory.mktemp("bundle"))


@pytest.fixture
def root(env, monkeypatch):
    """The tool run as root: the privilege drop is recorded, and every read
    of the state directory checks it already happened."""
    dropped = env["dropped"]
    real_state = hsc.state

    def state():
        assert dropped, "the state directory was read by root"
        return real_state()
    monkeypatch.setattr(hsc, "state", state)
    real_fs = ss.FileStore.__init__

    def fs_init(self, *a, **k):
        assert dropped, "the console files were opened by root"
        return real_fs(self, *a, **k)
    monkeypatch.setattr(ss.FileStore, "__init__", fs_init)
    env["dropped"] = dropped
    return env


@pytest.fixture
def test_key(monkeypatch):
    monkeypatch.setattr(hsc, "COSIGN_KEY", cosign_test_key()[1])
    monkeypatch.setattr(hsc, "cosign_key", lambda: cosign_test_key()[1], raising=False)


def staged(env):
    d = env["state"] / "secrets-agent" / "staged"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# I1 : root ne lit ni n'écrit l'état de la console
# ---------------------------------------------------------------------------
def test_bundle_copy_never_follows_a_link_under_staged(env, capsys, tmp_path, bundle, test_key):
    """E1 of the review: staged/<name> -> a root file; the copy of the bundle
    must not overwrite it."""
    path, _ = bundle
    victim = tmp_path / "shadow"
    victim.write_text("root:x:0:0\n")
    (staged(env) / path.name).symlink_to(victim)
    src = tmp_path / "admin" / path.name
    src.parent.mkdir()
    src.write_bytes(path.read_bytes())
    settings(env, mode="local")
    cli(["enable", "--mode", "b1", "--bundle", str(src)], capsys=capsys)
    assert victim.read_text() == "root:x:0:0\n"


def test_bundle_copy_never_follows_a_staged_directory_link(env, capsys, tmp_path, bundle, test_key):
    path, _ = bundle
    outside = tmp_path / "outside"
    outside.mkdir()
    d = env["state"] / "secrets-agent"
    d.mkdir(parents=True)
    (d / "staged").symlink_to(outside)
    src = tmp_path / path.name
    src.write_bytes(path.read_bytes())
    settings(env, mode="local")
    cli(["enable", "--mode", "b1", "--bundle", str(src)], capsys=capsys)
    assert list(outside.iterdir()) == []


def test_settings_save_never_chmods_by_path(env, monkeypatch, tmp_path):
    """Review: chmod of the temporary file by path could be redirected by a
    link swapped in between; the result must stay readable by the console."""
    def no_chmod(*a, **k):
        raise AssertionError("chmod by path in the console directory")
    monkeypatch.setattr(os, "chmod", no_chmod)
    victim = tmp_path / "victim"
    victim.write_text("x")
    (env["state"] / ss.SETTINGS_FILE).symlink_to(victim)
    hsc.save_settings({"mode": "local"})
    p = env["state"] / ss.SETTINGS_FILE
    assert victim.read_text() == "x" and not p.is_symlink()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


@pytest.mark.parametrize("argv", [
    ["migrate", "--to", "openbao", "--class", "ssh-cluster", "--dry-run"],
    ["status", "--json"],
    ["confirm", "--class", "ssh-cluster"],
    ["rotate", "secret-id"],
])
def test_root_drops_before_reading_the_state(root, capsys, argv):
    creds(root)
    settings(root)
    cli(argv, capsys=capsys)
    assert root["dropped"]


def test_root_refuses_snapshot(root, capsys, tmp_path):
    settings(root, mode="b1")
    rc, doc, _ = cli(["snapshot", "--dest", str(tmp_path)], capsys=capsys)
    assert rc == 2 and doc["code"] == "root-refused" and not root["dropped"]


def test_migrate_refuses_a_key_reached_through_a_link_inside_the_state(env, tmp_path):
    """E2 variant: the declaration names a relative path whose directory is
    a link out of the state; the secret stays local."""
    outside = tmp_path / "root-only"
    outside.mkdir()
    (outside / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nroot\n")
    st = env["state"]
    (st / "lnk").symlink_to(outside)
    (st / "clusters.d").mkdir(exist_ok=True)
    (st / "clusters.d" / "x.yaml").write_text("name: x\nkubeconfig: kubeconfigs/x\nssh:\n  key: lnk/id_ed25519\n")
    assert ss.FileStore(st).eligible("ssh-cluster", "x", "private_key") == "secret-outside-state"


# -- la mécanique du fils sans privilège ---------------------------------------
def test_drop_privileges_uses_the_service_account(monkeypatch):
    import grp
    import pwd
    calls = []
    me = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(pwd, "getpwnam", lambda n: me if n == "harvester-ops" else (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(grp, "getgrnam", lambda n: grp.getgrgid(os.getgid()))
    monkeypatch.setattr(os, "setgroups", lambda g: calls.append(("setgroups", list(g))))
    monkeypatch.setattr(os, "setresgid", lambda *a: calls.append(("setresgid", a)))
    monkeypatch.setattr(os, "setresuid", lambda *a: calls.append(("setresuid", a)))
    monkeypatch.setattr(os, "getresuid", lambda: (me.pw_uid,) * 3)
    monkeypatch.setattr(os, "getresgid", lambda: (os.getgid(),) * 3)
    hsc._drop_privileges()
    names = [c[0] for c in calls]
    assert names == ["setgroups", "setresgid", "setresuid"]           # groupes, puis gid, puis uid
    assert calls[1][1] == (os.getgid(),) * 3 and calls[2][1] == (me.pw_uid,) * 3


def test_drop_privileges_refused_without_the_account(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda n: (_ for _ in ()).throw(KeyError(n)))
    with pytest.raises(hsc.CliError) as e:
        hsc._drop_privileges()
    assert e.value.code == "service-account-missing"


def test_forked_child_reaches_the_agent_only_through_the_parent(monkeypatch, capfd):
    """Real fork, drop recorded: the child's request goes through the pipe,
    the root parent answers with the agent's state and sets the requester."""
    monkeypatch.setattr(hsc, "_drop_privileges", lambda: os.environ.__setitem__("HOPS_TEST_DROPPED", "1"))
    monkeypatch.delenv("HOPS_TEST_DROPPED", raising=False)
    seen = []

    def handler(req):
        seen.append(req)
        return {"state": "done", "request_id": req.get("id"), "steps": [{"id": "x", "status": "done"}]}

    def child():
        st = hsc.AGENT_CHANNEL({"op": "status", "id": "0123abcd", "requested_by": "forged"})
        print(json.dumps({"child": st["state"], "dropped": os.environ.get("HOPS_TEST_DROPPED")}))
        return 3
    monkeypatch.setenv("SUDO_USER", "alice")
    rc = hsc._spawn_unprivileged(child, handler)
    assert rc == 3
    assert seen == [{"op": "status", "id": "0123abcd", "requested_by": "alice"}]
    assert '{"child": "done", "dropped": "1"}' in capfd.readouterr().out     # droits quittés avant fn
    assert "HOPS_TEST_DROPPED" not in os.environ                               # dans le fils seulement


# ---------------------------------------------------------------------------
# Mineurs
# ---------------------------------------------------------------------------
def test_cosign_key_is_the_one_installed_under_opt(monkeypatch, tmp_path):
    """M6: the script lives in /usr/local/bin, the key in /opt/harvester-ops."""
    (tmp_path / "openbao").mkdir()
    (tmp_path / "openbao" / "application-collection-cosign.pub").write_text("k")
    monkeypatch.setenv("HARVESTER_OPS_OPT", str(tmp_path))
    assert hsc.cosign_key() == tmp_path / "openbao" / "application-collection-cosign.pub"
    monkeypatch.setenv("HARVESTER_OPS_OPT", str(tmp_path / "absent"))
    assert hsc.cosign_key() == ROOT / "config" / "openbao" / "application-collection-cosign.pub"


def test_safe_fs_create_never_replaces(tmp_path):
    """M5: the b1-policies request is created only if none is waiting, in
    one step (no exists-then-write window)."""
    base = tmp_path / "state"
    (base / "secrets-agent").mkdir(parents=True)
    rp = base / "secrets-agent" / "request.json"
    lib = ROOT / "bin" / "lib" / "safe_fs.py"
    r = subprocess.run([sys.executable, str(lib), "create", str(base), "secrets-agent/request.json", "0600"],
                       input=b'{"op": "b1-policies"}\n', capture_output=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(rp.read_text()) == {"op": "b1-policies"} and stat.S_IMODE(rp.stat().st_mode) == 0o600
    r = subprocess.run([sys.executable, str(lib), "create", str(base), "secrets-agent/request.json", "0600"],
                       input=b'{"op": "other"}\n', capture_output=True)
    assert r.returncode == 4 and json.loads(rp.read_text()) == {"op": "b1-policies"}
    assert [p.name for p in rp.parent.iterdir()] == ["request.json"]      # aucun temporaire laissé
