"""v1.96.0 : les champs d'une action (étiquette, cluster, état, id, erreur,
étape, message) viennent du serveur mais pas toujours de la console : une
étiquette KVM porte le nom libvirt brut d'une VM. Toute interpolation de ces
champs dans du HTML de l'onglet Activité et du dock passe par escapeHtml.

Contrôle au niveau du source (le test Playwright
tests/e2e/test_activity_escaping_196.py le vérifie dans le navigateur)."""

import re
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[2] / "web" / "static" / "js"
FIELDS = r"(action|cluster|status|id|error_summary|step_id|message|exit_code)"
OBJ = r"(a|it|live|ev|e|run)"
FIELD_RE = re.compile(rf"\b{OBJ}\.{FIELDS}\b")
INTERP_RE = re.compile(r"\$\{([^{}]*)\}")

# (fichier, début de la zone, fin de la zone) : les rendus HTML des actions
REGIONS = [
    ("app.js", "async function refreshActivity()", "async function showLogFile("),
    ("app.js", "function showActionDetails(idx)", "const title = `${it.action}"),   # titre : textContent / escapeHtml de FloatingPanels
    ("dock.js", "const live = liveSteps[a.id] ||", "const cancelBtn = card.querySelector('[data-cancel]')"),
]


def unescaped(src):
    """Interpolations `${...}` qui sortent un champ d'action sans escapeHtml.
    Ne comptent pas : une comparaison (`it.status === 'done' ? ...`), un test
    d'appartenance (`expanded.has(a.id)`), un calcul de date."""
    bad = []
    src = re.sub(r"escapeHtml\(`[^`]*`\)", "", src)   # gabarit entier échappé
    for m in INTERP_RE.finditer(src):
        expr = m.group(1)
        if "escapeHtml(" in expr:
            continue
        rest = re.sub(rf"\b{OBJ}\.{FIELDS}\s*(===|!==)", "", expr)
        rest = re.sub(r"\.has\([^)]*\)", "", rest)
        if FIELD_RE.search(rest):
            bad.append(expr.strip())
    return bad


def region(name, start, end):
    src = (JS / name).read_text()
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


@pytest.mark.parametrize("name,start,end", REGIONS, ids=[f"{r[0]}:{r[1][:30]}" for r in REGIONS])
def test_every_action_field_in_html_is_escaped(name, start, end):
    assert unescaped(region(name, start, end)) == []


def test_the_dock_step_text_is_escaped_before_it_reaches_innerhtml():
    # stepText / stepTitle sont construits hors du gabarit puis interpolés tels quels
    src = region(*REGIONS[2])
    assert "? escapeHtml(`${live.step_id}${live.message" in src
    assert ": escapeHtml(live.step_id);" in src


@pytest.mark.parametrize("snippet,flagged", [
    ("tr.innerHTML = `<td>${it.action}</td>`", True),
    ("x = `<code>${a.cluster}</code>`", True),
    ("x = `${live.step_id}${live.message ? ' - ' + live.message : ''}`", True),
    ("x = `<td>${escapeHtml(it.action)}</td>`", False),
    ("x = `class=\"${it.status === 'done' ? 'Running' : 'Failed'}\"`", False),
    ("x = `${expanded.has(a.id) ? 'Hide' : 'Show'}`", False),
    ("x = `${new Date(a.started_at * 1000).toLocaleString()}`", False),
    ("x = escapeHtml(`${live.step_id}: ${live.message}`)", False),
])
def test_the_checker_itself_flags_raw_fields(snippet, flagged):
    assert bool(unescaped(snippet)) is flagged
