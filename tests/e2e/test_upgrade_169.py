"""v1.69.0 : la fenêtre « Mettre à jour Harvester » dans un navigateur :
ouverte depuis l'aperçu et depuis server-version, versions inéligibles
grisées, case « j'ai lu » exigée et décochée à chaque changement, chemin ISO
avec SHA-512 prérempli, progression d'une mise à jour, abandon et Dismiss
selon ce que Harvester permet."""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402
from nav import address, goto  # noqa: E402

CHECKS = [{"ok": True, "key": k, "text": ""} for k in ("running", "cleanup", "nodes-ready", "nodes-schedulable", "backups",
                                                       "schedules", "addons", "charts")]
IDLE = {"cluster": "harv-fake", "current": "v1.8.2", "checker": True, "running": None, "history": 0, "upgrade": None,
        "versions": [{"name": "v1.9.0", "iso_url": "https://x/h.iso", "checksum": True, "release_date": "20260916",
                      "min_upgradable": "v1.8.0", "tags": [], "eligible": True, "reason": "", "notes": ""},
                     {"name": "v1.7.1", "iso_url": "https://y", "checksum": True, "release_date": "", "min_upgradable": "",
                      "tags": [], "eligible": False, "reason": "downgrading is prohibited", "notes": ""}],
        "isos": [{"name": "harvester-v1.9.0-amd64.iso", "size": 8 * 2 ** 30, "eligible": True, "reason": "", "sha512": "a" * 128,
                  "release": {"harvester": "v1.9.0", "os": "Harvester v1.9.0", "kubernetes": "", "rancher": "", "min_upgradable": "v1.8.0"}}],
        "os_images": [], "prechecks": CHECKS}


def cond(t, s, time="2026-09-27T10:00:00Z"):
    return {"type": t, "status": s, "reason": "", "message": "", "time": time}


RUNNING = dict(IDLE, running="hvst-upgrade-abcde", upgrade={
    "name": "hvst-upgrade-abcde", "created": "2026-09-27T10:00:00Z", "version": "v1.9.0", "image": "", "target": "v1.9.0",
    "previous": "v1.8.2", "log": True, "state": "UpgradingNodes", "cleanup": "", "dismissed": False, "completed": None,
    "failure": "", "single_node": "n1", "image_progress": 100, "log_name": "hvst-upgrade-abcde-upgradelog",
    "repo": {"harvester": "v1.9.0", "os": "Harvester v1.9.0", "kubernetes": "v1.36.3+rke2r1", "rancher": "v2.15.0", "monitoringChart": ""},
    "notes": "https://github.com/harvester/harvester/releases/tag/v1.9.0", "can_abort": False, "can_dismiss": False,
    "conditions": [cond("LogReady", "True"), cond("ImageReady", "True"), cond("RepoReady", "True"), cond("NodesPrepared", "True"),
                   cond("SystemServicesUpgraded", "True"), cond("NodesUpgraded", "Unknown"), cond("Completed", "Unknown")],
    "nodes": [{"name": "n1", "state": "Node-upgrade paused", "reason": "", "message": "", "paused": True}]})


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    sent, state = [], {"data": IDLE}

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "upg000000169"}, 202)
    page.route("**/api/upgrade/harv-fake", lambda r, q: fulfill(r, state["data"]))
    page.route("**/api/upgrade/harv-fake/do/**", writes)
    page.route("**/api/stream/upg000000169", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address("overview"))
    page.wait_for_function("window.Upgrade && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    return page, sent, state


def win(page):
    return page.locator(".floating-panel", has=page.locator(".upg-win")).last


def test_the_upgrade_button_opens_the_form_with_its_rules(ui):
    page, sent, _ = ui
    # 2.0.0 : le bouton de l'aperçu mène à Paramètres > Mise à jour de l'hôte,
    # le même contenu dans la page
    page.locator("#btn-upgrade").click()
    page.wait_for_function("() => location.hash === '#harvester/harv-fake/params/upgrade'")
    w = page.locator("#tab-hupgrade .upg-win")
    f = w.locator('[data-upg="form"]')
    expect(f).to_be_visible(timeout=8000)
    expect(w.locator('[data-upg="head"]')).to_contain_text("v1.8.2")
    expect(f.locator('[name="version"] option[value="v1.7.1"]')).to_have_attribute("disabled", "")     # rétrogradation
    start = f.locator('[data-upg="start"]')
    f.locator('[name="version"]').select_option("v1.9.0")
    expect(f.locator('[data-upg="notes"] a')).to_have_attribute("href", "https://github.com/harvester/harvester/releases/tag/v1.9.0")
    expect(start).to_be_disabled()                                       # la case d'abord
    f.locator('[name="read"]').check()
    expect(start).to_be_enabled()
    f.locator('[name="skip"]').check()
    start.click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/upgrade/harv-fake/do/start", {"log": True, "skip_single_replica": True, "version": "v1.9.0"})


def test_the_iso_path_prefills_its_checksum_and_the_box_resets(ui):
    page, sent, _ = ui
    page.evaluate("Upgrade.open('harv-fake')")
    f = win(page).locator('[data-upg="form"]')
    f.locator('[name="source"][value="iso"]').check()
    f.locator('[name="read"]').check()
    f.locator('[name="iso"]').select_option("harvester-v1.9.0-amd64.iso")
    expect(f.locator('[name="read"]')).not_to_be_checked()               # décochée au changement de cible
    expect(f.locator('[name="checksum"]')).to_have_value("a" * 128)
    f.locator('[name="read"]').check()
    f.locator('[data-upg="start"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][1] == {"log": True, "skip_single_replica": False, "iso": "harvester-v1.9.0-amd64.iso", "checksum": "a" * 128}


def test_a_version_is_added_from_its_published_file(ui):
    page, sent, _ = ui
    page.evaluate("Upgrade.open('harv-fake')")
    f = win(page).locator('[data-upg="form"]')
    f.locator("details.upg-versions summary").click()
    f.locator('[name="vurl"]').fill("https://releases.rancher.com/harvester/v1.9.1/version.yaml")
    f.locator('[data-upg="vadd"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/upgrade/harv-fake/do/version-add", {"url": "https://releases.rancher.com/harvester/v1.9.1/version.yaml"})


def test_a_running_upgrade_shows_its_steps_and_what_harvester_allows(ui):
    page, sent, state = ui
    state["data"] = RUNNING
    page.evaluate("Upgrade.open('harv-fake')")
    w = win(page)
    prog = w.locator('[data-upg="progress"]')
    expect(prog.locator(".upg-step")).to_have_count(7, timeout=8000)
    expect(prog.locator(".upg-step.upg-ok")).to_have_count(5)
    expect(w.locator('[data-upg="form"]')).to_have_count(0)               # pas de nouvelle mise à jour pendant celle-ci
    expect(prog.locator('[data-upg="abort"]')).to_have_count(0)           # phase des hôtes : Harvester refuse
    expect(prog.locator('[data-upg="dismiss"]')).to_have_count(0)
    prog.locator('[data-upg="resume"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/upgrade/harv-fake/do/resume-node", {"name": "hvst-upgrade-abcde", "node": "n1"})


def test_a_finished_upgrade_can_be_dismissed(ui):
    page, sent, state = ui
    done = json.loads(json.dumps(RUNNING))
    done.update(running=None)
    done["upgrade"].update(completed=True, can_dismiss=True, state="Succeeded")
    state["data"] = done
    page.evaluate("Upgrade.open('harv-fake')")
    w = win(page)
    expect(w.locator('[data-upg="progress"] .sto-finding')).to_contain_text("upgraded to v1.9.0", timeout=8000)
    expect(w.locator('[data-upg="form"]')).to_be_visible()                # la suivante reste possible
    w.locator('[data-upg="dismiss"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/upgrade/harv-fake/do/dismiss", {"name": "hvst-upgrade-abcde"})
