"""2.4.0 : formulaires de déclaration Terraform pour le réseau de VM, la
classe de stockage et le pool d'adresses, dans un navigateur.

Chaque formulaire est rempli comme le ferait l'exploitant (champs, lignes
clé-valeur, blocs répétés), enregistré, puis l'onglet Code montre le HCL que
la console a généré depuis le magasin du serveur de test. Les listes du
cluster (espaces de noms, réseaux) sont simulées : pas de cluster ici.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import settle  # noqa: E402


def fulfill(route, body):
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def open_view(page, lang="en"):
    page.route("**/api/namespaces/harv-fake", lambda r, q: fulfill(r, [{"name": "default"}, {"name": "lab"}]))
    page.route("**/api/networks/harv-fake",
               lambda r, q: fulfill(r, [{"name": "vlan42", "namespace": "default"}]))
    page.evaluate(f"""async () => {{
      localStorage.setItem('harvester_ops_language', '{lang}');
      localStorage.setItem('harvester_ops_automation_subtab', 'terraform');
      localStorage.removeItem('harvester_ops_tfd_tab');
      localStorage.removeItem('harvester_ops_tf_active_decl');
      const d = await fetch('/api/tf-declarations').then(r => r.json());
      for (const x of d.declarations || []) await fetch('/api/tf-declarations/' + x.id, {{ method: 'DELETE' }});
    }}""")
    page.evaluate("history.replaceState(null, '', '#automation/terraform')")
    page.reload()
    settle(page)
    page.wait_for_function("window.TFDecl && window.TFDeclView && window.TF")
    page.evaluate("window.TFDecl.ready")
    page.locator('#host-nav [data-hnav-root="automation"][data-hnav-tab="terraform"]').click()
    page.wait_for_selector("#tf-decls-view .tfd", timeout=8000)


def seed(page, name):
    return page.evaluate("""async (name) => {
      const d = await fetch('/api/tf-declarations', { method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ cluster: 'harv-fake', name, resources: [] }) }).then(r => r.json());
      await window.TFDecl.load();
      window.TFDecl.setActive(d.id);
      window.TFDeclView.render();
      return d;
    }""", name)


def add(page, kind):
    page.click(f'.tfd-add-kind[data-kind="{kind}"]')
    page.wait_for_selector(".tfd-edit .tfd-sec", timeout=5000)
    # chaque contrôle a sa bulle d'aide
    assert all(lab.get_attribute("data-tip") for lab in page.locator(".tfd-sec .tf-label").all())


def sec(page, sid):
    return page.locator(f'.tfd-sec[data-sec="{sid}"]')


def save_and_read_code(page, decl_id, filename):
    expect(page.locator("[data-x='check'] .tfd-check-row.fail")).to_have_count(0, timeout=5000)
    page.click(".tfd-save")
    page.evaluate(f"window.TFDecl.flush('{decl_id}')")
    page.wait_for_timeout(300)
    saved = page.evaluate(f"fetch('/api/tf-declarations/{decl_id}').then(r => r.json())")
    page.click('[data-tfd-tab="code"]')
    f = page.locator(".tfd-file", has_text=filename)
    expect(f).to_be_visible(timeout=5000)
    expect(page.locator(".tfd-notice")).to_have_count(0)
    return saved["resources"][0]["spec"], f.locator("pre").inner_text()


def test_network_form_generates_its_code(page):
    open_view(page)
    d = seed(page, "net")
    add(page, "network")
    g = sec(page, "specs")
    g.locator('[name="name"]').fill("vlan42")
    g.locator('[name="vlan_id"]').fill("42")
    g.locator('[name="description"]').fill('site "A"')
    # étiquettes : une ligne clé-valeur, avec sa bulle
    g.locator(".tf-map-add").click()
    row = g.locator(".tf-map-row").last
    assert row.locator(".tf-map-key").get_attribute("data-tip")
    assert row.locator(".tf-map-remove").get_attribute("data-tip")
    row.locator(".tf-map-key").fill("site")
    row.locator(".tf-map-val").fill("a")
    r = sec(page, "route")
    r.locator('[name="route_mode"]').select_option("manual")
    # mode manual : sous-réseau et passerelle deviennent obligatoires
    assert r.locator('[name="route_cidr"]').evaluate("el => el.required")
    r.locator('[name="route_cidr"]').fill("172.16.42.0/24")
    r.locator('[name="route_gateway"]').fill("172.16.42.1")
    r.locator('[name="route_gateway"]').dispatch_event("input")
    spec, code = save_and_read_code(page, d["id"], "vlan42.tf")
    assert spec["vlan_id"] == 42 and spec["labels"] == {"site": "a"} and spec["cluster_network_name"] == "mgmt"
    for line in ('resource "harvester_network" "vlan42"', 'cluster_network_name = "mgmt"',
                 "vlan_id              = 42", 'description = "site \\"A\\""', 'route_mode = "manual"',
                 'route_cidr = "172.16.42.0/24"', 'route_gateway = "172.16.42.1"', '"site" = "a"'):
        assert line in code, (line, code)


def test_storage_class_form_proposes_migratable_and_generates_its_code(page):
    open_view(page)
    d = seed(page, "sc")
    add(page, "storageclass")
    g = sec(page, "specs")
    keys = [k.input_value() for k in g.locator(".tf-map-key").all()]
    assert keys[0] == "migratable" and g.locator(".tf-map-val").first.input_value() == "true"
    assert g.locator('[name="is_default"]').get_attribute("type") == "checkbox"
    tip = g.locator('label[for="tf-is_default"]').get_attribute("data-tip")
    assert "only one" in tip.lower()
    g.locator('[name="name"]').fill("fast")
    # retirer staleReplicaTimeout, passer à 2 répliques, ajouter un paramètre
    g.locator(".tf-map-row", has=page.locator('input.tf-map-key[value="staleReplicaTimeout"]')) \
        .locator(".tf-map-remove").click()
    g.locator(".tf-map-row", has=page.locator('input.tf-map-key[value="numberOfReplicas"]')) \
        .locator(".tf-map-val").fill("2")
    g.locator(".tf-map-add").click()
    g.locator(".tf-map-row").last.locator(".tf-map-key").fill("diskSelector")
    g.locator(".tf-map-row").last.locator(".tf-map-val").fill("ssd")
    g.locator('[name="reclaim_policy"]').select_option("Retain")
    t = sec(page, "topology")
    t.locator(".tf-block-add").click()
    t.locator('[name$=".key"]').fill("topology.kubernetes.io/zone")
    t.locator('[name$=".values"]').fill("a, b")
    t.locator('[name$=".values"]').dispatch_event("input")
    spec, code = save_and_read_code(page, d["id"], "fast.tf")
    assert spec["parameters"] == {"migratable": "true", "numberOfReplicas": "2", "diskSelector": "ssd"}
    assert spec["is_default"] is False and spec["allow_volume_expansion"] is True
    for line in ('resource "harvester_storageclass" "fast"', 'reclaim_policy = "Retain"',
                 "allow_volume_expansion = true", "is_default = false", '"migratable" = "true"',
                 '"numberOfReplicas" = "2"', '"diskSelector" = "ssd"', "allowed_topologies {",
                 'key    = "topology.kubernetes.io/zone"', 'values = ["a", "b"]'):
        assert line in code, (line, code)
    assert "staleReplicaTimeout" not in code


def test_ip_pool_form_generates_its_code(page):
    open_view(page)
    d = seed(page, "pool")
    add(page, "ippool")
    sec(page, "specs").locator('[name="name"]').fill("pool-a")
    rg = sec(page, "ranges")
    item = rg.locator(".tf-block-item").first
    for field, value in (("start", "10.0.0.10"), ("end", "10.0.0.20"), ("subnet", "10.0.0.0/24"),
                         ("gateway", "10.0.0.1")):
        item.locator(f'[name$=".{field}"]').fill(value)
    rg.locator(".tf-block-add").click()
    second = rg.locator(".tf-block-item").nth(1)
    for field, value in (("start", "10.0.1.10"), ("end", "10.0.1.11"), ("subnet", "10.0.1.0/24"),
                         ("gateway", "10.0.1.1")):
        second.locator(f'[name$=".{field}"]').fill(value)
    s = sec(page, "selector")
    s.locator(".tf-block-add").click()
    expect(s.locator('select[name$=".network"] option[value="default/vlan42"]')).to_have_count(1, timeout=5000)
    s.locator('select[name$=".network"]').select_option("default/vlan42")
    s.locator('[name$=".priority"]').fill("5")
    sc = sec(page, "scope")
    sc.locator(".tf-block-add").click()
    sc.locator('[name$=".namespace"]').fill("default")
    sc.locator('[name$=".namespace"]').dispatch_event("input")
    spec, code = save_and_read_code(page, d["id"], "pool_a.tf")
    assert spec["selector"] == [{"network": "default/vlan42", "priority": 5}]
    assert spec["scope"] == [{"namespace": "default"}] and len(spec["range"]) == 2
    assert code.count("range {") == 2
    for line in ('resource "harvester_ippool" "pool_a"', 'start   = "10.0.0.10"', 'gateway = "10.0.1.1"',
                 "selector {", 'network  = "default/vlan42"', "priority = 5", "scope {",
                 'namespace = "default"'):
        assert line in code, (line, code)


def test_a_gateway_outside_its_subnet_is_told_in_the_code_tab(page):
    """Le refus du serveur arrive avec sa raison (la console ne génère rien
    pour la ressource refusée)."""
    open_view(page)
    d = page.evaluate("""async () => {
      const d = await fetch('/api/tf-declarations', { method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ cluster: 'harv-fake', name: 'bad', resources: [{ id: 'abcabcabcabc', kind: 'ippool',
          spec: { name: 'p', range: [{ start: '10.0.0.10', end: '10.0.0.20', subnet: '10.0.0.0/24',
                                       gateway: '10.9.0.1' }] } }] }) }).then(r => r.json());
      await window.TFDecl.load(); window.TFDecl.setActive(d.id); window.TFDeclView.render(); return d;
    }""")
    out = page.evaluate(f"fetch('/api/tf-declarations/{d['id']}/code').then(r => r.json())")
    assert out["errors"][0]["error"] == "range #1: gateway 10.9.0.1 is not inside subnet 10.0.0.0/24"
    page.click('[data-tfd-tab="code"]')
    expect(page.locator(".tfd-notice")).to_be_visible(timeout=5000)


def test_the_forms_speak_french(page):
    open_view(page, "fr")
    seed(page, "fr")
    expect(page.locator('.tfd-add-kind[data-kind="storageclass"]')).to_contain_text("Classe de stockage")
    expect(page.locator('.tfd-add-kind[data-kind="network"]')).to_contain_text("Réseau de VM")
    page.click('.tfd-add-kind[data-kind="ippool"]')
    page.wait_for_selector(".tfd-edit .tfd-sec", timeout=5000)
    expect(sec(page, "ranges").locator("legend").first).to_contain_text("Plages")
    expect(sec(page, "ranges").locator(".tf-label").first).to_contain_text("Première adresse")
