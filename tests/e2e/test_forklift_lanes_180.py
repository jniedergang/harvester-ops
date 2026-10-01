"""v1.80.0 : les vagues VMware en couloirs sur un axe du temps commun.

Onglet Vagues d'un cluster : bascule Blocs / Couloirs retenue par le
navigateur, un couloir par vague, ligne de maintenant, copies passées
(complète puis incrémentales) avec leur bulle, prochaine copie, bascule
prévue et son compte à rebours, fenêtre de bascule vécue, zoom, fenêtre de
maintenance tenue dans le navigateur, clic qui ouvre la fenêtre de suivi.
Vue « Migrations (tous clusters) » : les mêmes couloirs, un par cluster et
vague. Toutes les routes sont simulées ; les dates sont relatives à
maintenant pour que l'axe ajusté les montre toutes."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

READY = {"ready": True, "cert_manager": True, "cert_manager_missing": [], "addon": "ready",
         "addon_message": "the forklift-operator add-on is deployed", "operator": True, "controller": True,
         "components_missing": [], "running": True, "inventory_access": True}


def iso(**delta):
    t = datetime.now(timezone.utc) + timedelta(**delta)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def copy(start_min, minutes=None):
    """Une copie commencée `start_min` minutes avant maintenant (négatif = passé)."""
    end = iso(minutes=start_min + minutes) if minutes is not None else ""
    return {"start": iso(minutes=start_min), "end": end, "seconds": minutes * 60 if minutes is not None else None}


def vm(vm_id, name, copies, **kw):
    row = {"id": vm_id, "name": name, "phase": "Running", "step": "copying disks", "step_name": "DiskTransfer",
           "progress": {"done": 0, "total": 0}, "precopies": len(copies), "last_precopy": None,
           "next_precopy": None, "error": "", "rolled_back": False, "cutover_started": False,
           "copies": copies, "started": copies[0]["start"] if copies else None, "completed": None,
           "cutover_window": None}
    row.update(kw)
    return row


def wave(name, state, vms, **kw):
    w = {"name": name, "target_namespace": "mig-b2", "provider": {"namespace": "forklift", "name": "vmwlab"},
         "state": state, "message": "", "migration": f"{name}-m1", "vms": vms, "cutover": None,
         "cutover_started": False, "next_precopy": None, "created": vms[0]["copies"][0]["start"],
         "started": vms[0]["copies"][0]["start"], "completed": None}
    w.update(kw)
    return w


def waves():
    w_copy = wave("w-copy", "copying",
                  [vm("vm-16", "vmwlab-src-1", [copy(-180, 20), copy(-120, 2), copy(-60, 1)],
                      next_precopy=iso(minutes=30))],
                  next_precopy=iso(minutes=30))
    w_sched = wave("w-sched", "cutover-scheduled",
                   [vm("vm-20", "vmwlab-src-2", [copy(-300, 30), copy(-200, 3)], cutover_started=True)],
                   cutover=iso(minutes=60), cutover_started=True)
    w_done = wave("w-done", "succeeded",
                  [vm("vm-21", "vmwlab-src-3", [copy(-240, 25), copy(-160, 2), copy(-151)],
                      completed=iso(minutes=-120), cutover_started=True,
                      cutover_window={"start": iso(minutes=-150), "end": iso(minutes=-120)})],
                  cutover=iso(minutes=-151), cutover_started=True, completed=iso(minutes=-120))
    return [w_copy, w_sched, w_done]


VCENTER = "https://vmwlab-vc.home.lo/sdk"


def tab_data():
    return {"cluster": "harv-fake", "install": READY, "harvester_addon": False, "bundle": True,
            "vddk": {"image": "", "digest": "", "archive": "", "pushed_at": ""},
            "registry": {"image": "", "host": "", "plain_http": True, "auth": False},
            "providers": [{"name": "vmwlab", "namespace": "forklift", "url": VCENTER, "ready": True,
                           "message": "", "vddk_image": "", "plans": [], "managed": True}],
            "vmimport_sources": [], "cdi_importer": {"image": "", "kind": "upstream", "original": ""},
            "precopy_interval": 60, "waves": waves(), "networks": [], "storage_classes": [], "namespaces": []}


def global_data():
    return {"clusters": [
        {"cluster": "harv-fake", "reachable": True, "forklift_ready": True, "cdi_importer_kind": "upstream",
         "providers": [{"name": "vmwlab", "namespace": "forklift", "url": VCENTER, "ready": True, "message": ""}],
         "waves": waves()}]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def open_page(context, flask_server, tab="forklift", view=None, global_view=None):
    init = ("localStorage.setItem('harvester_ops_language','en');"
            "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
            "localStorage.setItem('harvester_ops_section_forklift','waves');"
            f"localStorage.setItem('harvester_ops_current_tab','{tab}');")
    if view:
        init += f"localStorage.setItem('harvester_ops_fk_waves_view','{view}');"
    if global_view:
        init += f"localStorage.setItem('harvester_ops_fkg_view','{global_view}');"
    # une seule fois : un rechargement garde ce que la page a enregistré
    context.add_init_script(f"if (!sessionStorage.getItem('fkl-init')) {{ {init} sessionStorage.setItem('fkl-init','1'); }}")
    page = context.new_page()
    page.route(re.compile(r".*/api/forklift/harv-fake(\?.*)?$"), lambda r, q: fulfill(r, tab_data()))
    page.route("**/api/forklift-global", lambda r, q: fulfill(r, global_data()))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.Forklift && window.ForkliftLanes && window.Sections && window.App && App.getCurrentCluster()")
    return page


def lanes(page, scope="#tab-forklift"):
    return page.locator(f"{scope} [data-fkl-lane]")


def lane(page, name, scope="#tab-forklift"):
    return page.locator(f'{scope} [data-fkl-lane][data-wave="{name}"]')


def stored(page, key):
    return page.evaluate(f"localStorage.getItem({json.dumps(key)})")


def test_blocks_by_default_then_lanes_remembered_across_a_reload(context, flask_server):
    page = open_page(context, flask_server)
    expect(page.locator('#tab-forklift [data-fk-wave]')).to_have_count(3)
    toggle = page.locator('#tab-forklift [data-fk="waves-mode"]')
    expect(toggle).to_have_count(2)
    for b in toggle.all():
        assert b.get_attribute("data-tip")
    expect(page.locator('#tab-forklift [data-fk="waves-mode"][data-mode="blocks"]')).to_have_class(re.compile(r"\bactive\b"))
    page.locator('#tab-forklift [data-fk="waves-mode"][data-mode="lanes"]').click()
    expect(lanes(page)).to_have_count(3)
    expect(page.locator('#tab-forklift [data-fk-wave]')).to_have_count(0)
    assert stored(page, "harvester_ops_fk_waves_view") == "lanes"
    page.reload(wait_until="domcontentloaded")
    expect(lanes(page)).to_have_count(3)
    page.locator('#tab-forklift [data-fk="waves-mode"][data-mode="blocks"]').click()
    expect(page.locator('#tab-forklift [data-fk-wave]')).to_have_count(3)
    assert stored(page, "harvester_ops_fk_waves_view") == "blocks"


def test_one_lane_per_wave_with_its_state_and_a_now_line(context, flask_server):
    page = open_page(context, flask_server, view="lanes")
    expect(lanes(page)).to_have_count(3)
    assert [lanes(page).nth(i).get_attribute("data-wave") for i in range(3)] == ["w-copy", "w-sched", "w-done"]
    expect(lane(page, "w-done").locator("[data-fk-state]")).to_have_attribute("data-fk-state", "succeeded")
    expect(lane(page, "w-done")).to_have_class(re.compile(r"fkl-st-ok"))
    # maintenant : une ligne par couloir et son libellé sur l'axe
    expect(page.locator('#tab-forklift [data-fkl-lane] [data-fkl="now"]')).to_have_count(3)
    expect(page.locator('#tab-forklift [data-fkl="now-label"]')).to_contain_text("now")
    assert "Now:" in page.locator('#tab-forklift [data-fkl-lane] [data-fkl="now"]').first.get_attribute("data-tip")
    # l'axe ajusté va jusqu'à maintenant + 2 h : la ligne est aux deux tiers environ
    left = float(re.search(r"left:([\d.]+)%", page.locator('#tab-forklift [data-fkl="now-label"]').get_attribute("style")).group(1))
    assert 60 < left < 80, left
    expect(page.locator('#tab-forklift [data-fkl="axis"] .fkl-tick').first).to_be_attached()


def test_scheduled_cutover_mark_and_countdown(context, flask_server):
    page = open_page(context, flask_server, view="lanes")
    cut = lane(page, "w-sched").locator('[data-fkl="cutover"]')
    expect(cut).to_have_class(re.compile(r"fkl-cut-planned"))
    assert cut.get_attribute("data-tip").startswith("Scheduled switchover:")
    countdown = lane(page, "w-sched").locator('[data-fkl="countdown"] [data-fk-at]')
    expect(countdown).to_contain_text(re.compile(r"in (59 min|1 h)"))
    # une bascule passée : une marque, pas de compte à rebours, et sa fenêtre
    done = lane(page, "w-done")
    expect(done.locator('[data-fkl="countdown"]')).to_have_count(0)
    assert done.locator('[data-fkl="cutover"]').get_attribute("data-tip").startswith("Switchover requested:")
    win = done.locator('[data-fkl="window"]')
    expect(win).to_have_count(1)
    assert "Switchover window:" in win.get_attribute("data-tip") and "(30 min 00 s)" in win.get_attribute("data-tip")
    # la prochaine copie d'une vague qui copie
    nxt = lane(page, "w-copy").locator('[data-fkl="next"]')
    expect(nxt).to_have_count(1)
    assert nxt.get_attribute("data-tip").startswith("Next copy:")


def test_past_copies_are_marks_with_their_details_on_hover(context, flask_server):
    page = open_page(context, flask_server, view="lanes")
    copies = lane(page, "w-copy").locator('[data-fkl="copy"]')
    expect(copies).to_have_count(3)
    assert [copies.nth(i).get_attribute("data-fkl-copy") for i in range(3)] == ["full", "incr", "incr"]
    assert copies.nth(0).get_attribute("data-tip").startswith("Initial copy, vmwlab-src-1:")
    assert "(20 min 00 s)" in copies.nth(0).get_attribute("data-tip")
    assert copies.nth(2).get_attribute("data-tip").startswith("Incremental copy 2, vmwlab-src-1:")
    # la copie finale sans fin relevée d'une vague finie reste une marque
    assert "end not recorded" in lane(page, "w-done").locator('[data-fkl="copy"]').nth(2).get_attribute("data-tip")
    copies.nth(0).hover()
    bubble = page.locator(".tip-layer")
    expect(bubble).to_be_visible()
    expect(bubble).to_contain_text("Initial copy, vmwlab-src-1")


def test_zoom_changes_the_span_and_is_remembered(context, flask_server):
    page = open_page(context, flask_server, view="lanes")
    tools = page.locator('#tab-forklift [data-fkl="tools"]')
    for c in tools.locator("button, input").all():
        assert c.get_attribute("data-tip"), c.evaluate("e => e.outerHTML")
    expect(tools.locator('[data-fkl-zoom="fit"]')).to_have_class(re.compile(r"is-on"))
    # ajusté : la plus ancienne copie (il y a 5 h) est visible
    expect(lane(page, "w-sched").locator('[data-fkl="copy"]')).to_have_count(2)
    tools.locator('[data-fkl-zoom="6h"]').click()
    expect(tools.locator('[data-fkl-zoom="6h"]')).to_have_class(re.compile(r"is-on"))
    # 6 h : de maintenant - 4 h à maintenant + 2 h, la copie d'il y a 5 h sort de l'axe
    expect(lane(page, "w-sched").locator('[data-fkl="copy"]')).to_have_count(1)
    assert stored(page, "harvester_ops_fk_lanes_zoom:harv-fake") == "6h"
    tools.locator('[data-fkl-zoom="7d"]').click()
    expect(lane(page, "w-sched").locator('[data-fkl="copy"]')).to_have_count(2)
    tip = page.locator('#tab-forklift [data-fkl="axis"]').get_attribute("data-tip")
    assert tip.startswith("Time axis from")
    tools.locator('[data-fkl-zoom="fit"]').click()
    expect(tools.locator('[data-fkl-zoom="fit"]')).to_have_class(re.compile(r"is-on"))


def test_maintenance_window_is_drawn_kept_in_the_browser_and_cleared(context, flask_server):
    page = open_page(context, flask_server, view="lanes")
    expect(lanes(page)).to_have_count(3)
    expect(page.locator('#tab-forklift [data-fkl="maint"]')).to_have_count(0)
    local = page.evaluate("""() => {
        const f = (ms) => { const d = new Date(Date.now() + ms);
          return new Date(d.getTime() - d.getTimezoneOffset() * 60e3).toISOString().slice(0, 16); };
        return [f(-3600e3), f(3600e3)]; }""")
    page.locator('#tab-forklift [data-fkl-maint="start"]').fill(local[0])
    page.locator('#tab-forklift [data-fkl-maint="end"]').fill(local[1])
    # l'axe et chaque couloir portent la bande
    expect(page.locator('#tab-forklift [data-fkl="maint"]')).to_have_count(4)
    assert "Maintenance window:" in page.locator('#tab-forklift [data-fkl="maint"]').first.get_attribute("data-tip")
    assert json.loads(stored(page, "harvester_ops_fk_maint:harv-fake")) == {"start": local[0], "end": local[1]}
    # la relecture de fond ne l'efface pas
    page.evaluate("Forklift.backgroundRefresh()")
    expect(page.locator('#tab-forklift [data-fkl="maint"]')).to_have_count(4)
    expect(page.locator('#tab-forklift [data-fkl-maint="start"]')).to_have_value(local[0])
    page.locator('#tab-forklift [data-fkl="maint-clear"]').click()
    expect(page.locator('#tab-forklift [data-fkl="maint"]')).to_have_count(0)
    assert stored(page, "harvester_ops_fk_maint:harv-fake") is None


def test_clicking_a_lane_opens_the_follow_window(context, flask_server):
    page = open_page(context, flask_server, view="lanes")
    lane(page, "w-copy").locator(".fkl-label").click()
    win = page.locator("#fp-fk-wave-follow-harv-fake-w-copy")
    expect(win).to_be_visible()
    expect(win.locator('[data-fk-vm="vm-16"]')).to_be_visible()
    # au clavier aussi
    win.locator('[data-action="close"]').click()
    lane(page, "w-sched").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#fp-fk-wave-follow-harv-fake-w-sched")).to_be_visible()


def test_the_global_view_offers_the_same_lanes_per_cluster_and_wave(context, flask_server):
    page = open_page(context, flask_server, tab="forkliftglobal")
    scope = "#tab-forkliftglobal"
    expect(page.locator(f"{scope} table.data-table")).to_be_visible(timeout=10000)
    page.locator(f'{scope} [data-fkg="view"][data-mode="lanes"]').click()
    expect(lanes(page, scope)).to_have_count(3)
    assert stored(page, "harvester_ops_fkg_view") == "lanes"
    expect(lane(page, "w-copy", scope).locator(".fkl-name")).to_have_text("harv-fake / w-copy")
    expect(page.locator(f"{scope} .res-count")).to_have_text("3 wave(s)")
    expect(lane(page, "w-sched", scope).locator('[data-fkl="countdown"]')).to_be_visible()
    for b in page.locator(f'{scope} [data-fkg="view"], {scope} [data-fkl="tools"] button').all():
        assert b.get_attribute("data-tip")
    lane(page, "w-done", scope).locator(".fkl-label").click()
    expect(page.locator("#fp-fk-wave-follow-harv-fake-w-done")).to_be_visible()
    page.locator('#fp-fk-wave-follow-harv-fake-w-done [data-action="close"]').click()
    # retour au tableau
    page.locator(f'{scope} [data-fkg="view"][data-mode="table"]').click()
    expect(page.locator(f"{scope} table.data-table tbody tr")).to_have_count(3)
