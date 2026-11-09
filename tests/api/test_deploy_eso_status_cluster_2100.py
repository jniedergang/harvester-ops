"""2.10.0 (tâche 13, réel) : `harvester-capi eso-status` calcule la clé du
cluster enfant (`<harvester>/<ns>/<cluster>`) : sans le nom du cluster
Harvester il refuse (« harvester: a name »). Les lectures du Défaire (détails
de l'élément `eso`, garde du chart `eso`) passaient par `_deploy_capi_json`
sans ce nom : le détail disait « error » et le chart restait toujours gardé
(`eso-status-unreadable`), même sans aucun ExternalSecret. Vu sur harvlab2."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402


@pytest.fixture
def seen(monkeypatch):
    calls = []
    monkeypatch.setattr(wapp, "_capi_script", lambda: Path("/x/harvester-capi.py"))

    def run_json(cmd, spec=None, timeout=60):
        calls.append(list(cmd))
        return ({"externalsecrets": [], "namespaces": ["team-a"], "auth": {"mount": "m"}, "crs": None}, 0), None
    monkeypatch.setattr(wapp, "_capi_run_json", run_json)
    return calls


def _eso_reads(calls):
    return [c for c in calls if len(c) > 2 and c[2] == "eso-status"]


def test_undo_details_of_eso_name_the_harvester_cluster(seen):
    det = wapp._undo_details("/kc", "harvlab2", {"kind": "eso", "ref": "c13/c13"}, {})
    assert "error" not in det and det["namespaces"] == ["team-a"]
    assert all("--harvester-name=harvlab2" in c for c in _eso_reads(seen)) and _eso_reads(seen)


def test_eso_chart_guard_names_the_harvester_cluster(seen):
    assert wapp._undo_eso_chart_kept("/kc", "harvlab2", "c13", "c13", "eso") is None
    assert all("--harvester-name=harvlab2" in c for c in _eso_reads(seen)) and _eso_reads(seen)


# Revue finale m-D : `eso-status` répond rc 0 avec `error` et une liste vide
# quand l'enfant ne se lit pas ; le constat préalable du Défaire garde alors
# le chart (même règle que `service-remove`), sauf si le cluster est parti.
@pytest.mark.parametrize("error, kept", [
    ("child-unreachable: connection refused", True),
    ("child-kubeconfig-missing", True),
    ("eso-cluster-missing", False),
    (None, False),
])
def test_eso_chart_guard_unreadable_child_keeps_the_chart(monkeypatch, error, kept):
    monkeypatch.setattr(wapp, "_capi_script", lambda: Path("/x/harvester-capi.py"))

    def run_json(cmd, spec=None, timeout=60):
        return ({"externalsecrets": [], **({"error": error} if error else {})}, 0), None
    monkeypatch.setattr(wapp, "_capi_run_json", run_json)
    got = wapp._undo_eso_chart_kept("/kc", "harvlab2", "c13", "c13", "eso")
    if kept:
        assert got.startswith("eso-status-unreadable:") and error.split(":")[0] in got
    else:
        assert got is None
