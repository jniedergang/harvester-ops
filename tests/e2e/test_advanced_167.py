"""v1.67.0 : la section Advanced dans un navigateur : l'onglet Settings (groupes,
états, fenêtres typées, avertissement des réglages dangereux, secrets masqués,
cible de sauvegarde, remise au défaut, test) et l'onglet Support (paquets de
support, kubeconfigs téléchargés une fois, révocation)."""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

SETTINGS = {"cluster": "harv-fake", "groups": ["general"], "items": [
    {"name": "log-level", "group": "general", "kind": "enum", "choices": ["info", "debug", "trace"], "value": "debug",
     "default": "info", "modified": True, "applied": "ok", "message": "", "danger": "", "readonly": False, "elsewhere": "",
     "inert": False, "min": None, "max": None},
    {"name": "max-hotplug-ratio", "group": "performance", "kind": "number", "choices": [], "value": "", "default": "4",
     "modified": False, "applied": "", "message": "", "danger": "", "readonly": False, "elsewhere": "", "inert": False,
     "min": 1, "max": 20},
    {"name": "ssl-certificates", "group": "security", "kind": "json", "choices": [],
     "value": '{"ca":"C","publicCertificate":"P","privateKey":"•••"}', "default": "{}", "modified": True, "applied": "error",
     "message": "bad chain", "danger": "tls", "readonly": False, "elsewhere": "", "inert": False, "min": None, "max": None},
    {"name": "backup-target", "group": "backup", "kind": "backup", "choices": [],
     "value": '{"type":"nfs","endpoint":"nfs://172.16.0.5:/volume1/BACKUP/lab/"}', "default": "", "modified": True,
     "applied": "ok", "message": "", "danger": "", "readonly": False, "elsewhere": "", "inert": False, "min": None, "max": None},
    {"name": "storage-network", "group": "network", "kind": "json", "choices": [], "value": "", "default": "",
     "modified": False, "applied": "", "message": "", "danger": "", "readonly": False, "elsewhere": "network", "inert": False,
     "min": None, "max": None},
    {"name": "server-version", "group": "upgrade", "kind": "text", "choices": [], "value": "v1.9.0", "default": "",
     "modified": True, "applied": "", "message": "", "danger": "", "readonly": True, "elsewhere": "", "inert": False,
     "min": None, "max": None}]}
SUPPORT = {"cluster": "harv-fake",
           "bundles": [{"name": "bundle-a", "description": "slow VM", "state": "ready", "progress": 100,
                        "filename": "supportbundle_a.zip", "filesize": 2048000, "created": "2026-09-27T10:00:00Z"},
                       {"name": "bundle-b", "description": "disk", "state": "generating", "progress": 40,
                        "filename": "", "filesize": 0, "created": "2026-09-27T11:00:00Z"}],
           "kubeconfigs": [{"name": "ci", "role": "view", "scope": "default", "expires": "2026-09-27T18:00:00Z",
                            "expired": False, "description": "", "created": None}],
           "roles": [{"name": "view", "exists": True, "reads_secrets": True},
                     {"name": "harvesterhci.io:edit", "exists": True, "reads_secrets": False},
                     {"name": "cluster-admin", "exists": True, "reads_secrets": True},
                     {"name": "edit", "exists": False, "reads_secrets": True}],
           "namespaces": ["default", "prod"]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','namespaces');")
    page = context.new_page()
    sent = []

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        body = {"action_id": "adv000000167"}
        if req.url.endswith("/kc-create"):
            body["download"] = "a" * 32
        if req.url.endswith("/bundle-create"):
            body["name"] = "bundle-new"
        fulfill(route, body, 202)
    page.route("**/api/hv-settings/harv-fake", lambda r, q: fulfill(r, SETTINGS))
    page.route("**/api/hv-support/harv-fake", lambda r, q: fulfill(r, SUPPORT))
    page.route("**/api/hv-settings/harv-fake/do/**", writes)
    page.route("**/api/hv-support/harv-fake/do/**", writes)
    page.route("**/api/hv-support/harv-fake/kubeconfig/**", lambda r, q: r.fulfill(
        status=200, content_type="application/yaml", body="kind: Config\n"))
    page.route("**/api/stream/adv000000167", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.Advanced && window.Sections && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    return page, sent


def settings_tab(page):
    page.evaluate("Sections.open('advanced', 'settings')")
    body = page.locator('#tab-advanced .section-pane[data-pane="settings"] [data-adv="body"]')
    expect(body.locator("tr[data-setting]")).to_have_count(6, timeout=8000)
    return body


def test_the_advanced_section_sits_under_cluster(ui):
    page, _ = ui
    link = page.locator('.tab[data-tab="advanced"]')
    assert link.count() == 1 and link.get_attribute("data-i18n-title") == "tab.advancedTip"
    assert page.locator("#tab-advanced [data-section-tab]").evaluate_all("els => els.map(e => e.dataset.sectionTab)") \
        == ["settings", "pci", "usb", "gpu", "sriov", "support"]            # v1.68.0 : les périphériques


def test_settings_show_their_state_and_filter_by_group(ui):
    page, _ = ui
    body = settings_tab(page)
    ssl = body.locator('tr[data-setting="ssl-certificates"]')
    expect(ssl.locator(".badge.fail")).to_have_count(1)                   # le refus de Harvester, dit
    expect(ssl.locator(".badge.warn")).to_have_count(1)                   # réglage qui peut couper l'accès
    expect(body.locator('tr[data-setting="server-version"] [data-adv="edit"]')).to_have_count(0)
    expect(body.locator('tr[data-setting="storage-network"] [data-adv="elsewhere"]')).to_have_count(1)
    expect(body.locator('tr[data-setting="max-hotplug-ratio"] [data-adv="reset"]')).to_be_disabled()
    page.locator('#tab-advanced [data-adv="group"][data-group="performance"]').click()
    expect(body.locator("tr[data-setting]")).to_have_count(1)
    page.locator('#tab-advanced [data-adv="group"][data-group=""]').click()
    page.locator('#tab-advanced [data-adv="modified"]').check()
    expect(body.locator("tr[data-setting]")).to_have_count(4)


def test_an_enum_setting_is_changed_and_reset(ui):
    page, sent = ui
    body = settings_tab(page)
    body.locator('tr[data-setting="log-level"] [data-adv="edit"]').click()
    w = page.locator("#fp-adv-set-harv-fake-log-level")
    w.locator('select[name="value"]').select_option("trace")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/hv-settings/harv-fake/do/set", {"name": "log-level", "value": "trace"})
    body.locator('tr[data-setting="log-level"] [data-adv="reset"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/hv-settings/harv-fake/do/reset", {"name": "log-level"})


def test_a_dangerous_setting_needs_the_box_and_keeps_its_masked_secret(ui):
    page, sent = ui
    body = settings_tab(page)
    body.locator('tr[data-setting="ssl-certificates"] [data-adv="edit"]').click()
    w = page.locator("#fp-adv-set-harv-fake-ssl-certificates")
    expect(w.locator(".adv-danger")).to_be_visible()
    text = w.locator('textarea[name="value"]').input_value()
    assert '"privateKey": "•••"' in text                                  # jamais la clé
    w.locator('textarea[name="value"]').fill(text.replace('"C"', '"C2"'))
    w.locator('button[type="submit"]').click()
    expect(w.locator(".of-msg .res-error")).to_be_visible()               # la case d'abord
    assert not sent
    w.locator('[name="understand"]').check()
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, b = sent[-1]
    assert path == "api/hv-settings/harv-fake/do/set" and json.loads(b["value"]) == {
        "ca": "C2", "publicCertificate": "P", "privateKey": "•••"}


def test_the_backup_target_form_and_its_test(ui):
    page, sent = ui
    body = settings_tab(page)
    row = body.locator('tr[data-setting="backup-target"]')
    row.locator('[data-adv="test"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/hv-settings/harv-fake/do/test", {})
    row.locator('[data-adv="edit"]').click()
    w = page.locator("#fp-adv-bt-harv-fake")
    expect(w.locator('[name="endpoint"]')).to_have_value("172.16.0.5:/volume1/BACKUP/lab/")
    expect(w.locator('[data-f="bucketName"]')).to_be_hidden()
    w.locator('[name="type"]').select_option("s3")
    w.locator('[name="endpoint"]').fill("https://s3.lan:9000")
    w.locator('[name="bucketName"]').fill("vms")
    w.locator('[name="bucketRegion"]').fill("eu")
    w.locator('button[type="submit"]').click()
    expect(w.locator(".of-msg .res-error")).to_be_visible()               # les deux clés sont exigées
    w.locator('[name="accessKeyId"]').fill("k")
    w.locator('[name="secretAccessKey"]').fill("s")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, b = sent[-1]
    assert path == "api/hv-settings/harv-fake/do/set" and json.loads(b["value"]) == {
        "type": "s3", "endpoint": "https://s3.lan:9000", "refreshIntervalInSeconds": 0, "bucketName": "vms",
        "bucketRegion": "eu", "accessKeyId": "k", "secretAccessKey": "s", "virtualHostedStyle": False}


def test_support_bundles_and_kubeconfigs(ui):
    page, sent = ui
    page.evaluate("Sections.open('advanced', 'support')")
    body = page.locator('#tab-advanced .section-pane[data-pane="support"] [data-adv="body"]')
    expect(body.locator("tr[data-bundle]")).to_have_count(2, timeout=8000)
    expect(body.locator('tr[data-bundle="bundle-a"] [data-adv="download-bundle"]')).to_have_attribute(
        "href", "/api/hv-support/harv-fake/bundle/bundle-a/download")
    expect(body.locator('tr[data-bundle="bundle-b"] [data-adv="download-bundle"]')).to_have_count(0)
    body.locator('[data-adv="new-bundle"]').click()
    w = page.locator("#fp-adv-sb-harv-fake")
    w.locator('[name="description"]').fill("VM slow to boot")
    w.locator('[name="ns"][value="prod"]').check()
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/hv-support/harv-fake/do/bundle-create", {"spec": {
        "description": "VM slow to boot", "issue_url": "", "namespaces": ["prod"], "timeout": "", "expiration": "",
        "node_timeout": ""}})
    w.locator('[data-action="close"]').click()
    body.locator('[data-adv="new-kc"]').click()
    w = page.locator("#fp-adv-kc-harv-fake")
    expect(w.locator('[name="role"] option')).to_have_count(3)            # edit n'existe pas sur ce cluster
    expect(w.locator(".adv-secrets")).to_be_visible()                     # view lit les secrets sur Harvester
    w.locator('[name="role"]').select_option("harvesterhci.io:edit")
    expect(w.locator(".adv-secrets")).to_be_hidden()
    w.locator('[name="name"]').fill("ci2")
    w.locator('[name="namespace"]').select_option("prod")
    w.locator('[name="duration"]').select_option("8h")
    with page.expect_download() as dl:
        w.locator('button[type="submit"]').click()
    assert dl.value.suggested_filename == "harv-fake-kubeconfig.yaml"
    assert sent[-1] == ("api/hv-support/harv-fake/do/kc-create", {"name": "ci2", "description": "", "role": "harvesterhci.io:edit",
                                                                  "namespace": "prod", "duration": "8h"})
    body.locator('tr[data-kc="ci"] [data-adv="revoke-kc"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/hv-support/harv-fake/do/kc-revoke", {"name": "ci"})
