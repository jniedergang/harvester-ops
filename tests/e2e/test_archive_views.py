"""v1.47.0 : l'archive d'un export, et le dépôt d'une archive, dans un navigateur.

Demandé par l'exploitant : « quand je passe par un export fichier, comment
est-ce que je récupère l'image ? comment la réimporter ? ». La fin d'un
export désigne maintenant l'archive (télécharger, importer, magasin), et le
magasin accepte une archive déposée depuis le poste.

Le dépôt passe par le VRAI serveur de test (magasin isolé dans le répertoire
de la session) : l'envoi en flux, la vérification des sommes et le refus
d'une archive abîmée sont ceux de la production. Les archives déposées sont
supprimées à la fin de chaque test.
"""

import json
import sys
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import vm_transfer as vt  # noqa: E402

VM = "/api/vm/harv-fake/default/vm1"
ARCHIVE = "vm1-20260925-101500.hvx"
GIB = 1024 ** 3


def fulfill(route, body, status=200, ctype="application/json"):
    route.fulfill(status=status, content_type=ctype,
                  body=body if isinstance(body, str) else json.dumps(body))


def sse(events):
    return "".join(f"event: {t}\ndata: {json.dumps(d)}\n\n" for t, d in events)


# -- la fin d'un export -----------------------------------------------------

@pytest.fixture
def finished_export(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','fr');")
    page = context.new_page()
    page.route("**/api/clusters", lambda r, q: fulfill(r, {"clusters": [
        {"name": "harv-fake"}, {"name": "other"}]}))
    page.route(f"**{VM}/migrate-info", lambda r, q: fulfill(r, {"phase": "Running", "nodes": [], "migrations": []}))
    page.route(f"**{VM}/transfer/check", lambda r, q: fulfill(r, {
        "engine": "file", "reason": "file-requested", "blocked": False,
        "findings": [{"code": "engine", "level": "ok", "facts": {}}],
        "amount": {"disks": 1, "size": 20 * GIB, "used": int(3.6 * GIB)}}))
    page.route(f"**{VM}/transfer", lambda r, q: fulfill(r, {"action_id": "exp000000001"}, 201))
    page.route("**/api/stream/exp000000001", lambda r, q: fulfill(r, sse([
        ("progress", {"phase": "download", "item": "rootdisk", "done": 20 * GIB, "total": 20 * GIB,
                      "wire": 3 * GIB, "rate": 56 * 1024 ** 2, "eta": 0, "elapsed": 364,
                      "items_done": 1, "items_total": 1, "final": True}),
        ("end", {"status": "done", "result": {"archive": ARCHIVE}, "progress": {}}),
    ]), ctype="text/event-stream"))
    page.route("**/api/exports", lambda r, q: fulfill(r, {"free": 50 * GIB, "exports": [
        {"name": ARCHIVE, "size": 3175766528, "complete": True, "vm": "vm1",
         "namespace": "default", "cluster": "harv-fake", "version": "v1.9.0",
         "created": "2026-09-25T10:15:00Z", "disks": [], "networks": []}]}))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.VMMigrate && window.VMTransfer && window.XferProgress")
    page.evaluate("VMMigrate.open('harv-fake', 'default', 'vm1', 'file')")
    pane = page.locator('#fp-vm-migrate-harv-fake-default-vm1 [data-pane="file"]')
    expect(pane.locator('[data-x="start"]')).to_be_enabled(timeout=5000)
    pane.locator('[data-x="start"]').click()
    return page, pane


def test_the_end_of_an_export_names_its_archive(finished_export):
    page, pane = finished_export
    box = pane.locator('[data-x="archive"]')
    expect(box).to_be_visible(timeout=5000)
    expect(box).to_contain_text(ARCHIVE)
    expect(box).to_contain_text("3,0 Gio")
    link = box.locator('[data-x="archive-download"]')
    expect(link).to_have_attribute("href", f"/api/exports/{ARCHIVE}/download")
    expect(link).to_contain_text("Télécharger")
    # chaque geste s'explique
    for x in ("archive-download", "archive-import", "archive-store"):
        assert box.locator(f'[data-x="{x}"]').get_attribute("data-tip")


def test_the_archive_opens_its_import(finished_export):
    page, pane = finished_export
    box = pane.locator('[data-x="archive"]')
    expect(box).to_be_visible(timeout=5000)
    box.locator('[data-x="archive-import"]').click()
    panel_id = "#fp-vm-import-" + ARCHIVE.replace(".", "\\.")
    expect(page.locator(panel_id)).to_be_visible(timeout=5000)


def test_the_archive_opens_the_store(finished_export):
    page, pane = finished_export
    box = pane.locator('[data-x="archive"]')
    expect(box).to_be_visible(timeout=5000)
    box.locator('[data-x="archive-store"]').click()
    expect(page.locator("#fp-vm-exports")).to_contain_text(ARCHIVE, timeout=5000)


# -- le dépôt, sur le vrai serveur ------------------------------------------

def _archive(tmp_path, name="depot-test", damage=False):
    path = tmp_path / f"{name}.hvx"
    path.unlink(missing_ok=True)
    w = vt.ArchiveWriter(path)
    w.add_json(vt.MANIFEST, {"format": 1, "created": "2026-09-25T00:00:00Z",
                             "source": {"cluster": "site-a", "version": "v1.8.2",
                                        "namespace": "default", "name": name},
                             "vm": {"metadata": {"name": name}},
                             "inventory": {"disks": [{"volume": "disk-0", "size": 1,
                                                      "member": "disks/disk-0.raw.gz"}],
                                           "networks": []},
                             "secrets": []})
    w.add_stream("disks/disk-0.raw.gz", iter([b"d" * 2_000_000]))
    w.close()
    data = path.read_bytes()
    if damage:
        off, size = vt.ArchiveReader(path).member("disks/disk-0.raw.gz")
        i = off + size // 2
        data = data[:i] + bytes([data[i] ^ 0xFF]) + data[i + 1:]
    return data


@pytest.fixture
def store(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','fr');")
    page = context.new_page()
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.VMTransfer && window.XferProgress && window.i18n")
    page.evaluate("VMTransfer.openStore()")
    panel = page.locator("#fp-vm-exports")
    expect(panel).to_be_visible(timeout=5000)
    added = []
    yield page, panel, added
    for name in added:
        req = urllib.request.Request(f"{flask_server['base_url']}/api/exports/{name}", method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:                       # noqa: BLE001
            pass


def test_an_archive_is_uploaded_from_this_computer(store, tmp_path):
    page, panel, added = store
    added.append("depot-test.hvx")
    btn = panel.locator('[data-x="upload"]')
    expect(btn).to_contain_text("Déposer une archive")
    assert btn.get_attribute("data-tip")
    panel.locator('[data-x="upload-file"]').set_input_files(files=[{
        "name": "depot-test.hvx", "mimeType": "application/octet-stream",
        "buffer": _archive(tmp_path)}])
    line = panel.locator('[data-x="up-line"]')
    expect(line).to_contain_text("Archive déposée dans le magasin : depot-test.hvx", timeout=15000)
    # le magasin la liste aussitôt, complète, avec son origine
    row = panel.locator('tr[data-file="depot-test.hvx"]')
    expect(row).to_contain_text("site-a", timeout=5000)
    expect(row).to_contain_text("complète")
    expect(panel.locator('[data-x="up-cancel"]')).to_be_hidden()
    expect(panel.locator('[data-x="upload"]')).to_be_enabled()


def test_a_damaged_archive_is_refused_and_says_why(store, tmp_path):
    page, panel, added = store
    added.append("abimee.hvx")
    panel.locator('[data-x="upload-file"]').set_input_files(files=[{
        "name": "abimee.hvx", "mimeType": "application/octet-stream",
        "buffer": _archive(tmp_path, name="abimee", damage=True)}])
    line = panel.locator('[data-x="up-line"]')
    expect(line).to_contain_text("Dépôt refusé", timeout=15000)
    expect(line).to_contain_text("checksum mismatch")
    expect(panel.locator('tr[data-file="abimee.hvx"]')).to_have_count(0)


def test_a_file_that_is_not_an_archive_is_refused_before_sending(store):
    page, panel, _ = store
    sent = []
    page.on("request", lambda r: sent.append(r.method) if "/api/exports/" in r.url else None)
    panel.locator('[data-x="upload-file"]').set_input_files(files=[{
        "name": "photo.jpg", "mimeType": "image/jpeg", "buffer": b"\xff\xd8\xff"}])
    expect(panel.locator('[data-x="up-line"]')).to_contain_text(
        "« photo.jpg » n'est pas un nom d'archive .hvx", timeout=5000)
    assert "PUT" not in sent


def test_the_store_is_wired_once_even_if_opened_twice(store, tmp_path):
    """FloatingPanels rend la même fenêtre au second appel : la rebrancher
    envoyait deux fois le fichier choisi."""
    page, panel, added = store
    added.append("double.hvx")
    page.evaluate("VMTransfer.openStore()")
    puts = []
    page.on("request", lambda r: puts.append(r.url) if r.method == "PUT" else None)
    panel.locator('[data-x="upload-file"]').set_input_files(files=[{
        "name": "double.hvx", "mimeType": "application/octet-stream",
        "buffer": _archive(tmp_path, name="double")}])
    expect(panel.locator('[data-x="up-line"]')).to_contain_text("double.hvx", timeout=15000)
    expect(panel.locator('[data-x="up-line"]')).to_contain_text("Archive déposée", timeout=15000)
    assert len(puts) == 1
