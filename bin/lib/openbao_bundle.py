"""Paquet OpenBao du mode B1 (2.9.0, tâches 6 et 7), bibliothèque standard.

Le paquet est construit par le CLIENT (`harvester-secrets bundle`), depuis la
SUSE Application Collection avec son compte de service, ou depuis son registre
miroir : l'éditeur ne redistribue pas l'image (point 251). Contenu :

    harvester-ops-openbao-<version>-<tag>/
        openbao-image.tar   archive OCI (une image, une plateforme) chargée
                            par `podman load`
        openbao.hcl.tpl     modèle de configuration (config/openbao/)
        provenance.json     image, tag, empreintes de l'index et du
                            manifeste, version, date, vérification cosign
                            (déclarative : jamais crue à la pose)
        cosign-signature.json  signature cosign de l'index, telle que le
                            registre la sert (étiquette sha256-<hex>.sig) :
                            charge utile et signature
        SHA256SUMS          somme de chaque fichier

Confiance (revue I2 des tâches 5-7) : rien de ce que le paquet déclare n'est
cru. Un tag connu (`KNOWN`) doit donner l'empreinte d'index épinglée ici,
quel que soit le nom de l'image (un miroir garde l'empreinte). Un autre tag
n'est accepté que si la signature cosign embarquée se vérifie avec la clé
publique de l'Application Collection fournie par l'appelant (celle de
/opt/harvester-ops pour l'agent), signe CETTE empreinte d'index et désigne le
dépôt `containers/openbao` (la même clé signe toutes les images de la
collection). `verify` relit ensuite tout : sommes, provenance, index OCI,
chaque blob (empreinte recalculée), et le manifeste listé par l'index pour
linux/amd64.

La version affichée vient de l'étiquette `org.opencontainers.image.version`
de l'image : la construction SUSE n'estampille pas `bao version` (2.0.0-HEAD,
essai 1).
"""

import base64
import hashlib
import io
import json
import re
import subprocess
import tarfile
import time
from pathlib import Path

DEFAULT_REGISTRY = "dp.apps.rancher.io"
DEFAULT_REPOSITORY = "containers/openbao"
DEFAULT_TAG = "2.7.2-5.4"
# Tag -> empreinte de l'index OCI (essai 1 de la tâche 1, signée cosign par la
# clé de l'Application Collection).
KNOWN = {"2.7.2-5.4": "sha256:355cbcca102b8a187a329d70a259fec31a19f607a5dee83488d5b35aafa96964"}
PLATFORM = "linux/amd64"
IMAGE_TAR = "openbao-image.tar"
TEMPLATE = "openbao.hcl.tpl"
PROVENANCE = "provenance.json"
SUMS = "SHA256SUMS"
SIGNATURE = "cosign-signature.json"
FILES = (IMAGE_TAR, TEMPLATE, PROVENANCE)
OPTIONAL = (SIGNATURE,)
COSIGN_KEY_NAME = "application-collection-cosign.pub"
SIMPLESIGNING = "application/vnd.dev.cosign.simplesigning.v1+json"
SIG_ANNOTATION = "dev.cosignproject.cosign/signature"
SIGNATURE_MAX = 64 << 10
NAME_RE = re.compile(r"^harvester-ops-openbao-(?P<version>[0-9][0-9A-Za-z.+]{0,30})-(?P<tag>[A-Za-z0-9._-]{1,64})"
                     r"\.tar\.gz$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_LABEL = "org.opencontainers.image.version"
A_NAME = "io.containerd.image.name"
A_REF = "org.opencontainers.image.ref.name"
M_INDEX = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
MAX_BUNDLE = 2 << 30


class BundleError(Exception):
    """Paquet refusé ; `code` stable, message sans secret."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def bundle_name(version, tag):
    name = f"harvester-ops-openbao-{version}-{tag}.tar.gz"
    m = NAME_RE.match(name)
    if not m or (m["version"], m["tag"]) != (version, tag):       # nom relu à l'identique
        raise BundleError("bundle-bad-name", f"cannot name a bundle for version {version!r}, tag {tag!r}")
    return name


def _sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha_file(f):
    h = hashlib.sha256()
    while True:
        b = f.read(1 << 20)
        if not b:
            break
        h.update(b)
    return "sha256:" + h.hexdigest()


# ---------------------------------------------------------------------------
# Archive OCI
# ---------------------------------------------------------------------------
def write_image_tar(layout, entry, index_body, out):
    """Archive OCI d'UNE image de la disposition `layout` (oci_layout.Layout) :
    oci-layout, index.json réduit à l'image (nom complet en
    `io.containerd.image.name`, lu par `podman load`), ses blobs, et l'index
    multi-architecture d'origine (blob seulement : il porte l'empreinte
    signée)."""
    blobs = list(dict.fromkeys(layout.blobs_of(entry)))
    idx = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [entry]}

    def add(t, name, data):
        ti = tarfile.TarInfo(name)
        ti.size, ti.mode, ti.mtime = len(data), 0o644, 0
        t.addfile(ti, io.BytesIO(data))

    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as t:
        add(t, "oci-layout", json.dumps({"imageLayoutVersion": "1.0.0"}).encode())
        add(t, "index.json", json.dumps(idx).encode())
        add(t, f"blobs/sha256/{_sha(index_body)[7:]}", index_body)
        for d in blobs:
            p = layout.blob_path(d)
            ti = t.gettarinfo(str(p), arcname=f"blobs/sha256/{d[7:]}")
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mode, ti.mtime = 0o644, 0
            with open(p, "rb") as f:
                t.addfile(ti, f)


def check_image_tar(path, prov):
    """Relit l'archive OCI : une image, nommée comme la provenance, chaque
    blob à son empreinte, manifeste = celui de la provenance et listé par
    l'index d'origine pour linux/amd64. Rend l'étiquette de version."""
    with tarfile.open(path, "r:") as t:
        members = {m.name: m for m in t.getmembers()}
        for name, m in members.items():
            if not m.isfile() or name.startswith("/") or ".." in name.split("/"):
                raise BundleError("bundle-image-bad", f"unexpected member in the image archive: {name[:80]}")

        def blob(d):
            m = members.get(f"blobs/sha256/{d[7:]}") if DIGEST_RE.match(str(d)) else None
            if m is None:
                raise BundleError("bundle-image-bad", f"blob {str(d)[:19]} missing from the image archive")
            f = t.extractfile(m)
            got = _sha_file(f)
            if got != d:
                raise BundleError("bundle-digest-mismatch", f"blob {d[:19]} does not match its digest")
            return t.extractfile(m).read() if m.size <= (4 << 20) else None

        try:
            idx = json.loads(t.extractfile(members["index.json"]).read())
        except (KeyError, ValueError):
            raise BundleError("bundle-image-bad", "the image archive has no readable index.json") from None
        entries = idx.get("manifests") or []
        if len(entries) != 1:
            raise BundleError("bundle-image-bad", "the image archive must hold exactly one image")
        e = entries[0]
        if e.get("digest") != prov["manifest_digest"]:
            raise BundleError("bundle-digest-mismatch", "the image archive does not hold the image of the provenance")
        if (e.get("annotations") or {}).get(A_NAME) != f"{prov['image']}:{prov['tag']}":
            raise BundleError("bundle-image-bad", "the image archive names another image")
        index_body = blob(prov["index_digest"])
        index = json.loads(index_body)
        if index.get("mediaType") not in M_INDEX and not index.get("manifests"):
            raise BundleError("bundle-image-bad", "the pinned digest is not an image index")
        os_, arch = PLATFORM.split("/")
        if not any(m.get("digest") == prov["manifest_digest"] and (m.get("platform") or {}).get("os") == os_
                   and (m.get("platform") or {}).get("architecture") == arch for m in index.get("manifests") or []):
            raise BundleError("bundle-digest-mismatch", f"the pinned index does not list this {PLATFORM} image")
        man = json.loads(blob(prov["manifest_digest"]))
        cfg = json.loads(blob(man["config"]["digest"]))
        for layer in man.get("layers") or []:
            blob(layer["digest"])
        return ((cfg.get("config") or {}).get("Labels") or {}).get(VERSION_LABEL)


# ---------------------------------------------------------------------------
# Vérification d'un paquet
# ---------------------------------------------------------------------------
def verify(path, workdir, cosign_key=None):
    """Extrait le paquet `path` dans `workdir` (vide, privé) et le vérifie.
    `cosign_key` : clé publique de la collection (fichier PEM), exigée pour
    un tag hors de `KNOWN`. Rend {"name", "version", "tag", "image",
    "index_digest", "manifest_digest", "root", "image_tar", "template",
    "provenance", "trust"} (`trust` : "pinned" ou "signature"). BundleError
    sinon (`bundle-bad-name`, `bundle-unsafe`, `bundle-sums`,
    `bundle-provenance`, `bundle-pin-mismatch`, `bundle-unsigned`,
    `bundle-signature-bad`, `bundle-digest-mismatch`, `bundle-image-bad`)."""
    path, workdir = Path(path), Path(workdir)
    m = NAME_RE.match(path.name)
    if not m:
        raise BundleError("bundle-bad-name", "not an OpenBao bundle name (harvester-ops-openbao-<version>-<tag>.tar.gz)")
    top = path.name[: -len(".tar.gz")]
    try:
        tf = tarfile.open(path, "r:gz")
    except (OSError, tarfile.TarError):
        raise BundleError("bundle-unsafe", "unreadable bundle archive") from None
    with tf:
        members, total = [], 0
        for mem in tf:
            parts = mem.name.split("/")
            if mem.name.startswith("/") or ".." in parts or parts[0] != top or not (mem.isfile() or mem.isdir()):
                raise BundleError("bundle-unsafe", f"unexpected member in the bundle: {mem.name[:80]}")
            if mem.isfile() and (len(parts) != 2 or parts[1] not in FILES + OPTIONAL + (SUMS,)):
                raise BundleError("bundle-unsafe", f"unexpected file in the bundle: {mem.name[:80]}")
            total += mem.size
            if total > MAX_BUNDLE:
                raise BundleError("bundle-unsafe", "bundle too large")
            mem.uid = mem.gid = 0
            mem.mode = 0o700 if mem.isdir() else 0o600
            members.append(mem)
        try:
            tf.extractall(workdir, members=members, filter="data")
        except TypeError:
            tf.extractall(workdir, members=members)
    root = workdir / top
    sums = {}
    try:
        for line in (root / SUMS).read_text().splitlines():
            h, _, name = line.strip().partition("  ")
            if line.strip():
                sums[name] = h
    except OSError:
        raise BundleError("bundle-sums", "SHA256SUMS missing") from None
    present = set(FILES) | {f for f in OPTIONAL if (root / f).is_file()}
    if set(sums) != present:
        raise BundleError("bundle-sums", "SHA256SUMS does not list exactly the bundle files")
    for name, h in sums.items():
        try:
            with open(root / name, "rb") as f:
                got = _sha_file(f)[7:]
        except OSError:
            raise BundleError("bundle-sums", f"{name} missing") from None
        if got != h:
            raise BundleError("bundle-sums", f"{name} does not match SHA256SUMS")
    try:
        prov = json.loads((root / PROVENANCE).read_text())
    except ValueError:
        raise BundleError("bundle-provenance", "unreadable provenance") from None
    for k in ("image", "tag", "index_digest", "manifest_digest", "version"):
        if not isinstance(prov.get(k), str) or not prov[k]:
            raise BundleError("bundle-provenance", f"provenance without {k}")
    if not TAG_RE.match(prov["tag"]) or not DIGEST_RE.match(prov["index_digest"]) \
            or not DIGEST_RE.match(prov["manifest_digest"]):
        raise BundleError("bundle-provenance", "malformed provenance")
    if prov["tag"] != m["tag"] or prov["version"] != m["version"]:
        raise BundleError("bundle-provenance", "the bundle name does not match its provenance")
    pin = KNOWN.get(prov["tag"])
    if pin:
        # quel que soit le nom de l'image : un miroir garde l'empreinte
        if prov["index_digest"] != pin:
            raise BundleError("bundle-pin-mismatch", f"tag {prov['tag']} is pinned to {pin[:19]}, "
                              f"the bundle holds {prov['index_digest'][:19]}")
        trust = "pinned"
    else:
        check_signature(root / SIGNATURE, prov["index_digest"], cosign_key, workdir)
        trust = "signature"
    label = check_image_tar(root / IMAGE_TAR, prov)
    if label and label != prov["version"]:
        raise BundleError("bundle-provenance", "the image version label does not match the provenance")
    return {"name": path.name, "version": prov["version"], "tag": prov["tag"], "image": prov["image"],
            "index_digest": prov["index_digest"], "manifest_digest": prov["manifest_digest"], "root": root,
            "image_tar": root / IMAGE_TAR, "template": (root / TEMPLATE).read_text(), "provenance": prov,
            "trust": trust}


# ---------------------------------------------------------------------------
# Signature cosign (clé de la collection), sans l'outil cosign
# ---------------------------------------------------------------------------
def _reference_repo(ref):
    """Le dépôt d'une référence d'image, sans l'hôte : `h/containers/x` ->
    `containers/x`."""
    parts = str(ref).split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    return "/".join(parts)


def signature_entries(sig_manifest, fetch_blob):
    """Entrées {payload, signature} (base64) des couches d'une signature
    cosign (manifeste de l'étiquette sha256-<hex>.sig)."""
    out = []
    for layer in (sig_manifest or {}).get("layers") or []:
        sig = (layer.get("annotations") or {}).get(SIG_ANNOTATION)
        if layer.get("mediaType") != SIMPLESIGNING or not sig or int(layer.get("size") or 0) > SIGNATURE_MAX:
            continue
        payload = fetch_blob(layer["digest"])
        out.append({"payload": base64.b64encode(payload).decode(), "signature": sig})
    return out


def check_signature(path, index_digest, key, workdir, openssl="openssl"):
    """Une des signatures de `path` (cosign-signature.json) se vérifie avec
    `key` (RSA ou ECDSA, PEM), signe `index_digest` et désigne le dépôt
    containers/openbao. openssl fait la vérification (déjà exigé par
    l'agent pour la CA de B1) ; la charge utile est celle que cosign signe."""
    if not key or not Path(key).is_file():
        raise BundleError("bundle-unsigned", "this tag is not pinned and the collection key is not available: "
                          "only a pinned tag or a signed image can be installed")
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise BundleError("bundle-unsigned", "this tag is not pinned and the bundle holds no cosign signature "
                          "(rebuild it with harvester-secrets bundle)") from None
    try:
        if len(raw) > SIGNATURE_MAX:
            raise ValueError
        entries = json.loads(raw)["signatures"]
        if not isinstance(entries, list):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise BundleError("bundle-signature-bad", "unreadable cosign signature") from None
    work = Path(workdir)
    for i, e in enumerate(entries[:8]):
        try:
            payload = base64.b64decode(e["payload"], validate=True)
            sig = base64.b64decode(e["signature"], validate=True)
            crit = json.loads(payload)["critical"]
            if crit.get("type") != "cosign container image signature" \
                    or crit["image"]["docker-manifest-digest"] != index_digest \
                    or _reference_repo(crit["identity"]["docker-reference"]) != DEFAULT_REPOSITORY:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        pf, sf = work / f".sig-payload-{i}", work / f".sig-{i}"
        pf.write_bytes(payload)
        sf.write_bytes(sig)
        try:
            r = subprocess.run([openssl, "dgst", "-sha256", "-verify", str(key), "-signature", str(sf), str(pf)],
                               capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise BundleError("bundle-signature-bad", "openssl is needed to check the cosign signature") from None
        finally:
            pf.unlink(missing_ok=True)
            sf.unlink(missing_ok=True)
        if r.returncode == 0:
            return True
    raise BundleError("bundle-signature-bad", f"no cosign signature of index {index_digest[:19]} "
                      "for containers/openbao verifies with the collection key")


# ---------------------------------------------------------------------------
# Construction (harvester-secrets bundle, tâche 7)
# ---------------------------------------------------------------------------
def build(src, registry, repository, tag, template_text, out_dir, cosign=None, now=time.time, progress=None,
          cosign_key=None):
    """Tire `<registry>/<repository>:<tag>` par `src` (oci_push.Registry
    avec les identifiants du client), une plateforme, octets inchangés
    (oci_layout.copy_image), et écrit le paquet dans `out_dir`. `cosign
    (référence épinglée) -> (état, détail)` vérifie la signature si l'outil
    est présent ; sinon la provenance le dit. Rend {"path", "name",
    "version", "index_digest", "manifest_digest", "cosign"}."""
    import shutil
    import tempfile
    import oci_layout as ol
    import oci_push as op
    if not TAG_RE.match(str(tag)):
        raise BundleError("bundle-bad-tag", "invalid image tag")
    image = f"{registry}/{repository}"
    body, mt = src.manifest(repository, tag)
    index_digest = op.digest_of(body)
    if mt not in M_INDEX:
        raise BundleError("bundle-image-bad", f"{image}:{tag} is not a multi-platform index ({mt})")
    pin = KNOWN.get(tag)
    if pin and index_digest != pin:
        raise BundleError("bundle-pin-mismatch", f"{image}:{tag} has index {index_digest[:19]}, pinned {pin[:19]}")
    cos = cosign(f"{image}@{index_digest}") if cosign else ("not-run", "cosign not found on this machine")
    if cos[0] == "failed":
        raise BundleError("bundle-cosign-failed", f"cosign refused the signature of {image}@{index_digest[:19]}")
    # la signature telle que le registre la sert (miroir Hauler compris) :
    # embarquée, elle est revérifiée à la pose avec la clé de l'hôte
    try:
        sig_body, _ = src.manifest(repository, f"sha256-{index_digest[7:]}.sig")
        sigs = signature_entries(json.loads(sig_body), lambda d: src.blob(repository, d))
    except (op.RegistryError, ValueError):
        sigs = []
    if not pin and not sigs:
        raise BundleError("bundle-unsigned", f"{image}:{tag} is not a pinned tag and the registry serves no cosign "
                          "signature for it: it could not be installed")
    work = Path(tempfile.mkdtemp(prefix=".openbao-bundle-", dir=str(out_dir)))
    try:
        layout = ol.Layout(work / "layout")
        got = ol.copy_image(src, f"{image}:{tag}@{index_digest}", layout, platform=PLATFORM, progress=progress)
        entry = layout.find(f"{image}:{tag}@{index_digest}") or layout.entries()[-1]
        entry = dict(entry, annotations={A_NAME: f"{image}:{tag}", A_REF: tag})
        man = json.loads(layout.read_blob(got["digest"]))
        cfg = json.loads(layout.read_blob(man["config"]["digest"]))
        version = ((cfg.get("config") or {}).get("Labels") or {}).get(VERSION_LABEL) or tag.split("-")[0]
        name = bundle_name(version, tag)
        top = work / name[: -len(".tar.gz")]
        top.mkdir()
        write_image_tar(layout, entry, body, top / IMAGE_TAR)
        (top / TEMPLATE).write_text(template_text)
        files = FILES
        if sigs:
            (top / SIGNATURE).write_text(json.dumps({"signatures": sigs}, indent=2) + "\n")
            files = FILES + (SIGNATURE,)
            if not pin:
                # même contrôle qu'à la pose : un paquet qui ne s'installerait pas n'est pas rendu
                check_signature(top / SIGNATURE, index_digest, cosign_key, work)
        prov = {"image": image, "tag": tag, "index_digest": index_digest, "manifest_digest": got["digest"],
                "platform": PLATFORM, "version": version, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                                     time.gmtime(now())),
                "cosign": {"state": cos[0], "detail": cos[1]}, "size": got["size"]}
        (top / PROVENANCE).write_text(json.dumps(prov, indent=2) + "\n")
        lines = []
        for f in files:
            with open(top / f, "rb") as fh:
                lines.append(f"{_sha_file(fh)[7:]}  {f}")
        (top / SUMS).write_text("\n".join(lines) + "\n")
        dest = Path(out_dir) / name
        part = dest.with_name(dest.name + ".part")
        with tarfile.open(part, "w:gz") as t:
            for f in (top, *[top / x for x in files + (SUMS,)]):
                ti = t.gettarinfo(str(f), arcname=str(f.relative_to(work)))
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                ti.mtime = int(now())
                if f.is_dir():
                    t.addfile(ti)
                else:
                    with open(f, "rb") as fh:
                        t.addfile(ti, fh)
        part.replace(dest)
        with open(dest, "rb") as fh:
            digest = _sha_file(fh)[7:]
        dest.with_name(dest.name + ".sha256").write_text(f"{digest}  {name}\n")
        return {"path": str(dest), "name": name, "version": version, "index_digest": index_digest,
                "manifest_digest": got["digest"], "cosign": cos[0]}
    finally:
        shutil.rmtree(work, ignore_errors=True)
