"""harvester-ops : images et charts en disposition OCI sur disque (v2.6.0).

Le paquet d'artefacts porte ses images dans une disposition OCI
(`oci-layout`, `index.json`, `blobs/sha256/<empreinte>`) : les couches
partagées entre images n'y sont qu'une fois, `ctr images import` la lit
telle quelle, et l'API de registre v2 la publie couche par couche.

- copie d'un registre vers la disposition et retour, en flux (`oci_push`),
  octets inchangés : les empreintes, donc une signature amont éventuelle,
  restent valables ;
- une seule plateforme (`linux/amd64` par défaut) tirée d'un index
  multi-architecture ;
- un chart Helm devient un artefact OCI aux types de Helm ;
- rangement dans un registre : `<registre>/<hôte d'origine>/<chemin>:<tag>`.

Bibliothèque standard seulement (le module est chargé par
`harvester-artefacts pack` sur une machine quelconque).
"""

import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path

import oci_push as op
from self_update import sha256_file

LAYOUT_VERSION = {"imageLayoutVersion": "1.0.0"}
M_HELM_CONFIG = "application/vnd.cncf.helm.config.v1+json"
M_HELM_CHART = "application/vnd.cncf.helm.chart.content.v1.tar+gzip"
A_REF = "org.opencontainers.image.ref.name"
A_NAME = "io.containerd.image.name"
A_CHART = "io.harvester-ops.chart"
DEFAULT_PLATFORM = "linux/amd64"
MANIFEST_TYPES = (op.M_OCI, op.M_DOCKER)
INDEX_TYPES = (op.M_OCI_INDEX, op.M_DOCKER_LIST)
IMAGE_CONFIG_TYPES = tuple(op.CONFIG_TYPE.values())


def normalize_ref(image_ref):
    """(hôte, chemin, tag, empreinte) d'une référence lue comme Docker :
    `docker.io` explicite, `library/` pour les images officielles, `latest`
    seulement s'il n'y a ni tag ni empreinte."""
    ref = str(image_ref or "").strip()
    first, _, rest = ref.partition("/")
    if not rest or not ("." in first or ":" in first or first == "localhost"):
        ref = "docker.io/" + (ref if rest else "library/" + ref)
    m = op.REF_RE.match(ref)
    if not m:
        raise ValueError(f"image: {image_ref!r} is not an image reference")
    host, path = m.group("reg"), m.group("repo")
    if host == "docker.io" and "/" not in path:
        path = "library/" + path
    tag, digest = m.group("tag"), m.group("digest")
    if not tag and not digest:
        tag = "latest"
    return host, path, tag, digest


def full_name(image_ref):
    """La référence complète, telle qu'écrite dans `io.containerd.image.name`."""
    host, path, tag, digest = normalize_ref(image_ref)
    return f"{host}/{path}" + (f":{tag}" if tag else "") + (f"@{digest}" if digest else "")


def target_repo(image_ref):
    """(dépôt, tag ou empreinte) dans un registre de la console (point 186) :
    `<hôte>/<chemin>` ; un port d'hôte devient `_<port>` (« : » est interdit
    dans un nom de dépôt), à reprendre tel quel dans les `rewrite` des
    miroirs RKE2."""
    host, path, tag, digest = normalize_ref(image_ref)
    return f"{host.replace(':', '_')}/{path}", tag or digest


def chart_repo(name, version):
    """(dépôt, tag) d'un chart : `charts/<chart>`, tag = version (« + »
    remplacé par « _ » comme le fait Helm)."""
    return f"charts/{name}", version.replace("+", "_")


def _platform(platform):
    parts = (platform or DEFAULT_PLATFORM).split("/")
    if len(parts) not in (2, 3) or not all(parts):
        raise ValueError(f"platform: {platform!r} is not os/arch[/variant]")
    return parts


def _write_atomic(path, data):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Layout:
    """Une disposition OCI : `init()` la crée si besoin, `add_entry` puis
    `write_index()` nomment les images."""

    def __init__(self, root):
        self.root = Path(root)
        self.index = {"schemaVersion": 2, "mediaType": op.M_OCI_INDEX, "manifests": []}
        idx = self.root / "index.json"
        if idx.is_file():
            self.index = json.loads(idx.read_text())

    def init(self):
        (self.root / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
        if not (self.root / "oci-layout").is_file():
            _write_atomic(self.root / "oci-layout", json.dumps(LAYOUT_VERSION).encode())
        if not (self.root / "index.json").is_file():
            self.write_index()
        return self

    def blob_path(self, digest):
        if not op.DIGEST_RE.match(digest or ""):
            raise ValueError(f"{digest!r} is not a sha256 digest")
        return self.root / "blobs" / "sha256" / digest[7:]

    def has_blob(self, digest):
        return self.blob_path(digest).is_file()

    def read_blob(self, digest):
        data = self.blob_path(digest).read_bytes()
        if op.digest_of(data) != digest:
            raise op.RegistryError(f"layout: blob {digest[:19]} does not match its digest", code="digest-mismatch")
        return data

    def add_blob_bytes(self, data):
        digest = op.digest_of(data)
        if not self.has_blob(digest):
            _write_atomic(self.blob_path(digest), data)
        return digest

    def add_blob_file(self, src, digest):
        """Copie d'un fichier, empreinte contrôlée pendant la copie."""
        dest = self.blob_path(digest)
        if dest.is_file():
            return dest
        part = Path(str(dest) + ".part")
        h = hashlib.sha256()
        try:
            with open(src, "rb") as fi, open(part, "wb") as fo:
                for b in iter(lambda: fi.read(op.CHUNK), b""):
                    h.update(b)
                    fo.write(b)
            if "sha256:" + h.hexdigest() != digest:
                raise op.RegistryError(f"layout: {src} does not match {digest[:19]}", code="digest-mismatch")
            os.replace(part, dest)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return dest

    def entries(self):
        return list(self.index.get("manifests") or [])

    def find(self, name):
        """L'entrée dont `io.containerd.image.name` (image) ou le chart vaut
        `name`, ou None."""
        for e in self.entries():
            ann = e.get("annotations") or {}
            if name in (ann.get(A_NAME), ann.get(A_CHART)):
                return e
        return None

    def add_entry(self, desc, ref_name, image_name, extra=None):
        """Ajoute (ou remplace, même nom) une entrée de l'index ; écrite par
        `write_index`."""
        ann = dict(desc.get("annotations") or {}, **(extra or {}))
        if ref_name:
            ann[A_REF] = ref_name
        if image_name:
            ann[A_NAME] = image_name
        entry = dict(desc, annotations=ann)
        key = image_name or ann.get(A_CHART)
        self.index["manifests"] = [e for e in self.entries()
                                   if key not in ((e.get("annotations") or {}).get(A_NAME),
                                                  (e.get("annotations") or {}).get(A_CHART))]
        self.index["manifests"].append(entry)
        return entry

    def write_index(self):
        self.root.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.root / "index.json", json.dumps(self.index, indent=2).encode())

    def blobs_of(self, entry):
        """Toutes les empreintes qu'une entrée emploie : manifeste,
        configuration, couches (et manifestes d'un index)."""
        out, todo = [], [entry]
        while todo:
            d = todo.pop()
            out.append(d["digest"])
            body = json.loads(self.read_blob(d["digest"]))
            if d.get("mediaType") in INDEX_TYPES or body.get("manifests"):
                todo.extend(body.get("manifests") or [])
                continue
            out.append(body["config"]["digest"])
            out.extend(l["digest"] for l in body.get("layers") or [])
        return out


def _pick_platform(src, repo, body, ref, platform):
    want = _platform(platform)
    for m in json.loads(body).get("manifests") or []:
        p = m.get("platform") or {}
        got = [p.get("os"), p.get("architecture")] + ([p["variant"]] if len(want) == 3 and p.get("variant") else [])
        if got[:len(want)] == want:
            return src.manifest(repo, m["digest"]), {"os": want[0], "architecture": want[1],
                                                     **({"variant": want[2]} if len(want) == 3 else {})}
    raise op.RegistryError(f"{ref}: no {platform} image in its index", code="no-platform")


def copy_image(src, ref, layout, platform=DEFAULT_PLATFORM, progress=None):
    """Copie l'image `ref` du registre `src` (un `oci_push.Registry`) dans
    `layout`, une plateforme, octets inchangés. Rend {"name", "digest",
    "size", "platform", "layers"}. `progress(octets)` reçoit l'avancement."""
    host, path, tag, digest = normalize_ref(ref)
    name = full_name(ref)
    (body, mt) = src.manifest(path, digest or tag)
    if digest and op.digest_of(body) != digest:
        raise op.RegistryError(f"{ref}: manifest does not match its digest", code="digest-mismatch")
    plat = None
    if mt in INDEX_TYPES:
        (body, mt), plat = _pick_platform(src, path, body, ref, platform)
    if mt not in MANIFEST_TYPES:
        raise op.RegistryError(f"{ref}: unsupported manifest type {mt!r}")
    man = json.loads(body)
    layout.init()
    if plat is None and man["config"].get("mediaType") in IMAGE_CONFIG_TYPES:
        # image d'une seule plateforme : sa configuration dit laquelle
        cd = man["config"]["digest"]
        if not layout.has_blob(cd):
            src.blob_to_file(path, cd, layout.blob_path(cd))
        cfg = json.loads(layout.read_blob(cd))
        want = _platform(platform)
        got = [cfg.get("os"), cfg.get("architecture")] + ([cfg.get("variant")] if len(want) == 3 else [])
        if got != want:
            raise op.RegistryError(f"{ref}: a {'/'.join(map(str, got))} image, not {platform}", code="no-platform")
        plat = {"os": want[0], "architecture": want[1], **({"variant": want[2]} if len(want) == 3 else {})}
    blobs = [man["config"]] + list(man.get("layers") or [])
    for b in blobs:
        if not layout.has_blob(b["digest"]):
            src.blob_to_file(path, b["digest"], layout.blob_path(b["digest"]), on_bytes=progress)
    mdigest = layout.add_blob_bytes(body)
    desc = {"mediaType": mt, "digest": mdigest, "size": len(body), **({"platform": plat} if plat else {})}
    layout.add_entry(desc, tag or "", name)
    layout.write_index()
    return {"name": name, "digest": mdigest, "size": sum(int(b.get("size") or 0) for b in blobs) + len(body),
            "platform": "/".join(plat.values()) if plat else None, "layers": len(man.get("layers") or [])}


def push_image(layout, entry, dst, repo, tag, progress=None, mounts=None):
    """Publie une entrée de la disposition dans `dst` sous `repo:tag` :
    couches, puis configuration, puis manifeste. Même empreinte déjà sous ce
    tag : {"pushed": False} ; autre empreinte : RegistryError
    `publish-tag-differs` (jamais écrasée). `mounts` (empreinte -> dépôt du
    même registre qui l'a déjà) permet le montage entre dépôts et se
    complète au fil des envois."""
    want = entry["digest"]
    have = dst.manifest_digest(repo, tag)
    if have == want:
        return {"pushed": False, "digest": want, "sent": 0, "skipped": 0, "mounted": 0}
    if have is not None:
        raise op.RegistryError(f"{dst.host}/{repo}:{tag} already exists with another digest ({have[:19]})",
                               code="publish-tag-differs")
    body = layout.read_blob(want)
    man = json.loads(body)
    stats = {"sent": 0, "skipped": 0, "mounted": 0}
    mounts = mounts if mounts is not None else {}
    for b in list(man.get("layers") or []) + [man["config"]]:
        d = b["digest"]
        p = layout.blob_path(d)
        r = dst.push_blob_file(repo, d, p, p.stat().st_size, mount_from=mounts.get(d), on_bytes=progress)
        stats["sent" if r is True else "mounted" if r == "mounted" else "skipped"] += 1
        mounts.setdefault(d, repo)
    digest = dst.put_manifest(repo, tag, body, entry.get("mediaType") or man.get("mediaType") or op.M_OCI)
    if digest != want:
        raise op.RegistryError(f"{dst.host}/{repo}:{tag}: registry stored {digest[:19]}, expected {want[:19]}",
                               code="digest-mismatch")
    return {"pushed": True, "digest": want, **stats}


_TOP_KV = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):[ \t]*(.*?)[ \t]*$")


def _chart_yaml(text):
    """Les scalaires de premier niveau de Chart.yaml (`clé: valeur`), seuls
    utiles ici ; guillemets retirés, listes et blocs ignorés."""
    out = {}
    for line in text.splitlines():
        if not line or line[0] in " \t#-":
            continue
        m = _TOP_KV.match(line)
        if not m or not m.group(2) or m.group(2)[0] in "|>[{&*":
            continue
        v = m.group(2)
        if " #" in v and v[0] not in "\"'":
            v = v.split(" #", 1)[0].rstrip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[m.group(1)] = v
    return out


def chart_to_layout(tgz, layout):
    """Le chart `tgz` (sortie de `helm package`) en artefact OCI aux types de
    Helm. Rend {"name", "version", "digest", "size"}."""
    meta = None
    try:
        with tarfile.open(tgz, "r:gz") as t:
            for m in t:
                parts = m.name.split("/")
                if len(parts) == 2 and parts[1] == "Chart.yaml" and m.isfile():
                    meta = _chart_yaml(t.extractfile(m).read().decode("utf-8", "replace"))
                    break
    except (OSError, tarfile.TarError) as e:
        raise ValueError(f"chart {Path(tgz).name}: not a readable .tgz ({e})") from None
    if meta is None:
        raise ValueError(f"chart {Path(tgz).name}: no <chart>/Chart.yaml")
    if not meta.get("name") or not meta.get("version"):
        raise ValueError(f"chart {Path(tgz).name}: Chart.yaml has no name or version")
    layout.init()
    size = os.path.getsize(tgz)
    cdigest = "sha256:" + sha256_file(tgz)
    layout.add_blob_file(tgz, cdigest)
    keys = ("name", "version", "apiVersion", "appVersion", "description", "type", "kubeVersion")
    cfg = json.dumps({k: meta[k] for k in keys if k in meta}, separators=(",", ":")).encode()
    cfg_d = layout.add_blob_bytes(cfg)
    man = json.dumps({"schemaVersion": 2, "mediaType": op.M_OCI,
                      "config": {"mediaType": M_HELM_CONFIG, "digest": cfg_d, "size": len(cfg)},
                      "layers": [{"mediaType": M_HELM_CHART, "digest": cdigest, "size": size}],
                      "annotations": {"org.opencontainers.image.title": meta["name"],
                                      "org.opencontainers.image.version": meta["version"]}},
                     separators=(",", ":")).encode()
    mdigest = layout.add_blob_bytes(man)
    ref = f"{meta['name']}:{meta['version']}"
    layout.add_entry({"mediaType": op.M_OCI, "digest": mdigest, "size": len(man)},
                     chart_repo(meta["name"], meta["version"])[1], None, extra={A_CHART: ref})
    layout.write_index()
    return {"name": meta["name"], "version": meta["version"], "digest": mdigest, "size": size}


def pinned_name(entry):
    """`<hôte>/<chemin>@<empreinte>` d'une entrée d'image."""
    host, path, _, _ = normalize_ref((entry.get("annotations") or {}).get(A_NAME, ""))
    return f"{host}/{path}@{entry['digest']}"


def export_ctr_tar(layout, names, out):
    """Archive OCI (tar non compressé) des images `names` de la disposition,
    lisible par `ctr images import` : oci-layout, index.json réduit à ces
    images, et leurs seuls blobs. Chaque image y figure deux fois, sous son
    tag et sous `<dépôt>@<empreinte>` : containerd ne connaît une image
    importée que par les noms de l'archive, et un pod qui l'épingle par
    empreinte (`image: x@sha256:...`) ne la trouverait pas sans le second
    (vu sur harvlab2, tâche 1 de la 2.6.0 : ErrImageNeverPull)."""
    picked, blobs = [], []
    for n in names:
        e = layout.find(n)
        if e is None:
            raise ValueError(f"layout: no image named {n!r}")
        picked.append(e)
        if "@" not in e["annotations"].get(A_NAME, "@"):
            ann = {k: v for k, v in e["annotations"].items() if k != A_REF}
            picked.append(dict(e, annotations=dict(ann, **{A_NAME: pinned_name(e)})))
        for d in layout.blobs_of(e):
            if d not in blobs:
                blobs.append(d)
    index = dict(layout.index, manifests=picked)

    def add_bytes(t, name, data):
        ti = tarfile.TarInfo(name)
        ti.size, ti.mode, ti.mtime = len(data), 0o644, 0
        t.addfile(ti, io.BytesIO(data))

    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as t:
        add_bytes(t, "oci-layout", json.dumps(LAYOUT_VERSION).encode())
        add_bytes(t, "index.json", json.dumps(index).encode())
        for d in blobs:
            p = layout.blob_path(d)
            ti = t.gettarinfo(str(p), arcname=f"blobs/sha256/{d[7:]}")
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mode, ti.mtime = 0o644, 0
            with open(p, "rb") as f:
                t.addfile(ti, f)
    return {"images": len(names), "blobs": len(blobs)}
