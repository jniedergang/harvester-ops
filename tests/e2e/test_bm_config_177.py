"""v1.77.0 : la fenêtre d'installation bare-metal reçoit une configuration
complète (agrégat de gestion, VLAN, disques, libellés, modules, YAML avancé),
importe un fichier d'exploitant et montre le YAML final.

La découverte BMC et le lancement sont simulés ; l'import et l'aperçu passent
par les VRAIES routes du serveur de test (sans effet de bord), sur la copie
anonymisée du fichier d'exploitant. Aucune installation réelle ne part : la
route d'installation est interceptée et répond un faux 202."""

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/api/fixtures/bm_config_177/operator.yaml"
HOST = "192.0.2.31"
MACS = ["94:18:82:00:00:01", "94:18:82:00:00:02"]
NODE = {"ok": True, "host": HOST, "model": "ProLiant XL170r Gen9", "power_state": "Off",
        "serial": "SN0001", "bios_version": "U14", "memory_gib": 128,
        "nics": [{"name": "System Ethernet Interface", "mac": MACS[0], "status": "Up", "speed_mbps": 1000},
                 {"name": "System Ethernet Interface", "mac": MACS[1], "status": "Down", "speed_mbps": None}],
        "uefi_targets": ["HD.Emb.1.1"], "virtualmedia_path": "/redfish/v1/Managers/1/VirtualMedia/2",
        "boot_targets": ["Cd", "Hdd"]}
ISOS = {"isos": [{"name": "harvester-v1.9.0-amd64.iso", "size": 7 * 2 ** 30, "sha256": "ab" * 32}],
        "disk_free": 10 ** 11}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def shot(page, name):
    """Capture facultative (E2E_SHOT_DIR), pour relire la fenêtre à l'œil."""
    d = os.environ.get("E2E_SHOT_DIR")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(d) / f"{name}.png"))


def open_window(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    installs = []

    def install(route, req):
        installs.append(req.post_data_json)
        fulfill(route, {"action_id": "bm0000000177", "hostname": "x"}, 202)
    page.route("**/api/isos", lambda r, q: fulfill(r, ISOS))
    page.route("**/api/bmc/discover", lambda r, q: fulfill(r, {"nodes": [NODE]}))
    page.route("**/api/baremetal/install", install)
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.BMC && window.FloatingPanels && window.i18n")
    page.on("dialog", lambda d: d.accept())
    page.click('.tab-group-head[data-group="automation"]')
    page.click('.tab-child[data-subtab="pxe"]')
    # le menu latéral s'ouvre en calque au survol : écarter la souris, sinon
    # il recouvre la fenêtre (ouverte à 120 px du bord) et prend les clics
    page.mouse.move(1100, 450)
    form = page.locator("#bmc-discover-form")
    expect(form).to_be_visible()
    form.locator('[name="hosts"]').fill(HOST)
    form.locator('[name="password"]').fill("bmc-secret")
    form.locator('button[type="submit"]').click()
    page.locator(f'.bmc-install[data-host="{HOST}"]').click()
    win = page.locator(f'[id="fp-bm-install-{HOST}"]')
    expect(win.locator("#bm-install-form")).to_be_visible()
    return page, win, installs


def do_import(win):
    win.locator('[data-bm="file"]').set_input_files(str(FIXTURE))
    expect(win.locator(".bm-imported")).to_be_visible()


def test_fresh_window_keeps_the_historical_defaults(context, flask_server):
    page, win, _ = open_window(context, flask_server)
    boxes = win.locator('[name="mgmt_nic"]')
    expect(boxes).to_have_count(2)
    expect(boxes.nth(0)).to_be_checked()
    expect(boxes.nth(1)).not_to_be_checked()
    expect(win.locator('[name="bond_mode"]')).to_have_value("balance-tlb")
    expect(win.locator('[name="bond_miimon"]')).to_have_value("100")
    expect(win.locator('[name="bond_lacp_rate"]')).to_be_hidden()
    expect(win.locator('[name="bond_xmit_hash_policy"]')).to_be_hidden()
    expect(win.locator(".bm-wipe-warn")).to_be_visible()
    # la fenêtre tient dans l'écran, son contenu défile à l'intérieur
    box = win.bounding_box()
    assert box["y"] + box["height"] <= page.viewport_size["height"]
    shot(page, "bm-install-fresh")


def test_802_3ad_shows_its_options(context, flask_server):
    _, win, _ = open_window(context, flask_server)
    win.locator('[name="bond_mode"]').select_option("802.3ad")
    expect(win.locator('[name="bond_lacp_rate"]')).to_be_visible()
    expect(win.locator('[name="bond_xmit_hash_policy"]')).to_be_visible()
    win.locator('[name="bond_mode"]').select_option("active-backup")
    expect(win.locator('[name="bond_lacp_rate"]')).to_be_hidden()


def test_dhcp_hides_the_static_addressing(context, flask_server):
    _, win, _ = open_window(context, flask_server)
    for name in ("ip", "subnet_mask", "gateway"):
        expect(win.locator(f'[name="{name}"]')).to_be_visible()
    win.locator('[name="method"]').select_option("dhcp")
    for name in ("ip", "subnet_mask", "gateway"):
        expect(win.locator(f'[name="{name}"]')).to_be_hidden()
        assert win.locator(f'[name="{name}"]').evaluate("e => e.required") is False


def test_import_fills_the_form_and_says_the_iso_url_is_replaced(context, flask_server):
    page, win, _ = open_window(context, flask_server)
    do_import(win)
    val = lambda n: win.locator(f'[name="{n}"]').input_value()  # noqa: E731
    assert val("hostname") == "hv-node-01"
    assert val("device") == "/dev/disk/by-path/pci-0000:02:00.0-nvme-1"
    assert val("data_disk") == "/dev/sda"
    expect(win.locator('[name="wipe_all_disks"]')).to_be_checked()
    assert val("bond_mode") == "802.3ad"
    expect(win.locator('[name="bond_lacp_rate"]')).to_be_visible()
    assert val("bond_lacp_rate") == "fast"
    assert val("bond_xmit_hash_policy") == "layer3+4"
    assert val("vlan_id") == "200"
    assert val("labels") == "topology.kubernetes.io/zone=zone-a\nharvester.cattle.io/nic-profile=generic-1u"
    assert val("modules") == "rbd, nbd"
    assert "write_files" in val("advanced_yaml")
    assert "auto-disk-provision-paths" in val("advanced_yaml")
    # les deux cartes du fichier sont des NOMS : gardées, cochées ; les MAC
    # découvertes décochées
    checked = win.locator('[name="mgmt_nic"]:checked')
    assert [checked.nth(i).input_value() for i in range(checked.count())] == ["ens1f1np1", "ens15f1np1"]
    # jeton et mot de passe restés côté serveur
    for name in ("token", "password"):
        f = win.locator(f'[name="{name}"]')
        assert f.input_value() == ""
        assert f.get_attribute("placeholder") == "taken from the imported file"
        assert f.evaluate("e => e.required") is False
    expect(win.locator("#bm-config-msg")).to_contain_text("iso_url of the file is replaced")
    assert "example-token" not in page.content() and "example-password" not in page.content()
    shot(page, "bm-install-imported")


def test_preview_shows_the_yaml_without_the_secrets(context, flask_server):
    page, win, _ = open_window(context, flask_server)
    do_import(win)
    win.locator('[data-bm="preview"]').click()
    text = page.locator(f'[id="fp-bm-preview-{HOST}"] .bm-preview-text')
    expect(text).to_be_visible()
    yaml_text = text.input_value()
    assert "write_files" in yaml_text and "bond_options" in yaml_text
    assert "vlan_id: 200" in yaml_text
    assert "example-token" not in yaml_text and "example-password" not in yaml_text
    assert "•••" in yaml_text
    shot(page, "bm-install-preview")


def test_an_unknown_key_is_refused_with_its_path(context, flask_server):
    _, win, _ = open_window(context, flask_server)
    win.locator('[name="advanced_yaml"]').fill("os:\n  write_filez: []\n")
    win.locator('[data-bm="preview"]').click()
    msg = win.locator("#bm-config-msg .bm-refusal")
    expect(msg).to_be_visible()
    expect(msg).to_contain_text("os.write_filez")
    expect(msg).to_contain_text("unknown key for the installer")


def test_a_reserved_key_in_the_advanced_yaml_is_refused(context, flask_server):
    _, win, _ = open_window(context, flask_server)
    win.locator('[name="advanced_yaml"]').fill("install:\n  iso_url: http://192.0.2.9/x.iso\n")
    win.locator('[data-bm="preview"]').click()
    msg = win.locator("#bm-config-msg .bm-refusal")
    expect(msg).to_contain_text("install.iso_url")
    expect(msg).to_contain_text("kept by the console")


def test_install_sends_interfaces_advanced_yaml_and_import_id(context, flask_server):
    page, win, installs = open_window(context, flask_server)
    do_import(win)
    win.locator('#bm-install-form button[type="submit"]').click()
    expect(win.locator("#bm-install-result")).to_contain_text("bm0000000177")
    body = installs[-1]
    assert body["mgmt_interfaces"] == ["ens1f1np1", "ens15f1np1"]
    assert "mgmt_nic" not in body
    assert "write_files" in body["advanced_yaml"]
    assert body["import_id"]
    assert body["token"] == "" and body["password"] == ""
    assert body["wipe_all_disks"] is True
    assert body["bond_mode"] == "802.3ad" and body["bond_lacp_rate"] == "fast"
    assert body["bmc_host"] == HOST and body["bmc_password"] == "bmc-secret"
    assert "server_url" not in body


def test_install_refusal_lists_the_paths(context, flask_server):
    page, win, installs = open_window(context, flask_server)
    page.unroute("**/api/baremetal/install")
    page.route("**/api/baremetal/install", lambda r, q: fulfill(r, {
        "error": "invalid configuration", "fields": ["install.management_interface.vlan_id"],
        "reasons": {"install.management_interface.vlan_id": "range:0-4094"}}, 400))
    win.locator('[name="ip"]').fill("192.0.2.10")
    win.locator('[name="gateway"]').fill("192.0.2.1")
    win.locator('[name="vip"]').fill("192.0.2.100")
    win.locator('[name="token"]').fill("t0k3n")
    win.locator('[name="password"]').fill("pw")
    win.locator('#bm-install-form button[type="submit"]').click()
    msg = win.locator("#bm-config-msg .bm-refusal")
    expect(msg).to_contain_text("install.management_interface.vlan_id")
    expect(msg).to_contain_text("out of range (0-4094)")


def test_install_needs_a_management_interface(context, flask_server):
    _, win, installs = open_window(context, flask_server)
    win.locator('[name="mgmt_nic"]').nth(0).uncheck()
    for name, v in (("ip", "192.0.2.10"), ("gateway", "192.0.2.1"), ("vip", "192.0.2.100"),
                    ("token", "t"), ("password", "p")):
        win.locator(f'[name="{name}"]').fill(v)
    win.locator('#bm-install-form button[type="submit"]').click()
    expect(win.locator("#bm-install-result")).to_contain_text("at least one management interface")
    assert installs == []
