"""2.5.0 : phases Cluster API et Clusters des plans, dans l'interface (routes simulées).

* carte Cluster API : interrupteur, délai, versions du paquet actif, bulle
  qui dit que la pile n'est jamais mise à niveau par un plan ;
* carte Clusters : deux entrées (modèle, nom à variables, dépendance),
  ordre par monter/descendre, raccourci « Plusieurs » envoyé tel quel et
  développé par le serveur ; enregistrement 409 montré, puis remarques ;
* modèle de cluster et nom de cluster hostiles : échappés partout ;
* fenêtre Déployer : constats de flotte regroupés, bloquants d'abord,
  Lancer grisé avec la raison ; Résumé avec les clusters et la durée ;
* Historique de Bare-metal : un cluster enfant d'un déroulé, lien vers
  Cluster API > Clusters ; une création hors plan n'y figure pas.

Mutations jouées (rapport des tâches 5-8) : M-launch (Lancer actif malgré
un constat de flotte), M-escape (`esc` retiré du nom de cluster du Résumé).
"""

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402
from test_bm_profiles_180 import shot  # noqa: E402

C = "harv-fake"
EVIL = '<img src=x onerror="window.__capi250=1">'
PLAN = {"name": "site-k8s", "revision": 2, "description": "sites", "variables": [],
        "phases": {"readiness": {"enabled": True, "timeout_minutes": 20},
                   "terraform": {"enabled": False, "templates": []},
                   "capi": {"enabled": True, "timeout_minutes": 12},
                   "clusters": {"enabled": True, "timeout_minutes": 40,
                                "items": [{"template": "rke2-small", "name": "{{cluster}}-apps"}]},
                   "registry": {"enabled": False}, "artefacts": {"enabled": False}}}
LIST = {"plans": [{"name": "site-k8s", "revision": 2, "description": "sites", "variables": [],
                   "phases_enabled": ["readiness", "capi", "clusters"]}],
        "builtins": ["cluster", "vip"],
        "phases": ["readiness", "terraform", "capi", "clusters", "registry", "artefacts"],
        "available": ["readiness", "terraform", "capi", "clusters"]}
CTEMPLATES = {"templates": [
    {"name": "rke2-small", "revision": 3, "description": "1+1", "variables": [], "uses": [], "vms": 2},
    {"name": EVIL, "revision": 1, "description": EVIL, "variables": [EVIL], "uses": [], "vms": None}]}
BUNDLES = {"bundles": [], "active": "capi-bundle-x.tar.gz",
           "active_providers": ["bootstrap/rke2 v0.25.2", "controlPlane/rke2 v0.25.2",
                                "infrastructure/harvester v0.10.1", "addon/helm v0.6.4"]}
FLEET = {"findings": [
    {"code": "capi-later", "level": "info", "phase": "capi", "detail": "checked when the phase starts"},
    {"code": "cluster-exists", "level": "info", "phase": "clusters", "detail": "harv-fake-apps"},
    {"code": "fleet-memory-short", "level": "blocking", "phase": "clusters",
     "detail": "needed 48 GiB, free 30 GiB"}],
    "summary": {"declarations": [], "estimate_minutes": 32,
                "capi": {"providers": ["infrastructure/harvester v0.10.1"], "ready": True,
                         "installed_version_differs": False},
                "clusters": [{"name": "harv-fake-apps", "template": "rke2-small", "vms": 2, "exists": True},
                             {"name": EVIL, "template": EVIL, "vms": 4, "exists": False}]},
    "blocking": True}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def open_page(context, flask_server, hash, role="admin", put=None, activity=None, check=None):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    st = {"put": [], "check": [], "deploy": [], "errors": []}
    puts = list(put or [])

    def plans(route, req):
        url, method = req.url.split("?")[0], req.method
        if url.endswith("/api/baremetal/plans") and method == "GET":
            return fulfill(route, LIST)
        if url.endswith("/api/baremetal/plans/site-k8s") and method == "GET":
            return fulfill(route, PLAN)
        if method == "PUT":
            st["put"].append(req.post_data_json)
            body, status = puts.pop(0) if puts else (dict(PLAN, revision=3), 200)
            return fulfill(route, body, status)
        return fulfill(route, {"error": "unexpected"}, 500)

    def deploy(route, req):
        url = req.url.split("?")[0]
        if url.endswith("/check"):
            st["check"].append(req.post_data_json)
            return fulfill(route, check or FLEET)
        st["deploy"].append(req.post_data_json)
        return fulfill(route, {"action_id": "abcdef012345", "findings": []}, 202)

    def act(route, req):
        q = parse_qs(urlparse(req.url).query).get("action", [""])[0]
        fulfill(route, (activity or {}).get(q, {"in_progress": [], "actions_done": []}))

    page.route(re.compile(r".*/api/baremetal/plans.*"), plans)
    page.route(re.compile(r".*/api/baremetal/tf-templates$"), lambda r, q: fulfill(r, {"templates": [], "kinds": []}))
    page.route(re.compile(r".*/api/baremetal/capi-templates$"), lambda r, q: fulfill(r, CTEMPLATES))
    page.route(re.compile(r".*/api/capi/bundles$"), lambda r, q: fulfill(r, BUNDLES))
    page.route(re.compile(r".*/api/deploy/.*"), deploy)
    page.route(re.compile(r".*/api/activity\?.*"), act)
    page.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(r, {"role": role, "roles_active": True,
                                                                       "user": "u", "auth": "local"}))
    page.on("dialog", lambda d: d.accept())
    page.on("pageerror", lambda e: st["errors"].append(str(e)))
    goto(page, flask_server["base_url"], hash)
    page.mouse.move(1100, 450)
    return page, st


def test_plan_with_two_clusters_and_the_several_shortcut(context, flask_server):
    saved = dict(PLAN, revision=3, warnings=["phases.clusters.free_key: unknown key removed"])
    saved["phases"] = dict(PLAN["phases"], clusters={"enabled": True, "timeout_minutes": 40, "items": [
        {"template": "rke2-small", "name": "{{cluster}}-apps"},
        {"template": "rke2-small", "name": "{{cluster}}-tools", "depends_on": ["{{cluster}}-apps"]},
        {"template": "rke2-small", "name": "{{cluster}}-k8s-1"},
        {"template": "rke2-small", "name": "{{cluster}}-k8s-2"}]})
    conflict = ({"error": "the plan changed in the meantime", "code": "conflict", "current_revision": 3}, 409)
    page, st = open_page(context, flask_server, "#automation/pxe/plans", put=[conflict, (saved, 200)])
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    form = page.locator("#bm-plans .bmpl-form")
    # Registre et Artefacts restent à venir ; Cluster API et Clusters actifs
    for pid in ("registry", "artefacts"):
        expect(form.locator(f'.bmpl-phase[data-phase="{pid}"] .bmpl-coming')).to_have_text("coming in 2.6+")
    capi = form.locator('.bmpl-phase[data-phase="capi"]')
    expect(capi.locator(".bmpl-phase-on")).to_be_enabled()
    expect(capi.locator(".bmpl-phase-on")).to_be_checked()
    expect(capi.locator(".bmpl-coming")).to_have_count(0)
    expect(capi.locator('[name="capi_timeout"]')).to_have_value("12")
    expect(capi.locator(".bmpl-capi-provider")).to_have_count(4)
    expect(capi.locator(".bmpl-capi-versions")).to_contain_text("infrastructure/harvester v0.10.1")
    assert "never upgrades" in capi.locator(".bmpl-capi-versions").get_attribute("data-tip")
    cl = form.locator('.bmpl-phase[data-phase="clusters"]')
    expect(cl.locator('[name="clusters_timeout"]')).to_have_value("40")
    rows = cl.locator(".bmpl-cl")
    expect(rows).to_have_count(1)
    # deuxième cluster, nommé, qui dépend du premier
    cl.locator(".bmpl-cl-add").click()
    expect(rows).to_have_count(2)
    rows.nth(1).locator(".bmpl-cl-name").fill("{{cluster}}-tools")
    rows.nth(1).locator(".bmpl-cl-name").press("Tab")
    rows.nth(1).locator('.bmpl-cl-dep[value="{{cluster}}-apps"]').check()
    # monter/descendre : la dépendance qui passerait après tombe, comme Terraform
    rows.nth(1).locator(".bmpl-cl-up").click()
    expect(rows.nth(0).locator(".bmpl-cl-name")).to_have_value("{{cluster}}-tools")
    expect(rows.nth(1).locator('.bmpl-cl-dep[value="{{cluster}}-tools"]')).not_to_be_checked()
    rows.nth(0).locator(".bmpl-cl-down").click()
    rows.nth(1).locator('.bmpl-cl-dep[value="{{cluster}}-apps"]').check()
    for el in cl.locator("button, select, input").all():
        if el.is_visible():
            assert el.get_attribute("data-tip") or el.evaluate("e => !!e.closest('.tip')"), el.evaluate("e => e.outerHTML")
    # « Plusieurs » : motif sans {{n}} refusé sur place, puis envoyé tel quel
    cl.locator(".bmpl-cl-several").click()
    box = cl.locator(".bmpl-cl-shortcut")
    expect(box).to_be_visible()
    box.locator(".bmpl-sc-tpl").select_option("rke2-small")
    box.locator(".bmpl-sc-count").fill("2")
    box.locator(".bmpl-sc-pattern").fill("{{cluster}}-k8s")
    box.locator(".bmpl-sc-ok").click()
    expect(box.locator(".bmpl-sc-msg")).to_contain_text("{{n}}")
    box.locator(".bmpl-sc-pattern").fill("{{cluster}}-k8s-{{n}}")
    box.locator(".bmpl-sc-ok").click()
    expect(box).to_be_hidden()
    expect(cl.locator(".bmpl-sc-pending")).to_contain_text("2 cluster(s) named {{cluster}}-k8s-{{n}}")
    cl.scroll_into_view_if_needed()
    shot(page, "bm-plans-capi-clusters")
    # enregistrer : 409 dit, saisie gardée
    form.locator(".bmpl-save").click()
    expect(form.locator(".bmpl-conflict")).to_contain_text("revision 3")
    expect(rows).to_have_count(2)
    body = st["put"][0]
    assert body["revision"] == 2
    assert body["phases"]["capi"] == {"enabled": True, "timeout_minutes": 12}
    assert body["phases"]["clusters"] == {
        "enabled": True, "timeout_minutes": 40,
        "items": [{"template": "rke2-small", "name": "{{cluster}}-apps"},
                  {"template": "rke2-small", "name": "{{cluster}}-tools", "depends_on": ["{{cluster}}-apps"]}],
        "template": "rke2-small", "count": 2, "name_pattern": "{{cluster}}-k8s-{{n}}"}
    assert body["phases"]["registry"] == {"enabled": False}
    # deuxième essai : le serveur a développé le raccourci et rend ses remarques
    form.locator(".bmpl-save").click()
    expect(page.locator("#bm-plans .bmpl-warnings")).to_contain_text("unknown key removed")
    expect(page.locator("#bm-plans .bmpl-cl")).to_have_count(4)
    expect(page.locator("#bm-plans .bmpl-sc-pending")).to_have_count(0)
    assert st["errors"] == []


def test_capi_timeout_out_of_bounds_is_refused_before_sending(context, flask_server):
    page, st = open_page(context, flask_server, "#automation/pxe/plans")
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    form = page.locator("#bm-plans .bmpl-form")
    form.locator('[name="capi_timeout"]').fill("61")
    form.locator(".bmpl-save").click()
    expect(form.locator(".bmpl-msg")).to_contain_text("from 1 to 60")
    assert st["put"] == []


def test_hostile_cluster_template_name_is_escaped(context, flask_server):
    page, st = open_page(context, flask_server, "#automation/pxe/plans")
    table = page.locator("#bmpl-ctpl-table")
    expect(table.locator("tbody tr")).to_have_count(2)
    expect(table).to_contain_text(EVIL)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    sel = page.locator("#bm-plans .bmpl-cl-tpl").first
    expect(sel.locator("option")).to_have_count(2)
    assert EVIL in sel.locator("option").all_inner_texts()
    assert page.evaluate("window.__capi250") is None
    assert page.locator("#bm-plans img[src=x]").count() == 0


def test_deploy_window_shows_fleet_findings_and_disables_launch(context, flask_server):
    page, st = open_page(context, flask_server, "#automation/pxe/plans")
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    page.locator("#bm-plans .bmpl-apply").click()
    win = page.locator("#fp-deploy-plan")
    win.locator('[name="cluster"]').select_option(C)
    launch = win.locator(".dpw-launch")
    expect(win.locator(".dpw-count")).to_have_text("1")
    expect(launch).to_be_disabled()
    assert "Not enough free memory for all the clusters together" in launch.get_attribute("title")
    assert win.locator(".dpw-dry").is_disabled()
    win.locator('[data-dpw-tab="checks"]').click()
    groups = win.locator(".dpw-group")
    expect(groups.first).to_have_attribute("data-phase", "clusters")          # bloquant d'abord
    expect(groups.first.locator(".dpw-finding").first).to_have_attribute("data-code", "fleet-memory-short")
    expect(groups.first).to_contain_text("needed 48 GiB, free 30 GiB")
    expect(groups.nth(1)).to_have_attribute("data-phase", "capi")
    expect(groups.nth(1)).to_contain_text("Harvester version checked when the phase starts")
    win.locator('[data-dpw-tab="summary"]').click()
    expect(win.locator(".dpw-summary")).to_contain_text("about 32 min")
    expect(win.locator(".dpw-capi")).to_contain_text("already ready")
    rows = win.locator(".dpw-clusters tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("already present")
    expect(rows.nth(1)).to_contain_text("new")
    expect(rows.nth(1).locator(".dpw-cluster-name")).to_have_text(EVIL)
    shot(page, "bm-plans-capi-deploy-summary")
    assert page.evaluate("window.__capi250") is None
    assert st["deploy"] == []
    assert st["errors"] == []


def test_bare_metal_history_links_a_child_cluster(context, flask_server):
    parent = {"id": "a0000000d250", "action": f"deploy:{C}", "cluster": C, "status": "done",
              "started_at": 1000, "ended_at": 2000, "result": {"plan": "site-k8s"}}
    child = {"id": "a0000000c250", "action": "capi-cluster-create:harv-fake-apps/harv-fake-apps", "cluster": C,
             "status": "done", "started_at": 1100, "ended_at": 1900, "parent": "a0000000d250"}
    alone = {"id": "a0000000e250", "action": "capi-cluster-create:web/web", "cluster": C,
             "status": "done", "started_at": 900, "ended_at": 950}
    act = {"deploy": {"in_progress": [], "actions_done": [parent]},
           "capi-cluster-create": {"in_progress": [], "actions_done": [child, alone]}}
    page, st = open_page(context, flask_server, "#automation/pxe/history", activity=act)
    rows = page.locator("#bm-history tr.bm-history-row")
    expect(rows).to_have_count(1)                     # hors plan : absent ; fille : sous son parent
    rows.first.locator("[data-history-expand]").click()
    kid = page.locator('#bm-history tr[data-action-id="a0000000c250"]')
    expect(kid).to_be_visible()
    expect(kid).to_contain_text("Child cluster")
    link = kid.locator(".bmpl-cluster-link")
    expect(link).to_have_attribute("href", "#automation/capi/clusters")
    assert link.get_attribute("data-tip")
    assert st["errors"] == []
