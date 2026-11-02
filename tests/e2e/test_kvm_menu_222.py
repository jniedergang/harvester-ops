"""2.2.2 : le menu d'une VM KVM de la vue Machines virtuelles est celui de la
page de l'hôte.

Avant, la vue n'offrait que console, instantanés, clonage, suppression,
rangement et « ouvrir sur son hôte » : ni Modifier ni gestes
d'alimentation. Le menu est maintenant construit par kvm.js (openVmMenu),
d'après les tables de la console servies avec la liste (`kvm_tables`). On
vérifie : mêmes entrées, mêmes grisés et mêmes raisons que la page de l'hôte
(VM en marche, VM arrêtée, VM transitoire, lecteur), « Modifier » ouvre la
fenêtre de modification, un geste part vers son hôte, l'arrêt forcé demande
une confirmation dans la vue, la page de l'hôte garde « Modifier »."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import kvm_form_fakes as F  # noqa: E402
from test_folders_200 import KUUID, kvm, world  # noqa: E402,F401  (fixture)

AVM = "#all-vms-host"
ITEMS = """els => els.map(e => [e.dataset.kvmGesture || e.dataset.kvmOp || e.dataset.kvmHot
  || (e.dataset.kvmConsole && 'console') || e.dataset.vmfKvm, !!e.getAttribute('aria-disabled'), e.dataset.tip])"""
OFF_UUID = "0d6f2a43-3c55-4c1e-9a1e-2f1d1c0b0a01"
TMP_UUID = "0d6f2a43-3c55-4c1e-9a1e-2f1d1c0b0a02"


@pytest.fixture
def kw(world):
    go, page, state = world
    state["vms"] += [dict(kvm("kvm-off", OFF_UUID), state="shut off", phase="Stopped"),
                     dict(kvm("temp", TMP_UUID), persistent=False)]
    k = F.install(page, {"role": state["role"],
                         "vms": [{"host": "node2", "name": n, "state": s, "persistent": p, "autostart": False,
                                  "vcpus": 1, "memory_kib": 1048576}
                                 for n, s, p in (("kvmtest-1", "running", True), ("kvm-off", "shut off", True),
                                                 ("temp", "running", False))]})
    return go, page, state, k


def unified_menu(page, uuid):
    page.locator(f'{AVM} tr[data-avm-row="kvm/{uuid}"] [data-avm-act="more"]').click()
    menu = page.locator(".vma-menu")
    expect(menu).to_have_count(1)
    return menu


def host_menu(page, name):
    row = page.locator(f'#tab-kvmvms tr[data-kvm-vm="node2/{name}"]')
    expect(row).to_be_visible(timeout=8000)
    row.locator('[data-kvm="menu"]').click()
    menu = page.locator(".vma-menu")
    expect(menu).to_have_count(1)
    return menu


def close(page):
    page.keyboard.press("Escape")
    expect(page.locator(".vma-menu")).to_have_count(0)


@pytest.mark.parametrize("role", ["operator", "admin", "viewer"])
def test_same_entries_greys_and_reasons_as_the_host_page(kw, role):
    go, page, state, k = kw
    state["role"] = k["role"] = role
    go("#vms")
    page.wait_for_function(f"() => App.role && App.role() === '{role}'")
    got = {}
    for name, uuid in (("kvmtest-1", KUUID), ("kvm-off", OFF_UUID), ("temp", TMP_UUID)):
        items = unified_menu(page, uuid).locator(".vma-item").evaluate_all(ITEMS)
        close(page)
        assert [i[0] for i in items[-2:]] == ["folder", "page"]
        got[name] = items[:-2]
    page.evaluate("Router.go('#kvm/node2/vms')")
    for name in got:
        want = host_menu(page, name).locator(".vma-item").evaluate_all(ITEMS)
        close(page)
        assert got[name] == want, name
    run = {i[0]: (i[1], i[2]) for i in got["kvmtest-1"]}
    off = {i[0]: (i[1], i[2]) for i in got["kvm-off"]}
    if role == "viewer":
        for g in ("start", "shutdown", "reboot", "suspend", "resume", "destroy", "console", "edit", "clone"):
            assert run[g][0] and "operator role" in run[g][1], g
        return
    # VM en marche : arrêter, redémarrer, suspendre, arrêt forcé, Modifier
    assert [g for g, (dis, _) in run.items() if not dis] == \
        ["shutdown", "reboot", "suspend", "destroy", "console", "snapshots", "edit"]
    assert run["start"][0] and "running" in run["start"][1]
    assert run["clone"][0] and "shut off" in run["clone"][1].lower()
    # VM arrêtée : démarrer, cloner (et supprimer pour l'administrateur)
    assert not off["start"][0] and off["shutdown"][0] and not off["clone"][0] and not off["edit"][0]
    assert off["delete"][0] == (role != "admin")
    if role != "admin":
        assert "administrator" in off["delete"][1].lower() or "admin" in off["delete"][1].lower()
    # VM transitoire : ni clonage ni suppression, raison dite
    tmp = {i[0]: (i[1], i[2]) for i in got["temp"]}
    assert tmp["clone"][0] and "transient" in tmp["clone"][1].lower()


def test_edit_opens_the_edit_window_of_that_vm(kw):
    go, page, state, k = kw
    go("#vms")
    menu = unified_menu(page, KUUID)
    menu.locator('[data-kvm-hot="edit"]').click()
    expect(page.locator(".vma-menu")).to_have_count(0)
    panel = page.locator(".floating-panel").filter(has=page.locator('.kvmf[data-kvmf-mode="edit"]')).last
    expect(panel.locator('.vm-create-section[data-vmf-section="general"]')).to_be_visible(timeout=8000)
    expect(panel.locator(".kvmf-edit-head")).to_contain_text("kvmtest-1")
    expect(panel.locator(".kvmf-edit-head")).to_contain_text("node2")
    assert any(g.startswith("vms/node2/kvmtest-1") for g in k["gets"]), k["gets"]
    # rien n'est écrit en ouvrant la fenêtre
    assert not [s for s in k["sent"] if s[0] in ("PATCH", "POST")]
    assert state["errors"] == []


def test_a_gesture_goes_to_the_host_and_the_view_tells_the_outcome(kw):
    go, page, state, k = kw
    go("#vms")
    unified_menu(page, KUUID).locator('[data-kvm-gesture="shutdown"]').click()
    fb = page.locator(f'{AVM} [data-avm="feedback"]')
    expect(fb).to_contain_text("kvmtest-1", timeout=8000)
    assert ("POST", "vms/node2/kvmtest-1/shutdown") in [tuple(s[:2]) for s in k["sent"]], k["sent"]


def test_force_off_asks_in_the_view_and_no_cancels(kw):
    go, page, state, k = kw
    go("#vms")
    unified_menu(page, KUUID).locator('[data-kvm-gesture="destroy"]').click()
    fb = page.locator(f'{AVM} [data-avm="feedback"]')
    expect(fb.locator('[data-kvm="confirm-no"]')).to_be_focused()
    assert not [s for s in k["sent"] if s[1].endswith("/destroy")]
    fb.locator('[data-kvm="confirm-no"]').click()
    expect(fb).to_be_empty()
    unified_menu(page, KUUID).locator('[data-kvm-gesture="destroy"]').click()
    fb.locator('[data-kvm="destroy-yes"]').click()
    expect(fb.locator('[data-kvm="destroy-yes"]')).to_have_count(0)
    expect(fb).to_contain_text("kvmtest-1", timeout=8000)
    assert [s[1] for s in k["sent"] if s[1].endswith("/destroy")] == ["vms/node2/kvmtest-1/destroy"]


def test_a_greyed_entry_does_nothing(kw):
    go, page, state, k = kw
    state["role"] = k["role"] = "viewer"
    go("#vms")
    page.wait_for_function("() => App.role && App.role() === 'viewer'")
    menu = unified_menu(page, KUUID)
    for sel in ('[data-kvm-hot="edit"]', '[data-kvm-gesture="shutdown"]'):
        menu.locator(sel).dispatch_event("click")
    expect(page.locator('.kvmf[data-kvmf-mode="edit"]')).to_have_count(0)
    assert not k["sent"]


def test_the_host_page_menu_still_offers_edit(kw):
    go, page, state, k = kw
    go("#kvm/node2/vms", tree=False)
    menu = host_menu(page, "kvmtest-1")
    item = menu.locator('[data-kvm-hot="edit"]')
    expect(item).to_be_visible()
    assert item.get_attribute("aria-disabled") is None
    item.click()
    panel = page.locator(".floating-panel").filter(has=page.locator('.kvmf[data-kvmf-mode="edit"]')).last
    expect(panel.locator('.vm-create-section[data-vmf-section="general"]')).to_be_visible(timeout=8000)
