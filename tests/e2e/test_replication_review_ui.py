"""Écrans de réplication, corrections de la revue (API simulée) :
répliques de la classe de secours au-delà des nœuds de la cible (changer de
classe en un clic, ou accord explicite), carnet de reprise en retard dans la
fenêtre de bascule (protéger d'abord, ou basculer avec les réglages
précédents), Annuler qui rend les valeurs exactes ou se refuse en le
disant, TLS implicite des courriels, action annexe renommée avec un secret
masqué."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from test_replication_ui import (ACTION, PID, open_create, open_plans, open_view, plan_detail, ui,  # noqa: E402,F401
                                 ui_factory, wait_sent)

ok = lambda body, status=200: dict(status=status, content_type="application/json", body=json.dumps(body))  # noqa: E731


def _capacity(page, nodes=1):
    page.route(re.compile(r".*/api/storage-capacity/harv-dr$"), lambda r, q: r.fulfill(**ok(
        {"schedulable_nodes": nodes, "classes": {"harvester-longhorn": {"replicas": 3, "allocatable": 0},
                                                 "dr-nvme": {"replicas": 1, "allocatable": 10 ** 11}}})))


def _ready_create(page):
    panel = open_create(page)
    panel.locator('[name="to"]').select_option("harv-dr")
    panel.locator('[name="name"]').fill("ui-erp")
    panel.locator('[data-rpc-pick="default/ui-cirros-2"]').check()
    panel.locator('[data-rpc-netmap="default/lab-net"]').select_option("default/prod")
    return panel


def test_create_window_warns_when_replicas_exceed_target_nodes_and_offers_a_class(ui):
    page, sent, _ = ui
    _capacity(page)
    panel = _ready_create(page)
    box = panel.locator('[data-rpc="replicas"]')
    expect(box).to_contain_text("harvester-longhorn asks for 3 replicas")
    expect(panel.locator('[data-rpc-disk="default/ui-cirros-2/rootdisk"] [data-rpc-ready="warn"]')).to_be_visible()
    expect(panel.locator('[data-rpc="create"]')).to_be_disabled()
    expect(panel.locator('[data-rpc-check="fail"]').filter(has_text="more replicas than nodes")).to_be_visible()
    use = box.locator('[data-rpc-usesc="harvester-longhorn"]')
    assert use.get_attribute("data-tip")
    use.click()
    expect(panel.locator('[data-rpc-disk="default/ui-cirros-2/rootdisk"] [data-rpc-sc]')).to_have_value("dr-nvme")
    expect(panel.locator('[data-rpc="replicas"]')).to_have_count(0)
    expect(panel.locator('[data-rpc="create"]')).to_be_enabled()


def test_create_window_degraded_standby_needs_an_explicit_acknowledgement(ui):
    page, sent, _ = ui
    _capacity(page)
    panel = _ready_create(page)
    ack = panel.locator("[data-rpc-ackdeg]")
    assert panel.locator('[data-rpc="replicas"] label.tip').get_attribute("data-tip")
    ack.check()
    create = panel.locator('[data-rpc="create"]')
    expect(create).to_be_enabled()
    create.click()
    [post] = wait_sent(page, sent, lambda s: s[0] == "POST" and s[1] == "plans")
    assert post[2]["recovery"]["accept_degraded_standby"] is True


def _stale_actions(page, state):
    """Sortie des essais à blanc : refus `carnet-stale` tant que l'accord n'est pas donné."""
    def action(route, req):
        if state["accepted"]:
            out = {"id": ACTION, "status": "done", "result": {"output": {"ok": True, "message": "dry run",
                   "findings": [{"code": "carnet-stale", "level": "warn", "facts": {"keys": ["resize"], "vms": []}}]}}}
        else:
            out = {"id": ACTION, "status": "error", "result": {"output": {"ok": False, "code": "check",
                   "message": "the pre-check refuses this failover",
                   "findings": [{"code": "carnet-stale", "level": "block", "facts": {"keys": ["resize"], "vms": []}}]}}}
        route.fulfill(**ok(out))
    page.route(re.compile(r".*/api/action/rpact.*"), action)
    page.route(re.compile(r".*/api/stream/.*"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream",
        body='event: end\ndata: {"status": "%s"}\n\n' % ("done" if state["accepted"] else "error")))


def test_failover_window_names_stale_settings_and_needs_a_choice(ui):
    page, sent, _ = ui
    state = {"accepted": False}
    _stale_actions(page, state)
    pane = open_plans(page)
    pane.locator(f'.rp-plan-card[data-rp-plan="{PID}"] [data-rp-act="failover"]').click()
    dlg = page.locator(f"#fp-rp-failover-{PID}")
    stale = dlg.locator('[data-rp-fo="stale"]')
    expect(stale).to_contain_text("Changed since the last protect: resize")
    dlg.locator('[name="confirm"]').fill("erp")
    go = dlg.locator('[data-rp-fo="go"]')
    expect(go).to_be_disabled()
    # protéger d'abord : la protection part, puis l'essai à blanc se relance
    stale.locator('[data-rp-fo="protect-first"]').click()
    wait_sent(page, sent, lambda s: s[0] == "POST" and s[1] == f"plans/{PID}/protect")
    # ou basculer avec les réglages précédents : accord explicite
    state["accepted"] = True
    dlg.locator('[name="accept_stale"]').check()
    wait_sent(page, sent, lambda s: s[1] == f"plans/{PID}/failover" and s[2].get("dry_run")
              and s[2].get("accept_stale_carnet") is True)
    expect(go).to_be_enabled()
    go.click()
    [real] = wait_sent(page, sent, lambda s: s[1] == f"plans/{PID}/failover" and not s[2].get("dry_run"))
    assert real[2]["accept_stale_carnet"] is True


def test_contract_view_shows_a_protect_needed_banner(ui):
    page, sent, _ = ui
    d = plan_detail("admin")
    d["carnet_stale"] = {"keys": ["network_data"], "since": "2030-01-01T00:00:00Z"}
    page.route(re.compile(rf".*/api/replication/plans/{PID}$"), lambda r, q: r.fulfill(**ok(d)))
    view = open_view(page)
    banner = view.locator('[data-rpv="stale"]')
    expect(banner).to_contain_text("network_data")
    again = banner.locator('[data-rpv="protect-again"]')
    assert again.get_attribute("data-tip")
    again.click()
    wait_sent(page, sent, lambda s: s[0] == "POST" and s[1] == f"plans/{PID}/protect")


def test_undo_keeps_an_exact_objective_and_never_sends_an_unchanged_one(ui):
    page, sent, _ = ui
    d = plan_detail("admin")
    d["plan"]["rpo_objective_s"] = 90
    page.route(re.compile(rf".*/api/replication/plans/{PID}$"), lambda r, q: r.fulfill(**ok(d)))
    view = open_view(page)
    view.locator('[data-rpv-tab="settings"]').click()
    f = view.locator(".rp-settings")
    expect(f.locator('[name="rpo"]')).to_have_value("2")
    f.locator('[name="paused"]').check()
    f.locator('[data-rpv="save-follow"]').click()
    [patch] = wait_sent(page, sent, lambda s: s[0] == "PATCH")
    assert patch[2] == {"paused": True}                  # l'objectif de 90 s n'est pas arrondi à 2 min
    f.locator('[name="rpo"]').fill("3")
    f.locator('[data-rpv="save-follow"]').click()
    wait_sent(page, sent, lambda s: s[0] == "PATCH" and s[2].get("rpo") == "3m")
    view.locator('[data-rp-undo]').last.click()
    wait_sent(page, sent, lambda s: s[0] == "PATCH" and s[2].get("rpo") == "90s")


def test_no_undo_when_a_hook_secret_changes_and_the_reason_is_said(ui):
    page, sent, _ = ui
    view = open_view(page)
    view.locator('[data-rpv-tab="settings"]').click()
    view.locator('[data-rph-edit="0"]').click()
    view.locator('[name="h_svalue"]').fill("Bearer n3w")
    view.locator('[data-rph="keep"]').click()
    view.locator('[data-rpv="save-recovery"]').click()
    wait_sent(page, sent, lambda s: s[0] == "PATCH" and "recovery" in (s[2] or {}))
    expect(view.locator("[data-rp-noundo]")).to_contain_text("No undo")
    expect(view.locator("[data-rp-undo]")).to_have_count(0)


@pytest.mark.parametrize("change,message", [
    ("name", "renamed hook has no stored secret"),
    ("url", "address or the method changed"),
    ("path", "address or the method changed"),
    ("method", "address or the method changed"),
])
def test_hook_form_refuses_a_masked_secret_on_rename_or_new_host(ui, change, message):
    page, sent, _ = ui
    view = open_view(page)
    view.locator('[data-rpv-tab="settings"]').click()
    view.locator('[data-rph-edit="0"]').click()
    if change == "name":
        view.locator('[name="h_name"]').fill("dns-2")
    elif change == "url":
        view.locator('[name="h_url"]').fill("https://other.example/erp")
    elif change == "path":
        url = view.locator('[name="h_url"]')
        url.fill(url.input_value().split("/********")[0].rstrip("/") + "/other")
    else:
        view.locator('[name="h_method"]').select_option("DELETE")
    view.locator('[data-rph="keep"]').click()
    expect(view.locator('[data-rph="formmsg"]')).to_contain_text(message)


def test_mail_channel_implicit_tls_replaces_starttls(ui):
    page, sent, _ = ui
    pane = open_plans(page)
    pane.locator('[data-rp="notify"]').click()
    win = page.locator("#fp-rp-notify")
    win.locator('[data-rpn="kind"]').select_option("email")
    win.locator('[data-rpn="add"]').click()
    ch = win.locator('[data-rp-channel="1"]')
    ch.locator('[name="smtp_host"]').fill("smtp.example")
    ch.locator('[name="smtp_port"]').fill("465")
    tls = ch.locator('[name="implicit_tls"]')
    assert ch.locator('label.tip:has([name="implicit_tls"])').get_attribute("data-tip")
    tls.check()
    ch.locator('[name="from"]').fill("a@example")
    ch.locator('[name="to"]').fill("b@example")
    win.locator('[data-rpn="save"]').click()
    [put] = wait_sent(page, sent, lambda s: s[0] == "PUT")
    c = put[2]["channels"][1]
    assert c["implicit_tls"] is True and c["starttls"] is False and c["smtp_port"] == 465
