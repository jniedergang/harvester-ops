"""2.9.0 (second pass of the review fixes): root never writes, creates or
hands over a file by following a link planted in a directory the console
account can write, for the secret store steps of install.sh and for the
root secrets agent (bin/harvester-secrets-agent.py).

Same method as test_root_writes_no_follow_283.py: everything runs
unprivileged in temporary directories, each case plants a link (or swaps an
entry at the worst moment) and checks that the file or directory outside is
untouched; a chown is recorded (the inode it would change), not done.
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_root_writes_no_follow_283 import (shell_fn, plant_dir_link, plant_file_link,  # noqa: E402
                                            chowns, ino)  # noqa: F401

BUNDLE = "harvester-ops-openbao-2.7.2-2.7.2-5.4.tar.gz"


# ---------------------------------------------------------------------------
# install.sh : étapes des secrets, rejouées à chaque --upgrade
# ---------------------------------------------------------------------------
def release(tmp_path):
    """Livrable minimal : config, VERSION, safe_fs, paquet OpenBao avec sa somme."""
    d = tmp_path / "livrable"
    (d / "config").mkdir(parents=True)
    subprocess.run(["cp", "-r", str(ROOT / "config" / "systemd"), str(ROOT / "config" / "openbao"),
                    str(d / "config")], check=True)
    (d / "VERSION").write_text("2.9.0\n")
    (d / "bin" / "lib").mkdir(parents=True)
    (d / "bin" / "lib" / "safe_fs.py").symlink_to(ROOT / "bin" / "lib" / "safe_fs.py")
    b = d / "bundles" / "openbao"
    b.mkdir(parents=True)
    (b / BUNDLE).write_bytes(b"bundle")
    (b / f"{BUNDLE}.sha256").write_text(f"{hashlib.sha256(b'bundle').hexdigest()}  {BUNDLE}\n")
    return d


def run_install(tmp_path, call, script_dir, state, b1=False):
    src = (ROOT / "install.sh").read_text()
    names = ["detect_update_python", "render_python_unit", "install_secrets_agent", "install_bundles", "_safe_fs"]
    body = "".join(shell_fn(src, n) for n in names)
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    bao_etc = tmp_path / "etc-bao"
    if b1:
        bao_etc.mkdir(exist_ok=True)
        (bao_etc / "agent.json").write_text("{}")
    script = ("set -eo pipefail\nok(){ echo \"OK $*\"; }\nwarn(){ echo \"WARN $*\"; }\ninfo(){ echo \"INFO $*\"; }\n"
              "err(){ echo \"ERR $*\" >&2; }\nsystemctl(){ :; }\n"
              f"SCRIPT_DIR={script_dir}\nSTATE_DIR={state}\nINSTALL_DIR={tmp_path / 'opt'}\n"
              f"SECRETS_UNIT_DIR={units}\nOPENBAO_ETC={bao_etc}\n{body}\n{call}\n")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


# (fonction, chemin planté sous l'état, nature du lien, ce qui doit exister ensuite)
INSTALL_CASES = [
    ("install_secrets_agent", "secrets-agent", "dir", "secrets-agent/agent.json"),
    ("install_secrets_agent", "secrets-agent/staged", "dir", "secrets-agent/staged"),
    ("install_secrets_agent", "secrets-agent/agent.json", "file", "secrets-agent/agent.json"),
    ("install_bundles", "secrets-agent", "dir", f"secrets-agent/staged/{BUNDLE}"),
    ("install_bundles", "secrets-agent/staged", "dir", f"secrets-agent/staged/{BUNDLE}"),
    ("install_bundles", f"secrets-agent/staged/{BUNDLE}", "file", f"secrets-agent/staged/{BUNDLE}"),
    ("install_bundles", f"secrets-agent/staged/{BUNDLE}.sha256", "file", f"secrets-agent/staged/{BUNDLE}.sha256"),
]


@pytest.mark.parametrize("fn,rel,kind,expect", INSTALL_CASES, ids=[f"{c[0]}:{c[1]}" for c in INSTALL_CASES])
def test_install_secrets_steps_never_follow_a_planted_link(tmp_path, fn, rel, kind, expect):
    state = tmp_path / "state"
    state.mkdir()
    script_dir = release(tmp_path)
    if kind == "dir":
        victim = plant_dir_link(state, rel, tmp_path)
        before = (stat.S_IMODE(victim.stat().st_mode), sorted(os.listdir(victim)))
    else:
        victim = plant_file_link(state, rel, tmp_path)
        before = (stat.S_IMODE(victim.stat().st_mode), victim.read_text())
    r = run_install(tmp_path, fn, script_dir, state)
    if kind == "dir":
        after = (stat.S_IMODE(victim.stat().st_mode), sorted(os.listdir(victim)))
    else:
        after = (stat.S_IMODE(victim.stat().st_mode), victim.read_text())
    assert after == before, (r.stdout, r.stderr)
    assert r.returncode == 0, r.stderr
    p = state / expect
    assert not p.is_symlink() and p.exists(), (r.stdout, r.stderr)
    for parent in p.relative_to(state).parents:
        assert not (state / parent).is_symlink()


def test_install_secrets_agent_marker_and_modes(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    r = run_install(tmp_path, "install_secrets_agent", release(tmp_path), state)
    assert r.returncode == 0, r.stderr
    d = state / "secrets-agent"
    assert json.loads((d / "agent.json").read_text())["installed"] == "2.9.0"
    assert stat.S_IMODE((d / "agent.json").stat().st_mode) == 0o644
    assert stat.S_IMODE(d.stat().st_mode) == 0o750 and stat.S_IMODE((d / "staged").stat().st_mode) == 0o750
    # la clé de la collection, pour la vérification à la pose, dans /opt (root)
    key = tmp_path / "opt" / "openbao" / "application-collection-cosign.pub"
    assert key.read_bytes() == (ROOT / "config" / "openbao" / "application-collection-cosign.pub").read_bytes()


def test_install_bundles_stages_the_openbao_bundle_through_the_helper(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    r = run_install(tmp_path, "install_bundles", release(tmp_path), state)
    assert r.returncode == 0, r.stderr
    staged = state / "secrets-agent" / "staged"
    assert (staged / BUNDLE).read_bytes() == b"bundle"
    assert stat.S_IMODE((staged / BUNDLE).stat().st_mode) == 0o640
    assert "could not give" not in r.stdout


@pytest.mark.parametrize("b1,waiting,queued", [
    (False, False, False),          # pas de B1 : rien à remettre
    (True, False, True),            # B1 posé : b1-policies demandé à l'agent
    (True, True, False),            # une demande de la console attend : jamais remplacée
])
def test_upgrade_queues_b1_policies_when_b1_is_installed(tmp_path, b1, waiting, queued):
    state = tmp_path / "state"
    (state / "secrets-agent").mkdir(parents=True)
    rp = state / "secrets-agent" / "request.json"
    if waiting:
        rp.write_text('{"op": "status"}')
    r = run_install(tmp_path, "install_secrets_agent", release(tmp_path), state, b1=b1)
    assert r.returncode == 0, r.stderr
    if queued:
        req = json.loads(rp.read_text())
        assert req == {"op": "b1-policies", "requested_by": "install.sh 2.9.0"}
        assert stat.S_IMODE(rp.stat().st_mode) == 0o600
        # la demande passe la liste fermée de l'agent
        assert set(req) <= hsa.COMMON | hsa.KEYS[req["op"]]
    elif waiting:
        assert rp.read_text() == '{"op": "status"}' and "already waiting" in r.stdout
    else:
        assert not rp.exists()


def test_upgrade_b1_policies_request_never_follows_a_link(tmp_path):
    state = tmp_path / "state"
    (state / "secrets-agent").mkdir(parents=True)
    victim = plant_file_link(state, "secrets-agent/request.json", tmp_path)
    before = victim.read_text()
    r = run_install(tmp_path, "install_secrets_agent", release(tmp_path), state, b1=True)
    assert r.returncode == 0 and victim.read_text() == before
    assert not (state / "secrets-agent" / "request.json").is_symlink()


@pytest.mark.parametrize("name", ["install_secrets_agent", "install_bundles"])
def test_secrets_steps_touch_the_state_only_through_the_helper(name):
    """Source check, as in 2.8.3: nothing under $STATE_DIR by a redirection,
    install(1), chown/chgrp/chmod, cp or mv."""
    body = shell_fn((ROOT / "install.sh").read_text(), name)
    offenders = [line.strip() for line in body.splitlines()
                 if "STATE_DIR" in line and not line.strip().startswith("#")
                 and not line.strip().startswith("_safe_fs")
                 and line.strip() != 'install -d -m 0750 "$STATE_DIR"'
                 and any(tok in line for tok in (">", "install ", "chown", "chgrp", "chmod", "cp ", "mv "))]
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# Agent des secrets (root) : journal, état, demande, réglages, paquet
# ---------------------------------------------------------------------------
import time  # noqa: E402

from test_secrets_agent_290 import env, bundle, request, install, hsa, CID  # noqa: E402,F401
import openbao_client as oc  # noqa: E402


def agent_log_name(env):
    return f"secrets-agent-{time.strftime('%Y%m%d-%H%M%S', time.localtime(env['clock']['t']))}.log"


def last_status(env):
    return json.loads((env["p"].state / hsa.STATUS).read_text())


def test_agent_log_link_is_never_written(env, chowns, tmp_path):
    """C1 (P1 of the review): a link planted under the predictable log name
    plus a request key holding a newline must not append a line to the
    victim (authorized_keys primitive)."""
    p = env["p"]
    p.logs.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "authorized_keys"
    victim.write_text("root key\n")
    (p.logs / agent_log_name(env)).symlink_to(victim)
    rc, st = request(env, op="status", **{"\nssh-ed25519 AAAAinjected console": 1})
    assert rc == 1 and st["code"] == "bad-request"
    assert victim.read_text() == "root key\n"
    assert ino(victim) not in chowns
    log = p.logs / st["log"]
    assert st["log"] != agent_log_name(env) and log.is_file() and not log.is_symlink()
    lines = log.read_text().splitlines()
    assert not any(line.startswith("ssh-ed25519") for line in lines), lines
    assert any("\\x0assh-ed25519 AAAAinjected" in line for line in lines), lines


def test_agent_messages_and_requester_are_one_line(env):
    rc, st = request(env, op="status", requested_by="ju\nssh-ed25519 AAAA\x1b[31m‮")
    assert rc == 0
    assert "\n" not in st["requested_by"] and "\x1b" not in st["requested_by"] and "‮" not in st["requested_by"]
    rc, st = request(env, op="status", **{"x\ny": 1})
    assert "\n" not in st["message"]


def test_agent_refuses_a_state_directory_that_is_a_link(env, chowns, tmp_path):
    """C1 second path: secrets-agent replaced by a link to another
    directory; nothing is read or created there, the link is removed."""
    p = env["p"]
    outside = tmp_path / "etc-somewhere"
    outside.mkdir()
    (outside / hsa.REQUEST).write_text(json.dumps({"op": "status", "id": "0123abcd"}))
    os.rename(p.state, tmp_path / "old-state")
    p.state.symlink_to(outside)
    assert env["agent"]().process() == 1
    assert sorted(x.name for x in outside.iterdir()) == [hsa.REQUEST]
    assert ino(outside) not in chowns
    assert not p.state.is_symlink()


def test_agent_state_directory_swapped_mid_operation(env, tmp_path):
    """The console swaps secrets-agent for a link while the agent works:
    the following status writes stay in the directory the agent opened."""
    p = env["p"]
    install(env)
    outside = tmp_path / "outside"
    outside.mkdir()
    host = env["host"]
    real_run = host.run

    def run(argv, **kw):
        if argv[:2] == ["systemctl", "is-active"] and not p.state.is_symlink():
            os.rename(p.state, tmp_path / "moved-state")
            p.state.symlink_to(outside)
        return real_run(argv, **kw)
    host.run = run
    (p.state / hsa.REQUEST).write_text(json.dumps({"op": "status", "id": "0123abcd"}))
    env["agent"]().process()
    assert list(outside.iterdir()) == []
    st = json.loads((tmp_path / "moved-state" / hsa.STATUS).read_text())
    assert st["state"] == "done" and st["op"] == "status"


def test_agent_request_planted_as_a_directory_is_set_aside(env):
    p = env["p"]
    (p.state / hsa.REQUEST).mkdir()
    assert env["agent"]().process() == 1
    assert last_status(env)["code"] == "bad-request"
    assert not (p.state / hsa.REQUEST).exists()          # l'unité de chemin ne boucle pas


def test_agent_status_link_is_replaced_not_followed(env, chowns, tmp_path):
    p = env["p"]
    victim = tmp_path / "agent-script.py"
    victim.write_text("root code\n")
    (p.state / hsa.STATUS).symlink_to(victim)
    rc, st = request(env, op="status")
    assert rc == 0 and victim.read_text() == "root code\n" and ino(victim) not in chowns
    m = os.stat(p.state / hsa.STATUS, follow_symlinks=False)
    assert stat.S_ISREG(m.st_mode) and stat.S_IMODE(m.st_mode) == 0o644
    log = p.logs / st["log"]
    assert stat.S_IMODE(log.stat().st_mode) == 0o640


def test_agent_does_not_read_console_settings_through_a_link(env, tmp_path):
    p = env["p"]
    secret = tmp_path / "root-only.json"
    secret.write_text(json.dumps({"mode": "a", "addr": "https://attacker.example", "console_id": CID}))
    (p.console_state / "secrets-settings.json").symlink_to(secret)
    assert env["agent"]().console_settings() == {}


def test_agent_status_cli_does_not_read_through_a_link(env, tmp_path, capsys, monkeypatch):
    p = env["p"]
    secret = tmp_path / "root-only.json"
    secret.write_text('{"root": "only"}')
    (p.state / hsa.STATUS).symlink_to(secret)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(p.console_state))
    assert hsa.main(["--status"]) == 0
    assert "root" not in capsys.readouterr().out


def test_agent_refuses_a_staging_directory_that_is_a_link(env, tmp_path):
    """M1: staged/ replaced by a link to a directory holding a valid bundle."""
    p = env["p"]
    outside = tmp_path / "outside"
    os.rename(p.state / "staged", outside)
    (p.state / "staged").symlink_to(outside)
    rc, st = request(env, op="b1-install", console_id=CID, bundle=env["bundle"].name)
    assert rc == 1 and st["code"] == "bundle-missing", st
    assert not env["host"].ran("podman", "load")
    assert (outside / env["bundle"].name).exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_agent_refuses_a_bundle_that_is_a_link(env, tmp_path, kind):
    p = env["p"]
    name = env["bundle"].name
    keep = tmp_path / "keep.tar.gz"
    os.rename(p.state / "staged" / name, keep)
    if kind == "symlink":
        (p.state / "staged" / name).symlink_to(keep)
    else:
        os.link(keep, p.state / "staged" / name)
    rc, st = request(env, op="b1-install", console_id=CID, bundle=name)
    assert rc == 1 and st["code"] == "bundle-missing", st
    assert not env["host"].ran("podman", "load")


def test_agent_work_directory_removed_after_a_failure(env):
    env["host"].digest = "sha256:" + "0" * 64            # échec après la copie et l'extraction
    rc, st = request(env, op="b1-install", console_id=CID, bundle=env["bundle"].name)
    assert rc == 1 and st["code"] == "bundle-digest-mismatch"
    assert not env["p"].work.exists()


# -- I6 / M10 : CA de l'agent, dépôt refusé en B1, demande validée -------------
def recording_agent(env, seen):
    srv = env["srv"]

    def client(addr, ca_pem=None):
        seen.append((addr, ca_pem))
        return oc.Client(srv.addr, timeout=2, retry_delays=(0.01,))
    a = env["agent"]()
    a.client = client
    return a


def test_agent_talks_to_b1_with_its_own_ca_only(env):
    """I6: the console can rewrite /etc/harvester-ops/openbao/ca.pem
    (store-secret-id); every call to B1 uses /etc/harvester-ops-openbao/ca.pem."""
    install(env)
    p = env["p"]
    b1_ca = (p.b1_etc / "ca.pem").read_text()
    (p.etc / "ca.pem").write_text("-----BEGIN CERTIFICATE-----\nconsole\n-----END CERTIFICATE-----\n")
    for op in ("b1-policies", "rotate-unseal-key"):
        seen = []
        (p.state / hsa.REQUEST).write_text(json.dumps({"op": op, "id": "0123abcd"}))
        env["clock"]["t"] += 86400
        assert recording_agent(env, seen).process() == 0, last_status(env)
        assert seen and all(ca == b1_ca for _, ca in seen), (op, seen)


def test_store_secret_id_refused_once_b1_is_installed(env):
    install(env)
    before = (env["p"].etc / "ca.pem").read_text()
    rc, st = request(env, op="store-secret-id", console_id=CID, addr=env["srv"].addr, role_id="rid-console-1",
                     wrap_token="s.x", ca_pem="-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    assert rc == 1 and st["code"] == "b1-installed"
    assert (env["p"].etc / "ca.pem").read_text() == before


@pytest.mark.parametrize("addr,ok", [
    ("http://openbao.example:8200", False), ("ftp://127.0.0.1:8200", False), ("https://", False),
    ("https://openbao.example:8200", True), ("http://127.0.0.1:8200", True), ("http://[::1]:8200", True),
    ("http://localhost:8200", True), ("https://ok.example/" + "a" * 600, False),
])
def test_store_secret_id_address_checked(env, addr, ok):
    assert hsa.check_addr(addr) == ok


@pytest.mark.parametrize("value", [None, 12, "", "a\nb", "x" * 300, "ok\x00"])
def test_unwrapped_secret_id_checked(env, value):
    with pytest.raises(hsa.AgentError):
        hsa.check_secret_value(value, "secret_id")


def test_unwrapped_secret_id_plain_value_accepted():
    assert hsa.check_secret_value("2a3b-4c5d-6e7f", "secret_id") == "2a3b-4c5d-6e7f"


# -- M3 : identifiants à leur groupe avant le renommage --------------------------
def test_credentials_get_their_group_before_the_rename(env, monkeypatch):
    events = []
    p = env["p"]
    env["host"].group_gid = lambda name: os.getgid()
    real_fchown, real_rename, real_replace = os.fchown, os.rename, os.replace
    monkeypatch.setattr(os, "fchown", lambda fd, u, g: events.append(("fchown", os.fstat(fd).st_ino)))
    monkeypatch.setattr(env["host"], "chown", lambda path, u, g: events.append(("chown", Path(path).name)))

    def ren(fn):
        def w(src, dst, *a, **k):
            fn(src, dst, *a, **k)
            if str(dst).endswith("secret-id"):
                events.append(("rename", os.stat(dst).st_ino))
        return w
    monkeypatch.setattr(os, "rename", ren(real_rename))
    monkeypatch.setattr(os, "replace", ren(real_replace))
    env["agent"]().write_credentials("rid-1", "sid-1", "acc-1")
    rn = [e for e in events if e[0] == "rename"]
    assert rn, events
    i = events.index(rn[0])
    assert ("fchown", rn[0][1]) in events[:i], events
    assert ("chown", "secret-id") not in events


# -- M7 : groupe du compte du conteneur, plus le groupe 0 -------------------------
def test_b1_uses_the_account_group(env):
    env["host"].gids = {}
    real = env["host"].run

    def run(argv, **kw):
        r = real(argv, **kw)
        if argv[:1] == ["useradd"]:
            env["host"].gids[argv[-1]] = 991
        return r
    env["host"].run = run
    env["host"].group_gid = lambda name: env["host"].gids.get(name)
    install(env)
    p = env["p"]
    assert "OPENBAO_GID=991" in (p.b1_etc / "unit.env").read_text()
    owned = {path: (u, g) for path, u, g in env["host"].chowns}
    for d in (p.b1_data / "data", p.b1_data / "audit", p.b1_etc / "tls", p.b1_etc / "openbao.hcl"):
        assert owned[str(d)] == (990, 991), d
    unit = (ROOT / "config" / "systemd" / hsa.UNIT).read_text()
    assert ':0"' not in unit and "gid=0" not in unit and "OPENBAO_GID" in unit


def test_agent_unit_hardening():
    unit = (ROOT / "config" / "systemd" / "harvester-secrets-agent.service").read_text()
    assert "ProtectHome=read-only" in unit and "PrivateTmp=yes" in unit


# -- I2 : signature vérifiée à la pose avec la clé de /opt ------------------------
@pytest.mark.parametrize("key,code", [("absent", "bundle-unsigned"), ("other", "bundle-signature-bad")])
def test_agent_checks_the_bundle_signature_with_the_host_key(env, tmp_path, key, code):
    p = env["p"]
    if key == "absent":
        p.cosign_key.unlink()
    else:
        subprocess.run(["openssl", "genrsa", "-out", str(tmp_path / "k"), "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", str(tmp_path / "k"), "-pubout", "-out", str(p.cosign_key)],
                       check=True, capture_output=True)
    rc, st = request(env, op="b1-install", console_id=CID, bundle=env["bundle"].name)
    assert rc == 1 and st["code"] == code, st
    assert not env["host"].ran("podman", "load")
    assert not p.work.exists()
