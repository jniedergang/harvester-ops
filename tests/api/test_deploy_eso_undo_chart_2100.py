"""2.10.0 (tâche 13, point 300) : le Défaire d'un plan ne retire pas le chart
`eso` tant que des `ExternalSecret` des équipes (laissés exprès par
`eso-remove`, revue 8-10 I-1) existent dans le cluster enfant qui reste : le
chart emporterait leurs CRD. Le chart est gardé, l'élément le dit
(`eso-foreign-objects`) ; aucun retrait lancé. Même règle quand le cluster
enfant part dans le même Défaire ; décocher le chart laisse le cluster tout
emporter. Le refus lui-même est porté par `service-remove` (revue finale I-1,
tests de `test_capi_eso_cli_2100.py`)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_deploy_undo_routes_270 as tu  # noqa: E402

env = tu.env
world = tu.world
capi = tu.capi
art = tu.art
undo = tu.undo


@pytest.fixture
def eso_undo(undo, monkeypatch):
    fake = undo["undo"]
    fake.applied[("web", "eso")] = {"type": "chart", "cluster": "web", "package": "socle", "version": "2026.11",
                                    "origin": {"plan": "site", "run": "c0ffee000001"}, "ready": True}
    undo["orig"].result["phases"][-1]["items"].append(tu.item("chart", "web/web/eso", id="art-web-eso"))
    fake.team_es = [{"ns": "team-c", "name": "db", "ready": True, "store": "vault-team-c"}]
    base_json = fake.json

    def json_(kc, sub, args, spec=None, timeout=180):
        if sub == "eso-status":
            fake.status_reads = getattr(fake, "status_reads", 0) + 1
            return {"externalsecrets": list(fake.team_es), "namespaces": [], "auth": None, "crs": None}, 0, None
        return base_json(kc, sub, args, spec, timeout)
    fake.json = json_
    monkeypatch.setattr(tu.wapp, "_deploy_capi_json", json_)
    return undo


def test_eso_chart_kept_while_team_externalsecrets_remain(eso_undo):
    fake = eso_undo["undo"]
    run = tu.finish(tu.launch(eso_undo, include=["chart-web/web/eso"]))
    it = tu.items_of(run)["chart-web/web/eso"]
    assert it["status"] == "error"
    assert "eso-foreign-objects" in it["message"] and "team-c/db" in it["message"]
    assert "chart kept" in it["message"]
    assert ("service-remove", "web/eso") not in fake.removals
    assert ("web", "eso") in fake.applied


def test_eso_chart_removed_when_no_externalsecret_is_left(eso_undo):
    fake = eso_undo["undo"]
    fake.team_es = []
    run = tu.finish(tu.launch(eso_undo, include=["chart-web/web/eso"]))
    assert tu.items_of(run)["chart-web/web/eso"]["status"] == "done", run.result
    assert ("service-remove", "web/eso") in fake.removals


def test_eso_chart_kept_with_its_cluster_in_the_same_undo(eso_undo):
    """Le retrait du chart avant le cluster bloquerait aussi (CRD en
    suppression, release « uninstalling ») : refus ; le cluster attend son
    chart, donc reste. Décocher le chart laisse le cluster tout emporter."""
    fake = eso_undo["undo"]
    run = tu.finish(tu.launch(eso_undo, include=["chart-web/web/eso", "chart-web/web/podinfo",
                                                 "manifest-web/web/regles", "capi-cluster-web/web"]))
    items = tu.items_of(run)
    assert items["chart-web/web/eso"]["status"] == "error" and "eso-foreign-objects" in items["chart-web/web/eso"]["message"]
    assert ("service-remove", "web/eso") not in fake.removals
    assert ("delete", "web/web") not in fake.removals


def test_cluster_removed_when_the_eso_chart_is_left_out(eso_undo):
    fake = eso_undo["undo"]
    run = tu.finish(tu.launch(eso_undo, include=["chart-web/web/podinfo", "manifest-web/web/regles",
                                                 "capi-cluster-web/web"]))
    assert tu.items_of(run)["capi-cluster-web/web"]["status"] == "done", run.result
    assert getattr(fake, "status_reads", 0) == 0


def test_eso_chart_kept_when_the_child_cannot_be_read(eso_undo, monkeypatch):
    fake = eso_undo["undo"]
    base = fake.json

    def broken(kc, sub, args, spec=None, timeout=180):
        if sub == "eso-status":
            return None, 1, "child unreachable"
        return base(kc, sub, args, spec, timeout)
    monkeypatch.setattr(tu.wapp, "_deploy_capi_json", broken)
    run = tu.finish(tu.launch(eso_undo, include=["chart-web/web/eso"]))
    it = tu.items_of(run)["chart-web/web/eso"]
    assert it["status"] == "error" and "chart kept" in it["message"]
    assert ("service-remove", "web/eso") not in fake.removals
