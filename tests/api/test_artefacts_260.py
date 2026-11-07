"""v2.6.0 : paquet d'artefacts (index, inspection, extraction vérifiée) et
confiance (signature à quatre états, décision permissive ou stricte, clés
de confiance), avec de vraies clés générées par ssh-keygen."""

import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin" / "lib"))
import artefacts as art  # noqa: E402
import self_update as su  # noqa: E402

LIB = Path(__file__).resolve().parents[2] / "bin" / "lib"
needs_keygen = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="ssh-keygen needed")

INDEX = {"name": "socle", "version": "2026.11", "description": "base",
         "items": [{"name": "podinfo", "kind": "chart", "version": "6.9.2", "chart": "charts/podinfo-6.9.2.tgz",
                    "values": {"replicaCount": 1}, "images": ["ghcr.io/stefanprodan/podinfo:6.9.2"]},
                   {"name": "ns", "kind": "manifest", "files": ["manifests/ns/ns.yaml"]}]}


def files_of(index=None):
    idx = json.dumps(index or INDEX).encode()
    return {"artefacts.yaml": idx, "charts/podinfo-6.9.2.tgz": b"chart-bytes",
            "manifests/ns/ns.yaml": b"kind: Namespace\n", "images/oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
            "images/index.json": b'{"manifests":[]}'}


def sums_of(files):
    return "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in sorted(files.items())).encode()


def make_package(dirpath, files=None, top="socle-2026.11", name=None, sums=None, first=None,
                 extra=(), sums_first=True):
    """Un paquet ; `extra` : membres ajoutés tels quels (TarInfo, données)."""
    files = dict(files_of() if files is None else files)
    sums = sums_of(files) if sums is None else sums
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as t:
        def add(n, data, kind=tarfile.REGTYPE, link=""):
            ti = tarfile.TarInfo(n)
            ti.type, ti.linkname = kind, link
            ti.size = len(data) if kind == tarfile.REGTYPE else 0
            t.addfile(ti, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        add(top, b"", tarfile.DIRTYPE)
        order = list(files.items())
        if first:
            add(f"{top}/{first}", files[first])
            order = [(n, d) for n, d in order if n != first]
        if sums_first:
            add(f"{top}/SHA256SUMS", sums)
        for n, d in order:
            add(f"{top}/{n}", d)
        if not sums_first:
            add(f"{top}/SHA256SUMS", sums)
        for args in extra:
            add(*args)
    p = Path(dirpath) / (name or f"{top}.tar.gz")
    p.write_bytes(gzip.compress(raw.getvalue(), mtime=0))
    return p


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
def test_a_good_index_is_read_and_later_kinds_are_kept_marked():
    doc = json.loads(json.dumps(INDEX))
    doc["items"].append({"name": "pol", "kind": "policy", "files": ["manifests/pol/p.yaml"]})
    idx, errors = art.check_index(doc, image_names=["ghcr.io/stefanprodan/podinfo:6.9.2"])
    assert errors == [] and idx["name"] == "socle" and idx["version"] == "2026.11"
    assert idx["items"][2] == {"name": "pol", "kind": "policy", "files": ["manifests/pol/p.yaml"], "later": True}
    assert "later" not in idx["items"][0]


@pytest.mark.parametrize("mutate,expected", [
    (lambda d: d.update(signed_by="x"), "unknown key: signed_by"),
    (lambda d: d["items"][0].update(extra=1), "items[0]: unknown key: extra"),
    (lambda d: d["items"].append(dict(d["items"][1])), "items[2].name: ns appears twice"),
    (lambda d: d.update(name="Socle"), "name: RFC 1123"),
    (lambda d: d.update(name="a" * 41), "name: RFC 1123"),
    (lambda d: d.update(version="v1"), "version:"),
    (lambda d: d["items"][0].update(kind="helm"), "items[0].kind"),
    (lambda d: d["items"][0].update(chart="../x.tgz"), "items[0].chart"),
    (lambda d: d["items"][0].update(values=[1]), "items[0].values"),
    (lambda d: d["items"][1].update(files=["manifests/other/x.yaml"]), "items[1].files"),
    (lambda d: d["items"][1].update(files=[]), "items[1].files"),
    (lambda d: d["items"][1].update(chart="charts/x.tgz"), "items[1]: chart and values are for a chart"),
    (lambda d: d["items"][0].update(files=["manifests/podinfo/a.yaml"]), "items[0]: files are for a manifest"),
    (lambda d: d["items"].append({"name": "imgs", "kind": "images"}), "items[2].images: one image at least"),
    (lambda d: d["items"][0].update(images="nginx"), "items[0].images: a list"),
    (lambda d: d["items"][0].update(images=["nginx:1.27"]), "nginx:1.27 is not in images/index.json"),
    (lambda d: d.update(items={}), "items: a list"),
])
def test_a_bad_index_is_refused_with_the_reason(mutate, expected):
    doc = json.loads(json.dumps(INDEX))
    mutate(doc)
    idx, errors = art.check_index(doc, image_names=["ghcr.io/stefanprodan/podinfo:6.9.2"])
    assert idx is None and any(expected in e for e in errors), errors


@pytest.mark.parametrize("version,ok", [("1.0.0", True), ("2026.11", True), ("2026.11.05", True),
                                        ("2026.11.05-2", True), ("2026.13", False), ("2026.1", False), ("2026.11.32", True),
                                        ("1.0", False), ("latest", False)])
def test_versions_are_semver_or_dates(version, ok):
    assert bool(art.VERSION_RE.match(version)) is ok


# ---------------------------------------------------------------------------
# Inspection et extraction
# ---------------------------------------------------------------------------
def test_a_clean_package_is_inspected_and_extracted(tmp_path):
    p = make_package(tmp_path)
    info = art.inspect_package(p)
    assert (info["name"], info["version"], info["top"], info["files"]) == ("socle", "2026.11", "socle-2026.11", 6)
    dest = tmp_path / "state" / "artefacts.d" / "socle-2026.11"
    got = []
    res = art.extract_verified(p, dest, on_bytes=got.append)
    assert res == {"files": 5, "bytes": sum(len(d) for d in files_of().values())} and sum(got) == res["bytes"]
    assert (dest / "charts" / "podinfo-6.9.2.tgz").read_bytes() == b"chart-bytes"
    assert oct((dest / "charts" / "podinfo-6.9.2.tgz").stat().st_mode & 0o777) == "0o600"
    assert oct(dest.stat().st_mode & 0o777) == "0o700"


def _dev():
    return (("socle-2026.11/dev", b"", tarfile.CHRTYPE),)


@pytest.mark.parametrize("kw,code", [
    (dict(extra=(("socle-2026.11/../x", b"evil"),)), "unsafe-member"),
    (dict(extra=(("/etc/x", b"evil"),)), "unsafe-member"),
    (dict(extra=(("socle-2026.11/l", b"", tarfile.SYMTYPE, "/etc/shadow"),)), "unsafe-member"),
    (dict(extra=(("socle-2026.11/l", b"", tarfile.SYMTYPE, "inside"),)), "unsafe-member"),
    (dict(extra=_dev()), "unsafe-member"),
    (dict(extra=(("other-1.0.0/x", b"x"),)), "two-roots"),
    (dict(top="socle-2026.12", name="socle-2026.12.tar.gz"), "root-mismatch"),
    (dict(name="socle-2026.12.tar.gz"), "root-mismatch"),
    (dict(sums_first=False), "sums-not-first"),
    (dict(first="artefacts.yaml"), "sums-not-first"),
    (dict(name="socle.tar.gz"), "not-a-package"),
    (dict(files={k: v for k, v in files_of().items() if k != "artefacts.yaml"}), "index-invalid"),
    (dict(files=dict(files_of(), **{"artefacts.yaml": b"{not json"})), "index-invalid"),
    (dict(files=files_of(dict(INDEX, extra=1))), "index-invalid"),
])
def test_a_malicious_or_broken_package_is_refused_by_inspection(tmp_path, kw, code):
    p = make_package(tmp_path, **kw)
    with pytest.raises(art.ArtefactError) as e:
        art.inspect_package(p)
    assert e.value.code == code, str(e.value)


def test_a_file_that_is_not_gzip_is_not_a_package(tmp_path):
    p = tmp_path / "socle-2026.11.tar.gz"
    p.write_bytes(b"garbage")
    for fn in (art.inspect_package, lambda x: art.extract_verified(x, tmp_path / "d")):
        with pytest.raises(art.ArtefactError) as e:
            fn(p)
        assert e.value.code == "not-a-package"
    assert not (tmp_path / "d").exists()


@pytest.mark.parametrize("kw,code", [
    (dict(extra=(("socle-2026.11/unlisted.yaml", b"x"),)), "sums-mismatch"),                  # non listé
    (dict(sums=sums_of(dict(files_of(), **{"images/index.json": b"other"}))), "sums-mismatch"),  # dernier membre faux
    (dict(sums=sums_of(dict(files_of(), **{"ghost": b"x"}))), "sums-mismatch"),               # listé, absent
    (dict(sums=b"not a sums file\n"), "sums-mismatch"),
    (dict(sums_first=False), "sums-not-first"),
    (dict(extra=(("socle-2026.11/../x", b"evil"),)), "unsafe-member"),
    (dict(extra=(("socle-2026.11/l", b"", tarfile.SYMTYPE, "inside"),)), "unsafe-member"),
    (dict(extra=_dev()), "unsafe-member"),
    (dict(extra=(("other-1.0.0/x", b"x"),)), "two-roots"),
])
def test_extraction_refuses_and_removes_the_partial_directory(tmp_path, kw, code):
    p = make_package(tmp_path, **kw)
    dest = tmp_path / "artefacts.d" / "socle-2026.11"
    with pytest.raises(art.ArtefactError) as e:
        art.extract_verified(p, dest)
    assert e.value.code == code, str(e.value)
    assert not dest.exists() and not (tmp_path / "x").exists()


def test_extraction_never_overwrites_an_existing_package(tmp_path):
    p = make_package(tmp_path)
    dest = tmp_path / "d"
    dest.mkdir()
    (dest / "keep").write_text("x")
    with pytest.raises(art.ArtefactError) as e:
        art.extract_verified(p, dest)
    assert e.value.code == "exists" and (dest / "keep").exists()


# ---------------------------------------------------------------------------
# Confiance
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen needed")
    d = tmp_path_factory.mktemp("keys")
    for k in ("client", "stranger"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", k, "-f", str(d / k)], check=True)
    pub = (d / "client.pub").read_text().split()
    (d / "signers").write_text(f'ops@client namespaces="{art.SIG_NAMESPACE}" {pub[0]} {pub[1]}\n')
    return d


def sign(keys, path, key="client", ns=art.SIG_NAMESPACE):
    sig = Path(str(path) + ".sig")
    sig.unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(keys / key), "-n", ns, str(path)], check=True)
    return sig


def fpr(keys, key):
    out = subprocess.run(["ssh-keygen", "-lf", str(keys / f"{key}.pub")], capture_output=True, text=True).stdout
    return out.split()[1]


@needs_keygen
@pytest.mark.parametrize("case,state,principal,key", [
    ("known", "valid", "ops@client", "client"),
    ("stranger", "unknown-key", None, "stranger"),
    ("other-file", "invalid", None, None),
    ("empty-sig", "invalid", None, None),
    ("garbage-sig", "invalid", None, None),
    ("unsigned", "unsigned", None, None),
    ("console-namespace", "invalid", None, None),
    ("console-namespace-stranger", "invalid", None, None),
    ("tampered", "invalid", None, None),
    ("no-signers", "unknown-key", None, "client"),
])
def test_the_four_trust_states(keys, tmp_path, case, state, principal, key):
    p = make_package(tmp_path)
    signers = keys / "signers"
    if case in ("known", "no-signers"):
        sig = sign(keys, p)
    elif case == "stranger":
        sig = sign(keys, p, "stranger")
    elif case == "other-file":
        other = tmp_path / "other"
        other.write_bytes(b"something else")
        sig = sign(keys, other).rename(str(p) + ".sig")
    elif case == "empty-sig":
        sig = Path(str(p) + ".sig")
        sig.write_bytes(b"")
    elif case == "garbage-sig":
        sig = Path(str(p) + ".sig")
        sig.write_text("-----BEGIN SSH SIGNATURE-----\nnope\n-----END SSH SIGNATURE-----\n")
    elif case == "unsigned":
        sig = Path(str(p) + ".sig")
    elif case == "console-namespace":
        sig = sign(keys, p, ns=su.SIG_NAMESPACE)
    elif case == "console-namespace-stranger":
        sig = sign(keys, p, "stranger", ns=su.SIG_NAMESPACE)
    elif case == "tampered":
        sig = sign(keys, p)
        data = bytearray(p.read_bytes())
        data[len(data) // 2] ^= 1
        p.write_bytes(bytes(data))
    if case == "no-signers":
        signers = tmp_path / "absent"
    trust = art.verify_trust(p, sig, signers)
    assert trust == {"state": state, "principal": principal, "key_fingerprint": fpr(keys, key) if key else None}


@needs_keygen
def test_a_key_restricted_to_the_console_namespace_does_not_sign_packages(keys, tmp_path):
    pub = (keys / "client.pub").read_text().split()
    signers = tmp_path / "signers"
    signers.write_text(f'ops@client namespaces="{su.SIG_NAMESPACE}" {pub[0]} {pub[1]}\n')
    p = make_package(tmp_path)
    assert art.verify_trust(p, sign(keys, p), signers)["state"] == "invalid"


def test_ssh_keygen_output_never_leaks_and_old_openssh_is_said(tmp_path):
    p = make_package(tmp_path)
    sig = Path(str(p) + ".sig")
    sig.write_text("x")
    signers = tmp_path / "s"
    signers.write_text("a ssh-ed25519 AAAA\n")

    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    old = lambda *a, **k: R(1, "", "unknown option -- Y\nusage: ssh-keygen")  # noqa: E731
    with pytest.raises(art.ArtefactError) as e:
        art.verify_trust(p, sig, signers, runner=old)
    assert e.value.code == "no-ssh-keygen"

    def missing(*a, **k):
        raise FileNotFoundError("ssh-keygen")
    with pytest.raises(art.ArtefactError) as e:
        art.verify_trust(p, sig, signers, runner=missing)
    assert e.value.code == "no-ssh-keygen"
    chatty = lambda cmd, **k: R(0, "secret-principal SHA256:" + "A" * 43 + " /home/x/raw output")  # noqa: E731
    t = art.verify_trust(p, sig, None, runner=chatty)
    assert t == {"state": "unknown-key", "principal": None, "key_fingerprint": "SHA256:" + "A" * 43}


DECIDE = [
    # état, strict, acquittement, accepté, code
    ("valid", False, None, True, "valid"),
    ("valid", True, None, True, "valid"),
    ("valid", False, "unsigned", True, "valid"),
    ("unknown-key", False, None, False, "acknowledge-unknown-key"),
    ("unknown-key", False, "unknown-key", True, "accepted-unknown-key"),
    ("unknown-key", False, "unsigned", False, "acknowledge-unknown-key"),
    ("unknown-key", True, "unknown-key", False, "strict-refuses-unknown-key"),
    ("unknown-key", True, None, False, "strict-refuses-unknown-key"),
    ("unsigned", False, None, False, "acknowledge-unsigned"),
    ("unsigned", False, "unsigned", True, "accepted-unsigned"),
    ("unsigned", False, "unknown-key", False, "acknowledge-unsigned"),
    ("unsigned", True, "unsigned", False, "strict-refuses-unsigned"),
    ("invalid", False, None, False, "invalid"),
    ("invalid", False, "unsigned", False, "invalid"),
    ("invalid", False, "unknown-key", False, "invalid"),
    ("invalid", True, None, False, "invalid"),
    ("sums-mismatch", False, "unsigned", False, "sums-mismatch"),
    ("sums-mismatch", True, None, False, "sums-mismatch"),
    ("unsigned", False, "yes", False, "acknowledge-invalid"),
    ("weird", False, None, False, "invalid"),
]


@pytest.mark.parametrize("state,strict,ack,accepted,code", DECIDE)
def test_decide(state, strict, ack, accepted, code):
    assert art.decide({"state": state}, strict, ack) == (accepted, code)


def test_the_journal_line_names_what_was_accepted_and_by_whom(tmp_path):
    assert (art.journal_line({"state": "unsigned"}, "socle", "2026.11", "alice")
            == "accepted UNSIGNED package socle-2026.11 after acknowledgement by alice")
    assert "UNKNOWN-KEY" in art.journal_line({"state": "unknown-key"}, "socle", "2026.11", "bob")
    t = art.write_trust(tmp_path, {"state": "unknown-key", "principal": None, "key_fingerprint": "SHA256:x"},
                        "bob", False, now=lambda: 0)
    assert json.loads((tmp_path / "trust.json").read_text()) == t == {
        "state": "unknown-key", "principal": None, "key_fingerprint": "SHA256:x", "person": "bob",
        "date": "1970-01-01T00:00:00Z", "mode": "permissive"}
    assert oct((tmp_path / "trust.json").stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------------------
# Clés de confiance et réglages
# ---------------------------------------------------------------------------
def rsa_pub(bits):
    n = (1 << (bits - 1)) | 1

    def s(b):
        return len(b).to_bytes(4, "big") + b
    blob = s(b"ssh-rsa") + s(b"\x01\x00\x01") + s(b"\x00" + n.to_bytes(bits // 8, "big"))
    return "ssh-rsa " + base64.b64encode(blob).decode()


@needs_keygen
def test_trusted_keys_get_the_artefacts_namespace_and_are_saved_0600(keys, tmp_path):
    pub = (keys / "client.pub").read_text().split()
    saved = art.save_signers(tmp_path, f"# client keys\n\nops@client {pub[0]} {pub[1]} laptop\n")
    assert saved == [{"principal": "ops@client", "key_type": "ssh-ed25519", "fingerprint": fpr(keys, "client"),
                      "comment": "laptop"}]
    p = art.signers_path(tmp_path)
    assert p.read_text() == f'ops@client namespaces="{art.SIG_NAMESPACE}" {pub[0]} {pub[1]} laptop\n'
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    pkg = make_package(tmp_path)
    assert art.verify_trust(pkg, sign(keys, pkg), p)["state"] == "valid"       # le fichier écrit sert tel quel


@pytest.mark.parametrize("line,match", [
    (rsa_pub(2048), "expected"),
    ("ops " + rsa_pub(2048), "RSA key of 2048 bits"),
    ("ops ssh-dss AAAAB3NzaC1kc3M=", "no supported key type"),
    ('ops cert-authority ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==', "option 'cert-authority'"),
    ('ops namespaces="harvester-ops-release" ' + rsa_pub(3072), "restricted to other namespaces"),
    ("ops ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x07ssh-rsa").decode(), "not of type ssh-ed25519"),
    ("ops ssh-ed25519 !!!", "not base64"),
    ("o$p ssh-ed25519 AAAA", "principal"),
])
def test_a_bad_trusted_key_line_is_refused_and_nothing_is_written(tmp_path, line, match):
    with pytest.raises(art.ArtefactError) as e:
        art.save_signers(tmp_path, line + "\n")
    assert e.value.code == "signers-invalid" and match in str(e.value)
    assert not art.signers_path(tmp_path).exists()


def test_an_rsa_key_of_3072_bits_is_accepted_with_its_options_kept():
    k = art.parse_signer_line('ops namespaces="harvester-artefacts,other",valid-after="20260101" ' + rsa_pub(3072))
    assert k["options"] == {"namespaces": "harvester-artefacts,other", "valid-after": "20260101"}
    assert k["line"].startswith('ops namespaces="harvester-artefacts,other",valid-after="20260101" ssh-rsa ')


@pytest.mark.parametrize("config,saved,strict,locked", [
    ({}, None, False, False),
    ({}, True, True, False),
    ({"artefacts": {"strict": True}}, None, True, True),
    ({"artefacts": {"strict": True}}, False, True, True),
    ({"artefacts": {"strict": "yes"}}, None, False, False),
])
def test_strict_mode_from_settings_or_locked_by_the_configuration(tmp_path, config, saved, strict, locked):
    if saved is not None:
        (tmp_path / "artefacts").mkdir()
        (tmp_path / "artefacts" / "settings.json").write_text(json.dumps({"strict": saved}))
    s = art.settings(tmp_path, config)
    assert (s["strict"], s["locked"], s["signers"]) == (strict, locked, [])


def test_strict_mode_cannot_be_turned_off_when_locked(tmp_path):
    cfg = {"artefacts": {"strict": True}}
    with pytest.raises(art.ArtefactError) as e:
        art.save_settings(tmp_path, False, cfg)
    assert e.value.code == "strict-locked"
    assert art.save_settings(tmp_path, True)["strict"] is True
    assert art.save_settings(tmp_path, False)["strict"] is False
    assert oct((tmp_path / "artefacts" / "settings.json").stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------------------
# Paquets rangés
# ---------------------------------------------------------------------------
def test_stored_packages_are_listed_loaded_and_removed(tmp_path):
    p = make_package(tmp_path)
    d = art.package_dir(tmp_path, "socle", "2026.11")
    art.extract_verified(p, d)
    art.write_trust(d, {"state": "unsigned"}, "alice", False, now=lambda: 0)
    (art.packages_dir(tmp_path) / "stray").mkdir()
    assert art.list_packages(tmp_path) == [{
        "name": "socle", "version": "2026.11", "description": "base", "items": 2, "trust": "unsigned",
        "principal": None, "imported_by": "alice", "imported_at": "1970-01-01T00:00:00Z"}]
    assert art.load_package(tmp_path, "socle", "2026.11")["trust"]["person"] == "alice"
    with pytest.raises(art.ArtefactError) as e:
        art.remove_package(tmp_path, "socle", "2026.11", used_by=["prod", "prod"])
    assert e.value.code == "package-in-use" and "prod" in str(e.value) and d.is_dir()
    assert art.remove_package(tmp_path, "socle", "2026.11") and not d.exists()
    for name, version in (("socle", "2026.11"), ("../x", "1.0.0"), ("socle", "../../x")):
        with pytest.raises(art.ArtefactError) as e:
            art.load_package(tmp_path, name, version)
        assert e.value.code == "not-found"


def test_the_module_needs_only_the_standard_library():
    code = (f"import sys; sys.path.insert(0, {str(LIB)!r}); import artefacts; "
            "bad = [m for m in sys.modules if m.split('.')[0] in ('yaml', 'requests', 'flask')]; "
            "assert not bad, bad")
    subprocess.run([sys.executable, "-I", "-S", "-c", code], check=True)


def test_inspect_does_not_trust_a_huge_index(tmp_path, monkeypatch):
    os.environ.pop("X", None)
    big = files_of()
    big["artefacts.yaml"] = b" " * ((1 << 22) + 1)
    with pytest.raises(art.ArtefactError) as e:
        art.inspect_package(make_package(tmp_path, files=big))
    assert e.value.code == "index-invalid"
