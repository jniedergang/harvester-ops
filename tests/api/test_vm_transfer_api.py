"""v1.45.0 : la console lance le transfert de VM, elle ne le refait pas.

Le contrôle et le transfert passent par `bin/harvester-vm-transfer.py` avec
les kubeconfigs préparés dans le thread de la requête (identité de
l'opérateur). Ce que la console décide elle-même : valider chaque option
avant qu'elle parte sur une ligne de commande, refuser un second transfert
de la même VM ou un transfert vers un cluster qu'on éteint, et tenir le
magasin d'exports sans jamais rendre un secret ni un chemin.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")
import app as wapp  # noqa: E402
import vm_transfer as vt  # noqa: E402


class _NoThread:
    started = []

    def __init__(self, target=None, args=(), **k):
        self.target, self.args = target, args

    def start(self):
        _NoThread.started.append(self.args)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [
        {"name": "harvlab", "kubeconfig": "/k/harvlab.yaml"},
        {"name": "harvlab2", "kubeconfig": "/k/harvlab2.yaml"}]})
    monkeypatch.setattr(wapp, "_kubectl_for_cluster",
                        lambda c: {"harvlab": "/staged/a.yaml",
                                   "harvlab2": "/staged/b.yaml"}.get(c))
    monkeypatch.setattr(wapp, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(wapp.threading, "Thread", _NoThread)
    _NoThread.started = []
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    yield wapp.app.test_client()
    wapp.ACTIONS.clear()
    wapp.ACTIONS.update(saved)


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _fake_check(monkeypatch, out, rc=0, stderr=""):
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return _Proc(json.dumps(out) if out is not None else "", stderr, rc)
    monkeypatch.setattr(wapp.subprocess, "run", run)
    return seen


CHECK = {"engine": "backup", "reason": "shared-target",
         "findings": [{"code": "engine", "level": "ok",
                       "facts": {"engine": "backup", "reason": "shared-target"}}],
         "mappings": {"networks": {}, "storage_classes": {}}}


def test_check_runs_the_script_with_staged_kubeconfigs(client, monkeypatch):
    seen = _fake_check(monkeypatch, CHECK)
    r = client.post("/api/vm/harvlab/default/xfer-test/transfer/check",
                    json={"to": "harvlab2", "name": "xfer-b", "mode": "short",
                          "networks": {"default/lab-net": "default/lab-net2"}})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["engine"] == "backup" and r.get_json()["blocked"] is False
    cmd = seen[0]
    assert cmd[1].endswith("harvester-vm-transfer.py") and cmd[2] == "check"
    assert cmd[cmd.index("--from-kubeconfig") + 1] == "/staged/a.yaml"
    assert cmd[cmd.index("--to-kubeconfig") + 1] == "/staged/b.yaml"
    assert cmd[cmd.index("--from") + 1] == "harvlab"
    assert cmd[cmd.index("--map-net") + 1] == "default/lab-net=default/lab-net2"
    assert cmd[cmd.index("--mode") + 1] == "short"


def test_check_reports_a_block(client, monkeypatch):
    _fake_check(monkeypatch, dict(CHECK, findings=[
        {"code": "vm-name-taken", "level": "block", "facts": {}}]), rc=2)
    r = client.post("/api/vm/harvlab/default/xfer-test/transfer/check", json={"to": "harvlab2"})
    assert r.status_code == 200 and r.get_json()["blocked"] is True


def test_check_failure_returns_the_script_error(client, monkeypatch):
    _fake_check(monkeypatch, None, rc=1,
                stderr="STEP_EVENT|check|running|reading\nSTEP_EVENT|check|error|VM default/x not found\n")
    r = client.post("/api/vm/harvlab/default/xfer-test/transfer/check", json={"to": "harvlab2"})
    assert r.status_code == 502 and "not found" in r.get_json()["error"]


def test_export_check_is_a_dry_run_into_the_store(client, monkeypatch):
    seen = _fake_check(monkeypatch, CHECK)
    r = client.post("/api/vm/harvlab/default/xfer-test/transfer/check", json={"source": "running"})
    assert r.status_code == 200
    cmd = seen[0]
    assert cmd[2] == "export" and "--dry-run" in cmd
    assert Path(cmd[cmd.index("--out") + 1]) == wapp.EXPORT_DIR


@pytest.mark.parametrize("cluster,body", [("nope", {"to": "harvlab2"}),
                                          ("harvlab", {"to": "nope"})])
def test_unknown_clusters(client, monkeypatch, cluster, body):
    _fake_check(monkeypatch, CHECK)
    r = client.post(f"/api/vm/{cluster}/default/xfer-test/transfer/check", json=body)
    assert r.status_code == 404


@pytest.mark.parametrize("body", [
    {"to": "harvlab2", "name": "Bad_Name"},
    {"to": "harvlab2", "mode": "fast"},
    {"to": "harvlab2", "networks": {"default/lab": "; rm -rf /"}},
    {"to": "harvlab2", "storage_classes": {"a": "b c"}},
    {"source": "deleted"},                       # un export ne supprime jamais
])
def test_every_option_is_validated(client, monkeypatch, body):
    seen = _fake_check(monkeypatch, CHECK)
    r = client.post("/api/vm/harvlab/default/xfer-test/transfer/check", json=body)
    assert r.status_code == 400 and seen == []


def test_launch_creates_an_action_without_paths_in_it(client):
    r = client.post("/api/vm/harvlab/default/xfer-test/transfer",
                    json={"to": "harvlab2", "source": "deleted", "target": "stopped",
                          "keep_mac": False, "keep_backups": True})
    assert r.status_code == 201
    rid = r.get_json()["action_id"]
    run = wapp.ACTIONS[rid]
    assert run.action == "vm-transfer:default/xfer-test" and run.cluster == "harvlab"
    # la commande affichée dans l'Activité ne porte aucun chemin de kubeconfig
    assert not any("/staged/" in a or "kubeconfig" in a for a in run.cmd)
    run_cmd = _NoThread.started[0][1]
    assert "--new-mac" in run_cmd and "--keep-backups" in run_cmd
    assert run_cmd[run_cmd.index("--source") + 1] == "deleted"
    assert run_cmd[-2:] == ["--id", rid[:8]]


def test_a_second_transfer_of_the_same_vm_is_refused(client):
    assert client.post("/api/vm/harvlab/default/xfer-test/transfer",
                       json={"to": "harvlab2"}).status_code == 201
    for rid in list(wapp.ACTIONS):
        wapp.ACTIONS[rid].status = "running"
    r = client.post("/api/vm/harvlab/default/xfer-test/transfer", json={"to": "harvlab2"})
    assert r.status_code == 409
    assert r.get_json()["running_action"] == "vm-transfer:default/xfer-test"
    # une autre VM passe
    assert client.post("/api/vm/harvlab/default/other/transfer",
                       json={"to": "harvlab2"}).status_code == 201


def test_no_transfer_to_a_cluster_being_shut_down(client):
    run = wapp.ActionRun("sd0000000001", "shutdown", "harvlab2", ["true"])
    run.status = "running"
    wapp.ACTIONS[run.id] = run
    r = client.post("/api/vm/harvlab/default/xfer-test/transfer", json={"to": "harvlab2"})
    assert r.status_code == 409 and r.get_json()["running_action"] == "shutdown"


def _archive(path, name="xfer-test", secret="c2VjcmV0"):
    w = vt.ArchiveWriter(path)
    w.add_json(vt.MANIFEST, {"format": 1, "created": "2026-09-25T00:00:00Z",
                             "source": {"cluster": "harvlab", "version": "v1.8.2",
                                        "namespace": "default", "name": name},
                             "vm": {"metadata": {"name": name}},
                             "inventory": {"disks": [{"volume": "disk-0", "size": 1024,
                                                      "storage_class": "harvester-longhorn",
                                                      "member": "disks/disk-0.raw.gz"}],
                                           "networks": ["default/lab-net"]},
                             "secrets": [{"name": "s", "data": {"userdata": secret}}]})
    w.add_stream("disks/disk-0.raw.gz", iter([b"x" * 100]))
    w.close()


def test_the_store_lists_archives_without_secrets(client):
    wapp._export_dir()
    _archive(wapp.EXPORT_DIR / "xfer-test-20260925.hvx")
    (wapp.EXPORT_DIR / "broken.hvx").write_bytes(b"not a tar")
    r = client.get("/api/exports")
    assert r.status_code == 200
    body = r.get_json()
    names = {e["name"]: e for e in body["exports"]}
    e = names["xfer-test-20260925.hvx"]
    assert e["complete"] is True and e["vm"] == "xfer-test" and e["cluster"] == "harvlab"
    assert e["disks"] == [{"volume": "disk-0", "size": 1024, "storage_class": "harvester-longhorn"}]
    assert names["broken.hvx"]["complete"] is False
    assert "c2VjcmV0" not in r.get_data(as_text=True)
    assert str(wapp.EXPORT_DIR) not in r.get_data(as_text=True)
    assert oct(wapp.EXPORT_DIR.stat().st_mode & 0o777) == "0o700"


def test_store_delete_and_download(client):
    wapp._export_dir()
    _archive(wapp.EXPORT_DIR / "a.hvx")
    r = client.get("/api/exports/a.hvx/download")
    assert r.status_code == 200 and r.headers["Content-Type"] == "application/x-tar"
    r.close()
    assert client.delete("/api/exports/a.hvx").status_code == 200
    assert not (wapp.EXPORT_DIR / "a.hvx").exists()
    assert client.delete("/api/exports/a.hvx").status_code == 404


@pytest.mark.parametrize("name", ["..hvx", "a.tar", ".hidden.hvx", "a%2F..%2Fb.hvx"])
def test_store_refuses_odd_names(client, name):
    r = client.delete(f"/api/exports/{name}")
    assert r.status_code in (400, 404)


def test_import_check_and_launch(client, monkeypatch):
    wapp._export_dir()
    _archive(wapp.EXPORT_DIR / "a.hvx")
    seen = _fake_check(monkeypatch, CHECK)
    r = client.post("/api/exports/a.hvx/check", json={"to": "harvlab2", "namespace": "lab",
                                                      "create_namespace": True})
    assert r.status_code == 200
    cmd = seen[0]
    assert cmd[2] == "import" and "--dry-run" in cmd and "--create-namespace" in cmd
    assert cmd[cmd.index("--to-kubeconfig") + 1] == "/staged/b.yaml"
    r = client.post("/api/exports/a.hvx/import", json={"to": "harvlab2"})
    assert r.status_code == 201
    run = wapp.ACTIONS[r.get_json()["action_id"]]
    assert run.action == "vm-import:a.hvx" and run.cluster == "harvlab2"
    assert not any("/staged/" in a or str(wapp.EXPORT_DIR) in a for a in run.cmd)
    assert client.post("/api/exports/a.hvx/import", json={"to": "nope"}).status_code == 404
    assert client.post("/api/exports/zz.hvx/import", json={"to": "harvlab2"}).status_code == 404


def test_the_runner_relays_steps_and_maps_exit_codes(monkeypatch):
    class P:
        def __init__(self, rc, lines):
            self.stderr = iter(lines)
            self._rc = rc

        def wait(self):
            return self._rc

        def poll(self):
            return self._rc

    for rc, status in ((0, "done"), (1, "error"), (2, "error"), (3, "cancelled")):
        run = wapp.ActionRun(f"r{rc}", "vm-transfer:default/x", "harvlab", ["x"])
        monkeypatch.setattr(wapp.subprocess, "Popen", lambda *a, **k: P(rc, [
            "STEP_EVENT|check|done|engine: backup\n",
            "noise\n",
            "STEP_EVENT|restore|error|restore failed\n" if rc == 1 else "\n"]))
        monkeypatch.setattr(run, "close", lambda: None)
        wapp._vm_transfer_runner(run, ["x"])
        assert run.status == status
        steps = [e for e in run.events if e.get("type") == "step"]
        assert steps[0]["step_id"] == "check"
        if rc == 1:
            assert run.error_summary == "restore failed"


def test_rate_limits_are_valid():
    from limits import parse_many
    src = (ROOT / "web" / "app.py").read_text()
    block = src.split("# Transfert de VM entre clusters, export et import (v1.45.0)", 1)[1]
    block = block.split("# VM snapshots (VirtualMachineBackup", 1)[0]
    import re
    specs = re.findall(r'@_rate_limit\("([^"]+)"\)', block)
    assert len(specs) >= 5
    for spec in specs:
        assert parse_many(spec)


def test_the_service_keeps_exports_on_the_persistent_volume():
    """Le conteneur a son système de fichiers en lecture seule et /tmp en
    mémoire : le magasin doit vivre dans /var/lib/harvester-ops, monté."""
    unit = (ROOT / "config" / "systemd" / "harvester-ops.service").read_text()
    assert "HARVESTER_OPS_EXPORT_DIR=/var/lib/harvester-ops/exports" in unit
    assert "-v /var/lib/harvester-ops:/var/lib/harvester-ops:rw" in unit
