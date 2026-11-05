"""2.4.0 : constats de disponibilité (bin/lib/deploy_ready.py).

Jeu d'essai : lectures relevées en lecture seule sur harv1 le 05/11/2026
(tests/fixtures/deploy/harv1-reads.json, objets réduits aux champs lus :
nœud unique, cinq ManagedChart, un nœud Longhorn, 22 classes). Le cas à
trois nœuds en est dérivé (harvlab, éteint ce jour-là).

* un cas par code, plus « tout prêt » et trois nœuds dont un seul en défaut ;
* une lecture `None` n'est jamais prise pour prête ;
* `expected_nodes` plus grand que les nœuds lus = `nodes-missing` ;
* règle des nœuds et des charts : celle de hv_upgrade.prechecks (identité).

Mutations jouées (rapport de la tâche 4) : `charts-not-ready` recopié au lieu
de relire hv_upgrade (le test d'identité casse), lecture `None` comptée
comme vide (les cas `read-failed` cassent).
"""

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import deploy_ready as dr      # noqa: E402
import hv_upgrade              # noqa: E402

FIX = json.loads((ROOT / "tests" / "fixtures" / "deploy" / "harv1-reads.json").read_text())
NODE = FIX["nodes"][0]["metadata"]["name"]


def reads(**over):
    r = copy.deepcopy(FIX)
    r.update(over)
    return r


def cond(obj, ctype, status):
    obj = copy.deepcopy(obj)
    for c in obj["status"]["conditions"]:
        if c["type"] == ctype:
            c["status"] = status
    return obj


def renamed(obj, name):
    obj = copy.deepcopy(obj)
    obj["metadata"]["name"] = name
    return obj


def three(bad_index=None, lh_bad_index=None):
    nodes = [renamed(FIX["nodes"][0], f"harvlab-n{i}") for i in (1, 2, 3)]
    lh = [renamed(FIX["lh_nodes"][0], f"harvlab-n{i}") for i in (1, 2, 3)]
    if bad_index is not None:
        nodes[bad_index] = cond(nodes[bad_index], "Ready", "False")
    if lh_bad_index is not None:
        lh[lh_bad_index] = cond(lh[lh_bad_index], "Schedulable", "False")
    return reads(nodes=nodes, lh_nodes=lh)


def no_default(scs):
    out = copy.deepcopy(scs)
    for s in out:
        ann = s["metadata"].get("annotations")
        if ann:
            ann[dr.DEFAULT_CLASS_ANN] = "false"
    return out


def closed_lh():
    lh = copy.deepcopy(FIX["lh_nodes"])
    lh[0]["spec"]["allowScheduling"] = False
    return lh


CHART_DOWN = [cond(FIX["charts"][0], "Ready", "False")] + FIX["charts"][1:]


@pytest.mark.parametrize("r,expected,codes,detail", [
    (reads(), None, [], None),                                               # tout prêt (harv1)
    (three(), None, [], None),                                               # trois nœuds prêts
    (reads(version=None), None, ["api-unreachable"], "the Kubernetes API did not answer"),
    (reads(version=None, nodes=None), None, ["api-unreachable"], None),
    (reads(nodes=None), None, ["read-failed"], "nodes"),
    (reads(charts=None, storageclasses=None), None, ["read-failed"], "charts, storageclasses"),
    (reads(lh_nodes=None), None, ["read-failed"], "lh_nodes"),
    (reads(), [NODE, "node2", "node3"], ["nodes-missing", "longhorn-not-schedulable"], "node2, node3"),
    (reads(nodes=[cond(FIX["nodes"][0], "Ready", "False")]), None, ["nodes-not-ready"], NODE),
    (three(bad_index=1), None, ["nodes-not-ready"], "harvlab-n2"),           # un seul des trois
    (reads(charts=CHART_DOWN), None, ["charts-not-ready"], "harvester"),
    (three(lh_bad_index=2), None, ["longhorn-not-schedulable"], "harvlab-n3"),
    (reads(lh_nodes=closed_lh()), None, ["longhorn-not-schedulable"], NODE),
    (reads(lh_nodes=[]), None, ["longhorn-not-schedulable"], f"{NODE} (not registered)"),
    (reads(storageclasses=no_default(FIX["storageclasses"])), None, ["no-default-storageclass"],
     "no storage class is marked as default"),
    (reads(storageclasses=[]), None, ["no-default-storageclass"], None),
])
def test_evaluate(r, expected, codes, detail):
    out = dr.evaluate(r, expected_nodes=expected)
    assert [u["code"] for u in out["unmet"]] == codes
    assert out["ready"] is (not codes)
    if detail is not None:
        assert out["unmet"][0]["detail"] == detail


def test_expected_nodes_filter_the_others():
    """Un nœud hors de la série (déjà là, en défaut) ne retient pas la chaîne."""
    r = three(bad_index=2, lh_bad_index=2)
    assert dr.evaluate(r, expected_nodes=["harvlab-n1", "harvlab-n2"])["ready"] is True
    assert dr.evaluate(r)["ready"] is False


@pytest.mark.parametrize("key", dr.READS)
def test_none_is_never_ready(key):
    out = dr.evaluate(reads(**{key: None}))
    assert out["ready"] is False and out["unmet"]


def test_empty_reads_never_ready():
    assert dr.evaluate({})["ready"] is False
    assert dr.evaluate(None)["ready"] is False


def test_rules_are_those_of_hv_upgrade(monkeypatch):
    """Identité de règle : nœuds et charts passent par hv_upgrade.prechecks."""
    seen = []

    def fake(**kw):
        seen.append(sorted(kw))
        return [{"ok": False, "key": "nodes-ready", "text": "from-hv-upgrade-n"},
                {"ok": False, "key": "charts", "text": "from-hv-upgrade-c"}]
    monkeypatch.setattr(hv_upgrade, "prechecks", fake)
    out = dr.evaluate(reads())
    got = {u["code"]: u["detail"] for u in out["unmet"]}
    assert got["nodes-not-ready"] == "from-hv-upgrade-n"
    assert got["charts-not-ready"] == "from-hv-upgrade-c"
    assert ["nodes"] in seen and ["charts"] in seen


def test_capi_checks_not_evaluated_in_240():
    """rke2-traefik et Turtles : phase Cluster API, à partir de 2.5.0."""
    out = dr.evaluate(reads(), phases=("readiness", "terraform", "capi"))
    assert out == {"ready": True, "unmet": []}
