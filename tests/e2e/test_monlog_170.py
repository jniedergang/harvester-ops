"""v1.70.0 : la section Monitoring & Logging dans un navigateur : métriques
(instantané, Prometheus), sorties (formulaire par type, secret saisi),
flux (sorties proposées selon le namespace et la nature), Alertmanager
(receiver et route), états et suppression bloquée d'une sortie utilisée."""

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402
from nav import address, goto  # noqa: E402

SHAPES = {"loki": {"text": ["url", "tenant", "username"], "secret": ["password"], "bool": ["configure_kubernetes_labels"]},
          "nullout": {}, "file": {"text": ["path"], "example": {"path": "/tmp/logs/${tag}/%Y/%m/%d.%H.%M"}}}
DATA = {"cluster": "harv-fake", "logging": {"enabled": True, "status": "AddonDeploySuccessful"},
        "monitoring": {"enabled": False, "status": "AddonDisabled", "alertmanager": True},
        "outputs": [{"kind": "Output", "namespace": "apps", "name": "loki", "types": ["loki"], "audit": False, "state": "applied",
                     "problems": [], "secrets": ["c"], "fields": {"url": "http://loki:3100"}, "secret_refs": {"password": {"name": "c", "key": "p"}},
                     "server": {}, "system": False, "created": None},
                    {"kind": "ClusterOutput", "namespace": "cattle-logging-system", "name": "audit-file", "types": ["file"], "audit": True,
                     "state": "problems", "problems": ["no output target configured"], "secrets": [], "fields": {"path": "/tmp/a"},
                     "secret_refs": {}, "server": {}, "system": False, "created": None}],
        "flows": [{"kind": "Flow", "namespace": "apps", "name": "web", "type": "logging", "local": ["loki"], "global": [],
                   "rules": [], "filters": 0, "state": "applied", "problems": [], "system": False, "created": None}],
        "loggings": [{"name": "rancher-logging-root", "loggingRef": "", "ok": True, "failed": []}],
        "amcs": [], "namespaces": ["apps", "cattle-monitoring-system", "default"], "shapes": SHAPES,
        "receiver_types": ["webhook", "slack", "email", "pagerduty", "opsgenie", "msteams"]}
METRICS = {"cluster": "harv-fake", "monitoring": False, "prometheus": None,
           "hosts": [{"name": "n1", "cpu": 3.29, "memory": 32 * 2 ** 30, "cpu_total": 8, "memory_total": 62 * 2 ** 30}],
           "vms": [{"namespace": "default", "name": "web", "cpu": 1.455, "vcpus": 2, "cpu_share": 0.7275, "memory": 8 * 2 ** 30}]}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def ui(context, flask_server):
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    sent = []

    def writes(route, req):
        sent.append((req.url.split("://")[1].split("/", 1)[1], req.post_data_json))
        fulfill(route, {"action_id": "ml0000000170"}, 202)
    page.route("**/api/monlog/harv-fake", lambda r, q: fulfill(r, DATA))
    page.route("**/api/monlog/harv-fake/metrics", lambda r, q: fulfill(r, METRICS))
    page.route("**/api/monlog/harv-fake/do/**", writes)
    page.route("**/api/stream/ml0000000170", lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    goto(page, flask_server["base_url"], address("namespaces"))
    page.wait_for_function("window.MonLog && window.Sections && window.App && App.getCurrentCluster()")
    page.on("dialog", lambda d: d.accept())
    return page, sent


def pane(page, kind):
    page.evaluate(f"Sections.open('monlog', '{kind}')")
    return page.locator(f'#tab-monlog .section-pane[data-pane="{kind}"] [data-ml="body"]')


def test_the_section_and_its_metrics(ui):
    page, _ = ui
    body = pane(page, "metrics")
    # 2.0.0 : l'onglet Monitoring de la page d'hôte porte l'info-bulle tab.monlogTip
    tab = page.locator('#host-page-head .hp-tabs a[data-hp-tab="monlog"]')
    expect(tab).to_be_visible()
    assert tab.get_attribute("title") == page.evaluate("i18n.t('tab.monlogTip')")
    expect(body.locator('[data-ml="open-addons"]')).to_be_visible(timeout=8000)          # monitoring éteint, dit
    expect(body).to_contain_text("72.8 %")                                               # part des vCPU, pas le / 1000


def test_outputs_show_their_state_and_a_used_one_cannot_be_deleted(ui):
    page, sent = ui
    body = pane(page, "outputs")
    loki = body.locator('tr[data-name="loki"]')
    expect(loki.locator(".badge.ok")).to_have_count(1, timeout=8000)
    expect(loki.locator('[data-ml="del-output"]')).to_be_disabled()                      # un flux s'en sert
    expect(body.locator('tr[data-name="audit-file"] .badge.fail')).to_have_count(1)
    page.locator('#tab-monlog [data-ml="new-output"]').click()
    w = page.locator("#fp-ml-out-harv-fake-new")
    w.locator('[name="type"]').select_option("file")
    expect(w.locator('[name="f-path"]')).to_have_value("/tmp/logs/${tag}/%Y/%m/%d.%H.%M")   # fluentd exige ${tag}
    w.locator('[name="type"]').select_option("loki")
    w.locator('[name="name"]').fill("loki2")
    w.locator('[name="namespace"]').select_option("apps")
    w.locator('[name="f-url"]').fill("http://loki2:3100")
    w.locator('[name="s-password-name"]').fill("loki2-cred")
    w.locator('[name="s-password-key"]').fill("password")
    w.locator('[name="s-password-value"]').fill("hunter2")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, body_ = sent[-1]
    assert path == "api/monlog/harv-fake/do/output-apply"
    assert body_["spec"]["fields"]["url"] == "http://loki2:3100" and body_["spec"]["namespace"] == "apps"
    assert body_["spec"]["secrets"]["password"] == {"name": "loki2-cred", "key": "password", "value": "hunter2"}


def test_a_flow_offers_the_outputs_of_its_namespace_and_kind(ui):
    page, sent = ui
    pane(page, "flows")
    page.locator('#tab-monlog [data-ml="new-flow"]').click()
    w = page.locator("#fp-ml-flow-harv-fake-new")
    w.locator('[name="name"]').fill("web2")
    w.locator('[name="namespace"]').select_option("apps")
    expect(w.locator('[name="out"]')).to_have_count(1)                                   # loki ; pas la sortie d'audit
    w.locator('[name="type"]').select_option("audit")
    expect(w.locator('[name="out"]')).to_have_count(1)
    expect(w.locator('[name="out"]')).to_have_value("ClusterOutput/audit-file")
    w.locator('[name="type"]').select_option("logging")
    w.locator('[name="out"]').check()
    w.locator("[data-rule-add]").click()
    w.locator('[name="r-labels"]').fill("app=web")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, b = sent[-1]
    assert path == "api/monlog/harv-fake/do/flow-apply"
    assert b["spec"]["local"] == ["loki"] and b["spec"]["rules"][0]["labels"] == "app=web" and b["spec"]["type"] == "logging"


def test_an_alertmanager_config_with_its_receiver_and_route(ui):
    page, sent = ui
    body = pane(page, "alerts")
    expect(body.locator('[data-ml="open-addons"]')).to_be_visible(timeout=8000)
    page.locator('#tab-monlog [data-ml="new-amc"]').click()
    w = page.locator("#fp-ml-amc-harv-fake-new")
    w.locator('[name="name"]').fill("ops")
    w.locator('[name="rname"]').fill("hook")
    w.locator('[name="rf-url"]').fill("http://10.0.0.9/alert")
    w.locator('[name="matchers"]').fill("severity=~critical|warning")
    w.locator('button[type="submit"]').click()
    page.wait_for_timeout(500)
    path, b = sent[-1]
    assert path == "api/monlog/harv-fake/do/amc-apply"
    assert b["spec"]["namespace"] == "cattle-monitoring-system"
    assert b["spec"]["receivers"] == [{"name": "hook", "type": "webhook", "send_resolved": False, "url": "http://10.0.0.9/alert"}]
    assert b["spec"]["route"]["matchers"] == [{"name": "severity", "matchType": "=~", "value": "critical|warning"}]


def test_a_new_button_clicked_before_the_data_arrives_still_opens_its_form(context, flask_server):
    """Vu en réel : sur un vrai cluster la lecture prend près d'une seconde,
    un clic sur « Nouvelle sortie » avant son arrivée ne faisait rien."""
    context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');")
    page = context.new_page()
    held = []
    page.route("**/api/monlog/harv-fake", lambda r, q: held.append(r))
    goto(page, flask_server["base_url"], address("namespaces"))
    page.wait_for_function("window.MonLog && window.Sections && window.App && App.getCurrentCluster()")
    pane(page, "outputs")
    for _ in range(50):
        if held:
            break
        page.wait_for_timeout(100)
    page.locator('#tab-monlog [data-ml="new-coutput"]').click()
    held.pop().fulfill(status=200, content_type="application/json", body=json.dumps(DATA))
    expect(page.locator("#fp-ml-out-harv-fake-new [name=\"name\"]")).to_be_visible(timeout=8000)
