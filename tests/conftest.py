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
# v1.54.0 : les tests qui importent `app` dans le processus ne doivent pas
# écrire dans les espaces Terraform ni les déclarations réels (audit D17).
import tempfile as _tempfile  # noqa: E402
_TF_TEST_DIR = _tempfile.mkdtemp(prefix="hops-tf-tests-")
os.environ.setdefault("HARVESTER_OPS_TF_WORKSPACES", _TF_TEST_DIR + "/terraform")
os.environ.setdefault("HARVESTER_OPS_TF_DB", _TF_TEST_DIR + "/tf-declarations.db")
# v1.57.0 : la console exige une connexion ; les tests qui n'éprouvent pas
# l'authentification tournent en mode ouvert, DEMANDÉ explicitement, et
# leurs comptes éventuels restent dans leur dossier.
os.environ.setdefault("HARVESTER_OPS_AUTH", "none")
os.environ.setdefault("HARVESTER_OPS_ACCOUNTS", _TF_TEST_DIR + "/accounts.json")

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
        # v1.47.0 : sans lui, le serveur de test lisait (et un dépôt aurait
        # écrit) le magasin d'exports réel de la console de dev
        "HARVESTER_OPS_EXPORT_DIR": str(test_config["root"] / "exports"),
        # v1.54.0 : les espaces Terraform et les déclarations du serveur de
        # test restent dans son dossier (ils atterrissaient dans le vrai
        # /tmp/harvester-ops-terraform, audit D17)
        "HARVESTER_OPS_TF_WORKSPACES": str(test_config["root"] / "terraform"),
        "HARVESTER_OPS_TF_DB": str(test_config["root"] / "tf-declarations.db"),
        # Force no auth in tests — point to a path that won't exist, and ask
        # for the open mode explicitly (v1.57.0 : sinon la console attend son
        # premier administrateur)
        "HARVESTER_OPS_HTPASSWD": str(test_config["root"] / "no-such-htpasswd"),
        "HARVESTER_OPS_AUTH": "none",
        "HARVESTER_OPS_ACCOUNTS": str(test_config["root"] / "accounts.json"),
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
    # Wait until the port responds.
    #
    # 8 s was too tight. Importing app.py pulls Flask, flask-sock,
    # flask-limiter, passlib and y_py; on an idle machine that is a couple of
    # seconds, but on a busy host it is not. Measured on node1 under load
    # average 27: 14 s for the import alone, which failed EVERY test with
    # "Flask did not start" — 117 errors that look like a catastrophic
    # regression and are only a loaded machine. The budget is now generous,
    # and the failure message says how long it actually waited.
    startup_budget = float(os.environ.get("HARVESTER_OPS_TEST_STARTUP", "60"))
    base_url = f"http://127.0.0.1:{port}"
    started = time.time()
    deadline = started + startup_budget
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
        raise RuntimeError(
            f"Flask did not start within {startup_budget:.0f}s "
            f"(waited {time.time() - started:.1f}s). On a loaded host the "
            f"import alone can take 15s; raise HARVESTER_OPS_TEST_STARTUP if "
            f"needed.\nSTDOUT:\n{out.decode()}\nSTDERR:\n{err_dump}")

    yield {"base_url": base_url, "port": port, "proc": proc, "config": test_config}
    proc.terminate()
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()


# Certains points d'entrée sondent en SSH ou en kubectl avec leurs propres
# délais ; sur un hôte chargé, 10 s ne suffisent pas et le test échoue sur un
# TimeoutError qui n'apprend rien sur le code.
_REQ_TIMEOUT = float(os.environ.get("HARVESTER_OPS_TEST_TIMEOUT", "30"))


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
            with urllib.request.urlopen(req, data=body, timeout=_REQ_TIMEOUT) as r:
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
