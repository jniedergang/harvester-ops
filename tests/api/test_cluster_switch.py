"""v1.20.0 / v1.21.0 — voile de chargement et fraîcheur des vues.

Le comportement est exercé dans un vrai navigateur par
`tests/e2e/test_cluster_switch.py`. Ce qui est verrouillé ici, ce sont les
détails qui régresseraient sans que rien n'échoue visiblement : l'ordre de
chargement des scripts, le garde-fou qui lève le voile même si un
chargement ne rend jamais la main, la couverture de TOUS les onglets, et
les deux endroits par où la topologie pouvait afficher le mauvais cluster.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"
VEIL = (JS / "veil.js").read_text()
APP = (JS / "app.js").read_text()
TOPO = (JS / "topology.js").read_text()
CSS = (ROOT / "web" / "static" / "css" / "style.css").read_text()
HTML = (ROOT / "web" / "templates" / "index.html").read_text()


def test_veil_loads_before_the_modules_that_use_it():
    assert "/static/js/veil.js" in HTML
    assert HTML.index("/static/js/veil.js") < HTML.index("/static/js/app.js"), (
        "app.js appelle Veil : il doit être défini avant")


def test_a_stuck_load_cannot_lock_the_interface():
    """Le voile plein écran bloque les clics. Un chargement qui ne rend
    jamais la main laisserait l'application inutilisable : deux filets, le
    `finally` et le délai de sécurité."""
    assert "SAFETY_MS" in VEIL
    assert "setTimeout(() => hide(target, true), SAFETY_MS)" in VEIL
    during = VEIL.split("async function during(", 1)[1].split("\n  }", 1)[0]
    assert "finally" in during and "hide(target)" in during


def test_a_fast_load_shows_nothing_at_all():
    """Sans seuil, une réponse de 80 ms produit un clignotement plus gênant
    que l'attente qu'il annonce."""
    during = VEIL.split("async function during(", 1)[1].split("\n  }", 1)[0]
    assert "opts.delay" in during and "setTimeout" in during
    assert "if (armed)" in during, "ne rien lever si rien n'a été montré"
    # les zones de l'aperçu utilisent bien ce seuil
    assert APP.count("delay: 250") >= 2


def test_the_veil_is_removed_from_the_flow_when_gone():
    """`display: flex` l'emporterait sur un simple `hidden` : le voile est
    donc retiré du DOM, pas seulement masqué."""
    hide = VEIL.split("function hide(target, immediate)", 1)[1].split("\n  }", 1)[0]
    assert "entry.el.remove()" in hide and "active.delete(key)" in hide


def test_the_veil_blurs_the_background_and_respects_reduced_motion():
    assert "backdrop-filter: blur" in CSS
    assert "-webkit-backdrop-filter: blur" in CSS, "Safari a besoin du préfixe"
    assert "@supports not ((backdrop-filter" in CSS, (
        "sans repli, un navigateur sans backdrop-filter ne signale plus rien")
    reduced = CSS.split("@media (prefers-reduced-motion: reduce)")[-1]
    assert ".veil-card" in reduced and "animation: none" in reduced


def test_the_scoped_veil_stays_inside_its_zone():
    """Une vue lente ne doit flouter QUE sa zone : le reste de la page
    demeure lisible et cliquable."""
    assert ".veil-scoped { position: absolute; inset: 0;" in CSS
    assert ".veil-fullscreen { position: fixed; inset: 0;" in CSS
    # seul le plein écran bloque les clics
    assert "body.veil-blocking #app { pointer-events: none; }" in CSS
    show = VEIL.split("function show(target, opts = {})", 1)[1].split("\n  }", 1)[0]
    assert "if (!target) document.body.classList.add('veil-blocking')" in show
    assert "host.style.position = 'relative'" in show, (
        "sans contexte de positionnement, le voile se calerait sur la fenêtre")


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
    assert "Veil.during(null, {" in set_cluster
    assert "reloadForCluster" in set_cluster
    assert "if (!previous || opts.silent)" in set_cluster, (
        "le premier chargement n'a rien à masquer")
    reload_fn = APP.split("async function reloadForCluster()", 1)[1].split("\n  }", 1)[0]
    assert "Promise.allSettled" in reload_fn, (
        "un onglet en erreur ne doit pas laisser le voile en place")


def test_capi_exposes_a_reactivate_entry_point():
    """L'onglet Automation ne se reconstruit qu'au clic ; sans ce point
    d'entrée, y rester pendant une bascule laissait le diagnostic du
    cluster précédent à l'écran."""
    capi = (JS / "capi.js").read_text()
    assert "async function reactivate()" in capi
    assert re.search(r"return \{[^}]*reactivate[^}]*\}", capi)
    assert "window.CAPI.reactivate" in APP


def test_the_cluster_name_reaches_the_veil_escaped():
    assert "esc(opts.name)" in VEIL
    paint = VEIL.split("function paint(el, opts)", 1)[1].split("\n  }", 1)[0]
    assert "esc(msg)" in paint, "le message aussi passe par l'échappement"


# ---------------------------------------------------------------------------
# v1.21.0 — la topologie affichait le cluster précédent
# ---------------------------------------------------------------------------

def test_returning_to_the_overview_remounts_the_topology():
    """Constaté : avec harv3 sélectionné depuis Cluster API, revenir sur
    Cluster/Aperçu affichait le nœud et les VMs de harv1. La topologie
    n'était (re)montée que par un clic sur son sous-onglet."""
    set_tab = APP.split("function setTab(name)", 1)[1].split("\n  }", 1)[0]
    assert "mountTopology(overviewMode())" in set_tab
    assert "if (name !== 'overview' && window.Topology) window.Topology.stop();" in set_tab, (
        "quitter l'aperçu doit couper le polling, qui visait l'ancien cluster")


def test_the_topology_drops_a_response_from_the_previous_cluster():
    """Une réponse en vol au moment de la bascule appartient au cluster
    précédent : la peindre écraserait la vue du nouveau."""
    refresh = TOPO.split("async function refresh()", 1)[1].split("\n  }", 1)[0]
    assert "const askedCluster = lastCluster;" in refresh
    assert "if (askedCluster !== lastCluster || askedMode !== currentMode) return;" in refresh


def test_the_topology_load_can_be_awaited_and_veiled():
    start = TOPO.split("function start(cluster, mode", 1)[1].split("\n  }", 1)[0]
    assert "const first = refresh();" in start and "return first;" in start
    mount = APP.split("function mountTopology(mode)", 1)[1].split("\n  }\n", 1)[0]
    assert "Veil.during(host, {" in mount
    assert "if (mode === 'metrics') { window.Topology.stop(); return; }" in mount, (
        "l'onglet Métriques n'a pas de canevas mais doit couper le polling")


def test_background_refreshes_do_not_veil():
    """L'aperçu se rafraîchit périodiquement. Sans distinguer le fond d'un
    chargement demandé, la zone clignotait à chaque cycle du minuteur —
    constaté en ralentissant /api/status."""
    status = APP.split("async function refreshStatus(opts = {})", 1)[1].split("\n  }", 1)[0]
    assert "if (!opts.veil) return refreshStatusInner();" in status
    # le minuteur périodique appelle sans option
    timer = APP.split("statusRefreshTimer = setInterval(", 1)[1].split("}, ", 1)[0]
    assert "refreshStatus()" in timer and "veil" not in timer
    # les chargements déclenchés par l'opérateur la demandent
    assert APP.count("refreshStatus({ veil: true })") >= 3
