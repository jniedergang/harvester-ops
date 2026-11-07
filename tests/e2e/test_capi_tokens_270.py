"""2.7.0 (tâche 6, interface de la tâche 8) : comptes d'automatisation d'un
cluster enfant et jeton court, sur routes simulées.

* panneau de l'onglet Clusters de Cluster API : comptes déclarés par la
  console, noms hostiles échappés ;
* « Émettre un jeton » : durée, jeton montré UNE fois avec Copier et la
  validité (plus une minute au plus), absent de la page après fermeture,
  jamais réaffiché ;
* refus du serveur (droit sur le Secret) dit dans la fenêtre ;
* lecteur : bouton grisé avec la raison.
"""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402

HOSTILE = '<img src=x onerror="window.__pwned=1">'
TOKEN = "eyJhbGciOiJSUzI1NiJ9.E2E-TOKEN-314159.sig"
APPLIED = {"charts": [], "manifests": [], "clusters": ["web/web"], "accounts": [
    {"cluster": "web/web", "namespace": "equipe-a", "name": "ci", "role": "edit"},
    {"cluster": "web/web", "namespace": "equipe-a", "name": HOSTILE, "role": HOSTILE}]}


def fulfill(route, body, status=200, headers=None):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body), headers=headers or {})


@pytest.fixture
def ui(context, flask_server):
    def make(role="operator", token=None):
        context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
        page = context.new_page()
        asked = []

        def on_token(route, request):
            asked.append(json.loads(request.post_data or "{}"))
            if token is not None:
                fulfill(route, *token)
            else:
                fulfill(route, {"token": TOKEN, "expires_at": "2026-11-07T15:00:00Z", "seconds": asked[-1]["seconds"],
                                "action_id": "tok000000001"}, headers={"Cache-Control": "no-store"})
        page.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(r, {"role": role, "roles_active": True,
                                                                           "user": "u", "auth": "local"}))
        page.route(re.compile(r".*/api/capi/harv-fake/artefacts/applied.*"), lambda r, q: fulfill(r, APPLIED))
        page.route("**/api/capi/harv-fake/token", on_token)
        goto(page, flask_server["base_url"], "#automation/capi")
        page.wait_for_function("window.CapiTokens && window.i18n")
        page.evaluate("() => CapiTokens.refresh(true)")
        expect(page.locator("#capi-tokens-body tr[data-tok-row]")).to_have_count(2, timeout=8000)
        return page, asked
    return make


def page_text(page):
    """Tout ce que la page garde : balisage et valeurs des champs."""
    return page.evaluate("""() => document.documentElement.outerHTML
        + [...document.querySelectorAll('input, textarea')].map(e => e.value).join('|')""")


def test_the_token_is_shown_once_then_gone_after_close(ui):
    page, asked = ui()
    btn = page.locator('#capi-tokens-body [data-account="equipe-a/ci"]')
    expect(btn).to_be_enabled()
    assert btn.get_attribute("title")
    btn.click()
    m = page.locator(".tok-modal")
    m.locator('[data-x="seconds"]').fill("600")
    m.locator("[data-tok-go]").click()
    val = m.locator('[data-x="token"]')
    expect(val).to_have_value(TOKEN)
    expect(m).to_contain_text("Valid 600 s, plus up to one minute")
    expect(m).to_contain_text("Shown once")
    assert m.locator("[data-tok-copy]").get_attribute("title")
    expect(m.locator("[data-tok-go]")).to_have_count(0)                 # jamais réaffiché dans cette fenêtre
    assert asked == [{"cluster": "web/web", "account": "equipe-a/ci", "seconds": 600}]
    m.locator("[data-tok-close]").first.click()
    expect(page.locator(".tok-modal")).to_have_count(0)
    assert TOKEN not in page_text(page)
    # une nouvelle fenêtre ne montre rien tant qu'on n'a pas redemandé
    btn.click()
    expect(page.locator('.tok-modal [data-x="token"]')).to_have_count(0)
    assert TOKEN not in page_text(page)


@pytest.mark.parametrize("seconds", ["599", "86401"])
def test_a_duration_out_of_bounds_is_refused_in_the_page(ui, seconds):
    page, asked = ui()
    page.locator('#capi-tokens-body [data-account="equipe-a/ci"]').click()
    m = page.locator(".tok-modal")
    m.locator('[data-x="seconds"]').fill(seconds)
    m.locator("[data-tok-go]").click()
    expect(m.locator('[data-x="feedback"]')).to_contain_text("600 to 86400")
    assert asked == []


def test_a_refusal_of_the_server_is_said(ui):
    page, _ = ui(token=({"error": "x", "code": "child-admin-required"}, 403))
    page.locator('#capi-tokens-body [data-account="equipe-a/ci"]').click()
    m = page.locator(".tok-modal")
    m.locator("[data-tok-go]").click()
    expect(m.locator('[data-x="feedback"]')).to_contain_text("you cannot read the kubeconfig Secret of web/web")
    expect(m.locator('[data-x="token"]')).to_have_count(0)


def test_hostile_names_are_escaped(ui):
    page, _ = ui()
    body = page.locator("#capi-tokens-body")
    expect(body).to_contain_text(HOSTILE)
    assert body.locator("img").count() == 0
    page.locator("#capi-tokens-body [data-tok-issue]").nth(1).click()
    expect(page.locator(".tok-modal .modal-subtitle")).to_contain_text(HOSTILE)
    assert page.locator(".tok-modal img").count() == 0
    assert page.evaluate("() => window.__pwned") is None


def test_a_viewer_sees_the_button_greyed_with_the_reason(ui):
    page, _ = ui(role="viewer")
    btn = page.locator('#capi-tokens-body [data-account="equipe-a/ci"]')
    expect(btn).to_be_disabled()
    assert "operator role" in btn.get_attribute("title")
