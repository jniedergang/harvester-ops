"""Écrans de la réplication asynchrone (Disaster Recovery), API simulée.

Place dans le menu (groupe racine et sous-onglet d'un cluster), liste des
contrats en cartes OU en tableau (bascule gardée), jauge RPO, sens et
joignabilité, transfert ; fenêtre de création (choix par liste ou par règle,
mesure sautable avec son avertissement, réseau absent bloquant avec « Créer
sur la cible » et VLAN modifiable, classe disque par disque, vagues par
glisser-déposer, actions annexes à secret masqué, contrôles avant Créer) ;
vue d'un contrat (quatre onglets, graphiques 24 h puis 7 j, journal,
réglages avec Annuler) ; fenêtre de bascule (étapes, essai à blanc d'office,
nom tapé, réservée aux administrateurs) ; sous-onglet d'un cluster et son
filtre ; liens (passif, test actif) ; notifications ; pastilles d'alerte et
onglet Événements."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)
PID, PID2 = "rp-0123456789ab", "rp-ba9876543210"
ACTION = "rpact0000001"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def act(name, role, allowed=True, count=1, reason=None):
    a = {"name": name, "role": role, "allowed": allowed, "count": count}
    if reason:
        a["reason"] = reason
    return a


def resource(uid, name, state, rpo, side="any", plan=PID, plan_name="erp", peer=None, reasons=()):
    return {"plan": plan, "plan_name": plan_name, "resource_kind": "vm", "resource_uid": uid, "namespace": "default",
            "name": name, "side": side, "host_kind": "harvester", "peer_host": peer, "state": state,
            "reasons": list(reasons), "state_since": iso(NOW - timedelta(hours=2)), "rpo_held_s": rpo,
            "rpo_objective_s": 900, "last_applied_at": iso(NOW - timedelta(seconds=rpo or 0)),
            "last_events": [], "actions": []}


def plans_payload(role):
    adm, op = role == "admin", role in ("operator", "admin")
    erp = {"id": PID, "name": "erp", "source_host": "harv-fake", "target_host": "harv-dr",
           "source_host_kind": "harvester", "target_host_kind": "harvester", "state": "protected", "generation": 1,
           "active_side": "source", "rpo_objective_s": 900, "rpo_held_s": 1100, "interval_s": 300,
           "next_tick_at": iso(NOW + timedelta(seconds=120)),
           "resources_by_state": {"protected": 1, "lagging": 1},
           "last_events": [{"id": 9, "ts": iso(NOW - timedelta(minutes=3)), "level": "warn", "code": "rpo-exceeded",
                            "operation": "follow", "step": "status", "message": "RPO held 1100 s, objective 900 s"}],
           "actions": [act("protect", "operator", op), act("failover", "admin", adm), act("failover-unplanned", "admin", adm),
                       act("pause", "operator", op), act("unprotect", "operator", op),
                       act("unprotect-delete-backups", "admin", adm)],
           "resources": [resource("u-web", "web-1", "protected", 400), resource("u-db", "db-1", "lagging", 1100,
                                                                                 reasons=("rpo-exceeded",))],
           "transfer": {"last_at": iso(NOW - timedelta(minutes=2)), "last_bytes": 25 * 1024 * 1024,
                        "upload_bps": 4 * 1024 * 1024, "apply_bps": 9 * 1024 * 1024, "bytes_24h": 3 * 1024 ** 3,
                        "passes_24h": 280},
           "pending_vms": ["default/web-2"], "alerts": 1, "reach": {"source": True, "target": False, "at": iso(NOW)}}
    crm = {"id": PID2, "name": "crm", "source_host": "harv-dr", "target_host": "harv-fake",
           "source_host_kind": "harvester", "target_host_kind": "harvester", "state": "draft", "generation": 1,
           "active_side": "source", "rpo_objective_s": 3600, "rpo_held_s": None, "interval_s": None,
           "next_tick_at": None, "resources_by_state": {"draft": 1}, "last_events": [],
           "actions": [act("measure-start", "operator", op), act("protect", "operator", op), act("delete", "operator", op)],
           "resources": [resource("u-crm", "crm-1", "draft", None, plan=PID2, plan_name="crm")],
           "transfer": {"last_at": None, "last_bytes": 0, "upload_bps": None, "apply_bps": None, "bytes_24h": 0,
                        "passes_24h": 0},
           "pending_vms": [], "alerts": 0, "reach": {"source": None, "target": None, "at": None}}
    return {"plans": [erp, crm], "side": "any", "role": role,
            "follower": {"enabled": True, "state": "running", "running": True}}


ALERTS = {"count": 2, "alerts": [
    {"plan": PID, "plan_name": "erp", "resource_uid": "u-db", "namespace": "default", "name": "db-1",
     "source_host": "harv-fake", "target_host": "harv-dr", "code": "rpo-exceeded", "level": "warn",
     "since": iso(NOW - timedelta(minutes=12)), "message": "RPO held 1100 s, objective 900 s"},
    {"plan": PID, "plan_name": "erp", "resource_uid": "", "namespace": "", "name": "",
     "source_host": "harv-fake", "target_host": "harv-dr", "code": "selector-new-vm", "level": "warn",
     "since": iso(NOW - timedelta(minutes=1)), "message": "default/web-2 carries the label"}]}


def plan_detail(role):
    row = plans_payload(role)["plans"][0]
    plan = {"id": PID, "name": "erp", "source_cluster": "harv-fake", "target_cluster": "harv-dr", "state": "protected",
            "generation": 1, "rpo_objective_s": 900, "interval_s": 300, "retain": 6, "created_by": "alice",
            "selector": "", "selector_namespace": "", "vms": ["default/web-1", "default/db-1"],
            "mappings": {}, "options": {"cloud_init": "keep", "mac": "keep", "start": "started", "follow": "console",
                                        "paused": False, "skew_tolerance_s": 10,
                                        "waves": [{"name": "db", "vms": ["default/db-1"], "gate": {"kind": "agent", "seconds": 300}},
                                                  {"vms": ["default/web-1"], "gate": {"kind": "running"}}],
                                        "hooks": [{"name": "dns", "at": "end", "method": "PUT", "url": "https://dns.example/erp",
                                                   "headers": {}, "secret_headers": {"Authorization": "********"},
                                                   "body": "", "timeout_s": 15}]}}
    up = {"at": iso(NOW), "bytes": 1024 ** 2 * 12, "seconds": 3, "bps": 4 * 1024 ** 2}
    status = {"plan_id": PID, "state": "protected", "rpo_objective_s": 900, "rpo_held_s": 1100, "exceeded": True,
              "backlog": 1, "drift": False, "skew_s": 4, "interval_s": 300, "next_tick_at": iso(NOW + timedelta(seconds=120)),
              "resources_by_state": {"protected": 1, "lagging": 1},
              "vms": [{"uid": "u-web", "namespace": "default", "name": "web-1", "state": "protected", "reasons": [],
                       "rpo_held_s": 400, "last_applied_at": iso(NOW - timedelta(seconds=400)), "backlog": 0, "skew_s": 2,
                       "throughput": {"upload": up, "apply": dict(up, seconds=5)}, "disks": [{}, {}]},
                      {"uid": "u-db", "namespace": "default", "name": "db-1", "state": "lagging", "reasons": ["rpo-exceeded"],
                       "rpo_held_s": 1100, "last_applied_at": iso(NOW - timedelta(seconds=1100)), "backlog": 1, "skew_s": 4,
                       "throughput": {"upload": None, "apply": None}, "disks": [{}]}]}
    return {"plan": plan, "row": row, "status": status, "volumes": [], "role": role, "lock": None, "lease": None,
            "runs": [{"operation": "failover", "started_at": iso(NOW - timedelta(days=3)), "finished_at": iso(NOW - timedelta(days=3)),
                      "result": "rolled-back", "vms": ["default/web-1"], "by": "patronne"}]}


def series(rng):
    n = {"1h": 6, "24h": 30, "7d": 40, "30d": 40}[rng]
    step = {"1h": 60, "24h": 600, "7d": 3600, "30d": 21600}[rng]
    start = NOW - timedelta(seconds=step * n)
    return {"plan": PID, "range": rng, "bucket_s": step, "rpo_objective_s": 900,
            "points": [{"t": iso(start + timedelta(seconds=step * i)), "rpo_held_s": 300 + (i % 7) * 120,
                        "upload_bps": 4e6 + i * 1e4, "apply_bps": 8e6, "bytes": 20 * 1024 ** 2, "passes": 2, "backlog": 0}
                       for i in range(n)]}


CANDIDATES = {"cluster": "harv-fake", "vms": [
    {"namespace": "default", "name": "ui-cirros", "uid": "u1", "running": True, "labels": {"dr": "gold"}, "cpu": 1,
     "memory": "256Mi", "networks": ["default/lab-net"], "cloud_init": "nocloud",
     "disks": [{"disk": "rootdisk", "claim": "c1", "size": 2 * 1024 ** 3, "used": 50 * 1024 ** 2,
                "storage_class": "harvester-longhorn", "image": "default/image-a", "eligible": True, "reason": ""},
               {"disk": "data", "claim": "c2", "size": 4 * 1024 ** 3, "used": 1024 ** 3, "storage_class": "fast",
                "image": "", "eligible": True, "reason": ""}],
     "eligible": True, "reasons": [], "plans": []},
    {"namespace": "default", "name": "ui-cirros-2", "uid": "u2", "running": False, "labels": {}, "cpu": 2, "memory": "1Gi",
     "networks": ["default/lab-net"], "cloud_init": "",
     "disks": [{"disk": "rootdisk", "claim": "c3", "size": 1024 ** 3, "used": 1024 ** 2, "storage_class": "harvester-longhorn",
                "image": "", "eligible": True, "reason": ""}], "eligible": True, "reasons": [], "plans": []},
    {"namespace": "default", "name": "legacy", "uid": "u3", "running": True, "labels": {}, "cpu": 1, "memory": "1Gi",
     "networks": [], "cloud_init": "", "disks": [], "eligible": False, "reasons": ["longhorn-v2"], "plans": []}]}

LINKS = {"links": [{"source": "harv-fake", "target": "harv-dr", "plans": [{"id": PID, "name": "erp"}],
                    "backup_target": "nfs://172.16.0.5:/volume1/BACKUP/lab/",
                    "passive": {"last_at": iso(NOW - timedelta(minutes=2)), "upload_bps": 4.2e6, "apply_bps": 9e6,
                                "upload_bps_p50_24h": 4e6, "apply_bps_p50_24h": 9e6, "bytes_24h": 3 * 1024 ** 3,
                                "passes_24h": 280,
                                "series": [{"t": iso(NOW - timedelta(hours=h)), "upload_bps": 4e6 + h * 1e5, "apply_bps": 9e6}
                                           for h in range(24, 0, -1)]},
                    "active": {"at": iso(NOW - timedelta(hours=5)), "by": "alice", "action_id": "x", "size_bytes": 512 * 1024 ** 2,
                               "write_bps": 110 * 1024 ** 2, "read_bps": 95 * 1024 ** 2, "write_s": 4.6, "read_s": 5.4, "error": ""}}]}

NOTIFY = {"enabled": True, "min_level": "warn", "codes": None, "channels": [
    {"id": "c1", "name": "ops room", "kind": "matrix", "homeserver": "https://matrix.example", "room": "!abc:example",
     "token": "********"}]}


@pytest.fixture
def ui_factory(context, flask_server):
    def make(role="admin"):
        context.add_init_script(
            "if (!sessionStorage.getItem('rp-init')) {"
            "localStorage.setItem('harvester_ops_language','en');"
            "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
            "localStorage.setItem('harvester_ops_current_tab','namespaces');"
            "localStorage.removeItem('harvester_ops_replication');"
            "localStorage.removeItem('harvester_ops_open_panels');"
            "sessionStorage.setItem('rp-init','1'); }")
        page = context.new_page()
        sent = []
        created = {}
        ok = lambda body, status=200: dict(status=status, content_type="application/json", body=json.dumps(body))  # noqa: E731

        def repl(route, req):
            path = req.url.split("/api/replication/")[1]
            bare = path.split("?")[0]
            q = path.split("?")[1] if "?" in path else ""
            if req.method != "GET":
                body = req.post_data_json if req.post_data else None
                sent.append((req.method, bare, body))
                if req.method == "POST" and bare == "plans":
                    created["name"] = body["name"]
                return route.fulfill(**ok({"action_id": ACTION, "plan": PID}, 202))
            sent.append(("GET", bare, q))
            if bare == "plans":
                d = plans_payload(role)
                if created:
                    d["plans"][0]["name"] = created["name"]       # le contrat tout juste créé
                return route.fulfill(**ok(d))
            if bare == "alerts":
                return route.fulfill(**ok(ALERTS))
            if bare == f"plans/{PID}":
                return route.fulfill(**ok(plan_detail(role)))
            if bare == f"plans/{PID2}":
                d = plan_detail(role)
                d["plan"].update(id=PID2, name="crm", state="draft")
                d["row"] = plans_payload(role)["plans"][1]
                return route.fulfill(**ok(d))
            if bare == f"plans/{PID}/series":
                rng = re.search(r"range=(\w+)", q).group(1)
                return route.fulfill(**ok(series(rng)))
            if bare == f"plans/{PID}/events":
                evs = [{"id": 30 - i, "ts": iso(NOW - timedelta(minutes=i * 5)), "plan_id": PID,
                        "resource_uid": "u-db" if i % 2 else "", "level": "warn" if i == 1 else "info",
                        "operation": "follow", "step": "status", "code": "rpo-exceeded" if i == 1 else "applied",
                        "message": f"event {i}", "by": "follow", "action_id": ""} for i in range(5)]
                if "level=warn" in q:
                    evs = [e for e in evs if e["level"] != "info"]
                return route.fulfill(**ok({"plan": PID, "events": evs, "next_before": None}))
            if bare == "hosts/harv-fake/resources":
                return route.fulfill(**ok({"host": "harv-fake", "side": "any", "role": role, "resources": [
                    resource("u-web", "web-1", "protected", 400, side="source", peer="harv-dr"),
                    resource("u-crm", "crm-1", "draft", None, side="target", plan=PID2, plan_name="crm", peer="harv-dr")]}))
            if bare.startswith("candidates/"):
                d = json.loads(json.dumps(CANDIDATES))
                if "selector=" in q:
                    d["vms"] = [v for v in d["vms"] if v["labels"].get("dr") == "gold"]
                return route.fulfill(**ok(d))
            if bare == "links":
                return route.fulfill(**ok(LINKS))
            if bare == "notify":
                return route.fulfill(**ok({"notify": NOTIFY, "codes": [], "kinds": ["matrix", "slack", "webhook", "email"]}))
            return route.fulfill(**ok({"error": "unexpected " + bare}, 404))
        page.route(re.compile(r".*/api/replication/.*"), repl)
        tnets = [{"name": "prod", "namespace": "default", "vlan": "200", "cluster_network": "mgmt"}]
        page.route(re.compile(r".*/api/networks/harv-dr$"), lambda r, q: r.fulfill(**ok(tnets)))
        page.route(re.compile(r".*/api/networks/harv-fake$"), lambda r, q: r.fulfill(**ok(
            [{"name": "lab-net", "namespace": "default", "vlan": "100", "cluster_network": "mgmt"}])))
        page.route(re.compile(r".*/api/storageclasses/harv-dr$"), lambda r, q: r.fulfill(**ok(
            [{"name": "harvester-longhorn", "is_default": True}, {"name": "dr-nvme", "is_default": False}])))
        page.route(re.compile(r".*/api/namespaces/harv-(dr|fake)$"), lambda r, q: r.fulfill(**ok([{"name": "default"}])))
        page.route(re.compile(r".*/api/network-fabric/harv-dr$"), lambda r, q: r.fulfill(**ok(
            {"cluster_networks": ["mgmt", "storage"]})))

        def objects(route, req):
            sent.append(("POST", "objects/" + req.url.split("/api/objects/")[1], req.post_data_json))
            tnets.append({"name": req.post_data_json["spec"]["name"], "namespace": "default",
                          "vlan": str(req.post_data_json["spec"]["vlan"]), "cluster_network": "mgmt"})
            return route.fulfill(**ok({"action_id": ACTION}, 202))
        page.route(re.compile(r".*/api/objects/harv-dr/network$"), objects)
        page.route(re.compile(r".*/api/stream/.*"), lambda r, q: r.fulfill(
            status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
        page.route(re.compile(r".*/api/action/rpact.*"), lambda r, q: r.fulfill(**ok(
            {"id": ACTION, "status": "done", "result": {"output": {"ok": True, "message": "dry run: 2 VM(s) would fail over",
                                                                    "point_at": {"default/web-1": iso(NOW)}, "skew_s": {"default/web-1": 2},
                                                                    "lost_after": {}, "waves": [{"index": 1, "vms": ["default/db-1"]}]}}})))
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        # navigation 2.0 : la liste des clusters vient d'App.listClusters()
        # (/api/clusters), plus du sélecteur ; harv-dr y est ajouté
        clusters_body = {"clusters": [{"name": n, "description": "", "node_count": 1, "origin": "config"}
                                      for n in ("harv-fake", "harv-dr")], "config_writable": True}
        page.route(re.compile(r".*/api/clusters$"), lambda r, q: r.fulfill(**ok(clusters_body))
                   if q.method == "GET" else r.continue_())
        page.goto(flask_server["base_url"], wait_until="domcontentloaded")
        page.wait_for_function("window.Replication && window.App && App.getCurrentCluster()")
        page.evaluate("() => App.clustersInfo()")
        page.evaluate("""() => { const s = document.getElementById('cluster-select');
            if (![...s.options].some(o => o.value === 'harv-dr')) { const o = document.createElement('option');
            o.value = 'harv-dr'; o.textContent = 'harv-dr'; s.appendChild(o); } }""")
        page.mouse.move(1200, 600)
        return page, sent, dialogs
    return make


@pytest.fixture
def ui(ui_factory):
    return ui_factory("admin")


def wait_sent(page, sent, pred, timeout=5000):
    for _ in range(timeout // 100):
        hits = [s for s in sent if pred(s)]
        if hits:
            return hits
        page.wait_for_timeout(100)
    raise AssertionError(f"not sent; got {sent}")


def open_plans(page):
    page.evaluate("App.setTab('drplans')")
    pane = page.locator("#tab-drplans")
    expect(pane.locator(".rp-card")).to_be_visible()
    return pane


# --- menu et liste ----------------------------------------------------------------------

def test_menu_places_and_alert_badges(ui):
    page, _, _ = ui
    grp = page.locator("#tab-group-dr")
    assert grp.locator(".tab-child").evaluate_all("els => els.map(e => e.dataset.tab)") == ["drplans", "drlinks"]
    assert page.locator('#tab-group-cluster .tab[data-tab="clusterdr"]').count() == 1
    for t in ("drplans", "drlinks", "clusterdr"):
        assert page.locator(f'.tab[data-tab="{t}"]').get_attribute("title")
    # pastilles : 2 alertes, toutes sur harv-fake
    expect(grp.locator('[data-rp-badge="all"]')).to_have_text("2")
    expect(page.locator('[data-rp-badge="cluster"]')).to_have_text("2")
    assert "2 active" in grp.locator('[data-rp-badge="all"]').get_attribute("data-tip")


def test_contract_cards_show_gauge_direction_vms_event_and_transfer(ui):
    page, _, _ = ui
    pane = open_plans(page)
    card = pane.locator(f'.rp-plan-card[data-rp-plan="{PID}"]')
    expect(card).to_be_visible()
    expect(card.locator('[data-rp-state="lagging"]')).to_have_text("Objective exceeded")
    expect(card.locator('[data-rp-gauge="over"]')).to_be_visible()
    assert "objective of 15 min" in card.locator(".rp-gauge").get_attribute("data-tip")
    expect(card.locator('[data-rp-reach="ok"]')).to_have_count(1)
    expect(card.locator('[data-rp-reach="fail"]')).to_have_count(1)
    expect(card).to_contain_text("2 VM(s)")
    expect(card.locator('[data-rp-vmstate="lagging"]')).to_contain_text("1")
    expect(card).to_contain_text("RPO held 1100 s")
    expect(card).to_contain_text("last 25 MiB, up 4 MiB/s, apply 9 MiB/s")
    expect(card).to_contain_text("3 GiB in 24 h (280 passes)")
    # alertes en tête
    expect(pane.locator('[data-rp-alert="rpo-exceeded"]')).to_contain_text("RPO objective exceeded")
    for el in pane.locator(".res-tools button, .res-tools select, .res-tools input").all():
        assert el.get_attribute("data-tip"), el.evaluate("e => e.outerHTML")


def test_cards_table_toggle_is_kept_and_filters_work(ui):
    page, _, _ = ui
    pane = open_plans(page)
    expect(pane.locator(".rp-plan-card")).to_have_count(2)
    pane.locator('[data-rp-view="table"]').click()
    expect(pane.locator(".rp-table tbody tr")).to_have_count(2)
    expect(pane.locator(".rp-plan-card")).to_have_count(0)
    assert json.loads(page.evaluate("localStorage.getItem('harvester_ops_replication')"))["view"] == "table"
    page.evaluate("App.setTab('drlinks')")
    pane = open_plans(page)
    expect(pane.locator(".rp-table tbody tr")).to_have_count(2)          # le choix reste
    pane.locator('[data-rp="state"]').select_option("draft")
    expect(pane.locator(".rp-table tbody tr")).to_have_count(1)
    expect(pane.locator(".rp-table tbody tr")).to_contain_text("crm")
    pane.locator('[data-rp="state"]').select_option("")
    pane.locator('[data-rp="q"]').fill("db-1")
    expect(pane.locator(".rp-table tbody tr")).to_have_count(1)
    expect(pane.locator(".rp-table tbody tr")).to_contain_text("erp")


def test_cluster_subview_shows_both_roles_and_filters(ui):
    page, _, _ = ui
    page.evaluate("App.setTab('clusterdr')")
    pane = page.locator("#tab-clusterdr")
    expect(pane.locator(".rp-card")).to_be_visible()
    expect(pane.locator('.rp-plan-card [data-rp-role="source"]')).to_have_count(1)
    expect(pane.locator('.rp-plan-card [data-rp-role="target"]')).to_have_count(1)
    expect(pane.locator(".rp-host-table tbody tr")).to_have_count(2)
    pane.locator('[data-rp-side="target"]').click()
    expect(pane.locator(".rp-plan-card")).to_have_count(1)
    expect(pane.locator(".rp-plan-card")).to_contain_text("crm")
    expect(pane.locator(".rp-host-table tbody tr")).to_have_count(1)
    expect(pane.locator('.rp-host-table tbody tr[data-side="target"]')).to_have_count(1)
    pane.locator('[data-rp-side="source"]').click()
    expect(pane.locator(".rp-plan-card")).to_have_count(1)
    expect(pane.locator(".rp-plan-card")).to_contain_text("erp")


def test_events_tab_lists_replication_alerts(ui):
    page, _, _ = ui
    page.evaluate("App.setTab('overview')")
    page.locator('[data-overview-tab="events"]').click()
    board = page.locator('[data-board="events"]')
    btn = board.locator('[data-ev-group="dr"]')
    expect(btn).to_be_visible()
    expect(board.locator('[data-ev-count="dr"]')).to_contain_text("2", timeout=8000)
    btn.click()
    expect(board.locator(".ev-table tbody tr")).to_have_count(2)
    expect(board.locator(".ev-table")).to_contain_text("RPO objective exceeded")


# --- fenêtre de création ----------------------------------------------------------------

def open_create(page):
    pane = open_plans(page)
    pane.locator('[data-rp="new"]').click()
    panel = page.locator("#fp-rp-create")
    expect(panel.locator('[data-rpc-vm="default/ui-cirros"]')).to_be_visible()
    return panel


def test_create_window_blocks_until_network_is_created_on_target(ui):
    page, sent, _ = ui
    panel = open_create(page)
    panel.locator('[name="to"]').select_option("harv-dr")
    panel.locator('[name="name"]').fill("ui-erp")
    panel.locator('[data-rpc-pick="default/ui-cirros"]').check()
    expect(panel.locator('[data-rpc-pick="default/legacy"]')).to_be_disabled()
    net = panel.locator('[data-rpc-net="default/lab-net"]')
    expect(net.locator('[data-rpc-ready="fail"]')).to_have_text("missing, blocking")
    expect(panel.locator('[data-rpc="create"]')).to_be_disabled()
    expect(panel.locator('[data-rpc-issue="3"]').first).to_contain_text("lab-net")
    expect(panel.locator('[data-rpc-state="3"] [data-rpc-ready="fail"]')).to_be_visible()
    # créer sur la cible, VLAN changé
    net.locator("[data-rpc-netcreate]").click()
    form = panel.locator('[data-rpc-netform="default/lab-net"]')
    expect(form.locator('[data-rpc-nc="vlan"]')).to_have_value("100")
    form.locator('[data-rpc-nc="vlan"]').fill("310")
    form.locator('[data-rpc-nc="cn"]').select_option("storage")
    form.locator("[data-rpc-ncgo]").click()
    [post] = wait_sent(page, sent, lambda s: s[1] == "objects/harv-dr/network")
    assert post[2]["spec"] == {"namespace": "default", "name": "lab-net", "type": "vlan", "vlan": 310,
                               "cluster_network": "storage", "route_mode": "auto"}
    expect(net.locator('[data-rpc-ready="ok"]')).to_be_visible()
    expect(panel.locator('[data-rpc="issues"]')).not_to_contain_text("lab-net")
    # reste bloquant : le disque « data » n'a pas de classe sur le secours
    expect(panel.locator('[data-rpc-issue="3"]')).to_contain_text("Disk data of default/ui-cirros")


def test_create_window_per_disk_class_waves_hooks_and_skip_measure(ui):
    page, sent, _ = ui
    panel = open_create(page)
    panel.locator('[name="to"]').select_option("harv-dr")
    panel.locator('[name="name"]').fill("ui-erp")
    panel.locator('[data-rpc-pick="default/ui-cirros"]').check()
    panel.locator('[data-rpc-pick="default/ui-cirros-2"]').check()
    panel.locator('[data-rpc-netmap="default/lab-net"]').select_option("default/prod")
    panel.locator('[data-rpc-drill="default/lab-net"]').select_option("default/prod")
    # classe disque par disque : « fast » n'existe pas sur le secours, bloquant
    disk = panel.locator('[data-rpc-disk="default/ui-cirros/data"]')
    expect(disk.locator('[data-rpc-ready="fail"]')).to_be_visible()
    disk.locator("[data-rpc-sc]").select_option("dr-nvme")
    panel.locator('[data-rpc-disk="default/ui-cirros/rootdisk"] [data-rpc-sc]').select_option("dr-nvme")
    expect(panel.locator('[data-rpc-disk="default/ui-cirros-2/rootdisk"] [data-rpc-sc]')).to_have_value("harvester-longhorn")
    # vagues : une deuxième, ui-cirros-2 glissé dedans, condition délai
    panel.locator('[data-rpw="add"]').click()
    panel.locator('[data-rpw-vm="default/ui-cirros-2"]').drag_to(panel.locator('[data-rp-wave="1"]'))
    expect(panel.locator('[data-rp-wave="1"] [data-rpw-vm="default/ui-cirros-2"]')).to_be_visible()
    panel.locator('[data-rpw-gate="0"]').select_option("delay")
    panel.locator('[data-rpw-sec="0"]').fill("45")
    panel.locator('[data-rpw-sec="0"]').dispatch_event("change")
    # CPU réduit, cloud-init rejoué avec une configuration réseau
    panel.locator('[data-rpr="cloud_init"]').select_option("rerun")
    panel.locator('[data-rpr-cpu="default/ui-cirros"]').fill("1")
    panel.locator('[data-rpr-mem="default/ui-cirros"]').fill("512Mi")
    panel.locator('[data-rpr-net="default/ui-cirros"]').fill("version: 2\nethernets: {}")
    panel.locator('[data-rpr-net="default/ui-cirros"]').dispatch_event("change")
    # action annexe avec un secret
    panel.locator('[data-rph="add"]').click()
    panel.locator('[name="h_name"]').fill("dns")
    panel.locator('[name="h_at"]').select_option("wave:2")
    panel.locator('[name="h_url"]').fill("https://dns.example/erp")
    panel.locator('[name="h_sname"]').fill("Authorization")
    panel.locator('[name="h_svalue"]').fill("Bearer s3cr3t")
    panel.locator('[data-rph="keep"]').click()
    row = panel.locator('[data-rp-hook="dns"]')
    expect(row).to_contain_text("After wave 2")
    expect(row).to_contain_text("Authorization: ********")
    expect(row).not_to_contain_text("s3cr3t")
    # mesure sautée : avertissement avec l'estimation
    panel.locator('[name="measure"][value="no"]').check()
    warn = panel.locator('[data-rpc="skipwarn"]')
    expect(warn).to_contain_text("the RPO is estimated, not measured")
    expect(warn).to_contain_text("First full copy")
    expect(panel.locator('[data-rpc="create"]')).to_contain_text("Create and protect")
    expect(panel.locator('[data-rpc-check="ok"]').first).to_be_visible()
    assert "s3cr3t" not in panel.locator('[data-rpc="cli"]').inner_text()
    panel.locator('[data-rpc="create"]').click()
    [post] = wait_sent(page, sent, lambda s: s[0] == "POST" and s[1] == "plans")
    b = post[2]
    assert b["name"] == "ui-erp" and b["from"] == "harv-fake" and b["to"] == "harv-dr"
    assert b["vms"] == ["default/ui-cirros", "default/ui-cirros-2"]
    assert b["map_net"] == {"default/lab-net": "default/prod"}
    assert b["map_drill_net"] == {"default/lab-net": "default/prod"}
    assert b["map_disk_class"] == {"default/ui-cirros/data": "dr-nvme", "default/ui-cirros/rootdisk": "dr-nvme"}
    assert b["cloud_init"] == "rerun" and b["rpo"] == "15m"
    assert b["interval"] == "360s"             # protection directe : l'intervalle estimé part avec le contrat
    r = b["recovery"]
    assert r["waves"] == [{"vms": ["default/ui-cirros"], "gate": {"kind": "delay", "seconds": 45}},
                          {"vms": ["default/ui-cirros-2"], "gate": {"kind": "running"}}]
    assert r["resize"] == {"default/ui-cirros": {"cpu": 1, "memory": "512Mi"}}
    assert r["network_data"] == {"default/ui-cirros": "version: 2\nethernets: {}"}
    assert r["hooks"][0]["secret_headers"] == {"Authorization": "Bearer s3cr3t"} and r["hooks"][0]["at"] == "wave:2"
    # puis la protection directe (mesure sautée)
    wait_sent(page, sent, lambda s: s[0] == "POST" and s[1] == f"plans/{PID}/protect")


def test_create_window_label_rule_and_measure_first(ui):
    page, sent, _ = ui
    panel = open_create(page)
    panel.locator('[name="to"]').select_option("harv-dr")
    panel.locator('[name="name"]').fill("gold")
    panel.locator('[data-rpc-mode="rule"]').click()
    panel.locator('[name="rule"]').fill("dr=gold")
    expect(panel.locator('[data-rpc="rulecount"]')).to_contain_text("1 VM(s) of default carry the label")
    expect(panel.locator('[data-rpc="vms"] [data-rpc-vm="default/ui-cirros"]')).to_be_visible()
    expect(panel.locator('[data-rpc="vms"] [data-rpc-vm="default/ui-cirros-2"]')).to_have_count(0)
    panel.locator('[data-rpc-netmap="default/lab-net"]').select_option("default/prod")
    panel.locator('[data-rpc-disk="default/ui-cirros/data"] [data-rpc-sc]').select_option("dr-nvme")
    expect(panel.locator('[data-rpc="create"]')).to_contain_text("Create and measure")
    panel.locator('[data-rpc="create"]').click()
    [post] = wait_sent(page, sent, lambda s: s[0] == "POST" and s[1] == "plans")
    assert post[2]["selector"] == "dr=gold" and post[2]["namespace"] == "default" and "vms" not in post[2]
    assert post[2]["recovery"]["auto_protect"] is True and "interval" not in post[2]
    [m] = wait_sent(page, sent, lambda s: s[0] == "POST" and s[1] == f"plans/{PID}/measure")
    assert m[2] == {"op": "start"}


# --- vue d'un contrat ------------------------------------------------------------------

def open_view(page):
    pane = open_plans(page)
    pane.locator(f'.rp-plan-card[data-rp-plan="{PID}"] [data-rp-open]').first.click()
    view = page.locator(f"#fp-rp-plan-{PID}")
    expect(view.locator(".rp-vm-table")).to_be_visible()
    return view


def test_contract_view_tabs_overview_charts_journal(ui):
    page, sent, _ = ui
    view = open_view(page)
    expect(view.locator(".floating-panel-title")).to_have_text("Contract erp")
    expect(view.locator('[data-rp-vm="u-db"]')).to_contain_text("objective exceeded")
    expect(view.locator('[data-rpv="pending"]')).to_contain_text("default/web-2")
    expect(view.locator(".rp-runs")).to_contain_text("rolled-back")
    view.locator('[data-rpv-tab="charts"]').click()
    expect(view.locator('[data-chart="rpo"] path.line.rpo')).to_have_count(1)
    expect(view.locator('[data-chart="rpo"] line.ref')).to_have_count(1)
    expect(view.locator('[data-chart="rate"] path.line')).to_have_count(2)
    expect(view.locator('[data-chart="bytes"] rect.bar')).to_have_count(30)
    assert any(s[1] == f"plans/{PID}/series" and "range=24h" in s[2] for s in sent)
    view.locator('[data-chart="rpo"] svg').hover()
    expect(view.locator('[data-chart="rpo"] .rp-tip')).to_be_visible()
    view.locator('[data-rpv-range="7d"]').click()
    expect(view.locator('[data-chart="bytes"] rect.bar')).to_have_count(40)
    assert any(s[1] == f"plans/{PID}/series" and "range=7d" in s[2] for s in sent)
    view.locator('[data-rpv-tab="journal"]').click()
    expect(view.locator(".rp-journal tbody tr")).to_have_count(5)
    expect(view.locator(".rp-journal")).to_contain_text("default/db-1")
    view.locator('[data-rpv="warn"]').check()
    expect(view.locator(".rp-journal tbody tr")).to_have_count(1)


def test_contract_settings_save_then_undo(ui):
    page, sent, _ = ui
    view = open_view(page)
    view.locator('[data-rpv-tab="settings"]').click()
    f = view.locator(".rp-settings")
    f.locator('[name="rpo"]').fill("20")
    f.locator('[name="paused"]').check()
    f.locator('[data-rpv="save-follow"]').click()
    [patch] = wait_sent(page, sent, lambda s: s[0] == "PATCH")
    assert patch[1] == f"plans/{PID}/settings" and patch[2] == {"rpo": "20m", "paused": True}
    undo = view.locator('[data-rp-undo]')
    expect(undo).to_be_visible()
    assert undo.get_attribute("data-tip")
    undo.click()
    # valeur d'avant exacte, en secondes (pas arrondie aux minutes du champ)
    patches = wait_sent(page, sent, lambda s: s[0] == "PATCH" and s[2] == {"rpo": "900s", "paused": False})
    assert patches
    # la reprise existante est relue : vagues et action annexe masquée
    expect(view.locator('[data-rp-wave="0"] [data-rpw-vm="default/db-1"]')).to_be_visible()
    expect(view.locator('[data-rp-hook="dns"]')).to_contain_text("********")
    view.locator('[data-rph-try="dns"]').click()
    [t] = wait_sent(page, sent, lambda s: s[1] == f"plans/{PID}/hooks/test")
    assert t[2] == {"name": "dns"}


def test_failover_dialog_steps_dry_run_typed_name(ui):
    page, sent, _ = ui
    pane = open_plans(page)
    pane.locator(f'.rp-plan-card[data-rp-plan="{PID}"] [data-rp-act="failover"]').click()
    dlg = page.locator(f"#fp-rp-failover-{PID}")
    expect(dlg.locator(".floating-panel-title")).to_have_text("Fail over erp to harv-dr")
    steps = dlg.locator('[data-rp-fo="steps"] li')
    expect(steps).to_have_count(9)
    expect(dlg.locator('li[data-step="rp.fo.step.activate"]')).to_contain_text("point of no return")
    # essai à blanc d'office, contrôles avant le bouton
    [dry] = wait_sent(page, sent, lambda s: s[1] == f"plans/{PID}/failover" and s[2].get("dry_run"))
    assert dry[2] == {"mode": "planned", "dry_run": True, "all_vms": True}
    expect(dlg.locator('[data-rp-fo-check="ok"]').filter(has_text="Dry run")).to_be_visible()
    expect(dlg.locator('[data-rp-fo-check="fail"]').filter(has_text="harv-dr does not answer")).to_be_visible()
    dlg.locator('[name="mode"][value="unplanned"]').check()
    expect(steps).to_have_count(8)
    expect(dlg.locator('[data-rp-fo="loss"]')).to_contain_text("up to 18 min")
    go = dlg.locator('[data-rp-fo="go"]')
    expect(go).to_be_disabled()
    dlg.locator('[name="confirm"]').fill("er")
    expect(go).to_be_disabled()
    dlg.locator('[name="confirm"]').fill("erp")
    expect(go).to_be_enabled()
    dlg.locator('[name="all"]').uncheck()
    dlg.locator('[name="vm"][value="default/db-1"]').uncheck()
    go.click()
    [real] = wait_sent(page, sent, lambda s: s[1] == f"plans/{PID}/failover" and not s[2].get("dry_run"))
    assert real[2] == {"mode": "unplanned", "dry_run": False, "vms": ["default/web-1"], "force_unplanned": False,
                       "accept_skew": False, "confirm": "erp"}


def test_failover_dialog_is_admin_only(ui_factory):
    page, sent, _ = ui_factory("operator")
    pane = open_plans(page)
    btn = pane.locator(f'.rp-plan-card[data-rp-plan="{PID}"] [data-rp-act="failover"]')
    assert "Needs the admin role" in btn.get_attribute("data-tip")
    btn.click()
    dlg = page.locator(f"#fp-rp-failover-{PID}")
    expect(dlg.locator('[data-rp-fo="notadmin"]')).to_contain_text("your role is operator")
    expect(dlg.locator('[name="confirm"]')).to_be_disabled()
    expect(dlg.locator('[data-rp-fo="go"]')).to_be_disabled()
    expect(dlg.locator('[data-rp-fo="dry"]')).to_be_disabled()
    page.wait_for_timeout(400)
    assert not [s for s in sent if s[1].endswith("/failover")]


def test_viewer_cannot_create(ui_factory):
    page, _, _ = ui_factory("viewer")
    pane = open_plans(page)
    nb = pane.locator('[data-rp="new"]')
    expect(nb).to_be_disabled()
    assert "operator role" in nb.get_attribute("data-tip")
    expect(pane.locator(f'.rp-plan-card[data-rp-plan="{PID2}"] [data-rp-act="protect"]')).to_be_disabled()


def test_unprotect_needs_the_typed_name(ui):
    page, sent, _ = ui
    view = open_view(page)
    view.locator('[data-rpv-tab="settings"]').click()
    view.locator('[data-rp-act="unprotect"]').click()
    dlg = page.locator(f"#fp-rp-danger-{PID}")
    btn = dlg.locator("[data-rp-confirm]")
    expect(btn).to_be_disabled()
    dlg.locator('[name="confirm"]').fill("erp")
    btn.click()
    [u] = wait_sent(page, sent, lambda s: s[1] == f"plans/{PID}/unprotect")
    assert u[2] == {"confirm": "erp", "delete_backups": False}


# --- liens, notifications -------------------------------------------------------------

def test_links_passive_and_active_test(ui):
    page, sent, _ = ui
    page.evaluate("App.setTab('drlinks')")
    pane = page.locator("#tab-drlinks")
    link = pane.locator('[data-rp-link="harv-fake|harv-dr"]')
    expect(link).to_be_visible()
    expect(link).to_contain_text("nfs://172.16.0.5:/volume1/BACKUP/lab/")
    expect(link.locator("[data-rpl-write]")).to_have_text("110 MiB/s")
    expect(link.locator("svg.rp-spark")).to_have_count(2)
    link.locator('[data-rpl="size"]').select_option("1024")
    link.locator('[data-rpl="test"]').click()
    [t] = wait_sent(page, sent, lambda s: s[1] == "links/test")
    assert t[2] == {"from": "harv-fake", "to": "harv-dr", "size_mib": 1024}
    # le résultat reste affiché après la relecture qui suit l'essai
    expect(pane.locator('[data-rp-link="harv-fake|harv-dr"] [data-rpl="msg"]')).to_contain_text("Link test finished")


def test_notifications_window_keeps_masked_secrets(ui):
    page, sent, _ = ui
    pane = open_plans(page)
    pane.locator('[data-rp="notify"]').click()
    win = page.locator("#fp-rp-notify")
    ch = win.locator('[data-rp-channel="0"]')
    expect(ch.locator('[name="token"]')).to_have_value("********")
    win.locator('[data-rpn="kind"]').select_option("slack")
    win.locator('[data-rpn="add"]').click()
    win.locator('[data-rp-channel="1"] [name="url"]').fill("https://hooks.slack.example/T/B/x")
    win.locator('[data-rpn="save"]').click()
    [put] = wait_sent(page, sent, lambda s: s[0] == "PUT")
    body = put[2]
    assert body["channels"][0]["token"] == "********" and body["channels"][0]["id"] == "c1"
    new = dict(body["channels"][1])
    assert re.match(r"^c-[a-z0-9]+-[a-z0-9]+$", new.pop("id"))
    assert new == {"kind": "slack", "name": "", "url": "https://hooks.slack.example/T/B/x"}
    ch.locator('[data-rpn="test"]').click()
    [t] = wait_sent(page, sent, lambda s: s[1] == "notify/test")
    assert t[2] == {"channel": "c1"}
