"""v1.54.0 : les déclarations Terraform gardées par la console, dans le
navigateur.

Demandé par l'exploitant : pouvoir renommer une déclaration, et les garder
côté console (partagées, sauvegardées) plutôt que dans chaque navigateur.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

LEGACY = {"schema_version": 1, "active_declaration_id": "a1a1a1a1a1a1", "declarations": [
    {"id": "a1a1a1a1a1a1", "name": "from-the-browser", "cluster": "harv-fake",
     "resources": [{"id": "b2b2b2b2b2b2", "kind": "ssh_key",
                    "spec": {"name": "ops", "namespace": "default", "public_key": "ssh-ed25519 AAAA x"}}]}]}


def server_decls(page):
    return page.evaluate("fetch('/api/tf-declarations?cluster=harv-fake').then(r => r.json())")["declarations"]


def clean(page):
    page.evaluate("""async () => {
      const d = await fetch('/api/tf-declarations').then(r => r.json());
      for (const x of d.declarations || []) await fetch('/api/tf-declarations/' + x.id, { method: 'DELETE' });
      localStorage.removeItem('harvester_ops_tf_declarations');
      localStorage.removeItem('harvester_ops_tf_declarations_imported');
    }""")


def open_decls(page):
    page.evaluate("""() => { localStorage.setItem('harvester_ops_current_tab', 'automation');
      localStorage.setItem('harvester_ops_automation_subtab', 'terraform');
      localStorage.setItem('harvester_ops_tf_subtab', 'decls'); }""")
    page.reload()
    page.wait_for_function("window.TFDecl && window.TF")
    page.evaluate("window.TFDecl.ready")
    page.evaluate("document.querySelector('.tab-child[data-subtab=terraform]')?.click()")
    page.wait_for_selector("#tf-decl-list", timeout=8000)


def test_declarations_left_in_the_browser_move_to_the_console(page):
    clean(page)
    page.evaluate(f"localStorage.setItem('harvester_ops_tf_declarations', {json.dumps(json.dumps(LEGACY))})")
    open_decls(page)
    expect(page.locator(".tf-decl-item__name", has_text="from-the-browser")).to_be_visible(timeout=8000)
    decls = server_decls(page)
    assert [d["name"] for d in decls] == ["from-the-browser"]
    assert decls[0]["id"] == "a1a1a1a1a1a1" and decls[0]["resources"][0]["kind"] == "ssh_key"
    assert page.evaluate("localStorage.getItem('harvester_ops_tf_declarations')") is None
    assert page.evaluate("localStorage.getItem('harvester_ops_tf_declarations_imported')")
    clean(page)


def test_rename_in_place_and_duplicates_refused(page):
    clean(page)
    page.evaluate("""async () => {
      for (const n of ['web', 'db'])
        await fetch('/api/tf-declarations', { method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ cluster: 'harv-fake', name: n }) });
    }""")
    open_decls(page)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    row = page.locator(".tf-decl-item", has_text="web")
    btn = row.locator(".tf-decl-rename-btn")
    assert btn.get_attribute("data-tip")
    btn.click()
    field = page.locator("input.tf-decl-rename")
    field.fill("web-2026")
    field.press("Enter")
    expect(page.locator(".tf-decl-item__name", has_text="web-2026")).to_be_visible(timeout=5000)
    assert sorted(d["name"] for d in server_decls(page)) == ["db", "web-2026"]
    # un nom déjà pris est refusé par la console, et dit
    page.locator(".tf-decl-item", has_text="db").locator(".tf-decl-rename-btn").click()
    page.locator("input.tf-decl-rename").fill("WEB-2026")
    page.locator("input.tf-decl-rename").press("Enter")
    page.wait_for_timeout(800)
    assert any("existe déjà" in m or "already exists" in m for m in dialogs), dialogs
    assert sorted(d["name"] for d in server_decls(page)) == ["db", "web-2026"]
    clean(page)


def test_changes_are_saved_and_a_conflict_is_told(page):
    clean(page)
    open_decls(page)
    decl_id = page.evaluate("window.TFDecl.createAsync('shared', 'harv-fake').then(d => d.id)")
    page.evaluate(f"window.TFDecl.describe('{decl_id}', 'first')")
    page.evaluate(f"window.TFDecl.flush('{decl_id}')")
    page.wait_for_timeout(300)
    got = [d for d in server_decls(page) if d["id"] == decl_id][0]
    assert got["description"] == "first"
    # quelqu'un d'autre passe entre-temps
    page.evaluate(f"""fetch('/api/tf-declarations/{decl_id}', {{ method: 'PUT',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ revision: {got['revision']}, description: 'from a colleague' }}) }})""")
    errors = page.evaluate(f"""new Promise(res => {{
      const off = window.TFDecl.onError(e => {{ off(); res(e.code || e.message); }});
      window.TFDecl.describe('{decl_id}', 'mine');
      window.TFDecl.flush('{decl_id}');
    }})""")
    assert errors == "conflict"
    assert page.evaluate(f"window.TFDecl.get('{decl_id}').description") == "from a colleague"
    clean(page)
