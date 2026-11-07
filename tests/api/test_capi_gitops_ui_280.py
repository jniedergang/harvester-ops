"""2.8.0 (tâche 9) : contrôles au niveau du source de l'onglet GitOps
(capi-gitops.js) et des sortes gitops/rke2 de l'onglet Artefacts.

* chaque interpolation `${...}` d'un gabarit HTML passe par esc(), un appel
  de traduction, une icône ou un fragment rendu par une fonction du module ;
* chaque bouton, liste et champ porte une info-bulle (data-tip) ;
* le suivi passe par SSEReconnect.connect, jamais un EventSource brut ;
* un appel littéral de traduction par constat attendu de l'outil, présent
  dans les cinq langues.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"
SRC = (JS / "capi-gitops.js").read_text()
ART = (JS / "artefacts.js").read_text()
I18N = (JS / "i18n.js").read_text()

CODES = ["gitops-name-too-long", "gitops-source-unsupported", "gitops-flux-missing", "gitops-namespace-not-team",
         "gitops-privileges-admin-only", "gitops-fleet-admin-only", "gitops-fleet-not-confined",
         "gitops-fleet-needs-rancher", "gitops-secret-missing", "gitops-package-not-published",
         "gitops-flux-not-locked", "gitops-cluster-missing", "rancher-session-needed", "child-admin-required",
         "child-admin-unknown", "gitops-invalid"]

# une valeur venue du serveur ou de la saisie, interpolée telle quelle
# (en contenu ou en valeur d'attribut d'un gabarit HTML)
RAW = re.compile(r"(?:>|(?<!data-x)(?<!data-f)(?<!data-c)=\")\s*\$\{\s*(?:e|x|p|o|d|f|s|st|it|row|r|body|pre|src0|v|ref|c)(?:\.[\w]+)*\s*\}")
# un fragment interpolé doit être échappé, traduit, ou rendu par le module
FRAG = re.compile(r"\$\{\s*(?!esc\(|enc\(|icon\(|tr\(|btn\(|field\(|opt\()([a-zA-Z_.]+)\(")


def test_no_raw_value_interpolated_in_capi_gitops():
    assert RAW.findall(SRC) == []


def test_function_calls_interpolated_are_module_renderers():
    calls = set(FRAG.findall(SRC))
    calls = {x for x in calls if not x.endswith(".map")}     # listes rendues par opt()/esc()
    assert calls <= {"findingsHtml", "stateBadge", "originHtml", "toolLabel"}, calls
    # toolLabel rend du texte : toujours sous esc()
    assert "${toolLabel(" not in SRC


def test_hostile_fields_are_escaped_where_shown():
    for x in ("esc(e.entry)", "esc(ref)", "esc(e.name)", "esc(o.plan)", "esc(o.run)", "esc(findingText(f))"):
        assert x in SRC, x


@pytest.mark.parametrize("code", CODES)
def test_each_finding_has_a_literal_translation_in_five_languages(code):
    key = f"gop.fd.{code}"
    assert re.search(rf"'{re.escape(code)}': \(v\) => tr\('{re.escape(key)}'", SRC), code
    assert I18N.count(f'"{key}"') + I18N.count(f"'{key}'") == 5, key


def test_controls_carry_a_tooltip():
    # boutons rendus par btn() ou explicitement avec data-tip ; champs par field()
    for m in re.finditer(r"<button[^>]*>", SRC):
        tag = m.group(0)
        assert "data-tip" in tag or "data-gop-go" in tag, tag      # Appliquer : info-bulle sur son enveloppe
    assert 'data-x="go-wrap" data-tip=' in SRC
    for m in re.finditer(r"<(input|select|textarea)[^>]*>", SRC):
        line = SRC[SRC.rfind("\n", 0, m.start()):m.end()]
        assert "field(" in line or "data-tip" in line, line


def test_sse_only_through_the_reconnecting_helper():
    assert "new EventSource(" not in SRC
    assert "SSEReconnect.connect(" in SRC


def test_apply_waits_for_a_check_of_the_current_values():
    # chaque saisie grise Appliquer et relance la vérification (séquence)
    later = SRC[SRC.index("function later("):SRC.index("m._cleanup =")]
    assert "state.n++" in later and "setGo(false" in later
    run = SRC[SRC.index("async function runChecks()"):SRC.index("function later(")]
    assert "n !== state.n" in run and "dry_run: true" in run
    assert "secretsOf(false)" in run                       # les valeurs ne partent pas à la vérification


def test_credentials_cleared_after_sending():
    click = SRC[SRC.index("go.addEventListener('click'"):]
    assert click.index("secretsOf(true)") < click.index("clearSecrets()") < click.index("await json('POST'")


def test_artefacts_kinds_gitops_and_rke2_are_explained_and_gitops_opens_the_window():
    assert "tr('art.kind.t.gitops'" in ART and "tr('art.kind.t.rke2'" in ART
    assert "CapiGitops.openAdd({ source: { type: 'oci'" in ART
    for k in ("art.kind.t.gitops", "art.kind.t.rke2", "art.gop.t.open", "art.gop.open"):
        assert I18N.count(f'"{k}"') == 5, k
