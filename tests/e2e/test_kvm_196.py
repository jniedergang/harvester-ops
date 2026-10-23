"""v1.96.0 : la section KVM de la console, API simulée.

Groupe KVM de la barre latérale (titres traduits), blocs des hôtes (dont un
hôte injoignable qui dit pourquoi), tableau des VMs (filtres, tri), menu des
gestes grisés selon l'état (table des refus servie par le serveur), arrêt
forcé confirmé dans la page (jamais `confirm()`), résultat honnête d'un
reboot, gestes forcés proposés après un échec, fenêtre « Déclarer un hôte »
(empreinte à choisir explicitement, envoi multipart, points du test affichés
un par un), et échappement d'un nom de VM hostile."""

import json
import re
from urllib.parse import unquote

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

EVIL = "<img src=x onerror=alert(1)>"
FP_ED = "SHA256:81Nop5uq4NBaqU/hzSyopOIxSoyytc6DgyLZd5/xXJY"
FP_RSA = "SHA256:9DcPqiJ5rsafakefingerprintAAAAAAAAAAAAAAAAAA"
GESTURES = {"destroy": ["shut off"], "reboot": ["paused", "shut off"], "resume": ["idle", "running", "shut off"],
            "shutdown": ["paused", "shut off"],
            "start": ["crashed", "idle", "in shutdown", "paused", "pmsuspended", "running"],
            "suspend": ["paused", "pmsuspended", "shut off"]}


def vm(name, state, vcpus=2, mem=2097152, host="node2", persistent=True):
    return {"host": host, "name": name, "id": 1 if state == "running" else None, "state": state,
            "persistent": persistent, "autostart": name == "web", "vcpus": vcpus, "memory_kib": mem,
            "memory_max_kib": mem, "cpu_time_ns": None}


VMS = [vm("web", "running", 4, 8388608), vm("db", "shut off", 2, 4194304), vm("frozen", "paused", 1, 1048576),
       vm(EVIL, "shut off", 1, 524288, persistent=False), vm("broken", "crashed", 2, 3145728)]
NODE2 = {"name": "node2", "address": "172.16.1.12", "port": 22, "user": "ju", "uri": "qemu:///system",
         "labels": {"site": "cave"}, "hostname": "node2-xl170r",
         "cpu": {"cpus": 28, "cpu_model": "x86_64", "mhz": 2400, "sockets": 1},
         "memory": {"total_kib": 131276800, "free_kib": 2000000, "available_kib": 55000000},
         "libvirt_version": "12.7.0", "qemu_version": "10.2.2", "vms": VMS, "errors": []}
GHOST = {"name": "ghost", "address": "172.16.1.250", "port": 22, "user": "ju", "uri": "qemu:///system", "labels": {},
         "error": {"kind": "unreachable", "message": "ghost: unreachable: ssh: connect to host 172.16.1.250 port 22: No route to host"}}
DETAIL = dict(NODE2, pools=[{"name": "default", "state": "running", "autostart": True, "persistent": True,
                             "capacity": 107374182400, "allocation": 53687091200, "available": 53687091200,
                             "volumes": [{"name": "web.qcow2", "path": "/var/lib/libvirt/images/web.qcow2", "type": "file",
                                          "capacity": 21474836480, "allocation": 5368709120}]},
                            {"name": "iso", "state": "inactive", "autostart": False, "persistent": True,
                             "capacity": None, "allocation": None, "available": None, "volumes": []}],
              networks=[{"name": "default", "state": "active", "active": True, "autostart": True, "persistent": True,
                         "forward": "nat", "bridge": "virbr0",
                         "ips": [{"family": "ipv4", "address": "192.168.122.1", "prefix": 24,
                                  "dhcp_ranges": [{"start": "192.168.122.2", "end": "192.168.122.254"}]}]}])


def sse(steps, status="done", output=None, error=None):
    body = "".join(f"event: step\ndata: {json.dumps(s)}\n\n" for s in steps)
    end = {"status": status, "error_summary": error, "result": {"output": output or {}}}
    return body + f"event: end\ndata: {json.dumps(end)}\n\n"


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "if (!sessionStorage.getItem('kvm-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_tab','overview');"
        "sessionStorage.setItem('kvm-init','1'); }")
    page = context.new_page()
    state = {"sent": [], "streams": {}, "role": "admin"}

    def ok(route, body, status=200):
        return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def api(route, req):
        path = unquote(req.url.split("/api/kvm/", 1)[1].split("?", 1)[0])
        m = req.method
        if m == "GET":
            if path == "hosts":
                return ok(route, {"hosts": [NODE2, GHOST], "warnings": [], "role": state["role"], "gestures": GESTURES})
            if path == "vms":
                return ok(route, {"vms": VMS, "role": state["role"], "gestures": GESTURES, "warnings": [],
                                  "hosts": [{"name": "node2", "state": "ok", "count": 5},
                                            {"name": "ghost", "state": "unreachable", "error": GHOST["error"]["message"]}]})
            if path == "hosts/node2":
                return ok(route, DETAIL)
            if path.startswith("vms/node2/"):
                name = path.split("/", 2)[2]
                return ok(route, {"name": name, "host": "node2", "state": "running", "vcpus": 4, "memory_kib": 8388608,
                                  "disks": [{"target": "vda", "device": "disk", "source": "/var/lib/libvirt/images/web.qcow2",
                                             "format": "qcow2", "bus": "virtio"}],
                                  "nics": [{"type": "network", "source": "default", "mac": "52:54:00:aa:bb:cc", "model": "virtio"}],
                                  "graphics": [{"type": "vnc", "listen": "127.0.0.1", "port": 5900, "autoport": True}],
                                  "cpu_pinned": True, "persistent": True, "autostart": True, "machine": "pc-q35-10.2", "firmware": "efi"})
            return ok(route, {"error": "not found"}, 404)
        body = req.post_data or ""
        state["sent"].append((m, path, body))
        aid = f"kvm{len(state['sent']):09d}"
        if path == "hosts/scan":
            return ok(route, {"address": "172.16.1.12", "port": 22,
                              "keys": [{"type": "ssh-rsa", "fingerprint": FP_RSA}, {"type": "ssh-ed25519", "fingerprint": FP_ED}]})
        if path == "hosts" and m == "POST":
            state["streams"][aid] = sse([
                {"step_id": "validate", "status": "done", "message": "ju@172.16.1.12:22"},
                {"step_id": "scan", "status": "done", "message": f"pinning the ssh-ed25519 key {FP_ED}"},
                {"step_id": "save", "status": "done", "message": "host node3 saved"},
                {"step_id": "check-ssh", "status": "ok", "message": "connected as ju@172.16.1.12"},
                {"step_id": "check-virsh", "status": "ok", "message": "virsh 12.7.0"},
                {"step_id": "check-uri", "status": "ok", "message": "qemu:///system: libvirt 12.7.0, QEMU 10.2.2"},
                {"step_id": "check-qemu-img", "status": "warn", "message": "qemu-img not found"},
                {"step_id": "check-vnc", "status": "ok", "message": "no running domain"}],
                output={"ok": True, "checks": []})
        elif path.endswith("/test"):
            state["streams"][aid] = sse([{"step_id": "check-ssh", "status": "ok", "message": "connected"}], output={
                "ok": True, "checks": [{"check": "ssh", "status": "ok", "message": "connected as ju@172.16.1.12"},
                                       {"check": "qemu-img", "status": "warn", "message": "qemu-img not found"}]})
        elif path.endswith("/reboot"):
            state["streams"][aid] = sse([{"step_id": "wait", "status": "done", "message": "web: reboot requested"}],
                                        output={"ok": True, "state": "running", "confirmed": False, "gone": False})
        elif path.endswith("/shutdown"):
            state["streams"][aid] = sse([{"step_id": "wait", "status": "error", "message": "still running"}], status="error",
                                        error="still running", output={
                "ok": False, "kind": "timeout", "suggest": ["destroy"],
                "error": "web is still running after 120s: the guest did not shut down; use destroy to force it off"})
        else:
            state["streams"][aid] = sse([], output={"ok": True, "state": "shut off", "confirmed": True, "gone": False})
        return ok(route, {"action_id": aid}, 202)

    page.route(re.compile(r".*/api/kvm/.*"), api)
    page.route(re.compile(r".*/api/stream/kvm\d+$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body=state["streams"].get(q.url.rsplit("/", 1)[1], sse([]))))
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.KVM && window.App")
    page.mouse.move(1200, 600)
    return page, state, dialogs


def open_tab(page, tab):
    page.locator(f'#tab-group-kvm .tab[data-tab="{tab}"]').evaluate("el => el.click()")
    pane = page.locator(f"#tab-{tab}")
    expect(pane.locator(".kvm-card")).to_be_visible()
    return pane


def test_sidebar_has_the_kvm_group_with_translated_titles(ui):
    page, _, _ = ui
    groups = page.locator("nav .tab-group").evaluate_all("els => els.map(e => e.id)")
    assert groups[groups.index("tab-group-shared") + 1] == "tab-group-kvm"
    group = page.locator("#tab-group-kvm")
    expect(group.locator(".tab-group-head .sidebar-label")).to_have_text("KVM")
    tabs = group.locator(".tab-child").evaluate_all("els => els.map(e => e.dataset.tab)")
    assert tabs == ["kvmhosts", "kvmvms", "kvmstorage", "kvmnetworks"]
    for t in group.locator(".tab").all():
        key = t.get_attribute("data-i18n-title")
        assert t.get_attribute("title") == page.evaluate(f"i18n.t('{key}')")
    page.evaluate("i18n.setLang('fr')")
    expect(group.locator('.tab[data-tab="kvmhosts"] .sidebar-label')).to_have_text("Hôtes")
    assert group.locator('.tab[data-tab="kvmstorage"]').get_attribute("title").startswith("Les pools de stockage")
    page.evaluate("i18n.setLang('en')")


def test_host_cards_including_an_unreachable_host(ui):
    page, state, dialogs = ui
    pane = open_tab(page, "kvmhosts")
    tiles = pane.locator("[data-kvm-host]")
    expect(tiles).to_have_count(2)
    node2 = pane.locator('[data-kvm-host="node2"]')
    expect(node2).to_contain_text("28 CPU")
    expect(node2).to_contain_text("libvirt 12.7.0 · QEMU 10.2.2")
    expect(node2).to_contain_text("5 VM(s)")
    expect(node2.locator(".kvm-labels")).to_contain_text("site=cave")
    expect(node2.locator(".badge.ok").first).to_have_text("reachable")
    ghost = pane.locator('[data-kvm-host="ghost"]')
    expect(ghost).to_have_class(re.compile("is-error"))
    expect(ghost.locator(".badge.fail")).to_have_text("unreachable")
    assert "No route to host" in ghost.locator(".badge.fail").get_attribute("data-tip")
    for el in pane.locator(".res-tools button, .res-tools select, [data-kvm-host] button").all():
        assert el.get_attribute("data-tip"), el.evaluate("e => e.outerHTML")
    # tester : une action, puis les points un par un
    node2.locator('[data-kvm="test"]').click()
    expect(node2.locator(".kvm-checks li")).to_have_count(2)
    expect(node2.locator('.kvm-checks li[data-check="qemu-img"] .badge.warn')).to_have_text("warning")
    # retirer : confirmation dans la page, rien n'est envoyé avant
    node2.locator('[data-kvm="remove"]').click()
    expect(node2.locator(".kvm-confirm")).to_contain_text("Remove host node2 from the console?")
    assert [s for s in state["sent"] if s[0] == "DELETE"] == []
    node2.locator('[data-kvm="remove-yes"]').click()
    expect(node2.locator(".kvm-tile-msg")).to_contain_text("Host node2 removed")
    assert ("DELETE", "hosts/node2", "") in state["sent"]
    assert dialogs == []


def test_viewer_cannot_declare_test_or_remove(ui):
    page, state, _ = ui
    state["role"] = "viewer"
    pane = open_tab(page, "kvmhosts")
    expect(pane.locator('[data-kvm="declare"]')).to_be_disabled()
    expect(pane.locator('[data-kvm-host="node2"] [data-kvm="test"]')).to_be_disabled()
    expect(pane.locator('[data-kvm-host="node2"] [data-kvm="remove"]')).to_be_disabled()


def names(pane):
    return pane.locator("tbody tr[data-kvm-vm] .kvm-vm-name").all_inner_texts()


def test_vm_table_filters_and_sort(ui):
    page, _, dialogs = ui
    pane = open_tab(page, "kvmvms")
    expect(pane.locator("tbody tr[data-kvm-vm]")).to_have_count(5)
    expect(pane.locator(".kvm-host-states")).to_contain_text("ghost")
    expect(pane.locator(".kvm-host-states .badge.fail")).to_have_text("unreachable")
    assert names(pane) == sorted(names(pane), key=str.lower)
    pane.locator('[data-kvm-filter="state"]').select_option("paused")
    assert names(pane) == ["frozen"]
    pane.locator('[data-kvm-filter="state"]').select_option("")
    pane.locator('[data-kvm-filter="text"]').fill("we")
    assert names(pane) == ["web"]
    pane.locator('[data-kvm-filter="text"]').fill("")
    pane.locator('th[data-sort="memory"]').click()
    assert names(pane)[0] == EVIL and names(pane)[-1] == "web"
    pane.locator('th[data-sort="memory"]').click()
    assert names(pane)[0] == "web"
    expect(pane.locator('th[data-sort="memory"]')).to_have_class(re.compile("sort-desc"))
    pane.locator('[data-kvm-filter="host"]').select_option("node2")
    expect(pane.locator("tbody tr[data-kvm-vm]")).to_have_count(5)
    # le détail se déplie
    pane.locator('tr[data-kvm-vm="node2/web"] [data-kvm="detail"]').click()
    detail = pane.locator('tr[data-kvm-detail="node2/web"]')
    expect(detail).to_contain_text("pc-q35-10.2 · efi")
    expect(detail).to_contain_text("52:54:00:aa:bb:cc")
    expect(detail.locator(".kvm-detail-grid")).to_contain_text("CPU pinning yes")
    assert dialogs == []


def menu_item(page, g):
    return page.locator(f'.kvm-menu [data-kvm-gesture="{g}"]')


@pytest.mark.parametrize("vm_key,enabled,disabled", [
    ("node2/web", ["shutdown", "reboot", "suspend", "destroy"], ["start", "resume"]),
    ("node2/frozen", ["resume", "destroy"], ["start", "shutdown", "reboot", "suspend"]),
    ("node2/db", ["start"], ["shutdown", "reboot", "suspend", "resume", "destroy"]),
    # plantée : libvirt la garde active ; reprendre n'a pas de sens, l'arrêt forcé si
    ("node2/broken", ["shutdown", "reboot", "suspend", "destroy"], ["start", "resume"]),
])
def test_menu_disables_invalid_gestures(ui, vm_key, enabled, disabled):
    page, _, _ = ui
    pane = open_tab(page, "kvmvms")
    pane.locator(f'tr[data-kvm-vm="{vm_key}"] [data-kvm="menu"]').click()
    for g in enabled:
        assert menu_item(page, g).get_attribute("aria-disabled") is None, g
    for g in disabled:
        expect(menu_item(page, g)).to_have_attribute("aria-disabled", "true")
        assert menu_item(page, g).get_attribute("data-tip").startswith(("Not possible", "Only a paused")), g


def test_destroy_is_confirmed_in_the_page_and_reboot_is_honest(ui):
    page, state, dialogs = ui
    pane = open_tab(page, "kvmvms")
    pane.locator('tr[data-kvm-vm="node2/web"] [data-kvm="menu"]').click()
    menu_item(page, "destroy").click()
    fb = pane.locator('[data-kvm="feedback"]')
    expect(fb.locator(".kvm-confirm")).to_contain_text("Force off web on node2?")
    assert state["sent"] == []
    fb.locator('[data-kvm="confirm-no"]').click()
    expect(fb.locator(".kvm-confirm")).to_have_count(0)
    assert state["sent"] == []
    pane.locator('tr[data-kvm-vm="node2/web"] [data-kvm="menu"]').click()
    menu_item(page, "destroy").click()
    fb.locator('[data-kvm="destroy-yes"]').click()
    expect(fb).to_contain_text("web: shut off")
    assert state["sent"][-1][:2] == ("POST", "vms/node2/web/destroy")
    pane.locator('tr[data-kvm-vm="node2/web"] [data-kvm="menu"]').click()
    menu_item(page, "reboot").click()
    expect(fb).to_contain_text("reboot requested, the VM is running (libvirt cannot confirm")
    assert dialogs == []


def test_a_failed_shutdown_offers_the_forced_gesture(ui):
    page, state, _ = ui
    pane = open_tab(page, "kvmvms")
    pane.locator('tr[data-kvm-vm="node2/web"] [data-kvm="menu"]').click()
    menu_item(page, "shutdown").click()
    fb = pane.locator('[data-kvm="feedback"]')
    expect(fb.locator(".res-error")).to_contain_text("use destroy to force it off")
    fb.locator('[data-kvm="suggest"][data-gesture="destroy"]').click()
    expect(fb.locator(".kvm-confirm")).to_be_visible()         # le forcé reste confirmé
    assert state["sent"][-1][1] == "vms/node2/web/shutdown"


def test_hostile_vm_name_is_escaped(ui):
    page, _, dialogs = ui
    pane = open_tab(page, "kvmvms")
    expect(pane.locator("tbody tr[data-kvm-vm]")).to_have_count(5)
    assert EVIL in names(pane)
    assert pane.locator("table img").count() == 0
    pane.locator(f'tr[data-kvm-vm="node2/{EVIL}"] [data-kvm="menu"]').click()
    expect(page.locator(".kvm-menu .vma-head")).to_contain_text(EVIL)
    assert page.locator(".kvm-menu img").count() == 0
    assert dialogs == []


def test_storage_and_networks(ui):
    page, _, _ = ui
    pane = open_tab(page, "kvmstorage")
    block = pane.locator('[data-kvm-block="node2"]')
    expect(block.locator('tr[data-kvm-pool="default"]')).to_contain_text("100.0 GiB")
    expect(block.locator('tr[data-kvm-pool="default"]')).to_contain_text("50 %")
    expect(block.locator('tr[data-kvm-pool="iso"]')).to_contain_text("inactive pool")
    block.locator('tr[data-kvm-pool="default"] summary').click()
    expect(block.locator(".kvm-vols")).to_contain_text("web.qcow2")
    expect(pane.locator('[data-kvm-block="ghost"] .badge.fail')).to_have_text("unreachable")
    pane = open_tab(page, "kvmnetworks")
    row = pane.locator('[data-kvm-block="node2"] tr[data-kvm-net="default"]')
    expect(row).to_contain_text("nat")
    expect(row).to_contain_text("virbr0")
    expect(row).to_contain_text("192.168.122.1/24")


def test_declare_window_requires_an_explicit_fingerprint(ui):
    page, state, dialogs = ui
    pane = open_tab(page, "kvmhosts")
    pane.locator('[data-kvm="declare"]').click()
    panel = page.locator(".floating-panel", has=page.locator(".kvm-declare"))
    f = panel.locator("form")
    f.locator('[name="address"]').fill("172.16.1.12")
    f.locator('[name="name"]').fill("node3")
    f.locator('[name="user"]').fill("ju")
    f.locator('[name="key"]').set_input_files({"name": "id_ed25519", "mimeType": "text/plain",
                                               "buffer": b"-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----\n"})
    panel.locator('[data-kvm-dc="submit"]').click()
    expect(panel.locator('[data-kvm-dc="msg"]')).to_contain_text("Choose the host key to pin")
    assert state["sent"] == []
    panel.locator('[data-kvm-dc="scan"]').click()
    radios = panel.locator('input[name="fingerprint"]')
    expect(radios).to_have_count(2)
    assert all(not r.is_checked() for r in radios.all())        # rien n'est choisi d'office
    panel.locator('[data-kvm-dc="submit"]').click()
    expect(panel.locator('[data-kvm-dc="msg"]')).to_contain_text("Choose the host key to pin")
    assert [s[1] for s in state["sent"]] == ["hosts/scan"]
    panel.locator(f'input[name="fingerprint"][value="{FP_ED}"]').check()
    f.locator('[name="labels"]').fill("site=cave\nrack=b2")
    panel.locator('[data-kvm-dc="submit"]').click()
    steps = panel.locator('[data-kvm-dc="steps"] li')
    expect(steps).to_have_count(8)
    expect(panel.locator('[data-kvm-dc="steps"] li[data-step="check-qemu-img"] .badge.warn')).to_have_text("warning")
    expect(panel.locator('[data-kvm-dc="msg"]')).to_contain_text("Host node3 declared and tested")
    method, path, body = state["sent"][-1]
    assert (method, path) == ("POST", "hosts")
    payload = json.loads(re.search(r'name="payload"\r\n\r\n(.*?)\r\n--', body, re.S).group(1))
    assert payload == {"name": "node3", "address": "172.16.1.12", "port": 22, "user": "ju", "fingerprint": FP_ED,
                       "labels": {"site": "cave", "rack": "b2"}}
    assert 'name="key"; filename="id_ed25519"' in body
    # changer d'adresse efface les empreintes relevées
    f.locator('[name="address"]').fill("172.16.1.13")
    expect(radios).to_have_count(0)
    assert dialogs == []
