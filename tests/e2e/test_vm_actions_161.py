"""v1.61.0 : les gestes à chaud du menu d'une VM et la liste des VMs, dans un navigateur.

Le menu suit l'état lu (/state) : CPU et mémoire quand le branchement à chaud
est activé, insérer une image dans un lecteur SATA vide, l'éjecter d'un
lecteur plein, brancher ou débrancher une carte, migrer un volume ou annuler.
Les fenêtres envoient ce que l'outil attend ; un mot de passe d'accès ne part
que dans le corps de la requête. La liste a ses colonnes et son filtre.
"""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto  # noqa: E402

VMS = {"vms": [
    {"namespace": "default", "name": "web", "phase": "Running", "runStrategy": "RerunOnFailure", "agent_connected": "True",
     "priority": 0, "cpu": 2, "memory": "4Gi", "node": "n1", "ips": ["10.0.0.5", "10.0.1.5"], "labels": {"app": "shop"}},
    {"namespace": "default", "name": "db", "phase": "Running", "runStrategy": "RerunOnFailure", "agent_connected": "True",
     "priority": 0, "cpu": 8, "memory": "16Gi", "node": "n2", "ips": ["10.0.0.9"], "labels": {"app": "erp"}}]}
STATE = {"running": True, "paused": False, "agent": True, "migrating": False, "node": "n1", "targets": ["n2"],
         "run_strategy": "RerunOnFailure", "cloudinit_secrets": [], "restart_required": False, "storage_migration": None,
         "quota": 10 * 2**30,
         "cpumem": {"enabled": True, "sockets": 2, "max_sockets": 8, "memory": "4Gi", "max_memory": "16Gi"},
         "sata_cdroms": [{"name": "cd1", "empty": True}, {"name": "cd2", "empty": False}],
         "interfaces": [{"name": "default", "network": "pod", "bridge": False, "unpluggable": False},
                        {"name": "nic-2", "network": "default/vlan20", "bridge": True, "unpluggable": True}],
         "volumes": [{"volume": "root", "claim": "web-root", "kind": "disk", "source": "pvc", "hotpluggable": False},
                     {"volume": "cd2", "claim": "web-cd2-x", "kind": "cdrom", "source": "pvc", "hotpluggable": True}]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','fr');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    sent = []

    def writes(route, req):
        sent.append((req.method, req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "vma000000161"}, 202)
    page.route("**/api/vms/harv-fake", lambda r, q: fulfill(r, VMS))
    page.route("**/api/namespaces/harv-fake", lambda r, q: fulfill(r, [{"name": "default"}]))
    page.route("**/api/vm/harv-fake/default/web/state", lambda r, q: fulfill(r, STATE))
    page.route("**/api/vm/harv-fake/default/web/do/**", writes)
    page.route("**/api/cluster-objects/harv-fake/images", lambda r, q: fulfill(r, {"items": [
        {"namespace": "default", "name": "debian", "display_name": "debian-13.iso", "state": "ready"},
        {"namespace": "default", "name": "leap", "display_name": "leap.qcow2", "state": "ready"}]}))
    page.route("**/api/networks/harv-fake", lambda r, q: fulfill(r, [
        {"namespace": "default", "name": "vlan20", "vlan": "20", "config": json.dumps({"type": "bridge"})},
        {"namespace": "default", "name": "ovn", "config": json.dumps({"type": "kube-ovn"})},
        {"namespace": "harvester-system", "name": "storagenet", "config": json.dumps({"type": "bridge"})}]))
    page.route("**/api/pvcs/harv-fake**", lambda r, q: fulfill(r, [
        {"name": "web-root-fast", "namespace": "default", "phase": "Bound", "capacity": "40Gi", "storage_class": "fast"},
        {"name": "web-root", "namespace": "default", "phase": "Bound", "capacity": "40Gi"}]))
    page.route("**/api/sshkeys/harv-fake", lambda r, q: fulfill(r, [{"name": "ops", "namespace": "default"}]))
    page.route("**/api/stream/vma000000161", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address('namespaces'))
    page.wait_for_function("window.VMActions && window.App && App.getCurrentCluster()")
    page.evaluate("App.setTab('namespaces')")
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    expect(page.locator("#ns-vms-table [data-vm-more]").first).to_be_visible(timeout=8000)
    return page, sent, dialogs


def open_menu(page, vm="web"):
    page.locator("#ns-vms-table tr", has_text=vm).first.locator("[data-vm-more]").click()
    expect(page.locator(".vma-menu .vma-item").first).to_be_visible(timeout=5000)
    return page.locator(".vma-menu")


def win(page):
    return page.locator(".floating-panel", has=page.locator(".vma-form")).last


def test_the_list_has_harvester_columns_and_a_label_filter(ui):
    page, _, _ = ui
    row = page.locator("#ns-vms-table tr", has_text="web").first
    cells = row.locator("td").all_inner_texts()
    assert "2" in cells and "4Gi" in cells and "n1" in cells
    assert any("10.0.0.5" in c and "+1" in c for c in cells)
    f = page.locator("#ns-vm-filter")
    assert f.get_attribute("data-tip")
    f.fill("app=erp")
    expect(page.locator("#ns-vms-table tbody tr")).to_have_count(1)
    expect(page.locator("#ns-vms-table tbody tr")).to_contain_text("db")
    f.fill("10.0.0.5")
    expect(page.locator("#ns-vms-table tbody tr")).to_contain_text("web")
    f.fill("")
    page.locator('#ns-vms-table th[data-sort-by="cpu"]').click()
    first = page.locator("#ns-vms-table tbody tr").first
    expect(first).to_contain_text("web")                      # 2 vCPU avant 8


def test_the_menu_offers_the_hot_actions_of_the_state(ui):
    page, _, _ = ui
    m = open_menu(page)
    expect(m.locator('[data-vma="cpumem"]')).to_be_visible()
    expect(m.locator('[data-vma="insert-cdrom"][data-volume="cd1"]')).to_be_visible()
    expect(m.locator('[data-vma="eject-image"][data-volume="cd2"]')).to_be_visible()
    expect(m.locator('[data-vma="eject"]')).to_have_count(0)            # le lecteur SATA s'éjecte à chaud
    expect(m.locator('[data-vma="remove-volume"]')).to_have_count(0)    # cd2 est un lecteur, pas un disque
    expect(m.locator('[data-vma="remove-nic"][data-volume="nic-2"]')).to_be_visible()
    expect(m.locator('[data-vma="remove-nic"][data-volume="default"]')).to_have_count(0)
    for act in ("add-nic", "storage-migrate", "schedule", "quota", "access", "serial", "logs"):
        expect(m.locator(f'[data-vma="{act}"]')).to_be_visible()
    expect(m.locator('[data-vma="quota"]')).to_contain_text("10 Gi")


def test_cpu_memory_window_sends_sockets_and_memory(ui):
    page, sent, _ = ui
    open_menu(page).locator('[data-vma="cpumem"]').click()
    w = win(page)
    expect(w.locator('[name="cpu"]')).to_have_attribute("max", "8")
    w.locator('[name="cpu"]').fill("4")
    w.locator('[name="memory"]').fill("8")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("POST", "api/vm/harv-fake/default/web/do/cpumem", {"cpu": 4, "memory": "8Gi"})


def test_insert_offers_iso_images_first(ui):
    page, sent, _ = ui
    open_menu(page).locator('[data-vma="insert-cdrom"]').click()
    w = win(page)
    expect(w.locator('[name="image"] option')).to_have_count(1)
    expect(w.locator('[name="image"] option')).to_have_text("debian-13.iso")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][2] == {"volume": "cd1", "image": "default/debian"}


def test_a_nic_only_on_bridge_networks_outside_the_system(ui):
    page, sent, _ = ui
    open_menu(page).locator('[data-vma="add-nic"]').click()
    w = win(page)
    expect(w.locator('[name="network"] option')).to_have_count(1)         # ni overlay ni réseau système
    expect(w.locator('[name="iface"]')).to_have_value("nic-3")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][2] == {"iface": "nic-3", "network": "default/vlan20"}


def test_storage_migration_targets_a_free_volume(ui):
    page, sent, _ = ui
    open_menu(page).locator('[data-vma="storage-migrate"]').click()
    w = win(page)
    expect(w.locator('[name="target"] option')).to_have_count(1)          # web-root est déjà à la VM
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][2] == {"volume": "web-root", "target": "web-root-fast"}


def test_an_access_password_goes_in_the_body_only(ui):
    page, sent, _ = ui
    open_menu(page).locator('[data-vma="access"]').click()
    w = win(page)
    w.locator('[name="users"]').fill("root")
    w.locator('[name="password"]').fill("a-secret-pw")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    method, path, body = sent[-1]
    assert path == "api/vm/harv-fake/default/web/do/access" and "a-secret-pw" not in path
    assert body == {"kind": "basic", "users": ["root"], "password": "a-secret-pw"}
    open_menu(page).locator('[data-vma="access"]').click()
    w = win(page)
    w.locator('[name="kind"]').select_option("ssh")
    expect(w.locator('[name="password"]')).to_be_hidden()
    w.locator('[name="users"]').fill("ops, admin")
    w.locator('[name="keys"]').select_option(["default/ops"])
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][2] == {"kind": "ssh", "users": ["ops", "admin"], "keys": ["default/ops"]}
