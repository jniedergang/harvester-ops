"""2.8.0 (tâche 3) : kubeconfig OIDC des personnes, par la route et par
`harvester-capi oidc-kubeconfig`. Lisible par un opérateur qui administre
le cluster enfant ; aucun identifiant (point 230)."""

import base64
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as wapp  # noqa: E402
import capi_stack as cs  # noqa: E402
import child_oidc as co  # noqa: E402
from test_capi_artefact_routes_270 import calls, env  # noqa: E402,F401


def test_the_route_answers_the_tool_output_without_caching(env):
    (env.fake / "oidc-kubeconfig.json").write_text(json.dumps({"kubeconfig": "apiVersion: v1\n",
                                                               "issuer_url": "https://idp"}))
    r = env.client.get("/api/capi/harvlab2/oidc-kubeconfig/web/web")
    assert r.status_code == 200 and r.get_json()["issuer_url"] == "https://idp"
    assert "no-store" in r.headers["Cache-Control"]
    argv = calls(env)[-1]["argv"]
    assert argv[0] == "oidc-kubeconfig" and argv[argv.index("--name") + 1] == "web/web" and "--json" in argv


@pytest.mark.parametrize("case,status", [("viewer", 403), ("no-right", 403), ("bad-name", 400),
                                         ("not-configured", 409)])
def test_route_refusals(env, case, status):
    path = "/api/capi/harvlab2/oidc-kubeconfig/web/web"
    if case == "viewer":
        env.right["role"] = "viewer"
    if case == "no-right":
        env.right["answer"] = "no"
    if case == "bad-name":
        path = "/api/capi/harvlab2/oidc-kubeconfig/web/Web_"
    if case == "not-configured":
        (env.fake / "oidc-kubeconfig.json").write_text('{"code": "oidc-not-configured"}')
        (env.fake / "oidc-kubeconfig.rc").write_text("2")
    r = env.client.get(path)
    assert r.status_code == status
    if case in ("viewer", "no-right", "bad-name"):
        assert calls(env) == []


@pytest.fixture(scope="module")
def cli():
    s = importlib.util.spec_from_file_location("harvester_capi_oidc_r", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


class Mgmt:
    def __init__(self, cluster):
        self.cluster, self.kubeconfig = cluster, "/nonexistent"

    def get(self, kind, ns, name):
        return self.cluster if kind == cs.K_CLUSTER else None


CHILD_KC = json.dumps({"clusters": [{"name": "c", "cluster": {"server": "https://172.16.11.26:6443",
                                                              "certificate-authority-data": "Q0E="}}],
                       "users": [{"name": "admin", "user": {"client-key-data": base64.b64encode(b"KEY").decode()}}]})


@pytest.mark.parametrize("annotated,code", [(True, 0), (False, 2)])
def test_cli_writes_a_kubeconfig_without_the_admin_credential(cli, monkeypatch, capsys, annotated, code):
    cfg, _ = co.check({"issuer_url": "https://idp/dex", "audience": "hops-child"}, "v1.34.11")
    obj = {"metadata": {"annotations": {co.ANNOTATION: co.public(cfg)} if annotated else {}}}
    monkeypatch.setattr(cli, "kube_from", lambda a: (Mgmt(obj), "/x", None))
    monkeypatch.setattr(cli.ca, "read", lambda k, ns, c: CHILD_KC)
    assert cli.main(["oidc-kubeconfig", "--kubeconfig", "/x", "--name", "c/c", "--json"]) == code
    out = json.loads(capsys.readouterr().out)
    if code:
        assert out["code"] == "oidc-not-configured"
        return
    kc = json.loads(out["kubeconfig"])
    assert kc["clusters"][0]["cluster"]["server"] == "https://172.16.11.26:6443"
    assert "client-key-data" not in out["kubeconfig"] and base64.b64encode(b"KEY").decode() not in out["kubeconfig"]
