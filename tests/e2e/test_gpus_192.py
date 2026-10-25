"""v1.92.0 : l'inventaire des GPU (lecture seule) ; v1.93.0 : déplacé dans
Shared resources > GPU, vue de la flotte avec un filtre par cluster.

L'API est routée : la vue de tous les clusters avec un cluster qui a un GPU
entier pris par une VM, un vGPU libre, une instance MIG non synchronisée et
des GPU de conteneurs pris par un pod, un deuxième cluster, un cluster éteint
et un sans l'add-on. On vérifie la place dans le menu (et le retrait de
Advanced), les cartes, les filtres (cluster compris), les états vides qui
parlent et les liens vers la VM et le pod."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto  # noqa: E402


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


ALL = {"clusters": [{"cluster": "alpha", "state": "ok", "count": 5, "addon": True, "available": AVAIL},
                    {"cluster": "bravo", "state": "ok", "count": 1, "addon": True,
                     "available": dict(AVAIL, vgpu=False, mig=False)},
                    {"cluster": "charlie", "state": "unreachable"},
                    {"cluster": "delta", "state": "no-addon", "count": 0, "addon": False},
                    {"cluster": "echo", "state": "ok", "count": 0, "addon": True, "available": AVAIL}],
       "units": units_for("alpha") + [unit("bravo", "b1-00003b000", "gpu", "b1", "free", reason="")],
       "summary": SUM0}
EMPTY = dict(ALL, units=[])


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "if (!sessionStorage.getItem('gpu-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_gpus', JSON.stringify({scope: 'cluster'}));"   # réglage de 1.92.0
        "localStorage.removeItem('harvester_ops_allcontainers');"
        "sessionStorage.setItem('gpu-init','1'); }")
    page = context.new_page()
    state = {"all": ALL, "calls": []}

    def gpus_all(route, req):
        state["calls"].append(req.url)
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(state["all"]))
    page.route(re.compile(r".*/api/gpus-all(\?.*)?$"), gpus_all)
    page.route(re.compile(r".*/api/gpus/[^/?]+(\?.*)?$"), lambda r, q: (state["calls"].append(q.url), r.fulfill(
        status=200, content_type="application/json", body="{}"))[1])
    page.route("**/api/containers-all", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"clusters": [], "workloads": []})))
    goto(page, flask_server["base_url"], address('namespaces'))
    page.wait_for_function("window.Gpus && window.App && App.getCurrentCluster()")
    page.mouse.move(1200, 600)
    page.evaluate("App.setTab('gpus')")
    pane = page.locator("#tab-gpus")
    return page, pane, state


def rows(pane):
    return pane.locator("tbody tr[data-unit]")


def test_gpu_lives_in_shared_resources_after_containers(ui):
    page, pane, state = ui
    # 2.0.0 : le groupe « Ressources partagées » du menu des hôtes vient
    # après les racines de flotte (Migrations) et garde GPU, Réservations, Usage
    roots = page.locator("#host-nav [data-hnav-root]").evaluate_all("els => els.map(e => e.dataset.hnavRoot)")
    assert roots[roots.index("migrations") + 1] == "gpus"
    group = page.locator("#hnav-shared")
    expect(page.locator("#hnav-shared").locator("xpath=preceding-sibling::div[1]")).to_have_text("Shared resources")
    assert group.locator("[data-hnav-root]").evaluate_all("els => els.map(e => e.dataset.hnavRoot)") == ["gpus", "bookings", "usage"]
    for t in group.locator(".tab").all():
        assert t.get_attribute("title")
    expect(group.locator('[data-hnav-root="gpus"]')).to_have_class(re.compile("active"))
    # plus d'onglet GPU dans Advanced
    assert page.locator('#tab-advanced [data-section-tab="gpu"]').count() == 0
    assert page.locator('#tab-advanced [data-pane="gpu"]').count() == 0
    expect(rows(pane)).to_have_count(6, timeout=8000)
    # la vue de la flotte : jamais /api/gpus/<un cluster>, même avec un ancien réglage « ce cluster »
    assert state["calls"] and all("/api/gpus-all" in u for u in state["calls"])
    expect(pane.locator('[data-gpu="scope"]')).to_have_count(0)
    for el in pane.locator(".res-tools button, .res-tools select, .res-tools input").all():
        assert el.get_attribute("data-tip"), el.evaluate("e => e.outerHTML")


def test_summary_cards_states_and_consumers(ui):
    _, pane, _ = ui
    expect(rows(pane)).to_have_count(6, timeout=8000)
    expect(pane.locator(".gpu-metric")).to_have_count(4)
    expect(pane.locator('.gpu-metric[data-gpu-type="gpu"] .value')).to_have_text("3")
    expect(pane.locator('.gpu-metric[data-gpu-type="gpu"]')).to_contain_text("1 used, 1 free")
    expect(pane.locator('.gpu-metric[data-gpu-type="node"]')).to_contain_text("2 used, 2 free")
    mig = pane.locator('tr[data-type="mig"]')
    expect(mig.locator(".badge.warn")).to_have_text("out of sync")
    expect(mig.locator(".badge.warn")).to_have_attribute("data-tip", "profile 9 not applied")
    node = pane.locator('tr[data-type="node"]')
    expect(node.locator('[data-gpu="open-pod"]')).to_have_count(1)            # le pod système n'est pas un lien
    expect(node).to_contain_text("x2")
    expect(pane.locator('tr[data-unit="alpha|n1-0000af000"] .badge.tip')).to_have_attribute(
        "data-tip", re.compile("vGPUs"))
    # le cluster est une colonne
    expect(pane.locator("thead th").first).to_have_text("Cluster")


def test_filters_by_cluster_node_type_state_and_text(ui):
    _, pane, _ = ui
    expect(rows(pane)).to_have_count(6, timeout=8000)
    sel = pane.locator('[data-gpu="cluster"]')
    assert sel.locator("option").all_inner_texts()[1:] == ["alpha", "bravo", "charlie", "delta", "echo"]
    chips = pane.locator('[data-gpu="clusters"] .avm-chip')
    expect(chips).to_have_count(2)                                      # éteint, sans l'add-on
    expect(chips.nth(0)).to_contain_text("charlie")
    expect(chips.nth(1)).to_contain_text("add-on off")
    sel.select_option("alpha")
    expect(rows(pane)).to_have_count(5)
    expect(pane.locator('[data-gpu="clusters"]')).to_have_count(0)      # les autres clusters ne sont plus dits
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
    pane.locator('[data-gpu="text"]').fill("")
    sel.select_option("bravo")
    expect(rows(pane)).to_have_count(1)
    expect(pane.locator('.gpu-metric[data-gpu-type="gpu"] .value')).to_have_text("1")
    expect(pane.locator('[data-gpu="notice"]')).to_contain_text("Not available on this cluster: vGPU")      # vGPU et MIG absents


@pytest.mark.parametrize("cluster,text", [
    ("echo", "No GPU on this cluster."),
    ("delta", "GPU devices add-on is not enabled"),
    ("charlie", ""),
])
def test_empty_states_speak(ui, cluster, text):
    _, pane, _ = ui
    expect(rows(pane)).to_have_count(6, timeout=8000)
    pane.locator('[data-gpu="cluster"]').select_option(cluster)
    empty = pane.locator(".gpu-empty")
    expect(empty).to_be_visible()
    assert empty.inner_text().strip()                                   # jamais une vue vide muette
    if text:
        expect(empty).to_contain_text(text)
    if cluster == "delta":
        expect(pane.locator('[data-gpu="open-addons"]')).to_be_visible()


def test_no_gpu_anywhere(ui):
    _, pane, state = ui
    expect(rows(pane)).to_have_count(6, timeout=8000)
    state["all"] = EMPTY
    pane.locator('[data-gpu="refresh"]').click()
    expect(pane.locator(".gpu-empty")).to_contain_text("No GPU on the clusters that answered.")


def test_consumer_links_open_the_vm_and_the_pod(ui):
    page, pane, _ = ui
    expect(rows(pane)).to_have_count(6, timeout=8000)
    page.evaluate("() => { window.__opened = []; VMEdit.open = (c, n, v) => window.__opened.push([c, n, v]); }")
    pane.locator('[data-gpu="open-vm"]').first.click()
    assert page.evaluate("window.__opened") == [["alpha", "prod", "train"]]
    pane.locator('[data-gpu="open-pod"]').click()
    expect(page.locator("#tab-allcontainers")).to_be_visible()
    expect(page.locator('#all-containers-host [data-ct="text"]')).to_have_value("trainer-0")


def test_a_booking_by_count_is_said_at_pool_level_not_pinned_on_each_unit(ui):
    """v1.94.1 : « 1 x vGPU NVIDIA A2-4Q » peignait « Booked by bob » sur
    CHAQUE vGPU du modèle, comme si tous étaient pris. Chaque unité du lot
    dit « 1 of 2 booked now » ; le nom du titulaire n'est que dans la bulle.
    Une réservation de l'unité précise garde son badge « Booked by »."""
    page, pane, state = ui
    expect(rows(pane)).to_have_count(6, timeout=8000)
    now = page.evaluate("Date.now()")

    def iso(ms):
        return page.evaluate(f"new Date({ms}).toISOString()")

    def bk(bid, typ, model, count, holder, unit=""):
        return {"id": bid, "cluster": "alpha", "type": typ, "model": model, "unit": unit, "count": count,
                "holder": {"kind": "user", "name": holder}, "start": iso(now - 3600000), "end": iso(now + 3600000),
                "state": "active", "cancelled_at": "", "target": None, "note": ""}
    listing = {"user": "alice", "role": "operator", "bookings": [
        bk("bk-1", "vgpu", "NVIDIA A2-4Q", 1, "bob"),
        bk("bk-2", "node", "NVIDIA A2", 4, "ml-team"),                  # ressource vide : le modèle
        bk("bk-3", "gpu", "NVIDIA A2", 1, "carol", unit="n1-00003b000")]}
    page.route(re.compile(r".*/api/bookings(\?.*)?$"), lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(listing)))
    state["all"] = dict(ALL, pools=[
        {"cluster": "alpha", "type": "gpu", "model": "NVIDIA A2", "capacity": 1, "used": 1},
        {"cluster": "alpha", "type": "vgpu", "model": "NVIDIA A2-4Q", "capacity": 2, "used": 0},
        {"cluster": "alpha", "type": "node", "model": "NVIDIA A2", "capacity": 4, "used": 2}])
    pane.locator('[data-gpu="refresh"]').click()
    vg = pane.locator('tr[data-unit="alpha|n1-0000af004"] .bkg-badge')
    expect(vg).to_have_count(1, timeout=8000)
    expect(vg).to_have_text(re.compile(r"1 of 2 booked now$"))
    expect(vg).to_have_attribute("data-bkg-badge", "pool")
    expect(vg).to_have_class(re.compile(r"\binfo\b"))
    expect(vg).not_to_contain_text("bob")
    assert "bob" in vg.get_attribute("data-tip") and "NVIDIA A2-4Q" in vg.get_attribute("data-tip")
    # le lot plein : 4 of 4, en alerte
    node = pane.locator('tr[data-unit="alpha|n3/nvidia.com/gpu"] .bkg-badge')
    expect(node).to_have_text(re.compile(r"4 of 4 booked now$"))
    expect(node).to_have_class(re.compile(r"\bwarn\b"))
    # l'unité réservée pour elle-même garde « Booked by »
    own = pane.locator('tr[data-unit="alpha|n1-00003b000"] .bkg-badge')
    expect(own).to_have_count(1)
    expect(own).to_have_attribute("data-bkg-badge", "unit")
    expect(own).to_contain_text("Booked by carol until")
