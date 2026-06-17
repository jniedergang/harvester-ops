"""
harvester-ops — pytest fixtures shared by api/ and e2e/ tests.

The fixtures spin up the Flask app on a random ephemeral port against a
sandboxed test config (config.yaml.test) so tests are isolated from any
real cluster. The "harv-fake" cluster in the test config points to a fake
kubeconfig and unreachable nodes; this is fine for tests that only exercise
the HTTP surface and UI logic — no kubectl/ssh succeeds.

For tests that need real cluster data (smoke tests against a reachable
Harvester cluster), use the `--live` flag (skipped by default in CI).
"""

import json
import os
import socket
import subprocess
import time
from pathlib import Path

# v1.5.6: opt out of flask-limiter at the earliest possible moment so
# tests that import `app` directly (in-process test_client) don't
# instantiate the limiter at module load.
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
BIN_DIR = ROOT / "bin"
DOCS_DIR = ROOT / "docs"
FIXTURES = Path(__file__).parent / "fixtures"


def _free_port():
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run tests that require a reachable Harvester cluster.",
    )


@pytest.fixture(scope="session")
def test_config(tmp_path_factory):
    """Generate a minimal test config.yaml pointing to a fake kubeconfig."""
    tmp = tmp_path_factory.mktemp("harvester-ops-test")
    kc = tmp / "kubeconfig-fake.yaml"
    kc.write_text("""apiVersion: v1
clusters:
- cluster:
    server: https://127.0.0.1:9 # invalid, used to verify endpoint logic only
  name: fake
contexts:
- context:
    cluster: fake
    user: fake
  name: fake
current-context: fake
kind: Config
users:
- name: fake
  user:
    token: dummy
""")
    cfg = tmp / "config.yaml"
    cfg.write_text(f"""
settings:
  log_dir: {tmp}/logs
web:
  bind_host: 127.0.0.1
  bind_port: 0
clusters:
  - name: harv-fake
    description: Fake test cluster
    kubeconfig: {kc}
    ssh:
      user: tester
      port: 22
    nodes:
      - hostname: fake-cp1
        ip: 127.0.0.99
        role: control-plane
""")
    (tmp / "logs").mkdir()
    return {
        "root": tmp,
        "config": cfg,
        "logs": tmp / "logs",
        "kubeconfig": kc,
    }


@pytest.fixture(scope="session")
def flask_server(test_config):
    """Spin up the Flask app on an ephemeral port for the duration of the session."""
    port = _free_port()
    env = {
        **os.environ,
        "PATH": "/tmp:" + os.environ.get("PATH", ""),
        "HARVESTER_OPS_CONFIG": str(test_config["config"]),
        "HARVESTER_OPS_BIN": str(BIN_DIR),
        "HARVESTER_OPS_LOG_DIR": str(test_config["logs"]),
        "HARVESTER_OPS_DOCS": str(DOCS_DIR),
        "HARVESTER_OPS_VERSION": "test",
        "HARVESTER_OPS_BUNDLE_DIR": str(test_config["root"] / "bundles"),
        "HARVESTER_OPS_NOTES_DB": str(test_config["root"] / "notes.db"),
        "HARVESTER_OPS_ACTIONS_DB": str(test_config["root"] / "actions.db"),
        # Force no auth in tests — point to a path that won't exist
        "HARVESTER_OPS_HTPASSWD": str(test_config["root"] / "no-such-htpasswd"),
        # v1.5.6: disable flask-limiter in tests; the suite hits some
        # endpoints dozens of times in a row.
        "HARVESTER_OPS_DISABLE_RATELIMIT": "1",
        "NO_COLOR": "1",
    }
    # Override bind_port via env injection: we patch the app's port read
    # by passing FLASK_RUN_PORT, but the app uses config.yaml — let's edit it
    cfg = test_config["config"]
    cfg.write_text(cfg.read_text().replace("bind_port: 0", f"bind_port: {port}"))

    # v1.4.18: env was missing HARVESTER_OPS_LOG_LEVEL — left at INFO,
    # the structured logger now emits many lines, and the subprocess
    # stderr=PIPE has a ~64KB buffer with no reader → server hangs once
    # the buffer is full. We lower the level to WARNING (still surfaces
    # errors) AND start a drainer thread to be safe against future log
    # bursts. Both knobs together kill the freeze.
    env.setdefault("HARVESTER_OPS_LOG_LEVEL", "WARNING")
    proc = subprocess.Popen(
        ["python3", str(WEB_DIR / "app.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(WEB_DIR),
    )

    import threading as _t
    _server_stderr_buf = []
    def _drain_stderr():
        for line in iter(proc.stderr.readline, b""):
            _server_stderr_buf.append(line)
    _t.Thread(target=_drain_stderr, daemon=True, name="server-stderr").start()
    # Wait until the port responds (max 8s)
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 8
    import urllib.request
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=0.5) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        out, _err = proc.communicate()
        err_dump = b"".join(_server_stderr_buf).decode("utf-8", errors="replace")
        raise RuntimeError(f"Flask did not start.\nSTDOUT:\n{out.decode()}\nSTDERR:\n{err_dump}")

    yield {"base_url": base_url, "port": port, "proc": proc, "config": test_config}
    proc.terminate()
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def api(flask_server):
    """Tiny helper to GET/POST JSON against the test server."""
    import urllib.request
    import urllib.error

    base = flask_server["base_url"]

    def _req(method, path, json_body=None, expect_status=200):
        req = urllib.request.Request(
            f"{base}{path}",
            method=method,
            headers={"Content-Type": "application/json"},
        )
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
        try:
            with urllib.request.urlopen(req, data=body, timeout=10) as r:
                status = r.status
                payload = r.read().decode()
        except urllib.error.HTTPError as e:
            status = e.code
            payload = e.read().decode()
        if expect_status and status != expect_status:
            raise AssertionError(f"{method} {path}: expected {expect_status}, got {status}\n{payload}")
        try:
            return status, json.loads(payload) if payload else None
        except json.JSONDecodeError:
            return status, payload

    _req.base = base
    return _req


# ---------------------------------------------------------------------------
# Live (real cluster) fixture — only when --live is passed
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def live_config():
    """Use /tmp/harvester-ops-test/config.yaml pointing at a real cluster."""
    p = Path("/tmp/harvester-ops-test/config.yaml")
    if not p.exists():
        pytest.skip("live config /tmp/harvester-ops-test/config.yaml not found")
    return p


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        skip_live = pytest.mark.skip(reason="needs --live flag")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)
