"""2.10.0 (tâche 10) : External Secrets dans les plans de déploiement et
leur défaire. Phase `eso` tirée de l'option `external_secrets` des
clusters, après les artefacts (chart `eso`) et avant GitOps ; défaire :
ESO retiré tant que son chart tourne (point 300), le chart avec les
artefacts, son cluster après lui."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import deploy_chain as dch  # noqa: E402
import deploy_undo as du  # noqa: E402

RUN = "aaaabbbbcccc"


def test_chain_order_puts_eso_between_artefacts_and_gitops():
    o = dch.ORDER
    assert o.index("artefacts") < o.index("eso") < o.index("gitops")
    assert du.ORDER.index("gitops") < du.ORDER.index("eso") < du.ORDER.index("artefacts")


def _u(kind, ref):
    return {"id": du.undo_id(kind, ref), "kind": kind, "ref": ref, "phase": du.PHASE_OF[kind], "run": RUN,
            "proven": True, "default": True, "note": ""}


def test_undo_plan_removes_eso_before_its_chart_and_its_cluster():
    undos = [_u("capi-cluster", "c1/c1"), _u("chart", "c1/c1/eso"), _u("eso", "c1/c1"), _u("gitops", "c1/c1/app"),
             _u("chart", "c2/c2/eso")]
    steps = {s["id"]: s for s in du.plan(undos)}
    order = [s["kind"] for s in du.plan(undos)]
    assert order.index("gitops") < order.index("eso") < order.index("chart") < order.index("capi-cluster")
    assert steps["eso-c1/c1"]["depends_on"] == ["gitops:gitops-c1/c1/app"]
    assert "eso:eso-c1/c1" in steps["chart-c1/c1/eso"]["depends_on"]
    assert "eso:eso-c1/c1" not in steps["chart-c2/c2/eso"]["depends_on"]          # autre cluster
    assert "eso:eso-c1/c1" in steps["capi-cluster-c1/c1"]["depends_on"]
    assert du.cluster_of(_u("eso", "c1/c1")) == "c1/c1"


def test_undo_collects_an_eso_item_created_by_the_run():
    res = [{"_run": RUN, "phases": [{"id": "eso", "items": [
        {"id": "eso-c1/c1", "created": True, "undo": {"kind": "eso", "ref": "c1/c1"}},
        {"id": "eso-c2/c2", "created": False}]}]}]
    got = du.collect(res)
    assert [(u["kind"], u["ref"], u["phase"]) for u in got] == [("eso", "c1/c1", "eso")]


# ---------------------------------------------------------------------------
# Lancement (lecture seule)
# ---------------------------------------------------------------------------
def _plan(clusters_on=True):
    return {"name": "p1", "phases": {"clusters": {"enabled": clusters_on}}}


def _entries():
    return [{"raw_name": "c1", "name": "c1", "spec": {"name": "c1", "namespace": "c1",
                                                      "external_secrets": {"namespaces": ["team-a"]}}},
            {"raw_name": "c2", "name": "c2", "spec": {"name": "c2", "namespace": "c2"}},
            {"raw_name": "c3", "name": "c3", "spec": {"name": "c3", "namespace": "c3",
                                                      "external_secrets": {"namespaces": []}}}]


@pytest.mark.parametrize("active,role,arts,codes", [
    (True, "admin", [{"ref": "c1/c1", "item": "eso"}, {"ref": "c3/c3", "item": "eso"}], ["eso-no-namespace"]),
    (False, "admin", [{"ref": "c1/c1", "item": "eso"}, {"ref": "c3/c3", "item": "eso"}],
     ["eso-no-namespace", "eso-needs-openbao"]),
    (True, "operator", [{"ref": "c1/c1", "item": "eso"}, {"ref": "c3/c3", "item": "eso"}],
     ["eso-admin-only", "eso-no-namespace", "eso-admin-only"]),
    (True, None, [], ["eso-chart-not-in-plan", "eso-no-namespace", "eso-chart-not-in-plan"]),
])
def test_prepare_rows_and_findings(monkeypatch, active, role, arts, codes):
    monkeypatch.setattr(wapp, "_secrets_settings", lambda: {"mode": "b1", "addr": "x", "prefix": "p"} if active
                        else {"mode": "local"})
    findings = []
    rows = wapp._deploy_eso_prepare(_plan(), findings, _entries(), arts, role)
    assert [(r["id"], r["ref"], r["namespaces"]) for r in rows] == [("eso-c1/c1", "c1/c1", ["team-a"]),
                                                                    ("eso-c3/c3", "c3/c3", [])]
    assert [f["code"] for f in findings] == codes
    assert all(f.get("level") == "warning" for f in findings if f["code"] == "eso-chart-not-in-plan")
    assert wapp._deploy_eso_prepare(_plan(False), [], _entries(), arts, role) == []


# ---------------------------------------------------------------------------
# Phase
# ---------------------------------------------------------------------------
@pytest.fixture
def phase(monkeypatch):
    box = {"launched": [], "origin_run": RUN, "status": "done"}

    def launch(cluster, kc, sub, args, spec=None, parent=None, cluster_user=None, label=None, dry_run=False):
        box["launched"].append((sub, args, spec, parent, label))
        return SimpleNamespace(id="child0000001"), None
    monkeypatch.setattr(wapp, "_capi_launch", launch)
    monkeypatch.setattr(wapp, "_deploy_wait_child", lambda aid, **kw: (box["status"], 0))
    monkeypatch.setattr(wapp, "_deploy_capi_json", lambda kc, sub, args, spec=None, timeout=180:
                        ({"present": True, "origin": {"run": box["origin_run"]}}, 0, None))

    def make(dry=False):
        run = SimpleNamespace(dry_run=dry, result={"by": "alice"}, launch_role="admin", id=RUN, cluster_user=None)
        dctx = {"plan": {"name": "p1"}, "eso": [{"id": "eso-c1/c1", "ref": "c1/c1", "namespaces": ["team-a"],
                                                  "depends_on": []}]}
        return wapp._deploy_eso_phase(run, "harvlab2", lambda: "/kc", dctx)
    box["make"] = make
    return box


def test_phase_launches_eso_deploy_and_marks_what_it_created(phase):
    ph = phase["make"]()
    assert ph.id == "eso"
    out = ph.run_item(None, {"previous": None}, dch.Item("eso-c1/c1"))
    assert out.status == "done" and out.data["created"] is True
    assert out.data["undo"] == {"kind": "eso", "ref": "c1/c1"}
    sub, args, spec, parent, label = phase["launched"][0]
    assert sub == "eso-deploy" and parent == RUN and label == "capi-eso:c1/c1"
    assert spec == {"cluster": "c1/c1", "namespaces": ["team-a"], "by": "alice", "role": "admin",
                    "origin": {"plan": "p1", "run": RUN}}


def test_phase_does_not_claim_what_another_run_posed(phase):
    phase["origin_run"] = "000011112222"
    out = phase["make"]().run_item(None, {"previous": None}, dch.Item("eso-c1/c1"))
    assert out.status == "done" and out.data["created"] is False and "undo" not in out.data


def test_phase_dry_run_launches_nothing(phase):
    out = phase["make"](dry=True).run_item(None, {"previous": None}, dch.Item("eso-c1/c1"))
    assert out.status == "done" and phase["launched"] == []


def test_phase_failure(phase):
    phase["status"] = "error"
    out = phase["make"]().run_item(None, {"previous": None}, dch.Item("eso-c1/c1"))
    assert out.status == "error" and "child0000001" in out.message


def test_undo_target_reads_the_store_objects(monkeypatch):
    seen = []
    monkeypatch.setattr(wapp, "_deploy_capi_json", lambda kc, sub, args, spec=None, timeout=180:
                        seen.append((sub, args)) or ({"present": True, "origin": {"run": RUN}}, 0, None))
    assert wapp._undo_target("/kc", "harvlab2", {"kind": "eso", "ref": "c1/c1"}, {RUN}) == ("ours", RUN)
    assert seen == [("manifest-verify", ["--name", "c1/c1/eso"])]
    assert wapp._undo_target("/kc", "harvlab2", {"kind": "eso", "ref": "c1/c1"}, {"ffffffffffff"})[0] == "foreign"
