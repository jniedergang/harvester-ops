"""v1.20.0 — invariants de la bascule de cluster (niveau source).

Le comportement lui-même est exercé dans un vrai navigateur par
`tests/e2e/test_cluster_switch.py`. Ce qui est verrouillé ici, ce sont les
détails qui régresseraient sans que rien n'échoue visiblement : l'ordre de
chargement des scripts, le garde-fou qui lève le voile même si un
rafraîchissement ne rend jamais la main, et la couverture de TOUS les
onglets par le rechargement.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"
SWITCH = (JS / "cluster-switch.js").read_text()
APP = (JS / "app.js").read_text()
CSS = (ROOT / "web" / "static" / "css" / "style.css").read_text()
HTML = (ROOT / "web" / "templates" / "index.html").read_text()


def test_overlay_loads_before_the_module_that_uses_it():
    assert "/static/js/cluster-switch.js" in HTML
    assert HTML.index("/static/js/cluster-switch.js") < HTML.index("/static/js/app.js"), (
        "app.js appelle ClusterSwitch : il doit être défini avant")


def test_a_stuck_refresh_cannot_lock_the_interface():
    """Le voile bloque les clics. Un rafraîchissement qui ne rend jamais la
    main laisserait donc l'application inutilisable : deux filets, le
    `finally` et le délai de sécurité."""
    assert "SAFETY_MS" in SWITCH
    assert "setTimeout(() => hide(true), SAFETY_MS)" in SWITCH
    during = SWITCH.split("async function during(", 1)[1].split("\n  }", 1)[0]
    assert "finally" in during and "hide()" in during


def test_the_veil_is_removed_from_the_flow_when_hidden():
    """`display: flex` l'emporte sur l'attribut `hidden` du navigateur :
    sans cette règle le voile reste par-dessus la page, invisible et
    avalant tous les clics."""
    assert ".cluster-switch-overlay[hidden] { display: none; }" in CSS


def test_the_veil_blurs_the_background_and_respects_reduced_motion():
    assert "backdrop-filter: blur" in CSS
    assert "-webkit-backdrop-filter: blur" in CSS, "Safari a besoin du préfixe"
    assert "@supports not ((backdrop-filter" in CSS, (
        "sans repli, un navigateur sans backdrop-filter ne signale plus rien")
    reduced = CSS.split("@media (prefers-reduced-motion: reduce)")[-1]
    assert ".cluster-switch-card" in reduced and "animation: none" in reduced


def test_every_tab_is_reloaded_for_the_new_cluster():
    """Le défaut d'origine : seul l'aperçu se rafraîchissait. Si un onglet
    est ajouté à la page sans être traité ici, il affichera les données du
    cluster précédent — silencieusement."""
    tabs = set(re.findall(r'<section id="tab-([a-z]+)" class="tab-content', HTML))
    assert {"overview", "namespaces", "activity", "shutdown", "automation"} <= tabs

    reload_fn = APP.split("async function reloadForCluster()", 1)[1].split("\n  }", 1)[0]
    # `startup` n'a pas de liste propre : son contenu est le séquenceur,
    # alimenté par refreshStatus comme le reste de l'en-tête.
    for tab in tabs - {"startup"}:
        assert tab in reload_fn, f"l'onglet {tab} n'est pas rechargé au changement de cluster"


def test_the_switch_waits_behind_the_veil():
    set_cluster = APP.split("async function setCluster(", 1)[1].split("\n  }", 1)[0]
    assert "ClusterSwitch.during(name, reloadForCluster)" in set_cluster
    assert "if (!previous || opts.silent)" in set_cluster, (
        "le premier chargement n'a rien à masquer")
    # un onglet en erreur ne doit pas laisser le voile en place
    reload_fn = APP.split("async function reloadForCluster()", 1)[1].split("\n  }", 1)[0]
    assert "Promise.allSettled" in reload_fn


def test_capi_exposes_a_reactivate_entry_point():
    """L'onglet Automation ne se reconstruit qu'au clic ; sans ce point
    d'entrée, y rester pendant une bascule laissait le diagnostic du
    cluster précédent à l'écran."""
    capi = (JS / "capi.js").read_text()
    assert "async function reactivate()" in capi
    assert re.search(r"return \{[^}]*reactivate[^}]*\}", capi)
    assert "window.CAPI.reactivate" in APP


def test_the_cluster_name_reaches_the_veil_escaped():
    assert "esc(cluster)" in SWITCH
    assert "innerHTML" in SWITCH and "esc(" in SWITCH
