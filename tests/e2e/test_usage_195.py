"""v1.95.0 : Shared resources > Usage, API simulée.

La place dans le menu, le choix de la période (la demande suit), le tableau
par titulaire avec le quota et sa barre, le détail déplié par cluster et
type, le graphique par jour, le lien CSV (servi par la console), les états
vides qui parlent, et la fenêtre des quotas réservée aux administrateurs
(action suivie, retrait confirmé dans la page)."""

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

ACTION = "us0000000195"


def report(role="admin", holders=True, sampled=True):
    rows = [
        {"holder": {"kind": "user", "name": "alice"}, "booked": 12, "consumed": 7.5, "waste": 4.5, "out_of_rule": 0,
         "bookings": 2, "quota": {"monthly_hours": 40, "max_units": 2, "month_booked": 30, "remaining": 10,
                                  "over": False},
         "breakdown": [{"cluster": "harv-fake", "type": "vgpu", "booked": 8, "consumed": 6},
                       {"cluster": "gpudemo", "type": "node", "booked": 4, "consumed": 1.5}]},
        {"holder": {"kind": "group", "name": "ml-team"}, "booked": 6, "consumed": 9, "waste": 0, "out_of_rule": 3,
         "bookings": 1, "quota": {"monthly_hours": 5, "max_units": None, "month_booked": 6, "remaining": 0,
                                  "over": True},
         "breakdown": [{"cluster": "gpudemo", "type": "node", "booked": 6, "consumed": 9}]},
        {"holder": {"kind": "namespace", "name": "prod"}, "booked": 0, "consumed": 2, "waste": 0, "out_of_rule": 2,
         "bookings": 0, "quota": None, "breakdown": [{"cluster": "gpudemo", "type": "gpu", "booked": 0, "consumed": 2}]},
    ] if holders else []
    days = [{"day": f"2026-10-{d:02d}", "booked": (d % 3) * 2.0 if holders else 0, "consumed": (d % 4) * 1.0 if holders else 0}
            for d in range(1, 32)]
    return {"from": "2026-09-30T22:00:00Z", "to": "2026-10-31T23:00:00Z", "tz": 120, "quota_month": "2026-10",
            "holders": rows, "days": days,
            "totals": {"booked": 18, "consumed": 18.5, "waste": 4.5, "out_of_rule": 5} if holders else
            {"booked": 0, "consumed": 0, "waste": 0, "out_of_rule": 0},
            "quotas": [], "now": "2026-10-19T09:00:00Z", "interval": 300, "sampling": True, "role": role,
            "user": "patronne", "enforced": False,
            "sampler": {"first": "2026-10-01T00:00:00Z" if sampled else None,
                        "clusters": [{"cluster": "gpudemo", "ts": "2026-10-19T08:55:00Z", "state": "ok", "used": 5,
                                      "interval": 300, "detail": ""},
                                     {"cluster": "harv-fake", "ts": "2026-10-19T08:55:00Z", "state": "unreachable",
                                      "used": 0, "interval": 300, "detail": ""}] if sampled else []}}


QUOTAS = {"role": "admin", "quotas": [
    {"holder": {"kind": "user", "name": "alice"}, "monthly_hours": 40, "max_units": 2, "updated_by": "patronne",
     "updated_at": "2026-10-18T10:00:00Z"}]}


def open_usage(context, flask_server, rep):
    context.add_init_script(
        "if (!sessionStorage.getItem('usg-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','namespaces');"
        "localStorage.removeItem('harvester_ops_usage');"
        "sessionStorage.setItem('usg-init','1'); }")
    page = context.new_page()
    seen, sent = [], []

    def usage(route, req):
        seen.append(req.url)
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(rep))
    page.route(re.compile(r".*/api/usage\?.*$"), usage)

    def quotas(route, req):
        if req.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(QUOTAS))
        sent.append((req.method, req.post_data_json))
        return route.fulfill(status=202, content_type="application/json",
                             body=json.dumps({"action_id": ACTION, "holder": req.post_data_json.get("holder")}))
    page.route(re.compile(r".*/api/usage/quotas$"), quotas)
    page.route(f"**/api/stream/{ACTION}", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.Usage && window.App && App.getCurrentCluster()")
    page.mouse.move(1200, 600)
    page.evaluate("App.setTab('usage')")
    pane = page.locator("#tab-usage")
    expect(pane.locator(".usg-card")).to_be_visible()
    return page, pane, seen, sent, dialogs


def test_usage_is_the_third_shared_resource_and_shows_the_table(context, flask_server):
    page, pane, seen, _, _ = open_usage(context, flask_server, report())
    group = page.locator("#tab-group-shared")
    assert group.locator(".tab-child").evaluate_all("els => els.map(e => e.dataset.tab)") == ["gpus", "bookings", "usage"]
    assert group.locator('.tab[data-tab="usage"]').get_attribute("title")
    expect(group.locator('.tab[data-tab="usage"]')).to_have_class(re.compile("active"))
    rows = pane.locator("tbody tr.usg-row")
    expect(rows).to_have_count(3)
    alice = pane.locator('tr[data-holder="user|alice"]')
    expect(alice).to_contain_text("alice")
    expect(alice.locator(".usg-quota-text")).to_contain_text("30 / 40 h, 10 left")
    expect(alice.locator(".usg-quota-fill")).to_have_attribute("style", re.compile(r"width:\s*75"))
    expect(alice).to_contain_text("at most 2 unit(s) at once")
    expect(pane.locator('tr[data-holder="group|ml-team"] .usg-quota')).to_have_class(re.compile("is-over"))
    expect(pane.locator('tr[data-holder="namespace|prod"]')).to_contain_text("No booking · namespace prod")
    expect(pane.locator('[data-usg-total="out_of_rule"] .usg-tile-value')).to_have_text("5")
    expect(pane.locator(".usg-sampler")).to_contain_text("Not read at the last sample: harv-fake (unreachable)")
    # chaque contrôle a sa bulle
    for el in pane.locator(".res-tools button, .res-tools a, .res-tools input, .usg-toggle").all():
        if el.is_visible():
            assert el.get_attribute("data-tip"), el.evaluate("e => e.outerHTML")
    # la demande porte la période du mois et le fuseau
    q = parse_qs(urlparse(seen[-1]).query)
    assert set(q) == {"from", "to", "tz"}


def test_breakdown_expands_per_cluster_and_type(context, flask_server):
    _, pane, _, _, _ = open_usage(context, flask_server, report())
    subs = pane.locator('tr.usg-sub[data-sub-of="user|alice"]')
    expect(subs).to_have_count(2)
    expect(subs.first).to_be_hidden()
    pane.locator('tr[data-holder="user|alice"] [data-usg="toggle"]').click()
    expect(subs.first).to_be_visible()
    expect(subs.first).to_contain_text("harv-fake")
    expect(pane.locator('tr[data-holder="user|alice"] [data-usg="toggle"]')).to_have_attribute("aria-expanded", "true")
    pane.locator('tr[data-holder="user|alice"] [data-usg="toggle"]').click()
    expect(subs.first).to_be_hidden()


def test_chart_draws_one_group_per_day_with_bars_and_titles(context, flask_server):
    _, pane, _, _, _ = open_usage(context, flask_server, report())
    svg = pane.locator("svg.usg-chart")
    expect(svg).to_be_visible()
    assert svg.get_attribute("role") == "img" and "31 days" in svg.get_attribute("aria-label")
    expect(svg.locator("g.usg-day")).to_have_count(31)
    assert svg.locator("rect.usg-bar-consumed").count() > 0 and svg.locator("rect.usg-bar-booked").count() > 0
    assert "booked" in svg.locator("g.usg-day title").first.text_content()
    # hauteur réelle : le graphique n'est pas écrasé
    assert svg.bounding_box()["height"] > 100


def test_period_switch_changes_the_request_and_custom_shows_dates(context, flask_server):
    page, pane, seen, _, _ = open_usage(context, flask_server, report())
    first = parse_qs(urlparse(seen[-1]).query)
    pane.locator('[data-usg-period="last"]').click()
    page.wait_for_function("n => document.querySelectorAll('[data-usg-period].active').length === 1", arg=1)
    expect(pane.locator('[data-usg-period="last"]')).to_have_class(re.compile("active"))
    page.wait_for_timeout(300)
    last = parse_qs(urlparse(seen[-1]).query)
    assert last["to"] == first["from"]                    # le mois précédent finit où commence ce mois
    expect(pane.locator(".usg-custom")).to_be_hidden()
    pane.locator('[data-usg-period="custom"]').click()
    expect(pane.locator(".usg-custom")).to_be_visible()
    n = len(seen)
    pane.locator('[data-usg="from"]').fill("2026-10-05")
    pane.locator('[data-usg="to"]').fill("2026-10-06")
    pane.locator('[data-usg="to"]').dispatch_event("change")
    page.wait_for_function("n => true", arg=1)
    for _ in range(30):
        if len(seen) > n and "2026-10-0" in seen[-1]:
            break
        page.wait_for_timeout(100)
    q = parse_qs(urlparse(seen[-1]).query)
    assert q["from"][0][:10] in ("2026-10-04", "2026-10-05") and q["to"][0][:10] in ("2026-10-06", "2026-10-07")
    pane.locator('[data-usg-period="week"]').click()
    expect(pane.locator('[data-usg-period="week"]')).to_have_attribute("aria-selected", "true")


def test_csv_link_downloads_from_the_console(context, flask_server):
    page, pane, _, _, _ = open_usage(context, flask_server, report())
    link = pane.locator('[data-usg="csv"]')
    href = link.get_attribute("href")
    assert href.startswith("/api/usage.csv?from=") and "tz=" in href and link.get_attribute("download") is not None
    r = page.request.get(flask_server["base_url"].rstrip("/") + href)
    assert r.status == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text().startswith("holder_kind,holder,cluster,type,booked_gpu_hours")


def test_empty_states_speak(context, flask_server):
    _, pane, _, _, _ = open_usage(context, flask_server, report(holders=False, sampled=False))
    expect(pane.locator('[data-usg-sampler="never"]')).to_contain_text("has not sampled GPU use yet")
    expect(pane.locator('[data-usg-sampler="never"]')).to_contain_text("every 5 min")
    expect(pane.locator(".usg-chart-empty")).to_contain_text("nothing to chart")
    expect(pane.locator(".usg-empty")).to_contain_text("No booking and no GPU use in this period.")


def test_viewer_sees_no_quota_editor(context, flask_server):
    _, pane, _, _, _ = open_usage(context, flask_server, report(role="viewer"))
    expect(pane.locator("tbody tr.usg-row")).to_have_count(3)
    expect(pane.locator('[data-usg="quotas"]')).to_be_hidden()
    expect(pane.locator('[data-usg="quota-edit"]')).to_have_count(0)


def test_admin_quota_editor_sets_and_removes_with_in_page_confirmation(context, flask_server):
    page, pane, _, sent, dialogs = open_usage(context, flask_server, report())
    expect(pane.locator('[data-usg="quotas"]')).to_be_visible()
    # pas de bouton de quota sur une ligne « sans réservation »
    expect(pane.locator('tr[data-holder="namespace|prod"] [data-usg="quota-edit"]')).to_have_count(0)
    pane.locator('tr[data-holder="group|ml-team"] [data-usg="quota-edit"]').click()
    panel = page.locator(".floating-panel", has=page.locator(".usg-q-form"))
    expect(panel).to_be_visible()
    f = panel.locator(".usg-q-form")
    expect(f.locator('[name="kind"]')).to_have_value("group")
    expect(f.locator('[name="name"]')).to_have_value("ml-team")
    expect(panel.locator('tr[data-quota="user|alice"]')).to_contain_text("40")
    for el in panel.locator("button, input, select").all():
        if el.is_visible() and not el.evaluate("e => e.closest('.floating-panel-header, .fp-header, header')"):
            assert el.get_attribute("data-tip") or el.get_attribute("title"), el.evaluate("e => e.outerHTML")
    # vide : refusé dans la page
    f.locator('[name="hours"]').fill("")
    f.locator('[name="units"]').fill("")
    f.locator('[data-usgq="save"]').click()
    expect(f.locator(".of-msg")).to_contain_text("Give a monthly GPU-hours cap")
    assert sent == []
    f.locator('[name="hours"]').fill("12.5")
    f.locator('[name="units"]').fill("3")
    f.locator('[data-usgq="save"]').click()
    for _ in range(30):
        if sent:
            break
        page.wait_for_timeout(100)
    assert sent[0] == ("POST", {"holder": {"kind": "group", "name": "ml-team"}, "monthly_hours": 12.5, "max_units": 3})
    # retrait confirmé dans la page, jamais confirm()
    panel.locator('tr[data-quota="user|alice"] [data-usgq="delete"]').click()
    expect(panel.locator('tr[data-quota="user|alice"] .bkg-confirm')).to_contain_text("Remove this quota?")
    panel.locator('[data-usgq="delete-yes"]').click()
    for _ in range(30):
        if len(sent) > 1:
            break
        page.wait_for_timeout(100)
    assert sent[1] == ("DELETE", {"holder": {"kind": "user", "name": "alice"}})
    assert dialogs == []
