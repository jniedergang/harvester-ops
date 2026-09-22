"""v1.44.1 : le service packagé démarre, lit sa configuration et garde son état.

Constaté en lançant l'unité systemd telle qu'installée (podman rootful,
système de fichiers en lecture seule, répertoires créés par install.sh) :

  * l'application plantait au démarrage, en créant `/var/lib/harvester-ops`
    sur un système de fichiers en lecture seule (`Read-only file system`,
    que seul `PermissionError` était prévu de rattraper). Vrai depuis la
    première version publique : le service packagé n'avait jamais démarré ;
  * le conteneur tourne sous un compte non root, et install.sh rendait la
    clé TLS, le htpasswd et les kubeconfigs lisibles par root seul ;
  * aucun volume persistant : historique des actions, notes et photo de la
    surveillance vivaient en mémoire (`/tmp`), perdus à chaque redémarrage.

Ces tests gardent le contrat au niveau des sources ; la preuve en réel est
dans le CHANGELOG (unité lancée sur un hôte, commandes extraites du fichier).
"""

import errno
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
UNIT = (ROOT / "config" / "systemd" / "harvester-ops.service").read_text()
INSTALL = (ROOT / "install.sh").read_text()
UNINSTALL = (ROOT / "uninstall.sh").read_text()

sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


def _joined(unit):
    """Les lignes continuées par `\\` comme systemd les lit."""
    return re.sub(r"\\\n\s*", " ", unit)


def _exec(prefix):
    return [line.split("=", 1)[1] for line in _joined(UNIT).splitlines()
            if line.startswith(prefix + "=")]


# ---------------------------------------------------------------------------
# L'unité systemd
# ---------------------------------------------------------------------------

def test_the_state_has_a_persistent_volume():
    start = " ".join(_exec("ExecStart"))
    assert "-v /var/lib/harvester-ops:/var/lib/harvester-ops:rw,Z" in start


def test_the_container_runs_as_the_service_account():
    """Le compte est résolu sur l'hôte : l'uid 1001 de l'image peut y être
    celui d'une vraie personne."""
    start = " ".join(_exec("ExecStart"))
    assert "u=$$(id -u harvester-ops); g=$$(id -g harvester-ops);" in start
    assert '--user "$$u:$$g"' in start
    # kubectl écrit sous HOME, ssh sous le répertoire de /etc/passwd : les
    # deux dans le volume persistant. Sinon ssh ne retient aucune clé
    # d'hôte (constaté : « Failed to add the host to the list of known
    # hosts ») et en accepte une nouvelle à chaque connexion.
    assert "-e HOME=/var/lib/harvester-ops" in start
    assert ('--passwd-entry "harvester-ops:*:$$u:$$g:harvester-ops web UI:'
            '/var/lib/harvester-ops:/bin/sh"') in start


def test_the_container_stays_locked_down():
    start = " ".join(_exec("ExecStart"))
    assert "--read-only" in start and "--tmpfs /tmp" in start
    assert "-v /etc/harvester-ops:/etc/harvester-ops:ro,Z" in start


def test_permissions_are_prepared_before_each_start():
    """Une kubeconfig copiée à la main plus tard (en 0600 root) doit
    devenir lisible par le service au démarrage suivant, sans que rien ne
    devienne lisible par les autres comptes de l'hôte."""
    pre = " ".join(_exec("ExecStartPre"))
    assert "chgrp -R harvester-ops /etc/harvester-ops" in pre
    assert "chmod -R g+rX,g-w,o-rwx /etc/harvester-ops" in pre
    assert ("install -d -m 0750 -o harvester-ops -g harvester-ops "
            "/var/lib/harvester-ops /var/log/harvester-ops") in pre


def test_the_start_command_is_one_shell_exec():
    """`$$(...)` n'est interprété que par un shell ; `exec` garde podman
    comme processus principal du service (Type=simple)."""
    starts = _exec("ExecStart")
    assert len(starts) == 1
    assert starts[0].startswith("/bin/sh -c 'u=")
    assert " exec /usr/bin/podman run --rm " in starts[0]
    assert starts[0].rstrip().endswith("serve'")


# ---------------------------------------------------------------------------
# install.sh et uninstall.sh
# ---------------------------------------------------------------------------

def _fn(src, name):
    return src.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]


def test_install_creates_a_system_account():
    body = _fn(INSTALL, "setup_service_account")
    assert "groupadd --system harvester-ops" in body
    assert "useradd --system" in body and "--home-dir /var/lib/harvester-ops" in body
    assert "nologin" in body
    # idempotent : une réinstallation ne doit pas échouer
    assert "getent group harvester-ops" in body and "id -u harvester-ops" in body


def test_install_calls_it_before_preparing_the_directories():
    main = _fn(INSTALL, "main")
    assert main.index("setup_service_account") < main.index("prepare_config")


def test_install_gives_state_and_logs_to_the_account():
    body = _fn(INSTALL, "prepare_config")
    assert 'install -d -m 0750 -o harvester-ops -g harvester-ops "$LOG_DIR" "$STATE_DIR"' in body
    assert 'STATE_DIR="/var/lib/harvester-ops"' in INSTALL


def test_install_secures_the_configuration_after_writing_it():
    """Après htpasswd et TLS : ce sont eux qui écrivaient en root seul."""
    body = _fn(INSTALL, "secure_config")
    assert 'chgrp -R harvester-ops "$CONF_DIR"' in body
    assert 'chmod -R g+rX,g-w,o-rwx "$CONF_DIR"' in body
    main = _fn(INSTALL, "main")
    assert main.index("setup_tls") < main.index("secure_config") < main.index("install_systemd")


def test_the_next_steps_no_longer_lock_the_service_out():
    """`chmod 600` sur une kubeconfig la rendait illisible par le service."""
    assert "chmod 600" not in INSTALL
    for doc in ("docs/en/install.md", "docs/fr/installation.md"):
        text = (ROOT / doc).read_text()
        assert "sudo chmod 600" not in text, doc
        assert "install -m 0640 -g harvester-ops" in text, doc
        assert "harvester-ops" in text and "/var/lib/harvester-ops" in text, doc


def test_purge_removes_state_and_account():
    purge = UNINSTALL.split('if [[ "$PURGE" == "1" ]]; then', 1)[1].split("\nfi", 1)[0]
    assert '"$STATE_DIR"' in purge
    assert "userdel harvester-ops" in purge and "groupdel harvester-ops" in purge


# ---------------------------------------------------------------------------
# L'application sur un système de fichiers en lecture seule
# ---------------------------------------------------------------------------

def test_a_read_only_filesystem_falls_back_instead_of_crashing(monkeypatch, tmp_path):
    real_mkdir = Path.mkdir

    def mkdir(self, *a, **k):
        if str(self).startswith("/var/lib/harvester-ops"):
            raise OSError(errno.EROFS, "Read-only file system", str(self))
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    got = wapp._usable_dir(Path("/var/lib/harvester-ops"), tmp_path / "fallback")
    assert got == tmp_path / "fallback" and got.is_dir()


def test_a_usable_directory_is_kept(tmp_path):
    assert wapp._usable_dir(tmp_path / "state", tmp_path / "fallback") == tmp_path / "state"


@pytest.mark.parametrize("name", ["ACTIONS_DB", "NOTES_DB", "TF_WORKSPACES"])
def test_every_state_path_goes_through_the_fallback(name):
    src = (ROOT / "web" / "app.py").read_text()
    assign = src.split(f"\n{name} = ", 1)[1].split("\n\n", 1)[0]
    assert "_usable_dir(" in assign, name


# ---------------------------------------------------------------------------
# L'arrêt du service
# ---------------------------------------------------------------------------

def test_sigterm_stops_the_server_cleanly(tmp_path):
    """Dans le conteneur, l'application est le processus 1 : le noyau ne lui
    applique pas l'action par défaut de SIGTERM. Constaté : `podman stop`
    attendait 10 s puis tuait par SIGKILL. Elle doit sortir d'elle-même,
    proprement (code 0 et non tuée par le signal)."""
    import signal
    import socket
    import subprocess
    import time
    import urllib.request

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"web:\n  bind_host: 127.0.0.1\n  bind_port: {port}\nclusters: []\n")
    env = dict(os.environ, HARVESTER_OPS_CONFIG=str(cfg), HARVESTER_OPS_WATCH="0",
               HARVESTER_OPS_ACTIONS_DB=str(tmp_path / "actions.db"),
               HARVESTER_OPS_NOTES_DB=str(tmp_path / "notes.db"),
               HARVESTER_OPS_LOG_DIR=str(tmp_path / "logs"),
               HARVESTER_OPS_WATCH_STATE_DIR=str(tmp_path / "watch"))
    proc = subprocess.Popen([sys.executable, str(ROOT / "web" / "app.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("the server did not start")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
