"""2.7.1 (revue 2.7.0 m3 ii) : la case « retirer aussi les images » de la
fenêtre Désinstaller, sur routes simulées.

* images chargées par la console, aucune employée ailleurs : case active,
  cochée elle part dans la demande ;
* aucune image inscrite : case grisée, l'info-bulle dit pourquoi ;
* une image employée ailleurs : case grisée, la liste est affichée (noms
  hostiles échappés).
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capi_uninstall_270 import HOSTILE, PLAN, open_window  # noqa: E402

IMAGES = ["ghcr.io/rancher-sandbox/cluster-api-provider-harvester:v0.10.1"]


def test_loaded_images_can_be_removed(context, flask_server):
    calls = []
    page = open_window(context, flask_server, dict(PLAN, images=IMAGES, images_in_use=[]), calls)
    m = page.locator("#capi-uninstall-modal")
    expect(m.locator('[data-x="providers"] li')).to_have_count(2)
    box = m.locator('[data-x="images"]')
    expect(box).to_be_enabled()
    box.check()
    m.locator('[data-x="name"]').fill("harv-fake")
    m.locator('[data-x="go"]').click()
    expect(page.locator("#capi-uninstall-modal")).to_have_count(0)
    assert calls == [{"confirm": "harv-fake", "remove_images": True}]


def test_no_recorded_image_greys_the_box_with_the_reason(context, flask_server):
    page = open_window(context, flask_server, dict(PLAN, images=[], images_in_use=[]), [])
    m = page.locator("#capi-uninstall-modal")
    expect(m.locator('[data-x="providers"] li')).to_have_count(2)
    expect(m.locator('[data-x="images"]')).to_be_disabled()
    assert "No image loaded by the console" in m.locator(".apply-dry").get_attribute("data-tip")


def test_an_image_used_elsewhere_greys_the_box_and_is_listed(context, flask_server):
    plan = dict(PLAN, images=IMAGES, images_in_use=[{"image": IMAGES[0], "used_by": ["pod web/app", HOSTILE]}])
    page = open_window(context, flask_server, plan, [])
    m = page.locator("#capi-uninstall-modal")
    expect(m.locator('[data-x="images-in-use"]')).to_contain_text("pod web/app")
    expect(m.locator('[data-x="images-in-use"]')).to_contain_text(HOSTILE)
    expect(m.locator('[data-x="images"]')).to_be_disabled()
    assert "Another workload uses" in m.locator(".apply-dry").get_attribute("data-tip")
    assert m.locator("img").count() == 0 and page.evaluate("() => window.__pwned") is None
