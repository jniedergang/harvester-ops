"""2.10.2 (point 331 B) : identités CAPHV d'avant la 2.10.1 dans la vue Cluster API.

* constat en tête de la liste des clusters, badge et bouton sur la ligne du
  cluster, info-bulles ;
* « Mettre à jour l'identité » : aperçu des changements exacts (Role, règle),
  constats bloquants à part, action lancée pour un cluster ou pour tous,
  essai à blanc au choix ;
* opérateur : boutons grisés, info-bulle sur leur enveloppe ;
* textes du cluster échappés.
"""

import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_auto_tabs_220 import fulfill, open_auto  # noqa: E402

DIAG = {"components": [], "capi_clusters": [
    {"namespace": "web", "name": "web", "phase": "Provisioned", "ready": True, "k8sVersion": "v1.34.11"},
    {"namespace": "api", "name": "api", "phase": "Provisioned", "ready": True, "k8sVersion": "v1.34.11"},
    {"namespace": "ops", "name": "<b>", "phase": "Provisioned", "ready": True, "k8sVersion": "v1.34.11"}]}
CHANGE = {"namespace": "default", "role": "caphv-identity-web", "image": "image-abc",
          "rule": {"apiGroups": ["harvesterhci.io"], "resources": ["virtualmachineimages"], "verbs": ["get"],
                   "resourceNames": ["image-abc"]}}
REPORT = {"unreachable": False, "caphv_version": "v0.10.1", "clusters": [
    {"cluster": "web/web", "state": "outdated", "changes": [CHANGE],
     "findings": [{"code": "identity-outdated", "level": "warn",
                   "facts": {"cluster": "web/web", "roles": "default/caphv-identity-web", "images": "image-abc"}}]},
    {"cluster": "api/api", "state": "current", "changes": [], "findings": []},
    {"cluster": "ops/<b>", "state": "blocked", "changes": [],
     "findings": [{"code": "identity-role-foreign", "level": "block",
                   "facts": {"cluster": "ops/<b>", "role": "default/caphv-identity-ops"}}]}]}


def open_clusters(context, flask_server, role="admin", report=REPORT, posted=None):
    """Routes posées sur le contexte AVANT l'ouverture : le premier rendu de
    la liste lit déjà les identités simulées."""
    context.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(
        r, {"role": role, "roles_active": True, "user": "u", "auth": "local"}))

    def ids(route, req):
        name = re.search(r"name=([^&]+)", req.url)
        if name:
            want = name.group(1).replace("%2F", "/")
            fulfill(route, dict(report, clusters=[c for c in report["clusters"] if c["cluster"] == want]))
        else:
            fulfill(route, report)
    context.route(re.compile(r".*/api/capi/harv-fake/identities.*"), ids)
    context.route(re.compile(r".*/api/capi/harv-fake/identity-update$"),
                  lambda r, q: ((posted if posted is not None else []).append(json.loads(q.post_data)),
                                fulfill(r, {"action_id": "id1", "dry_run": False}, 201)))
    page = open_auto(context, flask_server, "#automation/capi/clusters", diag=DIAG)
    expect(page.locator("#capi-k8s-body tbody tr")).to_have_count(3, timeout=8000)
    expect(page.locator("#capi-k8s-body").locator("xpath=ancestor::*[contains(@class,'card')][1]")
           .locator(".veil.visible")).to_have_count(0, timeout=8000)
    return page


def test_the_list_says_which_identities_are_outdated(context, flask_server):
    page = open_clusters(context, flask_server)
    body = page.locator("#capi-k8s-body")
    summary = body.locator("[data-capi-identity-summary]")
    expect(summary).to_be_visible(timeout=8000)
    expect(summary).to_contain_text("1 cluster(s)")
    expect(summary).to_contain_text("web/web")
    assert summary.get_attribute("data-tip")
    expect(body.locator('[data-capi-identity-state="outdated"]')).to_have_count(1)
    blocked = body.locator('[data-capi-identity-state="blocked"]')
    expect(blocked).to_have_count(1)
    assert "ops/<b>" in blocked.get_attribute("data-tip")                       # texte échappé, pas du HTML
    expect(body.locator("b")).to_have_count(0)
    expect(body.locator('[data-capi-identity-update="api/api"]')).to_have_count(0)  # à jour : rien à faire


@pytest.mark.parametrize("which,dry,body", [
    ("row", False, {"name": "web/web", "dry_run": False}),
    ("all", False, {"name": "", "dry_run": False}),
    ("all", True, {"name": "", "dry_run": True}),
])
def test_update_shows_the_exact_change_then_starts_the_action(context, flask_server, which, dry, body):
    posted = []
    page = open_clusters(context, flask_server, posted=posted)
    sel = '[data-capi-identity-update="web/web"]' if which == "row" else '[data-capi-identity-summary] [data-capi-identity-update=""]'
    btn = page.locator(f"#capi-k8s-body {sel}").first
    expect(btn).to_be_enabled(timeout=8000)
    assert btn.evaluate("b => b.parentElement.classList.contains('tip') && !!b.parentElement.dataset.tip")
    btn.click()
    modal = page.locator("#capi-identity-modal")
    expect(modal.locator("[data-capi-identity-change]")).to_have_count(1)
    row = modal.locator("[data-capi-identity-change]")
    expect(row).to_contain_text("default/caphv-identity-web")
    expect(row).to_contain_text("get on virtualmachineimages image-abc")
    if which == "all":
        expect(modal.locator('[data-code="identity-role-foreign"]')).to_contain_text("ops/<b>")
    if dry:
        modal.locator('[data-x="dry"]').check()
    modal.locator('[data-x="go"]').click()
    expect(page.locator("#capi-identity-modal")).to_have_count(0)
    expect(page.locator("[data-capi-identity-started]")).to_contain_text("id1")
    assert posted == [body]


def test_an_operator_sees_the_finding_but_cannot_update(context, flask_server):
    page = open_clusters(context, flask_server, role="operator")
    btn = page.locator('#capi-k8s-body [data-capi-identity-update="web/web"]')
    expect(btn).to_be_disabled(timeout=8000)
    assert "administrator" in btn.evaluate("b => b.parentElement.dataset.tip")


def test_nothing_to_change_keeps_update_disabled(context, flask_server):
    report = dict(REPORT, clusters=[REPORT["clusters"][1]])
    page = open_clusters(context, flask_server, report=report)
    expect(page.locator("#capi-k8s-body tbody tr")).to_have_count(3, timeout=8000)
    expect(page.locator("[data-capi-identity-summary]")).to_have_count(0)
    page.evaluate("CAPI.openIdentityUpdate('')")
    modal = page.locator("#capi-identity-modal")
    expect(modal.locator("[data-capi-identity-none]")).to_be_visible()
    expect(modal.locator('[data-x="go"]')).to_be_disabled()


def test_a_failed_read_does_not_hide_the_list(context, flask_server):
    context.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(
        r, {"role": "admin", "roles_active": True, "user": "u", "auth": "local"}))
    context.route(re.compile(r".*/api/capi/harv-fake/identities.*"), lambda r, q: fulfill(r, {"error": "x"}, 502))
    page = open_auto(context, flask_server, "#automation/capi/clusters", diag=DIAG)
    expect(page.locator("#capi-k8s-body tbody tr")).to_have_count(3, timeout=8000)
    expect(page.locator("[data-capi-identity-summary]")).to_have_count(0)


def test_the_cluster_details_say_it_too(context, flask_server):
    context.route(re.compile(r".*/api/capi/harv-fake/cluster/web/web/details$"), lambda r, q: fulfill(r, {
        "name": "web", "namespace": "web", "identity": "restricted", "phase": "Provisioned", "ready": True,
        "conditions": [], "machines": [], "topology": {}, "controlPlaneEndpoint": {}}))
    page = open_clusters(context, flask_server)
    page.locator('#capi-k8s-body .capi-cluster-details[data-name="web"]').click()
    box = page.locator("[data-capi-identity-box]")
    expect(box.locator('[data-code="identity-outdated"]')).to_contain_text("image-abc", timeout=8000)
    assert box.locator('[data-code="identity-outdated"]').get_attribute("data-tip")
    expect(box.locator('[data-capi-identity-update="web/web"]')).to_be_enabled()
