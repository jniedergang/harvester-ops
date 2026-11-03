"""2.2.1 : la fenêtre de série d'un profil garde les machines qui portent
déjà un système.

Routes simulées (aucune installation ne part) :
* série mixte absent + présent confirmé + présent sans nom + inconnu : une
  bannière par machine avec ses signes ; la série ne part pas tant qu'un nom
  manque ou est faux (nom faux dit) ; ensuite le nom tapé part avec sa
  ligne, et seulement pour les machines où un système semble présent ;
* le tableau change après la lecture : relue avant le départ ;
* refus du serveur (409) : la fenêtre montre ce que la console a relu ;
* info-bulles, échappement.
"""

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_bm_profiles_180 as t180  # noqa: E402

HOSTS = ["198.51.100.1", "198.51.100.2", "198.51.100.3", "198.51.100.4"]
ROWS = [{"bmc_host": h, "bmc_user": "admin",
         "values": {"hostname": f"node{i + 1}", "ip": f"192.0.2.1{i + 1}", "storage_ip": f"172.18.122.10{i + 1}"}}
        for i, h in enumerate(HOSTS)]
EVIDENCE = [{"code": "boots-from-disk", "name": "sles-secureboot"},
            {"code": "boot-entry", "name": "<img src=x onerror=window.__x221=1>", "loader": "\\EFI\\sles\\shim.efi",
             "disk": "HD.Emb.1.3"}]


def machine(row, state):
    return {"row": row, "bmc_host": HOSTS[row - 1], "state": state, "source": "hpe-oem",
            "confirm_name": f"USE22{row}", "evidence": EVIDENCE if state == "present" else []}


MIXED = [machine(1, "absent"), machine(2, "present"), machine(3, "present"), machine(4, "unknown")]


def open_batch(context, flask_server, checks=None, batch=None):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    asked = {"check": [], "batch": []}
    checks = list(checks or [MIXED])

    def profiles(route, req):
        url, method = req.url.split("?")[0], req.method
        body = req.post_data_json if method in ("POST", "PUT") else None
        if url.endswith("/profiles") and method == "GET":
            return t180.fulfill(route, t180.LIST)
        if url.endswith("/profiles/rack-a") and method == "GET":
            return t180.fulfill(route, t180.PROFILE)
        if url.endswith("/csv"):
            return t180.fulfill(route, {"rows": ROWS, "passwords_dropped": False})
        if url.endswith("/system-check"):
            asked["check"].append(body)
            return t180.fulfill(route, {"machines": checks.pop(0) if len(checks) > 1 else checks[0]})
        if url.endswith("/batch"):
            asked["batch"].append(body)
            resp, status = batch or ({"action_id": "batch0000221", "cluster": "rack-a", "nodes": 4}, 202)
            return t180.fulfill(route, resp, status)
        return t180.fulfill(route, {"error": "unexpected"}, 500)

    page.route(re.compile(r".*/api/baremetal/profiles.*"), profiles)
    page.route("**/api/isos", lambda r, q: t180.fulfill(r, {"isos": [{"name": "harvester.iso", "size": 1}],
                                                           "disk_free": 1}))
    page.on("dialog", lambda d: d.accept())
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.BMC && window.BMProfiles && window.FloatingPanels && window.i18n")
    page.click('#host-nav [data-hnav-root="automation"][data-hnav-tab="pxe"]')
    page.mouse.move(1100, 450)
    page.locator('#tab-automation [data-bm-tab="profiles"]').click()
    page.locator('#bmp-table tr[data-profile="rack-a"] .bmp-batch').click()
    win = page.locator('[id="fp-bm-batch-rack-a"]')
    win.locator('[name="csv"]').fill("bmc_host,bmc_user,hostname,ip,storage_ip\n")
    win.locator('[data-bmp="load-csv"]').click()
    expect(win.locator(".bmp-nodes tbody tr")).to_have_count(4)
    win.locator('[name="cluster_name"]').fill("rack-a")
    win.locator('[name="vip"]').fill("192.0.2.100")
    win.locator('[name="token"]').fill("TOK-221")
    win.locator('[name="bmc_password"]').fill("BMC-221")
    return page, win, asked


def test_mixed_batch_needs_each_present_name(context, flask_server):
    page, win, asked = open_batch(context, flask_server)
    box = win.locator('[data-bmp="system"]')
    assert win.locator('[data-bmp="system-check"]').get_attribute("data-tip")
    win.locator('[data-bmp="system-check"]').click()
    expect(box.locator(".ct-banner")).to_have_count(4)
    expect(box.locator('[data-sys-row="1"]')).to_have_class(re.compile("bm-system-absent"))
    expect(box.locator('[data-sys-row="1"]')).to_contain_text("Row 1 · 198.51.100.1")
    expect(box.locator('[data-sys-row="1"]')).to_contain_text("No sign of an installed system")
    expect(box.locator('[data-sys-row="4"]')).to_contain_text("Unknown: the BMC does not tell")
    present = box.locator(".bm-system-present")
    expect(present).to_have_count(2)
    expect(present.first).to_contain_text('The machine boots first from "sles-secureboot"')
    expect(present.first.locator(".bm-system-name")).to_have_text("USE222")
    assert page.evaluate("window.__x221") is None                       # signe échappé
    expect(present.first).to_contain_text("<img src=x")
    assert present.first.locator("label.bm-system-confirm").get_attribute("data-tip")
    assert asked["check"][0]["bmc_password"] == "BMC-221" and len(asked["check"][0]["rows"]) == 4
    t180.shot(page, "bm-batch-system-mixed")

    # ligne 2 confirmée, ligne 3 fausse : la série ne part pas
    box.locator('[data-sys-host="198.51.100.2"]').fill("USE222")
    box.locator('[data-sys-host="198.51.100.3"]').fill("use223")
    expect(box.locator('[data-sys-row="3"] .bm-system-mismatch')).to_be_visible()
    expect(box.locator('[data-sys-row="2"] .bm-system-mismatch')).to_be_hidden()
    win.locator('button[type="submit"]').click()
    expect(win.locator(".bmp-msg")).to_contain_text("A system seems present on 1 machine(s)")
    assert asked["batch"] == []
    # nom juste : la série part, le nom avec sa ligne seulement là où un système semble présent
    box.locator('[data-sys-host="198.51.100.3"]').fill(" USE223 ")
    expect(box.locator('[data-sys-row="3"] .bm-system-mismatch')).to_be_hidden()
    win.locator('button[type="submit"]').click()
    expect(win.locator(".bmp-result")).to_contain_text("batch0000221")
    sent = asked["batch"][-1]["rows"]
    assert [r.get("system_confirm") for r in sent] == [None, "USE222", "USE223", None]
    assert len(asked["check"]) == 1                                      # pas relue : rien n'a changé


def test_a_changed_table_is_read_again_before_starting(context, flask_server):
    absent = [machine(i, "absent") for i in (1, 2, 3, 4)]
    page, win, asked = open_batch(context, flask_server, checks=[absent, MIXED])
    win.locator('[data-bmp="system-check"]').click()
    expect(win.locator('[data-bmp="system"] .bm-system-absent')).to_have_count(4)
    win.locator(".bmp-nodes tbody tr").nth(2).locator('[data-col="bmc_user"]').fill("root")
    win.locator('button[type="submit"]').click()
    expect(win.locator(".bmp-msg")).to_contain_text("A system seems present on 2 machine(s)")
    assert len(asked["check"]) == 2 and asked["check"][1]["rows"][2]["bmc_user"] == "root"
    assert asked["batch"] == []


def test_the_server_refusal_is_shown(context, flask_server):
    absent = [machine(i, "absent") for i in (1, 2, 3, 4)]
    refused = [dict(m, confirmed=m["state"] != "present") for m in [machine(1, "absent"), machine(2, "present"),
                                                                      machine(3, "absent"), machine(4, "absent")]]
    page, win, asked = open_batch(context, flask_server, checks=[absent], batch=(
        {"error": "a system seems present on some machines: type each name to start the batch",
         "fields": ["system_confirm"], "refused": [HOSTS[1]], "machines": refused}, 409))
    win.locator('button[type="submit"]').click()                        # lecture faite au départ
    box = win.locator('[data-bmp="system"]')
    expect(box.locator(".bm-system-refused")).to_contain_text("The console refused the batch")
    expect(box.locator(".bm-system-present")).to_have_count(1)
    expect(box.locator('[data-sys-host="198.51.100.2"]')).to_be_visible()
    expect(win.locator(".bmp-msg")).to_contain_text("1 machine(s)")
    assert len(asked["check"]) == 1 and len(asked["batch"]) == 1
    assert all("system_confirm" not in r for r in asked["batch"][0]["rows"])
    t180.shot(page, "bm-batch-system-refused")
    assert "BMC-221" not in page.content()
