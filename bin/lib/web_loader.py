"""Chargement de la console (web/) par les lignes de commande, et lecture
de leurs secrets (2.4.0, partagé par harvester-baremetal et
harvester-plans).

Une commande qui déroule ce que fait la console (série d'installations,
plan de déploiement) charge web/app.py dans son propre processus : même
code, deux appelants. `prepare_console_env` pose ce qu'il faut pour qu'elle
ne surveille aucun cluster et range son historique dans l'état.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET_KEYS = ("token", "password", "bmc_password", "bmc_passwords")


def load(name):
    """Le module `name` de la console (web/), ou None."""
    for d in (os.environ.get("HARVESTER_OPS_WEB_DIR"), ROOT / "web", "/opt/harvester-ops/web"):
        if d and (Path(d) / f"{name}.py").is_file():
            if str(d) not in sys.path:
                sys.path.insert(0, str(d))
            return __import__(name)
    return None


def prepare_console_env(state):
    """Variables d'environnement d'une console chargée par une commande."""
    os.environ.setdefault("HARVESTER_OPS_STATE_DIR", str(state))
    # v1.83.2 : la commande ne surveille aucun cluster, et son historique va
    # dans le même répertoire d'état
    os.environ.setdefault("HARVESTER_OPS_WATCH", "0")
    os.environ.setdefault("HARVESTER_OPS_READ_WORKERS", "0")
    # v1.99.0 : jamais de suiveur de réplication depuis une commande
    os.environ["HARVESTER_OPS_REPLICATION_FOLLOW"] = "0"
    os.environ.setdefault("HARVESTER_OPS_ACTIONS_DB", str(Path(state) / "actions.db"))
    os.environ.setdefault("HARVESTER_OPS_NOTES_DB", str(Path(state) / "notes.db"))


class SecretsError(ValueError):
    """Fichier de secrets refusé (droits, YAML, clés)."""


def read_secrets(path=None, stdin=None):
    """Secrets en YAML d'un fichier privé (0600, refusé sinon) ou de l'entrée
    standard ; jamais de la ligne de commande. Clés : token, password (OS),
    bmc_password (commun), bmc_passwords (par hôte de BMC)."""
    import yaml
    if stdin is not None:
        text = stdin.read()
    else:
        p = Path(path)
        if p.stat().st_mode & 0o077:
            raise SecretsError("the secrets file must not be readable by others (chmod 600)")
        text = p.read_text()
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        raise SecretsError("secrets: invalid YAML") from None
    if not isinstance(doc, dict):
        raise SecretsError("secrets: expected a mapping (token, password, bmc_password, bmc_passwords)")
    unknown = set(doc) - set(SECRET_KEYS)
    if unknown:
        raise SecretsError(f"secrets: unknown keys {sorted(unknown)}")
    return doc
