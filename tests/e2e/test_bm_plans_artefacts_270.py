"""2.7.0 (tâche 8) : phase Artefacts des plans, dans l'interface (routes
simulées).

* carte Artefacts active : délai, politiques (socle DÉCOCHÉ par défaut, bulle
  qui dit pourquoi), rôles par groupe et comptes d'automatisation en lignes ;
* éléments choisis dans les paquets importés : une sorte `images` ou
  `gitops` n'est jamais proposée ; un chart exige son registre et des valeurs
  JSON objet, refusés AVANT l'envoi ;
* document enregistré à la forme de deploy_plans ; clés non éditées d'un
  namespace d'équipe (quota, limits, network) gardées au réenregistrement ;
* fenêtre Déployer : constats de la phase Artefacts nommés, bloc Résumé des
  artefacts, noms hostiles échappés.

Mutations jouées (relevé /tmp/bmpost/270-mut/task8.txt) : « socle coché par
défaut », « élément images proposé ».
"""

import copy
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

import test_bm_plans_capi_250 as t250  # noqa: E402
from test_bm_profiles_180 import shot  # noqa: E402

EVIL = '<img src=x onerror="window.__art270=1">'
fulfill = t250.fulfill
ITEMS = [{"name": "podinfo", "kind": "chart"}, {"name": "net-pol", "kind": "manifest"},
         {"name": "imgs", "kind": "images"}, {"name": "flux", "kind": "gitops"},
         {"name": "socle-pol", "kind": "policy"}]


@pytest.fixture
def lists(monkeypatch):
    lst = copy.deepcopy(t250.LIST)
    lst["phases"] = ["readiness", "terraform", "registry", "capi", "clusters", "artefacts"]
    lst["available"] = ["readiness", "terraform", "registry", "capi", "clusters", "artefacts"]
    monkeypatch.setattr(t250, "LIST", lst)
    return lst


def open_page(context, flask_server, check=None):
    context.route(re.compile(r".*/api/registries$"), lambda r, q: fulfill(r, {"registries": [
        {"name": "client", "kind": "external", "endpoint": "h:443", "clusters": ["harv-fake"]}]}))
    context.route(re.compile(r".*/api/artefacts$"), lambda r, q: fulfill(r, {"packages": [
        {"name": "socle", "version": "2026.11", "items": 5, "trust": "valid"}]}))
    context.route(re.compile(r".*/api/artefacts/socle/2026\.11$"), lambda r, q: fulfill(r, {
        "name": "socle", "version": "2026.11", "index": {"name": "socle", "version": "2026.11", "items": ITEMS},
        "images": [], "trust": {"state": "valid"}, "published": []}))
    return t250.open_page(context, flask_server, "#automation/pxe/plans", check=check)


def test_artefacts_card_policies_items_and_saved_shape(context, flask_server, lists):
    page, st = open_page(context, flask_server)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    form = page.locator("#bm-plans .bmpl-form")
    card = form.locator('.bmpl-phase[data-phase="artefacts"]')
    expect(card.locator(".bmpl-coming")).to_have_count(0)
    expect(card.locator(".bmpl-phase-on")).to_be_enabled()
    card.locator(".bmpl-phase-on").check()
    expect(card.locator('[name="artefacts_timeout"]')).to_have_value("15")
    expect(card.locator(".bmpl-art-clusters-hint")).to_contain_text("{{cluster}}-apps")
    # politiques : socle décoché par défaut, la bulle dit pourquoi
    card.locator(".bmpl-pol-add").click()
    pol = card.locator(".bmpl-pol").first
    base = pol.locator('.bmpl-pol-f[data-k="base"]')
    expect(base).not_to_be_checked()
    assert "Off by default" in pol.locator('label:has(.bmpl-pol-f[data-k="base"])').get_attribute("data-tip")
    expect(pol.locator(".bmpl-pol-role-add")).to_be_disabled()          # pas de namespace d'équipe
    teams = pol.locator('.bmpl-pol-f[data-k="teams"]')
    teams.fill("equipe-a, equipe-b")
    teams.dispatch_event("change")
    pol = card.locator(".bmpl-pol").first
    pol.locator(".bmpl-pol-role-add").click()
    role = card.locator(".bmpl-pol-role").first
    role.locator('[data-k="namespace"]').select_option("equipe-b")
    role.locator('[data-k="group"]').fill("cn=devs,ou=groups")
    role.locator('[data-k="group"]').dispatch_event("change")
    role.locator('[data-k="role"]').select_option("edit")
    card.locator(".bmpl-pol-acc-add").click()
    acc = card.locator(".bmpl-pol-account").first
    acc.locator('[data-k="name"]').fill("ci")
    acc.locator('[data-k="name"]').dispatch_event("change")
    acc.locator('[data-k="role"]').select_option("edit")
    # élément : images et gitops jamais proposés
    card.locator(".bmpl-art-add").click()
    row = card.locator(".bmpl-art").first
    sel = row.locator(".bmpl-art-item")
    expect(sel.locator("option")).to_have_count(3)
    values = sel.locator("option").evaluate_all("os => os.map(o => o.value)")
    assert values == ["podinfo", "net-pol", "socle-pol"]
    expect(row).to_have_attribute("data-kind", "chart")
    # chart sans registre : refusé avant l'envoi
    form.locator(".bmpl-save").click()
    expect(form.locator(".bmpl-art-refusal")).to_contain_text("choose the registry")
    assert st["put"] == []
    row.locator(".bmpl-art-reg").select_option("client")
    vals = row.locator(".bmpl-art-values")
    vals.fill("[1, 2]")
    vals.dispatch_event("change")
    form.locator(".bmpl-save").click()
    expect(form.locator(".bmpl-art-refusal")).to_contain_text("not a JSON object")
    assert st["put"] == []
    vals.fill('{"replicaCount": 2}')
    vals.dispatch_event("change")
    # second élément, un manifeste qui dépend du chart : pas de registre
    card.locator(".bmpl-art-add").click()
    row2 = card.locator(".bmpl-art").nth(1)
    expect(row2.locator(".bmpl-art-item option")).to_have_count(3)
    row2.locator(".bmpl-art-item").select_option("net-pol")
    row2 = card.locator(".bmpl-art").nth(1)
    expect(row2.locator(".bmpl-art-reg")).to_have_count(0)
    clusters = row2.locator(".bmpl-art-clusters")
    clusters.fill("{{cluster}}-apps, outils/outils")
    clusters.dispatch_event("change")
    row2.locator('.bmpl-art-dep[value="podinfo"]').check()
    for el in card.locator("button, select, input, textarea").all():
        if el.is_visible():
            assert el.get_attribute("data-tip") or el.evaluate("e => !!e.closest('.tip')"), el.evaluate("e => e.outerHTML")
    card.scroll_into_view_if_needed()
    shot(page, "bm-plans-artefacts")
    form.locator(".bmpl-save").click()
    expect(form.locator(".bmpl-msg .bm-imported")).to_be_visible()
    body = st["put"][-1]
    assert body["phases"]["artefacts"] == {
        "enabled": True, "timeout_minutes": 15,
        "policies": [{"clusters": ["{{cluster}}-apps"], "base": False,
                      "teams": [{"namespace": "equipe-a"}, {"namespace": "equipe-b"}],
                      "roles": [{"namespace": "equipe-b", "group": "cn=devs,ou=groups", "role": "edit"}],
                      "accounts": [{"namespace": "equipe-a", "name": "ci", "role": "edit"}]}],
        "items": [{"package": "socle", "version": "2026.11", "item": "podinfo", "clusters": ["{{cluster}}-apps"],
                   "registry": "client", "values": {"replicaCount": 2}},
                  {"package": "socle", "version": "2026.11", "item": "net-pol",
                   "clusters": ["{{cluster}}-apps", "outils/outils"], "depends_on": ["podinfo"]}]}
    assert st["errors"] == []


def test_loaded_plan_keeps_team_keys_it_does_not_edit(context, flask_server, lists, monkeypatch):
    plan = copy.deepcopy(t250.PLAN)
    team = {"namespace": "equipe-a", "quota": {"pods": "20"}, "limits": {"max": {"cpu": "1"}},
            "network": {"default_deny": True, "allow_ingress": False}}
    plan["phases"]["artefacts"] = {"enabled": True, "timeout_minutes": 20, "policies": [
        {"clusters": ["{{cluster}}-apps"], "base": True, "teams": [team], "roles": [], "accounts": []}],
        "items": [{"package": EVIL, "version": "1.0.0", "item": "gone", "registry": "client",
                   "values": {"a": 1}, "clusters": ["x/y"]}]}
    monkeypatch.setattr(t250, "PLAN", plan)
    page, st = open_page(context, flask_server)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    card = page.locator('#bm-plans .bmpl-phase[data-phase="artefacts"]')
    expect(card.locator('.bmpl-pol-f[data-k="base"]')).to_be_checked()
    # paquet absent, nom hostile : montré échappé, entrée gardée telle quelle
    expect(card.locator(".bmpl-art-pkg option").first).to_contain_text(EVIL)
    page.locator("#bm-plans .bmpl-save").click()
    expect(page.locator("#bm-plans .bmpl-msg .bm-imported")).to_be_visible()
    art = st["put"][-1]["phases"]["artefacts"]
    assert art["timeout_minutes"] == 20
    assert art["policies"][0]["teams"] == [team]
    assert art["items"] == [{"package": EVIL, "version": "1.0.0", "item": "gone", "clusters": ["x/y"],
                             "registry": "client", "values": {"a": 1}}]
    assert page.evaluate("window.__art270") is None
    assert st["errors"] == []


def test_deploy_window_names_artefact_findings_and_summarises(context, flask_server, lists):
    check = {"findings": [
        {"code": "artefact-not-published", "level": "blocking", "phase": "artefacts", "detail": "podinfo on a/a"},
        {"code": "manifest-kind-refused", "level": "blocking", "phase": "artefacts", "detail": "CustomResourceDefinition"},
        {"code": "artefact-images-not-published", "level": "warning", "phase": "artefacts", "detail": "x"},
        {"code": "registry-cert-expiring", "level": "warning", "phase": "registry", "detail": "amorcage"}],
        "summary": {"declarations": [], "estimate_minutes": 4, "capi": None, "clusters": [],
                    "artefacts": [{"id": "pol-a", "type": "policies", "ref": "a/a", "item": "hops-policies",
                                   "package": None, "version": None, "registry": None},
                                  {"id": "art-a-x", "type": "chart", "ref": "a/a", "item": EVIL,
                                   "package": "socle", "version": "2026.11", "registry": EVIL}]},
        "blocking": True}
    page, st = open_page(context, flask_server, check=check)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    page.locator("#bm-plans .bmpl-apply").click()
    win = page.locator("#fp-deploy-plan")
    win.locator('[name="cluster"]').select_option(t250.C)
    expect(win.locator(".dpw-launch")).to_be_disabled()
    expect(win.locator(".dpw-launch")).to_have_attribute("title", re.compile("not published in its registry"))
    win.locator('[data-dpw-tab="checks"]').click()
    group = win.locator('.dpw-group[data-phase="artefacts"]')
    expect(group).to_contain_text("refused")
    expect(group).to_contain_text("images of an item")
    expect(win.locator('.dpw-group[data-phase="registry"]')).to_contain_text("expires soon")
    win.locator('[data-dpw-tab="summary"]').click()
    rows = win.locator(".dpw-artefacts tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("policies and access")
    expect(rows.nth(1).locator(".dpw-artefact-item")).to_have_text(EVIL)
    assert page.evaluate("window.__art270") is None
    assert st["deploy"] == [] and st["errors"] == []
