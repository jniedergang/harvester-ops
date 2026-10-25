"""v1.72.0 : les projets Rancher dans la fenêtre Namespaces : colonne Projet
(nom du projet, annotation d'un autre cluster signalée), onglet Projets
(quotas, protection des projets Default/System et peuplés), formulaire de
projet, déplacement d'un namespace, quota du namespace borné par son projet,
et le compte local qui voit le rangement sans pouvoir le changer."""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402
from nav import address, goto  # noqa: E402

CID = "c-sg2q6"
TEAM = {"id": "p-abcde", "full_id": f"{CID}:p-abcde", "name": "Team A", "description": "web", "quota": {"limitsCpu": "4000m"},
        "used": {"limitsCpu": "2000m"}, "ns_default": {"limitsCpu": "2000m"}, "container": {}, "state": "active",
        "system": False, "default": False}
DEFAULT = {"id": "p-nc8d2", "full_id": f"{CID}:p-nc8d2", "name": "Default", "description": "", "quota": {}, "used": {},
           "ns_default": {}, "container": {}, "state": "active", "system": False, "default": True}


def row(name, project, system=False):
    return {"name": name, "system": system, "phase": "Active", "description": "", "labels": {}, "annotations": {},
            "created": "2026-09-01T10:00:00Z", "vms": 0, "volumes": 0, "snapshot_quota": None, "project": project}


def pr(state, pid=None, cluster=CID, name=None, quota=None):
    return {"cluster": cluster, "project": pid, "state": state, "name": name, "quota": quota or {}, "quota_ok": None, "quota_message": ""}


MANAGED = {"items": [row("apps", pr("member", "p-abcde", name="Team A", quota={"limitsCpu": "2000m"})),
                     row("default", pr("foreign", "p-kcgzz", cluster="c-qt5jz")),
                     row("loose", pr("none"))],
           "projects": {"managed": True, "cid": CID, "items": [DEFAULT, TEAM]},
           "quota_keys": ["limitsCpu", "limitsMemory", "requestsStorage", "pods"],
           "limit_keys": ["requestsCpu", "requestsMemory", "limitsCpu", "limitsMemory"]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def setup(context, flask_server, data):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    sent = []

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "pj0000000172"}, 202)
    page.route("**/api/ns-admin/harv-fake", lambda r, q: fulfill(r, data))
    page.route("**/api/projects/harv-fake/do/**", writes)
    page.route("**/api/stream/pj0000000172", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address("namespaces"))
    page.wait_for_function("window.Namespaces && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    page.evaluate("Namespaces.open('harv-fake')")
    w = page.locator(".floating-panel", has=page.locator(".nsw-win")).last
    return page, w, sent


def test_namespaces_show_their_project_and_a_stale_annotation(context, flask_server):
    page, w, sent = setup(context, flask_server, MANAGED)
    expect(w.locator('tr[data-ns="apps"] .badge.info')).to_have_text("Team A", timeout=8000)
    stale = w.locator('tr[data-ns="default"] .badge.warn')
    expect(stale).to_have_text("project of another cluster")
    assert "c-qt5jz" in stale.get_attribute("data-tip")
    w.locator('tr[data-ns="loose"] [data-nsw-act="move"]').click()
    w.locator('.nsw-form [name="project"]').select_option("p-abcde")
    w.locator('.nsw-form button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/projects/harv-fake/do/move", {"namespace": "loose", "id": "p-abcde"})


def test_a_namespace_quota_offers_only_what_its_project_limits(context, flask_server):
    page, w, sent = setup(context, flask_server, MANAGED)
    w.locator('tr[data-ns="apps"] [data-nsw-act="edit"]').click()
    box = w.locator("[data-pj-nsq]")
    expect(box.locator("input[data-key]")).to_have_count(1)                 # limitsCpu seulement
    box.locator('input[data-key="limitsCpu"]').fill("3")
    w.locator('.nsw-form button[type="submit"]').click()
    page.wait_for_timeout(800)
    assert sent[-1] == ("api/projects/harv-fake/do/ns-quota", {"namespace": "apps", "limit": {"limitsCpu": "3"}})


def test_the_projects_tab_creates_a_project_and_keeps_the_default_one(context, flask_server):
    page, w, sent = setup(context, flask_server, MANAGED)
    expect(w.locator('tbody tr[data-ns="apps"]')).to_have_count(1, timeout=8000)
    w.locator('[data-nsw-tab="projects"]').click()
    expect(w.locator('tr[data-pj="p-nc8d2"] [data-pj-act="delete"]')).to_be_disabled()   # Default
    expect(w.locator('tr[data-pj="p-abcde"] [data-pj-act="delete"]')).to_be_disabled()   # peuplé
    expect(w.locator(".sto-finding")).to_contain_text("1 namespace(s) point to a project of another cluster")
    w.locator('[data-nsw="new-project"]').click()
    f = w.locator(".nsw-form")
    f.locator('[name="name"]').fill("Team B")
    f.locator("[data-q-add]").click()
    f.locator('[data-q="key"]').select_option("limitsMemory")
    f.locator('[data-q="limit"]').fill("8Gi")
    f.locator('[data-q="default"]').fill("4Gi")
    f.locator('[data-limit="limitsCpu"]').fill("1")
    f.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, b = sent[-1]
    assert path == "api/projects/harv-fake/do/create"
    assert b["spec"] == {"name": "Team B", "description": "", "quota": {"limitsMemory": "8Gi"},
                         "ns_default": {"limitsMemory": "4Gi"}, "container": {"limitsCpu": "1"}}


def test_a_local_account_sees_the_grouping_without_managing_projects(context, flask_server):
    local = {**MANAGED, "projects": {"managed": False, "cid": CID, "items": None},
             "items": [row("apps", pr("member", "p-abcde")), row("default", pr("foreign", "p-kcgzz", cluster="c-qt5jz"))]}
    page, w, sent = setup(context, flask_server, local)
    expect(w.locator('tr[data-ns="apps"] .badge.info')).to_have_text("p-abcde", timeout=8000)
    expect(w.locator('[data-nsw-act="move"]')).to_have_count(0)
    w.locator('[data-nsw-tab="projects"]').click()
    expect(w.locator(".sto-finding")).to_contain_text("sign in to the console through Rancher")
    expect(w.locator("code.res-key")).to_have_count(2)
