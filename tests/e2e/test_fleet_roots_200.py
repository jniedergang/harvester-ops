"""2.0.0, tâche 9 : les entrées racines de flotte et les pages de gestion.

Chaque racine chargée à froid montre sa section (Conteneurs = toutes les
charges, Migrations = la vue globale Forklift...) ; l'en-tête « Cluster de
gestion » de Cluster API et Terraform montre le cluster que visent les
appels (requêtes interceptées), le changer ne change pas l'hôte ouvert, et
Bare-metal n'a pas cet en-tête ; `#activity/host/kvm/<h>` filtre l'Activité
sur les actions de cet hôte KVM (`kvm:<h>`)."""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto, settle  # noqa: E402
from test_cluster_switch import two_clusters  # noqa: E402,F401  (fixture)

A, B = "harv-fake", "harv-second"

# (adresse, section active, entrée du menu active [racine, onglet], sélecteur visible)
ROOTS = [
    ("#containers", "allcontainers", ("containers", None), "#all-containers-host"),
    ("#migrations", "forkliftglobal", ("migrations", None), "#forklift-global-host"),
    ("#gpus", "gpus", ("gpus", None), "#gpus-host"),
    ("#bookings", "bookings", ("bookings", None), "#bookings-host"),
    ("#usage", "usage", ("usage", None), "#usage-host"),
    ("#activity", "activity", ("activity", None), "#activity-running"),
    ("#dr", "drplans", ("dr", None), "#rp-plans-host"),
    ("#dr/links", "drlinks", ("dr", "links"), "#rp-links-host"),
    ("#automation/capi", "automation", ("automation", "capi"), '#tab-automation [data-subtab="capi"].active'),
    ("#automation/terraform", "automation", ("automation", "terraform"), '#tab-automation [data-subtab="terraform"].active'),
    ("#automation/pxe", "automation", ("automation", "pxe"), '#tab-automation [data-subtab="pxe"].active'),
    ("#hosts", "hosts", ("hosts", None), "#hosts-index-host .hix-card"),
    ("#vms", "allvms", ("vms", None), "#all-vms-host"),
]


def cold(context, flask_server, hash, routes=None):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            f"localStorage.setItem('harvester_ops_current_cluster','{A}');"
                            "localStorage.setItem('harvester_ops_sidebar_pinned','1');")
    page = context.new_page()
    for pat, fn in (routes or {}).items():
        page.route(pat, fn)
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors
    goto(page, flask_server["base_url"], hash)
    return page, calls


@pytest.mark.parametrize("hash,section,entry,selector", ROOTS, ids=[r[0] for r in ROOTS])
def test_each_root_cold(context, flask_server, hash, section, entry, selector):
    page, _ = cold(context, flask_server, hash)
    expect(page.locator(f"#tab-{section}")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(selector).first).to_be_attached()
    root, tab = entry
    sel = f'#host-nav [data-hnav-root="{root}"]' + (f'[data-hnav-tab="{tab}"]' if tab else ':not([data-hnav-tab])')
    expect(page.locator(sel)).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#host-page-head")).to_be_hidden()          # aucun hôte courant
    assert page.evaluate("location.hash") == hash
    assert page.errors == []


def test_bare_metal_has_no_management_cluster_header(context, flask_server):
    page, _ = cold(context, flask_server, "#automation/pxe")
    expect(page.locator("#mgmt-cluster-bar")).to_be_hidden()
    page.evaluate("Router.go('#automation/capi')")
    settle(page)
    expect(page.locator("#mgmt-cluster-bar")).to_be_visible()
    page.evaluate("Router.go('#automation/terraform')")
    settle(page)
    expect(page.locator("#mgmt-cluster-bar")).to_be_visible()


def test_the_management_cluster_shown_is_the_one_capi_calls(context, flask_server, two_clusters):
    page, calls = cold(context, flask_server, f"#harvester/{A}/params/storage")
    page.evaluate("Router.go('#automation/capi')")
    settle(page)
    pick = page.locator("#mgmt-cluster-pick")
    expect(pick).to_have_value(A)
    assert sorted(pick.locator("option").evaluate_all("els => els.map(e => e.value)")) == sorted([A, B])
    assert pick.get_attribute("title")
    page.wait_for_function(f"() => performance.getEntriesByType('resource').some(e => e.name.includes('/api/capi/{A}/diag'))")
    calls.clear()
    pick.select_option(B)
    page.wait_for_function(f"() => performance.getEntriesByType('resource').some(e => e.name.includes('/api/capi/{B}/diag'))")
    capi = [c for c in calls if "/api/capi/" in c]
    assert capi and all(f"/api/capi/{B}/" in c for c in capi), capi
    expect(pick).to_have_value(B)
    # l'hôte ouvert ne change pas
    assert page.evaluate("App.getCurrentCluster()") == A
    page.evaluate("history.back()")
    page.wait_for_function("() => location.hash.startsWith('#harvester/')")
    settle(page)
    assert page.evaluate("location.hash").startswith(f"#harvester/{A}/params/storage")
    assert page.evaluate("App.getCurrentCluster()") == A
    # Terraform lit le même cluster de gestion
    calls.clear()
    page.evaluate("Router.go('#automation/terraform')")
    settle(page)
    expect(page.locator("#mgmt-cluster-pick")).to_have_value(B)


def test_activity_filtered_on_a_kvm_host(context, flask_server):
    decl = {"hosts": [{"name": "node2", "address": "172.16.1.12", "port": 22, "user": "ju",
                       "uri": "qemu:///system", "labels": {}}], "warnings": [], "role": "admin", "gestures": {}}
    page, calls = cold(context, flask_server, "#activity/host/kvm/node2", routes={
        "**/api/kvm/hosts?declared=1": lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                              body=json.dumps(decl))})
    expect(page.locator("#tab-activity")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#act-f-cluster")).to_have_value("kvm:node2")
    assert any(re.search(r"/api/activity\?.*cluster=kvm%3Anode2", c) for c in calls), [c for c in calls if "activity" in c]
    assert page.evaluate("location.hash") == "#activity/host/kvm/node2"
    # F5 rend le même écran, filtre compris
    page.reload()
    settle(page)
    expect(page.locator("#act-f-cluster")).to_have_value("kvm:node2")


def test_activity_of_a_harvester_host_and_the_filter_rewrites_the_address(context, flask_server):
    page, calls = cold(context, flask_server, f"#activity/host/harvester/{A}")
    expect(page.locator("#act-f-cluster")).to_have_value(A)
    assert any(re.search(rf"/api/activity\?.*cluster={A}", c) for c in calls)
    # retirer le filtre : l'adresse redevient #activity
    page.click("#btn-act-filter-reset")
    page.wait_for_function("() => location.hash === '#activity'")
