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
from urllib.parse import urlsplit

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


def mask_url(url):
    """L'adresse telle qu'elle se montre : schéma, hôte, port et chemin ;
    requête remplacée par `********`, identifiants et fragment retirés."""
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
    out = f"{u.scheme}://{host}" + (f":{port}" if port else "") + (u.path or "")
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
    jeté). Lève `ufg.FetchRefused` (refus d'adresse, connexion, TLS,
    redirection)."""
    _scheme, host, port, path = ufg.check_url(url)
    ip = ufg.resolve_checked(host, port, allow, resolver)
    conn = connector(_scheme, host, ip, port, timeout)
    hdrs = dict({"User-Agent": "harvester-ops"}, **(headers or {}))
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        status = int(resp.status)
        resp.read(BODY_READ_MAX)
    except ssl.SSLError:
        raise ufg.FetchRefused("tls", f"TLS verification failed for {host}") from None
    except (OSError, http.client.HTTPException) as e:
        raise ufg.FetchRefused("connect", f"cannot reach {host} ({type(e).__name__})") from None
    finally:
        conn.close()
    if status in ufg.REDIRECT_CODES:
        raise ufg.FetchRefused("redirect", f"{host} answered a redirect (HTTP {status}): redirects are not "
                                           "followed, so headers never reach another address; use the final "
                                           "address")
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
