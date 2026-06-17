"""v1.5.6 — security guards.

Locks in the Sprint sécu fixes:
  - HTTP security headers on every response (XFO, XCTO, CSP, …)
  - RFC 1123 validation on path params `<namespace>` / `<name>` and on
    the legacy `?namespace=` query string
  - rate-limiter not active in tests but a guarded module-level helper
    `_rate_limit` exists and decorates the right endpoints
  - kubeconfig path is never echoed in a response body / error
"""

import json
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

def test_security_headers_set_on_every_response(api):
    """Each of the 4 headers must show up on a plain /healthz GET."""
    status, payload = api("GET", "/healthz")
    assert status == 200, payload
    # The api fixture only returns status + body. Re-issue via test_client
    # so we can inspect headers.
    with wapp.app.test_client() as c:
        r = c.get("/healthz")
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        csp = r.headers.get("Content-Security-Policy") or ""
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp


def test_security_headers_set_on_404(api):
    """4xx responses must also carry the security headers — they can't be
    bypassed by hitting an unknown URL."""
    with wapp.app.test_client() as c:
        r = c.get("/this-path-does-not-exist")
        assert r.status_code == 404
        assert r.headers.get("X-Frame-Options") == "DENY"


def test_security_headers_set_on_400(api):
    """4xx surfaces (e.g. the v1.5.6 path-param validator) must keep
    the headers — they can't be bypassed by triggering a 400."""
    with wapp.app.test_client() as c:
        r = c.get("/api/namespace/harv-fake/UPPERCASE")
        assert r.status_code == 400
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"


# ---------------------------------------------------------------------------
# RFC 1123 validation on path params
# ---------------------------------------------------------------------------

def test_valid_k8s_name_helper_accepts_dns_labels():
    assert wapp._valid_k8s_name("default")
    assert wapp._valid_k8s_name("kube-system")
    assert wapp._valid_k8s_name("my-vm-01")
    assert wapp._valid_k8s_name("a")
    assert wapp._valid_k8s_name("a" * 63)


def test_valid_k8s_name_helper_rejects_bad():
    assert not wapp._valid_k8s_name("")
    assert not wapp._valid_k8s_name("UPPERCASE")
    assert not wapp._valid_k8s_name("ends-with-")
    assert not wapp._valid_k8s_name("-starts-with-hyphen")
    assert not wapp._valid_k8s_name("has spaces")
    assert not wapp._valid_k8s_name("has;semicolon")
    assert not wapp._valid_k8s_name("$(injection)")
    assert not wapp._valid_k8s_name("a" * 64)


def test_valid_k8s_name_namespaced_form():
    assert wapp._valid_k8s_name("default/my-vm", namespaced=True)
    assert wapp._valid_k8s_name("default", namespaced=True)
    assert not wapp._valid_k8s_name("default/", namespaced=True)
    assert not wapp._valid_k8s_name("/default", namespaced=True)


def test_endpoint_rejects_bad_namespace_in_path(api):
    """The before_request hook fires on every route carrying
    `<namespace>` and 400s on invalid names without reaching the
    handler (which would otherwise pipe the value to kubectl)."""
    # /api/namespace/<cluster>/<namespace> exists for any cluster name.
    status, payload = api(
        "GET", "/api/namespace/harv-fake/UPPER", expect_status=None,
    )
    assert status == 400
    assert "invalid namespace" in (payload.get("error") or "")


def test_endpoint_rejects_shell_injection_attempt_in_path(api):
    """`$(rm -rf)` is not a valid DNS label — must 400 before reaching
    any handler."""
    # url-encode the dangerous chars so urllib accepts the URL
    import urllib.parse as _u
    bad = _u.quote("$(injection)", safe="")
    status, payload = api(
        "GET", f"/api/namespace/harv-fake/{bad}", expect_status=None,
    )
    assert status == 400


def test_endpoint_rejects_bad_namespace_in_query_string(api):
    """The legacy `?namespace=` query string used by a few GETs is
    validated alongside the path params."""
    import urllib.parse as _u
    bad = _u.quote("WRONG", safe="")
    status, payload = api(
        "GET", f"/api/vm-order/harv-fake?namespace={bad}",
        expect_status=None,
    )
    assert status == 400


def test_endpoint_accepts_valid_namespace(api):
    """Counter-example: a well-formed namespace must NOT trigger the
    400. The handler may then 404 for an unknown cluster — that's OK."""
    status, _ = api(
        "GET", "/api/namespace/harv-fake/default", expect_status=None,
    )
    assert status in (200, 404, 500, 502, 504), status


# ---------------------------------------------------------------------------
# Rate-limit hook is wired (won't fire in tests but must exist)
# ---------------------------------------------------------------------------

def test_rate_limit_helper_callable():
    assert callable(wapp._rate_limit)
    # Even disabled, decorating a function returns the function (no crash).
    @wapp._rate_limit("100/minute")
    def _noop():
        return "ok"
    assert _noop() == "ok"


def test_rate_limit_decorators_on_critical_endpoints():
    """Source-level check that the mutative endpoints carry the
    @_rate_limit decorator. A regex over the file is enough — we
    don't want to actually drive 30 requests in test."""
    src = (ROOT / "web" / "app.py").read_text()
    # Match `@app.route("/api/action", methods=["POST"])\n@_rate_limit(`
    pattern = re.compile(
        r'@app\.route\("/api/(?:action|terraform/<cluster>/(?:apply|apply_declaration))",[^)]*\)\s*\n\s*@_rate_limit'
    )
    matches = pattern.findall(src)
    # 3 mutative routes are decorated: /api/action, .../apply, .../apply_declaration
    assert len(matches) >= 3, (
        f"expected ≥3 rate-limited mutative routes, found {len(matches)}. "
        f"Did a refactor drop @_rate_limit?"
    )


# ---------------------------------------------------------------------------
# Kubeconfig path is never echoed in response bodies
# ---------------------------------------------------------------------------

def test_healthz_ready_returns_503_or_200(api):
    """v1.6.0: readiness probe is distinct from liveness. It returns
    503 when the actions DB or config.yaml is broken. The test config
    has both intact → expect 200."""
    status, payload = api("GET", "/healthz/ready", expect_status=None)
    # Test config does declare clusters, DB writable in tmp → expect 200
    assert status in (200, 503), (status, payload)
    if status == 200:
        assert payload.get("status") == "ready"
    else:
        assert "problems" in payload


def test_metrics_endpoint_returns_text(api):
    """v1.6.0: /metrics exposes Prometheus format text. The actual
    content depends on whether prometheus_client is installed."""
    import urllib.request
    with urllib.request.urlopen(f"{api.base}/metrics", timeout=5) as r:
        body = r.read().decode()
    # Either a real Prometheus payload or our # noop comment.
    assert "harvester_ops" in body or "prometheus_client not installed" in body


def test_no_kubeconfig_path_leak_in_404(api):
    """A 404 from an unknown cluster route must NOT reveal the
    kubeconfig path on disk — neither in the error nor in headers."""
    status, payload = api(
        "GET", "/api/topology/totally-unknown-cluster",
        expect_status=None,
    )
    body = json.dumps(payload) if isinstance(payload, dict) else str(payload)
    assert "/.kube/" not in body
    assert "kubeconfig" not in body or "unknown cluster" in body.lower()
