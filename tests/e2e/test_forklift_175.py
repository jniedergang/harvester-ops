"""v1.75.0 : l'onglet Migrations VMware dans un navigateur : la Préparation
en trois étapes avec leur état, l'installation et la poussée de l'image
VDDK envoyées sans secret visible, le dépôt d'une archive."""

import json
import time

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto  # noqa: E402

ARCHIVE = "VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz"
READY = {"ready": True, "cert_manager": True, "cert_manager_missing": [], "addon": "ready",
         "addon_message": "the forklift-operator add-on is deployed", "operator": True, "controller": True,
         "components_missing": [], "running": True, "inventory_access": True}
DATA = {"cluster": "harv-fake", "install": READY, "harvester_addon": False, "bundle": True,
        "vddk": {"image": "172.16.1.11:5005/harvops/vddk:8.0.3", "digest": "sha256:" + "4f1c" * 16,
                 "archive": ARCHIVE, "pushed_at": "2026-09-28T20:00:00Z"},
        "registry": {"image": "172.16.1.11:5005/harvops/vddk:8.0.3", "host": "172.16.1.11:5005",
                     "plain_http": True, "auth": True},
        "providers": [{"name": "vmwlab", "namespace": "forklift", "url": "https://vmwlab-vc.home.lo/sdk", "ready": True,
                       "message": "ready: vCenter reached, inventory loaded",
                       "vddk_image": "172.16.1.11:5005/harvops/vddk:8.0.3", "plans": [], "managed": True}],
        "vmimport_sources": [{"namespace": "mig", "name": "vc", "endpoint": "https://vmwlab-vc.home.lo/sdk"}]}
ABSENT = {**DATA, "install": {**READY, "ready": False, "addon": "absent", "addon_message": "the forklift-operator add-on is not declared",
                              "operator": False, "controller": False, "components_missing": ["forklift-api"],
                              "running": False, "inventory_access": False},
          "vddk": None, "providers": []}
STORE = {"archives": [{"name": ARCHIVE, "version": "8.0.3", "size": 41626848, "mtime": 1790000000}], "free": 10 ** 11}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def open_tab(context, flask_server, data):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_section_forklift','prep');")
    page = context.new_page()
    sent = []

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.method, req.post_data_json if req.method == "POST" else None))
        fulfill(route, {"action_id": "fk0000000175"}, 202)
    # `data` : l'état lu, ou une fonction qui le rend (il change entre deux lectures)
    page.route("**/api/forklift/harv-fake", lambda r, q: fulfill(r, data() if callable(data) else data))
    page.route("**/api/forklift/harv-fake/do/**", writes)
    page.route("**/api/forklift-vddk", lambda r, q: fulfill(r, STORE))
    page.route("**/api/stream/fk0000000175", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    # 2.0.0 : Imports et migrations > VMware de l'hôte, par son adresse
    goto(page, flask_server["base_url"], address("forklift", sub="prep"))
    page.wait_for_function("window.Forklift && window.Sections && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    return page, sent


def test_preparation_shows_three_steps_with_their_state(context, flask_server):
    # v1.76.0 : la Préparation gagne l'étape CDI (entre Forklift et VDDK) et
    # l'intervalle des copies (après les sources) : 4 [data-fk-step], pas 3.
    page, _ = open_tab(context, flask_server, DATA)
    steps = page.locator("#tab-forklift [data-fk-step]")
    expect(steps).to_have_count(4)
    expect(steps.nth(0)).to_contain_text("ready")
    expect(steps.nth(2)).to_contain_text("172.16.1.11:5005/harvops/vddk:8.0.3")
    expect(steps.nth(3)).to_contain_text("1")
    for b in page.locator("#tab-forklift button:visible").all():
        assert b.get_attribute("data-tip") or b.get_attribute("title"), b.inner_text()


def test_install_is_offered_when_forklift_is_absent_and_sent_as_an_action(context, flask_server):
    page, sent = open_tab(context, flask_server, ABSENT)
    page.locator('#tab-forklift [data-fk="install"]').click()
    page.wait_for_timeout(500)
    assert sent[0][0] == "api/forklift/harv-fake/do/install"


def test_the_vddk_image_is_pushed_with_harvester_s_registry_credentials(context, flask_server):
    page, sent = open_tab(context, flask_server, DATA)
    form = page.locator('#tab-forklift [data-fk-step="vddk"]')
    expect(form.locator('[name="archive"]')).to_have_value(ARCHIVE)
    expect(form.locator('[name="image"]')).to_have_value("172.16.1.11:5005/harvops/vddk:8.0.3")
    expect(form.locator('[name="use_cluster_auth"]')).to_be_checked()
    form.locator('[data-fk="push-vddk"]').click()
    page.wait_for_timeout(500)
    url, method, body = sent[0]
    assert url == "api/forklift/harv-fake/do/vddk-image"
    assert body == {"archive": ARCHIVE, "image": "172.16.1.11:5005/harvops/vddk:8.0.3", "plain_http": True,
                    "use_cluster_auth": True}


VMS = json.load(open(__import__("pathlib").Path(__file__).resolve().parents[1] / "api" / "fixtures" / "forklift_inventory_vms_175.json"))


def rows_of(vms):
    """Les lignes que rend l'outil (hf.inventory_rows), depuis le relevé réel."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin" / "lib"))
    import hv_forklift as hf
    return hf.inventory_rows("vms", vms)


def test_a_source_block_says_its_state_and_offers_inventory_edit_delete(context, flask_server):
    page, sent = open_tab(context, flask_server, DATA)
    page.evaluate("Sections.open('forklift', 'sources')")
    block = page.locator('#tab-forklift [data-fk-source="vmwlab"]')
    expect(block).to_contain_text("https://vmwlab-vc.home.lo/sdk")
    expect(block.locator('[data-fk="del-source"]')).to_be_enabled()
    block.locator('[data-fk="del-source"]').click()
    page.wait_for_timeout(400)
    assert sent[-1][0] == "api/forklift/harv-fake/do/provider-delete"
    assert sent[-1][2] == {"name": "vmwlab", "namespace": "forklift"}


def test_a_source_used_by_a_plan_cannot_be_deleted(context, flask_server):
    used = {**DATA, "providers": [{**DATA["providers"][0], "plans": ["forklift/wave-1"]}]}
    page, _ = open_tab(context, flask_server, used)
    page.evaluate("Sections.open('forklift', 'sources')")
    expect(page.locator('#tab-forklift [data-fk-source="vmwlab"] [data-fk="del-source"]')).to_be_disabled()


def test_a_vcenter_of_vm_import_is_taken_without_retyping_the_password(context, flask_server):
    page, sent = open_tab(context, flask_server, DATA)
    page.evaluate("Sections.open('forklift', 'sources')")
    page.locator('#tab-forklift [data-fk="new-source"]').click()
    form = page.locator(".floating-panel .of-form").last
    form.locator('[name="from"]').select_option("mig/vc")
    expect(form.locator('[name="password"]')).to_be_hidden()
    form.locator('[name="name"]').fill("vmwlab2")
    form.locator('button[type="submit"]').click()
    page.wait_for_timeout(400)
    url, _, body = sent[-1]
    assert url == "api/forklift/harv-fake/do/provider-apply"
    assert body == {"spec": {"name": "vmwlab2", "from_vmimport": {"namespace": "mig", "name": "vc"},
                             "vddk_image": "172.16.1.11:5005/harvops/vddk:8.0.3"}}


def test_a_typed_vcenter_goes_with_its_certificate_choice(context, flask_server):
    page, sent = open_tab(context, flask_server, DATA)
    page.evaluate("Sections.open('forklift', 'sources')")
    page.locator('#tab-forklift [data-fk="new-source"]').click()
    form = page.locator(".floating-panel .of-form").last
    form.locator('[name="name"]').fill("vc2")
    form.locator('[name="url"]').fill("vc2.lan")
    form.locator('[name="user"]').fill("administrator@vsphere.local")
    form.locator('[name="password"]').fill("pw")
    form.locator('[name="tls"][value="insecure"]').check()
    form.locator('button[type="submit"]').click()
    page.wait_for_timeout(400)
    assert sent[-1][2] == {"spec": {"name": "vc2", "url": "vc2.lan", "user": "administrator@vsphere.local", "password": "pw",
                                    "insecure": True, "vddk_image": "172.16.1.11:5005/harvops/vddk:8.0.3"}}


def test_editing_a_source_keeps_its_password_unless_retyped(context, flask_server):
    page, sent = open_tab(context, flask_server, DATA)
    page.evaluate("Sections.open('forklift', 'sources')")
    page.locator('#tab-forklift [data-fk-source="vmwlab"] [data-fk="edit-source"]').click()
    form = page.locator(".floating-panel .of-form").last
    expect(form.locator('[name="name"]')).to_have_attribute("readonly", "")
    form.locator('button[type="submit"]').click()
    page.wait_for_timeout(400)
    spec = sent[-1][2]["spec"]
    assert spec["keep_credentials"] is True and spec["password"] == "" and spec["name"] == "vmwlab"
    assert "insecure" not in spec and "cacert" not in spec   # « garder le réglage actuel » : le serveur reprend le TLS du secret


def test_the_inventory_shows_warm_capability_and_forklift_s_concerns(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA)
    page.route("**/api/forklift/harv-fake/inventory/vmwlab/vms*", lambda r, q: fulfill(r, {"rows": rows_of(VMS)}))
    page.evaluate("Forklift.openInventory('vmwlab')")
    table = page.locator('#tab-forklift [data-fk="inv-table"]')
    expect(table.locator("tbody tr")).to_have_count(4)
    vc = table.locator('tr[data-vm="vmwlab-vc"]')
    expect(vc).to_contain_text("CBT")
    expect(vc.locator('[data-fk-warm="no"]')).to_have_count(1)
    expect(table.locator('tr[data-vm="vmwlab-src-1"] [data-fk-warm="yes"]')).to_have_count(1)
    page.locator('#tab-forklift [name="warm_only"]').check()
    expect(table.locator("tbody tr")).to_have_count(3)
    page.locator('#tab-forklift [name="q"]').fill("src-3")
    expect(table.locator("tbody tr")).to_have_count(1)


def test_an_inventory_error_is_said(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA)
    page.route("**/api/forklift/harv-fake/inventory/vmwlab/vms*",
               lambda r, q: fulfill(r, {"error": "the inventory service answered 503"}, 502))
    page.evaluate("Forklift.openInventory('vmwlab')")
    expect(page.locator("#tab-forklift .res-error")).to_contain_text("503")


# --- fix : un fournisseur peut vivre hors du namespace forklift -------------

def test_a_provider_outside_forklift_is_read_with_its_own_namespace(context, flask_server):
    """Vu en réel : un fournisseur fait par la CLI, dans `default`. L'onglet
    doit lire son inventaire dans SON namespace, pas dans `forklift`."""
    other = {**DATA, "providers": [{**DATA["providers"][0], "namespace": "default"}]}
    page, _ = open_tab(context, flask_server, other)
    urls = []

    def rec(route, req):
        urls.append(route.request.url)
        fulfill(route, {"rows": []})
    page.route("**/api/forklift/harv-fake/inventory/vmwlab/vms*", rec)
    page.evaluate("Sections.open('forklift', 'sources')")
    page.locator('#tab-forklift [data-fk-source="vmwlab"] [data-fk="inv-source"]').click()
    page.wait_for_timeout(300)
    assert urls and "namespace=default" in urls[-1]


# --- fix : une réponse d'inventaire plus lente ne doit jamais écraser -------
# celle d'une source choisie ensuite (jeton de requête).

def test_switching_source_ignores_a_slower_stale_answer(context, flask_server):
    two = {**DATA, "providers": [DATA["providers"][0], {**DATA["providers"][0], "name": "vmwlab2"}]}
    page, _ = open_tab(context, flask_server, two)

    def slow(route, req):
        time.sleep(0.6)
        fulfill(route, {"rows": [{"name": "old-vm"}]})
    page.route("**/api/forklift/harv-fake/inventory/vmwlab/vms*", slow)
    page.route("**/api/forklift/harv-fake/inventory/vmwlab2/vms*", lambda r, q: fulfill(r, {"rows": [{"name": "new-vm"}]}))
    page.evaluate("Forklift.openInventory('vmwlab')")
    page.wait_for_timeout(100)
    page.locator('#tab-forklift [name="source"]').select_option("forklift/vmwlab2")
    table = page.locator('#tab-forklift [data-fk="inv-table"]')
    expect(table).to_contain_text("new-vm", timeout=2000)
    page.wait_for_timeout(700)                       # laisse la réponse lente arriver
    expect(table).to_contain_text("new-vm")
    expect(table).not_to_contain_text("old-vm")


# --- fix : « Ajouter un vCenter » avant que Forklift soit prêt -------------

def test_the_new_source_button_is_disabled_until_forklift_is_ready(context, flask_server):
    page, _ = open_tab(context, flask_server, ABSENT)
    page.evaluate("Sections.open('forklift', 'sources')")
    btn = page.locator('#tab-forklift [data-fk="new-source"]')
    expect(btn).to_be_disabled()
    assert "not ready" in (btn.get_attribute("data-tip") or "")


def test_the_new_source_button_is_enabled_once_forklift_is_ready(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA)
    page.evaluate("Sections.open('forklift', 'sources')")
    btn = page.locator('#tab-forklift [data-fk="new-source"]')
    expect(btn).to_be_enabled()


# --- relecture finale ---------------------------------------------------------

def test_a_background_refresh_keeps_the_vddk_form_being_typed(context, flask_server):
    """La relecture de fond (10 s) redessinait toute la Préparation : utilisateur
    et mot de passe du registre, image modifiée, choix de l'archive et case des
    identifiants disparaissaient sous les doigts."""
    state = {"data": DATA}
    page, _ = open_tab(context, flask_server, lambda: state["data"])
    form = page.locator('#tab-forklift [data-fk-step="vddk"]')
    expect(form.locator('[name="image"]')).to_have_value("172.16.1.11:5005/harvops/vddk:8.0.3")
    form.locator('[name="image"]').fill("reg.lan/harvops/vddk:9.9.9")
    form.locator('[name="use_cluster_auth"]').uncheck()
    form.locator('[name="username"]').fill("pusher")
    form.locator('[name="password"]').fill("pw-typed")
    form.locator('[name="plain_http"]').uncheck()
    # entre-temps, Forklift a disparu du cluster : l'étape 1 doit le montrer
    state["data"] = ABSENT
    page.evaluate("Forklift.backgroundRefresh()")
    expect(page.locator('#tab-forklift [data-fk="install"]')).to_be_visible()
    expect(form.locator('[name="image"]')).to_have_value("reg.lan/harvops/vddk:9.9.9")
    expect(form.locator('[name="username"]')).to_have_value("pusher")
    expect(form.locator('[name="password"]')).to_have_value("pw-typed")
    expect(form.locator('[name="use_cluster_auth"]')).not_to_be_checked()
    expect(form.locator('[name="plain_http"]')).not_to_be_checked()
    expect(form.locator('[data-fk="reg-creds"]')).to_be_visible()
    # un clic sur Actualiser, lui, relit tout
    state["data"] = DATA
    page.locator('#tab-forklift [data-fk="refresh"]').click()
    expect(form.locator('[name="image"]')).to_have_value("172.16.1.11:5005/harvops/vddk:8.0.3")
    expect(form.locator('[name="username"]')).to_have_value("")


def test_the_form_is_read_again_once_the_push_is_sent(context, flask_server):
    page, sent = open_tab(context, flask_server, DATA)
    form = page.locator('#tab-forklift [data-fk-step="vddk"]')
    form.locator('[name="image"]').fill("reg.lan/harvops/vddk:9.9.9")
    form.locator('[data-fk="push-vddk"]').click()
    page.wait_for_timeout(300)
    assert sent[-1][2]["image"] == "reg.lan/harvops/vddk:9.9.9"
    page.evaluate("Forklift.backgroundRefresh()")
    expect(form.locator('[name="image"]')).to_have_value("172.16.1.11:5005/harvops/vddk:8.0.3")


def test_an_upload_in_progress_survives_a_background_refresh(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA)
    pending = []
    page.route(f"**/api/forklift-vddk/{ARCHIVE}", lambda r, q: pending.append(r))
    form = page.locator('#tab-forklift [data-fk-step="vddk"]')
    form.locator('[data-fk="upload-file"]').set_input_files(
        {"name": ARCHIVE, "mimeType": "application/gzip", "buffer": b"x" * 2048})
    line = form.locator('[data-fk="upload-line"]')
    expect(line).not_to_be_empty()
    page.evaluate("Forklift.backgroundRefresh()")
    expect(line).not_to_be_empty()
    page.wait_for_timeout(200)
    assert pending, "the upload was not sent"
    pending[0].fulfill(status=201, content_type="application/json",
                       body=json.dumps({"action_id": "fk0000000175", "archive": ARCHIVE, "size": 2048}))
    # fin de l'envoi : la Préparation est relue, le résultat reste dit
    expect(line).to_contain_text("kept by the console")
    page.evaluate("Forklift.backgroundRefresh()")
    expect(line).to_contain_text("kept by the console")


def test_a_completed_upload_only_refreshes_the_archive_choice(context, flask_server):
    """La fin d'un envoi ne doit jamais effacer l'image ou l'utilisateur du
    registre en cours de saisie : seuls les choix de l'archive changent."""
    new_archive = "VMware-vix-disklib-8.0.4-23960000.x86_64.tar.gz"
    page, _ = open_tab(context, flask_server, DATA)
    form = page.locator('#tab-forklift [data-fk-step="vddk"]')
    form.locator('[name="image"]').fill("reg.lan/harvops/vddk:9.9.9")
    form.locator('[name="use_cluster_auth"]').uncheck()
    form.locator('[name="username"]').fill("pusher")
    updated_store = {"archives": STORE["archives"] + [
        {"name": new_archive, "version": "8.0.4", "size": 4096, "mtime": 1790000100}], "free": 10 ** 11}
    page.route("**/api/forklift-vddk", lambda r, q: fulfill(r, updated_store))
    pending = []
    page.route(f"**/api/forklift-vddk/{new_archive}", lambda r, q: pending.append(r))
    form.locator('[data-fk="upload-file"]').set_input_files(
        {"name": new_archive, "mimeType": "application/gzip", "buffer": b"x" * 2048})
    page.wait_for_timeout(200)
    assert pending, "the upload was not sent"
    pending[0].fulfill(status=201, content_type="application/json",
                       body=json.dumps({"action_id": "fk0000000175", "archive": new_archive, "size": 2048}))
    line = form.locator('[data-fk="upload-line"]')
    expect(line).to_contain_text("kept by the console")
    expect(form.locator('[name="archive"]')).to_have_value(new_archive)
    expect(form.locator('[name="image"]')).to_have_value("reg.lan/harvops/vddk:9.9.9")
    expect(form.locator('[name="username"]')).to_have_value("pusher")
    expect(form.locator('[name="use_cluster_auth"]')).not_to_be_checked()


def test_a_failed_background_read_does_not_wipe_the_form(context, flask_server):
    """Un aléa de la relecture de fond (10 s), pendant une saisie en cours,
    ne doit jamais remplacer le formulaire par un message d'erreur."""
    state = {"ok": True}

    def read(route, req):
        if state["ok"]:
            fulfill(route, DATA)
        else:
            route.fulfill(status=500, content_type="application/json", body="{}")
    page, _ = open_tab(context, flask_server, DATA)
    page.route("**/api/forklift/harv-fake", read)
    form = page.locator('#tab-forklift [data-fk-step="vddk"]')
    form.locator('[name="image"]').fill("reg.lan/harvops/vddk:9.9.9")
    state["ok"] = False
    page.evaluate("Forklift.backgroundRefresh()")
    page.wait_for_timeout(300)
    expect(form.locator('[name="image"]')).to_have_value("reg.lan/harvops/vddk:9.9.9")
    expect(page.locator('#tab-forklift .sto-finding')).to_have_count(0)


def test_reopening_a_source_window_does_not_double_submit(context, flask_server):
    """FloatingPanels.open rend le même panneau tant qu'il n'a pas été fermé
    par la croix : reprendre l'édition ne doit jamais poser un second
    écouteur de soumission."""
    page, sent = open_tab(context, flask_server, DATA)
    page.evaluate("Sections.open('forklift', 'sources')")
    edit = page.locator('#tab-forklift [data-fk-source="vmwlab"] [data-fk="edit-source"]')
    edit.click()
    panel = page.locator(".floating-panel").last
    panel.locator('[data-action="min"]').click()
    edit.click()   # reprend la même fenêtre (encore dans la carte de FloatingPanels)
    form = page.locator(".floating-panel .of-form").last
    form.locator('button[type="submit"]').click()
    page.wait_for_timeout(400)
    posts = [s for s in sent if s[0] == "api/forklift/harv-fake/do/provider-apply"]
    assert len(posts) == 1, posts


def test_the_inventory_access_is_a_part_and_resume_is_offered_without_it(context, flask_server):
    no_sa = {**DATA, "install": {**READY, "ready": False, "running": True, "inventory_access": False}}
    page, sent = open_tab(context, flask_server, no_sa)
    one = page.locator('#tab-forklift [data-fk-step="forklift"]')
    part = one.locator('.fk-part', has_text="inventory access")
    expect(part).to_have_count(1)
    assert part.get_attribute("data-tip")
    expect(one).not_to_contain_text("ready")
    btn = one.locator('[data-fk="install"]')
    expect(btn).to_contain_text("Resume")
    btn.click()
    page.wait_for_timeout(300)
    assert sent[-1][0] == "api/forklift/harv-fake/do/install"


def test_a_source_window_submits_to_the_cluster_it_was_opened_for(context, flask_server):
    page, sent = open_tab(context, flask_server, DATA)
    page.evaluate("Sections.open('forklift', 'sources')")
    page.locator('#tab-forklift [data-fk="new-source"]').click()
    form = page.locator(".floating-panel .of-form").last
    form.locator('[name="name"]').fill("vc2")
    form.locator('[name="url"]').fill("vc2.lan")
    form.locator('[name="user"]').fill("administrator@vsphere.local")
    form.locator('[name="password"]').fill("pw")
    form.locator('[name="tls"][value="insecure"]').check()
    other = []
    page.route("**/api/forklift/harv-other", lambda r, q: fulfill(r, {**DATA, "cluster": "harv-other"}))
    page.route("**/api/forklift/harv-other/do/**",
               lambda r, q: (other.append(q.url), fulfill(r, {"action_id": "fk0000000175"}, 202)))
    # l'onglet passe à un autre cluster pendant que la fenêtre est ouverte
    page.evaluate("Forklift.start('harv-other', document.querySelector("
                  "'#tab-forklift .section-pane[data-pane=\"sources\"] .na-host'))")
    form.locator('button[type="submit"]').click()
    page.wait_for_timeout(400)
    assert not other, other
    assert sent[-1][0] == "api/forklift/harv-fake/do/provider-apply"


def test_a_new_source_with_a_taken_name_is_refused_in_the_window(context, flask_server):
    page, _ = open_tab(context, flask_server, DATA)
    page.route("**/api/forklift/harv-fake/do/provider-apply", lambda r, q: fulfill(
        r, {"error": "a provider named vmwlab already exists in forklift: pick another name, or change that source instead"}, 409))
    page.evaluate("Sections.open('forklift', 'sources')")
    page.locator('#tab-forklift [data-fk="new-source"]').click()
    form = page.locator(".floating-panel .of-form").last
    form.locator('[name="from"]').select_option("mig/vc")
    form.locator('[name="name"]').fill("vmwlab")
    form.locator('button[type="submit"]').click()
    expect(form.locator(".of-msg")).to_contain_text("already exists")
