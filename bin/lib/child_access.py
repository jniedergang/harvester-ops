"""harvester-ops : accès à un cluster enfant (2.7.0, point 205).

Le kubeconfig d'un cluster enfant est le Secret `<cluster>-kubeconfig` que
Cluster API garde dans le namespace du cluster, sur Harvester. Il est lu avec
l'identité de la personne (le kubeconfig usurpé que la console passe) : qui
lit ce Secret est administrateur du cluster enfant (règle de la conception).
Il n'est jamais rangé dans l'état de la console : un fichier 0600 de
l'espace de travail, effacé à la sortie de `session`.

Jetons d'automatisation : `TokenRequest` sur un compte de service du
cluster enfant, durée bornée (600 à 86 400 s). Le jeton n'est jamais écrit
dans un fichier, une étape ni un message d'erreur.
"""

import base64
import contextlib
import json
import time
from pathlib import Path

import work_dir
from kube import KubeError

SECRET_SUFFIX = "-kubeconfig"
TOKEN_MIN, TOKEN_MAX, TOKEN_DEFAULT = 600, 86400, 3600


class ChildAccessError(Exception):
    """Refus explicable ; `code` le nomme."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def secret_name(cluster):
    return cluster + SECRET_SUFFIX


def read(kube, ns, cluster):
    """Texte du kubeconfig du cluster enfant ; ChildAccessError si le Secret
    manque (`child-kubeconfig-missing`) ou n'est pas lisible par la personne
    (`child-admin-required`)."""
    try:
        sec = kube.get("secrets", ns, secret_name(cluster))
    except KubeError as e:
        if "forbidden" in str(e).lower():
            raise ChildAccessError("child-admin-required",
                                   f"reading {ns}/{secret_name(cluster)} is not allowed for this identity") from None
        raise
    value = ((sec or {}).get("data") or {}).get("value")
    if not value:
        raise ChildAccessError("child-kubeconfig-missing", f"no kubeconfig for {ns}/{cluster}")
    return base64.b64decode(value).decode()


PREFIX, SUFFIX = "hops-child-", ".kubeconfig"
# Une session ne dure que le temps de quelques lectures ou suppressions : un
# fichier plus vieux est le reste d'un outil tué (SIGKILL), effacé.
STALE_SECONDS = 15 * 60


def sweep(now=None):
    """Efface les kubeconfigs de cluster enfant laissés dans l'espace de
    travail par un outil tué (revue 1-3, I4) ; rend leur nombre."""
    now = time.time() if now is None else now
    n = 0
    for f in work_dir.path().glob(f"{PREFIX}*{SUFFIX}"):
        try:
            if now - f.stat().st_mtime > STALE_SECONDS:
                f.unlink()
                n += 1
        except OSError:
            continue
    return n


def kubeconfig(kube, ns, cluster):
    """Chemin d'un fichier 0600 de l'espace de travail ; à effacer par
    l'appelant (préférer `session`)."""
    sweep()
    return work_dir.private_file(read(kube, ns, cluster), prefix=PREFIX, suffix=SUFFIX)


@contextlib.contextmanager
def session(kube, ns, cluster):
    """`with session(kube, ns, c) as path:` : le kubeconfig du cluster
    enfant, effacé en sortie, exception ou non."""
    path = kubeconfig(kube, ns, cluster)
    try:
        yield path
    finally:
        Path(path).unlink(missing_ok=True)


def check_seconds(seconds):
    try:
        n = int(seconds)
    except (TypeError, ValueError):
        raise ValueError(f"token duration: whole seconds, {TOKEN_MIN} to {TOKEN_MAX}") from None
    if not TOKEN_MIN <= n <= TOKEN_MAX:
        raise ValueError(f"token duration: {TOKEN_MIN} to {TOKEN_MAX} seconds")
    return n


def token(kube_child, ns, sa, seconds):
    """`TokenRequest` sur `ns/sa` ; rend (jeton, expiration RFC 3339). Le
    serveur peut raccourcir la durée : l'expiration rendue fait foi."""
    n = check_seconds(seconds)
    body = {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest",
            "spec": {"expirationSeconds": n}}
    try:
        out = kube_child.run("create", "--raw", f"/api/v1/namespaces/{ns}/serviceaccounts/{sa}/token",
                             "-f", "-", input=json.dumps(body))
    except KubeError as e:
        # le message de kubectl ne porte jamais le jeton (refus avant émission)
        raise KubeError(f"token request for {ns}/{sa} refused: {str(e)[:200]}") from None
    st = (json.loads(out) or {}).get("status") or {}
    if not st.get("token"):
        raise KubeError(f"token request for {ns}/{sa}: no token in the answer")
    return st["token"], st.get("expirationTimestamp") or ""
