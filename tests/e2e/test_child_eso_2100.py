"""2.10.0 (tâche 10) : onglet « Secrets externes » de Cluster API, sur routes simulées.

* onglet joint à `#automation/capi/secrets`, état d'un cluster enfant rendu
  (chart, OpenBao, accès en attente de l'administrateur, empreinte courte,
  namespaces, magasins, ExternalSecret, exemple à copier) ;
* fenêtre « Mettre en place » : Appliquer grisé avec la raison tant qu'un
  constat bloque, actif une fois levé, corps exact envoyé (M-apply) ;
* activation et retrait d'un namespace ; retrait d'ESO par saisie du nom ;
* écrire une valeur : champs envoyés une fois, vidés aussitôt, jamais
  réaffichés (M-clear) ;
* noms hostiles échappés (M-escape) ; boutons d'administrateur grisés avec
  une info-bulle pour un opérateur.
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
SECRET = "Sup3r-S3cret-Value-42"
fulfill = t220.fulfill
DIAG = {"capi_clusters": [{"namespace": "apps", "name": "apps", "phase": "Provisioned", "ready": True},
                          {"namespace": "web", "name": "web", "phase": "Provisioned", "ready": True}],
        "components": []}


def state_doc(**kw):
    d = {"cluster": "apps/apps", "key": "harv-fake-apps-apps", "openbao": True, "mode": "a", "listener": None,
         "endpoint": "https://bao.example:8200", "chart": {"present": True, "ready": True, "locked": True},
         "auth": {"mount": "eso-harv-fake-apps-apps", "keys_sha256": "abcdef0123456789" * 4, "applied": False,
                  "at": 1794100000, "by": "alice", "node_cidr": "172.16.2.0/24"},
         "namespaces": ["equipe-a", "equipe-b"],
         "stores": [{"ns": "equipe-a", "name": "hops-openbao", "ready": True, "reason": "Valid", "message": "store validated",
                     "ours": True},
                    {"ns": "equipe-b", "name": "hops-openbao", "ready": False, "reason": "InvalidProviderConfig",
                     "message": HOSTILE, "ours": True}],
         "externalsecrets": [{"ns": "equipe-a", "name": HOSTILE, "ready": False, "reason": "SecretSyncedError",
                              "message": HOSTILE, "synced_at": "2026-11-09T10:00:00Z", "store": "hops-openbao",
                              "target": "db"}],
         "labelled": ["equipe-a", "equipe-b"], "crs": {"applied": True, "message": "applied"},
         "example": {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
                     "metadata": {"name": "db", "namespace": "equipe-a"},
                     "spec": {"refreshInterval": "1h", "secretStoreRef": {"kind": "SecretStore", "name": "hops-openbao"},
                              "target": {"name": "db", "creationPolicy": "Owner"},
                              "dataFrom": [{"extract": {"key": "consoles/x/eso/harv-fake-apps-apps/equipe-a/db"}}]}}}
    d.update(kw)
    return d


def open_eso(context, flask_server, calls, role="admin", doc=None, blocked=True):
    context.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(
        r, {"role": role, "roles_active": True, "user": "u", "auth": "local"}))
    st = {"blocked": blocked, "doc": doc or state_doc()}

    def eso(route, req):
        url = req.url.split("/api/capi/harv-fake/eso/", 1)[1]
        body = json.loads(req.post_data or "null") if req.method in ("POST",) else None
        calls.setdefault("all", []).append((req.method, url, body))
        if url.startswith("apps/apps/namespaces/"):
            return fulfill(route, {"action_id": "n1"}, 202)
        if url == "apps/apps/secrets":
            return fulfill(route, {"action_id": "w1", "version": 3, "identity": "person"})
        if req.method == "GET":
            return fulfill(route, st["doc"])
        if req.method == "DELETE":
            return fulfill(route, {"action_id": "d1"}, 202)
        if body.get("dry_run"):
            fs = ([{"code": "eso-chart-missing", "level": "block", "facts": {"service": HOSTILE}}] if st["blocked"]
                  else [{"code": "eso-openbao-unchecked", "level": "warn",
                         "facts": {"endpoint": "https://bao.example:8200", "reason": "probe"}}])
            return fulfill(route, {"blocked": st["blocked"], "findings": fs, "key": "k", "mode": "a",
                                   "endpoint": "https://bao.example:8200", "namespaces": body["namespaces"]})
        fulfill(route, {"action_id": "s1"}, 202)
    context.route(re.compile(r".*/api/capi/harv-fake/eso/.*"), eso)
    context.route(re.compile(r".*/api/artefacts$"), lambda r, q: fulfill(r, {"packages": [
        {"name": "secrets", "version": "1.2.0"}]}))
    context.route(re.compile(r".*/api/registries$"), lambda r, q: fulfill(r, {"registries": [
        {"name": "amorcage", "endpoint": "172.16.2.76", "clusters": ["harv-fake"]}]}))
    context.route(re.compile(r".*/api/stream/[a-z0-9]+$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    page = t220.open_auto(context, flask_server, "#automation/capi/secrets", diag=DIAG)
    expect(page.locator("#capi-eso-state .eso-kv")).to_be_visible(timeout=8000)
    return page, st


def test_tab_reachable_and_state_rendered_with_hostile_names_escaped(context, flask_server):
    page, _ = open_eso(context, flask_server, {})
    expect(page.locator('#tab-automation .sub-tab[data-capi-tab="secrets"]')).to_have_class(re.compile(r"\bactive\b"))
    assert t220.capi_visible(page) == ["secrets"]
    body = page.locator("#capi-eso-body")
    expect(body.locator('[data-eso="child"]')).to_have_value("apps/apps")
    expect(body.locator('[data-eso-chart="ready"]')).to_be_visible()
    expect(body.locator('[data-eso-auth="waiting"]')).to_contain_text("waiting for the OpenBao administrator")
    expect(body.locator("[data-eso-fp]")).to_have_text("abcdef012345")
    expect(body.locator(".eso-ns")).to_have_count(2)
    expect(body.locator(".eso-store")).to_have_count(2)
    expect(body.locator(".eso-es")).to_have_count(1)
    expect(body.locator(".eso-es")).to_contain_text(HOSTILE)          # texte, pas balise
    expect(body.locator("[data-eso-example]")).to_contain_text("kind: ExternalSecret")
    expect(body.locator(".eso-example")).to_contain_text("every hour")
    assert page.evaluate("window.__pwned") is None
    assert body.locator("img").count() == 0
    assert all(b.get_attribute("data-tip") for b in body.locator("button").all())


def test_setup_apply_greyed_while_blocked_then_posts_the_expected_body(context, flask_server):
    calls = {}
    page, st = open_eso(context, flask_server, calls)
    page.locator('[data-eso-act="setup"]').click()
    m = page.locator(".eso-modal")
    expect(m.locator("[data-eso-tab]")).to_have_count(3)
    go = m.locator("[data-eso-go]")
    expect(m.locator('[data-x="go-wrap"]')).to_have_attribute("data-tip", re.compile("is not installed"), timeout=5000)
    expect(go).to_be_disabled()
    m.locator('[data-eso-tab="checks"]').click()
    expect(m.locator('[data-eso-tab="checks"]')).to_have_class(re.compile(r"\bactive\b"))
    expect(m.locator('[data-code="eso-chart-missing"]')).to_contain_text(HOSTILE)
    assert m.locator("img").count() == 0 and page.evaluate("window.__pwned") is None
    st["blocked"] = False
    m.locator('[data-eso-tab="params"]').click()
    m.locator('[data-f="namespaces"]').fill("equipe-a, equipe-c")
    m.locator('[data-f="package"]').select_option("secrets@1.2.0")
    expect(go).to_be_enabled(timeout=5000)
    m.locator('[data-eso-tab="summary"]').click()
    expect(m.locator('[data-x="summary"]')).to_contain_text("equipe-a, equipe-c")
    expect(m.locator('[data-x="summary"]')).to_contain_text("secrets 1.2.0")
    dry = [b for (meth, url, b) in calls["all"] if meth == "POST" and url == "apps/apps" and b.get("dry_run")]
    assert dry[-1] == {"namespaces": ["equipe-a", "equipe-c"], "dry_run": True,
                       "chart": {"registry": "amorcage", "package": "secrets", "version": "1.2.0"}}
    go.click()
    expect(m.locator('[data-x="progress"]')).to_be_visible()
    posted = [b for (meth, url, b) in calls["all"] if meth == "POST" and url == "apps/apps" and not b.get("dry_run")]
    assert posted == [{"namespaces": ["equipe-a", "equipe-c"],
                       "chart": {"registry": "amorcage", "package": "secrets", "version": "1.2.0"}}]
    assert all(t.get_attribute("data-tip") for t in m.locator(".tip").all())


def test_namespace_enable_and_remove_calls(context, flask_server):
    calls = {}
    page, _ = open_eso(context, flask_server, calls)
    page.evaluate("window.confirm = () => true")
    body = page.locator("#capi-eso-body")
    body.locator("[data-eso-ns-input]").fill("equipe-c")
    body.locator("[data-eso-ns-add]").click()
    body.locator('[data-eso-ns-del="equipe-b"]').click()
    expect(page.locator("#eso-feedback")).to_contain_text(re.compile(r"n1|Done"), timeout=5000)
    for _ in range(50):                                   # les deux demandes parties
        got = [(m, u) for (m, u, _) in calls["all"] if "/namespaces/" in u]
        if len(got) >= 2:
            break
        page.wait_for_timeout(100)
    assert got == [("POST", "apps/apps/namespaces/equipe-c"), ("DELETE", "apps/apps/namespaces/equipe-b")]


def test_remove_needs_the_typed_name_and_keeps_the_chart_on_request(context, flask_server):
    calls = {}
    page, _ = open_eso(context, flask_server, calls)
    page.locator('[data-eso-act="remove"]').click()
    m = page.locator(".eso-remove-modal")
    expect(m).to_contain_text("last value")
    go = m.locator("[data-eso-remove-go]")
    expect(go).to_be_disabled()
    m.locator("[data-eso-typed]").fill("apps/app")
    expect(go).to_be_disabled()
    m.locator("[data-eso-typed]").fill("apps/apps")
    m.locator("[data-eso-keep]").check()
    go.click()
    expect(page.locator("#eso-feedback")).to_contain_text(re.compile(r"d1|External Secrets removed"), timeout=5000)
    dels = [(u) for (meth, u, _) in calls["all"] if meth == "DELETE"]
    assert dels == ["apps/apps?keep_chart=1"]


def test_write_a_value_sends_the_fields_once_then_clears_them(context, flask_server):
    calls = {}
    page, _ = open_eso(context, flask_server, calls)
    w = page.locator("#capi-eso-body .eso-write")
    w.locator('[data-eso-w="namespace"]').select_option("equipe-b")
    w.locator('[data-eso-w="name"]').fill("db")
    w.locator("[data-eso-fk]").fill("password")
    w.locator("[data-eso-fv]").fill(SECRET)
    w.locator("[data-eso-field-add]").click()
    w.locator("[data-eso-fk]").nth(1).fill("user")
    w.locator("[data-eso-fv]").nth(1).fill("app-user")
    w.locator("[data-eso-write]").click()
    expect(w.locator("[data-eso-write-result]")).to_have_text("Version 3 written (person token).", timeout=5000)
    posted = [b for (meth, u, b) in calls["all"] if u == "apps/apps/secrets"]
    assert posted == [{"namespace": "equipe-b", "name": "db", "fields": {"password": SECRET, "user": "app-user"}}]
    expect(w.locator(".eso-field-row")).to_have_count(1)
    expect(w.locator("[data-eso-fv]")).to_have_value("")
    expect(w.locator("[data-eso-fk]")).to_have_value("")
    assert SECRET not in page.content()
    assert page.evaluate(f"[...document.querySelectorAll('input')].some(i => i.value === {json.dumps(SECRET)})") is False


def test_write_refused_still_clears_and_shows_the_code_text(context, flask_server):
    calls = {}
    page, _ = open_eso(context, flask_server, calls)
    page.route(re.compile(r".*/api/capi/harv-fake/eso/apps/apps/secrets$"), lambda r, q: fulfill(
        r, {"error": "the value was not written", "code": "openbao-person-expired", "action_id": "w2"}, 403))
    w = page.locator("#capi-eso-body .eso-write")
    w.locator('[data-eso-w="name"]').fill("db")
    w.locator("[data-eso-fk]").fill("password")
    w.locator("[data-eso-fv]").fill(SECRET)
    w.locator("[data-eso-write]").click()
    expect(w.locator("[data-eso-write-result]")).to_contain_text("token has expired", timeout=5000)
    expect(w.locator("[data-eso-fv]")).to_have_value("")
    assert SECRET not in page.content()


def test_operator_sees_admin_buttons_greyed_with_a_tooltip(context, flask_server):
    page, _ = open_eso(context, flask_server, {}, role="operator")
    body = page.locator("#capi-eso-body")
    for sel in ('[data-eso-act="setup"]', '[data-eso-act="remove"]', "[data-eso-ns-add]", '[data-eso-ns-del="equipe-a"]'):
        b = body.locator(sel)
        expect(b).to_be_disabled()
        expect(b).to_have_attribute("data-tip", "Reserved to administrators")
    expect(body.locator("[data-eso-write]")).to_be_enabled()          # écrire : opérateur


def test_viewer_cannot_write(context, flask_server):
    page, _ = open_eso(context, flask_server, {}, role="viewer")
    b = page.locator("#capi-eso-body [data-eso-write]")
    expect(b).to_be_disabled()
    expect(b).to_have_attribute("data-tip", re.compile("operator role"))


def test_without_openbao_a_blocking_finding_is_shown(context, flask_server):
    doc = state_doc(openbao=False, endpoint=None, auth=None, namespaces=[], stores=[], externalsecrets=[], example=None)
    page, _ = open_eso(context, flask_server, {}, doc=doc)
    body = page.locator("#capi-eso-body")
    expect(body.locator('[data-code="eso-needs-openbao"]')).to_contain_text("need OpenBao")
    expect(body.locator('[data-eso-act="remove"]')).to_be_disabled()
    expect(body.locator("[data-eso-example]")).to_have_count(0)


SHOTS = Path("/tmp/bmpost/shots-2100")


@pytest.mark.parametrize("size", [(1440, 900), (390, 844)])
def test_screenshots_desktop_and_narrow(context, flask_server, size):
    """Relecture visuelle : l'état et la fenêtre, large et étroit ; les
    tableaux défilent dans leur cadre au lieu d'élargir la page."""
    page, _ = open_eso(context, flask_server, {}, blocked=False)
    page.set_viewport_size({"width": size[0], "height": size[1]})
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.add_style_tag(content="#bottom-dock { display: none !important; }")
    body = page.locator("#capi-eso-body")
    body.scroll_into_view_if_needed()
    page.screenshot(path=str(SHOTS / f"capi-eso-{size[0]}.png"))
    for i, card in enumerate(body.locator(".eso-card").all()):       # le panneau défile en interne
        card.scroll_into_view_if_needed()
        page.screenshot(path=str(SHOTS / f"capi-eso-{size[0]}-card{i}.png"))
    # aucun débordement : les tableaux défilent dans leur cadre
    assert page.evaluate("""() => { const b = document.getElementById('capi-eso-body');
        return b.scrollWidth <= b.clientWidth + 1; }""")
    page.locator('[data-eso-act="setup"]').click()
    m = page.locator(".eso-modal")
    expect(m.locator("[data-eso-go]")).to_be_enabled(timeout=5000)
    page.screenshot(path=str(SHOTS / f"capi-eso-setup-{size[0]}.png"))
    m.locator('[data-eso-tab="checks"]').click()
    page.screenshot(path=str(SHOTS / f"capi-eso-checks-{size[0]}.png"))
