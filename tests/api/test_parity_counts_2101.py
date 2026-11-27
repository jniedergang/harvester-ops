"""Relecture finale 2.10.1 (m6) : les compteurs de la page de parité
s'accordent en nombre (« 1 manquante », pas « 1 manquantes ») et les pages
publiées sont celles que rend le générateur."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def gen():
    s = importlib.util.spec_from_file_location("parity_gen_2101", ROOT / "tools" / "parity" / "gen.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


def _rows(gen, counts):
    return [{"s": k, "fr": "f", "en": "f", "v": "", "nfr": "", "nen": ""} for k, n in counts.items() for _ in range(n)]


@pytest.mark.parametrize("counts,want,never", [
    ({"ok": 1, "part": 1, "todo": 1, "na": 1}, ["**1 faite**", "**1 partielle**", "**1 manquante**"],
     ["1 faites", "1 partielles", "1 manquantes"]),
    ({"ok": 2, "part": 0, "todo": 3, "na": 0}, ["**2 faites**", "**0 partielle**", "**3 manquantes**"],
     ["0 partielles"]),
])
def test_french_counts_agree(gen, monkeypatch, counts, want, never):
    monkeypatch.setattr(gen.d, "S", [{"key": "x", "fr": "X", "en": "X", "hen": "", "rows": _rows(gen, counts)}])
    head = "\n".join(gen.md("fr").splitlines()[:6])
    for w in want:
        assert w in head, (w, head)
    for n in never:
        assert n not in head, (n, head)


@pytest.mark.parametrize("lang,path", [("fr", "docs/fr/parite-harvester.md"), ("en", "docs/en/harvester-parity.md")])
def test_published_pages_match_the_generator(gen, lang, path):
    assert (ROOT / path).read_text().rstrip("\n") == gen.md(lang).rstrip("\n")
