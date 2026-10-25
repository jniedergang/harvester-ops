"""v1.98.0 : instantanés, clonage et suppression d'une VM KVM, API simulée.

Menu d'une VM (second groupe grisé d'après la table servie par le serveur,
avec la raison), fenêtre Instantanés (actuel, pause annoncée, raisons de
refus, retour et suppression confirmés dans la fenêtre, mise hors tension
annoncée), fenêtre Cloner (aperçu, refus, progression, résultat), fenêtre
Supprimer (aperçu d'abord, fichier illisible à accepter explicitement, nom à
saisir, empreinte de l'aperçu montré, nouvel aperçu quand l'hôte a changé),
et noms hostiles affichés en texte partout (fenêtres, confirmation, dock)."""

import json
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402

EVIL = "<img src=x onerror=window.__xss=1>"
EVIL_SNAP = "<img src=x onerror=window.__xss=2>"
UNREAD = "/var/lib/machines/moon/netperf.qcow2"
D0, D1, D2, D3 = ("0" * 64, "1" * 64, "2" * 64, "3" * 64)
GESTURES = {"destroy": ["shut off"], "reboot": ["paused", "shut off"],
            "resume": ["blocked", "crashed", "idle", "in shutdown", "running", "shut off"],
            "shutdown": ["paused", "shut off"],
            "start": ["crashed", "idle", "in shutdown", "paused", "pmsuspended", "running"],
            "suspend": ["paused", "pmsuspended", "shut off"]}
OPERATIONS = {"snapshots": {"role": "operator", "states": None, "persistent": False},
              "clone": {"role": "operator", "states": ["shut off"], "persistent": True},
              "delete": {"role": "admin", "states": ["shut off"], "persistent": True}}


def vm(name, state, persistent=True):
    return {"host": "node2", "name": name, "id": 1 if state == "running" else None, "state": state,
            "persistent": persistent, "autostart": False, "vcpus": 1, "memory_kib": 262144,
            "memory_max_kib": 262144, "cpu_time_ns": None}


VMS = [vm("web", "running"), vm("db", "shut off"), vm("temp", "shut off", persistent=False),
       vm("rawvm", "shut off"), vm(EVIL, "shut off")]

SNAPS = {
    "web": [{"name": "s-run", "created": "2026-10-24T10:00:00+02:00", "state": "running", "parent": None,
             "location": "internal", "current": True},
            {"name": "s-off", "created": "2026-10-24T11:00:00+02:00", "state": "shutoff", "parent": "s-run",
             "location": "internal", "current": False},
            {"name": "ext1", "created": "2026-10-24T12:00:00+02:00", "state": "shutoff", "parent": None,
             "location": "external", "current": False}],
    EVIL: [{"name": EVIL_SNAP, "created": "2026-10-24T10:00:00+02:00", "state": "shutoff", "parent": None,
            "location": "internal", "current": True}],
}


def disk(target, path, reason=None, deletable=False):
    return {"target": target, "source": path, "pool": None, "volume": None, "path": path, "format": "qcow2",
            "kind": "file", "deletable": deletable, "reason": reason}


def delete_plan(name, remove, ack, changed):
    plan = {"name": name, "uuid": "u-1", "state": "shut off", "persistent": True, "refusals": [],
            "internal_snapshots": 2, "external_snapshots": 0, "managed_save": False, "managed_save_lost": False,
            "nvram": "/var/lib/libvirt/qemu/nvram/db_VARS.fd", "tpm_state": "removed", "remove_storage": remove,
            "delete": [], "keep": [], "unreadable": [], "acknowledged_unreadable": [], "notes": [], "digest": D0}
    a = f"/var/lib/libvirt/images/{name}.qcow2"
    b = "/var/lib/libvirt/images/shared.qcow2"
    if not remove:
        plan["keep"] = [disk("vda", a, "not requested"), disk("vdb", b, "not requested")]
        return plan
    plan["unreadable"] = [UNREAD]
    if UNREAD not in ack:
        plan["keep"] = [disk("vda", a, "could not prove that no other VM uses it"), disk("vdb", b, "shared with web")]
        plan["refusals"] = [f"storage removal requested, but 1 disk(s) could not be proven unused by another VM "
                            f"(vda): the VM is kept; files the SSH user cannot read: {UNREAD}"]
        plan["digest"] = D1
        return plan
    plan["acknowledged_unreadable"] = [UNREAD]
    if changed:
        plan["delete"] = [disk("vda", a, deletable=True), disk("vdb", b, deletable=True)]
        plan["digest"] = D3
    else:
        plan["delete"] = [disk("vda", a, deletable=True)]
        plan["keep"] = [disk("vdb", b, "shared with web")]
        plan["digest"] = D2
    return plan


def clone_plan(name, new):
    refusals = [f"a VM named {new} already exists"] if new == "web" else []
    return {"source": name, "new_name": new, "refusals": refusals,
            "disks": [{"target": "vda", "source": f"/var/lib/libvirt/images/{name}.qcow2", "type": "file",
                       "format": "qcow2", "mode": "file", "pool": None, "volume": None,
                       "directory": "/var/lib/libvirt/images", "dest_name": f"{new}-vda.qcow2",
                       "destination": f"/var/lib/libvirt/images/{new}-vda.qcow2", "capacity": 2147483648,
                       "allocation": 1073741824, "needed": 1073741824}],
            "shared": [{"target": "sdb", "source": "/iso/cirros.iso", "reason": "read-only"}],
            "space": [{"where": "/var/lib/libvirt/images", "available": 53687091200, "needed": 1073741824}],
            "nics": [{"mac": "52:54:00:aa:bb:cc"}], "nvram": True, "uefi_entries_lost": True,
            "tpm_state_lost": False, "regenerated": ["disk serial of vda removed"]}


def sse(steps, status="done", output=None, error=None):
    body = "".join(f"event: step\ndata: {json.dumps(s)}\n\n" for s in steps)
    end = {"status": status, "error_summary": error, "result": {"output": output or {}}}
    return body + f"event: end\ndata: {json.dumps(end)}\n\n"


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "if (!sessionStorage.getItem('kvmv-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "sessionStorage.setItem('kvmv-init','1'); }")
    page = context.new_page()
    state = {"role": "admin", "sent": [], "streams": {}, "gets": [], "changed": False, "deletes": 0}

    def ok(route, body, status=200):
        return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def api(route, req):
        parts = urlsplit(req.url)
        path = unquote(parts.path.split("/api/kvm/", 1)[1])
        q = parse_qs(parts.query)
        m = req.method
        if m == "GET":
            state["gets"].append((path, q))
            if path == "vms":
                return ok(route, {"vms": VMS, "role": state["role"], "gestures": GESTURES, "operations": OPERATIONS,
                                  "warnings": [], "hosts": [{"name": "node2", "state": "ok", "count": len(VMS)}]})
            if path == "hosts":
                return ok(route, {"hosts": [], "warnings": [], "role": state["role"], "gestures": GESTURES})
            mm = re.fullmatch(r"vms/node2/(.+)/(snapshots|clone-plan|delete-plan)", path)
            if mm:
                name, what = mm.groups()
                v = next(x for x in VMS if x["name"] == name)
                if what == "snapshots":
                    if state.get("fail_lists"):
                        state["fail_lists"] -= 1
                        return ok(route, {"error": "node2: failed: error: domain is busy", "kind": "failed"}, 502)
                    vstate = state.get("vm_state", {}).get(name, v["state"])
                    return ok(route, {"host": "node2", "name": name, "role": state["role"],
                                      "snapshots": SNAPS.get(name, []), "current": None,
                                      "support": ["raw disk vda: internal snapshots need qcow2"] if name == "rawvm" else [],
                                      "running": vstate == "running", "state": vstate,
                                      "persistent": v["persistent"], "memory_kib": 262144})
                if what == "clone-plan":
                    p = clone_plan(name, q.get("name", [""])[0])
                    return ok(route, {"host": "node2", "name": name, "possible": not p["refusals"], "plan": p,
                                      "role": state["role"]})
                p = delete_plan(name, q.get("remove_storage") == ["1"], q.get("acknowledge", []), state["changed"])
                return ok(route, {"host": "node2", "name": name, "possible": not p["refusals"], "plan": p,
                                  "role": state["role"]})
            return ok(route, {"error": "not found"}, 404)
        body = json.loads(req.post_data or "{}")
        state["sent"].append((m, path, body))
        aid = f"kvm{len(state['sent']):09d}"
        if m == "DELETE" and "/snapshots/" not in path:
            state["deletes"] += 1
            if body.get("expect_delete") == D2:
                state["changed"] = True        # l'hôte change entre l'aperçu et la suppression
                state["streams"][aid] = sse(
                    [{"step_id": "plan", "status": "error", "message": "the preview changed: preview again"}],
                    status="error", error="the preview changed: preview again",
                    output={"ok": False, "kind": "plan-changed", "error": "the preview changed: preview again",
                            "refusals": [], "leftovers": [], "unreadable": [UNREAD]})
            else:
                state["streams"][aid] = sse(
                    [{"step_id": "plan", "status": "done", "message": "2 disk(s) to delete"},
                     {"step_id": "undefine", "status": "done", "message": "undefined"},
                     {"step_id": "storage", "status": "done", "message": "2 deleted"},
                     {"step_id": "verify", "status": "done", "message": "gone"}],
                    output={"ok": True, "name": "db", "undefined": True,
                            "deleted": [disk("vda", "/var/lib/libvirt/images/db.qcow2"),
                                        disk("vdb", "/var/lib/libvirt/images/shared.qcow2")],
                            "kept": [], "skipped": [], "leftovers": [], "unreadable": [UNREAD],
                            "notes": ["the vTPM state is removed with the VM"]})
        elif path.endswith("/clone"):
            state["streams"][aid] = sse(
                [{"step_id": "plan", "status": "done", "message": "1 disk(s) to copy"},
                 {"step_id": "copy-vda", "status": "running", "message": "50% (536870912 of 2147483648 bytes)"},
                 {"step_id": "copy-vda", "status": "done", "message": "copied"},
                 {"step_id": "define", "status": "done", "message": "defined"},
                 {"step_id": "verify", "status": "done", "message": "shut off"}],
                output={"ok": True, "source": "db", "name": body["name"], "uuid": "c0ffee00-1111", "state": "shut off",
                        "macs": ["52:54:00:12:34:56"], "elapsed_s": 10.5,
                        "disks": [{"target": "vda", "destination": f"/var/lib/libvirt/images/{body['name']}-vda.qcow2",
                                   "pool": None}],
                        "shared": [], "nvram": True, "uefi_entries_lost": True, "tpm_state_lost": False,
                        "regenerated": ["disk serial of vda removed"],
                        "warnings": ["owner not copied (chown needs root)"], "leftovers": []})
        elif path.endswith("/revert"):
            state["streams"][aid] = sse([{"step_id": "plan", "status": "done", "message": "reverting powers web off"},
                                         {"step_id": "revert", "status": "done", "message": "reverted"}],
                                        output={"ok": True, "snapshot": "s-off", "taken_state": "shutoff",
                                                "state": "shut off", "elapsed_s": 0.4})
        elif m == "DELETE":
            state["streams"][aid] = sse([{"step_id": "delete", "status": "done", "message": "deleted"}],
                                        output={"ok": True, "current": None, "snapshots": []})
        else:
            state["streams"][aid] = sse([{"step_id": "create", "status": "done", "message": "taken"}],
                                        output={"ok": True, "snapshot": body.get("name"), "memory": True,
                                                "state": "running", "elapsed_s": 1.8, "snapshots": []})
        return ok(route, {"action_id": aid}, 202)

    page.route(re.compile(r".*/api/kvm/.*"), api)
    page.route(re.compile(r".*/api/stream/kvm\d+$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body=state["streams"].get(q.url.rsplit("/", 1)[1], sse([]))))
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    goto(page, flask_server["base_url"], "#activity")   # 2.0.0 : écran ouvert par son adresse
    page.wait_for_function("window.KVM && window.App && window.KVMVm")
    page.mouse.move(1200, 600)
    return page, state, dialogs


def open_menu(page, name):
    page.evaluate("Router.go('#vms/host/kvm')")   # 2.0.0 : KVM > Virtual machines, par son adresse
    pane = page.locator("#tab-kvmvms")
    row = pane.locator(f'tr[data-kvm-vm="node2/{name}"]')
    expect(row).to_be_visible()
    row.locator('[data-kvm="menu"]').click()
    expect(page.locator(".kvm-menu")).to_be_visible()


def op_item(page, op):
    return page.locator(f'.kvm-menu [data-kvm-op="{op}"]')


def open_op(page, name, op, panel_cls):
    open_menu(page, name)
    op_item(page, op).click()
    panel = page.locator(".floating-panel").filter(has=page.locator(panel_cls)).last
    expect(panel).to_be_visible()
    return panel


# --- menu ---------------------------------------------------------------------------------

@pytest.mark.parametrize("role,name,enabled,disabled", [
    ("admin", "web", ["snapshots"], {"clone": "Not possible while the VM is running (needs: shut off)",
                                     "delete": "Not possible while the VM is running (needs: shut off)"}),
    ("admin", "db", ["snapshots", "clone", "delete"], {}),
    ("admin", "temp", ["snapshots"], {"clone": "Not possible for a transient VM", "delete": "Not possible for a transient VM"}),
    ("operator", "db", ["snapshots", "clone"], {"delete": "Needs the admin role"}),
    ("viewer", "db", [], {"snapshots": "Needs the operator role", "clone": "Needs the operator role",
                          "delete": "Needs the admin role"}),
])
def test_menu_greys_the_operations_from_the_served_table(ui, role, name, enabled, disabled):
    page, state, _ = ui
    state["role"] = role
    open_menu(page, name)
    for op in enabled:
        assert op_item(page, op).get_attribute("aria-disabled") is None, op
        assert op_item(page, op).get_attribute("data-tip")
    for op, why in disabled.items():
        expect(op_item(page, op)).to_have_attribute("aria-disabled", "true")
        assert op_item(page, op).get_attribute("data-tip").startswith(why), (op, op_item(page, op).get_attribute("data-tip"))
    if disabled:
        op_item(page, next(iter(disabled))).click(force=True)
        expect(page.locator(".kvm-menu")).to_be_visible()           # un item grisé n'ouvre rien
        assert page.locator(".floating-panel .kvm-vmw").count() == 0


# --- instantanés ----------------------------------------------------------------------------

def test_snapshot_window_lists_takes_and_reverts_with_in_page_confirmations(ui):
    page, state, dialogs = ui
    panel = open_op(page, "web", "snapshots", ".kvm-snapshots")
    rows = panel.locator("tr[data-ks-row]")
    expect(rows).to_have_count(3)
    expect(panel.locator('tr[data-ks-row="s-run"]')).to_contain_text("current")
    expect(panel.locator('tr[data-ks-row="s-run"]')).to_contain_text("disks and memory")
    expect(panel.locator('tr[data-ks-row="s-off"]')).to_contain_text("disks only")
    # l'instantané externe est montré, sans action
    ext = panel.locator('tr[data-ks-row="ext1"]')
    expect(ext).to_contain_text("external: not managed by the console")
    assert ext.locator("button").count() == 0
    expect(panel.locator('[data-ks="pause"]')).to_contain_text("paused while its memory (256.0 MiB) is saved")
    # nom proposé <vm>-AAAAMMJJ-HHMM
    assert re.fullmatch(r"web-\d{8}-\d{4}", panel.locator('input[name="name"]').input_value())
    for b in panel.locator("button.tip, input.tip, textarea.tip").all():
        assert b.get_attribute("data-tip"), b.inner_html()

    # prendre un instantané
    panel.locator('input[name="name"]').fill("before-upgrade")
    panel.locator('textarea[name="description"]').fill("-x <b>desc</b>")
    # la fin de l'action relit la liste (lecture forcée)
    with page.expect_request(lambda r: "/web/snapshots?fresh=1" in r.url and r.method == "GET"):
        panel.locator('[data-ks="go"]').click()
    expect(panel.locator('[data-ks="msg"]')).to_contain_text("Snapshot before-upgrade taken with the memory in 1.8 s")
    assert state["sent"][-1] == ("POST", "vms/node2/web/snapshots", {"name": "before-upgrade", "description": "-x <b>desc</b>"})
    expect(panel.locator('[data-ks="steps"] li')).to_have_count(1)

    # revenir à un instantané pris arrêté : la mise hors tension est annoncée
    sent = len(state["sent"])
    panel.locator('tr[data-ks-row="s-off"] [data-ks-act="revert"]').click()
    conf = panel.locator('[data-ks="confirm"]')
    expect(conf).to_contain_text("Revert web to s-off? The current state that was not saved is lost.")
    expect(conf).to_contain_text("web is running and this snapshot was taken shut off: reverting powers it off.")
    conf.locator('[data-ks="no"]').click()
    expect(conf).to_be_hidden()
    assert len(state["sent"]) == sent
    panel.locator('tr[data-ks-row="s-off"] [data-ks-act="revert"]').click()
    conf.locator('[data-ks="yes"]').click()
    expect(panel.locator('[data-ks="msg"]')).to_contain_text("web reverted to s-off, now shut off")
    assert state["sent"][-1][:2] == ("POST", "vms/node2/web/snapshots/s-off/revert")

    # supprimer un parent : ses enfants sont annoncés
    panel.locator('tr[data-ks-row="s-run"] [data-ks-act="delete"]').click()
    expect(conf).to_contain_text("Delete the snapshot s-run? Its children (s-off) are attached to its parent.")
    conf.locator('[data-ks="yes"]').click()
    expect(panel.locator('[data-ks="msg"]')).to_contain_text("Snapshot s-run deleted")
    assert state["sent"][-1][:2] == ("DELETE", "vms/node2/web/snapshots/s-run")
    assert dialogs == []


def test_snapshot_refusal_reasons_disable_create(ui):
    page, state, _ = ui
    panel = open_op(page, "rawvm", "snapshots", ".kvm-snapshots")
    expect(panel.locator('[data-ks="support"]')).to_contain_text("raw disk vda: internal snapshots need qcow2")
    go = panel.locator('[data-ks="go"]')
    expect(go).to_be_disabled()
    assert go.get_attribute("data-tip") == "raw disk vda: internal snapshots need qcow2"
    expect(panel.locator('[data-ks="pause"]')).to_be_hidden()
    assert state["sent"] == []


def test_snapshot_window_from_the_kvm_console_button(ui):
    page, _, _ = ui
    page.evaluate("window.KVMSnapshots.open('node2', 'web')")
    panel = page.locator(".floating-panel").filter(has=page.locator(".kvm-snapshots")).last
    expect(panel.locator("tr[data-ks-row]")).to_have_count(3)
    expect(panel.locator(".floating-panel-title")).to_have_text("Snapshots: web on node2")


# --- clonage ---------------------------------------------------------------------------------

def test_clone_window_previews_then_copies(ui):
    page, state, dialogs = ui
    panel = open_op(page, "db", "clone", ".kvm-clone")
    plan = panel.locator('[data-kc="plan"]')
    expect(plan).to_contain_text("The copy db-copy is possible.")
    expect(plan.locator(".kvm-clone-disks")).to_contain_text("/var/lib/libvirt/images/db-copy-vda.qcow2")
    expect(plan.locator(".kvm-clone-disks")).to_contain_text("1.0 GiB")
    expect(plan).to_contain_text("/iso/cirros.iso")                       # disque gardé partagé
    expect(plan).to_contain_text("new uuid")
    expect(plan).to_contain_text("new MAC address for 1 network card(s)")
    expect(plan).to_contain_text("new UEFI variables file")
    expect(plan).to_contain_text("disk serial of vda removed")
    expect(plan).to_contain_text("The UEFI boot entries are not copied")
    go = panel.locator('[data-kc="go"]')
    expect(go).to_be_enabled()
    # un autre nom : l'aperçu est à refaire
    name = panel.locator('input[name="name"]')
    name.fill("web")
    expect(go).to_be_disabled()
    assert go.get_attribute("data-tip") == "Preview the copy under this name first"
    panel.locator('[data-kc="preview"]').click()
    expect(plan).to_contain_text("a VM named web already exists")
    expect(go).to_be_disabled()
    assert state["sent"] == []
    name.fill("db-2")
    panel.locator('[data-kc="preview"]').click()
    expect(plan).to_contain_text("The copy db-2 is possible.")
    go.click()
    msg = panel.locator('[data-kc="msg"]')
    expect(msg).to_contain_text("db-2 created as a copy of db in 10.5 s")
    assert state["sent"][-1] == ("POST", "vms/node2/db/clone", {"name": "db-2"})
    steps = panel.locator('[data-kc="steps"] li')
    expect(steps).to_have_count(4)                                         # une ligne par étape
    expect(panel.locator('[data-kc="steps"] li[data-step="copy-vda"]')).to_contain_text("copied")
    res = panel.locator('[data-kc="result"]')
    expect(res).to_contain_text("db-2: uuid c0ffee00-1111, shut off")
    expect(res).to_contain_text("52:54:00:12:34:56")
    expect(res).to_contain_text("owner not copied (chown needs root)")
    expect(go).to_be_disabled()
    assert dialogs == []


# --- suppression ------------------------------------------------------------------------------

def test_delete_needs_the_preview_the_acknowledgement_and_the_typed_name(ui):
    page, state, dialogs = ui
    panel = open_op(page, "db", "delete", ".kvm-delete")
    plan = panel.locator('[data-kd="plan"]')
    go = panel.locator('[data-kd="go"]')
    # sans les disques : tout est gardé, la suppression reste possible
    expect(plan).to_contain_text("not requested")
    expect(plan).to_contain_text("2 internal snapshot(s) are deleted with the VM")
    expect(plan).to_contain_text("The vTPM state is removed with the VM")
    expect(plan).to_contain_text("db_VARS.fd")
    expect(go).to_be_disabled()
    # avec les disques : un fichier illisible bloque tant qu'il n'est pas accepté
    panel.locator('input[name="storage"]').check()
    expect(plan.locator('[data-kd-list="unreadable"]')).to_contain_text(UNREAD)
    expect(plan.locator(".kvm-op-refusals")).to_contain_text("could not be proven unused")
    expect(plan.locator('[data-kd-list="keep"]')).to_contain_text("could not prove that no other VM uses it")
    panel.locator('input[name="confirm"]').fill("db")
    expect(go).to_be_disabled()
    assert go.get_attribute("data-tip") == "The preview has refusals: see them above"
    ack = plan.locator(f'input[data-kd-ack="{UNREAD}"]')
    assert ack.get_attribute("data-tip")
    ack.check()
    expect(plan.locator('[data-kd-list="delete"]')).to_contain_text("/var/lib/libvirt/images/db.qcow2")
    expect(plan.locator('[data-kd-list="keep"]')).to_contain_text("shared with web")
    expect(plan.locator(f'input[data-kd-ack="{UNREAD}"]')).to_be_checked()
    assert any(g[1].get("acknowledge") == [UNREAD] and g[1].get("remove_storage") == ["1"] for g in state["gets"])
    expect(go).to_be_enabled()
    # un nom mal saisi : grisé
    panel.locator('input[name="confirm"]').fill("DB")
    expect(go).to_be_disabled()
    assert state["deletes"] == 0
    panel.locator('input[name="confirm"]').fill("db")

    # l'hôte change entre l'aperçu et la suppression : nouvel aperçu montré
    go.click()
    expect(panel.locator('[data-kd="msg"]')).to_contain_text("The host changed since the preview")
    assert state["sent"][-1] == ("DELETE", "vms/node2/db", {"confirm": "db", "expect_delete": D2, "remove_storage": True,
                                                            "acknowledge_unreadable": [UNREAD]})
    expect(plan.locator('[data-kd-list="delete"]')).to_contain_text("/var/lib/libvirt/images/shared.qcow2")
    expect(panel.locator('input[name="confirm"]')).to_have_value("")
    expect(go).to_be_disabled()
    panel.locator('input[name="confirm"]').fill("db")
    go.click()
    expect(panel.locator('[data-kd="msg"]')).to_contain_text("db deleted from node2")
    assert state["sent"][-1] == ("DELETE", "vms/node2/db", {"confirm": "db", "expect_delete": D3, "remove_storage": True,
                                                            "acknowledge_unreadable": [UNREAD]})
    expect(panel.locator('[data-kd="result"]')).to_contain_text("shared.qcow2")
    expect(go).to_be_disabled()
    assert dialogs == []


def test_an_operator_sees_the_delete_preview_but_cannot_delete(ui):
    page, state, _ = ui
    page.evaluate("Router.go('#vms/host/kvm')")   # 2.0.0 : KVM > Virtual machines, par son adresse
    expect(page.locator('#tab-kvmvms tr[data-kvm-vm="node2/db"]')).to_be_visible()
    state["role"] = "operator"
    page.evaluate("window.KVMVm.openDelete('node2', 'db')")
    panel = page.locator(".floating-panel").filter(has=page.locator(".kvm-delete")).last
    expect(panel.locator('[data-kd="plan"]')).to_contain_text("not requested")
    panel.locator('input[name="confirm"]').fill("db")
    go = panel.locator('[data-kd="go"]')
    expect(go).to_be_disabled()
    assert go.get_attribute("data-tip") == "Needs the admin role"


# --- noms hostiles ---------------------------------------------------------------------------

def test_hostile_vm_and_snapshot_names_are_text_everywhere(ui):
    page, state, dialogs = ui
    snaps = open_op(page, EVIL, "snapshots", ".kvm-snapshots")
    expect(snaps.locator(".floating-panel-title")).to_have_text(f"Snapshots: {EVIL} on node2")
    row = snaps.locator("tr[data-ks-row]")
    expect(row).to_contain_text(EVIL_SNAP)
    row.locator('[data-ks-act="delete"]').click()
    expect(snaps.locator('[data-ks="confirm"]')).to_contain_text(f"Delete the snapshot {EVIL_SNAP}?")
    snaps.locator('[data-ks="yes"]').click()
    expect(snaps.locator('[data-ks="msg"]')).to_contain_text(f"Snapshot {EVIL_SNAP} deleted")
    assert state["sent"][-1][:2] == ("DELETE", f"vms/node2/{EVIL}/snapshots/{EVIL_SNAP}")
    clone = open_op(page, EVIL, "clone", ".kvm-clone")
    expect(clone.locator('[data-kc="plan"]')).to_contain_text(f"{EVIL}-copy")
    dele = open_op(page, EVIL, "delete", ".kvm-delete")
    expect(dele.locator('[data-kd="typeLabel"]')).to_have_text(f"Type {EVIL} to confirm")
    expect(dele.locator('[data-kd="plan"]')).to_contain_text(f"/var/lib/libvirt/images/{EVIL}.qcow2")
    dele.locator('input[name="confirm"]').fill(EVIL)
    expect(dele.locator('[data-kd="go"]')).to_be_enabled()
    assert page.locator(".floating-panel img").count() == 0
    assert page.evaluate("window.__xss") is None
    assert dialogs == []


def run_entry(rid, label, status):
    return {"id": rid, "action": label, "cluster": "kvm:node2", "status": status,
            "exit_code": None if status == "running" else 1, "started_at": time.time() - 5,
            "ended_at": None if status == "running" else time.time() - 1, "dry_run": False,
            "error_summary": None if status == "running" else f"failed on {EVIL}", "cluster_user": None,
            "progress": {}, "progress_current": None, "result": {}}


@pytest.mark.parametrize("verb", ["snapshot-delete", "clone", "delete"])
def test_the_dock_shows_the_new_labels_as_text(context, flask_server, verb):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_dock_visible','true');"
                            "localStorage.setItem('harvester_ops_dock_collapsed','false');")
    page = context.new_page()
    label = f"kvm:vm-{verb}:node2/{EVIL}"
    live = run_entry("kvmxss000197", label, "running")
    page.route("**/api/activity*", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps({
        "in_progress": [live], "actions_done": [], "log_files": [], "total": 1,
        "facets": {"clusters": ["kvm:node2"], "statuses": ["running"], "actions": [label]}})))
    page.route("**/api/stream/*", lambda r, q: r.fulfill(status=200, content_type="text/event-stream", body=(
        "event: step\ndata: " + json.dumps({"step_id": "plan", "status": "running",
                                            "message": f"snapshot {EVIL_SNAP} of {EVIL}", "ts": 1}) + "\n\n")))
    goto(page, flask_server["base_url"], "#activity")   # 2.0.0 : écran ouvert par son adresse
    card = page.locator("#dock-card-kvmxss000197")
    expect(card).to_be_visible(timeout=10000)
    expect(card.locator(".name")).to_contain_text(EVIL)
    expect(card.locator(".step-msg")).to_contain_text(f"snapshot {EVIL_SNAP} of {EVIL}", timeout=10000)
    assert page.locator("img[src='x']").count() == 0 and page.evaluate("window.__xss") is None


@pytest.mark.parametrize("failures,rows", [(1, 3), (2, 0)])
def test_the_reload_after_an_action_tries_once_more(ui, failures, rows):
    """Relecture juste après une action : un échec passager (vu sur node2
    juste après un retour) est relu une fois, 2 s plus tard ; un second
    échec est dit tel quel."""
    page, state, _ = ui
    panel = open_op(page, "web", "snapshots", ".kvm-snapshots")
    expect(panel.locator("tr[data-ks-row]")).to_have_count(3)
    panel.locator('tr[data-ks-row="s-run"] [data-ks-act="revert"]').click()
    expect(panel.locator('[data-ks="confirm"]')).to_be_visible()
    # après la relecture de la confirmation : seules les relectures d'après l'action échouent
    state["fail_lists"] = failures
    panel.locator('[data-ks="confirm"] [data-ks="yes"]').click()
    expect(panel.locator('[data-ks="msg"]')).to_contain_text("web reverted to s-run")
    if rows:
        expect(panel.locator("tr[data-ks-row]")).to_have_count(rows, timeout=6000)
    else:
        expect(panel.locator('[data-ks="list"] .res-error')).to_contain_text("domain is busy", timeout=6000)
    assert state["fail_lists"] == 0


@pytest.mark.parametrize("shown,now,fail,warned", [
    # affichée arrêtée, démarrée depuis : la relecture fait prévenir
    ("shut off", "running", False, True),
    # affichée en marche, arrêtée depuis : plus rien à éteindre
    ("running", "shut off", False, False),
    ("running", "running", False, True),
    # relecture en échec : état inconnu, on prévient quand même
    ("shut off", "shut off", True, True),
])
def test_revert_confirmation_rereads_the_state(ui, shown, now, fail, warned):
    """La mise hors tension n'est annoncée qu'après avoir relu l'état de la VM
    (instantané pris arrêté)."""
    page, state, _ = ui
    state["vm_state"] = {"web": shown}
    panel = open_op(page, "web", "snapshots", ".kvm-snapshots")
    expect(panel.locator("tr[data-ks-row]")).to_have_count(3)
    state["vm_state"] = {"web": now}
    if fail:
        state["fail_lists"] = 1
    before = len([g for g in state["gets"] if g[0] == "vms/node2/web/snapshots"])
    panel.locator('tr[data-ks-row="s-off"] [data-ks-act="revert"]').click()
    conf = panel.locator('[data-ks="confirm"]')
    expect(conf).to_contain_text("Revert web to s-off?")
    assert len([g for g in state["gets"] if g[0] == "vms/node2/web/snapshots"]) == before + 1
    power = "reverting powers it off"
    if warned:
        expect(conf).to_contain_text(power)
    else:
        expect(conf).not_to_contain_text(power)


def test_reads_after_an_action_are_chained_not_concurrent(ui):
    """Fin d'une action : la fenêtre relit ses instantanés, PUIS la liste des
    VMs, jamais les deux en même temps (deux lectures simultanées sur la même
    connexion SSH partagée ont fait tomber le sshd de node2)."""
    page, state, _ = ui
    panel = open_op(page, "web", "snapshots", ".kvm-snapshots")
    expect(panel.locator("tr[data-ks-row]")).to_have_count(3)
    # chaque lecture KVM est ralentie et suivie : nombre en vol, ordre
    page.evaluate("""() => {
      const real = window.fetch.bind(window);
      window.__kvmReads = { inflight: 0, max: 0, log: [] };
      window.fetch = async (url, opts) => {
        const u = String(url);
        const m = (opts && opts.method) || 'GET';
        if (!u.includes('/api/kvm/') || m !== 'GET') return real(url, opts);
        const r = window.__kvmReads;
        r.inflight += 1; r.max = Math.max(r.max, r.inflight); r.log.push('start ' + u);
        try {
          await new Promise((ok) => setTimeout(ok, 300));
          return await real(url, opts);
        } finally { r.inflight -= 1; r.log.push('end ' + u); }
      };
    }""")
    panel.locator('tr[data-ks-row="s-run"] [data-ks-act="delete"]').click()
    expect(panel.locator('[data-ks="confirm"]')).to_be_visible()
    panel.locator('[data-ks="confirm"] [data-ks="yes"]').click()
    expect(panel.locator('[data-ks="msg"]')).to_contain_text("Snapshot s-run deleted")
    # la liste des VMs relue (fin de la chaîne)
    for _ in range(100):
        reads = page.evaluate("() => window.__kvmReads")
        if any(x.startswith("end ") and re.search(r"/api/kvm/vms(\?|$)", x) for x in reads["log"]):
            break
        page.wait_for_timeout(50)
    reads = page.evaluate("window.__kvmReads")
    assert reads["max"] == 1, reads["log"]
    log = [x.split("/api/kvm/", 1)[0] + x.split("/api/kvm/", 1)[1].split("?")[0] for x in reads["log"]]
    snap_end = log.index("end vms/node2/web/snapshots")
    vms_start = log.index("start vms")
    assert snap_end < vms_start, reads["log"]


def test_the_windows_reuse_the_follow_of_kvm_js(ui):
    page, _, _ = ui
    assert page.evaluate("typeof window.KVM.follow") == "function"
