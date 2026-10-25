"""v1.91.0 : la section Containers, les charges de conteneurs (lecture seule).

L'API est routée : la vue de tous les clusters (trois clusters, dont un éteint
et un qui refuse la lecture), la vue du cluster choisi, le journal d'un pod et
le YAML. On vérifie la place dans le menu, les filtres (cluster, type, état,
namespace, texte), le tri, le dépliage des pods, les journaux (500 lignes au
plus, conteneur au choix), le YAML en lecture seule et un refus dit."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto  # noqa: E402


def pod(name, node="n1", state="Running", ready=True, restarts=0, containers=("app",)):
    return {"name": name, "namespace": "x", "node": node, "phase": "Running", "state": state, "ready": ready,
            "restarts": restarts, "created": "2026-10-17T10:00:00Z", "containers": list(containers),
            "init_containers": []}


def wl(cluster, ns, name, kind, state, ready=1, desired=1, images=("nginx:1",), gpus=0, pods=()):
    return {"cluster": cluster, "namespace": ns, "name": name, "kind": kind, "state": state, "ready": ready,
            "desired": desired, "images": list(images), "gpus": gpus, "created": "2026-10-16T10:00:00Z",
            "labels": {"app": name}, "pods": list(pods)}


ALL = {
    "clusters": [{"cluster": "alpha", "state": "ok", "count": 3}, {"cluster": "bravo", "state": "ok", "count": 1},
                 {"cluster": "charlie", "state": "unreachable"},
                 {"cluster": "delta", "state": "denied", "error": "forbidden"}],
    "workloads": [
        wl("alpha", "apps", "web", "Deployment", "Running", 2, 2,
           pods=[pod("web-1a-aaa", containers=("app", "sidecar")), pod("web-1a-bbb", node="n2", restarts=4)]),
        wl("alpha", "ai", "train", "Job", "Progressing", 0, 1, images=("cuda:12",), gpus=2, pods=[pod("train-x")]),
        wl("alpha", "ops", "backup", "CronJob", "Suspended", 0, None, images=("busybox:1.36",)),
        wl("bravo", "apps", "api", "StatefulSet", "Degraded", 1, 3, pods=[pod("api-0")]),
    ],
}
CURRENT = {"cluster": "harv-fake", "denials": [], "workloads": [
    {k: v for k, v in w.items() if k != "cluster"} for w in ALL["workloads"][:2]]}


@pytest.fixture
def view(context, flask_server):
    context.add_init_script(
        "if (!sessionStorage.getItem('ct-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.removeItem('harvester_ops_allcontainers');"
        "localStorage.removeItem('harvester_ops_containers');"
        "sessionStorage.setItem('ct-init','1'); }")
    page = context.new_page()
    calls = []
    state = {"current": CURRENT, "status": 200}
    page.route("**/api/containers-all", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                               body=json.dumps(ALL)))
    page.route(re.compile(r".*/api/containers/harv-fake(\?.*)?$"),
               lambda r, q: r.fulfill(status=state["status"], content_type="application/json",
                                      body=json.dumps(state["current"])))

    def logs(route, req):
        calls.append(req.url)
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"logs": "line 1\nline 2\n", "tail": 500}))
    page.route(re.compile(r".*/api/containers/[^/]+/[^/]+/pod/[^/]+/logs.*"), logs)

    def yaml(route, req):
        calls.append(req.url)
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"yaml": "kind: Deployment\nmetadata:\n  name: web\n", "writable": False}))
    page.route(re.compile(r".*/api/yaml/.*"), yaml)
    goto(page, flask_server["base_url"], address('allcontainers'))
    page.wait_for_function("window.Containers && window.i18n")
    page.mouse.move(1200, 600)                     # loin du menu latéral, qui s'ouvre au survol
    rows = page.locator("#all-containers-host tbody tr[data-ct-row]")
    expect(rows).to_have_count(4)
    return page, rows, calls, state


def names(rows):
    return rows.evaluate_all("rs => rs.map(r => r.children[3].textContent.trim())")


def test_containers_is_a_group_right_below_virtual_machines(view):
    """2.0.0 : l'entrée racine Conteneurs (toutes les charges) suit
    Machines virtuelles dans le menu des hôtes ; les conteneurs du cluster
    sont l'entrée « Conteneurs » du sous-menu de l'hôte ouvert."""
    page, _, _, _ = view
    roots = page.locator("#host-nav [data-hnav-root]").evaluate_all("els => els.map(e => e.dataset.hnavRoot)")
    assert roots[roots.index("vms") + 1] == "containers"
    root = page.locator('#host-nav [data-hnav-root="containers"]')
    expect(root.locator(".sidebar-label")).to_have_text("Containers")
    assert root.get_attribute("href") == "#containers"
    page.evaluate("Router.go('#harvester/harv-fake')")
    page.evaluate("Router.idle()")
    subs = page.locator('#hnav-families .hnav-sub a').evaluate_all("els => els.map(e => e.dataset.sub)")
    assert subs[subs.index("vms") + 1] == "containers"
    sub = page.locator('#hnav-families .hnav-sub a[data-sub="containers"]')
    assert sub.get_attribute("href") == "#harvester/harv-fake/containers"
    for t in (root, sub):
        assert t.get_attribute("title")                        # bulle d'aide sur chaque entrée


def test_banner_clusters_and_count(view):
    page, _, _, _ = view
    banner = page.locator("#all-containers-host .ct-banner")
    expect(banner).to_contain_text("experimental before Harvester 1.9.1")
    expect(banner).to_contain_text("System namespaces are hidden")
    expect(banner).not_to_contain_text("read-only")                # v1.93.0 : les gestes sont là
    expect(page.locator("#all-containers-host [data-ct-cluster]")).to_have_count(5)
    expect(page.locator('#all-containers-host [data-ct-cluster="charlie"] .badge')).to_have_text("unreachable")
    expect(page.locator('#all-containers-host [data-ct-cluster="delta"] .badge')).to_have_text("denied")
    expect(page.locator("#all-containers-host [data-ct=count]")).to_have_text("4 of 4 workloads")


def test_every_control_has_a_tooltip(view):
    page, _, _, _ = view
    missing = page.locator("#all-containers-host").evaluate(
        "h => [...h.querySelectorAll('button, select, input, th.sortable')]"
        ".filter(e => !(e.dataset.tip || e.closest('[data-tip]') || e.title)).map(e => e.outerHTML.slice(0, 80))")
    assert missing == []


def test_sort_and_filters(view):
    page, rows, _, _ = view
    assert names(rows) == ["api", "backup", "train", "web"]
    page.click('#all-containers-host [data-ct-sort="gpus"]')
    page.click('#all-containers-host [data-ct-sort="gpus"]')
    assert names(rows)[0] == "train"
    page.click('#all-containers-host [data-ct-cluster="alpha"]')
    expect(rows).to_have_count(3)
    page.select_option('#all-containers-host [data-ct="kind"]', "CronJob")
    assert names(rows) == ["backup"]
    page.select_option('#all-containers-host [data-ct="kind"]', "")
    page.select_option('#all-containers-host [data-ct="state"]', "Progressing")
    assert names(rows) == ["train"]
    page.select_option('#all-containers-host [data-ct="state"]', "")
    page.select_option('#all-containers-host [data-ct="ns"]', "apps")
    assert names(rows) == ["web"]
    page.select_option('#all-containers-host [data-ct="ns"]', "")
    page.fill('#all-containers-host [data-ct="text"]', "cuda")
    assert names(rows) == ["train"]
    page.fill('#all-containers-host [data-ct="text"]', "web-1a-bbb")       # un nom de pod
    assert names(rows) == ["web"]


def test_unfold_a_row_shows_its_pods(view):
    page, rows, _, _ = view
    row = rows.filter(has_text="web")
    row.locator('[data-ct-act="expand"]').click()
    pods = page.locator("#all-containers-host .ct-pods tbody tr")
    expect(pods).to_have_count(2)
    expect(pods.nth(1)).to_contain_text("n2")
    expect(pods.nth(1)).to_contain_text("4")
    expect(row.locator('[data-ct-act="expand"]')).to_have_attribute("aria-expanded", "true")
    row.locator('[data-ct-act="expand"]').click()
    expect(page.locator("#all-containers-host .ct-pods")).to_have_count(0)


def test_pod_logs_with_a_container_choice(view):
    page, rows, calls, _ = view
    rows.filter(has_text="web").locator('[data-ct-act="expand"]').click()
    page.locator('#all-containers-host [data-ct-pod="web-1a-aaa"] [data-ct-act="podlogs"]').click()
    panel = page.locator(".ct-logs")
    expect(panel.locator(".vma-logs-text")).to_contain_text("line 2")
    assert panel.locator('[name="container"] option').all_inner_texts() == ["app", "sidecar"]
    assert panel.locator('[name="tail"] option').all_inner_texts() == ["100", "200", "500"]
    assert any("/api/containers/alpha/apps/pod/web-1a-aaa/logs?tail=500&container=app" in u for u in calls)
    panel.locator('[name="container"]').select_option("sidecar")
    page.wait_for_timeout(300)
    assert "container=sidecar" in calls[-1]
    panel.locator('[name="pod"]').select_option("web-1a-bbb")
    page.wait_for_timeout(300)
    assert "/pod/web-1a-bbb/logs" in calls[-1]


def test_row_menu_yaml_is_read_only(view):
    page, rows, calls, _ = view
    rows.filter(has_text="web").locator('[data-ct-act="more"]').click()
    menu = page.locator(".ct-menu")
    expect(menu).to_be_visible()
    expect(menu.locator('[data-ct-menu="logs"]')).not_to_have_attribute("aria-disabled", "true")
    menu.locator('[data-ct-menu="yaml"]').click()
    expect(page.locator(".yw-text")).to_have_value(re.compile("kind: Deployment"))
    expect(page.locator(".yw-ro")).to_be_visible()
    expect(page.locator('[data-yw="save"]')).to_be_hidden()
    assert any("/api/yaml/alpha/deployment/web?namespace=apps" in u for u in calls)
    # une charge sans pod : l'entrée des journaux est grisée
    rows.filter(has_text="backup").locator('[data-ct-act="more"]').click()
    expect(page.locator('.ct-menu [data-ct-menu="logs"]')).to_have_attribute("aria-disabled", "true")


def test_current_cluster_view_and_a_refusal_said(view):
    page, _, _, state = view
    # 2.0.0 : les conteneurs du cluster, par la page de l'hôte
    page.evaluate("Router.go('#harvester/harv-fake/containers')")
    rows = page.locator("#containers-host tbody tr[data-ct-row]")
    expect(rows).to_have_count(2)
    expect(page.locator("#containers-host [data-ct-cluster]")).to_have_count(0)     # pas de puces
    expect(page.locator("#containers-host [data-ct=notice]")).to_be_hidden()
    state["current"], state["status"] = {"error": "cluster refused"}, 403
    page.click('#containers-host [data-ct="refresh"]')
    expect(page.locator("#containers-host [data-ct=notice]")).to_contain_text("may not read the workloads")
    expect(rows).to_have_count(0)
    expect(page.locator("#containers-host [data-ct=empty]")).to_be_hidden()     # « aucune charge » serait faux
