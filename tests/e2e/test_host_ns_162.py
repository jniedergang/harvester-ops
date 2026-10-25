"""v1.62.0 : la fenêtre d'un hôte et la fenêtre Namespaces, dans un navigateur.

La fenêtre d'un hôte a ses six onglets ; chaque enregistrement envoie ce que
l'outil attend (labels système jamais envoyés, mot de passe du BMC dans le
corps seulement) ; l'alimentation n'est offerte qu'en maintenance. La fenêtre
Namespaces cache le système, crée, modifie et supprime en faisant taper le nom.
"""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

GI = 1024 ** 3
SETTINGS = {
    "node": "n1", "custom_name": "baie 1", "console_url": "https://10.0.0.21", "labels": {"rack": "r2"},
    "maintenance": None, "witness": False, "nodes": 3, "capi_machine": None, "tags": ["ssd"], "longhorn": True,
    "disks": [
        {"name": "bd-sde", "dev_path": "/dev/sde", "size": 100 * GI, "type": "disk", "state": "Active", "phase": "Provisioned",
         "in_longhorn": True, "provisioned": True, "addable": False, "tags": [], "scheduling": True, "ready": True,
         "schedulable": True, "storage_available": 90 * GI, "storage_maximum": 100 * GI, "storage_scheduled": 10 * GI,
         "provisioner": "LonghornV1"},
        {"name": "bd-sdb", "dev_path": "/dev/sdb", "size": 50 * GI, "type": "disk", "state": "Active", "phase": "Unprovisioned",
         "in_longhorn": False, "provisioned": False, "addable": True, "tags": [], "scheduling": None}],
    "hugepages": {"transparent": {"enabled": "always", "shmemEnabled": "never", "defrag": "madvise"}, "meminfo": {"HugePages_Total": 0}},
    "ksmtuned": {"spec": {"run": "stop", "mode": "standard", "thresCoef": 20, "mergeAcrossNodes": 0,
                          "ksmtunedParameters": {"sleepMsec": 20, "boost": 0, "decay": 0, "minPages": 100, "maxPages": 100}},
                 "status": {"shared": 0}},
    "cpu_manager": {"enabled": False, "label": "false", "status": None, "policy": None},
    "seeder": True, "seeder_installed": True,
    "oob": {"host": "10.0.0.21", "port": 623, "insecure": False, "secret": "harvester-system/n1-bmc",
            "events": {"enabled": True, "pollingInterval": "1h"}, "status": "inventoryNodeReady", "power_state": "on",
            "power_action": {}, "requested": None},
}
NAMESPACES = {"items": [
    {"name": "team-a", "system": False, "phase": "Active", "description": "Équipe A", "labels": {"env": "prod"},
     "annotations": {}, "created": "2026-09-01T10:00:00Z", "vms": 2, "volumes": 3, "snapshot_quota": 10 * GI},
    {"name": "kube-system", "system": True, "phase": "Active", "description": "", "labels": {}, "annotations": {},
     "created": "2026-01-01T00:00:00Z", "vms": 0, "volumes": 0, "snapshot_quota": None}]}


DETAIL = {"node": "n1", "basics": {
    "custom_name": "baie 1", "console_url": "", "ip": "172.16.2.61", "role": "management", "os": "Harvester v1.9.0",
    "kernel": "6.12", "runtime": "containerd://2", "kubelet": "v1.36", "uuid": "u-1", "created": "2026-09-01T10:00:00Z",
    "ready": True, "unschedulable": False, "maintenance": "", "manufacturer": "HPE", "serial": "USE6236RY1", "model": "",
    "ntp": {"status": "unsynced", "servers": "pool.lan"},
    "cpu": {"capacity": 8, "allocatable": 8, "used": 2}, "memory": {"capacity": 16 * GI, "allocatable": 16 * GI, "used": 4 * GI},
    "storage": {"maximum": 100 * GI, "available": 60 * GI, "scheduled": 50 * GI}},
    "instances": [{"namespace": "default", "name": "web", "phase": "Running", "ips": ["10.52.0.9"], "cpu": 2,
                   "memory": GI, "created": None, "migrating": False}],
    "vlans": [{"cluster_network": "data", "vlan_config": "data-all", "vlans": [20], "ready": True, "message": ""}],
    "nics": [{"name": "enp1s0", "type": "device", "state": "up", "mac": "52:54:00:00:00:01", "master": "mgmt-bo"}],
    "events": [{"type": "Warning", "reason": "Rebooted", "message": "node rebooted", "count": 1, "last": "2026-09-27T08:00:00Z"}]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','fr');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','namespaces');"
        "localStorage.removeItem('harvester_ops_host_tab');")
    page = context.new_page()
    sent = []
    state = {"settings": json.loads(json.dumps(SETTINGS))}

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "host00000162"}, 202)
    page.route("**/api/host/harv-fake/n1/settings", lambda r, q: fulfill(r, state["settings"]))
    page.route("**/api/host/harv-fake/n1/detail", lambda r, q: fulfill(r, DETAIL))
    page.route("**/api/host/harv-fake/n1/do/**", writes)
    page.route("**/api/ns-admin/harv-fake", lambda r, q: writes(r, q) if q.method == "POST" else fulfill(r, NAMESPACES))
    page.route("**/api/ns-admin/harv-fake/*/do/**", writes)
    page.route("**/api/vms/harv-fake", lambda r, q: fulfill(r, {"vms": []}))
    page.route("**/api/namespaces/harv-fake", lambda r, q: fulfill(r, [{"name": "team-a"}]))
    page.route("**/api/stream/host00000162", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.HostSettings && window.Namespaces && window.App && App.getCurrentCluster()")
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    return page, sent, state, dialogs


def host_win(page):
    return page.locator(".floating-panel", has=page.locator(".hs-win")).last


def open_host(page, tab="general"):
    page.evaluate(f"HostSettings.open('harv-fake', 'n1', {{ tab: '{tab}' }})")
    w = host_win(page)
    expect(w.locator(".hs-body form").first).to_be_visible(timeout=8000)
    page.wait_for_load_state("networkidle")
    return w


def test_the_host_window_has_its_tabs_and_saves_only_changes(ui):
    page, sent, _, _ = ui
    w = open_host(page)
    # v1.68.1 : Basics, Instances, Network et Events, comme dans Harvester
    assert w.locator("[data-hs-tab]").evaluate_all("els => els.map(e => e.dataset.hsTab)") == [
        "basics", "instances", "network", "general", "disks", "hugepages", "ksm", "oob", "events", "actions"]
    for b in w.locator("[data-hs-tab]").all():
        assert b.get_attribute("data-tip")
    expect(w.locator('[data-hs="name"]')).to_have_text("baie 1 (n1)")
    w.locator('[name="custom_name"]').fill("baie 2")
    w.locator('[name="tags"]').fill("ssd, fast")
    w.locator('.hs-body button[type="submit"]').click()
    page.wait_for_timeout(600)
    assert sent[0] == ("api/host/harv-fake/n1/do/basics", {"custom_name": "baie 2"})
    assert sent[1] == ("api/host/harv-fake/n1/do/tags", {"tags": ["ssd", "fast"]})


def test_a_system_label_is_refused_before_sending(ui):
    page, sent, _, _ = ui
    w = open_host(page)
    w.locator('[data-hs="kv-add"]').click()
    w.locator("[data-hs-kv]").last.locator('[data-kv="key"]').fill("cpumanager")
    w.locator("[data-hs-kv]").last.locator('[data-kv="value"]').fill("true")
    w.locator('.hs-body button[type="submit"]').click()
    expect(w.locator(".of-msg .res-error")).to_be_visible()
    assert sent == []


def test_disks_add_with_format_and_change_tags(ui):
    page, sent, _, dialogs = ui
    w = open_host(page, "disks")
    expect(w.locator("form.hs-disk")).to_have_count(2)
    w.locator('form.hs-disk[data-disk="bd-sdb"] [data-hs-op="disk-add"]').click()
    page.wait_for_timeout(600)
    assert any("Formater /dev/sdb" in d for d in dialogs)
    assert sent[-1] == ("api/host/harv-fake/n1/do/disk-add", {"disk": "bd-sdb", "provisioner": "LonghornV1", "format": True})
    f = w.locator('form.hs-disk[data-disk="bd-sde"]')
    f.locator('[name="tags"]').fill("nvme")
    f.locator('[name="scheduling"]').uncheck()
    f.locator('[data-hs-op="disk-set"]').click()
    page.wait_for_timeout(600)
    assert sent[-1] == ("api/host/harv-fake/n1/do/disk-set", {"disk": "bd-sde", "tags": ["nvme"], "scheduling": False})


def test_power_needs_maintenance_and_the_password_stays_in_the_body(ui):
    page, sent, state, _ = ui
    w = open_host(page, "oob")
    expect(w.locator('[data-hs-power="reboot"]')).to_be_disabled()
    assert "maintenance" in w.locator('[data-hs-power="reboot"]').get_attribute("data-tip")
    state["settings"]["maintenance"] = "completed"                  # relu à la fin de l'enregistrement
    w.locator('[name="username"]').fill("admin")
    w.locator('[name="password"]').fill("bmc-secret")
    w.locator('[data-hs-op="oob"]').click()
    page.wait_for_timeout(600)
    path, body = sent[-1]
    assert path == "api/host/harv-fake/n1/do/oob" and body["password"] == "bmc-secret" and "secret" not in path
    expect(w.locator('[data-hs-power="reboot"]')).to_be_enabled(timeout=5000)
    expect(w.locator('[name="password"]')).to_have_value("")
    w.locator('[data-hs-power="reboot"]').click()
    page.wait_for_timeout(600)
    assert sent[-1] == ("api/host/harv-fake/n1/do/power", {"operation": "reboot"})


def test_cpu_manager_and_host_deletion_by_typed_name(ui):
    page, sent, _, _ = ui
    w = open_host(page, "actions")
    w.locator('[data-hs-form="cpu"] button[type="submit"]').click()
    page.wait_for_timeout(600)
    assert sent[-1] == ("api/host/harv-fake/n1/do/cpu-manager", {"enable": True})
    d = w.locator('[data-hs-form="delete"]')
    d.locator('[name="confirm"]').fill("n2")
    d.locator('button[type="submit"]').click()
    expect(d.locator(".of-msg .res-error")).to_be_visible()
    d.locator('[name="confirm"]').fill("n1")
    d.locator('button[type="submit"]').click()
    page.wait_for_timeout(600)
    assert sent[-1] == ("api/host/harv-fake/n1/do/delete", {})


def test_the_namespaces_window(ui):
    page, sent, _, _ = ui
    page.evaluate("App.setTab('namespaces')")
    btn = page.locator("#btn-vm-namespaces")
    expect(btn).to_be_visible(timeout=8000)
    assert btn.get_attribute("data-tip") or btn.get_attribute("title")
    btn.click()
    w = page.locator(".floating-panel", has=page.locator(".nsw-win")).last
    expect(w.locator("tbody tr")).to_have_count(1)                          # le système caché
    w.locator('[data-nsw="system"]').check()
    expect(w.locator("tbody tr")).to_have_count(2)
    expect(w.locator('tr[data-ns="kube-system"] [data-nsw-act="delete"]')).to_have_count(0)
    w.locator('[data-nsw="new"]').click()
    w.locator('.nsw-form [name="name"]').fill("team-b")
    w.locator('.nsw-form [name="description"]').fill("Équipe B")
    w.locator('.nsw-form button[type="submit"]').click()
    page.wait_for_timeout(600)
    assert sent[-1] == ("api/ns-admin/harv-fake", {"name": "team-b", "description": "Équipe B", "labels": {}})
    w.locator('tr[data-ns="team-a"] [data-nsw-act="edit"]').click()
    w.locator('.nsw-form [name="quota"]').fill("20")
    w.locator('.nsw-form button[type="submit"]').click()
    page.wait_for_timeout(800)
    assert sent[-2] == ("api/ns-admin/harv-fake/team-a/do/update",
                        {"description": "Équipe A", "labels": {"env": "prod"}, "annotations": {}})
    assert sent[-1] == ("api/ns-admin/harv-fake/team-a/do/quota", {"size": "20Gi"})
    w.locator('tr[data-ns="team-a"] [data-nsw-act="delete"]').click()
    expect(w.locator(".nsw-form")).to_contain_text("2")
    w.locator('.nsw-form [name="confirm"]').fill("team-a")
    w.locator('.nsw-form button[type="submit"]').click()
    page.wait_for_timeout(600)
    assert sent[-1] == ("api/ns-admin/harv-fake/team-a/do/delete", {})


EVENTS = {"items": [
    {"group": "hosts", "kind": "Node", "name": "n1", "namespace": "", "type": "Warning", "reason": "NodeNotReady",
     "message": "Node n1 status is now: NodeNotReady", "count": 1, "last": "2026-09-27T09:00:00Z", "first": "", "source": "kubelet"},
    {"group": "vms", "kind": "VirtualMachine", "name": "web", "namespace": "default", "type": "Normal", "reason": "Started",
     "message": "started", "count": 1, "last": "2026-09-27T08:00:00Z", "first": "", "source": "virt"}],
    "counts": {"hosts": 1, "vms": 1}, "warnings": {"hosts": 1}}
USAGE = {"cpu": {"used": 2, "reserved": 4, "total": 8, "live": True},
         "memory": {"used": 8 * GI, "reserved": 16 * GI, "total": 32 * GI, "live": True},
         "storage": {"used": 100 * GI, "scheduled": 300 * GI, "reserved": 0, "total": 1000 * GI,
                     "allocatable": 2000 * GI, "over_provisioning": 200}}


def test_the_events_board_and_the_usage_gauges(ui):
    page, _, _, _ = ui
    page.route("**/api/events/harv-fake", lambda r, q: fulfill(r, EVENTS))
    page.route("**/api/usage/harv-fake", lambda r, q: fulfill(r, USAGE))
    page.route("**/api/status/harv-fake", lambda r, q: fulfill(r, {"summary": {"nodes_total": 1, "nodes_ready": 1}, "nodes": []}))
    # 2.0.0 : les événements sont un bloc sous les métriques de l'aperçu
    page.evaluate("Router.go('#harvester/harv-fake/overview/events')")
    rows = page.locator(".ev-table tbody tr")
    expect(rows).to_have_count(2, timeout=8000)
    expect(page.locator('[data-ev-count="hosts"] .ev-warn')).to_contain_text("1")
    page.locator('[data-ev="warn"]').check()
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("NodeNotReady")
    page.locator('[data-ev="warn"]').uncheck()
    page.locator('[data-ev-group="vms"]').click()
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("web")
    page.locator('[data-ev="q"]').fill("nothing-matches")
    expect(page.locator(".ev-list .hint")).to_be_visible()
    # les jauges sont sur la même page, au-dessus des événements
    expect(page.locator('#tab-overview .overview-subtab[data-subtab="metrics"]')).to_be_visible()
    expect(page.locator("#overview-usage .usage-gauge")).to_have_count(3, timeout=8000)
    expect(page.locator("#overview-usage")).to_contain_text("25 %")               # 2 cœurs sur 8
    expect(page.locator("#overview-usage")).to_contain_text("200 %")             # sur-provisionnement


def test_switching_tabs_rereads_the_host(ui):
    """Vu sur harvlab : la fenêtre ne relisait l'hôte qu'après ses propres
    gestes et ignorait une mise en maintenance faite ailleurs."""
    page, _, state, _ = ui
    w = open_host(page, "oob")
    expect(w.locator('[data-hs-power="shutdown"]')).to_be_disabled()
    state["settings"]["maintenance"] = "completed"
    w.locator('[data-hs-tab="general"]').click()
    w.locator('[data-hs-tab="oob"]').click()
    expect(w.locator('[data-hs-power="shutdown"]')).to_be_enabled(timeout=5000)
    w.locator('[name="host"]').fill("10.0.0.99")                 # saisie en cours...
    state["settings"]["custom_name"] = "baie 9"
    page.evaluate("document.querySelector('.hs-win [data-hs-tab=\"oob\"]').click()")
    page.wait_for_timeout(800)
    expect(w.locator('[data-hs="name"]')).to_have_text("baie 9 (n1)")


def test_the_host_detail_shows_what_harvester_shows(ui):
    """v1.68.1 : Basics (jauges, NTP désynchronisé dit), Instances, Network,
    Events, lus par /detail."""
    page, _, _, _ = ui
    page.evaluate("HostSettings.open('harv-fake', 'n1', { tab: 'basics' })")
    w = host_win(page)
    body = w.locator('[data-hs="body"]')
    expect(body.locator(".hs-gauge")).to_have_count(3, timeout=8000)
    expect(body.locator(".sto-finding")).to_contain_text("pool.lan")               # NTP désynchronisé
    expect(body.locator(".hs-dl")).to_contain_text("USE6236RY1")
    expect(body.locator(".hs-gauge").nth(0)).to_contain_text("25 %")
    w.locator('[data-hs-tab="instances"]').click()
    expect(body.locator('[data-hs="open-vm"]')).to_have_text("web")
    w.locator('[data-hs-tab="network"]').click()
    expect(body).to_contain_text("data-all")
    expect(body).to_contain_text("mgmt-bo")
    w.locator('[data-hs-tab="events"]').click()
    expect(body).to_contain_text("node rebooted")
