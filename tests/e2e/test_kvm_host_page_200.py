"""2.0.0, tâche 7 : la page d'un hôte KVM (host-page.js, kvm.js).

API KVM simulée (mêmes jeux que test_kvm_196 : node2 joignable, ghost
injoignable). Chaque adresse d'hôte KVM chargée à froid montre sa vue avec
l'hôte imposé (pas de menu d'hôte), le bandeau KVM et son onglet actif ; un
hôte KVM et un cluster du même nom ne se mélangent pas ; Retirer demande
l'administrateur (bouton grisé avec la raison, jamais caché) ; un hôte
injoignable garde une tuile en erreur classée et des Paramètres lisibles ;
les étiquettes se modifient en action suivie après des contrôles."""

import json
import re
from urllib.parse import unquote

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402
from test_kvm_196 import DETAIL, GESTURES, GHOST, NODE2, VMS, sse  # noqa: E402

FP = "SHA256:81Nop5uq4NBaqU/hzSyopOIxSoyytc6DgyLZd5/xXJY"
# un hôte KVM qui porte le nom du cluster du serveur de test
TWIN = dict(NODE2, name="harv-fake", address="172.16.1.99", labels={"twin": "yes"},
            vms=[dict(VMS[0], host="harv-fake", name="twin-vm")])
PUBLIC = ("name", "address", "port", "user", "uri", "labels")


def hosts_api(extra=()):
    hosts = [{"kind": "harvester", "name": "harv-fake", "state": "ok", "error": None, "vm_count": 3,
              "labels": {}, "version": "v1.9.0", "favorite": False, "folder": None, "origin": "config"},
             {"kind": "kvm", "name": "node2", "state": "ok", "error": None, "vm_count": 5,
              "labels": {"site": "cave"}, "version": "12.7.0", "favorite": False, "folder": None, "origin": "console"},
             {"kind": "kvm", "name": "ghost", "state": "unreachable", "error": "No route to host", "vm_count": None,
              "labels": {}, "version": None, "favorite": False, "folder": None, "origin": "console"},
             {"kind": "kvm", "name": "harv-fake", "state": "ok", "error": None, "vm_count": 1,
              "labels": {"twin": "yes"}, "version": "12.7.0", "favorite": False, "folder": None, "origin": "console"}] + list(extra)
    return {"families": {}, "hosts": hosts}


@pytest.fixture
def kvm_page(context, flask_server):
    state = {"role": "admin", "sent": [], "gets": [], "streams": {}, "labels": dict(NODE2["labels"])}

    def ok(route, body, status=200):
        return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def decl_of(h):
        d = {k: h[k] for k in PUBLIC}
        if h["name"] == "node2":
            d["labels"] = dict(state["labels"])
        return d

    def api(route, req):
        path = unquote(req.url.split("/api/kvm/", 1)[1].split("?", 1)[0])
        if req.method == "GET":
            state["gets"].append(path)
            hosts = {h["name"]: h for h in (NODE2, GHOST, TWIN)}
            if path == "hosts" and "declared=1" in req.url:
                return ok(route, {"hosts": [decl_of(h) for h in hosts.values()], "warnings": [],
                                  "role": state["role"], "gestures": GESTURES})
            if path == "hosts":
                return ok(route, {"hosts": [NODE2, GHOST, TWIN], "warnings": [], "role": state["role"],
                                  "gestures": GESTURES})
            m = re.fullmatch(r"hosts/([^/]+)/declaration", path)
            if m and m.group(1) in hosts:
                return ok(route, dict(decl_of(hosts[m.group(1)]), image_pools=None, role=state["role"],
                                      fingerprints=[{"type": "ssh-ed25519", "fingerprint": FP}]))
            if path == "hosts/ghost":
                return ok(route, dict(GHOST, role=state["role"]), 502)
            if path == "hosts/node2":
                return ok(route, dict(DETAIL, labels=dict(state["labels"]), role=state["role"]))
            if path == "hosts/harv-fake":
                return ok(route, dict(TWIN, pools=[], networks=[], role=state["role"]))
            if path == "vms":
                return ok(route, {"vms": VMS + TWIN["vms"], "role": state["role"], "gestures": GESTURES,
                                  "warnings": [], "operations": {}, "edit_operations": {},
                                  "hosts": [{"name": "node2", "state": "ok", "count": 5},
                                            {"name": "harv-fake", "state": "ok", "count": 1},
                                            {"name": "ghost", "state": "unreachable", "error": GHOST["error"]["message"]}]})
            if path.endswith("/pools") or path.endswith("/links"):
                return ok(route, {"pools": [], "links": []})
            return ok(route, {"error": "not found"}, 404)
        body = req.post_data or ""
        state["sent"].append((req.method, path, body))
        aid = f"kvm{len(state['sent']):09d}"
        if path.endswith("/labels"):
            state["labels"] = json.loads(body)["labels"]
            state["streams"][aid] = sse([{"step_id": "labels", "status": "done", "message": "site=lab"}],
                                        output={"ok": True})
        elif path.endswith("/test"):
            state["streams"][aid] = sse([], output={"ok": True, "checks": [
                {"check": "ssh", "status": "ok", "message": "connected as ju@172.16.1.12"}]})
        else:
            state["streams"][aid] = sse([], output={"ok": True})
        return ok(route, {"action_id": aid}, 202)

    page = context.new_page()
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_sidebar_pinned','1');")
    page.route(re.compile(r".*/api/kvm/.*"), api)
    page.route(re.compile(r".*/api/hosts$"), lambda r, q: ok(r, hosts_api()))
    page.route(re.compile(r".*/api/stream/kvm\d+$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body=state["streams"].get(q.url.rsplit("/", 1)[1], sse([]))))
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    state.update(calls=calls, errors=errors, dialogs=dialogs)

    def go(hash):
        goto(page, flask_server["base_url"], hash)
        return page
    return go, page, state


# (adresse, sous-menu actif, onglet de Paramètres, section, sélecteur visible)
ROWS = [
    ("#kvm/node2", "overview", None, "kvmhosts", '#tab-kvmhosts [data-kvm-host="node2"]'),
    ("#kvm/node2/vms", "vms", None, "kvmvms", '#tab-kvmvms tr[data-kvm-vm="node2/web"]'),
    ("#kvm/node2/params/system", "params", "system", "kvmparams", "#kvmparams-host .hp-kvm-system"),
    ("#kvm/node2/params/pools", "params", "pools", "kvmstorage", '#tab-kvmstorage [data-kvm-block="node2"]'),
    ("#kvm/node2/params/networks", "params", "networks", "kvmnetworks", '#tab-kvmnetworks [data-kvm-block="node2"]'),
    ("#kvm/node2/params/labels", "params", "labels", "kvmparams", "#kvmparams-host .hp-kvm-labels-table"),
    ("#kvm/node2/params/conn", "params", "conn", "kvmparams", "#kvmparams-host .hp-kvm-conn"),
    ("#kvm/node2/dr", "dr", None, "kvmdr", "#tab-kvmdr .kvm-dr-empty"),
]


@pytest.mark.parametrize("hash,sub,tab,section,selector", ROWS, ids=[r[0] for r in ROWS])
def test_each_kvm_host_address_cold(kvm_page, hash, sub, tab, section, selector):
    go, page, state = kvm_page
    go(hash)
    expect(page.locator(f"#tab-{section}")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(selector).first).to_be_visible()
    head = page.locator("#host-page-head")
    expect(head.locator(".hp-family")).to_have_text("KVM")
    expect(head.locator(".hp-name")).to_have_text("node2")
    expect(head.locator(f'.hp-sub-link[data-hp-sub="{sub}"]')).to_have_class(re.compile(r"\bactive\b"))
    assert [a.get_attribute("data-hp-sub") for a in head.locator(".hp-sub-link").all()] == ["overview", "vms", "params", "dr"]
    if tab:
        expect(head.locator(f'.hp-tabs [data-hp-tab="{tab}"]')).to_have_class(re.compile(r"\bactive\b"))
        assert [a.get_attribute("data-hp-tab") for a in head.locator(".hp-tabs .sub-tab").all()] == \
            ["system", "pools", "networks", "labels", "conn"]
    else:
        assert head.locator(".hp-tabs").count() == 0
    # l'hôte est imposé : aucun menu d'hôte dans la vue, aucun autre hôte montré
    assert page.locator(f'#tab-{section} [data-kvm-filter="host"]').count() == 0
    assert page.locator(f'#tab-{section} [data-kvm-host="ghost"], #tab-{section} [data-kvm-block="ghost"]').count() == 0
    assert page.evaluate("location.hash") == hash
    # Disaster Recovery : aucun appel à la réplication pour un hôte KVM
    assert not [c for c in state["calls"] if "/api/replication/hosts" in c]
    assert state["errors"] == []


def test_vms_of_the_host_only_and_no_other_host(kvm_page):
    go, page, _ = kvm_page
    go("#kvm/node2/vms")
    rows = page.locator("#tab-kvmvms tr[data-kvm-vm]")
    expect(rows).to_have_count(len(VMS))
    assert all(k.startswith("node2/") for k in rows.evaluate_all("els => els.map(e => e.dataset.kvmVm)"))
    # un autre hôte du même module (cluster du même nom) n'y figure pas
    assert page.locator('#tab-kvmvms tr[data-kvm-vm="harv-fake/twin-vm"]').count() == 0


def test_a_kvm_host_and_a_cluster_of_the_same_name_do_not_mix(kvm_page):
    go, page, state = kvm_page
    go("#kvm/harv-fake/params/system")
    head = page.locator("#host-page-head")
    expect(head.locator(".hp-family")).to_have_text("KVM")
    expect(page.locator("#kvmparams-host .hp-kvm-system")).to_contain_text("harv-fake")
    assert "hosts/harv-fake" in state["gets"]
    # le cluster homonyme, lui, ouvre la page Harvester
    page.evaluate("Router.go('#harvester/harv-fake')")
    page.evaluate("Router.idle()")
    expect(head.locator(".hp-family")).to_have_text("Harvester")
    expect(page.locator("#tab-overview")).to_have_class(re.compile(r"\bactive\b"))
    # le menu des hôtes garde deux entrées distinctes, une par famille
    page.evaluate("Router.go('#kvm/harv-fake')")
    page.evaluate("Router.idle()")
    expect(page.locator('#host-nav .hnav-host.open[data-key="kvm/harv-fake"]')).to_have_count(1)
    expect(page.locator('#host-nav .hnav-host[data-key="harvester/harv-fake"]')).to_have_count(1)
    expect(page.locator('#tab-kvmhosts [data-kvm-host="harv-fake"]')).to_be_visible()


@pytest.mark.parametrize("role,test_on,remove_on,edit_on", [
    ("admin", True, True, True),
    ("operator", True, False, False),
    ("viewer", False, False, False),
])
def test_buttons_follow_the_role_greyed_with_the_reason(kvm_page, role, test_on, remove_on, edit_on):
    go, page, state = kvm_page
    state["role"] = role
    go("#kvm/node2/params/conn")
    test = page.locator('#kvmparams-host [data-hp-kvm="test"]')
    remove = page.locator('#kvmparams-host [data-hp-kvm="remove"]')
    expect(test).to_be_visible()
    expect(remove).to_be_visible()                      # grisé, jamais caché
    assert test.is_enabled() is test_on and remove.is_enabled() is remove_on
    assert remove.get_attribute("title") == ("Remove the host from the console" if remove_on else "Needs the admin role") \
        or (remove_on and remove.get_attribute("title"))
    if not remove_on:
        assert remove.get_attribute("title") == "Needs the admin role"
    if not test_on:
        assert test.get_attribute("title") == "Needs the operator role"
    page.evaluate("Router.go('#kvm/node2/params/labels')")
    edit = page.locator('#kvmparams-host [data-hp-kvm="edit-labels"]')
    expect(edit).to_be_visible()
    assert edit.is_enabled() is edit_on
    if not edit_on:
        assert edit.get_attribute("title") == "Needs the admin role"


def test_conn_shows_the_declaration_and_tests_the_host(kvm_page):
    go, page, state = kvm_page
    go("#kvm/node2/params/conn")
    box = page.locator("#kvmparams-host")
    for text in ("172.16.1.12", "ju", "qemu:///system", FP, "ssh-ed25519"):
        expect(box).to_contain_text(text)
    assert "_known" not in box.inner_text() and "_id" not in box.inner_text()
    box.locator('[data-hp-kvm="test"]').click()
    expect(box.locator('.kvm-checks li[data-check="ssh"]')).to_be_visible()
    assert ("POST", "hosts/node2/test", "{}") in state["sent"]


def test_remove_confirms_in_the_page_then_opens_the_kvm_hosts(kvm_page):
    go, page, state = kvm_page
    go("#kvm/node2/params/conn")
    box = page.locator("#kvmparams-host")
    box.locator('[data-hp-kvm="remove"]').click()
    expect(box.locator(".kvm-confirm")).to_contain_text("Remove host node2 from the console?")
    assert not [s for s in state["sent"] if s[0] == "DELETE"]
    box.locator('[data-hp-kvm="remove-no"]').click()
    expect(box.locator(".kvm-confirm")).to_have_count(0)
    box.locator('[data-hp-kvm="remove"]').click()
    box.locator('[data-hp-kvm="remove-yes"]').click()
    expect(box).to_contain_text("Host node2 removed")
    assert ("DELETE", "hosts/node2", "") in state["sent"]
    page.wait_for_function("() => location.hash === '#hosts/kvm'")
    assert state["dialogs"] == []


def test_unreachable_host_tile_in_error_and_parameters_readable(kvm_page):
    go, page, state = kvm_page
    go("#kvm/ghost")
    tile = page.locator('#tab-kvmhosts [data-kvm-host="ghost"]')
    expect(tile).to_have_class(re.compile("is-error"))
    expect(tile.locator(".badge.fail")).to_have_text("unreachable")
    assert "No route to host" in tile.locator(".badge.fail").get_attribute("data-tip")
    page.evaluate("Router.go('#kvm/ghost/params/system')")
    box = page.locator("#kvmparams-host")
    expect(box.locator(".hp-kvm-error")).to_have_attribute("data-kind", "unreachable")
    expect(box).to_contain_text("only its declaration is shown")
    expect(box).to_contain_text("qemu:///system")
    page.evaluate("Router.go('#kvm/ghost/params/conn')")
    expect(box.locator(".hp-kvm-conn")).to_contain_text("172.16.1.250")
    page.evaluate("Router.go('#kvm/ghost/params/labels')")
    expect(box.locator(".hp-kvm-nolabel")).to_be_visible()
    assert state["errors"] == []


def test_labels_are_edited_after_checks_in_a_tracked_action(kvm_page):
    go, page, state = kvm_page
    go("#kvm/node2/params/labels")
    box = page.locator("#kvmparams-host")
    expect(box.locator(".hp-kvm-labels-table")).to_contain_text("site")
    box.locator('[data-hp-kvm="edit-labels"]').click()
    modal = page.locator(".hp-labels-modal")
    expect(modal).to_be_visible()
    apply = modal.locator('[data-lbl="apply"]')
    # rien n'a changé : Appliquer grisé et dit pourquoi
    expect(apply).to_be_disabled()
    assert apply.get_attribute("title") == "Nothing changes yet."
    modal.locator('[data-lbl="add"]').click()
    modal.locator("tbody tr:last-child [data-lbl-k]").fill("-bad")
    expect(modal.locator(".hp-labels-checks li.bad")).to_contain_text("Invalid name: -bad")
    expect(apply).to_be_disabled()
    assert apply.get_attribute("title") == "Fix the failed checks first"
    modal.locator("tbody tr:last-child [data-lbl-k]").fill("site")
    expect(modal.locator(".hp-labels-checks li.bad")).to_contain_text("Name given twice: site")
    modal.locator("tbody tr:last-child [data-lbl-k]").fill("rack")
    modal.locator("tbody tr:last-child [data-lbl-v]").fill("b2")
    modal.locator("tbody tr:first-child [data-lbl-v]").fill("lab")
    expect(modal.locator(".hp-labels-diff")).to_have_text("Added: rack. Changed: site. Removed: –.")
    assert not [s for s in state["sent"] if s[0] == "PUT"]          # rien avant Appliquer
    apply.click()
    expect(modal).to_have_count(0)
    assert ("PUT", "hosts/node2/labels", json.dumps({"labels": {"site": "lab", "rack": "b2"}}).replace(" ", "")) \
        in [(m, p, b.replace(" ", "")) for m, p, b in state["sent"]]
    expect(box.locator(".hp-kvm-labels-table")).to_contain_text("rack")
    expect(box.locator(".hp-kvm-result")).to_contain_text("Labels written")
    assert state["dialogs"] == [] and state["errors"] == []


def test_labels_window_escape_closes_without_writing(kvm_page):
    go, page, state = kvm_page
    go("#kvm/node2/params/labels")
    page.locator('#kvmparams-host [data-hp-kvm="edit-labels"]').click()
    modal = page.locator(".hp-labels-modal")
    modal.locator("tbody tr:first-child [data-lbl-v]").fill("x")
    page.keyboard.press("Escape")
    expect(modal).to_have_count(0)
    assert not [s for s in state["sent"] if s[0] == "PUT"]


def test_every_control_of_the_kvm_page_has_a_tooltip(kvm_page):
    go, page, _ = kvm_page
    for hash in ("#kvm/node2/params/conn", "#kvm/node2/params/labels"):
        go(hash)
        for el in page.locator("#host-page-head a, #kvmparams-host button").all():
            assert el.get_attribute("title") or el.get_attribute("data-tip"), el.evaluate("e => e.outerHTML")
