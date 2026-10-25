"""2.0.0, tâche 7 : ce que la page d'un hôte KVM demande au serveur.

- `GET /api/kvm/hosts/<h>/declaration` : la déclaration seule (aucun SSH),
  avec les empreintes épinglées calculées comme `ssh-keygen -l -E sha256`,
  jamais un chemin de clé ni d'empreinte ;
- le rôle dans la lecture d'un hôte (boutons grisés de la page) ;
- `PUT /api/kvm/hosts/<h>/labels` (décision 8) : administrateur, action suivie
  par `harvester-kvm.py host label`, refus sans rien lancer ;
- `host label` du script : remplace le jeu, `--clear`, refus ;
- `/api/replication/hosts/<kind>/<nom>/resources` (décision 9), l'ancienne
  forme toujours comprise pour Harvester.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kvm_routes_196 import (  # noqa: E402,F401 : fixtures partagées
    SRC, _NoThread, as_user, client, decl, env, kd, only_run, script_cmd, wapp)

ROOT = Path(__file__).resolve().parent.parent.parent
CLI = str(ROOT / "bin" / "harvester-kvm.py")


def base(env, *sub):
    return [sys.executable, CLI, *sub, "--json", f"--state-dir={env['state']}"]


# --- déclaration -------------------------------------------------------------------------

@pytest.fixture
def pinned(env, tmp_path):
    """Une vraie clé publique épinglée pour node2 et son empreinte attendue."""
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen unavailable")
    key = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    pub = (tmp_path / "k.pub").read_text().split()
    out = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(key) + ".pub"],
                         capture_output=True, text=True, check=True).stdout
    fp = re.search(r"SHA256:\S+", out).group(0)
    kd.write_known_hosts(env["state"], "node2", [f"172.16.1.12 {pub[0]} {pub[1]}"])
    return fp


def test_declaration_is_read_without_ssh_and_without_paths(client, env, pinned, monkeypatch):
    monkeypatch.setattr(wapp._kh, "host_inventory", lambda *a, **k: pytest.fail("no SSH read"))
    r = client.get("/api/kvm/hosts/node2/declaration")
    assert r.status_code == 200
    d = r.get_json()
    assert {k: d[k] for k in ("name", "address", "port", "user", "uri", "labels")} == {
        "name": "node2", "address": "172.16.1.12", "port": 22, "user": "ju",
        "uri": "qemu:///system", "labels": {"site": "cave"}}
    assert d["fingerprints"] == [{"type": "ssh-ed25519", "fingerprint": pinned}]
    assert d["role"] == "admin"
    text = r.get_data(as_text=True)
    assert str(env["state"]) not in text and "_known" not in text and "_id" not in text


@pytest.mark.parametrize("prepare,host,code", [
    (None, "absent", 404),
    (None, "-bad", 400),
    (None, "a b", 400),
    ("corrupt", "node2", 500),
])
def test_declaration_refusals(client, env, prepare, host, code):
    if prepare == "corrupt":
        kd.decl_path(env["state"], "node2").write_text('{"name": "other"}')
    r = client.get(f"/api/kvm/hosts/{host}/declaration")
    assert r.status_code == code
    assert str(env["state"]) not in r.get_data(as_text=True)


@pytest.mark.parametrize("content,want", [
    ("", []),
    ("# commentaire\n@revoked h ssh-ed25519 AAAA\n", []),
    ("h ssh-ed25519 !!!pas-du-base64\n", []),
    ("h ssh-ed25519\n", []),
])
def test_pinned_keys_skip_what_is_not_a_key_line(env, content, want):
    kd.ensure_ssh_dir(env["state"])
    (Path(env["state"]) / kd.known_hosts_rel("node2")).write_text(content)
    assert kd.pinned_keys(env["state"], "node2") == want


def test_pinned_keys_of_a_missing_file_is_empty(env):
    (Path(env["state"]) / kd.known_hosts_rel("node2")).unlink(missing_ok=True)
    assert kd.pinned_keys(env["state"], "node2") == []


@pytest.mark.parametrize("user", ["curieux", "alice", "patronne"])
def test_every_role_reads_the_declaration_and_sees_its_role(as_user, user):
    r = as_user(user, "GET", "/api/kvm/hosts/node2/declaration")
    assert r.status_code == 200
    assert r.get_json()["role"] == {"curieux": "viewer", "alice": "operator", "patronne": "admin"}[user]


@pytest.mark.parametrize("host,code", [("node2", 200), ("ghost", 502)])
def test_host_read_carries_the_role(client, host, code):
    r = client.get(f"/api/kvm/hosts/{host}")
    assert r.status_code == code and r.get_json()["role"] == "admin"


# --- étiquettes : route --------------------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    ({"labels": {"site": "cave", "rack": "b2"}}, ["--label=rack=b2", "--label=site=cave"]),
    ({"labels": {"a/b.c-d_e": "x=y"}}, ["--label=a/b.c-d_e=x=y"]),
    ({"labels": {}}, ["--clear"]),
])
def test_labels_route_runs_host_label(client, env, body, expected):
    r = client.put("/api/kvm/hosts/node2/labels", json=body)
    assert r.status_code == 202, r.get_json()
    assert script_cmd() == base(env, "host", "label") + ["--host=node2"] + expected
    run = only_run()
    assert run.cluster == "kvm:node2" and run.action == "kvm:host-label:node2"
    assert str(env["state"]) not in " ".join(run.cmd)


@pytest.mark.parametrize("body,msg", [
    ({}, "labels must map"),
    ({"labels": ["a=b"]}, "labels must map"),
    ({"labels": {"-a": "b"}}, "invalid label name"),
    ({"labels": {"a b": "c"}}, "invalid label name"),
    ({"labels": {"a": "b\nc"}}, "invalid value"),
    ({"labels": {"a": 1}}, "invalid value"),
    ({"labels": {"a": "x" * 254}}, "invalid value"),
    ({"labels": {f"k{i}": "v" for i in range(33)}}, "at most 32"),
])
def test_labels_refusals_start_nothing(client, env, body, msg):
    r = client.put("/api/kvm/hosts/node2/labels", json=body)
    assert r.status_code == 400 and msg in r.get_json()["error"]
    assert _NoThread.started == [] and not wapp.ACTIONS


@pytest.mark.parametrize("host,code", [("absent", 404), ("-x", 400)])
def test_labels_of_an_unknown_host(client, env, host, code):
    assert client.put(f"/api/kvm/hosts/{host}/labels", json={"labels": {}}).status_code == code
    assert _NoThread.started == []


@pytest.mark.parametrize("user,allowed", [("curieux", False), ("alice", False), ("patronne", True)])
def test_labels_need_the_admin_role(as_user, user, allowed):
    r = as_user(user, "PUT", "/api/kvm/hosts/node2/labels", json={"labels": {"site": "x"}})
    if allowed:
        assert r.status_code == 202, r.get_json()
    else:
        assert r.status_code == 403 and r.get_json()["required"] == "admin"
        assert _NoThread.started == []


def test_labels_route_is_authenticated_and_rate_limited():
    m = re.search(r'@app\.route\("/api/kvm/hosts/<host>/labels", methods=\["PUT"\]\)\n@requires_auth\n'
                  r'@_rate_limit\("([^"]+)"\)', SRC)
    assert m
    wapp._validate_rate_spec(m.group(1))
    assert wapp.required_role_for("/api/kvm/hosts/node2/labels", "PUT") == "admin"
    assert wapp.required_role_for("/api/kvm/hosts/node2/declaration", "GET") == "viewer"


# --- étiquettes : script ------------------------------------------------------------------

@pytest.fixture
def state(tmp_path):
    kd.write(tmp_path, dict(decl("node2"), image_pools=["images"]))
    return tmp_path


def cli(state, *a):
    r = subprocess.run([sys.executable, CLI, "host", "label", *a, "--json", f"--state-dir={state}"],
                       capture_output=True, text=True)
    return r.returncode, json.loads(r.stdout or "{}")


def test_cli_label_replaces_the_set_and_keeps_the_rest(state):
    rc, out = cli(state, "--host=node2", "--label=rack=b2", "--label=env=lab")
    assert rc == 0 and out["labels"] == {"rack": "b2", "env": "lab"} and out["previous"] == {"site": "cave"}
    d = kd.load(state, "node2")
    assert d["labels"] == {"rack": "b2", "env": "lab"}
    assert d["image_pools"] == ["images"] and d["address"] == "10.0.0.1"   # rien d'autre ne bouge


def test_cli_label_clear(state):
    rc, out = cli(state, "--host=node2", "--clear")
    assert rc == 0 and out["labels"] == {} and kd.load(state, "node2")["labels"] == {}


@pytest.mark.parametrize("args,msg", [
    (["--host=node2"], "--clear to remove"),
    (["--host=node2", "--clear", "--label=a=b"], "exclusive"),
    (["--host=node2", "--label=a=b", "--label=a=c"], "twice"),
    (["--host=node2", "--label=noequal"], "expected key=value"),
    (["--host=node2", "--label=-a=b"], "invalid label name"),
    (["--host=node2", "--label=a=b\tc"], "invalid value"),
    (["--host=absent", "--label=a=b"], "not declared"),
    (["--host=../x", "--label=a=b"], "invalid host name"),
])
def test_cli_label_refusals_write_nothing(state, args, msg):
    before = kd.decl_path(state, "node2").read_text()
    rc, out = cli(state, *args)
    assert rc == 2 and out["kind"] == "usage" and msg in out["error"], out
    assert kd.decl_path(state, "node2").read_text() == before


# --- DR : route d'hôte avec sa famille -----------------------------------------------------

@pytest.fixture
def repl(monkeypatch, tmp_path):
    seen = []

    def host_rows(conn, cluster, side, now, role=None):
        seen.append((cluster, side))
        return [{"resource_uid": "u-a", "name": "rep-a", "side": "target"}]

    class Conn:
        def close(self):
            pass
    monkeypatch.setattr(wapp._rdb, "host_rows", host_rows)
    monkeypatch.setattr(wapp, "_replication_conn", lambda: Conn())
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    return seen


@pytest.mark.parametrize("path,status,kind,rows,available", [
    ("/api/replication/hosts/harvester/harvlab2/resources?side=target", 200, "harvester", 1, True),
    ("/api/replication/hosts/harvlab2/resources?side=target", 200, "harvester", 1, True),   # forme d'avant
    ("/api/replication/hosts/kvm/node2/resources", 200, "kvm", 0, False),
    ("/api/replication/hosts/kvm/kvm-paris.01/resources?side=any", 200, "kvm", 0, False),
])
def test_host_dr_route_by_family(repl, path, status, kind, rows, available):
    r = wapp.app.test_client().get(path)
    assert r.status_code == status, r.get_json()
    d = r.get_json()
    assert d["host_kind"] == kind and len(d["resources"]) == rows and d["available"] is available
    assert (repl != []) is (kind == "harvester")          # KVM : aucune base lue


@pytest.mark.parametrize("path", [
    "/api/replication/hosts/xen/node2/resources",
    "/api/replication/hosts/kvm/-x/resources",
    "/api/replication/hosts/kvm/node2/resources?side=both",
    "/api/replication/hosts/harvester/bad%20host/resources",
])
def test_host_dr_route_refusals(repl, path):
    assert wapp.app.test_client().get(path).status_code == 400
    assert repl == []
