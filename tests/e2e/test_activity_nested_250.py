"""2.5.0 (décision 174) : actions imbriquées. L'Activité montre le premier
niveau, une action à filles se déplie (filles lues par `?parent=`), trois
niveaux possibles ; le dock montre le parent et sa fille en cours ; une
recherche liste à plat avec le lien « fait partie de ». Noms hostiles en
texte."""

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402

EVIL = "<img src=x onerror=window.__xss=1>"
NOW = time.time()


def fulfill(route, body, status=200, ctype="application/json"):
    route.fulfill(status=status, content_type=ctype,
                  body=body if isinstance(body, str) else json.dumps(body))


def act(rid, action, status="done", parent=None, children=0, started=0, current=None):
    d = {"id": rid, "action": action, "cluster": "harv1", "status": status,
         "exit_code": None if status == "running" else 0, "started_at": NOW - 100 + started,
         "ended_at": None if status == "running" else NOW - 50 + started, "dry_run": False,
         "error_summary": None, "cluster_user": None, "progress": {}, "progress_current": None,
         "result": {}, "parent": parent, "children": []}
    if children is not None:
        d["children_count"] = children
    if current:
        d["child_current"] = current
    return d


DEPLOY = act("aaaaaaaaaaa1", "deploy:plan1", "running", children=2,
             current={"id": "bbbbbbbbbbb2", "action": f"capi-cluster-create:{EVIL}", "status": "running"})
DONE_PARENT = act("aaaaaaaaaaa2", "deploy:plan0", children=2, started=-30)
LEVEL2 = {
    "aaaaaaaaaaa1": [act("bbbbbbbbbbb1", "baremetal-batch:harv9", parent="aaaaaaaaaaa1", children=1, started=1),
                     act("bbbbbbbbbbb2", f"capi-cluster-create:{EVIL}", "running",
                         parent="aaaaaaaaaaa1", started=2)],
    "aaaaaaaaaaa2": [act("ccccccccccc1", "baremetal-batch:harv8", parent="aaaaaaaaaaa2", children=1, started=-29),
                     act("ccccccccccc2", f"tf-apply-decl:{EVIL}:1", parent="aaaaaaaaaaa2", started=-28)],
    "ccccccccccc1": [act("ddddddddddd1", "baremetal-install:n1", parent="ccccccccccc1", started=-27)],
    "bbbbbbbbbbb1": [act("ddddddddddd2", "baremetal-install:n2", parent="bbbbbbbbbbb1", started=1)],
}


def activity(route, req):
    q = parse_qs(urlparse(req.url).query)
    parent = (q.get("parent") or [""])[0]
    if parent:
        kids = LEVEL2.get(parent, [])
        return fulfill(route, {"in_progress": [k for k in kids if k["status"] == "running"],
                               "actions_done": [k for k in kids if k["status"] != "running"],
                               "log_files": [], "total": 9, "matched": len(kids),
                               "filters": {"active": False, "parent": parent},
                               "facets": {"clusters": ["harv1"], "statuses": [], "actions": []}})
    if q.get("q"):
        flat = [dict(LEVEL2["aaaaaaaaaaa2"][1], children_count=None)]
        return fulfill(route, {"in_progress": [], "actions_done": flat, "log_files": [], "total": 9,
                               "matched": 1, "filters": {"active": True, "q": q["q"][0]},
                               "facets": {"clusters": ["harv1"], "statuses": [], "actions": []}})
    return fulfill(route, {"in_progress": [DEPLOY], "actions_done": [DONE_PARENT], "log_files": [],
                           "total": 9, "matched": 2, "filters": {"active": False},
                           "facets": {"clusters": ["harv1"], "statuses": [], "actions": []}})


@pytest.fixture
def page_nested(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_dock_visible','true');"
                            "localStorage.setItem('harvester_ops_dock_collapsed','false');"
                            "localStorage.removeItem('harvester_ops_activity_filters');"
                            "localStorage.setItem('harvester_ops_sidebar_pinned','1');")
    page = context.new_page()
    page.route("**/api/activity*", activity)
    page.route("**/api/stream/*", lambda r, q: fulfill(r, "", ctype="text/event-stream"))
    goto(page, flask_server["base_url"], "#gpus")
    page.wait_for_function("window.App && window.i18n")
    return page


def open_activity(page):
    page.click('#host-nav [data-hnav-root="activity"]', timeout=5000)
    expect(page.locator("#activity-history tbody tr[data-act-id='aaaaaaaaaaa2']")).to_be_visible(timeout=10000)


def test_history_unfolds_three_levels(page_nested):
    page = page_nested
    open_activity(page)
    body = page.locator("#activity-history tbody")
    expect(body.locator("tr")).to_have_count(1)
    btn = body.locator("[data-act-expand='aaaaaaaaaaa2']")
    expect(btn).to_have_attribute("title", "Show the 2 child actions launched by this action")
    btn.click()
    expect(body.locator("tr")).to_have_count(3)
    expect(body.locator("tr[data-act-id='ccccccccccc1']")).to_have_attribute("data-depth", "1")
    # nom de fille hostile rendu en texte
    expect(body.locator("tr[data-act-id='ccccccccccc2']")).to_contain_text(EVIL)
    body.locator("[data-act-expand='ccccccccccc1']").click()
    third = body.locator("tr[data-act-id='ddddddddddd1']")
    expect(third).to_have_attribute("data-depth", "2")
    # ordre : parent, fille 1, petite-fille, fille 2
    ids = body.locator("tr").evaluate_all("rs => rs.map(r => r.dataset.actId)")
    assert ids == ["aaaaaaaaaaa2", "ccccccccccc1", "ddddddddddd1", "ccccccccccc2"]
    # replier le parent retire toute sa descendance
    btn.click()
    expect(body.locator("tr")).to_have_count(1)
    assert page.locator("#tab-activity img").count() == 0
    assert page.evaluate("window.__xss") is None


def test_running_parent_unfolds_in_the_running_table(page_nested):
    page = page_nested
    open_activity(page)
    run = page.locator("#activity-running tbody")
    run.locator("[data-act-expand='aaaaaaaaaaa1']").click()
    expect(run.locator("tr")).to_have_count(3)
    expect(run.locator("tr[data-act-id='bbbbbbbbbbb2'] [data-cancel]")).to_be_visible()
    expect(run.locator("tr[data-act-id='bbbbbbbbbbb1'] [data-cancel]")).to_have_count(0)


def test_dock_shows_the_parent_and_its_current_child(page_nested):
    page = page_nested
    card = page.locator("#dock-card-aaaaaaaaaaa1")
    expect(card).to_be_visible(timeout=10000)
    child = card.locator(".dock-child")
    expect(child).to_contain_text(f"capi-cluster-create:{EVIL}")
    expect(child).to_contain_text("2 child actions")
    assert child.get_attribute("title")
    # les filles n'ont pas de carte à elles
    expect(page.locator("#dock-card-bbbbbbbbbbb2")).to_have_count(0)
    assert page.locator("#bottom-dock img").count() == 0
    assert page.evaluate("window.__xss") is None


def test_search_lists_flat_with_the_parent_link(page_nested):
    page = page_nested
    open_activity(page)
    page.fill("#act-f-q", "tf-apply")
    row = page.locator("#activity-history tbody tr[data-act-id='ccccccccccc2']")
    expect(row).to_be_visible(timeout=5000)
    link = row.locator(".activity-part-of")
    expect(link).to_have_text("part of aaaaaaaaaaa2")
    assert link.get_attribute("href") == "#activity/action/aaaaaaaaaaa2"
    expect(row.locator("[data-act-expand]")).to_have_count(0)
