"""2.7.0 : deux règles du moteur de la chaîne.

* `after_failure_of` : une phase va à son terme derrière l'échec d'une
  phase nommée (artefacts derrière une phase Clusters partielle, chaque
  phase du Défaire) ; jamais derrière un arrêt ni une autre phase ;
* dépendance `<phase>:<élément>` vers un élément d'une phase déjà déroulée ;
  un élément sauté pour elle rend la phase `partial`.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_deploy_chain_240 import Ctx, dc, run  # noqa: E402


def items_phase(ctx, pid, outcomes, deps=None, key="id", **kw):
    def run_item(c, state, item):
        ctx.calls.append(f"{pid}:{item.id}")
        return dc.ItemOutcome(outcomes[item.id], message=outcomes[item.id])
    items = [dc.Item(i, tuple((deps or {}).get(i, ()))) for i in outcomes]
    return dc.Phase(pid, items=items, run_item=run_item, item_key=key, item_step=f"{pid}-", **kw)


@pytest.mark.parametrize("clusters,after,expect_status,ran", [
    ({"a": "done", "b": "error"}, ("clusters",), "partial", ["artefacts:x-a"]),
    ({"a": "done", "b": "error"}, (), "partial", []),                        # sans la règle : arrêt
    ({"a": "error", "b": "error"}, ("clusters",), "error", []),             # tout en échec : x-a sauté aussi
])
def test_after_failure_of(clusters, after, expect_status, ran):
    ctx = Ctx()
    out = run(ctx, [items_phase(ctx, "clusters", clusters, key="cluster"),
                    items_phase(ctx, "artefacts", {"x-a": "done", "x-b": "done"},
                                deps={"x-a": ["clusters:a"], "x-b": ["clusters:b"]}, after_failure_of=after)])
    assert out["status"] == expect_status and out["failed_phase"] == "clusters"
    assert [c for c in ctx.calls if c.startswith("artefacts")] == ran
    art = out["phases"][1]
    if after:
        got = {i["id"]: (i["status"], i["message"]) for i in art["items"]}
        assert got["x-b"] == ("skipped", "depends on clusters:b, not done")
        assert art["status"] == ("partial" if ran else "error")
    else:
        assert art["status"] == "skipped"


def test_a_failure_after_a_tolerated_one_still_stops_the_rest():
    ctx = Ctx()
    out = run(ctx, [items_phase(ctx, "clusters", {"a": "done", "b": "error"}, key="cluster"),
                    items_phase(ctx, "artefacts", {"x": "error"}, after_failure_of=("clusters",))])
    assert out["status"] == "error" and out["failed_phase"] == "clusters"


def test_cancel_is_never_tolerated():
    ctx = Ctx(cancel_after=2)
    out = run(ctx, [items_phase(ctx, "clusters", {"a": "done", "b": "done"}, key="cluster"),
                    items_phase(ctx, "artefacts", {"x": "done"}, after_failure_of=("clusters",))])
    assert out["status"] == "cancelled" and not any(c.startswith("artefacts") for c in ctx.calls)


def test_the_item_key_is_kept_for_later_phases():
    ctx = Ctx()
    out = run(ctx, [items_phase(ctx, "clusters", {"a": "done"}, key="cluster")])
    assert out["phases"][0]["item_key"] == "cluster"
