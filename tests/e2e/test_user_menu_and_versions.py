"""v1.56.0 : le menu du compte, l'historique des versions et le bandeau des
lectures refusées, dans un navigateur.

La déconnexion d'un compte LOCAL est éprouvée dans un vrai Chromium, avec son
propre serveur et un htpasswd : le navigateur garde le mot de passe d'une
authentification HTTP Basic, et c'est ce cache qu'il faut lui faire oublier.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def fr(context):
    context.add_init_script("localStorage.setItem('harvester_ops_language','fr');")
    return context


def test_the_version_opens_the_version_history(fr, flask_server):
    page = fr.new_page()
    page.goto(flask_server["base_url"] + "/")
    page.wait_for_function("window.Versions && window.i18n")
    btn = page.locator("#btn-version")
    assert btn.get_attribute("data-tip-i18n") == "versions.openTip"
    page.hover("#sidebar")                      # le menu replié s'ouvre au survol
    expect(btn).to_be_visible()
    btn.click()
    modal = page.locator("#versions-modal")
    expect(modal).to_have_class("modal-overlay active")
    first = modal.locator(".version-rel").first
    expect(first).to_be_visible(timeout=8000)
    version = (ROOT / "VERSION").read_text().strip()
    expect(first.locator(".version-num")).to_have_text("v" + version)
    assert first.get_attribute("open") is not None               # la plus récente, dépliée
    total = modal.locator(".version-rel").count()
    assert total > 50
    # « Ajouts » : chaque version montrée n'a plus que ses sections Added
    modal.locator('[data-vkind="added"]').click()
    heads = modal.locator(".version-sec h5").all_text_contents()
    assert heads and all(h.lower().startswith(("added", "new")) for h in heads), heads
    # filtre par mots : les versions qui les contiennent tous
    modal.locator("#versions-filter").fill("kube-vip")
    expect(modal.locator(".version-rel .version-num")).to_contain_text(["v1.52.0"])
    modal.locator("#versions-filter").fill("zzz-nothing-matches")
    expect(modal.locator(".form-hint")).to_contain_text("Aucune version")
    page.keyboard.press("Escape")
    expect(modal).to_have_class("modal-overlay")


def test_the_notes_are_escaped(fr, flask_server):
    """Le CHANGELOG est du texte : échappé avant d'en rendre le gras."""
    page = fr.new_page()
    page.route("**/api/changelog", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"current": "9.9.9", "releases": [{
            "version": "9.9.9", "date": "2030-01-01", "title": "<img src=x onerror=alert(1)>",
            "sections": [{"name": "Added", "items": ["**bold** and `code` <script>x</script>"]}]}]})))
    page.goto(flask_server["base_url"] + "/")
    page.wait_for_function("window.Versions")
    page.evaluate("Versions.open()")
    rel = page.locator("#versions-modal .version-rel").first
    expect(rel).to_be_visible()
    assert rel.locator("img").count() == 0 and rel.locator("script").count() == 0
    expect(rel.locator("strong")).to_have_text("bold")
    expect(rel.locator("code")).to_have_text("code")


def test_refused_reads_are_told_and_grouped(fr, flask_server):
    page = fr.new_page()
    denied = [{"verb": "list", "resource": "virtualmachines", "group": "kubevirt.io"}] + [
        {"verb": "get", "resource": "deployments", "group": "apps", "namespace": ns}
        for ns in ("cert-manager", "capi-system", "caphv-system", "rke2-bootstrap-system")]

    def with_denials(route):
        resp = route.fetch()
        route.fulfill(response=resp, headers={**resp.headers, "x-cluster-denied": json.dumps(denied)})
    page.route("**/api/status/**", with_denials)
    page.goto(flask_server["base_url"] + "/")
    notice = page.locator("#access-notice")
    expect(notice).to_be_visible(timeout=15000)
    expect(notice).to_contain_text("Une partie de cette vue vous est cachée")
    items = notice.locator(".an-list li").all_inner_texts()
    assert "list virtualmachines (kubevirt.io), tout le cluster" in items
    assert "get deployments (apps) dans cert-manager, capi-system, caphv-system et 1 autres" in items
    assert len(items) == 2
    close = notice.locator(".an-close")
    assert close.get_attribute("data-tip")
    close.click()
    expect(notice).to_be_hidden()
    page.wait_for_timeout(9000)                 # l'aperçu se rafraîchit : mêmes refus, bandeau fermé
    expect(notice).to_be_hidden()


# -- déconnexion d'un compte local, dans un vrai navigateur -------------------

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def local_server(tmp_path_factory, test_config):
    from passlib.apache import HtpasswdFile
    tmp = tmp_path_factory.mktemp("local-auth")
    ht = HtpasswdFile(str(tmp / "htpasswd"), new=True)
    ht.set_password("alice", "wonderland")
    ht.save()
    port = _free_port()
    cfg = tmp / "config.yaml"
    cfg.write_text(test_config["config"].read_text().replace("bind_port: 0", f"bind_port: {port}"))
    import re
    cfg.write_text(re.sub(r"bind_port: \d+", f"bind_port: {port}", cfg.read_text()))
    env = {**os.environ, "HARVESTER_OPS_CONFIG": str(cfg), "HARVESTER_OPS_HTPASSWD": str(tmp / "htpasswd"),
           "HARVESTER_OPS_ROLES": str(tmp / "roles.yaml"),
           "HARVESTER_OPS_ACTIONS_DB": str(tmp / "actions.db"), "HARVESTER_OPS_NOTES_DB": str(tmp / "notes.db"),
           "HARVESTER_OPS_LOG_DIR": str(tmp / "logs"), "HARVESTER_OPS_DISABLE_RATELIMIT": "1",
           "HARVESTER_OPS_LOG_LEVEL": "WARNING", "HARVESTER_OPS_VERSION": "test"}
    proc = subprocess.Popen([sys.executable, str(ROOT / "web" / "app.py")], env=env, cwd=str(ROOT / "web"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/healthz", timeout=0.5)
            break
        except Exception:
            time.sleep(0.3)
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_a_local_account_really_signs_out(browser, local_server):
    """Le mot de passe est donné une fois (dans l'adresse, comme le ferait la
    fenêtre du navigateur) ; le navigateur le renvoie ensuite de lui-même.
    Après « Se déconnecter », il ne le renvoie plus : la console redemande."""
    ctx = browser.new_context()
    page = ctx.new_page()
    host = local_server.replace("http://", "")
    # la réponse à l'invite du navigateur : on la rejoue par l'adresse, puis
    # l'on revient sur une adresse sans identifiants (celle de tous les jours)
    page.goto(f"http://alice:wonderland@{host}/login/local")
    page.goto(local_server + "/")
    page.wait_for_function("window.UserMenu && window.i18n")
    r = page.evaluate("fetch('/api/whoami').then(r => r.status)")
    assert r == 200                                   # le navigateur renvoie seul le mot de passe
    page.wait_for_timeout(500)
    page.locator("#btn-user").click()
    expect(page.locator("#user-menu .um-name")).to_have_text("alice")
    signout = page.locator("#user-signout")
    assert signout.get_attribute("data-tip")
    signout.click()
    page.wait_for_url("**/login?signed_out=1")
    expect(page.locator(".login-signed-out")).to_be_visible()
    # le mot de passe est oublié : la console ne s'ouvre plus sans le redonner
    resp = page.goto(local_server + "/")
    assert resp.status == 401
    assert page.evaluate("fetch('/api/whoami').then(r => r.status)") == 401
    ctx.close()
