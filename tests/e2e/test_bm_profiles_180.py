"""v1.80.0 : profils d'installation multi-nœuds dans l'onglet Bare-metal.

Les routes des profils sont simulées : la liste, l'éditeur (enregistrement
en YAML), la fenêtre de série (CSV, aperçu par ligne, lancement). Aucune
installation ne part ; on vérifie ce que la fenêtre envoie, et que les mots
de passe ne passent pas par le CSV ni l'aperçu."""

import json
import os
import re
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

PROFILE = {"name": "rack-a", "description": "rack A", "variables": ["storage_ip"],
           "fields": {"iso": "harvester.iso", "hostname": "{{hostname}}", "ip": "{{ip}}"},
           "fields_yaml": "iso: harvester.iso\nhostname: '{{hostname}}'\nip: '{{ip}}'\n",
           "advanced_yaml": "os:\n  write_files:\n  - path: /x\n    content: |\n      address1={{storage_ip}}/24\n"}
LIST = {"profiles": [{"name": "rack-a", "description": "rack A", "variables": ["storage_ip"],
                      "uses": ["hostname", "ip", "storage_ip"], "updated": 1}],
        "builtins": ["hostname", "ip", "mgmt_mac", "vip"]}
CSV = ("bmc_host,bmc_user,bmc_password,hostname,ip,storage_ip\n"
       "198.51.100.1,admin,PWINCSV,node1,192.0.2.11,172.18.122.101\n"
       "198.51.100.2,admin,PWINCSV,node2,192.0.2.12,172.18.122.102\n")


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def shot(page, name):
    """Capture facultative (E2E_SHOT_DIR), pour relire la fenêtre à l'œil."""
    d = os.environ.get("E2E_SHOT_DIR")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(d) / f"{name}.png"))


def open_tab(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    calls = []

    def profiles(route, req):
        url, method = req.url.split("?")[0], req.method
        body = req.post_data_json if method in ("POST", "PUT") else None
        calls.append((method, url.rsplit("/api/baremetal/", 1)[1], body))
        if url.endswith("/profiles") and method == "GET":
            return fulfill(route, LIST)
        if url.endswith("/profiles") and method == "POST":
            return fulfill(route, dict(PROFILE, name=body["name"]), 201)
        m = re.search(r"/profiles/([a-z0-9-]+)$", url)
        if m and method == "GET" and m.group(1) != "from-config":
            return fulfill(route, dict(PROFILE, name=m.group(1)))
        if url.endswith("/profiles/rack-a") and method == "PUT":
            return fulfill(route, PROFILE)
        if url.endswith("/csv"):
            return fulfill(route, {"rows": [
                {"bmc_host": "198.51.100.1", "bmc_user": "admin",
                 "values": {"hostname": "node1", "ip": "192.0.2.11", "storage_ip": "172.18.122.101"}},
                {"bmc_host": "198.51.100.2", "bmc_user": "admin",
                 "values": {"hostname": "node2", "ip": "192.0.2.12", "storage_ip": "172.18.122.102"}}],
                "passwords_dropped": True})
        if url.endswith("/render"):
            return fulfill(route, {"ok": False, "rows": [
                {"row": 1, "hostname": "node1", "mode": "create", "yaml": "token: •••\nos:\n  hostname: node1\n"},
                {"row": 2, "errors": [["row 2 {{storage_ip}}", "missing variable"]]}]})
        if url.endswith("/batch"):
            return fulfill(route, {"action_id": "batch0000180", "cluster": "rack-a", "nodes": 2}, 202)
        return fulfill(route, {"error": "unexpected"}, 500)

    page.route(re.compile(r".*/api/baremetal/profiles.*"), profiles)
    page.route("**/api/isos", lambda r, q: fulfill(r, {"isos": [{"name": "harvester.iso", "size": 1}],
                                                      "disk_free": 1}))
    page.on("dialog", lambda d: d.accept())
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_function("window.BMC && window.BMProfiles && window.FloatingPanels && window.i18n")
    page.click('.tab-group-head[data-group="automation"]')
    page.click('.tab-child[data-subtab="pxe"]')
    page.mouse.move(1100, 450)
    expect(page.locator('#bmp-table tr[data-profile="rack-a"]')).to_be_visible()
    return page, calls


def test_profile_list_and_editor(context, flask_server):
    page, calls = open_tab(context, flask_server)
    row = page.locator('#bmp-table tr[data-profile="rack-a"]')
    expect(row).to_contain_text("{{storage_ip}}")
    for sel in (".bmp-batch", ".bmp-edit", ".bmp-del"):
        assert row.locator(sel).get_attribute("data-tip")
    # éditeur d'un profil existant : YAML en texte, nom figé
    row.locator(".bmp-edit").click()
    win = page.locator('[id="fp-bm-profile-rack-a"]')
    expect(win.locator('[name="fields_yaml"]')).to_have_value(PROFILE["fields_yaml"])
    assert win.locator('[name="name"]').get_attribute("readonly") is not None
    win.locator('[name="description"]').fill("rack B")
    win.locator('button[type="submit"]').click()
    expect(win.locator(".bmp-msg")).to_contain_text("Profile saved")
    shot(page, "bm-profile-editor")
    put = [c for c in calls if c[0] == "PUT"][-1]
    assert put[2]["description"] == "rack B" and put[2]["fields_yaml"] == PROFILE["fields_yaml"]
    # nouveau profil : un gabarit à variables
    page.locator(".bmp-new").click()
    new = page.locator('[id="fp-bm-profile-new"]')
    assert "{{hostname}}" in new.locator('[name="fields_yaml"]').input_value()
    new.locator('[name="name"]').fill("rack-c")
    new.locator('[name="variables"]').fill("storage_ip")
    new.locator('button[type="submit"]').click()
    expect(page.locator('[id="fp-bm-profile-rack-c"]')).to_be_visible()
    post = [c for c in calls if c[0] == "POST" and c[1] == "profiles"][-1]
    assert post[2]["name"] == "rack-c" and post[2]["variables"] == "storage_ip"


def test_batch_window_csv_preview_and_start(context, flask_server):
    page, calls = open_tab(context, flask_server)
    page.locator('#bmp-table tr[data-profile="rack-a"] .bmp-batch').click()
    win = page.locator('[id="fp-bm-batch-rack-a"]')
    heads = win.locator(".bmp-nodes thead th")
    expect(heads).to_contain_text(["#", "bmc_host", "bmc_user", "bmc_password", "{{hostname}}",
                                   "{{ip}}", "{{storage_ip}}"])
    win.locator('[name="csv"]').fill(CSV)
    win.locator('[data-bmp="load-csv"]').click()
    expect(win.locator(".bmp-nodes tbody tr")).to_have_count(2)
    expect(win.locator(".bmp-msg")).to_contain_text("2 machine(s) read")
    expect(win.locator(".bmp-msg")).to_contain_text("bmc_password column was ignored")
    win.locator('[name="cluster_name"]').fill("rack-a")
    win.locator('[name="vip"]').fill("192.0.2.100")
    win.locator('[data-bmp="preview"]').click()
    expect(win.locator(".bmp-row-preview")).to_have_count(1)
    expect(win.locator(".bmp-check .bm-refusal")).to_have_count(1)
    assert "missing variable" in win.locator(".bmp-check .bm-refusal").get_attribute("data-tip")
    render = [c for c in calls if c[1].endswith("/render")][-1][2]
    assert all("bmc_password" not in r for r in render["rows"])
    shot(page, "bm-batch-preview")
    win.locator(".bmp-row-preview").click()
    expect(page.locator('[id="fp-bm-batch-preview-rack-a-1"] .bm-preview-text')).to_have_value(
        "token: •••\nos:\n  hostname: node1\n")
    # lancement : secrets saisis, mot de passe propre à la ligne 2
    win.locator('[name="token"]').fill("TOKSECRET")
    win.locator('[name="bmc_password"]').fill("COMMONPW")
    win.locator('.bmp-nodes tbody tr').nth(1).locator('[data-col="bmc_password"]').fill("ROW2PW")
    win.locator('button[type="submit"]').click()
    expect(win.locator(".bmp-result")).to_contain_text("batch0000180")
    body = [c for c in calls if c[1].endswith("/batch")][-1][2]
    assert body["token"] == "TOKSECRET" and body["bmc_password"] == "COMMONPW"
    assert [r["bmc_password"] for r in body["rows"]] == ["", "ROW2PW"]
    assert body["rows"][1]["values"] == {"hostname": "node2", "ip": "192.0.2.12",
                                         "storage_ip": "172.18.122.102"}
    assert body["cluster_name"] == "rack-a" and body["vip"] == "192.0.2.100"
    assert body["concurrency"] == 2
    assert "PWINCSV" not in page.content()
