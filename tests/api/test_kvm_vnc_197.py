"""v1.97.0 : console VNC d'une VM KVM, par tunnel SSH.

Trois étages :
- `vnc_des` : DES d'un bloc en Python pur, sur des vecteurs publiés, et la
  réponse au défi VNC, comparée à une implémentation de référence (OpenSSL) ;
- `kvm_vnc.SshStdioUpstream` sur de vrais processus locaux (`cat` comme
  écho, un faux serveur RFB à mot de passe lancé à la place de ssh) ;
- les routes de la console : ticket, état, websocket, partage, retrait
  d'hôte, rôles, et le mot de passe qui ne sort jamais.
"""

import base64
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")
import app as wapp  # noqa: E402
import kvm_vnc  # noqa: E402
import vnc_des  # noqa: E402
import vnc_mux  # noqa: E402

kd, kh = wapp._kd, wapp._kh
SRC = (ROOT / "web" / "app.py").read_text()
FIX = ROOT / "tests" / "fixtures" / "kvm"
FAKE_PASSWORD = "Fk7pw3Zq"       # valeur factice du jeu d'essai dumpxml-vnc-password.xml
CHALLENGE = bytes(range(16))
# OpenSSL, `openssl enc -des-ecb -K <clé aux bits inversés> -nopad` (fournisseur
# legacy), calculé une fois et figé : défi 00..0f, mot de passe factice
FAKE_RESPONSE = "605e8da8b5747d3fa22b1892e9ab713d"


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# vnc_des
# ---------------------------------------------------------------------------

# (clé, clair, chiffré) publiés :
# - J. Orlin Grabbe, « The DES Algorithm Illustrated » (exemple déroulé pas à pas) ;
# - NBS Special Publication 500-20 (1980), « Validating the correctness of
#   hardware implementations of the NBS Data Encryption Standard » : test de
#   la permutation initiale et de l'expansion (clé 0101..., clair 8000...,
#   ...0001), test des clés (8001...), données d'exemple des S-boîtes
#   (7CA110454A1A6E57), et le vecteur 0E329232EA6D0D73 / 8787878787878787
#   qui chiffre en zéros (repris dans de nombreuses suites de test DES).
DES_VECTORS = [
    ("133457799BBCDFF1", "0123456789ABCDEF", "85E813540F0AB405"),
    ("0E329232EA6D0D73", "8787878787878787", "0000000000000000"),
    ("0101010101010101", "8000000000000000", "95F8A5E5DD31D900"),
    ("0101010101010101", "0000000000000001", "166B40B44ABA4BD6"),
    ("8001010101010101", "0000000000000000", "95A8D72813DAA94D"),
    ("7CA110454A1A6E57", "01A1D6D039776742", "690F5B0D9A26939B"),
]


@pytest.mark.parametrize("key,plain,cipher", DES_VECTORS)
def test_des_block_matches_published_vectors(key, plain, cipher):
    assert vnc_des.encrypt_block(bytes.fromhex(key), bytes.fromhex(plain)).hex().upper() == cipher


# Réponses VNC de référence (OpenSSL, clé = mot de passe sur 8 octets aux
# bits inversés), figées une fois pour toutes.
VNC_VECTORS = [
    (FAKE_PASSWORD, CHALLENGE.hex(), FAKE_RESPONSE),
    ("secret", "00112233445566778899aabbccddeeff", "f19b50471f60f42298e5c0147db50e1e"),
    # au-delà de 8 octets, seul le début compte (comme QEMU)
    ("longerthan8chars", bytes(range(16, 32)).hex(), "b6228cc62ef7f3c96640afbdc85b5f67"),
    ("longerth", bytes(range(16, 32)).hex(), "b6228cc62ef7f3c96640afbdc85b5f67"),
]


@pytest.mark.parametrize("password,challenge,response", VNC_VECTORS)
def test_vnc_response_matches_the_reference(password, challenge, response):
    assert vnc_des.vnc_response(password, bytes.fromhex(challenge)).hex() == response


def test_vnc_key_reverses_the_bits_of_each_byte_and_pads():
    assert vnc_des.vnc_key("\x01") == b"\x80" + b"\x00" * 7
    assert vnc_des.vnc_key(b"\x03\xf0") == b"\xc0\x0f" + b"\x00" * 6


@pytest.mark.parametrize("call", [
    lambda: vnc_des.encrypt_block(b"1234567", b"12345678"),
    lambda: vnc_des.encrypt_block(b"12345678", b"123"),
    lambda: vnc_des.vnc_response("pw", b"short"),
])
def test_des_refuses_wrong_lengths(call):
    with pytest.raises(ValueError):
        call()


# ---------------------------------------------------------------------------
# SshStdioUpstream sur de vrais processus
# ---------------------------------------------------------------------------

def test_upstream_sends_receives_and_times_out_against_cat():
    up = kvm_vnc.SshStdioUpstream(["sh", "-c", "cat"])
    try:
        assert up.receive(timeout=0.1) is None and up.connected
        up.send(b"hello")
        got = b""
        end = time.time() + 3
        while len(got) < 5 and time.time() < end:
            got += up.receive(timeout=0.5) or b""
        assert got == b"hello"
    finally:
        up.close()
    assert up.proc.poll() is not None and not up.connected
    with pytest.raises(ConnectionError):
        up.send(b"x")


def test_upstream_reports_the_end_of_the_stream():
    up = kvm_vnc.SshStdioUpstream(["sh", "-c", "printf abc"])
    data = b""
    while True:
        chunk = up.receive(timeout=2)
        if chunk is None:
            break
        data += chunk
    assert data == b"abc" and not up.connected
    up.close()


def test_close_kills_a_process_that_ignores_sigterm():
    up = kvm_vnc.SshStdioUpstream(["sh", "-c", "trap '' TERM; while :; do sleep 0.1; done"])
    time.sleep(0.2)
    t0 = time.time()
    up.close()
    assert up.proc.poll() is not None
    assert time.time() - t0 < kvm_vnc.CLOSE_GRACE + 2
    up.close()                       # sans effet la seconde fois


class FakeProc:
    """Un Popen factice : rend des tuyaux réels, note ce qu'on lui fait."""
    def __init__(self, argv, **kw):
        self.argv, self.kw, self.calls = argv, kw, []
        r_out, self._w_out = os.pipe()
        r_err, self._w_err = os.pipe()
        self._r_in, w_in = os.pipe()
        self.stdout, self.stderr = os.fdopen(r_out, "rb", 0), os.fdopen(r_err, "rb", 0)
        self.stdin = os.fdopen(w_in, "wb", 0)
        self.pid, self.rc = 4242, None

    def poll(self):
        return self.rc

    def terminate(self):
        self.calls.append("terminate")

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if "kill" not in self.calls:
            raise subprocess.TimeoutExpired("ssh", timeout)
        self.rc = -9
        for fd in (self._w_out, self._w_err):
            os.close(fd)
        return self.rc

    def kill(self):
        self.calls.append("kill")


def test_upstream_popen_arguments_and_kill_sequence():
    made = []
    up = kvm_vnc.SshStdioUpstream(["ssh", "-W", "127.0.0.1:5900"],
                                  popen=lambda a, **k: made.append(FakeProc(a, **k)) or made[-1])
    p = made[0]
    assert p.argv == ["ssh", "-W", "127.0.0.1:5900"]
    assert p.kw == {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE, "start_new_session": True}
    up.vnc_password = "x"
    up.close()
    assert p.calls[0] == "terminate" and "kill" in p.calls
    assert up.vnc_password is None
    assert "x" not in repr(up)


def test_failure_reason_names_the_closed_display_and_hides_paths():
    up = kvm_vnc.SshStdioUpstream(
        ["sh", "-c", "echo 'channel 0: open failed: connect failed: Connection refused' >&2"])
    assert wait_for(lambda: up.receive(timeout=0.2) is None and not up.connected)
    up.close()
    assert up.failure_reason() == "the VM display is not reachable from the host (connection refused)"
    up = kvm_vnc.SshStdioUpstream(
        ["sh", "-c", "echo \"Warning: identity file /secret/state/ssh/node2_id passwd='zz' bad\" >&2"],
        redact=["/secret/state"])
    assert wait_for(lambda: up.receive(timeout=0.2) is None and not up.connected)
    up.close()
    reason = up.failure_reason()
    assert "/secret/state" not in reason and "zz" not in reason and "<state>" in reason


# ---------------------------------------------------------------------------
# Un faux serveur RFB, lancé à la place de ssh : il parle sur son entrée et sa
# sortie standard comme le ferait QEMU au bout du `ssh -W`.
# ---------------------------------------------------------------------------

FAKE_RFB = r'''
import os, struct, sys, json
mode, expected, log = sys.argv[1], sys.argv[2], sys.argv[3]
out = sys.stdout.buffer
def rd(n):
    b = b""
    while len(b) < n:
        c = os.read(0, n - len(b))
        if not c:
            sys.exit(0)
        b += c
    return b
def note(**k):
    with open(log, "a") as f:
        f.write(json.dumps(k) + "\n")
out.write(b"RFB 003.008\n"); out.flush()
rd(12)
if mode == "refuse":
    why = b"<b>Too many</b> & failures"
    out.write(b"\x00" + struct.pack(">I", len(why)) + why); out.flush()
    sys.exit(0)
types = {"vnc": [2], "both": [2, 1], "none": [1], "vencrypt": [19], "sasl": [20],
         "tls": [18]}[mode]
out.write(bytes([len(types)] + types)); out.flush()
choice = rd(1)[0]
note(choice=choice)
if choice == 2:
    out.write(bytes(range(16))); out.flush()
    ok = rd(16).hex() == expected
    note(auth=ok)
    if not ok:
        out.write(b"\x00\x00\x00\x01" + struct.pack(">I", 21) + b"Authentication failed"); out.flush()
        sys.exit(0)
out.write(b"\x00\x00\x00\x00"); out.flush()
rd(1)
pf = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
out.write(struct.pack(">HH", 640, 480) + pf + struct.pack(">I", 7) + b"kvmtest"); out.flush()
while os.read(0, 65536):
    pass
'''


@pytest.fixture
def fake_rfb(tmp_path):
    script = tmp_path / "fake_rfb.py"
    script.write_text(FAKE_RFB)
    log = tmp_path / "rfb.log"

    def argv(mode="vnc", expected=FAKE_RESPONSE):
        return [sys.executable, "-I", str(script), mode, expected, str(log)]

    def notes():
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return argv, notes


@pytest.fixture(autouse=True)
def _clean_hubs():
    vnc_mux._hubs.clear()
    vnc_mux._creating.clear()
    yield
    for h in list(vnc_mux._hubs.values()):
        h.close("test end")
    vnc_mux._hubs.clear()


def test_rfb_auth_type_2_through_a_real_process(fake_rfb):
    argv, notes = fake_rfb
    up = kvm_vnc.SshStdioUpstream(argv("vnc"))
    up.vnc_password = FAKE_PASSWORD
    hub = vnc_mux.VncHub(("kvm:node2", "vm1"), up)
    hub.start(timeout=5)
    assert (hub.width, hub.height, hub.name) == (640, 480, b"kvmtest")
    assert notes() == [{"choice": 2}, {"auth": True}]
    assert up.vnc_password is None
    hub.close("test")
    assert wait_for(lambda: up.proc.poll() is not None)


def test_rfb_wrong_password_through_a_real_process(fake_rfb):
    argv, notes = fake_rfb
    up = kvm_vnc.SshStdioUpstream(argv("vnc"))
    up.vnc_password = "wrong-pw"
    hub = vnc_mux.VncHub(("kvm:node2", "vm1"), up)
    with pytest.raises(vnc_mux.ProtocolError, match="refused the password"):
        hub.start(timeout=5)
    assert notes() == [{"choice": 2}, {"auth": False}]
    up.close()


def test_rfb_prefers_none_through_a_real_process(fake_rfb):
    argv, notes = fake_rfb
    up = kvm_vnc.SshStdioUpstream(argv("both"))
    up.vnc_password = FAKE_PASSWORD
    hub = vnc_mux.VncHub(("kvm:node2", "vm1"), up)
    hub.start(timeout=5)
    assert notes() == [{"choice": 1}]
    hub.close("test")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def fx(name):
    return (FIX / name).read_text()


UEFI = fx("dumpxml-kvmtest-uefi.xml")          # VNC 0.0.0.0:5901, en marche
PASSWORD_XML = fx("dumpxml-vnc-password.xml")  # VNC 127.0.0.1:5900, passwd factice
SPICE = fx("dumpxml-spice-only.xml")
SOCKET = fx("dumpxml-vnc-socket.xml")
HOSTNAME = UEFI.replace("listen='0.0.0.0'", "listen='node2.home.lo'").replace(
    "address='0.0.0.0'", "address='node2.home.lo'")
OTHER_IP = UEFI.replace("0.0.0.0", "172.16.1.99")
HOST_IP = UEFI.replace("0.0.0.0", "172.16.1.12")
NOLISTEN = re.sub(r"<graphics type='vnc'.*?</graphics>",
                  "<graphics type='vnc' autoport='yes'><listen type='none'/></graphics>", UEFI, flags=re.S)
NODISPLAY = re.sub(r"<graphics type='vnc'.*?</graphics>", "", UEFI, flags=re.S)
IP_ADDR = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
           "2: br0    inet 172.16.1.12/16 brd 172.16.255.255 scope global br0\n")
HOSTILE = ["a;rm -rf ~", "$(id)", "-x", "x y", "<img src=x onerror=alert(1)>", "--json"]


def decl(name, address):
    return {"name": name, "address": address, "port": 22, "user": "ju", "ssh_key": kd.key_rel(name),
            "known_hosts": kd.known_hosts_rel(name), "labels": {}}


@pytest.fixture
def env(monkeypatch, fake_rfb):
    state = Path(tempfile.mkdtemp(prefix="kvv-", dir="/tmp"))
    kd.write(state, decl("node2", "172.16.1.12"))
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    # les websockets des tests s'authentifient en Basic avec « pw »
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: p == "pw")
    sim = {"domains": {"web": ("running", UEFI)}, "batches": [], "runs": [], "popen": [],
           "procs": [], "mode": "none"}

    def fake_batch(self, commands, timeout=None, cmd_timeout=None):
        sim["batches"].append(dict(commands))
        out = {}
        for k, cmd in commands.items():
            name = cmd[-1]
            if name not in sim["domains"]:
                out[k] = kh.CmdOutput("", rc=1, error=kh.KvmError(
                    "notfound", f"node2: notfound: error: failed to get domain '{name}'"))
            elif cmd[0] == "domstate":
                out[k] = kh.CmdOutput(sim["domains"][name][0] + "\n")
            else:
                out[k] = kh.CmdOutput(sim["domains"][name][1])
        return out
    monkeypatch.setattr(kh.VirshHost, "batch", fake_batch)

    def fake_run(self, remote_argv, timeout=None):
        sim["runs"].append(list(remote_argv))
        return IP_ADDR
    monkeypatch.setattr(kh.VirshHost, "run", fake_run)

    def fake_virsh(self, *args, timeout=None):
        name = args[-1]
        if name not in sim["domains"]:
            raise kh.KvmError("notfound", "node2: notfound: no domain")
        return kh.CmdOutput(sim["domains"][name][0] + "\n")
    monkeypatch.setattr(kh.VirshHost, "virsh", fake_virsh)

    rfb_argv, _notes = fake_rfb

    def popen(argv, **kw):
        # la ligne de ssh est notée, puis le faux serveur RFB la remplace
        sim["popen"].append(list(argv))
        p = subprocess.Popen(rfb_argv(sim["mode"]), **kw)
        sim["procs"].append(p)
        return p
    monkeypatch.setattr(kvm_vnc, "_popen", popen)
    saved = dict(wapp._vnc_last_close)
    wapp._vnc_last_close.clear()
    yield {"state": state, "sim": sim, "notes": _notes}
    for p in sim["procs"]:
        if p.poll() is None:
            p.kill()
            p.wait()
    wapp._vnc_last_close.clear()
    wapp._vnc_last_close.update(saved)
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

    def call(user, method, path, **kw):
        token = base64.b64encode(f"{user}:x".encode()).decode()
        with wapp.app.test_client() as c:
            return c.open(path, method=method, headers={"Authorization": f"Basic {token}"}, **kw)
    return call


def ticket(client, domain="web", host="node2"):
    return client.post(f"/api/kvm/vms/{host}/{quote(domain, safe='')}/console-ticket")


def test_ticket_for_a_running_vm(client, env):
    r = ticket(client)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ws_path"] == "/ws/kvm/vnc/node2/web"
    assert body["password"] is False and body["expires_in"] == wapp._VNC_TICKET_TTL
    # le XML est lu avec --security-info : la présence d'un mot de passe se sait
    assert env["sim"]["batches"][-1]["xml"] == ("dumpxml", "--security-info", "--domain", "web")
    entry = wapp._vnc_take_ticket(body["ticket"], "kvm:node2", "", "web", kind="kvm-vnc")
    assert entry and entry["kind"] == "kvm-vnc"


def test_ticket_of_a_password_display_says_so_without_the_value(client, env):
    env["sim"]["domains"]["pw"] = ("running", PASSWORD_XML)
    r = ticket(client, "pw")
    assert r.status_code == 200 and r.get_json()["password"] is True
    assert FAKE_PASSWORD not in r.get_data(as_text=True)


@pytest.mark.parametrize("state,xml,code,reason", [
    ("shut off", UEFI, 409, "notrunning"),
    ("running", SPICE, 422, "spice"),
    ("running", SOCKET, 422, "socket"),
    ("running", HOSTNAME, 422, "listen"),
    ("running", OTHER_IP, 422, "listen"),
    ("running", NOLISTEN, 422, "nolisten"),
    ("running", NODISPLAY, 422, "nodisplay"),
])
def test_ticket_refusals(client, env, state, xml, code, reason):
    env["sim"]["domains"]["web"] = (state, xml)
    r = ticket(client)
    assert r.status_code == code, r.get_json()
    body = r.get_json()
    assert body["reason"] == reason and body["error"] == kvm_vnc.REASON_MESSAGES[reason]
    assert body["terminal"] is (code == 422)
    assert "ticket" not in body


def test_spice_message_announces_the_settings_of_1_98(client, env):
    env["sim"]["domains"]["web"] = ("running", SPICE)
    assert "possible from the VM settings in 1.98" in ticket(client).get_json()["error"]


def test_an_ip_of_the_host_itself_is_accepted_after_reading_its_addresses(client, env):
    env["sim"]["domains"]["web"] = ("running", HOST_IP)
    r = ticket(client)
    assert r.status_code == 200
    assert env["sim"]["runs"] == [["env", "LC_ALL=C", "ip", "-o", "addr", "show"]]


@pytest.mark.parametrize("domain,code", [("nosuch", 404), ("a\x01b", 400), ("a" * 256, 400)])
def test_ticket_unknown_or_invalid_domain(client, domain, code):
    assert ticket(client, domain).status_code == code


def test_ticket_unreachable_host_is_502(client, monkeypatch):
    def boom(self, commands, timeout=None, cmd_timeout=None):
        raise kh.KvmError("unreachable", "node2: unreachable: ssh: connect to host 172.16.1.12 port 22: No route")
    monkeypatch.setattr(kh.VirshHost, "batch", boom)
    r = ticket(client)
    assert r.status_code == 502 and r.get_json()["kind"] == "unreachable"


@pytest.mark.parametrize("host,code", [("node9", 404), ("Bad_Name!", 400)])
def test_ticket_undeclared_or_invalid_host(client, host, code):
    assert ticket(client, host=host).status_code == code


def test_ticket_session_cap(client, monkeypatch):
    monkeypatch.setattr(wapp, "_vnc_viewers", lambda: wapp._VNC_MAX_SESSIONS)
    assert ticket(client).status_code == 429


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_domain_names_stay_one_argument_and_never_reach_ssh(client, env, name):
    env["sim"]["domains"][name] = ("running", UEFI)
    r = ticket(client, name)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["ws_path"] == f"/ws/kvm/vnc/node2/{quote(name, safe='')}"
    assert env["sim"]["batches"][-1]["state"] == ("domstate", "--domain", name)
    ws, t = attach_browser(r.get_json()["ticket"], name)
    assert wait_for(lambda: env["sim"]["popen"])
    argv = env["sim"]["popen"][-1]
    assert name not in argv and argv[argv.index("-W") + 1] == "127.0.0.1:5901"
    assert "ControlMaster=no" in argv and "StrictHostKeyChecking=yes" in argv
    ws.close()
    t.join(timeout=5)


# --- rôles et limites -------------------------------------------------------

@pytest.mark.parametrize("method,path,role", [
    ("POST", "/api/kvm/vms/node2/web/console-ticket", "operator"),
    ("GET", "/api/kvm/vms/node2/web/console-status", "viewer"),
])
def test_required_role_of_the_console_routes(method, path, role):
    assert wapp.required_role_for(path, method) == role


@pytest.mark.parametrize("user,allowed", [("curieux", False), ("alice", True), ("patronne", True)])
def test_console_ticket_role_is_enforced(as_user, user, allowed):
    r = as_user(user, "POST", "/api/kvm/vms/node2/web/console-ticket")
    if allowed:
        assert r.status_code == 200, r.get_json()
    else:
        assert r.status_code == 403 and r.get_json()["required"] == "operator"


def test_console_status_is_readable_by_a_viewer(as_user):
    r = as_user("curieux", "GET", "/api/kvm/vms/node2/web/console-status")
    assert r.status_code == 200 and r.get_json() == {"viewers": [], "count": 0, "last_close": None}


def test_ticket_route_is_authenticated_and_rate_limited():
    m = re.search(r'@app\.route\("/api/kvm/vms/<host>/<domain>/console-ticket", methods=\["POST"\]\)\n'
                  r'@requires_auth\n@_rate_limit\("([^"]+)"\)\ndef api_kvm_console_ticket', SRC)
    assert m and m.group(1) == "30/minute"
    wapp._validate_rate_spec(m.group(1))
    assert re.search(r'@app\.route\("/api/kvm/vms/<host>/<domain>/console-status"\)\n@requires_auth\n', SRC)


def test_the_ticket_route_wins_over_the_gesture_route():
    adapter = wapp.app.url_map.bind("localhost")
    assert adapter.match("/api/kvm/vms/node2/web/console-ticket", method="POST")[0] == "api_kvm_console_ticket"
    assert adapter.match("/api/kvm/vms/node2/web/start", method="POST")[0] == "api_kvm_vm_gesture"


def test_a_console_ticket_invalidates_no_shared_read(client, env, monkeypatch):
    seen = []
    monkeypatch.setattr(wapp.READ_SHARE, "invalidate", lambda scope: seen.append(scope))
    ticket(client)
    assert seen == []


# --- websocket : partage, isolement, fin du tunnel ----------------------------

class BrowserWs:
    """Un navigateur noVNC : poignée de main RFB en sécurité None."""
    def __init__(self):
        import queue
        self.inbox, self.sent, self.connected = queue.Queue(), bytearray(), True
        self.close_message = None
        for d in (b"RFB 003.008\n", b"\x01", b"\x01"):
            self.inbox.put(d)

    def receive(self, timeout=None):
        import queue
        if not self.connected:
            return None
        try:
            item = self.inbox.get(timeout=min(timeout or 0.05, 0.05))
        except queue.Empty:
            return None
        if item is None:
            self.connected = False
        return item

    def send(self, data):
        if not self.connected:
            raise ConnectionError("closed")
        self.sent += data

    def close(self, *a, message=None, **k):
        self.close_message = self.close_message or message
        self.connected = False
        self.inbox.put(None)


def basic(user):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:pw".encode()).decode()}


def attach_browser(tok, domain="web", host="node2", user="alice"):
    """Le websocket vient de la session de `user` (en-tête Basic ; None :
    aucune session)."""
    ws = BrowserWs()
    view = wapp.app.view_functions["ws_kvm_vnc"].__wrapped__
    headers = basic(user) if user else {}

    def run():
        with wapp.app.test_request_context(f"/ws/kvm/vnc/{host}/x?ticket={tok}", headers=headers):
            view(ws, host, domain)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return ws, t


def new_ticket(domain="web", host="node2"):
    return wapp._vnc_issue_ticket(f"kvm:{host}", "", domain, user="alice", kind="kvm-vnc")


KEY = ("kvm:node2", "web")


def test_two_browsers_share_one_tunnel_closed_by_the_last_one(env):
    ws1, t1 = attach_browser(new_ticket())
    assert wait_for(lambda: vnc_mux.sessions().get(KEY) == ["alice"])
    ws2, t2 = attach_browser(new_ticket())
    assert wait_for(lambda: vnc_mux.sessions().get(KEY) == ["alice", "alice"])
    assert len(env["sim"]["popen"]) == 1, "un tunnel par navigateur"
    proc = env["sim"]["procs"][0]
    ws1.close(); t1.join(timeout=5)
    assert proc.poll() is None, "le tunnel tombe alors qu'un navigateur reste"
    ws2.close(); t2.join(timeout=5)
    assert wait_for(lambda: proc.poll() is not None), "le dernier parti laisse ssh -W vivant"
    assert KEY not in vnc_mux.sessions()


def test_host_remove_cuts_every_tunnel_of_the_host(env, client):
    ws, t = attach_browser(new_ticket())
    assert wait_for(lambda: vnc_mux.sessions().get(KEY))
    proc = env["sim"]["procs"][0]
    started = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wapp, "_kvm_action", lambda host, label, cmd, cleanup=None: (
            started.append((label, cleanup)), (type("R", (), {"id": "x"})(), None))[1])
        r = client.delete("/api/kvm/hosts/node2")
    assert r.status_code == 202
    assert wait_for(lambda: proc.poll() is not None), "le tunnel survit au retrait de l'hôte"
    t.join(timeout=5)
    assert not ws.connected and KEY not in vnc_mux.sessions()
    st = client.get("/api/kvm/vms/node2/web/console-status").get_json()
    assert st["last_close"]["reason"] == "host-removed"
    # la fin du retrait coupe aussi une console rouverte entre-temps
    [(label, cleanup)] = started
    ws2, t2 = attach_browser(new_ticket())
    assert wait_for(lambda: vnc_mux.sessions().get(KEY))
    cleanup()
    t2.join(timeout=5)
    assert KEY not in vnc_mux.sessions()


def test_host_remove_leaves_other_hosts_and_kubevirt_consoles(env):
    import queue as _q
    class Up:
        def __init__(self):
            self.inbox, self.connected = _q.Queue(), True
            pf = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
            for d in (b"RFB 003.008\n", b"\x01\x01", b"\x00\x00\x00\x00",
                      struct.pack(">HH", 8, 8) + pf + struct.pack(">I", 1) + b"v"):
                self.inbox.put(d)
        def receive(self, timeout=None):
            try:
                return self.inbox.get(timeout=min(timeout or 0.05, 0.05))
            except _q.Empty:
                return None
        def send(self, data):
            pass
        def close(self):
            self.connected = False
    kube = vnc_mux.get_hub(("node2", "default", "web"), Up)
    other = vnc_mux.get_hub(("kvm:node3", "web"), Up)
    mine = vnc_mux.get_hub(KEY, Up)
    assert wapp._kvm_vnc_cut_host("node2") == [KEY]
    assert mine.closed and not kube.closed and not other.closed


def test_kubevirt_and_kvm_tickets_never_cross(env):
    kube = wapp._vnc_issue_ticket("node2", "", "web")
    assert wapp._vnc_take_ticket(kube, "kvm:node2", "", "web", kind="kvm-vnc") is None
    kvm = new_ticket()
    assert wapp._vnc_take_ticket(kvm, "kvm:node2", "", "web") is None          # genre "vnc"
    # la route KubeVirt refuse un nom de cluster à deux-points
    ws = BrowserWs()
    with wapp.app.test_request_context(f"/ws/vnc/kvm:node2/x/web?ticket={new_ticket()}"):
        wapp.app.view_functions["ws_vnc"].__wrapped__(ws, "kvm:node2", "x", "web")
    assert ws.close_message == "invalid cluster"
    # un ticket KubeVirt sur la route KVM
    ws, t = attach_browser(wapp._vnc_issue_ticket("kvm:node2", "", "web", kind="vnc"))
    t.join(timeout=5)
    assert ws.close_message == "invalid or expired ticket" and env["sim"]["popen"] == []


def test_a_ticket_opens_only_its_own_domain(env):
    ws, t = attach_browser(new_ticket("other"), "web")
    t.join(timeout=5)
    assert ws.close_message == "invalid or expired ticket" and env["sim"]["popen"] == []


def test_a_display_gone_between_ticket_and_dial_is_remembered(env, client):
    env["sim"]["domains"]["web"] = ("shut off", UEFI)
    ws, t = attach_browser(new_ticket())
    t.join(timeout=5)
    assert ws.close_message == "notrunning" and env["sim"]["popen"] == []
    assert client.get("/api/kvm/vms/node2/web/console-status").get_json()["last_close"]["reason"] == "notrunning"


def test_the_port_is_read_from_the_live_xml_at_each_dial(env):
    ws, t = attach_browser(new_ticket())
    assert wait_for(lambda: env["sim"]["popen"])
    ws.close(); t.join(timeout=5)
    env["sim"]["domains"]["web"] = ("running", UEFI.replace("port='5901'", "port='5907'"))
    ws, t = attach_browser(new_ticket())
    assert wait_for(lambda: len(env["sim"]["popen"]) == 2)
    ws.close(); t.join(timeout=5)
    assert [a[a.index("-W") + 1] for a in env["sim"]["popen"]] == ["127.0.0.1:5901", "127.0.0.1:5907"]


def test_vm_stopped_while_watched_is_classified(env, client):
    ws, t = attach_browser(new_ticket())
    assert wait_for(lambda: vnc_mux.sessions().get(KEY))
    env["sim"]["domains"]["web"] = ("shut off", UEFI)
    env["sim"]["procs"][0].kill()            # QEMU parti : le tunnel se ferme
    t.join(timeout=5)
    st = client.get("/api/kvm/vms/node2/web/console-status").get_json()
    assert st["last_close"]["reason"] == "vm-stopped"


# --- le mot de passe ne sort jamais -----------------------------------------

def test_the_password_never_leaves_the_process(env, client, caplog):
    caplog.set_level(logging.DEBUG)
    env["sim"]["domains"]["web"] = ("running", PASSWORD_XML)
    env["sim"]["mode"] = "vnc"
    outputs = []
    r = ticket(client)
    outputs.append(r.get_data(as_text=True))
    ws, t = attach_browser(r.get_json()["ticket"])
    assert wait_for(lambda: vnc_mux.sessions().get(KEY)), env["notes"]()
    assert env["notes"]() == [{"choice": 2}, {"auth": True}]
    hub = vnc_mux._hubs[KEY]
    assert hub.up.vnc_password is None
    # le navigateur n'a vu que la sécurité None (une sécurité, type 1)
    assert bytes(ws.sent)[:14] == b"RFB 003.008\n\x01\x01"
    outputs += [repr(hub.up), bytes(ws.sent).decode("latin-1")]
    outputs.append(client.get("/api/kvm/vms/node2/web/console-status").get_data(as_text=True))
    outputs.append(" ".join(env["sim"]["popen"][0]))
    ws.close(); t.join(timeout=5)
    # mot de passe changé sur l'hôte : refus dit, sans la valeur
    env["sim"]["domains"]["web"] = ("running", PASSWORD_XML.replace(FAKE_PASSWORD, "Zz9other"))
    ws, t = attach_browser(new_ticket())
    t.join(timeout=5)
    st = client.get("/api/kvm/vms/node2/web/console-status").get_data(as_text=True)
    assert json.loads(st)["last_close"]["reason"] == "auth"
    assert "refused the password" in json.loads(st)["last_close"]["detail"]
    outputs += [st, str(ws.close_message), json.dumps({str(k): v for k, v in wapp._vnc_last_close.items()})]
    outputs += [rec.getMessage() for rec in caplog.records]
    for text in outputs:
        assert FAKE_PASSWORD not in text and "Zz9other" not in text


# --- après revue ---------------------------------------------------------------

class _Up:
    def __init__(self, reason):
        self._reason = reason

    def failure_reason(self):
        return self._reason


@pytest.mark.parametrize("exc,up,reason,detail", [
    # une erreur RFB dit son propre message, jamais la dernière ligne de ssh
    (vnc_mux.ProtocolError("server refused: busy"), _Up("ssh: last line"), "unreachable",
     "server refused: busy"),
    (vnc_mux.AuthFailed("the VM display refused the password"), _Up("ssh: x"), "auth",
     "the VM display refused the password"),
    (vnc_mux.UnsupportedSecurity("offered: VeNCrypt (19)"), _Up("ssh: x"), "unsupported-security",
     "offered: VeNCrypt (19)"),
    # un message qui contient « password » ne suffit plus à faire un refus
    (vnc_mux.ProtocolError("password field garbled"), None, "unreachable", "password field garbled"),
    # panne du transport : la raison que ssh a donnée
    (EOFError("connection closed"), _Up("ssh: connect to host node2 port 22: refused"), "unreachable",
     "ssh: connect to host node2 port 22: refused"),
    (OSError("spawn failed"), None, "unreachable", "OSError"),
    (kvm_vnc.DisplayUnavailable("socket"), None, "unreachable", kvm_vnc.REASON_MESSAGES["socket"]),
])
def test_failure_classification_uses_exception_types(exc, up, reason, detail):
    assert wapp._kvm_vnc_failure(exc, up) == (reason, detail)


@pytest.mark.parametrize("mode,reason,detail", [
    ("vencrypt", "unsupported-security", "server offers no 'None' security type nor VNC password "
                                         "authentication (offered: VeNCrypt (19))"),
    ("sasl", "unsupported-security", "server offers no 'None' security type nor VNC password "
                                     "authentication (offered: SASL (20))"),
    ("tls", "unsupported-security", "server offers no 'None' security type nor VNC password "
                                    "authentication (offered: TLS (18))"),
    # la raison écrite par le serveur arrive telle quelle (la page l'échappe)
    ("refuse", "unreachable", "server refused: <b>Too many</b> & failures"),
])
def test_a_refusing_display_is_remembered_with_its_own_message(env, client, mode, reason, detail):
    env["sim"]["mode"] = mode
    ws, t = attach_browser(new_ticket())
    t.join(timeout=5)
    assert not t.is_alive()
    last = client.get("/api/kvm/vms/node2/web/console-status").get_json()["last_close"]
    assert (last["reason"], last["detail"]) == (reason, detail)
    assert ws.close_message == "VM display unreachable"
    # une seule ouverture : pas de nouvel essai côté serveur
    assert len(env["sim"]["popen"]) == 1
    assert KEY not in vnc_mux.sessions()


@pytest.mark.parametrize("ticket_user,ws_user,opens", [
    ("alice", "alice", True),
    ("alice", "bob", False),           # ticket d'une autre personne
    ("alice", None, False),            # aucune session
    ("", None, True),                  # console en mode ouvert : personne à comparer
])
def test_the_kvm_websocket_wants_the_user_of_the_ticket(env, ticket_user, ws_user, opens):
    tok = wapp._vnc_issue_ticket("kvm:node2", "", "web", user=ticket_user, kind="kvm-vnc")
    ws, t = attach_browser(tok, user=ws_user)
    if opens:
        assert wait_for(lambda: vnc_mux.sessions().get(KEY))
        ws.close(); t.join(timeout=5)
    else:
        t.join(timeout=5)
        assert ws.close_message == "invalid or expired ticket" and env["sim"]["popen"] == []
        # consommé quand même : il ne resservira pas
        assert wapp._vnc_take_ticket(tok, "kvm:node2", "", "web", kind="kvm-vnc") is None


@pytest.mark.parametrize("ticket_user,ws_user,refused", [
    ("alice", "bob", True), ("alice", None, True), ("alice", "alice", False), ("", None, False),
])
def test_the_kubevirt_websockets_want_the_user_of_the_ticket(env, monkeypatch, ticket_user, ws_user,
                                                             refused):
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": []})
    headers = basic(ws_user) if ws_user else {}
    for kind, view in (("vnc", "ws_vnc"), ("serial", "ws_serial")):
        tok = wapp._vnc_issue_ticket("harv1", "default", "web", user=ticket_user, kind=kind)
        ws = BrowserWs()
        with wapp.app.test_request_context(f"/ws/{kind}/harv1/default/web?ticket={tok}", headers=headers):
            wapp.app.view_functions[view].__wrapped__(ws, "harv1", "default", "web")
        # refusé : ticket ; admis : il va jusqu'au cluster (inconnu ici)
        assert ws.close_message == ("invalid or expired ticket" if refused else "unknown cluster"), kind


def test_the_access_log_hides_console_tickets(caplog):
    from werkzeug.serving import WSGIRequestHandler
    caplog.set_level(logging.INFO, logger="werkzeug")
    for path in ("/ws/kvm/vnc/node2/web?ticket=SeCrEt-Tk_1", "/ws/vnc/harv1/default/web?ticket=SeCrEt-Tk_2&x=1"):
        h = WSGIRequestHandler.__new__(WSGIRequestHandler)
        h.path, h.command, h.request_version = path, "GET", "HTTP/1.1"
        h.client_address, h.requestline = ("127.0.0.1", 4242), f"GET {path} HTTP/1.1"
        h.log_request(101)
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "werkzeug")
    assert "SeCrEt" not in text and caplog.text.count("ticket=***") == 2
    assert "/ws/kvm/vnc/node2/web?ticket=***" in text and "ticket=***&x=1" in text


def test_host_removal_does_not_wait_for_its_tunnels(env, client):
    """Fermer un tunnel laisse 2 s à ssh ; la requête de retrait n'attend pas."""
    closing = threading.Event()

    class SlowUp:
        connected = True

        def __init__(self):
            import queue
            self.inbox = queue.Queue()
            pf = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
            for d in (b"RFB 003.008\n", b"\x01\x01", b"\x00\x00\x00\x00",
                      struct.pack(">HH", 8, 8) + pf + struct.pack(">I", 1) + b"v"):
                self.inbox.put(d)

        def receive(self, timeout=None):
            import queue
            try:
                return self.inbox.get(timeout=min(timeout or 0.05, 0.05))
            except queue.Empty:
                return None

        def send(self, data):
            pass

        def close(self):
            closing.set()
            time.sleep(1.5)                # la grâce laissée à ssh
            self.connected = False
    hubs = [vnc_mux.get_hub(("kvm:node2", d), SlowUp) for d in ("a", "b")]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wapp, "_kvm_action", lambda host, label, cmd, cleanup=None:
                   (type("R", (), {"id": "x"})(), None))
        t0 = time.monotonic()
        r = client.delete("/api/kvm/hosts/node2")
        elapsed = time.monotonic() - t0
    assert r.status_code == 202
    assert elapsed < 1.0, f"the removal waited {elapsed:.1f}s for its tunnels"
    assert closing.wait(5)
    assert wait_for(lambda: all(h.closed for h in hubs) and all(not h.up.connected for h in hubs))
