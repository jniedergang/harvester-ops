"""v1.45.0 : le script `harvester-vm-transfer`, avec un faux kubectl.

Le script relève les deux clusters, rend le contrôle préalable (texte ou
JSON), refuse avec le code 2 ce que le contrôle bloque, et ne modifie rien
en `check` ni en `--dry-run`. Le faux kubectl répond depuis un état JSON
(le « kubeconfig » EST ce fichier d'état) et journalise chaque appel.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "bin" / "harvester-vm-transfer.py"
FIX = ROOT / "tests" / "fixtures" / "vm_transfer"
GIB = 1024 ** 3

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
i = args.index("--kubeconfig")
kc = args[i + 1]
rest = args[:i] + args[i + 2:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a") as f:
    f.write(json.dumps({"kc": os.path.basename(kc), "args": rest}) + "\n")
state = json.load(open(kc))
if rest[:2] == ["get", "--raw"]:
    if state.get("down"):
        sys.stderr.write("Unable to connect to the server\n"); sys.exit(1)
    print("ok"); sys.exit(0)
if rest[0] == "config":
    print(state.get("server", "https://127.0.0.1:6443")); sys.exit(0)
if rest[0] in ("create", "patch", "delete", "replace"):
    print("{}"); sys.exit(0)
if rest[0] == "get":
    kind = rest[1]
    ns = rest[rest.index("-n") + 1] if "-n" in rest else None
    name = rest[2] if len(rest) > 2 and not rest[2].startswith("-") else None
    objs = state["objects"].get(kind, {})
    if name:
        key = f"{ns}/{name}" if ns else name
        if key not in objs:
            sys.stderr.write(f'Error from server (NotFound): {kind} "{name}" not found\n')
            sys.exit(1)
        print(json.dumps(objs[key])); sys.exit(0)
    items = [o for k, o in objs.items() if ns is None or k.startswith(ns + "/")]
    print(json.dumps({"items": items})); sys.exit(0)
sys.stderr.write("unsupported: " + " ".join(rest) + "\n"); sys.exit(1)
'''

FAKE_YQ = r'''#!/usr/bin/env python3
import json, sys
print(json.dumps(json.load(open(sys.argv[-1]))))
'''


def setting(name, value, configured=None):
    o = {"metadata": {"name": name}, "value": value}
    if configured is not None:
        o["status"] = {"conditions": [{"type": "configured",
                                       "status": "True" if configured else "False"}]}
    return o


def lh_node(name="n1", room=500 * GIB):
    return {"metadata": {"name": name, "namespace": "longhorn-system"},
            "spec": {"disks": {"d": {"allowScheduling": True, "storageReserved": 0}}},
            "status": {"diskStatus": {"d": {
                "conditions": [{"type": "Ready", "status": "True"},
                               {"type": "Schedulable", "status": "True"}],
                "storageMaximum": 4 * room, "storageScheduled": 0,
                "storageAvailable": 4 * room}}}}


def sc(name, default=False, replicas="1"):
    o = {"metadata": {"name": name}, "provisioner": "driver.longhorn.io",
         "parameters": {"numberOfReplicas": replicas}}
    if default:
        o["metadata"]["annotations"] = {"storageclass.kubernetes.io/is-default-class": "true"}
    return o


NFS = '{"type":"nfs","endpoint":"172.16.0.5:/volume1/BACKUP/lab"}'


def common(version, target=NFS):
    return {
        "settings.harvesterhci.io": {"server-version": setting("server-version", version),
                                     "backup-target": setting("backup-target", target, True)},
        "settings.longhorn.io": {},
        "nodes.longhorn.io": {"longhorn-system/n1": lh_node()},
        "storageclasses": {"harvester-longhorn": sc("harvester-longhorn", True),
                           "harv-rep1": sc("harv-rep1")},
        "virtualmachineimages.harvesterhci.io": {},
    }


def source_state(running=True, target=NFS):
    vm = json.loads((FIX / "vm-leap156.json").read_text())
    vm.pop("status", None)
    objs = common("v1.8.2", target)
    objs["virtualmachines.kubevirt.io"] = {"default/leap156": vm}
    objs["virtualmachineinstances.kubevirt.io"] = (
        {"default/leap156": {"metadata": {"name": "leap156"}, "status": {"phase": "Running"}}}
        if running else {})
    objs["persistentvolumeclaims"] = {"default/leap156-disk-0-gfcec": {
        "metadata": {"name": "leap156-disk-0-gfcec", "namespace": "default",
                     "annotations": {"harvesterhci.io/imageId": "default/opensuse-leap-cloud"}},
        "spec": {"accessModes": ["ReadWriteMany"], "volumeMode": "Block",
                 "storageClassName": "longhorn-opensuse-leap-cloud", "volumeName": "pvc-1",
                 "resources": {"requests": {"storage": "10Gi"}}}}}
    objs["volumes.longhorn.io"] = {"longhorn-system/pvc-1": {"status": {"actualSize": 3 * GIB}}}
    objs["virtualmachineimages.harvesterhci.io"] = {"default/opensuse-leap-cloud": {
        "metadata": {"name": "opensuse-leap-cloud", "namespace": "default"},
        "spec": {"displayName": "leap.qcow2"}, "status": {"size": 700, "virtualSize": 10 * GIB}}}
    return {"objects": objs}


def target_state(networks=("default/production",), down=False, target=NFS, cdi=True):
    objs = common("v1.8.2", target)
    objs["customresourcedefinitions"] = {
        n: {"metadata": {"name": n}} for n in ("virtualmachines.kubevirt.io",
                                               "datavolumes.cdi.kubevirt.io")}
    objs["cdis.cdi.kubevirt.io"] = {"cdi": {"status": {"phase": "Deployed" if cdi else "Error"}}}
    objs["namespaces"] = {n: {"metadata": {"name": n}} for n in ("default", "lab")}
    objs["virtualmachines.kubevirt.io"] = {}
    objs["network-attachment-definitions.k8s.cni.cncf.io"] = {
        n: {"metadata": {"namespace": n.split("/")[0], "name": n.split("/")[1]}} for n in networks}
    return {"objects": objs, "down": down}


@pytest.fixture
def sandbox(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("kubectl", FAKE_KUBECTL), ("yq", FAKE_YQ)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")

    def write(name, state):
        p = tmp_path / f"{name}.kubeconfig"
        p.write_text(json.dumps(state))
        return p

    def call(*argv, src=None, dst=None, config=None):
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                   FAKE_KUBECTL_LOG=str(log))
        if config:
            env["HARVESTER_OPS_CONFIG"] = str(config)
        return subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True,
                              text=True, env=env, timeout=60)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()]

    return type("S", (), {"write": staticmethod(write), "call": staticmethod(call),
                          "calls": staticmethod(calls), "tmp": tmp_path})


@pytest.mark.parametrize("cmd", [[], ["check"], ["migrate"], ["export"], ["import"]])
def test_help(cmd):
    r = subprocess.run([sys.executable, str(SCRIPT), *cmd, "--help"], capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0 and "usage:" in r.stdout


def test_check_json_nominal(sandbox):
    s = sandbox.write("harvlab", source_state())
    d = sandbox.write("harvlab2", target_state())
    r = sandbox.call("check", "--from-kubeconfig", str(s), "--vm", "default/leap156",
                     "--to-kubeconfig", str(d), "--name", "leap156", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["engine"] == "backup" and out["reason"] == "shared-target"
    assert not [f for f in out["findings"] if f["level"] == "block"]
    assert out["mappings"]["networks"] == {"default/production": "default/production"}
    # les secrets cloud-init ne sont jamais décrits, seulement comptés
    assert out["inventory"]["secrets"] == 1
    assert out["inventory"]["disks"][0]["used"] == 3 * GIB
    assert "STEP_EVENT|check|done|engine: backup" in r.stderr


def test_check_refuses_with_code_2(sandbox):
    s = sandbox.write("harvlab", source_state())
    d = sandbox.write("harvlab2", target_state(networks=("default/other",)))
    r = sandbox.call("check", "--from-kubeconfig", str(s), "--vm", "default/leap156",
                     "--to-kubeconfig", str(d))
    assert r.returncode == 2
    assert "[block] network-unmapped" in r.stdout
    assert "STEP_EVENT|check|error|" in r.stderr


def test_an_explicit_mapping_lifts_the_block(sandbox):
    s = sandbox.write("harvlab", source_state())
    d = sandbox.write("harvlab2", target_state(networks=("default/other",)))
    r = sandbox.call("check", "--from-kubeconfig", str(s), "--vm", "default/leap156",
                     "--to-kubeconfig", str(d), "--map-net", "default/production=default/other")
    assert r.returncode == 0, r.stdout + r.stderr


def test_unreachable_target(sandbox):
    s = sandbox.write("harvlab", source_state())
    d = sandbox.write("harvlab2", target_state(down=True))
    r = sandbox.call("check", "--from-kubeconfig", str(s), "--vm", "default/leap156",
                     "--to-kubeconfig", str(d), "--json")
    assert r.returncode == 2
    assert [f["code"] for f in json.loads(r.stdout)["findings"]] == ["target-unreachable"]


def test_file_engine_when_targets_differ(sandbox):
    s = sandbox.write("harvlab", source_state())
    d = sandbox.write("harvlab2", target_state(target='{"type":"nfs","endpoint":"10.0.0.9:/x"}'))
    r = sandbox.call("check", "--from-kubeconfig", str(s), "--vm", "default/leap156",
                     "--to-kubeconfig", str(d), "--json")
    out = json.loads(r.stdout)
    assert (out["engine"], out["reason"]) == ("file", "different-targets")
    assert out["mappings"]["storage_classes"] == {"longhorn-opensuse-leap-cloud": "harvester-longhorn"}


def test_clusters_by_name_from_the_configuration(sandbox):
    s = sandbox.write("harvlab", source_state())
    d = sandbox.write("harvlab2", target_state())
    cfg = sandbox.tmp / "config.yaml"
    cfg.write_text(json.dumps({"clusters": [{"name": "harvlab", "kubeconfig": str(s)},
                                            {"name": "harvlab2", "kubeconfig": str(d)}]}))
    r = sandbox.call("check", "--from", "harvlab", "--vm", "default/leap156", "--to", "harvlab2",
                     config=cfg)
    assert r.returncode == 0, r.stderr
    r = sandbox.call("check", "--from", "nope", "--vm", "default/leap156", "--to", "harvlab2",
                     config=cfg)
    assert r.returncode == 1 and "unknown cluster: nope" in r.stderr


def test_dry_run_changes_nothing(sandbox):
    s = sandbox.write("harvlab", source_state())
    d = sandbox.write("harvlab2", target_state())
    r = sandbox.call("migrate", "--from-kubeconfig", str(s), "--vm", "default/leap156",
                     "--to-kubeconfig", str(d), "--mode", "short", "--dry-run")
    assert r.returncode == 0, r.stderr
    verbs = {c["args"][0] for c in sandbox.calls()}
    assert verbs <= {"get", "config"}, verbs


def test_export_check_counts_the_store(sandbox):
    s = sandbox.write("harvlab", source_state())
    out_dir = sandbox.tmp / "exports"
    out_dir.mkdir()
    r = sandbox.call("export", "--from-kubeconfig", str(s), "--vm", "default/leap156",
                     "--out", str(out_dir), "--dry-run", "--json")
    assert r.returncode == 0, r.stderr
    codes = [f["code"] for f in json.loads(r.stdout)["findings"]]
    assert "secrets-in-archive" in codes
    assert list(out_dir.iterdir()) == []


def test_short_mode_without_shared_target_is_refused(sandbox):
    s = sandbox.write("harvlab", source_state(target=""))
    d = sandbox.write("harvlab2", target_state())
    r = sandbox.call("migrate", "--from-kubeconfig", str(s), "--vm", "default/leap156",
                     "--to-kubeconfig", str(d), "--mode", "short")
    assert r.returncode == 2
    assert "short-mode-needs-backup" in r.stdout
    verbs = {c["args"][0] for c in sandbox.calls()}
    assert verbs <= {"get", "config"}


def test_check_reports_the_amount_and_the_speed(sandbox):
    s = sandbox.write("harvlab", source_state())
    d = sandbox.write("harvlab2", target_state())
    r = sandbox.call("check", "--from-kubeconfig", str(s), "--vm", "default/leap156",
                     "--to-kubeconfig", str(d), "--json", "--speed", "eco")
    out = json.loads(r.stdout)
    assert out["amount"] == {"disks": 1, "size": 10 * GIB, "used": 3 * GIB}
    assert out["request"]["speed"] == "eco" and out["request"]["parallel"] == 1
    assert out["request"]["boost"] is False
    r = sandbox.call("check", "--from-kubeconfig", str(s), "--vm", "default/leap156",
                     "--to-kubeconfig", str(d), "--json", "--speed", "max", "--parallel", "3",
                     "--bandwidth", "200")
    req = json.loads(r.stdout)["request"]
    assert (req["boost"], req["parallel"], req["bandwidth"]) == (True, 3, 200.0)
    r = sandbox.call("check", "--from-kubeconfig", str(s), "--vm", "default/leap156",
                     "--to-kubeconfig", str(d))
    assert "to transfer: 1 disk(s), 10.0 GiB (3.0 GiB used)" in r.stdout


def test_a_kubectl_timeout_is_transient_and_leaks_no_path(tmp_path, monkeypatch):
    """Vécu sur le banc : un appel kubectl est resté 60 s sans réponse (API
    à genoux). L'erreur de subprocess citait la ligne de commande, chemin du
    kubeconfig compris, jusque dans l'interface."""
    import importlib.util
    import subprocess as sp
    spec = importlib.util.spec_from_file_location("hvt", SCRIPT)
    hvt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hvt)

    def slow(*a, **k):
        raise sp.TimeoutExpired(cmd=a[0], timeout=60)
    monkeypatch.setattr(hvt.subprocess, "run", slow)
    k = hvt.Kube("/secret/place/kubeconfig.yaml")
    with pytest.raises(hvt.KubeError) as ei:
        k.get("virtualmachineimages.harvesterhci.io", "default", "x")
    msg = str(ei.value)
    assert "/secret/place" not in msg and "kubeconfig" not in msg
    assert "timed out" in msg
    sys.path.insert(0, str(ROOT / "bin" / "lib"))
    import vm_transfer_run as run
    assert run.transient(ei.value)
