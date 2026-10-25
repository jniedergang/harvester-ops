"""Disaster Recovery dans la navigation 2.0 (tâche 16), API simulée.

Les écrans de la réplication existent depuis la 1.101.0 (replication*.js) ;
ces cas vérifient leur place : racine #dr (cartes et tableau, filtres), un
contrat par son adresse #dr/<id> (à froid, au clic, à la fermeture),
#dr/links, le DR d'un hôte Harvester vu comme source ET comme secours (même
contrats, rôles inversés, filtre), l'état vide d'un hôte KVM, les pastilles
d'alertes (racine, entrée DR de l'hôte ouvert, ligne de chaque hôte), le
lien du journal d'un contrat vers son action d'Activité
(#activity/action/<id>), la pastille DR des VMs relue sans recharger la page,
et la liste des réseaux de la cible quand elle n'en a aucun.
"""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402
from test_replication_ui import ALERTS, PID, PID2, plan_detail, ui_factory  # noqa: E402,F401
from test_folders_200 import world  # noqa: E402,F401

ACT_ID = "rpjournal0001"


def ok(body, status=200):
    return dict(status=status, content_type="application/json", body=json.dumps(body))


def base(page):
    return page.url.split("#")[0].rsplit("/", 1)[0]


def hosts_body(*names):
    return {"families": {}, "hosts": [
        {"kind": "harvester", "name": n, "state": "ok", "error": None, "vm_count": 1, "labels": {}, "version": None,
         "favorite": False, "folder": None, "folder_source": None, "origin": "config", "key": f"host/harvester/{n}"}
        for n in names]}


@pytest.fixture
def ui(ui_factory):  # noqa: F811
    return ui_factory("admin")


# --- racine et contrat -------------------------------------------------------------

def test_dr_root_cold_keeps_cards_table_and_filters(ui):
    page, sent, _ = ui
    goto(page, base(page), "#dr")
    pane = page.locator("#tab-drplans")
    expect(pane).to_have_class(re.compile(r"\bactive\b"))
    expect(pane.locator(".rp-plan-card")).to_have_count(2)
    expect(page.locator('#host-nav a[data-hnav-root="dr"]:not([data-hnav-tab])')).to_have_attribute("aria-current", "page")
    # les liens d'un contrat sont de vraies adresses
    expect(pane.locator(f'.rp-plan-card[data-rp-plan="{PID}"] h3 a')).to_have_attribute("href", f"#dr/{PID}")
    pane.locator('[data-rp-view="table"]').click()
    expect(pane.locator(".rp-table tbody tr")).to_have_count(2)
    pane.locator('[data-rp="state"]').select_option("draft")
    expect(pane.locator(".rp-table tbody tr")).to_have_count(1)
    # la racine lit tous les contrats, sans filtre d'hôte, et garde les notifications externes
    assert any(s[0] == "GET" and s[1] == "plans" and s[2] == "" for s in sent)
    expect(pane.locator('[data-rp="notify"]')).to_be_visible()
    assert page.evaluate("location.hash") == "#dr"


def test_contract_address_cold_open_close_and_reload(ui):
    page, _, _ = ui
    goto(page, base(page), f"#dr/{PID}")
    view = page.locator(f"#fp-rp-plan-{PID}")
    expect(view.locator(".floating-panel-title")).to_have_text("Contract erp")
    expect(page.locator("#tab-drplans")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#tab-drplans .rp-plan-card")).to_have_count(2)     # la liste est dessous
    assert page.evaluate("location.hash") == f"#dr/{PID}"
    # F5 rend le même écran
    page.reload()
    page.wait_for_function("() => window.Router && Router.current()")
    page.evaluate("() => Router.idle()")
    expect(view.locator(".floating-panel-title")).to_have_text("Contract erp")
    # fermer la fenêtre rend l'adresse de la liste, sans entrée d'historique
    n = page.evaluate("history.length")
    view.locator(".floating-panel-header [data-action=close]").click()
    expect(view).to_have_count(0)
    page.wait_for_function("() => location.hash === '#dr'")
    assert page.evaluate("history.length") == n


def test_a_click_on_a_contract_writes_its_address_without_history(ui):
    page, _, _ = ui
    page.evaluate("Router.go('#dr')")
    page.evaluate("() => Router.idle()")
    pane = page.locator("#tab-drplans")
    expect(pane.locator(".rp-plan-card")).to_have_count(2)
    n = page.evaluate("history.length")
    pane.locator(f'.rp-plan-card[data-rp-plan="{PID2}"] h3 a').click()
    expect(page.locator(f"#fp-rp-plan-{PID2} .floating-panel-title")).to_have_text("Contract crm")
    page.wait_for_function(f"() => location.hash === '#dr/{PID2}'")
    assert page.evaluate("history.length") == n
    assert page.evaluate("Router.current().id") == PID2


def test_dr_links_address(ui):
    page, _, _ = ui
    goto(page, base(page), "#dr/links")
    expect(page.locator("#tab-drlinks")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator('#host-nav a[data-hnav-root="dr"][data-hnav-tab="links"]')).to_have_attribute("aria-current", "page")
    expect(page.locator("#rp-links-host")).to_contain_text("harv-dr")


# --- DR d'un hôte ------------------------------------------------------------------

@pytest.mark.parametrize("host,erp_role,crm_role", [
    ("harv-fake", "source", "target"),
    ("harv-dr", "target", "source"),
])
def test_host_dr_shows_both_roles_with_the_side_filter(ui, host, erp_role, crm_role):
    page, sent, _ = ui
    goto(page, base(page), f"#harvester/{host}/dr")
    pane = page.locator("#tab-clusterdr")
    expect(pane).to_have_class(re.compile(r"\bactive\b"))
    expect(pane.locator(f'.rp-plan-card[data-rp-plan="{PID}"] [data-rp-role="{erp_role}"]')).to_have_count(1)
    expect(pane.locator(f'.rp-plan-card[data-rp-plan="{PID2}"] [data-rp-role="{crm_role}"]')).to_have_count(1)
    assert any(s[1] == "plans" and s[2] == f"host={host}" for s in sent)
    # le filtre de la relecture (écran 4) : source / secours
    pane.locator(f'[data-rp-side="{erp_role}"]').click()
    expect(pane.locator(".rp-plan-card")).to_have_count(1)
    expect(pane.locator(".rp-plan-card")).to_contain_text("erp")
    # ouvrir un contrat depuis la page d'hôte garde l'adresse de la page
    pane.locator('[data-rp-side="any"]').click()
    pane.locator(f'.rp-plan-card[data-rp-plan="{PID}"] h3 a').click()
    expect(page.locator(f"#fp-rp-plan-{PID} .floating-panel-title")).to_have_text("Contract erp")
    assert page.evaluate("location.hash") == f"#harvester/{host}/dr"


def test_kvm_host_dr_is_an_empty_state_without_replication_reads(ui):
    page, sent, _ = ui
    page.route(re.compile(r".*/api/kvm/hosts\?declared=1$"), lambda r, q: r.fulfill(**ok({"hosts": [{"name": "node2"}]})))
    before = len(sent)
    goto(page, base(page), "#kvm/node2/dr")
    expect(page.locator("#tab-kvmdr")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#tab-kvmdr .kvm-dr-empty")).to_contain_text("not available yet")
    page.wait_for_timeout(500)
    assert not [s for s in sent[before:] if s[1].startswith("hosts/") or s[1] == "plans" and "host=" in s[2]]


# --- pastilles d'alertes -----------------------------------------------------------

def test_alert_badges_on_root_open_host_entry_and_every_host_row(ui):
    page, _, _ = ui
    page.route(re.compile(r".*/api/hosts$"), lambda r, q: r.fulfill(**ok(hosts_body("harv-fake", "harv-dr", "harv-quiet"))))
    goto(page, base(page), "#harvester/harv-fake/vms")
    page.evaluate("() => HostNav.load()")
    expect(page.locator('#host-nav [data-rp-badge="all"]')).to_have_text("2")
    # entrée DR du sous-menu de l'hôte ouvert
    sub = page.locator('#hnav-families .hnav-sub a[data-sub="dr"] [data-rp-badge="cluster"]')
    expect(sub).to_have_text("2")
    # ligne de chaque hôte : source ET secours comptent, un hôte sans alerte n'a rien
    for h in ("harv-fake", "harv-dr"):
        b = page.locator(f'#hnav-families [data-rp-badge="host"][data-rp-host="{h}"]')
        expect(b).to_have_text("2")
        assert h in b.get_attribute("data-tip")
        assert b.get_attribute("href") == f"#harvester/{h}/dr"
    expect(page.locator('#hnav-families [data-rp-badge="host"][data-rp-host="harv-quiet"]')).to_be_hidden()
    # la pastille mène au DR de l'hôte
    page.locator('#hnav-families [data-rp-badge="host"][data-rp-host="harv-dr"]').click()
    page.wait_for_function("() => location.hash === '#harvester/harv-dr/dr'")
    expect(page.locator("#tab-clusterdr")).to_have_class(re.compile(r"\bactive\b"))
    # elles survivent à un menu redessiné
    page.evaluate("HostNav.render()")
    expect(page.locator('#hnav-families [data-rp-badge="host"][data-rp-host="harv-fake"]')).to_have_text("2")


def test_badge_level_follows_the_worst_alert(ui):
    page, _, _ = ui
    page.route(re.compile(r".*/api/hosts$"), lambda r, q: r.fulfill(**ok(hosts_body("harv-fake", "harv-dr"))))
    err = json.loads(json.dumps(ALERTS))
    err["alerts"][0]["level"] = "error"
    err["alerts"][1].update(source_host="harv-dr", target_host="harv-x")
    page.route(re.compile(r".*/api/replication/alerts(\?.*)?$"), lambda r, q: r.fulfill(**ok(err)))
    goto(page, base(page), "#dr")
    page.evaluate("() => HostNav.load()")
    page.evaluate("() => Replication.refreshBadges()")
    fake = page.locator('#hnav-families [data-rp-badge="host"][data-rp-host="harv-fake"]')
    dr = page.locator('#hnav-families [data-rp-badge="host"][data-rp-host="harv-dr"]')
    expect(fake).to_have_text("1")
    expect(fake).to_have_class(re.compile(r"\bfail\b"))
    expect(dr).to_have_text("2")


# --- journal vers Activité ---------------------------------------------------------

def test_journal_action_link_opens_the_action_in_activity(ui):
    page, _, _ = ui
    detail = plan_detail("admin")
    events = {"plan": PID, "next_before": None, "events": [
        {"id": 3, "ts": "2026-10-25T10:00:00Z", "plan_id": PID, "resource_uid": "", "level": "info",
         "operation": "protect", "step": "start", "code": "started", "message": "protect started", "by": "alice",
         "action_id": ACT_ID}]}
    page.route(re.compile(rf".*/api/replication/plans/{PID}/events(\?.*)?$"), lambda r, q: r.fulfill(**ok(events)))
    page.route(re.compile(rf".*/api/replication/plans/{PID}$"), lambda r, q: r.fulfill(**ok(detail)))
    queries = []

    def activity(route, req):
        queries.append(req.url)
        return route.fulfill(**ok({"in_progress": [], "log_files": [], "matched": 1, "total": 1,
                                   "filters": {"active": True}, "facets": {},
                                   "actions_done": [{"id": ACT_ID, "action": "replication:protect", "cluster": "harv-dr",
                                                     "status": "done", "ts": 1761386400, "started_at": 1761386400,
                                                     "ended_at": 1761386460, "duration": 60.0, "dry_run": False,
                                                     "error_summary": None, "kind": "action"}]}))
    page.route(re.compile(r".*/api/activity(\?.*)?$"), activity)
    page.route(re.compile(rf".*/api/stream/{ACT_ID}$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, base(page), f"#dr/{PID}")
    view = page.locator(f"#fp-rp-plan-{PID}")
    view.locator('[data-rpv-tab="journal"]').click()
    link = view.locator(f'[data-rp-ev-action="{ACT_ID}"]')
    expect(link).to_have_attribute("href", f"#activity/action/{ACT_ID}")
    assert ACT_ID in link.get_attribute("data-tip")
    link.click()
    page.wait_for_function(f"() => location.hash === '#activity/action/{ACT_ID}'")
    page.evaluate("() => Router.idle()")
    expect(page.locator("#tab-activity")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#act-f-q")).to_have_value(ACT_ID)
    expect(page.locator("#fp-activity-detail")).to_be_visible()
    expect(page.locator("#fp-activity-detail")).to_contain_text(ACT_ID)
    assert any(f"q={ACT_ID}" in u for u in queries)
    # changer le filtre rend l'adresse de l'Activité seule
    page.locator("#fp-activity-detail .floating-panel-header [data-action=close]").click()
    page.locator("#act-f-q").fill("")
    page.wait_for_function("() => location.hash === '#activity'", timeout=5000)


# --- pastille DR des VMs -----------------------------------------------------------

def test_vm_dr_pill_is_read_again_when_the_view_comes_back(world):  # noqa: F811
    go, page, state = world
    plans = {"plans": []}
    page.route(re.compile(r".*/api/replication/plans(\?.*)?$"), lambda r, q: r.fulfill(**ok(plans)))
    go("#vms")
    row = page.locator('tr[data-avm-row="harvester/harv-fake/default/db"]')
    expect(row).to_be_visible()
    expect(row.locator(".vmf-dr")).to_have_count(0)
    plans["plans"] = [{"id": PID, "resources": [{"resource_uid": "uid-db"}]}]   # contrat créé ailleurs
    page.evaluate("Router.go('#hosts')")
    page.evaluate("Router.go('#vms')")
    page.evaluate("() => Router.idle()")
    expect(row.locator(".vmf-dr")).to_have_attribute("href", f"#dr/{PID}")
    plans["plans"] = []                                                         # contrat retiré
    page.evaluate("Router.go('#hosts')")
    page.evaluate("Router.go('#vms')")
    page.evaluate("() => Router.idle()")
    expect(row.locator(".vmf-dr")).to_have_count(0)
    assert state["errors"] == []


# --- fenêtre de création : cible sans réseau de VM -------------------------------

def test_create_window_says_the_target_has_no_vm_network(ui):
    page, sent, _ = ui
    page.route(re.compile(r".*/api/networks/harv-dr$"), lambda r, q: r.fulfill(**ok([])))
    page.evaluate("Router.go('#dr')")
    page.evaluate("() => Router.idle()")
    page.locator('#tab-drplans [data-rp="new"]').click()
    panel = page.locator("#fp-rp-create")
    expect(panel.locator('[data-rpc-vm="default/ui-cirros"]')).to_be_visible()
    panel.locator('[name="to"]').select_option("harv-dr")
    panel.locator('[data-rpc-pick="default/ui-cirros"]').check()
    net = panel.locator('[data-rpc-net="default/lab-net"]')
    sel = net.locator("[data-rpc-netmap]")
    expect(sel.locator("option")).to_have_count(1)
    expect(sel.locator("option").first).to_have_text("No VM network on harv-dr")
    assert "harv-dr has no VM network" in sel.get_attribute("data-tip")
    hint = net.locator('[data-rpc-nonet="default/lab-net"]')
    expect(hint).to_contain_text('"Create on the target"')
    # le réseau de gestion (pod) n'est jamais proposé : le moteur ne le sait pas restaurer
    assert not [o for o in sel.locator("option").all_text_contents() if "mgmt" in o.lower() or "pod" in o.lower()]
    expect(net.locator('[data-rpc-ready="fail"]')).to_be_visible()
    # la ligne mène à « Créer sur la cible » ; le réseau créé devient le choix
    net.locator("[data-rpc-netcreate]").click()
    expect(hint).to_have_count(0)
    panel.locator('[data-rpc-netform="default/lab-net"] [data-rpc-ncgo]').click()
    expect(net.locator('[data-rpc-ready="ok"]')).to_be_visible()
    expect(sel.locator("option").first).to_have_text("choose a network")


def test_create_window_with_target_networks_keeps_the_usual_choice(ui):
    page, _, _ = ui
    page.evaluate("Router.go('#dr')")
    page.evaluate("() => Router.idle()")
    page.locator('#tab-drplans [data-rp="new"]').click()
    panel = page.locator("#fp-rp-create")
    expect(panel.locator('[data-rpc-vm="default/ui-cirros"]')).to_be_visible()
    panel.locator('[name="to"]').select_option("harv-dr")
    panel.locator('[data-rpc-pick="default/ui-cirros"]').check()
    net = panel.locator('[data-rpc-net="default/lab-net"]')
    expect(net.locator("[data-rpc-netmap] option").first).to_have_text("choose a network")
    expect(net.locator("[data-rpc-nonet]")).to_have_count(0)
