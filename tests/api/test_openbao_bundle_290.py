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
from fake_openbao_b1 import (make_bundle, make_registry, cosign_test_key, add_signature,  # noqa: E402
                             cosign_payload, cosign_sign)
import base64  # noqa: E402

TPL = (ROOT / "config" / "openbao" / "openbao.hcl.tpl").read_text()
KEY = cosign_test_key()[1]
APPCO_KEY = ROOT / "config" / "openbao" / ob.COSIGN_KEY_NAME


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return make_bundle(tmp_path_factory.mktemp("b"))


def test_build_and_verify(built, tmp_path):
    path, got = built
    assert path.name == "harvester-ops-openbao-9.9.9-9.9.9-1.tar.gz" and got["version"] == "9.9.9"
    assert Path(str(path) + ".sha256").read_text().split()[0] == hashlib.sha256(path.read_bytes()).hexdigest()
    info = ob.verify(path, tmp_path, cosign_key=KEY)
    assert info["tag"] == "9.9.9-1" and info["manifest_digest"] == got["manifest_digest"]
    assert info["trust"] == "signature"
    assert info["provenance"]["cosign"]["state"] == "not-run" and info["template"] == TPL
    assert info["provenance"]["platform"] == "linux/amd64"
    with tarfile.open(path) as t:
        assert sorted(m.name.split("/")[-1] for m in t if m.isfile()) == sorted(ob.FILES + (ob.SIGNATURE, ob.SUMS))
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
            got = ob.build(src, reg.host, ob.DEFAULT_REPOSITORY, "9.9.9-1", TPL, tmp_path, cosign=cos, cosign_key=KEY)
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
    files = ob.FILES + tuple(f for f in ob.OPTIONAL if (root / f).exists())
    lines = [f"{hashlib.sha256((root / f).read_bytes()).hexdigest()}  {f}" for f in files]
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
        ob.verify(out, tmp_path / "check", cosign_key=KEY)
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


# ---------------------------------------------------------------------------
# Revue I2 (tâches 5-7) : rien de ce que le paquet déclare n'est cru
# ---------------------------------------------------------------------------
def rebuilt(built, tmp_path, change, tag=None, version=None):
    """Le paquet de test modifié par change(root), renommé pour un autre tag."""
    path, _ = built
    root = unpack(path, tmp_path / "x")
    if tag:
        prov = json.loads((root / ob.PROVENANCE).read_text())
        prov.update(tag=tag, version=version)
        (root / ob.PROVENANCE).write_text(json.dumps(prov))
    change(root)
    resum(root)
    name = ob.bundle_name(version, tag) if tag else path.name
    top = tmp_path / name[: -len(".tar.gz")]
    root.rename(top)
    return repack(top, tmp_path / name)


def set_sigs(root, entries):
    (root / ob.SIGNATURE).write_text(json.dumps({"signatures": entries}))


def entry(digest, reference="dp.example/containers/openbao", key=None, payload=None):
    payload = payload or cosign_payload(digest, reference)
    return {"payload": base64.b64encode(payload).decode(), "signature": cosign_sign(payload, key)}


def other_key(tmp_path):
    import subprocess
    k = tmp_path / "other.key"
    subprocess.run(["openssl", "genrsa", "-out", str(k), "2048"], check=True, capture_output=True)
    return k


def index_of(built, tmp_path):
    return ob.verify(built[0], tmp_path / "idx", cosign_key=KEY)["index_digest"]


@pytest.mark.parametrize("case,code", [
    ("no-signature", "bundle-unsigned"),
    ("self-declared-cosign", "bundle-unsigned"),
    ("other-digest", "bundle-signature-bad"),
    ("other-repository", "bundle-signature-bad"),
    ("other-key", "bundle-signature-bad"),
    ("payload-altered", "bundle-signature-bad"),
    ("not-cosign-type", "bundle-signature-bad"),
    ("garbage", "bundle-signature-bad"),
    ("critical-not-object", "bundle-signature-bad"),
])
def test_unpinned_tag_needs_a_collection_signature(built, tmp_path, case, code):
    idx = index_of(built, tmp_path)

    def change(root):
        if case == "no-signature":
            (root / ob.SIGNATURE).unlink()
        elif case == "self-declared-cosign":
            (root / ob.SIGNATURE).unlink()
            prov = json.loads((root / ob.PROVENANCE).read_text())
            prov["cosign"] = {"state": "verified", "detail": "trust me"}
            (root / ob.PROVENANCE).write_text(json.dumps(prov))
        elif case == "other-digest":
            set_sigs(root, [entry("sha256:" + "2" * 64)])
        elif case == "other-repository":
            set_sigs(root, [entry(idx, reference="dp.example/containers/redis")])
        elif case == "other-key":
            set_sigs(root, [entry(idx, key=other_key(tmp_path))])
        elif case == "payload-altered":
            e = entry(idx)
            e["payload"] = base64.b64encode(cosign_payload(idx, "dp.example/containers/openbao") + b" ").decode()
            set_sigs(root, [e])
        elif case == "not-cosign-type":
            p = json.dumps({"critical": {"identity": {"docker-reference": "x/containers/openbao"},
                                         "image": {"docker-manifest-digest": idx}, "type": "other"}}).encode()
            set_sigs(root, [entry(idx, payload=p)])
        elif case == "critical-not-object":
            set_sigs(root, [entry(idx, payload=json.dumps({"critical": "x"}).encode())])
        else:
            (root / ob.SIGNATURE).write_text("not json")
    out = rebuilt(built, tmp_path, change)
    with pytest.raises(ob.BundleError) as e:
        ob.verify(out, tmp_path / "check", cosign_key=KEY)
    assert e.value.code == code, str(e.value)


def test_a_good_signature_among_bad_ones_is_enough(built, tmp_path):
    idx = index_of(built, tmp_path)
    out = rebuilt(built, tmp_path, lambda r: set_sigs(r, [entry("sha256:" + "3" * 64), entry(idx)]))
    assert ob.verify(out, tmp_path / "check", cosign_key=KEY)["trust"] == "signature"


@pytest.mark.parametrize("key", [None, "absent"])
def test_unpinned_tag_refused_without_the_collection_key(built, tmp_path, key):
    with pytest.raises(ob.BundleError) as e:
        ob.verify(built[0], tmp_path / "check", cosign_key=None if key is None else tmp_path / "absent.pub")
    assert e.value.code == "bundle-unsigned"


def test_known_tag_pin_holds_for_any_image_name(built, tmp_path):
    """Before, the pin was skipped when the image was not named
    .../containers/openbao (a mirror name chosen by whoever built it)."""
    def change(root):
        prov = json.loads((root / ob.PROVENANCE).read_text())
        prov["image"] = "mirror.example/openbao"
        (root / ob.PROVENANCE).write_text(json.dumps(prov))
    out = rebuilt(built, tmp_path, change, tag="2.7.2-5.4", version="2.7.2")
    with pytest.raises(ob.BundleError) as e:
        ob.verify(out, tmp_path / "check", cosign_key=KEY)
    assert e.value.code == "bundle-pin-mismatch"


def test_real_application_collection_signature_verifies_with_the_shipped_key(tmp_path):
    """Signature of dp.apps.rancher.io/containers/openbao:2.7.2-5.4 as the
    registry serves it (read on 08/11/2026), checked with
    config/openbao/application-collection-cosign.pub by openssl."""
    doc = json.loads((Path(__file__).parent / "fixtures" / "appco-openbao-2.7.2-5.4-cosign.json").read_text())
    sig = tmp_path / ob.SIGNATURE
    sig.write_text(json.dumps({"signatures": doc["signatures"]}))
    assert doc["index_digest"] == ob.KNOWN["2.7.2-5.4"]
    assert ob.check_signature(sig, doc["index_digest"], APPCO_KEY, tmp_path)
    with pytest.raises(ob.BundleError):
        ob.check_signature(sig, "sha256:" + "4" * 64, APPCO_KEY, tmp_path)
    with pytest.raises(ob.BundleError):
        ob.check_signature(sig, doc["index_digest"], KEY, tmp_path)


def test_build_refuses_an_unpinned_unsigned_tag(tmp_path):
    reg = make_registry(signed=False)
    try:
        src = op.Registry(reg.host, {"username": "svc", "password": "Pw-AppCo-777"}, plain_http=True)
        with pytest.raises(ob.BundleError) as e:
            ob.build(src, reg.host, ob.DEFAULT_REPOSITORY, "9.9.9-1", TPL, tmp_path, cosign_key=KEY)
        assert e.value.code == "bundle-unsigned" and not list(tmp_path.glob("*.tar.gz"))
    finally:
        reg.close()


def test_build_refuses_a_signature_that_would_not_install(tmp_path):
    reg = make_registry(signed=False)
    add_signature(reg, ob.DEFAULT_REPOSITORY, reg.index_digest, signature=cosign_sign(b"something else"))
    try:
        src = op.Registry(reg.host, {"username": "svc", "password": "Pw-AppCo-777"}, plain_http=True)
        with pytest.raises(ob.BundleError) as e:
            ob.build(src, reg.host, ob.DEFAULT_REPOSITORY, "9.9.9-1", TPL, tmp_path, cosign_key=KEY)
        assert e.value.code == "bundle-signature-bad" and not list(tmp_path.glob("*.tar.gz"))
    finally:
        reg.close()


def test_version_below_the_minimum_refused(built, tmp_path):
    """Review M3 of pass 2: an older OpenBao, even signed, is refused."""
    out = rebuilt(built, tmp_path, lambda r: None, tag="2.7.1-1", version="2.7.1")
    with pytest.raises(ob.BundleError) as e:
        ob.verify(out, tmp_path / "check", cosign_key=KEY)
    assert e.value.code == "bundle-version-too-old"


@pytest.mark.parametrize("a,b", [("2.7.2", "2.7.10"), ("2.7.2", "2.8.0"), ("2.7", "2.7.1"), ("1.99.9", "2.0.0")])
def test_version_order(a, b):
    assert ob.version_key(a) < ob.version_key(b)
