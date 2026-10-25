"""2.0.0 (tâche 14) : les routes des dossiers de la console.

Lecture de l'arborescence pour tous, comptes limités à ce que la personne
voit ; chaque geste est une action suivie qui lance bin/harvester-folders.py
avec le rôle et les kubeconfigs de la personne (jamais affichés) ; les
contrôles avant geste disent objet par objet si l'objet sera écrit, si seul
le rangement de la console changera, ou si le geste échouera."""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")
import app as wapp  # noqa: E402
import folders_db as fdb  # noqa: E402
import kvm_decl as kd  # noqa: E402
import kvm_host as kh  # noqa: E402

SRC = (ROOT / "web" / "app.py").read_text()
UUID1 = "49960a5e-a239-42da-95f6-f55383ea5c62"
import threading  # noqa: E402
REAL_THREAD = threading.Thread
KC = "/staged/secret-alice-harv1.yaml"


class _NoThread:
    started = []

    def __init__(self, target=None, args=(), **k):
        self.target, self.args = target, args

    def start(self):
        _NoThread.started.append(self)


def script_cmd():
    t = _NoThread.started[-1]
    cell = dict(zip(t.target.__code__.co_freevars, t.target.__closure__ or ()))
    return cell["cmd"].cell_contents


def script_spec():
    cmd = script_cmd()
    return json.loads(Path(cmd[cmd.index("--spec") + 1]).read_text())


@pytest.fixture
def world(monkeypatch, tmp_path):
    state = Path(tempfile.mkdtemp(prefix="fld-", dir="/tmp"))
    kd.write(state, {"name": "node2", "address": "10.0.0.9", "port": 22, "user": "ju",
                     "ssh_key": kd.key_rel("node2"), "known_hosts": kd.known_hosts_rel("node2"), "labels": {}})
    cfg = {"clusters": [{"name": "harv1", "kubeconfig": "/k/harv1.yaml"},
                        {"name": "lab", "kubeconfig": "/k/lab.yaml"}]}
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: cfg)
    monkeypatch.setattr(wapp, "_config_cluster_names", lambda: {"harv1"})
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: KC if c == "harv1" else
                        ("/staged/lab.yaml" if c == "lab" else None))
    reach = {"/staged/lab.yaml": False}
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc: reach.get(kc, True))
    db = tmp_path / "folders.db"
    monkeypatch.setenv("HARVESTER_OPS_FOLDERS_DB", str(db))
    monkeypatch.setattr(wapp, "_capi_work_dir", lambda: tmp_path)
    monkeypatch.setattr(wapp.threading, "Thread", _NoThread)
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: {"user": "alice"})
    _NoThread.started = []
    conn = fdb.connect(db)
    for p in ("/Prod", "/Prod/ERP", "/Lab"):
        fdb.ensure_path(conn, p, "console")
    fdb.upsert_membership(conn, "harvester/harv1/nav/a", "/Prod/ERP", "vm", scope="harvester/harv1")
    fdb.upsert_membership(conn, f"kvm/{UUID1}", "/Prod", "console", scope="kvm/node2")
    fdb.upsert_membership(conn, "harvester/harv1/secret/hidden", "/Prod", "vm", scope="harvester/harv1")
    conn.close()
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    yield {"db": db, "state": state, "reach": reach}
    wapp.ACTIONS.clear()
    wapp.ACTIONS.update(saved)


@pytest.fixture
def call(world, monkeypatch, tmp_path):
    """Requête au nom d'un compte du fichier de rôles (None : mode ouvert, admin)."""
    (tmp_path / "htpasswd").write_text("")
    roles = tmp_path / "roles.yaml"
    roles.write_text("default_role: viewer\nusers:\n  patronne: admin\n  alice: operator\n  curieux: viewer\n")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    monkeypatch.setattr(wapp, "ROLES_PATH", roles)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: True)

    def do(user, method, path, body=None, raw=None):
        token = base64.b64encode(f"{user}:x".encode()).decode()
        with wapp.app.test_client() as c:
            kw = {"data": raw, "content_type": "application/json"} if raw is not None else {"json": body}
            return c.open(path, method=method, headers={"Authorization": f"Basic {token}"}, **kw)
    return do


def fid(world, path):
    conn = fdb.connect(world["db"])
    try:
        return fdb.id_of(conn, path)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rôles : la règle par défaut (operator pour toute écriture) est figée
# ---------------------------------------------------------------------------
MUTATIONS = [("POST", "/api/folders"), ("PATCH", "/api/folders/1"), ("DELETE", "/api/folders/1"),
             ("POST", "/api/folders/1/empty-into"), ("POST", "/api/folders/check"),
             ("POST", "/api/folders/assign"), ("POST", "/api/folders/actions/abc/undo"),
             ("POST", "/api/folders/1/write-back"), ("POST", "/api/folders/actions/abc/resume")]


@pytest.mark.parametrize("method,path", MUTATIONS)
def test_every_folder_gesture_needs_the_operator_role(method, path):
    assert wapp.required_role_for(path, method) == "operator"
    assert not path.startswith(wapp.ADMIN_ONLY_PREFIXES)


def test_reading_the_tree_is_for_everyone():
    assert wapp.required_role_for("/api/folders", "GET") == "viewer"


def test_folders_routes_mutation_role(monkeypatch, call):
    """Mutation volontaire : la route assign déclarée en lecteur dans une copie
    de la règle ; le cas « lecteur refusé » doit alors échouer."""
    real = wapp.required_role_for

    def mutated(path, method):
        return "viewer" if path == "/api/folders/assign" else real(path, method)
    monkeypatch.setattr(wapp, "required_role_for", mutated)
    r = call("curieux", "POST", "/api/folders/assign", {"objects": ["host/kvm/node2"], "to": "unfiled"})
    assert r.status_code != 403            # la garde est tombée : le test des rôles le voit


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/folders", {"parent_id": None, "name": "Ops"}),
    ("PATCH", "/api/folders/{prod}", {"name": "Prod2"}),
    ("POST", "/api/folders/assign", {"objects": ["host/kvm/node2"], "to": "{lab}"}),
    ("POST", "/api/folders/{prod}/write-back", {}),
])
@pytest.mark.parametrize("user,code", [("curieux", 403), ("alice", 202), ("patronne", 202)])
def test_roles_on_each_gesture(world, call, method, path, body, user, code):
    p = path.format(prod=fid(world, "/Prod"))
    b = json.loads(json.dumps(body).replace('"{lab}"', str(fid(world, "/Lab"))))
    r = call(user, method, p, b)
    assert r.status_code == code, r.get_json()
    if code == 202:
        assert re.match(r"^[0-9a-f]{12}$", r.get_json()["action_id"])
        cmd = script_cmd()
        assert cmd[cmd.index("--role") + 1] == ("admin" if user == "patronne" else "operator")
        assert cmd[cmd.index("--as") + 1] == user


# ---------------------------------------------------------------------------
# Chaque route : réussite et erreurs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,path,body,raw,code,message", [
    ("POST", "/api/folders", None, "[1]", 400, "JSON object"),
    ("POST", "/api/folders", {"name": "a/b"}, None, 400, "'/'"),
    ("POST", "/api/folders", {"name": "x" * 64}, None, 400, "63"),
    ("POST", "/api/folders", {"name": 3}, None, 400, "text"),
    ("POST", "/api/folders", {"name": "N", "parent_id": 999}, None, 404, "no folder 999"),
    ("POST", "/api/folders", {"name": "N", "parent_id": "x"}, None, 400, "folder id"),
    ("PATCH", "/api/folders/999", {"name": "N"}, None, 404, "no folder"),
    ("PATCH", "/api/folders/{prod}", {}, None, 400, "either name or parent_id"),
    ("PATCH", "/api/folders/{prod}", {"name": "a", "parent_id": None}, None, 400, "either"),
    ("PATCH", "/api/folders/{prod}", {"position": 2}, None, 400, "sorted by name"),
    ("PATCH", "/api/folders/{prod}", {"name": ".."}, None, 400, ""),
    ("PATCH", "/api/folders/{prod}", None, "nope", 400, "JSON object"),
    ("DELETE", "/api/folders/999", None, None, 404, "no folder"),
    ("DELETE", "/api/folders/{prod}", None, None, 409, "not empty"),
    ("POST", "/api/folders/{prod}/empty-into", {"target_id": 999}, None, 404, "no folder"),
    ("POST", "/api/folders/{prod}/empty-into", {"target_id": "{erp}"}, None, 400, "itself"),
    ("POST", "/api/folders/{prod}/empty-into", {}, None, 400, "folder id or 'unfiled'"),
    ("POST", "/api/folders/999/empty-into", {"target_id": "unfiled"}, None, 404, "no folder"),
    ("POST", "/api/folders/assign", {"objects": [], "to": "unfiled"}, None, 400, "non-empty list"),
    ("POST", "/api/folders/assign", {"objects": ["bogus/x"], "to": "unfiled"}, None, 400, "invalid object key"),
    ("POST", "/api/folders/assign", {"objects": ["harvester/nocluster/ns/a"], "to": "unfiled"}, None, 400,
     "unknown cluster"),
    ("POST", "/api/folders/assign", {"objects": ["harvester/harv1/NS/a"], "to": "unfiled"}, None, 400,
     "invalid object key"),
    ("POST", "/api/folders/assign", {"objects": ["container/harv1/ns/job/j"], "to": "unfiled"}, None, 400,
     "cannot be filed"),
    ("POST", "/api/folders/assign", {"objects": [f"kvm/{UUID1}"], "to": "unfiled"}, None, 200, ""),
    ("POST", "/api/folders/assign", {"objects": [{"key": "kvm/not-a-uuid", "host": "node2"}], "to": "unfiled"}, None, 400,
     "invalid object key"),
    ("POST", "/api/folders/assign", {"objects": [{"key": "kvm/6b128cd4-f440-482d-8e90-1eb32a2c2094"}],
                                     "to": "unfiled"}, None, 400, "unknown KVM host"),
    ("POST", "/api/folders/assign", {"objects": ["host/kvm/ghost"], "to": "unfiled"}, None, 400, "not declared"),
    ("POST", "/api/folders/assign", {"objects": ["host/kvm/node2"], "to": 999}, None, 404, "no folder"),
    ("POST", "/api/folders/assign", {"objects": ["host/kvm/node2"] * 501, "to": "unfiled"}, None, 400, "500"),
    ("POST", "/api/folders/actions/bad%20id/undo", None, None, 400, "invalid action"),
    ("POST", "/api/folders/actions/unknown/undo", None, None, 404, "no folder action"),
    ("POST", "/api/folders/999/write-back", None, None, 404, "no folder"),
    ("POST", "/api/folders/check", {"objects": ["host/kvm/node2"], "to": 999}, None, 404, "no folder"),
    ("POST", "/api/folders/check", {"objects": "x"}, None, 400, "non-empty list"),
])
def test_route_errors(world, call, method, path, body, raw, code, message):
    p = path.format(prod=fid(world, "/Prod"))
    b = json.loads(json.dumps(body).replace('"{erp}"', str(fid(world, "/Prod/ERP")))) if body is not None else None
    r = call("alice", method, p, b, raw=raw)
    if code == 200:                          # cas valide de la table : l'action part
        assert r.status_code == 202
        return
    assert r.status_code == code, r.get_json()
    assert message in r.get_json()["error"]
    assert _NoThread.started == []


def test_create_launches_the_command_with_the_parent(world, call):
    r = call("alice", "POST", "/api/folders", {"parent_id": fid(world, "/Prod"), "name": "Run"})
    assert r.status_code == 202
    cmd = script_cmd()
    assert cmd[cmd.index("create"):cmd.index("create") + 5] == ["create", "--parent", str(fid(world, "/Prod")),
                                                                 "--name", "Run"]
    run = wapp.ACTIONS[r.get_json()["action_id"]]
    assert run.action == "folders:create:Run" and "undo" not in run.to_dict()


def test_rename_sends_the_members_with_kvm_on_its_host_by_uuid(world, call):
    r = call("alice", "PATCH", f"/api/folders/{fid(world, '/Prod')}", {"name": "Production"})
    assert r.status_code == 202
    objs = {o["key"]: o for o in script_spec()["objects"]}
    assert set(objs) == {"harvester/harv1/nav/a", f"kvm/{UUID1}", "harvester/harv1/secret/hidden"}
    assert objs[f"kvm/{UUID1}"] == {"kind": "kvm", "uuid": UUID1, "host": "node2", "domain": UUID1,
                                    "key": f"kvm/{UUID1}"}
    assert script_spec()["kubeconfigs"] == {"harv1": KC}


def test_move_folder_to_the_root(world, call):
    r = call("alice", "PATCH", f"/api/folders/{fid(world, '/Prod/ERP')}", {"parent_id": None})
    assert r.status_code == 202
    cmd = script_cmd()
    assert cmd[cmd.index("move-folder"):cmd.index("move-folder") + 5] == [
        "move-folder", "--folder", str(fid(world, "/Prod/ERP")), "--parent", "/"]


def test_delete_of_an_empty_folder_and_the_non_empty_refusal_never_counts_hidden(world, call):
    r = call("alice", "DELETE", f"/api/folders/{fid(world, '/Lab')}")
    assert r.status_code == 202
    r = call("alice", "DELETE", f"/api/folders/{fid(world, '/Prod')}")
    d = r.get_json()
    assert r.status_code == 409 and d["nonempty"] is True and d["subfolders"] == 1
    # aucune lecture de flotte n'a été faite : rien n'est compté, le reste est dit caché
    assert d["count_visible"] == 0 and d["hidden"] is True


def test_non_empty_refusal_counts_what_the_person_read(world, call, monkeypatch):
    seen = {"api_vms_all": {"vms": [{"key": "harvester/harv1/nav/a"}, {"key": f"kvm/{UUID1}"}]}}
    monkeypatch.setattr(wapp, "_peek_query", lambda ep, q: seen.get(ep))
    d = call("alice", "DELETE", f"/api/folders/{fid(world, '/Prod')}").get_json()
    assert d["count_visible"] == 2 and d["hidden"] is True        # la VM de « secret » n'est pas comptée


def test_empty_into_launches_one_undoable_action(world, call):
    r = call("alice", "POST", f"/api/folders/{fid(world, '/Prod')}/empty-into", {"target_id": "unfiled"})
    assert r.status_code == 202
    cmd = script_cmd()
    assert cmd[cmd.index("empty-into"):cmd.index("empty-into") + 5] == [
        "empty-into", "--folder", str(fid(world, "/Prod")), "--to", "unfiled"]
    d = wapp.ACTIONS[r.get_json()["action_id"]].to_dict()
    assert d["undo"] == {"path": f"/api/folders/actions/{d['id']}/undo"}


def test_assign_spec_carries_objects_and_the_person_kubeconfig_never_shown(world, call):
    body = {"objects": ["harvester/harv1/nav/b", {"key": f"kvm/{UUID1}", "host": "node2"},
                        "container/harv1/web/deployment/front", "host/harvester/harv1", "host/kvm/node2"],
            "to": fid(world, "/Lab")}
    r = call("alice", "POST", "/api/folders/assign", body)
    assert r.status_code == 202
    spec = script_spec()
    kinds = sorted((o["kind"], o.get("workload_kind")) for o in spec["objects"])
    assert kinds == [("container", "Deployment"), ("harvester", None), ("host", None), ("host", None), ("kvm", None)]
    assert spec["kubeconfigs"] == {"harv1": KC}
    run = wapp.ACTIONS[r.get_json()["action_id"]]
    shown = json.dumps(run.to_dict()) + " ".join(run.cmd)
    assert KC not in shown and "secret-alice" not in shown and str(world["db"]) not in shown
    # les chemins (« /Lab ») sont masqués de la ligne montrée, comme tout chemin ; le libellé le dit
    assert run.cmd[:6] == ["harvester-folders", "--as", "alice", "--role", "operator", "assign"]
    assert run.action == "folders:assign:/Lab"
    assert r.get_json().keys() == {"action_id"}


def test_undo_is_launched_for_a_finished_action_only(world, call):
    conn = fdb.connect(world["db"])
    fdb.record_move(conn, "act1", 0, "harvester/harv1/nav/a", "/Lab", "/Prod/ERP", "done")
    conn.close()
    running = wapp.ActionRun("act1", "folders:assign:/Prod/ERP", "folders", ["x"])
    running.status = "running"
    wapp.ACTIONS["act1"] = running
    assert call("alice", "POST", "/api/folders/actions/act1/undo").status_code == 409
    running.status = "done"
    r = call("alice", "POST", "/api/folders/actions/act1/undo")
    assert r.status_code == 202
    cmd = script_cmd()
    assert cmd[cmd.index("undo"):cmd.index("undo") + 3] == ["undo", "--action", "act1"]
    assert [o["key"] for o in script_spec()["objects"]] == ["harvester/harv1/nav/a"]


SEEN = {"api_vms_all": {"vms": [{"key": "harvester/harv1/nav/a"}, {"key": f"kvm/{UUID1}"}]}}


def _undo_req(world):
    conn = fdb.connect(world["db"])
    fdb.record_move(conn, "act9", 0, "harvester/harv1/secret/hidden", "/Lab", "/Prod", "done")
    fdb.record_move(conn, "act9", 1, "harvester/harv1/nav/a", "/Lab", "/Prod/ERP", "done")
    conn.close()
    return "POST", "/api/folders/actions/act9/undo", None


@pytest.mark.parametrize("gesture", ["rename", "move", "empty-into", "undo"])
def test_visible_sent_to_the_command_is_only_what_the_person_sees(world, call, monkeypatch, gesture):
    """Revue I-1 : `visible` = les clés de SES lectures, jamais les objets de
    la demande ; un objet rangé qu'elle ne voit pas (namespace `secret`) est
    dans la demande (la console le réécrit) mais pas dans `visible`."""
    monkeypatch.setattr(wapp, "_peek_query", lambda ep, q: SEEN.get(ep))
    req = {"rename": lambda: ("PATCH", f"/api/folders/{fid(world, '/Prod')}", {"name": "P2"}),
           "move": lambda: ("PATCH", f"/api/folders/{fid(world, '/Prod/ERP')}", {"parent_id": None}),
           "empty-into": lambda: ("POST", f"/api/folders/{fid(world, '/Prod')}/empty-into", {"target_id": "unfiled"}),
           "undo": lambda: _undo_req(world)}[gesture]()
    assert call("alice", *req).status_code == 202
    spec = script_spec()
    assert "harvester/harv1/secret/hidden" not in spec["visible"]
    assert {"harvester/harv1/nav/a", f"kvm/{UUID1}"} <= set(spec["visible"])
    if gesture != "move":
        assert "harvester/harv1/secret/hidden" in {o["key"] for o in spec["objects"]}


def test_assign_counts_the_chosen_objects_as_visible(world, call, monkeypatch):
    monkeypatch.setattr(wapp, "_peek_query", lambda ep, q: None)
    body = {"objects": ["harvester/harv1/nav/b"], "to": fid(world, "/Lab")}
    assert call("alice", "POST", "/api/folders/assign", body).status_code == 202
    assert "harvester/harv1/nav/b" in script_spec()["visible"]


def test_undo_survives_a_console_restart(world, monkeypatch, tmp_path):
    """Revue I-4 : `run.undo` est gardé dans actions.db (colonne additive) ;
    après un redémarrage, l'Activité propose toujours Annuler."""
    db = tmp_path / "actions.db"
    monkeypatch.setattr(wapp, "ACTIONS_DB", db)
    wapp._actions_init_db()
    run = wapp.ActionRun("fa0000000001", "folders:rename:/Prod", "folders", ["x"])
    run.undo = {"path": "/api/folders/actions/fa0000000001/undo"}
    run.status, run.ended_at = "done", run.started_at + 1
    plain = wapp.ActionRun("fa0000000002", "folders:create:/X", "folders", ["x"])
    plain.status, plain.ended_at = "done", plain.started_at + 1
    wapp._actions_persist(run)
    wapp._actions_persist(plain)
    rows = {r["id"]: r for r in wapp._actions_db_recent(10)}
    assert rows["fa0000000001"]["undo"] == run.undo
    assert "undo" not in rows["fa0000000002"]
    assert wapp._actions_db_get("fa0000000001")[0]["undo"] == run.undo
    wapp.ACTIONS.clear()
    wapp._actions_load_history()
    assert wapp.ACTIONS["fa0000000001"].to_dict()["undo"] == run.undo
    assert "undo" not in wapp.ACTIONS["fa0000000002"].to_dict()


def test_actions_db_undo_column_is_additive(monkeypatch, tmp_path):
    """Une base d'avant la colonne `undo` est migrée à chaud, sans réécriture."""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE actions (id TEXT PRIMARY KEY, action TEXT NOT NULL, cluster TEXT, "
                 "status TEXT NOT NULL, exit_code INTEGER, started_at REAL NOT NULL, ended_at REAL, "
                 "dry_run INTEGER DEFAULT 0, cmd TEXT, events TEXT, error_summary TEXT, result TEXT)")
    conn.execute("INSERT INTO actions(id, action, status, started_at) VALUES ('old1', 'x', 'done', 1)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(wapp, "ACTIONS_DB", db)
    wapp._actions_init_db()
    assert [r["id"] for r in wapp._actions_db_recent(5)] == ["old1"]


@pytest.mark.parametrize("source", ["memory", "history"])
def test_undo_of_another_persons_action_says_whose_and_by_whom(world, call, monkeypatch, tmp_path, source):
    """Revue M-4 : un opérateur peut annuler le geste d'un autre (règle
    gardée) ; l'action d'annulation dit de qui était le geste et qui l'annule."""
    conn = fdb.connect(world["db"])
    fdb.record_move(conn, "act7", 0, "harvester/harv1/nav/a", "/Lab", "/Prod/ERP", "done")
    conn.close()
    first = wapp.ActionRun("act7", "folders:assign:/Prod/ERP", "folders",
                           ["harvester-folders", "--as", "alice", "--role", "operator", "assign"])
    first.status = "done"
    if source == "memory":
        wapp.ACTIONS["act7"] = first
    else:
        monkeypatch.setattr(wapp, "ACTIONS_DB", tmp_path / "a.db")
        wapp._actions_init_db()
        wapp._actions_persist(first)
    r = call("patronne", "POST", "/api/folders/actions/act7/undo")
    assert r.status_code == 202
    cmd = script_cmd()
    assert cmd[cmd.index("undo"):cmd.index("undo") + 5] == ["undo", "--action", "act7", "--of", "alice"]
    run = wapp.ACTIONS[r.get_json()["action_id"]]
    assert run.result == {"undoes": "act7", "of": "alice", "by": "patronne"}


@pytest.mark.parametrize("method,path,body,clash", [
    # créer à la racine un nom déjà pris
    ("POST", "/api/folders", lambda w: {"parent_id": None, "name": "Lab"}, "Lab"),
    # créer sous /Prod un nom déjà pris
    ("POST", "/api/folders", lambda w: {"parent_id": fid(w, "/Prod"), "name": "ERP"}, "ERP"),
    # renommer /Lab en Prod (frère à la racine)
    ("PATCH", "/api/folders/{Lab}", lambda w: {"name": "Prod"}, "Prod"),
    # déplacer /Prod/ERP sous la racine où ERP... n'existe pas : pas de conflit (cas témoin plus bas)
    # déplacer /Lab sous /Prod alors qu'un /Prod/Lab existe
    ("PATCH", "/api/folders/{Lab}", lambda w: {"parent_id": fid(w, "/Prod")}, "Lab"),
])
def test_name_clash_is_refused_with_409_before_any_action(world, call, method, path, body, clash):
    """Revue M-1 : un doublon de nom entre frères est refusé (409) avant le
    202, comme pour empty-into ; aucune action n'est lancée."""
    conn = fdb.connect(world["db"])
    fdb.ensure_path(conn, "/Prod/Lab", "console")
    conn.close()
    r = call("alice", method, path.format(Lab=fid(world, "/Lab")), body(world))
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["conflicts"] == [clash]
    assert _NoThread.started == [] and wapp.ACTIONS == {}


@pytest.mark.parametrize("method,path,body", [
    ("PATCH", "/api/folders/{Lab}", {"name": "Lab"}),                 # même nom : lui-même
    ("PATCH", "/api/folders/{ERP}", {"parent_id": None}),             # pas d'ERP à la racine
    ("POST", "/api/folders", {"parent_id": None, "name": "lab"}),     # la casse distingue (index SQLite)
])
def test_no_clash_launches_the_action(world, call, method, path, body):
    r = call("alice", method, path.format(Lab=fid(world, "/Lab"), ERP=fid(world, "/Prod/ERP")), body)
    assert r.status_code == 202


def test_resume_relaunches_the_lines_not_done_of_an_interrupted_action(world, call):
    """Revue M-5 : reprendre une action de dossiers interrompue depuis la
    console (comme `resume` en ligne de commande)."""
    conn = fdb.connect(world["db"])
    fdb.record_move(conn, "act5", 0, "harvester/harv1/nav/a", "/Lab", "/Prod/ERP", "done")
    fdb.record_move(conn, "act5", 1, f"kvm/{UUID1}", "/Lab", "/Prod", "pending")
    conn.close()
    running = wapp.ActionRun("act5", "folders:assign:/Prod", "folders", ["x"])
    running.status = "running"
    wapp.ACTIONS["act5"] = running
    assert call("alice", "POST", "/api/folders/actions/act5/resume").status_code == 409
    running.status = "interrupted"
    assert call("curieux", "POST", "/api/folders/actions/act5/resume").status_code == 403
    assert call("alice", "POST", "/api/folders/actions/nope/resume").status_code == 404
    r = call("alice", "POST", "/api/folders/actions/act5/resume")
    assert r.status_code == 202
    cmd = script_cmd()
    assert cmd[cmd.index("resume"):cmd.index("resume") + 3] == ["resume", "--action", "act5"]
    assert [o["key"] for o in script_spec()["objects"]] == [f"kvm/{UUID1}"]
    run = wapp.ACTIONS[r.get_json()["action_id"]]
    assert run.action == "folders:resume:act5" and run.undo is None


def test_write_back_sends_only_console_filings(world, call):
    r = call("alice", "POST", f"/api/folders/{fid(world, '/Prod')}/write-back")
    assert r.status_code == 202
    assert [o["key"] for o in script_spec()["objects"]] == [f"kvm/{UUID1}"]


# ---------------------------------------------------------------------------
# Arborescence et comptes
# ---------------------------------------------------------------------------
def test_tree_counts_only_what_the_person_saw(world, call, monkeypatch):
    seen = {"api_vms_all": {"vms": [{"key": "harvester/harv1/nav/a"}, {"key": f"kvm/{UUID1}"}]}}
    monkeypatch.setattr(wapp, "_peek_query", lambda ep, q: seen.get(ep))
    r = call("curieux", "GET", "/api/folders")
    assert r.status_code == 200
    tree = {f["path"]: f for f in r.get_json()["tree"]}
    assert tree["/Prod"]["counts"] == {"vms": 2, "containers": None, "hosts": 0}   # la VM cachée n'est pas comptée
    assert tree["/Prod/ERP"]["counts"]["vms"] == 1
    assert {"id", "parent_id", "name", "position", "path", "origin", "counts"} <= tree["/Lab"].keys()
    assert "created_by" not in tree["/Lab"]


def test_tree_without_any_read_counts_nothing(world, call, monkeypatch):
    monkeypatch.setattr(wapp, "_peek_query", lambda ep, q: None)
    tree = {f["path"]: f for f in call("curieux", "GET", "/api/folders").get_json()["tree"]}
    assert tree["/Prod"]["counts"]["vms"] is None


# ---------------------------------------------------------------------------
# Contrôles avant geste
# ---------------------------------------------------------------------------
@pytest.fixture
def pool(world, monkeypatch):
    """Les contrôles lisent en parallèle (ThreadPoolExecutor) : vrais fils."""
    monkeypatch.setattr(wapp.threading, "Thread", REAL_THREAD)


def fake_can_i(answers):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        name = argv[argv.index("patch") + 1].split("/", 1)[1]
        rc, out, err = answers[name]
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)
    return run, calls


@pytest.mark.parametrize("answer,write,outcome,reason", [
    ((0, "yes\n", ""), True, "object", "written on it"),
    ((1, "no\n", ""), False, "console", "only the console's filing changes"),
    ((1, "", f"error: open {KC}: permission denied"), False, "fail", "<kubeconfig>"),
])
def test_check_harvester_by_access_review(pool, world, call, monkeypatch, answer, write, outcome, reason):
    run, calls = fake_can_i({"a": answer})
    monkeypatch.setattr(wapp, "_kubectl_run", run)
    r = call("alice", "POST", "/api/folders/check", {"objects": ["harvester/harv1/nav/a"], "to": "unfiled"})
    d = r.get_json()["objects"][0]
    assert (d["write"], d["outcome"], d["check"]) == (write, outcome, "ssar")
    assert reason in d["reason"] and KC not in json.dumps(d)
    assert calls[0] == ["kubectl", "--kubeconfig", KC, "auth", "can-i", "patch",
                        "virtualmachines.kubevirt.io/a", "-n", "nav"]


def test_check_workload_uses_its_resource_and_an_unreachable_cluster_fails(pool, world, call, monkeypatch):
    run, calls = fake_can_i({"front": (0, "yes", "")})
    monkeypatch.setattr(wapp, "_kubectl_run", run)
    r = call("alice", "POST", "/api/folders/check",
             {"objects": ["container/harv1/web/statefulset/front", "harvester/lab/ns/x"]})
    a, b = r.get_json()["objects"]
    assert calls[0][6] == "statefulsets.apps/front" and a["write"] is True
    assert (b["write"], b["outcome"]) == (False, "fail") and "does not answer" in b["reason"]
    assert len(calls) == 1                     # rien demandé au cluster éteint


def fake_can_i_ns(ns_answer, names):
    """can-i sans nom (tout l'espace de noms) puis par nom."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        target = argv[argv.index("patch") + 1]
        out = ns_answer if "/" not in target else names[target.split("/", 1)[1]]
        return types.SimpleNamespace(returncode=0 if out == "yes" else 1, stdout=out + "\n", stderr="")
    return run, calls


@pytest.mark.parametrize("ns_answer,names,expected_calls,writes", [
    # oui sur tout l'espace de noms : une seule revue pour les trois VMs
    ("yes", {}, 1, [True, True, True]),
    # non sur l'espace de noms : une revue par nom ensuite
    ("no", {"a": "yes", "b": "no", "c": "yes"}, 4, [True, False, True]),
])
def test_check_asks_once_per_namespace_then_per_name_on_no(pool, world, call, monkeypatch,
                                                            ns_answer, names, expected_calls, writes):
    """Revue M-2 : une revue d'accès par (cluster, espace de noms, ressource)
    sans nom ; par nom seulement sur « no » (500 objets ne font plus 500 appels)."""
    run, calls = fake_can_i_ns(ns_answer, names)
    monkeypatch.setattr(wapp, "_kubectl_run", run)
    keys = [f"harvester/harv1/nav/{n}" for n in "abc"]
    d = call("alice", "POST", "/api/folders/check", {"objects": keys}).get_json()["objects"]
    assert [o["write"] for o in d] == writes
    assert len(calls) == expected_calls
    assert calls[0][6:] == ["virtualmachines.kubevirt.io", "-n", "nav"]


def test_check_without_delegation_says_the_console_writes_with_its_own_identity(pool, world, call, monkeypatch):
    """Revue M-3 : compte local sans délégation d'identité, le kubeconfig est
    celui de la console : la raison le dit au lieu de « you can write »."""
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    run, _calls = fake_can_i({"a": (0, "yes\n", "")})
    monkeypatch.setattr(wapp, "_kubectl_run", run)
    d = call("alice", "POST", "/api/folders/check", {"objects": ["harvester/harv1/nav/a"]}).get_json()["objects"][0]
    assert d["outcome"] == "object" and "its own identity" in d["reason"] and "you can write" not in d["reason"]


class FakeHost:
    def __init__(self, decl, state, timeout=None, infos=None):
        self.infos = FakeHost.INFOS

    def batch(self, commands, idempotent=False):
        out = {}
        for k, args in commands.items():
            info = self.infos.get(args[-1])
            out[k] = kh.CmdOutput(info) if info else kh.CmdOutput("error: domain not found", rc=1)
        return out


DOMINFO = "Id:             {id}\nName: x\nUUID: {u}\nState: running\nPersistent:     {p}\n"


@pytest.mark.parametrize("info,write,outcome,reason", [
    (DOMINFO.format(id=3, u=UUID1, p="yes"), True, "object", "libvirt metadata"),
    (DOMINFO.format(id=3, u=UUID1, p="no"), False, "console", "transient"),
    (DOMINFO.format(id="-", u=UUID1, p="no"), False, "fail", "neither persistent"),
    (None, False, "fail", "not found"),
])
def test_check_kvm_reads_dominfo_once_per_host(pool, world, call, monkeypatch, info, write, outcome, reason):
    FakeHost.INFOS = {UUID1: info} if info else {}
    monkeypatch.setattr(wapp._kh, "VirshHost", FakeHost)
    d = call("alice", "POST", "/api/folders/check", {"objects": [f"kvm/{UUID1}"]}).get_json()["objects"][0]
    assert (d["write"], d["outcome"], d["check"]) == (write, outcome, "virsh") and reason in d["reason"]


def test_check_unreachable_kvm_host(pool, world, call, monkeypatch):
    class Down(FakeHost):
        def batch(self, commands, idempotent=False):
            raise kh.KvmError("unreachable", "node2: unreachable: connection refused", host="node2")
    monkeypatch.setattr(wapp._kh, "VirshHost", Down)
    d = call("alice", "POST", "/api/folders/check", {"objects": [f"kvm/{UUID1}"]}).get_json()["objects"][0]
    assert (d["write"], d["outcome"]) == (False, "fail") and "cannot be reached" in d["reason"]


@pytest.mark.parametrize("key,write,outcome", [
    ("host/harvester/harv1", False, "console"),     # config.yaml : console seulement (décision 12)
    ("host/harvester/lab", True, "object"),
    ("host/kvm/node2", True, "object"),
])
def test_check_hosts_by_declaration(pool, world, call, key, write, outcome):
    d = call("alice", "POST", "/api/folders/check", {"objects": [key]}).get_json()["objects"][0]
    assert (d["write"], d["outcome"], d["check"]) == (write, outcome, "decl")


def test_check_writes_nothing_and_keeps_the_shared_reads(pool, world, call, monkeypatch):
    dropped = []
    monkeypatch.setattr(wapp.READ_SHARE, "invalidate", lambda c=None: dropped.append(c))
    call("alice", "POST", "/api/folders/check", {"objects": ["host/kvm/node2"]})
    assert dropped == [] and _NoThread.started == []


# ---------------------------------------------------------------------------
# Garde-fous
# ---------------------------------------------------------------------------
def test_routes_are_authenticated_and_rate_limited_with_valid_limits():
    from limits import parse_many
    assert parse_many(wapp.FOLDERS_RATE)
    for head in re.findall(r'(@app\.route\("/api/folders[^"]*"[^\n]*\n(?:@[^\n]*\n)+)def ', SRC):
        assert "@requires_auth" in head, head
        if "methods=" in head:
            spec = re.search(r"@_rate_limit\(([^)]+)\)", head).group(1)
            spec = wapp.FOLDERS_RATE if spec == "FOLDERS_RATE" else spec.strip('"')
            assert parse_many(spec), head


def test_to_dict_carries_undo_only_for_folder_actions():
    run = wapp.ActionRun("x1", "kvm:start:node2/vm", "kvm:node2", ["harvester-kvm"])
    assert "undo" not in run.to_dict()
    run.undo = {"path": "/api/folders/actions/x1/undo"}
    assert run.to_dict()["undo"] == {"path": "/api/folders/actions/x1/undo"}


# ---------------------------------------------------------------------------
# Lectures de flotte : dossiers des conteneurs et des hôtes, plafond M-8
# ---------------------------------------------------------------------------
def workload(kind, name, folder=None, ns="web"):
    return {"kind": kind, "namespace": ns, "name": name, "state": "Running", "ready": 1, "desired": 1,
            "images": [], "gpus": 0, "created": None, "labels": {}, "pods": [], "folder_annotation": folder}


@pytest.mark.parametrize("rows,expected", [
    ([workload("Deployment", "front", "/Lab")], {"container/harv1/web/deployment/front": ("/Lab", "vm")}),
    ([workload("StatefulSet", "pg")], {"container/harv1/web/statefulset/pg": (None, None)}),
    ([workload("Deployment", "bad", "no-slash")], {"container/harv1/web/deployment/bad": (None, None)}),
    ([workload("Job", "once", "/Lab"), workload("Pod", "lone", "/Lab")], {}),     # non rangeables
])
def test_container_rows_carry_key_and_folder(pool, world, call, monkeypatch, rows, expected):
    monkeypatch.setattr(wapp, "_containers_collect",
                        lambda c, kc: ({"cluster": c, "workloads": [dict(r) for r in rows], "denials": []}, 200, [])
                        if c == "harv1" else ({"error": "x"}, 500, []))
    d = call("curieux", "GET", "/api/containers-all").get_json()
    got = {w["key"]: (w["folder"], w["folder_source"]) for w in d["workloads"] if "key" in w}
    assert got == expected
    assert all("folder_annotation" not in w for w in d["workloads"])


def test_container_console_filing_shows_on_the_row(world, call, monkeypatch):
    conn = fdb.connect(world["db"])
    fdb.upsert_membership(conn, "container/harv1/web/deployment/front", "/Lab", "console")
    conn.close()
    monkeypatch.setattr(wapp, "_containers_collect",
                        lambda c, kc: ({"cluster": c, "workloads": [workload("Deployment", "front")], "denials": []},
                                       200, []))
    w = call("curieux", "GET", "/api/containers/harv1").get_json()["workloads"][0]
    assert (w["key"], w["folder"], w["folder_source"]) == ("container/harv1/web/deployment/front", "/Lab", "console")


def test_hosts_carry_the_folder_of_their_declaration_or_the_console(pool, world, call, monkeypatch):
    kd.write(world["state"], dict(kd.load(world["state"], "node2"), folder="/Lab"))
    conn = fdb.connect(world["db"])
    fdb.upsert_membership(conn, "host/harvester/harv1", "/Prod", "console", scope="hosts")
    conn.close()
    monkeypatch.setattr(wapp, "_harvester_hosts", lambda: [
        {"kind": "harvester", "name": "harv1", "state": "ok", "error": None, "vm_count": None, "vm_count_age": None,
         "labels": {}, "version": None, "favorite": False, "folder": None, "origin": "config"}])
    monkeypatch.setattr(wapp, "_peek_read", lambda *a, **k: (None, None) if k.get("with_age") else None)
    hosts = {h["key"]: h for h in call("curieux", "GET", "/api/hosts").get_json()["hosts"]}
    assert (hosts["host/kvm/node2"]["folder"], hosts["host/kvm/node2"]["folder_source"]) == ("/Lab", "vm")
    assert (hosts["host/harvester/harv1"]["folder"], hosts["host/harvester/harv1"]["folder_source"]) == \
        ("/Prod", "console")


def test_every_folder_named_by_objects_is_created_without_cap(world, monkeypatch, caplog):
    """Décision 90 : aucun plafond ni journal de débordement."""
    rows = [{"key": f"harvester/harv1/ns/v{i}", "folder": f"/Flood/F{i}", "_scope": "harvester/harv1"}
            for i in range(700)]
    with caplog.at_level("WARNING"):
        wapp._folders_apply([dict(r) for r in rows], [])
    assert not [r for r in caplog.records if "folders" in r.message]
    conn = fdb.connect(world["db"])
    try:
        made = [f for f in fdb.list_tree(conn) if f["origin"] == "objects"]
    finally:
        conn.close()
    assert len(made) == 701


def test_empty_into_refuses_a_subfolder_name_already_in_the_target_before_any_action(world, call):
    conn = fdb.connect(world["db"])
    fdb.ensure_path(conn, "/ERP", "objects")                  # même nom que /Prod/ERP, à la racine
    conn.close()
    r = call("alice", "POST", f"/api/folders/{fid(world, '/Prod')}/empty-into", {"target_id": "unfiled"})
    assert r.status_code == 409 and r.get_json()["conflicts"] == ["ERP"]
    assert "already exists in the root" in r.get_json()["error"] and _NoThread.started == []


def test_tree_of_2000_folders_with_2000_objects_reads_fast(world, call, monkeypatch):
    """Décision 90 : une grande arborescence recréée depuis les objets reste
    rapide à lire (temps mesuré, rapport de l'étape 3b)."""
    import time
    conn = fdb.connect(world["db"])
    keys = [f"harvester/harv1/ns/vm{i}" for i in range(2000)]
    fdb.realign(conn, [{"key": k, "folder": f"/Big/G{i % 40}/F{i}", "scope": "harvester/harv1"}
                       for i, k in enumerate(keys)])
    conn.close()
    seen = {"api_vms_all": {"vms": [{"key": k} for k in keys]}}
    monkeypatch.setattr(wapp, "_peek_query", lambda ep, q: seen.get(ep))
    t = time.perf_counter()
    r = call("curieux", "GET", "/api/folders")
    took = time.perf_counter() - t
    tree = {f["path"]: f for f in r.get_json()["tree"]}
    assert len(tree) >= 2041 and tree["/Big"]["counts"]["vms"] == 2000 and tree["/Big/G0"]["counts"]["vms"] == 50
    print(f"GET /api/folders, {len(tree)} folders, 2000 objects: {took:.3f}s")
    assert took < 5


@pytest.mark.parametrize("action,status,expect", [
    ("folders:move:/Prod", "interrupted", "/api/folders/actions/AID/resume"),
    ("folders:assign:/Prod", "error", "/api/folders/actions/AID/resume"),
    ("folders:rename:/X", "done", None),
    ("folders:rename:/X", "running", None),
    ("folders:resume:fa0000000001", "error", "/api/folders/actions/fa0000000001/resume"),   # vise l'origine
    ("vm-start", "error", None),
    (None, "error", None),
])
def test_stopped_folder_actions_carry_their_resume_path(action, status, expect):
    """2.0.0 : le serveur dit qu'une action de dossiers se reprend (dock et
    Activité n'ont aucune règle d'état), en mémoire comme relue de la base."""
    got = wapp._folder_resume(action, status, "AID")
    assert got == ({"resume": {"path": expect}} if expect else {})
    row = wapp._row_finish({"id": "AID", "action": action, "status": status, "dry_run": 0, "result": None,
                            "undo": None})
    assert row.get("resume") == got.get("resume")
    run = wapp.ActionRun("AID", action or "", "folders", ["x"])
    run.status = status
    assert run.to_dict().get("resume") == got.get("resume")
