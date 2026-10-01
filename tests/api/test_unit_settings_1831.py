"""v1.83.1 : les réglages documentés dans le guide de dimensionnement
atteignent la console packagée. L'unité ne transmet au conteneur que les
variables passées par -e : aucun réglage de performance n'y figurait (vu en
réel sur la VM de banc : lecteurs impossibles à activer)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def documented():
    names = set()
    for p in ("docs/en/sizing.md", "docs/fr/dimensionnement.md"):
        names |= set(re.findall(r"`(HARVESTER_OPS_[A-Z_]+)`", (ROOT / p).read_text()))
    # « _IDLE_INTERVAL » est écrit en abrégé à côté de WATCH_IDLE_AFTER
    names.discard("HARVESTER_OPS_WATCH_IDLE_AFTER")
    return names | {"HARVESTER_OPS_WATCH_IDLE_AFTER", "HARVESTER_OPS_WATCH_IDLE_INTERVAL"}


def test_every_documented_setting_reaches_the_container():
    unit = (ROOT / "config" / "systemd" / "harvester-ops.service").read_text()
    passed = dict(re.findall(r"-e (HARVESTER_OPS_[A-Z_]+)=\$\$\{\1:-([^}]*)\}", unit))
    missing = sorted(documented() - set(passed))
    assert not missing, missing
    # un réglage lu comme un nombre a une valeur par défaut numérique : une
    # valeur vide ferait tomber la console au démarrage
    for name in ("HARVESTER_OPS_WATCH_INTERVAL", "HARVESTER_OPS_WATCH_IDLE_AFTER",
                 "HARVESTER_OPS_WATCH_IDLE_INTERVAL", "HARVESTER_OPS_READ_SHARE_TTL",
                 "HARVESTER_OPS_READ_WORKER_TASKS"):
        float(passed[name])


def test_an_empty_worker_count_falls_back_to_the_default(monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "web"))
    import read_workers as rw
    monkeypatch.setenv("HARVESTER_OPS_READ_WORKERS", "")
    assert rw.configured() == rw.default_workers()
