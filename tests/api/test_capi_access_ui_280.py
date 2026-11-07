"""2.8.0 (tâche 9) : contrôles au niveau du source des fenêtres Accès et
Renouveler (capi.js) et du menu Accès de la création (capi-create.js).

* chaque interpolation d'une valeur venue du serveur passe par esc() ;
* chaque contrôle porte une info-bulle ; un bouton qui écrit est grisé par
  `gate`, raison sur son enveloppe ;
* le suivi passe par SSEReconnect.connect ;
* un appel littéral de traduction par constat ou refus, dans les cinq
  langues ;
* Rancher externe grisé sans session, Renouveler grisé sans différence
  (formes vérifiées dans le source, la conduite l'est en e2e).
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"
CAPI = (JS / "capi.js").read_text()
CREATE = (JS / "capi-create.js").read_text()
I18N = (JS / "i18n.js").read_text()


def block(src, start, end):
    return src[src.index(start):src.index(end, src.index(start))]


ACCESS = block(CAPI, "async function openAccess(", "// --- Renouveler les machines")
RENEW = block(CAPI, "// --- Renouveler les machines", "* Rejoue l'activation du sous-onglet courant")
CREATE_ACCESS = block(CREATE, "// --- accès humain au cluster enfant", "// Comportement")

# valeur du serveur ou de la saisie, interpolée telle quelle dans du HTML
RAW = re.compile(r"(?:>|(?<!data-x)(?<!data-w-box)(?<!data-win-pane)=\")\s*\$\{\s*(?:row|o|d|dt|rs|c|f|x|r|session|cur)(?:\.[\w]+|\[[^\]]+\])*\s*\}")


@pytest.mark.parametrize("name,src", [("access", ACCESS), ("renew", RENEW), ("create-access", CREATE_ACCESS)])
def test_no_raw_value_interpolated(name, src):
    assert RAW.findall(src) == [], name


def test_hostile_fields_are_escaped_where_shown():
    for x in ("esc(row.rancher)", "esc(row.name)", "esc(row.cid)", "esc(row.state || ", "esc(run)", "esc(o[k])",
              "esc(session)"):
        assert x in ACCESS, x
    for x in ("esc(c.template)", "esc(x)", "esc(renewErrorText(d))"):
        assert x in RENEW, x


@pytest.mark.parametrize("name,src", [("access", ACCESS), ("renew", RENEW), ("create-access", CREATE_ACCESS)])
def test_controls_carry_a_tooltip(name, src):
    for m in re.finditer(r"<(button|input|select|textarea|label)[^>]*>", src):
        tag = m.group(0)
        if tag.startswith("<label") and "tf-type-bool" not in tag:
            continue
        line = src[src.rfind("\n", 0, m.start()):m.end()]
        # bouton qui écrit : info-bulle sur son enveloppe (gate), voisine
        wrapped = re.search(r'<span class="tip" data-x="[\w-]+-wrap">\s*$', src[max(0, m.start() - 60):m.start()])
        radio_in_label = tag.startswith("<input") and ('type="radio"' in tag or 'type="checkbox"' in tag)
        assert "data-tip" in line or wrapped or radio_in_label, (name, tag)


def test_writing_buttons_are_gated_until_the_check():
    assert "function gate(wrap, btn, ok, why)" in CAPI
    for x in ("gate(q('import-wrap')", "gate(q('unimport-wrap')"):
        assert x in ACCESS
    go = block(RENEW, "function paintGo()", "function paintDiff()")
    # frais (paramètres de la différence), sans blocage, une différence, confirmé
    assert "st.sig === JSON.stringify(body())" in go
    assert "(d.changes || []).length || d.secrets_changed" in go
    assert "q('understand').checked" in go
    later = block(RENEW, "const later = () =>", "root.querySelectorAll('[data-w]')")
    assert "st.seq++" in later and "st.diff = null" in later and "paintGo()" in later


def test_external_rancher_needs_a_session():
    paint = block(CREATE, "function paintAccessSession(", "function wireAccess(")
    assert "ext.disabled = !session" in paint and "wrap.dataset.tip = why" in paint
    assert "tr('capi.acc.t.noSession')" in paint
    # la création lit la session, le modèle non (le plan la demande au lancement)
    wire = block(CREATE, "function wire(root, cluster, inv)", "function readBody(root)")
    assert "/rancher-status" in wire and "paintAccessSession(root, undefined)" in wire
    tpl = block(CREATE, "function templateForm(", "function stepText(")
    assert "paintAccessSession" not in tpl


def test_oidc_prefixes_are_checked_while_typing_and_always_sent():
    local = block(CREATE, "function oidcLocalErrors(", "function readOidc(")
    assert "'oidc-prefix-required'" in local and "'oidc-prefix-system'" in local
    read = block(CREATE, "function readOidc(", "function fillOidc(")
    assert "k === 'username_prefix' || k === 'groups_prefix'" in read
    chk = block(CREATE, "async function check()", "function later()")
    assert chk.index("oidcLocalErrors(root)") < chk.index("cluster-check")


def test_sse_only_through_the_reconnecting_helper():
    assert "new EventSource(" not in CAPI and "new EventSource(" not in CREATE
    assert "SSEReconnect.connect(`/api/stream/${enc(id)}`" in CAPI


def test_oidc_kubeconfig_is_handed_not_shown():
    oidc = block(ACCESS, "function paintOidc(", "function paintMembers(")
    assert "new Blob([r.d.kubeconfig]" in oidc and "a.download" in oidc
    assert "innerHTML = r.d.kubeconfig" not in oidc and "textContent = r.d.kubeconfig" not in oidc


LITERALS = [
    (RENEW, r"'renew-busy': \(\) => tr\('capi\.ren\.err\.busy'\)", "capi.ren.err.busy"),
    (RENEW, r"'renew-nothing': \(\) => tr\('capi\.ren\.err\.nothing'\)", "capi.ren.err.nothing"),
    (RENEW, r"'renew-plan-running': \(\) => tr\('capi\.ren\.err\.planRunning'\)", "capi.ren.err.planRunning"),
    (RENEW, r"'renew-pool-short': \(v\) => tr\('capi\.ren\.fd\.renew-pool-short'", "capi.ren.fd.renew-pool-short"),
    (RENEW, r"'renew-pool-missing': \(v\) => tr\('capi\.ren\.fd\.renew-pool-missing'", "capi.ren.fd.renew-pool-missing"),
    (CREATE, r"'offline-needs-no-fallback': \(v\) => tr\('capi\.new\.fd\.offline-needs-no-fallback'",
     "capi.new.fd.offline-needs-no-fallback"),
    (CREATE, r"'offline-rke2-missing': \(v\) => tr\('capi\.new\.fd\.offline-rke2-missing'", "capi.new.fd.offline-rke2-missing"),
    (CREATE, r"'offline-rke2-version-mismatch': \(v\) => tr\('capi\.new\.fd\.offline-rke2-version-mismatch'",
     "capi.new.fd.offline-rke2-version-mismatch"),
]


@pytest.mark.parametrize("src,pattern,key", LITERALS, ids=[k for _, _, k in LITERALS])
def test_each_code_has_a_literal_translation_in_five_languages(src, pattern, key):
    assert re.search(pattern, src), key
    assert I18N.count(f'"{key}"') + I18N.count(f"'{key}'") == 5, key


def test_access_findings_count_on_the_access_menu():
    for code in ("rancher-import-conflict", "rancher-session-needed", "oidc-prefix-required", "oidc-with-rancher"):
        assert f"'{code}': 'access'" in CREATE, code
    assert "'access'" in block(CREATE, "const SECTIONS = [", "];")


def test_new_history_verbs_are_labelled():
    kinds = block(CAPI, "function kindLabel(action)", "const capiHistory")
    for verb in ("capi-rancher-import", "capi-rancher-unimport", "capi-gitops", "capi-gitops-remove", "capi-renew"):
        assert f"case '{verb}':" in kinds, verb
