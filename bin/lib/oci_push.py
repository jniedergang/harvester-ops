"""harvester-ops : l'image VDDK construite et poussée sans outil de conteneur (v1.75.0).

Forklift copie le VDDK de VMware dans ses pods par une image d'amorçage
(`vddkInitImage` du fournisseur) : le dossier vmware-vix-disklib-distrib/ à
la racine, et une commande qui le copie dans /opt. La licence de VMware
interdit de livrer le VDDK ; la console n'a ni podman ni buildah. L'image se
construit donc ici, par l'API de registre v2 :

- l'archive VDDK de VMware (tar.gz, tout sous vmware-vix-disklib-distrib/)
  EST la couche du dessus, telle quelle : son empreinte est celle du
  fichier, son diff_id celle du tar décompressé ;
- la base (bci-busybox de SUSE, pour `cp`) est recopiée couche par couche
  depuis son registre, sans être décompressée ; la version linux/amd64 d'un
  index multi-architecture ;
- jeton Bearer (realm, service, scope) ou Basic ; blobs envoyés d'un bloc ;
  redirections suivies SANS l'en-tête Authorization (les CDN des registres
  refusent un second mode d'authentification).

Bibliothèque standard seulement.
"""

import base64
import gzip
import hashlib
import json
import os
import re
import ssl
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

VDDK_DIR = "vmware-vix-disklib-distrib"
VDDK_LIB = f"{VDDK_DIR}/lib64/libvixDiskLib.so"
DEFAULT_BASE = "registry.suse.com/bci/bci-busybox:16.0"
ENTRYPOINT = ["cp", "-r", f"/{VDDK_DIR}", "/opt"]

M_DOCKER = "application/vnd.docker.distribution.manifest.v2+json"
M_DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
M_OCI = "application/vnd.oci.image.manifest.v1+json"
M_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
LAYER_TYPE = {M_DOCKER: "application/vnd.docker.image.rootfs.diff.tar.gzip",
              M_OCI: "application/vnd.oci.image.layer.v1.tar+gzip"}
CONFIG_TYPE = {M_DOCKER: "application/vnd.docker.container.image.v1+json",
               M_OCI: "application/vnd.oci.image.config.v1+json"}
ACCEPT = ", ".join((M_OCI_INDEX, M_DOCKER_LIST, M_OCI, M_DOCKER))

REF_RE = re.compile(r"^(?P<reg>[a-z0-9.-]+(?::\d{1,5})?)/"
                    r"(?P<repo>[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)"
                    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?$")


class RegistryError(Exception):
    """Panne de registre explicable ; `code` nomme les cas que l'appelant
    distingue (`publish-tag-differs`, `no-platform`, `digest-mismatch`)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


CHUNK = 1 << 20                  # bloc de lecture et d'écriture en flux
UPLOAD_CHUNK = 64 << 20          # au-delà, envoi par morceaux (PATCH) reprenable
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class _Limited:
    """Lecture d'au plus `n` octets d'un fichier ouvert (un morceau d'envoi)."""

    def __init__(self, f, n, on_bytes=None):
        self.f, self.n, self.left, self.on_bytes = f, n, n, on_bytes
        self.start = f.tell()

    def tell(self):
        return 0

    def seek(self, _pos):
        """Relecture complète (nouvel essai après un 401)."""
        self.f.seek(self.start)
        self.left = self.n

    def read(self, size=-1):
        if self.left <= 0:
            return b""
        size = self.left if size is None or size < 0 else min(size, self.left)
        b = self.f.read(size)
        self.left -= len(b)
        if b and self.on_bytes:
            self.on_bytes(len(b))
        return b


def digest_of(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def parse_ref(ref):
    """(registre, dépôt, étiquette ou empreinte), lus comme Docker : un
    premier élément sans point, deux-points ni « localhost » est Docker Hub."""
    ref = str(ref or "").strip()
    first, _, rest = ref.partition("/")
    if not rest or not ("." in first or ":" in first or first == "localhost"):
        ref = "docker.io/" + (ref if rest else "library/" + ref)
    m = REF_RE.match(ref)
    if not m:
        raise ValueError(f"image: {ref!r} is not an image reference")
    reg = "registry-1.docker.io" if m.group("reg") == "docker.io" else m.group("reg")
    return reg, m.group("repo"), m.group("digest") or m.group("tag") or "latest"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Registry:
    """Client minimal de l'API de registre v2 (distribution)."""

    def __init__(self, host, creds=None, plain_http=False, timeout=120, ca_pem=None):
        """`ca_pem` : CA propre au registre (PEM), à la place des CA du
        système (registre d'amorçage, registre du client à CA privée)."""
        self.host = host
        self.creds = creds or None
        self.base = f"{'http' if plain_http else 'https'}://{host}"
        self.timeout = timeout
        self.auth = {}
        try:
            ctx = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
        except (ssl.SSLError, ValueError) as e:
            raise RegistryError(f"{host}: unreadable registry CA ({e})") from None
        self.opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ctx))

    def _own(self, url):
        """Vrai si `url` vise le registre même (schéma et hôte:port)."""
        a, b = urllib.parse.urlparse(url), urllib.parse.urlparse(self.base)
        return (a.scheme, a.netloc) == (b.scheme, b.netloc)

    def _basic(self):
        if not self.creds:
            return None
        raw = f"{self.creds.get('username', '')}:{self.creds.get('password', '')}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _token(self, challenge, scope):
        p = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if "realm" not in p:
            raise RegistryError(f"{self.host}: unreadable authentication challenge")
        q = {"service": p.get("service", "")}
        if scope:
            q["scope"] = scope
        req = urllib.request.Request(p["realm"] + "?" + urllib.parse.urlencode(q))
        if self._basic():
            req.add_header("Authorization", self._basic())
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            e.close()
            raise RegistryError(f"{self.host}: authentication refused (HTTP {e.code})") from None
        except urllib.error.URLError as e:
            raise RegistryError(f"{self.host}: authentication failed ({e.reason})") from None
        except OSError as e:
            raise RegistryError(f"{self.host}: authentication failed ({e})") from None
        tok = body.get("token") or body.get("access_token")
        if not tok:
            raise RegistryError(f"{self.host}: authentication refused (no token)")
        return "Bearer " + tok

    def _http_error(self, method, url, e):
        text = e.read()[:200].decode(errors="replace")
        e.close()
        return RegistryError(f"{method} {self.host}{urllib.parse.urlparse(url).path}: HTTP {e.code} {text}")

    def _read(self, r, stream_to):
        if stream_to is None:
            return r.read()
        if hasattr(stream_to, "start"):
            stream_to.start(r.status)
        while True:
            b = r.read(CHUNK)
            if not b:
                return b""
            stream_to.write(b)

    def request(self, method, url, scope, data=None, headers=None, ok=(),
                stream_to=None, body_file=None, body_size=None):
        """(code, en-têtes, corps). Un 401 fait prendre un jeton ou poser
        Basic, une fois ; une redirection est suivie sans identifiant. Toute
        panne, y compris celle de la cible d'une redirection, devient une
        RegistryError qui nomme l'hôte, jamais un identifiant.

        En flux : `stream_to` (objet à `write`) reçoit la réponse par blocs
        de 1 Mio, le corps rendu est alors vide ; `body_file` (fichier ouvert,
        lu à partir de sa position) est envoyé avec `Content-Length:
        body_size`, relu depuis la même position si un 401 oblige à
        recommencer."""
        if not url.startswith("http"):
            url = self.base + url
        # revue 2.6.0 : une adresse d'un autre hôte (Location d'un envoi) ne
        # reçoit jamais d'identifiant, et son 401 n'en fait pas chercher
        own = self._own(url)
        start = body_file.tell() if body_file is not None and hasattr(body_file, "tell") else None
        for attempt in (0, 1):
            h = dict(headers or {})
            if self.auth.get(scope) and own:
                h["Authorization"] = self.auth[scope]
            body = data
            if body_file is not None:
                if attempt and start is not None:
                    body_file.seek(start)
                body = body_file
                h["Content-Length"] = str(body_size)
            req = urllib.request.Request(url, data=body, method=method, headers=h)
            try:
                with self.opener.open(req, timeout=self.timeout) as r:
                    return r.status, r.headers, self._read(r, stream_to)
            except urllib.error.HTTPError as e:
                try:
                    if e.code in (301, 302, 303, 307, 308) and method in ("GET", "HEAD") and e.headers.get("Location"):
                        loc = urllib.parse.urljoin(url, e.headers["Location"])
                        rh = {k: v for k, v in (headers or {}).items() if k.lower() in ("range", "accept")}
                        try:
                            with self.opener.open(urllib.request.Request(loc, method=method, headers=rh),
                                                  timeout=self.timeout) as r:
                                return r.status, r.headers, self._read(r, stream_to)
                        except urllib.error.HTTPError as e2:
                            if e2.code in ok:
                                e2.close()
                                return e2.code, e2.headers, b""
                            raise self._http_error(method, loc, e2) from None
                        except urllib.error.URLError as e2:
                            raise RegistryError(f"{method} {self.host}: {e2.reason}") from None
                        except OSError as e2:
                            raise RegistryError(f"{method} {self.host}: {e2}") from None
                    if e.code == 401 and not own:
                        raise RegistryError(f"{self.host}: authentication refused by "
                                            f"{urllib.parse.urlparse(url).netloc}, not the registry") from None
                    if e.code == 401 and attempt == 0:
                        ch = e.headers.get("WWW-Authenticate", "")
                        if ch.lower().startswith("bearer"):
                            self.auth[scope] = self._token(ch, scope)
                        elif self._basic():
                            self.auth[scope] = self._basic()
                        else:
                            raise RegistryError(f"{self.host}: authentication required") from None
                        continue
                    if e.code in ok:
                        return e.code, e.headers, b""
                    if e.code == 401:
                        raise RegistryError(f"{self.host}: authentication refused") from None
                    raise self._http_error(method, url, e) from None
                finally:
                    e.close()
            except urllib.error.URLError as e:
                raise RegistryError(f"{method} {self.host}: {e.reason}") from None
            except OSError as e:
                raise RegistryError(f"{method} {self.host}: {e}") from None
        raise RegistryError(f"{self.host}: authentication refused")

    def manifest(self, repo, ref):
        _, h, body = self.request("GET", f"/v2/{repo}/manifests/{ref}", f"repository:{repo}:pull",
                                  headers={"Accept": ACCEPT})
        mt = (h.get("Content-Type") or "").split(";")[0].strip() or json.loads(body).get("mediaType", "")
        return body, mt

    def blob(self, repo, digest):
        _, _, body = self.request("GET", f"/v2/{repo}/blobs/{digest}", f"repository:{repo}:pull")
        if digest_of(body) != digest:
            raise RegistryError(f"{self.host}/{repo}: blob {digest[:19]} does not match its digest")
        return body

    def has_blob(self, repo, digest):
        code, _, _ = self.request("HEAD", f"/v2/{repo}/blobs/{digest}", f"repository:{repo}:pull,push", ok=(404,))
        return code == 200

    def push_blob(self, repo, digest, data):
        """Envoi d'un bloc (POST puis PUT ?digest=) ; False si déjà présent."""
        if self.has_blob(repo, digest):
            return False
        scope = f"repository:{repo}:pull,push"
        _, h, _ = self.request("POST", f"/v2/{repo}/blobs/uploads/", scope, data=b"",
                               headers={"Content-Length": "0"})
        loc = h.get("Location")
        if not loc:
            raise RegistryError(f"{self.host}/{repo}: no upload location")
        loc = urllib.parse.urljoin(self.base + "/", loc)
        sep = "&" if "?" in loc else "?"
        self.request("PUT", f"{loc}{sep}digest={urllib.parse.quote(digest)}", scope, data=data,
                     headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(data))})
        return True

    def put_manifest(self, repo, tag, body, media_type):
        _, h, _ = self.request("PUT", f"/v2/{repo}/manifests/{tag}", f"repository:{repo}:pull,push",
                               data=body, headers={"Content-Type": media_type})
        return h.get("Docker-Content-Digest") or digest_of(body)

    def manifest_digest(self, repo, ref):
        """Empreinte du manifeste désigné par `ref`, ou None s'il n'existe
        pas (HEAD ; GET si le registre ne donne pas Docker-Content-Digest)."""
        code, h, _ = self.request("HEAD", f"/v2/{repo}/manifests/{ref}", f"repository:{repo}:pull",
                                  headers={"Accept": ACCEPT}, ok=(404,))
        if code == 404:
            return None
        d = h.get("Docker-Content-Digest")
        if d and DIGEST_RE.match(d):
            return d
        body, _ = self.manifest(repo, ref)
        return digest_of(body)

    def blob_to_file(self, repo, digest, path, on_bytes=None):
        """Blob écrit dans `path` en flux : `path.part` d'abord, empreinte
        calculée pendant l'écriture, renommé seulement si elle est juste
        (sinon supprimé, RegistryError `digest-mismatch`). Un `.part` laissé
        par une copie interrompue est repris où il s'arrête (Range) ; si le
        registre renvoie tout le blob (200), la copie repart de zéro."""
        if not DIGEST_RE.match(digest or ""):
            raise RegistryError(f"{self.host}/{repo}: {digest!r} is not a sha256 digest")
        part = str(path) + ".part"
        h, have = hashlib.sha256(), 0
        try:
            with open(part, "rb") as f:
                for b in iter(lambda: f.read(CHUNK), b""):
                    h.update(b)
                    have += len(b)
        except FileNotFoundError:
            pass
        try:
            with open(part, "ab" if have else "wb") as f:
                sink = _BlobSink(f, h, have, on_bytes)
                self.request("GET", f"/v2/{repo}/blobs/{digest}", f"repository:{repo}:pull",
                             headers={"Range": f"bytes={have}-"} if have else None, ok=(416,),
                             stream_to=sink)
            if "sha256:" + sink.h.hexdigest() != digest:
                raise RegistryError(f"{self.host}/{repo}: blob {digest[:19]} does not match its digest",
                                    code="digest-mismatch")
            os.replace(part, path)
        except BaseException:
            try:
                os.unlink(part)
            except OSError:
                pass
            raise
        return path

    def _upload_url(self, loc):
        return urllib.parse.urljoin(self.base + "/", loc)

    def push_blob_file(self, repo, digest, path, size, mount_from=None, on_bytes=None,
                       chunk=UPLOAD_CHUNK):
        """Envoi d'un blob depuis un fichier, sans le garder en mémoire.
        Rend False s'il est déjà présent, "mounted" s'il a été monté depuis
        le dépôt `mount_from` du même registre (201), True s'il a été
        envoyé. Jusqu'à `chunk` octets : un seul PUT ; au-delà, des PATCH
        successifs, et une coupure en cours de route reprend à l'octet que
        le registre dit avoir reçu (GET de la session d'envoi)."""
        if self.has_blob(repo, digest):
            return False
        scope = f"repository:{repo}:pull,push"
        url = f"/v2/{repo}/blobs/uploads/"
        if mount_from and mount_from != repo:
            url += "?" + urllib.parse.urlencode({"mount": digest, "from": mount_from})
        code, h, _ = self.request("POST", url, scope, data=b"", headers={"Content-Length": "0"})
        if code == 201:
            return "mounted"
        loc = h.get("Location")
        if not loc:
            raise RegistryError(f"{self.host}/{repo}: no upload location")
        loc = self._upload_url(loc)
        with open(path, "rb") as f:
            if size > chunk:
                loc = self._patch_chunks(repo, scope, loc, f, size, chunk, on_bytes)
                f.seek(size)
                body, n = None, 0
            else:
                body, n = _Limited(f, size, on_bytes), size
            sep = "&" if "?" in loc else "?"
            hdr = {"Content-Type": "application/octet-stream"}
            if body is None:
                self.request("PUT", f"{loc}{sep}digest={urllib.parse.quote(digest)}", scope,
                             data=b"", headers=dict(hdr, **{"Content-Length": "0"}))
            else:
                self.request("PUT", f"{loc}{sep}digest={urllib.parse.quote(digest)}", scope,
                             headers=hdr, body_file=body, body_size=n)
        return True

    def _patch_chunks(self, repo, scope, loc, f, size, chunk, on_bytes, retries=3):
        """Morceaux `Content-Range: début-fin` ; après une panne réseau, la
        session dit (`Range: 0-<dernier octet>`) où reprendre."""
        offset, failures = 0, 0
        while offset < size:
            n = min(chunk, size - offset)
            f.seek(offset)
            try:
                _, h, _ = self.request("PATCH", loc, scope, body_file=_Limited(f, n, on_bytes), body_size=n,
                                       headers={"Content-Type": "application/octet-stream",
                                                "Content-Range": f"{offset}-{offset + n - 1}"})
            except RegistryError:
                failures += 1
                if failures > retries:
                    raise
                code, sh, _ = self.request("GET", loc, scope, ok=(404,))
                if code == 404:
                    raise
                offset = _range_end(sh.get("Range")) + 1
                loc = self._upload_url(sh.get("Location") or loc)
                continue
            offset += n
            loc = self._upload_url(h.get("Location") or loc)
        return loc


def _range_end(value):
    """'0-1023' -> 1023 ; absent ou vide -> -1 (rien de reçu)."""
    m = re.match(r"^(?:bytes=)?0-(\d+)$", (value or "").strip())
    return int(m.group(1)) if m else -1


class _BlobSink:
    """Destination d'un GET de blob : écrit et hache ; si le registre a
    ignoré la demande de reprise (statut 200 et non 206), vide d'abord le
    fichier partiel."""

    def __init__(self, f, h, have, on_bytes):
        self.f, self.h, self.have, self.on_bytes = f, h, have, on_bytes

    def start(self, status):
        if self.have and status != 206:
            self.f.seek(0)
            self.f.truncate()
            self.h = hashlib.sha256()

    def write(self, b):
        self.h.update(b)
        self.f.write(b)
        if self.on_bytes:
            self.on_bytes(len(b))


def archive_layer(path):
    """L'archive VDDK comme couche, après contrôle : tout sous
    vmware-vix-disklib-distrib/, chemins relatifs sans « .. », liens qui ne
    sortent pas du dossier, et la bibliothèque présente."""
    h_gz, h_tar, size = hashlib.sha256(), hashlib.sha256(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h_gz.update(chunk)
            size += len(chunk)
    with gzip.open(path, "rb") as g:
        for chunk in iter(lambda: g.read(1 << 20), b""):
            h_tar.update(chunk)
    names = []
    with tarfile.open(path, "r:gz") as t:
        for m in t:
            n = m.name[2:] if m.name.startswith("./") else m.name
            parts = n.split("/")
            if n.startswith("/") or ".." in parts or parts[0] != VDDK_DIR:
                raise ValueError(f"VDDK archive: unexpected entry {m.name!r}, everything must be under {VDDK_DIR}/")
            if m.issym() or m.islnk():
                target = m.linkname
                if target.startswith("/") or ".." in target.split("/"):
                    raise ValueError(f"VDDK archive: link {m.name!r} points outside ({target!r})")
            names.append(n)
    if not any(n.startswith(VDDK_LIB) for n in names):
        raise ValueError(f"VDDK archive: no {VDDK_LIB}, this is not a VDDK")
    return {"digest": "sha256:" + h_gz.hexdigest(), "diff_id": "sha256:" + h_tar.hexdigest(), "size": size}


def vddk_config(base_config, diff_id, created):
    cfg = json.loads(json.dumps(base_config))
    cfg.setdefault("rootfs", {"type": "layers", "diff_ids": []}).setdefault("diff_ids", []).append(diff_id)
    c = cfg.get("config") or {}
    c["Entrypoint"] = list(ENTRYPOINT)
    c["Cmd"] = None
    c["User"] = "1001"
    c["Labels"] = dict(c.get("Labels") or {}, **{"io.harvester-ops.vddk": "true"})
    cfg["config"] = c
    cfg["created"] = created
    cfg.setdefault("history", []).append({"created": created,
                                          "created_by": f"harvester-ops: VDDK layer ({VDDK_DIR})"})
    return cfg


def push_vddk_image(archive, image, base=DEFAULT_BASE, target_creds=None, base_creds=None,
                    plain_http=False, base_plain_http=False, step=lambda msg: None, now=time.gmtime):
    t_reg, t_repo, t_tag = parse_ref(image)
    if t_tag.startswith("sha256:"):
        raise ValueError("image: give a tag to push to, not a digest")
    layer = archive_layer(archive)
    step(f"VDDK archive checked ({layer['size'] >> 20} MiB)")
    b_reg, b_repo, b_ref = parse_ref(base)
    src = Registry(b_reg, base_creds, plain_http=base_plain_http)
    dst = Registry(t_reg, target_creds, plain_http=plain_http)
    body, mt = src.manifest(b_repo, b_ref)
    if mt in (M_OCI_INDEX, M_DOCKER_LIST):
        pick = next((m for m in json.loads(body).get("manifests", [])
                     if (m.get("platform") or {}).get("os") == "linux"
                     and (m.get("platform") or {}).get("architecture") == "amd64"), None)
        if not pick:
            raise RegistryError(f"{base}: no linux/amd64 image")
        body, mt = src.manifest(b_repo, pick["digest"])
    if mt not in (M_OCI, M_DOCKER):
        raise RegistryError(f"{base}: unsupported manifest type {mt!r}")
    man = json.loads(body)
    cfg = json.loads(src.blob(b_repo, man["config"]["digest"]))
    for lay in man["layers"]:
        if not dst.has_blob(t_repo, lay["digest"]):
            dst.push_blob(t_repo, lay["digest"], src.blob(b_repo, lay["digest"]))
            step(f"base layer {lay['digest'][7:19]} copied")
    with open(archive, "rb") as f:
        if dst.push_blob(t_repo, layer["digest"], f.read()):
            step("VDDK layer sent")
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", now())
    new_cfg = json.dumps(vddk_config(cfg, layer["diff_id"], created), separators=(",", ":")).encode()
    dst.push_blob(t_repo, digest_of(new_cfg), new_cfg)
    new_man = {"schemaVersion": 2, "mediaType": mt,
               "config": {"mediaType": CONFIG_TYPE[mt], "digest": digest_of(new_cfg), "size": len(new_cfg)},
               "layers": man["layers"] + [{"mediaType": LAYER_TYPE[mt], "digest": layer["digest"],
                                           "size": layer["size"]}]}
    digest = dst.put_manifest(t_repo, t_tag, json.dumps(new_man, separators=(",", ":")).encode(), mt)
    step(f"pushed {t_reg}/{t_repo}:{t_tag}")
    return {"image": f"{t_reg}/{t_repo}:{t_tag}", "pinned": f"{t_reg}/{t_repo}@{digest}", "digest": digest}
