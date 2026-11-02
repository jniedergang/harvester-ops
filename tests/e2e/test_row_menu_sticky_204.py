"""2.0.4 : le bouton « ⋮ » d'une ligne reste visible.

Les tableaux à bouton « ⋮ » (Machines virtuelles, VMs d'un hôte Harvester,
VMs KVM, Conteneurs) défilent dans un `.tbl-scroll` et leur colonne
d'actions reste collée au bord droit (position: sticky). À 1440 px, menu
épinglé, la vue Machines virtuelles montre Hôte à Dossier sans défilement ;
plus étroit, le tableau défile et le « ⋮ » reste à l'écran, qu'on soit au
début, au milieu ou au bout. Les noms tiennent sur une ligne : un nom plus
long que sa colonne finit par une ellipse et porte sa valeur entière en
title.

Mutation : retirer `position: sticky` de `.tbl-scroll .vm-actions-cell` fait
échouer les tests de visibilité (le bouton sort de l'écran ou passe sous le
bord du conteneur)."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto  # noqa: E402
from test_containers_191 import pod, wl  # noqa: E402
from test_folders_200 import hvm, rows, world  # noqa: E402,F401  (fixture)
from test_kvm_vm_197 import ui  # noqa: E402,F401  (fixture)
from test_row_menu_203 import host_vms  # noqa: E402,F401  (fixture)

AVM = "#all-vms-host"

PROBE = """(btn) => {
  const w = btn.closest('.tbl-scroll');
  const r = btn.getBoundingClientRect(), wr = w.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return { left: r.left, right: r.right, vw: innerWidth, wl: wr.left, wr: wr.right,
           hit: !!hit && (hit === btn || btn.contains(hit)), sl: w.scrollLeft,
           over: w.scrollWidth - w.clientWidth, more: w.classList.contains('tbl-more-r') }; }"""

SCROLL = "(btn, f) => { const w = btn.closest('.tbl-scroll'); w.scrollLeft = (w.scrollWidth - w.clientWidth) * f; }"


def visible(btn):
    p = btn.evaluate(PROBE)
    assert p["left"] >= max(0, p["wl"]) - 1 and p["right"] <= min(p["vw"], p["wr"]) + 1, p
    assert p["hit"], p                                   # rien ne le recouvre
    return p


def scrolled_through(page, btn):
    """Le tableau déborde : le bouton reste visible au début, au milieu, au bout."""
    p = visible(btn)
    assert p["over"] > 40 and p["sl"] == 0, p
    page.wait_for_function("b => b.closest('.tbl-scroll').classList.contains('tbl-more-r')", arg=btn.element_handle())
    for f in (0.5, 1):
        btn.evaluate(SCROLL, f)
        page.wait_for_timeout(50)
        p = visible(btn)
        assert p["sl"] > 0, p
    # au bout, plus rien ne passe sous la colonne : l'ombre s'efface
    page.wait_for_function("b => !b.closest('.tbl-scroll').classList.contains('tbl-more-r')", arg=btn.element_handle())


def test_vms_view_at_1440_shows_host_to_folder_and_the_menu_without_scrolling(world):
    go, page, state = world
    page.set_viewport_size({"width": 1440, "height": 900})
    go("#vms")
    expect(rows(page)).to_have_count(6)
    btn = page.locator(f"{AVM} tbody tr[data-avm-row] [data-avm-act=more]").first
    p = visible(btn)
    assert p["sl"] == 0 and p["over"] <= 0, p            # Hôte à Dossier sans défilement
    heads = page.locator(f"{AVM} thead th").evaluate_all("ts => ts.map(t => t.getBoundingClientRect().right)")
    assert max(heads) <= p["vw"], heads
    # plus étroit, le tableau défile et le « ⋮ » reste là
    page.set_viewport_size({"width": 900, "height": 900})
    scrolled_through(page, btn)
    # la colonne collée a le fond de la page (pas transparente)
    bg = btn.evaluate("b => getComputedStyle(b.closest('td')).backgroundColor")
    assert bg not in ("rgba(0, 0, 0, 0)", "transparent"), bg


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_the_sticky_column_takes_the_page_background_in_every_mode(world, mode):
    go, page, state = world
    page.add_init_script(f"localStorage.setItem('harvester_ops_mode','{mode}')")
    page.set_viewport_size({"width": 900, "height": 900})
    go("#vms")
    btn = page.locator(f"{AVM} tbody tr[data-avm-row] [data-avm-act=more]").first
    expect(btn).to_be_visible()
    assert page.evaluate("document.documentElement.dataset.mode") == mode
    td, body = btn.evaluate("b => [getComputedStyle(b.closest('td')).backgroundColor, getComputedStyle(document.body).backgroundColor]")
    assert td == body, (td, body)
    visible(btn)


def test_host_vm_list_keeps_the_menu_in_view(host_vms):
    page = host_vms
    page.set_viewport_size({"width": 560, "height": 900})
    scrolled_through(page, page.locator("#ns-vms-table tbody tr [data-vm-more]").first)


def test_containers_keep_the_menu_in_view(context, flask_server):
    data = {"clusters": [{"cluster": "alpha", "state": "ok", "count": 3}],
            "workloads": [wl("alpha", "observability", f"workload-{i}", "Deployment", "Running",
                             images=("registry.example.com/team/image:2026.10.31",), pods=[pod(f"w{i}-a")])
                          for i in range(3)]}
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_sidebar_pinned','1');"
                            "localStorage.removeItem('harvester_ops_allcontainers');")
    page = context.new_page()
    page.set_viewport_size({"width": 900, "height": 900})
    page.route("**/api/containers-all", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                               body=json.dumps(data)))
    goto(page, flask_server["base_url"], address("allcontainers"))
    btn = page.locator("#all-containers-host tbody tr[data-ct-row] [data-ct-act=more]").first
    expect(btn).to_be_attached(timeout=15000)
    scrolled_through(page, btn)


def test_kvm_vms_keep_the_menu_in_view(ui):
    page, state, _ = ui
    page.set_viewport_size({"width": 700, "height": 900})
    page.evaluate("Router.go('#vms/host/kvm')")
    btn = page.locator('#tab-kvmvms tr[data-kvm-vm="node2/web"] [data-kvm="menu"]')
    expect(btn).to_be_visible()
    scrolled_through(page, btn)


LONG_VM = "a-virtual-machine-name-much-longer-than-its-column"
LONG_NS = "namespace-longer-than-the-column"
LONG_HOST = "cluster-with-a-very-long-name"


@pytest.mark.parametrize("cls,value", [("avm-name", LONG_VM), ("avm-ns", LONG_NS), ("avm-cluster", LONG_HOST)])
def test_a_long_name_ends_with_an_ellipsis_and_keeps_its_full_value_in_the_title(world, cls, value):
    go, page, state = world
    state["vms"].append(hvm(LONG_VM, ns=LONG_NS, cluster=LONG_HOST))
    page.set_viewport_size({"width": 1440, "height": 900})
    go("#vms")
    expect(rows(page)).to_have_count(7)
    row = page.locator(f'{AVM} tr[data-avm-row="harvester/{LONG_HOST}/{LONG_NS}/{LONG_VM}"]')
    cell = row.locator(f".{cls}")
    expect(cell).to_have_text(value)
    assert cell.get_attribute("title") == value
    clip = cell.evaluate("e => [e.scrollWidth > e.clientWidth, getComputedStyle(e).textOverflow, getComputedStyle(e).whiteSpace]")
    assert clip == [True, "ellipsis", "nowrap"], clip
    # la ligne reste sur une seule ligne, comme les autres
    heights = page.locator(f"{AVM} tbody tr[data-avm-row]").evaluate_all("rs => rs.map(r => Math.round(r.getBoundingClientRect().height))")
    assert max(heights) - min(heights) <= 1, heights
    assert re.fullmatch(r"\d+", str(max(heights))) and max(heights) < 50, heights
