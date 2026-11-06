"""2.6.0 : onglet Artefacts de Cluster API et Paramètres > Artefacts, sur
routes simulées.

* import d'un paquet non signé : avertissement, case à cocher, Accepter
  grisé puis actif, demande envoyée avec l'acquittement ;
* mode strict : la fenêtre dit le refus et le réglage qui l'impose ;
* badges de confiance ; échappement d'un nom de paquet et d'un principal
  hostiles ;
* Paramètres > Artefacts : interrupteur grisé quand config.yaml verrouille.
"""

import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_auto_tabs_220 as t220  # noqa: E402

HOSTILE = '<img src=x onerror="window.__pwned=1">'
fulfill = t220.fulfill


def staged(state="unsigned", ack="unsigned", code="acknowledge-unsigned", ok=True, strict=False, locked=False,
           principal=None):
    return {"ok": ok, "name": "socle", "version": "2026.11", "items": 3, "size": 1234, "files": 4,
            "trust": {"state": state, "principal": principal, "key_fingerprint": None},
            "acknowledge_needed": ack, "code": code, "strict": strict, "locked": locked}


def open_artefacts(context, flask_server, packages=None, stage=None, accept_log=None, detail=None):
    page = t220.open_auto(context, flask_server, "#automation/capi")
    pk = packages if packages is not None else []
    page.route(re.compile(r".*/api/artefacts$"), lambda r, q: fulfill(r, {"packages": pk, "strict": False,
                                                                          "locked": False}))
    page.route(re.compile(r".*/api/artefacts/upload.*"),
               lambda r, q: fulfill(r, {"ok": True, "action_id": "a1", "staged": stage or staged()}))

    def accept(route, req):
        if accept_log is not None:
            accept_log.append(json.loads(req.post_data or "{}"))
        fulfill(route, {"action_id": "a2"}, 202)
    page.route(re.compile(r".*/api/artefacts/socle/2026\.11/accept"), accept)
    if detail:
        page.route(re.compile(r".*/api/artefacts/[^/]+/[^/]+$"), lambda r, q: fulfill(r, detail))
    page.locator('#tab-automation .sub-tab[data-capi-tab="artefacts"]').click()
    return page


def choose_archive(page, tmp_path):
    f = tmp_path / "socle-2026.11.tar.gz"
    f.write_bytes(b"x")
    page.locator("#btn-artefacts-import").click()
    page.locator('.art-modal [data-x="archive"]').set_input_files(str(f))
    page.locator(".art-modal [data-art-upload]").click()


def test_an_unsigned_package_needs_the_box_ticked_before_accept(context, flask_server, tmp_path):
    log = []
    page = open_artefacts(context, flask_server, accept_log=log)
    choose_archive(page, tmp_path)
    warn = page.locator(".art-modal .art-warning")
    expect(warn).to_contain_text("This package is not signed. Its origin cannot be checked.")
    accept = page.locator(".art-modal [data-art-accept]")
    expect(accept).to_be_disabled()
    assert accept.get_attribute("title")                                   # info-bulle
    page.locator('.art-modal [data-x="ack"]').check()
    expect(accept).to_be_enabled()
    page.locator('.art-modal [data-x="ack"]').uncheck()
    expect(accept).to_be_disabled()
    page.locator('.art-modal [data-x="ack"]').check()
    accept.click()
    expect(page.locator(".art-modal")).to_have_count(0)
    assert log == [{"acknowledge": "unsigned"}]


def test_a_valid_package_is_accepted_without_a_box(context, flask_server, tmp_path):
    page = open_artefacts(context, flask_server, stage=staged("valid", None, "valid", principal="ops@client"))
    choose_archive(page, tmp_path)
    expect(page.locator(".art-modal .art-staged")).to_contain_text("Signed (ops@client)")
    expect(page.locator(".art-modal [data-x=ack]")).to_have_count(0)
    expect(page.locator(".art-modal [data-art-accept]")).to_be_enabled()


@pytest.mark.parametrize("locked,reason", [
    (True, "imposed by the console configuration"),
    (False, "Settings > Artefacts"),
])
def test_strict_mode_says_the_refusal_and_what_imposes_it(context, flask_server, tmp_path, locked, reason):
    page = open_artefacts(context, flask_server,
                          stage=staged(ack=None, code="strict-refuses-unsigned", ok=False, strict=True, locked=locked))
    choose_archive(page, tmp_path)
    refused = page.locator('.art-modal [data-x="refused"]')
    expect(refused).to_contain_text("Strict mode refuses this package")
    expect(refused).to_contain_text(reason)
    expect(page.locator(".art-modal [data-art-accept]")).to_be_disabled()


def test_badges_and_hostile_names_are_escaped(context, flask_server):
    pk = [{"name": HOSTILE, "version": "1.0.0", "items": 1, "trust": "valid", "principal": HOSTILE,
           "imported_by": "alice", "imported_at": "2026-11-06"},
          {"name": "b", "version": "2.0.0", "items": 2, "trust": "unknown-key", "principal": None},
          {"name": "c", "version": "3.0.0", "items": 2, "trust": "unsigned", "principal": None}]
    detail = {"name": HOSTILE, "version": "1.0.0", "index": {"items": [{"name": "p", "kind": "policy", "later": True}]},
              "images": [{"name": HOSTILE, "digest": "sha256:" + "a" * 64, "size": 10, "platform": "linux/amd64"}],
              "trust": {"state": "valid", "principal": HOSTILE, "key_fingerprint": "SHA256:abc", "person": HOSTILE,
                        "date": "d", "mode": "permissive"}, "published": []}
    page = open_artefacts(context, flask_server, packages=pk, detail=detail)
    badges = page.locator("#artefacts-body .art-table .art-trust")
    expect(badges).to_have_count(3)
    expect(badges.nth(0)).to_contain_text("Signed")
    expect(badges.nth(1)).to_contain_text("Unknown key")
    expect(badges.nth(2)).to_contain_text("Unsigned")
    expect(page.locator("#artefacts-body .art-table")).to_contain_text(HOSTILE)       # texte, pas balise
    page.locator(".art-row").first.click()
    expect(page.locator("#artefacts-detail")).to_contain_text("coming (2.7)")
    expect(page.locator("#artefacts-detail .art-images")).to_contain_text(HOSTILE)
    assert page.evaluate("() => window.__pwned") is None
    assert page.locator("#artefacts-body img").count() == 0


def test_settings_strict_switch_is_greyed_when_config_locks_it(context, flask_server):
    page = t220.open_auto(context, flask_server, "#automation/capi")
    page.route(re.compile(r".*/api/artefacts/settings$"), lambda r, q: fulfill(r, {
        "strict": True, "locked": True,
        "signers": [{"principal": HOSTILE, "key_type": "ssh-ed25519", "fingerprint": "SHA256:x", "comment": ""}]}))
    page.locator("#btn-settings").click()
    page.locator('.settings-tab[data-stab="artefacts"]').click()
    sw = page.locator("#art-strict")
    expect(sw).to_be_disabled()
    expect(sw).to_be_checked()
    expect(page.locator("#artefacts-settings-body")).to_contain_text("imposed by the console configuration")
    expect(page.locator("#artefacts-settings-body table")).to_contain_text(HOSTILE)
    assert page.evaluate("() => window.__pwned") is None
