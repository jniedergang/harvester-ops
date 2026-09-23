"""v1.44.10 : un nœud isolé est Ready.

Constaté sur le banc harvlab (3 nœuds) en filmant l'extinction puis le
redémarrage : les trois nœuds étaient revenus, mais le démarrage restait
à « 1/3 Ready » et attendait ses quinze minutes de délai.

Cause : le script lisait la colonne STATUS de `kubectl get nodes` et la
comparait à « Ready ». L'extinction isole tous les nœuds ; un nœud isolé
s'y affiche « Ready,SchedulingDisabled », donc pas compté. La levée de
l'isolement vient APRÈS cette attente : le démarrage patientait pour une
condition qu'il était seul à pouvoir remplir. Un cluster mono-nœud ne le
montrait pas, Harvester refusant d'isoler son dernier nœud.

Le même défaut, inversé, faisait dire au pré-contrôle de l'extinction
qu'un nœud en maintenance n'était « pas Ready ».
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMMON = ROOT / "bin" / "lib" / "common.sh"


def run_helper(tmp_path, fn, kubectl_out):
    """Appelle une fonction de common.sh avec un faux kubectl."""
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "kubectl").write_text("#!/bin/sh\ncat <<'EOF'\n" + kubectl_out + "EOF\n")
    (fake / "kubectl").chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", NO_COLOR="1")
    r = subprocess.run(["bash", "-c", f'source "{COMMON}"; {fn}'], env=env,
                       capture_output=True, text=True, check=True, timeout=60)
    return r.stdout.strip()


# Ce que rend le jsonpath du helper sur le banc après une extinction : trois
# nœuds dont la condition Ready vaut True, deux d'entre eux isolés (ce que
# le jsonpath ne voit pas, et c'est voulu).
AFTER_SHUTDOWN = "harvlab-n1 True\nharvlab-n2 True\nharvlab-n3 True\n"


def test_cordoned_nodes_are_counted_as_ready(tmp_path):
    assert run_helper(tmp_path, "ready_nodes_count", AFTER_SHUTDOWN) == "3"


def test_a_node_whose_condition_is_not_true_is_not_ready(tmp_path):
    out = "n1 True\nn2 False\nn3 Unknown\n"
    assert run_helper(tmp_path, "ready_nodes_count", out) == "1"
    assert run_helper(tmp_path, "not_ready_nodes", out).split() == ["n2", "n3"]


def test_a_node_without_ready_condition_is_not_ready(tmp_path):
    """Un nœud qui vient de rejoindre n'a parfois pas encore de condition."""
    out = "n1 True\nn2\n"
    assert run_helper(tmp_path, "ready_nodes_count", out) == "1"
    assert run_helper(tmp_path, "not_ready_nodes", out).split() == ["n2"]


def test_no_script_reads_the_status_column_any_more():
    """La colonne STATUS est faite pour les yeux : « Ready,SchedulingDisabled »,
    « NotReady », etc. Les scripts passent par le helper."""
    for script in list((ROOT / "bin").glob("*.sh")) + [COMMON]:
        text = script.read_text()
        assert '$2 == "Ready"' not in text, script.name
        assert '$2 != "Ready"' not in text, script.name
