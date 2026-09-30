"""Clusters déclarés par la console elle-même (v1.78.0).

La configuration de l'opérateur (`/etc/harvester-ops/config.yaml`) est en
lecture seule pour le service packagé. Tout ce que la console écrit d'elle-
même vit dans son répertoire d'état (`/var/lib/harvester-ops` par défaut,
`HARVESTER_OPS_STATE_DIR` sinon) :

    <état>/clusters.d/<nom>.yaml     une déclaration par cluster (0600)
    <état>/kubeconfigs/<nom>.yaml    son kubeconfig (0600)
    <état>/ssh/<nom>_id              sa clé (+ .pub, _known_hosts)

Les chemins rangés dans ces déclarations sont RELATIFS au répertoire d'état
(`kubeconfigs/lab1.yaml`) et résolus au chargement : copier la configuration
et l'état vers un autre hôte, ou sous un autre chemin, suffit à déplacer la
console. Un chemin absolu donné par l'opérateur reste absolu.

Partagé par la console (web/app.py) et la ligne de commande (kube.py) ;
common.sh applique les mêmes règles pour les scripts bash.
"""

import os
import re
from pathlib import Path

DEFAULT_STATE_DIR = "/var/lib/harvester-ops"
DECL_DIR = "clusters.d"
KUBECONFIG_DIR = "kubeconfigs"
SSH_DIR = "ssh"
# même règle que la déclaration d'un cluster (Settings > Clusters) : un nom
# qui commence par une lettre ou un chiffre ne remonte jamais d'un répertoire
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$")


def state_dir(default=None):
    """Répertoire d'état de la console, toujours absolu : un chemin relatif
    donné par l'environnement le serait au répertoire courant du processus,
    qui diffère entre la console et les scripts qu'elle lance."""
    return Path(os.path.abspath(str(os.environ.get("HARVESTER_OPS_STATE_DIR")
                                    or default or DEFAULT_STATE_DIR)))


def decl_path(state, name):
    """Fichier de déclaration d'un cluster, None pour un nom refusé."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None
    return Path(state) / DECL_DIR / f"{name}.yaml"


def _map_paths(cluster, fn):
    out = dict(cluster)
    if isinstance(out.get("kubeconfig"), str) and out["kubeconfig"]:
        out["kubeconfig"] = fn(out["kubeconfig"])
    if isinstance(out.get("ssh"), dict):
        ssh = dict(out["ssh"])
        if isinstance(ssh.get("key"), str) and ssh["key"]:
            ssh["key"] = fn(ssh["key"])
        out["ssh"] = ssh
    return out


def resolve(cluster, state):
    """Chemins relatifs rendus absolus sous le répertoire d'état."""
    state = Path(state)
    return _map_paths(cluster, lambda p: p if os.path.isabs(p) else str(state / p))


def relativize(cluster, state):
    """Chemins situés sous le répertoire d'état rendus relatifs à lui ; les
    autres (donnés par l'opérateur ailleurs) restent tels quels."""
    base = os.path.abspath(str(state))

    def rel(p):
        if not os.path.isabs(p):
            return p
        ap = os.path.abspath(p)
        if ap == base or not ap.startswith(base + os.sep):
            return p
        return Path(os.path.relpath(ap, base)).as_posix()
    return _map_paths(cluster, rel)


def check_decl(doc, path):
    """Raison du refus d'un fichier de déclaration, None s'il est bon."""
    if not isinstance(doc, dict):
        return "not a mapping"
    name = doc.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        return "invalid or missing name"
    if name != Path(path).stem:
        return f"name '{name}' does not match the file name"
    if not isinstance(doc.get("nodes", []), list):
        return "nodes must be a list"
    return None


def decl_files(state):
    """Fichiers de déclaration présents, triés."""
    d = Path(state) / DECL_DIR
    try:
        return sorted(p for p in d.glob("*.yaml") if p.is_file())
    except OSError:
        return []


def merge(config_clusters, decls, state, warn=None):
    """Clusters de config.yaml suivis de ceux de la console. `decls` :
    [(chemin, document lu ou None)]. Un nom déjà dans config.yaml l'emporte ;
    un fichier illisible ou invalide est ignoré, jamais fatal."""
    warn = warn or (lambda msg: None)
    out = list(config_clusters or [])
    seen = {c.get("name") for c in out if isinstance(c, dict)}
    for path, doc in decls:
        why = check_decl(doc, path)
        if why:
            warn(f"cluster declaration {Path(path).name} ignored: {why}")
            continue
        if doc["name"] in seen:
            warn(f"cluster declaration {Path(path).name} ignored: "
                 f"'{doc['name']}' is declared in config.yaml")
            continue
        seen.add(doc["name"])
        out.append(resolve(doc, state))
    return out
