"""2.8.0 (tâche 7, point 236) : `harvester-registry harvester-nodes` et les
routes `renew-cert` et `harvester-nodes` (administrateur, action suivie,
valeur précédente masquée dans le résultat)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import registries as rg  # noqa: E402

SPEC = importlib.util.spec_from_file_location("harvester_registry_nodes", ROOT / "bin" / "harvester-registry.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)
PULL_PW = "PULL-PW-NODES-1618"
CURRENT = {"Mirrors": {"quay.io": {"Endpoints": ["https://172.16.1.11:5005"], "Rewrites": None}},
           "Configs": {"172.16.1.11:5005": {"Auth": None, "TLS": {"CAFile": "", "InsecureSkipVerify": False}}},
           "Auths": None}


class Kube:
    def __init__(self, value):
        self.settings = {"containerd-registry": {"value": value}, "additional-ca": {"value": "CA-X\n"}}
        self.patched = []

    def get(self, kind, ns, name):
        if kind == "settings.harvesterhci.io":
            return self.settings.get(name)
        return None

    def patch(self, kind, ns, name, body, dry_run=False):
        self.patched.append((name, body["value"]))
        self.settings[name] = {"value": body["value"]}


@pytest.fixture
def world(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    rg.save(state, "amorcage", {"kind": "external", "endpoint": "172.16.2.76", "ca_pem": None, "usage": ["images"],
                                "mirrors": ["docker.io"], "clusters": ["harvlab2"]})
    kube = Kube(json.dumps(CURRENT))
    monkeypatch.setattr(cli, "kube_from", lambda args, need_entry=False: (kube, None))
    monkeypatch.setattr(cli.rg, "read_creds", lambda k, n, role: {"username": "pull", "password": PULL_PW})
    return {"kube": kube, "state": state}


def nodes(capsys, *extra):
    code = cli.main(["harvester-nodes", "amorcage", "--cluster", "harvlab2", "--kubeconfig", "/kc", *extra])
    o = capsys.readouterr()
    return code, json.loads(o.out) if o.out.strip().startswith("{") else {}, o.err


def test_merge_writes_the_setting_and_returns_the_previous_value_masked(world, capsys):
    code, out, err = nodes(capsys)
    assert code == 0, err
    [(name, value)] = world["kube"].patched
    assert name == "containerd-registry"
    got = json.loads(value)
    assert got["Mirrors"]["quay.io"] == CURRENT["Mirrors"]["quay.io"] and "docker.io" in got["Mirrors"]
    assert json.loads(out["previous"]) == CURRENT and PULL_PW not in json.dumps(out) and PULL_PW not in err
    assert out["findings"][0]["code"] == "harvester-rke2-restart" and "harvester-rke2-restart" in err


def test_remove_takes_only_this_registry(world, capsys):
    nodes(capsys)
    code, out, _err = nodes(capsys, "--remove")
    assert code == 0 and json.loads(world["kube"].settings["containerd-registry"]["value"]) == CURRENT


def test_dry_run_writes_nothing(world, capsys):
    code, out, _err = nodes(capsys, "--dry-run")
    assert code == 0 and out["changed"] is True and world["kube"].patched == []


def test_a_mirror_conflict_writes_nothing(world, capsys):
    world["kube"].settings["containerd-registry"]["value"] = json.dumps(
        {"Mirrors": {"docker.io": {"Endpoints": ["https://other"], "Rewrites": {"^(.*)$": "x/$1"}}}})
    code, _out, err = nodes(capsys)
    assert code == 2 and "harvester-nodes-mirror-conflict" in err and world["kube"].patched == []


def test_a_registry_not_attached_to_the_cluster_is_refused(world, capsys):
    code = cli.main(["harvester-nodes", "amorcage", "--cluster", "harv1", "--kubeconfig", "/kc"])
    assert code == 2 and "registry-not-attached" in capsys.readouterr().err


@pytest.mark.skipif(not __import__("shutil").which("openssl"), reason="openssl needed")
def test_the_ca_goes_to_additional_ca(world, capsys):
    import test_registry_seed_260 as tseed
    pem = tseed.real_ca(world["state"])
    rg.update(world["state"], "amorcage", lambda d: d.update(ca_pem=pem))
    code, out, _err = nodes(capsys)
    assert code == 0 and out["additional_ca_changed"] is True
    assert [n for n, _v in world["kube"].patched] == ["additional-ca", "containerd-registry"]
    assert world["kube"].settings["additional-ca"]["value"].startswith("CA-X\n-----BEGIN CERTIFICATE-----")


# --- routes ---------------------------------------------------------------------------

import app as wapp  # noqa: E402
from test_capi_artefact_routes_270 import env as aenv  # noqa: E402,F401


@pytest.mark.parametrize("path", ["/api/registries/amorcage/renew-cert", "/api/registries/amorcage/harvester-nodes"])
def test_routes_are_for_administrators(path):
    from limits import parse_many
    import re
    assert wapp.required_role_for(path, "POST") == "admin"
    src = (ROOT / "web" / "app.py").read_text()
    fn = "api_registry_renew_cert" if path.endswith("renew-cert") else "api_registry_harvester_nodes"
    i = src.index(f"def {fn}(")
    head = src[src.rindex("@app.route", 0, i):i]
    assert "@requires_auth" in head and parse_many(re.findall(r'@_rate_limit\("([^"]+)"\)', head)[0])


@pytest.fixture
def renv(aenv, monkeypatch, tmp_path):
    rg.save(aenv.state, "amorcage", {"kind": "external", "endpoint": "172.16.2.76", "usage": ["images"],
                                     "mirrors": ["docker.io"], "clusters": ["harvlab2"]})
    (aenv.fake / "harvester-registry.py").write_text(
        (aenv.fake / "harvester-capi.py").read_text().replace("sub = argv[0]", "sub = argv[1]"))
    monkeypatch.setattr(wapp, "BIN_DIR", aenv.fake)
    aenv.right["role"] = "admin"
    return aenv


def reg_calls(env):
    p = env.fake / "calls.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.is_file() else []


def test_harvester_nodes_route_tracks_the_gesture_and_keeps_the_output(renv):
    (renv.fake / "harvester-nodes.json").write_text(json.dumps({"previous": "masked", "changed": True}))
    r = renv.client.post("/api/registries/amorcage/harvester-nodes", json={"cluster": "harvlab2", "remove": True})
    assert r.status_code == 202, r.get_json()
    from test_capi_artefact_routes_270 import wait
    run = wait(r.get_json()["action_id"])
    assert run.action == "registry-harvester-nodes:harvlab2/amorcage" and run.required_role == "admin"
    assert run.result["output"] == {"previous": "masked", "changed": True}
    argv = reg_calls(renv)[-1]["argv"]
    assert argv[1:3] == ["harvester-nodes", "amorcage"] and "--remove" in argv


@pytest.mark.parametrize("body,status", [({"cluster": "harvlab2", "days": 0}, 400), ({"cluster": "Bad"}, 400),
                                         ({"cluster": "harvlab2", "force": 1}, 400), ({"cluster": "zz"}, 404)])
def test_renew_cert_route_refusals(renv, body, status):
    r = renv.client.post("/api/registries/amorcage/renew-cert", json=body)
    assert r.status_code == status and reg_calls(renv) == []


def test_renew_cert_route_dry_run_is_a_read(renv):
    (renv.fake / "renew-cert.json").write_text(json.dumps({"registry": "amorcage", "not_after": 1}))
    r = renv.client.post("/api/registries/amorcage/renew-cert", json={"cluster": "harvlab2", "dry_run": True,
                                                                      "new_ca": True})
    assert r.status_code == 200 and r.get_json()["not_after"] == 1
    argv = reg_calls(renv)[-1]["argv"]
    assert "--dry-run" in argv and "--new-ca" in argv and "--days" in argv
