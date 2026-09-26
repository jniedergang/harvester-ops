"""v1.57.0 : `bin/harvester-resources.py`, l'outil par lequel passent les
écritures de la console sur les objets rangés sous Cluster.

Add-ons : les statuts sont ceux lus sur harv1 (Harvester v1.9.0) :
AddonEnabling / AddonDisabling en chemin, AddonDeploySuccessful et
AddonDisabled à l'arrivée, un échec dans AddonDeployFailed ou la condition
OperationFailed.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
_spec = importlib.util.spec_from_file_location("hres", ROOT / "bin" / "harvester-resources.py")
hres = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hres)


class FakeKube:
    def __init__(self, obj, statuses):
        self.obj = obj
        self.statuses = list(statuses)     # statut rendu à chaque relecture après le patch
        self.patches = []

    def get(self, kind, ns, name):
        assert kind == "addons.harvesterhci.io"
        if self.obj is None:
            return None
        if self.patches and self.statuses:
            st = self.statuses.pop(0)
            self.obj["status"] = st if isinstance(st, dict) else {"status": st}
        return self.obj

    def patch(self, kind, ns, name, patch):
        self.patches.append(patch)
        self.obj["spec"].update(patch["spec"])


def addon(enabled, status):
    return {"metadata": {"name": "descheduler", "namespace": "kube-system"},
            "spec": {"enabled": enabled, "chart": "descheduler"}, "status": {"status": status}}


def run(kube, want, timeout=600):
    t = [0.0]
    return hres.set_addon(kube, "kube-system", "descheduler", want, timeout=timeout,
                          sleep=lambda s: t.__setitem__(0, t[0] + s), now=lambda: t[0])


def test_enabling_waits_for_the_deployment(capsys):
    kube = FakeKube(addon(False, "AddonDisabled"),
                    ["AddonDisabled", "AddonEnabling", "AddonEnabling", "AddonDeploySuccessful"])
    assert run(kube, True) == hres.EXIT_OK
    assert kube.patches == [{"spec": {"enabled": True}}]
    err = capsys.readouterr().err
    assert "STEP_EVENT|wait|running|AddonEnabling" in err
    assert "STEP_EVENT|wait|done|AddonDeploySuccessful" in err


def test_a_stale_success_is_not_read_as_the_end():
    """Juste après le patch, le statut est encore celui de l'état précédent :
    un add-on qu'on désactive est encore « AddonDeploySuccessful »."""
    kube = FakeKube(addon(True, "AddonDeploySuccessful"),
                    ["AddonDeploySuccessful", "AddonDisabling", "AddonDisabled"])
    assert run(kube, False) == hres.EXIT_OK
    assert kube.patches == [{"spec": {"enabled": False}}]


def test_a_failure_is_said(capsys):
    failed = {"status": "AddonDeployFailed", "conditions": [
        {"type": "OperationFailed", "status": "True", "message": "chart not found"}]}
    kube = FakeKube(addon(False, "AddonDisabled"), ["AddonEnabling", failed])
    assert run(kube, True) == hres.EXIT_FAIL
    assert "STEP_EVENT|wait|error|chart not found" in capsys.readouterr().err


def test_nothing_to_do_when_already_there(capsys):
    kube = FakeKube(addon(True, "AddonDeploySuccessful"), [])
    assert run(kube, True) == hres.EXIT_OK
    assert kube.patches == []
    assert "already enabled" in capsys.readouterr().err


def test_a_slow_addon_times_out(capsys):
    kube = FakeKube(addon(False, "AddonDisabled"), ["AddonEnabling"] * 500)
    assert run(kube, True, timeout=60) == hres.EXIT_FAIL
    assert "still AddonEnabling after 60 s" in capsys.readouterr().err


def test_an_unknown_addon_is_refused():
    with pytest.raises(ValueError, match="no add-on"):
        run(FakeKube(None, []), True)


def test_the_cli_parses():
    with pytest.raises(SystemExit):
        hres.main(["addon", "--name", "x"])                 # --namespace et --enable manquent


def test_a_webhook_refusal_is_said_plainly(capsys, monkeypatch):
    """Vu sur harv1 (un seul nœud) : Harvester refuse d'activer le
    descheduler. Le message garde la raison, sans l'enveloppe de kubectl."""
    class Refusing(FakeKube):
        def patch(self, *a):
            raise hres.KubeError('Error from server (BadRequest): admission webhook '
                                 '"validator.harvesterhci.io" denied the request: descheduler addon '
                                 'cannot be enabled as not enough nodes exist in the cluster')
    monkeypatch.setattr(hres, "kube_from", lambda args: Refusing(addon(False, "AddonDisabled"), []))
    assert hres.main(["addon", "--namespace", "kube-system", "--name", "descheduler", "--enable"]) == hres.EXIT_FAIL
    err = capsys.readouterr().err
    assert "STEP_EVENT|addon|error|Harvester refused: descheduler addon cannot be enabled" in err
