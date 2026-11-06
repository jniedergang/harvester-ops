"""2.6.0, revue I-1 : le dépôt Helm HTTP de `harvester-artefacts pack`.

Les redirections sont suivies à la main : `Authorization` n'atteint jamais
un autre hôte, http n'est permis que pour un hôte de `--plain-http`, et des
identifiants ne partent jamais en clair. Deux faux serveurs : le dépôt
(Basic) et un autre hôte qui note les en-têtes reçus."""

import base64
import hashlib
import importlib.util
import io
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SECRET = "Helm-Pw-1618"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("harvester_artefacts_fetch", ROOT / "bin" / "harvester-artefacts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def chart():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"apiVersion: v2\nname: podinfo\nversion: 6.9.2\n"
        info = tarfile.TarInfo("podinfo/Chart.yaml")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Server:
    def __init__(self, routes, user=None):
        self.routes, self.seen = routes, []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                srv.seen.append((self.path, self.headers.get("Authorization")))
                if user and self.headers.get("Authorization") != "Basic " + base64.b64encode(
                        f"{user}:{SECRET}".encode()).decode():
                    self.send_response(401)
                    self.end_headers()
                    return
                r = srv.routes.get(self.path)
                if isinstance(r, tuple):                 # (code, Location)
                    self.send_response(r[0])
                    self.send_header("Location", r[1])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200 if r is not None else 404)
                self.send_header("Content-Length", str(len(r or b"")))
                self.end_headers()
                self.wfile.write(r or b"")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def index(url):
    data = chart()
    return (f"apiVersion: v1\nentries:\n  podinfo:\n  - name: podinfo\n    version: 6.9.2\n"
            f"    digest: {hashlib.sha256(data).hexdigest()}\n    urls:\n    - {url}\n").encode(), data


@pytest.fixture
def servers():
    other = Server({})
    repo = Server({}, user="ju")
    yield repo, other
    repo.close()
    other.close()


def sources(tool, repo, plain):
    return tool.Sources({repo.host: {"username": "ju", "password": SECRET}}, plain)


def test_a_redirect_to_another_host_never_carries_the_credentials(tool, servers, tmp_path):
    repo, other = servers
    body, data = index(f"http://{other.host}/podinfo-6.9.2.tgz")
    other.routes.update({"/index.yaml": body, "/podinfo-6.9.2.tgz": data})
    repo.routes["/index.yaml"] = (302, f"http://{other.host}/index.yaml")
    src = sources(tool, repo, [repo.host, other.host])
    tool.fetch_chart(src, {"repo": f"http://{repo.host}", "name": "podinfo", "version": "6.9.2"}, tmp_path / "c.tgz")
    assert (tmp_path / "c.tgz").read_bytes() == data
    assert repo.seen and repo.seen[0][1]                     # le dépôt lui-même reçoit ses identifiants
    assert [a for _, a in other.seen] == [None, None], other.seen


@pytest.mark.parametrize("plain,ok", [((), False), (("repo",), True)])
def test_credentials_never_go_in_clear_without_plain_http(tool, servers, tmp_path, plain, ok):
    repo, _ = servers
    body, data = index("podinfo-6.9.2.tgz")
    repo.routes.update({"/index.yaml": body, "/podinfo-6.9.2.tgz": data})
    src = sources(tool, repo, [repo.host] if plain else [])
    c = {"repo": f"http://{repo.host}", "name": "podinfo", "version": "6.9.2"}
    if ok:
        tool.fetch_chart(src, c, tmp_path / "c.tgz")
    else:
        with pytest.raises(ValueError, match="never sent in clear"):
            tool.fetch_chart(src, c, tmp_path / "c.tgz")
        assert repo.seen == []                              # aucune requête


@pytest.mark.parametrize("location", ["file:///etc/passwd", "ftp://x/y", "http://{other}/index.yaml"])
def test_a_redirect_to_a_forbidden_scheme_is_refused(tool, servers, location):
    repo, other = servers                  # `other` redirige, sans identifiants ; `repo` n'est pas en clair
    other.routes["/index.yaml"] = (302, location.format(other=repo.host))
    with pytest.raises(ValueError, match="https:// only"):
        tool._fetch(f"http://{other.host}/index.yaml", None, None, 1 << 20, plain=[other.host])
    assert repo.seen == []


def test_redirections_are_bounded(tool, servers):
    _, other = servers
    other.routes["/loop"] = (302, "/loop")
    with pytest.raises(ValueError, match="redirections"):
        tool._fetch(f"http://{other.host}/loop", None, None, 1 << 20, plain=[other.host])
