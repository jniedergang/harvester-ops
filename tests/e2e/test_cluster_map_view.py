"""v1.43.0 : la vue Cluster en blocs, dans un vrai navigateur.

Ce que ces tests tiennent : un bloc par hôte avec son état et ses deux
jauges (surallocation signalée) ; chaque VM en carte avec ce qu'elle
consomme ; le détail complet en calque au survol et au focus clavier ; le
filtre ; les actions d'une VM et leurs verrous ; isoler et réintégrer un
hôte ; la maintenance, dont le contrôle préalable s'affiche AVANT toute
demande, avec le forçage derrière le verrou ; bulles d'aide ; français ;
échappement.

Le réseau est intercepté : aucun test ne touche à un cluster.
"""

import json

import pytest

playwright = pytest.importorskip("playwright")

GIB = 1024 ** 3
XSS = '<img src=x onerror="window.__xss=1">'


def node(name, cpu=8, mem=32, vcpu=0, used=0, ready=True, schedulable=True,
         maintenance=None, roles=("control-plane", "etcd", "master"), last=False):
    return {"name": name, "uid": "u-" + name, "ready": ready, "schedulable": schedulable,
            "roles": list(roles), "addresses": {"InternalIP": "172.16.3." + str(len(name))},
            "capacity": {}, "allocatable": {}, "cpu_allocatable": cpu,
            "memory_allocatable": mem * GIB, "maintenance": maintenance,
            "vcpu_allocated": vcpu, "memory_allocated": used * GIB, "last_available": last}


def vm(name, node_name=None, vcpu=2, mem=4, disks=(), nics=(), phase="Running",
       run="Always", guest_os=None, guest_only=()):
    return {"namespace": "default", "name": name, "uid": "u-" + name, "phase": phase,
            "run_strategy": run, "node": node_name, "networks": [], "interfaces": [],
            "volumes": [], "vcpu": vcpu, "memory": mem * GIB, "disks": list(disks),
            "disk_total": sum(d.get("size") or 0 for d in disks), "nics": list(nics),
            "guest_only": list(guest_only), "guest_os": guest_os}


def disk(name, size=None, source="pvc", boot=None, sc="harvester-longhorn", device="disk"):
    return {"disk": name, "device": device, "boot_order": boot, "source": source,
            "pvc": (name + "-claim") if source == "pvc" else None,
            "size": size * GIB if size else None,
            "storage_class": sc if source == "pvc" else None}


def nic(name, network=None, mac="52:54:00:aa:bb:01", ips=(), pod=False, iface="eth0"):
    return {"nic": name, "network": network, "pod": pod, "model": "virtio",
            "binding": "bridge", "mac": mac, "ips": list(ips), "guest_iface": iface,
            "link_state": "up"}


TOPO = {
    "cluster": "harv-fake", "fetched_at": 0,
    "nodes": [node("n1.lo", vcpu=10, used=12), node("n2.lo", schedulable=False, roles=()),
              node("n3.lo", schedulable=False, maintenance="completed", roles=())],
    "vms": [
        vm("web", "n1.lo", vcpu=2, mem=4, guest_os="openSUSE Leap 15.6",
           disks=[disk("root", 20, boot=1), disk("cloudinitdisk", source="cloudinit")],
           nics=[nic("default", "default/vlan10", ips=["172.16.3.43", "fe80::1"])],
           guest_only=[{"iface": "docker0", "mac": None, "ips": ["172.17.0.1"]}]),
        vm("db", "n1.lo", vcpu=8, mem=8, disks=[disk("root", 20), disk("data", 40)],
           nics=[nic("default", pod=True, mac="52:54:00:aa:bb:02", ips=["10.52.0.9"])]),
        vm("off", None, phase="Stopped", run="Halted",
           disks=[disk("root", 10), dict(disk("install", source=None, boot=2), device="cdrom")]),
    ],
}


def open_cluster(page, base_url, data=TOPO, lang="en", calls=None, checks=None,
                 status=201):
    """Ouvre la vue Cluster ; `calls` reçoit (méthode, url, corps) de chaque
    geste, `checks` les réponses successives du contrôle de maintenance."""
    def topo(route, request):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    def mutate(route, request):
        if "maintenance-check" in request.url:
            plan = (checks or [{}]).pop(0) if checks else {}
            if calls is not None:
                calls.append(("GET", request.url, None))
            route.fulfill(status=200, content_type="application/json", body=json.dumps(plan))
            return
        if calls is not None:
            calls.append((request.method, request.url,
                          json.loads(request.post_data) if request.post_data else None))
        body = ({"action_id": "a1"} if status == 201
                else {"error": "no-change", "detail": "the node is already cordoned"})
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/topology/**", topo)
    page.route("**/api/node/**", mutate)
    page.route("**/api/vm/**", mutate)
    page.context.add_init_script(
        f"localStorage.setItem('harvester_ops_language','{lang}');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','overview');")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    page.click('[data-overview-tab="cluster"]')
    page.wait_for_selector('.overview-subtab[data-subtab="cluster"] .cm-host', timeout=10000)
    page.wait_for_timeout(500)
    return page.locator('.overview-subtab[data-subtab="cluster"]')


def host(view, name):
    return view.locator(f'.cm-host[data-host="{name}"]')


def card(view, name):
    return view.locator(f'.cm-vm[data-vm="default/{name}"]')


def test_one_block_per_host_with_state_and_gauges(context, flask_server):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    view = open_cluster(page, flask_server["base_url"])
    names = view.locator('.cm-host[data-host]').evaluate_all(
        "els => els.map(e => e.dataset.host)")
    assert names == ["n1.lo", "n2.lo", "n3.lo"]
    n1 = host(view, "n1.lo")
    assert "control-plane" in n1.locator('.vsw-head').inner_text()
    gauges = n1.locator('.cm-gauge')
    assert gauges.count() == 2
    # 10 vCPU sur 8 : surallocation montrée, la barre ne déborde pas
    assert "10 / 8 (125 %)" in gauges.nth(0).inner_text()
    assert "overcommitted" in gauges.nth(0).inner_text()
    assert "over" in (gauges.nth(0).get_attribute("class") or "")
    assert gauges.nth(0).locator('.sto-bar-used').evaluate("e => e.style.width") == "100%"
    assert "12 GiB / 32 GiB (38 %)" in gauges.nth(1).inner_text()
    assert "cordoned" in host(view, "n2.lo").locator('.vsw-head').inner_text()
    assert "in maintenance" in host(view, "n3.lo").locator('.vsw-head').inner_text()
    assert "warn" in (host(view, "n2.lo").get_attribute("class") or "")
    assert view.locator('canvas').count() == 0
    assert not errors, errors


def test_each_vm_card_says_what_it_consumes(context, flask_server):
    page = context.new_page()
    view = open_cluster(page, flask_server["base_url"])
    web = card(view, "web").inner_text()
    assert "2 vCPU · 4 GiB" in web and "20 GiB" in web
    assert "vlan10" in web and "172.16.3.43" in web
    db = card(view, "db").inner_text()
    assert "8 vCPU · 8 GiB" in db and "2 disks · 60 GiB" in db and "pod network" in db
    # rangées sous leur hôte ; la VM arrêtée dans le bloc des arrêtées
    assert host(view, "n1.lo").locator('.cm-vm').count() == 2
    idle = view.locator('.cm-idle')
    assert idle.locator('.cm-vm[data-vm="default/off"]').count() == 1
    assert "Stopped / unscheduled" in idle.text_content()   # capitales en CSS


def test_hover_shows_the_full_detail(context, flask_server):
    page = context.new_page()
    view = open_cluster(page, flask_server["base_url"])
    pop = page.locator('.cm-pop')
    card(view, "web").hover()
    page.wait_for_timeout(150)
    assert pop.is_visible()
    text = pop.inner_text()
    for part in ("default/web", "openSUSE Leap 15.6", "root", "20 GiB", "harvester-longhorn",
                 "boot 1", "cloud-init", "52:54:00:aa:bb:01", "172.16.3.43", "fe80::1",
                 "docker0 172.17.0.1", "Always"):
        assert part in text, part
    # le calque tient dans la fenêtre
    box = pop.bounding_box()
    vw = page.evaluate("window.innerWidth")
    assert box["x"] >= 0 and box["x"] + box["width"] <= vw
    page.mouse.move(2, 2)
    page.wait_for_timeout(150)
    assert not pop.is_visible()


def test_the_hover_detail_flips_left_near_the_right_edge(context, flask_server):
    """Sur la dernière colonne, le calque passerait sous le panneau de
    détail ou hors de la fenêtre : il s'ouvre alors à gauche de la carte."""
    page = context.new_page()
    data = json.loads(json.dumps(TOPO))
    data["vms"] += [vm(f"extra{i:02d}", "n1.lo") for i in range(12)]
    view = open_cluster(page, flask_server["base_url"], data=data)
    cards = host(view, "n1.lo").locator('.cm-vm')
    boxes = [(cards.nth(i).bounding_box()["x"], i) for i in range(cards.count())]
    right = cards.nth(max(boxes)[1])
    right.hover()
    page.wait_for_timeout(150)
    pop = page.locator('.cm-pop').bounding_box()
    box = right.bounding_box()
    assert pop["x"] + pop["width"] <= box["x"], "le calque couvre la carte survolée"
    assert pop["x"] >= 0


def test_an_empty_cdrom_is_said_empty(context, flask_server):
    """Relevé sur harv1 : un lecteur CD-ROM dont l'ISO a été retiré n'a plus
    de volume. Le calque le dit vide, pas « ? »."""
    page = context.new_page()
    view = open_cluster(page, flask_server["base_url"])
    card(view, "off").hover()
    page.wait_for_timeout(150)
    text = page.locator('.cm-pop').inner_text()
    assert "install" in text and "CD-ROM" in text and "empty, no medium" in text
    assert "?" not in text


def test_keyboard_focus_shows_the_detail_and_enter_opens_the_panel(context, flask_server):
    page = context.new_page()
    view = open_cluster(page, flask_server["base_url"])
    card(view, "db").focus()
    page.wait_for_timeout(150)
    assert page.locator('.cm-pop').is_visible()
    assert "default/db" in page.locator('.cm-pop').inner_text()
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    assert view.locator('.fabric-detail [data-cm-act="vm-console"]').count() == 1


def test_the_filter_narrows_the_cards(context, flask_server):
    page = context.new_page()
    view = open_cluster(page, flask_server["base_url"])
    view.locator('.cm-filter').fill("vlan10")
    page.wait_for_timeout(400)
    assert card(view, "web").count() == 1 and card(view, "db").count() == 0
    assert "1 of 2 VM" in host(view, "n1.lo").locator('.vsw-head').inner_text()
    assert "no VM matches the filter" in view.locator('.cm-idle').inner_text()
    view.locator('.cm-filter').fill("10.52.0.9")
    page.wait_for_timeout(400)
    assert card(view, "db").count() == 1 and card(view, "web").count() == 0


def test_vm_panel_actions_and_the_lock(context, flask_server):
    page = context.new_page()
    view = open_cluster(page, flask_server["base_url"])
    card(view, "web").click()
    side = view.locator('.fabric-detail')
    for act in ("vm-notes", "vm-edit", "vm-console", "vm-snap", "vm-migrate", "vm-stop"):
        assert side.locator(f'[data-cm-act="{act}"]').count() == 1, act
    assert side.locator('[data-cm-act="vm-start"]').count() == 0
    assert side.locator('[data-cm-act="vm-delete"]').count() == 0
    view.locator('.cm-unlock').check()
    assert side.locator('[data-cm-act="vm-delete"]').count() == 1
    card(view, "off").click()
    assert side.locator('[data-cm-act="vm-start"]').count() == 1
    assert side.locator('[data-cm-act="vm-stop"]').count() == 0


def test_stopping_a_vm_needs_a_confirmation(context, flask_server):
    page = context.new_page()
    calls = []
    view = open_cluster(page, flask_server["base_url"], calls=calls)
    card(view, "web").click()
    page.once("dialog", lambda d: d.dismiss())
    view.locator('[data-cm-act="vm-stop"]').click()
    page.wait_for_timeout(300)
    assert calls == []
    page.once("dialog", lambda d: d.accept())
    view.locator('[data-cm-act="vm-stop"]').click()
    page.wait_for_timeout(400)
    assert calls == [("PATCH", flask_server["base_url"] + "/api/vm/harv-fake/default/web/runStrategy",
                      {"runStrategy": "Halted"})]
    assert "actions dock" in view.locator('.cm-out').inner_text()


def test_cordon_and_uncordon(context, flask_server):
    page = context.new_page()
    calls = []
    view = open_cluster(page, flask_server["base_url"], calls=calls)
    host(view, "n1.lo").locator('.cm-host-head').click()
    side = view.locator('.fabric-detail')
    assert side.locator('[data-cm-act="node-uncordon"]').count() == 0
    page.once("dialog", lambda d: d.accept())
    side.locator('[data-cm-act="node-cordon"]').click()
    page.wait_for_timeout(400)
    host(view, "n2.lo").locator('.cm-host-head').click()
    assert side.locator('[data-cm-act="node-cordon"]').count() == 0
    page.once("dialog", lambda d: d.accept())
    side.locator('[data-cm-act="node-uncordon"]').click()
    page.wait_for_timeout(400)
    base = flask_server["base_url"] + "/api/node/harv-fake/"
    assert [(m, u) for m, u, _ in calls] == [("POST", base + "n1.lo/cordon"),
                                              ("POST", base + "n2.lo/uncordon")]


def test_the_last_available_node_cannot_be_cordoned(context, flask_server):
    """Relevé en réel sur harv1 : le webhook de Harvester refuse d'isoler le
    dernier nœud disponible. Le bouton reste visible mais désactivé, et la
    raison est écrite ; la maintenance dit son refus au contrôle."""
    page = context.new_page()
    calls = []
    data = json.loads(json.dumps(TOPO))
    data["nodes"] = [node("harv1", last=True)]
    checks = [{"node": "harv1", "refusal": "last-available-node", "force": False,
               "vms_on_node": [], "non_migratable": {}, "blocked": False,
               "will_stop": [], "migrate": []}]
    view = open_cluster(page, flask_server["base_url"], data=data, calls=calls, checks=checks)
    host(view, "harv1").locator('.cm-host-head').click()
    side = view.locator('.fabric-detail')
    assert side.locator('[data-cm-act="node-cordon"]').is_disabled()
    assert "last node still available" in side.locator('.cm-last-node').inner_text()
    side.locator('[data-cm-act="node-maint-check"]').click()
    page.wait_for_timeout(400)
    assert "No other node is available" in view.locator('.cm-maint-box').inner_text()
    assert [m for m, _, _ in calls] == ["GET"]


def test_a_server_refusal_is_shown(context, flask_server):
    page = context.new_page()
    view = open_cluster(page, flask_server["base_url"], status=409)
    host(view, "n1.lo").locator('.cm-host-head').click()
    page.once("dialog", lambda d: d.accept())
    view.locator('[data-cm-act="node-cordon"]').click()
    page.wait_for_timeout(400)
    assert "the node is already cordoned" in view.locator('.cm-out').inner_text()


def test_maintenance_refused_on_a_single_control_plane(context, flask_server):
    page = context.new_page()
    calls = []
    checks = [{"node": "n1.lo", "refusal": "single-control-plane", "force": False,
               "vms_on_node": ["default/db", "default/web"], "non_migratable": {},
               "blocked": False, "will_stop": [], "migrate": ["default/db", "default/web"]}]
    view = open_cluster(page, flask_server["base_url"], calls=calls, checks=checks)
    host(view, "n1.lo").locator('.cm-host-head').click()
    view.locator('[data-cm-act="node-maint-check"]').click()
    page.wait_for_timeout(400)
    box = view.locator('.cm-maint-box')
    assert "only control plane" in box.inner_text()
    assert box.locator('[data-cm-act="node-maint-enter"]').is_disabled()
    # contrôler n'a rien demandé
    assert [m for m, _, _ in calls] == ["GET"]


def test_maintenance_blocked_then_forced_behind_the_lock(context, flask_server):
    page = context.new_page()
    calls = []
    blocked = {"node": "n1.lo", "refusal": None, "force": False,
               "vms_on_node": ["default/db", "default/web"],
               "non_migratable": {"LastHealthyReplica": ["default/db"]},
               "blocked": True, "will_stop": [], "migrate": ["default/web"]}
    forced = dict(blocked, force=True, blocked=False, will_stop=["default/db"])
    view = open_cluster(page, flask_server["base_url"], calls=calls,
                        checks=[blocked, blocked, forced])
    host(view, "n1.lo").locator('.cm-host-head').click()
    view.locator('[data-cm-act="node-maint-check"]').click()
    page.wait_for_timeout(400)
    box = view.locator('.cm-maint-box')
    text = box.inner_text()
    assert "default/web" in text
    assert "default/db : the last healthy replica" in text
    assert box.locator('[data-cm-act="node-maint-enter"]').is_disabled()
    assert box.locator('[data-cm-force]').is_disabled()
    # le verrou ouvert, le forçage devient possible ; le contrôle est refait
    view.locator('.cm-unlock').check()
    view.locator('[data-cm-act="node-maint-check"]').click()
    page.wait_for_timeout(400)
    view.locator('[data-cm-force]').check()
    page.wait_for_timeout(400)
    assert calls[-1][1].endswith("/maintenance-check?force=1")
    assert "Will be shut down" in view.locator('.cm-maint-box').text_content()
    enter = view.locator('[data-cm-act="node-maint-enter"]')
    assert enter.is_enabled()
    page.once("dialog", lambda d: d.accept())
    enter.click()
    page.wait_for_timeout(400)
    assert calls[-1] == ("POST", flask_server["base_url"] + "/api/node/harv-fake/n1.lo/maintenance",
                         {"force": True})


def test_vms_the_drain_will_stop_are_said_so(context, flask_server):
    """Relevé sur harvlab : une VM sans stratégie d'éviction par migration
    est ARRÊTÉE par le drain. Le contrôle le dit, VM par VM, avec ce qu'elle
    devient et comment la faire migrer ; en forçant, il dit que les VMs
    arrêtées le restent."""
    page = context.new_page()
    base = {"node": "n1.lo", "refusal": None, "vms_on_node": ["default/db", "default/web"],
            "non_migratable": {}, "blocked": False, "migrate": ["default/web"]}
    drained = dict(base, force=False, will_stop=[], drain_stops=[
        {"vm": "default/db", "restarts": False},
        {"vm": "default/batch", "restarts": True}],
        volume_waits=[{"vm": "default/web", "volume": "pvc-web"}],
        stuck_volumes=[{"volume": "pvc-db", "claim": "default/db-data", "pods": ["db-0"]}])
    forced = dict(base, force=True, will_stop=["default/gpu"], drain_stops=[],
                  non_migratable={"LiveMigratable": ["default/gpu"]})
    view = open_cluster(page, flask_server["base_url"], checks=[drained, forced])
    host(view, "n1.lo").locator('.cm-host-head').click()
    view.locator('[data-cm-act="node-maint-check"]').click()
    page.wait_for_timeout(400)
    text = view.locator('.cm-maint-box').inner_text()
    assert "default/db : stays stopped" in text
    assert "default/batch : restarted on another node (not live-migrated)" in text
    assert "LiveMigrateIfPossible" in text
    # un volume pas sain retarde la migration : dit avant
    assert "default/web : pvc-web" in text and "Storage view" in text
    # un volume de pod dont la seule réplique saine est ici : le drain attendra
    assert "the maintenance will not finish" in text.lower() and "default/db-data (db-0)" in text
    view.locator('[data-cm-act="node-maint-check"]').click()
    page.wait_for_timeout(400)
    assert "they stay stopped after the maintenance" in view.locator('.cm-maint-box').inner_text()


def test_leaving_maintenance(context, flask_server):
    page = context.new_page()
    calls = []
    view = open_cluster(page, flask_server["base_url"], calls=calls)
    host(view, "n3.lo").locator('.cm-host-head').click()
    side = view.locator('.fabric-detail')
    assert side.locator('[data-cm-act="node-maint-check"]').count() == 0
    assert side.locator('[data-cm-act="node-cordon"]').count() == 0
    page.once("dialog", lambda d: d.accept())
    side.locator('[data-cm-act="node-maint-leave"]').click()
    page.wait_for_timeout(400)
    assert calls == [("DELETE", flask_server["base_url"] + "/api/node/harv-fake/n3.lo/maintenance",
                      None)]


def test_every_control_has_a_tooltip(context, flask_server):
    page = context.new_page()
    view = open_cluster(page, flask_server["base_url"])
    card(view, "web").click()
    for el in view.locator('button, .cm-gauge, .cm-filter, label.topology-unlock').all():
        assert (el.get_attribute("data-tip") or "").strip(), el.evaluate("e => e.outerHTML")[:120]
    host(view, "n1.lo").locator('.cm-host-head').click()
    for el in view.locator('.fabric-detail button').all():
        assert (el.get_attribute("data-tip") or "").strip()


def test_french(context, flask_server):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    view = open_cluster(page, flask_server["base_url"], lang="fr")
    assert "Hôte" in host(view, "n1.lo").text_content()
    assert "isolé" in host(view, "n2.lo").inner_text()
    assert "surallocation" in host(view, "n1.lo").inner_text()
    assert "Arrêtées / non planifiées" in view.locator('.cm-idle').text_content()
    host(view, "n1.lo").locator('.cm-host-head').click()
    side = view.locator('.fabric-detail').inner_text()
    assert "Isoler" in side and "Mettre en maintenance..." in side
    assert not errors, errors


def test_values_from_the_cluster_are_escaped(context, flask_server):
    page = context.new_page()
    data = json.loads(json.dumps(TOPO))
    data["vms"][0]["guest_os"] = XSS
    data["vms"][0]["nics"][0]["network"] = "default/" + XSS
    view = open_cluster(page, flask_server["base_url"], data=data)
    card(view, "web").hover()
    page.wait_for_timeout(200)
    card(view, "web").click()
    page.wait_for_timeout(200)
    assert page.evaluate("window.__xss") is None
    assert XSS in page.locator('.cm-pop').inner_text()
