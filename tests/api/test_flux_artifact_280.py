"""2.8.0 (tâche 4) : bin/lib/flux_artifact.py. Archive déterministe, types
de média de Flux (acceptés par l'OCIRepository de source-controller v1.10.1
sur harvlab2, plan 2.8.0, « Essais préalables (tâche 1) », point 4), envoi
par oci_push.Registry."""

import gzip
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import flux_artifact as fa  # noqa: E402

FILES = {"manifests/demo/b.yaml": "kind: ConfigMap\n", "manifests/demo/a.yaml": "kind: ConfigMap\n",
         "manifests/demo/sub/c.yaml": "x: 1\n"}


def test_two_builds_give_the_same_bytes():
    """Mutation « archive non déterministe (mtime réel) »."""
    a, b = fa.build(FILES, "demo", "1.0.0"), fa.build(dict(reversed(list(FILES.items()))), "demo", "1.0.0")
    assert a == b


def test_the_layer_is_sorted_with_zero_times_and_owners():
    lay = fa.layer(FILES, "demo")
    assert lay[4:8] == b"\x00\x00\x00\x00"                       # gzip sans date
    with tarfile.open(fileobj=io.BytesIO(lay), mode="r:gz") as t:
        members = t.getmembers()
        assert [m.name for m in members] == ["a.yaml", "b.yaml", "sub/c.yaml"]
        assert all((m.mtime, m.uid, m.gid, m.mode, m.uname, m.gname) == (0, 0, 0, 0o644, "", "") for m in members)
        assert t.extractfile("sub/c.yaml").read() == b"x: 1\n"


def test_media_types_of_flux():
    lay, config, manifest = fa.build(FILES, "demo", "1.0.0")
    m = json.loads(manifest)
    assert m["mediaType"] == "application/vnd.oci.image.manifest.v1+json"
    assert m["config"] == {"mediaType": "application/vnd.cncf.flux.config.v1+json", "digest": fa.digest_of(config),
                           "size": len(config)}
    assert m["layers"] == [{"mediaType": "application/vnd.cncf.flux.content.v1.tar+gzip",
                            "digest": fa.digest_of(lay), "size": len(lay)}]
    assert m["annotations"]["org.opencontainers.image.created"] == "1970-01-01T00:00:00Z"
    assert fa.repo_of("banc-280", "demo") == "harvester-ops/gitops/banc-280/demo"


@pytest.mark.parametrize("files", [
    {"manifests/other/a.yaml": "x"}, {"manifests/demo/../x.yaml": "x"}, {}, {"manifests/demo/": "x"},
])
def test_files_outside_the_item_are_refused(files):
    with pytest.raises(fa.FluxArtifactError):
        fa.layer(files, "demo")


def test_a_too_big_content_is_refused(monkeypatch):
    monkeypatch.setattr(fa, "MAX_BYTES", 10)
    with pytest.raises(fa.FluxArtifactError):
        fa.layer(FILES, "demo")


class Reg:
    def __init__(self, have=()):
        self.have, self.blobs, self.manifests = set(have), [], []

    def push_blob(self, repo, digest, data):
        assert fa.digest_of(data) == digest
        if digest in self.have:
            return False
        self.blobs.append(digest)
        self.have.add(digest)
        return True

    def put_manifest(self, repo, tag, body, media_type):
        self.manifests.append((repo, tag, media_type, body))


def test_push_sends_blobs_then_the_manifest():
    built = fa.build(FILES, "demo", "1.0.0")
    r = Reg()
    d = fa.push(r, "harvester-ops/gitops/banc/demo", "1.0.0", built)
    assert r.blobs == [fa.digest_of(built[1]), fa.digest_of(built[0])]
    assert r.manifests == [("harvester-ops/gitops/banc/demo", "1.0.0", fa.M_OCI, built[2])]
    assert d == fa.digest_of(built[2])
    r2 = Reg(have=r.have)
    fa.push(r2, "x", "1.0.0", built)
    assert r2.blobs == []                                         # republication : rien de renvoyé


# ---------------------------------------------------------------------------
# Publication d'un élément gitops (harvester-artefacts publish)
# ---------------------------------------------------------------------------
import importlib.util  # noqa: E402


@pytest.fixture(scope="module")
def tool():
    s = importlib.util.spec_from_file_location("harvester_artefacts_280", ROOT / "bin" / "harvester-artefacts.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


def package(tmp_path, content):
    root = tmp_path / "banc-280-1.0.0"
    (root / "manifests" / "demo").mkdir(parents=True)
    (root / "manifests" / "demo" / "a.yaml").write_text(content)
    item = {"name": "demo", "kind": "gitops", "files": ["manifests/demo/a.yaml"]}
    return {"path": str(root), "index": {"name": "banc-280", "version": "1.0.0", "items": [item]}}, item


CM = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: demo, namespace: equipe-a}\ndata: {a: b}\n"
CRD = "apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata: {name: x.example.io}\n"


def test_a_gitops_item_is_published_as_a_flux_artefact(tool, tmp_path):
    pkg, item = package(tmp_path, CM)
    images, charts, skipped, gitops = tool.publication(pkg, None)
    assert gitops == [item] and skipped == [] and images == charts == []
    repo, tag, built = tool.gitops_artifact(pkg, item)
    assert (repo, tag) == ("harvester-ops/gitops/banc-280/demo", "1.0.0")
    assert built == fa.build({"manifests/demo/a.yaml": CM}, "demo", "1.0.0", source="banc-280/demo")


def test_a_gitops_item_with_a_crd_is_refused_at_build(tool, tmp_path):
    """Un outil GitOps n'est pas une porte dérobée vers les CRD."""
    pkg, item = package(tmp_path, CRD)
    with pytest.raises(tool.Refused) as e:
        tool.gitops_artifact(pkg, item)
    assert e.value.findings[0]["code"] == "publish-gitops-refused"
    assert "manifest-kind-refused" in e.value.findings[0]["facts"]["reason"]


class Dst:
    host = "172.16.2.76:5000"

    def __init__(self, have=None):
        self.have = have

    def manifest_digest(self, repo, tag):
        return self.have


@pytest.mark.parametrize("have,state", [(None, "new"), ("same", "present"), ("sha256:" + "0" * 64, "differs")])
def test_the_plan_reads_the_tag_already_there(tool, tmp_path, have, state):
    pkg, item = package(tmp_path, CM)
    want = fa.digest_of(tool.gitops_artifact(pkg, item)[2][2])
    decl = {"name": "amorcage", "usage": ["images", "charts"]}
    findings, todo = tool.plan_publication(pkg, decl, Dst(want if have == "same" else have), [], [], tmp_path,
                                           gitops=[item])
    assert [(t["kind"], t["state"]) for t in todo] == [("gitops", state)]
    assert [f["code"] for f in findings] == (["publish-tag-differs"] if state == "differs" else [])
    findings, _ = tool.plan_publication(pkg, {"name": "r", "usage": ["images"]}, Dst(), [], [], tmp_path,
                                        gitops=[item])
    assert [f["code"] for f in findings] == ["registry-no-usage"]
