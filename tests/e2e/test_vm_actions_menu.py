"""v1.60.0 : le menu d'actions d'une VM, dans un navigateur.

Le bouton « ⋮ » d'une VM ouvre un menu qui lit l'état réel de la VM : un
geste impossible est grisé et dit pourquoi. Les gestes à choix ouvrent une
fenêtre (clone, suppression avec les volumes cochés). Les routes sont
simulées ; les requêtes envoyées sont vérifiées.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto  # noqa: E402

VMS = {"vms": [{"namespace": "default", "name": "web", "phase": "Running", "runStrategy": "RerunOnFailure",
                "agent_connected": "False", "priority": 0}]}
STATE = {"running": True, "paused": False, "agent": False, "migrating": False, "node": "n1", "targets": [],
         "run_strategy": "RerunOnFailure", "cloudinit_secrets": ["web-ci"],
         "volumes": [{"volume": "root", "claim": "web-root", "kind": "disk", "source": "pvc", "hotpluggable": False},
                     {"volume": "iso", "claim": "web-iso", "kind": "cdrom", "source": "pvc", "hotpluggable": False},
                     {"volume": "cloudinitdisk", "claim": None, "kind": "disk", "source": "cloudinit",
                      "hotpluggable": False}]}


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
        sent.append((req.method, req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "vma000000001"}, 202)
    page.route("**/api/vms/harv-fake", lambda r, q: fulfill(r, VMS))
    page.route("**/api/namespaces/harv-fake", lambda r, q: fulfill(r, [{"name": "default"}]))
    page.route("**/api/vm/harv-fake/default/web/state", lambda r, q: fulfill(r, STATE))
    page.route("**/api/vm/harv-fake/default/web/do/**", writes)
    page.route("**/api/vm/harv-fake/default/web", lambda r, q: writes(r, q) if q.method == "DELETE" else r.fallback())
    page.route("**/api/stream/vma000000001", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address('namespaces'))
    page.wait_for_function("window.VMActions && window.App && App.getCurrentCluster()")
    page.evaluate("App.setTab('namespaces')")
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    more = page.locator("#ns-vms-table [data-vm-more]").first
    expect(more).to_be_visible(timeout=8000)
    return page, more, sent, dialogs


def test_the_menu_follows_the_vm_state(ui):
    page, more, sent, _ = ui
    assert more.get_attribute("data-tip")
    more.click()
    menu = page.locator(".vma-menu")
    expect(menu.locator(".vma-item").first).to_be_visible(timeout=5000)
    groups = menu.locator(".vma-title").all_inner_texts()
    assert [g.lower() for g in groups] == ["alimentation", "protection", "disques", "réseau", "migration", "copie",
                                          "observer", "yaml"]
    # pas d'agent : redémarrage doux grisé, avec la raison ; pas d'autre nœud : migration grisée
    soft = menu.locator('[data-vma="softreboot"]')
    assert soft.get_attribute("aria-disabled") == "true" and "agent" in soft.get_attribute("data-tip")
    assert menu.locator('[data-vma="migrate"]').get_attribute("aria-disabled") == "true"
    expect(menu.locator('[data-vma="eject"]')).to_contain_text("iso")
    assert all(i.get_attribute("data-tip") for i in menu.locator(".vma-item").all())
    soft.click(force=True)                               # grisé : rien ne part, le menu reste
    expect(menu).to_be_visible()
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    assert sent == []


def test_pause_is_sent_after_a_confirmation(ui):
    page, more, sent, dialogs = ui
    more.click()
    page.locator('.vma-menu [data-vma="pause"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][:2] == ("POST", "api/vm/harv-fake/default/web/do/pause") and "web" in dialogs[-1]


def test_a_clone_window_sends_the_new_name(ui):
    page, more, sent, _ = ui
    more.click()
    page.locator('.vma-menu [data-vma="clone"]').click()
    w = page.locator(".floating-panel", has=page.locator(".vma-form")).last
    expect(w.locator('[name="new_name"]')).to_have_value("web-clone")
    w.locator('[name="new_name"]').fill("web2")
    w.locator('button[type="submit"]').click()
    expect(w.locator(".of-msg")).to_contain_text("web", timeout=5000)
    assert sent[-1] == ("POST", "api/vm/harv-fake/default/web/do/clone",
                        {"new_name": "web2", "with_data": True, "start": False})


def test_delete_checks_the_system_disk_like_harvester(ui):
    page, more, sent, dialogs = ui
    more.click()
    page.locator('.vma-menu [data-vma="delete"]').click()
    w = page.locator(".floating-panel", has=page.locator(".vma-vols")).last
    boxes = w.locator('.vma-vols input[type="checkbox"]')
    expect(boxes).to_have_count(2)
    assert boxes.nth(0).is_checked() and not boxes.nth(1).is_checked()
    boxes.nth(1).check()
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(600)
    assert sent[-1] == ("DELETE", "api/vm/harv-fake/default/web",
                        {"remove_volumes": ["web-root", "web-iso"], "keep_cloudinit": False})
    assert "2" in dialogs[-1]


def test_bulk_restart_goes_through_the_same_action(ui):
    page, more, sent, _ = ui
    page.locator("#ns-vms-table tbody input[type=checkbox]").first.check()
    btn = page.locator('[data-bulk-do="restart"]')
    expect(btn).to_be_visible()
    assert btn.get_attribute("data-tip")
    btn.click()
    page.wait_for_timeout(600)
    assert sent[-1][:2] == ("POST", "api/vm/harv-fake/default/web/do/restart")
