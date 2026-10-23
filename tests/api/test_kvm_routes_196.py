"""v1.96.0 : les routes KVM de la console.

Lectures par la bibliothèque (bin/lib/kvm_host.py, kvm_decl.py), en
parallèle, un hôte injoignable rendant son erreur classée sans faire échouer
les autres. Toute écriture lance bin/harvester-kvm.py en action suivie, les
valeurs en `--option=valeur` (un nom de VM hostile reste UN argument), avec
le répertoire d'état de la console. Ni chemin de clé ni secret dans une
réponse ; la clé envoyée passe par un fichier privé effacé à la fin."""

import base64
import io
import json
import os
import re
import stat
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")
import app as wapp  # noqa: E402

SRC = (ROOT / "web" / "app.py").read_text()
kd, kh = wapp._kd, wapp._kh
KEY = b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAfake\n-----END OPENSSH PRIVATE KEY-----\n"
FP = "SHA256:81Nop5uq4NBaqU/hzSyopOIxSoyytc6DgyLZd5/xXJY"
HOSTILE = ["a;rm -rf ~", "$(id)", "-x", "x y", "<img src=x onerror=alert(1)>", "Ünïcode VM", "q'uote\"d", "--json"]


_RealThread = threading.Thread


class _NoThread:
    """Retient le fil de travail d'une action (`work`) sans le lancer ; les
    autres fils (lecture parallèle des hôtes) partent pour de vrai."""
    started = []

    def __new__(cls, target=None, args=(), **k):
        if getattr(target, "__name__", "") != "work":
            return _RealThread(target=target, args=args, **k)
        return super().__new__(cls)

    def __init__(self, target=None, args=(), **k):
        self.target, self.args = target, args

    def start(self):
        _NoThread.started.append(self)


def decl(name, address="10.0.0.1"):
    return {"name": name, "address": address, "port": 22, "user": "ju", "ssh_key": kd.key_rel(name),
            "known_hosts": kd.known_hosts_rel(name), "labels": {"site": "cave"}}


def inventory(host, details=False):
    inv = {"name": host.name, "hostname": f"{host.name}-real", "address": host.decl["address"],
           "labels": dict(host.decl.get("labels") or {}),
           "cpu": {"cpus": 28, "cpu_model": "x86_64", "mhz": 2400},
           "memory": {"total_kib": 131072000, "free_kib": 1000, "available_kib": 2000},
           "libvirt_version": "12.7.0", "qemu_version": "10.2.2", "errors": [],
           "vms": [{"host": host.name, "name": n, "id": None, "state": s, "persistent": True, "autostart": False,
                    "vcpus": 2, "memory_kib": 2097152, "memory_max_kib": 2097152, "cpu_time_ns": None}
                   for n, s in (("web", "running"), ("db", "shut off"))]}
    if details:
        inv["pools"] = [{"name": "default", "state": "running", "autostart": True, "persistent": True,
                         "capacity": 10, "allocation": 5, "available": 5, "volumes": []}]
        inv["networks"] = [{"name": "default", "state": "active", "active": True, "autostart": True,
                            "persistent": True}]
    return inv


NET_XML = ("<network><name>default</name><forward mode='nat'/><bridge name='virbr0'/>"
           "<ip address='192.168.122.1' netmask='255.255.255.0'><dhcp>"
           "<range start='192.168.122.2' end='192.168.122.254'/></dhcp></ip></network>")


@pytest.fixture
def env(monkeypatch):
    # le chemin d'un socket de contrôle ssh doit rester court : pas tmp_path
    state = Path(tempfile.mkdtemp(prefix="kvr-", dir="/tmp"))
    work = state / "work"
    work.mkdir()
    kd.write(state, decl("node2", "172.16.1.12"))
    kd.write(state, decl("ghost", "172.16.1.250"))
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    monkeypatch.setattr(wapp, "_capi_work_dir", lambda: work)
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    monkeypatch.setattr(wapp.threading, "Thread", _NoThread)
    calls = {"inventory": [], "details": [], "batch": []}

    def fake_inventory(host, details=False):
        calls["inventory"].append((host.name, details, host.timeout))
        if host.name == "ghost":
            raise kh.KvmError("unreachable", "ghost: unreachable: ssh: connect to host 172.16.1.250 port 22: No route to host")
        return inventory(host, details)
    monkeypatch.setattr(kh, "host_inventory", fake_inventory)

    def fake_batch(self, commands, timeout=None, cmd_timeout=None):
        calls["batch"].append(dict(commands))
        return {k: kh.CmdOutput(NET_XML) for k in commands}
    monkeypatch.setattr(kh.VirshHost, "batch", fake_batch)

    def fake_detail(host, name):
        calls["details"].append((host.name, name))
        if name == "nosuch":
            raise kh.KvmError("notfound", f"{host.name}: notfound: error: failed to get domain '{name}'")
        return {"name": name, "host": host.name, "state": "running", "vcpus": 2, "memory_kib": 2097152,
                "disks": [], "nics": [], "graphics": [], "cpu_pinned": True, "persistent": True, "autostart": False}
    monkeypatch.setattr(kh, "vm_detail", fake_detail)
    _NoThread.started = []
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    yield {"state": state, "work": work, "calls": calls}
    wapp.ACTIONS.clear()
    wapp.ACTIONS.update(saved)
    import shutil
    shutil.rmtree(state, ignore_errors=True)


@pytest.fixture
def client(env, monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")       # mode ouvert : admin
    return wapp.app.test_client()


@pytest.fixture
def as_user(env, monkeypatch, tmp_path):
    (tmp_path / "htpasswd").write_text("")
    roles = tmp_path / "roles.yaml"
    roles.write_text("default_role: viewer\nusers:\n  patronne: admin\n  alice: operator\n  curieux: viewer\n")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    monkeypatch.setattr(wapp, "ROLES_PATH", roles)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: True)
    monkeypatch.setattr(kd, "scan_host_key", lambda a, p, timeout=15: [])

    def call(user, method, path, **kw):
        token = base64.b64encode(f"{user}:x".encode()).decode()
        with wapp.app.test_client() as c:
            return c.open(path, method=method, headers={"Authorization": f"Basic {token}"}, **kw)
    return call


def script_cmd():
    t = _NoThread.started[-1]
    cell = dict(zip(t.target.__code__.co_freevars, t.target.__closure__ or ()))
    return cell["cmd"].cell_contents


def only_run():
    [run] = wapp.ACTIONS.values()
    return run


def add_form(payload=None, key=KEY, **over):
    p = {"name": "node3", "address": "172.16.1.13", "port": 22, "user": "ju", "fingerprint": FP,
         "labels": {"site": "cave", "rack": "-b2"}}
    p.update(over)
    data = {"payload": json.dumps(p) if payload is None else payload}
    if key is not None:
        data["key"] = (io.BytesIO(key), "id_ed25519")
    return data


# --- lectures ------------------------------------------------------------------------

def test_hosts_list_isolates_an_unreachable_host(client, env):
    r = client.get("/api/kvm/hosts")
    assert r.status_code == 200
    d = r.get_json()
    by = {h["name"]: h for h in d["hosts"]}
    assert set(by) == {"node2", "ghost"}
    assert by["ghost"]["error"] == {"kind": "unreachable", "message": by["ghost"]["error"]["message"]}
    assert "No route to host" in by["ghost"]["error"]["message"]
    assert by["node2"]["libvirt_version"] == "12.7.0" and len(by["node2"]["vms"]) == 2
    # la déclaration publique est jointe, sans chemin
    assert by["node2"]["user"] == "ju" and by["node2"]["port"] == 22 and by["ghost"]["address"] == "172.16.1.250"
    assert d["role"] == "admin" and d["gestures"]["resume"] == ["blocked", "crashed", "idle", "in shutdown", "running", "shut off"]
    # lecture parallèle, sans détails, avec le budget de la console
    assert sorted(env["calls"]["inventory"]) == [("ghost", False, wapp.KVM_READ_TIMEOUT),
                                                 ("node2", False, wapp.KVM_READ_TIMEOUT)]


def test_vm_list_flattens_and_reports_each_host(client):
    d = client.get("/api/kvm/vms").get_json()
    assert [(v["name"], v["host"]) for v in d["vms"]] == [("db", "node2"), ("web", "node2")]
    hosts = {h["name"]: h for h in d["hosts"]}
    assert hosts["node2"] == {"name": "node2", "state": "ok", "count": 2}
    assert hosts["ghost"]["state"] == "unreachable" and "No route" in hosts["ghost"]["error"]


def test_host_detail_adds_network_definitions(client, env):
    r = client.get("/api/kvm/hosts/node2")
    assert r.status_code == 200
    d = r.get_json()
    assert d["pools"][0]["name"] == "default"
    [net] = d["networks"]
    assert net["forward"] == "nat" and net["bridge"] == "virbr0"
    assert net["ips"][0]["prefix"] == 24 and net["ips"][0]["dhcp_ranges"][0]["start"] == "192.168.122.2"
    assert env["calls"]["batch"] == [{"default": ("net-dumpxml", "--network", "default")}]


def test_host_detail_of_an_unreachable_host_is_its_error(client):
    r = client.get("/api/kvm/hosts/ghost")
    assert r.status_code == 502
    assert r.get_json()["error"]["kind"] == "unreachable"


@pytest.mark.parametrize("url,code", [
    ("/api/kvm/hosts/nope", 404),
    ("/api/kvm/vms/nope/web", 404),
    ("/api/kvm/vms/node2/nosuch", 404),
])
def test_reads_not_found(client, url, code):
    assert client.get(url).status_code == code


@pytest.mark.parametrize("name", HOSTILE + ["My VM"])
def test_vm_detail_accepts_any_libvirt_name(client, env, name):
    r = client.get(f"/api/kvm/vms/node2/{quote(name, safe='')}")
    assert r.status_code == 200, r.get_json()
    assert env["calls"]["details"][-1] == ("node2", name)


def test_no_kvm_route_uses_a_kubernetes_checked_parameter():
    """Le crochet before_request contrôle `name`, `vm`, `namespace`, `n`
    comme des noms Kubernetes : un nom libvirt y serait refusé."""
    for rule in wapp.app.url_map.iter_rules():
        if str(rule).startswith("/api/kvm/"):
            assert not set(rule.arguments) & set(wapp._VALIDATED_PATH_PARAMS), str(rule)


def test_reads_never_show_a_path(client, env):
    for url in ("/api/kvm/hosts", "/api/kvm/vms", "/api/kvm/hosts/node2", "/api/kvm/vms/node2/web"):
        text = client.get(url).get_data(as_text=True)
        assert str(env["state"]) not in text and "_id" not in text and "_known" not in text, url


# --- noms refusés -----------------------------------------------------------------------

BAD_HOSTS = ["bad name", "-x", "a" * 62, "x;y", "é"]


@pytest.mark.parametrize("host", BAD_HOSTS)
@pytest.mark.parametrize("method,tpl", [
    ("get", "/api/kvm/hosts/{h}"), ("get", "/api/kvm/vms/{h}/web"), ("delete", "/api/kvm/hosts/{h}"),
    ("post", "/api/kvm/hosts/{h}/test"), ("post", "/api/kvm/vms/{h}/web/start"),
])
def test_host_names_are_checked_by_name_re(client, host, method, tpl):
    r = getattr(client, method)(tpl.format(h=quote(host, safe="")))
    assert r.status_code == 400 and r.get_json()["error"] == "invalid host name"
    assert wapp.ACTIONS == {} and _NoThread.started == []


@pytest.mark.parametrize("name", ["a\x01b", "tab\there", "line\nfeed", "del\x7f", "a" * 256])
@pytest.mark.parametrize("method,tpl", [("get", "/api/kvm/vms/node2/{d}"), ("post", "/api/kvm/vms/node2/{d}/start")])
def test_vm_names_refused_only_when_empty_long_or_control(client, name, method, tpl):
    r = getattr(client, method)(tpl.format(d=quote(name, safe="")))
    assert r.status_code == 400 and r.get_json()["error"] == "invalid domain name"
    assert _NoThread.started == []


# --- relevé des empreintes ---------------------------------------------------------------

def test_scan_returns_fingerprints_without_key_lines(client, monkeypatch):
    seen = []

    def scan(address, port, timeout=15):
        seen.append((address, port, timeout))
        return [{"type": "ssh-ed25519", "fingerprint": FP, "line": "172.16.1.12 ssh-ed25519 AAAA"}]
    monkeypatch.setattr(kd, "scan_host_key", scan)
    r = client.post("/api/kvm/hosts/scan", json={"address": "172.16.1.12", "port": 2222})
    assert r.status_code == 200
    assert r.get_json() == {"address": "172.16.1.12", "port": 2222, "keys": [{"type": "ssh-ed25519", "fingerprint": FP}]}
    assert seen == [("172.16.1.12", 2222, wapp.KVM_SCAN_TIMEOUT)]


@pytest.mark.parametrize("body,code,msg", [
    ({"address": "-oProxyCommand=x"}, 400, "invalid address"),
    ({"address": ""}, 400, "invalid address"),
    ({"address": "h", "port": 0}, 400, "port must be"),
    ({"address": "h", "port": "22"}, 400, "port must be"),
    ({"address": "h", "port": True}, 400, "port must be"),
    ([], 400, "JSON object"),
])
def test_scan_refusals_reach_no_host(client, monkeypatch, body, code, msg):
    monkeypatch.setattr(kd, "scan_host_key", lambda *a, **k: pytest.fail("no scan expected"))
    r = client.post("/api/kvm/hosts/scan", json=body)
    assert r.status_code == code and msg in r.get_json()["error"]


@pytest.mark.parametrize("exc,code,kind,hint", [
    (kh.KvmError("failed", "172.16.1.12: no host key received"), 502, "failed", "PerSourcePenalties"),
    (kh.KvmError("timeout", "172.16.1.12: ssh-keyscan timed out"), 502, "timeout", None),
    (kd.DeclError("invalid port"), 400, None, None),
])
def test_scan_failures_are_classified(client, monkeypatch, exc, code, kind, hint):
    def scan(*a, **k):
        raise exc
    monkeypatch.setattr(kd, "scan_host_key", scan)
    r = client.post("/api/kvm/hosts/scan", json={"address": "172.16.1.12"})
    d = r.get_json()
    assert r.status_code == code and d.get("kind") == kind
    if hint:
        assert hint in d["error"]


# --- déclarer un hôte ---------------------------------------------------------------------

def test_host_add_spawns_the_cli_with_a_private_key_file(client, env):
    r = client.post("/api/kvm/hosts", data=add_form(), content_type="multipart/form-data")
    assert r.status_code == 202, r.get_json()
    run = only_run()
    assert r.get_json() == {"action_id": run.id, "host": "node3"}
    assert run.action == "kvm:host-add:node3" and run.cluster == "kvm:node3"
    cmd = script_cmd()
    assert cmd[1].endswith("bin/harvester-kvm.py") and cmd[2:6] == ["host", "add", "--json", f"--state-dir={env['state']}"]
    key = Path(next(a for a in cmd if a.startswith("--ssh-key=")).split("=", 1)[1])
    # La copie va dans ssh/ du répertoire d'état, forcé en 0700 (jamais /tmp)
    assert key.parent == env["state"] / "ssh" and key.read_bytes() == KEY
    assert (key.parent.stat().st_mode & 0o777) == 0o700
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert cmd[6:] == ["--name=node3", "--address=172.16.1.13", "--port=22", "--user=ju", f"--ssh-key={key}",
                       f"--fingerprint={FP}", "--label=rack=-b2", "--label=site=cave"]
    # montré dans le dock : ni clé ni répertoire d'état
    shown = " ".join(run.cmd)
    assert str(env["state"]) not in shown and str(key) not in shown and "--ssh-key" not in shown
    assert run.cmd[:3] == ["harvester-kvm", "host", "add"]
    assert str(env["state"]) not in r.get_data(as_text=True)


def test_host_add_key_file_is_deleted_after_the_action(client, env, monkeypatch):
    seen = {}

    def runner(run, cmd, capture=False):
        key = Path(next(a for a in cmd if a.startswith("--ssh-key=")).split("=", 1)[1])
        seen["during"] = key.exists()
        seen["key"], seen["capture"] = key, capture
    monkeypatch.setattr(wapp, "_vm_transfer_runner", runner)
    assert client.post("/api/kvm/hosts", data=add_form(), content_type="multipart/form-data").status_code == 202
    _NoThread.started[-1].target()            # ce que ferait le fil de travail
    assert seen["during"] is True and seen["capture"] is True
    assert not seen["key"].exists() and list(env["work"].iterdir()) == []


@pytest.mark.parametrize("data,code,msg", [
    (add_form(payload="{not json"), 400, "JSON payload"),
    (add_form(payload=""), 400, "JSON payload"),
    (add_form(name="bad name"), 400, "invalid host name"),
    (add_form(name="node2"), 409, "already declared"),
    (add_form(address="-oProxyCommand=x"), 400, "invalid address"),
    (add_form(port=70000), 400, "port must be"),
    (add_form(user="-root"), 400, "user"),
    (add_form(fingerprint="SHA256:x y"), 400, "fingerprint"),
    (add_form(fingerprint=""), 400, "fingerprint"),
    (add_form(labels={"bad key": "v"}), 400, "labels"),
    (add_form(labels={"k": "line\nfeed"}), 400, "invalid label"),
    (add_form(labels=["site=cave"]), 400, "labels"),
    (add_form(key=None), 400, "private key file is missing"),
    (add_form(key=b""), 400, "private key file is missing"),
    (add_form(key=b"x" * (64 * 1024 + 1)), 400, "too large"),
    (add_form(key=b"ssh-ed25519 AAAA public"), 400, "not a private key"),
])
def test_host_add_refusals_start_nothing_and_leave_no_key(client, env, data, code, msg):
    r = client.post("/api/kvm/hosts", data=data, content_type="multipart/form-data")
    assert r.status_code == code and msg in r.get_json()["error"], r.get_json()
    assert wapp.ACTIONS == {} and _NoThread.started == []
    assert list(env["work"].iterdir()) == []


# --- retirer, tester ----------------------------------------------------------------------

@pytest.mark.parametrize("method,url,sub,label", [
    ("delete", "/api/kvm/hosts/node2", ["host", "remove"], "kvm:host-remove:node2"),
    ("post", "/api/kvm/hosts/node2/test", ["host", "test"], "kvm:host-test:node2"),
])
def test_host_remove_and_test_are_tracked_actions(client, env, method, url, sub, label):
    r = getattr(client, method)(url)
    assert r.status_code == 202
    run = only_run()
    assert run.action == label and run.cluster == "kvm:node2"
    assert script_cmd()[2:] == sub + ["--json", f"--state-dir={env['state']}", "node2"]


@pytest.mark.parametrize("method,url", [("delete", "/api/kvm/hosts/node9"), ("post", "/api/kvm/hosts/node9/test"),
                                        ("post", "/api/kvm/vms/node9/web/start")])
def test_undeclared_host_is_404(client, method, url):
    assert getattr(client, method)(url).status_code == 404
    assert _NoThread.started == []


# --- gestes ----------------------------------------------------------------------------------

@pytest.mark.parametrize("gesture", ["start", "shutdown", "reboot", "destroy", "suspend", "resume"])
@pytest.mark.parametrize("name", ["web"] + HOSTILE)
def test_gestures_pass_the_vm_name_as_one_value(client, env, gesture, name):
    r = client.post(f"/api/kvm/vms/node2/{quote(name, safe='')}/{gesture}", json={})
    assert r.status_code == 202, r.get_json()
    run = only_run()
    assert run.action == f"kvm:vm-{gesture}:node2/{name}" and run.cluster == "kvm:node2"
    assert script_cmd()[2:] == ["vm", gesture, "--json", f"--state-dir={env['state']}", "--host=node2", f"--name={name}"]


@pytest.mark.parametrize("body,arg,code", [
    ({"timeout": 300}, "--timeout=300", 202),
    ({"timeout": 1}, "--timeout=1", 202),
    ({"timeout": 0}, None, 400),
    ({"timeout": 3601}, None, 400),
    ({"timeout": "60"}, None, 400),
    ({"timeout": True}, None, 400),
    ({"timeout": 1.5}, None, 400),
])
def test_gesture_timeout(client, body, arg, code):
    r = client.post("/api/kvm/vms/node2/web/shutdown", json=body)
    assert r.status_code == code
    if arg:
        assert script_cmd()[-1] == arg
    else:
        assert _NoThread.started == []


def test_a_slash_never_reaches_the_cli(client):
    """libvirt refuse `/` dans un nom ; encodé dans l'URL, il ne désigne
    aucune route, et rien ne part."""
    r = client.post(f"/api/kvm/vms/node2/{quote('a/b', safe='')}/start", json={})
    assert r.status_code != 202 and _NoThread.started == []


def test_unknown_gesture_is_refused(client):
    r = client.post("/api/kvm/vms/node2/web/undefine", json={})
    assert r.status_code == 404 and "unknown gesture" in r.get_json()["error"]


def test_second_gesture_on_the_same_vm_waits(client):
    assert client.post("/api/kvm/vms/node2/web/shutdown", json={}).status_code == 202
    only_run().status = "running"
    r = client.post("/api/kvm/vms/node2/web/destroy", json={})
    assert r.status_code == 409 and r.get_json()["running_action"] == "kvm:vm-shutdown:node2/web"
    # une autre VM, ou la même une fois l'action finie, passe
    assert client.post("/api/kvm/vms/node2/db/start", json={}).status_code == 202
    for run in wapp.ACTIONS.values():
        run.status = "done"
    assert client.post("/api/kvm/vms/node2/web/destroy", json={}).status_code == 202


# --- le relais des actions : résultat JSON du script --------------------------------------

def test_runner_keeps_the_json_output_and_the_steps():
    run = wapp.ActionRun("kvmtest00001", "kvm:vm-reboot:node2/web", "kvm:node2", ["harvester-kvm"])
    script = ("import sys,json;sys.stderr.write('STEP_EVENT|wait|done|web: reboot requested | running\\n');"
              "print(json.dumps({'ok': True, 'confirmed': False, 'state': 'running'}))")
    wapp._vm_transfer_runner(run, [sys.executable, "-c", script], capture=True)
    assert run.status == "done" and run.result["output"] == {"ok": True, "confirmed": False, "state": "running"}
    steps = [e for e in run.events if e.get("type") == "step"]
    assert steps[0]["step_id"] == "wait" and steps[0]["message"] == "web: reboot requested | running"


def test_runner_keeps_the_failure_object_and_without_capture_nothing():
    run = wapp.ActionRun("kvmtest00002", "kvm:vm-shutdown:node2/web", "kvm:node2", ["harvester-kvm"])
    script = ("import sys,json;sys.stderr.write('STEP_EVENT|wait|error|still running\\n');"
              "print(json.dumps({'ok': False, 'kind': 'timeout', 'suggest': ['destroy']}));sys.exit(1)")
    wapp._vm_transfer_runner(run, [sys.executable, "-c", script], capture=True)
    assert run.status == "error" and run.error_summary == "still running"
    assert run.result["output"]["suggest"] == ["destroy"]
    run2 = wapp.ActionRun("kvmtest00003", "x", "kvm:node2", ["harvester-kvm"])
    wapp._vm_transfer_runner(run2, [sys.executable, "-c", "print('{\"ok\": true}')"])
    assert "output" not in run2.result


# --- rôles et limites ------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,role", [
    ("GET", "/api/kvm/hosts", "viewer"),
    ("GET", "/api/kvm/hosts/node2", "viewer"),
    ("GET", "/api/kvm/vms", "viewer"),
    ("GET", "/api/kvm/vms/node2/web", "viewer"),
    ("POST", "/api/kvm/hosts", "admin"),
    ("POST", "/api/kvm/hosts/scan", "admin"),
    ("DELETE", "/api/kvm/hosts/node2", "admin"),
    ("POST", "/api/kvm/hosts/node2/test", "operator"),
    ("POST", "/api/kvm/vms/node2/web/start", "operator"),
    ("POST", "/api/kvm/vms/node2/web/destroy", "operator"),   # pas le /destroy admin de Terraform
    ("POST", "/api/kvm/vms/node2/web/shutdown", "operator"),
])
def test_required_role_per_route(method, path, role):
    assert wapp.required_role_for(path, method) == role


@pytest.mark.parametrize("user,method,path,allowed", [
    ("curieux", "GET", "/api/kvm/hosts", True),
    ("curieux", "GET", "/api/kvm/vms", True),
    ("curieux", "POST", "/api/kvm/hosts/node2/test", False),
    ("curieux", "POST", "/api/kvm/vms/node2/web/start", False),
    ("alice", "POST", "/api/kvm/hosts/node2/test", True),
    ("alice", "POST", "/api/kvm/vms/node2/web/destroy", True),
    ("alice", "POST", "/api/kvm/hosts/scan", False),
    ("alice", "POST", "/api/kvm/hosts", False),
    ("alice", "DELETE", "/api/kvm/hosts/node2", False),
    ("patronne", "POST", "/api/kvm/hosts/scan", True),
    ("patronne", "DELETE", "/api/kvm/hosts/node2", True),
])
def test_roles_are_enforced(as_user, user, method, path, allowed):
    kw = {"json": {"address": "h"}} if path.endswith("/scan") else ({"json": {}} if method == "POST" else {})
    r = as_user(user, method, path, **kw)
    if allowed:
        assert r.status_code != 403, r.get_json()
    else:
        assert r.status_code == 403 and r.get_json()["required"] in ("operator", "admin")
        assert _NoThread.started == []


@pytest.mark.parametrize("route", [
    '"/api/kvm/hosts/scan", methods=["POST"]',
    '"/api/kvm/hosts", methods=["POST"]',
    '"/api/kvm/hosts/<host>", methods=["DELETE"]',
    '"/api/kvm/hosts/<host>/test", methods=["POST"]',
    '"/api/kvm/vms/<host>/<domain>/<gesture>", methods=["POST"]',
])
def test_every_kvm_mutation_is_authenticated_and_rate_limited(route):
    m = re.search(r'@app\.route\(' + re.escape(route) + r'\)\n@requires_auth\n@_rate_limit\("([^"]+)"\)', SRC)
    assert m, route
    wapp._validate_rate_spec(m.group(1))


@pytest.mark.parametrize("route", ['"/api/kvm/hosts"', '"/api/kvm/hosts/<host>"', '"/api/kvm/vms"',
                                   '"/api/kvm/vms/<host>/<domain>"'])
def test_every_kvm_read_is_authenticated_and_shared(route):
    assert re.search(r'@app\.route\(' + re.escape(route) + r'\)\n@requires_auth\n@shared_read\(scope=KVM_SCOPE, fresh_min=KVM_FRESH_MIN\)', SRC), route


def test_the_gestures_come_from_the_cli_table():
    assert wapp.KVM_GESTURES == tuple(wapp._kvm_cli.GESTURES)
    assert set(wapp.KVM_GESTURES) == {"start", "shutdown", "reboot", "destroy", "suspend", "resume"}


# --- corrections après revue (1.96.0) --------------------------------------------------------

def _e2e_gestures():
    """La copie de la table des refus dans le test Playwright."""
    import ast
    src = (ROOT / "tests" / "e2e" / "test_kvm_196.py").read_text()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == "GESTURES")
    return ast.literal_eval(node.value)


def test_the_refusal_table_has_one_source_and_the_e2e_copy_matches():
    served = {a: sorted(r[1]) for a, r in wapp._kvm_cli.GESTURES.items()}
    assert _e2e_gestures() == served
    # reprendre : seulement depuis paused ou pmsuspended (états libvirt connus)
    for st in ("running", "idle", "shut off", "crashed", "in shutdown", "blocked"):
        assert st in served["resume"], st
    assert "paused" not in served["resume"] and "pmsuspended" not in served["resume"]
    # l'interface n'a plus de règle à elle
    js = (ROOT / "web" / "static" / "js" / "kvm.js").read_text()
    body = js[js.index("function gestureWhy"):js.index("// -- requêtes")]
    assert "resume" not in body


@pytest.fixture
def shared(monkeypatch):
    """Lectures partagées actives, horloge des relectures forcées injectée."""
    monkeypatch.setattr(wapp, "READ_SHARE_ENABLED", True)
    wapp.READ_SHARE.invalidate()
    wapp._FRESH_LAST.clear()
    now = [1000.0]
    monkeypatch.setattr(wapp, "_FRESH_CLOCK", lambda: now[0])
    yield now
    wapp.READ_SHARE.invalidate()
    wapp._FRESH_LAST.clear()


@pytest.mark.parametrize("url", ["/api/kvm/hosts", "/api/kvm/vms", "/api/kvm/hosts/node2"])
def test_fresh_reads_are_throttled_per_key(client, env, shared, url):
    reads = lambda: len(env["calls"]["inventory"])  # noqa: E731
    assert client.get(url + "?fresh=1").status_code in (200, 502)
    first = reads()
    assert first >= 1
    # un second « Actualiser » dans les 5 s : la lecture qui vient d'être faite sert
    client.get(url + "?fresh=1")
    client.get(url)
    assert reads() == first
    shared[0] += 5.1
    client.get(url + "?fresh=1")
    assert reads() == 2 * first


def test_fresh_reads_of_other_routes_are_not_throttled(monkeypatch, shared):
    calls = []
    app2 = wapp.Flask("t")

    @app2.route("/x")
    @wapp.shared_read(scope="_t")
    def x():
        calls.append(1)
        return "ok"
    monkeypatch.setattr(wapp, "current_role", lambda: "admin")
    c = app2.test_client()
    c.get("/x?fresh=1")
    c.get("/x?fresh=1")
    assert len(calls) == 2


def test_hosts_declared_only_asks_no_host(client, env):
    r = client.get("/api/kvm/hosts?declared=1")
    d = r.get_json()
    assert r.status_code == 200 and env["calls"]["inventory"] == []
    assert sorted(h["name"] for h in d["hosts"]) == ["ghost", "node2"]
    assert all("error" not in h and "vms" not in h and "ssh_key" not in h for h in d["hosts"])
    assert str(env["state"]) not in r.get_data(as_text=True)


def test_a_slow_host_is_cut_at_the_deadline_and_the_others_are_returned(client, env, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(wapp, "KVM_READ_DEADLINE", 0.3)
    real = kh.host_inventory

    def slow(host, details=False):
        if host.name == "node2":
            release.wait(5)
        return real(host, details)
    monkeypatch.setattr(kh, "host_inventory", slow)
    import time as _t
    t0 = _t.monotonic()
    try:
        r = client.get("/api/kvm/hosts")
        took = _t.monotonic() - t0
    finally:
        release.set()
    by = {h["name"]: h for h in r.get_json()["hosts"]}
    assert r.status_code == 200 and took < 2
    assert by["node2"]["error"] == {"kind": "timeout", "message": "node2: timeout: no answer within 0.3s"}
    assert by["node2"]["address"] == "172.16.1.12" and by["node2"]["user"] == "ju"
    assert by["ghost"]["error"]["kind"] == "unreachable"
    r = client.get("/api/kvm/vms")
    hosts = {h["name"]: h for h in r.get_json()["hosts"]}
    assert hosts["node2"]["state"] in ("timeout", "ok")


def test_the_cli_read_all_without_deadline_waits_for_everyone():
    host = type("H", (), {"name": "a", "decl": {"address": "x"}})()
    import time as _t

    def inv(h, details=False):
        _t.sleep(0.2)
        return {"name": h.name}
    saved = kh.host_inventory
    kh.host_inventory = inv
    try:
        assert wapp._kvm_cli.read_all([host]) == [{"name": "a"}]
        assert wapp._kvm_cli.read_all([host], deadline=0.05)[0]["error"]["kind"] == "timeout"
    finally:
        kh.host_inventory = saved


def _share_keys():
    return {k[0] for k in wapp.READ_SHARE._done}


def test_scan_invalidates_nothing_and_kvm_writes_only_kvm(client, env, shared, monkeypatch):
    monkeypatch.setattr(kd, "scan_host_key", lambda a, p, timeout=15: [])
    for scope in ("harv1", wapp.KVM_SCOPE):
        wapp.READ_SHARE.get((scope, "x"), 60, lambda: (200, b"", "application/json", [], None, []))
    assert client.post("/api/kvm/hosts/scan", json={"address": "172.16.1.12"}).status_code == 200
    assert _share_keys() == {"harv1", wapp.KVM_SCOPE}
    assert client.post("/api/kvm/vms/node2/web/start", json={}).status_code == 202
    assert _share_keys() == {"harv1"}


def test_reserved_host_name_is_refused(client, env):
    r = client.post("/api/kvm/hosts", data=add_form(name="scan"), content_type="multipart/form-data")
    assert r.status_code == 400 and "reserved" in r.get_json()["error"]
    assert wapp.ACTIONS == {} and list(env["work"].iterdir()) == []


def test_host_detail_unexpected_error_is_classified(client, env, monkeypatch):
    def boom(host, details=False):
        raise ValueError("parser exploded at /secret/path")
    monkeypatch.setattr(kh, "host_inventory", boom)
    r = client.get("/api/kvm/hosts/node2")
    d = r.get_json()
    assert r.status_code == 502 and d["error"] == {
        "kind": "failed", "message": "node2: failed: unexpected error (ValueError)"}
    assert "/secret" not in r.get_data(as_text=True)


def test_host_add_body_too_large_is_refused_before_parsing(client, env, monkeypatch):
    def never():
        raise AssertionError("form parsed")
    monkeypatch.setattr(wapp, "_kvm_add_request", lambda state: never())
    data = add_form(key=b"x" * (256 * 1024))
    r = client.post("/api/kvm/hosts", data=data, content_type="multipart/form-data")
    assert r.status_code == 413 and "256 KiB" in r.get_json()["error"]
    assert wapp.ACTIONS == {} and list(env["work"].iterdir()) == []
    # la limite ne vaut que pour cette route
    assert wapp.app.config.get("MAX_CONTENT_LENGTH") is None


def test_host_add_without_a_private_work_dir_is_refused(client, env, monkeypatch):
    def no_dir(state):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(wapp._kd, "ensure_ssh_dir", no_dir)
    before = set(Path(tempfile.gettempdir()).glob("kvm-key-*"))
    r = client.post("/api/kvm/hosts", data=add_form(), content_type="multipart/form-data")
    assert r.status_code == 503 and wapp.ACTIONS == {}
    assert set(Path(tempfile.gettempdir()).glob("kvm-key-*")) == before


def test_gesture_on_an_unreadable_declaration_is_refused(client, env):
    (env["state"] / "kvm.d" / "node2.yaml").write_text("{not json")
    r = client.post("/api/kvm/vms/node2/web/start", json={})
    assert r.status_code == 500 and wapp.ACTIONS == {}


def test_two_simultaneous_gestures_on_one_vm_start_one_action(client, env, monkeypatch):
    import time as _t
    real = wapp._cli_action

    def slow(*a, **k):
        _t.sleep(0.2)                         # fenêtre entre le contrôle et la création
        return real(*a, **k)
    monkeypatch.setattr(wapp, "_cli_action", slow)
    codes, barrier = [], threading.Barrier(2)

    def go(g):
        barrier.wait()
        with wapp.app.test_client() as c:
            codes.append(c.post(f"/api/kvm/vms/node2/web/{g}", json={}).status_code)
    ts = [_RealThread(target=go, args=(g,)) for g in ("shutdown", "destroy")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert sorted(codes) == [202, 409] and len(wapp.ACTIONS) == 1


def test_cancel_needs_the_role_that_started_the_action(as_user, env):
    r = as_user("patronne", "POST", "/api/kvm/hosts", data=add_form(), content_type="multipart/form-data")
    assert r.status_code == 202, r.get_json()
    aid = r.get_json()["action_id"]
    assert wapp.ACTIONS[aid].required_role == "admin"
    r = as_user("alice", "DELETE", f"/api/action/{aid}")
    assert r.status_code == 403 and r.get_json()["required"] == "admin"
    assert not getattr(wapp.ACTIONS[aid], "_cancel", False)
    assert as_user("patronne", "DELETE", f"/api/action/{aid}").status_code == 200
    assert wapp.ACTIONS[aid]._cancel is True
    # un geste lancé par un opérateur, il peut l'annuler
    r = as_user("alice", "POST", "/api/kvm/vms/node2/web/start", json={})
    gid = r.get_json()["action_id"]
    assert wapp.ACTIONS[gid].required_role == "operator"
    assert as_user("alice", "DELETE", f"/api/action/{gid}").status_code == 200


@pytest.mark.parametrize("summary,want", [
    (None, "cancelled, transfer undone"),       # les autres outils : inchangé
    ("cancelled", "host add cancelled (SIGTERM): nothing saved"),
])
def test_runner_cancel_message_is_the_tool_s_own(summary, want):
    run = wapp.ActionRun("kvmtest00009", "kvm:host-add:n", "kvm:n", ["harvester-kvm"])
    run.cancel_summary = summary
    script = ("import sys,json;sys.stderr.write('STEP_EVENT|cancelled|error|host add cancelled (SIGTERM): nothing saved\\n');"
              "print(json.dumps({'ok': False, 'kind': 'cancelled'}));sys.exit(3)")
    wapp._vm_transfer_runner(run, [sys.executable, "-c", script], capture=True)
    assert run.status == "cancelled" and run.error_summary == want


def test_cancel_of_an_action_that_names_no_role_stays_admin(as_user, env):
    run = wapp.ActionRun("legacy000001", "shutdown", "harv1", ["harvester-shutdown"])
    wapp.ACTIONS[run.id] = run
    assert wapp.required_role_for(f"/api/action/{run.id}", "DELETE") == "operator"
    assert wapp.required_role_for("/api/action", "POST") == "admin"
    r = as_user("alice", "DELETE", f"/api/action/{run.id}")
    assert r.status_code == 403 and r.get_json()["required"] == "admin"
    assert as_user("curieux", "DELETE", f"/api/action/{run.id}").status_code == 403
    assert as_user("patronne", "DELETE", f"/api/action/{run.id}").status_code == 200
