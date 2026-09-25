"""harvester-ops : guichet HTTP des disques pendant un import (v1.45.0).

Pendant un import, chaque disque arrive sur la cible par un `DataVolume`
CDI de source HTTP : l'importeur de CDI, qui tourne dans le cluster cible,
vient chercher le disque ici. Relevé sur harv1 : il fait un HEAD puis un
seul GET, sans requête Range. On sert donc en flux, sans rien stocker :
le contenu vient d'un membre d'archive, ou directement du téléchargement
depuis le cluster source (transfert sans fichier intermédiaire).

Même principe que le guichet de l'installation bare-metal
(`web/pxe_server.py`), en plus petit, et sans dépendre de la console : le
script de transfert l'ouvre lui-même, aussi en ligne de commande.

* un jeton aléatoire par disque, révoqué dès l'import terminé ;
* aucun listing, aucun autre chemin, aucune autre méthode ;
* le jeton n'apparaît dans aucun journal.
"""

import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Entry:
    def __init__(self, opener, size):
        self.opener = opener
        self.size = size
        self.hits = 0
        self.sent = 0
        self.aborted = 0
        self.errors = 0
        self.error = None


class DiskServer:
    """`rate` : plafond de débit en octets par seconde, partagé par tous les
    disques servis (un seau à jetons) ; aucun par défaut."""

    def __init__(self, bind="0.0.0.0", port=0, rate=None):
        self.bind = bind
        self.port = port
        self.rate = rate
        self._next = 0.0
        self._rate_lock = threading.Lock()
        self._entries = {}
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None

    # -- cycle de vie ------------------------------------------------------
    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):     # noqa: A003
                pass                                # le chemin porte le jeton

            def _entry(self):
                with server._lock:
                    return server._entries.get(self.path)

            def _deny(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def _headers(self, entry):
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                if entry.size is not None:
                    self.send_header("Content-Length", str(entry.size))
                self.send_header("Connection", "close")
                self.end_headers()

            def do_HEAD(self):                      # noqa: N802
                entry = self._entry()
                if entry is None:
                    return self._deny(404)
                self._headers(entry)

            def do_GET(self):                       # noqa: N802
                entry = self._entry()
                if entry is None:
                    return self._deny(404)
                with server._lock:
                    entry.hits += 1
                self._headers(entry)
                # Deux pannes à ne pas confondre (vécu sur harvlab2) : le
                # CLIENT qui raccroche est normal, l'importeur de CDI coupe sa
                # première connexion après avoir reconnu le format, puis
                # recommence ; la SOURCE qui échoue est une vraie erreur. Dans
                # les deux cas la lecture de la source est refermée (un
                # téléchargement depuis le cluster source s'arrête).
                it = iter(entry.opener())
                try:
                    while True:
                        try:
                            chunk = next(it)
                        except StopIteration:
                            break
                        except Exception as e:      # noqa: BLE001
                            entry.error = f"{type(e).__name__}: {e}"
                            entry.errors += 1
                            break
                        if not chunk:
                            continue
                        server._throttle(len(chunk))
                        try:
                            self.wfile.write(chunk)
                        except OSError:
                            with server._lock:
                                entry.aborted += 1
                            break
                        entry.sent += len(chunk)
                finally:
                    close = getattr(it, "close", None)
                    if close:
                        close()
                self.close_connection = True

            def _refuse(self):
                self._deny(405 if self._entry() is not None else 404)

            do_POST = do_PUT = do_DELETE = do_PATCH = _refuse   # noqa: N815

        self._httpd = ThreadingHTTPServer((self.bind, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        with self._lock:
            self._entries.clear()

    def _throttle(self, n):
        """Réserve la place de `n` octets dans le débit plafonné, et attend
        son tour : la moyenne ne dépasse pas `rate`, tous flux confondus."""
        if not self.rate:
            return
        with self._rate_lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + n / float(self.rate)
        if start > now:
            time.sleep(start - now)

    # -- publication -------------------------------------------------------
    def publish(self, opener, size):
        """`opener()` rend un itérable d'octets, rappelé à chaque GET."""
        path = f"/{secrets.token_urlsafe(24)}.raw.gz"
        with self._lock:
            self._entries[path] = _Entry(opener, size)
        return path

    def revoke(self, path):
        with self._lock:
            self._entries.pop(path, None)

    def _get(self, path):
        with self._lock:
            return self._entries.get(path)

    def hits(self, path):
        e = self._get(path)
        return e.hits if e else 0

    def sent(self, path):
        e = self._get(path)
        return e.sent if e else 0

    def aborted(self, path):
        e = self._get(path)
        return e.aborted if e else 0

    def errors(self, path):
        e = self._get(path)
        return e.errors if e else 0

    def error(self, path):
        e = self._get(path)
        return e.error if e else None
