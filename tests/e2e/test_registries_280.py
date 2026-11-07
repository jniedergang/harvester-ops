"""2.8.0 (tâche 9) : onglet Registres, échéance du certificat, fenêtres
Renouveler le certificat et Nœuds Harvester, sur routes simulées.

* échéance du certificat d'un registre d'amorçage (date, jours, alerte) ;
* Renouveler : Lancer grisé jusqu'à l'essai des valeurs courantes (M-renew),
  CA non gardée : chemin « Nouvelle CA » et clusters en attente ;
* Nœuds Harvester : valeurs masquées montrées, case RKE2 exigée
  (M-rke2), rien à changer = Lancer grisé, conflit de miroir traduit ;
* nom hostile échappé ; boutons grisés pour un opérateur.
"""

import json
import re
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_registries_260 as t260  # noqa: E402

fulfill = t260.fulfill
HOSTILE = t260.HOSTILE
SOON = int(time.time()) + 10 * 86400
LATER = int(time.time()) + 400 * 86400


def open_detail(context, flask_server, calls, status=None, renew=None, nodes=None, role=None, registries=None):
    page, _ = t260.open_registries(context, flask_server, calls, role=role, registries=registries)
    st = dict({"ready": True, "volume": {"used_bytes": 1 << 30}, "repositories": 4, "cert_not_after": SOON,
               "findings": []}, **(status or {}))
    page.route(re.compile(r".*/api/registries/bootstrap/harv-fake/amorcage$"), lambda r, q: fulfill(r, st))

    def renew_cert(route, req):
        body = json.loads(req.post_data or "{}")
        calls.setdefault("renew", []).append(body)
        if body.get("dry_run"):
            return fulfill(route, (renew or (lambda b: {"registry": "amorcage", "not_after": LATER, "new_ca": False,
                                                          "blocked": False}))(body))
        fulfill(route, {"action_id": "rc1", "registry": "amorcage", "cluster": "harv-fake"}, 202)
    page.route(re.compile(r".*/api/registries/[^/]+/renew-cert$"), renew_cert)

    def hnodes(route, req):
        body = json.loads(req.post_data or "{}")
        calls.setdefault("nodes", []).append(body)
        if body.get("dry_run"):
            res = (nodes or (lambda b: (200, {"registry": "amorcage", "changed": True, "previous": '{"Mirrors":{}}',
                                              "value": '{"Mirrors":{"ghcr.io":{}},"Configs":{"172.16.2.76":{"Auth":{"Password":"***"}}}}',
                                              "additional_ca_changed": True, "blocked": False,
                                              "findings": [{"code": "harvester-rke2-restart", "level": "warn",
                                                            "facts": {"setting": "containerd-registry"}}]})))(body)
            return fulfill(route, res[1], res[0])
        fulfill(route, {"action_id": "hn1", "registry": "amorcage", "cluster": "harv-fake"}, 202)
    page.route(re.compile(r".*/api/registries/[^/]+/harvester-nodes$"), hnodes)
    page.locator('.reg-row[data-reg="amorcage"]').first.click()
    expect(page.locator("#registries-detail h3")).to_have_text("amorcage")
    return page


def test_certificate_expiry_is_shown_with_a_warning_under_30_days(context, flask_server):
    page = open_detail(context, flask_server, {})
    cert = page.locator('#registries-detail [data-x="cert"]')
    expect(cert).to_contain_text(time.strftime("%Y-%m-%d", time.gmtime(SOON)))
    expect(cert.locator(".badge.warn")).to_contain_text("day(s) left")


def test_renew_launch_waits_for_the_dry_run_of_the_current_values(context, flask_server):
    calls = {}
    page = open_detail(context, flask_server, calls)
    page.locator("#registries-detail [data-reg-renew]").click()
    m = page.locator(".reg-modal")
    launch = m.locator("[data-reg-launch]")
    expect(launch).to_be_enabled(timeout=5000)
    m.locator('[data-f="days"]').fill("365")
    expect(launch).to_be_disabled()                       # nouvelles valeurs : pas encore vérifiées
    expect(m.locator('[data-x="launch-wrap"]')).to_have_attribute("data-tip", re.compile("Waiting for the checks"))
    expect(launch).to_be_enabled(timeout=5000)
    assert calls["renew"][-1] == {"dry_run": True, "cluster": "harv-fake", "days": 365}
    m.locator('[data-reg-tab="summary"]').click()
    expect(m.locator('[data-x="summary"]')).to_contain_text(time.strftime("%Y-%m-%d", time.gmtime(LATER)))
    launch.click()
    expect(m.locator('[data-x="progress"]')).to_contain_text("rc1")
    assert calls["renew"][-1] == {"cluster": "harv-fake", "days": 365}
    assert all(t.get_attribute("data-tip") for t in m.locator(".tip").all())


def test_renew_without_kept_ca_offers_the_new_ca_path_and_lists_pending_clusters(context, flask_server):
    calls = {}

    def renew(b):
        if not b.get("new_ca"):
            return {"registry": "amorcage", "code": "registry-ca-key-missing", "blocked": True}
        return {"registry": "amorcage", "code": "registry-ca-transition-pending", "clusters": ["web/web", HOSTILE],
                "blocked": True}
    page = open_detail(context, flask_server, calls, renew=renew,
                       status={"findings": [{"code": "registry-ca-key-missing", "level": "ok",
                                             "facts": {"registry": "amorcage"}}]})
    expect(page.locator('#registries-detail [data-code="registry-ca-key-missing"]')).to_contain_text("two steps")
    page.locator("#registries-detail [data-reg-renew]").click()
    m = page.locator(".reg-modal")
    expect(m.locator('[data-f="newca"]')).to_be_checked()          # CA non gardée : cochée d'office
    expect(m.locator('[data-x="newca-help"]')).to_be_visible()
    m.locator('[data-reg-tab="checks"]').click()
    expect(m.locator('[data-code="registry-ca-transition-pending"]')).to_contain_text("web/web")
    expect(m.locator('[data-x="pending"] li')).to_have_count(2)
    expect(m.locator("[data-reg-launch]")).to_be_disabled()
    assert calls["renew"][-1]["new_ca"] is True
    m.locator('[data-reg-tab="params"]').click()
    m.locator('[data-f="newca"]').uncheck()
    m.locator('[data-reg-tab="checks"]').click()
    expect(m.locator('[data-code="registry-ca-key-missing"]')).to_be_visible(timeout=5000)
    expect(m.locator("[data-reg-launch]")).to_be_disabled()
    assert page.evaluate("window.__pwned") is None


def test_harvester_nodes_needs_the_rke2_acceptance_and_shows_masked_values(context, flask_server):
    calls = {}
    page = open_detail(context, flask_server, calls)
    page.locator("#registries-detail [data-reg-nodes]").click()
    m = page.locator(".reg-modal")
    m.locator('[data-reg-tab="checks"]').click()
    expect(m.locator('[data-x="value"]')).to_contain_text('"Password":"***"')
    expect(m.locator('[data-code="harvester-rke2-restart"]')).to_contain_text("restarts RKE2")
    launch = m.locator("[data-reg-launch]")
    expect(launch).to_be_disabled()
    expect(m.locator('[data-x="launch-wrap"]')).to_have_attribute("data-tip", re.compile("Accept the restart"))
    m.locator('[data-reg-tab="params"]').click()
    m.locator('[data-f="rke2"]').check()
    expect(launch).to_be_enabled()
    m.locator('[data-f="mode"]').select_option("remove")
    expect(launch).to_be_disabled()                        # nouvel essai attendu
    expect(launch).to_be_enabled(timeout=5000)
    assert calls["nodes"][-1] == {"dry_run": True, "cluster": "harv-fake", "remove": True}
    launch.click()
    expect(m.locator('[data-x="progress"]')).to_contain_text("hn1")
    assert calls["nodes"][-1] == {"cluster": "harv-fake", "remove": True}


def test_harvester_nodes_nothing_to_change_and_mirror_conflict_grey_the_launch(context, flask_server):
    state = {"mode": "same"}

    def nodes(b):
        if state["mode"] == "same":
            return 200, {"changed": False, "previous": "{}", "value": "{}", "additional_ca_changed": False,
                         "findings": [], "blocked": False}
        return 502, {"error": 'harvester-nodes-mirror-conflict {"origin": "docker.io", "endpoints": "other:5000"}'}
    page = open_detail(context, flask_server, {}, nodes=nodes)
    page.locator("#registries-detail [data-reg-nodes]").click()
    m = page.locator(".reg-modal")
    m.locator('[data-f="rke2"]').check()
    launch = m.locator("[data-reg-launch]")
    expect(m.locator('[data-x="launch-wrap"]')).to_have_attribute("data-tip", re.compile("Nothing to change"))
    expect(launch).to_be_disabled()
    state["mode"] = "conflict"
    m.locator('[data-f="mode"]').select_option("remove")
    m.locator('[data-reg-tab="checks"]').click()
    expect(m.locator('[data-code="harvester-nodes-mirror-conflict"]')).to_contain_text("docker.io through other:5000")
    expect(launch).to_be_disabled()


def test_operator_sees_both_gestures_greyed_and_hostile_name_is_escaped(context, flask_server):
    hostile = dict(t260.BOOT, name=HOSTILE[:30], clusters=["harv-fake"])
    page, _ = t260.open_registries(context, flask_server, {}, role="operator", registries=[hostile])
    page.locator(".reg-row").first.click()
    d = page.locator("#registries-detail")
    expect(d.locator("[data-reg-nodes]")).to_be_disabled()
    expect(d.locator("[data-reg-nodes]")).to_have_attribute("title", re.compile("admin role"))
    page.route(re.compile(r".*/api/registries/[^/]+(\?.*)?$"), lambda r, q: fulfill(r, dict(t260.BOOT)))
    page.locator(".reg-row").first.click()
    expect(d.locator("[data-reg-renew]")).to_be_disabled()
    expect(d.locator("[data-reg-renew]")).to_have_attribute("title", re.compile("admin role"))
    assert page.evaluate("window.__pwned") is None
