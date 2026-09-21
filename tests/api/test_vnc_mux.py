"""v1.41.0 : la console VNC partagée, sans navigateur ni cluster.

KubeVirt n'accepte qu'une connexion VNC par VM et ferme la précédente quand
une autre arrive : deux exploitants sur la même VM s'éjectaient l'un l'autre
toutes les secondes. `vnc_mux` tient une seule connexion par VM et la
partage. Ces tests le font parler à un faux QEMU et à de faux navigateurs,
octet par octet.
"""

import queue
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "web"))
import vnc_mux as vm  # noqa: E402


# ---------------------------------------------------------------------------
# Doublures
# ---------------------------------------------------------------------------

class FakeWs:
    """Un websocket : ce qu'on lui injecte sort par receive(), ce qu'on lui
    envoie s'accumule dans `sent`."""

    def __init__(self):
        self.inbox = queue.Queue()
        self.sent = bytearray()
        self.sent_lock = threading.Lock()
        self.connected = True

    def feed(self, data):
        self.inbox.put(bytes(data))

    def receive(self, timeout=None):
        if not self.connected:
            return None
        try:
            item = self.inbox.get(timeout=min(timeout or 0.05, 0.05))
        except queue.Empty:
            return None
        if item is None:
            self.connected = False
            return None
        return item

    def send(self, data):
        if not self.connected:
            raise ConnectionError("closed")
        with self.sent_lock:
            self.sent += data

    def close(self, *a, **k):
        self.connected = False
        self.inbox.put(None)

    def take(self):
        with self.sent_lock:
            out = bytes(self.sent)
            self.sent.clear()
        return out


def server_init(w=800, h=600, name=b"vm"):
    pf = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
    return struct.pack(">HH", w, h) + pf + struct.pack(">I", len(name)) + name


def fake_qemu(w=800, h=600):
    up = FakeWs()
    up.feed(b"RFB 003.008\n")
    up.feed(b"\x01\x01")                 # une sécurité : None
    up.feed(b"\x00\x00\x00\x00")         # SecurityResult OK
    up.feed(server_init(w, h))
    return up


def rect(x, y, w, h, enc, payload=b""):
    return struct.pack(">HHHHi", x, y, w, h, enc) + payload


def update(*rects, count=None):
    return struct.pack(">BxH", 0, len(rects) if count is None else count) + b"".join(rects)


def raw_rect(w=2, h=2):
    return rect(0, 0, w, h, vm.ENC_RAW, b"\x11" * (w * h * 4))


def hextile_rect():
    """Un rectangle de 20x20 : quatre tuiles, un sous-encodage de chaque sorte."""
    tiles = [
        bytes([1]) + b"\x22" * (16 * 16 * 4),                   # Raw
        bytes([2]) + b"\x00\x00\xff\x00",                        # fond seul
        bytes([2 | 8]) + b"\x01\x02\x03\x00" + bytes([2]) + b"\x00\x11" * 2,   # sous-rect. monochromes
        bytes([8 | 16]) + bytes([1]) + b"\x09\x09\x09\x00\x00\x11",           # colorés
    ]
    return rect(0, 0, 20, 20, vm.ENC_HEXTILE, b"".join(tiles))


def cursor_rect(w=2, h=2):
    return rect(1, 1, w, h, vm.PS_CURSOR, b"\x33" * (w * h * 4) + b"\x80" * (((w + 7) // 8) * h))


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def browser_handshake(ws):
    ws.feed(b"RFB 003.008\n")
    ws.feed(bytes([vm.SEC_NONE]))
    ws.feed(b"\x01")                     # ClientInit, partagé


HANDSHAKE_LEN = 12 + 2 + 4               # version, sécurités, résultat


@pytest.fixture(autouse=True)
def _clean_registry():
    vm._hubs.clear()
    vm._creating.clear()
    yield
    for h in list(vm._hubs.values()):
        h.close("test end")
    vm._hubs.clear()


# ---------------------------------------------------------------------------
# Analyse des messages du serveur
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("msg", [
    update(raw_rect()),
    update(hextile_rect()),
    update(rect(0, 0, 4, 4, vm.ENC_COPYRECT, b"\x00\x01\x00\x02")),
    update(rect(0, 0, 1024, 768, vm.PS_DESKTOP_SIZE)),
    update(cursor_rect()),
    update(rect(0, 0, 0, 0, vm.PS_QEMU_EXT_KEY)),
    update(raw_rect(), hextile_rect(), cursor_rect()),
    update(raw_rect(), rect(0, 0, 0, 0, vm.PS_LAST_RECT), count=0xFFFF),
    b"\x02",                                                      # Bell
    b"\x03\x00\x00\x00" + struct.pack(">I", 5) + b"hello",         # ServerCutText
    b"\x01\x00" + struct.pack(">HH", 0, 2) + b"\x00" * 12,          # colour map
])
def test_a_server_message_is_measured_exactly_at_every_split(msg):
    """Le flux arrive découpé n'importe où : la longueur n'est connue qu'une
    fois le message complet, jamais avant, et jamais fausse."""
    trailing = b"\x02"                          # un message suivant (Bell)
    for cut in range(len(msg)):
        with pytest.raises(vm.Incomplete) as e:
            vm.parse_server_message(msg[:cut])
        assert e.value.need <= len(msg)
    end, _ = vm.parse_server_message(msg + trailing)
    assert end == len(msg)


def test_events_say_what_a_newcomer_must_be_replayed():
    msg = update(rect(0, 0, 1280, 800, vm.PS_DESKTOP_SIZE), cursor_rect(),
                 rect(0, 0, 0, 0, vm.PS_QEMU_EXT_KEY))
    _, events = vm.parse_server_message(msg)
    kinds = [e[0] for e in events]
    assert kinds == ["size", "cursor", "extkey"]
    assert events[0][1:] == (1280, 800)
    _, start, end = events[1]
    assert msg[start:end] == cursor_rect()


def test_a_stateful_encoding_is_refused_not_misread():
    """Tight ou ZRLE : on ne saurait pas où finit le message, et un
    arrivant ne pourrait pas le décoder. Mieux vaut couper que corrompre."""
    with pytest.raises(vm.ProtocolError):
        vm.parse_server_message(update(rect(0, 0, 8, 8, 7, b"\x00" * 20)))


def test_the_upstream_encodings_are_all_stateless():
    stateful = {6, 7, 16, -260, 21, 50}         # zlib, Tight, ZRLE, TightPNG, JPEG, H.264
    assert not stateful & set(vm.UPSTREAM_ENCODINGS)
    assert vm.UPSTREAM_ENCODINGS[0] == vm.ENC_HEXTILE
    # L'extension clavier QEMU : sans elle, un clavier AZERTY passe par une
    # table américaine.
    assert vm.PS_QEMU_EXT_KEY in vm.UPSTREAM_ENCODINGS


# ---------------------------------------------------------------------------
# Analyse des messages du navigateur
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("msg,action", [
    (b"\x00" + b"\x00" * 19, vm.DROP),                                  # SetPixelFormat
    (b"\x02\x00" + struct.pack(">H", 2) + struct.pack(">ii", 7, 16), vm.DROP),
    (vm.full_update_request(800, 600), vm.FORWARD),
    (b"\x04\x01\x00\x00" + struct.pack(">I", 0x61), vm.FORWARD),         # KeyEvent
    (b"\x05\x01" + struct.pack(">HH", 10, 20), vm.FORWARD),             # PointerEvent
    (b"\x06\x00\x00\x00" + struct.pack(">I", 3) + b"abc", vm.FORWARD),  # ClientCutText
    (b"\xff\x00\x00\x01" + struct.pack(">II", 0x61, 0x10), vm.FORWARD),  # touche QEMU
])
def test_a_client_message_is_measured_and_sorted(msg, action):
    for cut in range(len(msg)):
        with pytest.raises(vm.Incomplete):
            vm.parse_client_message(msg[:cut])
    assert vm.parse_client_message(msg + b"\x05") == (len(msg), action)


def test_an_unknown_client_message_ends_that_client_only():
    with pytest.raises(vm.ProtocolError):
        vm.parse_client_message(b"\x96" + b"\x00" * 9)   # EnableContinuousUpdates


# ---------------------------------------------------------------------------
# La connexion partagée
# ---------------------------------------------------------------------------

def open_hub(up=None):
    up = up or fake_qemu()
    dialed = []

    def dial():
        dialed.append(1)
        return up
    hub = vm.get_hub(("c", "ns", "vm1"), dial)
    return hub, up, dialed


def run_browser(hub, user="alice"):
    ws = FakeWs()
    browser_handshake(ws)
    t = threading.Thread(target=hub.serve, args=(ws, user), daemon=True)
    t.start()
    return ws, t


def test_the_upstream_handshake_fixes_format_and_encodings():
    hub, up, _ = open_hub()
    sent = up.take()
    assert sent.startswith(b"RFB 003.008\n" + bytes([vm.SEC_NONE]) + b"\x01")
    rest = sent[14:]
    assert rest[:20] == b"\x00\x00\x00\x00" + vm.PIXEL_FORMAT
    n = struct.unpack_from(">H", rest, 22)[0]
    encs = struct.unpack_from(">" + "i" * n, rest, 24)
    assert rest[20] == 2 and encs == vm.UPSTREAM_ENCODINGS
    assert (hub.width, hub.height) == (800, 600)


def test_two_browsers_share_one_upstream_connection():
    """Le cœur du correctif : deux navigateurs, UNE connexion à KubeVirt."""
    hub, up, dialed = open_hub()
    a, _ = run_browser(hub, "alice")
    assert wait_for(lambda: len(hub.clients) == 1)
    hub2 = vm.get_hub(("c", "ns", "vm1"), lambda: (_ for _ in ()).throw(AssertionError("second dial")))
    assert hub2 is hub
    b, _ = run_browser(hub, "bob")
    assert wait_for(lambda: len(hub.clients) == 2)
    assert dialed == [1]
    assert sorted(hub.users()) == ["alice", "bob"]
    a.take(); b.take()
    msg = update(raw_rect())
    up.feed(msg[:7]); up.feed(msg[7:])
    assert wait_for(lambda: a.sent.endswith(msg) and b.sent.endswith(msg))


def test_each_browser_gets_a_proper_handshake():
    hub, up, _ = open_hub()
    a, _ = run_browser(hub)
    assert wait_for(lambda: len(a.sent) >= HANDSHAKE_LEN + 24)
    got = a.take()
    assert got[:12] == vm.VERSION
    assert got[12:14] == bytes([1, vm.SEC_NONE])
    assert got[14:18] == b"\x00\x00\x00\x00"
    w, h = struct.unpack_from(">HH", got, 18)
    assert (w, h) == (800, 600)
    # Le format annoncé est celui qu'on a imposé en amont, pas celui de QEMU.
    assert got[22:38] == vm.PIXEL_FORMAT


def test_input_from_every_browser_reaches_the_vm_and_settings_do_not():
    hub, up, _ = open_hub()
    a, _ = run_browser(hub, "alice")
    b, _ = run_browser(hub, "bob")
    assert wait_for(lambda: len(hub.clients) == 2)
    up.take()
    key_a = b"\x04\x01\x00\x00" + struct.pack(">I", 0x61)
    key_b = b"\xff\x00\x00\x01" + struct.pack(">II", 0x62, 0x30)
    set_enc = b"\x02\x00" + struct.pack(">H", 1) + struct.pack(">i", 7)
    a.feed(set_enc + key_a[:3]); a.feed(key_a[3:])
    b.feed(key_b)
    assert wait_for(lambda: key_a in up.sent and key_b in up.sent)
    assert set_enc not in up.sent, "un navigateur a changé les encodages de tous"


def test_a_newcomer_is_replayed_size_cursor_and_keyboard():
    """Ce qui ne s'envoie qu'une fois doit être rejoué à l'arrivant, sinon il
    n'a ni la bonne taille, ni le curseur, ni le clavier AZERTY."""
    hub, up, _ = open_hub()
    a, _ = run_browser(hub, "alice")
    assert wait_for(lambda: len(hub.clients) == 1)
    up.feed(update(rect(0, 0, 1280, 800, vm.PS_DESKTOP_SIZE), cursor_rect(),
                   rect(0, 0, 0, 0, vm.PS_QEMU_EXT_KEY)))
    assert wait_for(lambda: hub.ext_key and hub.width == 1280)
    up.take()
    b, _ = run_browser(hub, "bob")
    assert wait_for(lambda: len(hub.clients) == 2)
    got = b.take()
    after = got[HANDSHAKE_LEN:]
    assert struct.unpack_from(">HH", after, 0) == (1280, 800)
    init_len = 4 + 16 + 4 + len(hub.name)
    replay = after[init_len:]
    end, events = vm.parse_server_message(replay)
    assert [e[0] for e in events] == ["cursor", "extkey"]
    # Et une image complète est demandée pour lui.
    assert wait_for(lambda: vm.full_update_request(1280, 800) in up.sent)


def test_a_newcomer_starts_on_a_message_boundary():
    """Arrivé au milieu d'un rectangle, il ne pourrait rien décoder."""
    hub, up, _ = open_hub()
    a, _ = run_browser(hub, "alice")
    assert wait_for(lambda: len(hub.clients) == 1)
    big = update(hextile_rect(), raw_rect(8, 8))
    up.feed(big[:30])
    time.sleep(0.1)
    b, _ = run_browser(hub, "bob")
    assert wait_for(lambda: len(hub.clients) == 2)
    up.feed(big[30:])
    up.feed(update(raw_rect()))
    assert wait_for(lambda: b.sent.endswith(update(raw_rect())))
    stream = b.take()[HANDSHAKE_LEN + 4 + 16 + 4 + len(hub.name):]
    pos = 0
    while pos < len(stream):                    # tout se relit message par message
        end, _ = vm.parse_server_message(stream[pos:])
        pos += end
    assert pos == len(stream)


def test_the_last_viewer_leaving_closes_the_upstream():
    """Garder la connexion ouverte sans personne tiendrait la VM occupée : la
    console d'Harvester elle-même ne pourrait plus s'y attacher."""
    hub, up, _ = open_hub()
    a, t = run_browser(hub)
    assert wait_for(lambda: len(hub.clients) == 1)
    a.close()
    t.join(timeout=3)
    assert hub.closed and not up.connected
    assert vm._hubs == {}


def test_losing_the_upstream_closes_every_browser_and_says_so():
    lost = []
    up = fake_qemu()
    hub = vm.get_hub(("c", "ns", "vm1"), lambda: up, on_lost=lost.append)
    a, _ = run_browser(hub, "alice")
    b, _ = run_browser(hub, "bob")
    assert wait_for(lambda: len(hub.clients) == 2)
    up.close()                                   # KubeVirt coupe
    assert wait_for(lambda: hub.closed)
    assert lost == [hub]
    assert wait_for(lambda: not a.connected and not b.connected)


def test_leaving_on_our_own_is_not_reported_as_lost():
    lost = []
    up = fake_qemu()
    hub = vm.get_hub(("c", "ns", "vm1"), lambda: up, on_lost=lost.append)
    a, t = run_browser(hub)
    assert wait_for(lambda: len(hub.clients) == 1)
    a.close(); t.join(timeout=3)
    time.sleep(0.2)
    assert lost == []


def test_two_simultaneous_arrivals_dial_only_once():
    ups = []

    def dial():
        time.sleep(0.2)                         # une ouverture lente
        ups.append(fake_qemu())
        return ups[-1]
    hubs = []
    ts = [threading.Thread(target=lambda: hubs.append(vm.get_hub(("c", "ns", "vm1"), dial)))
          for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ups) == 1 and len({id(h) for h in hubs}) == 1


def test_a_slow_browser_is_dropped_not_hoarded(monkeypatch):
    monkeypatch.setattr(vm, "CLIENT_QUEUE_LIMIT", 1000)
    hub, up, _ = open_hub()
    a, _ = run_browser(hub, "alice")
    stuck, _ = run_browser(hub, "stuck")
    assert wait_for(lambda: len(hub.clients) == 2)
    client = next(c for c in hub.clients if c.user == "stuck")
    gate = threading.Event()
    stuck.send = lambda data: gate.wait(5)      # ne rend jamais la main
    for _ in range(6):
        up.feed(update(raw_rect(8, 8)))          # 272 octets chacun
        time.sleep(0.05)                         # alice a le temps de vider
    assert wait_for(lambda: not client.alive)
    assert wait_for(lambda: [c.user for c in hub.clients] == ["alice"])
    gate.set()


def test_attach_opens_a_fresh_connection_after_the_last_one_closed():
    ups = []

    def dial():
        ups.append(fake_qemu())
        return ups[-1]
    key = ("c", "ns", "vm1")
    ws1 = FakeWs(); browser_handshake(ws1)
    t1 = threading.Thread(target=vm.attach, args=(key, dial, ws1, "a"), daemon=True)
    t1.start()
    assert wait_for(lambda: vm.sessions().get(key) == ["a"])
    ws1.close(); t1.join(timeout=3)
    ws2 = FakeWs(); browser_handshake(ws2)
    t2 = threading.Thread(target=vm.attach, args=(key, dial, ws2, "b"), daemon=True)
    t2.start()
    assert wait_for(lambda: vm.sessions().get(key) == ["b"])
    assert len(ups) == 2
    ws2.close(); t2.join(timeout=3)


def test_the_reason_is_known_before_browsers_are_told():
    """À la coupure, chaque console demande aussitôt POURQUOI pour décider
    de se reconnecter : la raison doit être enregistrée avant."""
    seen = []
    up = fake_qemu()

    def lost(hub):
        seen.append([ws.connected for ws in browsers])
    hub = vm.get_hub(("c", "ns", "vm1"), lambda: up, on_lost=lost)
    browsers = [run_browser(hub, u)[0] for u in ("alice", "bob")]
    assert wait_for(lambda: len(hub.clients) == 2)
    up.close()
    assert wait_for(lambda: seen)
    assert seen[0] == [True, True], "les navigateurs étaient déjà fermés"
