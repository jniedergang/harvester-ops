"""v1.68.0 : les onglets PCI Devices, USB Devices et SR-IOV Networks dans un
navigateur : états, groupe IOMMU, VMs qui s'en servent, activer et désactiver
à l'unité ou par sélection, fonctions virtuelles, add-on absent."""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


def pci(name, addr, state="disabled", driver="e1000e", group="22", siblings=(), used=(), can=True, vf_of=""):
    return {"name": name, "address": addr, "node": "n3", "vendor_id": "8086", "device_id": "10d3", "class_id": "0200",
            "description": "Ethernet controller: Intel 82574L", "driver": driver, "original_driver": "e1000e",
            "resource": "intel.com/82574L", "iommu_group": group, "siblings": list(siblings), "state": state,
            "claimed_by": "ju" if state != "disabled" else "", "used_by": list(used), "vf_of": vf_of, "can_enable": can}


DEVICES = {"cluster": "harv-fake", "addon": True,
           "pci": [pci("n3-000005000", "0000:05:00.0", siblings=["n3-000005001"]),
                   pci("n3-000005001", "0000:05:00.1"),
                   pci("n3-000006000", "0000:06:00.0", state="enabled", driver="vfio-pci", used=["default/gw"], can=False),
                   pci("n3-000000010", "0000:00:01.0", group="", can=False)],
           "usb": [{"name": "n1-0627-0001-002002", "node": "n1", "vendor_id": "0627", "product_id": "0001",
                    "description": "QEMU Tablet", "path": "/dev/bus/usb/002/002", "pci_address": "0000:00:1d.7",
                    "resource": "kubevirt.io/n1-0627-0001-002002", "state": "disabled", "message": "", "claimed_by": "",
                    "used_by": [], "can_enable": True}],
           "sriov": [{"name": "n3-enp6s0", "node": "n3", "address": "0000:06:00.0", "interface": "enp6s0", "num_vfs": 0,
                      "enabled": False, "status": "", "vf_addresses": [], "vf_devices": [], "vfs_claimed": []}]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','namespaces');")
    page = context.new_page()
    sent, state = [], {"devices": DEVICES}

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "dev000000168"}, 202)
    page.route("**/api/devices/harv-fake", lambda r, q: fulfill(r, state["devices"]))
    page.route("**/api/devices/harv-fake/do/**", writes)
    page.route("**/api/stream/dev000000168", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.Devices && window.Sections && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    return page, sent, state


def tab(page, kind, rows):
    page.evaluate(f"Sections.open('advanced', '{kind}')")
    body = page.locator(f'#tab-advanced .section-pane[data-pane="{kind}"] [data-dev="body"]')
    expect(body.locator("tr[data-device]")).to_have_count(rows, timeout=8000)
    return body


def test_the_device_tabs_sit_in_advanced(ui):
    page, _, _ = ui
    assert page.locator("#tab-advanced [data-section-tab]").evaluate_all("els => els.map(e => e.dataset.sectionTab)") \
        == ["settings", "pci", "usb", "sriov", "support"]


def test_pci_rows_say_their_state_and_enable_one(ui):
    page, sent, _ = ui
    body = tab(page, "pci", 4)
    used = body.locator('tr[data-device="n3-000006000"]')
    expect(used.locator(".badge.ok")).to_have_count(1)
    expect(used.locator('[data-dev="disable"]')).to_be_disabled()            # une VM s'en sert
    expect(body.locator('tr[data-device="n3-000000010"] [data-dev="enable"]')).to_be_disabled()   # pas de groupe IOMMU
    body.locator('tr[data-device="n3-000005000"] [data-dev="enable"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/devices/harv-fake/do/pci-enable", {"names": ["n3-000005000"]})


def test_pci_devices_are_enabled_by_selection(ui):
    page, sent, _ = ui
    body = tab(page, "pci", 4)
    bulk = page.locator('#tab-advanced .section-pane[data-pane="pci"] [data-dev="bulk-enable"]')
    expect(bulk).to_be_disabled()
    body.locator('[data-dev="pick"][value="n3-000005000"]').check()
    body.locator('[data-dev="pick"][value="n3-000005001"]').check()
    expect(bulk).to_be_enabled()
    body.locator('[data-dev="pick"][value="n3-000006000"]').check()
    expect(bulk).to_be_disabled()                                            # déjà activé dans le lot
    body.locator('[data-dev="pick"][value="n3-000006000"]').uncheck()
    bulk.click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/devices/harv-fake/do/pci-enable", {"names": ["n3-000005000", "n3-000005001"]})


def test_usb_and_sriov(ui):
    page, sent, _ = ui
    body = tab(page, "usb", 1)
    body.locator('[data-dev="enable"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/devices/harv-fake/do/usb-enable", {"names": ["n1-0627-0001-002002"]})
    body = tab(page, "sriov", 1)
    body.locator('[data-dev="sriov-on"]').click()
    w = page.locator("#fp-dev-vfs-harv-fake-n3-enp6s0")
    w.locator('[name="vfs"]').fill("3")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/devices/harv-fake/do/sriov", {"name": "n3-enp6s0", "vfs": 3})


def test_without_the_addon_the_tab_says_so(ui):
    page, _, state = ui
    state["devices"] = dict(DEVICES, addon=False)
    page.evaluate("Sections.open('advanced', 'pci')")
    body = page.locator('#tab-advanced .section-pane[data-pane="pci"] [data-dev="body"]')
    expect(body.locator('[data-dev="open-addons"]')).to_be_visible(timeout=8000)
