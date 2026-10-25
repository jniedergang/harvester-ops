"""v1.64.0 : les fenêtres Modèles et Configurations cloud, le formulaire de
classe de stockage (moteur, chiffrement), les secrets typés et leurs nouvelles
valeurs, la clé SSH modifiable, dans un navigateur."""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import address, goto  # noqa: E402

TEMPLATES = {"items": [{"namespace": "default", "name": "web", "description": "web tier", "default_version": "default/web-2",
                        "versions": [{"name": "web-3", "namespace": "default", "ref": "default/web-3", "version": 3, "description": "v3",
                                      "ready": True, "default": False},
                                     {"name": "web-2", "namespace": "default", "ref": "default/web-2", "version": 2, "description": "v2",
                                      "ready": True, "default": True}]}]}
CLOUD = {"items": [{"namespace": "default", "name": "base", "type": "user", "text": "#cloud-config\npackages: [htop]\n",
                    "description": "base tools"}]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','fr');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    sent = []

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "adv000000164"}, 202)
    page.route("**/api/templates/harv-fake", lambda r, q: fulfill(r, TEMPLATES))
    page.route("**/api/templates/harv-fake/*/*/do/**", writes)
    page.route("**/api/cloud-templates/harv-fake", lambda r, q: writes(r, q) if q.method == "POST" else fulfill(r, CLOUD))
    page.route("**/api/cloud-templates/harv-fake/*/*/do/**", writes)
    page.route("**/api/storage-options/harv-fake", lambda r, q: fulfill(r, {"longhorn_v2": False, "lvm": False, "lvm_groups": [],
                                                                            "crypto_secrets": ["default/crypto"]}))
    page.route("**/api/storageclass/harv-fake", writes)
    page.route("**/api/secret/harv-fake/**", writes)
    page.route("**/api/sshkey/harv-fake/**", writes)
    page.route("**/api/namespaces/harv-fake", lambda r, q: fulfill(r, [{"name": "default"}]))
    page.route("**/api/stream/adv000000164", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address("namespaces"))
    page.wait_for_function("window.Templates && window.ObjectForms && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    return page, sent


def test_the_templates_window(ui):
    page, sent = ui
    page.evaluate("App.setTab('namespaces')")
    page.locator("#btn-vm-templates").click()
    w = page.locator(".floating-panel", has=page.locator(".tpl-win")).last
    expect(w.locator('tr[data-ver]')).to_have_count(2, timeout=8000)
    expect(w.locator('tr[data-ver="default/web-2"] [data-tpl-act="delete-version"]')).to_have_count(0)   # pas la version par défaut
    w.locator('tr[data-ver="default/web-3"] [data-tpl-act="set-default"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/templates/harv-fake/default/web/do/set-default", {"version": "default/web-3"})
    w.locator('tr[data-ver="default/web-3"] [data-tpl-act="delete-version"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/templates/harv-fake/default/web/do/delete-version", {"version": "default/web-3"})


def test_the_cloud_templates_window(ui):
    page, sent = ui
    page.evaluate("Templates.openCloud('harv-fake')")
    w = page.locator(".floating-panel", has=page.locator(".tpl-win")).last
    expect(w.locator('tr[data-ct-ref]')).to_have_count(1, timeout=8000)
    w.locator('[data-ct="new"]').click()
    f = w.locator('[data-ct="form"] form')
    f.locator('[name="name"]').fill("net1")
    f.locator('[name="type"]').select_option("network")
    f.locator('[name="text"]').fill("version: 2\nethernets:\n  eth0:\n    dhcp4: true\n")
    f.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][0] == "api/cloud-templates/harv-fake" and sent[-1][1]["type"] == "network" and sent[-1][1]["name"] == "net1"
    w.locator('tr[data-ct-ref="default/base"] [data-ct-act="edit"]').click()
    f = w.locator('[data-ct="form"] form')
    expect(f.locator('[name="text"]')).to_have_value("#cloud-config\npackages: [htop]\n")
    f.locator('[name="description"]').fill("updated")
    f.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1][0] == "api/cloud-templates/harv-fake/default/base/do/update" and sent[-1][1]["description"] == "updated"


def test_an_encrypted_storage_class_form(ui):
    page, sent = ui
    page.evaluate("ObjectForms.openNew('storageclass', 'harv-fake', {})")
    w = page.locator("#fp-new-storageclass-harv-fake")
    w.locator('[name="name"]').wait_for(timeout=8000)
    expect(w.locator('[name="engine"] option[value="lvm"]')).to_have_attribute("disabled", "")
    expect(w.locator('[data-f="secret"]')).to_be_hidden()
    w.locator('[name="name"]').fill("enc")
    w.locator('[name="encrypted"]').check()
    expect(w.locator('[data-f="secret"]')).to_be_visible()
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, body = sent[-1]
    assert path == "api/storageclass/harv-fake" and body["encrypted"] is True and body["secret"] == "default/crypto"
    assert body["engine"] == "longhorn-v1"


def test_typed_secret_and_new_values(ui):
    page, sent = ui
    page.evaluate("ObjectForms.openNew('secret', 'harv-fake', {})")
    w = page.locator("#fp-new-secret-harv-fake")
    w.locator('[name="name"]').wait_for(timeout=8000)
    w.locator('[name="name"]').fill("reg")
    w.locator('[name="type"]').select_option("kubernetes.io/dockerconfigjson")
    expect(w.locator('[data-f="server"]')).to_be_visible()
    w.locator('[name="server"]').fill("quay.io")
    w.locator('[name="username"]').fill("u")
    w.locator('[name="password"]').fill("p-secret")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, body = sent[-1]
    assert path == "api/secret/harv-fake/default/reg/do/create"
    assert body["type"] == "kubernetes.io/dockerconfigjson" and body["fields"] == {"server": "quay.io", "username": "u", "password": "p-secret"}
    page.evaluate("ObjectForms.editSecret('harv-fake', {namespace: 'default', name: 'app', type: 'Opaque', keys: ['a', 'b']}, () => {})")
    e = page.locator("#fp-edit-secret-harv-fake-default-app")
    e.locator('[data-sec-key="a"]').fill("new-a")
    e.locator('[data-sec-remove="b"]').check()
    e.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/secret/harv-fake/default/app/do/update", {"fields": {"a": "new-a", "remove": ["b"]}})
    expect(e.locator('[data-sec-key="a"]')).to_have_value("")                   # jamais gardée à l'écran


def test_an_ssh_key_can_be_edited(ui):
    page, sent = ui
    page.evaluate("ObjectForms.editSshKey('harv-fake', {namespace: 'default', name: 'ops', public_key: 'ssh-ed25519 AAAA old'}, () => {})")
    e = page.locator("#fp-edit-sshkey-harv-fake-default-ops")
    e.locator('[name="public_key"]').fill("ssh-ed25519 BBBB new@host")
    e.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert sent[-1] == ("api/sshkey/harv-fake/default/ops/do/update", {"public_key": "ssh-ed25519 BBBB new@host", "description": ""})


def test_launching_a_version_fills_the_creation_window(ui):
    """Lancer depuis une version : le modèle est choisi, la version demandée
    au serveur, et son cloud-init apparaît modifiable dans la section."""
    page, sent = ui
    asked = []

    def spec(route, req):
        asked.append(req.url)
        fulfill(route, {"template": "default/web", "version": "default/web-3",
                        "vm": {"metadata": {}, "spec": {"template": {"spec": {"domain": {"cpu": {"cores": 2}}}}}},
                        "cloudinit": {"user_data": "#cloud-config\npackages: [nginx]\n", "network_data": ""}})
    page.route("**/api/vmtemplates/harv-fake", lambda r, q: fulfill(r, {"templates": [{"namespace": "default", "name": "web"}]}))
    page.route("**/api/vmtemplates/harv-fake/default/web**", spec)
    page.evaluate("Templates.open('harv-fake')")
    w = page.locator(".floating-panel", has=page.locator(".tpl-win")).last
    w.locator('tr[data-ver="default/web-3"] [data-tpl-act="launch"]').click()
    c = page.locator("#fp-vm-create")
    expect(c.locator('[name="template"]')).to_have_value("default/web", timeout=8000)
    page.wait_for_function("document.querySelector('#fp-vm-create [data-result]') !== null")
    page.wait_for_timeout(800)
    assert asked and asked[-1].endswith("?version=default%2Fweb-3")
    c.locator('.vm-edit-nav button[data-section="cloudinit"]').click()
    expect(c.locator('[data-ci="userData"]')).to_have_value("#cloud-config\npackages: [nginx]\n")
