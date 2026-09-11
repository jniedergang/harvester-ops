"""v1.26.0 — deux fois la même clé dans un dictionnaire i18n.

Un objet littéral JavaScript accepte une clé en double sans broncher : la
dernière écrase la première, silencieusement. Dans un fichier de 3 800
lignes édité à la main, c'est une faute facile et invisible.

Vécu en ajoutant `common.loading` : la clé existait déjà, l'ajout a été
écrasé, et l'écran affichait « Chargement... » là où l'on attendait
« Chargement de harv1… ». Aucun test ne l'a vu, la parité entre langues
étant parfaite des deux côtés.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
I18N = ROOT / "web" / "static" / "js" / "i18n.js"

LANG_HEADER_RE = re.compile(r"^\s*(en|fr|it|es|de):\s*\{")
KEY_RE = re.compile(r"^\s*'([\w.\-]+)'\s*:")


def per_language_keys():
    """Renvoie {langue: [clés dans l'ordre]}. Le même découpage que
    test_i18n, pour ne pas diverger de lui."""
    out, lang = {}, None
    for line in I18N.read_text().splitlines():
        header = LANG_HEADER_RE.match(line)
        if header:
            lang = header.group(1)
            out.setdefault(lang, [])
            continue
        if lang is None:
            continue
        m = KEY_RE.match(line)
        if m:
            out[lang].append(m.group(1))
    return out


def test_the_dictionaries_are_found():
    langs = per_language_keys()
    assert set(langs) == {"en", "fr", "it", "es", "de"}, sorted(langs)
    for lang, keys in langs.items():
        assert len(keys) > 300, f"{lang} : {len(keys)} clés, extraction cassée"


def test_no_key_is_declared_twice_in_a_language():
    problems = []
    for lang, keys in per_language_keys().items():
        seen = set()
        for key in keys:
            if key in seen:
                problems.append(f"{lang}:{key}")
            seen.add(key)
    assert not problems, (
        "clés déclarées deux fois — la seconde écrase la première sans "
        "erreur, et la traduction qu'on croit avoir posée n'est jamais "
        "affichée : " + ", ".join(sorted(problems)))
