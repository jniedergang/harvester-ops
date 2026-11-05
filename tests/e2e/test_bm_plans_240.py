"""2.4.0 : plans de déploiement dans l'interface (routes simulées).

Aucun plan ne s'applique pour de vrai ; on vérifie ce que l'interface
montre et envoie :
* onglet Plans : éditer et enregistrer un plan à la révision lue, conflit
  409 dit clairement (saisie gardée, Recharger) ; phases de 2.5 grisées,
  non activables, avec leur badge et leur bulle ; modèles choisis dans une
  liste et ordonnés par boutons ;
* modèle au nom hostile : échappé partout (tableau, liste du plan) ;
* fenêtre Déployer : constats bloquants d'abord, Lancer grisé avec la raison,
  aucun secret pour un cluster existant, lancement puis suivi (étapes,
  action fille liée) ; case « mettre à jour les déclarations qui diffèrent »
  seulement devant un constat decl-differs ;
* page d'hôte : « Appliquer un plan » ouvre la fenêtre, cluster choisi ;
  grisé pour un lecteur avec la raison ;
* Historique : un déroulé `deploy:` arrêté, son libellé et « Reprendre »
  (secrets redemandés sur 400 secrets-required).
"""

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto, settle  # noqa: E402
from test_bm_profiles_180 import shot  # noqa: E402

C = "harv-fake"
EVIL = '<img src=x onerror="window.__tpl240=1">'
PLAN = {"name": "site-std", "revision": 3, "description": "standard site", "variables": ["vlan"],
        "phases": {"readiness": {"enabled": True, "timeout_minutes": 20},
                   "terraform": {"enabled": True, "templates": [{"template": "net"}, {"template": "sc"}]},
                   "capi": {"enabled": False}}}
LIST = {"plans": [{"name": "site-std", "revision": 3, "description": "standard site", "variables": ["vlan"],
                   "phases_enabled": ["readiness", "terraform"]}],
        "builtins": ["cluster", "vip"],
        "phases": ["readiness", "terraform", "capi", "clusters", "registry", "artefacts"],
        "available": ["readiness", "terraform"]}
TEMPLATES = {"templates": [
    {"name": "net", "revision": 1, "description": "networks", "variables": ["vlan"], "kinds": ["raw"], "uses": ["vlan"]},
    {"name": "sc", "revision": 2, "description": "storage", "variables": [], "kinds": ["raw"], "uses": []},
    {"name": EVIL, "revision": 1, "description": EVIL, "variables": [], "kinds": ["raw"], "uses": []}],
    "kinds": ["vm", "image", "ssh_key", "raw"]}
BLOCKING = {"findings": [
    {"code": "readiness-later", "level": "info", "phase": "readiness", "detail": ""},
    {"code": "var-missing", "level": "blocking", "phase": "terraform",
     "detail": "templates[net].resources[0].spec.tf: missing variable"}],
    "summary": {"declarations": [{"name": "site-std-net", "template": "net", "exists": False, "origin_match": False}],
                "estimate_minutes": 22}, "blocking": True}
CLEAN = {"findings": [
    {"code": "decl-exists", "level": "info", "phase": "terraform", "detail": "site-std-net exists"},
    {"code": "decl-differs", "level": "warning", "phase": "terraform", "detail": "site-std-sc differs: it is kept"}],
    "summary": {"declarations": [{"name": "site-std-net", "template": "net", "exists": True, "origin_match": True},
                                 {"name": "site-std-sc", "template": "sc", "exists": False, "origin_match": False}],
                "estimate_minutes": 24}, "blocking": False}
STREAM = ('event: step\ndata: {"type": "step", "step_id": "phase-readiness", "status": "done", "message": "ready"}\n\n'
          'event: step\ndata: {"type": "step", "step_id": "tf-net", "status": "error", '
          '"message": "apply failed (action 0123456789ab)"}\n\n'
          'event: end\ndata: {"status": "error"}\n\n')


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def open_page(context, flask_server, hash, role="admin", put=None, activity=None, resume=None):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    st = {"put": [], "check": [], "deploy": [], "resume": [], "errors": []}
    puts = list(put or [({"error": "the plan changed in the meantime", "code": "conflict", "current_revision": 4}, 409)])
    resumes = list(resume or [])

    def plans(route, req):
        url, method = req.url.split("?")[0], req.method
        if url.endswith("/api/baremetal/plans") and method == "GET":
            return fulfill(route, LIST)
        if url.endswith("/api/baremetal/plans/site-std") and method == "GET":
            return fulfill(route, PLAN)
        if "/api/baremetal/plans/" in url and method == "PUT":
            st["put"].append(req.post_data_json)
            body, status = puts.pop(0) if len(puts) > 1 else puts[0]
            return fulfill(route, body, status)
        return fulfill(route, {"error": "unexpected"}, 500)

    def deploy(route, req):
        url = req.url.split("?")[0]
        body = req.post_data_json
        if url.endswith("/check"):
            st["check"].append(body)
            return fulfill(route, CLEAN if (body.get("variables") or {}).get("vlan") else BLOCKING)
        if url.endswith("/resume"):
            st["resume"].append(body)
            resp, status = resumes.pop(0) if resumes else ({"action_id": "fedcba987654", "resumes": body["action_id"],
                                                             "findings": []}, 202)
            return fulfill(route, resp, status)
        st["deploy"].append(body)
        return fulfill(route, {"action_id": "abcdef012345", "findings": []}, 202)

    def act(route, req):
        q = parse_qs(urlparse(req.url).query).get("action", [""])[0]
        fulfill(route, (activity or {}).get(q, {"in_progress": [], "actions_done": []}))

    page.route(re.compile(r".*/api/baremetal/plans.*"), plans)
    page.route(re.compile(r".*/api/baremetal/tf-templates$"), lambda r, q: fulfill(r, TEMPLATES))
    page.route(re.compile(r".*/api/deploy/.*"), deploy)
    page.route(re.compile(r".*/api/activity\?.*"), act)
    page.route(re.compile(r".*/api/stream/(abcdef012345|fedcba987654)$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body=STREAM))
    page.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(r, {"role": role, "roles_active": True,
                                                                       "user": "u", "auth": "local"}))
    page.on("dialog", lambda d: d.accept())
    page.on("pageerror", lambda e: st["errors"].append(str(e)))
    goto(page, flask_server["base_url"], hash)
    page.mouse.move(1100, 450)
    return page, st


def test_plans_tab_edits_a_plan_and_shows_a_conflict(context, flask_server):
    page, st = open_page(context, flask_server, "#automation/pxe/plans")
    expect(page.locator('#bmc-body [data-bm-pane="plans"]')).to_be_visible()
    expect(page.locator('#tab-automation [data-bm-tab="plans"]')).to_have_class(re.compile(r"\bactive\b"))
    page.locator('.bmpl-card[data-plan="site-std"]').click()
    form = page.locator("#bm-plans .bmpl-form")
    expect(form.locator('[name="name"]')).to_have_value("site-std")
    assert form.locator('[name="name"]').get_attribute("readonly") is not None
    # phases que le serveur ne dit pas disponibles : grisées, non activables,
    # badge et bulle (2.5.0 : registre et artefacts, à venir en 2.6+)
    for pid in ("capi", "clusters", "registry", "artefacts"):
        card = form.locator(f'.bmpl-phase[data-phase="{pid}"]')
        expect(card.locator(".bmpl-phase-on")).to_be_disabled()
        expect(card.locator(".bmpl-coming")).to_have_text("coming in 2.6+")
        assert "2.6" in card.locator(".bmpl-coming").get_attribute("data-tip")
    page.evaluate("document.querySelector('.bmpl-phase[data-phase=capi] .bmpl-phase-on').click()")
    expect(form.locator('.bmpl-phase[data-phase="capi"] .bmpl-phase-on')).not_to_be_checked()
    # modèles : ordre par boutons, ajout depuis la liste
    tpls = form.locator(".bmpl-tpl")
    expect(tpls).to_have_count(2)
    tpls.nth(1).locator(".bmpl-up").click()
    expect(tpls.nth(0)).to_have_attribute("data-tpl", "sc")
    assert tpls.nth(0).locator(".bmpl-up").is_disabled()
    form.locator(".bmpl-tpl").nth(1).locator('.bmpl-dep[value="sc"]').check()
    form.locator('[name="timeout_minutes"]').fill("45")
    form.locator('[name="description"]').fill("edited site")
    form.locator(".bmpl-save").click()
    conflict = form.locator(".bmpl-conflict")
    expect(conflict).to_contain_text("Someone changed this plan in the meantime (now revision 4)")
    expect(form.locator('[name="description"]')).to_have_value("edited site")          # saisie gardée
    assert conflict.locator(".bmpl-reload").get_attribute("data-tip")
    sent = st["put"][0]
    assert sent["revision"] == 3                                                      # révision lue
    assert sent["phases"]["readiness"] == {"enabled": True, "timeout_minutes": 45}
    assert sent["phases"]["terraform"]["templates"] == [{"template": "sc"}, {"template": "net", "depends_on": ["sc"]}]
    assert sent["phases"]["capi"] == {"enabled": False}
    assert sent["variables"] == ["vlan"] and "name" not in sent
    shot(page, "bm-plans-conflict")
    assert st["errors"] == []


def test_a_saved_plan_shows_its_new_revision(context, flask_server):
    saved = dict(PLAN, revision=4)
    page, st = open_page(context, flask_server, "#automation/pxe/plans", put=[(saved, 200)])
    page.locator('.bmpl-card[data-plan="site-std"]').click()
    page.locator("#bm-plans .bmpl-save").click()
    expect(page.locator("#bm-plans .bmpl-msg")).to_contain_text("Saved, revision 4.")
    expect(page.locator("#bm-plans .bmpl-actions")).to_contain_text("Revision 4")


def test_hostile_template_name_is_escaped(context, flask_server):
    page, st = open_page(context, flask_server, "#automation/pxe/plans")
    rows = page.locator("#bmpl-tpl-table tbody tr")
    expect(rows).to_have_count(3)
    expect(rows.nth(2).locator(".bmpl-tpl-row-name")).to_have_text(EVIL)
    page.locator('.bmpl-card[data-plan="site-std"]').click()
    opts = page.locator("#bm-plans .bmpl-add-tpl option")
    expect(opts).to_have_count(1)
    expect(opts.first).to_have_text(EVIL)
    page.locator("#bm-plans .bmpl-add-tpl-go").click()
    expect(page.locator("#bm-plans .bmpl-tpl")).to_have_count(3)
    assert page.locator("#bm-plans img").count() == 0
    assert page.evaluate("window.__tpl240") is None


def test_deploy_window_blocks_then_launches_and_follows(context, flask_server):
    page, st = open_page(context, flask_server, "#automation/pxe/plans")
    page.locator('.bmpl-card[data-plan="site-std"]').click()
    page.locator("#bm-plans .bmpl-apply").click()
    win = page.locator("#fp-deploy-plan")
    expect(win).to_be_visible()
    win.locator('[name="cluster"]').select_option(C)
    # existant : aucun secret demandé
    expect(win.locator(".dpw-secrets")).to_be_hidden()
    launch = win.locator(".dpw-launch")
    expect(win.locator(".dpw-count")).to_have_text("1")
    expect(launch).to_be_disabled()
    assert "Blocked: A variable has no value." in launch.get_attribute("title")
    assert win.locator(".dpw-dry").is_disabled()
    win.locator('[data-dpw-tab="checks"]').click()
    groups = win.locator(".dpw-group")
    expect(groups.first).to_have_attribute("data-phase", "terraform")         # bloquant d'abord
    expect(groups.first).to_contain_text("Blocking")
    expect(groups.first).to_contain_text("templates[net].resources[0].spec.tf")
    shot(page, "bm-plans-deploy-checks")
    win.locator('[data-dpw-tab="params"]').click()
    win.locator('[data-dpw-var="vlan"]').fill("3999")
    expect(launch).to_be_enabled()
    assert st["check"][-1] == {"plan": "site-std", "variables": {"vlan": "3999"}}
    # decl-differs : la case n'apparaît que devant ce constat, et part avec la vérification
    win.locator('[data-dpw-tab="checks"]').click()
    upd = win.locator(".dpw-update-wrap")
    expect(upd).to_be_visible()
    assert upd.get_attribute("data-tip")
    upd.locator(".dpw-update").check()
    expect(launch).to_be_enabled()
    assert st["check"][-1]["update_differing"] is True
    win.locator('[data-dpw-tab="summary"]').click()
    expect(win.locator(".dpw-summary")).to_contain_text("about 24 min")
    expect(win.locator(".dpw-decls")).to_contain_text("reused")
    launch.click()
    assert st["deploy"][-1] == {"plan": "site-std", "variables": {"vlan": "3999"}, "dry_run": False,
                                "update_differing": True}
    follow = win.locator(".dpw-follow")
    expect(follow).to_be_visible()
    assert follow.locator(".dpw-action-link").get_attribute("href") == "#activity/action/abcdef012345"
    child = follow.locator('.dpw-step[data-step="tf-net"] a.dpw-child')
    expect(child).to_have_attribute("href", "#activity/action/0123456789ab")
    expect(follow.locator('.dpw-step[data-step="tf-net"]')).to_contain_text("Template net")
    expect(launch).to_be_disabled()
    shot(page, "bm-plans-deploy-follow")
    assert st["errors"] == []


def test_dry_run_sends_dry_run(context, flask_server):
    page, st = open_page(context, flask_server, "#automation/pxe/plans")
    page.evaluate(f"BMPlans.openDeploy({{ cluster: '{C}', plan: 'site-std' }})")
    win = page.locator("#fp-deploy-plan")
    win.locator('[data-dpw-var="vlan"]').fill("10")
    expect(win.locator(".dpw-dry")).to_be_enabled()
    win.locator(".dpw-dry").click()
    expect(win.locator(".dpw-msg")).to_contain_text("Dry run started")
    assert st["deploy"][-1]["dry_run"] is True


@pytest.mark.parametrize("role,enabled", [("operator", True), ("viewer", False)])
def test_host_page_button(context, flask_server, role, enabled):
    page, st = open_page(context, flask_server, f"#harvester/{C}/params/nodes", role=role)
    btn = page.locator("#btn-apply-plan")
    expect(btn).to_be_visible()
    if not enabled:
        expect(btn).to_be_disabled()
        assert "operator role" in btn.get_attribute("title")
        assert "operator role" in btn.get_attribute("data-tip")
        return
    expect(btn).to_be_enabled()
    assert btn.get_attribute("data-tip")
    btn.click()
    win = page.locator("#fp-deploy-plan")
    expect(win).to_be_visible()
    expect(win.locator('[name="cluster"]')).to_have_value(C)
    expect(win.locator(".dpw-secrets")).to_be_hidden()


DEPLOY_RUN = {"id": "a0000000d240", "action": f"deploy:{C}", "status": "error", "started_at": 1767000000,
              "ended_at": 1767000300, "dry_run": False,
              "result": {"plan": "site-std", "cluster": C, "variables": {"vlan": "3999"},
                         "install": {"kind": "batch", "profile": "rack-a"},
                         "phases": [{"id": "install", "status": "error"}]}}


def test_history_lists_a_deploy_run_and_resumes_it(context, flask_server):
    act = {"deploy": {"in_progress": [], "actions_done": [DEPLOY_RUN, dict(DEPLOY_RUN, id="a0000000d241", status="done")]}}
    secrets_needed = ({"error": "the installation phase is not done: its secrets are asked again",
                       "code": "secrets-required", "fields": ["token", "bmc_password"]}, 400)
    page, st = open_page(context, flask_server, "#automation/pxe/history", activity=act, resume=[secrets_needed])
    rows = page.locator("#bm-history tr.bm-history-row")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("Deployment plan")
    expect(page.locator(".bm-deploy-resume")).to_have_count(1)          # seulement le déroulé en erreur
    btn = rows.locator(".bm-deploy-resume")
    assert btn.get_attribute("data-tip")
    btn.click()
    win = page.locator("#fp-deploy-resume-a0000000d240")
    expect(win).to_be_visible()
    # installation de la chaîne non faite : secrets demandés, avec la phrase
    expect(win.locator(".dpw-secrets")).to_be_visible()
    expect(win.locator(".dpw-secrets")).to_contain_text("asked again on resume")
    expect(win.locator('[data-dpw-var="vlan"]')).to_have_value("3999")
    launch = win.locator(".dpw-launch")
    expect(launch).to_be_enabled()
    launch.click()                                                       # sans secret : 400
    expect(win.locator(".dpw-msg")).to_contain_text("type its secrets")
    win.locator('[data-dpw-secret="token"]').fill("TOK-240")
    win.locator('[data-dpw-secret="bmc_password"]').fill("BMC-240")
    launch.click()
    expect(win.locator(".dpw-follow")).to_be_visible()
    assert st["resume"][0] == {"action_id": "a0000000d240"}
    assert st["resume"][1] == {"action_id": "a0000000d240", "secrets": {"token": "TOK-240", "bmc_password": "BMC-240"}}
    assert win.locator(".dpw-follow .dpw-action-link").get_attribute("href") == "#activity/action/fedcba987654"


def test_resume_asks_the_typed_names(context, flask_server):
    act = {"deploy": {"in_progress": [], "actions_done": [dict(DEPLOY_RUN, status="interrupted")]}}
    refused = ({"error": "a system seems present", "fields": ["system_confirm"], "refused": ["198.51.100.7"],
                "machines": [{"bmc_host": "198.51.100.7", "state": "present", "confirm_name": "USE240",
                              "evidence": [{"code": "boots-from-disk", "name": "sles"}]}]}, 409)
    page, st = open_page(context, flask_server, "#automation/pxe/history", activity=act, resume=[refused])
    page.locator(".bm-deploy-resume").click()
    win = page.locator("#fp-deploy-resume-a0000000d240")
    win.locator('[data-dpw-secret="token"]').fill("T")
    win.locator('[data-dpw-secret="bmc_password"]').fill("B")
    win.locator(".dpw-launch").click()
    box = win.locator(".dpw-system")
    expect(box).to_be_visible()
    expect(box.locator(".bm-system-name")).to_have_text("USE240")
    box.locator('[data-dpw-sys="198.51.100.7"]').fill("USE240")
    win.locator(".dpw-launch").click()
    expect(win.locator(".dpw-follow")).to_be_visible()
    assert st["resume"][1]["system_confirm"] == {"198.51.100.7": "USE240"}


def test_template_editor_uses_the_terraform_forms_and_announces_removals(context, flask_server):
    """Le modèle s'écrit par les formulaires Terraform : un nombre ou une
    référence reçoivent une variable ; les retraits de secrets sont dits."""
    page, st = open_page(context, flask_server, "#automation/pxe/plans")
    sent = []

    def put(route, req):
        body = req.post_data_json
        sent.append(body)
        out = dict(body, name="web", revision=1, removed=["resources[0].spec.cloudinit[0].user_data"])
        return fulfill(route, out, 201)
    page.route(re.compile(r".*/api/baremetal/tf-templates/web$"), put)
    page.locator("#bm-plans .bmpl-tpl-new").click()
    win = page.locator("#fp-bm-tpl-new")
    expect(win).to_be_visible()
    win.locator('[name="name"]').fill("web")
    win.locator('[name="variables"]').fill("cpus, ns")
    win.locator(".bmpl-add-kind").select_option("vm")
    win.locator(".bmpl-add-res").click()
    res = win.locator(".bmpl-res")
    expect(res).to_have_count(1)
    res.locator('.tf-form [name="name"]').fill("web-{{cluster}}")
    res.locator('.tf-form [name="cpu"]').fill("{{cpus}}")
    res.locator('.tf-form [name="namespace"]').fill("{{ns}}")
    win.locator('button[type="submit"]').click()
    expect(win.locator(".bmpl-tpl-msg")).to_contain_text("Template saved, revision 1.")
    expect(win.locator(".bmpl-removed")).to_contain_text("resources[0].spec.cloudinit[0].user_data")
    body = sent[0]
    assert "revision" not in body and body["variables"] == ["cpus", "ns"]
    spec = body["resources"][0]["spec"]
    assert body["resources"][0]["kind"] == "vm"
    assert spec["name"] == "web-{{cluster}}" and spec["cpu"] == "{{cpus}}" and spec["namespace"] == "{{ns}}"
    expect(win.locator('[data-bmpl="name"]')).to_have_attribute("readonly", "")


@pytest.mark.parametrize("plan,missing", [("site-std", False), ("gone-plan", True)])
def test_batch_window_carries_the_plan_of_the_profile(context, flask_server, plan, missing):
    """Fenêtre de série : plan prérempli par le profil (absent signalé), ses
    variables, et l'action suivie est le déroulé `deploy:`."""
    import test_bm_profiles_180 as t180
    page, st = open_page(context, flask_server, "#automation/pxe/profiles")
    batches = []
    prof = dict(t180.PROFILE, plan=plan)

    def profiles(route, req):
        url, method = req.url.split("?")[0], req.method
        if url.endswith("/profiles") and method == "GET":
            return fulfill(route, t180.LIST)
        if url.endswith("/profiles/rack-a") and method == "GET":
            return fulfill(route, prof)
        if url.endswith("/system-check"):
            return fulfill(route, {"machines": [{"row": 1, "bmc_host": "198.51.100.1", "state": "absent",
                                                 "evidence": [], "confirm_name": "x", "source": "hpe-oem"}]})
        if url.endswith("/batch"):
            batches.append(req.post_data_json)
            return fulfill(route, {"action_id": "dep000000240", "cluster": "rack-a", "nodes": 1, "plan": plan}, 202)
        return fulfill(route, {"error": "unexpected"}, 500)
    page.route(re.compile(r".*/api/baremetal/profiles.*"), profiles)
    page.route("**/api/isos", lambda r, q: fulfill(r, {"isos": [{"name": "harvester.iso", "size": 1}], "disk_free": 1}))
    page.locator('#tab-automation [data-bm-tab="images"]').click()
    page.locator('#tab-automation [data-bm-tab="profiles"]').click()
    page.locator('#bmp-table tr[data-profile="rack-a"] .bmp-batch').click()
    win = page.locator('[id="fp-bm-batch-rack-a"]')
    pick = win.locator(".bmp-plan-pick")
    expect(pick.locator('[data-pp="plan"]')).to_have_value(plan)
    if missing:
        expect(pick.locator(".bmpl-pick-missing")).to_contain_text("The plan gone-plan does not exist")
        return
    pick.locator('[data-pp-var="vlan"]').fill("3999")
    row = win.locator(".bmp-nodes tbody tr").first
    row.locator('[data-col="bmc_host"]').fill("198.51.100.1")
    for k, v in {"hostname": "node1", "ip": "192.0.2.11", "storage_ip": "172.18.122.101"}.items():
        row.locator(f'[data-var="{k}"]').fill(v)
    win.locator('[name="cluster_name"]').fill("rack-a")
    win.locator('[name="vip"]').fill("192.0.2.100")
    win.locator('[name="token"]').fill("TOK")
    win.locator('[name="bmc_password"]').fill("BMC")
    win.locator('button[type="submit"]').click()
    expect(win.locator(".bmp-result a.dpw-action-link")).to_have_attribute("href", "#activity/action/dep000000240")
    assert batches[0]["plan"] == "site-std" and batches[0]["plan_variables"] == {"vlan": "3999"}
