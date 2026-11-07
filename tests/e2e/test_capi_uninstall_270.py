"""2.7.0 (décision 215) : fenêtre « Désinstaller » d'une pile Turtles, sur routes simulées.

* le bouton est actif sur une pile Turtles et ouvre la fenêtre ;
* refus listés, bouton grisé même avec le nom ;
* sans refus : grisé sans le nom, actif une fois le nom retapé ; images
  décochées par défaut ; ce qui est envoyé ;
* noms hostiles échappés ;
* fenêtre fermée avant le retour du plan : rien ne s'affiche ni ne part.
"""

import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_auto_tabs_220 as t220  # noqa: E402

HOSTILE = '<img src=x onerror="window.__pwned=1">'
fulfill = t220.fulfill
STACK = dict(t220.STACK_READY, bundle={"active": "capi-bundle.tar.gz", "turtles": True}, providers=[
    {"type": "addon", "name": "helm", "version": "v0.6.4", "phase": "Ready", "ready": True,
     "managed_by_console": True, "namespace": "caaph-system"}])
PLAN = {"providers": [{"key": "helm", "namespace": "caaph-system", "type": "addon", "name": "helm", "version": "v0.6.4"},
                      {"key": "harvester", "namespace": "caphv-system", "type": "infrastructure",
                       "name": "harvester", "version": "v0.10.1"}],
        "configmaps": ["caaph-system/v0.6.4"], "shim": True, "images": [],
        "refusals": [], "kept": [{"provider": "turtles", "namespace": None, "reason": "turtles"},
                                 {"provider": "core/cluster-api", "namespace": "cattle-capi-system", "reason": "core"}],
        "blocked": False}


def open_window(context, flask_server, plan, calls, held=None):
    page = t220.open_auto(context, flask_server, "#automation/capi/installation", stack=STACK)

    def plan_route(route, req):
        if held is not None:
            held.append(route)                   # répondu plus tard par le test
            return
        fulfill(route, plan)
    page.route(re.compile(r".*/api/capi/harv-fake/uninstall/plan$"), plan_route)
    page.route(re.compile(r".*/api/capi/harv-fake/uninstall$"),
               lambda r, q: (calls.append(json.loads(q.post_data or "{}")),
                             fulfill(r, {"action_id": "u1u1u1u1u1u1", "mode": "turtles"}, 201)))
    btn = page.locator("#btn-capi-uninstall")
    expect(btn).to_be_enabled()
    assert btn.get_attribute("data-tip")
    btn.click()
    return page


def test_refusals_are_listed_and_keep_the_button_greyed(context, flask_server):
    plan = dict(PLAN, blocked=True, refusals=[
        {"code": "capi-clusters-present", "level": "block", "facts": {"clusters": "web/web", "count": 1}},
        {"code": "capi-plan-running", "level": "block", "facts": {"plan": "site-a", "run": "r1"}}])
    calls = []
    page = open_window(context, flask_server, plan, calls)
    m = page.locator("#capi-uninstall-modal")
    expect(m.locator('[data-code="capi-clusters-present"]')).to_contain_text("web/web")
    expect(m.locator('[data-code="capi-plan-running"]')).to_contain_text("site-a")
    expect(m.locator('[data-x="kept"]')).to_contain_text("core/cluster-api")
    m.locator('[data-x="name"]').fill("harv-fake")
    expect(m.locator('[data-x="go"]')).to_be_disabled()
    assert calls == []


def test_greyed_without_the_name_then_sent_with_images_unchecked(context, flask_server):
    calls = []
    page = open_window(context, flask_server, PLAN, calls)
    m = page.locator("#capi-uninstall-modal")
    expect(m.locator('[data-x="providers"] li')).to_have_count(2)
    expect(m.locator('[data-x="providers"] li').first).to_contain_text("addon/helm")
    go = m.locator('[data-x="go"]')
    expect(go).to_be_disabled()
    assert not m.locator('[data-x="images"]').is_checked()
    for ctl in ('[data-x="go"]', '[data-x="name"]', '.apply-dry', '.btn-close'):
        assert m.locator(ctl).first.get_attribute("data-tip"), ctl
    m.locator('[data-x="name"]').fill("harv-fak")
    expect(go).to_be_disabled()
    m.locator('[data-x="name"]').fill("harv-fake")
    expect(go).to_be_enabled()
    go.click()
    expect(page.locator("#capi-uninstall-modal")).to_have_count(0)
    assert calls == [{"confirm": "harv-fake", "remove_images": False}]
    expect(page.locator("#capi-install-result")).to_contain_text("u1u1u1u1u1u1")


def test_hostile_names_are_escaped(context, flask_server):
    plan = dict(PLAN, providers=[dict(PLAN["providers"][0], name=HOSTILE, namespace=HOSTILE)],
                kept=[{"provider": HOSTILE, "namespace": None, "reason": HOSTILE}], blocked=True,
                refusals=[{"code": "caaph-services-present", "level": "block",
                           "facts": {"services": HOSTILE, "count": 1}},
                          {"code": HOSTILE, "level": "block", "facts": {}}])
    page = open_window(context, flask_server, plan, [])
    m = page.locator("#capi-uninstall-modal")
    expect(m.locator('[data-code="caaph-services-present"]')).to_contain_text(HOSTILE)
    assert m.locator("img").count() == 0
    assert page.evaluate("() => window.__pwned") is None


def test_closing_before_the_plan_comes_back_leaves_nothing(context, flask_server):
    calls, held = [], []
    page = open_window(context, flask_server, PLAN, calls, held=held)
    page.wait_for_function("() => document.querySelector('#capi-uninstall-modal [data-x=\"plan\"]')"
                           ".textContent.includes('Reading')")
    page.locator('#capi-uninstall-modal [data-x="close"]').first.click()
    expect(page.locator("#capi-uninstall-modal")).to_have_count(0)
    assert len(held) == 1
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps(PLAN))
    page.wait_for_timeout(500)
    expect(page.locator("#capi-uninstall-modal")).to_have_count(0)
    assert page.evaluate("() => document.body.innerHTML.includes('caphv-system')") is False
    assert calls == []
