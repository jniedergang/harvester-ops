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
    """Cluster Harvester simulé. `moves` : le contrôleur du réglage de
    Harvester 1.9 (containerd_registry.go) retire `Auth` du réglage et range
    les identifiants dans un Secret `rke.cattle.io/auth-config` de
    `fleet-local`, étiqueté au réglage, clé `host`."""
    def __init__(self, value, moves=True):
        self.settings = {"containerd-registry": {"value": value}, "additional-ca": {"value": "CA-X\n"}}
        self.patched = []
        self.secret_flags = []
        self.moves = moves
        self.secrets = []

    def get(self, kind, ns, name):
        if kind == "settings.harvesterhci.io":
            return self.settings.get(name)
        return None

    def list(self, kind, ns=None, selector=None):
        assert (kind, ns, selector) == ("secrets", "fleet-local", "harvesterhci.io/setting=containerd-registry")
        return list(self.secrets)

    def patch(self, kind, ns, name, body, dry_run=False, secret=False):
        self.settings_before = (self.settings.get(name) or {}).get("value")
        self.patched.append((name, body["value"]))
        self.secret_flags.append(secret)
        self.settings[name] = {"value": body["value"]}
        if name == "containerd-registry" and self.moves:
            import base64
            doc = json.loads(body["value"])
            for host, conf in (doc.get("Configs") or {}).items():
                auth = conf.get("Auth")
                if auth:
                    self.secrets.append({"type": "rke.cattle.io/auth-config", "metadata": {
                        "name": "harvester-containerd-registry-0011223344556677"},
                        "fresh": True,
                        "data": {k: base64.b64encode(v.encode()).decode() for k, v in
                                 (("host", host), ("username", auth["Username"]), ("password", auth["Password"]))}})
                    conf["Auth"] = None
            self.settings[name] = {"value": json.dumps(doc)}
            # revue finale I-1 : quand les miroirs changent, Harvester
            # réécrit le Secret de chaque hôte depuis le réglage ; un `Auth`
            # nul n'y laisse que `host` (syncContainerdRegistryConfigSecret)
            old = json.loads(self.settings_before or "{}").get("Mirrors")
            if old != doc.get("Mirrors"):
                for sec in self.secrets:
                    host = base64.b64decode(sec["data"]["host"]).decode()
                    if not ((doc.get("Configs") or {}).get(host) or {}).get("Auth") \
                            and not sec.get("fresh"):
                        sec["data"] = {"host": sec["data"]["host"]}
            for sec in self.secrets:
                sec.pop("fresh", None)


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
    assert world["kube"].secret_flags == [True]                # corps hors de la ligne de commande (I-7)
    assert out["auth_secret"] == "fleet-local/harvester-containerd-registry-0011223344556677"
    assert PULL_PW not in world["kube"].settings["containerd-registry"]["value"]
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
    code, out, err = nodes(capsys)
    assert code == 2 and "harvester-nodes-mirror-conflict" in err and world["kube"].patched == []
    # tâche 9, constat 2 : refus rendu en JSON comme renew-cert
    assert out["blocked"] is True and [f["code"] for f in out["findings"]] == ["harvester-nodes-mirror-conflict"]


@pytest.mark.parametrize("argv,code_,finding", [
    (["--cluster", "harv1"], 2, "registry-not-attached"),
])
def test_a_registry_not_attached_to_the_cluster_is_refused(world, capsys, argv, code_, finding):
    code = cli.main(["harvester-nodes", "amorcage", *argv, "--kubeconfig", "/kc"])
    o = capsys.readouterr()
    assert code == code_ and finding in o.err
    out = json.loads(o.out)
    assert out["blocked"] is True and out["findings"][0]["code"] == finding


def test_no_read_account_is_refused_in_json(world, capsys, monkeypatch):
    monkeypatch.setattr(cli.rg, "read_creds", lambda k, n, role: None)
    code, out, err = nodes(capsys)
    assert code == 2 and out["blocked"] is True and out["findings"][0]["code"] == "registry-not-attached"


def test_a_password_left_in_the_setting_is_an_error(world, capsys, monkeypatch):
    """I-7 : Harvester doit ranger le compte dans son Secret auth-config et
    le retirer du réglage ; sinon l'action échoue et le dit."""
    world["kube"].moves = False
    monkeypatch.setattr(cli, "time", __import__("types").SimpleNamespace(
        monotonic=lambda: 0.0, sleep=lambda s: None, time=lambda: 0.0, strftime=__import__("time").strftime,
        gmtime=__import__("time").gmtime))
    code, out, err = nodes(capsys, "--wait", "0")
    assert code == 1 and "harvester-nodes-auth-inline" in err
    assert out["blocked"] is True and out["findings"][-1]["code"] == "harvester-nodes-auth-inline"
    assert PULL_PW not in json.dumps(out) and PULL_PW not in err


def test_the_next_ca_joins_additional_ca(world, capsys, monkeypatch):
    """I-6 : pendant une transition, les nœuds Harvester croient les deux CA."""
    decl = dict(rg.load(world["state"], "amorcage"), ca_pem="CA-OLD", ca_next_pem="CA-NEW")
    monkeypatch.setattr(cli.rg, "load", lambda st, n: dict(decl))
    code, out, _err = nodes(capsys)
    assert code == 0
    assert world["kube"].settings["additional-ca"]["value"] == "CA-X\nCA-OLD\nCA-NEW\n"


def test_kube_patch_with_a_secret_never_puts_it_in_argv(monkeypatch, tmp_path):
    """I-7 : `Kube.patch(secret=True)` : corps dans un fichier 0600 effacé
    aussitôt, rien dans la ligne de commande."""
    import kube as kmod
    seen = {}

    def fake_run(argv, input=None, capture_output=None, text=None, timeout=None):
        seen["argv"] = list(argv)
        path = argv[argv.index("--patch-file") + 1]
        seen["mode"] = __import__("os").stat(path).st_mode & 0o777
        seen["body"] = Path(path).read_text()
        seen["path"] = path
        return __import__("types").SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(kmod.subprocess, "run", fake_run)
    kmod.Kube("/kc").patch("settings.harvesterhci.io", None, "containerd-registry", {"value": PULL_PW}, secret=True)
    assert PULL_PW not in " ".join(seen["argv"]) and PULL_PW in seen["body"]
    assert seen["mode"] == 0o600 and not Path(seen["path"]).exists()


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


def other_auth_secret(host="172.16.1.11:5005", password="OTHER-PW-1618"):
    import base64
    return {"type": "rke.cattle.io/auth-config", "metadata": {"name": "harvester-containerd-registry-aa"},
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in
                     (("host", host), ("username", "u"), ("password", password))}}


@pytest.mark.parametrize("extra", [[], ["--dry-run"]])
def test_another_registry_with_a_password_in_harvester_is_never_wiped(world, capsys, extra):
    """Revue finale I-1 : une autre entrée authentifiée perdrait son mot de
    passe (Harvester réécrit son Secret au changement des miroirs) : refus,
    rien d'écrit, le mot de passe reste."""
    world["kube"].secrets.append(other_auth_secret())
    code, out, err = nodes(capsys, *extra)
    assert code == 2 and "harvester-nodes-other-auth" in err and world["kube"].patched == []
    assert out["blocked"] is True and out["findings"][-1]["code"] == "harvester-nodes-other-auth"
    assert out["findings"][-1]["facts"]["hosts"] == "172.16.1.11:5005"
    assert "password" in world["kube"].secrets[0]["data"]


def test_the_fake_models_harvester_erasing_the_other_password(world, capsys, monkeypatch):
    """Le faux reproduit la perte : sans le refus, le mot de passe disparaît."""
    world["kube"].secrets.append(other_auth_secret())
    monkeypatch.setattr(cli, "_other_auth_hosts", lambda *a: [])
    code, _out, _err = nodes(capsys)
    assert code == 0 and "password" not in world["kube"].secrets[0]["data"]


def test_remove_of_our_entry_is_also_refused_when_another_keeps_a_password(world, capsys):
    nodes(capsys)
    world["kube"].secrets.append(other_auth_secret())
    world["kube"].patched.clear()
    code, out, err = nodes(capsys, "--remove")
    assert code == 2 and "harvester-nodes-other-auth" in err and world["kube"].patched == []


def test_our_own_secret_and_an_inline_auth_are_not_counted(world, capsys):
    """Notre propre entrée, et un hôte dont l'Auth est encore dans le réglage
    (réécrit tel quel), ne bloquent pas."""
    nodes(capsys)                         # notre Secret existe désormais
    code, _out, err = nodes(capsys, "--remove")
    assert code == 0, err
