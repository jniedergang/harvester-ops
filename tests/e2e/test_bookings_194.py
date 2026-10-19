"""v1.94.0 : Shared resources > Bookings, API simulée.

La place dans le menu, l'avertissement « pas encore appliqué sur le
cluster », le calendrier (barres, voies, marque d'aujourd'hui, semaines),
les tableaux et l'annulation confirmée dans la page (jamais `confirm()`), le
formulaire avec son contrôle à blanc qui dit pourquoi il refuse, le badge et
le bouton Réserver de l'inventaire GPU, et l'indication dans le formulaire
de création de charge."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

ACTION = "bk0000000194"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def booking(bid, start, end, state, unit="", count=1, holder="alice", mine=True, can_cancel=True,
            model="NVIDIA H100-20C", typ="vgpu", cluster="harv-fake"):
    return {"id": bid, "cluster": cluster, "type": typ, "unit": unit, "model": model, "count": count,
            "holder": {"kind": "user", "name": holder}, "start": iso(start), "end": iso(end),
            "target": {"kind": "vm", "namespace": "ml", "name": "notebook"} if unit else None,
            "note": "", "created_by": holder, "created_at": iso(NOW - timedelta(days=1)),
            "cancelled_at": "", "cancelled_by": "", "state": state, "mine": mine, "can_cancel": can_cancel}


LIST = {"user": "alice", "role": "operator", "now": iso(NOW), "enforced": False, "max_days": 90, "bookings": [
    booking("bk-aaaaaaaa0001", NOW - timedelta(hours=1), NOW + timedelta(hours=3), "active", unit="gpu-n1-0000af005"),
    booking("bk-aaaaaaaa0002", NOW - timedelta(minutes=30), NOW + timedelta(hours=5), "active", count=1,
            holder="bob", mine=False, can_cancel=False),
    booking("bk-aaaaaaaa0003", NOW - timedelta(hours=1), NOW + timedelta(hours=2), "active",
            typ="node", model="nvidia.com/gpu", count=2, holder="ml-team"),
]}
OPTIONS = {"cluster": "harv-fake", "now": iso(NOW), "enforced": False, "catalog": [
    {"type": "vgpu", "model": "NVIDIA H100-20C", "capacity": 2, "used": 1, "booked_now": 1, "free_now": 1,
     "mine_now": ["bk-aaaaaaaa0001"], "units": [
         {"id": "gpu-n1-0000af004", "name": "gpu-n1-0000af004", "node": "gpu-n1", "address": "0000:af:00.4", "state": "used", "booked_now": False},
         {"id": "gpu-n1-0000af005", "name": "gpu-n1-0000af005", "node": "gpu-n1", "address": "0000:af:00.5", "state": "free", "booked_now": True}]},
    {"type": "mig", "model": "MIG 3g.40gb", "capacity": 2, "used": 1, "booked_now": 0, "free_now": 1, "mine_now": [], "units": []},
    {"type": "node", "model": "nvidia.com/gpu", "capacity": 4, "used": 2, "booked_now": 2, "free_now": 2, "mine_now": [], "units": []},
]}


def unit(uid, typ, model, state="free", **extra):
    return dict({"id": uid, "type": typ, "cluster": "harv-fake", "node": "gpu-n1", "name": uid, "model": model,
                 "address": "0000:af:00.5", "parent": "", "resource": "", "state": state, "total": 1, "used": 0,
                 "free": 1, "consumers": []}, **extra)


GPUS_ALL = {"clusters": [{"cluster": "harv-fake", "state": "ok", "count": 3, "addon": True,
                          "available": {"addon": True, "gpu": True, "vgpu": True, "mig": True, "node": True}}],
            "units": [unit("gpu-n1-0000af004", "vgpu", "NVIDIA H100-20C", "used", used=1, free=0),
                      unit("gpu-n1-0000af005", "vgpu", "NVIDIA H100-20C"),
                      unit("gpu-n1-0000af006", "vgpu", "", "disabled"),
                      unit("gpu-n3/nvidia.com/gpu", "node", "nvidia.com/gpu", total=4, used=2, free=2,
                           resource="nvidia.com/gpu", address="")],
            "summary": {t: {"total": 0, "used": 0, "free": 0} for t in ("gpu", "vgpu", "mig", "node")},
            # v1.94.1 : les lots réservables ; l'A100 n'a aucune réservation
            "pools": [{"cluster": "harv-fake", "type": "gpu", "model": "NVIDIA A100", "capacity": 1, "used": 0},
                      {"cluster": "harv-fake", "type": "vgpu", "model": "NVIDIA H100-20C", "capacity": 2, "used": 1},
                      {"cluster": "harv-fake", "type": "node", "model": "nvidia.com/gpu", "capacity": 4, "used": 3}]}


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "if (!sessionStorage.getItem('bkg-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','namespaces');"
        "localStorage.removeItem('harvester_ops_bookings');"
        "localStorage.removeItem('harvester_ops_gpus');"
        "sessionStorage.setItem('bkg-init','1'); }")
    page = context.new_page()
    sent = []
    json_ok = lambda body: dict(status=200, content_type="application/json", body=json.dumps(body))  # noqa: E731

    def bookings(route, req):
        if req.method == "POST":
            body = req.post_data_json
            sent.append(("POST", "bookings", body))
            if body.get("count", 1) > 1:
                return route.fulfill(status=409, content_type="application/json", body=json.dumps({
                    "error": "conflict", "capacity": 2, "hint": "refused",
                    "conflicts": [{"code": "capacity", "peak": 3, "capacity": 2, "model": body.get("model"), "at": body["start"]}]}))
            return route.fulfill(status=202, content_type="application/json",
                                 body=json.dumps({"action_id": ACTION, "id": "bk-bbbbbbbb0001", "enforced": False}))
        return route.fulfill(**json_ok(LIST))
    page.route(re.compile(r".*/api/bookings(\?.*)?$"), bookings)

    def check(route, req):
        body = req.post_data_json
        sent.append(("POST", "check", body))
        if body.get("count", 1) > 1:
            return route.fulfill(**json_ok({"ok": False, "capacity": 2, "conflicts": [
                {"code": "capacity", "peak": 3, "capacity": 2, "model": body.get("model"), "at": body["start"],
                 "message": "3 x vgpu would be booked"}]}))
        if body.get("unit") == "gpu-n1-0000af005":
            return route.fulfill(**json_ok({"ok": False, "capacity": 2, "conflicts": [
                {"code": "unit-taken", "holder": {"kind": "user", "name": "alice"}, "start": LIST["bookings"][0]["start"],
                 "end": LIST["bookings"][0]["end"], "booking": "bk-aaaaaaaa0001"}]}))
        return route.fulfill(**json_ok({"ok": True, "capacity": 2, "conflicts": []}))
    page.route(re.compile(r".*/api/bookings/check$"), check)
    page.route(re.compile(r".*/api/bookings/options/harv-fake(\?.*)?$"), lambda r, q: r.fulfill(**json_ok(OPTIONS)))

    def cancel(route, req):
        sent.append(("POST", req.url.split("/api/")[1], None))
        return route.fulfill(status=202, content_type="application/json", body=json.dumps({"action_id": ACTION}))
    page.route(re.compile(r".*/api/bookings/[^/]+/cancel$"), cancel)
    page.route(re.compile(r".*/api/vms/harv-fake(\?.*)?$"), lambda r, q: r.fulfill(**json_ok(
        {"vms": [{"namespace": "ml", "name": "notebook"}]})))
    page.route(re.compile(r".*/api/containers/harv-fake(\?.*)?$"), lambda r, q: r.fulfill(**json_ok(
        {"cluster": "harv-fake", "denials": [], "workloads": [
            {"namespace": "ml", "name": "trainer", "kind": "Deployment", "state": "Running", "ready": 1, "desired": 1,
             "images": ["cuda:12"], "gpus": 2, "created": "2026-10-16T10:00:00Z", "labels": {}, "pods": []}]})))
    page.route(re.compile(r".*/api/gpus/harv-fake(\?.*)?$"), lambda r, q: r.fulfill(**json_ok(
        {"cluster": "harv-fake", "units": [], "summary": {"node": {"total": 4, "used": 2, "free": 2}}})))
    page.route(re.compile(r".*/api/gpus-all(\?.*)?$"), lambda r, q: r.fulfill(**json_ok(GPUS_ALL)))
    page.route(f"**/api/stream/{ACTION}", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.Bookings && window.App && App.getCurrentCluster()")
    page.mouse.move(1200, 600)
    return page, sent, dialogs


def open_bookings(page):
    page.evaluate("App.setTab('bookings')")
    pane = page.locator("#tab-bookings")
    expect(pane.locator(".bkg-card")).to_be_visible()
    return pane


def test_bookings_lives_under_shared_resources_and_says_it_is_not_enforced(ui):
    page, _, _ = ui
    group = page.locator("#tab-group-shared")
    assert group.locator(".tab-child").evaluate_all("els => els.map(e => e.dataset.tab)") == ["gpus", "bookings"]
    assert group.locator('.tab[data-tab="bookings"]').get_attribute("title")
    pane = open_bookings(page)
    expect(group.locator('.tab[data-tab="bookings"]')).to_have_class(re.compile("active"))
    expect(pane.locator(".bkg-notice")).to_contain_text("not yet enforced on the cluster")
    for el in pane.locator(".res-tools button, .res-tools select").all():
        assert el.get_attribute("data-tip"), el.evaluate("e => e.outerHTML")


def test_calendar_renders_bars_lanes_today_marker_and_weeks(ui):
    page, _, _ = ui
    pane = open_bookings(page)
    expect(pane.locator(".bkg-row[data-pool]")).to_have_count(3)
    vg = pane.locator('.bkg-row[data-pool="harv-fake|vgpu|NVIDIA H100-20C"]')
    expect(vg.locator(".bkg-bar")).to_have_count(2)
    # deux créneaux qui se chevauchent : deux voies
    tops = sorted(vg.locator(".bkg-bar").evaluate_all("els => els.map(e => e.style.top)"))
    assert tops == ["28px", "4px"] or tops == ["4px", "28px"]
    expect(vg.locator(".bkg-bar.is-mine")).to_have_count(1)
    assert vg.locator(".bkg-bar").first.get_attribute("data-tip")
    expect(vg.locator(".bkg-today")).to_have_count(1)
    expect(pane.locator(".bkg-dayname")).to_have_count(7)
    # deux semaines plus loin : rien (un créneau du soir peut déborder sur la semaine suivante)
    pane.locator('[data-bkg="week-next"]').click()
    pane.locator('[data-bkg="week-next"]').click()
    expect(pane.locator(".bkg-bar")).to_have_count(0)
    expect(pane.locator(".bkg-row[data-pool]")).to_have_count(3)         # les lots restent, vides
    expect(pane.locator(".bkg-empty")).to_contain_text("No booking this week")
    pane.locator('[data-bkg="week-today"]').click()
    expect(pane.locator(".bkg-bar")).to_have_count(3)


def test_tables_and_cancel_with_in_page_confirmation(ui):
    page, sent, dialogs = ui
    pane = open_bookings(page)
    pane.locator('[data-bkg-view="all"]').click()
    rows = pane.locator("tbody tr[data-booking]")
    expect(rows).to_have_count(3)
    expect(pane.locator('tr[data-booking="bk-aaaaaaaa0002"] [data-bkg="cancel"]')).to_have_count(0)    # celle de bob
    pane.locator('[data-bkg-view="mine"]').click()
    expect(rows).to_have_count(2)
    row = pane.locator('tr[data-booking="bk-aaaaaaaa0001"]')
    expect(row).to_contain_text("ml/notebook")
    row.locator('[data-bkg="cancel"]').click()
    expect(row.locator(".bkg-confirm")).to_contain_text("Cancel this booking?")
    row.locator('[data-bkg="cancel-no"]').click()
    expect(row.locator(".bkg-confirm")).to_have_count(0)
    assert sent == []
    row.locator('[data-bkg="cancel"]').click()
    row.locator('[data-bkg="cancel-yes"]').click()
    expect(page.locator('tr[data-booking="bk-aaaaaaaa0001"]')).to_be_visible()
    for _ in range(30):
        if sent:
            break
        page.wait_for_timeout(100)
    assert sent == [("POST", "bookings/bk-aaaaaaaa0001/cancel", None)]
    assert dialogs == []


def test_new_booking_form_checks_the_slot_and_says_why(ui):
    page, sent, _ = ui
    pane = open_bookings(page)
    pane.locator('[data-bkg="new"]').click()
    panel = page.locator(".floating-panel", has=page.locator(".bkg-form"))
    expect(panel.locator(".bkg-form-notice")).to_contain_text("not yet enforced")
    what = panel.locator('[name="what"]')
    expect(what.locator("option")).to_have_count(3)                       # le modèle + deux unités
    expect(what.locator('option[value="u:gpu-n1-0000af005"]')).to_contain_text("booked now")
    expect(panel.locator("[data-bkg-free]")).to_contain_text("1 free now of 2")
    expect(panel.locator("[data-bkg-free]")).to_contain_text("You hold 1 booking")
    expect(panel.locator('[name="target"] option')).to_have_count(3)
    panel.locator('[name="count"]').fill("2")
    expect(panel.locator("[data-bkg-check]")).to_contain_text("would be refused", timeout=5000)
    expect(panel.locator("[data-bkg-check]")).to_contain_text("3 x NVIDIA H100-20C would be booked")
    panel.locator('[name="count"]').fill("1")
    expect(panel.locator("[data-bkg-check]")).to_contain_text("This slot is free", timeout=5000)
    what.select_option("u:gpu-n1-0000af005")
    expect(panel.locator('[name="count"]')).to_be_disabled()
    expect(panel.locator("[data-bkg-check]")).to_contain_text("already booked by alice", timeout=5000)
    what.select_option("m:NVIDIA H100-20C")
    panel.locator('[name="target"]').select_option("vm:ml/notebook")
    panel.locator('[name="end"]').fill("2020-01-01T10:00")
    expect(panel.locator("[data-bkg-check]")).to_contain_text("end must come after", timeout=5000)
    panel.locator('[name="end"]').fill(panel.locator('[name="start"]').input_value()[:11] + "23:59")
    sent.clear()
    panel.locator("[data-bkg-submit]").click()
    # v1.94.1 : acceptée, la fenêtre se ferme ; la fin se dit par une notification
    expect(panel).to_have_count(0)
    expect(page.locator(".vma-toast")).to_contain_text("Slot booked")
    [post] = [s for s in sent if s[1] == "bookings"]
    body = post[2]
    assert body["cluster"] == "harv-fake" and body["type"] == "vgpu" and body["model"] == "NVIDIA H100-20C"
    assert body["count"] == 1 and body["target"] == {"kind": "vm", "namespace": "ml", "name": "notebook"}
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:00\.000Z$", body["start"]) and "holder" not in body


def test_gpu_inventory_badge_and_book_prefills_the_form(ui):
    page, _, _ = ui
    page.evaluate("App.setTab('gpus')")
    pane = page.locator("#tab-gpus")
    row = pane.locator('tr[data-unit="harv-fake|gpu-n1-0000af005"]')
    expect(row.locator('.bkg-badge[data-bkg-badge="unit"]')).to_contain_text("Booked by alice until", timeout=8000)
    # v1.94.1 : la réservation par nombre de bob se dit au lot, sur chaque unité
    expect(row.locator('.bkg-badge[data-bkg-badge="pool"]')).to_have_text(re.compile(r"2 of 2 booked now"))
    other = pane.locator('tr[data-unit="harv-fake|gpu-n1-0000af004"]')
    expect(other.locator(".bkg-badge")).to_have_count(1)
    expect(other.locator(".bkg-badge")).to_have_text(re.compile(r"2 of 2 booked now"))
    expect(other).not_to_contain_text("Booked by")
    node = pane.locator('tr[data-unit="harv-fake|gpu-n3/nvidia.com/gpu"] .bkg-badge')
    expect(node).to_have_text(re.compile(r"2 of 4 booked now"))
    expect(node).not_to_contain_text("ml-team")
    assert "ml-team" in node.get_attribute("data-tip")
    expect(pane.locator('tr[data-unit="harv-fake|gpu-n1-0000af006"] [data-gpu="book"]')).to_have_count(0)   # désactivée
    assert row.locator('[data-gpu="book"]').get_attribute("data-tip")
    row.locator('[data-gpu="book"]').click()
    panel = page.locator(".floating-panel", has=page.locator(".bkg-form"))
    expect(panel.locator('[name="what"]')).to_have_value("u:gpu-n1-0000af005", timeout=5000)
    expect(panel.locator('[name="type"]')).to_have_value("vgpu")
    expect(panel.locator('[name="count"]')).to_be_disabled()


def test_container_form_hints_availability_and_links_to_bookings(ui):
    page, _, _ = ui
    page.evaluate("App.setTab('containers')")
    page.locator("#containers-host .ct-create-btn").click()
    panel = page.locator(".floating-panel", has=page.locator(".ct-form [name=image]"))
    hint = panel.locator("[data-bkg-hint]")
    expect(hint).to_contain_text("2 free now of 4, 2 booked now", timeout=5000)
    expect(hint).not_to_contain_text("H100")                               # les GPU de conteneurs seulement
    hint.locator("[data-bkg-open]").click()
    expect(page.locator('#tab-group-shared .tab[data-tab="bookings"]')).to_have_class(re.compile("active"))


def test_calendar_shows_every_bookable_pool_with_what_is_free(ui):
    """v1.94.1 : le calendrier montrait seulement les lots réservés dans la
    semaine. Tous les lots réservables des clusters affichés ont une ligne,
    vide s'il n'y a rien, avec ce qui reste libre maintenant."""
    page, _, _ = ui
    pane = open_bookings(page)
    a100 = pane.locator('.bkg-row[data-pool="harv-fake|gpu|NVIDIA A100"]')
    expect(a100).to_have_class(re.compile("is-empty"), timeout=8000)
    expect(a100.locator(".bkg-bar")).to_have_count(0)
    expect(a100.locator(".bkg-free-now")).to_have_text("1 of 1 free now")
    # 2 unités, 1 utilisée, 2 réservées maintenant : plus rien de libre
    vg = pane.locator('.bkg-row[data-pool="harv-fake|vgpu|NVIDIA H100-20C"] .bkg-free-now')
    expect(vg).to_have_text("0 of 2 free now")
    expect(vg).to_have_class(re.compile("is-full"))
    assert "2 booked now" in vg.get_attribute("data-tip")
    expect(pane.locator('.bkg-row[data-pool="harv-fake|node|nvidia.com/gpu"] .bkg-free-now')).to_have_text("1 of 4 free now")  # 3 en service, 2 réservées


def test_calendar_cluster_filter_applies_to_pools(ui):
    page, _, _ = ui
    GPUS_ALL["pools"].append({"cluster": "elsewhere", "type": "gpu", "model": "NVIDIA L4", "capacity": 3, "used": 0})
    try:
        pane = open_bookings(page)
        expect(pane.locator('.bkg-row[data-pool="elsewhere|gpu|NVIDIA L4"]')).to_have_count(1, timeout=8000)
        pane.locator('[data-bkg="cluster"]').select_option("harv-fake")
        expect(pane.locator('.bkg-row[data-pool="elsewhere|gpu|NVIDIA L4"]')).to_have_count(0)
        expect(pane.locator(".bkg-row[data-pool]")).to_have_count(3)
    finally:
        GPUS_ALL["pools"].pop()


DANGER_METRICS = """
(el) => {
  const rgb = (c) => (c.match(/\\d+(\\.\\d+)?/g) || []).slice(0, 3).map(Number);
  const lum = ([r, g, b]) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const cs = getComputedStyle(el);
  const probe = document.createElement('span');
  probe.style.color = 'var(--danger)';
  document.body.appendChild(probe);
  const danger = getComputedStyle(probe).color;
  probe.remove();
  const a = lum(rgb(cs.color)), b = lum(rgb(cs.backgroundColor));
  return { text: (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05), bg: rgb(cs.backgroundColor).join(','),
           danger: rgb(danger).join(','), opacity: cs.opacity };
}
"""


def test_danger_button_is_solid_and_readable_in_every_theme(ui):
    """v1.94.1 : « Yes, cancel it » et « Delete permanently » semblaient
    grisés (fond --danger-bg pâle ou sombre). Plein, lisible (AA) dans les
    cinq thèmes et les deux modes ; grisé seulement pendant l'envoi."""
    page, _, _ = ui
    pane = open_bookings(page)
    pane.locator('[data-bkg-view="mine"]').click()
    row = pane.locator('tr[data-booking="bk-aaaaaaaa0001"]')
    row.locator('[data-bkg="cancel"]').click()
    btn = row.locator('[data-bkg="cancel-yes"]')
    expect(btn).to_be_enabled()
    page.add_style_tag(content=".btn { transition: none !important; }")
    for theme in ("suse", "nord", "solarized", "catppuccin", "tokyo"):
        for mode in ("dark", "light"):
            page.evaluate(f"document.documentElement.setAttribute('data-theme','{theme}');"
                          f"document.documentElement.setAttribute('data-mode','{mode}')")
            m = btn.evaluate(DANGER_METRICS)
            assert m["text"] >= 4.5, f"{theme}/{mode}: text {m}"
            assert m["bg"] == m["danger"], f"{theme}/{mode}: fond {m}"      # le rouge du thème, plein
            assert m["opacity"] == "1", f"{theme}/{mode}: {m}"
    # pendant l'envoi seulement : grisé
    btn.evaluate("b => { b.disabled = true; }")
    assert float(btn.evaluate("b => getComputedStyle(b).opacity")) < 0.6
