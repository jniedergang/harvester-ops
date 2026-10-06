"""v1.85.0 : la vue « Toutes les VMs », les VMs de tous les clusters.

L'API est routée : trois clusters, dont un éteint et un qui refuse la
lecture. On vérifie la place dans le menu, les puces de clusters, les
filtres (cluster, état, namespace, texte), le tri, la mémoire des choix, la
sélection qui mêle deux clusters et l'action groupée qui part vers le bon
cluster pour chaque VM, et le menu d'actions ouvert avec le cluster de la
ligne."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


def vm(cluster, ns, name, phase, rs="Always", ip=None, node="n1", mem="4Gi", cpu=2):
    return {"cluster": cluster, "namespace": ns, "name": name, "phase": phase, "runStrategy": rs,
            "agent_connected": "True" if phase == "Running" else "False", "cpu": cpu, "memory": mem,
            "node": node if phase == "Running" else None, "ips": [ip] if ip else [], "labels": {"app": name}}


DATA = {
    "clusters": [{"cluster": "alpha", "state": "ok", "count": 3}, {"cluster": "bravo", "state": "ok", "count": 2},
                 {"cluster": "charlie", "state": "unreachable"}, {"cluster": "delta", "state": "denied",
                                                                  "error": "forbidden"}],
    "vms": [vm("alpha", "default", "web-1", "Running", ip="10.0.0.11", mem="2Gi"),
            vm("alpha", "default", "web-2", "Stopped", rs="Halted", mem="8Gi"),
            vm("alpha", "prod", "db", "Running", ip="10.0.0.9", cpu=8, mem="16Gi"),
            vm("bravo", "default", "api", "Running", ip="10.1.0.4"),
            vm("bravo", "lab", "cirros", "Paused")],
}


@pytest.fixture
def view(context, flask_server):
    context.add_init_script(
        "if (!sessionStorage.getItem('avm-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_tab','allvms');"
        "localStorage.removeItem('harvester_ops_allvms');"
        "sessionStorage.setItem('avm-init','1'); }")
    page = context.new_page()
    sent = []
    page.route("**/api/vms-all", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                         body=json.dumps(DATA)))

    def patch(route, req):
        if req.method == "PATCH":
            sent.append((req.url.split("/api/vm/", 1)[1], req.post_data_json))
            return route.fulfill(status=200, content_type="application/json", body="{}")
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"runStrategy": "Always", "phase": "Running", "volumes": []}))
    page.route(re.compile(r".*/api/vm/[^/]+/[^/]+/[^/]+/(runStrategy|state)$"), patch)
    page.on("dialog", lambda d: d.accept())
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.AllVMs && window.i18n")
    rows = page.locator("#all-vms-host tbody tr")
    expect(rows).to_have_count(5)
    return page, rows, sent


def names(rows):
    return rows.evaluate_all("rs => rs.map(r => r.children[3].textContent.trim())")


def test_virtual_machines_is_a_group_of_two_entries(view):
    """Décision de ju : « Virtual machines » regroupe les VMs du cluster
    choisi (l'onglet d'avant) et celles de tous les clusters."""
    page, _, _ = view
    group = page.locator("#tab-group-vms")
    expect(group.locator(".tab-group-head .sidebar-label")).to_have_text("Virtual machines")
    tabs = group.locator(".tab-child").evaluate_all("els => els.map(e => e.dataset.tab)")
    assert tabs == ["namespaces", "allvms"]
    labels = group.locator(".tab-child .sidebar-label").all_inner_texts()
    assert [x.strip() for x in labels] == ["Current cluster's VMs", "All clusters VMs"]
    assert page.locator('nav > .tab[data-tab="namespaces"]').count() == 0
    expect(group).to_have_class(re.compile("expanded"))
    page.mouse.move(1200, 600)
    page.locator('.tab[data-tab="namespaces"]').click()
    expect(page.locator("#tab-namespaces")).to_have_class(re.compile("active"))


def test_clusters_are_listed_with_their_state(view):
    page, _, _ = view
    chips = page.locator("#all-vms-host [data-avm-cluster]")
    expect(chips).to_have_count(5)                       # « Tous » + 4 clusters
    expect(page.locator('[data-avm-cluster="charlie"] .badge')).to_have_text("unreachable")
    expect(page.locator('[data-avm-cluster="delta"] .badge')).to_have_text("denied")
    expect(page.locator('[data-avm-cluster="alpha"] .avm-chip-n')).to_have_text("3")
    expect(page.locator("#all-vms-host [data-avm=count]")).to_have_text("5 of 5 VMs")


def test_sorted_by_name_then_by_any_column_both_ways(view):
    page, rows, _ = view
    assert names(rows) == ["api", "cirros", "db", "web-1", "web-2"]
    page.click('[data-avm-sort="memory"]')
    assert names(rows) == ["web-1", "api", "cirros", "web-2", "db"]
    page.click('[data-avm-sort="memory"]')
    assert names(rows)[0] == "db"
    expect(page.locator('[data-avm-sort="memory"]')).to_have_attribute("aria-sort", "descending")


def test_filters_by_cluster_state_namespace_and_text_and_remembers_them(view):
    page, rows, _ = view
    page.click('[data-avm-cluster="alpha"]')
    assert names(rows) == ["db", "web-1", "web-2"]
    page.select_option('[data-avm="phase"]', "running")
    assert names(rows) == ["db", "web-1"]
    page.select_option('[data-avm="ns"]', "prod")
    assert names(rows) == ["db"]
    page.select_option('[data-avm="ns"]', "")
    page.fill('[data-avm="text"]', "10.0.0.11")
    assert names(rows) == ["web-1"]
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("window.AllVMs")
    page.mouse.move(1200, 600)                 # loin du menu latéral, qui s'ouvre au survol
    expect(page.locator("#all-vms-host tbody tr")).to_have_count(1)
    expect(page.locator('[data-avm="text"]')).to_have_value("10.0.0.11")
    page.click('[data-avm-cluster="*"]')
    page.select_option('[data-avm="phase"]', "")
    page.fill('[data-avm="text"]', "")
    expect(page.locator("#all-vms-host tbody tr")).to_have_count(5)


def test_a_selection_across_two_clusters_starts_each_vm_on_its_cluster(view):
    page, rows, sent = view
    for n in ("cirros", "web-2"):
        rows.filter(has_text=n).locator("input[type=checkbox]").check()
    expect(page.locator('[data-avm="selcount"]')).to_have_text("2 selected on 2 cluster(s)")
    page.click('[data-avm-bulk="start"]')
    expect(page.locator('[data-avm="log"]')).to_contain_text("alpha/default/web-2")
    assert sorted(sent) == [("alpha/default/web-2/runStrategy", {"runStrategy": "Always"}),
                            ("bravo/lab/cirros/runStrategy", {"runStrategy": "Always"})]


def test_row_gestures_carry_the_row_s_cluster(view):
    page, rows, sent = view
    row = rows.filter(has_text="api")
    row.locator('[data-avm-act="stop"]').click()
    page.wait_for_timeout(300)
    assert sent == [("bravo/default/api/runStrategy", {"runStrategy": "Halted"})]
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "/state" in r.url else None)
    row.locator('[data-avm-act="more"]').click()
    expect(page.locator(".vma-menu")).to_be_visible()
    assert any("/api/vm/bravo/default/api/state" in u for u in calls)
