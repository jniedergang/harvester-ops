"""Prochaine version KVM, tâche 1 : écrivain ISO 9660 + Joliet de l'ISO NoCloud
(bin/lib/iso9660.py). Lecture par notre lecteur, puis par `xorriso` et
`isoinfo` quand ils sont présents (sautés sinon)."""

import datetime
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import iso9660 as iso  # noqa: E402

SEED = iso.seed_files("#cloud-config\nusers:\n  - name: ju\n", "version: 1\nconfig: []\n",
                      "0b6e5c1e-1111-4a5b-9c3d-000000000001", "kvmtest-1")


def test_round_trip_seed():
    img = iso.build_iso(SEED)
    assert len(img) % iso.SECTOR == 0
    r = iso.read_iso(img)
    assert r["label"] == "cidata" and r["joliet"] is True
    assert r["files"] == SEED
    assert sorted(r["files"]) == ["meta-data", "network-config", "user-data"]


def test_label_in_primary_descriptor():
    img = iso.build_iso(SEED)
    pvd = img[16 * 2048:17 * 2048]
    assert pvd[0] == 1 and pvd[1:6] == b"CD001"
    assert pvd[40:72].rstrip(b" ") == b"cidata"
    svd = img[17 * 2048:18 * 2048]
    assert svd[0] == 2 and svd[88:91] == b"%/E"
    assert svd[40:52].decode("utf-16-be") == "cidata"


def test_joliet_names_and_level1_fallback():
    r = iso.read_iso(iso.build_iso(SEED))
    assert set(r["files"]) == {"user-data", "meta-data", "network-config"}
    assert set(r["primary_files"]) == {"USER_DAT", "META_DAT", "NETWORK_"}
    assert r["primary_files"]["USER_DAT"] == SEED["user-data"]


def test_without_joliet_only_level1_names():
    r = iso.read_iso(iso.build_iso(SEED, joliet=False))
    assert r["joliet"] is False
    assert "user-data" not in r["files"]


def test_reproducible_and_dated():
    a = iso.build_iso(SEED)
    assert a == iso.build_iso(SEED)
    b = iso.build_iso(SEED, date=datetime.datetime(2026, 10, 24, 12, 0, 0))
    assert a != b
    assert b[16 * 2048 + 813:16 * 2048 + 829] == b"2026102412000000"
    assert iso.build_iso(SEED, date=1792843200) == iso.build_iso(
        SEED, date=datetime.datetime.fromtimestamp(1792843200, tz=datetime.timezone.utc))


def test_source_date_epoch(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1792843200")
    a = iso.build_iso(SEED)
    monkeypatch.delenv("SOURCE_DATE_EPOCH")
    assert a != iso.build_iso(SEED)
    assert a == iso.build_iso(SEED, date=1792843200)


def test_utf8_empty_and_large_files():
    files = {"user-data": "#cloud-config\n# Zoé, 日本, ☃ <&>'\"\n".encode("utf-8"),
             "meta-data": b"", "big": bytes(range(256)) * 8192 + b"tail"}
    r = iso.read_iso(iso.build_iso(files))
    assert r["files"] == files
    assert len(files["big"]) > 2 * 1024 * 1024 - 1


def test_level1_name_collisions():
    files = {"network-config": b"a", "network-config2": b"b", "network.cfg": b"c"}
    r = iso.read_iso(iso.build_iso(files))
    assert r["files"] == files
    assert len(set(r["primary_files"])) == 3


@pytest.mark.parametrize("files,label", [
    ({}, "cidata"),
    ({"a/b": b"x"}, "cidata"),
    ({"..": b"x"}, "cidata"),
    ({"a;1": b"x"}, "cidata"),
    ({"a\nb": b"x"}, "cidata"),
    ({"x" * 65: b"x"}, "cidata"),
    ({"user-data": "text"}, "cidata"),
    ({"user-data": b"x"}, "ci data"),
    ({"user-data": b"x"}, ""),
    ({"user-data": b"x"}, "x" * 33),
    ({f"f{i}": b"" for i in range(51)}, "cidata"),
])
def test_build_errors(files, label):
    with pytest.raises(iso.IsoError):
        iso.build_iso(files, label=label)


@pytest.mark.parametrize("data", [b"", b"\x00" * 40000, bytes(18 * 2048)])
def test_read_errors(data):
    with pytest.raises(iso.IsoError):
        iso.read_iso(data)


def test_read_rejects_record_outside_image():
    img = bytearray(iso.build_iso(SEED))
    # étendue du répertoire racine du primaire hors de l'image
    off = 16 * 2048 + 156 + 2
    img[off:off + 4] = (10 ** 6).to_bytes(4, "little")
    with pytest.raises(iso.IsoError):
        iso.read_iso(bytes(img))


def test_seed_files_meta_data_is_safe_json():
    files = iso.seed_files(None, None, "i-1", "host.example")
    assert files == {"meta-data": b'{\n  "instance-id": "i-1",\n  "local-hostname": "host.example"\n}\n',
                     "user-data": b""}
    with_vendor = iso.seed_files("u", "n", "i-1", "h", vendor_data="#cloud-config\n")
    assert sorted(with_vendor) == ["meta-data", "network-config", "user-data", "vendor-data"]


@pytest.mark.parametrize("instance_id,hostname", [
    ("i-1\nlocal-hostname: evil", "h"), ("", "h"), ("i 1", "h"),
    ("i-1", "h\nx"), ("i-1", "-h"), ("i-1", "h_x"), ("i-1", "a" * 64), ("i-1", ""),
])
def test_seed_files_refuses_hostile_identity(instance_id, hostname):
    with pytest.raises(iso.IsoError):
        iso.seed_files("u", None, instance_id, hostname)


def test_seed_files_refuses_non_text():
    with pytest.raises(iso.IsoError):
        iso.seed_files(123, None, "i-1", "h")


@pytest.mark.skipif(not shutil.which("xorriso"), reason="xorriso absent : lecture externe sautée")
def test_read_by_xorriso(tmp_path):
    f = tmp_path / "seed.iso"
    f.write_bytes(iso.build_iso(SEED))
    r = subprocess.run(["xorriso", "-indev", str(f), "-find", "/", "-type", "f"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    names = sorted(line.strip().strip("'") for line in r.stdout.splitlines() if line.strip())
    assert names == ["/meta-data", "/network-config", "/user-data"]
    assert "Volume id    : 'cidata'" in r.stderr
    out = tmp_path / "ud"
    r = subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(f), "-extract", "/user-data",
                        str(out)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() == SEED["user-data"]


@pytest.mark.skipif(not shutil.which("isoinfo"), reason="isoinfo absent : lecture externe sautée")
def test_read_by_isoinfo(tmp_path):
    f = tmp_path / "seed.iso"
    f.write_bytes(iso.build_iso(SEED))
    r = subprocess.run(["isoinfo", "-J", "-l", "-i", str(f)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    for name in ("user-data", "meta-data", "network-config"):
        assert name in r.stdout
    r = subprocess.run(["isoinfo", "-d", "-i", str(f)], capture_output=True, text=True, timeout=60)
    assert "Volume id: cidata" in r.stdout
    assert "Joliet with UCS level 3 found" in r.stdout
    r = subprocess.run(["isoinfo", "-J", "-x", "/meta-data", "-i", str(f)], capture_output=True,
                       timeout=60)
    assert r.stdout == SEED["meta-data"]
