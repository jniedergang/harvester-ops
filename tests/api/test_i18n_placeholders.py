"""v1.44.12 : un texte traduit qui attend une valeur la reçoit.

Relevé sur la page GitHub du projet : l'aperçu animé du README montrait
« Loading {name}'s topology… », marqueur compris. Quatre vues et le
pré-contrôle de maintenance appelaient `tr('topology.loading')` sans jamais
passer le nom du cluster, et `Board.tr` ne savait pas en recevoir.

Un texte à marqueur est bien servi s'il reçoit ses valeurs dans l'appel,
s'il passe par `fill(...)` ou `.replace(...)`, ou s'il est le message d'un
voile de chargement (qui remplit `{name}` lui-même).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"


def _english_placeholder_keys():
    text = (JS / "i18n.js").read_text()
    en = text[:text.index("  fr: {")]
    keys = set()
    for m in re.finditer(r"^\s*'([\w.]+)':\s*'((?:[^'\\]|\\.)*)'", en, re.M):
        if re.search(r"\{\w+\}", m.group(2)):
            keys.add(m.group(1))
    return keys


CALL = re.compile(r"\b(tr|i18n\.t)\(\s*'([\w.]+)'\s*(,\s*'(?:[^'\\]|\\.)*'\s*)?\)")


def test_no_placeholder_text_is_shown_without_its_values():
    keys = _english_placeholder_keys()
    assert "topology.loading" in keys
    bad = []
    for f in sorted(JS.glob("*.js")):
        if f.name == "i18n.js":
            continue
        lines = f.read_text().splitlines()
        for i, line in enumerate(lines):
            for m in CALL.finditer(line):
                if m.group(2) not in keys:
                    continue
                before, after = line[:m.start()], line[m.end():]
                # `fill(` ouvert sur la ligne ou juste avant (ternaire :
                # fill(cond ? tr(a) : tr(b), valeurs)).
                if "fill(" in " ".join(lines[max(0, i - 2):i]) + before:
                    continue
                if after.lstrip().startswith(".replace(") or (
                        not after.strip() and i + 1 < len(lines)
                        and lines[i + 1].lstrip().startswith(".replace(")):
                    continue
                window = " ".join(lines[i:i + 3])
                if "message:" in before and re.search(r"\bname\b\s*[:,]", window):
                    continue
                bad.append(f"{f.name}:{i + 1}: {m.group(0)}")
    assert not bad, "texte à marqueur affiché sans valeur :\n" + "\n".join(bad)


def test_board_tr_passes_the_values_on():
    board = (JS / "board.js").read_text()
    assert "const tr = (k, f, vars) =>" in board
    assert "i18n.t(k, vars)" in board
