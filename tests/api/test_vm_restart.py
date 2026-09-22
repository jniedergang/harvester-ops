"""v1.44.5 : la réinitialisation brutale redémarre la VM, quelle que soit sa
stratégie de démarrage.

Constaté sur harvlab avec deux consoles ouvertes : le bouton de
réinitialisation SUPPRIMAIT le VMI, en croyant faire comme `virtctl
restart`. Un VMI supprimé ne revient que si la VM est en `runStrategy:
Always` ; Harvester crée ses VMs en `RerunOnFailure`. La VM s'arrêtait, et
l'action finissait quand même en « done » au bout de 180 s.

`virtctl restart` passe par la sous-ressource `restart` de la VM, qui
redémarre quelle que soit la stratégie ; `gracePeriodSeconds: 0` la rend
immédiate (vérifié sur harvlab : nouvelle instance en 18 s).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "web"))
import app as wapp  # noqa: E402


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Run:
    def __init__(self):
        self.events = []
        self.status = None
        self.error_summary = None
        self.exit_code = None
        self.ended_at = None

    def emit(self, e):
        self.events.append(e)

    def close(self):
        pass


def fake_kubectl(monkeypatch, vmi_answers, restart_rc=0, restart_err=""):
    """vmi_answers : réponses successives de `get vmi` (uid phase) ; la
    première sert à relever l'instance d'avant."""
    calls = []
    answers = iter(vmi_answers)

    def run(cmd, **kw):
        calls.append((cmd, kw.get("input")))
        if "replace" in cmd:
            return R(restart_rc, "", restart_err)
        if "get" in cmd and "vmi" in cmd:
            try:
                return R(0, next(answers))
            except StopIteration:
                return R(1, "", "not found")
        return R(0)

    monkeypatch.setattr(wapp.subprocess, "run", run)
    monkeypatch.setattr(wapp.time, "sleep", lambda s: None)
    return calls


def test_the_restart_subresource_is_used_never_a_vmi_deletion(monkeypatch):
    calls = fake_kubectl(monkeypatch, ["old", "new Running"])
    run = Run()
    wapp._vm_restart_runner(run, "/kc", "default", "web")
    cmds = [c for c, _ in calls]
    assert not any("delete" in c for c in cmds)
    restart = next((c, i) for c, i in calls if "replace" in c)
    assert "--raw" in restart[0]
    assert ("/apis/subresources.kubevirt.io/v1/namespaces/default/virtualmachines/web/restart"
            in restart[0])
    assert json.loads(restart[1]) == {"gracePeriodSeconds": 0}
    assert run.status == "done"


def test_a_vm_that_does_not_come_back_is_an_error_not_a_success(monkeypatch):
    fake_kubectl(monkeypatch, ["old"] + ["old Running"] * 5)
    monkeypatch.setattr(wapp, "VM_RESTART_TIMEOUT", 0)
    run = Run()
    wapp._vm_restart_runner(run, "/kc", "default", "web")
    assert run.status == "error" and run.exit_code == 1
    assert "did not come back" in run.error_summary


def test_a_refused_restart_is_reported_as_is(monkeypatch):
    fake_kubectl(monkeypatch, ["old"], restart_rc=1,
                 restart_err="Error from server (Conflict): VM is not running\n")
    run = Run()
    wapp._vm_restart_runner(run, "/kc", "default", "web")
    assert run.status == "error"
    assert run.error_summary == "Error from server (Conflict): VM is not running"
