"""v1.73.0 : les membres Rancher dans un navigateur : ceux du cluster sous
ses comptes (Réglages), ceux d'un projet depuis l'onglet Projets ; recherche
d'un utilisateur ou d'un groupe, ajout avec un rôle, retrait (pas d'un
compte système), et le compte local renvoyé vers la connexion par Rancher."""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402
from nav import address, goto  # noqa: E402

MEMBERS = {"managed": True, "scope": "cluster",
           "roles": [{"id": "cluster-owner", "name": "Cluster Owner"}, {"id": "cluster-member", "name": "Cluster Member"},
                     {"id": "nodes-view", "name": "View Nodes"}],
           "members": [{"id": "c-sg2q6:creator-cluster-owner", "principal": "local://user-kk67j", "user": "user-kk67j", "kind": "user",
                        "name": "Default Admin", "login": "admin", "provider": "local", "role": "cluster-owner",
                        "role_name": "Cluster Owner", "system": False, "created": None},
                       {"id": "c-sg2q6:u-sys-admin", "principal": "", "user": "u-sys", "kind": "user",
                        "name": "System account for Cluster c-sg2q6", "login": "", "provider": "", "role": "cluster-owner",
                        "role_name": "Cluster Owner", "system": True, "created": None}]}
PRINCIPALS = {"items": [{"id": "keycloakoidc_user://jn", "name": "Julien Niedergang", "login": "jn", "kind": "user",
                         "provider": "keycloakoidc_user"}]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def setup(context, flask_server, members=MEMBERS):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    sent = []

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "mb0000000173"}, 202)
    page.route("**/api/rancher-members/harv-fake?**", lambda r, q: fulfill(r, members))
    page.route("**/api/rancher-principals/harv-fake?**", lambda r, q: fulfill(r, PRINCIPALS))
    page.route("**/api/rancher-members/harv-fake/do/**", writes)
    page.route("**/api/harvester-users/harv-fake", lambda r, q: fulfill(r, {"users": [], "admins": []}))
    page.route("**/api/stream/mb0000000173", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address("namespaces"))
    page.wait_for_function("window.RancherMembers && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    return page, sent


def test_cluster_members_are_added_after_a_search_and_system_accounts_are_kept(context, flask_server):
    page, sent = setup(context, flask_server)
    # 2.0.0 : les comptes du cluster sont dans Paramètres > Sécurité de l'hôte (décision 7)
    page.evaluate("Router.go('#harvester/harv-fake/params/security/accounts')")   # chargé à l'ouverture
    box = page.locator("#rmembers-body")
    expect(box.locator('tr[data-rm="c-sg2q6:u-sys-admin"] [data-rm-act="remove"]')).to_be_disabled(timeout=8000)
    box.locator('[name="q"]').fill("jul")
    expect(box.locator('[name="principal"]')).to_have_value("keycloakoidc_user://jn", timeout=5000)
    box.locator('[name="role"]').select_option("nodes-view")
    box.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/rancher-members/harv-fake/do/add",
                        {"scope": "cluster", "principal": "keycloakoidc_user://jn", "role": "nodes-view"})
    expect(box.locator(".of-msg")).to_contain_text("Member added", timeout=5000)
    page.wait_for_timeout(1500)                                       # la liste se relit...
    expect(box.locator(".of-msg")).to_contain_text("Member added")    # ...et le message reste
    box.locator('tr[data-rm="c-sg2q6:creator-cluster-owner"] [data-rm-act="remove"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/rancher-members/harv-fake/do/remove", {"scope": "cluster", "id": "c-sg2q6:creator-cluster-owner"})


def test_a_project_s_members_open_from_the_projects_tab(context, flask_server):
    page, sent = setup(context, flask_server, {**MEMBERS, "scope": "project", "members": []})
    ns_data = {"items": [], "projects": {"managed": True, "cid": "c-sg2q6", "items": [
        {"id": "p-abcde", "full_id": "c-sg2q6:p-abcde", "name": "Team A", "description": "", "quota": {}, "used": {}, "ns_default": {},
         "container": {}, "state": "active", "system": False, "default": False}]}, "quota_keys": [], "limit_keys": []}
    page.route("**/api/ns-admin/harv-fake", lambda r, q: fulfill(r, ns_data))
    page.evaluate("Namespaces.open('harv-fake')")
    w = page.locator(".floating-panel", has=page.locator(".nsw-win")).last
    w.locator('[data-nsw-tab="projects"]').click()
    w.locator('tr[data-pj="p-abcde"] [data-pj-act="members"]').click()
    expect(w.locator(".rm-box h4")).to_have_text("Members of project Team A", timeout=8000)
    w.locator('[name="q"]').fill("jul")
    expect(w.locator('[name="principal"]')).to_have_value("keycloakoidc_user://jn", timeout=5000)
    w.locator('.rm-add button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][1]["scope"] == "project" and sent[-1][1]["project"] == "p-abcde"


def test_a_console_account_is_told_to_sign_in_through_rancher(context, flask_server):
    page, _ = setup(context, flask_server, {"managed": False, "members": [], "roles": []})
    page.evaluate("Router.go('#harvester/harv-fake/params/security/accounts')")
    expect(page.locator("#rmembers-body")).to_contain_text("sign in to the console through Rancher", timeout=8000)
