"""harvester-ops : artefact OCI au format Flux d'un élément `gitops` (2.8.0).

Un élément `gitops` d'un paquet (fichiers `manifests/<élément>/*.yaml`)
devient, à la publication dans un registre, un artefact OCI que
l'`OCIRepository` de Flux tire depuis le cluster enfant :

- une couche : archive tar.gz DÉTERMINISTE des fichiers (ordre trié, `mtime`
  0, uid et gid 0, noms vides, mode 0644, en-tête gzip sans nom ni date) :
  deux constructions des mêmes fichiers donnent les mêmes octets, donc la
  même empreinte, et une republication ne change rien ;
- types de média de `flux push artifact` (Flux 2.10, `oci/client/push.go`
  de fluxcd/pkg) : configuration `application/vnd.cncf.flux.config.v1+json`,
  couche `application/vnd.cncf.flux.content.v1.tar+gzip`, manifeste OCI ;
  vu sur harvlab2 le 07/11/2026 : l'`OCIRepository` (source-controller
  v1.10.1) prend la couche et la `Kustomization` applique son contenu ;
- dépôt `<registre>/harvester-ops/gitops/<paquet>/<élément>`, étiquette
  `<version>` du paquet.

Le contenu est jugé par les règles des manifestes (`capi_manifests.check`)
avant la construction : un outil GitOps n'est pas une porte dérobée vers les
CRD et les webhooks.

Bibliothèque standard seulement.
"""

import gzip
import hashlib
import io
import json
import tarfile

M_OCI = "application/vnd.oci.image.manifest.v1+json"
CONFIG_TYPE = "application/vnd.cncf.flux.config.v1+json"
LAYER_TYPE = "application/vnd.cncf.flux.content.v1.tar+gzip"
REPO_PREFIX = "harvester-ops/gitops"
A_CREATED = "org.opencontainers.image.created"
A_SOURCE = "org.opencontainers.image.source"
A_REVISION = "org.opencontainers.image.revision"
# date fixe : l'annotation ne doit pas défaire le déterminisme
EPOCH = "1970-01-01T00:00:00Z"
MAX_BYTES = 8 * 1024 * 1024


class FluxArtifactError(Exception):
    pass


def digest_of(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def repo_of(package, item):
    """Chemin du dépôt de l'artefact dans le registre."""
    return f"{REPO_PREFIX}/{package}/{item}"


def _arcname(rel, item):
    """`manifests/<élément>/a/b.yaml` -> `a/b.yaml` (la racine de l'archive
    est le contenu de l'élément) ; FluxArtifactError hors de l'élément."""
    prefix = f"manifests/{item}/"
    if not rel.startswith(prefix) or ".." in rel.split("/") or rel.endswith("/"):
        raise FluxArtifactError(f"{rel}: outside manifests/{item}/")
    return rel[len(prefix):]


def layer(files, item):
    """Octets de la couche tar.gz déterministe de `files` ({chemin: texte})."""
    if not files:
        raise FluxArtifactError(f"{item}: no file")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in sorted(files):
            data = files[rel].encode() if isinstance(files[rel], str) else bytes(files[rel])
            info = tarfile.TarInfo(_arcname(rel, item))
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as gz:
        gz.write(raw.getvalue())
    data = out.getvalue()
    if len(data) > MAX_BYTES:
        raise FluxArtifactError(f"{item}: {len(data)} bytes, {MAX_BYTES} at most")
    return data


def build(files, item, version, source=""):
    """(couche, configuration, manifeste) en octets ; le manifeste cite les
    deux autres par empreinte."""
    lay = layer(files, item)
    config = b"{}"
    manifest = {
        "schemaVersion": 2, "mediaType": M_OCI,
        "config": {"mediaType": CONFIG_TYPE, "digest": digest_of(config), "size": len(config)},
        "layers": [{"mediaType": LAYER_TYPE, "digest": digest_of(lay), "size": len(lay)}],
        "annotations": {A_CREATED: EPOCH, A_REVISION: f"{version}@{digest_of(lay)}",
                        A_SOURCE: source or f"harvester-ops/{item}"},
    }
    return lay, config, json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def push(registry, repo, tag, built):
    """Pousse couche et configuration (`push_blob` saute un blob présent), puis le
    manifeste sous `tag` ; rend l'empreinte du manifeste. `registry` :
    `oci_push.Registry`."""
    lay, config, manifest = built
    for blob in (config, lay):
        registry.push_blob(repo, digest_of(blob), blob)      # sauté s'il est déjà là
    registry.put_manifest(repo, tag, manifest, M_OCI)
    return digest_of(manifest)
