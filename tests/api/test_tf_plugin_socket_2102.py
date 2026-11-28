"""2.10.2 : socket des providers d'OpenTofu hors de l'espace de travail.

Vu au banc 2.10.1 : go-plugin crée le socket Unix du provider dans
`PLUGIN_UNIX_SOCKET_DIR`, à défaut dans `TMPDIR` ; avec TMPDIR sous l'espace
(`<espace>/.tmp`), un chemin d'environ 110 caractères dépasse la limite d'une
adresse Unix (108 octets) et la poignée de main échoue (« Failed to read any
lines from plugin's stdout »). TMPDIR reste dans l'espace (le provider
libvirt y écrit les ISO cloud-init, dont le chemin est dans le plan gardé) ;
le socket va dans un répertoire court, propre à chaque commande, retiré à la
fin. Les deux providers embarqués lisent la variable (go-plugin v1.7.0 et
v1.8.0).
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402

# Comme go-plugin (server.go, serverListener_unix) : le répertoire de la
# variable, sinon os.TempDir() (TMPDIR) ; nom `plugin` + 10 chiffres
FAKE_TOFU = r"""#!/usr/bin/env python3
import json, os, socket, sys
d = os.environ.get("PLUGIN_UNIX_SOCKET_DIR") or os.environ.get("TMPDIR") or "/tmp"
path = os.path.join(d, "plugin1234567890")
out = {"dir": d, "tmpdir": os.environ.get("TMPDIR"), "mode": oct(os.stat(d).st_mode & 0o777)}
s = socket.socket(socket.AF_UNIX)
try:
    s.bind(path)
    out["bound"] = True
except OSError as e:
    out["bound"] = False
    out["error"] = str(e)
finally:
    s.close()
    try:
        os.unlink(path)
    except OSError:
        pass
print(json.dumps(out))
"""


@pytest.fixture
def fake_tofu(tmp_path, monkeypatch):
    exe = tmp_path / "tofu"
    exe.write_text(FAKE_TOFU)
    exe.chmod(0o755)
    monkeypatch.setattr(wapp, "TF_BIN", str(exe))
    return exe


def long_ws(tmp_path, total):
    """Un espace dont `<espace>/.tmp/plugin1234567890` fait `total` caractères."""
    base = tmp_path / "w"
    room = total - len(str(base)) - len("/.tmp/plugin1234567890")
    ws = base / ("d" * max(1, room))
    ws.mkdir(parents=True)
    return ws


@pytest.mark.parametrize("total", [80, 120, 200])
def test_the_provider_socket_binds_whatever_the_workspace_length(tmp_path, fake_tofu, total):
    ws = long_ws(tmp_path, total)
    rc, out, err = wapp._tf_run_cmd(ws, "", ["version"], timeout=30)
    assert rc == 0, err
    got = json.loads(out)
    assert got["bound"], got
    assert got["tmpdir"] == str(ws / ".tmp")            # TMPDIR reste dans l'espace (ISO cloud-init)
    assert not got["dir"].startswith(str(ws))          # le socket, lui, n'y est pas
    assert got["mode"] == "0o700"
    assert not Path(got["dir"]).exists()               # répertoire de la commande retiré


def test_the_socket_dir_is_removed_on_timeout(tmp_path, monkeypatch):
    exe = tmp_path / "tofu"
    exe.write_text("#!/bin/sh\necho \"$PLUGIN_UNIX_SOCKET_DIR\" > \"$TMPDIR/where\"\nsleep 5\n")
    exe.chmod(0o755)
    monkeypatch.setattr(wapp, "TF_BIN", str(exe))
    ws = long_ws(tmp_path, 60)
    rc, _out, err = wapp._tf_run_cmd(ws, "", ["version"], timeout=1)
    assert rc == 124 and err == "timeout"
    where = (ws / ".tmp" / "where").read_text().strip()
    assert where and not Path(where).exists()


def test_the_socket_dir_is_short_even_with_a_long_console_tmpdir(tmp_path, monkeypatch, fake_tofu):
    deep = tmp_path / ("t" * 150)
    deep.mkdir()
    monkeypatch.setenv("TMPDIR", str(deep))
    ws = long_ws(tmp_path, 60)
    rc, out, err = wapp._tf_run_cmd(ws, "", ["version"], timeout=30)
    assert rc == 0, err
    got = json.loads(out)
    assert got["bound"] and len(got["dir"]) <= wapp.TF_SOCKET_DIR_MAX, got


def test_a_caller_cannot_point_the_socket_back_into_the_workspace(tmp_path, fake_tofu):
    ws = long_ws(tmp_path, 200)
    rc, out, err = wapp._tf_run_cmd(ws, "", ["version"], timeout=30,
                                    extra_env={"TMPDIR": str(ws / ".tmp")})      # comme une cible KVM
    assert rc == 0, err
    assert json.loads(out)["bound"]


def test_the_socket_dir_is_private(tmp_path, fake_tofu):
    d = wapp._tf_socket_dir()
    try:
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
        assert len(d) <= wapp.TF_SOCKET_DIR_MAX
    finally:
        os.rmdir(d)


class _Boom(Exception):
    pass


@pytest.mark.parametrize("label,session_of,want", [
    ("OpenBao child token refused", "secret-error", (125, "state encryption: OpenBao child token unavailable")),
    ("session lookup raises", "raise", None),
])
def test_the_socket_dir_is_removed_when_the_state_session_fails(tmp_path, monkeypatch, fake_tofu, label,
                                                                session_of, want):
    """Revue M-7 : l'espace chiffré dont la session OpenBao échoue (refus
    connu ou erreur inattendue) ne laisse pas de répertoire de sockets."""
    made = []
    real = wapp._tf_socket_dir
    monkeypatch.setattr(wapp, "_tf_socket_dir", lambda: made.append(real()) or made[-1])
    ws = long_ws(tmp_path, 60)
    (ws / wapp._tfe.ENCRYPTION_FILE).write_text("")
    monkeypatch.setattr(wapp._tfg, "check_workspace", lambda *a, **kw: [])

    class Sess:
        def env(self):
            raise wapp._sst.SecretError("bao-down", "no token")

    def lookup(ws_dir):
        if session_of == "raise":
            raise _Boom("unexpected")
        return Sess()
    monkeypatch.setattr(wapp, "_tf_bao_session_of", lookup)
    if want is None:
        with pytest.raises(_Boom):
            wapp._tf_run_cmd(ws, "", ["version"], timeout=30)
    else:
        rc, _out, err = wapp._tf_run_cmd(ws, "", ["version"], timeout=30)
        assert (rc, err.split(" (")[0]) == want, label
    assert made and not Path(made[0]).exists(), label
