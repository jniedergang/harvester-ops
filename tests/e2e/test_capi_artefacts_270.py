"""2.7.0 (tâche 6, interface de la tâche 8) : éléments de paquet appliqués aux
clusters enfants, sur routes simulées.

* Cluster API > Services : colonne Sorte (chart, manifeste, politique,
  socle), élément d'un chart de paquet, état du binding, retrait d'un
  manifeste avec sa sorte ;
* Artefacts : bouton Appliquer d'un élément (absent pour une sorte
  `images` ou à venir), fenêtre Paramètres, Vérifications, Résumé, bouton
  grisé jusqu'aux constats, demande envoyée ;
* noms hostiles échappés partout.
"""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402

HOSTILE = '<img src=x onerror="window.__pwned=1">'
ST_OK = {"applied": True, "hash": "sha256:" + "a" * 64, "clusters": [], "message": "applied"}
ST_WAIT = {"applied": False, "hash": None, "clusters": [], "message": "waiting for the binding"}
ART = {"item": "podinfo", "package": "banc", "version": "1.0.0", "cluster": "web", "by": "alice", "origin": {}}
SERVICES = {"caaph": True, "clusters": ["web/web"], "catalog": [], "services": [
    {"namespace": "web", "name": "podinfo", "service": "registry", "chart": "podinfo", "version": "6.9.2",
     "release_namespace": "podinfo", "ready": True, "message": "", "artefact": ART,
     "releases": [{"cluster": "web", "status": "deployed", "revision": 1, "ready": True, "message": ""}]}],
    "registry_charts": [{"registry": "amorcage", "package": "banc", "version": "1.0.0", "item": "podinfo",
                         "chart": "podinfo", "chart_version": "6.9.2"}]}
APPLIED = {"charts": [], "clusters": ["web/web"], "accounts": [], "manifests": [
    {"namespace": "web", "cluster": "web", "item": "essai", "kind": "manifest", "package": "banc",
     "version": "1.0.0", "objects": 3, "applied_by": "alice", "state": ST_OK},
    {"namespace": "web", "cluster": "web", "item": "regles", "kind": "policy", "package": "banc",
     "version": "1.0.0", "objects": 2, "applied_by": HOSTILE, "state": ST_WAIT},
    {"namespace": "web", "cluster": "web", "item": "hops-policies", "kind": "policies", "package": "harvester-ops",
     "version": "builtin", "objects": 7, "applied_by": "alice", "state": ST_OK}]}
ITEMS = [{"name": "podinfo", "kind": "chart"}, {"name": "essai", "kind": "manifest"},
         {"name": "outils", "kind": "images"}, {"name": "flux", "kind": "gitops", "later": True}]


def fulfill(route, body, status=200, ctype="application/json"):
    route.fulfill(status=status, content_type=ctype, body=body if isinstance(body, str) else json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    def make(hash_, applied=APPLIED, check=None, items=ITEMS):
        context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
        page = context.new_page()
        calls = {"check": [], "apply": [], "delete": []}

        def on_check(route, request):
            calls["check"].append(json.loads(request.post_data or "{}"))
            fulfill(route, check(len(calls["check"])) if check else {"blocked": False, "findings": [], "objects": 3})

        def on_apply(route, request):
            calls["apply"].append(json.loads(request.post_data or "{}"))
            fulfill(route, {"action_id": "art000000001", "kind": "manifest"}, 202)

        def on_delete(route, request):
            calls["delete"].append(request.url)
            fulfill(route, {"action_id": "art000000002"}, 202)
        page.route(re.compile(r".*/api/capi/harv-fake/services.*"), lambda r, q: fulfill(r, SERVICES))
        page.route(re.compile(r".*/api/capi/harv-fake/artefacts/applied.*"), lambda r, q: fulfill(r, applied))
        page.route("**/api/capi/harv-fake/artefact-check", on_check)
        page.route("**/api/capi/harv-fake/artefact", on_apply)
        page.route(re.compile(r".*/api/capi/harv-fake/artefact/.*"), on_delete)
        page.route("**/api/stream/art00000000*", lambda r, q: fulfill(r, "", ctype="text/event-stream"))
        page.route(re.compile(r".*/api/artefacts$"), lambda r, q: fulfill(r, {"packages": [
            {"name": "banc", "version": "1.0.0", "items": len(items), "trust": "valid", "principal": "ops"}],
            "strict": False, "locked": False}))
        page.route(re.compile(r".*/api/artefacts/banc/1\.0\.0$"), lambda r, q: fulfill(r, {
            "name": "banc", "version": "1.0.0", "index": {"items": items}, "images": [], "published": [],
            "trust": {"state": "valid", "principal": "ops"}}))
        page.on("dialog", lambda d: d.accept())
        goto(page, flask_server["base_url"], hash_)
        page.wait_for_function("window.CapiServices && window.Artefacts && window.i18n")
        return page, calls
    return make


def test_the_services_table_has_a_kind_column_and_the_binding_state(ui):
    page, _ = ui("#automation/capi/services")
    host = page.locator("#capi-services-body")
    expect(host.locator("tr[data-svc-art]")).to_have_count(3, timeout=8000)
    kinds = host.locator(".svc-kind")
    expect(kinds).to_have_count(4)
    assert [k.get_attribute("data-kind") for k in kinds.all()] == ["chart", "manifest", "policy", "policies"]
    for k in kinds.all():
        assert k.get_attribute("data-tip")                                      # info-bulle
    expect(host.locator('tr[data-svc="web/podinfo"] .svc-artefact')).to_contain_text("Item podinfo of banc 1.0.0, by alice")
    expect(host.locator('tr[data-svc-art="web/web/essai"] .svc-binding')).to_contain_text("applied")
    expect(host.locator('tr[data-svc-art="web/web/regles"] .svc-binding')).to_contain_text("not applied yet")
    expect(host.locator('tr[data-svc-art="web/web/regles"]')).to_contain_text(HOSTILE)   # texte, pas balise
    assert page.evaluate("() => window.__pwned") is None
    assert host.locator("img").count() == 0


@pytest.mark.parametrize("ref,kind", [("web/web/essai", "manifest"), ("web/web/hops-policies", "policies")])
def test_removing_a_manifest_sends_its_kind(ui, ref, kind):
    page, calls = ui("#automation/capi/services")
    btn = page.locator(f'#capi-services-body [data-svc-art-remove="{ref}"]')
    expect(btn).to_be_enabled(timeout=8000)
    assert btn.get_attribute("title")
    btn.click()
    page.wait_for_function("() => true")
    expect(page.locator("#capi-services-body .svc-feedback")).to_contain_text("art000000002")
    ns, child, item = ref.split("/")
    assert calls["delete"] and calls["delete"][0].endswith(f"/artefact/{ns}/{child}/{item}?kind={kind}")


def open_detail(page):
    page.locator(".art-row").first.click()
    expect(page.locator("#artefacts-detail [data-art-apply]").first).to_be_visible(timeout=8000)


def test_apply_buttons_only_for_applicable_kinds(ui):
    page, _ = ui("#automation/capi/artefacts")
    open_detail(page)
    btns = page.locator("#artefacts-detail [data-art-apply]")
    assert sorted(b.get_attribute("data-art-apply") for b in btns.all()) == ["essai", "podinfo"]
    for b in btns.all():
        assert b.get_attribute("title")


def test_the_apply_window_checks_before_its_button_and_sends_the_request(ui):
    blocked = {"blocked": True, "objects": 3, "findings": [
        {"code": "child-admin-required", "level": "block", "facts": {"cluster": "web/web"}}]}
    ok = {"blocked": False, "objects": 3, "findings": []}
    page, calls = ui("#automation/capi/artefacts", check=lambda n: blocked if n == 1 else ok)
    open_detail(page)
    page.locator('#artefacts-detail [data-art-apply="podinfo"]').click()
    m = page.locator(".art-apply")
    expect(m).to_be_visible()
    go = m.locator("[data-art-apply-go]")
    expect(m.locator('[data-x="count"]')).to_have_text("1")
    expect(go).to_be_disabled()
    assert "kubeconfig Secret of web/web" in (m.locator('[data-x="go-wrap"]').get_attribute("data-tip") or "")
    m.locator('[data-art-tab="checks"]').click()
    expect(m.locator('[data-code="child-admin-required"]')).to_be_visible()
    # une saisie grise le bouton jusqu'aux nouveaux constats
    m.locator('[data-art-tab="params"]').click()
    m.locator('[data-x="values"]').fill("replicaCount: 2\n")
    expect(go).to_be_enabled(timeout=5000)
    m.locator('[data-art-tab="summary"]').click()
    expect(m.locator('[data-x="summary"]')).to_contain_text("Installed by CAAPH from amorcage")
    go.click()
    expect(m.locator('[data-x="progress"]')).to_contain_text("art000000001")
    assert calls["apply"] == [{"cluster": "web/web", "package": "banc", "version": "1.0.0", "item": "podinfo",
                               "registry": "amorcage", "values": "replicaCount: 2\n"}]
    assert calls["check"][-1]["registry"] == "amorcage"


def test_a_manifest_has_no_registry_and_hostile_names_are_escaped(ui):
    items = [{"name": "essai", "kind": "manifest", "description": HOSTILE}]
    check = lambda n: {"blocked": True, "objects": 1, "findings": [  # noqa: E731
        {"code": HOSTILE, "level": "block", "facts": {"x": HOSTILE}}]}
    page, calls = ui("#automation/capi/artefacts", items=items, check=check)
    open_detail(page)
    page.locator('#artefacts-detail [data-art-apply="essai"]').click()
    m = page.locator(".art-apply")
    expect(m.locator('[data-x="count"]')).to_have_text("1")
    assert m.locator('[data-x="registry"]').count() == 0 and m.locator('[data-x="values"]').count() == 0
    m.locator('[data-art-tab="checks"]').click()
    expect(m.locator('[data-x="report"]')).to_contain_text(HOSTILE)
    expect(m.locator("[data-art-apply-go]")).to_be_disabled()
    assert calls["check"][0] == {"cluster": "web/web", "package": "banc", "version": "1.0.0", "item": "essai"}
    assert page.evaluate("() => window.__pwned") is None
    assert page.locator(".art-apply img").count() == 0
    m.locator("[data-art-close]").first.click()
    expect(page.locator(".art-apply")).to_have_count(0)
    assert calls["apply"] == []


def test_apply_stays_disabled_after_launch_even_after_an_edit(ui):
    """Revue 4-8 I3 : une saisie après le lancement ne réactive pas Appliquer."""
    page, calls = ui("#automation/capi/artefacts")
    open_detail(page)
    page.locator('#artefacts-detail [data-art-apply="podinfo"]').click()
    m = page.locator(".art-apply")
    go = m.locator("[data-art-apply-go]")
    expect(go).to_be_enabled(timeout=5000)
    go.click()
    expect(m.locator('[data-x="progress"]')).to_contain_text("art000000001")
    n_checks = len(calls["check"])
    m.locator('[data-x="values"]').fill("replicaCount: 3\n")
    page.wait_for_timeout(900)                     # au-delà du délai de vérification (500 ms)
    expect(go).to_be_disabled()
    assert len(calls["check"]) == n_checks and len(calls["apply"]) == 1
    assert "follow the action in the dock" in (go.get_attribute("title") or "")
