"""v1.58.0 : la fenêtre Backups, dans un navigateur.

Le bouton « Backups » à droite du sélecteur d'espace de noms ouvre une
fenêtre à quatre onglets (VM Schedules, VM Backups, VM Snapshots, Volume
Snapshots), comme le menu « Backup and Snapshots » de Harvester. Les routes
sont simulées ; les gestes envoyés sont vérifiés.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto  # noqa: E402

LISTS = {
    "vmbackups": [{"namespace": "default", "name": "web-b1", "vm": "web", "ready": True, "progress": 100,
                   "error": "", "target": "nfs://nas/x", "size": 5 * 2**30, "volumes": 1, "schedule": "nightly",
                   "created": "2026-09-25T02:00:00Z"}],
    "vmsnapshots": [{"namespace": "default", "name": "web-s1", "vm": "web", "ready": True, "progress": None,
                     "error": "", "target": "", "size": None, "volumes": 1, "schedule": None,
                     "created": "2026-09-26T08:00:00Z"}],
    "schedules": [{"namespace": "default", "name": "nightly", "vm": "web", "type": "backup", "cron": "0 2 * * *",
                   "retain": 7, "max_failure": 3, "suspended": False, "failures": 0, "kept": 3,
                   "last": "2026-09-25T02:00:00Z", "created": None}],
    "volsnaps": [{"namespace": "default", "name": "web-s1-volume-root", "pvc": "web-root", "class": "longhorn-snapshot",
                  "ready": True, "size": "20Gi", "error": "", "owner": "web-s1", "created": None}],
}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def win(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','fr');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.removeItem('harvester_ops_backups_tab');")
    page = context.new_page()
    posted = []

    def lists(route, req):
        kind = req.url.split("/api/cluster-objects/harv-fake/")[1].split("?")[0]
        fulfill(route, {"items": LISTS.get(kind, [])})

    def writes(route, req):
        posted.append((req.method, req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "bk0000000001"}, 202)
    page.route("**/api/cluster-objects/**", lists)
    page.route("**/api/backup-target/harv-fake", lambda r, q: fulfill(
        r, {"type": "nfs", "endpoint": "172.16.0.5:/volume1/BACKUP", "bucket": "", "region": "", "set": True}))
    page.route("**/api/vms/harv-fake", lambda r, q: fulfill(r, {"vms": [
        {"namespace": "default", "name": "web"}, {"namespace": "lab", "name": "db"}]}))
    page.route("**/api/namespaces/harv-fake", lambda r, q: fulfill(r, [{"name": "default"}, {"name": "lab"}]))
    for pat in ("**/api/backups/**", "**/api/schedules/**", "**/api/volsnaps/**"):
        page.route(pat, writes)
    page.route("**/api/stream/bk0000000001", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address('namespaces'))
    page.wait_for_function("window.Backups && window.ResourceViews && window.App && App.getCurrentCluster()")
    btn = page.locator("#btn-vm-backups")
    assert btn.get_attribute("data-tip-i18n") == "vms.backupsTip"
    btn.click()
    w = page.locator(".floating-panel", has=page.locator(".bk-win"))
    expect(w).to_be_visible(timeout=8000)
    return page, w, posted


def test_the_window_has_the_four_harvester_tabs(win):
    page, w, _ = win
    tabs = w.locator("[data-bk-tab]")
    assert tabs.evaluate_all("els => els.map(e => e.dataset.bkTab)") == ["schedules", "vmbackups", "vmsnapshots", "volsnaps"]
    assert all(t.get_attribute("data-tip") for t in tabs.all())
    expect(w.locator('[data-bk="target"]')).to_contain_text("nfs 172.16.0.5:/volume1/BACKUP")
    expect(w.locator(".res-table")).to_contain_text("web-b1")          # sauvegardes d'abord
    expect(w.locator(".res-table")).to_contain_text("planification nightly")


def test_a_schedule_form_sends_a_readable_cron(win):
    page, w, posted = win
    w.locator('[data-bk-tab="schedules"]').click()
    expect(w.locator(".res-table")).to_contain_text("chaque jour à 02:00")
    w.locator('[data-bk="new"]').click()
    f = w.locator(".bk-form")
    expect(f.locator('[name="name"]')).to_have_value("web-backup")        # proposé d'après la VM
    f.locator('[name="freq"]').select_option("weekly")
    expect(f.locator('[name="weekday"]')).to_be_visible()
    f.locator('[name="weekday"]').select_option("0")
    f.locator('[name="time"]').fill("03:30")
    f.locator('[name="retain"]').fill("4")
    f.locator('button[type="submit"]').click()
    expect(w.locator('[data-bk="feedback"]')).to_contain_text("créée", timeout=5000)
    method, path, body = posted[0]
    assert method == "POST" and path == "api/schedules/harv-fake/default"
    assert body == {"name": "web-backup", "vm": "web", "type": "backup", "cron": "30 3 * * 0",
                    "retain": 4, "max_failure": 3}


def test_a_restore_goes_to_a_new_vm_or_over_the_original(win):
    page, w, posted = win
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    w.locator('[data-bk-tab="vmsnapshots"]').click()
    w.locator("tr", has_text="web-s1").locator('[data-act="restore"]').click()
    f = w.locator(".bk-form")
    expect(f.locator('[name="new_vm"]')).to_have_value("web-restore")
    f.locator('[name="halt"]').check()
    f.locator('button[type="submit"]').click()
    expect(w.locator('[data-bk="feedback"]')).to_contain_text("restaurée", timeout=5000)
    assert posted[-1][1] == "api/backups/harv-fake/default/web-s1/restore"
    assert posted[-1][2] == {"replace": False, "new_vm": "web-restore", "keep_mac": False, "halt": True}
    w.locator("tr", has_text="web-s1").locator('[data-act="restore"]').click()
    w.locator('.bk-form [name="mode"][value="replace"]').check()
    expect(w.locator(".bk-form [data-when='replace']")).to_be_visible()
    w.locator('.bk-form button[type="submit"]').click()
    expect(w.locator('[data-bk="feedback"]')).to_contain_text("restaurée", timeout=5000)
    assert dialogs and "web" in dialogs[-1]
    assert posted[-1][2]["replace"] is True


def test_a_volume_snapshot_of_a_vm_snapshot_cannot_be_deleted_alone(win):
    page, w, posted = win
    w.locator('[data-bk-tab="volsnaps"]').click()
    row = w.locator("tr", has_text="web-s1-volume-root")
    expect(row).to_be_visible(timeout=5000)
    expect(row.locator('[data-act="delete"]')).to_be_disabled()
    row.locator('[data-act="restore"]').click()
    expect(w.locator('.bk-form [name="new_volume"]')).to_have_value("web-root-restore")
    w.locator('.bk-form button[type="submit"]').click()
    expect(w.locator('[data-bk="feedback"]')).to_contain_text("restauré", timeout=5000)
    assert posted[-1][1] == "api/volsnaps/harv-fake/default/web-s1-volume-root/restore"


def test_suspend_and_delete_a_schedule(win):
    page, w, posted = win
    page.on("dialog", lambda d: d.accept())
    w.locator('[data-bk-tab="schedules"]').click()
    row = w.locator("tr", has_text="nightly")
    row.locator('[data-act="suspend"]').click()
    expect(w.locator('[data-bk="feedback"]')).to_contain_text("suspendue", timeout=5000)
    assert posted[-1][:2] == ("POST", "api/schedules/harv-fake/default/nightly/suspend")
    w.locator("tr", has_text="nightly").locator('[data-act="delete"]').click()
    expect(w.locator('[data-bk="feedback"]')).to_contain_text("supprimé", timeout=5000)
    assert posted[-1][:2] == ("DELETE", "api/schedules/harv-fake/default/nightly")


def test_the_window_comes_back_after_a_reload(win):
    page, w, _ = win
    w.locator('[data-bk-tab="volsnaps"]').click()
    page.reload()
    page.wait_for_function("window.Backups && window.FloatingPanels")
    w2 = page.locator(".floating-panel", has=page.locator(".bk-win"))
    expect(w2).to_be_visible(timeout=8000)


# -- v1.68.0 : gel, anciens volumes, modification d'une planification ------------------

def test_a_backup_can_carry_a_freeze_deadline(win):
    page, w, posted = win
    w.locator('[data-bk-tab="vmbackups"]').click()
    w.locator('[data-bk="new"]').click()
    f = w.locator(".bk-form")
    # le formulaire remplit ses choix après coup : les lire trop tôt donnait [] (vu : 1 fois sur 3)
    expect(f.locator('[name="freeze"] option')).to_have_count(7, timeout=5000)
    assert f.locator('[name="freeze"] option').evaluate_all("els => els.map(e => e.value)") \
        == ["", "5s", "10s", "30s", "1m", "3m", "5m"]                     # pas de gel sans fin
    f.locator('[name="freeze"]').select_option("10s")
    f.locator('button[type="submit"]').click()
    expect(w.locator('[data-bk="feedback"]')).not_to_be_empty(timeout=5000)
    assert posted[-1][2] == {"vm": "web", "type": "backup", "freeze": "10s"}


def test_replacing_from_a_backup_says_what_becomes_of_the_old_volumes(win):
    page, w, posted = win
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    w.locator('[data-bk-tab="vmbackups"]').click()
    w.locator("tr", has_text="web-b1").locator('[data-act="restore"]').click()
    f = w.locator(".bk-form")
    expect(f.locator('[name="delete_policy"]')).to_be_hidden()           # nouvelle VM : sans objet
    f.locator('[name="mode"][value="replace"]').check()
    f.locator('[name="delete_policy"]').select_option("delete")
    f.locator('button[type="submit"]').click()
    expect(w.locator('[data-bk="feedback"]')).not_to_be_empty(timeout=5000)
    assert posted[-1][2] == {"replace": True, "keep_mac": False, "halt": False, "delete_policy": "delete"}
    assert "web" in dialogs[-1] and dialogs[-1] != ""                    # la suppression est dite


def test_a_snapshot_restore_offers_no_volume_deletion(win):
    page, w, _ = win
    w.locator('[data-bk-tab="vmsnapshots"]').click()
    w.locator("tr", has_text="web-s1").locator('[data-act="restore"]').click()
    expect(w.locator('.bk-form [name="delete_policy"]')).to_have_count(0)


def test_a_schedule_is_edited_in_place(win):
    page, w, posted = win
    w.locator('[data-bk-tab="schedules"]').click()
    w.locator("tr", has_text="nightly").locator('[data-act="sched-edit"]').click()
    f = w.locator(".bk-form")
    expect(f.locator('[name="freq"]')).to_have_value("daily")
    expect(f.locator('[name="time"]')).to_have_value("02:00")
    expect(f.locator('[name="retain"]')).to_have_value("7")
    f.locator('[name="time"]').fill("04:15")
    f.locator('[name="retain"]').fill("10")
    f.locator('[name="max_failure"]').fill("4")
    f.locator('button[type="submit"]').click()
    expect(w.locator('[data-bk="feedback"]')).not_to_be_empty(timeout=5000)
    assert posted[-1] == ("POST", "api/schedules/harv-fake/default/nightly/update",
                          {"cron": "15 4 * * *", "retain": 10, "max_failure": 4})
