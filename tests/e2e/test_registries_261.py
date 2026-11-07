"""2.6.1 : Vérifier un registre depuis un réseau de VMs choisi (pod sonde,
constats traduits avec leur raison) et Paramètres > Registres (port
d'écriture réservé à la console), sur routes simulées."""

import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_auto_tabs_220 as t220  # noqa: E402
import test_registries_260 as t260  # noqa: E402

fulfill = t220.fulfill
HOSTILE = t260.HOSTILE


@pytest.mark.parametrize("finding,text", [
    ({"code": "registry-vm-network-ok", "level": "ok",
      "facts": {"registry": "amorcage", "endpoint": "172.16.2.76", "network": "default/lab-net",
                "address": "172.16.10.86"}},
     "172.16.2.76 answers from the VM network default/lab-net (probe address 172.16.10.86, TLS handshake done)."),
    ({"code": "registry-unreachable-from-vm-network", "level": "warn",
      "facts": {"registry": "amorcage", "endpoint": "172.16.2.76", "network": "default/lab-net",
                "address": "192.168.250.10", "failed": "route"}},
     "172.16.2.76 cannot be reached from the VM network default/lab-net: no route (the network gives no gateway)."),
    ({"code": "registry-vm-network-unchecked", "level": "warn",
      "facts": {"registry": "amorcage", "endpoint": "172.16.2.76", "network": "default/lab-net",
                "reason": "no-address", "detail": ""}},
     "not checked: the probe got no address on default/lab-net (no DHCP lease, no IPAM)."),
])
def test_check_probes_from_the_chosen_vm_network(context, flask_server, finding, text):
    calls = {}
    page, _ = t260.open_registries(context, flask_server, calls)
    page.route(re.compile(r".*/api/networks/harv-fake$"), lambda r, q: fulfill(r, [
        {"namespace": "default", "name": "lab-net", "vlan": None}, {"namespace": "default", "name": HOSTILE[:20],
                                                                    "vlan": "999"}]))

    def check(route, req):
        calls.setdefault("check", []).append(json.loads(req.post_data or "{}"))
        fulfill(route, {"findings": [finding], "blocked": False})
    page.route(re.compile(r".*/api/registries/amorcage/check/harv-fake$"), check)
    page.locator('.reg-row[data-reg="amorcage"]').click()
    sel = page.locator('#registries-detail [data-x="vm-net"]')
    expect(sel.locator("option")).to_have_count(3)
    assert sel.get_attribute("data-tip")                                     # info-bulle
    sel.select_option("default/lab-net")
    page.locator("#registries-detail [data-reg-check]").click()
    box = page.locator('#registries-detail [data-x="check"]')
    expect(box.locator(f'[data-code="{finding["code"]}"]')).to_contain_text(text)
    assert calls["check"] == [{"vm_network": "default/lab-net"}]
    assert page.evaluate("() => window.__pwned") is None


def test_settings_reserve_the_write_port_to_the_suggested_address(context, flask_server):
    page = t220.open_auto(context, flask_server, "#automation/capi")
    calls = []
    state = {"src": []}

    def settings(route, req):
        if req.method == "PUT":
            body = json.loads(req.post_data or "{}")
            calls.append(body)
            state["src"] = body["write_sources"]
            return fulfill(route, {"write_sources": state["src"], "action_id": "a1",
                                   "actions": [{"cluster": "harv-fake", "action_id": "a2"}], "skipped": []})
        fulfill(route, {"write_sources": state["src"], "suggested": {"harv-fake": "172.16.1.11/32"},
                        "bootstrap": {"harv-fake": ["amorcage"]}})
    page.route(re.compile(r".*/api/registries/settings(\?.*)?$"), settings)
    page.locator("#btn-settings").click()
    page.locator('.settings-tab[data-stab="registries"]').click()
    body = page.locator("#registries-settings-body")
    expect(body).to_contain_text("Applied on: harv-fake (amorcage)")
    expect(body).to_contain_text("does not see through a NAT")
    use = body.locator('[data-regset-use="172.16.1.11/32"]')
    expect(use).to_contain_text("Use 172.16.1.11/32 (toward harv-fake)")
    use.click()
    expect(page.locator("#regset-sources")).to_have_value("172.16.1.11/32")
    page.locator("#regset-sources").fill("172.16.1.11/32, 10.0.0.0/8")
    body.locator("[data-regset-save]").click()
    expect(page.locator("#regset-feedback")).to_contain_text("Saved; 1 cluster(s) being updated")
    assert calls == [{"write_sources": ["172.16.1.11/32", "10.0.0.0/8"]}]
    expect(page.locator("#regset-sources")).to_have_value("172.16.1.11/32, 10.0.0.0/8")
