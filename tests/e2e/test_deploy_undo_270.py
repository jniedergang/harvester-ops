"""2.7.0 (tâche 8) : fenêtre « Défaire le déploiement » depuis l'Historique
de Bare-metal (routes simulées).

* Historique : bouton Défaire sur un déroulé `deploy:` fini, jamais sur un
  essai ni sur une ligne `deploy-undo:` (point 214) ; grisé avec sa raison
  pour un non-administrateur ; badge « Défait en partie » ;
* Contenu : cases cochées selon le serveur, les décochées expliquées
  (registre, création non prouvée, déclaration modifiée) ;
* le bouton Défaire reste grisé sans plan de destruction et sans le nom
  tapé, et de nouveau après un changement de cases jusqu'au nouveau plan ;
  aucune requête après la fermeture ; second nom tapé pour les données d'un
  registre ; noms hostiles échappés ; lancement suivi.

Mutations jouées (relevé /tmp/bmpost/270-mut/task8.txt) : « bouton Défaire
actif sans nom », « échappement retiré ».
"""

import json
import re
import time
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402
from test_bm_profiles_180 import shot  # noqa: E402

C = "harv-fake"
EVIL = '<img src=x onerror="window.__undo270=1">'
RUN = {"id": "a0000000d270", "action": f"deploy:{C}", "status": "done", "started_at": 1767000000,
       "ended_at": 1767000300, "dry_run": False, "result": {"plan": "site-k8s", "cluster": C}}
DRY = dict(RUN, id="a0000000d271", dry_run=True)
PARTIAL = dict(RUN, id="a0000000d272", status="error",
               result={"plan": "site-k8s", "cluster": C, "undone_by": "b0000000d273", "undone_status": "partial"})
UNDO_ROW = {"id": "b0000000d273", "action": f"deploy-undo:{C}", "status": "error", "started_at": 1767000400,
            "ended_at": 1767000500, "dry_run": False, "result": {"undoes": "a0000000d272", "status": "partial"}}
ITEMS = [
    {"id": "chart-a/a/podinfo", "kind": "chart", "ref": "a/a/podinfo", "phase": "artefacts", "run": "a0000000d270",
     "proven": True, "default": True, "note": "", "selected": True,
     "details": {"present": True, "release": "podinfo", "release_namespace": EVIL, "chart": "podinfo", "version": "6.0.0"}},
    {"id": "capi-cluster-a/a", "kind": "capi-cluster", "ref": "a/a", "phase": "clusters", "run": "a0000000d270",
     "proven": True, "default": True, "note": "", "selected": True,
     "details": {"present": True, "machines": 2, "target_namespace": "a", "vms": [EVIL], "volumes": [],
                 "loadbalancers": [], "namespace_removed": True}},
    {"id": "registry-harv-fake/amorcage", "kind": "registry", "ref": "harv-fake/amorcage", "phase": "registry",
     "run": "a0000000d270", "proven": True, "default": False,
     "note": "unchecked by default: the registry and its data outlive the plan", "selected": False,
     "details": {"present": True, "address": "172.16.2.76", "volume": "amorcage-data"}},
    {"id": f"tf-declaration-{EVIL}", "kind": "tf-declaration", "ref": EVIL, "phase": "terraform",
     "run": "a0000000d270", "proven": False, "default": False, "note": "creation by this run not proven",
     "selected": False, "details": {"present": True, "name": EVIL, "resources": ["harvester_network.x"]}},
    {"id": "tf-declaration-d2", "kind": "tf-declaration", "ref": "d2", "phase": "terraform", "run": "a0000000d270",
     "proven": True, "default": False, "note": "changed since it was applied", "selected": False,
     "details": {"present": True, "name": "d2", "resources": []}},
]
LIMITS = [{"code": "lb-dhcp", "detail": "x"}, {"code": "unknown-limit", "detail": EVIL}]
STREAM = ('event: step\ndata: {"type": "step", "step_id": "undo-chart-a/a/podinfo", "status": "done", '
          '"message": "removed (action 0123456789ab)"}\n\n'
          'event: end\ndata: {"status": "done"}\n\n')


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def plan_body(include):
    chosen = set(include) if include is not None else {i["id"] for i in ITEMS if i["default"]}
    return {"undoes": RUN["id"], "plan": "site-k8s", "cluster": C, "limits": LIMITS, "undone_by": None,
            "items": [dict(i, selected=i["id"] in chosen) for i in ITEMS]}


def open_page(context, flask_server, role="admin", hold=False, runs=None):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    st = {"plan": [], "undo": [], "held": [], "errors": []}

    def deploy(route, req):
        url = req.url.split("?")[0]
        body = req.post_data_json
        if url.endswith("/undo/plan"):
            st["plan"].append(body)
            # premier plan servi tout de suite ; les suivants retenus si demandé
            if hold and len(st["plan"]) > 1:
                st["held"].append((route, body))
                return None
            return fulfill(route, plan_body(body.get("include")))
        if url.endswith("/undo"):
            st["undo"].append(body)
            return fulfill(route, {"action_id": "c0000000d274", "undoes": body["action_id"],
                                   "selected": body["include"]}, 202)
        return fulfill(route, {"error": "unexpected"}, 500)

    def act(route, req):
        q = parse_qs(urlparse(req.url).query).get("action", [""])[0]
        lst = runs if runs is not None else [RUN, DRY, PARTIAL, UNDO_ROW]
        fulfill(route, {"in_progress": [], "actions_done": [a for a in lst if q in a["action"]]})

    page.route(re.compile(r".*/api/deploy/.*"), deploy)
    page.route(re.compile(r".*/api/activity\?.*"), act)
    page.route(re.compile(r".*/api/stream/c0000000d274$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body=STREAM))
    page.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(r, {"role": role, "roles_active": True,
                                                                       "user": "u", "auth": "local"}))
    page.on("dialog", lambda d: d.accept())
    page.on("pageerror", lambda e: st["errors"].append(str(e)))
    goto(page, flask_server["base_url"], "#automation/pxe/history")
    page.mouse.move(1100, 450)
    return page, st


def row(page, action_id):
    return page.locator(f'#bm-history tr.bm-history-row[data-action-id="{action_id}"]')


def wait_until(page, cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        page.wait_for_timeout(50)
    return cond()


def test_history_buttons_badge_and_kinds(context, flask_server):
    page, st = open_page(context, flask_server)
    expect(page.locator("#bm-history tr.bm-history-row")).to_have_count(4)
    expect(row(page, RUN["id"]).locator(".bm-deploy-undo")).to_be_enabled()
    expect(row(page, DRY["id"]).locator(".bm-deploy-undo")).to_have_count(0)            # essai : rien à défaire
    expect(row(page, UNDO_ROW["id"]).locator(".bm-deploy-undo")).to_have_count(0)       # pas de défaire du défaire
    expect(row(page, UNDO_ROW["id"])).to_contain_text("Deployment undone")
    badge = row(page, PARTIAL["id"]).locator(".bm-undone")
    expect(badge).to_have_text("Partially undone")
    assert badge.get_attribute("href") == "#activity/action/b0000000d273"
    expect(row(page, PARTIAL["id"]).locator(".bm-deploy-undo")).to_be_enabled()         # reprise du défaire
    assert row(page, RUN["id"]).locator(".bm-deploy-undo").get_attribute("data-tip")
    assert st["errors"] == []


def test_undo_button_greyed_for_an_operator(context, flask_server):
    page, st = open_page(context, flask_server, role="operator")
    btn = row(page, RUN["id"]).locator(".bm-deploy-undo")
    expect(btn).to_be_disabled()
    assert "reserved to administrators" in btn.get_attribute("data-tip")


def test_content_defaults_plan_name_and_launch(context, flask_server):
    page, st = open_page(context, flask_server)
    row(page, RUN["id"]).locator(".bm-deploy-undo").click()
    win = page.locator(f"#fp-deploy-undo-{RUN['id']}")
    expect(win).to_be_visible()
    # valeur hostile : visée par sa place, pas par un sélecteur d'attribut
    box = lambda i: (win.locator('.udw-group[data-kind="tf-declaration"] .udw-item').first if i == EVIL  # noqa: E731
                     else win.locator(f'.udw-item[value="{i}"]'))
    expect(box("chart-a/a/podinfo")).to_be_checked()
    expect(box("capi-cluster-a/a")).to_be_checked()
    expect(box("registry-harv-fake/amorcage")).not_to_be_checked()
    expect(box(EVIL)).not_to_be_checked()
    expect(box("tf-declaration-d2")).not_to_be_checked()
    tip = lambda i: box(i).evaluate("e => e.closest('.tip').dataset.tip")     # noqa: E731
    assert "outlive the plan" in tip("registry-harv-fake/amorcage")
    assert "not proven" in tip(EVIL)
    assert "changed since" in tip("tf-declaration-d2")
    assert st["plan"][0] == {"action_id": RUN["id"]}
    # plan revenu, nom pas tapé : grisé, avec la raison
    launch = win.locator(".udw-launch")
    expect(launch).to_be_disabled()
    expect(launch).to_have_attribute("title", re.compile("typed name matches"))
    win.locator('[data-udw-tab="plan"]').click()
    expect(win.locator(".udw-plan-item")).to_have_count(2)
    expect(win.locator(".udw-limits")).to_contain_text("DHCP")
    expect(win.locator(".udw-limits")).to_contain_text(EVIL)                    # limite inconnue : texte du serveur
    expect(win.locator('.udw-plan-item[data-kind="capi-cluster"]')).to_contain_text(EVIL)   # VM hostile, échappée
    expect(win.locator('.udw-group[data-kind="tf-declaration"] .udw-ref').first).to_have_text(EVIL)
    shot(page, "deploy-undo-plan")
    win.locator('[data-udw-tab="confirm"]').click()
    win.locator(".udw-typed").fill("harv-fak")
    expect(launch).to_be_disabled()
    win.locator(".udw-typed").fill(C)
    expect(launch).to_be_enabled()
    win.locator('[data-udw-tab="content"]').click()
    shot(page, "deploy-undo-content")
    win.locator('[data-udw-tab="confirm"]').click()
    for el in win.locator("button, input").all():
        if el.is_visible():
            assert el.get_attribute("data-tip") or el.evaluate("e => !!e.closest('.tip')"), el.evaluate("e => e.outerHTML")
    assert page.evaluate("window.__undo270") is None
    launch.click()
    expect(win.locator(".dpw-follow")).to_be_visible()
    expect(win.locator(".dpw-steps")).to_contain_text("removed")
    assert st["undo"] == [{"action_id": RUN["id"], "confirm": C,
                           "include": ["chart-a/a/podinfo", "capi-cluster-a/a"], "delete_data": []}]
    assert win.locator(".dpw-follow .dpw-action-link").get_attribute("href") == "#activity/action/c0000000d274"
    expect(launch).to_be_disabled()
    assert st["errors"] == []


def test_checkbox_change_greys_until_the_new_plan(context, flask_server):
    page, st = open_page(context, flask_server, hold=True)
    row(page, RUN["id"]).locator(".bm-deploy-undo").click()
    win = page.locator(f"#fp-deploy-undo-{RUN['id']}")
    win.locator('[data-udw-tab="confirm"]').click()
    win.locator(".udw-typed").fill(C)
    launch = win.locator(".udw-launch")
    expect(launch).to_be_enabled()
    win.locator('[data-udw-tab="content"]').click()
    # deux changements rapprochés : une seule requête, celle des cases finales
    win.locator('.udw-item[value="capi-cluster-a/a"]').uncheck()
    win.locator('.udw-item[value="tf-declaration-d2"]').check()
    expect(launch).to_be_disabled()
    expect(launch).to_have_attribute("title", re.compile("Waiting for the destruction plan"))
    assert wait_until(page, lambda: len(st["held"]) == 1)
    page.wait_for_timeout(700)
    assert len(st["held"]) == 1
    route, body = st["held"][0]
    assert sorted(body["include"]) == ["chart-a/a/podinfo", "tf-declaration-d2"]
    expect(launch).to_be_disabled()                          # plan pas encore revenu
    route.fulfill(status=200, content_type="application/json", body=json.dumps(plan_body(body["include"])))
    expect(launch).to_be_enabled()
    win.locator('[data-udw-tab="plan"]').click()
    expect(win.locator(".udw-plan-item")).to_have_count(2)
    expect(win.locator('.udw-plan-item[data-kind="tf-declaration"]')).to_be_visible()
    assert st["errors"] == []


def test_registry_data_needs_its_typed_name(context, flask_server):
    page, st = open_page(context, flask_server)
    row(page, RUN["id"]).locator(".bm-deploy-undo").click()
    win = page.locator(f"#fp-deploy-undo-{RUN['id']}")
    win.locator('.udw-item[value="registry-harv-fake/amorcage"]').check()
    win.locator('[data-udw-tab="confirm"]').click()
    win.locator(".udw-typed").fill(C)
    launch = win.locator(".udw-launch")
    expect(launch).to_be_enabled()
    win.locator('.udw-del-data[value="amorcage"]').check()
    expect(launch).to_be_disabled()
    expect(launch).to_have_attribute("title", re.compile("Type amorcage"))
    win.locator('.udw-reg-typed[data-registry="amorcage"]').fill("amorcage")
    expect(launch).to_be_enabled()
    launch.click()
    expect(win.locator(".dpw-follow")).to_be_visible()
    body = st["undo"][0]
    assert body["delete_data"] == ["amorcage"] and body["confirm_registry"] == {"amorcage": "amorcage"}
    assert "registry-harv-fake/amorcage" in body["include"]


def test_no_late_request_after_close(context, flask_server):
    page, st = open_page(context, flask_server)
    row(page, RUN["id"]).locator(".bm-deploy-undo").click()
    win = page.locator(f"#fp-deploy-undo-{RUN['id']}")
    expect(win.locator('.udw-item[value="capi-cluster-a/a"]')).to_be_checked()
    win.locator('.udw-item[value="capi-cluster-a/a"]').uncheck()
    page.evaluate(f"FloatingPanels.close('deploy-undo-{RUN['id']}')")
    expect(win).to_have_count(0)
    page.wait_for_timeout(1200)
    assert len(st["plan"]) == 1 and st["undo"] == []
    assert st["errors"] == []
