"""v1.42.0 : un volume dégradé, dans la vue Stockage d'un vrai navigateur.

Ce que ces tests tiennent : le bandeau n'apparaît que s'il y a quelque
chose et mène au volume le plus urgent ; chaque constat dit ce qu'on
observe, quoi faire, et propose la correction avec sa commande équivalente ;
une correction ne part qu'après confirmation et n'envoie que son genre (le
serveur calcule le reste) ; un refus du serveur est affiché ; rien n'est
proposé sur un volume en panne.

Le réseau est intercepté : aucun test ne touche à un cluster.
"""

import json

import pytest
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent))
from boards import goto_board  # noqa: E402

playwright = pytest.importorskip("playwright")

GIB = 1024 ** 3


def vol(claim, health, findings, sc="three", state="attached", wanted=3):
    return {"pvc_namespace": "default", "pvc_name": claim, "claim_missing": False,
            "storage_class": sc, "phase": "Bound", "requested": 10 * GIB,
            "longhorn": "pvc-" + claim, "size": 10 * GIB, "actual_size": GIB,
            "state": state, "robustness": "degraded", "attached_to": "n1.lo",
            "replicas_wanted": wanted,
            "replicas": [{"node": "n1.lo", "disk": "d1", "running": True}],
            "image": None, "image_iso": False, "vm": None, "disk": None,
            "device": None, "boot_order": None, "pods": [{"name": "db-0"}],
            "last_pods": [], "orphan": False, "health": health, "findings": findings}


NOT_ENOUGH = {"cause": "not-enough-nodes", "severity": "action",
              "facts": {"wanted": 3, "nodes": 1},
              "fix": {"kind": "set-replicas", "params": {"replicas": 1}}}
REBUILDING = {"cause": "rebuilding", "severity": "info",
              "facts": {"replicas": [{"replica": "r2", "node": "n2.lo", "progress": 42}]},
              "fix": None}
FAULTED = {"cause": "faulted", "severity": "critical",
           "facts": {"replicas": 1, "failed": 1}, "fix": None}


def storage(volumes, summary):
    return {"cluster": "harv-fake", "over_provisioning_pct": 200.0,
            "minimal_available_pct": 25.0, "schedulable_nodes": 1,
            "classes": [{"name": "three", "provisioner": "driver.longhorn.io",
                         "replicas": 3, "default": False, "allocatable": 0,
                         "reason": "not enough schedulable nodes"},
                        {"name": "harv-rep1", "provisioner": "driver.longhorn.io",
                         "replicas": 1, "default": True, "allocatable": 500 * GIB}],
            "disks": [{"node": "n1.lo", "disk": "d1", "path": "/var/lib/harvester/defaultdisk",
                       "schedulable": True, "maximum": 1000 * GIB, "available": 800 * GIB,
                       "used": 200 * GIB, "scheduled": 300 * GIB, "reserved": 0,
                       "room": 500 * GIB, "limited_by": "free-space", "replicas": 3}],
            "volumes": volumes, "vms": [], "health_summary": summary}


DEGRADED = storage(
    [vol("db", "degraded", [NOT_ENOUGH]),
     vol("mirror", "degraded", [REBUILDING], sc="harv-rep1", wanted=2),
     dict(vol("ok", "healthy", [], sc="harv-rep1", wanted=1), robustness="healthy")],
    {"degraded": 2, "faulted": 0, "at_risk": 0, "top_cause": "not-enough-nodes"})


def open_storage(page, base_url, data, fix_status=201, fixes=None, lang="en"):
    def fix(route, request):
        if fixes is not None:
            fixes.append(json.loads(request.post_data or "{}"))
        body = ({"action_id": "a1", "summary": "3 -> 1 replica(s)", "params": {"replicas": 1}}
                if fix_status == 201 else
                {"error": "not-applicable", "detail": "this fix no longer applies"})
        route.fulfill(status=fix_status, content_type="application/json", body=json.dumps(body))
    page.route("**/api/storage-map/**", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(data)))
    page.route("**/api/volume-health/**", fix)
    page.context.add_init_script(
        f"localStorage.setItem('harvester_ops_language','{lang}');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','overview');")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    goto_board(page, "storage")
    page.wait_for_selector('[data-board="storage"] .vsw', timeout=10000)
    page.wait_for_timeout(600)
    return page.locator('[data-board="storage"]')


def test_the_banner_says_how_many_and_why(context, flask_server):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    view = open_storage(page, flask_server["base_url"], DEGRADED)
    banner = view.locator('.sto-health-banner')
    assert banner.count() == 1
    text = banner.inner_text()
    assert "2 degraded" in text and "Not enough nodes for 3 replicas" in text
    assert not errors, errors


def test_no_banner_when_everything_is_fine(context, flask_server):
    page = context.new_page()
    fine = storage([dict(vol("ok", "healthy", [], sc="harv-rep1", wanted=1),
                         robustness="healthy")],
                   {"degraded": 0, "faulted": 0, "at_risk": 0, "top_cause": None})
    view = open_storage(page, flask_server["base_url"], fine)
    assert view.locator('.sto-health-banner').count() == 0


def test_the_banner_opens_the_most_urgent_volume(context, flask_server):
    page = context.new_page()
    view = open_storage(page, flask_server["base_url"], DEGRADED)
    view.locator('.sto-health-banner').click()
    page.wait_for_timeout(300)
    assert view.locator('.fabric-detail h3').inner_text() == "db"


def test_a_degraded_row_is_marked(context, flask_server):
    page = context.new_page()
    view = open_storage(page, flask_server["base_url"], DEGRADED)
    row = view.locator('[data-vol="default/db"]')
    assert "health-degraded" in row.get_attribute("class")
    assert row.locator('.sto-health-tag').inner_text() == "degraded"
    assert view.locator('[data-vol="default/ok"] .sto-health-tag').count() == 0


def test_the_health_box_explains_and_offers_the_fix(context, flask_server):
    page = context.new_page()
    view = open_storage(page, flask_server["base_url"], DEGRADED)
    view.locator('[data-vol="default/db"]').click()
    page.wait_for_timeout(300)
    box = view.locator('.sto-health')
    text = box.inner_text()
    assert "Not enough nodes for 3 replicas" in text
    assert "3 replicas wanted, 1 schedulable node(s)" in text
    assert "add nodes" in text
    btn = box.locator('[data-vol-fix="set-replicas"]')
    assert btn.inner_text() == "Set 1 replica(s)" and btn.get_attribute("data-tip")
    box.locator('.sto-kubectl summary').click()
    code = box.locator('.sto-kubectl code').inner_text()
    assert code == ("kubectl -n longhorn-system patch volumes.longhorn.io pvc-db "
                    "--type merge -p '{\"spec\":{\"numberOfReplicas\":1}}'")
    assert box.locator('.sto-kubectl [data-copy]').count() == 1


def test_a_fix_goes_only_after_confirmation_and_sends_only_its_kind(context, flask_server):
    page = context.new_page()
    fixes = []
    view = open_storage(page, flask_server["base_url"], DEGRADED, fixes=fixes)
    view.locator('[data-vol="default/db"]').click()
    page.wait_for_timeout(300)
    msgs = []
    page.once("dialog", lambda d: (msgs.append(d.message), d.dismiss()))
    view.locator('[data-vol-fix="set-replicas"]').click()
    page.wait_for_timeout(300)
    assert fixes == [] and "loses redundancy" in msgs[0]
    page.once("dialog", lambda d: d.accept())
    view.locator('[data-vol-fix="set-replicas"]').click()
    page.wait_for_timeout(500)
    assert fixes == [{"kind": "set-replicas"}]
    assert "3 -> 1 replica(s)" in view.locator('.sto-fix-out').inner_text()


def test_a_refusal_from_the_server_is_shown(context, flask_server):
    page = context.new_page()
    view = open_storage(page, flask_server["base_url"], DEGRADED, fix_status=409)
    view.locator('[data-vol="default/db"]').click()
    page.wait_for_timeout(300)
    page.once("dialog", lambda d: d.accept())
    view.locator('[data-vol-fix="set-replicas"]').click()
    page.wait_for_timeout(500)
    assert "no longer applies" in view.locator('.sto-fix-out').inner_text()


def test_a_rebuild_shows_its_progress_and_offers_nothing(context, flask_server):
    page = context.new_page()
    view = open_storage(page, flask_server["base_url"], DEGRADED)
    view.locator('[data-vol="default/mirror"]').click()
    page.wait_for_timeout(300)
    box = view.locator('.sto-health')
    assert "Rebuilding in progress" in box.inner_text()
    assert "42 %" in box.locator('.sto-rebuild').inner_text()
    assert box.locator('.sto-rebuild .sto-bar-used').evaluate("e => e.style.width") == "42%"
    assert box.locator('[data-vol-fix]').count() == 0


def test_a_faulted_volume_is_red_and_offers_no_fix(context, flask_server):
    page = context.new_page()
    data = storage([vol("lost", "faulted", [FAULTED])],
                   {"degraded": 0, "faulted": 1, "at_risk": 0, "top_cause": "faulted"})
    view = open_storage(page, flask_server["base_url"], data)
    assert "critical" in view.locator('.sto-health-banner').get_attribute("class")
    view.locator('[data-vol="default/lost"]').click()
    page.wait_for_timeout(300)
    box = view.locator('.sto-health')
    assert "Do not delete anything" in box.inner_text()
    assert box.locator('[data-vol-fix]').count() == 0


def test_health_controls_have_tooltips_and_french_renders(context, flask_server):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    view = open_storage(page, flask_server["base_url"], DEGRADED)
    view.locator('[data-vol="default/db"]').click()
    page.wait_for_timeout(300)
    missing = page.evaluate("""() => [...document.querySelectorAll(
        '[data-board="storage"] button, [data-board="storage"] [role=button]')]
        .filter(el => !el.getAttribute('data-tip')).map(el => el.className)""")
    assert not missing, missing
    page2 = context.new_page()
    view2 = open_storage(page2, flask_server["base_url"], DEGRADED, lang="fr")
    view2.locator('[data-vol="default/db"]').click()
    page2.wait_for_timeout(300)
    assert "Pas assez de nœuds pour 3 répliques" in view2.locator('.sto-health').inner_text()
    assert not errors, errors
