"""v1.28.0 — le panneau de création de VM, dans un vrai navigateur.

Ce que les tests de source ne voient pas : que les huit sections de
l'éditeur se rendent VRAIMENT sur un squelette de VM, sans exploser parce
que la machine n'existe pas encore. C'est tout le pari de la fonctionnalité
(rejouer l'éditeur au lieu de réécrire un formulaire), et c'est exactement
là qu'il pouvait se casser.

Le réseau est intercepté : aucun test ne crée quoi que ce soit sur un
cluster. La création réelle a été faite à la main sur harv1 et consignée
dans le CHANGELOG.
"""

import json

import pytest

playwright = pytest.importorskip("playwright")


def open_panel(page, base_url):
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','namespaces');")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    page.click('#btn-vm-create')
    page.wait_for_selector('#fp-vm-create', timeout=10000)
    page.wait_for_timeout(600)


def test_the_button_opens_the_panel(context, flask_server):
    page = context.new_page()
    open_panel(page, flask_server["base_url"])
    assert page.locator('#fp-vm-create').count() == 1
    # Ce qui n'existe qu'à la création.
    for field in ('name', 'namespace', 'count', 'start'):
        assert page.locator(f'#fp-vm-create [name="{field}"]').count() == 1, field
    assert page.locator('#fp-vm-create [name="start"]').is_checked(), \
        "démarrer après création est le défaut choisi"


def test_every_editor_section_renders_on_a_skeleton(context, flask_server):
    """Le pari de la fonctionnalité. Une section qui suppose une VM
    existante planterait ici, et le panneau perdrait un pan entier des
    réglages sans que rien ne le dise."""
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    open_panel(page, flask_server["base_url"])

    buttons = page.locator('#fp-vm-create .vm-edit-nav button')
    assert buttons.count() == 8, f"{buttons.count()} sections au lieu de 8"
    for i in range(buttons.count()):
        btn = buttons.nth(i)
        section = btn.get_attribute('data-section')
        btn.click()
        page.wait_for_timeout(400)
        visible = page.locator('#fp-vm-create .vm-create-section:not([hidden])')
        assert visible.count() == 1, f"section {section} non rendue"
        assert visible.inner_text().strip(), f"section {section} vide"
    assert not errors, f"erreurs JS : {errors}"


def test_no_per_section_apply_button_in_create_mode(context, flask_server):
    """Ces boutons patchent une VM vivante. Ici il n'y en a pas : c'est le
    bouton Créer qui vaut validation."""
    page = context.new_page()
    open_panel(page, flask_server["base_url"])
    page.click('#fp-vm-create .vm-edit-nav button[data-section="compute"]')
    page.wait_for_timeout(500)
    bar = page.locator('#fp-vm-create .vm-create-section:not([hidden]) .apply-bar')
    if bar.count():
        assert not bar.first.is_visible(), \
            "la barre d'application d'une section n'a rien à patcher en création"


def test_the_request_carries_a_complete_manifest(context, flask_server):
    page = context.new_page()
    sent = {}

    def handle(route, request):
        try:
            sent.update(json.loads(request.post_data or "{}"))
        except Exception:
            pass
        route.fulfill(status=202, content_type="application/json",
                      body=json.dumps({"action_id": "act-1", "names": ["web-01", "web-02"]}))

    page.route("**/api/vms/*/create", handle)
    open_panel(page, flask_server["base_url"])
    page.fill('#fp-vm-create [name="name"]', 'web')
    page.fill('#fp-vm-create [name="count"]', '2')
    page.click('#fp-vm-create [data-action="create"]')
    page.wait_for_timeout(900)

    assert sent.get("name") == "web"
    assert sent.get("count") == 2
    assert sent.get("start") is True
    manifest = sent.get("manifest") or {}
    assert manifest.get("kind") == "VirtualMachine"
    assert manifest["spec"]["template"]["spec"]["domain"]["cpu"], \
        "le squelette doit porter un domaine complet"
    # L'identifiant d'action revient à l'opérateur, sans quoi il ne peut pas
    # suivre la création dans le dock.
    assert "act-1" in page.locator('#fp-vm-create [data-result]').inner_text()


def test_validate_only_asks_for_a_dry_run(context, flask_server):
    page = context.new_page()
    sent = {}

    def handle(route, request):
        sent.update(json.loads(request.post_data or "{}"))
        route.fulfill(status=202, content_type="application/json",
                      body=json.dumps({"action_id": "act-2", "names": ["vm-01"]}))

    page.route("**/api/vms/*/create", handle)
    open_panel(page, flask_server["base_url"])
    page.click('#fp-vm-create [data-action="dry-run"]')
    page.wait_for_timeout(900)
    assert sent.get("dry_run") is True


def test_a_refusal_is_shown_to_the_operator(context, flask_server):
    page = context.new_page()
    page.route("**/api/vms/*/create", lambda route: route.fulfill(
        status=400, content_type="application/json",
        body=json.dumps({"error": "invalid VM name (RFC 1123)"})))
    open_panel(page, flask_server["base_url"])
    page.click('#fp-vm-create [data-action="create"]')
    page.wait_for_timeout(900)
    assert "RFC 1123" in page.locator('#fp-vm-create [data-result]').inner_text()
