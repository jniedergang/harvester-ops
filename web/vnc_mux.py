"""
harvester-ops : console VNC partagée entre plusieurs navigateurs (v1.41.0).

Pourquoi ce module existe
-------------------------
KubeVirt n'accepte qu'UNE connexion VNC par VM : une nouvelle connexion au
sous-résource `/vnc` ferme la précédente (vérifié contre harv1, sans notre
relais : la première reçoit une fermeture 1005 dès que la seconde s'ouvre).
Chaque console se reconnectant d'elle-même en 400 ms, deux exploitants sur
la même VM s'éjectaient l'un l'autre en boucle : 15 coupures chacun en 25 s,
l'écran « clignotait ».

La console tient donc UNE connexion vers KubeVirt par VM, et la partage : ce
module parle RFB (RFC 6143) des deux côtés.

  navigateur 1 ─┐                                   ┌─ KubeVirt /vnc (QEMU)
  navigateur 2 ─┼─ poignée de main locale ─ VncHub ─┤  une seule connexion
  navigateur 3 ─┘   (ServerInit rejoué)             └─ format et encodages fixés

Trois contraintes, qui dictent la forme :

  * un arrivant doit prendre le flux à une FRONTIÈRE de message, pas au
    milieu d'un rectangle : les messages du serveur sont donc analysés pour
    en connaître la longueur, et diffusés entiers ;
  * il doit pouvoir décoder ce qu'il reçoit sans avoir vu le début : les
    encodages à état (Tight, ZRLE, zlib, dont le dictionnaire de compression
    court depuis la première image) sont exclus. On impose Hextile et Raw,
    sans état ;
  * le format de pixel est unique pour tous : celui que noVNC impose de
    toute façon (32 bits, profondeur 24, petit-boutiste). Les SetPixelFormat
    et SetEncodings des navigateurs sont absorbés.

Ce qui ne se voit qu'une fois est rejoué à chaque arrivant : la taille de
l'écran (dans son ServerInit), le curseur courant, et l'accusé de
l'extension clavier QEMU. Ce dernier compte pour un clavier AZERTY : sans
lui, noVNC enverrait des keysyms que QEMU traduit avec un clavier américain.

Le module ne dépend ni de Flask ni du cluster : les connexions sont des
objets à `send`/`receive`/`close`/`connected` (simple-websocket en
production, des doublures dans les tests).
"""

import logging
import queue
import struct
import threading
import time

log = logging.getLogger("harvester-ops.vnc")

VERSION = b"RFB 003.008\n"
SEC_NONE = 1

# Le format que noVNC impose quel que soit le serveur (rfb.js, pixelFormat) :
# 32 bpp, profondeur 24, petit-boutiste, vraies couleurs, rouge à 0.
PIXEL_FORMAT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
BPP = 4

ENC_RAW, ENC_COPYRECT, ENC_HEXTILE = 0, 1, 5
PS_DESKTOP_SIZE, PS_LAST_RECT, PS_CURSOR, PS_QEMU_EXT_KEY = -223, -224, -239, -258

# Par ordre de préférence : QEMU prend le premier qu'il connaît.
UPSTREAM_ENCODINGS = (ENC_HEXTILE, ENC_COPYRECT, ENC_RAW,
                      PS_DESKTOP_SIZE, PS_LAST_RECT, PS_QEMU_EXT_KEY, PS_CURSOR)

# Au-delà, un navigateur trop lent est déconnecté plutôt que de faire
# grossir la mémoire du serveur : il se reconnectera et recevra une image
# complète.
CLIENT_QUEUE_LIMIT = 64 * 1024 * 1024

FORWARD, DROP = "forward", "drop"


class Incomplete(Exception):
    """Il manque des octets ; `need` est la longueur totale minimale connue."""

    def __init__(self, need):
        super().__init__(need)
        self.need = need


class ProtocolError(Exception):
    pass


def _need(buf, end):
    if len(buf) < end:
        raise Incomplete(end)


# ---------------------------------------------------------------------------
# Analyse des messages
# ---------------------------------------------------------------------------

def parse_server_message(buf):
    """(longueur, évènements) du premier message serveur complet de `buf`.

    Les évènements disent ce qu'un arrivant devra recevoir en rattrapage :
    ('size', w, h), ('cursor', début, fin) dans le message, ('extkey',).
    Lève Incomplete s'il manque des octets, ProtocolError sur un message
    qu'on n'a pas négocié (on ne saurait pas où il finit)."""
    _need(buf, 1)
    kind = buf[0]
    if kind == 0:
        return _parse_update(buf)
    if kind == 1:                                   # SetColourMapEntries
        _need(buf, 6)
        n = struct.unpack_from(">H", buf, 4)[0]
        _need(buf, 6 + 6 * n)
        return 6 + 6 * n, []
    if kind == 2:                                   # Bell
        return 1, []
    if kind == 3:                                   # ServerCutText
        _need(buf, 8)
        length = struct.unpack_from(">I", buf, 4)[0]
        if length & 0x80000000:
            raise ProtocolError("extended clipboard was not negotiated")
        _need(buf, 8 + length)
        return 8 + length, []
    raise ProtocolError(f"server message type {kind}")


def _parse_update(buf):
    _need(buf, 4)
    count = struct.unpack_from(">H", buf, 2)[0]
    pos, events, i = 4, [], 0
    # 0xFFFF : nombre inconnu, la liste se termine par un rectangle LastRect.
    while count == 0xFFFF or i < count:
        _need(buf, pos + 12)
        _x, _y, w, h, enc = struct.unpack_from(">HHHHi", buf, pos)
        start = pos
        pos += 12
        if enc == ENC_RAW:
            pos += w * h * BPP
        elif enc == ENC_COPYRECT:
            pos += 4
        elif enc == ENC_HEXTILE:
            pos = _skip_hextile(buf, pos, w, h)
        elif enc == PS_DESKTOP_SIZE:
            events.append(("size", w, h))
        elif enc == PS_LAST_RECT:
            break
        elif enc == PS_CURSOR:
            pos += w * h * BPP + ((w + 7) // 8) * h
            events.append(("cursor", start, pos))
        elif enc == PS_QEMU_EXT_KEY:
            events.append(("extkey",))
        else:
            raise ProtocolError(f"encoding {enc} was not negotiated")
        _need(buf, pos)
        i += 1
    return pos, events


def _skip_hextile(buf, pos, w, h):
    """Fin des données Hextile d'un rectangle (tuiles de 16x16)."""
    for ty in range(0, h, 16):
        th = min(16, h - ty)
        for tx in range(0, w, 16):
            tw = min(16, w - tx)
            _need(buf, pos + 1)
            sub = buf[pos]
            pos += 1
            if sub & 1:                              # Raw
                pos += tw * th * BPP
                continue
            if sub & 2:                              # BackgroundSpecified
                pos += BPP
            if sub & 4:                              # ForegroundSpecified
                pos += BPP
            if sub & 8:                              # AnySubrects
                _need(buf, pos + 1)
                n = buf[pos]
                pos += 1 + n * ((BPP if sub & 16 else 0) + 2)
    return pos


def parse_client_message(buf):
    """(longueur, FORWARD ou DROP) du premier message client complet.

    SetPixelFormat et SetEncodings sont absorbés : le format et les
    encodages de la connexion partagée sont fixés une fois pour tous."""
    _need(buf, 1)
    kind = buf[0]
    if kind == 0:                                   # SetPixelFormat
        _need(buf, 20)
        return 20, DROP
    if kind == 2:                                   # SetEncodings
        _need(buf, 4)
        n = struct.unpack_from(">H", buf, 2)[0]
        _need(buf, 4 + 4 * n)
        return 4 + 4 * n, DROP
    if kind == 3:                                   # FramebufferUpdateRequest
        _need(buf, 10)
        return 10, FORWARD
    if kind == 4:                                   # KeyEvent
        _need(buf, 8)
        return 8, FORWARD
    if kind == 5:                                   # PointerEvent
        _need(buf, 6)
        return 6, FORWARD
    if kind == 6:                                   # ClientCutText
        _need(buf, 8)
        length = struct.unpack_from(">I", buf, 4)[0]
        if length & 0x80000000:
            raise ProtocolError("extended clipboard was not negotiated")
        _need(buf, 8 + length)
        return 8 + length, FORWARD
    if kind == 255:                                 # message QEMU
        _need(buf, 2)
        if buf[1] == 0:                             # touche étendue (scancode)
            _need(buf, 12)
            return 12, FORWARD
        raise ProtocolError(f"QEMU client message {buf[1]}")
    raise ProtocolError(f"client message type {kind}")


def full_update_request(width, height):
    return struct.pack(">BBHHHH", 3, 0, 0, 0, width, height)


# ---------------------------------------------------------------------------
# Lecture par octets au-dessus d'un websocket
# ---------------------------------------------------------------------------

class ByteStream:
    """Lecture d'un nombre exact d'octets sur un websocket, dont les trames
    découpent le flux RFB n'importe où."""

    def __init__(self, ws):
        self.ws = ws
        self.buf = bytearray()

    def fill(self, timeout):
        data = self.ws.receive(timeout=timeout)
        if data is None:
            if not self.ws.connected:
                raise EOFError("connection closed")
            return False
        if isinstance(data, str):
            data = data.encode()
        self.buf += data
        return True

    def read(self, n, timeout=10.0):
        deadline = time.monotonic() + timeout
        while len(self.buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"waited {timeout}s for {n} bytes")
            self.fill(min(left, 5.0))
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


# ---------------------------------------------------------------------------
# Un navigateur
# ---------------------------------------------------------------------------

class VncClient:
    """Un navigateur rattaché. Un seul fil écrit dans son websocket (celui-ci),
    ce qui est la règle de simple-websocket."""

    def __init__(self, ws, user=""):
        self.ws = ws
        self.user = user or ""
        self.joined_at = time.time()
        self.alive = True
        self._q = queue.Queue()
        self._qbytes = 0
        self._qlock = threading.Lock()
        self._writer = threading.Thread(target=self._write_loop, daemon=True,
                                        name="vnc-client-writer")
        self._writer.start()

    def push(self, data):
        if not self.alive:
            return
        with self._qlock:
            if self._qbytes + len(data) > CLIENT_QUEUE_LIMIT:
                log.warning("console client too slow (%d bytes queued), dropping it",
                            self._qbytes)
                self.kill()
                return
            self._qbytes += len(data)
        self._q.put(bytes(data))

    def kill(self):
        self.alive = False
        self._q.put(None)

    def _write_loop(self):
        while True:
            data = self._q.get()
            if data is None:
                break
            with self._qlock:
                self._qbytes -= len(data)
            try:
                self.ws.send(data)
            except Exception:                       # noqa: BLE001
                self.alive = False
                break
        try:
            self.ws.close()
        except Exception:                           # noqa: BLE001
            pass

    def wait_closed(self, timeout=5):
        self._writer.join(timeout=timeout)


# ---------------------------------------------------------------------------
# La connexion partagée d'une VM
# ---------------------------------------------------------------------------

class VncHub:
    """Une connexion vers KubeVirt, partagée par tous les navigateurs d'une VM."""

    def __init__(self, key, upstream, on_lost=None, on_empty=None):
        self.key = key
        self.up = upstream
        self.stream = ByteStream(upstream)
        self.on_lost = on_lost          # connexion amont perdue (pas par nous)
        self.on_empty = on_empty        # plus personne : se retirer du registre
        self.lock = threading.RLock()   # clients, état de rattrapage, diffusion
        self.up_lock = threading.Lock() # un seul écrivain vers l'amont
        self.clients = []
        self.width = self.height = 0
        self.name = b""
        self.cursor_rect = None
        self.ext_key = False
        self.closed = False
        self.close_reason = None
        self.created_at = time.time()
        self._reader = None

    # --- amont ------------------------------------------------------------
    def start(self, timeout=10.0):
        """Poignée de main avec QEMU, puis lecture continue."""
        s = self.stream
        version = s.read(12, timeout)
        if not version.startswith(b"RFB 003."):
            raise ProtocolError(f"unexpected server version {version!r}")
        self.send_upstream(VERSION)
        count = s.read(1, timeout)[0]
        if count == 0:
            length = struct.unpack(">I", s.read(4, timeout))[0]
            raise ProtocolError("server refused: " + s.read(length, timeout).decode(errors="replace"))
        if SEC_NONE not in s.read(count, timeout):
            raise ProtocolError("server offers no 'None' security type")
        self.send_upstream(bytes([SEC_NONE]))
        if struct.unpack(">I", s.read(4, timeout))[0] != 0:
            raise ProtocolError("security handshake failed")
        self.send_upstream(b"\x01")                 # ClientInit : partagé
        self.width, self.height = struct.unpack(">HH", s.read(4, timeout))
        s.read(16, timeout)                         # son format : remplacé
        self.name = s.read(struct.unpack(">I", s.read(4, timeout))[0], timeout)
        self.send_upstream(b"\x00\x00\x00\x00" + PIXEL_FORMAT)
        self.send_upstream(struct.pack(">BxH", 2, len(UPSTREAM_ENCODINGS))
                           + b"".join(struct.pack(">i", e) for e in UPSTREAM_ENCODINGS))
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"vnc-hub-{self.key[-1]}")
        self._reader.start()

    def send_upstream(self, data):
        with self.up_lock:
            self.up.send(bytes(data))

    def _read_loop(self):
        buf = self.stream.buf               # ce qui a suivi le ServerInit
        need = 1
        reason = "upstream closed"
        try:
            while not self.closed:
                if len(buf) < need:
                    if not self.stream.fill(30):
                        continue
                    continue
                try:
                    end, events = parse_server_message(buf)
                except Incomplete as e:
                    # Ne pas réanalyser à chaque trame : attendre au moins la
                    # longueur connue. Une image complète en Hextile arrive
                    # en dizaines de trames.
                    need = e.need
                    continue
                msg = bytes(buf[:end])
                del buf[:end]
                need = 1
                self._broadcast(msg, events)
        except EOFError:
            reason = "upstream closed"
        except ProtocolError as e:
            reason = f"protocol error: {e}"
            log.error("console %s: %s", "/".join(self.key), e)
        except Exception as e:                       # noqa: BLE001
            reason = f"upstream error: {e}"
        if not self.closed:
            self.close(reason, lost=True)

    def _broadcast(self, msg, events):
        with self.lock:
            for ev in events:
                if ev[0] == "size":
                    self.width, self.height = ev[1], ev[2]
                elif ev[0] == "cursor":
                    self.cursor_rect = msg[ev[1]:ev[2]]
                elif ev[0] == "extkey":
                    self.ext_key = True
            for c in list(self.clients):
                if c.alive:
                    c.push(msg)

    # --- navigateurs ------------------------------------------------------
    def serve(self, ws, user=""):
        """Sert un navigateur jusqu'à sa déconnexion. Rend False si la
        connexion partagée s'est fermée avant qu'il ait pu la rejoindre (le
        dernier venait de partir) : l'appelant en ouvre alors une nouvelle."""
        client = VncClient(ws, user)
        stream = ByteStream(ws)
        try:
            client.push(VERSION)
            if not stream.read(12, 10).startswith(b"RFB 003."):
                raise ProtocolError("unexpected client version")
            client.push(bytes([1, SEC_NONE]))
            if stream.read(1, 10)[0] != SEC_NONE:
                raise ProtocolError("client chose another security type")
            client.push(b"\x00\x00\x00\x00")
            stream.read(1, 10)                      # ClientInit (partagé ou non)
            if not self._join(client):
                client.kill()
                return False
        except Exception as e:                       # noqa: BLE001
            log.warning("console client handshake failed on %s: %s",
                        "/".join(self.key), e)
            client.kill()
            return True
        try:
            # Une image complète pour l'arrivant ; les autres la reçoivent
            # aussi, c'est le prix d'un encodage sans état.
            self.send_upstream(full_update_request(self.width, self.height))
            buf = stream.buf
            while client.alive and not self.closed:
                try:
                    end, action = parse_client_message(buf)
                except Incomplete:
                    if not stream.fill(30):
                        continue
                    continue
                msg = bytes(buf[:end])
                del buf[:end]
                if action == FORWARD:
                    self.send_upstream(msg)
        except (EOFError, ProtocolError) as e:
            if isinstance(e, ProtocolError):
                log.warning("console client sent %s, disconnecting it", e)
        except Exception:                            # noqa: BLE001
            pass
        finally:
            self._leave(client)
        return True

    def _join(self, client):
        with self.lock:
            if self.closed:
                return False
            client.push(struct.pack(">HH", self.width, self.height) + PIXEL_FORMAT
                        + struct.pack(">I", len(self.name)) + self.name)
            rects = []
            if self.cursor_rect:
                rects.append(self.cursor_rect)
            if self.ext_key:
                rects.append(struct.pack(">HHHHi", 0, 0, 0, 0, PS_QEMU_EXT_KEY))
            if rects:
                client.push(struct.pack(">BxH", 0, len(rects)) + b"".join(rects))
            self.clients.append(client)
            log.info("console %s: %s joined (%d connected)", "/".join(self.key),
                     client.user or "someone", len(self.clients))
            return True

    def _leave(self, client):
        client.kill()
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)
            empty = not self.clients
            log.info("console %s: %s left (%d connected)", "/".join(self.key),
                     client.user or "someone", len(self.clients))
        if empty and not self.closed:
            self.close("no viewer left")

    def users(self):
        with self.lock:
            return [c.user for c in self.clients]

    def close(self, reason, lost=False):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.close_reason = reason
            clients = list(self.clients)
            self.clients.clear()
        if self.on_empty:
            self.on_empty(self)
        try:
            self.up.close()
        except Exception:                            # noqa: BLE001
            pass
        # La raison d'abord, les navigateurs ensuite : à la coupure, chacun
        # demande aussitôt POURQUOI, pour savoir s'il doit se reconnecter.
        if lost and self.on_lost:
            try:
                self.on_lost(self)
            except Exception:                        # noqa: BLE001
                log.exception("console lost-callback failed")
        for c in clients:
            c.kill()


# ---------------------------------------------------------------------------
# Registre : une connexion partagée par VM
# ---------------------------------------------------------------------------

_hubs = {}
_hubs_lock = threading.Lock()
_creating = {}


def _forget(hub):
    with _hubs_lock:
        if _hubs.get(hub.key) is hub:
            _hubs.pop(hub.key, None)


def get_hub(key, dial, on_lost=None):
    """La connexion partagée de `key`, ouverte par `dial()` si besoin.

    Une seule ouverture à la fois par VM : deux navigateurs qui arrivent
    ensemble ne doivent pas ouvrir deux connexions, KubeVirt fermerait la
    première."""
    with _hubs_lock:
        hub = _hubs.get(key)
        if hub and not hub.closed:
            return hub
        lock = _creating.setdefault(key, threading.Lock())
    with lock:
        with _hubs_lock:
            hub = _hubs.get(key)
            if hub and not hub.closed:
                return hub
        upstream = dial()
        hub = VncHub(key, upstream, on_lost=on_lost, on_empty=_forget)
        try:
            hub.start()
        except Exception:
            try:
                upstream.close()
            except Exception:                        # noqa: BLE001
                pass
            raise
        with _hubs_lock:
            _hubs[key] = hub
        return hub


def attach(key, dial, ws, user="", on_lost=None):
    """Rattache un navigateur à la console partagée de la VM `key`."""
    for _ in range(2):
        hub = get_hub(key, dial, on_lost=on_lost)
        if hub.serve(ws, user):
            return
    ws.close()


def sessions():
    """{clé: [utilisateurs]} des consoles ouvertes."""
    with _hubs_lock:
        hubs = list(_hubs.values())
    return {h.key: h.users() for h in hubs if not h.closed}


def viewers_total():
    return sum(len(u) for u in sessions().values())
