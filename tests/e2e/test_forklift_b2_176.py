"""v1.76.0 : la Préparation gagne l'importeur de disques (CDI) et l'intervalle
des copies incrémentales ; l'inventaire dit les outils VMware, pourquoi une
VM ne peut pas migrer à chaud, et permet de la sélectionner."""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

ARCHIVE = "VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz"
READY = {"ready": True, "cert_manager": True, "cert_manager_missing": [], "addon": "ready",
         "addon_message": "the forklift-operator add-on is deployed", "operator": True, "controller": True,
         "components_missing": [], "running": True, "inventory_access": True}
DATA = {"cluster": "harv-fake", "install": READY, "harvester_addon": False, "bundle": True,
        "vddk": {"image": "172.16.1.11:5005/harvops/vddk:8.0.3", "digest": "sha256:" + "4f1c" * 16,
                 "archive": ARCHIVE, "pushed_at": "2026-09-28T20:00:00Z"},
        "registry": {"image": "172.16.1.11:5005/harvops/vddk:8.0.3", "host": "172.16.1.11:5005",
                     "plain_http": True, "auth": True},
        "providers": [{"name": "vmwlab", "namespace": "forklift", "url": "https://vmwlab-vc.home.lo/sdk", "ready": True,
                       "message": "ready: vCenter reached, inventory loaded",
                       "vddk_image": "172.16.1.11:5005/harvops/vddk:8.0.3", "plans": [], "managed": True}],
        "vmimport_sources": [{"namespace": "mig", "name": "vc", "endpoint": "https://vmwlab-vc.home.lo/sdk"}],
        "cdi_importer": {"image": "registry.suse.com/harvester/cdi-importer:v1.65.0", "kind": "suse-no-vddk", "original": ""},
        "precopy_interval": 60, "waves": []}
STORE = {"archives": [{"name": ARCHIVE, "version": "8.0.3", "size": 41626848, "mtime": 1790000000}], "free": 10 ** 11}

# Deux VMs éligibles (CBT actif, une allumée avec ses outils, une éteinte),
# une refusée par CBT, une refusée par les outils VMware (allumée, sans eux).
VMS_ROWS = [
    {"id": "vm-16", "name": "vmwlab-src-1", "path": "/dc/vm/vmwlab-src-1", "power": "poweredOn", "cbt": True,
     "cpus": 1, "memory_mib": 1024, "guest": "Debian GNU/Linux 11 (64-bit)", "disks": [{"datastore": "datastore-12", "capacity": 10737418240}],
     "networks": ["network-13"], "concerns": [], "tools": True, "snapshot": "", "uuid": "uuid-16"},
    {"id": "vm-17", "name": "vmwlab-off", "path": "/dc/vm/vmwlab-off", "power": "poweredOff", "cbt": True,
     "cpus": 1, "memory_mib": 1024, "guest": "Debian GNU/Linux 11 (64-bit)", "disks": [], "networks": [],
     "concerns": [], "tools": False, "snapshot": "", "uuid": "uuid-17"},
    {"id": "vm-18", "name": "vmwlab-no-cbt", "path": "/dc/vm/vmwlab-no-cbt", "power": "poweredOn", "cbt": False,
     "cpus": 1, "memory_mib": 1024, "guest": "Debian GNU/Linux 11 (64-bit)", "disks": [], "networks": [],
     "concerns": [], "tools": True, "snapshot": "", "uuid": "uuid-18"},
    {"id": "vm-19", "name": "vmwlab-src-3", "path": "/dc/vm/vmwlab-src-3", "power": "poweredOn", "cbt": True,
     "cpus": 2, "memory_mib": 4096, "guest": "Microsoft Windows Server 2019 (64-bit)", "disks": [], "networks": [],
     "concerns": [], "tools": False, "snapshot": "", "uuid": "uuid-19"},
]


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def open_tab(context, flask_server, data, section="prep", lang="en"):
    context.add_init_script(
        f"localStorage.setItem('harvester_ops_language','{lang}');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        f"localStorage.setItem('harvester_ops_section_forklift','{section}');"
        "localStorage.setItem('harvester_ops_current_tab','forklift');")
    page = context.new_page()
    sent = []

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.method, req.post_data_json if req.method == "POST" else None))
        fulfill(route, {"action_id": "fk0000000176"}, 202)
    page.route(re.compile(r".*/api/forklift/harv-fake(\?.*)?$"), lambda r, q: fulfill(r, data() if callable(data) else data))
    page.route("**/api/forklift/harv-fake/do/**", writes)
    page.route("**/api/forklift-vddk", lambda r, q: fulfill(r, STORE))
    page.route("**/api/forklift/harv-fake/inventory/vmwlab/vms*", lambda r, q: fulfill(r, {"rows": VMS_ROWS}))
    page.route("**/api/stream/fk0000000176", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.Forklift && window.Sections && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    return page, sent


# --- Préparation : importeur CDI ------------------------------------------

def test_the_suse_importer_is_shown_in_red_with_its_explanation(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA)
    cdi = page.locator('#tab-forklift [data-fk-step="cdi"]')
    expect(cdi).to_contain_text("SUSE image, no VDDK")
    expect(cdi.locator(".sto-finding.sev-critical")).to_be_visible()
    expect(cdi.locator('[data-fk="cdi-upstream"]')).to_be_enabled()
    expect(cdi.locator('[data-fk="cdi-original"]')).to_be_disabled()


def test_switching_to_the_upstream_importer_is_sent_with_the_mirror_image(context, flask_server):
    page, sent = open_tab(context, flask_server, DATA)
    cdi = page.locator('#tab-forklift [data-fk-step="cdi"]')
    cdi.locator('[name="cdi_image"]').fill("172.16.1.11:5005/harvops/cdi-importer:v1.65.0")
    cdi.locator('[data-fk="cdi-upstream"]').click()
    page.wait_for_timeout(300)
    url, method, body = sent[-1]
    assert url == "api/forklift/harv-fake/do/cdi-importer"
    assert body == {"mode": "upstream", "image": "172.16.1.11:5005/harvops/cdi-importer:v1.65.0"}


def test_switching_to_the_upstream_importer_without_a_mirror_omits_the_image(context, flask_server):
    page, sent = open_tab(context, flask_server, DATA)
    page.locator('#tab-forklift [data-fk-step="cdi"] [data-fk="cdi-upstream"]').click()
    page.wait_for_timeout(300)
    assert sent[-1][2] == {"mode": "upstream"}


def test_the_upstream_importer_is_shown_in_green_and_can_go_back_to_the_original(context, flask_server):
    upstream = {**DATA, "cdi_importer": {"image": "quay.io/kubevirt/cdi-importer:v1.60.0", "kind": "upstream",
                                          "original": "registry.suse.com/harvester/cdi-importer:v1.65.0"}}
    page, sent = open_tab(context, flask_server, upstream)
    cdi = page.locator('#tab-forklift [data-fk-step="cdi"]')
    expect(cdi).to_contain_text("Upstream image")
    expect(cdi.locator(".sto-finding.sev-critical")).to_have_count(0)
    expect(cdi.locator('[data-fk="cdi-upstream"]')).to_be_disabled()
    original_btn = cdi.locator('[data-fk="cdi-original"]')
    expect(original_btn).to_be_enabled()
    assert "registry.suse.com" in (original_btn.get_attribute("data-tip") or "")
    original_btn.click()
    page.wait_for_timeout(300)
    assert sent[-1] == ("api/forklift/harv-fake/do/cdi-importer", "POST", {"mode": "original"})


def test_the_upgrade_warning_is_always_shown(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA)
    expect(page.locator('#tab-forklift [data-fk-step="cdi"]')).to_contain_text("upgrade")


# --- Préparation : intervalle des copies -----------------------------------

def test_the_precopy_interval_proposes_the_current_value(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA)
    box = page.locator('#tab-forklift [data-fk-precopy]')
    expect(box).to_contain_text("whole cluster")
    expect(box.locator('[name="precopy_minutes"]')).to_have_value("60")


def test_saving_the_interval_is_sent_in_minutes(context, flask_server):
    page, sent = open_tab(context, flask_server, DATA)
    box = page.locator('#tab-forklift [data-fk-precopy]')
    box.locator('[name="precopy_minutes"]').fill("15")
    box.locator('[data-fk="precopy-save"]').click()
    page.wait_for_timeout(300)
    assert sent[-1] == ("api/forklift/harv-fake/do/precopy-interval", "POST", {"minutes": 15})


def test_an_interval_out_of_bounds_is_refused_before_sending(context, flask_server):
    page, sent = open_tab(context, flask_server, DATA)
    box = page.locator('#tab-forklift [data-fk-precopy]')
    box.locator('[name="precopy_minutes"]').fill("4")
    box.locator('[data-fk="precopy-save"]').click()
    page.wait_for_timeout(300)
    assert not sent
    expect(box.locator('[data-fk="precopy-msg"]')).to_contain_text("5")


# --- Préparation : une saisie survit à la relecture de fond ------------------

def test_the_importer_mirror_being_typed_survives_the_background_refresh(context, flask_server):
    reads = []

    def data():
        reads.append(1)
        return DATA
    page, _ = open_tab(context, flask_server, data)
    cdi = page.locator('#tab-forklift [data-fk-step="cdi"]')
    cdi.locator('[name="cdi_image"]').fill("172.16.1.11:5005/harvops/cdi-importer:v1.65.0")
    n = len(reads)
    page.evaluate("Forklift.backgroundRefresh()")
    page.wait_for_timeout(400)
    assert len(reads) > n
    expect(cdi.locator('[name="cdi_image"]')).to_have_value("172.16.1.11:5005/harvops/cdi-importer:v1.65.0")


def test_the_interval_being_typed_survives_the_background_refresh(context, flask_server):
    reads = []

    def data():
        reads.append(1)
        return DATA
    page, _ = open_tab(context, flask_server, data)
    box = page.locator('#tab-forklift [data-fk-precopy]')
    box.locator('[name="precopy_minutes"]').fill("15")
    n = len(reads)
    page.evaluate("Forklift.backgroundRefresh()")
    page.wait_for_timeout(400)
    assert len(reads) > n
    expect(box.locator('[name="precopy_minutes"]')).to_have_value("15")


def test_an_untouched_interval_follows_the_cluster_value(context, flask_server):
    state = {"minutes": 60}
    page, _ = open_tab(context, flask_server, lambda: {**DATA, "precopy_interval": state["minutes"]})
    box = page.locator('#tab-forklift [data-fk-precopy]')
    expect(box.locator('[name="precopy_minutes"]')).to_have_value("60")
    state["minutes"] = 15
    page.evaluate("Forklift.backgroundRefresh()")
    expect(box.locator('[name="precopy_minutes"]')).to_have_value("15")


# --- Inventaire : outils VMware, raison, sélection --------------------------

def test_the_tools_column_says_running_or_not(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA, section="inventory")
    page.evaluate("Forklift.openInventory('vmwlab')")
    table = page.locator('#tab-forklift [data-fk="inv-table"]')
    expect(table.locator('tr[data-vm="vmwlab-src-1"] [data-fk-tools="yes"]')).to_have_count(1)
    expect(table.locator('tr[data-vm="vmwlab-no-cbt"] [data-fk-tools="yes"]')).to_have_count(1)
    expect(table.locator('tr[data-vm="vmwlab-src-3"] [data-fk-tools="no"]')).to_have_count(1)


def test_the_reason_column_explains_why_a_vm_is_refused(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA, section="inventory")
    page.evaluate("Forklift.openInventory('vmwlab')")
    table = page.locator('#tab-forklift [data-fk="inv-table"]')
    no_cbt = table.locator('tr[data-vm="vmwlab-no-cbt"] [data-fk-eligible]')
    expect(no_cbt).to_have_attribute("data-fk-eligible", "no")
    expect(no_cbt).to_contain_text("Changed Block Tracking")
    no_tools = table.locator('tr[data-vm="vmwlab-src-3"] [data-fk-eligible]')
    expect(no_tools).to_have_attribute("data-fk-eligible", "no")
    expect(no_tools).to_contain_text("VMware Tools")
    eligible = table.locator('tr[data-vm="vmwlab-src-1"] [data-fk-eligible]')
    expect(eligible).to_have_attribute("data-fk-eligible", "yes")
    expect(eligible).to_contain_text("eligible")


def test_only_eligible_vms_can_be_checked(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA, section="inventory")
    page.evaluate("Forklift.openInventory('vmwlab')")
    table = page.locator('#tab-forklift [data-fk="inv-table"]')
    expect(table.locator('tr[data-vm="vmwlab-no-cbt"] [data-fk="vm-select"]')).to_be_disabled()
    expect(table.locator('tr[data-vm="vmwlab-src-3"] [data-fk="vm-select"]')).to_be_disabled()
    expect(table.locator('tr[data-vm="vmwlab-src-1"] [data-fk="vm-select"]')).to_be_enabled()
    expect(table.locator('tr[data-vm="vmwlab-off"] [data-fk="vm-select"]')).to_be_enabled()


def test_selecting_vms_updates_the_count_and_selected_vms(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA, section="inventory")
    page.evaluate("Forklift.openInventory('vmwlab')")
    table = page.locator('#tab-forklift [data-fk="inv-table"]')
    expect(page.locator('[data-fk="inv-selected-count"]')).to_contain_text("0")
    table.locator('tr[data-vm="vmwlab-src-1"] [data-fk="vm-select"]').check()
    table.locator('tr[data-vm="vmwlab-off"] [data-fk="vm-select"]').check()
    expect(page.locator('[data-fk="inv-selected-count"]')).to_contain_text("2")
    ids = page.evaluate("Forklift.selectedVms().map(v => v.id).sort()")
    assert ids == ["vm-16", "vm-17"]
    names = page.evaluate("Forklift.selectedVms().map(v => v.name)")
    assert "vmwlab-src-1" in names and "vmwlab-off" in names
    # la source accompagne chaque VM sélectionnée (utile pour W6)
    src = page.evaluate("Forklift.selectedVms()[0].source")
    assert src == "vmwlab"


def test_the_compose_wave_button_opens_the_composer_once_a_vm_is_selected(context, flask_server):
    """W6 : le bouton de l'inventaire ouvre la fenêtre « Composer une vague »
    dès qu'une VM éligible est cochée."""
    page, _ = open_tab(context, flask_server, DATA_T, section="inventory")
    page.evaluate("Forklift.openInventory('vmwlab')")
    table = page.locator('#tab-forklift [data-fk="inv-table"]')
    btn = page.locator('[data-fk="compose-wave"]')
    expect(btn).to_be_disabled()   # rien de coché encore
    table.locator('tr[data-vm="vmwlab-src-1"] [data-fk="vm-select"]').check()
    expect(btn).to_be_enabled()
    btn.click()
    expect(page.locator("#fp-fk-wave-new-harv-fake [data-fk-net]")).to_have_count(1)


def test_switching_source_resets_the_selection(context, flask_server):
    two = {**DATA, "providers": [DATA["providers"][0], {**DATA["providers"][0], "name": "vmwlab2"}]}
    page, _ = open_tab(context, flask_server, two, section="inventory")
    page.route("**/api/forklift/harv-fake/inventory/vmwlab2/vms*", lambda r, q: fulfill(r, {"rows": []}))
    page.evaluate("Forklift.openInventory('vmwlab')")
    table = page.locator('#tab-forklift [data-fk="inv-table"]')
    table.locator('tr[data-vm="vmwlab-src-1"] [data-fk="vm-select"]').check()
    expect(page.locator('[data-fk="inv-selected-count"]')).to_contain_text("1")
    page.locator('#tab-forklift [name="source"]').select_option("forklift/vmwlab2")
    expect(page.locator('[data-fk="inv-selected-count"]')).to_contain_text("0")


# --- W6 : onglet Vagues ------------------------------------------------------
#
# Les états de vague viennent de la vraie fonction (hv_forklift.wave_state)
# appliquée aux objets relevés sur le banc (fixtures api), puis déclinés.

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import hv_forklift as hf  # noqa: E402

_FIX = ROOT / "tests" / "api" / "fixtures"
_PLANS = json.loads((_FIX / "forklift_b2_plans_176.json").read_text())
_MIGS = json.loads((_FIX / "forklift_b2_migrations_176.json").read_text())
_PLANS = _PLANS.get("items", _PLANS) if isinstance(_PLANS, dict) else _PLANS
_MIGS = _MIGS.get("items", _MIGS) if isinstance(_MIGS, dict) else _MIGS
REAL = {w["name"]: w for w in (hf.wave_state(p, _MIGS) for p in _PLANS)}
SUCCEEDED = REAL["vague-1"]          # vm-16, 3 copies, la dernière de 62 s


def iso(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def variant(name, state, **vm):
    w = json.loads(json.dumps(SUCCEEDED))
    w.update(name=name, state=state)
    w["vms"][0].update(vm)
    return w


def copying(name="w-copy"):
    nxt = iso(12)
    w = variant(name, "copying", phase="Running", step="copying disks", step_name="DiskTransfer",
                progress={"done": 5120, "total": 10240}, next_precopy=nxt)
    w.update(next_precopy=nxt, cutover=None)
    return w


def scheduled(name="w-sched", minutes=90):
    w = copying(name)
    w.update(state="cutover-scheduled", cutover=iso(minutes))
    return w


def failed(name="w-fail"):
    w = variant(name, "failed", phase="Failed", error="Unable to connect to vddk data source")
    w["vms"].append({**w["vms"][0], "id": "vm-20", "name": "vmwlab-src-4",
                     "error": "Cannot complete operation because VMware Tools is not running"})
    w["message"] = "; ".join(v["error"] for v in w["vms"])
    return w


READY_WAVE = variant("w-ready", "ready", phase="", step="", step_name="", progress={"done": 0, "total": 0},
                     precopies=0, last_precopy=None)
READY_WAVE.update(migration=None, cutover=None)

TARGETS = {"nads": ["default/lab-net", "mig-b2/vlan1"], "classes": ["harv-rep1", "harvester-longhorn"],
           "default_class": "harvester-longhorn", "namespaces": ["default", "mig-b2"]}
DATA_T = {**DATA, **TARGETS}
NETS = [{"id": "network-13", "name": "lab-net", "path": "/dc/network/lab-net"},
        {"id": "network-14", "name": "VM Network", "path": "/dc/network/VM Network"}]
STORES = [{"id": "datastore-12", "name": "datastore1", "path": "/dc/datastore/datastore1",
           "capacity": 10 ** 12, "free": 10 ** 11}]
WINDOWS_VM = {**VMS_ROWS[3], "id": "vm-20", "name": "win-ok", "tools": True, "networks": ["network-14"],
              "disks": [{"datastore": "datastore-12", "capacity": 10 ** 10}]}


def open_waves(context, flask_server, waves, section="waves", lang="en"):
    page, sent = open_tab(context, flask_server, {**DATA_T, "waves": waves}, section=section, lang=lang)
    page.route("**/api/forklift/harv-fake/inventory/vmwlab/networks*", lambda r, q: fulfill(r, {"rows": NETS}))
    page.route("**/api/forklift/harv-fake/inventory/vmwlab/datastores*", lambda r, q: fulfill(r, {"rows": STORES}))
    dialogs = []
    page.on("dialog", lambda d: dialogs.append(d.message))
    return page, sent, dialogs


def wave_box(page, name):
    return page.locator(f'#tab-forklift [data-fk-wave="{name}"]')


def test_the_waves_tab_is_a_sub_tab_of_vmware_migrations(context, flask_server):
    page, _, _ = open_waves(context, flask_server, [])
    tab = page.locator('#tab-forklift [data-section-tab="waves"]')
    expect(tab).to_be_visible()
    assert tab.get_attribute("data-tip") or tab.get_attribute("title")
    expect(page.locator("#tab-forklift")).to_contain_text("No wave yet")


def test_each_wave_shows_its_state_and_only_the_actions_it_allows(context, flask_server):
    page, _, _ = open_waves(context, flask_server, [READY_WAVE, copying(), SUCCEEDED, scheduled()])
    expect(page.locator("#tab-forklift [data-fk-wave]")).to_have_count(4)
    acts = lambda n: sorted(b.get_attribute("data-fk") for b in wave_box(page, n).locator("button").all())  # noqa: E731
    expect(wave_box(page, "w-ready").locator("[data-fk-state]")).to_have_attribute("data-fk-state", "ready")
    assert acts("w-ready") == ["wave-close", "wave-delete", "wave-follow", "wave-start"]
    assert acts("w-copy") == ["wave-cutover", "wave-follow", "wave-schedule"]
    assert acts("vague-1") == ["wave-close", "wave-delete", "wave-follow", "wave-rollback"]
    assert acts("w-sched") == ["wave-cutover", "wave-follow", "wave-schedule"]
    expect(wave_box(page, "w-copy").locator('[data-fk="wave-next"] [data-fk-at]')).to_contain_text("in ")
    expect(wave_box(page, "w-sched").locator('[data-fk="wave-cutover-at"] [data-fk-at]')).to_contain_text("in 1 h")
    for b in page.locator("#tab-forklift button:visible").all():
        assert b.get_attribute("data-tip") or b.get_attribute("title"), b.inner_text()


def test_composing_a_wave_from_the_inventory_sends_the_maps_and_ticks_raw_copy_for_linux(context, flask_server):
    page, sent, _ = open_waves(context, flask_server, [], section="inventory")
    page.evaluate("Forklift.openInventory('vmwlab')")
    table = page.locator('#tab-forklift [data-fk="inv-table"]')
    table.locator('tr[data-vm="vmwlab-src-1"] [data-fk="vm-select"]').check()
    table.locator('tr[data-vm="vmwlab-off"] [data-fk="vm-select"]').check()
    page.locator('[data-fk="compose-wave"]').click()
    win = page.locator("#fp-fk-wave-new-harv-fake")
    # le réseau vCenter « lab-net » va vers le réseau de VM du même nom, le datastore vers la classe par défaut
    expect(win.locator('[data-fk-net="network-13"]')).to_have_value("default/lab-net")
    expect(win.locator('[data-fk-sto="datastore-12"]')).to_have_value("harvester-longhorn")
    expect(win.locator('[name="skip_conversion"]')).to_be_checked()
    expect(win.locator('[data-fk="raw-linux"]')).to_be_visible()
    expect(win).to_contain_text("1 min 44 s")
    win.locator('[name="name"]').fill("wave-a")
    win.locator('[name="target_namespace"]').select_option("mig-b2")
    win.locator('button[type="submit"]').click()
    page.wait_for_timeout(300)
    url, method, body = sent[-1]
    assert url == "api/forklift/harv-fake/do/wave-apply"
    assert body == {"spec": {"name": "wave-a", "target_namespace": "mig-b2",
                             "provider": {"namespace": "forklift", "name": "vmwlab"},
                             "vms": ["vm-16", "vm-17"],
                             "networks": [{"source": "network-13", "destination": "default/lab-net"}],
                             "storages": [{"source": "datastore-12", "storage_class": "harvester-longhorn"}],
                             "skip_conversion": True, "compat_mode": False, "preserve_static_ips": False}}


def test_a_windows_guest_gets_the_verified_raw_copy_and_can_still_ask_for_the_conversion(context, flask_server):
    """v1.87.0 : vu en réel, la conversion de Harvester 1.9 (virt-v2v 2.7.7)
    échoue sur Windows Server 2025 après l'arrêt de la source ; la copie
    brute en mode compatibilité démarre. C'est elle qui est cochée, avec
    l'avertissement sur l'IP fixe ; décocher revient à la conversion."""
    page, sent, _ = open_waves(context, flask_server, [])
    page.evaluate("vms => Forklift.composeWave(vms)",
                  [{**VMS_ROWS[0], "namespace": "forklift", "source": "vmwlab"},
                   {**WINDOWS_VM, "namespace": "forklift", "source": "vmwlab"}])
    win = page.locator("#fp-fk-wave-new-harv-fake")
    expect(win.locator('[data-fk="raw-windows"]')).to_contain_text("Windows Server 2025")
    expect(win.locator('[data-fk="raw-windows-ip"]')).to_contain_text("DHCP")
    expect(win.locator('[name="skip_conversion"]')).to_be_checked()
    expect(win.locator('[name="compat_mode"]')).to_be_checked()
    expect(win.locator('[name="compat_mode"]')).to_be_enabled()
    win.locator('[name="skip_conversion"]').uncheck()
    expect(win.locator('[name="compat_mode"]')).to_be_disabled()
    expect(win.locator('[name="compat_mode"]')).not_to_be_checked()
    expect(win.locator('[data-fk-net="network-14"]')).to_have_value("pod")
    for el in win.locator("select, input:not([type=hidden]), button").all():
        assert el.get_attribute("data-tip") or el.get_attribute("title") or el.evaluate(
            "e => !!e.closest('.tip[data-tip]')"), el.evaluate("e => e.outerHTML")
    win.locator('[name="name"]').fill("wave-w")
    win.locator('[name="preserve_static_ips"]').check()
    win.locator('button[type="submit"]').click()
    page.wait_for_timeout(300)
    spec = sent[-1][2]["spec"]
    assert spec["skip_conversion"] is False and spec["compat_mode"] is False and spec["preserve_static_ips"] is True
    assert spec["target_namespace"] == "default"
    assert spec["networks"] == [{"source": "network-13", "destination": "default/lab-net"},
                                {"source": "network-14", "destination": "pod"}]
    assert spec["storages"] == [{"source": "datastore-12", "storage_class": "harvester-longhorn"}]


def test_a_vm_taken_on_another_cluster_is_refused_with_the_server_message(context, flask_server):
    page, _, _ = open_waves(context, flask_server, [])
    # un cluster injoignable n'a pas pu dire s'il tenait déjà ces VMs : le
    # message d'erreur du serveur reste montré, et la liste des clusters
    # sautés (portée par l'erreur jetée) apparaît en dessous
    page.route("**/api/forklift/harv-fake/do/wave-apply", lambda r, q: fulfill(
        r, {"error": "vm-16 is already in wave vague-b on cluster harv3", "skipped": ["harv4"]}, 409))
    page.evaluate("vms => Forklift.composeWave(vms)", [{**VMS_ROWS[0], "namespace": "forklift", "source": "vmwlab"}])
    win = page.locator("#fp-fk-wave-new-harv-fake")
    win.locator('[name="name"]').fill("wave-b")
    win.locator('button[type="submit"]').click()
    expect(win.locator(".of-msg .res-error")).to_have_text("vm-16 is already in wave vague-b on cluster harv3")
    expect(win.locator(".of-msg")).to_contain_text("harv4")


def test_start_is_sent_for_the_wave(context, flask_server):
    page, sent, _ = open_waves(context, flask_server, [READY_WAVE])
    wave_box(page, "w-ready").locator('[data-fk="wave-start"]').click()
    page.wait_for_timeout(300)
    assert sent[-1] == ("api/forklift/harv-fake/do/wave-start", "POST", {"wave": "w-ready"})


def test_switching_over_now_is_confirmed_with_the_source_shutdown(context, flask_server):
    page, sent, dialogs = open_waves(context, flask_server, [copying()])
    wave_box(page, "w-copy").locator('[data-fk="wave-cutover"]').click()
    page.wait_for_timeout(300)
    assert "VMware Tools" in dialogs[-1] and "shut down" in dialogs[-1]
    assert sent[-1] == ("api/forklift/harv-fake/do/wave-cutover", "POST", {"wave": "w-copy"})


def test_a_scheduled_switchover_is_sent_in_utc(context, flask_server):
    page, sent, dialogs = open_waves(context, flask_server, [copying()])
    wave_box(page, "w-copy").locator('[data-fk="wave-schedule"]').click()
    win = page.locator("#fp-fk-wave-sched-harv-fake-w-copy")
    win.locator('[name="at"]').fill("2031-01-02T03:04")
    win.locator('button[type="submit"]').click()
    page.wait_for_timeout(300)
    assert "VMware Tools" in dialogs[-1]
    url, _, body = sent[-1]
    assert url == "api/forklift/harv-fake/do/wave-cutover"
    expected = page.evaluate("new Date('2031-01-02T03:04').toISOString().replace(/\\.\\d{3}Z$/, 'Z')")
    assert body == {"wave": "w-copy", "at": expected}


def test_rolling_back_is_confirmed_with_the_loss_of_writes(context, flask_server):
    page, sent, dialogs = open_waves(context, flask_server, [SUCCEEDED])
    wave_box(page, "vague-1").locator('[data-fk="wave-rollback"]').click()
    page.wait_for_timeout(300)
    assert "since the switchover is lost" in dialogs[-1]
    assert sent[-1] == ("api/forklift/harv-fake/do/wave-rollback", "POST", {"wave": "vague-1"})


def test_closing_removes_forklift_snapshots_by_default(context, flask_server):
    page, sent, _ = open_waves(context, flask_server, [SUCCEEDED])
    wave_box(page, "vague-1").locator('[data-fk="wave-close"]').click()
    win = page.locator("#fp-fk-wave-close-harv-fake-vague-1")
    expect(win.locator('[name="clean_snapshots"]')).to_be_checked()
    win.locator('button[type="submit"]').click()
    page.wait_for_timeout(300)
    assert sent[-1] == ("api/forklift/harv-fake/do/wave-close", "POST", {"wave": "vague-1", "clean_snapshots": True})
    win.locator('[name="clean_snapshots"]').uncheck()
    win.locator('button[type="submit"]').click()
    page.wait_for_timeout(300)
    assert sent[-1][2] == {"wave": "vague-1", "clean_snapshots": False}


def test_a_closed_wave_offers_removing_forklift_snapshots(context, flask_server):
    closed = variant("w-closed", "closed")
    page, sent, _ = open_waves(context, flask_server, [closed])
    box = wave_box(page, "w-closed")
    btn = box.locator('[data-fk="wave-clean-snapshots"]')
    expect(btn).to_be_visible()
    assert btn.get_attribute("data-tip") or btn.get_attribute("title")
    btn.click()
    page.wait_for_timeout(300)
    assert sent[-1] == ("api/forklift/harv-fake/do/wave-close", "POST", {"wave": "w-closed", "clean_snapshots": True})


def test_deleting_is_confirmed_and_says_the_vms_stay(context, flask_server):
    page, sent, dialogs = open_waves(context, flask_server, [SUCCEEDED])
    wave_box(page, "vague-1").locator('[data-fk="wave-delete"]').click()
    page.wait_for_timeout(300)
    assert "migrated VMs stay" in dialogs[-1]
    assert sent[-1] == ("api/forklift/harv-fake/do/wave-delete", "POST", {"wave": "vague-1"})


def test_the_follow_window_shows_each_vm_step_progress_and_copies(context, flask_server):
    # DiskTransfer/Cutover : `completed`/`total` sont des Mio, montrés en
    # taille (5120/10240 Mio = 5.0 Gio / 10 Gio), jamais un pourcentage brut
    page, _, _ = open_waves(context, flask_server, [copying(), SUCCEEDED])
    wave_box(page, "w-copy").locator('[data-fk="wave-follow"]').click()
    row = page.locator('#fp-fk-wave-follow-harv-fake-w-copy [data-fk-vm="vm-16"]')
    expect(row).to_contain_text("vmwlab-src-1")
    expect(row).to_contain_text("copying disks")
    expect(row.locator('[data-fk="vm-pct"]')).to_have_text("5.0 GiB / 10 GiB")
    expect(row.locator('[data-fk="vm-copies"]')).to_have_text("3")
    expect(row.locator('[data-fk="vm-last"]')).to_have_text("1 min 02 s")
    expect(row.locator('[data-fk="vm-next"] [data-fk-at]')).to_contain_text("in 1")
    # une vague réussie : étape finie (VirtualMachineCreation), l'étape suffit
    # (pas de pourcentage, un 0/1 fini ne veut rien dire), retour par VM possible
    page.locator('#fp-fk-wave-follow-harv-fake-w-copy [data-action="close"]').click()
    wave_box(page, "vague-1").locator('[data-fk="wave-follow"]').click()
    done = page.locator('#fp-fk-wave-follow-harv-fake-vague-1 [data-fk-vm="vm-16"]')
    expect(done.locator('[data-fk="vm-pct"]')).to_have_text("–")
    expect(done.locator('[data-fk="vm-next"]')).to_have_text("–")
    expect(done.locator("[data-fk-vm-rollback]")).to_be_visible()


def copying_paused(name="w-paused"):
    """Vu en réel : entre deux copies incrémentales, `phase: CopyingPaused`
    doit se lire comme une attente, jamais comme une bascule commencée."""
    nxt = iso(6)
    w = variant(name, "copying", phase="CopyingPaused", step="waiting for the next copy",
                step_name="CopyingPaused", progress={"done": 10240, "total": 10240}, next_precopy=nxt)
    w.update(next_precopy=nxt, cutover=None)
    return w


def test_the_follow_window_shows_copying_paused_as_waiting_not_as_a_switchover(context, flask_server):
    page, _, _ = open_waves(context, flask_server, [copying_paused()])
    wave_box(page, "w-paused").locator('[data-fk="wave-follow"]').click()
    row = page.locator('#fp-fk-wave-follow-harv-fake-w-paused [data-fk-vm="vm-16"]')
    expect(row).to_contain_text("Waiting for the next copy")
    expect(row).not_to_contain_text("final copy")
    expect(row.locator('[data-fk="vm-next"] [data-fk-at]')).to_contain_text("in ")


def test_the_follow_window_counts_down_to_a_scheduled_switchover(context, flask_server):
    page, _, _ = open_waves(context, flask_server, [scheduled(minutes=3)])
    wave_box(page, "w-sched").locator('[data-fk="wave-follow"]').click()
    at = page.locator('#fp-fk-wave-follow-harv-fake-w-sched [data-fk="follow-cutover"] [data-fk-at]')
    expect(at).to_contain_text(re.compile(r"in [23] min \d\d s"))
    first = at.inner_text()
    # le compte à rebours avance chaque seconde, sans relecture du cluster
    expect(at).not_to_have_text(first, timeout=5000)


def test_known_forklift_errors_are_shown_as_is_with_a_translated_hint(context, flask_server):
    page, sent, dialogs = open_waves(context, flask_server, [failed()])
    box = wave_box(page, "w-fail")
    expect(box.locator("[data-fk-error]")).to_contain_text("Unable to connect to vddk data source")
    wave_box(page, "w-fail").locator('[data-fk="wave-follow"]').click()
    win = page.locator("#fp-fk-wave-follow-harv-fake-w-fail")
    vddk = win.locator('[data-fk-vm="vm-16"]')
    expect(vddk.locator("[data-fk-error]")).to_have_text("Unable to connect to vddk data source")
    expect(vddk.locator("[data-fk-hint]")).to_contain_text("Preparation (step 2)")
    tools = win.locator('[data-fk-vm="vm-20"]')
    expect(tools.locator("[data-fk-error]")).to_have_text("Cannot complete operation because VMware Tools is not running")
    expect(tools.locator("[data-fk-hint]")).to_contain_text("open-vm-tools")
    # retour à la source d'une seule VM, confirmé
    tools.locator("[data-fk-vm-rollback]").click()
    page.wait_for_timeout(300)
    assert "vmwlab-src-4" in dialogs[-1] and "lost" in dialogs[-1]
    assert sent[-1] == ("api/forklift/harv-fake/do/wave-rollback", "POST", {"wave": "w-fail", "vms": ["vm-20"]})


def test_the_vddk_hint_points_to_the_vddk_image_step_with_another_importer(context, flask_server):
    # l'importeur SUSE (sans VDDK) se répare en changeant d'importeur (étape
    # 2) ; un autre importeur a plutôt une image VDDK à corriger (étape 3)
    data = {**DATA_T, "cdi_importer": {**DATA_T["cdi_importer"], "kind": "upstream"}, "waves": [failed()]}
    page, _ = open_tab(context, flask_server, data, section="waves")
    wave_box(page, "w-fail").locator('[data-fk="wave-follow"]').click()
    vddk = page.locator('#fp-fk-wave-follow-harv-fake-w-fail [data-fk-vm="vm-16"]')
    expect(vddk.locator("[data-fk-hint]")).to_contain_text("Preparation (step 3)")


def test_a_wave_that_failed_before_any_switchover_never_offers_a_rollback(context, flask_server):
    early = variant("w-fail-early", "failed", phase="Failed", error="Unable to connect to vddk data source",
                     cutover_started=False)
    early["cutover_started"] = False
    page, _, _ = open_waves(context, flask_server, [early])
    box = wave_box(page, "w-fail-early")
    expect(box.locator('[data-fk="wave-rollback"]')).to_have_count(0)
    box.locator('[data-fk="wave-follow"]').click()
    win = page.locator("#fp-fk-wave-follow-harv-fake-w-fail-early")
    expect(win.locator("[data-fk-vm-rollback]")).to_have_count(0)


def test_the_compose_window_disables_submit_without_a_usable_storage_class(context, flask_server):
    page, _, _ = open_waves(context, flask_server, [])
    page.route(re.compile(r".*/api/forklift/harv-fake\?targets=1$"), lambda r, q: fulfill(r, {**TARGETS, "classes": []}))
    page.evaluate("vms => Forklift.composeWave(vms)", [{**VMS_ROWS[0], "namespace": "forklift", "source": "vmwlab"}])
    win = page.locator("#fp-fk-wave-new-harv-fake")
    expect(win.locator('[data-fk="no-class"]')).to_be_visible()
    expect(win.locator('button[type="submit"]')).to_be_disabled()


def test_the_waves_tab_never_asks_for_the_targets_on_its_own_refresh(context, flask_server):
    # la liste des vagues ne s'en sert jamais : seule la fenêtre de
    # composition (une lecture à elle) a besoin des réseaux/classes/namespaces
    urls = []
    page, _, _ = open_waves(context, flask_server, [])
    page.on("request", lambda r: urls.append(r.url) if "/api/forklift/harv-fake" in r.url and "/do/" not in r.url else None)
    page.evaluate("Forklift.backgroundRefresh()")
    page.wait_for_timeout(400)
    assert any(u.endswith("/api/forklift/harv-fake") for u in urls)
    assert not any("targets=1" in u for u in urls)


def test_the_compose_window_still_asks_for_the_targets(context, flask_server):
    urls = []
    page, _, _ = open_waves(context, flask_server, [])
    page.on("request", lambda r: urls.append(r.url) if "/api/forklift/harv-fake" in r.url and "/do/" not in r.url else None)
    page.evaluate("vms => Forklift.composeWave(vms)", [{**VMS_ROWS[0], "namespace": "forklift", "source": "vmwlab"}])
    page.wait_for_timeout(300)
    assert any(u.endswith("/api/forklift/harv-fake?targets=1") for u in urls)


# --- Display fixes (v1.76.0) : étapes traduites, miroir CDI, inventaire ------

def test_step_names_are_translated_in_the_follow_window(context, flask_server):
    """La bibliothèque Forklift ne connaît que l'anglais (`step_name`) : la
    console traduit elle-même, plutôt que de montrer son texte brut dans une
    console en français."""
    page, _, _ = open_waves(context, flask_server, [copying(), SUCCEEDED], lang="fr")
    wave_box(page, "w-copy").locator('[data-fk="wave-follow"]').click()
    copying_row = page.locator('#fp-fk-wave-follow-harv-fake-w-copy [data-fk-vm="vm-16"]')
    expect(copying_row).to_contain_text("copie des disques")
    expect(copying_row).not_to_contain_text("copying disks")
    page.locator('#fp-fk-wave-follow-harv-fake-w-copy [data-action="close"]').click()
    wave_box(page, "vague-1").locator('[data-fk="wave-follow"]').click()
    done_row = page.locator('#fp-fk-wave-follow-harv-fake-vague-1 [data-fk-vm="vm-16"]')
    expect(done_row).to_contain_text("création de la VM")
    expect(done_row).not_to_contain_text("creating VM")


def test_an_unknown_step_name_falls_back_to_the_raw_text(context, flask_server):
    w = variant("w-unknown", "copying", phase="Running", step="doing something new", step_name="SomethingNew",
                progress={"done": 0, "total": 0})
    w.update(cutover=None, next_precopy=None)
    page, _, _ = open_waves(context, flask_server, [w], lang="fr")
    wave_box(page, "w-unknown").locator('[data-fk="wave-follow"]').click()
    row = page.locator('#fp-fk-wave-follow-harv-fake-w-unknown [data-fk-vm="vm-16"]')
    expect(row).to_contain_text("doing something new")


def test_a_vm_rolled_back_reads_back_on_the_source_not_its_last_step(context, flask_server):
    """Une vague revenue à la source : la VM qu'on y a fait revenir ne
    montre plus sa dernière étape Forklift (une bascule qui n'a plus cours)
    mais qu'elle est repartie sur son hôte d'origine."""
    w = variant("w-back", "rolled-back", rolled_back=True)
    page, _, _ = open_waves(context, flask_server, [w])
    wave_box(page, "w-back").locator('[data-fk="wave-follow"]').click()
    row = page.locator('#fp-fk-wave-follow-harv-fake-w-back [data-fk-vm="vm-16"]')
    expect(row).to_contain_text("back on the source")
    expect(row).not_to_contain_text("creating VM")


def test_the_cdi_mirror_placeholder_uses_the_current_importer_version(context, flask_server):
    """Le miroir proposé était toujours `v1.60.0`, quelle que soit la version
    de CDI en place : deviné dans le tag de l'importeur SUSE courant."""
    page, _ = open_tab(context, flask_server, DATA)   # cdi_importer image ...:v1.65.0, kind suse-no-vddk
    cdi = page.locator('#tab-forklift [data-fk-step="cdi"]')
    expect(cdi.locator('[name="cdi_image"]')).to_have_attribute("placeholder", "quay.io/kubevirt/cdi-importer:v1.65.0")


def test_the_cdi_mirror_placeholder_follows_the_upstream_tag_already_in_place(context, flask_server):
    upstream = {**DATA, "cdi_importer": {"image": "quay.io/kubevirt/cdi-importer:v1.60.0", "kind": "upstream",
                                          "original": "registry.suse.com/harvester/cdi-importer:v1.65.0"}}
    page, _ = open_tab(context, flask_server, upstream)
    cdi = page.locator('#tab-forklift [data-fk-step="cdi"]')
    expect(cdi.locator('[name="cdi_image"]')).to_have_attribute("placeholder", "quay.io/kubevirt/cdi-importer:v1.60.0")


def test_the_cdi_mirror_placeholder_is_neutral_for_an_unknown_importer(context, flask_server):
    other = {**DATA, "cdi_importer": {"image": "myregistry.lan/cdi-importer:latest", "kind": "other", "original": ""}}
    page, _ = open_tab(context, flask_server, other)
    cdi = page.locator('#tab-forklift [data-fk-step="cdi"]')
    expect(cdi.locator('[name="cdi_image"]')).to_have_attribute("placeholder", "quay.io/kubevirt/cdi-importer")


def test_the_inventory_table_fits_inside_the_card_at_1440(context, flask_server):
    """La colonne « raison du refus à chaud » débordait à droite, coupée par
    `.card { overflow: hidden }` : le texte long doit se replier plutôt que
    forcer la table plus large que sa carte."""
    page, _ = open_tab(context, flask_server, DATA, section="inventory")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.evaluate("Forklift.openInventory('vmwlab')")
    table = page.locator('#tab-forklift [data-fk="inv-table"]')
    expect(table).to_be_visible()
    card = page.locator('#tab-forklift .fk-card')
    t_box = table.bounding_box()
    c_box = card.bounding_box()
    assert t_box["x"] + t_box["width"] <= c_box["x"] + c_box["width"] + 1
