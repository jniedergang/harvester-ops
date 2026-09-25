"""v1.46.0 : la progression d'une action, et un flux qui ne se fige plus.

Une action garde ses 500 derniers événements. Le flux SSE les parcourait par
position : passé 500, les plus anciens sortaient, la file ne grandissait
plus, et le flux n'envoyait plus rien (le dock se figeait). Il suit
désormais un numéro absolu.

La progression d'un transfert (débit, temps restant, quantités) ne va jamais
dans cette file : un point par phase, remplacé à chaque mise à jour, rendu
par `to_dict()` et diffusé à part.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")
import app as wapp  # noqa: E402


def run():
    r = wapp.ActionRun("prog00000001", "vm-transfer:default/x", "harvlab", ["x"])
    r.close = lambda: None
    return r


def test_events_since_keeps_up_past_the_cap():
    r = run()
    for i in range(600):
        r.emit({"type": "step", "n": i})
    evs, seq = r.events_since(0)
    # les 100 premiers sont sortis de la file : on reçoit ce qui reste
    assert [e["n"] for e in evs] == list(range(100, 600)) and seq == 600
    r.emit({"type": "step", "n": 600})
    evs, seq = r.events_since(seq)
    assert [e["n"] for e in evs] == [600] and seq == 601
    evs, seq = r.events_since(seq)
    assert evs == [] and seq == 601


def test_progress_keeps_one_point_per_phase_outside_the_log():
    r = run()
    for done in range(0, 1000, 10):
        r.emit_progress({"phase": "import", "done": done, "total": 1000})
    r.emit_progress({"phase": "freeze", "done": 5, "total": 5, "final": True})
    assert len(r.events) == 0
    d = r.to_dict()
    assert d["progress"]["import"]["done"] == 990
    assert d["progress"]["freeze"]["final"] is True
    assert d["progress_current"]["phase"] == "freeze"
    assert r.progress_ver == 101


def test_the_runner_relays_progress_lines(monkeypatch):
    class P:
        def __init__(self, lines):
            self.stderr = iter(lines)

        def wait(self):
            return 0

        def poll(self):
            return 0

    snap = {"phase": "import", "done": 5, "total": 10, "rate": 2.5, "eta": 2, "final": False}
    lines = ["STEP_EVENT|import|running|disk-0: importing\n",
             "PROGRESS_EVENT|import|" + json.dumps(snap) + "\n",
             "PROGRESS_EVENT|import|{not json\n",
             "PROGRESS_EVENT|broken\n"]
    monkeypatch.setattr(wapp.subprocess, "Popen", lambda *a, **k: P(lines))
    r = run()
    wapp._vm_transfer_runner(r, ["x"])
    assert r.status == "done"
    assert r.progress == {"import": snap}
    assert all(e.get("type") != "progress" for e in r.events)


def test_the_stream_sends_progress_and_does_not_stall(monkeypatch):
    r = run()
    r.close = wapp.ActionRun.close.__get__(r)
    monkeypatch.setattr(wapp, "_actions_persist", lambda *_: None)
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[r.id] = r
    try:
        for i in range(520):
            r.emit({"type": "step", "step_id": "s", "status": "running", "message": str(i)})
        r.emit_progress({"phase": "import", "done": 1, "total": 2})
        r.status = "done"
        r.close()
        client = wapp.app.test_client()
        body = client.get(f"/api/stream/{r.id}").get_data(as_text=True)
        assert body.count("event: step") == 500
        assert "event: progress" in body and '"phase": "import"' in body
        assert body.rstrip().splitlines()[-1].startswith("data:")
        assert "event: end" in body
    finally:
        with wapp.ACTIONS_LOCK:
            wapp.ACTIONS.pop(r.id, None)
