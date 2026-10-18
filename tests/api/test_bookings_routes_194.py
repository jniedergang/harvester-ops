"""v1.94.0 : les routes des réservations.

Lectures pour tous (lecteur compris) ; créer et annuler sont des actions
suivies qui lancent bin/harvester-bookings.py avec la base de la console et
l'inventaire GPU lu par la console avec l'identité de la personne. Conflit
refusé tout de suite (409, raisons dites). Un opérateur annule les siennes,
un admin celles de tout le monde. Rien n'est écrit sur un cluster."""

import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")
import app as wapp  # noqa: E402

SRC = (ROOT / "web" / "app.py").read_text()
# le script lancé pour de vrai lit l'heure réelle : les créneaux sont
# posés après-demain, à partir de maintenant
NOW = datetime.now(timezone.utc).replace(microsecond=0)
DAY = (NOW + timedelta(days=2)).replace(hour=0, minute=0, second=0)


class _NoThread:
    started = []

    def __init__(self, target=None, args=(), **k):
        self.target, self.args = target, args

    def start(self):
        _NoThread.started.append(self)


def unit(uid, typ, model, state="free", total=1, used=0, **extra):
    return dict({"id": uid, "type": typ, "cluster": "harv", "node": "n1", "name": uid, "model": model,
                 "address": "", "state": state, "total": total, "used": used, "free": total - used}, **extra)


UNITS = [unit("v1", "vgpu", "NVIDIA H100-20C", "used", used=1,
              consumers=[{"kind": "vm", "cluster": "harv", "namespace": "ml", "name": "nb"}]),
         unit("v2", "vgpu", "NVIDIA H100-20C"),
         unit("m1/mig", "mig", "MIG 3g.40gb x2", total=2, used=1,
              profiles=[{"name": "MIG 3g.40gb", "id": 9, "created": 2, "available": 0, "total": 2}]),
         unit("n3/nvidia.com/gpu", "node", "nvidia.com/gpu", total=4, used=2, resource="nvidia.com/gpu")]


@pytest.fixture
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [{"name": "harv", "kubeconfig": "/k/harv.yaml"}]})
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: {"harv": "/staged/alice-harv.yaml"}.get(c))
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc: True)
    state = {"reads": [], "status": 200}

    def collect(cluster, kc):
        state["reads"].append((cluster, kc))
        if state["status"] != 200:
            return {"error": "kubectl failed", "denied": True, "detail": "forbidden"}, 500, []
        return {"cluster": cluster, "units": [dict(u) for u in UNITS], "addon": True}, 200, []
    monkeypatch.setattr(wapp, "_gpus_collect", collect)
    monkeypatch.setenv("HARVESTER_OPS_BOOKINGS_DB", str(tmp_path / "bookings.db"))
    monkeypatch.setattr(wapp, "_bookings_now", lambda: NOW)
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: {"user": "alice"})
    monkeypatch.setattr(wapp, "_capi_work_dir", lambda: tmp_path)
    monkeypatch.setattr(wapp.threading, "Thread", _NoThread)
    _NoThread.started = []
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    yield state
    wapp.ACTIONS.clear()
    wapp.ACTIONS.update(saved)


@pytest.fixture
def client(base, monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")            # mode ouvert : admin
    return wapp.app.test_client()


@pytest.fixture
def as_user(base, monkeypatch, tmp_path):
    """Un client au nom d'un compte du fichier de rôles."""
    (tmp_path / "htpasswd").write_text("")
    roles = tmp_path / "roles.yaml"
    roles.write_text("default_role: viewer\nusers:\n  patronne: admin\n  alice: operator\n"
                     "  bob: operator\n  curieux: viewer\n")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    monkeypatch.setattr(wapp, "ROLES_PATH", roles)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: True)

    def call(user, method, path, body=None):
        token = base64.b64encode(f"{user}:x".encode()).decode()
        with wapp.app.test_client() as c:
            return c.open(path, method=method, json=body, headers={"Authorization": f"Basic {token}"})
    return call


def T(h):
    return (DAY + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def body(**over):
    b = {"cluster": "harv", "type": "vgpu", "model": "NVIDIA H100-20C", "count": 1, "start": T(9), "end": T(13)}
    b.update(over)
    return b


def script_cmd():
    t = _NoThread.started[-1]
    cell = dict(zip(t.target.__code__.co_freevars, t.target.__closure__ or ()))
    return cell["cmd"].cell_contents


def run_script():
    """Lance pour de vrai ce que le fil de travail lancerait (la base est
    temporaire, l'inventaire est dans la demande)."""
    r = subprocess.run(script_cmd(), capture_output=True, text=True, timeout=60)
    return r


def test_create_starts_a_tracked_action_and_the_script_books(client, base):
    r = client.post("/api/bookings", json=body(count=2, target={"kind": "vm", "namespace": "ml", "name": "nb"}))
    assert r.status_code == 202, r.get_json()
    d = r.get_json()
    [run] = wapp.ACTIONS.values()
    assert d["action_id"] == run.id and d["enforced"] is False
    assert run.action == f"bookings:create:{d['id']}" and run.cluster == "harv"
    assert base["reads"] == [("harv", "/staged/alice-harv.yaml")]     # inventaire lu avec l'identité
    cmd = script_cmd()
    assert cmd[1].endswith("bin/harvester-bookings.py") and cmd[2] == "create"
    assert cmd[cmd.index("--id") + 1] == d["id"] and cmd[cmd.index("--as") + 1] == "anonymous"
    spec = json.loads(Path(cmd[cmd.index("--spec") + 1]).read_text())
    assert spec["inventory"][0]["id"] == "v1" and spec["model"] == "NVIDIA H100-20C"
    assert spec["holder"] == {"kind": "user", "name": "anonymous"}
    shown = " ".join(run.cmd)
    assert "/tmp" not in shown and str(wapp._bookings_db_path()) not in shown
    assert run.cmd[:2] == ["harvester-bookings", "create"] and "--db" not in run.cmd
    r = run_script()
    assert r.returncode == 0, r.stderr
    listed = client.get("/api/bookings").get_json()
    [b] = listed["bookings"]
    assert (b["id"], b["state"], b["count"], b["mine"], b["can_cancel"]) == (d["id"], "upcoming", 2, True, True)
    assert listed["enforced"] is False


def test_conflict_is_refused_with_its_reason_and_starts_nothing(client):
    client.post("/api/bookings", json=body(count=2))
    assert run_script().returncode == 0
    wapp.ACTIONS.clear()
    _NoThread.started.clear()
    r = client.post("/api/bookings", json=body(start=T(12), end=T(14)))
    assert r.status_code == 409
    d = r.get_json()
    assert d["error"] == "conflict" and d["capacity"] == 2
    assert d["conflicts"][0]["code"] == "capacity" and d["conflicts"][0]["at"] == T(12)
    assert f"3 x vgpu NVIDIA H100-20C would be booked at {T(12)}, the cluster has 2" in d["hint"]
    assert wapp.ACTIONS == {} and _NoThread.started == []
    # bout à bout : accepté
    assert client.post("/api/bookings", json=body(start=T(13), end=T(14))).status_code == 202


def test_check_tells_without_writing(client):
    r = client.post("/api/bookings/check", json=body(unit="v2", model=""))
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] is True and d["booking"]["model"] == "NVIDIA H100-20C"
    r = client.post("/api/bookings/check", json=body(count=3))
    d = r.get_json()
    assert d["ok"] is False and d["conflicts"][0]["message"].startswith("3 x vgpu")
    assert wapp.ACTIONS == {} and client.get("/api/bookings").get_json()["bookings"] == []


@pytest.mark.parametrize("over,code,msg", [
    ({"end": T(8)}, 400, "end must come after"),
    ({"type": "tpu"}, 400, "type must be"),
    ({"unit": "zz", "model": ""}, 400, "not in the GPU inventory"),
    ({"cluster": "nope"}, 404, "unknown cluster"),
    ({"end": (DAY + timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%SZ")}, 400, "90 days"),
])
def test_refusals(client, over, code, msg):
    r = client.post("/api/bookings", json=body(**over))
    assert r.status_code == code and msg in r.get_json()["error"]
    assert wapp.ACTIONS == {}


def test_inventory_unreadable_or_cluster_off_is_said(client, base, monkeypatch):
    base["status"] = 500
    r = client.post("/api/bookings", json=body())
    assert r.status_code == 403 and "GPU inventory" in r.get_json()["error"]
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc: False)
    monkeypatch.setattr(wapp, "_cluster_api_endpoint", lambda kc: ("10.0.0.1", 6443))
    r = client.post("/api/bookings", json=body())
    assert r.status_code == 503 and r.get_json()["unreachable"] is True
    assert wapp.ACTIONS == {}


def test_options_give_capacity_free_now_and_my_bookings(client, monkeypatch):
    client.post("/api/bookings", json=body(start=(NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), end=T(13), unit="v2", model=""))
    assert run_script().returncode == 0
    d = client.get("/api/bookings/options/harv").get_json()
    cat = {(e["type"], e["model"]): e for e in d["catalog"]}
    vg = cat[("vgpu", "NVIDIA H100-20C")]
    assert (vg["capacity"], vg["used"], vg["booked_now"], vg["free_now"]) == (2, 1, 1, 1)
    assert len(vg["mine_now"]) == 1
    assert {u["id"]: u["booked_now"] for u in vg["units"]} == {"v1": False, "v2": True}
    assert cat[("mig", "MIG 3g.40gb")]["capacity"] == 2
    assert cat[("node", "nvidia.com/gpu")]["free_now"] == 2
    assert client.get("/api/bookings/options/nope").status_code == 404


# -- rôles ----------------------------------------------------------------------------

def test_viewer_reads_but_cannot_book(as_user):
    assert as_user("curieux", "GET", "/api/bookings").status_code == 200
    assert as_user("curieux", "GET", "/api/bookings/options/harv").status_code == 200
    r = as_user("curieux", "POST", "/api/bookings", body())
    assert r.status_code == 403 and r.get_json()["required"] == "operator"
    assert wapp.ACTIONS == {}


def test_operator_cancels_own_admin_cancels_any(as_user):
    r = as_user("alice", "POST", "/api/bookings", body())
    assert r.status_code == 202
    bid = r.get_json()["id"]
    cmd = script_cmd()
    assert cmd[cmd.index("--as") + 1] == "alice"
    assert run_script().returncode == 0
    lst = as_user("bob", "GET", "/api/bookings").get_json()["bookings"]
    assert [(b["mine"], b["can_cancel"]) for b in lst] == [(False, False)]
    assert as_user("bob", "GET", "/api/bookings?scope=mine").get_json()["bookings"] == []
    r = as_user("bob", "POST", f"/api/bookings/{bid}/cancel")
    assert r.status_code == 403 and "only the bookings they created" in r.get_json()["hint"]
    r = as_user("alice", "POST", f"/api/bookings/{bid}/cancel")
    assert r.status_code == 202
    cmd = script_cmd()
    assert cmd[2] == "cancel" and cmd[cmd.index("--role") + 1] == "operator" and cmd[cmd.index("--as") + 1] == "alice"
    [run] = [x for x in wapp.ACTIONS.values() if x.action.startswith("bookings:cancel")]
    assert run.action == f"bookings:cancel:{bid}"
    # l'admin, sur celle d'un autre
    r = as_user("bob", "POST", "/api/bookings", body(start=T(14), end=T(15)))
    bid2 = r.get_json()["id"]
    assert run_script().returncode == 0
    assert as_user("patronne", "GET", "/api/bookings").get_json()["bookings"][-1]["can_cancel"] is True
    r = as_user("patronne", "POST", f"/api/bookings/{bid2}/cancel")
    assert r.status_code == 202
    cmd = script_cmd()
    assert cmd[cmd.index("--role") + 1] == "admin"
    assert run_script().returncode == 0
    states = {b["id"]: b["state"] for b in as_user("bob", "GET", "/api/bookings").get_json()["bookings"]}
    assert states[bid2] == "cancelled"
    # déjà annulée, inconnue, id invalide
    assert as_user("patronne", "POST", f"/api/bookings/{bid2}/cancel").status_code == 409
    assert as_user("patronne", "POST", "/api/bookings/bk-ffffffffffff/cancel").status_code == 404
    assert as_user("patronne", "POST", "/api/bookings/bk-XYZ/cancel").status_code in (400, 404)


ROUTES = ['"/api/bookings/check", methods=["POST"]', '"/api/bookings", methods=["POST"]',
          '"/api/bookings/<bid>/cancel", methods=["POST"]']


@pytest.mark.parametrize("route", ROUTES)
def test_every_mutation_route_is_authenticated_and_rate_limited(route):
    m = re.search(r'@app\.route\(' + re.escape(route) + r'\)\n@requires_auth\n@_rate_limit\("([^"]+)"\)', SRC)
    assert m, route
    wapp._validate_rate_spec(m.group(1))


@pytest.mark.parametrize("route", ['"/api/bookings")', '"/api/bookings/options/<cluster>")'])
def test_reads_are_authenticated(route):
    assert re.search(r'@app\.route\(' + re.escape(route) + r'\n@requires_auth\n', SRC), route


def test_signed_out_is_refused(base, monkeypatch, tmp_path):
    (tmp_path / "htpasswd").write_text("u:x\n")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    c = wapp.app.test_client()
    assert c.get("/api/bookings").status_code == 401
    assert c.post("/api/bookings", json=body()).status_code == 401
