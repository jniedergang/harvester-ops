"""2.9.0, relecture finale I6 : `sudo harvester-secrets ... --bundle/--ca`.
Root ouvre le fichier tapé avant de quitter ses droits ; un chemin que la
console contrôle (le fichier, un répertoire parent, un lien) pouvait lui
faire lire un fichier de root (lien vers /etc/shadow posé dans staged/ :
avec --ca, tout fichier qui contient un certificat finissait dans ca.pem).
Désormais root n'ouvre que ce que la console ne peut pas avoir posé ;
sinon le fils l'ouvre sous le compte de la console."""
import importlib.util
import os
import stat
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
spec = importlib.util.spec_from_file_location("hsc_rootopen", ROOT / "bin" / "harvester-secrets.py")
hsc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hsc)

OTHER = 987654          # un compte de console qui n'est pas nous
ME = os.getuid()


def acct(uid, gid=None):
    return types.SimpleNamespace(pw_uid=uid, pw_gid=OTHER if gid is None else gid)


@pytest.fixture
def tree(tmp_path):
    d = tmp_path / "admin"
    d.mkdir(mode=0o755)
    f = d / "bundle.tar.gz"
    f.write_bytes(b"x")
    f.chmod(0o600)
    return d, f


def test_plain_file_of_the_admin_is_opened_by_root(tree):
    _d, f = tree
    assert hsc._root_may_open(str(f), acct(OTHER)) is True


@pytest.mark.parametrize("case", ["final-link", "parent-link", "file-of-console", "parent-of-console",
                                  "parent-writable-by-console-group", "world-writable-parent", "fifo",
                                  "relative"])
def test_paths_the_console_controls_are_left_to_the_child(tree, tmp_path, case):
    d, f = tree
    a = acct(OTHER)
    path = str(f)
    if case == "final-link":
        (d / "l").symlink_to(f)
        path = str(d / "l")
    elif case == "parent-link":
        (tmp_path / "ldir").symlink_to(d)
        path = str(tmp_path / "ldir" / f.name)
    elif case == "file-of-console":
        a = acct(ME)
        d.chmod(0o755)
    elif case == "parent-of-console":
        a = acct(ME)
    elif case == "parent-writable-by-console-group":
        a = acct(OTHER, gid=os.stat(d).st_gid)
        d.chmod(0o775)
    elif case == "world-writable-parent":
        d.chmod(0o777)
    elif case == "fifo":
        os.mkfifo(d / "p")
        path = str(d / "p")
    elif case == "relative":
        path = "bundle.tar.gz"
    assert hsc._root_may_open(path, a) is False


def test_sticky_world_writable_parent_is_allowed(tree):
    d, f = tree
    d.chmod(0o1777)
    assert hsc._root_may_open(str(f), acct(OTHER)) is True


def test_preopen_skips_unsafe_paths(tree, monkeypatch):
    d, f = tree
    (d / "l").symlink_to(f)
    monkeypatch.setattr(hsc, "_console_account", lambda: acct(OTHER))
    hsc.INPUT_FDS.clear()
    args = types.SimpleNamespace(ca=str(d / "l"), bundle=str(f), cmd="enable")
    hsc._preopen(args)
    try:
        assert str(f) in hsc.INPUT_FDS and str(d / "l") not in hsc.INPUT_FDS
        assert stat.S_ISREG(os.fstat(hsc.INPUT_FDS[str(f)]).st_mode)
    finally:
        for fd in hsc.INPUT_FDS.values():
            os.close(fd)
        hsc.INPUT_FDS.clear()
