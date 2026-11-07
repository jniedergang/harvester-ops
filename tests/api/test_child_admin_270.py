"""2.7.0 (point 205) : le droit d'administrer un cluster enfant (lire le
Secret `<c>-kubeconfig`) est posé au serveur par `kubectl auth can-i` avec le
kubeconfig de la personne. Une réponse absente n'est jamais un oui."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402


@pytest.fixture
def calls(monkeypatch):
    class Seen(list):
        pass
    seen = Seen()

    def fake(answer):
        def run(argv, **kw):
            seen.append(argv)
            if isinstance(answer, Exception):
                raise answer
            return SimpleNamespace(stdout=answer[0], stderr=answer[1], returncode=answer[2])
        monkeypatch.setattr(wapp, "_kubectl_run", run)
    seen.fake = fake
    return seen


@pytest.mark.parametrize("answer,expected", [
    (("yes\n", "", 0), True),
    (("no\n", "", 1), False),
    (("no - RBAC\n", "", 1), False),
    (("", "error: You must be logged in /k/kc", 1), None),
    (subprocess.TimeoutExpired("kubectl", 15), None),
])
def test_answer(calls, answer, expected):
    calls.fake(answer)
    ok, why = wapp._child_admin_allowed("/k/kc", "web", "web")
    assert ok is expected
    if expected is None:
        assert "/k/kc" not in why
    assert calls[0][-4:] == ["get", "secrets/web-kubeconfig", "-n", "web"]
    assert calls[0][:3] == ["kubectl", "--kubeconfig", "/k/kc"]


@pytest.mark.parametrize("ns,child", [("Web", "web"), ("web", "a/b"), ("", "web"), ("web", None)])
def test_invalid_names_never_asked(calls, ns, child):
    calls.fake(("yes", "", 0))
    assert wapp._child_admin_allowed("/k/kc", ns, child) == (False, "invalid name")
    assert calls == []


@pytest.mark.parametrize("answer,status,code", [
    (("yes", "", 0), None, None),
    (("no", "", 1), 403, "child-admin-required"),
    (("", "boom", 1), 502, None),
])
def test_refusal(calls, answer, status, code):
    calls.fake(answer)
    with wapp.app.test_request_context():
        out = wapp._child_admin_refusal("/k/kc", "web", "web")
        if status is None:
            assert out is None
            return
        body, st = out
        assert st == status
        assert body.get_json().get("code") == code
