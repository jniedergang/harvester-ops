"""1.78.0 : le cadre Disques de la fenêtre d'installation bare-metal devient
un tableau rempli par l'inventaire du démarrage de découverte, avec un rôle
par disque (système, données, pool, effacer, ignorer), les pools groupés par
étiquette et leurs répliques, les refus du serveur rangés sur les lignes.

L'inventaire servi à la fenêtre est celui de la fixture de l'analyseur, passé
par le VRAI `parse_discovery`. La découverte BMC, le démarrage de découverte,
l'état de l'action et le lancement sont simulés ; l'import passe par la vraie
route (qui lit l'inventaire enregistré du serveur de test)."""

import json
import os
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import baremetal_disks  # noqa: E402
import bm_discover  # noqa: E402

RAW = (ROOT / "tests/api/fixtures/bm_disks_178/discovery.txt").read_text()
PARSED = baremetal_disks.parse_discovery(RAW)
INVENTORY = {"source": "discovery", "at": 1790000000, "system_serial": "SERIAL-0001",
             "disks": PARSED["disks"], "nics": PARSED["nics"]}
BY_NAME = {d["name"]: d for d in PARSED["disks"]}
PATH = {n: d["stable_path"] for n, d in BY_NAME.items()}

HOST = "192.0.2.41"
NODE = {"ok": True, "host": HOST, "model": "ProLiant XL170r Gen9", "power_state": "Off",
        "serial": "SN0041", "bios_version": "U14", "memory_gib": 128,
        "nics": [{"name": "System Ethernet Interface", "mac": n["mac"].upper(), "status": "Up",
                  "speed_mbps": 1000} for n in PARSED["nics"]],
        "uefi_targets": ["HD.Emb.1.1"], "virtualmedia_path": "/redfish/v1/Managers/1/VirtualMedia/2",
        "boot_targets": ["Cd", "Hdd"]}
STORAGE = {"source": "redfish", "supported": True, "controllers": [{
    "id": "HBA1", "name": "HBA330 Mini", "model": "HBA330", "raid_types": [], "drives": [{}, {}],
    "volumes": [{"id": "Disk.0", "name": "Disk 0", "volume_type": "RawDevice",
                 "capacity_bytes": 480 * 2 ** 30, "health": "OK"}]}]}
ISOS = {"isos": [{"name": "harvester-v1.9.0-amd64.iso", "size": 7 * 2 ** 30, "sha256": "ab" * 32}],
        "disk_free": 10 ** 11}
SHOT_DIR = os.environ.get("E2E_SHOT_DIR")


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def shot(page, name):
    if SHOT_DIR:
        Path(SHOT_DIR).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOT_DIR) / f"{name}.png"))


def open_window(context, flask_server, inventory=INVENTORY, node=None, state=None):
    """Fenêtre ouverte sur HOST. `state["inventory"]` peut changer en cours
    de test (démarrage de découverte) ; None = 404."""
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    state = state if state is not None else {}
    state.setdefault("inventory", inventory)
    state.setdefault("installs", [])

    def inv(route, req):
        if state["inventory"] is None:
            fulfill(route, {"error": "no inventory for this host"}, 404)
        else:
            fulfill(route, state["inventory"])

    def install(route, req):
        state["installs"].append(req.post_data_json)
        if state.get("install_refusal"):
            fulfill(route, state["install_refusal"], 400)
        else:
            fulfill(route, {"action_id": "bm0000000178", "hostname": "x"}, 202)
    page.route("**/api/isos", lambda r, q: fulfill(r, ISOS))
    page.route("**/api/bmc/discover", lambda r, q: fulfill(r, {"nodes": [node or NODE]}))
    page.route(f"**/api/baremetal/inventory/{HOST}", inv)
    page.route("**/api/baremetal/install", install)
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.BMC && window.FloatingPanels && window.i18n")
    page.on("dialog", lambda d: d.accept())
    page.click('.tab-group-head[data-group="automation"]')
    page.click('.tab-child[data-subtab="pxe"]')
    # menu latéral en calque au survol : écarter la souris
    page.mouse.move(1300, 450)
    form = page.locator("#bmc-discover-form")
    expect(form).to_be_visible()
    form.locator('[name="hosts"]').fill(HOST)
    form.locator('[name="password"]').fill("bmc-secret")
    form.locator('button[type="submit"]').click()
    page.locator(f'.bmc-install[data-host="{HOST}"]').click()
    win = page.locator(f'[id="fp-bm-install-{HOST}"]')
    expect(win.locator("#bm-install-form")).to_be_visible()
    return page, win, state


def row(win, name):
    return win.locator(f'tr.bm-disk-row[data-path="{PATH[name]}"]')


def notes(win, name):
    return win.locator(f'tr.bm-disk-row[data-path="{PATH[name]}"] + tr.bm-disk-notes')


def set_role(win, name, role, tag=None):
    row(win, name).locator("[data-disk-role]").select_option(role)
    if tag is not None:
        row(win, name).locator("[data-disk-tag]").fill(tag)


def fill_required(win):
    for name, v in (("ip", "192.0.2.10"), ("gateway", "192.0.2.1"), ("vip", "192.0.2.100"),
                    ("token", "t0k3n"), ("password", "pw")):
        win.locator(f'[name="{name}"]').fill(v)


def submit(win):
    win.locator('#bm-install-form button[type="submit"]').click()


def test_table_shows_the_inventory_with_its_badges(context, flask_server):
    node = dict(NODE, storage=STORAGE)
    page, win, _ = open_window(context, flask_server, node=node)
    expect(win.locator("tr.bm-disk-row")).to_have_count(len(PARSED["disks"]))
    expect(win.locator("#bm-inv-source")).to_contain_text("discovery boot")
    sdd = row(win, "sdd")
    expect(sdd).to_contain_text("1024 GiB")
    expect(sdd).to_contain_text("Samsung SSD 870")
    expect(sdd).to_contain_text("TEST-SSD-0001")
    expect(sdd).to_contain_text("SSD")
    expect(sdd).to_contain_text("3 (vfat, ext4, LVM2_member)")
    expect(sdd.locator(".bm-badge-data")).to_be_visible()
    expect(row(win, "nvme0n1")).to_contain_text("NVMe")
    expect(row(win, "sdc")).to_contain_text("HDD")
    # sans lien stable : le nom noyau est signalé
    expect(row(win, "sde").locator(".bm-badge-kernel")).to_be_visible()
    expect(row(win, "sdc").locator(".bm-badge-kernel")).to_have_count(0)
    expect(win.locator(".bm-badge-data")).to_have_count(1)
    # saisie libre disponible mais pas active
    expect(win.locator('[data-bm="free-text"]')).not_to_be_checked()
    expect(win.locator('[name="device"]')).to_be_hidden()
    # cartes : nom Linux et état du lien à côté de la MAC
    expect(win.locator(".bm-nics")).to_contain_text("enp3s0 · UP · 1000 Mbps")
    expect(win.locator(".bm-nics")).to_contain_text("enp4s0 · UP")
    # cadre RAID en lecture seule quand le BMC publie son stockage
    raid = win.locator("fieldset.bm-raid")
    expect(raid).to_contain_text("HBA330 Mini")
    expect(raid).to_contain_text("pass-through")
    expect(raid).to_contain_text("RAID 1")
    expect(win.locator(".bm-no-os")).to_contain_text("no system disk chosen")
    box = win.bounding_box()
    assert box["y"] + box["height"] <= page.viewport_size["height"]
    set_role(win, "nvme0n1", "os")
    set_role(win, "sdc", "data")
    set_role(win, "sdd", "pool", "fast")
    win.locator(".bm-disk-table").scroll_into_view_if_needed()
    shot(page, "bm-disks-table")


def test_roles_pools_and_the_body_sent(context, flask_server):
    page, win, state = open_window(context, flask_server)
    set_role(win, "sdb", "os")
    set_role(win, "nvme0n1", "os")
    # un seul disque système : le précédent revient à « ignorer »
    expect(row(win, "sdb").locator("[data-disk-role]")).to_have_value("ignore")
    set_role(win, "sdc", "data")
    set_role(win, "sdd", "pool", "fast")
    expect(notes(win, "sdd")).to_contain_text("carries data")
    row(win, "sdd").locator("[data-disk-wipe]").check()
    expect(notes(win, "sdd")).to_be_hidden()
    set_role(win, "sde", "pool", "slow")
    set_role(win, "vda", "wipe")
    expect(row(win, "vda").locator("[data-disk-wipe]")).to_be_checked()
    expect(row(win, "vda").locator("[data-disk-wipe]")).to_be_disabled()
    # étiquette refusée par le navigateur (le serveur revérifie)
    row(win, "sde").locator("[data-disk-tag]").fill("Slow_1")
    expect(notes(win, "sde")).to_contain_text("invalid pool tag")
    row(win, "sde").locator("[data-disk-tag]").fill("slow")
    expect(notes(win, "sde")).to_be_hidden()
    pools = win.locator("#bm-pools")
    expect(pools.locator(".bm-pool-line")).to_have_count(2)
    expect(pools).to_contain_text("fast: 1 disk(s), storage class longhorn-fast")
    pools.locator('[data-replicas="fast"]').fill("2")
    fill_required(win)
    submit(win)
    expect(win.locator("#bm-install-result")).to_contain_text("bm0000000178")
    body = state["installs"][-1]
    assert body["device"] == PATH["nvme0n1"]
    assert body["data_disk"] == PATH["sdc"]
    assert body["wipe_disks_list"] == [PATH["vda"], PATH["sdd"]]
    assert body["pools"] == [
        {"tag": "fast", "replicas": 2, "disks": [
            {"serial": "TEST-SSD-0001", "wwn": "0x5002538f00000001", "path": PATH["sdd"]}]},
        {"tag": "slow", "replicas": 1, "disks": [
            {"serial": "TEST-HDD-0001", "wwn": "", "path": PATH["sde"]}]}]
    assert body["cluster_name"] == "harvester-node1"
    assert body["wipe_all_disks"] is False
    assert not any(k.startswith("data-") for k in body)


def test_size_hint_and_skipchecks(context, flask_server):
    _, win, _ = open_window(context, flask_server)
    set_role(win, "sdb", "os")
    expect(notes(win, "sdb")).to_contain_text("too small, 250 GiB at least")
    set_role(win, "vda", "data")
    expect(notes(win, "vda")).to_contain_text("too small, 50 GiB at least")
    expect(notes(win, "sdb")).to_contain_text("too small, 180 GiB at least")
    win.locator('[name="extra_args"]').fill("harvester.install.skipchecks=true")
    expect(notes(win, "sdb")).to_be_hidden()
    expect(win.locator("#bm-pools")).to_contain_text("size checks are lifted")


def test_server_refusals_land_on_the_rows(context, flask_server):
    page, win, state = open_window(context, flask_server)
    state["install_refusal"] = {
        "error": "invalid disks", "fields": ["device", "pools"],
        "reasons": [[PATH["sdb"], "too-small:250"], ["serial TEST-SAS-0001", "role-twice"],
                    [PATH["sdd"], "has-data"]]}
    set_role(win, "sdb", "os")
    set_role(win, "sdc", "pool", "hdd")
    fill_required(win)
    submit(win)
    msg = win.locator("#bm-config-msg .bm-refusal")
    expect(msg).to_contain_text("Disks refused:")
    expect(msg).to_contain_text("too small, 250 GiB at least")
    expect(msg).to_contain_text("already used in another role")
    expect(msg).to_contain_text("carries data")
    expect(notes(win, "sdb").locator(".bm-disk-refused")).to_contain_text("too small, 250 GiB")
    expect(notes(win, "sdc").locator(".bm-disk-refused")).to_contain_text("already used in another role")
    expect(row(win, "sdc")).to_have_class(re.compile(r"\bbm-disk-bad\b"))
    # changer le rôle efface le refus de cette ligne
    set_role(win, "sdc", "ignore")
    expect(notes(win, "sdc")).to_be_hidden()
    shot(page, "bm-disks-refused")


def test_an_install_without_system_disk_does_not_leave(context, flask_server):
    _, win, state = open_window(context, flask_server)
    fill_required(win)
    submit(win)
    expect(win.locator("#bm-install-result")).to_contain_text("no system disk chosen")
    assert state["installs"] == []


def test_discovery_boot_fills_the_table(context, flask_server):
    state = {"inventory": None, "polls": 0, "discovers": []}
    page, win, state = open_window(context, flask_server, state=state)
    expect(win.locator("tr.bm-disk-row")).to_have_count(0)
    expect(win.locator("#bm-inv-source")).to_contain_text("No inventory")

    def discover(route, req):
        state["discovers"].append(req.post_data_json)
        fulfill(route, {"action_id": "disc00000178", "host": HOST}, 202)

    def action(route, req):
        state["polls"] += 1
        if state["polls"] < 2:
            fulfill(route, {"id": "disc00000178", "status": "running"})
        else:
            state["inventory"] = INVENTORY
            fulfill(route, {"id": "disc00000178", "status": "done", "exit_code": 0})
    page.route("**/api/baremetal/discover", discover)
    page.route("**/api/action/disc00000178", action)
    win.locator('[data-bm="discover-boot"]').click()
    expect(win.locator("#bm-disc-status")).to_contain_text("disc00000178")
    expect(win.locator('[data-bm="discover-boot"]')).to_be_disabled()
    expect(win.locator("tr.bm-disk-row")).to_have_count(len(PARSED["disks"]), timeout=15000)
    expect(win.locator("#bm-disc-status")).to_contain_text("Inventory received.")
    expect(win.locator('[data-bm="discover-boot"]')).to_be_enabled()
    body = state["discovers"][0]
    assert body == {"bmc_host": HOST, "bmc_user": "admin", "bmc_password": "bmc-secret",
                    "iso": "harvester-v1.9.0-amd64.iso"}
    # le tableau remplace la saisie libre
    expect(win.locator('[data-bm="free-text"]')).not_to_be_checked()


def test_a_failed_discovery_says_so(context, flask_server):
    state = {"inventory": None}
    page, win, state = open_window(context, flask_server, state=state)
    page.route("**/api/baremetal/discover",
               lambda r, q: fulfill(r, {"action_id": "disc00000179", "host": HOST}, 202))
    page.route("**/api/action/disc00000179", lambda r, q: fulfill(r, {
        "id": "disc00000179", "status": "error", "error_summary": "no inventory within 15 min"}))
    win.locator('[data-bm="discover-boot"]').click()
    expect(win.locator("#bm-disc-status")).to_contain_text("ended with status error", timeout=10000)
    expect(win.locator("#bm-disc-status")).to_contain_text("no inventory within 15 min")
    expect(win.locator("tr.bm-disk-row")).to_have_count(0)


def test_free_text_without_inventory(context, flask_server):
    _, win, state = open_window(context, flask_server, inventory=None)
    free = win.locator('[data-bm="free-text"]')
    expect(free).to_be_checked()
    expect(free).to_be_disabled()
    expect(win.locator('[name="device"]')).to_be_visible()
    expect(win.locator("#bm-disks")).to_contain_text("typed in by hand")
    win.locator('[name="device"]').fill("/dev/disk/by-id/wwn-0x5000c500a0000009")
    fill_required(win)
    submit(win)
    expect(win.locator("#bm-install-result")).to_contain_text("bm0000000178")
    body = state["installs"][-1]
    assert body["device"] == "/dev/disk/by-id/wwn-0x5000c500a0000009"
    assert "pools" not in body and "wipe_disks_list" not in body


def test_free_text_takes_over_the_table_choice(context, flask_server):
    _, win, state = open_window(context, flask_server)
    set_role(win, "nvme0n1", "os")
    set_role(win, "sdc", "data")
    win.locator('[data-bm="free-text"]').check()
    assert win.locator('[name="device"]').input_value() == PATH["nvme0n1"]
    assert win.locator('[name="data_disk"]').input_value() == PATH["sdc"]
    expect(row(win, "nvme0n1").locator('[data-disk-role] option[value="os"]')).to_be_disabled()
    win.locator('[name="device"]').fill("/dev/nvme0n1")
    win.locator('[data-bm="free-text"]').uncheck()
    # un nom connu de l'inventaire retrouve sa ligne
    expect(row(win, "nvme0n1").locator("[data-disk-role]")).to_have_value("os")
    fill_required(win)
    submit(win)
    expect(win.locator("#bm-install-result")).to_contain_text("bm0000000178")
    assert state["installs"][-1]["device"] == PATH["nvme0n1"]


def _store_inventory(test_config):
    bm_discover.store_inventory(test_config["root"] / "inventory", "SERIAL-0041", HOST, RAW)


def _file(extra):
    return ("token: t0k3n\nos:\n  hostname: hv-imp\n  password: pw\n"
            "install:\n  mode: create\n  vip: 192.0.2.100\n  vip_mode: static\n"
            "  management_interface:\n    method: dhcp\n    interfaces:\n    - name: eth0\n" + extra)


def test_import_places_the_disks_and_the_wipe_list(context, flask_server, test_config, tmp_path):
    _store_inventory(test_config)
    _, win, state = open_window(context, flask_server)
    f = tmp_path / "hv.yaml"
    f.write_text(_file(f"  device: /dev/nvme0n1\n  data_disk: {PATH['sdc']}\n"
                       f"  wipe_disks_list:\n  - {PATH['vda']}\n  - {PATH['sdd']}\n"))
    win.locator('[data-bm="file"]').set_input_files(str(f))
    expect(win.locator(".bm-imported")).to_be_visible()
    expect(row(win, "nvme0n1").locator("[data-disk-role]")).to_have_value("os")
    expect(row(win, "sdc").locator("[data-disk-role]")).to_have_value("data")
    expect(row(win, "vda").locator("[data-disk-role]")).to_have_value("wipe")
    expect(row(win, "sdd").locator("[data-disk-role]")).to_have_value("wipe")
    assert "wipe_disks_list" not in win.locator('[name="advanced_yaml"]').input_value()
    # nom du cluster = nom d'hôte du fichier
    assert win.locator('[name="cluster_name"]').input_value() == "hv-imp"
    submit(win)
    body = state["installs"][-1]
    assert body["device"] == PATH["nvme0n1"] and body["data_disk"] == PATH["sdc"]
    assert body["wipe_disks_list"] == [PATH["vda"], PATH["sdd"]]


def test_an_unknown_wipe_path_stays_in_the_advanced_yaml(context, flask_server, test_config, tmp_path):
    _store_inventory(test_config)
    _, win, _ = open_window(context, flask_server)
    f = tmp_path / "hv.yaml"
    f.write_text(_file(f"  device: /dev/nvme0n1\n  wipe_disks_list:\n  - {PATH['vda']}\n"
                       "  - /dev/disk/by-id/wwn-0x0000000000000bad\n"))
    win.locator('[data-bm="file"]').set_input_files(str(f))
    expect(win.locator("#bm-config-msg")).to_contain_text("it stays in the advanced YAML")
    assert "wwn-0x0000000000000bad" in win.locator('[name="advanced_yaml"]').input_value()
    expect(row(win, "vda").locator("[data-disk-role]")).to_have_value("ignore")


def test_join_mode_hides_the_cluster_name(context, flask_server, tmp_path):
    _, win, state = open_window(context, flask_server)
    expect(win.locator('[name="cluster_name"]')).to_be_visible()
    win.locator('[name="hostname"]').fill("hv-node-9")
    assert win.locator('[name="cluster_name"]').input_value() == "hv-node-9"
    f = tmp_path / "join.yaml"
    f.write_text("server_url: https://192.0.2.100:443\ntoken: t0k3n\n"
                 "os:\n  hostname: hv-join\n  password: pw\n"
                 "install:\n  mode: join\n  device: /dev/sda\n"
                 "  management_interface:\n    method: dhcp\n    interfaces:\n    - name: eth0\n")
    win.locator('[data-bm="file"]').set_input_files(str(f))
    expect(win.locator(".bm-imported")).to_be_visible()
    expect(win.locator('[name="cluster_name"]')).to_be_hidden()
    expect(win.locator('[name="vip"]')).to_be_hidden()
    # /dev/sda est dans l'inventaire (disque de 10 Gio) : pris dans le tableau
    expect(row(win, "sda").locator("[data-disk-role]")).to_have_value("os")
    submit(win)
    body = state["installs"][-1]
    assert body["mode"] == "join" and "cluster_name" not in body
    assert body["device"] == PATH["sda"]


# --- round 2 : stockage lu par le BMC, refus du serveur par chemin ----------

IDRAC_STORAGE = {"source": "redfish", "supported": True, "controllers": [{
    "id": "RAID.Integrated.1-1", "name": "PERC H730P Mini", "model": "PERC H730P Mini",
    "raid_types": ["RAID0", "RAID1"], "can_create_volume": True,
    "drives": [{"id": "Disk.Bay.0", "model": "MZ7LH480HAHQ", "media": "SSD", "protocol": "SATA",
                "capacity_bytes": 480 * 10 ** 9, "serial": "TEST-BMC-0001"},
               {"id": "Disk.Bay.1", "model": "PM1725b", "media": "SSD", "protocol": "NVMe",
                "capacity_bytes": 1600 * 10 ** 9, "serial": "TEST-BMC-0002"}],
    "volumes": [{"id": "Disk.Virtual.0", "name": "os-mirror", "raid_type": "RAID1",
                 "volume_type": "Mirrored", "capacity_bytes": 480 * 10 ** 9, "health": "OK"}]}]}


def test_read_the_disks_from_the_bmc(context, flask_server):
    page, win, state = open_window(context, flask_server, inventory=None)
    calls = []

    def storage(route, req):
        calls.append(req.post_data_json)
        fulfill(route, IDRAC_STORAGE)
    page.route("**/api/bmc/storage", storage)
    win.locator('[data-bm="read-bmc"]').click()
    expect(win.locator("#bm-disc-status")).to_contain_text("2 disk(s) listed by the BMC.")
    assert calls == [{"host": HOST, "user": "admin", "password": "bmc-secret"}]
    rows = win.locator("tr.bm-redfish-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(1)).to_contain_text("NVMe")
    expect(rows.nth(0)).to_contain_text("TEST-BMC-0001")
    expect(rows.nth(0).locator(".bm-disk-path")).to_have_text("")
    expect(win.locator("#bm-inv-source")).to_contain_text("Redfish (BMC)")
    expect(win.locator(".bm-redfish-only")).to_contain_text("run a discovery boot")
    # pas de chemin Linux : saisie libre imposée, aucun rôle proposé
    expect(win.locator('[data-bm="free-text"]')).to_be_checked()
    expect(win.locator("[data-disk-role]")).to_have_count(0)
    raid = win.locator("#bm-raid-box fieldset.bm-raid")
    expect(raid).to_contain_text("PERC H730P Mini")
    expect(raid.locator(".badge")).to_have_text("RAID")
    expect(raid).to_contain_text("os-mirror")
    expect(raid).to_contain_text("mirrored volume (RAID 1)")
    raid.scroll_into_view_if_needed()
    shot(page, "bm-disks-redfish")


def test_a_bmc_without_storage_points_to_the_discovery_boot(context, flask_server):
    page, win, _ = open_window(context, flask_server, inventory=None)
    page.route("**/api/bmc/storage", lambda r, q: fulfill(
        r, {"source": None, "supported": False, "controllers": []}))
    win.locator('[data-bm="read-bmc"]').click()
    expect(win.locator("#bm-disc-status")).to_contain_text("publishes no storage controller")
    expect(win.locator("#bm-disks")).to_contain_text("run a discovery boot")
    expect(win.locator("#bm-raid-box fieldset")).to_have_count(0)


def test_invalid_disks_by_path_land_on_the_rows(context, flask_server):
    _, win, state = open_window(context, flask_server)
    state["install_refusal"] = {"error": "invalid disks", "fields": [PATH["sdb"], ""],
                                "reasons": {PATH["sdb"]: "too-small:250", "": "no-os"}}
    set_role(win, "sdb", "os")
    fill_required(win)
    submit(win)
    msg = win.locator("#bm-config-msg .bm-refusal")
    expect(msg).to_contain_text("too small, 250 GiB at least")
    expect(msg).to_contain_text("no system disk chosen")
    expect(notes(win, "sdb").locator(".bm-disk-refused")).to_contain_text("too small, 250 GiB")


def test_the_preview_checks_the_disks_on_the_server(context, flask_server, test_config):
    # vraie route d'aperçu, avec l'inventaire enregistré du serveur de test
    _store_inventory(test_config)
    _, win, _ = open_window(context, flask_server)
    set_role(win, "sdb", "os")
    win.locator('[data-bm="preview"]').click()
    msg = win.locator("#bm-config-msg .bm-refusal")
    expect(msg).to_contain_text("Disks refused:")
    expect(msg).to_contain_text("too small, 250 GiB at least")
    expect(notes(win, "sdb").locator(".bm-disk-refused")).to_be_visible()


# --- round 3 ----------------------------------------------------------------

def test_wipe_on_the_system_disk_is_sent(context, flask_server):
    _, win, state = open_window(context, flask_server)
    set_role(win, "sdd", "os")
    expect(notes(win, "sdd")).to_contain_text("carries data")
    row(win, "sdd").locator("[data-disk-wipe]").check()
    expect(notes(win, "sdd")).to_be_hidden()
    fill_required(win)
    submit(win)
    expect(win.locator("#bm-install-result")).to_contain_text("bm0000000178")
    body = state["installs"][-1]
    assert body["device"] == PATH["sdd"]
    # envoyé pour lever `has-data` ; le serveur le retire du YAML rendu
    assert body["wipe_disks_list"] == [PATH["sdd"]]
    assert body["wipe_all_disks"] is False


def test_discovery_polling_gives_up_after_five_failures(context, flask_server):
    state = {"inventory": None, "polls": 0}
    page, win, state = open_window(context, flask_server, state=state)
    page.route("**/api/baremetal/discover",
               lambda r, q: fulfill(r, {"action_id": "disc00000180", "host": HOST}, 202))

    def action(route, req):
        state["polls"] += 1
        fulfill(route, {"error": "not found"}, 404)
    page.route("**/api/action/disc00000180", action)
    win.locator('[data-bm="discover-boot"]').click()
    expect(win.locator("#bm-config-msg")).to_contain_text(
        "Stopped following action disc00000180", timeout=25000)
    expect(win.locator("#bm-disc-status")).to_contain_text("Stopped following")
    expect(win.locator('[data-bm="discover-boot"]')).to_be_enabled()
    polls = state["polls"]
    assert polls == 5
    page.wait_for_timeout(3500)
    assert state["polls"] == 5
