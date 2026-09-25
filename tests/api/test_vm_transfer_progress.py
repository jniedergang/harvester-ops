"""v1.46.0 : le calcul de la progression d'un transfert.

Débit sur une fenêtre glissante, temps restant, étranglement des émissions
(la console garde un point par phase, mais un transfert de plusieurs heures
ne doit pas inonder le flux), bilan de fin de phase, et comptage des octets
d'un flux gzip : bruts (ce qui est transféré, rapporté à la taille des
disques) et transmis (ce qui circule vraiment).
"""

import gzip
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import vm_transfer_progress as vp  # noqa: E402

MIB = 1024 ** 2
GIB = 1024 ** 3


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t


def make(total=10 * GIB, **kw):
    c = Clock()
    out = []
    p = vp.Progress(out.append, "download", total, now=c.now, **kw)
    return p, c, out


def test_rate_and_eta_over_the_window():
    p, c, out = make()
    assert p.snapshot()["rate"] is None and p.snapshot()["eta"] is None
    for _ in range(10):
        c.t += 2
        p.update(p.done + 200 * MIB)             # 100 Mio/s
    snap = out[-1]
    assert abs(snap["rate"] - 100 * MIB) < 1
    assert snap["done"] == 2000 * MIB and snap["total"] == 10 * GIB
    assert abs(snap["eta"] - (10 * GIB - 2000 * MIB) / (100 * MIB)) < 0.01
    assert snap["elapsed"] == 20 and snap["final"] is False


def test_the_window_forgets_the_past():
    """Un débit qui chute se voit dans les 20 s, pas noyé dans la moyenne."""
    p, c, out = make(window=20.0)
    for _ in range(30):
        c.t += 2
        p.update(p.done + 200 * MIB)             # 100 Mio/s pendant 60 s
    for _ in range(15):
        c.t += 2
        p.update(p.done + 20 * MIB)              # puis 10 Mio/s
    assert abs(out[-1]["rate"] - 10 * MIB) < 1


def test_a_stall_has_no_eta():
    p, c, out = make()
    c.t += 2
    p.update(100 * MIB)
    c.t += 30
    p.update(100 * MIB)
    assert out[-1]["rate"] == 0 and out[-1]["eta"] is None


def test_emissions_are_throttled_to_every_two_seconds():
    p, c, out = make()
    for _ in range(100):
        c.t += 0.1
        p.add(MIB)
    # 10 s au total : une émission toutes les 2 s au plus
    assert 5 <= len(out) <= 6
    assert p.done == 100 * MIB


def test_finish_always_emits_and_sums_up():
    p, c, out = make(total=GIB)
    c.t += 1
    p.add(GIB // 2, 10 * MIB)
    c.t += 1
    p.add(GIB // 2, 10 * MIB)
    n = len(out)
    res = p.finish()
    assert len(out) == n + 1 and out[-1]["final"] is True and out[-1]["eta"] == 0
    assert res == {"done": GIB, "wire": 20 * MIB, "elapsed": 2.0, "rate": GIB / 2}
    s = vp.summary("download", res)
    assert s == "download: 1.0 GiB (20.0 MiB sent) in 2 s, 512.0 MiB/s average"


def test_items_and_item_are_carried():
    p, c, out = make(items_total=2)
    c.t += 3
    p.update(GIB, item="disk-1", items_done=1)
    assert out[-1]["item"] == "disk-1" and out[-1]["items_done"] == 1 and out[-1]["items_total"] == 2


def test_human_line():
    snap = {"phase": "import", "item": "disk-0", "done": 3 * GIB + GIB // 5, "total": 10 * GIB,
            "wire": 512 * MIB, "rate": 85 * MIB, "eta": 90, "elapsed": 40,
            "items_done": 0, "items_total": 2, "final": False}
    assert vp.human(snap) == "import disk-0: 3.2/10.0 GiB (512.0 MiB sent), 85.0 MiB/s, 1 min 30 s left"


def test_fmt_duration():
    assert vp.fmt_duration(5) == "5 s"
    assert vp.fmt_duration(125) == "2 min 5 s"
    assert vp.fmt_duration(3 * 3600 + 120) == "3 h 2 min"


def test_emit_line_is_one_parsable_line(capsys):
    vp.emit_line({"phase": "backup", "done": 1, "total": 2}, tty=False)
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    tag, phase, payload = err.rstrip("\n").split("|", 2)
    assert (tag, phase) == ("PROGRESS_EVENT", "backup")
    assert json.loads(payload) == {"phase": "backup", "done": 1, "total": 2}


def test_gzip_counter_counts_raw_and_wire():
    raw = os.urandom(3 * MIB) + b"\0" * (40 * MIB)   # comme un disque creux
    gz = gzip.compress(raw, compresslevel=6)
    c = vp.GzipCounter()
    total = 0
    for i in range(0, len(gz), 65536):
        total += c.feed(gz[i:i + 65536])
    assert total == len(raw) == c.raw
    assert c.wire == len(gz) < len(raw) // 5


def test_gzip_counter_keeps_memory_bounded_on_zeros():
    """40 Mio de zéros tiennent dans quelques dizaines de kilo-octets de gzip :
    décompresser un morceau d'un coup allouerait tout. On décompresse par
    tranches bornées."""
    gz = gzip.compress(b"\0" * (200 * MIB), compresslevel=6)
    c = vp.GzipCounter(max_chunk=4 * MIB)
    assert c.feed(gz) == 200 * MIB


def test_gzip_counter_handles_concatenated_members():
    gz = gzip.compress(b"a" * 1000) + gzip.compress(b"b" * 500)
    c = vp.GzipCounter()
    assert c.feed(gz) == 1500
