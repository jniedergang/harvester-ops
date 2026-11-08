"""2.8.0 (tâche 9) : phase GitOps de l'onglet Plans, lue dans la source.

Les bornes de la carte suivent celles du serveur (deploy_plans,
child_gitops), l'ordre des sortes de Défaire suit deploy_undo.KINDS_ORDER, et
aucune valeur de secret Git n'entre dans la signature du contrôle.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
SRC = (ROOT / "web" / "static" / "js" / "bm-plans.js").read_text()

import child_gitops as cg  # noqa: E402
import deploy_plans as dpl  # noqa: E402
import deploy_undo as du  # noqa: E402


@pytest.mark.parametrize("js,server", [
    (r"const GITOPS_TIMEOUT = \[(\d+), (\d+), (\d+)\];", dpl.GITOPS_TIMEOUT),
    (r"const MAX_GITOPS = (\d+);", (dpl.MAX_GITOPS_ENTRIES,)),
    (r"const GITOPS_INTERVAL = \[(\d+), (\d+), (\d+)\];", (cg.INTERVAL_MIN, cg.INTERVAL_MAX, cg.INTERVAL_DEFAULT)),
])
def test_card_bounds_follow_the_server(js, server):
    m = re.search(js, SRC)
    assert m and tuple(int(x) for x in m.groups()) == tuple(server)


def test_tools_and_sources_are_those_of_child_gitops():
    assert re.search(r"const GITOPS_TOOLS = \[([^\]]+)\];", SRC).group(1).replace("'", "").split(", ") == list(cg.TOOLS)
    assert re.search(r"const GITOPS_SOURCES = \[([^\]]+)\];", SRC).group(1).replace("'", "").split(", ") == list(cg.SOURCES)
    js_name = re.search(r"const GITOPS_NAME_RE = /(.+)/;", SRC).group(1)
    assert js_name == cg.NAME_RE.pattern


def test_undo_kinds_follow_the_removal_order():
    js = re.search(r"const UNDO_KINDS = \[([^\]]+)\];", SRC).group(1).replace("'", "").split(", ")
    assert js == list(du.KINDS_ORDER)


def test_collected_entry_keys_are_known_to_the_server():
    body = SRC[SRC.index("function collectGitops("):SRC.index("function gitopsChange(")]
    keys = set(re.findall(r"\b([a-z_]+): ", body.split("Object.assign({}, e.raw, {", 1)[1].split("});", 1)[0]))
    assert keys <= dpl.GITOPS_ENTRY_KEYS, keys - dpl.GITOPS_ENTRY_KEYS


def test_check_signature_carries_secret_names_only():
    sig = re.search(r"const sig = \(\) => JSON\.stringify\(\[(.+)\]\);", SRC).group(1)
    assert "Object.keys(gopValues()).sort()" in sig
    assert "gopValues()]" not in sig and "gopValues())" not in sig.replace("Object.keys(gopValues())", "")


def test_secrets_cleared_after_a_launch():
    after = SRC[SRC.index("if (r.ok && r.d.action_id) {"):]
    assert after.split("\n", 2)[1].strip().startswith("gopClear();")
