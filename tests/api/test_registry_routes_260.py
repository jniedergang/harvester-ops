"""v2.6.0 : routes des registres déclarés (tâche 6) : lecture lecteur,
déclaration administrateur, rattachement et vérification opérateur ;
identifiants jamais renvoyés ni en argument de l'outil ; limites lisibles."""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import test_deploy_routes_240 as tr  # noqa: E402

env = tr.env
SECRET = "Pw-Never-Shown-77"


def body(**over):
    b = {"kind": "external", "endpoint": "harbor.client.example:443", "usage": ["images", "charts"],
         "mirrors": ["docker.io", "ghcr.io"]}
    b.update(over)
    return b


@pytest.mark.parametrize("path,method,role", [
    ("/api/registries", "GET", "viewer"),
    ("/api/registries/harbor", "GET", "viewer"),
    ("/api/registries/harbor", "PUT", "admin"),
    ("/api/registries/harbor", "DELETE", "admin"),
    ("/api/registries/harbor/attach/harvlab2", "POST", "operator"),
    ("/api/registries/harbor/check/harvlab2", "POST", "operator"),
    ("/api/registries/bootstrap/harvlab2", "POST", "admin"),
    ("/api/registries/bootstrap/harvlab2/amorcage", "DELETE", "admin"),
    ("/api/registries/bootstrap/attach/check", "POST", "admin"),
    ("/api/artefacts", "GET", "viewer"),
    ("/api/artefacts/upload", "POST", "admin"),
    ("/api/artefacts/socle/2026.11/accept", "POST", "admin"),
    ("/api/artefacts/socle/2026.11", "DELETE", "admin"),
    ("/api/artefacts/settings", "PUT", "admin"),
    ("/api/artefacts/socle/2026.11/publish", "POST", "operator"),
])
def test_roles_follow_point_195(path, method, role):
    assert wapp.required_role_for(path, method) == role


def test_limits_of_every_new_write_route_are_readable():
    from limits import parse_many
    src = (ROOT / "web" / "app.py").read_text()
    for fn in ("api_registry_put", "api_registry_delete", "api_registry_attach", "api_registry_check"):
        i = src.index(f"def {fn}(")
        m = re.findall(r'@_rate_limit\("([^"]+)"\)', src[i - 160:i])
        assert m, fn
        parse_many(m[-1])


def test_declare_read_update_and_forget(env):
    c = env["client"]
    r = c.put("/api/registries/harbor", json=body())
    assert r.status_code == 201 and r.get_json()["revision"] == 1
    assert r.get_json()["updated_by"] == "alice"
    assert c.put("/api/registries/harbor", json=body()).status_code == 409
    r = c.put("/api/registries/harbor", json=dict(body(mirrors=["quay.io"]), revision=1, clusters=["forged"]))
    assert r.status_code == 200 and r.get_json()["clusters"] == []          # jamais écrit par le formulaire
    assert [x["name"] for x in c.get("/api/registries").get_json()["registries"]] == ["harbor"]
    assert c.get("/api/registries/harbor").get_json()["mirrors"] == ["quay.io"]
    assert c.delete("/api/registries/harbor").status_code == 204
    assert c.get("/api/registries/harbor").status_code == 404
    assert c.delete("/api/registries/harbor").status_code == 404


@pytest.mark.parametrize("name,b,status,field", [
    ("harbor", body(endpoint="https://x"), 400, "endpoint"),
    ("harbor", body(colour=1), 400, "colour"),
    ("harbor", body(kind="bootstrap"), 409, None),
    ("Bad", body(), 400, None),                     # refusé par le contrôle commun des chemins
    ("r" * 31, body(), 400, "name"),
    ("bootstrap", body(), 400, "name"),
])
def test_declaration_refusals(env, name, b, status, field):
    r = env["client"].put(f"/api/registries/{name}", json=b)
    assert r.status_code == status, r.get_json()
    if field:
        assert field in r.get_json()["fields"]


def test_attach_starts_an_action_with_the_credentials_in_a_private_spec(env, monkeypatch):
    c = env["client"]
    c.put("/api/registries/harbor", json=body())
    seen = []

    class Run:
        id = "a1a1a1a1a1a1"
    monkeypatch.setattr(wapp, "_cli_action", lambda cluster, label, cmd, tool, spec=None, **kw:
                        seen.append((cluster, label, cmd, tool, spec)) or (Run(), None))
    r = c.post("/api/registries/harbor/attach/harvlab",
               json={"push": {"username": "push", "password": SECRET}})
    assert r.status_code == 202, r.get_json()
    res = r.get_json()
    assert res["action_id"] == "a1a1a1a1a1a1" and res["warnings"] == ["registry-pull-is-push"]
    assert SECRET not in json.dumps(res)
    cluster, label, cmd, tool, spec = seen[0]
    assert label == "registry-attach:harvlab/harbor" and tool == "harvester-registry"
    assert SECRET not in " ".join(cmd) and spec["push"]["password"] == SECRET
    assert cmd[2].startswith("--state-dir=") and cmd[3:6] == ["attach", "harbor", "--cluster"]


@pytest.mark.parametrize("name,cluster,b,status", [
    ("nope", "harvlab", {"push": {"username": "u", "password": "p"}}, 404),
    ("harbor", "other", {"push": {"username": "u", "password": "p"}}, 404),
    ("harbor", "harvlab", {"push": {"username": "u"}}, 400),
    ("harbor", "harvlab", None, 400),
])
def test_attach_refusals_start_nothing(env, monkeypatch, name, cluster, b, status):
    env["client"].put("/api/registries/harbor", json=body())
    monkeypatch.setattr(wapp, "_cli_action", lambda *a, **k: pytest.fail("no action expected"))
    r = env["client"].post(f"/api/registries/{name}/attach/{cluster}", json=b)
    assert r.status_code == status


def test_check_of_an_unknown_registry_is_a_blocking_finding(env):
    r = env["client"].post("/api/registries/nope/check/harvlab")
    assert r.status_code == 200 and r.get_json()["blocked"] is True
    assert [f["code"] for f in r.get_json()["findings"]] == ["registry-unknown"]


def test_check_runs_the_tool_read_only(env, monkeypatch):
    env["client"].put("/api/registries/harbor", json=body())
    got = []
    monkeypatch.setattr(wapp, "_capi_run_json", lambda cmd, spec=None, timeout=180:
                        got.append(cmd) or (({"findings": [], "blocked": False}, 0), None))
    r = env["client"].post("/api/registries/harbor/check/harvlab")
    assert r.get_json() == {"findings": [], "blocked": False}
    assert got[0][3:5] == ["check", "harbor"]


def test_an_operator_cannot_declare_but_can_attach(env, monkeypatch):
    monkeypatch.setattr(wapp, "current_role", lambda: "operator")
    monkeypatch.setattr(wapp, "open_mode", lambda: True)
    assert env["client"].put("/api/registries/harbor", json=body()).status_code == 403
    monkeypatch.setattr(wapp, "current_role", lambda: "viewer")
    assert env["client"].get("/api/registries").status_code == 200
    assert env["client"].post("/api/registries/harbor/attach/harvlab", json={}).status_code == 403
