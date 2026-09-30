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
    POST /pxe/inventory/<token>  dépôt de l'inventaire d'un démarrage de
                                 découverte (1.78.0) : 1 Mio au plus, un seul
                                 envoi accepté, commençant par `== lsblk`,
                                 écrit en 0600

Le script de découverte n'est PAS servi ici : il est déposé DANS l'ISO
(`/run/initramfs/live/discover.sh`), ce qui évite toute commande entre
guillemets sur la ligne noyau et tout téléchargement avant le réseau.

Les jetons sont aléatoires, à durée de vie limitée, révoqués dès la fin de
l'installation. Aucun listing de répertoire, aucun autre chemin : ce n'est
pas un serveur de fichiers, c'est un guichet à deux tickets.

Aucune dépendance nouvelle — `http.server` de la bibliothèque standard.
"""

import logging
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("harvester-ops.pxe")

# token -> {"path": Path, "kind": "iso"|"config"|"inventory", "expires": epoch, "hits": int}
_TOKENS = {}
_LOCK = threading.Lock()
_SERVER = None
_THREAD = None

DEFAULT_TTL = 4 * 3600      # une installation dépasse rarement 30 min
INVENTORY_MAX = 1024 * 1024  # un inventaire réel pèse quelques dizaines de Kio


class TokenInUse(Exception):
    """Le jeton demandé est déjà armé (une autre action l'utilise)."""


def issue(path, kind, ttl=DEFAULT_TTL, token=None):
    """Enregistre un artefact et renvoie son jeton.

    `token` arme un jeton choisi par l'appelant au lieu d'en tirer un :
    c'est le cas du dépôt d'inventaire, dont l'adresse est gravée dans le
    script de l'ISO avant que le fichier soit servi. Déjà armé, il est
    refusé."""
    with _LOCK:
        if token is None:
            token = secrets.token_urlsafe(24)
        elif token in _TOKENS and _TOKENS[token]["expires"] >= time.time():
            raise TokenInUse(kind)
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


def _peek(token, kind):
    """Jeton valide, du bon type et non expiré ; ne le consomme pas."""
    with _LOCK:
        entry = _TOKENS.get(token)
        return bool(entry and entry["kind"] == kind and entry["expires"] >= time.time())


def _consume(token, kind):
    """Comme `_resolve`, mais retire le jeton : un seul dépôt par jeton."""
    with _LOCK:
        entry = _TOKENS.get(token)
        if not entry or entry["kind"] != kind:
            return None
        _TOKENS.pop(token, None)
        if entry["expires"] < time.time():
            return None
        return entry["path"]


def _write_private(target, data):
    """Écrit en 0600 dès la création (jamais lisible par d'autres, même un
    instant), puis renomme : le lecteur ne voit jamais un fichier partiel."""
    target = Path(target)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Handler(BaseHTTPRequestHandler):
    server_version = "harvester-ops-pxe"
    protocol_version = "HTTP/1.1"      # nécessaire pour les Range/keep-alive

    def log_message(self, fmt, *args):          # noqa: A003
        # Le chemin porte le jeton (ISO, configuration qui contient le jeton
        # du cluster et le mot de passe, dépôt d'inventaire) : ne journaliser
        # que la méthode, le type de ressource et le code, jamais le chemin
        # (relecture de la 1.78.0, fuite vérifiée dans le journal).
        req = getattr(self, "requestline", "") or ""
        method = req.split(" ", 1)[0] if req else "-"
        path = getattr(self, "path", "") or ""
        kind = path.split("/")[2] if path.startswith("/pxe/") and path.count("/") >= 3 else "-"
        code = args[1] if fmt.startswith('"%s" %s') and len(args) > 1 else ""
        log.info("pxe %s %s %s %s", self.address_string(), method, kind, code)

    def _deny(self, code=404):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):                          # noqa: N802
        self._serve(head_only=True)

    def do_GET(self):                           # noqa: N802
        self._serve(head_only=False)

    def do_POST(self):                          # noqa: N802
        """Dépôt d'un inventaire : seul verbe d'écriture, seul type admis."""
        token, kind = self._match()
        if not token or kind != "inventory":
            return self._deny(404)
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length < 0:
            return self._deny(411)
        if length > INVENTORY_MAX:
            # refusé avant lecture ; la connexion est fermée plutôt que de
            # laisser traîner un corps qu'on n'a pas lu
            self.close_connection = True
            return self._deny(413)
        if length == 0:
            return self._deny(400)
        # jeton vérifié AVANT de lire le corps (sans le consommer) : un
        # inconnu n'obtient pas qu'on lise son Mio
        if not _peek(token, "inventory"):
            self.close_connection = True
            return self._deny(404)
        body = self.rfile.read(length)
        # un inventaire commence par sa section lsblk ; autre chose est
        # refusé sans consommer le jeton (le vrai envoi peut encore venir)
        if not re.search(rb"(?:^|\n)== lsblk\r?\n", body):
            return self._deny(400)
        target = _consume(token, "inventory")
        if not target:
            return self._deny(404)
        try:
            _write_private(target, body)
        except OSError:
            return self._deny(500)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _match(self):
        """Analyse le chemin. Rien d'autre que les formes attendues."""
        path = self.path.split("?", 1)[0]
        for prefix, suffix, kind in (("/pxe/iso/", ".iso", "iso"),
                                     ("/pxe/config/", ".yaml", "config"),
                                     ("/pxe/inventory/", "", "inventory")):
            if path.startswith(prefix) and path.endswith(suffix):
                token = path[len(prefix):len(path) - len(suffix)]
                # un jeton est urlsafe-base64 : pas de / ni de .., donc pas
                # de traversée possible, mais on refuse explicitement.
                if "/" in token or ".." in token or not token:
                    return None, None
                return token, kind
        return None, None

    def _serve(self, head_only):
        token, kind = self._match()
        # un jeton de dépôt ne se lit jamais
        if not token or kind == "inventory":
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
    # `port=0` demande un port éphémère : le distinguer de « non précisé »,
    # qu'un simple `or` confondait avec 0 et renvoyait sur le port fixe.
    if port is None:
        port = os.environ.get("HARVESTER_OPS_PXE_PORT", 8091)
    port = int(port)
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
