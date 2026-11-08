"""2.9.0 tâche 6 : installation de l'agent des secrets et unités (install.sh,
uninstall.sh, config/systemd/).

`install_secrets_agent` et `install_bundles` sont joués dans un bac à sable
(systemctl et chown factices) ; la commande de l'unité de B1 est rendue par
/bin/sh avec un faux podman, pour lire les arguments qu'il recevrait.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UNITS = ROOT / "config" / "systemd"


def function(name, script="install.sh"):
    source = (ROOT / script).read_text()
    start = source.index(name + "() {\n")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


@pytest.fixture
def sandbox(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("systemctl", "chown"):
        (bindir / tool).write_text(f'#!/bin/bash\necho "{tool} $*" >> {log}\n')
        (bindir / tool).chmod(0o755)
    src = tmp_path / "src"
    (src / "config").mkdir(parents=True)
    subprocess.run(["cp", "-r", str(ROOT / "config" / "systemd"), str(ROOT / "config" / "openbao"),
                    str(src / "config")], check=True)
    (src / "VERSION").write_text("2.9.0\n")
    (src / "bin" / "lib").mkdir(parents=True)
    (src / "bin" / "lib" / "safe_fs.py").symlink_to(ROOT / "bin" / "lib" / "safe_fs.py")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    return {"tmp": tmp_path, "src": src, "log": log, "env": env}


def run_fn(sb, body, unit_dir):
    pre = f"""
SCRIPT_DIR={shlex.quote(str(sb['src']))}
INSTALL_DIR={shlex.quote(str(sb['tmp'] / 'opt'))}
STATE_DIR={shlex.quote(str(sb['tmp'] / 'state'))}
SECRETS_UNIT_DIR={shlex.quote(str(unit_dir))}
ok() {{ echo "OK $*"; }}
warn() {{ echo "WARN $*"; }}
err() {{ echo "ERR $*" >&2; }}
detect_update_python() {{ echo {shlex.quote(sys.executable)}; }}
"""
    script = pre + function("_safe_fs") + function("render_python_unit") + function("install_secrets_agent") + function("install_bundles") \
        + body
    return subprocess.run(["/bin/bash", "-c", script], env=sb["env"], capture_output=True, text=True)


def test_install_secrets_agent(sandbox):
    units = sandbox["tmp"] / "units"
    r = run_fn(sandbox, "install_secrets_agent", units)
    assert r.returncode == 0, r.stderr
    svc = (units / "harvester-secrets-agent.service").read_text()
    assert f"ExecStart={sys.executable} /usr/local/bin/harvester-secrets-agent.py" in svc
    assert (units / "harvester-secrets-agent.path").read_text() == (UNITS / "harvester-secrets-agent.path").read_text()
    assert (units / "harvester-secrets-rotate.timer").exists()
    assert not (units / "harvester-ops-openbao.service").exists()        # posée par l'agent à l'installation de B1
    opt = sandbox["tmp"] / "opt" / "openbao"
    assert (opt / "harvester-ops-openbao.service").exists() and (opt / "openbao.hcl.tpl").exists()
    agent = json.loads((sandbox["tmp"] / "state" / "secrets-agent" / "agent.json").read_text())
    assert agent["installed"] == "2.9.0"
    assert (sandbox["tmp"] / "state" / "secrets-agent" / "staged").is_dir()
    calls = sandbox["log"].read_text()
    assert "systemctl enable --now harvester-secrets-agent.path harvester-secrets-rotate.timer" in calls
    assert "harvester-ops-openbao" not in calls                          # jamais démarrée par l'installation


def test_install_secrets_agent_refreshes_an_installed_b1_unit_without_restart(sandbox):
    units = sandbox["tmp"] / "units"
    units.mkdir()
    (units / "harvester-ops-openbao.service").write_text("old")
    assert run_fn(sandbox, "install_secrets_agent", units).returncode == 0
    assert (units / "harvester-ops-openbao.service").read_text() == (UNITS / "harvester-ops-openbao.service").read_text()
    assert "restart" not in sandbox["log"].read_text()


@pytest.mark.parametrize("good", [True, False])
def test_install_bundles_stages_a_checked_openbao_bundle(sandbox, good):
    b = sandbox["src"] / "bundles" / "openbao"
    b.mkdir(parents=True)
    name = "harvester-ops-openbao-2.7.2-2.7.2-5.4.tar.gz"
    (b / name).write_bytes(b"bundle")
    digest = __import__("hashlib").sha256(b"bundle" if good else b"other").hexdigest()
    (b / f"{name}.sha256").write_text(f"{digest}  {name}\n")
    r = run_fn(sandbox, "install_bundles", sandbox["tmp"] / "units")
    staged = sandbox["tmp"] / "state" / "secrets-agent" / "staged" / name
    assert r.returncode == 0 and staged.exists() == good
    assert ("checksum mismatch" in r.stdout) != good


def test_install_and_upgrade_install_the_agent():
    src = (ROOT / "install.sh").read_text()
    up = function("upgrade")
    assert "install_secrets_agent" in up and up.index("install_update_agent") < up.index("install_secrets_agent")
    main = function("main")
    assert re.search(r"install_update_agent\n\s+install_secrets_agent", main)
    assert src.count("install_secrets_agent") == 3


# ---------------------------------------------------------------------------
# Unités
# ---------------------------------------------------------------------------
def unit(name):
    return (UNITS / name).read_text()


def test_agent_units():
    p = unit("harvester-secrets-agent.path")
    assert "PathExists=/var/lib/harvester-ops/secrets-agent/request.json" in p
    assert "Unit=harvester-secrets-agent.service" in p
    s = unit("harvester-secrets-agent.service")
    assert "Type=oneshot" in s and "ExecStart=/usr/bin/python3 /usr/local/bin/harvester-secrets-agent.py" in s
    assert "UMask=0077" in s
    t = unit("harvester-secrets-rotate.timer")
    assert "OnCalendar=hourly" in t and "Persistent=true" in t and "Unit=harvester-secrets-agent.service" in t


def exec_start(text):
    m = re.search(r"^ExecStart=/bin/sh -c '(.*?)'\n", text, re.M | re.S)
    return m.group(1).replace("\\\n", " ").replace("$$", "$")


@pytest.mark.parametrize("prev,boot", [(False, False), (True, True)])
def test_b1_unit_podman_arguments(tmp_path, prev, boot):
    """La commande de l'unité, rendue par /bin/sh avec un faux podman."""
    fake = tmp_path / "podman"
    fake.write_text('#!/bin/bash\nfor a in "$@"; do echo "$a"; done\n')
    fake.chmod(0o755)
    cmd = exec_start(unit("harvester-ops-openbao.service")).replace("/usr/bin/podman", str(fake))
    if not boot:
        cmd = cmd.replace("/run/hops-openbao-boot ]", f"{tmp_path}/nope ]")
    else:
        cmd = cmd.replace("[ -d /run/hops-openbao-boot ]", "true")
    env = {"OPENBAO_IMAGE": "localhost/harvester-ops-openbao:current", "OPENBAO_UID": "990",
           "OPENBAO_PREV_KEY": "1" if prev else "0", "PATH": os.environ["PATH"]}
    args = subprocess.run(["/bin/sh", "-c", cmd], env=env, capture_output=True, text=True, check=True).stdout.split("\n")
    joined = " ".join(args)
    for part in ("run --rm --name harvester-ops-openbao --network host --read-only --tmpfs /tmp",
                 "--tmpfs /run/secrets:mode=0755", "--memory 512m --memory-swap 512m", "--user 990:0",
                 "--entrypoint /usr/bin/bao", "--secret hops-openbao-unseal,uid=990,gid=0,mode=0400",
                 "--cap-drop=all --security-opt=no-new-privileges",          # revue M7 des tâches 5-7
                 "localhost/harvester-ops-openbao:current server -config=/openbao/config/openbao.hcl"):
        assert part in joined, part
    assert ("hops-openbao-unseal-prev,uid=990" in joined) == prev
    assert ("/run/hops-openbao-boot:/run/hops-openbao-boot:ro,Z" in joined) == boot
    assert "-p" not in args and "--publish" not in args and "0.0.0.0" not in joined


def test_b1_unit_order_and_independence():
    u = unit("harvester-ops-openbao.service")
    assert "Before=harvester-ops.service" in u
    assert "Requires=" not in u and "BindsTo=" not in u
    assert "EnvironmentFile=/etc/harvester-ops-openbao/unit.env" in u
    assert "Restart=on-failure" in u and "ExecStop=/usr/bin/podman stop" in u
    assert "harvester-ops-openbao" not in unit("harvester-ops.service")     # la console ne l'exige pas


def test_uninstall_removes_units_and_purges_b1():
    u = (ROOT / "uninstall.sh").read_text()
    for name in ("harvester-secrets-agent.path", "harvester-secrets-agent.service", "harvester-secrets-rotate.timer",
                 "harvester-ops-openbao.service"):
        assert f"/etc/systemd/system/{name}" in u
    purge = u[u.index('if [[ "$PURGE" == "1" ]]'):]
    assert "/etc/harvester-ops-openbao" in purge and "/var/lib/harvester-ops-openbao" in purge
    assert "userdel harvester-ops-openbao" in purge and "podman secret rm hops-openbao-unseal" in purge
    head = u[:u.index('if [[ "$PURGE" == "1" ]]')]
    assert "rm -rf /etc/harvester-ops-openbao" not in head and "rm -rf /var/lib/harvester-ops-openbao" not in head


def test_scripts_parse():
    for s in ("install.sh", "uninstall.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / s)]).returncode == 0
