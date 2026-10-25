"""Prochaine version KVM, reprises après revue des fenêtres Créer / Modifier
(API simulée).

- agent invité : décoché par défaut pour un démarrage sur ISO et pour
  Windows ; sans user-data ni agent ni clé, pas de seed ; un script shell
  passe case décochée, case cochée la fenêtre explique ;
- clés et agent ajoutés à un cloud-config en liste à indentation nulle (relu
  par PyYAML), forme entre crochets non extensible refusée sans rien envoyer ;
- fenêtre de création déjà ouverte : nouvel hôte et image appliqués, après
  accord si une saisie serait perdue ; réponse d'un hôte abandonné ignorée ;
- aria-invalid sur le contrôle ;
- cloud-init dans la fenêtre Modifier : rien n'est envoyé tant que le seed
  n'est pas lu, erreur de lecture dite, vide seulement par « Vider » (clear) ;
- relecture à trois voies après un 409 et annonce précoce par la relecture
  périodique ;
- badge « redémarrage nécessaire » servi par la liste, repère local effacé
  quand la VM est vue arrêtée ou redémarrée."""

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
        "if (!sessionStorage.getItem('kvmr-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "sessionStorage.setItem('kvmr-init','1'); }")
    page = context.new_page()
    state = F.install(page, {})
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    goto(page, flask_server["base_url"], "#activity")   # 2.0.0 : écran ouvert par son adresse
    page.wait_for_function("window.KVM && window.App && window.KVMForm && window.VMForm && window.CloudInitForm")
    page.mouse.move(1200, 600)
    return page, state, dialogs


def open_create(page, host="node2", image=None):
    arg = json.dumps({"image": image}) if image else "{}"
    page.evaluate(f"KVMForm.openCreate({json.dumps(host)}, {arg})")
    panel = page.locator("#fp-kvm-create")
    expect(panel.locator('.vm-create-section[data-vmf-section="general"]')).to_be_visible()
    return panel


def tab(panel, sid):
    panel.locator(f'.vm-edit-nav [data-section="{sid}"]').click()
    sec = panel.locator(f'.vm-create-section[data-vmf-section="{sid}"]')
    expect(sec).to_be_visible()
    return sec


def created_spec(state):
    return [s for s in state["sent"] if s[1] == "vms/node2"][-1][2]["spec"]


def open_menu(page, name):
    page.evaluate("Router.go('#vms/host/kvm')")   # 2.0.0 : KVM > Virtual machines, par son adresse
    row = page.locator("#tab-kvmvms tr[data-kvm-vm]").filter(has=page.locator(".kvm-vm-name", has_text=name)).first
    expect(row).to_be_visible()
    row.locator('[data-kvm="menu"]').click()
    menu = page.locator(".kvm-menu")
    expect(menu).to_be_visible()
    return menu


def open_edit(page, name="kvmtest-1"):
    open_menu(page, name).locator('[data-kvm-hot="edit"]').click()
    panel = page.locator(".floating-panel").filter(has=page.locator('.kvmf[data-kvmf-mode="edit"]')).last
    expect(panel.locator('.vm-create-section[data-vmf-section="general"]')).to_be_visible()
    return panel


# --- agent invité ------------------------------------------------------------------------

def test_agent_is_unchecked_for_an_iso_boot_and_no_seed_is_made_without_user_data(ui):
    page, state, dialogs = ui
    panel = open_create(page, image={"pool": "images", "volume": "netinst.iso", "iso": True})
    agent = panel.locator('.kvmf-head [name="head_agent"]')
    expect(agent).not_to_be_checked()
    panel.locator('.kvmf-head [name="name"]').fill("kvmtest-iso")
    panel.locator('[data-kvmf="create"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Created: kvmtest-iso")
    spec = created_spec(state)
    assert "cloudinit" not in spec and spec["guest_agent"] is False
    assert dialogs == []


def test_agent_follows_the_os_until_the_box_is_touched(ui):
    page, state, _ = ui
    panel = open_create(page)
    agent = panel.locator('.kvmf-head [name="head_agent"]')
    expect(agent).to_be_checked()                       # image cloud par défaut
    gen = tab(panel, "general")
    gen.locator('[name="os"]').select_option("windows")
    expect(agent).not_to_be_checked()
    gen.locator('[name="os"]').select_option("linux")
    expect(agent).to_be_checked()
    # la personne décide : la case ne bouge plus
    agent.uncheck()
    gen.locator('[name="os"]').select_option("other")
    gen.locator('[name="os"]').select_option("linux")
    expect(agent).not_to_be_checked()
    # disque de démarrage changé en ISO : défaut recalculé seulement tant que non touché
    panel2 = page.evaluate("KVMForm._internals.agentDefault([{source: 'iso', boot_order: 1}, {source: 'image', boot_order: 2}], 'linux')")
    assert panel2 is False
    assert page.evaluate("KVMForm._internals.agentDefault([{source: 'iso', boot_order: 2}, {source: 'image', boot_order: 1}], 'linux')") is True


def test_a_shell_script_passes_with_the_agent_unchecked_and_is_explained_when_checked(ui):
    page, state, _ = ui
    panel = open_create(page)
    panel.locator('.kvmf-head [name="name"]').fill("kvmtest-sh")
    ci = tab(panel, "cloudinit")
    script = "#!/bin/sh\necho hello > /tmp/x\n"
    ci.locator('[data-ci="userData"]').fill(script)
    hint = ci.locator("[data-ci-agent-hint]")
    expect(hint).to_be_visible()
    expect(hint).to_contain_text("can only add the agent to a cloud-config")
    panel.locator('[data-kvmf="create"]').click()
    expect(panel.locator('[data-kvmf="errors"]')).to_contain_text("uncheck \"Guest agent\"")
    expect(ci.locator('[data-ci="userData"]')).to_have_attribute("aria-invalid", "true")
    assert not [s for s in state["sent"] if s[1] == "vms/node2"]
    panel.locator('.kvmf-head [name="head_agent"]').uncheck()
    expect(hint).to_be_hidden()
    panel.locator('[data-kvmf="create"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Created: kvmtest-sh")
    assert created_spec(state)["cloudinit"]["user_data"] == script


def test_zero_indent_cloud_config_gets_keys_and_agent_at_its_own_indentation(ui):
    yaml = pytest.importorskip("yaml")
    page, state, _ = ui
    panel = open_create(page)
    panel.locator('.kvmf-head [name="name"]').fill("kvmtest-z")
    panel.locator('.kvmf-head [name="ssh_extra"]').fill("ssh-rsa AAAAB3NzaC1yc2E extra@x")
    ci = tab(panel, "cloudinit")
    ci.locator('[data-ci="userData"]').fill("#cloud-config\npackages:\n- vim\nruncmd:\n- echo hi\n")
    panel.locator('[data-kvmf="create"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Created: kvmtest-z")
    doc = yaml.safe_load(created_spec(state)["cloudinit"]["user_data"])
    assert doc == {"packages": ["vim", "qemu-guest-agent"],
                   "runcmd": ["echo hi", "systemctl enable --now qemu-guest-agent"],
                   "ssh_authorized_keys": ["ssh-rsa AAAAB3NzaC1yc2E extra@x"]}


def test_a_bracket_list_on_several_lines_is_refused_and_nothing_is_sent(ui):
    page, state, _ = ui
    panel = open_create(page)
    ci = tab(panel, "cloudinit")
    ci.locator('[data-ci="userData"]').fill("#cloud-config\npackages: [vim,\n  curl]\n")
    panel.locator('[data-kvmf="create"]').click()
    expect(panel.locator('[data-kvmf="errors"]')).to_contain_text('"packages" is written in brackets')
    assert not [s for s in state["sent"] if s[1] == "vms/node2"]
    assert ci.locator('[data-ci="userData"]').input_value() == "#cloud-config\npackages: [vim,\n  curl]\n"


def test_aria_invalid_lands_on_the_control_not_its_label(ui):
    page, state, _ = ui
    panel = open_create(page)
    comp = tab(panel, "compute")
    comp.locator('[name="cpu.vcpus"]').fill("9")
    panel.locator('[data-kvmf="create"]').click()
    expect(comp.locator('[name="cpu.vcpus"]')).to_have_attribute("aria-invalid", "true")
    assert comp.locator('[data-vmf-field="cpu.vcpus"]').get_attribute("aria-invalid") is None
    comp.locator('[name="cpu.vcpus"]').fill("1")
    panel.locator('[data-kvmf="create"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Created")
    assert comp.locator('[name="cpu.vcpus"]').get_attribute("aria-invalid") is None


# --- fenêtre déjà ouverte, course entre hôtes ----------------------------------------------

def test_reopening_the_create_window_applies_the_new_host_and_image(ui):
    page, state, _ = ui
    panel = open_create(page, "node2")
    # rien de saisi : appliqué tout de suite
    page.evaluate("KVMForm.openCreate('node9', {image: {pool: 'images', volume: 'netinst.iso', iso: true}})")
    expect(panel.locator('.kvmf-head [name="host"]')).to_have_value("node9")
    disks = tab(panel, "disks")
    expect(disks.locator('[name$=".source"]').first).to_have_value("iso")
    # une saisie : accord demandé dans la page, « non » garde tout
    panel.locator('.kvmf-head [name="name"]').fill("typed-name")
    page.evaluate("KVMForm.openCreate('node2')")
    box = panel.locator('[data-kvmf="retarget"]')
    expect(box).to_contain_text("Discard what you typed and start again on node2?")
    box.locator('[data-c="no"]').click()
    expect(panel.locator('.kvmf-head [name="host"]')).to_have_value("node9")
    assert panel.locator('.kvmf-head [name="name"]').input_value() == "typed-name"
    page.evaluate("KVMForm.openCreate('node2')")
    box.locator('[data-c="yes"]').click()
    expect(panel.locator('.kvmf-head [name="host"]')).to_have_value("node2")
    disks = tab(panel, "disks")
    expect(disks.locator('[name$=".source"]').first).to_have_value("image")


def test_a_late_answer_for_an_abandoned_host_is_ignored(ui):
    page, state, _ = ui
    state["pools9"] = [{"name": "only9", "state": "running", "type": "dir", "target": "/srv/9", "autostart": True,
                        "capacity": 2 ** 30, "allocation": 0, "available": 2 ** 30}]
    panel = open_create(page, "node2")
    state["hold"] = "hosts/node9/caps"
    sel = panel.locator('.kvmf-head [name="host"]')
    sel.select_option("node9")
    page.wait_for_function("() => true")
    sel.select_option("node2")
    expect(panel.locator('.vm-create-section[data-vmf-section="general"]')).to_be_visible()
    page.wait_for_timeout(300)
    F.release(state)                                     # la réponse de node9 arrive après coup
    page.wait_for_timeout(500)
    disks = tab(panel, "disks")
    pools = disks.locator('[name$=".pool"]').first.locator("option").evaluate_all("els => els.map(e => e.value)")
    assert "only9" not in pools and "images" in pools
    assert sel.input_value() == "node2"


# --- cloud-init dans la fenêtre Modifier (1.101.3 : seul chemin depuis le menu) ----------

def test_edit_window_sends_no_cloud_init_before_the_current_seed_is_read(ui):
    page, state, _ = ui
    state["hold"] = "vms/node2/kvmtest-1/cloudinit"
    panel = open_edit(page)
    ci = tab(panel, "cloudinit")
    ci.locator('[name="ci_rerun"]').check()
    panel.locator('[data-kvmf="apply"]').click()
    # seed pas encore lu : rien ne part (une zone vide effacerait le cloud-init)
    expect(panel.locator("[data-result]")).to_contain_text("Nothing to change.")
    assert not [s for s in state["sent"] if s[0] == "PATCH"]
    F.release(state)
    ta = ci.locator('[data-ci="userData"]')
    expect(ta).to_have_value(re.compile(F.USER_SECRET))
    # seed lu : la relance part avec le user-data actuel
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Changes applied")
    spec = [s for s in state["sent"] if s[0] == "PATCH"][-1][2]["spec"]
    assert spec == {"cloudinit": {"user_data": f"#cloud-config\nchpasswd:\n  list: |\n    root:{F.USER_SECRET}\n",
                                  "network_data": None, "rerun": True}}


def test_edit_window_cloud_init_read_failure_is_said_and_nothing_is_sent(ui):
    page, state, _ = ui
    state["ci_error"] = "node2: failed: the cloud-init seed images/x.iso could not be read"
    panel = open_edit(page)
    ci = tab(panel, "cloudinit")
    expect(ci.locator("[data-ci-state]")).to_contain_text("could not be read")
    ci.locator('[name="ci_rerun"]').check()
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Nothing to change.")
    assert not [s for s in state["sent"] if s[0] == "PATCH"]


def test_edit_window_needs_clear_to_send_an_empty_user_data(ui):
    page, state, _ = ui
    panel = open_edit(page)
    ci = tab(panel, "cloudinit")
    ta = ci.locator('[data-ci="userData"]')
    expect(ta).to_have_value(re.compile(F.USER_SECRET))
    ta.fill("")
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator('[data-kvmf="errors"]')).to_contain_text('use "Clear user-data"')
    assert not [s for s in state["sent"] if s[0] == "PATCH"]
    ci.locator("[data-ci-clear]").click()
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Changes applied")
    spec = [s for s in state["sent"] if s[0] == "PATCH"][-1][2]["spec"]
    assert spec == {"cloudinit": {"user_data": "", "network_data": None, "rerun": False, "clear": True}}


# --- relecture à trois voies -------------------------------------------------------------

def remote_form(state):
    form = F.vm_form(digest=F.DIGEST1)
    view = form["view"]
    view["title"] = "Remote title"                      # aussi modifié ici : conflit
    view["description"] = "Remote description"          # non touché : relu
    view["cpu"] = dict(view["cpu"], vcpus=3)            # non touché : relu
    return form


def test_409_lists_remote_fields_then_reload_keeps_edits_and_refreshes_the_rest(ui):
    page, state, dialogs = ui
    panel = open_edit(page)
    gen = tab(panel, "general")
    gen.locator('[name="title"]').fill("My title")
    state["form"] = remote_form(state)
    panel.locator('[data-kvmf="apply"]').click()
    ban = panel.locator(".kvmf-changed")
    expect(ban).to_contain_text("The VM was changed since you opened it")
    expect(ban.locator('[data-remote-field="title"]')).to_contain_text("also changed here")
    expect(ban.locator('[data-remote-field="description"]')).to_be_visible()
    expect(ban.locator('[data-remote-field="cpu.vcpus"]')).to_be_visible()
    # rien n'est encore accepté : la saisie et les anciennes valeurs restent
    assert gen.locator('[name="description"]').input_value() == "multi\nline ]]> &amp;"
    ban.locator('[data-kvmf="reload-now"]').click()
    expect(panel.locator(".kvmf-refreshed")).to_contain_text("Refreshed from the VM")
    expect(panel.locator(".kvmf-refreshed")).to_contain_text("Your edits kept")
    assert gen.locator('[name="title"]').input_value() == "My title"
    assert gen.locator('[name="description"]').input_value() == "Remote description"
    comp = tab(panel, "compute")
    assert comp.locator('[name="cpu.vcpus"]').input_value() == "3"
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("Changes applied")
    patch = [s for s in state["sent"] if s[0] == "PATCH"][-1][2]
    assert patch == {"spec": {"title": "My title"}, "expect_config": F.DIGEST1}
    assert dialogs == []


def test_the_poll_announces_a_remote_change_without_taking_the_digest(ui):
    page, state, _ = ui
    page.evaluate("KVMForm._internals.poll.ms = 300")
    panel = open_edit(page)
    gen = tab(panel, "general")
    gen.locator('[name="title"]').fill("Mine")
    state["form"] = remote_form(state)
    ban = panel.locator(".kvmf-changed")
    expect(ban).to_be_visible(timeout=5000)
    expect(ban.locator('[data-remote-field="description"]')).to_be_visible()
    # l'empreinte n'a pas été remplacée : un envoi sans relire est refusé
    panel.locator('[data-kvmf="apply"]').click()
    expect(panel.locator("[data-result]")).to_contain_text("The VM was changed")
    assert all(s[2].get("expect_config") == F.DIGEST0 for s in state["sent"] if s[0] == "PATCH")


# --- badge « redémarrage nécessaire » ----------------------------------------------------

def test_restart_badge_comes_from_the_list_and_the_local_flag_falls_when_the_vm_stops(ui):
    page, state, _ = ui
    vms = [dict(v) for v in F.VMS]
    vms[0].update(id=12, pending=["display.video"])
    state["vms"] = vms
    page.evaluate("Router.go('#vms/host/kvm')")   # 2.0.0 : KVM > Virtual machines, par son adresse
    row = page.locator('#tab-kvmvms tr[data-kvm-vm="node2/kvmtest-1"]')
    expect(row).to_contain_text("restart needed")
    # repère local (appris par une modification) : tombe quand la VM est vue arrêtée
    page.evaluate("KVMForm._internals.notePending('node2', 'kvmtest-1', ['machine'], 12)")
    vms[0].update(state="shut off", id=None, pending=[])
    page.evaluate("KVM.load(true)")
    expect(row).not_to_contain_text("restart needed")
    assert page.evaluate("KVMForm.restartFor({host: 'node2', name: 'kvmtest-1', state: 'running', id: 13, pending: []})") is None
    # redémarrée (autre Id) : même chose
    page.evaluate("KVMForm._internals.notePending('node2', 'kvmtest-1', ['machine'], 12)")
    assert page.evaluate("KVMForm.restartFor({host: 'node2', name: 'kvmtest-1', state: 'running', id: 12, pending: []})") == ["machine"]
    assert page.evaluate("KVMForm.restartFor({host: 'node2', name: 'kvmtest-1', state: 'running', id: 14, pending: []})") is None
