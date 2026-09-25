"""v1.48.1 : au démarrage, ne relancer que les VMs qui tournaient à l'arrêt.

La v1.9.0 le faisait déjà, mais en lisant la seule runStrategy :
- une VM éteinte depuis son propre système garde RerunOnFailure (le défaut
  de Harvester) sans VMI active : elle était annotée, puis relancée ;
- une VM en Manual qui tournait était arrêtée, sa stratégie rétablie, mais
  rétablir Manual ne démarre rien : elle restait éteinte.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
COMMON = ROOT / "bin" / "lib" / "common.sh"


def bash(script, env=None):
    return subprocess.run(["bash", "-c", f'source "{COMMON}" >/dev/null 2>&1; {script}'],
                          capture_output=True, text=True, env=env or os.environ.copy())


@pytest.mark.parametrize("rs, phase, running", [
    ("Always", "Running", True),
    ("Always", "", True),                     # KubeVirt la recrée : intention de marche
    ("RerunOnFailure", "Running", True),
    ("RerunOnFailure", "Succeeded", False),   # éteinte depuis le système invité
    ("RerunOnFailure", "", False),
    ("Manual", "Running", True),
    ("Manual", "", False),
    ("Manual", "Failed", False),
    ("Halted", "", False),
    ("", "Running", False),                   # spec.running hérité : laissé tel quel
    ("RerunOnFailure", "Scheduling", True),   # en cours de placement : voulue en marche
])
def test_was_running(rs, phase, running):
    r = bash(f'vm_was_running "{rs}" "{phase}" && echo yes || echo no')
    assert r.stdout.strip() == ("yes" if running else "no"), r.stderr


def test_a_manual_vm_is_started_through_the_subresource(tmp_path):
    log = tmp_path / "argv"
    fake = tmp_path / "kubectl"
    fake.write_text(f'#!/bin/bash\necho "$@" >> "{log}"\ncat >> "{log}.stdin"\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    r = bash('KUBECONFIG_PATH=/k DRY_RUN=0 vm_start_subresource apps web', env)
    assert r.returncode == 0, r.stderr
    argv = log.read_text()
    assert "replace --raw /apis/subresources.kubevirt.io/v1/namespaces/apps/virtualmachines/web/start -f -" in argv
    assert (tmp_path / "argv.stdin").read_text().strip() == "{}"


def test_dry_run_starts_nothing(tmp_path):
    log = tmp_path / "argv"
    fake = tmp_path / "kubectl"
    fake.write_text(f'#!/bin/bash\necho "$@" >> "{log}"\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    bash('KUBECONFIG_PATH=/k DRY_RUN=1 vm_start_subresource apps web', env)
    assert not log.exists()


def test_the_scripts_use_them():
    stop = (ROOT / "bin" / "harvester-shutdown.sh").read_text()
    body = stop[stop.index("_stop_one_sync()"):].split("\n    }\n", 1)[0]
    assert 'get vmi "$name"' in body and 'vm_was_running "$current_rs" "$vmi_phase"' in body
    start = (ROOT / "bin" / "harvester-startup.sh").read_text()
    body = start[start.index("_start_one_sync()"):].split("\n    }\n", 1)[0]
    assert '"$target_rs" == "Manual"' in body and "vm_start_subresource" in body


def test_the_stop_summary_counts_only_the_vms_it_stopped(tmp_path):
    """Vu sur harvlab2 : « 3 VMs stopped » alors que l'une était déjà éteinte."""
    fake = tmp_path / "kubectl"
    fake.write_text('#!/bin/bash\nprintf "RerunOnFailure\\n\\nManual\\n\\n"\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    assert bash("KUBECONFIG_PATH=/k count_vms_to_resume", env).stdout.strip() == "2"
    stop = (ROOT / "bin" / "harvester-shutdown.sh").read_text()
    assert "VMs stopped (ordered)" not in stop and "already off" in stop
