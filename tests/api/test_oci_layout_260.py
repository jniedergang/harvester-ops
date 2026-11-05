"""v2.6.0 : images et charts en disposition OCI sur disque, copie en flux
depuis un registre et publication vers un autre (registres simulés de
test_oci_push_175), archive pour `ctr images import`, rangement dans un
registre de la console."""

import gzip
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin" / "lib"))
import oci_layout as ol  # noqa: E402
import oci_push as op  # noqa: E402
import test_oci_push_175 as t175  # noqa: E402  (FakeRegistry, payload)

sha = t175.sha
LIB = Path(__file__).resolve().parents[2] / "bin" / "lib"


def put_image(reg, repo, arch, layers, os_="linux"):
    """Une image d'une plateforme dans le registre simulé ; rend
    (empreinte du manifeste, empreintes des couches)."""
    digs = []
    for data in layers:
        reg.blobs[sha(data)] = data
        digs.append(sha(data))
    cfg = json.dumps({"architecture": arch, "os": os_, "rootfs": {"type": "layers", "diff_ids": digs}}).encode()
    reg.blobs[sha(cfg)] = cfg
    man = json.dumps({"schemaVersion": 2, "mediaType": op.M_OCI,
                      "config": {"mediaType": op.CONFIG_TYPE[op.M_OCI], "digest": sha(cfg), "size": len(cfg)},
                      "layers": [{"mediaType": op.LAYER_TYPE[op.M_OCI], "digest": d, "size": len(x)}
                                 for d, x in zip(digs, layers)]}).encode()
    reg.manifests[(repo, sha(man))] = (man, op.M_OCI)
    return sha(man), digs


def put_index(reg, repo, tag, arches, shared=b""):
    images = {a: put_image(reg, repo, a, [shared or t175.payload(2000, a.encode()),
                                          t175.payload(3000, (repo + a).encode())]) for a in arches}
    idx = json.dumps({"schemaVersion": 2, "mediaType": op.M_OCI_INDEX, "manifests": [
        {"mediaType": op.M_OCI, "digest": images[a][0], "size": 1, "platform": {"os": "linux", "architecture": a}}
        for a in arches]}).encode()
    reg.manifests[(repo, tag)] = (idx, op.M_OCI_INDEX)
    reg.manifests[(repo, sha(idx))] = (idx, op.M_OCI_INDEX)
    return images, sha(idx)


@pytest.fixture
def src():
    r = t175.FakeRegistry()
    yield r
    r.close()


@pytest.fixture
def dst():
    r = t175.FakeRegistry(user="push", password="pw", basic=True)
    r.where = set()
    yield r
    r.close()


def registry(r, creds=None):
    return op.Registry(r.host, creds, plain_http=True)


def push_client(r):
    return registry(r, {"username": "push", "password": "pw"})


@pytest.mark.parametrize("ref,expected", [
    ("nginx", ("docker.io/library/nginx", "latest")),
    ("nginx:1.27", ("docker.io/library/nginx", "1.27")),
    ("docker.io/nginx", ("docker.io/library/nginx", "latest")),
    ("docker.io/bitnami/redis:7", ("docker.io/bitnami/redis", "7")),
    ("ghcr.io/a/b@sha256:" + "a" * 64, ("ghcr.io/a/b", "sha256:" + "a" * 64)),
    ("ghcr.io/a/b:1@sha256:" + "a" * 64, ("ghcr.io/a/b", "1")),
    ("localhost:5000/x", ("localhost_5000/x", "latest")),
    ("registry.suse.com/suse/registry:3.1", ("registry.suse.com/suse/registry", "3.1")),
])
def test_images_are_stored_under_their_origin_host(ref, expected):
    assert ol.target_repo(ref) == expected


@pytest.mark.parametrize("ref", ["", "UPPER/case:1", "a/b:bad tag", "x@sha256:short"])
def test_a_bad_reference_is_refused(ref):
    with pytest.raises(ValueError):
        ol.target_repo(ref)


def test_charts_go_under_charts_with_their_version_as_tag():
    assert ol.chart_repo("podinfo", "6.9.2") == ("charts/podinfo", "6.9.2")
    assert ol.chart_repo("x", "1.0.0+build.3") == ("charts/x", "1.0.0_build.3")


def test_an_image_is_copied_once_for_amd64_bytes_unchanged(src, tmp_path):
    images, idx = put_index(src, "a/podinfo", "6.9.2", ["arm64", "amd64"])
    lay = ol.Layout(tmp_path / "images")
    res = ol.copy_image(registry(src), f"{src.host}/a/podinfo:6.9.2", lay)
    man_d, layers = images["amd64"]
    assert res == {"name": f"{src.host}/a/podinfo:6.9.2", "digest": man_d, "platform": "linux/amd64",
                   "layers": 2, "size": res["size"]}
    assert json.loads((tmp_path / "images" / "oci-layout").read_text()) == {"imageLayoutVersion": "1.0.0"}
    for d in layers + [man_d]:
        assert lay.blob_path(d).read_bytes() == (src.blobs.get(d) or src.manifests[("a/podinfo", d)][0])
    assert not any(lay.has_blob(d) for d in images["arm64"][1])            # rien de l'arm64
    entry, = ol.Layout(tmp_path / "images").entries()                       # relu depuis le disque
    assert entry["annotations"] == {ol.A_REF: "6.9.2", ol.A_NAME: f"{src.host}/a/podinfo:6.9.2"}
    assert entry["platform"] == {"os": "linux", "architecture": "amd64"}
    gets = len([x for x in src.log if x[0] == "GET" and "/blobs/" in x[1]])
    ol.copy_image(registry(src), f"{src.host}/a/podinfo:6.9.2", lay)       # deuxième copie : rien à tirer
    assert len([x for x in src.log if x[0] == "GET" and "/blobs/" in x[1]]) == gets
    assert len(lay.entries()) == 1


def test_layers_shared_between_images_are_stored_once(src, tmp_path):
    shared = t175.payload(4000, b"base")
    put_index(src, "a/one", "1", ["amd64"], shared=shared)
    put_index(src, "a/two", "1", ["amd64"], shared=shared)
    lay = ol.Layout(tmp_path / "l")
    ol.copy_image(registry(src), f"{src.host}/a/one:1", lay)
    ol.copy_image(registry(src), f"{src.host}/a/two:1", lay)
    assert len(lay.entries()) == 2
    assert len([x for x in src.log if x[0] == "GET" and x[1].endswith(sha(shared)[7:])]) == 1


@pytest.mark.parametrize("arches,single,platform", [
    (["arm64", "s390x"], None, "linux/amd64"),          # index sans amd64
    (None, "arm64", "linux/amd64"),                     # image d'une seule plateforme, autre
    (["amd64"], None, "linux/arm64"),                   # plateforme demandée absente
])
def test_a_missing_platform_is_a_named_error(src, tmp_path, arches, single, platform):
    if arches:
        put_index(src, "a/x", "1", arches)
    else:
        d, _ = put_image(src, "a/x", single, [b"layer"])
        src.manifests[("a/x", "1")] = src.manifests[("a/x", d)]
    with pytest.raises(op.RegistryError) as e:
        ol.copy_image(registry(src), f"{src.host}/a/x:1", ol.Layout(tmp_path / "l"), platform=platform)
    assert e.value.code == "no-platform"
    assert ol.Layout(tmp_path / "l").entries() == []


def test_a_single_platform_image_of_the_right_platform_is_taken(src, tmp_path):
    d, _ = put_image(src, "a/x", "amd64", [b"layer"])
    src.manifests[("a/x", "1")] = src.manifests[("a/x", d)]
    res = ol.copy_image(registry(src), f"{src.host}/a/x:1", ol.Layout(tmp_path / "l"))
    assert res["digest"] == d and res["platform"] == "linux/amd64"


def test_an_image_pinned_by_a_wrong_digest_is_refused(src, tmp_path):
    images, idx = put_index(src, "a/x", "1", ["amd64"])
    src.manifests[("a/x", "sha256:" + "b" * 64)] = src.manifests[("a/x", idx)]
    with pytest.raises(op.RegistryError) as e:
        ol.copy_image(registry(src), f"{src.host}/a/x@sha256:" + "b" * 64, ol.Layout(tmp_path / "l"))
    assert e.value.code == "digest-mismatch"
    ok = ol.copy_image(registry(src), f"{src.host}/a/x@{idx}", ol.Layout(tmp_path / "l"))
    assert ok["digest"] == images["amd64"][0]


def test_a_corrupted_layer_leaves_nothing_named(src, tmp_path):
    images, _ = put_index(src, "a/x", "1", ["amd64"])
    src.corrupt.add(images["amd64"][1][1])
    lay = ol.Layout(tmp_path / "l")
    with pytest.raises(op.RegistryError):
        ol.copy_image(registry(src), f"{src.host}/a/x:1", lay)
    assert lay.entries() == [] and not lay.has_blob(images["amd64"][1][1])
    assert not list((tmp_path / "l" / "blobs" / "sha256").glob("*.part"))


@pytest.fixture
def copied(src, tmp_path):
    shared = t175.payload(4000, b"base")
    put_index(src, "a/one", "1", ["amd64"], shared=shared)
    put_index(src, "a/two", "1", ["amd64"], shared=shared)
    lay = ol.Layout(tmp_path / "l")
    for n in ("one", "two"):
        ol.copy_image(registry(src), f"{src.host}/a/{n}:1", lay)
    return lay, shared


def test_an_image_is_published_layers_config_then_manifest(dst, copied):
    lay, shared = copied
    e = lay.entries()[0]
    res = ol.push_image(lay, e, push_client(dst), "x/one", "1")
    assert res == {"pushed": True, "digest": e["digest"], "sent": 3, "skipped": 0, "mounted": 0}
    assert dst.manifests[("x/one", "1")][0] == lay.blob_path(e["digest"]).read_bytes()
    puts = [x[1] for x in dst.log if x[0] in ("PUT", "PATCH")]
    assert puts[-1] == "/v2/x/one/manifests/1"                              # le manifeste en dernier
    again = ol.push_image(lay, e, push_client(dst), "x/one", "1")
    assert again["pushed"] is False and len(dst.uploads) == 3              # idempotent


def test_a_layer_already_published_elsewhere_is_mounted(dst, copied):
    lay, shared = copied
    mounts = {}
    one, two = lay.entries()
    ol.push_image(lay, one, push_client(dst), "x/one", "1", mounts=mounts)
    res = ol.push_image(lay, two, push_client(dst), "x/two", "1", mounts=mounts)
    assert res["mounted"] == 1 and res["sent"] == 2
    assert dst.mounts[-1] == (sha(shared), "x/one")


def test_a_tag_with_another_digest_is_never_overwritten(dst, copied):
    lay, _ = copied
    one, two = lay.entries()
    ol.push_image(lay, one, push_client(dst), "x/app", "1")
    before = dict(dst.manifests)
    with pytest.raises(op.RegistryError) as e:
        ol.push_image(lay, two, push_client(dst), "x/app", "1")
    assert e.value.code == "publish-tag-differs"
    assert dst.manifests == before


def test_a_registry_that_stores_another_digest_is_reported(dst, copied, monkeypatch):
    lay, _ = copied
    c = push_client(dst)
    monkeypatch.setattr(c, "put_manifest", lambda *a: "sha256:" + "f" * 64)
    with pytest.raises(op.RegistryError) as e:
        ol.push_image(lay, lay.entries()[0], c, "x/one", "1")
    assert e.value.code == "digest-mismatch"


def make_chart(path, chart_yaml, top="podinfo"):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as t:
        for name, data in ((f"{top}/Chart.yaml", chart_yaml), (f"{top}/values.yaml", b"replicaCount: 1\n")):
            if data is None:
                continue
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    path.write_bytes(gzip.compress(raw.getvalue(), mtime=0))
    return path


CHART_YAML = b"""apiVersion: v2
name: podinfo   # the chart
version: "6.9.2"
appVersion: 6.9.2
description: 'Podinfo Helm chart for Kubernetes'
keywords:
  - podinfo
maintainers:
- name: stefanprodan
"""


def test_a_chart_becomes_an_oci_artifact_with_helm_types(tmp_path):
    tgz = make_chart(tmp_path / "podinfo-6.9.2.tgz", CHART_YAML)
    lay = ol.Layout(tmp_path / "l")
    res = ol.chart_to_layout(tgz, lay)
    assert res["name"] == "podinfo" and res["version"] == "6.9.2" and res["size"] == tgz.stat().st_size
    man = json.loads(lay.read_blob(res["digest"]))
    assert man["config"]["mediaType"] == ol.M_HELM_CONFIG
    assert man["layers"] == [{"mediaType": ol.M_HELM_CHART, "digest": sha(tgz.read_bytes()), "size": res["size"]}]
    assert json.loads(lay.read_blob(man["config"]["digest"])) == {
        "name": "podinfo", "version": "6.9.2", "apiVersion": "v2", "appVersion": "6.9.2",
        "description": "Podinfo Helm chart for Kubernetes"}
    assert lay.find("podinfo:6.9.2")["annotations"][ol.A_REF] == "6.9.2"


@pytest.mark.parametrize("content,top,match", [
    (None, "podinfo", "no <chart>/Chart.yaml"),
    (b"name: podinfo\n", "podinfo", "no name or version"),
    (CHART_YAML, "a/b", "no <chart>/Chart.yaml"),          # Chart.yaml trop profond
])
def test_a_chart_without_a_readable_chart_yaml_is_refused(tmp_path, content, top, match):
    tgz = make_chart(tmp_path / "c.tgz", content, top=top)
    with pytest.raises(ValueError, match=match):
        ol.chart_to_layout(tgz, ol.Layout(tmp_path / "l"))


def test_a_file_that_is_not_a_tgz_is_refused(tmp_path):
    p = tmp_path / "c.tgz"
    p.write_bytes(b"not gzip")
    with pytest.raises(ValueError, match="not a readable .tgz"):
        ol.chart_to_layout(p, ol.Layout(tmp_path / "l"))


def test_a_chart_is_published_and_read_back(tmp_path, dst):
    tgz = make_chart(tmp_path / "podinfo-6.9.2.tgz", CHART_YAML)
    lay = ol.Layout(tmp_path / "l")
    res = ol.chart_to_layout(tgz, lay)
    repo, tag = ol.chart_repo(res["name"], res["version"])
    assert ol.push_image(lay, lay.find("podinfo:6.9.2"), push_client(dst), repo, tag)["pushed"]
    body, mt = dst.manifests[("charts/podinfo", "6.9.2")]
    assert mt == op.M_OCI and sha(body) == res["digest"]


def test_the_ctr_archive_names_each_image_by_tag_and_by_digest(copied, tmp_path):
    lay, shared = copied
    one = lay.entries()[0]
    name = one["annotations"][ol.A_NAME]
    out = tmp_path / "ctr.tar"
    assert ol.export_ctr_tar(lay, [name], out) == {"images": 1, "blobs": 4}
    with tarfile.open(out) as t:
        names = t.getnames()
        index = json.load(t.extractfile("index.json"))
    assert names[:2] == ["oci-layout", "index.json"]
    assert sorted(names[2:]) == sorted(f"blobs/sha256/{d[7:]}" for d in lay.blobs_of(one))
    assert [m["annotations"][ol.A_NAME] for m in index["manifests"]] == [name, ol.pinned_name(one)]
    assert ol.pinned_name(one).endswith("/a/one@" + one["digest"])
    assert all(m["digest"] == one["digest"] for m in index["manifests"])
    with pytest.raises(ValueError, match="no image named"):
        ol.export_ctr_tar(lay, ["nope:1"], tmp_path / "x.tar")


def test_a_blob_copied_into_the_layout_is_checked(tmp_path):
    lay = ol.Layout(tmp_path / "l").init()
    f = tmp_path / "f"
    f.write_bytes(b"data")
    with pytest.raises(op.RegistryError) as e:
        lay.add_blob_file(f, "sha256:" + "0" * 64)
    assert e.value.code == "digest-mismatch" and not lay.has_blob("sha256:" + "0" * 64)
    assert lay.add_blob_file(f, sha(b"data")).read_bytes() == b"data"
    lay.blob_path(sha(b"data")).write_bytes(b"tampered")
    with pytest.raises(op.RegistryError):
        lay.read_blob(sha(b"data"))
    with pytest.raises(ValueError):
        lay.blob_path("sha256:../../x")


def test_the_modules_need_only_the_standard_library():
    code = (f"import sys; sys.path.insert(0, {str(LIB)!r}); import oci_layout, oci_push; "
            "bad = [m for m in sys.modules if m.split('.')[0] in ('yaml', 'requests', 'flask')]; "
            "assert not bad, bad")
    subprocess.run([sys.executable, "-I", "-S", "-c", code], check=True)
