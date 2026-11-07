"""2.6.0 (tâche 12) : phase Registre des plans, dans l'interface (routes
simulées).

* carte Registre active (badge « à venir » retiré ; Artefacts le garde,
  « coming in 2.7 ») ; registre d'amorçage et registre du client ajoutés,
  publication d'un paquet importé ; registres d'une entrée de cluster ;
  le tout envoyé tel quel à l'enregistrement ;
* fenêtre Déployer : constats Registre groupés, Résumé avec les registres
  et les paquets ; nom de registre et de paquet hostiles échappés.
"""

import copy
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

import test_bm_plans_capi_250 as t250  # noqa: E402

EVIL = '<img src=x onerror="window.__reg260=1">'
fulfill = t250.fulfill


@pytest.fixture
def lists(monkeypatch):
    lst = copy.deepcopy(t250.LIST)
    lst["phases"] = ["readiness", "terraform", "registry", "capi", "clusters", "artefacts"]
    lst["available"] = ["readiness", "terraform", "registry", "capi", "clusters"]
    monkeypatch.setattr(t250, "LIST", lst)
    return lst


def open_page(context, flask_server, check=None):
    # posées sur le contexte : lues par la page dès son premier rendu
    context.route(re.compile(r".*/api/registries$"), lambda r, q: fulfill(r, {"registries": [
        {"name": "client", "kind": "external", "endpoint": "h:443", "clusters": ["harv-fake"]}]}))
    context.route(re.compile(r".*/api/artefacts$"), lambda r, q: fulfill(r, {"packages": [
        {"name": "socle", "version": "2026.11", "items": 2, "trust": "valid"}]}))
    return t250.open_page(context, flask_server, "#automation/pxe/plans", check=check)


def test_registry_card_registries_publications_and_cluster_entries(context, flask_server, lists):
    page, st = open_page(context, flask_server)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    form = page.locator("#bm-plans .bmpl-form")
    card = form.locator('.bmpl-phase[data-phase="registry"]')
    expect(card.locator(".bmpl-phase-on")).to_be_enabled()
    expect(card.locator(".bmpl-coming")).to_have_count(0)
    art = form.locator('.bmpl-phase[data-phase="artefacts"]')
    expect(art.locator(".bmpl-coming")).to_contain_text("coming in 2.7")
    card.locator(".bmpl-phase-on").check()
    card.locator('.bmpl-reg-add[data-mode="bootstrap"]').click()
    row = card.locator(".bmpl-reg").first
    row.locator('[data-k="address"]').fill("{{registre_ip}}")
    row.locator('[data-k="address"]').dispatch_event("change")
    row.locator('[data-k="storage_class"]').fill("harv-rep1")
    row.locator('[data-k="storage_class"]').dispatch_event("change")
    row.locator('[data-k="mirrors"]').fill("ghcr.io, quay.io")
    row.locator('[data-k="mirrors"]').dispatch_event("change")
    card.locator('.bmpl-reg-add[data-mode="external"]').click()
    ext = card.locator(".bmpl-reg").nth(1)
    ext.locator('[data-k="name"]').fill("client")
    ext.locator('[data-k="name"]').dispatch_event("change")
    expect(card.locator(".bmpl-reg").nth(1)).to_contain_text("declared")
    card.locator(".bmpl-pub-add").click()
    expect(card.locator(".bmpl-pub-reg")).to_have_value("amorcage")
    regs = form.locator(".bmpl-cl-regs").first
    regs.fill("amorcage, client")
    regs.dispatch_event("change")
    form.locator(".bmpl-save").click()
    expect(form.locator(".bmpl-msg")).to_be_visible()
    body = st["put"][-1]
    assert body["phases"]["registry"] == {
        "enabled": True, "timeout_minutes": 10, "publish_timeout_minutes": 60,
        "registries": [{"name": "amorcage", "mode": "bootstrap", "address": "{{registre_ip}}", "size_gib": 20,
                        "storage_class": "harv-rep1", "mirrors": ["ghcr.io", "quay.io"]},
                       {"name": "client", "mode": "external"}],
        "publish": [{"package": "socle", "version": "2026.11", "registry": "amorcage"}]}
    assert body["phases"]["clusters"]["items"][0]["registries"] == ["amorcage", "client"]
    assert all(t.get_attribute("data-tip") for t in card.locator(".tip").all())
    assert st["errors"] == []


def test_deploy_window_groups_registry_findings_and_summarises(context, flask_server, lists):
    check = {"findings": [
        {"code": "registry-address-answers", "level": "blocking", "phase": "registry", "detail": "amorcage: 172.16.2.76"},
        {"code": "publish-later", "level": "info", "phase": "registry", "detail": "socle-2026.11 to amorcage"},
        {"code": "cluster-registry-later", "level": "info", "phase": "clusters", "detail": "amorcage"}],
        "summary": {"declarations": [], "estimate_minutes": 9, "capi": None, "clusters": [],
                    "registries": [{"name": EVIL, "mode": "bootstrap", "address": "172.16.2.76", "size_gib": 8,
                                    "exists": False}],
                    "publish": [{"package": EVIL, "version": "1.0.0", "registry": "amorcage", "size": 1 << 20}]},
        "blocking": True}
    page, st = open_page(context, flask_server, check=check)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    page.locator("#bm-plans .bmpl-apply").click()
    win = page.locator("#fp-deploy-plan")
    win.locator('[name="cluster"]').select_option(t250.C)
    expect(win.locator(".dpw-launch")).to_be_disabled()
    expect(win.locator(".dpw-launch")).to_have_attribute("title", re.compile("Something already answers"))
    win.locator('[data-dpw-tab="checks"]').click()
    first = win.locator(".dpw-group").first
    expect(first).to_have_attribute("data-phase", "registry")
    expect(first).to_contain_text("Publication checked once the registry is there")
    win.locator('[data-dpw-tab="summary"]').click()
    expect(win.locator(".dpw-registries .dpw-registry-name")).to_have_text(EVIL)
    expect(win.locator(".dpw-publish .dpw-package-name")).to_contain_text(EVIL)
    expect(win.locator(".dpw-registries")).to_contain_text("to pose")
    assert page.evaluate("window.__reg260") is None
    assert st["deploy"] == [] and st["errors"] == []
