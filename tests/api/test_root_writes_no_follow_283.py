"""2.8.3 : root never writes, creates or hands over a file by following a link
planted in a directory the console account can write.

The update agent and `install.sh --upgrade` run as root and work inside
/var/lib/harvester-ops and /var/log/harvester-ops, which belong to the
console account (its container mounts them read-write). Each case below
plants a link (or swaps an entry at the worst moment) and checks that the
file or directory outside stays untouched, and that nothing outside is
handed to the service account. Everything runs unprivileged, in temporary
directories: a chown is recorded (which inode it would change), not done.
"""
import grp
import json
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_self_update_182 import agent_env, keys, FakeHost, make_release, sign  # noqa: E402,F401
from test_embedded_providers import livrable  # noqa: E402

STAMP = 1_800_000_000.0          # instant fixe : nom de journal prévisible


def ino(p):
    st = os.stat(p, follow_symlinks=False)
    return st.st_dev, st.st_ino


@pytest.fixture
def chowns(monkeypatch):
    """Every chown/fchown is recorded as the inode it would change; the
    service account resolves to the current user, so the code takes its
    normal path."""
    seen = []
    me = pwd.getpwuid(os.getuid())
    real_getpwnam, real_getgrnam = pwd.getpwnam, grp.getgrnam

    def rec_chown(path, uid, gid, *, dir_fd=None, follow_symlinks=True):
        st = os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        seen.append((st.st_dev, st.st_ino))

    def rec_fchown(fd, uid, gid):
        st = os.fstat(fd)
        seen.append((st.st_dev, st.st_ino))

    monkeypatch.setattr(os, "chown", rec_chown)
    monkeypatch.setattr(os, "fchown", rec_fchown)
    monkeypatch.setattr(os, "lchown", lambda *a, **k: None)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwnam", lambda n: me if n == "harvester-ops" else real_getpwnam(n))
    monkeypatch.setattr(grp, "getgrnam",
                        lambda n: grp.getgrgid(os.getgid()) if n == "harvester-ops" else real_getgrnam(n))
    return seen


def run(ag, request_body=None, probe=lambda run: (True, "1.82.0"), host=None, dirs=None):
    upd = ag.STATE_DIR / "updates"
    if request_body is not None:
        (upd / "request.json").write_text(json.dumps(request_body))
    agent = ag.Agent(run=host or FakeHost(dirs, "1.82.0"), sleep=lambda s: None, now=lambda: STAMP)
    ag._probe = probe
    return agent.process()


def log_name(ag):
    import time
    return f"update-{time.strftime('%Y%m%d-%H%M%S', time.localtime(STAMP))}.log"


# ---------------------------------------------------------------------------
# Agent de mise à jour
# ---------------------------------------------------------------------------
def test_agent_log_link_is_neither_written_nor_handed_over(agent_env, chowns, tmp_path):
    ag, dirs = agent_env
    victim = tmp_path / "authorized_keys"
    victim.write_text("root key\n")
    (dirs["log"] / log_name(ag)).symlink_to(victim)
    assert run(ag, {"archive": "not-a-release"}, dirs=dirs) == 1
    assert victim.read_text() == "root key\n"
    assert ino(victim) not in chowns
    st = json.loads((dirs["state"] / "updates" / "status.json").read_text())
    assert st["state"] == "failed" and "bad archive name" in st["message"]
    # le journal existe, sous un autre nom, et c'est un vrai fichier
    log = dirs["log"] / st["log"]
    assert st["log"] != log_name(ag) and log.is_file() and not log.is_symlink()
    assert "bad archive name" in log.read_text()


def test_agent_status_swapped_for_a_link_after_writing_is_not_handed_over(agent_env, chowns, tmp_path,
                                                                         monkeypatch):
    """The console replaces status.json by a link right after root renamed it
    into place: the hand-over must not follow it."""
    ag, dirs = agent_env
    victim = tmp_path / "agent-script.py"
    victim.write_text("root code\n")
    upd = dirs["state"] / "updates"
    real_replace, real_rename = os.replace, os.rename

    def swap(fn):
        def wrapper(src, dst, *a, **k):
            fn(src, dst, *a, **k)
            if str(dst).endswith("status.json"):
                (upd / "status.json").unlink()
                (upd / "status.json").symlink_to(victim)
        return wrapper

    monkeypatch.setattr(os, "replace", swap(real_replace))
    monkeypatch.setattr(os, "rename", swap(real_rename))
    run(ag, {"archive": "not-a-release"}, dirs=dirs)
    assert victim.read_text() == "root code\n"
    assert ino(victim) not in chowns


def test_agent_refuses_an_updates_directory_that_is_a_link(agent_env, chowns, tmp_path):
    ag, dirs = agent_env
    outside = tmp_path / "etc-somewhere"
    outside.mkdir()
    (outside / "request.json").write_text(json.dumps({"archive": "x"}))
    upd = dirs["state"] / "updates"
    os.rename(upd, tmp_path / "old-updates")
    upd.symlink_to(outside)
    assert run(ag, dirs=dirs) == 1
    assert sorted(p.name for p in outside.iterdir()) == ["request.json"]
    assert ino(outside) not in chowns
    assert not upd.is_symlink()          # le lien posé est retiré : l'unité de chemin ne boucle pas


def test_agent_refuses_a_staging_directory_that_is_a_link(agent_env, keys, chowns, tmp_path):
    ag, dirs = agent_env
    outside = tmp_path / "outside"
    outside.mkdir()
    a = make_release(outside, "1.82.0")
    sign(keys, a)
    staged = dirs["state"] / "updates" / "staged"
    staged.rmdir()
    staged.symlink_to(outside)
    host = FakeHost(dirs, "1.82.0")
    rc = run(ag, {"archive": a.name}, probe=lambda run: (True, (dirs["opt"] / "VERSION").read_text().strip()),
             host=host)
    st = json.loads((dirs["state"] / "updates" / "status.json").read_text())
    assert rc == 1 and st["state"] == "failed" and "staging" in st["message"], st
    assert a.exists() and Path(str(a) + ".sig").exists()      # rien d'effacé dehors
    assert not any(c[0] == "bash" for c in host.calls)


def test_agent_does_not_read_a_request_through_a_link(agent_env, chowns, tmp_path):
    ag, dirs = agent_env
    secret = tmp_path / "root-only.json"
    secret.write_text(json.dumps({"archive": "x", "requested_by": "root-only-content"}))
    (dirs["state"] / "updates" / "request.json").symlink_to(secret)
    assert run(ag, dirs=dirs) == 1
    st = json.loads((dirs["state"] / "updates" / "status.json").read_text())
    assert st["requested_by"] is None and "unreadable request" in st["message"]
    assert secret.exists() and not (dirs["state"] / "updates" / "request.json").exists()


def test_agent_log_lines_cannot_be_split_by_archive_content(agent_env, keys, chowns, tmp_path):
    """The archive is read before its signature is checked: a crafted
    directory name must stay on one log line."""
    ag, dirs = agent_env
    staged = dirs["state"] / "updates" / "staged"
    a = make_release(staged, "1.82.0", top="x\nssh-ed25519 AAAAinjected console\r")
    sign(keys, a)
    assert run(ag, {"archive": a.name}, dirs=dirs) == 1
    st = json.loads((dirs["state"] / "updates" / "status.json").read_text())
    lines = (dirs["log"] / st["log"]).read_text().splitlines()
    assert not any(line.startswith("ssh-ed25519") for line in lines), lines
    assert any("ssh-ed25519 AAAAinjected" in line and "\\x0a" in line for line in lines), lines


def test_agent_status_and_log_are_root_files_the_console_reads(agent_env, chowns):
    ag, dirs = agent_env
    run(ag, {"archive": "not-a-release"}, dirs=dirs)
    upd = dirs["state"] / "updates"
    st = json.loads((upd / "status.json").read_text())
    for p in (upd / "status.json", dirs["log"] / st["log"]):
        m = os.stat(p, follow_symlinks=False)
        assert stat.S_ISREG(m.st_mode) and m.st_nlink == 1
        assert not m.st_mode & 0o022, oct(m.st_mode)           # personne d'autre n'écrit


# ---------------------------------------------------------------------------
# install.sh : étapes root rejouées à chaque --upgrade
# ---------------------------------------------------------------------------
def shell_fn(src, name):
    return src[src.index(f"\n{name}() {{") + 1:].split("\n}\n", 1)[0] + "\n}\n"


def run_install_fn(tmp_path, call, script_dir, state, extra_env=""):
    src = (ROOT / "install.sh").read_text()
    names = ["detect_update_python", "render_update_service", "install_update_agent",
             "install_bundles"]
    if "_safe_fs() {" in src:
        names.append("_safe_fs")
    body = "".join(shell_fn(src, n) for n in names)
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    body = body.replace("/etc/systemd/system", str(units))
    script = ("set -eo pipefail\nok(){ echo \"OK $*\"; }\nwarn(){ echo \"WARN $*\"; }\n"
              "err(){ echo \"ERR $*\" >&2; }\nsystemctl(){ :; }\n"
              f"SCRIPT_DIR={script_dir}\nSTATE_DIR={state}\nINSTALL_DIR={tmp_path / 'opt'}\n"
              f"{extra_env}\n{body}\n{call}\n")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def full_livrable(tmp_path):
    d = livrable(tmp_path)
    lv = d / "bundles" / "terraform-provider-libvirt"
    (lv / "bin").mkdir(parents=True)
    (lv / "bin" / "terraform-provider-libvirt").write_text("#!/bin/sh\n")
    (lv / "bin" / "terraform-provider-libvirt").chmod(0o755)
    (lv / "provider.json").write_text(json.dumps({"version": "0.9.0"}))
    rb = d / "bundles" / "registry"
    rb.mkdir(parents=True)
    for n in ("registry-images.tar", "registry-images.json"):
        (rb / n).write_text(n)
    subprocess.run("sha256sum registry-images.tar registry-images.json > SHA256SUMS", shell=True,
                   cwd=rb, check=True)
    (d / "bin" / "lib").mkdir(parents=True)
    (d / "bin" / "lib" / "safe_fs.py").symlink_to(ROOT / "bin" / "lib" / "safe_fs.py")
    return d


def plant_dir_link(state, rel, tmp_path):
    victim_dir = tmp_path / "victim-dir"
    victim_dir.mkdir()
    victim_dir.chmod(0o755)
    link = state / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(victim_dir)
    return victim_dir


def plant_file_link(state, rel, tmp_path, content='{"source": "embedded in 2.8.2"}\n'):
    victim = tmp_path / "victim-file"
    victim.write_text(content)
    victim.chmod(0o600)
    link = state / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(victim)
    return victim


# (fonction, chemin planté sous l'état, nature du lien, ce qui doit exister ensuite)
INSTALL_CASES = [
    ("install_update_agent", "updates", "dir", "updates/agent.json"),
    ("install_update_agent", "updates/staged", "dir", "updates/staged"),
    ("install_update_agent", "updates/agent.json", "file", "updates/agent.json"),
    ("install_bundles", "capi-bundles", "dir", "capi-bundles/active.json"),
    ("install_bundles", "capi-bundles/active.json", "file", "capi-bundles/active.json"),
    ("install_bundles", ".local", "dir", ".local/share/harvester-ops/terraform-provider/provider.json"),
    ("install_bundles", ".local/share/harvester-ops/terraform-provider/provider.json", "file",
     ".local/share/harvester-ops/terraform-provider/provider.json"),
    ("install_bundles", ".local/share/harvester-ops/terraform-provider-libvirt", "dir",
     ".local/share/harvester-ops/terraform-provider-libvirt/provider.json"),
    ("install_bundles", ".local/share/harvester-ops/terraform-provider-libvirt/provider.json", "file",
     ".local/share/harvester-ops/terraform-provider-libvirt/provider.json"),
    ("install_bundles", "registry-bundle", "dir", "registry-bundle/SHA256SUMS"),
]


@pytest.mark.parametrize("fn,rel,kind,expect", INSTALL_CASES, ids=[f"{c[0]}:{c[1]}" for c in INSTALL_CASES])
def test_install_steps_never_follow_a_planted_link(tmp_path, fn, rel, kind, expect):
    state = tmp_path / "state"
    state.mkdir()
    script_dir = full_livrable(tmp_path) if fn == "install_bundles" else ROOT
    if kind == "dir":
        victim = plant_dir_link(state, rel, tmp_path)
        before = (stat.S_IMODE(victim.stat().st_mode), sorted(os.listdir(victim)))
    else:
        victim = plant_file_link(state, rel, tmp_path)
        before = (stat.S_IMODE(victim.stat().st_mode), victim.read_text())
    r = run_install_fn(tmp_path, fn, script_dir, state)
    if kind == "dir":
        after = (stat.S_IMODE(victim.stat().st_mode), sorted(os.listdir(victim)))
    else:
        after = (stat.S_IMODE(victim.stat().st_mode), victim.read_text())
    assert after == before, (r.stdout, r.stderr)
    assert r.returncode == 0, r.stderr
    # le résultat attendu est là, en vrai fichier ou répertoire de l'état
    p = state / expect
    assert not p.is_symlink() and p.exists(), (r.stdout, r.stderr)
    for parent in p.relative_to(state).parents:
        assert not (state / parent).is_symlink()


def test_install_update_agent_writes_its_marker(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    r = run_install_fn(tmp_path, "install_update_agent", ROOT, state)
    assert r.returncode == 0, r.stderr
    marker = json.loads((state / "updates" / "agent.json").read_text())
    assert marker["installed"] == (ROOT / "VERSION").read_text().strip()
    assert stat.S_IMODE((state / "updates" / "agent.json").stat().st_mode) == 0o644
    assert (state / "updates" / "staged").is_dir()


ROOT_STEPS = ("install_update_agent", "install_bundles")


@pytest.mark.parametrize("name", ROOT_STEPS)
def test_install_root_steps_touch_the_state_only_through_the_helper(name):
    """Source check: in the steps replayed by --upgrade, nothing under
    $STATE_DIR is written by a shell redirection, created by install(1),
    handed over by chown/chgrp or chmod-ed: only `_safe_fs` touches it."""
    body = shell_fn((ROOT / "install.sh").read_text(), name)
    offenders = [line.strip() for line in body.splitlines()
                 if "STATE_DIR" in line and not line.strip().startswith("#")
                 and not line.strip().startswith("_safe_fs")
                 and line.strip() != 'install -d -m 0750 "$STATE_DIR"'
                 and any(tok in line for tok in (">", "install ", "chown", "chgrp", "chmod", "cp ", "mv ",
                                                  "open(", "grep "))]
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# bin/lib/safe_fs.py : chaque primitive, chaque chemin d'erreur
# ---------------------------------------------------------------------------
import safe_fs as sf  # noqa: E402


@pytest.fixture
def base(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("host file\n")
    victim.chmod(0o600)
    vdir = tmp_path / "victim-dir"
    vdir.mkdir()
    with sf.SafeDir.open_base(d) as b:
        yield b, d, victim, vdir


def _untouched(victim, vdir):
    assert victim.read_text() == "host file\n" and stat.S_IMODE(victim.stat().st_mode) == 0o600
    assert os.listdir(vdir) == []


def case_subdir_refuses_a_link(b, d, victim, vdir):
    (d / "x").symlink_to(vdir)
    with pytest.raises(sf.UnsafePath):
        b.subdir("x", create_mode=0o750)
    assert (d / "x").is_symlink()


def case_subdir_replaces_a_link_when_asked(b, d, victim, vdir):
    (d / "x").symlink_to(vdir)
    b.subdir("x", create_mode=0o750, replace=True).close()
    assert (d / "x").is_dir() and not (d / "x").is_symlink()
    assert stat.S_IMODE((d / "x").stat().st_mode) == 0o750


def case_subdir_replaces_a_file_link(b, d, victim, vdir):
    (d / "x").symlink_to(victim)
    b.subdir("x", create_mode=0o750, replace=True).close()
    assert (d / "x").is_dir()


def case_makedirs_through_a_link_component(b, d, victim, vdir):
    (d / "a").symlink_to(vdir)
    b.makedirs("a/b/c", 0o750).close()
    assert (d / "a" / "b" / "c").is_dir() and not (d / "a").is_symlink()


def case_write_replaces_a_link(b, d, victim, vdir):
    (d / "f.json").symlink_to(victim)
    b.write("f.json", b"{}", 0o644)
    assert (d / "f.json").read_bytes() == b"{}" and not (d / "f.json").is_symlink()
    assert stat.S_IMODE((d / "f.json").stat().st_mode) == 0o644


def case_write_sets_a_planted_directory_aside(b, d, victim, vdir):
    """A directory planted at the target name would make the rename fail
    and block every upgrade: it is moved aside, never followed."""
    (d / "f.json").mkdir()
    (d / "f.json" / "inside").write_text("console")
    b.write("f.json", b"{}", 0o644)
    assert (d / "f.json").read_bytes() == b"{}"
    aside = [n for n in os.listdir(d) if n.startswith(".f.json.set-aside.")]
    assert len(aside) == 1 and (d / aside[0] / "inside").read_text() == "console"
    assert sorted(os.listdir(d)) == sorted(["f.json"] + aside)       # aucun temporaire laissé


def case_read_refuses_a_link(b, d, victim, vdir):
    (d / "r.json").symlink_to(victim)
    with pytest.raises(sf.UnsafePath):
        b.read("r.json")


def case_read_refuses_a_fifo_without_blocking(b, d, victim, vdir):
    os.mkfifo(d / "r.json")
    with pytest.raises(sf.UnsafePath):
        b.read("r.json")


def case_read_refuses_too_large(b, d, victim, vdir):
    (d / "r.json").write_bytes(b"x" * 20)
    with pytest.raises(sf.UnsafePath):
        b.read("r.json", limit=10)


def case_read_refuses_a_hard_link(b, d, victim, vdir):
    os.link(victim, d / "r.json")
    with pytest.raises(sf.UnsafePath):
        b.read("r.json")
    os.unlink(d / "r.json")


def case_read_absent_is_none(b, d, victim, vdir):
    assert b.read("nothing") is None


def case_append_refuses_a_planted_name(b, d, victim, vdir):
    (d / "x.log").symlink_to(victim)
    with pytest.raises(FileExistsError):
        b.open_new_append("x.log", 0o640)


def case_names_are_single_components(b, d, victim, vdir):
    for bad in ("", ".", "..", "a/b", "../victim"):
        with pytest.raises(sf.UnsafePath):
            b.write(bad, b"x", 0o644) if bad not in ("",) else b.subdir(bad)


def case_unlink_never_follows(b, d, victim, vdir):
    (d / "l").symlink_to(victim)
    b.unlink("l")
    b.unlink("absent")
    assert not (d / "l").exists() and victim.exists()


def case_open_base_refuses_a_link(b, d, victim, vdir):
    link = d.parent / "base-link"
    link.symlink_to(vdir)
    with pytest.raises(sf.UnsafePath):
        sf.SafeDir.open_base(link)


SAFE_CASES = [v for k, v in sorted(globals().items()) if k.startswith("case_")]


@pytest.mark.parametrize("case", SAFE_CASES, ids=[c.__name__[5:] for c in SAFE_CASES])
def test_safe_fs_primitives(base, case):
    b, d, victim, vdir = base
    case(b, d, victim, vdir)
    _untouched(victim, vdir)


def test_chown_tree_changes_only_plain_entries(base, chowns):
    b, d, victim, vdir = base
    (d / "sub").mkdir()
    (d / "sub" / "plain").write_text("x")
    (d / "sub" / "to-file").symlink_to(victim)
    (d / "sub" / "to-dir").symlink_to(vdir)
    own = d / "sub" / "mine"
    own.write_text("x")
    os.link(own, d / "sub" / "hardlink")              # deux liens physiques
    os.mkfifo(d / "sub" / "fifo")
    b.chown_tree((os.getuid(), os.getgid()))
    changed = set(chowns)
    assert ino(d) in changed and ino(d / "sub") in changed and ino(d / "sub" / "plain") in changed
    for p in (victim, vdir, own, d / "sub" / "fifo"):
        assert ino(p) not in changed, p


@pytest.mark.parametrize("text,expect", [
    ("a\nb", "a\\x0ab"),
    ("a\rb", "a\\x0db"),
    ("\x1b[31mred\x1b[0m", "red"),
    ("nul\x00", "nul\\x00"),
    ("sep x", "sep\\u2028x"),
    ("bidi‮evil​", "bidi\\u202eevil\\u200b"),
    ("tab\tok é", "tab\tok é"),
    ("x" * 10, "xxxx..."),
])
def test_clean_line(text, expect):
    assert sf.clean_line(text, limit=4 if expect.endswith("...") else 4000) == expect


@pytest.mark.parametrize("argv,rc", [
    (["exists", "{s}", "f"], 0),
    (["exists", "{s}", "absent"], 3),
    (["exists", "{s}", "link"], 3),
    (["read", "{s}", "absent/f"], 3),
    (["read", "{s}", "link"], 1),
    (["chown-tree", "{s}", "absent"], 0),          # sans --owner : rien à faire
    (["bogus", "{s}", "f"], 2),
])
def test_safe_fs_cli_exit_codes(tmp_path, argv, rc):
    s = tmp_path / "s"
    s.mkdir()
    (s / "f").write_text("x")
    (s / "link").symlink_to(s / "f")
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "lib" / "safe_fs.py")]
                       + [a.format(s=s) for a in argv], capture_output=True, text=True)
    assert r.returncode == rc, r.stderr


def test_safe_fs_cli_mkdir_sets_the_mode_of_an_existing_directory(tmp_path):
    s = tmp_path / "s"
    (s / "updates").mkdir(parents=True)
    (s / "updates").chmod(0o700)
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "lib" / "safe_fs.py"), "mkdir", str(s),
                        "updates", "0750"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE((s / "updates").stat().st_mode) == 0o750


def test_agent_source_has_no_path_based_write_or_chown():
    src = (ROOT / "bin" / "harvester-ops-update.py").read_text()
    for bad in ("shutil.chown", "open(self.log_path", "write_json_atomic", "STAGED).resolve()", "_give_to_service"):
        assert bad not in src, bad


def test_uninstall_only_removes_console_paths():
    """uninstall.sh does nothing but rm -rf on the console's directories
    (rm does not follow links below its arguments)."""
    for line in (ROOT / "uninstall.sh").read_text().splitlines():
        if ("STATE_DIR" in line or "LOG_DIR" in line) and not line.strip().startswith(("#", "info", "warn",
                                                                                        "--purge", "LOG_DIR=",
                                                                                        "STATE_DIR=")):
            assert line.strip().startswith("rm -rf "), line


def test_chown_tree_is_done_on_the_opened_entry_not_its_name(base, chowns, monkeypatch):
    """The console swaps a plain file for a link between the moment root
    opens it and the change of owner: the change hits what was opened."""
    b, d, victim, vdir = base
    (d / "plain").write_text("x")
    real_fstat = os.fstat
    swapped = []

    def racing_fstat(fd):
        st = real_fstat(fd)
        if not swapped and (st.st_dev, st.st_ino) == ino(d / "plain"):
            swapped.append(1)
            (d / "plain").unlink()
            (d / "plain").symlink_to(victim)
        return st

    monkeypatch.setattr(os, "fstat", racing_fstat)
    b.chown_tree((os.getuid(), os.getgid()))
    assert swapped and ino(victim) not in chowns


def test_agent_refuses_an_archive_that_is_a_link(agent_env, keys, chowns, tmp_path):
    ag, dirs = agent_env
    a = make_release(tmp_path, "1.82.0")
    sign(keys, a)
    staged = dirs["state"] / "updates" / "staged"
    (staged / a.name).symlink_to(a)
    (staged / (a.name + ".sig")).symlink_to(str(a) + ".sig")
    host = FakeHost(dirs, "1.82.0")
    rc = run(ag, {"archive": a.name}, host=host,
             probe=lambda run: (True, (dirs["opt"] / "VERSION").read_text().strip()))
    st = json.loads((dirs["state"] / "updates" / "status.json").read_text())
    assert rc == 1 and "staging" in st["message"] and not any(c[0] == "bash" for c in host.calls)


def test_chown_tree_never_opens_a_directory_name_without_nofollow(base, chowns, monkeypatch):
    """The console swaps a subdirectory for a link (to a device, say) after
    root listed it: root never opens that name in a way that follows it."""
    b, d, victim, vdir = base
    (d / "sub").mkdir()
    real_stat, real_open = os.stat, os.open
    opened = []

    def racing_stat(path, *a, **k):
        st = real_stat(path, *a, **k)
        if path == "sub" and not (d / "sub").is_symlink():
            (d / "sub").rmdir()
            (d / "sub").symlink_to(vdir)
        return st

    def spy_open(path, flags, *a, **k):
        if path == "sub":
            opened.append(flags)
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "stat", racing_stat)
    monkeypatch.setattr(os, "open", spy_open)
    b.chown_tree((os.getuid(), os.getgid()))
    assert opened and all(f & os.O_NOFOLLOW and f & os.O_NOCTTY for f in opened)
    assert ino(vdir) not in chowns


def test_agent_sets_aside_a_directory_planted_as_the_request(agent_env, chowns):
    """Otherwise the path unit fires again and again until systemd stops it."""
    ag, dirs = agent_env
    upd = dirs["state"] / "updates"
    (upd / "request.json").mkdir()
    assert run(ag, dirs=dirs) == 1
    assert not (upd / "request.json").exists()
    st = json.loads((upd / "status.json").read_text())
    assert "unreadable request" in st["message"]


def test_agent_refuses_a_hard_linked_archive_with_a_clear_message(agent_env, keys, chowns, tmp_path):
    ag, dirs = agent_env
    staged = dirs["state"] / "updates" / "staged"
    a = make_release(staged, "1.82.0")
    sign(keys, a)
    os.link(a, tmp_path / "second-name.tar.gz")
    host = FakeHost(dirs, "1.82.0")
    assert run(ag, {"archive": a.name}, host=host) == 1
    st = json.loads((dirs["state"] / "updates" / "status.json").read_text())
    assert "several hard links" in st["message"] and not any(c[0] == "bash" for c in host.calls)


def test_agent_copy_stops_at_the_size_seen_at_open(agent_env, keys, chowns, monkeypatch):
    """The console owns the staged archive and could keep it growing while
    root copies it into its own work directory."""
    ag, dirs = agent_env
    staged = dirs["state"] / "updates" / "staged"
    a = make_release(staged, "1.82.0")
    sign(keys, a)
    real_open_plain = ag._open_plain

    def growing(d, name):
        f = real_open_plain(d, name)
        if name == a.name:
            with open(a, "ab") as g:
                g.write(b"x" * 4096)
        return f

    monkeypatch.setattr(ag, "_open_plain", growing)
    host = FakeHost(dirs, "1.82.0")
    assert run(ag, {"archive": a.name}, host=host) == 1
    st = json.loads((dirs["state"] / "updates" / "status.json").read_text())
    assert "changed while it was copied" in st["message"]


def test_agent_status_messages_are_cleaned(agent_env, keys, chowns):
    ag, dirs = agent_env
    staged = dirs["state"] / "updates" / "staged"
    a = make_release(staged, "1.82.0", top="x\nforged\u202e")
    sign(keys, a)
    run(ag, {"archive": a.name}, dirs=dirs)
    st = json.loads((dirs["state"] / "updates" / "status.json").read_text())
    assert "\n" not in st["message"] and "\u202e" not in st["message"] and "\\x0a" in st["message"]


def test_safe_fs_runs_without_the_agent_python(tmp_path):
    """The embedded bundles still install when no Python >= 3.9 is found
    (the agent step alone requires it)."""
    src = (ROOT / "install.sh").read_text()
    fn = shell_fn(src, "_safe_fs")
    (tmp_path / "s").mkdir()
    lib = tmp_path / "rel" / "bin" / "lib"
    lib.mkdir(parents=True)
    (lib / "safe_fs.py").symlink_to(ROOT / "bin" / "lib" / "safe_fs.py")
    script = (f"set -eo pipefail\ndetect_update_python(){{ return 2; }}\nSCRIPT_DIR={tmp_path / 'rel'}\n{fn}\n"
              f"_safe_fs mkdir {tmp_path / 's'} a/b 0750\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "s" / "a" / "b").is_dir()
