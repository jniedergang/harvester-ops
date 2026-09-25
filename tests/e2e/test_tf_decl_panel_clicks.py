"""v1.52.1 : un clic dans la fenêtre d'une déclaration part une seule fois.

Vu à l'audit du 26/09/2026 : chaque réaffichage de la fenêtre reposait ses
écouteurs sur le même élément. Après quelques réaffichages, un seul « + Add »
créait cinq ressources et un Dry-run envoyait 81 requêtes.
"""

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


def test_one_click_is_one_action_after_redraws(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.removeItem('harvester_ops_tf_declarations');")
    page = context.new_page()
    page.on("dialog", lambda d: d.accept())
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.TFDecl && window.TFDeclPanel && window.FloatingPanels")
    decl_id = page.evaluate("() => TFDecl.create('clicks', 'harv-fake').id")
    page.evaluate("(id) => TFDeclPanel.open(id)", decl_id)
    add = page.locator(".tf-dp-add-btn").first
    expect(add).to_be_visible(timeout=5000)
    # des réaffichages, comme en fait chaque sélection ou enregistrement
    for _ in range(5):
        page.evaluate("(id) => TFDeclPanel.refresh(id)", decl_id)
    add.click()
    page.wait_for_timeout(300)
    count = page.evaluate("(id) => TFDecl.get(id).resources.length", decl_id)
    assert count == 1, f"one click added {count} resources"
