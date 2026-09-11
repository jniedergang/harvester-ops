"""v1.25.0 — toute limite de débit déclarée doit être lisible par le limiteur.

Défaut trouvé à l'occasion de la mise à jour du provider : flask-limiter
IGNORE EN SILENCE une chaîne de limite qu'il ne sait pas analyser. Le point
d'entrée répond normalement, sans aucune limitation, et rien ne le signale
(pas d'exception, pas de log). Six points d'entrée mutatifs portaient ainsi
un décorateur écrit comme un nom d'action (`@_rate_limit("iso-fetch")`) et
n'étaient en réalité pas limités du tout, alors que le CLAUDE.md du projet
exige une limite sur chacun.

Ces tests verrouillent les deux moitiés du correctif :
  * toute chaîne présente dans le source s'analyse vraiment ;
  * `_rate_limit` lève sur une chaîne invalide, y compris quand la
    limitation est désactivée — sans quoi la suite de tests, qui tourne
    précisément dans ce mode, ne verrait jamais rien.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402

SPEC_RE = re.compile(r"@_rate_limit\(\s*\"([^\"]+)\"\s*\)")


def declared_specs():
    src = (ROOT / "web" / "app.py").read_text()
    return SPEC_RE.findall(src)


def test_there_are_rate_limited_endpoints():
    """Garde-fou du test lui-même : une regexp qui ne trouve plus rien
    passerait au vert sans rien vérifier."""
    assert len(declared_specs()) >= 10


def test_every_declared_spec_is_parseable():
    limits = pytest.importorskip("limits")
    bad = []
    for spec in declared_specs():
        try:
            limits.parse_many(spec)
        except ValueError:
            bad.append(spec)
    assert not bad, (
        "limites ignorées en silence par flask-limiter, donc points d'entrée "
        "non protégés : " + ", ".join(sorted(set(bad)))
    )


def test_an_invalid_spec_is_rejected_loudly():
    """C'est la garde qui empêche la régression de revenir."""
    with pytest.raises(ValueError):
        wapp._rate_limit("iso-fetch")


def test_a_valid_spec_still_decorates():
    def fn():
        return "ok"

    assert wapp._rate_limit("6/minute")(fn)() == "ok"
