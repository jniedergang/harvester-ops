"""2.7.0, revue des tâches 1 à 3 : le socle de la console est un nom réservé
(m3), et les commandes qui ouvrent un cluster enfant transforment l'arrêt de
la console (SIGTERM) en sortie, pour que le kubeconfig du cluster enfant soit
effacé (I4)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capi_manifests_cli_270 import cli, world  # noqa: E402,F401


@pytest.mark.parametrize("over", [{"item": "hops-policies"}, {"package": "harvester-ops"}])
def test_m3_the_builtin_names_are_reserved(cli, world, over):
    _, found = cli.manifest_objects(cli.manifest_spec_from(world["spec"](**over)))
    assert [f["code"] for f in found] == ["artefact-name-reserved"]


def test_i4_the_child_commands_turn_sigterm_into_an_exit(cli, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.signal, "signal", lambda sig, fn: seen.__setitem__(sig, fn))
    monkeypatch.setattr(cli, "cmd_manifests", lambda a: 0)
    cli.main(["manifests"])
    assert seen == {}                                                        # lecture seule : rien à effacer
    for argv in (["manifest-remove", "--name", "a/b/c"], ["token", "--name", "a/b", "--account", "a/s"]):
        seen.clear()
        monkeypatch.setattr(cli, "kube_from", lambda a: (_ for _ in ()).throw(ValueError("stop")))
        cli.main(argv)
        assert seen[cli.signal.SIGTERM] is cli._exit_cancelled and seen[cli.signal.SIGINT] is cli._exit_cancelled
    with pytest.raises(SystemExit) as e:
        cli._exit_cancelled(15, None)
    assert e.value.code == cli.EXIT_CANCELLED
