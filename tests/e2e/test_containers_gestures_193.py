"""v1.93.0 : les gestes des Containers (créer, mettre à l'échelle, redémarrer,
supprimer), API simulée.

On vérifie le bouton Créer et sa fenêtre (formulaire validé avant tout envoi,
onglet YAML, indication des GPU de conteneurs libres), le menu de la ligne
(entrées grisées selon le type), la confirmation dans la page avant une
suppression (jamais `confirm()`), les requêtes envoyées, le suivi de l'action
et la relecture de la liste à la fin."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

ACTION = "ct0000000193"


def wl(name, kind, desired=1, ns="apps", pods=()):
    return {"namespace": ns, "name": name, "kind": kind, "state": "Running", "ready": desired,
            "desired": desired, "images": ["nginx:1"], "gpus": 0, "created": "2026-10-16T10:00:00Z",
            "labels": {}, "pods": list(pods)}


CURRENT = {"cluster": "harv-fake", "denials": [], "workloads": [
    wl("web", "Deployment", 2, pods=[{"name": "web-1", "namespace": "apps", "node": "n1", "phase": "Running",
                                      "state": "Running", "ready": True, "restarts": 0, "created": "2026-10-16T10:00:00Z",
                                      "containers": ["web"], "init_containers": []}]),
    wl("once", "Job", 1), wl("agent", "DaemonSet", 1, ns="ops")]}
GPUS = {"cluster": "harv-fake", "units": [], "addon": True,
        "summary": {"gpu": {"total": 0, "used": 0, "free": 0}, "vgpu": {"total": 0, "used": 0, "free": 0},
                    "mig": {"total": 0, "used": 0, "free": 0}, "node": {"total": 4, "used": 1, "free": 3}}}


@pytest.fixture
def view(context, flask_server):
    context.add_init_script(
        "if (!sessionStorage.getItem('ctg-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','containers');"
        "localStorage.removeItem('harvester_ops_containers');"
        "sessionStorage.setItem('ctg-init','1'); }")
    page = context.new_page()
    sent, lists = [], []

    def listing(route, req):
        lists.append(req.url)
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(CURRENT))
    page.route(re.compile(r".*/api/containers/harv-fake(\?.*)?$"), listing)
    page.route(re.compile(r".*/api/gpus/harv-fake(\?.*)?$"), lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(GPUS)))

    def write(route, req):
        body = req.post_data_json if req.post_data else None
        sent.append((req.method, req.url.split("/api/")[1], body))
        if body and body.get("spec", {}).get("name") == "refused":
            return route.fulfill(status=400, content_type="application/json",
                                 body=json.dumps({"error": "namespace kube-system is a system namespace"}))
        name = (body or {}).get("spec", {}).get("name", "web")
        return route.fulfill(status=202, content_type="application/json", body=json.dumps(
            {"action_id": ACTION, "namespace": "apps", "name": name, "kind": "Deployment"}))
    page.route(re.compile(r".*/api/containers/harv-fake/create$"), write)
    page.route(re.compile(r".*/api/containers/harv-fake/[^/]+/[^/]+/[^/]+(/scale|/restart)?$"), write)
    page.route(f"**/api/stream/{ACTION}", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.Containers && window.App && App.getCurrentCluster()")
    page.mouse.move(1200, 600)
    rows = page.locator("#containers-host tbody tr[data-ct-row]")
    expect(rows).to_have_count(3)
    return page, rows, sent, lists, dialogs


def closed_with_toast(page, panel, text):
    """v1.94.1 : demande acceptée, la fenêtre se ferme ; l'action suivie reste
    dans le dock et sa fin s'annonce par une notification."""
    expect(panel).to_have_count(0)
    expect(page.locator(".vma-toast").filter(has_text=text)).to_have_count(1)


def menu_of(page, rows, name):
    rows.filter(has=page.locator(f'td:text-is("{name}")')).locator('[data-ct-act="more"]').click()
    menu = page.locator(".ct-menu")
    expect(menu).to_be_visible()
    return menu


def test_the_menu_greys_what_a_kind_cannot_do(view):
    page, rows, _, _, _ = view
    menu = menu_of(page, rows, "web")
    for act in ("scale", "restart", "delete"):
        expect(menu.locator(f'[data-ct-menu="{act}"]')).not_to_have_attribute("aria-disabled", "true")
        assert menu.locator(f'[data-ct-menu="{act}"]').get_attribute("data-tip")
    page.keyboard.press("Escape")
    menu = menu_of(page, rows, "once")                                  # un Job
    expect(menu.locator('[data-ct-menu="scale"]')).to_have_attribute("aria-disabled", "true")
    expect(menu.locator('[data-ct-menu="restart"]')).to_have_attribute("aria-disabled", "true")
    expect(menu.locator('[data-ct-menu="scale"]')).to_have_attribute("data-tip", re.compile("Job"))
    expect(menu.locator('[data-ct-menu="delete"]')).not_to_have_attribute("aria-disabled", "true")
    page.keyboard.press("Escape")
    menu = menu_of(page, rows, "agent")                                 # un DaemonSet
    expect(menu.locator('[data-ct-menu="scale"]')).to_have_attribute("aria-disabled", "true")
    expect(menu.locator('[data-ct-menu="restart"]')).not_to_have_attribute("aria-disabled", "true")


def test_scale_sends_the_number_and_refreshes(view):
    page, rows, sent, lists, _ = view
    menu_of(page, rows, "web").locator('[data-ct-menu="scale"]').click()
    panel = page.locator(".floating-panel", has=page.locator('[name="replicas"]'))
    expect(panel.locator('[name="replicas"]')).to_have_value("2")
    panel.locator('[name="replicas"]').fill("-3")
    panel.locator("[data-ct-submit]").click()
    expect(panel.locator(".of-msg")).to_contain_text("whole number")
    assert sent == []
    before = len(lists)
    panel.locator('[name="replicas"]').fill("5")
    panel.locator("[data-ct-submit]").click()
    closed_with_toast(page, panel, "apps/web set to 5 pods.")
    assert sent == [("POST", "containers/harv-fake/apps/deployment/web/scale", {"replicas": 5})]
    for _ in range(30):
        if any("fresh=1" in u for u in lists[before:]):
            break
        page.wait_for_timeout(100)
    assert any("fresh=1" in u for u in lists[before:])                  # la liste relue à la fin


def test_restart(view):
    page, rows, sent, _, _ = view
    menu_of(page, rows, "agent").locator('[data-ct-menu="restart"]').click()
    panel = page.locator(".floating-panel", has_text="replace the old ones")
    expect(panel).to_contain_text("DaemonSet ops/agent")
    panel.locator("[data-ct-submit]").click()
    closed_with_toast(page, panel, "ops/agent is restarting.")
    assert sent == [("POST", "containers/harv-fake/ops/daemonset/agent/restart", {})]


def test_delete_asks_in_the_page_never_with_confirm(view):
    page, rows, sent, _, dialogs = view
    menu_of(page, rows, "web").locator('[data-ct-menu="delete"]').click()
    panel = page.locator(".floating-panel", has_text="cannot be undone")
    expect(panel).to_contain_text("Delete Deployment apps/web on harv-fake? Its pods (1) stop")
    assert sent == []                                                   # rien avant la confirmation
    btn = panel.locator("[data-ct-submit]")
    expect(btn).to_have_class(re.compile("btn-danger"))
    btn.click()
    closed_with_toast(page, panel, "apps/web deleted.")
    assert sent == [("DELETE", "containers/harv-fake/apps/deployment/web", None)]
    assert dialogs == []                                                # jamais de confirm() du navigateur


def create_panel(page):
    page.locator('#containers-host [data-ct="create"]').click()
    panel = page.locator(".floating-panel", has=page.locator('.ct-create-tabs'))
    expect(panel).to_be_visible()
    return panel


def test_create_button_form_has_tooltips_and_the_gpu_hint(view):
    page, _, _, _, _ = view
    btn = page.locator('#containers-host [data-ct="create"]')
    assert btn.get_attribute("data-tip")
    panel = create_panel(page)
    expect(panel.locator("[data-ct-gpu-hint]")).to_have_text("3 of 4 container GPUs free on harv-fake.")
    expect(panel.locator('[name="cluster"]')).to_have_value("harv-fake")
    expect(panel.locator('[name="memory"]')).to_have_value("256Mi")
    missing = panel.evaluate(
        "p => [...p.querySelectorAll('button, select, input, textarea')]"
        ".filter(e => !(e.dataset.tip || e.closest('[data-tip]') || e.title) && e.offsetParent !== null"
        " && !e.closest('.fp-head, .floating-panel-head, header'))"
        ".map(e => e.outerHTML.slice(0, 80))")
    assert missing == []


def test_create_form_validation_sends_nothing(view):
    page, _, sent, _, _ = view
    panel = create_panel(page)
    panel.locator('[name="name"]').fill("Bad_Name")
    panel.locator('[name="namespace"]').fill("apps")
    panel.locator('[name="replicas"]').fill("1001")
    panel.locator('[name="memory"]').fill("lots")
    panel.locator('[name="env"]').fill("NOEQUALS")
    panel.locator("[data-ct-submit]").click()
    msg = panel.locator(".of-msg")
    for text in ("Name: a-z", "An image is required", "Pods: a whole number", "Memory limit:", "Environment:"):
        expect(msg).to_contain_text(text)
    expect(panel.locator('[name="name"]')).to_have_attribute("aria-invalid", "true")
    expect(panel.locator('[name="namespace"]')).not_to_have_attribute("aria-invalid", "true")
    assert sent == []


def test_create_from_the_form(view):
    page, _, sent, _, _ = view
    panel = create_panel(page)
    panel.locator('[name="name"]').fill("trainer")
    panel.locator('[name="namespace"]').fill("ml")
    panel.locator('[name="kind"]').select_option("Job")
    panel.locator('[name="image"]').fill("cuda:12")
    panel.locator('[name="replicas"]').fill("2")
    panel.locator('[name="command"]').fill("python train.py")
    panel.locator('[name="env"]').fill("EPOCHS=3\nLR=0.1")
    panel.locator('[name="gpus"]').fill("1")
    panel.locator('[name="cpu"]').fill("2")
    panel.locator('[name="create_namespace"]').check()
    panel.locator("[data-ct-submit]").click()
    closed_with_toast(page, panel, "created on harv-fake.")
    assert sent == [("POST", "containers/harv-fake/create", {"spec": {
        "name": "trainer", "namespace": "ml", "kind": "Job", "image": "cuda:12", "replicas": 2,
        "command": "python train.py", "env": ["EPOCHS=3", "LR=0.1"], "gpus": 1,
        "gpu_resource": "nvidia.com/gpu", "cpu": "2", "memory": "256Mi"}, "create_namespace": True})]


def test_create_from_yaml_and_a_server_refusal_said(view):
    page, _, sent, _, _ = view
    panel = create_panel(page)
    panel.locator('[data-ct-mode="yaml"]').click()
    expect(panel.locator('[data-ct-pane="form"]')).to_be_hidden()
    panel.locator("[data-ct-submit]").click()
    expect(panel.locator(".of-msg")).to_contain_text("Paste the manifest")
    assert sent == []
    panel.locator('[name="yaml"]').fill("apiVersion: v1\nkind: Pod\nmetadata:\n  name: p\n")
    panel.locator("[data-ct-submit]").click()
    closed_with_toast(page, panel, "created on harv-fake.")
    assert sent[-1] == ("POST", "containers/harv-fake/create",
                        {"yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: p\n", "create_namespace": False})
    # un refus du serveur est dit dans la fenêtre, qui reste ouverte
    panel = create_panel(page)
    panel.locator('[name="name"]').fill("refused")
    panel.locator('[name="namespace"]').fill("apps")
    panel.locator('[name="image"]').fill("nginx")
    panel.locator("[data-ct-submit]").click()
    expect(panel.locator(".of-msg .res-error")).to_contain_text("system namespace")
    expect(panel).to_be_visible()
    expect(panel.locator("[data-ct-submit]")).to_be_enabled()           # on peut corriger et renvoyer
