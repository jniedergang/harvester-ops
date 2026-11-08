"""2.8.0 (tâche 8) : sorte de paquet `rke2`, `harvester-artefacts pack
--rke2 VERSION [--cni C]` (côté connecté, récupération simulée) et
publication des fichiers en blobs d'un artefact `harvester-ops/rke2`."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import artefacts as art  # noqa: E402
import rke2_offline as ro  # noqa: E402

V = "v1.34.11+rke2r1"
SPEC = importlib.util.spec_from_file_location("harvester_artefacts_rke2", ROOT / "bin" / "harvester-artefacts.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def index(**over):
    it = {"name": "rke2", "kind": "rke2", "version": V, "files": ro.files_of(V), "images": ["docker.io/rancher/x:1"]}
    it.update(over)
    return {"name": "rke2", "version": "1.34.11-rke2r1", "items": [it]}


@pytest.mark.parametrize("over,needle", [
    ({}, None),
    ({"version": "latest"}, "items[0].version"),
    ({"files": ["manifests/rke2/a.yaml"]}, "items[0].files"),
    ({"images": []}, "items[0].images"),
])
def test_check_index_of_an_rke2_item(over, needle):
    idx, errors = art.check_index(index(**over), image_names=["docker.io/rancher/x:1"])
    if needle is None:
        assert errors == [] and idx["items"][0]["kind"] == "rke2" and "later" not in idx["items"][0]
    else:
        assert any(e.startswith(needle) for e in errors), errors


def test_rke2_description_names_a_valid_package():
    d = cli.rke2_description(V, "cilium")
    desc, errors = cli.check_description(d)
    assert errors == [] and desc["items"][0]["rke2"] == {"version": V, "cni": "cilium"}
    assert art.ARCHIVE_RE.match(f"{d['name']}-{d['version']}.tar.gz")


@pytest.mark.parametrize("rke2,needle", [({"version": "v1.34"}, "items[0].rke2"),
                                         ({"version": V, "cni": "weave"}, "items[0].rke2.cni"),
                                         ({"version": V, "extra": 1}, "items[0].rke2")])
def test_rke2_description_refusals(rke2, needle):
    _, errors = cli.check_description({"name": "p", "version": "1.0.0", "items": [
        {"name": "rke2", "kind": "rke2", "rke2": rke2}]})
    assert any(e.startswith(needle) for e in errors), errors


TAR = b"rke2 binaries"
LISTING = b"docker.io/rancher/rke2-runtime:v1.34.11-rke2r1\ndocker.io/rancher/hardened-cilium:v1\n"


def fake_fetch(store, tar=TAR):
    def fetch(url, dest, auth, cap, timeout=120, plain=()):
        store.append(url)
        name = url.rsplit("/", 1)[-1]
        data = {"rke2.linux-amd64.tar.gz": tar,
                "sha256sum-amd64.txt": f"{hashlib.sha256(TAR).hexdigest()}  rke2.linux-amd64.tar.gz\n".encode(),
                "get.rke2.io": b"#!/bin/sh\n", "rke2-images-all.linux-amd64.txt": LISTING}[name]
        if dest is None:
            return data, hashlib.sha256(data).hexdigest()
        Path(dest).write_bytes(data)
        return None, hashlib.sha256(data).hexdigest()
    return fetch


def test_fetch_rke2_writes_the_three_files_and_returns_the_cni_images(tmp_path, monkeypatch):
    urls = []
    monkeypatch.setattr(cli, "_fetch", fake_fetch(urls))
    it = {"name": "rke2", "kind": "rke2", "rke2": {"version": V, "cni": "calico"}}
    imgs = cli.fetch_rke2(cli.Sources({}), it, tmp_path)
    assert sorted(p.name for p in (tmp_path / "rke2" / V).iterdir()) == sorted(ro.FILES)
    assert [i["ref"] for i in imgs] == ["docker.io/rancher/rke2-runtime:v1.34.11-rke2r1"]
    assert f"{ro.DOWNLOAD}/v1.34.11%2Brke2r1/rke2.linux-amd64.tar.gz" in urls


def test_fetch_rke2_refuses_an_archive_that_does_not_match_its_sums(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_fetch", fake_fetch([], tar=b"altered"))
    with pytest.raises(ValueError) as e:
        cli.fetch_rke2(cli.Sources({}), {"name": "rke2", "kind": "rke2", "rke2": {"version": V, "cni": "calico"}},
                       tmp_path)
    assert "does not match" in str(e.value)


class Dst:
    host = "172.16.2.76"

    def __init__(self):
        self.blobs, self.manifests = {}, {}

    def manifest_digest(self, repo, tag):
        m = self.manifests.get((repo, tag))
        return "sha256:" + hashlib.sha256(m).hexdigest() if m else None

    def push_blob(self, repo, digest, data):
        self.blobs[(repo, digest)] = data

    def push_blob_file(self, repo, digest, path, size, mount_from=None, on_bytes=None):
        assert "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
        self.blobs[(repo, digest)] = Path(path).read_bytes()
        return True

    def put_manifest(self, repo, tag, body, media_type):
        self.manifests[(repo, tag)] = body


def test_publish_pushes_the_files_as_blobs_of_one_artefact(tmp_path):
    root = tmp_path / "rke2-1.34.11-rke2r1"
    for rel in ro.files_of(V):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"content of {rel}\n")
    idx = index(images=[])
    idx["items"][0].pop("images")
    pkg = {"path": str(root), "index": dict(idx, items=[dict(idx["items"][0], images=[])])}
    decl = {"name": "amorcage", "usage": ["images", "charts"]}
    dst = Dst()
    res = cli.publish(pkg, decl, dst, work=tmp_path / "work")
    assert res["rke2"] == 1
    body = json.loads(dst.manifests[(ro.REPO, "v1.34.11_rke2r1")])
    assert body["artifactType"] == ro.ARTIFACT_TYPE
    digests = {x["annotations"][ro.A_TITLE]: x["digest"] for x in body["layers"]}
    for rel in ro.files_of(V):
        want = "sha256:" + hashlib.sha256((root / rel).read_bytes()).hexdigest()
        assert digests[Path(rel).name] == want and (ro.REPO, want) in dst.blobs
    again = cli.publish(pkg, decl, dst, dry_run=True, work=tmp_path / "work2")
    assert [t["state"] for t in again["todo"]] == ["present"]
    no_images = cli.publish(pkg, {"name": "amorcage", "usage": ["charts"]}, Dst(), dry_run=True, work=tmp_path / "w3")
    assert "registry-no-usage" in [f["code"] for f in no_images["findings"]]
