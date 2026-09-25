"""harvester-ops : progression d'un transfert de VM (v1.46.0).

Le script calcule, la console relaie, l'interface traduit. Pour chaque phase
d'un transfert (gel des disques, téléchargement, import, sauvegarde,
restauration...), un `Progress` suit la quantité faite sur la quantité
totale, en déduit un débit sur une fenêtre glissante et un temps restant, et
publie au plus un point toutes les deux secondes : une ligne
`PROGRESS_EVENT|<phase>|<json>` que la console garde à part (un point par
phase, jamais dans la file des événements, plafonnée à 500), ou une ligne
lisible quand le script tourne dans un terminal.

`GzipCounter` compte un flux gzip au passage : les octets transmis (ce qui
circule vraiment) et les octets bruts qu'ils représentent (rapportés à la
taille des disques), en décompressant par tranches bornées : un disque creux
compresse ses zéros à mille pour un.

Bibliothèque standard seulement.
"""

import json
import sys
import time
import zlib
from collections import deque

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def fmt_bytes(n):
    n = float(n or 0)
    i = 0
    while n >= 1024 and i < len(_UNITS) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f} {_UNITS[i]}" if i else f"{int(n)} B"


def fmt_duration(s):
    s = int(round(s or 0))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s"
    return f"{s // 3600} h {(s % 3600) // 60} min"


class Progress:
    def __init__(self, emit, phase, total, now=time.time, items_total=None,
                 window=20.0, every=2.0):
        self.emit, self.phase, self.total, self.now = emit, phase, int(total or 0), now
        self.items_total = items_total
        self.window, self.every = window, every
        self.done = 0
        self.wire = 0
        self.item = None
        self.items_done = 0
        self.start = now()
        self._samples = deque([(self.start, 0)])
        self._last_emit = None

    # -- mesure -------------------------------------------------------------
    def _rate(self):
        t = self.now()
        # garder un point à la frontière de la fenêtre, pour mesurer dessus
        while len(self._samples) > 1 and t - self._samples[1][0] >= self.window:
            self._samples.popleft()
        t0, d0 = self._samples[0]
        dt = t - t0
        if dt <= 0:
            return None
        return max(0.0, (self.done - d0) / dt)

    def snapshot(self, final=False):
        if final:
            # un bilan donne le débit moyen de la phase, pas celui de ses
            # dernières secondes
            elapsed = self.now() - self.start
            rate = (self.done / elapsed) if elapsed > 0 else None
        else:
            rate = self._rate()
        remaining = max(0, self.total - self.done)
        if final:
            eta = 0
        elif rate:
            eta = remaining / rate
        else:
            eta = None
        return {"phase": self.phase, "item": self.item, "done": self.done,
                "total": self.total, "wire": self.wire, "rate": rate, "eta": eta,
                "elapsed": self.now() - self.start, "items_done": self.items_done,
                "items_total": self.items_total, "final": final}

    # -- mise à jour --------------------------------------------------------
    def update(self, done, wire=None, item=None, items_done=None):
        self.done = int(done)
        if wire is not None:
            self.wire = int(wire)
        if item is not None:
            self.item = item
        if items_done is not None:
            self.items_done = items_done
        t = self.now()
        self._samples.append((t, self.done))
        if self._last_emit is None or t - self._last_emit >= self.every:
            self._last_emit = t
            self.emit(self.snapshot())

    def add(self, n_raw, n_wire=0):
        self.update(self.done + n_raw, self.wire + n_wire)

    def finish(self):
        elapsed = self.now() - self.start
        self.emit(self.snapshot(final=True))
        return {"done": self.done, "wire": self.wire, "elapsed": elapsed,
                "rate": (self.done / elapsed) if elapsed > 0 else None}


def human(snap):
    """Une ligne lisible (terminal, bilan d'étape)."""
    total = snap.get("total") or 0
    unit_div = 1024 ** 3 if total >= 1024 ** 3 else 1024 ** 2
    unit = "GiB" if unit_div == 1024 ** 3 else "MiB"
    head = snap["phase"] + (f" {snap['item']}" if snap.get("item") else "")
    parts = [f"{head}: {snap['done'] / unit_div:.1f}/{total / unit_div:.1f} {unit}"]
    if snap.get("wire"):
        parts[0] += f" ({fmt_bytes(snap['wire'])} sent)"
    if snap.get("rate") is not None:
        parts.append(f"{fmt_bytes(snap['rate'])}/s")
    if not snap.get("final") and total and snap["done"] >= total:
        parts.append("target still writing")
    elif snap.get("eta") is not None and not snap.get("final"):
        parts.append(f"{fmt_duration(snap['eta'])} left")
    return ", ".join(parts)


def summary(phase, res):
    """Le bilan d'une phase, gardé comme message d'étape dans l'Activité."""
    wire = f" ({fmt_bytes(res['wire'])} sent)" if res.get("wire") else ""
    rate = f", {fmt_bytes(res['rate'])}/s average" if res.get("rate") else ""
    return f"{phase}: {fmt_bytes(res['done'])}{wire} in {fmt_duration(res['elapsed'])}{rate}"


def emit_line(snap, tty=None):
    """Publie un point : une ligne machine pour la console, ou une ligne
    lisible qui se réécrit dans un terminal."""
    err = sys.stderr
    if tty is None:
        tty = hasattr(err, "isatty") and err.isatty()
    if tty:
        end = "\n" if snap.get("final") else ""
        err.write("\r\033[K" + human(snap) + end)
    else:
        err.write(f"PROGRESS_EVENT|{snap['phase']}|{json.dumps(snap, separators=(',', ':'))}\n")
    err.flush()


class GzipCounter:
    """Compte un flux gzip : `wire` octets vus, `raw` octets qu'ils
    décompressent. Décompression par tranches de `max_chunk` au plus (des
    zéros compressés à mille pour un ne doivent pas tout allouer d'un coup)."""

    def __init__(self, max_chunk=4 * 1024 * 1024):
        self.max_chunk = max_chunk
        self.wire = 0
        self.raw = 0
        self._d = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def feed(self, chunk):
        self.wire += len(chunk)
        if self._d is None:              # pas du gzip : compté tel quel
            self.raw += len(chunk)
            return len(chunk)
        n = 0
        buf = chunk
        while buf:
            try:
                out = self._d.decompress(buf, self.max_chunk)
            except zlib.error:
                # compter n'est qu'un plus : jamais une cause d'échec
                self._d = None
                self.raw += len(chunk) - n
                return len(chunk)
            n += len(out)
            if self._d.eof:
                # membre suivant d'un gzip concaténé
                buf = self._d.unused_data
                self._d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                continue
            buf = self._d.unconsumed_tail
        self.raw += n
        return n
