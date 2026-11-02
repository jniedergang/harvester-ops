"""2.1.0 : Bare-metal en onglets (Machines, Profils, Images, Historique).

* onglet par défaut, un test par onglet (adresse écrite sans entrée
  d'historique, contenu de l'onglet), ancienne adresse #automation/pxe ;
* découverte dans sa fenêtre (formulaire d'avant, Échap, info-bulles) ;
* dernière découverte gardée par la console : relue après rechargement par
  la VRAIE route (BMC injoignable 127.0.0.1:9, aucune machine touchée), sans
  identifiant dans le fichier d'état ; Relire sans identifiants les demande ;
* bandeau « aucun ISO » / « aucun ISO de la version X » et son lien ;
* Historique = Activité filtrée sur les actions bare-metal, liens de journal ;
* mutations volontaires.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nav import bm_discover, goto, settle  # noqa: E402

HOST = "192.0.2.41"
NODE = {"ok": True, "host": HOST, "manufacturer": "HPE", "model": "ProLiant XL170r Gen9", "power_state": "Off",
        "serial": "SN0210", "bios_version": "U14", "memory_gib": 128, "cpu_count": 1,
        "nics": [{"name": "System Ethernet Interface", "mac": "94:18:82:00:02:10", "status": "Enabled",
                  "speed_mbps": 1000}],
        "uefi_targets": ["HD.Emb.1.1", "HD.Emb.1.2"], "virtualmedia_path": "/redfish/v1/Managers/1/VirtualMedia/2",
        "boot_targets": ["Cd", "Hdd"]}
ISOS = {"isos": [{"name": "harvester-v1.9.0-amd64.iso", "size": 7 * 2 ** 30, "sha256": "ab" * 32}],
        "disk_free": 10 ** 11}
TABS = ["machines", "profiles", "images", "history"]
SECRET = "E2E-BMC-SECRET-210"


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def open_bm(context, flask_server, hash="#automation/pxe", isos=ISOS, last=None):
    """Bare-metal à froid ; la dernière découverte et le magasin sont simulés
    (sauf `last=False` : la vraie route de la console)."""
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    page.route("**/api/isos", lambda r, q: fulfill(r, isos))
    page.route("**/api/bmc/discover", lambda r, q: fulfill(r, {"nodes": [NODE], "count": 1, "at": 1767000000}))
    if last is not False:
        page.route("**/api/bmc/last-discovery", lambda r, q: fulfill(r, {"discovery": last}))
    page.on("dialog", lambda d: d.accept())
    goto(page, flask_server["base_url"], hash)
    page.mouse.move(1100, 450)
    return page


def visible_pane(page):
    return page.evaluate("""() => [...document.querySelectorAll('#bmc-body [data-bm-pane]')]
        .filter(p => !p.hidden).map(p => p.dataset.bmPane)""")


@pytest.mark.parametrize("hash", ["#automation/pxe", "#automation/pxe/machines"])
def test_default_tab_is_machines_and_the_legacy_address_opens_it(context, flask_server, hash):
    page = open_bm(context, flask_server, hash)
    expect(page.locator('#tab-automation [data-bm-tab="machines"]')).to_have_class(re.compile(r"\bactive\b"))
    assert visible_pane(page) == ["machines"]
    expect(page.locator('#tab-automation .bm-tabs')).to_be_visible()
    # la bande de Cluster API reste celle de Cluster API
    expect(page.locator('#tab-automation .sub-tabs-inline[data-only-for="capi"]')).to_be_hidden()
    assert page.locator('#tab-automation [data-bm-tab="machines"]').get_attribute("aria-selected") == "true"
    expect(page.locator('#bmc-body [data-bm="discover-open"]')).to_be_visible()
    expect(page.locator("#bm-last-at")).to_contain_text("No discovery yet")
    expect(page.locator(".bm-machines-empty")).to_be_visible()


@pytest.mark.parametrize("tab,content", [
    ("profiles", "#bm-profiles .card"),
    ("images", "#bmc-iso-form"),
    ("history", "#bm-history .form-hint"),
    ("machines", '#bmc-body [data-bm="discover-open"]'),
])
def test_each_tab_shows_its_content_and_writes_its_address(context, flask_server, tab, content):
    start = "#automation/pxe/images" if tab == "machines" else "#automation/pxe"
    page = open_bm(context, flask_server, start)
    n = page.evaluate("history.length")
    btn = page.locator(f'#tab-automation [data-bm-tab="{tab}"]')
    assert btn.get_attribute("title") and btn.get_attribute("data-tip-i18n")      # info-bulle
    btn.click()
    expect(page.locator(content).first).to_be_visible()
    assert visible_pane(page) == [tab]
    assert page.evaluate("location.hash") == f"#automation/pxe/{tab}"
    assert page.evaluate("history.length") == n                                 # clic DANS l'écran
    # F5 rend le même onglet
    page.reload(wait_until="domcontentloaded")
    settle(page)
    assert visible_pane(page) == [tab]


def test_the_strip_follows_the_automation_sub_tab(context, flask_server):
    page = open_bm(context, flask_server, "#automation/pxe/profiles")
    page.evaluate("Router.go('#automation/capi')")
    settle(page)
    expect(page.locator('#tab-automation .bm-tabs')).to_be_hidden()
    expect(page.locator('#tab-automation .sub-tabs-inline[data-only-for="capi"]')).to_be_visible()
    # retour par le menu : #automation/pxe = Machines
    page.locator('#host-nav [data-hnav-root="automation"][data-hnav-tab="pxe"]').click()
    settle(page)
    assert visible_pane(page) == ["machines"]


def test_discovery_opens_in_a_window_and_lists_the_machines(context, flask_server):
    page = open_bm(context, flask_server)
    page.locator('#bmc-body [data-bm="discover-open"]').click()
    modal = page.locator("#bm-discover-modal")
    expect(modal).to_be_visible()
    for name in ("hosts", "user", "password"):
        assert modal.locator(f'[name="{name}"]').get_attribute("data-tip"), name
    page.keyboard.press("Escape")
    expect(modal).to_have_count(0)
    bm_discover(page, HOST, "bmc-secret")
    expect(page.locator("#bm-discover-modal")).to_have_count(0)
    row = page.locator(f'#bmc-discover-result tr.bm-machine[data-host="{HOST}"]')
    expect(row).to_contain_text("ProLiant XL170r Gen9")
    expect(row).to_contain_text("94:18:82:00:02:10")
    expect(row).to_contain_text("2 disk(s)")
    expect(row.locator(".bmc-install")).to_contain_text("Install")
    powers = row.locator(".bmc-power")
    expect(powers).to_have_count(4)
    for i in range(4):
        assert powers.nth(i).get_attribute("data-tip") and powers.nth(i).get_attribute("aria-label")
    # le geste ouvre la fenêtre d'installation d'avant
    row.locator(".bmc-install").click()
    expect(page.locator(f'[id="fp-bm-install-{HOST}"] #bm-install-form')).to_be_visible()


@pytest.fixture()
def real_memory(test_config):
    """Fichier de la dernière découverte du serveur de test, retiré après."""
    f = Path(test_config["root"]) / "state" / "baremetal-last-discovery.json"
    f.unlink(missing_ok=True)
    yield f
    f.unlink(missing_ok=True)


def test_the_last_discovery_is_remembered_after_a_reload(context, flask_server, real_memory):
    page = open_bm(context, flask_server, last=False)
    page.unroute("**/api/bmc/discover")                      # vraie route : BMC injoignable
    target = "127.0.0.1:9"
    page.locator('#bmc-body [data-bm="discover-open"]').click()
    form = page.locator("#bm-discover-modal #bmc-discover-form")
    form.locator('[name="hosts"]').fill(target)
    form.locator('[name="user"]').fill("e2e-user-210")
    form.locator('[name="password"]').fill(SECRET)
    form.locator('button[type="submit"]').click()
    row = page.locator(f'#bmc-discover-result tr.bm-machine-down[data-host="{target}"]')
    expect(row).to_contain_text("unreachable", timeout=20000)
    raw = real_memory.read_text()
    assert target in raw
    assert SECRET not in raw and "e2e-user-210" not in raw        # jamais d'identifiant
    page.reload(wait_until="domcontentloaded")
    settle(page)
    page.mouse.move(1100, 450)
    expect(page.locator(f'#bmc-discover-result tr[data-host="{target}"]')).to_be_visible()
    expect(page.locator("#bm-last-at")).to_contain_text("Last discovery:")
    expect(page.locator("#bm-last-at")).to_contain_text("1 BMC")
    # Relire sans identifiants (page rechargée) : la fenêtre les demande
    reread = page.locator('#bmc-body [data-bm="reread"]')
    expect(reread).to_be_visible()
    assert reread.get_attribute("data-tip")
    reread.click()
    modal = page.locator("#bm-discover-modal")
    expect(modal.locator(".bm-discover-reason")).to_be_visible()
    assert modal.locator('[name="hosts"]').input_value() == target
    assert modal.locator('[name="password"]').input_value() == ""


def test_mutation_a_password_in_the_state_file_is_seen(real_memory):
    """Mutation volontaire : un fichier qui porterait le mot de passe est vu."""
    real_memory.parent.mkdir(parents=True, exist_ok=True)
    real_memory.write_text(json.dumps({"hosts": ["192.0.2.1"], "nodes": [], "password": SECRET}))
    with pytest.raises(AssertionError):
        assert SECRET not in real_memory.read_text()


@pytest.mark.parametrize("isos,last,banner", [
    ({"isos": [], "disk_free": 1}, None, "No ISO in the store"),
    (ISOS, None, None),
    ({"isos": [], "disk_free": 1}, {"at": 1767000000, "hosts": [HOST], "nodes": [NODE]}, "No ISO in the store"),
])
def test_the_iso_missing_banner(context, flask_server, isos, last, banner):
    page = open_bm(context, flask_server, isos=isos, last=last)
    box = page.locator("#bm-iso-missing .bm-iso-missing")
    if banner is None:
        page.wait_for_timeout(400)
        expect(box).to_have_count(0)
        return
    expect(box).to_contain_text(banner)
    link = box.locator('[data-bm-goto="images"]')
    assert link.get_attribute("data-tip") and link.get_attribute("href") == "#automation/pxe/images"
    link.click()
    assert visible_pane(page) == ["images"]
    assert page.evaluate("location.hash") == "#automation/pxe/images"


@pytest.mark.parametrize("version,isos_of_version,store,expected", [
    ("v1.9.0", [], ISOS["isos"][:0] + [{"name": "harvester-v1.8.2-amd64.iso", "size": 1}],
     "No ISO of version v1.9.0 in the store"),
    ("v1.9.0", ["custom.iso"], [{"name": "custom.iso", "size": 1}], None),
    ("v1.9.0", [], ISOS["isos"], None),            # trouvé par son nom
    (None, [], ISOS["isos"], None),                # version illisible : rien n'est affirmé
])
def test_join_checks_the_iso_of_the_cluster_version(context, flask_server, version, isos_of_version, store,
                                                    expected):
    page = open_bm(context, flask_server, isos={"isos": store, "disk_free": 1},
                   last={"at": 1767000000, "hosts": [HOST], "nodes": [NODE]})
    page.route("**/api/clusters/harv-fake/join-url", lambda r, q: fulfill(r, {
        "cluster": "harv-fake", "join_url": "https://192.0.2.100:443", "reason": None,
        "harvester_version": version, "isos_of_version": isos_of_version}))
    page.evaluate("BMC.openJoin('harv-fake')")
    settle(page)
    expect(page.locator("#bm-join-target")).to_contain_text("harv-fake")
    expect(page.locator(f'tr.bm-machine[data-host="{HOST}"] .bmc-install')).to_contain_text("Join harv-fake")
    box = page.locator("#bm-iso-missing .bm-iso-missing")
    if expected is None:
        page.wait_for_timeout(400)
        expect(box).to_have_count(0)
    else:
        expect(box).to_contain_text(expected)
    # annuler la jonction rend « Installer » et retire le bandeau de version
    page.locator('[data-bm="join-cancel"]').click()
    expect(page.locator("#bm-join-target")).to_have_count(0)
    expect(page.locator(f'tr.bm-machine[data-host="{HOST}"] .bmc-install')).to_contain_text("Install")
    expect(box).to_have_count(0)


ACT = {
    "baremetal-": {"in_progress": [{"id": "a00000000001", "action": "baremetal-install:node7", "status": "running",
                                    "started_at": 1767000100, "cluster": "(local)"}],
                   "actions_done": [{"id": "a00000000002", "action": "baremetal-discover:192.0.2.41", "status": "done",
                                     "started_at": 1767000000, "ended_at": 1767000090, "cluster": "(local)"},
                                    {"id": "a00000000003", "action": "baremetal-batch:rack-a", "status": "error",
                                     "started_at": 1766990000, "ended_at": 1766990600, "cluster": "(local)"}]},
    "bmc-power": {"in_progress": [], "actions_done": [
        {"id": "a00000000004", "action": "bmc-power:192.0.2.41:On", "status": "done",
         "started_at": 1766980000, "ended_at": 1766980002, "cluster": "192.0.2.41"}]},
}


def test_history_is_activity_filtered_on_bare_metal_actions(context, flask_server):
    page = open_bm(context, flask_server)
    asked = []

    def activity(route, req):
        q = parse_qs(urlparse(req.url).query)
        asked.append(q.get("action", [""])[0])
        fulfill(route, ACT.get(q.get("action", [""])[0], {"in_progress": [], "actions_done": []}))
    page.route(re.compile(r".*/api/activity\?.*"), activity)
    page.locator('#tab-automation [data-bm-tab="history"]').click()
    rows = page.locator("#bm-history tr.bm-history-row")
    expect(rows).to_have_count(4)
    assert sorted(set(asked)) == ["baremetal-", "bmc-power"]
    tables = page.locator("#bm-history table.bm-history")
    expect(tables).to_have_count(2)                                    # en cours, puis passées
    expect(tables.nth(0)).to_contain_text("Install or join")
    expect(tables.nth(0)).to_contain_text("node7")
    expect(tables.nth(1).locator("tr.bm-history-row").first).to_contain_text("Discovery boot")
    expect(tables.nth(1)).to_contain_text("Batch")
    expect(tables.nth(1)).to_contain_text("Power")
    for i in range(4):
        link = rows.nth(i).locator("a.bm-history-log")
        aid = rows.nth(i).get_attribute("data-action-id")
        assert link.get_attribute("href") == f"#activity/action/{aid}"
        assert link.get_attribute("data-tip")
    rows.nth(1).locator("a.bm-history-log").click()
    page.wait_for_function("() => location.hash === '#activity/action/a00000000002'")
    settle(page)
    expect(page.locator("#tab-activity")).to_have_class(re.compile(r"\bactive\b"))


@pytest.mark.parametrize("status,body,expected", [
    (500, {"error": "boom"}, "Activity could not be read."),
    (200, {"in_progress": [], "actions_done": []}, "No bare-metal action yet."),
])
def test_history_error_and_empty_paths(context, flask_server, status, body, expected):
    page = open_bm(context, flask_server)
    page.route(re.compile(r".*/api/activity\?.*"), lambda r, q: fulfill(r, body, status))
    page.locator('#tab-automation [data-bm-tab="history"]').click()
    expect(page.locator("#bm-history")).to_contain_text(expected)
    expect(page.locator("#bm-history tr.bm-history-row")).to_have_count(0)


def test_history_escapes_what_it_shows(context, flask_server):
    page = open_bm(context, flask_server)
    evil = {"in_progress": [], "actions_done": [{"id": "a00000000009", "status": "done", "started_at": 1,
                                                 "action": 'baremetal-install:<img src=x onerror="window.__pwn=1">'}]}
    page.route(re.compile(r".*/api/activity\?.*"), lambda r, q: fulfill(r, evil))
    page.locator('#tab-automation [data-bm-tab="history"]').click()
    expect(page.locator("#bm-history tr.bm-history-row")).to_have_count(1)
    assert page.locator("#bm-history img").count() == 0
    assert page.evaluate("window.__pwn") is None


def test_history_reads_a_running_action_again_only_while_shown(context, flask_server):
    page = open_bm(context, flask_server)
    asked = []

    def activity(route, req):
        asked.append(req.url)
        fulfill(route, ACT["baremetal-"] if "baremetal-" in req.url else ACT["bmc-power"])
    page.route(re.compile(r".*/api/activity\?.*"), activity)
    page.locator('#tab-automation [data-bm-tab="history"]').click()
    expect(page.locator("#bm-history tr.bm-history-row")).to_have_count(4)
    page.wait_for_function("() => true")
    page.wait_for_timeout(5600)
    assert len(asked) >= 4                                   # relue : une action tourne
    page.evaluate("Router.go('#vms')")
    settle(page)
    page.wait_for_timeout(600)
    n = len(asked)
    page.wait_for_timeout(5600)
    assert len(asked) == n                                   # écran quitté : plus de relecture
