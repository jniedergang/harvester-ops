"""v1.25.0 — le panneau de mise à jour du provider Terraform.

Exercé dans un vrai navigateur, sur la page servie par l'application. Le
réseau est intercepté : aucun test ne déclenche de téléchargement depuis
GitHub, seule la vraie installation faite à la main l'a fait.

Ce qui se vérifie ici et que les tests de source ne voient pas : le panneau
est bien rendu dans le sous-onglet Install, le formulaire part vers le bon
point d'entrée avec le bon corps, et un refus du serveur atterrit sous les
yeux de l'opérateur au lieu de disparaître dans la console.
"""

import json

import pytest

playwright = pytest.importorskip("playwright")


def _open_install_tab(page, base_url):
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','automation');"
        "localStorage.setItem('harvester_ops_tf_subtab','install');")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    # Les enfants d'Automation sont repliés tant que le groupe n'est pas
    # ouvert : cliquer l'onglet Terraform directement vise un élément
    # invisible.
    page.click('.tab-group-head[data-group="automation"]', timeout=5000)
    page.wait_for_timeout(300)
    page.click('.tab-child[data-subtab="terraform"]', timeout=5000)
    page.wait_for_selector('#tf-status-body .sub-tab[data-tf-tab="install"]',
                           timeout=10000)
    page.click('#tf-status-body .sub-tab[data-tf-tab="install"]')
    page.wait_for_timeout(300)


def test_the_update_panel_is_rendered(context, flask_server):
    page = context.new_page()
    _open_install_tab(page, flask_server["base_url"])

    assert page.locator('#tf-prov-form').count() == 1, "formulaire absent"
    assert page.locator('#tf-prov-file-form').count() == 1, \
        "la voie airgap (fichier local) doit être offerte, pas seulement l'URL"
    assert page.locator('#tf-prov-form input[name="source"]').count() == 1
    # Chaque contrôle porte son infobulle : règle du projet.
    for sel in ('#tf-prov-form button[type="submit"]',
                '#tf-prov-file-form button[type="submit"]'):
        tip = page.locator(sel).get_attribute('data-tip')
        assert tip, f"infobulle manquante sur {sel}"


def test_submitting_a_version_posts_it_to_the_install_endpoint(
        context, flask_server):
    page = context.new_page()
    seen = {}

    def handle(route, request):
        seen["url"] = request.url
        seen["body"] = json.loads(request.post_data or "{}")
        route.fulfill(status=202, content_type="application/json",
                      body=json.dumps({"action_id": "abc123def456",
                                       "source": "1.7.3"}))

    page.route("**/api/terraform/provider/install", handle)
    _open_install_tab(page, flask_server["base_url"])

    page.fill('#tf-prov-form input[name="source"]', '1.7.3')
    page.click('#tf-prov-form button[type="submit"]')
    page.wait_for_timeout(600)

    assert seen.get("body", {}).get("source") == "1.7.3"
    # L'action est rendue à l'opérateur : sans son identifiant il ne peut
    # pas la suivre dans le dock.
    assert "abc123def456" in page.locator('#tf-prov-result').inner_text()


def test_a_refused_source_is_shown_to_the_operator(context, flask_server):
    """Le serveur refuse un chemin local. Le message doit atterrir sous les
    yeux, pas seulement dans la console du navigateur."""
    page = context.new_page()
    _open_install_tab(page, flask_server["base_url"])

    page.fill('#tf-prov-form input[name="source"]', '/etc/passwd')
    page.click('#tf-prov-form button[type="submit"]')
    page.wait_for_timeout(800)

    text = page.locator('#tf-prov-result').inner_text().lower()
    assert "version" in text or "url" in text, \
        f"le refus du serveur n'est pas affiché : {text!r}"


def test_the_checksum_field_rejects_a_malformed_value(context, flask_server):
    """Garde côté navigateur : une empreinte tronquée est le moyen le plus
    simple de croire à une vérification qui n'a pas lieu."""
    page = context.new_page()
    _open_install_tab(page, flask_server["base_url"])

    page.fill('#tf-prov-form input[name="source"]', '1.7.3')
    page.fill('#tf-prov-form input[name="sha256"]', 'deadbeef')
    valid = page.evaluate(
        "document.querySelector('#tf-prov-form input[name=\"sha256\"]')"
        ".checkValidity()")
    assert valid is False
