"""2.0.0, tâche 8 : l'index des hôtes (`#hosts`, hosts-index.js).

Source unique `/api/hosts` (simulée) : tableau des deux familles, tri par
colonne, filtres, `#hosts/harvester` et `#hosts/kvm` préfiltrés (et KVM >
Hôtes repris : Déclarer, Tester, Retirer par ligne), 100 hôtes, étoile, et
un lien vers un hôte disparu qui arrive ici avec le bandeau."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402


def h(name, kind="harvester", state="ok", version=None, labels=None, vms=None, fav=False, error=None):
    return {"kind": kind, "name": name, "state": state, "error": error, "vm_count": vms, "labels": labels or {},
            "version": version, "favorite": fav, "folder": None, "origin": "config"}


FLEET = [h("harv-fake", version="v1.9.0", vms=17, labels={"site": "cave"}),
         h("harvlab2", state="unreachable", version="v1.8.2", error="dial tcp: i/o timeout"),
         h("gpudemo", version="v1.9.0", vms=3),
         h("node2", "kvm", version="12.8.0", vms=15, labels={"site": "cave", "role": "bench"}),
         h("ghost", "kvm", state="unreachable", error="No route to host")]


@pytest.fixture
def index(context, flask_server):
    state = {"hosts": FLEET, "sent": [], "role": "admin", "calls": []}
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_sidebar_pinned','1');")
    page = context.new_page()

    def ok(route, body, status=200):
        return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def kvm(route, req):
        path = req.url.split("/api/kvm/", 1)[1].split("?", 1)[0]
        if req.method == "GET":
            names = [x["name"] for x in state["hosts"] if x["kind"] == "kvm"]
            return ok(route, {"hosts": [{"name": n, "labels": {}} for n in names], "role": state["role"]})
        state["sent"].append((req.method, path))
        return ok(route, {"action_id": f"kvm{len(state['sent']):09d}"}, 202)

    def fav(route, req):
        state["sent"].append((req.method, req.url.split("/api/", 1)[1]))
        return ok(route, {"ok": True})

    page.route(re.compile(r".*/api/hosts$"), lambda r, q: ok(r, {"families": {}, "hosts": state["hosts"]}))
    page.route(re.compile(r".*/api/kvm/.*"), kvm)
    page.route(re.compile(r".*/api/me/favorites/.*"), fav)
    page.route(re.compile(r".*/api/whoami$"), lambda r, q: ok(r, {"role": state["role"], "roles_active": True,
                                                                  "user": "u", "auth": "local"}))
    page.route(re.compile(r".*/api/stream/kvm\d+$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream",
        body='event: end\ndata: {"status": "done", "result": {"output": {"ok": true, "checks": []}}}\n\n'))
    page.on("request", lambda r: state["calls"].append(r.url))
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    state["errors"] = errors

    def go(hash):
        goto(page, flask_server["base_url"], hash)
        expect(page.locator("#tab-hosts")).to_have_class(re.compile(r"\bactive\b"))
        return page
    return go, page, state


def names(page):
    return page.locator("#hosts-index-host tr[data-hix-row]").evaluate_all("els => els.map(e => e.dataset.hixRow)")


def test_every_host_of_both_families_with_its_columns(index):
    go, page, state = index
    go("#hosts")
    expect(page.locator("#hosts-index-host tr[data-hix-row]")).to_have_count(5)
    assert names(page) == ["kvm/ghost", "harvester/gpudemo", "harvester/harv-fake", "harvester/harvlab2", "kvm/node2"]
    row = page.locator('#hosts-index-host tr[data-hix-row="kvm/node2"]')
    for text in ("KVM", "node2", "reachable", "12.8.0", "site=cave", "role=bench", "15", "Not filed"):
        expect(row).to_contain_text(text)
    expect(row.locator("a.hix-name")).to_have_attribute("href", "#kvm/node2")
    expect(page.locator('#hosts-index-host tr[data-hix-row="harvester/harv-fake"] a.hix-name')).to_have_attribute(
        "href", "#harvester/harv-fake")
    bad = page.locator('#hosts-index-host tr[data-hix-row="harvester/harvlab2"] .badge.fail')
    assert "i/o timeout" in bad.get_attribute("title")
    expect(page.locator("#hosts-index-host .res-count")).to_have_text("5 of 5 hosts")
    # source unique : l'index ne lit aucun hôte (ni KVM, ni la flotte des VMs)
    assert not [c for c in state["calls"] if re.search(r"/api/(kvm/hosts/|kvm/vms|vms-all|status/(?!harv-fake))", c)]
    assert state["errors"] == []


# (adresse, filtres posés, lignes attendues)
PRESETS = [
    ("#hosts/harvester", {"kind": "harvester"}, ["harvester/gpudemo", "harvester/harv-fake", "harvester/harvlab2"]),
    ("#hosts/kvm", {"kind": "kvm"}, ["kvm/ghost", "kvm/node2"]),
    ("#hosts/folder/prod", {"kind": ""}, ["kvm/ghost", "harvester/gpudemo", "harvester/harv-fake",
                                          "harvester/harvlab2", "kvm/node2"]),
]


@pytest.mark.parametrize("hash,filters,want", PRESETS, ids=[p[0] for p in PRESETS])
def test_address_prefilters_the_family(index, hash, filters, want):
    go, page, _ = index
    go(hash)
    expect(page.locator("#hosts-index-host tr[data-hix-row]")).to_have_count(len(want))
    assert names(page) == want
    for f, v in filters.items():
        assert page.locator(f'#hosts-index-host [data-hix-filter="{f}"]').input_value() == v
    expect(page.locator('#host-nav [data-hnav-root="hosts"]')).to_have_class(re.compile(r"\bactive\b"))


def test_changing_the_family_filter_rewrites_the_address(index):
    go, page, _ = index
    go("#hosts")
    page.select_option('#hosts-index-host [data-hix-filter="kind"]', "kvm")
    page.wait_for_function("() => location.hash === '#hosts/kvm'")
    page.reload()
    page.wait_for_function("() => window.Router && Router.current()")
    expect(page.locator("#hosts-index-host tr[data-hix-row]")).to_have_count(2)


# (filtre, valeur, lignes attendues)
FILTERS = [
    ("state", "alert", ["kvm/ghost", "harvester/harvlab2"]),
    ("state", "ok", ["harvester/gpudemo", "harvester/harv-fake", "kvm/node2"]),
    ("label", "site=cave", ["harvester/harv-fake", "kvm/node2"]),
    ("label", "role=bench", ["kvm/node2"]),
    ("version", "v1.9.0", ["harvester/gpudemo", "harvester/harv-fake"]),
]


@pytest.mark.parametrize("filt,value,want", FILTERS, ids=[f"{f[0]}={f[1]}" for f in FILTERS])
def test_filters(index, filt, value, want):
    go, page, _ = index
    go("#hosts")
    page.select_option(f'#hosts-index-host [data-hix-filter="{filt}"]', value)
    expect(page.locator("#hosts-index-host tr[data-hix-row]")).to_have_count(len(want))
    assert names(page) == want


def test_sort_by_state_puts_hosts_in_alert_first_and_reverses(index):
    go, page, _ = index
    go("#hosts")
    th = page.locator('#hosts-index-host th[data-sort="state"]')
    th.click()
    assert names(page)[:2] == ["kvm/ghost", "harvester/harvlab2"]
    expect(th).to_have_attribute("aria-sort", "ascending")
    th.click()
    assert set(names(page)[-2:]) == {"kvm/ghost", "harvester/harvlab2"}
    expect(th).to_have_attribute("aria-sort", "descending")
    page.locator('#hosts-index-host th[data-sort="vms"]').click()
    assert names(page)[-3:] == ["harvester/gpudemo", "kvm/node2", "harvester/harv-fake"]


def test_a_hundred_hosts(index):
    go, page, state = index
    state["hosts"] = [h(f"h{i:03d}", "kvm" if i % 2 else "harvester", "unreachable" if i in (40, 77) else "ok")
                      for i in range(100)]
    go("#hosts")
    expect(page.locator("#hosts-index-host tr[data-hix-row]")).to_have_count(100)
    page.select_option('#hosts-index-host [data-hix-filter="state"]', "alert")
    expect(page.locator("#hosts-index-host tr[data-hix-row]")).to_have_count(2)


def test_star_from_the_index(index):
    go, page, state = index
    go("#hosts")
    star = page.locator('#hosts-index-host [data-hix="star"][data-key="kvm/node2"]')
    star.click()
    expect(star).to_have_attribute("aria-pressed", "true")
    assert ("PUT", "me/favorites/kvm/node2") in state["sent"]


def test_kvm_rows_offer_test_and_remove_and_the_declare_button(index):
    go, page, state = index
    go("#hosts/kvm")
    add = page.locator('#hosts-index-host [data-hix="add"]')
    expect(add).to_contain_text("Declare a host")
    row = page.locator('#hosts-index-host tr[data-hix-row="kvm/node2"]')
    row.locator('[data-hix="test"]').click()
    expect(row.locator(".hix-msg")).to_contain_text("Connection test passed")
    assert ("POST", "hosts/node2/test") in state["sent"]
    row.locator('[data-hix="remove"]').click()
    expect(row.locator(".kvm-confirm")).to_contain_text("Remove host node2 from the console?")
    assert not [s for s in state["sent"] if s[0] == "DELETE"]
    row.locator('[data-hix="remove-yes"]').click()
    expect(row.locator(".hix-msg")).to_contain_text("Host node2 removed")
    assert ("DELETE", "hosts/node2") in state["sent"]
    # le geste « Déclarer » ouvre la fenêtre de KVM > Hôtes
    add.click()
    expect(page.locator(".floating-panel form.kvm-declare")).to_be_visible()
    # les hôtes Harvester n'ont pas ces boutons
    go("#hosts")
    assert page.locator('#hosts-index-host tr[data-hix-row="harvester/harv-fake"] [data-hix="test"]').count() == 0


@pytest.mark.parametrize("role,test_on,remove_on", [("operator", True, False), ("viewer", False, False)])
def test_kvm_buttons_greyed_by_role_with_the_reason(index, role, test_on, remove_on):
    go, page, state = index
    state["role"] = role
    go("#hosts/kvm")
    row = page.locator('#hosts-index-host tr[data-hix-row="kvm/node2"]')
    t, r = row.locator('[data-hix="test"]'), row.locator('[data-hix="remove"]')
    expect(r).to_be_disabled()
    assert t.is_enabled() is test_on and r.is_enabled() is remove_on
    assert r.get_attribute("title") == "Needs the admin role"
    expect(page.locator('#hosts-index-host [data-hix="add"]')).to_be_disabled()


def test_a_link_to_a_vanished_host_lands_here_with_the_notice(index):
    go, page, _ = index
    go("#harvester/absent/params/storage")
    assert page.evaluate("location.hash") == "#hosts"
    notice = page.locator("#access-notice")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("absent")


def test_every_control_has_a_tooltip(index):
    go, page, _ = index
    go("#hosts/kvm")
    for el in page.locator("#hosts-index-host select, #hosts-index-host button, #hosts-index-host th[data-sort]").all():
        assert el.get_attribute("title") or el.get_attribute("data-tip"), el.evaluate("e => e.outerHTML")
