"""v1.76.0 : client vSphere de harvester-forklift (rallumer la source, retirer
les instantanés de Forklift), contre un faux vCenter local : REST pour
l'alimentation, SOAP vim25 pour les instantanés. Les réponses SOAP sont
celles d'un vCenter 8.0.1 réel (banc vmwlab), capturées en lecture seule et
nettoyées (clé de session, compte, IP) ; les formes de tâche suivent le
schéma vim25 (aucune tâche récente à capturer sur le banc)."""

import base64
import json
import re
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin" / "lib"))
import vsphere_api as vs  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "vsphere_176"
USER = "operator@vsphere.local"
PASSWORD = 'S3cr<e>t&"pw'


def fixture(name, **subs):
    text = (FIX / name).read_text()
    for k, v in subs.items():
        text = text.replace(f"__{k.upper()}__", v)
    return text.encode()


class FakeVCenter:
    """Faux vCenter : /api (session Basic, alimentation) et /sdk (vim25)."""

    def __init__(self):
        self.tokens = set()
        self.power = {"vm-16": "POWERED_ON", "vm-18": "POWERED_OFF"}
        self.slow_power = {}       # vm -> secondes de retard avant de repondre
        self.snapshots = {"vm-16": "snapshots_forklift_chain.xml", "vm-18": "snapshots_none.xml"}
        self.questions = {}        # vm -> lectures sans question avant qu'elle apparaisse
        self.power_tasks = {}      # tâche d'allumage -> vm
        self.cookies = set()
        self.removed = []          # (snapshot, removeChildren, consolidate)
        self.fail_snaps = set()    # instantanés dont la tâche échoue
        self.task_polls = {}       # tâche -> états restants avant la fin
        self.task_final = {}
        self.soap_actions = []
        self.rest_calls = []
        self.logins = 0
        self.soap_html = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_GET(self):
                self._rest()

            def do_DELETE(self):
                self._rest()

            def do_POST(self):
                if self.path == "/sdk":
                    return self._soap()
                self._rest()

            # -- REST -------------------------------------------------------
            def _rest(self):
                path, _, query = self.path.partition("?")
                self._body()
                fake.rest_calls.append((self.command, path, query))
                if path == "/api/session" and self.command == "POST":
                    want = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
                    if self.headers.get("Authorization") != want:
                        return self._send(401, json.dumps({"error_type": "UNAUTHENTICATED"}).encode())
                    tok = f"tok-{len(fake.tokens) + 1}-{fake.logins}"
                    fake.logins += 1
                    fake.tokens.add(tok)
                    return self._send(201, json.dumps(tok).encode())
                tok = self.headers.get("vmware-api-session-id")
                if tok not in fake.tokens:
                    return self._send(401, json.dumps({"error_type": "UNAUTHENTICATED"}).encode())
                if path == "/api/session" and self.command == "DELETE":
                    fake.tokens.discard(tok)
                    return self._send(204)
                m = re.match(r"^/api/vcenter/vm/([^/]+)/power$", path)
                if not m:
                    return self._send(404, b"{}")
                vm = m.group(1)
                if vm not in fake.power:
                    return self._send(404, json.dumps({"error_type": "NOT_FOUND", "messages": [
                        {"default_message": f"VirtualMachine with identifier '{vm}' does not exist."}]}).encode())
                if self.command == "GET":
                    return self._send(200, json.dumps({"state": fake.power[vm]}).encode())
                if query != "action=start":
                    return self._send(400, b"{}")
                delay = fake.slow_power.get(vm)
                if delay:
                    fake.power[vm] = "POWERED_ON"
                    time.sleep(delay)
                    return self._send(204)
                if fake.power[vm] == "POWERED_ON":
                    return self._send(400, json.dumps({"error_type": "ALREADY_IN_DESIRED_STATE", "messages": [
                        {"default_message": "Virtual machine is already powered on."}]}).encode())
                fake.power[vm] = "POWERED_ON"
                return self._send(204)

            # -- SOAP -------------------------------------------------------
            def _xml(self, name, code=200, headers=None, **subs):
                self._send(code, fixture(name, **subs), "text/xml; charset=utf-8", headers)

            def _soap(self):
                if fake.soap_html:
                    self._body()
                    return self._send(200, b"<html>proxy login page", "text/html")
                fake.soap_actions.append(self.headers.get("SOAPAction"))
                body = ET.fromstring(self._body())
                op = body.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")[0]
                name = op.tag.split("}")[-1]
                V = "{urn:vim25}"
                if name == "RetrieveServiceContent":
                    return self._xml("service_content.xml")
                if name == "Login":
                    if op.findtext(f"{V}userName") != USER or op.findtext(f"{V}password") != PASSWORD:
                        return self._xml("fault_invalid_login.xml", 500)
                    fake.cookies.add("abc123")
                    return self._xml("login.xml", headers={
                        "Set-Cookie": 'vmware_soap_session="abc123"; Path=/; HttpOnly; Secure;'})
                cookie = self.headers.get("Cookie") or ""
                if 'vmware_soap_session="abc123"' not in cookie or "abc123" not in fake.cookies:
                    return self._xml("fault_not_authenticated.xml", 500)
                if name == "Logout":
                    fake.cookies.discard("abc123")
                    return self._xml("logout.xml")
                if name == "PowerOnVM_Task":
                    vm = op.findtext(f"{V}_this")
                    if vm not in fake.power:
                        return self._xml("fault_object_not_found.xml", 500)
                    task = f"task-p{len(fake.power_tasks) + 1}"
                    fake.power_tasks[task] = vm
                    if fake.power[vm] == "POWERED_ON":
                        fake.task_final[task] = "already"
                    return self._xml("power_on_task.xml", task=task)
                if name == "RemoveSnapshot_Task":
                    snap = op.findtext(f"{V}_this")
                    fake.removed.append((snap, op.findtext(f"{V}removeChildren"), op.findtext(f"{V}consolidate")))
                    task = f"task-{len(fake.removed)}"
                    fake.task_final[task] = "error" if snap in fake.fail_snaps else "success"
                    return self._xml("remove_snapshot_task.xml", task=task)
                if name == "RetrievePropertiesEx":
                    obj = op.find(f"{V}specSet/{V}objectSet/{V}obj")
                    kind, oid = obj.get("type"), obj.text
                    if kind == "VirtualMachine":
                        if oid not in fake.snapshots:
                            return self._xml("fault_object_not_found.xml", 500)
                        if op.findtext(f"{V}specSet/{V}propSet/{V}pathSet") == "runtime.question":
                            left = fake.questions.get(oid)
                            if left is None or left > 0:
                                if left:
                                    fake.questions[oid] = left - 1
                                return self._xml("snapshots_none.xml")
                            return self._xml("question_serial.xml")
                        return self._xml(fake.snapshots[oid])
                    if kind == "Task" and oid in fake.power_tasks:
                        vm = fake.power_tasks[oid]
                        if fake.task_final.get(oid) == "already":
                            return self._xml("task_error_power.xml", task=oid)
                        left = fake.task_polls.get(oid, 0)
                        # une question pendante retient la tâche
                        if vm in fake.questions or left:
                            fake.task_polls[oid] = max(left - 1, 0)
                            return self._xml("task_state.xml", task=oid, state="running")
                        fake.power[vm] = "POWERED_ON"
                        return self._xml("task_state.xml", task=oid, state="success")
                    if kind == "Task":
                        left = fake.task_polls.get(oid, 0)
                        if left:
                            fake.task_polls[oid] = left - 1
                            return self._xml("task_state.xml", task=oid, state="running")
                        if fake.task_final.get(oid) == "error":
                            return self._xml("task_error.xml", task=oid)
                        return self._xml("task_state.xml", task=oid, state=fake.task_final.get(oid, "success"))
                return self._send(500, b"unexpected", "text/plain")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def vc():
    fake = FakeVCenter()
    yield fake
    fake.close()


def client(fake, password=PASSWORD, user=USER):
    c = vs.VSphere(fake.url + "/sdk", user, password)
    c.sleep = lambda s: None
    return c


def assert_clean(msg):
    """Aucun identifiant, mot de passe ni chemin dans un message."""
    for secret in (USER, PASSWORD, "operator", "S3cr", "/api/", "/sdk", "/tmp", "/home"):
        assert secret not in msg, (secret, msg)


# -- forklift_snapshots (pure) -------------------------------------------------

def test_forklift_snapshots_leaves_first_and_only_forklift():
    tree = [{"id": "s-op", "name": "before", "children": [
        {"id": "s-a", "name": vs.FORKLIFT_SNAPSHOT, "children": [
            {"id": "s-b", "name": vs.FORKLIFT_SNAPSHOT, "children": []},
            {"id": "s-op2", "name": "keep-me", "children": [
                {"id": "s-c", "name": vs.FORKLIFT_SNAPSHOT, "children": []}]}]}]},
        {"id": "s-d", "name": vs.FORKLIFT_SNAPSHOT, "children": []}]
    assert vs.forklift_snapshots(tree) == ["s-b", "s-c", "s-a", "s-d"]
    assert vs.forklift_snapshots([]) == []
    assert vs.VSphere.forklift_snapshots(tree) == vs.forklift_snapshots(tree)


# -- REST ------------------------------------------------------------------------

def test_power_state_and_power_on(vc):
    c = client(vc)
    assert c.power_state("vm-16") == "POWERED_ON"
    assert c.power_state("vm-18") == "POWERED_OFF"
    assert c.power_on("vm-18") is True
    assert vc.power["vm-18"] == "POWERED_ON"
    # Déjà allumée : rejouable, pas une erreur, et aucune tâche lancée.
    assert c.power_on("vm-16") is False
    assert list(vc.power_tasks.values()) == ["vm-18"]
    # par la tâche SOAP, plus par l'API REST (qui reste bloquée sur une question)
    assert not [x for x in vc.rest_calls if x[0] == "POST" and x[1].endswith("/power")]
    tok = c.token
    c.logout()
    assert c.token is None and tok not in vc.tokens


def test_session_refused(vc):
    c = client(vc, password="wrong")
    with pytest.raises(vs.VSphereError) as e:
        c.power_state("vm-16")
    assert "session: refused (HTTP 401)" in str(e.value)
    assert_clean(str(e.value))


def test_expired_token_reopens_session_once(vc):
    c = client(vc)
    c.session()
    vc.tokens.clear()          # le vCenter a oublié la session
    assert c.power_state("vm-16") == "POWERED_ON"
    assert sum(1 for m, p, _ in vc.rest_calls if p == "/api/session" and m == "POST") == 2


def clocked(c):
    t = [0.0]
    c.clock = lambda: t[0]
    c.sleep = lambda s: t.__setitem__(0, t[0] + s)
    return t


def test_power_on_waits_for_a_slow_task(vc):
    """Un démarrage lent n'est pas une panne : la tâche est suivie."""
    vc.task_polls["task-p1"] = 3
    c = client(vc)
    t = clocked(c)
    assert c.power_on("vm-18") is True
    assert vc.power["vm-18"] == "POWERED_ON" and t[0] == 3 * vs.POWER_ON_POLL


def test_power_on_task_still_running_after_the_delay_is_an_error(vc):
    vc.task_polls["task-p1"] = 1000
    c = client(vc)
    c.power_on_timeout = 10
    clocked(c)
    with pytest.raises(vs.VSphereError) as e:
        c.power_on("vm-18")
    assert "power on vm-18: still running after 10 s" in str(e.value)
    assert not isinstance(e.value, vs.VSphereQuestion)
    assert_clean(str(e.value))


def test_power_on_raises_the_question_vmware_asks_while_starting(vc):
    """Vu sur vCenter 8.0.1 : la tâche reste en cours tant que la question du
    port série attend ; on la voit en secondes, pas après 5 minutes."""
    vc.questions["vm-18"] = 1          # la question paraît à la 2e lecture
    c = client(vc)
    t = clocked(c)
    with pytest.raises(vs.VSphereQuestion) as e:
        c.power_on("vm-18")
    assert e.value.question["choices"] == ["Append", "Replace", "Cancel"]
    assert "VMware waits for an answer: The serial port output file" in str(e.value)
    assert t[0] <= vs.POWER_ON_POLL and vc.power["vm-18"] == "POWERED_OFF"
    assert_clean(str(e.value))


def test_power_on_a_question_left_from_an_earlier_try_is_raised_without_a_new_task(vc):
    vc.questions["vm-18"] = 0
    c = client(vc)
    with pytest.raises(vs.VSphereQuestion):
        c.power_on("vm-18")
    assert vc.power_tasks == {}


def test_power_on_task_refused_as_already_on_is_not_an_error(vc):
    """L'état a changé entre la lecture et la tâche : InvalidPowerState."""
    c = client(vc)
    c.power_state = lambda vm: "POWERED_OFF"
    assert c.power_on("vm-16") is False


def test_power_unknown_vm(vc):
    c = client(vc)
    for f in (c.power_state, c.power_on):
        with pytest.raises(vs.VSphereError) as e:
            f("vm-404")
        assert "VM not found" in str(e.value)
        assert_clean(str(e.value))


# -- SOAP ------------------------------------------------------------------------

def test_soap_login_uses_api_version_and_escapes_password(vc):
    c = client(vc)
    c.soap_login()
    assert c.cookie == 'vmware_soap_session="abc123"'
    # RetrieveServiceContent sans version, puis la version annoncée.
    assert vc.soap_actions == ["urn:vim25", "urn:vim25/8.0.1.0"]
    c.soap_logout()
    assert c.cookie is None and "abc123" not in vc.cookies


def test_soap_login_refused(vc):
    c = client(vc, password="wrong")
    with pytest.raises(vs.VSphereError) as e:
        c.soap_login()
    assert str(e.value).endswith("login: login refused")
    assert_clean(str(e.value))


def test_snapshot_tree_real_chain(vc):
    c = client(vc)
    tree = c.snapshots("vm-16")
    assert [n["id"] for n in tree] == ["snapshot-1001"]
    assert tree[0]["created"] == "2026-09-29T18:53:18.75068Z"
    assert tree[0]["children"][0]["children"][0]["id"] == "snapshot-2001"
    assert vs.forklift_snapshots(tree) == ["snapshot-2001", "snapshot-1002", "snapshot-1001"]
    assert c.snapshots("vm-18") == []


def test_clear_mixed_tree_never_touches_operator_snapshots(vc):
    vc.snapshots["vm-16"] = "snapshots_mixed.xml"
    vc.task_polls = {"task-1": 2}
    c = client(vc)
    removed = c.clear_forklift_snapshots("vm-16", timeout=30, poll=0)
    assert removed == ["snapshot-2001", "snapshot-1002", "snapshot-1001"]
    assert [r[0] for r in vc.removed] == removed
    assert all(r[1:] == ("false", "true") for r in vc.removed)
    touched = {r[0] for r in vc.removed}
    assert "snapshot-900" not in touched and "snapshot-1500" not in touched


def test_failed_task_stops_the_cleanup(vc):
    vc.fail_snaps = {"snapshot-2001"}
    c = client(vc)
    with pytest.raises(vs.VSphereError) as e:
        c.clear_forklift_snapshots("vm-16", poll=0)
    assert "task task-1: failed: An error occurred while consolidating disks" in str(e.value)
    assert [r[0] for r in vc.removed] == ["snapshot-2001"]
    assert_clean(str(e.value))


def test_wait_task_timeout(vc):
    c = client(vc)
    task = c.remove_snapshot("snapshot-1001", consolidate=False)
    assert vc.removed[-1] == ("snapshot-1001", "false", "false")
    vc.task_polls[task] = 100
    t = [0.0]
    c.clock = lambda: t[0]
    c.sleep = lambda s: t.__setitem__(0, t[0] + s)
    with pytest.raises(vs.VSphereError) as e:
        c.wait_task(task, timeout=10, poll=4)
    assert "still running after 10 s" in str(e.value)


# -- correspondance des erreurs ------------------------------------------------------

def test_error_object_not_found(vc):
    with pytest.raises(vs.VSphereError) as e:
        client(vc).snapshots("vm-99999")
    assert str(e.value).endswith("snapshots of vm-99999: object not found")


def test_error_not_authenticated(vc):
    c = client(vc)
    c.soap_login()
    vc.cookies.clear()          # session SOAP expirée côté vCenter
    with pytest.raises(vs.VSphereError) as e:
        c.snapshots("vm-16")
    assert "not authenticated" in str(e.value)


def test_error_identifiers_validated_before_any_request(vc):
    c = client(vc)
    for bad in ("vm-1</obj><x>", "../session", "", None, "a" * 90):
        with pytest.raises(vs.VSphereError):
            c.snapshots(bad)
        with pytest.raises(vs.VSphereError):
            c.power_on(bad)
    with pytest.raises(vs.VSphereError):
        c.remove_snapshot("snap shot")
    assert vc.rest_calls == [] and vc.soap_actions == []


def test_error_unreachable_and_not_soap(vc):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    c = vs.VSphere(f"http://127.0.0.1:{port}", USER, PASSWORD, timeout=2)
    for f in (c.session, c.soap_login):
        with pytest.raises(vs.VSphereError) as e:
            f()
        assert "unreachable" in str(e.value)
        assert_clean(str(e.value))
    # Une réponse qui n'est pas du SOAP (page d'un proxy, par exemple).
    vc.soap_html = True
    with pytest.raises(vs.VSphereError) as e:
        client(vc).soap_login()
    assert "not a SOAP answer" in str(e.value)


def test_error_bad_url_and_unreadable_cacert(tmp_path):
    with pytest.raises(vs.VSphereError):
        vs.VSphere("ftp://vcenter", USER, PASSWORD)
    missing = tmp_path / "nowhere" / "ca.pem"
    with pytest.raises(vs.VSphereError) as e:
        vs.VSphere("https://vcenter", USER, PASSWORD, cacert=str(missing))
    assert str(e.value) == "unreadable CA certificate"
    with pytest.raises(vs.VSphereError):
        vs.VSphere("https://vcenter", USER, PASSWORD, cacert="-----BEGIN CERTIFICATE-----\nnope\n")


def test_fault_message_scrubs_credentials(vc):
    c = client(vc)
    fault = ET.fromstring(
        '<Fault><faultcode>ServerFaultCode</faultcode>'
        f'<faultstring>{escape(f"user {USER} with {PASSWORD} cannot do that")}</faultstring>'
        '<detail><SomeFault xmlns="urn:vim25"/></detail></Fault>')
    msg = str(c._fault(fault, "probe"))
    assert "***" in msg
    assert_clean(msg)


# -- question VMware (1.84.0) ------------------------------------------------------

def test_pending_question_none_when_vmware_asks_nothing(vc):
    assert client(vc).pending_question("vm-16") is None


def test_pending_question_reads_text_and_choices_on_one_line(vc):
    vc.questions["vm-16"] = 0
    q = client(vc).pending_question("vm-16")
    # réponse réelle d'un vCenter 8.0.1 (banc vmwlab) : `label` est une clé
    # interne, le libellé lisible est `summary`
    assert q["id"] == "3026" and q["choices"] == ["Append", "Replace", "Cancel"]
    assert q["text"].startswith('The serial port output file "/vmfs/volumes/') and "\n" not in q["text"]


def test_pending_question_text_is_capped(vc, monkeypatch):
    vc.questions["vm-16"] = 0
    monkeypatch.setattr(vs, "QUESTION_TEXT_MAX", 40)
    q = client(vc).pending_question("vm-16")
    assert len(q["text"]) <= 40 and q["text"].endswith("...")
