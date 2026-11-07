"""2.8.0 (tâche 9) : renouvellement des machines d'un cluster enfant.

Fenêtre à trois onglets (Paramètres, Différence, Confirmation) ouverte
depuis la liste des clusters ; la différence vient d'un essai du serveur
(`dry_run`), rien n'est écrit avant le bouton.

* Renouveler grisé sans différence (rien à renouveler), raison dans la bulle ;
* avec une différence : grisé jusqu'à la confirmation cochée, puis la
  demande part sans `dry_run`, suivie ;
* OIDC : réglages actuels relus, préfixe vide refusé sans demander au serveur ;
* registres remplacés : la liste part dans la demande ;
* refus traduits (renouvellement en cours) ; chemins hostiles échappés.
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
DIAG = {"components": [], "capi_clusters": [
    {"namespace": "apps", "name": "apps", "phase": "Provisioned", "ready": True, "clusterClass": "apps", "k8sVersion": "v1.34.11"}]}
OIDC = {"issuer_url": "https://idp.example/realms/main", "audience": "kubernetes", "username_prefix": "oidc:",
        "groups_prefix": "oidc:"}
CHANGES = {"cluster": "apps/apps", "revision": 2, "secrets_changed": True, "findings": [
    {"code": "renew-pool-short", "level": "warn", "facts": {"pool": "capi-pool", "available": 1}}],
    "changes": [{"template": "apps-control-plane", "kind": "RKE2ControlPlaneTemplate",
                 "paths": ["spec.template.spec.files[3]", HOSTILE]}], "blocked": False}
NOTHING = {"cluster": "apps/apps", "changes": [], "secrets_changed": False, "blocked": True}


def sse(events):
    return "".join(f"event: {t}\ndata: {json.dumps(d)}\n\n" for t, d in events)


def open_renew(context, flask_server, calls, answer, oidc=None, role="operator"):
    context.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(
        r, {"role": role, "roles_active": True, "user": "u", "auth": "local"}))
    page = t220.open_auto(context, flask_server, "#automation/capi", diag=DIAG)
    page.route(re.compile(r".*/api/capi/harv-fake/inventory.*"), lambda r, q: fulfill(r, {"registries": [
        {"name": "amorcage", "endpoint": "172.16.2.76", "attached": True, "mirrors": ["docker.io"], "system_images": True},
        {"name": "harbor", "endpoint": "harbor.example", "attached": True, "mirrors": ["ghcr.io"]}]}))
    page.route(re.compile(r".*/api/capi/harv-fake/cluster/apps/apps/details$"),
               lambda r, q: fulfill(r, {"name": "apps", "namespace": "apps", "oidc": oidc, "machines": []}))

    def renew(route, req):
        body = json.loads(req.post_data or "{}")
        calls.setdefault("renew", []).append(body)
        if body.get("dry_run"):
            a = answer(body)
            return fulfill(route, a[0], a[1]) if isinstance(a, tuple) else fulfill(route, a)
        fulfill(route, {"action_id": "ren000000001", "cluster": "apps/apps", "what": body.get("what") or []}, 202)
    page.route(re.compile(r".*/api/capi/harv-fake/renew/apps/apps$"), renew)
    page.route(re.compile(r".*/api/stream/ren000000001$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body=sse([
            ("step", {"step_id": "wait", "status": "running", "message": "1/2 machine(s) renewed"}),
            ("end", {"status": "done"})])))
    page.evaluate("window.confirm = () => true")
    btn = page.locator('.capi-cluster-renew[data-name="apps"]')
    expect(btn).to_be_visible(timeout=5000)
    assert btn.get_attribute("data-tip")
    btn.click()
    win = page.locator("#fp-capi-renew-apps-apps")
    expect(win).to_be_visible()
    return page, win


def x(win, k):
    return win.locator(f'[data-x="{k}"]')


def test_renew_is_greyed_without_a_difference(context, flask_server):
    calls = {}
    page, win = open_renew(context, flask_server, calls, lambda b: NOTHING)
    go = x(win, "go")
    expect(go).to_be_disabled()
    expect(x(win, "go-wrap")).to_have_attribute("data-tip", re.compile("Choose what to renew"))
    win.locator('[data-w="registries"]').check()
    win.locator('[data-win-tab="diff"]').click()
    expect(win.locator('[data-code="renew-nothing"]')).to_be_visible(timeout=5000)
    win.locator('[data-win-tab="confirm"]').click()
    x(win, "understand").check()
    expect(go).to_be_disabled()
    expect(x(win, "go-wrap")).to_have_attribute("data-tip", re.compile("Nothing to renew"))
    assert calls["renew"][-1] == {"dry_run": True, "what": ["registries"], "timeout": 3600}
    assert all(t.get_attribute("data-tip") for t in win.locator(".tip").all())


def test_a_difference_then_the_confirmation_launch_the_renewal(context, flask_server):
    calls = {}
    page, win = open_renew(context, flask_server, calls, lambda b: CHANGES)
    win.locator('[data-w="registries"]').check()
    win.locator('[data-win-tab="diff"]').click()
    expect(win.locator('[data-win-pane="params"]')).to_be_hidden()      # un onglet à la fois
    diff = x(win, "diff")
    expect(diff).to_contain_text("apps-control-plane", timeout=5000)
    expect(diff).to_contain_text("revision 2")
    expect(diff.locator('[data-code="renew-pool-short"]')).to_contain_text("capi-pool")
    go = x(win, "go")
    expect(go).to_be_disabled()
    expect(x(win, "go-wrap")).to_have_attribute("data-tip", re.compile("confirmation"))
    win.locator('[data-win-tab="confirm"]').click()
    expect(x(win, "summary")).to_contain_text("one at a time")
    x(win, "understand").check()
    expect(go).to_be_enabled()
    go.click()
    expect(x(win, "line")).to_contain_text("ren000000001", timeout=5000)
    assert calls["renew"][-1] == {"what": ["registries"], "timeout": 3600}
    assert page.evaluate("window.__pwned") is None and win.locator("img[src=x]").count() == 0
    # une demande changée après la différence regrise le bouton
    expect(go).to_be_disabled()


def test_changed_parameters_grey_the_button_until_the_new_difference(context, flask_server):
    calls = {}
    page, win = open_renew(context, flask_server, calls, lambda b: CHANGES)
    win.locator('[data-w="registries"]').check()
    win.locator('[data-win-tab="confirm"]').click()
    x(win, "understand").check()
    go = x(win, "go")
    expect(go).to_be_enabled(timeout=5000)
    win.locator('[data-win-tab="params"]').click()
    win.locator('[data-w="ca"]').check()
    expect(go).to_be_disabled()
    expect(go).to_be_enabled(timeout=5000)
    assert calls["renew"][-1]["what"] == ["registries", "ca"]


def test_oidc_is_read_back_and_an_empty_prefix_is_refused(context, flask_server):
    calls = {}
    page, win = open_renew(context, flask_server, calls, lambda b: CHANGES, oidc=OIDC)
    win.locator('[data-w="oidc"]').check()
    box = win.locator('[data-w-box="oidc"]')
    expect(box.locator('[data-o="issuer_url"]')).to_have_value(OIDC["issuer_url"])
    expect(box.locator("[data-o-on]")).to_be_checked()
    page.wait_for_timeout(900)
    assert calls["renew"][-1]["oidc"]["issuer_url"] == OIDC["issuer_url"]
    n = len(calls["renew"])
    box.locator('[data-o="groups_prefix"]').fill("")
    box.locator('[data-o="groups_prefix"]').dispatch_event("change")
    page.wait_for_timeout(900)
    assert len(calls["renew"]) == n                       # refusé sans demander au serveur
    win.locator('[data-win-tab="diff"]').click()
    expect(x(win, "diff").locator('[data-code="oidc-prefix-required"]')).to_be_visible()
    expect(x(win, "go")).to_be_disabled()
    # décocher l'OIDC le retire : oidc null dans la demande
    win.locator('[data-win-tab="params"]').click()
    box.locator("[data-o-on]").uncheck()
    page.wait_for_timeout(900)
    assert calls["renew"][-1]["oidc"] is None


def test_replaced_registries_reach_the_request(context, flask_server):
    calls = {}
    page, win = open_renew(context, flask_server, calls, lambda b: CHANGES)
    win.locator('[data-w="registries"]').check()
    win.locator('[data-reg-mode="replace"]').check()
    editor = win.locator("[data-reg-editor]")
    expect(editor).to_be_visible()
    editor.locator('[data-reg="add"]').select_option("harbor")
    page.wait_for_timeout(900)
    assert calls["renew"][-1]["registries"] == [{"registry": "harbor"}]


def test_refusals_are_translated(context, flask_server):
    calls = {}
    page, win = open_renew(context, flask_server, calls, lambda b: (
        {"error": "renew-busy: a renewal is in progress (machines of two revisions)"}, 502))
    win.locator('[data-w="registries"]').check()
    win.locator('[data-win-tab="diff"]').click()
    expect(x(win, "diff")).to_contain_text("is not finished", timeout=5000)
    expect(x(win, "go-wrap")).to_have_attribute("data-tip", re.compile("Blocked: A renewal of this child cluster"))


def test_a_viewer_sees_renew_greyed(context, flask_server):
    calls = {}
    page, win = open_renew(context, flask_server, calls, lambda b: CHANGES, role="viewer")
    win.locator('[data-w="registries"]').check()
    win.locator('[data-win-tab="confirm"]').click()
    x(win, "understand").check()
    page.wait_for_timeout(900)
    expect(x(win, "go")).to_be_disabled()
    expect(x(win, "go-wrap")).to_have_attribute("data-tip", re.compile("operator role"))


# --- revue 5-8 (I-1, m-2) : renouvellement pas fini, Finir et Abandonner ------------

BUSY = ({"error": "renew-busy: the renewal of revision 2 is not finished; renew --finish resumes it"}, 502)


@pytest.mark.parametrize("role,finish_on,abandon_on", [("operator", True, False), ("admin", True, True),
                                                       ("viewer", False, False)])
def test_an_unfinished_renewal_offers_finish_and_abandon(context, flask_server, role, finish_on, abandon_on):
    calls = {}
    page, win = open_renew(context, flask_server, calls, lambda b: BUSY, role=role)
    expect(x(win, "unfinished")).to_be_hidden()
    win.locator('[data-w="registries"]').check()
    expect(x(win, "unfinished")).to_be_visible(timeout=5000)
    for k, on in (("finish", finish_on), ("abandon", abandon_on)):
        btn = x(win, k)
        (expect(btn).to_be_enabled() if on else expect(btn).to_be_disabled())
        assert x(win, f"{k}-wrap").get_attribute("data-tip")
    if not abandon_on and role != "viewer":
        expect(x(win, "abandon-wrap")).to_have_attribute("data-tip", re.compile("administrator"))
    if finish_on:
        x(win, "finish").click()
        page.wait_for_timeout(600)
        assert calls["renew"][-1] == {"mode": "finish", "timeout": 3600}
        expect(x(win, "line")).to_contain_text("ren000000001", timeout=5000)


def test_an_administrator_abandons_the_renewal(context, flask_server):
    calls = {}
    page, win = open_renew(context, flask_server, calls, lambda b: BUSY, role="admin")
    win.locator('[data-w="ca"]').check()
    expect(x(win, "abandon")).to_be_enabled(timeout=5000)
    x(win, "abandon").click()
    page.wait_for_timeout(600)
    assert calls["renew"][-1] == {"mode": "abandon"}


def test_an_admin_group_change_alone_can_be_renewed(context, flask_server):
    """Revue finale I-2 : un changement du seul groupe administrateur ne
    touche aucun gabarit ; la différence le dit et le bouton s'active."""
    calls = {}
    access = {"cluster": "apps/apps", "revision": 2, "changes": [], "secrets_changed": False,
              "access_changed": True, "findings": [], "blocked": False}
    page, win = open_renew(context, flask_server, calls, lambda b: access, oidc=OIDC)
    win.locator('[data-w="oidc"]').check()
    win.locator('[data-win-tab="diff"]').click()
    expect(win.locator("[data-access-changed]")).to_contain_text("administrator group", timeout=5000)
    expect(win.locator('[data-code="renew-nothing"]')).to_have_count(0)
    win.locator('[data-win-tab="confirm"]').click()
    x(win, "understand").check()
    expect(x(win, "go")).to_be_enabled()
