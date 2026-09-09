"""harvester-ops — serveur d'artefacts d'installation (v1.17.0).

Pourquoi un second serveur plutôt qu'une route Flask de plus :

* l'application sert en **HTTPS auto-signé** et exige une authentification
  Basic sur tout. Un BMC qui va chercher un ISO ne sait ni s'authentifier
  ni faire confiance à ce certificat ;
* le serveur WSGI est celui de développement de Werkzeug : mono-processus,
  sans `sendfile`, avec une gestion des requêtes `Range` fragile. Or un
  iLO télécharge un ISO de plusieurs gigaoctets par plages.

Ce module n'expose donc que deux chemins, en HTTP simple, sur un port
dédié, et uniquement pour des jetons enregistrés explicitement par
l'action d'installation :

    GET /pxe/iso/<token>.iso     l'image remasterisée
    GET /pxe/config/<token>.yaml la configuration Harvester du node

Les jetons sont aléatoires, à durée de vie limitée, révoqués dès la fin de
l'installation. Aucun listing de répertoire, aucun autre chemin : ce n'est
pas un serveur de fichiers, c'est un guichet à deux tickets.

Aucune dépendance nouvelle — `http.server` de la bibliothèque standard.
"""

import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("harvester-ops.pxe")

# token -> {"path": Path, "kind": "iso"|"config", "expires": epoch, "hits": int}
_TOKENS = {}
_LOCK = threading.Lock()
_SERVER = None
_THREAD = None

DEFAULT_TTL = 4 * 3600      # une installation dépasse rarement 30 min


def issue(path, kind, ttl=DEFAULT_TTL):
    """Enregistre un artefact et renvoie son jeton."""
    token = secrets.token_urlsafe(24)
    with _LOCK:
        _TOKENS[token] = {
            "path": Path(path),
            "kind": kind,
            "expires": time.time() + ttl,
            "hits": 0,
        }
    return token


def revoke(*tokens):
    with _LOCK:
        for t in tokens:
            _TOKENS.pop(t, None)


def stats(token):
    with _LOCK:
        e = _TOKENS.get(token)
        return dict(e) if e else None


def _resolve(token, kind):
    """Jeton -> chemin, si valide, non expiré et du bon type."""
    with _LOCK:
        entry = _TOKENS.get(token)
        if not entry:
            return None
        if entry["expires"] < time.time():
            _TOKENS.pop(token, None)
            return None
        if entry["kind"] != kind:
            return None
        entry["hits"] += 1
        return entry["path"]


class _Handler(BaseHTTPRequestHandler):
    server_version = "harvester-ops-pxe"
    protocol_version = "HTTP/1.1"      # nécessaire pour les Range/keep-alive

    def log_message(self, fmt, *args):          # noqa: A003
        log.info("pxe %s %s", self.address_string(), fmt % args)

    def _deny(self, code=404):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):                          # noqa: N802
        self._serve(head_only=True)

    def do_GET(self):                           # noqa: N802
        self._serve(head_only=False)

    def _match(self):
        """Analyse le chemin. Rien d'autre que les deux formes attendues."""
        path = self.path.split("?", 1)[0]
        for prefix, suffix, kind in (("/pxe/iso/", ".iso", "iso"),
                                     ("/pxe/config/", ".yaml", "config")):
            if path.startswith(prefix) and path.endswith(suffix):
                token = path[len(prefix):-len(suffix)]
                # un jeton est urlsafe-base64 : pas de / ni de .., donc pas
                # de traversée possible, mais on refuse explicitement.
                if "/" in token or ".." in token or not token:
                    return None, None
                return token, kind
        return None, None

    def _serve(self, head_only):
        token, kind = self._match()
        if not token:
            return self._deny(404)
        target = _resolve(token, kind)
        if not target or not target.is_file():
            return self._deny(404)

        size = target.stat().st_size
        ctype = ("application/octet-stream" if kind == "iso"
                 else "text/yaml; charset=utf-8")
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            # Les BMC téléchargent un ISO par plages ; sans ce support,
            # l'insertion de média échoue ou repart de zéro sans fin.
            try:
                first, _, last = rng[6:].partition("-")
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:                      # suffixe : bytes=-N
                    start = max(0, size - int(last))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        with open(target, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


def start(port=None, bind="0.0.0.0"):
    """Démarre le serveur si besoin. Idempotent, renvoie le port utilisé."""
    global _SERVER, _THREAD
    port = int(port or os.environ.get("HARVESTER_OPS_PXE_PORT", 8091))
    if _SERVER is not None:
        return _SERVER.server_address[1]
    _SERVER = ThreadingHTTPServer((bind, port), _Handler)
    _SERVER.daemon_threads = True
    _THREAD = threading.Thread(target=_SERVER.serve_forever, daemon=True,
                               name="pxe-artifacts")
    _THREAD.start()
    log.info("PXE artifact server listening on %s:%s", bind, port)
    return _SERVER.server_address[1]


def stop():
    global _SERVER, _THREAD
    if _SERVER is not None:
        _SERVER.shutdown()
        _SERVER.server_close()
        _SERVER = None
        _THREAD = None


def is_running():
    return _SERVER is not None
