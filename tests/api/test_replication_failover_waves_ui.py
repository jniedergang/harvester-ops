"""Écrans de réplication (BE1) : déroulé de la bascule par vagues, actions
annexes HTTP, VMs candidates, sans cluster.

Banc simulé du déroulé (`test_replication_run_199.World`) ; les actions
annexes appellent un vrai serveur HTTP local (`http.server`) qui note chaque
requête et répond selon le chemin (`/ok`, `/fail` en 500, `/slow` au-delà du
délai)."""

import http.server
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import replication as rp  # noqa: E402
import replication_db as db  # noqa: E402
import replication_run as rr  # noqa: E402
from test_replication_run_199 import VMS, World, _vm, events  # noqa: E402

SECRET = "Bearer t0p-s3cr3t-value"


class _Handler(http.server.BaseHTTPRequestHandler):
    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        self.server.seen.append({"method": self.command, "path": self.path, "body": body,
                                 "headers": {k.lower(): v for k, v in self.headers.items()}, "at": time.time()})
        if self.path.startswith("/slow"):
            time.sleep(2)
        code = 500 if self.path.startswith("/fail") else 204
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any

    def log_message(self, *a):
        pass


def allow_loopback(monkeypatch, tmp_path):
    """Le serveur de test écoute en 127.0.0.1 : la garde anti-SSRF le refuse
    sauf autorisation de l'opérateur, posée ici dans un config.yaml."""
    cfg = tmp_path / "allow-config.yaml"
    cfg.write_text("replication:\n  hooks:\n    allow_private: [127.0.0.0/8]\n"
                   "  notifications:\n    allow_private: [127.0.0.0/8]\n")
    monkeypatch.setenv("HARVESTER_OPS_CONFIG", str(cfg))


@pytest.fixture
def server(monkeypatch, tmp_path):
    for v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(v, raising=False)
    allow_loopback(monkeypatch, tmp_path)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.seen = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    srv.server_close()


def hook(srv, name, at, path="/ok", **kw):
    return dict({"name": name, "at": at, "method": "POST", "url": f"{srv.url}{path}?h={name}",
                 "headers": {"X-Plan": "erp"}, "secret_headers": {"Authorization": SECRET},
                 "body": json.dumps({"hook": name}), "timeout_s": 5}, **kw)


def protected(tmp_path, vms=("vm-a", "vm-b"), **opts):
    w = World(tmp_path, vms=vms, options=opts)
    res = rr.protect(w.ctx())
    assert res["exit"] == 0, res
    return w


def agent_ready(kube):
    """L'agent invité répond dès que la VMI existe (condition KubeVirt)."""
    real = kube.put

    def put(kind, obj):
        if kind == rr.K_VMI:
            obj = dict(obj, status=dict(obj.get("status") or {},
                                        conditions=[{"type": "AgentConnected", "status": "True"}]))
        return real(kind, obj)
    kube.put = put


def order(w):
    """Suite des événements de la bascule qui comptent pour l'ordre."""
    out = []
    for e in events(w, operation="failover"):
        if e["step"] == "wave":
            out.append(f"{e['code']}:{json.loads(e['facts'])['index']}")
        elif e["step"] == "hook":
            out.append(f"{e['code']}:{json.loads(e['facts'])['name']}")
        elif e["step"] == "vm" and e["code"] == "start":
            out.append(f"vm:{e['resource_uid'][:1]}")
    return out


def journal_text(w):
    return json.dumps([dict(r) for r in w.conn.execute("SELECT * FROM events").fetchall()]) + \
        json.dumps(db.list_runs(w.conn, w.pid))


# ---------------------------------------------------------------------------
# Vagues et actions dans l'ordre
# ---------------------------------------------------------------------------

def test_failover_brings_vms_up_by_waves_with_hooks_at_their_points(tmp_path, server):
    w = protected(tmp_path, waves=[{"name": "back", "vms": ["default/vm-b"], "gate": {"kind": "agent",
                                                                                      "seconds": 120}}],
                  hooks=[hook(server, "announce", "start"), hook(server, "after-back", "wave:1"),
                         hook(server, "after-front", "wave:2"), hook(server, "done", "end")])
    agent_ready(w.dst)
    res = rr.failover(w.ctx())
    assert res["exit"] == 0, res
    assert order(w) == ["hook-ok:announce", "wave-start:1", "vm:b", "wave-passed:1", "hook-ok:after-back",
                        "wave-start:2", "vm:a", "wave-passed:2", "hook-ok:after-front", "hook-ok:done"]
    # appels réels, dans le même ordre, avec en-têtes et corps
    assert [r["path"].split("h=")[1] for r in server.seen] == ["announce", "after-back", "after-front", "done"]
    first = server.seen[0]
    assert first["method"] == "POST" and first["headers"]["authorization"] == SECRET
    assert first["headers"]["x-plan"] == "erp" and json.loads(first["body"]) == {"hook": "announce"}
    assert [h["name"] for h in res["hook_results"]] == ["announce", "after-back", "after-front", "done"]
    assert all(h["ok"] and h["status"] == 204 for h in res["hook_results"])
    ok = events(w, step="hook", code="hook-ok")[0]
    assert set(json.loads(ok["facts"])) == {"name", "at", "status", "seconds"}
    # le secret n'est jamais au journal ni dans la ligne de la bascule
    assert SECRET not in journal_text(w) and "t0p-s3cr3t" not in json.dumps(res)
    assert w.plan()["state"] == "failed_over"
    assert db.list_runs(w.conn, w.pid)[-1]["points"]["hooks_done"] == ["announce", "after-back", "after-front",
                                                                       "done"]


def test_a_failed_hook_warns_and_the_failover_goes_on(tmp_path, server):
    dead = f"http://127.0.0.1:{_free_port()}"
    w = protected(tmp_path, vms=("vm-a",),
                  hooks=[hook(server, "boom", "start", "/fail"), dict(hook(server, "nobody", "start"), url=dead + "/x"),
                         hook(server, "slow", "end", "/slow", timeout_s=1), hook(server, "fine", "end")])
    res = rr.failover(w.ctx())
    assert res["exit"] == 0 and w.plan()["state"] == "failed_over", res
    by = {h["name"]: h for h in res["hook_results"]}
    assert by["boom"]["ok"] is False and by["boom"]["status"] == 500
    assert by["nobody"]["ok"] is False and by["nobody"]["status"] is None
    assert by["slow"]["ok"] is False and by["fine"]["ok"] is True
    failed = events(w, step="hook", code="hook-failed")
    assert {json.loads(e["facts"])["name"] for e in failed} == {"boom", "nobody", "slow"}
    assert all(e["level"] == "warn" for e in failed)
    assert SECRET not in journal_text(w)


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.parametrize("gate,agent,passed,min_wait", [
    ({"kind": "running"}, False, True, 0),
    ({"kind": "agent", "seconds": 30}, True, True, 0),
    ({"kind": "agent", "seconds": 30}, False, False, 30),          # pas d'agent : avertissement, on continue
    ({"kind": "delay", "seconds": 45}, False, True, 45),
])
def test_wave_gates(tmp_path, gate, agent, passed, min_wait):
    w = protected(tmp_path, waves=[{"vms": ["default/vm-b"], "gate": gate}, {"vms": ["default/vm-a"]}])
    if agent:
        agent_ready(w.dst)
    res = rr.failover(w.ctx())
    assert res["exit"] == 0 and w.plan()["state"] == "failed_over", res
    start = events(w, step="wave", code="wave-start")[0]
    end = [e for e in events(w, step="wave") if e["code"] in ("wave-passed", "wave-gate-timeout")][0]
    assert end["code"] == ("wave-passed" if passed else "wave-gate-timeout")
    assert end["level"] == ("info" if passed else "warn")
    assert json.loads(end["facts"])["gate"]["kind"] == gate["kind"]
    waited = (rp._t(end["ts"]) - rp._t(start["ts"])).total_seconds()
    assert waited >= min_wait
    # la seconde vague ne démarre qu'après la condition de la première
    vm_a = [e for e in events(w, step="vm", code="start") if e["resource_uid"] == VMS["vm-a"]["uid"]][0]
    assert vm_a["id"] > end["id"]


def test_gates_do_not_wait_for_vms_left_stopped(tmp_path):
    w = protected(tmp_path, vms=("vm-c",), start="stopped",
                  waves=[{"vms": ["default/vm-c"], "gate": {"kind": "agent", "seconds": 3600}}])
    t0 = w.clock.now()
    res = rr.failover(w.ctx())
    assert res["exit"] == 0, res
    assert events(w, step="wave", code="wave-passed") and not events(w, step="wave", code="wave-gate-timeout")
    assert w.clock.now() - t0 < 3600


def test_partial_failover_skips_empty_waves_and_their_hooks(tmp_path, server):
    w = protected(tmp_path, waves=[{"vms": ["default/vm-b"]}],
                  hooks=[hook(server, "w1", "wave:1"), hook(server, "w2", "wave:2"), hook(server, "s", "start")])
    res = rr.failover(w.ctx(), vms=["default/vm-a"])
    assert res["exit"] == 0, res
    assert order(w) == ["hook-ok:s", "wave-start:2", "vm:a", "wave-passed:2", "hook-ok:w2"]
    assert [r["path"].split("h=")[1] for r in server.seen] == ["s", "w2"]


def test_dry_run_lists_waves_and_hooks_without_calling_them(tmp_path, server):
    w = protected(tmp_path, waves=[{"name": "db", "vms": ["default/vm-b"], "gate": {"kind": "delay",
                                                                                    "seconds": 10}}],
                  hooks=[hook(server, "announce", "start")])
    res = rr.failover(w.ctx(), dry_run=True)
    assert res["exit"] == 0, res
    assert res["waves"] == [
        {"index": 1, "name": "db", "vms": ["default/vm-b"], "gate": {"kind": "delay", "seconds": 10}},
        {"index": 2, "name": "", "vms": ["default/vm-a"], "gate": {"kind": "running", "seconds": 0},
         "implicit": True}]
    # adresse montrée sans sa requête (un jeton y est souvent porté)
    assert res["hooks"] == [{"name": "announce", "at": "start", "method": "POST",
                             "url": f"{server.url}/ok?********"}]
    assert "h=announce" not in json.dumps(res)
    assert server.seen == [] and "t0p" not in json.dumps(res)
    assert w.plan()["state"] == "protected"


# ---------------------------------------------------------------------------
# Annulation, arrêt de la console, reprise : sémantique d'avant
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["cancel", "suspend"])
def test_a_stop_during_a_gate_keeps_the_activation_and_resume_does_not_recall_hooks(tmp_path, server, how):
    w = protected(tmp_path, waves=[{"vms": ["default/vm-b"], "gate": {"kind": "delay", "seconds": 600}}],
                  hooks=[hook(server, "announce", "start"), hook(server, "after", "wave:1"),
                         hook(server, "done", "end")])
    ctx = w.ctx()
    real = ctx._sleep
    if how == "cancel":
        def sleep(s):
            if ctx.step == "wave":
                raise rr.Cancelled("signal 15")
            real(s)
        ctx._sleep = sleep
    else:
        ctx.suspend_requested = lambda: "console stop" if ctx.step == "wave" else None
    res = rr.failover(ctx)
    assert res["code"] == "interrupted" and res["exit"] == 3, res
    # après l'activation : rien n'est défait
    assert w.plan()["state"] == "error" and db.error_origin(w.conn, w.pid) == "failing_over"
    assert events(w, step="done", code="failed-over", resource_uid=VMS["vm-b"]["uid"])
    assert not events(w, step="done", code="failed-over", resource_uid=VMS["vm-a"]["uid"])
    assert all(w.dst.obj(rr.K_LH_VOLUME, rr.NS, v["target_volume"])["spec"]["Standby"] is False
               for v in db.list_volumes(w.conn, w.pid))
    assert [r["path"].split("h=")[1] for r in server.seen] == ["announce"]
    res = rr.resume(w.ctx())
    assert res["exit"] == 0 and w.plan()["state"] == "failed_over", res
    # `announce` n'est pas rappelée ; la vague 1 (VM déjà là) repasse sa
    # condition, puis ses actions et la suite
    assert [r["path"].split("h=")[1] for r in server.seen] == ["announce", "after", "done"]
    assert {r["state"] for r in db.list_resources(w.conn, w.pid)} == {"failed_over"}


def test_cancel_before_activation_still_rolls_back_without_any_hook(tmp_path, server):
    w = protected(tmp_path, vms=("vm-a",), hooks=[hook(server, "announce", "start")])
    w.dst.freeze.add("apply")
    res = rr.failover(w.ctx(timeouts={"resync_apply": 30}))
    assert res["exit"] == 1 and "undone" in res["message"]
    assert w.plan()["state"] == "protected" and server.seen == []


# ---------------------------------------------------------------------------
# hook test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,ok,status", [("/ok", True, 204), ("/fail", False, 500)])
def test_hook_test_calls_one_hook_and_writes_the_journal(tmp_path, server, path, ok, status):
    w = World(tmp_path, vms=("vm-a",), options={"hooks": [hook(server, "ping", "end", path, method="PUT")]})
    res, h = rr.hook_test(w.conn, w.plan(), "ping", by="alice", action_id="act123456789")
    assert res["ok"] is ok and res["status"] == status and h["name"] == "ping"
    assert server.seen[0]["method"] == "PUT" and server.seen[0]["headers"]["authorization"] == SECRET
    [e] = events(w, step="hook")
    assert e["operation"] == "plan" and e["code"] == ("hook-ok" if ok else "hook-failed")
    assert e["by"] == "alice" and e["action_id"] == "act123456789"
    assert json.loads(e["facts"])["test"] is True and SECRET not in journal_text(w)
    with pytest.raises(KeyError):
        rr.hook_test(w.conn, w.plan(), "nope")


def test_call_hook_scrubs_secret_values_from_errors():
    def connector(*a):
        class C:
            def request(self, *a, **k):
                raise OSError(f"proxy said no to {SECRET}")

            def close(self):
                pass
        return C()
    res = rr.call_hook({"name": "x", "at": "end", "method": "GET", "url": "http://h.example/",
                        "secret_headers": {"Authorization": SECRET}, "timeout_s": 3}, allow=(),
                       resolver=lambda *a: [(2, 1, 6, "", ("93.184.216.34", 80))], connector=connector)
    assert res["ok"] is False and SECRET not in res["error"]


def test_get_hooks_send_no_body(server):
    rr.call_hook({"name": "x", "at": "end", "method": "GET", "url": server.url + "/ok", "body": "ignored",
                  "timeout_s": 3})
    assert server.seen[0]["method"] == "GET" and server.seen[0]["body"] == ""


# ---------------------------------------------------------------------------
# VMs candidates
# ---------------------------------------------------------------------------

def test_candidates_of_a_source_cluster(tmp_path):
    w = World(tmp_path, vms=("vm-a", "vm-b"))
    src = w.src
    # une VM à containerDisk, une VM à volume chiffré, une VM étiquetée sans disque
    cd = _vm("vm-cd", "uid-cd", ["data"])
    cd["spec"]["template"]["spec"]["volumes"] = [{"name": "boot", "containerDisk": {"image": "x"}}]
    src.put(rr.K_VM, cd)
    enc = _vm("vm-enc", "uid-enc", ["data"])
    src.put(rr.K_VM, enc)
    src.put(rr.K_PVC, {"metadata": {"name": "vm-enc-data", "namespace": "default"},
                       "spec": {"volumeName": "pvc-enc", "storageClassName": "harvester-longhorn",
                                "resources": {"requests": {"storage": "1Gi"}}}})
    src.put(rr.K_PV, {"metadata": {"name": "pvc-enc"}, "spec": {"csi": {"volumeHandle": "pvc-enc"}}})
    src.put(rr.K_LH_VOLUME, {"metadata": {"name": "pvc-enc", "namespace": rr.NS},
                             "spec": {"encrypted": True, "size": str(1024 ** 3)}, "status": {"actualSize": "52428800"}})
    data = rr.candidates(src, "harvlab", held=rr.plans_holding(w.conn, "harvlab"))
    by = {v["name"]: v for v in data["vms"]}
    assert data["cluster"] == "harvlab" and set(by) == {"vm-a", "vm-b", "vm-cd", "vm-enc"}
    a = by["vm-a"]
    assert a["eligible"] and a["reasons"] == [] and a["running"] and a["cloud_init"] == "nocloud"
    assert a["plans"] == [w.pid] and a["uid"] == VMS["vm-a"]["uid"] and a["networks"] == ["default/prod"]
    disks = {d["disk"]: d for d in a["disks"]}
    assert disks["root"]["image"] == "default/img1" and disks["root"]["storage_class"] == "lh-img1"
    assert disks["data"]["size"] == 2 * 1024 ** 3 and disks["data"]["eligible"] and disks["data"]["reason"] == ""
    assert by["vm-cd"]["eligible"] is False and set(by["vm-cd"]["reasons"]) == {"containerDisk", "no-disk"}
    assert by["vm-cd"]["disks"] == [{"disk": "boot", "claim": "", "size": 0, "used": 0, "storage_class": "",
                                     "image": "", "eligible": False, "reason": "containerDisk"}]
    enc_disk = by["vm-enc"]["disks"][0]
    assert enc_disk["reason"] == "encrypted" and enc_disk["used"] == 52428800 and enc_disk["size"] == 1024 ** 3
    assert by["vm-enc"]["eligible"] is False and by["vm-enc"]["plans"] == []
    assert set(a) == {"namespace", "name", "uid", "running", "labels", "cpu", "memory", "networks", "cloud_init",
                      "disks", "eligible", "reasons", "plans"}


def test_candidates_by_selector_and_held_plans(tmp_path):
    w = World(tmp_path, vms=("vm-a", "vm-b"))
    vm = w.src.obj(rr.K_VM, "default", "vm-b")
    vm["metadata"]["labels"] = {"dr": "gold"}
    data = rr.candidates(w.src, "harvlab", "default", "dr=gold", held={VMS["vm-b"]["uid"]: ["rp-ffffffffffff"]})
    assert [v["name"] for v in data["vms"]] == ["vm-b"]
    assert data["vms"][0]["labels"] == {"dr": "gold"} and data["vms"][0]["plans"] == ["rp-ffffffffffff"]
    # un plan déprotégé ne tient plus ses VMs ; un plan d'un autre sens non plus
    assert rr.plans_holding(w.conn, "harvlab2") == {}
    db.set_state(w.conn, w.pid, "draft", "protecting")
    db.set_state(w.conn, w.pid, "protecting", "unprotecting")
    db.set_state(w.conn, w.pid, "unprotecting", "unprotected")
    assert rr.plans_holding(w.conn, "harvlab") == {}
