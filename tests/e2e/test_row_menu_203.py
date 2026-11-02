"""2.0.3 : lignes de VMs allégées et bandeau d'hôte compact.

Chaque ligne des listes de VMs (vue Machines virtuelles, VMs d'un hôte
Harvester) et des Conteneurs n'a plus qu'un bouton « ⋮ ». Il ouvre le menu
d'actions partagé (un seul élément dans la page), dont la tête porte les
gestes qui étaient des boutons de ligne, avec les mêmes droits et les mêmes
raisons de grisé, au clavier comme à la souris. Le bandeau d'une page d'hôte
tient sur une ligne, et Paramètres > Alimentation n'a plus qu'une barre
d'onglets (Démarrage / Arrêt passent dans un sélecteur du panneau).

Budget de dessin : nombre d'éléments d'une ligne (déterministe) et meilleur
de trois redessins complets de 300 lignes (borne large pour la suite en
parallèle ; la cible de 0,3 s mesurée seule est au CHANGELOG)."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto, settle  # noqa: E402
from test_containers_191 import pod, wl  # noqa: E402
from test_folders_200 import KUUID, hvm, kvm, rows, world  # noqa: E402,F401  (fixture)
from test_host_page_200 import open_cold  # noqa: E402

AVM = "#all-vms-host"
LEAD_HARV = ["stop", "console", "edit", "notes", "folder"]

FULL_BEST = """async (n) => { let best = 1e9; const cols = ['namespace', 'name'];
  for (let i = 0; i < n; i += 1) {
    const t = performance.now();
    document.querySelector(`#all-vms-host [data-avm-sort="${cols[i % 2]}"]`).click();
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    best = Math.min(best, performance.now() - t);
  }
  return best; }"""


def open_menu(page, key):
    btn = page.locator(f'{AVM} tr[data-avm-row="{key}"] [data-avm-act="more"]')
    btn.click()
    menu = page.locator(".vma-menu")
    expect(menu).to_have_count(1)
    return btn, menu


# -- vue Machines virtuelles --------------------------------------------------
def test_one_menu_button_per_row_and_nothing_else(world):
    go, page, state = world
    go("#vms")
    expect(rows(page)).to_have_count(6)
    per_row = page.locator(f"{AVM} tbody tr[data-avm-row]").evaluate_all(
        "rs => rs.map(r => [r.querySelectorAll('button').length, r.querySelectorAll('[data-avm-act=more]').length,"
        " r.querySelectorAll('.vm-actions-cell svg').length])")
    assert per_row == [[1, 1, 0]] * 6
    for b in page.locator(f'{AVM} [data-avm-act="more"]').all():
        assert b.get_attribute("aria-haspopup") == "menu"
        assert b.get_attribute("aria-expanded") == "false"
        assert "start or stop" in b.get_attribute("data-tip")
        assert b.get_attribute("aria-label").startswith("Actions on ")


@pytest.mark.parametrize("key,lead", [
    ("harvester/harv-fake/default/web-1", LEAD_HARV),
    ("harvester/harv-fake/lab/cirros", LEAD_HARV),
])
def test_the_harvester_menu_starts_with_the_former_row_buttons(world, key, lead):
    go, page, state = world
    go("#vms")
    _, menu = open_menu(page, key)
    acts = menu.locator("[data-vma-lead]").evaluate_all("els => els.map(e => e.dataset.act)")
    assert acts == lead
    for it in menu.locator("[data-vma-lead]").all():
        assert it.get_attribute("data-tip")
    # les groupes de Harvester suivent, une fois l'état lu
    expect(menu.locator(".vma-body .vma-item, .vma-body .res-error").first).to_be_visible()


def test_a_halted_vm_offers_start_and_the_gesture_goes_to_its_cluster(world):
    go, page, state = world
    state["vms"].append(hvm("off", phase="Stopped"))
    go("#vms")
    sent = []
    page.route(re.compile(r".*/api/vm/harv-fake/default/off/runStrategy$"),
               lambda r, q: (sent.append(q.post_data_json), r.fulfill(status=200, body="{}")))
    _, menu = open_menu(page, "harvester/harv-fake/default/off")
    assert menu.locator('[data-vma-lead][data-act="stop"]').count() == 0
    menu.locator('[data-vma-lead][data-act="start"]').click()
    expect(page.locator(".vma-menu")).to_have_count(0)
    page.wait_for_timeout(300)
    assert sent == [{"runStrategy": "Always"}]


def test_the_lead_gestures_open_their_windows(world):
    go, page, state = world
    go("#vms")
    page.evaluate("""() => { window.__opened = [];
      VMConsole.open = (...a) => __opened.push(['console', ...a]);
      VMEdit.open = (...a) => __opened.push(['edit', ...a]);
      Notes.open = (...a) => __opened.push(['notes', ...a]); }""")
    for act in ("console", "edit", "notes"):
        _, menu = open_menu(page, "harvester/harv-fake/lab/cirros")
        menu.locator(f'[data-vma-lead][data-act="{act}"]').click()
    assert page.evaluate("__opened") == [["console", "harv-fake", "lab", "cirros"],
                                         ["edit", "harv-fake", "lab", "cirros"],
                                         ["notes", "vm", "harv-fake", "lab", "cirros"]]


def test_the_kvm_menu_has_the_host_page_gestures_then_folder_and_host(world):
    """2.2.2 : le menu d'une VM KVM est celui de la page de l'hôte (gestes,
    console, opérations, Modifier), suivi du rangement et de la page de l'hôte."""
    go, page, state = world
    go("#vms")
    _, menu = open_menu(page, f"kvm/{KUUID}")
    acts = menu.locator(".vma-item").evaluate_all(
        "els => els.map(e => e.dataset.kvmGesture || e.dataset.kvmOp || e.dataset.kvmHot"
        " || (e.dataset.kvmConsole && 'console') || e.dataset.vmfKvm)")
    assert acts == ["start", "shutdown", "reboot", "suspend", "resume", "destroy", "console",
                    "snapshots", "clone", "delete", "edit", "folder", "page"]
    menu.locator('[data-vmf-kvm="folder"]').click()
    expect(page.locator("#fp-fld-move")).to_be_visible()


def test_keyboard_opens_walks_and_closes_the_single_shared_menu(world):
    go, page, state = world
    go("#vms")
    a = page.locator(f'{AVM} tr[data-avm-row="harvester/harv-fake/default/web-1"] [data-avm-act="more"]')
    b = page.locator(f'{AVM} tr[data-avm-row="harvester/harv-fake/lab/cirros"] [data-avm-act="more"]')
    a.focus()
    page.keyboard.press("Enter")
    expect(a).to_have_attribute("aria-expanded", "true")
    expect(page.locator(".vma-menu [data-act=stop]")).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(page.locator(".vma-menu [data-act=console]")).to_be_focused()
    page.keyboard.press("ArrowUp")
    expect(page.locator(".vma-menu [data-act=stop]")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator(".vma-menu")).to_have_count(0)
    expect(a).to_be_focused()
    expect(a).to_have_attribute("aria-expanded", "false")
    # un seul menu dans la page, même en passant d'une ligne à l'autre
    a.click()
    b.click()
    expect(page.locator(".vma-menu")).to_have_count(1)
    expect(page.locator(".vma-menu .vma-head")).to_contain_text("cirros")


def test_a_viewer_sees_the_folder_gesture_greyed_with_the_reason(world):
    go, page, state = world
    state["role"] = "viewer"
    go("#vms")
    page.wait_for_function("() => App.role && App.role() === 'viewer'")
    _, menu = open_menu(page, "harvester/harv-fake/lab/cirros")
    item = menu.locator('[data-vma-lead][data-act="folder"]')
    expect(item).to_be_disabled()
    expect(item).to_have_attribute("data-tip", re.compile("operator role"))
    item.dispatch_event("click")
    expect(page.locator("#fp-fld-move")).to_have_count(0)


@pytest.mark.parametrize("n", [2000, 10000])
def test_a_full_redraw_of_300_rows_stays_in_budget(world, n):
    """Garde déterministe : une ligne compte au plus 40 éléments et aucune
    icône SVG dans sa cellule d'actions (2.0.2 : six boutons à icône). Garde
    de temps : meilleur de trois redessins complets (nouveau tri), borne
    large pour la suite en parallèle ; seul, < 0,3 s (CHANGELOG 2.0.3)."""
    go, page, state = world
    state["vms"] = [hvm(f"vm{i:05d}", ns=f"ns{i % 7}", folder="/Prod" if i % 3 else None) for i in range(n)]
    go("#vms")
    expect(rows(page)).to_have_count(300, timeout=60000)
    sizes = page.locator(f"{AVM} tbody tr[data-avm-row]").evaluate_all(
        "rs => rs.map(r => r.querySelectorAll('*').length)")
    assert max(sizes) <= 40, max(sizes)
    assert page.locator(f"{AVM} tbody .vm-actions-cell svg").count() == 0
    best = page.evaluate(FULL_BEST, 3)
    print(f"{n} VMs: best full redraw of 300 rows {best:.0f} ms")
    assert best < 600


# -- VMs d'un hôte Harvester ----------------------------------------------------
HOST_VMS = {"vms": [{"namespace": "default", "name": "web", "phase": "Running", "runStrategy": "Always",
                     "agent_connected": "False", "priority": 0},
                    {"namespace": "default", "name": "off", "phase": "Stopped", "runStrategy": "Halted",
                     "agent_connected": "False", "priority": 0}]}


@pytest.fixture
def host_vms(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    body = lambda b: dict(status=200, content_type="application/json", body=json.dumps(b))  # noqa: E731
    page.route("**/api/vms/harv-fake", lambda r, q: r.fulfill(**body(HOST_VMS)))
    page.route("**/api/namespaces/harv-fake", lambda r, q: r.fulfill(**body([{"name": "default"}])))
    page.route(re.compile(r".*/api/vm/harv-fake/default/[^/]+/state$"),
               lambda r, q: r.fulfill(**body({"running": True, "paused": False, "agent": False, "node": "n1",
                                              "targets": [], "run_strategy": "Always", "volumes": []})))
    goto(page, flask_server["base_url"], "#harvester/harv-fake/vms")
    page.wait_for_function("window.VMActions && window.App && App.getCurrentCluster()")
    expect(page.locator("#ns-vms-table tbody tr")).to_have_count(2, timeout=8000)
    return page


def test_host_vm_rows_have_one_menu_button_with_the_former_gestures(host_vms):
    page = host_vms
    per_row = page.locator("#ns-vms-table tbody tr").evaluate_all(
        "rs => rs.map(r => [r.querySelectorAll('button').length, r.querySelectorAll('[data-vm-more]').length])")
    assert per_row == [[1, 1], [1, 1]]
    page.evaluate("""() => { window.__opened = [];
      VMMigrate.open = (...a) => __opened.push(['migrate', ...a]);
      VMEdit.open = (...a) => __opened.push(['edit', ...a]); }""")
    web = page.locator("#ns-vms-table tr", has_text="web").locator("[data-vm-more]")
    web.click()
    menu = page.locator(".vma-menu")
    assert menu.locator("[data-vma-lead]").evaluate_all("els => els.map(e => e.dataset.act)") == \
        ["stop", "console", "edit", "notes", "migrate-any"]
    menu.locator('[data-act="migrate-any"]').click()
    page.locator("#ns-vms-table tr", has_text="off").locator("[data-vm-more]").click()
    assert page.locator(".vma-menu [data-vma-lead]").first.get_attribute("data-act") == "start"
    page.locator('.vma-menu [data-act="edit"]').click()
    assert page.evaluate("__opened") == [["migrate", "harv-fake", "default", "web"], ["edit", "harv-fake", "default", "off"]]


# -- Conteneurs -------------------------------------------------------------------
def test_container_rows_have_one_menu_button_and_are_paged(context, flask_server):
    n = 650
    data = {"clusters": [{"cluster": "alpha", "state": "ok", "count": n}],
            "workloads": [wl("alpha", f"ns{i % 9}", f"w{i:04d}", "Deployment", "Running", pods=[pod(f"w{i}-a")])
                          for i in range(n)]}
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.removeItem('harvester_ops_allcontainers');")
    page = context.new_page()
    page.route("**/api/containers-all", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                               body=json.dumps(data)))
    goto(page, flask_server["base_url"], address("allcontainers"))
    trs = page.locator("#all-containers-host tbody tr[data-ct-row]")
    expect(trs).to_have_count(300, timeout=30000)
    acts = trs.evaluate_all("rs => rs.map(r => [...r.querySelectorAll('.vm-actions-cell button')].map(b => b.dataset.ctAct))")
    assert acts == [["more"]] * 300
    more = page.locator('#all-containers-host [data-ct="more"]')
    expect(more).to_have_text("Show 300 more")
    expect(more).to_have_attribute("title", "300 of 650 rows shown: show the next ones.")
    more.click()
    expect(trs).to_have_count(600)
    more.click()
    expect(trs).to_have_count(650)
    expect(more).to_be_hidden()
    # le menu porte toujours les journaux (l'ancien bouton de ligne)
    trs.first.locator('[data-ct-act="more"]').click()
    expect(page.locator('.ct-menu [data-ct-menu="logs"]')).to_be_visible()
    # un tri repart des 300 premières lignes
    page.click('#all-containers-host [data-ct-sort="namespace"]')
    expect(trs).to_have_count(300)


# -- bandeau d'hôte ---------------------------------------------------------------
@pytest.mark.parametrize("hash", ["#harvester/harv-fake", "#harvester/harv-fake/vms", "#harvester/harv-fake/params/storage"])
def test_the_host_band_is_one_line(context, flask_server, hash):
    page, _ = open_cold(context, flask_server, hash)
    settle(page)
    title = page.locator("#host-page-head .hp-title")
    expect(title).to_be_visible()
    box = title.bounding_box()
    assert box["height"] <= 56, box
    tops = title.evaluate("t => [...t.children].map(c => Math.round(c.getBoundingClientRect().top + c.getBoundingClientRect().height / 2))")
    assert max(tops) - min(tops) <= 6, tops                     # tout sur la même ligne
    act = page.locator("#host-page-head .hp-activity")
    assert act.get_attribute("title") and act.get_attribute("aria-label")


@pytest.mark.parametrize("pane,section", [("startup", "#tab-startup"), ("shutdown", "#tab-shutdown")])
def test_power_has_a_single_tab_bar_and_a_selector_in_the_panel(context, flask_server, pane, section):
    page, _ = open_cold(context, flask_server, f"#harvester/harv-fake/params/power/{pane}")
    settle(page)
    expect(page.locator("#host-page-head .hp-tabs")).to_have_count(1)
    expect(page.locator('#host-page-head [data-hp-tab="power"]')).to_have_class(re.compile(r"\bactive\b"))
    seg = page.locator(f"{section} [data-hp-power-seg]")
    expect(seg).to_be_visible()
    assert seg.locator("[data-hp-power]").evaluate_all("els => els.map(e => e.dataset.hpPower)") == ["startup", "shutdown"]
    expect(seg.locator(f'[data-hp-power="{pane}"]')).to_have_attribute("aria-selected", "true")
    for a in seg.locator("a").all():
        assert a.get_attribute("title")
    other = "shutdown" if pane == "startup" else "startup"
    seg.locator(f'[data-hp-power="{other}"]').click()
    page.wait_for_function(f"() => location.hash.endsWith('/params/power/{other}')")
    expect(page.locator(f"#tab-{other}")).to_have_class(re.compile(r"\bactive\b"))
    # ailleurs que dans Alimentation, le sélecteur se cache
    page.evaluate("Router.go('#harvester/harv-fake/params/storage')")
    settle(page)
    assert page.locator("[data-hp-power-seg]:visible").count() == 0
