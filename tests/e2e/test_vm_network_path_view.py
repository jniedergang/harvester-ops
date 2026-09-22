"""v1.36.0 : l'onglet « Chemin de connexion », dans un vrai navigateur.

On se met dans la peau d'un exploitant qui VÉRIFIE : la VM sort-elle par le
bon réseau et la bonne carte ? Ce que les tests de source ne voient pas :
que la chaîne se dessine dans le bon ordre, que les deux outils ne partent
QUE sur demande, et qu'une VM arrêtée est annoncée comme telle au lieu de
laisser croire à une panne.

Le réseau est intercepté : aucun test ne touche à un cluster.
"""

import json

import pytest

playwright = pytest.importorskip("playwright")


def path_payload(live=True):
    chain = [
        {"name": "mgmt-br", "type": "bridge", "state": "up", "mac": "dd:ee",
         "layer": 2, "fabric": "classic", "known": True},
        {"name": "mgmt-bo", "type": "bond", "state": "up", "mac": "aa:bb",
         "layer": 1, "fabric": "classic", "known": True},
        {"name": "enp1s0", "type": "device", "state": "up", "mac": "aa:bb",
         "layer": 0, "fabric": "classic", "known": True},
    ]
    return {
        "cluster": "harv-fake", "namespace": "default", "name": "vm1",
        "node": "n1.lo", "running": live, "chain_is_live": live,
        "nodes": ["n1.lo"], "full_linkmonitor": True,
        "nics": [{
            "name": "nic-1",
            "declared": {"mac": "b6:c3:e4:bb:40:b7", "model": "virtio",
                         "binding": "bridge", "network": "default/production",
                         "pod_network": False},
            "live": {"guest_interface": "eth0" if live else None,
                     "mac": "b6:c3:e4:bb:40:b7" if live else None,
                     "ip": "172.16.3.2" if live else None,
                     "ips": [], "link_state": "up" if live else None,
                     "host_port": "pod8fe0" if live else None,
                     "info_source": "guest-agent"},
            "mac_matches": True,
            "network_detail": {"kind": "UntaggedNetwork", "cni": "bridge",
                               "bridge": "mgmt-br", "vlan": None,
                               "ready": True, "fabric": "classic"},
            "chain": chain,
        }],
    }


# L'éditeur charge d'abord la VM elle-même : sans elle la section Network
# ne rend rien du tout, et l'onglet qu'on veut tester n'existe pas.
CANNED_VM = {
    "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
    "metadata": {"name": "vm1", "namespace": "default", "labels": {},
                 "annotations": {}},
    "spec": {"runStrategy": "Always", "template": {
        "metadata": {"labels": {}, "annotations": {}},
        "spec": {
            "domain": {"cpu": {"cores": 2}, "memory": {"guest": "4Gi"},
                       "resources": {"limits": {"memory": "4Gi"}},
                       "devices": {"disks": [], "interfaces": [
                           {"name": "nic-1", "bridge": {},
                            "macAddress": "b6:c3:e4:bb:40:b7",
                            "model": "virtio"}]}},
            "networks": [{"name": "nic-1",
                          "multus": {"networkName": "default/production"}}],
            "volumes": [],
        }}},
}


def open_path_tab(page, base_url, live=True):
    page.route("**/api/vm-network-path/*/*/*", lambda r, q: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(path_payload(live))))
    page.route("**/api/vm/*/*/*", lambda r, q: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(CANNED_VM)))
    page.route("**/api/vms/*", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','namespaces');")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1800)
    page.evaluate("() => window.VMEdit.open('harv-fake','default','vm1')")
    page.wait_for_timeout(2500)
    nav = page.locator('.vm-edit-nav button[data-section="network"]')
    if not nav.count():
        pytest.skip("l'éditeur de VM ne s'est pas ouvert sur ce harnais")
    nav.first.click()
    page.wait_for_timeout(800)
    page.locator('[data-net-tab="path"]').first.click()
    page.wait_for_timeout(1800)


def chain_titles(page):
    return [t.split("\n")[0] for t in
            page.locator('.netpath-box').all_inner_texts()]


def test_the_network_section_has_two_tabs(context, flask_server):
    """On ÉDITE les interfaces dans l'un, on VÉRIFIE par où elles sortent
    dans l'autre. Le second ne se lit dans aucun formulaire."""
    page = context.new_page()
    open_path_tab(page, flask_server["base_url"])
    assert page.locator('[data-net-tab]').count() == 2


def test_the_chain_reads_from_the_vm_down_to_the_card(context, flask_server):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    open_path_tab(page, flask_server["base_url"])
    titles = chain_titles(page)
    assert titles == ["default/vm1", "nic-1", "default/production",
                      "mgmt-br", "mgmt-bo", "enp1s0"], titles
    assert not errors, errors


def test_the_live_facts_are_shown_on_the_interface(context, flask_server):
    """IP et état du lien sont ce qu'on vient vérifier en premier."""
    page = context.new_page()
    open_path_tab(page, flask_server["base_url"])
    txt = page.locator('.netpath-box').nth(1).inner_text()
    assert "172.16.3.2" in txt
    assert "eth0" in txt


def test_a_stopped_vm_says_the_path_is_only_declared(context, flask_server):
    """Sans VMI il n'y a ni IP ni état de lien. Laisser croire le contraire
    ferait diagnostiquer une panne qui n'en est pas une."""
    page = context.new_page()
    open_path_tab(page, flask_server["base_url"], live=False)
    warn = page.locator('.netpath-warn').first.inner_text()
    assert "declared" in warn.lower()
    # La chaîne reste dessinée : elle ne dépend pas de l'exécution.
    assert "enp1s0" in chain_titles(page)


def test_the_two_probes_only_run_when_asked(context, flask_server):
    """Chacune est un aller-retour SSH vers le nœud : les lancer au rendu
    coûterait à chaque ouverture de l'onglet."""
    page = context.new_page()
    calls = []
    page.on("request", lambda r: calls.append(r.url)
            if ("/hostport" in r.url or "/lldp" in r.url) else None)
    page.route("**/hostport*", lambda r, q: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"host_port": "vethabc", "candidates": ["vethabc"]})))
    open_path_tab(page, flask_server["base_url"])
    assert not calls, "une sonde est partie sans qu'on la demande"
    page.locator('[data-netpath-port]').first.click()
    page.wait_for_timeout(1200)
    assert any("/hostport" in u for u in calls)
    assert "vethabc" in page.locator('.netpath-out').first.inner_text()


def test_the_switch_probe_is_offered_on_the_physical_card(context, flask_server):
    """LLDP n'a de sens que sur la carte physique, pas sur un bridge."""
    page = context.new_page()
    open_path_tab(page, flask_server["base_url"])
    btn = page.locator('[data-netpath-lldp]')
    assert btn.count() == 1
    assert btn.first.get_attribute("data-iface") == "enp1s0"


def test_the_switch_answer_reads_as_one_line_per_field(context, flask_server):
    """v1.44.6 : les champs d'une vraie trame (relevée sur harvlab), un par
    ligne avec son libellé, plus « system_name=... chassis_id=... »."""
    page = context.new_page()
    page.route("**/lldp*", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "found": True, "fields": {
                "system_name": "node2-xl170r", "port_description": "br0",
                "port_id": "c6:34:cd:cd:4f:1a", "chassis_id": "70:10:6f:b6:e6:3a",
                "management_address": "172.16.1.12",
                "system_description": "openSUSE Tumbleweed Linux"}})))
    open_path_tab(page, flask_server["base_url"])
    page.locator('[data-netpath-lldp]').first.click()
    page.wait_for_timeout(800)
    lines = page.locator('.netpath-out').last.inner_text().splitlines()
    assert lines == ["Switch : node2-xl170r", "Port : br0 (c6:34:cd:cd:4f:1a)",
                     "Management address : 172.16.1.12", "Chassis : 70:10:6f:b6:e6:3a",
                     "Description : openSUSE Tumbleweed Linux"]


