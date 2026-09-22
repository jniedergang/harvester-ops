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


def open_panel(page, base_url, lang='en'):
    page.context.add_init_script(
        f"localStorage.setItem('harvester_ops_language','{lang}');"
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
    # v1.44.3 : comme l'interface de Harvester. Sans stratégie d'éviction,
    # une VM est ARRÊTÉE par la mise en maintenance de son nœud au lieu de
    # migrer (constaté sur harvlab).
    assert manifest["spec"]["template"]["spec"].get("evictionStrategy") == \
        "LiveMigrateIfPossible"
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


# ---------------------------------------------------------------------------
# v1.34.0 : le disque naissait en « volume existant », sans champ de taille
# ---------------------------------------------------------------------------

def open_disks(page, base_url, lang='en'):
    open_panel(page, base_url, lang)
    page.click('#fp-vm-create .vm-edit-nav button[data-section="disks"]')
    page.wait_for_timeout(500)
    add = page.locator('#fp-vm-create .vm-create-section:not([hidden]) '
                       '.tf-block-add, #fp-vm-create .vm-create-section:not([hidden]) '
                       'button:has-text("Add Disks")')
    if page.locator('#fp-vm-create .vm-create-section:not([hidden]) '
                    '.tf-block-item').count() == 0:
        add.first.click()
        page.wait_for_timeout(500)
    return page.locator('#fp-vm-create .vm-create-section:not([hidden]) '
                        '.tf-block-item').first


def field_of(item, name):
    return item.locator(f'[name$=".{name}"]')


def visible_field(item, name):
    el = field_of(item, name)
    return el.count() > 0 and el.first.is_visible()


def test_a_new_disk_boots_from_an_image_not_an_existing_volume(context, flask_server):
    """Le défaut était `pvc`, c'est-à-dire « attacher un volume existant ».
    Ce mode n'a ni taille ni storage class à choisir (le PVC porte les
    siennes), donc le panneau de CRÉATION s'ouvrait sans champ de taille et
    on la croyait oubliée. Une VM qu'on crée doit d'abord démarrer."""
    page = context.new_page()
    item = open_disks(page, flask_server["base_url"])
    assert field_of(item, 'source').first.input_value() == 'image'


def test_the_size_field_is_there_when_the_disk_is_a_new_one(context, flask_server):
    page = context.new_page()
    item = open_disks(page, flask_server["base_url"])
    assert visible_field(item, 'size'), "pas de champ Taille sur un disque neuf"


def test_attaching_an_existing_volume_still_hides_the_size(context, flask_server):
    """Le masquage reste juste : un PVC déjà créé porte sa propre taille,
    proposer de la choisir mentirait."""
    page = context.new_page()
    item = open_disks(page, flask_server["base_url"])
    field_of(item, 'source').first.select_option('pvc')
    page.wait_for_timeout(300)
    assert not visible_field(item, 'size')
    assert not visible_field(item, 'storage_class')
    assert visible_field(item, 'pvc')


def test_a_blank_disk_lets_the_operator_pick_the_storage_class(context, flask_server):
    page = context.new_page()
    item = open_disks(page, flask_server["base_url"])
    field_of(item, 'source').first.select_option('blank')
    page.wait_for_timeout(300)
    sc = field_of(item, 'storage_class').first
    assert sc.is_visible(), "storage class absente sur un disque vierge"
    assert not sc.is_disabled(), "elle doit rester modifiable ici"


def test_an_image_disk_shows_its_storage_class_locked(context, flask_server):
    """Elle est imposée : la classe d'une image porte `backingImage`, et
    c'est elle qui fait démarrer le disque. La MONTRER verrouillée répond à
    « pourquoi je ne peux pas la choisir » ; la cacher ne répondait rien."""
    page = context.new_page()
    item = open_disks(page, flask_server["base_url"])
    sc = field_of(item, 'storage_class').first
    assert sc.is_visible(), "storage class cachée sur un disque d'image"
    assert sc.is_disabled(), "elle ne doit pas être modifiable pour une image"


def test_the_second_disk_is_a_blank_data_disk(context, flask_server):
    """Le premier démarre la VM, les suivants portent des données. Aucun ne
    doit retomber sur « volume existant », le cas rare."""
    page = context.new_page()
    open_disks(page, flask_server["base_url"])
    section = page.locator('#fp-vm-create .vm-create-section:not([hidden])')
    section.locator('.tf-block-add, button:has-text("Add Disks")').first.click()
    page.wait_for_timeout(500)
    items = section.locator('.tf-block-item')
    assert items.count() == 2, f"{items.count()} disque(s)"
    assert field_of(items.nth(1), 'source').first.input_value() == 'blank'


# ---------------------------------------------------------------------------
# v1.34.0 : la ligne de place restante, une unité sur CHAQUE nombre
# ---------------------------------------------------------------------------

# L'endpoint rend une LISTE nue, pas un objet : une charge simulée en
# {"images": [...]} laissait le select vide sans que rien ne le signale.
CANNED_IMAGES = [{"namespace": "default", "name": "img-1",
                  "display_name": "leap.qcow2",
                  "storage_class": "lh-test",
                  "virtual_size": 2 * 1024 ** 3}]
CANNED_CAPACITY = {"classes": {"lh-test": {"allocatable": 1107 * 1024 ** 3,
                                           "replicas": 1}}}


def with_storage(page):
    # `*` ne franchit pas le `?namespace=...` : sans `**` la réponse réelle
    # passait et la liste d'images du test restait vide.
    page.route("**/api/images/**", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(CANNED_IMAGES)))
    page.route("**/api/storage-capacity/**", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(CANNED_CAPACITY)))


def disk_hint(page):
    return page.locator('#fp-vm-create [data-disk-capacity]').first.inner_text()


def test_every_number_on_the_capacity_line_carries_its_unit(context, flask_server):
    """« 1107 GiB allocatable − 10 requested here » laissait deviner si ce 10
    était des GiB, des MiB ou des disques. L'unité manquait sur la quantité
    demandée, et sur elle seule."""
    page = context.new_page()
    with_storage(page)
    item = open_disks(page, flask_server["base_url"])
    item.locator('[name$=".size"]').first.fill('10Gi')
    field_of(item, 'image').first.select_option('default/img-1')
    page.wait_for_timeout(900)
    line = disk_hint(page)
    assert 'requested here' in line, line
    # Trois nombres, trois unités.
    assert line.count('GiB') == 3, line


def test_the_unit_follows_the_language(context, flask_server):
    """« Gio » était codé en dur : l'interface anglaise affichait une
    abréviation française."""
    page = context.new_page()
    with_storage(page)
    item = open_disks(page, flask_server["base_url"], lang='fr')
    item.locator('[name$=".size"]').first.fill('10Gi')
    field_of(item, 'image').first.select_option('default/img-1')
    page.wait_for_timeout(900)
    line = disk_hint(page)
    assert 'Gio' in line and 'GiB' not in line, line


def test_without_an_image_the_hint_asks_for_an_image(context, flask_server):
    """Source et taille étaient renseignées et l'écran réclamait quand même
    « a source and a size » : ce qui manquait, c'était l'image."""
    page = context.new_page()
    with_storage(page)
    item = open_disks(page, flask_server["base_url"])
    item.locator('[name$=".size"]').first.fill('10Gi')
    page.wait_for_timeout(700)
    assert 'image' in disk_hint(page).lower()


def test_the_pending_storage_class_says_where_it_will_come_from(context, flask_server):
    """Verrouillée sur une valeur vide, elle donnait un champ grisé qui avait
    l'air cassé."""
    page = context.new_page()
    with_storage(page)
    item = open_disks(page, flask_server["base_url"])
    sc = field_of(item, 'storage_class').first
    assert sc.is_disabled()
    shown = sc.locator('option:checked').inner_text()
    assert 'image' in shown.lower(), f"affiché : {shown!r}"
    field_of(item, 'image').first.select_option('default/img-1')
    page.wait_for_timeout(900)
    assert sc.locator('option:checked').inner_text() == 'lh-test'


def test_leaving_the_image_mode_unlocks_the_storage_class(context, flask_server):
    """L'option d'attente ne doit pas rester collée dans la liste."""
    page = context.new_page()
    with_storage(page)
    item = open_disks(page, flask_server["base_url"])
    field_of(item, 'source').first.select_option('blank')
    page.wait_for_timeout(500)
    sc = field_of(item, 'storage_class').first
    assert not sc.is_disabled()
    assert 'follows the image' not in sc.inner_text().lower()
