"""v2.6.0 : images importées dans le containerd des nœuds (node_images),
code extrait de `harvester-capi push_images`, qui l'appelle désormais."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import node_images as ni  # noqa: E402

# La commande distante d'avant l'extraction, recopiée telle quelle : elle ne
# doit pas changer (les nœuds n'ont que ctr, zcat et cat).
BEFORE = ("set -eo pipefail; TMP=$(mktemp); cat > \"$TMP\"; "
          "{ zcat \"$TMP\" 2>/dev/null || cat \"$TMP\"; } | sudo /var/lib/rancher/rke2/bin/ctr "
          "--address /run/k3s/containerd/containerd.sock --namespace k8s.io images import -; "
          "rm -f \"$TMP\"")


class Runner:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, argv, stdin=None, capture_output=None, timeout=None):
        self.calls.append((argv, stdin.read()))
        bad = self.fail_on and self.fail_on in argv[-2]
        return subprocess.CompletedProcess(argv, 1 if bad else 0, b"", b"secret stderr")


@pytest.fixture
def files(tmp_path):
    out = []
    for n in ("a.tar", "b.tar.gz"):
        (tmp_path / n).write_bytes(n.encode())
        out.append(str(tmp_path / n))
    return out


@pytest.mark.parametrize("entry,argv0", [
    ({"nodes": [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}, {"name": "no-ip"}]},
     ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
      "-p", "22", "rancher@10.0.0.1", BEFORE]),
    ({"nodes": [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}], "ssh": {"user": "ops", "port": 2222, "key": "/k"}},
     ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
      "-p", "2222", "-i", "/k", "ops@10.0.0.1", BEFORE]),
])
def test_every_file_goes_to_every_node_with_the_same_remote_command(files, entry, argv0):
    r, seen = Runner(), []
    res = ni.import_on_nodes(entry, files, lambda *a: seen.append(a), runner=r)
    assert res == {"nodes": 2, "imports": 4}
    assert r.calls[0] == (argv0, b"a.tar")
    assert [c[0][-2].split("@")[1] for c in r.calls] == ["10.0.0.1", "10.0.0.1", "10.0.0.2", "10.0.0.2"]
    assert [c[1] for c in r.calls] == [b"a.tar", b"b.tar.gz"] * 2
    assert seen[0] == ("images", "running", "2 image(s) to load on 2 node(s)")
    assert seen[-1] == ("images", "done", "4 image import(s)")


def test_a_failed_import_names_the_node_and_file_not_the_output(files):
    r = Runner(fail_on="10.0.0.2")
    with pytest.raises(RuntimeError) as e:
        ni.import_on_nodes({"nodes": [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]}, files, lambda *a: None,
                           sid="registry-images", runner=r)
    assert str(e.value) == "10.0.0.2: import of a.tar failed"
    assert len(r.calls) == 3


def test_harvester_capi_push_images_goes_through_node_images(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("harvester_capi_ni", ROOT / "bin" / "harvester-capi.py")
    capi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capi)
    got = []
    monkeypatch.setattr(capi.node_images, "import_on_nodes", lambda entry, files, step: got.append((entry, files)))
    entry = {"nodes": [{"ip": "10.0.0.1"}]}
    capi.push_images(entry, {"providers": [{"image_files": ["x.tar"]}, {"image_files": ["y.tar"]}, {}]})
    assert got == [(entry, ["x.tar", "y.tar"])]
    with pytest.raises(SystemExit):
        capi.push_images(None, {"providers": []})


def test_the_registry_bundle_is_a_ctr_archive_with_pinned_names(tmp_path):
    import json
    import tarfile
    import test_oci_layout_260 as t260
    import test_oci_push_175 as t175
    reg = t175.FakeRegistry()
    try:
        images, idx = t260.put_index(reg, "suse/registry", "3.1", ["arm64", "amd64"])
        t260.put_index(reg, "bci/bci-busybox", "16.0", ["amd64"])
        (tmp_path / "d.json").write_text(json.dumps({"name": "harvester-ops-registry", "version": "3.1.1", "items": [
            {"name": "registry", "kind": "images", "images": [f"{reg.host}/suse/registry:3.1@{idx}"]},
            {"name": "probe", "kind": "images", "images": [f"{reg.host}/bci/bci-busybox:16.0"]}]}))
        subprocess.run([sys.executable, str(ROOT / "bin" / "harvester-artefacts.py"), "pack", str(tmp_path / "d.json"),
                        "--out", str(tmp_path), "--plain-http", reg.host], check=True, capture_output=True)
        res = ni.bundle_from_package(tmp_path / "harvester-ops-registry-3.1.1.tar.gz", tmp_path / "b")
    finally:
        reg.close()
    amd = images["amd64"][0]
    assert res["images"]["registry"] == f"{reg.host}/suse/registry@{amd}"         # l'image amd64, pas l'index
    assert sorted(p.name for p in (tmp_path / "b").iterdir()) == ["SHA256SUMS", "registry-images.json",
                                                                  "registry-images.tar"]
    assert subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=tmp_path / "b", capture_output=True).returncode == 0
    with tarfile.open(tmp_path / "b" / "registry-images.tar") as t:
        index = json.load(t.extractfile("index.json"))
    names = sorted(m["annotations"]["io.containerd.image.name"] for m in index["manifests"])
    assert f"{reg.host}/suse/registry:3.1" in names and f"{reg.host}/suse/registry@{amd}" in names
