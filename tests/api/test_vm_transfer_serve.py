"""v1.45.0 : le guichet qui sert les disques à CDI pendant un import.

Relevé sur harv1 : l'importeur de CDI fait un HEAD puis un seul GET, sans
requête Range. Le guichet sert donc en flux, par jeton, et rien d'autre :
pas de listing, pas d'autre chemin, un jeton révoqué ne sert plus.
"""

import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import vm_transfer_serve as vs  # noqa: E402


@pytest.fixture
def server():
    s = vs.DiskServer(bind="127.0.0.1", port=0)
    port = s.start()
    yield s, f"http://127.0.0.1:{port}"
    s.stop()


def opener_of(data, size=65536):
    def opener():
        for i in range(0, len(data), size):
            yield data[i:i + size]
    return opener


def req(url, method="GET"):
    r = urllib.request.Request(url, method=method)
    return urllib.request.urlopen(r, timeout=5)


def status_of(url, method="GET"):
    try:
        with req(url, method) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_get_streams_the_published_content(server):
    s, base = server
    data = b"x" * 300_000 + b"end"
    path = s.publish(opener_of(data), len(data))
    assert path.endswith(".raw.gz") and len(path) > 30
    with req(base + path) as r:
        assert r.read() == data
        assert r.headers["Content-Length"] == str(len(data))


def test_head_answers_without_a_body_and_is_not_a_hit(server):
    s, base = server
    path = s.publish(opener_of(b"abc"), 3)
    with req(base + path, "HEAD") as r:
        assert r.status == 200
        assert r.headers["Content-Length"] == "3"
        assert r.read() == b""
    assert s.hits(path) == 0
    with req(base + path) as r:
        r.read()
    assert s.hits(path) == 1


def test_unknown_size_is_streamed_until_the_end(server):
    s, base = server
    path = s.publish(opener_of(b"a" * 10_000), None)
    with req(base + path) as r:
        assert r.read() == b"a" * 10_000


@pytest.mark.parametrize("suffix", ["/", "/index.html", "/nope.raw.gz", "/../etc/passwd"])
def test_nothing_else_is_served(server, suffix):
    s, base = server
    s.publish(opener_of(b"abc"), 3)
    assert status_of(base + suffix) == 404


def test_a_revoked_token_is_gone(server):
    s, base = server
    path = s.publish(opener_of(b"abc"), 3)
    s.revoke(path)
    assert status_of(base + path) == 404


def test_other_methods_are_refused(server):
    s, base = server
    path = s.publish(opener_of(b"abc"), 3)
    assert status_of(base + path, "POST") == 405
    assert status_of(base + path, "DELETE") == 405


def test_two_disks_at_once(server):
    s, base = server
    a = s.publish(opener_of(b"A" * 200_000), 200_000)
    b = s.publish(opener_of(b"B" * 200_000), 200_000)
    out = {}

    def fetch(p):
        with req(base + p) as r:
            out[p] = r.read()

    ts = [threading.Thread(target=fetch, args=(p,)) for p in (a, b)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert out[a] == b"A" * 200_000 and out[b] == b"B" * 200_000


def test_an_opener_error_is_recorded(server):
    s, base = server

    def broken():
        yield b"abc"
        raise OSError("source vanished")

    path = s.publish(broken, None)
    try:
        with req(base + path) as r:
            r.read()
    except Exception:
        pass
    assert "source vanished" in (s.error(path) or "")
