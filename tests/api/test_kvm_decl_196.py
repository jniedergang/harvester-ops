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


def named(name, **extra):
    """GOOD sous un autre nom, chemins dérivés du nom."""
    return {**GOOD, "name": name, "ssh_key": kvm_decl.key_rel(name),
            "known_hosts": kvm_decl.known_hosts_rel(name), **extra}


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
    ({"ssh_key": ""}, "ssh_key must be ssh/kvm-paris-01_id"),
    ({"known_hosts": None}, "known_hosts must be ssh/kvm-paris-01_known"),
    # la clé et l'empreinte sont TOUJOURS les chemins dérivés du nom
    ({"ssh_key": "actions.db"}, "ssh_key must be"),
    ({"ssh_key": "kvm.d/other.yaml"}, "ssh_key must be"),
    ({"ssh_key": "ssh/h2_id"}, "ssh_key must be"),
    ({"ssh_key": "/home/x/.ssh/id_ed25519"}, "ssh_key must be"),
    ({"ssh_key": "ssh/kvm-paris-01_known"}, "ssh_key must be"),
    ({"known_hosts": "actions.db"}, "known_hosts must be"),
    ({"known_hosts": "ssh/h2_known"}, "known_hosts must be"),
    ({"known_hosts": "/etc/ssh/ssh_known_hosts"}, "known_hosts must be"),
    ({"known_hosts": "ssh/../ssh/kvm-paris-01_known"}, "known_hosts must be"),
    ({"labels": ["a"]}, "labels must map"),
    ({"labels": {"site": 3}}, "labels must map"),
])
def test_check_decl_refuses(patch, why):
    assert why in kvm_decl.check_decl(kvm_decl.normalize({**GOOD, **patch}))


@pytest.mark.parametrize("patch", [
    {}, {"port": 2222}, {"port": 1}, {"port": 65535}, {"address": "kvm1.example.com"},
    {"address": "fe80::1"}, {"labels": {"site": "paris", "groupe": "prod"}},
])
def test_check_decl_accepts(patch):
    assert kvm_decl.check_decl(kvm_decl.normalize({**GOOD, **patch})) is None


# l'URI part telle quelle à virsh sur l'hôte : liste blanche de transports,
# aucune requête (`ext` + `command=` lancerait n'importe quel programme)
@pytest.mark.parametrize("uri,accepted", [
    ("qemu:///system", True),
    ("qemu+ssh://h/system", True),
    ("qemu+ssh://root@h:2222/system", True),
    ("qemu+tls://h/system", True),
    ("qemu+tcp://h/system", True),
    ("qemu+unix:///system", True),
    ("qemu+ext:///system?command=/tmp/x", False),
    ("qemu+ext:///system", False),
    ("qemu+libssh2://h/system", False),
    ("qemu+libssh://h/system?known_hosts=/x", False),
    ("qemu+ssh://h/system?command=/tmp/x", False),
    ("qemu+ssh://h/system?keyfile=/root/.ssh/id", False),
    ("qemu+unix:///system?socket=/tmp/s", False),
    ("qemu:///system?x=1", False),
    ("qemu:///session", False),
    ("xen:///system", False),
    ("test:///default", False),
    ("qemu+ssh://h/sys tem", False),
    ("", False),
    (None, False),
])
def test_check_decl_uri_whitelist(uri, accepted):
    why = kvm_decl.check_decl({**GOOD, "uri": uri})
    assert (why is None) is accepted, why
    if not accepted:
        assert why.startswith("uri must be")


def test_check_decl_not_a_mapping():
    assert kvm_decl.check_decl(["x"]) == "not a mapping"


@pytest.mark.parametrize("lines", ["10.0.0.21 ssh-ed25519 AAAA", b"10.0.0.21 ssh-ed25519 AAAA"])
def test_write_known_hosts_refuses_a_string(tmp_path, lines):
    # une chaîne serait écrite un caractère par ligne : refusée, rien d'écrit
    with pytest.raises(TypeError):
        kvm_decl.write_known_hosts(tmp_path, GOOD["name"], lines)
    assert not (tmp_path / kvm_decl.known_hosts_rel(GOOD["name"])).exists()


@pytest.mark.parametrize("lines,why", [
    (["10.0.0.21 ssh-ed25519"], "hosts keytype key"),
    ([""], "hosts keytype key"),
    (["10.0.0.21 ssh-ed25519 AAAA\nevil.example ssh-ed25519 BBBB"], "line break"),
    (["10.0.0.21 ssh-ed25519 AAAA\r"], "line break"),
    (["@cert-authority *.example ssh-ed25519 AAAA"], "markers"),
    (["@revoked 10.0.0.21 ssh-ed25519 AAAA"], "markers"),
    (["10.0.0.21 ssh-ed25519 AAAA", None], "must be a string"),
])
def test_write_known_hosts_validates_each_line(tmp_path, lines, why):
    with pytest.raises(kvm_decl.DeclError, match=why):
        kvm_decl.write_known_hosts(tmp_path, GOOD["name"], lines)
    assert not (tmp_path / kvm_decl.known_hosts_rel(GOOD["name"])).exists()


def test_write_known_hosts_accepts_one_trailing_newline(tmp_path):
    kvm_decl.write_known_hosts(tmp_path, "h1", ["[10.0.0.21]:2222 ssh-ed25519 AAAA\n",
                                               "10.0.0.21 ecdsa-sha2-nistp256 BBBB c"])
    assert (tmp_path / "ssh" / "h1_known").read_text() == (
        "[10.0.0.21]:2222 ssh-ed25519 AAAA\n10.0.0.21 ecdsa-sha2-nistp256 BBBB c\n")


def test_write_load_roundtrip_relative_paths_and_permissions(tmp_path):
    key = kvm_decl.write_key(tmp_path, GOOD["name"], b"-----BEGIN KEY-----\nabc")
    kh = kvm_decl.write_known_hosts(tmp_path, GOOD["name"], ["10.0.0.21 ssh-ed25519 AAAA"])
    path = kvm_decl.write(tmp_path, {**GOOD, "ssh_key": key, "known_hosts": kh,
                                     "labels": {"site": "paris"}})
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


@pytest.mark.parametrize("path", ["/etc/harvester-ops/keys/k", "ssh/kvm-paris-01_id-abs"])
def test_write_refuses_any_other_key_path(tmp_path, path):
    # un chemin absolu, même sous l'état, n'est plus rangé tel quel
    for p in (path, str(tmp_path / kvm_decl.key_rel(GOOD["name"]))):
        with pytest.raises(kvm_decl.DeclError, match="ssh_key must be"):
            kvm_decl.write(tmp_path, {**GOOD, "ssh_key": p})
    assert not (tmp_path / "kvm.d").exists()


@pytest.mark.parametrize("path", ["../outside", "ssh/../../etc/passwd", "..", "", "/etc/passwd"])
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
    kvm_decl.write(tmp_path, named("b"))
    kvm_decl.write(tmp_path, named("a"))
    (tmp_path / "kvm.d" / "broken.yaml").write_text("{not json")
    (tmp_path / "kvm.d" / "badport.yaml").write_text(json.dumps(named("badport", port=0)))
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


def test_remove_deletes_decl_key_and_known_hosts_only(tmp_path):
    kvm_decl.write_key(tmp_path, "h1", "k")
    kvm_decl.write_known_hosts(tmp_path, "h1", ["10.0.0.1 ssh-ed25519 AAAA"])
    kvm_decl.write_key(tmp_path, "h2", "k")
    kvm_decl.write(tmp_path, {**GOOD, "name": "h1", "ssh_key": "ssh/h1_id", "known_hosts": "ssh/h1_known"})
    assert kvm_decl.remove(tmp_path, "h1") is True
    assert sorted(p.name for p in (tmp_path / "ssh").iterdir()) == ["h2_id"]
    assert not (tmp_path / "kvm.d" / "h1.yaml").exists()
    assert kvm_decl.remove(tmp_path, "h1") is False


# une déclaration forgée à la main (le fichier n'est pas relu par check_decl
# au retrait) ne fait effacer QUE les deux chemins dérivés du nom
@pytest.mark.parametrize("field,value", [
    ("ssh_key", "actions.db"), ("ssh_key", "kvm.d/other.yaml"), ("ssh_key", "ssh/h2_id"),
    ("ssh_key", "/home/x/.ssh/id_ed25519"), ("known_hosts", "ssh/h2_known"),
    ("known_hosts", "notes.db"),
])
def test_remove_never_touches_other_files(tmp_path, field, value):
    state = tmp_path / "state"
    victims = {"actions.db", "notes.db", "kvm.d/other.yaml", "ssh/h2_id", "ssh/h2_known"}
    for rel in victims:
        (state / rel).parent.mkdir(parents=True, exist_ok=True)
        (state / rel).write_text("keep")
    outside = tmp_path / "home_x_key"
    outside.write_text("keep")
    if value.startswith("/"):
        value = str(outside)
    doc = {**GOOD, "name": "h1", "ssh_key": "ssh/h1_id", "known_hosts": "ssh/h1_known", field: value}
    (state / "kvm.d" / "h1.yaml").write_text(json.dumps(doc))
    assert kvm_decl.remove(state, "h1") is True
    assert all((state / rel).read_text() == "keep" for rel in victims) and outside.exists()
    assert not (state / "kvm.d" / "h1.yaml").exists()


def test_remove_tolerates_missing_files_and_deletes_decl_first(tmp_path, monkeypatch):
    # clé et empreinte déjà absentes : pas d'erreur
    kvm_decl.write(tmp_path, {**GOOD, "name": "h1", "ssh_key": "ssh/h1_id", "known_hosts": "ssh/h1_known"})
    assert kvm_decl.remove(tmp_path, "h1") is True
    # ordre : la déclaration part avant la clé et l'empreinte
    kvm_decl.write_key(tmp_path, "h1", "k")
    kvm_decl.write_known_hosts(tmp_path, "h1", ["10.0.0.1 ssh-ed25519 AAAA"])
    kvm_decl.write(tmp_path, {**GOOD, "name": "h1", "ssh_key": "ssh/h1_id", "known_hosts": "ssh/h1_known"})
    order, real = [], Path.unlink

    def spy(self, missing_ok=False):
        order.append(self.name)
        return real(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", spy)
    assert kvm_decl.remove(tmp_path, "h1") is True
    assert order == ["h1.yaml", "h1_id", "h1_known"]


# --- relevé des empreintes ----------------------------------------------

KEYSCAN = ("# 10.0.0.21:22 SSH-2.0-OpenSSH_9.6\n"
           "10.0.0.21 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEDKEY\n"
           "10.0.0.21 ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIECDSA\n")
FPS = {"ssh-ed25519": "256 SHA256:8gT5OWW2NYNipdMYH7US+nIEmhFuR/oWpsSingGgDJk 10.0.0.21 (ED25519)\n",
       "ecdsa-sha2-nistp256": "256 SHA256:UueQADEeOOneMBDPJF8Dj56EQZDYwCzt2zh0M7qvZyU 10.0.0.21 (ECDSA)\n"}


class ScanRunner:
    def __init__(self, keyscan=KEYSCAN, keygen_rc=0, timeout=False, missing=None):
        self.keyscan, self.keygen_rc, self.timeout, self.calls = keyscan, keygen_rc, timeout, []
        self.missing = missing                # programme absent (FileNotFoundError)

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[0] == self.missing:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
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
    # hors du port 22, ssh cherche `[adresse]:port` dans le fichier épinglé
    assert keys[0]["line"] == "[10.0.0.21]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEDKEY"
    assert r.calls[0][0] == ["ssh-keyscan", "-T", "10", "-p", "2222", "--", "10.0.0.21"]
    assert r.calls[0][1]["stdin"] is subprocess.DEVNULL
    assert r.calls[1][0] == ["ssh-keygen", "-l", "-E", "sha256", "-f", "-"]


@pytest.mark.parametrize("port,scanned,line", [
    (22, "10.0.0.21", "10.0.0.21 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEDKEY"),
    (2222, "[10.0.0.21]:2222", "[10.0.0.21]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEDKEY"),
    (2222, "10.0.0.21", "[10.0.0.21]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEDKEY"),
])
def test_scan_host_key_pinned_line_form(port, scanned, line):
    # le relevé réel écrit déjà `[adresse]:port` ; la ligne épinglée ne
    # dépend pas de lui (commentaire final retiré aussi)
    keyscan = f"{scanned} ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEDKEY trailing-comment\n"
    keys = kvm_decl.scan_host_key("10.0.0.21", port, runner=ScanRunner(keyscan=keyscan))
    assert [k["line"] for k in keys] == [line]


@pytest.mark.parametrize("runner,address,port,match", [
    (ScanRunner(), "-oProxyCommand=id", 22, "invalid address"),
    (ScanRunner(), "10.0.0.21", 0, "port"),
])
def test_scan_host_key_refuses_bad_input(runner, address, port, match):
    with pytest.raises(kvm_decl.DeclError, match=match):
        kvm_decl.scan_host_key(address, port, runner=runner)
    assert runner.calls == []


@pytest.mark.parametrize("runner,kind,match", [
    (ScanRunner(keyscan=""), "failed", "no host key"),
    (ScanRunner(keygen_rc=1), "failed", "no host key"),
    (ScanRunner(timeout=True), "timeout", "timed out"),
    (ScanRunner(missing="ssh-keyscan"), "failed", "cannot run ssh-keyscan"),
    (ScanRunner(missing="ssh-keygen"), "failed", "cannot run ssh-keygen"),
])
def test_scan_host_key_failures_are_classified(runner, kind, match):
    import kvm_host
    with pytest.raises(kvm_host.KvmError, match=match) as e:
        kvm_decl.scan_host_key("10.0.0.21", 22, runner=runner)
    assert e.value.kind == kind and e.value.host == "10.0.0.21"


# --- prochaine version KVM : pools d'images déclarés (facultatif) --------------------

@pytest.mark.parametrize("pools,why", [
    ("images", "image_pools must be a list"),
    ([1], "image_pools must be a list"),
    ([""], "image_pools must be a list"),
    (["a\nb"], "image_pools must be a list"),
    (["p"] * 33, "image_pools must be a list"),
    (["images", "images"], "lists a pool twice"),
])
def test_check_decl_refuses_bad_image_pools(pools, why):
    assert why in kvm_decl.check_decl(kvm_decl.normalize({**GOOD, "image_pools": pools}))


@pytest.mark.parametrize("pools", [[], ["images"], ["images", "iso"], [f"p{i}" for i in range(32)],
                                   ["a pool with spaces"]])
def test_check_decl_accepts_image_pools(pools):
    assert kvm_decl.check_decl(kvm_decl.normalize({**GOOD, "image_pools": pools})) is None


def test_old_declaration_without_image_pools_stays_valid(tmp_path):
    assert "image_pools" not in GOOD
    assert kvm_decl.check_decl(kvm_decl.normalize(dict(GOOD))) is None
