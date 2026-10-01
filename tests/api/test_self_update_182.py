"""v1.82.0 : mise à jour de la console depuis l'interface (bibliothèque,
agent de l'hôte, points d'entrée)."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import self_update as su  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="ssh-keygen needed")


def make_release(dirpath, version, extra=None, top=None):
    """Une archive de livraison minimale : VERSION, install.sh, image."""
    top = top or f"harvester-ops-{version}"
    out = Path(dirpath) / f"harvester-ops-{version}.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        def add(name, data, mode=0o644):
            ti = tarfile.TarInfo(f"{top}/{name}")
            ti.size = len(data)
            ti.mode = mode
            tf.addfile(ti, io.BytesIO(data))
        add("VERSION", f"{version}\n".encode())
        add("install.sh", b"#!/bin/bash\nexit 0\n", 0o755)
        add("images/harvester-ops-ui.tar", b"image")
        for name, data in (extra or {}).items():
            add(name, data)
    return out


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / "k")], check=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / "other")], check=True)
    pub = (d / "k.pub").read_text().split()
    (d / "signers").write_text(f'{su.SIG_IDENTITY} namespaces="{su.SIG_NAMESPACE}" {pub[0]} {pub[1]}\n')
    return d


def sign(keys, archive, key="k"):
    Path(str(archive) + ".sig").unlink(missing_ok=True)      # ssh-keygen n'écrase pas
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(keys / key), "-n", su.SIG_NAMESPACE,
                    str(archive)], check=True)
    return Path(str(archive) + ".sig")


# ---------------------------------------------------------------------------
# Bibliothèque
# ---------------------------------------------------------------------------
def test_versions_order():
    assert su.is_newer("1.82.0", "1.81.0")
    assert su.is_newer("1.100.0", "1.99.9")
    assert su.is_newer("1.82.0", "1.82.0-rc1")
    assert not su.is_newer("1.81.0", "1.81.0")
    assert not su.is_newer("garbage", "1.0.0")


def test_inspect_a_good_release(tmp_path):
    a = make_release(tmp_path, "9.9.9")
    assert su.inspect_archive(a) == {"version": "9.9.9", "top": "harvester-ops-9.9.9"}


@pytest.mark.parametrize("bad,why", [
    ({"../evil": b"x"}, "unsafe path"),
    (None, "does not match"),
])
def test_inspect_refuses(tmp_path, bad, why):
    a = make_release(tmp_path, "9.9.9", extra=bad, top=None if bad else "harvester-ops-1.0.0")
    with pytest.raises(su.UpdateError, match=why):
        su.inspect_archive(a)


def test_inspect_refuses_a_missing_image(tmp_path):
    out = tmp_path / "harvester-ops-9.9.9.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        ti = tarfile.TarInfo("harvester-ops-9.9.9/VERSION"); ti.size = 6
        tf.addfile(ti, io.BytesIO(b"9.9.9\n"))
    with pytest.raises(su.UpdateError, match="missing install.sh, images"):
        su.inspect_archive(out)


def test_inspect_refuses_an_absolute_link(tmp_path):
    out = make_release(tmp_path, "9.9.9")
    with tarfile.open(out, "r:gz") as src, tarfile.open(tmp_path / "x.tar.gz", "w:gz") as dst:
        for m in src:
            dst.addfile(m, src.extractfile(m))
        ln = tarfile.TarInfo("harvester-ops-9.9.9/etc"); ln.type = tarfile.SYMTYPE; ln.linkname = "/etc"
        dst.addfile(ln)
    with pytest.raises(su.UpdateError, match="unsafe link"):
        su.inspect_archive(tmp_path / "x.tar.gz")


def test_signature_valid_wrong_key_and_tampered(tmp_path, keys):
    a = make_release(tmp_path, "9.9.9")
    sig = sign(keys, a)
    assert su.verify_signature(a, sig, keys / "signers")
    other = sign(keys, a, key="other")
    with pytest.raises(su.UpdateError, match="signature refused"):
        su.verify_signature(a, other, keys / "signers")
    sig = sign(keys, a)
    with open(a, "ab") as f:
        f.write(b"tampered")
    with pytest.raises(su.UpdateError, match="signature refused"):
        su.verify_signature(a, sig, keys / "signers")
    with pytest.raises(su.UpdateError, match="no signature"):
        su.verify_signature(a, None, keys / "signers")
    with pytest.raises(su.UpdateError, match="no trusted"):
        su.verify_signature(a, sig, None)


def test_signers_file_prefers_the_operator(tmp_path):
    etc, opt = tmp_path / "etc", tmp_path / "opt"
    etc.mkdir(); opt.mkdir()
    (opt / "update-signers").write_text("a b c\n")
    assert su.signers_file(etc, opt) == opt / "update-signers"
    (etc / "update-signers").write_text("# only a comment\n")
    assert su.signers_file(etc, opt) == opt / "update-signers"
    (etc / "update-signers").write_text("x y z\n")
    assert su.signers_file(etc, opt) == etc / "update-signers"


def test_allow_unsigned_only_when_written(tmp_path):
    c = tmp_path / "update.conf"
    assert not su.allow_unsigned(c)
    c.write_text("allow_unsigned = true\n")
    assert su.allow_unsigned(c)


def test_manifest_checks():
    good = {"version": "1.82.0", "archive": "harvester-ops-1.82.0.tar.gz", "sha256": "a" * 64,
            "signature": "harvester-ops-1.82.0.tar.gz.sig"}
    assert su.check_manifest(good)["version"] == "1.82.0"
    for bad in ({**good, "archive": "../x.tar.gz"}, {**good, "archive": "harvester-ops-1.81.0.tar.gz"},
                {**good, "sha256": "zz"}, {**good, "signature": "../other.sig"}):
        with pytest.raises(su.UpdateError):
            su.check_manifest(bad)
    with pytest.raises(su.UpdateError):
        su.source_url("file:///etc/", "release.json")


def test_release_manifest_from_the_changelog(tmp_path):
    a = make_release(tmp_path, "9.9.9")
    text = ("# Changelog\n\n## [9.9.9] - 2026-10-01 - New thing\n\n### Added\n- one\n- two\n\n"
            "## [9.9.8] - 2026-09-30 - Older\n\n### Fixed\n- three\n")
    m = su.release_manifest("9.9.9", a, text, signed=True)
    assert m["signature"] == a.name + ".sig" and m["title"] == "New thing"
    assert m["sha256"] == su.sha256_file(a)
    assert [n["version"] for n in m["notes"]] == ["9.9.9", "9.9.8"]
    assert m["notes"][0]["sections"][0]["items"] == ["one", "two"]
    assert su.check_manifest(m)["version"] == "9.9.9"


# ---------------------------------------------------------------------------
# Agent de l'hôte
# ---------------------------------------------------------------------------
@pytest.fixture
def agent_env(tmp_path, keys, monkeypatch):
    spec = importlib.util.spec_from_file_location("hops_update_agent", ROOT / "bin" / "harvester-ops-update.py")
    ag = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ag)
    dirs = {k: tmp_path / k for k in ("state", "etc", "opt", "prefix", "work", "log", "units")}
    for d in dirs.values():
        d.mkdir()
    (dirs["state"] / "updates" / "staged").mkdir(parents=True)
    (dirs["opt"] / "VERSION").write_text("1.81.0\n")
    (dirs["opt"] / "update-signers").write_text((keys / "signers").read_text())
    (dirs["prefix"] / "harvester-status.sh").write_text("old\n")
    for name, key in (("STATE_DIR", "state"), ("ETC_DIR", "etc"), ("OPT_DIR", "opt"), ("PREFIX", "prefix"),
                      ("WORK_DIR", "work"), ("LOG_DIR", "log"), ("UNIT_DIR", "units")):
        monkeypatch.setattr(ag, name, dirs[key])
    monkeypatch.setattr(ag, "HEALTH_TIMEOUT", 6)
    return ag, dirs


class FakeHost:
    """Commandes de l'hôte simulées : install.sh « installe » la version en
    réécrivant VERSION et un script ; la console redémarrée répond `answers`."""
    def __init__(self, dirs, version, answers=None, install_rc=0):
        self.dirs, self.version, self.answers, self.install_rc = dirs, version, answers, install_rc
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] == "ssh-keygen":
            return subprocess.run(argv, **kw)
        out = ""
        rc = 0
        if argv[0] == "bash" and argv[1].endswith("install.sh"):
            rc = self.install_rc
            if rc == 0:
                (self.dirs["opt"] / "VERSION").write_text(self.version + "\n")
                (self.dirs["prefix"] / "harvester-status.sh").write_text("new\n")
        return subprocess.CompletedProcess(argv, rc, out, "")


def request(dirs, keys, version="1.82.0", signed=True, **extra):
    staged = dirs["state"] / "updates" / "staged"
    a = make_release(staged, version)
    if signed:
        sign(keys, a)
    (dirs["state"] / "updates" / "request.json").write_text(json.dumps({"archive": a.name, **extra}))
    return a


def run_agent(ag, host, probe):
    agent = ag.Agent(run=host, sleep=lambda s: None)
    ag._probe = probe
    rc = agent.process()
    return rc, json.loads((ag.STATE_DIR / "updates" / "status.json").read_text())


def test_agent_installs_a_signed_newer_release(agent_env, keys):
    ag, dirs = agent_env
    a = request(dirs, keys)
    host = FakeHost(dirs, "1.82.0")
    rc, st = run_agent(ag, host, lambda run: (True, (dirs["opt"] / "VERSION").read_text().strip()))
    assert rc == 0 and st["state"] == "done", st
    assert st["from"] == "1.81.0" and st["to"] == "1.82.0"
    assert [s["id"] for s in st["steps"]] == ["verify", "extract", "backup", "install", "restart", "check"]
    assert ["systemctl", "restart", "harvester-ops"] in host.calls
    assert not (dirs["state"] / "updates" / "request.json").exists()
    assert not a.exists()                     # archive consommée
    assert (dirs["log"] / st["log"]).is_file()


def test_agent_rolls_back_when_the_new_version_does_not_answer(agent_env, keys):
    ag, dirs = agent_env
    request(dirs, keys)
    host = FakeHost(dirs, "1.82.0")
    rc, st = run_agent(ag, host, lambda run: (False, None))
    assert rc == 1 and st["state"] == "rolled-back", st
    # fichiers et image d'avant remis en place
    assert (dirs["opt"] / "VERSION").read_text().strip() == "1.81.0"
    assert (dirs["prefix"] / "harvester-status.sh").read_text() == "old\n"
    assert any(c[:2] == ["podman", "tag"] and c[2].endswith(":previous") for c in host.calls) \
        or any(c[:2] == ["docker", "tag"] for c in host.calls)


def test_agent_rolls_back_when_install_fails(agent_env, keys):
    ag, dirs = agent_env
    request(dirs, keys)
    host = FakeHost(dirs, "1.82.0", install_rc=3)
    rc, st = run_agent(ag, host, lambda run: (True, "1.81.0"))
    assert st["state"] == "rolled-back"
    assert not any(c[:2] == ["systemctl", "restart"] and st["steps"][-1]["id"] == "check" for c in host.calls)


@pytest.mark.parametrize("kw,why", [
    ({"signed": False}, "no signature"),
    ({"version": "1.80.0"}, "not newer"),
])
def test_agent_refuses(agent_env, keys, kw, why):
    ag, dirs = agent_env
    request(dirs, keys, **kw)
    host = FakeHost(dirs, "x")
    rc, st = run_agent(ag, host, lambda run: (True, "x"))
    assert rc == 1 and st["state"] == "failed" and why in st["message"]
    assert not any(c[0] == "bash" for c in host.calls)        # rien d'exécuté


def test_agent_unsigned_only_if_root_allowed_it(agent_env, keys):
    ag, dirs = agent_env
    (dirs["etc"] / "update.conf").write_text("allow_unsigned=true\n")
    request(dirs, keys, signed=False)
    rc, st = run_agent(ag, FakeHost(dirs, "1.82.0"), lambda run: (True, (dirs["opt"] / "VERSION").read_text().strip()))
    assert st["state"] == "done" and "UNSIGNED" in st["steps"][0]["message"]


def test_agent_refuses_a_symlinked_archive(agent_env, keys, tmp_path):
    ag, dirs = agent_env
    elsewhere = make_release(tmp_path, "1.82.0")
    sign(keys, elsewhere)
    staged = dirs["state"] / "updates" / "staged"
    (staged / elsewhere.name).symlink_to(elsewhere)
    (dirs["state"] / "updates" / "request.json").write_text(json.dumps({"archive": elsewhere.name}))
    rc, st = run_agent(ag, FakeHost(dirs, "1.82.0"), lambda run: (True, "1.82.0"))
    assert st["state"] == "failed" and "staging" in st["message"]


def test_agent_with_no_request_does_nothing(agent_env):
    ag, dirs = agent_env
    assert ag.Agent(run=lambda *a, **k: None).process() == 0
    assert not (dirs["state"] / "updates" / "status.json").exists()


# ---------------------------------------------------------------------------
# Installeur et livrable
# ---------------------------------------------------------------------------
def test_install_has_a_non_interactive_upgrade():
    s = (ROOT / "install.sh").read_text()
    body = s[s.index("\nupgrade() {"):s.index("\n}\n", s.index("\nupgrade() {"))]
    assert "prompt" not in body and "setup_basic_auth" not in body and "setup_tls" not in body
    for f in ("install_scripts", "install_bundles", "install_web_ui", "install_update_agent"):
        assert f in body
    assert '"${1:-}" == "--upgrade"' in s


def test_units_and_signers_are_shipped():
    p = (ROOT / "config/systemd/harvester-ops-update.path").read_text()
    assert "PathExists=/var/lib/harvester-ops/updates/request.json" in p
    svc = (ROOT / "config/systemd/harvester-ops-update.service").read_text()
    assert "Type=oneshot" in svc and "harvester-ops-update.py" in svc
    signers = [line for line in (ROOT / "config/update-signers").read_text().splitlines()
               if line and not line.startswith("#")]
    assert signers and signers[0].startswith(f'{su.SIG_IDENTITY} namespaces="{su.SIG_NAMESPACE}" ssh-ed25519 ')
    assert "COPY config/update-signers /opt/harvester-ops/update-signers" in (ROOT / "container/Containerfile").read_text()
    pkg = (ROOT / "package.sh").read_text()
    assert "ssh-keygen -q -Y sign" in pkg and "release.json" in pkg


# ---------------------------------------------------------------------------
# Points d'entrée de la console
# ---------------------------------------------------------------------------
@pytest.fixture
def console(tmp_path, keys, monkeypatch):
    sys.path.insert(0, str(ROOT / "web"))
    import app as wapp
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    monkeypatch.setattr(wapp, "_update_signers", lambda: keys / "signers")
    monkeypatch.setattr(wapp, "_harvester_ops_version", lambda: "1.81.0")
    monkeypatch.setattr(wapp, "AUTH_OPEN_ALLOWED", True)
    monkeypatch.setattr(wapp, "auth_configured", lambda: False)
    monkeypatch.setattr(wapp, "_RATELIMIT_DISABLED", True, raising=False)
    monkeypatch.setattr(wapp, "LOG_DIR", tmp_path / "log")
    # un registre d'actions à soi : d'autres tests du processus en laissent « en cours »
    monkeypatch.setattr(wapp, "ACTIONS", {})
    with wapp._update_lock:
        wapp._update_check.update(ts=0, result=None)
    return wapp, state


def upload(c, path):
    data = Path(path).read_bytes()
    return c.post(f"/api/update/upload?name={Path(path).name}", data=data,
                  headers={"Content-Type": "application/octet-stream"})


def test_upload_then_apply_hands_over_to_the_agent(console, keys, tmp_path):
    wapp, state = console
    a = make_release(tmp_path, "1.82.0")
    sig = sign(keys, a)
    with wapp.app.test_client() as c:
        r = upload(c, a)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["staged"]["signature"] == "missing"
        r = upload(c, sig)
        assert r.get_json()["staged"]["ok"] is True
        st = c.get("/api/update/status").get_json()
        assert st["staged"][0]["version"] == "1.82.0" and st["staged"][0]["newer"]
        # pas d'agent sur l'hôte : on le dit
        r = c.post("/api/update/apply", json={"archive": a.name})
        assert r.status_code == 409 and "agent" in r.get_json()["error"]
        (state / "updates" / "agent.json").write_text('{"installed": "1.82.0"}')
        r = c.post("/api/update/apply", json={"archive": a.name})
        assert r.status_code == 202, r.get_json()
        req = json.loads((state / "updates" / "request.json").read_text())
        assert req["archive"] == a.name and req["version"] == "1.82.0" and req["from"] == "1.81.0"
        # une seule demande à la fois
        assert c.post("/api/update/apply", json={"archive": a.name}).status_code == 409


def test_apply_refuses_unsigned_older_and_while_actions_run(console, keys, tmp_path):
    wapp, state = console
    (state / "updates").mkdir(parents=True, exist_ok=True)
    (state / "updates" / "agent.json").write_text("{}")
    old = make_release(tmp_path, "1.80.0"); sign(keys, old)
    new = make_release(tmp_path, "1.82.0")
    with wapp.app.test_client() as c:
        upload(c, new)
        r = c.post("/api/update/apply", json={"archive": new.name})
        assert r.status_code == 409 and "sig" in r.get_json()["error"]
        upload(c, old); upload(c, Path(str(old) + ".sig"))
        r = c.post("/api/update/apply", json={"archive": old.name})
        assert r.status_code == 409 and "not newer" in r.get_json()["error"]
        sign(keys, new); upload(c, Path(str(new) + ".sig"))
        run = wapp.ActionRun("busy1", "shutdown", "harv1", [])
        run.status = "running"
        with wapp.ACTIONS_LOCK:
            wapp.ACTIONS["busy1"] = run
        try:
            r = c.post("/api/update/apply", json={"archive": new.name})
            assert r.status_code == 409 and r.get_json()["busy"][0]["action"] == "shutdown"
            assert c.post("/api/update/apply", json={"archive": new.name, "force": True}).status_code == 202
        finally:
            with wapp.ACTIONS_LOCK:
                wapp.ACTIONS.pop("busy1", None)


def test_upload_refuses_other_names(console):
    wapp, _ = console
    with wapp.app.test_client() as c:
        for name in ("evil.tar.gz", "../harvester-ops-1.0.0.tar.gz", "harvester-ops-1.0.0.tgz"):
            assert c.post(f"/api/update/upload?name={name}", data=b"x").status_code == 400


def test_source_must_be_http(console):
    wapp, _ = console
    with wapp.app.test_client() as c:
        assert c.put("/api/update/source", json={"url": "file:///etc/"}).status_code == 400
        r = c.put("/api/update/source", json={"url": "https://mirror.example/hops/"})
        assert r.get_json()["source"] == "https://mirror.example/hops/"
        r = c.put("/api/update/source", json={"url": ""})
        assert r.get_json()["source"] == wapp.UPDATE_DEFAULT_SOURCE


def test_check_and_download_from_a_mirror(console, keys, tmp_path, monkeypatch):
    """Miroir HTTP simulé : release.json, archive, signature."""
    wapp, state = console
    pub = tmp_path / "mirror"
    pub.mkdir()
    a = make_release(pub, "1.82.0")
    sign(keys, a)
    (pub / "release.json").write_text(json.dumps(su.release_manifest(
        "1.82.0", a, "## [1.82.0] - 2026-10-01 - Update\n\n### Added\n- x\n", True)))

    class Resp(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.headers = {"Content-Length": str(len(data))}
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fetch(url, timeout=20, limit=None):
        assert url.startswith("https://mirror.example/hops/")
        return Resp((pub / url.rsplit("/", 1)[1]).read_bytes())
    monkeypatch.setattr(wapp, "_update_fetch", fetch)
    with wapp.app.test_client() as c:
        c.put("/api/update/source", json={"url": "https://mirror.example/hops/"})
        r = c.post("/api/update/check").get_json()
        assert r["ok"] and r["newer"] and r["release"]["version"] == "1.82.0"
        aid = c.post("/api/update/download").get_json()["action_id"]
        run = wapp.ACTIONS[aid]
        for _ in range(200):
            if run.status not in ("starting", "running"):
                break
            import time; time.sleep(0.05)
        assert run.status == "done", run.error_summary
        st = c.get("/api/update/status").get_json()
        assert st["staged"][0]["ok"] and st["staged"][0]["signature"] == "valid"


def test_download_refuses_a_checksum_mismatch(console, keys, tmp_path, monkeypatch):
    wapp, state = console
    a = make_release(tmp_path, "1.82.0")
    man = su.check_manifest(su.release_manifest("1.82.0", a, "", False))
    man["sha256"] = "0" * 64

    class Resp(io.BytesIO):
        headers = {}
        def __enter__(self):
            return self
        def __exit__(self, *x):
            return False
    monkeypatch.setattr(wapp, "_update_fetch", lambda url, **k: Resp(a.read_bytes()))
    run = wapp.ActionRun("dl1", "console-update:download", "", [])
    wapp._update_download_runner(run, "https://m.example/", man)
    assert run.status == "error" and "checksum" in run.error_summary
    assert not list((state / "updates" / "staged").glob("*.tar.gz*"))


def test_outcome_recorded_once_at_startup(console):
    wapp, state = console
    d = state / "updates"
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({
        "state": "rolled-back", "from": "1.81.0", "to": "1.82.0", "started": 123.0,
        "message": "1.82.0 failed", "steps": [{"id": "check", "status": "error", "message": "no answer", "ts": 1}]}))
    before = len(wapp.ACTIONS)
    wapp._update_record_outcome()
    wapp._update_record_outcome()
    new = [a for a in wapp.ACTIONS.values() if a.action == "console-update:1.82.0"]
    assert len(new) == 1 and new[0].status == "error" and "1.82.0 failed" in new[0].error_summary
    assert len(wapp.ACTIONS) == before + 1
