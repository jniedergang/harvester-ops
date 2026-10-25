"""Préchauffage du relevé de stockage KVM (1.101.2).

Le premier catalogue d'images après le démarrage de la console, ou après une
écriture, attendait un relevé entier (24 s sur node2 avant les lots en
parallèle). `SurveyCache.warm` le lance en arrière-plan quand une vue KVM est
relue ; un `get` qui arrive pendant ce relevé l'attend au lieu d'en refaire
un. Jamais de minuterie : sans personne qui regarde, rien ne part."""

import contextlib
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import kvm_ops as ko  # noqa: E402

from test_kvm_routes_196 import client, env, wapp  # noqa: E402,F401 - fixtures


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def host(name="node2"):
    return SimpleNamespace(name=name, decl={"address": "10.0.0.1", "uri": "qemu:///system"}, state=None)


def cache(clock, factory=None, spawn=None, enabled=True):
    made = []

    def make(h):
        made.append(h.name)
        return f"survey-{len(made)}"
    spawned = []
    c = ko.SurveyCache(ttl=10, max_stale=60, clock=clock, enabled=enabled,
                       spawn=spawn or spawned.append, factory=factory or make)
    return c, made, spawned


@pytest.mark.parametrize("case,expect_spawn", [
    ("empty", True),            # rien en cache : relevé lancé
    ("fresh", False),           # entrée fraîche : rien
    ("stale", True),            # entrée périmée : relevé lancé
    ("warming", False),         # déjà en route : un seul
    ("disabled", False),        # cache coupé : rien
])
def test_warm_starts_only_when_useful(case, expect_spawn):
    clock = Clock()
    c, made, spawned = cache(clock, enabled=case != "disabled")
    h = host()
    if case in ("fresh", "stale"):
        c.get(h)
        clock.t += 1 if case == "fresh" else 30
    if case == "warming":
        assert c.warm(h) is True
    started = c.warm(h)
    assert started is expect_spawn
    assert len(spawned) == (1 if case == "warming" else int(expect_spawn))


def test_warm_fills_the_cache_and_get_uses_it():
    clock = Clock()
    c, made, spawned = cache(clock)
    h = host()
    assert c.warm(h) and made == []
    spawned.pop()()
    assert made == ["node2"]
    assert c.get(h) == ("survey-1", 0.0) and made == ["node2"]      # pas de second relevé


def test_a_get_during_a_warm_up_waits_for_it():
    gate, entered = threading.Event(), threading.Event()
    made = []

    def slow(h):
        entered.set()
        gate.wait(5)
        made.append(h.name)
        return "warm"
    c = ko.SurveyCache(ttl=10, enabled=True, factory=slow,
                       spawn=lambda fn: threading.Thread(target=fn, daemon=True).start())
    h = host()
    assert c.warm(h)
    assert entered.wait(5)
    got = []
    t = threading.Thread(target=lambda: got.append(c.get(h)))
    t.start()
    time.sleep(0.2)
    assert got == []                                                  # il attend
    gate.set()
    t.join(5)
    assert got and got[0][0] == "warm" and made == ["node2"]          # un seul relevé


def test_a_busy_slot_skips_the_warm_up():
    clock = Clock()
    c, made, spawned = cache(clock)
    h = host()
    assert c.warm(h, slot=lambda: contextlib.nullcontext(False))
    spawned.pop()()
    assert made == []                                                 # rien relevé
    assert c.warm(h, slot=lambda: contextlib.nullcontext(True))       # libéré : relançable
    spawned.pop()()
    assert made == ["node2"]


def test_a_write_during_a_warm_up_discards_it():
    clock = Clock()
    c, made, spawned = cache(clock)
    h = host()
    c.warm(h)
    c.invalidate(h)                                                   # écriture de la console
    spawned.pop()()
    assert c.get(h) == ("survey-2", 0.0)                              # le préchauffé est jeté


def test_a_failing_warm_up_leaves_no_trace():
    clock = Clock()

    def boom(h):
        raise RuntimeError("ssh down")
    c, _, spawned = cache(clock, factory=boom)
    h = host()
    assert c.warm(h)
    spawned.pop()()
    assert c.warm(h)                                                  # pas bloqué « en route »


# --- routes : préchauffage quand une vue KVM est relue -------------------------------

@pytest.fixture
def warmed(monkeypatch):
    seen = []
    monkeypatch.setattr(wapp._ko, "SURVEYS", SimpleNamespace(
        warm=lambda vh, slot=None: seen.append((vh.name, slot is not None))))
    return seen


@pytest.mark.parametrize("url,expect", [
    ("/api/kvm/vms", [("node2", True)]),                 # ghost injoignable : rien
    ("/api/kvm/hosts", [("node2", True)]),
    ("/api/kvm/hosts/node2", [("node2", True)]),
    ("/api/kvm/hosts/ghost", []),
    ("/api/kvm/hosts?declared=1", []),                   # aucun hôte interrogé : rien
])
def test_views_warm_the_hosts_that_answered(client, warmed, url, expect):
    client.get(url)
    assert warmed == expect
