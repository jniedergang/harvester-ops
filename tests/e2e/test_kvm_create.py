"""Prochaine version KVM : fenêtre « Créer une VM » d'un hôte KVM, API simulée.

Onglets à la Harvester (Général, Calcul, Micrologiciel, Disques, Réseau,
Cloud-init, Revue), choix venus des capacités de l'hôte (option grisée avec la
raison servie), erreurs de saisie par onglet, revue (aperçu du serveur, XML
au mot de passe masqué), création suivie, trois instances, modèle, clés SSH
et agent invité ajoutés au user-data, noms hostiles en texte, aucun secret
dans un attribut du DOM."""

import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import kvm_form_fakes as F  # noqa: E402


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "if (!sessionStorage.getItem('kvmf-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "sessionStorage.setItem('kvmf-init','1'); }")
    page = context.new_page()
    state = F.install(page, {})
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    goto(page, flask_server["base_url"], "#activity")   # 2.0.0 : écran ouvert par son adresse
    page.wait_for_function("window.KVM && window.App && window.KVMForm && window.VMForm && window.CloudInitForm")
    page.mouse.move(1200, 600)
    return page, state, dialogs


def open_create(page, host="node2", from_view=True):
    if from_view:
        page.evaluate("Router.go('#vms/host/kvm')")   # 2.0.0 : KVM > Virtual machines, par son adresse
        btn = page.locator('#tab-kvmvms .res-tools [data-kvm="create-vm"]')
        expect(btn).to_be_enabled()
        btn.click()
    else:
        page.evaluate(f"KVMForm.openCreate({json.dumps(host)})")
    panel = page.locator("#fp-kvm-create")
    expect(panel).to_be_visible()
    expect(panel.locator('.vm-create-section[data-vmf-section="general"]')).to_be_visible()
    return panel


def tab(panel, sid):
    panel.locator(f'.vm-edit-nav [data-section="{sid}"]').click()
    sec = panel.locator(f'.vm-create-section[data-vmf-section="{sid}"]')
    expect(sec).to_be_visible()
    return sec


def test_tabs_match_harvester_and_every_control_has_a_tooltip(ui):
    page, state, dialogs = ui
    panel = open_create(page)
    ids = panel.locator(".vm-edit-nav button").evaluate_all("els => els.map(e => e.dataset.section)")
    assert ids == ["general", "compute", "firmware", "disks", "network", "cloudinit", "review"]
    # choix venus des capacités : machines, modes CPU, vidéo, bus
    fw = tab(panel, "firmware")
    machines = fw.locator('[name="machine"] option').evaluate_all("els => els.map(e => e.value)")
    assert machines == ["q35", "pc"]
    assert fw.locator('[name="display.video"] option').evaluate_all("els => els.map(e => e.value)") == ["virtio", "vga", "bochs"]
    disks = tab(panel, "disks")
    buses = disks.locator('[name$=".bus"]').first.locator("option").evaluate_all("els => els.map(e => e.value)")
    assert [b for b in buses if b] == ["virtio", "scsi", "sata"]
    # le premier disque part d'une image (catalogue) dans le pool actif par défaut
    assert disks.locator('[name$=".source"]').first.input_value() == "image"
    # bulle sur chaque contrôle visible ou non de la fenêtre
    missing = panel.evaluate("""el => [...el.querySelectorAll('button, input, select, textarea')]
        .filter(c => c.type !== 'hidden')
        .filter(c => !(c.dataset.tip || (c.closest('.tip') && c.closest('.tip').dataset.tip) || c.closest('.tf-field')))
        .map(c => c.outerHTML.slice(0, 120))""")
    assert missing == []
    assert dialogs == []


def test_greyed_option_carries_the_reason_served_by_the_host(ui):
    page, state, _ = ui
    state["caps"] = F.caps_no_tpm()
    panel = open_create(page)
    fw = tab(panel, "firmware")
    tpm = fw.locator('[name="tpm"]')
    expect(tpm).to_be_disabled()
    why = state["caps"]["disabled"]["q35"]["tpm"]
    assert why == "the host has no TPM emulator (swtpm)"
    assert fw.locator('[data-vmf-field="tpm"]').get_attribute("data-tip") == why
    expect(fw.locator('[data-why="tpm"]')).to_have_text(why)
    # passer en pc : Secure Boot grisé avec la raison de l'hôte
    fw.locator('[name="machine"]').select_option("pc")
    fw = panel.locator('.vm-create-section[data-vmf-section="firmware"]')
    expect(fw.locator('[name="secure_boot"]')).to_be_disabled()
    expect(fw.locator('[data-why="secure_boot"]')).to_have_text("Secure Boot needs the q35 machine type")
    # les bus suivent : ide apparaît sur pc
    disks = tab(panel, "disks")
    buses = disks.locator('[name$=".bus"]').first.locator("option").evaluate_all("els => els.map(e => e.value)")
    assert "ide" in buses


def test_validation_errors_are_shown_per_tab_and_nothing_is_sent(ui):
    page, state, _ = ui
    panel = open_create(page)
    panel.locator('.kvmf-head [name="name"]').fill("")
    comp = tab(panel, "compute")
    comp.locator('[name="cpu.vcpus"]').fill("4")
    comp.locator('[name="cpu.max_vcpus"]').fill("2")
    disks = tab(panel, "disks")
    disks.locator('[name$=".size"]').first.fill("ten gigs")
    panel.locator('[data-kvmf="create"]').click()
    errs = panel.locator(".vmf-errors li")
    expect(errs.first).to_be_visible()
    text = panel.locator(".vmf-errors").inner_text()
    assert "A name is required." in text and "vCPUs above the maximum." in text
    assert "Disk 1: unreadable size" in text
    counts = panel.locator(".vm-edit-nav .vmf-err-count").evaluate_all(
        "els => els.map(e => [e.closest('button').dataset.section, e.textContent.trim()])")
    assert ["general", "1"] in counts and ["disks", "1"] in counts
    assert any(c[0] == "compute" for c in counts)
    assert panel.locator('.kvmf-head [name="name"]').get_attribute("aria-invalid") == "true"
    assert [s for s in state["sent"] if s[0] != "GET"] == []
    expect(panel.locator("[data-result]")).to_contain_text("Fix the errors")


def test_review_then_create_sends_the_spec_and_follows_the_action(ui):
    page, state, dialogs = ui
    panel = open_create(page)
    panel.locator('.kvmf-head [name="name"]').fill("kvmtest-c1")
    panel.locator('.kvmf-head [name="ssh_keys"]').select_option(index=0)
    panel.locator('.kvmf-head [name="ssh_extra"]').fill("ssh-rsa AAAAB3NzaC1yc2E extra@x")
    gen = tab(panel, "general")
    gen.locator('[name="title"]').fill("Test VM")
    ci = tab(panel, "cloudinit")
    ci.locator('[data-ci="userData"]').fill(f"#cloud-config\npassword: {F.USER_SECRET}\n")
    ci.locator('[name="ci_hostname"]').fill("c1")
    # revue : l'aperçu du serveur, XML au mot de passe masqué
    with page.expect_request(lambda r: r.url.endswith("/api/kvm/hosts/node2/vm-plan")):
        panel.locator('[data-kvmf="review-go"]').click()
    review = panel.locator('[data-kvmf="review"]')
    expect(review.locator('[data-instance="kvmtest-c1"]')).to_contain_text("images/kvmtest-c1-vda.qcow2")
    expect(review).to_contain_text("the network n<i>et is inactive")
    review.locator(".vmf-xml summary").click()
    expect(review.locator(".vmf-xml pre")).to_contain_text("passwd='***'")
    plan_spec = [s for s in state["sent"] if s[1] == "hosts/node2/vm-plan"][-1][2]["spec"]
    assert plan_spec["name"] == "kvmtest-c1" and plan_spec["title"] == "Test VM"
    # créer
    panel.locator('[data-kvmf="create"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Created: kvmtest-c1")
    sent = [s for s in state["sent"] if s[1] == "vms/node2"][-1]
    spec = sent[2]["spec"]
    assert sent[0] == "POST" and spec["name"] == "kvmtest-c1" and spec["start"] is True
    assert spec["machine"] == "q35" and spec["display"]["password"] is True
    assert spec["disks"][0]["source"] == {"kind": "image", "pool": "images", "image": "cirros-0.6.3.qcow2",
                                         "image_pool": "images", "linked": True}
    assert spec["nics"][0] == {"kind": "network", "source": "default", "model": "virtio"}
    ud = spec["cloudinit"]["user_data"]
    assert ud.startswith("#cloud-config\n") and F.USER_SECRET in ud
    assert "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOk ju@node1" in ud and "ssh-rsa AAAAB3NzaC1yc2E extra@x" in ud
    assert "qemu-guest-agent" in ud
    assert spec["cloudinit"]["seed_pool"] == "images" and spec["cloudinit"]["hostname"] == "c1"
    expect(panel.locator('[data-kvmf="steps"] li')).to_have_count(5)
    # aucun secret dans un attribut du DOM (le user-data vit dans la valeur de la zone de texte)
    attrs = page.evaluate("""() => [...document.querySelectorAll('*')].flatMap(e => [...e.attributes].map(a => a.value)).join('\\n')""")
    assert F.USER_SECRET not in attrs
    assert F.USER_SECRET not in page.locator("body").inner_text()
    assert dialogs == []


def test_three_instances_and_a_partial_failure_names_what_was_created_and_left(ui):
    page, state, _ = ui
    state["create_fails"] = True
    panel = open_create(page)
    panel.locator('.kvmf-head [name="name"]').fill("kvmtest-m")
    panel.locator('.kvmf-head [name="count"]').fill("3")
    panel.locator('[data-kvmf="create"]').click()
    res = panel.locator("[data-result]")
    expect(res).to_contain_text("define refused")
    expect(res).to_contain_text("created anyway: kvmtest-m-01")
    expect(res).to_contain_text("left on the host: images/kvmtest-m-03-vda.qcow2")
    sent = [s for s in state["sent"] if s[1] == "vms/node2"][-1]
    assert sent[2]["spec"]["instances"] == 3


def test_template_loads_and_saves_without_secrets_and_without_prompt(ui):
    page, state, dialogs = ui
    panel = open_create(page)
    panel.locator('.kvmf-head [name="template"]').select_option("web-small")
    expect(panel.locator("[data-result]")).to_contain_text("Template loaded")
    expect(panel.locator("[data-result]")).to_contain_text("fields not kept: cloudinit.user_data")
    comp = panel.locator('.vm-create-section[data-vmf-section="compute"]')
    assert comp.locator('[name="cpu.vcpus"]').input_value() == "2"
    assert comp.locator('[name="memory.current_mib"]').input_value() == "512"
    disks = panel.locator('.vm-create-section[data-vmf-section="disks"]')
    assert disks.locator('[name$=".source"]').first.input_value() == "blank"
    assert disks.locator('[name$=".size"]').first.input_value() == "2G"
    panel.locator('.kvmf-head [name="name"]').fill("tpl-src")
    panel.locator('[data-kvmf="save-template"]').click()
    inp = panel.locator('[name="tpl_name"]')
    expect(inp).to_have_value("tpl-src-template")
    panel.locator('[data-kvmf="tpl-yes"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Template saved")
    sent = [s for s in state["sent"] if s[1] == "templates"][-1]
    assert sent[2]["name"] == "tpl-src-template" and sent[2]["spec"]["memory"]["current_mib"] == 512
    assert dialogs == []


def test_hostile_names_stay_text_and_the_host_card_preselects_the_host(ui):
    page, state, dialogs = ui
    panel = open_create(page, host="node9", from_view=False)
    assert panel.locator('.kvmf-head [name="host"]').input_value() == "node9"
    disks = tab(panel, "disks")
    pools = disks.locator('[name$=".pool"]').first.locator("option").evaluate_all("els => els.map(e => e.value)")
    assert F.EVIL_POOL in pools
    imgs = disks.locator('[name$=".image"]').first.locator("option").evaluate_all("els => els.map(e => e.value)")
    assert f"images/{F.EVIL_IMG}" in imgs
    net = tab(panel, "network")
    assert F.EVIL_NET in net.locator('[name$=".network"]').first.locator("option").evaluate_all("els => els.map(e => e.value)")
    # une image hostile choisie s'affiche en texte dans le titre de la carte
    disks = tab(panel, "disks")
    disks.locator('[name$=".image"]').first.select_option(f"images/{F.EVIL_IMG}")
    disks.locator('[name$=".pool"]').first.select_option(F.EVIL_POOL)
    assert page.evaluate("window.__xss") is None
    assert page.locator("#fp-kvm-create img, #fp-kvm-create svg[onload]").count() == 0
    assert dialogs == []


def test_create_from_a_catalogue_image_prefills_the_first_disk(ui):
    page, state, _ = ui
    page.evaluate("KVMForm.openCreate('node2', {image: {pool: 'images', volume: 'netinst.iso', iso: true}})")
    panel = page.locator("#fp-kvm-create")
    # la fenêtre se redessine une fois l'hôte lu (suite chargée : un clic
    # avant serait perdu) ; l'image proposée posée = formulaire final
    expect(panel.locator('[name$=".iso"]').first).to_have_value("images/netinst.iso", timeout=15000)
    disks = tab(panel, "disks")
    src = disks.locator('[name$=".source"]').evaluate_all("els => els.map(e => e.value)")
    assert src == ["iso", "blank"]
    assert disks.locator('[name$=".iso"]').first.input_value() == "images/netinst.iso"
    assert disks.locator('[name$=".boot_order"]').evaluate_all("els => els.map(e => e.value)") == ["1", "2"]


def test_viewer_cannot_create(ui):
    page, state, _ = ui
    state["role"] = "viewer"
    page.evaluate("Router.go('#vms/host/kvm')")   # 2.0.0 : KVM > Virtual machines, par son adresse
    btn = page.locator('#tab-kvmvms .res-tools [data-kvm="create-vm"]')
    expect(btn).to_be_disabled()
    assert btn.get_attribute("data-tip") == "Needs the operator role"


# --- network-config par MAC (revue finale I2) -------------------------------------
# La console tire la MAC des cartes sans MAC, l'écrit dans la carte (envoyée
# avec le spec) et le network-config v2 de l'assistant désigne la carte par
# cette adresse : le nom que l'image donne à la carte ne compte plus.

def gen_net(panel, entries):
    ci = tab(panel, "cloudinit")
    if not ci.locator(".vm-edit-ci-wizard").evaluate("d => d.open"):
        ci.locator(".vm-edit-ci-wizard summary").click()
    net = ci.locator(".vm-edit-ci-netform")
    for i, e in enumerate(entries):
        net.locator('.tf-block-add[data-block="nic"]').click()
        item = net.locator(".tf-block-item").nth(i)
        for k, v in e.items():
            el = item.locator(f'[name$=".{k}"]')
            if el.evaluate("e => e.tagName") == "SELECT":
                el.select_option(v)
            else:
                el.fill(str(v))
    ci.locator('[data-action="gen-netdata"]').click()
    return ci


def test_network_config_names_cards_by_a_console_drawn_mac(ui):
    page, state, dialogs = ui
    panel = open_create(page)
    panel.locator('.kvmf-head [name="name"]').fill("kvmtest-net-c")
    net = tab(panel, "network")
    net.locator('.tf-block-add[data-block="nic"]').click()
    net.locator('.tf-block-item').nth(1).locator('[name$=".mac"]').fill("52:54:00:12:34:56")
    ci = gen_net(panel, [{"card": 1, "mode": "static", "address": "10.213.77.10/24",
                          "gateway": "10.213.77.1"},
                         {"card": 2, "iface": "lan1"}])
    nd = ci.locator('[data-ci="networkData"]')
    expect(nd).to_have_value(re.compile("macaddress: '52:54:00:5e:00:01'"))
    text = nd.input_value()
    assert text.startswith("version: 2\nethernets:\n")
    assert "macaddress: '52:54:00:12:34:56'\n    set-name: lan1" in text
    assert "addresses:\n      - 10.213.77.10/24" in text and "via: 10.213.77.1" in text
    assert "eth0" not in text
    # une seule MAC demandée à la console (la seconde carte avait la sienne)
    assert [g for g in state["gets"] if g.endswith("/macs")] == ["hosts/node2/macs"]
    assert state["macs_drawn"] == ["52:54:00:5e:00:01"]
    net = tab(panel, "network")
    assert net.locator('[name$=".mac"]').evaluate_all("els => els.map(e => e.value)") == [
        "52:54:00:5e:00:01", "52:54:00:12:34:56"]
    panel.locator('[data-kvmf="create"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Created: kvmtest-net-c")
    spec = [s for s in state["sent"] if s[1] == "vms/node2"][-1][2]["spec"]
    assert [n.get("mac") for n in spec["nics"]] == ["52:54:00:5e:00:01", "52:54:00:12:34:56"]
    assert spec["cloudinit"]["network_data"] == text
    # régénérer ne retire pas une nouvelle MAC : les cartes ont déjà la leur
    gen_net(panel, [])
    assert state["macs_drawn"] == ["52:54:00:5e:00:01"]
    assert dialogs == []


@pytest.mark.parametrize("label,setup,entries,error", [
    ("card that does not exist", None, [{"card": 3}], "card 3 does not exist: the VM has 1 card(s)"),
    ("card configured twice", None, [{"card": 1}, {"card": 1}], "card 1 is configured twice"),
    ("several instances", "count", [{"card": 1}], "Several instances cannot share"),
])
def test_network_config_by_mac_refusals(ui, label, setup, entries, error):
    page, state, _ = ui
    panel = open_create(page)
    if setup == "count":
        panel.locator('.kvmf-head [name="count"]').fill("2")
    ci = gen_net(panel, entries)
    expect(ci.locator('.apply-result[data-section="ci-net"]')).to_contain_text(error)
    assert ci.locator('[data-ci="networkData"]').input_value() == ""
    assert state.get("macs_drawn", []) == ([] if setup == "count" else ["52:54:00:5e:00:01"])


def test_the_create_window_opens_before_the_catalogue_and_then_proposes_an_image(ui):
    # 1.101.2 : la fenêtre s'affiche sans attendre le catalogue ; à son
    # arrivée, le premier disque (non touché) devient l'image du catalogue
    page, state, dialogs = ui
    state["hold"] = "hosts/node2/images"
    panel = open_create(page)
    assert len(state.get("held", [])) == 1
    disks = tab(panel, "disks")
    src = disks.locator('[data-editor="kvm-disks"] [name$=".source"]').first
    expect(src).to_have_value("blank")
    expect(disks.locator('[data-kvmf="images-state"]')).to_be_visible()
    F.release(state)
    expect(disks.locator('[data-kvmf="images-state"]')).to_be_hidden()
    expect(src).to_have_value("image")
    expect(disks.locator('[data-editor="kvm-disks"] [name$=".image"]').first).to_have_value("images/cirros-0.6.3.qcow2")
    assert dialogs == []


def test_a_disk_touched_before_the_catalogue_is_kept(ui):
    page, state, dialogs = ui
    state["hold"] = "hosts/node2/images"
    panel = open_create(page)
    disks = tab(panel, "disks")
    size = disks.locator('[data-editor="kvm-disks"] [name$=".size"]').first
    size.fill("33G")
    size.dispatch_event("change")
    F.release(state)
    expect(disks.locator('[data-kvmf="images-state"]')).to_be_hidden()
    expect(disks.locator('[data-editor="kvm-disks"] [name$=".source"]').first).to_have_value("blank")
    expect(size).to_have_value("33G")
