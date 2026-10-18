"""v1.93.0 : les routes des gestes des Containers.

La console ne refait rien : chaque geste valide la demande (400 tout de
suite), puis lance bin/harvester-containers.py en action suivie avec le
kubeconfig préparé dans le thread de la requête (identité de la personne),
et rend l'identifiant de l'action. La ligne de commande montrée ne porte ni
chemin ni kubeconfig ; la demande part par un fichier privé."""

import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")
import app as wapp  # noqa: E402


class _NoThread:
    started = []

    def __init__(self, target=None, args=(), **k):
        self.target, self.args = target, args

    def start(self):
        _NoThread.started.append(self)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")            # mode ouvert
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [{"name": "harv", "kubeconfig": "/k/harv.yaml"}]})
    # le kubeconfig de la personne, préparé dans le thread de la requête
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: {"harv": "/staged/alice-harv.yaml"}.get(c))
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: {"user": "alice"})
    monkeypatch.setattr(wapp, "_capi_work_dir", lambda: tmp_path)
    monkeypatch.setattr(wapp.threading, "Thread", _NoThread)
    _NoThread.started = []
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    yield wapp.app.test_client()
    wapp.ACTIONS.clear()
    wapp.ACTIONS.update(saved)


def only_run():
    [run] = wapp.ACTIONS.values()
    return run


def script_cmd():
    """La vraie ligne de commande, celle que le fil de travail lancerait."""
    t = _NoThread.started[-1]
    cell = dict(zip(t.target.__code__.co_freevars, t.target.__closure__ or ()))
    return cell["cmd"].cell_contents


SPEC = {"name": "web", "namespace": "apps", "kind": "Deployment", "image": "nginx:1", "replicas": 2,
        "env": ["A=1"], "gpus": 1, "memory": "512Mi"}


def test_create_starts_a_tracked_action_with_the_persons_kubeconfig(client):
    r = client.post("/api/containers/harv/create", json={"spec": SPEC, "create_namespace": True})
    assert r.status_code == 202
    d = r.get_json()
    run = only_run()
    assert d == {"action_id": run.id, "namespace": "apps", "name": "web", "kind": "Deployment"}
    assert run.action == "containers:create:apps/web" and run.cluster == "harv" and run.cluster_user == "alice"
    cmd = script_cmd()
    assert cmd[1].endswith("bin/harvester-containers.py") and cmd[2] == "create"
    assert cmd[cmd.index("--kubeconfig") + 1] == "/staged/alice-harv.yaml"
    assert "--create-namespace" in cmd
    spec = json.loads(Path(cmd[cmd.index("--spec") + 1]).read_text())
    assert spec["name"] == "web" and spec["memory"] == "512Mi" and spec["env"] == [{"name": "A", "value": "1"}]
    # ce qui est montré dans le dock et l'Activité : ni chemin, ni kubeconfig
    shown = " ".join(run.cmd)
    assert "/staged" not in shown and "--kubeconfig" not in shown and "/tmp" not in shown
    assert run.cmd[:2] == ["harvester-containers", "create"]


def test_create_from_yaml(client):
    text = ("apiVersion: v1\nkind: Pod\nmetadata:\n  name: p1\nspec:\n  containers:\n"
            "  - name: c\n    image: busybox\n")
    r = client.post("/api/containers/harv/create", json={"yaml": text, "namespace": "ml"})
    assert r.status_code == 202, r.get_json()
    cmd = script_cmd()
    spec = json.loads(Path(cmd[cmd.index("--spec") + 1]).read_text())
    m = spec["manifest"]
    assert m["metadata"] == {"name": "p1", "namespace": "ml"}
    assert m["spec"]["containers"][0]["resources"]["limits"] == {"memory": "256Mi"}
    assert "--create-namespace" not in cmd


@pytest.mark.parametrize("body,msg", [
    ({"spec": dict(SPEC, namespace="kube-system")}, "system namespace"),
    ({"spec": dict(SPEC, namespace="harvester-system")}, "system namespace"),
    ({"spec": dict(SPEC, name="Web")}, "invalid name"),
    ({"spec": dict(SPEC, memory="lots")}, "memory limit"),
    ({"spec": dict(SPEC, kind="DaemonSet")}, "kind must be"),
    ({"yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: p\n  namespace: cattle-system\nspec:\n  containers: [{name: c, image: i}]\n"},
     "system namespace"),
    ({"yaml": "a: [1"}, "unreadable YAML"),
    ({"yaml": "kind: Pod\n---\nkind: Pod\n"}, "exactly one object"),
    ({"yaml": "apiVersion: v1\nkind: Secret\nmetadata: {name: s}\n"}, "kind must be"),
    ({"yaml": 3}, "YAML text"),
    ({}, "spec (form) or a yaml"),
])
def test_create_refusals_start_nothing(client, body, msg):
    r = client.post("/api/containers/harv/create", json=body)
    assert r.status_code == 400 and msg in r.get_json()["error"]
    assert wapp.ACTIONS == {} and _NoThread.started == []


def test_create_needs_a_json_object_and_a_known_cluster(client):
    assert client.post("/api/containers/harv/create", data="x", content_type="text/plain").status_code == 400
    r = client.post("/api/containers/nope/create", json={"spec": SPEC})
    assert r.status_code == 404 and wapp.ACTIONS == {}


@pytest.mark.parametrize("method,url,body,verb,extra", [
    ("post", "/api/containers/harv/apps/deployment/web/scale", {"replicas": 3}, "scale", ["--replicas", "3"]),
    ("post", "/api/containers/harv/apps/statefulset/db/scale", {"replicas": 0}, "scale", ["--replicas", "0"]),
    ("post", "/api/containers/harv/apps/deployment/web/restart", None, "restart", []),
    ("post", "/api/containers/harv/apps/daemonset/agent/restart", None, "restart", []),
    ("delete", "/api/containers/harv/apps/job/once", None, "delete", []),
    ("delete", "/api/containers/harv/apps/pod/web-7d9f-abcde", None, "delete", []),
])
def test_gestures_start_tracked_actions(client, method, url, body, verb, extra):
    r = getattr(client, method)(url, json=body if body is not None else {})
    assert r.status_code == 202, r.get_json()
    run = only_run()
    ns, kind, name = url.split("/")[4:7]
    assert r.get_json()["action_id"] == run.id
    assert run.action == f"containers:{verb}:{ns}/{name}"
    cmd = script_cmd()
    assert cmd[2] == verb and cmd[cmd.index("--kubeconfig") + 1] == "/staged/alice-harv.yaml"
    i = cmd.index("--namespace")
    k = {"deployment": "Deployment", "statefulset": "StatefulSet", "daemonset": "DaemonSet",
         "job": "Job", "pod": "Pod"}[kind]
    assert cmd[i:i + 6] == ["--namespace", ns, "--kind", k, "--name", name]
    assert cmd[i + 6:] == extra
    assert "/staged" not in " ".join(run.cmd)


@pytest.mark.parametrize("method,url,body,code,msg", [
    ("post", "/api/containers/harv/kube-system/deployment/coredns/scale", {"replicas": 0}, 400, "system namespace"),
    ("post", "/api/containers/harv/cattle-system/deployment/rancher/restart", None, 400, "system namespace"),
    ("delete", "/api/containers/harv/longhorn-system/daemonset/longhorn-manager", None, 400, "system namespace"),
    ("post", "/api/containers/harv/apps/job/once/scale", {"replicas": 2}, 400, "cannot take this action"),
    ("post", "/api/containers/harv/apps/job/once/restart", None, 400, "cannot take this action"),
    ("delete", "/api/containers/harv/apps/virtualmachine/vm1", None, 400, "unknown workload kind"),
    ("post", "/api/containers/harv/apps/deployment/web/scale", {"replicas": -1}, 400, "replicas"),
    ("post", "/api/containers/harv/apps/deployment/web/scale", {"replicas": "3"}, 400, "replicas"),
    ("post", "/api/containers/harv/apps/deployment/web/scale", {"replicas": True}, 400, "replicas"),
    ("post", "/api/containers/harv/apps/deployment/web/scale", {}, 400, "replicas"),
    ("post", "/api/containers/harv/Apps/deployment/web/restart", None, 400, "invalid namespace"),
    ("post", "/api/containers/nope/apps/deployment/web/restart", None, 404, "unknown cluster"),
])
def test_gesture_refusals_start_nothing(client, method, url, body, code, msg):
    r = getattr(client, method)(url, json=body if body is not None else {})
    assert r.status_code == code and msg in r.get_json()["error"]
    assert wapp.ACTIONS == {} and _NoThread.started == []


def test_a_gesture_waits_for_a_shutdown_of_the_cluster(client):
    busy = wapp.ActionRun("busy1", "shutdown", "harv", ["x"])
    busy.status = "running"
    wapp.ACTIONS["busy1"] = busy
    r = client.delete("/api/containers/harv/apps/deployment/web")
    assert r.status_code == 409 and r.get_json()["running"] == "busy1"
    assert _NoThread.started == []


SRC = (ROOT / "web" / "app.py").read_text()


@pytest.mark.parametrize("route", [
    '"/api/containers/<cluster>/create", methods=["POST"]',
    '"/api/containers/<cluster>/<namespace>/<kind>/<oname>/scale", methods=["POST"]',
    '"/api/containers/<cluster>/<namespace>/<kind>/<oname>/restart", methods=["POST"]',
    '"/api/containers/<cluster>/<namespace>/<kind>/<oname>", methods=["DELETE"]',
])
def test_every_gesture_route_is_authenticated_and_rate_limited(route):
    m = re.search(r'@app\.route\(' + re.escape(route) + r'\)\n@requires_auth\n@_rate_limit\("([^"]+)"\)', SRC)
    assert m, route
    wapp._validate_rate_spec(m.group(1))


def test_gestures_are_for_operators():
    for method in ("POST", "DELETE"):
        assert wapp.required_role_for("/api/containers/harv/apps/deployment/web", method) == "operator"


def test_without_a_session_the_gestures_ask_to_sign_in(monkeypatch, tmp_path):
    ht = tmp_path / "htpasswd"
    ht.write_text("admin:$2y$05$abcdefghijklmnopqrstuuJ1yC3n0x2bXv0y4m5u6t7s8r9q0p1o2\n")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", ht)
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [{"name": "harv", "kubeconfig": "/k/harv.yaml"}]})
    c = wapp.app.test_client()
    assert c.post("/api/containers/harv/create", json={"spec": SPEC}).status_code == 401
    assert c.delete("/api/containers/harv/apps/deployment/web").status_code == 401
