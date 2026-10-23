"""v1.96.0 : déclarations des hôtes KVM (bin/lib/kvm_decl.py)."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import kvm_decl  # noqa: E402

GOOD = {"name": "kvm-paris-01", "address": "10.0.0.21", "user": "virtops",
        "ssh_key": "ssh/kvm-paris-01_id", "known_hosts": "ssh/kvm-paris-01_known"}


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.mark.parametrize("patch,why", [
    ({"name": "../etc"}, "invalid or missing name"),
    ({"name": ".hidden"}, "invalid or missing name"),
    ({"name": "a/b"}, "invalid or missing name"),
    ({"name": "x" * 62}, "invalid or missing name"),
    ({"name": None}, "invalid or missing name"),
    ({"address": ""}, "empty address"),
    ({"address": "   "}, "empty address"),
    ({"address": "-oProxyCommand=id"}, "invalid address"),
    ({"address": "a b"}, "invalid address"),
    ({"port": 0}, "port must be between 1 and 65535"),
    ({"port": 65536}, "port must be between 1 and 65535"),
    ({"port": "22"}, "port must be between 1 and 65535"),
    ({"port": True}, "port must be between 1 and 65535"),
    ({"user": ""}, "invalid or missing user"),
    ({"user": "-l root"}, "invalid or missing user"),
    ({"ssh_key": ""}, "missing ssh_key"),
    ({"known_hosts": None}, "missing known_hosts"),
    ({"uri": "qemu:///session"}, "uri must be"),
    ({"uri": "xen:///system"}, "uri must be"),
    ({"uri": "test:///default"}, "uri must be"),
    ({"labels": ["a"]}, "labels must map"),
    ({"labels": {"site": 3}}, "labels must map"),
])
def test_check_decl_refuses(patch, why):
    assert why in kvm_decl.check_decl(kvm_decl.normalize({**GOOD, **patch}))


@pytest.mark.parametrize("patch", [
    {}, {"port": 2222}, {"port": 1}, {"port": 65535}, {"uri": "qemu+ssh://h/system"},
    {"uri": "qemu+tls://h/system"}, {"address": "kvm1.example.com"}, {"address": "fe80::1"},
    {"labels": {"site": "paris", "groupe": "prod"}},
])
def test_check_decl_accepts(patch):
    assert kvm_decl.check_decl(kvm_decl.normalize({**GOOD, **patch})) is None


def test_check_decl_not_a_mapping():
    assert kvm_decl.check_decl(["x"]) == "not a mapping"


def test_write_load_roundtrip_relative_paths_and_permissions(tmp_path):
    key = kvm_decl.write_key(tmp_path, GOOD["name"], b"-----BEGIN KEY-----\nabc")
    kh = kvm_decl.write_known_hosts(tmp_path, GOOD["name"], ["10.0.0.21 ssh-ed25519 AAAA"])
    # chemins absolus sous l'état : rangés relatifs
    path = kvm_decl.write(tmp_path, {**GOOD, "ssh_key": str(tmp_path / key),
                                     "known_hosts": str(tmp_path / kh), "labels": {"site": "paris"}})
    doc = json.loads(path.read_text())
    assert doc["ssh_key"] == "ssh/kvm-paris-01_id" and doc["known_hosts"] == "ssh/kvm-paris-01_known"
    assert doc["port"] == 22 and doc["uri"] == "qemu:///system"
    assert mode(path) == 0o600 and mode(path.parent) == 0o700
    assert mode(tmp_path / key) == 0o600 and mode(tmp_path / "ssh") == 0o700
    assert (tmp_path / key).read_text().endswith("abc\n")       # fin de ligne ajoutée
    assert (tmp_path / kh).read_text() == "10.0.0.21 ssh-ed25519 AAAA\n"
    loaded = kvm_decl.load(tmp_path, GOOD["name"])
    assert loaded == {**GOOD, "port": 22, "uri": "qemu:///system", "labels": {"site": "paris"}}
    assert kvm_decl.resolve(tmp_path, loaded["ssh_key"]) == tmp_path / key
    # aucun fichier temporaire laissé par l'écriture atomique
    assert sorted(p.name for p in path.parent.iterdir()) == ["kvm-paris-01.yaml"]


def test_write_keeps_operator_absolute_path_outside_state(tmp_path):
    kvm_decl.write(tmp_path, {**GOOD, "ssh_key": "/etc/harvester-ops/keys/k"})
    assert kvm_decl.load(tmp_path, GOOD["name"])["ssh_key"] == "/etc/harvester-ops/keys/k"


@pytest.mark.parametrize("path", ["../outside", "ssh/../../etc/passwd", "..", ""])
def test_resolve_and_write_refuse_traversal(tmp_path, path):
    with pytest.raises(kvm_decl.DeclError):
        kvm_decl.resolve(tmp_path, path)
    if path:
        with pytest.raises(kvm_decl.DeclError):
            kvm_decl.write(tmp_path, {**GOOD, "ssh_key": path})
    assert not (tmp_path / "kvm.d").exists()


@pytest.mark.parametrize("name", ["../x", "a/b", "", ".x", None])
def test_invalid_names_never_touch_the_disk(tmp_path, name):
    for fn in (lambda: kvm_decl.load(tmp_path, name), lambda: kvm_decl.remove(tmp_path, name),
               lambda: kvm_decl.write_key(tmp_path, name, "k"),
               lambda: kvm_decl.write_known_hosts(tmp_path, name, ["l"]),
               lambda: kvm_decl.write(tmp_path, {**GOOD, "name": name})):
        with pytest.raises(kvm_decl.DeclError):
            fn()
    assert list(tmp_path.iterdir()) == []


def test_write_refuses_invalid_decl(tmp_path):
    with pytest.raises(kvm_decl.DeclError, match="port"):
        kvm_decl.write(tmp_path, {**GOOD, "port": 70000})


def test_load_missing_and_mismatched(tmp_path):
    with pytest.raises(FileNotFoundError):
        kvm_decl.load(tmp_path, "nope")
    kvm_decl.write(tmp_path, GOOD)
    other = tmp_path / "kvm.d" / "other.yaml"
    other.write_text((tmp_path / "kvm.d" / "kvm-paris-01.yaml").read_text())
    with pytest.raises(kvm_decl.DeclError, match="does not match"):
        kvm_decl.load(tmp_path, "other")


def test_load_all_skips_bad_files_and_sorts(tmp_path):
    kvm_decl.write(tmp_path, {**GOOD, "name": "b"})
    kvm_decl.write(tmp_path, {**GOOD, "name": "a"})
    (tmp_path / "kvm.d" / "broken.yaml").write_text("{not json")
    (tmp_path / "kvm.d" / "badport.yaml").write_text(json.dumps({**GOOD, "name": "badport", "port": 0}))
    warns = []
    assert [d["name"] for d in kvm_decl.load_all(tmp_path, warn=warns.append)] == ["a", "b"]
    assert len(warns) == 2 and any("badport" in w for w in warns)
    assert kvm_decl.load_all(tmp_path / "absent") == []


def test_load_reads_yaml_written_by_hand(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "kvm.d").mkdir()
    (tmp_path / "kvm.d" / "h1.yaml").write_text(
        "name: h1\naddress: 10.0.0.1\nuser: root\nssh_key: ssh/h1_id\n"
        "known_hosts: ssh/h1_known\nlabels: {site: paris}\n")
    assert kvm_decl.load(tmp_path, "h1")["labels"] == {"site": "paris"}


def test_remove_deletes_decl_key_pub_and_known_hosts(tmp_path):
    kvm_decl.write_key(tmp_path, "h1", "k")
    (tmp_path / "ssh" / "h1_id.pub").write_text("pub")
    kvm_decl.write_known_hosts(tmp_path, "h1", ["l"])
    kvm_decl.write_key(tmp_path, "h2", "k")
    kvm_decl.write(tmp_path, {**GOOD, "name": "h1", "ssh_key": "ssh/h1_id", "known_hosts": "ssh/h1_known"})
    assert kvm_decl.remove(tmp_path, "h1") is True
    assert sorted(p.name for p in (tmp_path / "ssh").iterdir()) == ["h2_id"]
    assert not (tmp_path / "kvm.d" / "h1.yaml").exists()
    assert kvm_decl.remove(tmp_path, "h1") is False


def test_remove_never_deletes_outside_state(tmp_path):
    outside = tmp_path / "operator_key"
    outside.write_text("k")
    state = tmp_path / "state"
    kvm_decl.write(state, {**GOOD, "ssh_key": str(outside)})
    assert kvm_decl.remove(state, GOOD["name"]) is True
    assert outside.exists()


# --- relevé des empreintes ----------------------------------------------

KEYSCAN = ("# 10.0.0.21:22 SSH-2.0-OpenSSH_9.6\n"
           "10.0.0.21 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEDKEY\n"
           "10.0.0.21 ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIECDSA\n")
FPS = {"ssh-ed25519": "256 SHA256:8gT5OWW2NYNipdMYH7US+nIEmhFuR/oWpsSingGgDJk 10.0.0.21 (ED25519)\n",
       "ecdsa-sha2-nistp256": "256 SHA256:UueQADEeOOneMBDPJF8Dj56EQZDYwCzt2zh0M7qvZyU 10.0.0.21 (ECDSA)\n"}


class ScanRunner:
    def __init__(self, keyscan=KEYSCAN, keygen_rc=0, timeout=False):
        self.keyscan, self.keygen_rc, self.timeout, self.calls = keyscan, keygen_rc, timeout, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[0] == "ssh-keyscan":
            if self.timeout:
                raise subprocess.TimeoutExpired(argv, 15)
            return SimpleNamespace(returncode=0, stdout=self.keyscan, stderr="")
        ktype = kw["input"].split()[1]
        return SimpleNamespace(returncode=self.keygen_rc, stdout=FPS.get(ktype, ""), stderr="")


def test_scan_host_key_lists_types_lines_and_fingerprints():
    r = ScanRunner()
    keys = kvm_decl.scan_host_key("10.0.0.21", 2222, runner=r)
    assert [(k["type"], k["fingerprint"]) for k in keys] == [
        ("ssh-ed25519", "SHA256:8gT5OWW2NYNipdMYH7US+nIEmhFuR/oWpsSingGgDJk"),
        ("ecdsa-sha2-nistp256", "SHA256:UueQADEeOOneMBDPJF8Dj56EQZDYwCzt2zh0M7qvZyU")]
    assert keys[0]["line"] == "10.0.0.21 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEDKEY"
    assert r.calls[0][0] == ["ssh-keyscan", "-T", "10", "-p", "2222", "--", "10.0.0.21"]
    assert r.calls[1][0] == ["ssh-keygen", "-l", "-E", "sha256", "-f", "-"]


@pytest.mark.parametrize("runner,address,port,match", [
    (ScanRunner(keyscan=""), "10.0.0.21", 22, "no host key"),
    (ScanRunner(keygen_rc=1), "10.0.0.21", 22, "no host key"),
    (ScanRunner(timeout=True), "10.0.0.21", 22, "timed out"),
    (ScanRunner(), "-oProxyCommand=id", 22, "invalid address"),
    (ScanRunner(), "10.0.0.21", 0, "port"),
])
def test_scan_host_key_errors(runner, address, port, match):
    with pytest.raises(kvm_decl.DeclError, match=match):
        kvm_decl.scan_host_key(address, port, runner=runner)
