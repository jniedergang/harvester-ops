"""2.10.0 (tâche 10) : option External Secrets de la fenêtre de création d'un
cluster (menu Accès) et du modèle de cluster des plans.

* coché, les namespaces et le chart partent dans `external_secrets` du
  contrôle et de la création ; décoché, la clé est absente ;
* refus du serveur (eso-needs-openbao) dit en clair ;
* OpenBao inactif : interrupteur grisé, la raison dite ;
* non-administrateur : grisé avec l'info-bulle.
"""

import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capi_create_view import form, menu, x  # noqa: E402,F401


def whoami(context, role):
    context.route(re.compile(r".*/api/whoami$"), lambda r, q: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"role": role, "roles_active": True, "user": "u", "auth": "local"})))


def secrets_status(context, active):
    context.route(re.compile(r".*/api/secrets/status$"), lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"active": active, "mode": "b1" if active else "local"})))


def test_checked_option_reaches_check_and_create(context, form):
    whoami(context, "admin")
    secrets_status(context, True)
    page, host, calls = form()
    page.on("dialog", lambda d: d.accept())
    x(host, "name").fill("web")
    menu(host, "access")
    box = host.locator(".capi-eso")
    on = box.locator("[data-e-on]")
    expect(on).to_be_enabled()
    expect(box.locator("[data-e-fields]")).to_be_hidden()
    on.check()
    box.locator('[data-e="namespaces"]').fill("equipe-a, equipe-b")
    box.locator('[data-e="registry"]').fill("amorcage")
    box.locator('[data-e="package"]').fill("secrets")
    box.locator('[data-e="version"]').fill("1.2.0")
    box.locator('[data-e="version"]').dispatch_event("change")
    want = {"namespaces": ["equipe-a", "equipe-b"],
            "chart": {"registry": "amorcage", "package": "secrets", "version": "1.2.0"}}
    for _ in range(50):
        if calls["check"] and calls["check"][-1].get("external_secrets") == want:
            break
        page.wait_for_timeout(100)
    assert calls["check"][-1]["external_secrets"] == want
    expect(x(host, "create")).to_be_enabled(timeout=5000)
    x(host, "create").click()
    expect(x(host, "feedback")).to_contain_text("capi00000001", timeout=5000)
    assert calls["create"][-1]["external_secrets"] == want
    assert all(el.get_attribute("data-tip") for el in box.locator(".tip").all())


def test_unchecked_means_no_key(context, form):
    whoami(context, "admin")
    secrets_status(context, True)
    page, host, calls = form()
    x(host, "name").fill("web")
    x(host, "name").dispatch_event("change")
    for _ in range(50):
        if calls["check"]:
            break
        page.wait_for_timeout(100)
    assert "external_secrets" not in calls["check"][-1]


def test_server_refusal_is_said_in_words(context, form):
    whoami(context, "admin")
    secrets_status(context, True)
    page, host, calls = form()
    page.route("**/api/capi/harv-fake/cluster-create", lambda r, q: r.fulfill(
        status=409, content_type="application/json",
        body=json.dumps({"error": "External Secrets need OpenBao", "code": "eso-needs-openbao"})))
    page.on("dialog", lambda d: d.accept())
    x(host, "name").fill("web")
    menu(host, "access")
    host.locator(".capi-eso [data-e-on]").check()
    host.locator('.capi-eso [data-e="namespaces"]').fill("equipe-a")
    host.locator('.capi-eso [data-e="namespaces"]').dispatch_event("change")
    expect(x(host, "create")).to_be_enabled(timeout=5000)
    x(host, "create").click()
    expect(x(host, "feedback")).to_contain_text("OpenBao", timeout=5000)
    expect(x(host, "feedback")).to_contain_text("Paramètres > Secrets")          # interface en français


def test_inactive_openbao_greys_the_switch_with_the_reason(context, form):
    whoami(context, "admin")
    secrets_status(context, False)
    page, host, _ = form()
    menu(host, "access")
    box = host.locator(".capi-eso")
    expect(box.locator("[data-e-on]")).to_be_disabled(timeout=5000)
    expect(box.locator("[data-e-reason]")).to_contain_text("OpenBao")


def test_operator_sees_the_switch_greyed_with_a_tooltip(context, form):
    whoami(context, "operator")
    page, host, _ = form()
    menu(host, "access")
    wrap = host.locator(".capi-eso [data-e-wrap]")
    expect(host.locator(".capi-eso [data-e-on]")).to_be_disabled()
    expect(wrap).to_have_attribute("data-tip", re.compile("administrateurs"))
