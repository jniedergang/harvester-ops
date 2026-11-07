"""2.7.0 (tâche 3) : `harvester-capi token` et `child_access.token`.

Le jeton n'apparaît que sur la sortie standard ({token, expires_at}) :
jamais dans une étape, sur stderr, ni dans un message d'erreur. Bornes
600 à 86 400 s (l'API du cluster enfant refuse sous 600 s et n'a pas de
maximum sur RKE2 : relevé sur rke2-banc, harvlab2, 07/11/2026).
"""

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_manifests as mf  # noqa: E402
import child_access as ca  # noqa: E402
import child_policies as cp  # noqa: E402
from kube import KubeError  # noqa: E402

TOKEN = "eyJhbGciOiJSUzI1NiJ9.secret-token-value.sig"
NS, CL = "rke2-banc", "rke2-banc"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_capi_tok", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Child:
    def __init__(self, sa=None, refuse=False):
        self.sa, self.refuse, self.requests = sa, refuse, []

    def get(self, kind, ns, name):
        return self.sa if (kind, ns, name) == ("serviceaccounts", "equipe-a", "ci") else None

    def run(self, *args, input=None, timeout=None):
        self.requests.append((args, json.loads(input)))
        if self.refuse:
            raise KubeError('serviceaccounts "ci" is forbidden')
        body = json.loads(input)
        return json.dumps({"kind": "TokenRequest", "spec": body["spec"],
                           "status": {"token": TOKEN, "expirationTimestamp": "2026-11-07T13:52:41Z"}})


def managed_sa():
    return {"metadata": {"name": "ci", "namespace": "equipe-a",
                         "labels": mf.marks(cp.ITEM, CL)}}


def run(cli, monkeypatch, child, *extra):
    monkeypatch.setattr(cli, "kube_from", lambda a: (object(), None, None))
    opened = []

    @contextlib.contextmanager
    def session(k, ns, c):
        opened.append((ns, c))
        yield "/nonexistent"
    monkeypatch.setattr(cli.ca, "session", session)
    monkeypatch.setattr(cli, "Kube", lambda path, **kw: child)
    return cli.main(["token", "--name", f"{NS}/{CL}", "--account", "equipe-a/ci", "--by", "alice", *extra]), opened


def test_the_token_goes_to_stdout_only(cli, monkeypatch, capsys):
    child = Child(managed_sa())
    rc, opened = run(cli, monkeypatch, child, "--seconds", "600")
    out = capsys.readouterr()
    assert rc == cli.EXIT_OK and opened == [(NS, CL)]
    assert json.loads(out.out) == {"token": TOKEN, "expires_at": "2026-11-07T13:52:41Z"}
    assert TOKEN not in out.err
    assert "STEP_EVENT|token|done|token issued for equipe-a/ci on rke2-banc, 600 s, by alice" in out.err
    args, body = child.requests[0]
    assert args[:3] == ("create", "--raw", "/api/v1/namespaces/equipe-a/serviceaccounts/ci/token")
    assert body["spec"] == {"expirationSeconds": 600} and TOKEN not in " ".join(args)


def test_the_default_duration_is_one_hour(cli, monkeypatch, capsys):
    child = Child(managed_sa())
    assert run(cli, monkeypatch, child)[0] == cli.EXIT_OK
    assert child.requests[0][1]["spec"]["expirationSeconds"] == 3600


@pytest.mark.parametrize("seconds", ["599", "86401", "0", "-5"])
def test_durations_out_of_bounds_are_refused_before_any_read(cli, monkeypatch, capsys, seconds):
    child = Child(managed_sa())
    rc, opened = run(cli, monkeypatch, child, "--seconds", seconds)
    assert rc == cli.EXIT_BLOCKED and opened == [] and child.requests == []


@pytest.mark.parametrize("seconds", [600, 86400])
def test_the_bounds_are_accepted(seconds):
    assert ca.check_seconds(seconds) == seconds


@pytest.mark.parametrize("sa,code", [
    (None, "account-not-found"),
    ({"metadata": {"name": "ci", "labels": {}}}, "account-not-managed"),
    ({"metadata": {"name": "ci", "labels": mf.marks(cp.ITEM, "autre-cluster")}}, "account-not-managed"),
    ({"metadata": {"name": "ci", "labels": mf.marks("autre-element", CL)}}, "account-not-managed"),
])
def test_only_accounts_of_the_console_get_a_token(cli, monkeypatch, capsys, sa, code):
    child = Child(sa)
    rc, _ = run(cli, monkeypatch, child)
    err = capsys.readouterr().err
    assert rc == cli.EXIT_BLOCKED and code in err and child.requests == []


def test_a_refused_request_carries_no_token(cli, monkeypatch, capsys):
    rc, _ = run(cli, monkeypatch, Child(managed_sa(), refuse=True))
    out = capsys.readouterr()
    assert rc == cli.EXIT_FAIL and out.out == "" and "refused" in out.err


def test_child_admin_required_when_the_secret_is_forbidden(cli, monkeypatch, capsys):
    class Mgmt:
        def get(self, kind, ns, name):
            raise KubeError('secrets "rke2-banc-kubeconfig" is forbidden: User "bob" cannot get resource')
    monkeypatch.setattr(cli, "kube_from", lambda a: (Mgmt(), None, None))
    rc = cli.main(["token", "--name", f"{NS}/{CL}", "--account", "equipe-a/ci"])
    assert rc == cli.EXIT_BLOCKED and "child-admin-required" in capsys.readouterr().err
