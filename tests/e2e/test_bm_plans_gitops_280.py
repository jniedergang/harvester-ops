"""2.8.0 (tâche 9) : phase GitOps des plans dans l'interface (routes simulées).

* carte GitOps : délai, entrées (nom, outil, clusters, namespace visé,
  source artefact de paquet, Git ou chart, droits, intervalle, élagage),
  document enregistré à la forme de deploy_plans, refus AVANT l'envoi ;
* outil déduit des modèles de cluster du plan (import Rancher externe =>
  Fleet ; cluster hors du plan : décidé au contrôle) ;
* fenêtre Déployer : secrets Git demandés par la phase, envoyés dans
  `gitops_secrets` du contrôle et du lancement, jamais réaffichés ensuite ;
  tableau GitOps du résumé ; constats GitOps nommés ;
* noms hostiles échappés, clés non éditées d'une entrée gardées.
"""

import copy
import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

import test_bm_plans_capi_250 as t250  # noqa: E402
from test_bm_profiles_180 import shot  # noqa: E402

EVIL = '<img src=x onerror="window.__gop280=1">'
fulfill = t250.fulfill
ITEMS = [{"name": "podinfo", "kind": "chart"}, {"name": "apps", "kind": "gitops"},
         {"name": "net-pol", "kind": "manifest"}]
PHASES = ["readiness", "terraform", "registry", "capi", "clusters", "artefacts", "gitops"]
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nSECRET-KEY-280\n-----END OPENSSH PRIVATE KEY-----"


@pytest.fixture
def lists(monkeypatch):
    lst = copy.deepcopy(t250.LIST)
    lst["phases"] = PHASES
    lst["available"] = PHASES
    monkeypatch.setattr(t250, "LIST", lst)
    return lst


def open_page(context, flask_server, check=None, imports=True):
    context.route(re.compile(r".*/api/registries$"), lambda r, q: fulfill(r, {"registries": [
        {"name": "client", "kind": "external", "endpoint": "h:443", "clusters": ["harv-fake"]}]}))
    context.route(re.compile(r".*/api/artefacts$"), lambda r, q: fulfill(r, {"packages": [
        {"name": "socle", "version": "2026.11", "items": 3, "trust": "valid"}]}))
    context.route(re.compile(r".*/api/artefacts/socle/2026\.11$"), lambda r, q: fulfill(r, {
        "name": "socle", "version": "2026.11", "index": {"name": "socle", "version": "2026.11", "items": ITEMS},
        "images": [], "trust": {"state": "valid"}, "published": []}))
    context.route(re.compile(r".*/api/baremetal/capi-templates/rke2-small$"), lambda r, q: fulfill(r, {
        "name": "rke2-small", "revision": 3, "spec": {"rancher_external": imports}}))
    return t250.open_page(context, flask_server, "#automation/pxe/plans", check=check)


def test_gitops_card_saves_the_phase_and_shows_the_deduced_tool(context, flask_server, lists):
    page, st = open_page(context, flask_server)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    form = page.locator("#bm-plans .bmpl-form")
    card = form.locator('.bmpl-phase[data-phase="gitops"]')
    expect(card.locator(".bmpl-coming")).to_have_count(0)
    card.locator(".bmpl-phase-on").check()
    expect(card.locator('[name="gitops_timeout"]')).to_have_value("15")
    expect(card.locator(".bmpl-gop-clusters-hint")).to_contain_text("{{cluster}}-apps")
    card.locator(".bmpl-gop-add").click()
    row = card.locator(".bmpl-gop").first
    # le cluster du plan vient d'un modèle qui demande l'import : Fleet
    expect(row.locator('[data-k="clusters"]')).to_have_value("{{cluster}}-apps")
    expect(row.locator(".bmpl-gop-tool")).to_have_attribute("data-tool", "fleet")
    # sans nom : refusé avant l'envoi
    form.locator(".bmpl-save").click()
    expect(form.locator(".bmpl-gop-refusal")).to_contain_text("needs a name")
    assert st["put"] == []

    def fill(k, v):
        el = card.locator(f'.bmpl-gop .bmpl-gop-f[data-k="{k}"]').first
        el.fill(v)
        el.dispatch_event("change")
    fill("name", "apps")
    fill("target_namespace", "equipe-a")
    # artefact de paquet : seuls les éléments gitops sont proposés
    card.locator(".bmpl-gop-pkg").select_option("socle@2026.11")
    item = card.locator('.bmpl-gop .bmpl-gop-f[data-k="item"]')
    expect(item.locator("option")).to_have_count(1)
    expect(item).to_have_value("apps")
    form.locator(".bmpl-save").click()
    expect(form.locator(".bmpl-gop-refusal")).to_contain_text("registry")
    assert st["put"] == []
    fill("registry", "client")
    card.locator('.bmpl-gop .bmpl-gop-f[data-k="tool"]').select_option("flux")
    expect(card.locator(".bmpl-gop-tool")).to_have_attribute("data-tool", "flux")
    card.locator('.bmpl-gop .bmpl-gop-f[data-k="tool"]').select_option("auto")
    fill("interval", "30")
    card.locator('.bmpl-gop .bmpl-gop-f[data-k="prune"]').uncheck()
    # seconde entrée, Git, sur un cluster hors du plan : outil décidé au contrôle
    card.locator(".bmpl-gop-add").click()
    second = card.locator(".bmpl-gop").nth(1)

    def fill2(k, v):
        el = second.locator(f'.bmpl-gop-f[data-k="{k}"]')
        el.fill(v)
        el.dispatch_event("change")
    fill2("name", "infra")
    fill2("clusters", "outils/outils")
    expect(second.locator(".bmpl-gop-tool")).to_have_attribute("data-tool", "?")
    expect(second.locator(".bmpl-gop-tool")).to_contain_text("decided at check")
    fill2("target_namespace", "infra")
    second.locator('.bmpl-gop-f[data-k="stype"]').select_option("git")
    second = card.locator(".bmpl-gop").nth(1)
    fill2("url", "ssh://git@git.example:22/team/infra.git")
    fill2("secret", "deploy-key")
    for el in card.locator("button, select, input, textarea").all():
        if el.is_visible():
            assert el.get_attribute("data-tip") or el.evaluate("e => !!e.closest('.tip')"), el.evaluate("e => e.outerHTML")
    card.scroll_into_view_if_needed()
    shot(page, "bm-plans-gitops")
    form.locator(".bmpl-save").click()
    expect(form.locator(".bmpl-msg .bm-imported")).to_be_visible()
    assert st["put"][-1]["phases"]["gitops"] == {
        "enabled": True, "timeout_minutes": 15, "entries": [
            {"name": "apps", "tool": "auto", "clusters": ["{{cluster}}-apps"], "target_namespace": "equipe-a",
             "source": {"type": "oci", "package": "socle", "version": "2026.11", "item": "apps", "registry": "client"},
             "privileges": "namespace", "interval_minutes": 30, "prune": False},
            {"name": "infra", "tool": "auto", "clusters": ["outils/outils"], "target_namespace": "infra",
             "source": {"type": "git", "url": "ssh://git@git.example:22/team/infra.git", "secret": "deploy-key"},
             "privileges": "namespace", "interval_minutes": 10, "prune": True}]}
    assert st["errors"] == []


def test_template_without_import_gives_flux(context, flask_server, lists):
    page, st = open_page(context, flask_server, imports=False)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    card = page.locator('#bm-plans .bmpl-phase[data-phase="gitops"]')
    card.locator(".bmpl-phase-on").check()
    card.locator(".bmpl-gop-add").click()
    expect(card.locator(".bmpl-gop-tool")).to_have_attribute("data-tool", "flux")
    assert st["errors"] == []


def test_loaded_entry_hostile_names_escaped_and_kept(context, flask_server, lists, monkeypatch):
    plan = copy.deepcopy(t250.PLAN)
    plan["phases"]["gitops"] = {"enabled": True, "timeout_minutes": 20, "entries": [
        {"name": "apps", "tool": "fleet", "clusters": [EVIL], "target_namespace": "equipe-a",
         "source": {"type": "chart", "package": EVIL, "version": "1.0.0", "item": EVIL, "registry": EVIL},
         "privileges": "cluster", "interval_minutes": 5, "prune": True}]}
    monkeypatch.setattr(t250, "PLAN", plan)
    page, st = open_page(context, flask_server)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    card = page.locator('#bm-plans .bmpl-phase[data-phase="gitops"]')
    expect(card.locator(".bmpl-gop-pkg option").first).to_contain_text(EVIL)
    expect(card.locator('.bmpl-gop-f[data-k="clusters"]')).to_have_value(EVIL)
    expect(card.locator(".bmpl-gop-tool")).to_have_attribute("data-tool", "fleet")
    page.locator("#bm-plans .bmpl-save").click()
    expect(page.locator("#bm-plans .bmpl-msg .bm-imported")).to_be_visible()
    assert st["put"][-1]["phases"]["gitops"]["entries"] == plan["phases"]["gitops"]["entries"]
    assert st["put"][-1]["phases"]["gitops"]["timeout_minutes"] == 20
    assert page.evaluate("window.__gop280") is None
    assert st["errors"] == []


def test_deploy_window_asks_the_git_secrets_and_never_shows_them_again(context, flask_server, lists, monkeypatch):
    plan = copy.deepcopy(t250.PLAN)
    plan["phases"]["gitops"] = {"enabled": True, "timeout_minutes": 15, "entries": [
        {"name": "infra", "tool": "auto", "clusters": ["outils/outils"], "target_namespace": "infra",
         "source": {"type": "git", "url": "ssh://git@g/x.git", "secret": "deploy-key"},
         "privileges": "namespace", "interval_minutes": 10, "prune": True}]}
    monkeypatch.setattr(t250, "PLAN", plan)
    check = {"findings": [
        {"code": "gitops-flux-in-plan", "level": "info", "phase": "gitops", "detail": "infra on outils/outils"},
        {"code": "gitops-fleet-not-confined", "level": "warning", "phase": "gitops", "detail": EVIL}],
        "summary": {"declarations": [], "estimate_minutes": 4, "capi": None, "clusters": [], "artefacts": [],
                    "gitops": [{"id": "gitops-outils-infra", "ref": "outils/outils", "entry": EVIL, "tool": None,
                                "source": "git"},
                               {"id": "gitops-a-apps", "ref": "a/a", "entry": "apps", "tool": "fleet",
                                "source": "chart"}]},
        "blocking": False}
    page, st = open_page(context, flask_server, check=check)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    page.locator("#bm-plans .bmpl-apply").click()
    win = page.locator("#fp-deploy-plan")
    win.locator('[name="cluster"]').select_option(t250.C)
    box = win.locator(".dpw-gop-secrets")
    expect(box).to_be_visible()
    sec = box.locator('.dpw-gop-secret[data-gop-secret="deploy-key"]')
    expect(sec).to_have_count(1)
    for el in box.locator("select, input, textarea").all():
        if el.is_visible():
            assert el.evaluate("e => !!e.closest('.tip')"), el.evaluate("e => e.outerHTML")
    sec.locator('[data-gop-k="identity"]').fill(KEY)
    sec.locator('[data-gop-k="known_hosts"]').fill("g ssh-ed25519 AAAA")
    expect(win.locator(".dpw-launch")).to_be_enabled(timeout=5000)
    page.wait_for_timeout(600)
    assert st["check"][-1]["gitops_secrets"] == {"deploy-key": {"identity": KEY, "known_hosts": "g ssh-ed25519 AAAA"}}
    shot(page, "bm-plans-gitops-deploy")
    win.locator('[data-dpw-tab="checks"]').click()
    group = win.locator('.dpw-group[data-phase="gitops"]')
    expect(group).to_contain_text("installed by the Artefacts phase")
    expect(group).to_contain_text("does not keep an entry to its namespace")
    win.locator('[data-dpw-tab="summary"]').click()
    rows = win.locator(".dpw-gitops tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.first.locator(".dpw-gitops-entry")).to_have_text(EVIL)
    expect(rows.first.locator(".dpw-gitops-later")).to_have_text("decided at check")
    expect(rows.nth(1)).to_contain_text("Fleet")
    win.locator(".dpw-launch").click()
    expect(win.locator(".dpw-follow")).to_be_visible()
    assert st["deploy"][-1]["gitops_secrets"] == {"deploy-key": {"identity": KEY, "known_hosts": "g ssh-ed25519 AAAA"}}
    # jamais réaffichés après le départ
    expect(sec.locator('[data-gop-k="identity"]')).to_have_value("")
    assert "SECRET-KEY-280" not in win.evaluate(
        "w => [...w.querySelectorAll('textarea, input')].map(e => e.value).join(' ') + w.innerHTML")
    assert page.evaluate("window.__gop280") is None
    assert st["errors"] == []


def test_basic_kind_needs_both_fields(context, flask_server, lists, monkeypatch):
    plan = copy.deepcopy(t250.PLAN)
    plan["phases"]["gitops"] = {"enabled": True, "timeout_minutes": 15, "entries": [
        {"name": "infra", "tool": "auto", "clusters": ["outils/outils"], "target_namespace": "infra",
         "source": {"type": "git", "url": "https://g/x.git", "secret": "creds"},
         "privileges": "namespace", "interval_minutes": 10, "prune": True}]}
    monkeypatch.setattr(t250, "PLAN", plan)
    check = {"findings": [], "summary": {"declarations": [], "estimate_minutes": 1}, "blocking": False}
    page, st = open_page(context, flask_server, check=check)
    page.locator('.bmpl-card[data-plan="site-k8s"]').click()
    page.locator("#bm-plans .bmpl-apply").click()
    win = page.locator("#fp-deploy-plan")
    win.locator('[name="cluster"]').select_option(t250.C)
    sec = win.locator('.dpw-gop-secret[data-gop-secret="creds"]')
    sec.locator('[data-gop-k="kind"]').select_option("basic")
    expect(sec.locator('[data-gop-k="identity"]')).to_be_hidden()
    sec.locator('[data-gop-k="username"]').fill("reader")
    page.wait_for_timeout(700)
    assert "gitops_secrets" not in st["check"][-1]                  # incomplet : non envoyé
    sec.locator('[data-gop-k="password"]').fill("tok-280")
    page.wait_for_timeout(700)
    assert st["check"][-1]["gitops_secrets"] == {"creds": {"username": "reader", "password": "tok-280"}}
    assert st["errors"] == []


def test_undo_window_groups_gitops_and_rancher_import_in_removal_order(context, flask_server, monkeypatch):
    import test_deploy_undo_270 as t270
    items = [
        {"id": "rancher-import-a/a", "kind": "rancher-import", "ref": "a/a", "phase": "rancher-import",
         "run": t270.RUN["id"], "proven": True, "default": True, "note": "", "selected": True,
         "details": {"rancher": "https://rancher.example", "name": EVIL, "present": True,
                     "delete": [{"kind": "ClusterRole", "name": "cattle-admin"}], "kept": [], "refused": []}},
        {"id": "gitops-a/a/apps", "kind": "gitops", "ref": "a/a/apps", "phase": "gitops", "run": t270.RUN["id"],
         "proven": True, "default": True, "note": "", "selected": True,
         "details": {"present": True, "tool": "flux", "name": EVIL}},
    ]
    monkeypatch.setattr(t270, "ITEMS", items)
    page, st = t270.open_page(context, flask_server)
    t270.row(page, t270.RUN["id"]).locator(".bm-deploy-undo").click()
    win = page.locator(f'#fp-deploy-undo-{t270.RUN["id"]}')
    groups = win.locator(".udw-group")
    expect(groups).to_have_count(2)
    assert [g.get_attribute("data-kind") for g in groups.all()] == ["gitops", "rancher-import"]
    expect(groups.first).to_contain_text("GitOps entries")
    expect(win.locator(".udw-gitops")).to_contain_text(EVIL)
    expect(win.locator(".udw-rancher")).to_contain_text("https://rancher.example")
    expect(win.locator(".udw-delete")).to_contain_text("cattle-admin")
    assert page.evaluate("window.__gop280") is None
    assert st["errors"] == []


def test_batch_window_asks_the_git_secrets_of_the_chained_plan(context, flask_server, monkeypatch):
    """Revue 5-8 (I-10) : une série enchaînée à un plan dont la phase GitOps
    a une entrée Git avec secret demande ce secret ; il part dans
    `gitops_secrets` de la série, puis les champs sont vidés."""
    import test_bm_plans_240 as t240
    import test_bm_profiles_180 as t180
    plan = copy.deepcopy(t240.PLAN)
    plan["phases"]["gitops"] = {"enabled": True, "timeout_minutes": 10, "entries": [
        {"name": "apps", "clusters": ["{{cluster}}-apps"], "target_namespace": "equipe",
         "source": {"type": "git", "url": "ssh://git@h/x.git", "branch": "main", "secret": "depot"}}]}
    monkeypatch.setattr(t240, "PLAN", plan)
    page, st = t240.open_page(context, flask_server, "#automation/pxe/profiles")
    batches = []
    prof = dict(t180.PROFILE, plan="site-std")

    def profiles(route, req):
        url, method = req.url.split("?")[0], req.method
        if url.endswith("/profiles") and method == "GET":
            return fulfill(route, t180.LIST)
        if url.endswith("/profiles/rack-a") and method == "GET":
            return fulfill(route, prof)
        if url.endswith("/system-check"):
            return fulfill(route, {"machines": [{"row": 1, "bmc_host": "198.51.100.1", "state": "absent",
                                                 "evidence": [], "confirm_name": "x", "source": "hpe-oem"}]})
        if url.endswith("/batch"):
            batches.append(req.post_data_json)
            return fulfill(route, {"action_id": "dep000000280", "cluster": "rack-a", "nodes": 1, "plan": "site-std"},
                           202)
        return fulfill(route, {"error": "unexpected"}, 500)
    page.route(re.compile(r".*/api/baremetal/profiles.*"), profiles)
    page.route("**/api/isos", lambda r, q: fulfill(r, {"isos": [{"name": "harvester.iso", "size": 1}], "disk_free": 1}))
    page.locator('#tab-automation [data-bm-tab="images"]').click()
    page.locator('#tab-automation [data-bm-tab="profiles"]').click()
    page.locator('#bmp-table tr[data-profile="rack-a"] .bmp-batch').click()
    win = page.locator('[id="fp-bm-batch-rack-a"]')
    block = win.locator('.bmpl-pick-gop .dpw-gop-secret[data-gop-secret="depot"]')
    expect(block).to_be_visible(timeout=5000)
    assert all(t.get_attribute("data-tip") for t in win.locator(".bmpl-pick-gop .tip").all())
    block.locator('[data-gop-k="identity"]').fill(KEY)
    win.locator('.bmp-plan-pick [data-pp-var="vlan"]').fill("3999")
    row = win.locator(".bmp-nodes tbody tr").first
    row.locator('[data-col="bmc_host"]').fill("198.51.100.1")
    for k, v in {"hostname": "node1", "ip": "192.0.2.11", "storage_ip": "172.18.122.101"}.items():
        row.locator(f'[data-var="{k}"]').fill(v)
    win.locator('[name="cluster_name"]').fill("rack-a")
    win.locator('[name="vip"]').fill("192.0.2.100")
    win.locator('[name="token"]').fill("TOK")
    win.locator('[name="bmc_password"]').fill("BMC")
    win.locator('button[type="submit"]').click()
    expect(win.locator(".bmp-result a.dpw-action-link")).to_have_attribute("href", "#activity/action/dep000000280")
    assert batches[0]["gitops_secrets"] == {"depot": {"identity": KEY}}
    expect(block.locator('[data-gop-k="identity"]')).to_have_value("")          # jamais gardé
    assert not st["errors"]
