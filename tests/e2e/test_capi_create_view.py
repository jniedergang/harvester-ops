"""v1.48.0 : la création d'un cluster RKE2, dans un navigateur.

v1.53.0 : une fenêtre à menus ouverte depuis Clusters K8S (demande de
l'exploitant), l'essentiel au premier menu.

Demandé par l'exploitant : un maximum de choix proposés, une explication au
survol, l'essentiel regroupé, les calculs d'adresses suggérés, toutes les
options du fournisseur mais les étendues repliées. Le réseau est intercepté :
relevés, contrôle, aperçu et flux de l'action sont simulés.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

GIB = 1024 ** 3
POOL_A = {"name": "capi-vm-pool", "subnet": "172.16.0.0/16", "gateway": "172.16.0.1",
          "mask": "255.255.0.0", "total": 16, "available": 13, "used": [],
          "ranges": [{"subnet": "172.16.0.0/16", "gateway": "172.16.0.1",
                      "start": "172.16.3.44", "end": "172.16.3.49", "size": 6},
                     {"subnet": "172.16.0.0/16", "gateway": "172.16.0.1",
                      "start": "172.16.3.60", "end": "172.16.3.69", "size": 10}]}
POOL_B = {"name": "lab-pool", "subnet": "10.20.0.0/24", "gateway": "10.20.0.254",
          "mask": "255.255.255.0", "total": 50, "available": 50, "used": [],
          "ranges": [{"subnet": "10.20.0.0/24", "gateway": "10.20.0.254",
                      "start": "10.20.0.10", "end": "10.20.0.59", "size": 50}]}
INVENTORY = {
    "unreachable": False,
    "stack": {"ready": True, "turtles": True, "missing": [],
              "providers": [{"name": "rke2", "type": "bootstrap", "version": "v0.25.2"},
                            {"name": "harvester", "type": "infrastructure", "version": "v0.10.1"}]},
    "namespaces": ["default", "kube-system", "apps"], "clusters": [],
    "images": [
        {"ref": "default/sles15-sp7-minimal-vm.x86_64-cloud-qu2.qcow2",
         "display_name": "sles15-sp7-minimal-vm.x86_64-cloud-qu2.qcow2", "ready": True,
         "iso": False, "virtual_size": 25 * GIB, "os": "sles"},
        {"ref": "default/Rocky-9-GenericCloud.qcow2", "display_name": "Rocky-9-GenericCloud.qcow2",
         "ready": True, "iso": False, "os": "rocky"},
        {"ref": "default/harvester-v1.8.2.iso", "display_name": "harvester-v1.8.2.iso",
         "ready": True, "iso": True, "os": None},
        {"ref": "default/pulling.qcow2", "display_name": "pulling.qcow2", "ready": False,
         "iso": False, "os": None}],
    "keypairs": ["default/capi-ssh-key"],
    "networks": [{"ref": "default/management", "vlan": None},
                 {"ref": "default/production", "vlan": 124}],
    "storage_classes": [{"name": "harv-rep1", "default": True}, {"name": "longhorn", "default": False}],
    "pools": [POOL_A, POOL_B],
    "versions": [{"version": "v1.34.11", "tested": True}, {"version": "v1.36.4", "tested": False}],
    "version_names": ["v1.34.11", "v1.36.4"], "default_version": "v1.34.11",
    "ssh_users": {"sles": "sles", "rocky": "rocky"},
    "free_cpu_m": 848, "free_memory": 40 * GIB, "overcommit": {"cpu": 1600, "memory": 150},
}


def fulfill(route, body, status=200, ctype="application/json"):
    route.fulfill(status=status, content_type=ctype,
                  body=body if isinstance(body, str) else json.dumps(body))


def sse(events):
    return "".join(f"event: {t}\ndata: {json.dumps(d)}\n\n" for t, d in events)


@pytest.fixture
def form(context, flask_server):
    def make(inventory=INVENTORY, check=None, stream=(), ready=True):
        context.add_init_script("localStorage.setItem('harvester_ops_language','fr');"
                                "localStorage.removeItem('harvester_ops_capi_dns');")
        page = context.new_page()
        calls = {"check": [], "create": [], "preview": []}

        def on_check(route, request):
            calls["check"].append(json.loads(request.post_data or "{}"))
            fulfill(route, check or {"blocked": False, "findings": [
                {"code": "cp-single", "level": "warn", "facts": {}},
                {"code": "endpoint-dhcp", "level": "ok", "facts": {}}]})

        def on_create(route, request):
            calls["create"].append(json.loads(request.post_data or "{}"))
            fulfill(route, {"action_id": "capi00000001", "cluster": "web/web"}, 201)

        def on_preview(route, request):
            calls["preview"].append(json.loads(request.post_data or "{}"))
            fulfill(route, {"yaml": "apiVersion: cluster.x-k8s.io/v1beta1\nkind: Cluster\n"})

        page.route("**/api/capi/harv-fake/inventory*", lambda r, q: fulfill(r, inventory))
        page.route("**/api/capi/harv-fake/cluster-check", on_check)
        page.route("**/api/capi/harv-fake/cluster-create", on_create)
        page.route("**/api/capi/harv-fake/cluster-preview", on_preview)
        page.route("**/api/stream/capi00000001",
                   lambda r, q: fulfill(r, sse(list(stream)), ctype="text/event-stream"))
        page.goto(flask_server["base_url"], wait_until="domcontentloaded")
        page.wait_for_function("window.CapiCreate && window.XferProgress && window.i18n")
        page.evaluate("CapiCreate.open('harv-fake')")
        host = page.locator("#fp-capi-create")
        if ready:
            expect(host.locator('[data-x="name"]')).to_be_visible(timeout=5000)
        return page, host, calls
    return make


def x(host, key):
    return host.locator(f'[data-x="{key}"]')


def menu(host, sec):
    host.locator(f'.vm-edit-nav [data-sec="{sec}"]').click()
    expect(host.locator(f'.capi-sec[data-sec="{sec}"]')).to_be_visible()


def test_the_essentials_are_filled_from_the_cluster(form):
    page, host, _ = form()
    # images : ni ISO, ni image pas prête
    opts = x(host, "image").locator("option").all_inner_texts()
    assert any("sles15-sp7" in o for o in opts) and any("Rocky-9" in o for o in opts)
    assert not any(".iso" in o or "pulling" in o for o in opts)
    # version par défaut, marquée testée
    expect(x(host, "k8s_version")).to_have_value("v1.34.11")
    assert "testée" in x(host, "k8s_version").locator("option").first.inner_text()
    # réseau : la production d'abord proposée, avec son VLAN
    expect(x(host, "network")).to_have_value("default/production")
    assert "VLAN 124" in x(host, "network").inner_text()
    assert "172.16.3.44-49, 172.16.3.60-69, 13 libres" in x(host, "ip_pool").inner_text()
    # l'essentiel au premier menu, le reste rangé : les autres menus fermés
    expect(host.locator('.capi-sec[data-sec="network"]')).to_be_hidden()
    navs = host.locator(".vm-edit-nav [data-sec]")
    assert navs.count() == 7 and all(b.get_attribute("data-tip") for b in navs.all())
    expect(x(host, "summary")).to_contain_text("2 VM : 4 vCPU")
    # passerelle et masque déduits du pool, au menu Réseau
    menu(host, "network")
    expect(x(host, "gateway")).to_have_value("172.16.0.1")
    expect(x(host, "subnet_mask")).to_have_value("255.255.0.0")
    expect(x(host, "gw-derived")).to_be_visible()
    # chaque contrôle s'explique
    for el in host.locator("input[data-x], select[data-x]").all():
        if el.get_attribute("type") == "checkbox":
            continue
        assert el.get_attribute("data-tip"), el.get_attribute("data-x")


def test_the_pool_fills_gateway_and_mask_and_says_so(form):
    page, host, _ = form()
    x(host, "ip_pool").select_option("lab-pool")
    menu(host, "network")
    expect(x(host, "gateway")).to_have_value("10.20.0.254")
    expect(x(host, "subnet_mask")).to_have_value("255.255.255.0")
    x(host, "gateway").fill("10.20.0.1")
    expect(x(host, "gw-derived")).to_be_hidden()
    expect(x(host, "mask-derived")).to_be_visible()


def test_presets_and_image_suggest_values(form):
    page, host, _ = form()
    x(host, "preset").select_option("medium")
    x(host, "image").select_option("default/Rocky-9-GenericCloud.qcow2")
    menu(host, "nodes")
    expect(x(host, "cpu")).to_have_value("4")
    expect(x(host, "memory")).to_have_value("8Gi")
    expect(x(host, "ssh_user")).to_have_value("rocky")
    x(host, "cpu").fill("6")
    expect(x(host, "preset")).to_have_value("custom")


def test_the_check_sends_the_request_and_explains_in_french(form):
    page, host, calls = form(check={"blocked": True, "findings": [
        {"code": "pool-short", "level": "block", "facts": {"needed": 6, "available": 3}},
        {"code": "memory-short", "level": "warn", "facts": {"needed": 8 * GIB, "free": 4 * GIB}}]})
    x(host, "name").fill("Web Prod")
    x(host, "name").dispatch_event("change")
    expect(x(host, "name")).to_have_value("web-prod")          # nom rendu valide en tapant
    # l'état du contrôle dans la barre, et un compteur sur chaque menu concerné
    expect(x(host, "check-status")).to_contain_text("1 blocage(s), 1 avertissement(s)", timeout=5000)
    expect(host.locator('[data-count="essentials"]')).to_have_text("1")
    expect(host.locator('[data-count="nodes"]')).to_have_text("1")
    assert "is-block" in host.locator('[data-count="essentials"]').get_attribute("class")
    x(host, "check-status").click()                            # mène au détail
    expect(host.locator('.capi-sec[data-sec="check"]')).to_be_visible()
    report = x(host, "report")
    expect(report).to_contain_text("Pas assez d'adresses : 6 nécessaires", timeout=5000)
    expect(report).to_contain_text("8,0 Gio")
    expect(x(host, "create")).to_be_disabled()
    body = calls["check"][-1]
    assert body["name"] == "web-prod" and body["network"] == "default/production"
    assert body["gateway"] == "172.16.0.1" and "preset" not in body
    assert "extra_disk_class" not in body                      # pas de disque, pas de classe


def test_advanced_options_reach_the_request(form):
    page, host, calls = form()
    x(host, "name").fill("web")
    menu(host, "network")
    x(host, "ip_pool_refs").select_option(["lab-pool"])
    menu(host, "storage")
    x(host, "extra_disk_size").fill("20Gi")
    menu(host, "integrations")
    x(host, "rancher_import").check()
    menu(host, "kubernetes")
    x(host, "cni").select_option("cilium")
    menu(host, "check")
    expect(x(host, "report")).to_contain_text("Aucun blocage", timeout=5000)
    body = calls["check"][-1]
    assert body["ip_pool_refs"] == ["capi-vm-pool", "lab-pool"]   # le principal d'abord
    assert body["extra_disk_size"] == "20Gi" and body["extra_disk_class"] == "harv-rep1"
    assert body["rancher_import"] is True and body["cni"] == "cilium"


def test_preview_shows_the_manifests(form):
    page, host, calls = form()
    x(host, "name").fill("web")
    x(host, "preview").click()
    pre = page.locator("#fp-capi-preview-harv-fake .capi-yaml")
    expect(pre).to_contain_text("kind: Cluster", timeout=5000)
    assert calls["preview"][-1]["name"] == "web"


def test_create_follows_the_action_until_available(form):
    page, host, calls = form(stream=[
        ("step", {"type": "step", "step_id": "check", "status": "done", "message": "2 VM(s)"}),
        ("step", {"type": "step", "step_id": "provision", "status": "running",
                  "message": "infrastructure ready, control plane 1/1, workers 0/1 (70%)"}),
        ("end", {"status": "done", "result": {}}),
    ])
    page.on("dialog", lambda d: d.accept())
    x(host, "name").fill("web")
    x(host, "name").dispatch_event("change")
    expect(x(host, "create")).to_be_enabled(timeout=5000)
    x(host, "create").click()
    expect(x(host, "feedback")).to_contain_text("capi00000001", timeout=5000)
    expect(x(host, "live-line")).to_contain_text("Le cluster web/web est disponible", timeout=5000)
    link = x(host, "kubeconfig")
    expect(link).to_have_attribute("href", "/api/capi/harv-fake/cluster/web/web/kubeconfig")
    assert calls["create"][-1]["name"] == "web"


def test_progress_lines_are_translated():
    """Le script écrit en anglais (le dock l'affiche tel quel) ; la page le
    redit dans la langue de l'interface."""
    import subprocess
    js = """
      global.window = global; global.i18n = { t: (k, v) => k + JSON.stringify(v || {}) };
      require('./web/static/js/xfer-progress.js'); require('./web/static/js/capi-create.js');
      const r = CapiCreate._stepText({ step_id: 'provision', status: 'running',
        message: 'infrastructure pending, control plane 0/3, workers 0/2 (10%)' });
      console.log(JSON.stringify(r));"""
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=str(
        __import__("pathlib").Path(__file__).resolve().parent.parent.parent))
    if out.returncode != 0:
        pytest.skip("node unavailable: " + out.stderr[:200])
    got = json.loads(out.stdout)
    assert got["pct"] == 10 and "capi.new.prog.line" in got["text"]
    assert '"cp":"0/3"' in got["text"] and '"workers":"0/2"' in got["text"]


def test_an_unreachable_cluster_says_so(form):
    page, host, _ = form(inventory=dict(INVENTORY, unreachable=True), ready=False)
    expect(host).to_contain_text("Ce cluster ne répond pas", timeout=5000)
    assert host.locator('[data-x="name"]').count() == 0


def test_a_shared_namespace_is_refused(form):
    page, host, _ = form(check={"blocked": True, "findings": [
        {"code": "namespace-has-cluster", "level": "block",
         "facts": {"namespace": "apps", "cluster": "apps/web"}}]})
    x(host, "name").fill("api")
    x(host, "name").dispatch_event("change")
    menu(host, "check")
    expect(x(host, "report")).to_contain_text("contient déjà le cluster apps/web", timeout=5000)
    expect(x(host, "create")).to_be_disabled()


def test_an_unready_stack_points_to_installation(form):
    inv = dict(INVENTORY, stack={"ready": False, "turtles": True,
                                 "missing": ["infrastructure/harvester"], "providers": []})
    page, host, _ = form(inventory=inv)
    note = x(host, "stack-note")
    expect(note).to_contain_text("infrastructure/harvester")
    assert note.locator('[data-x="go-install"]').get_attribute("data-tip")


def test_the_window_opens_from_the_k8s_clusters_tab_and_minimises(context, flask_server):
    """Demandé par l'exploitant : plus de sous-onglet de création, un bouton
    dans Clusters K8S qui ouvre une fenêtre qu'on replie dans la barre."""
    context.add_init_script("""localStorage.setItem('harvester_ops_language','fr');
      localStorage.setItem('harvester_ops_current_tab','automation');
      localStorage.setItem('harvester_ops_automation_subtab','capi');
      localStorage.setItem('harvester_ops_capi_subtab','clusters');""")      # ancien choix mémorisé
    page = context.new_page()
    page.route("**/api/capi/harv-fake/inventory*", lambda r, q: fulfill(r, INVENTORY))
    page.route("**/api/capi/harv-fake/diag*", lambda r, q: fulfill(r, {"capi_clusters": []}))
    page.route("**/api/capi/harv-fake/cluster-check", lambda r, q: fulfill(r, {"blocked": False, "findings": []}))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.CapiCreate && window.CAPI && window.i18n")
    page.evaluate("document.querySelector('.tab[data-tab=automation]')?.click()")
    assert page.locator('#tab-automation .sub-tab[data-capi-tab="clusters"]').count() == 0
    expect(page.locator('#tab-automation .sub-tab[data-capi-tab="k8s"]')).to_have_class("sub-tab active")
    btn = page.locator("#btn-capi-create")
    expect(btn).to_be_visible()
    assert btn.get_attribute("data-tip")
    btn.click()
    win = page.locator("#fp-capi-create")
    expect(win.locator('[data-x="name"]')).to_be_visible(timeout=5000)
    win.locator('[data-x="name"]').fill("web")
    page.evaluate("FloatingPanels.minimize('capi-create')")
    expect(win).to_be_hidden()
    btn.click()                                        # la même fenêtre revient, saisie gardée
    expect(win).to_be_visible()
    expect(win.locator('[data-x="name"]')).to_have_value("web")


def test_a_refused_field_counts_on_its_own_menu(form):
    """Vu à l'essai : le refus du DNS vide se comptait sur l'Essentiel alors
    que le champ est au menu Réseau."""
    page, host, _ = form(check={"blocked": True, "findings": [
        {"code": "invalid", "level": "block", "facts": {"option": "dns", "message": "at least one DNS server"}}]})
    x(host, "name").fill("web")
    x(host, "name").dispatch_event("change")
    expect(host.locator('[data-count="network"]')).to_have_text("1", timeout=5000)
    expect(host.locator('[data-count="essentials"]')).to_be_hidden()
