"""2.4.0 : interface des plans de déploiement (source et node).

* onglet Plans de Bare-metal entre Profils et Images (bmc.js, index.html) ;
* historique : les déroulés `deploy:` et leur libellé ;
* page d'hôte : « Appliquer un plan » à côté de « Joindre un nœud » ;
* bm-plans.js exécuté sous node : constats regroupés par phase, bloquants
  d'abord, code inconnu montré tel quel et échappé ; bouton « Reprendre »
  seulement sur un déroulé arrêté ; identifiants d'actions filles liés ;
* bulle d'aide sur chaque bouton, SSE par SSEReconnect, aucun tiret
  cadratin ni flèche ;
* mutations volontaires : chaque garde est prouvée par une copie cassée.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"
PLANS = JS / "bm-plans.js"
HTML = (ROOT / "web" / "templates" / "index.html").read_text(encoding="utf-8")

FINDING_CODES = ["plan-invalid", "phase-not-available", "template-missing", "var-missing", "var-invalid",
                 "decl-name-taken", "tf-missing", "cluster-unreachable", "template-newer", "decl-differs",
                 "plan-changed", "decl-exists", "readiness-later", "admin-kind", "forbidden-construct",
                 # 2.5.0 : phases Cluster API et Clusters (constats de flotte compris)
                 "capi-bundle-missing", "capi-bundle-no-turtles", "capi-needs-harvester-1.9", "capi-later",
                 "capi-version-differs", "capi-stack-missing", "capi-template-missing", "cluster-var-missing",
                 "cluster-name-taken", "cluster-exists", "identity-rules-unknown", "refs-later",
                 "cluster-failed", "capi-check-failed", "capi-install-admin",
                 # identité restreinte, copiés de check --many (2.5.0, revue)
                 "identity-name-reserved", "target-namespace-forced", "cloud-provider-binding-taken",
                 "vm-namespace-taken", "vm-namespace-too-long",
                 "fleet-name-twice", "fleet-cpu-short", "fleet-memory-short", "fleet-storage-short",
                 "fleet-storage-unknown",
                 "fleet-replicas-nodes", "fleet-pool-short", "fleet-dhcp-leases", "fleet-sizes",
                 "rights-missing", "rights-unchecked",
                 # 2.6.0 : phase Registre (constats de la console et des outils de registre)
                 "registry-name-taken", "registry-unknown", "registry-admin", "registry-check-failed",
                 "registry-package-too-big", "registry-address-differs", "artefact-package-missing",
                 "package-untrusted-strict", "registry-var-missing", "registry-exists", "registry-later",
                 "publish-later", "cluster-registry-later", "registry-address-answers", "registry-address-in-pool",
                 "registry-address-vip", "registry-room-short", "registry-not-attached", "registry-unreachable",
                 "publish-tag-differs", "cluster-registry-not-attached", "cluster-registry-no-fallback-system",
                 "registry-vm-network-unchecked", "cluster-registry-vm-network-unchecked"]  # revue finale I-4


# --- onglet, historique, page d'hôte (source) ----------------------------------

def test_plans_tab_sits_between_profiles_and_images():
    src = (JS / "bmc.js").read_text()
    assert "const TABS = ['machines', 'profiles', 'plans', 'images', 'history'];" in src
    assert '<div class="bm-pane" data-bm-pane="plans" role="tabpanel" hidden><div id="bm-plans"></div></div>' in src
    assert "BMPlans.render()" in src
    order = re.findall(r'data-bm-tab="([a-z]+)"', HTML)
    assert order == ["machines", "profiles", "plans", "images", "history"]
    btn = HTML.split('data-bm-tab="plans"', 1)[1].split("</button>", 1)[0]
    assert 'data-tip-i18n="bmt.plansTip"' in btn and 'title="' in btn


def test_history_lists_deployment_runs_with_their_label():
    src = (JS / "bmc.js").read_text()
    assert "const HISTORY_KINDS = ['baremetal-', 'bmc-power', 'deploy'];" in src
    assert "case 'deploy': return tr('bmh.kind.deploy');" in src
    assert "BMPlans.resumeButton(a)" in src
    hist = (JS / "auto-history.js").read_text()
    assert "${o.extra ? o.extra(a) : ''}" in hist and "get: (id) => byId.get(id) || null" in hist


def test_scripts_and_host_button_are_in_the_page():
    assert HTML.index('/static/js/bm-profiles.js') < HTML.index('/static/js/bm-plans.js')
    block = HTML.split('id="btn-join-node"', 1)[1].split("</div>", 1)[0]
    assert 'id="btn-apply-plan"' in block                      # à côté de Joindre un nœud
    btn = HTML.split('id="btn-apply-plan"', 1)[1].split("</button>", 1)[0]
    assert 'data-tip-i18n="hp.applyPlanTip"' in btn and "needs-admin" not in btn.split(">", 1)[0]
    hp = (JS / "host-page.js").read_text()
    assert "closest('#btn-apply-plan')" in hp and "BMPlans.openDeploy({ cluster: c })" in hp
    assert "tr('hp.applyPlanViewer')" in hp and "'operator'" in hp
    assert "document.addEventListener('hops:role', paintApplyPlan)" in hp


def test_install_and_batch_windows_carry_the_plan():
    bmc = (JS / "bmc.js").read_text()
    prof = (JS / "bm-profiles.js").read_text()
    assert "BMPlans.mountPicker(form.querySelector('.bm-plan-pick'), '', true)" in bmc
    assert "BMPlans.mountPicker(form.querySelector('.bmp-plan-pick'), prof.plan || '', true)" in prof   # série
    assert "BMPlans.mountPicker(form.querySelector('.bmp-plan-pick'), prof.plan || '', false)" in prof  # éditeur
    for src in (bmc, prof):
        assert "BMPlans.actionLink(" in src and "'blocking-findings'" in src


def test_save_as_template_goes_through_the_store():
    store = (JS / "tf-declarations.js").read_text()
    assert "'/api/baremetal/tf-templates/from-declaration'" in store and "saveAsTemplate," in store
    view = (JS / "tf-decl-view.js").read_text()
    assert "TFDecl.saveAsTemplate(d.id" in view and "familyOf(d) === 'harvester'" in view


# --- règles d'interface (source) -------------------------------------------------

def _buttons(src):
    return re.findall(r"<button\b[^>]*>", src, re.S)


@pytest.mark.parametrize("path", [PLANS])
def test_every_button_has_a_tooltip(path):
    src = path.read_text()
    tags = _buttons(src)
    assert len(tags) >= 20
    # le bouton Lancer porte sa raison en title/aria-label, posés par paintLaunch()
    missing = [t for t in tags if "data-tip" not in t and "dpw-launch" not in t]
    assert missing == []
    assert "btn.title = why;" in src and "dataset.tip = why" in src


def test_sse_goes_through_the_reconnecting_helper():
    src = PLANS.read_text()
    assert "SSEReconnect.connect(" in src and "new EventSource(" not in src


@pytest.mark.parametrize("path", ["bm-plans.js", "bmc.js", "bm-profiles.js", "host-page.js", "tf-decl-view.js",
                                  "tf-declarations.js", "auto-history.js", "i18n.js", "routes.js"])
def test_no_em_dash_nor_unicode_arrow_in_new_text(path):
    added = subprocess.run(["git", "diff", "3ddba27", "--", f"web/static/js/{path}"], capture_output=True,
                           text=True, cwd=str(ROOT)).stdout
    plus = "\n".join(ln for ln in added.splitlines() if ln.startswith("+"))
    assert "\u2014" not in plus and "\u2192" not in plus


def test_every_finding_code_has_a_label():
    src = PLANS.read_text()
    table = src.split("const FINDINGS = {", 1)[1].split("};", 1)[0]
    assert sorted(re.findall(r"'([a-z0-9.-]+)': \(\) => tr\('dpw\.f\.", table)) == sorted(FINDING_CODES)


# --- bm-plans.js sous node ---------------------------------------------------------

RUNNER = r"""
const fs = require('fs');
global.window = global;
global.document = { addEventListener() {} };
global.localStorage = { getItem() { return null; } };
global.i18n = { t: (k, p) => k + (p ? JSON.stringify(p) : '') };
const inp = JSON.parse(fs.readFileSync(0, 'utf8'));
global.App = { role: () => inp.role };
const BMPlans = new Function(fs.readFileSync(process.argv[1], 'utf8') + '\n;return BMPlans;')();
const out = {};
if (inp.findings) out.findings = BMPlans.findingsHtml(inp.findings);
if (inp.actions) out.resume = inp.actions.map(a => BMPlans.resumeButton(a));
if (inp.message) out.linked = BMPlans.linkIds(inp.message);
console.log(JSON.stringify(out));
"""


def run(inp, path=PLANS):
    try:
        r = subprocess.run(["node", "-e", RUNNER, str(path)], input=json.dumps(inp), capture_output=True,
                           text=True, cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node absent")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


FINDINGS = [
    {"code": "decl-exists", "level": "info", "phase": "terraform", "detail": "site-net exists"},
    {"code": "readiness-later", "level": "info", "phase": "readiness", "detail": ""},
    {"code": "var-missing", "level": "blocking", "phase": "terraform",
     "detail": "templates[net].resources[0].spec.vlan_id: missing variable"},
    {"code": "template-newer", "level": "warning", "phase": "terraform", "detail": "net 2 > 1"},
    {"code": "x-<b>new</b>", "level": "warning", "phase": "plan", "detail": "<img src=x onerror=alert(1)>"},
]


def test_findings_are_grouped_by_phase_blocking_first():
    html = run({"findings": FINDINGS, "role": "operator"})["findings"]
    groups = re.findall(r'data-phase="([a-z]+)"', html)
    assert groups == ["terraform", "plan", "readiness"]          # le groupe à constat bloquant d'abord
    tf = html.split('data-phase="terraform"', 1)[1].split('data-phase="plan"', 1)[0]
    codes = re.findall(r'data-code="([^"]+)"', tf)
    assert codes == ["var-missing", "template-newer", "decl-exists"]
    assert "dpw.f.varMissing" in tf and "dpw.lvl.blocking" in tf and 'class="badge fail"' in tf


def test_unknown_code_is_shown_as_is_and_escaped():
    html = run({"findings": FINDINGS, "role": "operator"})["findings"]
    assert "x-&lt;b&gt;new&lt;/b&gt;" in html and "<b>new</b>" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html and "<img" not in html


def test_no_finding_says_so():
    assert "dpw.noFinding" in run({"findings": [], "role": "operator"})["findings"]


@pytest.mark.parametrize("action,role,shown,enabled", [
    ({"id": "a1", "action": "deploy:harvlab", "status": "error"}, "operator", True, True),
    ({"id": "a1", "action": "deploy:harvlab", "status": "cancelled"}, "admin", True, True),
    ({"id": "a1", "action": "deploy:harvlab", "status": "interrupted"}, "operator", True, True),
    ({"id": "a1", "action": "deploy:harvlab", "status": "error"}, "viewer", True, False),
    ({"id": "a1", "action": "deploy:harvlab", "status": "done"}, "operator", False, False),
    ({"id": "a1", "action": "deploy:harvlab", "status": "running"}, "operator", False, False),
    ({"id": "a1", "action": "deploy:harvlab", "status": "error", "dry_run": True}, "operator", False, False),
    ({"id": "a1", "action": "baremetal-install:n1", "status": "error"}, "operator", False, False),
])
def test_resume_button_only_on_a_stopped_run(action, role, shown, enabled):
    btn = run({"actions": [action], "role": role})["resume"][0]
    assert bool(btn) == shown
    if shown:
        assert 'data-action-id="a1"' in btn and "data-tip=" in btn
        assert ("disabled" not in btn) == enabled
        assert ("dpw.t.viewer" in btn) == (not enabled)


def test_child_action_ids_are_linked_and_the_message_escaped():
    out = run({"message": "apply failed (action 0123456789ab) <script>x</script>", "role": "operator"})["linked"]
    assert 'href="#activity/action/0123456789ab"' in out
    assert "&lt;script&gt;" in out and "<script>" not in out


def test_mutation_unescaped_detail_is_seen(tmp_path):
    """Mutation volontaire : le détail d'un constat sans échappement est vu."""
    src = PLANS.read_text()
    broken = src.replace("<code class=\"dpw-detail\">${esc(f.detail)}</code>", "<code class=\"dpw-detail\">${f.detail}</code>")
    assert broken != src
    p = tmp_path / "bm-plans.js"
    p.write_text(broken)
    html = run({"findings": FINDINGS, "role": "operator"}, p)["findings"]
    with pytest.raises(AssertionError):
        assert "<img" not in html


def test_mutation_resume_on_a_finished_run_is_seen(tmp_path):
    """Mutation volontaire : « Reprendre » offert sur un déroulé terminé est vu."""
    src = PLANS.read_text()
    broken = src.replace("const RESUMABLE = ['error', 'cancelled', 'interrupted'];",
                         "const RESUMABLE = ['error', 'cancelled', 'interrupted', 'done'];")
    assert broken != src
    p = tmp_path / "bm-plans.js"
    p.write_text(broken)
    btn = run({"actions": [{"id": "a1", "action": "deploy:h", "status": "done"}], "role": "operator"}, p)["resume"][0]
    with pytest.raises(AssertionError):
        assert not btn
