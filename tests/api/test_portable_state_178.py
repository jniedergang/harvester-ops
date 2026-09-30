"""v1.78.0 : clusters déclarés par la console dans son répertoire d'état.

Le service packagé monte /etc/harvester-ops en lecture seule : tout ce que
la console écrit d'elle-même (déclarations de clusters, clés, kubeconfigs)
vit dans <état>/clusters.d, <état>/ssh, <état>/kubeconfigs, avec des chemins
relatifs au répertoire d'état pour que la console se déplace par simple
copie. config.yaml l'emporte à nom égal ; un de ses clusters est en lecture
seule (409) quand la console ne peut pas l'écrire.
"""

import io
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import cluster_decl  # noqa: E402

NODE = {"hostname": "n1", "ip": "192.0.2.50", "role": "control-plane"}


def _decl(name, **extra):
    d = {"name": name, "description": "", "kubeconfig": f"kubeconfigs/{name}.yaml",
         "ssh": {"user": "rancher", "port": 22, "key": f"ssh/{name}_id"}, "nodes": [NODE]}
    d.update(extra)
    return d


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """config.yaml de l'opérateur et répertoire d'état jetables."""
    etc = tmp_path / "etc"
    etc.mkdir()
    cfg = etc / "config.yaml"
    cfg.write_text(yaml.safe_dump({"web": {"bind_port": 1}, "clusters": [
        {"name": "op1", "description": "operator", "kubeconfig": "/abs/op1.yaml",
         "ssh": {"user": "rancher", "port": 22, "key": ""}, "nodes": [NODE]}]}))
    state = tmp_path / "state"
    monkeypatch.setattr(wapp, "CONFIG_PATH", cfg)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setattr(wapp, "current_user", lambda: "root")
    wapp.app.config["TESTING"] = True
    return {"etc": etc, "cfg": cfg, "state": state, "client": wapp.app.test_client()}


def _put_decl(state, doc, fname=None):
    d = state / "clusters.d"
    d.mkdir(parents=True, exist_ok=True)
    (d / (fname or f"{doc['name']}.yaml")).write_text(
        doc if isinstance(doc, str) else yaml.safe_dump(doc))


# -- chargement --------------------------------------------------------------------

def test_load_config_appends_console_clusters_after_config_ones(env, caplog):
    state = env["state"]
    _put_decl(state, _decl("zeta"))
    _put_decl(state, _decl("alpha"))
    # même nom que config.yaml : l'opérateur l'emporte, avertissement
    _put_decl(state, _decl("op1", description="shadowed"))
    # fichiers invalides : ignorés, jamais fatals
    _put_decl(state, "name: [unclosed\n", fname="broken.yaml")
    _put_decl(state, _decl("other"), fname="mismatch.yaml")
    _put_decl(state, "- a list\n", fname="list.yaml")
    with caplog.at_level(logging.WARNING, logger="harvester-ops"):
        clusters = wapp.load_config()["clusters"]
    assert [c["name"] for c in clusters] == ["op1", "alpha", "zeta"]
    assert clusters[0]["description"] == "operator"
    text = caplog.text
    assert "op1.yaml ignored" in text and "declared in config.yaml" in text
    assert "mismatch.yaml ignored" in text and "list.yaml ignored" in text
    assert "broken.yaml" in text


def test_console_paths_are_resolved_and_follow_a_moved_state_dir(env, monkeypatch, tmp_path):
    state = env["state"]
    _put_decl(state, _decl("lab1"))
    (state / "kubeconfigs").mkdir()
    (state / "kubeconfigs" / "lab1.yaml").write_text("apiVersion: v1\n")
    got = next(c for c in wapp.load_config()["clusters"] if c["name"] == "lab1")
    assert got["kubeconfig"] == str(state / "kubeconfigs" / "lab1.yaml")
    assert got["ssh"]["key"] == str(state / "ssh" / "lab1_id")
    # la console déplacée : copie de l'état ailleurs, rien à réécrire
    moved = tmp_path / "elsewhere" / "harvops-state"
    shutil.copytree(state, moved)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(moved))
    got = next(c for c in wapp.load_config()["clusters"] if c["name"] == "lab1")
    assert got["kubeconfig"] == str(moved / "kubeconfigs" / "lab1.yaml")
    assert Path(got["kubeconfig"]).read_text() == "apiVersion: v1\n"
    assert got["ssh"]["key"] == str(moved / "ssh" / "lab1_id")


def test_absolute_paths_outside_the_state_dir_stay_absolute(tmp_path):
    state = tmp_path / "s"
    c = {"name": "x", "kubeconfig": "/etc/harvester-ops/kc.yaml",
         "ssh": {"key": str(state / "ssh" / "x_id")}}
    rel = cluster_decl.relativize(c, state)
    assert rel["kubeconfig"] == "/etc/harvester-ops/kc.yaml"
    assert rel["ssh"]["key"] == "ssh/x_id"
    assert cluster_decl.resolve(rel, state) == c
    # un préfixe commun n'est pas « sous » le répertoire
    assert cluster_decl.relativize({"kubeconfig": str(tmp_path / "s2" / "k")}, state)["kubeconfig"] \
        == str(tmp_path / "s2" / "k")
    assert cluster_decl.decl_path(state, "../evil") is None
    assert cluster_decl.decl_path(state, ".hidden") is None


# -- écritures ---------------------------------------------------------------------

def _create(client, name, **files):
    data = {"payload": json.dumps({"name": name, "description": "d", "nodes": [NODE]}),
            "kubeconfig": (io.BytesIO(b"apiVersion: v1\nclusters: []\n"), "kc.yaml")}
    if files.get("ssh"):
        data["ssh_key"] = (io.BytesIO(b"-----BEGIN OPENSSH PRIVATE KEY-----\nx\n"), "id")
    return client.post("/api/clusters", data=data, content_type="multipart/form-data")


def test_a_cluster_added_by_the_console_goes_to_the_state_dir(env):
    before = env["cfg"].read_text()
    r = _create(env["client"], "lab1", ssh=True)
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["cluster"]["origin"] == "console"
    # config.yaml intact, même modifiable (environnement de dev)
    assert env["cfg"].read_text() == before
    assert not (env["etc"] / "kubeconfigs").exists() and not (env["etc"] / "ssh").exists()
    state = env["state"]
    decl = state / "clusters.d" / "lab1.yaml"
    doc = yaml.safe_load(decl.read_text())
    assert doc["kubeconfig"] == "kubeconfigs/lab1.yaml" and doc["ssh"]["key"] == "ssh/lab1_id"
    for f in (decl, state / "kubeconfigs" / "lab1.yaml", state / "ssh" / "lab1_id"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    for d in ("clusters.d", "kubeconfigs", "ssh"):
        assert stat.S_IMODE((state / d).stat().st_mode) == 0o700, d
    # listé, avec son origine
    body = env["client"].get("/api/clusters").get_json()
    assert body["config_writable"] is True
    assert {c["name"]: c["origin"] for c in body["clusters"]} == {"op1": "config", "lab1": "console"}
    # un nom déjà pris, où qu'il soit déclaré
    assert _create(env["client"], "lab1").status_code == 409
    assert _create(env["client"], "op1").status_code == 409


def test_console_clusters_are_edited_renamed_and_deleted_in_the_state_dir(env):
    c = env["client"]
    state = env["state"]
    assert _create(c, "lab1", ssh=True).status_code == 201
    ssh = state / "ssh"
    for n in ("lab1_id.pub", "lab1_known_hosts"):
        (ssh / n).write_text("x")
    # la marque « clé créée par la console » est gardée tant que la clé est la même
    doc = yaml.safe_load((state / "clusters.d" / "lab1.yaml").read_text())
    doc["ssh"]["generated"] = True
    (state / "clusters.d" / "lab1.yaml").write_text(yaml.safe_dump(doc))
    before = env["cfg"].read_text()
    r = c.put("/api/clusters/lab1", json={"name": "lab2", "description": "e", "nodes": [NODE]})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["cluster"]["origin"] == "console"
    assert not (state / "clusters.d" / "lab1.yaml").exists()
    doc = yaml.safe_load((state / "clusters.d" / "lab2.yaml").read_text())
    assert doc["kubeconfig"] == "kubeconfigs/lab2.yaml" and doc["ssh"]["key"] == "ssh/lab2_id"
    assert doc["ssh"]["generated"] is True and doc["description"] == "e"
    assert (ssh / "lab2_id.pub").exists() and (ssh / "lab2_known_hosts").exists()
    # kubeconfig et clé remplacés dans l'état
    r = c.post("/api/clusters/lab2/kubeconfig", data={"file": (io.BytesIO(b"kind: Config\n"), "k")},
               content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["origin"] == "console"
    assert (state / "kubeconfigs" / "lab2.yaml").read_text() == "kind: Config\n"
    r = c.post("/api/clusters/lab2/sshkey",
               data={"file": (io.BytesIO(b"-----BEGIN OPENSSH PRIVATE KEY-----\ny\n"), "id")},
               content_type="multipart/form-data")
    assert r.status_code == 200
    doc = yaml.safe_load((state / "clusters.d" / "lab2.yaml").read_text())
    assert "generated" not in doc["ssh"] and doc["ssh"]["key"] == "ssh/lab2_id"
    assert stat.S_IMODE((ssh / "lab2_id").stat().st_mode) == 0o600
    # supprimé : déclaration et fichiers partent
    assert c.delete("/api/clusters/lab2").status_code == 200
    assert not list((state / "clusters.d").iterdir())
    assert not list(ssh.iterdir()) and not list((state / "kubeconfigs").iterdir())
    assert env["cfg"].read_text() == before


def test_config_clusters_are_written_in_config_yaml_when_it_is_writable(env):
    r = env["client"].put("/api/clusters/op1", json={"name": "op1", "description": "new",
                                                     "nodes": [NODE]})
    assert r.status_code == 200 and r.get_json()["cluster"]["origin"] == "config"
    cfg = yaml.safe_load(env["cfg"].read_text())
    assert cfg["clusters"][0]["description"] == "new"
    # config.yaml ne reçoit jamais les clusters de la console
    assert _create(env["client"], "lab1").status_code == 201
    env["client"].put("/api/clusters/op1", json={"name": "op1", "nodes": [NODE]})
    assert [c["name"] for c in yaml.safe_load(env["cfg"].read_text())["clusters"]] == ["op1"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only file anyway")
def test_config_clusters_are_read_only_when_config_yaml_is_not_writable(env):
    c = env["client"]
    assert _create(c, "lab1").status_code == 201
    before = env["cfg"].read_text()
    env["cfg"].chmod(0o444)
    env["etc"].chmod(0o555)
    try:
        assert wapp._config_writable() is False
        assert c.get("/api/clusters").get_json()["config_writable"] is False
        ro = "declared by the operator in config.yaml, read-only for the console"
        for r in (c.put("/api/clusters/op1", json={"name": "op1", "nodes": [NODE]}),
                  c.delete("/api/clusters/op1"),
                  c.post("/api/clusters/op1/kubeconfig",
                         data={"file": (io.BytesIO(b"kind: Config\n"), "k")},
                         content_type="multipart/form-data"),
                  c.post("/api/clusters/op1/sshkey",
                         data={"file": (io.BytesIO(b"-----BEGIN RSA PRIVATE KEY-----\n"), "k")},
                         content_type="multipart/form-data")):
            assert r.status_code == 409, r.get_json()
            assert r.get_json()["error"] == ro
        # un cluster de la console, lui, reste modifiable
        r = c.put("/api/clusters/lab1", json={"name": "lab1", "description": "ok", "nodes": [NODE]})
        assert r.status_code == 200
        assert _create(c, "lab2").status_code == 201
        assert c.delete("/api/clusters/lab2").status_code == 200
    finally:
        env["etc"].chmod(0o755)
        env["cfg"].chmod(0o644)
    assert env["cfg"].read_text() == before


def test_unknown_cluster_is_404_for_every_writer(env):
    c = env["client"]
    assert c.put("/api/clusters/nope", json={"name": "nope", "nodes": [NODE]}).status_code == 404
    assert c.delete("/api/clusters/nope").status_code == 404


# -- ligne de commande -------------------------------------------------------------

def _cli_env(env):
    return {**os.environ, "HARVESTER_OPS_CONFIG": str(env["cfg"]),
            "HARVESTER_OPS_STATE_DIR": str(env["state"]), "NO_COLOR": "1",
            "HARVESTER_OPS_LOG_DIR": str(env["state"] / "logs")}


def test_kube_cluster_config_finds_console_clusters(env):
    _put_decl(env["state"], _decl("lab1"))
    code = ("import sys, json; sys.path.insert(0, %r); import kube; "
            "print(json.dumps([kube.cluster_config('lab1'), kube.cluster_config('op1')['name']]))"
            % str(ROOT / "bin" / "lib"))
    out = subprocess.run([sys.executable, "-c", code], env=_cli_env(env), capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    lab, op = json.loads(out.stdout)
    assert lab["kubeconfig"] == str(env["state"] / "kubeconfigs" / "lab1.yaml") and op == "op1"


@pytest.mark.skipif(not shutil.which("yq"), reason="yq absent")
def test_bash_load_cluster_finds_console_clusters(env):
    _put_decl(env["state"], _decl("lab1", nodes=[NODE, {"hostname": "n2", "ip": "192.0.2.51",
                                                        "role": "worker"}]))
    script = f"""
source {ROOT / 'bin' / 'lib' / 'common.sh'}
load_cluster lab1 >/dev/null 2>&1 || exit 7
echo "$KUBECONFIG_PATH|$SSH_KEY|${{#CLUSTER_NODES[@]}}"
list_clusters
load_cluster nope >/dev/null 2>&1 && exit 8
load_cluster op1 >/dev/null 2>&1 || exit 9
echo "$KUBECONFIG_PATH"
"""
    out = subprocess.run(["bash", "-c", script], env=_cli_env(env), capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, (out.returncode, out.stderr)
    lines = out.stdout.split()
    st = env["state"]
    assert lines[0] == f"{st}/kubeconfigs/lab1.yaml|{st}/ssh/lab1_id|2"
    assert lines[1:3] == ["op1", "lab1"]
    assert lines[3] == "/abs/op1.yaml"


def test_the_console_hands_its_state_dir_to_the_scripts_it_runs():
    assert os.environ.get("HARVESTER_OPS_STATE_DIR")
    src = (ROOT / "web" / "app.py").read_text()
    assert 'os.environ.setdefault("HARVESTER_OPS_STATE_DIR", str(NOTES_DB.parent))' in src


def test_the_packaged_unit_names_the_state_dir():
    unit = (ROOT / "config" / "systemd" / "harvester-ops.service").read_text()
    assert "HARVESTER_OPS_STATE_DIR=/var/lib/harvester-ops" in unit
    assert "/var/lib/harvester-ops:/var/lib/harvester-ops:rw" in unit
