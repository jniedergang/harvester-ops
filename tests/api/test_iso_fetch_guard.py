"""v1.99.0 : /api/iso/fetch sans SSRF.

La route téléchargeait n'importe quelle URL avec urllib : redirections
suivies sans contrôle, aucune adresse refusée. Depuis la console, une URL
http://127.0.0.1:8200/ atteignait Vault, et une redirection ou un nom DNS
pointant vers une adresse interne faisait de même. Ces tests rejouent ces
attaques sans réseau : résolveur et connexion sont injectés.
"""

import inspect
import ipaddress
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402
import url_fetch_guard as ufg  # noqa: E402

PUBLIC = "93.184.216.34"


# ---------------------------------------------------------------------------
# Adresses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip,allow,expected", [
    ("127.0.0.1", [], "loopback"),
    ("127.8.9.10", [], "loopback"),
    ("::1", [], "loopback"),
    ("::ffff:127.0.0.1", [], "loopback"),
    ("169.254.169.254", [], "metadata"),
    ("fd00:ec2::254", [], "metadata"),
    ("169.254.10.1", [], "link-local"),
    ("fe80::1", [], "link-local"),
    ("10.1.2.3", [], "private"),
    ("172.16.1.11", [], "private"),
    ("192.168.0.1", [], "private"),
    ("fd12:3456::1", [], "private"),
    ("100.64.0.1", [], "private"),
    ("224.0.0.1", [], "multicast"),
    ("ff02::1", [], "multicast"),
    ("0.0.0.0", [], "unspecified"),
    ("::", [], "unspecified"),
    ("240.0.0.1", [], "refused"),
    ("192.0.2.1", [], "refused"),
    (PUBLIC, [], None),
    ("2606:2800:220:1:248:1893:25c8:1946", [], None),
    # miroir interne autorisé par l'opérateur
    ("172.16.1.11", ["172.16.0.0/16"], None),
    ("127.0.0.1", ["127.0.0.0/8"], None),
    ("fd12:3456::1", ["fd12::/16"], None),
    # une autorisation ne couvre que son réseau
    ("10.1.2.3", ["172.16.0.0/16"], "private"),
    # les métadonnées restent refusées même dans une plage autorisée
    ("169.254.169.254", ["169.254.0.0/16"], "metadata"),
    ("fd00:ec2::254", ["fd00::/8"], "metadata"),
    ("224.0.0.1", ["224.0.0.0/4"], "multicast"),
])
def test_address_refusal(ip, allow, expected):
    nets = [ipaddress.ip_network(n) for n in allow]
    got = ufg.address_refusal(ip, nets)
    if expected == "refused":
        # raison selon la version de Python (réservée ou privée), refus garanti
        assert got is not None
    else:
        assert got == expected


@pytest.mark.parametrize("cfg,expected", [
    ({}, []),
    ({"iso_fetch": {}}, []),
    ({"iso_fetch": {"allow_private": ["10.0.0.0/8", "fd00::/8"]}}, ["10.0.0.0/8", "fd00::/8"]),
    ({"iso_fetch": {"allow_private": "172.16.0.0/16"}}, ["172.16.0.0/16"]),
    # une entrée invalide est ignorée, elle n'ouvre rien
    ({"iso_fetch": {"allow_private": ["nope", "10.0.0.1/8"]}}, ["10.0.0.0/8"]),
])
def test_parse_allowlist(cfg, expected):
    assert [str(n) for n in ufg.parse_allowlist(cfg)] == expected


@pytest.mark.parametrize("cfg,expected", [
    ({}, (ufg.DEFAULT_MAX_BYTES, ufg.DEFAULT_MIN_FREE_BYTES)),
    ({"iso_fetch": {"max_bytes": 1000, "min_free_bytes": 10}}, (1000, 10)),
    ({"iso_fetch": {"max_bytes": "x", "min_free_bytes": -1}},
     (ufg.DEFAULT_MAX_BYTES, ufg.DEFAULT_MIN_FREE_BYTES)),
])
def test_limits(cfg, expected):
    assert ufg.limits(cfg) == expected


@pytest.mark.parametrize("url,reason", [
    ("ftp://mirror/x.iso", "scheme"),
    ("file:///etc/passwd", "scheme"),
    ("gopher://127.0.0.1:8200/", "scheme"),
    ("http://user:pw@mirror/x.iso", "credentials"),
    ("http:///x.iso", "bad-url"),
    ("http://mirror:99999/x.iso", "bad-url"),
])
def test_check_url_refuses(url, reason):
    with pytest.raises(ufg.FetchRefused) as e:
        ufg.check_url(url)
    assert e.value.reason == reason


# ---------------------------------------------------------------------------
# Ouverture : résolveur et connexions simulés
# ---------------------------------------------------------------------------

def _resolver(table):
    def resolve(host, port, *a, **k):
        if host not in table:
            raise ufg.socket.gaierror("no such host")
        return [(2, 1, 6, "", (ip, port)) for ip in table[host]]
    return resolve


class _Resp:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self._headers = headers or {}
        self._body = body

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, n=-1):
        out, self._body = (self._body, b"") if n < 0 else (self._body[:n], self._body[n:])
        return out


class _Web:
    """Serveurs simulés : (scheme, host, chemin) -> réponse. Note chaque
    connexion ouverte (avec l'adresse vérifiée)."""

    def __init__(self, routes):
        self.routes = routes
        self.opened = []

    def connector(self, scheme, host, ip, port, timeout):
        web = self

        class Conn:
            def request(self, method, path, headers=None):
                web.opened.append((scheme, host, ip, port, path))
                self.resp = web.routes[(scheme, host, path)]

            def getresponse(self):
                return self.resp

            def close(self):
                pass
        return Conn()


DNS = {"mirror.example": [PUBLIC], "evil.example": ["10.0.0.5"],
       "mixed.example": [PUBLIC, "127.0.0.1"], "vault.example": ["127.0.0.1"]}


def _open(url, web, allow=()):
    return ufg.open_checked(url, list(allow), resolver=_resolver(DNS), connector=web.connector)


@pytest.mark.parametrize("url,reason", [
    # Vault de la console, directement
    ("http://127.0.0.1:8200/v1/secret/data/x", "address"),
    ("http://[::1]:8200/", "address"),
    ("http://169.254.169.254/latest/meta-data/", "address"),
    # nom qui résout vers une adresse interne (rebinding)
    ("https://evil.example/x.iso", "address"),
    ("https://vault.example:8200/", "address"),
    # une adresse interne parmi d'autres suffit à refuser
    ("https://mixed.example/x.iso", "address"),
    ("https://unknown.example/x.iso", "dns"),
])
def test_internal_targets_are_refused_before_any_connection(url, reason):
    web = _Web({})
    with pytest.raises(ufg.FetchRefused) as e:
        _open(url, web)
    assert e.value.reason == reason
    assert web.opened == []


@pytest.mark.parametrize("start,location,reason", [
    # Vault et métadonnées par une redirection
    ("http", "http://127.0.0.1:8200/v1/sys/health", "address"),
    ("http", "http://169.254.169.254/latest/", "address"),
    ("https", "https://127.0.0.1:8200/v1/sys/health", "address"),
    ("https", "https://evil.example/x.iso", "address"),
    # changement de schéma
    ("https", "file:///etc/passwd", "scheme"),
    ("http", "ftp://mirror.example/x.iso", "scheme"),
    ("https", "http://mirror.example/x.iso", "downgrade"),
])
def test_every_redirect_hop_is_checked_again(start, location, reason):
    web = _Web({(start, "mirror.example", "/x.iso"): _Resp(302, {"Location": location})})
    with pytest.raises(ufg.FetchRefused) as e:
        _open(f"{start}://mirror.example/x.iso", web)
    assert e.value.reason == reason
    # seule la première connexion, publique, a été ouverte
    assert [o[1] for o in web.opened] == ["mirror.example"]


def _chain(n, last=_Resp(200, {"Content-Length": "2"}, b"ok")):
    routes = {("https", "mirror.example", f"/{i}"): _Resp(302, {"Location": f"/{i + 1}"})
              for i in range(n)}
    routes[("https", "mirror.example", f"/{n}")] = last
    return _Web(routes)


def test_five_redirects_are_followed():
    web = _chain(5)
    resp, _conn, final = _open("https://mirror.example/0", web)
    assert resp.status == 200 and final == "https://mirror.example/5"
    assert len(web.opened) == 6


def test_a_sixth_redirect_is_refused():
    web = _chain(6)
    with pytest.raises(ufg.FetchRefused) as e:
        _open("https://mirror.example/0", web)
    assert e.value.reason == "redirect"


def test_the_connection_goes_to_the_checked_address():
    """Pas de seconde résolution : la connexion part vers l'adresse vérifiée,
    l'en-tête Host et le certificat gardent le nom."""
    web = _Web({("https", "mirror.example", "/x.iso"): _Resp(200, {}, b"")})
    _open("https://mirror.example/x.iso", web)
    assert web.opened == [("https", "mirror.example", PUBLIC, 443, "/x.iso")]


def test_an_allowed_internal_mirror_is_reached():
    web = _Web({("http", "evil.example", "/x.iso"): _Resp(200, {}, b"")})
    allow = [ipaddress.ip_network("10.0.0.0/8")]
    resp, _c, _f = _open("http://evil.example/x.iso", web, allow)
    assert resp.status == 200


def test_an_http_error_is_named_without_detail():
    web = _Web({("https", "mirror.example", "/x.iso"): _Resp(404)})
    with pytest.raises(ufg.FetchRefused) as e:
        _open("https://mirror.example/x.iso", web)
    assert e.value.message == "the server answered HTTP 404"


def test_tls_verification_stays_on():
    conn = ufg.default_connector("https", "mirror.example", PUBLIC, 443, 5)
    assert conn._context.verify_mode == ufg.ssl.CERT_REQUIRED
    assert conn._context.check_hostname is True
    assert conn.host == "mirror.example" and conn._ip == PUBLIC


# ---------------------------------------------------------------------------
# Téléchargement : taille, espace libre, messages
# ---------------------------------------------------------------------------

def _run():
    return wapp.ActionRun("isofetch0001", "iso-fetch:x.iso", "(local)", [])


def _serve(monkeypatch, resp):
    class Conn:
        closed = False

        def close(self):
            Conn.closed = True
    monkeypatch.setattr(wapp._ufg, "open_checked", lambda *a, **k: (resp, Conn(), a[0]))
    return Conn


def _steps(run):
    return [e for e in run.events if e.get("type") == "step"]


@pytest.mark.parametrize("headers,body,max_bytes,free,reason_text", [
    # annoncé trop grand : refusé avant d'écrire
    ({"Content-Length": "5000"}, b"", 1000, 10 ** 12, "larger than"),
    # non annoncé, mais le flux dépasse le plafond
    ({}, b"x" * 3000, 1000, 10 ** 12, "larger than"),
    # annoncé plus petit qu'envoyé
    ({"Content-Length": "10"}, b"x" * 3000, 1000, 10 ** 12, "larger than"),
    # pas la place pour le fichier annoncé
    ({"Content-Length": "500"}, b"x" * 500, 10 ** 9, 600, "free space"),
    # pas la place pendant le téléchargement (taille non annoncée)
    ({}, b"x" * 500, 10 ** 9, 600, "free space"),
    # coupé avant la taille annoncée
    ({"Content-Length": "900"}, b"x" * 10, 10 ** 9, 10 ** 12, "ended before"),
])
def test_download_limits(tmp_path, monkeypatch, headers, body, max_bytes, free, reason_text):
    conn = _serve(monkeypatch, _Resp(200, headers, body))
    monkeypatch.setattr(wapp, "_iso_free_bytes", lambda p: free)
    run = _run()
    dest = tmp_path / "x.iso"
    wapp._iso_fetch_runner(run, "https://mirror.example/x.iso", dest, [], max_bytes, 1000)
    assert run.status == "error" and run.exit_code == 1
    assert reason_text in run.error_summary
    assert not dest.exists() and list(tmp_path.iterdir()) == []
    assert conn.closed


def test_a_download_within_limits_lands_with_its_sha256(tmp_path, monkeypatch):
    _serve(monkeypatch, _Resp(200, {"Content-Length": "4"}, b"data"))
    monkeypatch.setattr(wapp, "_iso_free_bytes", lambda p: 10 ** 12)
    run = _run()
    dest = tmp_path / "x.iso"
    wapp._iso_fetch_runner(run, "https://mirror.example/x.iso", dest, [], 10 ** 9, 1000)
    assert run.status == "done" and dest.read_bytes() == b"data"
    assert (tmp_path / "x.iso.sha256").read_text().startswith(
        "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7")


def test_an_internal_error_never_shows_a_path(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError(f"[Errno 13] Permission denied: '{tmp_path}/secret/x.iso.downloading'")
    monkeypatch.setattr(wapp._ufg, "open_checked", boom)
    run = _run()
    wapp._iso_fetch_runner(run, "https://mirror.example/x.iso", tmp_path / "x.iso", [], 10, 1)
    assert run.error_summary == "download failed (OSError)"
    assert all(str(tmp_path) not in (s.get("message") or "") for s in _steps(run))


def test_the_runner_really_refuses_vault(tmp_path):
    """Sans simulation de la garde : le vrai open_checked refuse Vault avant
    toute connexion."""
    run = _run()
    wapp._iso_fetch_runner(run, "http://127.0.0.1:8200/v1/sys/health", tmp_path / "x.iso")
    assert run.status == "error"
    assert "loopback" in run.error_summary


# ---------------------------------------------------------------------------
# La route
# ---------------------------------------------------------------------------

@pytest.fixture
def route(monkeypatch, tmp_path):
    cfg = {}
    started = []
    monkeypatch.setattr(wapp, "load_config_ro", lambda: cfg)
    monkeypatch.setattr(wapp, "ISO_DIR", tmp_path / "iso")
    monkeypatch.setattr(wapp, "_iso_free_bytes", lambda p: 10 ** 12)
    monkeypatch.setattr(wapp, "track_action",
                        lambda label, cl, worker, *args: started.append((label, args)) or "a1")
    monkeypatch.setattr(wapp._ufg.socket, "getaddrinfo", _resolver(DNS))

    def post(url, **extra):
        with wapp.app.test_client() as c:
            return c.post("/api/iso/fetch", json={"url": url, **extra})
    return post, cfg, started


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8200/v1/secret/data/vault.iso",
    "http://vault.example:8200/x.iso",
    "https://evil.example/x.iso",
    "http://169.254.169.254/latest/x.iso",
    "http://u:p@mirror.example/x.iso",
])
def test_the_route_refuses_internal_targets(route, url):
    post, _cfg, started = route
    r = post(url)
    assert r.status_code == 400
    assert started == []


def test_the_route_reads_the_allowlist_at_each_request(route):
    post, cfg, started = route
    assert post("https://evil.example/x.iso").status_code == 400
    cfg["iso_fetch"] = {"allow_private": ["10.0.0.0/8"], "max_bytes": 123}
    r = post("https://evil.example/x.iso")
    assert r.status_code == 202
    label, args = started[0]
    assert label == "iso-fetch:x.iso"
    _url, _dest, allow, max_bytes, _min_free = args
    assert [str(n) for n in allow] == ["10.0.0.0/8"] and max_bytes == 123


def test_the_route_refuses_when_the_store_is_full(route, monkeypatch):
    post, _cfg, started = route
    monkeypatch.setattr(wapp, "_iso_free_bytes", lambda p: 10)
    r = post("https://mirror.example/x.iso")
    assert r.status_code == 507 and started == []


def test_the_route_needs_a_privileged_role_and_a_valid_rate_limit():
    # /api/iso/ est réservé aux admins : au moins le rôle opérateur
    need = wapp.required_role_for("/api/iso/fetch", "POST")
    assert wapp.ROLE_RANK[need] >= wapp.ROLE_RANK["operator"]
    src = inspect.getsource(wapp)
    block = src[src.index('@app.route("/api/iso/fetch"'):src.index("def api_iso_fetch")]
    spec = block.split('_rate_limit("')[1].split('")')[0]
    wapp._validate_rate_spec(spec)


def test_the_route_refuses_an_operator(monkeypatch, tmp_path):
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("")
    roles_file = tmp_path / "roles.yaml"
    roles_file.write_text("default_role: viewer\nusers:\n  operatrice: operator\n")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)
    monkeypatch.setattr(wapp, "ROLES_PATH", roles_file)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: True)
    import base64
    token = base64.b64encode(b"operatrice:x").decode()
    with wapp.app.test_client() as c:
        r = c.post("/api/iso/fetch", json={"url": "https://mirror.example/x.iso"},
                   headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 403
