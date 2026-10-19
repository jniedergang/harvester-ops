"""v1.95.0 : l'échantillonneur des GPU et les routes de Usage.

L'échantillonneur lit l'inventaire de chaque cluster déclaré avec le
kubeconfig de la console (collecteur simulé ici), range les unités tenues
dans une base temporaire, note les clusters injoignables sans s'arrêter, et
efface les relevés de plus de 400 jours. Un seul fil, jamais dans un
processus lecteur.

Routes : le rapport et son CSV pour tous (lecteur compris), les quotas lus
par tous, réglés et retirés par un admin seulement, en action suivie qui
lance bin/harvester-bookings.py quota."""

import base64
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
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)


class _NoThread:
    started = []

    def __init__(self, target=None, args=(), **k):
        self.target, self.args = target, args

    def start(self):
        _NoThread.started.append(self)

    def is_alive(self):
        return True


UNITS = [
    {"id": "g1", "type": "gpu", "model": "H100", "consumers": [{"kind": "vm", "namespace": "prod", "name": "train"}]},
    {"id": "v1", "type": "vgpu", "model": "H100-20C", "consumers": [{"kind": "vm", "namespace": "ml", "name": "nb"}]},
    {"id": "m/mig", "type": "mig", "model": "MIG", "consumers": [{"kind": "vm", "namespace": "ml", "name": "nb"}]},
    {"id": "n3/nvidia.com/gpu", "type": "node", "model": "nvidia.com/gpu",
     "consumers": [{"kind": "pod", "namespace": "ml", "name": "trainer-7b-x", "workload": "trainer", "count": 2}]},
]


@pytest.fixture
def sampler(monkeypatch, tmp_path):
    kcs = {}
    for n in ("alpha", "beta", "gamma", "delta"):
        kcs[n] = tmp_path / f"{n}.yaml"
        kcs[n].write_text("x")
    clusters = [{"name": n, "kubeconfig": str(k)} for n, k in kcs.items()] + [{"name": "ghost", "kubeconfig": ""}]
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"clusters": clusters})
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc: not kc.endswith("beta.yaml"))
    monkeypatch.setenv("HARVESTER_OPS_USAGE_DB", str(tmp_path / "usage.db"))
    calls = []

    def collect(name, kc):
        calls.append((name, kc))
        if name == "gamma":
            return {"error": "kubectl failed", "denied": True, "detail": "forbidden"}, 500, ["forbidden"]
        if name == "delta":
            raise RuntimeError("boom")
        return {"units": UNITS, "addon": True}, 200, []
    return {"collect": collect, "calls": calls, "db": tmp_path / "usage.db", "kcs": kcs}


def test_one_round_writes_used_units_and_survives_bad_clusters(sampler):
    out = wapp._usage_sample_once(collect=sampler["collect"], now=NOW)
    assert out == {"alpha": ("ok", 4), "beta": ("unreachable", 0), "gamma": ("denied", 0),
                   "delta": ("error", 0), "ghost": ("unknown", 0)}
    # le kubeconfig de la console (lecture seule), jamais celui d'une personne
    assert sorted(sampler["calls"]) == [("alpha", str(sampler["kcs"]["alpha"])), ("delta", str(sampler["kcs"]["delta"])),
                                        ("gamma", str(sampler["kcs"]["gamma"]))]
    conn = wapp._udb.connect(sampler["db"])
    rows = conn.execute("SELECT cluster, unit, type, kind, namespace, name, workload, count, interval, ts "
                        "FROM samples ORDER BY unit").fetchall()
    assert [tuple(r) for r in rows] == [
        ("alpha", "g1", "gpu", "vm", "prod", "train", "", 1, wapp.USAGE_INTERVAL, "2026-10-19T09:00:00Z"),
        ("alpha", "n3/nvidia.com/gpu", "node", "pod", "ml", "trainer-7b-x", "trainer", 2, wapp.USAGE_INTERVAL,
         "2026-10-19T09:00:00Z"),
        ("alpha", "v1", "vgpu", "vm", "ml", "nb", "", 1, wapp.USAGE_INTERVAL, "2026-10-19T09:00:00Z")]
    runs, first = wapp._udb.last_runs(conn)
    assert {r["cluster"]: r["state"] for r in runs} == {"alpha": "ok", "beta": "unreachable", "gamma": "denied",
                                                       "delta": "error", "ghost": "unknown"}
    assert first == "2026-10-19T09:00:00Z"
    assert "boom" in {r["cluster"]: r["detail"] for r in runs}["delta"]


def test_retention_drops_samples_older_than_400_days(sampler):
    conn = wapp._udb.connect(sampler["db"])
    old = {"unit": "u", "type": "gpu", "model": "M", "kind": "vm", "namespace": "a", "name": "b", "count": 1}
    wapp._udb.record(conn, NOW - timedelta(days=401), 300, "alpha", "ok", [old])
    wapp._udb.record(conn, NOW - timedelta(days=399), 300, "alpha", "ok", [old])
    wapp._usage_sample_once(collect=sampler["collect"], now=NOW)
    stamps = sorted({r[0] for r in conn.execute("SELECT ts FROM samples")})
    assert stamps == [wapp._hbk.iso(NOW - timedelta(days=399)), "2026-10-19T09:00:00Z"]


def test_two_rounds_count_consumed_hours(sampler):
    wapp._usage_sample_once(collect=sampler["collect"], now=NOW)
    wapp._usage_sample_once(collect=sampler["collect"], now=NOW + timedelta(seconds=wapp.USAGE_INTERVAL))
    b = wapp._bkdb.connect(sampler["db"].parent / "bookings.db")
    u = wapp._udb.connect(sampler["db"])
    rep = wapp._udb.build_report(b, u, "2026-10-19T00:00:00Z", "2026-10-20T00:00:00Z", 0, NOW)
    got = {r["holder"]["name"]: r["consumed"] for r in rep["holders"]}
    step = wapp.USAGE_INTERVAL / 3600
    assert got == {"ml": pytest.approx(round(2 * step * 3, 3)), "prod": pytest.approx(round(2 * step, 3))}


def test_the_sampler_is_one_thread_never_in_a_reader_process(monkeypatch):
    monkeypatch.setattr(wapp.threading, "Thread", _NoThread)
    monkeypatch.setattr(wapp, "_usage_sampler_thread", None)
    _NoThread.started = []
    monkeypatch.setattr(wapp, "USAGE_ENABLED", True)
    monkeypatch.setattr(wapp, "IS_READ_WORKER", True)
    assert wapp._start_usage_sampler() is None and _NoThread.started == []
    monkeypatch.setattr(wapp, "IS_READ_WORKER", False)
    t = wapp._start_usage_sampler()
    assert wapp._start_usage_sampler() is t and len(_NoThread.started) == 1
    assert t.target is wapp._usage_sampler_loop
    monkeypatch.setattr(wapp, "_usage_sampler_thread", None)
    monkeypatch.setattr(wapp, "USAGE_ENABLED", False)
    assert wapp._start_usage_sampler() is None


def test_the_sampler_reuses_the_gpu_collector_and_reads_the_interval_setting():
    body = SRC.split("def _usage_sample_once(", 1)[1].split("\ndef ", 1)[0]
    assert "collect = collect or _gpus_collect" in body
    assert 'c.get("kubeconfig")' in body and "_kubectl_for_cluster" not in body
    assert 'os.environ.get("HARVESTER_OPS_USAGE_INTERVAL"' in SRC


# -- routes ----------------------------------------------------------------------------

@pytest.fixture
def base(monkeypatch, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_BOOKINGS_DB", str(tmp_path / "bookings.db"))
    monkeypatch.setenv("HARVESTER_OPS_USAGE_DB", str(tmp_path / "usage.db"))
    monkeypatch.setattr(wapp, "_bookings_now", lambda: NOW)
    monkeypatch.setattr(wapp, "_capi_work_dir", lambda: tmp_path)
    monkeypatch.setattr(wapp.threading, "Thread", _NoThread)
    _NoThread.started = []
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    # une réservation et des relevés
    b = wapp._bkdb.connect(tmp_path / "bookings.db")
    b.execute("INSERT INTO bookings (id, cluster, type, model, count, holder_kind, holder, start, \"end\","
              " target_kind, target_namespace, target_name, created_at) VALUES"
              " ('bk-aaaaaaaa0001', 'c1', 'vgpu', 'M', 1, 'user', 'alice', '2026-10-19T06:00:00Z',"
              " '2026-10-19T10:00:00Z', 'vm', 'ml', 'nb', '2026-10-18T00:00:00Z')")
    u = wapp._udb.connect(tmp_path / "usage.db")
    s = {"unit": "v1", "type": "vgpu", "model": "M", "kind": "vm", "namespace": "ml", "name": "nb", "count": 1}
    wapp._udb.record(u, "2026-10-19T07:00:00Z", 300, "c1", "ok", [s])
    yield tmp_path
    wapp.ACTIONS.clear()
    wapp.ACTIONS.update(saved)


@pytest.fixture
def as_user(base, monkeypatch, tmp_path):
    (tmp_path / "htpasswd").write_text("")
    roles = tmp_path / "roles.yaml"
    roles.write_text("default_role: viewer\nusers:\n  patronne: admin\n  alice: operator\n  curieux: viewer\n")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    monkeypatch.setattr(wapp, "ROLES_PATH", roles)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: True)

    def call(user, method, path, body=None):
        token = base64.b64encode(f"{user}:x".encode()).decode()
        with wapp.app.test_client() as c:
            return c.open(path, method=method, json=body, headers={"Authorization": f"Basic {token}"})
    return call


RANGE = "from=2026-10-19T00:00:00Z&to=2026-10-20T00:00:00Z&tz=120"


def script_cmd():
    t = _NoThread.started[-1]
    cell = dict(zip(t.target.__code__.co_freevars, t.target.__closure__ or ()))
    return cell["cmd"].cell_contents


def test_viewer_reads_the_report_and_the_csv(as_user):
    r = as_user("curieux", "GET", f"/api/usage?{RANGE}")
    assert r.status_code == 200
    d = r.get_json()
    [a] = [h for h in d["holders"] if h["holder"]["name"] == "alice"]
    assert (a["booked"], a["consumed"], d["role"], d["tz"]) == (4.0, round(5 / 60, 3), "viewer", 120)
    assert d["days"][0]["day"] == "2026-10-19" and d["sampler"]["first"] == "2026-10-19T07:00:00Z"
    assert d["interval"] == wapp.USAGE_INTERVAL and d["enforced"] is False
    r = as_user("curieux", "GET", f"/api/usage.csv?{RANGE}")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert 'filename="gpu-usage-2026-10-19-2026-10-20.csv"' in r.headers["Content-Disposition"]
    assert r.get_data(as_text=True).splitlines()[1].startswith("user,alice,,,4.0,0.083")
    assert as_user("curieux", "GET", "/api/usage/quotas").get_json() == {"quotas": [], "role": "viewer"}


def test_bad_period_is_said(as_user):
    r = as_user("curieux", "GET", "/api/usage?from=2026-10-20T00:00:00Z&to=2026-10-19T00:00:00Z")
    assert r.status_code == 400 and "after its start" in r.get_json()["error"]
    assert as_user("curieux", "GET", "/api/usage.csv?from=nope").status_code == 400


def test_only_an_admin_sets_or_removes_a_quota_as_a_tracked_action(as_user):
    q = {"holder": {"kind": "group", "name": "ml-team"}, "monthly_hours": 40, "max_units": 2}
    for who in ("curieux", "alice"):
        r = as_user(who, "POST", "/api/usage/quotas", q)
        assert r.status_code == 403 and r.get_json()["required"] == "admin"
        assert as_user(who, "DELETE", "/api/usage/quotas", {"holder": q["holder"]}).status_code == 403
    assert wapp.ACTIONS == {}
    r = as_user("patronne", "POST", "/api/usage/quotas", q)
    assert r.status_code == 202, r.get_json()
    [run] = wapp.ACTIONS.values()
    assert r.get_json()["action_id"] == run.id and run.action == "usage:quota-set:group:ml-team"
    cmd = script_cmd()
    assert cmd[1].endswith("bin/harvester-bookings.py") and cmd[2:4] == ["quota", "set"]
    assert "--group=ml-team" in cmd and cmd[cmd.index("--as") + 1] == "patronne"
    assert cmd[cmd.index("--monthly-hours") + 1] == "40.0" and cmd[cmd.index("--max-units") + 1] == "2"
    assert "--db" not in run.cmd and str(wapp._bookings_db_path()) not in " ".join(run.cmd)
    assert subprocess.run(cmd, capture_output=True, text=True, timeout=60).returncode == 0
    [saved] = as_user("curieux", "GET", "/api/usage/quotas").get_json()["quotas"]
    assert (saved["monthly_hours"], saved["max_units"], saved["updated_by"]) == (40.0, 2, "patronne")
    # le quota se voit dans le rapport, même sans activité
    rows = as_user("curieux", "GET", f"/api/usage?{RANGE}").get_json()["holders"]
    assert [h["quota"]["remaining"] for h in rows if h["holder"]["name"] == "ml-team"] == [40.0]
    # retrait
    wapp.ACTIONS.clear()
    r = as_user("patronne", "DELETE", "/api/usage/quotas", {"holder": q["holder"]})
    assert r.status_code == 202
    [run] = wapp.ACTIONS.values()
    assert run.action == "usage:quota-delete:group:ml-team"
    assert subprocess.run(script_cmd(), capture_output=True, text=True, timeout=60).returncode == 0
    assert as_user("curieux", "GET", "/api/usage/quotas").get_json()["quotas"] == []
    assert as_user("patronne", "DELETE", "/api/usage/quotas", {"holder": q["holder"]}).status_code == 404


@pytest.mark.parametrize("body,msg", [
    ({"holder": {"kind": "user", "name": "a"}}, "monthly GPU-hours cap"),
    ({"holder": {"kind": "namespace", "name": "ml"}, "monthly_hours": 3}, "person (user) or a group"),
    ({"holder": {"kind": "user", "name": "a"}, "max_units": -2}, "from 0 to"),
    ([1, 2], "JSON object"),
])
def test_quota_refusals(as_user, body, msg):
    r = as_user("patronne", "POST", "/api/usage/quotas", body)
    assert r.status_code == 400 and msg in r.get_json()["error"]
    assert wapp.ACTIONS == {}


def test_a_holder_name_starting_with_a_dash_stays_a_value(as_user):
    r = as_user("patronne", "POST", "/api/usage/quotas", {"holder": {"kind": "user", "name": "-x"}, "monthly_hours": 1})
    assert r.status_code == 202
    cmd = script_cmd()
    assert "--holder=-x" in cmd
    assert subprocess.run(cmd, capture_output=True, text=True, timeout=60).returncode == 0


def test_the_booking_check_says_the_quota(as_user, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/k")
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc: True)
    monkeypatch.setattr(wapp, "_bookings_now", lambda: datetime.now(timezone.utc))
    units = [{"id": "n/nvidia.com/gpu", "type": "node", "model": "nvidia.com/gpu", "resource": "nvidia.com/gpu",
              "state": "free", "total": 8, "used": 0, "free": 8}]
    monkeypatch.setattr(wapp, "_gpus_collect", lambda c, kc: ({"units": units, "addon": True}, 200, []))
    conn = wapp._bkdb.connect(wapp._bookings_db_path())
    wapp._bkdb.set_quota(conn, {"holder": {"kind": "user", "name": "alice"}, "monthly_hours": None, "max_units": 1},
                         "patronne", NOW)
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    body = {"cluster": "c1", "type": "node", "model": "nvidia.com/gpu", "count": 2,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": (start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    d = as_user("alice", "POST", "/api/bookings/check", body).get_json()
    assert d["ok"] is False and d["conflicts"][0]["code"] == "quota-units"
    assert "the maximum is 1" in d["conflicts"][0]["message"]
    r = as_user("alice", "POST", "/api/bookings", body)
    assert r.status_code == 409 and wapp.ACTIONS == {}


@pytest.mark.parametrize("route", ['"/api/usage/quotas", methods=["POST"]', '"/api/usage/quotas", methods=["DELETE"]'])
def test_mutation_routes_are_authenticated_and_rate_limited(route):
    m = re.search(r'@app\.route\(' + re.escape(route) + r'\)\n@requires_auth\n@_rate_limit\("([^"]+)"\)', SRC)
    assert m, route
    wapp._validate_rate_spec(m.group(1))


@pytest.mark.parametrize("route", ['"/api/usage")', '"/api/usage.csv")', '"/api/usage/quotas")'])
def test_reads_are_authenticated(route):
    assert re.search(r'@app\.route\(' + re.escape(route) + r'\n@requires_auth\n', SRC), route


def test_quota_writes_are_admin_in_the_role_model():
    assert wapp.required_role_for("/api/usage/quotas", "POST") == "admin"
    assert wapp.required_role_for("/api/usage/quotas", "DELETE") == "admin"
    assert wapp.required_role_for("/api/usage/quotas", "GET") == "viewer"
    assert wapp.required_role_for("/api/usage", "GET") == "viewer"


def test_signed_out_is_refused(base, monkeypatch, tmp_path):
    (tmp_path / "htpasswd").write_text("u:x\n")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    c = wapp.app.test_client()
    assert c.get("/api/usage").status_code == 401
    assert c.get("/api/usage.csv").status_code == 401
    assert c.post("/api/usage/quotas", json={}).status_code == 401
