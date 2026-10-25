"""Prochaine version KVM, tâche 1 : en-tête qcow2 avant un envoi
(bin/lib/qcow2.py). En-têtes fabriqués à la main (toujours) et images
réelles créées par `qemu-img` quand il est présent (sautées sinon)."""

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import qcow2  # noqa: E402

GIB = 1 << 30


def make_header(version=3, size=10 * GIB, backing=b"", crypt=0, incompat=0, autoclear=0,
                extensions=(), header_length=112):
    """En-tête qcow2 v2/v3 minimal, extensions (type, données) ensuite, puis
    le nom du fichier de base s'il y en a un."""
    hl = 72 if version == 2 else header_length
    ext = b""
    for kind, data in extensions:
        ext += struct.pack(">II", kind, len(data)) + data + b"\x00" * (-len(data) % 8)
    ext += struct.pack(">II", 0, 0)
    backing_off = (hl + len(ext)) if backing else 0
    head = b"QFI\xfb" + struct.pack(">IQIIQIIQQIIQ", version, backing_off, len(backing), 16, size,
                                    crypt, 0, 0x30000, 0x10000, 1, 0, 0)
    if version == 3:
        head += struct.pack(">QQQII", incompat, 0, autoclear, 4, hl)
        head += b"\x00" * (hl - len(head))
    return head + ext + backing + b"\x00" * 512


HEADERS = [
    # (cas, octets, version, taille, fichier de base, données externes, chiffré, raisons)
    ("v2", make_header(2), 2, 10 * GIB, False, False, False, []),
    ("v3", make_header(3), 3, 10 * GIB, False, False, False, []),
    ("v3-backing", make_header(3, backing=b"/etc/shadow"), 3, 10 * GIB, True, False, False,
     [qcow2.REASON_BACKING]),
    ("v2-backing", make_header(2, backing=b"../../x.img"), 2, 10 * GIB, True, False, False,
     [qcow2.REASON_BACKING]),
    ("backing-format-ext-only", make_header(3, extensions=[(qcow2.EXT_BACKING_FORMAT, b"raw")]),
     3, 10 * GIB, True, False, False, [qcow2.REASON_BACKING]),
    ("data-file-bit", make_header(3, incompat=qcow2.INCOMPAT_DATA_FILE), 3, 10 * GIB, False,
     True, False, [qcow2.REASON_DATA_FILE]),
    ("data-file-ext", make_header(3, extensions=[(qcow2.EXT_DATA_FILE, b"/dev/sda")]), 3,
     10 * GIB, False, True, False, [qcow2.REASON_DATA_FILE]),
    ("raw-data-autoclear", make_header(3, autoclear=qcow2.AUTOCLEAR_RAW_DATA), 3, 10 * GIB,
     False, True, False, [qcow2.REASON_DATA_FILE]),
    ("aes", make_header(2, crypt=1), 2, 10 * GIB, False, False, True, [qcow2.REASON_ENCRYPTED]),
    ("luks", make_header(3, crypt=2, extensions=[(qcow2.EXT_CRYPTO, b"\x00" * 16)]), 3,
     10 * GIB, False, False, True, [qcow2.REASON_ENCRYPTED]),
    ("corrupt", make_header(3, incompat=qcow2.INCOMPAT_CORRUPT), 3, 10 * GIB, False, False,
     False, [qcow2.REASON_CORRUPT]),
    ("unknown-feature", make_header(3, incompat=1 << 40), 3, 10 * GIB, False, False, False,
     [qcow2.REASON_UNKNOWN_FEATURES]),
    ("dirty-ok", make_header(3, incompat=qcow2.INCOMPAT_DIRTY), 3, 10 * GIB, False, False, False,
     []),
    ("everything", make_header(3, backing=b"x", incompat=qcow2.INCOMPAT_DATA_FILE, crypt=2), 3,
     10 * GIB, True, True, True,
     [qcow2.REASON_BACKING, qcow2.REASON_DATA_FILE, qcow2.REASON_ENCRYPTED]),
]


@pytest.mark.parametrize("case,data,version,size,backing,data_file,encrypted,reasons", HEADERS,
                         ids=[h[0] for h in HEADERS])
def test_headers(case, data, version, size, backing, data_file, encrypted, reasons):
    h = qcow2.header(data)
    assert (h["version"], h["virtual_size"], h["backing_file"], h["external_data_file"],
            h["encrypted"]) == (version, size, backing, data_file, encrypted)
    assert qcow2.check_upload(data) == reasons


@pytest.mark.parametrize("data,error", [
    (b"", qcow2.REASON_NOT_QCOW2),
    (b"\x00" * 512, qcow2.REASON_NOT_QCOW2),
    (b"QFI\xfb" + b"\x00" * 10, qcow2.REASON_TRUNCATED),
    (make_header(3)[:90], qcow2.REASON_TRUNCATED),
    (make_header(3, header_length=80), qcow2.REASON_TRUNCATED),
    (b"QFI\xfb" + struct.pack(">I", 4) + b"\x00" * 200, qcow2.REASON_VERSION),
])
def test_header_errors(data, error):
    with pytest.raises(qcow2.Qcow2Error) as e:
        qcow2.header(data)
    assert str(e.value) == error


@pytest.mark.parametrize("data,reasons", [
    (b"", []),                                     # raw vide
    (b"\x00" * 65536, []),                         # raw
    (b"\x00" * 32768 + b"\x01CD001", []),          # ISO
    (b"QFI\xfb\x00\x00", [qcow2.REASON_TRUNCATED]),
    (b"QED\x00" + b"\x00" * 100, ["QED image: it can cite a host file; convert it to qcow2 or raw first"]),
    (b"KDMV" + b"\x00" * 100, ["VMDK image: it can cite a host file; convert it to qcow2 or raw first"]),
    (b"# Disk DescriptorFile\nparentFileNameHint=\"/etc/shadow\"\n",
     ["VMDK image: it can cite a host file; convert it to qcow2 or raw first"]),
])
def test_check_upload_other_formats(data, reasons):
    assert qcow2.check_upload(data) == reasons


QEMU_IMG = shutil.which("qemu-img")


def qemu_img(*args):
    subprocess.run(["qemu-img", *args], check=True, capture_output=True, timeout=60,
                   env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})


@pytest.mark.skipif(not QEMU_IMG, reason="qemu-img absent : images réelles non vérifiées")
@pytest.mark.parametrize("case,args,expect", [
    ("v3", ["-f", "qcow2", "{f}", "1G"], {"version": 3, "backing_file": False}),
    ("v2", ["-f", "qcow2", "-o", "compat=0.10", "{f}", "2G"], {"version": 2, "virtual_size": 2 * GIB}),
    ("backing", ["-f", "qcow2", "-F", "qcow2", "-b", "{base}", "{f}"], {"backing_file": True}),
    ("data-file", ["-f", "qcow2", "-o", "data_file={d}", "{f}", "1G"],
     {"external_data_file": True}),
    ("data-file-raw", ["-f", "qcow2", "-o", "data_file={d},data_file_raw=on", "{f}", "1G"],
     {"external_data_file": True}),
    ("luks", ["-f", "qcow2", "--object", "secret,id=s0,data=Fk7pw3Zq", "-o",
              "encrypt.format=luks,encrypt.key-secret=s0", "{f}", "64M"], {"encrypted": True}),
])
def test_real_images(tmp_path, case, args, expect):
    base = tmp_path / "base.qcow2"
    qemu_img("create", "-f", "qcow2", str(base), "1G")
    f = tmp_path / f"{case}.qcow2"
    fmt = {"f": str(f), "base": str(base), "d": str(tmp_path / "data.raw")}
    qemu_img("create", *[a.format(**fmt) for a in args])
    data = f.read_bytes()[:qcow2.HEADER_READ]
    h = qcow2.header(data)
    for key, value in expect.items():
        assert h[key] == value, key
    risky = expect.get("backing_file") or expect.get("external_data_file") or expect.get("encrypted")
    assert bool(qcow2.check_upload(data)) == bool(risky)
