"""2.10.0 (tâche 10) : External Secrets dans les plans (routes simulées).

* fenêtre Défaire : groupe « External Secrets » entre GitOps et les charts
  (ordre de deploy_undo.KINDS_ORDER), namespaces et note montrés, échappés ;
* fenêtre Déployer : constats de la phase `eso` nommés, sous le titre de la
  phase.
"""

import copy
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

import test_bm_plans_capi_250 as t250  # noqa: E402
import test_bm_plans_gitops_280 as t280  # noqa: E402

EVIL = '<img src=x onerror="window.__eso2100=1">'
lists = t280.lists


def test_undo_window_shows_the_eso_group_in_removal_order(context, flask_server, monkeypatch):
    import test_deploy_undo_270 as t270
    items = [
        {"id": "chart-a/a/flux", "kind": "chart", "ref": "a/a/flux", "phase": "artefacts", "run": t270.RUN["id"],
         "proven": True, "default": True, "note": "", "selected": True,
         "details": {"present": True, "delete": [], "kept": [], "refused": []}},
        {"id": "eso-a/a", "kind": "eso", "ref": "a/a", "phase": "eso", "run": t270.RUN["id"],
         "proven": True, "default": True, "note": "", "selected": True,
         "details": {"present": True, "namespaces": ["equipe-a", EVIL], "externalsecrets": 2, "note": EVIL}},
        {"id": "gitops-a/a/apps", "kind": "gitops", "ref": "a/a/apps", "phase": "gitops", "run": t270.RUN["id"],
         "proven": True, "default": True, "note": "", "selected": True,
         "details": {"present": True, "tool": "flux", "name": "apps"}},
    ]
    monkeypatch.setattr(t270, "ITEMS", items)
    page, st = t270.open_page(context, flask_server)
    t270.row(page, t270.RUN["id"]).locator(".bm-deploy-undo").click()
    win = page.locator(f'#fp-deploy-undo-{t270.RUN["id"]}')
    groups = win.locator(".udw-group")
    expect(groups).to_have_count(3)
    assert [g.get_attribute("data-kind") for g in groups.all()] == ["gitops", "eso", "chart"]
    eso = win.locator('.udw-group[data-kind="eso"]')
    expect(eso).to_contain_text("External Secrets")
    expect(win.locator(".udw-eso")).to_contain_text("equipe-a")
    expect(win.locator(".udw-eso")).to_contain_text("2 ExternalSecret")
    expect(win.locator(".udw-eso-note")).to_have_text(EVIL)
    assert page.evaluate("window.__eso2100") is None and win.locator("img").count() == 0
    assert st["errors"] == []


def test_deploy_window_names_the_eso_findings_under_their_phase(context, flask_server, lists, monkeypatch):
    plan = copy.deepcopy(t250.PLAN)
    monkeypatch.setattr(t250, "PLAN", plan)
    check = {"findings": [
        {"code": "eso-chart-not-in-plan", "level": "warning", "phase": "eso", "detail": EVIL},
        {"code": "eso-needs-openbao", "level": "blocking", "phase": "eso", "detail": "a/a"}],
        "summary": {"declarations": [], "estimate_minutes": 4}, "blocking": True}
    page, st = t280.open_page(context, flask_server, check=check)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    page.locator("#bm-plans .bmpl-apply").click()
    win = page.locator("#fp-deploy-plan")
    win.locator('[name="cluster"]').select_option(t250.C)
    win.locator('[data-dpw-tab="checks"]').click()
    group = win.locator('.dpw-group[data-phase="eso"]')
    expect(group.locator(".dpw-group-title")).to_have_text("External Secrets", timeout=5000)
    expect(group.locator('[data-code="eso-needs-openbao"]')).to_contain_text("need OpenBao")
    expect(group.locator('[data-code="eso-chart-not-in-plan"]')).to_contain_text("chart item eso")
    expect(group).to_contain_text(EVIL)
    assert page.evaluate("window.__eso2100") is None
    expect(win.locator(".dpw-launch")).to_be_disabled()
    assert st["errors"] == []
