"""2.9.0 tâche 7 : paquet OpenBao du mode B1 (bin/lib/openbao_bundle.py).

Construit contre le faux registre (manifeste, couches, index multi-
architecture, identifiants), puis relu par `verify` ; chaque altération a
son cas. Mutation jouée (rapport) : « empreinte d'un blob non recalculée ».
"""

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import oci_push as op  # noqa: E402
import openbao_bundle as ob  # noqa: E402
from fake_openbao_b1 import make_bundle, make_registry  # noqa: E402

TPL = (ROOT / "config" / "openbao" / "openbao.hcl.tpl").read_text()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return make_bundle(tmp_path_factory.mktemp("b"))


def test_build_and_verify(built, tmp_path):
    path, got = built
    assert path.name == "harvester-ops-openbao-9.9.9-9.9.9-1.tar.gz" and got["version"] == "9.9.9"
    assert Path(str(path) + ".sha256").read_text().split()[0] == hashlib.sha256(path.read_bytes()).hexdigest()
    info = ob.verify(path, tmp_path)
    assert info["tag"] == "9.9.9-1" and info["manifest_digest"] == got["manifest_digest"]
    assert info["provenance"]["cosign"]["state"] == "not-run" and info["template"] == TPL
    assert info["provenance"]["platform"] == "linux/amd64"
    with tarfile.open(path) as t:
        assert sorted(m.name.split("/")[-1] for m in t if m.isfile()) == sorted(ob.FILES + (ob.SUMS,))
        for m in t:
            if m.isfile():
                assert b"Pw-AppCo-777" not in t.extractfile(m).read()


def test_build_pins_the_known_tag(tmp_path):
    reg = make_registry(tag="2.7.2-5.4", version="2.7.2")
    try:
        src = op.Registry(reg.host, {"username": "svc", "password": "Pw-AppCo-777"}, plain_http=True)
        with pytest.raises(ob.BundleError) as e:
            ob.build(src, reg.host, ob.DEFAULT_REPOSITORY, "2.7.2-5.4", TPL, tmp_path)
        assert e.value.code == "bundle-pin-mismatch"
        assert not list(tmp_path.glob("*.tar.gz"))
    finally:
        reg.close()


@pytest.mark.parametrize("state,ok", [("verified", True), ("failed", False)])
def test_build_cosign(tmp_path, state, ok):
    reg = make_registry()
    seen = []
    try:
        src = op.Registry(reg.host, {"username": "svc", "password": "Pw-AppCo-777"}, plain_http=True)

        def cos(ref):
            seen.append(ref)
            return (state, "detail")
        if ok:
            got = ob.build(src, reg.host, ob.DEFAULT_REPOSITORY, "9.9.9-1", TPL, tmp_path, cosign=cos)
            assert got["cosign"] == "verified"
        else:
            with pytest.raises(ob.BundleError) as e:
                ob.build(src, reg.host, ob.DEFAULT_REPOSITORY, "9.9.9-1", TPL, tmp_path, cosign=cos)
            assert e.value.code == "bundle-cosign-failed"
        assert seen == [f"{reg.host}/{ob.DEFAULT_REPOSITORY}@{reg.index_digest}"]   # signature de l'index épinglé
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# Altérations
# ---------------------------------------------------------------------------
def unpack(path, dest):
    with tarfile.open(path) as t:
        t.extractall(dest, filter="data")
    return dest / path.name[: -len(".tar.gz")]


def repack(root, out, extra=None):
    with tarfile.open(out, "w:gz") as t:
        t.add(root, arcname=root.name)
        for name, data, kind in extra or ():
            ti = tarfile.TarInfo(name)
            if kind == "sym":
                ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
                t.addfile(ti)
            else:
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
    return out


def resum(root):
    lines = [f"{hashlib.sha256((root / f).read_bytes()).hexdigest()}  {f}" for f in ob.FILES]
    (root / ob.SUMS).write_text("\n".join(lines) + "\n")


def edit_image(root, fn):
    """Réécrit l'archive OCI : fn(membres {nom: octets}) -> membres."""
    with tarfile.open(root / ob.IMAGE_TAR) as t:
        members = {m.name: t.extractfile(m).read() for m in t if m.isfile()}
    members = fn(members)
    with tarfile.open(root / ob.IMAGE_TAR, "w") as t:
        for n, d in members.items():
            ti = tarfile.TarInfo(n)
            ti.size = len(d)
            t.addfile(ti, io.BytesIO(d))


def alter_blob(m):
    idx = json.loads(m["index.json"])
    man = json.loads(m[f"blobs/sha256/{idx['manifests'][0]['digest'][7:]}"])
    k = f"blobs/sha256/{man['layers'][0]['digest'][7:]}"
    m[k] = m[k][:-1] + bytes([m[k][-1] ^ 1])
    return m


def two_images(m):
    idx = json.loads(m["index.json"])
    idx["manifests"].append(dict(idx["manifests"][0]))
    m["index.json"] = json.dumps(idx).encode()
    return m


def other_name(m):
    idx = json.loads(m["index.json"])
    idx["manifests"][0]["annotations"][ob.A_NAME] = "evil.example/x:1"
    m["index.json"] = json.dumps(idx).encode()
    return m


def drop_index(m):
    prov_index = [k for k in m if k.startswith("blobs/")]
    for k in prov_index:
        body = m[k]
        try:
            if json.loads(body).get("manifests"):
                del m[k]
        except ValueError:
            pass
    return m


CASES = [
    ("template-changed", lambda r: (r / ob.TEMPLATE).write_text(TPL + "# changed\n"), None, "bundle-sums"),
    ("sums-missing", lambda r: (r / ob.SUMS).unlink(), None, "bundle-sums"),
    ("provenance-tag", lambda r: ((r / ob.PROVENANCE).write_text(json.dumps(dict(
        json.loads((r / ob.PROVENANCE).read_text()), tag="9.9.9-2"))), resum(r)), None, "bundle-provenance"),
    ("provenance-digest", lambda r: ((r / ob.PROVENANCE).write_text(json.dumps(dict(
        json.loads((r / ob.PROVENANCE).read_text()), manifest_digest="sha256:" + "1" * 64))), resum(r)), None,
     "bundle-digest-mismatch"),
    ("blob-altered", lambda r: (edit_image(r, alter_blob), resum(r)), None, "bundle-digest-mismatch"),
    ("two-images", lambda r: (edit_image(r, two_images), resum(r)), None, "bundle-image-bad"),
    ("other-image-name", lambda r: (edit_image(r, other_name), resum(r)), None, "bundle-image-bad"),
    ("pinned-index-missing", lambda r: (edit_image(r, drop_index), resum(r)), None, "bundle-image-bad"),
    ("extra-file", lambda r: None, [("{top}/run.sh", b"#!/bin/sh\n", "file")], "bundle-unsafe"),
    ("dotdot", lambda r: None, [("{top}/../evil", b"x", "file")], "bundle-unsafe"),
    ("symlink", lambda r: None, [("{top}/link", b"", "sym")], "bundle-unsafe"),
]


@pytest.mark.parametrize("name,change,extra,code", CASES, ids=[c[0] for c in CASES])
def test_verify_refuses_an_altered_bundle(built, tmp_path, name, change, extra, code):
    path, _ = built
    root = unpack(path, tmp_path / "x")
    change(root)
    out = repack(root, tmp_path / path.name, [(n.format(top=root.name), d, k) for n, d, k in extra or ()])
    with pytest.raises(ob.BundleError) as e:
        ob.verify(out, tmp_path / "check")
    assert e.value.code == code, str(e.value)


def test_verify_refuses_a_known_tag_with_another_index(built, tmp_path):
    path, _ = built
    root = unpack(path, tmp_path / "x")
    prov = json.loads((root / ob.PROVENANCE).read_text())
    prov.update(tag="2.7.2-5.4", version="2.7.2")
    (root / ob.PROVENANCE).write_text(json.dumps(prov))
    resum(root)
    top = tmp_path / "harvester-ops-openbao-2.7.2-2.7.2-5.4"
    root.rename(top)
    out = repack(top, tmp_path / "harvester-ops-openbao-2.7.2-2.7.2-5.4.tar.gz")
    with pytest.raises(ob.BundleError) as e:
        ob.verify(out, tmp_path / "check")
    assert e.value.code == "bundle-pin-mismatch"


@pytest.mark.parametrize("name", ["openbao.tar.gz", "harvester-ops-openbao-.tar.gz", "../harvester-ops-openbao-1-x.tar.gz",
                                  "harvester-ops-openbao-1.0-x.tgz"])
def test_bad_names(tmp_path, name):
    p = tmp_path / Path(name).name
    p.write_bytes(b"")
    with pytest.raises(ob.BundleError) as e:
        ob.verify(p, tmp_path)
    assert e.value.code in ("bundle-bad-name", "bundle-unsafe")


def test_bundle_name_refuses_a_dash_in_the_version():
    with pytest.raises(ob.BundleError):
        ob.bundle_name("2.7.2-rc1", "x")
