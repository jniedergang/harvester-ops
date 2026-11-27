"""2.10.1 : les outils de la console tournent dans le conteneur livré, où
`bin/` est copié dans /usr/local/bin et `web/` dans /opt/harvester-ops/web
(container/Containerfile). hcl_guard et deploy_plans cherchaient web/ à côté
de bin/ (`parents[2] / "web"` = /usr/web) : harvester-registry et
harvester-plans mouraient à l'import (ModuleNotFoundError tf_guard), vu sur
hops-upd avec le paquet 2.10.1 et l'image 2.9.1 précédente le 27/11/2026.
Ce test rejoue la disposition du conteneur : bin/ et web/ séparés, web/
désigné par HARVESTER_OPS_WEB_DIR (même liste que web_loader.load).

Mutation rapportée : retour au seul `parents[2] / "web"` -> harvester-registry
et harvester-plans échouent.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def layout(tmp_path_factory):
    t = tmp_path_factory.mktemp("container")
    shutil.copytree(ROOT / "bin", t / "usr" / "local" / "bin",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "web", t / "opt" / "harvester-ops" / "web",
                    ignore=shutil.ignore_patterns("__pycache__", "static", "templates", "vendor"))
    return t


# les deux agents de l hôte (secrets, mise à jour) n ont pas d aide et tournent hors du conteneur
TOOLS = sorted(p.name for p in (ROOT / "bin").glob("harvester-*.py") if p.name not in ("harvester-secrets-agent.py", "harvester-ops-update.py"))


@pytest.mark.parametrize("tool", TOOLS)
def test_tools_import_in_the_container_layout(layout, tool):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(layout),
           "HARVESTER_OPS_WEB_DIR": str(layout / "opt" / "harvester-ops" / "web")}
    r = subprocess.run([sys.executable, str(layout / "usr" / "local" / "bin" / tool), "--help"],
                       capture_output=True, text=True, env=env, cwd=str(layout), timeout=60)
    assert r.returncode == 0, r.stderr[-600:]
    assert "ModuleNotFoundError" not in r.stderr
