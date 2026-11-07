"""2.7.0 (tâche 5) : `harvester-plans undo` (parité avec Défaire de la
console : même collecte, même plan de destruction, même lancement).

* `--dry-run` : plan JSON, aucune action fille d'écriture ;
* `--include registry,unproven` : coche le registre (et les déclarations
  non prouvées) ; catégorie inconnue refusée ;
* sans `--confirm` exact : refus `confirm-name`, rien de lancé ;
* `--delete-registry-data NOM` : seconde confirmation du registre.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_deploy_undo_routes_270 as tu  # noqa: E402
from test_harvester_plans_cli_240 import _state, run  # noqa: E402

env, world, capi, art, undo = tu.env, tu.world, tu.capi, tu.art, tu.undo


def test_dry_run_prints_the_plan_and_writes_nothing(undo, monkeypatch, capsys):
    st = _state(monkeypatch, undo)
    rc, out, err = run(capsys, *st, "undo", undo["orig"].id, "--dry-run")
    body = json.loads(out)
    assert rc == 0 and body["undoes"] == undo["orig"].id
    assert [i["selected"] for i in body["items"]] == [True, True, True, False, True]
    assert "[ ] registry harvlab/amorcage" in err
    assert undo["undo"].removals == [] and undo["destroys"] == []
    rc, out, _ = run(capsys, *st, "undo", undo["orig"].id, "--dry-run", "--include", "registry")
    assert [i["selected"] for i in json.loads(out)["items"]] == [True] * 5


def test_refusals(undo, monkeypatch, capsys):
    st = _state(monkeypatch, undo)
    rc, out, _ = run(capsys, *st, "undo", undo["orig"].id)
    assert rc == 2 and json.loads(out)["code"] == "confirm-name"
    rc, out, _ = run(capsys, *st, "undo", undo["orig"].id, "--include", "everything", "--confirm", "harvlab")
    assert rc == 2 and json.loads(out)["code"] == "usage"
    rc, out, _ = run(capsys, *st, "undo", "0123456789ab", "--confirm", "harvlab")
    assert rc == 2
    assert undo["undo"].removals == []


def test_undo_runs_the_console_chain(undo, monkeypatch, capsys):
    st = _state(monkeypatch, undo)
    rc, out, err = run(capsys, *st, "undo", undo["orig"].id, "--confirm", "harvlab", "--include", "registry",
                       "--delete-registry-data", "amorcage")
    d = json.loads(out)
    assert rc == 0 and d["status"] == "done", json.dumps(d["result"]["phases"])[:3000]
    assert "STEP_EVENT|undo-capi-cluster-web/web|done|removed" in err
    assert undo["undo"].removals[-1][1][-1] == "--delete-data"


def _tf_variant(undo, monkeypatch, note, proven):
    """La déclaration du défaire, rendue non prouvée ou modifiée depuis."""
    real = tu.wapp._du.collect

    def collect(*a, **kw):
        out = real(*a, **kw)
        for u in out:
            if u["kind"] == "tf-declaration":
                u.update(default=False, proven=proven, note=note)
        return out
    monkeypatch.setattr(tu.wapp._du, "collect", collect)


@pytest.mark.parametrize("variant,include,selected", [
    ("unproven", "unproven", True),
    ("unproven", "modified", False),
    ("modified", "modified", True),
    ("modified", "unproven", False),
])
def test_include_unproven_and_modified_are_separate(undo, monkeypatch, capsys, variant, include, selected):
    """Revue 1-3 m9 / 4-8 m9 : `unproven` ne coche plus les déclarations
    modifiées depuis leur application (catégorie `modified`)."""
    du = tu.wapp._du
    if variant == "unproven":
        _tf_variant(undo, monkeypatch, du.NOT_PROVEN, False)
    else:
        _tf_variant(undo, monkeypatch, du.MODIFIED, True)
    st = _state(monkeypatch, undo)
    rc, out, _ = run(capsys, *st, "undo", undo["orig"].id, "--dry-run", "--include", include)
    tf = [i for i in json.loads(out)["items"] if i["kind"] == "tf-declaration"][0]
    assert rc == 0 and tf["selected"] is selected


def test_an_undo_id_is_refused_as_undo_of_undo(undo, monkeypatch, capsys):
    st = _state(monkeypatch, undo)
    rc, out, _ = run(capsys, *st, "undo", undo["orig"].id, "--confirm", "harvlab")
    undo_id = json.loads(out)["action_id"]
    rc, out, _ = run(capsys, *st, "undo", undo_id, "--dry-run")
    assert rc == 2 and json.loads(out)["code"] == "undo-of-undo"
