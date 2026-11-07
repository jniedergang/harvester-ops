"""2.2.0 : Terraform et Cluster API en onglets.

* onglets par défaut (Clusters, Déclarations), anciennes adresses
  #automation/capi et #automation/terraform ;
* un test par onglet : contenu, adresse écrite sans entrée d'historique, F5 ;
* la bande suit le sous-onglet d'Automatisation ; l'en-tête « Cluster de
  gestion » reste sur Cluster API et Terraform ;
* bandeaux « vérifier avant d'agir » (provider ou OpenTofu absent, pile
  Cluster API absente) et leur lien vers Installation ;
* Historique = Activité filtrée (requêtes, préfixes exacts, liens de
  journal, erreur, vide), relecture arrêtée quand on quitte l'écran ;
* mutations volontaires.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nav import goto, settle  # noqa: E402

INFO_OK = {"terraform_available": True, "terraform_flavor": "opentofu", "terraform_bin": "/usr/bin/tofu",
           "provider_binary": "/opt/harvester-ops/terraform/terraform-provider-harvester", "provider_version": "1.8.0",
           "provider_binary_size": 50 * 2 ** 20, "provider_arch": "linux_amd64", "provider_origin": "bundled",
           "provider_can_install": True, "provider_searched": [], "provider_env": "HARVESTER_OPS_TF_PROVIDER",
           "workspaces_dir": "/var/lib/harvester-ops/terraform", "example_resources": ["harvester_image"]}
STACK_READY = {"turtles": True, "core": {"type": "core", "name": "cluster-api", "version": "v1.10.0", "ready": True},
               "providers": [], "missing": [], "ready": True, "harvester_version": "v1.9.0", "bundle": {}}
STACK_MISSING = dict(STACK_READY, ready=False, missing=["infrastructure/harvester"])
SERVICES = {"caaph": True, "clusters": [], "catalog": [], "services": []}
CAPI_TABS = ["clusters", "services", "registries", "artefacts", "history", "installation"]
TF_TABS = ["declarations", "resources", "history", "installation"]


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def open_auto(context, flask_server, hash, info=INFO_OK, stack=STACK_READY, diag=None):
    """Automatisation à froid ; ce que lisent Terraform et Cluster API est
    simulé (aucun cluster n'est interrogé)."""
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    page.route("**/api/terraform/info", lambda r, q: fulfill(r, info))
    page.route("**/api/terraform/harv-fake/state", lambda r, q: fulfill(r, {
        "initialized": True, "resources": [], "resources_detail": [], "resource_count": 0}))
    page.route(re.compile(r".*/api/capi/harv-fake/stack.*"),
               lambda r, q: fulfill(r, stack) if isinstance(stack, dict) else fulfill(r, {"error": "x"}, stack))
    page.route(re.compile(r".*/api/capi/harv-fake/diag.*"),
               lambda r, q: fulfill(r, diag or {"capi_clusters": [], "components": []}))
    page.route(re.compile(r".*/api/capi/harv-fake/services.*"), lambda r, q: fulfill(r, SERVICES))
    page.route(re.compile(r".*/api/capi/bundles.*"), lambda r, q: fulfill(r, {"bundles": []}))
    page.on("dialog", lambda d: d.dismiss())
    goto(page, flask_server["base_url"], hash)
    page.mouse.move(1100, 450)
    return page


def capi_visible(page):
    return page.evaluate("""() => [...document.querySelectorAll('[data-subtab="capi"] .capi-tab-content.active')]
        .map(p => p.dataset.capiTab)""")


def tf_visible(page):
    return page.evaluate("""() => [...document.querySelectorAll('#tf-status-body [data-tf-pane]')]
        .filter(p => !p.hidden).map(p => p.dataset.tfPane)""")


# --- onglets par défaut et anciennes adresses ------------------------------------------

@pytest.mark.parametrize("hash", ["#automation/capi", "#automation/capi/clusters"])
def test_capi_default_tab_is_clusters_and_the_legacy_address_opens_it(context, flask_server, hash):
    page = open_auto(context, flask_server, hash)
    btn = page.locator('#tab-automation .sub-tab[data-capi-tab="clusters"]')
    expect(btn).to_have_class(re.compile(r"\bactive\b"))
    assert btn.get_attribute("aria-selected") == "true"
    assert capi_visible(page) == ["clusters"]
    expect(page.locator("#btn-capi-create")).to_be_visible()
    expect(page.locator('#tab-automation .sub-tabs-inline[data-only-for="capi"]')).to_be_visible()
    expect(page.locator('#tab-automation .sub-tabs-inline[data-only-for="terraform"]')).to_be_hidden()
    # l'en-tête « Cluster de gestion » de la 2.0 reste
    expect(page.locator("#mgmt-cluster-bar")).to_be_visible()


@pytest.mark.parametrize("hash", ["#automation/terraform", "#automation/terraform/declarations"])
def test_terraform_default_tab_is_declarations_and_the_legacy_address_opens_it(context, flask_server, hash):
    page = open_auto(context, flask_server, hash)
    expect(page.locator("#tf-decls-view")).to_be_visible()
    btn = page.locator('#tab-automation .sub-tab[data-tf-tab="declarations"]')
    expect(btn).to_have_class(re.compile(r"\bactive\b"))
    assert btn.get_attribute("aria-selected") == "true"
    assert tf_visible(page) == ["declarations"]
    expect(page.locator('#tab-automation .sub-tabs-inline[data-only-for="terraform"]')).to_be_visible()
    expect(page.locator('#tab-automation .sub-tabs-inline[data-only-for="capi"]')).to_be_hidden()
    expect(page.locator("#mgmt-cluster-bar")).to_be_visible()


# --- un test par onglet -----------------------------------------------------------------

@pytest.mark.parametrize("tab,content", [
    ("services", "#capi-services-body .svc-body"),
    ("history", "#capi-history-body .form-hint"),
    ("installation", "#capi-status-body h4"),
    ("clusters", "#btn-capi-create"),
])
def test_each_capi_tab_shows_its_content_and_writes_its_address(context, flask_server, tab, content):
    start = "#automation/capi/installation" if tab == "clusters" else "#automation/capi"
    page = open_auto(context, flask_server, start)
    n = page.evaluate("history.length")
    btn = page.locator(f'#tab-automation .sub-tab[data-capi-tab="{tab}"]')
    assert btn.get_attribute("title") and btn.get_attribute("data-tip-i18n")      # info-bulle
    btn.click()
    expect(page.locator(content).first).to_be_visible()
    assert capi_visible(page) == [tab]
    assert page.evaluate("location.hash") == f"#automation/capi/{tab}"
    assert page.evaluate("history.length") == n                                 # clic DANS l'écran
    page.reload(wait_until="domcontentloaded")
    settle(page)
    assert capi_visible(page) == [tab]


@pytest.mark.parametrize("tab,content", [
    ("resources", "#btn-tf-clean-stale"),
    ("history", "#tf-history .form-hint"),
    ("installation", "#tf-prov-form"),
    ("declarations", "#tf-decls-view"),
])
def test_each_terraform_tab_shows_its_content_and_writes_its_address(context, flask_server, tab, content):
    start = "#automation/terraform/installation" if tab == "declarations" else "#automation/terraform"
    page = open_auto(context, flask_server, start)
    expect(page.locator("#tf-status-body [data-tf-pane]").first).to_be_attached()
    n = page.evaluate("history.length")
    btn = page.locator(f'#tab-automation .sub-tab[data-tf-tab="{tab}"]')
    assert btn.get_attribute("title") and btn.get_attribute("data-tip-i18n")
    btn.click()
    expect(page.locator(content).first).to_be_visible()
    assert tf_visible(page) == [tab]
    assert page.evaluate("location.hash") == f"#automation/terraform/{tab}"
    assert page.evaluate("history.length") == n
    page.reload(wait_until="domcontentloaded")
    settle(page)
    expect(page.locator(f'#tf-status-body [data-tf-pane="{tab}"]')).to_be_visible()
    assert tf_visible(page) == [tab]


def test_the_strips_follow_the_automation_sub_tab(context, flask_server):
    page = open_auto(context, flask_server, "#automation/capi/history")
    strips = {k: page.locator(f'#tab-automation .sub-tabs-inline[data-only-for="{k}"]')
              for k in ("capi", "terraform", "pxe")}
    for sub, hnav in (("terraform", "terraform"), ("pxe", "pxe"), ("capi", "capi")):
        page.locator(f'#host-nav [data-hnav-root="automation"][data-hnav-tab="{hnav}"]').click()
        settle(page)
        for k, loc in strips.items():
            (expect(loc).to_be_visible if k == sub else expect(loc).to_be_hidden)()
    # retour par le menu : #automation/capi = Clusters, pas l'onglet quitté
    assert capi_visible(page) == ["clusters"]
    page.evaluate("Router.go('#automation/terraform/history')")
    settle(page)
    assert tf_visible(page) == ["history"]
    page.locator('#host-nav [data-hnav-root="automation"][data-hnav-tab="terraform"]').click()
    settle(page)
    assert tf_visible(page) == ["declarations"]
    # Bare-metal n'a pas d'en-tête de cluster de gestion
    page.locator('#host-nav [data-hnav-root="automation"][data-hnav-tab="pxe"]').click()
    settle(page)
    expect(page.locator("#mgmt-cluster-bar")).to_be_hidden()


def test_a_resource_of_resources_opens_its_declaration(context, flask_server):
    """« Ouvrir la déclaration » depuis Ressources ramène sur Déclarations
    et son adresse (rien de perdu en changeant d'onglet)."""
    page = open_auto(context, flask_server, "#automation/terraform/resources")
    page.route("**/api/terraform/harv-fake/state", lambda r, q: fulfill(r, {
        "initialized": True, "resources": ["harvester_image.a"], "resource_count": 1,
        "resources_detail": [{"address": "harvester_image.a", "local_name": "a", "has_sidecar": True,
                              "kind": "image", "declaration_id": "d0000000001", "declaration_name": "web"}]}))
    page.evaluate("TF.refresh()")
    btn = page.locator('.tf-open-decl[data-decl="d0000000001"]')
    expect(btn).to_be_visible()
    btn.click()
    assert tf_visible(page) == ["declarations"]
    assert page.evaluate("location.hash") == "#automation/terraform/declarations"


# --- bandeaux ----------------------------------------------------------------------------

@pytest.mark.parametrize("info,texts", [
    (dict(INFO_OK, provider_binary=""), ["Harvester provider is missing", "harv-fake"]),
    (dict(INFO_OK, terraform_available=False), ["OpenTofu command is missing"]),
    (dict(INFO_OK, terraform_available=False, terraform_flavor="terraform"), ["Terraform command is missing"]),
    (dict(INFO_OK, provider_binary="", terraform_available=False),
     ["Harvester provider is missing", "OpenTofu command is missing"]),
    (INFO_OK, None),
])
def test_terraform_banner_when_the_provider_or_the_command_is_missing(context, flask_server, info, texts):
    page = open_auto(context, flask_server, "#automation/terraform", info=info)
    expect(page.locator("#tf-decls-view")).to_be_visible()
    box = page.locator('#tf-status-body [data-tf-pane="declarations"] .tf-provider-missing')
    if texts is None:
        expect(box).to_have_count(0)
        # tout est prêt : le bandeau vert est dans Installation
        page.locator('#tab-automation .sub-tab[data-tf-tab="installation"]').click()
        expect(page.locator('#tf-status-body [data-tf-pane="installation"] .summary-bar.ok')).to_be_visible()
        return
    for t in texts:
        expect(box).to_contain_text(t)
    link = box.locator('[data-tf-goto="installation"]')
    assert link.get_attribute("data-tip") and link.get_attribute("href") == "#automation/terraform/installation"
    link.click()
    assert tf_visible(page) == ["installation"]
    assert page.evaluate("location.hash") == "#automation/terraform/installation"
    # Installation dit aussi ce qui manque
    expect(page.locator('#tf-status-body [data-tf-pane="installation"] .summary-bar.warn')).to_be_visible()


@pytest.mark.parametrize("stack,diag,expected", [
    (STACK_MISSING, None, "not installed on harv-fake (missing: infrastructure/harvester)"),
    (STACK_READY, None, None),
    (500, None, None),                                                     # lecture en échec : rien n'est affirmé
    (dict(STACK_READY, turtles=False, core=None, ready=False, missing=["turtles"]),
     {"capi_clusters": [], "components": [{"label": "CAPI core", "installed": True},
                                          {"label": "CAPHV", "installed": False}]}, "missing: CAPHV"),
    (dict(STACK_READY, turtles=False, core=None, ready=False, missing=["turtles"]),
     {"capi_clusters": [], "components": [{"label": "CAPI core", "installed": True}]}, None),
    (dict(STACK_READY, turtles=False, core=None, ready=False, missing=["turtles"]),
     {"unreachable": True, "cluster": "harv-fake"}, "missing: turtles"),
])
def test_capi_banner_when_the_stack_is_not_installed(context, flask_server, stack, diag, expected):
    page = open_auto(context, flask_server, "#automation/capi", stack=stack, diag=diag)
    box = page.locator("#capi-stack-banner .capi-stack-missing")
    if expected is None:
        page.wait_for_timeout(500)
        expect(box).to_have_count(0)
        return
    expect(box).to_contain_text(expected)
    link = box.locator('[data-capi-goto="installation"]')
    assert link.get_attribute("data-tip") and link.get_attribute("href") == "#automation/capi/installation"
    link.click()
    assert capi_visible(page) == ["installation"]
    assert page.evaluate("location.hash") == "#automation/capi/installation"


# --- historique ----------------------------------------------------------------------------

TF_ACT = {
    "tf-plan": {"in_progress": [{"id": "t00000000001", "action": "tf-plan-decl:web:2", "status": "running",
                                 "started_at": 1767000100, "cluster": "harv-fake", "dry_run": True}],
                "actions_done": [{"id": "t00000000009", "action": "vm:delete:default/tf-plan-demo",
                                  "status": "done", "started_at": 1767000000, "ended_at": 1767000001,
                                  "cluster": "harv-fake"}]},
    "tf-apply": {"in_progress": [], "actions_done": [
        {"id": "t00000000002", "action": "tf-apply-decl:web:2", "status": "done", "started_at": 1767000000,
         "ended_at": 1767000090, "cluster": "harv-fake"}]},
    "tf-destroy": {"in_progress": [], "actions_done": [
        {"id": "t00000000003", "action": "tf-destroy-decl:web:2", "status": "done", "dry_run": True,
         "started_at": 1766990000, "ended_at": 1766990020, "cluster": "harv-fake"},
        {"id": "t00000000004", "action": "tf-destroy-resource:harvester_image.a", "status": "error",
         "started_at": 1766980000, "ended_at": 1766980030, "cluster": "harv-fake"}]},
}
CAPI_ACT = {"capi-": {"in_progress": [], "actions_done": [
    {"id": "c00000000001", "action": "capi-cluster-create:default/essai", "status": "done",
     "started_at": 1767000000, "ended_at": 1767000600, "cluster": "harv-fake"},
    {"id": "c00000000002", "action": "capi-install:harv-fake", "status": "error",
     "started_at": 1766990000, "ended_at": 1766990100, "cluster": "harv-fake"},
    {"id": "c00000000009", "action": "vm:start:default/capi-demo", "status": "done",
     "started_at": 1766980000, "ended_at": 1766980001, "cluster": "harv-fake"}]}}


def route_activity(page, answers, asked):
    def activity(route, req):
        q = parse_qs(urlparse(req.url).query).get("action", [""])[0]
        asked.append(q)
        fulfill(route, answers.get(q, {"in_progress": [], "actions_done": []}))
    page.route(re.compile(r".*/api/activity\?.*"), activity)


def test_terraform_history_is_activity_filtered_on_plan_apply_destroy(context, flask_server):
    page = open_auto(context, flask_server, "#automation/terraform")
    asked = []
    route_activity(page, TF_ACT, asked)
    page.locator('#tab-automation .sub-tab[data-tf-tab="history"]').click()
    rows = page.locator("#tf-history tr.tf-history-row")
    expect(rows).to_have_count(4)                          # la VM « tf-plan-demo » n'entre pas
    assert sorted(set(asked)) == ["tf-apply", "tf-destroy", "tf-plan"]
    tables = page.locator("#tf-history table.tf-history")
    expect(tables).to_have_count(2)                        # en cours, puis passées
    expect(tables.nth(0)).to_contain_text("Plan")
    expect(tables.nth(1)).to_contain_text("Apply")
    expect(tables.nth(1)).to_contain_text("Destroy plan")
    expect(tables.nth(1)).to_contain_text("Destroy a resource")
    expect(tables.nth(1)).to_contain_text("harv-fake")
    for i in range(4):
        link = rows.nth(i).locator("a.tf-history-log")
        assert link.get_attribute("href") == f"#activity/action/{rows.nth(i).get_attribute('data-action-id')}"
        assert link.get_attribute("data-tip")
    rows.nth(1).locator("a.tf-history-log").click()
    page.wait_for_function("() => location.hash === '#activity/action/t00000000002'")
    settle(page)
    expect(page.locator("#tab-activity")).to_have_class(re.compile(r"\bactive\b"))


def test_capi_history_is_activity_filtered_on_capi_actions(context, flask_server):
    page = open_auto(context, flask_server, "#automation/capi")
    asked = []
    route_activity(page, CAPI_ACT, asked)
    page.locator('#tab-automation .sub-tab[data-capi-tab="history"]').click()
    rows = page.locator("#capi-history-body tr.capi-history-row")
    expect(rows).to_have_count(2)                          # la VM « capi-demo » n'entre pas
    assert set(asked) == {"capi-"}
    expect(rows.nth(0)).to_contain_text("Create a cluster")
    expect(rows.nth(0)).to_contain_text("default/essai")
    expect(rows.nth(1)).to_contain_text("Install the stack")
    assert rows.nth(0).locator("a.capi-history-log").get_attribute("href") == "#activity/action/c00000000001"
    # Rafraîchir relit l'Activité
    n = len(asked)
    page.locator("#btn-capi-history-refresh").click()
    expect(rows).to_have_count(2)
    page.wait_for_timeout(300)
    assert len(asked) > n


@pytest.mark.parametrize("hash,box,status,body,expected", [
    ("#automation/terraform/history", "#tf-history", 500, {"error": "x"}, "Activity could not be read."),
    ("#automation/terraform/history", "#tf-history", 200, {"in_progress": [], "actions_done": []},
     "No Terraform plan, apply or destroy yet."),
    ("#automation/capi/history", "#capi-history-body", 500, {"error": "x"}, "Activity could not be read."),
    ("#automation/capi/history", "#capi-history-body", 200, {"in_progress": [], "actions_done": []},
     "No Cluster API action yet."),
])
def test_history_error_and_empty_paths(context, flask_server, hash, box, status, body, expected):
    page = open_auto(context, flask_server, hash.rsplit("/", 1)[0])
    page.route(re.compile(r".*/api/activity\?.*"), lambda r, q: fulfill(r, body, status))
    page.evaluate(f"Router.go('{hash}')")
    settle(page)
    expect(page.locator(box)).to_contain_text(expected)
    expect(page.locator(f"{box} tr.auto-history-row")).to_have_count(0)


@pytest.mark.parametrize("hash,box", [
    ("#automation/terraform/history", "#tf-history"),
    ("#automation/capi/history", "#capi-history-body"),
])
def test_history_escapes_what_it_shows(context, flask_server, hash, box):
    page = open_auto(context, flask_server, hash.rsplit("/", 1)[0])
    verb = "tf-apply-decl" if "terraform" in hash else "capi-install"
    evil = {"in_progress": [], "actions_done": [{"id": "e00000000001", "status": "done", "started_at": 1,
                                                 "action": verb + ':<img src=x onerror="window.__pwn=1">',
                                                 "cluster": '<b id="pwn2">c</b>'}]}
    page.route(re.compile(r".*/api/activity\?.*"), lambda r, q: fulfill(r, evil))
    page.evaluate(f"Router.go('{hash}')")
    settle(page)
    expect(page.locator(f"{box} tr.auto-history-row")).to_have_count(1)
    assert page.locator(f"{box} img").count() == 0 and page.locator("#pwn2").count() == 0
    assert page.evaluate("window.__pwn") is None


@pytest.mark.parametrize("hash,answers,leave", [
    ("#automation/terraform/history", TF_ACT, "#vms"),
    ("#automation/capi/history", {"capi-": {"in_progress": [
        {"id": "c00000000005", "action": "capi-cluster-scale:default/essai->3", "status": "running",
         "started_at": 1767000000, "cluster": "harv-fake"}], "actions_done": []}}, "#vms"),
    # changer d'onglet DANS l'écran arrête aussi la relecture
    ("#automation/capi/history", {"capi-": {"in_progress": [
        {"id": "c00000000006", "action": "capi-cluster-delete:default/essai", "status": "running",
         "started_at": 1767000000, "cluster": "harv-fake"}], "actions_done": []}}, "#automation/capi/services"),
    ("#automation/terraform/history", TF_ACT, "#automation/terraform/resources"),
])
def test_history_reads_a_running_action_again_only_while_shown(context, flask_server, hash, answers, leave):
    page = open_auto(context, flask_server, hash.rsplit("/", 1)[0])
    asked = []
    route_activity(page, answers, asked)
    page.evaluate(f"Router.go('{hash}')")
    settle(page)
    expect(page.locator("tr.auto-history-row:visible").first).to_be_visible()
    first = len(asked)
    page.wait_for_timeout(5600)
    assert len(asked) > first                              # relue : une action tourne
    page.evaluate(f"Router.go('{leave}')")
    settle(page)
    page.wait_for_timeout(600)
    n = len(asked)
    page.wait_for_timeout(5600)
    assert len(asked) == n                                 # écran ou onglet quitté : plus de relecture


def test_mutation_a_history_that_ignores_the_shown_check_is_seen(context, flask_server):
    """Mutation volontaire : un historique qui relit sans regarder si son
    onglet est affiché continue après qu'on a quitté l'écran."""
    page = open_auto(context, flask_server, "#automation/capi")
    asked = []
    route_activity(page, {"capi-": {"in_progress": [
        {"id": "c00000000007", "action": "capi-install:harv-fake", "status": "running", "started_at": 1,
         "cluster": "harv-fake"}], "actions_done": []}}, asked)
    page.evaluate("""() => {
      const h = AutoHistory.create({ box: () => document.querySelector('#capi-history-body'), queries: ['capi-'],
        cls: 'capi', shown: () => true, label: (a) => a.action, hint: () => '', empty: () => '' });
      h.render();
    }""")
    page.wait_for_timeout(400)
    page.evaluate("Router.go('#vms')")
    settle(page)
    page.wait_for_timeout(600)
    n = len(asked)
    page.wait_for_timeout(5600)
    with pytest.raises(AssertionError):
        assert len(asked) == n
