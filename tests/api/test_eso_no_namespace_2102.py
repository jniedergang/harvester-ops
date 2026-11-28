"""2.10.2 (point 332 A) : External Secrets posé sans namespace activé.

Choisi à la création d'un cluster, ESO pose son chart, mais les namespaces
d'équipe n'existent pas encore sur un cluster neuf : ils s'activent ensuite,
un par un, depuis l'onglet Secrets externes. L'état le dit
(`eso-namespaces-none`, avertissement), et le formulaire de création comme
l'onglet le rappellent.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "tests" / "api"))

import test_capi_eso_cli_2100 as t  # noqa: E402
from test_capi_eso_cli_2100 import CL, KEY, LISTEN, NS, STORE, Child, world_ok  # noqa: E402

cli = t.cli
world = t.world


def status(world, capsys, mgmt):
    assert world["run"](mgmt, ["eso-status", "--kubeconfig", "/x", "--name", f"{NS}/{CL}", "--json"]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("label,chart,entry,expected", [
    ("chart posed, never set up", True, None, True),
    ("set up, last namespace gone from settings", True, {"mount": STORE["jwt_mount"], "namespaces": []}, True),
    ("one namespace enabled", True, {"mount": STORE["jwt_mount"], "namespaces": ["team-a"]}, False),
    ("no chart, nothing set", False, None, False),
])
def test_the_status_says_when_no_namespace_is_enabled(world, capsys, label, chart, entry, expected):
    world["settings"](eso={"listener": LISTEN, "clusters": {KEY: entry} if entry else {}})
    world["box"]["child"] = Child()
    doc = status(world, capsys, world_ok(chart=chart))
    got = [f for f in doc.get("findings") or [] if f["code"] == "eso-namespaces-none"]
    if expected:
        assert got == [{"code": "eso-namespaces-none", "level": "warn", "facts": {"cluster": f"{NS}/{CL}"}}], label
    else:
        assert got == [], label


def test_the_finding_is_said_even_when_the_child_cannot_be_read(world, capsys, monkeypatch, cli):
    world["settings"](eso={"listener": LISTEN, "clusters": {}})

    class Down(Child):
        def list(self, *a, **kw):
            raise cli.KubeError("Unable to connect to the server")
    world["box"]["child"] = Down()
    doc = status(world, capsys, world_ok())
    assert doc["error"].startswith("child-unreachable")
    assert [f["code"] for f in doc["findings"]] == ["eso-namespaces-none"]
