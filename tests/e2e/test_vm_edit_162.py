"""v1.62.0 : à la création, le fichier de réponses Windows et les volumes
virtiofs partent avec la demande ; dans les réglages, labels et annotations
en lignes clé / valeur (les clés du système jamais envoyées), et une carte
overlay réécrite en `managedtap` par Harvester reste modifiable (IP statique)."""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto  # noqa: E402

UNATTEND = '<unattend xmlns="urn:schemas-microsoft-com:unattend"><settings pass="oobeSystem"/></unattend>'
VM = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
      "metadata": {"name": "vm1", "namespace": "default", "resourceVersion": "7",
                   "labels": {"app": "shop", "harvesterhci.io/creator": "harvester"},
                   "annotations": {"owner": "ops", "harvesterhci.io/vmRunStrategy": "RerunOnFailure",
                                   "static-ip.harvesterhci.io/nic-1": "10.62.0.50"}},
      "spec": {"runStrategy": "RerunOnFailure", "template": {"metadata": {"labels": {"tier": "web"}}, "spec": {
          "domain": {"cpu": {"cores": 1, "sockets": 1, "threads": 1}, "memory": {"guest": "1Gi"},
                     "devices": {"disks": [], "interfaces": [
                         {"name": "nic-1", "binding": {"name": "managedtap"}, "model": "virtio", "macAddress": "0a:57:bb:d7:ca:b0"}]}},
          "networks": [{"name": "nic-1", "multus": {"networkName": "default/ovn-overlay"}}], "volumes": []}}}}


def boot(page, base_url):
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    goto(page, base_url, address('namespaces'))
    page.wait_for_function("window.VMEdit && window.App && App.getCurrentCluster()")


def test_the_answer_file_and_virtiofs_go_with_the_creation(context, flask_server):
    page = context.new_page()
    sent = {}

    def handle(route, request):
        sent.update(json.loads(request.post_data or "{}"))
        route.fulfill(status=202, content_type="application/json", body=json.dumps({"action_id": "act-162", "names": ["win"]}))
    page.route("**/api/vms/*/create", handle)
    boot(page, flask_server["base_url"])
    page.click('#btn-vm-create')
    page.wait_for_selector('#fp-vm-create', timeout=10000)
    page.fill('#fp-vm-create [name="name"]', 'win')
    page.click('#fp-vm-create [data-section="cloudinit"]')
    page.evaluate("document.querySelectorAll('#fp-vm-create details').forEach(d => d.open = true)")
    page.fill('#fp-vm-create [data-sysprep]', UNATTEND)
    row = page.locator('#fp-vm-create .vm-fs-row[data-fs-kind="configMap"]')
    assert row.locator('[data-fs="source"]').get_attribute("data-tip")
    row.locator('[data-fs="source"]').fill("app-cfg")
    page.click('#fp-vm-create [data-action="create"]')
    page.wait_for_timeout(900)
    assert sent["sysprep"] == UNATTEND
    assert sent["filesystems"] == [{"kind": "configMap", "source": "app-cfg"}] or \
        sent["filesystems"] == [{"kind": "configMap", "source": "app-cfg", "name": ""}]


def open_editor(page, base_url, patches):
    def vm_route(route, request):
        if request.method == "PATCH":
            patches.append(json.loads(request.post_data or "{}")["patch"])     # {patch, dry_run}
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(VM))
    page.route("**/api/vm/harv-fake/default/vm1", vm_route)
    page.route("**/api/vms/*", lambda r, q: r.fulfill(status=200, content_type="application/json", body='{"vms": []}'))
    boot(page, base_url)
    page.evaluate("() => window.VMEdit.open('harv-fake','default','vm1')")
    return page.locator(".floating-panel", has=page.locator(".vm-edit-nav")).last


def test_labels_and_annotations_as_key_value_rows(context, flask_server):
    page = context.new_page()
    patches = []
    ed = open_editor(page, flask_server["base_url"], patches)
    box = ed.locator('[data-kv-block="labels"]')
    expect(box).to_be_visible(timeout=8000)
    keys = box.locator('[data-kv="key"]').evaluate_all("els => els.map(e => e.value)")
    assert keys == ["app"]                                               # harvesterhci.io/creator caché
    annots = ed.locator('[data-kv-block="annots"] [data-kv="key"]').evaluate_all("els => els.map(e => e.value)")
    assert annots == ["owner"]
    box.locator("[data-kv-del]").first.click()
    ed.locator('[data-kv-block="annots"] [data-kv-add="annots"]').click()
    last = ed.locator('[data-kv-block="annots"] [data-kv-row]').last
    last.locator('[data-kv="key"]').fill("team")
    last.locator('[data-kv="value"]').fill("infra")
    ed.locator('[data-action="apply"][data-section="general"]').click()
    page.wait_for_timeout(1200)
    meta = patches[-1]["metadata"]
    assert meta["labels"]["app"] is None and "harvesterhci.io/creator" not in meta["labels"]
    assert meta["annotations"]["team"] == "infra" and meta["annotations"]["owner"] == "ops"
    assert "harvesterhci.io/vmRunStrategy" not in meta["annotations"]


def test_an_overlay_interface_keeps_its_static_ip_editable(context, flask_server):
    page = context.new_page()
    patches = []
    ed = open_editor(page, flask_server["base_url"], patches)
    ed.locator('.vm-edit-nav [data-section="network"]').click()
    ip = ed.locator('input[name$="static_ip"]').first
    expect(ip).to_have_value("10.62.0.50", timeout=8000)
    ip.fill("10.62.0.60")
    ed.locator('[data-action="apply"][data-section="network"]').click()
    page.wait_for_timeout(1200)
    p = patches[-1]
    assert p["metadata"]["annotations"] == {"static-ip.harvesterhci.io/nic-1": "10.62.0.60"}
    itf = p["spec"]["template"]["spec"]["domain"]["devices"]["interfaces"][0]
    assert itf["bridge"] == {} and itf["macAddress"] == "0a:57:bb:d7:ca:b0"
