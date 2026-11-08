"""2.8.0 (tâche 5) : « Défaire » des entrées GitOps et de l'import dans un
Rancher externe (deploy_undo).

Ordre de retrait : GitOps, artefacts, import, clusters, registre,
Terraform ; l'import part AVANT son cluster (sinon Rancher garde un cluster
mort), une entrée GitOps avant le chart de Flux et avant l'import (une
entrée Fleet vit dans le Rancher de l'import).

Mutation jouée (rapport des tâches 5 à 8) : « import retiré après le
cluster ».
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import deploy_undo as du  # noqa: E402

A, B = "aaaaaaaaaaa1", "aaaaaaaaaaa2"


def result(phases, resumes=None):
    return {"plan": "site", "resumes": resumes, "phases": [{"id": p, "items": its} for p, its in phases.items()]}


def cluster_item(ref="web/web", created=True, imported=True):
    it = {"cluster": ref.split("/")[1], "ref": ref, "status": "done", "created": created}
    if created:
        it["undo"] = {"kind": "capi-cluster", "ref": ref}
    if imported:
        it["undo_more"] = [{"kind": "rancher-import", "ref": ref}]
    return it


def gitops_item(ref="web/web/apps", created=True):
    return {"id": "gitops-web-apps", "status": "done", "created": created,
            "undo": {"kind": "gitops", "ref": ref}}


def chart_item(ref="web/web/flux"):
    return {"id": "art-web-flux", "status": "done", "created": True, "undo": {"kind": "chart", "ref": ref}}


def chain():
    return du.chain(A, {A: result({"clusters": [cluster_item()], "artefacts": [chart_item()],
                                   "gitops": [gitops_item()]})}.get)


def test_collect_finds_the_gitops_entry_and_the_import():
    got = {(u["kind"], u["ref"]): u for u in du.collect(chain())}
    assert set(got) == {("capi-cluster", "web/web"), ("rancher-import", "web/web"), ("chart", "web/web/flux"),
                        ("gitops", "web/web/apps")}
    assert got[("rancher-import", "web/web")]["phase"] == "rancher-import"
    assert got[("gitops", "web/web/apps")]["phase"] == "gitops"
    assert all(u["run"] == A and u["default"] and u["proven"] for u in got.values())


def test_an_import_made_by_a_resume_on_an_adopted_cluster_is_undone_alone():
    """Cluster repris (pas créé par la chaîne), import fait par la reprise :
    seul l'import est défait."""
    runs = {B: result({"clusters": [cluster_item(created=False)]}, resumes=A),
            A: result({"clusters": [{"cluster": "web", "ref": "web/web", "status": "error", "created": False}]})}
    got = du.collect(du.chain(B, runs.get))
    assert [(u["kind"], u["ref"], u["run"]) for u in got] == [("rancher-import", "web/web", B)]


def test_an_entry_adopted_from_another_run_is_never_undone():
    got = du.collect(du.chain(A, {A: result({"gitops": [gitops_item(created=False)]})}.get))
    assert got == []


def test_order_import_before_cluster_and_gitops_first():
    steps = du.plan(du.collect(chain()))
    assert [s["kind"] for s in steps] == ["gitops", "chart", "rancher-import", "capi-cluster"]
    by = {s["kind"]: s for s in steps}
    assert by["chart"]["depends_on"] == ["gitops:gitops-web/web/apps"]
    assert by["rancher-import"]["depends_on"] == ["gitops:gitops-web/web/apps"]
    assert by["capi-cluster"]["depends_on"] == ["gitops:gitops-web/web/apps", "artefacts:chart-web/web/flux",
                                                "rancher-import:rancher-import-web/web"]
    assert du.ORDER == ("gitops", "artefacts", "rancher-import", "clusters", "registry", "terraform")


def test_entries_of_another_cluster_do_not_hold_this_one():
    runs = {A: result({"clusters": [cluster_item("web/web"), cluster_item("api/api", imported=False)],
                       "gitops": [gitops_item("api/api/apps")]})}
    steps = {s["id"]: s for s in du.plan(du.collect(du.chain(A, runs.get)))}
    assert steps["capi-cluster-web/web"]["depends_on"] == ["rancher-import:rancher-import-web/web"]
    assert steps["capi-cluster-api/api"]["depends_on"] == ["gitops:gitops-api/api/apps"]


@pytest.mark.parametrize("u,ref", [({"kind": "gitops", "ref": "ns/c/apps"}, "ns/c"),
                                   ({"kind": "rancher-import", "ref": "ns/c"}, "ns/c"),
                                   ({"kind": "chart", "ref": "ns/c/flux"}, "ns/c")])
def test_cluster_of(u, ref):
    assert du.cluster_of(u) == ref


def test_phases_of_the_engine_go_to_their_end():
    steps = du.plan(du.collect(chain()))
    phases = du.phases(steps, lambda *a: None, lambda *a: False)
    assert [p.id for p in phases] == ["gitops", "artefacts", "rancher-import", "clusters"]
    assert phases[2].after_failure_of == ("gitops", "artefacts")
