"""2.0.0 (revue 3b, I-1) : le nom d'un objet que la personne ne voit pas
n'apparaît jamais dans les étapes, la sortie ou le --json d'un renommage,
d'un déplacement, d'un vidage ou d'une annulation.

Un dossier contient un objet visible (`a`), un objet d'un espace de noms que
la personne ne lit pas (`hidden-ns-vm`, relu « cannot get resource ») et un
objet d'un cluster que Rancher lui cache (`harvester/secret-c/...`, absent
de sa demande). Les deux derniers sont comptés et masqués (« 2 other
objects not shown to you ») ; la vraie clé ne reste que dans le journal
interne `moves` (Annuler en a besoin)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_folders_cli_200 import cli, env, folder_id, journal, run, vm  # noqa: E402,F401
import folders_db as fdb  # noqa: E402

HIDDEN = ("hidden-ns-vm", "secret-c", "spy")
VISIBLE = ["harvester/harv1/nav/a"]


def fill(env):
    """Rangés par un admin : a, l'objet d'un namespace illisible, l'objet du cluster caché."""
    objs = [vm("a"), vm("hidden-ns-vm", ns="spy"), vm("x", cluster="secret-c", ns="spy")]
    assert run(env, "--id=fill", "assign", "--to", "/Prod/ERP", spec={"objects": objs}) == 0
    env["kube"].fail = {"hidden-ns-vm": "hidden"}


def person_spec():
    # ce que la console envoie : le cluster caché n'est pas dans la demande,
    # l'objet du namespace illisible y est (cluster visible), `visible` = SES clés
    return {"objects": [vm("a"), vm("hidden-ns-vm", ns="spy")], "visible": VISIBLE}


def leaks(text):
    return [h for h in HIDDEN if h in text]


GESTURES = {
    "rename": lambda env: ["--id=g1", "rename", "--id", str(folder_id(env, "/Prod")), "--name", "Production"],
    "move": lambda env: ["--id=g1", "move-folder", "--id", str(folder_id(env, "/Prod/ERP")), "--parent",
                         str(folder_id(env, "/Lab"))],
    "empty-into": lambda env: ["--id=g1", "empty-into", "--folder", str(folder_id(env, "/Prod/ERP")), "--to",
                               str(folder_id(env, "/Lab"))],
}


@pytest.mark.parametrize("gesture", sorted(GESTURES))
def test_hidden_objects_are_counted_never_named(env, capsys, gesture):
    fill(env)
    capsys.readouterr()
    assert run(env, *GESTURES[gesture](env), spec=person_spec()) == 1
    out, err = capsys.readouterr()
    assert leaks(out) == [] and leaks(err) == [], (out, err)
    assert "2 other objects not shown to you" in err
    assert "harvester/harv1/nav/a" in err                      # le visible reste nommé
    # la vraie clé reste dans le journal interne, pour Annuler
    assert {"harvester/harv1/spy/hidden-ns-vm", "harvester/secret-c/spy/x"} <= {j[0] for j in journal(env, "g1")}


def test_undo_never_names_hidden_objects_nor_their_conflicts(env, capsys):
    fill(env)
    # la personne annule le rangement fait par un admin ; l'objet du cluster
    # caché a bougé depuis : conflit à l'annulation
    conn = fdb.connect(env["db"])
    fdb.upsert_membership(conn, "harvester/secret-c/spy/x", "/Lab", "vm", scope="harvester/secret-c")
    conn.close()
    capsys.readouterr()
    assert run(env, "--id=u1", "undo", "--action", "fill", spec=person_spec()) == 1
    out, err = capsys.readouterr()
    assert leaks(out) == [] and leaks(err) == [], (out, err)
    data = json.loads(out)
    assert data["conflicts"] == [] and data["hidden_conflicts"] == 1
    assert "not shown to you" in err


def test_a_readable_object_outside_the_fleet_read_is_named(env, capsys):
    """Pas encore de lecture de flotte (`visible` sans la VM) : un objet que la
    personne a pu relire elle-même est nommé (elle le voit)."""
    fill(env)
    spec = dict(person_spec(), visible=[])
    capsys.readouterr()
    run(env, *GESTURES["rename"](env), spec=spec)
    err = capsys.readouterr().err
    assert "harvester/harv1/nav/a" in err and leaks(err) == []


def test_without_visible_the_command_line_names_everything(env, capsys):
    """En ligne de commande (pas de `visible`), l'exploitant voit tout."""
    fill(env)
    capsys.readouterr()
    run(env, *GESTURES["rename"](env), spec={"objects": person_spec()["objects"]})
    assert "hidden-ns-vm" in capsys.readouterr().err


def test_masking_mutation_unmasked_steps_leak(env, capsys, monkeypatch):
    """Mutation volontaire : sans masque, les clés cachées sortent."""
    real = cli.Shown
    monkeypatch.setattr(cli, "Shown", lambda spec: real({}))
    fill(env)
    capsys.readouterr()
    run(env, *GESTURES["rename"](env), spec=person_spec())
    assert leaks(capsys.readouterr().err)
