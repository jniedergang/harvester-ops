"""Prochaine version KVM, tâche 6 : Images, Stockage et Réseaux, API simulée.

Vues Stockage (sous-onglets Pools et volumes, Images) et Réseaux, et les
fenêtres de kvm-storage.js : téléversement en deux temps (progression,
annulation pendant la réception et pendant l'envoi), refus du téléchargement
d'une URL, suppressions avec aperçu (raisons d'usage, fichiers illisibles à
accepter, empreinte renvoyée), refus de création d'un pool et d'un réseau
qui chevauche le LAN, gestes simples (arrêt confirmé, refus montrés), VM
créée depuis une image (premier disque prérempli), rôles, noms hostiles."""

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import kvm_form_fakes as F  # noqa: E402

EVIL_POOL = F.EVIL_POOL
EVIL_NET = F.EVIL_NET
EVIL_VOL = "v<img src=x onerror=window.__xss=3>.qcow2"
EVIL_IMG = F.EVIL_IMG
DIG = "d" * 64
UNREAD = "/var/lib/libvirt/qemu/locked.xml"

DETAIL = {"name": "node2", "address": "172.16.1.12", "vms": [], "errors": [],
          "pools": [{"name": "images", "state": "running", "autostart": True, "persistent": True,
                     "capacity": 900 * 2 ** 30, "allocation": 100 * 2 ** 30, "available": 800 * 2 ** 30,
                     "volumes": [{"name": "cirros.qcow2", "path": "/var/lib/libvirt/images/cirros.qcow2", "type": "file",
                                  "capacity": 117440512, "allocation": 20 * 2 ** 20},
                                 {"name": EVIL_VOL, "path": "/var/lib/libvirt/images/evil", "type": "file",
                                  "capacity": 2 ** 30, "allocation": 0},
                                 {"name": "kvmtest", "path": "/var/lib/libvirt/images/kvmtest", "type": "dir",
                                  "capacity": 4096, "allocation": 4096}]},
                    {"name": EVIL_POOL, "state": "running", "autostart": False, "persistent": True,
                     "capacity": 2 ** 30, "allocation": 0, "available": 2 ** 30, "volumes": []},
                    {"name": "off", "state": "inactive", "autostart": False, "persistent": True,
                     "capacity": None, "allocation": None, "available": None, "volumes": []}],
          "networks": [{"name": "default", "active": True, "autostart": True, "persistent": True, "forward": "nat",
                        "bridge": "virbr0", "ips": [{"family": "ipv4", "address": "192.168.122.1", "prefix": 24,
                                                     "dhcp_ranges": [{"start": "192.168.122.2", "end": "192.168.122.254"}]}]},
                       {"name": EVIL_NET, "active": False, "autostart": False, "persistent": True, "forward": "isolated",
                        "bridge": "virbr9", "ips": []}]}
POOLS = [{"name": "images", "type": "dir", "target_path": "/var/lib/libvirt/images", "state": "running", "autostart": True},
         {"name": EVIL_POOL, "type": "dir", "target_path": "/srv/evil", "state": "running", "autostart": False},
         {"name": "off", "type": "dir", "target_path": "/srv/off", "state": "inactive", "autostart": False}]
LINKS = {"bridges": [{"name": "br0", "state": "UP"}], "physical": [{"name": "eno1", "state": "UP", "master": "br0"}],
         "links": ["lo", "eno1", "br0", "virbr0"],
         "networks": [{"dev": "br0", "source": "address", "network": "172.16.0.0/16"},
                      {"dev": "virbr0", "source": "address", "network": "192.168.122.0/24"},
                      {"dev": "wg0", "source": "route", "network": "192.168.100.0/24"}]}
IMAGES = {"host": "node2", "image_pools": ["images"], "pools": ["images"], "missing_pools": [], "unknown": [],
          "survey_age_s": 3.0,
          "images": [{"pool": "images", "volume": "cirros.qcow2", "path": "/var/lib/libvirt/images/cirros.qcow2",
                      "format": "qcow2", "iso": False, "capacity": 117440512, "allocation": 20 * 2 ** 20,
                      "sha256": "a" * 64, "url": "https://download.example/cirros.img", "date": "2026-10-24",
                      "author": "ju", "used_by": [], "backing_users": ["/var/lib/libvirt/images/vm1-vda.qcow2",
                                                                       "/var/lib/libvirt/images/vm2-vda.qcow2"]},
                     {"pool": "images", "volume": "netinst.iso", "path": "/var/lib/libvirt/images/netinst.iso",
                      "format": "raw", "iso": True, "capacity": 650 * 2 ** 20, "allocation": 650 * 2 ** 20,
                      "sha256": None, "url": None, "date": None, "author": None, "used_by": ["web"], "backing_users": ["unknown"]},
                     {"pool": "images", "volume": EVIL_IMG, "path": "/var/lib/libvirt/images/evil.qcow2",
                      "format": "qcow2", "iso": False, "capacity": 2 ** 30, "allocation": 0, "sha256": None,
                      "url": "https://user:pw@mirror.example/x.qcow2?token=S3CRETTOKEN#frag", "date": None,
                      "author": None, "used_by": [], "backing_users": []}]}

FAKE_XHR = """
window.__xhrs = [];
window.__events = [];
class FakeXHR {
  constructor() { this.upload = {}; this.headers = {}; this.aborted = false; window.__xhrs.push(this); }
  open(m, u) { this.method = m; this.url = u; }
  setRequestHeader(k, v) { this.headers[k] = v; }
  send(body) { this.size = body && body.size; this.name = body && body.name; }
  abort() { this.aborted = true; window.__events.push('abort'); if (this.onabort) this.onabort(); }
  _progress(loaded, total) { this.upload.onprogress({ lengthComputable: true, loaded, total }); }
  _sent() { window.__events.push('last-byte'); if (this.upload.onload) this.upload.onload(); }
  _done(status, body) { this.status = status; this.responseText = JSON.stringify(body); this.onload(); }
}
window.XMLHttpRequest = FakeXHR;
// serveur contrôlable : l'annulation par jeton est notée, et peut être
// retenue (`__holdCancel`) jusqu'à `__releaseCancel()` pour jouer la course
// avec le dernier octet
const realFetch = window.fetch.bind(window);
window.__holdCancel = false;
window.fetch = (u, o) => {
  if (!String(u).includes('/api/kvm/uploads/')) return realFetch(u, o);
  window.__events.push('cancel:' + String(u));
  if (!window.__holdCancel) return realFetch(u, o);
  return new Promise((res) => { window.__releaseCancel = () => res(realFetch(u, o)); });
};
"""


def sse_events(events, end=None):
    body = "".join(f"event: {k}\ndata: {json.dumps(v)}\n\n" for k, v in events)
    if end is not None:
        body += f"event: end\ndata: {json.dumps(end)}\n\n"
    return body


def install(page, state):
    """Routes propres au stockage, posées APRÈS celles de kvm_form_fakes
    (prioritaires) ; tout le reste (capacités, modèles...) leur revient."""
    state.setdefault("pool_users", ["vm-a"])
    state.setdefault("deletes", [])
    state.setdefault("net_stop_refused", True)

    def ok(route, body, status=200):
        return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def new_action(steps, status="done", output=None, error=None, events=None):
        aid = f"kvm{len(state['sent']) + 500:09d}"
        ev = [("step", s) for s in steps] + list(events or [])
        state["streams"][aid] = sse_events(ev, {"status": status, "error_summary": error,
                                               "result": {"output": output or {}}})
        return aid

    def api(route, req):
        parts = urlsplit(req.url)
        path = unquote(parts.path.split("/api/kvm/", 1)[1])
        q = parse_qs(parts.query)
        m = req.method
        if m == "GET":
            if path == "hosts" and "declared=1" in req.url:
                return ok(route, {"hosts": [{"name": "node2"}, {"name": "node9"}], "warnings": [],
                                  "role": state["role"], "gestures": F.GESTURES})
            if path == "hosts/node2":
                return ok(route, DETAIL)
            if path == "hosts/node9":
                return ok(route, {"name": "node9", "error": {"kind": "unreachable", "message": "node9: unreachable"}}, 502)
            if path == "hosts/node2/pools":
                return ok(route, {"host": "node2", "pools": POOLS, "role": state["role"]})
            if path == "hosts/node2/images":
                state.setdefault("image_gets", []).append(req.url)
                return ok(route, dict(IMAGES, role=state["role"], unknown=state.get("images_unknown", [])))
            if path == "hosts/node2/links":
                return ok(route, dict(LINKS, host="node2", role=state["role"]))
            if path == "hosts/node2/networks":
                return ok(route, {"host": "node2", "networks": DETAIL["networks"], "role": state["role"]})
            if path.startswith("hosts/node9/"):
                return ok(route, {"error": "node9: unreachable", "kind": "unreachable"}, 502)
            mm = re.fullmatch(r"hosts/node2/volumes/(.+)/(.+)/delete-plan", path)
            if mm:
                acked = q.get("acknowledge", [])
                refusals = [] if UNREAD in acked else [f"could not prove that no VM uses it (files the SSH user cannot read: {UNREAD})"]
                if mm.group(2) == "cirros.qcow2":
                    refusals = ["backing file of /var/lib/libvirt/images/vm1-vda.qcow2"]
                state.setdefault("plans", []).append(("volume", mm.group(2), acked))
                return ok(route, {"host": "node2", "pool": mm.group(1), "volume": mm.group(2), "possible": not refusals,
                                  "role": state["role"],
                                  "plan": {"pool": mm.group(1), "volume": mm.group(2), "path": f"/var/lib/libvirt/images/{mm.group(2)}",
                                           "refusals": refusals, "unreadable": [UNREAD],
                                           "acknowledged_unreadable": [UNREAD] if UNREAD in acked else [], "digest": DIG}})
            mm = re.fullmatch(r"hosts/node2/pools/(.+)/delete-plan", path)
            if mm:
                state.setdefault("plan_times", []).append(time.monotonic())
            if mm and state.get("plan_busy"):
                state["plan_busy"] -= 1
                return ok(route, {"error": "node2: 2 disk surveys are already running on this host; try again in a few seconds",
                                  "kind": "busy"}, 429)
            if mm:
                remove_dir = q.get("remove_dir") == ["1"]
                users = state["pool_users"]
                refusals = [f"used by {', '.join(users)}"] if users else []
                state.setdefault("plans", []).append(("pool", mm.group(1), remove_dir))
                return ok(route, {"host": "node2", "pool": mm.group(1), "possible": not refusals, "role": state["role"],
                                  "plan": {"name": mm.group(1), "remove_dir": remove_dir, "volumes": [] if remove_dir else None,
                                           "state": {"type": "dir", "target_path": "/srv/evil", "state": "running"},
                                           "refusals": refusals, "unreadable": [], "digest": DIG}})
            mm = re.fullmatch(r"hosts/node2/networks/(.+)/delete-plan", path)
            if mm:
                return ok(route, {"host": "node2", "network": mm.group(1), "possible": False, "role": state["role"],
                                  "plan": {"name": mm.group(1), "network": {"forward": "isolated", "bridge": "virbr9"},
                                           "refusals": ["used by web, db"], "digest": DIG}})
            return route.fallback()
        mm = re.fullmatch(r"uploads/([0-9a-f]{32})/cancel", path)
        if mm and m == "POST":
            state.setdefault("upload_cancels", []).append(mm.group(1))
            code, resp = state.get("upload_cancel", (202, {"phase": "receive", "cancel_requested": True}))
            return ok(route, resp, code)
        body = json.loads(req.post_data or "{}")
        if not re.fullmatch(r"hosts/node2/(pools|networks|volumes|images|image-pools)(/.*)?", path):
            return route.fallback()
        state["sent"].append((m, path, body))
        if path == "hosts/node2/pools" and body.get("dry_run"):
            spec = body["spec"]
            if spec.get("path", "").startswith("/etc"):
                return ok(route, {"host": "node2", "possible": False, "kind": "refused", "error": "x",
                                  "refusals": [f"the target path {spec['path']} is or contains a system directory (/etc)"]})
            return ok(route, {"host": "node2", "possible": True, "name": spec["name"], "dry_run": True, "xml": "<pool/>"})
        if path == "hosts/node2/networks" and body.get("dry_run"):
            spec = body["spec"]
            if spec.get("subnet") == "172.16.0.0/16":
                return ok(route, {"host": "node2", "possible": False, "kind": "refused", "error": "x",
                                  "refusals": ["the subnet overlaps 172.16.0.0/16 (address of br0)"]})
            return ok(route, {"host": "node2", "possible": True, "name": spec["name"], "dry_run": True, "xml": "<network/>"})
        if path in ("hosts/node2/pools", "hosts/node2/networks"):
            aid = new_action([{"step_id": "plan", "status": "done", "message": "ok"},
                              {"step_id": "define", "status": "done", "message": body["spec"]["name"]}],
                             output={"ok": True, "name": body["spec"]["name"]})
            return ok(route, {"action_id": aid}, 202)
        if path == "hosts/node2/images/fetch":
            if "169.254.169.254" in body.get("url", ""):
                return ok(route, {"error": "169.254.169.254 is a metadata address: refused (cloud metadata, unspecified, "
                                           "multicast and broadcast addresses are never reachable)",
                                  "reason": "address", "kind": "refused"}, 400)
            aid = new_action([{"step_id": "download", "status": "done", "message": "12 MiB"}],
                             events=[("progress", {"phase": "download", "done": 50, "total": 100, "rate": 10, "eta": 5, "elapsed": 1}),
                                     ("progress", {"phase": "upload", "done": 100, "total": 100, "rate": 10, "eta": 0, "elapsed": 2, "final": True})],
                             output={"ok": True, "pool": body["pool"], "volume": body["name"], "sha256": "c" * 64,
                                     "hosts": [{"host": "node2", "ok": True}], "url": "https://mirror.example/x.qcow2"})
            return ok(route, {"action_id": aid, "url": "https://mirror.example/x.qcow2"}, 202)
        if m == "DELETE":
            state["deletes"].append((path, body))
            aid = new_action([{"step_id": "plan", "status": "done", "message": "nothing uses it"},
                              {"step_id": "delete", "status": "done", "message": "deleted"}], output={"ok": True})
            return ok(route, {"action_id": aid}, 202)
        if path.endswith("/networks/default/stop") and state["net_stop_refused"]:
            aid = new_action([{"step_id": "stop", "status": "error", "message": "a running VM has a card on it (web)"}],
                             status="error", error="a running VM has a card on it (web)",
                             output={"ok": False, "kind": "refused", "error": "a running VM has a card on it (web)",
                                     "refusals": ["used by web"]})
            return ok(route, {"action_id": aid}, 202)
        if path.endswith("/verify"):
            aid = new_action([{"step_id": "verify", "status": "done", "message": "ok"}],
                             output={"ok": True, "sha256": "a" * 64, "expected": "a" * 64, "match": True, "skipped": None})
            return ok(route, {"action_id": aid}, 202)
        aid = new_action([{"step_id": "g", "status": "done", "message": "ok"}], output={"ok": True})
        return ok(route, {"action_id": aid}, 202)

    page.route(re.compile(r".*/api/kvm/.*"), api)
    page.route(re.compile(r".*/api/action/kvm\d+$"), lambda r, q: (state.setdefault("cancels", []).append((q.method, q.url)),
                                                                    r.fulfill(status=200, content_type="application/json", body="{}")))
    return state


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "if (!sessionStorage.getItem('kvms-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "sessionStorage.setItem('kvms-init','1'); }")
    context.add_init_script(FAKE_XHR)
    page = context.new_page()
    state = F.install(page, {"role": "admin"})
    install(page, state)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    goto(page, flask_server["base_url"], "#activity")   # 2.0.0 : écran ouvert par son adresse
    page.wait_for_function("window.KVM && window.App && window.KVMStorage && window.KVMForm")
    page.mouse.move(1200, 600)
    return page, state, dialogs


def open_tab(page, tab, host="node2"):
    # 2.0.0 : Stockage et Réseaux KVM sont des Paramètres de chaque hôte
    where = {"kvmstorage": "pools", "kvmnetworks": "networks"}[tab]
    page.evaluate(f"Router.go('#kvm/{host}/params/{where}')")
    page.evaluate("Router.idle()")
    pane = page.locator(f"#tab-{tab}")
    expect(pane.locator(".kvm-card")).to_be_visible()
    return pane


def open_images(page):
    pane = open_tab(page, "kvmstorage")
    pane.locator('[data-kvm="sub"][data-sub="images"]').click()
    block = pane.locator('[data-kvm-block="node2"]')
    expect(block.locator("table.kvm-images")).to_be_visible()
    return pane, block


def sel(attr, value):
    """Sélecteur d'attribut sûr pour un nom hostile (guillemets échappés)."""
    return f"[{attr}={json.dumps(value)}]"


def no_injection(page):
    assert page.evaluate("window.__xss") is None
    assert page.locator("#tab-kvmstorage img, #tab-kvmnetworks img, .floating-panel img").count() == 0


def tips_everywhere(scope):
    """Chaque bouton, champ et liste de la portée porte une bulle (data-tip)."""
    missing = scope.locator("button, select, input:not([type=hidden])").evaluate_all(
        "els => els.filter(e => !(e.closest('.tip') && (e.closest('.tip').dataset.tip || '').length)"
        " && !(e.dataset && e.dataset.tip)).map(e => e.outerHTML.slice(0, 120))")
    assert missing == []


# --- Stockage : pools et volumes ------------------------------------------------------

def test_pools_and_volumes_with_actions_by_role_and_hostile_names(ui):
    page, state, dialogs = ui
    pane = open_tab(page, "kvmstorage")
    block = pane.locator('[data-kvm-block="node2"]')
    row = block.locator('tr[data-kvm-pool="images"]')
    expect(row).to_contain_text("dir")
    expect(row.locator('[data-kvm="pool-verb"][data-verb="stop"]')).to_be_enabled()
    expect(block.locator('tr[data-kvm-pool="off"] [data-kvm="pool-verb"][data-verb="start"]')).to_be_enabled()
    # rafraîchir un pool arrêté : grisé avec la raison
    ref = block.locator('tr[data-kvm-pool="off"] [data-kvm="pool-verb"][data-verb="refresh"]')
    expect(ref).to_be_disabled()
    assert ref.get_attribute("data-tip") == "Start the pool first"
    row.locator("summary").click()
    vols = row.locator(".kvm-vols")
    expect(vols).to_contain_text(EVIL_VOL)
    # le répertoire d'un pool imbriqué : aucune action
    expect(vols.locator('tr[data-kvm-vol="kvmtest"]')).to_contain_text("directory of a nested pool")
    assert vols.locator('tr[data-kvm-vol="kvmtest"] button').count() == 0
    evil_row = block.locator('tr' + sel('data-kvm-pool', EVIL_POOL))
    expect(evil_row.locator("td").first.locator("strong")).to_have_text(EVIL_POOL)
    assert evil_row.locator("b").count() == 0
    tips_everywhere(pane.locator(".kvm-card"))
    # un hôte seulement : la page de node2 impose son hôte (aucun menu d'hôte)
    expect(pane.locator('[data-kvm-block="node9"]')).to_have_count(0)
    assert pane.locator('[data-kvm-filter="host"]').count() == 0
    no_injection(page)
    # nœud injoignable : sur sa page, le bloc le dit, sans bouton
    pane = open_tab(page, "kvmstorage", "node9")
    expect(pane.locator('[data-kvm-block="node9"] .res-error')).to_be_visible()
    pane = open_tab(page, "kvmstorage")
    block = pane.locator('[data-kvm-block="node2"]')
    # opérateur : gestes d'administrateur grisés avec la raison
    state["role"] = "operator"
    pane.locator('[data-kvm="refresh"]').click()
    stop = block.locator('tr[data-kvm-pool="images"] [data-kvm="pool-verb"][data-verb="stop"]')
    expect(stop).to_be_disabled()
    assert stop.get_attribute("data-tip") == "Needs the admin role"
    expect(block.locator('[data-kvm="pool-create"]')).to_be_disabled()
    expect(block.locator('[data-kvm="vol-create"]')).to_be_enabled()
    assert dialogs == []


def test_pool_create_refusal_shown_then_created(ui):
    page, state, _ = ui
    pane = open_tab(page, "kvmstorage")
    pane.locator('[data-kvm-block="node2"] [data-kvm="pool-create"]').click()
    win = page.locator(".floating-panel", has=page.locator(".kvms-pool-create"))
    f = win.locator("form")
    tips_everywhere(win)
    f.locator('[name="name"]').fill("kvmtest-pool")
    expect(f.locator('[name="path"]')).to_have_value("/var/lib/libvirt/images/kvmtest-pool")
    f.locator('[name="path"]').fill("/etc/kvm")
    f.locator('[data-ks="go"]').click()
    expect(win.locator('[data-ks="refusals"]')).to_contain_text("is or contains a system directory (/etc)")
    assert [b for (m, p, b) in state["sent"] if p == "hosts/node2/pools" and not b.get("dry_run")] == []
    f.locator('[name="path"]').fill("/var/lib/libvirt/images/kvmtest")
    f.locator('[data-ks="go"]').click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("Pool kvmtest-pool created on node2")
    sent = [b for (m, p, b) in state["sent"] if p == "hosts/node2/pools"]
    assert sent[-2] == {"spec": {"name": "kvmtest-pool", "type": "dir", "path": "/var/lib/libvirt/images/kvmtest",
                                 "autostart": True}, "dry_run": True}
    assert sent[-1] == {"spec": sent[-2]["spec"]}
    expect(win.locator(".kvm-steps li")).to_have_count(2)


def test_pool_delete_preview_shows_users_then_digest_with_remove_dir(ui):
    page, state, _ = ui
    pane = open_tab(page, "kvmstorage")
    pane.locator('tr' + sel('data-kvm-pool', EVIL_POOL) + ' [data-kvm="pool-delete"]').click()
    win = page.locator(".floating-panel", has=page.locator(".kvms-delete"))
    expect(win.locator('[data-ks="refusals"]')).to_contain_text("used by vm-a")
    win.locator('[name="confirm"]').fill(EVIL_POOL)
    go = win.locator('[data-ks="go"]')
    expect(go).to_be_disabled()
    assert go.get_attribute("data-tip") == "The preview has refusals: see them above"
    no_injection(page)
    # plus d'utilisateur : nouvel aperçu (case du répertoire), puis suppression
    state["pool_users"] = []
    win.locator('[name="remove_dir"]').check()
    expect(win.locator('[data-ks="free"]')).to_be_visible()
    expect(win.locator(".kvm-op-plan")).to_contain_text("The empty directory is removed")
    expect(go).to_be_enabled()
    go.click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("deleted on node2")
    path, body = state["deletes"][-1]
    assert path == f"hosts/node2/pools/{EVIL_POOL}"
    assert body == {"expect_delete": DIG, "acknowledge_unreadable": [], "remove_dir": True}


def test_volume_delete_unreadable_acknowledgement_then_digest(ui):
    page, state, _ = ui
    pane = open_tab(page, "kvmstorage")
    row = pane.locator('tr[data-kvm-pool="images"]')
    row.locator("summary").click()
    row.locator('tr' + sel('data-kvm-vol', EVIL_VOL) + ' [data-kvm="vol-delete"]').click()
    win = page.locator(".floating-panel", has=page.locator(".kvms-delete"))
    expect(win.locator('[data-ks="refusals"]')).to_contain_text("could not prove that no VM uses it")
    expect(win.locator('[data-ks="go"]')).to_be_disabled()
    win.locator(f'[data-ks-ack="{UNREAD}"]').check()
    expect(win.locator('[data-ks="free"]')).to_be_visible()
    assert state["plans"][-1] == ("volume", EVIL_VOL, [UNREAD])
    win.locator('[data-ks="go"]').click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("deleted on node2")
    path, body = state["deletes"][-1]
    assert path == f"hosts/node2/volumes/images/{EVIL_VOL}"
    assert body == {"expect_delete": DIG, "acknowledge_unreadable": [UNREAD]}
    no_injection(page)


# --- Images ------------------------------------------------------------------------------

def test_images_catalogue_columns_and_origin_without_query(ui):
    page, state, _ = ui
    pane, block = open_images(page)
    cir = block.locator('tr[data-kvm-image="images/cirros.qcow2"]')
    expect(cir).to_contain_text("qcow2")
    expect(cir).to_contain_text("base of 2 disk(s)")
    expect(cir.locator("code")).to_have_text("aaaaaaaaaaaa…")
    assert "recorded on 2026-10-24 by ju" in cir.locator("code").get_attribute("data-tip")
    expect(cir).to_contain_text("https://download.example/cirros.img")
    iso = block.locator('tr[data-kvm-image="images/netinst.iso"]')
    expect(iso.locator(".badge", has_text="ISO")).to_be_visible()
    expect(iso).to_contain_text("used by 1")
    # lecture manquée (kvm_ops.UNKNOWN) : « usage non prouvé », jamais un disque de plus
    expect(iso).to_contain_text("use not proven")
    expect(iso).not_to_contain_text("base of")
    evil = block.locator("tr[data-kvm-image]").nth(2)
    expect(evil).to_contain_text("https://mirror.example/x.qcow2")
    html = block.inner_html()
    assert "S3CRETTOKEN" not in html and "user:pw" not in html and "frag" not in html
    expect(block.locator(".kvm-img-notes")).to_contain_text("Image pools: images")
    tips_everywhere(pane.locator(".kvm-card"))
    no_injection(page)


def test_create_vm_from_an_image_prefills_the_first_disk(ui):
    page, state, _ = ui
    pane, block = open_images(page)
    block.locator('tr[data-kvm-image="images/cirros.qcow2"] [data-kvm="img-create-vm"]').click()
    panel = page.locator("#fp-kvm-create")
    expect(panel).to_be_visible()
    expect(panel.locator('select[name="host"]')).to_have_value("node2")
    panel.locator('.vm-edit-nav [data-section="disks"]').click()
    disks = panel.locator('.vm-create-section[data-vmf-section="disks"]')
    expect(disks).to_be_visible()
    expect(disks.locator('[name$=".source"]').first).to_have_value("image")
    expect(disks.locator('[name$=".image"]').first).to_have_value("images/cirros.qcow2")


def test_image_delete_refused_while_it_is_a_backing_image(ui):
    page, state, _ = ui
    pane, block = open_images(page)
    btn = block.locator('tr[data-kvm-image="images/cirros.qcow2"] [data-kvm="img-delete"]')
    assert "2 disk(s) use it as their backing image" in btn.get_attribute("data-tip")
    btn.click()
    win = page.locator(".floating-panel", has=page.locator('.kvms-delete[data-ks-kind="image"]'))
    expect(win.locator('[data-ks="refusals"]')).to_contain_text("backing file of /var/lib/libvirt/images/vm1-vda.qcow2")
    expect(win.locator('[data-ks="go"]')).to_be_disabled()


def test_verify_an_image(ui):
    page, state, _ = ui
    pane, block = open_images(page)
    block.locator('tr[data-kvm-image="images/cirros.qcow2"] [data-kvm="img-verify"]').click()
    expect(pane.locator('[data-kvm="feedback"]')).to_contain_text("the SHA-256 computed on the host matches")
    assert ("POST", "hosts/node2/images/images/cirros.qcow2/verify", {}) in state["sent"]


def upload_window(page, block, fname="My Image (1).qcow2"):
    block.locator('[data-kvm="img-upload"]').click()
    win = page.locator(".floating-panel", has=page.locator(".kvms-upload"))
    f = win.locator("form")
    f.locator('[name="file"]').set_input_files({"name": fname, "mimeType": "application/octet-stream",
                                                "buffer": b"QFI\xfb" + b"\0" * 1020})
    return win, f


def test_upload_shows_receive_progress_and_cancel_keeps_nothing(ui):
    page, state, _ = ui
    pane, block = open_images(page)
    win, f = upload_window(page, block)
    tips_everywhere(win)
    expect(f.locator('[name="name"]')).to_have_value("My-Image-1-.qcow2")
    f.locator('[name="sha256"]').fill("B" * 64)
    f.locator('[data-ks-host="node9"]').check()
    f.locator('[name="limit_mbps"]').fill("200")
    f.locator('[data-ks="go"]').click()
    url = page.evaluate("window.__xhrs[0].url")
    q = parse_qs(urlsplit(url).query)
    assert urlsplit(url).path == "/api/kvm/hosts/node2/images/upload"
    token = q.pop("token")[0]
    assert re.fullmatch(r"[0-9a-f]{32}", token)
    assert q == {"pool": ["images"], "name": ["My-Image-1-.qcow2"], "sha256": ["b" * 64], "hosts": ["node9"],
                 "limit_mbps": ["200"]}
    assert page.evaluate("window.__xhrs[0].headers['Content-Type']") == "application/octet-stream"
    page.evaluate("window.__xhrs[0]._progress(512, 1024)")
    expect(win.locator('[data-ks="first-bar"]')).to_have_attribute("style", "width: 50%;")
    expect(win.locator('[data-ks="first-line"]')).to_contain_text("Received by the console")
    expect(win.locator('[data-ks="cancel"]')).to_be_visible()
    win.locator('[data-ks="cancel"]').click()
    page.wait_for_function("window.__xhrs[0].aborted")
    # la console est prévenue par le jeton AVANT l'interruption de la requête
    assert page.evaluate("window.__events") == [f"cancel:/api/kvm/uploads/{token}/cancel", "abort"]
    assert state["upload_cancels"] == [token]
    expect(win.locator('[data-ks="msg"]')).to_contain_text("nothing was kept, nothing was sent")
    expect(win.locator('[data-ks="go"]')).to_be_enabled()
    expect(win.locator('[data-ks="cancel"]')).to_be_hidden()


def test_upload_then_send_progress_and_cancel_while_sending(ui):
    page, state, _ = ui
    pane, block = open_images(page)
    win, f = upload_window(page, block, "cirros-0.6.3.img")
    expect(f.locator('[name="name"]')).to_have_value("cirros-0.6.3.img")
    f.locator('[data-ks="go"]').click()
    # envoi sans fin : l'annulation part pendant l'envoi
    state["streams"]["kvm000000777"] = sse_events([("progress", {"phase": "upload", "done": 30, "total": 100, "rate": 10,
                                                                  "eta": 7, "elapsed": 3})])
    page.evaluate("window.__xhrs[0]._progress(1024, 1024)")
    page.evaluate("window.__xhrs[0]._done(202, {action_id: 'kvm000000777'})")
    expect(win.locator('[data-ks="send-bar"]')).to_have_attribute("style", "width: 30%;")
    expect(win.locator('[data-ks="send-line"]')).to_contain_text("Upload")
    win.locator('[data-ks="cancel"]').click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("waiting for the console")
    token = parse_qs(urlsplit(page.evaluate("window.__xhrs[0].url")).query)["token"][0]
    assert state["upload_cancels"] == [token] and not state.get("cancels")
    assert page.evaluate("window.__xhrs[0].aborted") is False


def test_cancel_while_sending_falls_back_to_the_action_when_the_token_is_gone(ui):
    page, state, _ = ui
    state["upload_cancel"] = (404, {"error": "no upload is receiving or sending with this token", "reason": "not-active"})
    pane, block = open_images(page)
    win, f = upload_window(page, block, "cirros-0.6.3.img")
    f.locator('[data-ks="go"]').click()
    state["streams"]["kvm000000779"] = sse_events([("progress", {"phase": "upload", "done": 30, "total": 100, "rate": 10,
                                                                  "eta": 7, "elapsed": 3})])
    page.evaluate("window.__xhrs[0]._sent()")
    page.evaluate("window.__xhrs[0]._done(202, {action_id: 'kvm000000779'})")
    expect(win.locator('[data-ks="send-bar"]')).to_have_attribute("style", "width: 30%;")
    win.locator('[data-ks="cancel"]').click()
    for _ in range(50):
        if state.get("cancels"):
            break
        page.wait_for_timeout(100)
    assert [(m, u.rsplit("/api/", 1)[1]) for m, u in state["cancels"]] == [("DELETE", "action/kvm000000779")]


def test_upload_success_names_the_hosts(ui):
    page, state, _ = ui
    pane, block = open_images(page)
    win, f = upload_window(page, block, "ok.qcow2")
    f.locator('[data-ks="go"]').click()
    state["streams"]["kvm000000778"] = sse_events(
        [("step", {"step_id": "upload", "status": "done", "message": "sent"}),
         ("progress", {"phase": "upload", "done": 100, "total": 100, "rate": 10, "eta": 0, "elapsed": 3, "final": True})],
        {"status": "done", "result": {"output": {"ok": True, "pool": "images", "volume": "ok.qcow2", "sha256": "e" * 64,
                                                   "hosts": [{"host": "node2", "ok": True}]}}})
    page.evaluate("window.__xhrs[0]._done(202, {action_id: 'kvm000000778'})")
    expect(win.locator('[data-ks="msg"]')).to_contain_text("images/ok.qcow2 is on node2")
    expect(win.locator('[data-ks="send-bar"]')).to_have_attribute("style", "width: 100%;")


@pytest.mark.parametrize("body", [
    {"error": "the received file does not match the expected SHA-256: nothing was sent", "kind": "refused",
     "refusals": ["the received file does not match the expected SHA-256: nothing was sent"]},
    {"error": "the qcow2 image names a backing file: refused", "kind": "refused",
     "refusals": ["the qcow2 image names a backing file: refused", "the qcow2 image names an external data file: refused"]},
])
def test_upload_refusals_before_any_send_are_shown(ui, body):
    page, state, _ = ui
    pane, block = open_images(page)
    win, f = upload_window(page, block, "bad.qcow2")
    f.locator('[data-ks="go"]').click()
    page.evaluate(f"window.__xhrs[0]._done(422, {json.dumps(body)})")
    msg = win.locator('[data-ks="msg"]')
    for r in body["refusals"]:
        expect(msg).to_contain_text(r)
    expect(win.locator('[data-ks="go"]')).to_be_enabled()


def test_url_download_refusals_and_url_without_query(ui):
    page, state, _ = ui
    pane, block = open_images(page)
    block.locator('[data-kvm="img-fetch"]').click()
    win = page.locator(".floating-panel", has=page.locator(".kvms-fetch"))
    f = win.locator("form")
    tips_everywhere(win)
    # http sans somme : refusé dans la page, rien d'envoyé
    f.locator('[name="url"]').fill("http://mirror.example/a.qcow2")
    expect(f.locator('[name="name"]')).to_have_value("a.qcow2")
    f.locator('[data-ks="go"]').click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("A plain http URL needs the expected SHA-256")
    assert not [p for (m, p, b) in state["sent"] if p.endswith("/fetch")]
    # adresse de métadonnées : refus du serveur montré tel quel
    f.locator('[name="url"]').fill("https://169.254.169.254/latest")
    f.locator('[name="name"]').fill("meta.img")
    f.locator('[data-ks="go"]').click()
    expect(win.locator('[data-ks="refusals"]')).to_contain_text("169.254.169.254 is a metadata address: refused")
    # URL signée : la requête n'est jamais montrée
    f.locator('[name="url"]').fill("https://mirror.example/x.qcow2?token=S3CRETTOKEN")
    f.locator('[name="name"]').fill("x.qcow2")
    f.locator('[data-ks="go"]').click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("images/x.qcow2 is on node2")
    expect(win.locator('[data-ks="send-bar"]')).to_have_attribute("style", "width: 100%;")
    assert "S3CRETTOKEN" not in win.locator('[data-ks="msg"]').inner_html()
    sent = [b for (m, p, b) in state["sent"] if p.endswith("/fetch")][-1]
    assert sent["url"].endswith("?token=S3CRETTOKEN") and sent["pool"] == "images" and sent["hosts"] == []


def test_image_pools_setting(ui):
    page, state, _ = ui
    pane, block = open_images(page)
    block.locator('[data-kvm="img-pools"]').click()
    win = page.locator(".floating-panel", has=page.locator(".kvms-image-pools"))
    expect(win.locator('[data-ks-pool="images"]')).to_be_checked()
    win.locator(sel('data-ks-pool', EVIL_POOL)).check()
    win.locator('[data-ks="go"]').click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("Image pools saved")
    assert ("PUT", "hosts/node2/image-pools", {"pools": ["images", EVIL_POOL]}) in state["sent"]
    no_injection(page)


# --- Réseaux -----------------------------------------------------------------------------

def test_network_create_overlap_refusal_and_free_subnet_proposed(ui):
    page, state, _ = ui
    pane = open_tab(page, "kvmnetworks")
    pane.locator('[data-kvm-block="node2"] [data-kvm="net-create"]').click()
    win = page.locator(".floating-panel", has=page.locator(".kvms-net-create"))
    f = win.locator("form")
    # 192.168.100.0/24 est une route de l'hôte : la proposition passe au suivant
    expect(f.locator('[name="subnet"]')).to_have_value("192.168.101.0/24")
    expect(f.locator('[name="dhcp_start"]')).to_have_value("192.168.101.2")
    expect(f.locator('[name="dhcp_end"]')).to_have_value("192.168.101.254")
    tips_everywhere(win)
    f.locator('[name="name"]').fill("kvmtest-net")
    f.locator('[name="subnet"]').fill("172.16.0.0/16")
    f.locator('[data-ks="go"]').click()
    expect(win.locator('[data-ks="refusals"]')).to_contain_text("the subnet overlaps 172.16.0.0/16 (address of br0)")
    assert not [b for (m, p, b) in state["sent"] if p == "hosts/node2/networks" and not b.get("dry_run")]
    f.locator('[name="subnet"]').fill("10.213.77.0/24")
    f.locator('[name="mode"]').select_option("isolated")
    f.locator('[data-ks="go"]').click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("Network kvmtest-net created on node2")
    spec = [b for (m, p, b) in state["sent"] if p == "hosts/node2/networks"][-1]["spec"]
    assert spec == {"name": "kvmtest-net", "mode": "isolated", "autostart": True, "subnet": "10.213.77.0/24",
                    "dhcp": True, "dhcp_start": "10.213.77.2", "dhcp_end": "10.213.77.254"}
    # mode pont : un pont de l'hôte, sans sous-réseau
    f.locator('[name="mode"]').select_option("bridge")
    expect(f.locator('[name="host_bridge"]')).to_have_value("br0")
    expect(f.locator('[name="subnet"]')).to_be_hidden()


def test_network_stop_is_confirmed_and_refusal_reasons_shown(ui):
    page, state, _ = ui
    pane = open_tab(page, "kvmnetworks")
    row = pane.locator('[data-kvm-block="node2"] tr[data-kvm-net="default"]')
    row.locator('[data-kvm="net-verb"][data-verb="stop"]').click()
    fb = pane.locator('[data-kvm="feedback"]')
    expect(fb).to_contain_text("Stop the network default on node2?")
    assert not [p for (m, p, b) in state["sent"] if p.endswith("/stop")]
    fb.locator('[data-kvm="obj-yes"]').click()
    expect(fb).to_contain_text("a running VM has a card on it (web)")
    expect(fb).to_contain_text("used by web")
    # autostart : part tout de suite, avec la valeur voulue
    pane.locator('tr' + sel('data-kvm-net', EVIL_NET) + ' [data-kvm="net-verb"][data-verb="autostart"]').click()
    expect(fb).to_contain_text("done")
    assert ("POST", f"hosts/node2/networks/{EVIL_NET}/autostart", {"enable": True}) in state["sent"]
    no_injection(page)


def test_network_delete_preview_shows_users_and_bridges_listed(ui):
    page, state, _ = ui
    pane = open_tab(page, "kvmnetworks")
    block = pane.locator('[data-kvm-block="node2"]')
    block.locator('[data-kvm="net-links"]').click()
    links = block.locator('[data-kvm-links="node2"]')
    expect(links).to_contain_text("br0")
    expect(links).to_contain_text("eno1")
    expect(links).to_contain_text("172.16.0.0/16")
    block.locator('tr' + sel('data-kvm-net', EVIL_NET) + ' [data-kvm="net-delete"]').click()
    win = page.locator(".floating-panel", has=page.locator('.kvms-delete[data-ks-kind="network"]'))
    expect(win.locator('[data-ks="refusals"]')).to_contain_text("used by web, db")
    win.locator('[name="confirm"]').fill(EVIL_NET)
    expect(win.locator('[data-ks="go"]')).to_be_disabled()
    no_injection(page)


def test_pool_stop_then_start_and_volume_resize(ui):
    page, state, _ = ui
    pane = open_tab(page, "kvmstorage")
    block = pane.locator('[data-kvm-block="node2"]')
    block.locator('tr[data-kvm-pool="off"] [data-kvm="pool-verb"][data-verb="start"]').click()
    expect(pane.locator('[data-kvm="feedback"]')).to_contain_text("Start: off done")
    assert ("POST", "hosts/node2/pools/off/start", {}) in state["sent"]
    row = block.locator('tr[data-kvm-pool="images"]')
    row.locator("summary").click()
    row.locator('tr[data-kvm-vol="cirros.qcow2"] [data-kvm="vol-resize"]').click()
    win = page.locator(".floating-panel", has=page.locator(".kvms-vol-resize"))
    win.locator('[name="size"]').fill("10M")
    win.locator('[data-ks="go"]').click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("must be larger than 112.0 MiB")
    win.locator('[name="size"]').fill("2G")
    win.locator('[data-ks="go"]').click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("resized to 2.0 GiB")
    assert ("POST", "hosts/node2/volumes/images/cirros.qcow2/resize",
            {"capacity": 2 * 2 ** 30, "acknowledge_unreadable": []}) in state["sent"]


def test_a_busy_host_preview_is_retried_by_itself(ui):
    """Vu en réel : l'hôte relisait déjà ses disques (429) ; l'aperçu se
    relance tout seul au lieu de laisser la fenêtre bloquée."""
    page, state, _ = ui
    state["plan_busy"] = 1
    state["pool_users"] = []
    pane = open_tab(page, "kvmstorage")
    pane.locator('tr[data-kvm-pool="off"] [data-kvm="pool-delete"]').click()
    win = page.locator(".floating-panel", has=page.locator('.kvms-delete[data-ks-kind="pool"]'))
    expect(win.locator('[data-ks="plan"]')).to_contain_text("trying again by itself")
    expect(win.locator('[data-ks="free"]')).to_be_visible(timeout=10000)
    assert state["plan_busy"] == 0


# --- revue de la tâche 6 : course après le dernier octet ----------------------------------------

def _token(page):
    return parse_qs(urlsplit(page.evaluate("window.__xhrs[0].url")).query)["token"][0]


@pytest.mark.parametrize("end,expected,not_expected", [
    # la console a vu l'annulation avant l'envoi : elle dit ce qu'elle a fait
    ({"status": "cancelled", "error_summary": "cancelled before sending: nothing sent",
      "result": {"output": {"ok": False, "kind": "cancelled"}}},
     "Cancelled: cancelled before sending: nothing sent", "nothing was kept"),
    # l'envoi a fini avant que l'annulation n'arrive : c'est fini, pas « annulé »
    ({"status": "done", "result": {"output": {"ok": True, "pool": "images", "volume": "late.qcow2", "sha256": "e" * 64,
                                               "hosts": [{"host": "node2", "ok": True}]}}},
     "images/late.qcow2 is on node2", "Cancelled"),
])
def test_cancel_after_the_last_byte_shows_the_console_outcome(ui, end, expected, not_expected):
    page, state, _ = ui
    pane, block = open_images(page)
    win, f = upload_window(page, block, "late.qcow2")
    f.locator('[data-ks="go"]').click()
    page.evaluate("window.__xhrs[0]._progress(1024, 1024)")
    page.evaluate("window.__xhrs[0]._sent()")
    win.locator('[data-ks="cancel"]').click()
    msg = win.locator('[data-ks="msg"]')
    expect(msg).to_contain_text("waiting for the console")
    assert state["upload_cancels"] == [_token(page)]
    # jamais d'interruption locale ni de « rien gardé » affirmé par la page
    assert page.evaluate("window.__xhrs[0].aborted") is False
    expect(msg).not_to_contain_text("nothing was kept")
    state["streams"]["kvm000000780"] = sse_events([], end)
    page.evaluate("window.__xhrs[0]._done(202, {action_id: 'kvm000000780'})")
    expect(msg).to_contain_text(expected)
    expect(msg).not_to_contain_text(not_expected)
    assert not state.get("cancels")


def test_cancel_after_the_last_byte_with_the_token_gone_cancels_the_action_after_the_202(ui):
    page, state, _ = ui
    state["upload_cancel"] = (404, {"error": "no upload is receiving or sending with this token", "reason": "not-active"})
    pane, block = open_images(page)
    win, f = upload_window(page, block, "late.qcow2")
    f.locator('[data-ks="go"]').click()
    page.evaluate("window.__xhrs[0]._sent()")
    win.locator('[data-ks="cancel"]').click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("waiting for the console")
    assert not state.get("cancels")                            # pas encore d'identifiant
    state["streams"]["kvm000000781"] = sse_events([("progress", {"phase": "upload", "done": 5, "total": 100})])
    page.evaluate("window.__xhrs[0]._done(202, {action_id: 'kvm000000781'})")
    for _ in range(50):
        if state.get("cancels"):
            break
        page.wait_for_timeout(100)
    assert [(m, u.rsplit("/api/", 1)[1]) for m, u in state["cancels"]] == [("DELETE", "action/kvm000000781")]
    assert page.evaluate("window.__xhrs[0].aborted") is False


def test_last_byte_leaving_while_the_cancel_is_in_flight_never_aborts(ui):
    """Annulation cliquée pendant la réception ; le dernier octet part avant la
    réponse de la console : la page n'interrompt plus rien et attend."""
    page, state, _ = ui
    pane, block = open_images(page)
    win, f = upload_window(page, block, "race.qcow2")
    f.locator('[data-ks="go"]').click()
    page.evaluate("window.__xhrs[0]._progress(900, 1024)")
    page.evaluate("window.__holdCancel = true")
    win.locator('[data-ks="cancel"]').click()
    page.wait_for_function("window.__events.some((e) => e.startsWith('cancel:'))")
    page.evaluate("window.__xhrs[0]._sent()")
    page.evaluate("window.__releaseCancel()")
    msg = win.locator('[data-ks="msg"]')
    expect(msg).to_contain_text("waiting for the console")
    assert page.evaluate("window.__events")[-1] == "last-byte"
    assert page.evaluate("window.__xhrs[0].aborted") is False
    state["streams"]["kvm000000782"] = sse_events([], {"status": "cancelled", "error_summary": "cancelled before sending: nothing sent",
                                                       "result": {"output": {"ok": False, "kind": "cancelled"}}})
    page.evaluate("window.__xhrs[0]._done(202, {action_id: 'kvm000000782'})")
    expect(msg).to_contain_text("Cancelled: cancelled before sending: nothing sent")


def test_a_cancelled_receive_reported_by_the_console_is_shown(ui):
    page, state, _ = ui
    pane, block = open_images(page)
    win, f = upload_window(page, block, "x.qcow2")
    f.locator('[data-ks="go"]').click()
    page.evaluate("window.__xhrs[0]._sent()")
    win.locator('[data-ks="cancel"]').click()
    expect(win.locator('[data-ks="msg"]')).to_contain_text("waiting for the console")
    page.evaluate("window.__xhrs[0]._done(409, {error: 'cancelled', kind: 'cancelled', message: 'cancelled, nothing kept'})")
    expect(win.locator('[data-ks="msg"]')).to_contain_text("nothing was kept, nothing was sent")
    expect(win.locator('[data-ks="go"]')).to_be_enabled()


# --- revue : nouvel essai d'un aperçu refusé (429) ----------------------------------------------

def test_busy_retry_delay_doubles_with_jitter_and_a_cap(ui):
    page, _, _ = ui
    got = page.evaluate("""() => { const d = KVMStorage._internals.busyDelay;
      return [d(0, () => 0), d(0, () => 1), d(1, () => 1), d(3, () => 0.5), d(10, () => 1), d(10, () => 0)]; }""")
    assert got == [500, 1000, 2000, 6000, 20000, 10000]


def test_a_new_preview_restarts_the_busy_retries(ui):
    """Un aperçu relancé par l'opérateur (case changée) pendant une série de
    refus repart du premier délai, pas du délai déjà doublé."""
    page, state, _ = ui
    page.evaluate("Math.random = () => 0.999999")
    state["plan_busy"] = 3
    state["pool_users"] = []
    pane = open_tab(page, "kvmstorage")
    pane.locator('tr[data-kvm-pool="off"] [data-kvm="pool-delete"]').click()
    win = page.locator(".floating-panel", has=page.locator('.kvms-delete[data-ks-kind="pool"]'))
    expect(win.locator('[data-ks="plan"]')).to_contain_text("trying again by itself")
    for _ in range(60):                       # deux refus : le délai suivant serait 4 s
        if len(state.get("plan_times", [])) >= 2:
            break
        page.wait_for_timeout(100)
    assert len(state["plan_times"]) == 2
    page.wait_for_timeout(300)
    t_toggle = time.monotonic()
    win.locator('[name="remove_dir"]').check()
    expect(win.locator('[data-ks="free"]')).to_be_visible(timeout=10000)
    # aperçu neuf (refusé une fois de plus) puis relance après ~1 s, jamais 4 s
    assert state["plan_busy"] == 0
    assert state["plan_times"][-1] - t_toggle < 2.5, state["plan_times"][-1] - t_toggle
    assert len(state["plan_times"]) == 4


# --- revue : « usage non prouvé », illisible (acceptable) ou relevé en échec --------------------

@pytest.mark.parametrize("unknown,cls,tip", [
    ([], "info", "you can acknowledge them one by one"),
    (["domain web", "pool images"], "warn", "2 reads: timeout or I/O error"),
])
def test_unproven_tooltip_tells_acknowledgeable_from_failed(ui, unknown, cls, tip):
    page, state, _ = ui
    state["images_unknown"] = unknown
    pane, block = open_images(page)
    b = block.locator('tr[data-kvm-image="images/netinst.iso"] .badge', has_text="use not proven")
    expect(b).to_have_class(re.compile(rf"\b{cls}\b"))
    assert tip in b.get_attribute("data-tip")
    if unknown:
        assert "cannot be acknowledged" in b.get_attribute("data-tip")
