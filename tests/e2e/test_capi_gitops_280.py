"""2.8.0 (tâche 9) : onglet GitOps de Cluster API, sur routes simulées.

* liste : outil (Flux, Fleet) et état de chaque entrée, origine liée,
  indication sans session Rancher ;
* fenêtre Ajouter : outil déduit montré (cluster importé : Fleet, sinon
  Flux) ; Appliquer grisé avec la raison tant qu'un constat bloque, actif
  une fois levé (M-apply) ; identifiants Git envoyés une fois puis effacés,
  jamais réaffichés (M-creds) ;
* noms hostiles échappés (M-escape), info-bulle sur chaque contrôle.
"""

import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_auto_tabs_220 as t220  # noqa: E402

HOSTILE = '<img src=x onerror="window.__pwned=1">'
fulfill = t220.fulfill
DIAG = {"capi_clusters": [{"namespace": "apps", "name": "apps", "phase": "Provisioned", "ready": True},
                          {"namespace": "web", "name": "web", "phase": "Provisioned", "ready": True}],
        "components": []}
ENTRIES = [
    {"tool": "flux", "namespace": "apps", "cluster": "apps", "entry": "site",
     "origin": {"plan": "prod", "run": "0123456789ab"},
     "state": {"applied": True, "hash": "x", "clusters": [], "message": "applied"}},
    {"tool": "fleet", "namespace": "web", "cluster": "web", "entry": HOSTILE, "kind": "GitRepo",
     "name": "hops-harv-fake-web-x", "origin": {}, "state": {"ready": False, "message": "0/1 ready"}},
]


def open_gitops(context, flask_server, calls, role="admin", entries=None, session=True, blocked=True):
    context.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(
        r, {"role": role, "roles_active": True, "user": "u", "auth": "local"}))
    page = t220.open_auto(context, flask_server, "#automation/capi", diag=DIAG)
    state = {"blocked": blocked}
    page.route(re.compile(r".*/api/capi/harv-fake/gitops$"), lambda r, q: gitops(r, q))
    page.route(re.compile(r".*/api/capi/harv-fake/rancher-status$"), lambda r, q: fulfill(r, {
        "session": "https://rancher.example", "clusters": [
            {"namespace": "apps", "cluster": "apps", "imported": False, "turtles": False},
            {"namespace": "web", "cluster": "web", "imported": True, "turtles": False}]}))
    page.route(re.compile(r".*/api/artefacts$"), lambda r, q: fulfill(r, {"packages": [
        {"name": "apps", "version": "1.0.0"}]}))
    page.route(re.compile(r".*/api/artefacts/apps/1\.0\.0$"), lambda r, q: fulfill(r, {"index": {"items": [
        {"name": "site", "kind": "gitops"}, {"name": "flux", "kind": "chart"}]}}))
    page.route(re.compile(r".*/api/registries$"), lambda r, q: fulfill(r, {"registries": [
        {"name": "amorcage", "endpoint": "172.16.2.76", "clusters": ["harv-fake"]}]}))

    def gitops(route, req):
        if req.method == "GET":
            return fulfill(route, {"entries": ENTRIES if entries is None else entries, "session": session})
        body = json.loads(req.post_data or "{}")
        calls.setdefault("post", []).append(body)
        if body.get("dry_run"):
            fs = [{"code": "gitops-flux-missing", "level": "block", "facts": {"service": "flux"}}] if state["blocked"] else []
            return fulfill(route, {"blocked": state["blocked"], "tool": "flux", "findings": fs,
                                   "entry": body["entry"]["name"]})
        fulfill(route, {"action_id": "g1", "cluster": body["cluster"], "entry": body["entry"]["name"]}, 202)
    page.locator('#tab-automation .sub-tab[data-capi-tab="gitops"]').click()
    return page, state


def test_list_shows_tool_state_origin_and_escapes(context, flask_server):
    page, _ = open_gitops(context, flask_server, {})
    body = page.locator("#capi-gitops-body")
    rows = body.locator(".gop-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator(".gop-tool")).to_have_text("Flux")
    expect(rows.nth(0).locator(".gop-state")).to_have_text("ready")
    expect(rows.nth(0).locator(".gop-run")).to_have_attribute("href", "#activity/action/0123456789ab")
    expect(rows.nth(1).locator(".gop-tool")).to_have_text("Fleet")
    expect(rows.nth(1).locator(".gop-state")).to_have_text("pending")
    expect(rows.nth(1)).to_contain_text(HOSTILE)                # texte, pas balise
    assert page.evaluate("window.__pwned") is None
    assert body.locator("img").count() == 0
    assert all(t.get_attribute("data-tip") for t in body.locator(".tip").all())


def test_without_session_a_hint_and_operator_cannot_remove_fleet(context, flask_server):
    page, _ = open_gitops(context, flask_server, {}, role="operator", session=False)
    body = page.locator("#capi-gitops-body")
    expect(body.locator(".gop-no-session")).to_be_visible()
    expect(body.locator('.gop-row[data-tool="flux"] [data-gop-remove]')).to_be_enabled()
    expect(body.locator('.gop-row[data-tool="fleet"] [data-gop-remove]')).to_be_disabled()


def test_empty_list_says_what_to_do(context, flask_server):
    page, _ = open_gitops(context, flask_server, {}, entries=[])
    expect(page.locator("#capi-gitops-body .empty-state")).to_contain_text("No GitOps entry")


def test_deduced_tool_and_apply_greyed_until_no_blocker(context, flask_server):
    calls = {}
    page, state = open_gitops(context, flask_server, calls)
    page.locator("#btn-gitops-add").click()
    m = page.locator(".gop-modal")
    expect(m.locator("[data-gop-tab]")).to_have_count(3)
    deduced = m.locator('[data-x="deduced"]')
    expect(deduced).to_contain_text("Flux")                     # apps : non importé
    m.locator('[data-f="cluster"]').select_option("web/web")
    expect(deduced).to_contain_text("Fleet")                    # web : importé dans Rancher
    m.locator('[data-f="cluster"]').select_option("apps/apps")
    m.locator('[data-f="name"]').fill("site")
    m.locator('[data-f="namespace"]').fill("equipe-a")
    m.locator('[data-f="package"]').select_option("apps@1.0.0")
    expect(m.locator('[data-f="item"] option')).to_have_count(1)  # seulement la sorte gitops
    go = m.locator("[data-gop-go]")
    expect(m.locator('[data-x="go-wrap"]')).to_have_attribute("data-tip", re.compile("Flux is not installed"), timeout=5000)
    expect(go).to_be_disabled()
    m.locator('[data-gop-tab="checks"]').click()
    expect(m.locator('[data-code="gitops-flux-missing"]')).to_be_visible()
    state["blocked"] = False
    m.locator('[data-gop-tab="params"]').click()
    m.locator('[data-f="interval"]').fill("15")
    expect(go).to_be_enabled(timeout=5000)
    m.locator('[data-gop-tab="summary"]').click()
    expect(m.locator('[data-x="summary"]')).to_contain_text("equipe-a")
    go.click()
    expect(m.locator('[data-x="progress"]')).to_contain_text("g1")
    body = calls["post"][-1]
    assert "dry_run" not in body
    assert body == {"cluster": "apps/apps", "secrets": {}, "entry": {
        "name": "site", "tool": "auto", "namespace": "equipe-a", "privileges": "namespace", "interval_minutes": 15,
        "prune": True, "source": {"type": "oci", "package": "apps", "version": "1.0.0", "item": "site",
                                  "registry": "amorcage"}}}
    assert all(t.get_attribute("data-tip") for t in m.locator(".tip").all())


def test_git_credentials_are_sent_once_and_never_shown_again(context, flask_server):
    calls = {}
    page, _ = open_gitops(context, flask_server, calls, blocked=False)
    page.locator("#btn-gitops-add").click()
    m = page.locator(".gop-modal")
    m.locator('[data-f="name"]').fill("repo")
    m.locator('[data-f="namespace"]').fill("equipe-a")
    m.locator('[data-f="source"]').select_option("git")
    m.locator('[data-f="url"]').fill("ssh://git@git.example:22/team/apps.git")
    m.locator('[data-f="creds"]').select_option("basic")
    m.locator('[data-c="username"]').fill("reader")
    m.locator('[data-c="password"]').fill("Git-Secret-42")
    go = m.locator("[data-gop-go]")
    expect(go).to_be_enabled(timeout=5000)
    # les vérifications ne transportent jamais les valeurs
    assert all(b.get("secrets") in ({}, {"git": {}}) for b in calls["post"] if b.get("dry_run"))
    go.click()
    expect(m.locator('[data-x="progress"]')).to_contain_text("g1")
    body = calls["post"][-1]
    assert body["secrets"] == {"git": {"username": "reader", "password": "Git-Secret-42"}}
    assert body["entry"]["source"] == {"type": "git", "url": "ssh://git@git.example:22/team/apps.git",
                                       "branch": "main", "path": "./", "secret": "git"}
    for k in ("username", "password"):
        expect(m.locator(f'[data-c="{k}"]')).to_have_value("")
    assert "Git-Secret-42" not in m.inner_html()


def test_cluster_privileges_greyed_for_an_operator(context, flask_server):
    page, _ = open_gitops(context, flask_server, {}, role="operator")
    page.locator("#btn-gitops-add").click()
    m = page.locator(".gop-modal")
    expect(m.locator('[data-f="privileges"] option[value="cluster"]')).to_have_attribute("disabled", "")


def test_artefacts_gitops_item_opens_the_window_prefilled(context, flask_server):
    page, _ = open_gitops(context, flask_server, {})
    page.route(re.compile(r".*/api/artefacts/apps/1\.0\.0$"), lambda r, q: fulfill(r, {
        "name": "apps", "version": "1.0.0", "trust": {"state": "valid"}, "images": [], "published": [],
        "index": {"items": [{"name": "site", "kind": "gitops"}, {"name": "rke2", "kind": "rke2"}]}}))
    page.route(re.compile(r".*/api/artefacts$"), lambda r, q: fulfill(r, {"packages": [
        {"name": "apps", "version": "1.0.0", "trust": {"state": "valid"}}]}))
    page.locator('#tab-automation .sub-tab[data-capi-tab="artefacts"]').click()
    page.locator('.art-row[data-art-name="apps"]').click()
    detail = page.locator("#artefacts-detail")
    expect(detail.locator('.art-kind[data-kind="rke2"]')).to_have_attribute("data-tip", re.compile("RKE2"))
    detail.locator('[data-art-gitops="site"]').click()
    m = page.locator(".gop-modal")
    expect(m.locator('[data-f="package"]')).to_have_value("apps@1.0.0")
    expect(m.locator('[data-f="name"]')).to_have_value("site")
    expect(m.locator('[data-f="item"]')).to_have_value("site")
