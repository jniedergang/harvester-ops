"""2.5.0 (décision 174) : actions filles imbriquées sous leur action parente.

Colonne additive `parent` dans actions.db, parent posé par chaque lanceur
AVANT que la fille soit visible, Activité au premier niveau avec
`children_count`, `?parent=<id>` pour les filles, recherche à plat avec le
lien vers le parent, rétention comptée au premier niveau.
"""

import importlib
import inspect
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
app_module = importlib.import_module("app")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = tmp_path / "actions.db"
    monkeypatch.setattr(app_module, "ACTIONS_DB", d)
    monkeypatch.setattr(app_module, "ACTIONS", {})
    monkeypatch.setattr(app_module, "LOG_DIR", tmp_path / "no-logs")
    app_module._actions_init_db()
    return d


@pytest.fixture()
def client(db):
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _hex():
    return uuid.uuid4().hex[:12]


def _row(db, parent=None, started_at=None, status="done", action="shutdown", cluster="harv1", rid=None):
    rid = rid or _hex()
    now = started_at if started_at is not None else time.time()
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO actions (id, action, cluster, status, exit_code, started_at, ended_at,"
                 " dry_run, cmd, events, parent) VALUES (?,?,?,?,0,?,?,0,'','[]',?)",
                 (rid, action, cluster, status, now, now + 1, parent))
    conn.commit()
    conn.close()
    return rid


def _ids(db):
    conn = sqlite3.connect(str(db))
    out = {r[0] for r in conn.execute("SELECT id FROM actions")}
    conn.close()
    return out


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_a_240_database_is_migrated_and_its_rows_stay_top_level(tmp_path, monkeypatch, client):
    old = tmp_path / "old.db"
    conn = sqlite3.connect(str(old))
    conn.execute("""CREATE TABLE actions (id TEXT PRIMARY KEY, action TEXT NOT NULL, cluster TEXT,
        status TEXT NOT NULL, exit_code INTEGER, started_at REAL NOT NULL, ended_at REAL,
        dry_run INTEGER DEFAULT 0, cmd TEXT, events TEXT, error_summary TEXT, result TEXT, undo TEXT)""")
    ids = [_hex() for _ in range(3)]
    for i, rid in enumerate(ids):
        conn.execute("INSERT INTO actions (id, action, cluster, status, started_at) VALUES (?,?,?,?,?)",
                     (rid, f"shutdown{i}", "harv1", "done", time.time() - i))
    conn.commit()
    conn.close()
    monkeypatch.setattr(app_module, "ACTIONS_DB", old)
    app_module._actions_init_db()
    conn = sqlite3.connect(str(old))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(actions)")]
    idx = [r[1] for r in conn.execute("PRAGMA index_list(actions)")]
    rows = conn.execute("SELECT id, parent FROM actions").fetchall()
    conn.close()
    assert "parent" in cols and "actions_parent" in idx
    assert {r[0] for r in rows} == set(ids) and all(r[1] is None for r in rows)
    body = client.get("/api/activity").get_json()
    got = {a["id"]: a for a in body["actions_done"]}
    assert set(got) == set(ids)
    assert all(a["children_count"] == 0 and a["parent"] is None for a in got.values())


# ---------------------------------------------------------------------------
# Parent posé par chaque lanceur, avant que la fille soit visible
# ---------------------------------------------------------------------------

class _Recorder(dict):
    """ACTIONS qui note le parent de chaque action AU MOMENT où elle devient
    visible : une lecture concurrente ne voit que ce qui est posé alors."""
    def __init__(self):
        super().__init__()
        self.seen = {}

    def __setitem__(self, k, v):
        self.seen[k] = getattr(v, "parent", None)
        super().__setitem__(k, v)


def _noop_worker(run, *a, **kw):
    run.status = "done"
    run.ended_at = time.time()
    run.close()


def _launch_track(parent):
    return app_module.track_action("demo:x", "harv1", _noop_worker, parent=parent)


def _launch_cli(parent, monkeypatch):
    monkeypatch.setattr(app_module, "_vm_transfer_runner", lambda run, cmd, **kw: _noop_worker(run))
    with app_module.app.test_request_context("/"):
        run, err = app_module._cli_action("harv1", "capi-cluster-create:a/a",
                                          [sys.executable, "tool.py", "create"], "harvester-capi",
                                          check_busy=False, parent=parent)
    assert err is None
    return run.id


def _launch_tf(parent, monkeypatch):
    decl = {"id": "d" * 12, "cluster": "harv1", "name": "decl", "resources": [{}], "revision": 1,
            "target_kind": "harvester"}
    monkeypatch.setattr(app_module, "TF_DECLS", type("S", (), {"get": staticmethod(lambda i: decl)})())
    monkeypatch.setattr(app_module, "_tf_role_refusal", lambda *a, **kw: None)
    monkeypatch.setattr(app_module, "_tf_render_resources", lambda r, k: ([("a", 0, 0, 0, "addr")], []))
    monkeypatch.setattr(app_module, "_tf_guard_rendered", lambda r, k: [])
    monkeypatch.setattr(app_module, "_tf_decl_hash", lambda r: "h")
    monkeypatch.setattr(app_module, "_tf_decl_flock", lambda i, why: 1)
    monkeypatch.setattr(app_module, "TF_DECL_FDS", {})
    monkeypatch.setattr(app_module, "TF_DECL_RUNNING", set())
    monkeypatch.setattr(app_module, "_tf_decl_runner", lambda run, *a: _noop_worker(run))
    rid, _h = app_module._tf_decl_launch("harv1", "/kc", decl["id"], "plan", parent=parent)
    return rid


def _launch_batch(parent, monkeypatch):
    monkeypatch.setattr(app_module, "_baremetal_batch_runner", lambda run, plan, c: _noop_worker(run))
    return app_module._bm_batch_launch([], "harv9", 1, parent=parent)


def _launch_bm_track(parent, monkeypatch):
    monkeypatch.setattr(app_module, "_bm_busy", lambda host: None)
    with app_module.app.test_request_context("/"):
        rid, busy = app_module._bm_track("baremetal-install:n1", "10.0.0.1", _noop_worker, {},
                                         parent=parent)
    assert busy is None
    return rid


LAUNCHERS = [
    ("track_action", lambda p, mp: _launch_track(p)),
    ("_cli_action", _launch_cli),
    ("_tf_decl_launch", _launch_tf),
    ("_bm_batch_launch", _launch_batch),
    ("_bm_track", _launch_bm_track),
]


@pytest.mark.parametrize("name,launch", LAUNCHERS, ids=[n for n, _ in LAUNCHERS])
def test_each_launcher_sets_the_parent_before_the_child_is_visible(db, monkeypatch, name, launch):
    rec = _Recorder()
    monkeypatch.setattr(app_module, "ACTIONS", rec)
    parent = app_module.ActionRun(_hex(), "deploy:plan1", "harv1", [])
    parent.status = "running"
    dict.__setitem__(rec, parent.id, parent)
    child = launch(parent.id, monkeypatch)
    assert rec.seen[child] == parent.id, f"{name}: child visible without its parent"
    assert parent.children == [child]
    # le parent est en base dès sa première fille : la rétention ne garde
    # jamais une fille sans lui
    assert parent.id in _ids(db)
    got = None
    for _ in range(100):
        conn = sqlite3.connect(str(db))
        got = conn.execute("SELECT parent FROM actions WHERE id=?", (child,)).fetchone()
        conn.close()
        if got:
            break
        time.sleep(0.02)
    assert got == (parent.id,)


@pytest.mark.parametrize("name,launch", LAUNCHERS, ids=[n for n, _ in LAUNCHERS])
def test_without_a_parent_each_launcher_stays_top_level(db, monkeypatch, name, launch):
    rec = _Recorder()
    monkeypatch.setattr(app_module, "ACTIONS", rec)
    child = launch(None, monkeypatch)
    assert rec.seen[child] is None


def _calls(fn, names):
    """Appels de `names` dans la source de `fn`, texte de l'appel complet."""
    src = inspect.getsource(fn)
    out = []
    for n in names:
        for m in re.finditer(rf"\b{n}\(", src):
            depth, i = 0, m.end() - 1
            while True:
                c = src[i]
                depth += c == "("
                depth -= c == ")"
                i += 1
                if depth == 0:
                    break
            out.append((n, src[m.start():i]))
    return out


@pytest.mark.parametrize("fn", ["_deploy_install_phase", "_deploy_terraform_phase", "_baremetal_batch_steps"])
def test_chains_pass_their_own_id_to_every_child(fn):
    calls = _calls(getattr(app_module, fn), ["_bm_batch_launch", "_bm_track", "_tf_decl_launch"])
    assert calls, fn
    for name, text in calls:
        assert "parent=run.id" in text, f"{fn}: {name} launched without parent"


def test_three_levels_deploy_batch_install(db, client, monkeypatch):
    """deploy > baremetal-batch > baremetal-install, relus de la mémoire."""
    monkeypatch.setattr(app_module, "_bm_busy", lambda host: None)
    deploy = app_module.ActionRun(_hex(), "deploy:plan1", "harv1", [])
    deploy.status = "running"
    with app_module.ACTIONS_LOCK:
        app_module._register_action(deploy)
    hold = threading.Event()

    def slow(run, *a):
        run.status = "running"
        hold.wait(5)
        _noop_worker(run)
    monkeypatch.setattr(app_module, "_baremetal_batch_runner", lambda run, plan, c: slow(run))
    batch = app_module._bm_batch_launch([], "harv9", 1, parent=deploy.id)
    with app_module.app.test_request_context("/"):
        inst, _ = app_module._bm_track("baremetal-install:n1", "10.0.0.1", slow, {}, parent=batch)
    try:
        top = client.get("/api/activity").get_json()
        ids = [a["id"] for a in top["in_progress"] + top["actions_done"]]
        assert ids == [deploy.id]
        d = top["in_progress"][0]
        assert d["children_count"] == 1
        assert d["child_current"]["id"] == batch
        lvl2 = client.get(f"/api/activity?parent={deploy.id}").get_json()
        assert [a["id"] for a in lvl2["in_progress"]] == [batch]
        assert lvl2["in_progress"][0]["children_count"] == 1
        lvl3 = client.get(f"/api/activity?parent={batch}").get_json()
        assert [a["id"] for a in lvl3["in_progress"]] == [inst]
        assert lvl3["in_progress"][0]["parent"] == batch
    finally:
        hold.set()


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["x", "ABCDEF012345", "0123456789a", "0123456789abc", "../../etc", "' OR 1=1"])
def test_parent_query_rejects_an_invalid_identifier(client, bad):
    r = client.get("/api/activity", query_string={"parent": bad})
    assert r.status_code == 400


def test_unfiltered_list_hides_children_and_counts_them(db, client):
    p1 = _row(db, started_at=100)
    p2 = _row(db, started_at=200)
    kids = [_row(db, parent=p1, started_at=101 + i) for i in range(3)]
    grandkid = _row(db, parent=kids[0], started_at=150)
    body = client.get("/api/activity").get_json()
    got = {a["id"]: a["children_count"] for a in body["actions_done"]}
    assert got == {p1: 3, p2: 0}
    sub = client.get(f"/api/activity?parent={p1}").get_json()
    assert [a["id"] for a in sub["actions_done"]] == kids, "launch order"
    assert sub["actions_done"][0]["children_count"] == 1
    assert sub["log_files"] == []
    assert [a["id"] for a in client.get(f"/api/activity?parent={kids[0]}").get_json()["actions_done"]] == [grandkid]


def test_a_child_whose_parent_is_lost_stays_visible(db, client):
    orphan = _row(db, parent="f" * 12)
    body = client.get("/api/activity").get_json()
    assert orphan in [a["id"] for a in body["actions_done"]]


@pytest.mark.parametrize("query,key", [("q", "needle"), ("action", "needle"), ("status", "error")])
def test_a_filter_lists_flat_and_children_carry_their_parent(db, client, query, key):
    p = _row(db, action="deploy:plan1")
    child = _row(db, parent=p, action="capi-cluster-create:needle/needle", status="error")
    body = client.get("/api/activity", query_string={query: key}).get_json()
    got = {a["id"]: a for a in body["actions_done"]}
    assert child in got and got[child]["parent"] == p


# ---------------------------------------------------------------------------
# Rétention
# ---------------------------------------------------------------------------

def test_retention_counts_top_level_actions_and_children_follow_their_parent(db, monkeypatch):
    monkeypatch.setattr(app_module, "ACTIONS_KEEP", 500)
    oldest = _row(db, started_at=1)
    old_kids = [_row(db, parent=oldest, started_at=2 + i) for i in range(2)]
    old_grandkid = _row(db, parent=old_kids[0], started_at=3)
    parents = [_row(db, started_at=10 + i) for i in range(500)]
    newest = parents[-1]
    kids = [_row(db, parent=newest, started_at=1000 + i) for i in range(3)]
    conn = sqlite3.connect(str(db))
    app_module._actions_purge(conn)
    conn.close()
    left = _ids(db)
    assert oldest not in left and not (set(old_kids) | {old_grandkid}) & left
    assert set(parents) <= left, "500 top-level actions kept"
    assert set(kids) <= left, "children of a kept parent stay"
    assert len(left) == 503


def test_retention_never_drops_a_running_parent_or_its_children(db, monkeypatch):
    run_parent = _row(db, started_at=1, status="running")
    kid = _row(db, parent=run_parent, started_at=2)
    for i in range(3):
        _row(db, started_at=10 + i)
    conn = sqlite3.connect(str(db))
    app_module._actions_purge(conn, keep=1)
    conn.close()
    left = _ids(db)
    assert {run_parent, kid} <= left and len(left) == 3


def test_reload_marks_a_killed_parent_interrupted_in_the_database(db, monkeypatch):
    p = _row(db, status="running", started_at=time.time())
    kid = _row(db, parent=p)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE actions SET ended_at=NULL WHERE id=?", (p,))
    conn.commit()
    conn.close()
    app_module._actions_load_history()
    acts = app_module.ACTIONS
    assert acts[p].status == "interrupted" and acts[p].children == [kid]
    assert acts[kid].parent == p
    conn = sqlite3.connect(str(db))
    st = conn.execute("SELECT status FROM actions WHERE id=?", (p,)).fetchone()[0]
    conn.close()
    assert st == "interrupted"


# ---------------------------------------------------------------------------
# Interface : nom de fille échappé (source)
# ---------------------------------------------------------------------------

JS = ROOT / "web" / "static" / "js"


@pytest.mark.parametrize("path,needle", [
    ("dock.js", "escapeHtml(a.child_current.action)"),
    ("app.js", "escapeHtml(it.action)"),
    ("auto-history.js", "esc(o.label(a))"),
])
def test_child_names_are_escaped(path, needle):
    assert needle in (JS / path).read_text()


def test_bare_metal_history_folds_children_under_their_parent():
    """auto-history.js dans node : une fille dont le parent est dans la liste
    se range sous lui (repliée, bouton à bulle), une fille sans son parent
    reste au premier niveau."""
    import test_auto_tabs_220 as tat
    answers = {"baremetal-": {"in_progress": [], "actions_done": [
        {"id": "b1", "action": "baremetal-batch:harv9", "status": "done", "started_at": 10, "ended_at": 90,
         "cluster": "(local)", "parent": None},
        {"id": "i1", "action": "baremetal-install:<img src=x onerror=pwn()>", "status": "done",
         "started_at": 11, "ended_at": 80, "cluster": "(local)", "parent": "b1"},
        {"id": "i2", "action": "baremetal-install:n2", "status": "done", "started_at": 12, "ended_at": 70,
         "cluster": "(local)", "parent": "zzzzzzzzzzzz"}]}}
    out = tat.run_history(answers, queries=("baremetal-",))
    assert out["ids"] == ["b1", "i2"], "child folded under its parent, orphan kept"
    assert 'data-history-expand="b1"' in out["html"] and "activity.expandTip" in out["html"]
    assert "<img" not in out["html"]
