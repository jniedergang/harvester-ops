"""v1.55.0 : la vue des déclarations Terraform, dans un navigateur.

Demande de l'exploitant : une interface améliorée pour l'onglet Terraform,
où l'on puisse renommer les déclarations. Les déclarations viennent du vrai
magasin du serveur de test ; plans, applications, destructions et
historique sont simulés (pas de cluster ici, l'essai réel est fait sur harv1).
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJdDpkg ops@lab"


def fulfill(route, body, status=200, ctype="application/json"):
    route.fulfill(status=status, content_type=ctype,
                  body=body if isinstance(body, str) else json.dumps(body))


def sse(events):
    return "".join(f"event: {t}\ndata: {json.dumps(d)}\n\n" for t, d in events)


def open_view(page, lang="fr"):
    page.evaluate(f"""async () => {{
      localStorage.setItem('harvester_ops_language', '{lang}');
      localStorage.setItem('harvester_ops_current_tab', 'automation');
      localStorage.setItem('harvester_ops_automation_subtab', 'terraform');
      localStorage.setItem('harvester_ops_tf_subtab', 'decls');
      localStorage.removeItem('harvester_ops_tfd_tab');
      localStorage.removeItem('harvester_ops_tf_active_decl');
      const d = await fetch('/api/tf-declarations').then(r => r.json());
      for (const x of d.declarations || []) await fetch('/api/tf-declarations/' + x.id, {{ method: 'DELETE' }});
    }}""")
    page.reload()
    page.wait_for_function("window.TFDecl && window.TFDeclView && window.TF")
    page.evaluate("window.TFDecl.ready")
    page.evaluate("document.querySelector('.tab-child[data-subtab=terraform]')?.click()")
    page.wait_for_selector("#tf-decls-view .tfd", timeout=8000)


def create(page, name, desc=""):
    page.locator(".tfd-new").first.click()
    m = page.locator(".tfd-modal")
    m.locator("[data-x='name']").fill(name)
    if desc:
        m.locator("[data-x='desc']").fill(desc)
    m.locator("[data-x='create']").click()


def seed(page, name, resources, **extra):
    """Une déclaration posée directement dans le magasin."""
    return page.evaluate("""async ([name, resources]) => {
      const d = await fetch('/api/tf-declarations', { method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ cluster: 'harv-fake', name, resources }) }).then(r => r.json());
      await window.TFDecl.load();
      window.TFDecl.setActive(d.id);
      window.TFDeclView.render();
      return d;
    }""", [name, resources])


KEYRES = {"id": "0a0a0a0a0a0a", "kind": "ssh_key", "spec": {"name": "ops", "namespace": "default", "public_key": KEY}}


def test_empty_view_create_and_duplicate(page):
    open_view(page)
    expect(page.locator(".tfd-placeholder")).to_contain_text("Une déclaration réunit")
    create(page, "web-prod", "Front web")
    expect(page.locator(".tfd-name")).to_have_text("web-prod", timeout=5000)
    expect(page.locator(".tfd-card.is-active")).to_contain_text("jamais appliquée")
    expect(page.locator(".tfd-desc")).to_have_value("Front web")
    # un nom déjà pris est refusé dans la fenêtre, sans la fermer
    create(page, "WEB-PROD")
    expect(page.locator(".tfd-modal [data-x='err']")).to_contain_text("existe déjà", timeout=5000)
    page.locator(".tfd-modal [data-x='dismiss']").click()
    # chaque contrôle de la vue s'explique
    for b in page.locator("#tf-decls-view button, #tf-decls-view input").all():
        assert b.get_attribute("data-tip") or b.get_attribute("aria-label") or b.get_attribute("role") == "tab" \
            or "tfd-card" in (b.get_attribute("class") or ""), b.inner_html()[:80]


def test_one_click_adds_one_resource_and_opens_its_form(page):
    """Audit D2 : un clic ajoutait plusieurs ressources après quelques
    réaffichages. La vue pose ses écouteurs une fois."""
    open_view(page)
    d = seed(page, "clicks", [])
    for _ in range(5):
        page.evaluate("window.TFDeclView.render()")
    page.click('.tfd-add-kind[data-kind="ssh_key"]')
    expect(page.locator(".tfd-edit .tfd-sec")).to_have_count(1, timeout=5000)
    assert page.evaluate(f"window.TFDecl.get('{d['id']}').resources.length") == 1
    # libellés lisibles, nom Terraform en petit, bulles partout
    form = page.locator(".tfd-sec")
    expect(form.locator(".tf-label").first).to_contain_text("Nom")
    expect(form.locator(".tf-argname").first).to_have_text("name")
    assert all(l.get_attribute("data-tip") for l in form.locator(".tf-label").all())
    # contrôle pendant la saisie, puis enregistrement
    expect(page.locator("[data-x='check']")).to_contain_text("manque")
    form.locator('[name="name"]').fill("ops")
    form.locator('[name="public_key"]').fill(KEY)
    form.locator('[name="public_key"]').dispatch_event("input")
    expect(page.locator("[data-x='check']")).not_to_contain_text("manque")
    page.click(".tfd-save")
    card = page.locator(".tfd-res")
    expect(card).to_contain_text("Clé SSH ops")
    expect(card).to_contain_text("à créer")
    expect(card.locator(".tfd-addr")).to_have_text("harvester_ssh_key.ops")
    page.evaluate(f"window.TFDecl.flush('{d['id']}')")
    page.wait_for_timeout(400)
    saved = page.evaluate(f"fetch('/api/tf-declarations/{d['id']}').then(r => r.json())")
    assert saved["resources"][0]["spec"]["name"] == "ops"


def test_rename_in_place_and_code_tab(page):
    open_view(page)
    d = seed(page, "before", [KEYRES])
    page.click(".tfd-rename")
    page.locator(".tfd-name-input").fill("after")
    page.locator(".tfd-name-input").press("Enter")
    expect(page.locator(".tfd-name")).to_have_text("after")
    page.wait_for_timeout(500)
    assert page.evaluate(f"fetch('/api/tf-declarations/{d['id']}').then(r => r.json())")["name"] == "after"
    page.click('[data-tfd-tab="code"]')
    expect(page.locator(".tfd-file", has_text="ops.tf")).to_contain_text('resource "harvester_ssh_key" "ops"', timeout=5000)
    expect(page.locator(".tfd-file", has_text="_providers.tf")).to_be_visible()


def test_the_plan_reads_setting_by_setting_and_applies_that_plan(page):
    open_view(page)
    d = seed(page, "planned", [KEYRES])
    calls = []

    def on_apply(route, request):
        body = json.loads(request.post_data or "{}")
        calls.append(body)
        fulfill(route, {"action_id": "tfplan0000001" if body.get("dry_run") else "tfapply000001",
                        "plan_hash": "abc"}, 201)

    plan = {"counts": {"create": 0, "update": 1, "replace": 0, "delete": 1, "noop": 0}, "changes": [
        {"address": "harvester_ssh_key.ops", "type": "harvester_ssh_key", "name": "ops", "action": "update",
         "fields": [{"path": "public_key", "before": "ssh-ed25519 OLD", "after": "ssh-ed25519 NEW"}]},
        {"address": "harvester_ssh_key.gone", "type": "harvester_ssh_key", "name": "gone", "action": "delete",
         "fields": []}]}
    page.route("**/api/terraform/harv-fake/apply_declaration", on_apply)
    for aid, result in (("tfplan0000001", {"plan": plan, "plan_hash": "abc", "declaration_id": d["id"]}),
                        ("tfapply000001", {"plan": plan, "declaration_id": d["id"]})):
        page.route(f"**/api/stream/{aid}", lambda r, q: fulfill(r, sse([
            ("step", {"step_id": "plan", "status": "running", "message": "plan"}),
            ("log", {"message": "Plan: 0 to add, 1 to change, 1 to destroy."}),
            ("end", {"status": "done"})]), ctype="text/event-stream"))
        page.route(f"**/api/action/{aid}", lambda r, q, res=result: fulfill(r, {"status": "done", "result": res}))
    expect(page.locator(".tfd-apply")).to_be_disabled()             # pas de plan : rien à appliquer
    page.click(".tfd-plan")
    modal = page.locator(".tfd-modal")
    expect(modal.locator('[data-count="update"]')).to_have_text("1 à modifier", timeout=5000)
    expect(modal.locator('[data-count="delete"]')).to_have_text("1 à détruire")
    change = modal.locator('.tfd-change[data-address="harvester_ssh_key.ops"]')
    expect(change).to_contain_text("sera modifiée sur place")
    expect(change.locator(".tfd-before")).to_have_text("ssh-ed25519 OLD")
    expect(change.locator(".tfd-after")).to_have_text("ssh-ed25519 NEW")
    expect(modal.locator('.tfd-change[data-address="harvester_ssh_key.gone"]')).to_contain_text("n'est plus dans la déclaration")
    assert calls[-1] == {"declaration": {"id": d["id"]}, "dry_run": True}
    modal.locator("[data-x='apply']").click()
    expect(modal).to_contain_text("appliquée", timeout=5000)
    expect(modal).to_contain_text("1 modifiée(s), 1 détruite(s)")
    assert calls[-1] == {"declaration": {"id": d["id"]}, "dry_run": False, "plan_hash": "abc"}


def test_a_failed_plan_says_why(page):
    open_view(page)
    seed(page, "broken", [KEYRES])
    page.route("**/api/terraform/harv-fake/apply_declaration",
               lambda r, q: fulfill(r, {"action_id": "tfplan0000002", "plan_hash": "x"}, 201))
    page.route("**/api/stream/tfplan0000002", lambda r, q: fulfill(r, sse([("end", {"status": "error"})]),
                                                                  ctype="text/event-stream"))
    page.route("**/api/action/tfplan0000002",
               lambda r, q: fulfill(r, {"status": "error", "error_summary": "plan: provider refused noCloud"}))
    page.click(".tfd-plan")
    expect(page.locator(".tfd-modal")).to_contain_text("provider refused noCloud", timeout=5000)


def test_destroy_asks_the_name_and_deleting_a_deployed_declaration_is_off(page, test_config):
    open_view(page)
    d = seed(page, "deployed", [KEYRES])
    ws = test_config["root"] / "terraform" / "harv-fake" / "decls" / d["id"]
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "terraform.tfstate").write_text(json.dumps({"version": 4, "resources": [
        {"mode": "managed", "type": "harvester_ssh_key", "name": "ops", "instances": []}]}))
    page.evaluate(f"window.TFDecl.refresh('{d['id']}')")
    card = page.locator(".tfd-res")
    expect(card).to_contain_text("déployée", timeout=5000)
    expect(page.locator(".tfd-delete")).to_be_disabled()
    assert "Détruisez d'abord" in page.locator(".tfd-delete").get_attribute("data-tip")
    destroyed = []
    page.route("**/api/terraform/harv-fake/destroy_declaration",
               lambda r, q: (destroyed.append(json.loads(q.post_data)), fulfill(r, {"action_id": "tfdestr00001"}, 201)))
    page.route("**/api/stream/tfdestr00001", lambda r, q: fulfill(r, sse([("end", {"status": "done"})]),
                                                                 ctype="text/event-stream"))
    page.route("**/api/action/tfdestr00001", lambda r, q: fulfill(r, {"status": "done", "result": {}}))
    page.click(".tfd-destroy")
    confirm = page.locator(".tf-confirm-overlay")
    expect(confirm.locator(".tf-confirm-required")).to_have_text("deployed")
    expect(confirm.locator(".tf-confirm-go")).to_be_disabled()
    confirm.locator(".tf-confirm-input").fill("deployed")
    confirm.locator(".tf-confirm-go").click()
    expect(page.locator(".tfd-modal")).to_contain_text("détruite sur le cluster", timeout=5000)
    assert destroyed == [{"declaration": {"id": d["id"]}, "dry_run": False}]
    (ws / "terraform.tfstate").unlink()


def test_history_lists_the_runs(page):
    open_view(page)
    d = seed(page, "with-history", [KEYRES])
    page.route(f"**/api/tf-declarations/{d['id']}/history", lambda r, q: fulfill(r, {"runs": [
        {"id": "a", "status": "done", "mode": "apply", "by": "ju", "started_at": 1790000000,
         "counts": {"create": 2}},
        {"id": "b", "status": "error", "mode": "plan", "by": "ops-2", "started_at": 1789990000,
         "error_summary": "plan: timeout"}]}))
    page.click('[data-tfd-tab="history"]')
    rows = page.locator(".tfd-history tbody tr")
    expect(rows).to_have_count(2, timeout=5000)
    expect(rows.nth(0)).to_contain_text("Application")
    expect(rows.nth(0)).to_contain_text("2 à créer")
    expect(rows.nth(1)).to_contain_text("plan: timeout")


def test_a_shared_resource_is_adopted_into_a_declaration(page):
    """Une ressource de l'espace partagé (d'avant la v1.54) passe dans une
    déclaration depuis la vue en direct ; la vue s'ouvre sur son formulaire."""
    open_view(page)
    page.route("**/api/terraform/harv-fake/state", lambda r, q: fulfill(r, {
        "initialized": True, "resources": ["harvester_ssh_key.legacy"], "resource_count": 1,
        "resources_detail": [{"address": "harvester_ssh_key.legacy", "local_name": "legacy",
                              "has_sidecar": True, "kind": "ssh_key", "workspace": "shared",
                              "declaration_id": None, "declaration_name": "old-bundle"}]}))
    page.route("**/api/terraform/harv-fake/sidecar/legacy", lambda r, q: fulfill(r, {
        "kind": "ssh_key", "spec": {"name": "legacy", "namespace": "default", "public_key": KEY},
        "declaration_name": "old-bundle"}))
    page.evaluate("window.TF.refresh()")
    page.click('#tf-status-body .sub-tab[data-tf-tab="live"]')
    row = page.locator("tr", has_text="harvester_ssh_key.legacy")
    expect(row).to_contain_text("espace partagé", timeout=5000)
    row.locator(".tf-edit-resource").click()
    expect(page.locator(".tfd-name")).to_have_text("old-bundle", timeout=5000)
    expect(page.locator(".tfd-edit .tfd-sec [name='name']")).to_have_value("legacy")


def test_the_view_speaks_english_too(page):
    open_view(page, lang="en")
    seed(page, "english", [KEYRES])
    expect(page.locator(".tfd-plan")).to_contain_text("Preview the plan")
    expect(page.locator('[data-tfd-tab="history"]')).to_have_text("History")
    expect(page.locator(".tfd-res")).to_contain_text("to create")
