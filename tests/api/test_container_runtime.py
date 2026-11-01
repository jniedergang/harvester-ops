"""Keep Docker and Podman image stores, service units and update rollback aligned."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin/lib"))
import container_runtime as cr
from test_self_update_182 import agent_env, keys, FakeHost, request, run_agent


def function(name):
    source = (ROOT / "install.sh").read_text()
    start = source.index(name + "() {\n")
    return source[start:source.index("\n}\n", start) + 3]


def available(monkeypatch, names):
    original = shutil.which
    monkeypatch.setattr(cr.shutil, "which", lambda n: "/usr/bin/" + n if n in names
                        else None if n in cr.RUNTIMES else original(n))
    monkeypatch.delenv(cr.OVERRIDE, raising=False)


def unit_files(tmp_path, main, dropins=()):
    if main is not None:
        (tmp_path / "harvester-ops.service").write_text(main)
    if dropins:
        d = tmp_path / "harvester-ops.service.d"
        d.mkdir()
        for i, text in enumerate(dropins):
            (d / f"{i:02d}.conf").write_text(text)


PODMAN_UNIT = "ExecStop=/usr/bin/podman stop -t 600 harvester-ops\n"
DOCKER_UNIT = "ExecStop=/usr/bin/docker stop -t 600 harvester-ops\n"


# installed, override, main unit, drop-ins, expected runtime or error pattern
SELECTION_CASES = {
    "fresh, podman only": (["podman"], None, None, (), "podman"),
    "fresh, docker only": (["docker"], None, None, (), "docker"),
    "fresh, both: podman": (["podman", "docker"], None, None, (), "podman"),
    "unit docker, both installed": (["podman", "docker"], None, DOCKER_UNIT, (), "docker"),
    "unit podman, both installed": (["podman", "docker"], None, PODMAN_UNIT, (), "podman"),
    "absolute path elsewhere": (["podman", "docker"], None, "ExecStop=/usr/local/bin/docker stop x\n", (), "docker"),
    "ignored-failure prefix": (["podman", "docker"], None, "ExecStop=-/usr/bin/docker stop x\n", (), "docker"),
    "drop-in switches runtime": (["podman", "docker"], None, PODMAN_UNIT,
                                 ("[Service]\nExecStop=\nExecStop=/usr/bin/docker stop x\n",), "docker"),
    "drop-in only resets: detection": (["docker"], None, PODMAN_UNIT, ("[Service]\nExecStop=\n",), "docker"),
    "later drop-in wins": (["podman", "docker"], None, PODMAN_UNIT,
                           ("[Service]\nExecStop=\nExecStop=/usr/bin/docker stop x\n",
                            "[Service]\nExecStop=\nExecStop=/usr/bin/podman stop x\n"), "podman"),
    "override beats unit": (["podman", "docker"], "docker", PODMAN_UNIT, (), "docker"),
    "error: invalid override": (["podman"], "bad;command", None, (), "must be podman or docker"),
    "error: override not installed": (["podman"], "docker", None, (), "docker is not installed"),
    "error: unit runtime not installed": (["podman"], None, DOCKER_UNIT, (), "refusing to switch"),
    "error: no runtime at all": ([], None, None, (), "Neither podman nor docker"),
    "error: unreadable ExecStop": (["podman", "docker"], None, "ExecStop=/usr/bin/crun stop x\n", (),
                                   "Cannot identify the installed runtime"),
    "error: ExecStop in a drop-in unreadable": (["podman", "docker"], None, PODMAN_UNIT,
                                                ("[Service]\nExecStop=\nExecStop=/bin/sh -c stop\n",),
                                                "Cannot identify the installed runtime"),
}


@pytest.mark.parametrize("installed,override,main,dropins,expected", SELECTION_CASES.values(),
                         ids=SELECTION_CASES.keys())
def test_runtime_selection(tmp_path, monkeypatch, installed, override, main, dropins, expected):
    available(monkeypatch, installed)
    if override is not None:
        monkeypatch.setenv(cr.OVERRIDE, override)
    unit_files(tmp_path, main, dropins)
    if expected in cr.RUNTIMES:
        assert cr.select_runtime(tmp_path) == expected
    else:
        with pytest.raises(cr.RuntimeError, match=expected):
            cr.select_runtime(tmp_path)


# executable found on PATH, expected error (None = rendered)
EXECUTABLE_CASES = {
    "plain": ("/usr/bin/docker", None),
    "plus and dots": ("/opt/c++/v1.2_x/docker", None),
    "relative": ("bin/docker", "systemd-safe"),
    "space": ("/opt/my tools/docker", "systemd-safe"),
    "percent": ("/opt/%h/docker", "systemd-safe"),
    "quote": ("/opt/it's/docker", "systemd-safe"),
    "dollar": ("/opt/$HOME/docker", "systemd-safe"),
    "backslash": ("/opt/a\\b/docker", "systemd-safe"),
    "not found": (None, "systemd-safe"),
}


@pytest.mark.parametrize("path,error", EXECUTABLE_CASES.values(), ids=EXECUTABLE_CASES.keys())
def test_render_accepts_only_a_safe_executable(monkeypatch, path, error):
    monkeypatch.setattr(cr.shutil, "which", lambda n: path)
    template = (ROOT / "config/systemd/harvester-ops.service").read_text()
    if error is None:
        assert f"ExecStop={path} stop -t 600" in cr.render_service(template, "docker")
    else:
        with pytest.raises(cr.RuntimeError, match=error):
            cr.render_service(template, "docker")


@pytest.mark.parametrize("runtime", cr.RUNTIMES)
def test_unit_has_matching_dependencies_commands_and_user_identity(tmp_path, monkeypatch, runtime):
    available(monkeypatch, cr.RUNTIMES)
    template = (ROOT / "config/systemd/harvester-ops.service").read_text()
    unit = cr.render_service(template, runtime)
    assert f"ExecStop=/usr/bin/{runtime} stop -t 600" in unit
    assert "TimeoutStopSec=630s" in unit
    assert "HARVESTER_OPS_REPLICATION_FOLLOW" in unit
    assert "podman.socket" not in unit
    if runtime == "docker":
        assert "Requires=docker.service" in unit
        assert "After=network-online.target docker.service" in unit
        assert "/usr/bin/podman" not in unit and "--passwd-entry" not in unit
        assert f"-v {cr.NSS_DIR}/passwd:/etc/passwd:ro,Z" in unit
        assert f"-v {cr.NSS_DIR}/group:/etc/group:ro,Z" in unit
        assert f"RuntimeDirectory={cr.NSS_NAME}\nRuntimeDirectoryMode=0755\n" in unit
        assert ".nss" not in unit
        assert "ExecStartPre=/usr/local/bin/harvester-container-user.sh /usr/bin/docker" in unit
    else:
        assert "Requires=docker.service" not in unit
        assert "--passwd-entry" in unit
        assert "harvester-container-user.sh" not in unit


def test_real_upgrade_function_keeps_docker_for_load_and_unit(tmp_path):
    release = tmp_path / "release"
    for path in ["bin/lib", "config/systemd", "images"]:
        (release / path).mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "bin/lib/container_runtime.py", release / "bin/lib/container_runtime.py")
    shutil.copy(ROOT / "config/systemd/harvester-ops.service", release / "config/systemd/harvester-ops.service")
    (release / "VERSION").write_text("9.9.9\n")
    (release / "images/harvester-ops-ui.tar").write_text("test-image")
    units = tmp_path / "units"
    units.mkdir()
    (units / "harvester-ops.service").write_text("ExecStop=/usr/bin/docker stop harvester-ops\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in cr.RUNTIMES:
        executable = tools / name
        executable.write_text('#!/bin/bash\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$CALLS"\n')
        executable.chmod(0o755)
    script = '\n'.join(function(n) for n in ["detect_container_runtime", "render_container_service", "install_web_ui", "upgrade"])
    script += '\n' + '''require_root() { :; }
detect_update_python() { echo "$HOST_PYTHON"; }
info() { :; }; ok() { :; }; warn() { :; }; err() { echo "$*" >&2; }
install_scripts() { :; }; setup_service_account() { :; }; install_bundles() { :; }
install_update_agent() { :; }; install_uninstaller() { :; }
CONTAINER_RUNTIME=""
upgrade
'''
    environment = {**os.environ, "PATH":str(tools)+os.pathsep+os.environ["PATH"],
                   "CALLS":str(tmp_path / "calls"), "HOST_PYTHON":sys.executable,
                   "SCRIPT_DIR":str(release), "SYSTEMD_DIR":str(units)}
    environment.pop(cr.OVERRIDE, None)
    result = subprocess.run(["bash", "-ec", script], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "calls").read_text().startswith("docker load -i ")
    unit = (units / "harvester-ops.service").read_text()
    assert "Requires=docker.service" in unit and "--passwd-entry" not in unit
    assert f"ExecStop={tools}/docker stop" in unit


def test_upgrade_resolves_the_runtime_before_writing_anything(tmp_path):
    """A host whose unit runs a runtime that is gone must stop the upgrade
    before scripts, image or unit are touched."""
    release = tmp_path / "release"
    (release / "bin/lib").mkdir(parents=True)
    shutil.copy(ROOT / "bin/lib/container_runtime.py", release / "bin/lib/container_runtime.py")
    (release / "VERSION").write_text("9.9.9\n")
    units = tmp_path / "units"
    units.mkdir()
    (units / "harvester-ops.service").write_text(DOCKER_UNIT)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "podman").write_text("#!/bin/sh\nexit 0\n")
    (tools / "podman").chmod(0o755)
    script = "\n".join(function(n) for n in ["detect_container_runtime", "render_container_service",
                                             "install_web_ui", "upgrade"])
    script += """
require_root() { :; }
detect_update_python() { echo "$HOST_PYTHON"; }
info() { :; }; ok() { :; }; warn() { :; }; err() { echo "$*" >&2; }
for f in install_scripts setup_service_account install_bundles install_update_agent install_uninstaller; do
    eval "$f() { echo $f >> \\"\\$CALLS\\"; }"
done
CONTAINER_RUNTIME=""
upgrade
"""
    environment = {**os.environ, "PATH": f"{tools}:/usr/bin:/bin", "CALLS": str(tmp_path / "calls"),
                   "HOST_PYTHON": sys.executable, "SCRIPT_DIR": str(release), "SYSTEMD_DIR": str(units)}
    environment.pop(cr.OVERRIDE, None)
    result = subprocess.run(["bash", "-c", script], env=environment, capture_output=True, text=True)
    assert result.returncode != 0
    assert "refusing to switch" in result.stderr
    assert not (tmp_path / "calls").exists()
    assert (units / "harvester-ops.service").read_text() == DOCKER_UNIT


@pytest.mark.parametrize("install_rc", [0, 3])
def test_agent_keeps_docker_for_install_backup_and_rollback(agent_env, keys, monkeypatch, install_rc):
    ag, dirs = agent_env
    available(monkeypatch, cr.RUNTIMES)
    unit = dirs["units"] / "harvester-ops.service"
    previous = "ExecStop=/usr/bin/docker stop harvester-ops\n"
    unit.write_text(previous)
    request(dirs, keys)

    class Host(FakeHost):
        def __call__(self, argv, **kw):
            if argv[0] == "bash":
                assert kw["env"][cr.OVERRIDE] == "docker"
            return super().__call__(argv, **kw)

    host = Host(dirs, "1.82.0", install_rc=install_rc)
    rc, status = run_agent(ag, host, lambda run: (True, (dirs["opt"] / "VERSION").read_text().strip()))
    assert status["state"] == ("done" if install_rc == 0 else "rolled-back")
    assert ["docker", "tag", ag.IMAGE, ag.PREVIOUS] in host.calls
    assert not any(c[0] == "podman" for c in host.calls)
    if install_rc:
        assert ["docker", "tag", ag.PREVIOUS, ag.IMAGE] in host.calls
        assert unit.read_text() == previous


IMAGE_ACCOUNTS = """case "$*" in
  *"cat /etc/passwd; echo harvester-ops-group-file; cat /etc/group")
    printf 'root:x:0:0::/root:/bin/sh\\nharvops:x:1001:100::/opt:/bin/sh\\nold:x:988:981::/old:/bin/sh\\n'
    echo harvester-ops-group-file
    printf 'root:x:0:\\nusers:x:100:\\nold:x:981:\\n';;
  *) exit 99;;
esac
"""


def nss_host(tmp_path, docker_body=IMAGE_ACCOUNTS):
    """A fake host for bin/harvester-container-user.sh: the account ids, a
    runtime that prints the image accounts, and the root-owned runtime
    directory systemd would have created (owned by the test user here)."""
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "id").write_text('#!/bin/sh\ncase "$1" in -u) echo 988;; -g) echo 981;; *) exec /usr/bin/id "$@";; esac\n')
    docker = tools / "docker"
    docker.write_text("#!/bin/sh\n" + docker_body)
    for path in tools.iterdir():
        path.chmod(0o755)
    nss = tmp_path / "run" / cr.NSS_NAME
    nss.mkdir(parents=True, mode=0o755)
    nss.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
           "HARVESTER_OPS_NSS_DIR": str(nss), "HARVESTER_OPS_STATE_DIR": str(state)}
    return docker, nss, state, env


def run_nss(docker, env):
    return subprocess.run(["bash", str(ROOT / "bin/harvester-container-user.sh"), str(docker)],
                          env=env, text=True, capture_output=True)


@pytest.mark.parametrize("fails", [False, True])
def test_docker_nss_preserves_image_accounts_and_handles_failure(tmp_path, fails):
    docker, nss, _, env = nss_host(tmp_path, "exit 1\n" if fails else IMAGE_ACCOUNTS)
    (nss / "passwd").write_text("previous-passwd\n")
    (nss / "group").write_text("previous-group\n")
    result = run_nss(docker, env)
    assert not list(nss.glob(".build.*"))
    if fails:
        assert result.returncode != 0
        assert (nss / "passwd").read_text() == "previous-passwd\n"
        assert (nss / "group").read_text() == "previous-group\n"
    else:
        assert result.returncode == 0, result.stderr
        passwd = (nss / "passwd").read_text()
        assert "root:x:0:" in passwd and "harvops:x:1001:" in passwd
        assert "harvester-ops:x:988:981:" in passwd and "old:x:988:" not in passwd
        assert "harvester-ops-group-file" not in passwd
        group = (nss / "group").read_text()
        assert group == "root:x:0:\nusers:x:100:\nharvester-ops:x:981:\n"
        for name in ("passwd", "group"):
            assert (nss / name).stat().st_mode & 0o777 == 0o644


def test_docker_nss_reads_the_image_once(tmp_path):
    body = 'echo "$*" >> "$CALLS"\n' + IMAGE_ACCOUNTS
    docker, nss, _, env = nss_host(tmp_path, body)
    env["CALLS"] = str(tmp_path / "calls")
    assert run_nss(docker, env).returncode == 0
    assert len((tmp_path / "calls").read_text().splitlines()) == 1


def test_docker_nss_ignores_a_link_planted_in_the_state_directory(tmp_path):
    """The console account owns the state directory. A link planted where
    the account files used to live (2.0.2 before review) must not lead root
    to write or chown anything: the target stays untouched and the files
    land in the runtime directory only."""
    docker, nss, state, env = nss_host(tmp_path)
    target = tmp_path / "etc"
    target.mkdir(mode=0o755)
    (target / "passwd").write_text("root:x:0:0:root:/root:/bin/bash\n")
    before = {p.name: (p.read_text(), p.stat().st_mode) for p in target.iterdir()}
    mode = target.stat().st_mode
    (state / ".nss").symlink_to(target)
    for name in ("passwd", "group"):
        (state / f".nss-{name}").symlink_to(target / "passwd")
    result = run_nss(docker, env)
    assert result.returncode == 0, result.stderr
    assert {p.name: (p.read_text(), p.stat().st_mode) for p in target.iterdir()} == before
    assert target.stat().st_mode == mode
    assert sorted(p.name for p in state.iterdir()) == [".nss", ".nss-group", ".nss-passwd"]
    assert "harvester-ops:x:988:" in (nss / "passwd").read_text()


@pytest.mark.parametrize("plant", ["link-dir", "link-file", "group-writable", "missing"])
def test_docker_nss_refuses_a_directory_it_does_not_hold_alone(tmp_path, plant):
    docker, nss, _, env = nss_host(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "passwd").write_text("keep\n")
    if plant == "link-dir":
        nss.rmdir()
        nss.symlink_to(target)
    elif plant == "link-file":
        (nss / "passwd").symlink_to(target / "passwd")
    elif plant == "group-writable":
        nss.chmod(0o775)
    else:
        nss.rmdir()
    result = run_nss(docker, env)
    assert (target / "passwd").read_text() == "keep\n"
    if plant == "link-file":
        # rename(2) replaces the link itself; the target is never written
        assert result.returncode == 0, result.stderr
        assert not (nss / "passwd").is_symlink()
    else:
        assert result.returncode != 0
        assert "must be a directory" in result.stderr


def test_docker_nss_script_never_hands_files_to_the_console_account():
    script = (ROOT / "bin/harvester-container-user.sh").read_text()
    code = "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))
    for forbidden in ("install ", "chown", "STATE_DIR", "/var/lib/harvester-ops/.nss"):
        assert forbidden not in code
