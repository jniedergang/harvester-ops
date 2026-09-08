"""v1.7.0 — in-browser VNC console: noVNC vendor, ticket flow, WS relay helpers.

The relay itself (browser <-ws-> Flask <-wss-> KubeVirt /vnc) cannot run
under test_client(), so the strategy mirrors the repo's conventions:
source-level assertions on the JS/vendor side, direct unit tests on the
backend helpers, in-process HTTP tests on the ticket endpoint. The full
protocol path is covered by the --live test at the bottom (RFB banner
through a real cluster).
"""

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"
JS = (WEB / "static" / "js" / "vm-console.js").read_text()
INDEX = (WEB / "templates" / "index.html").read_text()
NOVNC = WEB / "static" / "vendor" / "novnc"

sys.path.insert(0, str(WEB))
import app as wapp  # noqa: E402


# ---------------------------------------------------------------------------
# Source-level — frontend wiring
# ---------------------------------------------------------------------------

def test_placeholder_is_gone():
    assert "coming soon" not in JS.lower()
    assert "virtctl" not in JS  # the CLI workaround block is history


def test_novnc_lazy_import_and_teardown():
    assert "import('/static/vendor/novnc/core/rfb.js')" in JS
    assert "onClose" in JS and "disconnect()" in JS


def test_opener_returns_panel_api():
    """restoreAll() re-applies saved dims only if the opener returns the
    panel api — the old placeholder returned nothing (geometry was lost)."""
    assert re.search(r"\breturn panel;", JS)


def test_vendor_tree_shipped():
    assert (NOVNC / "core" / "rfb.js").exists()
    assert (NOVNC / "LICENSE.txt").exists()          # MPL-2.0 obligation
    assert (NOVNC / "vendor" / "pako").is_dir()      # tight/zlib decoders
    assert "export default class RFB" in (NOVNC / "core" / "rfb.js").read_text()


def test_script_load_order():
    fp = INDEX.find('src="/static/js/floating-panels.js"')
    vc = INDEX.find('src="/static/js/vm-console.js"')
    assert 0 < fp < vc, "floating-panels.js must load before vm-console.js"


def test_ticket_preflight_wired_in_js():
    assert "/console-ticket" in JS
    assert "/ws/vnc/" in JS


# ---------------------------------------------------------------------------
# Unit — URL building and kubeconfig parsing
# ---------------------------------------------------------------------------

def test_vnc_subresource_url():
    url = wapp._vnc_subresource_url("https://1.2.3.4:6443", "ns1", "vm1")
    assert url == ("wss://1.2.3.4:6443/apis/subresources.kubevirt.io/v1"
                   "/namespaces/ns1/virtualmachineinstances/vm1/vnc")


def test_kubeconfig_wss_token_auth(tmp_path):
    kc = tmp_path / "kc.yaml"
    kc.write_text("""apiVersion: v1
current-context: c1
contexts: [{name: c1, context: {cluster: cl1, user: u1}}]
clusters: [{name: cl1, cluster: {server: "https://10.0.0.1:6443", insecure-skip-tls-verify: true}}]
users: [{name: u1, user: {token: sekret}}]
""")
    server, sslctx, token = wapp._kubeconfig_wss(str(kc))
    assert server == "https://10.0.0.1:6443"
    assert token == "sekret"
    assert sslctx.check_hostname is False


def test_kubeconfig_wss_rejects_plain_http(tmp_path):
    kc = tmp_path / "kc.yaml"
    kc.write_text("""apiVersion: v1
current-context: c1
contexts: [{name: c1, context: {cluster: cl1, user: u1}}]
clusters: [{name: cl1, cluster: {server: "http://10.0.0.1:8080"}}]
users: [{name: u1, user: {token: t}}]
""")
    with pytest.raises(ValueError):
        wapp._kubeconfig_wss(str(kc))


def test_kubeconfig_wss_leaves_no_temp_files(tmp_path, monkeypatch):
    """Cert material is staged under a vnc-kc-* tmpdir and deleted before
    any network I/O — nothing may survive the call."""
    monkeypatch.setattr(wapp.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(wapp.tempfile, "mkdtemp",
                        lambda prefix: str((tmp_path / f"{prefix}x").mkdir()
                                           or (tmp_path / f"{prefix}x")))
    kc = tmp_path / "kc.yaml"
    kc.write_text("""apiVersion: v1
current-context: c1
contexts: [{name: c1, context: {cluster: cl1, user: u1}}]
clusters: [{name: cl1, cluster: {server: "https://10.0.0.1:6443", insecure-skip-tls-verify: true}}]
users: [{name: u1, user: {token: t}}]
""")
    wapp._kubeconfig_wss(str(kc))
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("vnc-kc-")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Unit — ticket registry
# ---------------------------------------------------------------------------

def test_ticket_single_use():
    t = wapp._vnc_issue_ticket("c1", "ns", "vm")
    assert wapp._vnc_consume_ticket(t, "c1", "ns", "vm") is True
    assert wapp._vnc_consume_ticket(t, "c1", "ns", "vm") is False  # burned


def test_ticket_must_match_vm():
    t = wapp._vnc_issue_ticket("c1", "ns", "vm")
    assert wapp._vnc_consume_ticket(t, "c1", "ns", "OTHER") is False


def test_ticket_expiry(monkeypatch):
    t = wapp._vnc_issue_ticket("c1", "ns", "vm")
    real_time = time.time
    monkeypatch.setattr(wapp.time, "time",
                        lambda: real_time() + wapp._VNC_TICKET_TTL + 1)
    assert wapp._vnc_consume_ticket(t, "c1", "ns", "vm") is False


def test_ticket_none_or_garbage():
    assert wapp._vnc_consume_ticket(None, "c", "n", "v") is False
    assert wapp._vnc_consume_ticket("nope", "c", "n", "v") is False


# ---------------------------------------------------------------------------
# HTTP — ticket endpoint pre-flight semantics
# ---------------------------------------------------------------------------

class _FakeRun:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def client():
    wapp.app.config["TESTING"] = True
    return wapp.app.test_client()


def test_ticket_unknown_cluster(client, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: None)
    r = client.post("/api/vm/nope/ns1/vm1/console-ticket")
    assert r.status_code == 404


def test_ticket_vmi_absent_is_readable_409(client, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp.subprocess, "run",
                        lambda *a, **kw: _FakeRun(1, stderr='Error from server (NotFound): vmi "vm1" not found\n'))
    r = client.post("/api/vm/c1/ns1/vm1/console-ticket")
    assert r.status_code == 409
    assert "NotFound" in r.get_json()["error"]


def test_ticket_running_vmi_issues_ticket(client, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp.subprocess, "run",
                        lambda *a, **kw: _FakeRun(0, stdout="Running"))
    r = client.post("/api/vm/c1/ns1/vm1/console-ticket")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ws_path"] == "/ws/vnc/c1/ns1/vm1"
    assert wapp._vnc_consume_ticket(body["ticket"], "c1", "ns1", "vm1") is True


def test_ticket_session_cap(client, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp, "_vnc_sessions", wapp._VNC_MAX_SESSIONS)
    r = client.post("/api/vm/c1/ns1/vm1/console-ticket")
    assert r.status_code == 429


def test_ticket_invalid_name_rejected_by_hook(client):
    r = client.post("/api/vm/c1/ns1/UPPER_case/console-ticket")
    assert r.status_code == 400   # RFC 1123 before_request hook


# ---------------------------------------------------------------------------
# Live — full upstream leg against a real cluster (pytest --live)
# ---------------------------------------------------------------------------

def test_live_rfb_banner_through_upstream_leg(live_config):
    """Dial the KubeVirt /vnc subresource exactly like the relay does and
    read the RFB banner. Requires --live and a Running VMI named in the
    live config's first cluster (best-effort: picks the first Running VMI)."""
    import yaml as _yaml
    cfg = _yaml.safe_load(Path(live_config).read_text())
    kc = cfg["clusters"][0]["kubeconfig"]
    r = subprocess.run(
        ["kubectl", "--kubeconfig", kc, "get", "vmi", "--all-namespaces",
         "--no-headers"], capture_output=True, text=True, timeout=15)
    lines = [l.split() for l in r.stdout.splitlines() if " Running " in f" {l} "]
    if not lines:
        pytest.skip("no Running VMI on the live cluster")
    ns, name = lines[0][0], lines[0][1]
    server, sslctx, _tok = wapp._kubeconfig_wss(kc)
    from simple_websocket import Client
    c = Client.connect(wapp._vnc_subresource_url(server, ns, name),
                       ssl_context=sslctx,
                       subprotocols=wapp._VNC_SUBPROTOCOLS, ping_interval=20)
    try:
        banner = c.receive(timeout=10)
        assert banner and banner.startswith(b"RFB ")
    finally:
        c.close()


# ---------------------------------------------------------------------------
# v1.7.1 — console toolbar power actions + hard reset endpoint
# ---------------------------------------------------------------------------

def test_toolbar_buttons_present():
    for cls in ("vm-console-power-start", "vm-console-power-stop",
                "vm-console-power-reset", "vm-console-snapshots",
                "vm-console-settings"):
        assert cls in JS, f"toolbar button {cls} missing"
    # destructive power actions must ask first
    assert JS.count("confirm(") >= 2
    # panels reuse the existing overlays, not bespoke ones
    assert "VMSnapshots.open(cluster, namespace, name)" in JS
    assert "VMEdit.open(cluster, namespace, name)" in JS


def test_two_phase_retry():
    """Fast reattach right after a disconnect is what makes the firmware
    splash of a hard reset catchable."""
    assert "RETRY_FAST_MS" in JS and "RETRY_FAST_COUNT" in JS
    assert re.search(r"retries <= RETRY_FAST_COUNT \? RETRY_FAST_MS : RETRY_MS", JS)


def test_restart_unknown_cluster(client, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: None)
    r = client.post("/api/vm/nope/ns1/vm1/restart")
    assert r.status_code == 404


def test_restart_returns_tracked_action(client, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp.threading, "Thread",
                        lambda *a, **kw: type("T", (), {"start": lambda s: None})())
    r = client.post("/api/vm/c1/ns1/vm1/restart")
    assert r.status_code == 200
    aid = r.get_json()["action_id"]
    with wapp.ACTIONS_LOCK:
        run = wapp.ACTIONS.pop(aid)
    assert run.action == "vm-restart:ns1/vm1"


def test_restart_runner_surfaces_delete_error(monkeypatch):
    class _FR:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        if "delete" in cmd:
            return _FR(1, err='Error from server (NotFound): vmi "x" not found\n')
        return _FR(0, out="uid-1")
    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    run = wapp.ActionRun("r" * 12, "vm-restart:ns/x", "c1", [])
    wapp._vm_restart_runner(run, "/dev/null", "ns", "x")
    assert run.status == "error"
    assert "NotFound" in run.error_summary


def test_restart_runner_waits_for_new_uid(monkeypatch):
    class _FR:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    seq = {"polls": 0}

    def fake_run(cmd, **kw):
        if "delete" in cmd:
            return _FR(0)
        if "{.metadata.uid}" in " ".join(cmd) and "{.status.phase}" not in " ".join(cmd):
            return _FR(0, out="uid-old")
        seq["polls"] += 1
        # old uid still Running once, then the respawned VMI
        if seq["polls"] == 1:
            return _FR(0, out="uid-old Running")
        return _FR(0, out="uid-new Running")
    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    monkeypatch.setattr(wapp.time, "sleep", lambda s: None)
    run = wapp.ActionRun("s" * 12, "vm-restart:ns/x", "c1", [])
    wapp._vm_restart_runner(run, "/dev/null", "ns", "x")
    assert run.status == "done"
    steps = [e for e in run.events if e.get("type") == "step"
             and e.get("step_id") == "respawn" and e.get("status") == "done"]
    assert steps, "runner must confirm the NEW VMI reached Running"
