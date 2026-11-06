"""v1.75.0 : l'image VDDK construite et poussée sans podman ni buildah,
contre deux registres simulés (API v2) : la base est recopiée couche par
couche d'un registre anonyme, l'archive VDDK de l'exploitant devient la
couche du dessus telle quelle, la cible exige un jeton Bearer, et les blobs
redirigés vers un « CDN » qui refuse tout en-tête Authorization."""

import base64
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin" / "lib"))
import oci_push as op  # noqa: E402


def sha(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


class FakeRegistry:
    """Registre v2 en mémoire : Bearer optionnel (jeton contre Basic),
    blobs GET redirigés optionnels vers /cdn/ (refuse Authorization)."""

    def __init__(self, user=None, password=None, redirect_blobs=False, basic=False):
        self.blobs, self.manifests, self.uploads = {}, {}, []
        self.user, self.password, self.redirect = user, password, redirect_blobs
        self.blobs_cdn_broken = False
        # v2.6.0 : Range, envoi par morceaux (PATCH) et reprise, montage,
        # pannes injectées, journal des requêtes
        self.basic, self.range_ok, self.mount_ok, self.digest_header = basic, True, True, True
        self.corrupt, self.sessions, self.log = set(), {}, []
        self.fail_patch, self.patches, self.mounts = set(), 0, []
        self.sink, self.sunk = False, {}       # sink : envoi haché au fil de l'eau, rien gardé
        self.where = None                      # ensemble (dépôt, empreinte) : blobs rangés par dépôt
        # revue 2.6.0 : adresse d'envoi, royaume du jeton et cible de
        # redirection imposés (autre hôte, autre schéma)
        self.location_base, self.realm_url, self.redirect_to = "", None, None
        reg = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    view = memoryview(body)          # par tranches, sans copie
                    for i in range(0, len(view), 1 << 16):
                        self.wfile.write(view[i:i + (1 << 16)])

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_GET(self):
                self._route()

            def do_HEAD(self):
                self._route()

            def do_POST(self):
                self._route()

            def do_PUT(self):
                self._route()

            def do_PATCH(self):
                self._route()

            def _route(self):
                path, _, query = self.path.partition("?")
                reg.log.append((self.command, path, self.headers.get("Authorization"), self.headers.get("Range")))
                if path == "/token":
                    want = "Basic " + base64.b64encode(f"{reg.user}:{reg.password}".encode()).decode()
                    if self.headers.get("Authorization") != want:
                        return self._send(401, b"{}")
                    return self._send(200, json.dumps({"token": "good-token"}).encode())
                if path.startswith("/cdn/"):
                    if self.headers.get("Authorization"):
                        return self._send(400, b"only one auth mechanism allowed")
                    if reg.blobs_cdn_broken:
                        return self._send(404, b"not found")
                    return self._send(200, reg.blobs[path[5:]])
                if reg.user is not None and reg.basic:
                    want = "Basic " + base64.b64encode(f"{reg.user}:{reg.password}".encode()).decode()
                    if self.headers.get("Authorization") != want:
                        return self._send(401, b"{}", {"WWW-Authenticate": 'Basic realm="fake"'})
                elif reg.user is not None and self.headers.get("Authorization") != "Bearer good-token":
                    return self._send(401, b"{}", {"WWW-Authenticate":
                                                   f'Bearer realm="{reg.realm_url or reg.url + "/token"}",service="fake",'
                                                   'scope="repository:x:pull"'})
                if path == "/v2/":
                    return self._send(200, b"{}")
                m = re.match(r"^/v2/(.+?)/(blobs|manifests)/(uploads/)?(.*)$", path)
                if not m:
                    return self._send(404)
                repo, what, up, ref = m.groups()
                if what == "blobs" and up:
                    q = urllib.parse.parse_qs(query)
                    if self.command == "POST":
                        if "mount" in q:
                            reg.mounts.append((q["mount"][0], q.get("from", [""])[0]))
                            if reg.mount_ok and q["mount"][0] in reg.blobs:
                                if reg.where is not None:
                                    reg.where.add((repo, q["mount"][0]))
                                return self._send(201, headers={"Docker-Content-Digest": q["mount"][0]})
                        sid = uuid.uuid4().hex
                        reg.sessions[sid] = bytearray()
                        return self._send(202, headers={"Location": f"{reg.location_base}/v2/{repo}/blobs/uploads/{sid}"})
                    sid = ref
                    if sid not in reg.sessions:
                        return self._send(404, b"unknown upload")
                    buf = reg.sessions[sid]
                    if self.command == "GET":
                        return self._send(204, headers={"Range": f"0-{len(buf) - 1}",
                                                        "Location": f"/v2/{repo}/blobs/uploads/{sid}"})
                    if reg.sink and self.command in ("PATCH", "PUT"):
                        h = reg.sunk.setdefault(sid, [hashlib.sha256(), 0])
                        left = int(self.headers.get("Content-Length") or 0)
                        while left:
                            b = self.rfile.read(min(left, 1 << 16))
                            h[0].update(b)
                            h[1] += len(b)
                            left -= len(b)
                        if self.command == "PATCH":
                            reg.patches += 1
                            return self._send(202, headers={"Range": f"0-{h[1] - 1}",
                                                            "Location": f"/v2/{repo}/blobs/uploads/{sid}"})
                        digest = urllib.parse.parse_qs(query)["digest"][0]
                        if "sha256:" + h[0].hexdigest() != digest:
                            return self._send(400, b"digest mismatch")
                        reg.uploads.append(digest)
                        return self._send(201, headers={"Docker-Content-Digest": digest})
                    data = self._body()
                    if self.command == "PATCH":
                        reg.patches += 1
                        start = int((self.headers.get("Content-Range") or "0-0").split("-")[0])
                        if start != len(buf):
                            return self._send(416, b"out of order")
                        if reg.patches in reg.fail_patch:     # coupure : la moitié seulement
                            buf.extend(data[: len(data) // 2])
                            return self._send(500, b"connection lost")
                        buf.extend(data)
                        return self._send(202, headers={"Range": f"0-{len(buf) - 1}",
                                                        "Location": f"/v2/{repo}/blobs/uploads/{sid}"})
                    buf.extend(data)
                    digest = q["digest"][0]
                    if sha(bytes(buf)) != digest:
                        return self._send(400, b"digest mismatch")
                    reg.blobs[digest] = bytes(buf)
                    reg.uploads.append(digest)
                    if reg.where is not None:
                        reg.where.add((repo, digest))
                    del reg.sessions[sid]
                    return self._send(201, headers={"Docker-Content-Digest": digest})
                if what == "blobs":
                    if ref not in reg.blobs or (reg.where is not None and (repo, ref) not in reg.where):
                        return self._send(404)
                    if self.command == "GET" and reg.redirect:
                        return self._send(307, headers={"Location": reg.redirect_to or f"{reg.url}/cdn/{ref}"})
                    data = reg.blobs[ref]
                    if ref in reg.corrupt:
                        data = data[:-1] + bytes([data[-1] ^ 1])
                    rng = self.headers.get("Range")
                    if rng and reg.range_ok and self.command == "GET":
                        start = int(rng.split("=")[1].split("-")[0])
                        if start >= len(data):
                            return self._send(416)
                        return self._send(206, data[start:],
                                          {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
                    return self._send(200, data)
                if self.command == "PUT":
                    body = self._body()
                    mt = self.headers.get("Content-Type")
                    reg.manifests[(repo, ref)] = (body, mt)
                    reg.manifests[(repo, sha(body))] = (body, mt)
                    return self._send(201, headers={"Docker-Content-Digest": sha(body)})
                if (repo, ref) not in reg.manifests:
                    return self._send(404)
                body, mt = reg.manifests[(repo, ref)]
                hdr = {"Content-Type": mt}
                if reg.digest_header:
                    hdr["Docker-Content-Digest"] = sha(body)
                return self._send(200, body, hdr)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.host = f"127.0.0.1:{self.srv.server_address[1]}"
        self.url = f"http://{self.host}"
        threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def targz(files, links=()):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as t:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        for name, target in links:
            ti = tarfile.TarInfo(name)
            ti.type, ti.linkname = tarfile.SYMTYPE, target
            t.addfile(ti)
    return gzip.compress(raw.getvalue(), mtime=0), raw.getvalue()


@pytest.fixture
def base():
    """Une base « bci-busybox » : index multi-architecture, amd64 et arm64."""
    reg = FakeRegistry()
    images = {}
    for arch in ("amd64", "arm64"):
        layer, tar = targz({"usr/bin/cp": f"cp-{arch}".encode()})
        cfg = json.dumps({"architecture": arch, "os": "linux", "config": {"Cmd": ["/bin/sh"]},
                          "rootfs": {"type": "layers", "diff_ids": [sha(tar)]}, "history": []}).encode()
        reg.blobs[sha(layer)], reg.blobs[sha(cfg)] = layer, cfg
        man = json.dumps({"schemaVersion": 2, "mediaType": op.M_DOCKER,
                          "config": {"mediaType": op.CONFIG_TYPE[op.M_DOCKER], "digest": sha(cfg), "size": len(cfg)},
                          "layers": [{"mediaType": op.LAYER_TYPE[op.M_DOCKER], "digest": sha(layer), "size": len(layer)}]}).encode()
        reg.manifests[("bci/bci-busybox", sha(man))] = (man, op.M_DOCKER)
        images[arch] = (sha(man), sha(layer), sha(tar))
    idx = json.dumps({"schemaVersion": 2, "mediaType": op.M_DOCKER_LIST, "manifests": [
        {"mediaType": op.M_DOCKER, "digest": images[a][0], "size": 1, "platform": {"os": "linux", "architecture": a}}
        for a in ("arm64", "amd64")]}).encode()
    reg.manifests[("bci/bci-busybox", "16.0")] = (idx, op.M_DOCKER_LIST)
    reg.images = images
    yield reg
    reg.close()


@pytest.fixture
def vddk(tmp_path):
    data, tar = targz({"vmware-vix-disklib-distrib/lib64/libvixDiskLib.so.8": b"\x7fELF...",
                       "vmware-vix-disklib-distrib/doc/README": b"doc"},
                      links=[("vmware-vix-disklib-distrib/lib64/libvixDiskLib.so", "libvixDiskLib.so.8")])
    p = tmp_path / "VMware-vix-disklib-8.0.3.x86_64.tar.gz"
    p.write_bytes(data)
    return p, sha(data), sha(tar)


def test_the_vddk_archive_is_the_layer_as_is(vddk):
    p, digest, diff_id = vddk
    assert op.archive_layer(p) == {"digest": digest, "diff_id": diff_id, "size": p.stat().st_size}


@pytest.mark.parametrize("files,links,match", [
    ({"etc/passwd": b"x", "vmware-vix-disklib-distrib/lib64/libvixDiskLib.so.8": b"x"}, (), "unexpected entry"),
    ({"/vmware-vix-disklib-distrib/lib64/libvixDiskLib.so.8": b"x"}, (), "unexpected entry"),
    ({"vmware-vix-disklib-distrib/../x": b"x"}, (), "unexpected entry"),
    ({"vmware-vix-disklib-distrib/lib64/libvixDiskLib.so.8": b"x"},
     [("vmware-vix-disklib-distrib/lib64/evil", "../../../etc/shadow")], "link"),
    ({"vmware-vix-disklib-distrib/doc/README": b"x"}, (), "not a VDDK"),
])
def test_an_archive_that_is_not_a_clean_vddk_is_refused(tmp_path, files, links, match):
    data, _ = targz(files, links)
    p = tmp_path / "x.tar.gz"
    p.write_bytes(data)
    with pytest.raises(ValueError, match=match):
        op.archive_layer(p)


def test_references_are_read_like_docker_does():
    assert op.parse_ref("172.16.1.11:3000/jniedergang/vddk:8.0.3") == ("172.16.1.11:3000", "jniedergang/vddk", "8.0.3")
    assert op.parse_ref("registry.suse.com/bci/bci-busybox:16.0") == ("registry.suse.com", "bci/bci-busybox", "16.0")
    assert op.parse_ref("busybox") == ("registry-1.docker.io", "library/busybox", "latest")
    assert op.parse_ref("harbor.lan/p/vddk@sha256:" + "b" * 64)[2] == "sha256:" + "b" * 64
    with pytest.raises(ValueError):
        op.parse_ref("Harbor.lan/P/vddk:1")


def test_the_image_is_the_amd64_base_plus_the_vddk_layer(base, vddk):
    p, digest, diff_id = vddk
    target = FakeRegistry(user="ju", password="s3cret")
    try:
        res = op.push_vddk_image(p, f"{target.host}/ju/vddk:8.0.3", base=f"{base.host}/bci/bci-busybox:16.0",
                                 target_creds={"username": "ju", "password": "s3cret"},
                                 plain_http=True, base_plain_http=True, now=lambda: time.gmtime(0))
        body, mt = target.manifests[("ju/vddk", "8.0.3")]
        man = json.loads(body)
        assert mt == op.M_DOCKER and res["digest"] == sha(body)
        assert res["pinned"] == f"{target.host}/ju/vddk@{sha(body)}" and res["image"] == f"{target.host}/ju/vddk:8.0.3"
        amd_man, amd_layer, amd_tar = base.images["amd64"]
        assert [l["digest"] for l in man["layers"]] == [amd_layer, digest]
        assert man["layers"][1]["mediaType"] == op.LAYER_TYPE[op.M_DOCKER]
        cfg = json.loads(target.blobs[man["config"]["digest"]])
        assert cfg["rootfs"]["diff_ids"] == [amd_tar, diff_id]
        assert cfg["config"]["Entrypoint"] == ["cp", "-r", "/vmware-vix-disklib-distrib", "/opt"]
        assert cfg["config"]["Cmd"] is None and cfg["created"] == "1970-01-01T00:00:00Z"
        assert base.images["arm64"][1] not in target.blobs            # la seule base amd64
        assert target.blobs[digest] == p.read_bytes()                 # l'archive telle quelle
    finally:
        target.close()


def test_a_second_push_sends_no_blob_again(base, vddk):
    p, _, _ = vddk
    target = FakeRegistry()
    try:
        kw = dict(base=f"{base.host}/bci/bci-busybox:16.0", plain_http=True, base_plain_http=True,
                  now=lambda: time.gmtime(0))
        op.push_vddk_image(p, f"{target.host}/ju/vddk:8.0.3", **kw)
        first = len(target.uploads)
        op.push_vddk_image(p, f"{target.host}/ju/vddk:8.0.3", **kw)
        assert first == 3 and len(target.uploads) == first     # base, VDDK, configuration
    finally:
        target.close()


def test_redirected_blobs_are_fetched_without_the_credentials(vddk, base):
    # la base exige un jeton ET renvoie ses blobs vers un « CDN » qui refuse
    # tout en-tête Authorization : réussir prouve qu'il n'a pas suivi
    base.redirect, base.user, base.password = True, "reader", "pw"
    p, _, _ = vddk
    target = FakeRegistry()
    try:
        res = op.push_vddk_image(p, f"{target.host}/ju/vddk:1", base=f"{base.host}/bci/bci-busybox:16.0",
                                 base_creds={"username": "reader", "password": "pw"},
                                 plain_http=True, base_plain_http=True)
        assert base.images["amd64"][1] in target.blobs and res["digest"].startswith("sha256:")
    finally:
        target.close()


def test_wrong_credentials_are_said_without_the_password(base, vddk):
    p, _, _ = vddk
    target = FakeRegistry(user="ju", password="right")
    try:
        with pytest.raises(op.RegistryError, match="authentication refused") as e:
            op.push_vddk_image(p, f"{target.host}/ju/vddk:1", base=f"{base.host}/bci/bci-busybox:16.0",
                               target_creds={"username": "ju", "password": "wrong-pw"},
                               plain_http=True, base_plain_http=True)
        assert "wrong-pw" not in str(e.value)
    finally:
        target.close()


def test_a_digest_is_not_a_place_to_push_to(vddk):
    p, _, _ = vddk
    with pytest.raises(ValueError, match="tag"):
        op.push_vddk_image(p, "harbor.lan/p/vddk@sha256:" + "c" * 64)


def test_a_failing_redirect_target_is_a_registry_error(vddk, base):
    base.redirect = True
    base.blobs_cdn_broken = True          # the « CDN » answers 404
    target = FakeRegistry()
    try:
        with pytest.raises(op.RegistryError, match="HTTP 404"):
            op.push_vddk_image(vddk[0], f"{target.host}/ju/vddk:1", base=f"{base.host}/bci/bci-busybox:16.0",
                               plain_http=True, base_plain_http=True)
    finally:
        target.close()


def test_an_unreachable_registry_is_a_registry_error(vddk):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                  # nothing listens there any more
    with pytest.raises(op.RegistryError, match=f"127.0.0.1:{port}"):
        op.push_vddk_image(vddk[0], f"127.0.0.1:{port}/ju/vddk:1", base=f"127.0.0.1:{port}/bci/bci-busybox:16.0",
                           plain_http=True, base_plain_http=True)


# ---------------------------------------------------------------------------
# v2.6.0 : flux, reprise, envoi par morceaux, montage, CA propre au registre
# ---------------------------------------------------------------------------
import os  # noqa: E402
import shutil  # noqa: E402
import ssl  # noqa: E402
import subprocess  # noqa: E402
import tracemalloc  # noqa: E402


def payload(n, seed=b"layer"):
    """n octets déterministes et peu compressibles."""
    out, h = bytearray(), seed
    while len(out) < n:
        h = hashlib.sha256(h).digest()
        out.extend(h)
    return bytes(out[:n])


@pytest.fixture
def reg():
    r = FakeRegistry(user="ju", password="pw", basic=True)
    yield r
    r.close()


def client(r, **kw):
    return op.Registry(r.host, {"username": "ju", "password": "pw"}, plain_http=True, **kw)


def test_a_blob_is_streamed_to_disk_under_a_memory_ceiling(reg, tmp_path):
    data = payload(5 << 20)
    reg.blobs[sha(data)] = data
    out = tmp_path / "blob"
    c = client(reg)
    c.manifest_digest("x/y", "none")            # authentification faite avant la mesure
    tracemalloc.start()
    try:
        c.blob_to_file("x/y", sha(data), out)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert out.read_bytes() == data and not (tmp_path / "blob.part").exists()
    assert peak < (5 << 20) // 2, f"{peak} bytes held in memory for a 5 MiB blob"


def test_a_blob_is_sent_from_a_file_under_a_memory_ceiling(reg, tmp_path):
    data = payload(5 << 20, b"up")
    src = tmp_path / "src"
    src.write_bytes(data)
    reg.sink = True
    c = client(reg)
    c.manifest_digest("x/y", "none")
    for chunk in (1 << 20, 64 << 20):              # par morceaux, puis d'un seul PUT
        reg.uploads.clear()
        tracemalloc.start()
        try:
            assert c.push_blob_file("x/y", sha(data), src, len(data), chunk=chunk) is True
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert reg.uploads == [sha(data)]
        assert peak < (5 << 20) // 2, f"{peak} bytes held in memory (chunk {chunk})"


@pytest.mark.parametrize("case", ["corrupt", "corrupt-after-resume"])
def test_a_wrong_digest_removes_the_partial_file(reg, tmp_path, case):
    data = payload(300_000)
    d = sha(data)
    reg.blobs[d] = data
    reg.corrupt.add(d)
    out = tmp_path / "blob"
    if case == "corrupt-after-resume":
        (tmp_path / "blob.part").write_bytes(data[:1000])
    with pytest.raises(op.RegistryError) as e:
        client(reg).blob_to_file("x/y", d, out)
    assert e.value.code == "digest-mismatch"
    assert not out.exists() and not (tmp_path / "blob.part").exists()


@pytest.mark.parametrize("range_ok,fetched", [(True, 200_000), (False, 300_000)])
def test_an_interrupted_copy_is_resumed_where_it_stopped(reg, tmp_path, range_ok, fetched):
    data = payload(300_000)
    d = sha(data)
    reg.blobs[d], reg.range_ok = data, range_ok
    (tmp_path / "blob.part").write_bytes(data[:100_000])
    got = []
    client(reg).blob_to_file("x/y", d, tmp_path / "blob", on_bytes=got.append)
    assert (tmp_path / "blob").read_bytes() == data
    assert sum(got) == fetched                       # sans Range : tout est relu
    assert ("GET", f"/v2/x/y/blobs/{d}", reg.log[-1][2], "bytes=100000-") == reg.log[-1]


def test_a_complete_partial_file_is_kept(reg, tmp_path):
    data = payload(4096)
    reg.blobs[sha(data)] = data
    (tmp_path / "blob.part").write_bytes(data)
    client(reg).blob_to_file("x/y", sha(data), tmp_path / "blob")      # 416 du registre
    assert (tmp_path / "blob").read_bytes() == data


def test_a_redirected_streamed_blob_goes_without_the_credentials(tmp_path):
    r = FakeRegistry(user="reader", password="pw", redirect_blobs=True)
    try:
        data = payload(70_000)
        r.blobs[sha(data)] = data
        c = op.Registry(r.host, {"username": "reader", "password": "pw"}, plain_http=True)
        c.blob_to_file("x/y", sha(data), tmp_path / "b")
        assert (tmp_path / "b").read_bytes() == data
        cdn = [entry for entry in r.log if entry[1].startswith("/cdn/")]
        assert cdn and all(entry[2] is None for entry in cdn)
    finally:
        r.close()


def test_a_digest_that_is_not_one_is_refused_before_any_request(reg, tmp_path):
    with pytest.raises(op.RegistryError, match="not a sha256 digest"):
        client(reg).blob_to_file("x/y", "sha256:../../etc", tmp_path / "b")
    assert reg.log == []


@pytest.mark.parametrize("size,chunk,fail,patches", [
    (100_000, 1 << 20, set(), 0),                 # petit : un seul PUT
    (300_000, 100_000, set(), 3),                 # par morceaux
    (300_000, 100_000, {2}, 4),                   # coupure au 2e morceau : reprise
])
def test_uploads_go_whole_or_by_resumable_chunks(reg, tmp_path, size, chunk, fail, patches):
    data = payload(size, b"chunks")
    src = tmp_path / "src"
    src.write_bytes(data)
    reg.fail_patch = fail
    sent = []
    assert client(reg).push_blob_file("x/y", sha(data), src, size, chunk=chunk, on_bytes=sent.append) is True
    assert reg.blobs[sha(data)] == data and reg.patches == patches
    assert sum(sent) >= size


def test_an_upload_that_keeps_failing_gives_up(reg, tmp_path):
    data = payload(300_000, b"fail")
    src = tmp_path / "src"
    src.write_bytes(data)
    reg.fail_patch = set(range(1, 50))
    with pytest.raises(op.RegistryError, match="HTTP 500"):
        client(reg).push_blob_file("x/y", sha(data), src, len(data), chunk=100_000)
    assert sha(data) not in reg.blobs


@pytest.mark.parametrize("where,mount_from,mount_ok,result,sent", [
    ({("x/y", 1)}, None, True, False, False),          # déjà dans ce dépôt : rien
    ({("a/b", 1)}, "a/b", True, "mounted", False),     # monté depuis un autre dépôt
    ({("a/b", 1)}, "a/b", False, True, True),          # montage refusé (202) : envoyé
    ({("a/b", 1)}, None, True, True, True),            # pas de source de montage
    (set(), "x/y", True, True, True),                  # source = cible : pas de montage tenté
])
def test_a_blob_is_skipped_mounted_or_sent(reg, tmp_path, where, mount_from, mount_ok, result, sent):
    data = payload(5000, b"m")
    src = tmp_path / "src"
    src.write_bytes(data)
    if where:
        reg.blobs[sha(data)] = data
    reg.where = {(repo, sha(data)) for repo, _ in where}
    reg.mount_ok = mount_ok
    assert client(reg).push_blob_file("x/y", sha(data), src, len(data), mount_from=mount_from) == result
    assert (sha(data) in reg.uploads) == sent
    assert ("x/y", sha(data)) in reg.where
    assert reg.mounts == ([(sha(data), mount_from)] if mount_from and mount_from != "x/y" and result != False else [])


@pytest.mark.parametrize("header,known", [(True, True), (False, True), (True, False)])
def test_a_manifest_digest_is_read_by_head(reg, header, known):
    body = b'{"schemaVersion":2}'
    if known:
        reg.manifests[("x/y", "1.0")] = (body, op.M_OCI)
    reg.digest_header = header
    assert client(reg).manifest_digest("x/y", "1.0") == (sha(body) if known else None)


@pytest.fixture(scope="module")
def tls_pki(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl needed to make a test CA")
    d = tmp_path_factory.mktemp("pki")

    def run(*a):
        subprocess.run(["openssl", *a], cwd=d, check=True, capture_output=True)
    run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "ca.key",
        "-out", "ca.crt", "-days", "2", "-subj", "/CN=test CA", "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash")
    run("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "tls.key",
        "-out", "tls.csr", "-subj", "/CN=127.0.0.1")
    (d / "ext").write_text("subjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n"
                           "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
                           "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
    run("x509", "-req", "-in", "tls.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
        "-out", "tls.crt", "-days", "2", "-extfile", "ext")
    return d


@pytest.fixture
def tls_reg(tls_pki):
    r = FakeRegistry()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tls_pki / "tls.crt", tls_pki / "tls.key")
    r.srv.socket = ctx.wrap_socket(r.srv.socket, server_side=True)
    yield r
    r.close()


@pytest.mark.parametrize("ca,ok", [("own", True), (None, False), ("garbage", None)])
def test_a_registry_ca_of_its_own_is_trusted_only_when_given(tls_pki, tls_reg, ca, ok):
    body = b'{"schemaVersion":2}'
    tls_reg.manifests[("x/y", "1")] = (body, op.M_OCI)
    pem = {"own": (tls_pki / "ca.crt").read_text(), None: None, "garbage": "-----BEGIN CERTIFICATE-----\nzz\n"}[ca]
    if ok is None:
        with pytest.raises(op.RegistryError, match="unreadable registry CA"):
            op.Registry(tls_reg.host, ca_pem=pem)
        return
    c = op.Registry(tls_reg.host, ca_pem=pem)
    if ok:
        assert c.manifest_digest("x/y", "1") == sha(body)
    else:
        with pytest.raises(op.RegistryError, match="CERTIFICATE_VERIFY_FAILED|certificate verify failed"):
            c.manifest_digest("x/y", "1")


# ---------------------------------------------------------------------------
# Revue 2.6.0 (I1, I2) : identifiants jamais hors du registre, schémas
# ---------------------------------------------------------------------------
class Spy:
    """Second serveur : note chaque requête (méthode, chemin, Authorization)
    et répond `answer` (201 par défaut, ou 401 avec un défi Bearer qui
    désigne son propre royaume)."""

    def __init__(self, answer=201):
        self.seen, self.answer = [], answer
        spy = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                spy.seen.append((self.command, self.path.partition("?")[0], self.headers.get("Authorization")))
                if spy.answer == 401 and not self.path.startswith("/token"):
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", f'Bearer realm="{spy.url}/token",service="spy"')
                else:
                    self.send_response(201 if spy.answer != 401 else 200)
                    self.send_header("Docker-Content-Digest", "sha256:" + "0" * 64)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            do_GET = do_PUT = do_POST = do_PATCH = do_HEAD = _any

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def spy():
    s = Spy()
    yield s
    s.close()


@pytest.mark.parametrize("bearer", [False, True])
def test_an_upload_location_on_another_host_gets_no_credentials(spy, tmp_path, bearer):
    r = FakeRegistry(user="ju", password="pw", basic=not bearer)
    try:
        r.location_base = spy.url
        data = payload(5000)
        f = tmp_path / "b"
        f.write_bytes(data)
        client(r).push_blob_file("x/y", sha(data), f, len(data))
        assert [m for m, _, _ in spy.seen] == ["PUT"]
        assert all(a is None for _, _, a in spy.seen)
    finally:
        r.close()


def test_a_401_from_another_host_fetches_no_token_and_sends_nothing(tmp_path):
    s = Spy(answer=401)
    r = FakeRegistry(user="ju", password="pw", basic=True)
    try:
        r.location_base = s.url
        data = payload(3000)
        f = tmp_path / "b"
        f.write_bytes(data)
        with pytest.raises(op.RegistryError, match="authentication"):
            client(r).push_blob_file("x/y", sha(data), f, len(data))
        assert s.seen and not [p for _, p, _ in s.seen if p.startswith("/token")]
        assert all(a is None for _, _, a in s.seen)
    finally:
        s.close()
        r.close()


@pytest.mark.parametrize("what,target", [
    ("realm", "file:///etc/hostname"),
    ("realm", "ftp://127.0.0.1/token"),
    ("redirect", "file:///etc/hostname"),
    ("redirect", "data:text/plain,abc"),
])
def test_only_http_schemes_are_followed(tmp_path, what, target):
    r = FakeRegistry(user=None if what == "redirect" else "ju", password="pw", redirect_blobs=(what == "redirect"))
    try:
        data = b"abc"
        r.blobs[sha(data)] = data
        if what == "realm":
            r.realm_url = target
        else:
            r.redirect_to = target
        c = op.Registry(r.host, {"username": "ju", "password": "pw"}, plain_http=True)
        with pytest.raises(op.RegistryError, match="scheme"):
            c.blob_to_file("x/y", sha(data), tmp_path / "out")
        assert not (tmp_path / "out").exists() and not (tmp_path / "out.part").exists()
    finally:
        r.close()


def test_an_http_realm_is_refused_for_an_https_registry(tls_pki, tls_reg):
    tls_reg.user, tls_reg.password = "ju", "pw"
    tls_reg.realm_url = "http://127.0.0.1:9/token"
    tls_reg.manifests[("x/y", "1")] = (b"{}", op.M_OCI)
    c = op.Registry(tls_reg.host, {"username": "ju", "password": "pw"}, ca_pem=(tls_pki / "ca.crt").read_text())
    with pytest.raises(op.RegistryError, match="scheme"):
        c.manifest_digest("x/y", "1")


def test_the_opener_has_no_file_ftp_or_data_handler():
    c = op.Registry("127.0.0.1:1", plain_http=True)
    kinds = {type(h).__name__ for h in c.opener.handlers}
    assert not kinds & {"FileHandler", "FTPHandler", "CacheFTPHandler", "DataHandler"}
