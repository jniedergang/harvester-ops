"""v1.86.0 : la démo vivante du site, la vraie interface sur la fausse API.

Construite dans un répertoire temporaire et servie en statique, sans
console : rien ne doit partir vers un serveur, les gestes changent l'état de
la démo, l'historique est daté d'aujourd'hui, l'ancre choisit l'onglet."""

import re
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "demo-site"))
import build  # noqa: E402
import shots  # noqa: E402


@pytest.fixture(scope="module")
def demo_url(tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    build.build_demo(out)
    srv = shots.serve(out)
    yield f"http://127.0.0.1:{srv.server_address[1]}/demo/"
    srv.shutdown()


def open_demo(context, url, hash_="", lang="en"):
    page = context.new_page()
    errors, api = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: api.append(r.url) if "/api/" in r.url else None)
    page.goto(f"{url}?lang={lang}{hash_}", wait_until="domcontentloaded")
    page.wait_for_function("window.App && App.getCurrentCluster && App.getCurrentCluster()")
    page.mouse.move(1500, 900)
    return page, errors, api


def test_the_demo_opens_on_a_live_cluster_without_any_server(context, demo_url):
    page, errors, api = open_demo(context, demo_url, "#overview", lang="fr")
    expect(page.locator("#demo-banner")).to_contain_text("Démo vivante")
    expect(page.locator("#demo-banner a")).to_have_attribute("href", "../fr/")
    assert page.evaluate("App.getCurrentCluster()") == "lyon-lab"
    page.wait_for_timeout(1500)
    assert errors == [] and api == []          # tout est répondu dans la page


def test_stopping_and_starting_a_vm_changes_it_and_shows_in_the_dock(context, demo_url):
    page, errors, _ = open_demo(context, demo_url, "#allvms")
    row = page.locator("#all-vms-host tbody tr", has_text="api-01")
    expect(row.locator(".phase")).to_have_text("Running")
    page.on("dialog", lambda d: d.accept())
    row.locator('[data-avm-act="stop"]').click()
    page.locator('[data-avm="refresh"]').click()
    expect(row.locator(".phase")).to_have_text("Stopped")
    expect(page.locator("#dock-list")).to_contain_text("api-01")
    row.locator('[data-avm-act="start"]').click()
    page.locator('[data-avm="refresh"]').click()
    expect(row.locator(".phase")).to_have_text("Starting")
    page.wait_for_timeout(6500)
    page.locator('[data-avm="refresh"]').click()
    expect(row.locator(".phase")).to_have_text("Running")
    assert errors == []


def test_the_history_is_seeded_and_dated_today(context, demo_url):
    page, errors, _ = open_demo(context, demo_url, "#activity")
    body = page.locator("#tab-activity")
    expect(body).to_contain_text("upgrade")
    expect(body).to_contain_text("baremetal-batch")
    started = page.evaluate("async () => (await (await fetch('/api/activity')).json()).actions_done.map(a => a.started_at)")
    assert started and max(started) > time.time() - 3600 and min(started) > time.time() - 8 * 86400
    page.select_option("#tab-activity select >> nth=1", "failed")
    expect(body).to_contain_text("vm-import")
    assert errors == []


def test_the_anchor_picks_the_tab(context, demo_url):
    page, _, _ = open_demo(context, demo_url, "#storage")
    expect(page.locator("#tab-storage")).to_have_class(re.compile("active"))
