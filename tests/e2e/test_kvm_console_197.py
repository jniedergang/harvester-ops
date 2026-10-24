"""v1.97.0 : la console VNC d'une VM KVM, dans un vrai navigateur, API simulée.

Ouverte depuis le menu d'actions d'une VM KVM, elle demande un ticket à
/api/kvm/..., ouvre le websocket indiqué par `ws_path` et parle RFB à une
doublure de WebSocket qui joue un QEMU en sécurité None (comme la console
côté serveur le fait toujours vers les navigateurs). Ce que ces tests
tiennent : ouverture depuis le menu, partage dit (« shared by 2 viewers »),
affichage SPICE seul dit sans nouvel essai, mot de passe refusé dit sans
boucle, nom de domaine hostile affiché littéralement, arrêt forcé confirmé
dans la page (jamais `confirm()`), console grisée pour un lecteur.
"""

import json
import re
from urllib.parse import unquote

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

EVIL = "<img src=x onerror=window.__xss=1>"
GESTURES = {"destroy": ["shut off"], "reboot": ["paused", "shut off"],
            "resume": ["blocked", "crashed", "idle", "in shutdown", "running", "shut off"],
            "shutdown": ["paused", "shut off"],
            "start": ["crashed", "idle", "in shutdown", "paused", "pmsuspended", "running"],
            "suspend": ["paused", "pmsuspended", "shut off"]}

# Une doublure de WebSocket qui fait QEMU : version, sécurité None,
# ServerInit ; le reste (formats, demandes d'image) est absorbé.
FAKE_RFB_WS = r"""
window.__wsUrls = [];
window.WebSocket = class {
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
  constructor(url) {
    this.onopen = null; this.onmessage = null; this.onerror = null; this.onclose = null;
    this.url = url; this.readyState = 0; this.binaryType = 'arraybuffer'; this.protocol = '';
    this.step = 0;
    window.__wsUrls.push(url); window.__ws = this;
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen({});
      this.push(new TextEncoder().encode('RFB 003.008\n')); }, 20);
  }
  push(bytes) {
    setTimeout(() => this.onmessage && this.onmessage({ data: bytes.buffer.slice(0) }), 5);
  }
  send(data) {
    this.step += 1;
    if (this.step === 1) this.push(new Uint8Array([1, 1]));            // une sécurité : None
    else if (this.step === 2) this.push(new Uint8Array([0, 0, 0, 0]));  // résultat OK
    else if (this.step === 3) {                                          // ClientInit -> ServerInit
      const name = new TextEncoder().encode('kvmtest');
      const b = new Uint8Array(24 + name.length);
      const v = new DataView(b.buffer);
      v.setUint16(0, 64); v.setUint16(2, 48);
      b.set([32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0], 4);
      v.setUint32(20, name.length); b.set(name, 24);
      this.push(b);
    }
  }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000, reason: '', wasClean: true }); }
  addEventListener() {} removeEventListener() {}
};
"""


def vm(name, state):
    return {"host": "node2", "name": name, "id": 1 if state == "running" else None, "state": state,
            "persistent": True, "autostart": False, "vcpus": 1, "memory_kib": 1048576,
            "memory_max_kib": 1048576, "cpu_time_ns": None}


VMS = [vm("kvmtest-cirros", "running"), vm("spicey", "running"), vm("locked", "running"), vm(EVIL, "running")]


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(FAKE_RFB_WS)
    context.add_init_script(
        "if (!sessionStorage.getItem('kvmc-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_tab','overview');"
        "sessionStorage.setItem('kvmc-init','1'); }")
    state = {"role": "admin", "tickets": [], "posts": [], "status": {"count": 1, "viewers": ["admin"]},
             "last_close": None}

    def ok(route, body, status=200):
        return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def api(route, req):
        path = unquote(req.url.split("/api/kvm/", 1)[1].split("?", 1)[0])
        if req.method == "GET":
            if path == "vms":
                return ok(route, {"vms": VMS, "role": state["role"], "gestures": GESTURES, "warnings": [],
                                  "hosts": [{"name": "node2", "state": "ok", "count": len(VMS)}]})
            if path.endswith("/console-status"):
                return ok(route, dict(state["status"], last_close=state["last_close"]))
            if path == "hosts":
                return ok(route, {"hosts": [], "warnings": [], "role": state["role"], "gestures": GESTURES})
            return ok(route, {"error": "not found"}, 404)
        if path.endswith("/console-ticket"):
            name = path.split("/")[2]
            state["tickets"].append(name)
            if name == "spicey":
                return ok(route, {"error": "this VM has only a SPICE display: not supported", "reason": "spice",
                                  "terminal": True}, 422)
            if name == "locked" and len(state["tickets"]) > 1:
                return ok(route, {"error": "x", "reason": "notrunning", "terminal": False}, 409)
            return ok(route, {"ticket": f"t{len(state['tickets'])}", "expires_in": 30, "password": name == "locked",
                              "ws_path": "/ws/kvm/vnc/node2/" + req.url.split("/vms/node2/", 1)[1].split("/console-ticket")[0]})
        state["posts"].append(path)
        return ok(route, {"action_id": f"kvm{len(state['posts']):09d}"}, 202)

    def make_page():
        page = context.new_page()
        page.route(re.compile(r".*/api/kvm/.*"), api)
        page.route(re.compile(r".*/api/stream/kvm\d+$"), lambda r, q: r.fulfill(
            status=200, content_type="text/event-stream",
            body='event: end\ndata: {"status": "done", "result": {"output": {"ok": true}}}\n\n'))
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        page.goto(flask_server["base_url"], wait_until="domcontentloaded")
        page.wait_for_function("window.KVM && window.App && window.VMConsole")
        page.mouse.move(1200, 600)
        return page, dialogs
    return make_page, state


def open_console_from_menu(page, name):
    page.locator('#tab-group-kvm .tab[data-tab="kvmvms"]').evaluate("el => el.click()")
    pane = page.locator("#tab-kvmvms")
    row = pane.locator(f'tr[data-kvm-vm="node2/{name}"]')
    expect(row).to_be_visible()
    row.locator('[data-kvm="menu"]').click()
    item = page.locator(".kvm-menu [data-kvm-console]")
    expect(item).to_be_visible()
    assert item.get_attribute("data-tip")
    item.click()
    return page.locator(".floating-panel").filter(has=page.locator(".vm-console-wrap")).last


def test_the_kvm_console_opens_from_the_actions_menu(ui):
    make_page, state = ui
    page, dialogs = make_page()
    panel = open_console_from_menu(page, "kvmtest-cirros")
    expect(panel.locator(".floating-panel-title")).to_have_text("Console: kvmtest-cirros on node2")
    expect(panel.locator(".vm-console-status")).to_have_text("Connected", timeout=5000)
    urls = page.evaluate("window.__wsUrls")
    assert len(urls) == 1 and re.search(r"/ws/kvm/vnc/node2/kvmtest-cirros\?ticket=t1$", urls[0]), urls
    assert state["tickets"] == ["kvmtest-cirros"]
    # boutons de la cible KVM, chacun avec sa bulle ; pas de réglages avant 1.98
    for cls in ("vm-console-kvm-start", "vm-console-kvm-shutdown", "vm-console-kvm-destroy",
                "vm-console-kvm-snapshots"):
        assert panel.locator(f".{cls}").get_attribute("data-tip"), cls
    assert panel.locator(".vm-console-settings").count() == 0
    assert dialogs == []


def test_two_pages_share_the_session(ui):
    make_page, state = ui
    state["status"] = {"count": 2, "viewers": ["alice", "bob"]}
    pages = [make_page()[0] for _ in range(2)]
    for p in pages:
        panel = open_console_from_menu(p, "kvmtest-cirros")
        expect(panel.locator(".vm-console-status")).to_contain_text("shared by 2 viewers", timeout=5000)
        expect(panel.locator(".vm-console-status")).to_contain_text("alice, bob")


def test_a_spice_only_display_is_said_without_retrying(ui):
    make_page, state = ui
    page, _ = make_page()
    panel = open_console_from_menu(page, "spicey")
    status = panel.locator(".vm-console-status")
    expect(status).to_contain_text("only a SPICE display", timeout=5000)
    expect(status).to_contain_text("1.98")
    page.wait_for_timeout(2500)
    assert state["tickets"] == ["spicey"], "the console retried a terminal refusal"
    assert page.evaluate("window.__wsUrls") == []
    expect(panel.locator(".vm-console-reconnect")).to_be_visible()


def test_a_refused_password_is_said_without_looping(ui):
    make_page, state = ui
    page, _ = make_page()
    panel = open_console_from_menu(page, "locked")
    expect(panel.locator(".vm-console-status")).to_have_text("Connected", timeout=5000)
    # le serveur coupe et dit pourquoi : mot de passe refusé, pas de boucle
    state["last_close"] = {"reason": "auth", "detail": "the VM display refused the password", "at": 1}
    page.evaluate("window.__ws.close()")
    status = panel.locator(".vm-console-status")
    expect(status).to_contain_text("refused its password", timeout=5000)
    page.wait_for_timeout(1500)
    assert state["tickets"] == ["locked"], "the console looped on a refused password"
    expect(panel.locator(".vm-console-reconnect")).to_be_visible()


def test_hostile_domain_name_is_shown_literally(ui):
    make_page, _ = ui
    page, dialogs = make_page()
    panel = open_console_from_menu(page, EVIL)
    expect(panel.locator(".floating-panel-title")).to_have_text(f"Console: {EVIL} on node2")
    expect(panel.locator(".vm-console-status")).to_have_text("Connected", timeout=5000)
    assert page.locator(".floating-panel img").count() == 0
    assert page.evaluate("window.__xss") is None
    urls = page.evaluate("window.__wsUrls")
    assert "/ws/kvm/vnc/node2/%3Cimg%20src%3Dx%20onerror%3Dwindow.__xss%3D1%3E?ticket=" in urls[0], urls
    assert dialogs == []


def test_force_off_from_the_console_is_confirmed_in_the_page(ui):
    make_page, state = ui
    page, dialogs = make_page()
    panel = open_console_from_menu(page, "kvmtest-cirros")
    expect(panel.locator(".vm-console-status")).to_have_text("Connected", timeout=5000)
    panel.locator(".vm-console-kvm-destroy").click()
    box = panel.locator(".vm-console-confirm")
    expect(box).to_be_visible()
    expect(box).to_contain_text("Force off kvmtest-cirros on node2?")
    box.locator(".vm-console-destroy-no").click()
    expect(box).to_be_hidden()
    assert state["posts"] == []
    panel.locator(".vm-console-kvm-destroy").click()
    box.locator(".vm-console-destroy-yes").click()
    page.wait_for_timeout(300)
    assert state["posts"] == ["vms/node2/kvmtest-cirros/destroy"]
    panel.locator(".vm-console-kvm-shutdown").click()
    page.wait_for_timeout(300)
    assert state["posts"][-1] == "vms/node2/kvmtest-cirros/shutdown"
    assert dialogs == []


def test_a_viewer_sees_the_console_item_disabled(ui):
    make_page, state = ui
    state["role"] = "viewer"
    page, _ = make_page()
    page.locator('#tab-group-kvm .tab[data-tab="kvmvms"]').evaluate("el => el.click()")
    pane = page.locator("#tab-kvmvms")
    pane.locator('tr[data-kvm-vm="node2/kvmtest-cirros"] [data-kvm="menu"]').click()
    item = page.locator(".kvm-menu [data-kvm-console]")
    expect(item).to_have_attribute("aria-disabled", "true")
    item.click(force=True)
    page.wait_for_timeout(300)
    assert state["tickets"] == []
