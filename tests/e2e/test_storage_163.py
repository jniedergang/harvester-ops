"""v1.63.0 : les menus d'un volume et d'une image, dans un navigateur.

Le menu d'un volume suit son état (grisé et pourquoi : utilisé par une VM,
pas de CDI) ; chaque fenêtre envoie ce que l'outil attend. La liste des
images a son bouton Actions et l'envoi d'un fichier ; « Créer une VM »
ouvre la création avec un disque fait de l'image.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402
from nav import address, goto  # noqa: E402

INFO = {"name": "data", "namespace": "default", "size": "10Gi", "capacity": "10Gi", "phase": "Bound",
        "storage_class": "harvester-longhorn", "description": "old", "image": None, "used_by": [], "resizing": True,
        "longhorn_v1": True, "snapshot_class": "longhorn-snapshot", "cdi": True,
        "storage_classes": [{"name": "harvester-longhorn", "longhorn_v1": True, "encrypted": False, "image": False, "internal": False},
                            {"name": "fast", "longhorn_v1": True, "encrypted": False, "image": False, "internal": False},
                            {"name": "vmstate-persistence", "longhorn_v1": True, "encrypted": False, "image": False, "internal": True}]}
IMAGES = {"items": [{"namespace": "default", "name": "image-a", "display_name": "leap.qcow2", "source_type": "download",
                     "url": "https://x/leap.qcow2", "backend": "backingimage", "size": 1, "virtual_size": 3 * 2**30,
                     "storage_class": "lh-abc", "state": "ready", "volumes": 0, "used_by": [], "created": "2026-09-01T00:00:00Z",
                     "description": "old", "labels": {"env": "prod"}, "encrypted": False}]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','fr');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    sent = []

    def writes(route, req):
        sent.append((req.method, req.url.split("://")[1].split("/", 1)[1], req.post_data_json if req.method == "POST" else None))
        fulfill(route, {"action_id": "sto000000163"}, 202)
    page.route("**/api/volume/harv-fake/default/data/info", lambda r, q: fulfill(r, INFO))
    page.route("**/api/volume/harv-fake/default/data/do/**", writes)
    page.route("**/api/image/harv-fake/default/image-a/do/**", writes)
    page.route("**/api/image-upload/harv-fake/**", writes)
    page.route("**/api/cluster-objects/harv-fake/images", lambda r, q: fulfill(r, IMAGES))
    page.route("**/api/cluster-objects/harv-fake/storageclasses", lambda r, q: fulfill(r, {"items": [
        {"name": "harvester-longhorn", "provisioner": "driver.longhorn.io", "is_default": True, "parameters": {}},
        {"name": "enc", "provisioner": "driver.longhorn.io", "parameters": {"encrypted": "true"}}]}))
    page.route("**/api/namespaces/harv-fake", lambda r, q: fulfill(r, [{"name": "default"}]))
    page.route("**/api/stream/sto000000163", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address("namespaces"))
    page.wait_for_function("window.StorageActions && window.App && App.getCurrentCluster()")
    page.evaluate("""() => { const a = document.createElement('button'); a.id = 'anc'; a.textContent = 'x';
        a.style.position = 'fixed'; a.style.top = '100px'; a.style.right = '40px'; document.body.appendChild(a); }""")
    return page, sent


def vol_menu(page):
    page.evaluate("StorageActions.volumeMenu(document.getElementById('anc'), 'harv-fake', 'default', 'data', () => {})")
    expect(page.locator(".sta-menu .vma-item").first).to_be_visible(timeout=5000)
    return page.locator(".sta-menu")


def test_the_volume_menu_follows_the_state(ui):
    page, _ = ui
    m = vol_menu(page)
    for a in ("clone", "export", "snapshot", "copy", "cancel-expand", "describe", "delete"):
        expect(m.locator(f'[data-sta="{a}"]')).to_be_visible()
        assert m.locator(f'[data-sta="{a}"]').get_attribute("data-tip")
    page.keyboard.press("Escape")
    INFO_USED = dict(INFO, used_by=["web"], cdi=False)
    page.unroute("**/api/volume/harv-fake/default/data/info")
    page.route("**/api/volume/harv-fake/default/data/info", lambda r, q: fulfill(r, INFO_USED))
    m = vol_menu(page)
    expect(m.locator('[data-sta="delete"]')).to_have_attribute("aria-disabled", "true")
    assert "web" in m.locator('[data-sta="delete"]').get_attribute("data-tip")
    expect(m.locator('[data-sta="copy"]')).to_have_attribute("aria-disabled", "true")


def test_volume_windows_send_what_the_tool_expects(ui):
    page, sent = ui
    vol_menu(page).locator('[data-sta="clone"]').click()
    w = page.locator("#fp-sta-clone-harv-fake-default-data")
    w.locator('[name="with_data"]').uncheck()
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("POST", "api/volume/harv-fake/default/data/do/clone", {"new_name": "data-clone", "with_data": False})
    vol_menu(page).locator('[data-sta="copy"]').click()
    w = page.locator("#fp-sta-copy-harv-fake-default-data")
    expect(w.locator('[name="storage_class"] option')).to_have_count(1)          # ni la classe du volume ni une interne
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][2] == {"new_name": "data-copy", "storage_class": "fast"}
    vol_menu(page).locator('[data-sta="export"]').click()
    w = page.locator("#fp-sta-export-harv-fake-default-data")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][2] == {"display_name": "data-image", "storage_class": "harvester-longhorn"}


def test_the_image_menu_and_its_windows(ui):
    page, sent = ui
    page.evaluate("App.setTab('storage')")
    page.evaluate("document.querySelector('[data-section-tab=\"images\"]') && document.querySelector('[data-section-tab=\"images\"]').click()")
    more = page.locator('[data-resource="images"] [data-act="more"]').first
    expect(more).to_be_visible(timeout=8000)
    expect(page.locator('[data-resource="images"] .res-upload')).to_be_visible()
    more.click()
    m = page.locator(".sta-menu")
    expect(m.locator('[data-sta="encrypt"]')).to_be_visible(timeout=5000)
    m.locator('[data-sta="edit"]').click()
    w = page.locator("#fp-sta-edit-harv-fake-default-image-a")
    w.locator('[name="description"]').fill("nouvelle")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("POST", "api/image/harv-fake/default/image-a/do/edit", {"description": "nouvelle", "labels": {"env": "prod"}})
    more.click()
    page.locator('.sta-menu [data-sta="encrypt"]').click()
    w = page.locator("#fp-sta-encrypt-harv-fake-default-image-a")
    expect(w.locator('[name="storage_class"] option')).to_have_count(1)          # la classe chiffrée seulement
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][2] == {"display_name": "leap.qcow2-enc", "storage_class": "enc"}


def test_create_vm_from_an_image_and_upload(ui):
    page, sent = ui
    page.evaluate("StorageActions.imageMenu(document.getElementById('anc'), 'harv-fake', " + json.dumps(IMAGES["items"][0]) + ", () => {})")
    page.locator('.sta-menu [data-sta="createvm"]').click()
    page.wait_for_selector('#fp-vm-create', timeout=10000)
    page.click('#fp-vm-create .vm-edit-nav [data-section="disks"]')
    page.wait_for_timeout(800)
    vals = page.locator('#fp-vm-create [name$=".image"]').evaluate_all("els => els.map(e => e.value)")
    assert "default/image-a" in vals
    page.evaluate("StorageActions.uploadDialog('harv-fake', () => {})")
    w = page.locator("#fp-sta-upload-harv-fake")
    w.locator('[name="file"]').set_input_files({"name": "cirros.qcow2", "mimeType": "application/octet-stream", "buffer": b"QFI\xfb" + b"\0" * 64})
    w.locator('[name="display_name"]').fill("cirros")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(800)
    method, path, _ = sent[-1]
    assert method == "PUT" and path.startswith("api/image-upload/harv-fake/default?")
    assert "display_name=cirros" in path and "file_name=cirros.qcow2" in path and "storage_class=harvester-longhorn" in path


def test_a_cdi_image_prepares_its_download_then_fetches_it(ui):
    """v1.74.0 : une image CDI (hors Longhorn v1) se télécharge : Harvester
    convertit d'abord le volume (downloader), suivi comme une action, puis
    le fichier qcow2 part."""
    page, sent = ui
    cdi = dict(IMAGES["items"][0], backend="cdi", storage_class="lvm-sc")
    page.evaluate("document.body.insertAdjacentHTML('beforeend', '<button id=\"anc\">x</button>')")
    page.evaluate("StorageActions.imageMenu(document.getElementById('anc'), 'harv-fake', " + json.dumps(cdi) + ", () => {})")
    item = page.locator('.sta-menu [data-sta="download"]')
    expect(item).to_be_enabled(timeout=5000)                                  # plus réservé à Longhorn v1
    item.click()
    w = page.locator("#fp-sta-download-harv-fake-default-image-a")
    expect(w).to_contain_text("qcow2", timeout=5000)                          # la conversion est annoncée
    with page.expect_download(timeout=10000) as dl:
        w.locator('button[type="submit"]').click()
    assert sent[-1] == ("POST", "api/image/harv-fake/default/image-a/do/prepare-download", {})
    assert dl.value.url.endswith("/api/image/harv-fake/default/image-a/download")   # après la préparation
