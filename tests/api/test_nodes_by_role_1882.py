"""v1.88.2 : l'arrêt gracieux s'arrêtait sans un mot quand le dernier nœud
déclaré n'était pas un plan de contrôle.

Vu en réel sur le banc bmcfg juste après la jonction d'un worker :
`nodes_by_role` finissait par `[[ ... ]] && echo`, donc rendait 1 quand la
dernière entrée n'avait pas le rôle demandé ; sous `set -eo pipefail`,
`cp_node=$(nodes_by_role control-plane | head -n1 ...)` tuait le script à
l'étape « Snapshot etcd », sans message (aucun `trap ERR`).
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
COMMON = ROOT / "bin" / "lib" / "common.sh"

CP = "n1|10.0.0.1|control-plane"
WORKER = "n2|10.0.0.2|worker"


def run(nodes, role):
    arr = " ".join(f"'{n}'" for n in nodes)
    script = (f"set -eo pipefail; source '{COMMON}'; CLUSTER_NODES=({arr}); "
              f"x=$(nodes_by_role {role} | head -n1); echo \"OK:$x\"")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


@pytest.mark.parametrize("nodes,role,expected", [
    ([CP], "control-plane", "n1|10.0.0.1"),
    ([CP, WORKER], "control-plane", "n1|10.0.0.1"),     # le cas du banc
    ([WORKER, CP], "worker", "n2|10.0.0.2"),
    ([WORKER], "control-plane", ""),                    # aucun : vide, pas d'échec
    ([CP, WORKER, "n3|10.0.0.3|worker"], "control-plane", "n1|10.0.0.1"),
])
def test_nodes_by_role_never_fails_the_caller(nodes, role, expected):
    r = run(nodes, role)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == f"OK:{expected}"


@pytest.mark.parametrize("script", ["harvester-shutdown.sh", "harvester-startup.sh"])
def test_an_unexpected_failure_is_said_not_silent(script):
    """Toute sortie sur erreur imprévue est dite à l'opérateur et à la
    console (événement « complete » en erreur)."""
    src = (ROOT / "bin" / script).read_text()
    trap = [line for line in src.splitlines() if line.strip().startswith("trap ") and "ERR" in line]
    assert trap, f"{script} : pas de trap ERR"
    assert "log_error" in trap[0] and "emit_event" in trap[0]
