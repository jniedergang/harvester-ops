"""Prochaine version KVM : fenêtre « Modifier » et gestes à chaud d'une VM
KVM, API simulée.

Repères par champ (maintenant / prochain démarrage / refusé) d'après la table
HOTNESS servie et l'état de la VM, réglages non gérés en lecture seule,
modification mixte (spec partiel, empreinte rendue), revue classée par le
serveur, résultat par changement, bandeau et badge « redémarrage nécessaire »,
409 `changed` sans perte de saisie puis relecture de l'empreinte, VM
transitoire en lecture seule, gestes du menu, noms hostiles, aucun secret dans
un attribut du DOM."""

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import kvm_form_fakes as F  # noqa: E402


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "if (!sessionStorage.getItem('kvme-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_tab','overview');"
        "sessionStorage.setItem('kvme-init','1'); }")
    page = context.new_page()
    state = F.install(page, {})
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.KVM && window.App && window.KVMForm")
    page.mouse.move(1200, 600)
    return page, state, dialogs


def open_menu(page, name):
    page.locator('#tab-group-kvm .tab[data-tab="kvmvms"]').evaluate("el => el.click()")
    row = page.locator("#tab-kvmvms tr[data-kvm-vm]").filter(has=page.locator(".kvm-vm-name", has_text=name)).first
    expect(row).to_be_visible()
    row.locator('[data-kvm="menu"]').click()
    menu = page.locator(".kvm-menu")
    expect(menu).to_be_visible()
    return menu


def open_edit(page, name="kvmtest-1"):
    menu = open_menu(page, name)
    menu.locator('[data-kvm-hot="edit"]').click()
    panel = page.locator(".floating-panel").filter(has=page.locator('.kvmf[data-kvmf-mode="edit"]')).last
    expect(panel.locator('.vm-create-section[data-vmf-section="general"]')).to_be_visible()
    return panel


def tab(panel, sid):
    panel.locator(f'.vm-edit-nav [data-section="{sid}"]').click()
    sec = panel.locator(f'.vm-create-section[data-vmf-section="{sid}"]')
    expect(sec).to_be_visible()
    return sec


def badge(sec, field):
    return sec.locator(f'[data-hot="{field}"] .vmf-applied')


def test_running_vm_fields_carry_now_or_next_boot_and_unmanaged_are_listed(ui):
    page, state, dialogs = ui
    panel = open_edit(page)
    comp = tab(panel, "compute")
    assert comp.locator('[name="cpu.vcpus"]').input_value() == "2"
    assert comp.locator('[name="memory.max_mib"]').input_value() == "4096"
    expect(badge(comp, "cpu.vcpus")).to_have_attribute("data-applied", "now")
    expect(badge(comp, "cpu.vcpus")).to_have_text("now")
    expect(badge(comp, "cpu.max_vcpus")).to_have_attribute("data-applied", "next-boot")
    expect(badge(comp, "memory.current_mib")).to_have_attribute("data-applied", "now")
    expect(comp.locator('[data-unmanaged="compute"]')).to_contain_text("cputune")
    gen = tab(panel, "general")
    expect(badge(gen, "title")).to_have_attribute("data-applied", "now")
    expect(badge(gen, "name")).to_have_attribute("data-applied", "refused")      # renommage : VM arrêtée seulement
    expect(gen.locator('[data-unmanaged="general"]')).to_contain_text("metadata:urn:other")
    fw = tab(panel, "firmware")
    # réglage posé à la création seulement : grisé avec la raison
    expect(fw.locator('[name="watchdog"]')).to_be_disabled()
    expect(fw.locator('[data-why="watchdog"]')).to_contain_text("Set at creation only")
    disks = tab(panel, "disks")
    expect(disks.locator('[data-unmanaged="disks"]')).to_contain_text("devices/hostdev")
    # le seed n'est pas un média changeable ; sdb (vide) l'est, à chaud
    assert disks.locator('tr[data-disk="sdc"] [data-disk-field="source"]').count() == 0
    expect(disks.locator('tr[data-disk="sdb"] [data-hot="disks[].source"] .vmf-applied')).to_have_attribute("data-applied", "now")
    expect(disks.locator('tr[data-disk="vda"] [data-hot="disks[-]"] .vmf-applied')).to_have_attribute("data-applied", "now")
    expect(disks.locator('tr[data-disk="sdb"] [data-hot="disks[-]"] .vmf-applied')).to_have_attribute("data-applied", "next-boot")
    assert dialogs == []


def test_stopped_vm_applies_everything_now(ui):
    page, state, _ = ui
    state["form"] = F.vm_form(state="shut off")
    panel = open_edit(page)
    comp = tab(panel, "compute")
    expect(badge(comp, "cpu.max_vcpus")).to_have_attribute("data-applied", "now")
    gen = tab(panel, "general")
    expect(badge(gen, "name")).to_have_attribute("data-applied", "now")


def test_mixed_hot_and_cold_edit_sends_a_partial_spec_and_shows_the_restart_badge(ui):
    page, state, dialogs = ui
    panel = open_edit(page)
    comp = tab(panel, "compute")
    comp.locator('[name="cpu.vcpus"]').fill("3")
    comp.locator('[name="cpu.max_vcpus"]').fill("8")
    # revue : classification du serveur
    rev = tab(panel, "review")
    expect(rev.locator('.vmf-changes li[data-field="cpu.vcpus"] .vmf-applied')).to_have_attribute("data-applied", "now")
    expect(rev.locator('.vmf-changes li[data-field="cpu.max_vcpus"] .vmf-applied')).to_have_text("at next boot")
    plan = [s for s in state["sent"] if s[1].endswith("/edit-plan")][-1][2]
    assert plan == {"spec": {"cpu": {"vcpus": 3, "max_vcpus": 8}}, "expect_config": F.DIGEST0}
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Changes applied")
    patch = [s for s in state["sent"] if s[0] == "PATCH"][-1]
    assert patch[1] == "vms/node2/kvmtest-1"
    assert patch[2] == {"spec": {"cpu": {"vcpus": 3, "max_vcpus": 8}}, "expect_config": F.DIGEST0}
    res = panel.locator('[data-kvmf="review"]')
    expect(res.locator('li[data-field="cpu.vcpus"] .vmf-applied')).to_have_text("applied now")
    expect(res.locator('li[data-field="cpu.max_vcpus"] .vmf-applied')).to_have_text("at next boot")
    # relecture : nouvelle empreinte, bandeau « redémarrage nécessaire » avec le geste
    banner = panel.locator(".vmf-restart")
    expect(banner).to_contain_text("restart needed")
    expect(banner).to_contain_text("cpu.max_vcpus")
    expect(banner.locator('[data-kvmf="cycle"]')).to_be_visible()
    # un second « Appliquer » ne rejoue rien (la base est la VM relue)
    n = len(state["sent"])
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Nothing to change.")
    assert len([s for s in state["sent"][n:] if s[0] == "PATCH"]) == 0
    # la liste des VMs porte le badge
    row = page.locator('#tab-kvmvms tr[data-kvm-vm="node2/kvmtest-1"]')
    expect(row).to_contain_text("restart needed")
    assert dialogs == []


def test_restart_banner_shuts_down_then_starts(ui):
    page, state, _ = ui
    state["form"] = F.vm_form(pending=["machine"])
    panel = open_edit(page)
    panel.locator('[data-kvmf="cycle"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("restarted with its new configuration")
    posts = [s[1] for s in state["sent"] if s[0] == "POST"]
    assert posts[-2:] == ["vms/node2/kvmtest-1/shutdown", "vms/node2/kvmtest-1/start"]


def test_changed_elsewhere_keeps_the_input_and_reload_refreshes_the_digest(ui):
    page, state, _ = ui
    panel = open_edit(page)
    gen = tab(panel, "general")
    gen.locator('[name="title"]').fill("New title")
    # la VM change ailleurs après l'ouverture
    state["form"] = dict(state["form"], config_digest=F.DIGEST1)
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator(".kvmf-changed")).to_contain_text("The VM was changed since you opened it")
    assert gen.locator('[name="title"]').input_value() == "New title"
    panel.locator('[data-kvmf="reload-now"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("your changes are kept")
    assert gen.locator('[name="title"]').input_value() == "New title"
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Changes applied")
    patch = [s for s in state["sent"] if s[0] == "PATCH"][-1][2]
    assert patch == {"spec": {"title": "New title"}, "expect_config": F.DIGEST1}


def test_transient_vm_opens_read_only_with_the_reason(ui):
    page, state, _ = ui
    state["form"] = F.vm_form(persistent=False, name="temp")
    panel = open_edit(page, "temp")
    expect(panel.locator(".vmf-readonly")).to_contain_text("Transient VM")
    expect(panel.locator('[data-kvmf="apply"]')).to_be_disabled()
    comp = tab(panel, "compute")
    expect(comp.locator('[name="cpu.vcpus"]')).to_be_disabled()


def test_cloud_init_secret_stays_in_the_textarea_value_only(ui):
    page, state, _ = ui
    panel = open_edit(page)
    ci = tab(panel, "cloudinit")
    ta = ci.locator('[data-ci="userData"]')
    expect(ta).to_have_value(re.compile(F.USER_SECRET))
    expect(ci.locator("[data-ci-state]")).to_contain_text("images/kvmtest-1-cidata.iso")
    attrs = page.evaluate("""() => [...document.querySelectorAll('*')].flatMap(e => [...e.attributes].map(a => a.value)).join('\\n')""")
    assert F.USER_SECRET not in attrs
    # changé puis enregistré : part dans le spec comme cloudinit, avec relance
    ta.fill("#cloud-config\nhostname: x\n")
    ci.locator('[name="ci_rerun"]').check()
    expect(ci.locator('[data-hot="cloudinit"] .vmf-applied')).to_have_attribute("data-applied", "next-boot")
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Changes applied")
    spec = [s for s in state["sent"] if s[0] == "PATCH"][-1][2]["spec"]
    assert spec == {"cloudinit": {"user_data": "#cloud-config\nhostname: x\n", "network_data": None, "rerun": True}}


def test_menu_offers_the_hot_gestures_per_disk_and_card(ui):
    page, state, dialogs = ui
    menu = open_menu(page, "kvmtest-1")
    grp = menu.locator("[data-kvm-hot-group]")
    expect(grp.locator('[data-kvm-hot="remove-volume"][data-target="vda"]')).to_be_visible()
    expect(grp.locator('[data-kvm-hot="insert-cdrom"][data-target="sdb"]')).to_be_visible()
    expect(grp.locator('[data-kvm-hot="eject-image"][data-target="sdc"]')).to_be_visible()
    assert grp.locator('[data-kvm-hot="eject-image"][data-target="sdb"]').count() == 0      # vide
    expect(grp.locator('[data-kvm-hot="remove-nic"][data-mac="52:54:00:81:e2:fb"]')).to_be_visible()
    for b in grp.locator("button").all():
        assert b.get_attribute("data-tip"), b.inner_html()
    # CPU et mémoire : la fenêtre du geste, le résultat par changement
    grp.locator('[data-kvm-hot="cpumem"]').click()
    dlg = page.locator('.floating-panel form[data-kvm-hot-op="cpumem"]')
    expect(dlg).to_be_visible()
    expect(dlg.locator('[data-h="hint"]')).to_contain_text("4 vCPUs and 4096 MiB")
    dlg.locator('[name="vcpus"]').fill("3")
    dlg.locator('[name="memory_mib"]').fill("5000")
    dlg.locator('[data-h="go"]').click()
    expect(dlg.locator('[data-h="result"] li[data-field="cpu.vcpus"] .vmf-applied')).to_have_text("applied now")
    expect(dlg.locator('[data-h="result"] li[data-field="memory.current_mib"]')).to_contain_text("above the current maximum")
    expect(dlg.locator(".vmf-restart")).to_contain_text("memory.max_mib")
    sent = [s for s in state["sent"] if "/hot/" in s[1]][-1]
    assert sent[1] == "vms/node2/kvmtest-1/hot/cpumem" and sent[2] == {"vcpus": 3, "memory_mib": 5000}
    assert dialogs == []


@pytest.mark.parametrize("op,attr,value,body", [
    ("remove-volume", "data-target", "vda", {"target": "vda"}),
    ("eject-image", "data-target", "sdc", {"target": "sdc"}),
    ("remove-nic", "data-mac", "52:54:00:81:e2:fb", {"mac": "52:54:00:81:e2:fb"}),
    ("insert-cdrom", "data-target", "sdb", {"target": "sdb", "pool": "images", "volume": "netinst.iso"}),
])
def test_device_gestures_send_their_target(ui, op, attr, value, body):
    page, state, dialogs = ui
    menu = open_menu(page, "kvmtest-1")
    menu.locator(f'[data-kvm-hot="{op}"][{attr}="{value}"]').click()
    dlg = page.locator(f'.floating-panel form[data-kvm-hot-op="{op}"]')
    expect(dlg).to_be_visible()
    if op == "insert-cdrom":
        expect(dlg.locator('[name="iso"] option')).to_have_count(1)
    else:
        expect(dlg.locator('[data-h="hint"]')).to_contain_text(value)
    dlg.locator('[data-h="go"]').click()
    expect(dlg.locator('[data-h="msg"]')).to_contain_text("Changes applied")
    sent = [s for s in state["sent"] if "/hot/" in s[1]][-1]
    assert sent[1] == f"vms/node2/kvmtest-1/hot/{op}" and sent[2] == body
    assert dialogs == []


def test_add_disk_and_card_gestures_build_spec_items(ui):
    page, state, _ = ui
    menu = open_menu(page, "kvmtest-1")
    menu.locator('[data-kvm-hot="add-volume"]').click()
    dlg = page.locator('.floating-panel form[data-kvm-hot-op="add-volume"]')
    expect(dlg.locator('[name="pool"] option').first).to_be_attached()
    dlg.locator('[name="size"]').fill("512M")
    dlg.locator('[data-h="go"]').click()
    expect(dlg.locator('[data-h="msg"]')).to_contain_text("Changes applied")
    assert [s for s in state["sent"] if "/hot/" in s[1]][-1][2] == {"disk": {
        "device": "disk", "bus": "virtio", "source": {"kind": "blank", "pool": "images", "size_bytes": 512 * 2 ** 20, "format": "qcow2"}}}
    menu = open_menu(page, "kvmtest-1")
    menu.locator('[data-kvm-hot="add-nic"]').click()
    dlg = page.locator('.floating-panel form[data-kvm-hot-op="add-nic"]')
    expect(dlg.locator('[name="network"] option').first).to_be_attached()
    dlg.locator('[name="mac"]').fill("52:54:00:AA:BB:01")
    dlg.locator('[data-h="go"]').click()
    expect(dlg.locator('[data-h="msg"]')).to_contain_text("Changes applied")
    assert [s for s in state["sent"] if "/hot/" in s[1]][-1][2] == {"nic": {
        "kind": "network", "source": "default", "model": "virtio", "mac": "52:54:00:aa:bb:01"}}


def test_hostile_vm_name_stays_text_in_the_menu_window_and_dock(ui):
    page, state, dialogs = ui
    state["form"] = F.vm_form(state="shut off", name=F.EVIL)
    panel = open_edit(page, F.EVIL)
    expect(panel.locator(".kvmf-edit-head strong")).to_have_text(F.EVIL)
    assert page.locator(".floating-panel .kvmf img").count() == 0
    assert page.evaluate("window.__xss") is None
    gen = tab(panel, "general")
    gen.locator('[name="title"]').fill("x")
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Changes applied")
    patch = [s for s in state["sent"] if s[0] == "PATCH"][-1]
    assert patch[1] == f"vms/node2/{F.EVIL}"
    assert page.evaluate("window.__xss") is None
    assert dialogs == []


def test_viewer_sees_the_gestures_greyed_with_the_reason(ui):
    page, state, _ = ui
    state["role"] = "viewer"
    menu = open_menu(page, "kvmtest-1")
    item = menu.locator('[data-kvm-hot="cpumem"]')
    expect(item).to_have_attribute("aria-disabled", "true")
    assert item.get_attribute("data-tip") == "Needs the operator role"
    item.click(force=True)
    assert page.locator('.floating-panel form[data-kvm-hot-op="cpumem"]').count() == 0


def test_cloudinit_assistant_names_the_existing_cards_by_mac(ui):
    """Revue finale I2 : dans la fenêtre Modifier, l'assistant désigne les
    cartes existantes par leur MAC (aucune MAC tirée : elles en ont une)."""
    page, state, dialogs = ui
    panel = open_edit(page)
    ci = tab(panel, "cloudinit")
    ci.locator(".vm-edit-ci-wizard summary").click()
    net = ci.locator(".vm-edit-ci-netform")
    net.locator('.tf-block-add[data-block="nic"]').click()
    item = net.locator(".tf-block-item").first
    item.locator('[name$=".card"]').fill("2")
    item.locator('[name$=".mode"]').select_option("static")
    item.locator('[name$=".address"]').fill("10.213.77.20/24")
    ci.locator('[data-ci="networkData"]').fill("")
    ci.locator('[data-action="gen-netdata"]').click()
    nd = ci.locator('[data-ci="networkData"]')
    expect(nd).to_have_value(re.compile("macaddress: '52:54:00:aa:bb:cc'"))
    assert "nic2:" in nd.input_value() and "10.213.77.20/24" in nd.input_value()
    assert not [g for g in state["gets"] if g.endswith("/macs")]
    assert dialogs == []


# --- 1.101.2 : le catalogue d'images ne bloque plus la fenêtre -------------------------

def test_the_window_opens_while_the_image_catalogue_is_still_loading(ui):
    page, state, dialogs = ui
    state["hold"] = "hosts/node2/images"            # catalogue lent (relevé à froid)
    panel = open_edit(page)                         # le formulaire est là
    assert len(state.get("held", [])) == 1          # alors que le catalogue attend
    comp = tab(panel, "compute")
    assert comp.locator('[name="cpu.vcpus"]').input_value() == "2"
    disks = tab(panel, "disks")
    status = disks.locator('[data-kvmf="images-state"]')
    expect(status).to_be_visible()
    expect(status).to_have_text("Loading the host's images...")
    disks.locator('.tf-block-add[data-block="disk"]').click()
    sel = disks.locator('[data-editor="kvm-disks"] [name$=".image"]').last
    assert sel.locator("option").evaluate_all("els => els.map(e => e.value)") == [""]
    F.release(state)
    expect(status).to_be_hidden()
    expect(sel.locator('option[value="images/cirros-0.6.3.qcow2"]')).to_have_count(1)
    # une carte ajoutée après l'arrivée voit aussi le catalogue
    disks.locator('.tf-block-add[data-block="disk"]').click()
    later = disks.locator('[data-editor="kvm-disks"] [name$=".image"]').last
    expect(later.locator('option[value="images/cirros-0.6.3.qcow2"]')).to_have_count(1)
    assert dialogs == []


def test_an_unreadable_catalogue_is_said_in_the_disks_tab(ui):
    page, state, dialogs = ui
    state["images_body"] = {"error": "node2: timeout: no answer within 60s", "kind": "timeout"}
    panel = open_edit(page)
    disks = tab(panel, "disks")
    status = disks.locator('[data-kvmf="images-state"]')
    expect(status).to_be_visible()
    expect(status).to_contain_text("Images could not be read")
    expect(status).to_contain_text("no answer within 60s")
    # le reste de la fenêtre reste utilisable
    expect(tab(panel, "compute").locator('[name="cpu.vcpus"]')).to_have_value("2")
