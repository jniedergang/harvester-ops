"""2.5.0 : modèles de cluster dans l'interface (routes simulées, décision 173).

* fenêtre de création : namespace des VMs prérempli du namespace du cluster
  et grisé avec sa bulle (décision 164), jamais envoyé ; « Enregistrer comme
  modèle » ouvre l'éditeur avec la demande courante, sans nom ni namespaces ;
  grisé avec la raison pour qui n'est pas administrateur ;
* éditeur = formulaire de création en mode modèle : nom, namespace et
  namespace des VMs absents ; « Variable... » change un champ de référence
  en saisie `{{nom}}` ; enregistrement (PUT) avec la variable ;
* modèle existant rouvert : la variable revient en saisie, une référence
  absente du cluster choisi reste proposée et marquée.

Mutation jouée (rapport des tâches 5-8) : M-template-mode (nom du cluster
laissé visible dans l'éditeur de modèle).
"""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from test_bm_profiles_180 import shot  # noqa: E402
from test_capi_create_view import INVENTORY  # noqa: E402

TEMPLATE = {"name": "rke2-lab", "revision": 4, "description": "lab", "variables": ["net", "net2"],
            "updated_by": "alice", "used_by": [],
            "spec": {"network": "{{net}}", "image": "other/absent.qcow2", "cpu": 4, "memory": "8Gi",
                     "cp_replicas": 1, "worker_replicas": 2, "extra_networks": ["{{net2}}", "default/management"]}}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def world(context, flask_server):
    def make(role="admin"):
        context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
        page = context.new_page()
        st = {"put": [], "errors": [], "check": []}

        def ctpl(route, req):
            if req.method == "PUT":
                st["put"].append((req.url.rsplit("/", 1)[1], req.post_data_json))
                return fulfill(route, dict(req.post_data_json, name="x", revision=1), 201)
            if req.url.endswith("/rke2-lab"):
                return fulfill(route, TEMPLATE)
            return fulfill(route, {"templates": [{"name": "rke2-lab", "revision": 4, "description": "lab",
                                                  "variables": ["net"], "uses": ["net"], "vms": 3}]})

        def check(route, req):
            st["check"].append(req.post_data_json)
            fulfill(route, {"blocked": False, "findings": []})
        page.route("**/api/capi/harv-fake/inventory*", lambda r, q: fulfill(r, INVENTORY))
        page.route("**/api/capi/harv-fake/cluster-check", check)
        page.route(re.compile(r".*/api/baremetal/capi-templates.*"), ctpl)
        page.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(r, {"role": role, "roles_active": True,
                                                                           "user": "u", "auth": "local"}))
        page.on("pageerror", lambda e: st["errors"].append(str(e)))
        page.goto(flask_server["base_url"], wait_until="domcontentloaded")
        page.wait_for_function("window.CapiCreate && window.BMPlans && window.i18n && window.App && App.role()")
        return page, st
    return make


def x(host, key):
    return host.locator(f'[data-x="{key}"]')


def test_save_as_template_then_edit_with_a_variable(world):
    page, st = world()
    page.evaluate("CapiCreate.open('harv-fake')")
    host = page.locator("#fp-capi-create")
    expect(x(host, "name")).to_be_visible(timeout=5000)
    x(host, "name").fill("web")
    x(host, "name").press("Tab")
    # namespace des VMs : celui du cluster suivi de -vms, grisé, avec sa bulle, jamais envoyé
    tns = x(host, "target_namespace")
    expect(tns).to_be_disabled()
    expect(tns).to_have_value("web-vms")
    assert "restricted identity" in tns.get_attribute("data-tip")
    host.locator('.vm-edit-nav [data-sec="kubernetes"]').click()
    x(host, "namespace").fill("apps")
    expect(tns).to_have_value("apps-vms")
    x(host, "check").click()
    page.wait_for_timeout(300)
    assert st["check"] and "target_namespace" not in st["check"][-1]
    # enregistrer comme modèle : l'éditeur s'ouvre avec la demande courante
    save_as = x(host, "save-template")
    expect(save_as).to_be_enabled()
    save_as.click()
    ed = page.locator("#fp-bm-ctpl-new")
    expect(ed).to_be_visible()
    form = ed.locator(".bmpl-ctpl-form")
    expect(form.locator(".capi-create-tpl")).to_be_visible()
    # mode modèle : ni nom, ni namespace, ni namespace des VMs, ni contrôle
    for k in ("name", "namespace", "target_namespace", "create", "check"):
        expect(x(form, k)).to_have_count(0)
    expect(x(form, "network")).to_have_value("default/production")
    # « Variable... » sur l'image : saisie {{nom}}
    x(form, "image").select_option("__var__")
    var = form.locator('input[data-x="image"][data-var="1"]')
    expect(var).to_be_visible()
    assert var.get_attribute("data-tip")
    var.fill("{{image}}")
    shot(page, "capi-template-editor")
    form.locator('[data-ctpl="name"]').fill("rke2-web")
    form.locator('[data-ctpl="variables"]').fill("image")
    form.locator(".bmpl-ctpl-save").click()
    expect(form.locator(".bmpl-ctpl-msg")).to_contain_text("saved")
    name, body = st["put"][0]
    assert name == "rke2-web" and body["variables"] == ["image"]
    spec = body["spec"]
    assert spec["image"] == "{{image}}" and spec["network"] == "default/production"
    assert spec["cpu"] == 2 and spec["worker_replicas"] == 1
    assert not ({"name", "namespace", "target_namespace", "preset"} & set(spec))
    # « Liste » rend la liste de choix
    form.locator(".capi-var-back").click()
    expect(form.locator('select[data-x="image"]')).to_be_visible()
    assert st["errors"] == []


def test_an_existing_template_reopens_with_its_variables(world):
    page, st = world()
    page.evaluate("BMPlans.openCapiTemplate('rke2-lab', { cluster: 'harv-fake' })")
    form = page.locator("#fp-bm-ctpl-rke2-lab .bmpl-ctpl-form")
    expect(form.locator('[data-ctpl="name"]')).to_have_value("rke2-lab")
    net = form.locator('input[data-x="network"][data-var="1"]')
    expect(net).to_have_value("{{net}}")
    expect(form.locator('input[data-x="extra_networks"][data-var="1"]')).to_have_value("{{net2}}, default/management")
    img = form.locator('select[data-x="image"]')
    expect(img).to_have_value("other/absent.qcow2")
    assert "not on this cluster" in img.locator("option:checked").inner_text()
    expect(x(form, "cpu")).to_have_value("4")
    expect(x(form, "preset")).to_have_value("medium")
    form.locator(".bmpl-ctpl-save").click()
    expect(form.locator(".bmpl-ctpl-msg")).to_contain_text("saved")
    _name, body = st["put"][0]
    assert body["revision"] == 4
    assert body["spec"]["network"] == "{{net}}"
    assert body["spec"]["extra_networks"] == ["{{net2}}", "default/management"]
    assert st["errors"] == []


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_save_as_template_is_greyed_with_the_reason(world, role):
    page, st = world(role)
    page.evaluate("CapiCreate.open('harv-fake')")
    host = page.locator("#fp-capi-create")
    btn = x(host, "save-template")
    expect(btn).to_be_disabled(timeout=5000)
    assert "administrator" in btn.get_attribute("title")


def test_a_template_keeps_its_registries_and_a_registry_variable(world):
    """2.6.0 (tâche 9) : « Variable... » sur le nom du registre ; la liste
    revient dans l'ordre et repart telle quelle à l'enregistrement."""
    page, st = world()
    TEMPLATE["spec"]["registries"] = ["{{registre}}", {"registry": "client", "mirrors": ["ghcr.io"]}]
    try:
        inv = dict(INVENTORY, registries=[{"name": "client", "kind": "external", "endpoint": "h:443",
                                           "usage": ["images"], "mirrors": ["ghcr.io", "quay.io"],
                                           "attached": True, "system_images": False}])
        page.route("**/api/capi/harv-fake/inventory*", lambda r, q: fulfill(r, inv))
        page.evaluate("BMPlans.openCapiTemplate('rke2-lab', { cluster: 'harv-fake' })")
        form = page.locator("#fp-bm-ctpl-rke2-lab .bmpl-ctpl-form")
        rows = form.locator(".capi-reg-row")
        expect(rows).to_have_count(2)
        expect(rows.nth(0).locator("[data-reg-var]")).to_have_value("{{registre}}")
        expect(rows.nth(1).locator('[data-reg-mirror="quay.io"]')).not_to_be_checked()
        form.locator('.vm-edit-nav [data-sec="registries"]').click()
        form.locator('[data-reg="add"]').select_option("__var__")
        form.locator(".capi-reg-row").nth(2).locator("[data-reg-var]").fill("{{secours}}")
        form.locator(".bmpl-ctpl-save").click()
        expect(form.locator(".bmpl-ctpl-msg")).to_contain_text("saved")
        assert st["put"][0][1]["spec"]["registries"] == [
            {"registry": "{{registre}}"}, {"registry": "client", "mirrors": ["ghcr.io"]}, {"registry": "{{secours}}"}]
        assert st["errors"] == []
    finally:
        TEMPLATE["spec"].pop("registries")
