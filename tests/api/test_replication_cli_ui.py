"""Écrans de réplication (BE1) : ligne de commande.

`plan create` et `plan set` lisent les valeurs composées (`recovery`,
`mappings`) dans le fichier privé `--spec`, jamais dans la ligne de
commande ; chaque sortie et le journal masquent les en-têtes secrets ;
`plan set` garde un secret renvoyé masqué, refuse ce que l'état interdit et
dit quand le carnet est à réécrire ; `hook test` et `candidates`."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_replication_cli_199 import FAILURE_KEYS, PID, cli, db, draft_world, protected, rp, run  # noqa: E402
from test_replication_failover_waves_ui import SECRET, hook, server  # noqa: E402,F401

@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    """Les points d'injection de la ligne de commande reviennent à leur
    valeur d'avant chaque essai."""
    for name in ("KUBE_FACTORY", "NOW", "SLEEP"):
        monkeypatch.setattr(cli, name, getattr(cli, name))
    yield


CREATE = ["plan", "create", "--name=plan-b", "--from=harvlab", "--to=harvlab2", "--vm=default/vm-a",
          "--vm=default/vm-b", "--rpo=15m"]


def spec_file(tmp_path, obj, name="spec.json"):
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return f"--spec={p}"


def everything(w):
    """Tout ce qu'une personne peut lire : journal et exécutions."""
    rows = [dict(r) for r in w.conn.execute("SELECT * FROM events").fetchall()]
    return json.dumps(rows) + json.dumps([db.list_runs(w.conn, p["id"]) for p in db.list_plans(w.conn)])


def recovery(srv_url="https://hooks.example"):
    return {"waves": [{"name": "back", "vms": ["default/vm-b"], "gate": {"kind": "agent", "seconds": 60}}],
            "hooks": [{"name": "dns", "at": "wave:1", "method": "POST", "url": f"{srv_url}/ok",
                       "secret_headers": {"Authorization": SECRET}}],
            "resize": {"default/vm-a": {"cpu": 1, "memory": "1Gi"}}, "auto_protect": False}


def test_plan_create_takes_recovery_from_the_spec_file_and_never_shows_secrets(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a", "vm-b"))
    argv = CREATE + ["--map-disk-class=default/vm-a/data=harvester-longhorn",
                     "--map-drill-net=default/prod=default/lab-net", spec_file(tmp_path, {"recovery": recovery()})]
    assert not any(SECRET in a for a in argv)
    rc, data, err = run(capsys, w, *argv)
    assert rc == 0, data
    stored = db.get_plan_by_name(w.conn, "plan-b")
    assert stored["options"]["hooks"][0]["secret_headers"] == {"Authorization": SECRET}       # rangé
    assert stored["options"]["waves"][0]["gate"] == {"kind": "agent", "seconds": 60}
    assert stored["mappings"]["disk_classes"] == {"default/vm-a/data": "harvester-longhorn"}
    assert stored["mappings"]["drill_networks"] == {"default/prod": "default/lab-net"}
    assert data["plan"]["options"]["hooks"][0]["secret_headers"] == {"Authorization": rp.MASK}
    assert SECRET not in json.dumps(data) + err + everything(w)
    rc, data, err = run(capsys, w, "plan", "show", "plan-b")
    assert rc == 0 and data["plan"]["options"]["hooks"][0]["secret_headers"] == {"Authorization": rp.MASK}
    rc, data, err = run(capsys, w, "plan", "list")
    assert rc == 0 and SECRET not in json.dumps(data) + err


@pytest.mark.parametrize("spec,extra,kind", [
    ({"recovery": "waves"}, [], "usage"),
    ({"recovery": {"waves": [], "retries": 3}}, [], "bad-option"),
    ({"recovery": {"waves": [{"vms": ["default/zzz"]}]}}, [], "bad-option"),
    ({"recovery": {"hooks": [{"name": "x", "at": "start", "url": "https://h/",
                              "secret_headers": {"A": "********"}}]}}, [], "bad-option"),
    ({"recovery": {"network_data": {"default/vm-a": "v: 2"}}}, [], "bad-option"),
    ({"mappings": {"disk_classes": {}}}, [], None),                  # plan create ne lit pas `mappings`
    ({}, ["--map-disk-class=default/vm-a=fast"], "bad-mapping"),
    ({}, ["--recovery={}"], "usage"),                              # jamais une option de la ligne de commande
])
def test_plan_create_refusals(tmp_path, capsys, spec, extra, kind):
    w = draft_world(tmp_path, vms=("vm-a", "vm-b"))
    rc, data, _ = run(capsys, w, *(CREATE + extra + ([spec_file(tmp_path, spec)] if spec else [])))
    if kind is None:
        assert rc == 0, data
        return
    assert rc == 2 and set(data) == FAILURE_KEYS and data["kind"] == kind, data
    assert db.get_plan_by_name(w.conn, "plan-b") is None


def _create_with_hooks(tmp_path, capsys, w):
    rc, data, _ = run(capsys, w, *(CREATE[:3] + ["--name=plan-b"] + CREATE[3:]
                                   + [spec_file(tmp_path, {"recovery": recovery()}, "c.json")]))
    assert rc == 0, data


def test_plan_set_recovery_keeps_masked_secrets_and_replaces_given_keys(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a", "vm-b"))
    _create_with_hooks(tmp_path, capsys, w)
    shown = run(capsys, w, "plan", "show", "plan-b")[1]["plan"]["options"]
    # la personne renvoie ce qu'elle a lu (masqué), change une URL et ajoute un en-tête
    hooks = shown["hooks"]
    hooks[0]["url"] = "https://hooks.example/v2"
    hooks[0]["secret_headers"]["X-Key"] = "k-new"
    rc, data, err = run(capsys, w, "plan", "set", "plan-b",
                        spec_file(tmp_path, {"recovery": {"hooks": hooks, "accept_degraded_standby": True}}))
    assert rc == 0, data
    stored = db.get_plan_by_name(w.conn, "plan-b")["options"]
    assert stored["hooks"][0]["secret_headers"] == {"Authorization": SECRET, "X-Key": "k-new"}
    assert stored["hooks"][0]["url"] == "https://hooks.example/v2"
    assert stored["waves"] and stored["resize"] and stored["accept_degraded_standby"] is True    # clés non données gardées
    assert data["changed"]["hooks"][0]["secret_headers"] == {"Authorization": rp.MASK, "X-Key": rp.MASK}
    assert "protect_needed" not in data
    text = json.dumps(data) + err + everything(w)
    assert SECRET not in text and "k-new" not in text
    # une clé donnée vide la retire
    rc, data, _ = run(capsys, w, "plan", "set", "plan-b", spec_file(tmp_path, {"recovery": {"waves": []}}, "e.json"))
    assert rc == 0 and "waves" not in db.get_plan_by_name(w.conn, "plan-b")["options"]


def _to_state(w, *states):
    cur = w.plan()["state"]
    for s in states:
        db.set_state(w.conn, PID, cur, s)
        cur = s


@pytest.mark.parametrize("states,argv,spec,rc,kind", [
    (("protecting", "protected"), ["--cloud-init=rerun"], None, 0, None),
    (("protecting", "protected"), [], {"mappings": {"networks": {"default/prod": "default/lab-net"}}}, 0, None),
    (("protecting", "protected"), [], {"mappings": {"drill_networks": {"default/prod": "default/lab-net"}}}, 0, None),
    (("protecting", "protected"), [], {"mappings": {"disk_classes": {"default/vm-a/data": "x"}}}, 2,
     "state-forbids"),
    (("protecting", "protected", "failing_over"), ["--start=stopped"], None, 2, "state-forbids"),
    (("protecting", "protected", "failing_over"), [], {"recovery": {"auto_protect": True}}, 2, "state-forbids"),
    (("protecting", "protected", "failing_over"), ["--paused=true"], None, 0, None),
    ((), [], {"mappings": {"disk_classes": {"default/vm-a/data": "harvester-longhorn"}}}, 0, None),
    ((), [], {"mappings": {"volumes": {}}}, 2, "bad-mapping"),
    ((), [], {"recovery": {"bogus": 1}}, 2, "bad-option"),
    ((), ["--mac=both"], None, 2, "usage"),
])
def test_plan_set_state_rules(tmp_path, capsys, states, argv, spec, rc, kind):
    w = draft_world(tmp_path, vms=("vm-a",))
    _to_state(w, *states)
    extra = [spec_file(tmp_path, spec)] if spec else []
    got, data, _ = run(capsys, w, "plan", "set", "plan-a", *argv, *extra)
    assert got == rc and data.get("kind") == kind, data
    if rc == 0 and w.plan()["state"] == "protected" and (argv or "networks" in (spec or {}).get("mappings", {})):
        assert data["protect_needed"] is True
    if rc == 0 and "drill_networks" in json.dumps(spec or {}):
        assert "protect_needed" not in data


def test_hook_test_command(tmp_path, capsys, server):
    w = draft_world(tmp_path, vms=("vm-a",), options={"hooks": [hook(server, "ok", "end"),
                                                               hook(server, "ko", "end", "/fail")]})
    rc, data, err = run(capsys, w, "hook", "test", "plan-a", "--name=ok", "--by=ops", "--id=a2")
    assert rc == 0, data
    assert set(data) == FAILURE_KEYS | {"plan", "hook", "status", "seconds"}
    assert data["status"] == 204 and data["hook"] == {"name": "ok", "at": "end", "method": "POST",
                                                      "url": f"{server.url}/ok?********"}
    assert server.seen[-1]["headers"]["authorization"] == SECRET
    rc, data, err = run(capsys, w, "hook", "test", "plan-a", "--name=ko")
    assert rc == 1 and data["kind"] == "hook-failed" and data["status"] == 500
    assert SECRET not in json.dumps(data) + err + everything(w)
    evs = [e for e in db.plan_events(w.conn, PID) if e["step"] == "hook"]
    assert [e["code"] for e in evs] == ["hook-failed", "hook-ok"] and evs[1]["by"] == "ops"
    rc, data, _ = run(capsys, w, "hook", "test", "plan-a", "--name=nope")
    assert rc == 2 and data["kind"] == "unknown-hook"
    rc, data, _ = run(capsys, w, "hook", "test", "plan-a", "--name=Bad_Name")
    assert rc == 2 and data["kind"] == "bad-option"


def test_candidates_command(tmp_path, capsys):
    w = draft_world(tmp_path, vms=("vm-a",))
    rc, data, _ = run(capsys, w, "candidates", "--cluster=harvlab")
    assert rc == 0, data
    assert data["cluster"] == "harvlab" and [v["name"] for v in data["vms"]] == ["vm-a"]
    assert data["vms"][0]["plans"] == [PID] and data["vms"][0]["eligible"] is True


@pytest.mark.parametrize("argv,rc,kind", [
    (["--cluster=harvlab", "--selector=dr=gold"], 2, "bad-selection"),
    (["--cluster=harvlab", "--namespace=default", "--selector=dr"], 2, "bad-selector"),
    (["--cluster=harvlab", "--namespace=Bad"], 2, "bad-selection"),
    (["--cluster=nowhere"], 2, "unknown-cluster"),
    (["--cluster=bad name"], 2, "bad-cluster"),
    (["--cluster=denied"], 2, "source-denied"),
    (["--cluster=down"], 1, "source-unreachable"),
])
def test_candidates_command_refusals(tmp_path, capsys, argv, rc, kind):
    w = draft_world(tmp_path, vms=("vm-a",))

    class Refusing:
        def __init__(self, msg):
            self.msg = msg

        def list(self, *a, **k):
            raise RuntimeError(self.msg)
    kubes = {"harvlab": w.src, "denied": Refusing("Error from server (Forbidden): virtualmachines is forbidden"),
             "down": Refusing("Unable to connect to the server: dial tcp: i/o timeout")}
    cli.KUBE_FACTORY = lambda name, kc: kubes.get(name)
    got, data, _ = run(capsys, None, "candidates", *argv, f"--db={w.conn.execute('PRAGMA database_list').fetchone()[2]}")
    assert got == rc and data["kind"] == kind, data
