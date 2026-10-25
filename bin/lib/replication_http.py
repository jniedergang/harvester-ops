"""harvester-ops : appels HTTP sortants de la réplication (actions annexes
des bascules, notifications), par la garde anti-SSRF partagée.

La console tourne à côté de services internes (Vault, métadonnées de
cloud, API de clusters). Une adresse saisie par un opérateur ne doit pas
les atteindre : chaque appel passe par `url_fetch_guard` (nom résolu une
fois, connexion vers l'adresse vérifiée, certificat vérifié contre le nom).

Règles propres à ces appels :
- adresses internes refusées, sauf réseaux autorisés par l'opérateur :
  `replication.hooks.allow_private` (actions annexes) et
  `replication.notifications.allow_private` (notifications), listes de
  CIDR dans config.yaml, comme `iso_fetch.allow_private` ; les adresses de
  métadonnées de cloud restent refusées ;
- aucune redirection suivie : un en-tête secret (Authorization, jeton
  Matrix) ne part jamais vers une autre adresse que celle saisie ;
- une adresse se montre sans sa requête (`?********`), qui porte souvent
  un jeton (`mask_url`).

Bibliothèque standard seulement (PyYAML pour lire config.yaml, déjà requis).
"""

import http.client
import os
import socket
import ssl
import threading
from urllib.parse import urljoin, urlsplit

import url_fetch_guard as ufg

MASK = "********"
KINDS = ("hooks", "notifications")
CONFIG_DEFAULT = "/etc/harvester-ops/config.yaml"
BODY_READ_MAX = 65536


def config_key(kind):
    return f"replication.{kind}.allow_private"


def load_config(path=None):
    """config.yaml (ou {} s'il est absent ou illisible : rien n'est alors
    autorisé au-delà des adresses publiques)."""
    path = path or os.environ.get("HARVESTER_OPS_CONFIG") or CONFIG_DEFAULT
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:                           # noqa: BLE001 - absent, illisible, invalide
        return {}
    return data if isinstance(data, dict) else {}


def allowlist(cfg, kind):
    """Réseaux internes autorisés pour `kind` (hooks, notifications). Même
    lecture que `iso_fetch.allow_private` (entrée invalide ignorée)."""
    if kind not in KINDS:
        raise ValueError(kind)
    raw = ((((cfg or {}).get("replication") or {}).get(kind)) or {}).get("allow_private")
    return ufg.parse_allowlist({"iso_fetch": {"allow_private": raw}})


def allow_for(kind):
    return allowlist(load_config(), kind)


def refusal_text(e, kind):
    """Message d'un refus de la garde, avec la clé de configuration propre
    à `kind` au lieu de celle des ISO."""
    return str(getattr(e, "message", e)).replace("iso_fetch.allow_private", config_key(kind))


def origin(url):
    """(schéma, hôte en minuscules, port effectif) ; None si illisible."""
    try:
        u = urlsplit(url or "")
        port = u.port
    except ValueError:
        return None
    scheme = (u.scheme or "").lower()
    if not u.hostname:
        return None
    return scheme, u.hostname.lower(), port or (443 if scheme == "https" else 80)


def same_origin(a, b):
    oa = origin(a)
    return oa is not None and oa == origin(b)


def same_target(a, b):
    """Même destination exacte : schéma, hôte, port effectif, chemin et
    requête. Un secret rangé ne suit qu'une adresse inchangée (un autre
    chemin du même hôte peut être une autre API, ou un autre locataire)."""
    if not same_origin(a, b):
        return False
    try:
        ua, ub = urlsplit(a), urlsplit(b)
    except ValueError:
        return False
    return (ua.path or "/", ua.query) == (ub.path or "/", ub.query)


def mask_path(path):
    """Le chemin tel qu'il se montre : le premier segment, le reste
    remplacé par `/********` (un jeton y est souvent porté : Slack, Discord,
    Teams) ; un seul segment devient `/********` ; vide ou `/` restent."""
    if not path or path == "/":
        return path or ""
    parts = path[1:].split("/") if path.startswith("/") else path.split("/")
    if len(parts) == 1:
        return "/" + MASK
    return f"/{parts[0]}/{MASK}"


def mask_url(url):
    """L'adresse telle qu'elle se montre, la même pour tous les rôles (les
    sorties et le journal sont partagés) : schéma, hôte, port, premier
    segment du chemin (`mask_path`) ; requête remplacée par `********`,
    identifiants et fragment retirés."""
    if not isinstance(url, str) or not url:
        return url
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return MASK
    if not u.hostname:
        return MASK
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    out = f"{u.scheme}://{host}" + (f":{port}" if port else "") + mask_path(u.path)
    if u.query:
        out += "?" + MASK
    return out


def check_address(url, allow, resolver=None, strict_dns=True):
    """Lève `ufg.FetchRefused` si l'adresse (forme, puis hôte résolu) est
    refusée. `strict_dns=False` (enregistrement) : un nom qui ne se résout
    pas encore passe, il sera revérifié à l'appel."""
    _scheme, host, port, _path = ufg.check_url(url)
    try:
        ufg.resolve_checked(host, port, allow, resolver)
    except ufg.FetchRefused as e:
        if e.reason == "dns" and not strict_dns:
            return
        raise


def request(method, url, headers=None, body=None, timeout=15, allow=(), resolver=None,
            connector=ufg.default_connector):
    """Un appel HTTP vers une adresse vérifiée par la garde ; jamais de
    redirection suivie. Rend le statut (le corps est lu, 64 Kio au plus, et
    jeté). `timeout` borne l'appel ENTIER (un serveur qui répond octet par
    octet est coupé à l'échéance), pas seulement chaque lecture. Lève
    `ufg.FetchRefused` (refus d'adresse, connexion, TLS, délai,
    redirection, dont l'adresse visée est dite masquée)."""
    _scheme, host, port, path = ufg.check_url(url)
    ip = ufg.resolve_checked(host, port, allow, resolver)
    conn = connector(_scheme, host, ip, port, timeout)
    hdrs = dict({"User-Agent": "harvester-ops"}, **(headers or {}))
    expired = threading.Event()

    def _cut():
        expired.set()
        try:
            sock = getattr(conn, "sock", None)
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    timer = threading.Timer(timeout, _cut)
    timer.daemon = True
    timer.start()
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        status = int(resp.status)
        location = resp.getheader("Location") or ""
        resp.read(BODY_READ_MAX)
    except ssl.SSLError:
        if expired.is_set():
            raise ufg.FetchRefused("timeout", f"{host} did not answer within {timeout} s") from None
        raise ufg.FetchRefused("tls", f"TLS verification failed for {host}") from None
    except (OSError, http.client.HTTPException) as e:
        if expired.is_set():
            raise ufg.FetchRefused("timeout", f"{host} did not answer within {timeout} s") from None
        raise ufg.FetchRefused("connect", f"cannot reach {host} ({type(e).__name__})") from None
    finally:
        timer.cancel()
        conn.close()
    if status in ufg.REDIRECT_CODES:
        target = mask_url(urljoin(url, location)) if location else ""
        raise ufg.FetchRefused("redirect", f"{host} answered a redirect (HTTP {status}): redirects are not "
                                           "followed, so headers never reach another address; use the final "
                                           "address" + (f" ({target})" if target else ""))
    return status


def pinned_smtp(host, port, timeout, allow, implicit_tls=False, resolver=None):
    """Client SMTP vers l'adresse vérifiée de `host` (même garde que HTTP) ;
    STARTTLS et TLS implicite vérifient le certificat contre le NOM."""
    import smtplib
    _ufg_host = host.strip("[]")
    ip = ufg.resolve_checked(_ufg_host, port, allow, resolver)
    base = smtplib.SMTP_SSL if implicit_tls else smtplib.SMTP

    class _Pinned(base):
        def _get_socket(self, _host, _port, _timeout):
            sock = socket.create_connection((ip, _port), _timeout)
            if implicit_tls:
                return self.context.wrap_socket(sock, server_hostname=self._host)
            return sock

    kwargs = {"timeout": timeout}
    if implicit_tls:
        kwargs["context"] = ssl.create_default_context()
    return _Pinned(_ufg_host, port, **kwargs)
