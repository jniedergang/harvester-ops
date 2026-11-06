"""v2.6.0 : `harvester-registry deploy`, `status`, `delete` avec un cluster
simulé : clés et mots de passe jamais en argument, ni dans les étapes ni
sur stdout ; fichiers de travail effacés après succès et après échec ;
volume gardé sans --delete-data."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import registries as rg  # noqa: E402
import test_registry_seed_260 as tseed  # noqa: E402

pytestmark = pytest.mark.skipif(not __import__("shutil").which("openssl"), reason="openssl needed")

SPEC = importlib.util.spec_from_file_location("harvester_registry_cli", ROOT / "bin" / "harvester-registry.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class FakeKube:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.applied, self.deleted = [], []

    def list(self, kind, ns=None, selector=None):
        if kind == "storageclasses":
            return [{"metadata": {"name": "harv-rep1"}, "provisioner": "driver.longhorn.io",
                     "parameters": {"numberOfReplicas": "1"}}]
        if kind == "nodes.longhorn.io":
            return tseed.LH["nodes"]
        return []

    def get(self, kind, ns, name):
        return self.objects.get((kind, name))

    def apply(self, docs):
        self.applied.extend(docs)
        for d in docs:
            self.objects[(d["kind"].lower() + "s", d["metadata"]["name"])] = d

    def delete(self, kind, ns, name, cascade=None):
        self.deleted.append((kind, name))
        self.objects.pop((kind, name), None)

    def server_host(self):
        return "172.16.2.70"


@pytest.fixture
def world(tmp_path, monkeypatch):
    state, work = tmp_path / "state", tmp_path / "work"
    state.mkdir()
    work.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", str(work))
    kube = FakeKube()
    calls = []

    def runner(argv, input=None, **kw):                  # htpasswd simulé, openssl réel ; tout est noté
        calls.append((argv, input))
        if argv[0].endswith("htpasswd"):
            return subprocess.CompletedProcess(argv, 0, f"{argv[-1]}:$2y$05$fakehash\n", "")
        return subprocess.run(argv, input=input, capture_output=True, text=True, timeout=60)
    monkeypatch.setattr(cli.rs, "_run", lambda r, argv, **kw: runner(argv, **kw).stdout)
    monkeypatch.setattr(cli.rs, "bundle", lambda s: {"tar": "/b/registry-images.tar",
                                                     "images": {"registry": "r.io/suse/registry@sha256:" + "d" * 64}})
    monkeypatch.setattr(cli.rs, "tools_missing", lambda: [])
    imports = []
    import node_images
    monkeypatch.setattr(node_images, "import_on_nodes", lambda entry, files, step, sid="images": imports.append(files))
    monkeypatch.setattr(cli, "kube_from", lambda args, need_entry=False: (kube, {"nodes": [{"ip": "10.0.0.1"}]}))
    ready = {"why": None}
    monkeypatch.setattr(cli, "wait_ready", lambda decl, pull, timeout: ready["why"])
    return {"state": state, "work": work, "kube": kube, "calls": calls, "imports": imports, "ready": ready}


def deploy(capsys, *extra):
    code = cli.main(["deploy", "amorcage", "--cluster", "harvlab2", "--address", "172.16.2.76",
                     "--storage-class", "harv-rep1", *extra])
    o = capsys.readouterr()
    return code, o.out, o.err


def secrets_of(kube):
    out = []
    for d in kube.applied:
        if d["kind"] == "Secret" and d["metadata"]["name"] in ("amorcage-push", "amorcage-pull"):
            out.append(d["stringData"]["password"])
    return out


def test_deploy_poses_the_registry_and_its_declaration(world, capsys):
    code, out, err = deploy(capsys, "--mirrors", "ghcr.io,quay.io", "--dns-name", "harvlab2-registry.home.lo")
    assert code == 0, err
    kinds = [(d["kind"], d["metadata"]["name"]) for d in world["kube"].applied]
    assert ("Deployment", "amorcage") in kinds and ("Secret", "amorcage-push") in kinds
    assert ("Secret", "amorcage-pull") in kinds and ("Secret", "amorcage-ca") in kinds
    assert world["imports"] == [["/b/registry-images.tar"]]
    d = rg.load(world["state"], "amorcage")
    assert d["kind"] == "bootstrap" and d["endpoint"] == "172.16.2.76" and d["push_endpoint"] == "172.16.2.76:5443"
    assert d["mirrors"] == ["ghcr.io", "quay.io"] and d["clusters"] == ["harvlab2"] and d["dns_name"]
    pw = secrets_of(world["kube"])
    assert len(pw) == 2 and len(set(pw)) == 2 and all(len(p) >= 40 for p in pw)
    for p in pw:
        assert p not in out and p not in err and p not in json.dumps(d)
        for argv, stdin in world["calls"]:
            assert all(p not in a for a in argv)
    assert [stdin for argv, stdin in world["calls"] if argv[0].endswith("htpasswd")] == [f"{pw[0]}\n", f"{pw[1]}\n"]
    keys = [d for d in world["kube"].applied if d["kind"] == "Secret" and d["metadata"]["name"] == "amorcage-tls"]
    assert keys[0]["stringData"]["tls.key"] not in err + out
    assert list(world["work"].iterdir()) == []                         # clés effacées
    assert json.loads(out)["created"] is True


def test_a_failed_deploy_still_removes_its_work_files(world, capsys):
    world["ready"]["why"] = "the read account is refused"
    code, out, err = deploy(capsys)
    assert code == 1 and "not ready" in err
    assert list(world["work"].iterdir()) == []
    assert rg.load(world["state"], "amorcage") is None


def test_a_blocking_finding_writes_nothing(world, capsys):
    code, out, err = deploy(capsys, "--size-gib", "2000")
    assert code == 2 and [f["code"] for f in json.loads(out)["findings"]] == ["registry-room-short"]
    assert world["kube"].applied == [] and world["calls"] == [] and world["imports"] == []


def test_dry_run_only_checks(world, capsys):
    code, out, err = deploy(capsys, "--dry-run")
    assert code == 0 and json.loads(out)["blocked"] is False and world["kube"].applied == []


def test_a_client_certificate_comes_from_a_private_spec(world, capsys, tmp_path):
    spec = tmp_path / "s.json"
    ca = tseed.real_ca(tmp_path)
    world["calls"].clear()
    spec.write_text(json.dumps({"cert": "C", "key": "Client-Private-Key", "ca": ca}))
    code, out, err = deploy(capsys, "--spec", str(spec))
    assert code == 0, err
    assert not [c for c in world["calls"] if c[0][0] == "openssl"]
    tls = next(d for d in world["kube"].applied if d["metadata"]["name"] == "amorcage-tls")
    assert tls["stringData"] == {"tls.crt": "C", "tls.key": "Client-Private-Key", "ca.crt": ca}
    assert "Client-Private-Key" not in out + err


@pytest.mark.parametrize("delete_data,pvc_deleted", [(False, False), (True, True)])
def test_delete_keeps_the_volume_unless_asked(world, capsys, delete_data, pvc_deleted):
    assert deploy(capsys)[0] == 0
    code = cli.main(["delete", "amorcage", "--cluster", "harvlab2"] + (["--delete-data"] if delete_data else []))
    o = capsys.readouterr()
    assert code == 0, o.err
    deleted = [n for _, n in world["kube"].deleted]
    assert ("amorcage-data" in deleted) is pvc_deleted
    assert {"amorcage", "amorcage-push", "amorcage-pull", "amorcage-tls"} <= set(deleted)
    assert rg.load(world["state"], "amorcage") is None
    assert json.loads(o.out)["data_kept"] is (not delete_data)


def test_delete_refuses_a_client_registry(world, capsys):
    rg.save(world["state"], "harbor", {"kind": "external", "endpoint": "h.example"})
    assert cli.main(["delete", "harbor", "--cluster", "harvlab2"]) == 2
    assert world["kube"].deleted == []


def test_status_reads_without_writing(world, capsys, monkeypatch):
    assert deploy(capsys)[0] == 0
    world["kube"].objects[("persistentvolumeclaims", "amorcage-data")]["status"] = {"phase": "Bound"}
    world["kube"].objects[("deployments", "amorcage")]["status"] = {"readyReplicas": 1}
    world["kube"].applied.clear()
    monkeypatch.setattr(rg, "read_creds", lambda k, n, r: None)
    assert cli.main(["status", "amorcage", "--cluster", "harvlab2"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["ready"] is True and st["volume"]["size"] == "20Gi" and st["volume"]["phase"] == "Bound"
    assert st["accounts"] == {"push": True, "pull": True, "ca": True} and st["address"] == "172.16.2.76"
    assert world["kube"].applied == []


def test_wait_ready_wants_401_then_the_read_account(tmp_path):
    import oci_push as op
    import test_oci_push_175 as t175
    r = t175.FakeRegistry(user="pull", password="pw", basic=True)
    try:
        class Plain:
            def __new__(cls, host, creds, ca_pem=None, timeout=15):
                return op.Registry(host, creds, plain_http=True, timeout=timeout)
        d = {"endpoint": r.host, "ca_pem": None}
        assert cli.wait_ready(d, {"username": "pull", "password": "pw"}, 5, registry_cls=Plain) is None
        t = iter(range(100))
        why = cli.wait_ready(d, {"username": "pull", "password": "bad"}, 1, registry_cls=Plain,
                             clock=lambda: next(t), sleep=lambda s: None)
        assert why == "the read account is refused"
        r.user = None                                     # registre ouvert : refusé
        why = cli.wait_ready(d, {"username": "pull", "password": "pw"}, 1, registry_cls=Plain,
                             clock=lambda: next(t), sleep=lambda s: None)
        assert why == "the registry answered without authentication"
    finally:
        r.close()


@pytest.mark.parametrize("extra,entry,why", [
    (("--dns-name", "bad name\nsubjectAltName=DNS:evil"), {"nodes": [{"ip": "10.0.0.1"}]}, "--dns-name"),   # m-4
    ((), None, "no nodes in the console configuration"),                                                   # m-5
    ((), {"kubeconfig": "/k"}, "no nodes in the console configuration"),
])
def test_deploy_refuses_a_bad_dns_name_or_a_cluster_without_nodes(world, capsys, monkeypatch, extra, entry, why):
    monkeypatch.setattr(cli, "kube_from", lambda args, need_entry=False: (world["kube"], entry))
    code, out, err = deploy(capsys, *extra)
    assert code == 2 and why in err and "Traceback" not in err
    assert world["kube"].applied == [] and world["imports"] == []
