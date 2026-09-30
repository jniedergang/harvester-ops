"""1.78.0 : démarrage de découverte d'une machine nue.

L'ISO Harvester démarre une fois avec un script à elle (déposé dans l'ISO),
renvoie ce que Linux voit et éteint la machine. Ce que ces tests figent :

* le jeton de dépôt de l'inventaire : POST seulement, un seul envoi,
  1 Mio au plus, fichier écrit en 0600 ;
* le script : aucun secret, sortie immédiate dans l'initrd (vu en réel :
  `systemd.run=` s'y exécute aussi), extinction à la fin, format de
  l'inventaire lu tel quel par `parse_discovery` ;
* la remasterisation qui dépose un fichier exécutable (Rock Ridge) et
  remplace les arguments zéro-touch ;
* l'ISO de découverte en cache ;
* le déroulé complet contre un BMC simulé et le vrai serveur d'artefacts.
"""

import json
import os
import re
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"
BIN = ROOT / "bin"
sys.path.insert(0, str(WEB))
sys.path.insert(0, str(BIN / "lib"))

import app as wapp              # noqa: E402
import pxe_server as px         # noqa: E402
import bm_discover as bmd       # noqa: E402
import baremetal_disks as bdk   # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "bm_disks_178" / "discovery.txt"
XORRISO = pytest.mark.skipif(not Path("/usr/bin/xorriso").exists(), reason="xorriso absent")


@pytest.fixture()
def served():
    port = px.start(port=0, bind="127.0.0.1")
    yield port
    px.stop()


def _post(port, path, body, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# ---------------------------------------------------------------------------
# Jeton de dépôt
# ---------------------------------------------------------------------------

def test_inventory_token_accepts_one_post_written_private(served, tmp_path):
    dest = tmp_path / "inv.txt"
    tok = px.issue(dest, "inventory")
    assert _get(served, f"/pxe/inventory/{tok}") == 404, "un dépôt ne se lit pas"
    assert _post(served, f"/pxe/inventory/{tok}", b"== lsblk\n{}\n") == 204
    assert dest.read_bytes() == b"== lsblk\n{}\n"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    # usage unique : le second envoi est refusé et ne remplace rien
    assert _post(served, f"/pxe/inventory/{tok}", b"forged") == 404
    assert dest.read_bytes() == b"== lsblk\n{}\n"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".part")]


def test_inventory_token_refuses_more_than_one_mebibyte(served, tmp_path):
    dest = tmp_path / "inv.txt"
    tok = px.issue(dest, "inventory")
    # refusé sur la taille annoncée, sans lire le corps
    import socket
    with socket.create_connection(("127.0.0.1", served), timeout=5) as sk:
        sk.sendall(f"POST /pxe/inventory/{tok} HTTP/1.1\r\nHost: x\r\n"
                   f"Content-Length: {px.INVENTORY_MAX + 1}\r\n\r\n".encode())
        assert sk.recv(64).startswith(b"HTTP/1.1 413")
    assert _post(served, f"/pxe/inventory/{tok}", b"x", {"Content-Length": "abc"}) in (400, 411)
    assert not dest.exists()
    # refus avant lecture : le jeton n'est pas consommé
    assert _post(served, f"/pxe/inventory/{tok}", b"x" * px.INVENTORY_MAX) == 204


def test_inventory_needs_its_own_kind(served, tmp_path):
    iso = tmp_path / "a.iso"
    iso.write_bytes(b"iso")
    iso_tok = px.issue(iso, "iso")
    inv_tok = px.issue(tmp_path / "inv.txt", "inventory")
    # un jeton d'ISO n'accepte pas de dépôt, un jeton de dépôt ne sert pas d'ISO
    assert _post(served, f"/pxe/inventory/{iso_tok}", b"x") == 404
    assert _post(served, f"/pxe/iso/{iso_tok}.iso", b"x") == 404
    assert _get(served, f"/pxe/iso/{inv_tok}.iso") == 404
    assert iso.read_bytes() == b"iso"
    assert _post(served, "/pxe/inventory/../../etc", b"x") == 404


def test_an_armed_token_cannot_be_armed_twice(tmp_path):
    tok = px.issue(tmp_path / "a", "inventory", token="slot-abcdefghijklmnop")
    try:
        with pytest.raises(px.TokenInUse):
            px.issue(tmp_path / "b", "inventory", token="slot-abcdefghijklmnop")
    finally:
        px.revoke(tok)
    px.revoke(px.issue(tmp_path / "b", "inventory", token="slot-abcdefghijklmnop"))


# ---------------------------------------------------------------------------
# Script de découverte
# ---------------------------------------------------------------------------

def test_template_carries_no_secret_guards_the_initrd_and_powers_off():
    tpl = bmd.TEMPLATE.read_text()
    assert tpl.startswith("#!/bin/bash\n")
    # la seule donnée variable est l'adresse de dépôt
    assert set(re.findall(r"__[A-Z_]+__", tpl)) == {"__UPLOAD_URL__"}
    for word in ("token:", "password", "PASSWORD", "harvester.install"):
        assert word not in tpl
    code = [ln for ln in tpl.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert code[0] == "test -e /etc/initrd-release && exit 0", "la garde passe avant tout"
    assert code[-1] == "systemctl poweroff"
    assert "--retry 60 --retry-delay 5 --retry-all-errors" in tpl
    assert "lsblk -J -b -O" in tpl and "ip -j link" in tpl and 'readlink -f "$l"' in tpl


def test_rendered_script_only_takes_a_plain_upload_url():
    url = "http://10.0.0.5:8091/pxe/inventory/AbC_-123"
    out = bmd.render_script(url)
    assert f'UPLOAD_URL="{url}"' in out and "__UPLOAD_URL__" not in out
    for bad in ('http://x/pxe/inventory/a"; reboot', "http://x/pxe/iso/a.iso",
                "https://x/pxe/inventory/a", ""):
        with pytest.raises(ValueError):
            bmd.render_script(bad)


def test_kernel_line_is_a_path_without_quotes_nor_install():
    args = bmd.kernel_args("console=ttyS1,115200")
    assert args.startswith("systemd.run=/run/initramfs/live/discover.sh ")
    assert "systemd.run_success_action=none" in args
    assert "systemd.run_failure_action=none" in args
    assert args.endswith(" console=ttyS1,115200")
    assert '"' not in args and "'" not in args and "harvester.install" not in args
    for bad in ('a" b', "harvester.install.automatic=true", "systemd.run=/bin/sh"):
        with pytest.raises(ValueError):
            bmd.clean_extra_args(bad)


def _run_template(tmp_path, initrd=False):
    """Exécute le vrai gabarit, commandes remplacées : lsblk et ip rendent
    les sections de la capture du banc, les liens sont de vrais liens
    symboliques, curl recopie le corps envoyé."""
    sections = bdk.split_sections(FIXTURE.read_text())
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (tmp_path / "lsblk.json").write_text("\n".join(sections["lsblk"]))
    (tmp_path / "ip.json").write_text("\n".join(sections["nics"]))
    sent = tmp_path / "sent.txt"
    calls = tmp_path / "calls.txt"
    for name, body in {
        "lsblk": f'cat "{tmp_path}/lsblk.json"',
        "ip": f'cat "{tmp_path}/ip.json"',
        "udevadm": "exit 0",
        "curl": f'echo "curl $*" >> "{calls}"; for a; do case "$a" in @*) cat "${{a#@}}" > "{sent}";; esac; done',
        "systemctl": f'echo "systemctl $*" >> "{calls}"',
    }.items():
        p = stubs / name
        p.write_text(f"#!/bin/bash\n{body}\n")
        p.chmod(0o755)
    byid, bypath = tmp_path / "by-id", tmp_path / "by-path"
    byid.mkdir(); bypath.mkdir()
    for ln in sections["links"]:
        link, target = ln.split(" ", 1)
        d = byid if "/by-id/" in link else bypath
        (d / link.rsplit("/", 1)[1]).symlink_to(target)
    net = tmp_path / "net"
    for nic, speed in (("enp1s0", "1000"), ("lo", "")):
        (net / nic).mkdir(parents=True)
        if speed:
            (net / nic / "speed").write_text(speed + "\n")
    guard = tmp_path / "initrd-release"
    if initrd:
        guard.write_text("x")
    script = bmd.render_script("http://127.0.0.1:1/pxe/inventory/TOK")
    script = (script.replace("/etc/initrd-release", str(guard))
              .replace("/run/harvester-ops-discovery.txt", str(tmp_path / "out.txt"))
              .replace("/dev/disk/by-id", str(byid)).replace("/dev/disk/by-path", str(bypath))
              .replace("/sys/class/net", str(net)))
    sh = tmp_path / "discover.sh"
    sh.write_text(script)
    env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}")
    r = subprocess.run(["bash", str(sh)], env=env, capture_output=True, text=True, timeout=30)
    return r, sent, calls, (byid, bypath)


def test_the_script_emits_what_the_parser_reads(tmp_path):
    r, sent, calls, (byid, bypath) = _run_template(tmp_path)
    assert r.returncode == 0, r.stderr
    log = calls.read_text().splitlines()
    assert log[0].startswith("curl ") and "http://127.0.0.1:1/pxe/inventory/TOK" in log[0]
    assert log[-1] == "systemctl poweroff", "l'extinction vient après l'envoi"
    text = sent.read_text().replace(str(byid), "/dev/disk/by-id").replace(
        str(bypath), "/dev/disk/by-path")
    assert [ln for ln in text.splitlines() if ln.startswith("== ")] == [
        "== lsblk", "== links", "== nics", "== speeds"]
    got = bdk.parse_discovery(text)
    want = bdk.parse_discovery(FIXTURE.read_text())
    assert [d["name"] for d in got["disks"]] == [d["name"] for d in want["disks"]]
    assert [d["stable_path"] for d in got["disks"]] == [d["stable_path"] for d in want["disks"]]
    assert got["disks"] and got["nics"]


def test_the_script_does_nothing_in_the_initrd(tmp_path):
    r, sent, calls, _ = _run_template(tmp_path, initrd=True)
    assert r.returncode == 0
    assert not calls.exists() and not sent.exists(), "ni envoi ni extinction dans l'initrd"


# ---------------------------------------------------------------------------
# Remasterisation : fichier déposé, arguments remplacés
# ---------------------------------------------------------------------------

def _mini_iso(d):
    src = d / "src"
    (src / "boot/grub2").mkdir(parents=True)
    (src / "boot/grub2/grub.cfg").write_text(
        "set default=0\nsource (${root})/boot/grub2/harvester.cfg\n"
        'menuentry "Harvester Installer" {\n  $linux ($root)/boot/x86_64/loader/linux '
        "cdroot root=live:CDLABEL=COS_LIVE ${extra_iso_cmdline}\n}\n")
    (src / "boot/grub2/harvester.cfg").write_text("set harvester_version=v1.9.0\n")
    (src / "boot/efiboot.img").write_bytes(b"\0" * 4096)
    iso = d / "harvester-v1.9.0-amd64.iso"
    subprocess.run(["xorriso", "-as", "mkisofs", "-V", "COS_LIVE", "-e", "boot/efiboot.img",
                    "-no-emul-boot", "-o", str(iso), str(src)], capture_output=True, check=True)
    return iso


def _remaster(*args):
    return subprocess.run(["bash", str(BIN / "harvester-iso-remaster.sh"), *map(str, args)],
                          capture_output=True, text=True)


def _extract(iso, path, dest):
    subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", path,
                    str(dest)], capture_output=True)
    return dest.read_text() if dest.exists() else ""


@XORRISO
def test_remaster_adds_an_executable_file_and_replaces_the_install_args(tmp_path):
    iso = _mini_iso(tmp_path)
    script = tmp_path / "d.sh"
    script.write_text(bmd.render_script("http://10.0.0.5:8091/pxe/inventory/TOK"))
    script.chmod(0o600)
    out = tmp_path / "out.iso"
    r = _remaster("--src", iso, "--out", out, "--kernel-args", bmd.kernel_args(),
                  "--add-file", f"{script}:/discover.sh:0755")
    assert r.returncode == 0, r.stderr[-1500:]
    cfg = _extract(out, "/boot/grub2/harvester.cfg", tmp_path / "h.cfg")
    assert f'set extra_iso_cmdline="{bmd.kernel_args()}"' in cfg
    assert "harvester.install" not in cfg
    assert _extract(out, "/discover.sh", tmp_path / "back.sh") == script.read_text()
    ls = subprocess.run(["xorriso", "-indev", str(out), "-lsdl", "/discover.sh"],
                        capture_output=True, text=True).stdout
    assert ls.startswith("-rwxr-xr-x"), f"exécutable sur l'ISO (Rock Ridge) : {ls!r}"


@XORRISO
@pytest.mark.parametrize("args", [
    ["--config-url", "http://x/c.yaml", "--kernel-args", "a=b"],       # l'un ou l'autre
    [],                                                                 # ni l'un ni l'autre
    ["--kernel-args", 'a="b"'],                                         # guillemet
    ["--kernel-args", "a=b", "--add-file", "/nonexistent:/d.sh:0755"],
    ["--kernel-args", "a=b", "--add-file", "{script}:d.sh:0755"],       # chemin relatif
    ["--kernel-args", "a=b", "--add-file", "{script}:/d.sh:rwx"],
])
def test_remaster_refuses_bad_discovery_arguments(tmp_path, args):
    iso = _mini_iso(tmp_path)
    script = tmp_path / "d.sh"
    script.write_text("#!/bin/bash\n")
    args = [a.replace("{script}", str(script)) for a in args]
    r = _remaster("--src", iso, "--out", tmp_path / "out.iso", *args)
    assert r.returncode != 0
    assert not (tmp_path / "out.iso").exists()


# ---------------------------------------------------------------------------
# ISO de découverte en cache
# ---------------------------------------------------------------------------

class _FakeRemaster:
    """Tient lieu du script : écrit l'ISO demandée et retient le script
    déposé (donc l'adresse de dépôt gravée)."""

    def __init__(self):
        self.calls = []
        self.upload_url = None

    def __call__(self, cmd, step):
        self.calls.append(cmd)
        out = Path(cmd[cmd.index("--out") + 1])
        out.write_bytes(b"iso")
        spec = cmd[cmd.index("--add-file") + 1]
        src = spec.split(":", 1)[0]
        m = re.search(r'UPLOAD_URL="([^"]+)"', Path(src).read_text())
        self.upload_url = m.group(1)
        step("iso-build", "done", "fake")
        return 0


def test_discovery_iso_is_remastered_once_per_key(tmp_path, monkeypatch):
    src = tmp_path / "h.iso"
    src.write_bytes(b"source")
    cache = tmp_path / "discover"
    fake = _FakeRemaster()
    ensure = lambda base="http://10.0.0.5:8091", extra="": bmd.ensure_iso(  # noqa: E731
        src, cache, base, extra, BIN / "harvester-iso-remaster.sh", fake, lambda *a: None)
    iso, slot, reused = ensure()
    assert not reused and len(fake.calls) == 1 and iso.read_bytes() == b"iso"
    assert fake.upload_url == f"http://10.0.0.5:8091/pxe/inventory/{slot}"
    cmd = fake.calls[0]
    assert cmd[cmd.index("--kernel-args") + 1] == bmd.kernel_args()
    assert cmd[cmd.index("--add-file") + 1].endswith(":/discover.sh:0755")
    meta = cache / "h.discover.json"
    assert stat.S_IMODE(meta.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert not list(cache.glob(".discover-*.sh")), "le script local ne reste pas"

    assert ensure() == (iso, slot, True) and len(fake.calls) == 1
    ensure(base="http://10.0.0.6:8091")                     # autre adresse gravée
    assert len(fake.calls) == 2
    ensure(base="http://10.0.0.6:8091", extra="console=ttyS1")
    assert len(fake.calls) == 3
    monkeypatch.setattr(bmd, "template_version", lambda: "other")
    ensure(base="http://10.0.0.6:8091", extra="console=ttyS1")
    assert len(fake.calls) == 4
    assert len(list(cache.glob("*.discover.iso"))) == 1, "une seule ISO en cache par source"


def test_source_identity_prefers_the_store_checksum(tmp_path):
    src = tmp_path / "h.iso"
    src.write_bytes(b"x")
    assert bmd.source_identity(src).startswith("size:1:")
    (tmp_path / "h.iso.sha256").write_text("a" * 64 + "  h.iso\n")
    assert bmd.source_identity(src) == "sha256:" + "a" * 64


# ---------------------------------------------------------------------------
# Déroulé complet, BMC simulé, vrai serveur d'artefacts
# ---------------------------------------------------------------------------

class FakeBmc:
    def __init__(self, fake_remaster, post=True, self_off=True, power="Off"):
        self.fake, self.post, self.self_off = fake_remaster, post, self_off
        self.power = power
        self.calls = []

    def profile(self):
        self.calls.append("profile")
        return {"ok": True, "virtualmedia_path": "/vm/2", "boot_targets": ["Hdd", "Cd"],
                "serial": "SERIAL-0001", "model": "ProLiant", "power_state": self.power}

    def insert(self, url):
        self.calls.append("insert")
        with urllib.request.urlopen(url, timeout=5) as r:   # l'ISO est bien servie
            assert r.read() == b"iso"
        return True, ""

    def eject(self):
        self.calls.append("eject")

    def boot_once_cd(self):
        self.calls.append("boot")
        return True, ""

    def reset(self, kind):
        self.calls.append(f"reset:{kind}")
        if kind in ("On", "ForceRestart"):
            self.power = "On"
            if self.post:
                body = FIXTURE.read_bytes()
                req = urllib.request.Request(self.fake.upload_url, data=body, method="POST")
                urllib.request.urlopen(req, timeout=5).close()
                if self.self_off:
                    self.power = "Off"
        elif kind == "ForceOff":
            self.power = "Off"
        return True, ""

    def power_state(self):
        return self.power


def _opts(tmp_path, port, **kw):
    src = tmp_path / "h.iso"
    if not src.exists():
        src.write_bytes(b"source")
    return dict({"host": "192.0.2.21", "src_iso": src, "cache_dir": tmp_path / "discover",
                 "work_dir": tmp_path / "work", "store_dir": tmp_path / "inventory",
                 "remaster_script": BIN / "harvester-iso-remaster.sh",
                 "advertise": "127.0.0.1", "port": port, "extra_args": "",
                 "poll": 0.01}, **kw)


def test_discovery_runs_end_to_end(served, tmp_path):
    (tmp_path / "work").mkdir()
    fake = _FakeRemaster()
    bmc = FakeBmc(fake)
    steps = []
    res = bmd.run(_opts(tmp_path, served), bmc, px, fake, lambda *a: steps.append(a))
    assert res["serial"] == "SERIAL-0001" and res["forced_off"] is False
    assert bmc.calls == ["profile", "insert", "boot", "reset:On", "eject"], (
        "machine éteinte : allumée, pas redémarrée ; média éjecté à la fin")
    stored = tmp_path / "inventory" / "SERIAL-0001.json"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    doc = json.loads(stored.read_text())
    assert doc["raw"] == FIXTURE.read_text() and doc["bmc_host"] == "192.0.2.21"
    assert bmd.load_inventory(tmp_path / "inventory", "192.0.2.21")["system_serial"] == "SERIAL-0001"
    assert not list((tmp_path / "work").iterdir()), "le dépôt brut ne reste pas"
    ids = [s[0] for s in steps if s[1] == "done"]
    assert ids == ["preflight", "iso-build", "remaster", "serve", "bmc-insert", "bmc-boot",
                   "power", "wait-inventory", "power-off", "store"]
    # jetons révoqués : l'adresse gravée n'accepte plus rien
    assert _post(served, fake.upload_url.split(f":{served}", 1)[1], b"x") == 404
    # une seconde découverte reprend l'ISO en cache et réarme le même jeton
    bmc2 = FakeBmc(fake, power="On")
    bmd.run(_opts(tmp_path, served), bmc2, px, fake, lambda *a: None)
    assert len(fake.calls) == 1 and "reset:ForceRestart" in bmc2.calls


def test_a_machine_that_stays_on_is_forced_off_and_said_so(served, tmp_path):
    (tmp_path / "work").mkdir()
    fake = _FakeRemaster()
    bmc = FakeBmc(fake, self_off=False)
    steps = []
    res = bmd.run(_opts(tmp_path, served, poweroff_timeout=0.05), bmc, px, fake,
                  lambda *a: steps.append(a))
    assert res["forced_off"] is True
    assert bmc.calls[-2:] == ["reset:ForceOff", "eject"]
    assert any(s[0] == "power-off" and "forcée" in s[2] for s in steps)


def test_no_inventory_times_out_forces_off_and_ejects(served, tmp_path):
    (tmp_path / "work").mkdir()
    fake = _FakeRemaster()
    bmc = FakeBmc(fake, post=False)
    with pytest.raises(bmd.DiscoveryError) as e:
        bmd.run(_opts(tmp_path, served, inventory_timeout=0.05), bmc, px, fake, lambda *a: None)
    assert e.value.step == "wait-inventory"
    assert bmc.calls[-2:] == ["reset:ForceOff", "eject"]
    assert not (tmp_path / "inventory").exists()
    # le jeton gravé est libéré : une nouvelle découverte peut l'armer
    bmd.run(_opts(tmp_path, served), FakeBmc(fake), px, fake, lambda *a: None)


def test_a_cancelled_discovery_ejects_and_says_cancelled(served, tmp_path):
    (tmp_path / "work").mkdir()
    fake = _FakeRemaster()
    bmc = FakeBmc(fake, post=False)
    flag = {"n": 0}

    def cancelled():
        flag["n"] += 1
        return flag["n"] > 3
    with pytest.raises(bmd.Cancelled):
        bmd.run(_opts(tmp_path, served), bmc, px, fake, lambda *a: None, cancelled=cancelled)
    assert "eject" in bmc.calls


def test_preflight_refuses_without_virtual_media_or_serial(served, tmp_path):
    fake = _FakeRemaster()
    for broken in ({"virtualmedia_path": None}, {"boot_targets": ["Hdd"]},
                   {"serial": "", "uuid": ""}, {"ok": False, "error": "unreachable"}):
        bmc = FakeBmc(fake)
        base = bmc.profile()
        bmc.profile = lambda b=dict(base, **broken): b
        with pytest.raises(bmd.DiscoveryError) as e:
            bmd.run(_opts(tmp_path, served), bmc, px, fake, lambda *a: None)
        assert e.value.step == "preflight"
        assert not fake.calls, "rien n'est remasterisé ni allumé"


def test_the_app_runner_uses_the_console_bmc_helpers(tmp_path, monkeypatch):
    """Le runner de la console passe par ses propres fonctions Redfish et par
    le vrai serveur d'artefacts ; il analyse l'inventaire reçu."""
    port = px.start(port=0, bind="127.0.0.1")
    try:
        monkeypatch.setattr(wapp, "ISO_DIR", tmp_path / "iso")
        monkeypatch.setattr(wapp, "INVENTORY_DIR", tmp_path / "inventory")
        (tmp_path / "iso").mkdir()
        (tmp_path / "iso" / "h.iso").write_bytes(b"source")
        fake = _FakeRemaster()
        monkeypatch.setattr(wapp, "_bm_remaster_stream", fake)
        bmc = FakeBmc(fake)
        monkeypatch.setattr(wapp, "_bmc_discover_one",
                            lambda h, u, p: dict(bmc.profile(), system_path="/redfish/v1/Systems/1/"))
        seen = {}
        monkeypatch.setattr(wapp, "_bm_media_insert",
                            lambda h, u, p, url: (seen.setdefault("pwd", p), bmc.insert(url))[1] + (None,))
        monkeypatch.setattr(wapp, "_bm_media_eject", lambda h, u, p, vm=None: bmc.eject())
        monkeypatch.setattr(wapp, "_bm_boot_once_cd", lambda h, u, p, sp: bmc.boot_once_cd())
        monkeypatch.setattr(wapp, "_bm_reset", lambda h, u, p, sp, k: bmc.reset(k))
        monkeypatch.setattr(wapp, "_redfish_get", lambda *a, **k: {"PowerState": bmc.power})
        run = wapp.ActionRun("disc00000001", "baremetal-discover:192.0.2.21", "(local)", [])
        run.close = lambda: None
        wapp._baremetal_discover_runner(run, {
            "bmc_host": "192.0.2.21", "bmc_user": "u", "bmc_password": "S3CRET-PW",
            "iso": "h.iso", "extra_args": "", "advertise_host": "127.0.0.1", "poll": 0.01})
        assert run.status == "done", run.events
        assert seen["pwd"] == "S3CRET-PW"
        assert "S3CRET-PW" not in json.dumps(list(run.events))
        parse = [e for e in run.events if e.get("step_id") == "parse"]
        assert parse and "disque" in parse[0]["message"]
        assert (tmp_path / "inventory" / "SERIAL-0001.json").is_file()
    finally:
        px.stop()


def test_the_app_runner_reads_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "ISO_DIR", tmp_path / "iso")
    (tmp_path / "iso").mkdir()
    (tmp_path / "iso" / "h.iso").write_bytes(b"source")
    monkeypatch.setattr(wapp.pxe_server, "start", lambda *a, **k: 1)
    monkeypatch.setattr(wapp, "_bmc_discover_one", lambda *a: {"ok": False, "error": "x"})
    run = wapp.ActionRun("disc00000002", "baremetal-discover:h", "(local)", [])
    run.close = lambda: None
    run._cancel = True
    wapp._baremetal_discover_runner(run, {"bmc_host": "192.0.2.21", "bmc_user": "u",
                                          "bmc_password": "p", "iso": "h.iso",
                                          "advertise_host": "127.0.0.1"})
    assert run.status == "cancelled" and run.exit_code == 3


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    wapp.app.config["TESTING"] = True
    return wapp.app.test_client()


BODY = {"bmc_host": "192.0.2.21", "bmc_user": "admin", "bmc_password": "S3CRET-PW",
        "iso": "harvester-v1.9.0-amd64.iso"}


def test_discover_route_validates_then_tracks_an_action(client, monkeypatch):
    runs = []
    monkeypatch.setattr(wapp, "track_action",
                        lambda label, cluster, worker, *a: runs.append((label, worker, a)) or "a1")
    r = client.post("/api/baremetal/discover", json={})
    assert r.status_code == 400 and set(r.get_json()["fields"]) == {
        "bmc_host", "bmc_user", "bmc_password", "iso"}
    for bad in ({"iso": "../x.iso"}, {"bmc_host": "a b"}, {"extra_args": 'x" ; reboot'},
                {"extra_args": "harvester.install.automatic=true"}):
        assert client.post("/api/baremetal/discover", json=dict(BODY, **bad)).status_code == 400
    assert not runs
    r = client.post("/api/baremetal/discover", json=dict(BODY, extra_args=" console=ttyS1  "))
    assert r.status_code == 202 and r.get_json() == {"action_id": "a1", "host": "192.0.2.21"}
    label, worker, args = runs[0]
    assert label == "baremetal-discover:192.0.2.21" and "S3CRET" not in label
    assert worker is wapp._baremetal_discover_runner
    assert args[0]["extra_args"] == "console=ttyS1"
    assert "S3CRET" not in r.get_data(as_text=True)


def test_inventory_route_serves_the_parsed_inventory(client, monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "INVENTORY_DIR", tmp_path)
    assert client.get("/api/baremetal/inventory/192.0.2.21").status_code == 404
    bmd.store_inventory(tmp_path, "SERIAL-0001", "192.0.2.21", FIXTURE.read_text(), now=100)
    bmd.store_inventory(tmp_path, "OTHER", "192.0.2.22", "== lsblk\n{}\n", now=200)
    r = client.get("/api/baremetal/inventory/192.0.2.21")
    assert r.status_code == 200
    d = r.get_json()
    assert d["source"] == "discovery" and d["at"] == 100 and d["system_serial"] == "SERIAL-0001"
    assert d["disks"] == bdk.parse_discovery(FIXTURE.read_text())["disks"]
    assert "raw" not in d and d["nics"]
    assert client.get("/api/baremetal/inventory/bad%20host").status_code in (400, 404)


def test_new_routes_are_authenticated_and_rate_limited():
    from limits import parse_many
    src = (WEB / "app.py").read_text()
    for route in ("/api/baremetal/discover", "/api/baremetal/inventory/<host>"):
        head = src.split(f'@app.route("{route}"', 1)[1].split("\ndef ", 1)[0]
        assert "@requires_auth" in head
        spec = re.search(r'@_rate_limit\("([^"]+)"\)', head).group(1)
        assert parse_many(spec)


def test_the_command_line_reads_the_password_off_argv():
    src = (BIN / "harvester-baremetal.py").read_text()
    assert "--password-file" in src and "--password-stdin" in src
    assert 'add_argument("--password"' not in src
    r = subprocess.run([sys.executable, str(BIN / "harvester-baremetal.py"), "discover",
                        "--bmc", "x", "--user", "u", "--iso", "x.iso", "--password", "p"],
                       capture_output=True, text=True)
    assert r.returncode == 2


def test_the_command_line_shares_the_console_code_path():
    src = (BIN / "harvester-baremetal.py").read_text()
    assert "bm_discover.run(" in src and "harvester-iso-remaster.sh" in src
    app_src = (WEB / "app.py").read_text()
    runner = app_src.split("def _baremetal_discover_runner(", 1)[1].split("\ndef ", 1)[0]
    assert "_bmd.run(" in runner and '"harvester-iso-remaster.sh"' in runner
