"""v1.46.0 : la progression d'un transfert, dans un navigateur.

Demandé par l'exploitant : voir pendant un transfert le débit, le temps
restant, la quantité à transférer et celle déjà transférée ; et des options
de vitesse qui disent ce qu'elles coûtent. Le réseau est intercepté : le flux
de l'action est simulé.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

VM = "/api/vm/harv-fake/default/vm1"
GIB = 1024 ** 3
MIB = 1024 ** 2

CHECK = {"engine": "file", "reason": "different-targets", "blocked": False,
         "findings": [{"code": "engine", "level": "ok", "facts": {}}],
         "amount": {"disks": 1, "size": 10 * GIB, "used": 3 * GIB},
         "mappings": {"networks": {}, "storage_classes": {}},
         "target": {"cluster": "other", "networks": [], "storage_classes": []}}

RUNNING = {"phase": "import", "item": "disk-0", "done": int(3.2 * GIB), "total": 10 * GIB,
           "wire": 512 * MIB, "rate": 85 * MIB, "eta": 90, "elapsed": 40,
           "items_done": 0, "items_total": 1, "final": False}
FREEZE = {"phase": "freeze", "item": None, "done": 10 * GIB, "total": 10 * GIB, "wire": 0,
          "rate": 200 * MIB, "eta": 0, "elapsed": 51, "items_done": 1, "items_total": 1,
          "final": True}


def fulfill(route, body, status=200, ctype="application/json"):
    route.fulfill(status=status, content_type=ctype,
                  body=body if isinstance(body, str) else json.dumps(body))


def sse(events):
    return "".join(f"event: {t}\ndata: {json.dumps(d)}\n\n" for t, d in events)


@pytest.fixture
def page_fr(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','fr');")
    page = context.new_page()
    sent = []

    def start(route, request):
        sent.append(json.loads(request.post_data or "{}"))
        fulfill(route, {"action_id": "run000000001"}, 201)

    page.route("**/api/clusters", lambda r, q: fulfill(r, {"clusters": [
        {"name": "harv-fake"}, {"name": "other"}]}))
    page.route(f"**{VM}/migrate-info", lambda r, q: fulfill(r, {"phase": "Running", "nodes": [], "migrations": []}))
    page.route(f"**{VM}/transfer/check", lambda r, q: fulfill(r, CHECK))
    page.route(f"**{VM}/transfer", start)
    page.route("**/api/stream/run000000001", lambda r, q: fulfill(r, sse([
        ("step", {"type": "step", "step_id": "import", "status": "running",
                  "message": "disk-0: importing into harv-rep1"}),
        ("progress", FREEZE),
        ("progress", RUNNING),
    ]), ctype="text/event-stream"))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.VMMigrate && window.VMTransfer && window.XferProgress")
    page.evaluate("VMMigrate.open('harv-fake', 'default', 'vm1', 'cluster')")
    pane = page.locator('#fp-vm-migrate-harv-fake-default-vm1 [data-pane="cluster"]')
    return page, pane, sent


def test_the_amount_is_announced_before_the_start(page_fr):
    page, pane, _ = page_fr
    expect(pane.locator(".xfer-amount")).to_have_text(
        "À transférer : 1 disque(s), 10,0 Gio (3,0 Gio occupés)", timeout=5000)


def test_speed_options_say_what_they_cost_and_are_sent(page_fr):
    page, pane, sent = page_fr
    expect(pane.locator('[data-x="report"] .xfer-amount')).to_be_visible(timeout=5000)
    speed = pane.locator('[data-x="speed"]')
    expect(speed.locator("option")).to_have_count(3)
    assert "de 2 à 8" in speed.get_attribute("data-tip")
    speed.select_option("max")
    pane.locator('[data-x="bandwidth"]').fill("200")
    pane.locator('[data-x="bandwidth"]').dispatch_event("change")
    expect(pane.locator('[data-x="start"]')).to_be_enabled(timeout=5000)
    # cliquer « Lancer » juste après avoir tapé le plafond : le champ perd le
    # focus, le contrôle se relance, et le clic ne doit pas être perdu
    pane.locator('[data-x="bandwidth"]').fill("250")
    pane.locator('[data-x="start"]').click()
    expect(pane.locator('[data-x="feedback"]')).to_contain_text("run000000001", timeout=5000)
    assert sent[-1]["speed"] == "max" and sent[-1]["bandwidth"] == "250"


def test_the_live_block_shows_amounts_rate_and_time_left(page_fr):
    page, pane, _ = page_fr
    expect(pane.locator('[data-x="start"]')).to_be_enabled(timeout=5000)
    pane.locator('[data-x="start"]').click()
    live = pane.locator('[data-x="live"]')
    expect(live).to_be_visible(timeout=5000)
    # la fenêtre défile vers le suivi au lancement
    expect(live).to_be_in_viewport()
    line = live.locator('[data-x="live-line"]')
    expect(line).to_contain_text("Import disk-0 : 3,2 / 10,0 Gio", timeout=5000)
    expect(line).to_contain_text("512,0 Mio transmis")
    expect(line).to_contain_text("85,0 Mio/s")
    expect(line).to_contain_text("reste 1 min 30 s")
    expect(live.locator('[data-x="live-meta"]')).to_contain_text("écoulé 40 s")
    # pas de message d'étape du moteur (en anglais) dans la ligne d'état
    assert "importing" not in live.locator('[data-x="live-meta"]').inner_text()
    # la phase finie garde son bilan
    expect(live.locator('[data-x="live-done"] li')).to_contain_text("Gel des disques : 10,0 / 10,0 Gio")
    bar = live.locator('[data-x="live-bar"]').get_attribute("style")
    assert "width: 32%" in bar


def test_the_dock_shows_the_transfer_progress(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_dock_visible','true');"
                            "localStorage.setItem('harvester_ops_dock_collapsed','false');")
    page = context.new_page()
    run = {"id": "run000000002", "action": "vm-transfer:default/vm1", "cluster": "harv-fake",
           "status": "running", "exit_code": None, "started_at": 1_790_000_000,
           "ended_at": None, "dry_run": False, "error_summary": None, "cluster_user": None,
           "progress": {"import": RUNNING}, "progress_current": RUNNING}
    page.route("**/api/activity*", lambda r, q: fulfill(r, {"in_progress": [run], "recent": [],
                                                            "history": [], "total": 1}))
    page.route("**/api/stream/run000000002", lambda r, q: fulfill(r, sse([("progress", RUNNING)]),
                                                                 ctype="text/event-stream"))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    card = page.locator("#dock-card-run000000002")
    expect(card).to_be_visible(timeout=10000)
    expect(card.locator(".xfer-dock-line")).to_contain_text(
        "Import disk-0: 3.2 / 10.0 GiB", timeout=10000)
    expect(card.locator(".xfer-dock-line")).to_contain_text("1 min 30 s left")
    # la barre suit la phase (32 %), y compris après le rafraîchissement de la carte
    page.wait_for_timeout(3500)
    assert "width: 32%" in card.locator(".progress-mini .fill").get_attribute("style")
