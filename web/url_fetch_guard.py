"""Téléchargement côté serveur d'une URL choisie par l'opérateur, sans SSRF.

Utilisé par /api/iso/fetch (ISO bare-metal) et prévu pour les images KVM.
La console tourne à côté de services internes (Vault sur 127.0.0.1:8200,
métadonnées de cloud, API de clusters) : une URL arbitraire ne doit pas
pouvoir les atteindre, ni directement, ni par une redirection, ni par un
nom DNS qui résout vers une adresse interne.

Règles :
- http et https seulement, à chaque saut ; redirections suivies à la main,
  5 au plus, l'hôte de chaque saut revérifié ; pas de retour de https à http ;
- le nom est résolu UNE fois et la connexion part vers l'adresse vérifiée
  (pas de seconde résolution qu'un DNS « rebinding » pourrait détourner) ;
  en https, le certificat est vérifié contre le NOM demandé ;
- adresses refusées : bouclage, lien local, privées (RFC 1918, ULA, CGNAT),
  multicast, non spécifiée, réservées ; sauf si l'opérateur les autorise
  par `iso_fetch.allow_private` (miroir interne d'un site airgap) ;
- les adresses de métadonnées de cloud restent refusées même autorisées.
"""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlsplit

MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 20 * 1024 ** 3          # 20 GiB
DEFAULT_MIN_FREE_BYTES = 1024 ** 3          # garde 1 GiB libre

# Métadonnées de cloud (AWS/GCP/Azure/OpenStack ; AWS en IPv6) : jamais
METADATA_ADDRESSES = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
})

# Plage partagée des opérateurs (CGNAT), absente de is_private
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

REDIRECT_CODES = (301, 302, 303, 307, 308)


class FetchRefused(Exception):
    """Refus ou échec à montrer tel quel à l'opérateur : le message ne porte
    ni chemin local ni détail interne."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason
        self.message = message


def parse_allowlist(cfg):
    """Réseaux autorisés par `iso_fetch.allow_private` (liste de CIDR).
    Une entrée invalide est ignorée : elle ne doit pas tout ouvrir."""
    raw = ((cfg or {}).get("iso_fetch") or {}).get("allow_private") or []
    if isinstance(raw, str):
        raw = [raw]
    nets = []
    for item in raw:
        try:
            nets.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            continue
    return nets


def limits(cfg):
    """(plafond de taille, espace libre minimal) en octets, réglables par
    `iso_fetch.max_bytes` et `iso_fetch.min_free_bytes`."""
    sect = (cfg or {}).get("iso_fetch") or {}

    def num(key, default):
        try:
            v = int(sect.get(key, default))
            return v if v > 0 else default
        except (TypeError, ValueError):
            return default
    return num("max_bytes", DEFAULT_MAX_BYTES), num("min_free_bytes", DEFAULT_MIN_FREE_BYTES)


def address_refusal(ip, allow=()):
    """None si l'adresse est joignable, sinon la raison du refus."""
    ip = ipaddress.ip_address(ip)
    # ::ffff:127.0.0.1 est 127.0.0.1
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in METADATA_ADDRESSES:
        return "metadata"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_multicast:
        return "multicast"
    if any(ip in net for net in allow if net.version == ip.version):
        return None
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_private or (ip.version == 4 and ip in _CGNAT):
        return "private"
    if ip.is_reserved or not ip.is_global:
        return "reserved"
    return None


def check_url(url):
    """Découpe et valide la forme d'une URL. Rend (scheme, host, port, chemin)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FetchRefused("bad-url", "the URL is not valid")
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise FetchRefused("scheme", "only http and https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise FetchRefused("credentials", "credentials in the URL are not supported")
    host = parts.hostname
    if not host:
        raise FetchRefused("bad-url", "the URL has no host")
    port = port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, path


def resolve_checked(host, port, allow=(), resolver=None):
    """Résout le nom et vérifie TOUTES les adresses rendues (un nom qui
    mêle une adresse publique et une adresse interne est refusé). Rend
    l'adresse à laquelle se connecter."""
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        why = address_refusal(literal, allow)
        if why:
            raise FetchRefused(
                "address",
                f"{host} is a {why} address; internal addresses are refused "
                "unless allowed by iso_fetch.allow_private")
        return str(literal)
    resolver = resolver or socket.getaddrinfo
    try:
        infos = resolver(host, port, 0, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise FetchRefused("dns", f"cannot resolve {host}")
    addrs = []
    for info in infos:
        a = info[4][0].split("%", 1)[0]
        if a not in addrs:
            addrs.append(a)
    if not addrs:
        raise FetchRefused("dns", f"cannot resolve {host}")
    for a in addrs:
        why = address_refusal(a, allow)
        if why:
            raise FetchRefused(
                "address",
                f"{host} resolves to a {why} address; internal addresses are "
                "refused unless allowed by iso_fetch.allow_private")
    return addrs[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connexion vers l'adresse déjà vérifiée ; l'en-tête Host garde le nom."""

    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Comme ci-dessus, avec TLS vérifié contre le NOM (SNI + certificat)."""

    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout,
                         context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def default_connector(scheme, host, ip, port, timeout):
    cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    return cls(host, ip, port, timeout)


def open_checked(url, allow=(), *, timeout=60, resolver=None,
                 connector=default_connector, max_redirects=MAX_REDIRECTS):
    """Ouvre l'URL en suivant les redirections à la main. Rend
    (réponse, connexion, URL finale) ; l'appelant ferme la connexion."""
    current = url
    seen_https = False
    for _hop in range(max_redirects + 1):
        scheme, host, port, path = check_url(current)
        # une redirection de https vers http perdrait la vérification TLS
        if seen_https and scheme == "http":
            raise FetchRefused("downgrade", "redirect from https to http refused")
        seen_https = seen_https or scheme == "https"
        ip = resolve_checked(host, port, allow, resolver)
        conn = connector(scheme, host, ip, port, timeout)
        try:
            conn.request("GET", path, headers={"User-Agent": "harvester-ops",
                                                "Accept-Encoding": "identity"})
            resp = conn.getresponse()
        except ssl.SSLError:
            conn.close()
            raise FetchRefused("tls", f"TLS verification failed for {host}")
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise FetchRefused("connect", f"cannot reach {host} ({type(e).__name__})")
        if resp.status in REDIRECT_CODES:
            loc = resp.getheader("Location")
            conn.close()
            if not loc:
                raise FetchRefused("redirect", "redirect without a Location header")
            current = urljoin(current, loc)
            continue
        if resp.status != 200:
            conn.close()
            raise FetchRefused("http", f"the server answered HTTP {resp.status}")
        return resp, conn, current
    raise FetchRefused("redirect", f"more than {max_redirects} redirects")
