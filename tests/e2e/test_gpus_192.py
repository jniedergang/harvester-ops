"""v1.92.0 : l'onglet Advanced > GPU, l'inventaire des GPU (lecture seule).

L'API est routée : un cluster avec un GPU entier pris par une VM, un vGPU
libre, une instance MIG non synchronisée et des GPU de conteneurs pris par un
pod ; la vue de tous les clusters avec un cluster éteint et un sans l'add-on.
On vérifie la place de l'onglet, le sélecteur de portée, les cartes, les
filtres, les états vides qui parlent et les liens vers la VM et le pod."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


def unit(cluster, uid, typ, node, state, total=1, used=0, consumers=(), **extra):
    u = {"id": uid, "type": typ, "cluster": cluster, "node": node, "name": uid.split("/")[0], "model": "NVIDIA A2",
         "address": "0000:3b:00.0" if typ != "node" else "", "parent": "", "resource": "", "state": state,
         "total": total, "used": used, "free": total - used, "consumers": list(consumers)}
    u.update(extra)
    return u


def vmc(cluster, ns, name):
    return {"kind": "vm", "cluster": cluster, "namespace": ns, "name": name}


def podc(cluster, ns, name, count=1, system=False):
    return {"kind": "pod", "cluster": cluster, "namespace": ns, "name": name, "count": count, "system": system}


SUM0 = {t: {"total": 0, "used": 0, "free": 0} for t in ("gpu", "vgpu", "mig", "node")}
AVAIL = {"addon": True, "gpu": True, "vgpu": True, "mig": True, "node": True}


def units_for(c):
    return [
        unit(c, "n1-00003b000", "gpu", "n1", "used", used=1, consumers=[vmc(c, "prod", "train")], reason=""),
        unit(c, "n1-0000af000", "gpu", "n1", "disabled", reason="vgpu", vgpus=2),
        unit(c, "n1-0000af004", "vgpu", "n1", "free", parent="n1-0000af000", model="NVIDIA A2-4Q"),
        unit(c, "n2-00003b000/mig", "mig", "n2", "free", total=2, used=1, sync="out-of-sync",
             message="profile 9 not applied", model="MIG 3g.20gb x2",
             profiles=[{"name": "MIG 3g.20gb", "id": 9, "created": 2, "available": 0, "total": 2, "requested": 2}],
             consumers=[vmc(c, "ml", "big")]),
        unit(c, "n3/nvidia.com/gpu", "node", "n3", "free", total=4, used=2,
             consumers=[podc(c, "ml", "trainer-0", 2), podc(c, "gpu-operator-system", "validator", 1, True)]),
    ]


CURRENT = {"cluster": "harv-fake", "addon": True, "available": AVAIL, "denials": [], "units": units_for("harv-fake"),
           "summary": {"gpu": {"total": 2, "used": 1, "free": 0}, "vgpu": {"total": 1, "used": 0, "free": 1},
                       "mig": {"total": 2, "used": 1, "free": 1}, "node": {"total": 4, "used": 2, "free": 2}}}
ALL = {"clusters": [{"cluster": "alpha", "state": "ok", "count": 5, "addon": True, "available": AVAIL},
                    {"cluster": "bravo", "state": "ok", "count": 1, "addon": True, "available": AVAIL},
                    {"cluster": "charlie", "state": "unreachable"},
                    {"cluster": "delta", "state": "no-addon", "count": 0, "addon": False}],
       "units": units_for("alpha") + [unit("bravo", "b1-00003b000", "gpu", "b1", "free", reason="")],
       "summary": SUM0}
EMPTY = {"cluster": "harv-fake", "addon": True, "available": AVAIL, "denials": [], "units": [], "summary": SUM0}
NO_ADDON = dict(EMPTY, addon=False, available=dict(AVAIL, addon=False, gpu=False, vgpu=False, mig=False))


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "if (!sessionStorage.getItem('gpu-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','namespaces');"
        "localStorage.removeItem('harvester_ops_gpus');"
        "localStorage.removeItem('harvester_ops_allcontainers');"
        "sessionStorage.setItem('gpu-init','1'); }")
    page = context.new_page()
    state = {"current": CURRENT}
    page.route(re.compile(r".*/api/gpus/harv-fake(\?.*)?$"), lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(state["current"])))
    page.route(re.compile(r".*/api/gpus-all(\?.*)?$"), lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(ALL)))
    page.route("**/api/containers-all", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"clusters": [], "workloads": []})))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.Gpus && window.Sections && window.App && App.getCurrentCluster()")
    page.mouse.move(1200, 600)
    page.evaluate("Sections.open('advanced', 'gpu')")
    pane = page.locator('#tab-advanced .section-pane[data-pane="gpu"]')
    return page, pane, state


def rows(pane):
    return pane.locator("tbody tr[data-unit]")


def test_gpu_tab_sits_next_to_pci_and_usb(ui):
    page, pane, _ = ui
    assert page.locator("#tab-advanced [data-section-tab]").evaluate_all("els => els.map(e => e.dataset.sectionTab)") \
        == ["settings", "pci", "usb", "gpu", "sriov", "support"]
    expect(rows(pane)).to_have_count(5, timeout=8000)
    # chaque contrôle a sa bulle d'aide
    for el in pane.locator(".res-tools button, .res-tools select, .res-tools input").all():
        assert el.get_attribute("data-tip"), el.evaluate("e => e.outerHTML")


def test_summary_cards_states_and_consumers(ui):
    _, pane, _ = ui
    expect(rows(pane)).to_have_count(5, timeout=8000)
    cards = pane.locator(".gpu-metric")
    expect(cards).to_have_count(4)
    expect(pane.locator('.gpu-metric[data-gpu-type="gpu"] .value')).to_have_text("2")
    expect(pane.locator('.gpu-metric[data-gpu-type="gpu"]')).to_contain_text("1 used, 0 free")
    expect(pane.locator('.gpu-metric[data-gpu-type="node"]')).to_contain_text("2 used, 2 free")
    mig = pane.locator('tr[data-type="mig"]')
    expect(mig.locator(".badge.warn")).to_have_text("out of sync")
    expect(mig.locator(".badge.warn")).to_have_attribute("data-tip", "profile 9 not applied")
    node = pane.locator('tr[data-type="node"]')
    expect(node.locator('[data-gpu="open-pod"]')).to_have_count(1)            # le pod système n'est pas un lien
    expect(node).to_contain_text("x2")
    expect(pane.locator('tr[data-unit="harv-fake|n1-0000af000"] .badge.tip')).to_have_attribute(
        "data-tip", re.compile("vGPUs"))


def test_filters_by_node_type_state_and_text(ui):
    _, pane, _ = ui
    expect(rows(pane)).to_have_count(5, timeout=8000)
    pane.locator('[data-gpu="type"]').select_option("gpu")
    expect(rows(pane)).to_have_count(2)
    pane.locator('[data-gpu="state"]').select_option("disabled")
    expect(rows(pane)).to_have_count(1)
    pane.locator('[data-gpu="state"]').select_option("")
    pane.locator('[data-gpu="type"]').select_option("")
    pane.locator('[data-gpu="node"]').select_option("n3")
    expect(rows(pane)).to_have_count(1)
    expect(pane.locator('.gpu-metric[data-gpu-type="gpu"] .value')).to_have_text("0")   # cartes suivent le nœud
    pane.locator('[data-gpu="node"]').select_option("")
    pane.locator('[data-gpu="text"]').fill("train")
    expect(rows(pane)).to_have_count(2)                                 # la VM train, le pod trainer-0
    pane.locator('[data-gpu="text"]').fill("nothing-like-this")
    expect(pane.locator(".gpu-empty")).to_have_text("No unit matches these filters.")


def test_scope_all_clusters_says_the_clusters_not_read(ui):
    _, pane, _ = ui
    expect(rows(pane)).to_have_count(5, timeout=8000)
    pane.locator('[data-gpu="scope"][data-scope="all"]').click()
    expect(rows(pane)).to_have_count(6)
    expect(pane.locator('[data-gpu="scope"][data-scope="all"]')).to_have_attribute("aria-pressed", "true")
    chips = pane.locator('[data-gpu="clusters"] .avm-chip')
    expect(chips).to_have_count(2)
    expect(chips.nth(0)).to_contain_text("charlie")
    expect(chips.nth(1)).to_contain_text("add-on off")
    pane.locator('[data-gpu="cluster"]').select_option("bravo")
    expect(rows(pane)).to_have_count(1)
    expect(pane.locator('.gpu-metric[data-gpu-type="gpu"] .value')).to_have_text("1")
    pane.locator('[data-gpu="scope"][data-scope="cluster"]').click()
    expect(rows(pane)).to_have_count(5)
    expect(pane.locator('[data-gpu="cluster"]')).to_be_hidden()


@pytest.mark.parametrize("body,text", [
    (EMPTY, "No GPU on this cluster."),
    (NO_ADDON, "GPU devices add-on is not enabled"),
])
def test_empty_states_speak(ui, body, text):
    page, pane, state = ui
    expect(rows(pane)).to_have_count(5, timeout=8000)
    state["current"] = body
    pane.locator('[data-gpu="refresh"]').click()
    expect(pane.locator(".gpu-empty")).to_contain_text(text)
    if body is NO_ADDON:
        expect(pane.locator('[data-gpu="open-addons"]')).to_be_visible()


def test_consumer_links_open_the_vm_and_the_pod(ui):
    page, pane, _ = ui
    expect(rows(pane)).to_have_count(5, timeout=8000)
    page.evaluate("() => { window.__opened = []; VMEdit.open = (c, n, v) => window.__opened.push([c, n, v]); }")
    pane.locator('[data-gpu="open-vm"]').first.click()
    assert page.evaluate("window.__opened") == [["harv-fake", "prod", "train"]]
    pane.locator('[data-gpu="open-pod"]').click()
    expect(page.locator("#tab-allcontainers")).to_be_visible()
    expect(page.locator('#all-containers-host [data-ct="text"]')).to_have_value("trainer-0")
