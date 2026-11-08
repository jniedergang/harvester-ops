"""2.9.0, tâche 13 (essai réel sur la VM client) : le répertoire des
instantanés de B1 doit être visible dans le conteneur de la console. Le
paquet ne montait que /etc, /var/log et /var/lib/harvester-ops (refusé :
c'est l'état) : aucun répertoire de l'hôte ne pouvait servir. Un partage NFS
monté sous /var/log cassait le redémarrage (relabel `:Z` refusé par le
root_squash). Désormais : /var/lib/harvester-ops-snapshots, monté sans
relabel, créé seulement s'il manque."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNIT = (ROOT / "config" / "systemd" / "harvester-ops.service").read_text()
SNAPS = "/var/lib/harvester-ops-snapshots"


def test_snapshot_dir_is_mounted_without_relabel():
    m = re.search(rf"-v {SNAPS}:{SNAPS}(:[^ \\]*)?", UNIT)
    assert m, "snapshot directory not mounted in the console container"
    opts = (m.group(1) or "").lstrip(":").split(",")
    assert "Z" not in opts and "z" not in opts            # relabel refusé sur un partage


def test_snapshot_dir_created_only_when_missing_and_never_fatal():
    pre = [line for line in UNIT.splitlines() if line.startswith("ExecStartPre=") and "harvester-ops-snapshots" in
           UNIT[UNIT.index(line):UNIT.index(line) + 400]]
    assert pre and pre[0].startswith("ExecStartPre=-")      # un échec n'arrête pas la console
    block = UNIT[UNIT.index(pre[0]):UNIT.index(pre[0]) + 400]
    assert '[ -d "$$d" ] ||' in block                       # jamais de chown sur un partage existant


def test_snapshot_dir_is_outside_the_state_directory():
    import importlib.util
    import sys
    sys.path.insert(0, str(ROOT / "bin" / "lib"))
    spec = importlib.util.spec_from_file_location("hsc_mount", ROOT / "bin" / "harvester-secrets.py")
    hsc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hsc)
    assert not Path(SNAPS).is_relative_to("/var/lib/harvester-ops")
