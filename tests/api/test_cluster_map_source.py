"""v1.43.0 : la vue Cluster en blocs, gardes au niveau du source.

Le comportement est exercé dans un navigateur par
`tests/e2e/test_cluster_map_view.py`. Ici, les règles qui régresseraient
sans que rien n'échoue visiblement : les gestes offerts sur une VM et ce qui
les verrouille, l'ordre « contrôle préalable puis confirmation » de la
maintenance, et le retrait complet de Cytoscape.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"
CMAP = (WEB / "static" / "js" / "cluster-map.js").read_text()
APP = (WEB / "static" / "js" / "app.js").read_text()
HTML = (WEB / "templates" / "index.html").read_text()


def _fn(name):
    return CMAP.split(f"function {name}(", 1)[1].split("\n  }\n", 1)[0]


def test_a_vm_offers_every_operational_action():
    show = _fn("showVm")
    for act in ("vm-notes", "vm-edit", "vm-console", "vm-snap", "vm-migrate",
                "vm-start", "vm-stop", "vm-delete"):
        assert f"btn('{act}'" in show, f"missing VM action {act}"


def test_the_panels_open_the_existing_modules():
    act = _fn("act")
    for call in ("window.Notes?.open('vm', cluster, ns, name)",
                 "window.VMEdit?.open?.(cluster, ns, name)",
                 "window.VMSnapshots?.open?.(cluster, ns, name)",
                 "window.VMConsole?.open?.(cluster, ns, name)",
                 "window.VMMigrate?.open?.(cluster, ns, name)",
                 "window.Notes?.open('node', cluster, name)"):
        assert call in act, call


def test_start_stop_contextual_and_not_behind_the_lock():
    """Parité avec l'onglet des VMs : démarrer et arrêter demandent une
    confirmation, pas le verrou. Seule la suppression y reste."""
    show = _fn("showVm")
    assert "v.run_strategy === 'Halted'" in show
    assert "(unlocked ? btn('vm-delete'" in show
    act = _fn("act")
    power = act.index("action === 'vm-start' || action === 'vm-stop'")
    delete = act.index("action === 'vm-delete'")
    assert power < delete
    assert "if (!unlocked)" not in act[power:delete]
    assert "if (!unlocked)" in act[delete:delete + 300]


def test_every_mutation_is_confirmed():
    act = _fn("act")
    for branch in ("'vm-start' || action === 'vm-stop'", "'vm-delete'", "'node-cordon'",
                   "'node-uncordon'", "'node-maint-enter'", "'node-maint-leave'"):
        # la branche seule, jusqu'à la suivante
        chunk = re.split(r"\} else if|\n      \}", act.split(branch, 1)[1], maxsplit=1)[0]
        assert "window.confirm(" in chunk, branch


def test_maintenance_is_checked_before_it_is_asked():
    """Le bouton qui demande la maintenance n'existe que dans le rendu du
    contrôle préalable ; le panneau de l'hôte n'offre que « Mettre en
    maintenance... », qui lance ce contrôle."""
    assert CMAP.count("btn('node-maint-enter'") == 1
    assert "btn('node-maint-enter'" in _fn("maintenanceCheck")
    assert "btn('node-maint-check'" in _fn("showNode")
    assert "if (action === 'node-maint-check') return maintenanceCheck(name, false);" in _fn("act")
    check = _fn("maintenanceCheck")
    assert "/maintenance-check${force ? '?force=1' : ''}" in check
    # refusé ou bloqué sans forçage : bouton désactivé
    assert "const can = !refusal && (!p.blocked || force);" in check


def test_forcing_needs_the_destructive_lock():
    check = _fn("maintenanceCheck")
    assert "${unlocked ? '' : 'disabled'}" in check
    act = _fn("act")
    assert "await call('POST', nodeBase(name) + '/maintenance', { force });" in act
    assert "const force = el && el.dataset.force === '1';" in act


def test_the_node_routes_are_the_ones_the_server_serves():
    act = _fn("act")
    for route in ("nodeBase(name) + '/cordon'", "nodeBase(name) + '/uncordon'",
                  "call('DELETE', nodeBase(name) + '/maintenance')"):
        assert route in act, route
    app_py = (WEB / "app.py").read_text()
    for rule in ("/api/node/<cluster>/<node>/cordon", "/api/node/<cluster>/<node>/uncordon",
                 "/api/node/<cluster>/<node>/maintenance",
                 "/api/node/<cluster>/<node>/maintenance-check"):
        assert rule in app_py, rule


def test_every_button_has_a_tooltip():
    buttons = re.findall(r"<button[^>]*>", CMAP)
    assert buttons
    for b in buttons:
        assert "data-tip" in b, b


def test_cytoscape_is_gone():
    assert not (WEB / "static" / "js" / "topology.js").exists()
    assert not (WEB / "static" / "vendor" / "cytoscape").exists()
    for text in (HTML, APP):
        assert "vendor/cytoscape" not in text and "cytoscape(" not in text
        assert "window.Topology" not in text and "topology.js" not in text
    assert "/static/js/cluster-map.js" in HTML
    assert HTML.index("/static/js/board.js") < HTML.index("/static/js/cluster-map.js")
