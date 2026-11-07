"""harvester-ops : espace de travail des outils de bin/ (2.7.0, m11).

Les outils écrivent des fichiers passagers (paquet extrait, kubeconfig
d'identité ou d'un cluster enfant) : jamais dans /tmp, partagé par tous les
comptes de l'hôte, ni dans le répertoire courant. Ordre : la variable
`HARVESTER_OPS_WORK_DIR` (la console la pose toujours), sinon
`<répertoire d'état>/work`. Le répertoire est créé en 0700.

Bibliothèque standard seulement.
"""

import os
import tempfile
from pathlib import Path

ENV = "HARVESTER_OPS_WORK_DIR"
SUBDIR = "work"
# Racines partagées par tous les comptes : refusées telles quelles (un
# sous-répertoire privé, comme celui de pytest, reste permis).
SHARED_ROOTS = ("/tmp", "/var/tmp", "/dev/shm")


def _shared(p):
    roots = {os.path.realpath(r) for r in SHARED_ROOTS}
    roots.add(os.path.realpath(tempfile.gettempdir()))
    return os.path.realpath(p) in roots


def path(state_dir=None):
    """Le répertoire de travail, créé en 0700 s'il manque. ValueError pour
    un chemin relatif, une racine partagée (/tmp) ou un répertoire qui
    appartient à un autre compte."""
    raw = os.environ.get(ENV)
    if not raw:
        import cluster_decl
        raw = str(cluster_decl.state_dir(state_dir) / SUBDIR)
    if not os.path.isabs(raw):
        raise ValueError(f"work directory: an absolute path is required ({ENV})")
    if _shared(raw):
        raise ValueError(f"work directory: {raw} is shared by every account, choose a private one ({ENV})")
    p = Path(raw)
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = p.stat()
    if st.st_uid != os.getuid():
        raise ValueError(f"work directory: {raw} belongs to another account")
    if st.st_mode & 0o077:
        os.chmod(p, 0o700)
    return p


def mkdtemp(prefix, state_dir=None):
    """Un sous-répertoire privé de l'espace de travail."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(path(state_dir))))


def private_file(content, prefix="hops-", suffix="", state_dir=None):
    """Fichier 0600 de l'espace de travail ; rend son chemin, à effacer par
    l'appelant. Le chemin n'est jamais écrit dans une étape."""
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(path(state_dir)))
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(content)
    return name
