"""v1.40.0 : Réseau et Stockage dans la grammaire de la Fabrique.

Même lecture que la Fabrique : un bloc par objet, de gauche à droite. Ce que
ces tests tiennent, et que les tests de source ne voient pas :

  * Réseau : chaque VM sous le réseau qu'elle utilise, avec la MAC et les
    adresses qu'elle a VRAIMENT, et par où ce réseau sort (bridge, bond,
    carte ; subnet et carte ; ou rien pour l'overlay) ;
  * Stockage : les volumes rangés par classe puis par VM, les disques des
    nœuds avec leur jauge, et une suppression réservée aux orphelins,
    derrière le verrou et une confirmation. Un volume monté par un pod
    n'est JAMAIS proposé à la suppression : c'est le défaut que cette
    version corrige.

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


def lk(name, typ, state="up", master=None, layer=0):
    return {"node": "n1.lo", "name": name, "type": typ, "state": state,
            "mac": "aa:bb", "index": 0, "master_index": None, "master": master,
            "master_unresolved": False, "layer": layer, "fabric": "classic"}


def nic(net, mac, ips=(), guest=None, link=None, pod=False):
    return {"nic": "nic-1" if not pod else "default", "network": net, "pod": pod,
            "model": "virtio", "binding": "masquerade" if pod else "bridge",
            "mac": mac, "ips": list(ips), "guest_iface": guest, "link_state": link}


FABRIC = {
    "cluster": "harv-fake", "full_linkmonitor": False,
    "nodes": [{"name": "n1.lo"}],
    "links": [lk("enp1s0", "device", master="mgmt-bo"), lk("eno2", "device", state="down"),
              lk("mgmt-bo", "bond", master="mgmt-br", layer=1), lk("mgmt-br", "bridge", layer=2)],
    "cluster_networks": [{"name": "mgmt"}], "vlan_configs": [],
    "networks": [
        {"namespace": "default", "name": "production", "cluster_network": "mgmt",
         "kind": "UntaggedNetwork", "ready": True, "bridge": "mgmt-br", "vlan": None,
         "provider": None, "fabric": "classic"},
        {"namespace": "default", "name": "idle-net", "cluster_network": "mgmt",
         "kind": "L2VlanNetwork", "ready": True, "bridge": "mgmt-br", "vlan": 7,
         "provider": None, "fabric": "classic"},
        {"namespace": "kube-system", "name": "external", "cluster_network": "mgmt",
         "kind": "OverlayNetwork", "ready": True, "bridge": None, "vlan": None,
         "provider": "external.kube-system.ovn", "fabric": "ovn"},
        {"namespace": "default", "name": "tenant", "cluster_network": "mgmt",
         "kind": "OverlayNetwork", "ready": True, "bridge": None, "vlan": None,
         "provider": "ovn", "fabric": "ovn"},
    ],
    "vms": [
        {"namespace": "default", "name": "idle", "status": "Stopped", "node": None,
         "networks": [nic("default/production", "02:00:00:00:00:09")], "guest_only": []},
        {"namespace": "default", "name": "web", "status": "Running", "node": "n1.lo",
         "networks": [nic("default/production", "ce:0f:db:1f:33:ee",
                          ["172.16.3.43", "fe80::1"], "eth0", "up")],
         "guest_only": [{"iface": "docker0", "mac": "ca:df", "ips": ["172.17.0.1"]}]},
        {"namespace": "default", "name": "egress", "status": "Running", "node": "n1.lo",
         "networks": [nic("kube-system/external", "0a:0a", ["172.16.0.9"], "eth0", "up")],
         "guest_only": []},
        {"namespace": "default", "name": "inner", "status": "Running", "node": "n1.lo",
         "networks": [nic("default/tenant", "0b:0b", ["10.54.0.7"], "eth0", "up")],
         "guest_only": []},
        {"namespace": "default", "name": "podvm", "status": "Running", "node": "n1.lo",
         "networks": [nic(None, "0c:0c", ["10.52.0.12"], "eth0", "up", pod=True)],
         "guest_only": []},
    ],
    "kubeovn": {
        "provider_networks": [{"name": "external", "default_interface": "eno2", "ready": True}],
        "vlans": [{"name": "ext-vlan", "id": 0, "provider_network": "external"}],
        "subnets": [
            {"name": "egress-external", "cidr": "172.16.0.0/22", "gateway": "172.16.0.1",
             "provider": "external.kube-system.ovn", "vlan": "ext-vlan", "overlay": False},
            {"name": "ovn-default", "cidr": "10.54.0.0/16", "gateway": "10.54.0.1",
             "provider": "ovn", "vlan": None, "overlay": True}],
        "vpcs": [],
    },
}


def volume(claim, sc, vm=None, disk=None, boot=None, state="detached", pods=(),
           orphan=False, last=(), device="disk", iso=False, size=20):
    return {"pvc_namespace": "default", "pvc_name": claim, "storage_class": sc,
            "phase": "Bound", "requested": size * GIB, "longhorn": "pvc-" + claim,
            "size": size * GIB, "actual_size": GIB, "state": state,
            "robustness": "healthy", "attached_to": "n1.lo" if state == "attached" else None,
            "replicas_wanted": 1,
            "replicas": [{"node": "n1.lo", "disk": "d1", "running": state == "attached"}],
            "image": "debian.iso" if iso else None, "image_iso": iso,
            "vm": vm, "disk": disk, "device": device, "boot_order": boot,
            "pods": [{"name": p} for p in pods], "last_pods": list(last), "orphan": orphan}


STORAGE = {
    "cluster": "harv-fake", "over_provisioning_pct": 200.0, "minimal_available_pct": 25.0,
    "schedulable_nodes": 1,
    "classes": [
        {"name": "harv-rep1", "provisioner": "driver.longhorn.io", "replicas": 1,
         "reclaim_policy": "Delete", "default": True, "image": None,
         "allocatable": 1100 * GIB, "reason": None},
        {"name": "three", "provisioner": "driver.longhorn.io", "replicas": 3,
         "reclaim_policy": "Delete", "default": False, "image": None,
         "allocatable": 0, "reason": "not enough schedulable nodes"},
        {"name": "unused-a", "provisioner": "driver.longhorn.io", "replicas": 1,
         "default": False, "image": "rocky9", "allocatable": 1100 * GIB},
        {"name": "unused-b", "provisioner": "driver.longhorn.io", "replicas": 1,
         "default": False, "image": None, "allocatable": 1100 * GIB},
    ],
    "disks": [{"node": "n1.lo", "disk": "d1", "path": "/var/lib/harvester/defaultdisk",
               "schedulable": True, "maximum": 1000 * GIB, "available": 750 * GIB,
               "used": 250 * GIB, "scheduled": 500 * GIB, "reserved": 0,
               "room": 400 * GIB, "limited_by": "free-space", "replicas": 5, "tags": []}],
    "volumes": [
        volume("web-data", "harv-rep1", vm="default/web", disk="data", boot=2, state="attached"),
        volume("web-root", "harv-rep1", vm="default/web", disk="root", boot=1, state="attached"),
        volume("web-iso", "harv-rep1", vm="default/web", disk="cd", device="cdrom", iso=True),
        volume("prom-db", "three", pods=["prometheus-0"], state="attached", size=50),
        volume("old-restore", "harv-rep1", orphan=True),
        volume("alertmanager-db", "three", orphan=True, size=5,
               last=[{"name": "alertmanager-0", "workload": "alertmanager",
                      "kind": "StatefulSet", "at": "2026-08-25T13:49:18Z"}]),
    ],
    "vms": [
        {"namespace": "default", "name": "web", "status": "Running", "node": "n1.lo",
         "disks": [{"disk": "root", "device": "disk", "boot_order": 1, "pvc": "web-root"},
                   {"disk": "data", "device": "disk", "boot_order": 2, "pvc": "web-data"}]},
        {"namespace": "default", "name": "installed", "status": "Stopped", "node": None,
         "disks": [{"disk": "cdrom1", "device": "cdrom", "boot_order": None, "pvc": None}]},
    ],
}


def open_view(page, base_url, sub, lang="en", deletes=None, delete_status=201):
    def fabric(route, req):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(FABRIC))

    def storage(route, req):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(STORAGE))

    def pvc(route, req):
        if deletes is not None:
            deletes.append((req.method, req.url))
        body = ({"action_id": "a1"} if delete_status == 201 else
                {"error": "claim-in-use", "detail": "PVC default/old-restore is mounted by pod x."})
        route.fulfill(status=delete_status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/network-fabric/**", fabric)
    page.route("**/api/storage-map/**", storage)
    page.route("**/api/pvc/**", pvc)
    page.context.add_init_script(
        f"localStorage.setItem('harvester_ops_language','{lang}');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','overview');")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    goto_board(page, sub)
    page.wait_for_selector(f'[data-board="{sub}"] .vsw', timeout=10000)
    page.wait_for_timeout(800)
    return page.locator(f'[data-board="{sub}"]')


def no_tooltip_missing(page, sub):
    return page.evaluate(f"""() => [...document.querySelectorAll(
        '[data-board="{sub}"] button, [data-board="{sub}"] [role=button], [data-board="{sub}"] label')]
        .filter(el => !el.getAttribute('data-tip'))
        .map(el => el.className + ' ' + el.textContent.trim().slice(0, 20))""")


# ---------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------

def test_network_is_one_block_per_network_in_html(context, flask_server):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    view = open_view(page, flask_server["base_url"], "network")
    ids = view.locator('.vsw-net[data-block]').evaluate_all("els => els.map(e => e.dataset.block)")
    assert ids == ["nad-default/production", "nad-kube-system/external",
                   "nad-default/tenant", "pod"], ids
    assert view.locator('canvas').count() == 0
    # Un réseau qui ne porte aucune VM n'a pas de bloc : il est listé en bas.
    assert "default/idle-net" in view.locator('.vsw-unused').inner_text()
    assert not errors, errors


def test_a_vm_shows_what_it_really_has_on_that_network(context, flask_server):
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "network")
    prod = view.locator('.vsw[data-block="nad-default/production"]')
    cards = prod.locator('.net-vm')
    # Démarrées d'abord : ce sont celles qui ont une adresse.
    assert "default/web" in cards.first.inner_text()
    row = cards.first.locator('.net-nic').inner_text()
    for fact in ("nic-1", "eth0", "ce:0f:db:1f:33:ee", "172.16.3.43", "fe80::1", "up"):
        assert fact in row, (fact, row)
    # docker0 n'est sur aucun réseau du cluster : il est dit à part.
    assert "docker0" in cards.first.locator('.net-guest-only').inner_text()
    assert "docker0" not in row


def test_a_classic_network_leaves_through_bridge_bond_and_card(context, flask_server):
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "network")
    right = view.locator('.vsw[data-block="nad-default/production"] .vsw-right').inner_text()
    assert right.index("mgmt-br") < right.index("mgmt-bo") < right.index("enp1s0")


def test_an_underlay_leaves_through_its_subnet_and_card(context, flask_server):
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "network")
    blk = view.locator('.vsw[data-block="nad-kube-system/external"]')
    right = blk.locator('.vsw-right').inner_text()
    assert "egress-external" in right and "172.16.0.0/22" in right and "eno2" in right
    # VLAN 0 = sans étiquette.
    assert "untagged" in blk.locator('.vsw-head').inner_text()


def test_the_overlay_and_the_pod_network_have_no_card(context, flask_server):
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "network")
    for bid in ("nad-default/tenant", "pod"):
        right = view.locator(f'.vsw[data-block="{bid}"] .vsw-right')
        assert right.locator('.vsw-internal').count() == 1
        assert right.locator('.vsw-nic').count() == 0


def test_the_switch_link_opens_the_fabric(context, flask_server):
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "network")
    view.locator('[data-netmap-fabric]').first.click()
    page.wait_for_timeout(1500)
    assert page.locator('[data-board="fabric"]').is_visible()


def test_network_controls_all_have_a_tooltip(context, flask_server):
    page = context.new_page()
    open_view(page, flask_server["base_url"], "network")
    assert not no_tooltip_missing(page, "network")


# ---------------------------------------------------------------------------
# Stockage
# ---------------------------------------------------------------------------

def sto_class(view, name):
    return view.locator(f'.sto-class[data-class="{name}"]')


def test_storage_reads_classes_left_engine_middle_disks_right(context, flask_server):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    view = open_view(page, flask_server["base_url"], "storage")
    blk = view.locator('.vsw-storage')
    assert blk.count() == 1
    left = blk.locator('.sto-class').first.bounding_box()
    spine = blk.locator('.vsw-spine').bounding_box()
    disk = blk.locator('.sto-disk').first.bounding_box()
    assert left["x"] + left["width"] <= spine["x"] + 1 <= disk["x"] + 1
    assert view.locator('canvas').count() == 0
    assert not errors, errors


def test_a_vm_lists_its_disks_in_boot_order(context, flask_server):
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "storage")
    rows = sto_class(view, "harv-rep1").locator('.sto-group').first.locator('.sto-vol')
    names = [t.split("\n")[0].strip() for t in rows.all_inner_texts()]
    assert names[0].startswith("root") and names[1].startswith("data"), names
    # Un disque d'ISO se dit CD-ROM.
    assert "CD-ROM" in rows.nth(2).inner_text()


def test_a_class_says_what_it_can_still_allocate(context, flask_server):
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "storage")
    assert "1.1 TiB" in sto_class(view, "harv-rep1").inner_text()
    three = sto_class(view, "three").inner_text()
    assert "not enough schedulable nodes" in three and "3 replicas" in three


def test_classes_without_volume_fold_away(context, flask_server):
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "storage")
    assert view.locator('.sto-class[data-class="unused-a"]').count() == 0
    view.locator('[data-sto-idle]').click()
    page.wait_for_timeout(300)
    assert "unused-a" in view.locator('.sto-idle').inner_text()


def test_the_disk_gauge_shows_written_and_promised(context, flask_server):
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "storage")
    disk = view.locator('.sto-disk').first
    used = disk.locator('.sto-bar-used').evaluate("e => e.style.width")
    sched = disk.locator('.sto-bar-sched').evaluate("e => e.style.left")
    assert (used, sched) == ("25%", "50%")
    assert "5 replicas" in disk.inner_text() and "400 GiB" in disk.inner_text()


def test_an_empty_cdrom_drive_is_still_inventoried(context, flask_server):
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "storage")
    assert "default/installed" in view.inner_text()


def test_a_volume_mounted_by_a_pod_is_never_offered(context, flask_server):
    """Le défaut corrigé : la base Prometheus, montée par un pod, passait
    pour « non rattachée » et se supprimait en deux clics."""
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "storage")
    view.locator('.sto-unlock').check()
    three = sto_class(view, "three")
    assert "Mounted by pods" in three.inner_text()
    three.locator('[data-vol="default/prom-db"]').click()
    page.wait_for_timeout(300)
    assert view.locator('[data-sto-delete]').count() == 0
    assert "prometheus-0" in view.locator('.fabric-detail').inner_text()


def test_an_orphan_is_deleted_only_unlocked_and_confirmed(context, flask_server):
    page = context.new_page()
    deletes = []
    view = open_view(page, flask_server["base_url"], "storage", deletes=deletes)
    view.locator('[data-vol="default/old-restore"]').click()
    page.wait_for_timeout(300)
    # Verrouillé : pas de bouton, une explication.
    assert view.locator('[data-sto-delete]').count() == 0
    assert "Unlock" in view.locator('.fabric-detail').inner_text()
    view.locator('.sto-unlock').check()
    page.wait_for_timeout(200)
    # Refuser la confirmation : rien ne part.
    page.once("dialog", lambda d: d.dismiss())
    view.locator('[data-sto-delete]').click()
    page.wait_for_timeout(300)
    assert deletes == []
    page.once("dialog", lambda d: d.accept())
    view.locator('[data-sto-delete]').click()
    page.wait_for_timeout(500)
    assert deletes and deletes[0][0] == "DELETE"
    assert deletes[0][1].endswith("/api/pvc/harv-fake/default/old-restore")


def test_a_server_refusal_is_shown_not_swallowed(context, flask_server):
    """Le serveur revérifie : s'il refuse, l'exploitant doit lire pourquoi."""
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "storage", deletes=[],
                     delete_status=409)
    view.locator('.sto-unlock').check()
    view.locator('[data-vol="default/old-restore"]').click()
    page.wait_for_timeout(300)
    page.once("dialog", lambda d: d.accept())
    view.locator('[data-sto-delete]').click()
    page.wait_for_timeout(500)
    assert "mounted by pod x" in view.locator('.sto-delete-out').inner_text()


def test_a_leftover_warns_that_its_workload_may_come_back(context, flask_server):
    page = context.new_page()
    view = open_view(page, flask_server["base_url"], "storage")
    view.locator('[data-vol="default/alertmanager-db"]').click()
    page.wait_for_timeout(300)
    side = view.locator('.fabric-detail').inner_text()
    assert "alertmanager" in side and "StatefulSet" in side


def test_storage_controls_all_have_a_tooltip(context, flask_server):
    page = context.new_page()
    open_view(page, flask_server["base_url"], "storage")
    assert not no_tooltip_missing(page, "storage")


def test_both_views_render_in_french(context, flask_server):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    view = open_view(page, flask_server["base_url"], "storage", lang="fr")
    assert "Moteur de stockage" in view.locator('.vsw-kind').first.text_content()
    view = open_view(page, flask_server["base_url"], "network", lang="fr")
    assert "Réseau" in view.locator('.vsw-kind').first.text_content()
    assert not errors, errors
