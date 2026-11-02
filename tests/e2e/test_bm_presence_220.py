"""2.2.0 : la fenêtre d'installation bare-metal garde une machine qui porte
déjà un système.

* présent : signes listés, nom de la machine à taper, Installer inactif
  tant que le nom ne correspond pas (nom faux dit), actif ensuite et le nom
  part avec la demande ;
* absent : « aucun signe », sans rien affirmer d'autre ; inconnu : dit tel
  quel ; sans identifiants dans la page : inconnu, avec la raison ;
* refus du serveur (409) : la fenêtre montre ce que la console a relu ;
* info-bulles, échappement ; mutation volontaire.
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nav import bm_discover, goto  # noqa: E402

HOST = "192.0.2.42"
NODE = {"ok": True, "host": HOST, "manufacturer": "HPE", "model": "ProLiant XL170r Gen9", "power_state": "Off",
        "serial": "USE0000220", "nics": [{"name": "NIC", "mac": "94:18:82:00:02:20", "status": "Enabled"}],
        "uefi_targets": ["HD.Emb.1.1"], "virtualmedia_path": "/redfish/v1/Managers/1/VirtualMedia/2",
        "boot_targets": ["Cd", "Hdd"]}
ISOS = {"isos": [{"name": "harvester-v1.9.0-amd64.iso", "size": 1, "sha256": "ab" * 32}], "disk_free": 10 ** 11}
PRESENT = {"state": "present", "confirm_name": "USE0000220", "source": "hpe-oem", "host": HOST, "evidence": [
    {"code": "boots-from-disk", "name": "sles-secureboot"},
    {"code": "boot-entry", "name": "sles-secureboot", "loader": "\\EFI\\sles\\shim.efi", "disk": "HD.Emb.5.2"},
    {"code": "volume", "name": "Logical Drive 1", "controller": "Smart Array"}]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def open_window(context, flask_server, check, install=None, password="bmc-secret"):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    asked = {"check": [], "install": []}
    page.route("**/api/isos", lambda r, q: fulfill(r, ISOS))
    page.route("**/api/bmc/last-discovery", lambda r, q: fulfill(r, {"discovery": None}))
    page.route("**/api/bmc/discover", lambda r, q: fulfill(r, {"nodes": [NODE], "count": 1, "at": 1767000000}))

    def on_check(route, req):
        asked["check"].append(json.loads(req.post_data or "{}"))
        fulfill(route, check) if isinstance(check, dict) else fulfill(route, {"error": "x"}, check)
    page.route("**/api/bmc/system-check", on_check)

    def on_install(route, req):
        asked["install"].append(json.loads(req.post_data or "{}"))
        body, status = install or ({"action_id": "a00000000220", "hostname": "n1"}, 202)
        fulfill(route, body, status)
    page.route("**/api/baremetal/install", on_install)
    page.on("dialog", lambda d: d.accept())
    goto(page, flask_server["base_url"], "#automation/pxe/machines")
    bm_discover(page, HOST, password)
    page.locator(f'#bmc-discover-result tr.bm-machine[data-host="{HOST}"] .bmc-install').click()
    win = page.locator(f'[id="fp-bm-install-{HOST}"]')
    expect(win.locator("#bm-install-form")).to_be_visible()
    return page, win, asked


def fill_minimum(win):
    form = win.locator("#bm-install-form")
    form.locator('[name="ip"]').fill("192.0.2.10")
    form.locator('[name="gateway"]').fill("192.0.2.1")
    form.locator('[name="vip"]').fill("192.0.2.100")
    form.locator('[name="token"]').fill("token-220")
    form.locator('[name="password"]').fill("os-password-220")
    form.locator('[data-bm="free-text"]').check()


def test_a_present_system_is_listed_and_the_name_must_be_typed(context, flask_server):
    page, win, asked = open_window(context, flask_server, PRESENT)
    box = win.locator('[data-bm="system"] .bm-system-present')
    expect(box).to_contain_text("A system seems present on this machine")
    expect(box).to_contain_text('The machine boots first from "sles-secureboot"')
    expect(box).to_contain_text("\\EFI\\sles\\shim.efi")
    expect(box).to_contain_text("HD.Emb.5.2")
    expect(box).to_contain_text('Logical drive "Logical Drive 1" exists on Smart Array')
    expect(box.locator(".bm-system-name")).to_have_text("USE0000220")
    # la lecture part avec les identifiants de la page, jamais dans l'adresse
    assert asked["check"][0]["host"] == HOST and asked["check"][0]["password"] == "bmc-secret"
    go = win.locator('[data-bm="install-go"]')
    expect(go).to_be_disabled()
    typed = box.locator('[name="system_confirm"]')
    assert typed.get_attribute("title") and box.locator(".bm-system-confirm").get_attribute("data-tip")
    assert box.locator('[data-bm="system-recheck"]').get_attribute("data-tip")
    # nom faux : toujours inactif, et dit
    typed.fill("USE000022")
    expect(go).to_be_disabled()
    expect(box.locator(".bm-system-mismatch")).to_be_visible()
    typed.fill("use0000220")
    expect(go).to_be_disabled()
    # bon nom : actif, et le nom part avec la demande
    typed.fill("USE0000220")
    expect(go).to_be_enabled()
    expect(box.locator(".bm-system-mismatch")).to_be_hidden()
    fill_minimum(win)
    go.click()
    page.wait_for_function("() => true")
    expect(win.locator("#bm-install-result")).to_contain_text("a00000000220")
    assert asked["install"][0]["system_confirm"] == "USE0000220"


@pytest.mark.parametrize("check,text,cls", [
    ({"state": "absent", "evidence": [], "confirm_name": "USE0000220"}, "No sign of an installed system", "absent"),
    ({"state": "unknown", "evidence": [], "confirm_name": "USE0000220"},
     "Unknown: the BMC does not tell whether a system is present", "unknown"),
    (502, "Unknown: the BMC does not tell whether a system is present", "unknown"),   # lecture en échec
])
def test_absent_and_unknown_say_so_and_do_not_block(context, flask_server, check, text, cls):
    page, win, _ = open_window(context, flask_server, check)
    box = win.locator(f'[data-bm="system"] .bm-system-{cls}')
    expect(box).to_contain_text(text)
    expect(win.locator('[name="system_confirm"]')).to_have_count(0)
    expect(win.locator('[data-bm="install-go"]')).to_be_enabled()
    if cls == "unknown":
        # « inconnu » n'est jamais présenté comme une machine vide
        assert "No sign" not in box.inner_text()


def test_without_credentials_in_the_page_the_check_is_unknown(context, flask_server):
    """Page rechargée : la dernière découverte revient sans identifiants ;
    rien n'est lu, l'état est inconnu et la raison est dite."""
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    asked = []
    page.route("**/api/isos", lambda r, q: fulfill(r, ISOS))
    page.route("**/api/bmc/last-discovery", lambda r, q: fulfill(r, {"discovery": {
        "at": 1767000000, "hosts": [HOST], "nodes": [NODE]}}))
    page.route("**/api/bmc/system-check", lambda r, q: asked.append(1) or fulfill(r, PRESENT))
    goto(page, flask_server["base_url"], "#automation/pxe/machines")
    page.locator(f'#bmc-discover-result tr.bm-machine[data-host="{HOST}"] .bmc-install').click()
    win = page.locator(f'[id="fp-bm-install-{HOST}"]')
    expect(win.locator('[data-bm="system"] .bm-system-unknown')).to_contain_text("credentials")
    assert asked == []


def test_check_again_reads_the_bmc_again(context, flask_server):
    page, win, asked = open_window(context, flask_server, {"state": "absent", "evidence": [], "confirm_name": "x"})
    expect(win.locator(".bm-system-absent")).to_be_visible()
    win.locator('[data-bm="system-recheck"]').click()
    page.wait_for_function("() => true")
    expect(win.locator(".bm-system-absent")).to_be_visible()
    assert len(asked["check"]) == 2


def test_a_server_refusal_shows_what_the_console_read(context, flask_server):
    """La page a dit « absent », la console relit le BMC à l'installation et
    trouve un système : le refus (409) remplace l'état, le nom est demandé."""
    refusal = ({"error": "a system seems present on this machine: type its name to install",
                "fields": ["system_confirm"], "system": PRESENT}, 409)
    page, win, asked = open_window(context, flask_server, {"state": "absent", "evidence": [], "confirm_name": "x"},
                                   install=refusal)
    fill_minimum(win)
    win.locator('[data-bm="install-go"]').click()
    box = win.locator('[data-bm="system"] .bm-system-present')
    expect(box).to_contain_text("The console refused the installation")
    expect(box.locator(".bm-system-name")).to_have_text("USE0000220")
    expect(win.locator('[data-bm="install-go"]')).to_be_disabled()
    expect(win.locator("#bm-install-result")).to_contain_text("refused")
    assert len(asked["install"]) == 1


def test_the_evidence_is_escaped(context, flask_server):
    evil = dict(PRESENT, confirm_name='<b id="pwn-name">x</b>', evidence=[
        {"code": "boot-entry", "name": '<img src=x onerror="window.__pwn=1">', "loader": "<i>", "disk": "<u>"}])
    page, win, _ = open_window(context, flask_server, evil)
    expect(win.locator(".bm-system-present")).to_be_visible()
    assert win.locator(".bm-system img, #pwn-name").count() == 0
    assert page.evaluate("window.__pwn") is None


def test_mutation_a_gate_that_ignores_the_name_is_seen(context, flask_server):
    """Mutation volontaire : un bouton Installer laissé actif sans le nom
    tapé est vu par le contrôle de l'état du bouton."""
    page, win, _ = open_window(context, flask_server, PRESENT)
    go = win.locator('[data-bm="install-go"]')
    expect(go).to_be_disabled()
    page.evaluate(f"document.querySelector('[id=\"fp-bm-install-{HOST}\"] [data-bm=\"install-go\"]').disabled = false")
    with pytest.raises(AssertionError):
        expect(go).to_be_disabled(timeout=500)
