"""2.0.0, tâche 10 : chaque écran de l'inventaire s'ouvre à froid.

Table unique `tests/nav_inventory.py` : pour chaque ligne, l'adresse
chargée à froid active la section annoncée et montre son sélecteur
(cluster `harv-fake` du serveur de test, hôte KVM `node2` simulé). Aucun
écran n'est perdu au retrait de l'ancien menu."""

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402
from test_kvm_196 import DETAIL, GESTURES, NODE2, VMS  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nav_inventory import SCREENS, address  # noqa: E402

C, H = "harv-fake", "node2"
PUBLIC = ("name", "address", "port", "user", "uri", "labels")


def kvm_api(route, req):
    path = unquote(req.url.split("/api/kvm/", 1)[1].split("?", 1)[0])
    body, status = {"error": "not found"}, 404
    if path == "hosts" and "declared=1" in req.url:
        body, status = {"hosts": [{k: NODE2[k] for k in PUBLIC}], "warnings": [], "role": "admin",
                        "gestures": GESTURES}, 200
    elif path == "hosts":
        body, status = {"hosts": [NODE2], "warnings": [], "role": "admin", "gestures": GESTURES}, 200
    elif path == "hosts/node2":
        body, status = dict(DETAIL, role="admin"), 200
    elif path == "hosts/node2/declaration":
        body, status = dict({k: NODE2[k] for k in PUBLIC}, fingerprints=[], role="admin"), 200
    elif path == "vms":
        body, status = {"vms": VMS, "role": "admin", "gestures": GESTURES, "warnings": [], "operations": {},
                        "edit_operations": {}, "hosts": [{"name": "node2", "state": "ok", "count": len(VMS)}]}, 200
    elif path.endswith("/pools") or path.endswith("/links"):
        body, status = {"pools": [], "links": []}, 200
    return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.mark.parametrize("entry", SCREENS, ids=[s[1] for s in SCREENS])
def test_each_inventory_address_opens_its_screen_cold(context, flask_server, entry):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_sidebar_pinned','1');")
    page = context.new_page()
    page.route(re.compile(r".*/api/kvm/.*"), kvm_api)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    goto(page, flask_server["base_url"], address(entry, C, H))
    expect(page.locator(f"#tab-{entry[2]}")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(entry[3]).first).to_be_attached()
    expect(page.locator("#access-notice")).to_be_hidden()
    assert errors == [], errors
