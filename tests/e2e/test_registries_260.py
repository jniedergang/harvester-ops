"""2.6.0 (tâche 12) : onglet Registres de Cluster API et bouton Publier de
l'onglet Artefacts, sur routes simulées.

* registre d'amorçage : constat bloquant, Lancer grisé avec la raison
  (M-launch), puis actif une fois le constat levé ; trois onglets ;
* déclaration d'un registre du client : identifiants envoyés une fois,
  jamais réaffichés dans le formulaire (M-creds-echo) ;
* suppression d'un registre d'amorçage : le nom retapé pour les données ;
* échappement d'un nom de registre et d'une référence d'image hostiles ;
* publication d'un paquet : constats avant le bouton, progression.
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
BOOT = {"name": "amorcage", "kind": "bootstrap", "endpoint": "172.16.2.76", "push_endpoint": "172.16.2.76:5443",
        "usage": ["images", "charts"], "mirrors": ["ghcr.io"], "clusters": ["harv-fake"], "cluster": "harv-fake",
        "address": "172.16.2.76", "size_gib": 20, "storage_class": "harv-rep1", "published": [], "revision": 1,
        "used_by": ["plan:site"]}
CLIENT = {"name": HOSTILE[:30], "kind": "external", "endpoint": "h.example:443", "usage": ["images"],
          "mirrors": [], "clusters": [], "published": 0}


def open_registries(context, flask_server, calls, registries=None, deploy_blocked=True, leftovers=(), role=None):
    if role:
        context.route(re.compile(r".*/api/whoami$"), lambda r, q: fulfill(
            r, {"role": role, "roles_active": True, "user": "u", "auth": "local"}))
    page = t220.open_auto(context, flask_server, "#automation/capi")
    regs = registries if registries is not None else [dict(BOOT, published=0)]
    state = {"blocked": deploy_blocked}
    page.route(re.compile(r".*/api/registries$"), lambda r, q: fulfill(r, {"registries": regs}))
    page.route(re.compile(r".*/api/registries/bootstrap/harv-fake/leftovers$"),
               lambda r, q: fulfill(r, {"cluster": "harv-fake", "leftovers": list(leftovers)}))
    page.route(re.compile(r".*/api/capi/harv-fake/inventory.*"),
               lambda r, q: fulfill(r, {"storage_classes": [{"name": "harv-rep1"}, {"name": "longhorn"}]}))

    def boot(route, req):
        body = json.loads(req.post_data or "{}")
        calls.setdefault("boot", []).append(body)
        if body.get("dry_run"):
            fs = [{"code": "registry-address-answers", "level": "block", "facts": {"address": body.get("address")}}] \
                if state["blocked"] else [{"code": "registry-pull-is-push", "level": "warn", "facts": {"registry": "x"}}]
            return fulfill(route, {"findings": fs, "blocked": state["blocked"]})
        fulfill(route, {"action_id": "r1", "registry": body["name"]}, 202)
    page.route(re.compile(r".*/api/registries/bootstrap/harv-fake$"), boot)
    page.route(re.compile(r".*/api/registries/bootstrap/harv-fake/amorcage$"),
               lambda r, q: fulfill(r, {"ready": True, "volume": {"used_bytes": 1 << 30}, "repositories": 4})
               if q.method == "GET" else (calls.setdefault("delete", []).append(json.loads(q.post_data or "{}")),
                                          fulfill(r, {"action_id": "r2"}, 202)))

    def one(route, req):
        name = req.url.split("/api/registries/")[1].split("?")[0]
        if req.method == "PUT":
            calls.setdefault("put", []).append(json.loads(req.post_data or "{}"))
            return fulfill(route, {"name": name, "revision": 1}, 201)
        fulfill(route, BOOT if name == "amorcage" else dict(CLIENT, used_by=[]))
    page.route(re.compile(r".*/api/registries/[^/]+(\?.*)?$"), one)
    page.route(re.compile(r".*/api/registries/[^/]+/attach/harv-fake$"),
               lambda r, q: (calls.setdefault("attach", []).append(json.loads(q.post_data or "{}")),
                             fulfill(r, {"action_id": "r3", "warnings": []}, 202)))
    page.locator('#tab-automation .sub-tab[data-capi-tab="registries"]').click()
    return page, state


def test_bootstrap_launch_is_greyed_with_its_reason_until_no_blocker(context, flask_server):
    calls = {}
    page, state = open_registries(context, flask_server, calls)
    page.locator("#btn-registries-bootstrap").click()
    m = page.locator(".reg-modal")
    expect(m.locator("[data-reg-tab]")).to_have_count(3)
    m.locator('[data-f="address"]').fill("172.16.2.76")
    launch = m.locator("[data-reg-launch]")
    expect(launch).to_be_disabled()
    expect(m.locator('[data-x="launch-wrap"]')).to_have_attribute("data-tip", re.compile("already answers at 172.16.2.76"))
    m.locator('[data-reg-tab="checks"]').click()
    expect(m.locator('[data-code="registry-address-answers"]')).to_be_visible()
    state["blocked"] = False
    m.locator('[data-reg-tab="params"]').click()
    m.locator('[data-f="address"]').fill("172.16.2.77")
    expect(launch).to_be_enabled(timeout=5000)
    m.locator('[data-reg-tab="summary"]').click()
    expect(m.locator('[data-x="summary"]')).to_contain_text("172.16.2.77")
    launch.click()
    expect(m).to_have_count(0)
    page.wait_for_timeout(800)                 # au-delà du délai de vérification différée
    # revue finale m-1 : la création est la dernière demande, aucune
    # vérification ne part après Lancer ni après la fermeture
    body = calls["boot"][-1]
    assert "dry_run" not in body and body["address"] == "172.16.2.77" and body["storage_class"] == "harv-rep1"
    assert len([b for b in calls["boot"] if "dry_run" not in b]) == 1
    assert all(t.get_attribute("data-tip") for t in page.locator("#registries-body .tip").all())


def test_declared_credentials_are_never_shown_again(context, flask_server):
    calls = {}
    page, _ = open_registries(context, flask_server, calls)
    page.locator("#btn-registries-declare").click()
    m = page.locator(".reg-modal")
    m.locator('[data-f="name"]').fill("harbor")
    m.locator('[data-f="endpoint"]').fill("harbor.example:443")
    m.locator('[data-c="push-user"]').fill("pusher")
    m.locator('[data-c="push-pass"]').fill("Secret-Push-42")
    m.locator('[data-c="pull-pass"]').fill("Secret-Pull-42")
    m.locator("[data-reg-go]").click()
    expect(m.locator('[data-x="feedback"]')).to_contain_text("r3")
    # M-creds-echo : les mots de passe ne restent pas dans le formulaire
    for k in ("push-pass", "pull-pass", "push-user"):
        expect(m.locator(f'[data-c="{k}"]')).to_have_value("")
    assert "Secret-Push-42" not in m.inner_html()
    assert calls["put"][0] == {"kind": "external", "endpoint": "harbor.example:443", "mirrors": [],
                               "usage": ["images", "charts"]}
    assert calls["attach"][0] == {"push": {"username": "pusher", "password": "Secret-Push-42"}}


def test_deleting_the_data_needs_the_typed_name(context, flask_server):
    calls = {}
    page, _ = open_registries(context, flask_server, calls)
    page.locator('.reg-row[data-reg="amorcage"]').click()
    detail = page.locator("#registries-detail")
    expect(detail).to_contain_text("plan:site")
    expect(detail.locator("[data-reg-attach]")).to_be_disabled()           # comptes générés
    detail.locator("[data-reg-delete]").click()
    m = page.locator(".reg-modal")
    go = m.locator("[data-reg-go]")
    expect(go).to_be_enabled()                                              # volume gardé : pas de nom
    m.locator('[data-f="data"]').check()
    expect(go).to_be_disabled()
    m.locator('[data-f="typed"]').fill("amorcag")
    expect(go).to_be_disabled()
    m.locator('[data-f="typed"]').fill("amorcage")
    expect(go).to_be_enabled()
    go.click()
    expect(m).to_have_count(0)
    assert calls["delete"] == [{"delete_data": True, "confirm": "amorcage"}]


def test_hostile_names_are_escaped(context, flask_server):
    calls = {}
    page, _ = open_registries(context, flask_server, calls, registries=[dict(BOOT, published=0), CLIENT])
    expect(page.locator("#registries-body .reg-table")).to_contain_text(HOSTILE[:30])
    page.locator(".reg-row").nth(1).click()
    expect(page.locator("#registries-detail h3")).to_contain_text(HOSTILE[:30])
    assert page.locator("#registries-body img").count() == 0
    assert page.evaluate("window.__pwned") is None


def test_publish_shows_the_checks_before_the_button_and_follows(context, flask_server):
    page = t220.open_auto(context, flask_server, "#automation/capi")
    pk = [{"name": "socle", "version": "2026.11", "items": 2, "trust": "valid", "principal": "ops"}]
    detail = {"name": "socle", "version": "2026.11", "trust": {"state": "valid"},
              "index": {"items": [{"name": "podinfo", "kind": "chart"}, {"name": "quotas", "kind": "manifest"}]},
              "images": [{"name": HOSTILE}], "published": []}
    sent = []
    state = {"blocked": True}
    page.route(re.compile(r".*/api/artefacts$"), lambda r, q: fulfill(r, {"packages": pk, "strict": False}))
    page.route(re.compile(r".*/api/artefacts/socle/2026\.11$"), lambda r, q: fulfill(r, detail))
    page.route(re.compile(r".*/api/registries$"), lambda r, q: fulfill(r, {"registries": [dict(BOOT, published=0)]}))

    def publish(route, req):
        body = json.loads(req.post_data or "{}")
        sent.append(body)
        if body.get("dry_run"):
            fs = [{"code": "publish-tag-differs", "level": "block",
                   "facts": {"reference": f"amorcage/{HOSTILE}"}}] if state["blocked"] else []
            return fulfill(route, {"blocked": state["blocked"], "findings": fs,
                                   "todo": [{"kind": "chart", "name": "podinfo:6.9.2", "state": "new"}]})
        fulfill(route, {"action_id": "p1"}, 202)
    page.route(re.compile(r".*/api/artefacts/socle/2026\.11/publish$"), publish)
    page.route("**/api/stream/p1", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream",
        body='event: step\ndata: {"message": "images 1/1"}\n\nevent: end\ndata: {"status": "done"}\n\n'))
    page.locator('#tab-automation .sub-tab[data-capi-tab="artefacts"]').click()
    page.locator('.art-row[data-art-name="socle"]').click()
    page.locator("[data-art-publish]").click()
    m = page.locator(".art-publish")
    go = m.locator("[data-art-go]")
    expect(m.locator('[data-code="publish-tag-differs"]')).to_contain_text(HOSTILE)       # texte, pas balise
    expect(go).to_be_disabled()
    expect(m.locator('[data-x="go-wrap"]')).to_have_attribute("data-tip", re.compile("Blocked"))
    expect(m.locator('[data-item="quotas"]')).to_be_disabled()                           # manifeste : non publié
    state["blocked"] = False
    m.locator('[data-item="podinfo"]').uncheck()
    m.locator('[data-item="podinfo"]').check()
    expect(go).to_be_enabled(timeout=5000)
    go.click()
    expect(m.locator('[data-x="progress"]')).to_contain_text("Published", timeout=5000)
    assert sent[-1] == {"cluster": "harv-fake", "registry": "amorcage", "items": None}
    assert page.evaluate("window.__pwned") is None


def test_forget_uses_a_page_dialog_with_translated_refusals_then_follows_the_action(context, flask_server):
    """Revue finale 2.6.0 : m-4 (fenêtre de la page, refus traduits, jamais
    de boîte native) et I-3 (l'oubli est une action suivie)."""
    calls = {}
    harbor = {"name": "harbor", "kind": "external", "endpoint": "h.example:443", "usage": ["images"],
              "mirrors": [], "clusters": ["harv-fake"], "published": 0}
    page, _ = open_registries(context, flask_server, calls, registries=[harbor])
    natives = []
    page.on("dialog", lambda d: natives.append(d.type))
    answers = [({"error": "registry-in-use: plan:site", "code": "registry-in-use", "used_by": ["plan:site"]}, 409),
               ({"action_id": "f1f1f1f1f1f1", "registry": "harbor"}, 202)]

    def one(route, req):
        if req.method == "DELETE":
            calls.setdefault("forget", []).append(req.url)
            body, status = answers.pop(0)
            return fulfill(route, body, status)
        fulfill(route, dict(harbor, used_by=[]))
    page.route(re.compile(r".*/api/registries/harbor(\?.*)?$"), one)
    page.locator('.reg-row[data-reg="harbor"]').click()
    page.locator("#registries-detail [data-reg-delete]").click()
    m = page.locator(".reg-modal")
    expect(m).to_contain_text("Its accounts are removed from the attached Harvester clusters")
    m.locator("[data-reg-go]").click()
    expect(m.locator('[data-x="feedback"]')).to_have_text("Refused: harbor is used by plan:site.")
    m.locator("[data-reg-go]").click()
    expect(m.locator('[data-x="feedback"]')).to_contain_text("f1f1f1f1f1f1")
    assert natives == [] and len(calls["forget"]) == 2


def test_bootstrap_delete_aims_at_the_cluster_of_the_registry(context, flask_server):
    """Revue finale m-2 : vu depuis un autre cluster Harvester, Supprimer
    vise le cluster du registre, pas celui choisi dans la page."""
    calls = {}
    other = dict(BOOT, cluster="harv-other", clusters=["harv-other"], published=0)
    page, _ = open_registries(context, flask_server, calls, registries=[other])
    page.route(re.compile(r".*/api/registries/bootstrap/harv-other/amorcage$"),
               lambda r, q: (calls.setdefault("other", []).append(q.method), fulfill(r, {"action_id": "r9"}, 202)))
    page.locator('.reg-row[data-reg="amorcage"]').click()
    page.locator("#registries-detail [data-reg-delete]").click()
    page.locator(".reg-modal [data-reg-go]").click()
    expect(page.locator(".reg-modal")).to_have_count(0)
    assert calls.get("other") == ["DELETE"] and "delete" not in calls


def test_check_is_greyed_for_a_viewer_with_the_reason(context, flask_server):
    """Revue finale m-3."""
    calls = {}
    page, _ = open_registries(context, flask_server, calls, role="viewer")
    page.locator('.reg-row[data-reg="amorcage"]').click()
    chk = page.locator("#registries-detail [data-reg-check]")
    expect(chk).to_be_disabled()
    assert "operator role" in chk.get_attribute("data-tip") and "operator role" in chk.get_attribute("title")


def test_the_leftovers_of_a_failed_deploy_are_listed_and_deleted(context, flask_server):
    """Revue finale I-2 : restes d'un déploiement échoué (sans déclaration)
    listés avec leurs objets, supprimés depuis l'onglet."""
    calls = {}
    left = {"registry": "amorcage2", "cluster": "harv-fake", "address": "172.16.2.78", "origin": {"plan": "site"},
            "objects": ["deployments/amorcage2", "services/amorcage2"]}
    page, _ = open_registries(context, flask_server, calls, leftovers=[left])
    page.route(re.compile(r".*/api/registries/bootstrap/harv-fake/amorcage2$"),
               lambda r, q: (calls.setdefault("left", []).append((q.method, json.loads(q.post_data or "{}"))),
                             fulfill(r, {"action_id": "r8"}, 202)))
    row = page.locator('.reg-row[data-reg="amorcage2"]')
    expect(row).to_contain_text("Leftover")
    row.click()
    detail = page.locator("#registries-detail")
    expect(detail).to_contain_text("services/amorcage2")
    expect(detail).to_contain_text("failed deployment of the plan site")
    detail.locator("[data-reg-delete]").click()
    page.locator(".reg-modal [data-reg-go]").click()
    expect(page.locator(".reg-modal")).to_have_count(0)
    assert calls["left"] == [("DELETE", {})]


def test_check_says_reachability_is_seen_from_the_harvester_cluster_only(context, flask_server):
    """Revue finale I-4 (repli du point 192) : le verdict, puis
    l'avertissement du réseau des VMs."""
    calls = {}
    page, _ = open_registries(context, flask_server, calls)
    page.route(re.compile(r".*/api/registries/amorcage/check/harv-fake$"), lambda r, q: fulfill(r, {
        "findings": [{"code": "registry-vm-network-unchecked", "level": "warn",
                      "facts": {"registry": "amorcage", "endpoint": "172.16.2.76", "network": "",
                                "reason": "no-network"}}], "blocked": False}))
    page.locator('.reg-row[data-reg="amorcage"]').click()
    page.locator("#registries-detail [data-reg-check]").click()
    box = page.locator('#registries-detail [data-x="check"]')
    expect(box).to_contain_text("Reachable, CA valid, accounts accepted.")
    expect(box.locator('[data-code="registry-vm-network-unchecked"]')).to_contain_text(
        "Reachability of 172.16.2.76 from the VM network is not checked: no VM network chosen.")
