"""v1.78.0 : Settings > Clusters dit qui a déclaré chaque cluster.

Un cluster de config.yaml (l'opérateur) est en lecture seule quand la
console ne peut pas écrire ce fichier (service packagé) : ses boutons de
remplacement et de suppression sont désactivés, avec une bulle qui dit
pourquoi. Un cluster déclaré par la console reste modifiable.
"""

import json
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


def _clusters(config_writable):
    return {"config_writable": config_writable, "clusters": [
        {"name": "harv-fake", "description": "op", "node_count": 1, "origin": "config"},
        {"name": "bm-lab", "description": "bm", "node_count": 1, "origin": "console"},
    ]}


def _open(page, config_writable):
    page.route("**/api/clusters", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(_clusters(config_writable))))
    page.click("#btn-settings")
    page.click('.settings-tab[data-stab="clusters"]')
    expect(page.locator("#clusters-list .cluster-card")).to_have_count(2)
    return page.locator("#clusters-list .cluster-card")


def test_config_clusters_are_read_only_when_config_is_not_writable(page):
    cards = _open(page, False)
    op, bm = cards.nth(0), cards.nth(1)
    expect(op.locator(".cluster-origin")).to_have_attribute("data-origin", "config")
    expect(bm.locator(".cluster-origin")).to_have_attribute("data-origin", "console")
    for sel in ('[data-act="delete"]', '[data-act="upload-kc"]', '[data-act="upload-ssh"]'):
        expect(op.locator(sel)).to_be_disabled()
        expect(bm.locator(sel)).to_be_enabled()
    # la raison, en bulle, sur le groupe désactivé ; les tests restent possibles
    tip = op.locator(".cluster-readonly")
    # (langue du navigateur : le texte exact dépend d'elle)
    expect(tip).to_have_attribute("data-tip", re.compile(r"config\.yaml.+(console|Konsole|consola)"))
    expect(op.locator('[data-act="test-kc"]')).to_be_enabled()
    expect(op.locator(".cluster-origin")).to_have_attribute("data-tip", re.compile(r"config\.yaml"))


def test_config_clusters_stay_editable_when_config_is_writable(page):
    cards = _open(page, True)
    op = cards.nth(0)
    expect(op.locator(".cluster-readonly")).to_have_count(0)
    expect(op.locator('[data-act="delete"]')).to_be_enabled()
