"""v1.97.0 : instantanés, clonage et suppression d'une VM KVM dans la console.

Lectures (liste des instantanés, aperçus) par la bibliothèque, sans action
ni mutation ; chaque mutation lance bin/harvester-kvm.py en action suivie,
valeurs en `--option=valeur` (un nom hostile reste UN argument), libellé
`kvm:vm-<verbe>:<hôte>/<domaine>` pour que deux opérations sur la même VM
se refusent (409). Supprimer une VM = administrateur, nom saisi et
empreinte de l'aperçu exigés ; supprimer un instantané = opérateur."""

import re
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kvm_routes_196 import (  # noqa: E402,F401 : fixtures partagées
    HOSTILE, SRC, _NoThread, as_user, client, env, only_run, script_cmd, wapp)

ko, kh = wapp._ko, wapp._kh
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
UNREAD = "/var/lib/machines/moon/netperf.qcow2"


def base_args(env, *sub):
    return list(sub) + ["--json", f"--state-dir={env['state']}"]


@pytest.fixture
def lib(monkeypatch):
    """La bibliothèque simulée : chaque appel est noté, rien ne touche un hôte."""
    calls = []

    def list_snapshots(host, name):
        calls.append(("list", host.name, name))
        if name == "nosuch":
            raise kh.KvmError("notfound", f"{host.name}: notfound: error: failed to get domain '{name}'")
        if name == "gone":
            raise kh.KvmError("unreachable", f"{host.name}: unreachable: ssh: no route")
        return {"snapshots": [{"name": "s1", "created": "2026-10-24T10:00:00+02:00", "state": "running",
                               "parent": None, "location": "internal", "current": True}],
                "current": "s1", "support": [], "running": True, "state": "running",
                "persistent": True, "memory_kib": 262144}

    def clone_plan(host, name, new_name):
        calls.append(("clone-plan", host.name, name, new_name))
        return {"source": name, "new_name": new_name, "refusals": [] if name != "busy-disk" else ["x"],
                "disks": [], "shared": [], "space": [], "nics": [], "nvram": None,
                "uefi_entries_lost": False, "tpm_state_lost": False, "regenerated": []}

    def delete_plan(host, name, remove_storage=False, acknowledged_unreadable=None, survey_timeout=None):
        calls.append(("delete-plan", host.name, name, remove_storage, acknowledged_unreadable, survey_timeout))
        return {"name": name, "uuid": "u-1", "state": "shut off", "persistent": True, "refusals": [],
                "internal_snapshots": 0, "external_snapshots": 0, "managed_save": False,
                "managed_save_lost": False, "nvram": None, "tpm_state": None,
                "remove_storage": remove_storage, "delete": [], "keep": [], "unreadable": [UNREAD],
                "acknowledged_unreadable": sorted(acknowledged_unreadable or []), "notes": [],
                "digest": DIGEST}
    monkeypatch.setattr(ko, "list_snapshots", list_snapshots)
    monkeypatch.setattr(ko, "clone_plan", clone_plan)
    monkeypatch.setattr(ko, "delete_plan", delete_plan)
    return calls


def delete_body(name="web", **over):
    b = {"confirm": name, "expect_delete": DIGEST, "remove_storage": True, "acknowledge_unreadable": []}
    b.update(over)
    return b


# --- lectures ---------------------------------------------------------------------------

def test_snapshot_list_is_the_library_listing_with_the_role(client, env, lib):
    r = client.get("/api/kvm/vms/node2/web/snapshots")
    assert r.status_code == 200
    d = r.get_json()
    assert d["current"] == "s1" and d["snapshots"][0]["location"] == "internal"
    assert d["host"] == "node2" and d["name"] == "web" and d["role"] == "admin"
    assert lib == [("list", "node2", "web")]
    assert wapp.ACTIONS == {} and _NoThread.started == []


@pytest.mark.parametrize("name,code,kind", [("nosuch", 404, "notfound"), ("gone", 502, "unreachable")])
def test_snapshot_list_failures_are_classified(client, lib, name, code, kind):
    r = client.get(f"/api/kvm/vms/node2/{name}/snapshots")
    assert r.status_code == code and r.get_json()["kind"] == kind


@pytest.mark.parametrize("name", HOSTILE)
def test_reads_accept_any_libvirt_name_as_one_value(client, lib, name):
    q = quote(name, safe="")
    assert client.get(f"/api/kvm/vms/node2/{q}/snapshots").status_code == 200
    assert client.get(f"/api/kvm/vms/node2/{q}/delete-plan").status_code == 200
    assert lib[0] == ("list", "node2", name) and lib[1][2] == name


def test_clone_plan_is_read_only(client, lib):
    r = client.get("/api/kvm/vms/node2/db/clone-plan?name=db-copy")
    assert r.status_code == 200
    d = r.get_json()
    assert d["possible"] is True and d["plan"]["new_name"] == "db-copy" and d["role"] == "admin"
    assert lib == [("clone-plan", "node2", "db", "db-copy")]
    assert wapp.ACTIONS == {} and _NoThread.started == []


@pytest.mark.parametrize("new", ["", "a/b", ".hidden", "x\ny"])
def test_clone_plan_refuses_a_bad_name_before_the_host(client, lib, new):
    r = client.get(f"/api/kvm/vms/node2/db/clone-plan?name={quote(new, safe='')}")
    assert r.status_code == 400 and lib == []


@pytest.mark.parametrize("query,remove,ack", [
    ("", False, None),
    ("?remove_storage=1", True, None),
    (f"?remove_storage=1&acknowledge={quote(UNREAD, safe='')}", True, [UNREAD]),
    (f"?remove_storage=1&acknowledge={quote(UNREAD, safe='')}&acknowledge={quote(UNREAD, safe='')}", True, [UNREAD]),
])
def test_delete_plan_passes_storage_and_acknowledgements(client, lib, query, remove, ack):
    r = client.get(f"/api/kvm/vms/node2/db/delete-plan{query}")
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["plan"]["digest"] == DIGEST and d["possible"] is True and d["role"] == "admin"
    assert lib == [("delete-plan", "node2", "db", remove, ack, wapp.KVM_DELETE_SURVEY_TIMEOUT)]
    assert wapp.ACTIONS == {} and _NoThread.started == []


@pytest.mark.parametrize("query", [
    f"?acknowledge={quote(UNREAD, safe='')}",                 # sans suppression des disques
    "?remove_storage=1&acknowledge=relative/path",
    "?remove_storage=1&acknowledge=%2Fa%01b",
])
def test_delete_plan_refuses_bad_acknowledgements_before_the_host(client, lib, query):
    r = client.get(f"/api/kvm/vms/node2/db/delete-plan{query}")
    assert r.status_code == 400 and lib == []


def test_reads_never_show_the_state_path(client, env, lib):
    for url in ("/api/kvm/vms/node2/web/snapshots", "/api/kvm/vms/node2/web/clone-plan?name=c",
                "/api/kvm/vms/node2/web/delete-plan?remove_storage=1"):
        text = client.get(url).get_data(as_text=True)
        assert str(env["state"]) not in text and "_id" not in text and "_known" not in text, url


def test_vm_list_serves_the_operations_table(client):
    d = client.get("/api/kvm/vms").get_json()
    assert d["operations"] == {
        "snapshots": {"role": "operator", "states": None, "persistent": False},
        "clone": {"role": "operator", "states": ["shut off"], "persistent": True},
        "delete": {"role": "admin", "states": ["shut off"], "persistent": True},
    }


# --- instantanés -------------------------------------------------------------------------

@pytest.mark.parametrize("snap,desc,extra", [
    ("s1", None, []),
    ("web-20261024-1000", "", []),
    ("-not-valid", None, None),
    ("a b", None, None),
    ("x" * 65, None, None),
    ("ok.1", "-x --json", ["--description=-x --json"]),
    ("ok.2", "multi\nline <b>", ["--description=multi\nline <b>"]),
    ("ok.3", "d" * 1001, None),
    ("ok.4", 5, None),
])
def test_snapshot_create_argv(client, env, snap, desc, extra):
    body = {"name": snap}
    if desc is not None:
        body["description"] = desc
    r = client.post("/api/kvm/vms/node2/web/snapshots", json=body)
    if extra is None:
        assert r.status_code == 400 and _NoThread.started == [] and wapp.ACTIONS == {}
        return
    assert r.status_code == 202, r.get_json()
    run = only_run()
    assert run.action == "kvm:vm-snapshot-create:node2/web" and run.cluster == "kvm:node2"
    assert script_cmd()[2:] == base_args(env, "snapshot", "create") + [
        "--host=node2", "--name=web", f"--snapshot={snap}"] + extra
    assert run.required_role == "operator"


@pytest.mark.parametrize("verb,method,suffix", [("revert", "post", "/revert"), ("delete", "delete", "")])
@pytest.mark.parametrize("snap", ["s1"] + HOSTILE)
def test_snapshot_gestures_pass_any_existing_name_as_one_value(client, env, verb, method, suffix, snap):
    r = getattr(client, method)(f"/api/kvm/vms/node2/web/snapshots/{quote(snap, safe='')}{suffix}")
    assert r.status_code == 202, r.get_json()
    run = only_run()
    assert run.action == f"kvm:vm-snapshot-{verb}:node2/web"
    assert script_cmd()[2:] == base_args(env, "snapshot", verb) + [
        "--host=node2", "--name=web", f"--snapshot={snap}"]
    assert run.required_role == "operator"


@pytest.mark.parametrize("name", HOSTILE)
def test_snapshot_create_keeps_a_hostile_vm_name_as_one_value(client, env, name):
    r = client.post(f"/api/kvm/vms/node2/{quote(name, safe='')}/snapshots", json={"name": "s1"})
    assert r.status_code == 202
    assert script_cmd()[-2:] == [f"--name={name}", "--snapshot=s1"]


@pytest.mark.parametrize("snap", ["a\x01b", "del\x7f", "x" * 256])
def test_snapshot_gesture_refuses_a_control_character(client, snap):
    r = client.post(f"/api/kvm/vms/node2/web/snapshots/{quote(snap, safe='')}/revert")
    assert r.status_code == 400 and _NoThread.started == []


# --- clonage ------------------------------------------------------------------------------

@pytest.mark.parametrize("new", ["db-copy", "-x", "$(id) x;y", "a b", "<img src=x onerror=alert(1)>"])
def test_clone_argv(client, env, new):
    r = client.post("/api/kvm/vms/node2/db/clone", json={"name": new})
    assert r.status_code == 202, r.get_json()
    run = only_run()
    assert run.action == "kvm:vm-clone:node2/db" and run.required_role == "operator"
    assert script_cmd()[2:] == base_args(env, "clone") + ["--host=node2", "--name=db", f"--new-name={new}"]


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "a/b"}, {"name": ".x"}, {"name": 3},
                                  {"name": "db"}, {"name": "x\ty"}, ["db-copy"]])
def test_clone_refusals_start_nothing(client, body):
    r = client.post("/api/kvm/vms/node2/db/clone", json=body)
    assert r.status_code == 400 and _NoThread.started == [] and wapp.ACTIONS == {}


def test_clone_is_refused_while_its_new_name_is_busy(client):
    """Le nom du clone est tenu lui aussi : une opération en cours sur une VM
    de ce nom refuse le clonage (409), avant même le verrou de la CLI."""
    run = wapp.ActionRun("kvmbusy0002", "kvm:vm-start:node2/db-copy", "kvm:node2", ["harvester-kvm"])
    run.status = "running"
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[run.id] = run
    r = client.post("/api/kvm/vms/node2/db/clone", json={"name": "db-copy"})
    assert r.status_code == 409 and r.get_json()["running_action"] == "kvm:vm-start:node2/db-copy"
    assert _NoThread.started == []


# --- suppression ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,tail", [
    (delete_body(remove_storage=False), ["--expect-delete=" + DIGEST]),
    (delete_body(), ["--remove-storage", "--expect-delete=" + DIGEST]),
    (delete_body(acknowledge_unreadable=[UNREAD, "/srv/x y.qcow2"]),
     ["--remove-storage", f"--acknowledge-unreadable={UNREAD}", "--acknowledge-unreadable=/srv/x y.qcow2",
      "--expect-delete=" + DIGEST]),
    (delete_body(acknowledge_unreadable=[UNREAD, UNREAD]),
     ["--remove-storage", f"--acknowledge-unreadable={UNREAD}", "--expect-delete=" + DIGEST]),
])
def test_vm_delete_argv(client, env, body, tail):
    r = client.delete("/api/kvm/vms/node2/web", json=body)
    assert r.status_code == 202, r.get_json()
    run = only_run()
    assert run.action == "kvm:vm-delete:node2/web" and run.required_role == "admin"
    assert script_cmd()[2:] == base_args(env, "vm", "delete") + ["--host=node2", "--name=web"] + tail
    # le chemin d'un fichier accepté ne sort pas dans la commande montrée
    assert not any(UNREAD in a for a in run.cmd) and not any(str(env["state"]) in a for a in run.cmd)


@pytest.mark.parametrize("name", HOSTILE)
def test_vm_delete_keeps_a_hostile_name_as_one_value(client, env, name):
    r = client.delete(f"/api/kvm/vms/node2/{quote(name, safe='')}", json=delete_body(name))
    assert r.status_code == 202
    assert f"--name={name}" in script_cmd() and only_run().action == f"kvm:vm-delete:node2/{name}"


@pytest.mark.parametrize("body,msg", [
    (delete_body(confirm="Web"), "type the name"),
    (delete_body(confirm=None), "type the name"),
    ({"expect_delete": DIGEST}, "type the name"),
    (delete_body(expect_delete=None), "expect_delete is required"),
    (delete_body(expect_delete="A" * 64), "invalid delete digest"),
    (delete_body(expect_delete="a" * 63), "invalid delete digest"),
    (delete_body(remove_storage="yes"), "remove_storage"),
    (delete_body(remove_storage=False, acknowledge_unreadable=[UNREAD]), "only applies"),
    (delete_body(acknowledge_unreadable=["relative"]), "absolute paths"),
    (delete_body(acknowledge_unreadable=UNREAD), "absolute paths"),
    (delete_body(acknowledge_unreadable=["/" + str(i) for i in range(65)]), "absolute paths"),
])
def test_vm_delete_refusals_start_nothing(client, body, msg):
    r = client.delete("/api/kvm/vms/node2/web", json=body)
    assert r.status_code == 400 and msg in r.get_json()["error"]
    assert _NoThread.started == [] and wapp.ACTIONS == {}


def test_vm_delete_of_an_undeclared_host_is_404(client):
    r = client.delete("/api/kvm/vms/nohost/web", json=delete_body())
    assert r.status_code == 404 and _NoThread.started == []


# --- occupation par VM ---------------------------------------------------------------------

MUTATIONS = [
    ("post", "/api/kvm/vms/node2/web/snapshots", {"name": "s9"}, "kvm:vm-snapshot-create:node2/web"),
    ("post", "/api/kvm/vms/node2/web/snapshots/s1/revert", {}, "kvm:vm-snapshot-revert:node2/web"),
    ("delete", "/api/kvm/vms/node2/web/snapshots/s1", {}, "kvm:vm-snapshot-delete:node2/web"),
    ("post", "/api/kvm/vms/node2/web/clone", {"name": "web2"}, "kvm:vm-clone:node2/web"),
    ("delete", "/api/kvm/vms/node2/web", delete_body(), "kvm:vm-delete:node2/web"),
    ("post", "/api/kvm/vms/node2/web/start", {}, "kvm:vm-start:node2/web"),
]


@pytest.mark.parametrize("first", MUTATIONS, ids=[m[3] for m in MUTATIONS])
@pytest.mark.parametrize("second", MUTATIONS, ids=[m[3] for m in MUTATIONS])
def test_one_operation_at_a_time_per_vm(client, first, second):
    m, url, body, label = first
    assert getattr(client, m)(url, json=body).status_code == 202
    only_run().status = "running"
    m2, url2, body2, _ = second
    r = getattr(client, m2)(url2, json=body2)
    assert r.status_code == 409 and r.get_json()["running_action"] == label
    # une autre VM du même hôte passe
    other = getattr(client, m2)(url2.replace("/web", "/db", 1),
                                json=dict(body2, confirm="db") if "confirm" in body2 else body2)
    assert other.status_code == 202, other.get_json()


# --- routage, rôles et limites ---------------------------------------------------------------

@pytest.mark.parametrize("method,path,endpoint", [
    ("GET", "/api/kvm/vms/h/d/snapshots", "api_kvm_snapshots"),
    ("POST", "/api/kvm/vms/h/d/snapshots", "api_kvm_snapshot_create"),
    ("POST", "/api/kvm/vms/h/d/clone", "api_kvm_clone"),
    ("GET", "/api/kvm/vms/h/d/clone-plan", "api_kvm_clone_plan"),
    ("GET", "/api/kvm/vms/h/d/delete-plan", "api_kvm_delete_plan"),
    ("POST", "/api/kvm/vms/h/d/console-ticket", "api_kvm_console_ticket"),
    ("POST", "/api/kvm/vms/h/d/snapshots/s/revert", "api_kvm_snapshot_revert"),
    ("DELETE", "/api/kvm/vms/h/d/snapshots/s", "api_kvm_snapshot_delete"),
    ("DELETE", "/api/kvm/vms/h/d", "api_kvm_vm_delete"),
    ("GET", "/api/kvm/vms/h/d", "api_kvm_vm"),
    ("POST", "/api/kvm/vms/h/d/start", "api_kvm_vm_gesture"),
])
def test_static_segments_win_over_the_gesture_route(method, path, endpoint):
    adapter = wapp.app.url_map.bind("localhost")
    assert adapter.match(path, method=method)[0] == endpoint


def test_clone_and_snapshots_are_never_taken_for_a_gesture(client):
    assert "clone" not in wapp.KVM_GESTURES and "snapshots" not in wapp.KVM_GESTURES
    r = client.post("/api/kvm/vms/node2/web/snapshots", json={"name": "s1"})
    assert r.status_code == 202 and only_run().action.startswith("kvm:vm-snapshot-create:")


@pytest.mark.parametrize("method,path,role", [
    ("GET", "/api/kvm/vms/node2/web/snapshots", "viewer"),
    ("GET", "/api/kvm/vms/node2/web/clone-plan", "viewer"),
    ("GET", "/api/kvm/vms/node2/web/delete-plan", "viewer"),
    ("POST", "/api/kvm/vms/node2/web/snapshots", "operator"),
    ("POST", "/api/kvm/vms/node2/web/snapshots/s1/revert", "operator"),
    ("DELETE", "/api/kvm/vms/node2/web/snapshots/s1", "operator"),
    ("DELETE", "/api/kvm/vms/node2/web/snapshots/destroy", "operator"),
    ("POST", "/api/kvm/vms/node2/web/clone", "operator"),
    ("DELETE", "/api/kvm/vms/node2/web", "admin"),
    ("DELETE", "/api/kvm/vms/node2/destroy", "admin"),
    ("POST", "/api/kvm/vms/node2/web/destroy", "operator"),
    ("DELETE", "/api/kvm/hosts/node2", "admin"),
])
def test_required_role_per_route(method, path, role):
    assert wapp.required_role_for(path, method) == role


@pytest.mark.parametrize("user,method,path,body,allowed", [
    ("curieux", "GET", "/api/kvm/vms/node2/web/snapshots", None, True),
    ("curieux", "GET", "/api/kvm/vms/node2/web/clone-plan?name=c", None, True),
    ("curieux", "GET", "/api/kvm/vms/node2/web/delete-plan", None, True),
    ("curieux", "POST", "/api/kvm/vms/node2/web/snapshots", {"name": "s1"}, False),
    ("curieux", "POST", "/api/kvm/vms/node2/web/clone", {"name": "c"}, False),
    ("alice", "POST", "/api/kvm/vms/node2/web/snapshots", {"name": "s1"}, True),
    ("alice", "POST", "/api/kvm/vms/node2/web/snapshots/s1/revert", {}, True),
    ("alice", "DELETE", "/api/kvm/vms/node2/web/snapshots/s1", {}, True),
    ("alice", "POST", "/api/kvm/vms/node2/web/clone", {"name": "c"}, True),
    ("alice", "DELETE", "/api/kvm/vms/node2/web", delete_body(), False),
    ("patronne", "DELETE", "/api/kvm/vms/node2/web", delete_body(), True),
])
def test_roles_are_enforced(as_user, lib, user, method, path, body, allowed):
    kw = {"json": body} if body is not None else {}
    r = as_user(user, method, path, **kw)
    if allowed:
        assert r.status_code in (200, 202), r.get_json()
    else:
        assert r.status_code == 403 and r.get_json()["required"] in ("operator", "admin")
        assert _NoThread.started == []


@pytest.mark.parametrize("route,limit", [
    ('"/api/kvm/vms/<host>/<domain>/snapshots", methods=["POST"]', "10/minute"),
    ('"/api/kvm/vms/<host>/<domain>/snapshots/<snapshot>/revert", methods=["POST"]', "10/minute"),
    ('"/api/kvm/vms/<host>/<domain>/snapshots/<snapshot>", methods=["DELETE"]', "10/minute"),
    ('"/api/kvm/vms/<host>/<domain>/clone", methods=["POST"]', "5/minute"),
    ('"/api/kvm/vms/<host>/<domain>", methods=["DELETE"]', "10/minute"),
])
def test_every_mutation_is_authenticated_and_rate_limited(route, limit):
    m = re.search(r'@app\.route\(' + re.escape(route) + r'\)\n@requires_auth\n@_rate_limit\("([^"]+)"\)', SRC)
    assert m and m.group(1) == limit, route
    wapp._validate_rate_spec(m.group(1))


@pytest.mark.parametrize("route", ['"/api/kvm/vms/<host>/<domain>/snapshots"',
                                   '"/api/kvm/vms/<host>/<domain>/clone-plan"',
                                   '"/api/kvm/vms/<host>/<domain>/delete-plan"'])
def test_every_read_is_authenticated_and_shared(route):
    assert re.search(r'@app\.route\(' + re.escape(route) + r'\)\n@requires_auth\n'
                     r'@shared_read\(scope=KVM_SCOPE, fresh_min=KVM_FRESH_MIN\)', SRC), route


def test_cancel_of_a_vm_delete_needs_the_admin_role(as_user, env):
    r = as_user("patronne", "DELETE", "/api/kvm/vms/node2/web", json=delete_body())
    aid = r.get_json()["action_id"]
    assert wapp.ACTIONS[aid].required_role == "admin"
    r2 = as_user("alice", "DELETE", f"/api/action/{aid}")
    assert r2.status_code == 403


def test_a_failed_read_is_logged_with_the_library_message(client, lib, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        r = client.get("/api/kvm/vms/node2/gone/snapshots")
    assert r.status_code == 502
    assert any("snapshots failed" in m and "no route" in m for m in caplog.messages)
