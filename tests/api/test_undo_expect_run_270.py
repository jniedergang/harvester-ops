"""Revue finale 2.7.0 (m-b) : le Défaire juge un objet « à nous » puis le
supprime par son nom ; entre les deux, un objet recréé sous ce nom serait
parti. Les outils de suppression reçoivent l'exécution attendue
(`--expect-run`) et relisent la marque juste avant d'écrire : un objet sans
cette marque est refusé (`undo-not-ours`) et rien n'est supprimé.
"""

import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import capi_cluster as cc  # noqa: E402
import capi_manifests as mf  # noqa: E402
import test_capi_manifests_cli_270 as tm  # noqa: E402
from test_capi_manifests_cli_270 import cli, world  # noqa: E402,F401
from test_capi_services_cli import Kube, cluster_obj  # noqa: E402

RUN, OTHER = "0123456789ab", "beef00000002"
RUN_KEY = cc.ORIGIN_ANNOTATIONS["run"]


def marked(obj, run):
    if run:
        obj.setdefault("metadata", {}).setdefault("annotations", {})[RUN_KEY] = run
    return obj


@pytest.mark.parametrize("mark,expect,ok,needle", [
    (RUN, RUN, True, ""),
    (OTHER, RUN, False, f"recreated by run {OTHER}"),
    (None, RUN, False, "no mark of run"),
    (None, "", True, ""),                                    # sans --expect-run : inchangé
])
def test_run_mismatch(mark, expect, ok, needle):
    got = cc.run_mismatch(marked({"metadata": {}}, mark), expect)
    assert (got is None) is ok and (ok or (got.startswith("undo-not-ours") and needle in got))


def test_cluster_delete_refuses_an_object_recreated_since(cli, monkeypatch, capsys):
    cs = cli.cs
    kube = Kube({(cs.K_CLUSTER, "web", "web"): marked(cluster_obj("web", "web"), OTHER)})
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    assert cli.main(["delete", "--name", "web/web", "--expect-run", RUN]) == cli.EXIT_BLOCKED
    assert kube.deleted == [] and "undo-not-ours" in capsys.readouterr().err


def test_service_remove_refuses_an_object_recreated_since(cli, monkeypatch, capsys):
    sv, cs = cli.sv, cli.cs
    hcp = marked({"metadata": {"namespace": "essai", "name": "podinfo"}, "spec": {"namespace": "podinfo"}}, None)
    kube = Kube({(cs.K_CLUSTER, "essai", "essai"): cluster_obj("essai", "essai"), (sv.K_HCP, "essai", "podinfo"): hcp})
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    args = argparse.Namespace(name="essai/podinfo", timeout=60, expect_run=RUN)
    assert cli.cmd_service_remove(args) == cli.EXIT_BLOCKED
    assert kube.deleted == [] and "no mark of run" in capsys.readouterr().err
    marked(hcp, RUN)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.cmd_service_remove(args) == cli.EXIT_OK and kube.deleted


@pytest.mark.parametrize("mark,code", [(OTHER, 2), (None, 2), (RUN, 0)])
def test_manifest_remove_rereads_the_mark(cli, world, capsys, mark, code):
    kube = tm.deployed(cli, world, capsys)
    for key in ((mf.K_CRS, tm.NS, "art-essai"), ("configmaps", tm.NS, "art-essai")):
        marked(kube.objs[key], mark)
    child = tm.Child([tm.stamped(tm.NSO), tm.stamped(tm.CMO), tm.stamped(tm.DEP)])
    assert world["run"](kube, child, ["manifest-remove", "--name", f"{tm.NS}/{tm.CL}/essai",
                                      "--expect-run", RUN]) == code
    gone = kube.get(mf.K_CRS, tm.NS, "art-essai") is None
    assert gone is (code == 0)
    assert (child.deleted == []) is (code != 0)
    if code:
        assert "undo-not-ours" in capsys.readouterr().err
