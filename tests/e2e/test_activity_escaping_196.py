"""v1.96.0 : une étiquette d'action porte un nom libvirt brut
(`kvm:vm-start:<hôte>/<VM>`), donc choisi par qui a nommé la VM. L'onglet
Activité (en cours, historique, fenêtre de détail) et le dock l'affichent tel
quel, en texte : aucun script ne doit s'exécuter."""

import json
import time

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402

EVIL = "<img src=x onerror=window.__xss=1>"
LABEL = f"kvm:vm-start:node2/{EVIL}"


def fulfill(route, body, status=200, ctype="application/json"):
    route.fulfill(status=status, content_type=ctype,
                  body=body if isinstance(body, str) else json.dumps(body))


def sse(events):
    return "".join(f"event: {k}\ndata: {json.dumps(v)}\n\n" for k, v in events)


def run(rid, status, ended=None, error=None):
    return {"id": rid, "action": LABEL, "cluster": f"kvm:{EVIL}", "status": status,
            "exit_code": None if status == "running" else 1, "started_at": time.time() - 5,
            "ended_at": ended, "dry_run": False, "error_summary": error, "cluster_user": None,
            "progress": {}, "progress_current": None, "result": {}}


@pytest.fixture
def page_xss(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_dock_visible','true');"
                            "localStorage.setItem('harvester_ops_dock_collapsed','false');"
                            "localStorage.setItem('harvester_ops_sidebar_pinned','1');")
    page = context.new_page()
    live = run("kvmxss000001", "running")
    done = run("kvmxss000002", "error", ended=time.time() - 1, error=f"cannot start {EVIL}")
    page.route("**/api/activity*", lambda r, q: fulfill(r, {
        "in_progress": [live], "actions_done": [done], "log_files": [], "total": 2,
        "facets": {"clusters": [f"kvm:{EVIL}"], "statuses": ["running", "error"], "actions": [LABEL]}}))
    page.route("**/api/stream/*", lambda r, q: fulfill(r, sse([
        ("step", {"step_id": "action", "status": "running", "message": f"virsh start {EVIL}", "ts": 1}),
    ]), ctype="text/event-stream"))
    goto(page, flask_server["base_url"], "#gpus")     # 2.0.0 : écran ouvert par son adresse
    page.wait_for_function("window.App && window.i18n")
    return page


def test_the_dock_shows_a_hostile_label_as_text(page_xss):
    page = page_xss
    card = page.locator("#dock-card-kvmxss000001")
    expect(card).to_be_visible(timeout=10000)
    expect(card.locator(".name")).to_contain_text(EVIL)
    # le message d'étape (nom de VM compris) arrive par le flux
    expect(card.locator(".step-msg")).to_contain_text(f"virsh start {EVIL}", timeout=10000)
    # la carte finie dit l'erreur, en texte elle aussi
    expect(page.locator("#dock-card-kvmxss000002 .step-msg")).to_contain_text(EVIL)
    page.wait_for_timeout(3500)                  # un rafraîchissement complet de la carte
    expect(card.locator(".step-msg")).to_contain_text(EVIL)
    assert page.locator("#bottom-dock img").count() == 0
    assert page.evaluate("window.__xss") is None


def test_the_activity_tab_shows_a_hostile_label_as_text(page_xss):
    page = page_xss
    # 2.0.0 : l'entrée Activité du menu des hôtes
    page.click('#host-nav [data-hnav-root="activity"]', timeout=5000)
    running = page.locator("#activity-running tbody tr").first
    expect(running).to_contain_text(EVIL, timeout=10000)
    hist = page.locator("#activity-history tbody tr").first
    expect(hist).to_contain_text(LABEL)
    expect(hist).to_contain_text(f"kvm:{EVIL}")
    hist.click()
    panel = page.locator("#fp-activity-detail")
    expect(panel).to_be_visible(timeout=5000)
    expect(panel.locator(".floating-panel-title")).to_contain_text(LABEL)
    expect(panel.locator(".activity-detail-meta")).to_contain_text(LABEL)
    expect(panel.locator("#activity-detail-log")).to_contain_text(f"virsh start {EVIL}", timeout=5000)
    page.wait_for_timeout(300)
    assert page.locator("#tab-activity img, #fp-activity-detail img").count() == 0
    assert page.evaluate("window.__xss") is None
