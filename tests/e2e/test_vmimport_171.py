"""v1.71.0 : la section VM Import dans un navigateur : imports avec leurs
étapes et la progression des images, sources par type avec leur état,
formulaires de source (identifiants saisis) et d'import (correspondance des
réseaux, options VMware), raison d'un blocage lue dans le journal."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto  # noqa: E402

DATA = {"cluster": "harv-fake", "addon": {"enabled": True, "status": "AddonDeploySuccessful"}, "crds": True,
        "sources": [{"type": "ova", "kind": "OvaSource", "namespace": "mig", "name": "ova1", "endpoint": "http://10.0.0.1/a.ova",
                     "dc": "", "region": "", "http_timeout": None, "retry_count": None, "retry_delay": None, "secret": "",
                     "secret_ns": "", "state": "ready", "created": None, "users": ["mig/imp"]},
                    {"type": "vmware", "kind": "VmwareSource", "namespace": "mig", "name": "vc", "endpoint": "https://vc/sdk",
                     "dc": "DC1", "region": "", "http_timeout": None, "retry_count": None, "retry_delay": None, "secret": "vc-creds",
                     "secret_ns": "mig", "state": "pending", "created": None, "users": []}],
        "imports": [{"namespace": "mig", "name": "imp", "type": "ova", "source": "ova1", "source_ns": "mig", "vm_name": "cirros", "vm": "",
                     "phase": "diskImageSubmitted", "step": 4, "steps": 7, "done": False, "failed": False, "reason": "",
                     "disks": [{"name": "cirros-d1.img", "size": 117440512, "bus": "virtio", "image": "image-x", "progress": 64, "failed": ""}],
                     "networks": [], "storage_class": "", "conditions": [], "created": "2026-09-27T10:00:00Z"},
                    {"namespace": "mig", "name": "old", "type": "ova", "source": "ova1", "source_ns": "mig", "vm_name": "web", "vm": "web",
                     "phase": "virtualMachineRunning", "step": 7, "steps": 7, "done": True, "failed": False, "reason": "",
                     "disks": [], "networks": [], "storage_class": "", "conditions": [], "created": "2026-09-26T10:00:00Z"}],
        "nads": ["default/production", "mig/vlan20"], "classes": ["harv-rep1", "longhorn"], "default_class": "harv-rep1",
        "namespaces": ["default", "mig"], "nic_models": ["virtio", "e1000", "e1000e", "ne2k_pci", "pcnet", "rtl8139"],
        "disk_bus": ["virtio", "scsi", "sata", "usb"]}
LOG = {"lines": ['level=error msg="x" key=mig/imp err="admission webhook denied the request: displayName is not a valid Kubernetes label value"'],
       "reason": "admission webhook denied the request: displayName is not a valid Kubernetes label value"}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    sent = []

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "vi0000000171"}, 202)
    page.route("**/api/vmimport/harv-fake", lambda r, q: fulfill(r, DATA))
    page.route("**/api/vmimport/harv-fake/do/**", writes)
    page.route("**/api/vmimport-log/harv-fake/**", lambda r, q: fulfill(r, LOG))
    page.route("**/api/stream/vi0000000171", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address("namespaces"))   # 2.0.0 : VMs de l'hôte
    page.wait_for_function("window.VMImport && window.Sections && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    return page, sent


def pane(page, kind):
    page.evaluate(f"Sections.open('vmimport', '{kind}')")
    return page.locator(f'#tab-vmimport .section-pane[data-pane="{kind}"] [data-vi="body"]')


def test_imports_show_their_step_the_image_progress_and_why_they_are_blocked(ui):
    page, sent = ui
    body = pane(page, "imports")
    # 2.0.0 : l'onglet Import de VM du bandeau de l'hôte, avec son info-bulle traduite
    tab = page.locator('#host-page-head .hp-tabs a[data-hp-tab="vmimport"]')
    expect(tab).to_have_class(re.compile(r"\bactive\b"))
    assert tab.get_attribute("title") == page.evaluate("i18n.t('tab.vmimportTip')")
    row = body.locator('tr[data-name="imp"]')
    expect(row).to_contain_text("importing images", timeout=8000)
    expect(row).to_contain_text("step 4 of 7")
    expect(row).to_contain_text("64 %")
    expect(body.locator('tr[data-name="old"] [data-vi="open-vm"]')).to_have_text("web")
    row.locator('[data-vi="why"]').click()
    expect(page.locator("#fp-vi-log-harv-fake-mig-imp")).to_contain_text("displayName is not a valid Kubernetes label value")
    row.locator('[data-vi="follow"]').click()
    page.wait_for_timeout(400)
    assert sent[-1] == ("api/vmimport/harv-fake/do/import-follow", {"namespace": "mig", "name": "imp"})


def test_a_source_used_by_an_import_is_protected_and_a_vmware_source_is_typed(ui):
    page, sent = ui
    body = pane(page, "ova")
    expect(body.locator('tr[data-name="ova1"] [data-vi="del-source"]')).to_be_disabled(timeout=8000)
    expect(body.locator('tr[data-name="ova1"] .badge.ok')).to_have_count(1)
    vc = pane(page, "vmware")
    expect(vc.locator('tr[data-name="vc"] .badge.warn')).to_have_text("not checked")
    page.locator('#tab-vmimport .section-pane[data-pane="vmware"] [data-vi="new-source"]').click()
    w = page.locator("#fp-vi-src-harv-fake-vmware-new")
    w.locator('[name="name"]').fill("vc2")
    w.locator('[name="namespace"]').select_option("mig")
    w.locator('[name="endpoint"]').fill("https://vc2/sdk")
    w.locator('[name="dc"]').fill("DC2")
    w.locator('[name="username"]').fill("administrator@vsphere.local")
    w.locator('[name="password"]').fill("hunter2")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, b = sent[-1]
    assert path == "api/vmimport/harv-fake/do/source-apply"
    assert b["spec"]["dc"] == "DC2" and b["spec"]["credentials"]["mode"] == "new"
    assert b["spec"]["credentials"]["values"]["password"] == "hunter2"


def test_an_import_maps_networks_and_shows_the_name_harvester_will_use(ui):
    page, sent = ui
    pane(page, "imports")
    page.locator('#tab-vmimport [data-vi="new-import"]').click()
    w = page.locator("#fp-vi-imp-harv-fake-new")
    w.locator('[name="source"]').select_option("vmware|mig|vc")
    w.locator("summary").click()
    expect(w.locator("[data-vi-vmware]")).to_be_visible()                      # options VMware
    w.locator('[name="vm_name"]').fill("Web-01")
    expect(w.locator("[data-vi-final]")).to_have_text("The VM will be named web-01 in Harvester.")
    expect(w.locator('[name="name"]')).to_have_value("web-01")
    w.locator('[name="namespace"]').select_option("mig")
    w.locator('[name="n-src"]').fill("VM Network")
    w.locator('[name="n-dst"]').select_option("mig/vlan20")
    w.locator('[name="force_power_off"]').check()
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, b = sent[-1]
    assert path == "api/vmimport/harv-fake/do/import-create"
    s = b["spec"]
    assert s["source"] == {"type": "vmware", "namespace": "mig", "name": "vc"} and s["vm_name"] == "Web-01"
    assert s["networks"] == [{"source": "VM Network", "destination": "mig/vlan20", "model": ""}] and s["force_power_off"] is True
    w.locator('[name="source"]').select_option("ova|mig|ova1")
    expect(w.locator("[data-vi-vmware]")).to_be_hidden()


def test_a_vmware_source_pane_links_to_forklift(ui):
    page, _ = ui
    page.route("**/api/forklift/harv-fake", lambda r, q: fulfill(r, {
        "cluster": "harv-fake", "install": {"ready": False, "cert_manager": False, "cert_manager_missing": [],
                                             "addon": "absent", "addon_message": "", "operator": False,
                                             "controller": False, "components_missing": []},
        "harvester_addon": False, "bundle": False, "vddk": None,
        "registry": {"image": "", "host": "", "plain_http": False, "auth": False},
        "providers": [], "vmimport_sources": []}))
    page.route("**/api/forklift-vddk", lambda r, q: fulfill(r, {"archives": [], "free": 0}))
    vc = pane(page, "vmware")
    vc.locator('[data-vi="open-forklift"]').click(timeout=8000)
    expect(page.locator("#tab-forklift")).to_be_visible()


def test_a_disabled_add_on_is_said(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    page.route("**/api/vmimport/harv-fake", lambda r, q: fulfill(r, {**DATA, "addon": {"enabled": False, "status": "AddonDisabled"},
                                                                     "crds": False, "sources": [], "imports": []}))
    goto(page, flask_server["base_url"], address("namespaces"))   # 2.0.0 : VMs de l'hôte
    page.wait_for_function("window.VMImport && window.Sections && window.App && App.getCurrentCluster()")
    body = pane(page, "imports")
    expect(body.locator('[data-vi="open-addons"]')).to_be_visible(timeout=8000)
    expect(body).to_contain_text("vm-import-controller add-on is disabled")


def test_a_source_window_submits_to_the_cluster_it_was_opened_for(ui):
    """Relecture finale : la fenêtre envoyait au cluster affiché AU MOMENT de
    l'envoi, pas à celui pour lequel elle avait été ouverte."""
    page, sent = ui
    pane(page, "vmware")
    page.locator('#tab-vmimport .section-pane[data-pane="vmware"] [data-vi="new-source"]').click(timeout=8000)
    w = page.locator("#fp-vi-src-harv-fake-vmware-new")
    w.locator('[name="name"]').fill("vc2")
    w.locator('[name="endpoint"]').fill("https://vc2/sdk")
    w.locator('[name="dc"]').fill("DC2")
    w.locator('[name="username"]').fill("administrator@vsphere.local")
    w.locator('[name="password"]').fill("hunter2")
    other = []
    page.route("**/api/vmimport/harv-other", lambda r, q: fulfill(r, {**DATA, "cluster": "harv-other"}))
    page.route("**/api/vmimport/harv-other/do/**",
               lambda r, q: (other.append(q.url), fulfill(r, {"action_id": "vi0000000171"}, 202)))
    page.evaluate("VMImport.start('harv-other', document.querySelector("
                  "'#tab-vmimport .section-pane[data-pane=\"vmware\"] .na-host'))")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert not other, other
    assert sent[-1][0] == "api/vmimport/harv-fake/do/source-apply"
