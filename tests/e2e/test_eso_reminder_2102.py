"""2.10.2 (point 332 A) : External Secrets choisi à la création.

* la fenêtre de création rappelle que les namespaces d'équipe s'activent
  depuis l'onglet une fois créés dans le cluster ;
* l'onglet le rappelle sous les namespaces activés ;
* chart posé sans namespace activé : constat `eso-namespaces-none` avec son
  info-bulle.
"""

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capi_create_eso_2100 import secrets_status, whoami  # noqa: E402
from test_capi_create_view import form, menu, x  # noqa: E402,F401
from test_child_eso_2100 import open_eso, state_doc  # noqa: E402


def test_the_creation_window_reminds_the_namespaces(context, form):
    whoami(context, "admin")
    secrets_status(context, True)
    page, host, _calls = form()
    x(host, "name").fill("web")
    menu(host, "access")
    note = host.locator(".capi-eso [data-e-reminder]")
    expect(note).to_be_visible()
    expect(note).to_contain_text("Cluster API")              # langue du formulaire de test : le rappel, traduit


@pytest.mark.parametrize("doc,finding", [
    (state_doc(namespaces=[], auth=None, stores=[], externalsecrets=[], labelled=[], example=None,
               findings=[{"code": "eso-namespaces-none", "level": "warn", "facts": {"cluster": "apps/apps"}}]), True),
    (state_doc(), False),
])
def test_the_tab_reminds_and_says_no_namespace_enabled(context, flask_server, doc, finding):
    page, _ = open_eso(context, flask_server, {}, doc=doc)
    body = page.locator("#capi-eso-body")
    expect(body.locator("[data-eso-reminder]")).to_contain_text("create it there first")
    f = body.locator('[data-code="eso-namespaces-none"]')
    if finding:
        expect(f).to_contain_text("no namespace is enabled")
        assert re.search("ExternalSecret can sync", f.get_attribute("data-tip") or "")
        expect(f).to_have_class(re.compile(r"\bsev-watch\b"))
    else:
        expect(f).to_have_count(0)
